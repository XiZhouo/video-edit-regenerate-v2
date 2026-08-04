from __future__ import annotations

import dataclasses
from dataclasses import replace
from enum import Enum
import json

import pytest

from business_cutover_phase_binding_contract import (
    BUSINESS_CUTOVER_PHASE_BINDING_DOMAINS,
    BusinessCutoverActivationEvidence,
    BusinessCutoverAdmissionCheckpoint,
    BusinessCutoverAllocationHighWater,
    BusinessCutoverAssetCoverageCheckpoint,
    BusinessCutoverAuditEvidence,
    BusinessCutoverDispatchHighWater,
    BusinessCutoverDurableCheckpoint,
    BusinessCutoverMaintenanceCheckpoint,
    BusinessCutoverMountEvidence,
    BusinessCutoverPhaseBindingContractError,
    BusinessCutoverPhaseBindingHead,
    BusinessCutoverPhaseBindingIdentity,
    BusinessCutoverPhaseBindingProjection,
    BusinessCutoverPhaseBindingStage,
    BusinessCutoverReceiptPointer,
    BusinessCutoverRuntimeImage,
    BusinessCutoverWorkerHighWater,
)
from business_cutover_phase_binding_integrity import (
    BusinessCutoverPhaseBindingIntegrityError,
    _verify_business_cutover_phase_binding_chain,
    issue_business_cutover_asset_provider_evidence,
    issue_business_cutover_phase_binding,
    verify_business_cutover_phase_binding,
)
from business_cutover_phase_binding_persistability import (
    BusinessCutoverPhaseBindingPersistabilityError,
    _business_cutover_phase_binding_layout,
    _project_business_cutover_phase_binding_row,
)
from business_cutover_head_integrity import (
    BusinessCutoverHeadInput,
    BusinessCutoverHeadPointer,
    BusinessCutoverHeadPointerRole,
    issue_business_cutover_head_chain,
)


ZERO = b"\0" * 32
CUTOVER_ID = "1" * 64
REPOSITORY_EPOCH = "2" * 64
SCHEMA_FINGERPRINT = "3" * 64
T_COVERAGE = "2026-07-13T00:01:00.000Z"
T_INSTALL = "2026-07-13T00:02:00.000Z"
T_AUDIT = "2026-07-13T00:03:00.000Z"
T_ACTIVATION = "2026-07-13T00:04:00.000Z"
T_MOUNT = "2026-07-13T00:05:00.000Z"
T_OBSERVED = "2026-07-13T00:06:00.000Z"


def _d(value: int) -> bytes:
    return bytes([value]) * 32


def _pointer(prefix: str, suffix: str, kind: str, at: str, digest: int):
    return BusinessCutoverReceiptPointer(
        prefix + suffix,
        kind,
        at,
        _d(digest),
    )


INSTALL = _pointer(
    "_business-cutover-install:",
    CUTOVER_ID,
    "_business_cutover_install_v1",
    T_INSTALL,
    20,
)
COVERAGE_POINTER = _pointer(
    "_business-cutover-asset-coverage:",
    CUTOVER_ID,
    "_business_cutover_asset_coverage_v1",
    T_COVERAGE,
    21,
)
AUDIT_ID = "4" * 32
AUDIT_POINTER = _pointer(
    "_repository-audit:",
    AUDIT_ID,
    "_repository_audit_certificate_v1",
    T_AUDIT,
    22,
)
ACTIVATION_POINTER = _pointer(
    "_business-cutover-activation:",
    CUTOVER_ID,
    "_business_cutover_activation_v2",
    T_ACTIVATION,
    23,
)
MOUNT_POINTER = _pointer(
    "_business-cutover-mount:",
    CUTOVER_ID,
    "_business_cutover_mount_v2",
    T_MOUNT,
    24,
)


def _runtime(authority_state: str, state_revision: int, at: str):
    return BusinessCutoverRuntimeImage(
        authority="sqlite",
        authority_state=authority_state,
        source_admission_revision=7,
        repository_revision=3,
        state_revision=state_revision,
        schema_version=6,
        updated_at=at,
    )


COVERAGE = BusinessCutoverAssetCoverageCheckpoint(
    pointer=COVERAGE_POINTER,
    coverage_sha256=_d(40),
    member_root_sha256=_d(41),
    blob_root_sha256=_d(42),
    batch_count=2,
    repository_member_count=3,
    live_project_count=2,
    deleted_project_count=1,
    project_count=2,
    reference_count=3,
    unique_blob_count=2,
    unique_blob_bytes=600,
    referenced_bytes=900,
    observed_occurrence_count=3,
    observed_row_count=7,
    observed_manifest_bytes=1200,
)


def _provider():
    return issue_business_cutover_asset_provider_evidence(
        coverage=COVERAGE,
        provider_protocol="ze.business-cutover.asset-provider-checkpoint.v1",
        provider_id="provider-main",
        provider_generation="provider-generation-7",
        provider_sequence=9,
        checked_at=T_INSTALL,
        object_count=2,
        total_bytes=600,
        blob_root_sha256=_d(42),
    )


def _audit():
    return BusinessCutoverAuditEvidence(
        certificate_id=AUDIT_ID,
        certificate_sequence=1,
        purpose="install",
        receipt_parent=AUDIT_POINTER,
        previous_certificate_id=None,
        certificate_tail_sha256=_d(43),
        findings_sha256=_d(44),
        schema_version=13,
        schema_fingerprint=SCHEMA_FINGERPRINT,
        migration_ledger_count=13,
        migration_ledger_sha256=_d(31),
        install_manifest_sha256=INSTALL.manifest_sha256,
        covered_business_revision=3,
        started_at=T_INSTALL,
        completed_at=T_AUDIT,
        page_integrity="ok",
        foreign_key_violation_count=0,
        namespace_violation_count=0,
        audit_head_state_revision=1,
        audit_head_sequence=1,
        audit_head_certificate_id=AUDIT_ID,
        audit_head_previous_tail_sha256=ZERO,
        audit_head_tail_sha256=_d(43),
        audit_head_updated_at=T_AUDIT,
    )


def _activation():
    audit_binding = _audit()
    return BusinessCutoverActivationEvidence(
        activation=ACTIVATION_POINTER,
        install=INSTALL,
        audit_certificate_id=AUDIT_ID,
        audit_certificate_sequence=1,
        audit=AUDIT_POINTER,
        audit_binding=audit_binding,
        audit_certificate_tail_sha256=_d(43),
        audit_completed_at=T_AUDIT,
        expected_migration_ledger_count=13,
        expected_migration_ledger_sha256=_d(31),
        actual_schema_version=13,
        actual_schema_fingerprint=SCHEMA_FINGERPRINT,
        final_admission_state="sqlite",
        final_admission_epoch=REPOSITORY_EPOCH,
        final_admission_revision=9,
        final_admission_cutover_id=CUTOVER_ID,
        final_admission_sha256=_d(13),
        before_runtime=_runtime("read_only", 1, T_INSTALL),
        after_runtime=_runtime("active", 2, T_ACTIVATION),
    )


def _mount():
    activation_binding = _activation()
    return BusinessCutoverMountEvidence(
        mount=MOUNT_POINTER,
        source_admission_sha256=_d(10),
        final_admission_sha256=_d(13),
        install=INSTALL,
        coverage=COVERAGE,
        activation=ACTIVATION_POINTER,
        activation_binding=activation_binding,
        audit_certificate_id=AUDIT_ID,
        audit_certificate_sequence=1,
        audit=AUDIT_POINTER,
        audit_certificate_tail_sha256=_d(43),
        audit_completed_at=T_AUDIT,
        activated_at=T_ACTIVATION,
        target_schema_version=13,
        target_schema_fingerprint=SCHEMA_FINGERPRINT,
    )


_STAGES = {
    BusinessCutoverPhaseBindingStage.ASSET_PROVIDER: (
        1, "installed", "read_only", T_INSTALL,
        2, "assets_covered", "read_only", T_INSTALL, _provider,
    ),
    BusinessCutoverPhaseBindingStage.AUDIT: (
        2, "assets_covered", "read_only", T_INSTALL,
        3, "audited", "read_only", T_AUDIT, _audit,
    ),
    BusinessCutoverPhaseBindingStage.ACTIVATION: (
        3, "audited", "read_only", T_AUDIT,
        4, "activated", "active", T_ACTIVATION, _activation,
    ),
    BusinessCutoverPhaseBindingStage.MOUNT: (
        4, "activated", "active", T_ACTIVATION,
        5, "mounted", "active", T_MOUNT, _mount,
    ),
}


_HEAD_CHAIN = issue_business_cutover_head_chain(BusinessCutoverHeadInput(
    cutover_id=CUTOVER_ID,
    repository_epoch=REPOSITORY_EPOCH,
    runtime_generation="runtime-v13",
    code_digest=_d(12),
    install=BusinessCutoverHeadPointer(
        BusinessCutoverHeadPointerRole.INSTALL,
        INSTALL.transaction_id,
        INSTALL.receipt_kind,
        INSTALL.committed_at,
        INSTALL.manifest_sha256,
    ),
    coverage=BusinessCutoverHeadPointer(
        BusinessCutoverHeadPointerRole.COVERAGE,
        COVERAGE_POINTER.transaction_id,
        COVERAGE_POINTER.receipt_kind,
        COVERAGE_POINTER.committed_at,
        COVERAGE_POINTER.manifest_sha256,
    ),
    audit=BusinessCutoverHeadPointer(
        BusinessCutoverHeadPointerRole.AUDIT,
        AUDIT_POINTER.transaction_id,
        AUDIT_POINTER.receipt_kind,
        AUDIT_POINTER.committed_at,
        AUDIT_POINTER.manifest_sha256,
    ),
    activation=BusinessCutoverHeadPointer(
        BusinessCutoverHeadPointerRole.ACTIVATION,
        ACTIVATION_POINTER.transaction_id,
        ACTIVATION_POINTER.receipt_kind,
        ACTIVATION_POINTER.committed_at,
        ACTIVATION_POINTER.manifest_sha256,
    ),
    mount=BusinessCutoverHeadPointer(
        BusinessCutoverHeadPointerRole.MOUNT,
        MOUNT_POINTER.transaction_id,
        MOUNT_POINTER.receipt_kind,
        MOUNT_POINTER.committed_at,
        MOUNT_POINTER.manifest_sha256,
    ),
))


def _projection(stage: BusinessCutoverPhaseBindingStage):
    (
        old_revision, old_phase, old_authority, old_at,
        new_revision, new_phase, new_authority, new_at, evidence,
    ) = _STAGES[stage]
    pointers = {
        "install": INSTALL,
        "coverage": COVERAGE_POINTER if old_revision >= 2 else None,
        "audit": AUDIT_POINTER if old_revision >= 3 else None,
        "activation": ACTIVATION_POINTER if old_revision >= 4 else None,
        "mount": MOUNT_POINTER if old_revision >= 5 else None,
    }
    old_state = _HEAD_CHAIN[old_revision - 1]
    old_head = BusinessCutoverPhaseBindingHead(
        state_revision=old_revision,
        phase_revision=old_revision,
        phase=old_phase,
        authority_state=old_authority,
        head_sha256=old_state.head_sha256,
        updated_at=old_at,
        previous_head_sha256=old_state.previous_head_sha256,
        **pointers,
    )
    pointers = {
        "install": INSTALL,
        "coverage": COVERAGE_POINTER if new_revision >= 2 else None,
        "audit": AUDIT_POINTER if new_revision >= 3 else None,
        "activation": ACTIVATION_POINTER if new_revision >= 4 else None,
        "mount": MOUNT_POINTER if new_revision >= 5 else None,
    }
    new_state = _HEAD_CHAIN[new_revision - 1]
    new_head = BusinessCutoverPhaseBindingHead(
        state_revision=new_revision,
        phase_revision=new_revision,
        phase=new_phase,
        authority_state=new_authority,
        head_sha256=new_state.head_sha256,
        updated_at=new_at,
        previous_head_sha256=new_state.previous_head_sha256,
        **pointers,
    )
    admission = BusinessCutoverAdmissionCheckpoint(
        source_revision=7,
        source_admission_sha256=_d(10),
        source_freeze_sha256=_d(11),
        file_authority_state=(
            "cutover_pending"
            if stage in (
                BusinessCutoverPhaseBindingStage.ASSET_PROVIDER,
                BusinessCutoverPhaseBindingStage.AUDIT,
            )
            else "sqlite"
        ),
        file_authority_epoch=REPOSITORY_EPOCH,
        file_revision=(
            8
            if stage in (
                BusinessCutoverPhaseBindingStage.ASSET_PROVIDER,
                BusinessCutoverPhaseBindingStage.AUDIT,
            )
            else 9
        ),
        file_cutover_id=CUTOVER_ID,
        file_admission_sha256=_d(13),
    )
    runtime = _runtime(
        old_authority,
        1 if old_authority == "read_only" else 2,
        T_ACTIVATION
        if stage is BusinessCutoverPhaseBindingStage.MOUNT
        else T_INSTALL,
    )
    receipt_before_count, receipt_before_sha256, receipt_after_sha256 = {
        BusinessCutoverPhaseBindingStage.ASSET_PROVIDER: (10, _d(14), _d(14)),
        BusinessCutoverPhaseBindingStage.AUDIT: (10, _d(14), _d(15)),
        BusinessCutoverPhaseBindingStage.ACTIVATION: (11, _d(15), _d(17)),
        BusinessCutoverPhaseBindingStage.MOUNT: (12, _d(17), _d(18)),
    }[stage]
    receipt_delta = 0 if stage is BusinessCutoverPhaseBindingStage.ASSET_PROVIDER else 1
    checkpoint = BusinessCutoverDurableCheckpoint(
        runtime=runtime,
        receipt_before_count=receipt_before_count,
        receipt_before_sha256=receipt_before_sha256,
        receipt_after_count=receipt_before_count + receipt_delta,
        receipt_after_sha256=receipt_after_sha256,
        worker=BusinessCutoverWorkerHighWater(1, 0, ZERO, T_INSTALL),
        allocation=BusinessCutoverAllocationHighWater(1, 0, ZERO, T_INSTALL),
        dispatch=BusinessCutoverDispatchHighWater(
            1, 0, None, None, None, None, None, ZERO, T_INSTALL,
        ),
    )
    return BusinessCutoverPhaseBindingProjection(
        stage=stage,
        identity=BusinessCutoverPhaseBindingIdentity(
            CUTOVER_ID,
            REPOSITORY_EPOCH,
            "runtime-v13",
            _d(12),
        ),
        old_head=old_head,
        new_head=new_head,
        admission=admission,
        maintenance=BusinessCutoverMaintenanceCheckpoint(
            "catalog_ex_repository_ex",
            _d(16),
            T_OBSERVED,
        ),
        checkpoint=checkpoint,
        direct_evidence=evidence(),
    )


@pytest.mark.parametrize("stage", tuple(BusinessCutoverPhaseBindingStage))
def test_each_stage_has_distinct_canonical_domain_and_verifies(stage):
    binding = issue_business_cutover_phase_binding(_projection(stage))

    assert binding.binding_domain == BUSINESS_CUTOVER_PHASE_BINDING_DOMAINS[stage]
    assert verify_business_cutover_phase_binding(binding) is binding
    assert binding.manifest_sha256 != ZERO
    assert json.loads(binding.manifest_bytes)["stage"] == stage.value


def test_provider_receipt_is_canonical_self_describing_json():
    evidence = _provider()
    payload = json.loads(evidence.receipt_payload)

    assert evidence.receipt_payload == json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    assert payload["provider"] == {
        "blob_root_sha256": _d(42).hex(),
        "checked_at": T_INSTALL,
        "generation": "provider-generation-7",
        "object_count": 2,
        "protocol": "ze.business-cutover.asset-provider-checkpoint.v1",
        "provider_id": "provider-main",
        "sequence": 9,
        "total_bytes": 600,
    }
    assert payload["coverage"]["unique_blob_count"] == 2
    assert payload["coverage"]["unique_blob_bytes"] == 600


@pytest.mark.parametrize("stage", tuple(BusinessCutoverPhaseBindingStage))
def test_old_new_and_common_checkpoint_changes_are_not_hidden(stage):
    original = _projection(stage)
    expected = issue_business_cutover_phase_binding(original)

    changed_old = replace(original.old_head, head_sha256=_d(91))
    with pytest.raises(BusinessCutoverPhaseBindingContractError):
        replace(
            original,
            old_head=changed_old,
            new_head=replace(
                original.new_head,
                previous_head_sha256=changed_old.head_sha256,
            ),
        )

    with pytest.raises(BusinessCutoverPhaseBindingContractError):
        replace(
            original,
            new_head=replace(original.new_head, head_sha256=_d(92)),
        )

    changed_checkpoint = replace(
        original.checkpoint,
        worker=replace(
            original.checkpoint.worker,
            updated_at=T_COVERAGE,
        ),
    )
    changed = replace(original, checkpoint=changed_checkpoint)
    assert issue_business_cutover_phase_binding(changed).manifest_sha256 != expected.manifest_sha256


def test_each_direct_evidence_family_is_inside_the_manifest():
    provider = _projection(BusinessCutoverPhaseBindingStage.ASSET_PROVIDER)
    with pytest.raises(BusinessCutoverPhaseBindingIntegrityError):
        issue_business_cutover_phase_binding(replace(
            provider,
            direct_evidence=replace(provider.direct_evidence, provider_id="provider-forged"),
        ))

    audit = _projection(BusinessCutoverPhaseBindingStage.AUDIT)
    changed = replace(audit, direct_evidence=replace(audit.direct_evidence, findings_sha256=_d(94)))
    assert issue_business_cutover_phase_binding(changed).manifest_sha256 != issue_business_cutover_phase_binding(audit).manifest_sha256

    activation = _projection(BusinessCutoverPhaseBindingStage.ACTIVATION)
    with pytest.raises(BusinessCutoverPhaseBindingContractError):
        replace(
            activation.direct_evidence,
            actual_schema_fingerprint="9" * 64,
        )

    mount = _projection(BusinessCutoverPhaseBindingStage.MOUNT)
    with pytest.raises(BusinessCutoverPhaseBindingContractError):
        replace(
            mount.direct_evidence,
            target_schema_fingerprint="9" * 64,
        )


def test_stage_substitution_is_rejected_before_issue():
    asset = _projection(BusinessCutoverPhaseBindingStage.ASSET_PROVIDER)
    audit = _projection(BusinessCutoverPhaseBindingStage.AUDIT)

    with pytest.raises(BusinessCutoverPhaseBindingContractError) as rejected:
        replace(asset, direct_evidence=audit.direct_evidence)

    assert rejected.value.code == "business_cutover_phase_binding_evidence_invalid"


@pytest.mark.parametrize(
    ("stage", "pointer_name", "evidence_pointer"),
    (
        (
            BusinessCutoverPhaseBindingStage.ASSET_PROVIDER,
            "coverage",
            lambda projection: projection.direct_evidence.coverage.pointer,
        ),
        (
            BusinessCutoverPhaseBindingStage.AUDIT,
            "audit",
            lambda projection: projection.direct_evidence.receipt_parent,
        ),
        (
            BusinessCutoverPhaseBindingStage.ACTIVATION,
            "activation",
            lambda projection: projection.direct_evidence.activation,
        ),
        (
            BusinessCutoverPhaseBindingStage.MOUNT,
            "mount",
            lambda projection: projection.direct_evidence.mount,
        ),
    ),
)
def test_direct_evidence_cannot_fork_from_successor_head(
    stage,
    pointer_name,
    evidence_pointer,
):
    projection = _projection(stage)
    forged = replace(evidence_pointer(projection), manifest_sha256=_d(98))

    with pytest.raises(BusinessCutoverPhaseBindingContractError) as rejected:
        replace(
            projection,
            new_head=replace(projection.new_head, **{pointer_name: forged}),
        )

    assert rejected.value.code == "business_cutover_phase_binding_transition_invalid"


def test_cumulative_head_pointer_cannot_change_during_later_stage():
    projection = _projection(BusinessCutoverPhaseBindingStage.MOUNT)
    forged_coverage = replace(COVERAGE_POINTER, manifest_sha256=_d(98))

    with pytest.raises(BusinessCutoverPhaseBindingContractError) as rejected:
        replace(
            projection,
            new_head=replace(projection.new_head, coverage=forged_coverage),
        )

    assert rejected.value.code == "business_cutover_phase_binding_transition_invalid"


def test_head_requires_previous_digest_and_exact_cumulative_pointer_shape():
    projection = _projection(BusinessCutoverPhaseBindingStage.ASSET_PROVIDER)

    with pytest.raises(BusinessCutoverPhaseBindingContractError):
        replace(projection.old_head, previous_head_sha256=_d(98))
    with pytest.raises(BusinessCutoverPhaseBindingContractError):
        replace(projection.old_head, coverage=COVERAGE_POINTER)
    with pytest.raises(BusinessCutoverPhaseBindingContractError):
        replace(projection.new_head, coverage=None)


@pytest.mark.parametrize(
    "value",
    (
        lambda at: BusinessCutoverWorkerHighWater(2, 0, ZERO, at),
        lambda at: BusinessCutoverWorkerHighWater(1, 1, _d(98), at),
        lambda at: BusinessCutoverAllocationHighWater(2, 0, ZERO, at),
        lambda at: BusinessCutoverAllocationHighWater(1, 1, _d(98), at),
        lambda at: BusinessCutoverDispatchHighWater(
            2, 0, None, None, None, None, None, ZERO, at,
        ),
        lambda at: BusinessCutoverDispatchHighWater(
            1, 1, 3, 0, None, None, None, _d(98), at,
        ),
        lambda at: BusinessCutoverDispatchHighWater(
            1, 0, None, None, 1, 3, 0, ZERO, at,
        ),
    ),
)
def test_scheduler_checkpoints_are_pristine_only_like_sql(value):
    with pytest.raises(BusinessCutoverPhaseBindingContractError):
        value(T_INSTALL)


def test_runtime_authority_state_revision_matches_sql_control_row():
    with pytest.raises(BusinessCutoverPhaseBindingContractError):
        _runtime("read_only", 2, T_INSTALL)
    with pytest.raises(BusinessCutoverPhaseBindingContractError):
        _runtime("active", 1, T_INSTALL)


def test_activation_and_mount_reject_predecessor_evidence_forks():
    activation = _activation()
    with pytest.raises(BusinessCutoverPhaseBindingContractError):
        replace(activation, audit_certificate_tail_sha256=_d(99))
    with pytest.raises(BusinessCutoverPhaseBindingContractError):
        replace(activation, expected_migration_ledger_sha256=_d(99))

    mount = _mount()
    with pytest.raises(BusinessCutoverPhaseBindingContractError):
        replace(mount, audit_certificate_sequence=2)
    with pytest.raises(BusinessCutoverPhaseBindingContractError):
        replace(mount, target_schema_fingerprint="9" * 64)
    with pytest.raises(BusinessCutoverPhaseBindingContractError):
        replace(mount, install=replace(INSTALL, manifest_sha256=_d(99)))
    with pytest.raises(BusinessCutoverPhaseBindingContractError):
        replace(mount, final_admission_sha256=_d(99))


def test_binding_chain_rejects_common_checkpoint_forks():
    projections = tuple(_projection(stage) for stage in BusinessCutoverPhaseBindingStage)
    bindings = tuple(issue_business_cutover_phase_binding(value) for value in projections)
    assert _verify_business_cutover_phase_binding_chain(bindings) == bindings

    audit = projections[1]
    forged_audit = replace(
        audit,
        admission=replace(audit.admission, source_freeze_sha256=_d(90)),
    )
    activation = projections[2]
    forged_activation = replace(
        activation,
        checkpoint=replace(
            activation.checkpoint,
            receipt_before_count=20,
            receipt_before_sha256=_d(91),
            receipt_after_count=21,
            receipt_after_sha256=_d(92),
        ),
    )
    mount = projections[3]
    forged_mount = replace(
        mount,
        checkpoint=replace(
            mount.checkpoint,
            worker=replace(mount.checkpoint.worker, updated_at=T_COVERAGE),
        ),
    )
    for index, forged in ((1, forged_audit), (2, forged_activation), (3, forged_mount)):
        changed = list(bindings)
        changed[index] = issue_business_cutover_phase_binding(forged)
        with pytest.raises(BusinessCutoverPhaseBindingIntegrityError) as rejected:
            _verify_business_cutover_phase_binding_chain(tuple(changed))
        assert rejected.value.code == "business_cutover_phase_binding_chain_invalid"


def test_audit_and_activation_are_exact_v13_like_sql():
    with pytest.raises(BusinessCutoverPhaseBindingContractError):
        replace(_audit(), schema_version=12, migration_ledger_count=12)
    with pytest.raises(BusinessCutoverPhaseBindingContractError):
        replace(
            _activation(),
            expected_migration_ledger_count=12,
            actual_schema_version=12,
        )


def test_audit_cannot_start_before_its_runtime_checkpoint():
    projection = _projection(BusinessCutoverPhaseBindingStage.AUDIT)
    with pytest.raises(BusinessCutoverPhaseBindingContractError):
        replace(
            projection,
            direct_evidence=replace(
                projection.direct_evidence,
                started_at=T_COVERAGE,
            ),
        )


def test_projection_rejects_stale_or_future_checkpoint_times():
    projection = _projection(BusinessCutoverPhaseBindingStage.AUDIT)

    with pytest.raises(BusinessCutoverPhaseBindingContractError):
        replace(
            projection,
            maintenance=replace(
                projection.maintenance,
                observed_at=T_COVERAGE,
            ),
        )
    with pytest.raises(BusinessCutoverPhaseBindingContractError):
        replace(
            projection,
            checkpoint=replace(
                projection.checkpoint,
                worker=replace(
                    projection.checkpoint.worker,
                    updated_at=T_OBSERVED,
                ),
            ),
        )


def test_provider_checkpoint_must_not_predate_predecessor_head():
    projection = _projection(BusinessCutoverPhaseBindingStage.ASSET_PROVIDER)
    stale_provider = issue_business_cutover_asset_provider_evidence(
        coverage=COVERAGE,
        provider_protocol=projection.direct_evidence.provider_protocol,
        provider_id=projection.direct_evidence.provider_id,
        provider_generation=projection.direct_evidence.provider_generation,
        provider_sequence=projection.direct_evidence.provider_sequence,
        checked_at=T_COVERAGE,
        object_count=projection.direct_evidence.object_count,
        total_bytes=projection.direct_evidence.total_bytes,
        blob_root_sha256=projection.direct_evidence.blob_root_sha256,
    )

    with pytest.raises(BusinessCutoverPhaseBindingContractError):
        replace(projection, direct_evidence=stale_provider)


def test_activation_checkpoint_is_the_predecessor_runtime_image():
    projection = _projection(BusinessCutoverPhaseBindingStage.ACTIVATION)

    with pytest.raises(BusinessCutoverPhaseBindingContractError):
        replace(
            projection,
            checkpoint=replace(
                projection.checkpoint,
                runtime=replace(
                    projection.checkpoint.runtime,
                    authority_state="active",
                ),
            ),
        )


def test_post_construction_projection_mutation_and_manifest_tampering_fail():
    binding = issue_business_cutover_phase_binding(
        _projection(BusinessCutoverPhaseBindingStage.MOUNT)
    )
    object.__setattr__(binding.projection.old_head, "head_sha256", _d(97))

    with pytest.raises(BusinessCutoverPhaseBindingIntegrityError):
        verify_business_cutover_phase_binding(binding)

    clean = issue_business_cutover_phase_binding(
        _projection(BusinessCutoverPhaseBindingStage.MOUNT)
    )
    object.__setattr__(clean, "manifest_bytes", clean.manifest_bytes + b" ")
    with pytest.raises(BusinessCutoverPhaseBindingIntegrityError) as rejected:
        verify_business_cutover_phase_binding(clean)
    assert rejected.value.code == "business_cutover_phase_binding_manifest_mismatch"


def _projection_objects(value):
    if not dataclasses.is_dataclass(value):
        return
    yield value
    for item in dataclasses.fields(value):
        if item.name.startswith("_"):
            continue
        child = getattr(value, item.name)
        if dataclasses.is_dataclass(child):
            yield from _projection_objects(child)


def _tampered_value(value):
    if type(value) is bytes:
        return bytes([value[0] ^ 1]) + value[1:]
    if type(value) is str:
        return value + "x"
    if type(value) is int:
        return value + 1
    if isinstance(value, Enum):
        values = tuple(type(value))
        return values[(values.index(value) + 1) % len(values)]
    if value is None:
        return "forged"
    if dataclasses.is_dataclass(value):
        return None
    raise AssertionError("unhandled field type: %r" % (type(value),))


@pytest.mark.parametrize("stage", tuple(BusinessCutoverPhaseBindingStage))
def test_every_declared_projection_field_is_revalidated_after_mutation(stage):
    binding = issue_business_cutover_phase_binding(_projection(stage))
    checked = 0

    for owner in tuple(_projection_objects(binding.projection)):
        for item in dataclasses.fields(owner):
            if item.name.startswith("_"):
                continue
            original = getattr(owner, item.name)
            object.__setattr__(owner, item.name, _tampered_value(original))
            try:
                with pytest.raises(BusinessCutoverPhaseBindingIntegrityError):
                    verify_business_cutover_phase_binding(binding)
            finally:
                object.__setattr__(owner, item.name, original)
            checked += 1

    assert checked >= 75


@pytest.mark.parametrize("stage", tuple(BusinessCutoverPhaseBindingStage))
def test_fixed_vector(stage):
    binding = issue_business_cutover_phase_binding(_projection(stage))
    expected = {
        # Frozen after first canonical implementation; any field/order/domain
        # drift must be an intentional protocol version change.
        BusinessCutoverPhaseBindingStage.ASSET_PROVIDER: (
            "bee47a69da97810e7c1adba937086c34dd6ba7d968975685474f25465026c204"
        ),
        BusinessCutoverPhaseBindingStage.AUDIT: (
            "1c02851ec2f8c1ec4846a04aa252a71c010dddae73f00280c9747c824458b344"
        ),
            BusinessCutoverPhaseBindingStage.ACTIVATION: (
                "6670cba49882af2112ca717006786e09742546e746ccf1e3ec6cd8f4e1001865"
            ),
            BusinessCutoverPhaseBindingStage.MOUNT: (
                "8757b3d02e1a530731b6f9eff1fd77345f390942a7de621f5e46e7d2a2c7af3b"
            ),
    }
    assert binding.manifest_sha256.hex() == expected[stage]


@pytest.mark.parametrize(
    ("stage", "table", "column_count", "stage_pointer_column"),
    (
        (
            BusinessCutoverPhaseBindingStage.ASSET_PROVIDER,
            "business_cutover_asset_provider_bindings",
            91,
            "coverage_transaction_id",
        ),
        (
            BusinessCutoverPhaseBindingStage.AUDIT,
            "business_cutover_audit_bindings",
            87,
            "audit_transaction_id",
        ),
        (
            BusinessCutoverPhaseBindingStage.ACTIVATION,
            "business_cutover_activation_bindings",
            89,
            "activation_transaction_id",
        ),
        (
            BusinessCutoverPhaseBindingStage.MOUNT,
            "business_cutover_mount_bindings",
            99,
            "mount_transaction_id",
        ),
    ),
)
def test_v13_persisted_row_is_the_exact_stage_layout(
    stage,
    table,
    column_count,
    stage_pointer_column,
):
    binding = issue_business_cutover_phase_binding(_projection(stage))
    persisted = _project_business_cutover_phase_binding_row(binding)
    layout = _business_cutover_phase_binding_layout(stage)
    values = persisted.as_mapping()

    assert persisted.stage is stage
    assert persisted.table == layout.table == table
    assert persisted.columns == layout.columns
    assert len(persisted.columns) == len(persisted.values) == column_count
    assert tuple(values) == persisted.columns
    assert values["cutover_id"] == CUTOVER_ID
    assert values["binding_manifest_bytes"] == binding.manifest_bytes
    assert values["binding_manifest_sha256"] == binding.manifest_sha256
    assert values[stage_pointer_column] == {
        BusinessCutoverPhaseBindingStage.ASSET_PROVIDER:
            COVERAGE_POINTER.transaction_id,
        BusinessCutoverPhaseBindingStage.AUDIT: AUDIT_POINTER.transaction_id,
        BusinessCutoverPhaseBindingStage.ACTIVATION:
            ACTIVATION_POINTER.transaction_id,
        BusinessCutoverPhaseBindingStage.MOUNT: MOUNT_POINTER.transaction_id,
    }[stage]


def test_v13_persisted_row_rejects_a_changed_issued_binding():
    binding = issue_business_cutover_phase_binding(
        _projection(BusinessCutoverPhaseBindingStage.MOUNT)
    )
    changed = replace(binding, manifest_bytes=b"changed")

    with pytest.raises(
        BusinessCutoverPhaseBindingPersistabilityError,
        match="business_cutover_phase_binding_persistability_binding_invalid",
    ):
        _project_business_cutover_phase_binding_row(changed)
