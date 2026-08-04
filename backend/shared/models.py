"""Pydantic models and key helpers for the DynamoDB single-table design.

Key schema (see CLAUDE.md):

    PK = USER#<cognito_sub>
    SK = PROFILE
    SK = ENTITLEMENT          available, frozen, plan, period_end
    SK = SUB#<stripe_subscription_id>
    SK = JOB#<job_id>         status, audio_key, timestamps

These models are the contract between Step Functions tasks: every task Lambda
validates its payload on entry.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

ENTITLEMENT_SK = "ENTITLEMENT"
PROFILE_SK = "PROFILE"

# Freemium rule: one free generation granted at signup.
FREE_SIGNUP_CREDITS = 1
DEFAULT_PLAN = "free"


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


class JobStatus(StrEnum):
    """Lifecycle of a single meditation generation job."""

    PENDING = "PENDING"
    FROZEN = "FROZEN"
    GENERATING = "GENERATING"
    DONE = "DONE"
    FAILED = "FAILED"
    ROLLED_BACK = "ROLLED_BACK"


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
    script_key: str | None = None
    audio_key: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


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
