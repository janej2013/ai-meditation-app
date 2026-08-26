"""The native loop against a scripted provider and test-only tools.

Every scenario asserts on the request the loop built (through the fake's
call log) as much as on the result: the request shape is the contract with
Bedrock, and the model never sees anything else.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from agent.budget import FINALIZE_TOOL_NAME, MAX_TOOL_ITERATIONS_PER_TURN
from agent.contracts import (
    AgentEvent,
    Deadline,
    Finalized,
    ForcedTool,
    JsonBlock,
    Message,
    TextBlock,
    TextDelta,
    ToolResultBlock,
    ToolStarted,
    ToolUseBlock,
    TurnInput,
    Usage,
)
from agent.native.loop import NativeEngine, ProviderProtocolError
from agent.prompt import CONVERGE_HINT, NO_MORE_TOOLS_HINT, REFUSAL_TEXT
from agent.tools.registry import ToolContext, ToolOutcome, ToolRegistry, ToolSpec

from .fake_provider import FakeProvider, refusal_reply, run, text_reply, tool_reply

USER = ToolContext(user_id="user-1", session_id="sess-1")


class NoopIn(BaseModel):
    note: str = ""


class StrictIn(BaseModel):
    count: int


class FinishIn(BaseModel):
    brief: str


async def noop(ctx: ToolContext, inp: NoopIn) -> ToolOutcome:
    return ToolOutcome(content={"ok": True, "note": inp.note})


async def boom(ctx: ToolContext, inp: NoopIn) -> ToolOutcome:
    raise RuntimeError("the input was: " + inp.note)


async def strict(ctx: ToolContext, inp: StrictIn) -> ToolOutcome:
    return ToolOutcome(content={"count": inp.count})


async def finish(ctx: ToolContext, inp: FinishIn) -> ToolOutcome:
    return ToolOutcome(content={"job_id": "job-1"}, finalized=Finalized(job_id="job-1"))


async def sneaky_finish(ctx: ToolContext, inp: NoopIn) -> ToolOutcome:
    # Not registered as terminal: its finalized must be ignored.
    return ToolOutcome(content="done", finalized=Finalized(job_id="job-x"))


def registry() -> ToolRegistry:
    return ToolRegistry(
        [
            ToolSpec("noop", "Does nothing.", NoopIn, noop),
            ToolSpec("boom", "Raises.", NoopIn, boom),
            ToolSpec("strict", "Wants an int.", StrictIn, strict),
            ToolSpec(FINALIZE_TOOL_NAME, "Ends the session.", FinishIn, finish, terminal=True),
            ToolSpec("sneaky", "Claims to finalize.", NoopIn, sneaky_finish),
        ]
    )


class Collector:
    def __init__(self) -> None:
        self.events: list[AgentEvent] = []

    async def __call__(self, event: AgentEvent) -> None:
        self.events.append(event)


def run_turn(
    provider: FakeProvider,
    *,
    turn: int = 0,
    history: list[Message] | None = None,
    deadline: Deadline | None = None,
    user_text: str = "hello",
):
    engine = NativeEngine(provider, registry(), USER, system_prompt="SYSTEM")
    emit = Collector()
    result = run(
        engine.run_turn(
            TurnInput(history=history or [], user_text=user_text, turn=turn, memory_block="MEM"),
            deadline=deadline or Deadline.never(),
            emit=emit,
        )
    )
    return result, emit.events


# ----------------------------------------------------------------------
# Plain answers
# ----------------------------------------------------------------------


def test_text_turn_streams_deltas_and_calls_once():
    provider = FakeProvider([text_reply("hel", "lo")])

    result, events = run_turn(provider)

    assert events == [TextDelta("hel"), TextDelta("lo")]
    assert result.content == [TextBlock(text="hello")]
    assert result.tool_log == [] and result.rounds == []
    assert result.stop_reason == "end_turn"
    assert result.finalized is None
    assert len(provider.calls) == 1


def test_request_carries_system_memory_tools_and_the_user_text():
    provider = FakeProvider([text_reply("ok")])

    run_turn(
        provider, history=[Message.user_text("earlier"), Message.assistant([TextBlock(text="yes")])]
    )

    call = provider.calls[0]
    assert [b.text for b in call.system] == ["SYSTEM", "MEM"]
    assert [t["toolSpec"]["name"] for t in call.tools] == [
        "noop",
        "boom",
        "strict",
        FINALIZE_TOOL_NAME,
        "sneaky",
    ]
    assert call.tool_choice == "auto"
    assert call.messages[:2] == [
        Message.user_text("earlier"),
        Message.assistant([TextBlock(text="yes")]),
    ]
    assert call.messages[-1] == Message.user_text("hello")


# ----------------------------------------------------------------------
# Tool rounds
# ----------------------------------------------------------------------


def test_single_tool_round_replays_the_exchange_to_the_model():
    provider = FakeProvider(
        [tool_reply(("noop", {"note": "a"}, "tu-1"), text="let me check"), text_reply("done")]
    )

    result, events = run_turn(provider)

    assert events == [ToolStarted("noop"), TextDelta("done")]
    second = provider.calls[1].messages
    assert second[-2] == Message.assistant(
        [
            TextBlock(text="let me check"),
            ToolUseBlock(tool_use_id="tu-1", name="noop", input={"note": "a"}),
        ]
    )
    assert second[-1] == Message.tool_results(
        [ToolResultBlock(tool_use_id="tu-1", content=[JsonBlock(data={"ok": True, "note": "a"})])]
    )
    assert [r.name for r in result.tool_log] == ["noop"]
    assert result.tool_log[0].status == "success"
    assert len(result.rounds) == 1
    assert result.content == [TextBlock(text="done")]
    assert result.usage == Usage(input_tokens=20, output_tokens=10)


def test_parallel_tool_calls_are_answered_in_one_user_message():
    provider = FakeProvider(
        [
            tool_reply(("noop", {"note": "x"}, "tu-1"), ("noop", {"note": "y"}, "tu-2")),
            text_reply("ok"),
        ]
    )

    result, _ = run_turn(provider)

    reply = provider.calls[1].messages[-1]
    assert reply.role == "user"
    assert [b.tool_use_id for b in reply.content] == ["tu-1", "tu-2"]
    assert len(provider.calls[1].messages) == 3  # user, assistant, ONE user
    assert [r.tool_use_id for r in result.tool_log] == ["tu-1", "tu-2"]


def test_tool_exception_becomes_an_error_result_and_the_loop_continues():
    provider = FakeProvider([tool_reply(("boom", {"note": "s3cret"}, "tu-1")), text_reply("sorry")])

    result, _ = run_turn(provider)

    block = provider.calls[1].messages[-1].content[0]
    assert isinstance(block, ToolResultBlock)
    assert block.status == "error"
    assert "RuntimeError" in block.content[0].text
    # The exception message quoted the input; the result must not.
    assert "s3cret" not in block.content[0].text
    assert result.tool_log[0].status == "error"
    assert result.content == [TextBlock(text="sorry")]


def test_invalid_tool_input_names_the_field():
    provider = FakeProvider([tool_reply(("strict", {"count": "many"}, "tu-1")), text_reply("ok")])

    run_turn(provider)

    block = provider.calls[1].messages[-1].content[0]
    assert isinstance(block, ToolResultBlock)
    assert block.status == "error"
    assert "count" in block.content[0].text


def test_unknown_tool_is_an_error_result():
    provider = FakeProvider([tool_reply(("nope", {}, "tu-1")), text_reply("ok")])

    result, _ = run_turn(provider)

    assert result.tool_log[0].status == "error"


# ----------------------------------------------------------------------
# Finalizing
# ----------------------------------------------------------------------


def test_terminal_tool_ends_the_turn_without_another_model_call():
    provider = FakeProvider(
        [tool_reply((FINALIZE_TOOL_NAME, {"brief": "calm"}, "tu-1")), text_reply("never sent")]
    )

    result, _ = run_turn(provider)

    assert result.finalized == Finalized(job_id="job-1")
    assert provider.remaining == 1
    assert result.stop_reason == "end_turn"
    # The final content is the tool-calling assistant message itself.
    assert any(isinstance(b, ToolUseBlock) for b in result.content)
    assert len(result.rounds) == 1


def test_non_terminal_tool_cannot_finalize():
    provider = FakeProvider([tool_reply(("sneaky", {}, "tu-1")), text_reply("ok")])

    result, _ = run_turn(provider)

    assert result.finalized is None
    assert len(provider.calls) == 2


# ----------------------------------------------------------------------
# Steering: converge hint, forced finalize, deadline, iteration cap
# ----------------------------------------------------------------------


def test_ninth_turn_carries_the_converge_hint():
    provider = FakeProvider([text_reply("ok")])

    run_turn(provider, turn=8)

    text = provider.calls[0].last_user_text
    assert text.startswith("hello")
    assert text.endswith(CONVERGE_HINT)
    assert provider.calls[0].tool_choice == "auto"


def test_early_turns_carry_no_hint():
    provider = FakeProvider([text_reply("ok")])

    run_turn(provider, turn=7)

    assert provider.calls[0].last_user_text == "hello"


def test_last_turn_forces_the_finalize_tool_on_the_first_call_only():
    provider = FakeProvider([tool_reply(("noop", {}, "tu-1")), text_reply("explaining why not")])

    run_turn(provider, turn=11)

    assert provider.calls[0].tool_choice == ForcedTool(FINALIZE_TOOL_NAME)
    assert provider.calls[1].tool_choice == "auto"


def test_exhausted_deadline_asks_for_a_plain_answer_but_keeps_tools():
    provider = FakeProvider([text_reply("quick answer")])

    result, _ = run_turn(provider, turn=11, deadline=Deadline.after(0))

    call = provider.calls[0]
    # Converse needs toolConfig whenever history holds tool blocks, so tools
    # stay on the request; the steering is text plus an un-forced choice.
    assert call.tools
    assert call.tool_choice == "auto"
    assert call.last_user_text.endswith(NO_MORE_TOOLS_HINT)
    assert result.content == [TextBlock(text="quick answer")]


def test_tool_use_after_the_deadline_is_dropped_not_replayed():
    provider = FakeProvider([tool_reply(("noop", {}, "tu-1"), text="one more")])

    result, _ = run_turn(provider, deadline=Deadline.after(0))

    assert result.content == [TextBlock(text="one more")]
    assert result.tool_log == []
    assert len(provider.calls) == 1


def test_iteration_cap_ends_with_one_untooled_call():
    rounds = [tool_reply(("noop", {}, f"tu-{i}")) for i in range(MAX_TOOL_ITERATIONS_PER_TURN + 1)]
    provider = FakeProvider([*rounds, text_reply("closing")])

    result, _ = run_turn(provider)

    assert len(result.tool_log) == MAX_TOOL_ITERATIONS_PER_TURN
    # The fifth call is the wrap-up: hint appended to the tool-results
    # message, tools still present, and its tool calls ignored.
    wrap_up = provider.calls[MAX_TOOL_ITERATIONS_PER_TURN]
    assert wrap_up.tool_choice == "auto"
    assert isinstance(wrap_up.messages[-1].content[0], ToolResultBlock)
    assert wrap_up.messages[-1].content[-1] == TextBlock(text=NO_MORE_TOOLS_HINT)
    assert provider.remaining == 1
    assert not any(isinstance(b, ToolUseBlock) for b in result.content)
    assert len(provider.calls) == MAX_TOOL_ITERATIONS_PER_TURN + 1


# ----------------------------------------------------------------------
# Refusal and protocol errors
# ----------------------------------------------------------------------


def test_refusal_returns_the_fixed_text_without_retry():
    provider = FakeProvider([refusal_reply(), text_reply("never")])

    result, _ = run_turn(provider)

    assert result.content == [TextBlock(text=REFUSAL_TEXT)]
    assert result.stop_reason == "refusal"
    assert len(provider.calls) == 1


def test_stream_without_final_is_a_protocol_error():
    provider = FakeProvider([[TextDelta("half")]])

    with pytest.raises(ProviderProtocolError):
        run_turn(provider)


# ----------------------------------------------------------------------
# Empty replies
# ----------------------------------------------------------------------


def empty_reply(content=None):
    from agent.contracts import Final, Usage

    return [Final(content=content or [], stop_reason="end_turn", usage=Usage())]


def test_empty_reply_is_nudged_once():
    from agent.prompt import EMPTY_REPLY_HINT

    provider = FakeProvider([empty_reply(), text_reply("here you go")])

    result, events = run_turn(provider)

    assert result.content == [TextBlock(text="here you go")]
    assert events == [TextDelta("here you go")]
    assert len(provider.calls) == 2
    assert provider.calls[1].last_user_text.endswith(EMPTY_REPLY_HINT)
    assert provider.calls[1].tool_choice == "auto"


def test_blank_text_counts_as_empty_and_keeps_the_exchange_valid():
    from agent.prompt import EMPTY_REPLY_HINT

    provider = FakeProvider([empty_reply([TextBlock(text="  \n")]), text_reply("ok")])

    result, _ = run_turn(provider)

    assert result.content == [TextBlock(text="ok")]
    second = provider.calls[1].messages
    assert second[-2] == Message.assistant([TextBlock(text="  \n")])
    assert second[-1] == Message.user_text(EMPTY_REPLY_HINT)


def test_persistently_empty_reply_becomes_the_fallback_line():
    from agent.native.loop import _FALLBACK_TEXT

    provider = FakeProvider([empty_reply(), empty_reply(), text_reply("never")])

    result, _ = run_turn(provider)

    assert result.content == [TextBlock(text=_FALLBACK_TEXT)]
    assert result.stop_reason == "end_turn"
    assert provider.remaining == 1  # no third call


def test_empty_reply_after_the_deadline_falls_back_without_a_retry():
    from agent.native.loop import _FALLBACK_TEXT

    provider = FakeProvider([empty_reply(), text_reply("never")])

    result, _ = run_turn(provider, deadline=Deadline.after(0))

    assert result.content == [TextBlock(text=_FALLBACK_TEXT)]
    assert len(provider.calls) == 1


# ----------------------------------------------------------------------
# Proposals
# ----------------------------------------------------------------------


class ProposeIn(BaseModel):
    minutes: int = 5


async def propose(ctx: ToolContext, inp: ProposeIn) -> ToolOutcome:
    from agent.contracts import Proposal

    return ToolOutcome(
        content={"status": "awaiting_confirmation"}, proposal=Proposal(duration_minutes=inp.minutes)
    )


def proposing_registry() -> ToolRegistry:
    reg = registry()
    reg.register(ToolSpec("propose", "Proposes.", ProposeIn, propose))
    return reg


def test_a_proposal_is_emitted_and_the_turn_goes_on():
    from agent.contracts import Proposal, ProposalReady

    provider = FakeProvider(
        [tool_reply(("propose", {"minutes": 8}, "tu-1")), text_reply("ready when you are")]
    )
    engine = NativeEngine(provider, proposing_registry(), USER, system_prompt="S")
    emit = Collector()

    result = run(
        engine.run_turn(
            TurnInput(history=[], user_text="go", turn=0), deadline=Deadline.never(), emit=emit
        )
    )

    assert emit.events == [
        ToolStarted("propose"),
        ProposalReady(8),
        TextDelta("ready when you are"),
    ]
    assert result.proposal == Proposal(duration_minutes=8)
    assert result.finalized is None
    assert result.content == [TextBlock(text="ready when you are")]
    assert len(provider.calls) == 2


def test_the_last_proposal_of_a_turn_wins():
    from agent.contracts import Proposal

    provider = FakeProvider(
        [
            tool_reply(("propose", {"minutes": 5}, "tu-1")),
            tool_reply(("propose", {"minutes": 12}, "tu-2")),
            text_reply("twelve it is"),
        ]
    )
    engine = NativeEngine(provider, proposing_registry(), USER, system_prompt="S")

    result = run(
        engine.run_turn(
            TurnInput(history=[], user_text="go", turn=0),
            deadline=Deadline.never(),
            emit=Collector(),
        )
    )

    assert result.proposal == Proposal(duration_minutes=12)


def test_terminal_tools_still_end_the_turn_at_once():
    provider = FakeProvider(
        [tool_reply((FINALIZE_TOOL_NAME, {"brief": "calm"}, "tu-1")), text_reply("never sent")]
    )
    engine = NativeEngine(provider, proposing_registry(), USER, system_prompt="S")

    result = run(
        engine.run_turn(
            TurnInput(history=[], user_text="go", turn=0),
            deadline=Deadline.never(),
            emit=Collector(),
        )
    )

    assert result.finalized == Finalized(job_id="job-1") and result.proposal is None
    assert provider.remaining == 1
