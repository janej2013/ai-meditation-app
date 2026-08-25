"""``get_session_history``: what this listener has meditated on before.

The "it remembers you" half that needs no memory item -- every finished job
is already on the table. Summaries only: a 60-character excerpt of the
words, the picture keywords, the duration. The model gets enough to refer
back ("last time you wanted something for sleep"), not the full text.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from agent.tools.registry import ToolContext, ToolOutcome, ToolSpec
from shared.models import Job

logger = logging.getLogger(__name__)

MOOD_EXCERPT_CHARS = 60
MAX_HISTORY_ITEMS = 10

# Stand-in for a missing created_at when ordering: such jobs sort oldest.
_EPOCH = datetime.min.replace(tzinfo=UTC)


class HistoryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=5, ge=1, le=MAX_HISTORY_ITEMS)


async def get_session_history(ctx: ToolContext, inp: HistoryInput) -> ToolOutcome:
    if ctx.store is None:
        return ToolOutcome.error("session history is unavailable right now")
    # Synchronous DynamoDB call inside a coroutine: acceptable for one
    # request per invocation; the harness may move it to a thread later.
    jobs = ctx.store.list_done_jobs(ctx.user_id)
    ordered = sorted(jobs, key=lambda j: (j.created_at or _EPOCH, j.job_id), reverse=True)
    sessions = [_summary(job) for job in ordered[: inp.limit]]
    # Counts only: the excerpts are user content (constraint 7).
    logger.info("history returned=%d total=%d", len(sessions), len(jobs))
    return ToolOutcome(content={"sessions": sessions, "total": len(jobs)})


def _summary(job: Job) -> dict[str, Any]:
    excerpt: str | None = None
    if job.mood_text:
        text = job.mood_text.strip()
        excerpt = text[:MOOD_EXCERPT_CHARS] + ("…" if len(text) > MOOD_EXCERPT_CHARS else "")
    return {
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "duration_minutes": job.duration_minutes,
        "source": job.source or ("picture" if job.picture_key else "words"),
        "keywords": job.picture_keywords,
        "excerpt": excerpt,
    }


SPEC = ToolSpec(
    name="get_session_history",
    description=(
        "Look up the listener's previous meditations: when they were made, how long "
        "they were, and a short glimpse of what they were about (a few words, or the "
        "keywords read from a picture). Call this once near the start of a session "
        "when you have nothing noted about the listener yet, so you can refer back to "
        "what has worked for them. Most recent first."
    ),
    input_model=HistoryInput,
    handler=get_session_history,
)
