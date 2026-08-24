"""Tests for the FastAPI application.

Claims are injected via dependency overrides rather than by constructing API
Gateway events: the authorizer has already validated the token by the time the
app sees it, so the app's job is only to read claims correctly.

One test deliberately does *not* override the identity dependency, to exercise
the real claim-extraction path against a request with no Lambda event.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

from api.deps import CurrentUser, get_claims, get_current_user, get_store
from api.main import app
from shared.models import JobStatus

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
# POST /generate
# ----------------------------------------------------------------------

GOOD_BODY = {"mood": "anxious about work", "duration_minutes": 10}


@pytest.fixture
def sfn_client(monkeypatch):
    """Capture StartExecution instead of calling Step Functions."""
    from api.routers import generate as generate_router

    fake = MagicMock()
    monkeypatch.setenv("STATE_MACHINE_ARN", "arn:aws:states:ap-southeast-2:123:stateMachine:m")
    monkeypatch.setenv("AUDIO_BUCKET", "meditation-test-audio")
    monkeypatch.setattr(generate_router, "_get_sfn", lambda: fake)
    return fake


def test_generate_accepts_and_starts_the_pipeline(client, dynamodb_client, store, sfn_client):
    seed_entitlement(dynamodb_client, available=1)

    response = client.post("/generate", json=GOOD_BODY)

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "PENDING"

    job = store.get_job(USER_ID, body["job_id"])
    assert job is not None
    assert job.status is JobStatus.PENDING
    assert job.mood_text == GOOD_BODY["mood"]

    sfn_client.start_execution.assert_called_once()
    payload = json.loads(sfn_client.start_execution.call_args.kwargs["input"])
    # Constraint 7: the mood is on the JOB item, never in the execution input.
    assert payload == {
        "user_id": USER_ID,
        "job_id": body["job_id"],
        "duration_minutes": 10,
        "has_picture": False,
    }


def test_generate_names_the_execution_after_the_job(client, dynamodb_client, sfn_client):
    """A named execution makes a duplicated StartExecution a no-op, not a second run."""
    seed_entitlement(dynamodb_client, available=1)

    response = client.post("/generate", json=GOOD_BODY)

    assert sfn_client.start_execution.call_args.kwargs["name"] == response.json()["job_id"]


def test_generate_with_no_credits_is_payment_required(client, dynamodb_client, sfn_client):
    seed_entitlement(dynamodb_client, available=0)

    response = client.post("/generate", json=GOOD_BODY)

    assert response.status_code == 402
    sfn_client.start_execution.assert_not_called()


def test_generate_rejects_a_second_concurrent_job(client, dynamodb_client, sfn_client):
    """frozen >= 1 means a job is already in flight."""
    seed_entitlement(dynamodb_client, available=3, frozen=1)

    response = client.post("/generate", json=GOOD_BODY)

    assert response.status_code == 429
    assert "already in progress" in response.json()["detail"].lower()
    sfn_client.start_execution.assert_not_called()


def test_generate_reports_unavailable_when_the_pipeline_cannot_start(
    client, dynamodb_client, sfn_client
):
    seed_entitlement(dynamodb_client, available=1)
    sfn_client.start_execution.side_effect = ClientError(
        {"Error": {"Code": "ThrottlingException"}}, "StartExecution"
    )

    response = client.post("/generate", json=GOOD_BODY)

    assert response.status_code == 503


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
def test_generate_rejects_bad_bodies(client, payload, sfn_client):
    response = client.post("/generate", json=payload)

    assert response.status_code == 422
    sfn_client.start_execution.assert_not_called()


def test_generate_without_claims_is_unauthorized(anonymous_client):
    response = anonymous_client.post("/generate", json=GOOD_BODY)

    assert response.status_code == 401


# ----------------------------------------------------------------------
# GET /jobs/{job_id}
# ----------------------------------------------------------------------


def test_generate_with_a_picture_stores_the_key_and_flags_the_execution(
    client, dynamodb_client, store, sfn_client
):
    """The key lives on the JOB item; the execution input carries only a flag."""
    seed_entitlement(dynamodb_client, available=1)
    picture_id = "3f0c9f8e-6a3b-4c1d-9e2f-1a2b3c4d5e6f"

    response = client.post("/generate", json={**GOOD_BODY, "picture_id": picture_id})

    assert response.status_code == 202
    job_id = response.json()["job_id"]
    # Scoped to the caller's subject: a client cannot name another user's key.
    assert store.get_job(USER_ID, job_id).picture_key == f"pictures/{USER_ID}/{picture_id}.jpg"

    payload = json.loads(sfn_client.start_execution.call_args.kwargs["input"])
    assert payload["has_picture"] is True
    assert "pictures/" not in json.dumps(payload)


def test_generate_rejects_a_malformed_picture_id(client, dynamodb_client, sfn_client):
    seed_entitlement(dynamodb_client, available=1)

    response = client.post("/generate", json={**GOOD_BODY, "picture_id": "../other-user"})

    assert response.status_code == 422
    sfn_client.start_execution.assert_not_called()


def test_job_status_reports_picture_keywords_once_described(
    client, dynamodb_client, store, sfn_client
):
    from shared.models import PictureDescription

    seed_entitlement(dynamodb_client, available=1)
    job_id = client.post("/generate", json=GOOD_BODY).json()["job_id"]
    store.set_job_picture_description(
        USER_ID,
        job_id,
        PictureDescription(keywords=["dusk", "still water", "pines"], summary="Quiet dusk lake."),
    )

    body = client.get(f"/jobs/{job_id}").json()

    assert body["picture_keywords"] == ["dusk", "still water", "pines"]
    # The summary is prompt material only.
    assert "summary" not in json.dumps(body)


# ----------------------------------------------------------------------
# POST /pictures/upload
# ----------------------------------------------------------------------


def test_picture_upload_is_a_presigned_post_scoped_to_the_caller(client, monkeypatch):
    monkeypatch.setenv("AUDIO_BUCKET", "meditation-test-audio")

    response = client.post("/pictures/upload")

    assert response.status_code == 200
    body = response.json()
    assert body["fields"]["key"] == f"pictures/{USER_ID}/{body['picture_id']}.jpg"
    assert body["fields"]["Content-Type"] == "image/jpeg"
    assert body["expires_in"] == 300
    assert "meditation-test-audio" in body["url"]

    import base64

    policy = json.loads(base64.b64decode(body["fields"]["policy"]))
    conditions = policy["conditions"]
    # The policy is what S3 enforces: size and type are not client suggestions.
    assert ["content-length-range", 1, 4_000_000] in conditions
    assert {"Content-Type": "image/jpeg"} in conditions


def test_picture_upload_without_claims_is_unauthorized(anonymous_client):
    assert anonymous_client.post("/pictures/upload").status_code == 401


def test_job_status_while_running(client, dynamodb_client, store, sfn_client):
    seed_entitlement(dynamodb_client, available=1)
    job_id = client.post("/generate", json=GOOD_BODY).json()["job_id"]
    store.freeze_credit(USER_ID, job_id)

    response = client.get(f"/jobs/{job_id}")

    assert response.status_code == 200
    assert response.json() == {
        "job_id": job_id,
        "status": "FROZEN",
        "audio_url": None,
        "picture_keywords": None,
    }


def test_done_job_returns_a_cloudfront_signed_url(
    client, dynamodb_client, store, sfn_client, monkeypatch
):
    """Constraint 6: the URL comes from the CloudFront signer, not S3 presign."""
    from api.routers import generate as generate_router

    seed_entitlement(dynamodb_client, available=1)
    job_id = client.post("/generate", json=GOOD_BODY).json()["job_id"]
    store.freeze_credit(USER_ID, job_id)
    store.set_job_audio_key(USER_ID, job_id, f"jobs/{job_id}/final.mp3")
    store.commit_credit(USER_ID, job_id)

    signer = MagicMock()
    signer.signed_url.return_value = "https://audio.example/jobs/x/final.mp3?Signature=abc"
    monkeypatch.setattr(generate_router, "cloudfront_signer", signer)

    response = client.get(f"/jobs/{job_id}")

    assert response.json()["status"] == "DONE"
    assert response.json()["audio_url"] == signer.signed_url.return_value
    signer.signed_url.assert_called_once_with(f"jobs/{job_id}/final.mp3")


def test_rolled_back_job_is_reported_as_failed(client, dynamodb_client, store, sfn_client):
    """ROLLED_BACK is the internal truth; clients only need 'it failed'."""
    seed_entitlement(dynamodb_client, available=1)
    job_id = client.post("/generate", json=GOOD_BODY).json()["job_id"]
    store.freeze_credit(USER_ID, job_id)
    store.rollback_credit(USER_ID, job_id)

    response = client.get(f"/jobs/{job_id}")

    assert response.json()["status"] == "FAILED"
    assert response.json()["audio_url"] is None


def test_another_users_job_is_not_found(client, dynamodb_client, store, sfn_client):
    """Reads are scoped to the caller's partition -- no existence oracle."""
    seed_entitlement(dynamodb_client, user_id="someone-else", available=1)
    store.create_job("someone-else", "their-job", "private mood", 10)

    response = client.get("/jobs/their-job")

    assert response.status_code == 404


def test_unknown_job_is_not_found(client, sfn_client):
    assert client.get("/jobs/does-not-exist").status_code == 404


def test_job_status_without_claims_is_unauthorized(anonymous_client):
    assert anonymous_client.get("/jobs/anything").status_code == 401
