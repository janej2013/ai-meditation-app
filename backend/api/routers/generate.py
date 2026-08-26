"""Generation routes.

Constraint 2: this Lambda only validates the JWT, writes the job row and starts
the state machine. Bedrock, TTS and ffmpeg all run inside Step Functions.

The checks and the start itself live in ``shared.jobs`` -- the companion
agent's terminal tool starts a generation through the same function -- so
this module only maps outcomes to HTTP.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any
from uuid import UUID

import boto3
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field, model_validator

from api import cloudfront_signer
from api.deps import CurrentUserDep, StoreDep
from shared.jobs import Gate, GateOutcome, GenerationStartError, generation_gate, start_generation
from shared.models import JobStatus, PictureDescription, PictureStatus, picture_key
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

    # A session drifts from words or from a picture -- exactly one of them.
    mood: str | None = Field(default=None, min_length=1, max_length=500)
    duration_minutes: int = Field(ge=MIN_DURATION_MINUTES, le=MAX_DURATION_MINUTES)
    # From POST /pictures/upload, already described. The key is rebuilt from
    # the caller's own subject, so a client cannot point a job at anyone
    # else's picture.
    picture_id: UUID | None = None

    @model_validator(mode="after")
    def _one_source(self) -> GenerateRequest:
        if (self.mood is None) == (self.picture_id is None):
            raise ValueError("provide either mood or picture_id, not both")
        return self


class GenerateResponse(BaseModel):
    job_id: str
    status: JobStatus


class JobResponse(BaseModel):
    job_id: str
    status: JobStatus
    audio_url: str | None = None
    # Picture jobs: the keywords for the waiting screen and, once DONE, a
    # signed URL to the upload so a revisited dreamscape's cloud is the
    # user's own picture again. Both are minted per call, like audio_url.
    picture_keywords: list[str] | None = None
    picture_url: str | None = None


@router.post(
    "/generate",
    response_model=GenerateResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def generate(payload: GenerateRequest, user: CurrentUserDep, store: StoreDep) -> GenerateResponse:
    _raise_unless_open(generation_gate(store, user.sub))

    key: str | None = None
    description: PictureDescription | None = None
    if payload.picture_id is not None:
        picture = store.get_picture(user.sub, str(payload.picture_id))
        if picture is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Picture not found.")
        if (
            picture.status is not PictureStatus.DESCRIBED
            or not picture.keywords
            or not picture.summary
        ):
            # The keywords screen waits for DESCRIBED before offering Begin;
            # reaching here otherwise is a client racing itself. A reading is
            # keywords *and* summary -- never half of one.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="The picture has not been read yet.",
            )
        key = picture_key(user.sub, str(payload.picture_id))
        description = PictureDescription(keywords=picture.keywords, summary=picture.summary)

    job_id = str(uuid.uuid4())
    try:
        started = start_generation(
            store,
            _get_sfn(),
            user_id=user.sub,
            job_id=job_id,
            duration_minutes=payload.duration_minutes,
            mood_text=payload.mood,
            picture_key=key,
            description=description,
            source="picture" if key else "words",
        )
    except GenerationStartError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not start generation. Please retry.",
        ) from None
    if not started:
        # A uuid4 collision is not a thing; this means a retry replayed.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="That job already exists.")

    return GenerateResponse(job_id=job_id, status=JobStatus.PENDING)


@router.get("/jobs/{job_id}", response_model=JobResponse)
def get_job(job_id: str, user: CurrentUserDep, store: StoreDep) -> JobResponse:
    # Scoped to the caller's partition, so another user's job is simply absent
    # rather than forbidden -- no existence oracle.
    job = store.get_job(user.sub, job_id)
    # A soft-deleted dreamscape is gone from the caller's point of view: no
    # status leak, and above all no fresh signed URL for audio that the DELETE
    # route has already cleaned up.
    if job is None or job.status is JobStatus.DELETED:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")

    audio_url = None
    picture_url = None
    if job.status is JobStatus.DONE and job.audio_key:
        audio_url = _signed_url(job.audio_key)
        if job.picture_key:
            picture_url = _signed_url(job.picture_key)

    return JobResponse(
        job_id=job.job_id,
        # ROLLED_BACK means "failed, and the credit was refunded". Clients only
        # need to know it failed.
        status=JobStatus.FAILED if job.status is JobStatus.ROLLED_BACK else job.status,
        audio_url=audio_url,
        picture_keywords=job.picture_keywords,
        picture_url=picture_url,
    )


def _raise_unless_open(gate: Gate) -> None:
    """The gate's outcomes as HTTP. The reasoning behind each outcome is
    documented on ``shared.jobs.generation_gate``; this only picks codes."""
    if gate.outcome is GateOutcome.NO_CREDIT:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="No generations remaining. Add credits to continue.",
        )
    if gate.outcome is GateOutcome.JOB_IN_FLIGHT:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="A generation is already in progress. Wait for it to finish.",
        )


def _signed_url(key: str) -> str:
    """A short-lived CloudFront signed URL for user content (constraint 6).

    Signing happens at the edge distribution, not on the bucket: the object is
    served from CloudFront and the bucket stays reachable only through OAC.
    ``jobs/*`` (narration) and ``pictures/*`` (the upload) are the signed
    behaviours; the shared BGM under ``assets/*`` is public and cached, so the
    player can switch tracks without a round trip here.
    """
    return cloudfront_signer.signed_url(key)
