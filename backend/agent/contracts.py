"""The contract between an agent engine and everything around it.

Three parties meet here and nowhere else:

* an **engine** (``native/`` today, ``langgraph/`` later) turns one user
  message into one turn of assistant output, calling tools as it goes --
  ``AgentEngine.run_turn``;
* the **harness** (``agent_runner``) rebuilds history, calls the engine and
  writes the checkpoint; it only ever sees ``TurnInput`` and ``TurnResult``;
* an **LLM provider** streams one model call -- ``LLMProvider.stream_turn``.

The message vocabulary is Bedrock Converse's (text, toolUse, toolResult) with
snake_case attribute names; ``Message.to_converse`` / ``from_converse`` are the
one place the camelCase wire form is spelled, so a Converse provider passes
messages straight through and the transcript stored on the T-items is the
wire form. Keeping the neutral format equal to the wire format is what lets
a second engine read the same history without translation.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

# ----------------------------------------------------------------------
# Content blocks and messages
# ----------------------------------------------------------------------


class TextBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str


class JsonBlock(BaseModel):
    """A structured tool result. Converse spells it ``{"json": {...}}``."""

    model_config = ConfigDict(extra="forbid")

    data: dict[str, Any]


class ToolUseBlock(BaseModel):
    """The model asking for a tool. ``input`` is whatever JSON it produced;
    validation against the tool's schema happens in the registry."""

    model_config = ConfigDict(extra="forbid")

    tool_use_id: str
    name: str
    input: dict[str, Any] = Field(default_factory=dict)


class ToolResultBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_use_id: str
    content: list[TextBlock | JsonBlock]
    status: Literal["success", "error"] = "success"


ContentBlock = TextBlock | ToolUseBlock | ToolResultBlock
ToolOutputBlock = TextBlock | JsonBlock

Role = Literal["user", "assistant"]


def block_to_converse(block: ContentBlock | ToolOutputBlock) -> dict[str, Any]:
    """One block in Converse wire form."""
    if isinstance(block, TextBlock):
        return {"text": block.text}
    if isinstance(block, JsonBlock):
        return {"json": block.data}
    if isinstance(block, ToolUseBlock):
        return {
            "toolUse": {"toolUseId": block.tool_use_id, "name": block.name, "input": block.input}
        }
    return {
        "toolResult": {
            "toolUseId": block.tool_use_id,
            "content": [block_to_converse(part) for part in block.content],
            "status": block.status,
        }
    }


def block_from_converse(raw: dict[str, Any]) -> ContentBlock:
    """The inverse of ``block_to_converse``; unknown block types fail loudly.

    Converse can also return reasoning or image blocks; none of them belong
    in this transcript, and silently dropping one would hide a provider bug.
    """
    if "text" in raw:
        return TextBlock(text=raw["text"])
    if "toolUse" in raw:
        use = raw["toolUse"]
        return ToolUseBlock(tool_use_id=use["toolUseId"], name=use["name"], input=use["input"])
    if "toolResult" in raw:
        result = raw["toolResult"]
        return ToolResultBlock(
            tool_use_id=result["toolUseId"],
            content=[_output_from_converse(part) for part in result["content"]],
            status=result.get("status", "success"),
        )
    raise ValueError(f"unsupported content block: {sorted(raw)}")


def _output_from_converse(raw: dict[str, Any]) -> ToolOutputBlock:
    if "text" in raw:
        return TextBlock(text=raw["text"])
    if "json" in raw:
        return JsonBlock(data=raw["json"])
    raise ValueError(f"unsupported tool result block: {sorted(raw)}")


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Role
    content: list[ContentBlock]

    @classmethod
    def user_text(cls, text: str) -> Message:
        return cls(role="user", content=[TextBlock(text=text)])

    @classmethod
    def assistant(cls, content: list[ContentBlock]) -> Message:
        return cls(role="assistant", content=list(content))

    @classmethod
    def tool_results(cls, results: list[ToolResultBlock]) -> Message:
        """Every result of one assistant turn's tool calls, in ONE message.

        Splitting them across messages both breaks Converse's role
        alternation and teaches the model to stop calling tools in parallel.
        """
        return cls(role="user", content=list(results))

    def with_text_appended(self, text: str) -> Message:
        """A copy with a trailing text block -- steering appended to a user
        message (a converge hint, a no-more-tools hint) without touching the
        blocks the model must see first (tool results come before text)."""
        return Message(role=self.role, content=[*self.content, TextBlock(text=text)])

    def to_converse(self) -> dict[str, Any]:
        return {"role": self.role, "content": [block_to_converse(b) for b in self.content]}

    @classmethod
    def from_converse(cls, raw: dict[str, Any]) -> Message:
        return cls(role=raw["role"], content=[block_from_converse(b) for b in raw["content"]])


SystemBlock = TextBlock
# ``{"toolSpec": {"name", "description", "inputSchema": {"json": ...}}}`` --
# built by the tool registry, consumed verbatim by a Converse provider.
ConverseToolSpec = dict[str, Any]


# ----------------------------------------------------------------------
# What one model call yields (layer 1 -> layer 2)
# ----------------------------------------------------------------------


class Usage(BaseModel):
    """Token counts for one call; ``+`` folds calls into a turn and turns
    into a session. Cache counters are what the cost story is measured by."""

    model_config = ConfigDict(extra="forbid")

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
        )


StopReason = Literal["end_turn", "tool_use", "max_tokens", "refusal"]


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class ToolUseStart:
    name: str
    tool_use_id: str


@dataclass(frozen=True)
class Final:
    """The whole assistant message once the stream has ended."""

    content: list[ContentBlock]
    stop_reason: StopReason
    usage: Usage


LLMEvent = TextDelta | ToolUseStart | Final


@dataclass(frozen=True)
class ForcedTool:
    """``toolChoice: {"tool": {"name": ...}}`` -- the model must call this."""

    name: str


ToolChoice = Literal["auto"] | ForcedTool


class LLMProvider(Protocol):
    """One streamed model call. Implementations own request shape, stream
    parsing, error classification and retries; they know nothing about
    tools' meaning or sessions."""

    def stream_turn(
        self,
        system: list[SystemBlock],
        messages: list[Message],
        tools: list[ConverseToolSpec],
        *,
        tool_choice: ToolChoice | None,
    ) -> AsyncIterator[LLMEvent]: ...


# ----------------------------------------------------------------------
# What one turn yields (layer 2 -> layer 3)
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class ToolStarted:
    """A tool is running. Only the name: its input is user content."""

    name: str


@dataclass(frozen=True)
class Done:
    """Sent by the harness once the checkpoint is committed, never by an
    engine -- a turn is not done until it is durable."""

    turn: int
    job_id: str | None = None


@dataclass(frozen=True)
class ProposalReady:
    """The model proposed a meditation; the listener decides whether to
    start it. Duration only -- the brief is on the session for the owner
    to read, and starting it is a separate, deliberate request."""

    duration_minutes: int


AgentEvent = TextDelta | ToolStarted | ProposalReady | Done
Emit = Callable[[AgentEvent], Awaitable[None]]


class Finalized(BaseModel):
    """A terminal tool ran: a generation job exists and the session ends.

    No production tool is terminal any more -- generation waits for the
    listener's confirmation (docs/agent-runner-plan.md §4) -- but the
    contract keeps the path for test tools and other engines.
    """

    model_config = ConfigDict(extra="forbid")

    job_id: str


class Proposal(BaseModel):
    """A brief is waiting on the session for the listener to confirm."""

    model_config = ConfigDict(extra="forbid")

    duration_minutes: int


class ToolCallRecord(BaseModel):
    """One tool call as it happened, for the turn's record and the metrics."""

    model_config = ConfigDict(extra="forbid")

    name: str
    tool_use_id: str
    input: dict[str, Any]
    output: list[ToolOutputBlock]
    status: Literal["success", "error"]
    elapsed_ms: int


class ToolRound(BaseModel):
    """One assistant message that asked for tools, and their results.

    Stored as-is on the checkpoint so the next turn can replay the exchange
    verbatim: a model that cannot see what it already called repeats itself.
    """

    model_config = ConfigDict(extra="forbid")

    assistant_content: list[ContentBlock]
    results: list[ToolResultBlock]


class TurnInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    history: list[Message]
    user_text: str
    turn: int = Field(ge=0)
    memory_block: str = ""


class TurnResult(BaseModel):
    """Everything the harness needs to checkpoint and answer.

    ``content`` is the assistant message of the LAST model call. ``rounds``
    are the tool exchanges before it, in order. When ``finalized`` is set the
    turn ended on a tool round -- ``content`` is then that round's assistant
    message and no text-only message follows, which is why the checkpoint
    keeps ``finalized`` next to the blocks.
    """

    model_config = ConfigDict(extra="forbid")

    content: list[ContentBlock]
    rounds: list[ToolRound] = Field(default_factory=list)
    tool_log: list[ToolCallRecord] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    stop_reason: StopReason
    finalized: Finalized | None = None
    # The last proposal this turn made, if any; the session holds the brief.
    proposal: Proposal | None = None


@dataclass(frozen=True)
class Deadline:
    """A wall-clock budget for one turn, from ``time.monotonic()``.

    The harness derives it from the Lambda's remaining time; the engine
    checks it before every model call and stops asking for tools once it
    is close, so a turn ends with an answer rather than a timeout.
    """

    at: float | None

    @classmethod
    def after(cls, seconds: float) -> Deadline:
        return cls(at=time.monotonic() + seconds)

    @classmethod
    def never(cls) -> Deadline:
        return cls(at=None)

    def remaining(self) -> float:
        if self.at is None:
            return float("inf")
        return self.at - time.monotonic()

    def exhausted(self, margin_seconds: float = 10.0) -> bool:
        return self.remaining() <= margin_seconds


class AgentEngine(Protocol):
    """One turn: history + user text in, assistant content + tool log out.

    Pure computation. An engine never reads or writes the table; the
    harness rebuilds ``history`` from the checkpoints and commits the
    result, which is what makes the two engines interchangeable and their
    output comparable field by field.
    """

    async def run_turn(self, inp: TurnInput, *, deadline: Deadline, emit: Emit) -> TurnResult: ...
