"""describe_picture: reads an authorised upload, asks Nova, writes the reading
to the PICTURE item -- before any job exists -- and never deletes the picture.

The error taxonomy matters as much as the happy path: only BedrockTransientError
is retried by the state machine; a missing object or an off-contract answer is
permanent, marks the item FAILED so the keywords screen stops waiting, and
fails the execution visibly.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from functions.describe_picture import handler
from functions.describe_picture.prompt import SYSTEM_PROMPT
from shared.pipeline import BedrockTransientError, PictureDescriptionError

from .conftest import BUCKET, USER_ID, bedrock_response, seed_entitlement

PICTURE_ID = "0f0e0d0c-0b0a-4908-8706-050403020100"
KEY = f"pictures/{USER_ID}/{PICTURE_ID}.jpg"
GOOD_ANSWER = {
    "keywords": ["dusk", "still water", "pine shore"],
    "summary": "You stand at the edge of a quiet lake as the light fades.",
}


ATTEMPT = "2026-08-25T00:00:00+00:00"


@pytest.fixture
def state() -> dict:
    return {"user_id": USER_ID, "picture_id": PICTURE_ID, "attempt": ATTEMPT}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("AUDIO_BUCKET", BUCKET)
    monkeypatch.setenv("BEDROCK_MODEL_ID", "amazon.nova-lite-v1:0")
    monkeypatch.delenv("DURATION_MINUTES_OVERRIDE", raising=False)


def prepare(store, dynamodb_client, s3_bucket, monkeypatch, *, upload: bool = True):
    from datetime import datetime

    seed_entitlement(dynamodb_client, available=1)
    assert store.create_picture(USER_ID, PICTURE_ID)
    # The route's claim: the execution runs under this attempt token.
    assert store.mark_picture_describing(USER_ID, PICTURE_ID, now=datetime.fromisoformat(ATTEMPT))
    if upload:
        s3_bucket.put_object(
            Bucket=BUCKET, Key=KEY, Body=b"\xff\xd8jpeg-bytes", ContentType="image/jpeg"
        )
    monkeypatch.setattr(handler, "_get_store", lambda: store)
    monkeypatch.setattr(handler, "_get_s3", lambda: s3_bucket)
    bedrock = MagicMock()
    monkeypatch.setattr(handler, "_get_bedrock", lambda: bedrock)
    return bedrock


def test_describes_the_picture_onto_its_item(
    store, dynamodb_client, s3_bucket, env, monkeypatch, state
):
    bedrock = prepare(store, dynamodb_client, s3_bucket, monkeypatch)
    bedrock.converse.return_value = bedrock_response(json.dumps(GOOD_ANSWER))

    result = handler.lambda_handler(state, None)

    picture = store.get_picture(USER_ID, PICTURE_ID)
    assert picture.status.value == "DESCRIBED"
    assert picture.keywords == GOOD_ANSWER["keywords"]
    assert picture.summary == GOOD_ANSWER["summary"]
    # The state passes through untouched: the description is item-only.
    assert {k: result[k] for k in state} == state
    assert "keywords" not in json.dumps(result)

    content = bedrock.converse.call_args.kwargs["messages"][0]["content"]
    assert content[0]["image"]["format"] == "jpeg"
    assert content[0]["image"]["source"]["bytes"] == b"\xff\xd8jpeg-bytes"


def test_the_picture_is_never_deleted(store, dynamodb_client, s3_bucket, env, monkeypatch, state):
    """It backs the replay feature and expires by lifecycle rule alone."""
    bedrock = prepare(store, dynamodb_client, s3_bucket, monkeypatch)
    bedrock.converse.return_value = bedrock_response(json.dumps(GOOD_ANSWER))
    spy = MagicMock(wraps=s3_bucket)
    monkeypatch.setattr(handler, "_get_s3", lambda: spy)

    handler.lambda_handler(state, None)

    spy.delete_object.assert_not_called()
    assert s3_bucket.head_object(Bucket=BUCKET, Key=KEY)["ContentLength"] > 0


def test_tolerates_prose_or_a_code_fence_around_the_json(
    store, dynamodb_client, s3_bucket, env, monkeypatch, state
):
    bedrock = prepare(store, dynamodb_client, s3_bucket, monkeypatch)
    bedrock.converse.return_value = bedrock_response(
        "Here you go:\n```json\n" + json.dumps(GOOD_ANSWER) + "\n```"
    )

    handler.lambda_handler(state, None)

    assert store.get_picture(USER_ID, PICTURE_ID).keywords == GOOD_ANSWER["keywords"]


@pytest.mark.parametrize(
    "code", ["ThrottlingException", "ServiceUnavailableException", "ModelTimeoutException"]
)
def test_transient_bedrock_errors_are_retryable(
    store, dynamodb_client, s3_bucket, env, monkeypatch, state, code
):
    bedrock = prepare(store, dynamodb_client, s3_bucket, monkeypatch)
    bedrock.converse.side_effect = ClientError({"Error": {"Code": code}}, "Converse")

    with pytest.raises(BedrockTransientError):
        handler.lambda_handler(state, None)
    assert store.get_picture(USER_ID, PICTURE_ID).status.value == "DESCRIBING"  # retry pending


def test_a_validation_error_is_permanent(
    store, dynamodb_client, s3_bucket, env, monkeypatch, state
):
    bedrock = prepare(store, dynamodb_client, s3_bucket, monkeypatch)
    bedrock.converse.side_effect = ClientError(
        {"Error": {"Code": "ValidationException", "Message": "bad image"}}, "Converse"
    )

    with pytest.raises(PictureDescriptionError) as excinfo:
        handler.lambda_handler(state, None)
    assert not isinstance(excinfo.value, BedrockTransientError)
    assert "bad image" in str(excinfo.value)


def test_a_missing_upload_is_permanent(store, dynamodb_client, s3_bucket, env, monkeypatch, state):
    """Described before the upload landed: permanent, and the item says so."""
    bedrock = prepare(store, dynamodb_client, s3_bucket, monkeypatch, upload=False)

    with pytest.raises(PictureDescriptionError, match="never uploaded"):
        handler.lambda_handler(state, None)
    bedrock.converse.assert_not_called()
    assert store.get_picture(USER_ID, PICTURE_ID).status.value == "FAILED"


def test_an_oversized_picture_is_permanent(
    store, dynamodb_client, s3_bucket, env, monkeypatch, state
):
    bedrock = prepare(store, dynamodb_client, s3_bucket, monkeypatch)
    monkeypatch.setattr(handler, "MAX_PICTURE_BYTES", 4)

    with pytest.raises(PictureDescriptionError, match="exceeds 4"):
        handler.lambda_handler(state, None)
    bedrock.converse.assert_not_called()


@pytest.mark.parametrize(
    "text",
    [
        "I cannot describe this picture.",
        json.dumps({"keywords": ["one"], "summary": "Too few keywords to be useful."}),
        json.dumps({"keywords": ["a", "b", "c"], "summary": "short"}),
        json.dumps(
            {"keywords": ["a", "b", "c", "d", "e", "f"], "summary": "Too many keywords here."}
        ),
    ],
    ids=["prose", "too-few", "summary-too-short", "too-many"],
)
def test_an_off_contract_answer_is_permanent_not_downgraded(
    store, dynamodb_client, s3_bucket, env, monkeypatch, state, text
):
    bedrock = prepare(store, dynamodb_client, s3_bucket, monkeypatch)
    bedrock.converse.return_value = bedrock_response(text)

    with pytest.raises(PictureDescriptionError):
        handler.lambda_handler(state, None)
    picture = store.get_picture(USER_ID, PICTURE_ID)
    assert picture.keywords is None
    assert picture.status.value == "FAILED"


def test_a_transport_error_is_left_for_the_machines_catch(
    store, dynamodb_client, s3_bucket, env, monkeypatch, state
):
    """Not a ClientError, so nothing in the taxonomy names it. The handler
    lets it out; the machine's Catch runs the mark_failed pass (below), the
    one place that knows every way an attempt can end."""
    from botocore.exceptions import EndpointConnectionError

    bedrock = prepare(store, dynamodb_client, s3_bucket, monkeypatch)
    bedrock.converse.side_effect = EndpointConnectionError(endpoint_url="https://bedrock")

    with pytest.raises(EndpointConnectionError):
        handler.lambda_handler(state, None)
    assert store.get_picture(USER_ID, PICTURE_ID).status.value == "DESCRIBING"


def test_mark_failed_mode_records_the_attempt_and_touches_nothing_else(
    store, dynamodb_client, s3_bucket, env, monkeypatch, state
):
    bedrock = prepare(store, dynamodb_client, s3_bucket, monkeypatch)

    handler.lambda_handler({**state, "mode": "mark_failed"}, None)

    assert store.get_picture(USER_ID, PICTURE_ID).status.value == "FAILED"
    bedrock.converse.assert_not_called()


def test_a_superseded_attempt_cannot_write(
    store, dynamodb_client, s3_bucket, env, monkeypatch, state
):
    """The route judged this attempt dead and claimed a new one; a late
    reading (or a late failure) from the old attempt must not clobber it."""
    from datetime import UTC, datetime

    bedrock = prepare(store, dynamodb_client, s3_bucket, monkeypatch)
    bedrock.converse.return_value = bedrock_response(json.dumps(GOOD_ANSWER))
    newer = datetime.now(UTC)
    assert store.mark_picture_describing(USER_ID, PICTURE_ID, now=newer)  # stale reclaim

    with pytest.raises(PictureDescriptionError, match="superseded"):
        handler.lambda_handler(state, None)  # still carries the OLD token
    handler.lambda_handler({**state, "mode": "mark_failed"}, None)  # old token: no-op

    picture = store.get_picture(USER_ID, PICTURE_ID)
    assert picture.status.value == "DESCRIBING"
    assert picture.keywords is None


def test_an_unauthorised_picture_id_is_refused(
    store, dynamodb_client, s3_bucket, env, monkeypatch, state
):
    """Only POST /pictures/upload creates the item; an execution for an id
    without one was never authorised and must not spend on Bedrock."""
    monkeypatch.setattr(handler, "_get_store", lambda: store)
    bedrock = MagicMock()
    monkeypatch.setattr(handler, "_get_bedrock", lambda: bedrock)

    with pytest.raises(PictureDescriptionError, match="never authorised"):
        handler.lambda_handler(state, None)
    bedrock.converse.assert_not_called()


def test_prompt_forbids_identifying_people_or_reading_text():
    """Constraint 7 on the vision side."""
    lowered = SYSTEM_PROMPT.lower()
    assert "never identify or describe any person" in lowered
    assert "never transcribe text" in lowered
    assert '"keywords"' in SYSTEM_PROMPT and '"summary"' in SYSTEM_PROMPT
