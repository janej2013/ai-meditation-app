"""Generation route.

Stub until milestone 3. The request contract is already enforced so the
frontend can be built against it, but the pipeline does not exist yet: this
Lambda will only validate the JWT, freeze a credit and start the Step Functions
execution (constraint 2). Bedrock and TTS are never called from here.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from api.deps import CurrentUserDep

router = APIRouter(tags=["generate"])

MIN_DURATION_MINUTES = 3
MAX_DURATION_MINUTES = 30


class GenerateRequest(BaseModel):
    """How the user feels, and how long a meditation they want."""

    mood: str = Field(min_length=1, max_length=500)
    duration_minutes: int = Field(
        ge=MIN_DURATION_MINUTES,
        le=MAX_DURATION_MINUTES,
    )


@router.post("/generate", status_code=status.HTTP_501_NOT_IMPLEMENTED)
def generate(payload: GenerateRequest, user: CurrentUserDep) -> None:
    # Body and identity are validated first, so callers get 422 for a bad
    # request and 401 for a bad token -- 501 means only "not built yet".
    del payload, user
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Generation pipeline not implemented yet (milestone 3).",
    )
