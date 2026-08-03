"""Amazon Polly implementation of TTSProvider.

Polly is the milestone 3 placeholder; Volcano Engine becomes the primary
provider in milestone 4 and Polly stays as the fallback.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import boto3
from botocore.exceptions import ClientError

from shared.tts.base import (
    MAX_POLLY_CHARS,
    TTSError,
    TTSTransientError,
    VoiceConfig,
    chunk_script,
)

if TYPE_CHECKING:  # boto3-stubs is a dev dependency, never installed in Lambda.
    from mypy_boto3_polly.client import PollyClient

logger = logging.getLogger(__name__)

# Neural engine, Australian English -- the product's primary market.
DEFAULT_VOICE = VoiceConfig(voice_id="Olivia", language_code="en-AU")

_NEURAL_ENGINE = "neural"
_OUTPUT_FORMAT = "mp3"

# Polly signals overload and throttling with these; everything else (an unknown
# voice, an unsupported engine) is permanent and must not be retried.
_TRANSIENT_CODES = frozenset(
    {
        "ThrottlingException",
        "ServiceUnavailableException",
        "InternalServiceErrorException",
        "ServiceFailureException",
    }
)


class PollyProvider:
    """Synthesises via Polly's neural engine, chunking to respect its limits."""

    name = "polly"

    def __init__(self, client: PollyClient | None = None) -> None:
        self.client: PollyClient = client if client is not None else boto3.client("polly")

    def synthesize(self, script: str, voice: VoiceConfig | None = None) -> bytes:
        voice = voice or DEFAULT_VOICE
        chunks = chunk_script(script, MAX_POLLY_CHARS)
        if not chunks:
            raise TTSError("cannot synthesize an empty script")

        logger.info("synthesizing %d chunk(s) with polly voice=%s", len(chunks), voice.voice_id)
        segments = [self._synthesize_chunk(chunk, voice) for chunk in chunks]

        # Concatenating MP3 frames is valid for segments produced by the same
        # voice, engine and sample rate, and the seams land on paragraph
        # boundaries where a pause belongs -- so the join is inaudible.
        return b"".join(segments)

    def _synthesize_chunk(self, chunk: str, voice: VoiceConfig) -> bytes:
        request: dict[str, Any] = {
            "Text": chunk,
            "OutputFormat": _OUTPUT_FORMAT,
            "VoiceId": voice.voice_id,
            "Engine": _NEURAL_ENGINE,
            "SampleRate": str(voice.sample_rate_hz),
        }
        try:
            response = self.client.synthesize_speech(**request)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in _TRANSIENT_CODES:
                raise TTSTransientError(f"polly transient failure: {code}") from exc
            raise TTSError(f"polly synthesis failed: {code}") from exc

        stream = response.get("AudioStream")
        if stream is None:
            raise TTSError("polly returned no audio stream")
        return stream.read()
