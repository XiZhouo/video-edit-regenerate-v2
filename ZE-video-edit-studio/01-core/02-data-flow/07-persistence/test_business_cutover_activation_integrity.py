"""Adversarial tests for audited BusinessCutoverActivation protocol v2."""

from __future__ import annotations

import ast
import dataclasses
import json
import sys
from pathlib import Path

import pytest


_HERE = Path(__file__).resolve().parent
_DATA_STRUCTURE = _HERE.parents[1] / "01-data-structure"
for _directory in (
    _HERE,
    _DATA_STRUCTURE / "01-document",
    _DATA_STRUCTURE / "02-entity",
    _DATA_STRUCTURE / "04-field-types",
):
    if str(_directory) not in sys.path:
        sys.path.insert(0, str(_directory))

import business_cutover_activation_contract as contract_module
import business_cutover_activation_integrity as integrity_module
from business_cutover_activation_contract import (
    BUSINESS_CUTOVER_ACTIVATION_MANIFEST_SCHEMA,
    BUSINESS_CUTOVER_ACTIVATION_PROTOCOL,
    BUSINESS_CUTOVER_ACTIVATION_TRANSACTION_KIND,
    BUSINESS_CUTOVER_ACTIVATION_TRANSACTION_PREFIX,
    BusinessCutoverActivationFreshObservation,
    BusinessCutoverActivationGenericReceiptEvidence,
    BusinessCutoverActivationPersistedObservation,
    BusinessCutoverActivationPersistedReadStatus,
    BusinessCutoverActivationPrerequisiteSnapshot,
    BusinessCutoverActivationReconcileStatus,
    BusinessCutoverActivationRetryAction,
    _BUSINESS_CUTOVER_ACTIVATION_COORDINATOR_READER_CAPABILITY,
    _BUSINESS_CUTOVER_ACTIVATION_DURABLE_READER_CAPABILITY,
    _stamp_business_cutover_activation_fresh_observation,
    _stamp_business_cutover_activation_fresh_prerequisites,
)
from business_cutover_activation_integrity import (
    BUSINESS_CUTOVER_ACTIVATION_MANIFEST_HASH_DOMAIN,
    BusinessCutoverActivationIntegrityError,
    _classify_business_cutover_activation,
    _issue_business_cutover_activation_plan,
    _require_business_cutover_activation_evidence,
    _require_business_cutover_activation_plan,
)
from repository_audit_contract import (
    EMPTY_WORKER_LEDGER_TAIL_SHA256,
    RepositoryAuditAuthorityState,
    RepositoryAuditCertificateLedgerHead,
    RepositoryAuditCertificateLedgerEntry,
    RepositoryAuditCoverage,
    RepositoryAuditFindings,
    RepositoryAuditMembershipObservation,
    RepositoryAuditObservedInput,
    RepositoryAuditPersistedObservation,
    RepositoryAuditPersistedReadStatus,
    RepositoryAuditPurpose,
    RepositoryAuditRuntimeBinding,
    RepositoryAuditSchemaObservation,
    _REPOSITORY_AUDIT_DURABLE_READER_CAPABILITY,
    _REPOSITORY_AUDIT_LEDGER_READER_CAPABILITY,
    _REPOSITORY_FULL_AUDIT_ADAPTER_CAPABILITY,
    _stamp_repository_audit_fresh_observation,
    _stamp_repository_audit_fresh_membership_observation,
    _stamp_repository_fresh_audit_observation,
)
from repository_audit_integrity import (
    _issue_repository_audit_certificate_plan,
)
from test_business_cutover_integrity import _plan as _cutover_plan


T0 = "2026-07-12T00:00:00.000Z"
T1 = "2026-07-12T00:00:10.000Z"
T2 = "2026-07-12T00:00:20.000Z"
T3 = "2026-07-12T00:00:30.000Z"
T4 = "2026-07-12T00:00:40.000Z"
T5 = "2026-07-12T00:00:50.000Z"

_DEFAULT = object()


def _fresh_prerequisites(
    cutover,
    *,
    admission=_DEFAULT,
    install=_DEFAULT,
    expected_ledger_count=_DEFAULT,
    expected_ledger_sha256=_DEFAULT,
):
    selected_admission = (
        cutover.final_admission if admission is _DEFAULT else admission
    )
    selected_install = cutover.install if install is _DEFAULT else install
    return _stamp_business_cutover_activation_fresh_prerequisites(
        _BUSINESS_CUTOVER_ACTIVATION_COORDINATOR_READER_CAPABILITY,
        BusinessCutoverActivationPrerequisiteSnapshot(
            current_admission=selected_admission,
            observed_install=selected_install,
            expected_migration_ledger_count=(
                cutover.expected_target_schema_version
                if expected_ledger_count is _DEFAULT
                else expected_ledger_count
            ),
            expected_migration_ledger_sha256=(
                b"m" * 32
                if expected_ledger_sha256 is _DEFAULT
                else expected_ledger_sha256
            ),
        ),
    )


def _audit_bundle(
    cutover,
    *,
    purpose: RepositoryAuditPurpose = RepositoryAuditPurpose.INSTALL,
    certificate_id: str = "a" * 32,
    runtime_changes=None,
    schema_version=None,
    schema_fingerprint=None,
    migration_ledger_sha256: bytes = b"m" * 32,
    install_digest=_DEFAULT,
):
    install_runtime = cutover.install.runtime
    runtime_values = {
        "authority": install_runtime.authority,
        "authority_state": RepositoryAuditAuthorityState.READ_ONLY,
        "repository_epoch": install_runtime.repository_epoch,
        "cutover_id": install_runtime.cutover_id,
        "source_admission_revision": install_runtime.source_admission_revision,
        "repository_revision": install_runtime.repository_revision,
        "state_revision": install_runtime.state_revision,
        "runtime_generation": install_runtime.runtime_generation,
        "code_digest": install_runtime.code_digest,
        "schema_version": install_runtime.schema_version,
        "updated_at": T0,
    }
    if runtime_changes:
        runtime_values.update(runtime_changes)
    runtime = RepositoryAuditRuntimeBinding(**runtime_values)
    version = (
        cutover.expected_target_schema_version
        if schema_version is None
        else schema_version
    )
    fingerprint = (
        cutover.expected_target_schema_fingerprint
        if schema_fingerprint is None
        else schema_fingerprint
    )
    schema = RepositoryAuditSchemaObservation(
        user_version=version,
        schema_fingerprint=fingerprint,
        migration_ledger_count=version,
        migration_ledger_sha256=migration_ledger_sha256,
    )
    selected_install_digest = (
        cutover.install.manifest_sha256
        if install_digest is _DEFAULT
        else install_digest
    )
    coverage = RepositoryAuditCoverage(
        covered_business_revision=runtime.repository_revision,
        worker_ledger_sequence=0,
        worker_ledger_tail_sha256=EMPTY_WORKER_LEDGER_TAIL_SHA256,
        receipt_namespace_count=5,
        receipt_namespace_sha256=b"r" * 32,
        cutover_install_manifest_sha256=selected_install_digest,
    )
    observed = RepositoryAuditObservedInput(
        certificate_id=certificate_id,
        purpose=purpose,
        runtime=runtime,
        schema=schema,
        coverage=coverage,
        findings=RepositoryAuditFindings(
            page_integrity="ok",
            foreign_key_violation_count=0,
            namespace_violation_count=0,
            started_at=T1,
            completed_at=T2,
        ),
    )
    fresh_audit = _stamp_repository_fresh_audit_observation(
        _REPOSITORY_FULL_AUDIT_ADAPTER_CAPABILITY,
        observed,
        RepositoryAuditCertificateLedgerHead(sequence=0),
    )
    audit_plan = _issue_repository_audit_certificate_plan(fresh_audit)
    fresh_persisted = _audit_read(audit_plan, exact=True)
    return audit_plan, fresh_persisted


def _audit_read(
    plan,
    *,
    exact: bool = False,
    unknown: bool = False,
):
    if unknown:
        raw = RepositoryAuditPersistedObservation(
            RepositoryAuditPersistedReadStatus.UNKNOWN,
            error_code="audit_storage_unreadable",
        )
    elif exact:
        raw = RepositoryAuditPersistedObservation(
            RepositoryAuditPersistedReadStatus.COMPLETE,
            receipt=plan.evidence.receipt_parent,
            certificate=plan.evidence,
            certificate_head=plan.after_head,
        )
    else:
        raw = RepositoryAuditPersistedObservation(
            RepositoryAuditPersistedReadStatus.COMPLETE,
            certificate_head=plan.before_head,
        )
    return _stamp_repository_audit_fresh_observation(
        _REPOSITORY_AUDIT_DURABLE_READER_CAPABILITY,
        raw,
    )


def _activation_plan(*, activated_at: str = T3):
    cutover = _cutover_plan()
    audit_plan, audit_read = _audit_bundle(cutover)
    prerequisites = _fresh_prerequisites(cutover)
    plan = _issue_business_cutover_activation_plan(
        cutover_plan=cutover,
        fresh_prerequisites=prerequisites,
        audit_plan=audit_plan,
        audit_persisted=audit_read,
        activated_at=activated_at,
    )
    return cutover, audit_plan, audit_read, prerequisites, plan


def _activation_read(plan, raw=None):
    selected = (
        BusinessCutoverActivationPersistedObservation(
            BusinessCutoverActivationPersistedReadStatus.COMPLETE,
            receipt=plan.evidence.receipt_parent,
            activation=plan.evidence,
            runtime=plan.evidence.after_runtime,
        )
        if raw is None
        else raw
    )
    return _stamp_business_cutover_activation_fresh_observation(
        _BUSINESS_CUTOVER_ACTIVATION_DURABLE_READER_CAPABILITY,
        selected,
    )


def test_issuer_binds_final_install_audit_schema_and_runtime_transition() -> None:
    cutover, audit_plan, _audit_read_exact, _prerequisites, plan = (
        _activation_plan()
    )
    assert _require_business_cutover_activation_plan(plan) is plan
    evidence = plan.evidence
    assert _require_business_cutover_activation_evidence(evidence) == evidence
    assert evidence.transaction_id == (
        BUSINESS_CUTOVER_ACTIVATION_TRANSACTION_PREFIX + cutover.cutover_id
    )
    assert evidence.receipt_kind == BUSINESS_CUTOVER_ACTIVATION_TRANSACTION_KIND
    assert evidence.before_runtime == cutover.install.runtime
    assert evidence.before_runtime.authority_state == "read_only"
    assert evidence.before_runtime.state_revision == 1
    assert evidence.after_runtime.authority_state == "active"
    assert evidence.after_runtime.state_revision == 2
    assert evidence.after_runtime.repository_revision == (
        evidence.before_runtime.repository_revision
    )
    assert evidence.install_transaction_id == cutover.install.transaction_id
    assert evidence.install_manifest_sha256 == cutover.install.manifest_sha256
    assert evidence.audit_certificate_id == audit_plan.evidence.certificate_id
    assert evidence.audit_certificate_sequence == audit_plan.evidence.sequence
    assert evidence.audit_manifest_sha256 == audit_plan.evidence.manifest_sha256
    assert (
        evidence.audit_certificate_tail_sha256
        == audit_plan.evidence.certificate_tail_sha256
    )
    assert evidence.actual_schema.user_version == (
        cutover.expected_target_schema_version
    )
    assert evidence.actual_schema.schema_fingerprint == (
        cutover.expected_target_schema_fingerprint
    )
    assert evidence.expected_migration_ledger_count == (
        cutover.expected_target_schema_version
    )
    assert evidence.expected_migration_ledger_sha256 == b"m" * 32

    manifest = json.loads(evidence.manifest_bytes.decode("utf-8"))
    assert manifest["schema"] == BUSINESS_CUTOVER_ACTIVATION_MANIFEST_SCHEMA
    assert manifest["protocol"] == BUSINESS_CUTOVER_ACTIVATION_PROTOCOL
    assert manifest["manifest_hash_domain"] == (
        BUSINESS_CUTOVER_ACTIVATION_MANIFEST_HASH_DOMAIN.decode("ascii")
    )
    assert manifest["install"]["transaction_id"] == cutover.install.transaction_id
    assert manifest["audit_certificate"]["certificate_id"] == (
        audit_plan.evidence.certificate_id
    )
    assert manifest["final_admission"]["sha256"] == (
        cutover.closure.final_admission_sha256.hex()
    )
    assert manifest["target_schema"]["migration_ledger_sha256"] == (
        (b"m" * 32).hex()
    )
    # The old planner value is rebuilt as part of exact v1 plan provenance,
    # but no field from that pre-generated phase-two evidence enters v2.
    assert cutover.join.transaction_id not in evidence.manifest_bytes.decode("utf-8")


@pytest.mark.parametrize(
    ("admission_name", "install_present", "code"),
    (
        (
            "pending_admission",
            True,
            "business_cutover_activation_admission_not_final",
        ),
        (
            "final_admission",
            False,
            "business_cutover_activation_install_missing",
        ),
    ),
)
def test_issuer_rejects_nonfinal_admission_and_missing_install(
    admission_name: str,
    install_present: bool,
    code: str,
) -> None:
    cutover = _cutover_plan()
    audit_plan, audit_read = _audit_bundle(cutover)
    prerequisites = _fresh_prerequisites(
        cutover,
        admission=getattr(cutover, admission_name),
        install=cutover.install if install_present else None,
    )
    with pytest.raises(BusinessCutoverActivationIntegrityError) as rejected:
        _issue_business_cutover_activation_plan(
            cutover_plan=cutover,
            fresh_prerequisites=prerequisites,
            audit_plan=audit_plan,
            audit_persisted=audit_read,
            activated_at=T3,
        )
    assert rejected.value.code == code


@pytest.mark.parametrize(
    ("case", "expected_code"),
    (
        ("absent", "business_cutover_activation_audit_absent"),
        ("unknown", "business_cutover_activation_audit_unknown"),
        ("noninstall", "business_cutover_activation_audit_purpose_invalid"),
        ("runtime", "business_cutover_activation_audit_runtime_mismatch"),
        ("schema_version", "business_cutover_activation_audit_schema_mismatch"),
        ("schema_fingerprint", "business_cutover_activation_audit_schema_mismatch"),
        ("ledger_digest", "business_cutover_activation_audit_schema_mismatch"),
        ("install_digest", "business_cutover_activation_audit_coverage_mismatch"),
    ),
)
def test_issuer_rejects_every_nonexact_or_mismatched_audit(
    case: str,
    expected_code: str,
) -> None:
    cutover = _cutover_plan()
    kwargs = {}
    if case == "noninstall":
        kwargs["purpose"] = RepositoryAuditPurpose.STARTUP
    elif case == "runtime":
        kwargs["runtime_changes"] = {"runtime_generation": "other-runtime"}
    elif case == "schema_version":
        kwargs["schema_version"] = cutover.expected_target_schema_version + 1
    elif case == "schema_fingerprint":
        kwargs["schema_fingerprint"] = "9" * 64
    elif case == "ledger_digest":
        kwargs["migration_ledger_sha256"] = b"z" * 32
    elif case == "install_digest":
        kwargs["install_digest"] = b"x" * 32
    audit_plan, exact_read = _audit_bundle(cutover, **kwargs)
    selected_read = exact_read
    if case == "absent":
        selected_read = _audit_read(audit_plan)
    elif case == "unknown":
        selected_read = _audit_read(audit_plan, unknown=True)
    with pytest.raises(BusinessCutoverActivationIntegrityError) as rejected:
        _issue_business_cutover_activation_plan(
            cutover_plan=cutover,
            fresh_prerequisites=_fresh_prerequisites(cutover),
            audit_plan=audit_plan,
            audit_persisted=selected_read,
            activated_at=T3,
        )
    assert rejected.value.code == expected_code


def test_classifier_absent_exact_ack_loss_and_unknown_are_distinct() -> None:
    cutover, audit_plan, audit_read, prerequisites, plan = _activation_plan()
    absent = _classify_business_cutover_activation(
        plan,
        fresh_prerequisites=prerequisites,
        audit_persisted=audit_read,
        persisted=_activation_read(
            plan,
            BusinessCutoverActivationPersistedObservation(
                BusinessCutoverActivationPersistedReadStatus.COMPLETE,
                runtime=plan.evidence.before_runtime,
            ),
        ),
    )
    assert absent.status is BusinessCutoverActivationReconcileStatus.ABSENT
    assert absent.retry_action is BusinessCutoverActivationRetryAction.APPLY_EXACT

    # This is the acknowledgement-loss retry: the write completed, the caller
    # saw no reply, and a fresh read returns the exact committed pair.
    exact = _classify_business_cutover_activation(
        plan,
        fresh_prerequisites=_fresh_prerequisites(cutover),
        audit_persisted=_audit_read(audit_plan, exact=True),
        persisted=_activation_read(plan),
    )
    assert exact.status is BusinessCutoverActivationReconcileStatus.EXACT
    assert exact.evidence == plan.evidence

    assert exact.retry_action is BusinessCutoverActivationRetryAction.RETURN_EXACT

    unknown = _classify_business_cutover_activation(
        plan,
        fresh_prerequisites=_fresh_prerequisites(cutover),
        audit_persisted=_audit_read(audit_plan, exact=True),
        persisted=_activation_read(
            plan,
            BusinessCutoverActivationPersistedObservation(
                BusinessCutoverActivationPersistedReadStatus.UNKNOWN,
                error_code="activation_storage_unreadable",
            ),
        ),
    )
    assert unknown.status is BusinessCutoverActivationReconcileStatus.UNKNOWN
    assert unknown.retry_action is (
        BusinessCutoverActivationRetryAction.RECONCILE_REQUIRED
    )


def test_committed_activation_remains_exact_after_audit_head_advances() -> None:
    cutover, install_audit, _audit_read_exact, prerequisites, plan = (
        _activation_plan()
    )
    after = plan.evidence.after_runtime
    successor_observed = RepositoryAuditObservedInput(
        certificate_id="b" * 32,
        purpose=RepositoryAuditPurpose.STARTUP,
        runtime=RepositoryAuditRuntimeBinding(
            authority=after.authority,
            authority_state=RepositoryAuditAuthorityState.ACTIVE,
            repository_epoch=after.repository_epoch,
            cutover_id=after.cutover_id,
            source_admission_revision=after.source_admission_revision,
            repository_revision=after.repository_revision,
            state_revision=after.state_revision,
            runtime_generation=after.runtime_generation,
            code_digest=after.code_digest,
            schema_version=after.schema_version,
            updated_at=T3,
        ),
        schema=install_audit.evidence.schema,
        coverage=RepositoryAuditCoverage(
            covered_business_revision=after.repository_revision,
            worker_ledger_sequence=0,
            worker_ledger_tail_sha256=EMPTY_WORKER_LEDGER_TAIL_SHA256,
            receipt_namespace_count=6,
            receipt_namespace_sha256=b"s" * 32,
            cutover_install_manifest_sha256=cutover.install.manifest_sha256,
        ),
        findings=RepositoryAuditFindings(
            page_integrity="ok",
            foreign_key_violation_count=0,
            namespace_violation_count=0,
            started_at=T4,
            completed_at=T5,
        ),
    )
    successor_fresh = _stamp_repository_fresh_audit_observation(
        _REPOSITORY_FULL_AUDIT_ADAPTER_CAPABILITY,
        successor_observed,
        install_audit.after_head,
        install_audit.evidence,
    )
    successor = _issue_repository_audit_certificate_plan(successor_fresh)

    historical_current_read = _stamp_repository_audit_fresh_observation(
        _REPOSITORY_AUDIT_DURABLE_READER_CAPABILITY,
        RepositoryAuditPersistedObservation(
            RepositoryAuditPersistedReadStatus.COMPLETE,
            receipt=install_audit.evidence.receipt_parent,
            certificate=install_audit.evidence,
            certificate_head=successor.after_head,
        ),
    )
    without_membership = _classify_business_cutover_activation(
        plan,
        fresh_prerequisites=prerequisites,
        audit_persisted=historical_current_read,
        persisted=_activation_read(plan),
    )
    assert without_membership.status is (
        BusinessCutoverActivationReconcileStatus.CONFLICT
    )

    membership = _stamp_repository_audit_fresh_membership_observation(
        _REPOSITORY_AUDIT_LEDGER_READER_CAPABILITY,
        RepositoryAuditMembershipObservation(
            read_status=RepositoryAuditPersistedReadStatus.COMPLETE,
            receipt=install_audit.evidence.receipt_parent,
            certificate=install_audit.evidence,
            current_head=successor.after_head,
            successor_chain=(
                RepositoryAuditCertificateLedgerEntry(
                    receipt=successor.evidence.receipt_parent,
                    certificate=successor.evidence,
                ),
            ),
        ),
    )
    exact = _classify_business_cutover_activation(
        plan,
        fresh_prerequisites=_fresh_prerequisites(cutover),
        audit_persisted=historical_current_read,
        persisted=_activation_read(plan),
        audit_membership=membership,
    )
    assert exact.status is BusinessCutoverActivationReconcileStatus.EXACT
    assert exact.evidence == plan.evidence

    unknown_read = _activation_read(
        plan,
        BusinessCutoverActivationPersistedObservation(
            BusinessCutoverActivationPersistedReadStatus.UNKNOWN,
            error_code="activation_storage_unreadable",
        ),
    )
    unknown = _classify_business_cutover_activation(
        plan,
        fresh_prerequisites=_fresh_prerequisites(cutover),
        audit_persisted=historical_current_read,
        persisted=unknown_read,
        audit_membership=membership,
    )
    assert unknown.status is BusinessCutoverActivationReconcileStatus.UNKNOWN
    assert unknown.retry_action is (
        BusinessCutoverActivationRetryAction.RECONCILE_REQUIRED
    )


@pytest.mark.parametrize(
    ("case", "expected_status"),
    (
        ("pending", BusinessCutoverActivationReconcileStatus.CONFLICT),
        ("missing_install", BusinessCutoverActivationReconcileStatus.CORRUPT),
        ("audit_absent", BusinessCutoverActivationReconcileStatus.CORRUPT),
        ("audit_unknown", BusinessCutoverActivationReconcileStatus.UNKNOWN),
        ("target_changed", BusinessCutoverActivationReconcileStatus.CONFLICT),
    ),
)
def test_classifier_rechecks_fresh_external_prerequisites_every_time(
    case: str,
    expected_status: BusinessCutoverActivationReconcileStatus,
) -> None:
    cutover, audit_plan, audit_read, prerequisites, plan = _activation_plan()
    selected_prerequisites = prerequisites
    selected_audit = audit_read
    if case == "pending":
        selected_prerequisites = _fresh_prerequisites(
            cutover,
            admission=cutover.pending_admission,
        )
    elif case == "missing_install":
        selected_prerequisites = _fresh_prerequisites(cutover, install=None)
    elif case == "audit_absent":
        selected_audit = _audit_read(audit_plan)
    elif case == "audit_unknown":
        selected_audit = _audit_read(audit_plan, unknown=True)
    elif case == "target_changed":
        selected_prerequisites = _fresh_prerequisites(
            cutover,
            expected_ledger_sha256=b"x" * 32,
        )
    result = _classify_business_cutover_activation(
        plan,
        fresh_prerequisites=selected_prerequisites,
        audit_persisted=selected_audit,
        persisted=_activation_read(plan),
    )
    assert result.status is expected_status
    assert result.evidence is None


def test_same_id_different_valid_activation_is_conflict() -> None:
    cutover, audit_plan, audit_read, prerequisites, expected = _activation_plan(
        activated_at=T3
    )
    alternate = _issue_business_cutover_activation_plan(
        cutover_plan=cutover,
        fresh_prerequisites=_fresh_prerequisites(cutover),
        audit_plan=audit_plan,
        audit_persisted=_audit_read(audit_plan, exact=True),
        activated_at=T4,
    )
    assert alternate.transaction_id == expected.transaction_id
    assert alternate.evidence != expected.evidence
    result = _classify_business_cutover_activation(
        expected,
        fresh_prerequisites=prerequisites,
        audit_persisted=audit_read,
        persisted=_activation_read(
            expected,
            BusinessCutoverActivationPersistedObservation(
                BusinessCutoverActivationPersistedReadStatus.COMPLETE,
                receipt=alternate.evidence.receipt_parent,
                activation=alternate.evidence,
                runtime=alternate.evidence.after_runtime,
            ),
        ),
    )
    assert result.status is BusinessCutoverActivationReconcileStatus.CONFLICT
    assert result.error_code == "business_cutover_activation_content_conflict"


def test_partial_wrong_kind_and_tampered_rows_never_become_exact() -> None:
    cutover, _audit_plan, audit_read, prerequisites, plan = _activation_plan()
    receipt_only = _classify_business_cutover_activation(
        plan,
        fresh_prerequisites=prerequisites,
        audit_persisted=audit_read,
        persisted=_activation_read(
            plan,
            BusinessCutoverActivationPersistedObservation(
                BusinessCutoverActivationPersistedReadStatus.COMPLETE,
                receipt=plan.evidence.receipt_parent,
                runtime=plan.evidence.before_runtime,
            ),
        ),
    )
    assert receipt_only.status is BusinessCutoverActivationReconcileStatus.CORRUPT

    wrong_kind = BusinessCutoverActivationGenericReceiptEvidence(
        transaction_id=plan.transaction_id,
        kind="occupied_by_other_kind",
        committed_at=plan.evidence.activated_at,
    )
    occupied = _classify_business_cutover_activation(
        plan,
        fresh_prerequisites=_fresh_prerequisites(cutover),
        audit_persisted=audit_read,
        persisted=_activation_read(
            plan,
            BusinessCutoverActivationPersistedObservation(
                BusinessCutoverActivationPersistedReadStatus.COMPLETE,
                receipt=wrong_kind,
                runtime=plan.evidence.before_runtime,
            ),
        ),
    )
    assert occupied.status is BusinessCutoverActivationReconcileStatus.CONFLICT

    tampered = dataclasses.replace(
        plan.evidence,
        manifest_sha256=b"x" * 32,
    )
    corrupt = _classify_business_cutover_activation(
        plan,
        fresh_prerequisites=_fresh_prerequisites(cutover),
        audit_persisted=audit_read,
        persisted=_activation_read(
            plan,
            BusinessCutoverActivationPersistedObservation(
                BusinessCutoverActivationPersistedReadStatus.COMPLETE,
                receipt=plan.evidence.receipt_parent,
                activation=tampered,
                runtime=plan.evidence.after_runtime,
            ),
        ),
    )
    assert corrupt.status is BusinessCutoverActivationReconcileStatus.CORRUPT


def test_runtime_transition_is_part_of_the_fresh_atomic_observation() -> None:
    cutover, _audit_plan, audit_read, prerequisites, plan = _activation_plan()
    rows_without_runtime_cas = _classify_business_cutover_activation(
        plan,
        fresh_prerequisites=prerequisites,
        audit_persisted=audit_read,
        persisted=_activation_read(
            plan,
            BusinessCutoverActivationPersistedObservation(
                BusinessCutoverActivationPersistedReadStatus.COMPLETE,
                receipt=plan.evidence.receipt_parent,
                activation=plan.evidence,
                runtime=plan.evidence.before_runtime,
            ),
        ),
    )
    assert rows_without_runtime_cas.status is (
        BusinessCutoverActivationReconcileStatus.CORRUPT
    )
    assert rows_without_runtime_cas.error_code == (
        "business_cutover_activation_runtime_not_advanced"
    )

    runtime_without_rows = _classify_business_cutover_activation(
        plan,
        fresh_prerequisites=_fresh_prerequisites(cutover),
        audit_persisted=audit_read,
        persisted=_activation_read(
            plan,
            BusinessCutoverActivationPersistedObservation(
                BusinessCutoverActivationPersistedReadStatus.COMPLETE,
                runtime=plan.evidence.after_runtime,
            ),
        ),
    )
    assert runtime_without_rows.status is (
        BusinessCutoverActivationReconcileStatus.CORRUPT
    )
    assert runtime_without_rows.error_code == (
        "business_cutover_activation_runtime_without_row"
    )


def test_all_three_fresh_boundaries_reject_raw_or_forged_values() -> None:
    cutover, audit_plan, audit_read, prerequisites, plan = _activation_plan()
    raw_prerequisites = BusinessCutoverActivationPrerequisiteSnapshot(
        cutover.final_admission,
        cutover.install,
        cutover.expected_target_schema_version,
        b"m" * 32,
    )
    with pytest.raises(BusinessCutoverActivationIntegrityError) as rejected:
        _issue_business_cutover_activation_plan(
            cutover_plan=cutover,
            fresh_prerequisites=raw_prerequisites,
            audit_plan=audit_plan,
            audit_persisted=audit_read,
            activated_at=T3,
        )
    assert rejected.value.code == (
        "business_cutover_activation_fresh_prerequisites_invalid"
    )

    forged_prerequisites = object.__new__(type(prerequisites))
    object.__setattr__(forged_prerequisites, "snapshot", raw_prerequisites)
    object.__setattr__(forged_prerequisites, "_reader", object())
    object.__setattr__(forged_prerequisites, "_issuer", object())
    with pytest.raises(BusinessCutoverActivationIntegrityError):
        _issue_business_cutover_activation_plan(
            cutover_plan=cutover,
            fresh_prerequisites=forged_prerequisites,
            audit_plan=audit_plan,
            audit_persisted=audit_read,
            activated_at=T3,
        )

    raw_activation = BusinessCutoverActivationPersistedObservation(
        BusinessCutoverActivationPersistedReadStatus.COMPLETE,
        receipt=plan.evidence.receipt_parent,
        activation=plan.evidence,
        runtime=plan.evidence.after_runtime,
    )
    with pytest.raises(BusinessCutoverActivationIntegrityError) as rejected:
        _classify_business_cutover_activation(
            plan,
            fresh_prerequisites=_fresh_prerequisites(cutover),
            audit_persisted=audit_read,
            persisted=raw_activation,
        )
    assert rejected.value.code == (
        "business_cutover_activation_fresh_observation_invalid"
    )

    forged_read = object.__new__(BusinessCutoverActivationFreshObservation)
    object.__setattr__(forged_read, "persisted", raw_activation)
    object.__setattr__(forged_read, "_reader", object())
    object.__setattr__(forged_read, "_issuer", object())
    with pytest.raises(BusinessCutoverActivationIntegrityError):
        _classify_business_cutover_activation(
            plan,
            fresh_prerequisites=_fresh_prerequisites(cutover),
            audit_persisted=audit_read,
            persisted=forged_read,
        )


def test_activation_leaves_are_private_pure_py39_and_do_not_consume_v1_join() -> None:
    root = Path(__file__).resolve().parent
    forbidden_imports = {
        "sqlite3",
        "sqlite_business_runtime",
        "sqlite_kernel",
        "pathlib",
        "threading",
    }
    for name in (
        "business_cutover_activation_contract.py",
        "business_cutover_activation_integrity.py",
    ):
        source = (root / name).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = set()
        imported_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".", 1)[0])
                imported_names.update(alias.name for alias in node.names)
            assert type(node).__name__ != "Match"
        assert imported.isdisjoint(forbidden_imports)
        assert " | " not in source
        assert "BusinessCutoverJoinEvidence" not in imported_names
        assert "_issue_business_cutover_activation" not in source.replace(
            "_issue_business_cutover_activation_plan",
            "",
        )
    assert contract_module.__all__ == ()
    assert integrity_module.__all__ == ()
