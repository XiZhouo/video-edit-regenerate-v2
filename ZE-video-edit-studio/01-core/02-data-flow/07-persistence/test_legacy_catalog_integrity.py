"""Pure contract tests for historical legacy-catalog integrity."""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from typing import Any

import pytest


_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import legacy_catalog_contract as contract
import legacy_catalog_integrity as integrity


IMPORT_ID = "a" * 64
SCHEMA_FINGERPRINT = "b" * 64


def _project(
    project_id: str = "alpha",
    *,
    document_id: str | None = None,
    generation: int = 3,
    document_version: int = 7,
    source_head_sha256: bytes = b"h" * 32,
    source_head_bytes: int = 19,
    business_root_sha256: bytes = b"r" * 32,
    entity_count: int = 2,
    row_snapshot_bytes: int = 41,
) -> integrity.LegacyCatalogIntegrityProject:
    return integrity.LegacyCatalogIntegrityProject(
        project_id=project_id,
        generation=generation,
        document_id=document_id or (project_id + "-document"),
        document_version=document_version,
        storage_version="v2",
        source_head_sha256=source_head_sha256,
        source_head_bytes=source_head_bytes,
        business_root_sha256=business_root_sha256,
        entity_count=entity_count,
        row_snapshot_bytes=row_snapshot_bytes,
    )


def _build(
    projects: tuple[integrity.LegacyCatalogIntegrityProject, ...],
    *,
    import_id: str | None = IMPORT_ID,
    schema_fingerprint: str = SCHEMA_FINGERPRINT,
) -> integrity.LegacyCatalogIntegrity:
    return integrity.build_legacy_catalog_integrity(
        import_id,
        projects,
        schema_fingerprint=schema_fingerprint,
    )


def test_nonempty_catalog_is_canonical_content_addressed_and_closed() -> None:
    projects = (
        _project("alpha"),
        _project(
            "zeta",
            generation=5,
            document_version=11,
            source_head_sha256=b"i" * 32,
            source_head_bytes=23,
            business_root_sha256=b"s" * 32,
            entity_count=4,
            row_snapshot_bytes=59,
        ),
    )
    first = _build(projects)
    second = _build(projects)
    assert first == second
    assert len(first.catalog_id) == 64
    assert first.transaction_id == (
        contract.LEGACY_CATALOG_TRANSACTION_PREFIX + first.catalog_id
    )
    assert first.import_id == IMPORT_ID
    assert (
        first.project_count,
        first.entity_count,
        first.source_head_bytes,
        first.row_snapshot_bytes,
    ) == (2, 6, 42, 100)
    assert all(
        type(value) is bytes and len(value) == 32
        for value in (
            first.manifest_sha256,
            first.source_catalog_sha256,
            first.target_coverage_sha256,
            first.seal_sha256,
        )
    )

    manifest = json.loads(first.manifest_bytes)
    assert manifest == {
        "schema": contract.LEGACY_CATALOG_MANIFEST_SCHEMA,
        "protocol": contract.LEGACY_CATALOG_SEAL_PROTOCOL,
        "receipt_protocol": contract.LEGACY_CATALOG_RECEIPT_PROTOCOL,
        "receipt_kind": contract.LEGACY_CATALOG_TRANSACTION_KIND,
        "catalog_id": first.catalog_id,
        "import_id": IMPORT_ID,
        "status": contract.LEGACY_CATALOG_STATUS,
        "selection": contract.LEGACY_CATALOG_SELECTION,
        "cutover_ready": 0,
        "target_schema_version": contract.LEGACY_CATALOG_TARGET_SCHEMA_VERSION,
        "schema_fingerprint": SCHEMA_FINGERPRINT,
        "project_count": 2,
        "entity_count": 6,
        "source_head_bytes": 42,
        "row_snapshot_bytes": 100,
        "source_catalog_sha256": first.source_catalog_sha256.hex(),
        "target_coverage_sha256": first.target_coverage_sha256.hex(),
        "projects": [
            {
                "ordinal": ordinal,
                "project_id": project.project_id,
                "generation": project.generation,
                "document_id": project.document_id,
                "document_version": project.document_version,
                "storage_version": project.storage_version,
                "source_head_sha256": project.source_head_sha256.hex(),
                "source_head_bytes": project.source_head_bytes,
                "business_root_sha256": project.business_root_sha256.hex(),
                "entity_count": project.entity_count,
                "row_snapshot_bytes": project.row_snapshot_bytes,
            }
            for ordinal, project in enumerate(projects)
        ],
    }


def test_empty_catalog_has_one_deterministic_non_cutover_identity() -> None:
    empty = _build((), import_id=None)
    assert empty == _build((), import_id=None)
    assert empty.import_id is None
    assert (
        empty.project_count,
        empty.entity_count,
        empty.source_head_bytes,
        empty.row_snapshot_bytes,
    ) == (0, 0, 0, 0)
    manifest = json.loads(empty.manifest_bytes)
    assert manifest["projects"] == []
    assert manifest["import_id"] is None
    assert manifest["cutover_ready"] == 0
    assert manifest["status"] == "historical_fenced_snapshot"


def test_source_target_and_schema_domains_change_only_from_bound_inputs() -> None:
    base_project = _project()
    base = _build((base_project,))
    source_changed = _build(
        (base_project._replace(source_head_sha256=b"x" * 32),)
    )
    assert source_changed.source_catalog_sha256 != base.source_catalog_sha256
    assert source_changed.target_coverage_sha256 != base.target_coverage_sha256
    assert source_changed.catalog_id != base.catalog_id
    assert source_changed.seal_sha256 != base.seal_sha256

    import_changed = _build((base_project,), import_id="c" * 64)
    assert import_changed.source_catalog_sha256 == base.source_catalog_sha256
    assert import_changed.target_coverage_sha256 != base.target_coverage_sha256
    assert import_changed.catalog_id != base.catalog_id

    schema_changed = _build((base_project,), schema_fingerprint="d" * 64)
    assert schema_changed.source_catalog_sha256 == base.source_catalog_sha256
    assert schema_changed.target_coverage_sha256 != base.target_coverage_sha256
    assert schema_changed.catalog_id != base.catalog_id


def test_import_id_is_stable_and_binds_the_complete_source_observation() -> None:
    projects = (_project("alpha"), _project("zeta"))
    derived = integrity.derive_legacy_catalog_import_id(projects)
    assert derived == integrity.derive_legacy_catalog_import_id(projects)
    assert len(derived) == 64
    assert set(derived).issubset(set("0123456789abcdef"))
    assert integrity.derive_legacy_catalog_import_id(
        (projects[0]._replace(document_version=8), projects[1])
    ) != derived
    assert integrity.derive_legacy_catalog_import_id(
        (projects[0]._replace(business_root_sha256=b"x" * 32), projects[1])
    ) != derived
    with pytest.raises(integrity.LegacyCatalogIntegrityError) as empty:
        integrity.derive_legacy_catalog_import_id(())
    assert empty.value.code == "projects_invalid"


@pytest.mark.parametrize(
    "projects",
    (
        (_project("zeta"), _project("alpha")),
        (_project("alpha"), _project("alpha")),
        (
            _project("alpha", document_id="same-document"),
            _project("zeta", document_id="same-document"),
        ),
    ),
)
def test_catalog_requires_canonical_unique_live_descriptors(
    projects: tuple[integrity.LegacyCatalogIntegrityProject, ...],
) -> None:
    with pytest.raises(integrity.LegacyCatalogIntegrityError) as captured:
        _build(projects)
    assert captured.value.code == "project_order_invalid"


@pytest.mark.parametrize(
    ("import_id", "projects", "fingerprint", "code"),
    (
        (None, (_project(),), SCHEMA_FINGERPRINT, "import_id_invalid"),
        (IMPORT_ID, (), SCHEMA_FINGERPRINT, "import_id_invalid"),
        ("A" * 64, (_project(),), SCHEMA_FINGERPRINT, "import_id_invalid"),
        (IMPORT_ID, (_project(),), "B" * 64, "schema_fingerprint_invalid"),
        (IMPORT_ID, [_project()], SCHEMA_FINGERPRINT, "projects_invalid"),
    ),
)
def test_catalog_container_and_identity_types_are_exact(
    import_id: Any,
    projects: Any,
    fingerprint: Any,
    code: str,
) -> None:
    with pytest.raises(integrity.LegacyCatalogIntegrityError) as captured:
        integrity.build_legacy_catalog_integrity(
            import_id,
            projects,
            schema_fingerprint=fingerprint,
        )
    assert captured.value.code == code


@pytest.mark.parametrize(
    "project",
    (
        _project()._replace(generation=True),
        _project()._replace(document_version=0),
        _project()._replace(storage_version="future"),
        _project()._replace(source_head_sha256=bytearray(b"h" * 32)),
        _project()._replace(source_head_bytes=0),
        _project()._replace(business_root_sha256=b"short"),
        _project()._replace(entity_count=-1),
        _project()._replace(row_snapshot_bytes=0),
        _project()._replace(project_id="bad\ud800"),
    ),
)
def test_project_descriptor_shape_is_exact(project: Any) -> None:
    with pytest.raises(integrity.LegacyCatalogIntegrityError) as captured:
        _build((project,))
    assert captured.value.code == "project_invalid"


def test_one_source_head_cannot_borrow_the_catalog_aggregate_budget() -> None:
    oversized = _project()._replace(
        source_head_bytes=(
            contract.MAX_LEGACY_CATALOG_PROJECT_SOURCE_HEAD_BYTES + 1
        )
    )
    with pytest.raises(integrity.LegacyCatalogIntegrityError) as captured:
        _build((oversized,))
    assert captured.value.code == "project_invalid"


def test_aggregate_budgets_fail_before_a_seal_is_encoded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _project("alpha", source_head_bytes=3, row_snapshot_bytes=3)
    second = _project("zeta", source_head_bytes=3, row_snapshot_bytes=3)

    monkeypatch.setattr(integrity, "MAX_LEGACY_CATALOG_SOURCE_HEAD_BYTES", 5)
    with pytest.raises(integrity.LegacyCatalogIntegrityError) as source_error:
        _build((first, second))
    assert source_error.value.code == "source_head_bytes_exceeded"

    monkeypatch.setattr(integrity, "MAX_LEGACY_CATALOG_SOURCE_HEAD_BYTES", 10)
    monkeypatch.setattr(integrity, "MAX_LEGACY_CATALOG_TARGET_SNAPSHOT_BYTES", 5)
    with pytest.raises(integrity.LegacyCatalogIntegrityError) as row_error:
        _build((first, second))
    assert row_error.value.code == "row_snapshot_bytes_exceeded"

    monkeypatch.setattr(integrity, "MAX_LEGACY_CATALOG_TARGET_SNAPSHOT_BYTES", 10)
    monkeypatch.setattr(integrity, "MAX_LEGACY_ENTITIES", 3)
    first = first._replace(entity_count=2)
    second = second._replace(entity_count=2)
    with pytest.raises(integrity.LegacyCatalogIntegrityError) as entity_error:
        _build((first, second))
    assert entity_error.value.code == "entity_count_exceeded"


def test_hash_vectors_are_stable_protocol_evidence() -> None:
    sealed = _build((_project(),))
    assert sealed.catalog_id == (
        "e861d169c0d0f0d69f0ab5c09c0c9a0c"
        "b3945c94101045c0d9faba0018afd27c"
    )
    assert sealed.manifest_sha256.hex() == (
        "f87d03a70f3ebb7370c5bcdbded52206d"
        "2cafc427fc14c918734b1a943b0d9b6"
    )
    assert sealed.source_catalog_sha256.hex() == (
        "0c7552eb2ae0bbd1dfd0150358be49993"
        "6325ef3530ca1bb7503dcc49c98db00"
    )
    assert sealed.target_coverage_sha256.hex() == (
        "e2e2d12f2a7ea32d6b18969e49d1de3a"
        "df45c678df46431743061ebbd1f492bc"
    )
    assert sealed.seal_sha256.hex() == (
        "674af8d10419c2b17df3d27f887d794b"
        "7ebecfa61ed1f0500c5a6dcf73f58dfa"
    )


def test_integrity_leaf_has_no_sdk_filesystem_or_sqlite_dependency() -> None:
    source = (_HERE / "legacy_catalog_integrity.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported.isdisjoint(
        {"os", "pathlib", "sqlite3", "sdk", "project_topology", "legacy_import"}
    )
