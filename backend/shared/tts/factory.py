"""Provider selection.

The only place that knows which vendors exist. ``synthesize`` asks for a
provider by configuration and never imports a vendor module itself.
"""

from __future__ import annotations

import os

from shared.tts.base import TTSProvider, UnknownProviderError

# Volcano Engine is the primary provider; Polly remains the fallback and is
# what a deployment gets by passing -c tts_provider=polly.
DEFAULT_PROVIDER = "volcano"
PROVIDER_ENV_VAR = "TTS_PROVIDER"

KNOWN_PROVIDERS = ("volcano", "polly")


def get_provider(name: str | None = None) -> TTSProvider:
    """Return the configured provider, defaulting to Volcano Engine.

    ``name`` falls back to the ``TTS_PROVIDER`` environment variable, which
    CDK sets on the synthesize Lambda.
    """
    provider_name = (name or os.environ.get(PROVIDER_ENV_VAR) or DEFAULT_PROVIDER).lower()

    # Imported lazily so only the selected vendor's module is loaded -- the
    # Volcano provider reaches for Secrets Manager on first use, which a
    # Polly-only deployment should never pay for.
    if provider_name == "volcano":
        from shared.tts.volcano import VolcanoProvider

        return VolcanoProvider()

    if provider_name == "polly":
        from shared.tts.polly import PollyProvider

        return PollyProvider()

    raise UnknownProviderError(
        f"unknown TTS provider {provider_name!r}; set {PROVIDER_ENV_VAR} to one of: "
        f"{', '.join(KNOWN_PROVIDERS)}"
    )
