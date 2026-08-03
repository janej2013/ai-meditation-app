"""Volcano Engine (Doubao) Seed-TTS implementation of TTSProvider.

The primary provider; Polly stays as the fallback. See
``doubao-tts-integration.md`` at the repo root for the vendor contract.

Three details of that contract are easy to get wrong and are handled here:

* **``additions`` must be a JSON *string*, not an object.** The server ignores
  or rejects a nested object, so the emotion/context tuning would silently stop
  applying.
* **HTTP 200 does not mean success.** The response is a chunked stream of
  newline-delimited JSON; failures (bad credentials, unknown voice, resource
  not enabled) arrive as a non-zero ``code`` *inside* a 200 body.
* **``code`` 20000000 is the end-of-stream marker**, not an error.

Uses urllib3 rather than requests: botocore already vendors it, so the Lambda
layer gains no dependency.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from typing import Any

import boto3
import urllib3

from shared.tts.base import TTSError, TTSTransientError, VoiceConfig, chunk_script

logger = logging.getLogger(__name__)

ENDPOINT = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"
RESOURCE_ID = "seed-tts-2.0"

# Deliberately not MAX_POLLY_CHARS: the two vendors have unrelated limits, and
# sharing the constant would silently couple them. The vendor documents no hard
# cap for the streaming endpoint beyond "not too long" and points at the async
# long-text API instead, so this is a conservative value -- roughly two minutes
# of slow speech per request.
MAX_VOLCANO_CHARS = 1000

# Sentinel line that closes a successful stream.
END_OF_STREAM_CODE = 20000000

# Long scripts synthesise slowly; the vendor recommends 60s.
REQUEST_TIMEOUT_SECONDS = 60

# Enough of an error body to identify the failure, bounded so a vendor that
# answers with an HTML page cannot flood the logs.
MAX_ERROR_BODY_CHARS = 400

# Voice-cloned ("声音复刻") ids start with S_ and live in a different cluster.
_CLONED_VOICE_PREFIX = "S_"
_CLUSTER_CLONED = "volcano_icl"
_CLUSTER_PRESET = "volcano_tts"

# Section 6 of the integration doc: the English preset. The product serves
# Australian users, so the English voice and the ASMR emotion are the defaults.
DEFAULT_VOICE = VoiceConfig(
    voice_id="zh_male_m191_uranus_bigtts",
    language_code="en-AU",
    sample_rate_hz=24000,
)
DEFAULT_SPEECH_RATE = -25
DEFAULT_EMOTION = "ASMR"
DEFAULT_EMOTION_SCALE = 4

# English rendering of the meditation style prompt in section 6. Seed-TTS 2.0
# reads these as natural-language delivery direction, not as text to speak.
DEFAULT_CONTEXT_TEXTS = [
    "Speak in an extremely calm, restrained and ethereal tone. Keep the pitch "
    "low and broad, the pace very slow, and leave a distinct silence between "
    "every sentence. Carry no swings of emotion: sound like a natural voice "
    "echoing across remote mountains and open wilderness, so the delivery "
    "feels upholding and compassionate."
]

# Any business identifier; the vendor only requires the field to be present.
_REQUEST_UID = "meditation-app"

_SECRET_ARN_ENV_VAR = "VOLCANO_SECRET_ARN"

# Cached for the life of the execution environment. Secrets Manager charges per
# API call and adds latency to every cold path, so the secret is read once.
_credentials: VolcanoCredentials | None = None


class VolcanoCredentials:
    """Access key and optional app id, as stored in Secrets Manager."""

    __slots__ = ("api_key", "app_id")

    def __init__(self, api_key: str, app_id: str | None = None) -> None:
        self.api_key = api_key
        self.app_id = app_id


def load_credentials(secret_arn: str | None = None, client: Any = None) -> VolcanoCredentials:
    """Read the credentials secret, caching for the container's lifetime.

    The secret holds a JSON document: ``api_key`` is required, ``app_id`` is
    optional (the vendor recommends but does not require the header).
    Constraint 4 forbids plaintext Lambda environment variables for this, so
    only the ARN is injected.
    """
    global _credentials
    if _credentials is not None:
        return _credentials

    arn = secret_arn or os.environ.get(_SECRET_ARN_ENV_VAR)
    if not arn:
        raise TTSError(f"{_SECRET_ARN_ENV_VAR} is not set; cannot authenticate to Volcano")

    secrets = client if client is not None else boto3.client("secretsmanager")
    try:
        payload = secrets.get_secret_value(SecretId=arn)["SecretString"]
    except Exception as exc:
        # Deliberately broad: every Secrets Manager failure mode (missing
        # secret, denied, throttled) is permanent from this Lambda's point of
        # view, and the message stays about the lookup so nothing from the
        # secret can reach a log line.
        raise TTSError("could not read the Volcano credentials secret") from exc

    try:
        document = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise TTSError("Volcano credentials secret is not valid JSON") from exc

    api_key = document.get("api_key")
    if not api_key:
        raise TTSError("Volcano credentials secret has no 'api_key' field")

    _credentials = VolcanoCredentials(api_key=api_key, app_id=document.get("app_id"))
    return _credentials


def reset_credentials_cache() -> None:
    """Drop the cached secret. For tests; production reads once per container."""
    global _credentials
    _credentials = None


def cluster_for(voice_id: str) -> str:
    """Voice-cloned ids route to a different cluster than preset ones."""
    return _CLUSTER_CLONED if voice_id.startswith(_CLONED_VOICE_PREFIX) else _CLUSTER_PRESET


def _body_excerpt(response: Any, limit: int = MAX_ERROR_BODY_CHARS) -> str:
    """A bounded single-line view of an error response, for the exception text.

    Only ever called on a non-200, whose body is the vendor's error document --
    never audio, and never anything derived from the user's mood.
    """
    try:
        text = response.data.decode("utf-8", errors="replace")
    except Exception:  # pragma: no cover - a body that cannot even be read
        # Must not mask the status code this is being called to describe.
        return "<unreadable body>"

    text = " ".join(text.split())
    if not text:
        return "<empty body>"
    return text[:limit] + ("..." if len(text) > limit else "")


class VolcanoProvider:
    """Synthesises via Volcano Engine's unidirectional streaming HTTP API."""

    name = "volcano"

    def __init__(
        self,
        http: urllib3.PoolManager | None = None,
        credentials: VolcanoCredentials | None = None,
    ) -> None:
        # Reusing the pool keeps the TCP connection warm across chunks; the
        # vendor holds keep-alive open for a minute.
        self.http = http if http is not None else urllib3.PoolManager()
        self._credentials = credentials

    def _get_credentials(self) -> VolcanoCredentials:
        if self._credentials is None:
            self._credentials = load_credentials()
        return self._credentials

    def synthesize(self, script: str, voice: VoiceConfig | None = None) -> bytes:
        voice = voice or DEFAULT_VOICE
        chunks = chunk_script(script, MAX_VOLCANO_CHARS)
        if not chunks:
            raise TTSError("cannot synthesize an empty script")

        logger.info("synthesizing %d chunk(s) with volcano voice=%s", len(chunks), voice.voice_id)
        segments = [self._synthesize_chunk(chunk, voice) for chunk in chunks]

        # Same reasoning as Polly: identical voice, format and sample rate, and
        # the seams land on paragraph breaks the prompt put there as pauses.
        return b"".join(segments)

    # ------------------------------------------------------------------

    def _build_payload(self, text: str, voice: VoiceConfig) -> dict[str, Any]:
        audio_params: dict[str, Any] = {
            "format": "mp3",
            "sample_rate": voice.sample_rate_hz,
            "speech_rate": DEFAULT_SPEECH_RATE,
            "emotion": DEFAULT_EMOTION,
            "emotion_scale": DEFAULT_EMOTION_SCALE,
        }
        additions = {
            "context_texts": DEFAULT_CONTEXT_TEXTS,
            "cache_config": {"text_type": 1, "use_cache": True},
        }
        return {
            "user": {"uid": _REQUEST_UID},
            "req_params": {
                "text": text,
                "speaker": voice.voice_id,
                "audio_params": audio_params,
                # A JSON string, not an object -- see the module docstring.
                "additions": json.dumps(additions),
            },
        }

    def _headers(self, voice: VoiceConfig) -> dict[str, str]:
        credentials = self._get_credentials()
        headers = {
            "Content-Type": "application/json",
            "X-Api-Access-Key": credentials.api_key,
            "X-Api-Resource-Id": RESOURCE_ID,
            "X-Api-Cluster": cluster_for(voice.voice_id),
        }
        if credentials.app_id:
            headers["X-Api-App-Id"] = credentials.app_id
        return headers

    def _synthesize_chunk(self, text: str, voice: VoiceConfig) -> bytes:
        body = json.dumps(self._build_payload(text, voice)).encode("utf-8")

        try:
            response = self.http.request(
                "POST",
                ENDPOINT,
                body=body,
                headers=self._headers(voice),
                timeout=urllib3.Timeout(total=REQUEST_TIMEOUT_SECONDS),
                # The state machine owns retry policy; retrying here would
                # multiply against its 3 attempts and hide the failure class.
                retries=False,
            )
        except urllib3.exceptions.HTTPError as exc:
            # Timeouts, connection resets and protocol errors are all worth
            # another attempt -- hand them to the state machine's Retry. The
            # class name alone does not distinguish a connect timeout from a
            # read timeout, and urllib3's message says which, along with the
            # host and the elapsed limit.
            raise TTSTransientError(f"volcano request failed: {type(exc).__name__}: {exc}") from exc

        return self._read_stream(response)

    def _read_stream(self, response: Any) -> bytes:
        status = response.status
        if status != 200:
            # The body says *why* -- wrong key, resource not enabled, bad
            # request shape. Without it a 4xx is a bare number, and 4xx is
            # exactly the class of failure that needs a human to read it.
            detail = _body_excerpt(response)
            if status == 429 or status >= 500:
                raise TTSTransientError(f"volcano returned HTTP {status}: {detail}")
            # 4xx other than 429: bad credentials, malformed request. Retrying
            # cannot help, so this must not become a TTSTransientError.
            raise TTSError(f"volcano returned HTTP {status}: {detail}")

        raw = response.data.decode("utf-8", errors="replace")
        encoded: list[str] = []
        unparsed = 0

        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                # The whole body is already buffered, so this is not the tail
                # of a half-received stream: it is a line the vendor sent that
                # does not parse. Counted rather than dropped, because an error
                # reported in a malformed line would otherwise surface only as
                # the far less specific "no audio data" below.
                unparsed += 1
                continue

            code = message.get("code", 0)
            if code == END_OF_STREAM_CODE:
                break
            if code:
                # The message names the failure (auth, unknown voice, resource
                # not enabled); it never contains the synthesised text.
                raise TTSError(
                    f"volcano synthesis failed: code {code} - {message.get('message', 'unknown')}"
                )
            if message.get("data"):
                encoded.append(message["data"])

        if unparsed:
            logger.warning("volcano sent %d unparseable line(s)", unparsed)

        if not encoded:
            # Naming the count separates "the vendor said nothing useful" from
            # "the vendor said something this parser could not read".
            raise TTSError(f"volcano returned no audio data ({unparsed} unparseable line(s))")

        try:
            return base64.b64decode("".join(encoded))
        except (ValueError, TypeError) as exc:
            raise TTSError("volcano returned audio that is not valid base64") from exc
