"""A scripted LangChain chat model: the LangGraph engine's ``FakeProvider``.

It plays the SAME scripts (``LLMEvent`` lists from ``fake_provider``) so a
scenario is written once and run through both engines. Each call streams
its events as ``AIMessageChunk``s the way langchain-aws would -- text as
typed content blocks, tool calls as ``tool_call_chunks`` that aggregate
into ``tool_calls``, the stop reason and usage on the last chunk -- and
records what it was asked, so a test can compare requests across engines
after converting these messages back to the contract's.

Scripts must stream every character of a reply's text as deltas: a
``Final`` whose text was not streamed is a scenario bug, not something to
paper over (it would fire an extra chunk here and no event natively).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessageChunk, BaseMessage
from langchain_core.messages.ai import UsageMetadata
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import ConfigDict, Field

from agent.contracts import Final, TextBlock, TextDelta, ToolUseBlock, ToolUseStart

from .fake_provider import Pause, Raise

_WIRE_STOP = {
    "end_turn": "end_turn",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "refusal": "guardrail_intervened",
}


@dataclass
class ModelCall:
    messages: list[BaseMessage]
    tools: list[Any]
    tool_choice: Any

    @property
    def tool_names(self) -> list[str]:
        names: list[str] = []
        for tool in self.tools:
            if isinstance(tool, dict):
                if "cachePoint" in tool:
                    names.append("<cachePoint>")
                else:
                    names.append(tool.get("name") or tool.get("toolSpec", {}).get("name", "?"))
            else:
                names.append(tool.name)
        return names


class ScriptedChatModel(BaseChatModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    script: list[list[Any]] = Field(default_factory=list)
    calls: list[ModelCall] = Field(default_factory=list)
    model_id: str = "fake-model"

    @property
    def _llm_type(self) -> str:
        return "scripted"

    @property
    def remaining(self) -> int:
        return len(self.script)

    def queue(self, *turns: list) -> None:
        self.script.extend(list(t) for t in turns)

    def bind_tools(self, tools: Any, *, tool_choice: Any = None, **kwargs: Any) -> Any:
        return self.bind(tools=list(tools), tool_choice=tool_choice, **kwargs)

    # -- the calls -----------------------------------------------------

    def _take(self, messages: list[BaseMessage], kwargs: dict[str, Any]) -> list[Any]:
        assert self.script, "model called more often than the scenario scripted"
        self.calls.append(
            ModelCall(
                messages=list(messages),
                tools=list(kwargs.get("tools") or []),
                tool_choice=kwargs.get("tool_choice"),
            )
        )
        return self.script.pop(0)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        del stop, run_manager  # the base class's signature; the script decides
        total: AIMessageChunk | None = None
        for item in self._chunks(self._take(messages, kwargs)):
            if isinstance(item, Raise):
                raise item.exc
            if isinstance(item, Pause):
                continue
            total = item if total is None else total + item
        assert total is not None
        return ChatResult(generations=[ChatGeneration(message=total)])

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        del stop, run_manager  # the base class's signature; the script decides
        for item in self._chunks(self._take(messages, kwargs)):
            if isinstance(item, Raise):
                raise item.exc
            if not isinstance(item, Pause):
                yield ChatGenerationChunk(message=item)

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        del stop, run_manager  # the base class's signature; the script decides
        for item in self._chunks(self._take(messages, kwargs)):
            if isinstance(item, Pause):
                await asyncio.sleep(item.seconds)
            elif isinstance(item, Raise):
                raise item.exc
            else:
                yield ChatGenerationChunk(message=item)

    # -- events -> chunks ----------------------------------------------

    @staticmethod
    def _chunks(events: list[Any]) -> Iterator[AIMessageChunk | Pause | Raise]:
        """One call's events as chunks; ``Pause`` and ``Raise`` pass through
        for the caller to act on in its own (sync or async) way."""
        streamed: list[str] = []
        started: dict[str, int] = {}
        for event in events:
            if isinstance(event, (Pause, Raise)):
                yield event
            elif isinstance(event, TextDelta):
                streamed.append(event.text)
                yield AIMessageChunk(content=[{"type": "text", "text": event.text, "index": 0}])
            elif isinstance(event, ToolUseStart):
                index = len(started) + 1
                started[event.tool_use_id] = index
                # As langchain-aws streams a contentBlockStart: the block in
                # content with its input as a (so far empty) string, and the
                # matching tool_call_chunk.
                yield AIMessageChunk(
                    content=[
                        {
                            "type": "tool_use",
                            "name": event.name,
                            "id": event.tool_use_id,
                            "input": "",
                            "index": index,
                        }
                    ],
                    tool_call_chunks=[
                        {"name": event.name, "args": "", "id": event.tool_use_id, "index": index}
                    ],
                )
            elif isinstance(event, Final):
                text = "".join(b.text for b in event.content if isinstance(b, TextBlock))
                assert text == "".join(streamed), (
                    "scenario bug: a reply's text must be streamed as deltas "
                    f"(final {text!r} vs streamed {''.join(streamed)!r})"
                )
                tool_chunks = []
                tool_content = []
                for block in event.content:
                    if isinstance(block, ToolUseBlock):
                        index = started.get(block.tool_use_id, len(started) + 1)
                        # Only the first chunk of a call names it: LangChain
                        # concatenates ``name`` and ``id`` strings across
                        # chunks, as langchain-aws relies on.
                        announced = block.tool_use_id in started
                        started.setdefault(block.tool_use_id, index)
                        # The input's JSON arrives as string fragments in the
                        # content block too (a delta chunk, minus name/id).
                        tool_content.append(
                            {
                                "type": "tool_use",
                                "input": json.dumps(block.input),
                                "index": index,
                                **(
                                    {}
                                    if announced
                                    else {"name": block.name, "id": block.tool_use_id}
                                ),
                            }
                        )
                        tool_chunks.append(
                            {
                                "name": None if announced else block.name,
                                "args": json.dumps(block.input),
                                "id": None if announced else block.tool_use_id,
                                "index": index,
                            }
                        )
                usage = event.usage
                yield AIMessageChunk(
                    content=tool_content,
                    tool_call_chunks=tool_chunks,
                    response_metadata={"stopReason": _WIRE_STOP[event.stop_reason]},
                    usage_metadata=UsageMetadata(
                        input_tokens=usage.input_tokens,
                        output_tokens=usage.output_tokens,
                        total_tokens=usage.input_tokens + usage.output_tokens,
                        input_token_details={
                            "cache_read": usage.cache_read_tokens,
                            "cache_creation": usage.cache_write_tokens,
                        },
                    ),
                )
            else:
                raise TypeError(f"not an LLM event: {event!r}")
