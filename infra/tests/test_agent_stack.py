"""The companion agent's stack: what it is, and -- as much -- what it is not.

The plan's cost and residency promises (docs/agent-runner-plan.md §6, §12)
are only as real as this file: no always-on resource, no secret, a
concurrency ceiling, a model that cannot route a listener's words offshore,
and exactly the IAM the runner exercises.
"""

from __future__ import annotations

import shutil

import pytest

# aws-cdk-lib runs its constructs through jsii, which shells out to node at
# import time. Skip rather than let an ImportError abort collection and take the
# backend suite down with it. Node is pinned in .prototools -- see README.
if shutil.which("node") is None:  # pragma: no cover - environment guard
    pytest.skip("aws-cdk-lib needs node on PATH", allow_module_level=True)

from aws_cdk import assertions
from conftest import ACCOUNT, AU_PROFILE, REGION, build_agent_stack

from stacks.agent_stack import RECOMMENDED_RESERVED_CONCURRENCY
from stacks.bedrock import bedrock_invoke_resources

REQUIRED_ENV = {
    "TABLE_NAME",
    "STATE_MACHINE_ARN",
    "COGNITO_USER_POOL_ID",
    "COGNITO_CLIENT_ID",
    "AGENT_MODEL_ID",
    "AGENT_ENGINE",
    "LOG_LEVEL",
}


@pytest.fixture(scope="module")
def stack():
    return build_agent_stack()


@pytest.fixture(scope="module")
def template(stack) -> assertions.Template:
    return assertions.Template.from_stack(stack)


def function_properties(template: assertions.Template) -> dict:
    [fn] = template.find_resources("AWS::Lambda::Function").values()
    return fn["Properties"]


def statements(template: assertions.Template) -> list[dict]:
    found: list[dict] = []
    for policy in template.find_resources("AWS::IAM::Policy").values():
        found.extend(policy["Properties"]["PolicyDocument"]["Statement"])
    return found


def actions(statement: dict) -> set[str]:
    action = statement["Action"]
    return set(action) if isinstance(action, list) else {action}


# ----------------------------------------------------------------------
# What is there
# ----------------------------------------------------------------------


def test_one_container_function_with_one_streaming_url(template):
    template.resource_count_is("AWS::Lambda::Function", 1)
    template.resource_count_is("AWS::Lambda::Url", 1)
    template.has_resource_properties(
        "AWS::Lambda::Url", {"AuthType": "AWS_IAM", "InvokeMode": "RESPONSE_STREAM"}
    )


def test_function_shape(template):
    props = function_properties(template)

    assert props["PackageType"] == "Image"
    assert props["MemorySize"] == 512
    assert props["Timeout"] == 120
    assert props["Architectures"] == ["x86_64"]


def test_reserved_concurrency_is_opt_in():
    """A fresh account's quota of 10 refuses any reservation (and already
    caps the function at 10); the ceiling is set once the quota is raised."""
    default = assertions.Template.from_stack(build_agent_stack())
    assert "ReservedConcurrentExecutions" not in function_properties(default)

    capped = assertions.Template.from_stack(
        build_agent_stack(reserved_concurrency=RECOMMENDED_RESERVED_CONCURRENCY)
    )
    assert function_properties(capped)["ReservedConcurrentExecutions"] == 10


def test_environment_is_exactly_what_the_runner_reads(template):
    variables = function_properties(template)["Environment"]["Variables"]

    assert set(variables) == REQUIRED_ENV
    assert variables["AGENT_ENGINE"] == "native"
    assert variables["AGENT_MODEL_ID"] == "amazon.nova-lite-v1:0"
    # Pro-only in every deployed environment; only a laptop run widens it.
    assert "AGENT_ALLOWED_PLANS" not in variables
    assert not any(
        "SECRET" in k.upper() or "KEY" in k.upper() for k in variables if k != "COGNITO_CLIENT_ID"
    )


def test_log_retention_is_bounded(template):
    template.has_resource_properties("AWS::Logs::LogGroup", {"RetentionInDays": 30})


# ----------------------------------------------------------------------
# IAM: the least the runner exercises
# ----------------------------------------------------------------------


def test_table_grant_is_the_api_grant_plus_delete(template):
    [table_statement] = [
        s for s in statements(template) if any(a.startswith("dynamodb:") for a in actions(s))
    ]

    assert actions(table_statement) == {
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "dynamodb:Query",
        "dynamodb:DeleteItem",
    }


def test_may_start_the_generation_machine_and_nothing_else_there(template):
    [sfn_statement] = [
        s for s in statements(template) if any(a.startswith("states:") for a in actions(s))
    ]

    assert actions(sfn_statement) == {"states:StartExecution"}


def test_bedrock_grant_covers_streaming(template):
    [bedrock] = [
        s for s in statements(template) if any(a.startswith("bedrock:") for a in actions(s))
    ]

    assert actions(bedrock) == {"bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"}
    resources = (
        bedrock["Resource"] if isinstance(bedrock["Resource"], list) else [bedrock["Resource"]]
    )
    assert resources == [f"arn:aws:bedrock:{REGION}::foundation-model/amazon.nova-lite-v1:0"]


def test_no_s3_and_no_secrets_access(template):
    for statement in statements(template):
        assert not any(a.startswith(("s3:", "secretsmanager:", "ssm:")) for a in actions(statement))


# ----------------------------------------------------------------------
# What is deliberately absent
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "resource_type",
    [
        "AWS::EC2::VPC",
        "AWS::EC2::NatGateway",
        "AWS::ECS::Cluster",
        "AWS::ECS::Service",
        "AWS::ElasticLoadBalancingV2::LoadBalancer",
        "AWS::SecretsManager::Secret",
        "AWS::Lambda::Alias",
        "AWS::Lambda::Version",
    ],
)
def test_nothing_always_on_and_no_secret(template, resource_type):
    template.resource_count_is(resource_type, 0)


def test_no_provisioned_concurrency(template):
    assert "ProvisionedConcurrencyConfig" not in str(template.to_json())


# ----------------------------------------------------------------------
# Residency
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "model_id",
    ["apac.amazon.nova-lite-v1:0", "us.anthropic.claude-sonnet-4-6-v1:0", "global.anthropic.x"],
)
def test_offshore_profiles_fail_the_synth(model_id):
    with pytest.raises(ValueError, match="leave Australia"):
        build_agent_stack(model_id=model_id)


def test_au_profile_grants_the_profile_and_both_australian_regions():
    stack = build_agent_stack(model_id=AU_PROFILE)
    template = assertions.Template.from_stack(stack)

    [bedrock] = [
        s for s in statements(template) if any(a.startswith("bedrock:") for a in actions(s))
    ]
    assert bedrock["Resource"] == bedrock_invoke_resources(
        REGION, ACCOUNT, AU_PROFILE, allow_offshore=False
    )
    assert bedrock["Resource"] == [
        f"arn:aws:bedrock:{REGION}:{ACCOUNT}:inference-profile/{AU_PROFILE}",
        "arn:aws:bedrock:ap-southeast-2::foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0",
        "arn:aws:bedrock:ap-southeast-4::foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0",
    ]


def test_the_pipeline_keeps_its_offshore_option():
    """The shared helper refuses offshore only when asked to; the pipeline
    may still be pointed at an apac. profile for capacity."""
    assert bedrock_invoke_resources(REGION, ACCOUNT, "apac.amazon.nova-lite-v1:0")[0].endswith(
        "inference-profile/apac.amazon.nova-lite-v1:0"
    )
