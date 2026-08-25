"""The picture step's wiring: storage, its own state machine, least privilege.

Pictures are kept so a future replay variant could re-weave them, so the
bucket -- not any Lambda -- is what eventually deletes them. The vision step
runs in a one-task machine *before* any job exists, so the generation chain
no longer routes around it.
"""

from __future__ import annotations

import shutil

import pytest

# See conftest: aws-cdk-lib shells out to node at import time.
if shutil.which("node") is None:  # pragma: no cover - environment guard
    pytest.skip("aws-cdk-lib needs node on PATH", allow_module_level=True)

from aws_cdk import assertions
from conftest import build_data_stack

from stacks.data_stack import PICTURE_RETENTION_DAYS

ORIGINS = ["https://app.example.com"]


def audio_bucket_properties() -> dict:
    stack = build_data_stack(upload_origins=ORIGINS)
    template = assertions.Template.from_stack(stack)
    [bucket] = template.find_resources("AWS::S3::Bucket").values()
    return bucket["Properties"]


def test_pictures_expire_by_lifecycle_after_a_year() -> None:
    rules = audio_bucket_properties()["LifecycleConfiguration"]["Rules"]
    rule = next(r for r in rules if r["Id"] == "ExpireUploadedPictures")

    assert rule["Prefix"] == "pictures/"
    assert rule["ExpirationInDays"] == PICTURE_RETENTION_DAYS == 365
    # The audio rule is untouched: still jobs/ only.
    assert {r["Prefix"] for r in rules} == {"jobs/", "pictures/"}


def test_bucket_cors_admits_only_the_upload_post() -> None:
    """GET stays behind CloudFront signed URLs (constraint 6)."""
    rules = audio_bucket_properties()["CorsConfiguration"]["CorsRules"]

    assert len(rules) == 1
    assert rules[0]["AllowedMethods"] == ["POST"]
    assert rules[0]["AllowedOrigins"] == ORIGINS


def test_the_generation_chain_no_longer_branches_on_a_picture(states) -> None:
    """The picture is described before Begin; generate_script reads the
    result off the JOB item, so the chain is straight again."""
    assert states["FreezeCreditTask"]["Next"] == "GenerateScriptTask"
    assert "HasPicture" not in states
    assert "DescribePictureTask" not in states


def test_the_picture_machine_marks_the_item_before_failing(picture_states) -> None:
    """An attempt that ends in Step Functions never returns to the handler,
    so the Catch itself must record the failure -- or the item stays
    DESCRIBING until the stale window and the keywords screen waits for
    nothing. Nothing to refund: no credit exists yet."""
    states = picture_states
    task = states["DescribePictureTask"]
    assert task["Next"] == "PictureDescribed"
    assert states["PictureDescribed"]["Type"] == "Succeed"
    [catch] = task["Catch"]
    assert catch["Next"] == "MarkPictureFailed"
    mark = states["MarkPictureFailed"]
    assert mark["Next"] == "PictureDescriptionFailed"
    assert states["PictureDescriptionFailed"]["Type"] == "Fail"
    # payload_response_only renders the payload as the task's Parameters.
    payload = mark["Parameters"]
    assert payload["mode"] == "mark_failed"
    assert payload["attempt.$"] == "$.attempt"
    assert "RollbackCreditTask" not in states


def test_the_stale_window_matches_the_machine_timeout(pipeline_stack) -> None:
    """shared/models.PICTURE_DESCRIBE_TIMEOUT_SECONDS is mirrored by hand in
    CDK; if they drift, the API reclaims a still-running attempt (or strands a
    dead one). Pin them together."""
    from conftest import state_machine_definition

    from shared.models import PICTURE_DESCRIBE_TIMEOUT_SECONDS

    definition = state_machine_definition(pipeline_stack, name_contains="picture")
    assert definition["TimeoutSeconds"] == PICTURE_DESCRIBE_TIMEOUT_SECONDS


def test_describe_picture_retries_transient_bedrock_errors(picture_states) -> None:
    [retry] = picture_states["DescribePictureTask"]["Retry"]
    assert "BedrockTransientError" in retry["ErrorEquals"]
    assert retry["MaxAttempts"] == 3


def test_describe_picture_reads_pictures_and_never_deletes(pipeline_stack) -> None:
    template = assertions.Template.from_stack(pipeline_stack)
    policies = template.find_resources("AWS::IAM::Policy")
    [policy] = [p for name, p in policies.items() if name.startswith("DescribePicture")]

    s3_actions = {
        action
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
        for action in (
            statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]
        )
        if action.startswith("s3:")
    }
    assert s3_actions == {"s3:GetObject"}
    assert "pictures/*" in str(policy)
