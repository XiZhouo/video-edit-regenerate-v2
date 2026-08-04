"""Fixed-SQL schema-v8+ production writer boundary.

Pure target/receipt closure lives in ``production_atomic_write_*`` and the
fresh phase-5 reader lives in ``sqlite_production_authority``.  Keeping those
outside this module prevents SQL orchestration from becoming an alternative
issuer or authorization path.

The writer consumes every shape admitted by ``ProductionAtomicWritePlan``:
pipeline controls, durable actions and one or more outbox events.  The fixed
transaction installs the authorizer below, writes legacy mirrors and v8 closure
rows, and advances the runtime revision only after mandatory triggers can prove
the target/current/control/ledger/allocation closure.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import hmac
from typing import Any, Callable, Optional
from business_transaction_contract import AuthorityBinding
from production_atomic_write_contract import (
    ProductionAtomicWriteTestPoint,
    ProductionOutboxAllocationHead,
    ProductionWriteResult,
    ProductionWriteStatus,
)
from production_atomic_write_integrity import (
    ProductionAtomicWritePlan,
    _production_atomic_control_member_sha256,
    _production_outbox_allocation_tail,
    _require_production_atomic_write_plan,
)
from sqlite_business_runtime import (
    BusinessAuthorityState,
    BusinessRuntimeAccess,
    _validate_business_runtime_connection,
)
from sqlite_business_transaction import (
    BusinessReconcileStatus,
    SQLiteBusinessTransactionWriter,
    _HeadConflict,
    _IdentityConflict,
    _WRITABLE_TABLES as _HISTORICAL_WRITABLE_TABLES,
    _rollback_close,
    _row_is,
)
from sqlite_kernel import SqliteDatabase, _allow_authorizer
from sqlite_production_authority import (
    SQLiteProductionAuthorityError,
    _binding_from_snapshot,
    _mounted_row,
    _read_scheduler_head_bindings,
)
from sqlite_worker_durable_audit import (
    SQLiteWorkerDurableAuditStatus,
    SQLiteWorkerDurableAuditor,
)
from worker_execution_contract import (
    WORKER_EXECUTION_MANIFEST_SCHEMA,
    WORKER_EXECUTION_PROTOCOL,
    WORKER_EXECUTION_REQUEST_CLASS,
    WORKER_EXECUTION_STATUS_PREPARED,
    WorkerAttemptStart,
    WorkerBeforeState,
    WorkerClaimRequest,
    WorkerExecutionStatus,
    WorkerExpireRequest,
    WorkerLeaseToken,
    WorkerLedgerHead,
    WorkerOutboxDispatchCursor,
    WorkerOutboxDispatchFence,
    WorkerOutboxDispatchState,
    WorkerOutboxOrder,
    WorkerRenewRequest,
    WorkerRequestHeader,
    WorkerTargetKind,
)
from worker_execution_integrity import _issue_worker_execution_plan


_WORKER_MANIFEST_HASH_DOMAIN = b"ze.worker-execution.manifest-digest.v3"
_WORKER_AFTER_STATE_HASH_DOMAIN = b"ze.worker-execution.after-state.v3"
_WORKER_RECEIPT_HASH_DOMAIN = b"ze.worker-execution.receipt.v3"
_WORKER_LEDGER_TAIL_HASH_DOMAIN = b"ze.worker-ledger.tail.v1"
_ZERO_SHA256 = b"\0" * 32


def _framed_digest(domain: bytes, *parts: bytes) -> bytes:
    hasher = hashlib.sha256()
    for value in (domain, *parts):
        hasher.update(len(value).to_bytes(8, "big"))
        hasher.update(value)
    return hasher.digest()


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8", "strict")


def _decode_canonical_json(value: Any) -> Optional[dict[str, Any]]:
    if type(value) is not bytes:
        return None

    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, child in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = child
        return result

    def reject_float(_value: str) -> Any:
        raise ValueError("floats are not part of worker receipt manifests")

    try:
        decoded = json.loads(
            value.decode("utf-8", "strict"),
            object_pairs_hook=object_pairs,
            parse_float=reject_float,
            parse_constant=reject_float,
        )
        if type(decoded) is not dict or _canonical_json_bytes(decoded) != value:
            return None
        return decoded
    except (UnicodeError, ValueError, TypeError, OverflowError):
        return None


class SQLiteProductionWriterError(ValueError):
    """Stable, payload-free failure at the private production SQL boundary."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        if type(code) is not str or not code:
            raise TypeError("code must be a non-empty exact string")
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise SQLiteProductionWriterError(code)


_PRODUCTION_WRITABLE_TABLES = frozenset((
    *_HISTORICAL_WRITABLE_TABLES,
    "atomic_business_write_receipts",
    "atomic_business_control_members",
    "worker_ledger_entries",
    "worker_ledger_head",
    "pipeline_control_intents",
    "pipeline_control_receipts",
    "worker_targets",
    "worker_target_current",
    "outbox_allocations",
    "outbox_allocation_head",
))


class _ProductionWriterAuthorizer:
    """Allow only fixed data changes owned by one production transaction."""

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
                "SQLITE_ATTACH", "SQLITE_DETACH", "SQLITE_TRANSACTION",
                "SQLITE_SAVEPOINT", "SQLITE_CREATE_INDEX",
                "SQLITE_CREATE_TABLE", "SQLITE_CREATE_TEMP_INDEX",
                "SQLITE_CREATE_TEMP_TABLE", "SQLITE_CREATE_TEMP_TRIGGER",
                "SQLITE_CREATE_TEMP_VIEW", "SQLITE_CREATE_TRIGGER",
                "SQLITE_CREATE_VIEW", "SQLITE_CREATE_VTABLE",
                "SQLITE_DROP_INDEX", "SQLITE_DROP_TABLE",
                "SQLITE_DROP_TEMP_INDEX", "SQLITE_DROP_TEMP_TABLE",
                "SQLITE_DROP_TEMP_TRIGGER", "SQLITE_DROP_TEMP_VIEW",
                "SQLITE_DROP_TRIGGER", "SQLITE_DROP_VIEW",
                "SQLITE_DROP_VTABLE", "SQLITE_ALTER_TABLE",
                "SQLITE_REINDEX", "SQLITE_ANALYZE",
            )
            if hasattr(sqlite3, name)
        }
        if action in forbidden or action == getattr(sqlite3, "SQLITE_PRAGMA", -1):
            self.denied = True
            return sqlite3.SQLITE_DENY
        writes = {
            getattr(sqlite3, name)
            for name in ("SQLITE_INSERT", "SQLITE_UPDATE", "SQLITE_DELETE")
            if hasattr(sqlite3, name)
        }
        table = (arg1 or arg2 or "").lower()
        if action in writes and table not in _PRODUCTION_WRITABLE_TABLES:
            self.denied = True
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK


class _ProductionHeadConflict(RuntimeError):
    pass


class _ProductionIdentityConflict(RuntimeError):
    pass


class _ProductionClosureCorrupt(RuntimeError):
    pass


def _same(left: Any, right: Any) -> bool:
    if type(left) is bytes and type(right) is bytes:
        return hmac.compare_digest(left, right)
    return type(left) is type(right) and left == right


def _close_quietly(connection: Any) -> None:
    try:
        connection.close()
    except Exception:
        pass


class SQLiteProductionAtomicWriter:
    """Fixed production writer for every pure-plan atomic target group."""

    def __init__(self, database: SqliteDatabase) -> None:
        if type(database) is not SqliteDatabase:
            raise TypeError("database must be an exact SqliteDatabase")
        self._database = database
        self._historical = SQLiteBusinessTransactionWriter(database)

    @staticmethod
    def _require_vertical(plan: ProductionAtomicWritePlan) -> None:
        # ``_require_production_atomic_write_plan`` has already rebuilt the
        # complete target/control/allocation mapping.  Keep this seam so old
        # callers retain the same validation order without imposing a second,
        # narrower shape language in the SQL adapter.
        del plan

    @staticmethod
    def _read_allocation_head(connection: Any) -> ProductionOutboxAllocationHead:
        row = connection.execute(
            "SELECT state_revision,sequence,previous_tail_sha256,tail_sha256,"
            "updated_at FROM outbox_allocation_head WHERE singleton=1"
        ).fetchone()
        if row is None or len(row) != 5:
            raise _ProductionClosureCorrupt(
                "production_outbox_allocation_head_missing"
            )
        try:
            return ProductionOutboxAllocationHead(
                state_revision=row[0],
                sequence=row[1],
                previous_tail_sha256=row[2],
                tail_sha256=row[3],
                updated_at=row[4],
            )
        except Exception as exc:
            raise _ProductionClosureCorrupt(
                "production_outbox_allocation_head_corrupt"
            ) from exc

    @staticmethod
    def _require_planning_snapshot(
        connection: Any,
        profile: Any,
        plan: ProductionAtomicWritePlan,
    ) -> None:
        fresh = _binding_from_snapshot(
            connection,
            profile,
            _mounted_row(connection),
        )
        if fresh.evidence != plan.mounted_authority.evidence:
            stable = fresh.evidence
            planned = plan.mounted_authority.evidence
            if (
                stable.authority != planned.authority
                or stable.authority_state != planned.authority_state
                or stable.repository_epoch != planned.repository_epoch
                or stable.cutover_id != planned.cutover_id
                or stable.source_admission_revision
                != planned.source_admission_revision
                or stable.runtime_generation != planned.runtime_generation
                or not hmac.compare_digest(stable.code_digest, planned.code_digest)
                or stable.mount_transaction_id != planned.mount_transaction_id
                or not hmac.compare_digest(
                    stable.mount_manifest_sha256,
                    planned.mount_manifest_sha256,
                )
            ):
                raise _ProductionIdentityConflict(
                    "production_mount_identity_conflict"
                )
            raise _ProductionHeadConflict("production_mount_revision_conflict")
        current_head = SQLiteProductionAtomicWriter._read_allocation_head(connection)
        if current_head != plan.evidence.before_outbox_allocation:
            raise _ProductionHeadConflict(
                "production_outbox_allocation_head_conflict"
            )
        worker_head, dispatch_head = _read_scheduler_head_bindings(connection)
        if worker_head.evidence != plan.worker_ledger_head.evidence:
            raise _ProductionHeadConflict(
                "production_worker_ledger_head_conflict"
            )
        if dispatch_head.evidence != plan.outbox_dispatch_head.evidence:
            raise _ProductionHeadConflict(
                "production_outbox_dispatch_head_conflict"
            )

    @staticmethod
    def _reconcile_mount_exact(
        connection: Any,
        profile: Any,
        plan: ProductionAtomicWritePlan,
    ) -> Optional[tuple[ProductionWriteStatus, str]]:
        """Revalidate the full phase-5 seal in the reconciliation snapshot."""

        try:
            fresh = _binding_from_snapshot(
                connection,
                profile,
                _mounted_row(connection),
            ).evidence
        except SQLiteProductionAuthorityError:
            return (
                ProductionWriteStatus.CORRUPT,
                "production_mount_authorization_corrupt",
            )
        planned = plan.mounted_authority.evidence
        if (
            fresh.authority != planned.authority
            or fresh.authority_state != planned.authority_state
            or fresh.repository_epoch != planned.repository_epoch
            or fresh.cutover_id != planned.cutover_id
            or fresh.source_admission_revision
            != planned.source_admission_revision
            or fresh.runtime_generation != planned.runtime_generation
            or not hmac.compare_digest(fresh.code_digest, planned.code_digest)
            or fresh.target_schema_version != planned.target_schema_version
            or fresh.target_schema_fingerprint
            != planned.target_schema_fingerprint
            or fresh.mount_transaction_id != planned.mount_transaction_id
            or not hmac.compare_digest(
                fresh.mount_manifest_sha256,
                planned.mount_manifest_sha256,
            )
            or fresh.mounted_at != planned.mounted_at
        ):
            return (
                ProductionWriteStatus.CONFLICT,
                "production_mount_identity_conflict",
            )
        if (
            fresh.repository_revision < planned.repository_revision
            or fresh.control_revision < planned.control_revision
        ):
            return (
                ProductionWriteStatus.CORRUPT,
                "production_mount_revision_corrupt",
            )
        return None

    @staticmethod
    def _require_committed_time(
        connection: Any,
        plan: ProductionAtomicWritePlan,
    ) -> None:
        base = plan.atomic_write.atomic_plan
        row = connection.execute(
            """
            SELECT max(
                (SELECT updated_at FROM repository_runtime_control WHERE singleton=1),
                (SELECT updated_at FROM repository_projects WHERE project_id=?),
                (SELECT updated_at FROM document_commit_heads WHERE project_id=?),
                (SELECT updated_at FROM outbox_allocation_head WHERE singleton=1),
                (SELECT updated_at FROM worker_ledger_head WHERE singleton=1),
                (SELECT updated_at FROM outbox_dispatch_head WHERE singleton=1),
                (SELECT committed_at FROM business_cutover_mount_receipts
                  WHERE transaction_id=?)
            )
            """,
            (
                base.before.project_id,
                base.before.project_id,
                plan.mounted_authority.evidence.mount_transaction_id,
            ),
        ).fetchone()
        if (
            row is None
            or type(row[0]) is not str
            or plan.evidence.committed_at < row[0]
        ):
            raise _ProductionHeadConflict("production_committed_time_stale")

    @staticmethod
    def _identity_present(connection: Any, plan: ProductionAtomicWritePlan) -> bool:
        commit_id = plan.evidence.commit_id
        transaction_id = plan.expected_receipt.receipt_parent.transaction_id
        row = connection.execute(
            """
            SELECT
              EXISTS(SELECT 1 FROM transaction_receipts WHERE transaction_id=?),
              EXISTS(SELECT 1 FROM business_commits WHERE commit_id=?),
              EXISTS(SELECT 1 FROM atomic_business_write_receipts WHERE commit_id=?),
              EXISTS(SELECT 1 FROM event_outbox WHERE commit_id=?),
              EXISTS(SELECT 1 FROM outbox_allocations WHERE commit_id=?),
              EXISTS(SELECT 1 FROM worker_targets WHERE origin_commit_id=?),
              EXISTS(SELECT 1 FROM pipeline_control_intents
                       WHERE origin_commit_id=?),
              EXISTS(SELECT 1 FROM atomic_business_control_members
                       WHERE commit_id=?)
            """,
            (
                transaction_id,
                commit_id,
                commit_id,
                commit_id,
                commit_id,
                commit_id,
                commit_id,
                commit_id,
            ),
        ).fetchone()
        return row is not None and any(int(value) != 0 for value in row)

    @staticmethod
    def _pipeline_control_identity_conflict(
        connection: Any,
        plan: ProductionAtomicWritePlan,
    ) -> Optional[str]:
        """Detect every reserved control identity before the first write.

        A lone generic parent, specialised receipt, ledger member, intent or
        atomic membership row is not evidence that this business commit
        succeeded.  It is nevertheless an occupied immutable identity and
        must classify as CONFLICT rather than allowing an INSERT failure to be
        reconciled as NOT_COMMITTED.
        """

        base = plan.atomic_write.atomic_plan
        for intent, control in zip(
            base.pipeline_intents,
            plan.atomic_write.control_plans,
        ):
            receipt = control.expected_receipt
            parent = receipt.receipt_parent
            row = connection.execute(
                """
                SELECT
                  EXISTS(SELECT 1 FROM transaction_receipts
                    WHERE transaction_id=?),
                  EXISTS(SELECT 1 FROM pipeline_control_intents
                    WHERE intent_id=? OR
                          (origin_commit_id=? AND ordinal=?)),
                  EXISTS(SELECT 1 FROM pipeline_control_receipts
                    WHERE intent_id=? OR transaction_id=? OR sequence=? OR
                          ledger_tail_sha256=? OR receipt_sha256=?),
                  EXISTS(SELECT 1 FROM worker_ledger_entries
                    WHERE sequence=? OR transaction_id=? OR
                          ledger_tail_sha256=? OR receipt_sha256=?),
                  EXISTS(SELECT 1 FROM atomic_business_control_members
                    WHERE (commit_id=? AND ordinal=?) OR intent_id=? OR
                          control_transaction_id=? OR sequence=?)
                """,
                (
                    parent.transaction_id,
                    intent.intent_id,
                    plan.evidence.commit_id,
                    intent.ordinal,
                    intent.intent_id,
                    parent.transaction_id,
                    receipt.sequence,
                    receipt.ledger_tail_sha256,
                    receipt.receipt_sha256,
                    receipt.sequence,
                    parent.transaction_id,
                    receipt.ledger_tail_sha256,
                    receipt.receipt_sha256,
                    plan.evidence.commit_id,
                    intent.ordinal,
                    intent.intent_id,
                    parent.transaction_id,
                    receipt.sequence,
                ),
            ).fetchone()
            if row is None or len(row) != 5:
                raise _ProductionClosureCorrupt(
                    "production_pipeline_control_identity_unreadable"
                )
            if any(int(value) != 0 for value in row):
                return "production_pipeline_control_identity_conflict"
        return None

    def _write_historical_rows(
        self,
        connection: Any,
        plan: ProductionAtomicWritePlan,
    ) -> None:
        base = plan.atomic_write.atomic_plan
        committed_at = plan.evidence.committed_at
        writer = self._historical
        writer._write_current_snapshot(connection, base)
        writer._write_receipt_and_commit(connection, base, committed_at)
        writer._write_operations(connection, base)
        writer._write_outbox(connection, base, committed_at)
        writer._write_actions(connection, base, committed_at)
        writer._write_pipelines(connection, base, committed_at)
        writer._write_asset_references(connection, base)
        writer._consume_inbox(connection, base, committed_at)
        writer._consume_confirmation(connection, base, committed_at)

    @staticmethod
    def _write_atomic_receipt(
        connection: Any,
        plan: ProductionAtomicWritePlan,
    ) -> None:
        inner = plan.atomic_write.expected_receipt
        outer = plan.expected_receipt
        evidence = plan.evidence
        connection.execute(
            """
            INSERT INTO atomic_business_write_receipts(
                commit_id,transaction_id,receipt_kind,committed_at,
                atomic_manifest_sha256,pipeline_intents_sha256,controls_sha256,
                control_count,before_worker_sequence,before_worker_tail_sha256,
                after_worker_sequence,after_worker_tail_sha256,
                manifest_bytes,manifest_sha256,receipt_sha256,
                production_manifest_bytes,production_manifest_sha256,
                target_count,targets_sha256,production_receipt_sha256
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                inner.commit_id,
                inner.receipt_parent.transaction_id,
                inner.receipt_parent.kind,
                inner.committed_at,
                inner.atomic_manifest_sha256,
                inner.pipeline_intents_sha256,
                inner.controls_sha256,
                inner.control_count,
                inner.before_worker_ledger.sequence,
                inner.before_worker_ledger.tail_sha256,
                inner.after_worker_ledger.sequence,
                inner.after_worker_ledger.tail_sha256,
                plan.atomic_write.evidence.manifest_bytes,
                inner.manifest_sha256,
                inner.receipt_sha256,
                evidence.production_manifest_bytes,
                outer.production_manifest_sha256,
                outer.target_count,
                outer.targets_sha256,
                outer.production_receipt_sha256,
            ),
        )

    @staticmethod
    def _write_allocations(
        connection: Any,
        plan: ProductionAtomicWritePlan,
    ) -> None:
        for allocation in plan.evidence.allocations:
            connection.execute(
                """
                INSERT INTO outbox_allocations(
                    sequence,event_id,commit_id,repository_revision,ordinal,
                    previous_tail_sha256,allocation_tail_sha256,allocated_at
                ) VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    allocation.sequence,
                    allocation.event_id,
                    allocation.commit_id,
                    allocation.repository_revision,
                    allocation.ordinal,
                    allocation.previous_tail_sha256,
                    allocation.allocation_tail_sha256,
                    allocation.allocated_at,
                ),
            )

    @staticmethod
    def _advance_allocation_head(
        connection: Any,
        plan: ProductionAtomicWritePlan,
    ) -> None:
        if not plan.evidence.allocations:
            return
        before = plan.evidence.before_outbox_allocation
        after = plan.evidence.after_outbox_allocation
        changed = connection.execute(
            """
            UPDATE outbox_allocation_head
            SET state_revision=?,sequence=?,previous_tail_sha256=?,
                tail_sha256=?,updated_at=?
            WHERE singleton=1 AND state_revision=? AND sequence=? AND
                  previous_tail_sha256=? AND tail_sha256=? AND updated_at=?
            """,
            (
                after.state_revision,
                after.sequence,
                after.previous_tail_sha256,
                after.tail_sha256,
                after.updated_at,
                before.state_revision,
                before.sequence,
                before.previous_tail_sha256,
                before.tail_sha256,
                before.updated_at,
            ),
        ).rowcount
        if changed != 1:
            raise _ProductionHeadConflict(
                "production_outbox_allocation_head_cas_failed"
            )

    @staticmethod
    def _write_target(
        connection: Any,
        plan: ProductionAtomicWritePlan,
        wrapped: Any,
    ) -> None:
        target = wrapped.target
        order = target.outbox_order
        connection.execute(
            """
            INSERT INTO worker_targets(
                target_id,target_kind,genesis_status,genesis_state_sha256,
                project_id,generation,document_id,origin_commit_id,
                source_ordinal,payload_schema,payload_codec_id,
                payload_codec_version,payload_bytes,payload_sha256,
                idempotency_scope,idempotency_key,replay_policy,
                idempotency_policy,max_attempts,action_type,pipeline_id,
                intent_type,outbox_topic,outbox_partition_key,outbox_sequence,
                outbox_repository_revision,outbox_ordinal,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                target.target_id,
                target.target_kind.value,
                wrapped.genesis_status.value,
                wrapped.genesis_state_sha256,
                target.project_id,
                target.generation,
                target.document_id,
                target.origin_commit_id,
                wrapped.source_ordinal,
                target.payload.schema,
                target.payload.codec_id,
                target.payload.codec_version,
                target.payload.payload_bytes,
                target.payload.payload_sha256,
                target.idempotency_scope,
                target.idempotency_key,
                target.replay_policy.value,
                target.idempotency_policy.value,
                target.max_attempts,
                wrapped.action_type,
                target.pipeline_id,
                target.intent_type,
                wrapped.outbox_topic,
                wrapped.outbox_partition_key,
                None if order is None else order.sequence,
                None if order is None else order.repository_revision,
                None if order is None else order.ordinal,
                plan.evidence.committed_at,
            ),
        )

    @staticmethod
    def _insert_genesis_current(
        connection: Any,
        plan: ProductionAtomicWritePlan,
        wrapped: Any,
        receipt_transaction_id: str,
    ) -> None:
        target = wrapped.target
        connection.execute(
            """
            INSERT INTO worker_target_current(
                target_id,target_kind,status,state_revision,attempt_count,
                last_attempt_id,last_attempt_number,current_attempt_id,
                current_attempt_number,fence_high_water,cancellation_pending,
                next_attempt_at,lease_owner,lease_fence,lease_expires_at,
                current_effect_id,unresolved_effect_id,
                last_receipt_transaction_id,previous_state_sha256,
                state_sha256,updated_at
            ) VALUES (
                ?,?,?,1,0,NULL,NULL,NULL,NULL,0,0,NULL,NULL,NULL,NULL,
                NULL,NULL,?,?,?,?
            )
            """,
            (
                target.target_id,
                target.target_kind.value,
                wrapped.genesis_status.value,
                receipt_transaction_id,
                b"\0" * 32,
                wrapped.genesis_state_sha256,
                plan.evidence.committed_at,
            ),
        )

    @staticmethod
    def _write_pipeline_intent(
        connection: Any,
        plan: ProductionAtomicWritePlan,
        intent: Any,
        control: Any,
    ) -> None:
        binding = control.control_intent
        closure = control.closure
        connection.execute(
            """
            INSERT INTO pipeline_control_intents(
                intent_id,origin_commit_id,ordinal,target_id,target_kind,
                operation,intent_type,idempotency_key,payload_schema,
                payload_codec_id,payload_codec_version,payload_bytes,
                payload_sha256,replay_policy,idempotency_policy,max_attempts,
                before_status,after_status,before_state_revision,
                after_state_revision,request_sha256,committed_at
            ) VALUES (?,?,?,?,'pipeline',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                intent.intent_id,
                plan.evidence.commit_id,
                intent.ordinal,
                control.target.target_id,
                control.operation.value,
                binding.intent_type,
                binding.idempotency_key,
                binding.payload.schema,
                binding.payload.codec_id,
                binding.payload.codec_version,
                binding.payload.payload_bytes,
                binding.payload.payload_sha256,
                binding.replay_policy.value,
                binding.idempotency_policy.value,
                binding.max_attempts,
                None if closure.before_status is None else closure.before_status.value,
                closure.after_status.value,
                closure.before_state_revision,
                closure.after_state_revision,
                closure.request_sha256,
                plan.evidence.committed_at,
            ),
        )

    @staticmethod
    def _write_pipeline_control_receipt(
        connection: Any,
        plan: ProductionAtomicWritePlan,
        intent: Any,
        control: Any,
    ) -> None:
        receipt = control.expected_receipt
        parent = receipt.receipt_parent
        closure = control.closure
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
            ) VALUES (?,'pipeline_control',?,?,?,?,?,?,?,?,?)
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
            INSERT INTO pipeline_control_receipts(
                intent_id,transaction_id,entry_kind,receipt_kind,committed_at,
                operation,intent_type,target_id,before_status,after_status,
                before_state_revision,after_state_revision,sequence,
                previous_ledger_tail_sha256,ledger_tail_sha256,manifest_bytes,
                request_sha256,after_state_sha256,receipt_sha256
            ) VALUES (?,?,'pipeline_control',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                intent.intent_id,
                parent.transaction_id,
                parent.kind,
                parent.committed_at,
                control.operation.value,
                control.control_intent.intent_type,
                control.target.target_id,
                None if closure.before_status is None else closure.before_status.value,
                closure.after_status.value,
                closure.before_state_revision,
                closure.after_state_revision,
                receipt.sequence,
                receipt.previous_ledger_tail_sha256,
                receipt.ledger_tail_sha256,
                closure.manifest_bytes,
                receipt.request_sha256,
                receipt.after_state_sha256,
                receipt.receipt_sha256,
            ),
        )
        binding_sha256 = _production_atomic_control_member_sha256(
            commit_id=plan.evidence.commit_id,
            ordinal=intent.ordinal,
            intent_id=intent.intent_id,
            control_transaction_id=parent.transaction_id,
            sequence=receipt.sequence,
            ledger_tail_sha256=receipt.ledger_tail_sha256,
        )
        connection.execute(
            """
            INSERT INTO atomic_business_control_members(
                commit_id,ordinal,intent_id,control_transaction_id,sequence,
                ledger_tail_sha256,binding_sha256
            ) VALUES (?,?,?,?,?,?,?)
            """,
            (
                plan.evidence.commit_id,
                intent.ordinal,
                intent.intent_id,
                parent.transaction_id,
                receipt.sequence,
                receipt.ledger_tail_sha256,
                binding_sha256,
            ),
        )

    @staticmethod
    def _update_pipeline_current(
        connection: Any,
        control: Any,
    ) -> None:
        before = control.request.before
        if before is None:
            raise _ProductionClosureCorrupt("production_pipeline_before_missing")
        prior_rows = connection.execute(
            """
            SELECT state_sha256 FROM (
                SELECT genesis_state_sha256 AS state_sha256,1 AS revision
                FROM worker_targets WHERE target_id=?
                UNION ALL
                SELECT after_state_sha256,after_state_revision
                FROM worker_execution_receipts WHERE target_id=?
                UNION ALL
                SELECT after_state_sha256,after_state_revision
                FROM pipeline_control_receipts WHERE target_id=?
            ) WHERE revision=?
            """,
            (
                control.target.target_id,
                control.target.target_id,
                control.target.target_id,
                before.state_revision,
            ),
        ).fetchall()
        if (
            not prior_rows
            or type(prior_rows[0][0]) is not bytes
            or any(not _same(row[0], prior_rows[0][0]) for row in prior_rows)
        ):
            raise _ProductionClosureCorrupt("production_pipeline_history_corrupt")
        prior_state_sha256 = prior_rows[0][0]
        previous_state_sha256 = (
            _ZERO_SHA256
            if before.state_revision == 1
            else SQLiteProductionAtomicWriter._prior_state_sha256(
                connection,
                control.target.target_id,
                before.state_revision - 1,
            )
        )
        if previous_state_sha256 is None:
            raise _ProductionClosureCorrupt("production_pipeline_history_corrupt")
        after = control.closure.after_state
        receipt = control.expected_receipt
        changed = connection.execute(
            """
            UPDATE worker_target_current
            SET status=?,state_revision=?,attempt_count=?,last_attempt_id=?,
                last_attempt_number=?,current_attempt_id=?,
                current_attempt_number=?,fence_high_water=?,
                cancellation_pending=?,next_attempt_at=?,lease_owner=?,
                lease_fence=?,lease_expires_at=?,current_effect_id=?,
                unresolved_effect_id=?,last_receipt_transaction_id=?,
                previous_state_sha256=state_sha256,state_sha256=?,updated_at=?
            WHERE target_id=? AND target_kind='pipeline' AND status=? AND
                  state_revision=? AND attempt_count=? AND last_attempt_id IS ? AND
                  last_attempt_number IS ? AND current_attempt_id IS ? AND
                  current_attempt_number IS ? AND fence_high_water=? AND
                  cancellation_pending=? AND next_attempt_at IS ? AND
                  lease_owner IS ? AND lease_fence IS ? AND lease_expires_at IS ? AND
                  current_effect_id IS ? AND unresolved_effect_id IS ? AND
                  previous_state_sha256=? AND state_sha256=?
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
                None if after.unresolved_effect is None else after.unresolved_effect.effect_id,
                receipt.receipt_parent.transaction_id,
                receipt.after_state_sha256,
                receipt.occurred_at,
                control.target.target_id,
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
                None if before.unresolved_effect is None else before.unresolved_effect.effect_id,
                previous_state_sha256,
                prior_state_sha256,
            ),
        ).rowcount
        if changed != 1:
            raise _ProductionHeadConflict("production_pipeline_current_cas_failed")

    @staticmethod
    def _advance_worker_ledger_head(
        connection: Any,
        plan: ProductionAtomicWritePlan,
    ) -> None:
        controls = plan.atomic_write.control_plans
        if not controls:
            return
        before = plan.worker_ledger_head.evidence
        after = plan.atomic_write.evidence.after_worker_ledger
        changed = connection.execute(
            """
            UPDATE worker_ledger_head
            SET state_revision=?,sequence=?,previous_tail_sha256=?,
                tail_sha256=?,updated_at=?
            WHERE singleton=1 AND state_revision=? AND sequence=? AND
                  previous_tail_sha256=? AND tail_sha256=? AND updated_at=?
            """,
            (
                before.state_revision + 1,
                after.sequence,
                before.ledger_head.tail_sha256,
                after.tail_sha256,
                plan.evidence.committed_at,
                before.state_revision,
                before.ledger_head.sequence,
                before.previous_tail_sha256,
                before.ledger_head.tail_sha256,
                before.updated_at,
            ),
        ).rowcount
        if changed != 1:
            raise _ProductionHeadConflict("production_worker_ledger_head_cas_failed")

    def _write_transaction(
        self,
        connection: Any,
        plan: ProductionAtomicWritePlan,
    ) -> None:
        self._write_historical_rows(connection, plan)
        self._write_atomic_receipt(connection, plan)
        self._write_allocations(connection, plan)
        for wrapped in plan.targets:
            self._write_target(connection, plan, wrapped)

        atomic = plan.atomic_write
        base = atomic.atomic_plan
        for intent, control in zip(base.pipeline_intents, atomic.control_plans):
            self._write_pipeline_intent(connection, plan, intent, control)
        for intent, control in zip(base.pipeline_intents, atomic.control_plans):
            self._write_pipeline_control_receipt(connection, plan, intent, control)
            if control.request.before is None:
                wrapped = next(
                    value for value in plan.targets
                    if value.target.target_id == control.target.target_id
                )
                self._insert_genesis_current(
                    connection,
                    plan,
                    wrapped,
                    control.expected_receipt.receipt_parent.transaction_id,
                )
            else:
                self._update_pipeline_current(connection, control)

        for wrapped in plan.targets:
            if wrapped.target.target_kind is WorkerTargetKind.PIPELINE:
                continue
            self._insert_genesis_current(
                connection,
                plan,
                wrapped,
                plan.expected_receipt.receipt_parent.transaction_id,
            )
        self._advance_allocation_head(connection, plan)
        self._advance_worker_ledger_head(connection, plan)
        # Runtime revision is the final durable publication point. Its v8
        # trigger proves the production receipt, target/current and allocation
        # closure before accepting this CAS.
        self._historical._advance_heads(
            connection,
            plan.atomic_write.atomic_plan,
            plan.evidence.committed_at,
        )

    @staticmethod
    def _atomic_receipt_row(connection: Any, commit_id: str) -> Any:
        return connection.execute(
            """
            SELECT transaction_id,receipt_kind,committed_at,
                   atomic_manifest_sha256,pipeline_intents_sha256,
                   controls_sha256,control_count,before_worker_sequence,
                   before_worker_tail_sha256,after_worker_sequence,
                   after_worker_tail_sha256,manifest_bytes,manifest_sha256,
                   receipt_sha256,production_manifest_bytes,
                   production_manifest_sha256,target_count,targets_sha256,
                   production_receipt_sha256
            FROM atomic_business_write_receipts WHERE commit_id=?
            """,
            (commit_id,),
        ).fetchone()

    @staticmethod
    def _atomic_receipt_expected(plan: ProductionAtomicWritePlan) -> tuple[Any, ...]:
        inner = plan.atomic_write.expected_receipt
        outer = plan.expected_receipt
        return (
            inner.receipt_parent.transaction_id,
            inner.receipt_parent.kind,
            inner.committed_at,
            inner.atomic_manifest_sha256,
            inner.pipeline_intents_sha256,
            inner.controls_sha256,
            inner.control_count,
            inner.before_worker_ledger.sequence,
            inner.before_worker_ledger.tail_sha256,
            inner.after_worker_ledger.sequence,
            inner.after_worker_ledger.tail_sha256,
            plan.atomic_write.evidence.manifest_bytes,
            inner.manifest_sha256,
            inner.receipt_sha256,
            plan.evidence.production_manifest_bytes,
            outer.production_manifest_sha256,
            outer.target_count,
            outer.targets_sha256,
            outer.production_receipt_sha256,
        )

    @staticmethod
    def _target_row(connection: Any, target_id: str) -> Any:
        return connection.execute(
            """
            SELECT target_kind,genesis_status,genesis_state_sha256,project_id,
                   generation,document_id,origin_commit_id,source_ordinal,
                   payload_schema,payload_codec_id,payload_codec_version,
                   payload_bytes,payload_sha256,idempotency_scope,
                   idempotency_key,replay_policy,idempotency_policy,max_attempts,
                   action_type,pipeline_id,intent_type,outbox_topic,
                   outbox_partition_key,outbox_sequence,
                   outbox_repository_revision,outbox_ordinal,created_at
            FROM worker_targets WHERE target_id=?
            """,
            (target_id,),
        ).fetchone()

    @staticmethod
    def _target_expected(
        plan: ProductionAtomicWritePlan,
        wrapped: Any,
    ) -> tuple[Any, ...]:
        target = wrapped.target
        order = target.outbox_order
        return (
            target.target_kind.value,
            wrapped.genesis_status.value,
            wrapped.genesis_state_sha256,
            target.project_id,
            target.generation,
            target.document_id,
            target.origin_commit_id,
            wrapped.source_ordinal,
            target.payload.schema,
            target.payload.codec_id,
            target.payload.codec_version,
            target.payload.payload_bytes,
            target.payload.payload_sha256,
            target.idempotency_scope,
            target.idempotency_key,
            target.replay_policy.value,
            target.idempotency_policy.value,
            target.max_attempts,
            wrapped.action_type,
            target.pipeline_id,
            target.intent_type,
            wrapped.outbox_topic,
            wrapped.outbox_partition_key,
            None if order is None else order.sequence,
            None if order is None else order.repository_revision,
            None if order is None else order.ordinal,
            plan.evidence.committed_at,
        )

    @staticmethod
    def _current_row(connection: Any, target_id: str) -> Any:
        return connection.execute(
            """
            SELECT target_kind,status,state_revision,attempt_count,
                   last_attempt_id,last_attempt_number,current_attempt_id,
                   current_attempt_number,fence_high_water,
                   cancellation_pending,next_attempt_at,lease_owner,
                   lease_fence,lease_expires_at,current_effect_id,
                   unresolved_effect_id,last_receipt_transaction_id,
                   previous_state_sha256,state_sha256,updated_at
            FROM worker_target_current WHERE target_id=?
            """,
            (target_id,),
        ).fetchone()

    @staticmethod
    def _current_expected(
        plan: ProductionAtomicWritePlan,
        wrapped: Any,
        receipt_transaction_id: str,
    ) -> tuple[Any, ...]:
        return (
            wrapped.target.target_kind.value,
            wrapped.genesis_status.value,
            1,
            0,
            None,
            None,
            None,
            None,
            0,
            0,
            None,
            None,
            None,
            None,
            None,
            None,
            receipt_transaction_id,
            b"\0" * 32,
            wrapped.genesis_state_sha256,
            plan.evidence.committed_at,
        )

    @staticmethod
    def _worker_receipt_rows(
        connection: Any,
        target_id: Optional[str],
    ) -> tuple[Any, ...]:
        return tuple(connection.execute(
            """
            SELECT
                receipt.request_id AS request_id,
                receipt.transaction_id AS transaction_id,
                receipt.entry_kind AS entry_kind,
                receipt.receipt_kind AS receipt_kind,
                receipt.committed_at AS committed_at,
                receipt.operation AS operation,
                receipt.target_kind AS target_kind,
                receipt.target_id AS target_id,
                receipt.before_status AS before_status,
                receipt.after_status AS after_status,
                receipt.before_state_revision AS before_state_revision,
                receipt.after_state_revision AS after_state_revision,
                receipt.sequence AS sequence,
                receipt.previous_ledger_tail_sha256 AS previous_ledger_tail_sha256,
                receipt.ledger_tail_sha256 AS ledger_tail_sha256,
                receipt.manifest_bytes AS manifest_bytes,
                receipt.request_sha256 AS request_sha256,
                receipt.after_state_sha256 AS after_state_sha256,
                receipt.receipt_sha256 AS receipt_sha256,
                parent.kind AS parent_kind,
                parent.committed_at AS parent_committed_at,
                ledger.entry_kind AS ledger_entry_kind,
                ledger.transaction_id AS ledger_transaction_id,
                ledger.receipt_kind AS ledger_receipt_kind,
                ledger.committed_at AS ledger_committed_at,
                ledger.previous_tail_sha256 AS ledger_previous_tail_sha256,
                ledger.ledger_tail_sha256 AS ledger_entry_tail_sha256,
                ledger.request_sha256 AS ledger_request_sha256,
                ledger.after_state_sha256 AS ledger_after_state_sha256,
                ledger.receipt_sha256 AS ledger_receipt_sha256,
                ledger.occurred_at AS ledger_occurred_at
            FROM worker_execution_receipts AS receipt
            LEFT JOIN transaction_receipts AS parent
              ON parent.transaction_id = receipt.transaction_id
            LEFT JOIN worker_ledger_entries AS ledger
              ON ledger.sequence = receipt.sequence
            WHERE (? IS NULL OR receipt.target_id=?)
            ORDER BY receipt.sequence
            """,
            (target_id, target_id),
        ).fetchall())

    @staticmethod
    def _worker_receipt_manifest(row: Any) -> Optional[dict[str, Any]]:
        if (
            row["transaction_id"] != "_worker-execution:" + row["request_id"]
            or row["entry_kind"] != "worker_execution"
            or row["receipt_kind"] != "_worker_execution_v3"
            or row["parent_kind"] != row["receipt_kind"]
            or row["parent_committed_at"] != row["committed_at"]
            or row["ledger_entry_kind"] != row["entry_kind"]
            or row["ledger_transaction_id"] != row["transaction_id"]
            or row["ledger_receipt_kind"] != row["receipt_kind"]
            or row["ledger_committed_at"] != row["committed_at"]
            or not _same(
                row["ledger_previous_tail_sha256"],
                row["previous_ledger_tail_sha256"],
            )
            or not _same(
                row["ledger_entry_tail_sha256"],
                row["ledger_tail_sha256"],
            )
            or not _same(row["ledger_request_sha256"], row["request_sha256"])
            or not _same(
                row["ledger_after_state_sha256"],
                row["after_state_sha256"],
            )
            or not _same(row["ledger_receipt_sha256"], row["receipt_sha256"])
            or row["ledger_occurred_at"] != row["committed_at"]
        ):
            return None
        manifest = _decode_canonical_json(row["manifest_bytes"])
        if manifest is None:
            return None
        if (
            manifest.get("schema") != WORKER_EXECUTION_MANIFEST_SCHEMA
            or manifest.get("protocol") != WORKER_EXECUTION_PROTOCOL
            or manifest.get("status") != WORKER_EXECUTION_STATUS_PREPARED
            or manifest.get("operation") != row["operation"]
            or type(manifest.get("header")) is not dict
            or type(manifest.get("transition")) is not dict
            or type(manifest.get("after")) is not dict
            or type(manifest.get("request")) is not dict
        ):
            return None
        header = manifest["header"]
        transition = manifest["transition"]
        if (
            header.get("request_transaction_id") != row["request_id"]
            or header.get("occurred_at") != row["committed_at"]
            or type(header.get("target")) is not dict
            or type(header.get("before")) is not dict
            or header["target"].get("kind") != row["target_kind"]
            or header["target"].get("id") != row["target_id"]
            or header["before"].get("status") != row["before_status"]
            or header["before"].get("state_revision")
            != row["before_state_revision"]
            or transition != {
                "after_state_revision": row["after_state_revision"],
                "after_status": row["after_status"],
                "before_state_revision": row["before_state_revision"],
                "before_status": row["before_status"],
            }
            or manifest["after"].get("status") != row["after_status"]
            or manifest["after"].get("state_revision")
            != row["after_state_revision"]
        ):
            return None
        request_sha256 = _framed_digest(
            _WORKER_MANIFEST_HASH_DOMAIN,
            row["manifest_bytes"],
        )
        request = manifest["request"]
        dispatch = request.get("dispatch_fence")
        if dispatch is not None and type(dispatch) is not dict:
            return None
        dispatch_after = None if dispatch is None else dispatch.get("after")
        after_state_sha256 = _framed_digest(
            _WORKER_AFTER_STATE_HASH_DOMAIN,
            _canonical_json_bytes({
                "dispatch_after": dispatch_after,
                "worker_after": manifest["after"],
            }),
        )
        receipt_manifest = {
            "after_state_revision": row["after_state_revision"],
            "after_state_sha256": after_state_sha256.hex(),
            "after_status": row["after_status"],
            "before_state_revision": row["before_state_revision"],
            "before_status": row["before_status"],
            "occurred_at": row["committed_at"],
            "operation": row["operation"],
            "previous_ledger_tail_sha256": row[
                "previous_ledger_tail_sha256"
            ].hex(),
            "request_class": WORKER_EXECUTION_REQUEST_CLASS,
            "request_sha256": request_sha256.hex(),
            "request_transaction_id": row["request_id"],
            "sequence": row["sequence"],
            "target_id": row["target_id"],
            "target_kind": row["target_kind"],
        }
        receipt_sha256 = _framed_digest(
            _WORKER_RECEIPT_HASH_DOMAIN,
            _canonical_json_bytes(receipt_manifest),
        )
        ledger_tail_sha256 = _framed_digest(
            _WORKER_LEDGER_TAIL_HASH_DOMAIN,
            row["sequence"].to_bytes(8, "big"),
            row["previous_ledger_tail_sha256"],
            receipt_sha256,
        )
        if (
            not _same(request_sha256, row["request_sha256"])
            or not _same(after_state_sha256, row["after_state_sha256"])
            or not _same(receipt_sha256, row["receipt_sha256"])
            or not _same(ledger_tail_sha256, row["ledger_tail_sha256"])
        ):
            return None
        return manifest

    @staticmethod
    def _manifest_target_exact(
        manifest: dict[str, Any],
        wrapped: Any,
    ) -> bool:
        target = wrapped.target
        order = target.outbox_order
        value = manifest["header"]["target"]
        if order is None:
            return False
        return value == {
            "document_id": target.document_id,
            "generation": target.generation,
            "id": target.target_id,
            "idempotency_key": target.idempotency_key,
            "idempotency_policy": target.idempotency_policy.value,
            "idempotency_scope": target.idempotency_scope,
            "intent_type": None,
            "kind": target.target_kind.value,
            "max_attempts": target.max_attempts,
            "origin_commit_id": target.origin_commit_id,
            "outbox_order": {
                "ordinal": order.ordinal,
                "repository_revision": order.repository_revision,
                "sequence": order.sequence,
            },
            "payload": {
                "bytes": len(target.payload.payload_bytes),
                "codec_id": target.payload.codec_id,
                "codec_version": target.payload.codec_version,
                "schema": target.payload.schema,
                "sha256": target.payload.payload_sha256.hex(),
            },
            "pipeline_id": None,
            "project_id": target.project_id,
            "replay_policy": target.replay_policy.value,
        }

    @staticmethod
    def _dispatch_fence_from_manifest(value: Any) -> WorkerOutboxDispatchFence:
        if type(value) is not dict or set(value) != {"before", "after"}:
            raise ValueError("worker dispatch fence is incomplete")

        def cursor(raw: Any) -> WorkerOutboxDispatchCursor:
            if type(raw) is not dict:
                raise ValueError("worker dispatch cursor is incomplete")
            return WorkerOutboxDispatchCursor(
                raw["sequence"],
                raw["repository_revision"],
                raw["ordinal"],
            )

        def state(raw: Any) -> WorkerOutboxDispatchState:
            if type(raw) is not dict:
                raise ValueError("worker dispatch state is incomplete")
            inflight = raw["inflight"]
            return WorkerOutboxDispatchState(
                raw["control_revision"],
                cursor(raw["published"]),
                (
                    None
                    if inflight is None
                    else WorkerOutboxOrder(
                        inflight["sequence"],
                        inflight["repository_revision"],
                        inflight["ordinal"],
                    )
                ),
            )

        return WorkerOutboxDispatchFence(
            state(value["before"]),
            state(value["after"]),
        )

    @staticmethod
    def _worker_before_from_manifest(value: Any) -> WorkerBeforeState:
        if (
            type(value) is not dict
            or value.get("current_effect") is not None
            or value.get("unresolved_effect") is not None
        ):
            # Effect request bytes are not persisted by schema-v8, so an
            # effect-bearing state cannot be faithfully rebuilt yet.
            raise ValueError("worker effect projection is not reconstructable")
        return WorkerBeforeState(
            status=WorkerExecutionStatus(value["status"]),
            state_revision=value["state_revision"],
            attempt_count=value["attempt_count"],
            last_attempt_id=value["last_attempt_id"],
            last_attempt_number=value["last_attempt_number"],
            current_attempt_id=value["current_attempt_id"],
            current_attempt_number=value["current_attempt_number"],
            lease_owner=value["lease_owner"],
            lease_fence=value["lease_fence"],
            lease_expires_at=value["lease_expires_at"],
            fence_high_water=value["fence_high_water"],
            next_attempt_at=value["next_attempt_at"],
            cancellation_pending=value["cancellation_pending"],
        )

    @staticmethod
    def _worker_authority_from_manifest(
        value: Any,
        plan: ProductionAtomicWritePlan,
    ) -> AuthorityBinding:
        if type(value) is not dict:
            raise ValueError("worker authority is incomplete")
        evidence = plan.mounted_authority.evidence
        authority = AuthorityBinding(
            authority=value["authority"],
            authority_state=value["authority_state"],
            authority_epoch=value["epoch"],
            source_admission_revision=value["source_admission_revision"],
            authority_revision=value["revision"],
            control_revision=value["control_revision"],
            cutover_id=value["cutover_id"],
            runtime_generation=value["runtime_generation"],
            code_digest=bytes.fromhex(value["code_digest"]),
        )
        if (
            authority.authority != evidence.authority
            or authority.authority_state != evidence.authority_state
            or authority.authority_epoch != evidence.repository_epoch
            or authority.source_admission_revision
            != evidence.source_admission_revision
            or authority.cutover_id != evidence.cutover_id
            or authority.runtime_generation != evidence.runtime_generation
            or not _same(authority.code_digest, evidence.code_digest)
            or authority.authority_revision
            < plan.atomic_write.atomic_plan.candidate.authority_revision
        ):
            raise ValueError("worker authority does not descend from the mount")
        return authority

    @classmethod
    def _typed_worker_receipt_exact(
        cls,
        row: Any,
        manifest: dict[str, Any],
        plan: ProductionAtomicWritePlan,
        wrapped: Any,
    ) -> bool:
        """Reissue reconstructable operations through the frozen state machine."""

        try:
            header_value = manifest["header"]
            request_value = manifest["request"]
            before = cls._worker_before_from_manifest(header_value["before"])
            header = WorkerRequestHeader(
                row["request_id"],
                cls._worker_authority_from_manifest(
                    header_value["authority"],
                    plan,
                ),
                wrapped.target,
                before,
                row["committed_at"],
            )
            operation = row["operation"]
            if operation == "claim":
                request = WorkerClaimRequest(
                    header,
                    WorkerAttemptStart(
                        request_value["attempt_id"],
                        request_value["attempt_number"],
                        request_value["previous_attempt_id"],
                        request_value["previous_attempt_number"],
                        request_value["lease_owner"],
                        request_value["execution_fence"],
                        request_value["started_at"],
                        request_value["lease_expires_at"],
                    ),
                    cls._dispatch_fence_from_manifest(
                        request_value["dispatch_fence"]
                    ),
                )
            elif operation == "renew":
                lease = request_value["lease"]
                request = WorkerRenewRequest(
                    header,
                    WorkerLeaseToken(
                        lease["attempt_id"],
                        lease["attempt_number"],
                        lease["lease_owner"],
                        lease["execution_fence"],
                        lease["lease_expires_at"],
                    ),
                    request_value["new_lease_expires_at"],
                )
            elif operation == "expire":
                lease = request_value["lease"]
                request = WorkerExpireRequest(
                    header,
                    WorkerLeaseToken(
                        lease["attempt_id"],
                        lease["attempt_number"],
                        lease["lease_owner"],
                        lease["execution_fence"],
                        lease["lease_expires_at"],
                    ),
                    request_value["expired_at"],
                    WorkerExecutionStatus(request_value["target_status"]),
                    request_value["next_attempt_at"],
                    cls._dispatch_fence_from_manifest(
                        request_value["dispatch_fence"]
                    ),
                )
            else:
                # AUTHORIZE_EFFECT omits request bytes from durable SQL;
                # COMPLETE/RECONCILE have no frozen result-row projection.
                return False
            rebuilt = _issue_worker_execution_plan(
                request,
                before_ledger=WorkerLedgerHead(
                    row["sequence"] - 1,
                    row["previous_ledger_tail_sha256"],
                ),
            )
            receipt = rebuilt.expected_receipt
            parent = receipt.receipt_parent
            return bool(
                rebuilt.closure.manifest_bytes == row["manifest_bytes"]
                and receipt.request_transaction_id == row["request_id"]
                and parent.transaction_id == row["transaction_id"]
                and parent.kind == row["receipt_kind"]
                and parent.committed_at == row["committed_at"]
                and receipt.operation.value == row["operation"]
                and receipt.target_kind.value == row["target_kind"]
                and receipt.target_id == row["target_id"]
                and receipt.before_status.value == row["before_status"]
                and receipt.after_status.value == row["after_status"]
                and receipt.before_state_revision == row["before_state_revision"]
                and receipt.after_state_revision == row["after_state_revision"]
                and receipt.sequence == row["sequence"]
                and _same(
                    receipt.previous_ledger_tail_sha256,
                    row["previous_ledger_tail_sha256"],
                )
                and _same(receipt.ledger_tail_sha256, row["ledger_tail_sha256"])
                and _same(receipt.request_sha256, row["request_sha256"])
                and _same(
                    receipt.after_state_sha256,
                    row["after_state_sha256"],
                )
                and _same(receipt.receipt_sha256, row["receipt_sha256"])
            )
        except (
            AttributeError,
            KeyError,
            TypeError,
            ValueError,
            OverflowError,
        ):
            return False

    @staticmethod
    def _worker_state_core(value: Any) -> Optional[dict[str, Any]]:
        if type(value) is not dict:
            return None
        keys = (
            "status", "state_revision", "attempt_count", "last_attempt_id",
            "last_attempt_number", "current_attempt_id",
            "current_attempt_number", "lease_owner", "lease_fence",
            "lease_expires_at", "fence_high_water", "current_effect",
            "unresolved_effect", "next_attempt_at", "cancellation_pending",
        )
        if any(key not in value for key in keys):
            return None
        return {key: value[key] for key in keys}

    @staticmethod
    def _current_manifest_exact(current: Any, manifest: dict[str, Any]) -> bool:
        after = manifest["after"]
        if set(after) != {
            "status", "state_revision", "attempt_count", "last_attempt_id",
            "last_attempt_number", "current_attempt_id",
            "current_attempt_number", "lease_owner", "lease_fence",
            "lease_expires_at", "fence_high_water", "current_effect",
            "unresolved_effect", "next_attempt_at", "cancellation_pending",
            "result_effect_link", "durable_result",
        }:
            return False
        current_effect = after["current_effect"]
        unresolved_effect = after["unresolved_effect"]
        if (
            (current_effect is None) != (current[14] is None)
            or (unresolved_effect is None) != (current[15] is None)
            or (
                current_effect is not None
                and (
                    type(current_effect) is not dict
                    or current_effect.get("effect_id") != current[14]
                )
            )
            or (
                unresolved_effect is not None
                and (
                    type(unresolved_effect) is not dict
                    or unresolved_effect.get("effect_id") != current[15]
                )
            )
        ):
            return False
        return SQLiteProductionAtomicWriter._worker_state_core(after) == {
            "status": current[1],
            "state_revision": current[2],
            "attempt_count": current[3],
            "last_attempt_id": current[4],
            "last_attempt_number": current[5],
            "current_attempt_id": current[6],
            "current_attempt_number": current[7],
            "lease_owner": current[11],
            "lease_fence": current[12],
            "lease_expires_at": current[13],
            "fence_high_water": current[8],
            "current_effect": current_effect,
            "unresolved_effect": unresolved_effect,
            "next_attempt_at": current[10],
            "cancellation_pending": bool(current[9]),
        }

    @staticmethod
    def _event_current_exact(
        connection: Any,
        wrapped: Any,
        current: Any,
        latest_manifest: Optional[dict[str, Any]],
    ) -> bool:
        target_id = wrapped.target.target_id
        row = connection.execute(
            """
            SELECT status,state_revision,attempt_count,available_at,
                   lease_owner,lease_fence,lease_expires_at,published_at,
                   outcome_unknown_at,dead_at,last_error_bytes,
                   last_error_sha256,created_at,updated_at
            FROM event_outbox WHERE event_id=?
            """,
            (target_id,),
        ).fetchone()
        if row is None or not _row_is(
            row[:3],
            (current[1], current[2], current[3]),
        ):
            return False
        if not _row_is(row[4:7], (current[11], current[12], current[13])):
            return False
        if row[13] != current[19]:
            return False
        status = current[1]
        if status == "pending":
            if current[2] != 1 or row[3] != row[12]:
                return False
        elif status == "retry":
            if current[10] is None or row[3] != current[10]:
                return False
        elif status not in ("leased", "published", "dead", "outcome_unknown"):
            return False
        expected_times = (
            current[19] if status == "published" else None,
            current[19] if status == "outcome_unknown" else None,
            current[19] if status == "dead" else None,
        )
        if not _row_is(row[7:10], expected_times):
            return False
        error_bytes, error_sha256 = row[10], row[11]
        if (error_bytes is None) != (error_sha256 is None):
            return False
        if error_bytes is not None and not _same(
            hashlib.sha256(error_bytes).digest(),
            error_sha256,
        ):
            return False
        if latest_manifest is None:
            return error_bytes is None
        request_error = latest_manifest["request"].get("error")
        expected_error = (
            None
            if request_error is None
            else bytes.fromhex(request_error.get("sha256", ""))
        )
        return _same(error_sha256, expected_error)

    @staticmethod
    def _worker_member_history_exact(
        connection: Any,
        wrapped: Any,
        current: Any,
        receipts: tuple[tuple[Any, dict[str, Any]], ...],
    ) -> bool:
        target_id = wrapped.target.target_id
        target = wrapped.target
        claims = tuple(item for item in receipts if item[0]["operation"] == "claim")
        attempts = tuple(connection.execute(
            """
            SELECT attempt_id,target_id,attempt_number,previous_attempt_id,
                   execution_fence,owner_id,started_at,receipt_transaction_id
            FROM worker_attempts WHERE target_id=? ORDER BY attempt_number
            """,
            (target_id,),
        ).fetchall())
        if len(attempts) != len(claims) or len(attempts) != current[3]:
            return False
        for number, (attempt, (receipt, manifest)) in enumerate(
            zip(attempts, claims),
            start=1,
        ):
            request = manifest["request"]
            if not _row_is(attempt, (
                request.get("attempt_id"),
                target_id,
                number,
                None if number == 1 else attempts[number - 2][0],
                request.get("execution_fence"),
                request.get("lease_owner"),
                receipt["committed_at"],
                receipt["transaction_id"],
            )):
                return False
            if (
                request.get("attempt_number") != number
                or request.get("previous_attempt_id") != attempt[3]
                or request.get("previous_attempt_number")
                != (None if number == 1 else number - 1)
                or request.get("started_at") != receipt["committed_at"]
            ):
                return False
        if attempts and (
            current[8] != attempts[-1][4]
            or current[3] > target.max_attempts
        ):
            return False

        authorizations = tuple(
            item for item in receipts if item[0]["operation"] == "authorize_effect"
        )
        effects = tuple(connection.execute(
            """
            SELECT effect_id,target_id,attempt_id,effect_kind,
                   authorization_transaction_id,idempotency_key,authorized_at
            FROM worker_effects WHERE target_id=? ORDER BY authorized_at,effect_id
            """,
            (target_id,),
        ).fetchall())
        effects_by_receipt = {row[4]: row for row in effects}
        if len(effects) != len(authorizations):
            return False
        for receipt, manifest in authorizations:
            request = manifest["request"]
            effect = request.get("effect")
            lease = request.get("lease")
            after_effect = manifest["after"].get("current_effect")
            persisted = effects_by_receipt.get(receipt["transaction_id"])
            if (
                type(effect) is not dict
                or type(lease) is not dict
                or type(after_effect) is not dict
                or persisted is None
                or not _row_is(persisted, (
                    effect.get("effect_id"),
                    target_id,
                    lease.get("attempt_id"),
                    "outbox_publish",
                    receipt["transaction_id"],
                    target.idempotency_key,
                    receipt["committed_at"],
                ))
                or effect.get("effect_kind") != "outbox_publish"
                or effect.get("idempotency_scope") != target.idempotency_scope
                or effect.get("idempotency_key") != target.idempotency_key
                or after_effect.get("effect_id") != persisted[0]
                or after_effect.get("request_sha256")
                != effect.get("request", {}).get("sha256")
            ):
                return False

        def effectless_expiry(
            item: tuple[Any, dict[str, Any]],
        ) -> bool:
            receipt, manifest = item
            before = manifest["header"]["before"]
            after = manifest["after"]
            return bool(
                receipt["operation"] == "expire"
                and receipt["after_status"] in ("retry", "dead")
                and before.get("current_effect") is None
                and after.get("current_effect") is None
                and after.get("unresolved_effect") is None
                and after.get("result_effect_link") is None
                and after.get("durable_result") is None
                and (
                    receipt["after_status"] != "dead"
                    or after.get("attempt_count") == target.max_attempts
                )
            )

        unsupported_result_projection = any(
            receipt["operation"] in ("complete", "reconcile")
            or (
                receipt["operation"] == "expire"
                and not effectless_expiry((receipt, manifest))
            )
            for receipt, manifest in receipts
        )
        result_count = connection.execute(
            "SELECT count(*) FROM worker_results WHERE target_id=?",
            (target_id,),
        ).fetchone()
        proof_count = connection.execute(
            "SELECT count(*) FROM worker_reconcile_proofs WHERE target_id=?",
            (target_id,),
        ).fetchone()
        # No frozen result-row projection exists yet for result_id/result/proof
        # digests.  Claim/renew/effect authorization and effectless expiry are
        # fully provable; a result-bearing COMPLETE/RECONCILE must fail closed
        # until its own pure projection contract and fixed writer are sealed.
        if (
            unsupported_result_projection
            or result_count is None
            or proof_count is None
            or result_count[0] != 0
            or proof_count[0] != 0
        ):
            return False
        effect_ids = {row[0] for row in effects}
        return (
            (current[14] is None or current[14] in effect_ids)
            and (current[15] is None or current[15] in effect_ids)
        )

    @classmethod
    def _worker_current_progress_exact(
        cls,
        connection: Any,
        plan: ProductionAtomicWritePlan,
        wrapped: Any,
        genesis_receipt_transaction_id: str,
        current: Any,
    ) -> bool:
        rows = cls._worker_receipt_rows(
            connection,
            wrapped.target.target_id,
        )
        if current[2] == 1:
            return bool(
                not rows
                and _row_is(
                    current,
                    cls._current_expected(
                        plan,
                        wrapped,
                        genesis_receipt_transaction_id,
                    ),
                )
                and cls._event_current_exact(
                    connection, wrapped, current, None
                )
            )
        if current[2] < 2 or len(rows) != current[2] - 1:
            return False
        receipts: list[tuple[Any, dict[str, Any]]] = []
        previous_status = wrapped.genesis_status.value
        previous_state_sha256 = wrapped.genesis_state_sha256
        previous_core: dict[str, Any] = {
            "status": previous_status,
            "state_revision": 1,
            "attempt_count": 0,
            "last_attempt_id": None,
            "last_attempt_number": None,
            "current_attempt_id": None,
            "current_attempt_number": None,
            "lease_owner": None,
            "lease_fence": None,
            "lease_expires_at": None,
            "fence_high_water": 0,
            "current_effect": None,
            "unresolved_effect": None,
            "next_attempt_at": None,
            "cancellation_pending": False,
        }
        for revision, row in enumerate(rows, start=2):
            manifest = cls._worker_receipt_manifest(row)
            if (
                manifest is None
                or row["target_kind"] != "outbox"
                or row["before_state_revision"] != revision - 1
                or row["after_state_revision"] != revision
                or row["before_status"] != previous_status
                or cls._worker_state_core(manifest["header"]["before"])
                != previous_core
                or not cls._manifest_target_exact(manifest, wrapped)
                or not cls._typed_worker_receipt_exact(
                    row,
                    manifest,
                    plan,
                    wrapped,
                )
            ):
                return False
            receipts.append((row, manifest))
            previous_status = row["after_status"]
            previous_state_sha256 = row["after_state_sha256"]
            next_core = cls._worker_state_core(manifest["after"])
            if next_core is None:
                return False
            previous_core = next_core
        latest_row, latest_manifest = receipts[-1]
        prior_sha256 = (
            wrapped.genesis_state_sha256
            if len(receipts) == 1
            else receipts[-2][0]["after_state_sha256"]
        )
        if (
            latest_row["transaction_id"] != current[16]
            or latest_row["after_status"] != current[1]
            or not _same(latest_row["after_state_sha256"], current[18])
            or not _same(prior_sha256, current[17])
            or latest_row["committed_at"] != current[19]
            or not _same(previous_state_sha256, current[18])
            or not cls._current_manifest_exact(current, latest_manifest)
            or not cls._event_current_exact(
                connection, wrapped, current, latest_manifest
            )
            or not cls._worker_member_history_exact(
                connection,
                wrapped,
                current,
                tuple(receipts),
            )
        ):
            return False
        return True

    def _plan_free_outbox_progress_exact(
        self,
        connection: Any,
        wrapped: Any,
    ) -> Optional[bool]:
        """Use the V9 plan-free root when the legacy local replay is narrower.

        ``True`` proves the same ledger head and complete target chain observed
        by this reconciliation snapshot.  ``False`` means durable corruption
        or a partial identity.  ``None`` preserves UNKNOWN for a legal suffix
        the plan-free auditor does not yet understand or for an unavailable
        fresh publish fence.
        """

        row = connection.execute(
            "SELECT request_id FROM worker_execution_receipts "
            "WHERE target_id=? ORDER BY sequence DESC LIMIT 1",
            (wrapped.target.target_id,),
        ).fetchone()
        if row is None or len(row) != 1 or type(row[0]) is not str:
            return False
        try:
            result = SQLiteWorkerDurableAuditor(self._database).inspect(row[0])
        except Exception:
            return None
        if result.status is SQLiteWorkerDurableAuditStatus.UNKNOWN:
            return None
        if result.status is not SQLiteWorkerDurableAuditStatus.EXACT:
            return False
        identity = result.identity
        head = connection.execute(
            "SELECT sequence,tail_sha256 FROM worker_ledger_head "
            "WHERE singleton=1"
        ).fetchone()
        if not (
            identity is not None
            and identity.request_id == row[0]
            and identity.target_kind == WorkerTargetKind.OUTBOX.value
            and identity.target_id == wrapped.target.target_id
            and result.ledger_sequence is not None
            and result.ledger_tail_sha256 is not None
        ):
            return False
        if not _row_is(head, (
            result.ledger_sequence,
            result.ledger_tail_sha256,
        )):
            # Both snapshots may be individually exact while a concurrent
            # worker appends between them.  That is a retryable observation
            # race, not evidence that either durable root is corrupt.
            return None
        return True

    @staticmethod
    def _worker_ledger_exact(connection: Any) -> bool:
        head = connection.execute(
            """
            SELECT state_revision,sequence,previous_tail_sha256,
                   tail_sha256,updated_at
            FROM worker_ledger_head WHERE singleton=1
            """
        ).fetchone()
        if head is None:
            return False
        rows = tuple(connection.execute(
            """
            SELECT entry.sequence,entry.entry_kind,entry.transaction_id,
                   entry.receipt_kind,entry.committed_at,
                   entry.previous_tail_sha256,entry.ledger_tail_sha256,
                   entry.request_sha256,entry.after_state_sha256,
                   entry.receipt_sha256,entry.occurred_at,
                   CASE entry.entry_kind
                     WHEN 'worker_execution' THEN EXISTS(
                       SELECT 1 FROM worker_execution_receipts AS receipt
                       WHERE receipt.sequence=entry.sequence AND
                             receipt.transaction_id=entry.transaction_id AND
                             receipt.receipt_kind=entry.receipt_kind AND
                             receipt.committed_at=entry.committed_at AND
                             receipt.previous_ledger_tail_sha256=
                                 entry.previous_tail_sha256 AND
                             receipt.ledger_tail_sha256=entry.ledger_tail_sha256 AND
                             receipt.request_sha256=entry.request_sha256 AND
                             receipt.after_state_sha256=entry.after_state_sha256 AND
                             receipt.receipt_sha256=entry.receipt_sha256)
                     WHEN 'pipeline_control' THEN EXISTS(
                       SELECT 1 FROM pipeline_control_receipts AS receipt
                       WHERE receipt.sequence=entry.sequence AND
                             receipt.transaction_id=entry.transaction_id AND
                             receipt.receipt_kind=entry.receipt_kind AND
                             receipt.committed_at=entry.committed_at AND
                             receipt.previous_ledger_tail_sha256=
                                 entry.previous_tail_sha256 AND
                             receipt.ledger_tail_sha256=entry.ledger_tail_sha256 AND
                             receipt.request_sha256=entry.request_sha256 AND
                             receipt.after_state_sha256=entry.after_state_sha256 AND
                             receipt.receipt_sha256=entry.receipt_sha256)
                     ELSE 0
                   END AS specialised_exact,
                   EXISTS(
                     SELECT 1 FROM transaction_receipts AS parent
                     WHERE parent.transaction_id=entry.transaction_id AND
                           parent.kind=entry.receipt_kind AND
                           parent.committed_at=entry.committed_at)
                       AS parent_exact
            FROM worker_ledger_entries AS entry ORDER BY entry.sequence
            """
        ).fetchall())
        if head[1] == 0:
            return bool(
                head[0] == 1
                and not rows
                and _same(head[2], _ZERO_SHA256)
                and _same(head[3], _ZERO_SHA256)
            )
        if (
            len(rows) != head[1]
            or not 2 <= head[0] <= head[1] + 1
            or head[4] != rows[-1][10]
        ):
            return False
        previous = _ZERO_SHA256
        historical_tails = {_ZERO_SHA256}
        for sequence, row in enumerate(rows, start=1):
            if (
                row[0] != sequence
                or row[4] != row[10]
                or row[11] != 1
                or row[12] != 1
                or not _same(row[5], previous)
            ):
                return False
            tail = _framed_digest(
                _WORKER_LEDGER_TAIL_HASH_DOMAIN,
                sequence.to_bytes(8, "big"),
                previous,
                row[9],
            )
            if not _same(tail, row[6]):
                return False
            historical_tails.add(tail)
            previous = tail
        return bool(
            _same(previous, head[3])
            and head[2] in historical_tails
            and not _same(head[2], head[3])
        )

    @classmethod
    def _outbox_dispatch_head_exact(cls, connection: Any) -> bool:
        """Replay every durable dispatch-fence transition to the current head."""

        head = connection.execute(
            """
            SELECT state_revision,published_sequence,
                   published_repository_revision,published_ordinal,
                   inflight_sequence,inflight_repository_revision,
                   inflight_ordinal,previous_head_sha256,head_sha256,updated_at
            FROM outbox_dispatch_head WHERE singleton=1
            """
        ).fetchone()
        if head is None:
            return False
        state = WorkerOutboxDispatchState(
            1,
            WorkerOutboxDispatchCursor(0, 0, -1),
            None,
        )
        previous_head_sha256 = _ZERO_SHA256
        head_sha256 = _ZERO_SHA256
        updated_at: Optional[str] = None
        for row in cls._worker_receipt_rows(connection, None):
            if row["target_kind"] != "outbox":
                continue
            manifest = cls._worker_receipt_manifest(row)
            if manifest is None:
                return False
            operation = row["operation"]
            dispatch_value = manifest["request"].get("dispatch_fence")
            if operation in ("complete", "reconcile"):
                # Their cursor decision depends on the not-yet-frozen result
                # projection, so a global head containing one is not provable.
                return False
            if dispatch_value is None:
                if operation not in ("renew", "authorize_effect"):
                    return False
                continue
            try:
                fence = cls._dispatch_fence_from_manifest(dispatch_value)
            except (KeyError, TypeError, ValueError):
                return False
            order_row = connection.execute(
                """
                SELECT outbox_sequence,outbox_repository_revision,outbox_ordinal
                FROM worker_targets
                WHERE target_id=? AND target_kind='outbox'
                """,
                (row["target_id"],),
            ).fetchone()
            manifest_order = manifest["header"]["target"].get("outbox_order")
            if (
                order_row is None
                or type(manifest_order) is not dict
                or not _row_is(order_row, (
                    manifest_order.get("sequence"),
                    manifest_order.get("repository_revision"),
                    manifest_order.get("ordinal"),
                ))
            ):
                return False
            target_order = WorkerOutboxOrder(*tuple(order_row))
            if fence.before != state:
                return False
            if fence.after == fence.before:
                if not (
                    (
                        operation == "claim"
                        and row["before_status"] == "retry"
                        and fence.before.inflight == target_order
                    )
                    or (
                        operation == "expire"
                        and row["before_status"] == "leased"
                        and row["after_status"] == "retry"
                        and fence.before.inflight == target_order
                    )
                ):
                    return False
                continue
            if fence.after.control_revision != fence.before.control_revision + 1:
                return False
            first_claim = bool(
                operation == "claim"
                and row["before_status"] == "pending"
                and row["after_status"] == "leased"
                and fence.before.inflight is None
                and fence.after.inflight == target_order
                and fence.after.published == fence.before.published
            )
            expired_dead = bool(
                operation == "expire"
                and row["before_status"] == "leased"
                and row["after_status"] == "dead"
                and fence.before.inflight == target_order
                and fence.after.inflight is None
                and fence.after.published.sequence
                == target_order.sequence
                and fence.after.published.repository_revision
                == target_order.repository_revision
                and fence.after.published.ordinal
                == target_order.ordinal
            )
            if not (first_claim or expired_dead):
                return False
            previous_head_sha256 = head_sha256
            head_sha256 = row["after_state_sha256"]
            updated_at = row["committed_at"]
            state = fence.after

        published = state.published
        inflight = state.inflight
        expected = (
            state.control_revision,
            published.sequence,
            None if published.sequence == 0 else published.repository_revision,
            None if published.sequence == 0 else published.ordinal,
            None if inflight is None else inflight.sequence,
            None if inflight is None else inflight.repository_revision,
            None if inflight is None else inflight.ordinal,
            previous_head_sha256,
            head_sha256,
        )
        if not _row_is(head[:9], expected):
            return False
        if updated_at is not None:
            return head[9] == updated_at
        genesis = connection.execute(
            """
            SELECT mount.committed_at
            FROM business_cutover_head AS cutover
            JOIN business_cutover_mount_receipts AS mount
              ON mount.transaction_id=cutover.mount_transaction_id
            WHERE cutover.singleton=1 AND cutover.phase='mounted'
            """
        ).fetchone()
        return genesis is not None and head[9] == genesis[0]

    @staticmethod
    def _allocation_exact(connection: Any, plan: ProductionAtomicWritePlan) -> bool:
        for allocation in plan.evidence.allocations:
            row = connection.execute(
                """
                SELECT event_id,commit_id,repository_revision,ordinal,
                       previous_tail_sha256,allocation_tail_sha256,allocated_at
                FROM outbox_allocations WHERE sequence=?
                """,
                (allocation.sequence,),
            ).fetchone()
            if not _row_is(row, (
                allocation.event_id,
                allocation.commit_id,
                allocation.repository_revision,
                allocation.ordinal,
                allocation.previous_tail_sha256,
                allocation.allocation_tail_sha256,
                allocation.allocated_at,
            )):
                return False
        return True

    @staticmethod
    def _allocation_head_contains(
        connection: Any,
        plan: ProductionAtomicWritePlan,
        *,
        exact: bool,
    ) -> bool:
        expected = plan.evidence.after_outbox_allocation
        current = SQLiteProductionAtomicWriter._read_allocation_head(connection)
        if (
            (exact and current != expected)
            or current.sequence < expected.sequence
            or current.state_revision < expected.state_revision
            or current.updated_at < expected.updated_at
        ):
            return False
        rows = connection.execute(
            """
            SELECT allocation.sequence,allocation.event_id,
                   allocation.commit_id,allocation.repository_revision,
                   allocation.ordinal,allocation.previous_tail_sha256,
                   allocation.allocation_tail_sha256,allocation.allocated_at,
                   commit_row.outbox_event_count,
                   EXISTS(
                     SELECT 1
                     FROM event_outbox AS event
                     JOIN worker_targets AS target
                       ON target.target_id=event.event_id AND
                          target.target_kind='outbox'
                     WHERE event.event_id=allocation.event_id AND
                           event.commit_id=allocation.commit_id AND
                           event.repository_revision=
                               allocation.repository_revision AND
                           event.ordinal=allocation.ordinal AND
                           allocation.allocated_at=event.created_at AND
                           allocation.allocated_at=target.created_at AND
                           allocation.allocated_at=commit_row.committed_at AND
                           allocation.allocated_at=atomic.committed_at AND
                           event.commit_event_count=
                               commit_row.outbox_event_count AND
                           (SELECT count(*) FROM outbox_allocations AS member
                             WHERE member.commit_id=allocation.commit_id)=
                               commit_row.outbox_event_count AND
                           (SELECT count(*) FROM worker_targets AS member
                             WHERE member.origin_commit_id=
                                 allocation.commit_id)=atomic.target_count AND
                           (SELECT count(*) FROM worker_targets AS member
                             WHERE member.origin_commit_id=allocation.commit_id AND
                                   member.target_kind='outbox')=
                               commit_row.outbox_event_count AND
                           (SELECT count(*) FROM worker_targets AS member
                             WHERE member.origin_commit_id=allocation.commit_id AND
                                   member.target_kind='action')=
                               commit_row.durable_action_count AND
                           (SELECT count(*) FROM worker_targets AS member
                             WHERE member.origin_commit_id=allocation.commit_id AND
                                   member.target_kind='pipeline')=(
                               SELECT count(*) FROM pipeline_control_intents AS intent
                               WHERE intent.origin_commit_id=allocation.commit_id AND
                                     intent.operation='create') AND
                           target.origin_commit_id=allocation.commit_id AND
                           target.source_ordinal=allocation.ordinal AND
                           target.outbox_sequence=allocation.sequence AND
                           target.outbox_repository_revision=
                               allocation.repository_revision AND
                           target.outbox_ordinal=allocation.ordinal)
                       AS closure_exact
            FROM outbox_allocations AS allocation
            JOIN business_commits AS commit_row
              ON commit_row.commit_id=allocation.commit_id AND
                 commit_row.repository_revision=allocation.repository_revision
            JOIN atomic_business_write_receipts AS atomic
              ON atomic.commit_id=allocation.commit_id
            ORDER BY allocation.sequence
            """,
        ).fetchall()
        if len(rows) != current.sequence:
            return False
        if current.sequence == 0:
            return bool(
                current.state_revision == 1
                and _same(current.previous_tail_sha256, _ZERO_SHA256)
                and _same(current.tail_sha256, _ZERO_SHA256)
            )
        previous = _ZERO_SHA256
        previous_repository_revision = 0
        commits: set[str] = set()
        batch_count = 0
        batch_commit: Optional[str] = None
        batch_declared = 0
        batch_members = 0
        batch_allocated_at: Optional[str] = None
        final_batch_before = _ZERO_SHA256
        for expected_sequence, row in enumerate(rows, start=1):
            if (
                row[0] != expected_sequence
                or row[9] != 1
                or not _same(row[5], previous)
            ):
                return False
            if row[2] != batch_commit:
                if batch_commit is not None and batch_members != batch_declared:
                    return False
                if (
                    row[2] in commits
                    or row[4] != 0
                    or row[3] <= previous_repository_revision
                    or type(row[8]) is not int
                    or row[8] < 1
                ):
                    return False
                commits.add(row[2])
                batch_count += 1
                batch_commit = row[2]
                batch_declared = row[8]
                batch_members = 1
                batch_allocated_at = row[7]
                final_batch_before = row[5]
                previous_repository_revision = row[3]
            else:
                if (
                    row[3] != previous_repository_revision
                    or row[4] != batch_members
                    or row[8] != batch_declared
                    or row[7] != batch_allocated_at
                ):
                    return False
                batch_members += 1
            try:
                digest = _production_outbox_allocation_tail(
                    sequence=row[0],
                    event_id=row[1],
                    commit_id=row[2],
                    repository_revision=row[3],
                    ordinal=row[4],
                    previous_tail_sha256=row[5],
                )
            except Exception:
                return False
            if not _same(digest, row[6]):
                return False
            previous = row[6]
        if batch_members != batch_declared:
            return False
        return (
            current.state_revision == batch_count + 1
            and _same(previous, current.tail_sha256)
            and _same(final_batch_before, current.previous_tail_sha256)
            and current.updated_at == rows[-1][7]
        )

    @staticmethod
    def _pipeline_target_static_exact(connection: Any, control: Any) -> bool:
        target = control.target
        row = connection.execute(
            """
            SELECT target_kind,project_id,generation,document_id,
                   origin_commit_id,payload_schema,payload_codec_id,
                   payload_codec_version,payload_bytes,payload_sha256,
                   idempotency_scope,idempotency_key,replay_policy,
                   idempotency_policy,max_attempts,action_type,pipeline_id,
                   intent_type,outbox_topic,outbox_partition_key,
                   outbox_sequence,outbox_repository_revision,outbox_ordinal
            FROM worker_targets WHERE target_id=?
            """,
            (target.target_id,),
        ).fetchone()
        return _row_is(row, (
            "pipeline",
            target.project_id,
            target.generation,
            target.document_id,
            target.origin_commit_id,
            target.payload.schema,
            target.payload.codec_id,
            target.payload.codec_version,
            target.payload.payload_bytes,
            target.payload.payload_sha256,
            target.idempotency_scope,
            target.idempotency_key,
            target.replay_policy.value,
            target.idempotency_policy.value,
            target.max_attempts,
            None,
            target.pipeline_id,
            target.intent_type,
            None,
            None,
            None,
            None,
            None,
        ))

    @staticmethod
    def _prior_state_sha256(
        connection: Any,
        target_id: str,
        revision: int,
    ) -> Optional[bytes]:
        rows = connection.execute(
            """
            SELECT state_sha256 FROM (
                SELECT genesis_state_sha256 AS state_sha256,1 AS revision
                FROM worker_targets WHERE target_id=?
                UNION ALL
                SELECT after_state_sha256,after_state_revision
                FROM worker_execution_receipts WHERE target_id=?
                UNION ALL
                SELECT after_state_sha256,after_state_revision
                FROM pipeline_control_receipts WHERE target_id=?
            ) WHERE revision=?
            """,
            (target_id, target_id, target_id, revision),
        ).fetchall()
        if (
            not rows
            or type(rows[0][0]) is not bytes
            or any(not _same(row[0], rows[0][0]) for row in rows)
        ):
            return None
        return rows[0][0]

    @classmethod
    def _pipeline_control_exact(
        cls,
        connection: Any,
        plan: ProductionAtomicWritePlan,
        intent: Any,
        control: Any,
        *,
        exact_current: bool,
    ) -> Optional[bool]:
        closure = control.closure
        binding = control.control_intent
        receipt = control.expected_receipt
        parent = receipt.receipt_parent
        before_status = (
            None if closure.before_status is None else closure.before_status.value
        )
        if not _row_is(connection.execute(
            """
            SELECT origin_commit_id,ordinal,target_id,target_kind,operation,
                   intent_type,idempotency_key,payload_schema,payload_codec_id,
                   payload_codec_version,payload_bytes,payload_sha256,
                   replay_policy,idempotency_policy,max_attempts,before_status,
                   after_status,before_state_revision,after_state_revision,
                   request_sha256,committed_at
            FROM pipeline_control_intents WHERE intent_id=?
            """,
            (intent.intent_id,),
        ).fetchone(), (
            plan.evidence.commit_id,
            intent.ordinal,
            control.target.target_id,
            "pipeline",
            control.operation.value,
            binding.intent_type,
            binding.idempotency_key,
            binding.payload.schema,
            binding.payload.codec_id,
            binding.payload.codec_version,
            binding.payload.payload_bytes,
            binding.payload.payload_sha256,
            binding.replay_policy.value,
            binding.idempotency_policy.value,
            binding.max_attempts,
            before_status,
            closure.after_status.value,
            closure.before_state_revision,
            closure.after_state_revision,
            closure.request_sha256,
            plan.evidence.committed_at,
        )):
            return False
        if not _row_is(connection.execute(
            """
            SELECT transaction_id,entry_kind,receipt_kind,committed_at,
                   operation,intent_type,target_id,before_status,after_status,
                   before_state_revision,after_state_revision,sequence,
                   previous_ledger_tail_sha256,ledger_tail_sha256,
                   manifest_bytes,request_sha256,after_state_sha256,
                   receipt_sha256
            FROM pipeline_control_receipts WHERE intent_id=?
            """,
            (intent.intent_id,),
        ).fetchone(), (
            parent.transaction_id,
            "pipeline_control",
            parent.kind,
            parent.committed_at,
            control.operation.value,
            binding.intent_type,
            control.target.target_id,
            before_status,
            closure.after_status.value,
            closure.before_state_revision,
            closure.after_state_revision,
            receipt.sequence,
            receipt.previous_ledger_tail_sha256,
            receipt.ledger_tail_sha256,
            closure.manifest_bytes,
            receipt.request_sha256,
            receipt.after_state_sha256,
            receipt.receipt_sha256,
        )):
            return False
        if not _row_is(connection.execute(
            "SELECT kind,committed_at FROM transaction_receipts "
            "WHERE transaction_id=?",
            (parent.transaction_id,),
        ).fetchone(), (parent.kind, parent.committed_at)):
            return False
        if not _row_is(connection.execute(
            """
            SELECT entry_kind,transaction_id,receipt_kind,committed_at,
                   previous_tail_sha256,ledger_tail_sha256,request_sha256,
                   after_state_sha256,receipt_sha256,occurred_at
            FROM worker_ledger_entries WHERE sequence=?
            """,
            (receipt.sequence,),
        ).fetchone(), (
            "pipeline_control",
            parent.transaction_id,
            parent.kind,
            parent.committed_at,
            receipt.previous_ledger_tail_sha256,
            receipt.ledger_tail_sha256,
            receipt.request_sha256,
            receipt.after_state_sha256,
            receipt.receipt_sha256,
            receipt.occurred_at,
        )):
            return False
        member_sha256 = _production_atomic_control_member_sha256(
            commit_id=plan.evidence.commit_id,
            ordinal=intent.ordinal,
            intent_id=intent.intent_id,
            control_transaction_id=parent.transaction_id,
            sequence=receipt.sequence,
            ledger_tail_sha256=receipt.ledger_tail_sha256,
        )
        if not _row_is(connection.execute(
            """
            SELECT ordinal,intent_id,control_transaction_id,sequence,
                   ledger_tail_sha256,binding_sha256
            FROM atomic_business_control_members
            WHERE commit_id=? AND ordinal=?
            """,
            (plan.evidence.commit_id, intent.ordinal),
        ).fetchone(), (
            intent.ordinal,
            intent.intent_id,
            parent.transaction_id,
            receipt.sequence,
            receipt.ledger_tail_sha256,
            member_sha256,
        )):
            return False
        if not cls._pipeline_target_static_exact(connection, control):
            return False

        current = cls._current_row(connection, control.target.target_id)
        if current is None:
            return False
        after = closure.after_state
        previous_state_sha256 = (
            _ZERO_SHA256
            if closure.before_state_revision is None
            else cls._prior_state_sha256(
                connection,
                control.target.target_id,
                closure.before_state_revision,
            )
        )
        if previous_state_sha256 is None:
            return False
        expected_current = (
            "pipeline",
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
            None if after.unresolved_effect is None else after.unresolved_effect.effect_id,
            parent.transaction_id,
            previous_state_sha256,
            receipt.after_state_sha256,
            receipt.occurred_at,
        )
        if _row_is(current, expected_current):
            return True
        if not exact_current and type(current[2]) is int and current[2] > after.state_revision:
            return None
        return False

    def _reconcile_snapshot(
        self,
        connection: Any,
        plan: ProductionAtomicWritePlan,
        *,
        exact_heads: bool,
        profile: Any = None,
        validate_mount: bool = True,
    ) -> tuple[ProductionWriteStatus, str]:
        if validate_mount:
            if profile is None:
                try:
                    profile = _validate_business_runtime_connection(
                        self._database,
                        connection,
                        access=BusinessRuntimeAccess.READER,
                    )
                except Exception:
                    return (
                        ProductionWriteStatus.CORRUPT,
                        "production_runtime_structural_corruption",
                    )
            mount_failure = self._reconcile_mount_exact(
                connection,
                profile,
                plan,
            )
            if mount_failure is not None:
                return mount_failure
        base = plan.atomic_write.atomic_plan
        historical = self._historical._reconcile_snapshot(connection, base)
        if historical.status is BusinessReconcileStatus.NOT_COMMITTED:
            return (
                ProductionWriteStatus.NOT_COMMITTED,
                "production_atomic_absent",
            )
        if historical.status is BusinessReconcileStatus.CONFLICT:
            return ProductionWriteStatus.CONFLICT, historical.code
        if historical.status is BusinessReconcileStatus.UNKNOWN:
            return ProductionWriteStatus.UNKNOWN, historical.code
        if historical.status is BusinessReconcileStatus.CORRUPT:
            row = connection.execute(
                "SELECT manifest_sha256 FROM business_commits WHERE commit_id=?",
                (base.commit_id,),
            ).fetchone()
            if row is not None and not _same(row[0], base.closure.manifest_sha256):
                return (
                    ProductionWriteStatus.CONFLICT,
                    "production_business_identity_conflict",
                )
            return ProductionWriteStatus.CORRUPT, historical.code

        receipt = self._atomic_receipt_row(connection, base.commit_id)
        if receipt is None:
            return (
                ProductionWriteStatus.CORRUPT,
                "production_atomic_receipt_missing",
            )
        if not _row_is(receipt, self._atomic_receipt_expected(plan)):
            return (
                ProductionWriteStatus.CONFLICT,
                "production_atomic_receipt_conflict",
            )
        atomic = plan.atomic_write
        create_receipts = {
            control.target.target_id:
                control.expected_receipt.receipt_parent.transaction_id
            for control in atomic.control_plans
            if control.request.before is None
        }
        unsupported_progress = False
        plan_free_dispatch_proven = False
        plan_free_dispatch_unknown = False
        for wrapped in plan.targets:
            target_id = wrapped.target.target_id
            if not _row_is(
                self._target_row(connection, target_id),
                self._target_expected(plan, wrapped),
            ):
                return ProductionWriteStatus.CORRUPT, "production_target_corrupt"
            if wrapped.target.target_kind is WorkerTargetKind.PIPELINE:
                if target_id not in create_receipts:
                    return (
                        ProductionWriteStatus.CORRUPT,
                        "production_pipeline_target_receipt_missing",
                    )
                continue
            current = self._current_row(connection, target_id)
            if current is None:
                return ProductionWriteStatus.CORRUPT, "production_current_missing"
            expected_current = self._current_expected(
                plan,
                wrapped,
                plan.expected_receipt.receipt_parent.transaction_id,
            )
            if _row_is(current, expected_current):
                if (
                    wrapped.target.target_kind is WorkerTargetKind.OUTBOX
                    and not self._event_current_exact(
                        connection, wrapped, current, None
                    )
                ):
                    return (
                        ProductionWriteStatus.CORRUPT,
                        "production_current_corrupt",
                    )
                continue
            if exact_heads:
                return ProductionWriteStatus.CORRUPT, "production_current_corrupt"
            if wrapped.target.target_kind is WorkerTargetKind.OUTBOX:
                try:
                    progress_exact = self._worker_current_progress_exact(
                        connection,
                        plan,
                        wrapped,
                        plan.expected_receipt.receipt_parent.transaction_id,
                        current,
                    )
                except (TypeError, ValueError, OverflowError, UnicodeError):
                    progress_exact = False
                if not progress_exact:
                    plan_free = self._plan_free_outbox_progress_exact(
                        connection,
                        wrapped,
                    )
                    if plan_free is False:
                        return (
                            ProductionWriteStatus.CORRUPT,
                            "production_current_corrupt",
                        )
                    if plan_free is None:
                        unsupported_progress = True
                        plan_free_dispatch_unknown = True
                    else:
                        plan_free_dispatch_proven = True
            elif type(current[2]) is int and current[2] > 1:
                unsupported_progress = True
            else:
                return ProductionWriteStatus.CORRUPT, "production_current_corrupt"

        for intent, control in zip(base.pipeline_intents, atomic.control_plans):
            try:
                control_exact = self._pipeline_control_exact(
                    connection,
                    plan,
                    intent,
                    control,
                    exact_current=exact_heads,
                )
            except (TypeError, ValueError, OverflowError, UnicodeError):
                control_exact = False
            if control_exact is False:
                return ProductionWriteStatus.CORRUPT, "production_control_corrupt"
            if control_exact is None:
                unsupported_progress = True
        if not self._worker_ledger_exact(connection):
            return ProductionWriteStatus.CORRUPT, "production_worker_ledger_corrupt"
        if exact_heads:
            before_worker = plan.worker_ledger_head.evidence
            after_worker = atomic.evidence.after_worker_ledger
            worker_head = connection.execute(
                "SELECT state_revision,sequence,previous_tail_sha256,"
                "tail_sha256,updated_at FROM worker_ledger_head WHERE singleton=1"
            ).fetchone()
            expected_worker_head = (
                before_worker.state_revision + (1 if atomic.control_plans else 0),
                after_worker.sequence,
                (
                    before_worker.ledger_head.tail_sha256
                    if atomic.control_plans
                    else before_worker.previous_tail_sha256
                ),
                after_worker.tail_sha256,
                (
                    plan.evidence.committed_at
                    if atomic.control_plans
                    else before_worker.updated_at
                ),
            )
            if not _row_is(worker_head, expected_worker_head):
                return (
                    ProductionWriteStatus.CORRUPT,
                    "production_worker_ledger_head_corrupt",
                )
        if not self._outbox_dispatch_head_exact(connection):
            if not (plan_free_dispatch_proven or plan_free_dispatch_unknown):
                return (
                    ProductionWriteStatus.CORRUPT,
                    "production_dispatch_head_corrupt",
                )
        if not self._allocation_exact(connection, plan):
            return ProductionWriteStatus.CORRUPT, "production_allocation_corrupt"
        counts = connection.execute(
            """
            SELECT
              (SELECT count(*) FROM worker_targets WHERE origin_commit_id=?),
              (SELECT count(*) FROM outbox_allocations WHERE commit_id=?),
              (SELECT count(*) FROM pipeline_control_intents
                WHERE origin_commit_id=?),
              (SELECT count(*) FROM pipeline_control_receipts AS receipt
                JOIN pipeline_control_intents AS intent
                  ON intent.intent_id=receipt.intent_id
                WHERE intent.origin_commit_id=?),
              (SELECT count(*) FROM atomic_business_control_members
                WHERE commit_id=?)
            """,
            (base.commit_id,) * 5,
        ).fetchone()
        if not _row_is(counts, (
            len(plan.targets),
            len(plan.evidence.allocations),
            len(base.pipeline_intents),
            len(base.pipeline_intents),
            len(base.pipeline_intents),
        )):
            return ProductionWriteStatus.CORRUPT, "production_target_count_corrupt"
        if not self._allocation_head_contains(
            connection,
            plan,
            exact=exact_heads,
        ):
            return ProductionWriteStatus.CORRUPT, "production_allocation_head_corrupt"
        if unsupported_progress:
            return (
                ProductionWriteStatus.UNKNOWN,
                "production_worker_progress_unsupported",
            )
        return ProductionWriteStatus.COMMITTED, "production_atomic_exact"

    def _verify_precommit(
        self,
        connection: Any,
        plan: ProductionAtomicWritePlan,
    ) -> None:
        self._historical._verify_final_closure(
            connection,
            plan.atomic_write.atomic_plan,
        )
        self._historical._verify_exact_precommit_closure(
            connection,
            plan.atomic_write.atomic_plan,
        )
        status, _code = self._reconcile_snapshot(
            connection,
            plan,
            exact_heads=True,
            validate_mount=False,
        )
        if status is not ProductionWriteStatus.COMMITTED:
            raise _ProductionClosureCorrupt(
                "production_exact_precommit_closure_mismatch"
            )

    @staticmethod
    def _result(
        plan: ProductionAtomicWritePlan,
        status: ProductionWriteStatus,
        code: str,
        *,
        recovered: bool = False,
    ) -> ProductionWriteResult:
        return ProductionWriteResult(
            status=status,
            commit_id=plan.evidence.commit_id,
            code=code,
            repository_revision=(
                plan.atomic_write.atomic_plan.candidate.authority_revision
                if status is ProductionWriteStatus.COMMITTED
                else None
            ),
            recovered_after_ack_loss=recovered,
        )

    def reconcile(self, plan: ProductionAtomicWritePlan) -> ProductionWriteResult:
        plan = _require_production_atomic_write_plan(plan)
        self._require_vertical(plan)
        connection = None
        try:
            connection, _filesystem = self._database._open_unvalidated()
            connection.execute("BEGIN")
            try:
                profile = _validate_business_runtime_connection(
                    self._database,
                    connection,
                    access=BusinessRuntimeAccess.READER,
                )
            except Exception:
                if self._identity_present(connection, plan):
                    result = self._result(
                        plan,
                        ProductionWriteStatus.CORRUPT,
                        "production_runtime_structural_corruption",
                    )
                else:
                    result = self._result(
                        plan,
                        ProductionWriteStatus.UNKNOWN,
                        "production_runtime_validation_failed",
                    )
                _rollback_close(connection)
                connection = None
                return result
            if profile.authority_state is BusinessAuthorityState.REPAIR_REQUIRED:
                result = self._result(
                    plan,
                    ProductionWriteStatus.CORRUPT,
                    "production_runtime_repair_required",
                )
            else:
                status, code = self._reconcile_snapshot(
                    connection,
                    plan,
                    exact_heads=False,
                    profile=profile,
                )
                result = self._result(plan, status, code)
        except Exception:
            if connection is not None:
                _rollback_close(connection)
            return self._result(
                plan,
                ProductionWriteStatus.UNKNOWN,
                "production_reconcile_unavailable",
            )
        except BaseException:
            if connection is not None:
                _rollback_close(connection)
            raise
        else:
            _rollback_close(connection)

        if result.status is ProductionWriteStatus.COMMITTED:
            try:
                self._database._finalize_committed_storage()
            except Exception:
                return self._result(
                    plan,
                    ProductionWriteStatus.UNKNOWN,
                    "production_durability_barrier_failed",
                )
        return result

    def commit(
        self,
        plan: ProductionAtomicWritePlan,
        *,
        _test_hook: Optional[
            Callable[[ProductionAtomicWriteTestPoint], None]
        ] = None,
    ) -> ProductionWriteResult:
        plan = _require_production_atomic_write_plan(plan)
        self._require_vertical(plan)
        if _test_hook is not None and not callable(_test_hook):
            raise TypeError("_test_hook must be callable")

        connection = None
        committed_started = False
        identity_absent = False
        try:
            connection, _filesystem = self._database._open_unvalidated()
            connection.execute("BEGIN IMMEDIATE")
            profile = _validate_business_runtime_connection(
                self._database,
                connection,
                access=BusinessRuntimeAccess.WRITER,
            )
            control_identity_conflict = (
                self._pipeline_control_identity_conflict(connection, plan)
            )
            if self._identity_present(connection, plan):
                _rollback_close(connection)
                connection = None
                result = self.reconcile(plan)
                if (
                    result.status is ProductionWriteStatus.NOT_COMMITTED
                    and control_identity_conflict is not None
                ):
                    return self._result(
                        plan,
                        ProductionWriteStatus.CONFLICT,
                        control_identity_conflict,
                    )
                return ProductionWriteResult(
                    result.status,
                    result.commit_id,
                    result.code,
                    result.repository_revision,
                    result.status is ProductionWriteStatus.COMMITTED,
                )
            if control_identity_conflict is not None:
                _rollback_close(connection)
                connection = None
                return self._result(
                    plan,
                    ProductionWriteStatus.CONFLICT,
                    control_identity_conflict,
                )
            identity_absent = True
            base = plan.atomic_write.atomic_plan
            self._historical._require_authority(profile, base)
            self._require_planning_snapshot(connection, profile, plan)
            self._historical._require_before_head(connection, base)
            self._historical._require_member_identities_free(connection, base)
            self._require_committed_time(connection, plan)

            authorizer = _ProductionWriterAuthorizer()
            connection.set_authorizer(authorizer)
            self._write_transaction(connection, plan)
            self._verify_precommit(connection, plan)
            if authorizer.denied:
                raise _ProductionClosureCorrupt(
                    "production_writer_authorizer_denied"
                )
            if _test_hook is not None:
                _test_hook(ProductionAtomicWriteTestPoint.BEFORE_COMMIT)
            plan = _require_production_atomic_write_plan(plan)
            self._verify_precommit(connection, plan)
            if authorizer.denied:
                raise _ProductionClosureCorrupt(
                    "production_writer_authorizer_denied"
                )
            connection.set_authorizer(_allow_authorizer)
            committed_started = True
            connection.commit()
            if _test_hook is not None:
                _test_hook(ProductionAtomicWriteTestPoint.AFTER_COMMIT_BEFORE_ACK)
            connection.close()
            connection = None
            self._database._finalize_committed_storage()
            return self._result(
                plan,
                ProductionWriteStatus.COMMITTED,
                "production_atomic_committed",
            )
        except (
            _ProductionHeadConflict,
            _ProductionIdentityConflict,
            _HeadConflict,
            _IdentityConflict,
        ) as failure:
            if connection is not None:
                rollback_complete = _rollback_close(connection)
                connection = None
            else:
                rollback_complete = True
            if not rollback_complete:
                return self.reconcile(plan)
            return self._result(
                plan,
                ProductionWriteStatus.CONFLICT,
                str(failure) or "production_atomic_conflict",
            )
        except BaseException as failure:
            process_control = not isinstance(failure, Exception)
            if not committed_started:
                rollback_complete = True
                if connection is not None:
                    rollback_complete = _rollback_close(connection)
                    connection = None
                if process_control:
                    raise
                if rollback_complete and identity_absent:
                    reconciled = self.reconcile(plan)
                    if reconciled.status is ProductionWriteStatus.NOT_COMMITTED:
                        return self._result(
                            plan,
                            ProductionWriteStatus.NOT_COMMITTED,
                            "production_atomic_rolled_back",
                        )
                    return reconciled
            else:
                if connection is not None:
                    _close_quietly(connection)
                    connection = None
            reconciled = self.reconcile(plan)
            if process_control:
                raise
            if reconciled.status is ProductionWriteStatus.COMMITTED:
                return ProductionWriteResult(
                    reconciled.status,
                    reconciled.commit_id,
                    reconciled.code,
                    reconciled.repository_revision,
                    True,
                )
            return reconciled


__all__ = ()
