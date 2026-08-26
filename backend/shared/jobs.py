"""Starting a generation: one path for the API and the companion agent.

``POST /generate`` and the agent's terminal tool both end here, which is
what keeps constraint 2 honest with two starters: the checks, the JOB row
and the execution start are written once, and the credit is still frozen
only inside the state machine (``FreezeCredit`` is its first task).

Nothing in this module knows HTTP. The gate returns an outcome for the
caller to map -- the router to 402/429, the tool to an error result the
model can read -- and a start that cannot happen raises
``GenerationStartError`` for the same reason.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from botocore.exceptions import ClientError

from shared.db import EntitlementStore
from shared.models import Entitlement, JobSource, JobStatus, PictureDescription

logger = logging.getLogger(__name__)

# Step Functions rejects a second execution with the name of one that ran in
# the last 90 days. Since the name is the job id, that rejection means the
# job is already being processed -- a success for an idempotent caller.
_EXECUTION_ALREADY_EXISTS = "ExecutionAlreadyExists"


class GateOutcome(StrEnum):
    OK = "OK"
    NO_CREDIT = "NO_CREDIT"
    JOB_IN_FLIGHT = "JOB_IN_FLIGHT"


@dataclass(frozen=True)
class Gate:
    outcome: GateOutcome
    # Present whenever an ENTITLEMENT item exists, whatever the outcome, so
    # a caller that only cares about credit (the picture routes) can still
    # read the balance.
    entitlement: Entitlement | None


class GenerationStartError(Exception):
    """StartExecution failed. The JOB row exists but nothing will process
    it; no credit was frozen, so nothing leaks -- the caller may retry."""


def generation_gate(store: EntitlementStore, user_id: str) -> Gate:
    """May this user start a generation right now?

    Credit first, then concurrency. ``frozen >= 1`` is the invariant the
    ledger already maintains for an in-flight job, so the one-at-a-time rule
    needs no extra query and no GSI.

    It is not a hard lock: a job sits in PENDING for the moment between the
    row being written and ``freeze_credit`` running, so two starts landing
    inside that window both pass. The ledger bounds the damage -- the second
    freeze fails its ``available >= 1`` condition and that execution ends in
    InsufficientCredits without producing anything.
    """
    entitlement = store.get_entitlement(user_id)
    if entitlement is None or entitlement.available < 1:
        return Gate(GateOutcome.NO_CREDIT, entitlement)
    if entitlement.frozen >= 1:
        return Gate(GateOutcome.JOB_IN_FLIGHT, entitlement)
    return Gate(GateOutcome.OK, entitlement)


def start_generation(
    store: EntitlementStore,
    sfn: Any,
    *,
    user_id: str,
    job_id: str,
    duration_minutes: int,
    mood_text: str | None = None,
    picture_key: str | None = None,
    description: PictureDescription | None = None,
    source: JobSource | None = None,
    agent_session_id: str | None = None,
) -> bool:
    """Write the JOB row and start its execution. False if the id is taken.

    The execution input carries ids and the duration only; the mood text
    (or the agent's brief) lives on the JOB item, never in the execution
    history (constraint 7).

    Replay is recognised by ``agent_session_id``: the agent derives its job
    id from the session, so a second finalize meets its own PENDING row and
    must still make sure the execution runs -- the earlier attempt may have
    died between the write and the start. The API never passes a session id
    (its ids are uuid4), so for it a taken id stays a plain False.
    """
    created = store.create_job(
        user_id,
        job_id,
        mood_text,
        duration_minutes,
        picture_key,
        description,
        source=source,
        agent_session_id=agent_session_id,
    )
    if not created:
        if agent_session_id is None or not _is_own_pending_job(
            store, user_id, job_id, agent_session_id
        ):
            return False
        logger.info("generation replay job_id=%s", job_id)

    try:
        sfn.start_execution(
            stateMachineArn=os.environ["STATE_MACHINE_ARN"],
            name=job_id,
            input=json.dumps(
                {"user_id": user_id, "job_id": job_id, "duration_minutes": duration_minutes}
            ),
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == _EXECUTION_ALREADY_EXISTS:
            logger.info("generation already running job_id=%s", job_id)
            return True
        logger.exception("failed to start execution job_id=%s", job_id)
        raise GenerationStartError(job_id) from exc

    # Never the mood text or the brief (constraint 7).
    logger.info(
        "generation started job_id=%s duration=%d picture=%s source=%s",
        job_id,
        duration_minutes,
        picture_key is not None,
        source,
    )
    return True


def _is_own_pending_job(
    store: EntitlementStore, user_id: str, job_id: str, agent_session_id: str
) -> bool:
    job = store.get_job(user_id, job_id)
    return (
        job is not None
        and job.agent_session_id == agent_session_id
        and job.status is JobStatus.PENDING
    )
