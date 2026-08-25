"""A scripted ``bedrock-runtime`` client for the provider's tests.

Each ``converse_stream`` call records its request and plays the next
script: a list of raw stream events (an Exception inside the list is
raised when iteration reaches it, like a mid-stream error event), or an
Exception raised before any stream exists (a rejected request).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from botocore.exceptions import ClientError, EventStreamError

Script = list[Any] | Exception


class FakeBedrockClient:
    def __init__(self, scripts: list[Script]) -> None:
        self._scripts = list(scripts)
        self.requests: list[dict[str, Any]] = []

    def converse_stream(self, **request: Any) -> dict[str, Any]:
        assert self._scripts, "converse_stream called more often than scripted"
        self.requests.append(request)
        script = self._scripts.pop(0)
        if isinstance(script, Exception):
            raise script
        return {"stream": _play(script)}


def _play(events: list[Any]) -> Iterator[dict[str, Any]]:
    for event in events:
        if isinstance(event, Exception):
            raise event
        yield event


# ----------------------------------------------------------------------
# Event builders
# ----------------------------------------------------------------------


def text_events(*chunks: str, index: int = 0) -> list[dict[str, Any]]:
    return [
        {"contentBlockDelta": {"delta": {"text": chunk}, "contentBlockIndex": index}}
        for chunk in chunks
    ] + [{"contentBlockStop": {"contentBlockIndex": index}}]


def tool_events(
    name: str, tool_use_id: str, fragments: list[str], *, index: int = 1
) -> list[dict[str, Any]]:
    return [
        {
            "contentBlockStart": {
                "start": {"toolUse": {"toolUseId": tool_use_id, "name": name}},
                "contentBlockIndex": index,
            }
        },
        *(
            {"contentBlockDelta": {"delta": {"toolUse": {"input": f}}, "contentBlockIndex": index}}
            for f in fragments
        ),
        {"contentBlockStop": {"contentBlockIndex": index}},
    ]


def stop(reason: str) -> dict[str, Any]:
    return {"messageStop": {"stopReason": reason}}


def metadata(
    input_tokens: int = 10,
    output_tokens: int = 5,
    cache_read: int | None = None,
    cache_write: int | None = None,
) -> dict[str, Any]:
    usage: dict[str, Any] = {
        "inputTokens": input_tokens,
        "outputTokens": output_tokens,
        "totalTokens": input_tokens + output_tokens,
    }
    if cache_read is not None:
        usage["cacheReadInputTokens"] = cache_read
    if cache_write is not None:
        usage["cacheWriteInputTokens"] = cache_write
    return {"metadata": {"usage": usage, "metrics": {"latencyMs": 100}}}


def start() -> dict[str, Any]:
    return {"messageStart": {"role": "assistant"}}


def client_error(code: str, message: str = "") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, "ConverseStream")


def stream_error(code: str, message: str = "") -> EventStreamError:
    """A mid-stream exception event; Bedrock names these in camelCase."""
    return EventStreamError({"Error": {"Code": code, "Message": message}}, "ConverseStream")
