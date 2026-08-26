"""The companion agent's Lambdas: two containers -- one per engine -- each
behind a Function URL that streams its responses.

One invocation is one turn of conversation (docs/agent-runner-plan.md §1,
§6). The image runs FastAPI under the Lambda Web Adapter, which turns the
Function URL invocation into HTTP and streams the reply back as the model
produces it -- the reason this is a Function URL with RESPONSE_STREAM and
not a route on the existing HTTP API, whose integrations buffer and time
out at 30 s.

Nothing here is always-on: no VPC, no load balancer, no provisioned
concurrency, no secret. Bedrock and the table are reached with the
execution role, and the reserved concurrency is the cost ceiling.

Two functions, one image (docs/agent-runner-plan.md §3.4): ``AgentFunction``
runs the hand-built engine, ``AgentFunctionLangGraph`` the LangGraph one,
told apart by nothing but ``AGENT_ENGINE``. Both are zero-cost while idle,
so both are always deployed; the site distribution fronts them as
``agent/*`` and ``agent-lg/*`` (frontend_stack), and the ``Engine``
dimension on every metric is what makes the comparison free to collect.

The Function URLs are IAM-authenticated and only CloudFront may call them:
the behaviours use origin access control, which is also what keeps the
app same-origin -- no CORS.
User identity is a Cognito ID token the function verifies itself
(agent_runner/auth.py); the URL's IAM auth only proves the caller is our
distribution.
"""

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_cloudwatch as cloudwatch
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
# turns run at once across BOTH functions (the number is split between
# them, see split_concurrency), each at most AGENT_TIMEOUT on
# AGENT_MEMORY_MB. The worst case is a few cents a minute; a Function URL
# reached by someone who should not have it cannot spend more than that.
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


ENGINES = ("native", "langgraph")
# CloudWatch's first three dashboards are free; this is the account's first.
DASHBOARD_METRICS = (
    "TurnLatency",
    "InputTokens",
    "CacheReadTokens",
    "AgentTurnErrors",
    "ToolErrors",
)


def split_concurrency(total: int | None) -> tuple[int | None, int | None]:
    """One reservation, two functions: the native engine gets the larger
    half of an odd total, and the sum is exactly what was asked for -- it
    is the same account quota either way. Fewer than two cannot give each
    engine one, and a ceiling of zero would switch an engine off, so that
    is refused at synth rather than half-applied at deploy."""
    if total is None:
        return None, None
    if total < 2:
        raise ValueError(
            f"agent_reserved_concurrency={total}: at least 2 is needed, one per engine"
        )
    return total - total // 2, total // 2


class AgentStack(Stack):
    """The two agent Lambdas, their Function URLs, and the least IAM they need."""

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
        environment = {
            # Read once by agent_runner/settings.py; a missing one fails
            # the first request with its name.
            "TABLE_NAME": table.table_name,
            "STATE_MACHINE_ARN": state_machine.state_machine_arn,
            "COGNITO_USER_POOL_ID": user_pool.user_pool_id,
            "COGNITO_CLIENT_ID": user_pool_client.user_pool_client_id,
            "AGENT_MODEL_ID": agent_model_id,
            "LOG_LEVEL": log_level,
            # Deliberately absent: AGENT_ALLOWED_PLANS. The runner's
            # default is pro-only; only a laptop run widens it.
        }
        native_cap, langgraph_cap = split_concurrency(reserved_concurrency)

        # The native function keeps its original logical id: renaming it
        # would replace the deployed function (and its URL) on the next
        # deploy for no reason.
        self.function, self.function_url = self._function(
            "AgentFunction", engine="native", environment=environment, reserved=native_cap
        )
        self.langgraph_function, self.langgraph_function_url = self._function(
            "AgentFunctionLangGraph",
            engine="langgraph",
            environment=environment,
            reserved=langgraph_cap,
        )
        for function in (self.function, self.langgraph_function):
            self._grant(function, table, state_machine, bedrock_resources)

        # For debugging with SigV4 tools only. Production traffic reaches the
        # functions through the site distribution's behaviours, and a bare
        # curl of either URL answers 403.
        CfnOutput(
            self,
            "AgentFunctionUrl",
            value=self.function_url.url,
            description=(
                "IAM-authenticated URL of the native engine; reachable only via CloudFront."
            ),
        )
        CfnOutput(
            self,
            "AgentLangGraphFunctionUrl",
            value=self.langgraph_function_url.url,
            description=(
                "IAM-authenticated URL of the LangGraph engine; reachable only via CloudFront."
            ),
        )

        self.dashboard = self._dashboard(env_name)

    def _function(
        self,
        logical_id: str,
        *,
        engine: str,
        environment: dict[str, str],
        reserved: int | None,
    ) -> tuple[lambda_.DockerImageFunction, lambda_.IFunctionUrl]:
        function = lambda_.DockerImageFunction(
            self,
            logical_id,
            # The same directory and Dockerfile both times: CDK hashes the
            # build context, so the two functions share one image asset and
            # one push (test_agent_stack asserts the ImageUri is identical).
            code=lambda_.DockerImageCode.from_image_asset(
                str(BACKEND_DIR), file="agent_runner/Dockerfile"
            ),
            # The Web Adapter binary the image copies in is the x86_64 build;
            # moving to Graviton means the `-aarch64` tag in the Dockerfile too.
            architecture=lambda_.Architecture.X86_64,
            memory_size=AGENT_MEMORY_MB,
            timeout=AGENT_TIMEOUT,
            reserved_concurrent_executions=reserved,
            # An explicit log group with bounded retention, like every other
            # function in this app (see test_cost_hygiene).
            log_group=logs.LogGroup(
                self,
                f"{logical_id}Logs",
                retention=logs.RetentionDays.ONE_MONTH,
                removal_policy=RemovalPolicy.DESTROY,
            ),
            environment={**environment, "AGENT_ENGINE": engine},
            description=f"Companion agent ({engine} engine): FastAPI + SSE on Lambda Web Adapter.",
        )
        # RESPONSE_STREAM is the whole point (SSE); AWS_IAM plus CloudFront's
        # origin access control is what makes the URL unreachable except
        # through the site distribution.
        url = function.add_function_url(
            auth_type=lambda_.FunctionUrlAuthType.AWS_IAM,
            invoke_mode=lambda_.InvokeMode.RESPONSE_STREAM,
        )
        return function, url

    @staticmethod
    def _grant(
        function: lambda_.IFunction,
        table: dynamodb.ITable,
        state_machine: sfn.IStateMachine,
        bedrock_resources: list[str],
    ) -> None:
        # The API Lambda's grant plus DeleteItem: DELETE /agent/memory removes
        # the MEMORY item outright (shared/db.clear_memory). Still no Scan,
        # no BatchWrite, no streams.
        table.grant(
            function,
            "dynamodb:GetItem",
            "dynamodb:PutItem",
            "dynamodb:UpdateItem",
            "dynamodb:Query",
            "dynamodb:DeleteItem",
        )
        # The second permitted starter (CLAUDE.md constraint 2): the confirm
        # route goes through shared/jobs.start_generation(), and the credit
        # is still frozen only inside the state machine.
        state_machine.grant_start_execution(function)
        # Both actions: the runner streams (InvokeModelWithResponseStream),
        # and a plain InvokeModel grant alone fails at runtime.
        function.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                resources=bedrock_resources,
            )
        )
        # TODO(agent, later): a Bedrock guardrail on the output side (plan
        # §3.3) with bedrock:ApplyGuardrail here and AGENT_GUARDRAIL_ID above.
        # Not started so as not to ship half of it.

    def _dashboard(self, env_name: str) -> cloudwatch.Dashboard:
        """The two engines side by side, from the metrics the runner already
        emits (agent_runner/metrics.py, namespace Meditation/Agent, dimension
        Engine). Where the comparison note's charts come from."""

        def metric(name: str, engine: str, statistic: str) -> cloudwatch.Metric:
            return cloudwatch.Metric(
                namespace="Meditation/Agent",
                metric_name=name,
                dimensions_map={"Engine": engine},
                statistic=statistic,
                label=f"{name} {engine} {statistic}",
                period=Duration.minutes(5),
            )

        def per_engine(name: str, *statistics: str) -> list[cloudwatch.IMetric]:
            return [metric(name, engine, stat) for engine in ENGINES for stat in statistics]

        return cloudwatch.Dashboard(
            self,
            "AgentDashboard",
            dashboard_name=f"Meditation-{env_name}-Agent",
            widgets=[
                [
                    cloudwatch.GraphWidget(
                        title="Turn latency (ms)", left=per_engine("TurnLatency", "p50", "p90")
                    ),
                    cloudwatch.GraphWidget(
                        title="Input tokens vs cache reads",
                        left=per_engine("InputTokens", "Sum")
                        + per_engine("CacheReadTokens", "Sum"),
                    ),
                ],
                [
                    cloudwatch.GraphWidget(
                        title="Errors",
                        left=per_engine("AgentTurnErrors", "Sum") + per_engine("ToolErrors", "Sum"),
                    ),
                ],
            ],
        )
