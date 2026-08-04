"""Direct issuer and tamper tests for the V12 provenance reader."""

from __future__ import annotations

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


from business_cutover_install_closure import (
    BUSINESS_CUTOVER_BASELINE_PROJECTION_ARTIFACT,
    BUSINESS_CUTOVER_MEMBER_MAPPING_ARTIFACT,
    INSTALL_CLOSURE_COLUMNS,
)
from sqlite_cutover_install_closure_reader import _artifact, _mapping
from sqlite_cutover_install_provenance_reader import (
    SQLiteCutoverInstallProvenanceBinding,
    SQLiteCutoverInstallProvenanceError,
    _read_cutover_install_provenance,
    _require_cutover_install_provenance_binding,
)
from test_business_cutover_install_closure import COMMITTED_AT
from test_sqlite_cutover_install_closure_reader import (
    _drop_premount_guards,
    _live_stored_closure,
)


def _read_direct(connection, plan):
    mapping_header, _mapping_bytes, mapping_value = _artifact(
        connection,
        cutover_id=plan.cutover_id,
        artifact_kind=BUSINESS_CUTOVER_MEMBER_MAPPING_ARTIFACT,
    )
    _baseline_header, _baseline_bytes, baseline_value = _artifact(
        connection,
        cutover_id=plan.cutover_id,
        artifact_kind=BUSINESS_CUTOVER_BASELINE_PROJECTION_ARTIFACT,
    )
    members, _counts = _mapping(mapping_value, plan.cutover_id)
    row = connection.execute(
        "SELECT "
        + ",".join(INSTALL_CLOSURE_COLUMNS)
        + " FROM business_cutover_install_closures WHERE cutover_id=?",
        (plan.cutover_id,),
    ).fetchone()
    closure = dict(zip(INSTALL_CLOSURE_COLUMNS, tuple(row)))
    return _read_cutover_install_provenance(
        connection,
        cutover_id=plan.cutover_id,
        mapping=members,
        baseline=baseline_value,
        member_mapping_sha256=mapping_header["artifact_sha256"],
        install_transaction_id=plan.install.transaction_id,
        install_manifest_sha256=plan.install.manifest_sha256,
        install_committed_at=COMMITTED_AT,
        target_schema_version=plan.expected_target_schema_version,
        target_schema_fingerprint=plan.expected_target_schema_fingerprint,
        closure=closure,
    )


def test_direct_reader_issues_only_recomputed_provenance() -> None:
    connection, plan, projected = _live_stored_closure()
    try:
        binding = _require_cutover_install_provenance_binding(
            _read_direct(connection, plan)
        )
        assert binding.source_proof_sha256 == plan.legacy_proof.proof_sha256
        assert binding.source_admission_sha256 == (plan.closure.source_admission_sha256)
        assert binding.coverage_manifest_sha256 == (
            projected._coverage_plan.manifest_sha256
        )
        assert binding.coverage_sha256 == (
            projected._coverage_plan.coverage.coverage_sha256
        )
        forged = object.__new__(SQLiteCutoverInstallProvenanceBinding)
        with pytest.raises(SQLiteCutoverInstallProvenanceError):
            _require_cutover_install_provenance_binding(forged)
    finally:
        connection.close()


@pytest.mark.parametrize("tamper", ("source", "coverage", "source_time"))
def test_direct_reader_rejects_reachable_provenance_tamper(tamper: str) -> None:
    connection, plan, _projected = _live_stored_closure()
    try:
        _drop_premount_guards(connection)
        if tamper == "source":
            connection.execute("DROP TRIGGER legacy_repository_seals_no_update")
            connection.execute(
                "UPDATE legacy_repository_seals "
                "SET source_state_bytes=source_state_bytes+1"
            )
        elif tamper == "coverage":
            changed = b"z" * 32
            connection.execute(
                "DROP TRIGGER business_asset_coverage_receipts_no_update"
            )
            connection.execute(
                "DROP TRIGGER business_cutover_install_closures_no_update"
            )
            connection.execute(
                "UPDATE business_asset_coverage_receipts SET coverage_sha256=?",
                (changed,),
            )
            connection.execute(
                "UPDATE business_cutover_install_closures SET coverage_sha256=?",
                (changed,),
            )
        else:
            triggers = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' "
                "AND tbl_name='transaction_receipts'"
            ).fetchall()
            for trigger in triggers:
                connection.execute("DROP TRIGGER " + trigger[0])
            connection.execute(
                "UPDATE transaction_receipts SET committed_at=? "
                "WHERE transaction_id=?",
                ("0" * 24, plan.legacy_proof.transaction_id),
            )
        with pytest.raises(SQLiteCutoverInstallProvenanceError):
            _read_direct(connection, plan)
    finally:
        connection.close()
