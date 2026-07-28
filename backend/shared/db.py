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
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

from shared.models import (
    DEFAULT_PLAN,
    ENTITLEMENT_SK,
    FREE_SIGNUP_CREDITS,
    PROFILE_SK,
    CreditOperationResult,
    Entitlement,
    Job,
    JobStatus,
    job_sk,
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

_CONDITIONAL_CHECK_FAILED = "ConditionalCheckFailed"

# "status" is a DynamoDB reserved word.
_STATUS_NAMES = {"#status": "status"}


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
            replay_statuses={JobStatus.DONE},
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
            replay_statuses={JobStatus.PENDING, JobStatus.DONE, JobStatus.ROLLED_BACK},
            replay_when_job_missing=True,
            on_entitlement_failure=CreditLedgerError(
                f"no frozen credit to roll back for job {job_id}"
            ),
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

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
