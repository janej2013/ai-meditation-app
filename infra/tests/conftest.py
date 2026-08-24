"""Shared scaffolding for the CDK tests.

Nothing here imports aws-cdk-lib at module level. It shells out to node through
jsii on import, and a conftest that fails to import aborts collection for the
whole session -- backend suite included. Each test module carries its own
visible module-level skip; these helpers import cdk only when called.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

ACCOUNT = "111122223333"
REGION = "ap-southeast-2"

AU_PROFILE = "au.anthropic.claude-haiku-4-5-20251001-v1:0"
BARE_MODEL = "anthropic.claude-haiku-4-5-20251001-v1:0"


def build_pipeline_stack(model_id: str = AU_PROFILE) -> Any:
    """A PipelineStack with throwaway upstream resources."""
    import aws_cdk as cdk
    from aws_cdk import aws_dynamodb as dynamodb
    from aws_cdk import aws_s3 as s3

    from stacks.pipeline_stack import PipelineStack

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

    return PipelineStack(
        app,
        "Pipeline",
        env_name="dev",
        table=table,
        audio_bucket=bucket,
        bedrock_model_id=model_id,
        env=env,
    )


def state_machine_definition(stack: Any) -> dict[str, Any]:
    """The synthesized Amazon States Language document.

    DefinitionString is an Fn::Join over literal chunks and CloudFormation
    tokens (Lambda ARNs). The tokens sit inside JSON string values, so
    substituting a placeholder leaves a parseable document -- which is what
    lets these tests assert on the real ASL rather than on CDK's object model.
    """
    from aws_cdk import assertions

    template = assertions.Template.from_stack(stack)
    resources = template.find_resources("AWS::StepFunctions::StateMachine")
    properties = next(iter(resources.values()))["Properties"]

    raw = properties["DefinitionString"]
    parts = raw["Fn::Join"][1] if isinstance(raw, dict) else [raw]
    return json.loads("".join(p if isinstance(p, str) else "TOKEN" for p in parts))


@pytest.fixture(scope="module")
def pipeline_stack() -> Any:
    return build_pipeline_stack()


def build_data_stack(upload_origins: list[str] | None = None, app: Any = None) -> Any:
    """A DataStack with test defaults; one call site to absorb signature changes.

    Pass ``app`` when another stack in the same test must reference this one --
    CDK refuses references across apps.
    """
    import aws_cdk as cdk

    from stacks.data_stack import DataStack

    return DataStack(
        app or cdk.App(),
        "Data",
        env_name="dev",
        upload_origins=upload_origins or ["http://localhost:5173"],
        env=cdk.Environment(account=ACCOUNT, region=REGION),
    )


@pytest.fixture(scope="module")
def definition(pipeline_stack) -> Any:
    """The synthesized ASL document, shared so each module synthesizes once."""
    return state_machine_definition(pipeline_stack)


@pytest.fixture(scope="module")
def states(definition) -> Any:
    return definition["States"]
