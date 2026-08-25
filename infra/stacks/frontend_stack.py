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

Domain configuration is optional. With no ``domain_name`` in context the stack
still synthesises and deploys, using the CloudFront default domains -- so
``cdk synth`` never depends on a real hosted zone.
"""

from aws_cdk import Annotations, CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_route53 as route53
from aws_cdk import aws_route53_targets as targets
from aws_cdk import aws_s3 as s3
from constructs import Construct

# CloudFront only reads certificates from us-east-1, wherever the distribution
# itself lives (CLAUDE.md).
CERTIFICATE_REGION = "us-east-1"

# SPA routing: the bucket has exactly one HTML file, and every client-side route
# has to resolve to it. S3 answers a missing key with 403 when the caller lacks
# ListBucket (which OAC deliberately does not grant), so both codes are mapped.
SPA_ERROR_CODES = (403, 404)


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
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        is_prod = env_name == "prod"
        removal_policy = RemovalPolicy.RETAIN if is_prod else RemovalPolicy.DESTROY

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
            ),
            default_root_object="index.html",
            error_responses=[
                cloudfront.ErrorResponse(
                    http_status=code,
                    response_http_status=200,
                    response_page_path="/index.html",
                    # Short: a genuinely missing asset should not be cached as a
                    # page for long, and the SPA shell is cheap to re-fetch.
                    ttl=Duration.seconds(10),
                )
                for code in SPA_ERROR_CODES
            ],
            certificate=certificate,
            domain_names=domain_names,
            comment=f"meditation-{env_name} PWA",
        )

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
