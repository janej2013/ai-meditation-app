"""Layer 1: one streamed Bedrock Converse call, as the native loop consumes it.

Everything the loop must not know lives here -- the request shape, where
the cache breakpoints go, how a streamed tool call arrives (its JSON input
in string fragments), which failures are worth a retry. The two halves that
matter are pure: ``build_request`` turns the neutral messages into a
Converse request, ``StreamParser`` turns Converse events into ``LLMEvent``s,
and both are tested without a network.

Models: a bare Amazon id (Nova) or an ``au.``-prefixed Claude inference
profile. Nothing else, so that a user's words stay in Australia
(docs/agent-runner-plan.md §12).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
from collections.abc import AsyncIterator, Callable, Iterable, Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import boto3
from botocore.exceptions import ClientError

from agent.contracts import (
    ContentBlock,
    ConverseToolSpec,
    Final,
    ForcedTool,
    LLMEvent,
    Message,
    StopReason,
    SystemBlock,
    TextBlock,
    TextDelta,
    ToolChoice,
    ToolUseBlock,
    ToolUseStart,
    Usage,
)
from shared.pipeline import BEDROCK_TRANSIENT_CODES, BedrockTransientError

logger = logging.getLogger(__name__)

# Known available on demand in ap-southeast-2 (the pipeline runs on it), so
# the CLI works before anyone has looked up a Claude profile id.
DEFAULT_AGENT_MODEL_ID = "amazon.nova-lite-v1:0"
MODEL_ID_ENV = "AGENT_MODEL_ID"
GUARDRAIL_ID_ENV = "AGENT_GUARDRAIL_ID"
GUARDRAIL_VERSION_ENV = "AGENT_GUARDRAIL_VERSION"

# Cross-region profiles that may route outside Australia. Refused outright
# rather than warned about: residency is a product promise.
_FORBIDDEN_PROFILE_PREFIXES = ("us.", "eu.", "apac.", "global.", "jp.", "in.")

# Streamed failures arrive as exception events named in camelCase
# ("throttlingException"); the pipeline's list is the PascalCase API form.
# One extra code: a stream that broke mid-flight is worth one more try.
_STREAM_TRANSIENT_CODES = frozenset({*BEDROCK_TRANSIENT_CODES, "ModelStreamErrorException"})

_CACHE_POINT = {"cachePoint": {"type": "default"}}

_STOP_REASONS: dict[str, StopReason] = {
    "end_turn": "end_turn",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "stop_sequence": "end_turn",
    "guardrail_intervened": "refusal",
    "content_filtered": "refusal",
}


class ModelFamily(StrEnum):
    CLAUDE = "claude"
    NOVA = "nova"


class AgentProviderError(Exception):
    """A model call that will not succeed by retrying: a permanent Bedrock
    error, retries exhausted, or a stream the parser could not make sense
    of. The harness turns it into an error event; the turn is not committed."""


def model_family(model_id: str) -> ModelFamily:
    """Which request dialect a model id needs. The two families differ in
    where a cache breakpoint may sit."""
    if model_id.startswith(_FORBIDDEN_PROFILE_PREFIXES):
        raise ValueError(
            f"{model_id!r} is a cross-region profile that may leave Australia; "
            "use an au. profile or a bare in-region model id"
        )
    if "anthropic." in model_id:
        return ModelFamily.CLAUDE
    if "amazon.nova" in model_id:
        return ModelFamily.NOVA
    raise ValueError(f"unsupported model family for {model_id!r}")


# ----------------------------------------------------------------------
# Request
# ----------------------------------------------------------------------


def build_request(
    *,
    model_id: str,
    family: ModelFamily,
    system: list[SystemBlock],
    messages: list[Message],
    tools: list[ConverseToolSpec],
    tool_choice: ToolChoice | None,
    max_tokens: int,
    temperature: float,
    guardrail: tuple[str, str] | None,
) -> dict[str, Any]:
    """The Converse request for one call. Deterministic in its inputs: the
    order of system blocks and tools is the prompt-cache prefix.

    The first system block is the static prompt, and the breakpoint goes
    right after it; the memory block that follows differs per user and
    would otherwise split the cache per user. Claude also caches the tool
    definitions when a breakpoint closes the tool list; Nova rejects a
    breakpoint there, so for Nova the tools ride uncached.

    ``toolConfig`` is always present: Converse rejects a request whose
    history contains toolUse or toolResult blocks without it.
    """
    system_blocks: list[dict[str, Any]] = []
    for position, block in enumerate(system):
        system_blocks.append({"text": block.text})
        if position == 0:
            system_blocks.append(dict(_CACHE_POINT))

    tool_list: list[dict[str, Any]] = list(tools)
    if family is ModelFamily.CLAUDE:
        tool_list.append(dict(_CACHE_POINT))
    tool_config: dict[str, Any] = {"tools": tool_list}
    if tool_choice == "auto":
        tool_config["toolChoice"] = {"auto": {}}
    elif isinstance(tool_choice, ForcedTool):
        tool_config["toolChoice"] = {"tool": {"name": tool_choice.name}}

    request: dict[str, Any] = {
        "modelId": model_id,
        "system": system_blocks,
        "messages": [m.to_converse() for m in messages],
        "toolConfig": tool_config,
        "inferenceConfig": {"maxTokens": max_tokens, "temperature": temperature},
    }
    if guardrail is not None:
        identifier, version = guardrail
        request["guardrailConfig"] = {
            "guardrailIdentifier": identifier,
            "guardrailVersion": version,
            "streamProcessingMode": "async",
        }
    return request


# ----------------------------------------------------------------------
# Stream
# ----------------------------------------------------------------------


@dataclass
class _Block:
    """One content block as it accumulates over the stream."""

    tool_use_id: str | None = None
    name: str | None = None
    text: list[str] = field(default_factory=list)
    fragments: list[str] = field(default_factory=list)

    @property
    def is_tool(self) -> bool:
        return self.tool_use_id is not None


class StreamParser:
    """Feed Converse stream events in, get ``LLMEvent``s out.

    Incremental on purpose: the provider feeds one event at a time as it
    arrives from the background thread, so text reaches the client while
    the model is still writing. ``parse_stream`` wraps it for the tests.
    """

    def __init__(self) -> None:
        self._blocks: dict[int, _Block] = {}
        self._order: list[int] = []
        self._stop_reason: StopReason | None = None
        self._usage = Usage()
        self.unparseable_tool_inputs = 0

    def feed(self, raw: dict[str, Any]) -> list[LLMEvent]:
        if "contentBlockStart" in raw:
            start = raw["contentBlockStart"]
            block = self._block(start["contentBlockIndex"])
            tool = start.get("start", {}).get("toolUse")
            if tool:
                block.tool_use_id = tool["toolUseId"]
                block.name = tool["name"]
                return [ToolUseStart(name=tool["name"], tool_use_id=tool["toolUseId"])]
            return []
        if "contentBlockDelta" in raw:
            event = raw["contentBlockDelta"]
            block = self._block(event["contentBlockIndex"])
            delta = event.get("delta", {})
            if "text" in delta:
                block.text.append(delta["text"])
                return [TextDelta(delta["text"])]
            if "toolUse" in delta:
                # JSON arrives as string fragments; it is only parseable
                # once the block stops.
                block.fragments.append(delta["toolUse"].get("input", ""))
            return []
        if "messageStop" in raw:
            reason = raw["messageStop"].get("stopReason", "")
            mapped = _STOP_REASONS.get(reason)
            if mapped is None:
                raise AgentProviderError(f"unexpected stopReason {reason!r}")
            self._stop_reason = mapped
            return []
        if "metadata" in raw:
            usage = raw["metadata"].get("usage", {})
            self._usage = Usage(
                input_tokens=usage.get("inputTokens", 0),
                output_tokens=usage.get("outputTokens", 0),
                cache_read_tokens=usage.get("cacheReadInputTokens") or 0,
                cache_write_tokens=usage.get("cacheWriteInputTokens") or 0,
            )
            return []
        # messageStart, contentBlockStop and anything new: nothing to emit.
        return []

    def finish(self) -> Final:
        if self._stop_reason is None:
            raise AgentProviderError("stream ended without messageStop")
        content: list[ContentBlock] = []
        for index in self._order:
            block = self._blocks[index]
            if block.is_tool:
                content.append(
                    ToolUseBlock(
                        tool_use_id=block.tool_use_id or "",
                        name=block.name or "",
                        input=self._tool_input(block),
                    )
                )
            elif block.text:
                content.append(TextBlock(text="".join(block.text)))
        return Final(content=content, stop_reason=self._stop_reason, usage=self._usage)

    def _block(self, index: int) -> _Block:
        if index not in self._blocks:
            self._blocks[index] = _Block()
            self._order.append(index)
        return self._blocks[index]

    def _tool_input(self, block: _Block) -> dict[str, Any]:
        raw = "".join(block.fragments).strip() or "{}"
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
        if not isinstance(parsed, dict):
            # An empty input reaches the registry, whose validation sends
            # the model a field-level error to correct -- cheaper than
            # failing the whole turn over one malformed call.
            self.unparseable_tool_inputs += 1
            return {}
        return parsed


def parse_stream(events: Iterable[dict[str, Any]]) -> Iterator[LLMEvent]:
    """Whole-stream form of ``StreamParser``, for tests and offline replay."""
    parser = StreamParser()
    for raw in events:
        yield from parser.feed(raw)
    yield parser.finish()


# ----------------------------------------------------------------------
# Provider
# ----------------------------------------------------------------------


class _Failure:
    """An exception crossing from the producer thread to the event loop."""

    __slots__ = ("exc",)

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc


_DONE = object()


def _classify(exc: ClientError) -> Exception:
    """Transient or permanent, by the same list the pipeline retries on.

    Stream exception events name their code in camelCase, so the first
    letter is normalised before the lookup. The vendor message names the
    rejected parameter, never the prompt, so it is safe to carry.
    """
    error = getattr(exc, "response", {}).get("Error", {})
    code = error.get("Code", "")
    normalised = code[:1].upper() + code[1:]
    detail = f"{normalised}: {error.get('Message', '')}"
    if normalised in _STREAM_TRANSIENT_CODES:
        return BedrockTransientError(f"bedrock transient failure: {detail}")
    return AgentProviderError(f"bedrock call failed: {detail}")


class BedrockConverseProvider:
    """``LLMProvider`` over ``bedrock-runtime`` ``converse_stream``.

    Construct once per process and reuse: the boto3 client is the only
    expensive part. ``client``, ``sleep`` and ``jitter`` are injectable so
    the retry policy is tested without waiting or a network.
    """

    def __init__(
        self,
        model_id: str,
        *,
        client: Any | None = None,
        guardrail: tuple[str, str] | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        max_retries: int = 3,
        sleep: Callable[[float], Any] = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self.model_id = model_id
        self.family = model_family(model_id)
        self._client = client
        self._guardrail = guardrail
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._max_retries = max_retries
        self._sleep = sleep
        self._jitter = jitter

    @classmethod
    def from_env(cls) -> BedrockConverseProvider:
        model_id = os.environ.get(MODEL_ID_ENV) or DEFAULT_AGENT_MODEL_ID
        guardrail_id = os.environ.get(GUARDRAIL_ID_ENV)
        guardrail = (
            (guardrail_id, os.environ.get(GUARDRAIL_VERSION_ENV) or "DRAFT")
            if guardrail_id
            else None
        )
        return cls(model_id, guardrail=guardrail)

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = boto3.client("bedrock-runtime")
        return self._client

    async def stream_turn(
        self,
        system: list[SystemBlock],
        messages: list[Message],
        tools: list[ConverseToolSpec],
        *,
        tool_choice: ToolChoice | None,
    ) -> AsyncIterator[LLMEvent]:
        request = build_request(
            model_id=self.model_id,
            family=self.family,
            system=system,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            max_tokens=self._max_tokens,
            temperature=self._temperature,
            guardrail=self._guardrail,
        )
        for attempt in range(self._max_retries + 1):
            yielded = False
            try:
                async for event in self._stream_once(request, attempt):
                    yielded = True
                    yield event
                return
            except BedrockTransientError as exc:
                # Once anything has gone out, a retry would replay text the
                # client has already shown. Only a clean failure is retried.
                if yielded or attempt == self._max_retries:
                    raise AgentProviderError(str(exc)) from exc
                delay = 2 ** (attempt + 1) + self._jitter()
                logger.warning(
                    "bedrock transient failure model=%s attempt=%d retry_in=%.1fs",
                    self.model_id,
                    attempt,
                    delay,
                )
                await self._sleep(delay)
        raise AssertionError("unreachable: the loop returns or raises")

    async def _stream_once(self, request: dict[str, Any], attempt: int) -> AsyncIterator[LLMEvent]:
        """One call, streamed off a worker thread through a queue.

        boto3's event stream is synchronous; reading it to the end before
        yielding would make the SSE stream arrive all at once. The thread
        pushes each raw event as it lands, and this coroutine parses and
        yields it -- text reaches the client while the model is still
        generating.
        """
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[Any] = asyncio.Queue()
        client = self.client

        def produce() -> None:
            try:
                response = client.converse_stream(**request)
                for raw in response["stream"]:
                    loop.call_soon_threadsafe(queue.put_nowait, raw)
            except ClientError as exc:
                loop.call_soon_threadsafe(queue.put_nowait, _Failure(_classify(exc)))
                return
            except BaseException as exc:
                loop.call_soon_threadsafe(
                    queue.put_nowait, _Failure(AgentProviderError(type(exc).__name__))
                )
                return
            loop.call_soon_threadsafe(queue.put_nowait, _DONE)

        worker = loop.run_in_executor(None, produce)
        parser = StreamParser()
        try:
            while True:
                item = await queue.get()
                if item is _DONE:
                    break
                if isinstance(item, _Failure):
                    raise item.exc
                for event in parser.feed(item):
                    yield event
        finally:
            await worker
        final = parser.finish()
        logger.info(
            "converse done model=%s family=%s attempt=%d stop_reason=%s blocks=%d "
            "in=%d out=%d cache_read=%d cache_write=%d unparseable_tool_inputs=%d",
            self.model_id,
            self.family,
            attempt,
            final.stop_reason,
            len(final.content),
            final.usage.input_tokens,
            final.usage.output_tokens,
            final.usage.cache_read_tokens,
            final.usage.cache_write_tokens,
            parser.unparseable_tool_inputs,
        )
        yield final
