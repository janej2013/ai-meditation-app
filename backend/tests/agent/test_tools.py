"""The three real tools against a moto store and a stubbed Step Functions."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from functools import partial
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from agent.budget import FINALIZE_TOOL_NAME
from agent.contracts import Finalized, JsonBlock, TextBlock, ToolUseBlock
from agent.tools import finalize, history, memory
from agent.tools.default import default_registry
from agent.tools.registry import ToolContext
from shared.jobs import start_generation
from shared.models import AGENT_JOB_NAMESPACE, JobStatus, job_sk, user_pk

from ..conftest import TABLE_NAME, USER_ID, seed_entitlement
from .fake_provider import run

SESSION = "sess-1"
NOW = datetime(2026, 8, 25, 10, 0, tzinfo=UTC)
GOOD_BRIEF = "A calm evening meditation for someone who wants to set the day down slowly."


@pytest.fixture(autouse=True)
def _arn(monkeypatch):
    monkeypatch.setenv("STATE_MACHINE_ARN", "arn:aws:states:ap-southeast-2:123:stateMachine:m")


@pytest.fixture
def sfn():
    return MagicMock()


@pytest.fixture
def ctx(store, sfn):
    return ToolContext(
        user_id=USER_ID,
        session_id=SESSION,
        store=store,
        start_generation=partial(start_generation, store, sfn),
        now=lambda: NOW,
    )


def seed_done_job(
    client,
    job_id: str,
    *,
    created_at: str | None,
    mood_text: str | None = None,
    picture_key: str | None = None,
    keywords: list[str] | None = None,
    source: str | None = None,
    status: JobStatus = JobStatus.DONE,
    duration: int = 10,
) -> None:
    item = {
        "PK": {"S": user_pk(USER_ID)},
        "SK": {"S": job_sk(job_id)},
        "job_id": {"S": job_id},
        "status": {"S": status.value},
        "duration_minutes": {"N": str(duration)},
    }
    if created_at:
        item["created_at"] = {"S": created_at}
    if mood_text:
        item["mood_text"] = {"S": mood_text}
    if picture_key:
        item["picture_key"] = {"S": picture_key}
    if keywords:
        item["picture_keywords"] = {"L": [{"S": k} for k in keywords]}
    if source:
        item["source"] = {"S": source}
    client.put_item(TableName=TABLE_NAME, Item=item)


def call(registry, ctx, name: str, inp: dict, tool_use_id: str = "tu-1"):
    return run(registry.execute(ctx, ToolUseBlock(tool_use_id=tool_use_id, name=name, input=inp)))


def payload(execution) -> dict:
    block = execution.result.content[0]
    assert isinstance(block, JsonBlock), execution.result
    return block.data


def error_text(execution) -> str:
    assert execution.result.status == "error"
    block = execution.result.content[0]
    assert isinstance(block, TextBlock)
    return block.text


# ----------------------------------------------------------------------
# Registry shape
# ----------------------------------------------------------------------


def test_default_registry_order_and_terminal_tool():
    registry = default_registry()

    assert [spec.name for spec in registry] == [
        "get_session_history",
        "save_user_insight",
        FINALIZE_TOOL_NAME,
    ]
    assert finalize.SPEC.name == FINALIZE_TOOL_NAME and finalize.SPEC.terminal
    assert not history.SPEC.terminal and not memory.SPEC.terminal
    schema = registry.to_converse_spec()[2]["toolSpec"]["inputSchema"]["json"]
    assert set(schema["required"]) == {"brief", "duration_minutes"}


def test_tools_without_a_store_answer_with_errors(sfn):
    bare = ToolContext(user_id=USER_ID, session_id=SESSION)
    registry = default_registry()

    for name, inp in [
        ("get_session_history", {}),
        ("save_user_insight", {"insight": "slow pacing"}),
        (FINALIZE_TOOL_NAME, {"brief": GOOD_BRIEF, "duration_minutes": 5}),
    ]:
        execution = call(registry, bare, name, inp)
        assert execution.result.status == "error", name
        assert execution.finalized is None
    sfn.start_execution.assert_not_called()


# ----------------------------------------------------------------------
# get_session_history
# ----------------------------------------------------------------------


def test_history_is_empty_without_done_jobs(ctx, dynamodb_client):
    seed_done_job(
        dynamodb_client, "failed", created_at="2026-08-01T00:00:00+00:00", status=JobStatus.FAILED
    )

    data = payload(call(default_registry(), ctx, "get_session_history", {}))

    assert data == {"sessions": [], "total": 0}


def test_history_orders_limits_and_summarises(ctx, dynamodb_client):
    long_mood = "x" * 61
    seed_done_job(dynamodb_client, "old", created_at="2026-08-01T00:00:00+00:00", mood_text="calm")
    seed_done_job(dynamodb_client, "undated", created_at=None, mood_text="ancient")
    seed_done_job(
        dynamodb_client,
        "pic",
        created_at="2026-08-02T00:00:00+00:00",
        picture_key="pictures/u/p.jpg",
        keywords=["dusk", "shore"],
    )
    seed_done_job(
        dynamodb_client,
        "agent",
        created_at="2026-08-03T00:00:00+00:00",
        mood_text=long_mood,
        source="agent",
        duration=7,
    )
    seed_done_job(
        dynamodb_client, "deleted", created_at="2026-08-04T00:00:00+00:00", status=JobStatus.DELETED
    )

    data = payload(call(default_registry(), ctx, "get_session_history", {"limit": 3}))

    assert data["total"] == 4
    assert [s["source"] for s in data["sessions"]] == ["agent", "picture", "words"]
    newest = data["sessions"][0]
    assert newest["created_at"] == "2026-08-03T00:00:00+00:00"
    assert newest["duration_minutes"] == 7
    assert newest["excerpt"] == "x" * 60 + "…"
    assert newest["keywords"] is None
    picture = data["sessions"][1]
    assert picture["keywords"] == ["dusk", "shore"] and picture["excerpt"] is None
    assert data["sessions"][2]["excerpt"] == "calm"


def test_history_rejects_a_limit_out_of_range(ctx):
    assert "limit" in error_text(
        call(default_registry(), ctx, "get_session_history", {"limit": 11})
    )


# ----------------------------------------------------------------------
# save_user_insight
# ----------------------------------------------------------------------


def test_insight_saved_then_deduplicated(ctx, store):
    registry = default_registry()

    first = payload(
        call(registry, ctx, "save_user_insight", {"insight": "  prefers   slow pacing "})
    )
    second = payload(call(registry, ctx, "save_user_insight", {"insight": "Prefers slow pacing"}))

    assert first == {"saved": True}
    assert second == {"saved": False, "reason": "already_remembered"}
    memory_item = store.get_memory(USER_ID)
    assert [i.text for i in memory_item.insights] == ["prefers slow pacing"]
    assert memory_item.insights[0].created_at == NOW
    assert memory_item.insights[0].session_id == SESSION


def test_insight_length_is_validated(ctx):
    registry = default_registry()

    assert "insight" in error_text(call(registry, ctx, "save_user_insight", {"insight": "x" * 121}))
    assert "insight" in error_text(call(registry, ctx, "save_user_insight", {"insight": "  a  "}))


# ----------------------------------------------------------------------
# finalize_meditation_brief
# ----------------------------------------------------------------------


def test_finalize_without_credit_is_an_error_result(ctx, sfn, dynamodb_client):
    seed_entitlement(dynamodb_client, available=0)

    execution = call(
        default_registry(), ctx, FINALIZE_TOOL_NAME, {"brief": GOOD_BRIEF, "duration_minutes": 5}
    )

    assert error_text(execution) == finalize.NO_CREDIT_MESSAGE
    assert execution.finalized is None
    sfn.start_execution.assert_not_called()


def test_finalize_with_a_job_in_flight_is_an_error_result(ctx, sfn, dynamodb_client):
    seed_entitlement(dynamodb_client, available=1, frozen=1)

    execution = call(
        default_registry(), ctx, FINALIZE_TOOL_NAME, {"brief": GOOD_BRIEF, "duration_minutes": 5}
    )

    assert error_text(execution) == finalize.IN_FLIGHT_MESSAGE
    sfn.start_execution.assert_not_called()


def test_finalize_starts_the_job_and_ends_the_session(ctx, sfn, store, dynamodb_client):
    seed_entitlement(dynamodb_client, available=1)

    execution = call(
        default_registry(), ctx, FINALIZE_TOOL_NAME, {"brief": GOOD_BRIEF, "duration_minutes": 5}
    )

    job_id = finalize.agent_job_id(SESSION)
    assert execution.finalized == Finalized(job_id=job_id)
    assert payload(execution) == {"job_id": job_id}
    job = store.get_job(USER_ID, job_id)
    assert job is not None
    assert job.mood_text == GOOD_BRIEF
    assert job.source == "agent" and job.agent_session_id == SESSION
    assert job.duration_minutes == 5
    kwargs = sfn.start_execution.call_args.kwargs
    assert kwargs["name"] == job_id
    assert json.loads(kwargs["input"]) == {
        "user_id": USER_ID,
        "job_id": job_id,
        "duration_minutes": 5,
    }


def test_finalize_twice_is_one_job_and_one_start(ctx, sfn, dynamodb_client):
    seed_entitlement(dynamodb_client, available=1)
    registry = default_registry()
    inp = {"brief": GOOD_BRIEF, "duration_minutes": 5}

    first = call(registry, ctx, FINALIZE_TOOL_NAME, inp)
    # The second attempt meets its own PENDING row and re-issues the start;
    # Step Functions answers that the name is taken, which is success.
    sfn.start_execution.side_effect = ClientError(
        {"Error": {"Code": "ExecutionAlreadyExists", "Message": ""}}, "StartExecution"
    )
    second = call(registry, ctx, FINALIZE_TOOL_NAME, inp, tool_use_id="tu-2")

    assert first.finalized == second.finalized
    assert sfn.start_execution.call_count == 2  # asked twice, one execution


def test_finalize_after_a_failed_start_heals(ctx, sfn, dynamodb_client):
    seed_entitlement(dynamodb_client, available=1)
    registry = default_registry()
    inp = {"brief": GOOD_BRIEF, "duration_minutes": 5}
    sfn.start_execution.side_effect = ClientError(
        {"Error": {"Code": "ServiceUnavailable", "Message": ""}}, "StartExecution"
    )

    failed = call(registry, ctx, FINALIZE_TOOL_NAME, inp)
    assert error_text(failed) == finalize.START_FAILED_MESSAGE
    assert failed.finalized is None

    sfn.start_execution.side_effect = None
    healed = call(registry, ctx, FINALIZE_TOOL_NAME, inp, tool_use_id="tu-2")

    assert healed.finalized == Finalized(job_id=finalize.agent_job_id(SESSION))
    assert sfn.start_execution.call_count == 2


def test_job_id_is_per_session():
    assert finalize.agent_job_id("a") != finalize.agent_job_id("b")
    assert finalize.agent_job_id("a") == finalize.agent_job_id("a")
    assert str(AGENT_JOB_NAMESPACE) in repr(AGENT_JOB_NAMESPACE)


def test_finalize_validates_the_brief(ctx, sfn, dynamodb_client):
    seed_entitlement(dynamodb_client, available=1)

    execution = call(
        default_registry(), ctx, FINALIZE_TOOL_NAME, {"brief": "too short", "duration_minutes": 5}
    )

    assert "brief" in error_text(execution)
    sfn.start_execution.assert_not_called()
