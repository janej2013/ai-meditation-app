"""Tests for the TTS abstraction.

Chunking is the load-bearing part: Polly caps a single SynthesizeSpeech call
at 3000 billed characters, so a 30-minute script (~2850 words) must be split.
Splitting on paragraph boundaries is not just a size trick -- the prompt puts a
paragraph break exactly where the listener should pause, so every seam in the
concatenated audio lands on an intended silence.
"""

from __future__ import annotations

import base64
import json

import pytest
import urllib3
from botocore.exceptions import ClientError

from shared.pipeline import TransientError
from shared.tts import (
    TTSError,
    TTSTransientError,
    UnknownProviderError,
    VoiceConfig,
    chunk_script,
    get_provider,
)
from shared.tts.base import MAX_POLLY_CHARS
from shared.tts.polly import PollyProvider
from shared.tts.volcano import (
    DEFAULT_CONTEXT_TEXTS,
    END_OF_STREAM_CODE,
    MAX_ERROR_BODY_CHARS,
    MAX_VOLCANO_CHARS,
    VolcanoCredentials,
    VolcanoProvider,
    VolcanoTuning,
    cluster_for,
    load_credentials,
    reset_credentials_cache,
)


class FakePollyClient:
    """Records calls and returns a byte marker per chunk."""

    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self.error = error

    class _Stream:
        def __init__(self, payload: bytes) -> None:
            self.payload = payload

        def read(self) -> bytes:
            return self.payload

    def synthesize_speech(self, **kwargs):
        if self.error is not None:
            raise self.error
        self.calls.append(kwargs)
        return {"AudioStream": self._Stream(b"<audio>")}


def client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "SynthesizeSpeech")


# ----------------------------------------------------------------------
# Chunking
# ----------------------------------------------------------------------


def test_short_script_is_a_single_chunk():
    script = "Settle into your seat.\n\nBreathe out slowly."

    assert chunk_script(script) == [script]


def test_chunks_split_on_paragraph_boundaries():
    paragraphs = [f"paragraph number {i} " + "word " * 90 for i in range(10)]
    script = "\n\n".join(p.strip() for p in paragraphs)

    chunks = chunk_script(script, max_chars=1200)

    assert len(chunks) > 1
    assert all(len(chunk) <= 1200 for chunk in chunks)

    # Every seam lands on a paragraph boundary: re-splitting the chunks
    # reproduces the original paragraph sequence exactly, in order, with
    # nothing dropped and nothing cut in half.
    rejoined = [p for chunk in chunks for p in chunk.split("\n\n")]
    assert rejoined == [p.strip() for p in paragraphs]


def test_paragraphs_are_packed_not_emitted_one_per_chunk():
    script = "\n\n".join(["short paragraph"] * 20)

    chunks = chunk_script(script, max_chars=1000)

    # 20 * ~17 chars fits comfortably in one chunk.
    assert len(chunks) == 1


def test_oversized_paragraph_falls_back_to_sentences():
    sentence = "This is a sentence that goes on for a while. "
    script = sentence * 60  # one paragraph, ~2700 chars

    chunks = chunk_script(script, max_chars=500)

    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= 600  # allows one trailing sentence to overshoot slightly


def test_blank_script_yields_no_chunks():
    assert chunk_script("   \n\n  ") == []


def test_realistic_thirty_minute_script_respects_the_polly_cap():
    """~2850 words is what a 30-minute meditation comes to at 95 wpm."""
    paragraph = " ".join(["breathe"] * 60)
    script = "\n\n".join([paragraph] * 48)

    chunks = chunk_script(script)

    assert len(chunks) > 1
    assert all(len(c) <= MAX_POLLY_CHARS for c in chunks)


# ----------------------------------------------------------------------
# Polly provider
# ----------------------------------------------------------------------


def test_polly_synthesizes_each_chunk_and_concatenates():
    fake = FakePollyClient()
    provider = PollyProvider(client=fake)
    script = "\n\n".join(["word " * 100] * 10)

    audio = provider.synthesize(script, VoiceConfig(voice_id="Olivia"))

    assert len(fake.calls) == len(chunk_script(script))
    assert audio == b"<audio>" * len(fake.calls)


def test_polly_uses_the_neural_engine_and_configured_voice():
    fake = FakePollyClient()

    PollyProvider(client=fake).synthesize("Breathe in.\n\nBreathe out.")

    call = fake.calls[0]
    assert call["Engine"] == "neural"
    assert call["OutputFormat"] == "mp3"
    assert call["VoiceId"] == "Olivia"


def test_polly_empty_script_raises():
    with pytest.raises(TTSError):
        PollyProvider(client=FakePollyClient()).synthesize("  ")


@pytest.mark.parametrize(
    "code",
    ["ThrottlingException", "ServiceUnavailableException", "InternalServiceErrorException"],
)
def test_polly_transient_errors_are_retryable(code):
    """Only these become TTSTransientError, which the state machine retries."""
    provider = PollyProvider(client=FakePollyClient(error=client_error(code)))

    with pytest.raises(TTSTransientError):
        provider.synthesize("Breathe in.\n\nBreathe out.")


@pytest.mark.parametrize("code", ["InvalidSampleRateException", "AccessDeniedException"])
def test_polly_permanent_errors_are_not_retryable(code):
    """A bad request must fail straight to Catch, not burn three attempts."""
    provider = PollyProvider(client=FakePollyClient(error=client_error(code)))

    with pytest.raises(TTSError) as excinfo:
        provider.synthesize("Breathe in.\n\nBreathe out.")
    assert not isinstance(excinfo.value, TTSTransientError)


# ----------------------------------------------------------------------
# Volcano Engine provider
#
# The vendor's contract has three traps, each of which fails silently or
# misleadingly in production, so each gets a test: additions must be a JSON
# *string*, HTTP 200 can still carry a failure, and 20000000 is success rather
# than an error code.
# ----------------------------------------------------------------------


class FakeHTTPResponse:
    def __init__(self, status: int = 200, lines: list[dict] | None = None, body: str | None = None):
        self.status = status
        if body is not None:
            self.data = body.encode("utf-8")
        else:
            self.data = "\n".join(json.dumps(line) for line in (lines or [])).encode("utf-8")


class FakeHTTP:
    """Stands in for urllib3.PoolManager, recording each request."""

    def __init__(self, response=None, error: Exception | None = None) -> None:
        self.requests: list[dict] = []
        self.response = response
        self.error = error

    def request(self, method, url, *, body, headers, timeout, retries):
        if self.error is not None:
            raise self.error
        self.requests.append(
            {
                "method": method,
                "url": url,
                "body": json.loads(body.decode("utf-8")),
                "headers": headers,
                "timeout": timeout,
                "retries": retries,
            }
        )
        if callable(self.response):
            return self.response(len(self.requests))
        return self.response


def audio_line(payload: bytes) -> dict:
    return {"code": 0, "data": base64.b64encode(payload).decode("ascii")}


END_LINE = {"code": END_OF_STREAM_CODE, "message": "OK"}

CREDENTIALS = VolcanoCredentials(api_key="test-key", app_id="test-app")


def volcano(response=None, error=None, credentials=CREDENTIALS) -> VolcanoProvider:
    return VolcanoProvider(http=FakeHTTP(response=response, error=error), credentials=credentials)


def test_volcano_concatenates_data_chunks_in_order():
    """Multi-line streams are joined base64-first, then decoded once."""
    response = FakeHTTPResponse(lines=[audio_line(b"AAA"), audio_line(b"BBB"), END_LINE])
    provider = volcano(response)

    audio = provider.synthesize("Breathe in.\n\nBreathe out.")

    assert audio == b"AAABBB"


def test_volcano_stops_at_the_end_marker():
    """20000000 closes the stream; anything after it is not audio."""
    response = FakeHTTPResponse(lines=[audio_line(b"KEEP"), END_LINE, audio_line(b"AFTER-THE-END")])

    assert volcano(response).synthesize("Breathe.") == b"KEEP"


def test_volcano_joins_multiple_chunks_of_a_long_script():
    long_script = "\n\n".join(["word " * 150] * 12)  # ~9000 chars, several chunks
    http = FakeHTTP(response=FakeHTTPResponse(lines=[audio_line(b"X"), END_LINE]))
    provider = VolcanoProvider(http=http, credentials=CREDENTIALS)

    audio = provider.synthesize(long_script)

    expected_chunks = len(chunk_script(long_script, MAX_VOLCANO_CHARS))
    assert expected_chunks > 1
    assert len(http.requests) == expected_chunks
    assert audio == b"X" * expected_chunks


def test_volcano_additions_is_a_json_string_not_an_object():
    """The vendor ignores or rejects a nested object, silently dropping tuning."""
    http = FakeHTTP(response=FakeHTTPResponse(lines=[audio_line(b"A"), END_LINE]))
    VolcanoProvider(http=http, credentials=CREDENTIALS).synthesize("Breathe.")

    additions = http.requests[0]["body"]["req_params"]["additions"]
    assert isinstance(additions, str)

    parsed = json.loads(additions)
    assert parsed["cache_config"] == {"text_type": 1, "use_cache": True}
    assert parsed["context_texts"] == DEFAULT_CONTEXT_TEXTS


def test_volcano_sends_the_english_meditation_defaults():
    """No emotion keys by default -- the delivery direction is the context
    prompt's job. An exact match, so a new default cannot sneak in unasserted."""
    http = FakeHTTP(response=FakeHTTPResponse(lines=[audio_line(b"A"), END_LINE]))
    VolcanoProvider(http=http, credentials=CREDENTIALS).synthesize("Breathe.")

    params = http.requests[0]["body"]["req_params"]
    assert params["speaker"] == "ICL_uranus_en_male_zayne_tob"
    assert params["audio_params"] == {
        "format": "mp3",
        "sample_rate": 24000,
        "speech_rate": -25,
    }


def test_volcano_sends_both_emotion_keys_when_an_emotion_is_set():
    """The emotion pair is an opt-in override and travels together: a scale
    without an emotion is meaningless, and null is not "unset" to the API."""
    http = FakeHTTP(response=FakeHTTPResponse(lines=[audio_line(b"A"), END_LINE]))
    provider = VolcanoProvider(
        http=http, credentials=CREDENTIALS, tuning=VolcanoTuning(emotion="ASMR")
    )
    provider.synthesize("Breathe.")

    params = http.requests[0]["body"]["req_params"]["audio_params"]
    assert params["emotion"] == "ASMR"
    assert params["emotion_scale"] == 4


def test_volcano_sample_rate_follows_the_voice_config():
    http = FakeHTTP(response=FakeHTTPResponse(lines=[audio_line(b"A"), END_LINE]))
    provider = VolcanoProvider(http=http, credentials=CREDENTIALS)

    provider.synthesize("Breathe.", VoiceConfig(voice_id="some_voice", sample_rate_hz=16000))

    assert http.requests[0]["body"]["req_params"]["audio_params"]["sample_rate"] == 16000


def test_volcano_sends_the_auth_headers():
    http = FakeHTTP(response=FakeHTTPResponse(lines=[audio_line(b"A"), END_LINE]))
    VolcanoProvider(http=http, credentials=CREDENTIALS).synthesize("Breathe.")

    headers = http.requests[0]["headers"]
    assert headers["X-Api-Access-Key"] == "test-key"
    assert headers["X-Api-App-Id"] == "test-app"
    assert headers["X-Api-Resource-Id"] == "seed-tts-2.0"


def test_volcano_omits_the_app_id_header_when_unset():
    """Directly injected credentials may omit app_id (dry-run tooling); the
    header must then be absent rather than sent empty."""
    http = FakeHTTP(response=FakeHTTPResponse(lines=[audio_line(b"A"), END_LINE]))
    provider = VolcanoProvider(http=http, credentials=VolcanoCredentials(api_key="k"))

    provider.synthesize("Breathe.")

    assert "X-Api-App-Id" not in http.requests[0]["headers"]


@pytest.mark.parametrize(
    ("voice_id", "expected"),
    [
        ("S_cloned_voice_123", "volcano_icl"),
        ("ICL_uranus_en_male_zayne_tob", "volcano_tts"),
    ],
)
def test_volcano_cluster_follows_the_voice_id_prefix(voice_id, expected):
    """Cloned voices live in a different cluster; the wrong one 4xxs."""
    assert cluster_for(voice_id) == expected

    http = FakeHTTP(response=FakeHTTPResponse(lines=[audio_line(b"A"), END_LINE]))
    provider = VolcanoProvider(http=http, credentials=CREDENTIALS)
    provider.synthesize("Breathe.", VoiceConfig(voice_id=voice_id))

    assert http.requests[0]["headers"]["X-Api-Cluster"] == expected


def test_volcano_in_stream_error_code_is_permanent():
    """HTTP 200 with a non-zero code is a failure retrying cannot fix."""
    response = FakeHTTPResponse(lines=[{"code": 3000001, "message": "invalid speaker"}])

    with pytest.raises(TTSError) as excinfo:
        volcano(response).synthesize("Breathe.")

    assert not isinstance(excinfo.value, TTSTransientError)
    assert "3000001" in str(excinfo.value)


def test_volcano_error_message_never_leaks_the_script():
    """Constraint 7: no user text or generated script in an error or a log."""
    secret_line = "The listener mentioned Dana at Acme Corp."
    response = FakeHTTPResponse(lines=[{"code": 3000001, "message": "invalid speaker"}])

    with pytest.raises(TTSError) as excinfo:
        volcano(response).synthesize(secret_line)

    assert "Dana" not in str(excinfo.value)
    assert "Acme" not in str(excinfo.value)


def test_volcano_empty_stream_raises():
    with pytest.raises(TTSError):
        volcano(FakeHTTPResponse(lines=[END_LINE])).synthesize("Breathe.")


def test_volcano_empty_script_raises():
    with pytest.raises(TTSError):
        volcano(FakeHTTPResponse(lines=[END_LINE])).synthesize("   ")


def test_volcano_tolerates_a_partial_trailing_line():
    """A line that does not parse never costs audio that did.

    The body is buffered whole before parsing, so this is a line the vendor
    actually sent rather than a half-received stream -- but audio already
    collected is still returned.
    """
    body = json.dumps(audio_line(b"OK")) + "\n" + '{"code": 0, "da'

    assert volcano(FakeHTTPResponse(body=body)).synthesize("Breathe.") == b"OK"


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_volcano_throttling_and_5xx_are_retryable(status):
    with pytest.raises(TTSTransientError):
        volcano(FakeHTTPResponse(status=status, lines=[])).synthesize("Breathe.")


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_volcano_other_4xx_are_not_retryable(status):
    """Bad credentials or a malformed request will fail again identically."""
    with pytest.raises(TTSError) as excinfo:
        volcano(FakeHTTPResponse(status=status, lines=[])).synthesize("Breathe.")

    assert not isinstance(excinfo.value, TTSTransientError)


# ----------------------------------------------------------------------
# What a failure tells whoever has to read it
#
# A CloudWatch line and a Step Functions Cause are the whole diagnosis; the
# vendor's own words are the part worth keeping.
# ----------------------------------------------------------------------


def test_volcano_4xx_carries_the_vendor_explanation():
    """`HTTP 403` alone does not say whether the key or the resource is wrong."""
    body = '{"code": 4003, "message": "resource seed-tts-2.0 is not enabled"}'

    with pytest.raises(TTSError) as excinfo:
        volcano(FakeHTTPResponse(status=403, body=body)).synthesize("Breathe.")

    assert "403" in str(excinfo.value)
    assert "not enabled" in str(excinfo.value)


def test_volcano_5xx_carries_the_vendor_explanation():
    with pytest.raises(TTSTransientError) as excinfo:
        volcano(FakeHTTPResponse(status=503, body="upstream unavailable")).synthesize("Breathe.")

    assert "upstream unavailable" in str(excinfo.value)


def test_volcano_error_body_is_bounded():
    """A vendor that answers with an HTML page must not flood the logs."""
    with pytest.raises(TTSError) as excinfo:
        volcano(FakeHTTPResponse(status=400, body="x" * 5000)).synthesize("Breathe.")

    message = str(excinfo.value)
    assert message.endswith("...")
    assert len(message) < MAX_ERROR_BODY_CHARS * 2


def test_volcano_empty_error_body_says_so():
    """Distinguishes "the vendor said nothing" from a body that went missing."""
    with pytest.raises(TTSError) as excinfo:
        volcano(FakeHTTPResponse(status=401, body="")).synthesize("Breathe.")

    assert "<empty body>" in str(excinfo.value)


def test_volcano_transport_failure_carries_the_urllib3_detail():
    """Which timeout elapsed is the diagnosis, and the class name omits it."""
    error = urllib3.exceptions.ReadTimeoutError(None, "/tts", "read timed out after 60s")

    with pytest.raises(TTSTransientError) as excinfo:
        volcano(error=error).synthesize("Breathe.")

    assert "ReadTimeoutError" in str(excinfo.value)
    assert "read timed out" in str(excinfo.value)


def test_volcano_counts_unparseable_lines_when_no_audio_arrives():
    """An error sent in a line this parser cannot read must not vanish."""
    with pytest.raises(TTSError) as excinfo:
        volcano(FakeHTTPResponse(body="not json\nalso not json")).synthesize("Breathe.")

    assert "2 unparseable" in str(excinfo.value)


def test_tts_transient_error_is_catchable_as_a_tts_error():
    """`except TTSError` has to mean every TTS failure, transient included."""
    assert issubclass(TTSTransientError, TTSError)
    assert issubclass(TTSTransientError, TransientError)

    # The name is what Step Functions matches on, so it is part of the contract.
    assert TTSTransientError.__name__ == "TTSTransientError"

    with pytest.raises(TTSError):
        volcano(FakeHTTPResponse(status=503, body="busy")).synthesize("Breathe.")


@pytest.mark.parametrize(
    "error",
    [
        urllib3.exceptions.ConnectTimeoutError(None, "timed out"),
        urllib3.exceptions.ReadTimeoutError(None, "/", "timed out"),
        urllib3.exceptions.ProtocolError("connection reset"),
        urllib3.exceptions.NewConnectionError(None, "refused"),
    ],
)
def test_volcano_transport_failures_are_retryable(error):
    with pytest.raises(TTSTransientError):
        volcano(error=error).synthesize("Breathe.")


def test_volcano_leaves_retrying_to_the_state_machine():
    """urllib3-level retries would multiply against the state machine's 3."""
    http = FakeHTTP(response=FakeHTTPResponse(lines=[audio_line(b"A"), END_LINE]))
    VolcanoProvider(http=http, credentials=CREDENTIALS).synthesize("Breathe.")

    assert http.requests[0]["retries"] is False


# ----------------------------------------------------------------------
# Credentials
# ----------------------------------------------------------------------


class FakeSecretsClient:
    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.calls = 0
        self.secret_ids: list[str | None] = []

    def get_secret_value(self, **kwargs):
        """boto3 spells the argument SecretId; accept it without shadowing."""
        self.secret_ids.append(kwargs.get("SecretId"))
        self.calls += 1
        return {"SecretString": self.payload}


@pytest.fixture(autouse=True)
def _clear_volcano_credentials():
    reset_credentials_cache()
    yield
    reset_credentials_cache()


def test_credentials_are_read_from_secrets_manager():
    client = FakeSecretsClient(json.dumps({"api_key": "k", "app_id": "a"}))

    credentials = load_credentials(secret_arn="arn:secret", client=client)

    assert credentials.api_key == "k"
    assert credentials.app_id == "a"
    assert client.secret_ids == ["arn:secret"]


def test_credentials_fall_back_to_the_env_var_arn(monkeypatch):
    """CDK injects only the ARN; the provider resolves it at cold start."""
    monkeypatch.setenv("VOLCANO_SECRET_ARN", "arn:from-env")
    client = FakeSecretsClient(json.dumps({"api_key": "k", "app_id": "a"}))

    load_credentials(client=client)

    assert client.secret_ids == ["arn:from-env"]


def test_credentials_are_cached_across_calls():
    """Secrets Manager charges per call; a warm container must not re-read."""
    client = FakeSecretsClient(json.dumps({"api_key": "k", "app_id": "a"}))

    load_credentials(secret_arn="arn:secret", client=client)
    load_credentials(secret_arn="arn:secret", client=client)

    assert client.calls == 1


def test_credentials_without_an_app_id_raise():
    """seed-tts-2.0 rejects requests without the App Id header (400, code
    45000000), so a secret missing app_id must fail at load, not at synthesis."""
    client = FakeSecretsClient(json.dumps({"api_key": "k"}))

    with pytest.raises(TTSError, match="app_id"):
        load_credentials(secret_arn="arn:secret", client=client)


def test_credentials_without_an_api_key_raise():
    client = FakeSecretsClient(json.dumps({"app_id": "a"}))

    with pytest.raises(TTSError, match="api_key"):
        load_credentials(secret_arn="arn:secret", client=client)


def test_credentials_reject_a_non_json_secret():
    with pytest.raises(TTSError, match="valid JSON"):
        load_credentials(secret_arn="arn:secret", client=FakeSecretsClient("not-json"))


def test_credentials_without_the_arn_env_var_raise(monkeypatch):
    monkeypatch.delenv("VOLCANO_SECRET_ARN", raising=False)

    with pytest.raises(TTSError, match="VOLCANO_SECRET_ARN"):
        load_credentials()


# ----------------------------------------------------------------------
# Factory
# ----------------------------------------------------------------------


def test_factory_defaults_to_volcano(monkeypatch):
    """Volcano is primary from milestone 4 on."""
    monkeypatch.delenv("TTS_PROVIDER", raising=False)

    assert get_provider().name == "volcano"


def test_factory_returns_volcano_by_name():
    assert get_provider("volcano").name == "volcano"


def test_factory_falls_back_to_polly_on_request(monkeypatch):
    """Polly stays reachable without a code change."""
    monkeypatch.setenv("TTS_PROVIDER", "polly")

    assert get_provider().name == "polly"


def test_factory_rejects_an_unknown_provider():
    with pytest.raises(UnknownProviderError) as excinfo:
        get_provider("nonexistent")

    # The message names what is valid, so a typo is self-diagnosing.
    assert "volcano" in str(excinfo.value)
    assert "polly" in str(excinfo.value)
