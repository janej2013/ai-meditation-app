"""Converse's ``stopReason`` values, mapped onto the contract's four.

One table for both engines: the native provider reads the reason off the
``messageStop`` event, the LangGraph engine off ``response_metadata`` --
the same strings, so the same map. A guardrail or content filter is a
refusal; a stop sequence is an ordinary end of turn.
"""

from __future__ import annotations

from agent.contracts import StopReason

STOP_REASONS: dict[str, StopReason] = {
    "end_turn": "end_turn",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "stop_sequence": "end_turn",
    "guardrail_intervened": "refusal",
    "content_filtered": "refusal",
}


def map_stop_reason(raw: str) -> StopReason:
    """Raises ``ValueError`` on a reason the contract has no word for; the
    caller decides whether that fails the turn."""
    mapped = STOP_REASONS.get(raw)
    if mapped is None:
        raise ValueError(f"unexpected stopReason {raw!r}")
    return mapped
