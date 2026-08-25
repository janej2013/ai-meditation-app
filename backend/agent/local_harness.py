"""The claim / rebuild / run / commit sequence, for local drivers.

This is the harness's core loop before there is a harness: ``smoke.py``
and ``cli.py`` both drive a session through it. A4 moves exactly this
sequence into ``agent_runner`` (one call per turn, the claim and commit
around the engine, the checkpoint in between) and adds transport, JWT and
metrics around it. Until then it lives here so the two local drivers cannot
drift from each other.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any

from agent.budget import MAX_TURNS
from agent.checkpoint import TurnCheckpoint, rebuild_messages
from agent.contracts import AgentEngine, Deadline, Emit, TurnInput, TurnResult
from agent.prompt import render_memory_block
from shared.db import EntitlementStore
from shared.models import AgentSessionStatus

# What a turn may take locally: the Lambda budget (120 s) less a margin,
# so local behaviour matches what the runner will enforce.
TURN_SECONDS = 110


class DryRunStepFunctions:
    """Prints what would be started instead of starting it."""

    def start_execution(self, **kwargs: Any) -> dict[str, Any]:
        print(f"[dry-run] start_execution name={kwargs['name']} input={kwargs['input']}")
        return {"executionArn": "dry-run"}


def run_conversation(
    store: EntitlementStore,
    engine: AgentEngine,
    user_id: str,
    session_id: str,
    turns: Iterable[str],
    *,
    emit: Emit,
    on_turn: Callable[[int, TurnResult], None] | None = None,
    turn_seconds: float = TURN_SECONDS,
) -> str | None:
    """Drive an existing ACTIVE session through ``turns`` until it finalizes
    or the input runs out. Returns the job id when finalized.

    Each turn is independent, as it will be on Lambda: claim, rebuild the
    history from the checkpoints, run, commit. The memory block is re-read
    per turn so an insight saved this turn is in the prompt next turn.
    """
    import asyncio

    for user_text in turns:
        session = store.claim_turn(user_id, session_id, engine="native", now=datetime.now(UTC))
        if session.turn >= MAX_TURNS:
            store.mark_agent_session(user_id, session_id, AgentSessionStatus.ABANDONED)
            return None
        history = rebuild_messages(store.list_turns(user_id, session_id))
        memory = render_memory_block([i.text for i in store.get_memory(user_id).insights])
        result = asyncio.run(
            engine.run_turn(
                TurnInput(
                    history=history, user_text=user_text, turn=session.turn, memory_block=memory
                ),
                deadline=Deadline.after(turn_seconds),
                emit=emit,
            )
        )
        checkpoint = TurnCheckpoint.from_result(
            session_id=session_id, turn=session.turn, user_text=user_text, result=result
        )
        job_id = result.finalized.job_id if result.finalized else None
        committed = store.commit_turn(
            user_id,
            session_id,
            expected_turn=session.turn,
            checkpoint=checkpoint,
            finalized_job_id=job_id,
        )
        if not committed:
            raise RuntimeError(f"turn {session.turn} was not committed; the claim was lost")
        if on_turn is not None:
            on_turn(session.turn, result)
        if job_id:
            return job_id
    return None
