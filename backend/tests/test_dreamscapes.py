"""The dreamscapes collection: listing, cursor pagination, soft-delete.

The list endpoint sorts and paginates in the application (see
db.list_done_jobs for why there is no GSI), so ordering and cursor behaviour
are exercised here through the HTTP surface, against real moto items.
"""

from __future__ import annotations

import base64
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

from api.deps import CurrentUser, get_current_user, get_store
from api.main import app
from api.routers import dreamscapes as dreamscapes_router
from shared.models import JobStatus, job_sk, user_pk

from .conftest import BUCKET, TABLE_NAME, USER_ID

OTHER_USER = "other-user-sub"


@pytest.fixture
def client(store, s3_bucket, monkeypatch):
    monkeypatch.setenv("AUDIO_BUCKET", BUCKET)
    monkeypatch.setattr(dreamscapes_router, "_get_s3", lambda: s3_bucket)
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(sub=USER_ID, email="u@x.co")
    yield TestClient(app)
    app.dependency_overrides.clear()


def seed_dream(
    dynamodb_client,
    job_id: str,
    created_at: str,
    *,
    user_id: str = USER_ID,
    status: JobStatus = JobStatus.DONE,
    mood_text: str = "calm after rain",
    picture: bool = False,
) -> None:
    item = {
        "PK": {"S": user_pk(user_id)},
        "SK": {"S": job_sk(job_id)},
        "entity_type": {"S": "JOB"},
        "job_id": {"S": job_id},
        "status": {"S": status.value},
        "mood_text": {"S": mood_text},
        "duration_minutes": {"N": "10"},
        "audio_key": {"S": f"jobs/{job_id}/narration.mp3"},
        "created_at": {"S": created_at},
    }
    if picture:
        item["picture_key"] = {"S": f"pictures/{user_id}/{job_id}.jpg"}
        item["picture_keywords"] = {"L": [{"S": "dusk"}, {"S": "ocean"}, {"S": "longing"}]}
        item["picture_summary"] = {"S": "A quiet shoreline at dusk."}
    dynamodb_client.put_item(TableName=TABLE_NAME, Item=item)


def at(i: int) -> str:
    """Distinct, sortable timestamps: at(0) is the oldest."""
    return f"2026-08-{10 + i // 24:02d}T{i % 24:02d}:00:00+00:00"


# ----------------------------------------------------------------------
# GET /dreamscapes
# ----------------------------------------------------------------------


def test_list_is_newest_first_and_paginates_with_a_cursor(client, dynamodb_client):
    for i in range(25):
        seed_dream(dynamodb_client, f"job-{i:02d}", at(i))

    first = client.get("/dreamscapes")
    assert first.status_code == 200
    page = first.json()
    assert [item["job_id"] for item in page["items"]] == [f"job-{i:02d}" for i in range(24, 4, -1)]
    assert page["next_cursor"]
    assert page["total"] == 25  # the whole collection, not the page

    second = client.get("/dreamscapes", params={"cursor": page["next_cursor"]})
    rest = second.json()
    assert [item["job_id"] for item in rest["items"]] == [f"job-{i:02d}" for i in range(4, -1, -1)]
    assert rest["next_cursor"] is None


def test_the_cursor_survives_the_anchor_item_being_deleted(client, dynamodb_client, store):
    """A value cursor points at an ordering boundary, not a position: deleting
    the anchor row between pages must not skip or repeat items."""
    for i in range(21):
        seed_dream(dynamodb_client, f"job-{i:02d}", at(i))
    cursor = client.get("/dreamscapes").json()["next_cursor"]  # anchored on job-01

    assert store.mark_job_deleted(USER_ID, "job-01")

    rest = client.get("/dreamscapes", params={"cursor": cursor}).json()
    assert [item["job_id"] for item in rest["items"]] == ["job-00"]


@pytest.mark.parametrize(
    "status", [JobStatus.PENDING, JobStatus.GENERATING, JobStatus.FAILED, JobStatus.DELETED]
)
def test_only_done_jobs_are_listed(client, dynamodb_client, status):
    seed_dream(dynamodb_client, "job-done", at(0))
    seed_dream(dynamodb_client, "job-other", at(1), status=status)

    items = client.get("/dreamscapes").json()["items"]

    assert [item["job_id"] for item in items] == ["job-done"]


def test_items_carry_keywords_or_a_mood_excerpt_and_no_audio_url(client, dynamodb_client):
    seed_dream(dynamodb_client, "job-pic", at(1), picture=True)
    seed_dream(dynamodb_client, "job-txt", at(0), mood_text="x" * 60)

    pic, txt = client.get("/dreamscapes").json()["items"]

    assert pic["source_type"] == "picture"
    assert pic["keywords"] == ["dusk", "ocean", "longing"]
    assert pic["mood_excerpt"] is None
    assert txt["source_type"] == "text"
    assert txt["keywords"] is None
    assert txt["mood_excerpt"] == "x" * 40 + "…"
    assert "audio_url" not in pic and "audio_url" not in txt


def test_a_whitespace_mood_yields_no_excerpt_rather_than_an_empty_title(client, dynamodb_client):
    seed_dream(dynamodb_client, "job-a", at(0), mood_text="   ")

    [item] = client.get("/dreamscapes").json()["items"]

    assert item["mood_excerpt"] is None


@pytest.mark.parametrize(
    "cursor",
    [
        "%%%not-base64%%%",
        base64.urlsafe_b64encode(b"2026-01-01T00:00:00|job-x").decode(),  # naive stamp
        base64.urlsafe_b64encode(b"not-a-date|job-x").decode(),
    ],
    ids=["garbage", "naive-timestamp", "bad-date"],
)
def test_a_bad_cursor_is_a_400_not_a_500(client, dynamodb_client, cursor):
    seed_dream(dynamodb_client, "job-a", at(0))  # something to compare against
    assert client.get("/dreamscapes", params={"cursor": cursor}).status_code == 400


# ----------------------------------------------------------------------
# DELETE /dreamscapes/{job_id}
# ----------------------------------------------------------------------


def put_audio(s3_bucket, job_id: str) -> None:
    for name in ("narration.mp3", "script.txt"):
        s3_bucket.put_object(Bucket=BUCKET, Key=f"jobs/{job_id}/{name}", Body=b"bytes")


def keys_under(s3_bucket, prefix: str) -> list[str]:
    page = s3_bucket.list_objects_v2(Bucket=BUCKET, Prefix=prefix)
    return [o["Key"] for o in page.get("Contents", [])]


def test_delete_soft_deletes_and_sweeps_the_audio(client, dynamodb_client, s3_bucket, store):
    seed_dream(dynamodb_client, "job-a", at(0))
    put_audio(s3_bucket, "job-a")
    s3_bucket.put_object(Bucket=BUCKET, Key=f"pictures/{USER_ID}/job-a.jpg", Body=b"jpeg")

    response = client.delete("/dreamscapes/job-a")

    assert response.status_code == 204
    assert store.get_job(USER_ID, "job-a").status is JobStatus.DELETED
    assert keys_under(s3_bucket, "jobs/job-a/") == []
    # Constraint 9: the picture expires by lifecycle rule alone.
    assert keys_under(s3_bucket, f"pictures/{USER_ID}/") == [f"pictures/{USER_ID}/job-a.jpg"]
    assert client.get("/dreamscapes").json()["items"] == []


def test_delete_is_idempotent_and_the_retry_still_sweeps(client, dynamodb_client, s3_bucket):
    """The second call must re-run the S3 cleanup: that is what heals a first
    attempt whose S3 step failed after the status flip."""
    seed_dream(dynamodb_client, "job-a", at(0))
    put_audio(s3_bucket, "job-a")
    assert client.delete("/dreamscapes/job-a").status_code == 204

    put_audio(s3_bucket, "job-a")  # what a failed sweep would have left behind
    assert client.delete("/dreamscapes/job-a").status_code == 204
    assert keys_under(s3_bucket, "jobs/job-a/") == []


def test_delete_404s_for_missing_foreign_and_in_flight_jobs(client, dynamodb_client):
    seed_dream(dynamodb_client, "job-theirs", at(0), user_id=OTHER_USER)
    seed_dream(dynamodb_client, "job-running", at(1), status=JobStatus.GENERATING)

    assert client.delete("/dreamscapes/job-none").status_code == 404
    assert client.delete("/dreamscapes/job-theirs").status_code == 404
    assert client.delete("/dreamscapes/job-running").status_code == 404


def test_a_partial_sweep_is_a_500_not_a_silent_orphan(client, dynamodb_client, monkeypatch):
    """delete_objects reports per-key failures in Errors without raising;
    treating that as success would leave an unreaped narration forever."""
    seed_dream(dynamodb_client, "job-a", at(0))
    s3 = MagicMock()
    s3.list_objects_v2.return_value = {"Contents": [{"Key": "jobs/job-a/narration.mp3"}]}
    s3.delete_objects.return_value = {
        "Errors": [{"Key": "jobs/job-a/narration.mp3", "Code": "InternalError"}]
    }
    monkeypatch.setattr(dreamscapes_router, "_get_s3", lambda: s3)

    assert client.delete("/dreamscapes/job-a").status_code == 500


def test_a_failed_sweep_is_a_500_and_the_job_stays_deleted(
    client, dynamodb_client, store, monkeypatch
):
    seed_dream(dynamodb_client, "job-a", at(0))
    s3 = MagicMock()
    s3.list_objects_v2.side_effect = ClientError({"Error": {"Code": "500"}}, "ListObjectsV2")
    monkeypatch.setattr(dreamscapes_router, "_get_s3", lambda: s3)

    assert client.delete("/dreamscapes/job-a").status_code == 500
    # The retry path: status already DELETED, next attempt sweeps again.
    assert store.get_job(USER_ID, "job-a").status is JobStatus.DELETED
