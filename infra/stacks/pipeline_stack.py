"""The generation pipeline: a Standard state machine over small zip Lambdas.

Chain:

    FreezeCredit -> GenerateScript -> Synthesize -> CommitCredit

Every state after the freeze succeeds catches to RollbackCredit, which refunds
the credit and then fails the execution. FreezeCredit is the exception: an
InsufficientCreditsError there means nothing was frozen, so it fails directly
without a refund that would credit a user who never paid.

Retries name concrete error classes rather than States.TaskFailed, so a
validation error or a malformed Bedrock response fails immediately instead of
burning three attempts on something that cannot succeed.

There is no mix step: the PWA mixes background music under the narration in the
browser, which is the only way a listener can switch tracks mid-session. The
pipeline therefore ships narration only, and Synthesize records ``audio_key``.
``backend/functions/mix_audio/`` and its tests are retained for a possible
download/share feature; nothing here deploys them, and no ffmpeg layer is built.
"""

from aws_cdk import Annotations, CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_deployment as s3deploy
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as tasks
from constructs import Construct

from stacks.paths import ASSETS_DIR, BACKEND_DIR, SHARED_LAYER_DIR

# Transport-level Lambda failures. Always worth retrying, never a code bug.
LAMBDA_SERVICE_ERRORS = [
    "Lambda.ServiceException",
    "Lambda.AWSLambdaException",
    "Lambda.SdkClientException",
    "Lambda.TooManyRequestsException",
]

# Raised by shared.pipeline; Step Functions matches the Python class name.
BEDROCK_TRANSIENT = "BedrockTransientError"
TTS_TRANSIENT = "TTSTransientError"
INSUFFICIENT_CREDITS = "InsufficientCreditsError"

# Must exceed the sum of every task timeout multiplied out by its retries, plus
# backoff. MaxAttempts=3 means three retries *after* the first attempt, so
# Synthesize alone can spend 4 x 180s + 14s of backoff = 734s -- more than a
# 10-minute execution budget, which made its own retry policy unrunnable.
#
# This is not a tuning knob. An execution-level timeout does NOT run any Catch:
# the execution is terminated, rollback_credit never fires, and the credit stays
# frozen forever. `frozen >= 1` is also what POST /generate rejects new jobs on,
# so a single stranded job locks the user out permanently with no way back.
# A normal run finishes in about a minute, so the headroom costs nothing.
EXECUTION_TIMEOUT = Duration.minutes(30)
DEFAULT_TASK_TIMEOUT = Duration.seconds(30)


class PipelineStack(Stack):
    """Step Functions state machine plus its five task Lambdas."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        env_name: str,
        table: dynamodb.ITable,
        audio_bucket: s3.IBucket,
        bedrock_model_id: str,
        tts_provider: str = "polly",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        is_prod = env_name == "prod"
        self._warn_if_layers_unbuilt()

        shared_layer = lambda_.LayerVersion(
            self,
            "SharedLayer",
            code=lambda_.Code.from_asset(str(SHARED_LAYER_DIR)),
            compatible_runtimes=[lambda_.Runtime.PYTHON_3_12],
            description="shared package (models, db, tts) and its dependencies.",
        )
        # Royalty-free background music. Deployed to the audio bucket alongside
        # generated audio; the browser fetches these directly (they carry no
        # user content, so they need no signed URL) and mixes one under the
        # narration.
        s3deploy.BucketDeployment(
            self,
            "BgmAssets",
            sources=[s3deploy.Source.asset(str(ASSETS_DIR))],
            destination_bucket=audio_bucket,
            destination_key_prefix="assets",
            prune=False,  # never delete generated job audio under jobs/
            retain_on_delete=is_prod,
        )

        common_env = {"TABLE_NAME": table.table_name, "AUDIO_BUCKET": audio_bucket.bucket_name}

        freeze = self._task_function("FreezeCredit", "freeze_credit", shared_layer, common_env)
        commit = self._task_function("CommitCredit", "commit_credit", shared_layer, common_env)
        rollback = self._task_function(
            "RollbackCredit", "rollback_credit", shared_layer, common_env
        )
        generate = self._task_function(
            "GenerateScript",
            "generate_script",
            shared_layer,
            {**common_env, "BEDROCK_MODEL_ID": bedrock_model_id},
            memory_size=512,
            timeout=Duration.seconds(120),
        )
        synthesize = self._task_function(
            "Synthesize",
            "synthesize",
            shared_layer,
            {**common_env, "TTS_PROVIDER": tts_provider},
            memory_size=1024,
            timeout=Duration.seconds(180),
        )

        self._grant_permissions(
            table=table,
            audio_bucket=audio_bucket,
            bedrock_model_id=bedrock_model_id,
            freeze=freeze,
            generate=generate,
            synthesize=synthesize,
            commit=commit,
            rollback=rollback,
        )

        self.state_machine = self._build_state_machine(
            env_name=env_name,
            freeze=freeze,
            generate=generate,
            synthesize=synthesize,
            commit=commit,
            rollback=rollback,
        )

        CfnOutput(
            self,
            "StateMachineArn",
            value=self.state_machine.state_machine_arn,
            description="Generation pipeline state machine ARN.",
        )

    # ------------------------------------------------------------------
    # Lambdas
    # ------------------------------------------------------------------

    def _task_function(
        self,
        construct_id: str,
        module: str,
        shared_layer: lambda_.ILayerVersion,
        environment: dict[str, str],
        *,
        memory_size: int = 256,
        timeout: Duration | None = None,
    ) -> lambda_.Function:
        """One zip Lambda per pipeline step.

        The asset root is backend/functions rather than the individual step
        directory, so a handler can import its own sibling modules (
        generate_script/prompt.py) with a package-relative import that works
        identically in tests. CDK hashes the asset once, so all five functions
        share a single upload -- including the undeployed mix_audio/ sources,
        which cost a few KB in the zip and nothing at runtime.
        """
        return lambda_.Function(
            self,
            construct_id,
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler=f"{module}.handler.lambda_handler",
            code=lambda_.Code.from_asset(str(BACKEND_DIR / "functions")),
            layers=[shared_layer],
            memory_size=memory_size,
            timeout=timeout or DEFAULT_TASK_TIMEOUT,
            environment=environment,
            # An explicit log group rather than the deprecated log_retention,
            # which provisions a custom resource to do the same job.
            log_group=logs.LogGroup(
                self,
                f"{construct_id}Logs",
                retention=logs.RetentionDays.ONE_MONTH,
                removal_policy=RemovalPolicy.DESTROY,
            ),
            description=f"Pipeline step: {module}.",
        )

    # ------------------------------------------------------------------
    # IAM — each step gets only what it touches
    # ------------------------------------------------------------------

    def _grant_permissions(
        self,
        *,
        table: dynamodb.ITable,
        audio_bucket: s3.IBucket,
        bedrock_model_id: str,
        freeze: lambda_.Function,
        generate: lambda_.Function,
        synthesize: lambda_.Function,
        commit: lambda_.Function,
        rollback: lambda_.Function,
    ) -> None:
        # The credit ledger runs TransactWriteItems containing only Update
        # items, so UpdateItem covers the transaction and GetItem covers the
        # replay path that reads the job back. No PutItem: _put_if_absent
        # belongs to user provisioning, which these steps never reach.
        ledger_actions = ["dynamodb:GetItem", "dynamodb:UpdateItem"]
        for fn in (freeze, commit, rollback):
            table.grant(fn, *ledger_actions)
        table.grant(generate, "dynamodb:GetItem", "dynamodb:UpdateItem")
        # synthesize records audio_key on the JOB item now that it produces the
        # deliverable; the browser mixes the BGM, so no later step touches it.
        table.grant(synthesize, "dynamodb:UpdateItem")

        jobs_prefix = audio_bucket.arn_for_objects("jobs/*")

        # generate_script only ever writes the script.
        generate.add_to_role_policy(
            iam.PolicyStatement(actions=["s3:PutObject"], resources=[jobs_prefix])
        )
        # synthesize reads the script and writes the narration. It never reads
        # assets/: the BGM is fetched by the browser, not by a Lambda.
        synthesize.add_to_role_policy(
            iam.PolicyStatement(actions=["s3:GetObject", "s3:PutObject"], resources=[jobs_prefix])
        )

        generate.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel"],
                resources=self._bedrock_resources(bedrock_model_id),
            )
        )
        synthesize.add_to_role_policy(
            iam.PolicyStatement(actions=["polly:SynthesizeSpeech"], resources=["*"])
        )

    def _bedrock_resources(self, model_id: str) -> list[str]:
        """ARNs a Bedrock InvokeModel call needs.

        A cross-region inference profile requires permission on *both* the
        profile ARN and the underlying foundation model in every region the
        profile can route to -- granting only the profile fails at runtime with
        an opaque AccessDenied. Profile ids are prefixed with their geo
        ("au.", "apac.", "us.", "eu."); a bare model id needs only the model ARN.

        An unrecognised prefix falls through to the bare-model branch, which
        produces an ARN that names the whole profile id as a model and grants
        nothing on the profile itself. Adding a geo here is part of changing
        `bedrock_model_id` to a profile from a geo not already listed.
        """
        geo, _, base_model = model_id.partition(".")
        if geo not in ("au", "apac", "us", "eu"):
            return [f"arn:aws:bedrock:{self.region}::foundation-model/{model_id}"]

        regions = {
            # Australia-only routing: Sydney and Melbourne.
            "au": ["ap-southeast-2", "ap-southeast-4"],
            "apac": [
                "ap-southeast-1",
                "ap-southeast-2",
                "ap-northeast-1",
                "ap-northeast-2",
                "ap-south-1",
            ],
            "us": ["us-east-1", "us-east-2", "us-west-2"],
            "eu": ["eu-central-1", "eu-west-1", "eu-west-3", "eu-north-1"],
        }[geo]

        return [
            f"arn:aws:bedrock:{self.region}:{self.account}:inference-profile/{model_id}",
            *[f"arn:aws:bedrock:{r}::foundation-model/{base_model}" for r in regions],
        ]

    # ------------------------------------------------------------------
    # State machine
    # ------------------------------------------------------------------

    def _build_state_machine(
        self,
        *,
        env_name: str,
        freeze: lambda_.Function,
        generate: lambda_.Function,
        synthesize: lambda_.Function,
        commit: lambda_.Function,
        rollback: lambda_.Function,
    ) -> sfn.StateMachine:
        insufficient = sfn.Fail(
            self,
            "InsufficientCredits",
            error="InsufficientCredits",
            cause="The user had no available credit when the job started.",
        )
        failed = sfn.Fail(
            self,
            "GenerationFailed",
            error="GenerationFailed",
            cause="Generation failed; the frozen credit has been refunded.",
        )
        succeeded = sfn.Succeed(self, "GenerationSucceeded")

        rollback_task = self._task(
            "RollbackCreditTask", rollback, "Refund the frozen credit", Duration.seconds(30)
        )
        rollback_task.add_retry(
            errors=LAMBDA_SERVICE_ERRORS,
            interval=Duration.seconds(2),
            max_attempts=3,
            backoff_rate=2.0,
        )
        rollback_task.next(failed)

        freeze_task = self._task(
            "FreezeCreditTask", freeze, "Reserve one credit", Duration.seconds(30)
        )
        freeze_task.add_retry(
            errors=LAMBDA_SERVICE_ERRORS,
            interval=Duration.seconds(2),
            max_attempts=3,
            backoff_rate=2.0,
        )
        # Order matters: the specific catch must precede States.ALL, and an
        # insufficient-credit failure must NOT refund -- nothing was frozen.
        freeze_task.add_catch(insufficient, errors=[INSUFFICIENT_CREDITS])
        freeze_task.add_catch(rollback_task, errors=["States.ALL"], result_path="$.error")

        generate_task = self._task(
            "GenerateScriptTask", generate, "Generate the script", Duration.seconds(120)
        )
        generate_task.add_retry(
            errors=[BEDROCK_TRANSIENT, *LAMBDA_SERVICE_ERRORS],
            interval=Duration.seconds(2),
            max_attempts=3,
            backoff_rate=2.0,
        )
        generate_task.add_catch(rollback_task, errors=["States.ALL"], result_path="$.error")

        synthesize_task = self._task(
            "SynthesizeTask", synthesize, "Synthesize narration", Duration.seconds(180)
        )
        synthesize_task.add_retry(
            errors=[TTS_TRANSIENT, *LAMBDA_SERVICE_ERRORS],
            interval=Duration.seconds(2),
            max_attempts=3,
            backoff_rate=2.0,
        )
        synthesize_task.add_catch(rollback_task, errors=["States.ALL"], result_path="$.error")

        commit_task = self._task(
            "CommitCreditTask", commit, "Consume the frozen credit", Duration.seconds(30)
        )
        commit_task.add_retry(
            errors=LAMBDA_SERVICE_ERRORS,
            interval=Duration.seconds(2),
            max_attempts=3,
            backoff_rate=2.0,
        )
        # A commit failure refunds rather than leaving the credit frozen: the
        # audio is wasted, but the user is not charged for a job we cannot
        # mark done.
        commit_task.add_catch(rollback_task, errors=["States.ALL"], result_path="$.error")

        definition = freeze_task.next(
            generate_task.next(synthesize_task.next(commit_task.next(succeeded)))
        )

        return sfn.StateMachine(
            self,
            "GenerationStateMachine",
            state_machine_name=f"meditation-{env_name}-generation",
            state_machine_type=sfn.StateMachineType.STANDARD,
            definition_body=sfn.DefinitionBody.from_chainable(definition),
            timeout=EXECUTION_TIMEOUT,
            tracing_enabled=True,
            removal_policy=RemovalPolicy.RETAIN if env_name == "prod" else RemovalPolicy.DESTROY,
        )

    def _task(
        self,
        construct_id: str,
        fn: lambda_.IFunction,
        comment: str,
        timeout: Duration,
    ) -> tasks.LambdaInvoke:
        """A Lambda task that passes the payload straight through.

        payload_response_only unwraps the Lambda envelope so each step receives
        the previous step's PipelineState directly, keeping the state small and
        the handlers free of Step Functions plumbing.
        """
        return tasks.LambdaInvoke(
            self,
            construct_id,
            lambda_function=fn,
            payload_response_only=True,
            comment=comment,
            # task_timeout, not the deprecated timeout=.
            task_timeout=sfn.Timeout.duration(timeout),
            # LambdaInvoke otherwise prepends its own Retry block for transport
            # errors with maxAttempts=6. Step Functions applies the first
            # matching rule, so that default would silently override the
            # explicit 3-attempt policy below. Every state adds
            # LAMBDA_SERVICE_ERRORS itself instead.
            retry_on_service_exceptions=False,
        )

    # ------------------------------------------------------------------

    def _warn_if_layers_unbuilt(self) -> None:
        """Synth succeeds without the layer; deploying without it would not.

        A warning rather than an error so `cdk synth` works from a clean
        checkout (and in CI), while still making the missing step obvious.
        """
        # Look for the package itself, not just any file -- the directory holds
        # a .gitkeep, which would otherwise read as "already built".
        if not (SHARED_LAYER_DIR / "python" / "shared").is_dir():
            Annotations.of(self).add_warning(
                "Lambda layer not built: shared. Run scripts/build_layers.sh "
                "before `cdk deploy` -- synth will succeed but the deployed "
                "pipeline would fail at runtime."
            )
