"""Strict legacy-project scan and frozen-payload compatibility seam.

Preparation proves only that an explicit set of live projects produced one
canonical, verified virtual payload.  It is deliberately not a persistence
commit, authority receipt, cutover, or adapter write.  The current runtime lane
restores one scan handle into the existing detached-head pipeline; the private
adapter lane projects one complete immutable row-intent plan for the future
SQLite authority implementation.
"""

from __future__ import annotations

import hashlib
from typing import Any

from . import _paths  # noqa: F401  (install flat core imports first)
from ._legacy_import_state import (
    FrozenLegacyImport,
    LegacyImportError,
    LegacyScanHandle,
    PreparedLegacyPayloadReceipt,
    VerifiedLegacyImportPlan,
    _PreparedReservation,
    _abort_runtime_handle,
    _finish_prepared_import,
    _finish_runtime_handle,
    _issue_scan_handle,
    _prepare_frozen_import,
    _recover_prepared_import,
    _reserve_prepared_import,
    _reserve_runtime_handle,
    _verify_prepared_plan_binding,
    _verified_prepared_plan,
    _verified_runtime_snapshot,
)
from ._runtime_rehydration_projection import detach_restored_business_head
from ._runtime_rehydration_state import DetachedBusinessHead
from .project_topology import ProjectTopologyCoordinator, _LiveScanSource

from business_snapshot import (  # noqa: E402
    BusinessSnapshotError,
    capture_business_snapshot,
    restore_business_snapshot,
    verify_business_snapshot,
)
from document import (  # noqa: E402
    Document,
    DocumentHead,
    _LEGACY_SCAN_DOCUMENT_CAPABILITY,
    _open_document_for_legacy_scan,
)
from entity import ZeError  # noqa: E402


_LEGACY_IMPORT_ADAPTER_CAPABILITY = object()
_MAX_DURABLE_GENERATION = 2**63 - 1

# Preserve exact state-layer identity while presenting the stable facade path
# in repr/introspection.  No wrapper or duplicate carrier class is introduced.
_FACADE_REEXPORTS = (
    FrozenLegacyImport,
    LegacyImportError,
    LegacyScanHandle,
    PreparedLegacyPayloadReceipt,
)
for _facade_export in _FACADE_REEXPORTS:
    _facade_export.__module__ = __name__
del _facade_export


def _fail(code: str, reason: str) -> None:
    raise LegacyImportError(code, reason)


def _validate_scan_request(
    topology: Any,
    project_id: Any,
    expected_generation: Any,
) -> tuple[ProjectTopologyCoordinator, str, int]:
    if type(topology) is not ProjectTopologyCoordinator:
        _fail("legacy_import_scan_forbidden", "topology_authority_required")
    if type(project_id) is not str:
        _fail("legacy_import_scan_invalid", "project_id_invalid")
    try:
        # Reuse the topology owner's length/shape budget before UTF-8 encoding;
        # this prevents a huge exact str from causing a second huge allocation
        # and avoids a duplicate 160-character policy in this facade.
        project_id = topology._validate_project_id(project_id)
    except ZeError:
        raise LegacyImportError(
            "legacy_import_scan_invalid", "project_id_invalid",
        ) from None
    try:
        project_id.encode("utf-8", "strict")
    except UnicodeEncodeError:
        _fail("legacy_import_scan_invalid", "project_id_invalid")
    if (
        type(expected_generation) is not int
        or expected_generation < 1
        or expected_generation > _MAX_DURABLE_GENERATION
    ):
        _fail("legacy_import_scan_invalid", "expected_generation_invalid")
    return topology, project_id, expected_generation


def _validate_bound_source(
    source: Any,
    *,
    project_id: str,
    expected_generation: int,
) -> _LiveScanSource:
    if (
        type(source) is not _LiveScanSource
        or source.project_id != project_id
        or source.generation != expected_generation
        or type(source.head) is not DocumentHead
        or type(source.head.payload) is not bytes
    ):
        _fail("legacy_import_scan_invalid", "live_source_invalid")
    return source


def scan_legacy_project(
    topology: ProjectTopologyCoordinator,
    project_id: str,
    expected_generation: int,
) -> LegacyScanHandle:
    """Capture one exact live target while topology and both content SHs hold."""
    topology, project_id, expected_generation = _validate_scan_request(
        topology,
        project_id,
        expected_generation,
    )
    try:
        with topology.open_live_scan_source(
            project_id,
            expected_generation,
        ) as raw_source:
            source = _validate_bound_source(
                raw_source,
                project_id=project_id,
                expected_generation=expected_generation,
            )
            outer_head = source.head
            with _open_document_for_legacy_scan(
                source.document_path,
                _expected_root_identity=source.root_identity,
                _capability=_LEGACY_SCAN_DOCUMENT_CAPABILITY,
            ) as opened:
                if type(opened) is not tuple or len(opened) != 2:
                    _fail("legacy_import_scan_invalid", "document_source_invalid")
                inner_head, document = opened
                if type(inner_head) is not DocumentHead or type(document) is not Document:
                    _fail("legacy_import_scan_invalid", "document_source_invalid")
                if (
                    inner_head.payload != outer_head.payload
                    or inner_head.metadata.id != outer_head.metadata.id
                    or inner_head.metadata.version != outer_head.metadata.version
                ):
                    _fail("legacy_import_scan_invalid", "source_head_mismatch")
                if (
                    document.id != inner_head.metadata.id
                    or document.version != inner_head.metadata.version
                ):
                    _fail("legacy_import_scan_invalid", "document_head_mismatch")

                captured = capture_business_snapshot(
                    source.project_id,
                    source.generation,
                    document,
                )
                if captured.excluded_read_only:
                    _fail("legacy_import_scan_invalid", "read_only_entities_excluded")
                snapshot = verify_business_snapshot(captured.snapshot)
                if (
                    snapshot.project.project_id != source.project_id
                    or snapshot.project.generation != source.generation
                    or snapshot.document.document_id != inner_head.metadata.id
                    or snapshot.document.document_version
                    != inner_head.metadata.version
                ):
                    _fail("legacy_import_scan_invalid", "snapshot_source_mismatch")

                source_head_sha256 = hashlib.sha256(outer_head.payload).digest()
                source_head_bytes = len(outer_head.payload)
                storage_version = inner_head.metadata.storage_version

        # Issue only after the topology context's post-yield fences succeed.
        return _issue_scan_handle(
            snapshot,
            source_head_sha256=source_head_sha256,
            source_head_bytes=source_head_bytes,
            storage_version=storage_version,
        )
    except LegacyImportError:
        raise
    except BusinessSnapshotError as exc:
        raise LegacyImportError(
            "legacy_import_scan_invalid", exc.code,
        ) from None
    except ZeError as exc:
        raise LegacyImportError(
            "legacy_import_scan_failed", exc.code,
        ) from None
    except Exception:
        raise LegacyImportError(
            "legacy_import_scan_failed", "internal_failure",
        ) from None


def prepare_legacy_import(
    import_id: str,
    handles: tuple[LegacyScanHandle, ...],
) -> tuple[FrozenLegacyImport, PreparedLegacyPayloadReceipt]:
    """Consume every handle's payload lane into one verified virtual stream."""
    return _prepare_frozen_import(import_id, handles)


def detach_legacy_runtime_head(handle: LegacyScanHandle) -> DetachedBusinessHead:
    """Consume only the independent file-authority runtime compatibility lane."""
    reservation = _reserve_runtime_handle(handle)
    try:
        snapshot = _verified_runtime_snapshot(reservation)
        restored = restore_business_snapshot(snapshot)
        detached = detach_restored_business_head(restored)
        _finish_runtime_handle(reservation)
        return detached
    except LegacyImportError:
        _abort_runtime_handle(reservation)
        raise
    except BusinessSnapshotError as exc:
        _abort_runtime_handle(reservation)
        raise LegacyImportError(
            "legacy_import_runtime_failed", exc.code,
        ) from None
    except ZeError as exc:
        _abort_runtime_handle(reservation)
        raise LegacyImportError(
            "legacy_import_runtime_failed", exc.code,
        ) from None
    except Exception:
        _abort_runtime_handle(reservation)
        raise LegacyImportError(
            "legacy_import_runtime_failed", "internal_failure",
        ) from None
    except BaseException:
        _abort_runtime_handle(reservation)
        raise


def _reserve_prepared_legacy_import(
    package: FrozenLegacyImport,
    receipt: PreparedLegacyPayloadReceipt,
    *,
    _attempt_token: Any,
    _capability: Any = None,
) -> tuple[_PreparedReservation, VerifiedLegacyImportPlan]:
    """Reserve one caller-token attempt and atomically issue its complete plan."""
    if _capability is not _LEGACY_IMPORT_ADAPTER_CAPABILITY:
        _fail("legacy_import_adapter_forbidden", "adapter_capability_required")
    if type(_attempt_token) is not object:
        _fail("legacy_import_payload_invalid", "adapter_attempt_invalid")
    reservation = _reserve_prepared_import(package, receipt, _attempt_token)
    try:
        plan = _verified_prepared_plan(reservation)
        return reservation, plan
    except LegacyImportError:
        _finish_prepared_import(reservation, outcome="quarantine")
        raise
    except Exception:
        _finish_prepared_import(reservation, outcome="quarantine")
        raise LegacyImportError(
            "legacy_import_payload_invalid", "internal_failure",
        ) from None
    except BaseException:
        _finish_prepared_import(reservation, outcome="release")
        raise


def _finish_prepared_legacy_import(
    reservation: _PreparedReservation,
    *,
    outcome: str,
    _capability: Any = None,
) -> None:
    """Resolve a lane; consumed itself is not a database commit receipt."""
    if _capability is not _LEGACY_IMPORT_ADAPTER_CAPABILITY:
        _fail("legacy_import_adapter_forbidden", "adapter_capability_required")
    _finish_prepared_import(reservation, outcome=outcome)


def _recover_prepared_legacy_import(
    package: FrozenLegacyImport,
    receipt: PreparedLegacyPayloadReceipt,
    *,
    _attempt_token: Any,
    _capability: Any = None,
) -> tuple[_PreparedReservation, VerifiedLegacyImportPlan]:
    """Recover an issued pair using the caller's exact pre-reserve token."""

    if _capability is not _LEGACY_IMPORT_ADAPTER_CAPABILITY:
        _fail("legacy_import_adapter_forbidden", "adapter_capability_required")
    return _recover_prepared_import(package, receipt, _attempt_token)


def _verify_prepared_legacy_import_plan_binding(
    reservation: _PreparedReservation,
    plan: VerifiedLegacyImportPlan,
    *,
    _capability: Any = None,
) -> None:
    """Fail closed unless the private SQLite bridge holds the exact active pair."""

    if _capability is not _LEGACY_IMPORT_ADAPTER_CAPABILITY:
        _fail("legacy_import_adapter_forbidden", "adapter_capability_required")
    _verify_prepared_plan_binding(reservation, plan)


__all__ = [
    "FrozenLegacyImport",
    "LegacyImportError",
    "LegacyScanHandle",
    "PreparedLegacyPayloadReceipt",
    "detach_legacy_runtime_head",
    "prepare_legacy_import",
    "scan_legacy_project",
]
