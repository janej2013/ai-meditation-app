"""Cognito post-confirmation trigger.

Grants the free signup credit by creating the user's PROFILE and ENTITLEMENT
items. The actual write lives in ``shared.db.EntitlementStore.initialize_user``
because creating ENTITLEMENT is an entitlement mutation (constraint 1); this
handler is only the Cognito adapter around it.

Failure policy: a DynamoDB problem must never block signup. Every error is
logged and swallowed, and the event is returned unchanged so Cognito completes
the flow. The cost of that choice is a user who exists with no entitlement; the
lazy-init path in ``GET /account`` is the compensating control that repairs it
on their first authenticated request.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from shared.db import EntitlementStore

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Only a genuine signup grants the credit. Cognito reuses this trigger for
# password-reset confirmation, where there is nothing to provision.
SIGNUP_TRIGGER = "PostConfirmation_ConfirmSignUp"

_store: EntitlementStore | None = None


def _get_store() -> EntitlementStore:
    """Build the store lazily so import never depends on TABLE_NAME."""
    global _store
    if _store is None:
        _store = EntitlementStore()
    return _store


def lambda_handler(event: dict[str, Any], context: object) -> dict[str, Any]:  # noqa: ARG001
    trigger_source = event.get("triggerSource", "")
    attributes = event.get("request", {}).get("userAttributes", {})
    sub = attributes.get("sub")

    if trigger_source != SIGNUP_TRIGGER:
        logger.info("skipping trigger source=%s", trigger_source)
        return event

    if not sub:
        logger.error("post-confirmation event carried no sub; cannot provision")
        return event

    try:
        # No PII in logs (constraint 7): sub only, never the email address --
        # which is stored on the PROFILE item but never written to a log line.
        granted = _get_store().initialize_user(sub, attributes.get("email"))
        if granted:
            logger.info("provisioned new user sub=%s", sub)
        else:
            logger.info("user already provisioned, no-op sub=%s", sub)
    except Exception:
        # Deliberately broad: no DynamoDB failure mode is worth blocking a
        # signup for. GET /account will lazily initialize instead.
        logger.exception("failed to provision user sub=%s; continuing", sub)

    return event
