"""Failure path: refund the frozen credit.

Reached from the Catch on every state after freeze succeeds. It is also
reachable for a job that never froze anything (the Catch on freeze_credit's own
transport errors), which is why ``rollback_credit`` treats a PENDING or missing
job as a no-op rather than driving ``frozen`` negative.

The terminal status is ROLLED_BACK, not FAILED. FAILED sits *inside* rollback's
own allow-list (``status IN (FROZEN, GENERATING, FAILED)``), so writing it here
would let a retried rollback pass its own condition and refund the credit a
second time. ROLLED_BACK is outside the allow-list, which is what makes the
operation idempotent -- and it carries strictly more information: the job
failed *and* the credit was returned. The API surfaces it to clients as
"failed".
"""

from __future__ import annotations

import logging
import os
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from shared.audio import SweepError, sweep_job_objects
from shared.db import EntitlementStore
from shared.models import JobStatus
from shared.pipeline import PipelineState

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_store: EntitlementStore | None = None
_s3: Any = None


def _get_store() -> EntitlementStore:
    global _store
    if _store is None:
        _store = EntitlementStore()
    return _store


def _get_s3() -> Any:
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3")
    return _s3


def lambda_handler(event: dict[str, Any], context: object) -> dict[str, Any]:  # noqa: ARG001
    # Every Catch routing here sets result_path="$.error", which *merges* the
    # error into the original input rather than replacing it. The payload is
    # therefore a PipelineState carrying an extra `error` key, which the model
    # ignores. Dropping that result_path would replace the whole input with
    # {Error, Cause} and this validation would fail -- which is the behaviour to
    # want: a refund must never run against a payload that lost its user_id.
    state = PipelineState.model_validate(event)
    # Fail loudly on a misconfigured deployment rather than logging "sweep
    # failed" on every rollback and leaking narrations forever.
    bucket = os.environ["AUDIO_BUCKET"]

    result = _get_store().rollback_credit(state.user_id, state.job_id)
    logger.info(
        "rollback job_id=%s applied=%s status=%s",
        state.job_id,
        result.applied,
        result.job_status.value,
    )

    # A job that synthesized and then failed has a narration in the bucket:
    # untagged, so no lifecycle rule reaps it, and never DONE, so the user
    # cannot delete it either. Sweep it here -- but only when the ledger says
    # the job really is rolled back. This handler also runs as the Catch for
    # a commit whose transaction succeeded and whose invocation then failed;
    # rollback_credit reports that as a DONE replay, and sweeping there would
    # delete a paid, DONE dreamscape's narration. ROLLED_BACK covers both the
    # refund just applied and a retried rollback re-sweeping after a failed
    # first sweep. Never at the refund's expense: a failed sweep is logged,
    # not raised, because this is the last stop before the execution fails.
    if result.job_status is JobStatus.ROLLED_BACK:
        try:
            removed = sweep_job_objects(_get_s3(), bucket, state.job_id)
            logger.info("rollback swept job_id=%s objects=%d", state.job_id, removed)
        except (ClientError, BotoCoreError, SweepError):
            logger.warning("rollback sweep failed job_id=%s", state.job_id)

    return state.model_dump()
