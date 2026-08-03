"""Vendor-neutral text-to-speech.

Business logic depends only on ``TTSProvider`` and ``VoiceConfig`` (CLAUDE.md:
never call a TTS vendor SDK from business logic). Volcano Engine is the primary
provider and Polly the fallback; selection is `TTS_PROVIDER` alone, so swapping
them touches no caller.
"""

from shared.tts.base import (
    MAX_POLLY_CHARS,
    TTSError,
    TTSProvider,
    TTSTransientError,
    UnknownProviderError,
    VoiceConfig,
    chunk_script,
)
from shared.tts.factory import DEFAULT_PROVIDER, KNOWN_PROVIDERS, get_provider

__all__ = [
    "DEFAULT_PROVIDER",
    "KNOWN_PROVIDERS",
    "MAX_POLLY_CHARS",
    "TTSError",
    "TTSProvider",
    "TTSTransientError",
    "UnknownProviderError",
    "VoiceConfig",
    "chunk_script",
    "get_provider",
]
