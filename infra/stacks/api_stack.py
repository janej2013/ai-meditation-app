"""HTTP API, JWT authorizer, and the FastAPI Lambda behind it."""

from aws_cdk import CfnOutput, Duration, Stack
from aws_cdk import aws_apigatewayv2 as apigwv2
from aws_cdk import aws_apigatewayv2_authorizers as authorizers
from aws_cdk import aws_apigatewayv2_integrations as integrations
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_lambda as lambda_
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
            environment={"TABLE_NAME": table.table_name},
            description="FastAPI application (Mangum) behind the HTTP API.",
        )
        # Least privilege: this Lambda touches one table and nothing else.
        table.grant_read_write_data(self.api_function)

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

        integration = integrations.HttpLambdaIntegration(
            "ApiIntegration", handler=self.api_function
        )

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

        CfnOutput(
            self,
            "ApiUrl",
            value=self.http_api.api_endpoint,
            description="Base URL of the HTTP API.",
        )
