"""Provider selection.

The only place that knows which vendors exist. ``synthesize`` asks for a
provider by configuration and never imports a vendor module itself.
"""

from __future__ import annotations

import os

from shared.tts.base import TTSProvider, UnknownProviderError

DEFAULT_PROVIDER = "polly"
PROVIDER_ENV_VAR = "TTS_PROVIDER"


def get_provider(name: str | None = None) -> TTSProvider:
    """Return the configured provider, defaulting to Polly.

    ``name`` falls back to the ``TTS_PROVIDER`` environment variable, which
    CDK sets on the synthesize Lambda.
    """
    provider_name = (name or os.environ.get(PROVIDER_ENV_VAR) or DEFAULT_PROVIDER).lower()

    if provider_name == "polly":
        # Imported lazily so a provider's SDK is only loaded when selected.
        from shared.tts.polly import PollyProvider

        return PollyProvider()

    # Milestone 4 registers "volcano" here.
    raise UnknownProviderError(
        f"unknown TTS provider {provider_name!r}; set {PROVIDER_ENV_VAR} to one of: polly"
    )
