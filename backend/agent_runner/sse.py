"""Server-sent events for one turn.

The engine emits as it goes; this module puts those events on the wire in
the shape the PWA parses (docs/agent-runner-plan.md §5, §8), keeps the
connection alive with a comment line while the model is thinking, and
ends with ``done`` only once the turn is committed.

A client that disconnects does not cancel the turn. A turn is atomic --
half of one would leave the claim held until it goes stale -- so the work
runs to its commit either way and the transcript shows it on the next
read. The response generator waits for that to happen before it closes,
which on Lambda is also what keeps the invocation alive long enough.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

from agent.contracts import AgentEvent, Emit, ProposalReady, TextDelta, ToolStarted
from agent_runner.turns import TurnFailureError, TurnOutcome

logger = logging.getLogger(__name__)

PING = b": ping\n\n"


def encode(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


@dataclass(frozen=True)
class _Finished:
    outcome: TurnOutcome | None = None
    code: str | None = None
    retryable: bool = True


RunTurn = Callable[[Emit], Awaitable[TurnOutcome]]


async def stream_turn(run_turn: RunTurn, *, heartbeat_seconds: float) -> AsyncIterator[bytes]:
    """Run ``run_turn`` with an ``emit`` that feeds this stream.

    Events: ``delta`` and ``tool`` as they happen, ``: ping`` after
    ``heartbeat_seconds`` of silence, then exactly one of ``done`` (turn
    committed) or ``error`` (nothing committed; the claim was released).
    """
    queue: asyncio.Queue[AgentEvent | _Finished] = asyncio.Queue()

    async def emit(event: AgentEvent) -> None:
        await queue.put(event)

    async def runner() -> None:
        try:
            outcome = await run_turn(emit)
        except TurnFailureError as exc:
            await queue.put(_Finished(code=exc.code, retryable=exc.retryable))
        except Exception:
            logger.exception("turn runner crashed")
            await queue.put(_Finished(code="internal"))
        else:
            await queue.put(_Finished(outcome=outcome))

    task = asyncio.create_task(runner())
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=heartbeat_seconds)
            except TimeoutError:
                yield PING
                continue
            if isinstance(item, _Finished):
                if item.outcome is not None:
                    yield encode(
                        "done",
                        {
                            "turn": item.outcome.turn,
                            "job_id": item.outcome.job_id,
                            "awaiting_confirmation": item.outcome.awaiting_confirmation,
                        },
                    )
                else:
                    yield encode("error", {"code": item.code, "retryable": item.retryable})
                return
            if isinstance(item, TextDelta):
                yield encode("delta", {"text": item.text})
            elif isinstance(item, ToolStarted):
                yield encode("tool", {"name": item.name})
            elif isinstance(item, ProposalReady):
                yield encode("proposal", {"duration_minutes": item.duration_minutes})
    finally:
        # Reached early only when the client went away. Let the turn finish
        # and commit; a cancellation aimed at this generator must not reach
        # the task, hence the shield.
        while not task.done():
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(task)
