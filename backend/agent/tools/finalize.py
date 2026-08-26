"""``finalize_meditation_brief``: the model's proposal. Not the purchase.

A conversation is free; a generation costs a credit. The model decides
when it has understood the listener, but the listener decides when the
money moves: this tool only places the brief and duration on the session
as a pending proposal, and ``agent_runner.turns.confirm_session`` starts
the generation once the listener confirms in the app. Whatever a model
makes of "only after they agree", no turn can spend anything.

The gate still runs here, so a listener with no credit hears about it
before being offered a meditation they cannot start.
"""

from __future__ import annotations

import logging
import uuid

from pydantic import BaseModel, ConfigDict, Field

from agent.budget import FINALIZE_TOOL_NAME
from agent.contracts import Proposal
from agent.tools.registry import ToolContext, ToolOutcome, ToolSpec
from shared.jobs import GateOutcome, generation_gate
from shared.models import AGENT_JOB_NAMESPACE
from shared.pipeline import MAX_DURATION_MINUTES, MIN_DURATION_MINUTES

logger = logging.getLogger(__name__)

MIN_BRIEF_CHARS = 40
MAX_BRIEF_CHARS = 1200

NO_CREDIT_MESSAGE = (
    "The listener has no generations remaining; invite them to add credits before proposing."
)
IN_FLIGHT_MESSAGE = "A generation is already running for this listener; ask them to wait for it."
SESSION_CLOSED_MESSAGE = "This session is no longer open; nothing can be proposed."


class BriefInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    brief: str = Field(min_length=MIN_BRIEF_CHARS, max_length=MAX_BRIEF_CHARS)
    duration_minutes: int = Field(ge=MIN_DURATION_MINUTES, le=MAX_DURATION_MINUTES)


def agent_job_id(session_id: str) -> str:
    """One job per session, always the same id -- what makes confirming
    twice start one generation."""
    return str(uuid.uuid5(AGENT_JOB_NAMESPACE, session_id))


async def finalize_meditation_brief(ctx: ToolContext, inp: BriefInput) -> ToolOutcome:
    if ctx.store is None:
        return ToolOutcome.error("proposing is unavailable right now")

    gate = generation_gate(ctx.store, ctx.user_id)
    if gate.outcome is GateOutcome.NO_CREDIT:
        return ToolOutcome.error(NO_CREDIT_MESSAGE)
    if gate.outcome is GateOutcome.JOB_IN_FLIGHT:
        return ToolOutcome.error(IN_FLIGHT_MESSAGE)

    placed = ctx.store.set_pending_brief(
        ctx.user_id, ctx.session_id, brief=inp.brief, duration_minutes=inp.duration_minutes
    )
    if not placed:
        return ToolOutcome.error(SESSION_CLOSED_MESSAGE)

    # The brief is user content: duration only (constraint 7).
    logger.info("proposal placed duration=%d", inp.duration_minutes)
    return ToolOutcome(
        content={"status": "awaiting_confirmation", "duration_minutes": inp.duration_minutes},
        proposal=Proposal(duration_minutes=inp.duration_minutes),
    )


SPEC = ToolSpec(
    name=FINALIZE_TOOL_NAME,
    description=(
        "Propose the meditation once you understand what the listener needs. The brief "
        "is the whole instruction for the script writer: the feeling to speak to, the "
        "imagery and pacing that suit them, anything to avoid -- written about the "
        "feeling, never repeating the listener's personal details. Choose a duration in "
        "minutes that fits what they said. This does not start anything: the listener "
        "starts the meditation themselves in the app, so after proposing, tell them in "
        "one sentence what you prepared and that they can start it or ask for changes. "
        "Propose again if they want something different."
    ),
    input_model=BriefInput,
    handler=finalize_meditation_brief,
)
