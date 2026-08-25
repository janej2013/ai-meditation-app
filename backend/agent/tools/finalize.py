"""``finalize_meditation_brief``: the terminal tool, and the only one that
spends anything.

A conversation is free; this call is the moment a generation starts and a
credit is frozen (by the state machine, not here). So it runs the same gate
as ``POST /generate`` and reports a closed gate as an error result the
model can talk about -- "you have no generations left" is something to say
to the listener, not an exception to raise.

The job id is derived from the session (uuid5), so finalizing twice --
a retried turn, a double submit -- meets the same JOB row and the same
execution name, and ``start_generation`` recognises its own replay.
"""

from __future__ import annotations

import logging
import uuid

from pydantic import BaseModel, ConfigDict, Field

from agent.budget import FINALIZE_TOOL_NAME
from agent.contracts import Finalized
from agent.tools.registry import ToolContext, ToolOutcome, ToolSpec
from shared.jobs import GateOutcome, GenerationStartError, generation_gate
from shared.models import AGENT_JOB_NAMESPACE
from shared.pipeline import MAX_DURATION_MINUTES, MIN_DURATION_MINUTES

logger = logging.getLogger(__name__)

MIN_BRIEF_CHARS = 40
MAX_BRIEF_CHARS = 1200

NO_CREDIT_MESSAGE = (
    "The listener has no generations remaining; invite them to add credits before finalizing."
)
IN_FLIGHT_MESSAGE = "A generation is already running for this listener; ask them to wait for it."
START_FAILED_MESSAGE = "Could not start the generation; try finalizing again in a moment."
NOT_OURS_MESSAGE = "A job with this id belongs to something else; the session cannot finalize."


class BriefInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    brief: str = Field(min_length=MIN_BRIEF_CHARS, max_length=MAX_BRIEF_CHARS)
    duration_minutes: int = Field(ge=MIN_DURATION_MINUTES, le=MAX_DURATION_MINUTES)


def agent_job_id(session_id: str) -> str:
    """One job per session, always the same id."""
    return str(uuid.uuid5(AGENT_JOB_NAMESPACE, session_id))


async def finalize_meditation_brief(ctx: ToolContext, inp: BriefInput) -> ToolOutcome:
    if ctx.store is None or ctx.start_generation is None:
        return ToolOutcome.error("generation is unavailable right now")

    gate = generation_gate(ctx.store, ctx.user_id)
    if gate.outcome is GateOutcome.NO_CREDIT:
        return ToolOutcome.error(NO_CREDIT_MESSAGE)
    if gate.outcome is GateOutcome.JOB_IN_FLIGHT:
        return ToolOutcome.error(IN_FLIGHT_MESSAGE)

    job_id = agent_job_id(ctx.session_id)
    try:
        started = ctx.start_generation(
            user_id=ctx.user_id,
            job_id=job_id,
            duration_minutes=inp.duration_minutes,
            mood_text=inp.brief,
            source="agent",
            agent_session_id=ctx.session_id,
        )
    except GenerationStartError:
        return ToolOutcome.error(START_FAILED_MESSAGE)
    if not started:
        # start_generation already replays a PENDING job of this session; a
        # False here means the id is held by something that is not ours.
        # uuid5 makes that impossible, so this is a guard, not a path.
        logger.warning("finalize refused: job id taken job_id=%s", job_id)
        return ToolOutcome.error(NOT_OURS_MESSAGE)

    # The brief is user content: id and duration only (constraint 7).
    logger.info("finalized job_id=%s duration=%d", job_id, inp.duration_minutes)
    return ToolOutcome(content={"job_id": job_id}, finalized=Finalized(job_id=job_id))


SPEC = ToolSpec(
    name=FINALIZE_TOOL_NAME,
    description=(
        "Turn the conversation into a meditation. Call this once, only after the "
        "listener has confirmed what they want. The brief is the whole instruction "
        "for the script writer: the feeling to speak to, the imagery and pacing that "
        "suit them, anything to avoid -- written about the feeling, never repeating "
        "the listener's personal details. Choose a duration in minutes that fits what "
        "they said. This starts the generation and ends the session."
    ),
    input_model=BriefInput,
    handler=finalize_meditation_brief,
    terminal=True,
)
