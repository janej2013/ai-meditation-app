"""The Bedrock IAM grant for generate_script.

A cross-region inference profile needs `bedrock:InvokeModel` on the profile ARN
*and* on the foundation model in every region the profile can route to. Getting
this wrong does not fail `cdk synth` or `cdk deploy` -- it fails at runtime with
an opaque AccessDenied, on the one step that costs money to reach. Hence tests.

The region lists mirror `aws bedrock get-inference-profile --query models[].modelArn`.
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
from conftest import ACCOUNT, AU_PROFILE, BARE_MODEL, REGION


@pytest.fixture(scope="module")
def stack(pipeline_stack):
    return pipeline_stack


# ----------------------------------------------------------------------
# Geo prefixes
# ----------------------------------------------------------------------


def test_au_profile_grants_sydney_and_melbourne(stack):
    """Verified against get-inference-profile: au. routes to these two only."""
    resources = stack._bedrock_resources(AU_PROFILE)

    assert resources == [
        f"arn:aws:bedrock:{REGION}:{ACCOUNT}:inference-profile/{AU_PROFILE}",
        f"arn:aws:bedrock:ap-southeast-2::foundation-model/{BARE_MODEL}",
        f"arn:aws:bedrock:ap-southeast-4::foundation-model/{BARE_MODEL}",
    ]


@pytest.mark.parametrize("geo", ["au", "apac", "us", "eu"])
def test_every_known_geo_grants_the_profile_and_at_least_one_model(stack, geo):
    model_id = f"{geo}.{BARE_MODEL}"

    resources = stack._bedrock_resources(model_id)

    assert resources[0].endswith(f":inference-profile/{model_id}")
    assert len(resources) > 1
    # The geo prefix belongs to the profile, never to a foundation model.
    assert all(f"foundation-model/{BARE_MODEL}" in arn for arn in resources[1:])


@pytest.mark.parametrize("geo", ["au", "apac"])
def test_a_locally_servable_geo_includes_the_deploy_region(stack, geo):
    """au. and apac. are the two geos a Sydney deployment can be served from, so
    dropping ap-southeast-2 from either list would push every invocation abroad.

    us. and eu. are deliberately excluded here: they route only to their own
    regions. Choosing one is a data-residency decision, not an IAM bug -- see
    the au./global. reasoning in infra/app.py.
    """
    resources = stack._bedrock_resources(f"{geo}.{BARE_MODEL}")

    assert f"arn:aws:bedrock:{REGION}::foundation-model/{BARE_MODEL}" in resources


def test_a_bare_model_id_needs_only_the_model_arn(stack):
    resources = stack._bedrock_resources(BARE_MODEL)

    assert resources == [f"arn:aws:bedrock:{REGION}::foundation-model/{BARE_MODEL}"]


def test_an_unknown_geo_falls_through_instead_of_raising(stack):
    """Documents the trap: this synths and deploys, then AccessDenies at runtime.

    Whoever adds a new geo prefix should be changing the regions map, not this
    assertion.
    """
    resources = stack._bedrock_resources(f"ca.{BARE_MODEL}")

    assert resources == [f"arn:aws:bedrock:{REGION}::foundation-model/ca.{BARE_MODEL}"]
    assert not any("inference-profile" in arn for arn in resources)


# ----------------------------------------------------------------------
# The rendered policy
# ----------------------------------------------------------------------


def test_the_arns_reach_the_synthesized_policy(stack):
    """Guards the wiring, not just the helper: the ARNs have to land on a real
    InvokeModel statement for the grant to exist at all."""
    template = assertions.Template.from_stack(stack)
    policies = template.find_resources("AWS::IAM::Policy")

    invoke_statements = [
        statement
        for policy in policies.values()
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
        if statement.get("Action") == "bedrock:InvokeModel"
    ]

    assert len(invoke_statements) == 1
    rendered = str(invoke_statements[0]["Resource"])
    assert f"inference-profile/{AU_PROFILE}" in rendered
    assert "ap-southeast-4::foundation-model" in rendered


def test_only_generate_script_may_invoke_bedrock(stack):
    """Least privilege: TTS, mixing and the ledger steps have no Bedrock access."""
    template = assertions.Template.from_stack(stack)
    policies = template.find_resources("AWS::IAM::Policy")

    with_bedrock = [
        name
        for name, policy in policies.items()
        if any(
            "bedrock" in str(statement.get("Action", ""))
            for statement in policy["Properties"]["PolicyDocument"]["Statement"]
        )
    ]

    assert len(with_bedrock) == 1
    assert with_bedrock[0].startswith("GenerateScript")
