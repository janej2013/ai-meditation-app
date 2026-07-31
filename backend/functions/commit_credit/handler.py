"""Step 5: consume the frozen credit and mark the job DONE.

DONE is written *here*, not in mix_audio, because commit_credit writes it in
the same TransactWriteItems that decrements ``frozen``. If mix_audio had
already set DONE, this transaction's job condition
(``status IN (FROZEN, GENERATING)``) would fail, the whole transaction would
cancel, and the credit would stay frozen forever.
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

    result = _get_store().commit_credit(state.user_id, state.job_id)
    logger.info(
        "commit job_id=%s applied=%s status=%s",
        state.job_id,
        result.applied,
        result.job_status.value,
    )

    return state.model_dump()
