"""Optional step between FreezeCredit and GenerateScript: describe the picture
the user drifted from, with Amazon Nova Lite.

The picture's object key is read from the JOB item, never the payload, and the
description is written back there for generate_script -- both are user content
under constraint 7. The object itself is left in place: it backs the planned
replay feature and only expires by the bucket's lifecycle rule.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import boto3
from botocore.exceptions import ClientError
from pydantic import ValidationError

from shared.db import EntitlementStore
from shared.models import MAX_PICTURE_BYTES, PictureDescription
from shared.pipeline import PictureDescriptionError, PipelineState, raise_for_bedrock_error

from .prompt import SYSTEM_PROMPT, USER_MESSAGE

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_store: EntitlementStore | None = None
_bedrock: Any = None
_s3: Any = None


def _get_store() -> EntitlementStore:
    global _store
    if _store is None:
        _store = EntitlementStore()
    return _store


def _get_bedrock() -> Any:
    global _bedrock
    if _bedrock is None:
        _bedrock = boto3.client("bedrock-runtime")
    return _bedrock


def _get_s3() -> Any:
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3")
    return _s3


def lambda_handler(event: dict[str, Any], context: object) -> dict[str, Any]:  # noqa: ARG001
    state = PipelineState.model_validate(event)
    store = _get_store()

    job = store.get_job(state.user_id, state.job_id)
    if job is None or not job.picture_key:
        # The Choice state routed here on has_picture, yet the item carries no
        # key: an inconsistency worth failing on rather than silently skipping.
        raise PictureDescriptionError(f"job {state.job_id} has no picture to describe")

    image = _fetch(job.picture_key)
    description = _describe(image)
    store.set_job_picture_description(state.user_id, state.job_id, description)

    # Count only -- the keywords derive from the user's picture (constraint 7).
    logger.info("picture described job_id=%s keywords=%d", state.job_id, len(description.keywords))
    return state.model_dump()


def _fetch(key: str) -> bytes:
    """The picture bytes, or a permanent failure if absent or oversized."""
    s3 = _get_s3()
    bucket = os.environ["AUDIO_BUCKET"]
    try:
        # One read, capped: the presigned POST policy already limits the size
        # (shared MAX_PICTURE_BYTES), so the +1 only guards the Lambda's
        # memory against an object that somehow bypassed the policy.
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read(MAX_PICTURE_BYTES + 1)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in {"NoSuchKey", "404", "NotFound"}:
            raise PictureDescriptionError("picture was never uploaded") from exc
        raise PictureDescriptionError(f"could not read picture: {code}") from exc
    if len(body) > MAX_PICTURE_BYTES:
        raise PictureDescriptionError(f"picture exceeds {MAX_PICTURE_BYTES} bytes")
    return body


def _describe(image: bytes) -> PictureDescription:
    try:
        response = _get_bedrock().converse(
            modelId=os.environ["BEDROCK_MODEL_ID"],
            system=[{"text": SYSTEM_PROMPT}],
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"image": {"format": "jpeg", "source": {"bytes": image}}},
                        {"text": USER_MESSAGE},
                    ],
                }
            ],
            inferenceConfig={"maxTokens": 300, "temperature": 0.3},
        )
    except ClientError as exc:
        raise_for_bedrock_error(exc, PictureDescriptionError)

    return _parse(response)


def _parse(response: dict[str, Any]) -> PictureDescription:
    """The model's JSON, validated -- a description outside the contract is a
    failed step, never silently downgraded to a picture-less meditation."""
    try:
        blocks = response["output"]["message"]["content"]
    except (KeyError, TypeError) as exc:
        raise PictureDescriptionError("bedrock response had no output message") from exc
    text = "".join(block.get("text", "") for block in blocks).strip()

    try:
        data = json.loads(_json_object(text))
        return PictureDescription.model_validate(data)
    except (ValueError, ValidationError) as exc:
        # The raw text is the model's reading of the user's picture, so the
        # error names the shape problem only.
        raise PictureDescriptionError(
            f"picture description did not fit the contract: {type(exc).__name__}"
        ) from exc


def _json_object(text: str) -> str:
    """The outermost {...} in ``text`` -- models sometimes wrap JSON in prose
    or a code fence despite being told not to."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no JSON object in response")
    return text[start : end + 1]
