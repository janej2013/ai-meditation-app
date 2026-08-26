"""Pydantic models and key helpers for the DynamoDB single-table design.

Key schema (see CLAUDE.md):

    PK = USER#<cognito_sub>
    SK = PROFILE
    SK = ENTITLEMENT          available, frozen, plan, period_end
    SK = SUB#<stripe_subscription_id>
    SK = JOB#<job_id>         status, audio_key, picture_*, timestamps
    SK = AGENT#<session_id>   companion session header: status, turn, engine, in_flight
    SK = AGENT#<sid>#T<nnnn>  one checkpoint per turn (user content)
    SK = MEMORY               cross-session insights (user content)
    SK = AGENTQUOTA#<yyyy-mm> monthly session counter

These models are the contract between Step Functions tasks: every task Lambda
validates its payload on entry.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ENTITLEMENT_SK = "ENTITLEMENT"
PROFILE_SK = "PROFILE"

# Freemium rule: one free generation granted at signup.
FREE_SIGNUP_CREDITS = 1
DEFAULT_PLAN = "free"


# Where an uploaded picture lives in the audio bucket: one prefix per user,
# so the presigned upload policy and the IAM grants can both be scoped by it.
PICTURE_PREFIX = "pictures"


def picture_key(user_id: str, picture_id: str) -> str:
    return f"{PICTURE_PREFIX}/{user_id}/{picture_id}.jpg"


def picture_sk(picture_id: str) -> str:
    """Sort key for an uploaded picture within a user partition.

    A picture is described before any job exists (the keywords screen comes
    before Begin), so its reading lives on its own item; POST /generate copies
    it onto the JOB it starts.
    """
    return f"PICTURE#{picture_id}"


# PICTURE items outlive nothing useful past the object itself.
PICTURE_ITEM_TTL_DAYS = 365

# How long a description attempt may be considered in flight. Mirrors the
# picture state machine's execution timeout (infra/stacks/pipeline_stack.py):
# an attempt older than this can only be dead, so a new one may start.
PICTURE_DESCRIBE_TIMEOUT_SECONDS = 600

# The vision call runs before any credit is frozen -- uncompensated spend --
# so a picture may be tried only this many times before the answer is
# "choose another".
PICTURE_DESCRIBE_MAX_ATTEMPTS = 3


# The upload contract, shared by its two enforcement points: the presigned
# POST policy (api/routers/pictures) and the vision step's re-check
# (functions/describe_picture). One value, so they cannot drift apart --
# a policy admitting more than the pipeline accepts would freeze a credit
# and then roll it back on every oversized upload.
MAX_PICTURE_BYTES = 4_000_000
PICTURE_CONTENT_TYPE = "image/jpeg"


def user_pk(user_id: str) -> str:
    """Partition key for a user, keyed on the Cognito subject."""
    return f"USER#{user_id}"


def job_sk(job_id: str) -> str:
    """Sort key for a generation job within a user partition."""
    return f"JOB#{job_id}"


def subscription_sk(stripe_subscription_id: str) -> str:
    """Sort key for a Stripe subscription within a user partition."""
    return f"SUB#{stripe_subscription_id}"


def event_sk(stripe_event_id: str) -> str:
    """Sort key for a processed Stripe event.

    Writing this item conditionally in the same transaction as the entitlement
    update is what makes webhook processing idempotent (constraint 5): Stripe
    retries deliveries, and a replayed event must not grant credits twice.
    """
    return f"EVENT#{stripe_event_id}"


# Where a job's words came from. Jobs written before this field existed
# read as None; the dreamscapes list infers picture/words from picture_key.
JobSource = Literal["words", "picture", "agent"]


class JobStatus(StrEnum):
    """Lifecycle of a single meditation generation job."""

    PENDING = "PENDING"
    FROZEN = "FROZEN"
    GENERATING = "GENERATING"
    DONE = "DONE"
    FAILED = "FAILED"
    ROLLED_BACK = "ROLLED_BACK"
    # Soft-deleted from the dreamscapes collection; the audio objects are
    # cleaned up by the DELETE route, the item stays as the idempotency anchor.
    DELETED = "DELETED"


class PictureStatus(StrEnum):
    """The vision step's progress on an upload: the keywords screen polls it."""

    PENDING = "PENDING"  # authorised, not yet read
    DESCRIBING = "DESCRIBING"  # an execution owns it (see describe_started_at)
    DESCRIBED = "DESCRIBED"
    FAILED = "FAILED"  # a permanent failure; a new attempt may be started


class Picture(BaseModel):
    """An uploaded picture and, once the vision step has run, its reading.

    Keywords and summary derive from the user's picture: on this item, never
    in an execution payload, never in INFO logs (constraint 7).
    """

    model_config = ConfigDict(extra="ignore")

    user_id: str
    picture_id: str
    status: PictureStatus
    keywords: list[str] | None = None
    summary: str | None = None
    created_at: datetime | None = None
    # The attempt token: set by each claim, echoed by the execution, and the
    # condition on every write the attempt makes -- so a late write from an
    # attempt that was reclaimed as dead cannot clobber its successor.
    describe_started_at: datetime | None = None
    describe_attempts: int = 0


class Entitlement(BaseModel):
    """A user's credit balance.

    ``available`` credits can be spent; ``frozen`` credits are reserved by an
    in-flight job and are either committed (consumed) or rolled back.
    """

    model_config = ConfigDict(extra="ignore")

    user_id: str
    available: int = Field(default=0, ge=0)
    frozen: int = Field(default=0, ge=0)
    plan: str = "free"
    period_end: datetime | None = None


class Job(BaseModel):
    """A generation job item.

    ``mood_text`` lives here rather than in the Step Functions payload: the
    execution history is visible in the console and retained for 90 days, and
    constraint 7 keeps user input out of it. ``generate_script`` reads the mood
    back from this item.
    """

    model_config = ConfigDict(extra="ignore")

    user_id: str
    job_id: str
    status: JobStatus
    mood_text: str | None = None
    duration_minutes: int | None = None
    # Set by the API when the user drifted from a picture; the object is kept
    # for the planned replay feature and only ever expires by S3 lifecycle.
    picture_key: str | None = None
    # Written by describe_picture. Derived from user content, so they follow
    # mood_text's rules: on the item, never in the execution history or logs.
    picture_keywords: list[str] | None = None
    picture_summary: str | None = None
    source: JobSource | None = None
    # Agent jobs: the companion session whose brief became mood_text. The
    # job id is derived from it (AGENT_JOB_NAMESPACE), which is what makes
    # finalizing idempotent.
    agent_session_id: str | None = None
    script_key: str | None = None
    audio_key: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class PictureDescription(BaseModel):
    """What the vision model saw, shaped for the script prompt.

    The bounds are the contract with the model: a description outside them is
    a failed generation, not something to trim into shape.
    """

    model_config = ConfigDict(extra="ignore")

    keywords: list[str] = Field(min_length=3, max_length=5)
    summary: str = Field(min_length=10, max_length=240)

    @field_validator("keywords")
    @classmethod
    def _keywords_are_short_phrases(cls, keywords: list[str]) -> list[str]:
        cleaned = [k.strip() for k in keywords]
        if any(not k or len(k) > 24 for k in cleaned):
            raise ValueError("each keyword must be 1-24 characters")
        return cleaned


class BillingOperationResult(BaseModel):
    """Outcome of a Stripe-driven entitlement change.

    ``applied`` is False when the Stripe event had already been processed --
    the dedupe marker was present, so nothing was mutated. That is a success,
    not an error: the webhook must return 200 so Stripe stops retrying.
    """

    model_config = ConfigDict(extra="ignore")

    applied: bool
    entitlement: Entitlement


class CreditOperationResult(BaseModel):
    """Outcome of a freeze/commit/rollback.

    ``applied`` is False when the call was an idempotent replay: the job had
    already moved past this transition, so nothing was mutated.
    """

    model_config = ConfigDict(extra="ignore")

    applied: bool
    job_status: JobStatus
    entitlement: Entitlement


# ----------------------------------------------------------------------
# Companion agent (docs/agent-runner-plan.md §2)
# ----------------------------------------------------------------------

# A transcript is the most sensitive thing this table holds; it is kept only
# as long as a session could plausibly be resumed.
AGENT_SESSION_TTL_DAYS = 30
# Two months, so a counter is still readable at the start of the next one.
AGENT_QUOTA_TTL_DAYS = 62
# A turn is one Lambda invocation (120 s); a claim older than this belongs
# to an invocation that can only have died, so a new one may take over.
AGENT_IN_FLIGHT_TIMEOUT_SECONDS = 180
AGENT_INSIGHTS_MAX = 20
AGENT_SESSIONS_PER_MONTH = 30

MEMORY_SK = "MEMORY"

# uuid5 namespace for agent job ids: job_id = uuid5(AGENT_JOB_NAMESPACE,
# session_id). Fixed forever -- changing it would let a retried finalize
# start a second job for the same session.
AGENT_JOB_NAMESPACE = uuid.UUID("3f6c1c8e-2b7d-4e0a-9c55-7a1e4d2b8f10")

AgentEngineName = Literal["native", "langgraph"]


def agent_session_sk(session_id: str) -> str:
    return f"AGENT#{session_id}"


def agent_turns_prefix(session_id: str) -> str:
    """The key prefix shared by a session's turn items and by nothing else:
    the header is ``AGENT#<sid>`` with no trailing ``#T``."""
    return f"AGENT#{session_id}#T"


def agent_turn_sk(session_id: str, turn: int) -> str:
    """Zero-padded so that sort-key order is turn order."""
    return f"{agent_turns_prefix(session_id)}{turn:04d}"


def agent_quota_sk(month: str) -> str:
    """``month`` is ``YYYY-MM``; one counter per calendar month."""
    return f"AGENTQUOTA#{month}"


class AgentSessionStatus(StrEnum):
    ACTIVE = "ACTIVE"
    FINALIZED = "FINALIZED"  # the terminal tool started a generation job
    ABANDONED = "ABANDONED"  # the user left; nothing was charged
    FAILED = "FAILED"


class AgentUsage(BaseModel):
    """Token counters, summed over a session on the header item."""

    model_config = ConfigDict(extra="ignore")

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


_USAGE_ATTRIBUTE_PREFIX = "usage_"


class AgentSession(BaseModel):
    """A companion session's header: where it is up to, and who holds it.

    ``turn`` counts committed turns and is the fencing token every write
    conditions on; ``in_flight`` marks the invocation currently running a
    turn. Token counters live as flat ``usage_*`` attributes on the item so
    that ``commit_turn`` can ``ADD`` to them, and are folded into ``usage``
    on read.
    """

    model_config = ConfigDict(extra="ignore", protected_namespaces=())

    user_id: str
    session_id: str
    status: AgentSessionStatus
    turn: int = Field(default=0, ge=0)
    engine: AgentEngineName
    model_id: str
    in_flight: datetime | None = None
    job_id: str | None = None
    # The brief the model proposed and the listener has not yet confirmed.
    # User content: on the item, never in a log (constraint 7).
    pending_brief: str | None = None
    pending_duration_minutes: int | None = None
    usage: AgentUsage = Field(default_factory=AgentUsage)
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @model_validator(mode="before")
    @classmethod
    def _fold_usage(cls, data: Any) -> Any:
        if not isinstance(data, dict) or "usage" in data:
            return data
        usage = {
            key.removeprefix(_USAGE_ATTRIBUTE_PREFIX): value
            for key, value in data.items()
            if key.startswith(_USAGE_ATTRIBUTE_PREFIX)
        }
        return {**data, "usage": usage} if usage else data


class AgentTurn(BaseModel):
    """One turn's checkpoint. Everything but the counters is user content:
    on this item, never in a log (constraint 7).

    Content blocks are stored in Converse wire form (``agent.contracts``
    spells the mapping) so that any engine can replay them unchanged.
    """

    model_config = ConfigDict(extra="ignore")

    session_id: str
    turn: int = Field(ge=0)
    user_text: str
    assistant_content: list[dict[str, Any]]
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    usage: AgentUsage = Field(default_factory=AgentUsage)
    stop_reason: str
    # Set when this turn's tool round closed the session; the rebuilt
    # history then ends on the tool results rather than an assistant reply.
    finalized_job_id: str | None = None
    created_at: datetime | None = None


class Insight(BaseModel):
    """One thing the agent was told to remember. User content."""

    model_config = ConfigDict(extra="ignore")

    text: str
    created_at: datetime
    session_id: str


class Memory(BaseModel):
    model_config = ConfigDict(extra="ignore")

    insights: list[Insight] = Field(default_factory=list)
    updated_at: datetime | None = None
