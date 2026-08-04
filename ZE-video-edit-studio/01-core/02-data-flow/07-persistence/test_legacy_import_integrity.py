"""Golden and adversarial tests for pure legacy-import byte integrity."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from legacy_import_integrity import (  # noqa: E402
    LegacyImportIntegrityError,
    LegacyImportIntegrityProject,
    build_legacy_import_integrity,
)
import legacy_import_integrity as integrity_module  # noqa: E402


IMPORT_ID = "01" * 32


def _project(**changes) -> LegacyImportIntegrityProject:
    values = {
        "project_id": "alpha",
        "generation": 2,
        "document_id": "doc-alpha",
        "document_version": 7,
        "storage_version": "legacy",
        "source_head_sha256": bytes.fromhex("11" * 32),
        "source_head_bytes": 123,
        "business_root_sha256": bytes.fromhex("22" * 32),
        "entity_count": 1,
        "row_snapshot_bytes": 9,
        "project_snapshot_bytes": b"project",
        "document_snapshot_bytes": b"d",
        "entity_snapshot_bytes": (b"e",),
    }
    values.update(changes)
    return LegacyImportIntegrityProject(**values)


def test_integrity_has_one_exact_manifest_and_three_digest_vectors() -> None:
    integrity = build_legacy_import_integrity(IMPORT_ID, (_project(),))

    assert integrity.entity_count == 1
    assert integrity.row_snapshot_bytes == 9
    assert integrity.framed_payload_bytes == 100
    assert integrity.manifest_bytes == (
        b'{"entity_count":1,"framed_payload_bytes":100,'
        b'"import_id":"0101010101010101010101010101010101010101010101010101010101010101",'
        b'"payload_protocol":"ze.legacy-import.payload.v1",'
        b'"payload_sha256":"7fedf71de4b6ae48e36899057b1b522f46cccd575c6ba33586368b2fa47f7c38",'
        b'"project_count":1,"projects":['
        b'{"business_root_sha256":"2222222222222222222222222222222222222222222222222222222222222222",'
        b'"document_id":"doc-alpha","document_version":7,"entity_count":1,'
        b'"generation":2,"project_id":"alpha","row_snapshot_bytes":9,'
        b'"source_head_bytes":123,'
        b'"source_head_sha256":"1111111111111111111111111111111111111111111111111111111111111111",'
        b'"storage_version":"legacy"}],'
        b'"protocol":"ze.legacy-import.batch.v1","row_snapshot_bytes":9,'
        b'"schema":"ze.legacy-import.manifest.v1",'
        b'"selection":"explicit_projects"}'
    )
    assert integrity.payload_sha256.hex() == (
        "7fedf71de4b6ae48e36899057b1b522f46cccd575c6ba33586368b2fa47f7c38"
    )
    assert integrity.manifest_sha256.hex() == (
        "f07ac050c4012f4ebfde211dbf410b2519fd3cd28e70122eb57c1e221b3f049c"
    )
    assert integrity.batch_sha256.hex() == (
        "306710f3aede13322df3d832174912e8e844789316a6bdb5e436a0b094130ca3"
    )


@pytest.mark.parametrize(
    ("projects", "code"),
    [
        ((), "projects_invalid"),
        ((_project(row_snapshot_bytes=8),), "project_row_snapshot_bytes_mismatch"),
        ((_project(entity_snapshot_bytes=(bytearray(b"e"),)),), "project_invalid"),
        ((_project(generation=True),), "project_invalid"),
    ],
)
def test_integrity_rejects_non_exact_or_inconsistent_inputs(
    projects,
    code: str,
) -> None:
    with pytest.raises(LegacyImportIntegrityError) as raised:
        build_legacy_import_integrity(IMPORT_ID, projects)
    assert raised.value.code == code
    assert raised.value.args == (code,)


def test_declared_count_plus_one_stops_an_unbounded_iterable() -> None:
    consumed = []

    def payloads():
        consumed.append(1)
        yield b"e"
        consumed.append(2)
        yield b"never-hashed"
        pytest.fail("integrity reader consumed beyond count + 1 sentinel")

    with pytest.raises(LegacyImportIntegrityError) as raised:
        build_legacy_import_integrity(
            IMPORT_ID,
            (_project(entity_snapshot_bytes=payloads()),),
        )
    assert raised.value.code == "project_entity_count_mismatch"
    assert consumed == [1, 2]


def test_batch_budget_rejects_next_project_before_touching_its_iterable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    touched = []

    def forbidden_payloads():
        touched.append(True)
        yield b"e"

    monkeypatch.setattr(integrity_module, "MAX_LEGACY_ENTITIES", 1)
    second = _project(
        project_id="beta",
        document_id="doc-beta",
        entity_snapshot_bytes=forbidden_payloads(),
    )
    with pytest.raises(LegacyImportIntegrityError) as raised:
        build_legacy_import_integrity(IMPORT_ID, (_project(), second))
    assert raised.value.code == "entity_count_exceeded"
    assert touched == []
