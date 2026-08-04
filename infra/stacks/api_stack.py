"""HTTP API, JWT authorizer, and the FastAPI Lambda behind it."""

from aws_cdk import CfnOutput, Duration, Stack
from aws_cdk import aws_apigatewayv2 as apigwv2
from aws_cdk import aws_apigatewayv2_authorizers as authorizers
from aws_cdk import aws_apigatewayv2_integrations as integrations
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_stepfunctions as sfn
from constructs import Construct

from stacks.paths import BACKEND_DIR


class ApiStack(Stack):
    """API Gateway HTTP API fronting a container-image FastAPI Lambda."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        env_name: str,
        table: dynamodb.ITable,
        user_pool: cognito.IUserPool,
        user_pool_client: cognito.IUserPoolClient,
        allowed_origins: list[str],
        audio_bucket: s3.IBucket,
        state_machine: sfn.IStateMachine,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.api_function = lambda_.DockerImageFunction(
            self,
            "ApiFunction",
            code=lambda_.DockerImageCode.from_image_asset(
                str(BACKEND_DIR),
                file="api/Dockerfile",
            ),
            memory_size=512,
            timeout=Duration.seconds(15),
            environment={
                "TABLE_NAME": table.table_name,
                "AUDIO_BUCKET": audio_bucket.bucket_name,
                "STATE_MACHINE_ARN": state_machine.state_machine_arn,
            },
            description="FastAPI application (Mangum) behind the HTTP API.",
        )
        # Narrower than grant_read_write_data, which would also hand over Scan,
        # DeleteItem, BatchWriteItem and the stream actions.
        #
        # GetItem/PutItem cover reading the entitlement, lazily creating it, and
        # writing the PENDING job row. UpdateItem is what the Stripe webhook
        # needs: its TransactWriteItems carries an Update on the ENTITLEMENT
        # item, and IAM authorises a transaction per item, not per call. Without
        # it every paid webhook fails AccessDenied and grants nothing.
        #
        # Generation credits are still not mutated here -- freeze, commit and
        # rollback stay in the step Lambdas (constraint 2). Billing is the one
        # entitlement change that legitimately starts at the API, because only
        # Stripe can say a payment happened.
        table.grant(
            self.api_function,
            "dynamodb:GetItem",
            "dynamodb:PutItem",
            "dynamodb:UpdateItem",
        )

        # Starting the pipeline is the API's only write to Step Functions
        # (constraint 2) -- it never describes or stops an execution.
        state_machine.grant_start_execution(self.api_function)

        # GET /jobs/{id} presigns a download. A presigned URL carries the
        # signer's permissions, so the role needs read on generated audio --
        # and nothing else in the bucket.
        self.api_function.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject"],
                resources=[audio_bucket.arn_for_objects("jobs/*")],
            )
        )

        # The authorizer validates signature, issuer, audience and expiry before
        # the Lambda is invoked, so the app only reads claims.
        #
        # Audience is the app client id, which appears as `aud` on Cognito ID
        # tokens. Access tokens carry no `aud` and no `email`, so callers must
        # present an ID token -- api/deps.py enforces that via `token_use`.
        issuer = f"https://cognito-idp.{self.region}.amazonaws.com/{user_pool.user_pool_id}"
        jwt_authorizer = authorizers.HttpJwtAuthorizer(
            "CognitoJwtAuthorizer",
            issuer,
            jwt_audience=[user_pool_client.user_pool_client_id],
            identity_source=["$request.header.Authorization"],
        )

        # default_authorizer makes authentication the default and requires an
        # explicit opt-out, so a route added later is protected by omission
        # rather than exposed by it.
        self.http_api = apigwv2.HttpApi(
            self,
            "HttpApi",
            api_name=f"meditation-{env_name}",
            default_authorizer=jwt_authorizer,
            cors_preflight=apigwv2.CorsPreflightOptions(
                allow_origins=allowed_origins,
                allow_headers=["Authorization", "Content-Type"],
                allow_methods=[
                    apigwv2.CorsHttpMethod.GET,
                    apigwv2.CorsHttpMethod.POST,
                    apigwv2.CorsHttpMethod.OPTIONS,
                ],
                max_age=Duration.hours(1),
            ),
        )

        # Public so BillingStack can hang its own routes off the same Lambda
        # without redefining the integration (which would create a second,
        # identical permission and route target).
        self.integration = integrations.HttpLambdaIntegration(
            "ApiIntegration", handler=self.api_function
        )
        integration = self.integration

        # Preflight is answered by API Gateway before the authorizer runs, so
        # OPTIONS requests do not need credentials.
        self.http_api.add_routes(
            path="/health",
            methods=[apigwv2.HttpMethod.GET],
            integration=integration,
            authorizer=apigwv2.HttpNoneAuthorizer(),
        )
        self.http_api.add_routes(
            path="/account",
            methods=[apigwv2.HttpMethod.GET],
            integration=integration,
        )
        self.http_api.add_routes(
            path="/generate",
            methods=[apigwv2.HttpMethod.POST],
            integration=integration,
        )
        self.http_api.add_routes(
            path="/jobs/{job_id}",
            methods=[apigwv2.HttpMethod.GET],
            integration=integration,
        )

        CfnOutput(
            self,
            "ApiUrl",
            value=self.http_api.api_endpoint,
            description="Base URL of the HTTP API.",
        )
