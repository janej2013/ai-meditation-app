"""Cost hygiene the templates must keep.

Two rules that read as billing trivia but bite in production:

- The audio bucket's expiry rule must be scoped to ``jobs/``. The same bucket
  holds the shared BGM under ``assets/``, uploaded once by hand; an unprefixed
  rule deletes it after ``AUDIO_RETENTION_DAYS`` and every session goes
  voice-only without any deploy having changed anything.
- The container Lambdas must own their log groups with bounded retention. The
  implicit group is created on first invoke with retention "never expire" --
  log storage that only ever grows and that no stack update cleans up.
"""

from __future__ import annotations

import shutil

import pytest

# aws-cdk-lib runs its constructs through jsii, which shells out to node at
# import time. Skip rather than let an ImportError abort collection and take the
# backend suite down with it. Node is pinned in .prototools -- see README.
if shutil.which("node") is None:  # pragma: no cover - environment guard
    pytest.skip("aws-cdk-lib needs node on PATH", allow_module_level=True)

import aws_cdk as cdk
from aws_cdk import assertions
from aws_cdk import aws_dynamodb as dynamodb

from stacks.auth_stack import AuthStack
from stacks.data_stack import AUDIO_RETENTION_DAYS, DataStack

ENV = cdk.Environment(account="111122223333", region="ap-southeast-2")
ORIGINS = ["http://localhost:5173"]


def test_audio_expiry_only_touches_jobs() -> None:
    app = cdk.App()
    stack = DataStack(app, "Data", env_name="dev", upload_origins=ORIGINS, env=ENV)
    template = assertions.Template.from_stack(stack)

    buckets = template.find_resources("AWS::S3::Bucket")
    assert len(buckets) == 1, "data stack should own exactly the audio bucket"
    rules = next(iter(buckets.values()))["Properties"]["LifecycleConfiguration"]["Rules"]
    rule = next(r for r in rules if r["Id"] == "ExpireGeneratedAudio")

    # The prefix is the assertion that matters: without it the rule expires
    # assets/ (the shared BGM) along with the per-job narration.
    assert rule["Prefix"] == "jobs/"
    assert rule["ExpirationInDays"] == AUDIO_RETENTION_DAYS


def test_init_user_log_retention_is_bounded() -> None:
    app = cdk.App()
    upstream = cdk.Stack(app, "Upstream", env=ENV)
    table = dynamodb.Table(
        upstream,
        "Table",
        partition_key=dynamodb.Attribute(name="PK", type=dynamodb.AttributeType.STRING),
        sort_key=dynamodb.Attribute(name="SK", type=dynamodb.AttributeType.STRING),
    )
    stack = AuthStack(app, "Auth", env_name="dev", table=table, env=ENV)
    template = assertions.Template.from_stack(stack)

    # An explicit group with a cap, not the implicit never-expiring one.
    template.has_resource_properties("AWS::Logs::LogGroup", {"RetentionInDays": 30})
