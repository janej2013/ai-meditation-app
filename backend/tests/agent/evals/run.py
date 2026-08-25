"""Run the eval cases against the real model.

    python -m tests.agent.evals.run [--model-id ID] [--only NAME] [--json OUT]

Costs Bedrock calls (roughly 60 model calls for the full set). Run by
hand; paste the table into the PR that changes the prompt.

Every case runs like production: a fresh engine per turn, the history
rebuilt through the checkpoint bridge, the memory block re-rendered. The
store is in-memory (eval_store) and finalize never reaches Step Functions.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import asdict, dataclass, field

from agent.budget import FINALIZE_TOOL_NAME
from agent.checkpoint import TurnCheckpoint, rebuild_messages
from agent.contracts import AgentEvent, Deadline, Emit, TextDelta, ToolStarted, TurnInput
from agent.native.llm.converse import BedrockConverseProvider
from agent.native.loop import NativeEngine
from agent.prompt import render_memory_block
from agent.tools.default import default_registry
from agent.tools.registry import ToolContext
from shared.models import AgentTurn

from .cases import CASES, Case
from .eval_store import EvalStore, FakeStartGeneration


@dataclass
class Outcome:
    name: str
    passed: bool
    reasons: list[str]
    turns: int
    finalized_on: int | None
    tools_called: list[str]
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    mean_latency_ms: int
    replies: list[str] = field(default_factory=list)


def run_case(case: Case, provider: BedrockConverseProvider) -> Outcome:
    store = EvalStore(available=case.available, insights=case.insights)
    starter = FakeStartGeneration(store)
    context = ToolContext(
        user_id="eval", session_id=f"eval-{case.name}", store=store, start_generation=starter
    )
    turns: list[AgentTurn] = []
    replies: list[str] = []
    tools: list[str] = []
    finalized_on: int | None = None
    usage_in = usage_out = cache_read = 0
    latencies: list[float] = []

    for turn, user_text in enumerate(case.turns):
        text: list[str] = []
        emit = _collect_into(text, tools)
        engine = NativeEngine(provider, default_registry(), context)
        memory = render_memory_block([i.text for i in store.get_memory("eval").insights])
        started = time.monotonic()
        result = asyncio.run(
            engine.run_turn(
                TurnInput(
                    history=rebuild_messages(turns),
                    user_text=user_text,
                    turn=turn,
                    memory_block=memory,
                ),
                deadline=Deadline.after(110),
                emit=emit,
            )
        )
        latencies.append((time.monotonic() - started) * 1000)
        usage_in += result.usage.input_tokens
        usage_out += result.usage.output_tokens
        cache_read += result.usage.cache_read_tokens
        replies.append("".join(text))
        turns.append(
            TurnCheckpoint.from_result(
                session_id=context.session_id, turn=turn, user_text=user_text, result=result
            )
        )
        if result.finalized:
            finalized_on = turn + 1
            break

    reasons = judge(case, replies, tools, finalized_on, starter)
    return Outcome(
        name=case.name,
        passed=not reasons,
        reasons=reasons,
        turns=len(replies),
        finalized_on=finalized_on,
        tools_called=tools,
        input_tokens=usage_in,
        output_tokens=usage_out,
        cache_read_tokens=cache_read,
        mean_latency_ms=int(sum(latencies) / max(len(latencies), 1)),
        replies=replies,
    )


def _collect_into(text: list[str], tools: list[str]) -> Emit:
    async def emit(event: AgentEvent) -> None:
        if isinstance(event, TextDelta):
            text.append(event.text)
        elif isinstance(event, ToolStarted):
            tools.append(event.name)

    return emit


def judge(
    case: Case,
    replies: list[str],
    tools: list[str],
    finalized_on: int | None,
    starter: FakeStartGeneration,
) -> list[str]:
    e = case.expect
    reasons: list[str] = []
    joined = "\n".join(replies)
    for phrase in e.must_contain:
        if phrase not in joined:
            reasons.append(f"missing {phrase!r}")
    for name in e.must_call:
        if name not in tools:
            reasons.append(f"never called {name}")
    for name in e.must_not_call:
        if name in tools:
            reasons.append(f"called {name}")
    if e.must_finalize and finalized_on is None:
        reasons.append("did not finalize")
    if not e.must_finalize and finalized_on is not None and FINALIZE_TOOL_NAME in e.must_not_call:
        reasons.append("finalized")
    if e.max_turns_to_finalize and finalized_on and finalized_on > e.max_turns_to_finalize:
        reasons.append(f"finalized on turn {finalized_on} > {e.max_turns_to_finalize}")
    if e.duration_range and starter.calls:
        lo, hi = e.duration_range
        got = starter.calls[-1]["duration_minutes"]
        if not lo <= got <= hi:
            reasons.append(f"duration {got} outside {lo}-{hi}")
    return reasons


def _row(
    name: str, ok: str, turns: str, fin: str, tokens: str, cache: str, ms: str, why: str
) -> str:
    return f"{name:34} {ok:5} {turns:5} {fin:4} {tokens:14} {cache:6} {ms:6}  {why}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--model-id", help="overrides AGENT_MODEL_ID")
    parser.add_argument("--only", help="run one case by name")
    parser.add_argument("--json", help="write full outcomes (including replies) to this file")
    args = parser.parse_args(argv)

    provider = (
        BedrockConverseProvider(args.model_id)
        if args.model_id
        else BedrockConverseProvider.from_env()
    )
    cases = [c for c in CASES if not args.only or c.name == args.only]
    if not cases:
        print(f"no case named {args.only!r}", file=sys.stderr)
        return 2

    outcomes = [run_case(case, provider) for case in cases]

    print(f"model {provider.model_id}")
    print(_row("case", "pass", "turns", "fin", "tokens in/out", "cache%", "ms", "reasons"))
    for o in outcomes:
        cache = f"{100 * o.cache_read_tokens // max(o.input_tokens, 1)}%"
        print(
            _row(
                o.name,
                "ok" if o.passed else "FAIL",
                str(o.turns),
                str(o.finalized_on or "-"),
                f"{o.input_tokens}/{o.output_tokens}",
                cache,
                str(o.mean_latency_ms),
                "; ".join(o.reasons),
            )
        )
    passed = sum(o.passed for o in outcomes)
    print(f"\n{passed}/{len(outcomes)} passed")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump([asdict(o) for o in outcomes], fh, indent=2, ensure_ascii=False)
    return 0 if passed == len(outcomes) else 1


if __name__ == "__main__":
    sys.exit(main())
