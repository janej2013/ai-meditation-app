"""Talk to the companion from a terminal, against the real model and the
real dev table.

    python -m agent.cli --user-id <cognito_sub> [--model-id <id>] [--dry-run]

WHAT IT COSTS: every reply is a Bedrock call; finalizing starts a real
generation (one credit frozen and spent, TTS run) unless --dry-run, which
writes the JOB and AGENT rows but starts no execution. Run it by hand, on
dev, for a test user (CLAUDE.md constraint 8).

Environment: TABLE_NAME, STATE_MACHINE_ARN (Data and Pipeline stack
outputs), AGENT_MODEL_ID (optional; a bare Amazon id or an au. Claude
profile), AWS credentials for the dev account.

Your own words and the model's replies are printed to the terminal; the
logs carry ids and counts only.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import uuid
from collections.abc import Iterator
from functools import partial

import boto3

from agent.contracts import AgentEvent, TextDelta, ToolStarted, TurnResult
from agent.local_harness import DryRunStepFunctions, run_conversation
from agent.native.llm.converse import BedrockConverseProvider
from agent.native.loop import NativeEngine
from agent.tools.default import default_registry
from agent.tools.registry import ToolContext
from shared.db import EntitlementStore
from shared.jobs import start_generation
from shared.models import AgentSessionStatus

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

QUIT = "/quit"


async def _print_event(event: AgentEvent) -> None:
    if isinstance(event, TextDelta):
        print(event.text, end="", flush=True)
    elif isinstance(event, ToolStarted):
        print(f"\n[tool: {event.name}]", flush=True)


def _prompt_lines() -> Iterator[str]:
    """User turns from stdin; ends on EOF, an empty line is skipped."""
    while True:
        try:
            line = input("\nyou> ").strip()
        except EOFError:
            return
        if line == QUIT:
            return
        if line:
            print("companion> ", end="", flush=True)
            yield line


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--user-id", required=True, help="Cognito sub of a dev test user")
    parser.add_argument("--model-id", help="overrides AGENT_MODEL_ID")
    parser.add_argument("--dry-run", action="store_true", help="write rows, start no execution")
    args = parser.parse_args(argv)

    for var in ("TABLE_NAME", "STATE_MACHINE_ARN"):
        if not os.environ.get(var):
            print(f"{var} is not set", file=sys.stderr)
            return 2

    provider = (
        BedrockConverseProvider(args.model_id)
        if args.model_id
        else BedrockConverseProvider.from_env()
    )
    store = EntitlementStore()
    sfn = DryRunStepFunctions() if args.dry_run else boto3.client("stepfunctions")
    session_id = str(uuid.uuid4())
    context = ToolContext(
        user_id=args.user_id,
        session_id=session_id,
        store=store,
        start_generation=partial(start_generation, store, sfn),
    )
    engine = NativeEngine(provider, default_registry(), context)

    if not store.create_agent_session(
        args.user_id, session_id, engine="native", model_id=provider.model_id
    ):
        print("could not create the session", file=sys.stderr)
        return 1
    print(f"session {session_id} model {provider.model_id} (type {QUIT} to leave)")

    def report(turn: int, result: TurnResult) -> None:
        u = result.usage
        print(
            f"\n  [turn {turn}: in={u.input_tokens} out={u.output_tokens} "
            f"cache_read={u.cache_read_tokens} tools={[r.name for r in result.tool_log]}]"
        )

    def ask_to_start(minutes: int) -> bool:
        answer = input(f"\nstart a {minutes}-minute meditation? [y/N] ").strip().lower()
        return answer == "y"

    job_id = run_conversation(
        store,
        engine,
        args.user_id,
        session_id,
        _prompt_lines(),
        emit=_print_event,
        on_turn=report,
        confirm=ask_to_start,
        sfn=sfn,
    )
    if job_id:
        print(f"\nstarted: job {job_id}")
        return 0
    store.mark_agent_session(args.user_id, session_id, AgentSessionStatus.ABANDONED)
    print("\nsession abandoned")
    return 0


if __name__ == "__main__":
    sys.exit(main())
