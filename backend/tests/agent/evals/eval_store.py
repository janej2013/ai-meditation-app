"""An in-memory stand-in for ``EntitlementStore`` with just what the tools
call. Not moto: ``mock_aws`` would intercept bedrock-runtime too, and the
point of an eval is the real model with a throwaway table."""

from __future__ import annotations

from datetime import UTC, datetime

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
        # The model's proposal, as the session header would hold it.
        self.pending: tuple[str, int] | None = None
        self.proposals = 0
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

    def set_pending_brief(
        self, user_id: str, session_id: str, *, brief: str, duration_minutes: int
    ) -> bool:
        self.pending = (brief, duration_minutes)
        self.proposals += 1
        return True

    def clear_pending_brief(self, user_id: str, session_id: str) -> bool:  # noqa: ARG002
        self.pending = None
        return True
