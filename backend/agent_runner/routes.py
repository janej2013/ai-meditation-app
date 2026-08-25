"""The companion routes (docs/agent-runner-plan.md §5).

Every route but ``/health`` requires a verified ID token. Sessions and
memory are the caller's own: another user's session is simply absent
(404), never forbidden, so there is no existence oracle.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, field_validator

from agent_runner.deps import CurrentUserDep, DepsDep
from agent_runner.lambda_context import deadline_from_headers
from agent_runner.sse import stream_turn
from agent_runner.turns import SessionExhaustedError, build_engine, claim_turn, run_claimed
from shared.db import AgentTurnBusyError
from shared.models import AgentSessionStatus, AgentTurn

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agent", tags=["agent"])

MAX_TURN_TEXT_CHARS = 1000


class SessionCreated(BaseModel):
    session_id: str
    turn: int
    engine: str
    model_id: str
    insights_count: int


class TurnRequest(BaseModel):
    text: str

    @field_validator("text")
    @classmethod
    def _trimmed(cls, value: str) -> str:
        cleaned = value.strip()
        if not 1 <= len(cleaned) <= MAX_TURN_TEXT_CHARS:
            raise ValueError(f"text must be 1-{MAX_TURN_TEXT_CHARS} characters after trimming")
        return cleaned


class TranscriptTurn(BaseModel):
    turn: int
    user_text: str
    assistant_text: str
    tools: list[str]
    created_at: datetime | None


class Transcript(BaseModel):
    session_id: str
    status: AgentSessionStatus
    turn: int
    job_id: str | None
    turns: list[TranscriptTurn]


class InsightOut(BaseModel):
    text: str
    created_at: datetime


class MemoryOut(BaseModel):
    insights: list[InsightOut]


# ----------------------------------------------------------------------
# Sessions
# ----------------------------------------------------------------------


@router.post("/sessions", response_model=SessionCreated, status_code=status.HTTP_201_CREATED)
def create_session(user: CurrentUserDep, deps: DepsDep) -> SessionCreated:
    store, settings = deps.store, deps.settings
    entitlement = store.get_entitlement(user.sub)
    if entitlement is None:
        # Same self-repair as GET /account: the signup trigger may not have run.
        store.initialize_user(user.sub, user.email)
        entitlement = store.get_entitlement(user.sub)
    if entitlement is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="account_unavailable"
        )
    if entitlement.plan not in settings.allowed_plans:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="plan_required")

    month = deps.clock().strftime("%Y-%m")
    if not store.reserve_agent_session(user.sub, month, settings.sessions_per_month):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="quota_exhausted")

    session_id = str(uuid.uuid4())
    if not store.create_agent_session(
        user.sub, session_id, engine=settings.engine, model_id=deps.provider.model_id
    ):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="session_exists")
    insights = len(store.get_memory(user.sub).insights)
    logger.info("session created", extra={"session_id": session_id})
    return SessionCreated(
        session_id=session_id,
        turn=0,
        engine=settings.engine,
        model_id=deps.provider.model_id,
        insights_count=insights,
    )


@router.post("/sessions/{session_id}/turns")
async def post_turn(
    session_id: str, payload: TurnRequest, request: Request, user: CurrentUserDep, deps: DepsDep
) -> StreamingResponse:
    store, settings = deps.store, deps.settings
    if store.get_agent_session(user.sub, session_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="session_not_found")

    # Claimed before the stream opens, so "busy" is still an HTTP status the
    # client can branch on; only what happens during the turn is an SSE error.
    try:
        session = await claim_turn(
            store, user_id=user.sub, session_id=session_id, engine_name=settings.engine
        )
    except AgentTurnBusyError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="busy_or_closed") from None
    except SessionExhaustedError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="session_exhausted"
        ) from None

    engine = build_engine(
        engine_name=settings.engine,
        provider=deps.provider,
        store=store,
        sfn=deps.sfn,
        user_id=user.sub,
        session_id=session_id,
        clock=deps.clock,
    )
    deadline = deadline_from_headers(request.headers, settings.turn_seconds_fallback)

    async def run(emit: Any) -> Any:
        return await run_claimed(
            store,
            session,
            engine=engine,
            engine_name=settings.engine,
            user_id=user.sub,
            user_text=payload.text,
            deadline=deadline,
            emit=emit,
        )

    return StreamingResponse(
        stream_turn(run, heartbeat_seconds=settings.heartbeat_seconds),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/sessions/{session_id}", response_model=Transcript)
def get_session(session_id: str, user: CurrentUserDep, deps: DepsDep) -> Transcript:
    session = deps.store.get_agent_session(user.sub, session_id)
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="session_not_found")
    turns = deps.store.list_turns(user.sub, session_id)
    return Transcript(
        session_id=session_id,
        status=session.status,
        turn=session.turn,
        job_id=session.job_id,
        turns=[_transcript_turn(t) for t in turns],
    )


@router.post("/sessions/{session_id}/abandon", status_code=status.HTTP_204_NO_CONTENT)
def abandon_session(session_id: str, user: CurrentUserDep, deps: DepsDep) -> Response:
    session = deps.store.get_agent_session(user.sub, session_id)
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="session_not_found")
    if session.status is AgentSessionStatus.FINALIZED:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="already_finalized")
    deps.store.mark_agent_session(user.sub, session_id, AgentSessionStatus.ABANDONED)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ----------------------------------------------------------------------
# Memory
# ----------------------------------------------------------------------


@router.get("/memory", response_model=MemoryOut)
def get_memory(user: CurrentUserDep, deps: DepsDep) -> MemoryOut:
    memory = deps.store.get_memory(user.sub)
    return MemoryOut(
        insights=[InsightOut(text=i.text, created_at=i.created_at) for i in memory.insights]
    )


@router.delete("/memory", status_code=status.HTTP_204_NO_CONTENT)
def clear_memory(user: CurrentUserDep, deps: DepsDep) -> Response:
    deps.store.clear_memory(user.sub)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ----------------------------------------------------------------------


def _transcript_turn(turn: AgentTurn) -> TranscriptTurn:
    """What the client re-renders: the words both sides said and which
    tools ran. Tool inputs and outputs stay on the item."""
    tools = [
        block["toolUse"]["name"]
        for tool_round in turn.tool_calls
        for block in tool_round.get("assistant_content", [])
        if "toolUse" in block
    ]
    text = "".join(block["text"] for block in turn.assistant_content if "text" in block)
    return TranscriptTurn(
        turn=turn.turn,
        user_text=turn.user_text,
        assistant_text=text,
        tools=tools,
        created_at=turn.created_at,
    )
