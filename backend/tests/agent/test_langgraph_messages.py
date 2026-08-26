"""The bridge between the contract's messages and LangChain's, and -- the
part that matters for a real call -- what langchain-aws makes of them:
the same Converse wire form the native provider sends.

Two of these reach into langchain-aws's module privates
(``_messages_to_bedrock``) or build a client-bearing model with no
credentials (``bind_tools``); they exercise no network, and pinning the
adapter's output is the point -- an upgrade that changes the wire form
must show up here, not in a listener's transcript.
"""

from __future__ import annotations

import pytest

from agent.contracts import (
    ForcedTool,
    JsonBlock,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from agent.model_ids import ModelFamily
from agent.native.llm.converse import build_request
from agent.tools.registry import ToolRegistry

from .scenario_tools import registry

pytest.importorskip("langgraph")

from langchain_core.messages import AIMessage, HumanMessage

from agent.langgraph.messages import (
    content_blocks,
    from_langchain,
    system_message,
    to_langchain,
    usage_of,
)
from agent.langgraph.tools import bound_tools

HISTORY = [
    Message.user_text("hello"),
    Message.assistant(
        [
            TextBlock(text="let me look"),
            ToolUseBlock(tool_use_id="tu-1", name="noop", input={"note": "a"}),
            ToolUseBlock(tool_use_id="tu-2", name="strict", input={"count": 2}),
        ]
    ),
    Message(
        role="user",
        content=[
            ToolResultBlock(tool_use_id="tu-1", content=[JsonBlock(data={"ok": True})]),
            ToolResultBlock(
                tool_use_id="tu-2", content=[TextBlock(text="invalid input")], status="error"
            ),
            TextBlock(text="(a steering hint)"),
        ],
    ),
    Message.assistant([TextBlock(text="done")]),
    Message.user_text("thanks"),
]


def test_messages_round_trip_losslessly():
    assert from_langchain(to_langchain(HISTORY)) == HISTORY


def test_one_converse_user_message_becomes_tool_messages_then_a_human_message():
    lc = to_langchain(HISTORY[:3])
    assert [m.type for m in lc] == ["human", "ai", "tool", "tool", "human"]
    assert lc[2].status == "success" and lc[3].status == "error"


def test_langchain_aws_sends_the_same_wire_form_as_the_native_provider():
    from langchain_aws.chat_models.bedrock_converse import _messages_to_bedrock

    wire_messages, wire_system = _messages_to_bedrock(
        [system_message([TextBlock(text="SYSTEM"), TextBlock(text="MEM")]), *to_langchain(HISTORY)]
    )
    native = build_request(
        model_id="amazon.nova-lite-v1:0",
        family=ModelFamily.NOVA,
        system=[TextBlock(text="SYSTEM"), TextBlock(text="MEM")],
        messages=HISTORY,
        tools=[],
        tool_choice="auto",
        max_tokens=1,
        temperature=0,
        guardrail=None,
    )

    assert wire_system == native["system"]
    assert wire_messages == native["messages"]


def test_bound_tools_carry_the_native_schema_and_tool_choice():
    """What reaches Bedrock (``_format_tools`` is what the adapter applies
    at request time) must be the registry's own spec, byte for byte."""
    from langchain_aws import ChatBedrockConverse
    from langchain_aws.chat_models.bedrock_converse import _format_tools

    model = ChatBedrockConverse(model="amazon.nova-lite-v1:0", region_name="ap-southeast-2")
    bound = model.bind_tools(bound_tools(registry()), tool_choice="noop")

    assert _format_tools(bound.kwargs["tools"]) == ToolRegistry(registry()).to_converse_spec()
    assert bound.kwargs["tool_choice"] == {"tool": {"name": "noop"}}
    assert ForcedTool("noop").name == "noop"


def test_the_structured_tool_road_would_change_the_schema():
    """On record for the comparison note (plan §3.4): the framework's own
    way of binding a Pydantic model strips its ``title`` keys, so a
    ``StructuredTool`` over the same model is not the same request."""
    from langchain_core.tools import StructuredTool
    from langchain_core.utils.function_calling import convert_to_openai_tool

    from .scenario_tools import NoopIn, noop

    tool = StructuredTool.from_function(
        coroutine=noop, name="noop", description="Does nothing.", args_schema=NoopIn
    )
    via_framework = convert_to_openai_tool(tool)["function"]["parameters"]
    ours = NoopIn.model_json_schema()

    assert via_framework != ours
    assert "title" in ours and "title" not in via_framework


def test_assistant_blocks_keep_their_order_and_fill_in_from_tool_calls():
    message = AIMessage(
        content="just text",
        tool_calls=[{"name": "noop", "args": {}, "id": "tu-9", "type": "tool_call"}],
    )

    assert content_blocks(message) == [
        TextBlock(text="just text"),
        ToolUseBlock(tool_use_id="tu-9", name="noop", input={}),
    ]


def test_streamed_tool_use_blocks_take_their_input_from_tool_calls():
    """The shape a streamed reply has after the chunks are summed: the
    content block's input is the raw JSON string, tool_calls has the dict.
    Seen on dev with Nova Lite (`{"limit":5}` as a str) before this test."""
    message = AIMessage(
        content=[
            {"type": "text", "text": "looking", "index": 0},
            {"type": "tool_use", "id": "tu-1", "name": "noop", "input": '{"limit":5}', "index": 1},
            {"type": "tool_use", "id": "tu-2", "name": "noop", "input": "{not json", "index": 2},
        ],
        tool_calls=[{"name": "noop", "args": {"limit": 5}, "id": "tu-1", "type": "tool_call"}],
    )

    assert content_blocks(message) == [
        TextBlock(text="looking"),
        ToolUseBlock(tool_use_id="tu-1", name="noop", input={"limit": 5}),
        # No parsed call and no parseable JSON: an empty input, for the
        # registry to answer with a field-level error.
        ToolUseBlock(tool_use_id="tu-2", name="noop", input={}),
    ]


def test_usage_reads_cache_counters():
    message = AIMessage(
        content="",
        usage_metadata={
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
            "input_token_details": {"cache_read": 7, "cache_creation": 1},
        },
    )
    usage = usage_of(message)
    assert (usage.input_tokens, usage.output_tokens) == (10, 5)
    assert (usage.cache_read_tokens, usage.cache_write_tokens) == (7, 1)


def test_a_user_message_cannot_carry_a_tool_use():
    with pytest.raises(ValueError):
        to_langchain([Message(role="user", content=[ToolUseBlock(tool_use_id="x", name="n")])])
    with pytest.raises(ValueError):
        from_langchain([HumanMessage(content=[{"type": "image", "data": ""}])])
