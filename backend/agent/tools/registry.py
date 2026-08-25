"""Tools as both engines see them: a name, a Pydantic input model, a handler.

One definition serves two consumers. The native engine sends
``to_converse_spec()`` to Bedrock and routes tool calls through ``execute``;
the LangGraph engine (later) wraps the same specs as ``StructuredTool``s. The
input model is therefore the contract with the model *and* the validation
the handler relies on -- Converse has no strict schema mode, so a rejected
input goes back to the model as an error result it can correct.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from agent.contracts import (
    ConverseToolSpec,
    Finalized,
    JsonBlock,
    TextBlock,
    ToolCallRecord,
    ToolOutputBlock,
    ToolResultBlock,
    ToolUseBlock,
)

if TYPE_CHECKING:
    from shared.db import EntitlementStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolContext:
    """What a handler may touch. Built per request by the harness; the
    engine passes it through untouched."""

    user_id: str
    store: EntitlementStore | None = None


class ToolOutcome(BaseModel):
    """What a handler returns. ``finalized`` is only honoured from a tool
    registered as terminal -- see ``ToolRegistry.execute``."""

    model_config = ConfigDict(extra="forbid")

    content: dict[str, Any] | str
    status: Literal["success", "error"] = "success"
    finalized: Finalized | None = None

    @classmethod
    def error(cls, message: str) -> ToolOutcome:
        return cls(content=message, status="error")


Handler = Callable[[ToolContext, Any], Awaitable[ToolOutcome]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_model: type[BaseModel]
    handler: Handler
    # A terminal tool ends the turn -- and the session -- when it succeeds.
    terminal: bool = False

    def to_converse(self) -> ConverseToolSpec:
        return {
            "toolSpec": {
                "name": self.name,
                "description": self.description,
                "inputSchema": {"json": self.input_model.model_json_schema()},
            }
        }


@dataclass(frozen=True)
class ToolExecution:
    """One executed call: the block that goes back to the model, the record
    that goes on the checkpoint, and whether it closed the session."""

    result: ToolResultBlock
    record: ToolCallRecord
    finalized: Finalized | None = None


class ToolRegistry:
    """The tools offered on a session, in a fixed order.

    Order is part of the prompt-cache prefix: the same tools registered in a
    different order are a different prefix and a cache miss on every call.
    """

    def __init__(self, specs: Iterable[ToolSpec] = ()) -> None:
        self._specs: dict[str, ToolSpec] = {}
        for spec in specs:
            self.register(spec)

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._specs:
            raise ValueError(f"tool {spec.name!r} is already registered")
        self._specs[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def __iter__(self) -> Iterator[ToolSpec]:
        return iter(self._specs.values())

    def __len__(self) -> int:
        return len(self._specs)

    def to_converse_spec(self) -> list[ConverseToolSpec]:
        return [spec.to_converse() for spec in self._specs.values()]

    async def execute(self, ctx: ToolContext, block: ToolUseBlock) -> ToolExecution:
        """Run one tool call. Never raises: every failure becomes an error
        result the model can read, because a tool call the model made and
        never heard back from leaves the conversation in an invalid state.

        Logs carry the tool name, status and duration only -- input and
        output derive from user content (constraint 7).
        """
        started = time.monotonic()
        spec = self._specs.get(block.name)
        if spec is None:
            outcome = ToolOutcome.error(f"unknown tool: {block.name}")
        else:
            outcome = await _run(spec, ctx, block)

        finalized = outcome.finalized
        if finalized is not None and (spec is None or not spec.terminal):
            logger.warning("tool %s returned finalized but is not terminal; ignored", block.name)
            finalized = None
        if finalized is not None and outcome.status == "error":
            finalized = None

        output = _output_blocks(outcome)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.info("tool %s status=%s elapsed_ms=%d", block.name, outcome.status, elapsed_ms)
        return ToolExecution(
            result=ToolResultBlock(
                tool_use_id=block.tool_use_id, content=output, status=outcome.status
            ),
            record=ToolCallRecord(
                name=block.name,
                tool_use_id=block.tool_use_id,
                input=block.input,
                output=output,
                status=outcome.status,
                elapsed_ms=elapsed_ms,
            ),
            finalized=finalized,
        )


async def _run(spec: ToolSpec, ctx: ToolContext, block: ToolUseBlock) -> ToolOutcome:
    try:
        parsed = spec.input_model.model_validate(block.input)
    except ValidationError as exc:
        # Field-level reasons, so the model can fix the call. Pydantic's
        # messages name the constraint, not the offending value.
        reasons = "; ".join(
            f"{'.'.join(str(part) for part in err['loc']) or '<root>'}: {err['msg']}"
            for err in exc.errors()
        )
        return ToolOutcome.error(f"invalid input for {spec.name}: {reasons}")
    try:
        return await spec.handler(ctx, parsed)
    except Exception as exc:
        # Type only: the message may quote the input.
        logger.warning("tool %s raised %s", spec.name, type(exc).__name__)
        return ToolOutcome.error(f"{spec.name} failed: {type(exc).__name__}")


def _output_blocks(outcome: ToolOutcome) -> list[ToolOutputBlock]:
    if isinstance(outcome.content, str):
        return [TextBlock(text=outcome.content)]
    return [JsonBlock(data=outcome.content)]
