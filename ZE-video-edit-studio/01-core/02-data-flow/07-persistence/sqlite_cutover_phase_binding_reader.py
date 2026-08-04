"""Fresh semantic reader for the current V13 cutover phase-binding prefix.

The V13 tables are immutable witnesses, not opaque authorisation flags.  This
reader reconstructs each pure binding from every stored sidecar column,
reissues its canonical manifest, checks the independent durable evidence and
then proves that the resulting prefix ends at the current cutover head.  It
performs no writes and expects its caller to hold one read transaction for the
whole call.

The returned capability is private to the SQLite production boundary.  It is
not an SDK model and loose dictionaries cannot counterfeit it.
"""

from __future__ import annotations

import hmac
import sqlite3
from typing import Any, Mapping, Tuple

from business_cutover_phase_binding_contract import (
    BusinessCutoverActivationEvidence,
    BusinessCutoverAdmissionCheckpoint,
    BusinessCutoverAllocationHighWater,
    BusinessCutoverAssetCoverageCheckpoint,
    BusinessCutoverAssetProviderEvidence,
    BusinessCutoverAuditEvidence,
    BusinessCutoverDispatchHighWater,
    BusinessCutoverDurableCheckpoint,
    BusinessCutoverMaintenanceCheckpoint,
    BusinessCutoverMountEvidence,
    BusinessCutoverPhaseBinding,
    BusinessCutoverPhaseBindingContractError,
    BusinessCutoverPhaseBindingHead,
    BusinessCutoverPhaseBindingIdentity,
    BusinessCutoverPhaseBindingProjection,
    BusinessCutoverPhaseBindingStage,
    BusinessCutoverReceiptPointer,
    BusinessCutoverRuntimeImage,
    BusinessCutoverWorkerHighWater,
    _head_is_canonical,
)
from business_cutover_phase_binding_integrity import (
    BusinessCutoverPhaseBindingIntegrityError,
    _verify_business_cutover_phase_binding_chain,
    issue_business_cutover_phase_binding,
    verify_business_cutover_phase_binding,
)
from business_cutover_phase_binding_persistability import (
    BusinessCutoverPhaseBindingPersistabilityError,
    _BUSINESS_CUTOVER_PHASE_BINDING_LAYOUTS,
    _business_cutover_phase_binding_layout,
    _project_business_cutover_phase_binding_row,
)


_READER_ISSUER = object()

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

_STAGES = tuple(
    (layout.stage, layout.table, layout.columns, successor_revision)
    for successor_revision, layout in enumerate(
        _BUSINESS_CUTOVER_PHASE_BINDING_LAYOUTS,
        start=2,
    )
)

_INSTALL_COLUMNS = (
    "cutover_id", "transaction_id", "receipt_kind", "committed_at",
    "repository_epoch", "runtime_generation", "code_digest",
    "member_mapping_sha256", "manifest_bytes", "manifest_sha256",
)
_SOURCE_COVERAGE_COLUMNS = (
    "cutover_id", "transaction_id", "receipt_kind", "committed_at",
    "repository_epoch", "source_admission_revision",
    "source_admission_sha256", "batch_count", "member_root_sha256",
    "repository_member_count", "live_project_count", "deleted_project_count",
    "observed_occurrence_count", "observed_row_count",
    "observed_manifest_bytes", "project_count", "reference_count",
    "unique_blob_count", "unique_blob_bytes", "referenced_bytes",
    "project_root_sha256", "blob_root_sha256", "occurrence_root_sha256",
    "coverage_sha256", "manifest_bytes", "manifest_sha256",
)
_CERTIFICATE_COLUMNS = (
    "certificate_id", "cutover_id", "sequence", "purpose", "transaction_id",
    "receipt_kind", "committed_at", "previous_certificate_id",
    "previous_manifest_sha256", "previous_tail_sha256", "schema_version",
    "migration_ledger_count", "migration_ledger_sha256", "receipt_count",
    "receipt_root_sha256", "worker_ledger_sequence",
    "worker_ledger_tail_sha256", "findings_sha256", "manifest_bytes",
    "manifest_sha256", "certificate_tail_sha256",
)
_ACTIVATION_RECEIPT_COLUMNS = (
    "cutover_id", "transaction_id", "receipt_kind", "committed_at",
    "install_transaction_id", "install_manifest_sha256",
    "audit_transaction_id", "audit_manifest_sha256",
    "migration_ledger_sha256", "manifest_bytes", "manifest_sha256",
)
_MOUNT_RECEIPT_COLUMNS = (
    "cutover_id", "transaction_id", "receipt_kind", "committed_at",
    "install_transaction_id", "install_receipt_kind", "install_committed_at",
    "install_manifest_sha256", "coverage_transaction_id",
    "coverage_receipt_kind", "coverage_committed_at",
    "coverage_manifest_sha256", "audit_transaction_id",
    "audit_manifest_sha256", "activation_transaction_id",
    "activation_manifest_sha256", "repository_epoch", "runtime_generation",
    "code_digest", "manifest_bytes", "manifest_sha256",
)


class SQLiteCutoverPhaseBindingReaderError(ValueError):
    """Stable payload-free failure for a non-canonical V13 prefix."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        if type(code) is not str or not code:
            raise TypeError("code must be a non-empty exact string")
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise SQLiteCutoverPhaseBindingReaderError(code)


def _same(left: Any, right: Any) -> bool:
    if type(left) is bytes and type(right) is bytes:
        return hmac.compare_digest(left, right)
    return type(left) is type(right) and left == right


def _row(values: Any, columns: Tuple[str, ...], code: str) -> Mapping[str, Any]:
    if values is None or len(values) != len(columns):
        _fail(code)
    return dict(zip(columns, tuple(values)))


def _select_one(
    connection: Any,
    table: str,
    columns: Tuple[str, ...],
    where: str,
    parameters: tuple[Any, ...],
    code: str,
) -> Mapping[str, Any]:
    rows = connection.execute(
        "SELECT " + ",".join(columns) + " FROM " + table + " WHERE " + where,
        parameters,
    ).fetchall()
    if len(rows) != 1:
        _fail(code)
    return _row(rows[0], columns, code)


def _binding_rows(
    connection: Any,
    table: str,
    columns: Tuple[str, ...],
    expected_count: int,
) -> tuple[Mapping[str, Any], ...]:
    actual_columns = tuple(
        item[1] for item in connection.execute(
            "PRAGMA table_info(" + table + ")"
        ).fetchall()
    )
    if actual_columns != columns:
        _fail("cutover_phase_binding_schema_invalid")
    rows = connection.execute(
        "SELECT " + ",".join(columns) + " FROM " + table
    ).fetchall()
    if len(rows) != expected_count:
        _fail("cutover_phase_binding_prefix_shape_invalid")
    return tuple(
        _row(item, columns, "cutover_phase_binding_row_invalid")
        for item in rows
    )


def _pointer(
    transaction_id: Any,
    receipt_kind: Any,
    committed_at: Any,
    manifest_sha256: Any,
) -> BusinessCutoverReceiptPointer:
    return BusinessCutoverReceiptPointer(
        transaction_id=transaction_id,
        receipt_kind=receipt_kind,
        committed_at=committed_at,
        manifest_sha256=manifest_sha256,
    )


def _coverage(row: Mapping[str, Any]) -> BusinessCutoverAssetCoverageCheckpoint:
    return BusinessCutoverAssetCoverageCheckpoint(
        pointer=_pointer(
            row["coverage_transaction_id"],
            row["coverage_receipt_kind"],
            row["coverage_committed_at"],
            row["coverage_manifest_sha256"],
        ),
        coverage_sha256=row["coverage_sha256"],
        member_root_sha256=row["coverage_member_root_sha256"],
        blob_root_sha256=row["coverage_blob_root_sha256"],
        batch_count=row["coverage_batch_count"],
        repository_member_count=row["coverage_repository_member_count"],
        live_project_count=row["coverage_live_project_count"],
        deleted_project_count=row["coverage_deleted_project_count"],
        project_count=row["coverage_project_count"],
        reference_count=row["coverage_reference_count"],
        unique_blob_count=row["coverage_unique_blob_count"],
        unique_blob_bytes=row["coverage_unique_blob_bytes"],
        referenced_bytes=row["coverage_referenced_bytes"],
        observed_occurrence_count=row["coverage_observed_occurrence_count"],
        observed_row_count=row["coverage_observed_row_count"],
        observed_manifest_bytes=row["coverage_observed_manifest_bytes"],
    )


def _runtime(
    row: Mapping[str, Any],
    *,
    repository_revision: str = "runtime_repository_revision",
    state_revision: str = "runtime_state_revision",
    authority_state: str = "runtime_authority_state",
    updated_at: str = "runtime_updated_at",
) -> BusinessCutoverRuntimeImage:
    return BusinessCutoverRuntimeImage(
        authority=row["runtime_authority"],
        authority_state=row[authority_state],
        source_admission_revision=row["source_admission_revision"],
        repository_revision=row[repository_revision],
        state_revision=row[state_revision],
        schema_version=row["runtime_schema_version"],
        updated_at=row[updated_at],
    )


def _checkpoint(row: Mapping[str, Any]) -> BusinessCutoverDurableCheckpoint:
    return BusinessCutoverDurableCheckpoint(
        runtime=_runtime(row),
        receipt_before_count=row["before_receipt_count"],
        receipt_before_sha256=row["before_receipt_root_sha256"],
        receipt_after_count=row["after_receipt_count"],
        receipt_after_sha256=row["after_receipt_root_sha256"],
        worker=BusinessCutoverWorkerHighWater(
            row["worker_head_state_revision"],
            row["worker_ledger_sequence"],
            row["worker_ledger_tail_sha256"],
            row["worker_head_updated_at"],
        ),
        allocation=BusinessCutoverAllocationHighWater(
            row["allocation_head_state_revision"],
            row["allocation_sequence"],
            row["allocation_tail_sha256"],
            row["allocation_head_updated_at"],
        ),
        dispatch=BusinessCutoverDispatchHighWater(
            row["dispatch_head_state_revision"],
            row["dispatch_published_sequence"],
            row["dispatch_published_repository_revision"],
            row["dispatch_published_ordinal"],
            row["dispatch_inflight_sequence"],
            row["dispatch_inflight_repository_revision"],
            row["dispatch_inflight_ordinal"],
            row["dispatch_head_sha256"],
            row["dispatch_head_updated_at"],
        ),
    )


def _binding_head(
    row: Mapping[str, Any],
    prefix: str,
    pointers: Mapping[str, BusinessCutoverReceiptPointer],
) -> BusinessCutoverPhaseBindingHead:
    revision = row[prefix + "_phase_revision"]
    return BusinessCutoverPhaseBindingHead(
        state_revision=row[prefix + "_state_revision"],
        phase_revision=revision,
        phase=row[prefix + "_phase"],
        authority_state=row[prefix + "_authority_state"],
        head_sha256=row[prefix + "_head_sha256"],
        updated_at=row[prefix + "_updated_at"],
        previous_head_sha256=row[prefix + "_previous_head_sha256"],
        install=pointers["install"],
        coverage=pointers.get("coverage") if revision >= 2 else None,
        audit=pointers.get("audit") if revision >= 3 else None,
        activation=pointers.get("activation") if revision >= 4 else None,
        mount=pointers.get("mount") if revision >= 5 else None,
    )


def _direct_evidence(
    stage: BusinessCutoverPhaseBindingStage,
    row: Mapping[str, Any],
    prior_evidence: Mapping[BusinessCutoverPhaseBindingStage, Any],
) -> Any:
    if stage is BusinessCutoverPhaseBindingStage.ASSET_PROVIDER:
        return BusinessCutoverAssetProviderEvidence(
            coverage=_coverage(row),
            provider_protocol=row["provider_protocol"],
            provider_id=row["provider_id"],
            provider_generation=row["provider_generation"],
            provider_sequence=row["provider_checkpoint_sequence"],
            checked_at=row["provider_checked_at"],
            object_count=row["provider_blob_count"],
            total_bytes=row["provider_blob_bytes"],
            blob_root_sha256=row["provider_blob_root_sha256"],
            receipt_schema=row["provider_receipt_schema"],
            receipt_codec=row["provider_receipt_codec_id"],
            receipt_version=row["provider_receipt_codec_version"],
            receipt_payload=row["provider_receipt_payload_bytes"],
            receipt_payload_sha256=row["provider_receipt_payload_sha256"],
            checkpoint_sha256=row["provider_checkpoint_sha256"],
        )
    if stage is BusinessCutoverPhaseBindingStage.AUDIT:
        return BusinessCutoverAuditEvidence(
            certificate_id=row["audit_certificate_id"],
            certificate_sequence=row["audit_certificate_sequence"],
            purpose=row["audit_purpose"],
            receipt_parent=_pointer(
                row["audit_transaction_id"], row["audit_receipt_kind"],
                row["audit_committed_at"], row["audit_manifest_sha256"],
            ),
            previous_certificate_id=row["audit_previous_certificate_id"],
            previous_manifest_sha256=row["audit_previous_manifest_sha256"],
            previous_tail_sha256=row["audit_previous_tail_sha256"],
            certificate_tail_sha256=row["audit_certificate_tail_sha256"],
            findings_sha256=row["audit_findings_sha256"],
            schema_version=row["actual_schema_version"],
            schema_fingerprint=row["actual_schema_fingerprint"],
            migration_ledger_count=row["actual_migration_ledger_count"],
            migration_ledger_sha256=row["actual_migration_ledger_sha256"],
            install_manifest_sha256=row["audited_install_manifest_sha256"],
            covered_business_revision=row["covered_repository_revision"],
            started_at=row["audit_started_at"],
            completed_at=row["audit_completed_at"],
            page_integrity=row["page_integrity"],
            foreign_key_violation_count=row["foreign_key_violation_count"],
            namespace_violation_count=row["namespace_violation_count"],
            audit_head_state_revision=row["audit_head_state_revision"],
            audit_head_sequence=row["audit_head_sequence"],
            audit_head_certificate_id=row["audit_head_certificate_id"],
            audit_head_previous_tail_sha256=row["audit_head_previous_tail_sha256"],
            audit_head_tail_sha256=row["audit_head_tail_sha256"],
            audit_head_updated_at=row["audit_head_updated_at"],
        )
    if stage is BusinessCutoverPhaseBindingStage.ACTIVATION:
        audit_binding = prior_evidence.get(
            BusinessCutoverPhaseBindingStage.AUDIT
        )
        if type(audit_binding) is not BusinessCutoverAuditEvidence:
            _fail("cutover_phase_binding_audit_prefix_invalid")
        return BusinessCutoverActivationEvidence(
            activation=_pointer(
                row["activation_transaction_id"], row["activation_receipt_kind"],
                row["activation_committed_at"], row["activation_manifest_sha256"],
            ),
            install=_pointer(
                row["install_transaction_id"], row["install_receipt_kind"],
                _install_committed_at(row), row["install_manifest_sha256"],
            ),
            audit_certificate_id=row["audit_certificate_id"],
            audit_certificate_sequence=row["audit_certificate_sequence"],
            audit=_pointer(
                row["audit_transaction_id"], row["audit_receipt_kind"],
                row["audit_completed_at"], row["audit_manifest_sha256"],
            ),
            audit_binding=audit_binding,
            audit_certificate_tail_sha256=row["audit_certificate_tail_sha256"],
            audit_completed_at=row["audit_completed_at"],
            expected_migration_ledger_count=row["expected_migration_ledger_count"],
            expected_migration_ledger_sha256=row["expected_migration_ledger_sha256"],
            actual_schema_version=row["actual_schema_version"],
            actual_schema_fingerprint=row["actual_schema_fingerprint"],
            final_admission_state=row["final_admission_state"],
            final_admission_epoch=row["final_admission_epoch"],
            final_admission_revision=row["final_admission_revision"],
            final_admission_cutover_id=row["final_admission_cutover_id"],
            final_admission_sha256=row["final_admission_sha256"],
            before_runtime=_runtime(
                row,
                repository_revision="before_runtime_repository_revision",
                state_revision="before_runtime_state_revision",
                authority_state="before_runtime_authority_state",
                updated_at="before_runtime_updated_at",
            ),
            after_runtime=_runtime(
                row,
                repository_revision="after_runtime_repository_revision",
                state_revision="after_runtime_state_revision",
                authority_state="after_runtime_authority_state",
                updated_at="after_runtime_updated_at",
            ),
        )
    activation_binding = prior_evidence.get(
        BusinessCutoverPhaseBindingStage.ACTIVATION
    )
    if type(activation_binding) is not BusinessCutoverActivationEvidence:
        _fail("cutover_phase_binding_activation_prefix_invalid")
    return BusinessCutoverMountEvidence(
        mount=_pointer(
            row["mount_transaction_id"], row["mount_receipt_kind"],
            row["mount_committed_at"], row["mount_manifest_sha256"],
        ),
        source_admission_sha256=row["mount_source_admission_sha256"],
        final_admission_sha256=row["mount_final_admission_sha256"],
        install=_pointer(
            row["install_transaction_id"], row["install_receipt_kind"],
            row["install_committed_at"], row["install_manifest_sha256"],
        ),
        coverage=_coverage(row),
        activation=_pointer(
            row["activation_transaction_id"], row["activation_receipt_kind"],
            row["activation_committed_at"], row["activation_manifest_sha256"],
        ),
        activation_binding=activation_binding,
        audit_certificate_id=row["audit_certificate_id"],
        audit_certificate_sequence=row["audit_certificate_sequence"],
        audit=_pointer(
            row["audit_transaction_id"], row["audit_receipt_kind"],
            row["audit_committed_at"], row["audit_manifest_sha256"],
        ),
        audit_certificate_tail_sha256=_certificate_tail(row),
        audit_completed_at=row["audit_committed_at"],
        activated_at=row["activation_committed_at"],
        target_schema_version=row["target_schema_version"],
        target_schema_fingerprint=row["target_schema_fingerprint"],
    )


def _install_committed_at(row: Mapping[str, Any]) -> Any:
    value = row.get("_install_committed_at")
    if value is None:
        _fail("cutover_phase_binding_install_pointer_invalid")
    return value


def _certificate_tail(row: Mapping[str, Any]) -> Any:
    value = row.get("_audit_certificate_tail_sha256")
    if value is None:
        _fail("cutover_phase_binding_audit_pointer_invalid")
    return value


def _matches(
    binding: Mapping[str, Any],
    source: Mapping[str, Any],
    pairs: Tuple[tuple[str, str], ...],
) -> bool:
    return all(_same(binding[left], source[right]) for left, right in pairs)


def _require_direct_source(
    connection: Any,
    stage: BusinessCutoverPhaseBindingStage,
    row: Mapping[str, Any],
) -> None:
    if stage is BusinessCutoverPhaseBindingStage.ASSET_PROVIDER:
        source = _select_one(
            connection, "business_asset_coverage_receipts",
            _SOURCE_COVERAGE_COLUMNS, "cutover_id=? AND transaction_id=?",
            (row["cutover_id"], row["coverage_transaction_id"]),
            "cutover_phase_binding_coverage_source_invalid",
        )
        pairs = (
            ("cutover_id", "cutover_id"),
            ("coverage_transaction_id", "transaction_id"),
            ("coverage_receipt_kind", "receipt_kind"),
            ("coverage_committed_at", "committed_at"),
            ("repository_epoch", "repository_epoch"),
            ("source_admission_revision", "source_admission_revision"),
            ("source_admission_sha256", "source_admission_sha256"),
            ("coverage_batch_count", "batch_count"),
            ("coverage_member_root_sha256", "member_root_sha256"),
            ("coverage_blob_root_sha256", "blob_root_sha256"),
            ("coverage_repository_member_count", "repository_member_count"),
            ("coverage_live_project_count", "live_project_count"),
            ("coverage_deleted_project_count", "deleted_project_count"),
            ("coverage_observed_occurrence_count", "observed_occurrence_count"),
            ("coverage_observed_row_count", "observed_row_count"),
            ("coverage_observed_manifest_bytes", "observed_manifest_bytes"),
            ("coverage_project_count", "project_count"),
            ("coverage_reference_count", "reference_count"),
            ("coverage_unique_blob_count", "unique_blob_count"),
            ("coverage_unique_blob_bytes", "unique_blob_bytes"),
            ("coverage_referenced_bytes", "referenced_bytes"),
            ("coverage_sha256", "coverage_sha256"),
            ("coverage_manifest_sha256", "manifest_sha256"),
        )
        if not _matches(row, source, pairs):
            _fail("cutover_phase_binding_coverage_source_invalid")
        return
    if stage is BusinessCutoverPhaseBindingStage.AUDIT:
        source = _select_one(
            connection, "repository_audit_certificates", _CERTIFICATE_COLUMNS,
            "certificate_id=? AND cutover_id=?",
            (row["audit_certificate_id"], row["cutover_id"]),
            "cutover_phase_binding_audit_source_invalid",
        )
        pairs = (
            ("audit_certificate_id", "certificate_id"),
            ("cutover_id", "cutover_id"),
            ("audit_certificate_sequence", "sequence"),
            ("audit_purpose", "purpose"),
            ("audit_transaction_id", "transaction_id"),
            ("audit_receipt_kind", "receipt_kind"),
            ("audit_committed_at", "committed_at"),
            ("audit_previous_certificate_id", "previous_certificate_id"),
            ("audit_previous_manifest_sha256", "previous_manifest_sha256"),
            ("audit_previous_tail_sha256", "previous_tail_sha256"),
            ("actual_schema_version", "schema_version"),
            ("actual_migration_ledger_count", "migration_ledger_count"),
            ("actual_migration_ledger_sha256", "migration_ledger_sha256"),
            ("before_receipt_count", "receipt_count"),
            ("before_receipt_root_sha256", "receipt_root_sha256"),
            ("worker_ledger_sequence", "worker_ledger_sequence"),
            ("worker_ledger_tail_sha256", "worker_ledger_tail_sha256"),
            ("audit_findings_sha256", "findings_sha256"),
            ("audit_manifest_sha256", "manifest_sha256"),
            ("audit_certificate_tail_sha256", "certificate_tail_sha256"),
        )
        if not _matches(row, source, pairs):
            _fail("cutover_phase_binding_audit_source_invalid")
        return
    if stage is BusinessCutoverPhaseBindingStage.ACTIVATION:
        source = _select_one(
            connection, "business_cutover_activation_receipts",
            _ACTIVATION_RECEIPT_COLUMNS, "cutover_id=? AND transaction_id=?",
            (row["cutover_id"], row["activation_transaction_id"]),
            "cutover_phase_binding_activation_source_invalid",
        )
        pairs = (
            ("cutover_id", "cutover_id"),
            ("activation_transaction_id", "transaction_id"),
            ("activation_receipt_kind", "receipt_kind"),
            ("activation_committed_at", "committed_at"),
            ("install_transaction_id", "install_transaction_id"),
            ("install_manifest_sha256", "install_manifest_sha256"),
            ("audit_transaction_id", "audit_transaction_id"),
            ("audit_manifest_sha256", "audit_manifest_sha256"),
            ("expected_migration_ledger_sha256", "migration_ledger_sha256"),
            ("activation_manifest_sha256", "manifest_sha256"),
        )
        if not _matches(row, source, pairs):
            _fail("cutover_phase_binding_activation_source_invalid")
        return
    source = _select_one(
        connection, "business_cutover_mount_receipts", _MOUNT_RECEIPT_COLUMNS,
        "cutover_id=? AND transaction_id=?",
        (row["cutover_id"], row["mount_transaction_id"]),
        "cutover_phase_binding_mount_source_invalid",
    )
    pairs = (
        ("cutover_id", "cutover_id"),
        ("mount_transaction_id", "transaction_id"),
        ("mount_receipt_kind", "receipt_kind"),
        ("mount_committed_at", "committed_at"),
        ("install_transaction_id", "install_transaction_id"),
        ("install_receipt_kind", "install_receipt_kind"),
        ("install_committed_at", "install_committed_at"),
        ("install_manifest_sha256", "install_manifest_sha256"),
        ("coverage_transaction_id", "coverage_transaction_id"),
        ("coverage_receipt_kind", "coverage_receipt_kind"),
        ("coverage_committed_at", "coverage_committed_at"),
        ("coverage_manifest_sha256", "coverage_manifest_sha256"),
        ("audit_transaction_id", "audit_transaction_id"),
        ("audit_manifest_sha256", "audit_manifest_sha256"),
        ("activation_transaction_id", "activation_transaction_id"),
        ("activation_manifest_sha256", "activation_manifest_sha256"),
        ("repository_epoch", "repository_epoch"),
        ("runtime_generation", "runtime_generation"),
        ("code_digest", "code_digest"),
        ("mount_manifest_sha256", "manifest_sha256"),
    )
    if not _matches(row, source, pairs):
        _fail("cutover_phase_binding_mount_source_invalid")


def _stage_pointer(
    stage: BusinessCutoverPhaseBindingStage,
    evidence: Any,
) -> BusinessCutoverReceiptPointer:
    if stage is BusinessCutoverPhaseBindingStage.ASSET_PROVIDER:
        return evidence.coverage.pointer
    if stage is BusinessCutoverPhaseBindingStage.AUDIT:
        return evidence.receipt_parent
    if stage is BusinessCutoverPhaseBindingStage.ACTIVATION:
        return evidence.activation
    return evidence.mount


def _stage_role(stage: BusinessCutoverPhaseBindingStage) -> str:
    return {
        BusinessCutoverPhaseBindingStage.ASSET_PROVIDER: "coverage",
        BusinessCutoverPhaseBindingStage.AUDIT: "audit",
        BusinessCutoverPhaseBindingStage.ACTIVATION: "activation",
        BusinessCutoverPhaseBindingStage.MOUNT: "mount",
    }[stage]


def _require_transaction_receipt(
    connection: Any,
    pointer: BusinessCutoverReceiptPointer,
) -> None:
    receipt = _select_one(
        connection,
        "transaction_receipts",
        ("transaction_id", "kind", "committed_at"),
        "transaction_id=?",
        (pointer.transaction_id,),
        "cutover_phase_binding_transaction_receipt_invalid",
    )
    if (
        receipt["kind"] != pointer.receipt_kind
        or receipt["committed_at"] != pointer.committed_at
    ):
        _fail("cutover_phase_binding_transaction_receipt_invalid")


class SQLiteCutoverPhaseBindingPrefix:
    """Immutable reader-issued proof of the exact current binding prefix."""

    __slots__ = ("cutover_id", "current_head", "bindings", "_issuer")

    def __init__(
        self,
        *,
        cutover_id: str,
        current_head: BusinessCutoverPhaseBindingHead,
        bindings: tuple[BusinessCutoverPhaseBinding, ...],
        _issuer: Any,
    ) -> None:
        if _issuer is not _READER_ISSUER:
            _fail("cutover_phase_binding_reader_issuer_invalid")
        object.__setattr__(self, "cutover_id", cutover_id)
        object.__setattr__(self, "current_head", current_head)
        object.__setattr__(self, "bindings", bindings)
        object.__setattr__(self, "_issuer", _issuer)

    def __setattr__(self, name: str, value: Any) -> None:
        del name, value
        raise AttributeError("SQLiteCutoverPhaseBindingPrefix is immutable")


def _require_cutover_phase_binding_prefix(
    value: Any,
) -> SQLiteCutoverPhaseBindingPrefix:
    if (
        type(value) is not SQLiteCutoverPhaseBindingPrefix
        or getattr(value, "_issuer", None) is not _READER_ISSUER
    ):
        _fail("cutover_phase_binding_reader_prefix_invalid")
    return value


def _read_cutover_phase_binding_prefix(
    connection: Any,
) -> SQLiteCutoverPhaseBindingPrefix:
    """Reissue and verify the exact V13 prefix ending at the current head."""

    try:
        if getattr(connection, "in_transaction", None) is not True:
            _fail("cutover_phase_binding_read_transaction_required")
        actual_head_columns = tuple(
            item[1] for item in connection.execute(
                "PRAGMA table_info(business_cutover_head)"
            ).fetchall()
        )
        if actual_head_columns != _HEAD_COLUMNS:
            _fail("cutover_phase_binding_head_schema_invalid")
        head_rows = connection.execute(
            "SELECT " + ",".join(_HEAD_COLUMNS) + " FROM business_cutover_head"
        ).fetchall()
        if len(head_rows) != 1:
            _fail("cutover_phase_binding_current_head_invalid")
        current = _row(
            head_rows[0], _HEAD_COLUMNS, "cutover_phase_binding_current_head_invalid"
        )
        if (
            current["singleton"] != 1
            or type(current["phase_revision"]) is not int
            or not 1 <= current["phase_revision"] <= 5
            or current["state_revision"] != current["phase_revision"]
        ):
            _fail("cutover_phase_binding_current_head_invalid")

        install = _select_one(
            connection, "business_cutover_install_receipts", _INSTALL_COLUMNS,
            "cutover_id=?", (current["cutover_id"],),
            "cutover_phase_binding_install_source_invalid",
        )
        if not all(
            _same(install[left], current[right])
            for left, right in (
                ("cutover_id", "cutover_id"),
                ("repository_epoch", "repository_epoch"),
                ("runtime_generation", "runtime_generation"),
                ("code_digest", "code_digest"),
                ("transaction_id", "install_transaction_id"),
                ("manifest_sha256", "install_manifest_sha256"),
            )
        ):
            _fail("cutover_phase_binding_install_source_invalid")
        pointers: dict[str, BusinessCutoverReceiptPointer] = {
            "install": _pointer(
                install["transaction_id"], install["receipt_kind"],
                install["committed_at"], install["manifest_sha256"],
            )
        }
        _require_transaction_receipt(connection, pointers["install"])

        stage_rows: list[
            tuple[BusinessCutoverPhaseBindingStage, Mapping[str, Any]]
        ] = []
        for stage, table, columns, successor_revision in _STAGES:
            expected = 1 if current["phase_revision"] >= successor_revision else 0
            rows = _binding_rows(connection, table, columns, expected)
            if rows:
                row = dict(rows[0])
                row["_install_committed_at"] = install["committed_at"]
                stage_rows.append((stage, row))

        bindings: list[BusinessCutoverPhaseBinding] = []
        prior_evidence: dict[BusinessCutoverPhaseBindingStage, Any] = {}
        previous_successor: BusinessCutoverPhaseBindingHead | None = None
        for stage, row in stage_rows:
            if not all(
                _same(row[name], current[name])
                for name in (
                    "cutover_id", "repository_epoch", "runtime_generation",
                    "code_digest",
                )
            ):
                _fail("cutover_phase_binding_identity_mismatch")
            if stage in (
                BusinessCutoverPhaseBindingStage.ACTIVATION,
                BusinessCutoverPhaseBindingStage.MOUNT,
            ):
                audit_row = next(
                    item for item_stage, item in stage_rows
                    if item_stage is BusinessCutoverPhaseBindingStage.AUDIT
                )
                row["_audit_certificate_tail_sha256"] = audit_row[
                    "audit_certificate_tail_sha256"
                ]
            evidence = _direct_evidence(stage, row, prior_evidence)
            _require_transaction_receipt(
                connection,
                _stage_pointer(stage, evidence),
            )
            old_head = _binding_head(row, "predecessor", pointers)
            next_pointers = dict(pointers)
            next_pointers[_stage_role(stage)] = _stage_pointer(stage, evidence)
            new_head = _binding_head(row, "successor", next_pointers)
            if previous_successor is not None and old_head != previous_successor:
                _fail("cutover_phase_binding_chain_invalid")
            projection = BusinessCutoverPhaseBindingProjection(
                stage=stage,
                identity=BusinessCutoverPhaseBindingIdentity(
                    cutover_id=row["cutover_id"],
                    repository_epoch=row["repository_epoch"],
                    runtime_generation=row["runtime_generation"],
                    code_digest=row["code_digest"],
                ),
                old_head=old_head,
                new_head=new_head,
                admission=BusinessCutoverAdmissionCheckpoint(
                    source_revision=row["source_admission_revision"],
                    source_admission_sha256=row["source_admission_sha256"],
                    source_freeze_sha256=row["source_freeze_sha256"],
                    file_authority_state=row["file_admission_state"],
                    file_authority_epoch=row["file_admission_epoch"],
                    file_revision=row["file_admission_revision"],
                    file_cutover_id=row["file_admission_cutover_id"],
                    file_admission_sha256=row["file_admission_sha256"],
                ),
                maintenance=BusinessCutoverMaintenanceCheckpoint(
                    scope=row["maintenance_scope"],
                    proof_sha256=row["maintenance_repository_proof_sha256"],
                    observed_at=row["maintenance_observed_at"],
                ),
                checkpoint=_checkpoint(row),
                direct_evidence=evidence,
            )
            issued = verify_business_cutover_phase_binding(
                issue_business_cutover_phase_binding(projection)
            )
            persisted = _project_business_cutover_phase_binding_row(issued)
            layout = _business_cutover_phase_binding_layout(stage)
            if (
                persisted.table != layout.table
                or persisted.columns != layout.columns
                or any(
                    not _same(row[column], persisted.values[index])
                    for index, column in enumerate(persisted.columns)
                )
            ):
                _fail("cutover_phase_binding_stored_row_mismatch")
            _require_direct_source(connection, stage, row)
            bindings.append(issued)
            prior_evidence[stage] = evidence
            pointers = next_pointers
            previous_successor = new_head

        current_head = BusinessCutoverPhaseBindingHead(
            state_revision=current["state_revision"],
            phase_revision=current["phase_revision"],
            phase=current["phase"],
            authority_state=current["authority_state"],
            head_sha256=current["head_sha256"],
            updated_at=current["updated_at"],
            previous_head_sha256=current["previous_head_sha256"],
            install=pointers["install"],
            coverage=pointers.get("coverage"),
            audit=pointers.get("audit"),
            activation=pointers.get("activation"),
            mount=pointers.get("mount"),
        )
        current_identity = BusinessCutoverPhaseBindingIdentity(
            cutover_id=current["cutover_id"],
            repository_epoch=current["repository_epoch"],
            runtime_generation=current["runtime_generation"],
            code_digest=current["code_digest"],
        )
        if not _head_is_canonical(current_head, current_identity):
            _fail("cutover_phase_binding_current_head_invalid")
        pointer_columns = (
            ("install", "install_transaction_id", "install_manifest_sha256"),
            ("coverage", "coverage_transaction_id", "coverage_manifest_sha256"),
            ("audit", "audit_transaction_id", "audit_manifest_sha256"),
            ("activation", "activation_transaction_id", "activation_manifest_sha256"),
            ("mount", "mount_transaction_id", "mount_manifest_sha256"),
        )
        for role, transaction_column, manifest_column in pointer_columns:
            pointer_value = pointers.get(role)
            if pointer_value is None:
                if current[transaction_column] is not None or current[manifest_column] is not None:
                    _fail("cutover_phase_binding_current_head_invalid")
            elif (
                pointer_value.transaction_id != current[transaction_column]
                or not hmac.compare_digest(
                    pointer_value.manifest_sha256, current[manifest_column]
                )
            ):
                _fail("cutover_phase_binding_current_head_invalid")
        if previous_successor is not None and current_head != previous_successor:
            _fail("cutover_phase_binding_current_head_invalid")
        _verify_business_cutover_phase_binding_chain(tuple(bindings))
        return SQLiteCutoverPhaseBindingPrefix(
            cutover_id=current["cutover_id"],
            current_head=current_head,
            bindings=tuple(bindings),
            _issuer=_READER_ISSUER,
        )
    except SQLiteCutoverPhaseBindingReaderError:
        raise
    except (
        BusinessCutoverPhaseBindingContractError,
        BusinessCutoverPhaseBindingIntegrityError,
        BusinessCutoverPhaseBindingPersistabilityError,
        KeyError,
        StopIteration,
        TypeError,
        ValueError,
        sqlite3.DatabaseError,
    ) as exc:
        raise SQLiteCutoverPhaseBindingReaderError(
            "cutover_phase_binding_semantic_verification_failed"
        ) from exc


__all__ = ()
