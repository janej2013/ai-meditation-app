"""The turn route: the SSE contract, the fencing token around a request,
and what a failed turn leaves behind (nothing)."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import httpx

from agent.budget import FINALIZE_TOOL_NAME, MAX_TURNS
from agent.native.llm.converse import AgentProviderError
from agent.tools.finalize import agent_job_id
from shared.models import AgentSessionStatus, agent_session_sk, user_pk

from ..agent.fake_provider import Pause, Raise, text_reply, tool_reply
from ..conftest import TABLE_NAME, USER_ID
from .conftest import create_session, seed_pro_user, sse_events

BRIEF = "A calm evening meditation for someone who wants to set the day down slowly."


def turn(api, session_id: str, text: str = "hello", **kwargs):
    return api.stream(f"/agent/sessions/{session_id}/turns", json={"text": text}, **kwargs)


def test_text_turn_streams_deltas_then_done(api, store, provider, dynamodb_client):
    seed_pro_user(dynamodb_client)
    session_id = create_session(api)
    provider.queue(text_reply("hel", "lo"))

    status, body = turn(api, session_id)

    assert status == 200
    assert sse_events(body) == [
        ("delta", {"text": "hel"}),
        ("delta", {"text": "lo"}),
        ("done", {"turn": 1, "job_id": None, "awaiting_confirmation": False}),
    ]
    session = store.get_agent_session(USER_ID, session_id)
    assert session is not None
    assert session.turn == 1 and session.in_flight is None
    [stored] = store.list_turns(USER_ID, session_id)
    assert stored.user_text == "hello"


def test_tool_events_precede_the_answer(api, provider, dynamodb_client):
    seed_pro_user(dynamodb_client)
    session_id = create_session(api)
    provider.queue(
        tool_reply(("get_session_history", {"limit": 2}, "tu-1")), text_reply("nothing yet")
    )

    _, body = turn(api, session_id)

    assert [e for e, _ in sse_events(body)] == ["tool", "delta", "done"]
    assert sse_events(body)[0] == ("tool", {"name": "get_session_history"})


def test_terminal_tools_are_not_in_the_default_registry(api, store, provider, sfn, dynamodb_client):
    """The production tools can no longer end a session on their own; see
    the proposal tests below for how one ends now."""
    seed_pro_user(dynamodb_client, available=1)
    session_id = create_session(api)
    provider.queue(
        tool_reply((FINALIZE_TOOL_NAME, {"brief": BRIEF, "duration_minutes": 5}, "tu-1")),
        text_reply("ok"),
    )

    _, body = turn(api, session_id, "go ahead")

    name, data = sse_events(body)[-1]
    assert name == "done" and data["job_id"] is None
    session = store.get_agent_session(USER_ID, session_id)
    assert session is not None and session.status is AgentSessionStatus.ACTIVE
    sfn.start_execution.assert_not_called()


def test_busy_session_is_409_before_any_stream(api, store, dynamodb_client):
    seed_pro_user(dynamodb_client)
    session_id = create_session(api)
    from datetime import UTC, datetime

    store.claim_turn(USER_ID, session_id, engine="native", now=datetime.now(UTC))

    response = api.request("POST", f"/agent/sessions/{session_id}/turns", json={"text": "hi"})

    assert response.status_code == 409
    assert response.json()["detail"] == "busy_or_closed"


def test_unknown_session_is_404(api):
    assert api.request("POST", "/agent/sessions/nope/turns", json={"text": "hi"}).status_code == 404


def test_text_is_validated(api, dynamodb_client):
    seed_pro_user(dynamodb_client)
    session_id = create_session(api)

    for text in ("", "   ", "x" * 1001):
        response = api.request("POST", f"/agent/sessions/{session_id}/turns", json={"text": text})
        assert response.status_code == 422, text[:5]


def test_provider_failure_releases_the_claim_and_is_retryable(
    api, store, provider, dynamodb_client
):
    seed_pro_user(dynamodb_client)
    session_id = create_session(api)
    provider.queue([Raise(AgentProviderError("bedrock down"))])

    status, body = turn(api, session_id)

    assert status == 200
    assert sse_events(body) == [("error", {"code": "model_unavailable", "retryable": True})]
    session = store.get_agent_session(USER_ID, session_id)
    assert session is not None
    assert session.turn == 0 and session.in_flight is None
    assert store.list_turns(USER_ID, session_id) == []

    # The same message, resent right away.
    provider.queue(text_reply("ok now"))
    _, body = turn(api, session_id)
    assert sse_events(body)[-1] == (
        "done",
        {"turn": 1, "job_id": None, "awaiting_confirmation": False},
    )


def test_unexpected_exception_is_an_internal_error(api, store, provider, dynamodb_client):
    seed_pro_user(dynamodb_client)
    session_id = create_session(api)
    provider.queue([Raise(RuntimeError("bug"))])

    _, body = turn(api, session_id)

    assert sse_events(body) == [("error", {"code": "internal", "retryable": True})]
    session = store.get_agent_session(USER_ID, session_id)
    assert session is not None and session.in_flight is None


def test_heartbeat_while_the_model_is_silent(api, deps, provider, dynamodb_client):
    seed_pro_user(dynamodb_client)
    session_id = create_session(api)
    deps.settings = replace(deps.settings, heartbeat_seconds=0.01)
    provider.queue([Pause(0.08), *text_reply("late")])

    _, body = turn(api, session_id)

    assert ": ping" in body
    assert sse_events(body)[-1] == (
        "done",
        {"turn": 1, "job_id": None, "awaiting_confirmation": False},
    )


def test_exhausted_session_is_409_and_abandoned(api, store, dynamodb_client):
    seed_pro_user(dynamodb_client)
    session_id = create_session(api)
    dynamodb_client.update_item(
        TableName=TABLE_NAME,
        Key={"PK": {"S": user_pk(USER_ID)}, "SK": {"S": agent_session_sk(session_id)}},
        UpdateExpression="SET #t = :t",
        ExpressionAttributeNames={"#t": "turn"},
        ExpressionAttributeValues={":t": {"N": str(MAX_TURNS)}},
    )

    response = api.request("POST", f"/agent/sessions/{session_id}/turns", json={"text": "hi"})

    assert response.status_code == 409
    assert response.json()["detail"] == "session_exhausted"
    session = store.get_agent_session(USER_ID, session_id)
    assert session is not None and session.status is AgentSessionStatus.ABANDONED


def test_deadline_header_is_honoured(api, provider, dynamodb_client):
    seed_pro_user(dynamodb_client)
    session_id = create_session(api)
    provider.queue(text_reply("quick"))
    # A deadline already in the past: the engine must ask for a plain answer.
    header = {"x-amzn-lambda-context": '{"deadline": 1}'}

    status, body = turn(api, session_id, headers=header)

    assert status == 200
    assert sse_events(body)[-1][0] == "done"
    from agent.prompt import NO_MORE_TOOLS_HINT

    assert provider.calls[0].last_user_text.endswith(NO_MORE_TOOLS_HINT)


def test_client_disconnect_does_not_lose_the_turn(app, token, store, provider, dynamodb_client):
    seed_pro_user(dynamodb_client)
    from .conftest import Api

    session_id = create_session(Api(app, token))
    provider.queue([*text_reply("first"), Pause(0.05)])

    async def go() -> None:
        async with (
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client,
            client.stream(
                "POST",
                f"/agent/sessions/{session_id}/turns",
                json={"text": "hi"},
                headers={"Authorization": f"Bearer {token}"},
            ) as response,
        ):
            async for _ in response.aiter_text():
                break  # the client goes away after the first frame
        for _ in range(50):
            session = store.get_agent_session(USER_ID, session_id)
            if session is not None and session.turn == 1:
                return
            await asyncio.sleep(0.02)
        raise AssertionError("the turn was never committed")

    asyncio.run(go())


# ----------------------------------------------------------------------
# Proposals and confirmation
# ----------------------------------------------------------------------

PROPOSAL = tool_reply((FINALIZE_TOOL_NAME, {"brief": BRIEF, "duration_minutes": 5}, "tu-p"))


def propose(api, provider, session_id):
    provider.queue(PROPOSAL, text_reply("Start it whenever you like."))
    return turn(api, session_id, "go ahead")


def test_a_proposal_streams_and_starts_nothing(api, store, provider, sfn, dynamodb_client):
    seed_pro_user(dynamodb_client, available=1)
    session_id = create_session(api)

    _, body = propose(api, provider, session_id)

    assert sse_events(body) == [
        ("tool", {"name": FINALIZE_TOOL_NAME}),
        ("proposal", {"duration_minutes": 5}),
        ("delta", {"text": "Start it whenever you like."}),
        ("done", {"turn": 1, "job_id": None, "awaiting_confirmation": True}),
    ]
    sfn.start_execution.assert_not_called()
    transcript = api.request("GET", f"/agent/sessions/{session_id}").json()
    assert transcript["status"] == "ACTIVE"
    assert transcript["pending"] == {"brief": BRIEF, "duration_minutes": 5}


def test_a_new_message_withdraws_the_proposal(api, provider, dynamodb_client):
    seed_pro_user(dynamodb_client, available=1)
    session_id = create_session(api)
    propose(api, provider, session_id)
    provider.queue(text_reply("what would you change?"))

    _, body = turn(api, session_id, "actually, something different")

    assert sse_events(body)[-1] == (
        "done",
        {"turn": 2, "job_id": None, "awaiting_confirmation": False},
    )
    assert api.request("GET", f"/agent/sessions/{session_id}").json()["pending"] is None
    assert api.request("POST", f"/agent/sessions/{session_id}/confirm").status_code == 409


def test_confirm_starts_the_job_and_closes_the_session(api, store, provider, sfn, dynamodb_client):
    seed_pro_user(dynamodb_client, available=1)
    session_id = create_session(api)
    propose(api, provider, session_id)

    response = api.request("POST", f"/agent/sessions/{session_id}/confirm")

    job_id = agent_job_id(session_id)
    assert response.status_code == 200 and response.json() == {"job_id": job_id}
    sfn.start_execution.assert_called_once()
    session = store.get_agent_session(USER_ID, session_id)
    assert session is not None
    assert session.status is AgentSessionStatus.FINALIZED and session.job_id == job_id
    assert session.turn == 1 and session.pending_brief is None
    transcript = api.request("GET", f"/agent/sessions/{session_id}").json()
    assert transcript["job_id"] == job_id and transcript["pending"] is None

    # Confirming again: same job, no second start. A turn: closed.
    assert api.request("POST", f"/agent/sessions/{session_id}/confirm").json() == {"job_id": job_id}
    assert sfn.start_execution.call_count == 1
    assert (
        api.request("POST", f"/agent/sessions/{session_id}/turns", json={"text": "hi"}).status_code
        == 409
    )


def test_confirm_error_codes(api, store, provider, sfn, dynamodb_client):
    seed_pro_user(dynamodb_client, available=1)
    session_id = create_session(api)

    assert api.request("POST", "/agent/sessions/nope/confirm").status_code == 404
    response = api.request("POST", f"/agent/sessions/{session_id}/confirm")
    assert (response.status_code, response.json()["detail"]) == (409, "nothing_to_confirm")

    propose(api, provider, session_id)
    from datetime import UTC, datetime

    store.claim_turn(USER_ID, session_id, engine="native", now=datetime.now(UTC))
    response = api.request("POST", f"/agent/sessions/{session_id}/confirm")
    assert (response.status_code, response.json()["detail"]) == (409, "busy_or_closed")
    store.release_turn(USER_ID, session_id, expected_turn=1)

    seed_pro_user(dynamodb_client, available=0)
    response = api.request("POST", f"/agent/sessions/{session_id}/confirm")
    assert (response.status_code, response.json()["detail"]) == (402, "no_credit")
    seed_pro_user(dynamodb_client, available=1, frozen=1)
    response = api.request("POST", f"/agent/sessions/{session_id}/confirm")
    assert (response.status_code, response.json()["detail"]) == (409, "job_in_flight")

    seed_pro_user(dynamodb_client, available=1)
    from botocore.exceptions import ClientError

    sfn.start_execution.side_effect = ClientError(
        {"Error": {"Code": "ServiceUnavailable", "Message": ""}}, "StartExecution"
    )
    response = api.request("POST", f"/agent/sessions/{session_id}/confirm")
    assert (response.status_code, response.json()["detail"]) == (503, "start_failed")
    # Every refusal released the claim: the session is still usable.
    session = store.get_agent_session(USER_ID, session_id)
    assert session is not None
    assert session.status is AgentSessionStatus.ACTIVE and session.in_flight is None
    assert session.pending_brief == BRIEF

    sfn.start_execution.side_effect = None
    assert api.request("POST", f"/agent/sessions/{session_id}/confirm").status_code == 200


def test_exhausted_session_with_a_proposal_stays_confirmable(
    api, store, provider, sfn, dynamodb_client
):
    seed_pro_user(dynamodb_client, available=1)
    session_id = create_session(api)
    propose(api, provider, session_id)
    dynamodb_client.update_item(
        TableName=TABLE_NAME,
        Key={"PK": {"S": user_pk(USER_ID)}, "SK": {"S": agent_session_sk(session_id)}},
        UpdateExpression="SET #t = :t",
        ExpressionAttributeNames={"#t": "turn"},
        ExpressionAttributeValues={":t": {"N": str(MAX_TURNS)}},
    )

    response = api.request("POST", f"/agent/sessions/{session_id}/turns", json={"text": "more"})

    assert response.status_code == 409 and response.json()["detail"] == "session_exhausted"
    session = store.get_agent_session(USER_ID, session_id)
    assert session is not None
    assert session.status is AgentSessionStatus.ACTIVE and session.in_flight is None
    assert api.request("POST", f"/agent/sessions/{session_id}/confirm").status_code == 200
    sfn.start_execution.assert_called_once()


def test_abandon_with_a_pending_proposal(api, store, provider, sfn, dynamodb_client):
    seed_pro_user(dynamodb_client, available=1)
    session_id = create_session(api)
    propose(api, provider, session_id)

    assert api.request("POST", f"/agent/sessions/{session_id}/abandon").status_code == 204
    assert api.request("POST", f"/agent/sessions/{session_id}/confirm").status_code == 409
    sfn.start_execution.assert_not_called()
