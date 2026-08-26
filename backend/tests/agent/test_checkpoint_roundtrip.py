"""Completion criterion 2: a turn survives the table.

Run a turn, checkpoint it through the real store (moto), rebuild the history
from what was stored, and run the next turn on a NEW engine. The second
provider must receive, as a prefix, exactly the conversation the first
engine ended with -- that is what makes every turn a resume.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import BaseModel

from agent.checkpoint import TurnCheckpoint, rebuild_messages
from agent.contracts import Deadline, Finalized, Message, TextBlock, ToolResultBlock, TurnInput
from agent.native.loop import NativeEngine
from agent.prompt import CONVERGE_HINT
from agent.tools.registry import ToolContext, ToolOutcome, ToolRegistry, ToolSpec
from shared.models import AgentSessionStatus

from ..conftest import USER_ID
from .fake_provider import FakeProvider, run, text_reply, tool_reply

SESSION = "sess-1"
NOW = datetime(2026, 8, 25, 10, 0, tzinfo=UTC)


class NoopIn(BaseModel):
    note: str = ""


class FinishIn(BaseModel):
    brief: str


async def noop(ctx: ToolContext, inp: NoopIn) -> ToolOutcome:
    return ToolOutcome(content={"seen": inp.note})


async def finish(ctx: ToolContext, inp: FinishIn) -> ToolOutcome:
    return ToolOutcome(content={"job_id": "job-7"}, finalized=Finalized(job_id="job-7"))


def engine(provider: FakeProvider) -> NativeEngine:
    tools = ToolRegistry(
        [
            ToolSpec("noop", "noop", NoopIn, noop),
            ToolSpec("finalize_meditation_brief", "end", FinishIn, finish, terminal=True),
        ]
    )
    return NativeEngine(
        provider, tools, ToolContext(user_id=USER_ID, session_id=SESSION), system_prompt="S"
    )


async def silent(event) -> None:
    return None


def test_second_turn_on_a_new_engine_sees_the_first_turn_verbatim(store):
    first = FakeProvider(
        [
            tool_reply(("noop", {"note": "a", "score": 0.5}, "tu-1"), text="checking"),
            text_reply("all set"),
        ]
    )
    result = run(
        engine(first).run_turn(
            TurnInput(history=[], user_text="hello", turn=0), deadline=Deadline.never(), emit=silent
        )
    )

    assert store.create_agent_session(USER_ID, SESSION, engine="native", model_id="m")
    store.claim_turn(USER_ID, SESSION, engine="native", now=NOW)
    checkpoint = TurnCheckpoint.from_result(
        session_id=SESSION, turn=0, user_text="hello", result=result
    )
    assert store.commit_turn(USER_ID, SESSION, expected_turn=0, checkpoint=checkpoint)

    history = rebuild_messages(store.list_turns(USER_ID, SESSION))

    second = FakeProvider([text_reply("second")])
    run(
        engine(second).run_turn(
            TurnInput(history=history, user_text="again", turn=1),
            deadline=Deadline.never(),
            emit=silent,
        )
    )

    # What the first engine ended with: the messages of its last call plus
    # the answer that call produced.
    expected = [*first.calls[-1].messages, Message.assistant(result.content)]
    sent = second.calls[0].messages
    assert sent[: len(expected)] == expected
    assert sent[len(expected) :] == [Message.user_text("again")]
    # The float in the tool input survived DynamoDB's Decimal detour.
    assert sent[1].content[1].input == {"note": "a", "score": 0.5}


def test_finalized_turn_rebuilds_to_end_on_the_tool_results(store):
    provider = FakeProvider([tool_reply(("finalize_meditation_brief", {"brief": "b"}, "tu-1"))])
    result = run(
        engine(provider).run_turn(
            TurnInput(history=[], user_text="go", turn=3), deadline=Deadline.never(), emit=silent
        )
    )
    assert result.finalized is not None

    store.create_agent_session(USER_ID, SESSION, engine="native", model_id="m")
    # Turn 3 directly: the store does not require turns 0-2 to exist.
    store.client.update_item(
        TableName=store.table_name,
        Key={"PK": {"S": f"USER#{USER_ID}"}, "SK": {"S": f"AGENT#{SESSION}"}},
        UpdateExpression="SET #t = :t",
        ExpressionAttributeNames={"#t": "turn"},
        ExpressionAttributeValues={":t": {"N": "3"}},
    )
    store.claim_turn(USER_ID, SESSION, engine="native", now=NOW)
    checkpoint = TurnCheckpoint.from_result(
        session_id=SESSION, turn=3, user_text="go", result=result
    )
    assert store.commit_turn(
        USER_ID, SESSION, expected_turn=3, checkpoint=checkpoint, finalized_job_id="job-7"
    )

    history = rebuild_messages(store.list_turns(USER_ID, SESSION))

    assert history[0] == Message.user_text("go")
    assert history[1] == Message.assistant(result.content)
    assert isinstance(history[2].content[0], ToolResultBlock)
    assert len(history) == 3
    session = store.get_agent_session(USER_ID, SESSION)
    assert session is not None
    assert session.status is AgentSessionStatus.FINALIZED and session.job_id == "job-7"


def test_rebuild_reapplies_the_converge_hint_from_the_turn_number():
    provider = FakeProvider([text_reply("ok")])
    result = run(
        engine(provider).run_turn(
            TurnInput(history=[], user_text="late", turn=9), deadline=Deadline.never(), emit=silent
        )
    )
    checkpoint = TurnCheckpoint.from_result(
        session_id=SESSION, turn=9, user_text="late", result=result
    )

    history = rebuild_messages([checkpoint])

    assert history[0] == provider.calls[0].messages[-1]
    assert isinstance(history[0].content[0], TextBlock)
    assert history[0].content[0].text.endswith(CONVERGE_HINT)
    # The raw user words are what the item keeps; the hint is not user content.
    assert checkpoint.user_text == "late"


def test_a_stored_empty_reply_is_replayed_as_the_fallback_line():
    """Rows written before the empty-reply guard: still a valid history."""
    from agent.prompt import EMPTY_REPLY_TEXT
    from shared.models import AgentTurn

    turns = [
        AgentTurn(
            session_id=SESSION, turn=0, user_text="hi", assistant_content=[], stop_reason="end_turn"
        ),
        AgentTurn(
            session_id=SESSION,
            turn=1,
            user_text="again",
            assistant_content=[{"text": "  "}],
            stop_reason="end_turn",
        ),
    ]

    history = rebuild_messages(turns)

    assert [m.role for m in history] == ["user", "assistant", "user", "assistant"]
    assert history[1] == Message.assistant([TextBlock(text=EMPTY_REPLY_TEXT)])
    assert history[3] == Message.assistant([TextBlock(text=EMPTY_REPLY_TEXT)])


def test_langgraph_turn_checkpoints_to_the_same_item_as_native(store):
    """The harness stores whichever engine ran without knowing which: the
    T-item and the history rebuilt from it must be the same for both."""
    pytest.importorskip("langgraph")
    from agent.contracts import TextDelta
    from agent.langgraph.engine import LangGraphEngine

    from .fake_chat_model import ScriptedChatModel

    calls = [("noop", {"note": "a", "score": 0.5}, "tu-1")]
    native = FakeProvider([tool_reply(*calls, text="checking"), text_reply("all set")])
    # The LangChain fake streams every character, as a real model would.
    scripted = ScriptedChatModel(
        script=[
            [TextDelta("checking"), *tool_reply(*calls, text="checking")],
            text_reply("all set"),
        ]
    )
    inp = TurnInput(history=[], user_text="hello", turn=0)
    native_result = run(engine(native).run_turn(inp, deadline=Deadline.never(), emit=silent))
    tools = ToolRegistry(
        [
            ToolSpec("noop", "noop", NoopIn, noop),
            ToolSpec("finalize_meditation_brief", "end", FinishIn, finish, terminal=True),
        ]
    )
    langgraph_engine = LangGraphEngine(
        scripted, tools, ToolContext(user_id=USER_ID, session_id=SESSION), system_prompt="S"
    )
    langgraph_result = run(langgraph_engine.run_turn(inp, deadline=Deadline.never(), emit=silent))

    assert langgraph_result == native_result
    stored = [
        TurnCheckpoint.from_result(
            session_id=SESSION, turn=0, user_text="hello", result=r, created_at=NOW
        )
        for r in (native_result, langgraph_result)
    ]
    assert stored[0] == stored[1]
    assert rebuild_messages([stored[0]]) == rebuild_messages([stored[1]])
