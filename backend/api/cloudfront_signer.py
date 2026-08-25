"""CloudFront signed URLs for generated audio.

Constraint 6: narration is delivered as a CloudFront signed URL to an S3
object, never streamed through Lambda and never served from a public bucket.

The private key is a secret (constraint 4), so only ``CLOUDFRONT_KEY_SECRET_ARN``
is injected and the PEM is read through Secrets Manager once per container.
The key pair id is not a secret and travels as a plain environment variable.

Signed paths are ``jobs/*`` (narration) and ``pictures/*`` (uploads).
served as ordinary cached objects so the PWA can switch tracks mid-session
without asking the API for a new signature.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
from botocore.signers import CloudFrontSigner
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

logger = logging.getLogger(__name__)

SECRET_ARN_ENV_VAR = "CLOUDFRONT_KEY_SECRET_ARN"
KEY_PAIR_ID_ENV_VAR = "CLOUDFRONT_KEY_PAIR_ID"
DOMAIN_ENV_VAR = "CLOUDFRONT_AUDIO_DOMAIN"

# Long enough to start playback on a slow connection, short enough that a
# leaked URL stops working quickly. The PWA re-reads GET /jobs/{id} if a
# playback attempt 403s.
DEFAULT_EXPIRY = timedelta(minutes=15)


class SigningConfigError(RuntimeError):
    """CloudFront signing is not configured or the key is unusable."""


_private_key: Any = None


def load_private_key(secret_arn: str | None = None, client: Any = None) -> Any:
    """Read and parse the signing key, caching for the container's lifetime.

    The secret is either a bare PEM or a JSON document with a ``private_key``
    field -- both are accepted because the console's "plaintext" and
    "key/value" tabs produce different shapes and picking the wrong one is an
    easy, confusing mistake to make.
    """
    global _private_key
    if _private_key is not None:
        return _private_key

    arn = secret_arn or os.environ.get(SECRET_ARN_ENV_VAR)
    if not arn:
        raise SigningConfigError(f"{SECRET_ARN_ENV_VAR} is not set")

    secrets = client if client is not None else boto3.client("secretsmanager")
    try:
        payload = secrets.get_secret_value(SecretId=arn)["SecretString"]
    except Exception as exc:
        # Terse on purpose: nothing about the key's content may reach a log.
        raise SigningConfigError("could not read the CloudFront signing key") from exc

    pem = _extract_pem(payload)
    try:
        _private_key = serialization.load_pem_private_key(pem.encode(), password=None)
    except (ValueError, TypeError) as exc:
        raise SigningConfigError("CloudFront signing key is not a valid PEM private key") from exc
    return _private_key


def reset_key_cache() -> None:
    """Drop the cached key. For tests; production reads once per container."""
    global _private_key
    _private_key = None


def _extract_pem(payload: str) -> str:
    stripped = payload.strip()
    if stripped.startswith("-----BEGIN"):
        return stripped
    try:
        document = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise SigningConfigError("CloudFront signing secret is neither PEM nor JSON") from exc

    pem = document.get("private_key")
    if not pem:
        raise SigningConfigError("CloudFront signing secret has no 'private_key' field")
    return pem


def signed_url(key: str, expires_in: timedelta = DEFAULT_EXPIRY) -> str:
    """A signed CloudFront URL for a user-content key (``jobs/``, ``pictures/``)."""
    domain = os.environ.get(DOMAIN_ENV_VAR)
    key_pair_id = os.environ.get(KEY_PAIR_ID_ENV_VAR)
    if not domain or not key_pair_id:
        raise SigningConfigError(f"{DOMAIN_ENV_VAR} and {KEY_PAIR_ID_ENV_VAR} must both be set")

    private_key = load_private_key()

    def rsa_signer(message: bytes) -> bytes:
        # CloudFront canned policies are RSA-SHA1. Not a choice -- the service
        # rejects anything else.
        return private_key.sign(message, padding.PKCS1v15(), hashes.SHA1())

    signer = CloudFrontSigner(key_pair_id, rsa_signer)
    return signer.generate_presigned_url(
        f"https://{domain}/{key.lstrip('/')}",
        date_less_than=datetime.now(UTC) + expires_in,
    )
