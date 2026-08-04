"""Private exact coordinator for one non-authoritative v5 repository proof.

This module is intentionally unmounted: production callers are zero until the
later authority-cutover bootstrap supplies a trusted runtime-generation and
code-digest issuer.  Its sole entry holds the file repository's global EX -> EX
barrier from S0 through the v4 terminal shadow/catalog proof, the v5 SQLite
proof and S2.  It never changes repository admission and can only return
``authority=legacy`` / ``cutover_ready=False`` historical evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Tuple

from . import _paths  # noqa: F401  (install flat core imports first)
from ._legacy_import_sqlite_adapter import (
    LegacyCatalogSqliteAdapterError,
    LegacySqliteShadowAdapterError,
    _LEGACY_CATALOG_SQLITE_ADAPTER_CAPABILITY,
    _StrictLegacyCatalogEvidence,
    _StrictLegacyCatalogProjection,
    _project_strict_legacy_catalog_sqlite_shadow_locked,
    _seal_strict_legacy_catalog_sqlite_shadow_locked,
)
from .project_topology import (
    ProjectHandle,
    ProjectTopologyCoordinator,
    _REPOSITORY_ADMISSION_CAPABILITY,
    _RepositoryStateDescriptor,
    _StrictRepositorySnapshot,
)

from legacy_repository_integrity import (  # noqa: E402
    LegacyRepositoryIntegrityMember,
)
from sqlite_kernel import KernelReceipt, SqliteDatabase  # noqa: E402
from sqlite_legacy_repository import (  # noqa: E402
    LegacyRepositoryNamespaceStatus,
    LegacyRepositoryReconcileStatus,
    SqliteLegacyRepositorySealer,
    _LEGACY_REPOSITORY_SEAL_CAPABILITY,
)
from sqlite_legacy_repository_exact import (  # noqa: E402
    RepositoryExactProjectionError,
    _issue_repository_projection,
)


_LEGACY_REPOSITORY_SQLITE_ADAPTER_CAPABILITY = object()


class LegacyRepositorySqliteAdapterError(RuntimeError):
    """Stable, payload-free failure after exact target classification."""

    __slots__ = ("code", "status", "detail")

    def __init__(self, code: str, status: str, detail: str) -> None:
        if type(code) is not str or not code:
            raise TypeError("code must be a non-empty exact string")
        if type(status) is not str or not status:
            raise TypeError("status must be a non-empty exact string")
        if type(detail) is not str or not detail:
            raise TypeError("detail must be a non-empty exact string")
        self.code = code
        self.status = status
        self.detail = detail
        super().__init__(code)


@dataclass(frozen=True)
class LegacyRepositoryShadowSealResult:
    """Complete historical proof; never a writer-fence or cutover token."""

    cutover_id: str
    transaction_id: str
    catalog_id: str
    repository_epoch: str
    repository_revision: int
    project_count: int
    live_project_count: int
    deleted_project_count: int
    receipt: KernelReceipt
    already_sealed: bool
    recovered_after_ack_loss: bool
    authority: str = field(init=False, default="legacy")
    production_callers: int = field(init=False, default=0)

    @property
    def cutover_ready(self) -> bool:
        return False

    @property
    def mounted(self) -> bool:
        return False


@dataclass(frozen=True)
class _RepositorySealOutcome:
    receipt: KernelReceipt
    already_sealed: bool
    recovered_after_ack_loss: bool


def _failure(status: str, detail: str) -> LegacyRepositorySqliteAdapterError:
    return LegacyRepositorySqliteAdapterError(
        "legacy_repository_sqlite_shadow_" + status,
        status,
        detail,
    )


def _strict_live_handles(
    topology: ProjectTopologyCoordinator,
    snapshot: _StrictRepositorySnapshot,
) -> Tuple[ProjectHandle, ...]:
    """Create short-lived scan selectors; no path authority is returned."""

    if type(snapshot) is not _StrictRepositorySnapshot:
        raise TypeError("snapshot must be an exact strict repository snapshot")
    handles = tuple(
        ProjectHandle(
            descriptor.project_id,
            topology.path_for(descriptor.project_id),
            descriptor.generation,
        )
        for descriptor in snapshot.states
        if descriptor.status == "live"
    )
    return handles


def _repository_members(
    snapshot: _StrictRepositorySnapshot,
    catalog: Any,
) -> Tuple[LegacyRepositoryIntegrityMember, ...]:
    """Bind every terminal topology state to the exact current v4 catalog."""

    if type(snapshot) is not _StrictRepositorySnapshot:
        raise TypeError("snapshot must be an exact strict repository snapshot")
    if type(catalog) not in (
        _StrictLegacyCatalogEvidence,
        _StrictLegacyCatalogProjection,
    ):
        raise TypeError("catalog must be exact strict catalog evidence")
    catalog_by_id = {
        project.project_id: (ordinal, project)
        for ordinal, project in enumerate(catalog.descriptors)
    }
    if len(catalog_by_id) != len(catalog.descriptors):
        raise _failure("corrupt", "catalog_project_duplicate")

    members = []
    observed_live = set()
    for state in snapshot.states:
        if type(state) is not _RepositoryStateDescriptor:
            raise _failure("corrupt", "repository_state_descriptor_invalid")
        if state.status == "deleted":
            members.append(LegacyRepositoryIntegrityMember(
                project_id=state.project_id,
                generation=state.generation,
                status=state.status,
                operation_id=state.operation_id,
                source_state_sha256=state.source_sha256,
                source_state_bytes=0,
                catalog_ordinal=None,
                import_id=None,
                document_id=None,
                document_version=None,
                storage_version=None,
                business_root_sha256=None,
                entity_count=None,
                row_snapshot_bytes=None,
            ))
            continue
        if state.status != "live":
            raise _failure("corrupt", "repository_state_not_terminal")
        catalog_item = catalog_by_id.get(state.project_id)
        if catalog_item is None:
            raise _failure("conflict", "catalog_live_project_missing")
        catalog_ordinal, project = catalog_item
        if (
            project.generation != state.generation
            or project.source_head_sha256 != state.source_sha256
            or project.source_head_bytes != len(state.source_bytes)
        ):
            raise _failure("conflict", "catalog_live_source_mismatch")
        observed_live.add(state.project_id)
        members.append(LegacyRepositoryIntegrityMember(
            project_id=state.project_id,
            generation=state.generation,
            status=state.status,
            operation_id=state.operation_id,
            source_state_sha256=state.source_sha256,
            source_state_bytes=len(state.source_bytes),
            catalog_ordinal=catalog_ordinal,
            import_id=catalog.projection.integrity.import_id,
            document_id=project.document_id,
            document_version=project.document_version,
            storage_version=project.storage_version,
            business_root_sha256=project.business_root_sha256,
            entity_count=project.entity_count,
            row_snapshot_bytes=project.row_snapshot_bytes,
        ))
    if observed_live != set(catalog_by_id):
        raise _failure("conflict", "repository_live_catalog_mismatch")
    return tuple(members)


def _repository_sealer(
    database: SqliteDatabase,
    *,
    _capability: Any = None,
) -> SqliteLegacyRepositorySealer:
    if _capability is not _LEGACY_REPOSITORY_SQLITE_ADAPTER_CAPABILITY:
        raise _failure("forbidden", "adapter_capability_required")
    return SqliteLegacyRepositorySealer(
        database,
        _capability=_LEGACY_REPOSITORY_SEAL_CAPABILITY,
    )


def _seal_repository_projection(
    database: SqliteDatabase,
    projection: Any,
    *,
    _capability: Any = None,
) -> _RepositorySealOutcome:
    """Write then freshly classify the exact projection across return edges."""

    sealer = _repository_sealer(database, _capability=_capability)
    seal_result = None
    seal_error: Optional[BaseException] = None
    reconciled = None
    reconcile_error: Optional[BaseException] = None
    try:
        try:
            seal_result = sealer.seal(projection)
        except BaseException as exc:
            seal_error = exc
    finally:
        try:
            reconciled = sealer.reconcile(projection)
        except BaseException as exc:
            reconcile_error = exc

    if reconcile_error is not None:
        if not isinstance(reconcile_error, Exception):
            raise reconcile_error
        if seal_error is not None and not isinstance(seal_error, Exception):
            raise seal_error
        if seal_error is not None:
            raise _failure("unknown", type(reconcile_error).__name__) from seal_error
        raise _failure("unknown", type(reconcile_error).__name__) from reconcile_error

    assert reconciled is not None
    if seal_error is not None and not isinstance(seal_error, Exception):
        # Fresh classification completed, but cancellation remains caller
        # control and is never converted into a successful acknowledgement.
        raise seal_error
    if reconciled.status is not LegacyRepositoryReconcileStatus.SEALED:
        error = _failure(
            reconciled.status.value,
            reconciled.error_code or "fresh_reconcile",
        )
        if seal_error is not None:
            raise error from seal_error
        raise error
    if reconciled.receipt is None:
        raise _failure("corrupt", "receipt_missing")
    return _RepositorySealOutcome(
        receipt=reconciled.receipt,
        already_sealed=(
            bool(seal_result.already_sealed)
            if seal_result is not None
            else True
        ),
        recovered_after_ack_loss=(
            bool(seal_result.recovered_after_ack_loss)
            if seal_result is not None
            else seal_error is not None
        ),
    )


def _seal_strict_legacy_repository_sqlite_shadow(
    topology: ProjectTopologyCoordinator,
    database: SqliteDatabase,
    *,
    runtime_generation: str,
    code_digest: bytes,
    _capability: Any = None,
) -> LegacyRepositoryShadowSealResult:
    """Seal S0 exactly without mounting SQLite or changing file admission."""

    if _capability is not _LEGACY_REPOSITORY_SQLITE_ADAPTER_CAPABILITY:
        raise _failure("forbidden", "adapter_capability_required")
    if type(topology) is not ProjectTopologyCoordinator:
        raise TypeError("topology must be an exact ProjectTopologyCoordinator")
    if type(database) is not SqliteDatabase:
        raise TypeError("database must be an exact SqliteDatabase")
    if type(runtime_generation) is not str or not runtime_generation:
        raise TypeError("runtime_generation must be a non-empty exact string")
    if type(code_digest) is not bytes or len(code_digest) != 32:
        raise TypeError("code_digest must be exactly 32 bytes")

    with topology._open_strict_repository_migration(
        _capability=_REPOSITORY_ADMISSION_CAPABILITY,
    ) as snapshot_s0:
        if snapshot_s0.admission.authority_state != "legacy":
            raise _failure("conflict", "repository_admission_not_legacy")

        # The core probe is the only router.  ABSENT proves the complete v5
        # namespace is unused, so the legacy v4 terminal writers remain valid.
        # PRESENT forbids those old top-level receipt-total contracts; it uses
        # two current source scans and a released active-plan projection only.
        namespace = _repository_sealer(
            database,
            _capability=_LEGACY_REPOSITORY_SQLITE_ADAPTER_CAPABILITY,
        ).probe_namespace()
        live_handles = _strict_live_handles(topology, snapshot_s0)
        if namespace.status is LegacyRepositoryNamespaceStatus.ABSENT:
            try:
                catalog = _seal_strict_legacy_catalog_sqlite_shadow_locked(
                    topology,
                    database,
                    live_handles,
                    _capability=_LEGACY_CATALOG_SQLITE_ADAPTER_CAPABILITY,
                )
            except (
                LegacyCatalogSqliteAdapterError,
                LegacySqliteShadowAdapterError,
            ) as exc:
                raise _failure(exc.status, "v4_" + exc.detail) from exc
        elif namespace.status is LegacyRepositoryNamespaceStatus.PRESENT:
            catalog = _project_strict_legacy_catalog_sqlite_shadow_locked(
                topology,
                live_handles,
                _capability=_LEGACY_CATALOG_SQLITE_ADAPTER_CAPABILITY,
            )
        else:
            raise _failure(
                namespace.status.value,
                namespace.error_code or "repository_namespace_probe",
            )
        members = _repository_members(snapshot_s0, catalog)
        snapshot_s1 = topology._revalidate_strict_repository_snapshot(
            snapshot_s0,
            _capability=_REPOSITORY_ADMISSION_CAPABILITY,
        )
        if snapshot_s1 != snapshot_s0:  # defensive; topology already rejects
            raise _failure("conflict", "repository_snapshot_s1_mismatch")

        try:
            projection = _issue_repository_projection(
                catalog.projection,
                members,
                repository_epoch=snapshot_s0.admission.authority_epoch,
                repository_revision=snapshot_s0.admission.revision,
                catalog_revision=snapshot_s0.admission.revision,
                runtime_generation=runtime_generation,
                code_digest=code_digest,
            )
        except RepositoryExactProjectionError as exc:
            raise _failure("corrupt", exc.code) from exc

        proof = None
        proof_error: Optional[BaseException] = None
        try:
            proof = _seal_repository_projection(
                database,
                projection,
                _capability=_LEGACY_REPOSITORY_SQLITE_ADAPTER_CAPABILITY,
            )
        except BaseException as exc:
            proof_error = exc
        try:
            snapshot_s2 = topology._revalidate_strict_repository_snapshot(
                snapshot_s0,
                _capability=_REPOSITORY_ADMISSION_CAPABILITY,
            )
        except BaseException as revalidate_error:
            if proof_error is not None and not isinstance(proof_error, Exception):
                raise proof_error
            if proof_error is not None:
                raise revalidate_error from proof_error
            raise
        if proof_error is not None:
            raise proof_error
        assert proof is not None
        if snapshot_s2 != snapshot_s0:  # defensive; topology already rejects
            raise _failure("conflict", "repository_snapshot_s2_mismatch")

        integrity = projection.integrity
        return LegacyRepositoryShadowSealResult(
            cutover_id=integrity.cutover_id,
            transaction_id=integrity.transaction_id,
            catalog_id=integrity.catalog_id,
            repository_epoch=integrity.repository_epoch,
            repository_revision=integrity.repository_revision,
            project_count=integrity.project_count,
            live_project_count=integrity.live_project_count,
            deleted_project_count=integrity.deleted_project_count,
            receipt=proof.receipt,
            already_sealed=proof.already_sealed,
            recovered_after_ack_loss=proof.recovered_after_ack_loss,
        )


__all__: list[str] = []
