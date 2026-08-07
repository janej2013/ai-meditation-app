"""The dev-only duration override on the generate_script Lambda.

Dev caps every generation at a short duration so an end-to-end run costs
almost no LLM or TTS spend. The cap must never reach prod: a prod deploy that
carried it would silently hand every paying user a 1-minute meditation for a
full credit. Nothing fails at synth time either way, so the template is the
place to pin both halves down.
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
from aws_cdk import aws_s3 as s3

from stacks.pipeline_stack import DEV_DURATION_OVERRIDE_MINUTES, PipelineStack

ACCOUNT = "111122223333"
REGION = "ap-southeast-2"
MODEL_ID = "au.anthropic.claude-haiku-4-5-20251001-v1:0"


def build_template(env_name: str) -> assertions.Template:
    """A PipelineStack template with throwaway upstream resources."""
    app = cdk.App()
    env = cdk.Environment(account=ACCOUNT, region=REGION)

    upstream = cdk.Stack(app, "Upstream", env=env)
    table = dynamodb.Table(
        upstream,
        "Table",
        partition_key=dynamodb.Attribute(name="PK", type=dynamodb.AttributeType.STRING),
        sort_key=dynamodb.Attribute(name="SK", type=dynamodb.AttributeType.STRING),
    )
    bucket = s3.Bucket(upstream, "Audio")

    stack = PipelineStack(
        app,
        "Pipeline",
        env_name=env_name,
        table=table,
        audio_bucket=bucket,
        bedrock_model_id=MODEL_ID,
        env=env,
    )
    return assertions.Template.from_stack(stack)


def environment_variables(template: assertions.Template) -> dict[str, dict]:
    """Environment variables per Lambda, keyed by logical id."""
    return {
        name: fn["Properties"].get("Environment", {}).get("Variables", {})
        for name, fn in template.find_resources("AWS::Lambda::Function").items()
    }


def test_dev_generate_script_carries_the_override():
    variables = environment_variables(build_template("dev"))
    generate = [v for name, v in variables.items() if name.startswith("GenerateScript")]

    assert len(generate) == 1
    assert generate[0]["DURATION_MINUTES_OVERRIDE"] == str(DEV_DURATION_OVERRIDE_MINUTES)


def test_prod_carries_no_override_anywhere():
    for name, variables in environment_variables(build_template("prod")).items():
        assert "DURATION_MINUTES_OVERRIDE" not in variables, name
