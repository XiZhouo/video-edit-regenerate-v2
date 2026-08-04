"""Private fixed-SQL finalizer for the schema-v12 phase-1 cutover head.

The V12 install writer deliberately commits its closure before any cutover
head exists.  This leaf owns exactly the next durable transition: an exact
``INSTALLED_CLOSURE`` becomes the canonical ``INSTALLED_HEAD`` in a separate
``BEGIN IMMEDIATE`` transaction.  It cannot write coverage, audit,
activation, mount, runtime-control, receipt, or business rows.

The plan-free durable auditor is used only for read-only reconciliation.  Its
detached result never authorizes the write.  The write transaction rebuilds
its authority from current schema/security state, canonical receipt
namespaces, the stored install manifest, the V12 semantic closure reader, the
exact read-only runtime, and an empty post-install suffix.  It then derives
the phase head through the frozen pure issuer, performs one fixed INSERT,
reads every column back, and repeats the complete proof before COMMIT.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hmac
import sqlite3
from typing import Any, Callable, Optional, Tuple

from business_cutover_head_integrity import (
    BusinessCutoverHeadIntegrityError,
    BusinessCutoverHeadPointer,
    BusinessCutoverHeadPointerRole,
    BusinessCutoverHeadState,
    _issue_initial_business_cutover_head,
)
from business_cutover_stored_manifest import (
    BusinessCutoverStoredManifestError,
    _parse_stored_install_manifest,
)
from sqlite_business_audit import _read_receipt_namespace_identity
from sqlite_business_runtime import (
    BusinessAuthorityState,
    BusinessRuntimeAccess,
    _validate_business_runtime_connection,
)
from sqlite_cutover_durable_audit import (
    SQLiteCutoverDurableAuditStatus,
    SQLiteCutoverDurableAuditor,
    SQLiteCutoverDurableIdentity,
    SQLiteCutoverDurablePhase,
)
from sqlite_cutover_install_closure_reader import (
    SQLiteCutoverInstallClosureError,
    _read_business_cutover_install_closure,
    _require_cutover_install_closure_binding,
)
from sqlite_cutover_verification import (
    _CutoverVerificationBudgetExceeded,
    _CutoverVerificationCorrupt,
    _failure_is_unavailable,
    _install_cutover_verification_progress_budget,
    _read_cutover_verification_budget,
    _verify_legacy_receipt_namespace,
    _verify_production_receipt_namespace,
)
from sqlite_kernel import (
    SQLiteKernelError,
    SqliteDatabase,
    _allow_authorizer,
    _expected_schema_fingerprint,
)
from sqlite_production_authority import _CUTOVER_HEAD_ROW_COLUMNS
from sqlite_schema import MIGRATIONS, _expected_migration_ledger_sha256


_CUTOVER_PHASE_HEAD_WRITER_CAPABILITY = object()


class SQLiteCutoverPhaseHeadStatus(str, Enum):
    EXACT = "exact"
    ABSENT = "absent"
    CONFLICT = "conflict"
    CORRUPT = "corrupt"
    UNKNOWN = "unknown"


class SQLiteCutoverPhaseHeadTestPoint(str, Enum):
    BEFORE_COMMIT = "before_commit"
    AFTER_COMMIT_BEFORE_ACK = "after_commit_before_ack"


@dataclass(frozen=True)
class SQLiteCutoverPhaseHeadResult:
    status: SQLiteCutoverPhaseHeadStatus
    code: str
    identity: Optional[SQLiteCutoverDurableIdentity] = None
    recovered_after_ack_loss: bool = False

    def __post_init__(self) -> None:
        if type(self.status) is not SQLiteCutoverPhaseHeadStatus:
            raise TypeError("status must be an exact phase-head status")
        if type(self.code) is not str or not self.code:
            raise TypeError("code must be a non-empty exact string")
        if self.identity is not None and type(self.identity) is not SQLiteCutoverDurableIdentity:
            raise TypeError("identity must be an exact durable identity")
        if self.status is SQLiteCutoverPhaseHeadStatus.EXACT:
            if (
                self.identity is None
                or self.identity.phase
                is SQLiteCutoverDurablePhase.INSTALLED_CLOSURE
            ):
                raise TypeError("EXACT requires a finalized phase-head identity")
        elif self.status is SQLiteCutoverPhaseHeadStatus.ABSENT:
            if (
                self.identity is not None
                and self.identity.phase
                is not SQLiteCutoverDurablePhase.INSTALLED_CLOSURE
            ):
                raise TypeError("ABSENT may carry only the ready closure identity")
        elif self.identity is not None:
            raise TypeError("identity is forbidden for an uncertain result")
        if type(self.recovered_after_ack_loss) is not bool or (
            self.recovered_after_ack_loss
            and self.status is not SQLiteCutoverPhaseHeadStatus.EXACT
        ):
            raise TypeError("recovered_after_ack_loss is invalid")

    @property
    def phase(self) -> Optional[SQLiteCutoverDurablePhase]:
        return None if self.identity is None else self.identity.phase


class _PhaseHeadConflict(RuntimeError):
    pass


class _PhaseHeadCorrupt(RuntimeError):
    pass


@dataclass(frozen=True)
class _PhaseHeadSnapshot:
    state: BusinessCutoverHeadState
    closure_manifest_sha256: bytes
    source_proof_sha256: bytes
    source_admission_sha256: bytes
    coverage_transaction_id: str
    coverage_manifest_sha256: bytes
    coverage_sha256: bytes
    baseline_receipt_root_sha256: bytes
    source_admission_revision: int
    repository_revision: int
    runtime_state_revision: int
    schema_fingerprint: str
    migration_ledger_sha256: bytes
    receipt_namespace_count: int
    receipt_namespace_sha256: bytes


_POST_INSTALL_SUFFIX_TABLES = (
    "repository_audit_certificates",
    "repository_audit_head",
    "business_cutover_activation_receipts",
    "business_cutover_mount_receipts",
    "atomic_business_control_members",
    "worker_ledger_entries",
    "worker_execution_receipts",
    "pipeline_control_intents",
    "pipeline_control_receipts",
    "worker_targets",
    "worker_target_current",
    "worker_attempts",
    "worker_effects",
    "worker_results",
    "worker_reconcile_proofs",
    "outbox_allocations",
    "worker_effect_request_projections",
    "worker_result_payload_projections",
    "worker_proof_payload_projections",
)

_INSERT_PHASE_HEAD_SQL = """
INSERT INTO business_cutover_head(
    singleton,cutover_id,state_revision,phase_revision,phase,authority_state,
    repository_epoch,runtime_generation,code_digest,install_transaction_id,
    install_manifest_sha256,coverage_transaction_id,
    coverage_manifest_sha256,audit_transaction_id,audit_manifest_sha256,
    activation_transaction_id,activation_manifest_sha256,
    mount_transaction_id,mount_manifest_sha256,previous_head_sha256,
    head_sha256,updated_at
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""
_FIXED_WRITE_SQL = frozenset((_INSERT_PHASE_HEAD_SQL,))

_READ_ONLY_PRAGMAS_WITHOUT_ARGUMENT = frozenset(
    (
        "application_id",
        "busy_timeout",
        "checkpoint_fullfsync",
        "encoding",
        "foreign_keys",
        "fullfsync",
        "journal_mode",
        "schema_version",
        "synchronous",
        "trusted_schema",
        "user_version",
    )
)
_READ_ONLY_PRAGMAS_WITH_ARGUMENT = frozenset(
    (
        "foreign_key_list",
        "index_info",
        "index_list",
        "index_xinfo",
        "integrity_check",
        "table_info",
        "table_xinfo",
    )
)

_FORBIDDEN_ACTIONS = frozenset(
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
        "SQLITE_CREATE_VTABLE",
        "SQLITE_DETACH",
        "SQLITE_DROP_INDEX",
        "SQLITE_DROP_TABLE",
        "SQLITE_DROP_TEMP_INDEX",
        "SQLITE_DROP_TEMP_TABLE",
        "SQLITE_DROP_TEMP_TRIGGER",
        "SQLITE_DROP_TEMP_VIEW",
        "SQLITE_DROP_TRIGGER",
        "SQLITE_DROP_VIEW",
        "SQLITE_DROP_VTABLE",
        "SQLITE_REINDEX",
        "SQLITE_TRANSACTION",
        "SQLITE_SAVEPOINT",
    )
    if hasattr(sqlite3, name)
)


class _PhaseHeadAuthorizer:
    __slots__ = ("denied", "_head_insert_open")

    def __init__(self) -> None:
        self.denied = False
        self._head_insert_open = False

    def open_head_insert(self) -> None:
        if self._head_insert_open:
            raise RuntimeError("phase-head INSERT authority is already open")
        self._head_insert_open = True

    def close_head_insert(self) -> None:
        self._head_insert_open = False

    def __call__(
        self,
        action: int,
        arg1: Optional[str],
        arg2: Optional[str],
        database_name: Optional[str],
        trigger_name: Optional[str],
    ) -> int:
        table = (arg1 or "").lower()
        database = (database_name or "").lower()
        insert = getattr(sqlite3, "SQLITE_INSERT", -1)
        update = getattr(sqlite3, "SQLITE_UPDATE", -1)
        delete = getattr(sqlite3, "SQLITE_DELETE", -1)
        pragma = getattr(sqlite3, "SQLITE_PRAGMA", -1)
        mutation = action in (insert, update, delete)
        read_only_pragma = bool(
            action == pragma
            and (
                (
                    table in _READ_ONLY_PRAGMAS_WITHOUT_ARGUMENT
                    and arg2 is None
                )
                or (
                    table in _READ_ONLY_PRAGMAS_WITH_ARGUMENT
                    and arg2 is not None
                )
            )
        )
        denied = bool(
            action in _FORBIDDEN_ACTIONS
            or (action == pragma and not read_only_pragma)
            or action in (update, delete)
            or (mutation and database != "main")
            or (mutation and trigger_name is not None)
            or (
                action == insert
                and (
                    table != "business_cutover_head"
                    or not self._head_insert_open
                )
            )
        )
        if denied:
            self.denied = True
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK


def _same(left: Any, right: Any) -> bool:
    if type(left) is bytes and type(right) is bytes:
        return hmac.compare_digest(left, right)
    return type(left) is type(right) and left == right


def _row_exact(row: Any, expected: Tuple[Any, ...]) -> bool:
    return row is not None and len(row) == len(expected) and all(
        _same(row[index], value) for index, value in enumerate(expected)
    )


def _reset_authorizer(connection: Any) -> None:
    try:
        connection.set_authorizer(_allow_authorizer)
    except Exception:
        pass


def _rollback_close(connection: Any) -> bool:
    complete = True
    _reset_authorizer(connection)
    try:
        if connection.in_transaction:
            connection.rollback()
    except Exception:
        complete = False
    try:
        connection.close()
    except Exception:
        complete = False
    return complete


def _close_quietly(connection: Any) -> bool:
    complete = True
    _reset_authorizer(connection)
    try:
        connection.close()
    except Exception:
        complete = False
    return complete


def _process_control_cleanup(connection: Any, *, commit_started: bool) -> None:
    try:
        if commit_started:
            _close_quietly(connection)
        else:
            _rollback_close(connection)
    except BaseException:
        try:
            connection.close()
        except BaseException:
            pass


def _execute_fixed(connection: Any, sql: str, values: tuple) -> int:
    if type(sql) is not str or sql not in _FIXED_WRITE_SQL or type(values) is not tuple:
        raise _PhaseHeadCorrupt("cutover_phase_head_fixed_sql_invalid")
    cursor = connection.execute(sql, values)
    try:
        return cursor.rowcount
    finally:
        cursor.close()


class SQLiteCutoverPhaseHeadWriter:
    """Finalize and freshly reconcile only the canonical phase-1 head."""

    __slots__ = ("_database",)

    def __init__(self, database: SqliteDatabase, *, _capability: Any) -> None:
        if type(database) is not SqliteDatabase:
            raise TypeError("database must be an exact SqliteDatabase")
        if _capability is not _CUTOVER_PHASE_HEAD_WRITER_CAPABILITY:
            raise TypeError("cutover phase-head writer capability is required")
        self._database = database

    @staticmethod
    def _mapped_result(
        result: Any,
        *,
        recovered: bool = False,
    ) -> SQLiteCutoverPhaseHeadResult:
        status = result.status
        identity = result.identity
        if status is SQLiteCutoverDurableAuditStatus.EXACT:
            if identity.phase is SQLiteCutoverDurablePhase.INSTALLED_CLOSURE:
                return SQLiteCutoverPhaseHeadResult(
                    SQLiteCutoverPhaseHeadStatus.ABSENT,
                    "cutover_phase_head_installed_closure_ready",
                    identity,
                )
            elif identity.phase is SQLiteCutoverDurablePhase.INSTALLED_HEAD:
                code = "cutover_phase_head_installed_head"
            else:
                code = "cutover_phase_head_already_advanced"
            return SQLiteCutoverPhaseHeadResult(
                SQLiteCutoverPhaseHeadStatus.EXACT,
                code,
                identity,
                recovered,
            )
        mapped = {
            SQLiteCutoverDurableAuditStatus.ABSENT: (
                SQLiteCutoverPhaseHeadStatus.ABSENT
            ),
            SQLiteCutoverDurableAuditStatus.CONFLICT: (
                SQLiteCutoverPhaseHeadStatus.CONFLICT
            ),
            SQLiteCutoverDurableAuditStatus.CORRUPT: (
                SQLiteCutoverPhaseHeadStatus.CORRUPT
            ),
            SQLiteCutoverDurableAuditStatus.UNKNOWN: (
                SQLiteCutoverPhaseHeadStatus.UNKNOWN
            ),
        }[status]
        return SQLiteCutoverPhaseHeadResult(
            mapped,
            (
                "cutover_phase_head_no_install"
                if status is SQLiteCutoverDurableAuditStatus.ABSENT
                else "cutover_phase_head_" + mapped.value
            ),
        )

    def reconcile(self) -> SQLiteCutoverPhaseHeadResult:
        result = self._mapped_result(
            SQLiteCutoverDurableAuditor(self._database).inspect()
        )
        if result.status is SQLiteCutoverPhaseHeadStatus.EXACT:
            try:
                self._database._finalize_committed_storage()
            except Exception:
                return SQLiteCutoverPhaseHeadResult(
                    SQLiteCutoverPhaseHeadStatus.UNKNOWN,
                    "cutover_phase_head_durability_barrier_failed",
                )
        return result

    @staticmethod
    def _count(connection: Any, table: str) -> int:
        row = connection.execute('SELECT count(*) FROM "' + table + '"').fetchone()
        if row is None or len(row) != 1 or type(row[0]) is not int or row[0] < 0:
            raise _PhaseHeadCorrupt("cutover_phase_head_count_invalid")
        return row[0]

    def _prove_snapshot(
        self,
        connection: Any,
        *,
        expected_phase_revision: int,
    ) -> _PhaseHeadSnapshot:
        if type(expected_phase_revision) is not int or expected_phase_revision not in (0, 1):
            raise TypeError("expected_phase_revision must be zero or one")
        # These are direct transaction-scoped proofs.  No detached audit
        # identity participates in issuing the write authority.
        profile = _validate_business_runtime_connection(
            self._database,
            connection,
            access=BusinessRuntimeAccess.READER,
        )
        _read_cutover_verification_budget(connection)
        _verify_legacy_receipt_namespace(connection)
        _verify_production_receipt_namespace(connection)
        receipt_count, receipt_root = _read_receipt_namespace_identity(connection)
        install_rows = connection.execute(
            "SELECT i.cutover_id,i.transaction_id,i.receipt_kind,i.committed_at,"
            "i.repository_epoch,i.runtime_generation,i.code_digest,"
            "i.member_mapping_sha256,i.manifest_bytes,i.manifest_sha256,"
            "r.kind,r.committed_at FROM business_cutover_install_receipts AS i "
            "JOIN transaction_receipts AS r ON r.transaction_id=i.transaction_id "
            "ORDER BY i.cutover_id"
        ).fetchall()
        if len(install_rows) != 1:
            raise _PhaseHeadCorrupt("cutover_phase_head_install_singleton_invalid")
        row = install_rows[0]
        try:
            stored = _parse_stored_install_manifest(row[8])
        except BusinessCutoverStoredManifestError as exc:
            raise _PhaseHeadCorrupt("cutover_phase_head_install_manifest_invalid") from exc
        if (
            stored.target_schema_version != MIGRATIONS[-1].version
            or stored.target_schema_fingerprint != _expected_schema_fingerprint()
        ):
            raise _PhaseHeadCorrupt("cutover_phase_head_install_target_stale")
        install = stored.evidence
        if not _row_exact(
            row,
            (
                install.cutover_id,
                install.transaction_id,
                install.receipt_kind,
                row[3],
                install.runtime.repository_epoch,
                install.runtime.runtime_generation,
                install.runtime.code_digest,
                install.member_mapping_sha256,
                install.manifest_bytes,
                install.manifest_sha256,
                install.receipt_kind,
                row[3],
            ),
        ):
            raise _PhaseHeadCorrupt("cutover_phase_head_install_row_invalid")
        now = connection.execute(
            "SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')"
        ).fetchone()
        if (
            now is None
            or len(now) != 1
            or type(now[0]) is not str
            or type(row[3]) is not str
            or row[3] > now[0]
        ):
            raise _PhaseHeadCorrupt("cutover_phase_head_install_time_invalid")
        try:
            closure = _require_cutover_install_closure_binding(
                _read_business_cutover_install_closure(
                    connection,
                    cutover_id=install.cutover_id,
                    install_transaction_id=install.transaction_id,
                    install_manifest_sha256=install.manifest_sha256,
                    install_committed_at=row[3],
                    target_schema_version=stored.target_schema_version,
                    target_schema_fingerprint=stored.target_schema_fingerprint,
                    require_current_install_snapshot=True,
                )
            )
        except SQLiteCutoverInstallClosureError as exc:
            raise _PhaseHeadCorrupt("cutover_phase_head_closure_invalid") from exc
        runtime = install.runtime
        if (
            profile.authority != runtime.authority
            or profile.authority_state is not BusinessAuthorityState.READ_ONLY
            or profile.authority_state.value != runtime.authority_state
            or profile.repository_epoch != runtime.repository_epoch
            or profile.cutover_id != install.cutover_id
            or profile.source_admission_revision != runtime.source_admission_revision
            or profile.repository_revision != runtime.repository_revision
            or profile.state_revision != 1
            or profile.state_revision != runtime.state_revision
            or profile.runtime_generation != runtime.runtime_generation
            or not hmac.compare_digest(profile.code_digest, runtime.code_digest)
            or profile.schema_version != runtime.schema_version
            or profile.updated_at != row[3]
        ):
            raise _PhaseHeadCorrupt("cutover_phase_head_runtime_invalid")
        if (
            self._count(connection, "business_commits")
            != closure.baseline_commit_count
            or self._count(connection, "atomic_business_write_receipts")
            != closure.baseline_commit_count
            or self._count(connection, "business_cutover_baseline_atomic_receipts")
            != closure.baseline_commit_count
            or any(self._count(connection, table) for table in _POST_INSTALL_SUFFIX_TABLES)
        ):
            raise _PhaseHeadCorrupt("cutover_phase_head_suffix_invalid")
        coverage_rows = connection.execute(
            "SELECT transaction_id,manifest_sha256,coverage_sha256 "
            "FROM business_asset_coverage_receipts ORDER BY cutover_id"
        ).fetchall()
        if (
            len(coverage_rows) != 1
            or coverage_rows[0][1] != closure.coverage_manifest_sha256
            or coverage_rows[0][2] != closure.coverage_sha256
        ):
            raise _PhaseHeadCorrupt("cutover_phase_head_coverage_invalid")
        try:
            state = _issue_initial_business_cutover_head(
                cutover_id=install.cutover_id,
                repository_epoch=runtime.repository_epoch,
                runtime_generation=runtime.runtime_generation,
                code_digest=runtime.code_digest,
                install=BusinessCutoverHeadPointer(
                    BusinessCutoverHeadPointerRole.INSTALL,
                    install.transaction_id,
                    install.receipt_kind,
                    row[3],
                    install.manifest_sha256,
                ),
            )
        except BusinessCutoverHeadIntegrityError as exc:
            raise _PhaseHeadCorrupt("cutover_phase_head_projection_invalid") from exc
        head_rows = connection.execute(
            "SELECT " + ",".join(_CUTOVER_HEAD_ROW_COLUMNS)
            + " FROM business_cutover_head ORDER BY singleton"
        ).fetchall()
        expected_row = tuple(
            state.row_values()[name] for name in _CUTOVER_HEAD_ROW_COLUMNS
        )
        if expected_phase_revision == 0:
            if head_rows:
                raise _PhaseHeadConflict("cutover_phase_head_raced")
        elif len(head_rows) != 1 or not _row_exact(head_rows[0], expected_row):
            raise _PhaseHeadCorrupt("cutover_phase_head_readback_invalid")
        return _PhaseHeadSnapshot(
            state=state,
            closure_manifest_sha256=closure.closure_manifest_sha256,
            source_proof_sha256=closure.source_proof_sha256,
            source_admission_sha256=closure.source_admission_sha256,
            coverage_transaction_id=coverage_rows[0][0],
            coverage_manifest_sha256=closure.coverage_manifest_sha256,
            coverage_sha256=closure.coverage_sha256,
            baseline_receipt_root_sha256=closure.baseline_receipt_root_sha256,
            source_admission_revision=profile.source_admission_revision,
            repository_revision=profile.repository_revision,
            runtime_state_revision=profile.state_revision,
            schema_fingerprint=_expected_schema_fingerprint(),
            migration_ledger_sha256=_expected_migration_ledger_sha256(),
            receipt_namespace_count=receipt_count,
            receipt_namespace_sha256=receipt_root,
        )

    @staticmethod
    def _matches_detached(
        snapshot: _PhaseHeadSnapshot,
        identity: SQLiteCutoverDurableIdentity,
    ) -> bool:
        state = snapshot.state
        return (
            identity.phase is SQLiteCutoverDurablePhase.INSTALLED_CLOSURE
            and identity.cutover_id == state.cutover_id
            and identity.repository_epoch == state.repository_epoch
            and identity.source_admission_revision == snapshot.source_admission_revision
            and identity.runtime_generation == state.runtime_generation
            and hmac.compare_digest(identity.code_digest, state.code_digest)
            and identity.authority_state == state.authority_state
            and identity.repository_revision == snapshot.repository_revision
            and identity.runtime_state_revision == snapshot.runtime_state_revision
            and identity.install_transaction_id == state.pointers[0].transaction_id
            and hmac.compare_digest(
                identity.install_manifest_sha256,
                state.pointers[0].manifest_sha256,
            )
            and hmac.compare_digest(
                identity.closure_manifest_sha256,
                snapshot.closure_manifest_sha256,
            )
            and hmac.compare_digest(identity.source_proof_sha256, snapshot.source_proof_sha256)
            and hmac.compare_digest(
                identity.source_admission_sha256,
                snapshot.source_admission_sha256,
            )
            and identity.coverage_transaction_id == snapshot.coverage_transaction_id
            and hmac.compare_digest(
                identity.coverage_manifest_sha256,
                snapshot.coverage_manifest_sha256,
            )
            and hmac.compare_digest(identity.coverage_sha256, snapshot.coverage_sha256)
            and hmac.compare_digest(
                identity.baseline_receipt_root_sha256,
                snapshot.baseline_receipt_root_sha256,
            )
            and identity.head_sha256 is None
            and identity.schema_fingerprint == snapshot.schema_fingerprint
            and hmac.compare_digest(
                identity.migration_ledger_sha256,
                snapshot.migration_ledger_sha256,
            )
            and identity.receipt_namespace_count
            == snapshot.receipt_namespace_count
            and hmac.compare_digest(
                identity.receipt_namespace_sha256,
                snapshot.receipt_namespace_sha256,
            )
        )

    @staticmethod
    def _same_install_lineage(
        left: SQLiteCutoverDurableIdentity,
        right: SQLiteCutoverDurableIdentity,
    ) -> bool:
        return (
            left.cutover_id == right.cutover_id
            and left.repository_epoch == right.repository_epoch
            and left.source_admission_revision == right.source_admission_revision
            and left.runtime_generation == right.runtime_generation
            and hmac.compare_digest(left.code_digest, right.code_digest)
            and left.authority_state == right.authority_state == "read_only"
            and left.repository_revision == right.repository_revision
            and left.runtime_state_revision == right.runtime_state_revision
            and left.install_transaction_id == right.install_transaction_id
            and hmac.compare_digest(
                left.install_manifest_sha256,
                right.install_manifest_sha256,
            )
            and hmac.compare_digest(
                left.closure_manifest_sha256,
                right.closure_manifest_sha256,
            )
            and hmac.compare_digest(left.source_proof_sha256, right.source_proof_sha256)
            and hmac.compare_digest(
                left.source_admission_sha256,
                right.source_admission_sha256,
            )
            and left.coverage_transaction_id == right.coverage_transaction_id
            and hmac.compare_digest(
                left.coverage_manifest_sha256,
                right.coverage_manifest_sha256,
            )
            and hmac.compare_digest(left.coverage_sha256, right.coverage_sha256)
            and hmac.compare_digest(
                left.baseline_receipt_root_sha256,
                right.baseline_receipt_root_sha256,
            )
            and left.schema_fingerprint == right.schema_fingerprint
            and hmac.compare_digest(
                left.migration_ledger_sha256,
                right.migration_ledger_sha256,
            )
            and left.receipt_namespace_count == right.receipt_namespace_count
            and hmac.compare_digest(
                left.receipt_namespace_sha256,
                right.receipt_namespace_sha256,
            )
        )

    @staticmethod
    def _insert_state(connection: Any, state: BusinessCutoverHeadState) -> None:
        values = state.row_values()
        row = tuple(values[name] for name in _CUTOVER_HEAD_ROW_COLUMNS)
        if _execute_fixed(connection, _INSERT_PHASE_HEAD_SQL, row) != 1:
            raise _PhaseHeadCorrupt("cutover_phase_head_insert_failed")

    @staticmethod
    def _row_readback_exact(connection: Any, state: BusinessCutoverHeadState) -> bool:
        rows = connection.execute(
            "SELECT " + ",".join(_CUTOVER_HEAD_ROW_COLUMNS)
            + " FROM business_cutover_head ORDER BY singleton"
        ).fetchall()
        values = state.row_values()
        expected = tuple(values[name] for name in _CUTOVER_HEAD_ROW_COLUMNS)
        return len(rows) == 1 and _row_exact(rows[0], expected)

    def finalize(
        self,
        *,
        _test_hook: Optional[Callable[[SQLiteCutoverPhaseHeadTestPoint], None]] = None,
    ) -> SQLiteCutoverPhaseHeadResult:
        if _test_hook is not None and not callable(_test_hook):
            raise TypeError("_test_hook must be callable")
        initial = self.reconcile()
        if initial.status in (
            SQLiteCutoverPhaseHeadStatus.EXACT,
        ):
            return SQLiteCutoverPhaseHeadResult(
                initial.status,
                initial.code,
                initial.identity,
                True,
            )
        if not (
            initial.status is SQLiteCutoverPhaseHeadStatus.ABSENT
            and initial.phase is SQLiteCutoverDurablePhase.INSTALLED_CLOSURE
            and initial.code == "cutover_phase_head_installed_closure_ready"
        ):
            return initial

        connection = None
        authorizer = None
        progress = None
        commit_started = False
        hook_failed_before_commit = False
        try:
            connection, _filesystem = self._database._open_unvalidated()
            connection.execute("BEGIN IMMEDIATE")
            progress = _install_cutover_verification_progress_budget(connection)
            authorizer = _PhaseHeadAuthorizer()
            connection.set_authorizer(authorizer)
            before = self._prove_snapshot(connection, expected_phase_revision=0)
            if not self._matches_detached(before, initial.identity):
                raise _PhaseHeadConflict("cutover_phase_head_identity_changed")
            authorizer.open_head_insert()
            try:
                self._insert_state(connection, before.state)
            finally:
                authorizer.close_head_insert()
            if authorizer.denied or not self._row_readback_exact(connection, before.state):
                raise _PhaseHeadCorrupt("cutover_phase_head_insert_readback_invalid")
            if _test_hook is not None:
                try:
                    _test_hook(SQLiteCutoverPhaseHeadTestPoint.BEFORE_COMMIT)
                except Exception:
                    hook_failed_before_commit = True
                    raise
            if authorizer.denied or not self._row_readback_exact(connection, before.state):
                raise _PhaseHeadCorrupt("cutover_phase_head_final_readback_invalid")
            after = self._prove_snapshot(connection, expected_phase_revision=1)
            if after != before:
                raise _PhaseHeadCorrupt("cutover_phase_head_final_proof_mismatch")
            _reset_authorizer(connection)
            authorizer = None
            commit_started = True
            connection.commit()
            if _test_hook is not None:
                _test_hook(SQLiteCutoverPhaseHeadTestPoint.AFTER_COMMIT_BEFORE_ACK)
            connection.close()
            connection = None
            reconciled = self.reconcile()
            if (
                reconciled.status is SQLiteCutoverPhaseHeadStatus.EXACT
                and reconciled.phase is SQLiteCutoverDurablePhase.INSTALLED_HEAD
            ):
                return SQLiteCutoverPhaseHeadResult(
                    reconciled.status,
                    "cutover_phase_head_committed",
                    reconciled.identity,
                )
            return reconciled
        except BaseException as failure:
            progress_failure = None if progress is None else progress.failure
            if progress_failure is not None:
                failure = progress_failure
            process_control = not isinstance(failure, Exception)
            cleanup_complete = True
            if connection is not None:
                if process_control:
                    _process_control_cleanup(connection, commit_started=commit_started)
                elif commit_started:
                    cleanup_complete = _close_quietly(connection)
                else:
                    cleanup_complete = _rollback_close(connection)
                connection = None
            if process_control:
                try:
                    self.reconcile()
                except BaseException:
                    pass
                raise failure
            if commit_started or not cleanup_complete:
                reconciled = self.reconcile()
                if reconciled.status in (
                    SQLiteCutoverPhaseHeadStatus.EXACT,
                ):
                    return SQLiteCutoverPhaseHeadResult(
                        reconciled.status,
                        reconciled.code,
                        reconciled.identity,
                        True,
                    )
                if not cleanup_complete:
                    return SQLiteCutoverPhaseHeadResult(
                        SQLiteCutoverPhaseHeadStatus.UNKNOWN,
                        "cutover_phase_head_cleanup_failed",
                    )
                return reconciled
            if hook_failed_before_commit:
                reconciled = self.reconcile()
                if (
                    reconciled.status is SQLiteCutoverPhaseHeadStatus.ABSENT
                    and reconciled.phase
                    is SQLiteCutoverDurablePhase.INSTALLED_CLOSURE
                ):
                    return SQLiteCutoverPhaseHeadResult(
                        reconciled.status,
                        "cutover_phase_head_rolled_back",
                        reconciled.identity,
                    )
                return reconciled
            if isinstance(failure, _PhaseHeadConflict):
                reconciled = self.reconcile()
                if (
                    reconciled.status is SQLiteCutoverPhaseHeadStatus.EXACT
                    and reconciled.phase
                    is SQLiteCutoverDurablePhase.INSTALLED_HEAD
                    and reconciled.identity is not None
                    and initial.identity is not None
                    and self._same_install_lineage(
                        initial.identity,
                        reconciled.identity,
                    )
                ):
                    return SQLiteCutoverPhaseHeadResult(
                        SQLiteCutoverPhaseHeadStatus.EXACT,
                        "cutover_phase_head_race_recovered",
                        reconciled.identity,
                        True,
                    )
                return SQLiteCutoverPhaseHeadResult(
                    SQLiteCutoverPhaseHeadStatus.CONFLICT,
                    str(failure) or "cutover_phase_head_conflict",
                )
            if authorizer is not None and authorizer.denied:
                return SQLiteCutoverPhaseHeadResult(
                    SQLiteCutoverPhaseHeadStatus.CORRUPT,
                    "cutover_phase_head_authorizer_denied",
                )
            if isinstance(failure, _CutoverVerificationBudgetExceeded):
                return SQLiteCutoverPhaseHeadResult(
                    SQLiteCutoverPhaseHeadStatus.UNKNOWN,
                    str(failure),
                )
            if _failure_is_unavailable(failure):
                return SQLiteCutoverPhaseHeadResult(
                    SQLiteCutoverPhaseHeadStatus.UNKNOWN,
                    "cutover_phase_head_storage_unavailable",
                )
            if isinstance(
                failure,
                (
                    _PhaseHeadCorrupt,
                    _CutoverVerificationCorrupt,
                    BusinessCutoverHeadIntegrityError,
                    BusinessCutoverStoredManifestError,
                    SQLiteCutoverInstallClosureError,
                    SQLiteKernelError,
                    sqlite3.IntegrityError,
                    sqlite3.DatabaseError,
                ),
            ):
                return SQLiteCutoverPhaseHeadResult(
                    SQLiteCutoverPhaseHeadStatus.CORRUPT,
                    str(failure) or "cutover_phase_head_corrupt",
                )
            return SQLiteCutoverPhaseHeadResult(
                SQLiteCutoverPhaseHeadStatus.UNKNOWN,
                "cutover_phase_head_internal_unavailable",
            )


__all__ = ()
