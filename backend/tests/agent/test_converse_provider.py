"""The Converse provider: request shape, stream parsing, retries -- all
against the fake client. Nothing here touches Bedrock."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from agent.contracts import (
    Deadline,
    Final,
    ForcedTool,
    JsonBlock,
    Message,
    TextBlock,
    TextDelta,
    ToolResultBlock,
    ToolUseBlock,
    ToolUseStart,
    TurnInput,
    Usage,
)
from agent.native.llm.converse import (
    AgentProviderError,
    BedrockConverseProvider,
    ModelFamily,
    build_request,
    model_family,
    parse_stream,
)
from agent.native.loop import NativeEngine
from agent.tools.registry import ToolContext, ToolOutcome, ToolRegistry, ToolSpec

from .fake_bedrock import (
    FakeBedrockClient,
    client_error,
    metadata,
    start,
    stop,
    stream_error,
    text_events,
    tool_events,
)
from .fake_provider import run

CLAUDE = "au.anthropic.claude-sonnet-4-6-v1:0"
NOVA = "amazon.nova-lite-v1:0"
SYSTEM = [TextBlock(text="static"), TextBlock(text="memory")]
TOOLS = [{"toolSpec": {"name": "noop", "description": "d", "inputSchema": {"json": {}}}}]


def request(**overrides):
    kwargs = {
        "model_id": NOVA,
        "family": ModelFamily.NOVA,
        "system": SYSTEM,
        "messages": [Message.user_text("hi")],
        "tools": TOOLS,
        "tool_choice": "auto",
        "max_tokens": 4096,
        "temperature": 0.7,
        "guardrail": None,
    }
    kwargs.update(overrides)
    return build_request(**kwargs)


# ----------------------------------------------------------------------
# Model ids
# ----------------------------------------------------------------------


def test_model_family_detection():
    assert model_family(CLAUDE) is ModelFamily.CLAUDE
    assert model_family(NOVA) is ModelFamily.NOVA


@pytest.mark.parametrize(
    "model_id",
    ["us.anthropic.claude-sonnet-4-6-v1:0", "apac.amazon.nova-lite-v1:0", "global.anthropic.x"],
)
def test_cross_region_profiles_are_refused(model_id):
    with pytest.raises(ValueError, match="leave Australia"):
        model_family(model_id)


def test_unknown_family_is_refused():
    with pytest.raises(ValueError, match="unsupported model family"):
        model_family("meta.llama3-70b")


def test_from_env_reads_model_and_guardrail(monkeypatch):
    monkeypatch.setenv("AGENT_MODEL_ID", CLAUDE)
    monkeypatch.setenv("AGENT_GUARDRAIL_ID", "gr-1")
    monkeypatch.delenv("AGENT_GUARDRAIL_VERSION", raising=False)

    provider = BedrockConverseProvider.from_env()

    assert provider.model_id == CLAUDE and provider.family is ModelFamily.CLAUDE
    assert provider._guardrail == ("gr-1", "DRAFT")


# ----------------------------------------------------------------------
# Request shape
# ----------------------------------------------------------------------


def test_cache_point_follows_the_static_system_block():
    assert request()["system"] == [
        {"text": "static"},
        {"cachePoint": {"type": "default"}},
        {"text": "memory"},
    ]


def test_claude_caches_tools_and_nova_does_not():
    claude = request(model_id=CLAUDE, family=ModelFamily.CLAUDE)
    nova = request()

    assert claude["toolConfig"]["tools"] == [*TOOLS, {"cachePoint": {"type": "default"}}]
    assert nova["toolConfig"]["tools"] == TOOLS


def test_tool_choice_mapping():
    assert request(tool_choice="auto")["toolConfig"]["toolChoice"] == {"auto": {}}
    assert request(tool_choice=ForcedTool("finish"))["toolConfig"]["toolChoice"] == {
        "tool": {"name": "finish"}
    }
    assert "toolChoice" not in request(tool_choice=None)["toolConfig"]


def test_tool_config_is_always_present_even_with_no_tools():
    assert request(tools=[])["toolConfig"] == {"tools": [], "toolChoice": {"auto": {}}}


def test_guardrail_config_only_when_configured():
    assert "guardrailConfig" not in request()
    assert request(guardrail=("gr-1", "2"))["guardrailConfig"] == {
        "guardrailIdentifier": "gr-1",
        "guardrailVersion": "2",
        "streamProcessingMode": "async",
    }


def test_messages_are_the_wire_form_and_the_request_is_deterministic():
    messages = [
        Message.user_text("hi"),
        Message.assistant([ToolUseBlock(tool_use_id="t1", name="noop", input={"a": 1})]),
        Message.tool_results([ToolResultBlock(tool_use_id="t1", content=[JsonBlock(data={})])]),
    ]

    first = request(messages=messages)
    second = request(messages=messages)

    assert first == second
    assert first["messages"] == [m.to_converse() for m in messages]
    assert first["inferenceConfig"] == {"maxTokens": 4096, "temperature": 0.7}
    assert first["modelId"] == NOVA


# ----------------------------------------------------------------------
# Stream parsing
# ----------------------------------------------------------------------


def test_text_deltas_become_one_text_block():
    events = list(parse_stream([start(), *text_events("hel", "lo"), stop("end_turn"), metadata()]))

    assert events[:2] == [TextDelta("hel"), TextDelta("lo")]
    final = events[-1]
    assert isinstance(final, Final)
    assert final.content == [TextBlock(text="hello")]
    assert final.stop_reason == "end_turn"
    assert final.usage == Usage(input_tokens=10, output_tokens=5)


def test_tool_use_fragments_are_joined_and_parsed():
    events = list(
        parse_stream(
            [
                start(),
                *tool_events("noop", "tu-1", ['{"lim', 'it": 3', "}"]),
                stop("tool_use"),
                metadata(),
            ]
        )
    )

    assert events[0] == ToolUseStart(name="noop", tool_use_id="tu-1")
    final = events[-1]
    assert isinstance(final, Final)
    assert final.content == [ToolUseBlock(tool_use_id="tu-1", name="noop", input={"limit": 3})]
    assert final.stop_reason == "tool_use"


def test_blocks_keep_their_order_across_text_and_two_tools():
    events = [
        start(),
        *text_events("let me ", "check", index=0),
        *tool_events("a", "tu-a", ["{}"], index=1),
        *tool_events("b", "tu-b", [""], index=2),
        stop("tool_use"),
        metadata(),
    ]

    final = list(parse_stream(events))[-1]

    assert isinstance(final, Final)
    assert final.content == [
        TextBlock(text="let me check"),
        ToolUseBlock(tool_use_id="tu-a", name="a", input={}),
        ToolUseBlock(tool_use_id="tu-b", name="b", input={}),
    ]


@pytest.mark.parametrize(
    ("bedrock", "ours"),
    [
        ("end_turn", "end_turn"),
        ("tool_use", "tool_use"),
        ("max_tokens", "max_tokens"),
        ("stop_sequence", "end_turn"),
        ("guardrail_intervened", "refusal"),
        ("content_filtered", "refusal"),
    ],
)
def test_stop_reason_mapping(bedrock, ours):
    final = list(parse_stream([*text_events("x"), stop(bedrock), metadata()]))[-1]

    assert isinstance(final, Final) and final.stop_reason == ours


def test_unknown_stop_reason_is_a_provider_error():
    with pytest.raises(AgentProviderError, match="stopReason"):
        list(parse_stream([*text_events("x"), stop("weird"), metadata()]))


def test_usage_carries_cache_counters():
    final = list(
        parse_stream(
            [*text_events("x"), stop("end_turn"), metadata(100, 7, cache_read=80, cache_write=20)]
        )
    )[-1]

    assert isinstance(final, Final)
    assert final.usage == Usage(
        input_tokens=100, output_tokens=7, cache_read_tokens=80, cache_write_tokens=20
    )


def test_missing_message_stop_is_a_provider_error():
    with pytest.raises(AgentProviderError, match="messageStop"):
        list(parse_stream([*text_events("x"), metadata()]))


def test_unparseable_tool_input_becomes_empty_not_an_error():
    final = list(
        parse_stream([*tool_events("noop", "tu-1", ['{"limit": ']), stop("tool_use"), metadata()])
    )[-1]

    assert isinstance(final, Final)
    assert final.content == [ToolUseBlock(tool_use_id="tu-1", name="noop", input={})]


# ----------------------------------------------------------------------
# stream_turn: threading, retries
# ----------------------------------------------------------------------


class Sleeps:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def provider(client: FakeBedrockClient, sleeps: Sleeps | None = None) -> BedrockConverseProvider:
    return BedrockConverseProvider(
        NOVA, client=client, sleep=sleeps or Sleeps(), jitter=lambda: 0.0, max_retries=3
    )


async def collect(p: BedrockConverseProvider):
    events = []
    async for event in p.stream_turn(SYSTEM, [Message.user_text("hi")], TOOLS, tool_choice="auto"):
        events.append(event)
    return events


OK_STREAM = [start(), *text_events("ok"), stop("end_turn"), metadata()]


def test_stream_turn_yields_deltas_then_one_final():
    client = FakeBedrockClient([OK_STREAM])

    events = run(collect(provider(client)))

    assert events == [
        TextDelta("ok"),
        Final([TextBlock(text="ok")], "end_turn", Usage(input_tokens=10, output_tokens=5)),
    ]
    assert client.requests[0]["modelId"] == NOVA


def test_throttling_before_any_event_is_retried_with_backoff():
    sleeps = Sleeps()
    client = FakeBedrockClient(
        [client_error("ThrottlingException"), client_error("ThrottlingException"), OK_STREAM]
    )

    events = run(collect(provider(client, sleeps)))

    assert isinstance(events[-1], Final)
    assert len(client.requests) == 3
    assert sleeps.delays == [2.0, 4.0]


def test_retries_are_exhausted_after_max_retries():
    sleeps = Sleeps()
    client = FakeBedrockClient([client_error("ThrottlingException")] * 4)

    with pytest.raises(AgentProviderError, match="transient"):
        run(collect(provider(client, sleeps)))

    assert len(client.requests) == 4  # one attempt plus three retries
    assert sleeps.delays == [2.0, 4.0, 8.0]


def test_mid_stream_failure_after_text_is_not_retried():
    sleeps = Sleeps()
    client = FakeBedrockClient(
        [[start(), *text_events("par"), stream_error("throttlingException")], OK_STREAM]
    )

    with pytest.raises(AgentProviderError):
        run(collect(provider(client, sleeps)))

    assert len(client.requests) == 1
    assert sleeps.delays == []


def test_mid_stream_failure_before_any_event_is_retried():
    client = FakeBedrockClient([[start(), stream_error("modelStreamErrorException")], OK_STREAM])

    events = run(collect(provider(client)))

    assert isinstance(events[-1], Final)
    assert len(client.requests) == 2


def test_permanent_errors_are_not_retried():
    sleeps = Sleeps()
    client = FakeBedrockClient([client_error("ValidationException", "bad maxTokens")])

    with pytest.raises(AgentProviderError, match="ValidationException: bad maxTokens"):
        run(collect(provider(client, sleeps)))

    assert sleeps.delays == []


# ----------------------------------------------------------------------
# Through the loop
# ----------------------------------------------------------------------


class NoopIn(BaseModel):
    note: str = ""


async def noop(ctx: ToolContext, inp: NoopIn) -> ToolOutcome:
    return ToolOutcome(content={"ok": True, "note": inp.note})


async def silent(event) -> None:
    return None


def test_loop_replays_the_tool_exchange_in_wire_form():
    client = FakeBedrockClient(
        [
            [
                start(),
                *tool_events("noop", "tu-1", ['{"note": "a"}']),
                stop("tool_use"),
                metadata(),
            ],
            [start(), *text_events("done"), stop("end_turn"), metadata()],
        ]
    )
    engine = NativeEngine(
        provider(client),
        ToolRegistry([ToolSpec("noop", "d", NoopIn, noop)]),
        ToolContext(user_id="u", session_id="s"),
        system_prompt="S",
    )

    result = run(
        engine.run_turn(
            TurnInput(history=[], user_text="hi", turn=0), deadline=Deadline.never(), emit=silent
        )
    )

    assert result.content == [TextBlock(text="done")]
    second = client.requests[1]["messages"]
    assert second[-2] == {
        "role": "assistant",
        "content": [{"toolUse": {"toolUseId": "tu-1", "name": "noop", "input": {"note": "a"}}}],
    }
    assert second[-1] == {
        "role": "user",
        "content": [
            {
                "toolResult": {
                    "toolUseId": "tu-1",
                    "content": [{"json": {"ok": True, "note": "a"}}],
                    "status": "success",
                }
            }
        ],
    }
    assert result.usage == Usage(input_tokens=20, output_tokens=10)


# ----------------------------------------------------------------------
# Nova's thinking tags
# ----------------------------------------------------------------------


def nova_stream(*chunks: str, index: int = 0):
    return [start(), *text_events(*chunks, index=index), stop("end_turn"), metadata()]


def test_nova_thinking_tags_are_dropped_even_when_split_across_deltas():
    client = FakeBedrockClient(
        [nova_stream("<thin", "king>secret plan</thin", "king>\n\nHel", "lo there")]
    )

    events = run(collect(provider(client)))

    assert [e for e in events if isinstance(e, TextDelta)] == [
        TextDelta("Hel"),
        TextDelta("lo there"),
    ]
    final = events[-1]
    assert isinstance(final, Final)
    assert final.content == [TextBlock(text="Hello there")]


def test_nova_thinking_only_reply_yields_no_text():
    client = FakeBedrockClient([nova_stream("<thinking>just", " thoughts</thinking>")])

    events = run(collect(provider(client)))

    assert not any(isinstance(e, TextDelta) for e in events)
    final = events[-1]
    assert isinstance(final, Final) and final.content == []


def test_angle_brackets_that_are_not_tags_pass_through():
    client = FakeBedrockClient([nova_stream("a < b and <b>bold</b>")])

    events = run(collect(provider(client)))

    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "a < b and <b>bold</b>"


def test_claude_text_is_left_alone():
    client = FakeBedrockClient([nova_stream("<thinking>kept</thinking> hi")])
    claude = BedrockConverseProvider(CLAUDE, client=client, sleep=Sleeps(), jitter=lambda: 0.0)

    events = run(collect(claude))

    final = events[-1]
    assert isinstance(final, Final)
    assert final.content == [TextBlock(text="<thinking>kept</thinking> hi")]
