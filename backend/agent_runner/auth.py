"""Cognito ID-token verification, done in-process.

The API Lambda never verifies a token itself: API Gateway's JWT authorizer
does, and ``api/deps.py`` only reads the claims. A Function URL behind
CloudFront has no such authorizer -- its IAM auth proves the request came
from CloudFront, not who the user is -- so this module does what the
authorizer did: signature against the pool's JWKS, issuer, audience,
expiry. It then applies exactly the rules ``api/deps.py`` applies to the
claims (ID token only, subject required). The two must not drift;
tests/agent_runner/test_auth.py checks them against each other.

The token travels in ``X-Id-Token``, not ``Authorization``: CloudFront's
origin access control signs every request to the Function URL and, in
doing so, *replaces* the viewer's Authorization header with its own SigV4
one. A bearer token sent that way never reaches the function. The
``Authorization: Bearer`` form is still accepted so a laptop can talk to
uvicorn the ordinary way.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import jwt

from agent_runner.settings import Settings

logger = logging.getLogger(__name__)

# Cognito's marker for its two token types; access tokens carry neither
# `aud` nor `email`, so only ID tokens are accepted (same as api/deps.py).
ID_TOKEN_USE = "id"

# The header the PWA sends the ID token in (see the module docstring for
# why not Authorization).
ID_TOKEN_HEADER = "x-id-token"
_ALGORITHMS = ["RS256"]


class AuthError(Exception):
    """The request carries no acceptable identity. The message is safe to
    return: it never includes the token."""


@dataclass(frozen=True)
class CurrentUser:
    sub: str
    email: str | None = None


KeyResolver = Callable[[str], Any]


class TokenVerifier:
    """Verifies ``Authorization: Bearer <id token>``.

    ``key_resolver`` maps a token to its signing key -- the JWKS client in
    production, a test's own public key otherwise -- so verification is
    tested with real signatures and no network.
    """

    def __init__(self, *, issuer: str, audience: str, key_resolver: KeyResolver) -> None:
        self._issuer = issuer
        self._audience = audience
        self._key_resolver = key_resolver

    @classmethod
    def for_cognito(cls, settings: Settings) -> TokenVerifier:
        # Built once per process: PyJWKClient caches the pool's keys, so a
        # warm invocation verifies without fetching the JWKS again.
        client = jwt.PyJWKClient(settings.jwks_url, cache_keys=True)
        return cls(
            issuer=settings.issuer,
            audience=settings.cognito_client_id,
            key_resolver=lambda token: client.get_signing_key_from_jwt(token).key,
        )

    def verify(self, authorization: str | None, id_token: str | None = None) -> CurrentUser:
        """``id_token`` is the ``X-Id-Token`` header; ``authorization`` the
        Bearer fallback. Through CloudFront only the former can arrive."""
        token = (id_token or "").strip()
        if not token:
            if not authorization:
                raise AuthError(f"missing {ID_TOKEN_HEADER} header")
            scheme, _, bearer = authorization.partition(" ")
            if scheme.lower() != "bearer" or not bearer.strip():
                raise AuthError(
                    f"send the ID token in {ID_TOKEN_HEADER} (or Authorization: Bearer)"
                )
            token = bearer.strip()
        try:
            key = self._key_resolver(token)
            claims = jwt.decode(
                token,
                key,
                algorithms=_ALGORITHMS,
                audience=self._audience,
                issuer=self._issuer,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as exc:
            # The class name says what failed (expired, bad audience...)
            # without echoing anything from the token.
            logger.info("token rejected: %s", type(exc).__name__)
            raise AuthError("invalid token") from None
        except Exception as exc:
            logger.warning("signing key unavailable: %s", type(exc).__name__)
            raise AuthError("invalid token") from None

        if claims.get("token_use") != ID_TOKEN_USE:
            raise AuthError("an ID token is required")
        sub = claims.get("sub")
        if not sub:
            raise AuthError("token carries no subject")
        return CurrentUser(sub=sub, email=claims.get("email"))
