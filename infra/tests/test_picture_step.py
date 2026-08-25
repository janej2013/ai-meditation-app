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


def picture_machine_states(pipeline_stack) -> dict:
    template = assertions.Template.from_stack(pipeline_stack)
    machines = template.find_resources("AWS::StepFunctions::StateMachine")
    [picture] = [
        m for m in machines.values() if "picture" in str(m["Properties"].get("StateMachineName"))
    ]
    # The definition is an Fn::Join over literal chunks and Lambda ARN tokens.
    chunks = picture["Properties"]["DefinitionString"]["Fn::Join"][1]
    # Tokens sit inside JSON string values, so a bare placeholder keeps the
    # document parseable (same trick as conftest.state_machine_definition).
    text = "".join(c if isinstance(c, str) else "arn" for c in chunks)
    import json

    return json.loads(text)["States"]


def test_the_picture_machine_describes_then_succeeds_or_fails_without_a_refund(
    pipeline_stack,
) -> None:
    states = picture_machine_states(pipeline_stack)
    task = states["DescribePictureTask"]
    assert task["Next"] == "PictureDescribed"
    assert states["PictureDescribed"]["Type"] == "Succeed"
    [catch] = task["Catch"]
    assert catch["Next"] == "PictureDescriptionFailed"
    assert states["PictureDescriptionFailed"]["Type"] == "Fail"
    assert "RollbackCreditTask" not in states  # nothing was frozen


def test_describe_picture_retries_transient_bedrock_errors(pipeline_stack) -> None:
    [retry] = picture_machine_states(pipeline_stack)["DescribePictureTask"]["Retry"]
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
