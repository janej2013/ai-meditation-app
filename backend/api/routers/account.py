"""Account routes: the caller's credit balance."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from api.deps import CurrentUserDep, StoreDep

logger = logging.getLogger(__name__)

router = APIRouter(tags=["account"])


class AccountResponse(BaseModel):
    available: int
    frozen: int
    plan: str


@router.get("/account", response_model=AccountResponse)
def get_account(user: CurrentUserDep, store: StoreDep) -> AccountResponse:
    entitlement = store.get_entitlement(user.sub)

    if entitlement is None:
        # The post-confirmation trigger never ran, or ran while DynamoDB was
        # unavailable -- it is deliberately built to swallow such errors rather
        # than block signup. Repair the gap here. initialize_user is the same
        # idempotent call the trigger makes, so a race between the two is safe.
        logger.info("no entitlement found, lazily initializing sub=%s", user.sub)
        store.initialize_user(user.sub, user.email)
        entitlement = store.get_entitlement(user.sub)

    if entitlement is None:
        logger.error("entitlement still missing after initialization sub=%s", user.sub)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not load your account. Please retry shortly.",
        )

    return AccountResponse(
        available=entitlement.available,
        frozen=entitlement.frozen,
        plan=entitlement.plan,
    )
