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
from conftest import build_data_stack

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

    data = build_data_stack(app=app)
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


def test_pictures_requires_a_signed_url(template):
    """The revisit cloud samples the user's own upload through CloudFront; the
    key is unguessable but that is not a policy, a signature is."""
    config = distribution(template, "audio")
    pictures = next(b for b in config["CacheBehaviors"] if b["PathPattern"] == "pictures/*")

    assert pictures["TrustedKeyGroups"]
    assert pictures["ViewerProtocolPolicy"] == "redirect-to-https"


def test_assets_is_not_signed(template):
    """BGM is shared and switchable mid-session.

    Signing it would mean a round trip to the API for every track change, for
    files that carry no user content.
    """
    config = distribution(template, "audio")
    default = config["DefaultCacheBehavior"]

    assert not default.get("TrustedKeyGroups")
    assert not default.get("TrustedSigners")


def test_exactly_the_user_content_paths_are_signed(template):
    """Both directions matter: a path quietly joining the signed set, and --
    the trap the unsigned default makes easy -- user content that never got a
    behaviour and falls through to public. Narration and pictures are user
    content; the shared BGM is not."""
    config = distribution(template, "audio")
    signed = {b["PathPattern"] for b in config["CacheBehaviors"] if b.get("TrustedKeyGroups")}

    assert signed == {"jobs/*", "pictures/*"}


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

    # Locate the policy through the behaviours rather than by counting: a
    # security-headers policy on the site distribution must not break this.
    ref = config["DefaultCacheBehavior"]["ResponseHeadersPolicyId"]
    assert isinstance(ref, dict) and "Ref" in ref, "managed policy id instead of our own"
    assert jobs["ResponseHeadersPolicyId"] == ref

    policies = template.find_resources("AWS::CloudFront::ResponseHeadersPolicy")
    cors = policies[ref["Ref"]]["Properties"]["ResponseHeadersPolicyConfig"]["CorsConfig"]
    assert cors == {
        "AccessControlAllowCredentials": False,
        "AccessControlAllowHeaders": {"Items": ["*"]},
        "AccessControlAllowMethods": {"Items": ["GET", "HEAD", "OPTIONS"]},
        "AccessControlAllowOrigins": {"Items": ["*"]},
        "OriginOverride": True,
    }


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


# ----------------------------------------------------------------------
# The companion agent's behaviour
# ----------------------------------------------------------------------

# CloudFront's managed "CachingDisabled" policy id.
CACHING_DISABLED_ID = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"


def build_with_agent() -> tuple[cdk.Stack, FrontendStack]:
    from conftest import build_agent_stack

    app = cdk.App()
    env = cdk.Environment(account=ACCOUNT, region=REGION)
    data = build_data_stack(app=app)
    agent = build_agent_stack(app=app)
    frontend = FrontendStack(
        app,
        "Frontend",
        env_name="dev",
        audio_bucket=data.audio_bucket,
        agent_function_url=agent.function_url,
        env=env,
        cross_region_references=True,
    )
    return agent, frontend


@pytest.fixture(scope="module")
def agent_template() -> assertions.Template:
    _, frontend = build_with_agent()
    return assertions.Template.from_stack(frontend)


def test_agent_behavior_streams_uncached_over_every_method(agent_template):
    config = distribution(agent_template, "PWA")
    [agent] = [b for b in config["CacheBehaviors"] if b["PathPattern"] == "agent/*"]

    assert agent["CachePolicyId"] == CACHING_DISABLED_ID
    assert {"POST", "DELETE", "GET"} <= set(agent["AllowedMethods"])
    assert agent["Compress"] is False
    assert agent["ViewerProtocolPolicy"] == "redirect-to-https"
    # The Host header must not reach a Function URL; every other viewer
    # header (Authorization, x-amz-content-sha256) must.
    assert "OriginRequestPolicyId" in agent


def test_agent_origin_is_signed_by_origin_access_control(agent_template):
    config = distribution(agent_template, "PWA")
    [agent] = [b for b in config["CacheBehaviors"] if b["PathPattern"] == "agent/*"]
    [origin] = [o for o in config["Origins"] if o["Id"] == agent["TargetOriginId"]]

    assert origin["OriginAccessControlId"]
    assert origin["CustomOriginConfig"]["OriginProtocolPolicy"] == "https-only"
    assert origin["CustomOriginConfig"]["OriginReadTimeout"] == 60
    agent_template.has_resource_properties(
        "AWS::CloudFront::OriginAccessControl",
        {
            "OriginAccessControlConfig": {
                "OriginAccessControlOriginType": "lambda",
                "SigningBehavior": "always",
                "SigningProtocol": "sigv4",
            }
        },
    )


def test_only_this_distribution_may_invoke_the_url(agent_template):
    """The permission lives here, in the distribution's own stack -- the
    reason this wiring has no cycle and needs no wildcard."""
    agent_template.has_resource_properties(
        "AWS::Lambda::Permission",
        {"Action": "lambda:InvokeFunctionUrl", "Principal": "cloudfront.amazonaws.com"},
    )
    [permission] = agent_template.find_resources("AWS::Lambda::Permission").values()
    assert "distribution/" in str(permission["Properties"]["SourceArn"])


def test_without_the_agent_nothing_of_it_exists(template):
    config = distribution(template, "PWA")

    assert not any(b["PathPattern"] == "agent/*" for b in config.get("CacheBehaviors", []))
    template.resource_count_is("AWS::Lambda::Permission", 0)
    assert not [
        oac
        for oac in template.find_resources("AWS::CloudFront::OriginAccessControl").values()
        if oac["Properties"]["OriginAccessControlConfig"]["OriginAccessControlOriginType"]
        == "lambda"
    ]
