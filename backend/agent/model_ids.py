"""Which models the companion may use, and what dialect each one speaks.

A decision, not a mechanism (docs/agent-runner-plan.md §3.4), so it lives
once, here, and both engines import it: the native provider to shape its
request, the LangGraph engine to place its cache points and filter Nova's
thinking tags. The residency rule is the reason it is checked at all --
a cross-region profile could route a listener's words out of Australia.
"""

from __future__ import annotations

import os
from enum import StrEnum

# Known available on demand in ap-southeast-2 (the pipeline runs on it), so
# the CLI works before anyone has looked up a Claude profile id.
DEFAULT_AGENT_MODEL_ID = "amazon.nova-lite-v1:0"
MODEL_ID_ENV = "AGENT_MODEL_ID"

# Cross-region profiles that may route outside Australia. Refused outright
# rather than warned about: residency is a product promise.
FORBIDDEN_PROFILE_PREFIXES = ("us.", "eu.", "apac.", "global.", "jp.", "in.")


class ModelFamily(StrEnum):
    CLAUDE = "claude"
    NOVA = "nova"


def refuse_offshore(model_id: str) -> None:
    if model_id.startswith(FORBIDDEN_PROFILE_PREFIXES):
        raise ValueError(
            f"{model_id!r} is a cross-region profile that may leave Australia; "
            "use an au. profile or a bare in-region model id"
        )


def family_for(model_id: str) -> ModelFamily | None:
    """The family, or None for an id neither engine has a dialect for.
    Offshore profiles raise whatever the family."""
    refuse_offshore(model_id)
    if "anthropic." in model_id:
        return ModelFamily.CLAUDE
    if "amazon.nova" in model_id:
        return ModelFamily.NOVA
    return None


def model_family(model_id: str) -> ModelFamily:
    """Which request dialect a model id needs. The two families differ in
    where a cache breakpoint may sit."""
    family = family_for(model_id)
    if family is None:
        raise ValueError(f"unsupported model family for {model_id!r}")
    return family


def model_id_from_env() -> str:
    return os.environ.get(MODEL_ID_ENV) or DEFAULT_AGENT_MODEL_ID
