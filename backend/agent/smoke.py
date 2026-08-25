"""Manual end-to-end check of the terminal path against a real dev table
and the real generation state machine.

    python -m agent.smoke --user-id <cognito_sub> --duration 3 [--dry-run]

WHAT IT COSTS: without --dry-run this starts a real generation for the
given user -- one credit is frozen and then spent, Bedrock and TTS run.
Run it by hand, on dev, for a test user (CLAUDE.md constraint 8). With
--dry-run the JOB row and the AGENT items are still written, but no
execution is started and nothing is charged.

Environment: TABLE_NAME and STATE_MACHINE_ARN, from the Data and Pipeline
stack outputs (README, "Calling the deployed API"), plus AWS credentials
for the dev account.

The model is a scripted stand-in (no Bedrock): three turns that look up
history, save an insight and finalize a fixed, impersonal brief. A3 swaps
the stand-in for BedrockConverseProvider at the one marked line. The
claim / run / commit sequence is the harness's job from A4 on; it is
inlined here until then.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from functools import partial
from typing import Any

import boto3

from agent.checkpoint import TurnCheckpoint, rebuild_messages
from agent.contracts import (
    AgentEvent,
    ConverseToolSpec,
    Deadline,
    Final,
    LLMEvent,
    Message,
    SystemBlock,
    TextBlock,
    ToolChoice,
    ToolStarted,
    ToolUseBlock,
    ToolUseStart,
    TurnInput,
    Usage,
)
from agent.native.loop import NativeEngine
from agent.tools.default import default_registry
from agent.tools.registry import ToolContext
from shared.db import EntitlementStore
from shared.jobs import start_generation

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

# Impersonal on purpose: nothing here is about anyone.
BRIEF = (
    "A short evening meditation for someone carrying the tension of a long day. "
    "Speak to the feeling of wanting to set things down. Slow pacing, long pauses, "
    "imagery of a quiet shoreline at dusk. Avoid counting exercises."
)

# (user text, model behaviour): what the scripted model does on each turn.
SCRIPT: list[tuple[str, list[tuple[str, dict[str, Any]]], str]] = [
    (
        "I'd like something to help me wind down tonight.",
        [("get_session_history", {"limit": 3})],
        "Welcome back.",
    ),
    (
        "Slow, please. I always find fast narration stressful.",
        [("save_user_insight", {"insight": "prefers slow narration"})],
        "Noted.",
    ),
    ("Yes, that sounds right. Go ahead.", [], ""),  # finalize: duration filled in at runtime
]


class ScriptedProvider:
    """Plays one tool call per turn, then a line of text. Never reads the
    messages -- this is a stand-in for the transport, not for the model."""

    def __init__(self, duration_minutes: int) -> None:
        self._turn = 0
        self._pending_text: str | None = None
        self._duration = duration_minutes

    async def stream_turn(
        self,
        system: list[SystemBlock],  # noqa: ARG002
        messages: list[Message],  # noqa: ARG002
        tools: list[ConverseToolSpec],  # noqa: ARG002
        *,
        tool_choice: ToolChoice | None,  # noqa: ARG002
    ) -> AsyncIterator[LLMEvent]:
        if self._pending_text is not None:
            text, self._pending_text = self._pending_text, None
            yield Final(content=[TextBlock(text=text)], stop_reason="end_turn", usage=Usage())
            return
        _, calls, text = SCRIPT[self._turn]
        self._turn += 1
        if not calls:
            calls = [
                (
                    "finalize_meditation_brief",
                    {"brief": BRIEF, "duration_minutes": self._duration},
                )
            ]
        self._pending_text = text
        content: list[Any] = []
        for name, inp in calls:
            tool_use_id = f"tu-{uuid.uuid4().hex[:8]}"
            yield ToolUseStart(name=name, tool_use_id=tool_use_id)
            content.append(ToolUseBlock(tool_use_id=tool_use_id, name=name, input=inp))
        yield Final(content=content, stop_reason="tool_use", usage=Usage())


class DryRunStepFunctions:
    """Prints what would be started instead of starting it."""

    def start_execution(self, **kwargs: Any) -> dict[str, Any]:
        print(f"[dry-run] start_execution name={kwargs['name']} input={kwargs['input']}")
        return {"executionArn": "dry-run"}


async def _print_event(event: AgentEvent) -> None:
    # Tool names only; text from the scripted model is not worth echoing.
    if isinstance(event, ToolStarted):
        print(f"  tool: {event.name}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--user-id", required=True, help="Cognito sub of a dev test user with credit"
    )
    parser.add_argument("--duration", type=int, default=3, help="meditation length in minutes")
    parser.add_argument("--dry-run", action="store_true", help="write the rows, start no execution")
    args = parser.parse_args(argv)

    for var in ("TABLE_NAME", "STATE_MACHINE_ARN"):
        if not os.environ.get(var):
            print(f"{var} is not set", file=sys.stderr)
            return 2

    store = EntitlementStore()
    sfn = DryRunStepFunctions() if args.dry_run else boto3.client("stepfunctions")
    session_id = str(uuid.uuid4())
    context = ToolContext(
        user_id=args.user_id,
        session_id=session_id,
        store=store,
        start_generation=partial(start_generation, store, sfn),
    )
    # A3: replace ScriptedProvider with BedrockConverseProvider(...) here.
    engine = NativeEngine(ScriptedProvider(args.duration), default_registry(), context)

    if not store.create_agent_session(
        args.user_id, session_id, engine="native", model_id="scripted"
    ):
        print("could not create the session", file=sys.stderr)
        return 1
    print(f"session {session_id}")

    for turn, (user_text, _, _) in enumerate(SCRIPT):
        session = store.claim_turn(args.user_id, session_id, engine="native", now=datetime.now(UTC))
        history = rebuild_messages(store.list_turns(args.user_id, session_id))
        print(f"turn {turn} (claimed at turn={session.turn})")
        result = asyncio.run(
            engine.run_turn(
                TurnInput(history=history, user_text=user_text, turn=turn),
                deadline=Deadline.after(110),
                emit=_print_event,
            )
        )
        checkpoint = TurnCheckpoint.from_result(
            session_id=session_id, turn=turn, user_text=user_text, result=result
        )
        job_id = result.finalized.job_id if result.finalized else None
        committed = store.commit_turn(
            args.user_id,
            session_id,
            expected_turn=turn,
            checkpoint=checkpoint,
            finalized_job_id=job_id,
        )
        print(
            f"  committed={committed} tools={[r.name for r in result.tool_log]} finalized={job_id}"
        )
        if job_id:
            print(json.dumps({"session_id": session_id, "job_id": job_id}))
            return 0

    print("the script ended without finalizing", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
