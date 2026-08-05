#!/usr/bin/env python3
"""CDK app entry point.

Deployment environment is selected with CDK context, defaulting to dev:

    cdk synth                 # dev
    cdk synth -c env=dev
    cdk synth -c env=prod -c allowed_origins=https://app.example.com

Every stack is named ``Meditation-<env>-<Concern>`` so dev and prod can coexist
in one account.
"""

import os

import aws_cdk as cdk

from stacks.api_stack import DEFAULT_CLOUDFRONT_KEY_SECRET_NAME, ApiStack
from stacks.auth_stack import AuthStack
from stacks.billing_stack import DEFAULT_STRIPE_SECRET_NAME, BillingStack
from stacks.data_stack import DataStack
from stacks.frontend_stack import FrontendStack
from stacks.pipeline_stack import (
    DEFAULT_TTS_PROVIDER,
    DEFAULT_VOLCANO_SECRET_NAME,
    PipelineStack,
)

VALID_ENVS = ("dev", "prod")
REGION = "ap-southeast-2"  # Sydney, per CLAUDE.md
DEV_ORIGINS = ["http://localhost:5173"]  # Vite dev server

# Invocation goes through a cross-region inference profile rather than the bare
# model id (CLAUDE.md). The model is listed in ap-southeast-2, but current
# Anthropic models on Bedrock are invoked on demand through a profile, and the
# profile also gives the request more than one region of capacity to land in.
#
# Haiku 4.5 offers an `au.` and a `global.` profile; `au.` is the deliberate
# choice. The mood text a user submits
# is a description of their emotional state, and `global.` would route it to any
# commercial region worldwide, while `au.` keeps inference in Australia. The
# smaller `au.` capacity pool throttles sooner, which the state machine's
# exponential backoff on the Bedrock task already absorbs.
#
# Availability is per-account -- confirm with
#     aws bedrock list-inference-profiles --region ap-southeast-2
# and override with `-c bedrock_model_id=...`. A changed geo prefix also needs a
# matching entry in pipeline_stack._bedrock_resources.
DEFAULT_BEDROCK_MODEL_ID = "au.anthropic.claude-haiku-4-5-20251001-v1:0"


def resolve_allowed_origins(app: cdk.App, env_name: str, domain_name: str | None) -> list[str]:
    """CORS origins from context, with a dev-only default.

    Accepts either a JSON list in cdk.json or a comma-separated ``-c`` value.
    Prod must state its origins explicitly: silently falling back to localhost
    would ship a nonsense CORS policy to a real deployment.

    A configured ``domain_name`` is folded in automatically -- the PWA is served
    from there, so forgetting to list it would leave the app unable to call its
    own API.
    """
    raw = app.node.try_get_context("allowed_origins")
    if isinstance(raw, str):
        origins = [o.strip() for o in raw.split(",") if o.strip()]
    elif isinstance(raw, list):
        origins = [str(o).strip() for o in raw if str(o).strip()]
    else:
        origins = []

    site_origin = f"https://{domain_name}" if domain_name else None
    if site_origin and site_origin not in origins:
        origins.append(site_origin)

    if origins:
        return origins
    if env_name == "prod":
        raise ValueError(
            "context 'allowed_origins' is required for env=prod (or set "
            "'domain_name'), e.g. -c allowed_origins=https://app.example.com"
        )
    return DEV_ORIGINS


def main() -> None:
    app = cdk.App()

    env_name = app.node.try_get_context("env") or "dev"
    if env_name not in VALID_ENVS:
        raise ValueError(f"context 'env' must be one of {VALID_ENVS}, got {env_name!r}")

    domain_name = app.node.try_get_context("domain_name")
    allowed_origins = resolve_allowed_origins(app, env_name, domain_name)

    env = cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=REGION,
    )

    data = DataStack(
        app,
        f"Meditation-{env_name}-Data",
        env_name=env_name,
        env=env,
        description="DynamoDB single table and generated-audio bucket.",
    )

    auth = AuthStack(
        app,
        f"Meditation-{env_name}-Auth",
        env_name=env_name,
        table=data.table,
        env=env,
        description="Cognito user pool, app client, and user provisioning trigger.",
    )

    pipeline = PipelineStack(
        app,
        f"Meditation-{env_name}-Pipeline",
        env_name=env_name,
        table=data.table,
        audio_bucket=data.audio_bucket,
        bedrock_model_id=app.node.try_get_context("bedrock_model_id") or DEFAULT_BEDROCK_MODEL_ID,
        # Volcano Engine is primary; `-c tts_provider=polly` falls back without
        # a code change, and both providers keep their IAM grants either way.
        tts_provider=app.node.try_get_context("tts_provider") or DEFAULT_TTS_PROVIDER,
        volcano_secret_name=(
            app.node.try_get_context("volcano_secret_name") or DEFAULT_VOLCANO_SECRET_NAME
        ),
        env=env,
        description="Step Functions generation pipeline and its task Lambdas.",
    )

    # Before the API: the API needs the audio distribution's domain and key
    # pair id to sign playback URLs. Nothing here points back at the API, so
    # the graph stays acyclic.
    #
    # cross_region_references lets the ACM certificate live in us-east-1 (the
    # only region CloudFront reads certificates from) while the distribution
    # sits in ap-southeast-2.
    frontend = FrontendStack(
        app,
        f"Meditation-{env_name}-Frontend",
        env_name=env_name,
        audio_bucket=data.audio_bucket,
        audio_public_key_pem=app.node.try_get_context("audio_public_key_pem"),
        domain_name=domain_name,
        hosted_zone_id=app.node.try_get_context("hosted_zone_id"),
        env=env,
        cross_region_references=True,
        description="CloudFront delivery for the PWA and for generated audio.",
    )

    api = ApiStack(
        app,
        f"Meditation-{env_name}-Api",
        env_name=env_name,
        table=data.table,
        user_pool=auth.user_pool,
        user_pool_client=auth.user_pool_client,
        allowed_origins=allowed_origins,
        audio_bucket=data.audio_bucket,
        state_machine=pipeline.state_machine,
        audio_domain_name=frontend.audio_distribution.distribution_domain_name,
        cloudfront_key_pair_id=frontend.audio_key_pair_id,
        cloudfront_key_secret_name=(
            app.node.try_get_context("cloudfront_key_secret_name")
            or DEFAULT_CLOUDFRONT_KEY_SECRET_NAME
        ),
        env=env,
        description="HTTP API with Cognito JWT authorizer and the FastAPI Lambda.",
    )

    # Deliberately last, and deliberately owns no Lambda: it adds routes to the
    # API stack's existing integration. The direction of dependency is
    # one-way -- see the cycle note in billing_stack.
    BillingStack(
        app,
        f"Meditation-{env_name}-Billing",
        http_api=api.http_api,
        integration=api.integration,
        api_function=api.api_function,
        stripe_secret_name=(
            app.node.try_get_context("stripe_secret_name") or DEFAULT_STRIPE_SECRET_NAME
        ),
        env=env,
        description="Stripe secret wiring and the /billing routes.",
    )

    cdk.Tags.of(app).add("Project", "meditation")
    cdk.Tags.of(app).add("Environment", env_name)

    app.synth()


if __name__ == "__main__":
    main()
