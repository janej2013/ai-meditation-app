"""The bridge between a turn's result and the T-item that records it.

Engines produce ``TurnResult``; the store persists ``AgentTurn``; this module
converts one into the other and, going the other way, rebuilds the message
history the next turn starts from. It is the one place in ``backend/agent``
allowed to know the storage model -- engines stay pure.

What a rebuilt history reproduces, and what it does not:

* the user's words plus the converge hint (re-derived from the turn number
  through ``prompt.user_message_text``), every tool round verbatim, and the
  final assistant message -- byte-for-byte what the model saw;
* NOT the no-more-tools hint the loop appends when a turn runs out of
  iterations or time. That text is situational, not a function of the turn
  number, and the model does not need to see it again.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

from agent.contracts import (
    ContentBlock,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolRound,
    TurnResult,
    block_from_converse,
    block_to_converse,
)
from agent.prompt import EMPTY_REPLY_TEXT, user_message_text
from shared.models import AgentTurn, AgentUsage


class TurnCheckpoint(AgentTurn):
    """An ``AgentTurn`` built from a ``TurnResult``. Blocks are stored in
    Converse wire form, so the item is readable without this package."""

    @classmethod
    def from_result(
        cls,
        *,
        session_id: str,
        turn: int,
        user_text: str,
        result: TurnResult,
        created_at: datetime | None = None,
    ) -> TurnCheckpoint:
        return cls(
            session_id=session_id,
            turn=turn,
            user_text=user_text,
            assistant_content=[block_to_converse(b) for b in result.content],
            tool_calls=[_round_to_wire(r) for r in result.rounds],
            usage=AgentUsage(**result.usage.model_dump()),
            stop_reason=result.stop_reason,
            finalized_job_id=result.finalized.job_id if result.finalized else None,
            created_at=created_at or datetime.now(UTC),
        )


def rebuild_messages(turns: Iterable[AgentTurn]) -> list[Message]:
    """The conversation so far, as the next turn's ``history``.

    Turns are ordered here rather than trusted: the store returns them by
    sort key, which is turn order only because the key zero-pads the number.

    A finalized turn ends on its tool round -- the results are the last
    message and no assistant text follows. Converse requires alternating
    roles, so such a history could not take another user message; it does
    not need to, because finalizing closes the session.
    """
    messages: list[Message] = []
    for turn in sorted(turns, key=lambda t: t.turn):
        messages.append(Message.user_text(user_message_text(turn.turn, turn.user_text)))
        for raw in turn.tool_calls:
            tool_round = _round_from_wire(raw)
            messages.append(Message.assistant(tool_round.assistant_content))
            messages.append(Message.tool_results(tool_round.results))
        if turn.finalized_job_id is None:
            content: list[ContentBlock] = [block_from_converse(b) for b in turn.assistant_content]
            if not any(isinstance(b, TextBlock) and b.text.strip() for b in content):
                # A turn stored before the loop guarded against empty
                # replies. Converse refuses an empty assistant message and
                # the roles must alternate, so the reply the listener was
                # shown in its place stands in here too.
                content = [TextBlock(text=EMPTY_REPLY_TEXT)]
            messages.append(Message.assistant(content))
    return messages


def _round_to_wire(tool_round: ToolRound) -> dict:
    return {
        "assistant_content": [block_to_converse(b) for b in tool_round.assistant_content],
        "results": [block_to_converse(r) for r in tool_round.results],
    }


def _round_from_wire(raw: dict) -> ToolRound:
    results: list[ToolResultBlock] = []
    for r in raw["results"]:
        block = block_from_converse(r)
        if not isinstance(block, ToolResultBlock):
            raise ValueError("a tool round's results must be toolResult blocks")
        results.append(block)
    return ToolRound(
        assistant_content=[block_from_converse(b) for b in raw["assistant_content"]],
        results=results,
    )
