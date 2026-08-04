"""Adversarial tests for bounded asset batches and repository coverage."""

from __future__ import annotations

import hashlib
import inspect
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

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

from business_cutover_asset_contract import (
    BusinessCutoverAssetBlobCarrier,
    BusinessCutoverAssetContractError,
    BusinessCutoverAssetCoverageReconcileStatus,
    BusinessCutoverAssetCoverageRetryAction,
    BusinessCutoverAssetStageLimits,
    BusinessCutoverAssetStageReconcileStatus,
    BusinessCutoverAssetStageRetryAction,
)
from business_cutover_asset_integrity import (
    BusinessCutoverAssetCoveragePlan,
    BusinessCutoverAssetIntegrityError,
    BusinessCutoverAssetStageMemberProjection,
    BusinessCutoverAssetStagePlan,
    _DURABLE_READER_TOKEN,
    _FreshBusinessCutoverAssetCoverageObservation,
    _FreshBusinessCutoverAssetStageObservation,
    _classify_business_cutover_asset_coverage,
    _classify_business_cutover_asset_stage,
    _issue_business_cutover_asset_coverage_plan,
    _issue_business_cutover_asset_stage_member_projection,
    _issue_business_cutover_asset_stage_members,
    _issue_business_cutover_asset_stage_plan,
    _require_business_cutover_asset_coverage_plan,
    _require_business_cutover_asset_stage_member_projection,
    _require_business_cutover_asset_stage_members,
    _require_business_cutover_asset_stage_plan,
    _stamp_business_cutover_asset_coverage_observation,
    _stamp_business_cutover_asset_stage_observation,
)
from business_cutover_integrity import _issue_business_cutover_plan
from business_snapshot import BusinessSnapshotSet, capture_business_snapshot
from document import Document
from entity import Entity
from legacy_repository_contract import (
    LEGACY_REPOSITORY_LEGACY_ADOPT_OPERATION_ID,
    LEGACY_REPOSITORY_MEMBER_DELETED,
    LEGACY_REPOSITORY_MEMBER_LIVE,
)
from legacy_repository_integrity import (
    LegacyRepositoryIntegrityMember,
    build_legacy_repository_integrity,
)


CATALOG_SEAL = b"s" * 32
LEGACY_SCHEMA_FINGERPRINT = "f" * 64
TARGET_SCHEMA_FINGERPRINT = "8" * 64
RUNTIME_GENERATION = "core-3.15.0+asset-stage"
CODE_DIGEST = hashlib.sha256(b"asset-stage-code").digest()
BLOB_A = b"asset-a-content"
BLOB_B = b"asset-b-content"
BLOB_C = b"asset-c-content"
SHA_A = hashlib.sha256(BLOB_A).hexdigest()
SHA_B = hashlib.sha256(BLOB_B).hexdigest()
SHA_C = hashlib.sha256(BLOB_C).hexdigest()


def _ref(
    sha256: str,
    *,
    media_type: Optional[str] = "image/png",
    size: Optional[int] = None,
    extension: Optional[str] = ".png",
) -> Dict[str, Any]:
    value: Dict[str, Any] = {"sha256": sha256}
    if media_type is not None:
        value["mime"] = media_type
    if size is not None:
        value["size"] = size
    if extension is not None:
        value["ext"] = extension
    return value


def _snapshot(
    project_id: str,
    references: Iterable[Dict[str, Any]],
) -> BusinessSnapshotSet:
    document = Document(
        id="document-" + project_id,
        name="asset-cutover",
        schema_version="1",
        collaboration_mode="live_collab",
        save_policy="auto_commit",
        schema={
            "media": {
                "type": "object",
                "properties": {"asset": {"type": "asset_ref"}},
            },
            "item": {
                "type": "object",
                "properties": {"value": {"type": "integer"}},
            },
        },
    )
    entities = []
    for index, reference in enumerate(references):
        entity = Entity(
            type="media",
            id="media-%03d" % index,
            data={"asset": dict(reference)},
        )
        document.add(entity)
        entities.append(entity)
    item = Entity(type="item", id="item-1", data={"value": 1})
    document.add(item)
    entities.append(item)
    for version, entity in enumerate(entities, start=1):
        entity._version = version
        entity._created_at = version
        entity._updated_at = version
    document._v = 7
    document._created_at = 1
    document._updated_at = len(entities) + 1
    return capture_business_snapshot(project_id, 1, document).snapshot


def _cutover(
    snapshots: Tuple[BusinessSnapshotSet, ...],
    *,
    repository_epoch: str = "e" * 64,
    include_deleted: bool = False,
):
    ordered = tuple(sorted(snapshots, key=lambda value: value.project.project_id))
    live_members = tuple(
        LegacyRepositoryIntegrityMember(
            project_id=snapshot.project.project_id,
            generation=snapshot.project.generation,
            status=LEGACY_REPOSITORY_MEMBER_LIVE,
            operation_id=LEGACY_REPOSITORY_LEGACY_ADOPT_OPERATION_ID,
            source_state_sha256=hashlib.sha256(
                snapshot.project.project_id.encode("utf-8")
            ).digest(),
            source_state_bytes=17,
            catalog_ordinal=ordinal,
            import_id=hashlib.sha256(
                ("import:" + snapshot.project.project_id).encode("utf-8")
            ).hexdigest(),
            document_id=snapshot.document.document_id,
            document_version=snapshot.document.document_version,
            storage_version="v2",
            business_root_sha256=snapshot.project.snapshot_sha256,
            entity_count=len(snapshot.entities),
            row_snapshot_bytes=sum(
                len(row.snapshot_bytes) for row in snapshot.entities
            ) + len(snapshot.document.snapshot_bytes),
        )
        for ordinal, snapshot in enumerate(ordered)
    )
    deleted_members = (
        (
            LegacyRepositoryIntegrityMember(
                project_id="project-z-deleted",
                generation=9,
                status=LEGACY_REPOSITORY_MEMBER_DELETED,
                operation_id="1" * 32,
                source_state_sha256=hashlib.sha256(b"").digest(),
                source_state_bytes=0,
                catalog_ordinal=None,
                import_id=None,
                document_id=None,
                document_version=None,
                storage_version=None,
                business_root_sha256=None,
                entity_count=None,
                row_snapshot_bytes=None,
            ),
        )
        if include_deleted
        else ()
    )
    members = tuple(sorted(
        live_members + deleted_members,
        key=lambda value: value.project_id,
    ))
    proof = build_legacy_repository_integrity(
        catalog_id="c" * 64,
        catalog_seal_sha256=CATALOG_SEAL,
        repository_epoch=repository_epoch,
        repository_revision=11,
        catalog_revision=11,
        runtime_generation=RUNTIME_GENERATION,
        code_digest=CODE_DIGEST,
        members=members,
        schema_fingerprint=LEGACY_SCHEMA_FINGERPRINT,
    )
    plan = _issue_business_cutover_plan(
        legacy_integrity=proof,
        legacy_members=members,
        catalog_seal_sha256=CATALOG_SEAL,
        legacy_schema_fingerprint=LEGACY_SCHEMA_FINGERPRINT,
        runtime_generation=RUNTIME_GENERATION,
        code_digest=CODE_DIGEST,
        expected_target_schema_version=8,
        expected_target_schema_fingerprint=TARGET_SCHEMA_FINGERPRINT,
        business_snapshots=ordered,
    )
    return plan, ordered


def _carrier(
    data: bytes,
    *,
    sha256: Optional[str] = None,
    media_type: str = "image/png",
    extension: str = ".png",
) -> BusinessCutoverAssetBlobCarrier:
    return BusinessCutoverAssetBlobCarrier(
        sha256=hashlib.sha256(data).hexdigest() if sha256 is None else sha256,
        blob_bytes=data,
        byte_length=len(data),
        media_type=media_type,
        extension=extension,
    )


def _shared_repository():
    project_a = _snapshot(
        "project-a",
        (
            _ref(SHA_A, size=len(BLOB_A)),
            _ref(SHA_B, size=len(BLOB_B)),
        ),
    )
    project_b = _snapshot(
        "project-b",
        (_ref(SHA_A, size=len(BLOB_A)),),
    )
    return _cutover((project_b, project_a))


def _observe_stage(
    stage: BusinessCutoverAssetStagePlan,
    current_admission: Any,
    evidence: Any,
    *,
    indeterminate: bool = False,
):
    return _stamp_business_cutover_asset_stage_observation(
        plan=stage,
        current_admission=current_admission,
        evidence=evidence,
        indeterminate=indeterminate,
        _reader_token=_DURABLE_READER_TOKEN,
    )


def _exact_stage_observations(
    cutover: Any,
    stages: Iterable[BusinessCutoverAssetStagePlan],
):
    return tuple(sorted(
        (
            _observe_stage(
                stage,
                cutover.source_admission,
                stage.expected_evidence,
            )
            for stage in stages
        ),
        key=lambda value: value.evidence.batch_id,
    ))


def _observe_coverage(
    coverage: BusinessCutoverAssetCoveragePlan,
    current_admission: Any,
    evidence: Any,
    *,
    indeterminate: bool = False,
):
    return _stamp_business_cutover_asset_coverage_observation(
        plan=coverage,
        current_admission=current_admission,
        evidence=evidence,
        indeterminate=indeterminate,
        _reader_token=_DURABLE_READER_TOKEN,
    )


def test_bounded_stage_hashes_actual_bytes_and_deduplicates_occurrences() -> None:
    cutover, snapshots = _shared_repository()
    stage = _issue_business_cutover_asset_stage_plan(
        cutover_plan=cutover,
        business_snapshots=snapshots,
        blob_carriers=(_carrier(BLOB_A),),
    )
    assert isinstance(stage, BusinessCutoverAssetStagePlan)
    assert _require_business_cutover_asset_stage_plan(stage) is stage
    assert stage.coverage.unique_blob_count == 1
    assert stage.coverage.reference_count == 2
    assert stage.coverage.unique_blob_bytes == len(BLOB_A)
    assert stage.coverage.referenced_bytes == len(BLOB_A) * 2
    assert stage.blobs[0].project_count == 2
    assert stage.blobs[0].occurrence_count == 2
    assert len(stage.projects) == 2
    assert stage.expected_evidence.batch_id == stage.batch_id
    assert stage.repository_epoch == cutover.legacy_proof.repository_epoch
    assert stage.source_admission_sha256 == (
        cutover.closure.source_admission_sha256
    )
    assert stage.runtime_generation == RUNTIME_GENERATION
    assert stage.code_digest == CODE_DIGEST
    assert b'"unique_blob_count":1' in stage.manifest_bytes
    assert not hasattr(stage.expected_evidence, "blob_bytes")


def test_stage_member_projection_is_deterministic_domain_separated_and_dense() -> None:
    skipped = _snapshot(
        "project-a",
        (_ref(SHA_B, size=len(BLOB_B)),),
    )
    selected = _snapshot(
        "project-b",
        (_ref(SHA_A, size=len(BLOB_A)),),
    )
    cutover, snapshots = _cutover((selected, skipped))
    first = _issue_business_cutover_asset_stage_plan(
        cutover_plan=cutover,
        business_snapshots=snapshots,
        blob_carriers=(_carrier(BLOB_A),),
    )
    second = _issue_business_cutover_asset_stage_plan(
        cutover_plan=cutover,
        business_snapshots=snapshots,
        blob_carriers=(_carrier(BLOB_A),),
    )

    assert first.members == second.members
    assert _require_business_cutover_asset_stage_members(first.members) is (
        first.members
    )
    assert [member.member_kind for member in first.members] == [
        "project",
        "blob",
        "occurrence",
    ]
    assert [member.ordinal for member in first.members] == [0, 0, 0]
    # The only selected project is repository member 1, while its SQL member
    # ordinal is deliberately dense inside the project kind.
    assert first.projects[0].project_ordinal == 1
    assert first.members[0].ordinal == 0
    assert [member.member_bytes for member in first.members] == [
        0,
        len(BLOB_A),
        len(BLOB_A),
    ]
    assert all(
        type(member) is BusinessCutoverAssetStageMemberProjection
        and len(member.member_id) == 64
        and len(member.member_sha256) == 32
        and len(member.metadata_sha256) == 32
        and member.member_sha256 != member.metadata_sha256
        for member in first.members
    )
    assert len({member.member_id for member in first.members}) == 3
    assert tuple(
        (
            member.member_id,
            member.member_sha256.hex(),
            member.metadata_sha256.hex(),
        )
        for member in first.members
    ) == (
        (
            "dc3308b88ac8846891c6f1f9c1ad87cb835b90aeb27d80a290e6aa1285fae91c",
            "54d208268e50dacf067d5d51735528ff28d9c642bef2e699d6cce783088956ca",
            "4a5d58e9d58952bcdb7787ca5c3a2ccea1b4e36f7e2778a354483ed784d8840d",
        ),
        (
            "d8a82c563281e856dd2e0134973e23efaba9483b6b6f7b50004d2f66f3f8a748",
            "ff3edfe510807365ae85a483d257990fe5a1f439ba87ac19140428af07182075",
            "7b6421a76271182aa82142a453b0ff4d593e4594d8402fbf4827a817176498a2",
        ),
        (
            "6b9128408b4ab853e88fde9434cdf7a305b167f7ee05ba90a590966699efb4da",
            "bd4b93c8e8443fec0750a75e5d131384f260ead4489f06203099c9dd250f4909",
            "6d419859b40a59c2743f75bf15168e4cb490f2bab59f8f9c7ec134a552075191",
        ),
    )
    assert first.expected_evidence.manifest_bytes == first.manifest_bytes


def test_every_stage_member_source_field_is_bound_or_rejected() -> None:
    cutover, snapshots = _shared_repository()
    stage = _issue_business_cutover_asset_stage_plan(
        cutover_plan=cutover,
        business_snapshots=snapshots,
        blob_carriers=(_carrier(BLOB_A),),
    )
    digest_a = hashlib.sha256(b"changed-a").digest()
    digest_b = hashlib.sha256(b"changed-b").digest()

    project = stage.projects[0]
    project_base = _issue_business_cutover_asset_stage_member_projection(
        source=project,
        ordinal=0,
    )
    project_cases = {
        "project_ordinal": ({"project_ordinal": project.project_ordinal + 9}, False),
        "project_id": ({"project_id": project.project_id + "-changed"}, True),
        "generation": ({"generation": project.generation + 1}, True),
        "document_id": ({"document_id": project.document_id + "-changed"}, False),
        "business_root_sha256": ({"business_root_sha256": digest_a}, False),
        "projection_sha256": ({"projection_sha256": digest_b}, False),
        "occurrence_start": ({"occurrence_start": project.occurrence_start + 1}, False),
        "occurrence_count": ({"occurrence_count": project.occurrence_count + 1}, False),
        "unique_blob_count": ({"unique_blob_count": project.unique_blob_count + 1}, False),
        "unique_blob_bytes": ({"unique_blob_bytes": project.unique_blob_bytes + 1}, False),
        "occurrence_sha256": ({"occurrence_sha256": digest_a}, False),
    }
    assert set(project_cases) == set(project.__dataclass_fields__)
    for changes, identity_changes in project_cases.values():
        changed = _issue_business_cutover_asset_stage_member_projection(
            source=replace(project, **changes),
            ordinal=0,
        )
        assert changed.member_sha256 != project_base.member_sha256
        assert changed.metadata_sha256 != project_base.metadata_sha256
        assert (changed.member_id != project_base.member_id) is identity_changes

    blob = stage.blobs[0]
    blob_base = _issue_business_cutover_asset_stage_member_projection(
        source=blob,
        ordinal=0,
    )
    blob_cases = {
        "blob_ordinal": {"blob_ordinal": blob.blob_ordinal + 1},
        "asset_sha256": {
            "asset_sha256": SHA_B,
            "content_key": "sha256/%s/%s" % (SHA_B[:2], SHA_B),
        },
        "media_type": {"media_type": "application/octet-stream"},
        "byte_length": {"byte_length": blob.byte_length + 1},
        "extension": {"extension": ".bin"},
        "occurrence_count": {"occurrence_count": blob.occurrence_count + 1},
        "project_count": {"project_count": blob.project_count + 1},
    }
    assert set(blob_cases) | {"content_key"} == set(blob.__dataclass_fields__)
    for field_name, changes in blob_cases.items():
        changed = _issue_business_cutover_asset_stage_member_projection(
            source=replace(blob, **changes),
            ordinal=0,
        )
        assert changed.member_sha256 != blob_base.member_sha256
        assert changed.metadata_sha256 != blob_base.metadata_sha256
        assert (changed.member_id != blob_base.member_id) is (
            field_name == "asset_sha256"
        )
    # content_key is the canonical rendering of asset_sha256, so it has no
    # independent legal value: a one-field change is rejected before hashing.
    with pytest.raises(BusinessCutoverAssetIntegrityError):
        _issue_business_cutover_asset_stage_member_projection(
            source=replace(blob, content_key="sha256/00/" + "0" * 64),
            ordinal=0,
        )

    occurrence = stage.occurrences[0]
    occurrence_base = _issue_business_cutover_asset_stage_member_projection(
        source=occurrence,
        ordinal=0,
    )
    occurrence_cases = {
        "ordinal": ({"ordinal": occurrence.ordinal + 1}, False),
        "project_ordinal": ({"project_ordinal": occurrence.project_ordinal + 1}, False),
        "project_id": ({"project_id": occurrence.project_id + "-changed"}, True),
        "generation": ({"generation": occurrence.generation + 1}, True),
        "business_root_sha256": ({"business_root_sha256": digest_a}, False),
        "projection_sha256": ({"projection_sha256": digest_b}, False),
        "entity_type": ({"entity_type": occurrence.entity_type + "-changed"}, True),
        "entity_id": ({"entity_id": occurrence.entity_id + "-changed"}, True),
        "entity_deleted": ({"entity_deleted": not occurrence.entity_deleted}, True),
        "value_path": ({"value_path": occurrence.value_path + "/changed"}, True),
        "blob_ordinal": ({"blob_ordinal": occurrence.blob_ordinal + 1}, False),
        "asset_sha256": ({"asset_sha256": SHA_B}, True),
        "media_type": ({"media_type": "application/octet-stream"}, False),
        "byte_length": ({"byte_length": occurrence.byte_length + 1}, False),
        "extension": ({"extension": ".bin"}, False),
    }
    assert set(occurrence_cases) == set(occurrence.__dataclass_fields__)
    for changes, identity_changes in occurrence_cases.values():
        changed = _issue_business_cutover_asset_stage_member_projection(
            source=replace(occurrence, **changes),
            ordinal=0,
        )
        assert changed.member_sha256 != occurrence_base.member_sha256
        assert changed.metadata_sha256 != occurrence_base.metadata_sha256
        assert (changed.member_id != occurrence_base.member_id) is (
            identity_changes
        )


def test_stage_member_order_types_tamper_and_budgets_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _snapshot(
        "project-members",
        (
            _ref(SHA_A, size=len(BLOB_A)),
            _ref(SHA_B, size=len(BLOB_B)),
        ),
    )
    cutover, snapshots = _cutover((snapshot,))
    stage = _issue_business_cutover_asset_stage_plan(
        cutover_plan=cutover,
        business_snapshots=snapshots,
        blob_carriers=tuple(sorted(
            (_carrier(BLOB_A), _carrier(BLOB_B)),
            key=lambda value: value.sha256,
        )),
    )
    with pytest.raises(BusinessCutoverAssetIntegrityError) as order:
        _issue_business_cutover_asset_stage_members(
            projects=stage.projects,
            blobs=tuple(reversed(stage.blobs)),
            occurrences=stage.occurrences,
        )
    assert order.value.code == "business_cutover_asset_stage_members_order_invalid"
    with pytest.raises(BusinessCutoverAssetIntegrityError):
        _require_business_cutover_asset_stage_members(
            tuple(reversed(stage.members))
        )
    with pytest.raises(BusinessCutoverAssetIntegrityError) as invalid_collection:
        _issue_business_cutover_asset_stage_members(
            projects=list(stage.projects),  # type: ignore[arg-type]
            blobs=stage.blobs,
            occurrences=stage.occurrences,
        )
    assert invalid_collection.value.code == (
        "business_cutover_asset_stage_members_invalid"
    )
    with pytest.raises(BusinessCutoverAssetIntegrityError):
        _issue_business_cutover_asset_stage_member_projection(
            source={},
            ordinal=0,
        )
    with pytest.raises(BusinessCutoverAssetIntegrityError):
        _issue_business_cutover_asset_stage_member_projection(
            source=stage.projects[0],
            ordinal=True,  # type: ignore[arg-type]
        )
    with pytest.raises(BusinessCutoverAssetIntegrityError) as text_budget:
        _issue_business_cutover_asset_stage_member_projection(
            source=replace(stage.projects[0], project_id="x" * 1025),
            ordinal=0,
        )
    assert text_budget.value.code == (
        "business_cutover_asset_stage_member_project_invalid"
    )
    with pytest.raises(BusinessCutoverAssetIntegrityError):
        _issue_business_cutover_asset_stage_member_projection(
            source=replace(stage.occurrences[0], entity_deleted=1),
            ordinal=0,
        )

    member = _issue_business_cutover_asset_stage_member_projection(
        source=stage.projects[0],
        ordinal=0,
    )
    object.__setattr__(member, "member_sha256", b"x" * 32)
    with pytest.raises(BusinessCutoverAssetIntegrityError) as changed:
        _require_business_cutover_asset_stage_member_projection(member)
    assert changed.value.code == "business_cutover_asset_stage_member_changed"

    import business_cutover_asset_integrity as integrity
    from business_codec import BusinessCodecError

    def reject_manifest(value: Any, maximum_bytes: int, maximum_nodes: int) -> bytes:
        del value
        assert maximum_bytes > 0
        assert maximum_nodes > 0
        raise BusinessCodecError("business_codec_size_exceeded", "$", "dict")

    monkeypatch.setattr(integrity, "encode_payload_bounded", reject_manifest)
    with pytest.raises(BusinessCutoverAssetIntegrityError) as manifest_budget:
        _issue_business_cutover_asset_stage_member_projection(
            source=stage.projects[0],
            ordinal=0,
        )
    assert manifest_budget.value.code == (
        "business_cutover_asset_manifest_encoding_failed"
    )


def test_stage_plan_is_not_durable_evidence_and_reconcile_is_fresh() -> None:
    cutover, snapshots = _shared_repository()
    stage = _issue_business_cutover_asset_stage_plan(
        cutover_plan=cutover,
        business_snapshots=snapshots,
        blob_carriers=(_carrier(BLOB_A),),
    )
    absent = _classify_business_cutover_asset_stage(
        stage,
        _observe_stage(stage, cutover.source_admission, None),
    )
    assert absent.status is BusinessCutoverAssetStageReconcileStatus.NOT_STAGED
    assert absent.retry_action is BusinessCutoverAssetStageRetryAction.STAGE_EXACT
    exact = _classify_business_cutover_asset_stage(
        stage,
        _observe_stage(
            stage,
            cutover.source_admission,
            stage.expected_evidence,
        ),
    )
    assert exact.status is BusinessCutoverAssetStageReconcileStatus.STAGED_EXACT
    assert exact.retry_action is BusinessCutoverAssetStageRetryAction.RETURN_EXACT
    unknown = _classify_business_cutover_asset_stage(
        stage,
        _observe_stage(
            stage,
            cutover.source_admission,
            None,
            indeterminate=True,
        ),
    )
    assert unknown.status is BusinessCutoverAssetStageReconcileStatus.UNKNOWN
    assert unknown.retry_action is (
        BusinessCutoverAssetStageRetryAction.RECONCILE_REQUIRED
    )
    pending_missing = _classify_business_cutover_asset_stage(
        stage,
        _observe_stage(stage, cutover.pending_admission, None),
    )
    assert pending_missing.status is BusinessCutoverAssetStageReconcileStatus.CORRUPT
    with pytest.raises(BusinessCutoverAssetIntegrityError):
        _classify_business_cutover_asset_stage(
            stage,
            stage.expected_evidence,  # type: ignore[arg-type]
        )
    fake = object.__new__(_FreshBusinessCutoverAssetStageObservation)
    with pytest.raises(BusinessCutoverAssetIntegrityError):
        _classify_business_cutover_asset_stage(stage, fake)
    with pytest.raises(BusinessCutoverAssetIntegrityError):
        _stamp_business_cutover_asset_stage_observation(
            plan=stage,
            current_admission=cutover.source_admission,
            evidence=stage.expected_evidence,
            _reader_token=object(),
        )


def test_two_batches_form_byte_free_complete_repository_coverage() -> None:
    cutover, snapshots = _shared_repository()
    first = _issue_business_cutover_asset_stage_plan(
        cutover_plan=cutover,
        business_snapshots=snapshots,
        blob_carriers=(_carrier(BLOB_A),),
    )
    second = _issue_business_cutover_asset_stage_plan(
        cutover_plan=cutover,
        business_snapshots=snapshots,
        blob_carriers=(_carrier(BLOB_B),),
    )
    observations = _exact_stage_observations(cutover, (first, second))
    coverage = _issue_business_cutover_asset_coverage_plan(
        cutover_plan=cutover,
        business_snapshots=snapshots,
        stage_observations=observations,
    )
    assert isinstance(coverage, BusinessCutoverAssetCoveragePlan)
    assert _require_business_cutover_asset_coverage_plan(coverage) is coverage
    assert coverage.coverage.project_count == 2
    assert coverage.coverage.reference_count == 3
    assert coverage.coverage.unique_blob_count == 2
    assert coverage.coverage.unique_blob_bytes == len(BLOB_A) + len(BLOB_B)
    assert coverage.expected_evidence.batch_count == 2
    assert coverage.expected_evidence.observed_occurrence_count == sum(
        evidence.reference_count for evidence in coverage.batch_evidences
    )
    assert coverage.expected_evidence.observed_row_count == sum(
        len(evidence.projects)
        + len(evidence.blobs)
        + len(evidence.occurrences)
        for evidence in coverage.batch_evidences
    )
    assert coverage.expected_evidence.observed_manifest_bytes == sum(
        len(evidence.manifest_bytes) for evidence in coverage.batch_evidences
    )
    assert b'"observed"' in coverage.manifest_bytes
    assert not hasattr(coverage, "_blob_carriers")
    assert not hasattr(coverage, "stage_observations")
    assert all(
        not any(
            isinstance(value, bytes) and value in (BLOB_A, BLOB_B)
            for value in evidence.__dict__.values()
        )
        for evidence in coverage.batch_evidences
    )

    absent = _classify_business_cutover_asset_coverage(
        coverage,
        _observe_coverage(coverage, cutover.source_admission, None),
    )
    assert absent.status is BusinessCutoverAssetCoverageReconcileStatus.NOT_COVERED
    assert absent.retry_action is (
        BusinessCutoverAssetCoverageRetryAction.PERSIST_EXACT
    )
    exact = _classify_business_cutover_asset_coverage(
        coverage,
        _observe_coverage(
            coverage,
            cutover.source_admission,
            coverage.expected_evidence,
        ),
    )
    assert exact.status is BusinessCutoverAssetCoverageReconcileStatus.COVERED_EXACT
    assert exact.retry_action is (
        BusinessCutoverAssetCoverageRetryAction.RETURN_EXACT
    )
    tampered = replace(
        coverage.expected_evidence,
        coverage_sha256=b"x" * 32,
    )
    corrupt = _classify_business_cutover_asset_coverage(
        coverage,
        _observe_coverage(coverage, cutover.source_admission, tampered),
    )
    assert corrupt.status is BusinessCutoverAssetCoverageReconcileStatus.CORRUPT
    pending_missing = _classify_business_cutover_asset_coverage(
        coverage,
        _observe_coverage(coverage, cutover.pending_admission, None),
    )
    assert pending_missing.status is (
        BusinessCutoverAssetCoverageReconcileStatus.CORRUPT
    )
    with pytest.raises(BusinessCutoverAssetIntegrityError):
        _classify_business_cutover_asset_coverage(
            coverage,
            coverage.expected_evidence,  # type: ignore[arg-type]
        )
    fake = object.__new__(_FreshBusinessCutoverAssetCoverageObservation)
    with pytest.raises(BusinessCutoverAssetIntegrityError):
        _classify_business_cutover_asset_coverage(coverage, fake)


def test_zero_assets_are_explicitly_closed_without_a_fake_empty_batch() -> None:
    snapshot = _snapshot("project-zero", ())
    cutover, snapshots = _cutover((snapshot,), include_deleted=True)
    with pytest.raises(BusinessCutoverAssetIntegrityError) as exc:
        _issue_business_cutover_asset_stage_plan(
            cutover_plan=cutover,
            business_snapshots=snapshots,
            blob_carriers=(),
        )
    assert exc.value.code == "business_cutover_asset_empty_batch_invalid"

    coverage = _issue_business_cutover_asset_coverage_plan(
        cutover_plan=cutover,
        business_snapshots=snapshots,
        stage_observations=(),
    )
    assert coverage.expected_evidence.batch_count == 0
    assert coverage.coverage.project_count == 1
    assert coverage.coverage.reference_count == 0
    assert coverage.coverage.unique_blob_count == 0
    assert coverage.coverage.unique_blob_bytes == 0
    assert coverage.projects[0].occurrence_count == 0
    assert coverage.blobs == () and coverage.occurrences == ()
    assert b'"batches":[]' in coverage.manifest_bytes
    assert b'"unique_blob_count":0' in coverage.manifest_bytes
    assert b'"deleted_project_count":1' in coverage.manifest_bytes
    assert b'"project_id":"project-z-deleted"' in coverage.manifest_bytes


def test_missing_extra_and_exact_cross_batch_reuse_are_closed() -> None:
    cutover, snapshots = _shared_repository()
    first = _issue_business_cutover_asset_stage_plan(
        cutover_plan=cutover,
        business_snapshots=snapshots,
        blob_carriers=(_carrier(BLOB_A),),
    )
    with pytest.raises(BusinessCutoverAssetIntegrityError) as missing:
        _issue_business_cutover_asset_coverage_plan(
            cutover_plan=cutover,
            business_snapshots=snapshots,
            stage_observations=_exact_stage_observations(cutover, (first,)),
        )
    assert missing.value.code == "business_cutover_asset_repository_blob_missing"
    with pytest.raises(BusinessCutoverAssetIntegrityError) as raw:
        _issue_business_cutover_asset_coverage_plan(
            cutover_plan=cutover,
            business_snapshots=snapshots,
            stage_observations=(first.expected_evidence,),  # type: ignore[arg-type]
        )
    assert raw.value.code == "business_cutover_asset_stage_observation_not_exact"
    with pytest.raises(BusinessCutoverAssetIntegrityError) as absent:
        _issue_business_cutover_asset_coverage_plan(
            cutover_plan=cutover,
            business_snapshots=snapshots,
            stage_observations=(
                _observe_stage(first, cutover.source_admission, None),
            ),
        )
    assert absent.value.code == "business_cutover_asset_stage_observation_not_exact"

    with pytest.raises(BusinessCutoverAssetIntegrityError) as extra:
        _issue_business_cutover_asset_stage_plan(
            cutover_plan=cutover,
            business_snapshots=snapshots,
            blob_carriers=(_carrier(b"not-referenced"),),
        )
    assert extra.value.code == "business_cutover_asset_blob_extra"

    combined = _issue_business_cutover_asset_stage_plan(
        cutover_plan=cutover,
        business_snapshots=snapshots,
        blob_carriers=tuple(sorted(
            (_carrier(BLOB_A), _carrier(BLOB_B)),
            key=lambda value: value.sha256,
        )),
    )
    duplicate = _exact_stage_observations(cutover, (first, combined))
    reused = _issue_business_cutover_asset_coverage_plan(
        cutover_plan=cutover,
        business_snapshots=snapshots,
        stage_observations=duplicate,
    )
    assert reused.expected_evidence.batch_count == 2
    assert reused.coverage.unique_blob_count == 2
    assert reused.coverage.unique_blob_bytes == len(BLOB_A) + len(BLOB_B)


def test_shared_sha_can_connect_two_bounded_batches_without_double_charge() -> None:
    project_a = _snapshot(
        "project-a",
        (
            _ref(SHA_A, size=len(BLOB_A)),
            _ref(SHA_B, size=len(BLOB_B)),
        ),
    )
    project_b = _snapshot(
        "project-b",
        (
            _ref(SHA_A, size=len(BLOB_A)),
            _ref(SHA_C, size=len(BLOB_C)),
        ),
    )
    cutover, snapshots = _cutover((project_a, project_b))
    left = _issue_business_cutover_asset_stage_plan(
        cutover_plan=cutover,
        business_snapshots=snapshots,
        blob_carriers=tuple(sorted(
            (_carrier(BLOB_A), _carrier(BLOB_B)),
            key=lambda value: value.sha256,
        )),
    )
    right = _issue_business_cutover_asset_stage_plan(
        cutover_plan=cutover,
        business_snapshots=snapshots,
        blob_carriers=tuple(sorted(
            (_carrier(BLOB_A), _carrier(BLOB_C)),
            key=lambda value: value.sha256,
        )),
    )
    coverage = _issue_business_cutover_asset_coverage_plan(
        cutover_plan=cutover,
        business_snapshots=snapshots,
        stage_observations=_exact_stage_observations(
            cutover, (left, right)
        ),
    )
    assert coverage.expected_evidence.batch_count == 2
    assert coverage.coverage.reference_count == 4
    assert coverage.coverage.unique_blob_count == 3
    assert coverage.coverage.unique_blob_bytes == (
        len(BLOB_A) + len(BLOB_B) + len(BLOB_C)
    )
    shared = next(blob for blob in coverage.blobs if blob.asset_sha256 == SHA_A)
    assert shared.occurrence_count == 2
    assert shared.project_count == 2


def test_cross_batch_shared_sha_metadata_disagreement_is_conflict() -> None:
    project_a = _snapshot(
        "project-a",
        (_ref(SHA_A, media_type=None, size=None, extension=None),),
    )
    project_b = _snapshot(
        "project-b",
        (_ref(SHA_A, media_type=None, size=None, extension=None),),
    )
    cutover, snapshots = _cutover((project_a, project_b))
    image = _issue_business_cutover_asset_stage_plan(
        cutover_plan=cutover,
        business_snapshots=snapshots,
        blob_carriers=(_carrier(BLOB_A),),
    )
    binary = _issue_business_cutover_asset_stage_plan(
        cutover_plan=cutover,
        business_snapshots=snapshots,
        blob_carriers=(_carrier(
            BLOB_A,
            media_type="application/octet-stream",
            extension=".bin",
        ),),
    )
    observations = _exact_stage_observations(cutover, (image, binary))
    with pytest.raises(BusinessCutoverAssetIntegrityError) as conflict:
        _issue_business_cutover_asset_coverage_plan(
            cutover_plan=cutover,
            business_snapshots=snapshots,
            stage_observations=observations,
        )
    assert conflict.value.code == (
        "business_cutover_asset_blob_reuse_metadata_conflict"
    )


def test_metadata_conflicts_size_mismatch_and_hash_mismatch_are_rejected() -> None:
    sparse = _snapshot(
        "project-sparse",
        (_ref(SHA_A, media_type=None, size=None, extension=None),),
    )
    sparse_cutover, sparse_snapshots = _cutover((sparse,))
    sparse_stage = _issue_business_cutover_asset_stage_plan(
        cutover_plan=sparse_cutover,
        business_snapshots=sparse_snapshots,
        blob_carriers=(_carrier(BLOB_A),),
    )
    assert sparse_stage.blobs[0].media_type == "image/png"
    assert sparse_stage.blobs[0].extension == ".png"

    png = _snapshot(
        "project-a",
        (_ref(SHA_A, media_type="image/png", size=len(BLOB_A)),),
    )
    pdf = _snapshot(
        "project-b",
        (_ref(SHA_A, media_type="application/pdf", size=len(BLOB_A)),),
    )
    conflict_cutover, conflict_snapshots = _cutover((png, pdf))
    with pytest.raises(BusinessCutoverAssetIntegrityError) as conflict:
        _issue_business_cutover_asset_stage_plan(
            cutover_plan=conflict_cutover,
            business_snapshots=conflict_snapshots,
            blob_carriers=(_carrier(BLOB_A),),
        )
    assert conflict.value.code == "business_cutover_asset_reference_metadata_conflict"

    wrong_size = _snapshot(
        "project-size",
        (_ref(SHA_A, size=len(BLOB_A) + 1),),
    )
    size_cutover, size_snapshots = _cutover((wrong_size,))
    with pytest.raises(BusinessCutoverAssetIntegrityError) as size:
        _issue_business_cutover_asset_stage_plan(
            cutover_plan=size_cutover,
            business_snapshots=size_snapshots,
            blob_carriers=(_carrier(BLOB_A),),
        )
    assert size.value.code == "business_cutover_asset_blob_metadata_mismatch"

    with pytest.raises(BusinessCutoverAssetContractError) as digest:
        _carrier(BLOB_B, sha256=SHA_A)
    assert digest.value.code == "business_cutover_asset_blob_hash_mismatch"


def test_tampered_plan_and_durable_evidence_are_detected() -> None:
    cutover, snapshots = _shared_repository()
    stage = _issue_business_cutover_asset_stage_plan(
        cutover_plan=cutover,
        business_snapshots=snapshots,
        blob_carriers=(_carrier(BLOB_A),),
    )
    object.__setattr__(stage, "manifest_sha256", b"x" * 32)
    with pytest.raises(BusinessCutoverAssetIntegrityError) as changed:
        _require_business_cutover_asset_stage_plan(stage)
    assert changed.value.code == "business_cutover_asset_plan_changed"

    clean = _issue_business_cutover_asset_stage_plan(
        cutover_plan=cutover,
        business_snapshots=snapshots,
        blob_carriers=(_carrier(BLOB_A),),
    )
    tampered = replace(
        clean.expected_evidence,
        manifest_bytes=clean.expected_evidence.manifest_bytes + b"x",
    )
    result = _classify_business_cutover_asset_stage(
        clean,
        _observe_stage(clean, cutover.source_admission, tampered),
    )
    assert result.status is BusinessCutoverAssetStageReconcileStatus.CORRUPT


def test_low_level_frozen_carrier_byte_replacement_is_rehashed_and_rejected() -> None:
    cutover, snapshots = _shared_repository()
    carrier = _carrier(BLOB_A)
    stage = _issue_business_cutover_asset_stage_plan(
        cutover_plan=cutover,
        business_snapshots=snapshots,
        blob_carriers=(carrier,),
    )
    assert len(BLOB_A) == len(BLOB_B)
    object.__setattr__(carrier, "blob_bytes", BLOB_B)
    # The stale construction stamp still names A, but require hashes the
    # current bytes once and therefore refuses to consume B under A's SHA.
    with pytest.raises(BusinessCutoverAssetIntegrityError) as tampered:
        _require_business_cutover_asset_stage_plan(stage)
    assert tampered.value.code == "business_cutover_asset_blob_hash_mismatch"


def test_budget_is_per_executable_batch_not_a_multi_terabyte_claim() -> None:
    snapshot = _snapshot(
        "project-budget",
        (
            _ref(SHA_A, size=len(BLOB_A)),
            _ref(SHA_B, size=len(BLOB_B)),
        ),
    )
    cutover, snapshots = _cutover((snapshot,))
    limits = BusinessCutoverAssetStageLimits(
        max_blob_bytes=max(len(BLOB_A), len(BLOB_B)),
        max_project_unique_bytes=len(BLOB_A) + len(BLOB_B),
        max_batch_unique_bytes=len(BLOB_A) + len(BLOB_B) - 1,
    )
    with pytest.raises(BusinessCutoverAssetIntegrityError) as budget:
        _issue_business_cutover_asset_stage_plan(
            cutover_plan=cutover,
            business_snapshots=snapshots,
            blob_carriers=tuple(sorted(
                (_carrier(BLOB_A), _carrier(BLOB_B)),
                key=lambda value: value.sha256,
            )),
            limits=limits,
        )
    assert budget.value.code == "business_cutover_asset_batch_budget_exceeded"


@pytest.mark.parametrize(
    "limit_name,limit_value",
    (
        ("max_coverage_observed_occurrences", 1),
        ("max_coverage_observed_rows", 1),
        ("max_coverage_observed_manifest_bytes", 1),
    ),
)
def test_repository_coverage_aggregate_budgets_fail_before_expansion(
    limit_name: str,
    limit_value: int,
) -> None:
    cutover, snapshots = _shared_repository()
    limits = replace(
        BusinessCutoverAssetStageLimits(),
        **{limit_name: limit_value},
    )
    stage = _issue_business_cutover_asset_stage_plan(
        cutover_plan=cutover,
        business_snapshots=snapshots,
        blob_carriers=(_carrier(BLOB_A),),
        limits=limits,
    )
    with pytest.raises(BusinessCutoverAssetIntegrityError) as budget:
        _issue_business_cutover_asset_coverage_plan(
            cutover_plan=cutover,
            business_snapshots=snapshots,
            stage_observations=_exact_stage_observations(cutover, (stage,)),
            limits=limits,
        )
    assert budget.value.code == (
        "business_cutover_asset_coverage_aggregate_budget_exceeded"
    )


def test_repository_coverage_batch_count_budget_is_hard() -> None:
    cutover, snapshots = _shared_repository()
    limits = replace(
        BusinessCutoverAssetStageLimits(),
        max_coverage_batch_count=1,
    )
    first = _issue_business_cutover_asset_stage_plan(
        cutover_plan=cutover,
        business_snapshots=snapshots,
        blob_carriers=(_carrier(BLOB_A),),
        limits=limits,
    )
    second = _issue_business_cutover_asset_stage_plan(
        cutover_plan=cutover,
        business_snapshots=snapshots,
        blob_carriers=(_carrier(BLOB_B),),
        limits=limits,
    )
    with pytest.raises(BusinessCutoverAssetIntegrityError) as budget:
        _issue_business_cutover_asset_coverage_plan(
            cutover_plan=cutover,
            business_snapshots=snapshots,
            stage_observations=_exact_stage_observations(
                cutover, (first, second)
            ),
            limits=limits,
        )
    assert budget.value.code == (
        "business_cutover_asset_coverage_batch_budget_exceeded"
    )


def test_foreign_repository_batch_cannot_be_reused_across_cutovers() -> None:
    first_cutover, first_snapshots = _shared_repository()
    foreign_stage = _issue_business_cutover_asset_stage_plan(
        cutover_plan=first_cutover,
        business_snapshots=first_snapshots,
        blob_carriers=(_carrier(BLOB_A),),
    )
    second_snapshot = _snapshot(
        "project-foreign",
        (_ref(SHA_A, size=len(BLOB_A)),),
    )
    second_cutover, second_snapshots = _cutover(
        (second_snapshot,), repository_epoch="d" * 64
    )
    with pytest.raises(BusinessCutoverAssetIntegrityError) as foreign:
        _issue_business_cutover_asset_coverage_plan(
            cutover_plan=second_cutover,
            business_snapshots=second_snapshots,
            stage_observations=_exact_stage_observations(
                first_cutover, (foreign_stage,)
            ),
        )
    assert foreign.value.code == "business_cutover_asset_stage_observation_not_exact"


def test_contract_requires_actual_bytes_and_pure_modules_have_no_io_ports() -> None:
    with pytest.raises(BusinessCutoverAssetContractError):
        BusinessCutoverAssetBlobCarrier(
            sha256=SHA_A,
            blob_bytes=True,  # type: ignore[arg-type]
            byte_length=1,
            media_type="image/png",
            extension=".png",
        )
    import business_cutover_asset_contract as contract
    import business_cutover_asset_integrity as integrity

    for module in (contract, integrity):
        source = inspect.getsource(module)
        for forbidden in (
            "import sqlite3",
            "from pathlib",
            "import os",
            "import time",
            "datetime.now",
        ):
            assert forbidden not in source
        assert module.__all__ == ()
    contract_source = inspect.getsource(contract)
    integrity_source = inspect.getsource(integrity)
    assert contract_source.count("hashlib.sha256(self.blob_bytes)") == 1
    assert integrity_source.count("hashlib.sha256(carrier.blob_bytes)") == 1
    assert "blobs[next(" not in integrity_source
