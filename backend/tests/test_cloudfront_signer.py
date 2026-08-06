"""The CloudFront URL signer behind GET /jobs/{id}.

These tests generate a throwaway RSA key and verify the produced signature
cryptographically -- CloudFront will do exactly that in production, so a signer
that only *looks* right (wrong padding, wrong digest, policy bytes mangled)
must fail here rather than 403 at the edge.
"""

from __future__ import annotations

import base64
import json
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from api import cloudfront_signer
from api.cloudfront_signer import (
    SigningConfigError,
    load_private_key,
    reset_key_cache,
    signed_url,
)

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = KEY.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode()

DOMAIN = "d111111abcdef8.cloudfront.net"
KEY_PAIR_ID = "K2JCJMDEHXQW5F"


class FakeSecretsClient:
    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.calls = 0
        self.secret_ids: list[str | None] = []

    def get_secret_value(self, **kwargs):
        """boto3 spells the argument SecretId; the payload is fixed either way."""
        self.secret_ids.append(kwargs.get("SecretId"))
        self.calls += 1
        return {"SecretString": self.payload}


@pytest.fixture(autouse=True)
def _signing_env(monkeypatch):
    reset_key_cache()
    monkeypatch.setenv(cloudfront_signer.DOMAIN_ENV_VAR, DOMAIN)
    monkeypatch.setenv(cloudfront_signer.KEY_PAIR_ID_ENV_VAR, KEY_PAIR_ID)
    monkeypatch.delenv(cloudfront_signer.SECRET_ARN_ENV_VAR, raising=False)
    yield
    reset_key_cache()


def prime_key(payload: str = PEM) -> FakeSecretsClient:
    client = FakeSecretsClient(payload)
    load_private_key(secret_arn="arn:secret", client=client)
    return client


# ----------------------------------------------------------------------
# Key loading
# ----------------------------------------------------------------------


def test_the_key_loads_from_a_bare_pem():
    prime_key(PEM)


def test_the_key_loads_from_a_json_document():
    """The console's key/value tab wraps the PEM; both shapes must work."""
    prime_key(json.dumps({"private_key": PEM}))


def test_the_key_is_cached_across_calls():
    client = prime_key()
    load_private_key(secret_arn="arn:secret", client=client)

    assert client.calls == 1


def test_a_non_key_payload_is_rejected():
    with pytest.raises(SigningConfigError, match="PEM"):
        prime_key("not a key at all")


def test_a_json_document_without_the_field_is_rejected():
    with pytest.raises(SigningConfigError, match="private_key"):
        prime_key(json.dumps({"something_else": "x"}))


def test_a_missing_arn_is_a_config_error():
    with pytest.raises(SigningConfigError, match="CLOUDFRONT_KEY_SECRET_ARN"):
        load_private_key()


# ----------------------------------------------------------------------
# URL signing
# ----------------------------------------------------------------------


def test_the_url_targets_the_distribution_not_s3():
    prime_key()

    url = signed_url("jobs/abc/final.mp3")
    parsed = urlparse(url)

    assert parsed.netloc == DOMAIN
    assert parsed.path == "/jobs/abc/final.mp3"
    assert "amazonaws.com" not in url  # constraint 6: no S3 presign


def test_the_url_carries_the_canned_policy_parameters():
    prime_key()

    query = parse_qs(urlparse(signed_url("jobs/abc/final.mp3")).query)

    assert query["Key-Pair-Id"] == [KEY_PAIR_ID]
    assert "Expires" in query
    assert "Signature" in query


def test_the_signature_actually_verifies():
    """Round-trip the canned policy through the public key.

    This is the check CloudFront performs; RSA-SHA1 with PKCS1v15 is the only
    accepted combination, so a signer drifting to SHA256 or PSS fails here.
    """
    prime_key()

    url = signed_url("jobs/abc/final.mp3")
    parsed = urlparse(url)
    query = parse_qs(parsed.query)

    expires = int(query["Expires"][0])
    resource = f"https://{parsed.netloc}{parsed.path}"
    policy = json.dumps(
        {
            "Statement": [
                {
                    "Resource": resource,
                    "Condition": {"DateLessThan": {"AWS:EpochTime": expires}},
                }
            ]
        },
        separators=(",", ":"),
    ).encode()

    # CloudFront's URL-safe base64: + -> -, = -> _, / -> ~
    signature = base64.b64decode(
        query["Signature"][0].replace("-", "+").replace("_", "=").replace("~", "/")
    )

    KEY.public_key().verify(signature, policy, padding.PKCS1v15(), hashes.SHA1())


def test_the_expiry_matches_the_requested_lifetime():
    prime_key()
    import time

    url = signed_url("jobs/abc/final.mp3", expires_in=timedelta(minutes=15))
    expires = int(parse_qs(urlparse(url).query)["Expires"][0])
    horizon = time.time() + 15 * 60

    assert horizon - 120 < expires <= horizon + 5


def test_missing_domain_or_key_pair_id_is_a_config_error(monkeypatch):
    prime_key()
    monkeypatch.delenv(cloudfront_signer.DOMAIN_ENV_VAR)

    with pytest.raises(SigningConfigError):
        signed_url("jobs/abc/final.mp3")
