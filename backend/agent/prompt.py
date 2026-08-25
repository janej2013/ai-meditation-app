"""Prompt text shared by every engine.

Product copy, kept apart from loop plumbing so it can be reviewed as prose
and asserted on by tests without a model. SYSTEM_PROMPT itself lands in A3
together with the evals that exercise it; this module already fixes the
per-turn steering text and the memory block, which the loop and the
checkpoint bridge both depend on.
"""

from __future__ import annotations

from agent.budget import wants_converge_hint

# Filled in A3 (docs/agent-runner-plan.md §3.3). Empty rather than a
# placeholder sentence so nothing accidentally ships as the real prompt.
SYSTEM_PROMPT = ""

CONVERGE_HINT = (
    "(Guidance: bring the conversation to a close within the next two "
    "replies -- confirm what the listener wants and prepare the meditation "
    "brief.)"
)

NO_MORE_TOOLS_HINT = (
    "(Guidance: do not call any more tools. Reply to the listener directly with what you have.)"
)

# What the listener sees when the model declined to answer. Fixed text, never
# retried: a refusal is a signal, not a transient error.
REFUSAL_TEXT = (
    "I'm not able to go further with that here. If you're in distress, "
    "please reach out to someone who can help right now -- in Australia, "
    "Lifeline is on 13 11 14, and 000 in an emergency. I'm here when you'd "
    "like to return to preparing a meditation."
)

# Rendered when the user has no insights yet, so the second system block is
# stable across a session either way (prompt caching keys on the prefix).
EMPTY_MEMORY_BLOCK = "You have not recorded anything about this listener yet."


def render_memory_block(insights: list[str]) -> str:
    """The user's remembered preferences, as the model reads them.

    User content: this string goes into the prompt and nowhere else
    (constraint 7) -- never log it.
    """
    if not insights:
        return EMPTY_MEMORY_BLOCK
    lines = "\n".join(f"- {insight}" for insight in insights)
    return f"Things you have noted about this listener in earlier sessions:\n{lines}"


def converge_hint(turn: int) -> str:
    """The steering text appended to the user message on late turns; empty
    early on. Deterministic in the turn number so the checkpoint bridge can
    reproduce exactly what the model saw."""
    return CONVERGE_HINT if wants_converge_hint(turn) else ""


def user_message_text(turn: int, user_text: str) -> str:
    """The user's words plus this turn's steering, as sent to the model.

    Shared by the engine (when composing) and the checkpoint bridge (when
    rebuilding), so history replays byte-for-byte.
    """
    hint = converge_hint(turn)
    return f"{user_text}\n\n{hint}" if hint else user_text
