"""Unauthenticated liveness probe.

The only route not behind the JWT authorizer -- see api_stack, where it is
explicitly opted out with HttpNoneAuthorizer.
"""

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: str


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok")
