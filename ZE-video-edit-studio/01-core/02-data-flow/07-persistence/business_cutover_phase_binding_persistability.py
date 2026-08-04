"""Pure exact-row projection for current V13 cutover phase bindings.

The canonical binding manifest and the SQLite sidecar row are two different
representations of the same transition.  This module is their only forward
mapping: it first performs the strict issuer/integrity verification, then
projects every declared field into the exact table column order frozen by
schema V13.  It performs no I/O, imports no SQLite implementation, accepts no
connection or SQL callback, and grants no write authority.

The fresh semantic reader, test fixture and future fixed-SQL stage writers all
consume this representation so column order and value ownership cannot drift
independently between four stages.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Tuple

from business_cutover_phase_binding_contract import (
    MAX_BUSINESS_CUTOVER_PHASE_BINDING_MANIFEST_BYTES,
    BusinessCutoverAssetCoverageCheckpoint,
    BusinessCutoverPhaseBinding,
    BusinessCutoverPhaseBindingStage,
)
from business_cutover_phase_binding_integrity import (
    BusinessCutoverPhaseBindingIntegrityError,
    verify_business_cutover_phase_binding,
)


_COMMON_COLUMNS = (
    "cutover_id", "repository_epoch", "runtime_generation", "code_digest",
    "predecessor_state_revision", "predecessor_phase_revision",
    "predecessor_phase", "predecessor_authority_state",
    "predecessor_head_sha256", "predecessor_previous_head_sha256",
    "predecessor_updated_at", "successor_state_revision",
    "successor_phase_revision", "successor_phase",
    "successor_authority_state", "successor_previous_head_sha256",
    "successor_head_sha256", "successor_updated_at",
    "source_admission_revision", "source_admission_sha256",
    "source_freeze_sha256", "file_admission_state", "file_admission_epoch",
    "file_admission_revision", "file_admission_cutover_id",
    "file_admission_sha256", "maintenance_scope",
    "maintenance_repository_proof_sha256", "maintenance_observed_at",
    "runtime_authority", "runtime_authority_state",
    "runtime_repository_revision", "runtime_state_revision",
    "runtime_schema_version", "runtime_updated_at", "before_receipt_count",
    "before_receipt_root_sha256", "after_receipt_count",
    "after_receipt_root_sha256", "worker_head_state_revision",
    "worker_ledger_sequence", "worker_ledger_tail_sha256",
    "worker_head_updated_at", "allocation_head_state_revision",
    "allocation_sequence", "allocation_tail_sha256",
    "allocation_head_updated_at", "dispatch_head_state_revision",
    "dispatch_published_sequence", "dispatch_published_repository_revision",
    "dispatch_published_ordinal", "dispatch_inflight_sequence",
    "dispatch_inflight_repository_revision", "dispatch_inflight_ordinal",
    "dispatch_head_sha256", "dispatch_head_updated_at",
    "binding_manifest_bytes", "binding_manifest_sha256",
)

_COVERAGE_COLUMNS = (
    "coverage_transaction_id", "coverage_receipt_kind", "coverage_committed_at",
    "coverage_manifest_sha256", "coverage_sha256",
    "coverage_member_root_sha256", "coverage_blob_root_sha256",
    "coverage_batch_count", "coverage_repository_member_count",
    "coverage_live_project_count", "coverage_deleted_project_count",
    "coverage_observed_occurrence_count", "coverage_observed_row_count",
    "coverage_observed_manifest_bytes", "coverage_project_count",
    "coverage_reference_count", "coverage_unique_blob_count",
    "coverage_unique_blob_bytes", "coverage_referenced_bytes",
)

_ASSET_COLUMNS = _COMMON_COLUMNS + _COVERAGE_COLUMNS + (
    "provider_protocol", "provider_id", "provider_generation",
    "provider_checkpoint_sequence", "provider_checked_at",
    "provider_blob_count", "provider_blob_bytes", "provider_blob_root_sha256",
    "provider_receipt_schema", "provider_receipt_codec_id",
    "provider_receipt_codec_version", "provider_receipt_payload_bytes",
    "provider_receipt_payload_sha256", "provider_checkpoint_sha256",
)

_AUDIT_COLUMNS = _COMMON_COLUMNS + (
    "audit_certificate_id", "audit_certificate_sequence", "audit_purpose",
    "audit_transaction_id", "audit_receipt_kind", "audit_committed_at",
    "audit_previous_certificate_id", "audit_previous_manifest_sha256",
    "audit_previous_tail_sha256", "audit_manifest_sha256",
    "audit_certificate_tail_sha256", "audit_findings_sha256",
    "actual_schema_version", "actual_schema_fingerprint",
    "actual_migration_ledger_count", "actual_migration_ledger_sha256",
    "audited_install_manifest_sha256", "covered_repository_revision",
    "audit_started_at", "audit_completed_at", "page_integrity",
    "foreign_key_violation_count", "namespace_violation_count",
    "audit_head_state_revision", "audit_head_sequence",
    "audit_head_certificate_id", "audit_head_previous_tail_sha256",
    "audit_head_tail_sha256", "audit_head_updated_at",
)

_ACTIVATION_COLUMNS = _COMMON_COLUMNS + (
    "activation_transaction_id", "activation_receipt_kind",
    "activation_committed_at", "activation_manifest_sha256",
    "install_transaction_id", "install_receipt_kind", "install_manifest_sha256",
    "audit_certificate_id", "audit_certificate_sequence", "audit_transaction_id",
    "audit_receipt_kind", "audit_manifest_sha256",
    "audit_certificate_tail_sha256", "audit_completed_at",
    "expected_migration_ledger_count", "expected_migration_ledger_sha256",
    "actual_schema_version", "actual_schema_fingerprint",
    "final_admission_state", "final_admission_epoch",
    "final_admission_revision", "final_admission_cutover_id",
    "final_admission_sha256", "before_runtime_repository_revision",
    "before_runtime_state_revision", "before_runtime_authority_state",
    "before_runtime_updated_at", "after_runtime_repository_revision",
    "after_runtime_state_revision", "after_runtime_authority_state",
    "after_runtime_updated_at",
)

_MOUNT_COLUMNS = _COMMON_COLUMNS + (
    "mount_transaction_id", "mount_receipt_kind", "mount_committed_at",
    "mount_manifest_sha256", "mount_source_admission_sha256",
    "mount_final_admission_sha256", "install_transaction_id",
    "install_receipt_kind", "install_committed_at", "install_manifest_sha256",
) + _COVERAGE_COLUMNS + (
    "activation_transaction_id", "activation_receipt_kind",
    "activation_committed_at", "activation_manifest_sha256",
    "audit_certificate_id", "audit_certificate_sequence", "audit_transaction_id",
    "audit_receipt_kind", "audit_committed_at", "audit_manifest_sha256",
    "target_schema_version", "target_schema_fingerprint",
)


class BusinessCutoverPhaseBindingPersistabilityError(ValueError):
    """Stable payload-free failure at the V13 row projection boundary."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        if type(code) is not str or not code:
            raise TypeError("code must be a non-empty exact string")
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise BusinessCutoverPhaseBindingPersistabilityError(code)


@dataclass(frozen=True)
class BusinessCutoverPhaseBindingLayout:
    """One frozen V13 table identity and its exact declared column order."""

    stage: BusinessCutoverPhaseBindingStage
    table: str
    columns: Tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.stage) is not BusinessCutoverPhaseBindingStage:
            raise TypeError("stage must be an exact phase-binding stage")
        if (
            type(self.table) is not str
            or not self.table
            or not self.table.replace("_", "").isalnum()
        ):
            raise TypeError("table must be one exact private table name")
        if (
            type(self.columns) is not tuple
            or not self.columns
            or any(type(value) is not str or not value for value in self.columns)
            or len(set(self.columns)) != len(self.columns)
        ):
            raise TypeError("columns must be one unique non-empty exact tuple")


_BUSINESS_CUTOVER_PHASE_BINDING_LAYOUTS = (
    BusinessCutoverPhaseBindingLayout(
        BusinessCutoverPhaseBindingStage.ASSET_PROVIDER,
        "business_cutover_asset_provider_bindings",
        _ASSET_COLUMNS,
    ),
    BusinessCutoverPhaseBindingLayout(
        BusinessCutoverPhaseBindingStage.AUDIT,
        "business_cutover_audit_bindings",
        _AUDIT_COLUMNS,
    ),
    BusinessCutoverPhaseBindingLayout(
        BusinessCutoverPhaseBindingStage.ACTIVATION,
        "business_cutover_activation_bindings",
        _ACTIVATION_COLUMNS,
    ),
    BusinessCutoverPhaseBindingLayout(
        BusinessCutoverPhaseBindingStage.MOUNT,
        "business_cutover_mount_bindings",
        _MOUNT_COLUMNS,
    ),
)


def _business_cutover_phase_binding_layout(
    stage: BusinessCutoverPhaseBindingStage,
) -> BusinessCutoverPhaseBindingLayout:
    if type(stage) is not BusinessCutoverPhaseBindingStage:
        _fail("business_cutover_phase_binding_persistability_stage_invalid")
    matches = tuple(
        layout
        for layout in _BUSINESS_CUTOVER_PHASE_BINDING_LAYOUTS
        if layout.stage is stage
    )
    if len(matches) != 1:
        _fail("business_cutover_phase_binding_persistability_layout_invalid")
    return matches[0]


@dataclass(frozen=True)
class BusinessCutoverPhaseBindingPersistedRow:
    """Immutable exact row values; ``as_mapping`` returns a detached copy."""

    stage: BusinessCutoverPhaseBindingStage
    table: str
    columns: Tuple[str, ...]
    values: Tuple[Any, ...]

    def __post_init__(self) -> None:
        layout = _business_cutover_phase_binding_layout(self.stage)
        if self.table != layout.table or self.columns != layout.columns:
            raise ValueError("persisted phase-binding row layout is invalid")
        if type(self.values) is not tuple or len(self.values) != len(self.columns):
            raise TypeError("values must match the exact persisted column tuple")
        if any(
            value is not None and type(value) not in (bytes, int, str)
            for value in self.values
        ):
            raise TypeError("persisted values must be exact SQLite scalar types")

    def as_mapping(self) -> dict[str, Any]:
        return dict(zip(self.columns, self.values))


def _common_values(binding: BusinessCutoverPhaseBinding) -> dict[str, Any]:
    projection = binding.projection
    identity = projection.identity
    old_head = projection.old_head
    new_head = projection.new_head
    admission = projection.admission
    maintenance = projection.maintenance
    checkpoint = projection.checkpoint
    runtime = checkpoint.runtime
    worker = checkpoint.worker
    allocation = checkpoint.allocation
    dispatch = checkpoint.dispatch
    return {
        "cutover_id": identity.cutover_id,
        "repository_epoch": identity.repository_epoch,
        "runtime_generation": identity.runtime_generation,
        "code_digest": identity.code_digest,
        "predecessor_state_revision": old_head.state_revision,
        "predecessor_phase_revision": old_head.phase_revision,
        "predecessor_phase": old_head.phase,
        "predecessor_authority_state": old_head.authority_state,
        "predecessor_head_sha256": old_head.head_sha256,
        "predecessor_previous_head_sha256": old_head.previous_head_sha256,
        "predecessor_updated_at": old_head.updated_at,
        "successor_state_revision": new_head.state_revision,
        "successor_phase_revision": new_head.phase_revision,
        "successor_phase": new_head.phase,
        "successor_authority_state": new_head.authority_state,
        "successor_previous_head_sha256": new_head.previous_head_sha256,
        "successor_head_sha256": new_head.head_sha256,
        "successor_updated_at": new_head.updated_at,
        "source_admission_revision": admission.source_revision,
        "source_admission_sha256": admission.source_admission_sha256,
        "source_freeze_sha256": admission.source_freeze_sha256,
        "file_admission_state": admission.file_authority_state,
        "file_admission_epoch": admission.file_authority_epoch,
        "file_admission_revision": admission.file_revision,
        "file_admission_cutover_id": admission.file_cutover_id,
        "file_admission_sha256": admission.file_admission_sha256,
        "maintenance_scope": maintenance.scope,
        "maintenance_repository_proof_sha256": maintenance.proof_sha256,
        "maintenance_observed_at": maintenance.observed_at,
        "runtime_authority": runtime.authority,
        "runtime_authority_state": runtime.authority_state,
        "runtime_repository_revision": runtime.repository_revision,
        "runtime_state_revision": runtime.state_revision,
        "runtime_schema_version": runtime.schema_version,
        "runtime_updated_at": runtime.updated_at,
        "before_receipt_count": checkpoint.receipt_before_count,
        "before_receipt_root_sha256": checkpoint.receipt_before_sha256,
        "after_receipt_count": checkpoint.receipt_after_count,
        "after_receipt_root_sha256": checkpoint.receipt_after_sha256,
        "worker_head_state_revision": worker.state_revision,
        "worker_ledger_sequence": worker.sequence,
        "worker_ledger_tail_sha256": worker.tail_sha256,
        "worker_head_updated_at": worker.updated_at,
        "allocation_head_state_revision": allocation.state_revision,
        "allocation_sequence": allocation.sequence,
        "allocation_tail_sha256": allocation.tail_sha256,
        "allocation_head_updated_at": allocation.updated_at,
        "dispatch_head_state_revision": dispatch.state_revision,
        "dispatch_published_sequence": dispatch.published_sequence,
        "dispatch_published_repository_revision": (
            dispatch.published_repository_revision
        ),
        "dispatch_published_ordinal": dispatch.published_ordinal,
        "dispatch_inflight_sequence": dispatch.inflight_sequence,
        "dispatch_inflight_repository_revision": (
            dispatch.inflight_repository_revision
        ),
        "dispatch_inflight_ordinal": dispatch.inflight_ordinal,
        "dispatch_head_sha256": dispatch.head_sha256,
        "dispatch_head_updated_at": dispatch.updated_at,
        "binding_manifest_bytes": binding.manifest_bytes,
        "binding_manifest_sha256": binding.manifest_sha256,
    }


def _coverage_values(
    coverage: BusinessCutoverAssetCoverageCheckpoint,
) -> dict[str, Any]:
    if type(coverage) is not BusinessCutoverAssetCoverageCheckpoint:
        _fail("business_cutover_phase_binding_persistability_coverage_invalid")
    pointer = coverage.pointer
    return {
        "coverage_transaction_id": pointer.transaction_id,
        "coverage_receipt_kind": pointer.receipt_kind,
        "coverage_committed_at": pointer.committed_at,
        "coverage_manifest_sha256": pointer.manifest_sha256,
        "coverage_sha256": coverage.coverage_sha256,
        "coverage_member_root_sha256": coverage.member_root_sha256,
        "coverage_blob_root_sha256": coverage.blob_root_sha256,
        "coverage_batch_count": coverage.batch_count,
        "coverage_repository_member_count": coverage.repository_member_count,
        "coverage_live_project_count": coverage.live_project_count,
        "coverage_deleted_project_count": coverage.deleted_project_count,
        "coverage_observed_occurrence_count": coverage.observed_occurrence_count,
        "coverage_observed_row_count": coverage.observed_row_count,
        "coverage_observed_manifest_bytes": coverage.observed_manifest_bytes,
        "coverage_project_count": coverage.project_count,
        "coverage_reference_count": coverage.reference_count,
        "coverage_unique_blob_count": coverage.unique_blob_count,
        "coverage_unique_blob_bytes": coverage.unique_blob_bytes,
        "coverage_referenced_bytes": coverage.referenced_bytes,
    }


def _specialised_values(binding: BusinessCutoverPhaseBinding) -> dict[str, Any]:
    stage = binding.projection.stage
    evidence = binding.projection.direct_evidence
    if stage is BusinessCutoverPhaseBindingStage.ASSET_PROVIDER:
        values = _coverage_values(evidence.coverage)
        values.update({
            "provider_protocol": evidence.provider_protocol,
            "provider_id": evidence.provider_id,
            "provider_generation": evidence.provider_generation,
            "provider_checkpoint_sequence": evidence.provider_sequence,
            "provider_checked_at": evidence.checked_at,
            "provider_blob_count": evidence.object_count,
            "provider_blob_bytes": evidence.total_bytes,
            "provider_blob_root_sha256": evidence.blob_root_sha256,
            "provider_receipt_schema": evidence.receipt_schema,
            "provider_receipt_codec_id": evidence.receipt_codec,
            "provider_receipt_codec_version": evidence.receipt_version,
            "provider_receipt_payload_bytes": evidence.receipt_payload,
            "provider_receipt_payload_sha256": evidence.receipt_payload_sha256,
            "provider_checkpoint_sha256": evidence.checkpoint_sha256,
        })
        return values
    if stage is BusinessCutoverPhaseBindingStage.AUDIT:
        receipt = evidence.receipt_parent
        return {
            "audit_certificate_id": evidence.certificate_id,
            "audit_certificate_sequence": evidence.certificate_sequence,
            "audit_purpose": evidence.purpose,
            "audit_transaction_id": receipt.transaction_id,
            "audit_receipt_kind": receipt.receipt_kind,
            "audit_committed_at": receipt.committed_at,
            "audit_previous_certificate_id": evidence.previous_certificate_id,
            "audit_previous_manifest_sha256": evidence.previous_manifest_sha256,
            "audit_previous_tail_sha256": evidence.previous_tail_sha256,
            "audit_manifest_sha256": receipt.manifest_sha256,
            "audit_certificate_tail_sha256": evidence.certificate_tail_sha256,
            "audit_findings_sha256": evidence.findings_sha256,
            "actual_schema_version": evidence.schema_version,
            "actual_schema_fingerprint": evidence.schema_fingerprint,
            "actual_migration_ledger_count": evidence.migration_ledger_count,
            "actual_migration_ledger_sha256": evidence.migration_ledger_sha256,
            "audited_install_manifest_sha256": evidence.install_manifest_sha256,
            "covered_repository_revision": evidence.covered_business_revision,
            "audit_started_at": evidence.started_at,
            "audit_completed_at": evidence.completed_at,
            "page_integrity": evidence.page_integrity,
            "foreign_key_violation_count": evidence.foreign_key_violation_count,
            "namespace_violation_count": evidence.namespace_violation_count,
            "audit_head_state_revision": evidence.audit_head_state_revision,
            "audit_head_sequence": evidence.audit_head_sequence,
            "audit_head_certificate_id": evidence.audit_head_certificate_id,
            "audit_head_previous_tail_sha256": (
                evidence.audit_head_previous_tail_sha256
            ),
            "audit_head_tail_sha256": evidence.audit_head_tail_sha256,
            "audit_head_updated_at": evidence.audit_head_updated_at,
        }
    if stage is BusinessCutoverPhaseBindingStage.ACTIVATION:
        activation = evidence.activation
        install = evidence.install
        audit = evidence.audit
        before_runtime = evidence.before_runtime
        after_runtime = evidence.after_runtime
        return {
            "activation_transaction_id": activation.transaction_id,
            "activation_receipt_kind": activation.receipt_kind,
            "activation_committed_at": activation.committed_at,
            "activation_manifest_sha256": activation.manifest_sha256,
            "install_transaction_id": install.transaction_id,
            "install_receipt_kind": install.receipt_kind,
            "install_manifest_sha256": install.manifest_sha256,
            "audit_certificate_id": evidence.audit_certificate_id,
            "audit_certificate_sequence": evidence.audit_certificate_sequence,
            "audit_transaction_id": audit.transaction_id,
            "audit_receipt_kind": audit.receipt_kind,
            "audit_manifest_sha256": audit.manifest_sha256,
            "audit_certificate_tail_sha256": (
                evidence.audit_certificate_tail_sha256
            ),
            "audit_completed_at": evidence.audit_completed_at,
            "expected_migration_ledger_count": (
                evidence.expected_migration_ledger_count
            ),
            "expected_migration_ledger_sha256": (
                evidence.expected_migration_ledger_sha256
            ),
            "actual_schema_version": evidence.actual_schema_version,
            "actual_schema_fingerprint": evidence.actual_schema_fingerprint,
            "final_admission_state": evidence.final_admission_state,
            "final_admission_epoch": evidence.final_admission_epoch,
            "final_admission_revision": evidence.final_admission_revision,
            "final_admission_cutover_id": evidence.final_admission_cutover_id,
            "final_admission_sha256": evidence.final_admission_sha256,
            "before_runtime_repository_revision": before_runtime.repository_revision,
            "before_runtime_state_revision": before_runtime.state_revision,
            "before_runtime_authority_state": before_runtime.authority_state,
            "before_runtime_updated_at": before_runtime.updated_at,
            "after_runtime_repository_revision": after_runtime.repository_revision,
            "after_runtime_state_revision": after_runtime.state_revision,
            "after_runtime_authority_state": after_runtime.authority_state,
            "after_runtime_updated_at": after_runtime.updated_at,
        }
    if stage is not BusinessCutoverPhaseBindingStage.MOUNT:
        _fail("business_cutover_phase_binding_persistability_stage_invalid")
    mount = evidence.mount
    install = evidence.install
    activation = evidence.activation
    audit = evidence.audit
    values = {
        "mount_transaction_id": mount.transaction_id,
        "mount_receipt_kind": mount.receipt_kind,
        "mount_committed_at": mount.committed_at,
        "mount_manifest_sha256": mount.manifest_sha256,
        "mount_source_admission_sha256": evidence.source_admission_sha256,
        "mount_final_admission_sha256": evidence.final_admission_sha256,
        "install_transaction_id": install.transaction_id,
        "install_receipt_kind": install.receipt_kind,
        "install_committed_at": install.committed_at,
        "install_manifest_sha256": install.manifest_sha256,
    }
    values.update(_coverage_values(evidence.coverage))
    values.update({
        "activation_transaction_id": activation.transaction_id,
        "activation_receipt_kind": activation.receipt_kind,
        "activation_committed_at": activation.committed_at,
        "activation_manifest_sha256": activation.manifest_sha256,
        "audit_certificate_id": evidence.audit_certificate_id,
        "audit_certificate_sequence": evidence.audit_certificate_sequence,
        "audit_transaction_id": audit.transaction_id,
        "audit_receipt_kind": audit.receipt_kind,
        "audit_committed_at": audit.committed_at,
        "audit_manifest_sha256": audit.manifest_sha256,
        "target_schema_version": evidence.target_schema_version,
        "target_schema_fingerprint": evidence.target_schema_fingerprint,
    })
    return values


def _project_business_cutover_phase_binding_row(
    value: Any,
) -> BusinessCutoverPhaseBindingPersistedRow:
    """Strictly verify and project one canonical binding into its V13 row."""

    try:
        binding = verify_business_cutover_phase_binding(value)
    except (BusinessCutoverPhaseBindingIntegrityError, TypeError) as exc:
        raise BusinessCutoverPhaseBindingPersistabilityError(
            "business_cutover_phase_binding_persistability_binding_invalid"
        ) from exc
    if not (
        1 <= len(binding.manifest_bytes)
        <= MAX_BUSINESS_CUTOVER_PHASE_BINDING_MANIFEST_BYTES
    ):
        _fail("business_cutover_phase_binding_persistability_budget_exceeded")
    layout = _business_cutover_phase_binding_layout(binding.projection.stage)
    values_by_column = _common_values(binding)
    values_by_column.update(_specialised_values(binding))
    if set(values_by_column) != set(layout.columns):
        _fail("business_cutover_phase_binding_persistability_columns_invalid")
    return BusinessCutoverPhaseBindingPersistedRow(
        stage=layout.stage,
        table=layout.table,
        columns=layout.columns,
        values=tuple(values_by_column[column] for column in layout.columns),
    )


__all__ = ()
