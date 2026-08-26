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


def state_machine_definition(stack: Any, name_contains: str = "generation") -> dict[str, Any]:
    """The synthesized Amazon States Language document of one state machine.

    The pipeline stack holds two (generation, picture), so the machine is
    chosen by its name rather than by resource order. DefinitionString is an
    Fn::Join over literal chunks and CloudFormation tokens (Lambda ARNs). The
    tokens sit inside JSON string values, so substituting a placeholder
    leaves a parseable document -- which is what lets these tests assert on
    the real ASL rather than on CDK's object model.
    """
    from aws_cdk import assertions

    template = assertions.Template.from_stack(stack)
    resources = template.find_resources("AWS::StepFunctions::StateMachine")
    [properties] = [
        r["Properties"]
        for r in resources.values()
        if name_contains in str(r["Properties"].get("StateMachineName", ""))
    ]

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


@pytest.fixture(scope="module")
def picture_states(pipeline_stack) -> Any:
    """The picture machine's states; synthesized once per module."""
    return state_machine_definition(pipeline_stack, name_contains="picture")["States"]


def build_agent_stack(
    model_id: str = "amazon.nova-lite-v1:0",
    app: Any = None,
    reserved_concurrency: int | None = None,
) -> Any:
    """An AgentStack with throwaway upstream resources: a table, a one-state
    machine, a user pool and its client. Pass ``app`` to share one with a
    frontend stack that must reference the function URL."""
    import aws_cdk as cdk
    from aws_cdk import aws_cognito as cognito
    from aws_cdk import aws_dynamodb as dynamodb
    from aws_cdk import aws_stepfunctions as sfn

    from stacks.agent_stack import AgentStack

    app = app or cdk.App()
    env = cdk.Environment(account=ACCOUNT, region=REGION)

    upstream = cdk.Stack(app, "AgentUpstream", env=env)
    table = dynamodb.Table(
        upstream,
        "Table",
        partition_key=dynamodb.Attribute(name="PK", type=dynamodb.AttributeType.STRING),
        sort_key=dynamodb.Attribute(name="SK", type=dynamodb.AttributeType.STRING),
    )
    machine = sfn.StateMachine(
        upstream,
        "Machine",
        definition_body=sfn.DefinitionBody.from_chainable(sfn.Pass(upstream, "Noop")),
    )
    pool = cognito.UserPool(upstream, "Pool")
    client = pool.add_client("Client")

    return AgentStack(
        app,
        "Agent",
        env_name="dev",
        table=table,
        state_machine=machine,
        user_pool=pool,
        user_pool_client=client,
        agent_model_id=model_id,
        reserved_concurrency=reserved_concurrency,
        env=env,
    )
