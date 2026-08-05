"""The billing routes and the Stripe secret wiring.

Two of these assertions guard things that cannot fail at synth or deploy time
and would only surface in production: the webhook route must have **no**
authorizer, and the checkout route must **keep** one. Each failure mode is
described on its own test.

Note which template each route assertion reads. ``BillingStack`` calls
``http_api.add_routes()``, but ``HttpApi`` parents every ``HttpRoute`` to
itself, so the resources render in the **API** stack. The billing stack
contributes the routes without owning them.
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
from aws_cdk import aws_stepfunctions as sfn

from stacks.api_stack import ApiStack
from stacks.auth_stack import AuthStack
from stacks.billing_stack import DEFAULT_STRIPE_SECRET_NAME, BillingStack

ACCOUNT = "111122223333"
REGION = "ap-southeast-2"


def build() -> tuple[ApiStack, BillingStack]:
    """An API stack plus the billing stack that hangs routes off it."""
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
    machine = sfn.StateMachine(
        upstream,
        "Machine",
        definition_body=sfn.DefinitionBody.from_chainable(sfn.Pass(upstream, "P")),
    )
    auth = AuthStack(app, "Auth", env_name="dev", table=table, env=env)

    api = ApiStack(
        app,
        "Api",
        env_name="dev",
        table=table,
        user_pool=auth.user_pool,
        user_pool_client=auth.user_pool_client,
        allowed_origins=["http://localhost:5173"],
        audio_bucket=bucket,
        state_machine=machine,
        # Milestone 6 wiring; fixed values keep this test independent of a
        # FrontendStack instance.
        audio_domain_name="d111111abcdef8.cloudfront.net",
        cloudfront_key_pair_id="K2JCJMDEHXQW5F",
        env=env,
    )
    billing = BillingStack(
        app,
        "Billing",
        http_api=api.http_api,
        integration=api.integration,
        api_function=api.api_function,
        env=env,
    )
    return api, billing


@pytest.fixture(scope="module")
def stacks():
    return build()


@pytest.fixture(scope="module")
def api_template(stacks) -> assertions.Template:
    return assertions.Template.from_stack(stacks[0])


@pytest.fixture(scope="module")
def billing_template(stacks) -> assertions.Template:
    return assertions.Template.from_stack(stacks[1])


def routes(template: assertions.Template) -> dict[str, dict]:
    """Every route keyed by its route key ('POST /billing/webhook')."""
    return {
        route["Properties"]["RouteKey"]: route["Properties"]
        for route in template.find_resources("AWS::ApiGatewayV2::Route").values()
    }


# ----------------------------------------------------------------------
# Routes
#
# These assert against the **API** template, not the billing one. BillingStack
# calls http_api.add_routes(), and HttpApi creates each HttpRoute as its own
# child -- so the resources land in the stack that owns the HttpApi. The
# billing stack declares the routes but renders none of them, which is
# surprising enough to be worth pinning down.
# ----------------------------------------------------------------------


def test_both_billing_routes_exist(api_template):
    keys = routes(api_template)

    assert "POST /billing/checkout" in keys
    assert "POST /billing/webhook" in keys


def test_the_webhook_route_is_anonymous(api_template):
    """Stripe cannot present a Cognito token; the signature is the auth.

    The HTTP API sets a default authorizer, so this route is only anonymous
    because it opts out explicitly. Losing that opt-out would 401 every real
    Stripe delivery and silently stop applying payments.
    """
    webhook = routes(api_template)["POST /billing/webhook"]

    assert webhook.get("AuthorizationType") in (None, "NONE")
    assert not webhook.get("AuthorizerId")


def test_the_checkout_route_keeps_the_jwt_authorizer(api_template):
    """Losing it the other way would let anyone bill our Stripe account."""
    checkout = routes(api_template)["POST /billing/checkout"]

    assert checkout.get("AuthorizationType") == "JWT"
    assert checkout.get("AuthorizerId")


def test_the_billing_stack_renders_no_routes_of_its_own(billing_template):
    """Documents where add_routes actually puts things.

    Not a preference -- a future reader looking for these two routes needs to
    know they are in the API stack's template.
    """
    assert routes(billing_template) == {}


def test_billing_adds_no_lambda_of_its_own(billing_template):
    """It reuses the API stack's integration rather than duplicating it."""
    assert billing_template.find_resources("AWS::Lambda::Function") == {}


# ----------------------------------------------------------------------
# Secret wiring
# ----------------------------------------------------------------------


def test_the_api_lambda_receives_the_stripe_secret_arn(api_template):
    functions = api_template.find_resources("AWS::Lambda::Function")
    variables = next(
        fn["Properties"]["Environment"]["Variables"]
        for name, fn in functions.items()
        if name.startswith("ApiFunction")
    )

    assert "STRIPE_SECRET_ARN" in variables
    assert DEFAULT_STRIPE_SECRET_NAME in str(variables["STRIPE_SECRET_ARN"])


def test_the_api_lambda_may_read_the_stripe_secret(api_template):
    statements = [
        statement
        for policy in api_template.find_resources("AWS::IAM::Policy").values()
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

    # Two named secrets since milestone 6: Stripe, and the CloudFront signing
    # key. Each grant is scoped to its one secret; none is a wildcard.
    stripe = [s for s in statements if DEFAULT_STRIPE_SECRET_NAME in str(s["Resource"])]
    assert len(stripe) == 1
    assert all(statement["Resource"] != "*" for statement in statements)


def test_the_stripe_secret_value_never_reaches_a_template(api_template, billing_template):
    """Constraint 4: CDK references the secret, it never creates it.

    A generated secret would put its value in the template and in `cdk diff`.
    """
    for template in (api_template, billing_template):
        rendered = str(template.to_json())
        assert "AWS::SecretsManager::Secret" not in rendered
        assert "secret_key" not in rendered
        assert "webhook_secret" not in rendered


def test_the_api_lambda_may_update_the_entitlement_item(api_template):
    """A third assertion that only production would otherwise catch.

    The webhook applies a payment with TransactWriteItems, and that transaction
    carries an Update on the ENTITLEMENT item beside the Put of the event
    marker. IAM authorises a transaction item by item, so PutItem alone is not
    enough: without UpdateItem every paid webhook fails AccessDenied, grants
    nothing, and 500s back to Stripe until someone notices.

    The set is asserted exactly, so widening the grant is also a deliberate act.
    """
    actions = {
        str(action)
        for policy in api_template.find_resources("AWS::IAM::Policy").values()
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
        for action in (
            statement["Action"]
            if isinstance(statement.get("Action"), list)
            else [statement.get("Action", "")]
        )
        if str(action).startswith("dynamodb:")
    }

    assert actions == {"dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"}


def test_the_api_stack_does_not_import_from_billing(api_template):
    """The dependency runs one way, which is what keeps the graph acyclic.

    BillingStack mutates the API function, but the value it injects is an
    imported secret ARN -- a literal string, not a cross-stack export.
    """
    assert "Billing" not in str(api_template.to_json())
