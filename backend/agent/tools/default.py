"""The tools every companion session offers, in their fixed order.

Order matters twice: it is the order the model reads them in, and it is part
of the prompt-cache prefix (docs/agent-runner-plan.md §3.1). Add a tool at
the end.
"""

from __future__ import annotations

from agent.tools import finalize, history, memory
from agent.tools.registry import ToolRegistry


def default_registry() -> ToolRegistry:
    return ToolRegistry([history.SPEC, memory.SPEC, finalize.SPEC])
