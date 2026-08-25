"""Picture routes: authorise an upload, describe it, read the description.

The browser uploads straight to S3 with a presigned POST; the API only signs
the policy (constraint 6 in spirit: no user bytes through Lambda). A POST
policy, unlike a presigned PUT, lets the server *enforce* the object's size,
content type and exact key.

A picture is described **before** any job exists -- the keywords screen shows
what was found and the user decides whether to Begin -- so the vision call is
uncompensated Bedrock spend. Two gates keep it honest: every route here needs
a credit in hand (the same 402 as POST /generate), and each picture is
described at most once (the execution is named after it). The call itself
never happens in this Lambda (constraint 2): the API starts the picture state
machine and the client polls the PICTURE item's status.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from api.deps import CurrentUserDep, StoreDep, require_credit
from shared.models import MAX_PICTURE_BYTES, PICTURE_CONTENT_TYPE, PictureStatus, picture_key

logger = logging.getLogger(__name__)

router = APIRouter(tags=["pictures"])

# Long enough to survive a slow mobile upload, short enough that a leaked form
# is useless by the time anyone could reuse it.
UPLOAD_EXPIRES_SECONDS = 300

_s3: Any = None
_sfn: Any = None


def _get_s3() -> Any:
    global _s3
    if _s3 is None:
        # SigV4 is what a regional bucket accepts for POST policies.
        _s3 = boto3.client("s3", config=Config(signature_version="s3v4"))
    return _s3


def _get_sfn() -> Any:
    global _sfn
    if _sfn is None:
        _sfn = boto3.client("stepfunctions")
    return _sfn


class UploadPictureResponse(BaseModel):
    """A one-shot, pre-authorised S3 POST for one JPEG."""

    picture_id: str
    url: str
    fields: dict[str, str]
    expires_in: int


class PictureResponse(BaseModel):
    picture_id: str
    status: PictureStatus
    # Present once DESCRIBED. The summary is prompt material only.
    keywords: list[str] | None = None


@router.post("/pictures/upload", response_model=UploadPictureResponse)
def create_upload(user: CurrentUserDep, store: StoreDep) -> UploadPictureResponse:
    require_credit(store, user.sub)
    picture_id = str(uuid.uuid4())
    key = picture_key(user.sub, picture_id)

    # The item is what later authorises describing this picture: the vision
    # step refuses ids that never passed through here.
    store.create_picture(user.sub, picture_id)

    presigned = _get_s3().generate_presigned_post(
        Bucket=os.environ["AUDIO_BUCKET"],
        Key=key,
        Fields={"Content-Type": PICTURE_CONTENT_TYPE},
        Conditions=[
            {"Content-Type": PICTURE_CONTENT_TYPE},
            ["content-length-range", 1, MAX_PICTURE_BYTES],
        ],
        ExpiresIn=UPLOAD_EXPIRES_SECONDS,
    )

    # The id is random and the key is derived from the caller, so neither is
    # user content; the picture itself never passes through here.
    logger.info("picture upload authorised picture_id=%s", picture_id)
    return UploadPictureResponse(
        picture_id=picture_id,
        url=presigned["url"],
        fields=presigned["fields"],
        expires_in=UPLOAD_EXPIRES_SECONDS,
    )


@router.post(
    "/pictures/{picture_id}/describe",
    response_model=PictureResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def describe(picture_id: UUID, user: CurrentUserDep, store: StoreDep) -> PictureResponse:
    """Start (or restart) the vision step for an upload that has landed.

    The PICTURE item owns idempotency, not the execution name: a conditional
    PENDING/FAILED -> DESCRIBING claim (or a DESCRIBING whose attempt is
    older than the machine's timeout, i.e. dead) decides whether this call
    starts an execution. A double tap loses the claim and just reads back
    the status; a picture described already is returned as is; an attempt
    that died without marking the item is retried rather than stranded.
    """
    require_credit(store, user.sub)
    picture = store.get_picture(user.sub, str(picture_id))
    if picture is None:
        # Another user's, or never authorised: absent either way.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")

    if picture.status is not PictureStatus.DESCRIBED:
        now = datetime.now(UTC)
        if store.mark_picture_describing(user.sub, str(picture_id), now=now):
            try:
                _get_sfn().start_execution(
                    stateMachineArn=os.environ["PICTURE_STATE_MACHINE_ARN"],
                    # Unique per attempt; the item, not the name, dedupes.
                    name=f"{picture_id}-{int(now.timestamp())}",
                    input=json.dumps({"user_id": user.sub, "picture_id": str(picture_id)}),
                )
            except ClientError:
                # Nothing is running for the claim we just took: release it
                # so the next call can try again instead of waiting out the
                # stale window.
                store.mark_picture_failed(user.sub, str(picture_id))
                logger.exception("failed to start picture description picture_id=%s", picture_id)
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Could not read the picture. Please retry.",
                ) from None
            logger.info("picture description started picture_id=%s", picture_id)
        picture = store.get_picture(user.sub, str(picture_id)) or picture

    return PictureResponse(
        picture_id=picture.picture_id, status=picture.status, keywords=picture.keywords
    )


@router.get("/pictures/{picture_id}", response_model=PictureResponse)
def get_picture(picture_id: UUID, user: CurrentUserDep, store: StoreDep) -> PictureResponse:
    picture = store.get_picture(user.sub, str(picture_id))
    if picture is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")
    return PictureResponse(
        picture_id=picture.picture_id, status=picture.status, keywords=picture.keywords
    )
