"""The Volcano credentials wiring on the synthesize Lambda.

Constraint 4 keeps vendor keys out of plaintext Lambda environment variables,
so the Lambda gets an ARN and reads the value through Secrets Manager. Two
halves have to line up for that to work, and neither fails at synth time:
the env var must be present for the provider to find the secret, and the role
must be allowed to read it. Get either wrong and the pipeline deploys clean and
then fails on the one step that has already spent a credit.
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

from stacks.pipeline_stack import (
    DEFAULT_TTS_PROVIDER,
    DEFAULT_VOLCANO_SECRET_NAME,
    PipelineStack,
)

ACCOUNT = "111122223333"
REGION = "ap-southeast-2"
MODEL_ID = "au.anthropic.claude-haiku-4-5-20251001-v1:0"


def build_stack(**overrides) -> PipelineStack:
    """A PipelineStack with throwaway upstream resources."""
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
        bedrock_model_id=MODEL_ID,
        env=env,
        **overrides,
    )


@pytest.fixture(scope="module")
def template() -> assertions.Template:
    return assertions.Template.from_stack(build_stack())


def synthesize_function(template: assertions.Template) -> dict:
    functions = template.find_resources("AWS::Lambda::Function")
    matches = [fn for name, fn in functions.items() if name.startswith("Synthesize")]
    assert len(matches) == 1, sorted(functions)
    return matches[0]


# ----------------------------------------------------------------------
# Environment
# ----------------------------------------------------------------------


def test_synthesize_receives_the_secret_arn(template):
    variables = synthesize_function(template)["Properties"]["Environment"]["Variables"]

    assert "VOLCANO_SECRET_ARN" in variables
    assert DEFAULT_VOLCANO_SECRET_NAME in str(variables["VOLCANO_SECRET_ARN"])


def test_the_secret_value_never_reaches_the_template(template):
    """Constraint 4: only the ARN travels, and CDK must not create the secret.

    A generated secret would put its value in the template and in `cdk diff`.
    """
    rendered = str(template.to_json())

    assert "AWS::SecretsManager::Secret" not in rendered
    assert "api_key" not in rendered


def test_synthesize_defaults_to_volcano(template):
    variables = synthesize_function(template)["Properties"]["Environment"]["Variables"]

    assert variables["TTS_PROVIDER"] == DEFAULT_TTS_PROVIDER == "volcano"


def test_polly_stays_reachable_as_a_fallback():
    """`-c tts_provider=polly` must need no other change."""
    template = assertions.Template.from_stack(build_stack(tts_provider="polly"))
    variables = synthesize_function(template)["Properties"]["Environment"]["Variables"]

    assert variables["TTS_PROVIDER"] == "polly"
    # The ARN is still injected: switching back and forth is context-only.
    assert "VOLCANO_SECRET_ARN" in variables


def test_the_secret_name_is_overridable():
    template = assertions.Template.from_stack(build_stack(volcano_secret_name="custom/secret"))
    variables = synthesize_function(template)["Properties"]["Environment"]["Variables"]

    assert "custom/secret" in str(variables["VOLCANO_SECRET_ARN"])


# ----------------------------------------------------------------------
# IAM
# ----------------------------------------------------------------------


def secret_read_statements(template: assertions.Template) -> list[tuple[str, dict]]:
    """Every policy statement that can read a Secrets Manager value."""
    return [
        (name, statement)
        for name, policy in template.find_resources("AWS::IAM::Policy").items()
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
        if any(
            str(action).startswith("secretsmanager:Get")
            for action in (
                statement["Action"]
                if isinstance(statement.get("Action"), list)
                else [statement.get("Action", "")]
            )
        )
    ]


def test_synthesize_may_read_the_secret(template):
    statements = secret_read_statements(template)

    assert len(statements) == 1
    name, statement = statements[0]
    assert name.startswith("Synthesize")
    assert DEFAULT_VOLCANO_SECRET_NAME in str(statement["Resource"])


def test_only_synthesize_may_read_the_secret(template):
    """Least privilege: no other pipeline step needs the vendor key."""
    owners = {name for name, _ in secret_read_statements(template)}

    assert all(owner.startswith("Synthesize") for owner in owners)


def test_the_grant_is_scoped_to_one_secret(template):
    """A wildcard here would hand over every secret in the account."""
    _, statement = secret_read_statements(template)[0]

    assert statement["Resource"] != "*"
