"""Stripe billing: credentials, and the two routes that use them.

Deliberately owns no Lambda and no HTTP API of its own. It hangs two routes off
the API stack's existing integration and grants that stack's function read
access to the Stripe secret, which keeps billing config together without
duplicating the API surface.

**Why this does not create a cycle.** BillingStack depends on ApiStack (it
needs the HTTP API and the integration) and also mutates ApiStack's function by
adding an environment variable and a policy statement. That would normally be a
cyclic reference -- except the value being injected is an *imported* secret's
ARN. ``from_secret_name_v2`` builds that ARN from the account, region and name,
so it renders as a literal string rather than a cross-stack export, and no
reference points back from ApiStack to here.
"""

from aws_cdk import CfnOutput, Stack
from aws_cdk import aws_apigatewayv2 as apigwv2
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

# Created by hand in the console and merely referenced here: a CDK-generated
# secret would put its value in the CloudFormation template and in `cdk diff`
# output, which is exactly what constraint 4 forbids. Contents:
#
#     {"secret_key": "sk_live_...", "webhook_secret": "whsec_..."}
DEFAULT_STRIPE_SECRET_NAME = "meditation/stripe"


class BillingStack(Stack):
    """Stripe secret wiring plus the /billing routes."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        http_api: apigwv2.IHttpApi,
        integration: apigwv2.HttpRouteIntegration,
        api_function: lambda_.IFunction,
        stripe_secret_name: str = DEFAULT_STRIPE_SECRET_NAME,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.stripe_secret = secretsmanager.Secret.from_secret_name_v2(
            self, "StripeSecret", stripe_secret_name
        )

        # Only the ARN travels as an environment variable; the API Lambda reads
        # the key and the webhook signing secret through Secrets Manager at
        # cold start (constraint 4).
        api_function.add_environment("STRIPE_SECRET_ARN", self.stripe_secret.secret_arn)
        self.stripe_secret.grant_read(api_function)

        http_api.add_routes(
            path="/billing/checkout",
            methods=[apigwv2.HttpMethod.POST],
            integration=integration,
            # No authorizer argument: the HTTP API's default_authorizer applies,
            # so this route needs a Cognito ID token like every other one.
        )

        http_api.add_routes(
            path="/billing/webhook",
            methods=[apigwv2.HttpMethod.POST],
            integration=integration,
            # The one route in this API that is deliberately anonymous.
            #
            # Stripe's servers call it and cannot hold a Cognito token, so the
            # JWT authorizer would reject every real delivery. Authentication
            # is the HMAC signature instead: the handler runs
            # stripe.Webhook.construct_event over the raw body and the
            # Stripe-Signature header before touching any state, and returns
            # 400 without a write if that fails (constraint 5). An attacker who
            # POSTs here without the signing secret can therefore reach the
            # Lambda, but cannot move a credit.
            authorizer=apigwv2.HttpNoneAuthorizer(),
        )

        CfnOutput(
            self,
            "StripeWebhookPath",
            value="/billing/webhook",
            description="Append to the API's base URL to get the Stripe webhook endpoint.",
        )
