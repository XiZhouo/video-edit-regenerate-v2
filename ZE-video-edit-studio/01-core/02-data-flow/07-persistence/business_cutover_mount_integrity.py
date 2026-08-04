"""Final pure composition of cutover, asset coverage, and activation evidence.

The mount plan is deliberately not another activation mechanism.  It consumes
the frozen cutover-v1 plan, a freshly reconciled exact asset repository seal,
and a freshly reconciled exact activation-v2 row.  It never imports or reads
the pre-generated cutover-v1 join evidence.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any, NamedTuple, Optional

from business_codec import BusinessCodecError, encode_payload_bounded
from business_cutover_activation_contract import (
    BUSINESS_CUTOVER_ACTIVATION_AFTER_AUTHORITY_STATE,
    BUSINESS_CUTOVER_ACTIVATION_AFTER_STATE_REVISION,
    BusinessCutoverActivationFreshObservation,
    BusinessCutoverActivationFreshPrerequisites,
    BusinessCutoverActivationReconcileStatus,
)
from business_cutover_activation_integrity import (
    BusinessCutoverActivationIntegrityError,
    BusinessCutoverActivationPlan,
    _classify_business_cutover_activation,
    _require_business_cutover_activation_evidence,
    _require_business_cutover_activation_plan,
)
from business_cutover_asset_contract import (
    BUSINESS_CUTOVER_ASSET_COVERAGE_PROTOCOL,
    BusinessCutoverAssetCoverageEvidence,
    BusinessCutoverAssetCoverageReconcileStatus,
)
from business_cutover_asset_integrity import (
    BusinessCutoverAssetCoveragePlan,
    BusinessCutoverAssetIntegrityError,
    _FreshBusinessCutoverAssetCoverageObservation,
    _classify_business_cutover_asset_coverage,
    _repository_member_root,
    _require_business_cutover_asset_coverage_plan,
)
from business_cutover_integrity import (
    BusinessCutoverIntegrityError,
    BusinessCutoverPlan,
    _require_business_cutover_plan,
)
from business_cutover_contract import (
    BUSINESS_CUTOVER_PROTOCOL,
    BusinessCutoverInstallEvidence,
)
from repository_audit_contract import (
    RepositoryAuditFreshMembershipObservation,
    RepositoryAuditFreshObservation,
)

from business_cutover_mount_contract import (
    BUSINESS_CUTOVER_MOUNT_MANIFEST_SCHEMA,
    BUSINESS_CUTOVER_MOUNT_PLAN_STATUS,
    BUSINESS_CUTOVER_MOUNT_PROTOCOL,
    BUSINESS_CUTOVER_MOUNT_STATUS,
    BUSINESS_CUTOVER_MOUNT_TRANSACTION_KIND,
    BUSINESS_CUTOVER_MOUNT_TRANSACTION_PREFIX,
    MAX_BUSINESS_CUTOVER_MOUNT_MANIFEST_BYTES,
    MAX_BUSINESS_CUTOVER_MOUNT_MANIFEST_NODES,
    BusinessCutoverMountActivationBinding,
    BusinessCutoverMountClassification,
    BusinessCutoverMountContractError,
    BusinessCutoverMountCoverageBinding,
    BusinessCutoverMountEvidence,
    BusinessCutoverMountFreshObservation,
    BusinessCutoverMountGenericReceiptEvidence,
    BusinessCutoverMountInstallBinding,
    BusinessCutoverMountPersistedObservation,
    BusinessCutoverMountPersistedReadStatus,
    BusinessCutoverMountReconcileStatus,
    BusinessCutoverMountRuntimeIdentity,
    _unwrap_business_cutover_mount_fresh_observation,
)


BUSINESS_CUTOVER_MOUNT_MANIFEST_HASH_DOMAIN = (
    b"ze.business-cutover.mount-authorization-manifest-digest.v2"
)

_PLAN_ISSUER = object()
_INSTALL_LINKED_RECEIPT_READER_TOKEN = object()
_COVERAGE_LINKED_RECEIPT_READER_TOKEN = object()
_LINKED_RECEIPT_OBSERVATION_ISSUER = object()


class BusinessCutoverMountIntegrityError(ValueError):
    """Stable, payload-free mount composition failure."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        if type(code) is not str or not code:
            raise TypeError("code must be a non-empty exact string")
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise BusinessCutoverMountIntegrityError(code)


def _frame(hasher: Any, value: bytes) -> None:
    hasher.update(len(value).to_bytes(8, "big"))
    hasher.update(value)


def _manifest_digest(value: bytes) -> bytes:
    hasher = hashlib.sha256()
    _frame(hasher, BUSINESS_CUTOVER_MOUNT_MANIFEST_HASH_DOMAIN)
    _frame(hasher, value)
    return hasher.digest()


def _encode_manifest(value: Any) -> bytes:
    try:
        return encode_payload_bounded(
            value,
            MAX_BUSINESS_CUTOVER_MOUNT_MANIFEST_BYTES,
            MAX_BUSINESS_CUTOVER_MOUNT_MANIFEST_NODES,
        )
    except BusinessCodecError as exc:
        raise BusinessCutoverMountIntegrityError(
            "business_cutover_mount_manifest_encoding_failed"
        ) from exc


def _runtime_manifest(value: BusinessCutoverMountRuntimeIdentity) -> dict:
    return {
        "authority": value.authority,
        "authority_state": value.authority_state,
        "repository_epoch": value.repository_epoch,
        "source_admission_revision": value.source_admission_revision,
        "repository_revision": value.repository_revision,
        "state_revision": value.state_revision,
        "runtime_generation": value.runtime_generation,
        "code_digest": value.code_digest.hex(),
        "runtime_schema_version": value.runtime_schema_version,
        "target_schema_version": value.target_schema_version,
        "target_schema_fingerprint": value.target_schema_fingerprint,
    }


def _install_manifest(value: BusinessCutoverMountInstallBinding) -> dict:
    return {
        "transaction_id": value.transaction_id,
        "receipt_kind": value.receipt_kind,
        "manifest_sha256": value.manifest_sha256.hex(),
        "receipt_parent": {
            "ledger": "transaction_receipts",
            "transaction_id": value.transaction_id,
            "kind": value.receipt_kind,
            "committed_at": value.committed_at,
            "specialised_protocol": BUSINESS_CUTOVER_PROTOCOL,
        },
    }


def _coverage_manifest(value: BusinessCutoverMountCoverageBinding) -> dict:
    return {
        "transaction_id": value.transaction_id,
        "receipt_kind": value.receipt_kind,
        "manifest_sha256": value.manifest_sha256.hex(),
        "receipt_parent": {
            "ledger": "transaction_receipts",
            "transaction_id": value.transaction_id,
            "kind": value.receipt_kind,
            "committed_at": value.committed_at,
            "specialised_protocol": (
                BUSINESS_CUTOVER_ASSET_COVERAGE_PROTOCOL
            ),
        },
        "coverage_sha256": value.coverage_sha256.hex(),
        "member_root_sha256": value.member_root_sha256.hex(),
        "batch_count": value.batch_count,
        "repository_member_count": value.repository_member_count,
        "live_project_count": value.live_project_count,
        "deleted_project_count": value.deleted_project_count,
        "project_count": value.project_count,
        "reference_count": value.reference_count,
        "unique_blob_count": value.unique_blob_count,
        "unique_blob_bytes": value.unique_blob_bytes,
        "referenced_bytes": value.referenced_bytes,
        "observed_occurrence_count": value.observed_occurrence_count,
        "observed_row_count": value.observed_row_count,
        "observed_manifest_bytes": value.observed_manifest_bytes,
    }


def _activation_manifest(value: BusinessCutoverMountActivationBinding) -> dict:
    return {
        "transaction_id": value.transaction_id,
        "receipt_kind": value.receipt_kind,
        "manifest_sha256": value.manifest_sha256.hex(),
        "activated_at": value.activated_at,
        "audit_certificate": {
            "certificate_id": value.audit_certificate_id,
            "sequence": value.audit_certificate_sequence,
            "transaction_id": value.audit_transaction_id,
            "receipt_kind": value.audit_receipt_kind,
            "manifest_sha256": value.audit_manifest_sha256.hex(),
            "certificate_tail_sha256": (
                value.audit_certificate_tail_sha256.hex()
            ),
            "completed_at": value.audit_completed_at,
        },
        "target_migration_ledger": {
            "count": value.expected_migration_ledger_count,
            "sha256": value.expected_migration_ledger_sha256.hex(),
        },
    }


def _build_manifest(
    *,
    cutover_id: str,
    receipt: BusinessCutoverMountGenericReceiptEvidence,
    runtime: BusinessCutoverMountRuntimeIdentity,
    source_admission_sha256: bytes,
    final_admission_sha256: bytes,
    install: BusinessCutoverMountInstallBinding,
    coverage: BusinessCutoverMountCoverageBinding,
    activation: BusinessCutoverMountActivationBinding,
    mounted_at: str,
) -> bytes:
    return _encode_manifest({
        "schema": BUSINESS_CUTOVER_MOUNT_MANIFEST_SCHEMA,
        "protocol": BUSINESS_CUTOVER_MOUNT_PROTOCOL,
        "status": BUSINESS_CUTOVER_MOUNT_STATUS,
        "manifest_hash_domain": (
            BUSINESS_CUTOVER_MOUNT_MANIFEST_HASH_DOMAIN.decode("ascii")
        ),
        "mount": {
            "cutover_id": cutover_id,
            "mounted_at": mounted_at,
        },
        "receipt_parent": {
            "ledger": "transaction_receipts",
            "transaction_id": receipt.transaction_id,
            "kind": receipt.kind,
            "committed_at": receipt.committed_at,
            "specialised_protocol": BUSINESS_CUTOVER_MOUNT_PROTOCOL,
            "specialised_cutover_id": cutover_id,
        },
        "authority": {
            "runtime": _runtime_manifest(runtime),
            "source_admission_sha256": source_admission_sha256.hex(),
            "final_admission_sha256": final_admission_sha256.hex(),
        },
        "install": _install_manifest(install),
        "asset_repository_coverage": _coverage_manifest(coverage),
        "activation": _activation_manifest(activation),
    })


def _canonical_runtime(value: Any) -> BusinessCutoverMountRuntimeIdentity:
    try:
        rebuilt = BusinessCutoverMountRuntimeIdentity(
            authority=value.authority,
            authority_state=value.authority_state,
            repository_epoch=value.repository_epoch,
            source_admission_revision=value.source_admission_revision,
            repository_revision=value.repository_revision,
            state_revision=value.state_revision,
            runtime_generation=value.runtime_generation,
            code_digest=value.code_digest,
            runtime_schema_version=value.runtime_schema_version,
            target_schema_version=value.target_schema_version,
            target_schema_fingerprint=value.target_schema_fingerprint,
        )
    except (AttributeError, BusinessCutoverMountContractError, TypeError):
        _fail("business_cutover_mount_runtime_changed")
    if rebuilt != value:
        _fail("business_cutover_mount_runtime_changed")
    return rebuilt


def _canonical_install(value: Any) -> BusinessCutoverMountInstallBinding:
    try:
        rebuilt = BusinessCutoverMountInstallBinding(
            transaction_id=value.transaction_id,
            receipt_kind=value.receipt_kind,
            committed_at=value.committed_at,
            manifest_sha256=value.manifest_sha256,
        )
    except (AttributeError, BusinessCutoverMountContractError, TypeError):
        _fail("business_cutover_mount_install_changed")
    if rebuilt != value:
        _fail("business_cutover_mount_install_changed")
    return rebuilt


def _canonical_coverage(value: Any) -> BusinessCutoverMountCoverageBinding:
    try:
        rebuilt = BusinessCutoverMountCoverageBinding(
            transaction_id=value.transaction_id,
            receipt_kind=value.receipt_kind,
            committed_at=value.committed_at,
            manifest_sha256=value.manifest_sha256,
            coverage_sha256=value.coverage_sha256,
            member_root_sha256=value.member_root_sha256,
            batch_count=value.batch_count,
            repository_member_count=value.repository_member_count,
            live_project_count=value.live_project_count,
            deleted_project_count=value.deleted_project_count,
            project_count=value.project_count,
            reference_count=value.reference_count,
            unique_blob_count=value.unique_blob_count,
            unique_blob_bytes=value.unique_blob_bytes,
            referenced_bytes=value.referenced_bytes,
            observed_occurrence_count=value.observed_occurrence_count,
            observed_row_count=value.observed_row_count,
            observed_manifest_bytes=value.observed_manifest_bytes,
        )
    except (AttributeError, BusinessCutoverMountContractError, TypeError):
        _fail("business_cutover_mount_coverage_changed")
    if rebuilt != value:
        _fail("business_cutover_mount_coverage_changed")
    return rebuilt


def _canonical_activation(value: Any) -> BusinessCutoverMountActivationBinding:
    try:
        rebuilt = BusinessCutoverMountActivationBinding(
            transaction_id=value.transaction_id,
            receipt_kind=value.receipt_kind,
            manifest_sha256=value.manifest_sha256,
            audit_certificate_id=value.audit_certificate_id,
            audit_certificate_sequence=value.audit_certificate_sequence,
            audit_transaction_id=value.audit_transaction_id,
            audit_receipt_kind=value.audit_receipt_kind,
            audit_manifest_sha256=value.audit_manifest_sha256,
            audit_certificate_tail_sha256=value.audit_certificate_tail_sha256,
            expected_migration_ledger_count=(
                value.expected_migration_ledger_count
            ),
            expected_migration_ledger_sha256=(
                value.expected_migration_ledger_sha256
            ),
            audit_completed_at=value.audit_completed_at,
            activated_at=value.activated_at,
        )
    except (AttributeError, BusinessCutoverMountContractError, TypeError):
        _fail("business_cutover_mount_activation_changed")
    if rebuilt != value:
        _fail("business_cutover_mount_activation_changed")
    return rebuilt


def _canonical_receipt(value: Any) -> BusinessCutoverMountGenericReceiptEvidence:
    try:
        rebuilt = BusinessCutoverMountGenericReceiptEvidence(
            transaction_id=value.transaction_id,
            kind=value.kind,
            committed_at=value.committed_at,
        )
    except (AttributeError, BusinessCutoverMountContractError, TypeError):
        _fail("business_cutover_mount_receipt_changed")
    if rebuilt != value:
        _fail("business_cutover_mount_receipt_changed")
    return rebuilt


class _FreshBusinessCutoverMountLinkedReceiptObservation:
    """One durable-reader stamped specialised/generic parent join."""

    __slots__ = ("role", "evidence", "receipt_parent", "_reader", "_issuer")

    def __init__(
        self,
        *,
        role: str,
        evidence: Any,
        receipt_parent: BusinessCutoverMountGenericReceiptEvidence,
        _reader: Any,
        _issuer: Any,
    ) -> None:
        expected_reader = (
            _INSTALL_LINKED_RECEIPT_READER_TOKEN
            if role == "install"
            else _COVERAGE_LINKED_RECEIPT_READER_TOKEN
        )
        if (
            role not in ("install", "coverage")
            or _reader is not expected_reader
            or _issuer is not _LINKED_RECEIPT_OBSERVATION_ISSUER
            or type(receipt_parent)
            is not BusinessCutoverMountGenericReceiptEvidence
        ):
            _fail("business_cutover_mount_linked_receipt_issuer_invalid")
        object.__setattr__(self, "role", role)
        object.__setattr__(self, "evidence", evidence)
        object.__setattr__(self, "receipt_parent", receipt_parent)
        object.__setattr__(self, "_reader", _reader)
        object.__setattr__(self, "_issuer", _issuer)

    def __setattr__(self, name: str, value: Any) -> None:
        del name, value
        raise AttributeError("fresh linked receipt observation is immutable")


def _stamp_business_cutover_mount_install_receipt_observation(
    *,
    evidence: BusinessCutoverInstallEvidence,
    receipt_parent: BusinessCutoverMountGenericReceiptEvidence,
    _reader_token: Any,
) -> _FreshBusinessCutoverMountLinkedReceiptObservation:
    if (
        _reader_token is not _INSTALL_LINKED_RECEIPT_READER_TOKEN
        or type(evidence) is not BusinessCutoverInstallEvidence
        or type(receipt_parent)
        is not BusinessCutoverMountGenericReceiptEvidence
        or receipt_parent.transaction_id != evidence.transaction_id
        or receipt_parent.kind != evidence.receipt_kind
    ):
        _fail("business_cutover_mount_install_receipt_observation_invalid")
    return _FreshBusinessCutoverMountLinkedReceiptObservation(
        role="install",
        evidence=evidence,
        receipt_parent=_canonical_receipt(receipt_parent),
        _reader=_reader_token,
        _issuer=_LINKED_RECEIPT_OBSERVATION_ISSUER,
    )


def _stamp_business_cutover_mount_coverage_receipt_observation(
    *,
    evidence: BusinessCutoverAssetCoverageEvidence,
    receipt_parent: BusinessCutoverMountGenericReceiptEvidence,
    _reader_token: Any,
) -> _FreshBusinessCutoverMountLinkedReceiptObservation:
    if (
        _reader_token is not _COVERAGE_LINKED_RECEIPT_READER_TOKEN
        or type(evidence) is not BusinessCutoverAssetCoverageEvidence
        or type(receipt_parent)
        is not BusinessCutoverMountGenericReceiptEvidence
        or receipt_parent.transaction_id != evidence.transaction_id
        or receipt_parent.kind != evidence.receipt_kind
    ):
        _fail("business_cutover_mount_coverage_receipt_observation_invalid")
    return _FreshBusinessCutoverMountLinkedReceiptObservation(
        role="coverage",
        evidence=evidence,
        receipt_parent=_canonical_receipt(receipt_parent),
        _reader=_reader_token,
        _issuer=_LINKED_RECEIPT_OBSERVATION_ISSUER,
    )


def _require_linked_receipt_observation(
    value: Any,
    *,
    role: str,
    expected_evidence: Any,
) -> _FreshBusinessCutoverMountLinkedReceiptObservation:
    expected_reader = (
        _INSTALL_LINKED_RECEIPT_READER_TOKEN
        if role == "install"
        else _COVERAGE_LINKED_RECEIPT_READER_TOKEN
    )
    if (
        type(value) is not _FreshBusinessCutoverMountLinkedReceiptObservation
        or getattr(value, "_reader", None)
        is not expected_reader
        or getattr(value, "_issuer", None)
        is not _LINKED_RECEIPT_OBSERVATION_ISSUER
        or getattr(value, "role", None) != role
        or getattr(value, "evidence", None) != expected_evidence
    ):
        _fail("business_cutover_mount_linked_receipt_observation_invalid")
    receipt = _canonical_receipt(getattr(value, "receipt_parent", None))
    if (
        receipt.transaction_id != expected_evidence.transaction_id
        or receipt.kind != expected_evidence.receipt_kind
    ):
        _fail("business_cutover_mount_linked_receipt_observation_invalid")
    return value


class _PreparedMount(NamedTuple):
    cutover_plan: BusinessCutoverPlan
    coverage_plan: BusinessCutoverAssetCoveragePlan
    activation_plan: BusinessCutoverActivationPlan
    install_receipt_observation: _FreshBusinessCutoverMountLinkedReceiptObservation
    coverage_receipt_observation: _FreshBusinessCutoverMountLinkedReceiptObservation
    evidence: BusinessCutoverMountEvidence


def _prepare_mount_static(
    *,
    cutover_plan: Any,
    coverage_plan: Any,
    activation_plan: Any,
    install_receipt_observation: Any,
    coverage_receipt_observation: Any,
    mounted_at: Any,
) -> _PreparedMount:
    try:
        cutover = _require_business_cutover_plan(cutover_plan)
    except (BusinessCutoverIntegrityError, TypeError):
        _fail("business_cutover_mount_cutover_plan_invalid")
    try:
        coverage = _require_business_cutover_asset_coverage_plan(coverage_plan)
    except (BusinessCutoverAssetIntegrityError, TypeError):
        _fail("business_cutover_mount_coverage_plan_invalid")
    try:
        activation = _require_business_cutover_activation_plan(activation_plan)
        activation_evidence = _require_business_cutover_activation_evidence(
            activation.evidence
        )
    except (BusinessCutoverActivationIntegrityError, TypeError):
        _fail("business_cutover_mount_activation_plan_invalid")

    install = cutover.install
    install_runtime = install.runtime
    coverage_evidence = coverage.expected_evidence
    install_receipt = _require_linked_receipt_observation(
        install_receipt_observation,
        role="install",
        expected_evidence=install,
    ).receipt_parent
    coverage_receipt = _require_linked_receipt_observation(
        coverage_receipt_observation,
        role="coverage",
        expected_evidence=coverage_evidence,
    ).receipt_parent
    audit_started_at = activation._audit_plan.evidence.findings.started_at
    if (
        install_receipt.committed_at > audit_started_at
        or coverage_receipt.committed_at > audit_started_at
    ):
        _fail("business_cutover_mount_linked_receipt_after_audit_started")
    if coverage_receipt.committed_at > install_receipt.committed_at:
        _fail("business_cutover_mount_linked_receipt_order_invalid")
    if (
        coverage.cutover_id != cutover.cutover_id
        or coverage_evidence.cutover_id != cutover.cutover_id
        or coverage.source_admission != cutover.source_admission
        or coverage.pending_admission != cutover.pending_admission
        or coverage.final_admission != cutover.final_admission
        or coverage.repository_epoch != install_runtime.repository_epoch
        or coverage_evidence.repository_epoch != install_runtime.repository_epoch
        or coverage.source_admission_revision
        != install_runtime.source_admission_revision
        or coverage_evidence.source_admission_revision
        != install_runtime.source_admission_revision
        or coverage.source_admission_sha256
        != cutover.closure.source_admission_sha256
        or coverage_evidence.source_admission_sha256
        != cutover.closure.source_admission_sha256
        or coverage.runtime_generation != install_runtime.runtime_generation
        or not hmac.compare_digest(coverage.code_digest, install_runtime.code_digest)
        or coverage.coverage.project_count
        != cutover.legacy_proof.live_project_count
        or not hmac.compare_digest(
            _repository_member_root(coverage._cutover_plan),
            _repository_member_root(cutover),
        )
    ):
        _fail("business_cutover_mount_coverage_identity_mismatch")
    if (
        activation.cutover_id != cutover.cutover_id
        or activation_evidence.cutover_id != cutover.cutover_id
        or activation_evidence.before_runtime != install_runtime
        or activation_evidence.final_admission != cutover.final_admission
        or not hmac.compare_digest(
            activation_evidence.final_admission_sha256,
            cutover.closure.final_admission_sha256,
        )
        or activation_evidence.install_transaction_id != install.transaction_id
        or activation_evidence.install_receipt_kind != install.receipt_kind
        or not hmac.compare_digest(
            activation_evidence.install_manifest_sha256,
            install.manifest_sha256,
        )
        or activation_evidence.actual_schema.user_version
        != cutover.expected_target_schema_version
        or activation_evidence.actual_schema.schema_fingerprint
        != cutover.expected_target_schema_fingerprint
        or activation_evidence.actual_schema.migration_ledger_count
        != activation_evidence.expected_migration_ledger_count
        or not hmac.compare_digest(
            activation_evidence.actual_schema.migration_ledger_sha256,
            activation_evidence.expected_migration_ledger_sha256,
        )
    ):
        _fail("business_cutover_mount_activation_identity_mismatch")
    after_runtime = activation_evidence.after_runtime
    if (
        after_runtime.authority != install_runtime.authority
        or after_runtime.authority_state
        != BUSINESS_CUTOVER_ACTIVATION_AFTER_AUTHORITY_STATE
        or after_runtime.repository_epoch != install_runtime.repository_epoch
        or after_runtime.cutover_id != install_runtime.cutover_id
        or after_runtime.source_admission_revision
        != install_runtime.source_admission_revision
        or after_runtime.repository_revision != install_runtime.repository_revision
        or after_runtime.state_revision
        != BUSINESS_CUTOVER_ACTIVATION_AFTER_STATE_REVISION
        or after_runtime.runtime_generation != install_runtime.runtime_generation
        or not hmac.compare_digest(after_runtime.code_digest, install_runtime.code_digest)
        or after_runtime.schema_version != install_runtime.schema_version
    ):
        _fail("business_cutover_mount_activation_runtime_mismatch")

    runtime = BusinessCutoverMountRuntimeIdentity(
        authority=after_runtime.authority,
        authority_state=after_runtime.authority_state,
        repository_epoch=install_runtime.repository_epoch,
        source_admission_revision=install_runtime.source_admission_revision,
        repository_revision=install_runtime.repository_revision,
        state_revision=after_runtime.state_revision,
        runtime_generation=install_runtime.runtime_generation,
        code_digest=install_runtime.code_digest,
        runtime_schema_version=install_runtime.schema_version,
        target_schema_version=cutover.expected_target_schema_version,
        target_schema_fingerprint=cutover.expected_target_schema_fingerprint,
    )
    install_binding = BusinessCutoverMountInstallBinding(
        transaction_id=install.transaction_id,
        receipt_kind=install.receipt_kind,
        committed_at=install_receipt.committed_at,
        manifest_sha256=install.manifest_sha256,
    )
    coverage_binding = BusinessCutoverMountCoverageBinding(
        transaction_id=coverage_evidence.transaction_id,
        receipt_kind=coverage_evidence.receipt_kind,
        committed_at=coverage_receipt.committed_at,
        manifest_sha256=coverage_evidence.manifest_sha256,
        coverage_sha256=coverage_evidence.coverage_sha256,
        member_root_sha256=_repository_member_root(cutover),
        batch_count=coverage_evidence.batch_count,
        repository_member_count=len(cutover.members),
        live_project_count=cutover.legacy_proof.live_project_count,
        deleted_project_count=cutover.legacy_proof.deleted_project_count,
        project_count=coverage_evidence.project_count,
        reference_count=coverage_evidence.reference_count,
        unique_blob_count=coverage_evidence.unique_blob_count,
        unique_blob_bytes=coverage_evidence.unique_blob_bytes,
        referenced_bytes=coverage_evidence.referenced_bytes,
        observed_occurrence_count=coverage_evidence.observed_occurrence_count,
        observed_row_count=coverage_evidence.observed_row_count,
        observed_manifest_bytes=coverage_evidence.observed_manifest_bytes,
    )
    activation_binding = BusinessCutoverMountActivationBinding(
        transaction_id=activation_evidence.transaction_id,
        receipt_kind=activation_evidence.receipt_kind,
        manifest_sha256=activation_evidence.manifest_sha256,
        audit_certificate_id=activation_evidence.audit_certificate_id,
        audit_certificate_sequence=activation_evidence.audit_certificate_sequence,
        audit_transaction_id=activation_evidence.audit_transaction_id,
        audit_receipt_kind=activation_evidence.audit_receipt_kind,
        audit_manifest_sha256=activation_evidence.audit_manifest_sha256,
        audit_certificate_tail_sha256=(
            activation_evidence.audit_certificate_tail_sha256
        ),
        expected_migration_ledger_count=(
            activation_evidence.expected_migration_ledger_count
        ),
        expected_migration_ledger_sha256=(
            activation_evidence.expected_migration_ledger_sha256
        ),
        audit_completed_at=activation_evidence.audit_completed_at,
        activated_at=activation_evidence.activated_at,
    )
    try:
        receipt = BusinessCutoverMountGenericReceiptEvidence(
            transaction_id=(
                BUSINESS_CUTOVER_MOUNT_TRANSACTION_PREFIX + cutover.cutover_id
            ),
            kind=BUSINESS_CUTOVER_MOUNT_TRANSACTION_KIND,
            committed_at=mounted_at,
        )
    except (BusinessCutoverMountContractError, TypeError) as exc:
        raise BusinessCutoverMountIntegrityError(
            "business_cutover_mount_mounted_at_invalid"
        ) from exc
    if mounted_at < activation_evidence.activated_at:
        _fail("business_cutover_mount_before_activation")
    manifest_bytes = _build_manifest(
        cutover_id=cutover.cutover_id,
        receipt=receipt,
        runtime=runtime,
        source_admission_sha256=cutover.closure.source_admission_sha256,
        final_admission_sha256=cutover.closure.final_admission_sha256,
        install=install_binding,
        coverage=coverage_binding,
        activation=activation_binding,
        mounted_at=mounted_at,
    )
    manifest_sha256 = _manifest_digest(manifest_bytes)
    try:
        evidence = BusinessCutoverMountEvidence(
            cutover_id=cutover.cutover_id,
            receipt_parent=receipt,
            runtime=runtime,
            source_admission_sha256=cutover.closure.source_admission_sha256,
            final_admission_sha256=cutover.closure.final_admission_sha256,
            install=install_binding,
            coverage=coverage_binding,
            activation=activation_binding,
            mounted_at=mounted_at,
            manifest_bytes=manifest_bytes,
            manifest_sha256=manifest_sha256,
        )
    except (BusinessCutoverMountContractError, TypeError) as exc:
        raise BusinessCutoverMountIntegrityError(
            "business_cutover_mount_evidence_invalid"
        ) from exc
    return _PreparedMount(
        cutover,
        coverage,
        activation,
        install_receipt_observation,
        coverage_receipt_observation,
        evidence,
    )


class _DependencyCheck(NamedTuple):
    status: Optional[BusinessCutoverMountReconcileStatus]
    error_code: str


def _check_live_dependencies(
    *,
    coverage_plan: BusinessCutoverAssetCoveragePlan,
    coverage_observation: _FreshBusinessCutoverAssetCoverageObservation,
    activation_plan: BusinessCutoverActivationPlan,
    activation_fresh_prerequisites: BusinessCutoverActivationFreshPrerequisites,
    activation_audit_persisted: RepositoryAuditFreshObservation,
    activation_persisted: BusinessCutoverActivationFreshObservation,
    activation_audit_membership: Optional[
        RepositoryAuditFreshMembershipObservation
    ] = None,
) -> _DependencyCheck:
    if type(coverage_observation) is not _FreshBusinessCutoverAssetCoverageObservation:
        _fail("business_cutover_mount_coverage_observation_invalid")
    if coverage_observation.current_admission != coverage_plan.final_admission:
        return _DependencyCheck(
            BusinessCutoverMountReconcileStatus.CONFLICT,
            "business_cutover_mount_file_admission_not_final",
        )
    try:
        coverage_result = _classify_business_cutover_asset_coverage(
            coverage_plan,
            coverage_observation,
        )
    except (BusinessCutoverAssetIntegrityError, TypeError):
        _fail("business_cutover_mount_coverage_observation_invalid")
    if coverage_result.status is not BusinessCutoverAssetCoverageReconcileStatus.COVERED_EXACT:
        if coverage_result.status is BusinessCutoverAssetCoverageReconcileStatus.UNKNOWN:
            return _DependencyCheck(
                BusinessCutoverMountReconcileStatus.UNKNOWN,
                "business_cutover_mount_coverage_unknown",
            )
        if coverage_result.status is BusinessCutoverAssetCoverageReconcileStatus.CONFLICT:
            return _DependencyCheck(
                BusinessCutoverMountReconcileStatus.CONFLICT,
                "business_cutover_mount_coverage_conflict",
            )
        return _DependencyCheck(
            BusinessCutoverMountReconcileStatus.CORRUPT,
            "business_cutover_mount_coverage_not_exact",
        )
    try:
        activation_result = _classify_business_cutover_activation(
            activation_plan,
            fresh_prerequisites=activation_fresh_prerequisites,
            audit_persisted=activation_audit_persisted,
            persisted=activation_persisted,
            audit_membership=activation_audit_membership,
        )
    except (BusinessCutoverActivationIntegrityError, TypeError):
        _fail("business_cutover_mount_activation_observation_invalid")
    if activation_result.status is not BusinessCutoverActivationReconcileStatus.EXACT:
        if activation_result.status is BusinessCutoverActivationReconcileStatus.UNKNOWN:
            return _DependencyCheck(
                BusinessCutoverMountReconcileStatus.UNKNOWN,
                "business_cutover_mount_activation_unknown",
            )
        if activation_result.status is BusinessCutoverActivationReconcileStatus.CONFLICT:
            return _DependencyCheck(
                BusinessCutoverMountReconcileStatus.CONFLICT,
                "business_cutover_mount_activation_conflict",
            )
        return _DependencyCheck(
            BusinessCutoverMountReconcileStatus.CORRUPT,
            "business_cutover_mount_activation_not_exact",
        )
    return _DependencyCheck(None, "")


class BusinessCutoverMountPlan:
    """Issuer-stamped expectation; never proof that mount persistence happened."""

    cutover_id: str
    transaction_id: str
    evidence: BusinessCutoverMountEvidence
    protocol: str
    status: str
    _cutover_plan: BusinessCutoverPlan
    _coverage_plan: BusinessCutoverAssetCoveragePlan
    _activation_plan: BusinessCutoverActivationPlan
    _install_receipt_observation: _FreshBusinessCutoverMountLinkedReceiptObservation
    _coverage_receipt_observation: _FreshBusinessCutoverMountLinkedReceiptObservation
    _mounted_at: str
    _issuer: Any

    __slots__ = (
        "cutover_id",
        "transaction_id",
        "evidence",
        "protocol",
        "status",
        "_cutover_plan",
        "_coverage_plan",
        "_activation_plan",
        "_install_receipt_observation",
        "_coverage_receipt_observation",
        "_mounted_at",
        "_issuer",
    )

    def __init__(self, prepared: _PreparedMount, *, mounted_at: str, _issuer: Any) -> None:
        if _issuer is not _PLAN_ISSUER:
            _fail("business_cutover_mount_plan_issuer_invalid")
        object.__setattr__(self, "cutover_id", prepared.evidence.cutover_id)
        object.__setattr__(self, "transaction_id", prepared.evidence.transaction_id)
        object.__setattr__(self, "evidence", prepared.evidence)
        object.__setattr__(self, "protocol", BUSINESS_CUTOVER_MOUNT_PROTOCOL)
        object.__setattr__(self, "status", BUSINESS_CUTOVER_MOUNT_PLAN_STATUS)
        object.__setattr__(self, "_cutover_plan", prepared.cutover_plan)
        object.__setattr__(self, "_coverage_plan", prepared.coverage_plan)
        object.__setattr__(self, "_activation_plan", prepared.activation_plan)
        object.__setattr__(
            self,
            "_install_receipt_observation",
            prepared.install_receipt_observation,
        )
        object.__setattr__(
            self,
            "_coverage_receipt_observation",
            prepared.coverage_receipt_observation,
        )
        object.__setattr__(self, "_mounted_at", mounted_at)
        object.__setattr__(self, "_issuer", _issuer)

    def __setattr__(self, name: str, value: Any) -> None:
        del name, value
        raise AttributeError("BusinessCutoverMountPlan is immutable")


def _issue_business_cutover_mount_plan(
    *,
    cutover_plan: BusinessCutoverPlan,
    coverage_plan: BusinessCutoverAssetCoveragePlan,
    coverage_observation: _FreshBusinessCutoverAssetCoverageObservation,
    activation_plan: BusinessCutoverActivationPlan,
    install_receipt_observation: _FreshBusinessCutoverMountLinkedReceiptObservation,
    coverage_receipt_observation: _FreshBusinessCutoverMountLinkedReceiptObservation,
    activation_fresh_prerequisites: BusinessCutoverActivationFreshPrerequisites,
    activation_audit_persisted: RepositoryAuditFreshObservation,
    activation_persisted: BusinessCutoverActivationFreshObservation,
    mounted_at: str,
    activation_audit_membership: Optional[
        RepositoryAuditFreshMembershipObservation
    ] = None,
) -> BusinessCutoverMountPlan:
    dependencies = _check_live_dependencies(
        coverage_plan=coverage_plan,
        coverage_observation=coverage_observation,
        activation_plan=activation_plan,
        activation_fresh_prerequisites=activation_fresh_prerequisites,
        activation_audit_persisted=activation_audit_persisted,
        activation_persisted=activation_persisted,
        activation_audit_membership=activation_audit_membership,
    )
    if dependencies.status is not None:
        _fail(dependencies.error_code)
    prepared = _prepare_mount_static(
        cutover_plan=cutover_plan,
        coverage_plan=coverage_plan,
        activation_plan=activation_plan,
        install_receipt_observation=install_receipt_observation,
        coverage_receipt_observation=coverage_receipt_observation,
        mounted_at=mounted_at,
    )
    return BusinessCutoverMountPlan(
        prepared,
        mounted_at=mounted_at,
        _issuer=_PLAN_ISSUER,
    )


def _rebuild_mount_evidence(value: Any) -> BusinessCutoverMountEvidence:
    if type(value) is not BusinessCutoverMountEvidence:
        _fail("business_cutover_mount_evidence_type_invalid")
    try:
        receipt = _canonical_receipt(value.receipt_parent)
        runtime = _canonical_runtime(value.runtime)
        install = _canonical_install(value.install)
        coverage = _canonical_coverage(value.coverage)
        activation = _canonical_activation(value.activation)
        manifest_bytes = _build_manifest(
            cutover_id=value.cutover_id,
            receipt=receipt,
            runtime=runtime,
            source_admission_sha256=value.source_admission_sha256,
            final_admission_sha256=value.final_admission_sha256,
            install=install,
            coverage=coverage,
            activation=activation,
            mounted_at=value.mounted_at,
        )
        manifest_sha256 = _manifest_digest(manifest_bytes)
        rebuilt = BusinessCutoverMountEvidence(
            cutover_id=value.cutover_id,
            receipt_parent=receipt,
            runtime=runtime,
            source_admission_sha256=value.source_admission_sha256,
            final_admission_sha256=value.final_admission_sha256,
            install=install,
            coverage=coverage,
            activation=activation,
            mounted_at=value.mounted_at,
            manifest_bytes=manifest_bytes,
            manifest_sha256=manifest_sha256,
            protocol=BUSINESS_CUTOVER_MOUNT_PROTOCOL,
            status=BUSINESS_CUTOVER_MOUNT_STATUS,
        )
    except (
        AttributeError,
        BusinessCutoverMountContractError,
        BusinessCutoverMountIntegrityError,
        TypeError,
    ):
        _fail("business_cutover_mount_evidence_changed")
    if (
        rebuilt != value
        or not hmac.compare_digest(rebuilt.manifest_bytes, value.manifest_bytes)
        or not hmac.compare_digest(rebuilt.manifest_sha256, value.manifest_sha256)
    ):
        _fail("business_cutover_mount_evidence_changed")
    return rebuilt


def _require_business_cutover_mount_evidence(value: Any) -> BusinessCutoverMountEvidence:
    return _rebuild_mount_evidence(value)


def _require_business_cutover_mount_plan(value: Any) -> BusinessCutoverMountPlan:
    if (
        type(value) is not BusinessCutoverMountPlan
        or getattr(value, "_issuer", None) is not _PLAN_ISSUER
        or getattr(value, "protocol", None) != BUSINESS_CUTOVER_MOUNT_PROTOCOL
        or getattr(value, "status", None) != BUSINESS_CUTOVER_MOUNT_PLAN_STATUS
    ):
        _fail("business_cutover_mount_plan_invalid")
    prepared = _prepare_mount_static(
        cutover_plan=value._cutover_plan,
        coverage_plan=value._coverage_plan,
        activation_plan=value._activation_plan,
        install_receipt_observation=value._install_receipt_observation,
        coverage_receipt_observation=value._coverage_receipt_observation,
        mounted_at=value._mounted_at,
    )
    if (
        value.cutover_id != prepared.evidence.cutover_id
        or value.transaction_id != prepared.evidence.transaction_id
        or value.evidence != prepared.evidence
        or not hmac.compare_digest(
            value.evidence.manifest_bytes,
            prepared.evidence.manifest_bytes,
        )
        or not hmac.compare_digest(
            value.evidence.manifest_sha256,
            prepared.evidence.manifest_sha256,
        )
    ):
        _fail("business_cutover_mount_plan_changed")
    return value


def _classification(
    status: BusinessCutoverMountReconcileStatus,
    expected: BusinessCutoverMountEvidence,
    *,
    evidence: Optional[BusinessCutoverMountEvidence] = None,
    error_code: str = "",
) -> BusinessCutoverMountClassification:
    return BusinessCutoverMountClassification(
        status=status,
        cutover_id=expected.cutover_id,
        evidence=evidence,
        error_code=error_code,
    )


def _classify_business_cutover_mount(
    plan: BusinessCutoverMountPlan,
    *,
    coverage_observation: _FreshBusinessCutoverAssetCoverageObservation,
    activation_fresh_prerequisites: BusinessCutoverActivationFreshPrerequisites,
    activation_audit_persisted: RepositoryAuditFreshObservation,
    activation_persisted: BusinessCutoverActivationFreshObservation,
    persisted: BusinessCutoverMountFreshObservation,
    activation_audit_membership: Optional[
        RepositoryAuditFreshMembershipObservation
    ] = None,
) -> BusinessCutoverMountClassification:
    plan = _require_business_cutover_mount_plan(plan)
    expected = plan.evidence
    try:
        raw = _unwrap_business_cutover_mount_fresh_observation(persisted)
        canonical = BusinessCutoverMountPersistedObservation(
            read_status=raw.read_status,
            receipt=(
                None if raw.receipt is None else _canonical_receipt(raw.receipt)
            ),
            mount=raw.mount,
            error_code=raw.error_code,
        )
    except (
        AttributeError,
        BusinessCutoverMountContractError,
        BusinessCutoverMountIntegrityError,
        TypeError,
    ):
        _fail("business_cutover_mount_fresh_observation_invalid")
    if canonical != raw:
        _fail("business_cutover_mount_persisted_observation_changed")
    if canonical.read_status is BusinessCutoverMountPersistedReadStatus.UNKNOWN:
        return _classification(
            BusinessCutoverMountReconcileStatus.UNKNOWN,
            expected,
            error_code=canonical.error_code,
        )

    dependencies = _check_live_dependencies(
        coverage_plan=plan._coverage_plan,
        coverage_observation=coverage_observation,
        activation_plan=plan._activation_plan,
        activation_fresh_prerequisites=activation_fresh_prerequisites,
        activation_audit_persisted=activation_audit_persisted,
        activation_persisted=activation_persisted,
        activation_audit_membership=activation_audit_membership,
    )
    if dependencies.status is not None:
        return _classification(
            dependencies.status,
            expected,
            error_code=dependencies.error_code,
        )

    receipt = canonical.receipt
    mount = canonical.mount
    if receipt is None and mount is None:
        return _classification(BusinessCutoverMountReconcileStatus.ABSENT, expected)
    if receipt is None:
        return _classification(
            BusinessCutoverMountReconcileStatus.CORRUPT,
            expected,
            error_code="business_cutover_mount_without_receipt",
        )
    if mount is None:
        identity_conflict = (
            receipt.transaction_id == expected.transaction_id
            and receipt.kind != expected.receipt_kind
        )
        return _classification(
            (
                BusinessCutoverMountReconcileStatus.CONFLICT
                if identity_conflict
                else BusinessCutoverMountReconcileStatus.CORRUPT
            ),
            expected,
            error_code=(
                "business_cutover_mount_receipt_identity_conflict"
                if identity_conflict
                else "business_cutover_mount_receipt_without_row"
            ),
        )
    try:
        observed = _rebuild_mount_evidence(mount)
    except BusinessCutoverMountIntegrityError:
        return _classification(
            BusinessCutoverMountReconcileStatus.CORRUPT,
            expected,
            error_code="business_cutover_mount_row_corrupt",
        )
    if receipt != observed.receipt_parent:
        identity_conflict = (
            receipt.transaction_id == expected.transaction_id
            and receipt.kind != expected.receipt_kind
        )
        return _classification(
            (
                BusinessCutoverMountReconcileStatus.CONFLICT
                if identity_conflict
                else BusinessCutoverMountReconcileStatus.CORRUPT
            ),
            expected,
            error_code=(
                "business_cutover_mount_receipt_identity_conflict"
                if identity_conflict
                else "business_cutover_mount_receipt_row_mismatch"
            ),
        )
    if (
        observed.cutover_id != expected.cutover_id
        or observed.transaction_id != expected.transaction_id
    ):
        return _classification(
            BusinessCutoverMountReconcileStatus.CONFLICT,
            expected,
            error_code="business_cutover_mount_identity_conflict",
        )
    if observed != expected:
        return _classification(
            BusinessCutoverMountReconcileStatus.CONFLICT,
            expected,
            error_code="business_cutover_mount_content_conflict",
        )
    if receipt != expected.receipt_parent:
        return _classification(
            BusinessCutoverMountReconcileStatus.CORRUPT,
            expected,
            error_code="business_cutover_mount_receipt_parent_corrupt",
        )
    return _classification(
        BusinessCutoverMountReconcileStatus.EXACT,
        expected,
        evidence=observed,
    )


__all__ = ()
