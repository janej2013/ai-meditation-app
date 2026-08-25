from __future__ import annotations

from datetime import UTC, datetime

from ..conftest import USER_ID


def test_memory_starts_empty(api):
    response = api.request("GET", "/agent/memory")

    assert response.status_code == 200
    assert response.json() == {"insights": []}


def test_memory_lists_insights_in_order(api, store):
    now = datetime(2026, 8, 25, 10, 0, tzinfo=UTC)
    store.append_insight(USER_ID, "prefers slow pacing", "s1", now)
    store.append_insight(USER_ID, "dislikes ocean sounds", "s2", now)

    response = api.request("GET", "/agent/memory")

    assert [i["text"] for i in response.json()["insights"]] == [
        "prefers slow pacing",
        "dislikes ocean sounds",
    ]
    assert response.json()["insights"][0]["created_at"].startswith("2026-08-25T10:00:00")


def test_clear_memory_is_idempotent(api, store):
    store.append_insight(USER_ID, "gone soon", "s1", datetime(2026, 8, 25, tzinfo=UTC))

    assert api.request("DELETE", "/agent/memory").status_code == 204
    assert api.request("DELETE", "/agent/memory").status_code == 204
    assert api.request("GET", "/agent/memory").json() == {"insights": []}
