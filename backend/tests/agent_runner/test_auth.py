"""In-process token verification, and that it agrees with api/deps.py."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi import HTTPException

from agent_runner.auth import AuthError, TokenVerifier
from api.deps import get_current_user

from .conftest import CLIENT_ID, ISSUER, Api, make_token


def test_valid_id_token_is_accepted(app, token):
    response = Api(app, token).request("GET", "/agent/memory")

    assert response.status_code == 200


def test_missing_header_is_401(app):
    response = Api(app, None).request("GET", "/agent/memory", auth=False)

    assert response.status_code == 401
    assert "x-id-token" in response.json()["detail"]


def test_id_token_header_is_the_production_path(app, token):
    """Through CloudFront's OAC the Authorization header is overwritten by
    the origin signature, so the PWA sends the token in X-Id-Token."""
    response = Api(app, None).request("GET", "/agent/memory", headers={"X-Id-Token": token})

    assert response.status_code == 200


def test_id_token_header_wins_over_authorization(app, token, other_rsa_keys):
    # What CloudFront delivers: a good X-Id-Token next to an Authorization
    # header that is not a JWT at all.
    response = Api(app, None).request(
        "GET",
        "/agent/memory",
        headers={"X-Id-Token": token, "Authorization": "AWS4-HMAC-SHA256 Credential=cloudfront"},
    )

    assert response.status_code == 200


def test_bad_id_token_header_is_401(app, other_rsa_keys):
    response = Api(app, None).request(
        "GET", "/agent/memory", headers={"X-Id-Token": make_token(other_rsa_keys[0])}
    )

    assert response.status_code == 401


def test_malformed_scheme_is_401(app, token):
    response = Api(app, None).request("GET", "/agent/memory", headers={"Authorization": token})

    assert response.status_code == 401


@pytest.mark.parametrize(
    "kwargs",
    [
        {"expires_in": timedelta(minutes=-5)},
        {"aud": "someone-else"},
        {"iss": "https://cognito-idp.ap-southeast-2.amazonaws.com/other"},
        {"token_use": "access"},
        {"token_use": None},
        {"sub": None},
    ],
    ids=["expired", "wrong-audience", "wrong-issuer", "access-token", "no-token-use", "no-sub"],
)
def test_bad_claims_are_401(app, rsa_keys, kwargs):
    response = Api(app, make_token(rsa_keys[0], **kwargs)).request("GET", "/agent/memory")

    assert response.status_code == 401
    # The token itself never appears in the answer.
    assert "eyJ" not in response.text


def test_wrong_signing_key_is_401(app, other_rsa_keys):
    response = Api(app, make_token(other_rsa_keys[0])).request("GET", "/agent/memory")

    assert response.status_code == 401


def test_verifier_returns_sub_and_email(rsa_keys):
    verifier = TokenVerifier(issuer=ISSUER, audience=CLIENT_ID, key_resolver=lambda _t: rsa_keys[1])

    user = verifier.verify(f"Bearer {make_token(rsa_keys[0], sub='abc', email='a@b.c')}")

    assert (user.sub, user.email) == ("abc", "a@b.c")


def test_key_resolver_failure_is_an_auth_error(rsa_keys):
    def broken(token):
        raise ConnectionError("jwks down")

    verifier = TokenVerifier(issuer=ISSUER, audience=CLIENT_ID, key_resolver=broken)

    with pytest.raises(AuthError, match="invalid token"):
        verifier.verify(f"Bearer {make_token(rsa_keys[0])}")


@pytest.mark.parametrize(
    "claims",
    [
        {"sub": "u1", "token_use": "id", "email": "e"},
        {"sub": "u1", "token_use": "access"},
        {"token_use": "id"},
        {"sub": "", "token_use": "id"},
    ],
    ids=["ok", "access", "no-sub", "empty-sub"],
)
def test_claim_rules_match_the_api(rsa_keys, claims):
    """Same claims, same verdict: the API reads claims the authorizer
    verified; the runner verifies then reads. The reading must agree."""
    verifier = TokenVerifier(issuer=ISSUER, audience=CLIENT_ID, key_resolver=lambda _t: rsa_keys[1])
    token = make_token(
        rsa_keys[0],
        sub=claims.get("sub"),
        email=claims.get("email"),
        token_use=claims.get("token_use"),
    )

    try:
        get_current_user(dict(claims))
        api_accepts = True
    except HTTPException:
        api_accepts = False
    try:
        verifier.verify(f"Bearer {token}")
        runner_accepts = True
    except AuthError:
        runner_accepts = False

    assert api_accepts == runner_accepts
