"""Picture upload routes.

The browser uploads straight to S3 with a presigned POST; the API only signs
the policy (constraint 6 in spirit: no user bytes through Lambda). A POST
policy, unlike a presigned PUT, lets the server *enforce* the object's size,
content type and exact key.
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any

import boto3
from botocore.config import Config
from fastapi import APIRouter
from pydantic import BaseModel

from api.deps import CurrentUserDep
from shared.models import picture_key

logger = logging.getLogger(__name__)

router = APIRouter(tags=["pictures"])

# Long enough to survive a slow mobile upload, short enough that a leaked form
# is useless by the time anyone could reuse it.
UPLOAD_EXPIRES_SECONDS = 300

# Mirrors describe_picture.MAX_PICTURE_BYTES; the browser downsizes to well
# under this before uploading.
MAX_PICTURE_BYTES = 4_000_000
PICTURE_CONTENT_TYPE = "image/jpeg"

_s3: Any = None


def _get_s3() -> Any:
    global _s3
    if _s3 is None:
        # SigV4 is what a regional bucket accepts for POST policies.
        _s3 = boto3.client("s3", config=Config(signature_version="s3v4"))
    return _s3


class UploadPictureResponse(BaseModel):
    """A one-shot, pre-authorised S3 POST for one JPEG."""

    picture_id: str
    url: str
    fields: dict[str, str]
    expires_in: int


@router.post("/pictures/upload", response_model=UploadPictureResponse)
def create_upload(user: CurrentUserDep) -> UploadPictureResponse:
    picture_id = str(uuid.uuid4())
    key = picture_key(user.sub, picture_id)

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
