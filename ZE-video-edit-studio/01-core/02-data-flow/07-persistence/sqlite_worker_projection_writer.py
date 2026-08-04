"""Fixed-SQL OUTBOX worker effect/result/proof transition writer.

This is deliberately a narrow production vertical.  It accepts only issuer-
sealed OUTBOX ``AUTHORIZE_EFFECT``, ``COMPLETE`` and ``RECONCILE`` plans and
their independently issuer-sealed V9 payload projection.  One
``BEGIN IMMEDIATE`` writes the generic/specialised receipt and worker ledger,
the frozen V8 mirrors, the V9 payload sidecars, the legacy/current mirrors and
finally advances the worker ledger head.  No test helper is imported here and
``EXPIRE`` never manufactures a result or proof.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import hmac
import sqlite3
from typing import Any, Callable, Optional

from sqlite_business_runtime import (
    BusinessRuntimeAccess,
    _validate_business_runtime_connection,
)
from sqlite_business_transaction import _rollback_close, _row_is
from sqlite_kernel import SQLiteKernelError, SqliteDatabase, _allow_authorizer
from sqlite_production_authority import (
    SQLiteProductionAuthorityError,
    _binding_from_snapshot,
    _mounted_row,
)
from sqlite_worker_claim_writer import (
    SQLiteWorkerClaimWriter,
    _ClaimCorrupt,
    _DispatchHeadSnapshot,
    _WorkerHeadSnapshot,
    _close_quietly,
)
from sqlite_worker_durable_audit import (
    SQLiteWorkerDurableAuditStatus,
    WorkerDurableIdentity,
    _AuditBudget,
    _AuditConflict,
    _AuditCorrupt,
    _AuditUnsupported,
    _audit_snapshot,
)
from worker_execution_contract import (
    WorkerAuthorizeEffectRequest,
    WorkerCompleteRequest,
    WorkerExecutionOperation,
    WorkerExecutionStatus,
    WorkerReconcileRequest,
    WorkerTargetKind,
)
from worker_execution_integrity import (
    WorkerExecutionPlan,
    _require_worker_execution_plan,
)
from worker_result_projection_contract import (
    WORKER_EFFECT_REQUEST_PROJECTION_SCHEMA,
    WORKER_PROOF_PAYLOAD_PROJECTION_SCHEMA,
    WORKER_RESULT_PAYLOAD_PROJECTION_SCHEMA,
    WORKER_RESULT_PROJECTION_PROTOCOL,
)
from worker_result_projection_integrity import (
    WorkerResultProjectionPlan,
    _require_worker_result_projection_plan,
    _v8_worker_result_mirror_proof_sha256,
)


class SQLiteWorkerProjectionStatus(str, Enum):
    COMMITTED = "committed"
    NOT_COMMITTED = "not_committed"
    CONFLICT = "conflict"
    CORRUPT = "corrupt"
    UNKNOWN = "unknown"


class SQLiteWorkerProjectionTestPoint(str, Enum):
    BEFORE_COMMIT = "before_commit"
    AFTER_COMMIT_BEFORE_ACK = "after_commit_before_ack"


@dataclass(frozen=True)
class SQLiteWorkerProjectionResult:
    status: SQLiteWorkerProjectionStatus
    request_id: str
    code: str
    recovered_after_ack_loss: bool = False

    def __post_init__(self) -> None:
        if type(self.status) is not SQLiteWorkerProjectionStatus:
            raise TypeError("status must be an exact SQLiteWorkerProjectionStatus")
        if (
            type(self.request_id) is not str
            or len(self.request_id) != 32
            or any(value not in "0123456789abcdef" for value in self.request_id)
        ):
            raise TypeError("request_id must be 32 lowercase hexadecimal characters")
        if type(self.code) is not str or not self.code:
            raise TypeError("code must be a non-empty exact string")
        if type(self.recovered_after_ack_loss) is not bool or (
            self.recovered_after_ack_loss
            and self.status is not SQLiteWorkerProjectionStatus.COMMITTED
        ):
            raise TypeError("recovered_after_ack_loss is invalid")


class SQLiteWorkerProjectionError(ValueError):
    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        if type(code) is not str or not code:
            raise TypeError("code must be a non-empty exact string")
        self.code = code
        super().__init__(code)


class _ProjectionConflict(RuntimeError):
    pass


class _ProjectionCorrupt(RuntimeError):
    pass


class _ProjectionUnavailable(RuntimeError):
    pass


def _same(left: Any, right: Any) -> bool:
    if type(left) is bytes and type(right) is bytes:
        return hmac.compare_digest(left, right)
    return type(left) is type(right) and left == right


_INSERT_TABLES = frozenset((
    "transaction_receipts",
    "worker_ledger_entries",
    "worker_execution_receipts",
    "worker_effects",
    "worker_results",
    "worker_reconcile_proofs",
    "worker_effect_request_projections",
    "worker_result_payload_projections",
    "worker_proof_payload_projections",
))
_UPDATE_TABLES = frozenset((
    "event_outbox",
    "worker_target_current",
    "outbox_dispatch_head",
    "worker_ledger_head",
))
_UPDATE_COLUMNS = {
    "event_outbox": frozenset((
        "status",
        "state_revision",
        "attempt_count",
        "available_at",
        "lease_owner",
        "lease_fence",
        "lease_expires_at",
        "published_at",
        "outcome_unknown_at",
        "dead_at",
        "last_error_bytes",
        "last_error_sha256",
        "updated_at",
    )),
    "worker_target_current": frozenset((
        "status",
        "state_revision",
        "attempt_count",
        "last_attempt_id",
        "last_attempt_number",
        "current_attempt_id",
        "current_attempt_number",
        "fence_high_water",
        "cancellation_pending",
        "next_attempt_at",
        "lease_owner",
        "lease_fence",
        "lease_expires_at",
        "current_effect_id",
        "unresolved_effect_id",
        "last_receipt_transaction_id",
        "previous_state_sha256",
        "state_sha256",
        "updated_at",
    )),
    "outbox_dispatch_head": frozenset((
        "state_revision",
        "published_sequence",
        "published_repository_revision",
        "published_ordinal",
        "inflight_sequence",
        "inflight_repository_revision",
        "inflight_ordinal",
        "previous_head_sha256",
        "head_sha256",
        "updated_at",
    )),
    "worker_ledger_head": frozenset((
        "state_revision",
        "sequence",
        "previous_tail_sha256",
        "tail_sha256",
        "updated_at",
    )),
}


class _WorkerProjectionAuthorizer:
    """Deny every mutation outside this writer's fixed statement surface."""

    __slots__ = ("denied",)

    def __init__(self) -> None:
        self.denied = False

    def __call__(
        self,
        action: int,
        arg1: Optional[str],
        arg2: Optional[str],
        database_name: Optional[str],
        trigger_name: Optional[str],
    ) -> int:
        del database_name, trigger_name
        forbidden = {
            getattr(sqlite3, name)
            for name in (
                "SQLITE_ALTER_TABLE",
                "SQLITE_ANALYZE",
                "SQLITE_ATTACH",
                "SQLITE_CREATE_INDEX",
                "SQLITE_CREATE_TABLE",
                "SQLITE_CREATE_TEMP_INDEX",
                "SQLITE_CREATE_TEMP_TABLE",
                "SQLITE_CREATE_TEMP_TRIGGER",
                "SQLITE_CREATE_TEMP_VIEW",
                "SQLITE_CREATE_TRIGGER",
                "SQLITE_CREATE_VIEW",
                "SQLITE_DETACH",
                "SQLITE_DROP_INDEX",
                "SQLITE_DROP_TABLE",
                "SQLITE_DROP_TEMP_INDEX",
                "SQLITE_DROP_TEMP_TABLE",
                "SQLITE_DROP_TEMP_TRIGGER",
                "SQLITE_DROP_TEMP_VIEW",
                "SQLITE_DROP_TRIGGER",
                "SQLITE_DROP_VIEW",
                "SQLITE_REINDEX",
            )
            if hasattr(sqlite3, name)
        }
        table = (arg1 or "").lower()
        column = (arg2 or "").lower()
        insert = getattr(sqlite3, "SQLITE_INSERT", -1)
        update = getattr(sqlite3, "SQLITE_UPDATE", -1)
        delete = getattr(sqlite3, "SQLITE_DELETE", -1)
        denied = bool(
            action in forbidden
            or action == getattr(sqlite3, "SQLITE_PRAGMA", -1)
            or (action == insert and table not in _INSERT_TABLES)
            or (
                action == update
                and (
                    table not in _UPDATE_TABLES
                    or column not in _UPDATE_COLUMNS[table]
                )
            )
            or action == delete
        )
        if denied:
            self.denied = True
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK


def _payload_columns(value: Any) -> tuple[Any, Any, Any, Any, Any]:
    if value is None:
        return (None, None, None, None, None)
    return (
        value.schema,
        value.codec_id,
        value.codec_version,
        value.payload_bytes,
        value.payload_sha256,
    )


class SQLiteWorkerProjectionWriter:
    """Commit and freshly reconcile the three projected OUTBOX operations."""

    __slots__ = ("_database",)

    def __init__(self, database: SqliteDatabase) -> None:
        if type(database) is not SqliteDatabase:
            raise TypeError("database must be an exact SqliteDatabase")
        self._database = database

    @staticmethod
    def _result(
        plan: WorkerExecutionPlan,
        status: SQLiteWorkerProjectionStatus,
        code: str,
        *,
        recovered: bool = False,
    ) -> SQLiteWorkerProjectionResult:
        return SQLiteWorkerProjectionResult(
            status,
            plan.closure.request_transaction_id,
            code,
            recovered,
        )

    @staticmethod
    def _durable_identity(
        plan: WorkerExecutionPlan,
        projection: WorkerResultProjectionPlan,
    ) -> WorkerDurableIdentity:
        return WorkerDurableIdentity(
            plan.closure.request_transaction_id,
            plan.operation.value,
            plan.target.target_kind.value,
            plan.target.target_id,
            plan.closure.request_sha256,
            plan.closure.after_state_sha256,
            plan.expected_receipt.receipt_sha256,
            projection.closure.manifest_sha256,
        )

    @classmethod
    def _kernel_failure_result(
        cls,
        plan: WorkerExecutionPlan,
        failure: SQLiteKernelError,
    ) -> SQLiteWorkerProjectionResult:
        unavailable = failure.code in (
            "sqlite_business_runtime_unreadable",
            "sqlite_busy",
            "sqlite_locked",
            "sqlite_io_error",
        )
        return cls._result(
            plan,
            (
                SQLiteWorkerProjectionStatus.UNKNOWN
                if unavailable
                else SQLiteWorkerProjectionStatus.CORRUPT
            ),
            (
                "worker_projection_runtime_unavailable"
                if unavailable
                else "worker_projection_runtime_corrupt"
            ),
        )

    @staticmethod
    def _require_vertical(
        plan: WorkerExecutionPlan,
        projection: WorkerResultProjectionPlan,
    ) -> None:
        operation = plan.operation
        request = plan.request
        if (
            plan.target.target_kind is not WorkerTargetKind.OUTBOX
            or projection.worker_plan != plan
            or projection.binding.execution_receipt != plan.expected_receipt
            or projection.binding.target_id != plan.target.target_id
            or operation not in (
                WorkerExecutionOperation.AUTHORIZE_EFFECT,
                WorkerExecutionOperation.COMPLETE,
                WorkerExecutionOperation.RECONCILE,
            )
            or (
                operation is WorkerExecutionOperation.AUTHORIZE_EFFECT
                and (
                    type(request) is not WorkerAuthorizeEffectRequest
                    or projection.closure.effect_request is None
                    or projection.closure.result is not None
                    or projection.closure.proof is not None
                )
            )
            or (
                operation is WorkerExecutionOperation.COMPLETE
                and (
                    type(request) is not WorkerCompleteRequest
                    or projection.closure.effect_request is not None
                    or projection.closure.result is None
                )
            )
            or (
                operation is WorkerExecutionOperation.RECONCILE
                and (
                    type(request) is not WorkerReconcileRequest
                    or projection.closure.effect_request is not None
                    or projection.closure.result is None
                    or projection.closure.proof is None
                )
            )
            or plan.after_ledger.sequence != plan.before_ledger.sequence + 1
            or plan.expected_receipt.sequence != plan.after_ledger.sequence
            or not _same(
                plan.expected_receipt.previous_ledger_tail_sha256,
                plan.before_ledger.tail_sha256,
            )
            or not _same(
                plan.expected_receipt.ledger_tail_sha256,
                plan.after_ledger.tail_sha256,
            )
        ):
            raise SQLiteWorkerProjectionError(
                "worker_projection_vertical_unsupported"
            )

    @staticmethod
    def _read_current(connection: Any, target_id: str) -> Any:
        row = connection.execute(
            """
            SELECT target_kind,status,state_revision,attempt_count,last_attempt_id,
                   last_attempt_number,current_attempt_id,current_attempt_number,
                   fence_high_water,cancellation_pending,next_attempt_at,lease_owner,
                   lease_fence,lease_expires_at,current_effect_id,
                   unresolved_effect_id,last_receipt_transaction_id,
                   previous_state_sha256,state_sha256,updated_at
            FROM worker_target_current WHERE target_id=?
            """,
            (target_id,),
        ).fetchone()
        if row is None or len(row) != 20:
            raise _ProjectionCorrupt("worker_projection_current_missing")
        return row

    @staticmethod
    def _read_event(connection: Any, target_id: str) -> Any:
        row = connection.execute(
            """
            SELECT status,state_revision,attempt_count,available_at,lease_owner,
                   lease_fence,lease_expires_at,published_at,outcome_unknown_at,
                   dead_at,last_error_bytes,last_error_sha256,created_at,updated_at
            FROM event_outbox WHERE event_id=?
            """,
            (target_id,),
        ).fetchone()
        if row is None or len(row) != 14:
            raise _ProjectionCorrupt("worker_projection_event_missing")
        return row

    @staticmethod
    def _binding_exact(connection: Any, target_id: str, binding: Any) -> bool:
        if binding is None:
            return True
        row = connection.execute(
            """
            SELECT effect.target_id,effect.attempt_id,effect.effect_kind,
                   effect.idempotency_key,effect.authorized_at,
                   sidecar.attempt_number,sidecar.idempotency_scope,
                   sidecar.request_payload_bytes,sidecar.request_payload_sha256,
                   sidecar.authorized_at
            FROM worker_effects AS effect
            JOIN worker_effect_request_projections AS sidecar
              ON sidecar.effect_id=effect.effect_id AND
                 sidecar.target_id=effect.target_id AND
                 sidecar.attempt_id=effect.attempt_id AND
                 sidecar.authorization_transaction_id=
                    effect.authorization_transaction_id
            WHERE effect.effect_id=?
            """,
            (binding.effect_id,),
        ).fetchone()
        if row is None or len(row) != 10:
            return False
        return _row_is(row[:7] + row[8:10], (
            target_id,
            binding.attempt_id,
            binding.effect_kind.value,
            binding.idempotency_key,
            row[9],
            binding.attempt_number,
            binding.idempotency_scope,
            binding.request_sha256,
            row[4],
        )) and _same(hashlib.sha256(row[7]).digest(), row[8])

    @classmethod
    def _before_current_exact(
        cls,
        connection: Any,
        plan: WorkerExecutionPlan,
    ) -> tuple[Any, Any]:
        before = plan.before
        current = cls._read_current(connection, plan.target.target_id)
        expected = (
            "outbox",
            before.status.value,
            before.state_revision,
            before.attempt_count,
            before.last_attempt_id,
            before.last_attempt_number,
            before.current_attempt_id,
            before.current_attempt_number,
            before.fence_high_water,
            int(before.cancellation_pending),
            before.next_attempt_at,
            before.lease_owner,
            before.lease_fence,
            before.lease_expires_at,
            None if before.current_effect is None else before.current_effect.effect_id,
            (
                None
                if before.unresolved_effect is None
                else before.unresolved_effect.effect_id
            ),
        )
        if not _row_is(current[:16], expected):
            raise _ProjectionConflict("worker_projection_current_conflict")
        if not cls._binding_exact(
            connection,
            plan.target.target_id,
            before.current_effect,
        ) or not cls._binding_exact(
            connection,
            plan.target.target_id,
            before.unresolved_effect,
        ):
            raise _ProjectionCorrupt("worker_projection_effect_binding_corrupt")
        predecessor = connection.execute(
            "SELECT after_state_sha256,committed_at FROM worker_execution_receipts "
            "WHERE transaction_id=?",
            (current[16],),
        ).fetchone()
        if predecessor is None or not _row_is(
            predecessor,
            (current[18], current[19]),
        ):
            raise _ProjectionCorrupt("worker_projection_current_predecessor_corrupt")
        event = cls._read_event(connection, plan.target.target_id)
        if not _row_is(event[:3], (
            before.status.value,
            before.state_revision,
            before.attempt_count,
        )) or event[13] != current[19]:
            raise _ProjectionConflict("worker_projection_event_conflict")
        if not _row_is(event[4:7], (
            before.lease_owner,
            before.lease_fence,
            before.lease_expires_at,
        )):
            raise _ProjectionConflict("worker_projection_event_lease_conflict")
        return current, event

    @staticmethod
    def _require_database_time(connection: Any, plan: WorkerExecutionPlan) -> str:
        row = connection.execute(
            "SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')"
        ).fetchone()
        if row is None or len(row) != 1 or type(row[0]) is not str:
            raise _ProjectionCorrupt("worker_projection_database_clock_corrupt")
        now = row[0]
        if plan.request.header.occurred_at > now:
            raise _ProjectionConflict("worker_projection_time_future")
        if plan.operation in (
            WorkerExecutionOperation.AUTHORIZE_EFFECT,
            WorkerExecutionOperation.COMPLETE,
        ) and plan.request.lease.lease_expires_at <= now:
            raise _ProjectionConflict("worker_projection_lease_expired")
        return now

    @classmethod
    def _require_fresh_before(
        cls,
        connection: Any,
        profile: Any,
        plan: WorkerExecutionPlan,
    ) -> tuple[_WorkerHeadSnapshot, _DispatchHeadSnapshot, Any, Any, Any]:
        try:
            mounted = _binding_from_snapshot(
                connection,
                profile,
                _mounted_row(connection),
            )
        except SQLiteProductionAuthorityError as exc:
            raise _ProjectionCorrupt("worker_projection_mount_corrupt") from exc
        if not SQLiteWorkerClaimWriter._authority_exact(profile, mounted, plan):
            raise _ProjectionConflict("worker_projection_authority_conflict")
        cls._require_database_time(connection, plan)
        try:
            created_at = SQLiteWorkerClaimWriter._target_and_genesis_exact(
                connection,
                plan,
                expect_pending_event=False,
            )
        except _ClaimCorrupt as exc:
            raise _ProjectionCorrupt("worker_projection_origin_corrupt") from exc
        if not SQLiteWorkerClaimWriter._allocation_chain_exact(connection):
            raise _ProjectionCorrupt("worker_projection_allocation_corrupt")
        head = SQLiteWorkerClaimWriter._read_worker_head(connection)
        exact_head, _entries = SQLiteWorkerClaimWriter._ledger_chain(connection)
        if exact_head is None:
            raise _ProjectionCorrupt("worker_projection_ledger_corrupt")
        if (
            head.sequence != plan.before_ledger.sequence
            or not _same(head.tail_sha256, plan.before_ledger.tail_sha256)
        ):
            raise _ProjectionConflict("worker_projection_ledger_head_conflict")
        try:
            prefix = _audit_snapshot(
                connection,
                profile,
                mounted,
                plan.closure.request_transaction_id,
                None,
            )
        except _AuditCorrupt as exc:
            raise _ProjectionCorrupt(
                "worker_projection_prefix_audit_corrupt"
            ) from exc
        except _AuditConflict as exc:
            raise _ProjectionConflict(
                "worker_projection_prefix_audit_conflict"
            ) from exc
        except (_AuditBudget, _AuditUnsupported) as exc:
            raise _ProjectionUnavailable(
                "worker_projection_prefix_audit_unavailable"
            ) from exc
        if prefix.result.status is SQLiteWorkerDurableAuditStatus.UNKNOWN:
            raise _ProjectionUnavailable("worker_projection_prefix_audit_unavailable")
        if prefix.result.status is SQLiteWorkerDurableAuditStatus.CONFLICT:
            raise _ProjectionConflict("worker_projection_prefix_audit_conflict")
        if (
            prefix.result.status is not SQLiteWorkerDurableAuditStatus.ABSENT
            or prefix.result.ledger_sequence != plan.before_ledger.sequence
            or not _same(
                prefix.result.ledger_tail_sha256,
                plan.before_ledger.tail_sha256,
            )
        ):
            raise _ProjectionCorrupt("worker_projection_prefix_audit_mismatch")
        # The plan-free audit above proves current/event are the exact mirror
        # of the durable ledger prefix.  A mismatch with the sealed new plan
        # after that proof is a real concurrent/stale-plan conflict, not a
        # corruption classification shortcut.
        current, event = cls._before_current_exact(connection, plan)
        dispatch = SQLiteWorkerClaimWriter._read_dispatch_head(connection)
        fence = getattr(plan.request, "dispatch_fence", None)
        if fence is not None and dispatch.state() != fence.before:
            raise _ProjectionConflict("worker_projection_dispatch_conflict")
        order = plan.target.outbox_order
        if order is None or dispatch.inflight_sequence != order.sequence:
            raise _ProjectionConflict("worker_projection_outbox_not_inflight")
        latest = max(
            profile.updated_at,
            mounted.evidence.mounted_at,
            created_at,
            current[19],
            event[13],
            head.updated_at,
            dispatch.updated_at,
        )
        if plan.request.header.occurred_at < latest:
            raise _ProjectionConflict("worker_projection_time_stale")
        return head, dispatch, current, event, mounted

    @staticmethod
    def _identity_present(
        connection: Any,
        plan: WorkerExecutionPlan,
    ) -> bool:
        parent = plan.expected_receipt.receipt_parent.transaction_id
        row = connection.execute(
            """
            SELECT
              EXISTS(SELECT 1 FROM transaction_receipts WHERE transaction_id=?),
              EXISTS(SELECT 1 FROM worker_execution_receipts WHERE request_id=?),
              EXISTS(SELECT 1 FROM worker_ledger_entries WHERE transaction_id=?),
              EXISTS(SELECT 1 FROM worker_effects
                     WHERE authorization_transaction_id=?),
              EXISTS(SELECT 1 FROM worker_results
                     WHERE receipt_transaction_id=?),
              EXISTS(SELECT 1 FROM worker_reconcile_proofs
                     WHERE receipt_transaction_id=?),
              EXISTS(SELECT 1 FROM worker_effect_request_projections
                     WHERE authorization_transaction_id=?),
              EXISTS(SELECT 1 FROM worker_result_payload_projections
                     WHERE receipt_transaction_id=?),
              EXISTS(SELECT 1 FROM worker_proof_payload_projections
                     WHERE receipt_transaction_id=?)
            """,
            (
                parent,
                plan.closure.request_transaction_id,
                parent,
                parent,
                parent,
                parent,
                parent,
                parent,
                parent,
            ),
        ).fetchone()
        return row is not None and any(bool(value) for value in row)

    @staticmethod
    def _secondary_identity_present(
        connection: Any,
        projection: WorkerResultProjectionPlan,
    ) -> bool:
        effect = projection.closure.effect_request
        result = projection.closure.result
        proof = projection.closure.proof
        row = connection.execute(
            """
            SELECT
              EXISTS(SELECT 1 FROM worker_effects WHERE effect_id=?),
              EXISTS(SELECT 1 FROM worker_effect_request_projections
                     WHERE effect_id=?),
              EXISTS(SELECT 1 FROM worker_results WHERE result_id=?),
              EXISTS(SELECT 1 FROM worker_result_payload_projections
                     WHERE result_id=?),
              EXISTS(SELECT 1 FROM worker_reconcile_proofs WHERE proof_id=?),
              EXISTS(SELECT 1 FROM worker_proof_payload_projections
                     WHERE proof_id=?)
            """,
            (
                None if effect is None else effect.binding.effect.effect_id,
                None if effect is None else effect.binding.effect.effect_id,
                None if result is None else result.result_id,
                None if result is None else result.result_id,
                None if proof is None else proof.proof_id,
                None if proof is None else proof.proof_id,
            ),
        ).fetchone()
        if row is None or len(row) != 6:
            raise _ProjectionCorrupt(
                "worker_projection_secondary_identity_unreadable"
            )
        return any(bool(value) for value in row)

    @staticmethod
    def _write_receipt(connection: Any, plan: WorkerExecutionPlan) -> None:
        receipt = plan.expected_receipt
        parent = receipt.receipt_parent
        connection.execute(
            "INSERT INTO transaction_receipts(transaction_id,kind,committed_at) "
            "VALUES (?,?,?)",
            (parent.transaction_id, parent.kind, parent.committed_at),
        )
        connection.execute(
            """
            INSERT INTO worker_ledger_entries(
                sequence,entry_kind,transaction_id,receipt_kind,committed_at,
                previous_tail_sha256,ledger_tail_sha256,request_sha256,
                after_state_sha256,receipt_sha256,occurred_at
            ) VALUES (?,'worker_execution',?,?,?,?,?,?,?,?,?)
            """,
            (
                receipt.sequence,
                parent.transaction_id,
                parent.kind,
                parent.committed_at,
                receipt.previous_ledger_tail_sha256,
                receipt.ledger_tail_sha256,
                receipt.request_sha256,
                receipt.after_state_sha256,
                receipt.receipt_sha256,
                receipt.occurred_at,
            ),
        )
        connection.execute(
            """
            INSERT INTO worker_execution_receipts(
                request_id,transaction_id,entry_kind,receipt_kind,committed_at,
                operation,target_kind,target_id,before_status,after_status,
                before_state_revision,after_state_revision,sequence,
                previous_ledger_tail_sha256,ledger_tail_sha256,manifest_bytes,
                request_sha256,after_state_sha256,receipt_sha256
            ) VALUES (?,?,'worker_execution',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                receipt.request_transaction_id,
                parent.transaction_id,
                parent.kind,
                parent.committed_at,
                receipt.operation.value,
                receipt.target_kind.value,
                receipt.target_id,
                receipt.before_status.value,
                receipt.after_status.value,
                receipt.before_state_revision,
                receipt.after_state_revision,
                receipt.sequence,
                receipt.previous_ledger_tail_sha256,
                receipt.ledger_tail_sha256,
                plan.closure.manifest_bytes,
                receipt.request_sha256,
                receipt.after_state_sha256,
                receipt.receipt_sha256,
            ),
        )

    @staticmethod
    def _write_effect(
        connection: Any,
        plan: WorkerExecutionPlan,
        projection: WorkerResultProjectionPlan,
    ) -> None:
        value = projection.closure.effect_request
        assert value is not None and value.binding.effect is not None
        effect = value.binding.effect
        parent = plan.expected_receipt.receipt_parent
        connection.execute(
            """
            INSERT INTO worker_effects(
                effect_id,target_id,attempt_id,effect_kind,
                authorization_transaction_id,idempotency_key,authorized_at
            ) VALUES (?,?,?,?,?,?,?)
            """,
            (
                effect.effect_id,
                plan.target.target_id,
                effect.attempt_id,
                effect.effect_kind.value,
                parent.transaction_id,
                effect.idempotency_key,
                parent.committed_at,
            ),
        )
        payload = value.request_payload
        connection.execute(
            """
            INSERT INTO worker_effect_request_projections(
                effect_id,target_id,target_kind,attempt_id,attempt_number,
                effect_kind,authorization_transaction_id,protocol,
                projection_schema,idempotency_scope,idempotency_key,
                request_schema,request_codec_id,request_codec_version,
                request_payload_bytes,request_payload_sha256,
                projection_manifest_bytes,projection_sha256,
                closure_manifest_bytes,closure_manifest_sha256,authorized_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                effect.effect_id,
                plan.target.target_id,
                plan.target.target_kind.value,
                effect.attempt_id,
                effect.attempt_number,
                effect.effect_kind.value,
                parent.transaction_id,
                WORKER_RESULT_PROJECTION_PROTOCOL,
                WORKER_EFFECT_REQUEST_PROJECTION_SCHEMA,
                value.idempotency_scope,
                value.idempotency_key,
                payload.schema,
                payload.codec_id,
                payload.codec_version,
                payload.payload_bytes,
                payload.payload_sha256,
                value.projection_manifest_bytes,
                value.projection_sha256,
                projection.closure.manifest_bytes,
                projection.closure.manifest_sha256,
                parent.committed_at,
            ),
        )

    @staticmethod
    def _write_result(
        connection: Any,
        plan: WorkerExecutionPlan,
        projection: WorkerResultProjectionPlan,
    ) -> None:
        result = projection.closure.result
        assert result is not None
        proof = projection.closure.proof
        binding = result.binding
        parent = plan.expected_receipt.receipt_parent
        effect_id = None if binding.effect is None else binding.effect.effect_id
        v8_proof = _v8_worker_result_mirror_proof_sha256(projection)
        connection.execute(
            """
            INSERT INTO worker_results(
                result_id,target_id,attempt_id,effect_id,terminal_status,
                result_sha256,proof_sha256,receipt_transaction_id,completed_at
            ) VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                result.result_id,
                plan.target.target_id,
                binding.attempt_id,
                effect_id,
                result.after_status.value,
                result.result_sha256,
                v8_proof,
                parent.transaction_id,
                result.committed_at,
            ),
        )
        if plan.operation is WorkerExecutionOperation.RECONCILE:
            assert proof is not None and effect_id is not None
            connection.execute(
                """
                INSERT INTO worker_reconcile_proofs(
                    proof_id,target_id,effect_id,result_id,proof_kind,
                    proof_bytes,proof_sha256,receipt_transaction_id,observed_at
                ) VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    proof.proof_id,
                    plan.target.target_id,
                    effect_id,
                    (
                        proof.result_id
                        if proof.proof_kind == "durable_result"
                        else None
                    ),
                    proof.proof_kind,
                    proof.projection_manifest_bytes,
                    proof.proof_sha256,
                    parent.transaction_id,
                    proof.observed_at,
                ),
            )
        connection.execute(
            """
            INSERT INTO worker_result_payload_projections(
                result_id,target_id,target_kind,attempt_id,attempt_number,
                effect_id,receipt_transaction_id,operation,protocol,
                projection_schema,after_status,committed_at,
                result_schema,result_codec_id,result_codec_version,
                result_payload_bytes,result_payload_sha256,
                error_schema,error_codec_id,error_codec_version,
                error_payload_bytes,error_payload_sha256,
                projection_manifest_bytes,result_sha256,proof_sha256,
                closure_manifest_bytes,closure_manifest_sha256
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                result.result_id,
                plan.target.target_id,
                plan.target.target_kind.value,
                binding.attempt_id,
                binding.attempt_number,
                effect_id,
                parent.transaction_id,
                plan.operation.value,
                WORKER_RESULT_PROJECTION_PROTOCOL,
                WORKER_RESULT_PAYLOAD_PROJECTION_SCHEMA,
                result.after_status.value,
                result.committed_at,
                *_payload_columns(result.result_payload),
                *_payload_columns(result.error_payload),
                result.projection_manifest_bytes,
                result.result_sha256,
                result.proof_sha256,
                projection.closure.manifest_bytes,
                projection.closure.manifest_sha256,
            ),
        )
        if proof is not None:
            connection.execute(
                """
                INSERT INTO worker_proof_payload_projections(
                    proof_id,result_id,target_id,target_kind,attempt_id,
                    attempt_number,effect_id,receipt_transaction_id,operation,
                    protocol,projection_schema,proof_class,proof_kind,observed_at,
                    evidence_schema,evidence_codec_id,evidence_codec_version,
                    evidence_payload_bytes,evidence_payload_sha256,
                    provider_receipt_schema,provider_receipt_codec_id,
                    provider_receipt_codec_version,provider_receipt_payload_bytes,
                    provider_receipt_payload_sha256,durable_result_id,
                    durable_result_schema,durable_result_codec_id,
                    durable_result_codec_version,durable_result_payload_bytes,
                    durable_result_payload_sha256,durable_result_recorded_at,
                    result_commit_id,projection_manifest_bytes,proof_sha256,
                    closure_manifest_bytes,closure_manifest_sha256
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    proof.proof_id,
                    proof.result_id,
                    plan.target.target_id,
                    plan.target.target_kind.value,
                    binding.attempt_id,
                    binding.attempt_number,
                    effect_id,
                    parent.transaction_id,
                    plan.operation.value,
                    WORKER_RESULT_PROJECTION_PROTOCOL,
                    WORKER_PROOF_PAYLOAD_PROJECTION_SCHEMA,
                    proof.proof_class.value,
                    proof.proof_kind,
                    proof.observed_at,
                    *_payload_columns(proof.evidence_payload),
                    *_payload_columns(proof.provider_receipt_payload),
                    proof.durable_result_id,
                    (
                        None
                        if proof.durable_result_payload is None
                        else proof.durable_result_payload.schema
                    ),
                    (
                        None
                        if proof.durable_result_payload is None
                        else proof.durable_result_payload.codec_id
                    ),
                    (
                        None
                        if proof.durable_result_payload is None
                        else proof.durable_result_payload.codec_version
                    ),
                    (
                        None
                        if proof.durable_result_payload is None
                        else proof.durable_result_payload.payload_bytes
                    ),
                    (
                        None
                        if proof.durable_result_payload is None
                        else proof.durable_result_payload.payload_sha256
                    ),
                    proof.durable_result_recorded_at,
                    proof.result_commit_id,
                    proof.projection_manifest_bytes,
                    proof.proof_sha256,
                    projection.closure.manifest_bytes,
                    projection.closure.manifest_sha256,
                ),
            )

    @staticmethod
    def _event_after_values(plan: WorkerExecutionPlan, event: Any) -> tuple[Any, ...]:
        after = plan.closure.after_state
        status = after.status
        request = plan.request
        if type(request) is WorkerCompleteRequest:
            error_bytes = None if request.error is None else request.error.payload_bytes
            error_sha256 = (
                None if request.error is None else request.error.payload_sha256
            )
        else:
            # AUTHORIZE and RECONCILE do not own the legacy diagnostic slot.
            # Preserve it; the V9 sidecar is the sole owner of new proof bytes.
            error_bytes = event[10]
            error_sha256 = event[11]
        published_at = (
            request.header.occurred_at
            if status is WorkerExecutionStatus.PUBLISHED
            else None
        )
        unknown_at = (
            request.header.occurred_at
            if status is WorkerExecutionStatus.OUTCOME_UNKNOWN
            else None
        )
        dead_at = (
            request.header.occurred_at
            if status is WorkerExecutionStatus.DEAD
            else None
        )
        available_at = (
            after.next_attempt_at
            if status is WorkerExecutionStatus.RETRY
            else event[3]
        )
        return (
            status.value,
            after.state_revision,
            after.attempt_count,
            available_at,
            after.lease_owner,
            after.lease_fence,
            after.lease_expires_at,
            published_at,
            unknown_at,
            dead_at,
            error_bytes,
            error_sha256,
            request.header.occurred_at,
        )

    @classmethod
    def _write_current_and_event(
        cls,
        connection: Any,
        plan: WorkerExecutionPlan,
        current: Any,
        event: Any,
    ) -> None:
        after = plan.closure.after_state
        receipt = plan.expected_receipt
        parent_id = receipt.receipt_parent.transaction_id
        event_values = cls._event_after_values(plan, event)
        changed = connection.execute(
            """
            UPDATE event_outbox
            SET status=?,state_revision=?,attempt_count=?,available_at=?,
                lease_owner=?,lease_fence=?,lease_expires_at=?,published_at=?,
                outcome_unknown_at=?,dead_at=?,last_error_bytes=?,
                last_error_sha256=?,updated_at=?
            WHERE event_id=? AND status=? AND state_revision=? AND
                  attempt_count=? AND available_at=? AND lease_owner IS ? AND
                  lease_fence IS ? AND lease_expires_at IS ? AND
                  published_at IS ? AND outcome_unknown_at IS ? AND dead_at IS ? AND
                  last_error_bytes IS ? AND last_error_sha256 IS ? AND updated_at=?
            """,
            (
                *event_values,
                plan.target.target_id,
                *tuple(event[:12]),
                event[13],
            ),
        ).rowcount
        if changed != 1:
            raise _ProjectionConflict("worker_projection_event_cas_conflict")
        changed = connection.execute(
            """
            UPDATE worker_target_current
            SET status=?,state_revision=?,attempt_count=?,last_attempt_id=?,
                last_attempt_number=?,current_attempt_id=?,current_attempt_number=?,
                fence_high_water=?,cancellation_pending=?,next_attempt_at=?,
                lease_owner=?,lease_fence=?,lease_expires_at=?,current_effect_id=?,
                unresolved_effect_id=?,last_receipt_transaction_id=?,
                previous_state_sha256=state_sha256,state_sha256=?,updated_at=?
            WHERE target_id=? AND target_kind='outbox' AND status=? AND
                  state_revision=? AND attempt_count=? AND last_attempt_id IS ? AND
                  last_attempt_number IS ? AND current_attempt_id IS ? AND
                  current_attempt_number IS ? AND fence_high_water=? AND
                  cancellation_pending=? AND next_attempt_at IS ? AND lease_owner IS ? AND
                  lease_fence IS ? AND lease_expires_at IS ? AND current_effect_id IS ? AND
                  unresolved_effect_id IS ? AND last_receipt_transaction_id=? AND
                  previous_state_sha256=? AND state_sha256=? AND updated_at=?
            """,
            (
                after.status.value,
                after.state_revision,
                after.attempt_count,
                after.last_attempt_id,
                after.last_attempt_number,
                after.current_attempt_id,
                after.current_attempt_number,
                after.fence_high_water,
                int(after.cancellation_pending),
                after.next_attempt_at,
                after.lease_owner,
                after.lease_fence,
                after.lease_expires_at,
                None if after.current_effect is None else after.current_effect.effect_id,
                (
                    None
                    if after.unresolved_effect is None
                    else after.unresolved_effect.effect_id
                ),
                parent_id,
                receipt.after_state_sha256,
                receipt.occurred_at,
                plan.target.target_id,
                *tuple(current[1:16]),
                current[16],
                current[17],
                current[18],
                current[19],
            ),
        ).rowcount
        if changed != 1:
            raise _ProjectionConflict("worker_projection_current_cas_conflict")

    @staticmethod
    def _write_dispatch(
        connection: Any,
        plan: WorkerExecutionPlan,
        dispatch: _DispatchHeadSnapshot,
    ) -> None:
        fence = getattr(plan.request, "dispatch_fence", None)
        if fence is None or fence.after == fence.before:
            return
        after = fence.after
        inflight = after.inflight
        changed = connection.execute(
            """
            UPDATE outbox_dispatch_head
            SET state_revision=?,published_sequence=?,
                published_repository_revision=?,published_ordinal=?,
                inflight_sequence=?,inflight_repository_revision=?,
                inflight_ordinal=?,previous_head_sha256=head_sha256,
                head_sha256=?,updated_at=?
            WHERE singleton=1 AND state_revision=? AND published_sequence=? AND
                  published_repository_revision IS ? AND published_ordinal IS ? AND
                  inflight_sequence IS ? AND inflight_repository_revision IS ? AND
                  inflight_ordinal IS ? AND previous_head_sha256=? AND
                  head_sha256=? AND updated_at=?
            """,
            (
                after.control_revision,
                after.published.sequence,
                (
                    None
                    if after.published.sequence == 0
                    else after.published.repository_revision
                ),
                None if after.published.sequence == 0 else after.published.ordinal,
                None if inflight is None else inflight.sequence,
                None if inflight is None else inflight.repository_revision,
                None if inflight is None else inflight.ordinal,
                plan.expected_receipt.after_state_sha256,
                plan.expected_receipt.occurred_at,
                dispatch.state_revision,
                dispatch.published_sequence,
                dispatch.published_repository_revision,
                dispatch.published_ordinal,
                dispatch.inflight_sequence,
                dispatch.inflight_repository_revision,
                dispatch.inflight_ordinal,
                dispatch.previous_head_sha256,
                dispatch.head_sha256,
                dispatch.updated_at,
            ),
        ).rowcount
        if changed != 1:
            raise _ProjectionConflict("worker_projection_dispatch_cas_conflict")

    @staticmethod
    def _write_head(
        connection: Any,
        plan: WorkerExecutionPlan,
        head: _WorkerHeadSnapshot,
    ) -> None:
        receipt = plan.expected_receipt
        changed = connection.execute(
            """
            UPDATE worker_ledger_head
            SET state_revision=?,sequence=?,previous_tail_sha256=tail_sha256,
                tail_sha256=?,updated_at=?
            WHERE singleton=1 AND state_revision=? AND sequence=? AND
                  previous_tail_sha256=? AND tail_sha256=? AND updated_at=?
            """,
            (
                head.state_revision + 1,
                plan.after_ledger.sequence,
                plan.after_ledger.tail_sha256,
                receipt.occurred_at,
                head.state_revision,
                head.sequence,
                head.previous_tail_sha256,
                head.tail_sha256,
                head.updated_at,
            ),
        ).rowcount
        if changed != 1:
            raise _ProjectionConflict("worker_projection_ledger_cas_conflict")

    @classmethod
    def _write(
        cls,
        connection: Any,
        plan: WorkerExecutionPlan,
        projection: WorkerResultProjectionPlan,
        head: _WorkerHeadSnapshot,
        dispatch: _DispatchHeadSnapshot,
        current: Any,
        event: Any,
    ) -> None:
        cls._write_receipt(connection, plan)
        if plan.operation is WorkerExecutionOperation.AUTHORIZE_EFFECT:
            cls._write_effect(connection, plan, projection)
        else:
            cls._write_result(connection, plan, projection)
        cls._write_current_and_event(connection, plan, current, event)
        cls._write_dispatch(connection, plan, dispatch)
        cls._write_head(connection, plan, head)

    @staticmethod
    def _effect_projection_exact(
        connection: Any,
        plan: WorkerExecutionPlan,
        projection: WorkerResultProjectionPlan,
    ) -> bool:
        value = projection.closure.effect_request
        if value is None or value.binding.effect is None:
            return False
        effect = value.binding.effect
        parent = plan.expected_receipt.receipt_parent
        mirror = connection.execute(
            "SELECT target_id,attempt_id,effect_kind,authorization_transaction_id,"
            "idempotency_key,authorized_at FROM worker_effects WHERE effect_id=?",
            (effect.effect_id,),
        ).fetchone()
        sidecar = connection.execute(
            """
            SELECT target_id,target_kind,attempt_id,attempt_number,effect_kind,
                   authorization_transaction_id,protocol,projection_schema,
                   idempotency_scope,idempotency_key,request_schema,
                   request_codec_id,request_codec_version,request_payload_bytes,
                   request_payload_sha256,projection_manifest_bytes,
                   projection_sha256,closure_manifest_bytes,
                   closure_manifest_sha256,authorized_at
            FROM worker_effect_request_projections WHERE effect_id=?
            """,
            (effect.effect_id,),
        ).fetchone()
        return bool(
            _row_is(mirror, (
                plan.target.target_id,
                effect.attempt_id,
                effect.effect_kind.value,
                parent.transaction_id,
                effect.idempotency_key,
                parent.committed_at,
            ))
            and _row_is(sidecar, (
                plan.target.target_id,
                plan.target.target_kind.value,
                effect.attempt_id,
                effect.attempt_number,
                effect.effect_kind.value,
                parent.transaction_id,
                WORKER_RESULT_PROJECTION_PROTOCOL,
                WORKER_EFFECT_REQUEST_PROJECTION_SCHEMA,
                value.idempotency_scope,
                value.idempotency_key,
                value.request_payload.schema,
                value.request_payload.codec_id,
                value.request_payload.codec_version,
                value.request_payload.payload_bytes,
                value.request_payload.payload_sha256,
                value.projection_manifest_bytes,
                value.projection_sha256,
                projection.closure.manifest_bytes,
                projection.closure.manifest_sha256,
                parent.committed_at,
            ))
        )

    @staticmethod
    def _result_projection_exact(
        connection: Any,
        plan: WorkerExecutionPlan,
        projection: WorkerResultProjectionPlan,
    ) -> bool:
        result = projection.closure.result
        if result is None:
            return False
        proof = projection.closure.proof
        binding = result.binding
        parent = plan.expected_receipt.receipt_parent
        effect_id = None if binding.effect is None else binding.effect.effect_id
        mirror = connection.execute(
            "SELECT target_id,attempt_id,effect_id,terminal_status,result_sha256,"
            "proof_sha256,receipt_transaction_id,completed_at FROM worker_results "
            "WHERE result_id=?",
            (result.result_id,),
        ).fetchone()
        sidecar = connection.execute(
            """
            SELECT target_id,target_kind,attempt_id,attempt_number,effect_id,
                   receipt_transaction_id,operation,protocol,projection_schema,
                   after_status,committed_at,result_schema,result_codec_id,
                   result_codec_version,result_payload_bytes,result_payload_sha256,
                   error_schema,error_codec_id,error_codec_version,
                   error_payload_bytes,error_payload_sha256,
                   projection_manifest_bytes,result_sha256,proof_sha256,
                   closure_manifest_bytes,closure_manifest_sha256
            FROM worker_result_payload_projections WHERE result_id=?
            """,
            (result.result_id,),
        ).fetchone()
        if not _row_is(mirror, (
            plan.target.target_id,
            binding.attempt_id,
            effect_id,
            result.after_status.value,
            result.result_sha256,
            _v8_worker_result_mirror_proof_sha256(projection),
            parent.transaction_id,
            result.committed_at,
        )) or not _row_is(sidecar, (
            plan.target.target_id,
            plan.target.target_kind.value,
            binding.attempt_id,
            binding.attempt_number,
            effect_id,
            parent.transaction_id,
            plan.operation.value,
            WORKER_RESULT_PROJECTION_PROTOCOL,
            WORKER_RESULT_PAYLOAD_PROJECTION_SCHEMA,
            result.after_status.value,
            result.committed_at,
            *_payload_columns(result.result_payload),
            *_payload_columns(result.error_payload),
            result.projection_manifest_bytes,
            result.result_sha256,
            result.proof_sha256,
            projection.closure.manifest_bytes,
            projection.closure.manifest_sha256,
        )):
            return False
        reconcile = connection.execute(
            "SELECT proof_id,target_id,effect_id,result_id,proof_kind,proof_bytes,"
            "proof_sha256,receipt_transaction_id,observed_at "
            "FROM worker_reconcile_proofs WHERE receipt_transaction_id=?",
            (parent.transaction_id,),
        ).fetchone()
        proof_sidecar = connection.execute(
            """
            SELECT result_id,target_id,target_kind,attempt_id,attempt_number,
                   effect_id,receipt_transaction_id,operation,protocol,
                   projection_schema,proof_class,proof_kind,observed_at,
                   evidence_schema,evidence_codec_id,evidence_codec_version,
                   evidence_payload_bytes,evidence_payload_sha256,
                   provider_receipt_schema,provider_receipt_codec_id,
                   provider_receipt_codec_version,provider_receipt_payload_bytes,
                   provider_receipt_payload_sha256,durable_result_id,
                   durable_result_schema,durable_result_codec_id,
                   durable_result_codec_version,durable_result_payload_bytes,
                   durable_result_payload_sha256,durable_result_recorded_at,
                   result_commit_id,projection_manifest_bytes,proof_sha256,
                   closure_manifest_bytes,closure_manifest_sha256
            FROM worker_proof_payload_projections WHERE receipt_transaction_id=?
            """,
            (parent.transaction_id,),
        ).fetchone()
        if proof is None:
            return reconcile is None and proof_sidecar is None
        if plan.operation is WorkerExecutionOperation.RECONCILE:
            if not _row_is(reconcile, (
                proof.proof_id,
                plan.target.target_id,
                effect_id,
                proof.result_id if proof.proof_kind == "durable_result" else None,
                proof.proof_kind,
                proof.projection_manifest_bytes,
                proof.proof_sha256,
                parent.transaction_id,
                proof.observed_at,
            )):
                return False
        elif reconcile is not None:
            return False
        return _row_is(proof_sidecar, (
            proof.result_id,
            plan.target.target_id,
            plan.target.target_kind.value,
            binding.attempt_id,
            binding.attempt_number,
            effect_id,
            parent.transaction_id,
            plan.operation.value,
            WORKER_RESULT_PROJECTION_PROTOCOL,
            WORKER_PROOF_PAYLOAD_PROJECTION_SCHEMA,
            proof.proof_class.value,
            proof.proof_kind,
            proof.observed_at,
            *_payload_columns(proof.evidence_payload),
            *_payload_columns(proof.provider_receipt_payload),
            proof.durable_result_id,
            (
                None
                if proof.durable_result_payload is None
                else proof.durable_result_payload.schema
            ),
            (
                None
                if proof.durable_result_payload is None
                else proof.durable_result_payload.codec_id
            ),
            (
                None
                if proof.durable_result_payload is None
                else proof.durable_result_payload.codec_version
            ),
            (
                None
                if proof.durable_result_payload is None
                else proof.durable_result_payload.payload_bytes
            ),
            (
                None
                if proof.durable_result_payload is None
                else proof.durable_result_payload.payload_sha256
            ),
            proof.durable_result_recorded_at,
            proof.result_commit_id,
            proof.projection_manifest_bytes,
            proof.proof_sha256,
            projection.closure.manifest_bytes,
            projection.closure.manifest_sha256,
        ))

    @classmethod
    def _after_exact(
        cls,
        connection: Any,
        plan: WorkerExecutionPlan,
        projection: WorkerResultProjectionPlan,
    ) -> bool:
        after = plan.closure.after_state
        receipt = plan.expected_receipt
        current = cls._read_current(connection, plan.target.target_id)
        expected_current = (
            "outbox",
            after.status.value,
            after.state_revision,
            after.attempt_count,
            after.last_attempt_id,
            after.last_attempt_number,
            after.current_attempt_id,
            after.current_attempt_number,
            after.fence_high_water,
            int(after.cancellation_pending),
            after.next_attempt_at,
            after.lease_owner,
            after.lease_fence,
            after.lease_expires_at,
            None if after.current_effect is None else after.current_effect.effect_id,
            (
                None
                if after.unresolved_effect is None
                else after.unresolved_effect.effect_id
            ),
            receipt.receipt_parent.transaction_id,
            current[17],
            receipt.after_state_sha256,
            receipt.occurred_at,
        )
        if not _row_is(current, expected_current):
            return False
        event = cls._read_event(connection, plan.target.target_id)
        if not _row_is(event[:12] + event[13:14], cls._event_after_values(plan, event)):
            return False
        head = SQLiteWorkerClaimWriter._read_worker_head(connection)
        if (
            head.sequence != plan.after_ledger.sequence
            or not _same(head.previous_tail_sha256, plan.before_ledger.tail_sha256)
            or not _same(head.tail_sha256, plan.after_ledger.tail_sha256)
            or head.updated_at != receipt.occurred_at
        ):
            return False
        fence = getattr(plan.request, "dispatch_fence", None)
        dispatch = SQLiteWorkerClaimWriter._read_dispatch_head(connection)
        expected_dispatch = dispatch.state() if fence is None else fence.after
        if dispatch.state() != expected_dispatch:
            return False
        if fence is not None and fence.after != fence.before and (
            not _same(dispatch.head_sha256, receipt.after_state_sha256)
            or dispatch.updated_at != receipt.occurred_at
        ):
            return False
        if plan.operation is WorkerExecutionOperation.AUTHORIZE_EFFECT:
            projection_exact = cls._effect_projection_exact(
                connection,
                plan,
                projection,
            )
        else:
            projection_exact = cls._result_projection_exact(
                connection,
                plan,
                projection,
            )
        counts = connection.execute(
            """
            SELECT
              (SELECT count(*) FROM worker_effects
               WHERE authorization_transaction_id=?),
              (SELECT count(*) FROM worker_results
               WHERE receipt_transaction_id=?),
              (SELECT count(*) FROM worker_reconcile_proofs
               WHERE receipt_transaction_id=?),
              (SELECT count(*) FROM worker_effect_request_projections
               WHERE authorization_transaction_id=?),
              (SELECT count(*) FROM worker_result_payload_projections
               WHERE receipt_transaction_id=?),
              (SELECT count(*) FROM worker_proof_payload_projections
               WHERE receipt_transaction_id=?)
            """,
            (receipt.receipt_parent.transaction_id,) * 6,
        ).fetchone()
        expected_counts = {
            WorkerExecutionOperation.AUTHORIZE_EFFECT: (1, 0, 0, 1, 0, 0),
            WorkerExecutionOperation.COMPLETE: (
                0,
                1,
                0,
                0,
                1,
                int(projection.closure.proof is not None),
            ),
            WorkerExecutionOperation.RECONCILE: (0, 1, 1, 0, 1, 1),
        }[plan.operation]
        return bool(projection_exact and _row_is(counts, expected_counts))

    def _reconcile_snapshot(
        self,
        connection: Any,
        plan: WorkerExecutionPlan,
        projection: WorkerResultProjectionPlan,
        *,
        validate_runtime: bool = True,
        profile: Any = None,
        mounted: Any = None,
    ) -> SQLiteWorkerProjectionResult:
        if validate_runtime:
            try:
                profile = _validate_business_runtime_connection(
                    self._database,
                    connection,
                    access=BusinessRuntimeAccess.READER,
                )
                mounted = _binding_from_snapshot(
                    connection,
                    profile,
                    _mounted_row(connection),
                )
            except SQLiteProductionAuthorityError:
                return self._result(
                    plan,
                    SQLiteWorkerProjectionStatus.CORRUPT,
                    "worker_projection_mount_corrupt",
                )
            except SQLiteKernelError as exc:
                return self._kernel_failure_result(plan, exc)
            except Exception:
                return self._result(
                    plan,
                    SQLiteWorkerProjectionStatus.UNKNOWN,
                    "worker_projection_runtime_or_mount_unavailable",
                )
            if not SQLiteWorkerClaimWriter._authority_descends(
                profile,
                mounted,
                plan,
            ):
                return self._result(
                    plan,
                    SQLiteWorkerProjectionStatus.CONFLICT,
                    "worker_projection_authority_conflict",
                )
        elif profile is None or mounted is None:
            return self._result(
                plan,
                SQLiteWorkerProjectionStatus.UNKNOWN,
                "worker_projection_audit_context_missing",
            )
        try:
            audit = _audit_snapshot(
                connection,
                profile,
                mounted,
                plan.closure.request_transaction_id,
                self._durable_identity(plan, projection),
            )
            audit_status = audit.result.status
            if audit_status is SQLiteWorkerDurableAuditStatus.ABSENT:
                if (
                    audit.result.ledger_sequence != plan.before_ledger.sequence
                    or not _same(
                        audit.result.ledger_tail_sha256,
                        plan.before_ledger.tail_sha256,
                    )
                ):
                    return self._result(
                        plan,
                        SQLiteWorkerProjectionStatus.CONFLICT,
                        "worker_projection_absent_state_conflict",
                    )
                return self._result(
                    plan,
                    SQLiteWorkerProjectionStatus.NOT_COMMITTED,
                    "worker_projection_absent",
                )
            if audit_status is SQLiteWorkerDurableAuditStatus.CONFLICT:
                return self._result(
                    plan,
                    SQLiteWorkerProjectionStatus.CONFLICT,
                    audit.result.code,
                )
            if audit_status is SQLiteWorkerDurableAuditStatus.UNKNOWN:
                return self._result(
                    plan,
                    SQLiteWorkerProjectionStatus.UNKNOWN,
                    audit.result.code,
                )
            if audit_status is not SQLiteWorkerDurableAuditStatus.EXACT:
                return self._result(
                    plan,
                    SQLiteWorkerProjectionStatus.CORRUPT,
                    audit.result.code,
                )
            # When this request owns the current head, retain an independent
            # row-for-row assertion around the writer's exact update surface.
            # Historical membership is already fully proved by the plan-free
            # auditor and must not be downgraded merely because a suffix exists.
            if (
                audit.result.ledger_sequence == plan.after_ledger.sequence
                and not self._after_exact(connection, plan, projection)
            ):
                raise _ProjectionCorrupt("worker_projection_durable_closure_corrupt")
        except _AuditCorrupt as exc:
            return self._result(
                plan,
                SQLiteWorkerProjectionStatus.CORRUPT,
                str(exc) or "worker_projection_durable_audit_corrupt",
            )
        except _AuditConflict as exc:
            return self._result(
                plan,
                SQLiteWorkerProjectionStatus.CONFLICT,
                str(exc) or "worker_projection_durable_audit_conflict",
            )
        except (_AuditBudget, _AuditUnsupported) as exc:
            return self._result(
                plan,
                SQLiteWorkerProjectionStatus.UNKNOWN,
                str(exc) or "worker_projection_durable_audit_unavailable",
            )
        except (_ProjectionCorrupt, _ClaimCorrupt) as exc:
            return self._result(
                plan,
                SQLiteWorkerProjectionStatus.CORRUPT,
                str(exc) or "worker_projection_durable_closure_corrupt",
            )
        except (TypeError, ValueError, IndexError, OverflowError):
            return self._result(
                plan,
                SQLiteWorkerProjectionStatus.CORRUPT,
                "worker_projection_durable_closure_corrupt",
            )
        return self._result(
            plan,
            SQLiteWorkerProjectionStatus.COMMITTED,
            "worker_projection_exact",
        )

    def reconcile(
        self,
        plan: WorkerExecutionPlan,
        projection: WorkerResultProjectionPlan,
    ) -> SQLiteWorkerProjectionResult:
        plan = _require_worker_execution_plan(plan)
        projection = _require_worker_result_projection_plan(projection)
        self._require_vertical(plan, projection)
        connection = None
        try:
            connection, _filesystem = self._database._open_unvalidated()
            connection.execute("BEGIN")
            result = self._reconcile_snapshot(connection, plan, projection)
        except Exception:
            if connection is not None:
                _rollback_close(connection)
            return self._result(
                plan,
                SQLiteWorkerProjectionStatus.UNKNOWN,
                "worker_projection_reconcile_unavailable",
            )
        else:
            _rollback_close(connection)
        if result.status is SQLiteWorkerProjectionStatus.COMMITTED:
            try:
                self._database._finalize_committed_storage()
            except Exception:
                return self._result(
                    plan,
                    SQLiteWorkerProjectionStatus.UNKNOWN,
                    "worker_projection_durability_barrier_failed",
                )
        return result

    def commit(
        self,
        plan: WorkerExecutionPlan,
        projection: WorkerResultProjectionPlan,
        *,
        _test_hook: Optional[
            Callable[[SQLiteWorkerProjectionTestPoint], None]
        ] = None,
    ) -> SQLiteWorkerProjectionResult:
        plan = _require_worker_execution_plan(plan)
        projection = _require_worker_result_projection_plan(projection)
        self._require_vertical(plan, projection)
        if _test_hook is not None and not callable(_test_hook):
            raise TypeError("_test_hook must be callable")
        connection = None
        committed_started = False
        identity_absent = False
        authorizer = None
        test_hook_failed = False
        try:
            connection, _filesystem = self._database._open_unvalidated()
            connection.execute("BEGIN IMMEDIATE")
            profile = _validate_business_runtime_connection(
                self._database,
                connection,
                access=BusinessRuntimeAccess.WRITER,
            )
            if self._identity_present(connection, plan):
                _rollback_close(connection)
                connection = None
                result = self.reconcile(plan, projection)
                if result.status is SQLiteWorkerProjectionStatus.COMMITTED:
                    return SQLiteWorkerProjectionResult(
                        result.status,
                        result.request_id,
                        result.code,
                        True,
                    )
                return result
            identity_absent = True
            head, dispatch, current, event, mounted = self._require_fresh_before(
                connection,
                profile,
                plan,
            )
            if self._secondary_identity_present(connection, projection):
                raise _ProjectionConflict(
                    "worker_projection_secondary_identity_conflict"
                )
            authorizer = _WorkerProjectionAuthorizer()
            connection.set_authorizer(authorizer)
            self._write(
                connection,
                plan,
                projection,
                head,
                dispatch,
                current,
                event,
            )
            if authorizer.denied:
                raise _ProjectionCorrupt("worker_projection_authorizer_denied")
            verified = self._reconcile_snapshot(
                connection,
                plan,
                projection,
                validate_runtime=False,
                profile=profile,
                mounted=mounted,
            )
            if verified.status is not SQLiteWorkerProjectionStatus.COMMITTED:
                raise _ProjectionCorrupt("worker_projection_precommit_closure_mismatch")
            if _test_hook is not None:
                try:
                    _test_hook(SQLiteWorkerProjectionTestPoint.BEFORE_COMMIT)
                except Exception:
                    test_hook_failed = True
                    raise
            plan = _require_worker_execution_plan(plan)
            projection = _require_worker_result_projection_plan(projection)
            self._require_vertical(plan, projection)
            if authorizer.denied:
                raise _ProjectionCorrupt("worker_projection_authorizer_denied")
            final = self._reconcile_snapshot(
                connection,
                plan,
                projection,
                validate_runtime=False,
                profile=profile,
                mounted=mounted,
            )
            if final.status is not SQLiteWorkerProjectionStatus.COMMITTED:
                raise _ProjectionCorrupt("worker_projection_final_closure_mismatch")
            self._require_database_time(connection, plan)
            if authorizer.denied:
                raise _ProjectionCorrupt("worker_projection_authorizer_denied")
            connection.set_authorizer(_allow_authorizer)
            committed_started = True
            connection.commit()
            if _test_hook is not None:
                _test_hook(SQLiteWorkerProjectionTestPoint.AFTER_COMMIT_BEFORE_ACK)
            connection.close()
            connection = None
            self._database._finalize_committed_storage()
            return self._result(
                plan,
                SQLiteWorkerProjectionStatus.COMMITTED,
                "worker_projection_committed",
            )
        except _ProjectionConflict as failure:
            rollback_complete = True
            if connection is not None:
                rollback_complete = _rollback_close(connection)
                connection = None
            if not rollback_complete:
                reconciled = self.reconcile(plan, projection)
                if reconciled.status is SQLiteWorkerProjectionStatus.COMMITTED:
                    return SQLiteWorkerProjectionResult(
                        reconciled.status,
                        reconciled.request_id,
                        reconciled.code,
                        True,
                    )
                return reconciled
            return self._result(
                plan,
                SQLiteWorkerProjectionStatus.CONFLICT,
                str(failure) or "worker_projection_conflict",
            )
        except _ProjectionUnavailable as failure:
            rollback_complete = True
            if connection is not None:
                rollback_complete = _rollback_close(connection)
                connection = None
            if not rollback_complete:
                return self.reconcile(plan, projection)
            return self._result(
                plan,
                SQLiteWorkerProjectionStatus.UNKNOWN,
                str(failure) or "worker_projection_audit_unavailable",
            )
        except (_ProjectionCorrupt, _ClaimCorrupt) as failure:
            rollback_complete = True
            if connection is not None:
                rollback_complete = _rollback_close(connection)
                connection = None
            if not rollback_complete:
                reconciled = self.reconcile(plan, projection)
                if reconciled.status is SQLiteWorkerProjectionStatus.COMMITTED:
                    return SQLiteWorkerProjectionResult(
                        reconciled.status,
                        reconciled.request_id,
                        reconciled.code,
                        True,
                    )
                return reconciled
            return self._result(
                plan,
                SQLiteWorkerProjectionStatus.CORRUPT,
                str(failure) or "worker_projection_corrupt",
            )
        except BaseException as failure:
            process_control = not isinstance(failure, Exception)
            if not committed_started:
                if connection is not None:
                    rollback_complete = _rollback_close(connection)
                    connection = None
                else:
                    rollback_complete = True
                if process_control:
                    raise
                if rollback_complete and test_hook_failed and identity_absent:
                    reconciled = self.reconcile(plan, projection)
                    if reconciled.status is SQLiteWorkerProjectionStatus.NOT_COMMITTED:
                        return self._result(
                            plan,
                            SQLiteWorkerProjectionStatus.NOT_COMMITTED,
                            "worker_projection_rolled_back",
                        )
                    return reconciled
                if rollback_complete:
                    if isinstance(failure, SQLiteKernelError):
                        return self._kernel_failure_result(plan, failure)
                    if authorizer is not None and authorizer.denied:
                        return self._result(
                            plan,
                            SQLiteWorkerProjectionStatus.CORRUPT,
                            "worker_projection_authorizer_denied",
                        )
                    if isinstance(failure, sqlite3.IntegrityError):
                        return self._result(
                            plan,
                            SQLiteWorkerProjectionStatus.CORRUPT,
                            "worker_projection_schema_integrity_failure",
                        )
                    if isinstance(
                        failure,
                        (OSError, MemoryError, sqlite3.OperationalError),
                    ):
                        return self._result(
                            plan,
                            SQLiteWorkerProjectionStatus.UNKNOWN,
                            "worker_projection_storage_unavailable",
                        )
                    return self._result(
                        plan,
                        SQLiteWorkerProjectionStatus.CORRUPT,
                        "worker_projection_internal_failure",
                    )
            else:
                if connection is not None:
                    _close_quietly(connection)
                    connection = None
            if process_control:
                raise
            reconciled = self.reconcile(plan, projection)
            if reconciled.status is SQLiteWorkerProjectionStatus.COMMITTED:
                return SQLiteWorkerProjectionResult(
                    reconciled.status,
                    reconciled.request_id,
                    reconciled.code,
                    True,
                )
            return reconciled


__all__ = ()
