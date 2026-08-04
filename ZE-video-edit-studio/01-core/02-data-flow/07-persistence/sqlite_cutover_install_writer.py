"""Private fixed-SQL writer for one schema-v12 cutover install closure.

The writer consumes only an exact kernel database, this module's capability,
and an issuer-sealed :class:`BusinessCutoverInstallClosureProjection`.  It
does not acquire file authority, admit source work, create coverage evidence,
activate or mount the repository, or expose a general connection/callback
surface.

One ``BEGIN IMMEDIATE`` first proves that the V3 terminal shadow already in
``projects``/``documents``/``entities`` is byte-exact, then appends the V12
read-only install.  The install-closure row is deliberately the final DML.
The semantic closure reader runs twice before COMMIT and once through a fresh
snapshot after COMMIT; a successful result also crosses the kernel durability
barrier.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hmac
import sqlite3
from typing import Any, Callable, Optional, Tuple

from business_cutover_baseline_projection import (
    ASSET_BLOBS_COLUMNS,
    BUSINESS_COMMITS_COLUMNS,
    DOCUMENT_ASSET_REFS_COLUMNS,
    DOCUMENT_COMMIT_HEADS_COLUMNS,
    DOCUMENTS_COLUMNS,
    ENTITIES_COLUMNS,
    OUTBOX_DISPATCH_HEAD_COLUMNS,
    PROJECTS_COLUMNS,
    REPOSITORY_PROJECTS_COLUMNS,
    TRANSACTION_RECEIPTS_COLUMNS,
    WORKER_LEDGER_HEAD_COLUMNS,
)
from business_cutover_contract import BusinessCutoverInstallEvidence
from business_cutover_install_closure import (
    ATOMIC_BUSINESS_WRITE_RECEIPT_COLUMNS,
    BASELINE_ATOMIC_RECEIPT_COLUMNS,
    INSTALL_ARTIFACT_CHUNK_COLUMNS,
    INSTALL_ARTIFACT_COLUMNS,
    INSTALL_CLOSURE_COLUMNS,
    BusinessCutoverInstallClosureError,
    BusinessCutoverInstallClosureProjection,
    _require_business_cutover_install_closure,
)
from business_cutover_persistability import (
    BusinessCutoverPersistabilityError,
    _require_business_cutover_install_closure_persistability,
)
from sqlite_business_runtime import (
    BusinessRuntimeAccess,
    _validate_business_runtime_connection,
    _verify_security_profile,
)
from sqlite_cutover_install_closure_reader import (
    SQLiteCutoverInstallClosureError,
    _read_business_cutover_install_closure,
    _require_cutover_install_closure_binding,
)
from sqlite_kernel import (
    APPLICATION_ID,
    SQLiteKernelError,
    SqliteDatabase,
    _allow_authorizer,
    _expected_schema_fingerprint,
    _pragma_int,
)
from sqlite_schema import MIGRATIONS


_CUTOVER_INSTALL_WRITER_CAPABILITY = object()


class SQLiteCutoverInstallStatus(str, Enum):
    NOT_INSTALLED = "not_installed"
    INSTALLED_READ_ONLY = "installed_read_only"
    CONFLICT = "conflict"
    CORRUPT = "corrupt"
    UNKNOWN = "unknown"


class SQLiteCutoverInstallTestPoint(str, Enum):
    BEFORE_COMMIT = "before_commit"
    AFTER_COMMIT_BEFORE_ACK = "after_commit_before_ack"


@dataclass(frozen=True)
class SQLiteCutoverInstallResult:
    status: SQLiteCutoverInstallStatus
    cutover_id: str
    code: str
    evidence: Optional[BusinessCutoverInstallEvidence] = None
    recovered_after_ack_loss: bool = False

    def __post_init__(self) -> None:
        if type(self.status) is not SQLiteCutoverInstallStatus:
            raise TypeError("status must be an exact SQLiteCutoverInstallStatus")
        if (
            type(self.cutover_id) is not str
            or len(self.cutover_id) != 64
            or any(value not in "0123456789abcdef" for value in self.cutover_id)
        ):
            raise TypeError("cutover_id must be 64 lowercase hexadecimal characters")
        if type(self.code) is not str or not self.code:
            raise TypeError("code must be a non-empty exact string")
        installed = self.status is SQLiteCutoverInstallStatus.INSTALLED_READ_ONLY
        if installed != (type(self.evidence) is BusinessCutoverInstallEvidence):
            raise TypeError("evidence must be present exactly for an installed result")
        if type(self.recovered_after_ack_loss) is not bool or (
            self.recovered_after_ack_loss and not installed
        ):
            raise TypeError("recovered_after_ack_loss is invalid")


class SQLiteCutoverInstallError(ValueError):
    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        if type(code) is not str or not code:
            raise TypeError("code must be a non-empty exact string")
        self.code = code
        super().__init__(code)


class _InstallConflict(RuntimeError):
    pass


class _InstallCorrupt(RuntimeError):
    pass


_STORAGE_UNAVAILABLE_KERNEL_CODES = frozenset(
    (
        "sqlite_busy",
        "sqlite_locked",
        "sqlite_io_error",
        "sqlite_open_failed",
        "sqlite_database_missing",
        "sqlite_path_stat_failed",
        "sqlite_directory_permissions_failed",
        "sqlite_directory_sync_failed",
        "sqlite_file_permissions_failed",
        "sqlite_business_runtime_unreadable",
        "sqlite_file_stat_failed",
        "sqlite_identity_probe_failed",
        "sqlite_profile_failed",
    )
)
_STORAGE_UNAVAILABLE_SQLITE_PRIMARY_CODES = frozenset(
    code
    for code in (
        getattr(sqlite3, "SQLITE_ABORT", None),
        getattr(sqlite3, "SQLITE_BUSY", None),
        getattr(sqlite3, "SQLITE_CANTOPEN", None),
        getattr(sqlite3, "SQLITE_FULL", None),
        getattr(sqlite3, "SQLITE_INTERRUPT", None),
        getattr(sqlite3, "SQLITE_IOERR", None),
        getattr(sqlite3, "SQLITE_LOCKED", None),
        getattr(sqlite3, "SQLITE_NOMEM", None),
        getattr(sqlite3, "SQLITE_NOLFS", None),
        getattr(sqlite3, "SQLITE_PERM", None),
        getattr(sqlite3, "SQLITE_PROTOCOL", None),
        getattr(sqlite3, "SQLITE_READONLY", None),
    )
    if type(code) is int
)


def _is_storage_unavailable_failure(failure: BaseException) -> bool:
    current: Optional[BaseException] = failure
    seen = set()
    kernel_codes = []
    saw_os_failure = False
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, sqlite3.Error):
            error_code = getattr(current, "sqlite_errorcode", None)
            if type(error_code) is int:
                primary_code = error_code & 0xFF
                if primary_code in _STORAGE_UNAVAILABLE_SQLITE_PRIMARY_CODES:
                    return True
                # Any other concrete SQLite primary code is positive evidence
                # of a structural/programming failure, not unavailability.
                # This includes CORRUPT/NOTADB and SQLITE_ERROR from missing
                # schema objects.
                return False
            if isinstance(current, sqlite3.OperationalError):
                return True
            if isinstance(current, sqlite3.DatabaseError):
                # CPython/SQLite combinations may omit sqlite_errorcode for
                # NOTADB/CORRUPT raised by an identity PRAGMA.  Generic
                # DatabaseError (as opposed to OperationalError) is then the
                # remaining stable structural signal, not an I/O retry hint.
                return False
        elif isinstance(current, SQLiteKernelError):
            kernel_codes.append(current.code)
        elif isinstance(current, (OSError, MemoryError)):
            saw_os_failure = True
        current = current.__cause__ or current.__context__
    if isinstance(failure, SQLiteCutoverInstallClosureError):
        return failure.code == "cutover_install_closure_unavailable"
    return saw_os_failure or any(
        code in _STORAGE_UNAVAILABLE_KERNEL_CODES for code in kernel_codes
    )


def _same(left: Any, right: Any) -> bool:
    if type(left) is bytes and type(right) is bytes:
        return hmac.compare_digest(left, right)
    return type(left) is type(right) and left == right


def _row_values(row: Any) -> tuple:
    return tuple(row[index] for index in range(len(row)))


def _rows_exact(observed: Tuple[tuple, ...], expected: Tuple[tuple, ...]) -> bool:
    return len(observed) == len(expected) and all(
        len(left) == len(right)
        and all(_same(left[index], right[index]) for index in range(len(right)))
        for left, right in zip(observed, expected)
    )


_INSERT_TABLES = frozenset(
    (
        "transaction_receipts",
        "business_cutover_install_receipts",
        "repository_runtime_control",
        "worker_ledger_head",
        "outbox_allocation_head",
        "outbox_dispatch_head",
        "business_cutover_install_artifacts",
        "business_cutover_install_artifact_chunks",
        "asset_blobs",
        "business_commits",
        "repository_projects",
        "document_asset_refs",
        "document_commit_heads",
        "atomic_business_write_receipts",
        "business_cutover_baseline_atomic_receipts",
        "business_cutover_install_closures",
    )
)

_PRODUCTION_RECEIPT_PREFIXES = (
    "_business-cutover-install:",
    "_business-cutover-join:",
    "_business-cutover-asset-stage:",
    "_business-cutover-asset-coverage:",
    "_repository-audit:",
    "_business-cutover-activation:",
    "_business-cutover-mount:",
    "_worker-execution:",
    "_pipeline-business-control:",
    "ze-business:",
)
_PRODUCTION_RECEIPT_KINDS = (
    "_business_cutover_install_v1",
    "_business_cutover_join_v1",
    "_business_cutover_asset_stage_v1",
    "_business_cutover_asset_coverage_v1",
    "_repository_audit_certificate_v1",
    "_business_cutover_activation_v2",
    "_business_cutover_mount_v2",
    "_worker_execution_v3",
    "_pipeline_business_control_v2",
    "ze_business_commit_v1",
)
_SELECT_PRODUCTION_RECEIPTS_SQL = (
    "SELECT receipt.transaction_id,receipt.kind,"
    "((SELECT count(*) FROM business_asset_stage_receipts AS stage "
    "WHERE stage.transaction_id=receipt.transaction_id AND "
    "stage.receipt_kind=receipt.kind AND "
    "stage.committed_at=receipt.committed_at)+"
    "(SELECT count(*) FROM business_asset_coverage_receipts AS coverage "
    "WHERE coverage.transaction_id=receipt.transaction_id AND "
    "coverage.receipt_kind=receipt.kind AND "
    "coverage.committed_at=receipt.committed_at)) "
    "FROM transaction_receipts AS receipt WHERE "
    + " OR ".join(
        "substr(CAST(receipt.transaction_id AS TEXT),1,?)=?"
        for _value in _PRODUCTION_RECEIPT_PREFIXES
    )
    + " OR CAST(receipt.kind AS TEXT) IN ("
    + ",".join("?" for _value in _PRODUCTION_RECEIPT_KINDS)
    + ") ORDER BY receipt.transaction_id,receipt.kind"
)
_SELECT_PRODUCTION_RECEIPTS_VALUES = (
    tuple(
        value
        for prefix in _PRODUCTION_RECEIPT_PREFIXES
        for value in (len(prefix), prefix)
    )
    + _PRODUCTION_RECEIPT_KINDS
)

_LEGACY_RECEIPT_PREFIXES = (
    "legacy-import:",
    "_legacy-catalog-seal:",
    "_legacy-repository-seal:",
)
_LEGACY_RECEIPT_KINDS = (
    "legacy_import_v1",
    "_legacy_catalog_seal_v1",
    "_legacy_repository_seal_v1",
)
_SELECT_LEGACY_RECEIPTS_SQL = (
    "SELECT receipt.transaction_id,receipt.kind,"
    "CASE WHEN typeof(receipt.committed_at)='text' AND "
    "length(CAST(receipt.committed_at AS BLOB))=24 AND "
    "strftime('%Y-%m-%dT%H:%M:%fZ',receipt.committed_at)="
    "receipt.committed_at THEN 1 ELSE 0 END "
    "FROM transaction_receipts AS receipt WHERE "
    + " OR ".join(
        "substr(CAST(receipt.transaction_id AS TEXT),1,?)=?"
        for _value in _LEGACY_RECEIPT_PREFIXES
    )
    + " OR CAST(receipt.kind AS TEXT) IN ("
    + ",".join("?" for _value in _LEGACY_RECEIPT_KINDS)
    + ") ORDER BY receipt.transaction_id,receipt.kind"
)
_SELECT_LEGACY_RECEIPTS_VALUES = (
    tuple(
        value for prefix in _LEGACY_RECEIPT_PREFIXES for value in (len(prefix), prefix)
    )
    + _LEGACY_RECEIPT_KINDS
)
_SELECT_LEGACY_RECEIPT_CHILDREN_SQL = (
    "SELECT transaction_id,receipt_kind FROM legacy_import_batches "
    "UNION ALL SELECT transaction_id,receipt_kind FROM legacy_catalog_seals "
    "UNION ALL SELECT transaction_id,receipt_kind FROM legacy_repository_seals "
    "ORDER BY transaction_id,receipt_kind"
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


class _CutoverInstallAuthorizer:
    """Allow only the fixed append surface and the one legacy->sqlite CAS."""

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
        table = (arg1 or "").lower()
        column = (arg2 or "").lower()
        database = (database_name or "").lower()
        insert = getattr(sqlite3, "SQLITE_INSERT", -1)
        update = getattr(sqlite3, "SQLITE_UPDATE", -1)
        delete = getattr(sqlite3, "SQLITE_DELETE", -1)
        mutation = action in (insert, update, delete)
        denied = bool(
            action in _FORBIDDEN_ACTIONS
            or action == getattr(sqlite3, "SQLITE_PRAGMA", -1)
            or action == delete
            or (mutation and database != "main")
            or (mutation and trigger_name is not None)
            or (action == insert and table not in _INSERT_TABLES)
            or (
                action == update
                and not (table == "kernel_repository" and column == "authority")
            )
        )
        if denied:
            self.denied = True
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK


_INSERT_GENERIC_RECEIPT_SQL = """
INSERT INTO transaction_receipts(transaction_id,kind,committed_at)
VALUES (?,?,?)
"""
_INSERT_INSTALL_RECEIPT_SQL = """
INSERT INTO business_cutover_install_receipts(
    cutover_id,transaction_id,receipt_kind,committed_at,
    repository_epoch,runtime_generation,code_digest,
    member_mapping_sha256,manifest_bytes,manifest_sha256
) VALUES (?,?,?,?,?,?,?,?,?,?)
"""
_CAS_REPOSITORY_MARKER_SQL = """
UPDATE kernel_repository
SET authority='sqlite'
WHERE singleton=1 AND authority='legacy'
"""
_INSERT_RUNTIME_SQL = """
INSERT INTO repository_runtime_control(
    singleton,authority,authority_state,repository_epoch,cutover_id,
    source_admission_revision,repository_revision,state_revision,
    runtime_generation,code_digest,schema_version,updated_at
) VALUES (1,?,?,?,?,?,?,?,?,?,?,?)
"""
_INSERT_WORKER_HEAD_SQL = """
INSERT INTO worker_ledger_head(
    singleton,state_revision,sequence,previous_tail_sha256,tail_sha256,updated_at
) VALUES (?,?,?,?,?,?)
"""
_INSERT_ALLOCATION_HEAD_SQL = """
INSERT INTO outbox_allocation_head(
    singleton,state_revision,sequence,previous_tail_sha256,tail_sha256,updated_at
) VALUES (?,?,?,?,?,?)
"""
_INSERT_DISPATCH_HEAD_SQL = """
INSERT INTO outbox_dispatch_head(
    singleton,state_revision,published_sequence,published_repository_revision,
    published_ordinal,inflight_sequence,inflight_repository_revision,
    inflight_ordinal,previous_head_sha256,head_sha256,updated_at
) VALUES (?,?,?,?,?,?,?,?,?,?,?)
"""
_INSERT_ARTIFACT_SQL = """
INSERT INTO business_cutover_install_artifacts(
    cutover_id,artifact_kind,artifact_schema,byte_length,chunk_count,artifact_sha256
) VALUES (?,?,?,?,?,?)
"""
_INSERT_ARTIFACT_CHUNK_SQL = """
INSERT INTO business_cutover_install_artifact_chunks(
    cutover_id,artifact_kind,ordinal,byte_offset,chunk_bytes,chunk_sha256
) VALUES (?,?,?,?,?,?)
"""
_INSERT_ASSET_BLOB_SQL = """
INSERT INTO asset_blobs(
    asset_sha256,content_key,media_type,extension,byte_length,status,
    state_revision,created_at,updated_at
) VALUES (?,?,?,?,?,?,?,?,?)
"""
_INSERT_BUSINESS_COMMIT_SQL = """
INSERT INTO business_commits(
    commit_id,transaction_id,receipt_kind,project_id,generation,
    repository_revision,repository_epoch,cutover_id,source_admission_revision,
    document_id,document_version,previous_commit_id,commit_kind,save_policy,
    trace_id,operation_count,operation_target_count,last_operation_id,
    last_operation_ordinal,outbox_event_count,last_outbox_event_id,
    last_outbox_ordinal,durable_action_count,pipeline_intent_count,
    before_root_sha256,after_root_sha256,previous_operation_tail_sha256,
    operation_tail_sha256,asset_projection_sha256,asset_reference_count,
    entity_count,manifest_bytes,manifest_sha256,committed_at,
    project_revision,head_revision
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""
_INSERT_REPOSITORY_PROJECT_SQL = """
INSERT INTO repository_projects(
    repository_singleton,project_id,generation,project_revision,status,
    current_snapshot_sha256,created_at,updated_at,deleted_at
) VALUES (?,?,?,?,?,?,?,?,?)
"""
_INSERT_DOCUMENT_ASSET_REF_SQL = """
INSERT INTO document_asset_refs(
    project_id,generation,document_id,document_version,commit_id,
    commit_asset_reference_count,ordinal,asset_sha256,entity_type,entity_id,
    entity_deleted,value_path,ref_mime,ref_size,ref_ext
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""
_INSERT_DOCUMENT_HEAD_SQL = """
INSERT INTO document_commit_heads(
    project_id,generation,document_id,document_version,commit_id,head_revision,
    status,current_snapshot_sha256,current_project_snapshot_sha256,
    business_root_sha256,operation_tail_sha256,asset_projection_sha256,
    asset_reference_count,entity_count,updated_at
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""
_INSERT_ATOMIC_RECEIPT_SQL = """
INSERT INTO atomic_business_write_receipts(
    commit_id,transaction_id,receipt_kind,committed_at,
    atomic_manifest_sha256,pipeline_intents_sha256,controls_sha256,
    control_count,before_worker_sequence,before_worker_tail_sha256,
    after_worker_sequence,after_worker_tail_sha256,manifest_bytes,
    manifest_sha256,receipt_sha256,production_manifest_bytes,
    production_manifest_sha256,target_count,targets_sha256,
    production_receipt_sha256
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""
_INSERT_BASELINE_RECEIPT_SQL = """
INSERT INTO business_cutover_baseline_atomic_receipts(
    cutover_id,member_ordinal,repository_revision,baseline_outer_schema,
    baseline_outer_protocol,member_mapping_kind,member_mapping_sha256,
    baseline_projection_kind,baseline_projection_sha256,commit_id,
    transaction_id,receipt_kind,committed_at,install_transaction_id,
    install_manifest_sha256,atomic_manifest_sha256,pipeline_intents_sha256,
    controls_sha256,control_count,before_worker_sequence,
    before_worker_tail_sha256,after_worker_sequence,after_worker_tail_sha256,
    manifest_bytes,manifest_sha256,receipt_sha256,
    baseline_outer_manifest_bytes,baseline_outer_manifest_sha256,
    target_count,targets_sha256,baseline_outer_receipt_sha256
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""
_INSERT_INSTALL_CLOSURE_SQL = """
INSERT INTO business_cutover_install_closures(
    cutover_id,install_transaction_id,install_manifest_sha256,
    source_transaction_id,source_receipt_kind,source_admission_revision,
    source_proof_manifest_sha256,source_proof_sha256,source_admission_sha256,
    coverage_transaction_id,coverage_receipt_kind,coverage_manifest_sha256,
    coverage_sha256,committed_at,member_mapping_kind,member_mapping_sha256,
    baseline_projection_kind,baseline_projection_sha256,project_count,
    live_project_count,deleted_project_count,baseline_commit_count,
    baseline_outer_receipt_root_sha256,manifest_bytes,manifest_sha256
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""

_FIXED_WRITE_SQL = frozenset(
    (
        _INSERT_GENERIC_RECEIPT_SQL,
        _INSERT_INSTALL_RECEIPT_SQL,
        _CAS_REPOSITORY_MARKER_SQL,
        _INSERT_RUNTIME_SQL,
        _INSERT_WORKER_HEAD_SQL,
        _INSERT_ALLOCATION_HEAD_SQL,
        _INSERT_DISPATCH_HEAD_SQL,
        _INSERT_ARTIFACT_SQL,
        _INSERT_ARTIFACT_CHUNK_SQL,
        _INSERT_ASSET_BLOB_SQL,
        _INSERT_BUSINESS_COMMIT_SQL,
        _INSERT_REPOSITORY_PROJECT_SQL,
        _INSERT_DOCUMENT_ASSET_REF_SQL,
        _INSERT_DOCUMENT_HEAD_SQL,
        _INSERT_ATOMIC_RECEIPT_SQL,
        _INSERT_BASELINE_RECEIPT_SQL,
        _INSERT_INSTALL_CLOSURE_SQL,
    )
)


def _execute_fixed(connection: Any, sql: str, values: tuple) -> int:
    if type(sql) is not str or sql not in _FIXED_WRITE_SQL or type(values) is not tuple:
        raise _InstallCorrupt("cutover_install_fixed_sql_invalid")
    cursor = connection.execute(sql, values)
    try:
        return cursor.rowcount
    finally:
        cursor.close()


def _executemany_fixed(connection: Any, sql: str, rows: Tuple[tuple, ...]) -> None:
    if (
        type(sql) is not str
        or sql not in _FIXED_WRITE_SQL
        or type(rows) is not tuple
        or any(type(row) is not tuple for row in rows)
    ):
        raise _InstallCorrupt("cutover_install_fixed_sql_invalid")
    if rows:
        cursor = connection.executemany(sql, rows)
        cursor.close()


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
    """Best-effort cleanup that can never replace a process-control signal."""

    try:
        if commit_started:
            _close_quietly(connection)
        else:
            _rollback_close(connection)
    except BaseException:
        try:
            _close_quietly(connection)
        except BaseException:
            pass


def _v12_asset_blob_rows(
    projection: BusinessCutoverInstallClosureProjection,
) -> Tuple[tuple, ...]:
    """Project exact asset rows after V12 fresh-coverage metadata fill-in.

    V11 deliberately preserves optional reference metadata.  V12 binds the
    fresh coverage descriptor and may therefore fill a previously absent MIME
    type or extension.  The database rows must match that enhanced artifact,
    not the V11 source row used to prove backward reconstruction.
    """

    baseline = projection._baseline
    descriptors = {
        descriptor.asset_sha256: descriptor
        for descriptor in projection._coverage_plan.blobs
    }
    sources = {source.asset_sha256.hex(): source for source in baseline.asset_blobs}
    if set(descriptors) != set(sources):
        raise _InstallCorrupt("cutover_install_asset_coverage_mismatch")
    rows = []
    for source in baseline.asset_blobs:
        descriptor = descriptors[source.asset_sha256.hex()]
        if (
            descriptor.content_key != source.content_key
            or descriptor.byte_length != source.byte_length
            or (
                source.media_type is not None
                and source.media_type != descriptor.media_type
            )
            or (
                source.extension is not None
                and source.extension != descriptor.extension
            )
        ):
            raise _InstallCorrupt("cutover_install_asset_coverage_mismatch")
        row = (
            source.asset_sha256,
            source.content_key,
            descriptor.media_type,
            descriptor.extension,
            descriptor.byte_length,
            source.status,
            source.state_revision,
            source.created_at,
            source.updated_at,
        )
        if len(row) != len(ASSET_BLOBS_COLUMNS):
            raise _InstallCorrupt("cutover_install_asset_projection_invalid")
        rows.append(row)
    return tuple(rows)


class SQLiteCutoverInstallWriter:
    """Append, verify, commit, and freshly reconcile one read-only install."""

    __slots__ = ("_database",)

    def __init__(self, database: SqliteDatabase, *, _capability: Any) -> None:
        if type(database) is not SqliteDatabase:
            raise TypeError("database must be an exact SqliteDatabase")
        if _capability is not _CUTOVER_INSTALL_WRITER_CAPABILITY:
            raise TypeError("cutover install writer capability is required")
        self._database = database

    @staticmethod
    def _projection(value: Any) -> BusinessCutoverInstallClosureProjection:
        try:
            projection = _require_business_cutover_install_closure(value)
            _require_business_cutover_install_closure_persistability(projection)
        except (
            BusinessCutoverInstallClosureError,
            BusinessCutoverPersistabilityError,
        ) as exc:
            raise SQLiteCutoverInstallError(
                "cutover_install_projection_invalid"
            ) from exc
        plan = projection._plan
        if (
            plan.expected_target_schema_version != MIGRATIONS[-1].version
            or plan.expected_target_schema_fingerprint != _expected_schema_fingerprint()
        ):
            raise SQLiteCutoverInstallError("cutover_install_schema_target_stale")
        return projection

    @staticmethod
    def _result(
        projection: BusinessCutoverInstallClosureProjection,
        status: SQLiteCutoverInstallStatus,
        code: str,
        *,
        recovered: bool = False,
    ) -> SQLiteCutoverInstallResult:
        evidence = (
            projection._plan.install
            if status is SQLiteCutoverInstallStatus.INSTALLED_READ_ONLY
            else None
        )
        return SQLiteCutoverInstallResult(
            status,
            projection.cutover_id,
            code,
            evidence,
            recovered,
        )

    def _validate_current_schema(self, connection: Any) -> None:
        if _pragma_int(connection, "application_id") != APPLICATION_ID:
            raise _InstallCorrupt("cutover_install_application_id_invalid")
        if _pragma_int(connection, "user_version") != MIGRATIONS[-1].version:
            raise _InstallConflict("cutover_install_schema_version_conflict")
        _verify_security_profile(self._database, connection)
        self._database._verify_schema(connection)
        integrity = connection.execute("PRAGMA integrity_check(1)").fetchone()
        if integrity is None or str(integrity[0]).lower() != "ok":
            raise _InstallCorrupt("cutover_install_integrity_check_failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise _InstallCorrupt("cutover_install_foreign_key_check_failed")

    @staticmethod
    def _terminal_shadow_exact(
        connection: Any,
        projection: BusinessCutoverInstallClosureProjection,
    ) -> bool:
        baseline = projection._baseline
        projects = tuple(
            _row_values(row)
            for row in connection.execute(
                "SELECT repository_singleton,project_id,generation,codec_id,"
                "codec_version,snapshot_format_version,snapshot_bytes,"
                "snapshot_sha256 FROM projects ORDER BY project_id"
            ).fetchall()
        )
        documents = tuple(
            _row_values(row)
            for row in connection.execute(
                "SELECT project_id,document_id,document_version,schema_version,"
                "collaboration_mode,save_policy,codec_id,codec_version,"
                "snapshot_format_version,snapshot_bytes,snapshot_sha256 "
                "FROM documents ORDER BY project_id,document_id"
            ).fetchall()
        )
        entities = tuple(
            _row_values(row)
            for row in connection.execute(
                "SELECT project_id,document_id,entity_type,entity_id,"
                "entity_version,deleted,codec_id,codec_version,"
                "snapshot_format_version,snapshot_bytes,snapshot_sha256 "
                "FROM entities ORDER BY project_id,document_id,entity_type,entity_id"
            ).fetchall()
        )
        expected_projects = tuple(
            row.sql_values()
            for row in sorted(
                (live.project for live in baseline.live),
                key=lambda row: row.project_id,
            )
        )
        expected_documents = tuple(
            row.sql_values()
            for row in sorted(
                (live.document for live in baseline.live),
                key=lambda row: (row.project_id, row.document_id),
            )
        )
        expected_entities = tuple(
            row.sql_values()
            for row in sorted(
                (entity for live in baseline.live for entity in live.entities),
                key=lambda row: (
                    row.project_id,
                    row.document_id,
                    row.entity_type,
                    row.entity_id,
                ),
            )
        )
        if (
            any(len(row) != len(PROJECTS_COLUMNS) for row in expected_projects)
            or any(len(row) != len(DOCUMENTS_COLUMNS) for row in expected_documents)
            or any(len(row) != len(ENTITIES_COLUMNS) for row in expected_entities)
        ):
            return False
        return (
            _rows_exact(projects, expected_projects)
            and _rows_exact(documents, expected_documents)
            and _rows_exact(entities, expected_entities)
        )

    @staticmethod
    def _prerequisites_exact(
        connection: Any,
        projection: BusinessCutoverInstallClosureProjection,
    ) -> bool:
        plan = projection._plan
        proof = plan.legacy_proof
        closure = projection.closure
        source = connection.execute(
            "SELECT cutover_id,transaction_id,receipt_kind,repository_revision,"
            "manifest_sha256,proof_sha256 FROM legacy_repository_seals "
            "ORDER BY cutover_id"
        ).fetchall()
        coverage = connection.execute(
            "SELECT cutover_id,transaction_id,receipt_kind,"
            "source_admission_revision,source_admission_sha256,"
            "manifest_sha256,coverage_sha256 "
            "FROM business_asset_coverage_receipts ORDER BY cutover_id"
        ).fetchall()
        expected_source = (
            proof.cutover_id,
            proof.transaction_id,
            closure.source_receipt_kind,
            proof.source_admission_revision,
            proof.manifest_sha256,
            proof.proof_sha256,
        )
        expected_coverage = (
            plan.cutover_id,
            projection._coverage_plan.transaction_id,
            projection._coverage_plan.expected_evidence.receipt_kind,
            proof.source_admission_revision,
            closure.source_admission_sha256,
            closure.coverage_manifest_sha256,
            closure.coverage_sha256,
        )
        return (
            len(source) == 1
            and len(coverage) == 1
            and _rows_exact((_row_values(source[0]),), (expected_source,))
            and _rows_exact((_row_values(coverage[0]),), (expected_coverage,))
        )

    @staticmethod
    def _database_time(connection: Any) -> str:
        row = connection.execute(
            "SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')"
        ).fetchone()
        if row is None or len(row) != 1 or type(row[0]) is not str or len(row[0]) != 24:
            raise _InstallCorrupt("cutover_install_database_clock_corrupt")
        return row[0]

    @classmethod
    def _require_projection_time(
        cls,
        connection: Any,
        projection: BusinessCutoverInstallClosureProjection,
    ) -> None:
        if projection._baseline.committed_at > cls._database_time(connection):
            raise _InstallConflict("cutover_install_time_future")

    @staticmethod
    def _owned_namespace_count(connection: Any) -> int:
        row = connection.execute(
            "SELECT "
            "(SELECT count(*) FROM business_cutover_install_receipts)+"
            "(SELECT count(*) FROM repository_runtime_control)+"
            "(SELECT count(*) FROM worker_ledger_head)+"
            "(SELECT count(*) FROM outbox_allocation_head)+"
            "(SELECT count(*) FROM outbox_dispatch_head)+"
            "(SELECT count(*) FROM business_cutover_install_artifacts)+"
            "(SELECT count(*) FROM business_cutover_install_artifact_chunks)+"
            "(SELECT count(*) FROM asset_blobs)+"
            "(SELECT count(*) FROM business_commits)+"
            "(SELECT count(*) FROM repository_projects)+"
            "(SELECT count(*) FROM document_asset_refs)+"
            "(SELECT count(*) FROM document_commit_heads)+"
            "(SELECT count(*) FROM atomic_business_write_receipts)+"
            "(SELECT count(*) FROM business_cutover_baseline_atomic_receipts)+"
            "(SELECT count(*) FROM business_cutover_install_closures)+"
            "(SELECT count(*) FROM business_cutover_head)+"
            "(SELECT count(*) FROM transaction_receipts WHERE "
            "kind='_business_cutover_install_v1' OR "
            "substr(transaction_id,1,26)='_business-cutover-install:')"
        ).fetchone()
        if row is None or type(row[0]) is not int or row[0] < 0:
            raise _InstallCorrupt("cutover_install_namespace_unreadable")
        return row[0]

    @staticmethod
    def _preinstall_receipt_namespace_status(
        connection: Any,
        projection: BusinessCutoverInstallClosureProjection,
    ) -> Optional[SQLiteCutoverInstallStatus]:
        observed = tuple(
            _row_values(row)
            for row in connection.execute(
                _SELECT_PRODUCTION_RECEIPTS_SQL,
                _SELECT_PRODUCTION_RECEIPTS_VALUES,
            ).fetchall()
        )
        direct_prefix_by_kind = {
            "_business_cutover_asset_stage_v1": "_business-cutover-asset-stage:",
            "_business_cutover_asset_coverage_v1": (
                "_business-cutover-asset-coverage:"
            ),
        }
        if any(
            len(row) != 3
            or type(row[0]) is not str
            or type(row[1]) is not str
            or type(row[2]) is not int
            or row[2] != 1
            or row[1] not in direct_prefix_by_kind
            or not row[0].startswith(direct_prefix_by_kind[row[1]])
            for row in observed
        ):
            return SQLiteCutoverInstallStatus.CORRUPT
        coverage_rows = tuple(
            _row_values(row)
            for row in connection.execute(
                "SELECT transaction_id,receipt_kind,cutover_id,batch_count "
                "FROM business_asset_coverage_receipts ORDER BY cutover_id"
            ).fetchall()
        )
        stage_rows = tuple(
            _row_values(row)
            for row in connection.execute(
                "SELECT transaction_id,receipt_kind,cutover_id "
                "FROM business_asset_stage_receipts ORDER BY batch_id"
            ).fetchall()
        )
        if (
            len(coverage_rows) != 1
            or len(coverage_rows[0]) != 4
            or type(coverage_rows[0][2]) is not str
            or type(coverage_rows[0][3]) is not int
            or coverage_rows[0][3] != len(stage_rows)
            or any(
                len(row) != 3
                or type(row[0]) is not str
                or type(row[1]) is not str
                or type(row[2]) is not str
                or row[2] != coverage_rows[0][2]
                for row in stage_rows
            )
        ):
            return SQLiteCutoverInstallStatus.CORRUPT
        durable_identity = tuple(
            sorted((row[0], row[1]) for row in stage_rows + (coverage_rows[0][:2],))
        )
        observed_identity = tuple(row[:2] for row in observed)
        if not _rows_exact(observed_identity, durable_identity):
            return SQLiteCutoverInstallStatus.CORRUPT
        coverage_plan = projection._coverage_plan
        expected = tuple(
            sorted(
                (
                    evidence.transaction_id,
                    evidence.receipt_kind,
                )
                for evidence in (
                    tuple(coverage_plan.batch_evidences)
                    + (coverage_plan.expected_evidence,)
                )
            )
        )
        if _rows_exact(observed_identity, expected):
            return None
        return SQLiteCutoverInstallStatus.CONFLICT

    @staticmethod
    def _legacy_receipt_namespace_exact(connection: Any) -> bool:
        observed = tuple(
            _row_values(row)
            for row in connection.execute(
                _SELECT_LEGACY_RECEIPTS_SQL,
                _SELECT_LEGACY_RECEIPTS_VALUES,
            ).fetchall()
        )
        if any(
            len(row) != 3
            or type(row[0]) is not str
            or type(row[1]) is not str
            or type(row[2]) is not int
            or row[2] != 1
            for row in observed
        ):
            return False
        expected = tuple(
            _row_values(row)
            for row in connection.execute(
                _SELECT_LEGACY_RECEIPT_CHILDREN_SQL
            ).fetchall()
        )
        return _rows_exact(tuple(row[:2] for row in observed), expected)

    @staticmethod
    def _binding_exact(
        binding: Any,
        projection: BusinessCutoverInstallClosureProjection,
    ) -> bool:
        binding = _require_cutover_install_closure_binding(binding)
        closure = projection.closure
        return all(
            _same(getattr(binding, name), expected)
            for name, expected in (
                ("cutover_id", projection.cutover_id),
                ("install_transaction_id", closure.install_transaction_id),
                ("install_manifest_sha256", closure.install_manifest_sha256),
                ("member_mapping_sha256", closure.member_mapping_sha256),
                ("baseline_projection_sha256", closure.baseline_projection_sha256),
                (
                    "baseline_receipt_root_sha256",
                    closure.baseline_outer_receipt_root_sha256,
                ),
                ("closure_manifest_sha256", closure.manifest_sha256),
                ("source_proof_sha256", closure.source_proof_sha256),
                ("source_admission_sha256", closure.source_admission_sha256),
                ("coverage_manifest_sha256", closure.coverage_manifest_sha256),
                ("coverage_sha256", closure.coverage_sha256),
                ("project_count", closure.project_count),
                ("live_project_count", closure.live_project_count),
                ("deleted_project_count", closure.deleted_project_count),
                ("baseline_commit_count", closure.baseline_commit_count),
            )
        )

    def _readback_exact(
        self,
        connection: Any,
        projection: BusinessCutoverInstallClosureProjection,
    ) -> bool:
        plan = projection._plan
        binding = _read_business_cutover_install_closure(
            connection,
            cutover_id=projection.cutover_id,
            install_transaction_id=plan.install.transaction_id,
            install_manifest_sha256=plan.install.manifest_sha256,
            install_committed_at=projection._baseline.committed_at,
            target_schema_version=plan.expected_target_schema_version,
            target_schema_fingerprint=plan.expected_target_schema_fingerprint,
        )
        return self._binding_exact(binding, projection)

    def _read_stored_binding(
        self,
        connection: Any,
        projection: BusinessCutoverInstallClosureProjection,
    ) -> Any:
        """Prove the durable closure from its own identity before comparison."""

        rows = connection.execute(
            "SELECT install.cutover_id,install.transaction_id,"
            "install.manifest_sha256,install.committed_at "
            "FROM business_cutover_install_receipts AS install "
            "JOIN business_cutover_install_closures AS closure "
            "ON closure.cutover_id=install.cutover_id AND "
            "closure.install_transaction_id=install.transaction_id AND "
            "closure.install_manifest_sha256=install.manifest_sha256 "
            "ORDER BY install.cutover_id"
        ).fetchall()
        if len(rows) != 1:
            raise _InstallCorrupt("cutover_install_stored_identity_corrupt")
        stored = rows[0]
        stored_committed_at = stored["committed_at"]
        if (
            type(stored_committed_at) is not str
            or len(stored_committed_at) != 24
            or stored_committed_at > self._database_time(connection)
        ):
            raise _InstallCorrupt("cutover_install_stored_time_future")
        plan = projection._plan
        return _read_business_cutover_install_closure(
            connection,
            cutover_id=stored["cutover_id"],
            install_transaction_id=stored["transaction_id"],
            install_manifest_sha256=stored["manifest_sha256"],
            install_committed_at=stored["committed_at"],
            target_schema_version=plan.expected_target_schema_version,
            target_schema_fingerprint=plan.expected_target_schema_fingerprint,
        )

    def _snapshot_result(
        self,
        connection: Any,
        projection: BusinessCutoverInstallClosureProjection,
    ) -> SQLiteCutoverInstallResult:
        self._validate_current_schema(connection)
        markers = connection.execute(
            "SELECT singleton,authority FROM kernel_repository ORDER BY singleton"
        ).fetchall()
        if len(markers) != 1 or _row_values(markers[0])[0] != 1:
            return self._result(
                projection,
                SQLiteCutoverInstallStatus.CORRUPT,
                "cutover_install_marker_corrupt",
            )
        authority = markers[0]["authority"]
        if authority == "legacy":
            if self._owned_namespace_count(connection) != 0:
                return self._result(
                    projection,
                    SQLiteCutoverInstallStatus.CORRUPT,
                    "cutover_install_partial_namespace",
                )
            if not self._legacy_receipt_namespace_exact(connection):
                return self._result(
                    projection,
                    SQLiteCutoverInstallStatus.CORRUPT,
                    "cutover_install_legacy_receipt_namespace_corrupt",
                )
            receipt_status = self._preinstall_receipt_namespace_status(
                connection, projection
            )
            if receipt_status is SQLiteCutoverInstallStatus.CORRUPT:
                return self._result(
                    projection,
                    SQLiteCutoverInstallStatus.CORRUPT,
                    "cutover_install_reserved_receipt_namespace_corrupt",
                )
            if receipt_status is SQLiteCutoverInstallStatus.CONFLICT:
                return self._result(
                    projection,
                    SQLiteCutoverInstallStatus.CONFLICT,
                    "cutover_install_reserved_receipt_conflict",
                )
            if not self._terminal_shadow_exact(connection, projection):
                return self._result(
                    projection,
                    SQLiteCutoverInstallStatus.CONFLICT,
                    "cutover_install_terminal_shadow_conflict",
                )
            if not self._prerequisites_exact(connection, projection):
                return self._result(
                    projection,
                    SQLiteCutoverInstallStatus.CONFLICT,
                    "cutover_install_prerequisite_conflict",
                )
            return self._result(
                projection,
                SQLiteCutoverInstallStatus.NOT_INSTALLED,
                "cutover_install_absent",
            )
        if authority != "sqlite":
            return self._result(
                projection,
                SQLiteCutoverInstallStatus.CORRUPT,
                "cutover_install_marker_corrupt",
            )
        runtime = connection.execute(
            "SELECT authority_state,cutover_id FROM repository_runtime_control "
            "WHERE singleton=1"
        ).fetchall()
        if len(runtime) != 1 or runtime[0]["authority_state"] not in (
            "read_only",
            "active",
        ):
            return self._result(
                projection,
                SQLiteCutoverInstallStatus.CORRUPT,
                "cutover_install_runtime_corrupt",
            )
        try:
            _validate_business_runtime_connection(
                self._database,
                connection,
                access=BusinessRuntimeAccess.READER,
            )
            binding = self._read_stored_binding(connection, projection)
        except _InstallCorrupt:
            return self._result(
                projection,
                SQLiteCutoverInstallStatus.CORRUPT,
                "cutover_install_closure_corrupt",
            )
        except SQLiteCutoverInstallClosureError as failure:
            if _is_storage_unavailable_failure(failure):
                raise
            return self._result(
                projection,
                SQLiteCutoverInstallStatus.CORRUPT,
                "cutover_install_closure_corrupt",
            )
        if not self._binding_exact(binding, projection):
            return self._result(
                projection,
                SQLiteCutoverInstallStatus.CONFLICT,
                "cutover_install_projection_conflict",
            )
        return self._result(
            projection,
            SQLiteCutoverInstallStatus.INSTALLED_READ_ONLY,
            "cutover_install_exact",
        )

    @staticmethod
    def _write_install_rows_before_closure(
        connection: Any,
        projection: BusinessCutoverInstallClosureProjection,
    ) -> None:
        plan = projection._plan
        baseline = projection._baseline
        install = plan.install
        runtime = install.runtime
        committed_at = baseline.committed_at
        _execute_fixed(
            connection,
            _INSERT_GENERIC_RECEIPT_SQL,
            (install.transaction_id, install.receipt_kind, committed_at),
        )
        _execute_fixed(
            connection,
            _INSERT_INSTALL_RECEIPT_SQL,
            (
                install.cutover_id,
                install.transaction_id,
                install.receipt_kind,
                committed_at,
                runtime.repository_epoch,
                runtime.runtime_generation,
                runtime.code_digest,
                install.member_mapping_sha256,
                install.manifest_bytes,
                install.manifest_sha256,
            ),
        )
        if _execute_fixed(connection, _CAS_REPOSITORY_MARKER_SQL, ()) != 1:
            raise _InstallConflict("cutover_install_marker_cas_conflict")
        _execute_fixed(
            connection,
            _INSERT_RUNTIME_SQL,
            (
                runtime.authority,
                runtime.authority_state,
                runtime.repository_epoch,
                runtime.cutover_id,
                runtime.source_admission_revision,
                runtime.repository_revision,
                runtime.state_revision,
                runtime.runtime_generation,
                runtime.code_digest,
                runtime.schema_version,
                committed_at,
            ),
        )
        scheduler = baseline.scheduler_genesis
        _execute_fixed(
            connection,
            _INSERT_WORKER_HEAD_SQL,
            scheduler.worker_ledger.sql_values(),
        )
        _execute_fixed(
            connection,
            _INSERT_ALLOCATION_HEAD_SQL,
            scheduler.outbox_allocation.sql_values(),
        )
        _execute_fixed(
            connection,
            _INSERT_DISPATCH_HEAD_SQL,
            scheduler.outbox_dispatch.sql_values(),
        )
        artifacts = (projection.member_mapping, projection.baseline_projection)
        _executemany_fixed(
            connection,
            _INSERT_ARTIFACT_SQL,
            tuple(artifact.header.sql_values() for artifact in artifacts),
        )
        _executemany_fixed(
            connection,
            _INSERT_ARTIFACT_CHUNK_SQL,
            tuple(
                chunk.sql_values()
                for artifact in artifacts
                for chunk in artifact.chunks
            ),
        )
        _executemany_fixed(
            connection,
            _INSERT_ASSET_BLOB_SQL,
            _v12_asset_blob_rows(projection),
        )
        atomic_by_commit = {row.commit_id: row for row in projection.atomic_receipts}
        for live in baseline.live:
            _execute_fixed(
                connection,
                _INSERT_GENERIC_RECEIPT_SQL,
                live.transaction_receipt.sql_values(),
            )
            _execute_fixed(
                connection,
                _INSERT_BUSINESS_COMMIT_SQL,
                live.commit.sql_values(),
            )
            _execute_fixed(
                connection,
                _INSERT_REPOSITORY_PROJECT_SQL,
                live.repository_project.sql_values(),
            )
            _executemany_fixed(
                connection,
                _INSERT_DOCUMENT_ASSET_REF_SQL,
                tuple(row.sql_values() for row in live.asset_references),
            )
            _execute_fixed(
                connection,
                _INSERT_DOCUMENT_HEAD_SQL,
                live.document_head.sql_values(),
            )
            atomic = atomic_by_commit.get(live.commit.commit_id)
            if atomic is None:
                raise _InstallCorrupt("cutover_install_atomic_projection_missing")
            _execute_fixed(connection, _INSERT_ATOMIC_RECEIPT_SQL, atomic.sql_values())
        _executemany_fixed(
            connection,
            _INSERT_REPOSITORY_PROJECT_SQL,
            tuple(row.repository_project.sql_values() for row in baseline.deleted),
        )
        _executemany_fixed(
            connection,
            _INSERT_BASELINE_RECEIPT_SQL,
            tuple(row.sql_values() for row in projection.baseline_receipts),
        )

    @staticmethod
    def _write_install_closure_last(
        connection: Any,
        projection: BusinessCutoverInstallClosureProjection,
    ) -> None:
        """Perform the transaction's final DML; callers may only read afterward."""

        _execute_fixed(
            connection,
            _INSERT_INSTALL_CLOSURE_SQL,
            projection.closure.sql_values(),
        )

    def reconcile(
        self,
        projection: BusinessCutoverInstallClosureProjection,
    ) -> SQLiteCutoverInstallResult:
        projection = self._projection(projection)
        connection = None

        def cleanup() -> bool:
            nonlocal connection
            if connection is None:
                return True
            current = connection
            connection = None
            return _rollback_close(current)

        def cleanup_unknown() -> SQLiteCutoverInstallResult:
            return self._result(
                projection,
                SQLiteCutoverInstallStatus.UNKNOWN,
                "cutover_install_reconcile_cleanup_failed",
            )

        try:
            connection, _filesystem = self._database._open_unvalidated()
            connection.execute("BEGIN")
            result = self._snapshot_result(connection, projection)
        except _InstallConflict as failure:
            if not cleanup():
                return cleanup_unknown()
            return self._result(
                projection,
                SQLiteCutoverInstallStatus.CONFLICT,
                str(failure) or "cutover_install_reconcile_conflict",
            )
        except _InstallCorrupt as failure:
            if not cleanup():
                return cleanup_unknown()
            return self._result(
                projection,
                SQLiteCutoverInstallStatus.CORRUPT,
                str(failure) or "cutover_install_reconcile_corrupt",
            )
        except SQLiteKernelError as failure:
            if not cleanup():
                return cleanup_unknown()
            unavailable = _is_storage_unavailable_failure(failure)
            return self._result(
                projection,
                (
                    SQLiteCutoverInstallStatus.UNKNOWN
                    if unavailable
                    else SQLiteCutoverInstallStatus.CORRUPT
                ),
                (
                    "cutover_install_reconcile_unavailable"
                    if unavailable
                    else "cutover_install_schema_corrupt"
                ),
            )
        except SQLiteCutoverInstallClosureError as failure:
            if not cleanup():
                return cleanup_unknown()
            unavailable = _is_storage_unavailable_failure(failure)
            return self._result(
                projection,
                (
                    SQLiteCutoverInstallStatus.UNKNOWN
                    if unavailable
                    else SQLiteCutoverInstallStatus.CORRUPT
                ),
                (
                    "cutover_install_reconcile_unavailable"
                    if unavailable
                    else "cutover_install_closure_corrupt"
                ),
            )
        except sqlite3.IntegrityError:
            if not cleanup():
                return cleanup_unknown()
            return self._result(
                projection,
                SQLiteCutoverInstallStatus.CORRUPT,
                "cutover_install_reconcile_integrity_failure",
            )
        except sqlite3.OperationalError as failure:
            if not cleanup():
                return cleanup_unknown()
            unavailable = _is_storage_unavailable_failure(failure)
            return self._result(
                projection,
                (
                    SQLiteCutoverInstallStatus.UNKNOWN
                    if unavailable
                    else SQLiteCutoverInstallStatus.CORRUPT
                ),
                (
                    "cutover_install_reconcile_unavailable"
                    if unavailable
                    else "cutover_install_reconcile_corrupt"
                ),
            )
        except (OSError, MemoryError):
            if not cleanup():
                return cleanup_unknown()
            return self._result(
                projection,
                SQLiteCutoverInstallStatus.UNKNOWN,
                "cutover_install_reconcile_unavailable",
            )
        except (sqlite3.DatabaseError, TypeError, ValueError, IndexError):
            if not cleanup():
                return cleanup_unknown()
            return self._result(
                projection,
                SQLiteCutoverInstallStatus.CORRUPT,
                "cutover_install_reconcile_corrupt",
            )
        except Exception:
            if not cleanup():
                return cleanup_unknown()
            return self._result(
                projection,
                SQLiteCutoverInstallStatus.UNKNOWN,
                "cutover_install_reconcile_unavailable",
            )
        except BaseException:
            current = connection
            connection = None
            if current is not None:
                _process_control_cleanup(current, commit_started=False)
            raise
        else:
            if not cleanup():
                return cleanup_unknown()
        if result.status is SQLiteCutoverInstallStatus.INSTALLED_READ_ONLY:
            try:
                self._database._finalize_committed_storage()
            except Exception:
                return self._result(
                    projection,
                    SQLiteCutoverInstallStatus.UNKNOWN,
                    "cutover_install_durability_barrier_failed",
                )
        return result

    def install(
        self,
        projection: BusinessCutoverInstallClosureProjection,
        *,
        _test_hook: Optional[Callable[[SQLiteCutoverInstallTestPoint], None]] = None,
    ) -> SQLiteCutoverInstallResult:
        projection = self._projection(projection)
        if _test_hook is not None and not callable(_test_hook):
            raise TypeError("_test_hook must be callable")
        initial = self.reconcile(projection)
        if initial.status is SQLiteCutoverInstallStatus.INSTALLED_READ_ONLY:
            return SQLiteCutoverInstallResult(
                initial.status,
                initial.cutover_id,
                initial.code,
                initial.evidence,
                True,
            )
        if initial.status is not SQLiteCutoverInstallStatus.NOT_INSTALLED:
            return initial

        connection = None
        authorizer = None
        commit_started = False
        hook_failed_before_commit = False
        try:
            connection, _filesystem = self._database._open_unvalidated()
            connection.execute("BEGIN IMMEDIATE")
            before = self._snapshot_result(connection, projection)
            if before.status is not SQLiteCutoverInstallStatus.NOT_INSTALLED:
                cleanup_complete = _rollback_close(connection)
                connection = None
                if not cleanup_complete:
                    return self._result(
                        projection,
                        SQLiteCutoverInstallStatus.UNKNOWN,
                        "cutover_install_race_cleanup_failed",
                    )
                raced = self.reconcile(projection)
                if raced.status is SQLiteCutoverInstallStatus.INSTALLED_READ_ONLY:
                    return SQLiteCutoverInstallResult(
                        raced.status,
                        raced.cutover_id,
                        raced.code,
                        raced.evidence,
                        True,
                    )
                return raced
            # The terminal shadow is proved above, before the first install DML.
            self._require_projection_time(connection, projection)
            authorizer = _CutoverInstallAuthorizer()
            connection.set_authorizer(authorizer)
            self._write_install_rows_before_closure(connection, projection)
            self._write_install_closure_last(connection, projection)
            if authorizer.denied or not self._readback_exact(connection, projection):
                raise _InstallCorrupt("cutover_install_precommit_closure_mismatch")
            if _test_hook is not None:
                try:
                    _test_hook(SQLiteCutoverInstallTestPoint.BEFORE_COMMIT)
                except Exception:
                    hook_failed_before_commit = True
                    raise
            projection = self._projection(projection)
            self._require_projection_time(connection, projection)
            if authorizer.denied or not self._readback_exact(connection, projection):
                raise _InstallCorrupt("cutover_install_final_closure_mismatch")
            _reset_authorizer(connection)
            _validate_business_runtime_connection(
                self._database,
                connection,
                access=BusinessRuntimeAccess.READER,
            )
            commit_started = True
            connection.commit()
            if _test_hook is not None:
                _test_hook(SQLiteCutoverInstallTestPoint.AFTER_COMMIT_BEFORE_ACK)
            connection.close()
            connection = None
            reconciled = self.reconcile(projection)
            if reconciled.status is SQLiteCutoverInstallStatus.INSTALLED_READ_ONLY:
                return self._result(
                    projection,
                    SQLiteCutoverInstallStatus.INSTALLED_READ_ONLY,
                    "cutover_install_committed",
                )
            return reconciled
        except (_InstallConflict, _InstallCorrupt) as failure:
            cleanup_complete = True
            if connection is not None:
                cleanup_complete = _rollback_close(connection)
                connection = None
            if commit_started or not cleanup_complete:
                reconciled = self.reconcile(projection)
                if reconciled.status is SQLiteCutoverInstallStatus.INSTALLED_READ_ONLY:
                    return SQLiteCutoverInstallResult(
                        reconciled.status,
                        reconciled.cutover_id,
                        reconciled.code,
                        reconciled.evidence,
                        True,
                    )
                if not cleanup_complete:
                    return self._result(
                        projection,
                        SQLiteCutoverInstallStatus.UNKNOWN,
                        "cutover_install_cleanup_failed",
                    )
                return reconciled
            return self._result(
                projection,
                (
                    SQLiteCutoverInstallStatus.CONFLICT
                    if isinstance(failure, _InstallConflict)
                    else SQLiteCutoverInstallStatus.CORRUPT
                ),
                str(failure) or "cutover_install_failed",
            )
        except BaseException as failure:
            process_control = not isinstance(failure, Exception)
            cleanup_complete = True
            if connection is not None:
                if process_control:
                    _process_control_cleanup(
                        connection,
                        commit_started=commit_started,
                    )
                elif commit_started:
                    cleanup_complete = _close_quietly(connection)
                else:
                    cleanup_complete = _rollback_close(connection)
                connection = None
            if process_control:
                # Process-control exceptions are never translated, but the
                # contract still requires one best-effort fresh classification
                # after rollback/close or possible COMMIT acknowledgement loss.
                try:
                    self.reconcile(projection)
                except BaseException:
                    pass
                raise
            if commit_started or not cleanup_complete:
                reconciled = self.reconcile(projection)
                if reconciled.status is SQLiteCutoverInstallStatus.INSTALLED_READ_ONLY:
                    return SQLiteCutoverInstallResult(
                        reconciled.status,
                        reconciled.cutover_id,
                        reconciled.code,
                        reconciled.evidence,
                        True,
                    )
                if not cleanup_complete:
                    return self._result(
                        projection,
                        SQLiteCutoverInstallStatus.UNKNOWN,
                        "cutover_install_cleanup_failed",
                    )
                return reconciled
            if hook_failed_before_commit:
                reconciled = self.reconcile(projection)
                if reconciled.status is SQLiteCutoverInstallStatus.NOT_INSTALLED:
                    return self._result(
                        projection,
                        SQLiteCutoverInstallStatus.NOT_INSTALLED,
                        "cutover_install_rolled_back",
                    )
                return reconciled
            if authorizer is not None and authorizer.denied:
                return self._result(
                    projection,
                    SQLiteCutoverInstallStatus.CORRUPT,
                    "cutover_install_authorizer_denied",
                )
            if isinstance(failure, sqlite3.IntegrityError):
                return self._result(
                    projection,
                    SQLiteCutoverInstallStatus.CORRUPT,
                    "cutover_install_schema_integrity_failure",
                )
            if _is_storage_unavailable_failure(failure):
                return self._result(
                    projection,
                    SQLiteCutoverInstallStatus.UNKNOWN,
                    "cutover_install_storage_unavailable",
                )
            if isinstance(failure, SQLiteKernelError):
                return self._result(
                    projection,
                    SQLiteCutoverInstallStatus.CORRUPT,
                    "cutover_install_schema_corrupt",
                )
            return self._result(
                projection,
                SQLiteCutoverInstallStatus.CORRUPT,
                "cutover_install_internal_failure",
            )


__all__ = ()
