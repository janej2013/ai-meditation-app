"""Credit ledger access for the DynamoDB single table.

Per CLAUDE.md constraint 1, every credit/entitlement mutation goes through this
module. Freeze, commit and rollback are each a single ``TransactWriteItems``
containing exactly two ``Update`` items, always in this order:

    index 0   SK = ENTITLEMENT      moves the counters
    index 1   SK = JOB#<job_id>     advances the job status

The JOB item is the idempotency guard. On a replay the job has already moved
past the transition, its condition fails, the whole transaction is cancelled,
and the counters are left untouched. That is what makes every operation safe to
retry with the same ``job_id`` -- which Step Functions will do.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

from shared.models import (
    AGENT_IN_FLIGHT_TIMEOUT_SECONDS,
    AGENT_INSIGHTS_MAX,
    AGENT_QUOTA_TTL_DAYS,
    AGENT_SESSION_TTL_DAYS,
    DEFAULT_PLAN,
    ENTITLEMENT_SK,
    FREE_SIGNUP_CREDITS,
    MEMORY_SK,
    PICTURE_DESCRIBE_MAX_ATTEMPTS,
    PICTURE_DESCRIBE_TIMEOUT_SECONDS,
    PICTURE_ITEM_TTL_DAYS,
    PROFILE_SK,
    AgentEngineName,
    AgentSession,
    AgentSessionStatus,
    AgentTurn,
    BillingOperationResult,
    CreditOperationResult,
    Entitlement,
    Insight,
    Job,
    JobSource,
    JobStatus,
    Memory,
    Picture,
    PictureDescription,
    PictureStatus,
    agent_quota_sk,
    agent_session_sk,
    agent_turn_sk,
    agent_turns_prefix,
    event_sk,
    job_sk,
    picture_sk,
    subscription_sk,
    user_pk,
)

if TYPE_CHECKING:  # boto3-stubs is a dev dependency, never installed in Lambda.
    from mypy_boto3_dynamodb.client import DynamoDBClient
    from mypy_boto3_dynamodb.type_defs import TransactWriteItemTypeDef

logger = logging.getLogger(__name__)

_serializer = TypeSerializer()
_deserializer = TypeDeserializer()

# Index of each item within the TransactWriteItems request. The order is load
# bearing: CancellationReasons comes back parallel to TransactItems, and the
# JOB reason must be inspected first (see _handle_cancellation).
_ENTITLEMENT_ITEM = 0
_JOB_ITEM = 1

# Billing transactions use the same index-0 entitlement slot, with the Stripe
# event dedupe item where the job item sits for credit operations.
_EVENT_ITEM = 1
_SUBSCRIPTION_ITEM = 2

_CONDITIONAL_CHECK_FAILED = "ConditionalCheckFailed"

# "status" and "plan" are DynamoDB reserved words.
_STATUS_NAMES = {"#status": "status"}
_PLAN_NAMES = {"#plan": "plan"}

# How long a processed-event marker is kept. Stripe retries a failed webhook
# delivery for up to ~3 days, so 30 covers every retry window with room to
# spare while keeping the partition from growing without bound.
EVENT_TTL_DAYS = 30


class CreditLedgerError(Exception):
    """Base class for credit ledger failures."""


class InsufficientCreditsError(CreditLedgerError):
    """The user has no available credit to freeze."""


class JobStateError(CreditLedgerError):
    """The job is not in a state that allows the requested transition."""


class AgentTurnBusyError(Exception):
    """The session is not ACTIVE, belongs to another engine, or another
    invocation holds a live claim on it. The harness answers 409."""


class MemoryContentionError(Exception):
    """Three consecutive optimistic-lock failures on the MEMORY item."""


# How many times append_insight re-reads after losing the optimistic lock.
# Contention is one user's own parallel tool calls -- two or three at most.
_MEMORY_WRITE_ATTEMPTS = 3


def _marshal(data: dict[str, Any]) -> dict[str, Any]:
    """Marshal a plain dict (item key or expression values) into AttributeValues."""
    return {key: _serializer.serialize(value) for key, value in data.items()}


def _unmarshal(item: dict[str, Any]) -> dict[str, Any]:
    """Unmarshal a DynamoDB item into a plain dict."""
    return {key: _deserializer.deserialize(value) for key, value in item.items()}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _dynamo_json(value: Any) -> Any:
    """JSON-shaped data as DynamoDB accepts it: floats become Decimals,
    which the serializer wants, and nothing else changes."""
    return json.loads(json.dumps(value), parse_float=Decimal)


def _plain(value: Any) -> Any:
    """The inverse: Decimals back to int or float, recursively, so a
    read-back item can be handed to json.dumps and to the model layer."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def _ttl_epoch(days: int) -> int:
    """Absolute epoch seconds DynamoDB's TTL reaper compares against."""
    return int((datetime.now(UTC) + timedelta(days=days)).timestamp())


class EntitlementStore:
    """Reads and mutates user entitlements and job status.

    ``table_name`` defaults to the ``TABLE_NAME`` environment variable, which
    CDK wires into every Lambda. ``client`` is injectable for tests.
    """

    def __init__(self, table_name: str | None = None, client: DynamoDBClient | None = None) -> None:
        self.table_name = table_name or os.environ["TABLE_NAME"]
        self.client: DynamoDBClient = client if client is not None else boto3.client("dynamodb")

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get_entitlement(self, user_id: str) -> Entitlement | None:
        response = self.client.get_item(
            TableName=self.table_name,
            Key=_marshal({"PK": user_pk(user_id), "SK": ENTITLEMENT_SK}),
            ConsistentRead=True,
        )
        item = response.get("Item")
        if not item:
            return None
        return Entitlement.model_validate({**_unmarshal(item), "user_id": user_id})

    def get_job(self, user_id: str, job_id: str) -> Job | None:
        response = self.client.get_item(
            TableName=self.table_name,
            Key=_marshal({"PK": user_pk(user_id), "SK": job_sk(job_id)}),
            ConsistentRead=True,
        )
        item = response.get("Item")
        if not item:
            return None
        return Job.model_validate({**_unmarshal(item), "user_id": user_id, "job_id": job_id})

    # ------------------------------------------------------------------
    # Provisioning
    # ------------------------------------------------------------------

    def initialize_user(self, user_id: str, email: str | None = None) -> bool:
        """Create the PROFILE and ENTITLEMENT items for a new user.

        Grants the free signup credit. Lives here rather than in the Cognito
        trigger because creating ENTITLEMENT is an entitlement mutation, and
        constraint 1 routes all of those through this module. The Cognito
        post-confirmation trigger and the API's lazy-init path both call it.

        Idempotent via ``attribute_not_exists(PK)`` on each put, because Cognito
        can invoke a trigger more than once for the same signup.

        The two puts are deliberately independent rather than a transaction: if
        an earlier partial write left PROFILE present but ENTITLEMENT missing, a
        transaction would fail as a whole and never repair the gap, whereas
        independent conditional puts heal it.

        Returns True when this call created the ENTITLEMENT item -- i.e. when
        the free credit was actually granted.
        """
        now = _now_iso()

        profile: dict[str, Any] = {
            "PK": user_pk(user_id),
            "SK": PROFILE_SK,
            "entity_type": "PROFILE",
            "created_at": now,
        }
        if email:
            profile["email"] = email
        self._put_if_absent(profile)

        granted = self._put_if_absent(
            {
                "PK": user_pk(user_id),
                "SK": ENTITLEMENT_SK,
                "entity_type": "ENTITLEMENT",
                "available": FREE_SIGNUP_CREDITS,
                "frozen": 0,
                "plan": DEFAULT_PLAN,
                "created_at": now,
                "updated_at": now,
            }
        )
        if granted:
            logger.info("initialized user sub=%s free_credits=%s", user_id, FREE_SIGNUP_CREDITS)
        return granted

    def _put_if_absent(self, item: dict[str, Any]) -> bool:
        """Put an item only if its key is unused. True if written."""
        try:
            self.client.put_item(
                TableName=self.table_name,
                Item=_marshal(item),
                ConditionExpression="attribute_not_exists(PK)",
            )
        except self.client.exceptions.ConditionalCheckFailedException:
            return False
        return True

    # ------------------------------------------------------------------
    # Job lifecycle (non-credit attributes)
    # ------------------------------------------------------------------

    def create_job(
        self,
        user_id: str,
        job_id: str,
        mood_text: str | None,
        duration_minutes: int,
        picture_key: str | None = None,
        picture: PictureDescription | None = None,
        *,
        source: JobSource | None = None,
        agent_session_id: str | None = None,
    ) -> bool:
        """Create a PENDING job. False if the job_id was already used.

        A job drifts from words (``mood_text``) or from a picture (its key and
        the description the vision step already produced) -- never both. All
        of it is stored here rather than passed through the state machine,
        keeping user input out of the execution history (constraint 7).
        """
        now = _now_iso()
        item: dict[str, Any] = {
            "PK": user_pk(user_id),
            "SK": job_sk(job_id),
            "entity_type": "JOB",
            "job_id": job_id,
            "status": JobStatus.PENDING.value,
            "duration_minutes": duration_minutes,
            "created_at": now,
            "updated_at": now,
        }
        if mood_text:
            item["mood_text"] = mood_text
        if picture_key:
            item["picture_key"] = picture_key
        if picture is not None:
            item["picture_keywords"] = picture.keywords
            item["picture_summary"] = picture.summary
        if source is not None:
            item["source"] = source
        if agent_session_id is not None:
            item["agent_session_id"] = agent_session_id
        return self._put_if_absent(item)

    # ------------------------------------------------------------------
    # Pictures (described before any job exists)
    # ------------------------------------------------------------------

    def create_picture(self, user_id: str, picture_id: str) -> bool:
        """Record an upload the caller was authorised for. False on replay."""
        return self._put_if_absent(
            {
                "PK": user_pk(user_id),
                "SK": picture_sk(picture_id),
                "entity_type": "PICTURE",
                "picture_id": picture_id,
                "status": PictureStatus.PENDING.value,
                "created_at": _now_iso(),
                "expires_at": _ttl_epoch(PICTURE_ITEM_TTL_DAYS),
            }
        )

    def get_picture(self, user_id: str, picture_id: str) -> Picture | None:
        response = self.client.get_item(
            TableName=self.table_name,
            Key=_marshal({"PK": user_pk(user_id), "SK": picture_sk(picture_id)}),
            ConsistentRead=True,
        )
        item = response.get("Item")
        if not item:
            return None
        return Picture.model_validate({**_unmarshal(item), "user_id": user_id})

    def mark_picture_describing(
        self, user_id: str, picture_id: str, *, now: datetime
    ) -> str | None:
        """Claim the next description attempt; the attempt token if won.

        The item, not the execution name, is the idempotency anchor: PENDING
        and FAILED may be (re)claimed, and DESCRIBING only once its attempt
        is older than the machine's timeout -- so an attempt that died
        without marking the item does not strand the picture, while two
        quick taps cannot start two executions. The claim also counts: past
        PICTURE_DESCRIBE_MAX_ATTEMPTS the picture is not tried again, since
        each try is Bedrock spend no credit has paid for.

        The token is the claim timestamp; every write the attempt makes is
        conditioned on it (see set_picture_description / mark_picture_failed).
        """
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware: it is compared with stored UTC stamps")
        attempt = now.isoformat()
        stale = (now - timedelta(seconds=PICTURE_DESCRIBE_TIMEOUT_SECONDS)).isoformat()
        won = self._conditional_update(
            user_id,
            picture_sk(picture_id),
            kind="picture",
            update=(
                "SET #status = :describing, describe_started_at = :now, "
                "describe_attempts = if_not_exists(describe_attempts, :zero) + :one"
            ),
            condition=(
                "attribute_exists(PK) AND "
                "(attribute_not_exists(describe_attempts) OR describe_attempts < :max) AND ("
                "#status IN (:pending, :failed) OR "
                "(#status = :describing AND describe_started_at < :stale))"
            ),
            names=dict(_STATUS_NAMES),
            values={
                ":describing": PictureStatus.DESCRIBING.value,
                ":pending": PictureStatus.PENDING.value,
                ":failed": PictureStatus.FAILED.value,
                ":now": attempt,
                ":stale": stale,
                ":zero": 0,
                ":one": 1,
                ":max": PICTURE_DESCRIBE_MAX_ATTEMPTS,
            },
        )
        return attempt if won else None

    def set_picture_description(
        self, user_id: str, picture_id: str, description: PictureDescription, *, attempt: str
    ) -> bool:
        """Record what the vision model saw -- for this attempt only. A retry
        of the same attempt rewrites the same reading (and may lift a FAILED
        the route wrote when it wrongly believed nothing had started); a
        write from a reclaimed, dead attempt is rejected."""
        return self._conditional_update(
            user_id,
            picture_sk(picture_id),
            kind="picture",
            update="SET #status = :described, keywords = :kw, summary = :summary",
            condition=(
                "attribute_exists(PK) AND describe_started_at = :attempt "
                "AND #status IN (:describing, :failed, :described)"
            ),
            names=dict(_STATUS_NAMES),
            values={
                ":attempt": attempt,
                ":described": PictureStatus.DESCRIBED.value,
                ":describing": PictureStatus.DESCRIBING.value,
                ":failed": PictureStatus.FAILED.value,
                ":kw": description.keywords,
                ":summary": description.summary,
            },
        )

    def mark_picture_failed(self, user_id: str, picture_id: str, *, attempt: str) -> bool:
        """This attempt gave up: the keywords screen stops waiting, and a new
        attempt may be claimed. A stale attempt cannot fail its successor."""
        return self._conditional_update(
            user_id,
            picture_sk(picture_id),
            kind="picture",
            update="SET #status = :failed",
            condition=(
                "attribute_exists(PK) AND describe_started_at = :attempt AND #status = :describing"
            ),
            names=dict(_STATUS_NAMES),
            values={
                ":attempt": attempt,
                ":failed": PictureStatus.FAILED.value,
                ":describing": PictureStatus.DESCRIBING.value,
            },
        )

    def mark_job_generating(self, user_id: str, job_id: str) -> None:
        """Advance a frozen job to GENERATING. No-op if already generating."""
        self._update_job(
            user_id,
            job_id,
            update="SET #status = :generating, updated_at = :now",
            condition="attribute_exists(PK) AND #status IN (:frozen, :generating)",
            names=dict(_STATUS_NAMES),
            values={
                ":generating": JobStatus.GENERATING.value,
                ":frozen": JobStatus.FROZEN.value,
                ":now": _now_iso(),
            },
        )

    def set_job_script_key(self, user_id: str, job_id: str, script_key: str) -> None:
        self._update_job(
            user_id,
            job_id,
            update="SET script_key = :key, updated_at = :now",
            condition="attribute_exists(PK)",
            values={":key": script_key, ":now": _now_iso()},
        )

    def set_job_audio_key(self, user_id: str, job_id: str, audio_key: str) -> None:
        """Record the finished audio.

        Deliberately does NOT set status=DONE. ``commit_credit`` owns that
        transition, because it writes DONE in the same transaction that
        decrements ``frozen``. Setting DONE here would make commit's condition
        (`status IN (FROZEN, GENERATING)`) fail, cancelling the transaction and
        leaving the credit frozen forever.
        """
        self._update_job(
            user_id,
            job_id,
            update="SET audio_key = :key, updated_at = :now",
            condition="attribute_exists(PK)",
            values={":key": audio_key, ":now": _now_iso()},
        )

    def list_done_jobs(self, user_id: str) -> list[Job]:
        """Every DONE job in the caller's partition, unsorted.

        Reads the whole partition on purpose: job_ids are uuid4, so SK order
        is not time order, and the partition is hard-bounded (every job costs
        a paid credit -- hundreds at most, a few hundred bytes each with this
        projection). A GSI would double the write bill for a query this small;
        per CLAUDE.md, adding one needs a proposal first, and this method is
        the argument against. Sorting and pagination happen in the caller.

        The status filter trims transport only -- correctness never depends on
        a FilterExpression.
        """
        jobs: list[Job] = []
        kwargs: dict[str, Any] = {
            "TableName": self.table_name,
            "KeyConditionExpression": "PK = :pk AND begins_with(SK, :job)",
            "FilterExpression": "#status = :done",
            # "source" is a reserved word too.
            "ExpressionAttributeNames": {**_STATUS_NAMES, "#source": "source"},
            "ExpressionAttributeValues": _marshal(
                {":pk": user_pk(user_id), ":job": "JOB#", ":done": JobStatus.DONE.value}
            ),
            "ProjectionExpression": (
                "job_id, #status, picture_keywords, mood_text, "
                "duration_minutes, picture_key, created_at, #source"
            ),
        }
        while True:
            response = self.client.query(**kwargs)
            jobs.extend(
                Job.model_validate({**_unmarshal(item), "user_id": user_id})
                for item in response.get("Items", [])
            )
            last = response.get("LastEvaluatedKey")
            if not last:
                return jobs
            kwargs["ExclusiveStartKey"] = last

    def mark_job_deleted(self, user_id: str, job_id: str) -> bool:
        """DONE -> DELETED, idempotently. True when the job is now DELETED.

        A job already DELETED passes the condition again on purpose: that is
        the retry anchor -- the caller re-runs the S3 cleanup either way, so a
        delete whose S3 step failed last time heals on the next attempt. False
        means the item is missing or in flight, which the route treats as 404.
        This is a status update, not a credit mutation (constraint 1 untouched).
        """
        return self._update_job(
            user_id,
            job_id,
            update="SET #status = :deleted, updated_at = :now",
            condition="attribute_exists(PK) AND #status IN (:done, :deleted)",
            names=dict(_STATUS_NAMES),
            values={
                ":deleted": JobStatus.DELETED.value,
                ":done": JobStatus.DONE.value,
                ":now": _now_iso(),
            },
        )

    def _update_job(
        self,
        user_id: str,
        job_id: str,
        *,
        update: str,
        condition: str,
        values: dict[str, Any],
        names: dict[str, str] | None = None,
    ) -> bool:
        return self._conditional_update(
            user_id,
            job_sk(job_id),
            kind="job",
            update=update,
            condition=condition,
            values=values,
            names=names,
        )

    def _conditional_update(
        self,
        user_id: str,
        sk: str,
        *,
        kind: str,
        update: str,
        condition: str,
        values: dict[str, Any],
        names: dict[str, str] | None = None,
    ) -> bool:
        """Conditional update on one item in the user's partition; a failed
        condition is a no-op. Serves JOB and PICTURE items alike.

        Returns True when the update was applied, False when the condition
        rejected it -- callers that need to know (the delete route's 404)
        read that; the pipeline's status writers ignore it.

        Every condition here is ``attribute_exists(PK)`` plus, sometimes, a
        status allow-list, and those two halves fail for very different reasons.
        A status that has already moved on is the benign replay a retried task
        produces. A missing item means the job row does not exist at all, which
        cannot happen on a healthy path -- ``create_job`` succeeds before the
        execution starts -- and is worth finding in the logs.

        ``ReturnValuesOnConditionCheckFailure`` tells the two apart without a
        second read: DynamoDB attaches the item as it stood when the condition
        failed, so an absent one is the missing-row case.

        Neither case raises. These run on retry paths where a replay must stay
        harmless, and the credit ledger still fails loudly at ``commit_credit``
        if the job really is gone.
        """
        kwargs: dict[str, Any] = {
            "TableName": self.table_name,
            "Key": _marshal({"PK": user_pk(user_id), "SK": sk}),
            "UpdateExpression": update,
            "ConditionExpression": condition,
            "ExpressionAttributeValues": _marshal(values),
            "ReturnValuesOnConditionCheckFailure": "ALL_OLD",
        }
        if names:
            kwargs["ExpressionAttributeNames"] = names
        try:
            self.client.update_item(**kwargs)
        except self.client.exceptions.ConditionalCheckFailedException as exc:
            # The id after the prefix; never user content (constraint 7).
            item_id = sk.partition("#")[2]
            item = (getattr(exc, "response", None) or {}).get("Item")
            if not item:
                logger.warning("%s update skipped: no %s item %s_id=%s", kind, kind, kind, item_id)
                return False
            # Read the one attribute directly rather than unmarshalling the
            # whole item, which may hold mood_text.
            status = item.get("status", {}).get("S", "UNKNOWN")
            logger.info(
                "%s update skipped (replay) %s_id=%s status=%s", kind, kind, item_id, status
            )
            return False
        return True

    # ------------------------------------------------------------------
    # Credit operations
    # ------------------------------------------------------------------

    def freeze_credit(self, user_id: str, job_id: str) -> CreditOperationResult:
        """Reserve one credit for ``job_id``: ``available -1``, ``frozen +1``.

        Idempotent: a second call for the same ``job_id`` is a no-op returning
        ``applied=False``. Raises :class:`InsufficientCreditsError` when the
        user has no available credit (or no entitlement item at all).
        """
        now = _now_iso()
        entitlement_update: TransactWriteItemTypeDef = {
            "Update": {
                "TableName": self.table_name,
                "Key": _marshal({"PK": user_pk(user_id), "SK": ENTITLEMENT_SK}),
                "ConditionExpression": "attribute_exists(PK) AND available >= :one",
                "UpdateExpression": ("SET available = available - :one, frozen = frozen + :one"),
                "ExpressionAttributeValues": _marshal({":one": 1}),
            }
        }
        job_update: TransactWriteItemTypeDef = {
            "Update": {
                "TableName": self.table_name,
                "Key": _marshal({"PK": user_pk(user_id), "SK": job_sk(job_id)}),
                # Accepts a job the API Lambda pre-created as PENDING, or no job
                # item at all. Any other status means the freeze already ran.
                "ConditionExpression": "attribute_not_exists(PK) OR #status = :pending",
                "UpdateExpression": (
                    "SET #status = :frozen, updated_at = :now, "
                    "created_at = if_not_exists(created_at, :now), "
                    "job_id = if_not_exists(job_id, :job_id), "
                    "entity_type = if_not_exists(entity_type, :entity_type)"
                ),
                "ExpressionAttributeNames": dict(_STATUS_NAMES),
                "ExpressionAttributeValues": _marshal(
                    {
                        ":pending": JobStatus.PENDING.value,
                        ":frozen": JobStatus.FROZEN.value,
                        ":now": now,
                        ":job_id": job_id,
                        ":entity_type": "JOB",
                    }
                ),
            }
        }

        return self._run_credit_transaction(
            user_id=user_id,
            job_id=job_id,
            operation="freeze",
            entitlement_update=entitlement_update,
            job_update=job_update,
            replay_statuses={
                JobStatus.FROZEN,
                JobStatus.GENERATING,
                JobStatus.DONE,
                JobStatus.FAILED,
                JobStatus.ROLLED_BACK,
                JobStatus.DELETED,
            },
            on_entitlement_failure=InsufficientCreditsError(
                f"user has no available credit to freeze for job {job_id}"
            ),
        )

    def commit_credit(self, user_id: str, job_id: str) -> CreditOperationResult:
        """Consume the frozen credit for ``job_id``: ``frozen -1``.

        ``available`` is deliberately not restored -- the credit is spent.
        Idempotent: committing an already-committed job returns
        ``applied=False``.
        """
        now = _now_iso()
        entitlement_update: TransactWriteItemTypeDef = {
            "Update": {
                "TableName": self.table_name,
                "Key": _marshal({"PK": user_pk(user_id), "SK": ENTITLEMENT_SK}),
                "ConditionExpression": "attribute_exists(PK) AND frozen >= :one",
                "UpdateExpression": "SET frozen = frozen - :one",
                "ExpressionAttributeValues": _marshal({":one": 1}),
            }
        }
        job_update: TransactWriteItemTypeDef = {
            "Update": {
                "TableName": self.table_name,
                "Key": _marshal({"PK": user_pk(user_id), "SK": job_sk(job_id)}),
                "ConditionExpression": (
                    "attribute_exists(PK) AND #status IN (:frozen, :generating)"
                ),
                "UpdateExpression": "SET #status = :done, updated_at = :now",
                "ExpressionAttributeNames": dict(_STATUS_NAMES),
                "ExpressionAttributeValues": _marshal(
                    {
                        ":frozen": JobStatus.FROZEN.value,
                        ":generating": JobStatus.GENERATING.value,
                        ":done": JobStatus.DONE.value,
                        ":now": now,
                    }
                ),
            }
        }

        return self._run_credit_transaction(
            user_id=user_id,
            job_id=job_id,
            operation="commit",
            entitlement_update=entitlement_update,
            job_update=job_update,
            # DELETED: the user let the finished dreamscape go before a retried
            # commit landed -- still a replay, not a state error.
            replay_statuses={JobStatus.DONE, JobStatus.DELETED},
            on_entitlement_failure=CreditLedgerError(
                f"no frozen credit to commit for job {job_id}"
            ),
        )

    def rollback_credit(self, user_id: str, job_id: str) -> CreditOperationResult:
        """Return the frozen credit for ``job_id``: ``frozen -1``, ``available +1``.

        Idempotent, and a no-op in two cases that matter operationally:

        * the job is already ``DONE`` -- a consumed credit is never refunded;
        * the job never froze anything (``PENDING`` or no item). Constraint 3
          puts a ``Catch`` on every task including ``freeze_credit`` itself, so
          this Lambda runs for jobs that never reached ``FROZEN``.
        """
        now = _now_iso()
        entitlement_update: TransactWriteItemTypeDef = {
            "Update": {
                "TableName": self.table_name,
                "Key": _marshal({"PK": user_pk(user_id), "SK": ENTITLEMENT_SK}),
                "ConditionExpression": "attribute_exists(PK) AND frozen >= :one",
                "UpdateExpression": ("SET frozen = frozen - :one, available = available + :one"),
                "ExpressionAttributeValues": _marshal({":one": 1}),
            }
        }
        job_update: TransactWriteItemTypeDef = {
            "Update": {
                "TableName": self.table_name,
                "Key": _marshal({"PK": user_pk(user_id), "SK": job_sk(job_id)}),
                "ConditionExpression": (
                    "attribute_exists(PK) AND #status IN (:frozen, :generating, :failed)"
                ),
                "UpdateExpression": "SET #status = :rolled_back, updated_at = :now",
                "ExpressionAttributeNames": dict(_STATUS_NAMES),
                "ExpressionAttributeValues": _marshal(
                    {
                        ":frozen": JobStatus.FROZEN.value,
                        ":generating": JobStatus.GENERATING.value,
                        ":failed": JobStatus.FAILED.value,
                        ":rolled_back": JobStatus.ROLLED_BACK.value,
                        ":now": now,
                    }
                ),
            }
        }

        return self._run_credit_transaction(
            user_id=user_id,
            job_id=job_id,
            operation="rollback",
            entitlement_update=entitlement_update,
            job_update=job_update,
            # A job that never froze is nothing to refund, so treat PENDING and
            # a missing job item as a replay rather than an error.
            replay_statuses={
                JobStatus.PENDING,
                JobStatus.DONE,
                JobStatus.ROLLED_BACK,
                JobStatus.DELETED,
            },
            replay_when_job_missing=True,
            on_entitlement_failure=CreditLedgerError(
                f"no frozen credit to roll back for job {job_id}"
            ),
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Billing -- entitlement changes driven by Stripe
    #
    # Same shape as the credit operations above: one TransactWriteItems, with
    # a conditional item that makes a replay a no-op. Here the guard is an
    # EVENT#<stripe_event_id> marker rather than the job status, because Stripe
    # retries deliveries and constraint 5 requires the update to be idempotent
    # on the event id.
    #
    # These take plain data, never a Stripe object: shared/ does not import
    # stripe, so the vendor stays behind the API layer.
    # ------------------------------------------------------------------

    def apply_stripe_credit(
        self,
        user_id: str,
        event_id: str,
        credits: int,
    ) -> BillingOperationResult:
        """Add ``credits`` to ``available`` for a one-off credit pack.

        Idempotent per ``event_id``: a redelivered webhook finds the marker
        already written, the transaction cancels, and nothing is added.
        """
        if credits <= 0:
            raise ValueError(f"credit pack must grant a positive amount, got {credits}")

        now = _now_iso()
        entitlement_update: TransactWriteItemTypeDef = {
            "Update": {
                "TableName": self.table_name,
                "Key": _marshal({"PK": user_pk(user_id), "SK": ENTITLEMENT_SK}),
                # No attribute_exists guard and if_not_exists on every counter:
                # a paid-for top-up must land even if the ENTITLEMENT item is
                # somehow missing. Dropping a purchase is worse than creating
                # the row the signup trigger should already have made.
                "UpdateExpression": (
                    "SET available = if_not_exists(available, :zero) + :credits, "
                    "frozen = if_not_exists(frozen, :zero), "
                    "#plan = if_not_exists(#plan, :free), "
                    "updated_at = :now"
                ),
                "ExpressionAttributeNames": dict(_PLAN_NAMES),
                "ExpressionAttributeValues": _marshal(
                    {":credits": credits, ":zero": 0, ":free": DEFAULT_PLAN, ":now": now}
                ),
            }
        }

        return self._run_billing_transaction(
            user_id=user_id,
            event_id=event_id,
            operation="credit_pack",
            entitlement_update=entitlement_update,
            event_detail={"credits": credits},
        )

    def apply_subscription_update(
        self,
        user_id: str,
        event_id: str,
        *,
        plan: str,
        period_end: str | None = None,
        credits: int = 0,
        subscription_id: str | None = None,
    ) -> BillingOperationResult:
        """Set the plan, optionally grant the period's credits, record the sub.

        Covers all three subscription transitions:

        * first payment -- plan, period_end, credits and a SUB# item;
        * renewal (``invoice.paid``) -- a later period_end plus fresh credits;
        * cancellation -- ``plan="free"`` with ``credits=0``, which clears
          period_end and deliberately leaves ``available`` alone. Credits
          already paid for stay spendable.

        Idempotent per ``event_id``, exactly as ``apply_stripe_credit``.
        """
        if credits < 0:
            raise ValueError(f"subscription credits cannot be negative, got {credits}")

        now = _now_iso()
        set_clauses = [
            "frozen = if_not_exists(frozen, :zero)",
            "#plan = :plan",
            "updated_at = :now",
        ]
        values: dict[str, Any] = {":zero": 0, ":plan": plan, ":now": now}

        if credits:
            set_clauses.insert(0, "available = if_not_exists(available, :zero) + :credits")
            values[":credits"] = credits
        else:
            # Still guarantee the attribute exists, so a later freeze finds an
            # integer rather than a missing field.
            set_clauses.insert(0, "available = if_not_exists(available, :zero)")

        remove_clause = ""
        if period_end is not None:
            set_clauses.append("period_end = :period_end")
            values[":period_end"] = period_end
        else:
            # Cancellation: no period to be inside any more.
            remove_clause = " REMOVE period_end"

        entitlement_update: TransactWriteItemTypeDef = {
            "Update": {
                "TableName": self.table_name,
                "Key": _marshal({"PK": user_pk(user_id), "SK": ENTITLEMENT_SK}),
                "UpdateExpression": f"SET {', '.join(set_clauses)}{remove_clause}",
                "ExpressionAttributeNames": dict(_PLAN_NAMES),
                "ExpressionAttributeValues": _marshal(values),
            }
        }

        extra: list[TransactWriteItemTypeDef] = []
        if subscription_id:
            # Unconditional put: a renewal overwrites the same key with a later
            # period_end, and the EVENT marker already provides the replay
            # guard, so a condition here would only add a second failure mode.
            extra.append(
                {
                    "Put": {
                        "TableName": self.table_name,
                        "Item": _marshal(
                            {
                                "PK": user_pk(user_id),
                                "SK": subscription_sk(subscription_id),
                                "entity_type": "SUBSCRIPTION",
                                "subscription_id": subscription_id,
                                "plan": plan,
                                "period_end": period_end,
                                "updated_at": now,
                            }
                        ),
                    }
                }
            )

        return self._run_billing_transaction(
            user_id=user_id,
            event_id=event_id,
            operation="subscription",
            entitlement_update=entitlement_update,
            event_detail={"plan": plan, "credits": credits},
            extra_items=extra,
        )

    def _run_billing_transaction(
        self,
        *,
        user_id: str,
        event_id: str,
        operation: str,
        entitlement_update: TransactWriteItemTypeDef,
        event_detail: dict[str, Any],
        extra_items: list[TransactWriteItemTypeDef] | None = None,
    ) -> BillingOperationResult:
        """Apply an entitlement change exactly once per Stripe event."""
        event_put: TransactWriteItemTypeDef = {
            "Put": {
                "TableName": self.table_name,
                "Item": _marshal(
                    {
                        "PK": user_pk(user_id),
                        "SK": event_sk(event_id),
                        "entity_type": "STRIPE_EVENT",
                        "event_id": event_id,
                        "operation": operation,
                        "processed_at": _now_iso(),
                        # Reaped by DynamoDB's TTL; see EVENT_TTL_DAYS.
                        "expires_at": _ttl_epoch(EVENT_TTL_DAYS),
                        **event_detail,
                    }
                ),
                "ConditionExpression": "attribute_not_exists(PK)",
            }
        }

        # Order must match _ENTITLEMENT_ITEM / _EVENT_ITEM / _SUBSCRIPTION_ITEM.
        items = [entitlement_update, event_put, *(extra_items or [])]

        try:
            self.client.transact_write_items(TransactItems=items)
        except self.client.exceptions.TransactionCanceledException as exc:
            reasons = _cancellation_reasons(exc)
            if _condition_failed(reasons, _EVENT_ITEM):
                # The only condition in this transaction, so a cancellation
                # here means exactly one thing: Stripe redelivered an event
                # already applied. Constraint 5's idempotency, and a silent
                # success so Stripe stops retrying.
                logger.info("stripe %s already applied event_id=%s", operation, event_id)
                return BillingOperationResult(
                    applied=False,
                    entitlement=self._require_entitlement(user_id),
                )
            raise CreditLedgerError(f"stripe {operation} failed for event {event_id}") from exc

        # No PII and no event payload: ids and the outcome only (constraint 7).
        logger.info("stripe %s applied event_id=%s", operation, event_id)
        return BillingOperationResult(
            applied=True,
            entitlement=self._require_entitlement(user_id),
        )

    def _run_credit_transaction(
        self,
        *,
        user_id: str,
        job_id: str,
        operation: str,
        entitlement_update: TransactWriteItemTypeDef,
        job_update: TransactWriteItemTypeDef,
        replay_statuses: set[JobStatus],
        on_entitlement_failure: CreditLedgerError,
        replay_when_job_missing: bool = False,
    ) -> CreditOperationResult:
        """Run the two-item transaction and classify any cancellation."""
        # Order must match _ENTITLEMENT_ITEM / _JOB_ITEM: CancellationReasons
        # comes back parallel to this list.
        items = [entitlement_update, job_update]

        try:
            self.client.transact_write_items(TransactItems=items)
        except self.client.exceptions.TransactionCanceledException as exc:
            return self._handle_cancellation(
                exc=exc,
                user_id=user_id,
                job_id=job_id,
                operation=operation,
                replay_statuses=replay_statuses,
                replay_when_job_missing=replay_when_job_missing,
                on_entitlement_failure=on_entitlement_failure,
            )

        # No PII: job_id and status only (constraint 7).
        logger.info("credit %s applied job_id=%s", operation, job_id)
        job = self.get_job(user_id, job_id)
        entitlement = self._require_entitlement(user_id)
        return CreditOperationResult(
            applied=True,
            job_status=job.status if job else JobStatus.PENDING,
            entitlement=entitlement,
        )

    def _handle_cancellation(
        self,
        *,
        exc: Exception,
        user_id: str,
        job_id: str,
        operation: str,
        replay_statuses: set[JobStatus],
        replay_when_job_missing: bool,
        on_entitlement_failure: CreditLedgerError,
    ) -> CreditOperationResult:
        reasons = _cancellation_reasons(exc)
        job_failed = _condition_failed(reasons, _JOB_ITEM)
        entitlement_failed = _condition_failed(reasons, _ENTITLEMENT_ITEM)

        # Order matters. When both conditions fail the reasons are
        # [ConditionalCheckFailed, ConditionalCheckFailed]; checking the
        # entitlement first would report a genuine replay by a user who has
        # since spent down to zero credits as InsufficientCreditsError.
        if job_failed:
            job = self.get_job(user_id, job_id)
            if job is None and replay_when_job_missing:
                logger.info("credit %s no-op (no job item) job_id=%s", operation, job_id)
                return CreditOperationResult(
                    applied=False,
                    job_status=JobStatus.PENDING,
                    entitlement=self._require_entitlement(user_id),
                )
            if job is not None and job.status in replay_statuses:
                logger.info(
                    "credit %s no-op (replay) job_id=%s status=%s",
                    operation,
                    job_id,
                    job.status.value,
                )
                return CreditOperationResult(
                    applied=False,
                    job_status=job.status,
                    entitlement=self._require_entitlement(user_id),
                )
            status = job.status.value if job else "MISSING"
            raise JobStateError(f"cannot {operation} job {job_id}: status is {status}") from exc

        if entitlement_failed:
            raise on_entitlement_failure from exc

        raise CreditLedgerError(f"credit {operation} failed for job {job_id}") from exc

    def _require_entitlement(self, user_id: str) -> Entitlement:
        entitlement = self.get_entitlement(user_id)
        if entitlement is None:
            return Entitlement(user_id=user_id)
        return entitlement

    # ------------------------------------------------------------------
    # Companion agent: sessions, turns, memory, quota
    # ------------------------------------------------------------------
    #
    # DynamoDB actions, for the harness's IAM grant (A5):
    #   GetItem     get_agent_session, get_memory, append_insight (its read)
    #   PutItem     create_agent_session, append_insight
    #   UpdateItem  reserve_agent_session, claim_turn, mark_agent_session
    #   Query       list_turns
    #   DeleteItem  clear_memory
    #   commit_turn is a TransactWriteItems of one Put and one Update.

    def reserve_agent_session(self, user_id: str, month: str, cap: int) -> bool:
        """Count a new session against the month; False when the cap is hit.

        A dedicated AGENTQUOTA item rather than a counter on ENTITLEMENT: the
        credit transactions address their items by position (module
        docstring) and must not grow a third concern, and a per-month key
        expires by itself. ``ADD`` creates the counter on first use, so the
        condition has to admit the absent attribute explicitly.
        """
        try:
            self.client.update_item(
                TableName=self.table_name,
                Key=_marshal({"PK": user_pk(user_id), "SK": agent_quota_sk(month)}),
                UpdateExpression=(
                    "ADD sessions :one "
                    "SET expires_at = if_not_exists(expires_at, :ttl), "
                    "entity_type = if_not_exists(entity_type, :type)"
                ),
                ConditionExpression="attribute_not_exists(sessions) OR sessions < :cap",
                ExpressionAttributeValues=_marshal(
                    {
                        ":one": 1,
                        ":cap": cap,
                        ":ttl": _ttl_epoch(AGENT_QUOTA_TTL_DAYS),
                        ":type": "AGENT_QUOTA",
                    }
                ),
            )
        except self.client.exceptions.ConditionalCheckFailedException:
            logger.info("agent session quota reached month=%s", month)
            return False
        return True

    def create_agent_session(
        self, user_id: str, session_id: str, *, engine: AgentEngineName, model_id: str
    ) -> bool:
        """Open a session at turn 0. False if the id was already used."""
        now = _now_iso()
        return self._put_if_absent(
            {
                "PK": user_pk(user_id),
                "SK": agent_session_sk(session_id),
                "entity_type": "AGENT_SESSION",
                "session_id": session_id,
                "status": AgentSessionStatus.ACTIVE.value,
                "turn": 0,
                "engine": engine,
                "model_id": model_id,
                "usage_input_tokens": 0,
                "usage_output_tokens": 0,
                "usage_cache_read_tokens": 0,
                "usage_cache_write_tokens": 0,
                "created_at": now,
                "updated_at": now,
                "expires_at": _ttl_epoch(AGENT_SESSION_TTL_DAYS),
            }
        )

    def get_agent_session(self, user_id: str, session_id: str) -> AgentSession | None:
        response = self.client.get_item(
            TableName=self.table_name,
            Key=_marshal({"PK": user_pk(user_id), "SK": agent_session_sk(session_id)}),
            ConsistentRead=True,
        )
        item = response.get("Item")
        if not item:
            return None
        return AgentSession.model_validate({**_plain(_unmarshal(item)), "user_id": user_id})

    def claim_turn(
        self, user_id: str, session_id: str, *, engine: AgentEngineName, now: datetime
    ) -> AgentSession:
        """Take the session for one turn; the session as it stands if won.

        The claim is the fencing token's first half (``commit_turn`` is the
        second): a live ``in_flight`` stamp keeps a second invocation --
        a double-submitted message, a client retry -- from running the same
        turn concurrently, and a stale one is taken over because the
        invocation that set it is past its own timeout and can only be dead.
        The engine must match so one session is never processed by both
        engines in alternation, which would make their metrics meaningless.

        Stamps are ISO-8601 UTC with microseconds, so string comparison is
        time comparison. Raises AgentTurnBusyError on any rejected condition;
        the harness does not need to know which.
        """
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware: it is compared with stored UTC stamps")
        now_utc = now.astimezone(UTC)
        stamp = now_utc.isoformat(timespec="microseconds")
        stale = (now_utc - timedelta(seconds=AGENT_IN_FLIGHT_TIMEOUT_SECONDS)).isoformat(
            timespec="microseconds"
        )
        try:
            response = self.client.update_item(
                TableName=self.table_name,
                Key=_marshal({"PK": user_pk(user_id), "SK": agent_session_sk(session_id)}),
                UpdateExpression="SET in_flight = :now, updated_at = :now",
                ConditionExpression=(
                    "attribute_exists(PK) AND #status = :active AND engine = :engine "
                    "AND (attribute_not_exists(in_flight) OR in_flight < :stale)"
                ),
                ExpressionAttributeNames=dict(_STATUS_NAMES),
                ExpressionAttributeValues=_marshal(
                    {
                        ":now": stamp,
                        ":active": AgentSessionStatus.ACTIVE.value,
                        ":engine": engine,
                        ":stale": stale,
                    }
                ),
                ReturnValues="ALL_NEW",
            )
        except self.client.exceptions.ConditionalCheckFailedException as exc:
            logger.info("agent turn claim rejected session_id=%s", session_id)
            raise AgentTurnBusyError(f"session {session_id} is busy or closed") from exc
        return AgentSession.model_validate(
            {**_plain(_unmarshal(response["Attributes"])), "user_id": user_id}
        )

    def commit_turn(
        self,
        user_id: str,
        session_id: str,
        *,
        expected_turn: int,
        checkpoint: AgentTurn,
        finalized_job_id: str | None = None,
    ) -> bool:
        """Persist a turn and advance the session, atomically. False on replay.

        One transaction: the T-item is put only if absent, and the header
        moves ``turn`` forward only from ``expected_turn`` and only while a
        claim is held. Either condition failing cancels both writes, so a
        late commit from an invocation whose claim was taken over changes
        nothing -- that is the fencing token's second half. Token counters
        are ``ADD``ed to the flat ``usage_*`` attributes.
        """
        if checkpoint.turn != expected_turn:
            raise ValueError(
                f"checkpoint is for turn {checkpoint.turn}, commit expected {expected_turn}"
            )
        now = _now_iso()
        turn_item = {
            key: value
            for key, value in _dynamo_json(checkpoint.model_dump(mode="json")).items()
            if value is not None
        }
        turn_item.update(
            {
                "PK": user_pk(user_id),
                "SK": agent_turn_sk(session_id, checkpoint.turn),
                "entity_type": "AGENT_TURN",
                "expires_at": _ttl_epoch(AGENT_SESSION_TTL_DAYS),
            }
        )

        update = "SET #turn = #turn + :one, updated_at = :now"
        # DynamoDB rejects a declared name that no expression uses, so the
        # status alias is added only on the finalizing path.
        names = {"#turn": "turn"}
        values: dict[str, Any] = {
            ":one": 1,
            ":now": now,
            ":expected": expected_turn,
            ":in": checkpoint.usage.input_tokens,
            ":out": checkpoint.usage.output_tokens,
            ":cr": checkpoint.usage.cache_read_tokens,
            ":cw": checkpoint.usage.cache_write_tokens,
        }
        if finalized_job_id is not None:
            update += ", #status = :finalized, job_id = :job"
            names.update(_STATUS_NAMES)
            values[":finalized"] = AgentSessionStatus.FINALIZED.value
            values[":job"] = finalized_job_id
        update += (
            " ADD usage_input_tokens :in, usage_output_tokens :out, "
            "usage_cache_read_tokens :cr, usage_cache_write_tokens :cw REMOVE in_flight"
        )

        items: list[TransactWriteItemTypeDef] = [
            {
                "Put": {
                    "TableName": self.table_name,
                    "Item": _marshal(turn_item),
                    "ConditionExpression": "attribute_not_exists(PK)",
                }
            },
            {
                "Update": {
                    "TableName": self.table_name,
                    "Key": _marshal({"PK": user_pk(user_id), "SK": agent_session_sk(session_id)}),
                    "UpdateExpression": update,
                    "ConditionExpression": (
                        "attribute_exists(PK) AND #turn = :expected AND attribute_exists(in_flight)"
                    ),
                    "ExpressionAttributeNames": names,
                    "ExpressionAttributeValues": _marshal(values),
                }
            },
        ]
        try:
            self.client.transact_write_items(TransactItems=items)
        except self.client.exceptions.TransactionCanceledException as exc:
            reasons = _cancellation_reasons(exc)
            logger.info(
                "agent turn commit skipped session_id=%s turn=%d turn_item_failed=%s "
                "header_failed=%s",
                session_id,
                expected_turn,
                _condition_failed(reasons, 0),
                _condition_failed(reasons, 1),
            )
            return False
        # Counts only (constraint 7).
        logger.info(
            "agent turn committed session_id=%s turn=%d tool_rounds=%d finalized=%s",
            session_id,
            expected_turn,
            len(checkpoint.tool_calls),
            finalized_job_id is not None,
        )
        return True

    def set_pending_brief(
        self, user_id: str, session_id: str, *, brief: str, duration_minutes: int
    ) -> bool:
        """Place the model's proposal on the session. False if it is closed.

        Overwrites an earlier proposal: the model changing its mind is the
        normal case. Only ever read back by the session's owner and by
        ``confirm_session``; the brief is user content and stays off the
        logs. (UpdateItem.)
        """
        return self._conditional_update(
            user_id,
            agent_session_sk(session_id),
            kind="agent_session",
            update=(
                "SET pending_brief = :brief, pending_duration_minutes = :minutes, updated_at = :now"
            ),
            condition="attribute_exists(PK) AND #status = :active",
            names=dict(_STATUS_NAMES),
            values={
                ":brief": brief,
                ":minutes": duration_minutes,
                ":active": AgentSessionStatus.ACTIVE.value,
                ":now": _now_iso(),
            },
        )

    def clear_pending_brief(self, user_id: str, session_id: str) -> bool:
        """Withdraw a proposal. Nothing pending is also success. (UpdateItem.)"""
        return self._conditional_update(
            user_id,
            agent_session_sk(session_id),
            kind="agent_session",
            update="REMOVE pending_brief, pending_duration_minutes SET updated_at = :now",
            condition="attribute_exists(PK)",
            values={":now": _now_iso()},
        )

    def confirm_session(
        self, user_id: str, session_id: str, *, expected_turn: int, job_id: str
    ) -> bool:
        """Close the session on the listener's confirmation: the fencing
        token's fourth verb. Same lock as ``commit_turn`` (the claim must be
        held, the turn counter must match) but the counter does not move --
        confirming is not a turn of conversation. Requires a pending brief:
        there is nothing to confirm otherwise. (UpdateItem.)
        """
        return self._conditional_update(
            user_id,
            agent_session_sk(session_id),
            kind="agent_session",
            update=(
                "SET #status = :finalized, job_id = :job, updated_at = :now "
                "REMOVE in_flight, pending_brief, pending_duration_minutes"
            ),
            condition=(
                "attribute_exists(PK) AND #status = :active AND #turn = :expected "
                "AND attribute_exists(in_flight) AND attribute_exists(pending_brief)"
            ),
            names={**_STATUS_NAMES, "#turn": "turn"},
            values={
                ":finalized": AgentSessionStatus.FINALIZED.value,
                ":active": AgentSessionStatus.ACTIVE.value,
                ":job": job_id,
                ":expected": expected_turn,
                ":now": _now_iso(),
            },
        )

    def release_turn(self, user_id: str, session_id: str, *, expected_turn: int) -> bool:
        """Give the claim back without advancing: the fencing token's third
        verb, for a turn that failed before it could commit.

        Conditioned like ``commit_turn`` -- same ``turn``, a claim present --
        so a release from an invocation whose claim was already taken over
        (or whose turn was committed by a retry) changes nothing. Without
        this, a failed turn would block the session until the claim went
        stale; with it, the user can resend immediately.
        """
        return self._conditional_update(
            user_id,
            agent_session_sk(session_id),
            kind="agent_session",
            update="REMOVE in_flight SET updated_at = :now",
            condition="attribute_exists(PK) AND #turn = :expected AND attribute_exists(in_flight)",
            names={"#turn": "turn"},
            values={":expected": expected_turn, ":now": _now_iso()},
        )

    def list_turns(self, user_id: str, session_id: str) -> list[AgentTurn]:
        """Every checkpoint of a session, in turn order.

        Key-prefix query, paginated to completion: a session is at most
        MAX_TURNS items, but a page limit is a client-side knob and the
        loop must not depend on the default being large enough.
        """
        turns: list[AgentTurn] = []
        kwargs: dict[str, Any] = {
            "TableName": self.table_name,
            "KeyConditionExpression": "PK = :pk AND begins_with(SK, :prefix)",
            "ExpressionAttributeValues": _marshal(
                {":pk": user_pk(user_id), ":prefix": agent_turns_prefix(session_id)}
            ),
            "ConsistentRead": True,
        }
        while True:
            response = self.client.query(**kwargs)
            turns.extend(
                AgentTurn.model_validate(_plain(_unmarshal(item)))
                for item in response.get("Items", [])
            )
            last = response.get("LastEvaluatedKey")
            if not last:
                break
            kwargs["ExclusiveStartKey"] = last
        turns.sort(key=lambda t: t.turn)
        return turns

    def mark_agent_session(self, user_id: str, session_id: str, status: AgentSessionStatus) -> bool:
        """ACTIVE -> ABANDONED | FAILED, idempotently; releases any claim.

        FINALIZED is not reachable here: only ``commit_turn`` writes it, in
        the same transaction as the turn that earned it.
        """
        if status not in (AgentSessionStatus.ABANDONED, AgentSessionStatus.FAILED):
            raise ValueError(f"mark_agent_session cannot set {status}")
        return self._conditional_update(
            user_id,
            agent_session_sk(session_id),
            kind="agent_session",
            update="SET #status = :target, updated_at = :now REMOVE in_flight",
            condition="attribute_exists(PK) AND #status IN (:active, :target)",
            names=dict(_STATUS_NAMES),
            values={
                ":target": status.value,
                ":active": AgentSessionStatus.ACTIVE.value,
                ":now": _now_iso(),
            },
        )

    def get_memory(self, user_id: str) -> Memory:
        response = self.client.get_item(
            TableName=self.table_name,
            Key=_marshal({"PK": user_pk(user_id), "SK": MEMORY_SK}),
            ConsistentRead=True,
        )
        item = response.get("Item")
        if not item:
            return Memory()
        return Memory.model_validate(_plain(_unmarshal(item)))

    def append_insight(self, user_id: str, text: str, session_id: str, now: datetime) -> bool:
        """Remember one thing about the user. False if already remembered.

        Read-modify-write under an optimistic lock on ``updated_at``: two
        parallel tool calls in one turn may both append, and the loser
        re-reads rather than overwriting. Duplicates compare case-folded and
        whitespace-normalised, and the list is capped FIFO so the memory
        block the prompt carries stays bounded.
        """
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        cleaned = " ".join(text.split())
        if not cleaned:
            return False
        key = {"PK": user_pk(user_id), "SK": MEMORY_SK}
        for _ in range(_MEMORY_WRITE_ATTEMPTS):
            response = self.client.get_item(
                TableName=self.table_name, Key=_marshal(key), ConsistentRead=True
            )
            item = response.get("Item")
            insights = Memory.model_validate(_plain(_unmarshal(item))).insights if item else []
            if any(i.text.casefold() == cleaned.casefold() for i in insights):
                return False
            insights.append(Insight(text=cleaned, created_at=now, session_id=session_id))
            insights = insights[-AGENT_INSIGHTS_MAX:]

            new_item = {
                **key,
                "entity_type": "MEMORY",
                "insights": [i.model_dump(mode="json") for i in insights],
                "updated_at": now.astimezone(UTC).isoformat(timespec="microseconds"),
            }
            if item is None:
                condition = "attribute_not_exists(PK)"
                values: dict[str, Any] = {}
            else:
                # The stored string itself, not a re-rendered datetime: the
                # lock is an exact match.
                condition = "updated_at = :seen"
                values = {":seen": item["updated_at"]["S"]}
            try:
                self.client.put_item(
                    TableName=self.table_name,
                    Item=_marshal(new_item),
                    ConditionExpression=condition,
                    **({"ExpressionAttributeValues": _marshal(values)} if values else {}),
                )
            except self.client.exceptions.ConditionalCheckFailedException:
                continue
            logger.info("insight saved session_id=%s count=%d", session_id, len(insights))
            return True
        raise MemoryContentionError("memory item kept changing underneath the write")

    def clear_memory(self, user_id: str) -> None:
        """Forget everything. Deleting an absent item succeeds, so this is
        idempotent by construction."""
        self.client.delete_item(
            TableName=self.table_name,
            Key=_marshal({"PK": user_pk(user_id), "SK": MEMORY_SK}),
        )
        logger.info("memory cleared")


def _cancellation_reasons(exc: Exception) -> list[dict[str, Any]]:
    response = getattr(exc, "response", None) or {}
    return response.get("CancellationReasons") or []


def _condition_failed(reasons: list[dict[str, Any]], index: int) -> bool:
    if index >= len(reasons):
        return False
    return reasons[index].get("Code") == _CONDITIONAL_CHECK_FAILED
