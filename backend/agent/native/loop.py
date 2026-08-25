"""The hand-built agent loop: one turn, from user text to assistant answer.

Pure computation over an ``LLMProvider`` and a ``ToolRegistry``. It never
touches the table -- history comes in, a ``TurnResult`` goes out, and the
harness makes it durable. That split is what lets a turn be retried by
simply running it again.

The shape (docs/agent-runner-plan.md §3.2): compose the user message with
this turn's steering, then up to MAX_TOOL_ITERATIONS_PER_TURN model calls,
each followed by every requested tool run in parallel and answered in ONE
user message. A terminal tool ends the turn on its round. Running out of
iterations or time ends it with one more call that asks for a plain answer.
"""

from __future__ import annotations

import asyncio
import logging

from agent.budget import MAX_TOOL_ITERATIONS_PER_TURN, converge_policy
from agent.contracts import (
    ContentBlock,
    Deadline,
    Emit,
    Final,
    Finalized,
    Message,
    TextBlock,
    TextDelta,
    ToolCallRecord,
    ToolChoice,
    ToolRound,
    ToolStarted,
    ToolUseBlock,
    ToolUseStart,
    TurnInput,
    TurnResult,
    Usage,
)
from agent.native.llm.base import LLMProvider
from agent.prompt import NO_MORE_TOOLS_HINT, REFUSAL_TEXT, SYSTEM_PROMPT, user_message_text
from agent.tools.registry import ToolContext, ToolRegistry

logger = logging.getLogger(__name__)

# When the model ignores the no-more-tools hint there is nothing to say for
# it; this keeps the transcript from ending on an empty assistant message.
_FALLBACK_TEXT = "Let's pause here for a moment. What would you like to do next?"


class ProviderProtocolError(Exception):
    """The provider's stream ended without a ``Final`` event."""


class NativeEngine:
    """``AgentEngine`` over a provider, a tool registry and a tool context.

    Built per request: the context carries the caller, and the registry may
    differ per plan. Construction is cheap; the provider is the only thing
    worth reusing across requests, and it is passed in.
    """

    def __init__(
        self,
        provider: LLMProvider,
        tools: ToolRegistry,
        context: ToolContext,
        *,
        system_prompt: str = SYSTEM_PROMPT,
    ) -> None:
        self._provider = provider
        self._tools = tools
        self._context = context
        self._system_prompt = system_prompt

    async def run_turn(self, inp: TurnInput, *, deadline: Deadline, emit: Emit) -> TurnResult:
        system = [TextBlock(text=self._system_prompt)]
        if inp.memory_block:
            system.append(TextBlock(text=inp.memory_block))
        tool_specs = self._tools.to_converse_spec()

        messages = [*inp.history, Message.user_text(user_message_text(inp.turn, inp.user_text))]
        tool_choice: ToolChoice = converge_policy(inp.turn)
        rounds: list[ToolRound] = []
        tool_log: list[ToolCallRecord] = []
        usage = Usage()
        finalized: Finalized | None = None
        final: Final | None = None
        iterations = 0

        for _ in range(MAX_TOOL_ITERATIONS_PER_TURN):
            out_of_time = deadline.exhausted()
            if out_of_time:
                messages[-1] = messages[-1].with_text_appended(NO_MORE_TOOLS_HINT)
                tool_choice = "auto"
            iterations += 1
            final = await self._call(system, messages, tool_specs, tool_choice, emit)
            usage = usage + final.usage

            if final.stop_reason == "refusal":
                return self._refused(rounds, tool_log, usage, inp.turn, iterations)

            tool_uses = [b for b in final.content if isinstance(b, ToolUseBlock)]
            if final.stop_reason != "tool_use" or not tool_uses:
                break
            if out_of_time:
                # Asked for a plain answer and got tool calls anyway. Answer
                # with the text it did produce; an unanswered toolUse would
                # poison the next turn's history.
                final = _without_tool_uses(final)
                break

            executions = await asyncio.gather(
                *(self._tools.execute(self._context, block) for block in tool_uses)
            )
            results = [e.result for e in executions]
            rounds.append(ToolRound(assistant_content=final.content, results=results))
            tool_log.extend(e.record for e in executions)
            messages.append(Message.assistant(final.content))
            messages.append(Message.tool_results(results))

            finalized = next((e.finalized for e in executions if e.finalized), None)
            if finalized is not None:
                break
            # A forced choice applies to the first call only: if the forced
            # tool errored, the model needs to be free to explain rather
            # than being made to call it again.
            tool_choice = "auto"
        else:
            # Every iteration ended in tool calls. One more call, tools kept
            # (Converse requires toolConfig whenever the history holds tool
            # blocks) but the model told to answer; not counted as an
            # iteration because it cannot start another round.
            messages[-1] = messages[-1].with_text_appended(NO_MORE_TOOLS_HINT)
            final = _without_tool_uses(await self._call(system, messages, tool_specs, "auto", emit))
            usage = usage + final.usage
            if final.stop_reason == "refusal":
                return self._refused(rounds, tool_log, usage, inp.turn, iterations)

        assert final is not None  # the loop body ran at least once
        logger.info(
            "turn done turn=%d iterations=%d tools=%d stop_reason=%s finalized=%s",
            inp.turn,
            iterations,
            len(tool_log),
            final.stop_reason,
            finalized is not None,
        )
        return TurnResult(
            content=final.content,
            rounds=rounds,
            tool_log=tool_log,
            usage=usage,
            stop_reason="end_turn" if finalized is not None else final.stop_reason,
            finalized=finalized,
        )

    async def _call(
        self,
        system: list[TextBlock],
        messages: list[Message],
        tools: list[dict],
        tool_choice: ToolChoice,
        emit: Emit,
    ) -> Final:
        """One model call, streamed through ``emit``."""
        async for event in self._provider.stream_turn(
            system, messages, tools, tool_choice=tool_choice
        ):
            if isinstance(event, TextDelta):
                await emit(event)
            elif isinstance(event, ToolUseStart):
                await emit(ToolStarted(event.name))
            elif isinstance(event, Final):
                return event
        raise ProviderProtocolError("provider stream ended without a Final event")

    @staticmethod
    def _refused(
        rounds: list[ToolRound],
        tool_log: list[ToolCallRecord],
        usage: Usage,
        turn: int,
        iterations: int,
    ) -> TurnResult:
        # Fixed text, no retry: the model declined, and that is the answer.
        logger.info("turn refused turn=%d iterations=%d", turn, iterations)
        return TurnResult(
            content=[TextBlock(text=REFUSAL_TEXT)],
            rounds=rounds,
            tool_log=tool_log,
            usage=usage,
            stop_reason="refusal",
        )


def _without_tool_uses(final: Final) -> Final:
    content: list[ContentBlock] = [b for b in final.content if not isinstance(b, ToolUseBlock)]
    if not content:
        content = [TextBlock(text=_FALLBACK_TEXT)]
    return Final(content=content, stop_reason="end_turn", usage=final.usage)
