"""Step 1: reserve the user's credit.

``InsufficientCreditsError` propagates unchanged so Step Functions can Catch it
by name and fail the execution *without* routing through rollback_credit --
nothing was frozen, so there is nothing to refund.
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
    state = PipelineState.model_validate(event)

    result = _get_store().freeze_credit(state.user_id, state.job_id)
    logger.info(
        "freeze job_id=%s applied=%s status=%s",
        state.job_id,
        result.applied,
        result.job_status.value,
    )

    return state.model_dump()
