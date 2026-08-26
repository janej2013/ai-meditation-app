"""The one generation-start path (shared.jobs), as both starters use it."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from shared.jobs import GateOutcome, GenerationStartError, generation_gate, start_generation
from shared.models import JobStatus

from .conftest import JOB_ID, USER_ID, seed_entitlement

ARN = "arn:aws:states:ap-southeast-2:123:stateMachine:m"


@pytest.fixture(autouse=True)
def _arn(monkeypatch):
    monkeypatch.setenv("STATE_MACHINE_ARN", ARN)


def client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "StartExecution")


# ----------------------------------------------------------------------
# Gate
# ----------------------------------------------------------------------


def test_gate_without_entitlement_is_no_credit(store):
    gate = generation_gate(store, USER_ID)

    assert gate.outcome is GateOutcome.NO_CREDIT
    assert gate.entitlement is None


def test_gate_with_zero_available_is_no_credit(store, dynamodb_client):
    seed_entitlement(dynamodb_client, available=0)

    gate = generation_gate(store, USER_ID)

    assert gate.outcome is GateOutcome.NO_CREDIT
    assert gate.entitlement is not None and gate.entitlement.available == 0


def test_gate_with_a_frozen_credit_is_in_flight(store, dynamodb_client):
    seed_entitlement(dynamodb_client, available=1, frozen=1)

    assert generation_gate(store, USER_ID).outcome is GateOutcome.JOB_IN_FLIGHT


def test_gate_open(store, dynamodb_client):
    seed_entitlement(dynamodb_client, available=2)

    gate = generation_gate(store, USER_ID)

    assert gate.outcome is GateOutcome.OK
    assert gate.entitlement is not None and gate.entitlement.available == 2


# ----------------------------------------------------------------------
# Start
# ----------------------------------------------------------------------


def test_start_writes_the_job_and_starts_the_execution(store):
    sfn = MagicMock()

    assert start_generation(
        store,
        sfn,
        user_id=USER_ID,
        job_id=JOB_ID,
        duration_minutes=5,
        mood_text="a brief",
        source="agent",
        agent_session_id="sess-1",
    )

    job = store.get_job(USER_ID, JOB_ID)
    assert job is not None
    assert job.status is JobStatus.PENDING
    assert job.mood_text == "a brief"
    assert job.source == "agent" and job.agent_session_id == "sess-1"
    sfn.start_execution.assert_called_once()
    kwargs = sfn.start_execution.call_args.kwargs
    assert kwargs["stateMachineArn"] == ARN
    assert kwargs["name"] == JOB_ID
    assert json.loads(kwargs["input"]) == {
        "user_id": USER_ID,
        "job_id": JOB_ID,
        "duration_minutes": 5,
    }


def test_start_omits_source_fields_when_not_given(store):
    assert start_generation(
        store, MagicMock(), user_id=USER_ID, job_id=JOB_ID, duration_minutes=5, mood_text="m"
    )

    job = store.get_job(USER_ID, JOB_ID)
    assert job is not None
    assert job.source is None and job.agent_session_id is None


def test_taken_job_id_is_false_and_starts_nothing_for_the_api(store):
    sfn = MagicMock()
    assert start_generation(store, sfn, user_id=USER_ID, job_id=JOB_ID, duration_minutes=5)

    assert not start_generation(store, sfn, user_id=USER_ID, job_id=JOB_ID, duration_minutes=5)

    assert sfn.start_execution.call_count == 1


def test_own_pending_job_is_replayed_for_the_agent(store):
    """The earlier attempt wrote the row but never started the execution."""
    failing = MagicMock()
    failing.start_execution.side_effect = client_error("ThrottlingException")
    with pytest.raises(GenerationStartError):
        start_generation(
            store, failing, user_id=USER_ID, job_id=JOB_ID, duration_minutes=5, agent_session_id="s"
        )

    sfn = MagicMock()
    assert start_generation(
        store, sfn, user_id=USER_ID, job_id=JOB_ID, duration_minutes=5, agent_session_id="s"
    )

    sfn.start_execution.assert_called_once()


def test_another_sessions_job_is_not_replayed(store):
    assert start_generation(
        store,
        MagicMock(),
        user_id=USER_ID,
        job_id=JOB_ID,
        duration_minutes=5,
        agent_session_id="s1",
    )
    sfn = MagicMock()

    assert not start_generation(
        store, sfn, user_id=USER_ID, job_id=JOB_ID, duration_minutes=5, agent_session_id="s2"
    )

    sfn.start_execution.assert_not_called()


def test_execution_already_exists_is_success(store):
    sfn = MagicMock()
    sfn.start_execution.side_effect = client_error("ExecutionAlreadyExists")

    assert start_generation(store, sfn, user_id=USER_ID, job_id=JOB_ID, duration_minutes=5)


def test_other_client_errors_raise(store):
    sfn = MagicMock()
    sfn.start_execution.side_effect = client_error("AccessDeniedException")

    with pytest.raises(GenerationStartError):
        start_generation(store, sfn, user_id=USER_ID, job_id=JOB_ID, duration_minutes=5)
    # The row stays: a retry meets it, and nothing was frozen.
    assert store.get_job(USER_ID, JOB_ID) is not None
