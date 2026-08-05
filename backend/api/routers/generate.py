"""Generation routes.

Constraint 2: this Lambda only validates the JWT, writes the job row and starts
the state machine. Bedrock, TTS and ffmpeg all run inside Step Functions.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Any

import boto3
from botocore.exceptions import ClientError
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from api import cloudfront_signer
from api.deps import CurrentUserDep, StoreDep
from shared.models import JobStatus
from shared.pipeline import MAX_DURATION_MINUTES, MIN_DURATION_MINUTES

logger = logging.getLogger(__name__)

router = APIRouter(tags=["generate"])

_sfn: Any = None


def _get_sfn() -> Any:
    global _sfn
    if _sfn is None:
        _sfn = boto3.client("stepfunctions")
    return _sfn


class GenerateRequest(BaseModel):
    """How the user feels, and how long a meditation they want."""

    mood: str = Field(min_length=1, max_length=500)
    duration_minutes: int = Field(ge=MIN_DURATION_MINUTES, le=MAX_DURATION_MINUTES)


class GenerateResponse(BaseModel):
    job_id: str
    status: JobStatus


class JobResponse(BaseModel):
    job_id: str
    status: JobStatus
    audio_url: str | None = None


@router.post(
    "/generate",
    response_model=GenerateResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def generate(payload: GenerateRequest, user: CurrentUserDep, store: StoreDep) -> GenerateResponse:
    entitlement = store.get_entitlement(user.sub)
    if entitlement is None or entitlement.available < 1:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="No generations remaining. Add credits to continue.",
        )

    _reject_if_job_in_flight(entitlement.frozen)

    job_id = str(uuid.uuid4())
    if not store.create_job(user.sub, job_id, payload.mood, payload.duration_minutes):
        # A uuid4 collision is not a thing; this means a retry replayed.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="That job already exists.")

    try:
        _get_sfn().start_execution(
            stateMachineArn=os.environ["STATE_MACHINE_ARN"],
            name=job_id,
            input=json.dumps(
                {
                    "user_id": user.sub,
                    "job_id": job_id,
                    "duration_minutes": payload.duration_minutes,
                }
            ),
        )
    except ClientError:
        # The job row exists but nothing will ever process it. No credit was
        # frozen, so nothing leaks -- surface a retryable error.
        logger.exception("failed to start execution job_id=%s", job_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not start generation. Please retry.",
        ) from None

    # Never the mood text (constraint 7).
    logger.info("generation started job_id=%s duration=%d", job_id, payload.duration_minutes)
    return GenerateResponse(job_id=job_id, status=JobStatus.PENDING)


@router.get("/jobs/{job_id}", response_model=JobResponse)
def get_job(job_id: str, user: CurrentUserDep, store: StoreDep) -> JobResponse:
    # Scoped to the caller's partition, so another user's job is simply absent
    # rather than forbidden -- no existence oracle.
    job = store.get_job(user.sub, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")

    audio_url = None
    if job.status is JobStatus.DONE and job.audio_key:
        audio_url = _audio_url(job.audio_key)

    return JobResponse(
        job_id=job.job_id,
        # ROLLED_BACK means "failed, and the credit was refunded". Clients only
        # need to know it failed.
        status=JobStatus.FAILED if job.status is JobStatus.ROLLED_BACK else job.status,
        audio_url=audio_url,
    )


def _reject_if_job_in_flight(frozen: int) -> None:
    """One generation at a time, keeping abuse and cost bounded.

    ``frozen >= 1`` is the invariant the credit ledger already maintains for an
    in-flight job, so this needs no extra query and no GSI.

    It is not a hard lock: a job sits in PENDING for the moment between the row
    being written and freeze_credit running, so two requests landing inside
    that window both pass. The ledger bounds the damage -- the second freeze
    fails its `available >= 1` condition and that execution ends in
    InsufficientCredits without producing anything.
    """
    if frozen >= 1:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="A generation is already in progress. Wait for it to finish.",
        )


def _audio_url(audio_key: str) -> str:
    """A short-lived CloudFront signed URL for the narration (constraint 6).

    Signing happens at the edge distribution, not on the bucket: the object is
    served from CloudFront and the bucket stays reachable only through OAC.
    Only ``jobs/*`` is signed -- the shared BGM under ``assets/*`` is public and
    cached, so the player can switch tracks without a round trip here.
    """
    return cloudfront_signer.signed_url(audio_key)
