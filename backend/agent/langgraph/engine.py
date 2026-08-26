"""``AgentEngine`` over a LangChain chat model and the LangGraph turn graph.

Same contract as ``agent.native.loop.NativeEngine``: ``TurnInput`` in,
``TurnResult`` out, events through ``emit``, no table. The behaviours the
native loop spells out in one function -- the wrap-up call, dropped tool
calls after the deadline, the empty-reply nudge, the fixed refusal text --
are reproduced here with the graph doing the looping; the contract tests
hold the two to the same answers.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from agent.contracts import (
    ContentBlock,
    Deadline,
    Emit,
    ProposalReady,
    StopReason,
    TextBlock,
    TextDelta,
    ToolCallRecord,
    ToolRound,
    ToolStarted,
    ToolUseBlock,
    TurnInput,
    TurnResult,
    Usage,
)
from agent.langgraph.graph import PROPOSAL_EVENT, RECURSION_LIMIT, TurnBudget, build_graph
from agent.langgraph.messages import (
    content_blocks,
    stop_reason_of,
    system_message,
    to_langchain,
)
from agent.langgraph.tools import ToolRunner, bound_tools
from agent.model_ids import ModelFamily, family_for, model_id_from_env
from agent.prompt import (
    EMPTY_REPLY_HINT,
    EMPTY_REPLY_TEXT,
    REFUSAL_TEXT,
    SYSTEM_PROMPT,
    user_message_text,
)
from agent.thinking import ThinkingFilter, strip_thinking
from agent.tools.registry import ToolContext, ToolRegistry

logger = logging.getLogger(__name__)


def chat_model_from_env(*, region: str | None = None, model_id: str | None = None) -> BaseChatModel:
    """``ChatBedrockConverse`` on the same model id and residency rule as
    the native provider. Built here, and only here, so that importing the
    runner never imports langchain-aws."""
    from langchain_aws import ChatBedrockConverse

    model_id = model_id or model_id_from_env()
    family_for(model_id)  # refuses an offshore profile
    return ChatBedrockConverse(
        model=model_id,
        region_name=region or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"),
        max_tokens=4096,
        temperature=0.7,
        # The adapter infers what tool_choice a model accepts from its name
        # and its table lags the models; Converse accepts a forced tool on
        # both families this project uses.
        supports_tool_choice_values=("auto", "any", "tool"),
    )


class LangGraphEngine:
    def __init__(
        self,
        model: BaseChatModel,
        tools: ToolRegistry,
        context: ToolContext,
        *,
        model_id: str | None = None,
        system_prompt: str = SYSTEM_PROMPT,
    ) -> None:
        self._model = model
        self._tools = tools
        self._context = context
        self._system_prompt = system_prompt
        resolved = model_id or getattr(model, "model_id", None)
        self._family: ModelFamily | None = family_for(resolved) if resolved else None

    async def run_turn(self, inp: TurnInput, *, deadline: Deadline, emit: Emit) -> TurnResult:
        system = system_message(
            [TextBlock(text=self._system_prompt)]
            + ([TextBlock(text=inp.memory_block)] if inp.memory_block else [])
        )
        history: list[BaseMessage] = [
            *to_langchain(inp.history),
            HumanMessage(content=user_message_text(inp.turn, inp.user_text)),
        ]
        budget = TurnBudget(turn=inp.turn, deadline=deadline)
        runner = ToolRunner(self._tools, self._context)
        graph = build_graph(
            self._model,
            bound_tools(self._tools),
            system=system,
            budget=budget,
            runner=runner,
            family=self._family,
        )

        state = await self._drive(
            graph, {"messages": history, "iterations": 0, "hint": None, "wrap_up": False}, emit
        )
        last = _last_assistant(state["messages"])
        stop_reason = stop_reason_of(last)
        if stop_reason == "refusal":
            return self._refused(runner, budget.usage, inp.turn)
        content = self._clean(content_blocks(last))
        if state["wrap_up"]:
            content, stop_reason = _without_tool_uses(content), "end_turn"

        if runner.finalized is None and not _has_visible_text(content):
            # Nothing the listener could read: one nudge, then a fixed line.
            if not deadline.exhausted():
                messages = list(state["messages"])
                if not content:
                    # An empty assistant message is not something Converse
                    # accepts, so the nudge joins the previous user message.
                    messages.remove(last)
                nudged = await self._drive(
                    graph,
                    {
                        "messages": messages,
                        # Wrap-up mode: tools stay on the request (history
                        # holds tool blocks) but a tool call is not honoured.
                        "iterations": RECURSION_LIMIT,
                        "hint": EMPTY_REPLY_HINT,
                        "wrap_up": False,
                    },
                    emit,
                )
                reply = _last_assistant(nudged["messages"])
                if stop_reason_of(reply) == "refusal":
                    return self._refused(runner, budget.usage, inp.turn)
                content, stop_reason = (
                    _without_tool_uses(self._clean(content_blocks(reply))),
                    "end_turn",
                )
            if not _has_visible_text(content):
                logger.info("empty reply replaced with the fallback turn=%d", inp.turn)
                content, stop_reason = [TextBlock(text=EMPTY_REPLY_TEXT)], "end_turn"

        if runner.finalized is not None:
            stop_reason = "end_turn"
        logger.info(
            "turn done turn=%d iterations=%d tools=%d stop_reason=%s finalized=%s engine=langgraph",
            inp.turn,
            state["iterations"],
            len(runner.tool_log),
            stop_reason,
            runner.finalized is not None,
        )
        return TurnResult(
            content=content,
            rounds=runner.rounds,
            tool_log=runner.tool_log,
            usage=budget.usage,
            stop_reason=stop_reason,
            finalized=runner.finalized,
            proposal=runner.proposal,
        )

    async def _drive(self, graph: Any, state: dict[str, Any], emit: Emit) -> dict[str, Any]:
        """Run the graph, turning its event stream into the contract's
        events as they happen. ``astream_events`` puts model chunks, our
        custom proposal event and the node lifecycle on one ordered
        queue, which is what keeps a proposal after the text that preceded
        it."""
        thinking: ThinkingFilter | None = None
        final: dict[str, Any] | None = None
        async for event in graph.astream_events(
            state, version="v2", config={"recursion_limit": RECURSION_LIMIT}
        ):
            kind = event["event"]
            if kind == "on_chat_model_start":
                thinking = ThinkingFilter() if self._family is ModelFamily.NOVA else None
            elif kind == "on_chat_model_stream":
                chunk = event["data"]["chunk"]
                text = _chunk_text(chunk)
                if text and thinking is not None:
                    text = thinking.delta(text)
                if text:
                    await emit(TextDelta(text))
                for call in getattr(chunk, "tool_call_chunks", None) or []:
                    if call.get("name"):
                        await emit(ToolStarted(call["name"]))
            elif kind == "on_custom_event" and event["name"] == PROPOSAL_EVENT:
                await emit(ProposalReady(event["data"]["duration_minutes"]))
            elif kind == "on_chain_end" and event.get("parent_ids") == []:
                # The graph itself finishing: its output is the final state.
                final = event["data"]["output"]
        assert final is not None, "the graph ended without an output"
        return final

    def _clean(self, content: list[ContentBlock]) -> list[ContentBlock]:
        if self._family is not ModelFamily.NOVA:
            return content
        cleaned: list[ContentBlock] = []
        for block in content:
            if isinstance(block, TextBlock):
                text = strip_thinking(block.text)
                if text:
                    cleaned.append(TextBlock(text=text))
            else:
                cleaned.append(block)
        return cleaned

    @staticmethod
    def _refused(runner: ToolRunner, usage: Usage, turn: int) -> TurnResult:
        logger.info("turn refused turn=%d engine=langgraph", turn)
        return TurnResult(
            content=[TextBlock(text=REFUSAL_TEXT)],
            rounds=runner.rounds,
            tool_log=runner.tool_log,
            usage=usage,
            stop_reason="refusal",
        )


def _last_assistant(messages: list[BaseMessage]) -> AIMessage:
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            return message
    raise RuntimeError("the graph ended without an assistant message")


def _chunk_text(chunk: Any) -> str:
    content = getattr(chunk, "content", "")
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for raw in content:
        if isinstance(raw, str):
            parts.append(raw)
        elif raw.get("type") == "text" and raw.get("text"):
            parts.append(raw["text"])
    return "".join(parts)


def _has_visible_text(content: list[ContentBlock]) -> bool:
    return any(isinstance(b, TextBlock) and b.text.strip() for b in content)


def _without_tool_uses(content: list[ContentBlock]) -> list[ContentBlock]:
    kept: list[ContentBlock] = [b for b in content if not isinstance(b, ToolUseBlock)]
    return kept or [TextBlock(text=EMPTY_REPLY_TEXT)]


__all__ = [
    "LangGraphEngine",
    "StopReason",
    "ToolCallRecord",
    "ToolRound",
    "chat_model_from_env",
]
