"""Unit tests for the credit ledger.

The freeze/commit/rollback transactions are the correctness core of the whole
pipeline: Step Functions retries tasks and routes every failure to
``rollback_credit``, so these operations get replayed in production. Each test
below asserts on the counters, not just the return value -- a double deduction
is the failure that actually costs a user money.
"""

from __future__ import annotations

import logging

import pytest

from shared.db import (
    CreditLedgerError,
    EntitlementStore,
    InsufficientCreditsError,
    JobStateError,
)
from shared.models import JobStatus

from .conftest import JOB_ID, USER_ID, seed_entitlement, seed_job, set_available


def counters(store: EntitlementStore) -> tuple[int, int]:
    entitlement = store.get_entitlement(USER_ID)
    assert entitlement is not None
    return entitlement.available, entitlement.frozen


# ----------------------------------------------------------------------
# Happy paths
# ----------------------------------------------------------------------


def test_freeze_reserves_one_credit(store, dynamodb_client):
    seed_entitlement(dynamodb_client, available=1)

    result = store.freeze_credit(USER_ID, JOB_ID)

    assert result.applied is True
    assert result.job_status is JobStatus.FROZEN
    assert counters(store) == (0, 1)


def test_freeze_then_commit_consumes_the_credit(store, dynamodb_client):
    seed_entitlement(dynamodb_client, available=1)
    store.freeze_credit(USER_ID, JOB_ID)

    result = store.commit_credit(USER_ID, JOB_ID)

    assert result.applied is True
    assert result.job_status is JobStatus.DONE
    # available is NOT restored -- the credit is spent.
    assert counters(store) == (0, 0)


def test_freeze_then_rollback_returns_the_credit(store, dynamodb_client):
    seed_entitlement(dynamodb_client, available=1)
    store.freeze_credit(USER_ID, JOB_ID)

    result = store.rollback_credit(USER_ID, JOB_ID)

    assert result.applied is True
    assert result.job_status is JobStatus.ROLLED_BACK
    assert counters(store) == (1, 0)


def test_freeze_accepts_a_job_the_api_precreated_as_pending(store, dynamodb_client):
    seed_entitlement(dynamodb_client, available=1)
    seed_job(dynamodb_client, status=JobStatus.PENDING)

    result = store.freeze_credit(USER_ID, JOB_ID)

    assert result.applied is True
    assert counters(store) == (0, 1)


def test_commit_accepts_a_job_mid_flight_in_generating(store, dynamodb_client):
    seed_entitlement(dynamodb_client, available=1)
    store.freeze_credit(USER_ID, JOB_ID)
    dynamodb_client.update_item(
        TableName=store.table_name,
        Key={"PK": {"S": f"USER#{USER_ID}"}, "SK": {"S": f"JOB#{JOB_ID}"}},
        UpdateExpression="SET #s = :g",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":g": {"S": JobStatus.GENERATING.value}},
    )

    result = store.commit_credit(USER_ID, JOB_ID)

    assert result.applied is True
    assert counters(store) == (0, 0)


# ----------------------------------------------------------------------
# Insufficient credit
# ----------------------------------------------------------------------


def test_freeze_with_zero_credits_raises(store, dynamodb_client):
    seed_entitlement(dynamodb_client, available=0)

    with pytest.raises(InsufficientCreditsError):
        store.freeze_credit(USER_ID, JOB_ID)

    assert counters(store) == (0, 0)
    # The transaction is atomic: no job item should have been created.
    assert store.get_job(USER_ID, JOB_ID) is None


def test_freeze_without_an_entitlement_item_raises(store):
    with pytest.raises(InsufficientCreditsError):
        store.freeze_credit(USER_ID, JOB_ID)

    assert store.get_job(USER_ID, JOB_ID) is None


# ----------------------------------------------------------------------
# Idempotency -- the reason these are transactions at all
# ----------------------------------------------------------------------


def test_freeze_twice_does_not_double_deduct(store, dynamodb_client):
    seed_entitlement(dynamodb_client, available=2)
    store.freeze_credit(USER_ID, JOB_ID)

    result = store.freeze_credit(USER_ID, JOB_ID)

    assert result.applied is False
    assert result.job_status is JobStatus.FROZEN
    assert counters(store) == (1, 1)


def test_freeze_replay_after_balance_hits_zero_is_still_a_no_op(store, dynamodb_client):
    """Both conditions fail at once; the JOB reason must win.

    Reading the entitlement reason first would raise InsufficientCreditsError
    for what is really a harmless retry of an already-frozen job.
    """
    seed_entitlement(dynamodb_client, available=1)
    store.freeze_credit(USER_ID, JOB_ID)
    assert counters(store) == (0, 1)

    result = store.freeze_credit(USER_ID, JOB_ID)

    assert result.applied is False
    assert result.job_status is JobStatus.FROZEN
    assert counters(store) == (0, 1)


def test_commit_twice_is_a_no_op(store, dynamodb_client):
    seed_entitlement(dynamodb_client, available=1)
    store.freeze_credit(USER_ID, JOB_ID)
    store.commit_credit(USER_ID, JOB_ID)

    result = store.commit_credit(USER_ID, JOB_ID)

    assert result.applied is False
    assert result.job_status is JobStatus.DONE
    assert counters(store) == (0, 0)


def test_rollback_twice_is_a_no_op(store, dynamodb_client):
    seed_entitlement(dynamodb_client, available=1)
    store.freeze_credit(USER_ID, JOB_ID)
    store.rollback_credit(USER_ID, JOB_ID)

    result = store.rollback_credit(USER_ID, JOB_ID)

    assert result.applied is False
    assert result.job_status is JobStatus.ROLLED_BACK
    assert counters(store) == (1, 0)


# ----------------------------------------------------------------------
# Rollback edge cases the state machine will actually hit
# ----------------------------------------------------------------------


def test_rollback_after_commit_never_refunds(store, dynamodb_client):
    seed_entitlement(dynamodb_client, available=1)
    store.freeze_credit(USER_ID, JOB_ID)
    store.commit_credit(USER_ID, JOB_ID)

    result = store.rollback_credit(USER_ID, JOB_ID)

    assert result.applied is False
    assert result.job_status is JobStatus.DONE
    assert counters(store) == (0, 0)


def test_rollback_when_freeze_never_happened_is_a_no_op(store, dynamodb_client):
    """Constraint 3 catches failures on freeze_credit itself, so this runs."""
    seed_entitlement(dynamodb_client, available=1)

    result = store.rollback_credit(USER_ID, JOB_ID)

    assert result.applied is False
    assert counters(store) == (1, 0)


def test_rollback_on_a_pending_job_does_not_drive_frozen_negative(store, dynamodb_client):
    seed_entitlement(dynamodb_client, available=1)
    seed_job(dynamodb_client, status=JobStatus.PENDING)

    result = store.rollback_credit(USER_ID, JOB_ID)

    assert result.applied is False
    assert result.job_status is JobStatus.PENDING
    assert counters(store) == (1, 0)


def test_rollback_of_a_failed_job_returns_the_credit(store, dynamodb_client):
    seed_entitlement(dynamodb_client, available=1)
    store.freeze_credit(USER_ID, JOB_ID)
    dynamodb_client.update_item(
        TableName=store.table_name,
        Key={"PK": {"S": f"USER#{USER_ID}"}, "SK": {"S": f"JOB#{JOB_ID}"}},
        UpdateExpression="SET #s = :f",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":f": {"S": JobStatus.FAILED.value}},
    )

    result = store.rollback_credit(USER_ID, JOB_ID)

    assert result.applied is True
    assert counters(store) == (1, 0)


# ----------------------------------------------------------------------
# Illegal transitions -- a state machine bug, so they must never pass silently
# ----------------------------------------------------------------------


def test_commit_retry_after_the_user_deleted_the_dream_is_a_replay(store, dynamodb_client):
    """commit succeeded, the Lambda response was lost, and before Step
    Functions retried the user let the finished dreamscape go: a replay, not
    a state error, and the credit is not consumed twice."""
    seed_entitlement(dynamodb_client, available=1)
    store.create_job(USER_ID, JOB_ID, "calm", 10)
    store.freeze_credit(USER_ID, JOB_ID)
    store.commit_credit(USER_ID, JOB_ID)
    assert store.mark_job_deleted(USER_ID, JOB_ID)

    store.commit_credit(USER_ID, JOB_ID)  # the retry

    assert store.get_entitlement(USER_ID).available == 0
    assert store.get_job(USER_ID, JOB_ID).status is JobStatus.DELETED


def test_freeze_replay_on_a_deleted_dream_is_a_no_op(store, dynamodb_client):
    """The start_execution name guard keeps a job_id unique for 90 days; past
    that, or on a hand-replayed execution, a freeze that meets a DELETED job
    is the same benign replay as meeting a DONE one."""
    seed_entitlement(dynamodb_client, available=2)
    store.create_job(USER_ID, JOB_ID, "calm", 10)
    store.freeze_credit(USER_ID, JOB_ID)
    store.commit_credit(USER_ID, JOB_ID)
    assert store.mark_job_deleted(USER_ID, JOB_ID)

    result = store.freeze_credit(USER_ID, JOB_ID)

    assert not result.applied
    assert store.get_entitlement(USER_ID).available == 1


def test_rollback_after_the_user_deleted_the_dream_never_refunds(store, dynamodb_client):
    seed_entitlement(dynamodb_client, available=1)
    store.create_job(USER_ID, JOB_ID, "calm", 10)
    store.freeze_credit(USER_ID, JOB_ID)
    store.commit_credit(USER_ID, JOB_ID)
    assert store.mark_job_deleted(USER_ID, JOB_ID)

    result = store.rollback_credit(USER_ID, JOB_ID)

    assert not result.applied
    assert store.get_entitlement(USER_ID).available == 0


def test_commit_after_rollback_raises(store, dynamodb_client):
    """The credit was already refunded; charging for it now would bill twice."""
    seed_entitlement(dynamodb_client, available=1)
    store.freeze_credit(USER_ID, JOB_ID)
    store.rollback_credit(USER_ID, JOB_ID)

    with pytest.raises(JobStateError):
        store.commit_credit(USER_ID, JOB_ID)

    assert counters(store) == (1, 0)


def test_commit_on_a_job_that_never_froze_raises(store, dynamodb_client):
    seed_entitlement(dynamodb_client, available=1)
    seed_job(dynamodb_client, status=JobStatus.PENDING)

    with pytest.raises(JobStateError):
        store.commit_credit(USER_ID, JOB_ID)

    assert counters(store) == (1, 0)


def test_commit_with_no_frozen_credit_raises(store, dynamodb_client):
    """The job says FROZEN but the counter disagrees -- the ledger is corrupt."""
    seed_entitlement(dynamodb_client, available=1, frozen=0)
    seed_job(dynamodb_client, status=JobStatus.FROZEN)

    with pytest.raises(CreditLedgerError) as exc_info:
        store.commit_credit(USER_ID, JOB_ID)

    # The base class, not JobStateError: the job transition was legal, the
    # counters were not.
    assert type(exc_info.value) is CreditLedgerError
    assert counters(store) == (1, 0)


def test_rollback_with_no_frozen_credit_raises(store, dynamodb_client):
    seed_entitlement(dynamodb_client, available=1, frozen=0)
    seed_job(dynamodb_client, status=JobStatus.FROZEN)

    with pytest.raises(CreditLedgerError) as exc_info:
        store.rollback_credit(USER_ID, JOB_ID)

    assert type(exc_info.value) is CreditLedgerError
    assert counters(store) == (1, 0)


def test_rollback_for_a_user_with_no_ledger_is_a_no_op(store):
    """No entitlement item and no job item: nothing to refund, nothing to alarm on."""
    result = store.rollback_credit(USER_ID, JOB_ID)

    assert result.applied is False
    assert result.job_status is JobStatus.PENDING
    assert (result.entitlement.available, result.entitlement.frozen) == (0, 0)


# ----------------------------------------------------------------------
# Isolation between jobs and users
# ----------------------------------------------------------------------


def test_two_jobs_freeze_independently(store, dynamodb_client):
    seed_entitlement(dynamodb_client, available=2)

    store.freeze_credit(USER_ID, "job-1")
    store.freeze_credit(USER_ID, "job-2")

    assert counters(store) == (0, 2)


def test_second_freeze_fails_once_credits_run_out(store, dynamodb_client):
    seed_entitlement(dynamodb_client, available=1)
    store.freeze_credit(USER_ID, "job-1")

    with pytest.raises(InsufficientCreditsError):
        store.freeze_credit(USER_ID, "job-2")

    assert counters(store) == (0, 1)
    assert store.get_job(USER_ID, "job-2") is None


def test_set_available_helper_reflects_a_topped_up_balance(store, dynamodb_client):
    seed_entitlement(dynamodb_client, available=0)
    set_available(dynamodb_client, 3)

    result = store.freeze_credit(USER_ID, JOB_ID)

    assert result.applied is True
    assert counters(store) == (2, 1)


# ----------------------------------------------------------------------
# Job lifecycle updates
#
# Both halves of a JOB condition fail with the same exception, so the only
# thing separating a benign replay from a missing row is what gets logged.
# ----------------------------------------------------------------------


def _skip_record(caplog: pytest.LogCaptureFixture) -> logging.LogRecord:
    return next(r for r in caplog.records if "job update skipped" in r.getMessage())


def test_job_update_on_an_advanced_status_logs_a_replay(store, dynamodb_client, caplog):
    """What a retried task hits once the job has moved past the transition."""
    seed_job(dynamodb_client, status=JobStatus.DONE)

    with caplog.at_level(logging.INFO, logger="shared.db"):
        store.mark_job_generating(USER_ID, JOB_ID)

    record = _skip_record(caplog)
    assert record.levelno == logging.INFO
    # The status it found is the whole point -- it says why the write was
    # skipped, and it is the one job attribute constraint 7 allows in a log.
    assert JobStatus.DONE.value in record.getMessage()


def test_job_update_without_a_job_item_warns(store, caplog):
    """A missing row is not a replay: create_job runs before the execution."""
    with caplog.at_level(logging.INFO, logger="shared.db"):
        store.mark_job_generating(USER_ID, "job-never-created")

    record = _skip_record(caplog)
    assert record.levelno == logging.WARNING
    assert "no job item" in record.getMessage()


def test_job_update_never_logs_the_mood_text(store, caplog):
    """The condition-failure path reads the item back, so it could leak.

    ``create_job`` leaves the job PENDING, which is outside the allow-list, so
    the condition fails against a real item that still carries the mood text.
    """
    store.create_job(USER_ID, JOB_ID, "I feel overwhelmed about my job", 10)

    with caplog.at_level(logging.INFO, logger="shared.db"):
        store.mark_job_generating(USER_ID, JOB_ID)

    assert _skip_record(caplog).levelno == logging.INFO
    assert "overwhelmed" not in caplog.text
