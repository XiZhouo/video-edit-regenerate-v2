"""Adversarial tests for the pure RepositoryAuditCertificate v1 boundary."""

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

import repository_audit_contract as contract_module
import repository_audit_integrity as integrity_module
from repository_audit_contract import (
    EMPTY_RECEIPT_NAMESPACE_SHA256,
    EMPTY_WORKER_LEDGER_TAIL_SHA256,
    MAX_REPOSITORY_AUDIT_MANIFEST_BYTES,
    MAX_REPOSITORY_AUDIT_MEMBERSHIP_HOPS,
    MAX_REPOSITORY_AUDIT_MEMBERSHIP_MANIFEST_BYTES,
    REPOSITORY_AUDIT_CERTIFICATE_STATUS,
    REPOSITORY_AUDIT_PROTOCOL,
    REPOSITORY_AUDIT_TRANSACTION_KIND,
    REPOSITORY_AUDIT_TRANSACTION_PREFIX,
    RepositoryAuditCertificateLedgerHead,
    RepositoryAuditCertificateLedgerEntry,
    RepositoryAuditAuthorityState,
    RepositoryAuditContractError,
    RepositoryAuditCoverage,
    RepositoryAuditFindings,
    RepositoryAuditFreshObservation,
    RepositoryAuditGenericReceiptEvidence,
    RepositoryAuditMembershipObservation,
    RepositoryAuditObservedInput,
    RepositoryAuditPersistedObservation,
    RepositoryAuditPersistedReadStatus,
    RepositoryAuditPurpose,
    RepositoryAuditReconcileStatus,
    RepositoryAuditRetryAction,
    RepositoryAuditRuntimeBinding,
    RepositoryAuditSchemaObservation,
    RepositoryFreshAuditObservation,
    _REPOSITORY_AUDIT_DURABLE_READER_CAPABILITY,
    _REPOSITORY_AUDIT_LEDGER_READER_CAPABILITY,
    _REPOSITORY_FULL_AUDIT_ADAPTER_CAPABILITY,
    _stamp_repository_audit_fresh_observation,
    _stamp_repository_audit_fresh_membership_observation,
    _stamp_repository_fresh_audit_observation,
)
from repository_audit_integrity import (
    RepositoryAuditCertificatePlan,
    RepositoryAuditIntegrityError,
    _classify_repository_audit_certificate,
    _classify_repository_audit_certificate_membership,
    _findings_digest,
    _issue_repository_audit_certificate_plan as _issue_stamped_plan,
    _require_repository_audit_certificate_evidence,
    _require_repository_audit_certificate_plan,
)


T0 = "2026-07-12T00:00:00.000Z"
T1 = "2026-07-12T00:00:10.000Z"
T2 = "2026-07-12T00:00:20.000Z"
T3 = "2026-07-12T00:00:30.000Z"
T4 = "2026-07-12T00:00:40.000Z"
T5 = "2026-07-12T00:00:50.000Z"
T6 = "2026-07-12T00:01:00.000Z"


def _certificate_head(certificate=None) -> RepositoryAuditCertificateLedgerHead:
    if certificate is None:
        return RepositoryAuditCertificateLedgerHead(sequence=0)
    return RepositoryAuditCertificateLedgerHead(
        sequence=certificate.sequence,
        certificate_id=certificate.certificate_id,
        manifest_sha256=certificate.manifest_sha256,
        certificate_tail_sha256=certificate.certificate_tail_sha256,
    )


def _fresh_audit(observed, previous_certificate=None) -> RepositoryFreshAuditObservation:
    return _stamp_repository_fresh_audit_observation(
        _REPOSITORY_FULL_AUDIT_ADAPTER_CAPABILITY,
        observed,
        _certificate_head(previous_certificate),
        previous_certificate,
    )


def _issue_repository_audit_certificate_plan(
    observed,
    previous_certificate=None,
) -> RepositoryAuditCertificatePlan:
    return _issue_stamped_plan(_fresh_audit(observed, previous_certificate))


def _fresh_persisted(persisted: RepositoryAuditPersistedObservation):
    return _stamp_repository_audit_fresh_observation(
        _REPOSITORY_AUDIT_DURABLE_READER_CAPABILITY,
        persisted,
    )


def _runtime(
    state: RepositoryAuditAuthorityState = RepositoryAuditAuthorityState.ACTIVE,
    *,
    epoch: str = "e" * 64,
    repository_revision: int = 7,
    state_revision: int = 2,
    updated_at: str = T0,
) -> RepositoryAuditRuntimeBinding:
    return RepositoryAuditRuntimeBinding(
        authority="sqlite",
        authority_state=state,
        repository_epoch=epoch,
        cutover_id="c" * 64,
        source_admission_revision=5,
        repository_revision=repository_revision,
        state_revision=state_revision,
        runtime_generation="runtime-1",
        code_digest=b"d" * 32,
        schema_version=6,
        updated_at=updated_at,
    )


def _schema(*, fingerprint: str = "f" * 64, version: int = 8) -> RepositoryAuditSchemaObservation:
    return RepositoryAuditSchemaObservation(
        user_version=version,
        schema_fingerprint=fingerprint,
        migration_ledger_count=version,
        migration_ledger_sha256=b"m" * 32,
    )


def _coverage(
    *,
    business_revision: int = 7,
    worker_sequence: int = 0,
    worker_tail: bytes = EMPTY_WORKER_LEDGER_TAIL_SHA256,
    receipt_count: int = 5,
    receipt_digest: bytes = b"r" * 32,
    install_digest: bytes = None,
) -> RepositoryAuditCoverage:
    return RepositoryAuditCoverage(
        covered_business_revision=business_revision,
        worker_ledger_sequence=worker_sequence,
        worker_ledger_tail_sha256=worker_tail,
        receipt_namespace_count=receipt_count,
        receipt_namespace_sha256=receipt_digest,
        cutover_install_manifest_sha256=install_digest,
    )


def _findings(*, started_at: str = T1, completed_at: str = T2) -> RepositoryAuditFindings:
    return RepositoryAuditFindings(
        page_integrity="ok",
        foreign_key_violation_count=0,
        namespace_violation_count=0,
        started_at=started_at,
        completed_at=completed_at,
    )


def test_findings_schema_projection_has_one_frozen_domain_digest() -> None:
    assert _findings_digest(_findings()).hex() == (
        "c300bcf861a8603388ba790f67e189a1970cbcf3d9085de32cbc19fcc24a7046"
    )
    assert _findings_digest(_findings(completed_at=T3)) != _findings_digest(
        _findings()
    )


def _observed(
    certificate_id: str = "a" * 32,
    *,
    purpose: RepositoryAuditPurpose = RepositoryAuditPurpose.STARTUP,
    runtime: RepositoryAuditRuntimeBinding = None,
    schema: RepositoryAuditSchemaObservation = None,
    coverage: RepositoryAuditCoverage = None,
    findings: RepositoryAuditFindings = None,
) -> RepositoryAuditObservedInput:
    return RepositoryAuditObservedInput(
        certificate_id=certificate_id,
        purpose=purpose,
        runtime=_runtime() if runtime is None else runtime,
        schema=_schema() if schema is None else schema,
        coverage=_coverage() if coverage is None else coverage,
        findings=_findings() if findings is None else findings,
    )


def _persisted(plan: RepositoryAuditCertificatePlan):
    return _fresh_persisted(RepositoryAuditPersistedObservation(
        RepositoryAuditPersistedReadStatus.COMPLETE,
        receipt=plan.evidence.receipt_parent,
        certificate=plan.evidence,
        certificate_head=plan.after_head,
    ))


@pytest.mark.parametrize(
    ("purpose", "state", "install_digest"),
    (
        (RepositoryAuditPurpose.STARTUP, RepositoryAuditAuthorityState.ACTIVE, None),
        (RepositoryAuditPurpose.PERIODIC, RepositoryAuditAuthorityState.READ_ONLY, None),
        (RepositoryAuditPurpose.INSTALL, RepositoryAuditAuthorityState.READ_ONLY, b"i" * 32),
        (
            RepositoryAuditPurpose.REPAIR_EXIT,
            RepositoryAuditAuthorityState.REPAIR_REQUIRED,
            None,
        ),
    ),
)
def test_all_purposes_issue_exact_rebuildable_expected_evidence(
    purpose: RepositoryAuditPurpose,
    state: RepositoryAuditAuthorityState,
    install_digest: bytes,
) -> None:
    observed = _observed(
        purpose=purpose,
        runtime=_runtime(state),
        coverage=_coverage(install_digest=install_digest),
    )
    plan = _issue_repository_audit_certificate_plan(observed)
    assert _require_repository_audit_certificate_plan(plan) is plan
    evidence = plan.evidence
    assert evidence.protocol == REPOSITORY_AUDIT_PROTOCOL
    assert evidence.status == REPOSITORY_AUDIT_CERTIFICATE_STATUS
    assert evidence.sequence == 1
    assert evidence.transaction_id == (
        REPOSITORY_AUDIT_TRANSACTION_PREFIX + observed.certificate_id
    )
    assert evidence.receipt_kind == REPOSITORY_AUDIT_TRANSACTION_KIND
    assert evidence.receipt_parent.committed_at == observed.findings.completed_at
    assert evidence.previous_certificate_id is None
    assert evidence.certificate_tail_sha256 != b"\0" * 32
    assert _require_repository_audit_certificate_evidence(evidence) == evidence

    manifest = json.loads(evidence.manifest_bytes.decode("utf-8"))
    assert manifest["certificate"] == {
        "certificate_id": observed.certificate_id,
        "purpose": purpose.value,
        "sequence": 1,
    }
    assert manifest["receipt_parent"] == {
        "committed_at": observed.findings.completed_at,
        "kind": REPOSITORY_AUDIT_TRANSACTION_KIND,
        "ledger": "transaction_receipts",
        "specialised_certificate_id": observed.certificate_id,
        "specialised_protocol": REPOSITORY_AUDIT_PROTOCOL,
        "transaction_id": evidence.transaction_id,
    }
    assert manifest["predecessor"] is None


def test_certificate_chain_binds_predecessor_and_monotonic_high_waters() -> None:
    first = _issue_repository_audit_certificate_plan(_observed("a" * 32))
    second_observed = _observed(
        "b" * 32,
        purpose=RepositoryAuditPurpose.PERIODIC,
        runtime=_runtime(
            repository_revision=8,
            state_revision=3,
            updated_at=T2,
        ),
        coverage=_coverage(
            business_revision=8,
            worker_sequence=1,
            worker_tail=b"w" * 32,
            receipt_count=6,
            receipt_digest=b"s" * 32,
        ),
        findings=_findings(started_at=T3, completed_at=T4),
    )
    second = _issue_repository_audit_certificate_plan(
        second_observed,
        first.evidence,
    )
    assert second.evidence.sequence == 2
    assert second.evidence.previous_certificate_id == first.evidence.certificate_id
    assert second.evidence.previous_manifest_sha256 == first.evidence.manifest_sha256
    assert (
        second.evidence.previous_certificate_tail_sha256
        == first.evidence.certificate_tail_sha256
    )
    assert second.evidence.certificate_tail_sha256 != first.evidence.certificate_tail_sha256
    predecessor = json.loads(second.evidence.manifest_bytes)["predecessor"]
    assert predecessor["sequence"] == 1
    assert predecessor["manifest_sha256"] == first.evidence.manifest_sha256.hex()
    assert _require_repository_audit_certificate_plan(second) is second


def test_install_binding_cannot_be_dropped_before_activation_evidence() -> None:
    install_digest = b"i" * 32
    install = _issue_repository_audit_certificate_plan(
        _observed(
            purpose=RepositoryAuditPurpose.INSTALL,
            runtime=_runtime(
                RepositoryAuditAuthorityState.READ_ONLY,
                state_revision=1,
            ),
            coverage=_coverage(install_digest=install_digest),
        )
    )
    startup = _observed(
        "b" * 32,
        purpose=RepositoryAuditPurpose.STARTUP,
        runtime=_runtime(
            RepositoryAuditAuthorityState.ACTIVE,
            state_revision=2,
            updated_at=T2,
        ),
        coverage=_coverage(
            receipt_count=6,
            receipt_digest=b"s" * 32,
            install_digest=install_digest,
        ),
        findings=_findings(started_at=T3, completed_at=T4),
    )
    issued = _issue_repository_audit_certificate_plan(startup, install.evidence)
    assert (
        issued.evidence.coverage.cutover_install_manifest_sha256
        == install_digest
    )

    dropped = dataclasses.replace(
        startup,
        certificate_id="c" * 32,
        coverage=_coverage(receipt_count=6, receipt_digest=b"s" * 32),
    )
    with pytest.raises(RepositoryAuditIntegrityError) as rejected:
        _issue_repository_audit_certificate_plan(dropped, install.evidence)
    assert rejected.value.code == (
        "repository_audit_predecessor_install_binding_invalid"
    )


def test_install_audit_can_retake_current_head_after_read_only_periodic_audit() -> None:
    install_digest = b"i" * 32
    first_install = _issue_repository_audit_certificate_plan(
        _observed(
            purpose=RepositoryAuditPurpose.INSTALL,
            runtime=_runtime(
                RepositoryAuditAuthorityState.READ_ONLY,
                state_revision=1,
            ),
            coverage=_coverage(install_digest=install_digest),
        )
    )
    periodic = _issue_repository_audit_certificate_plan(
        _observed(
            "b" * 32,
            purpose=RepositoryAuditPurpose.PERIODIC,
            runtime=_runtime(
                RepositoryAuditAuthorityState.READ_ONLY,
                state_revision=1,
                updated_at=T2,
            ),
            coverage=_coverage(
                receipt_count=6,
                receipt_digest=b"s" * 32,
                install_digest=install_digest,
            ),
            findings=_findings(started_at=T3, completed_at=T4),
        ),
        first_install.evidence,
    )
    current_install = _issue_repository_audit_certificate_plan(
        _observed(
            "c" * 32,
            purpose=RepositoryAuditPurpose.INSTALL,
            runtime=_runtime(
                RepositoryAuditAuthorityState.READ_ONLY,
                state_revision=1,
                updated_at=T4,
            ),
            coverage=_coverage(
                receipt_count=7,
                receipt_digest=b"t" * 32,
                install_digest=install_digest,
            ),
            findings=_findings(started_at=T5, completed_at=T6),
        ),
        periodic.evidence,
    )
    assert current_install.evidence.sequence == 3
    assert current_install.before_head == periodic.after_head


def test_same_worker_tail_is_allowed_but_predecessor_receipt_must_be_covered() -> None:
    first = _issue_repository_audit_certificate_plan(_observed())
    unchanged_worker_tail = _observed(
        "b" * 32,
        runtime=_runtime(updated_at=T2),
        coverage=_coverage(
            worker_sequence=0,
            worker_tail=EMPTY_WORKER_LEDGER_TAIL_SHA256,
            receipt_count=6,
            receipt_digest=b"s" * 32,
        ),
        findings=_findings(started_at=T3, completed_at=T4),
    )
    # Exact unchanged zero worker tail is accepted.
    _issue_repository_audit_certificate_plan(unchanged_worker_tail, first.evidence)

    missing_predecessor_receipt = dataclasses.replace(
        unchanged_worker_tail,
        certificate_id="c" * 32,
        coverage=_coverage(receipt_count=5, receipt_digest=b"r" * 32),
    )
    with pytest.raises(RepositoryAuditIntegrityError) as rejected:
        _issue_repository_audit_certificate_plan(
            missing_predecessor_receipt,
            first.evidence,
        )
    assert rejected.value.code == "repository_audit_predecessor_high_water_invalid"

    unchanged_receipt_digest = dataclasses.replace(
        unchanged_worker_tail,
        certificate_id="d" * 32,
        coverage=_coverage(receipt_count=6, receipt_digest=b"r" * 32),
    )
    with pytest.raises(RepositoryAuditIntegrityError) as rejected:
        _issue_repository_audit_certificate_plan(
            unchanged_receipt_digest,
            first.evidence,
        )
    assert rejected.value.code == "repository_audit_predecessor_receipt_digest_invalid"

    worker_first = _issue_repository_audit_certificate_plan(
        _observed(
            "1" * 32,
            coverage=_coverage(worker_sequence=1, worker_tail=b"w" * 32),
        )
    )
    advanced_with_same_tail = _observed(
        "2" * 32,
        runtime=_runtime(updated_at=T2),
        coverage=_coverage(
            worker_sequence=2,
            worker_tail=b"w" * 32,
            receipt_count=6,
            receipt_digest=b"s" * 32,
        ),
        findings=_findings(started_at=T3, completed_at=T4),
    )
    with pytest.raises(RepositoryAuditIntegrityError) as rejected:
        _issue_repository_audit_certificate_plan(
            advanced_with_same_tail,
            worker_first.evidence,
        )
    assert rejected.value.code == "repository_audit_predecessor_worker_tail_invalid"

    advanced_with_new_tail = dataclasses.replace(
        advanced_with_same_tail,
        certificate_id="f" * 32,
        coverage=_coverage(
            worker_sequence=2,
            worker_tail=b"x" * 32,
            receipt_count=6,
            receipt_digest=b"s" * 32,
        ),
    )
    _issue_repository_audit_certificate_plan(
        advanced_with_new_tail,
        worker_first.evidence,
    )


def test_wrong_purpose_state_and_missing_install_digest_are_rejected() -> None:
    with pytest.raises(RepositoryAuditContractError) as rejected:
        _observed(purpose=RepositoryAuditPurpose.INSTALL)
    assert rejected.value.code == "repository_audit_install_binding_invalid"

    with pytest.raises(RepositoryAuditContractError) as rejected:
        _observed(
            purpose=RepositoryAuditPurpose.INSTALL,
            runtime=_runtime(RepositoryAuditAuthorityState.READ_ONLY),
        )
    assert rejected.value.code == "repository_audit_install_binding_invalid"

    with pytest.raises(RepositoryAuditContractError) as rejected:
        _observed(
            purpose=RepositoryAuditPurpose.REPAIR_EXIT,
            runtime=_runtime(RepositoryAuditAuthorityState.ACTIVE),
        )
    assert rejected.value.code == "repository_audit_repair_exit_binding_invalid"


def test_wrong_or_tampered_predecessor_is_rejected() -> None:
    first = _issue_repository_audit_certificate_plan(_observed())
    foreign = _issue_repository_audit_certificate_plan(
        _observed(
            "f" * 32,
            runtime=_runtime(epoch="9" * 64),
        )
    )
    successor = _observed(
        "b" * 32,
        runtime=_runtime(updated_at=T2),
        findings=_findings(started_at=T3, completed_at=T4),
    )
    with pytest.raises(RepositoryAuditIntegrityError) as rejected:
        _issue_repository_audit_certificate_plan(successor, foreign.evidence)
    assert rejected.value.code == "repository_audit_predecessor_identity_mismatch"

    tampered = dataclasses.replace(
        first.evidence,
        manifest_sha256=b"x" * 32,
    )
    with pytest.raises(RepositoryAuditIntegrityError) as rejected:
        _issue_repository_audit_certificate_plan(successor, tampered)
    assert rejected.value.code == "repository_audit_evidence_changed"

    regressed = _observed(
        "b" * 32,
        runtime=_runtime(repository_revision=6, updated_at=T2),
        coverage=_coverage(business_revision=6),
        findings=_findings(started_at=T3, completed_at=T4),
    )
    with pytest.raises(RepositoryAuditIntegrityError) as rejected:
        _issue_repository_audit_certificate_plan(regressed, first.evidence)
    assert rejected.value.code == "repository_audit_predecessor_high_water_invalid"


@pytest.mark.parametrize(
    ("field", "value", "code"),
    (
        ("page_integrity", "corrupt", "repository_audit_page_integrity_failed"),
        (
            "foreign_key_violation_count",
            1,
            "repository_audit_foreign_key_violation",
        ),
        ("namespace_violation_count", 1, "repository_audit_namespace_violation"),
    ),
)
def test_failed_findings_cannot_be_issued(field: str, value: object, code: str) -> None:
    values = {
        "page_integrity": "ok",
        "foreign_key_violation_count": 0,
        "namespace_violation_count": 0,
        "started_at": T1,
        "completed_at": T2,
    }
    values[field] = value
    with pytest.raises(RepositoryAuditContractError) as rejected:
        RepositoryAuditFindings(**values)
    assert rejected.value.code == code


def test_zero_ledger_and_namespace_forms_are_exact() -> None:
    RepositoryAuditCoverage(
        covered_business_revision=1,
        worker_ledger_sequence=0,
        worker_ledger_tail_sha256=EMPTY_WORKER_LEDGER_TAIL_SHA256,
        receipt_namespace_count=0,
        receipt_namespace_sha256=EMPTY_RECEIPT_NAMESPACE_SHA256,
    )
    with pytest.raises(RepositoryAuditContractError) as rejected:
        _coverage(worker_sequence=0, worker_tail=b"w" * 32)
    assert rejected.value.code == "repository_audit_worker_ledger_invalid"
    with pytest.raises(RepositoryAuditContractError) as rejected:
        RepositoryAuditCoverage(
            covered_business_revision=1,
            worker_ledger_sequence=0,
            worker_ledger_tail_sha256=EMPTY_WORKER_LEDGER_TAIL_SHA256,
            receipt_namespace_count=0,
            receipt_namespace_sha256=b"r" * 32,
        )
    assert rejected.value.code == "repository_audit_receipt_namespace_invalid"


def test_plan_and_runtime_tampering_are_detected_by_full_rebuild() -> None:
    plan = _issue_repository_audit_certificate_plan(_observed())
    tampered_runtime = dataclasses.replace(
        plan.observed.runtime,
        repository_epoch="9" * 64,
    )
    object.__setattr__(
        plan,
        "observed",
        dataclasses.replace(plan.observed, runtime=tampered_runtime),
    )
    with pytest.raises(RepositoryAuditIntegrityError) as rejected:
        _require_repository_audit_certificate_plan(plan)
    assert rejected.value.code == "repository_audit_plan_changed"

    with pytest.raises(RepositoryAuditIntegrityError) as rejected:
        RepositoryAuditCertificatePlan(
            None,
            fresh_observed=None,
            _issuer=object(),
        )
    assert rejected.value.code == "repository_audit_plan_issuer_invalid"


def test_invalid_or_untyped_runtime_projection_is_rejected() -> None:
    with pytest.raises(RepositoryAuditContractError) as rejected:
        RepositoryAuditRuntimeBinding(
            authority="legacy",
            authority_state=RepositoryAuditAuthorityState.ACTIVE,
            repository_epoch="e" * 64,
            cutover_id="c" * 64,
            source_admission_revision=5,
            repository_revision=7,
            state_revision=2,
            runtime_generation="runtime-1",
            code_digest=b"d" * 32,
            schema_version=6,
            updated_at=T0,
        )
    assert rejected.value.code == "repository_audit_runtime_identity_invalid"

    valid = _observed()
    with pytest.raises(RepositoryAuditContractError) as rejected:
        RepositoryAuditObservedInput(
            certificate_id=valid.certificate_id,
            purpose=valid.purpose,
            runtime=object(),
            schema=valid.schema,
            coverage=valid.coverage,
            findings=valid.findings,
        )
    assert rejected.value.code == "repository_audit_runtime_type_invalid"

    with pytest.raises(RepositoryAuditContractError) as rejected:
        _observed(
            runtime=_runtime(updated_at=T2),
            findings=_findings(started_at=T1, completed_at=T3),
        )
    assert rejected.value.code == "repository_audit_runtime_time_invalid"


def test_fresh_classifier_requires_exact_generic_and_specialised_evidence() -> None:
    plan = _issue_repository_audit_certificate_plan(_observed())
    absent = _classify_repository_audit_certificate(
        plan,
        _fresh_persisted(RepositoryAuditPersistedObservation(
            RepositoryAuditPersistedReadStatus.COMPLETE,
            certificate_head=plan.before_head,
        )),
    )
    assert absent.status is RepositoryAuditReconcileStatus.ABSENT
    assert absent.retry_action is RepositoryAuditRetryAction.APPLY_EXACT

    exact = _classify_repository_audit_certificate(plan, _persisted(plan))
    assert exact.status is RepositoryAuditReconcileStatus.EXACT
    assert exact.evidence == plan.evidence
    assert exact.retry_action is RepositoryAuditRetryAction.RETURN_EXACT

    bare_receipt = _classify_repository_audit_certificate(
        plan,
        _fresh_persisted(RepositoryAuditPersistedObservation(
            RepositoryAuditPersistedReadStatus.COMPLETE,
            receipt=plan.evidence.receipt_parent,
            certificate_head=plan.before_head,
        )),
    )
    assert bare_receipt.status is RepositoryAuditReconcileStatus.CORRUPT

    certificate_without_parent = _classify_repository_audit_certificate(
        plan,
        _fresh_persisted(RepositoryAuditPersistedObservation(
            RepositoryAuditPersistedReadStatus.COMPLETE,
            certificate=plan.evidence,
            certificate_head=plan.after_head,
        )),
    )
    assert certificate_without_parent.status is RepositoryAuditReconcileStatus.CORRUPT

    wrong_kind = RepositoryAuditGenericReceiptEvidence(
        plan.evidence.transaction_id,
        "occupied_by_other_kind",
        plan.evidence.findings.completed_at,
    )
    conflict = _classify_repository_audit_certificate(
        plan,
        _fresh_persisted(RepositoryAuditPersistedObservation(
            RepositoryAuditPersistedReadStatus.COMPLETE,
            receipt=wrong_kind,
            certificate_head=plan.before_head,
        )),
    )
    assert conflict.status is RepositoryAuditReconcileStatus.CONFLICT
    assert conflict.retry_action is RepositoryAuditRetryAction.STOP

    unknown = _classify_repository_audit_certificate(
        plan,
        _fresh_persisted(RepositoryAuditPersistedObservation(
            RepositoryAuditPersistedReadStatus.UNKNOWN,
            error_code="repository_audit_storage_unreadable",
        )),
    )
    assert unknown.status is RepositoryAuditReconcileStatus.UNKNOWN
    assert unknown.retry_action is RepositoryAuditRetryAction.RECONCILE_REQUIRED


def test_full_audit_and_durable_read_provenance_cannot_be_plain_dataclasses() -> None:
    observed = _observed()
    with pytest.raises(RepositoryAuditIntegrityError) as rejected:
        _issue_stamped_plan(observed)
    assert rejected.value.code == "repository_fresh_audit_observation_invalid"

    plan = _issue_repository_audit_certificate_plan(observed)
    raw_persisted = RepositoryAuditPersistedObservation(
        RepositoryAuditPersistedReadStatus.COMPLETE,
        receipt=plan.evidence.receipt_parent,
        certificate=plan.evidence,
        certificate_head=plan.after_head,
    )
    with pytest.raises(RepositoryAuditIntegrityError) as rejected:
        _classify_repository_audit_certificate(plan, raw_persisted)
    assert rejected.value.code == "repository_audit_fresh_observation_invalid"

    forged = object.__new__(RepositoryAuditFreshObservation)
    object.__setattr__(forged, "persisted", raw_persisted)
    object.__setattr__(forged, "_reader", object())
    object.__setattr__(forged, "_issuer", object())
    with pytest.raises(RepositoryAuditIntegrityError) as rejected:
        _classify_repository_audit_certificate(plan, forged)
    assert rejected.value.code == "repository_audit_fresh_observation_invalid"


def test_certificate_head_closes_forks_and_is_rechecked_after_write() -> None:
    first = _issue_repository_audit_certificate_plan(_observed())
    successor_observed = _observed(
        "b" * 32,
        runtime=_runtime(updated_at=T2),
        coverage=_coverage(receipt_count=6, receipt_digest=b"s" * 32),
        findings=_findings(started_at=T3, completed_at=T4),
    )
    wrong_head = RepositoryAuditCertificateLedgerHead(
        sequence=first.after_head.sequence,
        certificate_id="f" * 32,
        manifest_sha256=b"q" * 32,
        certificate_tail_sha256=b"t" * 32,
    )
    forked = _stamp_repository_fresh_audit_observation(
        _REPOSITORY_FULL_AUDIT_ADAPTER_CAPABILITY,
        successor_observed,
        wrong_head,
        first.evidence,
    )
    with pytest.raises(RepositoryAuditIntegrityError) as rejected:
        _issue_stamped_plan(forked)
    assert rejected.value.code == (
        "repository_audit_certificate_head_predecessor_mismatch"
    )

    successor = _issue_repository_audit_certificate_plan(
        successor_observed,
        first.evidence,
    )
    assert successor.before_head == first.after_head
    assert successor.after_head.sequence == first.after_head.sequence + 1

    exact_row_without_head_cas = _fresh_persisted(
        RepositoryAuditPersistedObservation(
            RepositoryAuditPersistedReadStatus.COMPLETE,
            receipt=successor.evidence.receipt_parent,
            certificate=successor.evidence,
            certificate_head=successor.before_head,
        )
    )
    result = _classify_repository_audit_certificate(
        successor,
        exact_row_without_head_cas,
    )
    assert result.status is RepositoryAuditReconcileStatus.CORRUPT
    assert result.error_code == "repository_audit_certificate_head_not_advanced"

    historical_retry = _fresh_persisted(
        RepositoryAuditPersistedObservation(
            RepositoryAuditPersistedReadStatus.COMPLETE,
            receipt=first.evidence.receipt_parent,
            certificate=first.evidence,
            certificate_head=successor.after_head,
        )
    )
    result = _classify_repository_audit_certificate(first, historical_retry)
    assert result.status is RepositoryAuditReconcileStatus.CONFLICT
    assert result.error_code == "repository_audit_certificate_head_conflict"


def test_independent_ledger_reader_proves_historical_head_membership() -> None:
    first = _issue_repository_audit_certificate_plan(_observed())
    successor = _issue_repository_audit_certificate_plan(
        _observed(
            "b" * 32,
            runtime=_runtime(updated_at=T2),
            coverage=_coverage(receipt_count=6, receipt_digest=b"s" * 32),
            findings=_findings(started_at=T3, completed_at=T4),
        ),
        first.evidence,
    )
    raw = RepositoryAuditMembershipObservation(
        read_status=RepositoryAuditPersistedReadStatus.COMPLETE,
        receipt=first.evidence.receipt_parent,
        certificate=first.evidence,
        current_head=successor.after_head,
        successor_chain=(
            RepositoryAuditCertificateLedgerEntry(
                receipt=successor.evidence.receipt_parent,
                certificate=successor.evidence,
            ),
        ),
    )
    fresh = _stamp_repository_audit_fresh_membership_observation(
        _REPOSITORY_AUDIT_LEDGER_READER_CAPABILITY,
        raw,
    )
    exact = _classify_repository_audit_certificate_membership(first, fresh)
    assert exact.status is RepositoryAuditReconcileStatus.EXACT
    assert exact.evidence == first.evidence

    missing_link = _stamp_repository_audit_fresh_membership_observation(
        _REPOSITORY_AUDIT_LEDGER_READER_CAPABILITY,
        dataclasses.replace(raw, successor_chain=()),
    )
    conflict = _classify_repository_audit_certificate_membership(
        first,
        missing_link,
    )
    assert conflict.status is RepositoryAuditReconcileStatus.CONFLICT

    missing_receipt = _stamp_repository_audit_fresh_membership_observation(
        _REPOSITORY_AUDIT_LEDGER_READER_CAPABILITY,
        dataclasses.replace(
            raw,
            successor_chain=(
                RepositoryAuditCertificateLedgerEntry(
                    receipt=None,
                    certificate=successor.evidence,
                ),
            ),
        ),
    )
    corrupt = _classify_repository_audit_certificate_membership(
        first,
        missing_receipt,
    )
    assert corrupt.status is RepositoryAuditReconcileStatus.CORRUPT

    wrong_kind = RepositoryAuditGenericReceiptEvidence(
        successor.evidence.transaction_id,
        "occupied_by_other_kind",
        successor.evidence.findings.completed_at,
    )
    wrong_receipt = _stamp_repository_audit_fresh_membership_observation(
        _REPOSITORY_AUDIT_LEDGER_READER_CAPABILITY,
        dataclasses.replace(
            raw,
            successor_chain=(
                RepositoryAuditCertificateLedgerEntry(
                    receipt=wrong_kind,
                    certificate=successor.evidence,
                ),
            ),
        ),
    )
    corrupt = _classify_repository_audit_certificate_membership(
        first,
        wrong_receipt,
    )
    assert corrupt.status is RepositoryAuditReconcileStatus.CORRUPT

    with pytest.raises(RepositoryAuditIntegrityError) as rejected:
        _classify_repository_audit_certificate_membership(first, raw)
    assert rejected.value.code == "repository_audit_fresh_membership_invalid"


def test_historical_membership_hop_and_byte_budgets_fail_closed() -> None:
    plan = _issue_repository_audit_certificate_plan(_observed())
    entry = RepositoryAuditCertificateLedgerEntry(
        receipt=plan.evidence.receipt_parent,
        certificate=plan.evidence,
    )
    boundary = RepositoryAuditMembershipObservation(
        read_status=RepositoryAuditPersistedReadStatus.COMPLETE,
        receipt=plan.evidence.receipt_parent,
        certificate=plan.evidence,
        current_head=plan.after_head,
        successor_chain=(entry,) * MAX_REPOSITORY_AUDIT_MEMBERSHIP_HOPS,
    )
    assert len(boundary.successor_chain) == MAX_REPOSITORY_AUDIT_MEMBERSHIP_HOPS

    with pytest.raises(RepositoryAuditContractError) as rejected:
        dataclasses.replace(
            boundary,
            successor_chain=(entry,)
            * (MAX_REPOSITORY_AUDIT_MEMBERSHIP_HOPS + 1),
        )
    assert rejected.value.code == "repository_audit_membership_budget_exceeded"

    large_certificate = dataclasses.replace(
        plan.evidence,
        manifest_bytes=b"x" * MAX_REPOSITORY_AUDIT_MANIFEST_BYTES,
    )
    large_entry = RepositoryAuditCertificateLedgerEntry(
        receipt=plan.evidence.receipt_parent,
        certificate=large_certificate,
    )
    assert 5 * MAX_REPOSITORY_AUDIT_MANIFEST_BYTES > (
        MAX_REPOSITORY_AUDIT_MEMBERSHIP_MANIFEST_BYTES
    )
    with pytest.raises(RepositoryAuditContractError) as rejected:
        RepositoryAuditMembershipObservation(
            read_status=RepositoryAuditPersistedReadStatus.COMPLETE,
            receipt=plan.evidence.receipt_parent,
            certificate=plan.evidence,
            current_head=plan.after_head,
            successor_chain=(large_entry,) * 5,
        )
    assert rejected.value.code == "repository_audit_membership_budget_exceeded"

    forged_oversized = object.__new__(RepositoryAuditMembershipObservation)
    object.__setattr__(
        forged_oversized,
        "read_status",
        RepositoryAuditPersistedReadStatus.COMPLETE,
    )
    object.__setattr__(
        forged_oversized,
        "receipt",
        plan.evidence.receipt_parent,
    )
    object.__setattr__(forged_oversized, "certificate", plan.evidence)
    object.__setattr__(forged_oversized, "current_head", plan.after_head)
    object.__setattr__(
        forged_oversized,
        "successor_chain",
        (entry,) * (MAX_REPOSITORY_AUDIT_MEMBERSHIP_HOPS + 1),
    )
    object.__setattr__(forged_oversized, "error_code", "")
    fresh = _stamp_repository_audit_fresh_membership_observation(
        _REPOSITORY_AUDIT_LEDGER_READER_CAPABILITY,
        forged_oversized,
    )
    result = _classify_repository_audit_certificate_membership(plan, fresh)
    assert result.status is RepositoryAuditReconcileStatus.UNKNOWN
    assert result.retry_action is RepositoryAuditRetryAction.RECONCILE_REQUIRED


def test_classifier_distinguishes_corrupt_bytes_from_valid_identity_conflict() -> None:
    plan = _issue_repository_audit_certificate_plan(_observed())
    corrupt = dataclasses.replace(
        plan.evidence,
        manifest_sha256=b"x" * 32,
    )
    corrupt_result = _classify_repository_audit_certificate(
        plan,
        _fresh_persisted(RepositoryAuditPersistedObservation(
            RepositoryAuditPersistedReadStatus.COMPLETE,
            receipt=plan.evidence.receipt_parent,
            certificate=corrupt,
            certificate_head=plan.after_head,
        )),
    )
    assert corrupt_result.status is RepositoryAuditReconcileStatus.CORRUPT

    overflowed_sequence = dataclasses.replace(plan.evidence)
    object.__setattr__(overflowed_sequence, "sequence", 2**64)
    overflow_result = _classify_repository_audit_certificate(
        plan,
        _fresh_persisted(RepositoryAuditPersistedObservation(
            RepositoryAuditPersistedReadStatus.COMPLETE,
            receipt=plan.evidence.receipt_parent,
            certificate=overflowed_sequence,
            certificate_head=plan.after_head,
        )),
    )
    assert overflow_result.status is RepositoryAuditReconcileStatus.CORRUPT

    alternate = _issue_repository_audit_certificate_plan(
        _observed(schema=_schema(fingerprint="9" * 64))
    )
    assert alternate.evidence.certificate_id == plan.evidence.certificate_id
    assert alternate.evidence.receipt_parent == plan.evidence.receipt_parent
    valid_conflict = _classify_repository_audit_certificate(
        plan,
        _persisted(alternate),
    )
    assert valid_conflict.status is RepositoryAuditReconcileStatus.CONFLICT


def test_contract_and_integrity_are_pure_private_py39_compatible_leaves() -> None:
    root = Path(__file__).resolve().parent
    forbidden_imports = {
        "sqlite3",
        "sqlite_business_runtime",
        "sqlite_kernel",
        "pathlib",
    }
    for name in ("repository_audit_contract.py", "repository_audit_integrity.py"):
        source = (root / name).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".", 1)[0])
            assert type(node).__name__ != "Match"
        assert imported.isdisjoint(forbidden_imports)
        assert " | " not in source
        assert "def _issue_repository_full_audit_adapter_capability" not in source
        assert "def _issue_repository_audit_durable_reader_capability" not in source
        assert "def _issue_repository_audit_ledger_reader_capability" not in source
    assert contract_module.__all__ == ()
    assert integrity_module.__all__ == ()
