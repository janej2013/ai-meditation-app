"""The contract between Step Functions tasks.

Every task Lambda validates ``PipelineState`` on entry and returns the updated
model, so a payload that drifts fails at the boundary rather than deep inside a
handler.

Two things are deliberately absent from this state:

* **the user's mood text** -- Step Functions persists execution input in the
  execution history, where it is visible in the console for 90 days. Constraint
  7 forbids putting user input there, so the API writes ``mood_text`` onto the
  JOB item and ``generate_script`` reads it from DynamoDB instead.
* **the generated script** -- same reasoning, plus it would bloat the state.
  ``generate_script`` writes it to S3 and passes only the key.

The error classes below are the vocabulary the state machine matches on. Step
Functions compares ``ErrorEquals`` against the Python exception class name, so
renaming one of these silently changes retry behaviour -- pipeline_stack
imports them rather than repeating the strings.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

MIN_DURATION_MINUTES = 3
MAX_DURATION_MINUTES = 30


class PipelineState(BaseModel):
    """Payload passed between every state of the generation state machine."""

    model_config = ConfigDict(extra="ignore")

    user_id: str
    job_id: str
    duration_minutes: int = Field(ge=MIN_DURATION_MINUTES, le=MAX_DURATION_MINUTES)

    # Filled in as the pipeline progresses; each is an S3 object key.
    script_key: str | None = None
    narration_key: str | None = None
    audio_key: str | None = None


# ----------------------------------------------------------------------
# Error vocabulary for the state machine
# ----------------------------------------------------------------------


class PipelineError(Exception):
    """Base class for pipeline task failures."""


class TransientError(PipelineError):
    """A failure worth retrying: throttling, timeouts, upstream 5xx.

    Subclasses are the only errors listed in a state's Retry block. Everything
    else -- validation failures, malformed responses, missing objects -- falls
    straight through to Catch, because retrying them cannot help.
    """


class BedrockTransientError(TransientError):
    """Bedrock throttled the request or returned a server-side error."""


class TTSTransientError(TransientError):
    """The TTS vendor throttled the request or returned a server-side error."""


class ScriptGenerationError(PipelineError):
    """Bedrock returned a response that is unusable as a meditation script."""


class AudioMixError(PipelineError):
    """ffmpeg failed to produce the final mix."""
