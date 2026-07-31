"""Tests for the TTS abstraction.

Chunking is the load-bearing part: Polly caps a single SynthesizeSpeech call
at 3000 billed characters, so a 30-minute script (~2850 words) must be split.
Splitting on paragraph boundaries is not just a size trick -- the prompt puts a
paragraph break exactly where the listener should pause, so every seam in the
concatenated audio lands on an intended silence.
"""

from __future__ import annotations

import pytest
from botocore.exceptions import ClientError

from shared.pipeline import TTSTransientError
from shared.tts import TTSError, UnknownProviderError, VoiceConfig, chunk_script, get_provider
from shared.tts.base import MAX_POLLY_CHARS
from shared.tts.polly import PollyProvider


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
# Factory
# ----------------------------------------------------------------------


def test_factory_defaults_to_polly(monkeypatch):
    monkeypatch.delenv("TTS_PROVIDER", raising=False)

    assert get_provider().name == "polly"


def test_factory_reads_the_environment(monkeypatch):
    monkeypatch.setenv("TTS_PROVIDER", "polly")

    assert get_provider().name == "polly"


def test_factory_rejects_an_unknown_provider():
    with pytest.raises(UnknownProviderError):
        get_provider("volcano")  # arrives in milestone 4
