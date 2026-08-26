"""Two engines, one contract (docs/agent-runner-plan.md §3.4, §10).

Every scenario here runs through the native loop and the LangGraph graph
and asserts the same things of both: the ``TurnResult`` field by field,
the events emitted in order, and the requests the model saw (converted
back to the contract's messages). The scripts are shared -- one ``LLMEvent``
list drives ``FakeProvider`` and ``ScriptedChatModel`` alike -- so a
scenario cannot drift between engines by being written twice.

What is deliberately not compared: how a provider-level failure surfaces
(a stream without an end is the native provider's protocol error and the
adapter's concern in LangChain); those keep their own tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from agent.budget import FINALIZE_TOOL_NAME, MAX_TOOL_ITERATIONS_PER_TURN
from agent.contracts import (
    AgentEvent,
    Deadline,
    Final,
    Finalized,
    ForcedTool,
    JsonBlock,
    LLMEvent,
    Message,
    Proposal,
    ProposalReady,
    TextBlock,
    TextDelta,
    ToolChoice,
    ToolResultBlock,
    ToolStarted,
    ToolUseBlock,
    TurnInput,
    TurnResult,
    Usage,
)
from agent.native.loop import NativeEngine
from agent.prompt import (
    CONVERGE_HINT,
    EMPTY_REPLY_HINT,
    EMPTY_REPLY_TEXT,
    NO_MORE_TOOLS_HINT,
    REFUSAL_TEXT,
)
from agent.tools.registry import ToolRegistry

from .fake_provider import FakeProvider, refusal_reply, run, text_reply
from .fake_provider import tool_reply as _tool_reply
from .scenario_tools import USER, Collector, registry

pytest.importorskip("langgraph")

from agent.langgraph.engine import LangGraphEngine
from agent.langgraph.messages import from_langchain

from .fake_chat_model import ScriptedChatModel

# ----------------------------------------------------------------------
# The shared script vocabulary
# ----------------------------------------------------------------------


def tool_reply(*calls: tuple[str, dict[str, Any], str], text: str | None = None) -> list[LLMEvent]:
    """Like the provider fake's, but the text -- if any -- is streamed, as a
    real model streams it, so both engines emit the same deltas."""
    events = _tool_reply(*calls, text=text)
    return [TextDelta(text), *events] if text else events


def empty_reply(text: str | None = None) -> list[LLMEvent]:
    content = [TextBlock(text=text)] if text is not None else []
    deltas = [TextDelta(text)] if text else []
    return [*deltas, Final(content=content, stop_reason="end_turn", usage=Usage())]


# ----------------------------------------------------------------------
# One harness per engine, with the same face
# ----------------------------------------------------------------------


@dataclass
class Call:
    """A model call as either engine made it, in the contract's terms."""

    system: list[str]
    messages: list[Message]
    tools: list[str]
    tool_choice: ToolChoice | None

    @property
    def last_user_text(self) -> str:
        blocks = [b for b in self.messages[-1].content if isinstance(b, TextBlock)]
        return "\n".join(b.text for b in blocks)


class NativeHarness:
    name = "native"

    def __init__(self, script: list[list[LLMEvent]]) -> None:
        self.provider = FakeProvider(script)

    def engine(self, tools: ToolRegistry) -> NativeEngine:
        return NativeEngine(self.provider, tools, USER, system_prompt="SYSTEM")

    @property
    def calls(self) -> list[Call]:
        return [
            Call(
                system=[b.text for b in c.system],
                messages=c.messages,
                tools=[t["toolSpec"]["name"] for t in c.tools],
                tool_choice=c.tool_choice,
            )
            for c in self.provider.calls
        ]

    @property
    def remaining(self) -> int:
        return self.provider.remaining


class LangGraphHarness:
    name = "langgraph"

    def __init__(self, script: list[list[LLMEvent]]) -> None:
        self.model = ScriptedChatModel(script=[list(s) for s in script])

    def engine(self, tools: ToolRegistry) -> LangGraphEngine:
        return LangGraphEngine(self.model, tools, USER, system_prompt="SYSTEM")

    @property
    def calls(self) -> list[Call]:
        out: list[Call] = []
        for c in self.model.calls:
            system = c.messages[0]
            assert system.type == "system"
            out.append(
                Call(
                    system=[
                        b["text"] for b in system.content if isinstance(b, dict) and "text" in b
                    ],
                    messages=from_langchain(c.messages[1:]),
                    tools=[n for n in c.tool_names if n != "<cachePoint>"],
                    tool_choice=(
                        "auto"
                        if c.tool_choice in (None, "auto")
                        else ForcedTool(c.tool_choice)  # bind_tools got the tool's name
                    ),
                )
            )
        return out

    @property
    def remaining(self) -> int:
        return self.model.remaining


HARNESSES = {"native": NativeHarness, "langgraph": LangGraphHarness}


@pytest.fixture(params=list(HARNESSES))
def make(request) -> Any:
    """``make(script)`` -> harness for the engine under test."""
    return HARNESSES[request.param]


def run_turn(
    harness: Any,
    *,
    tools: ToolRegistry | None = None,
    turn: int = 0,
    history: list[Message] | None = None,
    deadline: Deadline | None = None,
    user_text: str = "hello",
) -> tuple[TurnResult, list[AgentEvent]]:
    engine = harness.engine(tools or registry())
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


def test_text_turn_streams_deltas_and_calls_once(make):
    h = make([text_reply("hel", "lo")])

    result, events = run_turn(h)

    assert events == [TextDelta("hel"), TextDelta("lo")]
    assert result == TurnResult(
        content=[TextBlock(text="hello")],
        usage=Usage(input_tokens=10, output_tokens=5),
        stop_reason="end_turn",
    )
    assert len(h.calls) == 1


def test_request_carries_system_memory_tools_and_the_user_text(make):
    h = make([text_reply("ok")])

    run_turn(h, history=[Message.user_text("earlier"), Message.assistant([TextBlock(text="yes")])])

    call = h.calls[0]
    assert call.system == ["SYSTEM", "MEM"]
    assert call.tools == ["noop", "boom", "strict", FINALIZE_TOOL_NAME, "sneaky", "propose"]
    assert call.tool_choice == "auto"
    assert call.messages == [
        Message.user_text("earlier"),
        Message.assistant([TextBlock(text="yes")]),
        Message.user_text("hello"),
    ]


# ----------------------------------------------------------------------
# Tool rounds
# ----------------------------------------------------------------------


def test_single_tool_round_replays_the_exchange_to_the_model(make):
    h = make([tool_reply(("noop", {"note": "a"}, "tu-1"), text="let me check"), text_reply("done")])

    result, events = run_turn(h)

    assert events == [TextDelta("let me check"), ToolStarted("noop"), TextDelta("done")]
    assistant = Message.assistant(
        [
            TextBlock(text="let me check"),
            ToolUseBlock(tool_use_id="tu-1", name="noop", input={"note": "a"}),
        ]
    )
    results = [
        ToolResultBlock(tool_use_id="tu-1", content=[JsonBlock(data={"ok": True, "note": "a"})])
    ]
    assert h.calls[1].messages[-2:] == [assistant, Message.tool_results(results)]
    assert [r.name for r in result.tool_log] == ["noop"]
    assert result.tool_log[0].status == "success"
    assert result.rounds[0].assistant_content == assistant.content
    assert result.rounds[0].results == results
    assert result.content == [TextBlock(text="done")]
    assert result.stop_reason == "end_turn"
    assert result.usage == Usage(input_tokens=20, output_tokens=10)


def test_parallel_tool_calls_are_answered_in_one_user_message(make):
    h = make(
        [
            tool_reply(("noop", {"note": "x"}, "tu-1"), ("noop", {"note": "y"}, "tu-2")),
            text_reply("ok"),
        ]
    )

    result, events = run_turn(h)

    assert events == [ToolStarted("noop"), ToolStarted("noop"), TextDelta("ok")]
    reply = h.calls[1].messages[-1]
    assert reply.role == "user"
    assert [b.tool_use_id for b in reply.content] == ["tu-1", "tu-2"]
    assert len(h.calls[1].messages) == 3  # user, assistant, ONE user
    assert [r.tool_use_id for r in result.tool_log] == ["tu-1", "tu-2"]
    assert len(result.rounds) == 1 and len(result.rounds[0].results) == 2


def test_tool_exception_becomes_an_error_result_and_the_loop_continues(make):
    h = make([tool_reply(("boom", {"note": "secret"}, "tu-1")), text_reply("sorry")])

    result, _ = run_turn(h)

    error = result.rounds[0].results[0]
    assert error.status == "error"
    assert error.content == [TextBlock(text="boom failed: RuntimeError")]
    assert result.tool_log[0].status == "error"
    assert result.content == [TextBlock(text="sorry")]


def test_invalid_tool_input_names_the_field(make):
    h = make([tool_reply(("strict", {"count": "many"}, "tu-1")), text_reply("fixed")])

    result, _ = run_turn(h)

    text = result.rounds[0].results[0].content[0]
    assert isinstance(text, TextBlock)
    assert text.text.startswith("invalid input for strict: count:")


def test_unknown_tool_is_an_error_result(make):
    h = make([tool_reply(("nope", {}, "tu-1")), text_reply("ok")])

    result, _ = run_turn(h)

    assert result.rounds[0].results[0] == ToolResultBlock(
        tool_use_id="tu-1", content=[TextBlock(text="unknown tool: nope")], status="error"
    )


def test_terminal_tool_ends_the_turn_without_another_model_call(make):
    h = make([tool_reply((FINALIZE_TOOL_NAME, {"brief": "calm"}, "tu-1")), text_reply("never")])

    result, events = run_turn(h)

    assert events == [ToolStarted(FINALIZE_TOOL_NAME)]
    assert result.finalized == Finalized(job_id="job-1")
    assert result.stop_reason == "end_turn"
    assert result.content == [
        ToolUseBlock(tool_use_id="tu-1", name=FINALIZE_TOOL_NAME, input={"brief": "calm"})
    ]
    assert h.remaining == 1


def test_non_terminal_tool_cannot_finalize(make):
    h = make([tool_reply(("sneaky", {}, "tu-1")), text_reply("ok")])

    result, _ = run_turn(h)

    assert result.finalized is None
    assert len(h.calls) == 2


# ----------------------------------------------------------------------
# Steering: converge hint, forced tool, deadline, iteration cap
# ----------------------------------------------------------------------


def test_ninth_turn_carries_the_converge_hint(make):
    h = make([text_reply("ok")])

    run_turn(h, turn=8)

    text = h.calls[0].last_user_text
    assert text.startswith("hello") and text.endswith(CONVERGE_HINT)
    assert h.calls[0].tool_choice == "auto"


def test_early_turns_carry_no_hint(make):
    h = make([text_reply("ok")])

    run_turn(h, turn=7)

    assert h.calls[0].last_user_text == "hello"


def test_last_turn_forces_the_finalize_tool_on_the_first_call_only(make):
    h = make([tool_reply(("noop", {}, "tu-1")), text_reply("explaining why not")])

    run_turn(h, turn=11)

    assert h.calls[0].tool_choice == ForcedTool(FINALIZE_TOOL_NAME)
    assert h.calls[1].tool_choice == "auto"


def test_exhausted_deadline_asks_for_a_plain_answer_but_keeps_tools(make):
    h = make([text_reply("quick answer")])

    result, _ = run_turn(h, turn=11, deadline=Deadline.after(0))

    call = h.calls[0]
    assert call.tools
    assert call.tool_choice == "auto"
    assert call.last_user_text.endswith(NO_MORE_TOOLS_HINT)
    assert result.content == [TextBlock(text="quick answer")]


def test_tool_use_after_the_deadline_is_dropped_not_replayed(make):
    h = make([tool_reply(("noop", {}, "tu-1"), text="one more")])

    result, events = run_turn(h, deadline=Deadline.after(0))

    assert events == [TextDelta("one more"), ToolStarted("noop")]
    assert result.content == [TextBlock(text="one more")]
    assert result.tool_log == [] and result.rounds == []
    assert result.stop_reason == "end_turn"
    assert len(h.calls) == 1


def test_iteration_cap_ends_with_one_untooled_call(make):
    rounds = [tool_reply(("noop", {}, f"tu-{i}")) for i in range(MAX_TOOL_ITERATIONS_PER_TURN + 1)]
    h = make([*rounds, text_reply("closing")])

    result, _ = run_turn(h)

    assert len(result.tool_log) == MAX_TOOL_ITERATIONS_PER_TURN
    wrap_up = h.calls[MAX_TOOL_ITERATIONS_PER_TURN]
    assert wrap_up.tool_choice == "auto"
    assert wrap_up.tools
    assert isinstance(wrap_up.messages[-1].content[0], ToolResultBlock)
    assert wrap_up.messages[-1].content[-1] == TextBlock(text=NO_MORE_TOOLS_HINT)
    assert h.remaining == 1
    assert result.content == [TextBlock(text=EMPTY_REPLY_TEXT)]
    assert result.stop_reason == "end_turn"
    assert len(h.calls) == MAX_TOOL_ITERATIONS_PER_TURN + 1


# ----------------------------------------------------------------------
# Refusal and empty replies
# ----------------------------------------------------------------------


def test_refusal_returns_the_fixed_text_without_retry(make):
    h = make([refusal_reply(), text_reply("never")])

    result, events = run_turn(h)

    assert events == []
    assert result == TurnResult(
        content=[TextBlock(text=REFUSAL_TEXT)],
        usage=Usage(input_tokens=10, output_tokens=5),
        stop_reason="refusal",
    )
    assert len(h.calls) == 1


def test_refusal_after_a_tool_round_keeps_the_round(make):
    h = make([tool_reply(("noop", {}, "tu-1")), refusal_reply()])

    result, _ = run_turn(h)

    assert result.stop_reason == "refusal"
    assert [r.name for r in result.tool_log] == ["noop"] and len(result.rounds) == 1


def test_empty_reply_is_nudged_once(make):
    h = make([empty_reply(), text_reply("here you go")])

    result, events = run_turn(h)

    assert result.content == [TextBlock(text="here you go")]
    assert events == [TextDelta("here you go")]
    assert len(h.calls) == 2
    assert h.calls[1].last_user_text.endswith(EMPTY_REPLY_HINT)
    assert h.calls[1].tool_choice == "auto" and h.calls[1].tools


def test_blank_text_counts_as_empty_and_keeps_the_exchange_valid(make):
    h = make([empty_reply("  \n"), text_reply("ok")])

    result, events = run_turn(h)

    assert result.content == [TextBlock(text="ok")]
    assert events == [TextDelta("  \n"), TextDelta("ok")]
    second = h.calls[1].messages
    assert second[-2] == Message.assistant([TextBlock(text="  \n")])
    assert second[-1] == Message.user_text(EMPTY_REPLY_HINT)


def test_persistently_empty_reply_becomes_the_fallback_line(make):
    h = make([empty_reply(), empty_reply(), text_reply("never")])

    result, _ = run_turn(h)

    assert result.content == [TextBlock(text=EMPTY_REPLY_TEXT)]
    assert result.stop_reason == "end_turn"
    assert h.remaining == 1


def test_empty_reply_after_the_deadline_falls_back_without_a_retry(make):
    h = make([empty_reply(), text_reply("never")])

    result, _ = run_turn(h, deadline=Deadline.after(0))

    assert result.content == [TextBlock(text=EMPTY_REPLY_TEXT)]
    assert len(h.calls) == 1


# ----------------------------------------------------------------------
# Proposals
# ----------------------------------------------------------------------


def test_a_proposal_is_emitted_and_the_turn_goes_on(make):
    h = make([tool_reply(("propose", {"minutes": 8}, "tu-1")), text_reply("ready when you are")])

    result, events = run_turn(h, user_text="go")

    assert events == [ToolStarted("propose"), ProposalReady(8), TextDelta("ready when you are")]
    assert result.proposal == Proposal(duration_minutes=8)
    assert result.finalized is None
    assert result.content == [TextBlock(text="ready when you are")]
    assert len(h.calls) == 2


def test_the_last_proposal_of_a_turn_wins(make):
    h = make(
        [
            tool_reply(("propose", {"minutes": 5}, "tu-1")),
            tool_reply(("propose", {"minutes": 12}, "tu-2")),
            text_reply("twelve it is"),
        ]
    )

    result, events = run_turn(h, user_text="go")

    assert result.proposal == Proposal(duration_minutes=12)
    assert [e for e in events if isinstance(e, ProposalReady)] == [
        ProposalReady(5),
        ProposalReady(12),
    ]


def test_terminal_tools_still_end_the_turn_at_once(make):
    h = make(
        [tool_reply((FINALIZE_TOOL_NAME, {"brief": "calm"}, "tu-1")), text_reply("never sent")]
    )

    result, _ = run_turn(h, user_text="go")

    assert result.finalized == Finalized(job_id="job-1") and result.proposal is None
    assert h.remaining == 1
