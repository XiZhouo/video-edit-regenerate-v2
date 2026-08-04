"""Private fixed-SQL writer for the V13 asset-provider phase transition.

The writer never accepts a detached cutover plan or a pre-issued phase
binding.  Its only non-SQLite input is a private, same-thread observation of
the external provider and file-maintenance lease.  Inside one
``BEGIN IMMEDIATE`` it rebuilds the exact phase-1 predecessor, V12 install
closure, durable coverage, runtime, receipt and scheduler checkpoints from
raw rows; it then issues the pure provider evidence and V13 binding itself.

The only mutations are one fixed binding INSERT followed by one fixed CAS
UPDATE of ``business_cutover_head``.  The external observation is currently
issuable only by private test/protocol code.  A production issuer must remain
inside the future cutover coordinator's strict catalog-EX -> repository-EX
lease and provider consistency snapshot for the complete call.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hmac
import os
import sqlite3
import threading
from typing import Any, Callable, Optional, Tuple

from business_cutover_contract import (
    BusinessCutoverAdmission,
    digest_business_cutover_admission,
)
from business_cutover_head_integrity import (
    BusinessCutoverHeadIntegrityError,
    BusinessCutoverHeadPointer,
    BusinessCutoverHeadPointerRole,
    BusinessCutoverHeadState,
    _advance_business_cutover_head,
    _issue_initial_business_cutover_head,
)
from business_cutover_phase_binding_contract import (
    BusinessCutoverAdmissionCheckpoint,
    BusinessCutoverAllocationHighWater,
    BusinessCutoverAssetCoverageCheckpoint,
    BusinessCutoverDispatchHighWater,
    BusinessCutoverDurableCheckpoint,
    BusinessCutoverMaintenanceCheckpoint,
    BusinessCutoverPhaseBinding,
    BusinessCutoverPhaseBindingHead,
    BusinessCutoverPhaseBindingIdentity,
    BusinessCutoverPhaseBindingProjection,
    BusinessCutoverPhaseBindingStage,
    BusinessCutoverReceiptPointer,
    BusinessCutoverRuntimeImage,
    BusinessCutoverWorkerHighWater,
)
from business_cutover_phase_binding_integrity import (
    BusinessCutoverPhaseBindingIntegrityError,
    issue_business_cutover_asset_provider_evidence,
    issue_business_cutover_phase_binding,
    verify_business_cutover_phase_binding,
)
from business_cutover_phase_binding_persistability import (
    BusinessCutoverPhaseBindingPersistedRow,
    _business_cutover_phase_binding_layout,
    _project_business_cutover_phase_binding_row,
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
from sqlite_cutover_phase_binding_reader import (
    SQLiteCutoverPhaseBindingPrefix,
    SQLiteCutoverPhaseBindingReaderError,
    _read_cutover_phase_binding_prefix,
    _require_cutover_phase_binding_prefix,
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
from sqlite_schema import _expected_migration_ledger_sha256


_CUTOVER_ASSET_PROVIDER_PHASE_WRITER_CAPABILITY = object()
_FRESH_ASSET_PROVIDER_PHASE_OBSERVATION_ISSUER = object()


class SQLiteCutoverAssetProviderPhaseStatus(str, Enum):
    ABSENT = "absent"
    READY = "ready"
    EXACT = "exact"
    CONFLICT = "conflict"
    CORRUPT = "corrupt"
    UNKNOWN = "unknown"


class SQLiteCutoverAssetProviderPhaseTestPoint(str, Enum):
    AFTER_BINDING_BEFORE_HEAD = "after_binding_before_head"
    BEFORE_COMMIT = "before_commit"
    AFTER_COMMIT_BEFORE_ACK = "after_commit_before_ack"


@dataclass(frozen=True)
class SQLiteCutoverAssetProviderPhaseResult:
    status: SQLiteCutoverAssetProviderPhaseStatus
    code: str
    identity: Optional[SQLiteCutoverDurableIdentity] = None
    recovered_after_ack_loss: bool = False

    def __post_init__(self) -> None:
        if type(self.status) is not SQLiteCutoverAssetProviderPhaseStatus:
            raise TypeError("status must be an exact asset-provider phase status")
        if type(self.code) is not str or not self.code:
            raise TypeError("code must be a non-empty exact string")
        if self.identity is not None and type(self.identity) is not SQLiteCutoverDurableIdentity:
            raise TypeError("identity must be an exact durable identity")
        if self.status is SQLiteCutoverAssetProviderPhaseStatus.READY:
            if (
                self.identity is None
                or self.identity.phase is not SQLiteCutoverDurablePhase.INSTALLED_HEAD
            ):
                raise TypeError("READY requires the exact installed head")
        elif self.status is SQLiteCutoverAssetProviderPhaseStatus.EXACT:
            if (
                self.identity is None
                or self.identity.phase in (
                    SQLiteCutoverDurablePhase.INSTALLED_CLOSURE,
                    SQLiteCutoverDurablePhase.INSTALLED_HEAD,
                )
            ):
                raise TypeError("EXACT requires an asset-covered or later identity")
        elif self.identity is not None:
            raise TypeError("identity is forbidden for an uncertain result")
        if type(self.recovered_after_ack_loss) is not bool or (
            self.recovered_after_ack_loss
            and self.status is not SQLiteCutoverAssetProviderPhaseStatus.EXACT
        ):
            raise TypeError("recovered_after_ack_loss is invalid")


class _FreshAssetProviderPhaseObservation:
    """Private active-lease carrier for facts that SQLite cannot discover.

    The exact-type/issuer/PID/thread/live checks are a trusted-process seam,
    not a cryptographic lease.  The future coordinator must create and consume
    this object without leaving its external lock/provider snapshot contexts.
    """

    __slots__ = (
        "current_admission",
        "source_freeze_sha256",
        "maintenance_scope",
        "maintenance_proof_sha256",
        "maintenance_observed_at",
        "provider_protocol",
        "provider_id",
        "provider_generation",
        "provider_sequence",
        "provider_checked_at",
        "object_count",
        "total_bytes",
        "blob_root_sha256",
        "_pid",
        "_thread_id",
        "_active",
        "_issuer",
    )

    def __init__(
        self,
        *,
        current_admission: BusinessCutoverAdmission,
        source_freeze_sha256: bytes,
        maintenance_scope: str,
        maintenance_proof_sha256: bytes,
        maintenance_observed_at: str,
        provider_protocol: str,
        provider_id: str,
        provider_generation: str,
        provider_sequence: int,
        provider_checked_at: str,
        object_count: int,
        total_bytes: int,
        blob_root_sha256: bytes,
        _issuer: Any,
    ) -> None:
        if _issuer is not _FRESH_ASSET_PROVIDER_PHASE_OBSERVATION_ISSUER:
            raise TypeError("fresh asset-provider phase observation issuer is required")
        if type(current_admission) is not BusinessCutoverAdmission:
            raise TypeError("current_admission must be an exact BusinessCutoverAdmission")
        for name, value in (
            ("source_freeze_sha256", source_freeze_sha256),
            ("maintenance_proof_sha256", maintenance_proof_sha256),
            ("blob_root_sha256", blob_root_sha256),
        ):
            if type(value) is not bytes or len(value) != 32:
                raise TypeError(name + " must be exactly 32 bytes")
        for name, value in (
            ("maintenance_scope", maintenance_scope),
            ("maintenance_observed_at", maintenance_observed_at),
            ("provider_protocol", provider_protocol),
            ("provider_id", provider_id),
            ("provider_generation", provider_generation),
            ("provider_checked_at", provider_checked_at),
        ):
            if type(value) is not str or not value:
                raise TypeError(name + " must be a non-empty exact string")
        for name, value in (
            ("provider_sequence", provider_sequence),
            ("object_count", object_count),
            ("total_bytes", total_bytes),
        ):
            if type(value) is not int or value < 0:
                raise TypeError(name + " must be a non-negative exact integer")
        if provider_sequence < 1:
            raise TypeError("provider_sequence must be positive")
        for name in self.__slots__:
            if name.startswith("_"):
                continue
            object.__setattr__(self, name, locals()[name])
        object.__setattr__(self, "_pid", os.getpid())
        object.__setattr__(self, "_thread_id", threading.get_ident())
        object.__setattr__(self, "_active", True)
        object.__setattr__(self, "_issuer", _issuer)

    def _expire(self, *, _issuer: Any) -> None:
        if _issuer is not _FRESH_ASSET_PROVIDER_PHASE_OBSERVATION_ISSUER:
            raise TypeError("fresh observation issuer is required")
        object.__setattr__(self, "_active", False)

    def __setattr__(self, name: str, value: Any) -> None:
        del name, value
        raise AttributeError("fresh asset-provider phase observation is immutable")


class _AssetProviderPhaseConflict(RuntimeError):
    pass


class _AssetProviderPhaseCorrupt(RuntimeError):
    pass


class _AssetProviderPhaseUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class _AssetProviderPhaseSnapshot:
    binding: BusinessCutoverPhaseBinding
    new_state: BusinessCutoverHeadState
    closure_manifest_sha256: bytes
    source_proof_sha256: bytes
    source_admission_sha256: bytes
    coverage_sha256: bytes
    baseline_receipt_root_sha256: bytes
    schema_fingerprint: str
    migration_ledger_sha256: bytes
    receipt_namespace_count: int
    receipt_namespace_sha256: bytes


def _require_live_observation(value: Any) -> _FreshAssetProviderPhaseObservation:
    if (
        type(value) is not _FreshAssetProviderPhaseObservation
        or getattr(value, "_issuer", None)
        is not _FRESH_ASSET_PROVIDER_PHASE_OBSERVATION_ISSUER
        or getattr(value, "_active", None) is not True
        or getattr(value, "_pid", None) != os.getpid()
        or getattr(value, "_thread_id", None) != threading.get_ident()
    ):
        raise _AssetProviderPhaseConflict(
            "cutover_asset_provider_external_lease_not_live"
        )
    return value


_ASSET_BINDING_LAYOUT = _business_cutover_phase_binding_layout(
    BusinessCutoverPhaseBindingStage.ASSET_PROVIDER
)
_INSERT_BINDING_SQL = (
    "INSERT INTO " + _ASSET_BINDING_LAYOUT.table + "("
    + ",".join(_ASSET_BINDING_LAYOUT.columns) + ") VALUES ("
    + ",".join("?" for _name in _ASSET_BINDING_LAYOUT.columns) + ")"
)
_UPDATE_HEAD_SQL = """
UPDATE business_cutover_head
SET state_revision=?,phase_revision=?,phase=?,authority_state=?,
    coverage_transaction_id=?,coverage_manifest_sha256=?,
    audit_transaction_id=?,audit_manifest_sha256=?,
    activation_transaction_id=?,activation_manifest_sha256=?,
    mount_transaction_id=?,mount_manifest_sha256=?,
    previous_head_sha256=?,head_sha256=?,updated_at=?
WHERE singleton=? AND cutover_id=? AND state_revision=? AND phase_revision=? AND
      phase=? AND authority_state=? AND repository_epoch=? AND
      runtime_generation=? AND code_digest=? AND install_transaction_id=? AND
      install_manifest_sha256=? AND coverage_transaction_id IS ? AND
      coverage_manifest_sha256 IS ? AND audit_transaction_id IS ? AND
      audit_manifest_sha256 IS ? AND activation_transaction_id IS ? AND
      activation_manifest_sha256 IS ? AND mount_transaction_id IS ? AND
      mount_manifest_sha256 IS ? AND previous_head_sha256=? AND
      head_sha256=? AND updated_at=?
"""

_HEAD_COLUMNS = (
    "singleton", "cutover_id", "state_revision", "phase_revision", "phase",
    "authority_state", "repository_epoch", "runtime_generation", "code_digest",
    "install_transaction_id", "install_manifest_sha256",
    "coverage_transaction_id", "coverage_manifest_sha256",
    "audit_transaction_id", "audit_manifest_sha256",
    "activation_transaction_id", "activation_manifest_sha256",
    "mount_transaction_id", "mount_manifest_sha256",
    "previous_head_sha256", "head_sha256", "updated_at",
)
_HEAD_UPDATE_COLUMNS = frozenset((
    "state_revision", "phase_revision", "phase", "authority_state",
    "coverage_transaction_id", "coverage_manifest_sha256",
    "audit_transaction_id", "audit_manifest_sha256",
    "activation_transaction_id", "activation_manifest_sha256",
    "mount_transaction_id", "mount_manifest_sha256",
    "previous_head_sha256", "head_sha256", "updated_at",
))
_FIXED_WRITE_SQL = frozenset((_INSERT_BINDING_SQL, _UPDATE_HEAD_SQL))
_BINDING_TABLES = (
    "business_cutover_asset_provider_bindings",
    "business_cutover_audit_bindings",
    "business_cutover_activation_bindings",
    "business_cutover_mount_bindings",
)

_COVERAGE_COLUMNS = (
    "cutover_id", "transaction_id", "receipt_kind", "committed_at",
    "repository_epoch", "source_admission_revision", "source_admission_sha256",
    "batch_count", "member_root_sha256", "repository_member_count",
    "live_project_count", "deleted_project_count", "observed_occurrence_count",
    "observed_row_count", "observed_manifest_bytes", "project_count",
    "reference_count", "unique_blob_count", "unique_blob_bytes",
    "referenced_bytes", "project_root_sha256", "blob_root_sha256",
    "occurrence_root_sha256", "coverage_sha256", "manifest_bytes",
    "manifest_sha256",
)

_READ_ONLY_PRAGMAS_WITHOUT_ARGUMENT = frozenset((
    "application_id", "busy_timeout", "checkpoint_fullfsync", "encoding",
    "foreign_keys", "fullfsync", "journal_mode", "schema_version",
    "synchronous", "trusted_schema", "user_version",
))
_READ_ONLY_PRAGMAS_WITH_ARGUMENT = frozenset((
    "foreign_key_list", "index_info", "index_list", "index_xinfo",
    "integrity_check", "table_info", "table_xinfo",
))
_FORBIDDEN_ACTIONS = frozenset(
    getattr(sqlite3, name)
    for name in (
        "SQLITE_ALTER_TABLE", "SQLITE_ANALYZE", "SQLITE_ATTACH",
        "SQLITE_CREATE_INDEX", "SQLITE_CREATE_TABLE", "SQLITE_CREATE_TEMP_INDEX",
        "SQLITE_CREATE_TEMP_TABLE", "SQLITE_CREATE_TEMP_TRIGGER",
        "SQLITE_CREATE_TEMP_VIEW", "SQLITE_CREATE_TRIGGER", "SQLITE_CREATE_VIEW",
        "SQLITE_CREATE_VTABLE", "SQLITE_DETACH", "SQLITE_DROP_INDEX",
        "SQLITE_DROP_TABLE", "SQLITE_DROP_TEMP_INDEX", "SQLITE_DROP_TEMP_TABLE",
        "SQLITE_DROP_TEMP_TRIGGER", "SQLITE_DROP_TEMP_VIEW", "SQLITE_DROP_TRIGGER",
        "SQLITE_DROP_VIEW", "SQLITE_DROP_VTABLE", "SQLITE_REINDEX",
        "SQLITE_TRANSACTION", "SQLITE_SAVEPOINT",
    )
    if hasattr(sqlite3, name)
)


class _AssetProviderPhaseAuthorizer:
    __slots__ = ("denied", "_mode")

    def __init__(self) -> None:
        self.denied = False
        self._mode = "closed"

    def open_binding_insert(self) -> None:
        if self._mode != "closed":
            raise RuntimeError("asset-provider mutation authority is already open")
        self._mode = "binding"

    def open_head_update(self) -> None:
        if self._mode != "closed":
            raise RuntimeError("asset-provider mutation authority is already open")
        self._mode = "head"

    def close(self) -> None:
        self._mode = "closed"

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
        pragma = getattr(sqlite3, "SQLITE_PRAGMA", -1)
        mutation = action in (insert, update, delete)
        read_only_pragma = bool(
            action == pragma
            and (
                (table in _READ_ONLY_PRAGMAS_WITHOUT_ARGUMENT and arg2 is None)
                or (table in _READ_ONLY_PRAGMAS_WITH_ARGUMENT and arg2 is not None)
            )
        )
        allowed_insert = bool(
            action == insert
            and self._mode == "binding"
            and table == "business_cutover_asset_provider_bindings"
        )
        allowed_update = bool(
            action == update
            and self._mode == "head"
            and table == "business_cutover_head"
            and column in _HEAD_UPDATE_COLUMNS
        )
        denied = bool(
            action in _FORBIDDEN_ACTIONS
            or (action == pragma and not read_only_pragma)
            or (mutation and database != "main")
            or (mutation and trigger_name is not None)
            or (action == insert and not allowed_insert)
            or (action == update and not allowed_update)
            or action == delete
        )
        if denied:
            self.denied = True
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK


def _same(left: Any, right: Any) -> bool:
    if type(left) is bytes and type(right) is bytes:
        return hmac.compare_digest(left, right)
    return type(left) is type(right) and left == right


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
    """Preserve KeyboardInterrupt/SystemExit across best-effort cleanup."""

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


def _head_state(head: BusinessCutoverPhaseBindingHead, identity: BusinessCutoverPhaseBindingIdentity) -> BusinessCutoverHeadState:
    pointers = []
    for role, pointer in (
        (BusinessCutoverHeadPointerRole.INSTALL, head.install),
        (BusinessCutoverHeadPointerRole.COVERAGE, head.coverage),
        (BusinessCutoverHeadPointerRole.AUDIT, head.audit),
        (BusinessCutoverHeadPointerRole.ACTIVATION, head.activation),
        (BusinessCutoverHeadPointerRole.MOUNT, head.mount),
    ):
        if pointer is not None:
            pointers.append(BusinessCutoverHeadPointer(
                role, pointer.transaction_id, pointer.receipt_kind,
                pointer.committed_at, pointer.manifest_sha256,
            ))
    return BusinessCutoverHeadState(
        cutover_id=identity.cutover_id,
        state_revision=head.state_revision,
        phase_revision=head.phase_revision,
        phase=head.phase,
        authority_state=head.authority_state,
        repository_epoch=identity.repository_epoch,
        runtime_generation=identity.runtime_generation,
        code_digest=identity.code_digest,
        pointers=tuple(pointers),
        previous_head_sha256=head.previous_head_sha256,
        head_sha256=head.head_sha256,
        updated_at=head.updated_at,
    )


def _binding_head(state: BusinessCutoverHeadState) -> BusinessCutoverPhaseBindingHead:
    by_role = {pointer.role: pointer for pointer in state.pointers}

    def pointer(role: BusinessCutoverHeadPointerRole) -> Optional[BusinessCutoverReceiptPointer]:
        value = by_role.get(role)
        return None if value is None else BusinessCutoverReceiptPointer(
            value.transaction_id, value.receipt_kind, value.occurred_at,
            value.manifest_sha256,
        )

    return BusinessCutoverPhaseBindingHead(
        state_revision=state.state_revision,
        phase_revision=state.phase_revision,
        phase=state.phase,
        authority_state=state.authority_state,
        head_sha256=state.head_sha256,
        updated_at=state.updated_at,
        previous_head_sha256=state.previous_head_sha256,
        install=pointer(BusinessCutoverHeadPointerRole.INSTALL),  # type: ignore[arg-type]
        coverage=pointer(BusinessCutoverHeadPointerRole.COVERAGE),
        audit=pointer(BusinessCutoverHeadPointerRole.AUDIT),
        activation=pointer(BusinessCutoverHeadPointerRole.ACTIVATION),
        mount=pointer(BusinessCutoverHeadPointerRole.MOUNT),
    )


def _select_single(connection: Any, sql: str, parameters: tuple, code: str) -> tuple:
    rows = connection.execute(sql, parameters).fetchall()
    if len(rows) != 1:
        raise _AssetProviderPhaseCorrupt(code)
    return tuple(rows[0][index] for index in range(len(rows[0])))


class SQLiteCutoverAssetProviderPhaseWriter:
    """Bind durable coverage to one fresh provider checkpoint and phase-2 head."""

    __slots__ = ("_database",)

    def __init__(self, database: SqliteDatabase, *, _capability: Any) -> None:
        if type(database) is not SqliteDatabase:
            raise TypeError("database must be an exact SqliteDatabase")
        if _capability is not _CUTOVER_ASSET_PROVIDER_PHASE_WRITER_CAPABILITY:
            raise TypeError("cutover asset-provider phase writer capability is required")
        self._database = database

    @staticmethod
    def _mapped_result(result: Any, *, recovered: bool = False) -> SQLiteCutoverAssetProviderPhaseResult:
        if result.status is SQLiteCutoverDurableAuditStatus.EXACT:
            if result.identity.phase is SQLiteCutoverDurablePhase.INSTALLED_HEAD:
                return SQLiteCutoverAssetProviderPhaseResult(
                    SQLiteCutoverAssetProviderPhaseStatus.READY,
                    "cutover_asset_provider_phase_ready", result.identity,
                )
            if result.identity.phase is SQLiteCutoverDurablePhase.INSTALLED_CLOSURE:
                return SQLiteCutoverAssetProviderPhaseResult(
                    SQLiteCutoverAssetProviderPhaseStatus.ABSENT,
                    "cutover_asset_provider_phase_head_not_finalized",
                )
            return SQLiteCutoverAssetProviderPhaseResult(
                SQLiteCutoverAssetProviderPhaseStatus.EXACT,
                "cutover_asset_provider_phase_exact", result.identity, recovered,
            )
        mapped = {
            SQLiteCutoverDurableAuditStatus.ABSENT: SQLiteCutoverAssetProviderPhaseStatus.ABSENT,
            SQLiteCutoverDurableAuditStatus.CONFLICT: SQLiteCutoverAssetProviderPhaseStatus.CONFLICT,
            SQLiteCutoverDurableAuditStatus.CORRUPT: SQLiteCutoverAssetProviderPhaseStatus.CORRUPT,
            SQLiteCutoverDurableAuditStatus.UNKNOWN: SQLiteCutoverAssetProviderPhaseStatus.UNKNOWN,
        }[result.status]
        return SQLiteCutoverAssetProviderPhaseResult(
            mapped, "cutover_asset_provider_phase_" + mapped.value,
        )

    def reconcile(self) -> SQLiteCutoverAssetProviderPhaseResult:
        result = self._mapped_result(SQLiteCutoverDurableAuditor(self._database).inspect())
        if result.status is SQLiteCutoverAssetProviderPhaseStatus.EXACT:
            try:
                self._database._finalize_committed_storage()
            except Exception:
                return SQLiteCutoverAssetProviderPhaseResult(
                    SQLiteCutoverAssetProviderPhaseStatus.UNKNOWN,
                    "cutover_asset_provider_phase_durability_barrier_failed",
                )
        return result

    @staticmethod
    def _checkpoint(connection: Any, profile: Any, receipt: tuple[int, bytes]) -> BusinessCutoverDurableCheckpoint:
        worker = _select_single(
            connection,
            "SELECT state_revision,sequence,tail_sha256,updated_at FROM worker_ledger_head WHERE singleton=1",
            (), "cutover_asset_provider_worker_head_invalid",
        )
        allocation = _select_single(
            connection,
            "SELECT state_revision,sequence,tail_sha256,updated_at FROM outbox_allocation_head WHERE singleton=1",
            (), "cutover_asset_provider_allocation_head_invalid",
        )
        dispatch = _select_single(
            connection,
            "SELECT state_revision,published_sequence,published_repository_revision,published_ordinal,inflight_sequence,inflight_repository_revision,inflight_ordinal,head_sha256,updated_at FROM outbox_dispatch_head WHERE singleton=1",
            (), "cutover_asset_provider_dispatch_head_invalid",
        )
        return BusinessCutoverDurableCheckpoint(
            runtime=BusinessCutoverRuntimeImage(
                profile.authority, profile.authority_state.value,
                profile.source_admission_revision, profile.repository_revision,
                profile.state_revision, profile.schema_version, profile.updated_at,
            ),
            receipt_before_count=receipt[0],
            receipt_before_sha256=receipt[1],
            receipt_after_count=receipt[0],
            receipt_after_sha256=receipt[1],
            worker=BusinessCutoverWorkerHighWater(*worker),
            allocation=BusinessCutoverAllocationHighWater(*allocation),
            dispatch=BusinessCutoverDispatchHighWater(*dispatch),
        )

    def _prove_and_issue(
        self,
        connection: Any,
        observation: _FreshAssetProviderPhaseObservation,
    ) -> _AssetProviderPhaseSnapshot:
        _require_live_observation(observation)
        profile = _validate_business_runtime_connection(
            self._database, connection, access=BusinessRuntimeAccess.READER,
        )
        _read_cutover_verification_budget(connection)
        _verify_legacy_receipt_namespace(connection)
        _verify_production_receipt_namespace(connection)
        receipt = _read_receipt_namespace_identity(connection)
        install = _select_single(
            connection,
            "SELECT i.cutover_id,i.transaction_id,i.receipt_kind,i.committed_at,"
            "i.repository_epoch,i.runtime_generation,i.code_digest,"
            "i.member_mapping_sha256,i.manifest_bytes,i.manifest_sha256,"
            "r.kind,r.committed_at FROM business_cutover_install_receipts AS i "
            "JOIN transaction_receipts AS r ON r.transaction_id=i.transaction_id "
            "ORDER BY i.cutover_id",
            (), "cutover_asset_provider_install_invalid",
        )
        stored = _parse_stored_install_manifest(install[8])
        closure = _require_cutover_install_closure_binding(
            _read_business_cutover_install_closure(
                connection,
                cutover_id=install[0],
                install_transaction_id=install[1],
                install_manifest_sha256=install[9],
                install_committed_at=install[3],
                target_schema_version=stored.target_schema_version,
                target_schema_fingerprint=stored.target_schema_fingerprint,
                require_current_install_snapshot=True,
            )
        )
        if (
            stored.evidence.cutover_id != install[0]
            or stored.evidence.transaction_id != install[1]
            or stored.evidence.receipt_kind != install[2]
            or stored.evidence.runtime.repository_epoch != install[4]
            or stored.evidence.runtime.runtime_generation != install[5]
            or not hmac.compare_digest(
                stored.evidence.runtime.code_digest, install[6]
            )
            or not hmac.compare_digest(
                stored.evidence.member_mapping_sha256, install[7]
            )
            or stored.evidence.manifest_bytes != install[8]
            or not hmac.compare_digest(stored.evidence.manifest_sha256, install[9])
            or install[10] != install[2]
            or install[11] != install[3]
            or
            profile.authority_state is not BusinessAuthorityState.READ_ONLY
            or profile.cutover_id != install[0]
            or profile.repository_epoch != install[4]
            or profile.runtime_generation != install[5]
            or not hmac.compare_digest(profile.code_digest, install[6])
            or profile.source_admission_revision != stored.evidence.runtime.source_admission_revision
            or not hmac.compare_digest(closure.source_admission_sha256, stored.source_admission_sha256)
        ):
            raise _AssetProviderPhaseCorrupt("cutover_asset_provider_runtime_invalid")

        admission = observation.current_admission
        admission_sha256 = digest_business_cutover_admission(admission)
        if (
            admission.authority_state != "cutover_pending"
            or admission.authority_epoch != profile.repository_epoch
            or admission.revision != profile.source_admission_revision + 1
            or admission.cutover_id != install[0]
            or not hmac.compare_digest(admission_sha256, stored.pending_admission_sha256)
            or not hmac.compare_digest(observation.source_freeze_sha256, closure.source_proof_sha256)
            or observation.maintenance_scope != "catalog_ex_repository_ex"
            or not hmac.compare_digest(
                observation.maintenance_proof_sha256, closure.source_proof_sha256
            )
        ):
            raise _AssetProviderPhaseConflict("cutover_asset_provider_external_fence_mismatch")

        raw_coverage = _select_single(
            connection,
            "SELECT " + ",".join(_COVERAGE_COLUMNS)
            + " FROM business_asset_coverage_receipts WHERE cutover_id=?",
            (install[0],), "cutover_asset_provider_coverage_invalid",
        )
        coverage = dict(zip(_COVERAGE_COLUMNS, raw_coverage))
        if (
            coverage["repository_epoch"] != profile.repository_epoch
            or coverage["source_admission_revision"] != profile.source_admission_revision
            or not hmac.compare_digest(
                coverage["source_admission_sha256"], closure.source_admission_sha256
            )
            or not hmac.compare_digest(
                coverage["manifest_sha256"], closure.coverage_manifest_sha256
            )
            or not hmac.compare_digest(
                coverage["coverage_sha256"], closure.coverage_sha256
            )
        ):
            raise _AssetProviderPhaseCorrupt("cutover_asset_provider_coverage_invalid")
        coverage_checkpoint = BusinessCutoverAssetCoverageCheckpoint(
            pointer=BusinessCutoverReceiptPointer(
                coverage["transaction_id"], coverage["receipt_kind"],
                coverage["committed_at"], coverage["manifest_sha256"],
            ),
            coverage_sha256=coverage["coverage_sha256"],
            member_root_sha256=coverage["member_root_sha256"],
            blob_root_sha256=coverage["blob_root_sha256"],
            batch_count=coverage["batch_count"],
            repository_member_count=coverage["repository_member_count"],
            live_project_count=coverage["live_project_count"],
            deleted_project_count=coverage["deleted_project_count"],
            project_count=coverage["project_count"],
            reference_count=coverage["reference_count"],
            unique_blob_count=coverage["unique_blob_count"],
            unique_blob_bytes=coverage["unique_blob_bytes"],
            referenced_bytes=coverage["referenced_bytes"],
            observed_occurrence_count=coverage["observed_occurrence_count"],
            observed_row_count=coverage["observed_row_count"],
            observed_manifest_bytes=coverage["observed_manifest_bytes"],
        )
        provider = issue_business_cutover_asset_provider_evidence(
            coverage=coverage_checkpoint,
            provider_protocol=observation.provider_protocol,
            provider_id=observation.provider_id,
            provider_generation=observation.provider_generation,
            provider_sequence=observation.provider_sequence,
            checked_at=observation.provider_checked_at,
            object_count=observation.object_count,
            total_bytes=observation.total_bytes,
            blob_root_sha256=observation.blob_root_sha256,
        )
        identity = BusinessCutoverPhaseBindingIdentity(
            install[0], profile.repository_epoch,
            profile.runtime_generation, profile.code_digest,
        )
        old_state = _issue_initial_business_cutover_head(
            cutover_id=identity.cutover_id,
            repository_epoch=identity.repository_epoch,
            runtime_generation=identity.runtime_generation,
            code_digest=identity.code_digest,
            install=BusinessCutoverHeadPointer(
                BusinessCutoverHeadPointerRole.INSTALL,
                install[1], install[2], install[3], install[9],
            ),
        )
        raw_head = _select_single(
            connection,
            "SELECT " + ",".join(_HEAD_COLUMNS)
            + " FROM business_cutover_head ORDER BY singleton",
            (), "cutover_asset_provider_phase_one_head_invalid",
        )
        expected_head = old_state.row_values()
        if not all(
            _same(raw_head[index], expected_head[column])
            for index, column in enumerate(_HEAD_COLUMNS)
        ):
            raise _AssetProviderPhaseConflict(
                "cutover_asset_provider_phase_changed"
            )
        for table in _BINDING_TABLES:
            count = _select_single(
                connection,
                "SELECT count(*) FROM " + table,
                (), "cutover_asset_provider_binding_prefix_invalid",
            )[0]
            if type(count) is not int or count != 0:
                raise _AssetProviderPhaseConflict(
                    "cutover_asset_provider_phase_changed"
                )
        new_state = _advance_business_cutover_head(
            old_state,
            cutover_id=identity.cutover_id,
            repository_epoch=identity.repository_epoch,
            runtime_generation=identity.runtime_generation,
            code_digest=identity.code_digest,
            pointer=BusinessCutoverHeadPointer(
                BusinessCutoverHeadPointerRole.COVERAGE,
                coverage["transaction_id"], coverage["receipt_kind"],
                coverage["committed_at"], coverage["manifest_sha256"],
            ),
        )

        now = _select_single(
            connection, "SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')", (),
            "cutover_asset_provider_clock_invalid",
        )[0]
        if (
            type(now) is not str
            or observation.provider_checked_at > now
            or observation.maintenance_observed_at > now
        ):
            raise _AssetProviderPhaseCorrupt("cutover_asset_provider_external_time_invalid")

        projection = BusinessCutoverPhaseBindingProjection(
            stage=BusinessCutoverPhaseBindingStage.ASSET_PROVIDER,
            identity=identity,
            old_head=_binding_head(old_state),
            new_head=_binding_head(new_state),
            admission=BusinessCutoverAdmissionCheckpoint(
                source_revision=profile.source_admission_revision,
                source_admission_sha256=closure.source_admission_sha256,
                source_freeze_sha256=observation.source_freeze_sha256,
                file_authority_state=admission.authority_state,
                file_authority_epoch=admission.authority_epoch,
                file_revision=admission.revision,
                file_cutover_id=admission.cutover_id,
                file_admission_sha256=admission_sha256,
            ),
            maintenance=BusinessCutoverMaintenanceCheckpoint(
                observation.maintenance_scope,
                observation.maintenance_proof_sha256,
                observation.maintenance_observed_at,
            ),
            checkpoint=self._checkpoint(connection, profile, receipt),
            direct_evidence=provider,
        )
        issued = verify_business_cutover_phase_binding(
            issue_business_cutover_phase_binding(projection)
        )
        return _AssetProviderPhaseSnapshot(
            binding=issued,
            new_state=new_state,
            closure_manifest_sha256=closure.closure_manifest_sha256,
            source_proof_sha256=closure.source_proof_sha256,
            source_admission_sha256=closure.source_admission_sha256,
            coverage_sha256=closure.coverage_sha256,
            baseline_receipt_root_sha256=closure.baseline_receipt_root_sha256,
            schema_fingerprint=_expected_schema_fingerprint(),
            migration_ledger_sha256=_expected_migration_ledger_sha256(),
            receipt_namespace_count=receipt[0],
            receipt_namespace_sha256=receipt[1],
        )

    @staticmethod
    def _matches_detached(
        snapshot: _AssetProviderPhaseSnapshot,
        identity: SQLiteCutoverDurableIdentity,
    ) -> bool:
        projection = snapshot.binding.projection
        old_head = projection.old_head
        runtime = projection.checkpoint.runtime
        install = old_head.install
        coverage = projection.direct_evidence.coverage.pointer
        return bool(
            identity.phase is SQLiteCutoverDurablePhase.INSTALLED_HEAD
            and identity.cutover_id == projection.identity.cutover_id
            and identity.repository_epoch == projection.identity.repository_epoch
            and identity.source_admission_revision
            == runtime.source_admission_revision
            and identity.runtime_generation == projection.identity.runtime_generation
            and hmac.compare_digest(identity.code_digest, projection.identity.code_digest)
            and identity.authority_state == runtime.authority_state == "read_only"
            and identity.repository_revision == runtime.repository_revision
            and identity.runtime_state_revision == runtime.state_revision
            and identity.install_transaction_id == install.transaction_id
            and hmac.compare_digest(
                identity.install_manifest_sha256, install.manifest_sha256
            )
            and hmac.compare_digest(
                identity.closure_manifest_sha256,
                snapshot.closure_manifest_sha256,
            )
            and hmac.compare_digest(
                identity.source_proof_sha256, snapshot.source_proof_sha256
            )
            and hmac.compare_digest(
                identity.source_admission_sha256,
                snapshot.source_admission_sha256,
            )
            and identity.coverage_transaction_id == coverage.transaction_id
            and hmac.compare_digest(
                identity.coverage_manifest_sha256, coverage.manifest_sha256
            )
            and hmac.compare_digest(
                identity.coverage_sha256, snapshot.coverage_sha256
            )
            and hmac.compare_digest(
                identity.baseline_receipt_root_sha256,
                snapshot.baseline_receipt_root_sha256,
            )
            and identity.head_sha256 is not None
            and hmac.compare_digest(identity.head_sha256, old_head.head_sha256)
            and identity.audit_transaction_id is None
            and identity.audit_manifest_sha256 is None
            and identity.activation_transaction_id is None
            and identity.activation_manifest_sha256 is None
            and identity.mount_transaction_id is None
            and identity.mount_manifest_sha256 is None
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
    def _insert_binding(
        connection: Any,
        authorizer: _AssetProviderPhaseAuthorizer,
        persisted: BusinessCutoverPhaseBindingPersistedRow,
    ) -> None:
        if (
            type(persisted) is not BusinessCutoverPhaseBindingPersistedRow
            or persisted.table != "business_cutover_asset_provider_bindings"
        ):
            raise _AssetProviderPhaseCorrupt("cutover_asset_provider_persisted_row_invalid")
        if (
            persisted.columns != _ASSET_BINDING_LAYOUT.columns
            or _INSERT_BINDING_SQL not in _FIXED_WRITE_SQL
        ):
            raise _AssetProviderPhaseCorrupt(
                "cutover_asset_provider_fixed_sql_invalid"
            )
        authorizer.open_binding_insert()
        try:
            cursor = connection.execute(_INSERT_BINDING_SQL, persisted.values)
            try:
                if cursor.rowcount != 1:
                    raise _AssetProviderPhaseCorrupt("cutover_asset_provider_binding_insert_failed")
            finally:
                cursor.close()
        finally:
            authorizer.close()

    @staticmethod
    def _update_head(
        connection: Any,
        authorizer: _AssetProviderPhaseAuthorizer,
        old_state: BusinessCutoverHeadState,
        new_state: BusinessCutoverHeadState,
    ) -> None:
        old = old_state.row_values()
        new = new_state.row_values()
        values = tuple(new[name] for name in _HEAD_UPDATE_COLUMNS_ORDER) + tuple(
            old[name] for name in _HEAD_COLUMNS
        )
        authorizer.open_head_update()
        try:
            if _UPDATE_HEAD_SQL not in _FIXED_WRITE_SQL:
                raise _AssetProviderPhaseCorrupt(
                    "cutover_asset_provider_fixed_sql_invalid"
                )
            cursor = connection.execute(_UPDATE_HEAD_SQL, values)
            try:
                if cursor.rowcount != 1:
                    raise _AssetProviderPhaseConflict("cutover_asset_provider_head_cas_failed")
            finally:
                cursor.close()
        finally:
            authorizer.close()

    def _existing_matches(
        self,
        observation: _FreshAssetProviderPhaseObservation,
        *,
        expected_identity: SQLiteCutoverDurableIdentity,
    ) -> bool:
        if type(expected_identity) is not SQLiteCutoverDurableIdentity:
            raise TypeError("expected_identity must be an exact durable identity")
        connection = None
        try:
            _require_live_observation(observation)
            connection, _filesystem = self._database._open_unvalidated()
            connection.execute("BEGIN")
            prefix = _require_cutover_phase_binding_prefix(
                _read_cutover_phase_binding_prefix(connection)
            )
            if (
                not prefix.bindings
                or not self._prefix_matches_durable_identity(
                    prefix, expected_identity
                )
            ):
                return False
            projection = prefix.bindings[0].projection
            evidence = projection.direct_evidence
            admission = projection.admission
            maintenance = projection.maintenance
            current = observation.current_admission
            current_sha256 = digest_business_cutover_admission(current)
            return bool(
                evidence.provider_protocol == observation.provider_protocol
                and evidence.provider_id == observation.provider_id
                and evidence.provider_generation == observation.provider_generation
                and evidence.provider_sequence == observation.provider_sequence
                and evidence.checked_at == observation.provider_checked_at
                and evidence.object_count == observation.object_count
                and evidence.total_bytes == observation.total_bytes
                and hmac.compare_digest(
                    evidence.blob_root_sha256, observation.blob_root_sha256
                )
                and current.authority_state == "cutover_pending"
                and current.authority_epoch == projection.identity.repository_epoch
                and current.revision == admission.source_revision + 1
                and current.cutover_id == projection.identity.cutover_id
                and admission.file_authority_state == current.authority_state
                and admission.file_authority_epoch == current.authority_epoch
                and admission.file_revision == current.revision
                and admission.file_cutover_id == current.cutover_id
                and hmac.compare_digest(
                    admission.file_admission_sha256, current_sha256
                )
                and hmac.compare_digest(
                    admission.source_freeze_sha256,
                    observation.source_freeze_sha256,
                )
                and maintenance.scope == observation.maintenance_scope
                and hmac.compare_digest(
                    maintenance.proof_sha256,
                    observation.maintenance_proof_sha256,
                )
                and maintenance.observed_at
                == observation.maintenance_observed_at
            )
        except BaseException as failure:
            if connection is not None:
                if not isinstance(failure, Exception):
                    _process_control_cleanup(connection, commit_started=False)
                    connection = None
                    raise
                cleanup_complete = _rollback_close(connection)
                connection = None
                if not cleanup_complete:
                    raise _AssetProviderPhaseUnavailable(
                        "cutover_asset_provider_cleanup_failed"
                    ) from failure
            raise
        finally:
            if connection is not None:
                if not _rollback_close(connection):
                    raise _AssetProviderPhaseUnavailable(
                        "cutover_asset_provider_cleanup_failed"
                    )

    def _inspect_existing(
        self,
        observation: _FreshAssetProviderPhaseObservation,
        *,
        expected_identity: SQLiteCutoverDurableIdentity,
    ) -> tuple[bool, Optional[SQLiteCutoverAssetProviderPhaseResult]]:
        try:
            return self._existing_matches(
                observation,
                expected_identity=expected_identity,
            ), None
        except BaseException as failure:
            if not isinstance(failure, Exception):
                raise
            if isinstance(failure, _AssetProviderPhaseUnavailable) or (
                _failure_is_unavailable(failure)
            ):
                return False, SQLiteCutoverAssetProviderPhaseResult(
                    SQLiteCutoverAssetProviderPhaseStatus.UNKNOWN,
                    "cutover_asset_provider_storage_unavailable",
                )
            if isinstance(
                failure,
                (
                    _AssetProviderPhaseCorrupt,
                    _CutoverVerificationCorrupt,
                    BusinessCutoverHeadIntegrityError,
                    BusinessCutoverPhaseBindingIntegrityError,
                    BusinessCutoverStoredManifestError,
                    SQLiteCutoverInstallClosureError,
                    SQLiteCutoverPhaseBindingReaderError,
                    SQLiteKernelError,
                    sqlite3.IntegrityError,
                    sqlite3.DatabaseError,
                    TypeError,
                    ValueError,
                ),
            ):
                return False, SQLiteCutoverAssetProviderPhaseResult(
                    SQLiteCutoverAssetProviderPhaseStatus.CORRUPT,
                    str(failure) or "cutover_asset_provider_phase_corrupt",
                )
            raise

    @staticmethod
    def _prefix_matches_durable_identity(
        prefix: SQLiteCutoverPhaseBindingPrefix,
        identity: SQLiteCutoverDurableIdentity,
    ) -> bool:
        if (
            type(prefix) is not SQLiteCutoverPhaseBindingPrefix
            or type(identity) is not SQLiteCutoverDurableIdentity
            or not prefix.bindings
        ):
            return False
        binding_identity = prefix.bindings[-1].projection.identity
        head = prefix.current_head

        def pointer_matches(
            pointer: Optional[BusinessCutoverReceiptPointer],
            transaction_id: Optional[str],
            manifest_sha256: Optional[bytes],
        ) -> bool:
            if pointer is None:
                return transaction_id is None and manifest_sha256 is None
            return bool(
                transaction_id == pointer.transaction_id
                and type(manifest_sha256) is bytes
                and hmac.compare_digest(
                    manifest_sha256, pointer.manifest_sha256
                )
            )

        return bool(
            prefix.cutover_id == identity.cutover_id
            and binding_identity.cutover_id == identity.cutover_id
            and binding_identity.repository_epoch == identity.repository_epoch
            and binding_identity.runtime_generation == identity.runtime_generation
            and hmac.compare_digest(
                binding_identity.code_digest, identity.code_digest
            )
            and identity.phase.value == head.phase
            and identity.authority_state == head.authority_state
            and identity.head_sha256 is not None
            and hmac.compare_digest(identity.head_sha256, head.head_sha256)
            and pointer_matches(
                head.install,
                identity.install_transaction_id,
                identity.install_manifest_sha256,
            )
            and pointer_matches(
                head.coverage,
                identity.coverage_transaction_id,
                identity.coverage_manifest_sha256,
            )
            and pointer_matches(
                head.audit,
                identity.audit_transaction_id,
                identity.audit_manifest_sha256,
            )
            and pointer_matches(
                head.activation,
                identity.activation_transaction_id,
                identity.activation_manifest_sha256,
            )
            and pointer_matches(
                head.mount,
                identity.mount_transaction_id,
                identity.mount_manifest_sha256,
            )
        )

    def _recover_matching_existing(
        self,
        observation: _FreshAssetProviderPhaseObservation,
        *,
        expected_identity: SQLiteCutoverDurableIdentity,
        recovered: bool,
        code: Optional[str] = None,
    ) -> SQLiteCutoverAssetProviderPhaseResult:
        if type(expected_identity) is not SQLiteCutoverDurableIdentity:
            raise TypeError("expected_identity must be an exact durable identity")
        if type(recovered) is not bool:
            raise TypeError("recovered must be an exact bool")
        final = self.reconcile()
        if final.status is not SQLiteCutoverAssetProviderPhaseStatus.EXACT:
            return final
        if final.identity is None or not self._same_stable_lineage(
            expected_identity, final.identity
        ):
            return SQLiteCutoverAssetProviderPhaseResult(
                SQLiteCutoverAssetProviderPhaseStatus.CONFLICT,
                "cutover_asset_provider_final_identity_changed",
            )
        existing_matches, inspection_failure = self._inspect_existing(
            observation,
            expected_identity=final.identity,
        )
        if inspection_failure is not None:
            return inspection_failure
        if not existing_matches:
            return SQLiteCutoverAssetProviderPhaseResult(
                SQLiteCutoverAssetProviderPhaseStatus.CONFLICT,
                "cutover_asset_provider_checkpoint_conflict",
            )
        try:
            _require_live_observation(observation)
        except _AssetProviderPhaseConflict as failure:
            return SQLiteCutoverAssetProviderPhaseResult(
                SQLiteCutoverAssetProviderPhaseStatus.CONFLICT,
                str(failure),
            )
        return SQLiteCutoverAssetProviderPhaseResult(
            final.status,
            final.code if code is None else code,
            final.identity,
            recovered,
        )

    @staticmethod
    def _same_stable_lineage(
        expected: SQLiteCutoverDurableIdentity,
        actual: SQLiteCutoverDurableIdentity,
    ) -> bool:
        phase_order = (
            SQLiteCutoverDurablePhase.INSTALLED_CLOSURE,
            SQLiteCutoverDurablePhase.INSTALLED_HEAD,
            SQLiteCutoverDurablePhase.ASSETS_COVERED,
            SQLiteCutoverDurablePhase.AUDITED,
            SQLiteCutoverDurablePhase.ACTIVATED,
            SQLiteCutoverDurablePhase.MOUNTED,
        )
        return bool(
            type(expected) is SQLiteCutoverDurableIdentity
            and type(actual) is SQLiteCutoverDurableIdentity
            and phase_order.index(actual.phase) >= phase_order.index(expected.phase)
            and actual.cutover_id == expected.cutover_id
            and actual.repository_epoch == expected.repository_epoch
            and actual.source_admission_revision
            == expected.source_admission_revision
            and actual.runtime_generation == expected.runtime_generation
            and hmac.compare_digest(actual.code_digest, expected.code_digest)
            and actual.install_transaction_id == expected.install_transaction_id
            and hmac.compare_digest(
                actual.install_manifest_sha256,
                expected.install_manifest_sha256,
            )
            and hmac.compare_digest(
                actual.closure_manifest_sha256,
                expected.closure_manifest_sha256,
            )
            and hmac.compare_digest(
                actual.source_proof_sha256,
                expected.source_proof_sha256,
            )
            and hmac.compare_digest(
                actual.source_admission_sha256,
                expected.source_admission_sha256,
            )
            and actual.coverage_transaction_id
            == expected.coverage_transaction_id
            and hmac.compare_digest(
                actual.coverage_manifest_sha256,
                expected.coverage_manifest_sha256,
            )
            and hmac.compare_digest(
                actual.coverage_sha256,
                expected.coverage_sha256,
            )
            and hmac.compare_digest(
                actual.baseline_receipt_root_sha256,
                expected.baseline_receipt_root_sha256,
            )
            and actual.schema_fingerprint == expected.schema_fingerprint
            and hmac.compare_digest(
                actual.migration_ledger_sha256,
                expected.migration_ledger_sha256,
            )
        )

    def advance(
        self,
        observation: _FreshAssetProviderPhaseObservation,
        *,
        _test_hook: Optional[
            Callable[[SQLiteCutoverAssetProviderPhaseTestPoint], None]
        ] = None,
    ) -> SQLiteCutoverAssetProviderPhaseResult:
        if _test_hook is not None and not callable(_test_hook):
            raise TypeError("_test_hook must be callable")
        try:
            _require_live_observation(observation)
        except _AssetProviderPhaseConflict as failure:
            return SQLiteCutoverAssetProviderPhaseResult(
                SQLiteCutoverAssetProviderPhaseStatus.CONFLICT, str(failure)
            )
        initial = self.reconcile()
        if initial.status is SQLiteCutoverAssetProviderPhaseStatus.EXACT:
            return self._recover_matching_existing(
                observation,
                expected_identity=initial.identity,
                recovered=True,
            )
        if initial.status is not SQLiteCutoverAssetProviderPhaseStatus.READY:
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
            authorizer = _AssetProviderPhaseAuthorizer()
            connection.set_authorizer(authorizer)
            snapshot = self._prove_and_issue(
                connection, observation,
            )
            if initial.identity is None or not self._matches_detached(
                snapshot, initial.identity
            ):
                raise _AssetProviderPhaseConflict(
                    "cutover_asset_provider_ready_identity_changed"
                )
            binding = snapshot.binding
            new_state = snapshot.new_state
            old_state = _head_state(
                binding.projection.old_head, binding.projection.identity
            )
            _require_live_observation(observation)
            persisted = _project_business_cutover_phase_binding_row(binding)
            self._insert_binding(connection, authorizer, persisted)
            if _test_hook is not None:
                try:
                    _test_hook(
                        SQLiteCutoverAssetProviderPhaseTestPoint.AFTER_BINDING_BEFORE_HEAD
                    )
                except Exception:
                    hook_failed_before_commit = True
                    raise
            self._update_head(connection, authorizer, old_state, new_state)
            if _test_hook is not None:
                try:
                    _test_hook(SQLiteCutoverAssetProviderPhaseTestPoint.BEFORE_COMMIT)
                except Exception:
                    hook_failed_before_commit = True
                    raise
            _require_live_observation(observation)
            prefix = _require_cutover_phase_binding_prefix(
                _read_cutover_phase_binding_prefix(connection)
            )
            if (
                authorizer.denied
                or prefix.current_head.phase_revision != 2
                or prefix.bindings != (binding,)
            ):
                raise _AssetProviderPhaseCorrupt(
                    "cutover_asset_provider_final_readback_invalid"
                )
            _require_live_observation(observation)
            _reset_authorizer(connection)
            authorizer = None
            commit_started = True
            connection.commit()
            if _test_hook is not None:
                _test_hook(
                    SQLiteCutoverAssetProviderPhaseTestPoint.AFTER_COMMIT_BEFORE_ACK
                )
            connection.close()
            connection = None
            reconciled = self.reconcile()
            if reconciled.status is SQLiteCutoverAssetProviderPhaseStatus.EXACT:
                return self._recover_matching_existing(
                    observation,
                    expected_identity=initial.identity,
                    recovered=False,
                    code="cutover_asset_provider_phase_committed",
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
                    _process_control_cleanup(
                        connection, commit_started=commit_started
                    )
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
                if reconciled.status is SQLiteCutoverAssetProviderPhaseStatus.EXACT:
                    return self._recover_matching_existing(
                        observation,
                        expected_identity=initial.identity,
                        recovered=True,
                        code="cutover_asset_provider_phase_recovered",
                    )
                if not cleanup_complete:
                    return SQLiteCutoverAssetProviderPhaseResult(
                        SQLiteCutoverAssetProviderPhaseStatus.UNKNOWN,
                        "cutover_asset_provider_cleanup_failed",
                    )
                return reconciled
            if hook_failed_before_commit:
                reconciled = self.reconcile()
                if reconciled.status is SQLiteCutoverAssetProviderPhaseStatus.READY:
                    return SQLiteCutoverAssetProviderPhaseResult(
                        reconciled.status,
                        "cutover_asset_provider_phase_rolled_back",
                        reconciled.identity,
                    )
                if reconciled.status is SQLiteCutoverAssetProviderPhaseStatus.EXACT:
                    return self._recover_matching_existing(
                        observation,
                        expected_identity=initial.identity,
                        recovered=True,
                        code="cutover_asset_provider_phase_race_recovered",
                    )
                return reconciled
            if isinstance(failure, _AssetProviderPhaseConflict):
                reconciled = self.reconcile()
                if reconciled.status is not SQLiteCutoverAssetProviderPhaseStatus.EXACT:
                    if reconciled.status in (
                        SQLiteCutoverAssetProviderPhaseStatus.CORRUPT,
                        SQLiteCutoverAssetProviderPhaseStatus.UNKNOWN,
                    ):
                        return reconciled
                    return SQLiteCutoverAssetProviderPhaseResult(
                        SQLiteCutoverAssetProviderPhaseStatus.CONFLICT,
                        str(failure),
                    )
                return self._recover_matching_existing(
                    observation,
                    expected_identity=initial.identity,
                    recovered=True,
                    code="cutover_asset_provider_phase_race_recovered",
                )
            if authorizer is not None and authorizer.denied:
                return SQLiteCutoverAssetProviderPhaseResult(
                    SQLiteCutoverAssetProviderPhaseStatus.CORRUPT,
                    "cutover_asset_provider_authorizer_denied",
                )
            if isinstance(failure, _CutoverVerificationBudgetExceeded):
                return SQLiteCutoverAssetProviderPhaseResult(
                    SQLiteCutoverAssetProviderPhaseStatus.UNKNOWN, str(failure)
                )
            if _failure_is_unavailable(failure):
                return SQLiteCutoverAssetProviderPhaseResult(
                    SQLiteCutoverAssetProviderPhaseStatus.UNKNOWN,
                    "cutover_asset_provider_storage_unavailable",
                )
            if isinstance(
                failure,
                (
                    _AssetProviderPhaseCorrupt,
                    _CutoverVerificationCorrupt,
                    BusinessCutoverHeadIntegrityError,
                    BusinessCutoverPhaseBindingIntegrityError,
                    BusinessCutoverStoredManifestError,
                    SQLiteCutoverInstallClosureError,
                    SQLiteCutoverPhaseBindingReaderError,
                    SQLiteKernelError,
                    sqlite3.IntegrityError,
                    sqlite3.DatabaseError,
                    TypeError,
                    ValueError,
                ),
            ):
                return SQLiteCutoverAssetProviderPhaseResult(
                    SQLiteCutoverAssetProviderPhaseStatus.CORRUPT,
                    str(failure) or "cutover_asset_provider_phase_corrupt",
                )
            raise


_HEAD_UPDATE_COLUMNS_ORDER = (
    "state_revision", "phase_revision", "phase", "authority_state",
    "coverage_transaction_id", "coverage_manifest_sha256",
    "audit_transaction_id", "audit_manifest_sha256",
    "activation_transaction_id", "activation_manifest_sha256",
    "mount_transaction_id", "mount_manifest_sha256",
    "previous_head_sha256", "head_sha256", "updated_at",
)


__all__ = ()
