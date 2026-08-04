"""Adversarial tests for final cutover mount-authorization composition."""

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

import business_cutover_mount_contract as contract_module
import business_cutover_mount_integrity as integrity_module
import test_business_cutover_activation_integrity as activation_support
import test_business_cutover_asset_integrity as asset_support
from business_cutover_activation_contract import (
    BusinessCutoverActivationPersistedObservation,
    BusinessCutoverActivationPersistedReadStatus,
)
from business_cutover_activation_integrity import (
    _issue_business_cutover_activation_plan,
)
from business_cutover_asset_integrity import (
    _issue_business_cutover_asset_coverage_plan,
    _issue_business_cutover_asset_stage_plan,
    _repository_member_root,
)
from business_cutover_mount_contract import (
    BUSINESS_CUTOVER_MOUNT_MANIFEST_SCHEMA,
    BUSINESS_CUTOVER_MOUNT_PROTOCOL,
    BUSINESS_CUTOVER_MOUNT_TRANSACTION_KIND,
    BUSINESS_CUTOVER_MOUNT_TRANSACTION_PREFIX,
    BusinessCutoverMountContractError,
    BusinessCutoverMountFreshObservation,
    BusinessCutoverMountGenericReceiptEvidence,
    BusinessCutoverMountPersistedObservation,
    BusinessCutoverMountPersistedReadStatus,
    BusinessCutoverMountReconcileStatus,
    BusinessCutoverMountRetryAction,
    _BUSINESS_CUTOVER_MOUNT_DURABLE_READER_CAPABILITY,
    _stamp_business_cutover_mount_fresh_observation,
)
from business_cutover_mount_integrity import (
    BUSINESS_CUTOVER_MOUNT_MANIFEST_HASH_DOMAIN,
    BusinessCutoverMountIntegrityError,
    BusinessCutoverMountPlan,
    _COVERAGE_LINKED_RECEIPT_READER_TOKEN,
    _INSTALL_LINKED_RECEIPT_READER_TOKEN,
    _classify_business_cutover_mount,
    _issue_business_cutover_mount_plan,
    _require_business_cutover_mount_evidence,
    _require_business_cutover_mount_plan,
    _stamp_business_cutover_mount_coverage_receipt_observation,
    _stamp_business_cutover_mount_install_receipt_observation,
)
from repository_audit_contract import (
    EMPTY_WORKER_LEDGER_TAIL_SHA256,
    RepositoryAuditAuthorityState,
    RepositoryAuditCertificateLedgerEntry,
    RepositoryAuditCoverage,
    RepositoryAuditFindings,
    RepositoryAuditMembershipObservation,
    RepositoryAuditObservedInput,
    RepositoryAuditPersistedObservation,
    RepositoryAuditPersistedReadStatus,
    RepositoryAuditPurpose,
    RepositoryAuditRuntimeBinding,
    _REPOSITORY_AUDIT_DURABLE_READER_CAPABILITY,
    _REPOSITORY_AUDIT_LEDGER_READER_CAPABILITY,
    _REPOSITORY_FULL_AUDIT_ADAPTER_CAPABILITY,
    _stamp_repository_audit_fresh_membership_observation,
    _stamp_repository_audit_fresh_observation,
    _stamp_repository_fresh_audit_observation,
)
from repository_audit_integrity import _issue_repository_audit_certificate_plan


T0 = activation_support.T0
T1 = activation_support.T1
T3 = activation_support.T3
T4 = activation_support.T4
T5 = activation_support.T5
T_INSTALL = "2026-07-12T00:00:05.000Z"


def _linked_receipt_observations(cutover, coverage):
    install = _stamp_business_cutover_mount_install_receipt_observation(
        evidence=cutover.install,
        receipt_parent=BusinessCutoverMountGenericReceiptEvidence(
            cutover.install.transaction_id,
            cutover.install.receipt_kind,
            T_INSTALL,
        ),
        _reader_token=_INSTALL_LINKED_RECEIPT_READER_TOKEN,
    )
    covered = _stamp_business_cutover_mount_coverage_receipt_observation(
        evidence=coverage.expected_evidence,
        receipt_parent=BusinessCutoverMountGenericReceiptEvidence(
            coverage.expected_evidence.transaction_id,
            coverage.expected_evidence.receipt_kind,
            T0,
        ),
        _reader_token=_COVERAGE_LINKED_RECEIPT_READER_TOKEN,
    )
    return install, covered


def _linked_receipt_arguments(cutover, coverage):
    install, covered = _linked_receipt_observations(cutover, coverage)
    return {
        "install_receipt_observation": install,
        "coverage_receipt_observation": covered,
    }


def _bundle(*, mounted_at: str = T4):
    snapshot = asset_support._snapshot(
        "project-a",
        (
            asset_support._ref(
                asset_support.SHA_A,
                size=len(asset_support.BLOB_A),
            ),
        ),
    )
    cutover, snapshots = asset_support._cutover(
        (snapshot,),
        include_deleted=True,
    )
    stage = _issue_business_cutover_asset_stage_plan(
        cutover_plan=cutover,
        business_snapshots=snapshots,
        blob_carriers=(asset_support._carrier(asset_support.BLOB_A),),
    )
    coverage = _issue_business_cutover_asset_coverage_plan(
        cutover_plan=cutover,
        business_snapshots=snapshots,
        stage_observations=asset_support._exact_stage_observations(
            cutover,
            (stage,),
        ),
    )
    coverage_observation = asset_support._observe_coverage(
        coverage,
        cutover.final_admission,
        coverage.expected_evidence,
    )
    audit_plan, audit_read = activation_support._audit_bundle(cutover)
    activation_prerequisites = activation_support._fresh_prerequisites(cutover)
    activation = _issue_business_cutover_activation_plan(
        cutover_plan=cutover,
        fresh_prerequisites=activation_prerequisites,
        audit_plan=audit_plan,
        audit_persisted=audit_read,
        activated_at=T3,
    )
    activation_read = activation_support._activation_read(activation)
    install_receipt, coverage_receipt = _linked_receipt_observations(
        cutover,
        coverage,
    )
    plan = _issue_business_cutover_mount_plan(
        cutover_plan=cutover,
        coverage_plan=coverage,
        coverage_observation=coverage_observation,
        activation_plan=activation,
        install_receipt_observation=install_receipt,
        coverage_receipt_observation=coverage_receipt,
        activation_fresh_prerequisites=(
            activation_support._fresh_prerequisites(cutover)
        ),
        activation_audit_persisted=activation_support._audit_read(
            audit_plan,
            exact=True,
        ),
        activation_persisted=activation_read,
        mounted_at=mounted_at,
    )
    return (
        cutover,
        snapshots,
        coverage,
        coverage_observation,
        audit_plan,
        activation,
        plan,
    )


def _fresh_mount(raw):
    return _stamp_business_cutover_mount_fresh_observation(
        _BUSINESS_CUTOVER_MOUNT_DURABLE_READER_CAPABILITY,
        raw,
    )


def _exact_mount_read(plan):
    return _fresh_mount(BusinessCutoverMountPersistedObservation(
        BusinessCutoverMountPersistedReadStatus.COMPLETE,
        receipt=plan.evidence.receipt_parent,
        mount=plan.evidence,
    ))


def _classify(plan, cutover, coverage, audit_plan, activation, persisted, **kwargs):
    values = {
        "coverage_observation": asset_support._observe_coverage(
            coverage,
            cutover.final_admission,
            coverage.expected_evidence,
        ),
        "activation_fresh_prerequisites": (
            activation_support._fresh_prerequisites(cutover)
        ),
        "activation_audit_persisted": activation_support._audit_read(
            audit_plan,
            exact=True,
        ),
        "activation_persisted": activation_support._activation_read(activation),
        "persisted": persisted,
    }
    values.update(kwargs)
    return _classify_business_cutover_mount(plan, **values)


def test_mount_plan_closes_install_assets_activation_and_fixed_vector() -> None:
    cutover, _snapshots, coverage, _coverage_read, audit, activation, plan = (
        _bundle()
    )
    assert isinstance(plan, BusinessCutoverMountPlan)
    assert _require_business_cutover_mount_plan(plan) is plan
    evidence = _require_business_cutover_mount_evidence(plan.evidence)
    assert evidence.cutover_id == cutover.cutover_id
    assert evidence.transaction_id == (
        BUSINESS_CUTOVER_MOUNT_TRANSACTION_PREFIX + cutover.cutover_id
    )
    assert evidence.receipt_kind == BUSINESS_CUTOVER_MOUNT_TRANSACTION_KIND
    assert evidence.install.transaction_id == cutover.install.transaction_id
    assert evidence.install.manifest_sha256 == cutover.install.manifest_sha256
    assert evidence.coverage.transaction_id == coverage.transaction_id
    assert evidence.coverage.manifest_sha256 == (
        coverage.expected_evidence.manifest_sha256
    )
    assert evidence.coverage.coverage_sha256 == (
        coverage.expected_evidence.coverage_sha256
    )
    assert evidence.coverage.member_root_sha256 == _repository_member_root(cutover)
    assert evidence.coverage.repository_member_count == 2
    assert evidence.coverage.live_project_count == 1
    assert evidence.coverage.deleted_project_count == 1
    assert evidence.coverage.reference_count == 1
    assert evidence.coverage.unique_blob_bytes == len(asset_support.BLOB_A)
    assert evidence.activation.transaction_id == activation.transaction_id
    assert evidence.activation.manifest_sha256 == activation.evidence.manifest_sha256
    assert evidence.activation.audit_certificate_id == audit.evidence.certificate_id
    assert evidence.activation.audit_manifest_sha256 == audit.evidence.manifest_sha256
    assert evidence.runtime.repository_epoch == cutover.install.runtime.repository_epoch
    assert evidence.runtime.authority_state == "active"
    assert evidence.runtime.state_revision == 2
    assert evidence.runtime.target_schema_version == (
        cutover.expected_target_schema_version
    )
    assert evidence.runtime.target_schema_fingerprint == (
        cutover.expected_target_schema_fingerprint
    )
    assert evidence.manifest_sha256.hex() == (
        "f16a762bab8413bee4f7684cc662c9773f46aa26c81fb3567f23136f55460a01"
    )

    manifest = json.loads(evidence.manifest_bytes.decode("utf-8"))
    assert manifest["schema"] == BUSINESS_CUTOVER_MOUNT_MANIFEST_SCHEMA
    assert manifest["protocol"] == BUSINESS_CUTOVER_MOUNT_PROTOCOL
    assert manifest["manifest_hash_domain"] == (
        BUSINESS_CUTOVER_MOUNT_MANIFEST_HASH_DOMAIN.decode("ascii")
    )
    assert manifest["install"]["receipt_parent"] == {
        "ledger": "transaction_receipts",
        "transaction_id": cutover.install.transaction_id,
        "kind": cutover.install.receipt_kind,
        "committed_at": T_INSTALL,
        "specialised_protocol": "ze.business-cutover.v1",
    }
    assert manifest["asset_repository_coverage"]["receipt_parent"] == {
        "ledger": "transaction_receipts",
        "transaction_id": coverage.expected_evidence.transaction_id,
        "kind": coverage.expected_evidence.receipt_kind,
        "committed_at": T0,
        "specialised_protocol": "ze.business-cutover-asset-coverage.v1",
    }
    assert manifest["asset_repository_coverage"]["member_root_sha256"] == (
        _repository_member_root(cutover).hex()
    )
    assert manifest["activation"]["audit_certificate"]["certificate_id"] == (
        audit.evidence.certificate_id
    )
    # Frozen cutover-v1 join is neither imported nor copied into the mount seal.
    assert cutover.join.transaction_id not in evidence.manifest_bytes.decode("utf-8")


def test_zero_asset_repository_keeps_live_and_deleted_members_in_mount_closure() -> None:
    snapshot = asset_support._snapshot("project-zero", ())
    cutover, snapshots = asset_support._cutover(
        (snapshot,),
        include_deleted=True,
    )
    coverage = _issue_business_cutover_asset_coverage_plan(
        cutover_plan=cutover,
        business_snapshots=snapshots,
        stage_observations=(),
    )
    audit, audit_read = activation_support._audit_bundle(cutover)
    prerequisites = activation_support._fresh_prerequisites(cutover)
    activation = _issue_business_cutover_activation_plan(
        cutover_plan=cutover,
        fresh_prerequisites=prerequisites,
        audit_plan=audit,
        audit_persisted=audit_read,
        activated_at=T3,
    )
    mount = _issue_business_cutover_mount_plan(
        cutover_plan=cutover,
        coverage_plan=coverage,
        coverage_observation=asset_support._observe_coverage(
            coverage,
            cutover.final_admission,
            coverage.expected_evidence,
        ),
        activation_plan=activation,
        **_linked_receipt_arguments(cutover, coverage),
        activation_fresh_prerequisites=(
            activation_support._fresh_prerequisites(cutover)
        ),
        activation_audit_persisted=activation_support._audit_read(
            audit,
            exact=True,
        ),
        activation_persisted=activation_support._activation_read(activation),
        mounted_at=T4,
    )
    assert mount.evidence.coverage.batch_count == 0
    assert mount.evidence.coverage.reference_count == 0
    assert mount.evidence.coverage.unique_blob_bytes == 0
    assert mount.evidence.coverage.repository_member_count == 2
    assert mount.evidence.coverage.live_project_count == 1
    assert mount.evidence.coverage.deleted_project_count == 1
    assert mount.evidence.coverage.member_root_sha256 == _repository_member_root(
        cutover
    )


@pytest.mark.parametrize(
    "dependency",
    (
        "coverage_absent",
        "coverage_unknown",
        "coverage_source_exact",
        "activation_absent",
        "activation_unknown",
    ),
)
def test_issuer_requires_fresh_exact_persisted_dependencies(dependency: str) -> None:
    cutover, snapshots, coverage, coverage_read, audit, activation, _plan = _bundle()
    selected_coverage = coverage_read
    selected_activation = activation_support._activation_read(activation)
    if dependency == "coverage_absent":
        selected_coverage = asset_support._observe_coverage(
            coverage,
            cutover.final_admission,
            None,
        )
    elif dependency == "coverage_unknown":
        selected_coverage = asset_support._observe_coverage(
            coverage,
            cutover.final_admission,
            None,
            indeterminate=True,
        )
    elif dependency == "coverage_source_exact":
        selected_coverage = asset_support._observe_coverage(
            coverage,
            cutover.source_admission,
            coverage.expected_evidence,
        )
    elif dependency == "activation_absent":
        selected_activation = activation_support._activation_read(
            activation,
            BusinessCutoverActivationPersistedObservation(
                BusinessCutoverActivationPersistedReadStatus.COMPLETE,
                runtime=activation.evidence.before_runtime,
            ),
        )
    else:
        selected_activation = activation_support._activation_read(
            activation,
            BusinessCutoverActivationPersistedObservation(
                BusinessCutoverActivationPersistedReadStatus.UNKNOWN,
                error_code="activation_storage_unreadable",
            ),
        )
    with pytest.raises(BusinessCutoverMountIntegrityError):
        _issue_business_cutover_mount_plan(
            cutover_plan=cutover,
            coverage_plan=coverage,
            coverage_observation=selected_coverage,
            activation_plan=activation,
            **_linked_receipt_arguments(cutover, coverage),
            activation_fresh_prerequisites=(
                activation_support._fresh_prerequisites(cutover)
            ),
            activation_audit_persisted=activation_support._audit_read(
                audit,
                exact=True,
            ),
            activation_persisted=selected_activation,
            mounted_at=T4,
        )
    # Keep the fixture visibly bound to the same exact business snapshots.
    assert len(snapshots) == 1


def test_raw_upstream_plan_evidence_cannot_substitute_for_fresh_reads() -> None:
    cutover, _snapshots, coverage, _coverage_read, audit, activation, _plan = (
        _bundle()
    )
    with pytest.raises(BusinessCutoverMountIntegrityError) as coverage_error:
        _issue_business_cutover_mount_plan(
            cutover_plan=cutover,
            coverage_plan=coverage,
            coverage_observation=coverage.expected_evidence,  # type: ignore[arg-type]
            activation_plan=activation,
            **_linked_receipt_arguments(cutover, coverage),
            activation_fresh_prerequisites=(
                activation_support._fresh_prerequisites(cutover)
            ),
            activation_audit_persisted=activation_support._audit_read(
                audit,
                exact=True,
            ),
            activation_persisted=activation_support._activation_read(activation),
            mounted_at=T4,
        )
    assert coverage_error.value.code == (
        "business_cutover_mount_coverage_observation_invalid"
    )
    with pytest.raises(BusinessCutoverMountIntegrityError) as activation_error:
        _issue_business_cutover_mount_plan(
            cutover_plan=cutover,
            coverage_plan=coverage,
            coverage_observation=asset_support._observe_coverage(
                coverage,
                cutover.final_admission,
                coverage.expected_evidence,
            ),
            activation_plan=activation,
            **_linked_receipt_arguments(cutover, coverage),
            activation_fresh_prerequisites=(
                activation_support._fresh_prerequisites(cutover)
            ),
            activation_audit_persisted=activation_support._audit_read(
                audit,
                exact=True,
            ),
            activation_persisted=activation.evidence,  # type: ignore[arg-type]
            mounted_at=T4,
        )
    assert activation_error.value.code == (
        "business_cutover_mount_activation_observation_invalid"
    )


def test_issuer_requires_reader_stamped_linked_receipts_and_ordered_times() -> None:
    cutover, _snapshots, coverage, coverage_read, audit, activation, _plan = (
        _bundle()
    )
    install_observation, coverage_observation = _linked_receipt_observations(
        cutover,
        coverage,
    )
    common = {
        "cutover_plan": cutover,
        "coverage_plan": coverage,
        "coverage_observation": coverage_read,
        "activation_plan": activation,
        "activation_fresh_prerequisites": (
            activation_support._fresh_prerequisites(cutover)
        ),
        "activation_audit_persisted": activation_support._audit_read(
            audit,
            exact=True,
        ),
        "activation_persisted": activation_support._activation_read(activation),
        "mounted_at": T4,
    }
    with pytest.raises(BusinessCutoverMountIntegrityError) as unstamped:
        _issue_business_cutover_mount_plan(
            **common,
            install_receipt_observation=BusinessCutoverMountGenericReceiptEvidence(
                cutover.install.transaction_id,
                cutover.install.receipt_kind,
                T_INSTALL,
            ),  # type: ignore[arg-type]
            coverage_receipt_observation=coverage_observation,
        )
    assert unstamped.value.code == (
        "business_cutover_mount_linked_receipt_observation_invalid"
    )

    late_install = _stamp_business_cutover_mount_install_receipt_observation(
        evidence=cutover.install,
        receipt_parent=BusinessCutoverMountGenericReceiptEvidence(
            cutover.install.transaction_id,
            cutover.install.receipt_kind,
            T5,
        ),
        _reader_token=_INSTALL_LINKED_RECEIPT_READER_TOKEN,
    )
    with pytest.raises(BusinessCutoverMountIntegrityError) as unordered:
        _issue_business_cutover_mount_plan(
            **common,
            install_receipt_observation=late_install,
            coverage_receipt_observation=coverage_observation,
        )
    assert unordered.value.code == (
        "business_cutover_mount_linked_receipt_after_audit_started"
    )

    post_audit_start_coverage = (
        _stamp_business_cutover_mount_coverage_receipt_observation(
            evidence=coverage.expected_evidence,
            receipt_parent=BusinessCutoverMountGenericReceiptEvidence(
                coverage.expected_evidence.transaction_id,
                coverage.expected_evidence.receipt_kind,
                "2026-07-12T00:00:15.000Z",
            ),
            _reader_token=_COVERAGE_LINKED_RECEIPT_READER_TOKEN,
        )
    )
    with pytest.raises(BusinessCutoverMountIntegrityError) as after_start:
        _issue_business_cutover_mount_plan(
            **common,
            install_receipt_observation=install_observation,
            coverage_receipt_observation=post_audit_start_coverage,
        )
    assert after_start.value.code == (
        "business_cutover_mount_linked_receipt_after_audit_started"
    )

    early_install = _stamp_business_cutover_mount_install_receipt_observation(
        evidence=cutover.install,
        receipt_parent=BusinessCutoverMountGenericReceiptEvidence(
            cutover.install.transaction_id,
            cutover.install.receipt_kind,
            T0,
        ),
        _reader_token=_INSTALL_LINKED_RECEIPT_READER_TOKEN,
    )
    late_coverage = _stamp_business_cutover_mount_coverage_receipt_observation(
        evidence=coverage.expected_evidence,
        receipt_parent=BusinessCutoverMountGenericReceiptEvidence(
            coverage.expected_evidence.transaction_id,
            coverage.expected_evidence.receipt_kind,
            T_INSTALL,
        ),
        _reader_token=_COVERAGE_LINKED_RECEIPT_READER_TOKEN,
    )
    with pytest.raises(BusinessCutoverMountIntegrityError) as unordered:
        _issue_business_cutover_mount_plan(
            **common,
            install_receipt_observation=early_install,
            coverage_receipt_observation=late_coverage,
        )
    assert unordered.value.code == (
        "business_cutover_mount_linked_receipt_order_invalid"
    )
    assert install_observation.receipt_parent.committed_at == T_INSTALL


def test_foreign_coverage_and_tampered_constituent_identity_are_rejected() -> None:
    cutover, _snapshots, _coverage, _coverage_read, audit, activation, _plan = (
        _bundle()
    )
    foreign_snapshot = asset_support._snapshot(
        "project-foreign",
        (asset_support._ref(asset_support.SHA_A, size=len(asset_support.BLOB_A)),),
    )
    foreign_cutover, foreign_snapshots = asset_support._cutover(
        (foreign_snapshot,),
        repository_epoch="9" * 64,
    )
    foreign_stage = _issue_business_cutover_asset_stage_plan(
        cutover_plan=foreign_cutover,
        business_snapshots=foreign_snapshots,
        blob_carriers=(asset_support._carrier(asset_support.BLOB_A),),
    )
    foreign_coverage = _issue_business_cutover_asset_coverage_plan(
        cutover_plan=foreign_cutover,
        business_snapshots=foreign_snapshots,
        stage_observations=asset_support._exact_stage_observations(
            foreign_cutover,
            (foreign_stage,),
        ),
    )
    with pytest.raises(BusinessCutoverMountIntegrityError) as rejected:
        _issue_business_cutover_mount_plan(
            cutover_plan=cutover,
            coverage_plan=foreign_coverage,
            coverage_observation=asset_support._observe_coverage(
                foreign_coverage,
                foreign_cutover.final_admission,
                foreign_coverage.expected_evidence,
            ),
            activation_plan=activation,
            **_linked_receipt_arguments(cutover, foreign_coverage),
            activation_fresh_prerequisites=(
                activation_support._fresh_prerequisites(cutover)
            ),
            activation_audit_persisted=activation_support._audit_read(
                audit,
                exact=True,
            ),
            activation_persisted=activation_support._activation_read(activation),
            mounted_at=T4,
        )
    assert rejected.value.code == "business_cutover_mount_coverage_identity_mismatch"


def test_classifier_distinguishes_absent_exact_unknown_and_rechecks_dependencies() -> None:
    cutover, _snapshots, coverage, _coverage_read, audit, activation, plan = (
        _bundle()
    )
    absent = _classify(
        plan,
        cutover,
        coverage,
        audit,
        activation,
        _fresh_mount(BusinessCutoverMountPersistedObservation(
            BusinessCutoverMountPersistedReadStatus.COMPLETE,
        )),
    )
    assert absent.status is BusinessCutoverMountReconcileStatus.ABSENT
    assert absent.retry_action is BusinessCutoverMountRetryAction.APPLY_EXACT
    exact = _classify(
        plan,
        cutover,
        coverage,
        audit,
        activation,
        _exact_mount_read(plan),
    )
    assert exact.status is BusinessCutoverMountReconcileStatus.EXACT
    assert exact.evidence == plan.evidence
    assert exact.retry_action is BusinessCutoverMountRetryAction.RETURN_EXACT
    unknown = _classify(
        plan,
        cutover,
        coverage,
        audit,
        activation,
        _fresh_mount(BusinessCutoverMountPersistedObservation(
            BusinessCutoverMountPersistedReadStatus.UNKNOWN,
            error_code="mount_storage_unreadable",
        )),
    )
    assert unknown.status is BusinessCutoverMountReconcileStatus.UNKNOWN
    assert unknown.retry_action is BusinessCutoverMountRetryAction.RECONCILE_REQUIRED

    lost_coverage = _classify(
        plan,
        cutover,
        coverage,
        audit,
        activation,
        _exact_mount_read(plan),
        coverage_observation=asset_support._observe_coverage(
            coverage,
            cutover.final_admission,
            None,
        ),
    )
    assert lost_coverage.status is BusinessCutoverMountReconcileStatus.CORRUPT
    assert lost_coverage.evidence is None


def test_exact_mount_acknowledges_activation_after_audit_head_advances() -> None:
    cutover, _snapshots, coverage, _coverage_read, install_audit, activation, plan = (
        _bundle()
    )
    after = activation.evidence.after_runtime
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
    successor = _issue_repository_audit_certificate_plan(
        _stamp_repository_fresh_audit_observation(
            _REPOSITORY_FULL_AUDIT_ADAPTER_CAPABILITY,
            successor_observed,
            install_audit.after_head,
            install_audit.evidence,
        )
    )
    historical = _stamp_repository_audit_fresh_observation(
        _REPOSITORY_AUDIT_DURABLE_READER_CAPABILITY,
        RepositoryAuditPersistedObservation(
            RepositoryAuditPersistedReadStatus.COMPLETE,
            receipt=install_audit.evidence.receipt_parent,
            certificate=install_audit.evidence,
            certificate_head=successor.after_head,
        ),
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
    exact = _classify(
        plan,
        cutover,
        coverage,
        install_audit,
        activation,
        _exact_mount_read(plan),
        activation_audit_persisted=historical,
        activation_audit_membership=membership,
    )
    assert exact.status is BusinessCutoverMountReconcileStatus.EXACT
    assert exact.evidence == plan.evidence


def test_partial_wrong_kind_tamper_and_valid_content_conflict_never_exact() -> None:
    cutover, _snapshots, coverage, _coverage_read, audit, activation, plan = (
        _bundle()
    )
    row_without_receipt = _classify(
        plan,
        cutover,
        coverage,
        audit,
        activation,
        _fresh_mount(BusinessCutoverMountPersistedObservation(
            BusinessCutoverMountPersistedReadStatus.COMPLETE,
            mount=plan.evidence,
        )),
    )
    assert row_without_receipt.status is BusinessCutoverMountReconcileStatus.CORRUPT

    wrong_kind = BusinessCutoverMountGenericReceiptEvidence(
        transaction_id=plan.transaction_id,
        kind="occupied_by_other_kind",
        committed_at=plan.evidence.mounted_at,
    )
    occupied = _classify(
        plan,
        cutover,
        coverage,
        audit,
        activation,
        _fresh_mount(BusinessCutoverMountPersistedObservation(
            BusinessCutoverMountPersistedReadStatus.COMPLETE,
            receipt=wrong_kind,
        )),
    )
    assert occupied.status is BusinessCutoverMountReconcileStatus.CONFLICT

    tampered = dataclasses.replace(plan.evidence, manifest_sha256=b"x" * 32)
    corrupt = _classify(
        plan,
        cutover,
        coverage,
        audit,
        activation,
        _fresh_mount(BusinessCutoverMountPersistedObservation(
            BusinessCutoverMountPersistedReadStatus.COMPLETE,
            receipt=plan.evidence.receipt_parent,
            mount=tampered,
        )),
    )
    assert corrupt.status is BusinessCutoverMountReconcileStatus.CORRUPT

    alternate = _issue_business_cutover_mount_plan(
        cutover_plan=cutover,
        coverage_plan=coverage,
        coverage_observation=asset_support._observe_coverage(
            coverage,
            cutover.final_admission,
            coverage.expected_evidence,
        ),
        activation_plan=activation,
        **_linked_receipt_arguments(cutover, coverage),
        activation_fresh_prerequisites=activation_support._fresh_prerequisites(cutover),
        activation_audit_persisted=activation_support._audit_read(audit, exact=True),
        activation_persisted=activation_support._activation_read(activation),
        mounted_at=T5,
    )
    conflict = _classify(
        plan,
        cutover,
        coverage,
        audit,
        activation,
        _fresh_mount(BusinessCutoverMountPersistedObservation(
            BusinessCutoverMountPersistedReadStatus.COMPLETE,
            receipt=alternate.evidence.receipt_parent,
            mount=alternate.evidence,
        )),
    )
    assert conflict.status is BusinessCutoverMountReconcileStatus.CONFLICT


def test_plan_and_fresh_provenance_tamper_are_detected() -> None:
    cutover, _snapshots, coverage, _coverage_read, audit, activation, plan = (
        _bundle()
    )
    original = plan.evidence
    object.__setattr__(
        plan,
        "evidence",
        dataclasses.replace(
            original,
            coverage=dataclasses.replace(
                original.coverage,
                member_root_sha256=b"x" * 32,
            ),
        ),
    )
    with pytest.raises(BusinessCutoverMountIntegrityError) as changed:
        _require_business_cutover_mount_plan(plan)
    assert changed.value.code == "business_cutover_mount_plan_changed"

    fresh_plan = _bundle()[-1]
    raw = BusinessCutoverMountPersistedObservation(
        BusinessCutoverMountPersistedReadStatus.COMPLETE,
        receipt=fresh_plan.evidence.receipt_parent,
        mount=fresh_plan.evidence,
    )
    with pytest.raises(BusinessCutoverMountIntegrityError):
        _classify_business_cutover_mount(
            fresh_plan,
            coverage_observation=asset_support._observe_coverage(
                coverage,
                cutover.final_admission,
                coverage.expected_evidence,
            ),
            activation_fresh_prerequisites=activation_support._fresh_prerequisites(cutover),
            activation_audit_persisted=activation_support._audit_read(audit, exact=True),
            activation_persisted=activation_support._activation_read(activation),
            persisted=raw,  # type: ignore[arg-type]
        )
    forged = object.__new__(BusinessCutoverMountFreshObservation)
    object.__setattr__(forged, "persisted", raw)
    object.__setattr__(forged, "_reader", object())
    object.__setattr__(forged, "_issuer", object())
    with pytest.raises(BusinessCutoverMountIntegrityError):
        _classify_business_cutover_mount(
            fresh_plan,
            coverage_observation=asset_support._observe_coverage(
                coverage,
                cutover.final_admission,
                coverage.expected_evidence,
            ),
            activation_fresh_prerequisites=activation_support._fresh_prerequisites(cutover),
            activation_audit_persisted=activation_support._audit_read(audit, exact=True),
            activation_persisted=activation_support._activation_read(activation),
            persisted=forged,
        )
    with pytest.raises(BusinessCutoverMountContractError):
        _stamp_business_cutover_mount_fresh_observation(object(), raw)  # type: ignore[arg-type]


def test_mount_leaves_are_private_pure_py39_and_do_not_consume_v1_join() -> None:
    root = Path(__file__).resolve().parent
    forbidden_imports = {"sqlite3", "sqlite_kernel", "pathlib", "os", "threading"}
    for name in (
        "business_cutover_mount_contract.py",
        "business_cutover_mount_integrity.py",
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
    assert contract_module.__all__ == ()
    assert integrity_module.__all__ == ()
