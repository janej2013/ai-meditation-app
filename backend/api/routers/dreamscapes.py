"""The dreamscapes collection: list past meditations, soft-delete one.

Listing reads the whole user partition and paginates in the application --
see ``EntitlementStore.list_done_jobs`` for why that beats a GSI here. The
cursor is a value (last returned ``created_at|job_id``), so it stays correct
when items are deleted between pages, unlike an offset.

Playback URLs are deliberately absent: ``GET /jobs/{job_id}`` remains the one
place that signs them, freshly on every call.
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
from datetime import UTC, datetime
from typing import Any

import boto3
from botocore.exceptions import ClientError
from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel

from api.deps import CurrentUserDep, StoreDep
from shared.models import Job, JobStatus

logger = logging.getLogger(__name__)

router = APIRouter(tags=["dreamscapes"])

PAGE_SIZE = 20

# Stand-in for a missing created_at in comparisons; tz-aware so it can be
# ordered against the stored timestamps (naive datetime.min cannot).
_EPOCH = datetime.min.replace(tzinfo=UTC)

# Enough for a card title, short enough that the response cannot become a
# transcript of the mood. User content: response body only, never logs
# (constraint 7).
MOOD_EXCERPT_CHARS = 40

_s3: Any = None


def _get_s3() -> Any:
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3")
    return _s3


class DreamscapeItem(BaseModel):
    job_id: str
    # Present on picture jobs; the card falls back to mood_excerpt.
    keywords: list[str] | None = None
    mood_excerpt: str | None = None
    duration_minutes: int | None = None
    source_type: str  # "picture" | "text"
    created_at: datetime | None = None


class DreamscapeList(BaseModel):
    items: list[DreamscapeItem]
    next_cursor: str | None = None


@router.get("/dreamscapes", response_model=DreamscapeList)
def list_dreamscapes(
    user: CurrentUserDep, store: StoreDep, cursor: str | None = None
) -> DreamscapeList:
    jobs = sorted(
        store.list_done_jobs(user.sub),
        key=lambda j: (j.created_at or _EPOCH, j.job_id),
        reverse=True,
    )
    if cursor is not None:
        jobs = _after_cursor(jobs, cursor)

    page = jobs[:PAGE_SIZE]
    next_cursor = _cursor_for(page[-1]) if len(jobs) > PAGE_SIZE else None

    logger.info("dreamscapes listed count=%d more=%s", len(page), next_cursor is not None)
    return DreamscapeList(items=[_item(job) for job in page], next_cursor=next_cursor)


@router.delete("/dreamscapes/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_dreamscape(job_id: str, user: CurrentUserDep, store: StoreDep) -> Response:
    """Soft-delete (status DELETED) then clean the job's audio objects.

    DynamoDB first: if S3 then fails, the user still sees the card gone and
    the play route already 404s -- the only residue is an invisible orphan
    object, healed on retry because a DELETED job passes the condition again
    and the S3 sweep re-runs. The other order would leave a visible card whose
    audio 404s. ``pictures/`` is never touched (constraint 9); IAM enforces
    that independently of this code.
    """
    job = store.get_job(user.sub, job_id)
    # Absent, another user's (absent from this partition), or still in flight:
    # all the same 404, matching GET /jobs -- no existence oracle, and an
    # in-flight job is not a dreamscape yet.
    if job is None or job.status not in (JobStatus.DONE, JobStatus.DELETED):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")

    if not store.mark_job_deleted(user.sub, job_id):
        # The status moved between our read and the update; treat like the read.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")

    try:
        _delete_job_objects(job_id)
    except ClientError as exc:
        # The item is already DELETED, so a retry heals this (see docstring).
        logger.error("dreamscape cleanup failed job_id=%s", job_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not remove the audio. Try again.",
        ) from exc

    logger.info("dreamscape deleted job_id=%s", job_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _delete_job_objects(job_id: str) -> None:
    """Remove everything under jobs/{job_id}/. Deleting nothing is success."""
    s3 = _get_s3()
    bucket = os.environ["AUDIO_BUCKET"]
    prefix = f"jobs/{job_id}/"
    kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
    while True:
        page = s3.list_objects_v2(**kwargs)
        keys = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
        if keys:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": keys, "Quiet": True})
        if not page.get("IsTruncated"):
            return
        kwargs["ContinuationToken"] = page["NextContinuationToken"]


def _item(job: Job) -> DreamscapeItem:
    excerpt = None
    if not job.picture_keywords and job.mood_text:
        text = job.mood_text.strip()
        excerpt = text[:MOOD_EXCERPT_CHARS] + ("…" if len(text) > MOOD_EXCERPT_CHARS else "")
    return DreamscapeItem(
        job_id=job.job_id,
        keywords=job.picture_keywords,
        mood_excerpt=excerpt,
        duration_minutes=job.duration_minutes,
        source_type="picture" if job.picture_key else "text",
        created_at=job.created_at,
    )


def _cursor_for(job: Job) -> str:
    created = job.created_at.isoformat() if job.created_at else ""
    return base64.urlsafe_b64encode(f"{created}|{job.job_id}".encode()).decode()


def _after_cursor(jobs: list[Job], cursor: str) -> list[Job]:
    """The strict suffix after the cursor item in the sorted order.

    A value cursor survives deletions: if the anchor item itself is gone, the
    comparison still lands on the right boundary instead of drifting the way
    an offset would.
    """
    try:
        created_raw, _, job_id = base64.urlsafe_b64decode(cursor.encode()).decode().partition("|")
        created = datetime.fromisoformat(created_raw) if created_raw else _EPOCH
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Bad cursor.") from None
    boundary = (created, job_id)
    return [j for j in jobs if ((j.created_at or _EPOCH), j.job_id) < boundary]
