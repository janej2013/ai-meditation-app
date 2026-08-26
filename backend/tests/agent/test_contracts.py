"""The contract's wire mapping and the small value types."""

from __future__ import annotations

import pytest

from agent.contracts import (
    Deadline,
    JsonBlock,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
    block_from_converse,
)


def test_message_round_trips_through_converse_wire_form():
    message = Message(
        role="user",
        content=[
            ToolResultBlock(
                tool_use_id="tu-1",
                content=[JsonBlock(data={"ok": True, "n": 2}), TextBlock(text="note")],
                status="error",
            ),
            TextBlock(text="and then"),
        ],
    )

    wire = message.to_converse()

    assert wire == {
        "role": "user",
        "content": [
            {
                "toolResult": {
                    "toolUseId": "tu-1",
                    "content": [{"json": {"ok": True, "n": 2}}, {"text": "note"}],
                    "status": "error",
                }
            },
            {"text": "and then"},
        ],
    }
    assert Message.from_converse(wire) == message


def test_tool_use_block_wire_form_is_camel_case():
    message = Message.assistant([ToolUseBlock(tool_use_id="tu-9", name="noop", input={"a": 1})])

    assert message.to_converse()["content"] == [
        {"toolUse": {"toolUseId": "tu-9", "name": "noop", "input": {"a": 1}}}
    ]
    assert Message.from_converse(message.to_converse()) == message


def test_unknown_block_types_are_rejected_not_dropped():
    with pytest.raises(ValueError, match="unsupported content block"):
        block_from_converse({"image": {}})


def test_usage_adds_every_counter():
    total = Usage(
        input_tokens=1, output_tokens=2, cache_read_tokens=3, cache_write_tokens=4
    ) + Usage(input_tokens=10, output_tokens=20, cache_read_tokens=30, cache_write_tokens=40)

    assert total == Usage(
        input_tokens=11, output_tokens=22, cache_read_tokens=33, cache_write_tokens=44
    )


def test_deadline_never_is_never_exhausted():
    deadline = Deadline.never()

    assert deadline.remaining() == float("inf")
    assert not deadline.exhausted()


def test_deadline_counts_the_margin():
    # Five seconds left is "exhausted" against a ten-second margin: the
    # loop must not start a model call it cannot finish.
    assert Deadline.after(5).exhausted(margin_seconds=10)
    assert not Deadline.after(60).exhausted(margin_seconds=10)
    assert Deadline.after(0).exhausted(margin_seconds=0)


def test_user_message_has_one_text_block():
    assert Message.user_text("hi").to_converse() == {"role": "user", "content": [{"text": "hi"}]}
