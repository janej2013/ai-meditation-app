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

from stacks.agent_stack import (
    DASHBOARD_METRICS,
    RECOMMENDED_RESERVED_CONCURRENCY,
    split_concurrency,
)
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


def functions(template: assertions.Template) -> dict[str, dict]:
    """Both functions' properties, keyed by engine."""
    found = {}
    for fn in template.find_resources("AWS::Lambda::Function").values():
        found[fn["Properties"]["Environment"]["Variables"]["AGENT_ENGINE"]] = fn["Properties"]
    return found


def function_properties(template: assertions.Template) -> dict:
    """The native function's, where a test means the one that has always
    been there."""
    return functions(template)["native"]


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


def test_two_container_functions_each_with_a_streaming_url(template):
    template.resource_count_is("AWS::Lambda::Function", 2)
    template.resource_count_is("AWS::Lambda::Url", 2)
    for url in template.find_resources("AWS::Lambda::Url").values():
        assert url["Properties"]["AuthType"] == "AWS_IAM"
        assert url["Properties"]["InvokeMode"] == "RESPONSE_STREAM"
    assert set(functions(template)) == {"native", "langgraph"}


def test_the_native_function_keeps_its_logical_id(template):
    """Renaming it would replace the deployed function and its URL."""
    ids = list(template.find_resources("AWS::Lambda::Function"))
    assert any(
        i.startswith("AgentFunction") and not i.startswith("AgentFunctionLangGraph") for i in ids
    )
    assert any(i.startswith("AgentFunctionLangGraph") for i in ids)


def test_both_functions_share_one_image(template):
    """Same build context, same asset hash: one image, pushed once."""
    [native, langgraph] = [
        functions(template)[e]["Code"]["ImageUri"] for e in ("native", "langgraph")
    ]
    assert native == langgraph


def test_the_functions_differ_only_in_their_engine(template):
    native, langgraph = functions(template)["native"], functions(template)["langgraph"]
    for key in ("MemorySize", "Timeout", "Architectures", "PackageType"):
        assert native[key] == langgraph[key]
    native_env = dict(native["Environment"]["Variables"])
    langgraph_env = dict(langgraph["Environment"]["Variables"])
    assert native_env.pop("AGENT_ENGINE") == "native"
    assert langgraph_env.pop("AGENT_ENGINE") == "langgraph"
    assert native_env == langgraph_env


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
    # One quota, two functions: half each.
    assert functions(capped)["native"]["ReservedConcurrentExecutions"] == 5
    assert functions(capped)["langgraph"]["ReservedConcurrentExecutions"] == 5


def test_a_reservation_is_split_exactly_with_the_remainder_to_native():
    assert split_concurrency(None) == (None, None)
    assert split_concurrency(10) == (5, 5)
    assert split_concurrency(3) == (2, 1)
    assert split_concurrency(2) == (1, 1)


@pytest.mark.parametrize("total", [0, 1])
def test_a_reservation_too_small_for_two_engines_fails_the_synth(total):
    with pytest.raises(ValueError, match="one per engine"):
        build_agent_stack(reserved_concurrency=total)


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
    template.resource_count_is("AWS::Logs::LogGroup", 2)
    for group in template.find_resources("AWS::Logs::LogGroup").values():
        assert group["Properties"]["RetentionInDays"] == 30


def test_the_dashboard_compares_the_engines(template):
    """Every metric on it carries the Engine dimension, for both engines."""
    template.resource_count_is("AWS::CloudWatch::Dashboard", 1)
    [dashboard] = template.find_resources("AWS::CloudWatch::Dashboard").values()
    assert dashboard["Properties"]["DashboardName"] == "Meditation-dev-Agent"
    body = str(dashboard["Properties"]["DashboardBody"])
    for name in DASHBOARD_METRICS:
        assert name in body
    for engine in ("native", "langgraph"):
        assert body.count(f'"Engine","{engine}"') >= len(DASHBOARD_METRICS)
    assert "Meditation/Agent" in body


# ----------------------------------------------------------------------
# IAM: the least the runner exercises
# ----------------------------------------------------------------------


def test_table_grant_is_the_api_grant_plus_delete(template):
    table_statements = [
        s for s in statements(template) if any(a.startswith("dynamodb:") for a in actions(s))
    ]
    assert len(table_statements) == 2  # one per function, identical
    [table_statement] = table_statements[:1]

    assert actions(table_statement) == {
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "dynamodb:Query",
        "dynamodb:DeleteItem",
    }


def one_per_function(matches: list[dict]) -> dict:
    """The two functions get identical grants; return the one statement
    they share, having checked there is exactly one each."""
    assert len(matches) == 2 and matches[0] == matches[1]
    return matches[0]


def test_may_start_the_generation_machine_and_nothing_else_there(template):
    sfn_statement = one_per_function(
        [s for s in statements(template) if any(a.startswith("states:") for a in actions(s))]
    )

    assert actions(sfn_statement) == {"states:StartExecution"}


def test_bedrock_grant_covers_streaming(template):
    bedrock = one_per_function(
        [s for s in statements(template) if any(a.startswith("bedrock:") for a in actions(s))]
    )

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

    bedrock = one_per_function(
        [s for s in statements(template) if any(a.startswith("bedrock:") for a in actions(s))]
    )
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
