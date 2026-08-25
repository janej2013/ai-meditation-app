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

import logging
import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

from shared.models import (
    DEFAULT_PLAN,
    ENTITLEMENT_SK,
    FREE_SIGNUP_CREDITS,
    PICTURE_ITEM_TTL_DAYS,
    PROFILE_SK,
    BillingOperationResult,
    CreditOperationResult,
    Entitlement,
    Job,
    JobStatus,
    Picture,
    PictureDescription,
    PictureStatus,
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


def _marshal(data: dict[str, Any]) -> dict[str, Any]:
    """Marshal a plain dict (item key or expression values) into AttributeValues."""
    return {key: _serializer.serialize(value) for key, value in data.items()}


def _unmarshal(item: dict[str, Any]) -> dict[str, Any]:
    """Unmarshal a DynamoDB item into a plain dict."""
    return {key: _deserializer.deserialize(value) for key, value in item.items()}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


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

    def set_picture_description(
        self, user_id: str, picture_id: str, description: PictureDescription
    ) -> None:
        """Record what the vision model saw. Idempotent: a retry rewrites the
        same reading; a picture that has moved on is left alone."""
        self._update_picture(
            user_id,
            picture_id,
            update="SET #status = :described, keywords = :kw, summary = :summary",
            condition="attribute_exists(PK) AND #status IN (:pending, :described)",
            values={
                ":described": PictureStatus.DESCRIBED.value,
                ":pending": PictureStatus.PENDING.value,
                ":kw": description.keywords,
                ":summary": description.summary,
            },
        )

    def mark_picture_failed(self, user_id: str, picture_id: str) -> None:
        """The vision step gave up: the keywords screen stops waiting."""
        self._update_picture(
            user_id,
            picture_id,
            update="SET #status = :failed",
            condition="attribute_exists(PK) AND #status = :pending",
            values={
                ":failed": PictureStatus.FAILED.value,
                ":pending": PictureStatus.PENDING.value,
            },
        )

    def _update_picture(
        self, user_id: str, picture_id: str, *, update: str, condition: str, values: dict[str, Any]
    ) -> None:
        try:
            self.client.update_item(
                TableName=self.table_name,
                Key=_marshal({"PK": user_pk(user_id), "SK": picture_sk(picture_id)}),
                UpdateExpression=update,
                ConditionExpression=condition,
                ExpressionAttributeNames=dict(_STATUS_NAMES),
                ExpressionAttributeValues=_marshal(values),
            )
        except self.client.exceptions.ConditionalCheckFailedException:
            logger.info("picture update skipped (replay or missing) picture_id=%s", picture_id)

    def set_job_picture_description(
        self, user_id: str, job_id: str, description: PictureDescription
    ) -> None:
        """Record what the vision model saw, for generate_script to read."""
        self._update_job(
            user_id,
            job_id,
            update="SET picture_keywords = :kw, picture_summary = :summary, updated_at = :now",
            condition="attribute_exists(PK)",
            values={
                ":kw": description.keywords,
                ":summary": description.summary,
                ":now": _now_iso(),
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
            "ExpressionAttributeNames": dict(_STATUS_NAMES),
            "ExpressionAttributeValues": _marshal(
                {":pk": user_pk(user_id), ":job": "JOB#", ":done": JobStatus.DONE.value}
            ),
            "ProjectionExpression": (
                "job_id, #status, picture_keywords, mood_text, "
                "duration_minutes, picture_key, created_at"
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
        """Conditional update on a JOB item; a failed condition is a no-op.

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
            "Key": _marshal({"PK": user_pk(user_id), "SK": job_sk(job_id)}),
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
            item = (getattr(exc, "response", None) or {}).get("Item")
            if not item:
                logger.warning("job update skipped: no job item job_id=%s", job_id)
                return False
            # Read the one attribute directly rather than unmarshalling the
            # whole item, which holds mood_text (constraint 7).
            status = item.get("status", {}).get("S", "UNKNOWN")
            logger.info("job update skipped (replay) job_id=%s status=%s", job_id, status)
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


def _cancellation_reasons(exc: Exception) -> list[dict[str, Any]]:
    response = getattr(exc, "response", None) or {}
    return response.get("CancellationReasons") or []


def _condition_failed(reasons: list[dict[str, Any]], index: int) -> bool:
    if index >= len(reasons):
        return False
    return reasons[index].get("Code") == _CONDITIONAL_CHECK_FAILED
