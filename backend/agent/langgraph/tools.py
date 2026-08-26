"""Our tools, as the graph binds them and as it runs them.

Binding: ``bind_tools`` is handed the registry's own Converse ``toolSpec``
dicts, which langchain-aws passes through untouched, so the schema the
model reads is byte-for-byte the native one. The framework's own road --
``StructuredTool.from_function(args_schema=Model)`` -- goes through
``convert_to_openai_tool``, which strips Pydantic's ``title`` keys, and
the two requests stop being equal; ``tests/agent/test_langgraph_messages``
keeps that difference on record (docs/agent-runner-plan.md §3.4).

Running: calls go through ``ToolRegistry.execute`` -- validation messages,
the unknown-tool answer, exceptions turned into error results, proposals
and finalization all come from the one implementation -- and the runner
keeps the turn's rounds and log for the engine to hand to the harness.
That is also why the graph has its own tools node rather than ``ToolNode``:
it validates against the schema itself and words its errors its own way,
so the transcript would diverge from the native engine's on exactly the
paths that matter for a retry.
"""

from __future__ import annotations

import asyncio
from typing import Any

from agent.contracts import (
    ContentBlock,
    Finalized,
    Proposal,
    ToolCallRecord,
    ToolRound,
    ToolUseBlock,
)
from agent.tools.registry import ToolContext, ToolExecution, ToolRegistry


def bound_tools(registry: ToolRegistry) -> list[dict[str, Any]]:
    """What ``bind_tools`` receives: the registry's Converse specs, in the
    registry's order (the order is part of the prompt-cache prefix)."""
    return registry.to_converse_spec()


class ToolRunner:
    """One turn's tool executions, in the order the graph ran them."""

    def __init__(self, registry: ToolRegistry, context: ToolContext) -> None:
        self._registry = registry
        self._context = context
        self.rounds: list[ToolRound] = []
        self.tool_log: list[ToolCallRecord] = []
        self.proposal: Proposal | None = None
        self.finalized: Finalized | None = None

    async def run(
        self, assistant_content: list[ContentBlock], calls: list[ToolUseBlock]
    ) -> list[ToolExecution]:
        """Every call of one assistant message, in parallel, recorded as one
        round -- the same shape the native loop produces."""
        executions = await asyncio.gather(
            *(self._registry.execute(self._context, block) for block in calls)
        )
        self.rounds.append(
            ToolRound(assistant_content=assistant_content, results=[e.result for e in executions])
        )
        self.tool_log.extend(e.record for e in executions)
        for execution in executions:
            if execution.proposal is not None:
                # The last proposal of a turn wins, as in the native loop.
                self.proposal = execution.proposal
        if self.finalized is None:
            self.finalized = next((e.finalized for e in executions if e.finalized), None)
        return list(executions)
