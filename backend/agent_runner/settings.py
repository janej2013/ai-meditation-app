"""Configuration, read once from the environment.

Every required value is checked up front so a misconfigured deployment
fails at the first request with the missing names, instead of much later
with an opaque boto3 or JWT error.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from shared.models import AGENT_SESSIONS_PER_MONTH, AgentEngineName

_REQUIRED = (
    "TABLE_NAME",
    "STATE_MACHINE_ARN",
    "COGNITO_USER_POOL_ID",
    "COGNITO_CLIENT_ID",
)


@dataclass(frozen=True)
class Settings:
    table_name: str
    state_machine_arn: str
    cognito_user_pool_id: str
    cognito_client_id: str
    aws_region: str
    engine: AgentEngineName = "native"
    # Which plans may open a session. Production is pro-only; a local run
    # against dev can admit the free test accounts (AGENT_ALLOWED_PLANS=pro,free).
    allowed_plans: frozenset[str] = field(default_factory=lambda: frozenset({"pro"}))
    sessions_per_month: int = AGENT_SESSIONS_PER_MONTH
    # A turn's budget when no Lambda context header is present (local runs).
    turn_seconds_fallback: int = 110
    heartbeat_seconds: float = 15.0

    @classmethod
    def from_env(cls) -> Settings:
        missing = [name for name in _REQUIRED if not os.environ.get(name)]
        region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
        if not region:
            missing.append("AWS_REGION")
        if missing:
            raise RuntimeError(f"agent runner is missing environment: {', '.join(missing)}")
        engine = os.environ.get("AGENT_ENGINE", "native")
        if engine not in ("native", "langgraph"):
            raise RuntimeError(f"AGENT_ENGINE must be native or langgraph, got {engine!r}")
        plans = os.environ.get("AGENT_ALLOWED_PLANS", "pro")
        return cls(
            table_name=os.environ["TABLE_NAME"],
            state_machine_arn=os.environ["STATE_MACHINE_ARN"],
            cognito_user_pool_id=os.environ["COGNITO_USER_POOL_ID"],
            cognito_client_id=os.environ["COGNITO_CLIENT_ID"],
            aws_region=region,
            engine=engine,  # type: ignore[arg-type]
            allowed_plans=frozenset(p.strip() for p in plans.split(",") if p.strip()),
            sessions_per_month=int(
                os.environ.get("AGENT_SESSIONS_PER_MONTH", AGENT_SESSIONS_PER_MONTH)
            ),
            turn_seconds_fallback=int(os.environ.get("AGENT_TURN_SECONDS_FALLBACK", 110)),
            heartbeat_seconds=float(os.environ.get("AGENT_HEARTBEAT_SECONDS", 15)),
        )

    @property
    def issuer(self) -> str:
        return f"https://cognito-idp.{self.aws_region}.amazonaws.com/{self.cognito_user_pool_id}"

    @property
    def jwks_url(self) -> str:
        return f"{self.issuer}/.well-known/jwks.json"
