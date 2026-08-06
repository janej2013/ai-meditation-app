"""Cognito user pool, SPA app client, and the post-confirmation trigger."""

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from constructs import Construct

from stacks.paths import BACKEND_DIR


class AuthStack(Stack):
    """User pool + app client, with a Lambda that provisions new users."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        env_name: str,
        table: dynamodb.ITable,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        is_prod = env_name == "prod"
        removal_policy = RemovalPolicy.RETAIN if is_prod else RemovalPolicy.DESTROY

        # Container image rather than a zip: the handler needs `shared`, which
        # depends on pydantic, and pydantic cannot go in a zip without a
        # separate build step. The image excludes FastAPI to stay small --
        # this function sits in the signup path, so cold start matters.
        self.init_user_function = lambda_.DockerImageFunction(
            self,
            "InitUserFunction",
            code=lambda_.DockerImageCode.from_image_asset(
                str(BACKEND_DIR),
                file="functions/init_user/Dockerfile",
            ),
            memory_size=256,
            # Cognito abandons a synchronous trigger after 5s regardless of
            # this value; the extra headroom lets a slow write still land.
            timeout=Duration.seconds(10),
            # An explicit log group, like the pipeline's task Lambdas: the
            # default one is created on first invoke with retention "never
            # expire", which grows storage cost forever.
            log_group=logs.LogGroup(
                self,
                "InitUserFunctionLogs",
                retention=logs.RetentionDays.ONE_MONTH,
                removal_policy=RemovalPolicy.DESTROY,
            ),
            environment={"TABLE_NAME": table.table_name},
            description="Cognito post-confirmation: create PROFILE + ENTITLEMENT.",
        )
        # Narrower than grant_write_data: this function only ever puts the two
        # provisioning items, so it has no business deleting or updating.
        table.grant(self.init_user_function, "dynamodb:PutItem")

        self.user_pool = cognito.UserPool(
            self,
            "UserPool",
            user_pool_name=f"meditation-{env_name}",
            self_sign_up_enabled=True,
            sign_in_aliases=cognito.SignInAliases(email=True),
            sign_in_case_sensitive=False,
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            standard_attributes=cognito.StandardAttributes(
                email=cognito.StandardAttribute(required=True, mutable=True),
            ),
            user_verification=cognito.UserVerificationConfig(
                email_style=cognito.VerificationEmailStyle.CODE,
            ),
            password_policy=cognito.PasswordPolicy(
                min_length=8,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
                # Symbols add user friction without meaningfully improving
                # entropy at this length; length is the lever that matters.
                require_symbols=False,
            ),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            lambda_triggers=cognito.UserPoolTriggers(
                post_confirmation=self.init_user_function,
            ),
            deletion_protection=is_prod,
            removal_policy=removal_policy,
        )

        self.user_pool_client = self.user_pool.add_client(
            "SpaClient",
            user_pool_client_name=f"meditation-{env_name}-spa",
            # Public browser client: a secret could not be kept secret.
            generate_secret=False,
            auth_flows=cognito.AuthFlow(
                user_srp=True,
                # USER_PASSWORD_AUTH sends the password to Cognito directly.
                # Acceptable for curl-based testing in dev, never in prod.
                user_password=not is_prod,
            ),
            prevent_user_existence_errors=True,
            id_token_validity=Duration.hours(1),
            access_token_validity=Duration.hours(1),
            refresh_token_validity=Duration.days(30),
        )

        CfnOutput(
            self,
            "UserPoolId",
            value=self.user_pool.user_pool_id,
            description="Cognito user pool id.",
        )
        CfnOutput(
            self,
            "UserPoolClientId",
            value=self.user_pool_client.user_pool_client_id,
            description="Cognito SPA app client id (JWT audience).",
        )
