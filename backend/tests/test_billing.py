"""Stripe Checkout and webhook routes.

The webhook is the only unauthenticated route in the API, so its signature
check *is* its authentication. These tests compute real HMAC signatures with
the webhook secret and let ``stripe.Webhook.construct_event`` verify them --
mocking the verification away would test nothing worth testing.

No test reaches Stripe: Checkout's SDK call is patched, and the webhook needs
no network at all.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from api import products, stripe_client
from api.deps import CurrentUser, get_current_user, get_store
from api.main import app

from .conftest import USER_ID, seed_entitlement

WEBHOOK_SECRET = "whsec_test_secret"
SECRET_KEY = "sk_test_key"
SUBSCRIPTION = "sub_test_1"
PERIOD_END_EPOCH = 1788480000  # 2026-09-03T00:00:00Z


class FakeSecretsClient:
    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.secret_ids: list[str | None] = []

    def get_secret_value(self, **kwargs):
        """boto3 spells the argument SecretId; the payload is fixed either way."""
        self.secret_ids.append(kwargs.get("SecretId"))
        return {"SecretString": self.payload}


@pytest.fixture(autouse=True)
def _reset_module_caches():
    stripe_client.reset_credentials_cache()
    products.reset_catalogue_cache()
    yield
    stripe_client.reset_credentials_cache()
    products.reset_catalogue_cache()


@pytest.fixture
def stripe_configured():
    """Prime the credentials cache so no test reaches Secrets Manager."""
    stripe_client.load_credentials(
        secret_arn="arn:secret",
        client=FakeSecretsClient(
            json.dumps({"secret_key": SECRET_KEY, "webhook_secret": WEBHOOK_SECRET})
        ),
    )


@pytest.fixture
def client(store, stripe_configured):
    """Authenticated as USER_ID, backed by the moto table."""
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        sub=USER_ID, email="user@example.com"
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


def sign(payload: bytes, secret: str = WEBHOOK_SECRET, timestamp: int | None = None) -> str:
    """Build a Stripe-Signature header the way Stripe's servers do."""
    ts = timestamp if timestamp is not None else int(time.time())
    signed_payload = b"%d.%s" % (ts, payload)
    digest = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={digest}"


def post_event(client: TestClient, event: dict, *, secret: str = WEBHOOK_SECRET, **kwargs):
    body = json.dumps(event).encode()
    return client.post(
        "/billing/webhook",
        content=body,
        headers={"Stripe-Signature": sign(body, secret, **kwargs)},
    )


def checkout_event(
    event_id: str = "evt_1",
    product_key: str = "pack_10",
    user_id: str = USER_ID,
    **session_extra,
) -> dict:
    return {
        "id": event_id,
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_test_1",
                "client_reference_id": user_id,
                "metadata": {"cognito_sub": user_id, "product_key": product_key},
                **session_extra,
            }
        },
    }


def invoice_event(
    event_id: str = "evt_inv_1",
    price_id: str = "price_placeholder_plan_monthly",
    user_id: str = USER_ID,
    billing_reason: str | None = "subscription_cycle",
) -> dict:
    """A renewal invoice by default; pass ``billing_reason`` for other cases.

    ``None`` omits the field entirely, which is how an API version that does
    not send it would look.
    """
    invoice: dict = {
        "id": "in_test_1",
        "subscription": SUBSCRIPTION,
        "metadata": {"cognito_sub": user_id},
        "lines": {
            "data": [
                {
                    "price": {"id": price_id},
                    "period": {"end": PERIOD_END_EPOCH},
                }
            ]
        },
    }
    if billing_reason is not None:
        invoice["billing_reason"] = billing_reason

    return {"id": event_id, "type": "invoice.paid", "data": {"object": invoice}}


# ----------------------------------------------------------------------
# Signature verification -- constraint 5
# ----------------------------------------------------------------------


def test_a_valid_signature_is_accepted(client, dynamodb_client):
    seed_entitlement(dynamodb_client, available=0)

    response = post_event(client, checkout_event())

    assert response.status_code == 200


def test_a_forged_signature_is_rejected_and_changes_nothing(client, dynamodb_client, store):
    """Signed with the wrong secret: the body is well-formed, the MAC is not."""
    seed_entitlement(dynamodb_client, available=0)

    response = post_event(client, checkout_event(), secret="whsec_wrong")

    assert response.status_code == 400
    assert store.get_entitlement(USER_ID).available == 0


def test_a_missing_signature_header_is_rejected(client, dynamodb_client, store):
    seed_entitlement(dynamodb_client, available=0)

    response = client.post("/billing/webhook", content=json.dumps(checkout_event()).encode())

    assert response.status_code == 400
    assert store.get_entitlement(USER_ID).available == 0


def test_a_tampered_body_is_rejected(client, dynamodb_client, store):
    """The classic attack: sign a cheap purchase, then swap in a bigger one."""
    seed_entitlement(dynamodb_client, available=0)
    honest = json.dumps(checkout_event(product_key="pack_10")).encode()
    header = sign(honest)
    tampered = json.dumps(checkout_event(product_key="pack_30")).encode()

    response = client.post(
        "/billing/webhook", content=tampered, headers={"Stripe-Signature": header}
    )

    assert response.status_code == 400
    assert store.get_entitlement(USER_ID).available == 0


def test_a_stale_timestamp_is_rejected(client, dynamodb_client, store):
    """Replay protection: Stripe's tolerance is five minutes."""
    seed_entitlement(dynamodb_client, available=0)

    response = post_event(client, checkout_event(), timestamp=int(time.time()) - 3600)

    assert response.status_code == 400
    assert store.get_entitlement(USER_ID).available == 0


def test_the_store_is_never_touched_for_a_bad_signature(stripe_configured, dynamodb_client):
    """Belt and braces: assert on the store itself, not just on the balance."""
    store = MagicMock()
    app.dependency_overrides[get_store] = lambda: store
    try:
        response = post_event(TestClient(app), checkout_event(), secret="whsec_wrong")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400
    store.apply_stripe_credit.assert_not_called()
    store.apply_subscription_update.assert_not_called()


# ----------------------------------------------------------------------
# checkout.session.completed
# ----------------------------------------------------------------------


def test_a_credit_pack_grants_its_credits(client, dynamodb_client, store):
    seed_entitlement(dynamodb_client, available=1)

    assert post_event(client, checkout_event(product_key="pack_10")).status_code == 200

    assert store.get_entitlement(USER_ID).available == 11


def test_a_redelivered_event_grants_only_once(client, dynamodb_client, store):
    """Stripe retries on timeout; the event id makes that safe."""
    seed_entitlement(dynamodb_client, available=0)
    event = checkout_event(event_id="evt_dup")

    assert post_event(client, event).status_code == 200
    assert post_event(client, event).status_code == 200

    assert store.get_entitlement(USER_ID).available == 10


def test_a_subscription_checkout_sets_the_plan(client, dynamodb_client, store):
    seed_entitlement(dynamodb_client, available=0)

    response = post_event(
        client,
        checkout_event(
            product_key="plan_monthly",
            subscription=SUBSCRIPTION,
            current_period_end=PERIOD_END_EPOCH,
        ),
    )

    assert response.status_code == 200
    entitlement = store.get_entitlement(USER_ID)
    assert entitlement.plan == "monthly"
    assert entitlement.available == 20
    assert entitlement.period_end is not None


def test_an_unknown_product_key_is_acknowledged_without_change(client, dynamodb_client, store):
    """200 so Stripe stops retrying an event we can never act on."""
    seed_entitlement(dynamodb_client, available=3)

    response = post_event(client, checkout_event(product_key="pack_does_not_exist"))

    assert response.status_code == 200
    assert store.get_entitlement(USER_ID).available == 3


def test_a_session_without_a_user_is_acknowledged_without_change(client, dynamodb_client, store):
    seed_entitlement(dynamodb_client, available=3)
    event = checkout_event()
    event["data"]["object"]["client_reference_id"] = None
    event["data"]["object"]["metadata"] = {"product_key": "pack_10"}

    assert post_event(client, event).status_code == 200

    assert store.get_entitlement(USER_ID).available == 3


# ----------------------------------------------------------------------
# invoice.paid
# ----------------------------------------------------------------------


def test_a_renewal_grants_the_new_period(client, dynamodb_client, store):
    seed_entitlement(dynamodb_client, available=0)

    assert post_event(client, invoice_event()).status_code == 200

    entitlement = store.get_entitlement(USER_ID)
    assert entitlement.available == 20
    assert entitlement.plan == "monthly"
    assert entitlement.period_end is not None


def test_a_renewal_is_idempotent(client, dynamodb_client, store):
    seed_entitlement(dynamodb_client, available=0)
    event = invoice_event(event_id="evt_inv_dup")

    post_event(client, event)
    post_event(client, event)

    assert store.get_entitlement(USER_ID).available == 20


def test_an_invoice_for_an_unknown_price_changes_nothing(client, dynamodb_client, store):
    seed_entitlement(dynamodb_client, available=4)

    response = post_event(client, invoice_event(price_id="price_not_in_catalogue"))

    assert response.status_code == 200
    assert store.get_entitlement(USER_ID).available == 4


def test_a_one_off_invoice_is_ignored(client, dynamodb_client, store):
    """checkout.session.completed already granted it; acting here double-counts."""
    seed_entitlement(dynamodb_client, available=2)
    event = invoice_event(event_id="evt_oneoff")
    event["data"]["object"]["subscription"] = None

    assert post_event(client, event).status_code == 200

    assert store.get_entitlement(USER_ID).available == 2


def test_the_first_subscription_period_grants_credits_once(client, dynamodb_client, store):
    """Checkout and the opening invoice describe one period, not two.

    Stripe emits both for a subscription bought through Checkout, under
    different event ids, so the per-event marker cannot collapse them.
    """
    seed_entitlement(dynamodb_client, available=0)

    post_event(
        client,
        checkout_event(
            event_id="evt_checkout_first",
            product_key="plan_monthly",
            subscription=SUBSCRIPTION,
            current_period_end=PERIOD_END_EPOCH,
        ),
    )
    post_event(
        client,
        invoice_event(event_id="evt_invoice_first", billing_reason="subscription_create"),
    )

    assert store.get_entitlement(USER_ID).available == 20


def test_an_opening_invoice_alone_grants_nothing(client, dynamodb_client, store):
    """The accepted cost of the guard: the session is what grants a first period."""
    seed_entitlement(dynamodb_client, available=0)

    response = post_event(
        client,
        invoice_event(event_id="evt_inv_create", billing_reason="subscription_create"),
    )

    assert response.status_code == 200
    assert store.get_entitlement(USER_ID).available == 0


def test_a_renewal_without_a_billing_reason_still_grants(client, dynamodb_client, store):
    """Fail toward granting: a customer who paid and got nothing is the worse bug."""
    seed_entitlement(dynamodb_client, available=0)

    post_event(client, invoice_event(event_id="evt_inv_no_reason", billing_reason=None))

    assert store.get_entitlement(USER_ID).available == 20


def test_a_renewal_in_the_2025_api_shape_grants(client, dynamodb_client, store):
    """2025-03-31.basil: no invoice.subscription and no invoice.metadata -- both
    moved under invoice.parent.subscription_details. The old single-location
    reads answered 200 and silently dropped every renewal on this shape."""
    seed_entitlement(dynamodb_client, available=0)
    invoice = {
        "id": "in_test_modern",
        "billing_reason": "subscription_cycle",
        "parent": {
            "type": "subscription_details",
            "subscription_details": {
                "subscription": SUBSCRIPTION,
                "metadata": {"cognito_sub": USER_ID},
            },
        },
        "lines": {
            "data": [
                {
                    "price": {"id": "price_placeholder_plan_monthly"},
                    "period": {"end": PERIOD_END_EPOCH},
                }
            ]
        },
    }
    event = {"id": "evt_inv_2025", "type": "invoice.paid", "data": {"object": invoice}}

    response = post_event(client, event)

    assert response.status_code == 200
    entitlement = store.get_entitlement(USER_ID)
    assert entitlement.available == 20
    assert entitlement.period_end is not None


def test_a_renewal_with_metadata_on_subscription_details_grants(client, dynamodb_client, store):
    """2022-11 onwards: invoice.metadata is the invoice's own (empty) metadata;
    the subscription's copy sits at invoice.subscription_details.metadata."""
    seed_entitlement(dynamodb_client, available=0)
    event = invoice_event(event_id="evt_inv_subdetails")
    invoice = event["data"]["object"]
    invoice["metadata"] = {}
    invoice["subscription_details"] = {"metadata": {"cognito_sub": USER_ID}}

    post_event(client, event)

    assert store.get_entitlement(USER_ID).available == 20


# ----------------------------------------------------------------------
# customer.subscription.deleted
# ----------------------------------------------------------------------


def test_cancellation_drops_the_plan_but_keeps_credits(client, dynamodb_client, store):
    seed_entitlement(dynamodb_client, available=0)
    post_event(
        client,
        checkout_event(
            event_id="evt_sub_start",
            product_key="plan_monthly",
            subscription=SUBSCRIPTION,
            current_period_end=PERIOD_END_EPOCH,
        ),
    )

    response = post_event(
        client,
        {
            "id": "evt_sub_end",
            "type": "customer.subscription.deleted",
            "data": {
                "object": {"id": SUBSCRIPTION, "metadata": {"cognito_sub": USER_ID}},
            },
        },
    )

    assert response.status_code == 200
    entitlement = store.get_entitlement(USER_ID)
    assert entitlement.plan == "free"
    assert entitlement.available == 20  # paid for, still spendable
    assert entitlement.period_end is None


# ----------------------------------------------------------------------
# Unhandled events
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "event_type",
    ["payment_intent.succeeded", "customer.created", "invoice.payment_failed"],
)
def test_unhandled_events_are_acknowledged(client, dynamodb_client, store, event_type):
    """Anything not 2xx keeps Stripe retrying forever."""
    seed_entitlement(dynamodb_client, available=1)

    response = post_event(
        client, {"id": f"evt_{event_type}", "type": event_type, "data": {"object": {}}}
    )

    assert response.status_code == 200
    assert store.get_entitlement(USER_ID).available == 1


# ----------------------------------------------------------------------
# POST /billing/checkout
# ----------------------------------------------------------------------


@pytest.fixture
def fake_stripe(monkeypatch):
    from api.routers import billing

    fake = MagicMock()
    fake.checkout.Session.create.return_value = MagicMock(url="https://checkout.stripe.com/c/test")
    monkeypatch.setattr(billing, "get_stripe", lambda: fake)
    return fake


def test_checkout_returns_a_hosted_session_url(client, fake_stripe):
    response = client.post("/billing/checkout", json={"product_key": "pack_10"})

    assert response.status_code == 200
    assert response.json()["checkout_url"] == "https://checkout.stripe.com/c/test"


def test_checkout_uses_payment_mode_for_a_credit_pack(client, fake_stripe):
    client.post("/billing/checkout", json={"product_key": "pack_10"})

    assert fake_stripe.checkout.Session.create.call_args.kwargs["mode"] == "payment"


def test_checkout_uses_subscription_mode_for_a_plan(client, fake_stripe):
    client.post("/billing/checkout", json={"product_key": "plan_monthly"})

    kwargs = fake_stripe.checkout.Session.create.call_args.kwargs
    assert kwargs["mode"] == "subscription"
    # The metadata copy is what lets a renewal invoice, which has no session,
    # still be attributed to this user.
    assert kwargs["subscription_data"]["metadata"]["cognito_sub"] == USER_ID


def test_checkout_carries_the_cognito_sub(client, fake_stripe):
    client.post("/billing/checkout", json={"product_key": "pack_10"})

    kwargs = fake_stripe.checkout.Session.create.call_args.kwargs
    assert kwargs["client_reference_id"] == USER_ID
    assert kwargs["metadata"]["cognito_sub"] == USER_ID


def test_checkout_sends_the_catalogue_price_not_a_client_value(client, fake_stripe):
    """The client names a product key; only the server resolves the price."""
    client.post("/billing/checkout", json={"product_key": "pack_30"})

    line_items = fake_stripe.checkout.Session.create.call_args.kwargs["line_items"]
    assert line_items == [{"price": products.by_key("pack_30").price_id, "quantity": 1}]


def test_checkout_rejects_an_unknown_product(client, fake_stripe):
    response = client.post("/billing/checkout", json={"product_key": "pack_nonexistent"})

    assert response.status_code == 404
    fake_stripe.checkout.Session.create.assert_not_called()


def test_checkout_rejects_a_price_id_passed_as_a_product_key(client, fake_stripe):
    """A client must not be able to name an arbitrary Stripe price."""
    response = client.post("/billing/checkout", json={"product_key": "price_placeholder_pack_30"})

    assert response.status_code == 404
    fake_stripe.checkout.Session.create.assert_not_called()


def test_checkout_surfaces_a_stripe_failure_as_502(client, fake_stripe):
    fake_stripe.checkout.Session.create.side_effect = RuntimeError("stripe is down")

    assert client.post("/billing/checkout", json={"product_key": "pack_10"}).status_code == 502


def test_checkout_requires_authentication(store, stripe_configured):
    """The real identity dependency is left in place here."""
    app.dependency_overrides[get_store] = lambda: store
    try:
        response = TestClient(app).post("/billing/checkout", json={"product_key": "pack_10"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 401


# ----------------------------------------------------------------------
# Product catalogue
# ----------------------------------------------------------------------


def test_the_catalogue_can_be_overridden_by_environment(monkeypatch):
    monkeypatch.setenv(
        products.PRODUCTS_ENV_VAR,
        json.dumps({"pack_5": {"price_id": "price_real", "kind": "credit_pack", "credits": 5}}),
    )
    products.reset_catalogue_cache()

    product = products.by_key("pack_5")

    assert product is not None
    assert product.price_id == "price_real"
    assert product.checkout_mode == "payment"
    assert products.by_key("pack_10") is None  # the override replaces, not merges


def test_a_malformed_catalogue_falls_back_to_the_builtin(monkeypatch):
    """A bad env var must not take payments down entirely."""
    monkeypatch.setenv(products.PRODUCTS_ENV_VAR, "{not json")
    products.reset_catalogue_cache()

    assert products.by_key("pack_10") is not None


def test_a_catalogue_entry_missing_a_price_id_falls_back(monkeypatch):
    """Valid JSON with a broken entry gets the same treatment as invalid JSON --
    not a KeyError turned 500 on the first request that touches the catalogue."""
    monkeypatch.setenv(
        products.PRODUCTS_ENV_VAR,
        json.dumps({"pack_broken": {"kind": "credit_pack", "credits": 5}}),
    )
    products.reset_catalogue_cache()

    assert products.by_key("pack_broken") is None
    assert products.by_key("pack_10") is not None


def test_price_ids_round_trip_through_the_catalogue():
    for product in products.catalogue().values():
        assert products.by_price_id(product.price_id) is product
