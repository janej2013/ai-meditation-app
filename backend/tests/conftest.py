"""Shared fixtures: a moto-backed DynamoDB table matching the CDK data stack."""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import TYPE_CHECKING

import boto3
import pytest
from moto import mock_aws

from shared.db import EntitlementStore
from shared.models import ENTITLEMENT_SK, JobStatus, job_sk, user_pk

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient

TABLE_NAME = "meditation-test-table"
REGION = "ap-southeast-2"
USER_ID = "cognito-sub-123"
JOB_ID = "job-abc"


@pytest.fixture(autouse=True)
def _aws_credentials() -> Iterator[None]:
    """Keep moto from ever reaching real AWS."""
    env = {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "AWS_DEFAULT_REGION": REGION,
        "TABLE_NAME": TABLE_NAME,
    }
    previous = {key: os.environ.get(key) for key in env}
    os.environ.update(env)
    yield
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


@pytest.fixture
def dynamodb_client() -> Iterator[DynamoDBClient]:
    """A moto DynamoDB client with the single table created."""
    with mock_aws():
        client = boto3.client("dynamodb", region_name=REGION)
        client.create_table(
            TableName=TABLE_NAME,
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield client


@pytest.fixture
def store(dynamodb_client: DynamoDBClient) -> EntitlementStore:
    return EntitlementStore(table_name=TABLE_NAME, client=dynamodb_client)


def seed_entitlement(
    client: DynamoDBClient,
    user_id: str = USER_ID,
    available: int = 1,
    frozen: int = 0,
) -> None:
    """Write an ENTITLEMENT item for a user."""
    client.put_item(
        TableName=TABLE_NAME,
        Item={
            "PK": {"S": user_pk(user_id)},
            "SK": {"S": ENTITLEMENT_SK},
            "available": {"N": str(available)},
            "frozen": {"N": str(frozen)},
            "plan": {"S": "free"},
        },
    )


def seed_job(
    client: DynamoDBClient,
    job_id: str = JOB_ID,
    user_id: str = USER_ID,
    *,
    status: JobStatus,
) -> None:
    """Write a JOB item in a given status, as the API Lambda would."""
    client.put_item(
        TableName=TABLE_NAME,
        Item={
            "PK": {"S": user_pk(user_id)},
            "SK": {"S": job_sk(job_id)},
            "status": {"S": status.value},
            "job_id": {"S": job_id},
            "entity_type": {"S": "JOB"},
        },
    )


def set_available(client: DynamoDBClient, available: int, user_id: str = USER_ID) -> None:
    """Force the available counter, to simulate a balance changing over time."""
    client.update_item(
        TableName=TABLE_NAME,
        Key={"PK": {"S": user_pk(user_id)}, "SK": {"S": ENTITLEMENT_SK}},
        UpdateExpression="SET available = :a",
        ExpressionAttributeValues={":a": {"N": str(available)}},
    )


BUCKET = "meditation-test-audio"


@pytest.fixture
def s3_bucket(dynamodb_client):
    """An S3 bucket inside the same moto session the DynamoDB fixture opened."""
    client = boto3.client("s3", region_name="ap-southeast-2")
    client.create_bucket(
        Bucket=BUCKET,
        CreateBucketConfiguration={"LocationConstraint": "ap-southeast-2"},
    )
    return client


def bedrock_response(text: str, stop_reason: str = "end_turn") -> dict:
    """A minimal Converse response, as both Bedrock steps consume it."""
    return {
        "output": {"message": {"content": [{"text": text}]}},
        "stopReason": stop_reason,
    }


def patch_store(monkeypatch, module, store):
    monkeypatch.setattr(module, "_get_store", lambda: store)
