"""Stripe credentials and client configuration.

Constraint 4 keeps the API key and the webhook signing secret out of plaintext
Lambda environment variables, so only ``STRIPE_SECRET_ARN`` is injected and the
values are read through Secrets Manager once per container.

The secret is a JSON document:

    {"secret_key": "sk_live_...", "webhook_secret": "whsec_..."}
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import boto3
import stripe

logger = logging.getLogger(__name__)

SECRET_ARN_ENV_VAR = "STRIPE_SECRET_ARN"


class StripeConfigError(RuntimeError):
    """The Stripe credentials are missing or unusable."""


class StripeCredentials:
    """The two Stripe secrets this service needs."""

    __slots__ = ("secret_key", "webhook_secret")

    def __init__(self, secret_key: str, webhook_secret: str) -> None:
        self.secret_key = secret_key
        self.webhook_secret = webhook_secret


_credentials: StripeCredentials | None = None


def load_credentials(secret_arn: str | None = None, client: Any = None) -> StripeCredentials:
    """Read the Stripe secret, caching for the container's lifetime.

    Secrets Manager charges per call and adds latency, so a warm Lambda reads
    this once. Both fields are required: without ``webhook_secret`` the webhook
    could not verify a signature, and constraint 5 forbids acting on an
    unverified event.
    """
    global _credentials
    if _credentials is not None:
        return _credentials

    arn = secret_arn or os.environ.get(SECRET_ARN_ENV_VAR)
    if not arn:
        raise StripeConfigError(f"{SECRET_ARN_ENV_VAR} is not set")

    secrets = client if client is not None else boto3.client("secretsmanager")
    try:
        payload = secrets.get_secret_value(SecretId=arn)["SecretString"]
    except Exception as exc:
        # Deliberately broad, and deliberately terse: every failure mode here
        # is permanent for this request, and the message must not carry any of
        # the secret's content into a log line.
        raise StripeConfigError("could not read the Stripe credentials secret") from exc

    try:
        document = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise StripeConfigError("Stripe credentials secret is not valid JSON") from exc

    missing = [field for field in ("secret_key", "webhook_secret") if not document.get(field)]
    if missing:
        raise StripeConfigError(f"Stripe credentials secret is missing: {', '.join(missing)}")

    _credentials = StripeCredentials(
        secret_key=document["secret_key"],
        webhook_secret=document["webhook_secret"],
    )
    return _credentials


def reset_credentials_cache() -> None:
    """Drop the cached secret. For tests; production reads once per container."""
    global _credentials
    _credentials = None


def get_stripe() -> Any:
    """The configured ``stripe`` module.

    Returning the module rather than a client keeps the call sites readable
    (``get_stripe().checkout.Session.create(...)``) and gives tests one thing
    to patch.
    """
    stripe.api_key = load_credentials().secret_key
    return stripe
