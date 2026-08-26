"""Test-only tools shared by the engine scenarios: the same registry the
native loop's tests use, importable by the contract tests without
importing a test module."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from agent.budget import FINALIZE_TOOL_NAME
from agent.contracts import AgentEvent, Finalized, Proposal
from agent.tools.registry import ToolContext, ToolOutcome, ToolRegistry, ToolSpec

USER = ToolContext(user_id="user-1", session_id="sess-1")


class NoopIn(BaseModel):
    note: str = ""


class StrictIn(BaseModel):
    count: int


class FinishIn(BaseModel):
    brief: str


class ProposeIn(BaseModel):
    minutes: int = 5


async def noop(ctx: ToolContext, inp: NoopIn) -> ToolOutcome:
    return ToolOutcome(content={"ok": True, "note": inp.note})


async def boom(ctx: ToolContext, inp: NoopIn) -> ToolOutcome:
    raise RuntimeError("the input was: " + inp.note)


async def strict(ctx: ToolContext, inp: StrictIn) -> ToolOutcome:
    return ToolOutcome(content={"count": inp.count})


async def finish(ctx: ToolContext, inp: FinishIn) -> ToolOutcome:
    return ToolOutcome(content={"job_id": "job-1"}, finalized=Finalized(job_id="job-1"))


async def sneaky_finish(ctx: ToolContext, inp: NoopIn) -> ToolOutcome:
    # Not registered as terminal: its finalized must be ignored.
    return ToolOutcome(content="done", finalized=Finalized(job_id="job-x"))


async def propose(ctx: ToolContext, inp: ProposeIn) -> ToolOutcome:
    return ToolOutcome(
        content={"status": "awaiting_confirmation"}, proposal=Proposal(duration_minutes=inp.minutes)
    )


def registry() -> ToolRegistry:
    return ToolRegistry(
        [
            ToolSpec("noop", "Does nothing.", NoopIn, noop),
            ToolSpec("boom", "Raises.", NoopIn, boom),
            ToolSpec("strict", "Wants an int.", StrictIn, strict),
            ToolSpec(FINALIZE_TOOL_NAME, "Ends the session.", FinishIn, finish, terminal=True),
            ToolSpec("sneaky", "Claims to finalize.", NoopIn, sneaky_finish),
            ToolSpec("propose", "Proposes.", ProposeIn, propose),
        ]
    )


class Collector:
    def __init__(self) -> None:
        self.events: list[AgentEvent] = []

    async def __call__(self, event: Any) -> None:
        self.events.append(event)
