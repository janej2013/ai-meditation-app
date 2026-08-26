"""Run the eval cases against the real model.

    python -m tests.agent.evals.run [--engine native|langgraph] [--model-id ID]
                                    [--only NAME] [--json OUT]

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
from collections.abc import Callable
from dataclasses import asdict, dataclass, field

from agent.budget import FINALIZE_TOOL_NAME
from agent.checkpoint import TurnCheckpoint, rebuild_messages
from agent.contracts import (
    AgentEngine,
    AgentEvent,
    Deadline,
    Emit,
    TextDelta,
    ToolStarted,
    TurnInput,
)
from agent.native.llm.converse import BedrockConverseProvider
from agent.native.loop import NativeEngine
from agent.prompt import render_memory_block
from agent.tools.default import default_registry
from agent.tools.registry import ToolContext
from shared.models import AgentTurn

from .cases import CASES, Case
from .eval_store import EvalStore


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


MakeEngine = Callable[[ToolContext], AgentEngine]


def engine_factory(engine_name: str, model_id: str | None) -> tuple[MakeEngine, str]:
    """One model, reused across cases; an engine per turn, as production
    builds one per request."""
    if engine_name == "langgraph":
        from agent.langgraph.engine import LangGraphEngine, chat_model_from_env

        model = chat_model_from_env(model_id=model_id)
        return (lambda ctx: LangGraphEngine(model, default_registry(), ctx)), model.model_id
    provider = BedrockConverseProvider(model_id) if model_id else BedrockConverseProvider.from_env()
    return (lambda ctx: NativeEngine(provider, default_registry(), ctx)), provider.model_id


def run_case(case: Case, make_engine: MakeEngine) -> Outcome:
    store = EvalStore(available=case.available, insights=case.insights)
    context = ToolContext(user_id="eval", session_id=f"eval-{case.name}", store=store)
    turns: list[AgentTurn] = []
    replies: list[str] = []
    tools: list[str] = []
    finalized_on: int | None = None
    usage_in = usage_out = cache_read = 0
    latencies: list[float] = []

    for turn, user_text in enumerate(case.turns):
        text: list[str] = []
        emit = _collect_into(text, tools)
        engine = make_engine(context)
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
        if result.proposal is not None:
            # A proposal is the conversation's end from the model's side; the
            # listener's confirmation is the app's business, not the model's.
            finalized_on = turn + 1
            break

    reasons = judge(case, replies, tools, finalized_on, store)
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
    store: EvalStore,
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
    if e.brief_must_not_contain and store.pending:
        brief = store.pending[0].casefold()
        leaked = [w for w in e.brief_must_not_contain if w.casefold() in brief]
        if leaked:
            reasons.append(f"brief contains {leaked}")
    if e.duration_range and store.pending:
        lo, hi = e.duration_range
        got = store.pending[1]
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
    parser.add_argument(
        "--engine", choices=("native", "langgraph"), default="native", help="the engine under test"
    )
    parser.add_argument("--only", help="run one case by name")
    parser.add_argument("--json", help="write full outcomes (including replies) to this file")
    args = parser.parse_args(argv)

    make_engine, model_id = engine_factory(args.engine, args.model_id)
    cases = [c for c in CASES if not args.only or c.name == args.only]
    if not cases:
        print(f"no case named {args.only!r}", file=sys.stderr)
        return 2

    outcomes = [run_case(case, make_engine) for case in cases]

    print(f"engine {args.engine} model {model_id}")
    print(_row("case", "pass", "turns", "fin", "tokens in/out", "cache%", "ms", "reasons"))
    for o in outcomes:
        # Bedrock's inputTokens excludes what was served from cache.
        total_in = o.input_tokens + o.cache_read_tokens
        cache = f"{100 * o.cache_read_tokens // max(total_in, 1)}%"
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
    soft = {c.name for c in cases if c.expect.soft}
    hard_failures = [o for o in outcomes if not o.passed and o.name not in soft]
    soft_failures = [o for o in outcomes if not o.passed and o.name in soft]
    passed = sum(o.passed for o in outcomes)
    print(f"\n{passed}/{len(outcomes)} passed", end="")
    if soft_failures:
        print(
            f" ({len(soft_failures)} soft failure(s) not counted: "
            f"{', '.join(o.name for o in soft_failures)})",
            end="",
        )
    print()

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump([asdict(o) for o in outcomes], fh, indent=2, ensure_ascii=False)
    return 0 if not hard_failures else 1


if __name__ == "__main__":
    sys.exit(main())
