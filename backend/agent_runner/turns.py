"""One turn, made durable: claim, rebuild, run, commit -- or release.

This is the harness's core and knows nothing about HTTP; the routes wrap
it in a response and ``agent.local_harness`` wraps it in a terminal loop,
so the two cannot drift. The store's fencing token is exercised in full
here: ``claim_turn`` before the engine runs, ``commit_turn`` after, and
``release_turn`` on any failure so the user can retry at once instead of
waiting out the stale-claim window.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import Any

from agent.budget import MAX_TURNS
from agent.checkpoint import TurnCheckpoint, rebuild_messages
from agent.contracts import AgentEngine, Deadline, Emit, TurnInput, TurnResult
from agent.native.llm.converse import AgentProviderError
from agent.native.loop import NativeEngine
from agent.prompt import render_memory_block
from agent.tools.default import default_registry
from agent.tools.registry import ToolContext
from agent_runner.metrics import emit_metrics
from shared.db import EntitlementStore
from shared.jobs import start_generation
from shared.models import AgentEngineName, AgentSession, AgentSessionStatus

logger = logging.getLogger(__name__)


class SessionExhaustedError(Exception):
    """The session has used its MAX_TURNS; it has been marked ABANDONED."""


class TurnFailureError(Exception):
    """The turn did not complete. ``code`` is what the client sees;
    ``retryable`` says whether resending the same message makes sense."""

    def __init__(self, code: str, *, retryable: bool = True) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class TurnOutcome:
    turn: int
    result: TurnResult
    job_id: str | None


async def claim_turn(
    store: EntitlementStore, *, user_id: str, session_id: str, engine_name: AgentEngineName
) -> AgentSession:
    """Take the session for one turn. Raises ``AgentTurnBusyError`` when
    another invocation holds it (or it is closed), ``SessionExhaustedError``
    when the last turn has already been spent."""
    session = await asyncio.to_thread(
        store.claim_turn, user_id, session_id, engine=engine_name, now=datetime.now(UTC)
    )
    if session.turn >= MAX_TURNS:
        # mark_agent_session also drops the claim we just took.
        await asyncio.to_thread(
            store.mark_agent_session, user_id, session_id, AgentSessionStatus.ABANDONED
        )
        raise SessionExhaustedError(session_id)
    return session


def build_engine(
    *,
    engine_name: AgentEngineName,
    provider: Any,
    store: EntitlementStore,
    sfn: Any,
    user_id: str,
    session_id: str,
    clock: Callable[[], datetime] | None = None,
) -> AgentEngine:
    """The engine for one request. Cheap: the provider is the only thing
    worth reusing, and it is passed in."""
    if engine_name != "native":
        raise NotImplementedError("the langgraph engine arrives with milestone L1")
    context = ToolContext(
        user_id=user_id,
        session_id=session_id,
        store=store,
        start_generation=partial(start_generation, store, sfn),
        **({"now": clock} if clock else {}),
    )
    return NativeEngine(provider, default_registry(), context)


async def run_claimed(
    store: EntitlementStore,
    session: AgentSession,
    *,
    engine: AgentEngine,
    engine_name: AgentEngineName,
    user_id: str,
    user_text: str,
    deadline: Deadline,
    emit: Emit,
) -> TurnOutcome:
    """Run and commit a turn on a session this invocation has claimed.

    Any failure releases the claim before re-raising as ``TurnFailureError``:
    nothing was committed, ``turn`` did not move, and the user may resend.
    The ``Done`` event is the caller's to send -- after this returns, and
    therefore after the commit.
    """
    session_id = session.session_id
    started = time.monotonic()
    try:
        turns = await asyncio.to_thread(store.list_turns, user_id, session_id)
        memory = await asyncio.to_thread(store.get_memory, user_id)
        result = await engine.run_turn(
            TurnInput(
                history=rebuild_messages(turns),
                user_text=user_text,
                turn=session.turn,
                memory_block=render_memory_block([i.text for i in memory.insights]),
            ),
            deadline=deadline,
            emit=emit,
        )
        checkpoint = TurnCheckpoint.from_result(
            session_id=session_id, turn=session.turn, user_text=user_text, result=result
        )
        job_id = result.finalized.job_id if result.finalized else None
        committed = await asyncio.to_thread(
            store.commit_turn,
            user_id,
            session_id,
            expected_turn=session.turn,
            checkpoint=checkpoint,
            finalized_job_id=job_id,
        )
        if not committed:
            # The claim was ours, so only a takeover after a stale window
            # explains this -- the turn ran far past its budget.
            raise RuntimeError("commit rejected: the claim was taken over")
    except AgentProviderError as exc:
        await _release(store, user_id, session_id, session.turn)
        _record_failure(engine_name, session_id, session.turn, "model_unavailable")
        # The provider's message names the Bedrock error and the rejected
        # parameter, never the prompt (shared/pipeline.raise_for_bedrock_error).
        logger.warning(
            "turn failed: model unavailable: %s",
            exc,
            extra={"session_id": session_id, "turn": session.turn},
        )
        raise TurnFailureError("model_unavailable") from exc
    except Exception as exc:
        await _release(store, user_id, session_id, session.turn)
        _record_failure(engine_name, session_id, session.turn, "internal")
        logger.exception("turn failed: %s", type(exc).__name__, extra={"session_id": session_id})
        raise TurnFailureError("internal") from exc

    elapsed_ms = int((time.monotonic() - started) * 1000)
    emit_metrics(
        dimensions={"Engine": engine_name},
        metrics={
            "AgentTurns": (1, "Count"),
            "TurnLatency": (elapsed_ms, "Milliseconds"),
            "InputTokens": (result.usage.input_tokens, "Count"),
            "OutputTokens": (result.usage.output_tokens, "Count"),
            "CacheReadTokens": (result.usage.cache_read_tokens, "Count"),
            "ToolErrors": (sum(r.status == "error" for r in result.tool_log), "Count"),
        },
        properties={"session_id": session_id, "turn": session.turn},
    )
    logger.info(
        "turn committed tools=%d finalized=%s ms=%d",
        len(result.tool_log),
        job_id is not None,
        elapsed_ms,
        extra={"session_id": session_id, "turn": session.turn},
    )
    return TurnOutcome(turn=session.turn + 1, result=result, job_id=job_id)


async def execute_turn(
    store: EntitlementStore,
    *,
    engine: AgentEngine,
    engine_name: AgentEngineName,
    user_id: str,
    session_id: str,
    user_text: str,
    deadline: Deadline,
    emit: Emit,
) -> TurnOutcome:
    """Claim then run: the whole turn, for callers without a response to
    split it around (the local drivers)."""
    session = await claim_turn(
        store, user_id=user_id, session_id=session_id, engine_name=engine_name
    )
    return await run_claimed(
        store,
        session,
        engine=engine,
        engine_name=engine_name,
        user_id=user_id,
        user_text=user_text,
        deadline=deadline,
        emit=emit,
    )


async def _release(store: EntitlementStore, user_id: str, session_id: str, turn: int) -> None:
    try:
        released = await asyncio.to_thread(
            store.release_turn, user_id, session_id, expected_turn=turn
        )
    except Exception:
        logger.exception("release failed", extra={"session_id": session_id, "turn": turn})
        return
    if not released:
        logger.warning("release skipped: claim no longer ours", extra={"session_id": session_id})


def _record_failure(engine_name: str, session_id: str, turn: int, reason: str) -> None:
    emit_metrics(
        dimensions={"Engine": engine_name},
        metrics={"AgentTurnErrors": (1, "Count")},
        properties={"session_id": session_id, "turn": turn, "reason": reason},
    )
