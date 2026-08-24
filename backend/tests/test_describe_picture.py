"""describe_picture: reads the key off the JOB item, asks Nova, writes the
description back -- and never deletes the picture.

The error taxonomy matters as much as the happy path: only BedrockTransientError
is retried by the state machine, so a missing object or an off-contract answer
must surface as PictureDescriptionError and fall straight through to rollback.
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

JOB = "job-picture"
KEY = f"pictures/{USER_ID}/0f0e0d0c-0b0a-4908-8706-050403020100.jpg"
GOOD_ANSWER = {
    "keywords": ["dusk", "still water", "pine shore"],
    "summary": "You stand at the edge of a quiet lake as the light fades.",
}


@pytest.fixture
def state() -> dict:
    return {"user_id": USER_ID, "job_id": JOB, "duration_minutes": 10, "has_picture": True}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("AUDIO_BUCKET", BUCKET)
    monkeypatch.setenv("BEDROCK_MODEL_ID", "amazon.nova-lite-v1:0")
    monkeypatch.delenv("DURATION_MINUTES_OVERRIDE", raising=False)


def prepare(store, dynamodb_client, s3_bucket, monkeypatch, *, upload: bool = True):
    seed_entitlement(dynamodb_client, available=1)
    store.create_job(USER_ID, JOB, "calm", 10, picture_key=KEY)
    store.freeze_credit(USER_ID, JOB)
    if upload:
        s3_bucket.put_object(
            Bucket=BUCKET, Key=KEY, Body=b"\xff\xd8jpeg-bytes", ContentType="image/jpeg"
        )
    monkeypatch.setattr(handler, "_get_store", lambda: store)
    monkeypatch.setattr(handler, "_get_s3", lambda: s3_bucket)
    bedrock = MagicMock()
    monkeypatch.setattr(handler, "_get_bedrock", lambda: bedrock)
    return bedrock


def test_describes_the_picture_onto_the_job_item(
    store, dynamodb_client, s3_bucket, env, monkeypatch, state
):
    bedrock = prepare(store, dynamodb_client, s3_bucket, monkeypatch)
    bedrock.converse.return_value = bedrock_response(json.dumps(GOOD_ANSWER))

    result = handler.lambda_handler(state, None)

    job = store.get_job(USER_ID, JOB)
    assert job.picture_keywords == GOOD_ANSWER["keywords"]
    assert job.picture_summary == GOOD_ANSWER["summary"]
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

    assert store.get_job(USER_ID, JOB).picture_keywords == GOOD_ANSWER["keywords"]


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
    """The user clicked Begin before the upload landed: fail and refund."""
    bedrock = prepare(store, dynamodb_client, s3_bucket, monkeypatch, upload=False)

    with pytest.raises(PictureDescriptionError, match="never uploaded"):
        handler.lambda_handler(state, None)
    bedrock.converse.assert_not_called()


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
    assert store.get_job(USER_ID, JOB).picture_keywords is None


def test_a_job_without_a_picture_key_fails(
    store, dynamodb_client, s3_bucket, env, monkeypatch, state
):
    """Routed here on has_picture, yet the item has no key: inconsistent, not skippable."""
    seed_entitlement(dynamodb_client, available=1)
    store.create_job(USER_ID, JOB, "calm", 10)
    monkeypatch.setattr(handler, "_get_store", lambda: store)

    with pytest.raises(PictureDescriptionError, match="no picture"):
        handler.lambda_handler(state, None)


def test_prompt_forbids_identifying_people_or_reading_text():
    """Constraint 7 on the vision side."""
    lowered = SYSTEM_PROMPT.lower()
    assert "never identify or describe any person" in lowered
    assert "never transcribe text" in lowered
    assert '"keywords"' in SYSTEM_PROMPT and '"summary"' in SYSTEM_PROMPT
