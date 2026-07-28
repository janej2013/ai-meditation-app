"""Tests for user provisioning and the Cognito post-confirmation trigger.

Two properties matter here. Cognito can invoke a trigger more than once for the
same signup, so provisioning must never grant a second free credit. And a
DynamoDB failure must never block a signup -- the handler swallows errors, with
the API's lazy-init as the compensating control.
"""

from __future__ import annotations

from unittest.mock import Mock

from botocore.exceptions import ClientError

from functions.init_user.handler import SIGNUP_TRIGGER, lambda_handler
from shared.db import EntitlementStore
from shared.models import ENTITLEMENT_SK, PROFILE_SK, user_pk

from .conftest import USER_ID

EMAIL = "user@example.com"


def post_confirmation_event(
    sub: str = USER_ID,
    email: str = EMAIL,
    trigger_source: str = SIGNUP_TRIGGER,
) -> dict:
    return {
        "version": "1",
        "triggerSource": trigger_source,
        "region": "ap-southeast-2",
        "userPoolId": "ap-southeast-2_test",
        "userName": sub,
        "request": {
            "userAttributes": {
                "sub": sub,
                "email": email,
                "email_verified": "true",
            }
        },
        "response": {},
    }


def read_item(client, sk: str, user_id: str = USER_ID) -> dict | None:
    response = client.get_item(
        TableName="meditation-test-table",
        Key={"PK": {"S": user_pk(user_id)}, "SK": {"S": sk}},
    )
    return response.get("Item")


# ----------------------------------------------------------------------
# initialize_user
# ----------------------------------------------------------------------


def test_initialize_user_creates_profile_and_entitlement(store, dynamodb_client):
    granted = store.initialize_user(USER_ID, EMAIL)

    assert granted is True
    entitlement = store.get_entitlement(USER_ID)
    assert entitlement is not None
    assert (entitlement.available, entitlement.frozen) == (1, 0)
    assert entitlement.plan == "free"

    profile = read_item(dynamodb_client, PROFILE_SK)
    assert profile is not None
    assert profile["email"]["S"] == EMAIL


def test_initialize_user_twice_grants_only_one_credit(store):
    store.initialize_user(USER_ID, EMAIL)

    granted = store.initialize_user(USER_ID, EMAIL)

    assert granted is False
    entitlement = store.get_entitlement(USER_ID)
    assert entitlement is not None
    assert entitlement.available == 1


def test_initialize_user_does_not_reset_a_spent_balance(store):
    """A replayed trigger must not restore credits the user already spent."""
    store.initialize_user(USER_ID, EMAIL)
    store.freeze_credit(USER_ID, "job-1")
    store.commit_credit(USER_ID, "job-1")
    assert store.get_entitlement(USER_ID).available == 0

    store.initialize_user(USER_ID, EMAIL)

    assert store.get_entitlement(USER_ID).available == 0


def test_initialize_user_repairs_a_missing_entitlement(store, dynamodb_client):
    """Independent puts heal a partial write; a transaction could not."""
    dynamodb_client.put_item(
        TableName="meditation-test-table",
        Item={
            "PK": {"S": user_pk(USER_ID)},
            "SK": {"S": PROFILE_SK},
            "entity_type": {"S": "PROFILE"},
        },
    )
    assert read_item(dynamodb_client, ENTITLEMENT_SK) is None

    granted = store.initialize_user(USER_ID, EMAIL)

    assert granted is True
    assert store.get_entitlement(USER_ID).available == 1


def test_initialize_user_without_email_omits_the_attribute(store, dynamodb_client):
    store.initialize_user(USER_ID, None)

    profile = read_item(dynamodb_client, PROFILE_SK)
    assert profile is not None
    assert "email" not in profile


# ----------------------------------------------------------------------
# The Cognito adapter
# ----------------------------------------------------------------------


def test_handler_provisions_on_signup(store, monkeypatch):
    monkeypatch.setattr("functions.init_user.handler._get_store", lambda: store)
    event = post_confirmation_event()

    result = lambda_handler(event, None)

    assert result == event  # Cognito requires the event back unchanged.
    assert store.get_entitlement(USER_ID).available == 1


def test_handler_double_invocation_is_idempotent(store, monkeypatch):
    monkeypatch.setattr("functions.init_user.handler._get_store", lambda: store)
    event = post_confirmation_event()

    lambda_handler(event, None)
    lambda_handler(event, None)

    assert store.get_entitlement(USER_ID).available == 1


def test_handler_ignores_non_signup_trigger_sources(store, monkeypatch):
    monkeypatch.setattr("functions.init_user.handler._get_store", lambda: store)
    event = post_confirmation_event(trigger_source="PostConfirmation_ConfirmForgotPassword")

    lambda_handler(event, None)

    # Password reset must not provision, and must not grant a credit.
    assert store.get_entitlement(USER_ID) is None


def test_handler_survives_a_dynamodb_failure(monkeypatch):
    """A DynamoDB outage must not block signup."""
    broken = Mock(spec=EntitlementStore)
    broken.initialize_user.side_effect = ClientError(
        {"Error": {"Code": "InternalServerError", "Message": "boom"}}, "PutItem"
    )
    monkeypatch.setattr("functions.init_user.handler._get_store", lambda: broken)
    event = post_confirmation_event()

    result = lambda_handler(event, None)

    assert result == event
    broken.initialize_user.assert_called_once()


def test_handler_without_a_sub_does_not_raise(store, monkeypatch):
    monkeypatch.setattr("functions.init_user.handler._get_store", lambda: store)
    event = post_confirmation_event()
    del event["request"]["userAttributes"]["sub"]

    result = lambda_handler(event, None)

    assert result == event
