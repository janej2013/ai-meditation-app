"""HTTP API, JWT authorizer, and the FastAPI Lambda behind it."""

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_apigatewayv2 as apigwv2
from aws_cdk import aws_apigatewayv2_authorizers as authorizers
from aws_cdk import aws_apigatewayv2_integrations as integrations
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import aws_stepfunctions as sfn
from constructs import Construct

from stacks.paths import BACKEND_DIR

from .data_stack import PICTURE_PREFIX

# Created by hand: the CloudFront URL-signing private key, whose public half
# is registered on the audio distribution. Either a bare PEM or
# {"private_key": "-----BEGIN..."} -- the signer accepts both.
DEFAULT_CLOUDFRONT_KEY_SECRET_NAME = "meditation/cloudfront-signing-key"


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
        audio_domain_name: str,
        cloudfront_key_pair_id: str,
        cloudfront_key_secret_name: str = DEFAULT_CLOUDFRONT_KEY_SECRET_NAME,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Referenced, never created: a generated secret would put the private
        # key in the template and in `cdk diff` (constraint 4).
        cloudfront_key_secret = secretsmanager.Secret.from_secret_name_v2(
            self, "CloudFrontKeySecret", cloudfront_key_secret_name
        )

        self.api_function = lambda_.DockerImageFunction(
            self,
            "ApiFunction",
            code=lambda_.DockerImageCode.from_image_asset(
                str(BACKEND_DIR),
                file="api/Dockerfile",
            ),
            memory_size=512,
            timeout=Duration.seconds(15),
            # An explicit log group, like the pipeline's task Lambdas: the
            # default one is created on first invoke with retention "never
            # expire", which grows storage cost forever.
            log_group=logs.LogGroup(
                self,
                "ApiFunctionLogs",
                retention=logs.RetentionDays.ONE_MONTH,
                removal_policy=RemovalPolicy.DESTROY,
            ),
            environment={
                "TABLE_NAME": table.table_name,
                "AUDIO_BUCKET": audio_bucket.bucket_name,
                "STATE_MACHINE_ARN": state_machine.state_machine_arn,
                # Signing config for GET /jobs/{id}. The key pair id is public;
                # only the ARN of the private key travels here (constraint 4).
                "CLOUDFRONT_AUDIO_DOMAIN": audio_domain_name,
                "CLOUDFRONT_KEY_PAIR_ID": cloudfront_key_pair_id,
                "CLOUDFRONT_KEY_SECRET_ARN": cloudfront_key_secret.secret_arn,
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

        # GET /jobs/{id} mints a CloudFront signed URL, so the Lambda needs the
        # signing key -- and no longer needs s3:GetObject at all. Bytes are
        # served from the edge, and the bucket is reachable only through OAC
        # (constraint 6).
        cloudfront_key_secret.grant_read(self.api_function)

        # POST /pictures/upload signs an S3 POST policy with the Lambda's own
        # credentials, so the Lambda itself must be allowed to put the object.
        # Write-only, and only under pictures/: it never reads a picture back,
        # and it never touches jobs/.
        self.api_function.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:PutObject"],
                resources=[audio_bucket.arn_for_objects(f"{PICTURE_PREFIX}/*")],
            )
        )

        # DELETE /dreamscapes/{id} sweeps the job's audio objects. Delete is
        # granted on jobs/* only -- never pictures/*, whose objects expire by
        # lifecycle rule alone (constraint 9); the listing is likewise fenced
        # to the jobs/ prefix by condition.
        self.api_function.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:DeleteObject"],
                resources=[audio_bucket.arn_for_objects("jobs/*")],
            )
        )
        self.api_function.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:ListBucket"],
                resources=[audio_bucket.bucket_arn],
                conditions={"StringLike": {"s3:prefix": "jobs/*"}},
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
            path="/pictures/upload",
            methods=[apigwv2.HttpMethod.POST],
            integration=integration,
        )
        self.http_api.add_routes(
            path="/jobs/{job_id}",
            methods=[apigwv2.HttpMethod.GET],
            integration=integration,
        )
        self.http_api.add_routes(
            path="/dreamscapes",
            methods=[apigwv2.HttpMethod.GET],
            integration=integration,
        )
        self.http_api.add_routes(
            path="/dreamscapes/{job_id}",
            methods=[apigwv2.HttpMethod.DELETE],
            integration=integration,
        )

        CfnOutput(
            self,
            "ApiUrl",
            value=self.http_api.api_endpoint,
            description="Base URL of the HTTP API.",
        )
