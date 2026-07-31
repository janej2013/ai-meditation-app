"""Step 2: generate the meditation script with Bedrock (Claude Haiku).

Reads the mood from the JOB item rather than the state machine payload, so
user input never reaches the execution history (constraint 7). Writes the
script to S3 and passes only the key onward, for the same reason -- and logs
neither.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import boto3
from botocore.exceptions import ClientError

from shared.db import EntitlementStore
from shared.pipeline import BedrockTransientError, PipelineState, ScriptGenerationError

from .prompt import SYSTEM_PROMPT, build_user_message, min_script_chars, target_word_count

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Roughly 1.4 tokens per word, plus headroom for the model's own pacing.
_TOKENS_PER_WORD = 2

# Bedrock signals overload and throttling with these. Everything else --
# ValidationException, AccessDeniedException, a malformed response -- is
# permanent, so it must fall through to Catch instead of burning retries.
_TRANSIENT_CODES = frozenset(
    {
        "ThrottlingException",
        "ServiceUnavailableException",
        "InternalServerException",
        "ModelTimeoutException",
        "ModelNotReadyException",
    }
)

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


def script_key(job_id: str) -> str:
    return f"jobs/{job_id}/script.txt"


def lambda_handler(event: dict[str, Any], context: object) -> dict[str, Any]:  # noqa: ARG001
    state = PipelineState.model_validate(event)
    store = _get_store()

    job = store.get_job(state.user_id, state.job_id)
    if job is None or not job.mood_text:
        raise ScriptGenerationError(f"job {state.job_id} has no mood text to work from")

    store.mark_job_generating(state.user_id, state.job_id)

    script = _generate(job.mood_text, state.duration_minutes)

    key = script_key(state.job_id)
    _get_s3().put_object(
        Bucket=os.environ["AUDIO_BUCKET"],
        Key=key,
        Body=script.encode("utf-8"),
        ContentType="text/plain; charset=utf-8",
    )
    store.set_job_script_key(state.user_id, state.job_id, key)

    # Length only -- never the script itself (constraint 7).
    logger.info("script generated job_id=%s chars=%d", state.job_id, len(script))

    state.script_key = key
    return state.model_dump()


def _generate(mood_text: str, duration_minutes: int) -> str:
    words = target_word_count(duration_minutes)
    try:
        response = _get_bedrock().converse(
            modelId=os.environ["BEDROCK_MODEL_ID"],
            system=[{"text": SYSTEM_PROMPT}],
            messages=[
                {
                    "role": "user",
                    "content": [{"text": build_user_message(mood_text, duration_minutes)}],
                }
            ],
            inferenceConfig={
                "maxTokens": words * _TOKENS_PER_WORD,
                "temperature": 0.7,
            },
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in _TRANSIENT_CODES:
            raise BedrockTransientError(f"bedrock transient failure: {code}") from exc
        raise ScriptGenerationError(f"bedrock call failed: {code}") from exc

    return _extract_text(response, duration_minutes)


def _extract_text(response: dict[str, Any], duration_minutes: int) -> str:
    """Pull the script out of a Converse response, failing loudly if absent."""
    try:
        blocks = response["output"]["message"]["content"]
    except (KeyError, TypeError) as exc:
        raise ScriptGenerationError("bedrock response had no output message") from exc

    text = "\n".join(block["text"] for block in blocks if "text" in block).strip()

    minimum = min_script_chars(duration_minutes)
    if len(text) < minimum:
        # A truncated or empty generation would otherwise sail through to TTS
        # and produce a few seconds of audio the user paid a credit for. The
        # floor scales with the request: a flat one sized for 3 minutes would
        # wave through a 30-minute script truncated to a twentieth.
        raise ScriptGenerationError(
            f"bedrock returned {len(text)} chars, below the {minimum} minimum "
            f"for {duration_minutes} minutes"
        )
    return text
