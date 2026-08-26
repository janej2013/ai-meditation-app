"""The provider protocol the native loop drives, re-exported from the
contract so a provider module imports one thing.

A provider implements ``stream_turn`` as an async generator: text deltas and
tool-use starts as they arrive, then exactly one ``Final`` carrying the whole
message, its stop reason and its usage. Errors are the provider's to
classify and retry; what escapes is terminal for the turn.
"""

from agent.contracts import (
    ConverseToolSpec,
    Final,
    ForcedTool,
    LLMEvent,
    LLMProvider,
    Message,
    StopReason,
    SystemBlock,
    TextDelta,
    ToolChoice,
    ToolUseStart,
    Usage,
)

__all__ = [
    "ConverseToolSpec",
    "Final",
    "ForcedTool",
    "LLMEvent",
    "LLMProvider",
    "Message",
    "StopReason",
    "SystemBlock",
    "TextDelta",
    "ToolChoice",
    "ToolUseStart",
    "Usage",
]
