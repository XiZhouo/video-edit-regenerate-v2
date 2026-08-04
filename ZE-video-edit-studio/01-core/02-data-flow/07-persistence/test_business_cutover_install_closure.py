"""Fixed and adversarial vectors for the pure V12 install closure."""

from __future__ import annotations

import sys
from dataclasses import replace
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

import business_cutover_install_closure as closure_module
import test_business_cutover_asset_integrity as asset_support
from asset_reference_projection import project_asset_references
from business_codec import decode_payload_bounded
from business_cutover_asset_integrity import (
    _issue_business_cutover_asset_coverage_plan,
    _issue_business_cutover_asset_stage_plan,
)
from business_cutover_baseline_projection import (
    _project_business_cutover_baseline,
)
from business_cutover_install_closure import (
    ATOMIC_BUSINESS_WRITE_RECEIPT_COLUMNS,
    BASELINE_ATOMIC_RECEIPT_COLUMNS,
    INSTALL_ARTIFACT_CHUNK_COLUMNS,
    INSTALL_ARTIFACT_COLUMNS,
    INSTALL_CLOSURE_COLUMNS,
    BUSINESS_CUTOVER_BASELINE_PROJECTION_ARTIFACT,
    BUSINESS_CUTOVER_MEMBER_MAPPING_ARTIFACT,
    BusinessCutoverInstallClosureError,
    _project_business_cutover_install_closure,
    _require_business_cutover_install_closure,
)
from test_business_cutover_baseline_projection import _snapshot
from test_business_cutover_integrity import _deleted, _live, _plan
from production_atomic_write_integrity import (
    _groups as _production_target_groups,
    _production_targets_sha256,
)


COMMITTED_AT = "2026-07-12T01:00:00.000Z"


def _v12_plan(*, members=None, snapshots=None, target_version=12):
    if members is None and snapshots is None:
        snapshot = _snapshot()
        members = (_live(snapshot),)
        snapshots = (snapshot,)
    return _plan(
        members=members,
        snapshots=snapshots,
        expected_target_schema_version=target_version,
    )


def _coverage_for(plan):
    selected_snapshots = plan._business_snapshots
    stage_observations = ()
    referenced_shas = {
        occurrence.reference.sha256
        for snapshot in selected_snapshots
        for occurrence in project_asset_references(snapshot).occurrences
    }
    carriers = tuple(
        asset_support._carrier(asset_support.BLOB_A)
        for sha256 in sorted(referenced_shas)
        if sha256 == asset_support.SHA_A
    )
    if referenced_shas - {asset_support.SHA_A}:
        raise AssertionError("closure test fixture has no exact blob carrier")
    if carriers:
        stage = _issue_business_cutover_asset_stage_plan(
            cutover_plan=plan,
            business_snapshots=selected_snapshots,
            blob_carriers=carriers,
        )
        stage_observations = asset_support._exact_stage_observations(
            plan,
            (stage,),
        )
    return _issue_business_cutover_asset_coverage_plan(
        cutover_plan=plan,
        business_snapshots=selected_snapshots,
        stage_observations=stage_observations,
    )


def _projection(*, members=None, snapshots=None):
    plan = _v12_plan(members=members, snapshots=snapshots)
    coverage = _coverage_for(plan)
    baseline = _project_business_cutover_baseline(
        plan,
        committed_at=COMMITTED_AT,
    )
    return (
        plan,
        baseline,
        _project_business_cutover_install_closure(
            plan,
            baseline,
            coverage_plan=coverage,
        ),
    )


def test_mixed_install_closure_is_column_exact_and_revalidates() -> None:
    snapshot = _snapshot()
    plan, baseline, projected = _projection(
        members=(_live(snapshot), _deleted()),
        snapshots=(snapshot,),
    )

    assert _require_business_cutover_install_closure(projected) is projected
    assert projected.cutover_id == plan.cutover_id == baseline.cutover_id
    assert projected.member_mapping.header.artifact_kind == (
        BUSINESS_CUTOVER_MEMBER_MAPPING_ARTIFACT
    )
    assert projected.baseline_projection.header.artifact_kind == (
        BUSINESS_CUTOVER_BASELINE_PROJECTION_ARTIFACT
    )
    assert len(projected.member_mapping.header.sql_values()) == len(
        INSTALL_ARTIFACT_COLUMNS
    )
    assert all(
        len(value.sql_values()) == len(INSTALL_ARTIFACT_CHUNK_COLUMNS)
        for artifact in (
            projected.member_mapping,
            projected.baseline_projection,
        )
        for value in artifact.chunks
    )
    assert len(projected.baseline_receipts) == 1
    assert len(projected.baseline_receipts[0].sql_values()) == len(
        BASELINE_ATOMIC_RECEIPT_COLUMNS
    )
    assert len(projected.closure.sql_values()) == len(INSTALL_CLOSURE_COLUMNS)
    assert projected.closure.project_count == 2
    assert projected.closure.live_project_count == 1
    assert projected.closure.deleted_project_count == 1
    assert projected.closure.baseline_commit_count == 1
    assert projected.closure.install_transaction_id == plan.install.transaction_id
    assert projected.closure.install_manifest_sha256 == (plan.install.manifest_sha256)
    assert projected.closure.source_transaction_id == (plan.legacy_proof.transaction_id)
    assert projected.closure.source_proof_manifest_sha256 == (
        plan.legacy_proof.manifest_sha256
    )
    assert projected.closure.source_proof_sha256 == plan.legacy_proof.proof_sha256
    assert projected.closure.source_admission_sha256 == (
        plan.closure.source_admission_sha256
    )
    assert projected.closure.coverage_transaction_id == (
        projected._coverage_plan.transaction_id
    )
    assert projected.closure.coverage_manifest_sha256 == (
        projected._coverage_plan.manifest_sha256
    )
    assert projected.closure.coverage_sha256 == (
        projected._coverage_plan.coverage.coverage_sha256
    )


def test_v12_install_closure_hash_domains_have_fixed_cross_layer_vectors() -> None:
    _plan_value, _baseline, projected = _projection()
    receipt = projected.baseline_receipts[0]

    assert projected.member_mapping.header.artifact_sha256.hex() == (
        "f95a68a86abdc9cdde79fec8225e57a3b2daf1a1654c7387b40e0f3842fc5a09"
    )
    assert projected.member_mapping.chunks[0].chunk_sha256.hex() == (
        "4d5162e767107e18f24bf7bf9c1434b3722f7f59c9012504e26158ca7a2c7d5a"
    )
    assert projected.baseline_projection.header.artifact_sha256.hex() == (
        "019cdb45e8701a01b41e5aada99c8a6ca357bc41a9d35f3d8c18de1402536534"
    )
    assert projected.baseline_projection.chunks[0].chunk_sha256.hex() == (
        "804bac4ffe97b1b6d9b0da00b1181e7742bb2e8f4f905b0079779c3b8045f452"
    )
    assert receipt.targets_sha256.hex() == (
        "edc9bee4937fa3cc223a52ca677f0e6825dc6ef503000bfabd4fc24941ca153f"
    )
    assert receipt.baseline_outer_manifest_sha256.hex() == (
        "9b771d726b946175ef8adc5787e020e02ba26c9acf6eb32e8bac8c5ce9022e27"
    )
    assert receipt.baseline_outer_receipt_sha256.hex() == (
        "78b266060808f43818a192f6b57fc9d40c11ca5d36a3bf9bd19ab78f0b99218a"
    )
    assert projected.closure.baseline_outer_receipt_root_sha256.hex() == (
        "c53062af336c952efcbcbe06892647a62b3853cca6bf41211fa0d5874bcf6660"
    )
    assert projected.closure.manifest_sha256.hex() == (
        "069ad27c87fccf02be5a3b6dd026cf4aaae34047b2044219294dc4db5f656374"
    )


def test_empty_repository_has_two_artifacts_and_a_nonzero_empty_receipt_root() -> None:
    _plan_value, _baseline, projected = _projection(
        members=(),
        snapshots=(),
    )

    assert projected.baseline_receipts == ()
    assert projected.closure.project_count == 0
    assert projected.closure.baseline_commit_count == 0
    assert projected.closure.baseline_outer_receipt_root_sha256 != b"\0" * 32
    assert projected.member_mapping.header.chunk_count == 1
    assert projected.baseline_projection.header.chunk_count == 1


def test_artifacts_reassemble_exactly_and_chunks_are_domain_separated() -> None:
    plan, baseline, projected = _projection()

    assert (
        b"".join(value.chunk_bytes for value in projected.member_mapping.chunks)
        == plan.closure.member_mapping_bytes
    )
    baseline_v2 = b"".join(
        value.chunk_bytes for value in projected.baseline_projection.chunks
    )
    assert baseline_v2 != baseline.manifest_bytes
    assert (
        b'"schema":"ze.business-cutover-baseline-projection.manifest.v2"' in baseline_v2
    )
    assert b'"outer_atomic_status":"baseline_atomic_sidecar_v1"' in baseline_v2
    assert baseline.manifest_sha256.hex().encode() in baseline_v2

    for artifact in (
        projected.member_mapping,
        projected.baseline_projection,
    ):
        assert tuple(value.ordinal for value in artifact.chunks) == tuple(
            range(artifact.header.chunk_count)
        )
        assert tuple(value.byte_offset for value in artifact.chunks) == tuple(
            range(
                0,
                artifact.header.byte_length,
                closure_module.MAX_BUSINESS_CUTOVER_ARTIFACT_CHUNK_BYTES,
            )
        )
        assert len({value.chunk_sha256 for value in artifact.chunks}) == len(
            artifact.chunks
        )


def test_chunk_partition_boundaries_are_deterministic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        closure_module,
        "MAX_BUSINESS_CUTOVER_ARTIFACT_BYTES",
        64,
    )
    monkeypatch.setattr(
        closure_module,
        "MAX_BUSINESS_CUTOVER_ARTIFACT_CHUNK_BYTES",
        16,
    )
    monkeypatch.setattr(
        closure_module,
        "MAX_BUSINESS_CUTOVER_ARTIFACT_CHUNKS",
        4,
    )
    artifact = closure_module._artifact(
        cutover_id="a" * 64,
        artifact_kind=BUSINESS_CUTOVER_MEMBER_MAPPING_ARTIFACT,
        artifact_schema="ze.business-cutover.member-mapping.v1",
        artifact_bytes=b"x" * 33,
        artifact_sha256=b"h" * 32,
    )
    assert artifact.header.chunk_count == 3
    assert tuple(len(value.chunk_bytes) for value in artifact.chunks) == (16, 16, 1)
    assert tuple(value.byte_offset for value in artifact.chunks) == (0, 16, 32)
    assert len(set(value.chunk_sha256 for value in artifact.chunks)) == 3

    with pytest.raises(
        BusinessCutoverInstallClosureError,
        match="business_cutover_install_artifact_invalid",
    ):
        closure_module._artifact(
            cutover_id="a" * 64,
            artifact_kind=BUSINESS_CUTOVER_MEMBER_MAPPING_ARTIFACT,
            artifact_schema="ze.business-cutover.member-mapping.v1",
            artifact_bytes=b"x" * 65,
            artifact_sha256=b"h" * 32,
        )


def test_v11_target_is_explicitly_rejected_by_v12_closure() -> None:
    snapshot = _snapshot()
    plan = _plan(
        members=(_live(snapshot),),
        snapshots=(snapshot,),
        expected_target_schema_version=11,
    )
    baseline = _project_business_cutover_baseline(
        plan,
        committed_at=COMMITTED_AT,
    )
    with pytest.raises(
        BusinessCutoverInstallClosureError,
        match="business_cutover_install_closure_schema_target_invalid",
    ):
        _project_business_cutover_install_closure(
            plan,
            baseline,
            coverage_plan=_coverage_for(plan),
        )


def test_closure_protocol_accepts_an_additive_future_schema_target() -> None:
    plan = _v12_plan(target_version=13)
    baseline = _project_business_cutover_baseline(
        plan,
        committed_at=COMMITTED_AT,
    )
    projected = _project_business_cutover_install_closure(
        plan,
        baseline,
        coverage_plan=_coverage_for(plan),
    )

    assert b'"version":13' in projected.closure.manifest_bytes
    assert _require_business_cutover_install_closure(projected) is projected


def test_v2_preserves_the_real_available_revision_one_asset_row_shape() -> None:
    snapshot = _snapshot(
        references=(
            asset_support._ref(
                asset_support.SHA_A,
                size=len(asset_support.BLOB_A),
            ),
        )
    )
    _plan_value, _baseline, projected = _projection(
        members=(_live(snapshot),),
        snapshots=(snapshot,),
    )
    manifest = b"".join(
        chunk.chunk_bytes for chunk in projected.baseline_projection.chunks
    )
    decoded = decode_payload_bounded(manifest, len(manifest), 1_000_000)
    blob = decoded["asset_blobs"][0]

    assert (blob["status"], blob["state_revision"]) == ("available", 1)
    assert blob["created_at"] == blob["updated_at"] == COMMITTED_AT
    asset_write = decoded["asset_blob_write"]
    assert asset_write["mode"] == "insert_available_v1_from_fresh_coverage"
    assert asset_write["created_at"] == COMMITTED_AT
    assert asset_write["count"] == 1
    assert asset_write["asset_coverage"] == {
        "transaction_id": projected._coverage_plan.transaction_id,
        "receipt_kind": projected._coverage_plan.expected_evidence.receipt_kind,
        "manifest_sha256": projected._coverage_plan.manifest_sha256.hex(),
        "coverage_sha256": (projected._coverage_plan.coverage.coverage_sha256.hex()),
        "project_root_sha256": (
            projected._coverage_plan.coverage.project_root_sha256.hex()
        ),
        "occurrence_root_sha256": (
            projected._coverage_plan.coverage.occurrence_root_sha256.hex()
        ),
        "blob_root_sha256": (projected._coverage_plan.coverage.blob_root_sha256.hex()),
    }
    assert (
        closure_module._reconstruct_baseline_projection_v1_from_v2(decoded)
        == _baseline.manifest_bytes
    )
    assert "asset_blob_transitions" not in decoded


def test_v2_fills_coverage_metadata_without_changing_the_v1_source_proof() -> None:
    snapshot = _snapshot(
        references=(
            asset_support._ref(
                asset_support.SHA_A,
                media_type=None,
                size=len(asset_support.BLOB_A),
                extension=None,
            ),
        )
    )
    _plan_value, baseline, projected = _projection(
        members=(_live(snapshot),),
        snapshots=(snapshot,),
    )
    assert baseline.asset_blobs[0].media_type is None
    assert baseline.asset_blobs[0].extension is None

    encoded = b"".join(
        chunk.chunk_bytes for chunk in projected.baseline_projection.chunks
    )
    decoded = decode_payload_bounded(encoded, len(encoded), 1_000_000)
    assert decoded["asset_blobs"][0]["media_type"] == "image/png"
    assert decoded["asset_blobs"][0]["extension"] == ".png"
    assert decoded["source_projection_v1"]["asset_blob_metadata"] == [
        {
            "asset_sha256": asset_support.SHA_A,
            "media_type": None,
            "extension": None,
            "byte_length": len(asset_support.BLOB_A),
        }
    ]
    assert (
        closure_module._reconstruct_baseline_projection_v1_from_v2(decoded)
        == baseline.manifest_bytes
    )

    changed = dict(decoded)
    changed_source = dict(decoded["source_projection_v1"])
    changed_metadata = [dict(changed_source["asset_blob_metadata"][0])]
    changed_metadata[0]["media_type"] = "image/png"
    changed_source["asset_blob_metadata"] = changed_metadata
    changed["source_projection_v1"] = changed_source
    with pytest.raises(
        BusinessCutoverInstallClosureError,
        match="business_cutover_baseline_projection_v2_source_proof_invalid",
    ):
        closure_module._reconstruct_baseline_projection_v1_from_v2(changed)


def test_equivalent_but_different_plan_cannot_rebind_projection() -> None:
    first = _v12_plan()
    second = _v12_plan()
    baseline = _project_business_cutover_baseline(
        first,
        committed_at=COMMITTED_AT,
    )
    with pytest.raises(
        BusinessCutoverInstallClosureError,
        match="business_cutover_install_closure_plan_mismatch",
    ):
        _project_business_cutover_install_closure(
            second,
            baseline,
            coverage_plan=_coverage_for(second),
        )


def test_equivalent_but_different_coverage_plan_cannot_rebind_projection() -> None:
    first = _v12_plan()
    second = _v12_plan()
    baseline = _project_business_cutover_baseline(
        first,
        committed_at=COMMITTED_AT,
    )
    with pytest.raises(
        BusinessCutoverInstallClosureError,
        match="business_cutover_install_closure_plan_mismatch",
    ):
        _project_business_cutover_install_closure(
            first,
            baseline,
            coverage_plan=_coverage_for(second),
        )


@pytest.mark.parametrize(
    ("carrier", "error_code"),
    (
        ("projects", "business_cutover_install_closure_coverage_project_mismatch"),
        (
            "occurrences",
            "business_cutover_install_closure_coverage_occurrence_mismatch",
        ),
        ("blobs", "business_cutover_install_closure_coverage_project_mismatch"),
    ),
)
def test_project_occurrence_and_blob_coverage_are_cross_checked(
    carrier: str,
    error_code: str,
) -> None:
    snapshot = _snapshot(
        references=(
            asset_support._ref(
                asset_support.SHA_A,
                size=len(asset_support.BLOB_A),
            ),
        )
    )
    plan, baseline, projected = _projection(
        members=(_live(snapshot),),
        snapshots=(snapshot,),
    )
    coverage = projected._coverage_plan
    rows = getattr(coverage, carrier)
    if carrier == "projects":
        changed = replace(rows[0], document_id="wrong-document")
    elif carrier == "occurrences":
        changed = replace(rows[0], value_path="$.wrong")
    else:
        changed = replace(rows[0], byte_length=rows[0].byte_length + 1)
    object.__setattr__(coverage, carrier, (changed,))

    with pytest.raises(BusinessCutoverInstallClosureError, match=error_code):
        closure_module._validate_asset_coverage_binding(
            plan=plan,
            baseline=baseline,
            coverage_plan=coverage,
        )


def test_baseline_sidecar_is_not_a_post_mount_production_receipt() -> None:
    plan, baseline, projected = _projection()
    receipt = projected.baseline_receipts[0]
    atomic = projected.atomic_receipts[0]
    live = baseline.live[0]

    assert receipt.commit_id == live.commit.commit_id
    assert receipt.transaction_id == live.commit.transaction_id
    assert receipt.committed_at == baseline.committed_at
    assert receipt.install_transaction_id == plan.install.transaction_id
    assert receipt.atomic_manifest_sha256 == live.commit.manifest_sha256
    assert receipt.control_count == 0
    assert receipt.before_worker_sequence == receipt.after_worker_sequence == 0
    assert receipt.before_worker_tail_sha256 == b"\0" * 32
    assert receipt.after_worker_tail_sha256 == b"\0" * 32
    assert len(atomic.sql_values()) == len(ATOMIC_BUSINESS_WRITE_RECEIPT_COLUMNS)
    assert receipt.baseline_outer_receipt_sha256 not in (
        receipt.receipt_sha256,
        b"\0" * 32,
    )
    assert atomic.production_manifest_bytes == receipt.baseline_outer_manifest_bytes
    assert atomic.production_manifest_sha256 == (receipt.baseline_outer_manifest_sha256)
    assert atomic.production_receipt_sha256 == (receipt.baseline_outer_receipt_sha256)
    assert atomic.target_count == receipt.target_count == 0
    assert atomic.targets_sha256 == receipt.targets_sha256
    assert atomic.targets_sha256 == _production_targets_sha256(
        _production_target_groups(())
    )


def test_any_projected_row_tamper_breaks_private_revalidation() -> None:
    _plan_value, _baseline, projected = _projection()
    changed_receipt = replace(
        projected.baseline_receipts[0],
        baseline_outer_receipt_sha256=b"t" * 32,
    )
    changed_closure = replace(
        projected,
        baseline_receipts=(changed_receipt,),
    )
    with pytest.raises(
        BusinessCutoverInstallClosureError,
        match="business_cutover_install_closure_changed",
    ):
        _require_business_cutover_install_closure(changed_closure)

    for section, field_name, changed_value in (
        ("source_proof", "catalog_id", "0" * 64),
        ("asset_coverage", "repository_epoch", "0" * 64),
    ):
        decoded = decode_payload_bounded(
            projected.closure.manifest_bytes,
            len(projected.closure.manifest_bytes),
            100_000,
        )
        changed_section = dict(decoded[section])
        changed_section[field_name] = changed_value
        decoded[section] = changed_section
        changed_manifest = closure_module.encode_payload_bounded(
            decoded,
            closure_module.MAX_BUSINESS_SMALL_PAYLOAD_BYTES,
            100_000,
        )
        changed_row = replace(
            projected.closure,
            manifest_bytes=changed_manifest,
            manifest_sha256=closure_module._closure_manifest_digest(changed_manifest),
        )
        with pytest.raises(
            BusinessCutoverInstallClosureError,
            match="business_cutover_install_closure_changed",
        ):
            _require_business_cutover_install_closure(
                replace(projected, closure=changed_row)
            )


def test_projection_manifest_binds_artifacts_counts_and_receipt_root() -> None:
    _plan_value, _baseline, projected = _projection()
    manifest = projected.closure.manifest_bytes

    assert projected.closure.manifest_sha256 != b"\0" * 32
    assert (
        projected.closure.baseline_outer_receipt_root_sha256.hex().encode() in manifest
    )
    assert projected.member_mapping.header.artifact_sha256.hex().encode() in manifest
    assert (
        projected.baseline_projection.header.artifact_sha256.hex().encode() in manifest
    )
    assert b'"baseline_receipts":{"count":1' in manifest

    decoded = decode_payload_bounded(manifest, len(manifest), 100_000)
    source = decoded["source_proof"]
    coverage = decoded["asset_coverage"]
    assert source["transaction_id"] == projected.closure.source_transaction_id
    assert source["catalog_id"] == _plan_value.legacy_proof.catalog_id
    assert source["catalog_seal_sha256"] == (
        _plan_value.legacy_proof.catalog_seal_sha256.hex()
    )
    assert source["schema_fingerprint"] == (_plan_value.legacy_proof.schema_fingerprint)
    assert source["manifest_sha256"] == (
        projected.closure.source_proof_manifest_sha256.hex()
    )
    assert source["proof_sha256"] == projected.closure.source_proof_sha256.hex()
    assert source["source_admission_sha256"] == (
        projected.closure.source_admission_sha256.hex()
    )
    assert coverage["transaction_id"] == (projected.closure.coverage_transaction_id)
    assert coverage["repository_epoch"] == (projected._coverage_plan.repository_epoch)
    assert coverage["source_admission_revision"] == (
        projected._coverage_plan.source_admission_revision
    )
    assert coverage["source_admission_sha256"] == (
        projected._coverage_plan.source_admission_sha256.hex()
    )
    assert coverage["manifest_sha256"] == (
        projected.closure.coverage_manifest_sha256.hex()
    )
    assert coverage["coverage_sha256"] == (projected.closure.coverage_sha256.hex())
    assert coverage["member_root_sha256"] == source["member_root_sha256"]


def test_module_exports_no_public_capability() -> None:
    assert closure_module.__all__ == ()
