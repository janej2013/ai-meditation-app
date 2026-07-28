"""Tests for the FastAPI application.

Claims are injected via dependency overrides rather than by constructing API
Gateway events: the authorizer has already validated the token by the time the
app sees it, so the app's job is only to read claims correctly.

One test deliberately does *not* override the identity dependency, to exercise
the real claim-extraction path against a request with no Lambda event.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.deps import CurrentUser, get_claims, get_current_user, get_store
from api.main import app

from .conftest import USER_ID, seed_entitlement

EMAIL = "user@example.com"


@pytest.fixture
def client(store):
    """A client authenticated as USER_ID, backed by the moto table."""
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(sub=USER_ID, email=EMAIL)
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def anonymous_client(store):
    """A client with the real identity dependency left in place."""
    app.dependency_overrides[get_store] = lambda: store
    yield TestClient(app)
    app.dependency_overrides.clear()


# ----------------------------------------------------------------------
# /health
# ----------------------------------------------------------------------


def test_health_needs_no_authentication(anonymous_client):
    response = anonymous_client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# ----------------------------------------------------------------------
# /account
# ----------------------------------------------------------------------


def test_account_returns_the_entitlement(client, dynamodb_client):
    seed_entitlement(dynamodb_client, available=3, frozen=1)

    response = client.get("/account")

    assert response.status_code == 200
    assert response.json() == {"available": 3, "frozen": 1, "plan": "free"}


def test_account_lazily_initializes_a_missing_entitlement(client, store):
    """Covers a user confirmed before the trigger existed."""
    assert store.get_entitlement(USER_ID) is None

    response = client.get("/account")

    assert response.status_code == 200
    assert response.json() == {"available": 1, "frozen": 0, "plan": "free"}
    assert store.get_entitlement(USER_ID) is not None


def test_account_lazy_init_does_not_grant_a_second_credit(client, dynamodb_client):
    """A user who spent their credit must not be topped up by a re-read."""
    seed_entitlement(dynamodb_client, available=0, frozen=0)

    response = client.get("/account")

    assert response.status_code == 200
    assert response.json()["available"] == 0


def test_account_without_claims_is_unauthorized(anonymous_client):
    response = anonymous_client.get("/account")

    assert response.status_code == 401


# ----------------------------------------------------------------------
# Claim handling
# ----------------------------------------------------------------------


@pytest.fixture
def client_with_claims(store):
    """Override the raw claims, keeping the real get_current_user logic."""

    def _make(claims: dict[str, str]) -> TestClient:
        app.dependency_overrides[get_store] = lambda: store
        app.dependency_overrides[get_claims] = lambda: claims
        return TestClient(app)

    yield _make
    app.dependency_overrides.clear()


def test_access_tokens_are_rejected(client_with_claims):
    """Access tokens carry no email claim, so the API requires ID tokens."""
    response = client_with_claims({"sub": USER_ID, "token_use": "access"}).get("/account")

    assert response.status_code == 401
    assert "ID token" in response.json()["detail"]


def test_claims_without_a_subject_are_rejected(client_with_claims):
    response = client_with_claims({"token_use": "id"}).get("/account")

    assert response.status_code == 401


# ----------------------------------------------------------------------
# /generate (stub)
# ----------------------------------------------------------------------


def test_generate_is_not_implemented_yet(client):
    response = client.post("/generate", json={"mood": "anxious", "duration_minutes": 10})

    assert response.status_code == 501
    assert "not implemented" in response.json()["detail"].lower()


@pytest.mark.parametrize(
    "payload",
    [
        {"mood": "", "duration_minutes": 10},
        {"mood": "x" * 501, "duration_minutes": 10},
        {"mood": "anxious", "duration_minutes": 2},
        {"mood": "anxious", "duration_minutes": 31},
        {"mood": "anxious"},
        {"duration_minutes": 10},
    ],
)
def test_generate_rejects_bad_bodies_before_the_stub(client, payload):
    """Validation runs first, so 422 beats 501."""
    response = client.post("/generate", json=payload)

    assert response.status_code == 422


def test_generate_without_claims_is_unauthorized(anonymous_client):
    response = anonymous_client.post("/generate", json={"mood": "anxious", "duration_minutes": 10})

    assert response.status_code == 401
