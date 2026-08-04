"""Fixed and adversarial vectors for the pure V11 baseline projection."""

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

import business_cutover_baseline_projection as projection_module
from business_cutover_baseline_projection import (
    ASSET_BLOBS_COLUMNS,
    BUSINESS_COMMITS_COLUMNS,
    BUSINESS_CUTOVER_BASELINE_EMPTY_DIGEST,
    BUSINESS_CUTOVER_BASELINE_OUTER_ATOMIC_STATUS,
    DOCUMENT_ASSET_REFS_COLUMNS,
    DOCUMENT_COMMIT_HEADS_COLUMNS,
    DOCUMENTS_COLUMNS,
    ENTITIES_COLUMNS,
    PROJECTS_COLUMNS,
    REPOSITORY_PROJECTS_COLUMNS,
    BusinessCutoverBaselineProjectionError,
    _project_business_cutover_baseline,
    _require_business_cutover_baseline_outer_atomic_supported,
    _require_business_cutover_baseline_projection,
)
from business_snapshot import capture_business_snapshot
from document import Document
from entity import Entity
from test_business_cutover_integrity import _deleted, _live, _plan


COMMITTED_AT = "2026-07-12T00:00:00.000Z"
ASSET_SHA = "a" * 64


def _snapshot(
    *,
    project_id: str = "project-a",
    document_id: str = "document-a",
    references=(),
):
    document = Document(
        id=document_id,
        name="baseline projection",
        schema_version="1",
        collaboration_mode="live_collab",
        save_policy="auto_commit",
        schema={
            "media": {
                "type": "object",
                "properties": {
                    "assets": {
                        "type": "array",
                        "items": {"type": "asset_ref"},
                    },
                },
            },
        },
    )
    entity = Entity(type="media", id="media-1", data={"assets": list(references)})
    document.add(entity)
    entity._version = 1
    entity._created_at = 1
    entity._updated_at = 1
    document._v = 7
    document._created_at = 1
    document._updated_at = 2
    return capture_business_snapshot(project_id, 1, document).snapshot


def _asset(*, size=7, mime="image/png", ext="png"):
    value = {"sha256": ASSET_SHA}
    if size is not None:
        value["size"] = size
    if mime is not None:
        value["mime"] = mime
    if ext is not None:
        value["ext"] = ext
    return value


def _single_plan(snapshot=None):
    selected = _snapshot() if snapshot is None else snapshot
    return _plan(
        members=(_live(selected),),
        snapshots=(selected,),
        expected_target_schema_version=11,
    )


@pytest.mark.parametrize(
    ("members", "snapshots", "expected"),
    (
        ((), (), (0, 0, 1)),
        ((_deleted(),), (), (0, 1, 1)),
        ((_live(_snapshot()),), (_snapshot(),), (1, 0, 1)),
    ),
)
def test_empty_live_and_deleted_shapes_are_closed(members, snapshots, expected):
    plan = _plan(
        members=members,
        snapshots=snapshots,
        expected_target_schema_version=11,
    )
    projected = _project_business_cutover_baseline(
        plan,
        committed_at=COMMITTED_AT,
    )
    assert _require_business_cutover_baseline_projection(projected) is projected
    assert (
        len(projected.live),
        len(projected.deleted),
        projected.runtime_repository_revision,
    ) == expected
    assert projected.outer_atomic_status == (
        BUSINESS_CUTOVER_BASELINE_OUTER_ATOMIC_STATUS
    )
    assert projected.scheduler_genesis.worker_ledger.tail_sha256 == (
        BUSINESS_CUTOVER_BASELINE_EMPTY_DIGEST
    )


def test_mixed_projection_is_column_exact_and_freezes_inner_atomic_seed() -> None:
    snapshot = _snapshot()
    plan = _plan(
        members=(_live(snapshot), _deleted()),
        snapshots=(snapshot,),
        expected_target_schema_version=11,
    )
    projected = _project_business_cutover_baseline(plan, committed_at=COMMITTED_AT)
    live = projected.live[0]
    deleted = projected.deleted[0]

    assert projected.manifest_sha256.hex() == (
        "0de3955fb63874cdcfd82d4933fb899536f9ff3d3951c1a071c0b708fc547e70"
    )
    assert live.atomic_seed.manifest_sha256.hex() == (
        "90d838a111c3d7639e8b75c93cee2b5634c14745269b987406e0c2504e197fc3"
    )
    assert live.atomic_seed.receipt_sha256.hex() == (
        "6b4ad15e9a83a84eb7598b2a7315b28eb04c1366cd099c96ccf4c3d47639d5fb"
    )
    assert len(live.project.sql_values()) == len(PROJECTS_COLUMNS)
    assert len(live.document.sql_values()) == len(DOCUMENTS_COLUMNS)
    assert all(len(row.sql_values()) == len(ENTITIES_COLUMNS) for row in live.entities)
    assert len(live.commit.sql_values()) == len(BUSINESS_COMMITS_COLUMNS)
    assert len(live.repository_project.sql_values()) == len(REPOSITORY_PROJECTS_COLUMNS)
    assert len(deleted.repository_project.sql_values()) == len(
        REPOSITORY_PROJECTS_COLUMNS
    )
    assert len(live.document_head.sql_values()) == len(DOCUMENT_COMMIT_HEADS_COLUMNS)
    assert live.commit.before_root_sha256 == b"\0" * 32
    assert live.commit.previous_operation_tail_sha256 == b"\0" * 32
    assert live.commit.operation_tail_sha256 == b"\0" * 32
    assert live.atomic_seed.control_count == 0
    assert live.commit.project_revision == live.commit.head_revision == 1
    assert deleted.repository_project.deleted_at == COMMITTED_AT


def test_single_asset_projects_blob_and_occurrence_rows() -> None:
    plan = _single_plan(_snapshot(references=(_asset(),)))
    projected = _project_business_cutover_baseline(plan, committed_at=COMMITTED_AT)
    live = projected.live[0]

    assert projected.asset_reference_row_count == 1
    assert projected.unique_asset_blob_count == 1
    assert projected.manifest_sha256.hex() == (
        "d1b93765bc671fa2ffecccf8c451da9c9b715067bd601877dc33a8a4e6c68c34"
    )
    assert len(projected.asset_blobs) == len(live.asset_references) == 1
    blob = projected.asset_blobs[0]
    reference = live.asset_references[0]
    assert len(blob.sql_values()) == len(ASSET_BLOBS_COLUMNS)
    assert len(reference.sql_values()) == len(DOCUMENT_ASSET_REFS_COLUMNS)
    assert blob.asset_sha256 == reference.asset_sha256 == bytes.fromhex(ASSET_SHA)
    assert blob.content_key == "sha256/aa/" + ASSET_SHA
    assert (blob.media_type, blob.byte_length, blob.extension) == (
        "image/png",
        7,
        "png",
    )
    assert blob.status == "available" and blob.state_revision == 1
    assert reference.commit_id == live.commit.commit_id
    assert reference.commit_asset_reference_count == 1
    assert reference.value_path == "/assets/0"
    assert b'"unique_asset_blob_count":1' in projected.manifest_bytes


def test_same_blob_multiple_occurrences_keeps_refs_and_deduplicates_blob() -> None:
    plan = _single_plan(_snapshot(references=(_asset(), _asset())))
    projected = _project_business_cutover_baseline(plan, committed_at=COMMITTED_AT)
    live = projected.live[0]

    assert projected.asset_reference_row_count == 2
    assert projected.unique_asset_blob_count == 1
    assert len(projected.asset_blobs) == 1
    assert tuple(value.ordinal for value in live.asset_references) == (0, 1)
    assert tuple(value.value_path for value in live.asset_references) == (
        "/assets/0",
        "/assets/1",
    )


def test_repository_wide_unknown_metadata_merges_with_known_metadata() -> None:
    unknown = _snapshot(
        project_id="project-a",
        document_id="document-a",
        references=(_asset(size=None, mime=None, ext=None),),
    )
    known = _snapshot(
        project_id="project-b",
        document_id="document-b",
        references=(_asset(size=7, mime="image/png", ext="png"),),
    )
    plan = _plan(
        members=(
            _live(unknown, catalog_ordinal=0, import_id="a" * 64),
            _live(known, catalog_ordinal=1, import_id="b" * 64),
        ),
        snapshots=(unknown, known),
        expected_target_schema_version=11,
    )
    projected = _project_business_cutover_baseline(plan, committed_at=COMMITTED_AT)

    assert projected.asset_reference_row_count == 2
    assert projected.unique_asset_blob_count == 1
    blob = projected.asset_blobs[0]
    assert (blob.media_type, blob.byte_length, blob.extension) == (
        "image/png",
        7,
        "png",
    )
    assert projected.live[0].asset_references[0].ref_size is None
    assert projected.live[1].asset_references[0].ref_size == 7


def test_repository_wide_same_sha_metadata_conflict_fails_closed() -> None:
    first = _snapshot(
        project_id="project-a",
        document_id="document-a",
        references=(_asset(size=7),),
    )
    second = _snapshot(
        project_id="project-b",
        document_id="document-b",
        references=(_asset(size=8),),
    )
    plan = _plan(
        members=(
            _live(first, catalog_ordinal=0, import_id="a" * 64),
            _live(second, catalog_ordinal=1, import_id="b" * 64),
        ),
        snapshots=(first, second),
        expected_target_schema_version=11,
    )
    with pytest.raises(BusinessCutoverBaselineProjectionError) as error:
        _project_business_cutover_baseline(plan, committed_at=COMMITTED_AT)
    assert error.value.code == "business_cutover_baseline_asset_metadata_conflict"


def test_missing_asset_size_is_not_fabricated_for_not_null_blob_length() -> None:
    plan = _single_plan(_snapshot(references=(_asset(size=None),)))
    with pytest.raises(BusinessCutoverBaselineProjectionError) as error:
        _project_business_cutover_baseline(plan, committed_at=COMMITTED_AT)
    assert error.value.code == "business_cutover_baseline_asset_size_missing"


def test_asset_row_tamper_and_noncanonical_time_fail_closed() -> None:
    plan = _single_plan(_snapshot(references=(_asset(),)))
    with pytest.raises(BusinessCutoverBaselineProjectionError) as error:
        _project_business_cutover_baseline(
            plan,
            committed_at="2026-07-12T00:00:00.000001Z",
        )
    assert error.value.code == "business_cutover_baseline_timestamp_invalid"

    projected = _project_business_cutover_baseline(plan, committed_at=COMMITTED_AT)
    changed_blob = replace(projected.asset_blobs[0], byte_length=9)
    object.__setattr__(projected, "asset_blobs", (changed_blob,))
    with pytest.raises(BusinessCutoverBaselineProjectionError) as error:
        _require_business_cutover_baseline_projection(projected)
    assert error.value.code == "business_cutover_baseline_projection_changed"


def test_repository_wide_snapshot_and_sql_payload_budgets_preflight(monkeypatch) -> None:
    plan = _single_plan()
    monkeypatch.setattr(
        projection_module,
        "MAX_BUSINESS_CUTOVER_BASELINE_TOTAL_SNAPSHOT_BYTES",
        1,
    )
    with pytest.raises(BusinessCutoverBaselineProjectionError) as error:
        _project_business_cutover_baseline(plan, committed_at=COMMITTED_AT)
    assert error.value.code == "business_cutover_baseline_snapshot_budget_exceeded"

    monkeypatch.setattr(
        projection_module,
        "MAX_BUSINESS_CUTOVER_BASELINE_TOTAL_SNAPSHOT_BYTES",
        1024 * 1024 * 1024,
    )
    monkeypatch.setattr(
        projection_module,
        "MAX_BUSINESS_CUTOVER_BASELINE_SQL_PAYLOAD_BYTES",
        1,
    )
    with pytest.raises(BusinessCutoverBaselineProjectionError) as error:
        _project_business_cutover_baseline(plan, committed_at=COMMITTED_AT)
    assert error.value.code == "business_cutover_baseline_sql_payload_budget_exceeded"


def test_valid_install_before_mount_chronology_requires_v12_outer_carrier() -> None:
    projected = _project_business_cutover_baseline(
        _single_plan(),
        committed_at=COMMITTED_AT,
    )
    with pytest.raises(BusinessCutoverBaselineProjectionError) as error:
        _require_business_cutover_baseline_outer_atomic_supported(
            projected,
            mounted_at="2026-07-12T00:00:00.001Z",
        )
    assert error.value.code == (
        "business_cutover_baseline_outer_atomic_requires_schema_upgrade"
    )

    with pytest.raises(BusinessCutoverBaselineProjectionError) as error:
        _require_business_cutover_baseline_outer_atomic_supported(
            projected,
            mounted_at="2026-07-11T23:59:59.999Z",
        )
    assert error.value.code == "business_cutover_baseline_mount_chronology_invalid"
