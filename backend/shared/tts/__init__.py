"""Vendor-neutral text-to-speech.

Business logic depends only on ``TTSProvider`` and ``VoiceConfig`` (CLAUDE.md:
never call a TTS vendor SDK from business logic). Milestone 4 adds the Volcano
provider by registering it in the factory -- no caller changes.
"""

from shared.tts.base import (
    MAX_POLLY_CHARS,
    TTSError,
    TTSProvider,
    UnknownProviderError,
    VoiceConfig,
    chunk_script,
)
from shared.tts.factory import get_provider

__all__ = [
    "MAX_POLLY_CHARS",
    "TTSError",
    "TTSProvider",
    "UnknownProviderError",
    "VoiceConfig",
    "chunk_script",
    "get_provider",
]
