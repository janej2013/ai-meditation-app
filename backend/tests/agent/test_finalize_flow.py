"""Completion criterion 2: a scripted conversation through the real tools.

History, then an insight, then finalize -- each turn claimed, run on the
native engine with the default registry, and committed, exactly as the
harness will do it. The store is moto; Step Functions is a stub.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from functools import partial
from unittest.mock import MagicMock

import pytest

from agent.budget import FINALIZE_TOOL_NAME
from agent.checkpoint import TurnCheckpoint, rebuild_messages
from agent.contracts import Deadline, TurnInput
from agent.native.loop import NativeEngine
from agent.tools.default import default_registry
from agent.tools.finalize import agent_job_id
from agent.tools.registry import ToolContext
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
        [tool_reply((FINALIZE_TOOL_NAME, {"brief": BRIEF, "duration_minutes": 5}, "tu-3"))],
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
        assert store.commit_turn(
            USER_ID,
            SESSION,
            expected_turn=turn,
            checkpoint=checkpoint,
            finalized_job_id=result.finalized.job_id if result.finalized else None,
        )
        results.append(result)
    return results


def test_scripted_conversation_ends_in_a_started_job(store, dynamodb_client):
    seed_entitlement(dynamodb_client, available=1)
    sfn = MagicMock()
    assert store.create_agent_session(USER_ID, SESSION, engine="native", model_id="fake")

    results = drive(store, sfn, TURNS)

    job_id = agent_job_id(SESSION)
    assert results[-1].finalized is not None and results[-1].finalized.job_id == job_id
    assert [r.finalized for r in results[:2]] == [None, None]

    job = store.get_job(USER_ID, job_id)
    assert job is not None
    assert job.status is JobStatus.PENDING
    assert job.mood_text == BRIEF
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
    assert session.turn == 3
    assert len(store.list_turns(USER_ID, SESSION)) == 3
    assert [i.text for i in store.get_memory(USER_ID).insights] == ["prefers slow narration"]
    # The history tool saw an empty collection: the job it started is not DONE.
    first_round = results[0].tool_log[0]
    assert first_round.output[0].data == {"sessions": [], "total": 0}


def test_finalizing_again_after_the_session_closed_is_refused_by_the_claim(store, dynamodb_client):
    seed_entitlement(dynamodb_client, available=1)
    sfn = MagicMock()
    store.create_agent_session(USER_ID, SESSION, engine="native", model_id="fake")
    drive(store, sfn, TURNS)

    from shared.db import AgentTurnBusyError

    with pytest.raises(AgentTurnBusyError):
        store.claim_turn(USER_ID, SESSION, engine="native", now=NOW)
    assert sfn.start_execution.call_count == 1
