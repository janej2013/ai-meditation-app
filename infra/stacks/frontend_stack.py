"""CloudFront delivery: the PWA itself, and the generated audio.

Both distributions live here because they are the same concern -- what the
browser fetches directly -- even though one fronts the site bucket and the
other fronts the audio bucket from ``data_stack``.

**Audio delivery (constraint 6)** is the reason the audio distribution is not
optional. Two behaviours, deliberately different:

* ``jobs/*`` -- one user's narration. Trusted key group, so every request needs
  a signed URL the API mints. Nothing here is cacheable across users.
* ``assets/*`` -- the shared background music. Ordinary cached objects, **not**
  signed: the PWA mixes BGM under the narration at playback time and the
  listener can switch tracks mid-session, which a per-object signature would
  turn into a round trip to the API for every switch. These carry no user
  content, so there is nothing to protect.

**The companion agent** rides on the site distribution as an ``agent/*``
behaviour over the agent Lambda's Function URL. Same origin as the PWA, so
no CORS; origin access control so the URL answers only to this distribution.
The dependency runs one way (Frontend imports the URL): CDK creates the
``lambda:InvokeFunctionUrl`` permission *in this stack*, so unlike the audio
bucket (README, Known gaps) nothing has to reference the distribution from
the agent stack and no account-wide wildcard is needed.

Domain configuration is optional. With no ``domain_name`` in context the stack
still synthesises and deploys, using the CloudFront default domains -- so
``cdk synth`` never depends on a real hosted zone.
"""

from aws_cdk import Annotations, Aws, CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_route53 as route53
from aws_cdk import aws_route53_targets as targets
from aws_cdk import aws_s3 as s3
from constructs import Construct

# CloudFront only reads certificates from us-east-1, wherever the distribution
# itself lives (CLAUDE.md).
CERTIFICATE_REGION = "us-east-1"

# SPA routing: the bucket has exactly one HTML file, and every client-side
# route has to resolve to it. Done on the viewer request, for the site
# behaviour only: a path with no extension is an app route and is rewritten
# to /index.html before S3 is asked; a path with one is a file and 404s
# honestly. The alternative -- mapping 403/404 to index.html -- is
# distribution-wide, and would (did) dress the agent origin's 403s and 404s
# up as a 200 page.
SPA_ROUTER_CODE = """\
function handler(event) {
  var request = event.request;
  var last = request.uri.split('/').pop();
  if (last.indexOf('.') === -1) {
    request.uri = '/index.html';
  }
  return request;
}
"""


class FrontendStack(Stack):
    """The PWA's CloudFront distribution, plus the signed audio distribution."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        env_name: str,
        audio_bucket: s3.IBucket,
        audio_public_key_pem: str | None = None,
        domain_name: str | None = None,
        hosted_zone_id: str | None = None,
        agent_function_url: lambda_.IFunctionUrl | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        is_prod = env_name == "prod"
        removal_policy = RemovalPolicy.RETAIN if is_prod else RemovalPolicy.DESTROY

        # None keeps the stack deployable without the agent stack (and keeps
        # the existing tests' constructor calls valid).
        additional_behaviors: dict[str, cloudfront.BehaviorOptions] = {}
        if agent_function_url is not None:
            additional_behaviors["agent/*"] = self._agent_behavior(agent_function_url)

        self.site_bucket = s3.Bucket(
            self,
            "SiteBucket",
            # Private, like the audio bucket. CloudFront reaches it through OAC,
            # so there is no website endpoint and no public read anywhere.
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=removal_policy,
            auto_delete_objects=not is_prod,
        )

        certificate, domain_names = self._resolve_domain(domain_name, hosted_zone_id)

        self.site_distribution = cloudfront.Distribution(
            self,
            "SiteDistribution",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(self.site_bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                compress=True,
                function_associations=[
                    cloudfront.FunctionAssociation(
                        function=cloudfront.Function(
                            self,
                            "SpaRouter",
                            code=cloudfront.FunctionCode.from_inline(SPA_ROUTER_CODE),
                            runtime=cloudfront.FunctionRuntime.JS_2_0,
                            comment="App routes (no extension) resolve to index.html",
                        ),
                        event_type=cloudfront.FunctionEventType.VIEWER_REQUEST,
                    )
                ],
            ),
            additional_behaviors=additional_behaviors,
            default_root_object="index.html",
            certificate=certificate,
            domain_names=domain_names,
            comment=f"meditation-{env_name} PWA",
        )

        if agent_function_url is not None:
            self._grant_dual_auth_invoke(agent_function_url)

        self.audio_distribution, self.audio_key_group, self.audio_key_pair_id = (
            self._build_audio_distribution(
                env_name=env_name,
                audio_bucket=audio_bucket,
                public_key_pem=audio_public_key_pem,
            )
        )

        if domain_name and hosted_zone_id:
            self._add_alias_record(domain_name, hosted_zone_id)

        CfnOutput(
            self,
            "SiteBucketName",
            value=self.site_bucket.bucket_name,
            description="Upload the built PWA here (aws s3 sync frontend/dist ...).",
        )
        CfnOutput(
            self,
            "SiteUrl",
            value=f"https://{domain_name or self.site_distribution.distribution_domain_name}",
            description="Public URL of the PWA.",
        )
        # CI publishes a new PWA build with `aws s3 sync` + an invalidation,
        # and the invalidation API wants the id, which no other output carries.
        CfnOutput(
            self,
            "SiteDistributionId",
            value=self.site_distribution.distribution_id,
            description="For cache invalidation after uploading a new PWA build.",
        )
        CfnOutput(
            self,
            "AudioDomainName",
            value=self.audio_distribution.distribution_domain_name,
            description="CloudFront domain: jobs/* and pictures/* signed, assets/* public.",
        )

    # ------------------------------------------------------------------
    # The companion agent
    # ------------------------------------------------------------------

    def _agent_behavior(self, function_url: lambda_.IFunctionUrl) -> cloudfront.BehaviorOptions:
        """``/agent/*`` -> the agent Lambda's Function URL, signed by CloudFront.

        Every request is a live turn of conversation: nothing is cacheable,
        POST and DELETE must pass, and the reply is a server-sent event stream
        that must not be buffered -- hence no compression. The Host header is
        withheld because a Function URL validates it against its own domain;
        everything else the viewer sends (Content-Type, ``X-Id-Token``, the
        ``x-amz-content-sha256`` that SigV4-signed POSTs need) goes through.
        Not Authorization: the OAC signature *replaces* it on the way to
        the origin, which is why the runner reads the ID token from
        ``X-Id-Token`` (agent_runner/auth.py).
        The 60 s read timeout is the ceiling, not the expectation: the runner
        heartbeats every 15 s while the model is silent.
        """
        origin = origins.FunctionUrlOrigin.with_origin_access_control(
            function_url,
            origin_access_control=cloudfront.FunctionUrlOriginAccessControl(
                self, "AgentOac", signing=cloudfront.Signing.SIGV4_ALWAYS
            ),
            read_timeout=Duration.seconds(60),
        )
        return cloudfront.BehaviorOptions(
            origin=origin,
            allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
            cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
            origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
            viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
            compress=False,
        )

    def _grant_dual_auth_invoke(self, function_url: lambda_.IFunctionUrl) -> None:
        """The second half of the permission CDK's OAC origin grants.

        Function URLs created since October 2025 check *two* actions on the
        caller: ``lambda:InvokeFunctionUrl`` and ``lambda:InvokeFunction``.
        ``FunctionUrlOrigin.with_origin_access_control`` still adds only the
        first (aws/aws-cdk#35872), and the result is a 403 from the URL
        before the function ever runs -- an empty log group and, through
        this distribution's SPA error mapping, a 200 with index.html. Same
        principal, same distribution-scoped condition, same stack.
        """
        lambda_.CfnPermission(
            self,
            "AgentInvokeFunctionForOac",
            action="lambda:InvokeFunction",
            function_name=function_url.function_arn,
            principal="cloudfront.amazonaws.com",
            source_arn=(
                f"arn:{Aws.PARTITION}:cloudfront::{Aws.ACCOUNT_ID}:distribution/"
                f"{self.site_distribution.distribution_id}"
            ),
        )

    # ------------------------------------------------------------------
    # Audio delivery
    # ------------------------------------------------------------------

    def _build_audio_distribution(
        self,
        *,
        env_name: str,
        audio_bucket: s3.IBucket,
        public_key_pem: str | None,
    ) -> tuple[cloudfront.Distribution, cloudfront.KeyGroup | None, str]:
        """Signed narration, unsigned BGM, one distribution.

        Returns the key pair id as well: the API needs it to sign, and it is
        not a secret -- it names the public half CloudFront already holds.
        """
        # Re-imported by name rather than used directly. Handed the real
        # construct, S3BucketOrigin would append an origin-access policy naming
        # this distribution -- and that policy belongs to the *data* stack,
        # which the distribution already reads from. CDK rejects the resulting
        # cycle. An imported bucket has no policy CDK will manage, so the grant
        # lives in data_stack instead, written against any distribution in the
        # account.
        origin_bucket = s3.Bucket.from_bucket_attributes(
            self,
            "AudioOriginBucket",
            bucket_name=audio_bucket.bucket_name,
            region=self.region,
        )
        audio_origin = origins.S3BucketOrigin.with_origin_access_control(origin_bucket)
        # The warning this silences says CDK cannot write the imported bucket's
        # policy -- which is the arrangement, not an oversight: see the comment
        # above and the OAC grant in data_stack.py.
        Annotations.of(self).acknowledge_warning(
            "@aws-cdk/aws-cloudfront-origins:updateImportedBucketPolicyOac",
            "The OAC read policy is written by data_stack.py on the real bucket; "
            "adding it here would create a cross-stack cycle.",
        )

        # Replaces the managed SimpleCORS policy, which was the playback bug:
        # SimpleCORS only acts on requests CloudFront classifies as *simple*
        # CORS, and modern Chromium sends ``Priority: u=1, i`` on every fetch.
        # That header is not CORS-safelisted, so CloudFront withheld
        # Access-Control-Allow-Origin from exactly the browsers users run,
        # while curl -- which sends no Priority header -- kept seeing it.
        # ``allow_headers=["*"]`` is the load-bearing difference: no request
        # header can disqualify a request from getting the CORS response
        # headers. (A static custom header would sidestep classification
        # entirely, but the CloudFront API rejects ACAO as a custom header,
        # so the CORS section is the only route.) ``*`` origins is right for
        # both behaviours: access control on jobs/* is the signed URL, not
        # CORS, and assets/* is deliberately public.
        cors_headers = cloudfront.ResponseHeadersPolicy(
            self,
            "AudioCorsHeaders",
            comment=f"meditation-{env_name} audio CORS allow-all",
            cors_behavior=cloudfront.ResponseHeadersCorsBehavior(
                access_control_allow_credentials=False,
                access_control_allow_headers=["*"],
                access_control_allow_methods=["GET", "HEAD", "OPTIONS"],
                access_control_allow_origins=["*"],
                origin_override=True,
            ),
        )

        key_group = None
        # Empty until a public key is configured. The API surfaces that as a
        # runtime signing error rather than a synth failure, so `cdk synth`
        # works without the operator's key material.
        key_pair_id = ""
        if public_key_pem:
            public_key = cloudfront.PublicKey(
                self,
                "AudioSigningPublicKey",
                encoded_key=public_key_pem,
                comment=f"meditation-{env_name} audio URL signing",
            )
            key_group = cloudfront.KeyGroup(
                self,
                "AudioSigningKeyGroup",
                items=[public_key],
                comment=f"meditation-{env_name} trusted signers",
            )
            key_pair_id = public_key.public_key_id

        # assets/* is the default: unsigned and cacheable. Making the *signed*
        # behaviours the explicit ones means a new path added later is public
        # by naming rather than private by accident -- but the two paths that
        # carry user content, jobs/* and pictures/*, are pinned below and can
        # never fall through here.
        distribution = cloudfront.Distribution(
            self,
            "AudioDistribution",
            default_behavior=cloudfront.BehaviorOptions(
                origin=audio_origin,
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                # The PWA fetches BGM with crossOrigin="anonymous" so Web Audio
                # can read the buffer; without these headers the mix is silent.
                response_headers_policy=cors_headers,
                compress=False,  # already-compressed audio
            ),
            additional_behaviors={
                "jobs/*": cloudfront.BehaviorOptions(
                    origin=audio_origin,
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                    # Signed URLs only. Without a key group configured this
                    # behaviour is still separate, so wiring the key later does
                    # not move the path.
                    trusted_key_groups=[key_group] if key_group else None,
                    cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                    response_headers_policy=cors_headers,
                    compress=False,
                ),
                # A user's uploaded picture, re-sampled into the cloud when a
                # dreamscape is revisited. User content, so signed like the
                # narration -- and without this behaviour the path would fall
                # through to the unsigned default above. CORS because the
                # cloud samples pixels through an anonymous cross-origin image.
                "pictures/*": cloudfront.BehaviorOptions(
                    origin=audio_origin,
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                    trusted_key_groups=[key_group] if key_group else None,
                    cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                    response_headers_policy=cors_headers,
                    compress=False,
                ),
            },
            comment=f"meditation-{env_name} audio",
        )
        return distribution, key_group, key_pair_id

    # ------------------------------------------------------------------
    # Optional custom domain
    # ------------------------------------------------------------------

    def _resolve_domain(
        self, domain_name: str | None, hosted_zone_id: str | None
    ) -> tuple[acm.ICertificate | None, list[str] | None]:
        """Certificate and aliases, or (None, None) for the default domain.

        Returning None for both is what lets `cdk synth` work without a real
        hosted zone -- the distribution simply serves on its CloudFront domain.
        """
        if not domain_name:
            return None, None

        if not hosted_zone_id:
            raise ValueError(
                "context 'hosted_zone_id' is required alongside 'domain_name': "
                "the certificate needs DNS validation in that zone"
            )

        zone = route53.HostedZone.from_hosted_zone_attributes(
            self,
            "HostedZone",
            hosted_zone_id=hosted_zone_id,
            zone_name=_apex_of(domain_name),
        )
        # DnsValidatedCertificate is deprecated; this creates the certificate in
        # us-east-1 via a cross-region reference, which is why the stack must be
        # constructed with cross_region_references=True.
        certificate = acm.Certificate(
            self,
            "SiteCertificate",
            domain_name=domain_name,
            validation=acm.CertificateValidation.from_dns(zone),
        )
        return certificate, [domain_name]

    def _add_alias_record(self, domain_name: str, hosted_zone_id: str) -> None:
        zone = route53.HostedZone.from_hosted_zone_attributes(
            self,
            "AliasZone",
            hosted_zone_id=hosted_zone_id,
            zone_name=_apex_of(domain_name),
        )
        route53.ARecord(
            self,
            "SiteAliasRecord",
            zone=zone,
            record_name=domain_name,
            target=route53.RecordTarget.from_alias(
                targets.CloudFrontTarget(self.site_distribution)
            ),
        )


def _apex_of(domain_name: str) -> str:
    """The registrable domain a subdomain belongs to.

    ``app.example.com`` -> ``example.com``; an apex is returned unchanged.
    Good enough for the two-label TLDs this project uses; a name under a
    multi-part TLD would need the zone name passed in explicitly.
    """
    parts = domain_name.split(".")
    return ".".join(parts[-2:]) if len(parts) > 2 else domain_name
