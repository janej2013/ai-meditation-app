"""Step 4: mix background music under the narration with ffmpeg.

Records ``audio_key`` on the JOB item but deliberately does NOT set status to
DONE -- see ``EntitlementStore.set_job_audio_key`` and commit_credit for why
that transition belongs to the commit transaction.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import boto3

from shared.db import EntitlementStore
from shared.pipeline import AudioMixError, PipelineState

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Provided by the ffmpeg Lambda layer (see scripts/build_layers.sh).
FFMPEG = os.environ.get("FFMPEG_PATH", "/opt/bin/ffmpeg")
FFPROBE = os.environ.get("FFPROBE_PATH", "/opt/bin/ffprobe")

BGM_VOLUME = 0.2  # music sits well under the voice
FADE_SECONDS = 4
TAIL_SECONDS = 5  # music continues after the voice stops
_SUBPROCESS_TIMEOUT = 110  # under the Lambda's own timeout

_store: EntitlementStore | None = None
_s3: Any = None


def _get_store() -> EntitlementStore:
    global _store
    if _store is None:
        _store = EntitlementStore()
    return _store


def _get_s3() -> Any:
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3")
    return _s3


def final_key(job_id: str) -> str:
    return f"jobs/{job_id}/final.mp3"


def build_filter_complex(narration_seconds: float) -> str:
    """The mix graph.

    Two options carry the weight:

    ``normalize=0`` -- amix divides by the number of inputs by default, which
    would silently drop the narration to half volume. This is the single most
    common way this filter goes wrong.

    ``alimiter`` -- with normalize off the inputs are summed, so a loud passage
    could clip; the limiter catches that.

    The voice is padded so the music keeps playing after the last word, then
    fades out over the tail rather than stopping dead.
    """
    total = narration_seconds + TAIL_SECONDS
    fade_out_start = max(total - FADE_SECONDS, 0)
    return (
        f"[0:a]apad=pad_dur={TAIL_SECONDS}[voice];"
        f"[1:a]volume={BGM_VOLUME},"
        f"afade=t=in:st=0:d={FADE_SECONDS},"
        f"afade=t=out:st={fade_out_start:.2f}:d={FADE_SECONDS}[bg];"
        f"[voice][bg]amix=inputs=2:duration=first:normalize=0,"
        f"alimiter=limit=0.95[out]"
    )


def probe_duration(path: Path) -> float:
    """Length of an audio file in seconds, via ffprobe."""
    result = _run(
        [
            FFPROBE,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ]
    )
    try:
        return float(result.stdout.strip())
    except ValueError as exc:
        raise AudioMixError(f"ffprobe returned no duration for {path.name}") from exc


def lambda_handler(event: dict[str, Any], context: object) -> dict[str, Any]:  # noqa: ARG001
    state = PipelineState.model_validate(event)
    if not state.narration_key:
        raise ValueError(f"job {state.job_id} reached mix_audio with no narration_key")

    bucket = os.environ["AUDIO_BUCKET"]
    bgm_key = os.environ["BGM_KEY"]
    s3 = _get_s3()

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        narration = tmpdir / "narration.mp3"
        bgm = tmpdir / "bgm.mp3"
        final = tmpdir / "final.mp3"

        s3.download_file(bucket, state.narration_key, str(narration))
        s3.download_file(bucket, bgm_key, str(bgm))

        duration = probe_duration(narration)
        _run(
            [
                FFMPEG,
                "-y",
                "-i",
                str(narration),
                "-stream_loop",
                "-1",
                "-i",
                str(bgm),
                "-filter_complex",
                build_filter_complex(duration),
                "-map",
                "[out]",
                "-c:a",
                "libmp3lame",
                "-b:a",
                "128k",
                "-ar",
                "44100",
                str(final),
            ]
        )

        key = final_key(state.job_id)
        s3.upload_file(
            str(final),
            bucket,
            key,
            ExtraArgs={"ContentType": "audio/mpeg"},
        )
        size = final.stat().st_size

    _get_store().set_job_audio_key(state.user_id, state.job_id, key)
    logger.info("mix complete job_id=%s narration_s=%.1f bytes=%d", state.job_id, duration, size)

    state.audio_key = key
    return state.model_dump()


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    """Run ffmpeg/ffprobe, surfacing stderr on failure.

    ffmpeg failures are deterministic (a bad filter graph, a corrupt input), so
    AudioMixError is not in any Retry block -- it goes straight to Catch.
    """
    try:
        # Fixed binary path, argument list, no shell -- nothing user-supplied
        # reaches the command line.
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=True,
            timeout=_SUBPROCESS_TIMEOUT,
        )
    except FileNotFoundError as exc:
        raise AudioMixError(
            f"{command[0]} not found -- is the ffmpeg layer attached? "
            "Run scripts/build_layers.sh before deploying."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise AudioMixError(f"{Path(command[0]).name} timed out") from exc
    except subprocess.CalledProcessError as exc:
        tail = (exc.stderr or "").strip().splitlines()[-5:]
        raise AudioMixError(
            f"{Path(command[0]).name} failed ({exc.returncode}): {' | '.join(tail)}"
        ) from exc
