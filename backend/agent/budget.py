"""How long a session may run, and when it is steered to a close.

The numbers are the product's cost gate (docs/agent-runner-plan.md §7): a
conversation is free to have, so its length has to be bounded here rather
than by the credit ledger. Every threshold derives from MAX_TURNS so the
shape of a session is changed in one place.
"""

from __future__ import annotations

from agent.contracts import ForcedTool, ToolChoice

# Turns are 0-based: turn 11 is the twelfth and last.
MAX_TURNS = 12

# Model round-trips within one turn. Four is generous for "look at your
# history, remember something, finalize"; past it the loop asks for a plain
# answer instead of a fifth tool call.
MAX_TOOL_ITERATIONS_PER_TURN = 4

# From the ninth turn the user message carries a converge hint; on the last
# turn the model is not asked but told, via a forced tool choice.
CONVERGE_HINT_FROM_TURN = MAX_TURNS - 4
FORCE_FINALIZE_TURN = MAX_TURNS - 1

# The terminal tool's name, as the forced tool choice must spell it. The
# tool itself is registered in tools/finalize.py (A2); the name lives here so
# the budget does not import the tool.
FINALIZE_TOOL_NAME = "finalize_meditation_brief"


def wants_converge_hint(turn: int) -> bool:
    return turn >= CONVERGE_HINT_FROM_TURN


def converge_policy(turn: int) -> ToolChoice:
    """What the model may do with tools on this turn."""
    if turn >= FORCE_FINALIZE_TURN:
        return ForcedTool(FINALIZE_TOOL_NAME)
    return "auto"
