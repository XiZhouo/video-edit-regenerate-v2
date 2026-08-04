"""Fresh-row and tamper probes for the private V13 semantic reader."""

from __future__ import annotations

import sqlite3
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

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

import sqlite_production_test_support as production_support
from business_cutover_phase_binding_contract import (
    BusinessCutoverPhaseBindingStage,
)
from business_cutover_phase_binding_integrity import (
    issue_business_cutover_phase_binding,
)
from sqlite_cutover_phase_binding_reader import (
    SQLiteCutoverPhaseBindingReaderError,
    _read_cutover_phase_binding_prefix,
    _require_cutover_phase_binding_prefix,
)


def _database(tmp_path: Path) -> Path:
    path = tmp_path / "phase-binding-reader.sqlite3"
    production_support.support.create_active_business_database(path)
    return path


def _read(path: Path) -> Any:
    connection = sqlite3.connect(path)
    try:
        connection.execute("BEGIN")
        return _read_cutover_phase_binding_prefix(connection)
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()


def _drop_trigger(connection: Any, name: str) -> None:
    actual = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND name=?",
        (name,),
    ).fetchone()
    assert actual == (name,)
    connection.execute('DROP TRIGGER "' + name + '"')


def test_reader_reissues_exact_canonical_mounted_prefix(tmp_path: Path) -> None:
    prefix = _require_cutover_phase_binding_prefix(_read(_database(tmp_path)))

    assert prefix.current_head.phase_revision == 5
    assert prefix.current_head.phase == "mounted"
    assert tuple(binding.projection.stage for binding in prefix.bindings) == (
        BusinessCutoverPhaseBindingStage.ASSET_PROVIDER,
        BusinessCutoverPhaseBindingStage.AUDIT,
        BusinessCutoverPhaseBindingStage.ACTIVATION,
        BusinessCutoverPhaseBindingStage.MOUNT,
    )
    assert (
        prefix.bindings[-1].projection.new_head
        == prefix.current_head
    )
    assert (
        prefix.bindings[2].projection.direct_evidence.audit_binding
        == prefix.bindings[1].projection.direct_evidence
    )
    assert (
        prefix.bindings[3].projection.direct_evidence.activation_binding
        == prefix.bindings[2].projection.direct_evidence
    )


def test_reader_requires_one_caller_owned_read_transaction(
    tmp_path: Path,
) -> None:
    connection = sqlite3.connect(_database(tmp_path))
    try:
        with pytest.raises(
            SQLiteCutoverPhaseBindingReaderError,
            match="cutover_phase_binding_read_transaction_required",
        ):
            _read_cutover_phase_binding_prefix(connection)
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("column", "expression"),
    (
        (
            "binding_manifest_bytes",
            "CAST(binding_manifest_bytes || X'00' AS BLOB)",
        ),
        ("runtime_repository_revision", "runtime_repository_revision + 1"),
        ("runtime_authority", "'evil'"),
    ),
)
def test_reader_rejects_manifest_or_checkpoint_tamper(
    tmp_path: Path,
    column: str,
    expression: str,
) -> None:
    path = _database(tmp_path)
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA ignore_check_constraints=ON")
        _drop_trigger(
            connection,
            "business_cutover_asset_provider_bindings_no_update",
        )
        connection.execute(
            "UPDATE business_cutover_asset_provider_bindings SET "
            + column + "=" + expression
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(SQLiteCutoverPhaseBindingReaderError):
        _read(path)


def test_reader_rejects_resigned_cross_stage_checkpoint_fork(
    tmp_path: Path,
) -> None:
    path = _database(tmp_path)
    prefix = _read(path)
    audit = prefix.bindings[1]
    forged_projection = replace(
        audit.projection,
        admission=replace(
            audit.projection.admission,
            source_freeze_sha256=b"\x99" * 32,
        ),
    )
    forged = issue_business_cutover_phase_binding(forged_projection)

    connection = sqlite3.connect(path)
    try:
        _drop_trigger(connection, "business_cutover_audit_bindings_no_update")
        connection.execute(
            "UPDATE business_cutover_audit_bindings SET "
            "source_freeze_sha256=?,binding_manifest_bytes=?,"
            "binding_manifest_sha256=?",
            (
                forged_projection.admission.source_freeze_sha256,
                forged.manifest_bytes,
                forged.manifest_sha256,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(
        SQLiteCutoverPhaseBindingReaderError,
        match="cutover_phase_binding_semantic_verification_failed",
    ):
        _read(path)


def test_reader_rejects_durable_direct_evidence_tamper(tmp_path: Path) -> None:
    path = _database(tmp_path)
    connection = sqlite3.connect(path)
    try:
        _drop_trigger(connection, "business_asset_coverage_receipts_no_update")
        connection.execute(
            "UPDATE business_asset_coverage_receipts "
            "SET member_root_sha256=zeroblob(32)"
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(
        SQLiteCutoverPhaseBindingReaderError,
        match="cutover_phase_binding_coverage_source_invalid",
    ):
        _read(path)


def test_reader_rejects_missing_binding_row(tmp_path: Path) -> None:
    path = _database(tmp_path)
    connection = sqlite3.connect(path)
    try:
        _drop_trigger(connection, "business_cutover_mount_bindings_no_delete")
        connection.execute("DELETE FROM business_cutover_mount_bindings")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(
        SQLiteCutoverPhaseBindingReaderError,
        match="cutover_phase_binding_prefix_shape_invalid",
    ):
        _read(path)


def test_reader_rejects_rows_beyond_current_head_prefix(tmp_path: Path) -> None:
    path = _database(tmp_path)
    connection = sqlite3.connect(path)
    try:
        trigger_names = tuple(
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND "
                "tbl_name='business_cutover_head' AND sql LIKE '%UPDATE%'"
            ).fetchall()
        )
        assert trigger_names
        for name in trigger_names:
            _drop_trigger(connection, name)
        successor = connection.execute(
            "SELECT successor_state_revision,successor_phase_revision,"
            "successor_phase,successor_authority_state,"
            "successor_previous_head_sha256,successor_head_sha256,"
            "successor_updated_at FROM business_cutover_audit_bindings"
        ).fetchone()
        assert successor is not None and len(successor) == 7
        connection.execute(
            "UPDATE business_cutover_head SET state_revision=?,"
            "phase_revision=?,phase=?,authority_state=?,"
            "activation_transaction_id=NULL,activation_manifest_sha256=NULL,"
            "mount_transaction_id=NULL,mount_manifest_sha256=NULL,"
            "previous_head_sha256=?,head_sha256=?,updated_at=? WHERE singleton=1",
            tuple(successor),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(
        SQLiteCutoverPhaseBindingReaderError,
        match="cutover_phase_binding_prefix_shape_invalid",
    ):
        _read(path)
