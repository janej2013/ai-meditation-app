"""The Stripe-driven half of the credit ledger.

Constraint 5 requires webhook entitlement updates to be idempotent on the
Stripe event id, and Stripe genuinely does redeliver: a handler that is slow,
or that returns 5xx once, sees the same event again. Every test here asserts on
``available`` rather than the return value, because granting a pack twice is
the failure that actually costs money -- in the other direction from a double
deduction, but just as wrong.
"""

from __future__ import annotations

import pytest

from shared.db import EVENT_TTL_DAYS, CreditLedgerError
from shared.models import ENTITLEMENT_SK, JobStatus, event_sk, subscription_sk, user_pk

from .conftest import TABLE_NAME, USER_ID, seed_entitlement

EVENT = "evt_test_1"
SUBSCRIPTION = "sub_test_1"
PERIOD_END = "2026-09-03T00:00:00+00:00"


def counters(store) -> tuple[int, int]:
    entitlement = store.get_entitlement(USER_ID)
    assert entitlement is not None
    return entitlement.available, entitlement.frozen


def read(client, sk: str, user_id: str = USER_ID) -> dict | None:
    return client.get_item(
        TableName=TABLE_NAME,
        Key={"PK": {"S": user_pk(user_id)}, "SK": {"S": sk}},
    ).get("Item")


# ----------------------------------------------------------------------
# Credit packs
# ----------------------------------------------------------------------


def test_credit_pack_adds_to_available(store, dynamodb_client):
    seed_entitlement(dynamodb_client, available=1)

    result = store.apply_stripe_credit(USER_ID, EVENT, credits=10)

    assert result.applied is True
    assert counters(store) == (11, 0)


def test_credit_pack_is_idempotent_per_event(store, dynamodb_client):
    """The whole point of constraint 5: Stripe redelivers, we grant once."""
    seed_entitlement(dynamodb_client, available=1)

    first = store.apply_stripe_credit(USER_ID, EVENT, credits=10)
    second = store.apply_stripe_credit(USER_ID, EVENT, credits=10)

    assert first.applied is True
    assert second.applied is False
    assert counters(store) == (11, 0)


def test_a_different_event_grants_again(store, dynamodb_client):
    """Dedupe is per event, not per user -- a second purchase must land."""
    seed_entitlement(dynamodb_client, available=0)

    store.apply_stripe_credit(USER_ID, "evt_a", credits=10)
    store.apply_stripe_credit(USER_ID, "evt_b", credits=30)

    assert counters(store) == (40, 0)


def test_credit_pack_creates_a_missing_entitlement(store, dynamodb_client):
    """A paid-for top-up must land even if the signup trigger never ran.

    Dropping a purchase is worse than creating the row late.
    """
    assert store.get_entitlement(USER_ID) is None

    store.apply_stripe_credit(USER_ID, EVENT, credits=10)

    entitlement = store.get_entitlement(USER_ID)
    assert (entitlement.available, entitlement.frozen) == (10, 0)
    assert entitlement.plan == "free"


def test_credit_pack_leaves_frozen_alone(store, dynamodb_client):
    """A top-up during an in-flight job must not disturb the reservation."""
    seed_entitlement(dynamodb_client, available=0, frozen=1)

    store.apply_stripe_credit(USER_ID, EVENT, credits=5)

    assert counters(store) == (5, 1)


def test_credit_pack_rejects_a_non_positive_amount(store, dynamodb_client):
    seed_entitlement(dynamodb_client, available=1)

    with pytest.raises(ValueError):
        store.apply_stripe_credit(USER_ID, EVENT, credits=0)

    assert counters(store) == (1, 0)


# ----------------------------------------------------------------------
# The dedupe marker
# ----------------------------------------------------------------------


def test_the_event_marker_is_written(store, dynamodb_client):
    seed_entitlement(dynamodb_client, available=0)

    store.apply_stripe_credit(USER_ID, EVENT, credits=10)

    marker = read(dynamodb_client, event_sk(EVENT))
    assert marker is not None
    assert marker["event_id"]["S"] == EVENT
    assert marker["operation"]["S"] == "credit_pack"


def test_the_event_marker_carries_a_ttl(store, dynamodb_client):
    """Without expires_at the markers would accumulate forever.

    data_stack sets the table's TTL attribute to expires_at; only these items
    carry it, so nothing else is ever reaped.
    """
    import time

    seed_entitlement(dynamodb_client, available=0)

    store.apply_stripe_credit(USER_ID, EVENT, credits=10)

    expires_at = int(read(dynamodb_client, event_sk(EVENT))["expires_at"]["N"])
    horizon = time.time() + EVENT_TTL_DAYS * 86400
    assert horizon - 120 < expires_at <= horizon


def test_no_marker_is_left_behind_when_the_transaction_fails(store, dynamodb_client):
    """The marker and the entitlement move together or not at all."""
    seed_entitlement(dynamodb_client, available=1)

    with pytest.raises(ValueError):
        store.apply_stripe_credit(USER_ID, EVENT, credits=-5)

    assert read(dynamodb_client, event_sk(EVENT)) is None


# ----------------------------------------------------------------------
# Subscriptions
# ----------------------------------------------------------------------


def test_first_subscription_payment_sets_plan_and_grants_credits(store, dynamodb_client):
    seed_entitlement(dynamodb_client, available=0)

    result = store.apply_subscription_update(
        USER_ID,
        EVENT,
        plan="monthly",
        period_end=PERIOD_END,
        credits=20,
        subscription_id=SUBSCRIPTION,
    )

    assert result.applied is True
    entitlement = store.get_entitlement(USER_ID)
    assert entitlement.available == 20
    assert entitlement.plan == "monthly"
    assert entitlement.period_end is not None


def test_first_subscription_payment_records_the_sub_item(store, dynamodb_client):
    seed_entitlement(dynamodb_client, available=0)

    store.apply_subscription_update(
        USER_ID,
        EVENT,
        plan="monthly",
        period_end=PERIOD_END,
        credits=20,
        subscription_id=SUBSCRIPTION,
    )

    item = read(dynamodb_client, subscription_sk(SUBSCRIPTION))
    assert item is not None
    assert item["plan"]["S"] == "monthly"
    assert item["subscription_id"]["S"] == SUBSCRIPTION


def test_renewal_advances_the_period_and_grants_again(store, dynamodb_client):
    seed_entitlement(dynamodb_client, available=0)
    store.apply_subscription_update(
        USER_ID,
        "evt_first",
        plan="monthly",
        period_end=PERIOD_END,
        credits=20,
        subscription_id=SUBSCRIPTION,
    )

    later = "2026-10-03T00:00:00+00:00"
    store.apply_subscription_update(
        USER_ID,
        "evt_renewal",
        plan="monthly",
        period_end=later,
        credits=20,
        subscription_id=SUBSCRIPTION,
    )

    entitlement = store.get_entitlement(USER_ID)
    assert entitlement.available == 40
    assert entitlement.period_end.isoformat() == later


def test_renewal_is_idempotent_per_event(store, dynamodb_client):
    seed_entitlement(dynamodb_client, available=0)

    store.apply_subscription_update(
        USER_ID, EVENT, plan="monthly", period_end=PERIOD_END, credits=20
    )
    replay = store.apply_subscription_update(
        USER_ID, EVENT, plan="monthly", period_end=PERIOD_END, credits=20
    )

    assert replay.applied is False
    assert store.get_entitlement(USER_ID).available == 20


def test_cancellation_drops_to_free_without_taking_credits(store, dynamodb_client):
    """Credits already paid for stay spendable after the plan lapses."""
    seed_entitlement(dynamodb_client, available=0)
    store.apply_subscription_update(
        USER_ID, "evt_first", plan="monthly", period_end=PERIOD_END, credits=20
    )

    store.apply_subscription_update(USER_ID, "evt_cancel", plan="free", period_end=None, credits=0)

    entitlement = store.get_entitlement(USER_ID)
    assert entitlement.plan == "free"
    assert entitlement.available == 20
    assert entitlement.period_end is None


def test_cancellation_rejects_a_negative_grant(store, dynamodb_client):
    seed_entitlement(dynamodb_client, available=5)

    with pytest.raises(ValueError):
        store.apply_subscription_update(USER_ID, EVENT, plan="free", credits=-1)

    assert counters(store) == (5, 0)


def test_subscription_update_creates_a_missing_entitlement(store):
    assert store.get_entitlement(USER_ID) is None

    store.apply_subscription_update(
        USER_ID, EVENT, plan="monthly", period_end=PERIOD_END, credits=20
    )

    entitlement = store.get_entitlement(USER_ID)
    assert (entitlement.available, entitlement.frozen) == (20, 0)


# ----------------------------------------------------------------------
# Billing and the generation ledger must not interfere
# ----------------------------------------------------------------------


def test_a_top_up_does_not_disturb_an_in_flight_job(store, dynamodb_client):
    """A purchase mid-generation must leave the frozen credit reserved."""
    seed_entitlement(dynamodb_client, available=1)
    store.create_job(USER_ID, "job-1", "anxious", 10)
    store.freeze_credit(USER_ID, "job-1")
    assert counters(store) == (0, 1)

    store.apply_stripe_credit(USER_ID, EVENT, credits=10)

    assert counters(store) == (10, 1)
    # The job can still be committed, spending the originally frozen credit.
    store.commit_credit(USER_ID, "job-1")
    assert counters(store) == (10, 0)
    assert store.get_job(USER_ID, "job-1").status is JobStatus.DONE


def test_a_top_up_makes_a_blocked_freeze_succeed(store, dynamodb_client):
    seed_entitlement(dynamodb_client, available=0)
    store.apply_stripe_credit(USER_ID, EVENT, credits=1)

    result = store.freeze_credit(USER_ID, "job-2")

    assert result.applied is True
    assert counters(store) == (0, 1)


def test_an_event_marker_is_not_mistaken_for_a_job(store, dynamodb_client):
    """EVENT# and JOB# share a partition; the SK prefixes keep them apart."""
    seed_entitlement(dynamodb_client, available=1)

    store.apply_stripe_credit(USER_ID, EVENT, credits=5)

    assert store.get_job(USER_ID, EVENT) is None
    assert read(dynamodb_client, event_sk(EVENT)) is not None
    assert read(dynamodb_client, ENTITLEMENT_SK) is not None


def test_a_non_condition_cancellation_surfaces_as_a_ledger_error(
    store, dynamodb_client, monkeypatch
):
    """A transaction cancelled for any other reason must not look like a replay."""
    seed_entitlement(dynamodb_client, available=1)

    def explode(**kwargs):
        raise store.client.exceptions.TransactionCanceledException(
            {
                "Error": {"Code": "TransactionCanceledException"},
                "CancellationReasons": [
                    {"Code": "ThrottlingError"},
                    {"Code": "None"},
                ],
            },
            "TransactWriteItems",
        )

    monkeypatch.setattr(store.client, "transact_write_items", explode)

    with pytest.raises(CreditLedgerError):
        store.apply_stripe_credit(USER_ID, EVENT, credits=10)
