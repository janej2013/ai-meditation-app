"""The picture step's wiring: storage, routing, and least privilege.

Pictures are kept for the planned replay feature, so the bucket -- not any
Lambda -- is what eventually deletes them; and the Choice that routes around
DescribePicture must never be able to fault on an execution that predates it.
"""

from __future__ import annotations

import shutil

import pytest

# See conftest: aws-cdk-lib shells out to node at import time.
if shutil.which("node") is None:  # pragma: no cover - environment guard
    pytest.skip("aws-cdk-lib needs node on PATH", allow_module_level=True)

import aws_cdk as cdk
from aws_cdk import assertions
from conftest import state_machine_definition

from stacks.data_stack import PICTURE_RETENTION_DAYS, DataStack

ENV = cdk.Environment(account="111122223333", region="ap-southeast-2")
ORIGINS = ["https://app.example.com"]


def audio_bucket_properties() -> dict:
    app = cdk.App()
    stack = DataStack(app, "Data", env_name="dev", upload_origins=ORIGINS, env=ENV)
    template = assertions.Template.from_stack(stack)
    return next(iter(template.find_resources("AWS::S3::Bucket").values()))["Properties"]


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


@pytest.fixture(scope="module")
def states(pipeline_stack) -> dict:
    return state_machine_definition(pipeline_stack)["States"]


def test_freeze_routes_through_a_choice_on_has_picture(states) -> None:
    choice = states[states["FreezeCreditTask"]["Next"]]
    assert choice["Type"] == "Choice"

    [rule] = choice["Choices"]
    assert rule["Next"] == "DescribePictureTask"
    # Presence first: a Choice cannot Catch, and comparing a missing path is a
    # States.Runtime fault that would strand the frozen credit.
    assert rule["And"][0] == {"Variable": "$.has_picture", "IsPresent": True}
    assert rule["And"][1] == {"Variable": "$.has_picture", "BooleanEquals": True}
    assert choice["Default"] == "GenerateScriptTask"
    assert states["DescribePictureTask"]["Next"] == "GenerateScriptTask"


def test_describe_picture_retries_transient_bedrock_errors(states) -> None:
    [retry] = states["DescribePictureTask"]["Retry"]
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
