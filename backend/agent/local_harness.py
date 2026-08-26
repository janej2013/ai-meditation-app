"""The terminal drivers' loop over ``agent_runner.turns``.

``smoke.py`` and ``cli.py`` drive a session turn by turn through exactly
the code the runner uses per request -- claim, rebuild, run, commit or
release -- so a conversation on a laptop and one over SSE cannot differ
in what they persist. This module only adds the loop and the stop rules.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from typing import Any

from agent.contracts import AgentEngine, Deadline, Emit, TurnResult
from agent.native.llm.converse import BedrockConverseProvider
from agent.native.loop import NativeEngine
from agent.tools.default import default_registry
from agent.tools.registry import ToolContext
from agent_runner.turns import (
    ConfirmRefusedError,
    SessionExhaustedError,
    TurnFailureError,
    confirm_session,
    execute_turn,
)
from shared.db import EntitlementStore
from shared.models import AgentEngineName

# What a turn may take locally: the Lambda budget (120 s) less a margin,
# so local behaviour matches what the runner enforces.
TURN_SECONDS = 110


class DryRunStepFunctions:
    """Prints what would be started instead of starting it."""

    def start_execution(self, **kwargs: Any) -> dict[str, Any]:
        print(f"[dry-run] start_execution name={kwargs['name']} input={kwargs['input']}")
        return {"executionArn": "dry-run"}


def engine_for(
    engine_name: AgentEngineName, context: ToolContext, *, model_id: str | None = None
) -> tuple[AgentEngine, str]:
    """The engine a local driver asked for, and the model id it runs on.
    ``model_id`` overrides ``AGENT_MODEL_ID`` for a session of debugging."""
    if engine_name == "langgraph":
        from agent.langgraph.engine import LangGraphEngine, chat_model_from_env

        model = chat_model_from_env(model_id=model_id)
        return LangGraphEngine(model, default_registry(), context), model.model_id
    provider = BedrockConverseProvider(model_id) if model_id else BedrockConverseProvider.from_env()
    return NativeEngine(provider, default_registry(), context), provider.model_id


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
    confirm: Callable[[int], bool] | None = None,
    sfn: Any = None,
    engine_name: AgentEngineName = "native",
) -> str | None:
    """Drive an existing ACTIVE session through ``turns`` until a proposal
    is confirmed or the input runs out. Returns the job id when confirmed.

    ``confirm`` plays the listener's part when the model proposes: it gets
    the duration and answers whether to start (the CLI asks, smoke says
    yes). A failed turn (``TurnFailureError``) is reported and the loop
    continues with the next input, as a user would resend; the claim was
    released.
    """
    for user_text in turns:
        try:
            outcome = asyncio.run(
                execute_turn(
                    store,
                    engine=engine,
                    engine_name=engine_name,
                    user_id=user_id,
                    session_id=session_id,
                    user_text=user_text,
                    deadline=Deadline.after(turn_seconds),
                    emit=emit,
                )
            )
        except SessionExhaustedError:
            return None
        except TurnFailureError as exc:
            print(f"\n  [turn failed: {exc.code}; resend to retry]")
            continue
        if on_turn is not None:
            on_turn(outcome.turn - 1, outcome.result)
        if outcome.job_id:
            return outcome.job_id
        proposal = outcome.result.proposal
        if proposal is not None and confirm is not None and confirm(proposal.duration_minutes):
            try:
                return asyncio.run(
                    confirm_session(
                        store, sfn, user_id=user_id, session_id=session_id, engine_name=engine_name
                    )
                )
            except ConfirmRefusedError as exc:
                print(f"\n  [could not start: {exc.code}]")
    return None
