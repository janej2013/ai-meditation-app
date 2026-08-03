"""The TTS interface every provider implements.

Nothing vendor-specific crosses this boundary: ``synthesize`` takes plain text
plus a neutral ``VoiceConfig`` and returns MP3 bytes. ``voice_id`` is an opaque
string each provider interprets for itself, so adding Volcano in milestone 4
touches only the provider module and the factory.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from shared.pipeline import TransientError

# Polly's SynthesizeSpeech accepts 6000 characters, of which 3000 may be
# billed (spoken) characters. Chunk well under that so a long paragraph plus
# the request envelope can never trip the limit.
MAX_POLLY_CHARS = 2500

_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")


class TTSError(Exception):
    """Base class for TTS failures."""


class UnknownProviderError(TTSError):
    """TTS_PROVIDER names a provider that is not registered."""


class TTSTransientError(TTSError, TransientError):
    """A TTS failure worth retrying: throttling, timeouts, vendor 5xx.

    Both bases are load-bearing, which is why this lives here rather than
    alongside the other pipeline errors:

    * ``TransientError`` is the taxonomy the state machine's Retry blocks are
      built from. Step Functions matches the class *name*, so the name is the
      part that must not change.
    * ``TTSError`` is what makes ``except TTSError`` mean "any TTS failure".
      While this class sat outside that hierarchy, the one failure class a
      caller most wants to handle was the one such a clause silently missed.
    """


@dataclass(frozen=True)
class VoiceConfig:
    """How the script should sound, in vendor-neutral terms.

    ``voice_id`` is opaque: Polly reads it as a Polly voice name, Volcano will
    read it as a Volcano voice type. Callers get it from configuration, never
    by hard-coding a vendor's catalogue into business logic.
    """

    voice_id: str
    language_code: str = "en-AU"
    sample_rate_hz: int = 24000


@runtime_checkable
class TTSProvider(Protocol):
    """Synthesises a script to MP3 bytes."""

    name: str

    def synthesize(self, script: str, voice: VoiceConfig) -> bytes:
        """Return MP3 audio for ``script``.

        Implementations raise ``TTSTransientError`` for throttling and upstream
        5xx, and ``TTSError`` for anything permanent. Either way the message
        should carry whatever the vendor said about the failure: that text is
        all a CloudWatch reader gets.
        """
        ...


def chunk_script(script: str, max_chars: int = MAX_POLLY_CHARS) -> list[str]:
    """Split a script into synthesis-sized chunks on paragraph boundaries.

    Paragraphs are packed greedily so chunks stay near the limit without
    splitting one mid-sentence. This matters beyond the character cap: the
    prompt puts a paragraph break exactly where the listener should rest, so
    joining the resulting audio segments lands every seam on an intended
    pause rather than in the middle of a phrase.

    A single paragraph longer than ``max_chars`` falls back to sentence
    boundaries, and a sentence longer still is emitted whole -- better an
    oversized chunk the provider may reject loudly than silently truncated
    speech.
    """
    paragraphs = [p.strip() for p in script.split("\n\n") if p.strip()]
    if not paragraphs:
        return []

    chunks: list[str] = []
    current = ""

    for paragraph in paragraphs:
        pieces = (
            [paragraph] if len(paragraph) <= max_chars else _split_sentences(paragraph, max_chars)
        )
        for piece in pieces:
            candidate = f"{current}\n\n{piece}" if current else piece
            if current and len(candidate) > max_chars:
                chunks.append(current)
                current = piece
            else:
                current = candidate

    if current:
        chunks.append(current)
    return chunks


def _split_sentences(paragraph: str, max_chars: int) -> list[str]:
    """Break an oversized paragraph on sentence boundaries."""
    sentences = _SENTENCE_BOUNDARY.split(paragraph)
    pieces: list[str] = []
    current = ""

    for sentence in sentences:
        candidate = f"{current} {sentence}" if current else sentence
        if current and len(candidate) > max_chars:
            pieces.append(current)
            current = sentence
        else:
            current = candidate

    if current:
        pieces.append(current)
    return pieces
