"""Opening, reading and abandoning sessions: plan gate, quota, ownership."""

from __future__ import annotations

from dataclasses import replace

from shared.models import AgentSessionStatus, AgentTurn, AgentUsage

from ..conftest import USER_ID
from .conftest import NOW, OTHER_USER, Api, create_session, make_token, seed_pro_user


def test_create_session_for_a_pro_user(api, store, dynamodb_client):
    seed_pro_user(dynamodb_client)

    response = api.request("POST", "/agent/sessions")

    assert response.status_code == 201
    body = response.json()
    assert body["turn"] == 0 and body["engine"] == "native"
    assert body["model_id"] == "fake-model" and body["insights_count"] == 0
    session = store.get_agent_session(USER_ID, body["session_id"])
    assert session is not None
    assert session.status is AgentSessionStatus.ACTIVE
    assert session.engine == "native" and session.model_id == "fake-model"


def test_free_plan_is_forbidden(api, dynamodb_client):
    seed_pro_user(dynamodb_client, plan="free")

    response = api.request("POST", "/agent/sessions")

    assert response.status_code == 403
    assert response.json()["detail"] == "plan_required"


def test_allowed_plans_can_admit_free(api, deps, dynamodb_client):
    seed_pro_user(dynamodb_client, plan="free")
    deps.settings = replace(deps.settings, allowed_plans=frozenset({"pro", "free"}))

    assert api.request("POST", "/agent/sessions").status_code == 201


def test_missing_entitlement_is_initialised_then_gated(api, store):
    response = api.request("POST", "/agent/sessions")

    assert response.status_code == 403
    entitlement = store.get_entitlement(USER_ID)
    assert entitlement is not None and entitlement.plan == "free"


def test_monthly_quota(api, deps, dynamodb_client):
    seed_pro_user(dynamodb_client)
    deps.settings = replace(deps.settings, sessions_per_month=1)

    assert api.request("POST", "/agent/sessions").status_code == 201
    response = api.request("POST", "/agent/sessions")

    assert response.status_code == 429
    assert response.json()["detail"] == "quota_exhausted"


def test_insights_count_reflects_memory(api, store, dynamodb_client):
    seed_pro_user(dynamodb_client)
    store.append_insight(USER_ID, "prefers slow pacing", "s0", NOW)

    assert api.request("POST", "/agent/sessions").json()["insights_count"] == 1


def test_get_unknown_session_is_404(api):
    assert api.request("GET", "/agent/sessions/nope").status_code == 404


def test_another_users_session_is_absent(api, store):
    assert store.create_agent_session(OTHER_USER, "theirs", engine="native", model_id="m")

    assert api.request("GET", "/agent/sessions/theirs").status_code == 404
    assert api.request("POST", "/agent/sessions/theirs/abandon").status_code == 404


def test_transcript_shape(api, store, dynamodb_client):
    seed_pro_user(dynamodb_client)
    session_id = create_session(api)
    store.claim_turn(USER_ID, session_id, engine="native", now=NOW)
    store.commit_turn(
        USER_ID,
        session_id,
        expected_turn=0,
        checkpoint=AgentTurn(
            session_id=session_id,
            turn=0,
            user_text="hi",
            assistant_content=[{"text": "hel"}, {"text": "lo"}],
            tool_calls=[
                {
                    "assistant_content": [
                        {"text": "checking"},
                        {"toolUse": {"toolUseId": "t", "name": "get_session_history", "input": {}}},
                    ],
                    "results": [
                        {"toolResult": {"toolUseId": "t", "content": [{"json": {"secret": 1}}]}}
                    ],
                }
            ],
            usage=AgentUsage(),
            stop_reason="end_turn",
            created_at=NOW,
        ),
    )

    response = api.request("GET", f"/agent/sessions/{session_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ACTIVE" and body["turn"] == 1 and body["job_id"] is None
    [turn] = body["turns"]
    assert turn["user_text"] == "hi" and turn["assistant_text"] == "hello"
    assert turn["tools"] == ["get_session_history"]
    assert "secret" not in response.text


def test_abandon_is_idempotent_and_closes_the_session(api, store, dynamodb_client):
    seed_pro_user(dynamodb_client)
    session_id = create_session(api)

    assert api.request("POST", f"/agent/sessions/{session_id}/abandon").status_code == 204
    assert api.request("POST", f"/agent/sessions/{session_id}/abandon").status_code == 204
    session = store.get_agent_session(USER_ID, session_id)
    assert session is not None and session.status is AgentSessionStatus.ABANDONED

    response = api.request("POST", f"/agent/sessions/{session_id}/turns", json={"text": "hi"})
    assert response.status_code == 409
    assert response.json()["detail"] == "busy_or_closed"


def test_finalized_session_cannot_be_abandoned(api, store, dynamodb_client):
    seed_pro_user(dynamodb_client)
    session_id = create_session(api)
    store.claim_turn(USER_ID, session_id, engine="native", now=NOW)
    store.commit_turn(
        USER_ID,
        session_id,
        expected_turn=0,
        checkpoint=AgentTurn(
            session_id=session_id,
            turn=0,
            user_text="go",
            assistant_content=[],
            stop_reason="end_turn",
        ),
        finalized_job_id="job-1",
    )

    response = api.request("POST", f"/agent/sessions/{session_id}/abandon")

    assert response.status_code == 409
    assert response.json()["detail"] == "already_finalized"


def test_other_user_token_sees_nothing(app, rsa_keys, store, dynamodb_client):
    seed_pro_user(dynamodb_client)
    mine = create_session(Api(app, make_token(rsa_keys[0])))

    theirs = Api(app, make_token(rsa_keys[0], sub=OTHER_USER))
    assert theirs.request("GET", f"/agent/sessions/{mine}").status_code == 404
