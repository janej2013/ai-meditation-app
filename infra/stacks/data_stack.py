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
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from constructs import Construct

AUDIO_RETENTION_DAYS = 90

# The backend twin of this constant is shared/models.PICTURE_PREFIX; infra
# cannot import the Lambda package, so a rename must be mirrored by hand --
# the api and pipeline stacks import this one, keeping infra to one copy.
PICTURE_PREFIX = "pictures"

# The tag generate_script puts on intermediates so the lifecycle rule below
# expires them. Twin of shared/audio.TRANSIENT_TAG_KEY/VALUE, mirrored by hand
# for the same reason as PICTURE_PREFIX.
TRANSIENT_TAG = {"transient": "true"}

# Uploaded pictures back the planned replay feature (re-listen without spending
# a credit), so they outlive the job that used them. Nothing in the pipeline
# deletes them; this rule is the only reaper. When replay lands, the audio
# retention above should move to match.
PICTURE_RETENTION_DAYS = 365


class DataStack(Stack):
    """DynamoDB single table + S3 bucket for generated meditation audio."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        env_name: str,
        upload_origins: list[str],
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
        # stays fully private. The one browser-facing door is the picture
        # upload: a presigned POST straight from the PWA, which needs CORS for
        # POST from the site origin and nothing else -- GET never reaches the
        # bucket directly.
        self.audio_bucket = s3.Bucket(
            self,
            "AudioBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            lifecycle_rules=[
                # jobs/ holds two kinds of object with different lifetimes.
                # narration.mp3 is the paid deliverable a dreamscape replays,
                # so it must never expire; script.txt is an intermediate,
                # gone after AUDIO_RETENTION_DAYS. The split is by object tag
                # (generate_script uploads script.txt with transient=true)
                # rather than by key prefix, so no key convention, signed-URL
                # path or read grant moves -- and untagged legacy narrations
                # automatically stop expiring. Legacy script.txt files are
                # also untagged and thus now immortal: a few KB of text,
                # accepted. S3 forbids an abort-multipart clause on a
                # tag-filtered rule, hence the second, prefix-only rule.
                # The bucket also holds the shared BGM under assets/ -- an
                # unprefixed rule would delete it and leave players
                # voice-only, so both rules stay scoped to jobs/.
                s3.LifecycleRule(
                    id="ExpireJobIntermediates",
                    enabled=True,
                    prefix="jobs/",
                    tag_filters=TRANSIENT_TAG,
                    expiration=Duration.days(AUDIO_RETENTION_DAYS),
                ),
                s3.LifecycleRule(
                    id="AbortJobUploads",
                    enabled=True,
                    # Multipart uploads only ever target jobs/ (synthesize).
                    prefix="jobs/",
                    abort_incomplete_multipart_upload_after=Duration.days(7),
                ),
                s3.LifecycleRule(
                    id="ExpireUploadedPictures",
                    enabled=True,
                    prefix=f"{PICTURE_PREFIX}/",
                    expiration=Duration.days(PICTURE_RETENTION_DAYS),
                    abort_incomplete_multipart_upload_after=Duration.days(1),
                ),
            ],
            cors=[
                s3.CorsRule(
                    allowed_methods=[s3.HttpMethods.POST],
                    allowed_origins=upload_origins,
                    allowed_headers=["content-type"],
                    max_age=300,
                )
            ],
            removal_policy=removal_policy,
            # Lets `cdk destroy` actually succeed in dev; never in prod.
            auto_delete_objects=not is_prod,
        )

        # Origin access for the CloudFront distribution that frontend_stack
        # builds over this bucket (constraint 6).
        #
        # The condition names *any* distribution in this account rather than
        # the specific one, and that is load bearing: the distribution has to
        # read the bucket's regional domain name, so pinning its ARN here would
        # make the two stacks reference each other and CDK would refuse to
        # synthesise a dependency cycle. The grant is still narrow -- read-only,
        # service principal cloudfront.amazonaws.com, and scoped to this
        # account's distributions -- and the bucket stays fully private with no
        # public access and no website endpoint.
        self.audio_bucket.add_to_resource_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject"],
                resources=[self.audio_bucket.arn_for_objects("*")],
                principals=[iam.ServicePrincipal("cloudfront.amazonaws.com")],
                conditions={
                    "StringLike": {
                        "AWS:SourceArn": f"arn:aws:cloudfront::{self.account}:distribution/*"
                    }
                },
            )
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
