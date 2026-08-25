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
from typing import Any
from uuid import UUID

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from api.deps import CurrentUserDep, StoreDep
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


def _require_credit(store: StoreDep, user_id: str) -> None:
    entitlement = store.get_entitlement(user_id)
    if entitlement is None or entitlement.available < 1:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="No generations remaining. Add credits to continue.",
        )


@router.post("/pictures/upload", response_model=UploadPictureResponse)
def create_upload(user: CurrentUserDep, store: StoreDep) -> UploadPictureResponse:
    _require_credit(store, user.sub)
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
    """Start the vision step for an upload that has landed. Idempotent: a
    picture already described (or still being described) is returned as is,
    and the execution is named after the picture so a double call cannot
    start a second one."""
    _require_credit(store, user.sub)
    picture = store.get_picture(user.sub, str(picture_id))
    if picture is None:
        # Another user's, or never authorised: absent either way.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")

    if picture.status is PictureStatus.PENDING:
        try:
            _get_sfn().start_execution(
                stateMachineArn=os.environ["PICTURE_STATE_MACHINE_ARN"],
                name=str(picture_id),
                input=json.dumps({"user_id": user.sub, "picture_id": str(picture_id)}),
            )
        except ClientError as exc:
            # Same name already running: the earlier call won; nothing to do.
            if exc.response.get("Error", {}).get("Code") != "ExecutionAlreadyExists":
                logger.exception("failed to start picture description picture_id=%s", picture_id)
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Could not read the picture. Please retry.",
                ) from None
        logger.info("picture description started picture_id=%s", picture_id)

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
