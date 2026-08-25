"""The contract between Step Functions tasks.

Every task Lambda validates ``PipelineState`` on entry and returns the updated
model, so a payload that drifts fails at the boundary rather than deep inside a
handler.

Two things are deliberately absent from this state:

* **the user's mood text and picture** -- Step Functions persists execution input in the
  execution history, where it is visible in the console for 90 days. Constraint
  7 forbids putting user input there, so the API writes ``mood_text`` or the
  picture's key and description onto the JOB item and the task Lambdas read
  them from DynamoDB instead.
* **the generated script** -- same reasoning, plus it would bloat the state.
  ``generate_script`` writes it to S3 and passes only the key.

The error classes below are the vocabulary the state machine matches on. Step
Functions compares ``ErrorEquals`` against the Python exception class name, so
**renaming one of these silently changes retry behaviour**: pipeline_stack
repeats the names as string literals, and nothing links the two. Moving a class
between modules is safe; renaming it is not.
"""

from __future__ import annotations

from typing import Literal, NoReturn

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


class PictureState(BaseModel):
    """Payload of the picture state machine: ids, the attempt token, and
    which of the Lambda's two jobs to do. The key is derived from the ids and
    the reading lands on the PICTURE item; ``attempt`` is the claim timestamp
    (never user content) that every write is conditioned on."""

    model_config = ConfigDict(extra="ignore")

    user_id: str
    picture_id: str
    attempt: str
    # "describe" is the task; "mark_failed" is what the machine's Catch runs
    # so an attempt that ended in Step Functions (retries exhausted, task
    # timeout) still records its failure on the item.
    mode: Literal["describe", "mark_failed"] = "describe"


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


# Bedrock signals overload and throttling with these. Everything else --
# ValidationException, AccessDeniedException, a malformed response -- is
# permanent, so it must fall through to Catch instead of burning retries.
# One list for every Bedrock step: a code added here changes retry behaviour
# for generate_script and describe_picture at once.
BEDROCK_TRANSIENT_CODES = frozenset(
    {
        "ThrottlingException",
        "ServiceUnavailableException",
        "InternalServerException",
        "ModelTimeoutException",
        "ModelNotReadyException",
    }
)


def raise_for_bedrock_error(exc: Exception, permanent: type[PipelineError]) -> NoReturn:
    """Re-raise a botocore ClientError from Converse as transient or permanent.

    The vendor message names the rejected parameter or account restriction --
    never the prompt or the picture -- so it is safe to surface (constraint 7)
    and indispensable: the code alone turns every failure into a hunt.
    """
    error = getattr(exc, "response", {}).get("Error", {})
    code = error.get("Code", "")
    detail = f"{code}: {error.get('Message', '')}"
    if code in BEDROCK_TRANSIENT_CODES:
        raise BedrockTransientError(f"bedrock transient failure: {detail}") from exc
    raise permanent(f"bedrock call failed: {detail}") from exc


# ``TTSTransientError`` is deliberately not here. It has to be both a
# TransientError and a TTSError, and defining it in shared.tts.base keeps the
# dependency pointing one way -- the TTS layer knows about this taxonomy, and
# this module knows nothing about TTS.


class ScriptGenerationError(PipelineError):
    """Bedrock returned a response that is unusable as a meditation script."""


class PictureDescriptionError(PipelineError):
    """The picture could not be read or described: missing, oversized, or the
    model's answer did not fit the PictureDescription contract."""


class AudioMixError(PipelineError):
    """ffmpeg failed to produce the final mix."""
