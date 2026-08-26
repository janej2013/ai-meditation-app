"""What a request needs, built once per process and injectable in tests.

The provider owns a boto3 client and the verifier a JWKS cache; both are
worth keeping across warm invocations, so they live on one ``Deps`` that
is created lazily on the first request. Tests replace ``get_deps`` through
FastAPI's ``dependency_overrides`` with a ``Deps`` over moto and fakes.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated, Any

import boto3
from fastapi import Depends, HTTPException, Request, status

from agent.native.llm.converse import BedrockConverseProvider
from agent_runner.auth import ID_TOKEN_HEADER, AuthError, CurrentUser, TokenVerifier
from agent_runner.settings import Settings
from shared.db import EntitlementStore

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class Deps:
    settings: Settings
    store: EntitlementStore
    # The model, in the engine's own terms: an ``LLMProvider`` for native,
    # a LangChain chat model for langgraph. Both carry ``model_id``.
    provider: Any
    sfn: Any
    verifier: TokenVerifier
    clock: Callable[[], datetime] = field(default=_utc_now)


_deps: Deps | None = None


def get_deps() -> Deps:
    global _deps
    if _deps is None:
        settings = Settings.from_env()
        _deps = Deps(
            settings=settings,
            store=EntitlementStore(table_name=settings.table_name),
            provider=_model_for(settings),
            sfn=boto3.client("stepfunctions", region_name=settings.aws_region),
            verifier=TokenVerifier.for_cognito(settings),
        )
    return _deps


def _model_for(settings: Settings) -> Any:
    if settings.engine == "langgraph":
        from agent.langgraph.engine import chat_model_from_env  # optional dependency

        return chat_model_from_env(region=settings.aws_region)
    return BedrockConverseProvider.from_env()


DepsDep = Annotated[Deps, Depends(get_deps)]


def current_user(request: Request, deps: DepsDep) -> CurrentUser:
    try:
        return deps.verifier.verify(
            request.headers.get("authorization"), request.headers.get(ID_TOKEN_HEADER)
        )
    except AuthError as exc:
        # The reason, never the token: enough to tell "no header arrived"
        # from "the token was bad" when reading the function's logs.
        logger.info("unauthenticated request: %s", exc)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from None


CurrentUserDep = Annotated[CurrentUser, Depends(current_user)]
