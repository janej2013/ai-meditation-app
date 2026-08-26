"""The companion agent's store methods: quota, claim/commit fencing, turns,
memory. Like test_db.py, every test asserts on the item state, because a
replayed commit that silently double-advances a session is the failure
that would corrupt a transcript."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from shared.db import AgentTurnBusyError, EntitlementStore, MemoryContentionError
from shared.models import (
    AGENT_INSIGHTS_MAX,
    AgentSessionStatus,
    AgentTurn,
    AgentUsage,
    agent_session_sk,
    user_pk,
)

from .conftest import TABLE_NAME, USER_ID

SESSION = "sess-1"
NOW = datetime(2026, 8, 25, 10, 0, tzinfo=UTC)


def turn_item(turn: int, *, tokens: int = 10) -> AgentTurn:
    return AgentTurn(
        session_id=SESSION,
        turn=turn,
        user_text=f"user {turn}",
        assistant_content=[{"text": f"reply {turn}"}],
        tool_calls=[],
        usage=AgentUsage(input_tokens=tokens, output_tokens=1),
        stop_reason="end_turn",
        created_at=NOW,
    )


def open_session(store: EntitlementStore, engine: str = "native") -> None:
    assert store.create_agent_session(USER_ID, SESSION, engine=engine, model_id="model")


def raw_item(client, sk: str) -> dict:
    return client.get_item(
        TableName=TABLE_NAME, Key={"PK": {"S": user_pk(USER_ID)}, "SK": {"S": sk}}
    ).get("Item", {})


# ----------------------------------------------------------------------
# Quota
# ----------------------------------------------------------------------


def test_quota_admits_cap_sessions_then_refuses(store, dynamodb_client):
    assert store.reserve_agent_session(USER_ID, "2026-08", cap=2)
    assert store.reserve_agent_session(USER_ID, "2026-08", cap=2)
    assert not store.reserve_agent_session(USER_ID, "2026-08", cap=2)

    item = raw_item(dynamodb_client, "AGENTQUOTA#2026-08")
    assert item["sessions"]["N"] == "2"
    assert "expires_at" in item


def test_quota_is_per_month(store):
    assert store.reserve_agent_session(USER_ID, "2026-08", cap=1)
    assert not store.reserve_agent_session(USER_ID, "2026-08", cap=1)
    assert store.reserve_agent_session(USER_ID, "2026-09", cap=1)


# ----------------------------------------------------------------------
# Sessions and the claim
# ----------------------------------------------------------------------


def test_create_session_is_once_only(store):
    open_session(store)

    assert not store.create_agent_session(USER_ID, SESSION, engine="native", model_id="m")
    session = store.get_agent_session(USER_ID, SESSION)
    assert session is not None
    assert session.status is AgentSessionStatus.ACTIVE
    assert session.turn == 0 and session.in_flight is None
    assert session.usage == AgentUsage()


def test_missing_session_is_none(store):
    assert store.get_agent_session(USER_ID, "nope") is None


def test_claim_sets_in_flight_and_returns_the_session(store):
    open_session(store)

    session = store.claim_turn(USER_ID, SESSION, engine="native", now=NOW)

    assert session.turn == 0
    assert session.in_flight == NOW


def test_second_claim_is_busy_until_the_first_goes_stale(store):
    open_session(store)
    store.claim_turn(USER_ID, SESSION, engine="native", now=NOW)

    with pytest.raises(AgentTurnBusyError):
        store.claim_turn(USER_ID, SESSION, engine="native", now=NOW + timedelta(seconds=30))

    # Four minutes on, the first invocation is past its timeout: taken over.
    later = NOW + timedelta(minutes=4)
    session = store.claim_turn(USER_ID, SESSION, engine="native", now=later)
    assert session.in_flight == later


def test_claim_requires_the_same_engine(store):
    open_session(store, engine="native")

    with pytest.raises(AgentTurnBusyError):
        store.claim_turn(USER_ID, SESSION, engine="langgraph", now=NOW)


def test_claim_requires_an_active_session(store):
    open_session(store)
    assert store.mark_agent_session(USER_ID, SESSION, AgentSessionStatus.ABANDONED)

    with pytest.raises(AgentTurnBusyError):
        store.claim_turn(USER_ID, SESSION, engine="native", now=NOW)


def test_claim_requires_an_aware_datetime(store):
    open_session(store)

    with pytest.raises(ValueError, match="timezone-aware"):
        store.claim_turn(USER_ID, SESSION, engine="native", now=datetime(2026, 8, 25))


# ----------------------------------------------------------------------
# Commit: the fencing token's second half
# ----------------------------------------------------------------------


def test_commit_advances_the_turn_and_releases_the_claim(store, dynamodb_client):
    open_session(store)
    store.claim_turn(USER_ID, SESSION, engine="native", now=NOW)

    assert store.commit_turn(USER_ID, SESSION, expected_turn=0, checkpoint=turn_item(0, tokens=7))

    session = store.get_agent_session(USER_ID, SESSION)
    assert session is not None
    assert session.turn == 1 and session.in_flight is None
    assert session.status is AgentSessionStatus.ACTIVE
    assert session.usage == AgentUsage(input_tokens=7, output_tokens=1)
    assert raw_item(dynamodb_client, f"AGENT#{SESSION}#T0000")["user_text"]["S"] == "user 0"

    store.claim_turn(USER_ID, SESSION, engine="native", now=NOW)
    assert store.commit_turn(USER_ID, SESSION, expected_turn=1, checkpoint=turn_item(1, tokens=5))
    session = store.get_agent_session(USER_ID, SESSION)
    assert session is not None
    assert session.turn == 2
    assert session.usage == AgentUsage(input_tokens=12, output_tokens=2)


def test_commit_with_a_stale_expected_turn_changes_nothing(store, dynamodb_client):
    open_session(store)
    store.claim_turn(USER_ID, SESSION, engine="native", now=NOW)
    assert store.commit_turn(USER_ID, SESSION, expected_turn=0, checkpoint=turn_item(0))
    store.claim_turn(USER_ID, SESSION, engine="native", now=NOW)

    # A zombie from the turn-0 invocation, arriving after the takeover.
    assert not store.commit_turn(USER_ID, SESSION, expected_turn=0, checkpoint=turn_item(0))

    session = store.get_agent_session(USER_ID, SESSION)
    assert session is not None
    assert session.turn == 1
    assert session.in_flight == NOW  # the live claim is untouched
    assert len(store.list_turns(USER_ID, SESSION)) == 1


def test_commit_without_a_claim_is_rejected(store):
    open_session(store)

    assert not store.commit_turn(USER_ID, SESSION, expected_turn=0, checkpoint=turn_item(0))
    assert store.list_turns(USER_ID, SESSION) == []


def test_commit_rejects_a_checkpoint_for_another_turn(store):
    open_session(store)
    store.claim_turn(USER_ID, SESSION, engine="native", now=NOW)

    with pytest.raises(ValueError, match="expected 0"):
        store.commit_turn(USER_ID, SESSION, expected_turn=0, checkpoint=turn_item(1))


def test_commit_with_a_job_finalizes_the_session(store):
    open_session(store)
    store.claim_turn(USER_ID, SESSION, engine="native", now=NOW)

    assert store.commit_turn(
        USER_ID, SESSION, expected_turn=0, checkpoint=turn_item(0), finalized_job_id="job-9"
    )

    session = store.get_agent_session(USER_ID, SESSION)
    assert session is not None
    assert session.status is AgentSessionStatus.FINALIZED
    assert session.job_id == "job-9"
    with pytest.raises(AgentTurnBusyError):
        store.claim_turn(USER_ID, SESSION, engine="native", now=NOW)


def test_turn_items_carry_a_ttl_and_round_trip_nested_content(store, dynamodb_client):
    open_session(store)
    store.claim_turn(USER_ID, SESSION, engine="native", now=NOW)
    checkpoint = AgentTurn(
        session_id=SESSION,
        turn=0,
        user_text="hi",
        assistant_content=[{"toolUse": {"toolUseId": "t", "name": "n", "input": {"x": 1.5}}}],
        tool_calls=[{"assistant_content": [], "results": [{"text": "r"}]}],
        stop_reason="end_turn",
        finalized_job_id=None,
    )

    assert store.commit_turn(USER_ID, SESSION, expected_turn=0, checkpoint=checkpoint)

    assert "expires_at" in raw_item(dynamodb_client, f"AGENT#{SESSION}#T0000")
    [stored] = store.list_turns(USER_ID, SESSION)
    assert stored.assistant_content == checkpoint.assistant_content
    assert stored.tool_calls == checkpoint.tool_calls
    assert stored.finalized_job_id is None


# ----------------------------------------------------------------------
# Listing
# ----------------------------------------------------------------------


def test_list_turns_paginates_and_orders(store, monkeypatch):
    open_session(store)
    for turn in range(30):
        store.claim_turn(USER_ID, SESSION, engine="native", now=NOW)
        assert store.commit_turn(USER_ID, SESSION, expected_turn=turn, checkpoint=turn_item(turn))

    original = store.client.query
    pages = []

    def small_pages(**kwargs):
        response = original(**kwargs, Limit=7)
        pages.append(len(response["Items"]))
        return response

    monkeypatch.setattr(store.client, "query", small_pages)

    turns = store.list_turns(USER_ID, SESSION)

    assert [t.turn for t in turns] == list(range(30))
    assert len(pages) == 5  # 7+7+7+7+2: the LastEvaluatedKey loop ran


def test_list_turns_ignores_other_sessions_and_the_header(store):
    open_session(store)
    store.claim_turn(USER_ID, SESSION, engine="native", now=NOW)
    store.commit_turn(USER_ID, SESSION, expected_turn=0, checkpoint=turn_item(0))
    assert store.create_agent_session(USER_ID, "sess-10", engine="native", model_id="m")

    assert [t.session_id for t in store.list_turns(USER_ID, SESSION)] == [SESSION]
    assert store.list_turns(USER_ID, "sess-10") == []


# ----------------------------------------------------------------------
# Marking
# ----------------------------------------------------------------------


def test_mark_abandoned_is_idempotent_and_releases_the_claim(store):
    open_session(store)
    store.claim_turn(USER_ID, SESSION, engine="native", now=NOW)

    assert store.mark_agent_session(USER_ID, SESSION, AgentSessionStatus.ABANDONED)
    assert store.mark_agent_session(USER_ID, SESSION, AgentSessionStatus.ABANDONED)

    session = store.get_agent_session(USER_ID, SESSION)
    assert session is not None
    assert session.status is AgentSessionStatus.ABANDONED and session.in_flight is None
    assert not store.mark_agent_session(USER_ID, SESSION, AgentSessionStatus.FAILED)


def test_mark_rejects_finalized(store):
    with pytest.raises(ValueError):
        store.mark_agent_session(USER_ID, SESSION, AgentSessionStatus.FINALIZED)


def test_mark_missing_session_is_false(store):
    assert not store.mark_agent_session(USER_ID, "nope", AgentSessionStatus.FAILED)


# ----------------------------------------------------------------------
# Memory
# ----------------------------------------------------------------------


def test_memory_starts_empty_and_appends(store):
    assert store.get_memory(USER_ID).insights == []

    assert store.append_insight(USER_ID, "  prefers   slow pacing ", SESSION, NOW)

    memory = store.get_memory(USER_ID)
    assert [i.text for i in memory.insights] == ["prefers slow pacing"]
    assert memory.insights[0].session_id == SESSION
    assert memory.updated_at == NOW


def test_duplicate_insight_is_not_saved(store):
    assert store.append_insight(USER_ID, "Likes rain sounds", SESSION, NOW)

    assert not store.append_insight(USER_ID, "likes RAIN sounds", "sess-2", NOW)
    assert not store.append_insight(USER_ID, "   ", SESSION, NOW)
    assert len(store.get_memory(USER_ID).insights) == 1


def test_memory_is_capped_fifo(store):
    for i in range(AGENT_INSIGHTS_MAX + 3):
        assert store.append_insight(USER_ID, f"insight {i}", SESSION, NOW + timedelta(seconds=i))

    texts = [i.text for i in store.get_memory(USER_ID).insights]
    assert len(texts) == AGENT_INSIGHTS_MAX
    assert texts[0] == "insight 3" and texts[-1] == f"insight {AGENT_INSIGHTS_MAX + 2}"


def test_append_retries_after_losing_the_optimistic_lock(store, monkeypatch):
    assert store.append_insight(USER_ID, "first", SESSION, NOW)
    original = store.client.put_item
    failures = {"left": 1}

    def flaky(**kwargs):
        if failures["left"]:
            failures["left"] -= 1
            raise store.client.exceptions.ConditionalCheckFailedException(
                {"Error": {"Code": "ConditionalCheckFailedException", "Message": "lost"}},
                "PutItem",
            )
        return original(**kwargs)

    monkeypatch.setattr(store.client, "put_item", flaky)

    assert store.append_insight(USER_ID, "second", SESSION, NOW + timedelta(seconds=1))
    assert [i.text for i in store.get_memory(USER_ID).insights] == ["first", "second"]


def test_append_gives_up_after_three_lost_locks(store, monkeypatch):
    def always_lost(**kwargs):
        raise store.client.exceptions.ConditionalCheckFailedException(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "lost"}}, "PutItem"
        )

    monkeypatch.setattr(store.client, "put_item", always_lost)

    with pytest.raises(MemoryContentionError):
        store.append_insight(USER_ID, "x", SESSION, NOW)


def test_clear_memory_is_idempotent(store):
    store.append_insight(USER_ID, "gone soon", SESSION, NOW)

    store.clear_memory(USER_ID)
    store.clear_memory(USER_ID)

    assert store.get_memory(USER_ID).insights == []


def test_session_header_key_helper():
    assert agent_session_sk("abc") == "AGENT#abc"


# ----------------------------------------------------------------------
# Release: the fencing token's third verb
# ----------------------------------------------------------------------


def test_release_drops_the_claim_without_advancing(store):
    open_session(store)
    store.claim_turn(USER_ID, SESSION, engine="native", now=NOW)

    assert store.release_turn(USER_ID, SESSION, expected_turn=0)

    session = store.get_agent_session(USER_ID, SESSION)
    assert session is not None
    assert session.turn == 0 and session.in_flight is None
    # Immediately claimable again.
    store.claim_turn(USER_ID, SESSION, engine="native", now=NOW)


def test_release_with_a_stale_expected_turn_changes_nothing(store):
    open_session(store)
    store.claim_turn(USER_ID, SESSION, engine="native", now=NOW)
    assert store.commit_turn(USER_ID, SESSION, expected_turn=0, checkpoint=turn_item(0))
    store.claim_turn(USER_ID, SESSION, engine="native", now=NOW)

    assert not store.release_turn(USER_ID, SESSION, expected_turn=0)

    session = store.get_agent_session(USER_ID, SESSION)
    assert session is not None
    assert session.turn == 1 and session.in_flight == NOW


def test_release_without_a_claim_is_false(store):
    open_session(store)

    assert not store.release_turn(USER_ID, SESSION, expected_turn=0)


# ----------------------------------------------------------------------
# Proposals and confirmation: the fencing token's fourth verb
# ----------------------------------------------------------------------


def test_pending_brief_is_set_overwritten_and_cleared(store):
    open_session(store)

    assert store.set_pending_brief(USER_ID, SESSION, brief="first brief", duration_minutes=5)
    assert store.set_pending_brief(USER_ID, SESSION, brief="second brief", duration_minutes=8)
    session = store.get_agent_session(USER_ID, SESSION)
    assert session is not None
    assert (session.pending_brief, session.pending_duration_minutes) == ("second brief", 8)

    assert store.clear_pending_brief(USER_ID, SESSION)
    assert store.clear_pending_brief(USER_ID, SESSION)  # nothing pending: still fine
    session = store.get_agent_session(USER_ID, SESSION)
    assert session is not None
    assert session.pending_brief is None and session.pending_duration_minutes is None


def test_pending_brief_needs_an_active_session(store):
    open_session(store)
    store.mark_agent_session(USER_ID, SESSION, AgentSessionStatus.ABANDONED)

    assert not store.set_pending_brief(USER_ID, SESSION, brief="b", duration_minutes=5)
    assert not store.set_pending_brief(USER_ID, "nope", brief="b", duration_minutes=5)


def test_confirm_closes_the_session_without_advancing_the_turn(store):
    open_session(store)
    store.set_pending_brief(USER_ID, SESSION, brief="a brief", duration_minutes=5)
    store.claim_turn(USER_ID, SESSION, engine="native", now=NOW)

    assert store.confirm_session(USER_ID, SESSION, expected_turn=0, job_id="job-1")

    session = store.get_agent_session(USER_ID, SESSION)
    assert session is not None
    assert session.status is AgentSessionStatus.FINALIZED and session.job_id == "job-1"
    assert session.turn == 0 and session.in_flight is None
    assert session.pending_brief is None and session.pending_duration_minutes is None
    with pytest.raises(AgentTurnBusyError):
        store.claim_turn(USER_ID, SESSION, engine="native", now=NOW)


def test_confirm_requires_a_claim_and_a_proposal(store):
    open_session(store)
    store.set_pending_brief(USER_ID, SESSION, brief="a brief", duration_minutes=5)

    # No claim held.
    assert not store.confirm_session(USER_ID, SESSION, expected_turn=0, job_id="job-1")

    store.claim_turn(USER_ID, SESSION, engine="native", now=NOW)
    # Wrong turn.
    assert not store.confirm_session(USER_ID, SESSION, expected_turn=1, job_id="job-1")
    store.clear_pending_brief(USER_ID, SESSION)
    # Nothing pending.
    assert not store.confirm_session(USER_ID, SESSION, expected_turn=0, job_id="job-1")

    session = store.get_agent_session(USER_ID, SESSION)
    assert session is not None
    assert session.status is AgentSessionStatus.ACTIVE and session.in_flight == NOW


def test_session_count_reads_the_months_counter(store):
    assert store.get_agent_session_count(USER_ID, "2026-08") == 0
    store.reserve_agent_session(USER_ID, "2026-08", cap=5)
    store.reserve_agent_session(USER_ID, "2026-08", cap=5)

    assert store.get_agent_session_count(USER_ID, "2026-08") == 2
    assert store.get_agent_session_count(USER_ID, "2026-09") == 0
