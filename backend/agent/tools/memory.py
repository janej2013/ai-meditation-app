"""``save_user_insight``: remember one preference across sessions.

The "it remembers you" half that outlives the transcript. Only for things
the listener actually said about themselves as a listener ("prefers slow
pacing", "hates ocean sounds") -- the prompt says so, and the length cap
keeps an insight a phrase rather than a paragraph. The user can read and
clear the whole list (docs/agent-runner-plan.md §9).
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, ConfigDict, field_validator

from agent.tools.registry import ToolContext, ToolOutcome, ToolSpec
from shared.db import MemoryContentionError

logger = logging.getLogger(__name__)

MIN_INSIGHT_CHARS = 3
MAX_INSIGHT_CHARS = 120


class InsightInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    insight: str

    @field_validator("insight")
    @classmethod
    def _trimmed(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if not MIN_INSIGHT_CHARS <= len(cleaned) <= MAX_INSIGHT_CHARS:
            raise ValueError(
                f"must be {MIN_INSIGHT_CHARS}-{MAX_INSIGHT_CHARS} characters after trimming"
            )
        return cleaned


async def save_user_insight(ctx: ToolContext, inp: InsightInput) -> ToolOutcome:
    if ctx.store is None:
        return ToolOutcome.error("memory is unavailable right now")
    try:
        saved = ctx.store.append_insight(ctx.user_id, inp.insight, ctx.session_id, ctx.now())
    except MemoryContentionError:
        return ToolOutcome.error("memory could not be saved right now; try again later")
    logger.info("insight saved=%s", saved)
    if not saved:
        # A duplicate is a fact, not a failure: the model must not retry it.
        return ToolOutcome(content={"saved": False, "reason": "already_remembered"})
    return ToolOutcome(content={"saved": True})


SPEC = ToolSpec(
    name="save_user_insight",
    description=(
        "Remember one lasting preference the listener has stated about their "
        "meditations, so future sessions can honour it without asking again -- for "
        "example a pacing they like, a sound they dislike, a time of day they usually "
        "listen. One short phrase per call. Only for preferences the listener has "
        "actually expressed, never for how they feel today, and never for personal "
        "details such as names, places or events."
    ),
    input_model=InsightInput,
    handler=save_user_insight,
)
