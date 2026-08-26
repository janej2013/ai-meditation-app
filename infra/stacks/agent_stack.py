"""The companion agent's Lambda: a container behind a Function URL that
streams its responses.

One invocation is one turn of conversation (docs/agent-runner-plan.md §1,
§6). The image runs FastAPI under the Lambda Web Adapter, which turns the
Function URL invocation into HTTP and streams the reply back as the model
produces it -- the reason this is a Function URL with RESPONSE_STREAM and
not a route on the existing HTTP API, whose integrations buffer and time
out at 30 s.

Nothing here is always-on: no VPC, no load balancer, no provisioned
concurrency, no secret. Bedrock and the table are reached with the
execution role, and the reserved concurrency is the cost ceiling.

The Function URL is IAM-authenticated and only CloudFront may call it: the
site distribution adds an ``agent/*`` behaviour with origin access control
(frontend_stack), which is also what keeps the app same-origin -- no CORS.
User identity is a Cognito ID token the function verifies itself
(agent_runner/auth.py); the URL's IAM auth only proves the caller is our
distribution.
"""

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_stepfunctions as sfn
from constructs import Construct

from stacks.paths import BACKEND_DIR

from .bedrock import bedrock_invoke_resources

# Nova Lite, served on demand from ap-southeast-2. Decided on the A3 evals
# (docs/agent-runner-plan.md §3.1): 19/20 with every crisis case passing, at
# about a cent per full eval run. `-c agent_model_id=au.anthropic...` swaps
# in a Claude profile; anything that could route offshore is refused.
DEFAULT_AGENT_MODEL_ID = "amazon.nova-lite-v1:0"

# The cost ceiling and the abuse backstop in one number: at most this many
# turns run at once, each at most AGENT_TIMEOUT on AGENT_MEMORY_MB. The
# worst case is a few cents a minute; a Function URL reached by someone who
# should not have it cannot spend more than that.
#
# Opt-in (`-c agent_reserved_concurrency=10`) rather than on by default:
# Lambda refuses a reservation that would leave the account under 10
# unreserved executions, and a fresh account's whole quota *is* 10 -- which
# already caps this function at the same number. Set it once the account's
# concurrency quota has been raised (docs/deployment.md).
RECOMMENDED_RESERVED_CONCURRENCY = 10
# A turn's budget. The runner stops asking the model for tools ten seconds
# before this (agent_runner/lambda_context.py) so a turn ends committed.
AGENT_TIMEOUT = Duration.seconds(120)
AGENT_MEMORY_MB = 512


class AgentStack(Stack):
    """The agent Lambda, its Function URL, and the least IAM it needs."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        env_name: str,
        table: dynamodb.ITable,
        state_machine: sfn.IStateMachine,
        user_pool: cognito.IUserPool,
        user_pool_client: cognito.IUserPoolClient,
        agent_model_id: str = DEFAULT_AGENT_MODEL_ID,
        reserved_concurrency: int | None = None,
        log_level: str = "INFO",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Resolved first: an offshore profile fails the synth, not the deploy.
        bedrock_resources = bedrock_invoke_resources(
            self.region, self.account, agent_model_id, allow_offshore=False
        )

        self.function = lambda_.DockerImageFunction(
            self,
            "AgentFunction",
            code=lambda_.DockerImageCode.from_image_asset(
                str(BACKEND_DIR), file="agent_runner/Dockerfile"
            ),
            # The Web Adapter binary the image copies in is the x86_64 build;
            # moving to Graviton means the `-aarch64` tag in the Dockerfile too.
            architecture=lambda_.Architecture.X86_64,
            memory_size=AGENT_MEMORY_MB,
            timeout=AGENT_TIMEOUT,
            reserved_concurrent_executions=reserved_concurrency,
            # An explicit log group with bounded retention, like every other
            # function in this app (see test_cost_hygiene).
            log_group=logs.LogGroup(
                self,
                "AgentFunctionLogs",
                retention=logs.RetentionDays.ONE_MONTH,
                removal_policy=RemovalPolicy.DESTROY,
            ),
            environment={
                # Read once by agent_runner/settings.py; a missing one fails
                # the first request with its name.
                "TABLE_NAME": table.table_name,
                "STATE_MACHINE_ARN": state_machine.state_machine_arn,
                "COGNITO_USER_POOL_ID": user_pool.user_pool_id,
                "COGNITO_CLIENT_ID": user_pool_client.user_pool_client_id,
                "AGENT_MODEL_ID": agent_model_id,
                "AGENT_ENGINE": "native",
                "LOG_LEVEL": log_level,
                # Deliberately absent: AGENT_ALLOWED_PLANS. The runner's
                # default is pro-only; only a laptop run widens it.
            },
            description="Companion agent: FastAPI + SSE on Lambda Web Adapter.",
        )

        # RESPONSE_STREAM is the whole point (SSE); AWS_IAM plus CloudFront's
        # origin access control is what makes the URL unreachable except
        # through the site distribution.
        self.function_url = self.function.add_function_url(
            auth_type=lambda_.FunctionUrlAuthType.AWS_IAM,
            invoke_mode=lambda_.InvokeMode.RESPONSE_STREAM,
        )

        # The API Lambda's grant plus DeleteItem: DELETE /agent/memory removes
        # the MEMORY item outright (shared/db.clear_memory). Still no Scan,
        # no BatchWrite, no streams.
        table.grant(
            self.function,
            "dynamodb:GetItem",
            "dynamodb:PutItem",
            "dynamodb:UpdateItem",
            "dynamodb:Query",
            "dynamodb:DeleteItem",
        )
        # The second permitted starter (CLAUDE.md constraint 2): the confirm
        # route goes through shared/jobs.start_generation(), and the credit
        # is still frozen only inside the state machine.
        state_machine.grant_start_execution(self.function)
        # Both actions: the runner streams (InvokeModelWithResponseStream),
        # and a plain InvokeModel grant alone fails at runtime.
        self.function.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                resources=bedrock_resources,
            )
        )
        # TODO(agent, later): a Bedrock guardrail on the output side (plan
        # §3.3) with bedrock:ApplyGuardrail here and AGENT_GUARDRAIL_ID above.
        # Not started so as not to ship half of it.

        # For debugging with SigV4 tools only. Production traffic reaches the
        # function through the site distribution's agent/* behaviour, and a
        # bare curl of this URL answers 403.
        CfnOutput(
            self,
            "AgentFunctionUrl",
            value=self.function_url.url,
            description="IAM-authenticated Function URL; reachable only via CloudFront.",
        )
