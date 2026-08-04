"""Persistent data layer: the DynamoDB single table and the audio bucket.

Both resources are retained in prod and destroyed in dev, driven by the ``env``
context value. Later stacks take ``table`` and ``audio_bucket`` as construct
references, which is what lets CDK derive their IAM grants and deployment
order. That is an authoring convenience, not a template one: the wiring still
renders as CloudFormation exports, so neither resource can be renamed or
removed while another stack imports it.
"""

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
)
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_s3 as s3
from constructs import Construct

AUDIO_RETENTION_DAYS = 90


class DataStack(Stack):
    """DynamoDB single table + S3 bucket for generated meditation audio."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        env_name: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        is_prod = env_name == "prod"
        removal_policy = RemovalPolicy.RETAIN if is_prod else RemovalPolicy.DESTROY

        # Single-table design: PK = USER#<cognito_sub>, SK = PROFILE |
        # ENTITLEMENT | SUB#<id> | JOB#<id>. No GSI until a real access pattern
        # needs one.
        self.table = dynamodb.Table(
            self,
            "AppTable",
            partition_key=dynamodb.Attribute(name="PK", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="SK", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            # PITR is a prod-only cost: dev data is disposable and recreated by
            # redeploying the stack.
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=is_prod
            ),
            # Only the Stripe EVENT# dedupe markers carry this attribute, so
            # only they expire. Every other item type omits it and is kept
            # indefinitely -- TTL deletes nothing it is not given a timestamp
            # for. See shared/db.py EVENT_TTL_DAYS.
            time_to_live_attribute="expires_at",
            removal_policy=removal_policy,
        )

        # Delivery is via CloudFront signed URLs (constraint 6), so the bucket
        # stays fully private and needs no CORS configuration.
        self.audio_bucket = s3.Bucket(
            self,
            "AudioBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="ExpireGeneratedAudio",
                    enabled=True,
                    expiration=Duration.days(AUDIO_RETENTION_DAYS),
                    abort_incomplete_multipart_upload_after=Duration.days(7),
                )
            ],
            removal_policy=removal_policy,
            # Lets `cdk destroy` actually succeed in dev; never in prod.
            auto_delete_objects=not is_prod,
        )

        CfnOutput(
            self,
            "TableName",
            value=self.table.table_name,
            description="DynamoDB single table name (TABLE_NAME env var for Lambdas).",
        )
        CfnOutput(
            self,
            "AudioBucketName",
            value=self.audio_bucket.bucket_name,
            description="S3 bucket holding generated meditation audio.",
        )
