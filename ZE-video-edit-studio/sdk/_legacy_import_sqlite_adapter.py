"""Private SQLite bridges for a terminal shadow and one historical catalog seal.

The catalog entry below is the sole supported SDK consumer of the core seal
capability.  While the topology owner holds its strict repository-wide
migration barrier, it scans every live business project twice: once for the
terminal shadow and once for an independently prepared exact plan used by the
seal writer.  The durable result proves the complete *business live catalog*
at that completed observation only.  Releasing the barrier does not keep it
current, switch authority, or cover tombstones, operation logs, assets,
inbox/outbox rows, or pending topology actions.  Nothing in this module is
mounted into public SDK, bootstrap, runtime, or server paths.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Tuple

from . import _paths  # noqa: F401  (install flat persistence imports first)
from .legacy_import import (
    FrozenLegacyImport,
    LegacyImportError,
    LegacyScanHandle,
    PreparedLegacyPayloadReceipt,
    _LEGACY_IMPORT_ADAPTER_CAPABILITY,
    _finish_prepared_legacy_import,
    _recover_prepared_legacy_import,
    _reserve_prepared_legacy_import,
    _verify_prepared_legacy_import_plan_binding,
    prepare_legacy_import,
    scan_legacy_project,
)
from .project_topology import ProjectHandle, ProjectTopologyCoordinator

from legacy_catalog_integrity import (  # noqa: E402
    LegacyCatalogIntegrityProject,
    derive_legacy_catalog_import_id,
)
from sqlite_kernel import KernelReceipt, SqliteDatabase  # noqa: E402
from sqlite_legacy_catalog import (  # noqa: E402
    LegacyCatalogReconcileStatus,
    SQLiteLegacyCatalogError,
    SqliteLegacyCatalogSealer,
    _LEGACY_CATALOG_SEAL_CAPABILITY,
)
from sqlite_legacy_catalog_exact import (  # noqa: E402
    CatalogExactProjection,
    project_empty_catalog,
    project_normalized_import_plan,
)
from sqlite_legacy_import import (  # noqa: E402
    LegacyImportReconcileStatus,
    SQLiteLegacyImportError,
    SqliteLegacyImporter,
    _project_plan_graph,
)


_LEGACY_SQLITE_SHADOW_ADAPTER_CAPABILITY = object()
_LEGACY_CATALOG_SQLITE_ADAPTER_CAPABILITY = object()


class LegacySqliteShadowAdapterError(RuntimeError):
    """Stable failure after a fresh target-state classification."""

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


class LegacyCatalogSqliteAdapterError(RuntimeError):
    """Stable, payload-free failure after catalog target classification."""

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
class LegacySqliteShadowImportResult:
    """Exact terminal-shadow receipt; never a catalog/currentness proof."""

    import_id: str
    transaction_id: str
    receipt: KernelReceipt
    already_imported: bool
    recovered_after_ack_loss: bool


@dataclass(frozen=True)
class LegacyCatalogShadowSealResult:
    """Historical business-catalog evidence, never a cutover token."""

    catalog_id: str
    catalog_transaction_id: str
    import_id: Optional[str]
    live_project_count: int
    import_receipt: Optional[KernelReceipt]
    catalog_receipt: KernelReceipt
    already_imported: bool
    already_sealed: bool
    recovered_after_ack_loss: bool
    authority: str = field(init=False, default="legacy")

    @property
    def historical_fenced(self) -> bool:
        return True

    @property
    def cutover_ready(self) -> bool:
        return False


@dataclass(frozen=True)
class _StrictLegacyCatalogEvidence:
    """Path-free exact catalog evidence confined to private coordinators."""

    result: LegacyCatalogShadowSealResult
    descriptors: Tuple[LegacyCatalogIntegrityProject, ...] = field(repr=False)
    projection: CatalogExactProjection = field(repr=False)


@dataclass(frozen=True)
class _StrictLegacyCatalogProjection:
    """Safe path-free output of the two current-source scans."""

    descriptors: Tuple[LegacyCatalogIntegrityProject, ...] = field(repr=False)
    projection: CatalogExactProjection = field(repr=False)


def _finish_exact_outcome(reservation: Any, outcome: str) -> None:
    """Retry once when finish itself loses its acknowledgement edge."""

    try:
        _finish_prepared_legacy_import(
            reservation,
            outcome=outcome,
            _capability=_LEGACY_IMPORT_ADAPTER_CAPABILITY,
        )
    except BaseException as first_error:
        _finish_prepared_legacy_import(
            reservation,
            outcome=outcome,
            _capability=_LEGACY_IMPORT_ADAPTER_CAPABILITY,
        )
        # finish_prepared is exact-outcome idempotent.  A successful retry is
        # the acknowledgement for the first call whose return edge was lost;
        # propagating the stale exception would report failure after both the
        # durable target and the process lane are already terminal.
        if not isinstance(first_error, Exception):
            raise first_error
        return


def _reserve_or_recover(
    package: FrozenLegacyImport,
    receipt: PreparedLegacyPayloadReceipt,
    attempt_token: object,
):
    def recover_exact():
        return _recover_prepared_legacy_import(
            package,
            receipt,
            _attempt_token=attempt_token,
            _capability=_LEGACY_IMPORT_ADAPTER_CAPABILITY,
        )

    try:
        return _reserve_prepared_legacy_import(
            package,
            receipt,
            _attempt_token=attempt_token,
            _capability=_LEGACY_IMPORT_ADAPTER_CAPABILITY,
        )
    except BaseException as reserve_error:
        # The exact attempt token is created by the caller before reserve.  It
        # is therefore the only safe way to distinguish a genuine rejection
        # from an exception/async interruption on the successful return edge.
        recovery_error: BaseException | None = None
        try:
            recovered = recover_exact()
        except BaseException as first_recovery_error:
            recovery_error = first_recovery_error
            # Recovery has its own acknowledgement edge: it may have returned
            # the exact active pair internally before an ordinary exception or
            # process-control signal reached this frame.  One exact-token retry
            # either retrieves that same pair (so we can finish it) or proves
            # that no supported recovery is available.  Never let the older
            # reserve exception hide a newer KeyboardInterrupt/SystemExit.
            try:
                recovered = recover_exact()
            except BaseException as second_recovery_error:
                if not isinstance(second_recovery_error, Exception):
                    raise second_recovery_error
                if not isinstance(first_recovery_error, Exception):
                    raise first_recovery_error
                if not isinstance(reserve_error, Exception):
                    raise reserve_error
                raise reserve_error

        cancellation = next(
            (
                error
                for error in (recovery_error, reserve_error)
                if error is not None and not isinstance(error, Exception)
            ),
            None,
        )
        if cancellation is not None:
            # Recovery proves reserve won before an async return-edge
            # interruption.  Release that exact lane before preserving the
            # caller's cancellation; otherwise outer cleanup has no handle.
            _finish_exact_outcome(recovered[0], "release")
            raise cancellation
        return recovered


def _import_prepared_legacy_sqlite_shadow(
    database: SqliteDatabase,
    package: FrozenLegacyImport,
    receipt: PreparedLegacyPayloadReceipt,
    *,
    _attempt_token: object,
    _capability: Any = None,
) -> LegacySqliteShadowImportResult:
    """Write/reconcile one exact plan and resolve its process lane honestly."""

    if _capability is not _LEGACY_SQLITE_SHADOW_ADAPTER_CAPABILITY:
        raise LegacySqliteShadowAdapterError(
            "legacy_sqlite_shadow_adapter_forbidden",
            "forbidden",
            "adapter_capability_required",
        )
    if type(database) is not SqliteDatabase:
        raise TypeError("database must be an exact SqliteDatabase")
    if type(_attempt_token) is not object:
        raise LegacySqliteShadowAdapterError(
            "legacy_sqlite_shadow_attempt_invalid",
            "invalid",
            "attempt_token_invalid",
        )

    reservation, plan = _reserve_or_recover(
        package,
        receipt,
        _attempt_token,
    )
    decision_started: str | None = None
    write_started = False
    try:
        _verify_prepared_legacy_import_plan_binding(
            reservation,
            plan,
            _capability=_LEGACY_IMPORT_ADAPTER_CAPABILITY,
        )
        importer = SqliteLegacyImporter(database)
        commit_result = None
        commit_error: BaseException | None = None
        try:
            # From this instruction onward an async interruption may have
            # raced a durable target write even if Python never stores the
            # returned result.  Until fresh classification says otherwise,
            # outer cleanup must quarantine rather than release this lane.
            write_started = True
            commit_result = importer.import_plan(plan)
        except BaseException as exc:
            commit_error = exc

        # This call always opens a fresh connection.  Neither a returned
        # commit result nor an exception is allowed to choose the lane.
        try:
            reconciled = importer.reconcile(plan)
        except BaseException as reconcile_error:
            decision_started = "quarantine"
            _finish_exact_outcome(reservation, "quarantine")
            if not isinstance(reconcile_error, Exception):
                raise reconcile_error
            if commit_error is not None and not isinstance(commit_error, Exception):
                raise commit_error
            if commit_error is not None:
                raise LegacySqliteShadowAdapterError(
                    "legacy_sqlite_shadow_unknown",
                    "unknown",
                    type(reconcile_error).__name__,
                ) from commit_error
            raise LegacySqliteShadowAdapterError(
                "legacy_sqlite_shadow_unknown",
                "unknown",
                type(reconcile_error).__name__,
            ) from reconcile_error

        try:
            _verify_prepared_legacy_import_plan_binding(
                reservation,
                plan,
                _capability=_LEGACY_IMPORT_ADAPTER_CAPABILITY,
            )
        except BaseException:
            decision_started = "quarantine"
            _finish_exact_outcome(reservation, "quarantine")
            raise

        outcome = {
            LegacyImportReconcileStatus.IMPORTED: "consumed",
            LegacyImportReconcileStatus.NOT_IMPORTED: "release",
            LegacyImportReconcileStatus.CONFLICT: "quarantine",
            LegacyImportReconcileStatus.CORRUPT: "quarantine",
            LegacyImportReconcileStatus.UNKNOWN: "quarantine",
        }[reconciled.status]
        decision_started = outcome
        _finish_exact_outcome(reservation, outcome)

        if commit_error is not None and not isinstance(commit_error, Exception):
            raise commit_error
        if reconciled.status is not LegacyImportReconcileStatus.IMPORTED:
            error = LegacySqliteShadowAdapterError(
                "legacy_sqlite_shadow_" + reconciled.status.value,
                reconciled.status.value,
                reconciled.error_code or "fresh_reconcile",
            )
            if commit_error is not None:
                raise error from commit_error
            raise error
        if reconciled.receipt is None:
            raise LegacySqliteShadowAdapterError(
                "legacy_sqlite_shadow_corrupt",
                "corrupt",
                "receipt_missing",
            )
        return LegacySqliteShadowImportResult(
            import_id=reconciled.import_id,
            transaction_id=reconciled.transaction_id,
            receipt=reconciled.receipt,
            already_imported=(
                bool(commit_result.already_imported)
                if commit_result is not None
                else True
            ),
            recovered_after_ack_loss=(
                bool(commit_result.recovered_after_ack_loss)
                if commit_result is not None
                else False
            ),
        )
    except (LegacyImportError, SQLiteLegacyImportError):
        # Errors raised before a fresh classification are not guessed into a
        # release.  The surrounding branches either already resolved the lane
        # or this is a binding/integrity failure and must remain quarantined.
        if decision_started is None:
            decision_started = "quarantine"
            _finish_exact_outcome(reservation, "quarantine")
        raise
    except Exception:
        if decision_started is None:
            decision_started = "quarantine"
            _finish_exact_outcome(reservation, "quarantine")
        raise
    except BaseException:
        if decision_started is None:
            decision_started = "quarantine" if write_started else "release"
            _finish_exact_outcome(reservation, decision_started)
        raise


@dataclass(frozen=True)
class _CatalogSealOutcome:
    catalog_id: str
    import_id: Optional[str]
    transaction_id: str
    receipt: KernelReceipt
    already_sealed: bool
    recovered_after_ack_loss: bool
    projection: CatalogExactProjection = field(repr=False)


def _catalog_failure(status: str, detail: str) -> LegacyCatalogSqliteAdapterError:
    return LegacyCatalogSqliteAdapterError(
        "legacy_catalog_sqlite_shadow_" + status,
        status,
        detail,
    )


def _scan_descriptor(handle: Any) -> LegacyCatalogIntegrityProject:
    """Copy the complete public scan descriptor into the pure exact carrier."""

    if type(handle) is not LegacyScanHandle:
        raise LegacyCatalogSqliteAdapterError(
            "legacy_catalog_sqlite_shadow_scan_invalid",
            "invalid",
            "scan_handle_invalid",
        )
    return LegacyCatalogIntegrityProject(
        project_id=handle.project_id,
        generation=handle.generation,
        document_id=handle.document_id,
        document_version=handle.document_version,
        storage_version=handle.storage_version,
        source_head_sha256=handle.source_head_sha256,
        source_head_bytes=handle.source_head_bytes,
        business_root_sha256=handle.business_root,
        entity_count=handle.entity_count,
        row_snapshot_bytes=handle.row_snapshot_bytes,
    )


def _scan_complete_catalog(
    topology: ProjectTopologyCoordinator,
    live_handles: Tuple[Any, ...],
) -> tuple[Tuple[LegacyScanHandle, ...], Tuple[LegacyCatalogIntegrityProject, ...]]:
    scans = tuple(
        scan_legacy_project(
            topology,
            live_handle.project_id,
            live_handle.generation,
        )
        for live_handle in live_handles
    )
    return scans, tuple(_scan_descriptor(handle) for handle in scans)


def _validate_strict_live_handles(value: Any) -> Tuple[ProjectHandle, ...]:
    if type(value) is not tuple or any(
        type(item) is not ProjectHandle for item in value
    ):
        raise LegacyCatalogSqliteAdapterError(
            "legacy_catalog_sqlite_shadow_catalog_invalid",
            "invalid",
            "strict_catalog_handles_invalid",
        )
    project_ids = tuple(item.project_id for item in value)
    if project_ids != tuple(sorted(project_ids)) or len(set(project_ids)) != len(
        project_ids
    ):
        raise LegacyCatalogSqliteAdapterError(
            "legacy_catalog_sqlite_shadow_catalog_invalid",
            "invalid",
            "strict_catalog_order_invalid",
        )
    return value


def _catalog_sealer(
    database: SqliteDatabase,
    *,
    _capability: Any = None,
) -> SqliteLegacyCatalogSealer:
    # This line is intentionally the sole SDK consumption point for the core
    # seal capability.  Keeping it here makes the supported call graph static:
    # strict coordinator -> private bridge -> INSERT-only core sealer.
    if _capability is not _LEGACY_CATALOG_SQLITE_ADAPTER_CAPABILITY:
        raise LegacyCatalogSqliteAdapterError(
            "legacy_catalog_sqlite_shadow_forbidden",
            "forbidden",
            "adapter_capability_required",
        )
    return SqliteLegacyCatalogSealer(
        database,
        _capability=_LEGACY_CATALOG_SEAL_CAPABILITY,
    )


def _empty_catalog_outcome(
    database: SqliteDatabase,
    *,
    _capability: Any = None,
) -> _CatalogSealOutcome:
    """Seal/reconcile an exact empty target without creating scan lanes."""

    sealer = _catalog_sealer(database, _capability=_capability)
    seal_result = None
    seal_error: Optional[BaseException] = None
    reconciled = None
    reconcile_error: Optional[BaseException] = None
    try:
        try:
            seal_result = sealer.seal_empty()
        except BaseException as exc:
            seal_error = exc
    finally:
        # Keep the write call and its required fresh classification in one
        # protected region.  Process-control arriving on the return edge still
        # runs this reconcile before it can leave the adapter.
        try:
            reconciled = sealer.reconcile_empty()
        except BaseException as exc:
            reconcile_error = exc

    if reconcile_error is not None:
        if not isinstance(reconcile_error, Exception):
            raise reconcile_error
        if seal_error is not None and not isinstance(seal_error, Exception):
            raise seal_error
        if seal_error is not None:
            raise _catalog_failure(
                "unknown", type(reconcile_error).__name__,
            ) from seal_error
        raise _catalog_failure(
            "unknown", type(reconcile_error).__name__,
        ) from reconcile_error

    assert reconciled is not None
    if seal_error is not None and not isinstance(seal_error, Exception):
        # A fresh classification is still required before cancellation leaves
        # this frame, but cancellation is never converted into acknowledgement.
        raise seal_error
    if reconciled.status is not LegacyCatalogReconcileStatus.SEALED:
        error = _catalog_failure(
            reconciled.status.value,
            reconciled.error_code or "fresh_reconcile",
        )
        if seal_error is not None:
            raise error from seal_error
        raise error
    if reconciled.receipt is None:
        raise _catalog_failure("corrupt", "receipt_missing")
    return _CatalogSealOutcome(
        catalog_id=reconciled.catalog_id,
        import_id=reconciled.import_id,
        transaction_id=reconciled.transaction_id,
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
        projection=project_empty_catalog(),
    )


def _seal_prepared_legacy_catalog(
    database: SqliteDatabase,
    package: FrozenLegacyImport,
    receipt: PreparedLegacyPayloadReceipt,
    *,
    _attempt_token: object,
    _capability: Any = None,
) -> _CatalogSealOutcome:
    """Resolve the second prepared lane from a fresh catalog classification."""

    if _capability is not _LEGACY_CATALOG_SQLITE_ADAPTER_CAPABILITY:
        raise LegacyCatalogSqliteAdapterError(
            "legacy_catalog_sqlite_shadow_forbidden",
            "forbidden",
            "adapter_capability_required",
        )
    reservation, plan = _reserve_or_recover(package, receipt, _attempt_token)
    decision_started: Optional[str] = None
    write_started = False
    try:
        _verify_prepared_legacy_import_plan_binding(
            reservation,
            plan,
            _capability=_LEGACY_IMPORT_ADAPTER_CAPABILITY,
        )
        # Normalize the pure catalog projection while this exact active plan is
        # still owned by the reservation.  The later consumed/quarantined
        # finish edge must never be followed by a new read from plan payloads.
        projection = project_normalized_import_plan(_project_plan_graph(plan))
        sealer = _catalog_sealer(database, _capability=_capability)
        seal_result = None
        seal_error: Optional[BaseException] = None
        reconciled = None
        reconcile_error: Optional[BaseException] = None
        try:
            try:
                # The return edge is uncertain from this instruction onward.
                # Only the later fresh reconcile may consume or release this
                # lane.
                write_started = True
                seal_result = sealer.seal(plan)
            except BaseException as exc:
                seal_error = exc
        finally:
            # An async exception on the seal return edge cannot jump around the
            # required fresh target classification.
            try:
                reconciled = sealer.reconcile(plan)
            except BaseException as exc:
                reconcile_error = exc

        if reconcile_error is not None:
            decision_started = "quarantine"
            _finish_exact_outcome(reservation, "quarantine")
            if not isinstance(reconcile_error, Exception):
                raise reconcile_error
            if seal_error is not None and not isinstance(seal_error, Exception):
                raise seal_error
            if seal_error is not None:
                raise _catalog_failure(
                    "unknown", type(reconcile_error).__name__,
                ) from seal_error
            raise _catalog_failure(
                "unknown", type(reconcile_error).__name__,
            ) from reconcile_error

        assert reconciled is not None
        try:
            _verify_prepared_legacy_import_plan_binding(
                reservation,
                plan,
                _capability=_LEGACY_IMPORT_ADAPTER_CAPABILITY,
            )
        except BaseException:
            decision_started = "quarantine"
            _finish_exact_outcome(reservation, "quarantine")
            raise

        outcome = {
            LegacyCatalogReconcileStatus.SEALED: "consumed",
            LegacyCatalogReconcileStatus.NOT_SEALED: "release",
            LegacyCatalogReconcileStatus.CONFLICT: "quarantine",
            LegacyCatalogReconcileStatus.CORRUPT: "quarantine",
            LegacyCatalogReconcileStatus.UNKNOWN: "quarantine",
        }[reconciled.status]
        decision_started = outcome
        _finish_exact_outcome(reservation, outcome)

        if seal_error is not None and not isinstance(seal_error, Exception):
            raise seal_error
        if reconciled.status is not LegacyCatalogReconcileStatus.SEALED:
            error = _catalog_failure(
                reconciled.status.value,
                reconciled.error_code or "fresh_reconcile",
            )
            if seal_error is not None:
                raise error from seal_error
            raise error
        if reconciled.receipt is None:
            raise _catalog_failure("corrupt", "receipt_missing")
        return _CatalogSealOutcome(
            catalog_id=reconciled.catalog_id,
            import_id=reconciled.import_id,
            transaction_id=reconciled.transaction_id,
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
            projection=projection,
        )
    except (LegacyImportError, SQLiteLegacyCatalogError):
        if decision_started is None:
            decision_started = "quarantine"
            _finish_exact_outcome(reservation, "quarantine")
        raise
    except Exception:
        if decision_started is None:
            decision_started = "quarantine"
            _finish_exact_outcome(reservation, "quarantine")
        raise
    except BaseException:
        if decision_started is None:
            decision_started = "quarantine" if write_started else "release"
            _finish_exact_outcome(reservation, decision_started)
        raise


def _project_strict_legacy_catalog_sqlite_shadow_locked(
    topology: ProjectTopologyCoordinator,
    raw_live_handles: Tuple[Any, ...],
    *,
    _capability: Any = None,
) -> _StrictLegacyCatalogProjection:
    """Rebuild current v4 evidence without touching its legacy DB writers.

    This path exists only for a core-probed PRESENT v5 namespace, where the old
    v4 top-level receipt-total contracts must not run.  Both prepared payload
    lanes are reserved, verified, copied into a core-normalized pure projection
    while active, and explicitly released before this function returns.
    """

    if _capability is not _LEGACY_CATALOG_SQLITE_ADAPTER_CAPABILITY:
        raise LegacyCatalogSqliteAdapterError(
            "legacy_catalog_sqlite_shadow_forbidden",
            "forbidden",
            "adapter_capability_required",
        )
    if type(topology) is not ProjectTopologyCoordinator:
        raise TypeError("topology must be an exact ProjectTopologyCoordinator")

    live_handles = _validate_strict_live_handles(raw_live_handles)
    if not live_handles:
        return _StrictLegacyCatalogProjection(
            descriptors=(),
            projection=project_empty_catalog(),
        )

    first_scans, first_descriptors = _scan_complete_catalog(
        topology, live_handles,
    )
    import_id = derive_legacy_catalog_import_id(first_descriptors)
    first_package, first_receipt = prepare_legacy_import(import_id, first_scans)
    first_reservation = None
    second_reservation = None
    try:
        first_reservation, first_plan = _reserve_or_recover(
            first_package, first_receipt, object(),
        )
        _verify_prepared_legacy_import_plan_binding(
            first_reservation,
            first_plan,
            _capability=_LEGACY_IMPORT_ADAPTER_CAPABILITY,
        )
        # The first scan contributes only the independent comparison
        # descriptor.  Resolve its potentially very large active graph before
        # scanning/reserving the second copy, so legal 1 GiB inputs never hold
        # two normalized payload graphs at once.
        _finish_exact_outcome(first_reservation, "release")
        first_reservation = None
        del first_plan, first_package, first_receipt, first_scans

        second_scans, second_descriptors = _scan_complete_catalog(
            topology, live_handles,
        )
        if second_descriptors != first_descriptors:
            raise LegacyCatalogSqliteAdapterError(
                "legacy_catalog_sqlite_shadow_source_drift",
                "conflict",
                "second_scan_descriptor_mismatch",
            )
        second_import_id = derive_legacy_catalog_import_id(second_descriptors)
        if second_import_id != import_id:
            raise LegacyCatalogSqliteAdapterError(
                "legacy_catalog_sqlite_shadow_source_drift",
                "conflict",
                "second_scan_import_id_mismatch",
            )
        second_package, second_receipt = prepare_legacy_import(
            second_import_id, second_scans,
        )
        second_reservation, second_plan = _reserve_or_recover(
            second_package, second_receipt, object(),
        )
        _verify_prepared_legacy_import_plan_binding(
            second_reservation,
            second_plan,
            _capability=_LEGACY_IMPORT_ADAPTER_CAPABILITY,
        )
        projection = project_normalized_import_plan(
            _project_plan_graph(second_plan)
        )
        if projection.projects != second_descriptors:
            raise _catalog_failure("corrupt", "catalog_projection_mismatch")
        return _StrictLegacyCatalogProjection(
            descriptors=second_descriptors,
            projection=projection,
        )
    finally:
        try:
            if second_reservation is not None:
                _finish_exact_outcome(second_reservation, "release")
        finally:
            if first_reservation is not None:
                _finish_exact_outcome(first_reservation, "release")


def _seal_strict_legacy_catalog_sqlite_shadow_locked(
    topology: ProjectTopologyCoordinator,
    database: SqliteDatabase,
    raw_live_handles: Tuple[Any, ...],
    *,
    _capability: Any = None,
) -> _StrictLegacyCatalogEvidence:
    """Seal one catalog while an enclosing strict repository lease is held."""

    if _capability is not _LEGACY_CATALOG_SQLITE_ADAPTER_CAPABILITY:
        raise LegacyCatalogSqliteAdapterError(
            "legacy_catalog_sqlite_shadow_forbidden",
            "forbidden",
            "adapter_capability_required",
        )
    if type(topology) is not ProjectTopologyCoordinator:
        raise TypeError("topology must be an exact ProjectTopologyCoordinator")
    if type(database) is not SqliteDatabase:
        raise TypeError("database must be an exact SqliteDatabase")

    live_handles = _validate_strict_live_handles(raw_live_handles)
    if not live_handles:
        sealed = _empty_catalog_outcome(
            database,
            _capability=_LEGACY_CATALOG_SQLITE_ADAPTER_CAPABILITY,
        )
        if sealed.import_id is not None:
            raise _catalog_failure("corrupt", "empty_catalog_import_id_present")
        if sealed.projection.projects or (
            sealed.projection.integrity.catalog_id != sealed.catalog_id
        ):
            raise _catalog_failure("corrupt", "empty_catalog_projection_mismatch")
        result = LegacyCatalogShadowSealResult(
            catalog_id=sealed.catalog_id,
            catalog_transaction_id=sealed.transaction_id,
            import_id=None,
            live_project_count=0,
            import_receipt=None,
            catalog_receipt=sealed.receipt,
            already_imported=False,
            already_sealed=sealed.already_sealed,
            recovered_after_ack_loss=sealed.recovered_after_ack_loss,
        )
        return _StrictLegacyCatalogEvidence(
            result=result,
            descriptors=(),
            projection=sealed.projection,
        )

    first_scans, first_descriptors = _scan_complete_catalog(
        topology, live_handles,
    )
    import_id = derive_legacy_catalog_import_id(first_descriptors)
    first_package, first_receipt = prepare_legacy_import(
        import_id, first_scans,
    )
    imported = _import_prepared_legacy_sqlite_shadow(
        database,
        first_package,
        first_receipt,
        _attempt_token=object(),
        _capability=_LEGACY_SQLITE_SHADOW_ADAPTER_CAPABILITY,
    )
    if imported.import_id != import_id:
        raise _catalog_failure("corrupt", "import_identity_mismatch")
    # The terminal importer has copied and resolved the first payload.  Drop
    # every scan/package carrier before producing scan two so its independent
    # runtime lane and process-ledger records cannot double the peak graph.
    del first_scans, first_package, first_receipt

    second_scans, second_descriptors = _scan_complete_catalog(
        topology, live_handles,
    )
    if second_descriptors != first_descriptors:
        raise LegacyCatalogSqliteAdapterError(
            "legacy_catalog_sqlite_shadow_source_drift",
            "conflict",
            "second_scan_descriptor_mismatch",
        )
    second_import_id = derive_legacy_catalog_import_id(second_descriptors)
    if second_import_id != import_id:
        raise LegacyCatalogSqliteAdapterError(
            "legacy_catalog_sqlite_shadow_source_drift",
            "conflict",
            "second_scan_import_id_mismatch",
        )
    seal_package, seal_receipt = prepare_legacy_import(
        second_import_id, second_scans,
    )
    sealed = _seal_prepared_legacy_catalog(
        database,
        seal_package,
        seal_receipt,
        _attempt_token=object(),
        _capability=_LEGACY_CATALOG_SQLITE_ADAPTER_CAPABILITY,
    )
    if (
        sealed.import_id != import_id
        or sealed.catalog_id == ""
        or sealed.transaction_id == ""
    ):
        raise _catalog_failure("corrupt", "catalog_identity_missing")
    if (
        sealed.projection.projects != second_descriptors
        or sealed.projection.integrity.catalog_id != sealed.catalog_id
        or sealed.projection.integrity.transaction_id != sealed.transaction_id
    ):
        raise _catalog_failure("corrupt", "catalog_projection_mismatch")
    result = LegacyCatalogShadowSealResult(
        catalog_id=sealed.catalog_id,
        catalog_transaction_id=sealed.transaction_id,
        import_id=import_id,
        live_project_count=len(first_descriptors),
        import_receipt=imported.receipt,
        catalog_receipt=sealed.receipt,
        already_imported=imported.already_imported,
        already_sealed=sealed.already_sealed,
        recovered_after_ack_loss=(
            imported.recovered_after_ack_loss
            or sealed.recovered_after_ack_loss
        )
    )
    return _StrictLegacyCatalogEvidence(
        result=result,
        descriptors=second_descriptors,
        projection=sealed.projection,
    )


def _seal_strict_legacy_catalog_sqlite_shadow(
    topology: ProjectTopologyCoordinator,
    database: SqliteDatabase,
    *,
    _capability: Any = None,
) -> LegacyCatalogShadowSealResult:
    """Import and historically seal the exact strict live business catalog."""

    if _capability is not _LEGACY_CATALOG_SQLITE_ADAPTER_CAPABILITY:
        raise LegacyCatalogSqliteAdapterError(
            "legacy_catalog_sqlite_shadow_forbidden",
            "forbidden",
            "adapter_capability_required",
        )
    if type(topology) is not ProjectTopologyCoordinator:
        raise TypeError("topology must be an exact ProjectTopologyCoordinator")
    if type(database) is not SqliteDatabase:
        raise TypeError("database must be an exact SqliteDatabase")

    # The context spans both scans, both writes and the final fresh catalog
    # reconcile.  No active plan, reservation, path or scan carrier escapes it.
    with topology.open_strict_catalog_migration() as raw_live_handles:
        evidence = _seal_strict_legacy_catalog_sqlite_shadow_locked(
            topology,
            database,
            raw_live_handles,
            _capability=_LEGACY_CATALOG_SQLITE_ADAPTER_CAPABILITY,
        )
        return evidence.result


__all__: list[str] = []
