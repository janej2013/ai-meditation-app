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

from shared.db import EntitlementStore
from shared.pipeline import PipelineState

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_store: EntitlementStore | None = None


def _get_store() -> EntitlementStore:
    global _store
    if _store is None:
        _store = EntitlementStore()
    return _store


def lambda_handler(event: dict[str, Any], context: object) -> dict[str, Any]:  # noqa: ARG001
    # Every Catch routing here sets result_path="$.error", which *merges* the
    # error into the original input rather than replacing it. The payload is
    # therefore a PipelineState carrying an extra `error` key, which the model
    # ignores. Dropping that result_path would replace the whole input with
    # {Error, Cause} and this validation would fail -- which is the behaviour to
    # want: a refund must never run against a payload that lost its user_id.
    state = PipelineState.model_validate(event)

    result = _get_store().rollback_credit(state.user_id, state.job_id)
    logger.info(
        "rollback job_id=%s applied=%s status=%s",
        state.job_id,
        result.applied,
        result.job_status.value,
    )

    return state.model_dump()
