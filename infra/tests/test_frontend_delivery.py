"""CloudFront delivery: the site bucket, SPA routing, and signed audio.

The audio assertions are the ones that matter. Constraint 6 says generated
narration is delivered as a signed URL, and the failure mode is silent: drop
the trusted key group from ``jobs/*`` and every object becomes world-readable
to anyone who learns a URL, with nothing in synth, deploy or the app's own
behaviour to say so. The mirror-image mistake -- signing ``assets/*`` -- breaks
mid-session BGM switching instead, which at least shows up as a broken player.
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

from stacks.data_stack import DataStack
from stacks.frontend_stack import FrontendStack

ACCOUNT = "111122223333"
REGION = "ap-southeast-2"

# A syntactically valid RSA public key. Never used to sign anything -- CDK only
# passes the PEM through to CloudFront.
TEST_PUBLIC_KEY_PEM = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA1234567890abcdefghijkl
mnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890abcdefghijklmnop
qrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890abcdefghijklmnopqrst
uvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890abcdefghijklmnopqrstuvwx
yzABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890abcdefghijklmnopqrstuvwxyzAB
CDEFGHIJKLMNOPQRSTUVWXYZ1234567890abcdefghijklmnopqrstuvwxyzABCDEF
GQIDAQAB
-----END PUBLIC KEY-----"""


def build(**overrides) -> tuple[DataStack, FrontendStack]:
    app = cdk.App()
    env = cdk.Environment(account=ACCOUNT, region=REGION)

    data = DataStack(app, "Data", env_name="dev", env=env)
    frontend = FrontendStack(
        app,
        "Frontend",
        env_name="dev",
        audio_bucket=data.audio_bucket,
        env=env,
        cross_region_references=True,
        **overrides,
    )
    return data, frontend


@pytest.fixture(scope="module")
def stacks():
    return build(audio_public_key_pem=TEST_PUBLIC_KEY_PEM)


@pytest.fixture(scope="module")
def template(stacks) -> assertions.Template:
    return assertions.Template.from_stack(stacks[1])


def distribution(template: assertions.Template, comment_fragment: str) -> dict:
    """The one distribution whose comment contains ``comment_fragment``."""
    matches = [
        d["Properties"]["DistributionConfig"]
        for d in template.find_resources("AWS::CloudFront::Distribution").values()
        if comment_fragment in str(d["Properties"]["DistributionConfig"].get("Comment", ""))
    ]
    assert len(matches) == 1, comment_fragment
    return matches[0]


# ----------------------------------------------------------------------
# The site bucket
# ----------------------------------------------------------------------


def test_the_site_bucket_blocks_all_public_access(template):
    template.has_resource_properties(
        "AWS::S3::Bucket",
        {
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True,
                "BlockPublicPolicy": True,
                "IgnorePublicAcls": True,
                "RestrictPublicBuckets": True,
            }
        },
    )


def test_the_site_bucket_has_no_website_endpoint(template):
    """A website endpoint would bypass CloudFront and OAC entirely."""
    buckets = template.find_resources("AWS::S3::Bucket")

    assert all("WebsiteConfiguration" not in b["Properties"] for b in buckets.values())


def test_the_site_origin_uses_origin_access_control(template):
    config = distribution(template, "PWA")
    origin = config["Origins"][0]

    assert "OriginAccessControlId" in origin
    # The legacy OAI mechanism would show up here instead.
    assert origin.get("S3OriginConfig", {}).get("OriginAccessIdentity", "") == ""


# ----------------------------------------------------------------------
# SPA routing
# ----------------------------------------------------------------------


@pytest.mark.parametrize("code", [403, 404])
def test_spa_routes_fall_back_to_index(template, code):
    """Client-side routes have no S3 object; both codes must serve the shell.

    403 is not optional: OAC deliberately withholds ListBucket, so S3 answers a
    missing key with AccessDenied rather than NotFound.
    """
    config = distribution(template, "PWA")
    responses = {r["ErrorCode"]: r for r in config["CustomErrorResponses"]}

    assert responses[code]["ResponseCode"] == 200
    assert responses[code]["ResponsePagePath"] == "/index.html"


def test_the_site_serves_index_at_the_root(template):
    assert distribution(template, "PWA")["DefaultRootObject"] == "index.html"


# ----------------------------------------------------------------------
# Audio delivery -- constraint 6
# ----------------------------------------------------------------------


def test_jobs_requires_a_signed_url(template):
    """Generated narration is one user's content and must never be public."""
    config = distribution(template, "audio")
    jobs = next(b for b in config["CacheBehaviors"] if b["PathPattern"] == "jobs/*")

    assert jobs["TrustedKeyGroups"]


def test_assets_is_not_signed(template):
    """BGM is shared and switchable mid-session.

    Signing it would mean a round trip to the API for every track change, for
    files that carry no user content.
    """
    config = distribution(template, "audio")
    default = config["DefaultCacheBehavior"]

    assert not default.get("TrustedKeyGroups")
    assert not default.get("TrustedSigners")


def test_the_audio_distribution_has_exactly_one_signed_behavior(template):
    """Guards against a second path quietly joining the signed set."""
    config = distribution(template, "audio")
    signed = [b["PathPattern"] for b in config["CacheBehaviors"] if b.get("TrustedKeyGroups")]

    assert signed == ["jobs/*"]


def test_the_public_key_reaches_a_key_group(template):
    template.resource_count_is("AWS::CloudFront::PublicKey", 1)
    template.resource_count_is("AWS::CloudFront::KeyGroup", 1)


def test_audio_behaviors_send_cors_headers(template):
    """Web Audio needs crossOrigin reads; without CORS the mix is silent.

    Not the managed SimpleCORS policy: that one skips any request carrying a
    non-safelisted header, and modern Chromium sends ``Priority`` on every
    fetch -- so under SimpleCORS the mix worked in curl and failed in every
    real browser. Two things are pinned here: the policy allows *every*
    request header so no request shape can be disqualified, and both
    behaviours actually reference it (a managed policy id would be a bare
    string, not a Ref).
    """
    config = distribution(template, "audio")
    jobs = next(b for b in config["CacheBehaviors"] if b["PathPattern"] == "jobs/*")

    policies = template.find_resources("AWS::CloudFront::ResponseHeadersPolicy")
    assert len(policies) == 1, "expected exactly the audio CORS policy"
    [policy_id] = policies.keys()
    assert config["DefaultCacheBehavior"]["ResponseHeadersPolicyId"] == {"Ref": policy_id}
    assert jobs["ResponseHeadersPolicyId"] == {"Ref": policy_id}

    template.has_resource_properties(
        "AWS::CloudFront::ResponseHeadersPolicy",
        {
            "ResponseHeadersPolicyConfig": {
                "CorsConfig": {
                    "AccessControlAllowCredentials": False,
                    "AccessControlAllowHeaders": {"Items": ["*"]},
                    "AccessControlAllowMethods": {"Items": ["GET", "HEAD", "OPTIONS"]},
                    "AccessControlAllowOrigins": {"Items": ["*"]},
                    "OriginOverride": True,
                }
            }
        },
    )


def test_without_a_public_key_nothing_is_signed():
    """Synth must work before the operator has generated a key pair.

    The behaviour still exists, so wiring the key later does not move jobs/*.
    """
    _, frontend = build()
    template = assertions.Template.from_stack(frontend)
    config = distribution(template, "audio")

    assert frontend.audio_key_pair_id == ""
    template.resource_count_is("AWS::CloudFront::PublicKey", 0)
    jobs = next(b for b in config["CacheBehaviors"] if b["PathPattern"] == "jobs/*")
    assert not jobs.get("TrustedKeyGroups")


# ----------------------------------------------------------------------
# The audio bucket's origin-access policy
# ----------------------------------------------------------------------


def test_the_audio_bucket_grants_read_to_cloudfront_only(stacks):
    """Written in data_stack, against any distribution in the account.

    Pinning the distribution ARN would make the two stacks reference each
    other; see the comment there. It is still service-scoped and read-only.
    """
    template = assertions.Template.from_stack(stacks[0])
    policies = template.find_resources("AWS::S3::BucketPolicy")

    cloudfront_statements = [
        statement
        for policy in policies.values()
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
        if statement.get("Principal", {}).get("Service") == "cloudfront.amazonaws.com"
    ]

    assert len(cloudfront_statements) == 1
    statement = cloudfront_statements[0]
    assert statement["Action"] == "s3:GetObject"
    assert statement["Effect"] == "Allow"
    assert "distribution/*" in str(statement["Condition"])


def test_the_audio_bucket_is_still_private(stacks):
    template = assertions.Template.from_stack(stacks[0])

    template.has_resource_properties(
        "AWS::S3::Bucket",
        {
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True,
                "BlockPublicPolicy": True,
                "IgnorePublicAcls": True,
                "RestrictPublicBuckets": True,
            }
        },
    )


# ----------------------------------------------------------------------
# Optional custom domain
# ----------------------------------------------------------------------


def test_no_domain_means_no_certificate_and_no_dns(template):
    """`cdk synth` must not depend on a real hosted zone."""
    assert template.find_resources("AWS::CertificateManager::Certificate") == {}
    assert template.find_resources("AWS::Route53::RecordSet") == {}
    assert "Aliases" not in distribution(template, "PWA")


def test_a_domain_adds_a_certificate_and_an_alias_record():
    _, frontend = build(domain_name="app.example.com", hosted_zone_id="Z1234567890ABC")
    template = assertions.Template.from_stack(frontend)

    assert distribution(template, "PWA")["Aliases"] == ["app.example.com"]
    template.resource_count_is("AWS::Route53::RecordSet", 1)


def test_a_domain_without_a_zone_is_rejected():
    """Fail at synth rather than deploy a certificate that never validates."""
    with pytest.raises(ValueError, match="hosted_zone_id"):
        build(domain_name="app.example.com")
