"""Pure v2 activation issuer, verifier, and fresh-state classifier.

The v1 cutover plan is rebuilt in full, but its pre-generated ``join`` value is
never consumed as activation evidence.  The only activation-safe path is an
exact current final admission, the exact v1 install evidence, and a freshly
classified exact INSTALL audit certificate whose observed runtime and schema
close over that install.

This module has no SQLite, filesystem, lock, clock, or callback capability.
It emits expected evidence and can reconcile a fresh persisted observation;
the future fixed-SQL writer remains responsible for one atomic runtime update,
generic receipt insert, and specialised activation insert.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any, NamedTuple, Optional

from business_codec import BusinessCodecError, encode_payload_bounded
from business_cutover_activation_contract import (
    BUSINESS_CUTOVER_ACTIVATION_MANIFEST_SCHEMA,
    BUSINESS_CUTOVER_ACTIVATION_AFTER_AUTHORITY_STATE,
    BUSINESS_CUTOVER_ACTIVATION_AFTER_STATE_REVISION,
    BUSINESS_CUTOVER_ACTIVATION_BEFORE_AUTHORITY_STATE,
    BUSINESS_CUTOVER_ACTIVATION_BEFORE_STATE_REVISION,
    BUSINESS_CUTOVER_ACTIVATION_PLAN_STATUS,
    BUSINESS_CUTOVER_ACTIVATION_PROTOCOL,
    BUSINESS_CUTOVER_ACTIVATION_STATUS,
    BUSINESS_CUTOVER_ACTIVATION_TRANSACTION_KIND,
    BUSINESS_CUTOVER_ACTIVATION_TRANSACTION_PREFIX,
    MAX_BUSINESS_CUTOVER_ACTIVATION_MANIFEST_BYTES,
    MAX_BUSINESS_CUTOVER_ACTIVATION_MANIFEST_NODES,
    BusinessCutoverActivationClassification,
    BusinessCutoverActivationContractError,
    BusinessCutoverActivationEvidence,
    BusinessCutoverActivationFreshObservation,
    BusinessCutoverActivationFreshPrerequisites,
    BusinessCutoverActivationGenericReceiptEvidence,
    BusinessCutoverActivationPersistedObservation,
    BusinessCutoverActivationPersistedReadStatus,
    BusinessCutoverActivationPrerequisiteSnapshot,
    BusinessCutoverActivationReconcileStatus,
    _unwrap_business_cutover_activation_fresh_observation,
    _unwrap_business_cutover_activation_fresh_prerequisites,
)
from business_cutover_contract import (
    BusinessCutoverAdmission,
    BusinessCutoverContractError,
    BusinessCutoverInstallEvidence,
    BusinessCutoverRuntimeBinding,
    digest_business_cutover_admission,
)
from business_cutover_integrity import (
    BusinessCutoverIntegrityError,
    BusinessCutoverPlan,
    _require_business_cutover_plan,
)
from repository_audit_contract import (
    RepositoryAuditAuthorityState,
    RepositoryAuditCertificateEvidence,
    RepositoryAuditContractError,
    RepositoryAuditFreshObservation,
    RepositoryAuditFreshMembershipObservation,
    RepositoryAuditPurpose,
    RepositoryAuditReconcileStatus,
    RepositoryAuditRuntimeBinding,
    RepositoryAuditSchemaObservation,
)
from repository_audit_integrity import (
    RepositoryAuditCertificatePlan,
    RepositoryAuditIntegrityError,
    _classify_repository_audit_certificate,
    _classify_repository_audit_certificate_membership,
    _require_repository_audit_certificate_plan,
)


BUSINESS_CUTOVER_ACTIVATION_MANIFEST_HASH_DOMAIN = (
    b"ze.business-cutover.activation-manifest-digest.v2"
)

_PLAN_ISSUER = object()


class BusinessCutoverActivationIntegrityError(ValueError):
    """Stable payload-free activation integrity failure."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        if type(code) is not str or not code:
            raise TypeError("code must be a non-empty exact string")
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise BusinessCutoverActivationIntegrityError(code)


def _frame(hasher: Any, value: bytes) -> None:
    hasher.update(len(value).to_bytes(8, "big"))
    hasher.update(value)


def _manifest_digest(value: bytes) -> bytes:
    hasher = hashlib.sha256()
    _frame(hasher, BUSINESS_CUTOVER_ACTIVATION_MANIFEST_HASH_DOMAIN)
    _frame(hasher, value)
    return hasher.digest()


def _encode_manifest(value: Any) -> bytes:
    try:
        return encode_payload_bounded(
            value,
            MAX_BUSINESS_CUTOVER_ACTIVATION_MANIFEST_BYTES,
            MAX_BUSINESS_CUTOVER_ACTIVATION_MANIFEST_NODES,
        )
    except BusinessCodecError as exc:
        raise BusinessCutoverActivationIntegrityError(
            "business_cutover_activation_manifest_encoding_failed"
        ) from exc


def _canonical_admission(value: Any) -> BusinessCutoverAdmission:
    if type(value) is not BusinessCutoverAdmission:
        _fail("business_cutover_activation_admission_type_invalid")
    try:
        rebuilt = BusinessCutoverAdmission(
            authority_state=value.authority_state,
            authority_epoch=value.authority_epoch,
            revision=value.revision,
            cutover_id=value.cutover_id,
        )
    except (AttributeError, BusinessCutoverContractError, TypeError):
        _fail("business_cutover_activation_admission_changed")
    if rebuilt != value:
        _fail("business_cutover_activation_admission_changed")
    return rebuilt


def _canonical_runtime(value: Any) -> BusinessCutoverRuntimeBinding:
    if type(value) is not BusinessCutoverRuntimeBinding:
        _fail("business_cutover_activation_runtime_type_invalid")
    try:
        rebuilt = BusinessCutoverRuntimeBinding(
            authority=value.authority,
            authority_state=value.authority_state,
            repository_epoch=value.repository_epoch,
            cutover_id=value.cutover_id,
            source_admission_revision=value.source_admission_revision,
            repository_revision=value.repository_revision,
            state_revision=value.state_revision,
            runtime_generation=value.runtime_generation,
            code_digest=value.code_digest,
            schema_version=value.schema_version,
        )
    except (AttributeError, BusinessCutoverContractError, TypeError):
        _fail("business_cutover_activation_runtime_changed")
    if rebuilt != value:
        _fail("business_cutover_activation_runtime_changed")
    return rebuilt


def _canonical_schema(value: Any) -> RepositoryAuditSchemaObservation:
    if type(value) is not RepositoryAuditSchemaObservation:
        _fail("business_cutover_activation_schema_type_invalid")
    try:
        rebuilt = RepositoryAuditSchemaObservation(
            user_version=value.user_version,
            schema_fingerprint=value.schema_fingerprint,
            migration_ledger_count=value.migration_ledger_count,
            migration_ledger_sha256=value.migration_ledger_sha256,
        )
    except (AttributeError, RepositoryAuditContractError, TypeError):
        _fail("business_cutover_activation_schema_changed")
    if rebuilt != value:
        _fail("business_cutover_activation_schema_changed")
    return rebuilt


def _canonical_receipt(
    value: Any,
) -> BusinessCutoverActivationGenericReceiptEvidence:
    if type(value) is not BusinessCutoverActivationGenericReceiptEvidence:
        _fail("business_cutover_activation_receipt_type_invalid")
    try:
        rebuilt = BusinessCutoverActivationGenericReceiptEvidence(
            transaction_id=value.transaction_id,
            kind=value.kind,
            committed_at=value.committed_at,
        )
    except (AttributeError, BusinessCutoverActivationContractError, TypeError):
        _fail("business_cutover_activation_receipt_changed")
    if rebuilt != value:
        _fail("business_cutover_activation_receipt_changed")
    return rebuilt


def _runtime_manifest(value: BusinessCutoverRuntimeBinding) -> dict:
    return {
        "authority": value.authority,
        "authority_state": value.authority_state,
        "repository_epoch": value.repository_epoch,
        "cutover_id": value.cutover_id,
        "source_admission_revision": value.source_admission_revision,
        "repository_revision": value.repository_revision,
        "state_revision": value.state_revision,
        "runtime_generation": value.runtime_generation,
        "code_digest": value.code_digest.hex(),
        "schema_version": value.schema_version,
    }


def _schema_manifest(value: RepositoryAuditSchemaObservation) -> dict:
    return {
        "user_version": value.user_version,
        "schema_fingerprint": value.schema_fingerprint,
        "migration_ledger_count": value.migration_ledger_count,
        "migration_ledger_sha256": value.migration_ledger_sha256.hex(),
    }


def _admission_manifest(
    value: BusinessCutoverAdmission,
    digest: bytes,
) -> dict:
    manifest = value.canonical_value()
    manifest["sha256"] = digest.hex()
    return manifest


def _build_manifest(
    *,
    cutover_id: str,
    receipt: BusinessCutoverActivationGenericReceiptEvidence,
    final_admission: BusinessCutoverAdmission,
    final_admission_sha256: bytes,
    before_runtime: BusinessCutoverRuntimeBinding,
    after_runtime: BusinessCutoverRuntimeBinding,
    install_transaction_id: str,
    install_receipt_kind: str,
    install_manifest_sha256: bytes,
    audit_certificate_id: str,
    audit_certificate_sequence: int,
    audit_purpose: RepositoryAuditPurpose,
    audit_transaction_id: str,
    audit_receipt_kind: str,
    audit_manifest_sha256: bytes,
    audit_certificate_tail_sha256: bytes,
    audit_completed_at: str,
    expected_migration_ledger_count: int,
    expected_migration_ledger_sha256: bytes,
    actual_schema: RepositoryAuditSchemaObservation,
    activated_at: str,
) -> bytes:
    return _encode_manifest(
        {
            "schema": BUSINESS_CUTOVER_ACTIVATION_MANIFEST_SCHEMA,
            "protocol": BUSINESS_CUTOVER_ACTIVATION_PROTOCOL,
            "status": BUSINESS_CUTOVER_ACTIVATION_STATUS,
            "manifest_hash_domain": (
                BUSINESS_CUTOVER_ACTIVATION_MANIFEST_HASH_DOMAIN.decode("ascii")
            ),
            "activation": {
                "cutover_id": cutover_id,
                "activated_at": activated_at,
            },
            "receipt_parent": {
                "ledger": "transaction_receipts",
                "transaction_id": receipt.transaction_id,
                "kind": receipt.kind,
                "committed_at": receipt.committed_at,
                "specialised_protocol": BUSINESS_CUTOVER_ACTIVATION_PROTOCOL,
                "specialised_cutover_id": cutover_id,
            },
            "final_admission": _admission_manifest(
                final_admission,
                final_admission_sha256,
            ),
            "install": {
                "transaction_id": install_transaction_id,
                "receipt_kind": install_receipt_kind,
                "manifest_sha256": install_manifest_sha256.hex(),
            },
            "audit_certificate": {
                "certificate_id": audit_certificate_id,
                "sequence": audit_certificate_sequence,
                "purpose": audit_purpose.value,
                "transaction_id": audit_transaction_id,
                "receipt_kind": audit_receipt_kind,
                "manifest_sha256": audit_manifest_sha256.hex(),
                "certificate_tail_sha256": audit_certificate_tail_sha256.hex(),
                "completed_at": audit_completed_at,
            },
            "runtime_transition": {
                "before": _runtime_manifest(before_runtime),
                "after": _runtime_manifest(after_runtime),
            },
            "target_schema": {
                "user_version": actual_schema.user_version,
                "schema_fingerprint": actual_schema.schema_fingerprint,
                "migration_ledger_count": expected_migration_ledger_count,
                "migration_ledger_sha256": (
                    expected_migration_ledger_sha256.hex()
                ),
            },
            "actual_schema": _schema_manifest(actual_schema),
        }
    )


class _PrerequisiteCheck(NamedTuple):
    audit_certificate: Optional[RepositoryAuditCertificateEvidence]
    failure_status: Optional[BusinessCutoverActivationReconcileStatus]
    error_code: str


def _check_activation_prerequisites(
    cutover_plan: Any,
    fresh_prerequisites: Any,
    audit_plan: Any,
    audit_persisted: Any,
    *,
    allow_historical_audit: bool = False,
    audit_membership: Any = None,
    expected_migration_ledger_count: Optional[int] = None,
    expected_migration_ledger_sha256: Optional[bytes] = None,
) -> _PrerequisiteCheck:
    """Rebuild and freshly reconcile every authority needed to open writes."""

    try:
        exact_cutover = _require_business_cutover_plan(cutover_plan)
    except (BusinessCutoverIntegrityError, TypeError):
        _fail("business_cutover_activation_cutover_plan_invalid")
    try:
        exact_audit_plan = _require_repository_audit_certificate_plan(audit_plan)
    except (RepositoryAuditIntegrityError, TypeError):
        _fail("business_cutover_activation_audit_plan_invalid")

    try:
        snapshot = _unwrap_business_cutover_activation_fresh_prerequisites(
            fresh_prerequisites
        )
    except (BusinessCutoverActivationContractError, TypeError):
        _fail("business_cutover_activation_fresh_prerequisites_invalid")
    current = _canonical_admission(snapshot.current_admission)
    observed_install = snapshot.observed_install
    if (
        snapshot.expected_migration_ledger_count
        != exact_cutover.expected_target_schema_version
    ):
        return _PrerequisiteCheck(
            None,
            BusinessCutoverActivationReconcileStatus.CORRUPT,
            "business_cutover_activation_target_ledger_count_invalid",
        )
    if expected_migration_ledger_count is not None and (
        snapshot.expected_migration_ledger_count
        != expected_migration_ledger_count
        or expected_migration_ledger_sha256 is None
        or not hmac.compare_digest(
            snapshot.expected_migration_ledger_sha256,
            expected_migration_ledger_sha256,
        )
    ):
        return _PrerequisiteCheck(
            None,
            BusinessCutoverActivationReconcileStatus.CONFLICT,
            "business_cutover_activation_target_ledger_changed",
        )
    if current != exact_cutover.final_admission:
        return _PrerequisiteCheck(
            None,
            BusinessCutoverActivationReconcileStatus.CONFLICT,
            "business_cutover_activation_admission_not_final",
        )
    if observed_install is None:
        return _PrerequisiteCheck(
            None,
            BusinessCutoverActivationReconcileStatus.CORRUPT,
            "business_cutover_activation_install_missing",
        )
    if type(observed_install) is not BusinessCutoverInstallEvidence:
        _fail("business_cutover_activation_install_type_invalid")
    if observed_install.cutover_id != exact_cutover.cutover_id:
        return _PrerequisiteCheck(
            None,
            BusinessCutoverActivationReconcileStatus.CONFLICT,
            "business_cutover_activation_install_identity_conflict",
        )
    if observed_install != exact_cutover.install:
        return _PrerequisiteCheck(
            None,
            BusinessCutoverActivationReconcileStatus.CORRUPT,
            "business_cutover_activation_install_mismatch",
        )
    if type(audit_persisted) is not RepositoryAuditFreshObservation:
        _fail("business_cutover_activation_audit_observation_type_invalid")
    try:
        audit_result = _classify_repository_audit_certificate(
            exact_audit_plan,
            audit_persisted,
        )
    except (RepositoryAuditIntegrityError, TypeError):
        _fail("business_cutover_activation_audit_observation_invalid")
    if (
        audit_result.status is RepositoryAuditReconcileStatus.CONFLICT
        and audit_result.error_code
        == "repository_audit_certificate_head_conflict"
        and allow_historical_audit
        and audit_membership is not None
    ):
        if type(audit_membership) is not RepositoryAuditFreshMembershipObservation:
            _fail("business_cutover_activation_audit_membership_type_invalid")
        try:
            membership_result = (
                _classify_repository_audit_certificate_membership(
                    exact_audit_plan,
                    audit_membership,
                )
            )
        except (RepositoryAuditIntegrityError, TypeError):
            _fail("business_cutover_activation_audit_membership_invalid")
        if membership_result.status is RepositoryAuditReconcileStatus.EXACT:
            audit_result = membership_result
        elif membership_result.status is RepositoryAuditReconcileStatus.UNKNOWN:
            return _PrerequisiteCheck(
                None,
                BusinessCutoverActivationReconcileStatus.UNKNOWN,
                "business_cutover_activation_audit_membership_unknown",
            )
        elif membership_result.status is RepositoryAuditReconcileStatus.CONFLICT:
            return _PrerequisiteCheck(
                None,
                BusinessCutoverActivationReconcileStatus.CONFLICT,
                "business_cutover_activation_audit_membership_conflict",
            )
        else:
            return _PrerequisiteCheck(
                None,
                BusinessCutoverActivationReconcileStatus.CORRUPT,
                "business_cutover_activation_audit_membership_corrupt",
            )
    if audit_result.status is RepositoryAuditReconcileStatus.UNKNOWN:
        return _PrerequisiteCheck(
            None,
            BusinessCutoverActivationReconcileStatus.UNKNOWN,
            "business_cutover_activation_audit_unknown",
        )
    if audit_result.status is RepositoryAuditReconcileStatus.ABSENT:
        return _PrerequisiteCheck(
            None,
            BusinessCutoverActivationReconcileStatus.CORRUPT,
            "business_cutover_activation_audit_absent",
        )
    if audit_result.status is RepositoryAuditReconcileStatus.CONFLICT:
        return _PrerequisiteCheck(
            None,
            BusinessCutoverActivationReconcileStatus.CONFLICT,
            "business_cutover_activation_audit_conflict",
        )
    if audit_result.status is RepositoryAuditReconcileStatus.CORRUPT:
        return _PrerequisiteCheck(
            None,
            BusinessCutoverActivationReconcileStatus.CORRUPT,
            "business_cutover_activation_audit_corrupt",
        )
    audit = audit_result.evidence
    if type(audit) is not RepositoryAuditCertificateEvidence:
        _fail("business_cutover_activation_audit_exact_without_evidence")

    install_runtime = exact_cutover.install.runtime
    audit_runtime = audit.runtime
    if audit.purpose is not RepositoryAuditPurpose.INSTALL:
        return _PrerequisiteCheck(
            None,
            BusinessCutoverActivationReconcileStatus.CORRUPT,
            "business_cutover_activation_audit_purpose_invalid",
        )
    if (
        type(audit_runtime) is not RepositoryAuditRuntimeBinding
        or audit_runtime.authority_state is not RepositoryAuditAuthorityState.READ_ONLY
        or audit_runtime.authority != install_runtime.authority
        or audit_runtime.authority_state.value != install_runtime.authority_state
        or audit_runtime.repository_epoch != install_runtime.repository_epoch
        or audit_runtime.cutover_id != install_runtime.cutover_id
        or audit_runtime.source_admission_revision
        != install_runtime.source_admission_revision
        or audit_runtime.repository_revision != install_runtime.repository_revision
        or audit_runtime.state_revision != install_runtime.state_revision
        or audit_runtime.runtime_generation != install_runtime.runtime_generation
        or not hmac.compare_digest(
            audit_runtime.code_digest,
            install_runtime.code_digest,
        )
        or audit_runtime.schema_version != install_runtime.schema_version
    ):
        return _PrerequisiteCheck(
            None,
            BusinessCutoverActivationReconcileStatus.CORRUPT,
            "business_cutover_activation_audit_runtime_mismatch",
        )
    coverage = audit.coverage
    if (
        coverage.covered_business_revision
        != install_runtime.repository_revision
        or coverage.cutover_install_manifest_sha256 is None
        or not hmac.compare_digest(
            coverage.cutover_install_manifest_sha256,
            exact_cutover.install.manifest_sha256,
        )
    ):
        return _PrerequisiteCheck(
            None,
            BusinessCutoverActivationReconcileStatus.CORRUPT,
            "business_cutover_activation_audit_coverage_mismatch",
        )
    if (
        audit.schema.user_version
        != exact_cutover.expected_target_schema_version
        or audit.schema.schema_fingerprint
        != exact_cutover.expected_target_schema_fingerprint
        or audit.schema.migration_ledger_count
        != snapshot.expected_migration_ledger_count
        or not hmac.compare_digest(
            audit.schema.migration_ledger_sha256,
            snapshot.expected_migration_ledger_sha256,
        )
    ):
        return _PrerequisiteCheck(
            None,
            BusinessCutoverActivationReconcileStatus.CORRUPT,
            "business_cutover_activation_audit_schema_mismatch",
        )
    return _PrerequisiteCheck(audit, None, "")


class _PreparedActivation(NamedTuple):
    cutover_plan: BusinessCutoverPlan
    audit_plan: RepositoryAuditCertificatePlan
    evidence: BusinessCutoverActivationEvidence


def _prepare_activation(
    *,
    cutover_plan: Any,
    fresh_prerequisites: Any,
    audit_plan: Any,
    audit_persisted: Any,
    activated_at: Any,
) -> _PreparedActivation:
    prerequisites = _check_activation_prerequisites(
        cutover_plan,
        fresh_prerequisites,
        audit_plan,
        audit_persisted,
    )
    if prerequisites.failure_status is not None:
        _fail(prerequisites.error_code)
    audit = prerequisites.audit_certificate
    if audit is None:
        _fail("business_cutover_activation_audit_exact_without_evidence")
    exact_cutover = _require_business_cutover_plan(cutover_plan)
    exact_audit_plan = _require_repository_audit_certificate_plan(audit_plan)
    try:
        prerequisite_snapshot = (
            _unwrap_business_cutover_activation_fresh_prerequisites(
                fresh_prerequisites
            )
        )
    except (BusinessCutoverActivationContractError, TypeError):
        _fail("business_cutover_activation_fresh_prerequisites_invalid")

    install = exact_cutover.install
    before_runtime = _canonical_runtime(install.runtime)
    after_runtime = BusinessCutoverRuntimeBinding(
        authority=before_runtime.authority,
        authority_state=BUSINESS_CUTOVER_ACTIVATION_AFTER_AUTHORITY_STATE,
        repository_epoch=before_runtime.repository_epoch,
        cutover_id=before_runtime.cutover_id,
        source_admission_revision=before_runtime.source_admission_revision,
        repository_revision=before_runtime.repository_revision,
        state_revision=BUSINESS_CUTOVER_ACTIVATION_AFTER_STATE_REVISION,
        runtime_generation=before_runtime.runtime_generation,
        code_digest=before_runtime.code_digest,
        schema_version=before_runtime.schema_version,
    )
    if (
        before_runtime.authority_state
        != BUSINESS_CUTOVER_ACTIVATION_BEFORE_AUTHORITY_STATE
        or before_runtime.state_revision
        != BUSINESS_CUTOVER_ACTIVATION_BEFORE_STATE_REVISION
    ):
        _fail("business_cutover_activation_install_runtime_invalid")

    final_admission = _canonical_admission(exact_cutover.final_admission)
    final_digest = digest_business_cutover_admission(final_admission)
    if not hmac.compare_digest(
        final_digest,
        exact_cutover.closure.final_admission_sha256,
    ):
        _fail("business_cutover_activation_final_admission_digest_mismatch")

    try:
        receipt = BusinessCutoverActivationGenericReceiptEvidence(
            transaction_id=(
                BUSINESS_CUTOVER_ACTIVATION_TRANSACTION_PREFIX
                + exact_cutover.cutover_id
            ),
            kind=BUSINESS_CUTOVER_ACTIVATION_TRANSACTION_KIND,
            committed_at=activated_at,
        )
    except (BusinessCutoverActivationContractError, TypeError) as exc:
        raise BusinessCutoverActivationIntegrityError(
            "business_cutover_activation_activated_at_invalid"
        ) from exc
    if activated_at < audit.findings.completed_at:
        _fail("business_cutover_activation_before_audit_completed")

    actual_schema = _canonical_schema(audit.schema)
    manifest_bytes = _build_manifest(
        cutover_id=exact_cutover.cutover_id,
        receipt=receipt,
        final_admission=final_admission,
        final_admission_sha256=final_digest,
        before_runtime=before_runtime,
        after_runtime=after_runtime,
        install_transaction_id=install.transaction_id,
        install_receipt_kind=install.receipt_kind,
        install_manifest_sha256=install.manifest_sha256,
        audit_certificate_id=audit.certificate_id,
        audit_certificate_sequence=audit.sequence,
        audit_purpose=audit.purpose,
        audit_transaction_id=audit.transaction_id,
        audit_receipt_kind=audit.receipt_kind,
        audit_manifest_sha256=audit.manifest_sha256,
        audit_certificate_tail_sha256=audit.certificate_tail_sha256,
        audit_completed_at=audit.findings.completed_at,
        expected_migration_ledger_count=(
            prerequisite_snapshot.expected_migration_ledger_count
        ),
        expected_migration_ledger_sha256=(
            prerequisite_snapshot.expected_migration_ledger_sha256
        ),
        actual_schema=actual_schema,
        activated_at=activated_at,
    )
    manifest_sha256 = _manifest_digest(manifest_bytes)
    try:
        evidence = BusinessCutoverActivationEvidence(
            cutover_id=exact_cutover.cutover_id,
            receipt_parent=receipt,
            final_admission=final_admission,
            final_admission_sha256=final_digest,
            before_runtime=before_runtime,
            after_runtime=after_runtime,
            install_transaction_id=install.transaction_id,
            install_receipt_kind=install.receipt_kind,
            install_manifest_sha256=install.manifest_sha256,
            audit_certificate_id=audit.certificate_id,
            audit_certificate_sequence=audit.sequence,
            audit_transaction_id=audit.transaction_id,
            audit_receipt_kind=audit.receipt_kind,
            audit_manifest_sha256=audit.manifest_sha256,
            audit_certificate_tail_sha256=audit.certificate_tail_sha256,
            audit_completed_at=audit.findings.completed_at,
            expected_migration_ledger_count=(
                prerequisite_snapshot.expected_migration_ledger_count
            ),
            expected_migration_ledger_sha256=(
                prerequisite_snapshot.expected_migration_ledger_sha256
            ),
            actual_schema=actual_schema,
            activated_at=activated_at,
            manifest_bytes=manifest_bytes,
            manifest_sha256=manifest_sha256,
        )
    except (BusinessCutoverActivationContractError, TypeError) as exc:
        raise BusinessCutoverActivationIntegrityError(
            "business_cutover_activation_evidence_invalid"
        ) from exc
    return _PreparedActivation(exact_cutover, exact_audit_plan, evidence)


class BusinessCutoverActivationPlan:
    """Issuer-stamped expected v2 activation; never persistence evidence."""

    __slots__ = (
        "cutover_id",
        "transaction_id",
        "evidence",
        "protocol",
        "status",
        "_cutover_plan",
        "_fresh_prerequisites",
        "_audit_plan",
        "_audit_persisted",
        "_activated_at",
        "_issuer",
    )

    def __init__(
        self,
        prepared: _PreparedActivation,
        *,
        fresh_prerequisites: BusinessCutoverActivationFreshPrerequisites,
        audit_persisted: RepositoryAuditFreshObservation,
        activated_at: str,
        _issuer: Any,
    ) -> None:
        if _issuer is not _PLAN_ISSUER:
            _fail("business_cutover_activation_plan_issuer_invalid")
        object.__setattr__(self, "cutover_id", prepared.evidence.cutover_id)
        object.__setattr__(
            self,
            "transaction_id",
            prepared.evidence.transaction_id,
        )
        object.__setattr__(self, "evidence", prepared.evidence)
        object.__setattr__(
            self,
            "protocol",
            BUSINESS_CUTOVER_ACTIVATION_PROTOCOL,
        )
        object.__setattr__(
            self,
            "status",
            BUSINESS_CUTOVER_ACTIVATION_PLAN_STATUS,
        )
        object.__setattr__(self, "_cutover_plan", prepared.cutover_plan)
        object.__setattr__(
            self,
            "_fresh_prerequisites",
            fresh_prerequisites,
        )
        object.__setattr__(self, "_audit_plan", prepared.audit_plan)
        object.__setattr__(self, "_audit_persisted", audit_persisted)
        object.__setattr__(self, "_activated_at", activated_at)
        object.__setattr__(self, "_issuer", _issuer)

    def __setattr__(self, name: str, value: Any) -> None:
        del name, value
        raise AttributeError("BusinessCutoverActivationPlan is immutable")


def _issue_business_cutover_activation_plan(
    *,
    cutover_plan: BusinessCutoverPlan,
    fresh_prerequisites: BusinessCutoverActivationFreshPrerequisites,
    audit_plan: RepositoryAuditCertificatePlan,
    audit_persisted: RepositoryAuditFreshObservation,
    activated_at: str,
) -> BusinessCutoverActivationPlan:
    """Issue only after all four external prerequisites are currently exact."""

    prepared = _prepare_activation(
        cutover_plan=cutover_plan,
        fresh_prerequisites=fresh_prerequisites,
        audit_plan=audit_plan,
        audit_persisted=audit_persisted,
        activated_at=activated_at,
    )
    return BusinessCutoverActivationPlan(
        prepared,
        fresh_prerequisites=fresh_prerequisites,
        audit_persisted=audit_persisted,
        activated_at=activated_at,
        _issuer=_PLAN_ISSUER,
    )


def _rebuild_activation_evidence(value: Any) -> BusinessCutoverActivationEvidence:
    if type(value) is not BusinessCutoverActivationEvidence:
        _fail("business_cutover_activation_evidence_type_invalid")
    try:
        receipt = _canonical_receipt(value.receipt_parent)
        final_admission = _canonical_admission(value.final_admission)
        before_runtime = _canonical_runtime(value.before_runtime)
        after_runtime = _canonical_runtime(value.after_runtime)
        actual_schema = _canonical_schema(value.actual_schema)
        manifest_bytes = _build_manifest(
            cutover_id=value.cutover_id,
            receipt=receipt,
            final_admission=final_admission,
            final_admission_sha256=value.final_admission_sha256,
            before_runtime=before_runtime,
            after_runtime=after_runtime,
            install_transaction_id=value.install_transaction_id,
            install_receipt_kind=value.install_receipt_kind,
            install_manifest_sha256=value.install_manifest_sha256,
            audit_certificate_id=value.audit_certificate_id,
            audit_certificate_sequence=value.audit_certificate_sequence,
            audit_purpose=RepositoryAuditPurpose.INSTALL,
            audit_transaction_id=value.audit_transaction_id,
            audit_receipt_kind=value.audit_receipt_kind,
            audit_manifest_sha256=value.audit_manifest_sha256,
            audit_certificate_tail_sha256=value.audit_certificate_tail_sha256,
            audit_completed_at=value.audit_completed_at,
            expected_migration_ledger_count=(
                value.expected_migration_ledger_count
            ),
            expected_migration_ledger_sha256=(
                value.expected_migration_ledger_sha256
            ),
            actual_schema=actual_schema,
            activated_at=value.activated_at,
        )
        manifest_sha256 = _manifest_digest(manifest_bytes)
        rebuilt = BusinessCutoverActivationEvidence(
            cutover_id=value.cutover_id,
            receipt_parent=receipt,
            final_admission=final_admission,
            final_admission_sha256=value.final_admission_sha256,
            before_runtime=before_runtime,
            after_runtime=after_runtime,
            install_transaction_id=value.install_transaction_id,
            install_receipt_kind=value.install_receipt_kind,
            install_manifest_sha256=value.install_manifest_sha256,
            audit_certificate_id=value.audit_certificate_id,
            audit_certificate_sequence=value.audit_certificate_sequence,
            audit_transaction_id=value.audit_transaction_id,
            audit_receipt_kind=value.audit_receipt_kind,
            audit_manifest_sha256=value.audit_manifest_sha256,
            audit_certificate_tail_sha256=value.audit_certificate_tail_sha256,
            audit_completed_at=value.audit_completed_at,
            expected_migration_ledger_count=(
                value.expected_migration_ledger_count
            ),
            expected_migration_ledger_sha256=(
                value.expected_migration_ledger_sha256
            ),
            actual_schema=actual_schema,
            activated_at=value.activated_at,
            manifest_bytes=manifest_bytes,
            manifest_sha256=manifest_sha256,
        )
    except (
        AttributeError,
        BusinessCutoverActivationContractError,
        BusinessCutoverActivationIntegrityError,
        BusinessCutoverContractError,
        RepositoryAuditContractError,
        TypeError,
    ):
        _fail("business_cutover_activation_evidence_changed")
    if (
        rebuilt != value
        or not hmac.compare_digest(rebuilt.manifest_bytes, value.manifest_bytes)
        or not hmac.compare_digest(rebuilt.manifest_sha256, value.manifest_sha256)
    ):
        _fail("business_cutover_activation_evidence_changed")
    return rebuilt


def _require_business_cutover_activation_evidence(
    value: Any,
) -> BusinessCutoverActivationEvidence:
    """Rebuild every field and the v2 manifest digest before trusting a row."""

    return _rebuild_activation_evidence(value)


def _require_business_cutover_activation_plan(
    value: Any,
) -> BusinessCutoverActivationPlan:
    if (
        type(value) is not BusinessCutoverActivationPlan
        or getattr(value, "_issuer", None) is not _PLAN_ISSUER
        or getattr(value, "protocol", None)
        != BUSINESS_CUTOVER_ACTIVATION_PROTOCOL
        or getattr(value, "status", None)
        != BUSINESS_CUTOVER_ACTIVATION_PLAN_STATUS
    ):
        _fail("business_cutover_activation_plan_invalid")
    prepared = _prepare_activation(
        cutover_plan=value._cutover_plan,
        fresh_prerequisites=value._fresh_prerequisites,
        audit_plan=value._audit_plan,
        audit_persisted=value._audit_persisted,
        activated_at=value._activated_at,
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
        _fail("business_cutover_activation_plan_changed")
    return value


def _classification(
    status: BusinessCutoverActivationReconcileStatus,
    expected: BusinessCutoverActivationEvidence,
    *,
    evidence: Optional[BusinessCutoverActivationEvidence] = None,
    error_code: str = "",
) -> BusinessCutoverActivationClassification:
    return BusinessCutoverActivationClassification(
        status=status,
        cutover_id=expected.cutover_id,
        evidence=evidence,
        error_code=error_code,
    )


def _classify_business_cutover_activation(
    plan: BusinessCutoverActivationPlan,
    *,
    fresh_prerequisites: BusinessCutoverActivationFreshPrerequisites,
    audit_persisted: RepositoryAuditFreshObservation,
    persisted: BusinessCutoverActivationFreshObservation,
    audit_membership: Optional[
        RepositoryAuditFreshMembershipObservation
    ] = None,
) -> BusinessCutoverActivationClassification:
    """Classify one fresh read only after freshly rechecking every prerequisite."""

    plan = _require_business_cutover_activation_plan(plan)
    expected = plan.evidence
    try:
        persisted = _unwrap_business_cutover_activation_fresh_observation(
            persisted
        )
    except (BusinessCutoverActivationContractError, TypeError):
        _fail("business_cutover_activation_fresh_observation_invalid")
    try:
        rebuilt_persisted = BusinessCutoverActivationPersistedObservation(
            read_status=persisted.read_status,
            receipt=(
                None
                if persisted.receipt is None
                else _canonical_receipt(persisted.receipt)
            ),
            activation=persisted.activation,
            runtime=(
                None
                if persisted.runtime is None
                else _canonical_runtime(persisted.runtime)
            ),
            error_code=persisted.error_code,
        )
    except (
        AttributeError,
        BusinessCutoverActivationContractError,
        BusinessCutoverActivationIntegrityError,
        TypeError,
    ):
        _fail("business_cutover_activation_persisted_observation_changed")
    if rebuilt_persisted != persisted:
        _fail("business_cutover_activation_persisted_observation_changed")
    persisted = rebuilt_persisted
    allow_historical_audit = (
        persisted.read_status
        is BusinessCutoverActivationPersistedReadStatus.COMPLETE
        and persisted.receipt is not None
        and persisted.activation is not None
        and persisted.runtime == expected.after_runtime
    )
    prerequisites = _check_activation_prerequisites(
        plan._cutover_plan,
        fresh_prerequisites,
        plan._audit_plan,
        audit_persisted,
        allow_historical_audit=allow_historical_audit,
        audit_membership=audit_membership,
        expected_migration_ledger_count=(
            expected.expected_migration_ledger_count
        ),
        expected_migration_ledger_sha256=(
            expected.expected_migration_ledger_sha256
        ),
    )
    if persisted.read_status is BusinessCutoverActivationPersistedReadStatus.UNKNOWN:
        return _classification(
            BusinessCutoverActivationReconcileStatus.UNKNOWN,
            expected,
            error_code=persisted.error_code,
        )
    if prerequisites.failure_status is not None:
        return _classification(
            prerequisites.failure_status,
            expected,
            error_code=prerequisites.error_code,
        )

    receipt = persisted.receipt
    activation = persisted.activation
    runtime = persisted.runtime
    if runtime is None:
        return _classification(
            BusinessCutoverActivationReconcileStatus.CORRUPT,
            expected,
            error_code="business_cutover_activation_runtime_missing",
        )
    if receipt is None and activation is None:
        if runtime == expected.after_runtime:
            return _classification(
                BusinessCutoverActivationReconcileStatus.CORRUPT,
                expected,
                error_code="business_cutover_activation_runtime_without_row",
            )
        if runtime != expected.before_runtime:
            return _classification(
                BusinessCutoverActivationReconcileStatus.CONFLICT,
                expected,
                error_code="business_cutover_activation_runtime_conflict",
            )
        return _classification(
            BusinessCutoverActivationReconcileStatus.ABSENT,
            expected,
        )
    if receipt is None:
        return _classification(
            BusinessCutoverActivationReconcileStatus.CORRUPT,
            expected,
            error_code="business_cutover_activation_without_receipt",
        )
    if activation is None:
        identity_conflict = (
            receipt.transaction_id == expected.transaction_id
            and receipt.kind != expected.receipt_kind
        )
        return _classification(
            (
                BusinessCutoverActivationReconcileStatus.CONFLICT
                if identity_conflict
                else BusinessCutoverActivationReconcileStatus.CORRUPT
            ),
            expected,
            error_code=(
                "business_cutover_activation_receipt_identity_conflict"
                if identity_conflict
                else "business_cutover_activation_receipt_without_row"
            ),
        )
    try:
        observed_activation = _rebuild_activation_evidence(activation)
    except BusinessCutoverActivationIntegrityError:
        return _classification(
            BusinessCutoverActivationReconcileStatus.CORRUPT,
            expected,
            error_code="business_cutover_activation_row_corrupt",
        )
    if receipt != observed_activation.receipt_parent:
        identity_conflict = (
            receipt.transaction_id == expected.transaction_id
            and receipt.kind != expected.receipt_kind
        )
        return _classification(
            (
                BusinessCutoverActivationReconcileStatus.CONFLICT
                if identity_conflict
                else BusinessCutoverActivationReconcileStatus.CORRUPT
            ),
            expected,
            error_code=(
                "business_cutover_activation_receipt_identity_conflict"
                if identity_conflict
                else "business_cutover_activation_receipt_row_mismatch"
            ),
        )
    if (
        observed_activation.cutover_id != expected.cutover_id
        or observed_activation.transaction_id != expected.transaction_id
    ):
        return _classification(
            BusinessCutoverActivationReconcileStatus.CONFLICT,
            expected,
            error_code="business_cutover_activation_identity_conflict",
        )
    if observed_activation != expected:
        return _classification(
            BusinessCutoverActivationReconcileStatus.CONFLICT,
            expected,
            error_code="business_cutover_activation_content_conflict",
        )
    if receipt != expected.receipt_parent:
        return _classification(
            BusinessCutoverActivationReconcileStatus.CORRUPT,
            expected,
            error_code="business_cutover_activation_receipt_parent_corrupt",
        )
    if runtime != expected.after_runtime:
        if runtime == expected.before_runtime:
            return _classification(
                BusinessCutoverActivationReconcileStatus.CORRUPT,
                expected,
                error_code="business_cutover_activation_runtime_not_advanced",
            )
        return _classification(
            BusinessCutoverActivationReconcileStatus.CONFLICT,
            expected,
            error_code="business_cutover_activation_runtime_conflict",
        )
    return _classification(
        BusinessCutoverActivationReconcileStatus.EXACT,
        expected,
        evidence=observed_activation,
    )


__all__ = ()
