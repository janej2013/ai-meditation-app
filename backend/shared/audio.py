"""The audio bucket's object conventions under ``jobs/``.

Two things live here so the API and the pipeline cannot drift apart on them:
the tag that marks an object as an expirable intermediate (the bucket's
lifecycle rule keys on it -- infra mirrors the pair in data_stack.py), and the
sweep that removes a job's objects, used by DELETE /dreamscapes and by
rollback_credit for a job that produced audio but never completed.
"""

from __future__ import annotations

from typing import Any

# Lifecycle: objects carrying this tag expire; everything else under jobs/
# (the narration, a paid deliverable) never does. Mirrored by hand in
# infra/stacks/data_stack.py -- infra cannot import this package.
TRANSIENT_TAG_KEY = "transient"
TRANSIENT_TAG_VALUE = "true"
# The x-amz-tagging query form put_object takes.
TRANSIENT_TAGGING = f"{TRANSIENT_TAG_KEY}={TRANSIENT_TAG_VALUE}"


def job_prefix(job_id: str) -> str:
    return f"jobs/{job_id}/"


# The two objects a job writes, composed from the one prefix the sweep and
# the lifecycle rules key on -- so a writer and the reaper cannot disagree.
def script_key(job_id: str) -> str:
    return f"{job_prefix(job_id)}script.txt"


def narration_key(job_id: str) -> str:
    return f"{job_prefix(job_id)}narration.mp3"


class SweepError(Exception):
    """delete_objects reported per-key failures; some objects remain."""


def sweep_job_objects(s3: Any, bucket: str, job_id: str) -> int:
    """Delete everything under jobs/{job_id}/. Returns how many were removed.

    Deleting nothing is success, which is what makes callers idempotent.
    delete_objects does not raise for per-key failures -- they come back in
    the response's ``Errors`` -- so those are turned into an exception here:
    a caller that treated them as success would never retry, and the orphan
    would be permanent (untagged, so no lifecycle rule reaps it).
    """
    removed = 0
    kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": job_prefix(job_id)}
    while True:
        page = s3.list_objects_v2(**kwargs)
        keys = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
        if keys:
            result = s3.delete_objects(Bucket=bucket, Delete={"Objects": keys, "Quiet": True})
            errors = result.get("Errors") or []
            if errors:
                # Key names are job-scoped paths, not user content.
                raise SweepError(f"{len(errors)} of {len(keys)} objects not deleted")
            removed += len(keys)
        if not page.get("IsTruncated"):
            return removed
        kwargs["ContinuationToken"] = page["NextContinuationToken"]
