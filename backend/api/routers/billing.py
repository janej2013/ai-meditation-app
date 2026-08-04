"""Stripe Checkout and the webhook that grants what was paid for.

Two routes with opposite trust models:

* ``POST /billing/checkout`` sits behind the JWT authorizer like every other
  route. It only creates a hosted Checkout Session -- there is no custom
  payment UI anywhere in this project, and card details never reach us.
* ``POST /billing/webhook`` is called by Stripe, which cannot hold a Cognito
  token, so API Gateway is configured to let it through unauthenticated. Its
  security is the signature: constraint 5 requires
  ``stripe.Webhook.construct_event`` to verify the raw body against the
  ``Stripe-Signature`` header **before any state changes**, and a failure
  returns 400 having touched nothing.

Entitlement changes go through ``shared.db`` (constraint 1). This module pulls
plain values out of the Stripe event and hands those over; ``db.py`` never
imports stripe.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from api import products
from api.deps import CurrentUserDep, StoreDep
from api.stripe_client import StripeConfigError, get_stripe, load_credentials

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/billing", tags=["billing"])

# The frontend does not exist yet (milestone 6), so these are configurable
# placeholders rather than hard-coded URLs.
SUCCESS_URL_ENV_VAR = "STRIPE_SUCCESS_URL"
CANCEL_URL_ENV_VAR = "STRIPE_CANCEL_URL"
DEFAULT_SUCCESS_URL = "http://localhost:5173/billing/success"
DEFAULT_CANCEL_URL = "http://localhost:5173/billing/cancel"

SIGNATURE_HEADER = "stripe-signature"

# Everything else is acknowledged and ignored. Stripe sends dozens of event
# types; subscribing narrowly here means an unexpected one cannot take an
# unexpected path.
CHECKOUT_COMPLETED = "checkout.session.completed"
INVOICE_PAID = "invoice.paid"
SUBSCRIPTION_DELETED = "customer.subscription.deleted"

# Stripe's billing_reason on the invoice Checkout raises for a subscription's
# opening period. Renewals carry subscription_cycle instead.
FIRST_PERIOD_BILLING_REASON = "subscription_create"

_ACK: dict[str, str] = {"status": "ok"}


class CheckoutRequest(BaseModel):
    """Which catalogue entry the user wants to buy."""

    product_key: str = Field(min_length=1, max_length=64)


class CheckoutResponse(BaseModel):
    checkout_url: str
    product_key: str


@router.post("/checkout", response_model=CheckoutResponse)
def create_checkout_session(
    payload: CheckoutRequest,
    user: CurrentUserDep,
) -> CheckoutResponse:
    """Create a hosted Stripe Checkout Session and hand back its URL."""
    product = products.by_key(payload.product_key)
    if product is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown product {payload.product_key!r}.",
        )

    try:
        stripe = get_stripe()
        session = stripe.checkout.Session.create(
            mode=product.checkout_mode,
            line_items=[{"price": product.price_id, "quantity": 1}],
            # Both carry the Cognito sub: client_reference_id is what Stripe
            # surfaces on checkout.session.completed, and the metadata copy
            # rides onto the subscription so renewal invoices -- which have no
            # session -- can still be attributed to a user.
            client_reference_id=user.sub,
            metadata={"cognito_sub": user.sub, "product_key": product.key},
            subscription_data=(
                {"metadata": {"cognito_sub": user.sub, "product_key": product.key}}
                if product.kind == "subscription"
                else None
            ),
            success_url=os.environ.get(SUCCESS_URL_ENV_VAR, DEFAULT_SUCCESS_URL),
            cancel_url=os.environ.get(CANCEL_URL_ENV_VAR, DEFAULT_CANCEL_URL),
        )
    except StripeConfigError:
        logger.exception("Stripe is not configured")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Payments are unavailable. Please retry shortly.",
        ) from None
    except Exception:
        logger.exception("could not create a checkout session product=%s", product.key)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not start checkout. Please retry.",
        ) from None

    logger.info("checkout session created product=%s mode=%s", product.key, product.checkout_mode)
    return CheckoutResponse(checkout_url=session.url, product_key=product.key)


@router.post("/webhook", include_in_schema=False)
async def stripe_webhook(request: Request, store: StoreDep) -> dict[str, str]:
    """Apply what Stripe says was paid for.

    Async because the signature is computed over the **raw** body, which
    Starlette only exposes through an awaitable -- re-serialising a parsed
    payload would change the bytes and break the MAC.
    """
    payload = await request.body()
    signature = request.headers.get(SIGNATURE_HEADER)

    try:
        webhook_secret = load_credentials().webhook_secret
    except StripeConfigError:
        logger.exception("Stripe is not configured; refusing to process a webhook")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Webhook processing is unavailable.",
        ) from None

    # Constraint 5: verify before anything else. Nothing below this point runs
    # for a request that fails here, so an unsigned or forged body cannot move
    # a single credit.
    try:
        event = get_stripe().Webhook.construct_event(payload, signature, webhook_secret)
    except Exception:
        # Covers both a bad signature and a malformed body. No stack trace and
        # no payload: an attacker's probe should not fill the logs, and a
        # genuine body may carry customer details.
        logger.warning("rejected a webhook with an invalid signature")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid signature.",
        ) from None

    event_id = event["id"]
    event_type = event["type"]

    handler = {
        CHECKOUT_COMPLETED: _handle_checkout_completed,
        INVOICE_PAID: _handle_invoice_paid,
        SUBSCRIPTION_DELETED: _handle_subscription_deleted,
    }.get(event_type)

    if handler is None:
        # Stripe keeps retrying anything that is not 2xx, so an event we do not
        # care about still has to be acknowledged.
        logger.info("ignoring event id=%s type=%s", event_id, event_type)
        return _ACK

    # Only ids, types and outcomes reach the log -- never the event body, which
    # carries customer email and billing details (constraint 7).
    outcome = handler(store, event_id, event["data"]["object"])
    logger.info("processed event id=%s type=%s outcome=%s", event_id, event_type, outcome)
    return _ACK


# ----------------------------------------------------------------------
# Event handlers. Each returns a short outcome string for the log line.
# ----------------------------------------------------------------------


def _handle_checkout_completed(store: Any, event_id: str, session: dict[str, Any]) -> str:
    """First payment: a credit pack, or the opening period of a subscription."""
    user_id = _user_from(session)
    if user_id is None:
        return "no-user"

    product = _product_from_metadata(session)
    if product is None:
        return "unknown-product"

    if product.kind == "credit_pack":
        result = store.apply_stripe_credit(user_id, event_id, product.credits)
        return "granted" if result.applied else "duplicate"

    result = store.apply_subscription_update(
        user_id,
        event_id,
        plan=product.plan,
        period_end=_period_end_from(session),
        credits=product.credits,
        subscription_id=session.get("subscription"),
    )
    return "subscribed" if result.applied else "duplicate"


def _handle_invoice_paid(store: Any, event_id: str, invoice: dict[str, Any]) -> str:
    """Renewal: push period_end out and grant the new period's credits."""
    if not invoice.get("subscription"):
        # A one-off payment also produces an invoice; checkout.session.completed
        # already granted it, so acting here would double-count.
        return "not-a-subscription"

    if invoice.get("billing_reason") == FIRST_PERIOD_BILLING_REASON:
        # Buying a subscription through Checkout raises this invoice *and*
        # checkout.session.completed for the same opening period, under two
        # different event ids -- so the per-event marker cannot collapse them,
        # and granting here as well would double the first period. The session
        # is the event a Checkout purchase always produces, so it is the one
        # that grants.
        #
        # Only an explicit subscription_create is skipped. An invoice that
        # carries no billing_reason still grants: dropping a renewal leaves a
        # customer who paid with nothing, which is worse than the duplicate
        # this guard exists to prevent.
        return "first-period"

    user_id = _user_from(invoice)
    if user_id is None:
        return "no-user"

    product = _product_from_line_items(invoice)
    if product is None:
        logger.warning("invoice paid for a price outside the catalogue event_id=%s", event_id)
        return "unknown-product"

    result = store.apply_subscription_update(
        user_id,
        event_id,
        plan=product.plan,
        period_end=_period_end_from(invoice),
        credits=product.credits,
        subscription_id=invoice.get("subscription"),
    )
    return "renewed" if result.applied else "duplicate"


def _handle_subscription_deleted(store: Any, event_id: str, subscription: dict[str, Any]) -> str:
    """Cancellation: back to free, leaving already-paid-for credits spendable."""
    user_id = _user_from(subscription)
    if user_id is None:
        return "no-user"

    result = store.apply_subscription_update(
        user_id,
        event_id,
        plan="free",
        period_end=None,
        credits=0,
        subscription_id=subscription.get("id"),
    )
    return "cancelled" if result.applied else "duplicate"


# ----------------------------------------------------------------------
# Extracting plain values from Stripe objects
# ----------------------------------------------------------------------


def _user_from(obj: dict[str, Any]) -> str | None:
    """The Cognito sub, wherever this object type carries it.

    Checkout sessions have ``client_reference_id``; subscriptions and their
    invoices only have the metadata copy that ``subscription_data`` attached.
    """
    user_id = obj.get("client_reference_id") or (obj.get("metadata") or {}).get("cognito_sub")
    if not user_id:
        # Nothing to attribute the payment to. Logged as an error because it
        # means a paying customer got nothing and needs manual repair.
        logger.error("Stripe object carried no cognito sub; cannot grant entitlement")
    return user_id


def _product_from_metadata(session: dict[str, Any]) -> products.Product | None:
    key = (session.get("metadata") or {}).get("product_key")
    if not key:
        logger.warning("checkout session carried no product_key")
        return None

    product = products.by_key(key)
    if product is None:
        logger.warning("checkout session named a product outside the catalogue")
    return product


def _product_from_line_items(invoice: dict[str, Any]) -> products.Product | None:
    """Resolve the subscription's price from the invoice's line items."""
    for line in (invoice.get("lines") or {}).get("data") or []:
        price_id = (line.get("price") or {}).get("id")
        if price_id:
            product = products.by_price_id(price_id)
            if product is not None:
                return product
    return None


def _period_end_from(obj: dict[str, Any]) -> str | None:
    """The end of the paid period, as an ISO 8601 string.

    Stripe sends epoch seconds; ``db.py`` stores an ISO string so the value
    reads the same as every other timestamp in the table.

    Subscriptions carry ``current_period_end`` directly; invoices only have it
    on the line item's ``period``.
    """
    epoch = obj.get("current_period_end")
    if not epoch:
        lines = (obj.get("lines") or {}).get("data") or []
        epoch = next(
            (line.get("period", {}).get("end") for line in lines if line.get("period")), None
        )
    if not epoch:
        return None
    return datetime.fromtimestamp(int(epoch), tz=UTC).isoformat()
