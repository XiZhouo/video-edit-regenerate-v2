"""Plan-free durable audit for the singleton SQLite cutover lineage.

This reader is deliberately colder and stronger than the transaction fast
fence.  It accepts no cutover plan, projection, callback, clock, connection or
SQL.  One pinned read snapshot discovers the stored lineage, replays the V12
install closure and the canonical phase-head prefix from durable rows, and a
second fresh snapshot fences publication.  The detached result is diagnostic
evidence only; it is never a writer capability.

The install writer's exact success state is intentionally headless: its V12
closure is the transaction's final DML and the phase-1 head belongs to a later
recoverable transaction.  ``INSTALLED_CLOSURE`` therefore differs from the
canonical phase-1 ``INSTALLED_HEAD`` state.  Conflating the two would either
misclassify a successful install as corrupt or erase an independently durable
recovery point.

Production callers must hold the external repository/maintenance lease for
the whole audit.  The pinned WAL snapshot, inode samples and fresh fence detect
ordinary replacement races; no finite sampling defeats a malicious A→B→A file
swap that ignores that lease.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import sqlite3
from typing import Any, NamedTuple, Optional, Sequence

from business_cutover_activation_contract import (
    BUSINESS_CUTOVER_ACTIVATION_TRANSACTION_PREFIX,
    BusinessCutoverActivationContractError,
)
from business_cutover_activation_integrity import (
    BusinessCutoverActivationIntegrityError,
)
from business_cutover_asset_contract import (
    BUSINESS_CUTOVER_ASSET_COVERAGE_TRANSACTION_PREFIX,
    BusinessCutoverAssetContractError,
)
from business_cutover_asset_integrity import BusinessCutoverAssetIntegrityError
from business_cutover_contract import (
    BUSINESS_CUTOVER_INSTALL_TRANSACTION_PREFIX,
    MAX_BUSINESS_CUTOVER_RUNTIME_GENERATION_BYTES,
    MAX_SQLITE_INTEGER,
    BusinessCutoverContractError,
)
from business_cutover_head_integrity import (
    BusinessCutoverHeadIntegrityError,
    BusinessCutoverHeadPointer,
    BusinessCutoverHeadPointerRole,
    _advance_business_cutover_head,
    _issue_initial_business_cutover_head,
)
from business_cutover_mount_contract import (
    BUSINESS_CUTOVER_MOUNT_TRANSACTION_PREFIX,
    BusinessCutoverMountContractError,
)
from business_cutover_mount_integrity import BusinessCutoverMountIntegrityError
from business_cutover_stored_manifest import (
    BusinessCutoverStoredManifestError,
    _parse_stored_install_manifest,
)
from receipt_namespace_integrity import ReceiptNamespaceIntegrityError
from repository_audit_contract import (
    EMPTY_REPOSITORY_AUDIT_CERTIFICATE_TAIL_SHA256,
    REPOSITORY_AUDIT_TRANSACTION_PREFIX,
    RepositoryAuditContractError,
    RepositoryAuditObservedInput,
    RepositoryAuditPurpose,
)
from repository_audit_integrity import (
    RepositoryAuditIntegrityError,
    _require_predecessor,
)
from sqlite_business_audit import (
    _audit_business_runtime_connection,
    _main_storage_identity,
    _read_receipt_namespace_identity,
    _require_main_storage_identity,
    _verify_relational_integrity,
)
from sqlite_business_runtime import (
    BusinessRuntimeAccess,
    _canonical_utc,
    _verify_security_profile,
    _validate_business_runtime_connection,
)
from sqlite_cutover_install_closure_reader import (
    SQLiteCutoverInstallClosureError,
    _read_business_cutover_install_closure,
    _require_cutover_install_closure_binding,
)
from sqlite_cutover_install_provenance_reader import (
    SQLiteCutoverInstallProvenanceError,
)
from sqlite_cutover_verification import (
    MAX_CUTOVER_DURABLE_AUDIT_BYTES,
    MAX_CUTOVER_DURABLE_AUDIT_ENTRIES,
    MAX_CUTOVER_DURABLE_AUDIT_VM_STEPS,
    SQLiteCutoverVerificationBudget as SQLiteCutoverAuditBudget,
    _CutoverVerificationBudgetExceeded as _AuditBudgetExceeded,
    _CutoverVerificationCorrupt as _AuditCorrupt,
    _CutoverVerificationUnavailable as _AuditUnavailable,
    _CutoverVerificationUnsupported as _AuditUnsupported,
    _failure_is_unavailable,
    _install_cutover_verification_progress_budget as _install_progress_budget,
    _read_cutover_verification_budget as _budget,
    _row_tuple,
    _rows_exact,
    _same,
    _verify_legacy_receipt_namespace,
    _verify_production_receipt_namespace,
)
from sqlite_kernel import (
    APPLICATION_ID,
    SQLiteKernelError,
    SqliteDatabase,
    _expected_schema_fingerprint,
    _pragma_int,
)
from sqlite_production_authority import (
    SQLiteProductionAuthorityError,
    _CUTOVER_HEAD_ROW_COLUMNS,
    _activation_evidence_from_manifest,
    _audit_evidence_from_manifest,
    _binding_from_snapshot,
    _linked_mount_rows_exact,
    _mount_evidence_from_row,
    _mounted_row,
)
from sqlite_schema import (
    BUSINESS_TRANSACTION_TABLES,
    MIGRATIONS,
    _expected_migration_ledger_sha256,
)
from sqlite_cutover_phase_binding_reader import (
    SQLiteCutoverPhaseBindingReaderError,
    _read_cutover_phase_binding_prefix,
)


MAX_CUTOVER_DURABLE_AUDIT_CERTIFICATES = 256
MAX_CUTOVER_DURABLE_AUDIT_CERTIFICATE_BYTES = 16 * 1024 * 1024

_ZERO_SHA256 = b"\0" * 32
_LOWER_HEX = frozenset("0123456789abcdef")

_POST_INSTALL_SUFFIX_TABLES = (
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

_INSTALL_OWNED_TABLES = tuple(dict.fromkeys((
    "business_cutover_install_receipts",
    "repository_runtime_control",
    "worker_ledger_head",
    "outbox_allocation_head",
    "outbox_dispatch_head",
    "business_cutover_head",
    "repository_audit_certificates",
    "repository_audit_head",
    "business_cutover_activation_receipts",
    "business_cutover_mount_receipts",
    "business_cutover_install_artifacts",
    "business_cutover_install_artifact_chunks",
    "business_cutover_baseline_atomic_receipts",
    "business_cutover_install_closures",
    "business_cutover_asset_provider_bindings",
    "business_cutover_audit_bindings",
    "business_cutover_activation_bindings",
    "business_cutover_mount_bindings",
    *BUSINESS_TRANSACTION_TABLES,
    *_POST_INSTALL_SUFFIX_TABLES,
)))

_AUDIT_ROW_COLUMNS = (
    "certificate_id",
    "cutover_id",
    "sequence",
    "purpose",
    "transaction_id",
    "receipt_kind",
    "committed_at",
    "previous_certificate_id",
    "previous_manifest_sha256",
    "previous_tail_sha256",
    "schema_version",
    "migration_ledger_count",
    "migration_ledger_sha256",
    "receipt_count",
    "receipt_root_sha256",
    "worker_ledger_sequence",
    "worker_ledger_tail_sha256",
    "findings_sha256",
    "manifest_bytes",
    "manifest_sha256",
    "certificate_tail_sha256",
)

_ACTIVATION_ROW_COLUMNS = (
    "cutover_id",
    "transaction_id",
    "receipt_kind",
    "committed_at",
    "install_transaction_id",
    "install_manifest_sha256",
    "audit_transaction_id",
    "audit_manifest_sha256",
    "migration_ledger_sha256",
    "manifest_bytes",
    "manifest_sha256",
)


class SQLiteCutoverDurableAuditStatus(str, Enum):
    EXACT = "exact"
    ABSENT = "absent"
    CONFLICT = "conflict"
    CORRUPT = "corrupt"
    UNKNOWN = "unknown"


class SQLiteCutoverDurablePhase(str, Enum):
    INSTALLED_CLOSURE = "installed_closure"
    INSTALLED_HEAD = "installed_head"
    ASSETS_COVERED = "assets_covered"
    AUDITED = "audited"
    ACTIVATED = "activated"
    MOUNTED = "mounted"


_PHASE_REVISIONS = {
    SQLiteCutoverDurablePhase.INSTALLED_CLOSURE: 0,
    SQLiteCutoverDurablePhase.INSTALLED_HEAD: 1,
    SQLiteCutoverDurablePhase.ASSETS_COVERED: 2,
    SQLiteCutoverDurablePhase.AUDITED: 3,
    SQLiteCutoverDurablePhase.ACTIVATED: 4,
    SQLiteCutoverDurablePhase.MOUNTED: 5,
}


@dataclass(frozen=True)
class SQLiteCutoverDurableIdentity:
    cutover_id: str
    phase: SQLiteCutoverDurablePhase
    repository_epoch: str
    source_admission_revision: int
    runtime_generation: str
    code_digest: bytes
    authority_state: str
    repository_revision: int
    runtime_state_revision: int
    install_transaction_id: str
    install_manifest_sha256: bytes
    closure_manifest_sha256: bytes
    source_proof_sha256: bytes
    source_admission_sha256: bytes
    coverage_transaction_id: str
    coverage_manifest_sha256: bytes
    coverage_sha256: bytes
    baseline_receipt_root_sha256: bytes
    head_sha256: Optional[bytes]
    audit_transaction_id: Optional[str]
    audit_manifest_sha256: Optional[bytes]
    activation_transaction_id: Optional[str]
    activation_manifest_sha256: Optional[bytes]
    mount_transaction_id: Optional[str]
    mount_manifest_sha256: Optional[bytes]
    schema_fingerprint: str
    migration_ledger_sha256: bytes
    receipt_namespace_count: int
    receipt_namespace_sha256: bytes

    def __post_init__(self) -> None:
        _lower_hex(self.cutover_id, 64, "cutover_id")
        if type(self.phase) is not SQLiteCutoverDurablePhase:
            raise TypeError("phase must be an exact SQLiteCutoverDurablePhase")
        _lower_hex(self.repository_epoch, 64, "repository_epoch")
        _lower_hex(self.schema_fingerprint, 64, "schema_fingerprint")
        if (
            type(self.source_admission_revision) is not int
            or self.source_admission_revision < 1
            or self.source_admission_revision > MAX_SQLITE_INTEGER
            or type(self.repository_revision) is not int
            or self.repository_revision < 1
            or self.repository_revision > MAX_SQLITE_INTEGER
            or type(self.runtime_state_revision) is not int
            or self.runtime_state_revision < 1
            or self.runtime_state_revision > MAX_SQLITE_INTEGER
            or type(self.receipt_namespace_count) is not int
            or self.receipt_namespace_count < 0
            or self.receipt_namespace_count > MAX_SQLITE_INTEGER
        ):
            raise TypeError("identity revisions/counts exceed SQLite bounds")
        if type(self.runtime_generation) is not str:
            raise TypeError("runtime_generation must be an exact string")
        try:
            generation_bytes = self.runtime_generation.encode("utf-8", "strict")
        except (AttributeError, UnicodeEncodeError) as exc:
            raise TypeError("runtime_generation must be canonical UTF-8") from exc
        if not 1 <= len(generation_bytes) <= MAX_BUSINESS_CUTOVER_RUNTIME_GENERATION_BYTES:
            raise TypeError("runtime_generation exceeds its byte bound")
        if type(self.authority_state) is not str or self.authority_state not in (
            "read_only",
            "active",
        ):
            raise TypeError("authority_state is invalid")
        for value in (
            self.code_digest,
            self.install_manifest_sha256,
            self.closure_manifest_sha256,
            self.source_proof_sha256,
            self.source_admission_sha256,
            self.coverage_manifest_sha256,
            self.coverage_sha256,
            self.baseline_receipt_root_sha256,
            self.migration_ledger_sha256,
            self.receipt_namespace_sha256,
        ):
            _digest(value, "identity digest")
        for value in (
            self.head_sha256,
            self.audit_manifest_sha256,
            self.activation_manifest_sha256,
            self.mount_manifest_sha256,
        ):
            if value is not None:
                _digest(value, "optional identity digest")
        revision = _PHASE_REVISIONS[self.phase]
        if (self.head_sha256 is None) != (revision == 0):
            raise TypeError("head digest does not match phase")
        optional_pairs = (
            (self.audit_transaction_id, self.audit_manifest_sha256, revision >= 3),
            (
                self.activation_transaction_id,
                self.activation_manifest_sha256,
                revision >= 4,
            ),
            (self.mount_transaction_id, self.mount_manifest_sha256, revision >= 5),
        )
        if any(
            (left is None) != (right is None)
            or (left is not None) != required
            for left, right, required in optional_pairs
        ):
            raise TypeError("optional phase pointers do not match phase")
        if revision <= 3 and self.authority_state != "read_only":
            raise TypeError("pre-activation phase must remain read_only")
        if revision >= 4 and self.authority_state != "active":
            raise TypeError("activated phase must be active")
        for value, name in (
            (self.install_transaction_id, "install_transaction_id"),
            (self.coverage_transaction_id, "coverage_transaction_id"),
        ):
            if type(value) is not str or not value:
                raise TypeError(name + " must be a non-empty exact string")
        if (
            self.install_transaction_id
            != BUSINESS_CUTOVER_INSTALL_TRANSACTION_PREFIX + self.cutover_id
            or self.coverage_transaction_id
            != BUSINESS_CUTOVER_ASSET_COVERAGE_TRANSACTION_PREFIX + self.cutover_id
        ):
            raise TypeError("install/coverage transaction identity is invalid")
        optional_transactions = (
            (
                self.audit_transaction_id,
                REPOSITORY_AUDIT_TRANSACTION_PREFIX,
                32,
                "audit_transaction_id",
            ),
            (
                self.activation_transaction_id,
                BUSINESS_CUTOVER_ACTIVATION_TRANSACTION_PREFIX,
                64,
                "activation_transaction_id",
            ),
            (
                self.mount_transaction_id,
                BUSINESS_CUTOVER_MOUNT_TRANSACTION_PREFIX,
                64,
                "mount_transaction_id",
            ),
        )
        for value, prefix, suffix_length, name in optional_transactions:
            if value is None:
                continue
            if (
                type(value) is not str
                or not value.startswith(prefix)
                or _lower_hex(value[len(prefix):], suffix_length, name)
                != value[len(prefix):]
            ):
                raise TypeError(name + " is invalid")
        if (
            self.activation_transaction_id is not None
            and self.activation_transaction_id
            != BUSINESS_CUTOVER_ACTIVATION_TRANSACTION_PREFIX + self.cutover_id
        ):
            raise TypeError("activation_transaction_id belongs to another cutover")
        if (
            self.mount_transaction_id is not None
            and self.mount_transaction_id
            != BUSINESS_CUTOVER_MOUNT_TRANSACTION_PREFIX + self.cutover_id
        ):
            raise TypeError("mount_transaction_id belongs to another cutover")


@dataclass(frozen=True)
class SQLiteCutoverDurableAuditResult:
    status: SQLiteCutoverDurableAuditStatus
    code: str
    identity: Optional[SQLiteCutoverDurableIdentity] = None
    budget: Optional[SQLiteCutoverAuditBudget] = None

    def __post_init__(self) -> None:
        if type(self.status) is not SQLiteCutoverDurableAuditStatus:
            raise TypeError("status must be an exact audit status")
        if type(self.code) is not str or not self.code:
            raise TypeError("code must be a non-empty exact string")
        if self.identity is not None and type(self.identity) is not SQLiteCutoverDurableIdentity:
            raise TypeError("identity must be an exact durable identity")
        if self.budget is not None and type(self.budget) is not SQLiteCutoverAuditBudget:
            raise TypeError("budget must be an exact audit budget")
        if self.status in (
            SQLiteCutoverDurableAuditStatus.EXACT,
            SQLiteCutoverDurableAuditStatus.CONFLICT,
        ) and self.identity is None:
            raise TypeError("exact/conflict results require observed identity")
        if self.status in (
            SQLiteCutoverDurableAuditStatus.ABSENT,
            SQLiteCutoverDurableAuditStatus.CORRUPT,
            SQLiteCutoverDurableAuditStatus.UNKNOWN,
        ) and self.identity is not None:
            raise TypeError("absent/corrupt/unknown results cannot carry identity")


class _AuditFence(NamedTuple):
    storage_identity: tuple
    schema_fingerprint: Any
    marker: tuple
    runtime: Optional[tuple]
    install: Optional[tuple]
    closure: Optional[tuple]
    phase_bindings: tuple
    cutover_head: Optional[tuple]
    audit_head: Optional[tuple]
    worker_head: Optional[tuple]
    allocation_head: Optional[tuple]
    dispatch_head: Optional[tuple]
    receipt_identity: tuple
    business_high_water: tuple


class _SnapshotOutcome(NamedTuple):
    result: SQLiteCutoverDurableAuditResult
    fence: _AuditFence


def _lower_hex(value: Any, length: int, name: str) -> str:
    if (
        type(value) is not str
        or len(value) != length
        or any(character not in _LOWER_HEX for character in value)
    ):
        raise TypeError(name + " must be lowercase hexadecimal")
    return value


def _digest(value: Any, name: str) -> bytes:
    if type(value) is not bytes or len(value) != 32:
        raise TypeError(name + " must be exact 32-byte bytes")
    return value


def _row_is(row: Any, expected: Sequence[Any]) -> bool:
    if row is None:
        return False
    observed = _row_tuple(row)
    return len(observed) == len(expected) and all(
        _same(observed[index], expected[index]) for index in range(len(expected))
    )


def _count(connection: Any, table: str) -> int:
    row = connection.execute('SELECT count(*) FROM "' + table + '"').fetchone()
    if row is None or len(row) != 1 or type(row[0]) is not int or row[0] < 0:
        raise _AuditCorrupt("cutover_durable_audit_count_invalid")
    return row[0]


def _single_row(connection: Any, sql: str, values: tuple = ()) -> Any:
    cursor = connection.execute(sql, values)
    rows = cursor.fetchmany(2)
    cursor.close()
    if len(rows) != 1:
        raise _AuditCorrupt("cutover_durable_audit_singleton_invalid")
    return rows[0]


def _database_time(connection: Any) -> str:
    row = connection.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')").fetchone()
    if row is None or len(row) != 1 or type(row[0]) is not str or len(row[0]) != 24:
        raise _AuditCorrupt("cutover_durable_audit_clock_invalid")
    return row[0]


def _cleanup(connection: Any) -> bool:
    complete = True
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


def _process_control_cleanup(connection: Any) -> None:
    """Cleanup may fail, but it can never replace process-control."""

    try:
        _cleanup(connection)
    except BaseException:
        try:
            connection.close()
        except BaseException:
            pass


def _checked_cleanup(connection: Any) -> bool:
    """Retry best-effort close without replacing cleanup process-control."""

    try:
        return _cleanup(connection)
    except BaseException:
        _process_control_cleanup(connection)
        raise


def _current_cutover_ids(connection: Any) -> tuple[str, ...]:
    tables = (
        "repository_runtime_control",
        "business_cutover_head",
        "business_cutover_install_receipts",
        "business_asset_stage_receipts",
        "business_asset_stage_members",
        "business_asset_coverage_receipts",
        "repository_audit_certificates",
        "business_cutover_activation_receipts",
        "business_cutover_mount_receipts",
        "business_commits",
        "business_cutover_install_artifacts",
        "business_cutover_install_artifact_chunks",
        "business_cutover_baseline_atomic_receipts",
        "business_cutover_install_closures",
        "business_cutover_asset_provider_bindings",
        "business_cutover_audit_bindings",
        "business_cutover_activation_bindings",
        "business_cutover_mount_bindings",
    )
    sql = " UNION ".join(
        'SELECT cutover_id FROM "' + table + '"' for table in tables
    ) + " ORDER BY cutover_id COLLATE BINARY"
    values = tuple(row[0] for row in connection.execute(sql))
    if any(type(value) is not str for value in values):
        raise _AuditCorrupt("cutover_durable_audit_cutover_identity_invalid")
    return values


def _raw_fence(
    database: SqliteDatabase,
    connection: Any,
    storage_identity: tuple,
) -> _AuditFence:
    fingerprint = connection.execute(
        "SELECT value FROM kernel_metadata WHERE key='schema_fingerprint'"
    ).fetchall()
    marker = tuple(
        _row_tuple(row)
        for row in connection.execute(
            "SELECT singleton,authority,created_at FROM kernel_repository ORDER BY singleton"
        )
    )
    runtime_rows = connection.execute(
        "SELECT singleton,authority,authority_state,repository_epoch,cutover_id,"
        "source_admission_revision,repository_revision,state_revision,"
        "runtime_generation,code_digest,schema_version,updated_at "
        "FROM repository_runtime_control ORDER BY singleton"
    ).fetchall()
    install_rows = connection.execute(
        "SELECT cutover_id,transaction_id,committed_at,manifest_sha256 "
        "FROM business_cutover_install_receipts ORDER BY cutover_id"
    ).fetchall()
    closure_rows = connection.execute(
        "SELECT cutover_id,install_transaction_id,install_manifest_sha256,"
        "coverage_transaction_id,coverage_manifest_sha256,manifest_sha256 "
        "FROM business_cutover_install_closures ORDER BY cutover_id"
    ).fetchall()

    def optional(sql: str) -> Optional[tuple]:
        rows = connection.execute(sql).fetchall()
        if len(rows) > 1:
            return ("multiple", len(rows))
        return None if not rows else _row_tuple(rows[0])

    try:
        receipt_count, receipt_root = _read_receipt_namespace_identity(connection)
        receipt_identity = ("canonical", receipt_count, receipt_root)
    except (SQLiteKernelError, ReceiptNamespaceIntegrityError):
        # A corrupt namespace still needs a stable publication fence.  This is
        # deliberately not the protocol root: it only fingerprints raw SQLite
        # type/value bytes so a second full corrupt audit can prove it observed
        # the same damaged fact set.
        hasher = hashlib.sha256()
        raw_count = 0
        for raw in connection.execute(
            "SELECT transaction_id,kind,committed_at FROM transaction_receipts "
            "ORDER BY CAST(transaction_id AS BLOB),CAST(kind AS BLOB)"
        ):
            raw_count += 1
            for value in _row_tuple(raw):
                if value is None:
                    tag, payload = b"none", b""
                elif type(value) is bytes:
                    tag, payload = b"bytes", value
                elif type(value) is str:
                    tag, payload = b"text", value.encode("utf-8", "strict")
                elif type(value) is int:
                    tag, payload = b"int", str(value).encode("ascii")
                else:
                    tag, payload = type(value).__name__.encode("ascii"), repr(value).encode("utf-8")
                for part in (tag, payload):
                    hasher.update(len(part).to_bytes(8, "big"))
                    hasher.update(part)
        receipt_identity = ("raw", raw_count, hasher.digest())
    high = connection.execute(
        "SELECT count(*),max(repository_revision),"
        "(SELECT manifest_sha256 FROM business_commits ORDER BY repository_revision DESC LIMIT 1) "
        "FROM business_commits"
    ).fetchone()
    if high is None:
        raise _AuditUnavailable("cutover_durable_audit_fence_unavailable")
    return _AuditFence(
        storage_identity=storage_identity,
        schema_fingerprint=tuple(_row_tuple(row) for row in fingerprint),
        marker=marker,
        runtime=None if not runtime_rows else tuple(_row_tuple(row) for row in runtime_rows),
        install=None if not install_rows else tuple(_row_tuple(row) for row in install_rows),
        closure=None if not closure_rows else tuple(_row_tuple(row) for row in closure_rows),
        phase_bindings=tuple(
            (table,) + _row_tuple(row)
            for table in (
                "business_cutover_asset_provider_bindings",
                "business_cutover_audit_bindings",
                "business_cutover_activation_bindings",
                "business_cutover_mount_bindings",
            )
            for row in connection.execute(
                'SELECT cutover_id,predecessor_head_sha256,'
                'successor_head_sha256,binding_manifest_sha256 FROM "'
                + table + '" ORDER BY cutover_id'
            )
        ),
        cutover_head=optional(
            "SELECT " + ",".join(_CUTOVER_HEAD_ROW_COLUMNS) + " FROM business_cutover_head"
        ),
        audit_head=optional(
            "SELECT singleton,state_revision,sequence,certificate_id,"
            "previous_tail_sha256,tail_sha256,updated_at FROM repository_audit_head"
        ),
        worker_head=optional(
            "SELECT singleton,state_revision,sequence,previous_tail_sha256,"
            "tail_sha256,updated_at FROM worker_ledger_head"
        ),
        allocation_head=optional(
            "SELECT singleton,state_revision,sequence,previous_tail_sha256,"
            "tail_sha256,updated_at FROM outbox_allocation_head"
        ),
        dispatch_head=optional(
            "SELECT singleton,state_revision,published_sequence,"
            "published_repository_revision,published_ordinal,inflight_sequence,"
            "inflight_repository_revision,inflight_ordinal,previous_head_sha256,"
            "head_sha256,updated_at FROM outbox_dispatch_head"
        ),
        receipt_identity=receipt_identity,
        business_high_water=_row_tuple(high),
    )


def _verify_schema_and_marker(database: SqliteDatabase, connection: Any) -> str:
    if _pragma_int(connection, "application_id") != APPLICATION_ID:
        raise _AuditCorrupt("cutover_durable_audit_application_id_invalid")
    if _pragma_int(connection, "user_version") != MIGRATIONS[-1].version:
        raise _AuditCorrupt("cutover_durable_audit_schema_version_invalid")
    _verify_security_profile(database, connection)
    database._verify_schema(connection)
    rows = connection.execute(
        "SELECT singleton,authority,created_at FROM kernel_repository ORDER BY singleton"
    ).fetchall()
    if len(rows) != 1 or rows[0]["singleton"] != 1 or rows[0]["authority"] not in (
        "legacy",
        "sqlite",
    ) or not _canonical_utc(rows[0]["created_at"]):
        raise _AuditCorrupt("cutover_durable_audit_marker_invalid")
    return rows[0]["authority"]


def _audit_absent(
    database: SqliteDatabase,
    connection: Any,
    budget: SQLiteCutoverAuditBudget,
) -> SQLiteCutoverDurableAuditResult:
    if any(_count(connection, table) for table in _INSTALL_OWNED_TABLES):
        raise _AuditCorrupt("cutover_durable_audit_partial_install_namespace")
    _verify_legacy_receipt_namespace(connection)
    _verify_relational_integrity(connection)
    database._verify_legacy_evidence_state(connection)
    database._verify_repository_proof_state(connection)
    database._verify_inactive_business_transaction_state(connection)
    # Stage/coverage are an install prerequisite owned by the later asset
    # provider.  Until its plan-free durable binding is shipped, a non-empty
    # prerequisite namespace is a legal but unsupported preinstall shape, not
    # proof of absence and not corruption.
    stage_count = _count(connection, "business_asset_stage_receipts")
    coverage_count = _count(connection, "business_asset_coverage_receipts")
    if stage_count or coverage_count:
        _verify_production_receipt_namespace(connection)
        coverage_rows = connection.execute(
            "SELECT cutover_id,transaction_id,receipt_kind,batch_count "
            "FROM business_asset_coverage_receipts"
        ).fetchall()
        stage_rows = connection.execute(
            "SELECT cutover_id,transaction_id,receipt_kind "
            "FROM business_asset_stage_receipts ORDER BY batch_id"
        ).fetchall()
        cutover_ids = _current_cutover_ids(connection)
        if len(coverage_rows) > 1 or any(
            type(row[0]) is not str for row in stage_rows
        ):
            raise _AuditCorrupt("cutover_durable_audit_preinstall_structure_invalid")
        if coverage_rows:
            coverage = coverage_rows[0]
            if (
                type(coverage[0]) is not str
                or type(coverage[3]) is not int
                or coverage[3] != len(stage_rows)
                or any(row[0] != coverage[0] for row in stage_rows)
                or cutover_ids != (coverage[0],)
            ):
                raise _AuditCorrupt(
                    "cutover_durable_audit_preinstall_structure_invalid"
                )
        elif (
            not stage_rows
            or len(cutover_ids) != 1
            or any(row[0] != cutover_ids[0] for row in stage_rows)
        ):
            raise _AuditCorrupt("cutover_durable_audit_preinstall_structure_invalid")
        raise _AuditUnsupported("cutover_durable_audit_preinstall_unproven")
    database._validate_initialized(connection)
    _verify_relational_integrity(connection)
    return SQLiteCutoverDurableAuditResult(
        SQLiteCutoverDurableAuditStatus.ABSENT,
        "cutover_durable_audit_absent",
        budget=budget,
    )


def _install_state(
    connection: Any,
    *,
    snapshot_now: str,
    require_current_install_snapshot: bool,
) -> tuple[Any, Any, Any, Any]:
    row = _single_row(
        connection,
        "SELECT cutover_id,transaction_id,receipt_kind,committed_at,"
        "repository_epoch,runtime_generation,code_digest,member_mapping_sha256,"
        "manifest_bytes,manifest_sha256 FROM business_cutover_install_receipts",
    )
    try:
        stored = _parse_stored_install_manifest(row[8])
    except BusinessCutoverStoredManifestError as exc:
        raise _AuditCorrupt("cutover_durable_audit_install_manifest_invalid") from exc
    evidence = stored.evidence
    if (
        stored.target_schema_version != MIGRATIONS[-1].version
        or stored.target_schema_fingerprint != _expected_schema_fingerprint()
    ):
        raise _AuditCorrupt("cutover_durable_audit_install_target_schema_invalid")
    if not _row_is(
        row,
        (
            evidence.cutover_id,
            evidence.transaction_id,
            evidence.receipt_kind,
            row[3],
            evidence.runtime.repository_epoch,
            evidence.runtime.runtime_generation,
            evidence.runtime.code_digest,
            evidence.member_mapping_sha256,
            evidence.manifest_bytes,
            evidence.manifest_sha256,
        ),
    ):
        raise _AuditCorrupt("cutover_durable_audit_install_row_invalid")
    if row[3] > snapshot_now:
        raise _AuditCorrupt("cutover_durable_audit_install_time_future")
    try:
        closure = _require_cutover_install_closure_binding(
            _read_business_cutover_install_closure(
                connection,
                cutover_id=evidence.cutover_id,
                install_transaction_id=evidence.transaction_id,
                install_manifest_sha256=evidence.manifest_sha256,
                install_committed_at=row[3],
                target_schema_version=stored.target_schema_version,
                target_schema_fingerprint=stored.target_schema_fingerprint,
                require_current_install_snapshot=(
                    require_current_install_snapshot
                ),
            )
        )
    except (SQLiteCutoverInstallClosureError, SQLiteCutoverInstallProvenanceError) as exc:
        if _failure_is_unavailable(exc):
            raise _AuditUnavailable(
                "cutover_durable_audit_install_closure_unavailable"
            ) from exc
        raise _AuditCorrupt("cutover_durable_audit_install_closure_invalid") from exc
    coverage = _single_row(
        connection,
        "SELECT cutover_id,transaction_id,receipt_kind,committed_at,manifest_sha256,"
        "coverage_sha256 FROM business_asset_coverage_receipts",
    )
    if (
        coverage[0] != evidence.cutover_id
        or coverage[1] != "_business-cutover-asset-coverage:" + evidence.cutover_id
        or coverage[2] != "_business_cutover_asset_coverage_v1"
        or coverage[4] != closure.coverage_manifest_sha256
        or coverage[5] != closure.coverage_sha256
        or type(coverage[3]) is not str
        or coverage[3] > row[3]
    ):
        raise _AuditCorrupt("cutover_durable_audit_coverage_pointer_invalid")
    return row, stored, closure, coverage


def _audit_ledger(
    connection: Any,
    *,
    cutover_id: str,
    install_manifest_sha256: bytes,
    install_runtime: Any,
    install_committed_at: str,
    snapshot_now: str,
    current_runtime: Any,
    current_receipt_count: int,
) -> Any:
    rows = connection.execute(
        "SELECT " + ",".join(_AUDIT_ROW_COLUMNS)
        + " FROM repository_audit_certificates ORDER BY sequence LIMIT 257"
    ).fetchall()
    if not rows:
        raise _AuditCorrupt("cutover_durable_audit_certificate_missing")
    if len(rows) > MAX_CUTOVER_DURABLE_AUDIT_CERTIFICATES:
        raise _AuditUnsupported("cutover_durable_audit_certificate_budget_exceeded")
    if sum(
        len(row[18]) if type(row[18]) is bytes else 0
        for row in rows
    ) > MAX_CUTOVER_DURABLE_AUDIT_CERTIFICATE_BYTES:
        raise _AuditUnsupported("cutover_durable_audit_certificate_budget_exceeded")
    previous = None
    evidences = []
    for index, row in enumerate(rows):
        try:
            projection = _audit_evidence_from_manifest(row[18])
        except (SQLiteProductionAuthorityError, RepositoryAuditContractError, RepositoryAuditIntegrityError) as exc:
            raise _AuditCorrupt("cutover_durable_audit_certificate_manifest_invalid") from exc
        evidence = projection.evidence
        stored_previous_tail = (
            EMPTY_REPOSITORY_AUDIT_CERTIFICATE_TAIL_SHA256
            if evidence.previous_certificate_tail_sha256 is None
            else evidence.previous_certificate_tail_sha256
        )
        expected = (
            evidence.certificate_id,
            evidence.runtime.cutover_id,
            evidence.sequence,
            evidence.purpose.value,
            evidence.transaction_id,
            evidence.receipt_kind,
            evidence.receipt_parent.committed_at,
            evidence.previous_certificate_id,
            evidence.previous_manifest_sha256,
            stored_previous_tail,
            evidence.schema.user_version,
            evidence.schema.migration_ledger_count,
            evidence.schema.migration_ledger_sha256,
            evidence.coverage.receipt_namespace_count,
            evidence.coverage.receipt_namespace_sha256,
            evidence.coverage.worker_ledger_sequence,
            evidence.coverage.worker_ledger_tail_sha256,
            projection.findings_sha256,
            evidence.manifest_bytes,
            evidence.manifest_sha256,
            evidence.certificate_tail_sha256,
        )
        if not _row_is(row, expected):
            raise _AuditCorrupt("cutover_durable_audit_certificate_row_invalid")
        parent = _single_row(
            connection,
            "SELECT transaction_id,kind,committed_at FROM transaction_receipts "
            "WHERE transaction_id=?",
            (evidence.transaction_id,),
        )
        if not _row_is(
            parent,
            (
                evidence.transaction_id,
                evidence.receipt_kind,
                evidence.findings.completed_at,
            ),
        ):
            raise _AuditCorrupt("cutover_durable_audit_certificate_parent_invalid")
        if (
            evidence.runtime.cutover_id != cutover_id
            or evidence.coverage.cutover_install_manifest_sha256
            != install_manifest_sha256
            or evidence.coverage.covered_business_revision
            != evidence.runtime.repository_revision
            or evidence.schema.user_version != MIGRATIONS[-1].version
            or evidence.schema.schema_fingerprint
            != _expected_schema_fingerprint()
            or evidence.schema.migration_ledger_count != MIGRATIONS[-1].version
            or evidence.schema.migration_ledger_sha256
            != _expected_migration_ledger_sha256()
            or evidence.runtime.updated_at > evidence.findings.started_at
            or evidence.findings.completed_at > snapshot_now
            or evidence.runtime.repository_revision
            > current_runtime.repository_revision
            or evidence.runtime.state_revision > current_runtime.state_revision
            or evidence.runtime.updated_at > current_runtime.updated_at
            or evidence.coverage.receipt_namespace_count
            + len(rows)
            - index
            > current_receipt_count
        ):
            raise _AuditCorrupt("cutover_durable_audit_certificate_snapshot_invalid")
        if previous is None:
            if evidence.sequence != 1 or evidence.previous_certificate_id is not None:
                raise _AuditCorrupt("cutover_durable_audit_certificate_chain_invalid")
        elif (
            evidence.sequence != previous.sequence + 1
            or evidence.previous_certificate_id != previous.certificate_id
            or evidence.previous_manifest_sha256 != previous.manifest_sha256
            or evidence.previous_certificate_tail_sha256 != previous.certificate_tail_sha256
        ):
            raise _AuditCorrupt("cutover_durable_audit_certificate_chain_invalid")
        if previous is not None:
            try:
                _require_predecessor(
                    RepositoryAuditObservedInput(
                        certificate_id=evidence.certificate_id,
                        purpose=evidence.purpose,
                        runtime=evidence.runtime,
                        schema=evidence.schema,
                        coverage=evidence.coverage,
                        findings=evidence.findings,
                    ),
                    previous,
                )
            except (RepositoryAuditContractError, RepositoryAuditIntegrityError) as exc:
                raise _AuditCorrupt(
                    "cutover_durable_audit_certificate_predecessor_invalid"
                ) from exc
        previous = evidence
        evidences.append(evidence)
    head = _single_row(
        connection,
        "SELECT singleton,state_revision,sequence,certificate_id,"
        "previous_tail_sha256,tail_sha256,updated_at FROM repository_audit_head",
    )
    final = evidences[-1]
    final_previous = (
        _ZERO_SHA256
        if final.previous_certificate_tail_sha256 is None
        else final.previous_certificate_tail_sha256
    )
    if not _row_is(
        head,
        (
            1,
            final.sequence,
            final.sequence,
            final.certificate_id,
            final_previous,
            final.certificate_tail_sha256,
            final.findings.completed_at,
        ),
    ):
        raise _AuditCorrupt("cutover_durable_audit_certificate_head_invalid")
    current_worker_head = _single_row(
        connection,
        "SELECT sequence,tail_sha256 FROM worker_ledger_head WHERE singleton=1",
    )
    for evidence in evidences:
        worker_sequence = evidence.coverage.worker_ledger_sequence
        if (
            type(current_worker_head[0]) is not int
            or worker_sequence > current_worker_head[0]
        ):
            worker_exact = False
        elif worker_sequence == current_worker_head[0]:
            worker_exact = (
                evidence.coverage.worker_ledger_tail_sha256
                == current_worker_head[1]
            )
        elif worker_sequence == 0:
            worker_exact = (
                evidence.coverage.worker_ledger_tail_sha256 == _ZERO_SHA256
            )
        else:
            worker_row = connection.execute(
                "SELECT ledger_tail_sha256 FROM worker_ledger_entries "
                "WHERE sequence=?",
                (worker_sequence,),
            ).fetchone()
            worker_exact = bool(
                worker_row is not None
                and worker_row[0]
                == evidence.coverage.worker_ledger_tail_sha256
            )
        if not worker_exact:
            raise _AuditCorrupt(
                "cutover_durable_audit_certificate_worker_coverage_invalid"
            )
    install_audit = evidences[0]
    if (
        install_audit.purpose is not RepositoryAuditPurpose.INSTALL
        or install_audit.runtime.cutover_id != cutover_id
        or install_audit.runtime.authority != "sqlite"
        or install_audit.runtime.authority_state.value != "read_only"
        or install_audit.runtime.repository_epoch != install_runtime.repository_epoch
        or install_audit.runtime.source_admission_revision
        != install_runtime.source_admission_revision
        or install_audit.runtime.repository_revision != install_runtime.repository_revision
        or install_audit.runtime.state_revision != install_runtime.state_revision
        or install_audit.runtime.runtime_generation != install_runtime.runtime_generation
        or install_audit.runtime.code_digest != install_runtime.code_digest
        or install_audit.runtime.updated_at != install_committed_at
        or install_audit.findings.started_at < install_committed_at
        or install_audit.coverage.cutover_install_manifest_sha256
        != install_manifest_sha256
        or install_audit.coverage.covered_business_revision
        != install_runtime.repository_revision
        or install_audit.coverage.worker_ledger_sequence != 0
        or install_audit.coverage.worker_ledger_tail_sha256 != _ZERO_SHA256
        or install_audit.schema.user_version != MIGRATIONS[-1].version
        or install_audit.schema.schema_fingerprint != _expected_schema_fingerprint()
        or install_audit.schema.migration_ledger_count != MIGRATIONS[-1].version
        or install_audit.schema.migration_ledger_sha256
        != _expected_migration_ledger_sha256()
        or install_audit.findings.page_integrity != "ok"
        or install_audit.findings.foreign_key_violation_count != 0
        or install_audit.findings.namespace_violation_count != 0
    ):
        raise _AuditCorrupt("cutover_durable_audit_install_certificate_invalid")

    # The first certificate was issued while the repository was still
    # read-only.  Its historical receipt namespace can therefore be rebuilt
    # without treating wall-clock timestamps as a commit order: exclude every
    # receipt that is causally known to be after install audit publication.
    # Later periodic certificates have no authenticated global checkpoint and
    # are intentionally validated only as a chain suffix below.
    try:
        excluded_cursor = connection.execute(
            "SELECT transaction_id FROM repository_audit_certificates "
            "UNION ALL SELECT transaction_id FROM business_cutover_activation_receipts "
            "UNION ALL SELECT transaction_id FROM business_cutover_mount_receipts "
            "UNION ALL SELECT business.transaction_id FROM business_commits AS business "
            "LEFT JOIN business_cutover_baseline_atomic_receipts AS baseline "
            "ON baseline.transaction_id=business.transaction_id "
            "WHERE baseline.transaction_id IS NULL "
            "UNION ALL SELECT transaction_id FROM worker_execution_receipts "
            "UNION ALL SELECT transaction_id FROM pipeline_control_receipts"
        )
        excluded = tuple(row[0] for row in excluded_cursor)
        excluded_cursor.close()
        if any(type(value) is not str or not value for value in excluded):
            raise _AuditCorrupt(
                "cutover_durable_audit_install_receipt_coverage_invalid"
            )
        receipt_count, receipt_root = _read_receipt_namespace_identity(
            connection,
            excluded_transaction_ids=excluded,
        )
    except (_AuditCorrupt, _AuditUnavailable):
        raise
    except Exception as exc:
        if _failure_is_unavailable(exc):
            raise _AuditUnavailable(
                "cutover_durable_audit_install_receipt_coverage_unavailable"
            ) from exc
        raise _AuditCorrupt(
            "cutover_durable_audit_install_receipt_coverage_invalid"
        ) from exc
    if (
        receipt_count != install_audit.coverage.receipt_namespace_count
        or receipt_root != install_audit.coverage.receipt_namespace_sha256
    ):
        raise _AuditCorrupt(
            "cutover_durable_audit_install_receipt_coverage_invalid"
        )

    return install_audit, len(evidences) != 1


def _activation(
    connection: Any,
    *,
    install: Any,
    stored_install: Any,
    audit: Any,
    snapshot_now: str,
) -> Any:
    row = _single_row(
        connection,
        "SELECT " + ",".join(_ACTIVATION_ROW_COLUMNS)
        + " FROM business_cutover_activation_receipts",
    )
    try:
        evidence = _activation_evidence_from_manifest(row[9])
    except (
        SQLiteProductionAuthorityError,
        BusinessCutoverActivationContractError,
        BusinessCutoverActivationIntegrityError,
    ) as exc:
        raise _AuditCorrupt("cutover_durable_audit_activation_manifest_invalid") from exc
    if not _row_is(
        row,
        (
            evidence.cutover_id,
            evidence.transaction_id,
            evidence.receipt_kind,
            evidence.receipt_parent.committed_at,
            evidence.install_transaction_id,
            evidence.install_manifest_sha256,
            evidence.audit_transaction_id,
            evidence.audit_manifest_sha256,
            evidence.expected_migration_ledger_sha256,
            evidence.manifest_bytes,
            evidence.manifest_sha256,
        ),
    ):
        raise _AuditCorrupt("cutover_durable_audit_activation_row_invalid")
    if (
        evidence.cutover_id != stored_install.evidence.cutover_id
        or evidence.install_transaction_id != stored_install.evidence.transaction_id
        or evidence.install_manifest_sha256 != stored_install.evidence.manifest_sha256
        or evidence.audit_certificate_id != audit.certificate_id
        or evidence.audit_transaction_id != audit.transaction_id
        or evidence.audit_manifest_sha256 != audit.manifest_sha256
        or evidence.audit_certificate_tail_sha256 != audit.certificate_tail_sha256
        or evidence.before_runtime != stored_install.evidence.runtime
        or evidence.final_admission_sha256 != stored_install.final_admission_sha256
        or evidence.actual_schema.user_version != MIGRATIONS[-1].version
        or evidence.actual_schema.schema_fingerprint != _expected_schema_fingerprint()
        or evidence.actual_schema.migration_ledger_sha256
        != _expected_migration_ledger_sha256()
        or evidence.audit_completed_at > evidence.activated_at
        or evidence.activated_at > snapshot_now
    ):
        raise _AuditCorrupt("cutover_durable_audit_activation_binding_invalid")
    return evidence


def _head_state(
    connection: Any,
    *,
    install_row: Any,
    stored_install: Any,
    coverage_row: Any,
    audit: Any,
    activation: Any,
    snapshot_now: str,
) -> tuple[SQLiteCutoverDurablePhase, Optional[Any], Optional[Any]]:
    rows = connection.execute(
        "SELECT " + ",".join(_CUTOVER_HEAD_ROW_COLUMNS)
        + " FROM business_cutover_head ORDER BY singleton"
    ).fetchall()
    if not rows:
        if any(
            _count(connection, table)
            for table in (
                "repository_audit_certificates",
                "repository_audit_head",
                "business_cutover_activation_receipts",
                "business_cutover_mount_receipts",
            )
        ):
            raise _AuditCorrupt("cutover_durable_audit_headless_suffix_invalid")
        return SQLiteCutoverDurablePhase.INSTALLED_CLOSURE, None, None
    if len(rows) != 1:
        raise _AuditCorrupt("cutover_durable_audit_head_count_invalid")
    row = rows[0]
    revision = row["phase_revision"]
    if type(revision) is not int or revision not in (1, 2, 3, 4, 5):
        raise _AuditCorrupt("cutover_durable_audit_head_phase_invalid")
    pointers = [BusinessCutoverHeadPointer(
        BusinessCutoverHeadPointerRole.INSTALL,
        stored_install.evidence.transaction_id,
        stored_install.evidence.receipt_kind,
        install_row[3],
        stored_install.evidence.manifest_sha256,
    )]
    if revision >= 2:
        pointers.append(BusinessCutoverHeadPointer(
            BusinessCutoverHeadPointerRole.COVERAGE,
            coverage_row[1],
            coverage_row[2],
            coverage_row[3],
            coverage_row[4],
        ))
    if revision >= 3:
        if audit is None:
            raise _AuditCorrupt("cutover_durable_audit_head_audit_missing")
        pointers.append(BusinessCutoverHeadPointer(
            BusinessCutoverHeadPointerRole.AUDIT,
            audit.transaction_id,
            audit.receipt_kind,
            audit.findings.completed_at,
            audit.manifest_sha256,
        ))
    if revision >= 4:
        if activation is None:
            raise _AuditCorrupt("cutover_durable_audit_head_activation_missing")
        pointers.append(BusinessCutoverHeadPointer(
            BusinessCutoverHeadPointerRole.ACTIVATION,
            activation.transaction_id,
            activation.receipt_kind,
            activation.activated_at,
            activation.manifest_sha256,
        ))
    mount_binding = None
    mount_evidence = None
    if revision >= 5:
        mounted_row = _mounted_row(connection)
        if mounted_row is None:
            raise _AuditCorrupt("cutover_durable_audit_mount_missing")
        try:
            # The strict mounted reader repeats every install/coverage/audit/
            # activation/mount row comparison and V12 semantic closure.
            mount_binding = mounted_row
            mount_evidence = _mount_evidence_from_row(mounted_row)
        except (
            SQLiteProductionAuthorityError,
            BusinessCutoverMountContractError,
            BusinessCutoverMountIntegrityError,
        ) as exc:
            raise _AuditCorrupt("cutover_durable_audit_mount_manifest_invalid") from exc
        if mount_evidence.mounted_at > snapshot_now:
            raise _AuditCorrupt("cutover_durable_audit_mount_time_future")
        pointers.append(BusinessCutoverHeadPointer(
            BusinessCutoverHeadPointerRole.MOUNT,
            mount_evidence.transaction_id,
            mount_evidence.receipt_kind,
            mount_evidence.mounted_at,
            mount_evidence.manifest_sha256,
        ))
    try:
        state = _issue_initial_business_cutover_head(
            cutover_id=stored_install.evidence.cutover_id,
            repository_epoch=stored_install.evidence.runtime.repository_epoch,
            runtime_generation=stored_install.evidence.runtime.runtime_generation,
            code_digest=stored_install.evidence.runtime.code_digest,
            install=pointers[0],
        )
        for pointer in pointers[1:]:
            state = _advance_business_cutover_head(
                state,
                cutover_id=stored_install.evidence.cutover_id,
                repository_epoch=stored_install.evidence.runtime.repository_epoch,
                runtime_generation=stored_install.evidence.runtime.runtime_generation,
                code_digest=stored_install.evidence.runtime.code_digest,
                pointer=pointer,
            )
    except BusinessCutoverHeadIntegrityError as exc:
        raise _AuditCorrupt("cutover_durable_audit_head_replay_invalid") from exc
    expected = state.row_values()
    if not _row_is(row, tuple(expected[name] for name in _CUTOVER_HEAD_ROW_COLUMNS)):
        raise _AuditCorrupt("cutover_durable_audit_head_row_invalid")
    phase = {
        1: SQLiteCutoverDurablePhase.INSTALLED_HEAD,
        2: SQLiteCutoverDurablePhase.ASSETS_COVERED,
        3: SQLiteCutoverDurablePhase.AUDITED,
        4: SQLiteCutoverDurablePhase.ACTIVATED,
        5: SQLiteCutoverDurablePhase.MOUNTED,
    }[revision]
    return phase, state, mount_evidence


def _v13_binding_shape_exact(
    connection: Any,
    *,
    cutover_id: str,
    revision: int,
) -> bool:
    """Require the fresh all-field semantic V13 carrier prefix."""

    if revision == 0:
        return not any(
            _count(connection, table)
            for table in (
                "business_cutover_asset_provider_bindings",
                "business_cutover_audit_bindings",
                "business_cutover_activation_bindings",
                "business_cutover_mount_bindings",
            )
        )
    try:
        prefix = _read_cutover_phase_binding_prefix(connection)
    except SQLiteCutoverPhaseBindingReaderError:
        return False
    return bool(
        prefix.cutover_id == cutover_id
        and prefix.current_head.phase_revision == revision
        and len(prefix.bindings) == max(0, revision - 1)
    )


def _snapshot_audit(
    database: SqliteDatabase,
    connection: Any,
    *,
    marker: str,
    expected: Optional[SQLiteCutoverDurableIdentity],
    budget: SQLiteCutoverAuditBudget,
) -> SQLiteCutoverDurableAuditResult:
    _verify_legacy_receipt_namespace(connection)
    _verify_production_receipt_namespace(connection)
    if marker == "legacy":
        return _audit_absent(database, connection, budget)
    profile = _validate_business_runtime_connection(
        database, connection, access=BusinessRuntimeAccess.READER
    )
    snapshot_now = _database_time(connection)
    observation = _audit_business_runtime_connection(
        database, connection, access=BusinessRuntimeAccess.READER
    )
    baseline_sidecar_count = _count(
        connection, "business_cutover_baseline_atomic_receipts"
    )
    business_count = _count(connection, "business_commits")
    atomic_count = _count(connection, "atomic_business_write_receipts")
    suffix_count = sum(
        _count(connection, table) for table in _POST_INSTALL_SUFFIX_TABLES
    )
    zero_post_install_suffix = (
        business_count == baseline_sidecar_count
        and atomic_count == baseline_sidecar_count
        and suffix_count == 0
    )
    install_row, stored_install, closure, coverage_row = _install_state(
        connection,
        snapshot_now=snapshot_now,
        require_current_install_snapshot=zero_post_install_suffix,
    )
    ids = _current_cutover_ids(connection)
    if ids != (stored_install.evidence.cutover_id,):
        raise _AuditCorrupt("cutover_durable_audit_mixed_cutover_identity")
    baseline_count = closure.baseline_commit_count
    if (
        baseline_sidecar_count != baseline_count
        or business_count < baseline_count
        or atomic_count < baseline_count
        or business_count != atomic_count
    ):
        raise _AuditCorrupt("cutover_durable_audit_business_suffix_invalid")
    has_post_install_suffix = business_count > baseline_count or suffix_count > 0

    head_rows = connection.execute(
        "SELECT phase_revision FROM business_cutover_head"
    ).fetchall()
    revision = 0 if not head_rows else head_rows[0][0]
    if len(head_rows) > 1 or type(revision) is not int or revision not in range(0, 6):
        raise _AuditCorrupt("cutover_durable_audit_head_phase_invalid")
    if not _v13_binding_shape_exact(
        connection,
        cutover_id=stored_install.evidence.cutover_id,
        revision=revision,
    ):
        raise _AuditCorrupt("cutover_durable_audit_phase_binding_invalid")
    if has_post_install_suffix and revision < 5:
        raise _AuditCorrupt("cutover_durable_audit_premount_suffix_invalid")

    audit = None
    has_audit_suffix = False
    if revision >= 3:
        audit, has_audit_suffix = _audit_ledger(
            connection,
            cutover_id=stored_install.evidence.cutover_id,
            install_manifest_sha256=stored_install.evidence.manifest_sha256,
            install_runtime=stored_install.evidence.runtime,
            install_committed_at=install_row[3],
            snapshot_now=snapshot_now,
            current_runtime=profile,
            current_receipt_count=observation.receipt_namespace_count,
        )
    elif _count(connection, "repository_audit_certificates") or _count(
        connection, "repository_audit_head"
    ):
        raise _AuditCorrupt("cutover_durable_audit_premature_audit_suffix")

    activation = None
    if revision >= 4:
        activation = _activation(
            connection,
            install=install_row,
            stored_install=stored_install,
            audit=audit,
            snapshot_now=snapshot_now,
        )
    elif _count(connection, "business_cutover_activation_receipts"):
        raise _AuditCorrupt("cutover_durable_audit_premature_activation_suffix")
    if revision < 5 and _count(connection, "business_cutover_mount_receipts"):
        raise _AuditCorrupt("cutover_durable_audit_premature_mount_suffix")

    phase, head, mount = _head_state(
        connection,
        install_row=install_row,
        stored_install=stored_install,
        coverage_row=coverage_row,
        audit=audit,
        activation=activation,
        snapshot_now=snapshot_now,
    )
    phase_revision = _PHASE_REVISIONS[phase]
    if phase_revision != revision:
        raise _AuditCorrupt("cutover_durable_audit_head_phase_changed")
    expected_state = "read_only" if revision <= 3 else "active"
    expected_control_revision = 1 if revision <= 3 else 2
    expected_runtime_updated_at = (
        install_row[3]
        if revision <= 3
        else activation.activated_at if activation is not None else None
    )
    if (
        profile.cutover_id != stored_install.evidence.cutover_id
        or profile.repository_epoch != stored_install.evidence.runtime.repository_epoch
        or profile.source_admission_revision
        != stored_install.evidence.runtime.source_admission_revision
        or profile.runtime_generation != stored_install.evidence.runtime.runtime_generation
        or profile.code_digest != stored_install.evidence.runtime.code_digest
        or (
            not has_post_install_suffix
            and profile.repository_revision
            != stored_install.evidence.runtime.repository_revision
        )
        or (
            has_post_install_suffix
            and profile.repository_revision
            < stored_install.evidence.runtime.repository_revision
        )
        or profile.authority_state.value != expected_state
        or (
            not has_post_install_suffix
            and profile.state_revision != expected_control_revision
        )
        or (
            has_post_install_suffix
            and profile.state_revision < expected_control_revision
        )
        or (
            not has_post_install_suffix
            and profile.updated_at != expected_runtime_updated_at
        )
        or (
            has_post_install_suffix
            and (
                expected_runtime_updated_at is None
                or profile.updated_at < expected_runtime_updated_at
            )
        )
        or profile.updated_at > snapshot_now
    ):
        raise _AuditCorrupt("cutover_durable_audit_runtime_phase_invalid")
    if revision == 5:
        mounted_row = _mounted_row(connection)
        try:
            _binding_from_snapshot(connection, profile, mounted_row)
        except SQLiteProductionAuthorityError as exc:
            raise _AuditCorrupt("cutover_durable_audit_mount_authority_invalid") from exc
        if mount is None or not _linked_mount_rows_exact(connection, mount):
            raise _AuditCorrupt("cutover_durable_audit_mount_closure_invalid")
    if has_post_install_suffix:
        raise _AuditUnsupported(
            "cutover_durable_audit_post_mount_suffix_unsupported"
        )
    if has_audit_suffix:
        raise _AuditUnsupported(
            "cutover_durable_audit_certificate_suffix_unsupported"
        )

    identity = SQLiteCutoverDurableIdentity(
        cutover_id=stored_install.evidence.cutover_id,
        phase=phase,
        repository_epoch=profile.repository_epoch,
        source_admission_revision=profile.source_admission_revision,
        runtime_generation=profile.runtime_generation,
        code_digest=profile.code_digest,
        authority_state=profile.authority_state.value,
        repository_revision=profile.repository_revision,
        runtime_state_revision=profile.state_revision,
        install_transaction_id=stored_install.evidence.transaction_id,
        install_manifest_sha256=stored_install.evidence.manifest_sha256,
        closure_manifest_sha256=closure.closure_manifest_sha256,
        source_proof_sha256=closure.source_proof_sha256,
        source_admission_sha256=closure.source_admission_sha256,
        coverage_transaction_id=coverage_row[1],
        coverage_manifest_sha256=closure.coverage_manifest_sha256,
        coverage_sha256=closure.coverage_sha256,
        baseline_receipt_root_sha256=closure.baseline_receipt_root_sha256,
        head_sha256=None if head is None else head.head_sha256,
        audit_transaction_id=None if audit is None else audit.transaction_id,
        audit_manifest_sha256=None if audit is None else audit.manifest_sha256,
        activation_transaction_id=(
            None if activation is None else activation.transaction_id
        ),
        activation_manifest_sha256=(
            None if activation is None else activation.manifest_sha256
        ),
        mount_transaction_id=None if mount is None else mount.transaction_id,
        mount_manifest_sha256=None if mount is None else mount.manifest_sha256,
        schema_fingerprint=observation.schema_fingerprint,
        migration_ledger_sha256=observation.migration_ledger_sha256,
        receipt_namespace_count=observation.receipt_namespace_count,
        receipt_namespace_sha256=observation.receipt_namespace_sha256,
    )
    status = (
        SQLiteCutoverDurableAuditStatus.CONFLICT
        if expected is not None and identity != expected
        else SQLiteCutoverDurableAuditStatus.EXACT
    )
    return SQLiteCutoverDurableAuditResult(
        status,
        (
            "cutover_durable_audit_identity_conflict"
            if status is SQLiteCutoverDurableAuditStatus.CONFLICT
            else "cutover_durable_audit_exact"
        ),
        identity=identity,
        budget=budget,
    )


def _result(
    status: SQLiteCutoverDurableAuditStatus,
    code: str,
    *,
    budget: Optional[SQLiteCutoverAuditBudget] = None,
) -> SQLiteCutoverDurableAuditResult:
    return SQLiteCutoverDurableAuditResult(status, code, budget=budget)


def _structural_failure_identity(failure: Exception) -> tuple:
    """Return a detached, non-public identity for repeat-observation only."""

    return (
        type(failure).__module__,
        type(failure).__qualname__,
        getattr(failure, "code", None),
        getattr(failure, "sqlite_errorcode", None),
        str(failure),
    )


class SQLiteCutoverDurableAuditor:
    """Two-snapshot, plan-free reader for the singleton cutover lineage."""

    __slots__ = ("_database",)

    def __init__(self, database: SqliteDatabase) -> None:
        if type(database) is not SqliteDatabase:
            raise TypeError("database must be an exact SqliteDatabase")
        self._database = database

    def _once(
        self,
        expected: Optional[SQLiteCutoverDurableIdentity],
    ) -> _SnapshotOutcome:
        connection = None
        budget = None
        progress = None
        storage_identity = _main_storage_identity(
            self._database,
            readonly=True,
        )
        try:
            connection, _filesystem = self._database._open_readonly_unvalidated()
            progress = _install_progress_budget(connection)
            _require_main_storage_identity(
                self._database,
                storage_identity,
                readonly=True,
            )
            connection.execute("BEGIN")
            marker = _verify_schema_and_marker(self._database, connection)
            budget = _budget(connection)
            try:
                result = _snapshot_audit(
                    self._database,
                    connection,
                    marker=marker,
                    expected=expected,
                    budget=budget,
                )
            except _AuditBudgetExceeded:
                raise
            except _AuditUnsupported as exc:
                result = _result(
                    SQLiteCutoverDurableAuditStatus.UNKNOWN,
                    str(exc) or "cutover_durable_audit_unsupported",
                    budget=budget,
                )
            except _AuditCorrupt as exc:
                result = _result(
                    SQLiteCutoverDurableAuditStatus.CORRUPT,
                    str(exc) or "cutover_durable_audit_corrupt",
                    budget=budget,
                )
            except (
                BusinessCutoverActivationContractError,
                BusinessCutoverActivationIntegrityError,
                BusinessCutoverAssetContractError,
                BusinessCutoverAssetIntegrityError,
                BusinessCutoverContractError,
                BusinessCutoverHeadIntegrityError,
                BusinessCutoverMountContractError,
                BusinessCutoverMountIntegrityError,
                BusinessCutoverStoredManifestError,
                ReceiptNamespaceIntegrityError,
                RepositoryAuditContractError,
                RepositoryAuditIntegrityError,
                SQLiteCutoverInstallClosureError,
                SQLiteCutoverInstallProvenanceError,
                SQLiteProductionAuthorityError,
            ) as exc:
                result = _result(
                    SQLiteCutoverDurableAuditStatus.CORRUPT,
                    "cutover_durable_audit_semantic_corrupt",
                    budget=budget,
                )
            fence = _raw_fence(
                self._database, connection, storage_identity
            )
            _require_main_storage_identity(
                self._database,
                storage_identity,
                readonly=True,
            )
            current = connection
            connection = None
            if not _checked_cleanup(current):
                raise _AuditUnavailable("cutover_durable_audit_cleanup_failed")
            return _SnapshotOutcome(result, fence)
        except _AuditBudgetExceeded as exc:
            if connection is not None:
                current = connection
                connection = None
                if not _checked_cleanup(current):
                    return _SnapshotOutcome(
                        _result(
                            SQLiteCutoverDurableAuditStatus.UNKNOWN,
                            "cutover_durable_audit_cleanup_failed",
                            budget=exc.budget,
                        ),
                        _AuditFence(
                            (), (), (), None, None, None, (), None,
                            None, None, None, None, 0, (),
                        ),
                    )
            return _SnapshotOutcome(
                _result(
                    SQLiteCutoverDurableAuditStatus.UNKNOWN,
                    str(exc),
                    budget=exc.budget,
                ),
                _AuditFence(
                    (), (), (), None, None, None, (), None,
                    None, None, None, None, 0, (),
                ),
            )
        except BaseException as failure:
            progress_failure = None if progress is None else progress.failure
            if connection is not None:
                current = connection
                connection = None
                if progress_failure is not None or not isinstance(failure, Exception):
                    _process_control_cleanup(current)
                elif not _checked_cleanup(current):
                    raise _AuditUnavailable(
                        "cutover_durable_audit_cleanup_failed"
                    ) from failure
            if progress_failure is not None:
                raise progress_failure
            raise

    def _fresh_fence(self) -> _AuditFence:
        connection = None
        progress = None
        storage_identity = _main_storage_identity(
            self._database,
            readonly=True,
        )
        try:
            connection, _filesystem = self._database._open_readonly_unvalidated()
            progress = _install_progress_budget(connection)
            _require_main_storage_identity(
                self._database,
                storage_identity,
                readonly=True,
            )
            connection.execute("BEGIN")
            _verify_schema_and_marker(self._database, connection)
            fence = _raw_fence(self._database, connection, storage_identity)
            _require_main_storage_identity(
                self._database,
                storage_identity,
                readonly=True,
            )
            current = connection
            connection = None
            if not _checked_cleanup(current):
                raise _AuditUnavailable("cutover_durable_audit_fence_cleanup_failed")
            return fence
        except BaseException:
            progress_failure = None if progress is None else progress.failure
            if connection is not None:
                current = connection
                connection = None
                _process_control_cleanup(current)
            if progress_failure is not None:
                raise progress_failure
            raise

    def inspect(
        self,
        *,
        expected: Optional[SQLiteCutoverDurableIdentity] = None,
    ) -> SQLiteCutoverDurableAuditResult:
        if expected is not None and type(expected) is not SQLiteCutoverDurableIdentity:
            raise TypeError("expected must be an exact durable identity")
        try:
            first = self._once(expected)
        except BaseException as exc:
            if not isinstance(exc, Exception):
                raise
            if _failure_is_unavailable(exc) or isinstance(
                exc, (_AuditUnavailable, _AuditUnsupported)
            ):
                return _result(
                    SQLiteCutoverDurableAuditStatus.UNKNOWN,
                    "cutover_durable_audit_unavailable",
                )
            first_failure = _structural_failure_identity(exc)
            try:
                second = self._once(expected)
            except BaseException as second_exc:
                if not isinstance(second_exc, Exception):
                    raise
                if _failure_is_unavailable(second_exc) or isinstance(
                    second_exc, (_AuditUnavailable, _AuditUnsupported)
                ):
                    return _result(
                        SQLiteCutoverDurableAuditStatus.UNKNOWN,
                        "cutover_durable_audit_publish_fence_unavailable",
                    )
                if _structural_failure_identity(second_exc) == first_failure:
                    return _result(
                        SQLiteCutoverDurableAuditStatus.CORRUPT,
                        "cutover_durable_audit_structural_corrupt",
                    )
            else:
                del second
            return _result(
                SQLiteCutoverDurableAuditStatus.UNKNOWN,
                "cutover_durable_audit_corrupt_fence_changed",
            )
        if first.result.status is SQLiteCutoverDurableAuditStatus.UNKNOWN:
            return first.result
        try:
            if first.result.status is SQLiteCutoverDurableAuditStatus.CORRUPT:
                second = self._once(expected)
                if (
                    second.result.status is not SQLiteCutoverDurableAuditStatus.CORRUPT
                    or second.result.code != first.result.code
                    or second.fence != first.fence
                ):
                    return _result(
                        SQLiteCutoverDurableAuditStatus.UNKNOWN,
                        "cutover_durable_audit_corrupt_fence_changed",
                        budget=first.result.budget,
                    )
            elif self._fresh_fence() != first.fence:
                return _result(
                    SQLiteCutoverDurableAuditStatus.UNKNOWN,
                    "cutover_durable_audit_publish_fence_changed",
                    budget=first.result.budget,
                )
        except BaseException as exc:
            if not isinstance(exc, Exception):
                raise
            return _result(
                SQLiteCutoverDurableAuditStatus.UNKNOWN,
                "cutover_durable_audit_publish_fence_unavailable",
                budget=first.result.budget,
            )
        if first.result.status in (
            SQLiteCutoverDurableAuditStatus.EXACT,
            SQLiteCutoverDurableAuditStatus.ABSENT,
        ):
            try:
                self._database._finalize_readonly_storage()
            except BaseException as exc:
                if not isinstance(exc, Exception):
                    raise
                return _result(
                    SQLiteCutoverDurableAuditStatus.UNKNOWN,
                    "cutover_durable_audit_durability_barrier_failed",
                    budget=first.result.budget,
                )
        return first.result


__all__ = ()
