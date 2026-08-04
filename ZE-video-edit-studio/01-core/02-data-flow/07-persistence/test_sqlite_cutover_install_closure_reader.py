"""Stored-row and tamper evidence for the V12 install-closure reader."""

from __future__ import annotations

import copy
import sqlite3
import sys
from pathlib import Path
from typing import Any, Iterable

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

from business_cutover_baseline_projection import (
    ASSET_BLOBS_COLUMNS,
    BUSINESS_COMMITS_COLUMNS,
    DOCUMENT_ASSET_REFS_COLUMNS,
    DOCUMENT_COMMIT_HEADS_COLUMNS,
    DOCUMENTS_COLUMNS,
    ENTITIES_COLUMNS,
    OUTBOX_DISPATCH_HEAD_COLUMNS,
    PROJECTS_COLUMNS,
    REPOSITORY_PROJECTS_COLUMNS,
    TRANSACTION_RECEIPTS_COLUMNS,
    WORKER_LEDGER_HEAD_COLUMNS,
)
from business_cutover_install_closure import (
    ATOMIC_BUSINESS_WRITE_RECEIPT_COLUMNS,
    BASELINE_ATOMIC_RECEIPT_COLUMNS,
    INSTALL_ARTIFACT_CHUNK_COLUMNS,
    INSTALL_ARTIFACT_COLUMNS,
    INSTALL_CLOSURE_COLUMNS,
)
from business_cutover_asset_integrity import (
    _issue_business_cutover_asset_stage_members,
    _repository_member_root,
)
from legacy_repository_contract import (
    LEGACY_REPOSITORY_AUTHORITY,
    LEGACY_REPOSITORY_AUTHORITY_STATE,
    LEGACY_REPOSITORY_CUTOVER_READY,
    LEGACY_REPOSITORY_MANIFEST_SCHEMA,
    LEGACY_REPOSITORY_PROOF_PROTOCOL,
    LEGACY_REPOSITORY_RECEIPT_PROTOCOL,
    LEGACY_REPOSITORY_SELECTION,
    LEGACY_REPOSITORY_STATUS,
    LEGACY_REPOSITORY_TARGET_SCHEMA_VERSION,
    LEGACY_REPOSITORY_TRANSACTION_KIND,
)
from business_codec import decode_payload_bounded
from sqlite_cutover_install_closure_reader import (
    SQLiteCutoverInstallClosureError,
    _artifact,
    _baseline,
    _mapping,
    _read_business_cutover_install_closure,
    _require_cutover_install_closure_binding,
    _runtime,
)
from sqlite_cutover_install_provenance_reader import (
    _COVERAGE_COLUMNS,
    _SOURCE_CONTROL_COLUMNS,
    _SOURCE_MEMBER_COLUMNS,
    _SOURCE_SEAL_COLUMNS,
    _STAGE_COLUMNS,
    _STAGE_MEMBER_COLUMNS,
)
import test_business_cutover_asset_integrity as asset_support
from test_business_cutover_baseline_projection import _snapshot
from test_business_cutover_install_closure import COMMITTED_AT, _projection
from test_business_cutover_integrity import _deleted, _live
from test_sqlite_schema_v12_install_closure import _connection
from sqlite_production_test_support import create_canonical_mounted_business_database


def _insert_values(
    connection: Any,
    table: str,
    columns: Iterable[str],
    values: tuple[Any, ...],
) -> None:
    exact_columns = tuple(columns)
    connection.execute(
        "INSERT INTO " + table + "(" + ",".join(exact_columns) + ") "
        "VALUES (" + ",".join("?" for _column in exact_columns) + ")",
        values,
    )


def _insert_provenance(connection: Any, plan: Any, coverage: Any) -> None:
    integrity = plan._legacy_integrity
    connection.execute(
        "INSERT INTO transaction_receipts(transaction_id,kind,committed_at) "
        "VALUES (?,?,?)",
        (
            integrity.transaction_id,
            LEGACY_REPOSITORY_TRANSACTION_KIND,
            COMMITTED_AT,
        ),
    )
    _insert_values(
        connection,
        "legacy_repository_proof_control",
        _SOURCE_CONTROL_COLUMNS,
        (
            1,
            LEGACY_REPOSITORY_AUTHORITY,
            LEGACY_REPOSITORY_AUTHORITY_STATE,
            integrity.repository_epoch,
            integrity.repository_revision,
            integrity.catalog_revision,
            integrity.cutover_id,
            integrity.runtime_generation,
            integrity.code_digest,
            LEGACY_REPOSITORY_CUTOVER_READY,
        ),
    )
    _insert_values(
        connection,
        "legacy_repository_seals",
        _SOURCE_SEAL_COLUMNS,
        (
            1,
            integrity.cutover_id,
            integrity.transaction_id,
            LEGACY_REPOSITORY_TRANSACTION_KIND,
            LEGACY_REPOSITORY_RECEIPT_PROTOCOL,
            integrity.catalog_id,
            plan._catalog_seal_sha256,
            LEGACY_REPOSITORY_PROOF_PROTOCOL,
            LEGACY_REPOSITORY_MANIFEST_SCHEMA,
            LEGACY_REPOSITORY_STATUS,
            LEGACY_REPOSITORY_SELECTION,
            integrity.repository_epoch,
            integrity.repository_revision,
            integrity.catalog_revision,
            integrity.runtime_generation,
            integrity.code_digest,
            integrity.project_count,
            integrity.live_project_count,
            integrity.deleted_project_count,
            integrity.source_state_bytes,
            integrity.manifest_bytes,
            integrity.manifest_sha256,
            integrity.source_repository_sha256,
            integrity.catalog_coverage_sha256,
            integrity.proof_sha256,
            LEGACY_REPOSITORY_TARGET_SCHEMA_VERSION,
            plan._legacy_schema_fingerprint,
            LEGACY_REPOSITORY_CUTOVER_READY,
        ),
    )
    for ordinal, member in enumerate(plan._legacy_members):
        _insert_values(
            connection,
            "legacy_repository_members",
            _SOURCE_MEMBER_COLUMNS,
            (
                integrity.cutover_id,
                ordinal,
                member.project_id,
                member.generation,
                member.status,
                member.operation_id,
                member.source_state_sha256,
                member.source_state_bytes,
                integrity.catalog_id if member.status == "live" else None,
                member.catalog_ordinal,
                member.import_id,
                member.document_id,
                member.document_version,
                member.storage_version,
                member.business_root_sha256,
                member.entity_count,
                member.row_snapshot_bytes,
            ),
        )
    for stage in coverage.batch_evidences:
        connection.execute(
            "INSERT INTO transaction_receipts(transaction_id,kind,committed_at) "
            "VALUES (?,?,?)",
            (stage.transaction_id, stage.receipt_kind, COMMITTED_AT),
        )
        stage_members = _issue_business_cutover_asset_stage_members(
            projects=stage.projects,
            blobs=stage.blobs,
            occurrences=stage.occurrences,
        )
        for member in stage_members:
            _insert_values(
                connection,
                "business_asset_stage_members",
                _STAGE_MEMBER_COLUMNS,
                (
                    stage.batch_id,
                    stage.cutover_id,
                    member.member_kind,
                    member.ordinal,
                    member.member_id,
                    member.member_bytes,
                    member.member_sha256,
                    member.metadata_sha256,
                ),
            )
        _insert_values(
            connection,
            "business_asset_stage_receipts",
            _STAGE_COLUMNS,
            (
                stage.batch_id,
                stage.cutover_id,
                stage.transaction_id,
                stage.receipt_kind,
                COMMITTED_AT,
                stage.repository_epoch,
                stage.source_admission_revision,
                stage.source_admission_sha256,
                len(stage_members),
                stage.project_count,
                stage.unique_blob_count,
                stage.reference_count,
                stage.unique_blob_bytes,
                stage.referenced_bytes,
                stage.project_root_sha256,
                stage.blob_root_sha256,
                stage.occurrence_root_sha256,
                stage.coverage_sha256,
                stage.manifest_bytes,
                stage.manifest_sha256,
            ),
        )
    evidence = coverage.expected_evidence
    connection.execute(
        "INSERT INTO transaction_receipts(transaction_id,kind,committed_at) "
        "VALUES (?,?,?)",
        (evidence.transaction_id, evidence.receipt_kind, COMMITTED_AT),
    )
    _insert_values(
        connection,
        "business_asset_coverage_receipts",
        _COVERAGE_COLUMNS,
        (
            evidence.cutover_id,
            evidence.transaction_id,
            evidence.receipt_kind,
            COMMITTED_AT,
            evidence.repository_epoch,
            evidence.source_admission_revision,
            evidence.source_admission_sha256,
            evidence.batch_count,
            _repository_member_root(coverage._cutover_plan),
            integrity.project_count,
            integrity.live_project_count,
            integrity.deleted_project_count,
            evidence.observed_occurrence_count,
            evidence.observed_row_count,
            evidence.observed_manifest_bytes,
            evidence.project_count,
            evidence.reference_count,
            evidence.unique_blob_count,
            evidence.unique_blob_bytes,
            evidence.referenced_bytes,
            evidence.project_root_sha256,
            evidence.blob_root_sha256,
            evidence.occurrence_root_sha256,
            evidence.coverage_sha256,
            evidence.manifest_bytes,
            evidence.manifest_sha256,
        ),
    )


def _drop_premount_guards(connection: Any) -> None:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' "
        "AND sql LIKE '%production_cutover_premount_namespace_frozen%'"
    ).fetchall()
    for row in rows:
        connection.execute("DROP TRIGGER " + row[0])


def _live_stored_closure(*, members=None, snapshots=None):
    plan, baseline, projected = _projection(
        members=members,
        snapshots=snapshots,
    )
    connection = _connection()
    # These focused reader fixtures deliberately start from a synthetic V5
    # proof without recreating the unrelated V3/V4 catalog-import ancestry.
    # Triggers remain active; only those out-of-scope parent FKs are disabled.
    connection.execute("PRAGMA foreign_keys=OFF")
    install = plan.install
    runtime = install.runtime
    _insert_provenance(connection, plan, projected._coverage_plan)
    connection.execute(
        "INSERT INTO transaction_receipts(transaction_id,kind,committed_at) "
        "VALUES (?,?,?)",
        (install.transaction_id, install.receipt_kind, COMMITTED_AT),
    )
    connection.execute(
        "INSERT INTO business_cutover_install_receipts("
        "cutover_id,transaction_id,receipt_kind,committed_at,repository_epoch,"
        "runtime_generation,code_digest,member_mapping_sha256,manifest_bytes,"
        "manifest_sha256) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            plan.cutover_id,
            install.transaction_id,
            install.receipt_kind,
            COMMITTED_AT,
            runtime.repository_epoch,
            runtime.runtime_generation,
            runtime.code_digest,
            install.member_mapping_sha256,
            install.manifest_bytes,
            install.manifest_sha256,
        ),
    )
    connection.execute(
        "UPDATE kernel_repository SET authority='sqlite' WHERE singleton=1"
    )
    connection.execute(
        "INSERT INTO repository_runtime_control("
        "singleton,authority,authority_state,repository_epoch,cutover_id,"
        "source_admission_revision,repository_revision,state_revision,"
        "runtime_generation,code_digest,schema_version,updated_at) "
        "VALUES (1,'sqlite','read_only',?,?,?,?,?,?,?,6,?)",
        (
            runtime.repository_epoch,
            plan.cutover_id,
            runtime.source_admission_revision,
            baseline.runtime_repository_revision,
            runtime.state_revision,
            runtime.runtime_generation,
            runtime.code_digest,
            COMMITTED_AT,
        ),
    )
    _insert_values(
        connection,
        "worker_ledger_head",
        WORKER_LEDGER_HEAD_COLUMNS,
        baseline.scheduler_genesis.worker_ledger.sql_values(),
    )
    _insert_values(
        connection,
        "outbox_allocation_head",
        WORKER_LEDGER_HEAD_COLUMNS,
        baseline.scheduler_genesis.outbox_allocation.sql_values(),
    )
    _insert_values(
        connection,
        "outbox_dispatch_head",
        OUTBOX_DISPATCH_HEAD_COLUMNS,
        baseline.scheduler_genesis.outbox_dispatch.sql_values(),
    )
    for artifact in (projected.member_mapping, projected.baseline_projection):
        _insert_values(
            connection,
            "business_cutover_install_artifacts",
            INSTALL_ARTIFACT_COLUMNS,
            artifact.header.sql_values(),
        )
        for chunk in artifact.chunks:
            _insert_values(
                connection,
                "business_cutover_install_artifact_chunks",
                INSTALL_ARTIFACT_CHUNK_COLUMNS,
                chunk.sql_values(),
            )
    for asset_blob in baseline.asset_blobs:
        _insert_values(
            connection,
            "asset_blobs",
            ASSET_BLOBS_COLUMNS,
            asset_blob.sql_values(),
        )
    for live, atomic, sidecar in zip(
        baseline.live,
        projected.atomic_receipts,
        projected.baseline_receipts,
    ):
        _insert_values(
            connection,
            "projects",
            PROJECTS_COLUMNS,
            live.project.sql_values(),
        )
        _insert_values(
            connection,
            "documents",
            DOCUMENTS_COLUMNS,
            live.document.sql_values(),
        )
        for entity in live.entities:
            _insert_values(
                connection,
                "entities",
                ENTITIES_COLUMNS,
                entity.sql_values(),
            )
        _insert_values(
            connection,
            "transaction_receipts",
            TRANSACTION_RECEIPTS_COLUMNS,
            live.transaction_receipt.sql_values(),
        )
        _insert_values(
            connection,
            "business_commits",
            BUSINESS_COMMITS_COLUMNS,
            live.commit.sql_values(),
        )
        _insert_values(
            connection,
            "repository_projects",
            REPOSITORY_PROJECTS_COLUMNS,
            live.repository_project.sql_values(),
        )
        for reference in live.asset_references:
            _insert_values(
                connection,
                "document_asset_refs",
                DOCUMENT_ASSET_REFS_COLUMNS,
                reference.sql_values(),
            )
        _insert_values(
            connection,
            "document_commit_heads",
            DOCUMENT_COMMIT_HEADS_COLUMNS,
            live.document_head.sql_values(),
        )
        _insert_values(
            connection,
            "atomic_business_write_receipts",
            ATOMIC_BUSINESS_WRITE_RECEIPT_COLUMNS,
            atomic.sql_values(),
        )
        _insert_values(
            connection,
            "business_cutover_baseline_atomic_receipts",
            BASELINE_ATOMIC_RECEIPT_COLUMNS,
            sidecar.sql_values(),
        )
    for deleted in baseline.deleted:
        _insert_values(
            connection,
            "repository_projects",
            REPOSITORY_PROJECTS_COLUMNS,
            deleted.repository_project.sql_values(),
        )
    _insert_values(
        connection,
        "business_cutover_install_closures",
        INSTALL_CLOSURE_COLUMNS,
        projected.closure.sql_values(),
    )
    return connection, plan, projected


def _read(connection: Any, plan: Any):
    return _read_business_cutover_install_closure(
        connection,
        cutover_id=plan.cutover_id,
        install_transaction_id=plan.install.transaction_id,
        install_manifest_sha256=plan.install.manifest_sha256,
        install_committed_at=COMMITTED_AT,
        target_schema_version=plan.expected_target_schema_version,
        target_schema_fingerprint=plan.expected_target_schema_fingerprint,
    )


def test_live_v12_rows_rebuild_every_artifact_sidecar_and_root() -> None:
    connection, plan, projected = _live_stored_closure()
    try:
        binding = _require_cutover_install_closure_binding(_read(connection, plan))
        assert binding.cutover_id == plan.cutover_id
        assert binding.project_count == 1
        assert binding.live_project_count == 1
        assert binding.deleted_project_count == 0
        assert binding.baseline_commit_count == 1
        assert binding.closure_manifest_sha256 == (projected.closure.manifest_sha256)
    finally:
        connection.close()


def test_asset_v12_rows_close_reference_projection_and_blob_metadata() -> None:
    snapshot = _snapshot(
        references=(
            asset_support._ref(
                asset_support.SHA_A,
                size=len(asset_support.BLOB_A),
            ),
        )
    )
    connection, plan, _projected = _live_stored_closure(
        members=(_live(snapshot),),
        snapshots=(snapshot,),
    )
    try:
        binding = _read(connection, plan)
        assert binding.live_project_count == 1
        assert (
            connection.execute("SELECT count(*) FROM document_asset_refs").fetchone()[0]
            == 1
        )
        assert connection.execute("SELECT count(*) FROM asset_blobs").fetchone()[0] == 1
    finally:
        connection.close()


def test_active_mount_revalidates_immutable_install_history(tmp_path: Path) -> None:
    baseline = create_canonical_mounted_business_database(
        tmp_path / "active-install-history.sqlite3"
    )
    connection = sqlite3.connect(str(baseline.database.path))
    connection.row_factory = sqlite3.Row
    try:
        install = connection.execute(
            "SELECT cutover_id,transaction_id,committed_at,manifest_sha256 "
            "FROM business_cutover_install_receipts"
        ).fetchone()
        closure = connection.execute(
            "SELECT manifest_bytes FROM business_cutover_install_closures"
        ).fetchone()
        manifest = decode_payload_bounded(
            closure["manifest_bytes"],
            16 * 1024 * 1024,
            100_000,
        )
        binding = _read_business_cutover_install_closure(
            connection,
            cutover_id=install["cutover_id"],
            install_transaction_id=install["transaction_id"],
            install_manifest_sha256=install["manifest_sha256"],
            install_committed_at=install["committed_at"],
            target_schema_version=manifest["schema_target"]["version"],
            target_schema_fingerprint=manifest["schema_target"]["fingerprint"],
        )
        # The mounted-upgrade fixture carries an intentionally empty V12
        # install history on top of its older baseline project.  Active
        # verification audits that immutable history rather than reinterpreting
        # the current project table as the V12 install snapshot.
        assert binding.project_count == 0
        assert (
            connection.execute(
                "SELECT authority_state FROM repository_runtime_control"
            ).fetchone()[0]
            == "active"
        )
    finally:
        connection.close()


def test_active_but_unmounted_phase_still_requires_current_projection() -> None:
    connection, plan, _projected = _live_stored_closure()
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        _drop_premount_guards(connection)
        runtime_triggers = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND tbl_name='repository_runtime_control'"
        ).fetchall()
        for trigger in runtime_triggers:
            connection.execute("DROP TRIGGER " + trigger[0])
        connection.execute(
            "UPDATE repository_runtime_control "
            "SET authority_state='active',state_revision=2"
        )
        # Activation may make runtime active before mount.  Without a mounted
        # cutover head this remains the frozen install namespace, so current
        # rows must still equal the historical projection.
        connection.execute("UPDATE documents SET save_policy='legacy'")
        with pytest.raises(SQLiteCutoverInstallClosureError) as rejected:
            _read(connection, plan)
        assert rejected.value.code == (
            "cutover_install_closure_baseline_snapshot_invalid"
        )
    finally:
        connection.close()


@pytest.mark.parametrize("shape", ("mixed", "deleted_only", "multi_live"))
def test_reader_closes_mixed_deleted_only_and_multi_live_shapes(
    shape: str,
) -> None:
    first = _snapshot(project_id="project-a", document_id="document-a")
    second = _snapshot(project_id="project-b", document_id="document-b")
    if shape == "mixed":
        members = (_live(first), _deleted())
        snapshots = (first,)
        expected = (2, 1, 1, 1)
    elif shape == "deleted_only":
        members = (_deleted(),)
        snapshots = ()
        expected = (1, 0, 1, 0)
    else:
        members = (
            _live(first, catalog_ordinal=0, import_id="a" * 64),
            _live(second, catalog_ordinal=1, import_id="b" * 64),
        )
        snapshots = (first, second)
        expected = (2, 2, 0, 2)
    connection, plan, _projected = _live_stored_closure(
        members=members,
        snapshots=snapshots,
    )
    try:
        binding = _read(connection, plan)
        assert (
            binding.project_count,
            binding.live_project_count,
            binding.deleted_project_count,
            binding.baseline_commit_count,
        ) == expected
    finally:
        connection.close()


def test_mapping_rejects_ordinal_holes_and_nonexclusive_deleted_fields() -> None:
    connection, plan, _projected = _live_stored_closure()
    try:
        _header, _payload, value = _artifact(
            connection,
            cutover_id=plan.cutover_id,
            artifact_kind="member_mapping",
        )
        hole = copy.deepcopy(value)
        hole["members"][0]["ordinal"] = 1
        leaked = copy.deepcopy(value)
        leaked["members"][0]["status"] = "deleted"
        for candidate in (hole, leaked):
            with pytest.raises(SQLiteCutoverInstallClosureError) as rejected:
                _mapping(candidate, plan.cutover_id)
            assert rejected.value.code == (
                "cutover_install_closure_member_mapping_invalid"
            )
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("tamper", "expected_code"),
    (
        (
            "entity_nested",
            "cutover_install_closure_baseline_snapshot_binding_invalid",
        ),
        ("asset_nested", "cutover_install_closure_baseline_asset_invalid"),
    ),
)
def test_baseline_v2_nested_rows_are_rebuilt_from_the_database(
    tamper: str,
    expected_code: str,
) -> None:
    snapshot = _snapshot(
        references=(
            asset_support._ref(
                asset_support.SHA_A,
                size=len(asset_support.BLOB_A),
            ),
        )
    )
    connection, plan, _projected = _live_stored_closure(
        members=(_live(snapshot),),
        snapshots=(snapshot,),
    )
    try:
        _mapping_header, _mapping_payload, mapping_value = _artifact(
            connection,
            cutover_id=plan.cutover_id,
            artifact_kind="member_mapping",
        )
        members, counts = _mapping(mapping_value, plan.cutover_id)
        _baseline_header, _baseline_payload, baseline_value = _artifact(
            connection,
            cutover_id=plan.cutover_id,
            artifact_kind="baseline_projection",
        )
        changed = copy.deepcopy(baseline_value)
        if tamper == "entity_nested":
            changed["members"][0]["live"]["entities"][0]["snapshot_bytes"] += 1
        else:
            changed["asset_blobs"][0]["byte_length"] += 1
        with pytest.raises(SQLiteCutoverInstallClosureError) as rejected:
            _baseline(
                connection,
                changed,
                cutover_id=plan.cutover_id,
                committed_at=COMMITTED_AT,
                mapping=members,
                counts=counts,
                runtime=_runtime(connection, plan.cutover_id),
            )
        assert rejected.value.code == expected_code
    finally:
        connection.close()


def test_reader_rejects_mapping_to_business_commit_field_mismatch() -> None:
    connection, plan, _projected = _live_stored_closure()
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("DROP TRIGGER business_commits_no_update")
        connection.execute("UPDATE business_commits SET save_policy='legacy'")
        with pytest.raises(SQLiteCutoverInstallClosureError) as rejected:
            _read(connection, plan)
        assert rejected.value.code == (
            "cutover_install_closure_baseline_commit_invalid"
        )
    finally:
        connection.close()


def test_reader_recomputes_inner_atomic_roots_instead_of_trusting_mirrors() -> None:
    connection, plan, _projected = _live_stored_closure()
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        _drop_premount_guards(connection)
        connection.execute("DROP TRIGGER atomic_business_write_receipts_no_update")
        connection.execute(
            "DROP TRIGGER business_cutover_baseline_atomic_receipts_no_update"
        )
        changed = b"p" * 32
        connection.execute(
            "UPDATE atomic_business_write_receipts " "SET pipeline_intents_sha256=?",
            (changed,),
        )
        connection.execute(
            "UPDATE business_cutover_baseline_atomic_receipts "
            "SET pipeline_intents_sha256=?",
            (changed,),
        )
        with pytest.raises(SQLiteCutoverInstallClosureError) as rejected:
            _read(connection, plan)
        assert rejected.value.code == (
            "cutover_install_closure_baseline_sidecar_invalid"
        )
    finally:
        connection.close()


def test_reader_binds_mapping_artifact_back_to_install_receipt() -> None:
    connection, plan, _projected = _live_stored_closure()
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("DROP TRIGGER business_cutover_install_receipts_no_update")
        connection.execute(
            "UPDATE business_cutover_install_receipts " "SET member_mapping_sha256=?",
            (b"m" * 32,),
        )
        with pytest.raises(SQLiteCutoverInstallClosureError) as rejected:
            _read(connection, plan)
        assert rejected.value.code == (
            "cutover_install_closure_install_receipt_invalid"
        )
    finally:
        connection.close()


def test_reader_rejects_mapping_external_business_receipt_namespace() -> None:
    connection, plan, _projected = _live_stored_closure()
    try:
        _drop_premount_guards(connection)
        connection.execute(
            "INSERT INTO transaction_receipts(transaction_id,kind,committed_at) "
            "VALUES (?,?,?)",
            ("ze-business:" + "f" * 32, "ze_business_commit_v1", COMMITTED_AT),
        )
        with pytest.raises(SQLiteCutoverInstallClosureError) as rejected:
            _read(connection, plan)
        assert rejected.value.code == (
            "cutover_install_closure_baseline_database_coverage_invalid"
        )
    finally:
        connection.close()


def test_reader_binds_asset_blob_metadata_to_baseline_v2() -> None:
    snapshot = _snapshot(
        references=(
            asset_support._ref(
                asset_support.SHA_A,
                size=len(asset_support.BLOB_A),
            ),
        )
    )
    connection, plan, _projected = _live_stored_closure(
        members=(_live(snapshot),),
        snapshots=(snapshot,),
    )
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        _drop_premount_guards(connection)
        connection.execute("DROP TRIGGER asset_blobs_immutable_columns")
        connection.execute("DROP TRIGGER asset_blobs_revision_guard")
        connection.execute("UPDATE asset_blobs SET byte_length=byte_length+1")
        with pytest.raises(SQLiteCutoverInstallClosureError) as rejected:
            _read(connection, plan)
        assert rejected.value.code == ("cutover_install_closure_baseline_asset_invalid")
    finally:
        connection.close()


@pytest.mark.parametrize(
    "tamper,expected_code",
    (
        ("chunk_bytes", "cutover_install_closure_artifact_chunk_hash_mismatch"),
        ("chunk_and_hash", "cutover_install_closure_artifact_hash_mismatch"),
        ("member_ordinal", "cutover_install_closure_baseline_sidecar_invalid"),
        ("closure_manifest", "cutover_install_closure_payload_invalid"),
    ),
)
def test_fresh_reader_rejects_relationally_reachable_semantic_tamper(
    tamper: str,
    expected_code: str,
) -> None:
    connection, plan, _projected = _live_stored_closure()
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        if tamper in {"chunk_bytes", "chunk_and_hash"}:
            connection.execute(
                "DROP TRIGGER " "business_cutover_install_artifact_chunks_no_update"
            )
            row = connection.execute(
                "SELECT chunk_bytes FROM "
                "business_cutover_install_artifact_chunks "
                "WHERE artifact_kind='member_mapping' AND ordinal=0"
            ).fetchone()
            changed = bytes([row[0][0] ^ 1]) + row[0][1:]
            if tamper == "chunk_bytes":
                connection.execute(
                    "UPDATE business_cutover_install_artifact_chunks "
                    "SET chunk_bytes=? WHERE artifact_kind='member_mapping' "
                    "AND ordinal=0",
                    (changed,),
                )
            else:
                import business_cutover_install_closure as closure

                changed_hash = closure._digest(
                    closure.BUSINESS_CUTOVER_ARTIFACT_CHUNK_HASH_DOMAIN,
                    closure._text(plan.cutover_id),
                    closure._text("member_mapping"),
                    closure._integer(0),
                    closure._integer(0),
                    changed,
                )
                connection.execute(
                    "UPDATE business_cutover_install_artifact_chunks "
                    "SET chunk_bytes=?,chunk_sha256=? "
                    "WHERE artifact_kind='member_mapping' AND ordinal=0",
                    (changed, changed_hash),
                )
        elif tamper == "member_ordinal":
            connection.execute(
                "DROP TRIGGER " "business_cutover_baseline_atomic_receipts_no_update"
            )
            connection.execute(
                "UPDATE business_cutover_baseline_atomic_receipts "
                "SET member_ordinal=999"
            )
        else:
            connection.execute(
                "DROP TRIGGER business_cutover_install_closures_no_update"
            )
            connection.execute(
                "UPDATE business_cutover_install_closures " "SET manifest_bytes=?",
                (b"x" * len(_projected.closure.manifest_bytes),),
            )

        with pytest.raises(SQLiteCutoverInstallClosureError) as rejected:
            _read(connection, plan)
        assert rejected.value.code == expected_code
    finally:
        connection.close()


def test_reader_has_no_public_surface() -> None:
    import sqlite_cutover_install_closure_reader as module

    assert module.__all__ == ()
