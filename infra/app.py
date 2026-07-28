#!/usr/bin/env python3
"""CDK app entry point.

Deployment environment is selected with CDK context, defaulting to dev:

    cdk synth                 # dev
    cdk synth -c env=dev
    cdk synth -c env=prod

Every stack is named ``Meditation-<env>-<Concern>`` so dev and prod can coexist
in one account.
"""

import os

import aws_cdk as cdk

from stacks.data_stack import DataStack

VALID_ENVS = ("dev", "prod")
REGION = "ap-southeast-2"  # Sydney, per CLAUDE.md


def main() -> None:
    app = cdk.App()

    env_name = app.node.try_get_context("env") or "dev"
    if env_name not in VALID_ENVS:
        raise ValueError(f"context 'env' must be one of {VALID_ENVS}, got {env_name!r}")

    env = cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=REGION,
    )

    DataStack(
        app,
        f"Meditation-{env_name}-Data",
        env_name=env_name,
        env=env,
        description="DynamoDB single table and generated-audio bucket.",
    )

    cdk.Tags.of(app).add("Project", "meditation")
    cdk.Tags.of(app).add("Environment", env_name)

    app.synth()


if __name__ == "__main__":
    main()
