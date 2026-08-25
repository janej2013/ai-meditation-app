"""A scripted ``LLMProvider`` for the native loop's tests.

Each ``stream_turn`` call plays the next scripted event list and records
what it was asked, so a test can assert on exactly the request shape the
loop built (messages, tools, tool choice) without a model in the loop.
Being asked more often than scripted is itself a failure: it means the
loop made a call the scenario did not expect.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Coroutine
from dataclasses import dataclass
from typing import Any

from agent.contracts import (
    ContentBlock,
    ConverseToolSpec,
    Final,
    LLMEvent,
    Message,
    SystemBlock,
    TextBlock,
    TextDelta,
    ToolChoice,
    ToolUseBlock,
    ToolUseStart,
    Usage,
)

ONE_CALL = Usage(input_tokens=10, output_tokens=5)


@dataclass
class ProviderCall:
    system: list[SystemBlock]
    messages: list[Message]
    tools: list[ConverseToolSpec]
    tool_choice: ToolChoice | None

    @property
    def last_user_text(self) -> str:
        blocks = [b for b in self.messages[-1].content if isinstance(b, TextBlock)]
        return "\n".join(b.text for b in blocks)


class FakeProvider:
    def __init__(self, script: list[list[LLMEvent]]) -> None:
        self._script = [list(events) for events in script]
        self.calls: list[ProviderCall] = []

    @property
    def remaining(self) -> int:
        return len(self._script)

    async def stream_turn(
        self,
        system: list[SystemBlock],
        messages: list[Message],
        tools: list[ConverseToolSpec],
        *,
        tool_choice: ToolChoice | None,
    ) -> AsyncIterator[LLMEvent]:
        assert self._script, "provider called more often than the scenario scripted"
        self.calls.append(
            ProviderCall(
                system=list(system),
                messages=[m.model_copy(deep=True) for m in messages],
                tools=list(tools),
                tool_choice=tool_choice,
            )
        )
        for event in self._script.pop(0):
            yield event


def text_reply(*chunks: str, usage: Usage = ONE_CALL) -> list[LLMEvent]:
    """A streamed text answer: one delta per chunk, then the whole message."""
    return [
        *(TextDelta(chunk) for chunk in chunks),
        Final(content=[TextBlock(text="".join(chunks))], stop_reason="end_turn", usage=usage),
    ]


def tool_reply(
    *calls: tuple[str, dict[str, Any], str], text: str | None = None, usage: Usage = ONE_CALL
) -> list[LLMEvent]:
    """The model asking for tools: ``(name, input, tool_use_id)`` per call."""
    content: list[ContentBlock] = []
    if text is not None:
        content.append(TextBlock(text=text))
    content.extend(ToolUseBlock(tool_use_id=uid, name=name, input=inp) for name, inp, uid in calls)
    return [
        *(ToolUseStart(name=name, tool_use_id=uid) for name, _, uid in calls),
        Final(content=content, stop_reason="tool_use", usage=usage),
    ]


def refusal_reply(usage: Usage = ONE_CALL) -> list[LLMEvent]:
    return [Final(content=[], stop_reason="refusal", usage=usage)]


def run(coro: Coroutine[Any, Any, Any]) -> Any:
    """Drive a coroutine from a sync test: no async test plugin needed."""
    return asyncio.run(coro)
