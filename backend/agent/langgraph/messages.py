"""Our messages <-> LangChain's, losslessly.

The contract's vocabulary is Converse's (text, toolUse, toolResult) and
the checkpoint stores it in wire form. LangChain spells the same things as
``HumanMessage`` / ``AIMessage(tool_calls=...)`` / ``ToolMessage``, and
langchain-aws puts Converse's blocks into ``content`` as dicts with a
``type``. This module is the bridge, in both directions, so the LangGraph
engine reads the same history as the native one and the harness stores
the same T-item whichever engine ran.

One asymmetry to know about: a Converse user message holds every tool
result of a round plus any steering text in ONE message, while LangChain
wants one ``ToolMessage`` per result and a ``HumanMessage`` for the text.
``from_langchain`` merges runs of user-role messages back together, which
is also what langchain-aws does on the way to Bedrock.
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from agent.contracts import (
    ContentBlock,
    JsonBlock,
    Message,
    StopReason,
    SystemBlock,
    TextBlock,
    ToolOutputBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)
from agent.stop_reasons import map_stop_reason

CACHE_POINT: dict[str, Any] = {"cachePoint": {"type": "default"}}


# ----------------------------------------------------------------------
# Ours -> LangChain
# ----------------------------------------------------------------------


def system_message(blocks: list[SystemBlock]) -> SystemMessage:
    """The system prompt as one message, with the cache breakpoint after
    the first block -- the static prompt -- exactly where the native
    provider puts it (the memory block that follows differs per user)."""
    content: list[dict[str, Any]] = []
    for position, block in enumerate(blocks):
        content.append({"type": "text", "text": block.text})
        if position == 0:
            content.append(dict(CACHE_POINT))
    return SystemMessage(content=content)


def to_langchain(messages: list[Message]) -> list[BaseMessage]:
    out: list[BaseMessage] = []
    for message in messages:
        if message.role == "assistant":
            out.append(assistant_message(message.content))
            continue
        # A user message: tool results first (one ToolMessage each), then
        # any text as a HumanMessage. Converse orders them the same way.
        text_blocks: list[dict[str, Any]] = []
        for block in message.content:
            if isinstance(block, ToolResultBlock):
                out.append(
                    ToolMessage(
                        content=[_output_to_lc(part) for part in block.content],
                        tool_call_id=block.tool_use_id,
                        status=block.status,
                    )
                )
            elif isinstance(block, TextBlock):
                text_blocks.append({"type": "text", "text": block.text})
            else:
                raise ValueError("a user message cannot carry a toolUse block")
        if text_blocks:
            out.append(HumanMessage(content=text_blocks))
    return out


def assistant_message(content: list[ContentBlock]) -> AIMessage:
    """Tool uses appear twice, as langchain-aws returns them: as content
    blocks (keeping their position among the text) and as ``tool_calls``
    (what the graph routes on)."""
    lc_content: list[dict[str, Any]] = []
    tool_calls: list[dict[str, Any]] = []
    for block in content:
        if isinstance(block, TextBlock):
            lc_content.append({"type": "text", "text": block.text})
        elif isinstance(block, ToolUseBlock):
            lc_content.append(
                {
                    "type": "tool_use",
                    "id": block.tool_use_id,
                    "name": block.name,
                    "input": block.input,
                }
            )
            tool_calls.append(
                {
                    "name": block.name,
                    "args": block.input,
                    "id": block.tool_use_id,
                    "type": "tool_call",
                }
            )
        else:
            raise ValueError("an assistant message cannot carry a toolResult block")
    return AIMessage(content=lc_content, tool_calls=tool_calls)


def _output_to_lc(part: ToolOutputBlock) -> dict[str, Any]:
    if isinstance(part, TextBlock):
        return {"type": "text", "text": part.text}
    return {"type": "json", "json": part.data}


# ----------------------------------------------------------------------
# LangChain -> ours
# ----------------------------------------------------------------------


def from_langchain(messages: list[BaseMessage]) -> list[Message]:
    """System messages are not part of the conversation and are skipped;
    runs of user-role messages merge into one ``Message``."""
    out: list[Message] = []
    pending_user: list[ContentBlock] = []

    def flush() -> None:
        if pending_user:
            out.append(Message(role="user", content=list(pending_user)))
            pending_user.clear()

    for message in messages:
        if isinstance(message, SystemMessage):
            continue
        if isinstance(message, AIMessage):
            flush()
            out.append(Message.assistant(content_blocks(message)))
        elif isinstance(message, ToolMessage):
            pending_user.append(
                ToolResultBlock(
                    tool_use_id=message.tool_call_id,
                    content=_outputs_from_lc(message.content),
                    status="error" if message.status == "error" else "success",
                )
            )
        elif isinstance(message, HumanMessage):
            pending_user.extend(_text_blocks(message.content))
        else:
            raise ValueError(f"unsupported message type {type(message).__name__}")
    flush()
    return out


def content_blocks(message: AIMessage) -> list[ContentBlock]:
    """The assistant's blocks in the order the model produced them.

    A streamed reply's ``tool_use`` content block carries its ``input`` as
    the raw JSON string the fragments added up to; the parsed arguments
    live in ``tool_calls``, so those win, keyed by id. A block with no
    parsed call and unparseable JSON gets ``{}`` -- the native parser's
    rule, so the registry answers with a field-level error rather than
    the turn failing. Tool calls that only appear in ``tool_calls`` (a
    plain-string content) are appended after the text.
    """
    blocks: list[ContentBlock] = []
    seen_tool_ids: set[str] = set()
    parsed_args = {call["id"]: call["args"] for call in message.tool_calls if call.get("id")}
    if isinstance(message.content, str):
        if message.content:
            blocks.append(TextBlock(text=message.content))
    else:
        for raw in message.content:
            if isinstance(raw, str):
                if raw:
                    blocks.append(TextBlock(text=raw))
                continue
            kind = raw.get("type")
            if kind == "text":
                if raw.get("text"):
                    blocks.append(TextBlock(text=raw["text"]))
            elif kind == "tool_use":
                seen_tool_ids.add(raw["id"])
                blocks.append(
                    ToolUseBlock(
                        tool_use_id=raw["id"],
                        name=raw["name"],
                        input=parsed_args.get(raw["id"], _tool_input(raw.get("input"))),
                    )
                )
            # Anything else (reasoning, images) is not part of this transcript.
    for call in message.tool_calls:
        if call["id"] not in seen_tool_ids:
            blocks.append(
                ToolUseBlock(tool_use_id=call["id"], name=call["name"], input=call["args"])
            )
    return blocks


def _tool_input(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def stop_reason_of(message: AIMessage) -> StopReason:
    """langchain-aws keeps Converse's ``stopReason`` in ``response_metadata``
    (also on the last streamed chunk). A message without one -- history
    rebuilt from the checkpoint -- reads as a plain end of turn."""
    raw = message.response_metadata.get("stopReason")
    return "end_turn" if raw is None else map_stop_reason(raw)


def usage_of(message: AIMessage) -> Usage:
    meta = message.usage_metadata or {}
    details = meta.get("input_token_details") or {}
    return Usage(
        input_tokens=meta.get("input_tokens", 0),
        output_tokens=meta.get("output_tokens", 0),
        cache_read_tokens=details.get("cache_read", 0) or 0,
        cache_write_tokens=details.get("cache_creation", 0) or 0,
    )


def _text_blocks(content: Any) -> list[ContentBlock]:
    if isinstance(content, str):
        return [TextBlock(text=content)] if content else []
    blocks: list[ContentBlock] = []
    for raw in content:
        if isinstance(raw, str):
            blocks.append(TextBlock(text=raw))
        elif raw.get("type") == "text":
            blocks.append(TextBlock(text=raw["text"]))
        else:
            raise ValueError(f"unsupported user content block {raw.get('type')!r}")
    return blocks


def _outputs_from_lc(content: Any) -> list[ToolOutputBlock]:
    if isinstance(content, str):
        return [TextBlock(text=content)]
    out: list[ToolOutputBlock] = []
    for raw in content:
        if isinstance(raw, str):
            out.append(TextBlock(text=raw))
        elif raw.get("type") == "text":
            out.append(TextBlock(text=raw["text"]))
        elif raw.get("type") == "json":
            out.append(JsonBlock(data=raw["json"]))
        else:
            raise ValueError(f"unsupported tool result block {raw.get('type')!r}")
    return out
