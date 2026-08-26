"""A scripted conversation through the real tools, then the listener's
confirmation -- the only step that starts a generation.

History, then an insight, then a proposal: each turn claimed, run on the
native engine with the default registry, and committed, as the runner
does it. Step Functions is a stub and is asserted untouched until
``confirm_session``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from functools import partial
from unittest.mock import MagicMock

import pytest

from agent.budget import FINALIZE_TOOL_NAME
from agent.checkpoint import TurnCheckpoint, rebuild_messages
from agent.contracts import Deadline, Proposal, TurnInput
from agent.native.loop import NativeEngine
from agent.tools.default import default_registry
from agent.tools.finalize import agent_job_id
from agent.tools.registry import ToolContext
from agent_runner.turns import confirm_session
from shared.db import AgentTurnBusyError
from shared.jobs import start_generation
from shared.models import AgentSessionStatus, JobStatus

from ..conftest import USER_ID, seed_entitlement
from .fake_provider import FakeProvider, run, text_reply, tool_reply

SESSION = "sess-flow"
NOW = datetime(2026, 8, 25, 10, 0, tzinfo=UTC)
BRIEF = "A short, slow evening meditation about setting the day down; shoreline imagery."

TURNS = [
    (
        "I'd like to wind down.",
        [tool_reply(("get_session_history", {"limit": 3}, "tu-1")), text_reply("Welcome back.")],
    ),
    (
        "Slow, please.",
        [
            tool_reply(("save_user_insight", {"insight": "prefers slow narration"}, "tu-2")),
            text_reply("Noted."),
        ],
    ),
    (
        "Go ahead.",
        [
            tool_reply((FINALIZE_TOOL_NAME, {"brief": BRIEF, "duration_minutes": 5}, "tu-3")),
            text_reply("I've prepared a five-minute meditation; start it whenever you like."),
        ],
    ),
]


async def silent(event) -> None:
    return None


@pytest.fixture(autouse=True)
def _arn(monkeypatch):
    monkeypatch.setenv("STATE_MACHINE_ARN", "arn:aws:states:ap-southeast-2:123:stateMachine:m")


def drive(store, sfn, script) -> list:
    context = ToolContext(
        user_id=USER_ID,
        session_id=SESSION,
        store=store,
        start_generation=partial(start_generation, store, sfn),
        now=lambda: NOW,
    )
    results = []
    for turn, (user_text, events) in enumerate(script):
        session = store.claim_turn(USER_ID, SESSION, engine="native", now=NOW)
        assert session.turn == turn
        history = rebuild_messages(store.list_turns(USER_ID, SESSION))
        # A new engine and provider per turn: nothing survives in memory.
        engine = NativeEngine(FakeProvider(events), default_registry(), context)
        result = run(
            engine.run_turn(
                TurnInput(history=history, user_text=user_text, turn=turn),
                deadline=Deadline.never(),
                emit=silent,
            )
        )
        checkpoint = TurnCheckpoint.from_result(
            session_id=SESSION, turn=turn, user_text=user_text, result=result
        )
        assert store.commit_turn(USER_ID, SESSION, expected_turn=turn, checkpoint=checkpoint)
        results.append(result)
    return results


def test_conversation_ends_in_a_proposal_and_confirmation_starts_the_job(store, dynamodb_client):
    seed_entitlement(dynamodb_client, available=1)
    sfn = MagicMock()
    assert store.create_agent_session(USER_ID, SESSION, engine="native", model_id="fake")

    results = drive(store, sfn, TURNS)

    # The model proposed; the turn went on to say so; nothing was started.
    assert results[-1].proposal == Proposal(duration_minutes=5)
    assert results[-1].finalized is None
    assert [r.proposal for r in results[:2]] == [None, None]
    sfn.start_execution.assert_not_called()
    session = store.get_agent_session(USER_ID, SESSION)
    assert session is not None
    assert session.status is AgentSessionStatus.ACTIVE and session.turn == 3
    assert session.pending_brief == BRIEF and session.pending_duration_minutes == 5
    assert [i.text for i in store.get_memory(USER_ID).insights] == ["prefers slow narration"]

    job_id = run(
        confirm_session(store, sfn, user_id=USER_ID, session_id=SESSION, engine_name="native")
    )

    assert job_id == agent_job_id(SESSION)
    job = store.get_job(USER_ID, job_id)
    assert job is not None
    assert job.status is JobStatus.PENDING and job.mood_text == BRIEF
    assert job.source == "agent" and job.agent_session_id == SESSION
    sfn.start_execution.assert_called_once()
    kwargs = sfn.start_execution.call_args.kwargs
    assert kwargs["name"] == job_id
    assert json.loads(kwargs["input"]) == {
        "user_id": USER_ID,
        "job_id": job_id,
        "duration_minutes": 5,
    }
    session = store.get_agent_session(USER_ID, SESSION)
    assert session is not None
    assert session.status is AgentSessionStatus.FINALIZED and session.job_id == job_id
    assert session.turn == 3 and session.pending_brief is None
    assert len(store.list_turns(USER_ID, SESSION)) == 3


def test_confirming_twice_is_one_job(store, dynamodb_client):
    seed_entitlement(dynamodb_client, available=1)
    sfn = MagicMock()
    store.create_agent_session(USER_ID, SESSION, engine="native", model_id="fake")
    drive(store, sfn, TURNS)

    first = run(
        confirm_session(store, sfn, user_id=USER_ID, session_id=SESSION, engine_name="native")
    )
    second = run(
        confirm_session(store, sfn, user_id=USER_ID, session_id=SESSION, engine_name="native")
    )

    assert first == second
    assert sfn.start_execution.call_count == 1
    with pytest.raises(AgentTurnBusyError):
        store.claim_turn(USER_ID, SESSION, engine="native", now=NOW)
