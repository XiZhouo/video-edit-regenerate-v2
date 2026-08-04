"""Private exact SQLite writer for one prepared atomic business commit.

This adapter is intentionally unmounted: no production command imports it and
it is not exported from the SDK.  It accepts only an issuer-stamped
``AtomicCommitPlan`` and owns every SQL statement, transaction boundary and
post-COMMIT reconcile.  Callers never receive a connection or a SQL callback.
"""

from __future__ import annotations

import hashlib
import hmac
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_FLOOR
from enum import Enum
from typing import Any, Callable, Optional, Sequence, Tuple

from business_codec import CODEC_ID, CODEC_VERSION, encode_payload
from business_transaction_contract import (
    ATOMIC_COMMIT_STATUS_ABORTED,
    ATOMIC_COMMIT_STATUS_COMMITTED,
    ATOMIC_COMMIT_STATUS_INDETERMINATE,
    AtomicCommitFailure,
    AtomicCommitResult,
    AtomicFailureKind,
    PipelineIntentKind,
)
from business_transaction_integrity import (
    AtomicCommitPlan,
    _require_atomic_commit_plan,
)
from operation_record_codec import FrozenOperationBody, materialize_operation_body
from sqlite_business_runtime import (
    BusinessAuthorityState,
    BusinessRuntimeAccess,
    _validate_business_runtime_connection,
)
from sqlite_kernel import SQLiteKernelError, SqliteDatabase, _allow_authorizer
from sqlite_schema import BUSINESS_TRANSACTION_KIND, BUSINESS_TRANSACTION_PREFIX


class BusinessReconcileStatus(str, Enum):
    """Fact returned by a fresh exact reconcile."""

    COMMITTED = "committed"
    NOT_COMMITTED = "not_committed"
    CONFLICT = "conflict"
    CORRUPT = "corrupt"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class BusinessReconcileResult:
    status: BusinessReconcileStatus
    code: str
    repository_revision: Optional[int] = None


class BusinessCommitTestPoint(str, Enum):
    """Process-control/error seams; a hook never receives SQL capability."""

    BEFORE_COMMIT = "before_commit"
    AFTER_COMMIT_BEFORE_ACK = "after_commit_before_ack"


class _HeadConflict(RuntimeError):
    pass


class _IdentityConflict(RuntimeError):
    pass


class _ClosureCorrupt(RuntimeError):
    pass


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _canonical_operation_time(value: Any) -> str:
    """Map a validated non-negative operation epoch to UTC milliseconds.

    ``Decimal(str(value))`` makes the rule independent of platform timestamp
    conversion and binary-float multiplication.  Sub-millisecond values are
    deliberately floored; operation timestamps are never rounded into a later
    instant.
    """

    milliseconds = int(
        (Decimal(str(value)) * Decimal(1000)).to_integral_value(
            rounding=ROUND_FLOOR
        )
    )
    instant = _EPOCH + timedelta(milliseconds=milliseconds)
    return instant.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (
        instant.microsecond // 1000
    )


def _same(left: Any, right: Any) -> bool:
    if type(left) is bytes and type(right) is bytes:
        return hmac.compare_digest(left, right)
    return type(left) is type(right) and left == right


def _row_is(row: Any, expected: Sequence[Any]) -> bool:
    return row is not None and len(row) == len(expected) and all(
        _same(row[index], value) for index, value in enumerate(expected)
    )


def _close_quietly(connection: Any) -> bool:
    try:
        connection.close()
    except Exception:
        return False
    return True


def _rollback_close(connection: Any) -> bool:
    complete = True
    try:
        connection.set_authorizer(_allow_authorizer)
        if connection.in_transaction:
            connection.rollback()
    except Exception:
        complete = False
    if not _close_quietly(connection):
        complete = False
    return complete


_WRITABLE_TABLES = frozenset(
    (
        "transaction_receipts",
        "projects",
        "documents",
        "entities",
        "repository_runtime_control",
        "repository_projects",
        "document_commit_heads",
        "business_commits",
        "business_operations",
        "business_operation_targets",
        "idempotency_inbox",
        "event_outbox",
        "confirmation_requests",
        "deferred_actions",
        "pipeline_jobs",
        "pipeline_job_transitions",
        "document_asset_refs",
    )
)
class _BusinessWriterAuthorizer:
    """Fail closed if fixed writer SQL ever escapes its table/DDL boundary."""

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
                "SQLITE_ATTACH",
                "SQLITE_DETACH",
                "SQLITE_TRANSACTION",
                "SQLITE_SAVEPOINT",
                "SQLITE_CREATE_INDEX",
                "SQLITE_CREATE_TABLE",
                "SQLITE_CREATE_TEMP_INDEX",
                "SQLITE_CREATE_TEMP_TABLE",
                "SQLITE_CREATE_TEMP_TRIGGER",
                "SQLITE_CREATE_TEMP_VIEW",
                "SQLITE_CREATE_TRIGGER",
                "SQLITE_CREATE_VIEW",
                "SQLITE_CREATE_VTABLE",
                "SQLITE_DROP_INDEX",
                "SQLITE_DROP_TABLE",
                "SQLITE_DROP_TEMP_INDEX",
                "SQLITE_DROP_TEMP_TABLE",
                "SQLITE_DROP_TEMP_TRIGGER",
                "SQLITE_DROP_TEMP_VIEW",
                "SQLITE_DROP_TRIGGER",
                "SQLITE_DROP_VIEW",
                "SQLITE_DROP_VTABLE",
                "SQLITE_ALTER_TABLE",
                "SQLITE_REINDEX",
                "SQLITE_ANALYZE",
            )
            if hasattr(sqlite3, name)
        }
        if action in forbidden:
            self.denied = True
            return sqlite3.SQLITE_DENY
        if action == getattr(sqlite3, "SQLITE_PRAGMA", -1):
            self.denied = True
            return sqlite3.SQLITE_DENY
        table = (arg1 or arg2 or "").lower()
        # Reads are issued only by this module's fixed statements and schema
        # triggers.  Several receipt guards intentionally inspect historical
        # legacy-proof tables, so a table read allowlist would be incomplete.
        if action in {
            getattr(sqlite3, name)
            for name in ("SQLITE_INSERT", "SQLITE_UPDATE", "SQLITE_DELETE")
            if hasattr(sqlite3, name)
        } and table not in _WRITABLE_TABLES:
            self.denied = True
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK


class SQLiteBusinessTransactionWriter:
    """Private same-transaction writer plus mandatory fresh reconcile."""

    def __init__(self, database: SqliteDatabase) -> None:
        if type(database) is not SqliteDatabase:
            raise TypeError("database must be an exact SqliteDatabase")
        self._database = database

    def commit(
        self,
        plan: AtomicCommitPlan,
        *,
        _test_hook: Optional[Callable[[BusinessCommitTestPoint], None]] = None,
    ) -> AtomicCommitResult:
        plan = _require_atomic_commit_plan(plan)
        if _test_hook is not None and not callable(_test_hook):
            raise TypeError("_test_hook must be callable")

        connection = None
        committed_started = False
        identity_proven_absent = False
        try:
            connection, _filesystem = self._database._open_unvalidated()
            connection.execute("BEGIN IMMEDIATE")
            profile = _validate_business_runtime_connection(
                self._database,
                connection,
                access=BusinessRuntimeAccess.WRITER,
            )
            if self._commit_identity_exists(connection, plan):
                _rollback_close(connection)
                connection = None
                return self._result_from_reconcile(
                    plan,
                    self.reconcile(plan),
                    recovered=True,
                )

            identity_proven_absent = True
            self._require_authority(profile, plan)

            authorizer = _BusinessWriterAuthorizer()
            connection.set_authorizer(authorizer)
            self._require_before_head(connection, plan)
            self._require_member_identities_free(connection, plan)
            committed_at = self._database_time(connection, plan)
            self._write_transaction(connection, plan, committed_at)
            self._verify_final_closure(connection, plan)
            if authorizer.denied:
                raise _ClosureCorrupt("business_writer_authorizer_denied")
            if _test_hook is not None:
                _test_hook(BusinessCommitTestPoint.BEFORE_COMMIT)
            # The first verification protects the SQL boundary from an
            # already-tampered input.  Verify the carrier again after all
            # writes/test seams, then byte-compare the exact receipt, commit,
            # members and current snapshot while rollback is still possible.
            # Post-COMMIT fresh reconcile remains mandatory for crash/ack-loss
            # classification; it must not be the first place a same-cardinality
            # writer defect is discovered.
            plan = _require_atomic_commit_plan(plan)
            self._verify_exact_precommit_closure(connection, plan)
            if authorizer.denied:
                raise _ClosureCorrupt("business_writer_authorizer_denied")
            connection.set_authorizer(_allow_authorizer)
            committed_started = True
            connection.commit()
            if _test_hook is not None:
                _test_hook(BusinessCommitTestPoint.AFTER_COMMIT_BEFORE_ACK)
            connection.close()
            connection = None
            self._database._finalize_committed_storage()
        except (_HeadConflict, _IdentityConflict) as failure:
            rollback_complete = True
            if connection is not None:
                rollback_complete = _rollback_close(connection)
            if not rollback_complete:
                reconciled = self.reconcile(plan)
                return self._result_from_reconcile(
                    plan,
                    reconciled,
                    recovered=(
                        reconciled.status is BusinessReconcileStatus.COMMITTED
                    ),
                )
            kind = (
                AtomicFailureKind.HEAD_CONFLICT
                if isinstance(failure, _HeadConflict)
                else AtomicFailureKind.IDENTITY_CONFLICT
            )
            return self._failure(plan, kind, str(failure))
        except BaseException as failure:
            process_control = not isinstance(failure, Exception)
            if not committed_started:
                rollback_complete = True
                if connection is not None:
                    rollback_complete = _rollback_close(connection)
                if process_control:
                    raise
                if rollback_complete and identity_proven_absent:
                    return self._failure(
                        plan,
                        AtomicFailureKind.EXACT_NOT_COMMITTED,
                        "business_commit_rolled_back",
                    )
            else:
                if connection is not None:
                    _close_quietly(connection)

            reconciled = self.reconcile(plan)
            if process_control:
                # Cancellation/process control is never swallowed, even after
                # the exact durable outcome was successfully classified.
                raise
            return self._result_from_reconcile(
                plan,
                reconciled,
                recovered=(reconciled.status is BusinessReconcileStatus.COMMITTED),
            )

        reconciled = self.reconcile(plan)
        return self._result_from_reconcile(plan, reconciled, recovered=False)

    def reconcile(self, plan: AtomicCommitPlan) -> BusinessReconcileResult:
        """Classify exact durable evidence through a new connection/snapshot."""

        plan = _require_atomic_commit_plan(plan)
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
            except SQLiteKernelError:
                if self._plan_identity_present(connection, plan):
                    result = BusinessReconcileResult(
                        BusinessReconcileStatus.CORRUPT,
                        "business_runtime_structural_corruption",
                    )
                else:
                    result = BusinessReconcileResult(
                        BusinessReconcileStatus.UNKNOWN,
                        "business_runtime_validation_failed",
                    )
                _rollback_close(connection)
                connection = None
                return result
            if profile.authority_state is BusinessAuthorityState.REPAIR_REQUIRED:
                _rollback_close(connection)
                connection = None
                return BusinessReconcileResult(
                    BusinessReconcileStatus.CORRUPT,
                    "business_runtime_repair_required",
                )
            result = self._reconcile_snapshot(connection, plan)
        except Exception:
            if connection is not None:
                _rollback_close(connection)
            return BusinessReconcileResult(
                BusinessReconcileStatus.UNKNOWN,
                "business_reconcile_unavailable",
            )
        except BaseException:
            if connection is not None:
                _rollback_close(connection)
            raise
        else:
            _rollback_close(connection)

        if result.status is BusinessReconcileStatus.COMMITTED:
            try:
                self._database._finalize_committed_storage()
            except Exception:
                return BusinessReconcileResult(
                    BusinessReconcileStatus.UNKNOWN,
                    "business_durability_barrier_failed",
                )
        return result

    @staticmethod
    def _failure(
        plan: AtomicCommitPlan,
        kind: AtomicFailureKind,
        code: str,
    ) -> AtomicCommitResult:
        status = (
            ATOMIC_COMMIT_STATUS_INDETERMINATE
            if kind in (
                AtomicFailureKind.DURABLE_CORRUPT,
                AtomicFailureKind.OUTCOME_UNKNOWN,
            )
            else ATOMIC_COMMIT_STATUS_ABORTED
        )
        return AtomicCommitResult(
            plan.commit_id,
            status,
            plan.closure.manifest_sha256,
            AtomicCommitFailure(kind, code),
        )

    def _result_from_reconcile(
        self,
        plan: AtomicCommitPlan,
        reconciled: BusinessReconcileResult,
        *,
        recovered: bool,
    ) -> AtomicCommitResult:
        if reconciled.status is BusinessReconcileStatus.COMMITTED:
            assert reconciled.repository_revision is not None
            return AtomicCommitResult(
                plan.commit_id,
                ATOMIC_COMMIT_STATUS_COMMITTED,
                plan.closure.manifest_sha256,
                repository_revision=reconciled.repository_revision,
                recovered_after_ack_loss=recovered,
            )
        if reconciled.status is BusinessReconcileStatus.NOT_COMMITTED:
            kind = AtomicFailureKind.EXACT_NOT_COMMITTED
        elif reconciled.status is BusinessReconcileStatus.CONFLICT:
            kind = AtomicFailureKind.IDENTITY_CONFLICT
        elif reconciled.status is BusinessReconcileStatus.CORRUPT:
            kind = AtomicFailureKind.DURABLE_CORRUPT
        else:
            kind = AtomicFailureKind.OUTCOME_UNKNOWN
        return self._failure(plan, kind, reconciled.code)

    @staticmethod
    def _require_authority(profile: Any, plan: AtomicCommitPlan) -> None:
        binding = plan.authority
        identity_matches = (
            profile.authority == binding.authority
            and profile.authority_state.value == binding.authority_state
            and profile.repository_epoch == binding.authority_epoch
            and profile.cutover_id == binding.cutover_id
            and profile.source_admission_revision
            == binding.source_admission_revision
            and profile.runtime_generation == binding.runtime_generation
            and _same(profile.code_digest, binding.code_digest)
        )
        if not identity_matches:
            raise _IdentityConflict("business_authority_identity_conflict")
        if (
            profile.repository_revision != binding.authority_revision
            or profile.state_revision != binding.control_revision
        ):
            raise _HeadConflict("business_authority_revision_conflict")

    @staticmethod
    def _database_time(connection: Any, plan: AtomicCommitPlan) -> str:
        row = connection.execute(
            """
            SELECT max(
                strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                (SELECT updated_at FROM repository_runtime_control
                  WHERE singleton = 1),
                (SELECT updated_at FROM repository_projects
                  WHERE project_id = ?),
                (SELECT updated_at FROM document_commit_heads
                  WHERE project_id = ?),
                COALESCE((
                    SELECT updated_at FROM idempotency_inbox
                    WHERE scope = ? AND idempotency_key = ?
                ), '0001-01-01T00:00:00.000Z'),
                COALESCE((
                    SELECT updated_at FROM confirmation_requests
                    WHERE confirmation_id = ?
                ), '0001-01-01T00:00:00.000Z')
            )
            """,
            (
                plan.before.project_id,
                plan.before.project_id,
                "" if plan.inbox is None else plan.inbox.scope,
                "" if plan.inbox is None else plan.inbox.inbox_key,
                "" if plan.confirmation is None else plan.confirmation.confirmation_id,
            ),
        ).fetchone()
        if row is None or type(row[0]) is not str or len(row[0]) != 24:
            raise _ClosureCorrupt("business_database_clock_invalid")
        return row[0]

    @staticmethod
    def _commit_identity_exists(connection: Any, plan: AtomicCommitPlan) -> bool:
        transaction_id = BUSINESS_TRANSACTION_PREFIX + plan.commit_id
        row = connection.execute(
            """
            SELECT
                EXISTS(SELECT 1 FROM business_commits WHERE commit_id = ?),
                EXISTS(
                    SELECT 1 FROM transaction_receipts
                    WHERE transaction_id = ? OR
                          (kind = ? AND transaction_id = ?)
                )
            """,
            (plan.commit_id, transaction_id, BUSINESS_TRANSACTION_KIND, transaction_id),
        ).fetchone()
        return row is not None and (int(row[0]) != 0 or int(row[1]) != 0)

    @staticmethod
    def _require_before_head(connection: Any, plan: AtomicCommitPlan) -> None:
        before = plan.before
        project = connection.execute(
            """
            SELECT generation, project_revision, status,
                   current_snapshot_sha256
            FROM repository_projects
            WHERE project_id = ?
            """,
            (before.project_id,),
        ).fetchone()
        if not _row_is(
            project,
            (
                before.generation,
                before.project_revision,
                "live",
                before.business_root_sha256,
            ),
        ):
            raise _HeadConflict("business_project_head_conflict")

        head = connection.execute(
            """
            SELECT generation, document_id, document_version, commit_id,
                   head_revision, status, current_snapshot_sha256,
                   current_project_snapshot_sha256, business_root_sha256,
                   operation_tail_sha256, entity_count
            FROM document_commit_heads
            WHERE project_id = ?
            """,
            (before.project_id,),
        ).fetchone()
        if head is None:
            raise _HeadConflict("business_document_head_missing")
        expected_prefix = (
            before.generation,
            before.document_id,
            before.document_version,
            before.expected_last_commit_id,
            before.head_revision,
            "live",
        )
        if not all(_same(head[index], value) for index, value in enumerate(expected_prefix)):
            raise _HeadConflict("business_document_head_conflict")
        if (
            not _same(head[7], before.business_root_sha256)
            or not _same(head[8], before.business_root_sha256)
            or not _same(head[9], bytes.fromhex(before.operation_tail_hash))
        ):
            raise _HeadConflict("business_document_root_conflict")

        previous = connection.execute(
            """
            SELECT project_id, generation, document_id, document_version,
                   after_root_sha256, operation_tail_sha256, entity_count
            FROM business_commits WHERE commit_id = ?
            """,
            (before.expected_last_commit_id,),
        ).fetchone()
        if not _row_is(
            previous,
            (
                before.project_id,
                before.generation,
                before.document_id,
                before.document_version,
                before.business_root_sha256,
                bytes.fromhex(before.operation_tail_hash),
                head[10],
            ),
        ):
            raise _HeadConflict("business_previous_commit_conflict")

        current = connection.execute(
            """
            SELECT p.generation, p.snapshot_sha256,
                   d.document_id, d.document_version, d.snapshot_sha256,
                   (SELECT count(*) FROM entities e
                    WHERE e.project_id = d.project_id
                      AND e.document_id = d.document_id)
            FROM projects p JOIN documents d ON d.project_id = p.project_id
            WHERE p.project_id = ?
            """,
            (before.project_id,),
        ).fetchone()
        if not _row_is(
            current,
            (
                before.generation,
                before.business_root_sha256,
                before.document_id,
                before.document_version,
                head[6],
                head[10],
            ),
        ):
            raise _HeadConflict("business_current_snapshot_conflict")

    @staticmethod
    def _member_identity_conflict_code(
        connection: Any,
        plan: AtomicCommitPlan,
    ) -> str:
        def occupied(sql: str, values: Sequence[Any]) -> bool:
            row = connection.execute(sql, values).fetchone()
            return row is not None and int(row[0]) != 0

        for record in plan.operations:
            if occupied(
                "SELECT EXISTS(SELECT 1 FROM business_operations WHERE operation_id = ?)",
                (record.operation_id,),
            ):
                return "business_operation_identity_conflict"
        for event in plan.outbox:
            if occupied(
                "SELECT EXISTS(SELECT 1 FROM event_outbox WHERE event_id = ?)",
                (event.event_id,),
            ):
                return "business_event_identity_conflict"
        for action in plan.durable_actions:
            if occupied(
                """
                SELECT EXISTS(
                    SELECT 1 FROM deferred_actions
                    WHERE action_id = ? OR
                          (idempotency_scope = ? AND idempotency_key = ?)
                )
                """,
                (action.action_id, action.idempotency_scope, action.idempotency_key),
            ):
                return "business_action_identity_conflict"
        for intent in plan.pipeline_intents:
            if occupied(
                """
                SELECT EXISTS(
                    SELECT 1 FROM pipeline_job_transitions
                    WHERE intent_id = ? OR
                          (project_id = ? AND generation = ? AND idempotency_key = ?)
                )
                """,
                (
                    intent.intent_id,
                    plan.before.project_id,
                    plan.before.generation,
                    intent.idempotency_key,
                ),
            ):
                return "business_pipeline_intent_conflict"
            if intent.intent_kind is PipelineIntentKind.CREATE:
                if occupied(
                    """
                    SELECT EXISTS(
                        SELECT 1 FROM pipeline_jobs
                        WHERE job_id = ? OR
                              (project_id = ? AND generation = ? AND
                               idempotency_key = ?)
                    )
                    """,
                    (
                        intent.job_id,
                        plan.before.project_id,
                        plan.before.generation,
                        intent.idempotency_key,
                    ),
                ):
                    return "business_pipeline_job_conflict"
        return ""

    @staticmethod
    def _inbox_claim_matches(connection: Any, plan: AtomicCommitPlan) -> bool:
        inbox = plan.inbox
        if inbox is None:
            return True
        row = connection.execute(
            """
            SELECT request_sha256, request_bytes, request_schema,
                   request_codec_id, request_codec_version, actor_id,
                   claim_owner_id, claim_fence, status, state_revision
            FROM idempotency_inbox
            WHERE scope = ? AND idempotency_key = ?
            """,
            (inbox.scope, inbox.inbox_key),
        ).fetchone()
        return _row_is(
            row,
            (
                inbox.request_sha256,
                inbox.request.payload_bytes,
                inbox.request.schema,
                inbox.request.codec_id,
                inbox.request.codec_version,
                inbox.actor_id,
                inbox.claim_owner_id,
                inbox.claim_fence,
                inbox.expected_status,
                inbox.state_revision,
            ),
        )

    @staticmethod
    def _confirmation_reservation_matches(
        connection: Any,
        plan: AtomicCommitPlan,
    ) -> bool:
        confirmation = plan.confirmation
        if confirmation is None:
            return True
        row = connection.execute(
            """
            SELECT project_id, generation, document_id, actor_id,
                   approval_nonce_hmac, nonce_key_id, request_sha256,
                   status, state_revision, reservation_owner_id,
                   reservation_fence
            FROM confirmation_requests WHERE confirmation_id = ?
            """,
            (confirmation.confirmation_id,),
        ).fetchone()
        return _row_is(
            row,
            (
                plan.before.project_id,
                plan.before.generation,
                plan.before.document_id,
                confirmation.actor_id,
                confirmation.nonce_hmac,
                confirmation.nonce_key_id,
                confirmation.expected_request_sha256,
                confirmation.expected_status,
                confirmation.expected_revision,
                confirmation.reservation_owner_id,
                confirmation.reservation_fence,
            ),
        )

    @classmethod
    def _require_member_identities_free(
        cls,
        connection: Any,
        plan: AtomicCommitPlan,
    ) -> None:
        conflict = cls._member_identity_conflict_code(connection, plan)
        if conflict:
            raise _IdentityConflict(conflict)
        for intent in plan.pipeline_intents:
            if intent.intent_kind is not PipelineIntentKind.CREATE:
                job = connection.execute(
                    """
                    SELECT project_id, generation, document_id, pipeline_id,
                           status, state_revision
                    FROM pipeline_jobs WHERE job_id = ?
                    """,
                    (intent.job_id,),
                ).fetchone()
                if not _row_is(
                    job,
                    (
                        plan.before.project_id,
                        plan.before.generation,
                        plan.before.document_id,
                        intent.pipeline_id,
                        intent.expected_status.value,
                        intent.expected_state_revision,
                    ),
                ):
                    raise _HeadConflict("business_pipeline_state_conflict")

        for multiplicity in plan.asset_references.multiplicities:
            reference = multiplicity.reference
            asset = connection.execute(
                """
                SELECT status, media_type, byte_length, extension
                FROM asset_blobs WHERE asset_sha256 = ?
                """,
                (bytes.fromhex(reference.sha256),),
            ).fetchone()
            if asset is None or asset[0] != "available":
                raise _IdentityConflict("business_asset_unavailable")
            if (
                (reference.mime is not None and asset[1] != reference.mime)
                or (reference.size is not None and asset[2] != reference.size)
                or (reference.ext is not None and asset[3] != reference.ext)
            ):
                raise _IdentityConflict("business_asset_metadata_conflict")

        if not cls._inbox_claim_matches(connection, plan):
            raise _IdentityConflict("business_inbox_claim_conflict")
        if not cls._confirmation_reservation_matches(connection, plan):
            raise _IdentityConflict("business_confirmation_conflict")

    def _write_transaction(
        self,
        connection: Any,
        plan: AtomicCommitPlan,
        committed_at: str,
    ) -> None:
        self._write_current_snapshot(connection, plan)
        self._write_receipt_and_commit(connection, plan, committed_at)
        self._write_operations(connection, plan)
        self._write_outbox(connection, plan, committed_at)
        self._write_actions(connection, plan, committed_at)
        self._write_pipelines(connection, plan, committed_at)
        self._write_asset_references(connection, plan)
        self._consume_inbox(connection, plan, committed_at)
        self._consume_confirmation(connection, plan, committed_at)
        self._advance_heads(connection, plan, committed_at)

    @staticmethod
    def _write_current_snapshot(connection: Any, plan: AtomicCommitPlan) -> None:
        project = plan.after_snapshot.project
        document = plan.after_snapshot.document
        before = plan.before
        changed = connection.execute(
            """
            UPDATE projects
            SET generation = ?, codec_id = ?, codec_version = ?,
                snapshot_format_version = ?, snapshot_bytes = ?,
                snapshot_sha256 = ?
            WHERE project_id = ? AND generation = ? AND snapshot_sha256 = ?
            """,
            (
                project.generation,
                project.codec_id,
                project.codec_version,
                project.snapshot_format_version,
                project.snapshot_bytes,
                project.snapshot_sha256,
                before.project_id,
                before.generation,
                before.business_root_sha256,
            ),
        ).rowcount
        if changed != 1:
            raise _HeadConflict("business_project_snapshot_cas_failed")
        changed = connection.execute(
            """
            UPDATE documents
            SET document_id = ?, document_version = ?, schema_version = ?,
                collaboration_mode = ?, save_policy = ?, codec_id = ?,
                codec_version = ?, snapshot_format_version = ?,
                snapshot_bytes = ?, snapshot_sha256 = ?
            WHERE project_id = ? AND document_id = ? AND document_version = ?
            """,
            (
                document.document_id,
                document.document_version,
                document.schema_version,
                document.collaboration_mode,
                document.save_policy,
                document.codec_id,
                document.codec_version,
                document.snapshot_format_version,
                document.snapshot_bytes,
                document.snapshot_sha256,
                before.project_id,
                before.document_id,
                before.document_version,
            ),
        ).rowcount
        if changed != 1:
            raise _HeadConflict("business_document_snapshot_cas_failed")
        connection.execute(
            "DELETE FROM entities WHERE project_id = ? AND document_id = ?",
            (before.project_id, before.document_id),
        )
        for entity in plan.after_snapshot.entities:
            connection.execute(
                """
                INSERT INTO entities(
                    project_id, document_id, entity_type, entity_id,
                    entity_version, deleted, codec_id, codec_version,
                    snapshot_format_version, snapshot_bytes, snapshot_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entity.project_id,
                    entity.document_id,
                    entity.entity_type,
                    entity.entity_id,
                    entity.entity_version,
                    entity.deleted,
                    entity.codec_id,
                    entity.codec_version,
                    entity.snapshot_format_version,
                    entity.snapshot_bytes,
                    entity.snapshot_sha256,
                ),
            )

    @staticmethod
    def _write_receipt_and_commit(
        connection: Any,
        plan: AtomicCommitPlan,
        committed_at: str,
    ) -> None:
        transaction_id = BUSINESS_TRANSACTION_PREFIX + plan.commit_id
        connection.execute(
            """
            INSERT INTO transaction_receipts(transaction_id, kind, committed_at)
            VALUES (?, ?, ?)
            """,
            (transaction_id, BUSINESS_TRANSACTION_KIND, committed_at),
        )
        operation_count = len(plan.operations)
        outbox_count = len(plan.outbox)
        connection.execute(
            """
            INSERT INTO business_commits(
                commit_id, transaction_id, receipt_kind, project_id,
                generation, repository_revision, project_revision,
                head_revision, repository_epoch, cutover_id,
                source_admission_revision, document_id, document_version,
                previous_commit_id, commit_kind, save_policy, trace_id,
                operation_count, operation_target_count, last_operation_id,
                last_operation_ordinal, outbox_event_count,
                last_outbox_event_id, last_outbox_ordinal,
                durable_action_count, pipeline_intent_count,
                before_root_sha256, after_root_sha256,
                previous_operation_tail_sha256, operation_tail_sha256,
                asset_projection_sha256, asset_reference_count, entity_count,
                manifest_bytes, manifest_sha256, committed_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'change', ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                plan.commit_id,
                transaction_id,
                BUSINESS_TRANSACTION_KIND,
                plan.before.project_id,
                plan.before.generation,
                plan.candidate.authority_revision,
                plan.candidate.project_revision,
                plan.candidate.head_revision,
                plan.authority.authority_epoch,
                plan.authority.cutover_id,
                plan.authority.source_admission_revision,
                plan.before.document_id,
                plan.candidate.document_version,
                plan.before.expected_last_commit_id,
                plan.after_snapshot.document.save_policy or "legacy",
                plan.trace_id,
                operation_count,
                len(plan.operation_targets),
                plan.operations[-1].operation_id,
                operation_count - 1,
                outbox_count,
                plan.outbox[-1].event_id,
                outbox_count - 1,
                len(plan.durable_actions),
                len(plan.pipeline_intents),
                plan.before.business_root_sha256,
                plan.candidate.business_root_sha256,
                bytes.fromhex(plan.before.operation_tail_hash),
                bytes.fromhex(plan.candidate.operation_tail_hash),
                plan.asset_references.projection_sha256,
                plan.asset_references.reference_count,
                len(plan.after_snapshot.entities),
                plan.closure.manifest_bytes,
                plan.closure.manifest_sha256,
                committed_at,
            ),
        )

    @staticmethod
    def _operation_columns(record: Any, chain: Any) -> Tuple[Any, ...]:
        body = materialize_operation_body(
            FrozenOperationBody(record.protocol, record.body_bytes, record.body_sha256)
        )
        actor_bytes = encode_payload(body["actor"])
        return (
            body,
            actor_bytes,
            hashlib.sha256(actor_bytes).digest(),
            bytes.fromhex(chain.prev_hash),
            bytes.fromhex(chain.self_hash),
            _canonical_operation_time(body["ts"]),
        )

    @classmethod
    def _write_operations(cls, connection: Any, plan: AtomicCommitPlan) -> None:
        count = len(plan.operations)
        previous_id = None
        for ordinal, (record, chain) in enumerate(
            zip(plan.operations, plan.operation_chain)
        ):
            body, actor_bytes, actor_digest, previous_hash, self_hash, occurred_at = (
                cls._operation_columns(record, chain)
            )
            connection.execute(
                """
                INSERT INTO business_operations(
                    operation_id, previous_operation_id, previous_ordinal,
                    previous_hash, self_hash, commit_id,
                    commit_operation_count, commit_kind, ordinal, project_id,
                    generation, document_id, command_id, source, kind,
                    trace_id, group_id, actor_bytes, actor_sha256,
                    operation_bytes, operation_sha256, operation_schema,
                    codec_id, codec_version, occurred_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, 'change', ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    record.operation_id,
                    previous_id,
                    None if ordinal == 0 else ordinal - 1,
                    previous_hash,
                    self_hash,
                    plan.commit_id,
                    count,
                    ordinal,
                    plan.before.project_id,
                    plan.before.generation,
                    plan.before.document_id,
                    record.command_id,
                    record.source,
                    body["kind"],
                    record.trace_id,
                    body["group_id"] or None,
                    actor_bytes,
                    actor_digest,
                    record.body_bytes,
                    record.body_sha256,
                    record.protocol,
                    CODEC_ID,
                    CODEC_VERSION,
                    occurred_at,
                ),
            )
            previous_id = record.operation_id
        for target in plan.operation_targets:
            connection.execute(
                """
                INSERT INTO business_operation_targets(
                    operation_id, ordinal, entity_type, entity_id, field,
                    patch_kind
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    target.operation_id,
                    target.ordinal,
                    target.entity_type,
                    target.entity_id,
                    target.field,
                    target.patch_kind,
                ),
            )

    @staticmethod
    def _write_outbox(
        connection: Any,
        plan: AtomicCommitPlan,
        committed_at: str,
    ) -> None:
        count = len(plan.outbox)
        previous_id = None
        for ordinal, event in enumerate(plan.outbox):
            connection.execute(
                """
                INSERT INTO event_outbox(
                    event_id, previous_event_id, previous_ordinal, commit_id,
                    repository_revision, commit_event_count, operation_id,
                    ordinal, topic, partition_key, event_bytes, event_sha256,
                    payload_schema, codec_id, codec_version, status,
                    state_revision, attempt_count, available_at, created_at,
                    updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending',
                    1, 0, ?, ?, ?
                )
                """,
                (
                    event.event_id,
                    previous_id,
                    None if ordinal == 0 else ordinal - 1,
                    plan.commit_id,
                    plan.candidate.authority_revision,
                    count,
                    event.operation_id,
                    event.ordinal,
                    event.topic,
                    event.partition_key,
                    event.payload.payload_bytes,
                    event.payload.payload_sha256,
                    event.payload.schema,
                    event.payload.codec_id,
                    event.payload.codec_version,
                    committed_at,
                    committed_at,
                    committed_at,
                ),
            )
            previous_id = event.event_id

    @staticmethod
    def _write_actions(
        connection: Any,
        plan: AtomicCommitPlan,
        committed_at: str,
    ) -> None:
        count = len(plan.durable_actions)
        for action in plan.durable_actions:
            connection.execute(
                """
                INSERT INTO deferred_actions(
                    action_id, idempotency_scope, idempotency_key, project_id,
                    generation, document_id, origin_commit_id,
                    commit_action_count, ordinal, action_kind, action_bytes,
                    action_sha256, payload_schema, codec_id, codec_version,
                    replay_policy, idempotency_policy, status, state_revision,
                    attempt_count, max_attempts, next_attempt_at, created_at,
                    updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    'queued', 1, 0, ?, ?, ?, ?
                )
                """,
                (
                    action.action_id,
                    action.idempotency_scope,
                    action.idempotency_key,
                    plan.before.project_id,
                    plan.before.generation,
                    plan.before.document_id,
                    plan.commit_id,
                    count,
                    action.ordinal,
                    action.action_type,
                    action.payload.payload_bytes,
                    action.payload.payload_sha256,
                    action.payload.schema,
                    action.payload.codec_id,
                    action.payload.codec_version,
                    action.replay_policy.value,
                    action.idempotency_policy.value,
                    action.max_attempts,
                    committed_at,
                    committed_at,
                    committed_at,
                ),
            )

    @staticmethod
    def _write_pipelines(
        connection: Any,
        plan: AtomicCommitPlan,
        committed_at: str,
    ) -> None:
        count = len(plan.pipeline_intents)
        for intent in plan.pipeline_intents:
            before_revision = intent.expected_state_revision
            after_revision = (
                1 if before_revision is None else before_revision + 1
            )
            before_status = (
                None if intent.expected_status is None else intent.expected_status.value
            )
            connection.execute(
                """
                INSERT INTO pipeline_job_transitions(
                    intent_id, origin_commit_id, commit_pipeline_intent_count,
                    ordinal, job_id, project_id, generation, document_id,
                    intent_type, idempotency_key, intent_kind,
                    before_state_revision, after_state_revision, before_status,
                    after_status, intent_bytes, intent_sha256, payload_schema,
                    codec_id, codec_version, replay_policy, max_attempts,
                    committed_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?
                )
                """,
                (
                    intent.intent_id,
                    plan.commit_id,
                    count,
                    intent.ordinal,
                    intent.job_id,
                    plan.before.project_id,
                    plan.before.generation,
                    plan.before.document_id,
                    intent.intent_type,
                    intent.idempotency_key,
                    intent.intent_kind.value,
                    before_revision,
                    after_revision,
                    before_status,
                    intent.target_status.value,
                    intent.payload.payload_bytes,
                    intent.payload.payload_sha256,
                    intent.payload.schema,
                    intent.payload.codec_id,
                    intent.payload.codec_version,
                    intent.replay_policy.value,
                    intent.max_attempts,
                    committed_at,
                ),
            )
            if intent.intent_kind is PipelineIntentKind.CREATE:
                connection.execute(
                    """
                    INSERT INTO pipeline_jobs(
                        job_id, last_intent_id, idempotency_key, project_id,
                        generation, document_id, pipeline_id, intent_type,
                        intent_kind, expected_state_revision, input_commit_id,
                        head_commit_id, job_bytes, job_sha256, payload_schema,
                        codec_id, codec_version, replay_policy, status,
                        state_revision, attempt_count, max_attempts,
                        next_attempt_at, created_at, updated_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, 'create', NULL, ?, ?, ?, ?, ?,
                        ?, ?, ?, 'queued', 1, 0, ?, ?, ?, ?
                    )
                    """,
                    (
                        intent.job_id,
                        intent.intent_id,
                        intent.idempotency_key,
                        plan.before.project_id,
                        plan.before.generation,
                        plan.before.document_id,
                        intent.pipeline_id,
                        intent.intent_type,
                        plan.commit_id,
                        plan.commit_id,
                        intent.payload.payload_bytes,
                        intent.payload.payload_sha256,
                        intent.payload.schema,
                        intent.payload.codec_id,
                        intent.payload.codec_version,
                        intent.replay_policy.value,
                        intent.max_attempts,
                        committed_at,
                        committed_at,
                        committed_at,
                    ),
                )
            else:
                changed = connection.execute(
                    """
                    UPDATE pipeline_jobs
                    SET last_intent_id = ?, head_commit_id = ?, status = ?,
                        state_revision = ?, updated_at = ?
                    WHERE job_id = ? AND project_id = ? AND generation = ?
                      AND document_id = ? AND pipeline_id = ?
                      AND status = ? AND state_revision = ?
                    """,
                    (
                        intent.intent_id,
                        plan.commit_id,
                        intent.target_status.value,
                        after_revision,
                        committed_at,
                        intent.job_id,
                        plan.before.project_id,
                        plan.before.generation,
                        plan.before.document_id,
                        intent.pipeline_id,
                        intent.expected_status.value,
                        intent.expected_state_revision,
                    ),
                ).rowcount
                if changed != 1:
                    raise _HeadConflict("business_pipeline_state_cas_failed")

    @staticmethod
    def _write_asset_references(connection: Any, plan: AtomicCommitPlan) -> None:
        count = plan.asset_references.reference_count
        for ordinal, occurrence in enumerate(plan.asset_references.occurrences):
            reference = occurrence.reference
            connection.execute(
                """
                INSERT INTO document_asset_refs(
                    project_id, generation, document_id, document_version,
                    commit_id, commit_asset_reference_count, ordinal,
                    asset_sha256, entity_type, entity_id, entity_deleted,
                    value_path, ref_mime, ref_size, ref_ext
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan.before.project_id,
                    plan.before.generation,
                    plan.before.document_id,
                    plan.candidate.document_version,
                    plan.commit_id,
                    count,
                    ordinal,
                    bytes.fromhex(reference.sha256),
                    occurrence.entity_type,
                    occurrence.entity_id,
                    int(occurrence.entity_deleted),
                    occurrence.value_path,
                    reference.mime,
                    reference.size,
                    reference.ext,
                ),
            )

    @staticmethod
    def _consume_inbox(
        connection: Any,
        plan: AtomicCommitPlan,
        committed_at: str,
    ) -> None:
        inbox = plan.inbox
        if inbox is None:
            return
        changed = connection.execute(
            """
            UPDATE idempotency_inbox
            SET status = 'committed', state_revision = state_revision + 1,
                lease_expires_at = NULL, result_kind = 'commit',
                commit_id = ?, confirmation_id = NULL,
                response_bytes = ?, response_sha256 = ?, response_schema = ?,
                response_codec_id = ?, response_codec_version = ?, updated_at = ?
            WHERE scope = ? AND idempotency_key = ?
              AND request_sha256 = ? AND request_bytes = ?
              AND request_schema = ? AND request_codec_id = ?
              AND request_codec_version = ? AND actor_id = ?
              AND claim_owner_id = ? AND claim_fence = ?
              AND status = 'claimed' AND state_revision = ?
            """,
            (
                plan.commit_id,
                inbox.response.payload_bytes,
                inbox.response.payload_sha256,
                inbox.response.schema,
                inbox.response.codec_id,
                inbox.response.codec_version,
                committed_at,
                inbox.scope,
                inbox.inbox_key,
                inbox.request_sha256,
                inbox.request.payload_bytes,
                inbox.request.schema,
                inbox.request.codec_id,
                inbox.request.codec_version,
                inbox.actor_id,
                inbox.claim_owner_id,
                inbox.claim_fence,
                inbox.state_revision,
            ),
        ).rowcount
        if changed != 1:
            raise _IdentityConflict("business_inbox_claim_cas_failed")

    @staticmethod
    def _consume_confirmation(
        connection: Any,
        plan: AtomicCommitPlan,
        committed_at: str,
    ) -> None:
        confirmation = plan.confirmation
        if confirmation is None:
            return
        changed = connection.execute(
            """
            UPDATE confirmation_requests
            SET status = 'consumed', state_revision = state_revision + 1,
                reservation_owner_id = NULL, reservation_fence = NULL,
                reservation_expires_at = NULL, decision = 'confirmed',
                resolution_bytes = ?, resolution_sha256 = ?,
                resolution_schema = ?, resolution_codec_id = ?,
                resolution_codec_version = ?, result_commit_id = ?,
                resolved_at = ?, updated_at = ?
            WHERE confirmation_id = ? AND project_id = ? AND generation = ?
              AND document_id = ? AND actor_id = ?
              AND approval_nonce_hmac = ? AND nonce_key_id = ?
              AND request_sha256 = ? AND status = 'reserved'
              AND state_revision = ? AND reservation_owner_id = ?
              AND reservation_fence = ?
            """,
            (
                confirmation.resolution.payload_bytes,
                confirmation.resolution.payload_sha256,
                confirmation.resolution.schema,
                confirmation.resolution.codec_id,
                confirmation.resolution.codec_version,
                plan.commit_id,
                committed_at,
                committed_at,
                confirmation.confirmation_id,
                plan.before.project_id,
                plan.before.generation,
                plan.before.document_id,
                confirmation.actor_id,
                confirmation.nonce_hmac,
                confirmation.nonce_key_id,
                confirmation.expected_request_sha256,
                confirmation.expected_revision,
                confirmation.reservation_owner_id,
                confirmation.reservation_fence,
            ),
        ).rowcount
        if changed != 1:
            raise _IdentityConflict("business_confirmation_cas_failed")

    @staticmethod
    def _advance_heads(
        connection: Any,
        plan: AtomicCommitPlan,
        committed_at: str,
    ) -> None:
        before = plan.before
        candidate = plan.candidate
        changed = connection.execute(
            """
            UPDATE repository_projects
            SET project_revision = ?, current_snapshot_sha256 = ?, updated_at = ?
            WHERE project_id = ? AND generation = ? AND project_revision = ?
              AND status = 'live' AND current_snapshot_sha256 = ?
            """,
            (
                candidate.project_revision,
                candidate.business_root_sha256,
                committed_at,
                before.project_id,
                before.generation,
                before.project_revision,
                before.business_root_sha256,
            ),
        ).rowcount
        if changed != 1:
            raise _HeadConflict("business_project_head_cas_failed")

        changed = connection.execute(
            """
            UPDATE document_commit_heads
            SET generation = ?, document_version = ?, commit_id = ?,
                head_revision = ?, current_snapshot_sha256 = ?,
                current_project_snapshot_sha256 = ?, business_root_sha256 = ?,
                operation_tail_sha256 = ?, asset_projection_sha256 = ?,
                asset_reference_count = ?, entity_count = ?, updated_at = ?
            WHERE project_id = ? AND generation = ? AND document_id = ?
              AND document_version = ? AND commit_id = ? AND head_revision = ?
              AND status = 'live' AND business_root_sha256 = ?
              AND operation_tail_sha256 = ?
            """,
            (
                candidate.generation,
                candidate.document_version,
                candidate.commit_id,
                candidate.head_revision,
                plan.after_snapshot.document.snapshot_sha256,
                candidate.business_root_sha256,
                candidate.business_root_sha256,
                bytes.fromhex(candidate.operation_tail_hash),
                plan.asset_references.projection_sha256,
                plan.asset_references.reference_count,
                len(plan.after_snapshot.entities),
                committed_at,
                before.project_id,
                before.generation,
                before.document_id,
                before.document_version,
                before.expected_last_commit_id,
                before.head_revision,
                before.business_root_sha256,
                bytes.fromhex(before.operation_tail_hash),
            ),
        ).rowcount
        if changed != 1:
            raise _HeadConflict("business_document_head_cas_failed")

        changed = connection.execute(
            """
            UPDATE repository_runtime_control
            SET repository_revision = ?, updated_at = ?
            WHERE singleton = 1 AND authority = 'sqlite'
              AND authority_state = 'active' AND repository_epoch = ?
              AND cutover_id = ? AND source_admission_revision = ?
              AND repository_revision = ? AND state_revision = ?
              AND runtime_generation = ? AND code_digest = ?
            """,
            (
                candidate.authority_revision,
                committed_at,
                plan.authority.authority_epoch,
                plan.authority.cutover_id,
                plan.authority.source_admission_revision,
                plan.authority.authority_revision,
                plan.authority.control_revision,
                plan.authority.runtime_generation,
                plan.authority.code_digest,
            ),
        ).rowcount
        if changed != 1:
            raise _HeadConflict("business_runtime_head_cas_failed")

    @staticmethod
    def _verify_final_closure(connection: Any, plan: AtomicCommitPlan) -> None:
        if not connection.in_transaction:
            raise _ClosureCorrupt("business_transaction_boundary_lost")
        counts = connection.execute(
            """
            SELECT
              (SELECT count(*) FROM business_operations WHERE commit_id = ?),
              (SELECT count(*) FROM business_operation_targets t
                 JOIN business_operations o ON o.operation_id = t.operation_id
                WHERE o.commit_id = ?),
              (SELECT count(*) FROM event_outbox WHERE commit_id = ?),
              (SELECT count(*) FROM deferred_actions WHERE origin_commit_id = ?),
              (SELECT count(*) FROM pipeline_job_transitions
                WHERE origin_commit_id = ?),
              (SELECT count(*) FROM document_asset_refs WHERE commit_id = ?),
              (SELECT count(*) FROM entities
                WHERE project_id = ? AND document_id = ?)
            """,
            (
                plan.commit_id,
                plan.commit_id,
                plan.commit_id,
                plan.commit_id,
                plan.commit_id,
                plan.commit_id,
                plan.before.project_id,
                plan.before.document_id,
            ),
        ).fetchone()
        if not _row_is(
            counts,
            (
                len(plan.operations),
                len(plan.operation_targets),
                len(plan.outbox),
                len(plan.durable_actions),
                len(plan.pipeline_intents),
                plan.asset_references.reference_count,
                len(plan.after_snapshot.entities),
            ),
        ):
            raise _ClosureCorrupt("business_effect_closure_mismatch")
        head = connection.execute(
            """
            SELECT commit_id, head_revision, document_version,
                   business_root_sha256, operation_tail_sha256,
                   asset_projection_sha256, asset_reference_count, entity_count
            FROM document_commit_heads WHERE project_id = ?
            """,
            (plan.before.project_id,),
        ).fetchone()
        if not _row_is(
            head,
            (
                plan.commit_id,
                plan.candidate.head_revision,
                plan.candidate.document_version,
                plan.candidate.business_root_sha256,
                bytes.fromhex(plan.candidate.operation_tail_hash),
                plan.asset_references.projection_sha256,
                plan.asset_references.reference_count,
                len(plan.after_snapshot.entities),
            ),
        ):
            raise _ClosureCorrupt("business_final_head_mismatch")
        runtime = connection.execute(
            """
            SELECT repository_revision, state_revision, repository_epoch,
                   cutover_id, source_admission_revision,
                   runtime_generation, code_digest
            FROM repository_runtime_control WHERE singleton = 1
            """
        ).fetchone()
        if not _row_is(
            runtime,
            (
                plan.candidate.authority_revision,
                plan.authority.control_revision,
                plan.authority.authority_epoch,
                plan.authority.cutover_id,
                plan.authority.source_admission_revision,
                plan.authority.runtime_generation,
                plan.authority.code_digest,
            ),
        ):
            raise _ClosureCorrupt("business_final_runtime_mismatch")
        # Do not run history-wide PRAGMA foreign_key_check in a commit.  The
        # exact target/plan closure above proves every row written by this
        # transaction, while foreign_keys=ON and the deferred constraints make
        # SQLite reject an invalid delta at COMMIT.  Startup/periodic global FK
        # proof belongs to sqlite_business_audit.

    def _verify_exact_precommit_closure(
        self,
        connection: Any,
        plan: AtomicCommitPlan,
    ) -> None:
        """Reject any exact durable mismatch before COMMIT can make it permanent."""

        if not connection.in_transaction:
            raise _ClosureCorrupt("business_transaction_boundary_lost")
        reconciled = self._reconcile_snapshot(connection, plan)
        if (
            reconciled.status is not BusinessReconcileStatus.COMMITTED
            or reconciled.repository_revision != plan.candidate.authority_revision
        ):
            raise _ClosureCorrupt("business_exact_precommit_closure_mismatch")

    @staticmethod
    def _plan_identity_present(connection: Any, plan: AtomicCommitPlan) -> bool:
        transaction_id = BUSINESS_TRANSACTION_PREFIX + plan.commit_id
        row = connection.execute(
            """
            SELECT
              EXISTS(SELECT 1 FROM business_commits WHERE commit_id = ?),
              EXISTS(SELECT 1 FROM transaction_receipts WHERE transaction_id = ?),
              EXISTS(SELECT 1 FROM business_operations WHERE commit_id = ?),
              EXISTS(SELECT 1 FROM event_outbox WHERE commit_id = ?),
              EXISTS(SELECT 1 FROM deferred_actions WHERE origin_commit_id = ?),
              EXISTS(SELECT 1 FROM pipeline_job_transitions
                       WHERE origin_commit_id = ?),
              EXISTS(SELECT 1 FROM document_asset_refs WHERE commit_id = ?)
            """,
            (
                plan.commit_id,
                transaction_id,
                plan.commit_id,
                plan.commit_id,
                plan.commit_id,
                plan.commit_id,
                plan.commit_id,
            ),
        ).fetchone()
        return row is not None and any(int(row[index]) != 0 for index in range(7))

    def _reconcile_snapshot(
        self,
        connection: Any,
        plan: AtomicCommitPlan,
    ) -> BusinessReconcileResult:
        transaction_id = BUSINESS_TRANSACTION_PREFIX + plan.commit_id
        receipt = connection.execute(
            """
            SELECT kind, committed_at FROM transaction_receipts
            WHERE transaction_id = ?
            """,
            (transaction_id,),
        ).fetchone()
        commit = connection.execute(
            """
            SELECT transaction_id, receipt_kind, project_id, generation,
                   repository_revision, project_revision, head_revision,
                   repository_epoch, cutover_id, source_admission_revision,
                   document_id, document_version, previous_commit_id,
                   commit_kind, save_policy, trace_id,
                   operation_count, operation_target_count, last_operation_id,
                   last_operation_ordinal, outbox_event_count,
                   last_outbox_event_id, last_outbox_ordinal,
                   durable_action_count, pipeline_intent_count,
                   before_root_sha256, after_root_sha256,
                   previous_operation_tail_sha256, operation_tail_sha256,
                   asset_projection_sha256, asset_reference_count,
                   entity_count, manifest_bytes, manifest_sha256, committed_at
            FROM business_commits WHERE commit_id = ?
            """,
            (plan.commit_id,),
        ).fetchone()
        if receipt is None and commit is None:
            if self._member_identity_collision(connection, plan):
                return BusinessReconcileResult(
                    BusinessReconcileStatus.CONFLICT,
                    "business_member_identity_conflict",
                )
            return BusinessReconcileResult(
                BusinessReconcileStatus.NOT_COMMITTED,
                "business_exact_commit_absent",
            )
        if receipt is None or commit is None:
            return BusinessReconcileResult(
                BusinessReconcileStatus.CORRUPT,
                "business_receipt_commit_incomplete",
            )
        committed_at = commit[34]
        expected_commit = (
            transaction_id,
            BUSINESS_TRANSACTION_KIND,
            plan.before.project_id,
            plan.before.generation,
            plan.candidate.authority_revision,
            plan.candidate.project_revision,
            plan.candidate.head_revision,
            plan.authority.authority_epoch,
            plan.authority.cutover_id,
            plan.authority.source_admission_revision,
            plan.before.document_id,
            plan.candidate.document_version,
            plan.before.expected_last_commit_id,
            "change",
            plan.after_snapshot.document.save_policy or "legacy",
            plan.trace_id,
            len(plan.operations),
            len(plan.operation_targets),
            plan.operations[-1].operation_id,
            len(plan.operations) - 1,
            len(plan.outbox),
            plan.outbox[-1].event_id,
            len(plan.outbox) - 1,
            len(plan.durable_actions),
            len(plan.pipeline_intents),
            plan.before.business_root_sha256,
            plan.candidate.business_root_sha256,
            bytes.fromhex(plan.before.operation_tail_hash),
            bytes.fromhex(plan.candidate.operation_tail_hash),
            plan.asset_references.projection_sha256,
            plan.asset_references.reference_count,
            len(plan.after_snapshot.entities),
            plan.closure.manifest_bytes,
            plan.closure.manifest_sha256,
            committed_at,
        )
        if not _row_is(commit, expected_commit) or not _row_is(
            receipt, (BUSINESS_TRANSACTION_KIND, committed_at)
        ):
            return BusinessReconcileResult(
                BusinessReconcileStatus.CORRUPT,
                "business_commit_identity_mismatch",
            )
        runtime = connection.execute(
            """
            SELECT repository_revision
            FROM repository_runtime_control WHERE singleton = 1
            """
        ).fetchone()
        # The immutable commit row and exact manifest bind the authority that
        # accepted this transaction.  A later runtime generation/cutover must
        # not rewrite that historical fact into CONFLICT.  The current runtime
        # was structurally validated on entry; only a global revision rollback
        # can contradict an already committed repository revision here.
        if (
            runtime is None
            or type(runtime[0]) is not int
            or runtime[0] < plan.candidate.authority_revision
        ):
            return BusinessReconcileResult(
                BusinessReconcileStatus.CORRUPT,
                "business_repository_revision_regressed",
            )
        if not self._reconcile_members(connection, plan, committed_at):
            return BusinessReconcileResult(
                BusinessReconcileStatus.CORRUPT,
                "business_commit_member_mismatch",
            )
        return BusinessReconcileResult(
            BusinessReconcileStatus.COMMITTED,
            "business_exact_commit_durable",
            plan.candidate.authority_revision,
        )

    @classmethod
    def _member_identity_collision(
        cls,
        connection: Any,
        plan: AtomicCommitPlan,
    ) -> bool:
        return bool(cls._member_identity_conflict_code(connection, plan)) or not (
            cls._inbox_claim_matches(connection, plan)
            and cls._confirmation_reservation_matches(connection, plan)
        )

    def _reconcile_members(
        self,
        connection: Any,
        plan: AtomicCommitPlan,
        committed_at: str,
    ) -> bool:
        if not self._reconcile_member_cardinality(connection, plan):
            return False
        previous_id = None
        for ordinal, (record, chain) in enumerate(
            zip(plan.operations, plan.operation_chain)
        ):
            body, actor_bytes, actor_digest, previous_hash, self_hash, occurred_at = (
                self._operation_columns(record, chain)
            )
            row = connection.execute(
                """
                SELECT previous_operation_id, previous_ordinal, previous_hash,
                       self_hash, commit_id, commit_operation_count,
                       commit_kind, ordinal, project_id, generation,
                       document_id, command_id, source, kind, trace_id,
                       group_id, actor_bytes, actor_sha256, operation_bytes,
                       operation_sha256, operation_schema, codec_id,
                       codec_version, occurred_at
                FROM business_operations WHERE operation_id = ?
                """,
                (record.operation_id,),
            ).fetchone()
            if not _row_is(
                row,
                (
                    previous_id,
                    None if ordinal == 0 else ordinal - 1,
                    previous_hash,
                    self_hash,
                    plan.commit_id,
                    len(plan.operations),
                    "change",
                    ordinal,
                    plan.before.project_id,
                    plan.before.generation,
                    plan.before.document_id,
                    record.command_id,
                    record.source,
                    body["kind"],
                    record.trace_id,
                    body["group_id"] or None,
                    actor_bytes,
                    actor_digest,
                    record.body_bytes,
                    record.body_sha256,
                    record.protocol,
                    CODEC_ID,
                    CODEC_VERSION,
                    occurred_at,
                ),
            ):
                return False
            previous_id = record.operation_id
        for target in plan.operation_targets:
            row = connection.execute(
                """
                SELECT entity_type, entity_id, field, patch_kind
                FROM business_operation_targets
                WHERE operation_id = ? AND ordinal = ?
                """,
                (target.operation_id, target.ordinal),
            ).fetchone()
            if not _row_is(
                row,
                (
                    target.entity_type,
                    target.entity_id,
                    target.field,
                    target.patch_kind,
                ),
            ):
                return False

        previous_event = None
        for ordinal, event in enumerate(plan.outbox):
            row = connection.execute(
                """
                SELECT previous_event_id, previous_ordinal, commit_id,
                       repository_revision, commit_event_count, operation_id,
                       ordinal, topic, partition_key, event_bytes,
                       event_sha256, payload_schema, codec_id, codec_version,
                       created_at
                FROM event_outbox WHERE event_id = ?
                """,
                (event.event_id,),
            ).fetchone()
            if not _row_is(
                row,
                (
                    previous_event,
                    None if ordinal == 0 else ordinal - 1,
                    plan.commit_id,
                    plan.candidate.authority_revision,
                    len(plan.outbox),
                    event.operation_id,
                    event.ordinal,
                    event.topic,
                    event.partition_key,
                    event.payload.payload_bytes,
                    event.payload.payload_sha256,
                    event.payload.schema,
                    event.payload.codec_id,
                    event.payload.codec_version,
                    committed_at,
                ),
            ):
                return False
            previous_event = event.event_id

        for action in plan.durable_actions:
            row = connection.execute(
                """
                SELECT idempotency_scope, idempotency_key, project_id,
                       generation, document_id, origin_commit_id,
                       commit_action_count, ordinal, action_kind, action_bytes,
                       action_sha256, payload_schema, codec_id, codec_version,
                       replay_policy, idempotency_policy, max_attempts,
                       created_at
                FROM deferred_actions WHERE action_id = ?
                """,
                (action.action_id,),
            ).fetchone()
            if not _row_is(
                row,
                (
                    action.idempotency_scope,
                    action.idempotency_key,
                    plan.before.project_id,
                    plan.before.generation,
                    plan.before.document_id,
                    plan.commit_id,
                    len(plan.durable_actions),
                    action.ordinal,
                    action.action_type,
                    action.payload.payload_bytes,
                    action.payload.payload_sha256,
                    action.payload.schema,
                    action.payload.codec_id,
                    action.payload.codec_version,
                    action.replay_policy.value,
                    action.idempotency_policy.value,
                    action.max_attempts,
                    committed_at,
                ),
            ):
                return False

        for intent in plan.pipeline_intents:
            before_revision = intent.expected_state_revision
            after_revision = 1 if before_revision is None else before_revision + 1
            before_status = (
                None if intent.expected_status is None else intent.expected_status.value
            )
            row = connection.execute(
                """
                SELECT origin_commit_id, commit_pipeline_intent_count, ordinal,
                       job_id, project_id, generation, document_id, intent_type,
                       idempotency_key, intent_kind, before_state_revision,
                       after_state_revision, before_status, after_status,
                       intent_bytes, intent_sha256, payload_schema, codec_id,
                       codec_version, replay_policy, max_attempts, committed_at
                FROM pipeline_job_transitions WHERE intent_id = ?
                """,
                (intent.intent_id,),
            ).fetchone()
            if not _row_is(
                row,
                (
                    plan.commit_id,
                    len(plan.pipeline_intents),
                    intent.ordinal,
                    intent.job_id,
                    plan.before.project_id,
                    plan.before.generation,
                    plan.before.document_id,
                    intent.intent_type,
                    intent.idempotency_key,
                    intent.intent_kind.value,
                    before_revision,
                    after_revision,
                    before_status,
                    intent.target_status.value,
                    intent.payload.payload_bytes,
                    intent.payload.payload_sha256,
                    intent.payload.schema,
                    intent.payload.codec_id,
                    intent.payload.codec_version,
                    intent.replay_policy.value,
                    intent.max_attempts,
                    committed_at,
                ),
            ):
                return False
            if intent.intent_kind is PipelineIntentKind.CREATE:
                job = connection.execute(
                    """
                    SELECT idempotency_key, project_id, generation, document_id,
                           pipeline_id, intent_type, intent_kind,
                           expected_state_revision, input_commit_id, job_bytes,
                           job_sha256, payload_schema, codec_id, codec_version,
                           replay_policy, max_attempts, created_at
                    FROM pipeline_jobs WHERE job_id = ?
                    """,
                    (intent.job_id,),
                ).fetchone()
                if not _row_is(
                    job,
                    (
                        intent.idempotency_key,
                        plan.before.project_id,
                        plan.before.generation,
                        plan.before.document_id,
                        intent.pipeline_id,
                        intent.intent_type,
                        "create",
                        None,
                        plan.commit_id,
                        intent.payload.payload_bytes,
                        intent.payload.payload_sha256,
                        intent.payload.schema,
                        intent.payload.codec_id,
                        intent.payload.codec_version,
                        intent.replay_policy.value,
                        intent.max_attempts,
                        committed_at,
                    ),
                ):
                    return False

        for ordinal, occurrence in enumerate(plan.asset_references.occurrences):
            reference = occurrence.reference
            row = connection.execute(
                """
                SELECT project_id, generation, document_id, document_version,
                       commit_asset_reference_count, asset_sha256, entity_type,
                       entity_id, entity_deleted, value_path, ref_mime,
                       ref_size, ref_ext
                FROM document_asset_refs
                WHERE commit_id = ? AND ordinal = ?
                """,
                (plan.commit_id, ordinal),
            ).fetchone()
            if not _row_is(
                row,
                (
                    plan.before.project_id,
                    plan.before.generation,
                    plan.before.document_id,
                    plan.candidate.document_version,
                    plan.asset_references.reference_count,
                    bytes.fromhex(reference.sha256),
                    occurrence.entity_type,
                    occurrence.entity_id,
                    int(occurrence.entity_deleted),
                    occurrence.value_path,
                    reference.mime,
                    reference.size,
                    reference.ext,
                ),
            ):
                return False

        if not self._reconcile_inbox(connection, plan):
            return False
        if not self._reconcile_confirmation(connection, plan):
            return False
        return True

    @staticmethod
    def _reconcile_member_cardinality(
        connection: Any,
        plan: AtomicCommitPlan,
    ) -> bool:
        """Prove there are no unmanifested commit members or result links."""

        row = connection.execute(
            """
            SELECT
              (SELECT count(*) FROM business_operations WHERE commit_id = ?),
              (SELECT count(*) FROM business_operation_targets AS target
                 JOIN business_operations AS operation
                   ON operation.operation_id = target.operation_id
                WHERE operation.commit_id = ?),
              (SELECT count(*) FROM event_outbox WHERE commit_id = ?),
              (SELECT count(*) FROM deferred_actions WHERE origin_commit_id = ?),
              (SELECT count(*) FROM pipeline_job_transitions
                WHERE origin_commit_id = ?),
              (SELECT count(*) FROM document_asset_refs WHERE commit_id = ?),
              (SELECT count(*) FROM idempotency_inbox WHERE commit_id = ?),
              (SELECT count(*) FROM confirmation_requests
                WHERE result_commit_id = ?)
            """,
            (plan.commit_id,) * 8,
        ).fetchone()
        if not _row_is(
            row,
            (
                len(plan.operations),
                len(plan.operation_targets),
                len(plan.outbox),
                len(plan.durable_actions),
                len(plan.pipeline_intents),
                plan.asset_references.reference_count,
                0 if plan.inbox is None else 1,
                0 if plan.confirmation is None else 1,
            ),
        ):
            return False

        # Current entity rows are mutable rather than commit-scoped history.
        # Verify their exact cardinality only while this commit is still the
        # current head; after a later valid commit, the immutable manifest and
        # append-only members above remain the historical proof.
        head = connection.execute(
            """
            SELECT repository_project.generation, repository_project.status,
                   repository_project.current_snapshot_sha256,
                   document_head.generation, document_head.status,
                   document_head.commit_id,
                   document_head.business_root_sha256,
                   document_head.current_snapshot_sha256,
                   document_head.current_project_snapshot_sha256,
                   document_head.entity_count
            FROM repository_projects AS repository_project
            JOIN document_commit_heads AS document_head
              ON document_head.project_id = repository_project.project_id
            WHERE repository_project.project_id = ?
              AND document_head.document_id = ?
            """,
            (
                plan.before.project_id,
                plan.before.document_id,
            ),
        ).fetchone()
        if (
            head is None
            or type(head[0]) is not int
            or type(head[3]) is not int
            or head[0] != head[3]
            or head[0] < plan.before.generation
            or head[1] != head[4]
        ):
            return False
        if head[0] > plan.before.generation:
            return True
        if (
            head[1] == "live"
            and head[4] == "live"
            and head[5] == plan.commit_id
        ):
            if not _row_is(
                head,
                (
                    plan.before.generation,
                    "live",
                    plan.candidate.business_root_sha256,
                    plan.before.generation,
                    "live",
                    plan.commit_id,
                    plan.candidate.business_root_sha256,
                    plan.after_snapshot.document.snapshot_sha256,
                    plan.candidate.business_root_sha256,
                    len(plan.after_snapshot.entities),
                ),
            ) or not SQLiteBusinessTransactionWriter._current_snapshot_rows_match(
                connection,
                plan,
            ):
                return False
        return True

    @staticmethod
    def _current_snapshot_rows_match(
        connection: Any,
        plan: AtomicCommitPlan,
    ) -> bool:
        project = plan.after_snapshot.project
        project_row = connection.execute(
            """
            SELECT repository_singleton, generation, codec_id, codec_version,
                   snapshot_format_version, snapshot_bytes, snapshot_sha256
            FROM projects WHERE project_id = ?
            """,
            (project.project_id,),
        ).fetchone()
        if not _row_is(
            project_row,
            (
                1,
                project.generation,
                project.codec_id,
                project.codec_version,
                project.snapshot_format_version,
                project.snapshot_bytes,
                project.snapshot_sha256,
            ),
        ):
            return False

        document = plan.after_snapshot.document
        document_row = connection.execute(
            """
            SELECT document_id, document_version, schema_version,
                   collaboration_mode, save_policy, codec_id, codec_version,
                   snapshot_format_version, snapshot_bytes, snapshot_sha256
            FROM documents WHERE project_id = ?
            """,
            (document.project_id,),
        ).fetchone()
        if not _row_is(
            document_row,
            (
                document.document_id,
                document.document_version,
                document.schema_version,
                document.collaboration_mode,
                document.save_policy,
                document.codec_id,
                document.codec_version,
                document.snapshot_format_version,
                document.snapshot_bytes,
                document.snapshot_sha256,
            ),
        ):
            return False

        entity_rows = connection.execute(
            """
            SELECT project_id, document_id, entity_type, entity_id,
                   entity_version, deleted, codec_id, codec_version,
                   snapshot_format_version, snapshot_bytes, snapshot_sha256
            FROM entities
            WHERE project_id = ? AND document_id = ?
            ORDER BY entity_type, entity_id
            """,
            (document.project_id, document.document_id),
        ).fetchall()
        if len(entity_rows) != len(plan.after_snapshot.entities):
            return False
        for row, entity in zip(entity_rows, plan.after_snapshot.entities):
            if not _row_is(
                row,
                (
                    entity.project_id,
                    entity.document_id,
                    entity.entity_type,
                    entity.entity_id,
                    entity.entity_version,
                    entity.deleted,
                    entity.codec_id,
                    entity.codec_version,
                    entity.snapshot_format_version,
                    entity.snapshot_bytes,
                    entity.snapshot_sha256,
                ),
            ):
                return False
        return True

    @staticmethod
    def _reconcile_inbox(connection: Any, plan: AtomicCommitPlan) -> bool:
        inbox = plan.inbox
        if inbox is None:
            return True
        row = connection.execute(
            """
            SELECT request_sha256, request_bytes, request_schema,
                   request_codec_id, request_codec_version, actor_id,
                   claim_owner_id, claim_fence, status, state_revision,
                   lease_expires_at, result_kind, commit_id, confirmation_id,
                   response_bytes, response_sha256, response_schema,
                   response_codec_id, response_codec_version
            FROM idempotency_inbox
            WHERE scope = ? AND idempotency_key = ?
            """,
            (inbox.scope, inbox.inbox_key),
        ).fetchone()
        return _row_is(
            row,
            (
                inbox.request_sha256,
                inbox.request.payload_bytes,
                inbox.request.schema,
                inbox.request.codec_id,
                inbox.request.codec_version,
                inbox.actor_id,
                inbox.claim_owner_id,
                inbox.claim_fence,
                "committed",
                inbox.state_revision + 1,
                None,
                "commit",
                plan.commit_id,
                None,
                inbox.response.payload_bytes,
                inbox.response.payload_sha256,
                inbox.response.schema,
                inbox.response.codec_id,
                inbox.response.codec_version,
            ),
        )

    @staticmethod
    def _reconcile_confirmation(connection: Any, plan: AtomicCommitPlan) -> bool:
        confirmation = plan.confirmation
        if confirmation is None:
            return True
        row = connection.execute(
            """
            SELECT project_id, generation, document_id, actor_id,
                   approval_nonce_hmac, nonce_key_id, request_sha256, status,
                   state_revision, reservation_owner_id, reservation_fence,
                   reservation_expires_at, decision, resolution_bytes,
                   resolution_sha256, resolution_schema, resolution_codec_id,
                   resolution_codec_version, result_commit_id
            FROM confirmation_requests WHERE confirmation_id = ?
            """,
            (confirmation.confirmation_id,),
        ).fetchone()
        return _row_is(
            row,
            (
                plan.before.project_id,
                plan.before.generation,
                plan.before.document_id,
                confirmation.actor_id,
                confirmation.nonce_hmac,
                confirmation.nonce_key_id,
                confirmation.expected_request_sha256,
                "consumed",
                confirmation.expected_revision + 1,
                None,
                None,
                None,
                "confirmed",
                confirmation.resolution.payload_bytes,
                confirmation.resolution.payload_sha256,
                confirmation.resolution.schema,
                confirmation.resolution.codec_id,
                confirmation.resolution.codec_version,
                plan.commit_id,
            ),
        )


__all__ = ()
