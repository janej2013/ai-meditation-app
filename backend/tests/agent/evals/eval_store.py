"""An in-memory stand-in for ``EntitlementStore`` with just what the tools
call. Not moto: ``mock_aws`` would intercept bedrock-runtime too, and the
point of an eval is the real model with a throwaway table."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from shared.models import Entitlement, Insight, Job, JobStatus, Memory

_PAST_JOBS = [
    Job(
        user_id="eval",
        job_id="past-1",
        status=JobStatus.DONE,
        mood_text="tense after a long week, wanted to sleep",
        duration_minutes=10,
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
        source="words",
    ),
    Job(
        user_id="eval",
        job_id="past-2",
        status=JobStatus.DONE,
        picture_key="pictures/eval/p.jpg",
        picture_keywords=["dusk", "shoreline", "quiet"],
        duration_minutes=5,
        created_at=datetime(2026, 8, 10, tzinfo=UTC),
        source="picture",
    ),
]


class EvalStore:
    def __init__(self, *, available: int = 5, frozen: int = 0, insights: list[str] | None = None):
        self._entitlement = Entitlement(user_id="eval", available=available, frozen=frozen)
        self.insights: list[Insight] = [
            Insight(text=t, created_at=datetime.now(UTC), session_id="seed") for t in insights or []
        ]
        self.jobs: dict[str, Job] = {}
        self.history_reads = 0

    # -- read by the tools --------------------------------------------
    def get_entitlement(self, user_id: str) -> Entitlement | None:  # noqa: ARG002
        return self._entitlement

    def list_done_jobs(self, user_id: str) -> list[Job]:  # noqa: ARG002
        self.history_reads += 1
        return list(_PAST_JOBS)

    def get_memory(self, user_id: str) -> Memory:  # noqa: ARG002
        return Memory(insights=list(self.insights))

    def append_insight(self, user_id: str, text: str, session_id: str, now: datetime) -> bool:  # noqa: ARG002
        if any(i.text.casefold() == text.casefold() for i in self.insights):
            return False
        self.insights.append(Insight(text=text, created_at=now, session_id=session_id))
        return True

    def get_job(self, user_id: str, job_id: str) -> Job | None:  # noqa: ARG002
        return self.jobs.get(job_id)

    def create_job(
        self,
        user_id: str,
        job_id: str,
        mood_text: str | None,
        duration_minutes: int,
        *_positional: Any,
        **kwargs: Any,
    ) -> bool:
        if job_id in self.jobs:
            return False
        self.jobs[job_id] = Job(
            user_id=user_id,
            job_id=job_id,
            status=JobStatus.PENDING,
            mood_text=mood_text,
            duration_minutes=duration_minutes,
            source=kwargs.get("source"),
            agent_session_id=kwargs.get("agent_session_id"),
        )
        return True


class FakeStartGeneration:
    """Records what finalize asked for; never touches Step Functions."""

    def __init__(self, store: EvalStore) -> None:
        self.store = store
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> bool:
        self.calls.append(kwargs)
        return (
            self.store.create_job(
                kwargs["user_id"],
                kwargs["job_id"],
                kwargs.get("mood_text"),
                kwargs["duration_minutes"],
                source=kwargs.get("source"),
                agent_session_id=kwargs.get("agent_session_id"),
            )
            or True
        )
