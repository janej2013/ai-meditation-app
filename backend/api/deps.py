"""Request dependencies: the caller's identity and the data store.

The API Gateway HTTP API JWT authorizer has already validated the token's
signature, issuer, audience and expiry before the Lambda is ever invoked. This
module therefore only *reads* the resulting claims -- it does no cryptography
and must not, because re-verifying here would duplicate the authorizer while
being easier to get wrong.

Mangum places the raw Lambda event on the ASGI scope as ``aws.event``; payload
format 2.0 carries the validated claims at
``requestContext.authorizer.jwt.claims``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from shared.db import EntitlementStore
from shared.jobs import GateOutcome, generation_gate
from shared.models import Entitlement

# Cognito sets token_use to distinguish its two token types. Access tokens carry
# neither an `aud` claim nor `email`, so this API requires ID tokens.
ID_TOKEN_USE = "id"


@dataclass(frozen=True)
class CurrentUser:
    """The authenticated caller, as described by the JWT claims."""

    sub: str
    email: str | None = None


def get_claims(request: Request) -> dict[str, str]:
    """Pull the authorizer's validated claims off the Lambda event."""
    event = request.scope.get("aws.event") or {}
    claims = event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims")
    if not claims:
        # Reachable only if a route is misconfigured without the authorizer, or
        # when running outside Lambda. Fail closed either way.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No verified JWT claims on the request.",
        )
    return claims


def get_current_user(claims: Annotated[dict[str, str], Depends(get_claims)]) -> CurrentUser:
    if claims.get("token_use") != ID_TOKEN_USE:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="An ID token is required; access tokens carry no email claim.",
        )
    sub = claims.get("sub")
    if not sub:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="JWT claims carried no subject.",
        )
    return CurrentUser(sub=sub, email=claims.get("email"))


_store: EntitlementStore | None = None


def get_store() -> EntitlementStore:
    """Reuse one store across warm invocations.

    Built lazily rather than at import so that importing the app never requires
    TABLE_NAME to be set.
    """
    global _store
    if _store is None:
        _store = EntitlementStore()
    return _store


CurrentUserDep = Annotated[CurrentUser, Depends(get_current_user)]


def require_credit(store: EntitlementStore, user_id: str) -> Entitlement:
    """The one 402: every picture route (whose vision call is spent before
    anything is frozen) needs a credit in hand.

    Only the credit half of ``shared.jobs.generation_gate``: a picture may be
    read while another generation runs, so JOB_IN_FLIGHT passes here. The
    generate route maps the whole gate itself.
    """
    gate = generation_gate(store, user_id)
    if gate.outcome is GateOutcome.NO_CREDIT or gate.entitlement is None:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="No generations remaining. Add credits to continue.",
        )
    return gate.entitlement


StoreDep = Annotated[EntitlementStore, Depends(get_store)]
