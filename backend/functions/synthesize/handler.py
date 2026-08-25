"""Step 3: synthesise the script to narration audio.

Goes through the TTSProvider abstraction -- no vendor SDK is imported here, so
milestone 4 swaps Polly for Volcano by changing TTS_PROVIDER alone.

This is the last step that produces audio: the browser mixes background music
under the narration at playback time, so the narration *is* the deliverable.
That is why this step records ``audio_key`` on the JOB item, a job the deployed
pipeline no longer gives to mix_audio. Like mix_audio, it deliberately does NOT
set status to DONE -- see ``EntitlementStore.set_job_audio_key``.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import boto3

from shared.audio import narration_key
from shared.db import EntitlementStore
from shared.pipeline import PipelineState
from shared.tts import get_provider

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_s3: Any = None
_store: EntitlementStore | None = None


def _get_s3() -> Any:
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3")
    return _s3


def _get_store() -> EntitlementStore:
    global _store
    if _store is None:
        _store = EntitlementStore()
    return _store


def lambda_handler(event: dict[str, Any], context: object) -> dict[str, Any]:  # noqa: ARG001
    state = PipelineState.model_validate(event)
    if not state.script_key:
        raise ValueError(f"job {state.job_id} reached synthesize with no script_key")

    bucket = os.environ["AUDIO_BUCKET"]
    s3 = _get_s3()

    script = s3.get_object(Bucket=bucket, Key=state.script_key)["Body"].read().decode("utf-8")

    provider = get_provider()
    audio = provider.synthesize(script)

    key = narration_key(state.job_id)
    s3.put_object(Bucket=bucket, Key=key, Body=audio, ContentType="audio/mpeg")

    # Idempotent: a retry re-synthesises to the same key and rewrites the same
    # attribute, so a replay costs a TTS call but cannot corrupt the job.
    _get_store().set_job_audio_key(state.user_id, state.job_id, key)

    logger.info(
        "narration written job_id=%s provider=%s bytes=%d",
        state.job_id,
        provider.name,
        len(audio),
    )

    # Both names point at the same object today. narration_key describes what
    # the file is; audio_key is what the client plays, and a future server-side
    # mix step would overwrite it without touching narration_key.
    state.narration_key = key
    state.audio_key = key
    return state.model_dump()
