"""The runner under test: real FastAPI app, moto table, a scripted model, a
stubbed Step Functions client, and ID tokens signed with a key pair made
here -- so verification runs for real and nothing leaves the process."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from agent_runner.auth import TokenVerifier
from agent_runner.deps import Deps, get_deps
from agent_runner.main import create_app
from agent_runner.settings import Settings
from shared.models import ENTITLEMENT_SK, user_pk

from ..agent.fake_provider import FakeProvider
from ..conftest import TABLE_NAME, USER_ID

POOL_ID = "ap-southeast-2_test"
CLIENT_ID = "client-test"
REGION = "ap-southeast-2"
ISSUER = f"https://cognito-idp.{REGION}.amazonaws.com/{POOL_ID}"
ARN = "arn:aws:states:ap-southeast-2:123:stateMachine:m"
NOW = datetime(2026, 8, 25, 10, 0, tzinfo=UTC)
OTHER_USER = "cognito-sub-other"


@pytest.fixture(scope="session")
def rsa_keys() -> tuple[Any, Any]:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private, private.public_key()


@pytest.fixture(scope="session")
def other_rsa_keys() -> tuple[Any, Any]:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private, private.public_key()


def make_token(
    private_key: Any,
    sub: str | None = USER_ID,
    *,
    email: str | None = "test@example.com",
    token_use: str | None = "id",
    aud: str = CLIENT_ID,
    iss: str = ISSUER,
    expires_in: timedelta = timedelta(hours=1),
    kid: str = "k1",
) -> str:
    now = datetime.now(UTC)
    claims: dict[str, Any] = {
        "iss": iss,
        "aud": aud,
        "iat": int(now.timestamp()),
        "exp": int((now + expires_in).timestamp()),
    }
    if sub is not None:
        claims["sub"] = sub
    if email is not None:
        claims["email"] = email
    if token_use is not None:
        claims["token_use"] = token_use
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": kid})


@pytest.fixture
def settings() -> Settings:
    return Settings(
        table_name=TABLE_NAME,
        state_machine_arn=ARN,
        cognito_user_pool_id=POOL_ID,
        cognito_client_id=CLIENT_ID,
        aws_region=REGION,
    )


@pytest.fixture
def provider() -> FakeProvider:
    fake = FakeProvider([])
    fake.model_id = "fake-model"  # type: ignore[attr-defined]
    return fake


@pytest.fixture
def sfn() -> MagicMock:
    return MagicMock()


@pytest.fixture
def deps(store, settings, provider, sfn, rsa_keys, monkeypatch) -> Deps:
    monkeypatch.setenv("STATE_MACHINE_ARN", ARN)
    verifier = TokenVerifier(
        issuer=ISSUER, audience=CLIENT_ID, key_resolver=lambda _token: rsa_keys[1]
    )
    return Deps(
        settings=settings,
        store=store,
        provider=provider,
        sfn=sfn,
        verifier=verifier,
        clock=lambda: NOW,
    )


@pytest.fixture
def app(deps):
    application = create_app()
    application.dependency_overrides[get_deps] = lambda: deps
    return application


@pytest.fixture
def token(rsa_keys) -> str:
    return make_token(rsa_keys[0])


class Api:
    """Sync helpers over the ASGI app, one event loop per call -- the same
    asyncio.run style the agent tests use, no async test plugin."""

    def __init__(self, app, token: str | None) -> None:
        self.app = app
        self.token = token

    def _headers(self, headers: dict[str, str] | None, auth: bool) -> dict[str, str]:
        merged = dict(headers or {})
        if auth and self.token and "Authorization" not in merged:
            merged["Authorization"] = f"Bearer {self.token}"
        return merged

    def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        headers: dict[str, str] | None = None,
        auth: bool = True,
    ) -> httpx.Response:
        async def go() -> httpx.Response:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app), base_url="http://test"
            ) as client:
                return await client.request(
                    method, path, json=json, headers=self._headers(headers, auth)
                )

        return asyncio.run(go())

    def stream(
        self, path: str, *, json: Any, headers: dict[str, str] | None = None
    ) -> tuple[int, str]:
        """POST and read the whole SSE body."""

        async def go() -> tuple[int, str]:
            async with (
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=self.app), base_url="http://test"
                ) as client,
                client.stream(
                    "POST", path, json=json, headers=self._headers(headers, True)
                ) as response,
            ):
                chunks = [chunk async for chunk in response.aiter_text()]
                return response.status_code, "".join(chunks)

        return asyncio.run(go())


@pytest.fixture
def api(app, token) -> Api:
    return Api(app, token)


def sse_events(body: str) -> list[tuple[str, dict[str, Any]]]:
    """(event, data) pairs from an SSE body; comment lines (pings) dropped."""
    events: list[tuple[str, dict[str, Any]]] = []
    for frame in body.split("\n\n"):
        name, data = None, None
        for line in frame.splitlines():
            if line.startswith("event: "):
                name = line[len("event: ") :]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: ") :])
        if name is not None:
            events.append((name, data or {}))
    return events


def seed_pro_user(
    client, sub: str = USER_ID, *, available: int = 1, frozen: int = 0, plan: str = "pro"
) -> None:
    client.put_item(
        TableName=TABLE_NAME,
        Item={
            "PK": {"S": user_pk(sub)},
            "SK": {"S": ENTITLEMENT_SK},
            "available": {"N": str(available)},
            "frozen": {"N": str(frozen)},
            "plan": {"S": plan},
        },
    )


def create_session(api: Api) -> str:
    response = api.request("POST", "/agent/sessions")
    assert response.status_code == 201, response.text
    return response.json()["session_id"]


def _unused() -> Iterator[None]:  # keeps Iterator imported for fixtures that may need it
    yield
