"""Adversarial evidence for the plan-free V12 cutover durable auditor."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sqlite3
import sys

import pytest


_HERE = Path(__file__).resolve().parent
_DATA_STRUCTURE = _HERE.parents[1] / "01-data-structure"
for _path in (
    _HERE,
    _DATA_STRUCTURE / "01-document",
    _DATA_STRUCTURE / "02-entity",
    _DATA_STRUCTURE / "04-field-types",
):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import sqlite_cutover_durable_audit as module
import sqlite_cutover_verification as verification_module
from sqlite_business_audit import startup_business_runtime_full_audit
from receipt_namespace_integrity import _receipt_namespace_sha256
from receipt_namespace_contract import ReceiptNamespaceEntry
from repository_audit_contract import (
    RepositoryAuditAuthorityState,
    RepositoryAuditCoverage,
    RepositoryAuditFindings,
    RepositoryAuditObservedInput,
    RepositoryAuditPurpose,
    RepositoryAuditRuntimeBinding,
    RepositoryAuditSchemaObservation,
)
from repository_audit_integrity import (
    _findings_digest,
    _prepare_repository_audit_values,
)
from sqlite_cutover_durable_audit import (
    SQLiteCutoverDurableAuditStatus,
    SQLiteCutoverDurableAuditor,
    SQLiteCutoverDurablePhase,
)
from sqlite_cutover_durable_audit_test_support import (
    create_canonical_cutover_phase_database,
)
from sqlite_cutover_install_test_support import (
    _create_deleted_cutover_install_fixture,
    _create_empty_cutover_install_fixture,
    _create_live_cutover_install_fixture,
    _make_cutover_install_writer,
)
from sqlite_kernel import SqliteDatabase
from sqlite_production_test_support import create_canonical_mounted_business_database
from sqlite_production_authority import (
    SQLiteProductionAuthorityError,
    _read_production_mounted_authority,
)
from sqlite_production_writer import SQLiteProductionAtomicWriter
from sqlite_schema import MIGRATIONS, _expected_migration_ledger_sha256
from test_sqlite_production_writer import (
    _advance_outbox_to_effectless_dead,
    _production_plan,
)


def _install(fixture):
    result = _make_cutover_install_writer(fixture).install(fixture.projection)
    assert result.status.value == "installed_read_only"
    return fixture.database


def test_auditor_consumes_neutral_cutover_verification_leaf() -> None:
    assert module._budget is verification_module._read_cutover_verification_budget
    assert (
        module._install_progress_budget
        is verification_module._install_cutover_verification_progress_budget
    )
    assert (
        module._verify_legacy_receipt_namespace
        is verification_module._verify_legacy_receipt_namespace
    )
    assert (
        module._verify_production_receipt_namespace
        is verification_module._verify_production_receipt_namespace
    )
    assert module._failure_is_unavailable is verification_module._failure_is_unavailable


def _drop_restore_trigger(connection, name, mutate) -> None:
    names = (name,) if type(name) is str else tuple(name)
    originals = {}
    for value in names:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
            (value,),
        ).fetchone()
        assert row is not None and type(row[0]) is str
        originals[value] = row[0]
        connection.execute('DROP TRIGGER "' + value + '"')
    mutate()
    for value in names:
        connection.execute(originals[value])


def _drop_restore_table_triggers(connection, table: str, mutate) -> None:
    rows = connection.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name=? "
        "ORDER BY name",
        (table,),
    ).fetchall()
    assert rows and all(type(row[0]) is str and type(row[1]) is str for row in rows)
    for row in rows:
        connection.execute('DROP TRIGGER "' + row[0] + '"')
    mutate()
    for row in rows:
        connection.execute(row[1])


@pytest.mark.parametrize("tamper", ("manifest", "missing"))
def test_v13_binding_tamper_revokes_authority_and_exact_audit(
    tmp_path: Path,
    tamper: str,
) -> None:
    baseline = create_canonical_mounted_business_database(
        tmp_path / ("v13-binding-%s.sqlite3" % tamper)
    )
    connection = sqlite3.connect(baseline.database.path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        if tamper == "manifest":
            _drop_restore_trigger(
                connection,
                "business_cutover_mount_bindings_no_update",
                lambda: connection.execute(
                    "UPDATE business_cutover_mount_bindings SET "
                    "binding_manifest_bytes="
                    "CAST(binding_manifest_bytes || X'00' AS BLOB)"
                ),
            )
        else:
            _drop_restore_trigger(
                connection,
                "business_cutover_mount_bindings_no_delete",
                lambda: connection.execute(
                    "DELETE FROM business_cutover_mount_bindings"
                ),
            )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(SQLiteProductionAuthorityError):
        _read_production_mounted_authority(baseline.database)
    result = SQLiteCutoverDurableAuditor(baseline.database).inspect()
    assert result.status is SQLiteCutoverDurableAuditStatus.CORRUPT
    assert result.code == "cutover_durable_audit_phase_binding_invalid"


def _append_periodic_audit(
    database: SqliteDatabase,
    *,
    receipt_root_override: bytes | None = None,
    high_water_delta: int = 0,
) -> None:
    connection, _filesystem = database._open_unvalidated()
    try:
        connection.execute("BEGIN IMMEDIATE")
        first_row = connection.execute(
            "SELECT manifest_bytes FROM repository_audit_certificates "
            "WHERE sequence=1"
        ).fetchone()
        assert first_row is not None
        first = module._audit_evidence_from_manifest(first_row[0]).evidence
        runtime = connection.execute(
            "SELECT authority,authority_state,repository_epoch,cutover_id,"
            "source_admission_revision,repository_revision,state_revision,"
            "runtime_generation,code_digest,schema_version,updated_at "
            "FROM repository_runtime_control WHERE singleton=1"
        ).fetchone()
        install = connection.execute(
            "SELECT manifest_sha256 FROM business_cutover_install_receipts"
        ).fetchone()
        worker = connection.execute(
            "SELECT sequence,tail_sha256 FROM worker_ledger_head WHERE singleton=1"
        ).fetchone()
        receipt_rows = connection.execute(
            "SELECT transaction_id,kind,committed_at FROM transaction_receipts "
            "ORDER BY transaction_id COLLATE BINARY,kind COLLATE BINARY"
        ).fetchall()
        entries = tuple(
            ReceiptNamespaceEntry(row[0], row[1], row[2]) for row in receipt_rows
        )
        receipt_count, receipt_root = _receipt_namespace_sha256(
            entries,
            expected_count=len(entries),
        )
        now = connection.execute(
            "SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')"
        ).fetchone()[0]
        observed = RepositoryAuditObservedInput(
            certificate_id="f" * 32,
            purpose=RepositoryAuditPurpose.PERIODIC,
            runtime=RepositoryAuditRuntimeBinding(
                authority=runtime[0],
                authority_state=RepositoryAuditAuthorityState(runtime[1]),
                repository_epoch=runtime[2],
                cutover_id=runtime[3],
                source_admission_revision=runtime[4],
                repository_revision=runtime[5] + high_water_delta,
                state_revision=runtime[6] + high_water_delta,
                runtime_generation=runtime[7],
                code_digest=runtime[8],
                schema_version=runtime[9],
                updated_at=runtime[10],
            ),
            schema=RepositoryAuditSchemaObservation(
                user_version=MIGRATIONS[-1].version,
                schema_fingerprint=module._expected_schema_fingerprint(),
                migration_ledger_count=MIGRATIONS[-1].version,
                migration_ledger_sha256=_expected_migration_ledger_sha256(),
            ),
            coverage=RepositoryAuditCoverage(
                covered_business_revision=runtime[5] + high_water_delta,
                worker_ledger_sequence=worker[0],
                worker_ledger_tail_sha256=worker[1],
                receipt_namespace_count=receipt_count + high_water_delta,
                receipt_namespace_sha256=(
                    receipt_root
                    if receipt_root_override is None
                    else receipt_root_override
                ),
                cutover_install_manifest_sha256=install[0],
            ),
            findings=RepositoryAuditFindings(
                page_integrity="ok",
                foreign_key_violation_count=0,
                namespace_violation_count=0,
                started_at=now,
                completed_at=now,
            ),
        )
        evidence = _prepare_repository_audit_values(observed, first).evidence
        connection.execute(
            "INSERT INTO transaction_receipts(transaction_id,kind,committed_at) "
            "VALUES (?,?,?)",
            (
                evidence.transaction_id,
                evidence.receipt_kind,
                evidence.findings.completed_at,
            ),
        )
        connection.execute(
            "INSERT INTO repository_audit_certificates("
            + ",".join(module._AUDIT_ROW_COLUMNS)
            + ") VALUES ("
            + ",".join("?" for _ in module._AUDIT_ROW_COLUMNS)
            + ")",
            (
                evidence.certificate_id,
                evidence.runtime.cutover_id,
                evidence.sequence,
                evidence.purpose.value,
                evidence.transaction_id,
                evidence.receipt_kind,
                evidence.findings.completed_at,
                evidence.previous_certificate_id,
                evidence.previous_manifest_sha256,
                evidence.previous_certificate_tail_sha256,
                evidence.schema.user_version,
                evidence.schema.migration_ledger_count,
                evidence.schema.migration_ledger_sha256,
                evidence.coverage.receipt_namespace_count,
                evidence.coverage.receipt_namespace_sha256,
                evidence.coverage.worker_ledger_sequence,
                evidence.coverage.worker_ledger_tail_sha256,
                _findings_digest(evidence.findings),
                evidence.manifest_bytes,
                evidence.manifest_sha256,
                evidence.certificate_tail_sha256,
            ),
        )
        connection.execute(
            "UPDATE repository_audit_head SET state_revision=?,sequence=?,"
            "certificate_id=?,previous_tail_sha256=?,tail_sha256=?,updated_at=? "
            "WHERE singleton=1",
            (
                evidence.sequence,
                evidence.sequence,
                evidence.certificate_id,
                evidence.previous_certificate_tail_sha256,
                evidence.certificate_tail_sha256,
                evidence.findings.completed_at,
            ),
        )
        connection.commit()
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()


def test_pristine_database_is_absent(tmp_path: Path) -> None:
    database = SqliteDatabase((tmp_path / "absent.sqlite3").resolve())
    database.initialize()
    result = SQLiteCutoverDurableAuditor(database).inspect()
    assert result.status is SQLiteCutoverDurableAuditStatus.ABSENT
    assert result.identity is None
    assert result.budget is not None


def test_auditor_does_not_repair_persistent_journal_mode(tmp_path: Path) -> None:
    path = (tmp_path / "journal-drift.sqlite3").resolve()
    database = SqliteDatabase(path)
    database.initialize()
    raw = sqlite3.connect(str(path))
    try:
        assert raw.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
    finally:
        raw.close()
    result = SQLiteCutoverDurableAuditor(database).inspect()
    assert result.status is SQLiteCutoverDurableAuditStatus.CORRUPT
    raw = sqlite3.connect(str(path))
    try:
        assert raw.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    finally:
        raw.close()


def test_auditor_does_not_repair_file_permissions(tmp_path: Path) -> None:
    path = (tmp_path / "permission-drift.sqlite3").resolve()
    database = SqliteDatabase(path)
    database.initialize()
    path.chmod(0o644)
    result = SQLiteCutoverDurableAuditor(database).inspect()
    assert result.status is SQLiteCutoverDurableAuditStatus.CORRUPT
    assert path.stat().st_mode & 0o777 == 0o644


def test_complete_preinstall_is_unknown_not_corrupt(tmp_path: Path) -> None:
    fixture = _create_empty_cutover_install_fixture(
        tmp_path / "preinstall.sqlite3"
    )
    result = SQLiteCutoverDurableAuditor(fixture.database).inspect()
    assert result.status is SQLiteCutoverDurableAuditStatus.UNKNOWN
    assert result.code == "cutover_durable_audit_preinstall_unproven"


def test_stage_only_preinstall_is_unknown_not_corrupt(tmp_path: Path) -> None:
    fixture = _create_live_cutover_install_fixture(
        tmp_path / "stage-only-preinstall.sqlite3",
        asset_blob=b"staged-asset",
    )
    connection, _filesystem = fixture.database._open_unvalidated()
    try:
        connection.execute("BEGIN IMMEDIATE")
        coverage_transaction_id = connection.execute(
            "SELECT transaction_id FROM business_asset_coverage_receipts"
        ).fetchone()[0]
        _drop_restore_table_triggers(
            connection,
            "business_asset_coverage_receipts",
            lambda: connection.execute(
                "DELETE FROM business_asset_coverage_receipts"
            ),
        )
        _drop_restore_table_triggers(
            connection,
            "transaction_receipts",
            lambda: connection.execute(
                "DELETE FROM transaction_receipts WHERE transaction_id=?",
                (coverage_transaction_id,),
            ),
        )
        connection.commit()
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()
    result = SQLiteCutoverDurableAuditor(fixture.database).inspect()
    assert result.status is SQLiteCutoverDurableAuditStatus.UNKNOWN
    assert result.code == "cutover_durable_audit_preinstall_unproven"


@pytest.mark.parametrize(
    "factory,kwargs",
    (
        (_create_empty_cutover_install_fixture, {}),
        (_create_live_cutover_install_fixture, {}),
        (_create_live_cutover_install_fixture, {"include_deleted": True}),
        (_create_deleted_cutover_install_fixture, {}),
        (_create_live_cutover_install_fixture, {"asset_blob": b"asset-bytes"}),
    ),
)
def test_real_install_writer_is_exact_headless(
    tmp_path: Path,
    factory,
    kwargs,
) -> None:
    fixture = factory(tmp_path / (factory.__name__ + ".sqlite3"), **kwargs)
    database = _install(fixture)
    result = SQLiteCutoverDurableAuditor(database).inspect()
    assert result.status is SQLiteCutoverDurableAuditStatus.EXACT
    assert result.identity is not None
    assert result.identity.phase is SQLiteCutoverDurablePhase.INSTALLED_CLOSURE
    connection, _filesystem = database._open_unvalidated()
    try:
        assert connection.execute(
            "SELECT count(*) FROM business_cutover_head"
        ).fetchone()[0] == 0
    finally:
        connection.close()


def test_headless_writer_state_passes_shared_full_audit(tmp_path: Path) -> None:
    fixture = _create_empty_cutover_install_fixture(
        tmp_path / "headless-full-audit.sqlite3"
    )
    database = _install(fixture)
    profile = startup_business_runtime_full_audit(database)
    assert profile.authority_state.value == "read_only"
    assert profile.state_revision == 1


@pytest.mark.parametrize(
    "revision,phase",
    (
        (1, SQLiteCutoverDurablePhase.INSTALLED_HEAD),
        (2, SQLiteCutoverDurablePhase.ASSETS_COVERED),
        (3, SQLiteCutoverDurablePhase.AUDITED),
        (4, SQLiteCutoverDurablePhase.ACTIVATED),
        (5, SQLiteCutoverDurablePhase.MOUNTED),
    ),
)
def test_every_canonical_phase_is_exact(
    tmp_path: Path,
    revision: int,
    phase: SQLiteCutoverDurablePhase,
) -> None:
    database = create_canonical_cutover_phase_database(
        tmp_path / ("phase-" + str(revision) + ".sqlite3"),
        phase_revision=revision,
    )
    result = SQLiteCutoverDurableAuditor(database).inspect()
    assert result.status is SQLiteCutoverDurableAuditStatus.EXACT
    assert result.identity is not None
    assert result.identity.phase is phase


def test_expected_identity_is_compared_only_after_self_proof(tmp_path: Path) -> None:
    fixture = _create_empty_cutover_install_fixture(
        tmp_path / "expected.sqlite3"
    )
    database = _install(fixture)
    auditor = SQLiteCutoverDurableAuditor(database)
    exact = auditor.inspect()
    assert exact.identity is not None
    assert auditor.inspect(expected=exact.identity).status is (
        SQLiteCutoverDurableAuditStatus.EXACT
    )
    foreign = replace(exact.identity, repository_revision=2)
    conflict = auditor.inspect(expected=foreign)
    assert conflict.status is SQLiteCutoverDurableAuditStatus.CONFLICT
    assert conflict.identity == exact.identity


def test_detached_identity_rejects_half_and_foreign_phase_pointers(
    tmp_path: Path,
) -> None:
    database = create_canonical_cutover_phase_database(
        tmp_path / "identity-pointers.sqlite3",
        phase_revision=5,
    )
    identity = SQLiteCutoverDurableAuditor(database).inspect().identity
    assert identity is not None
    for changes in (
        {"audit_transaction_id": None},
        {"audit_manifest_sha256": None},
        {"activation_transaction_id": None},
        {"activation_manifest_sha256": None},
        {"mount_transaction_id": None},
        {"mount_manifest_sha256": None},
    ):
        with pytest.raises(TypeError):
            replace(identity, **changes)
    foreign_cutover_id = "0" * 64 if identity.cutover_id != "0" * 64 else "1" * 64
    with pytest.raises(TypeError, match="another cutover"):
        replace(
            identity,
            activation_transaction_id=(
                module.BUSINESS_CUTOVER_ACTIVATION_TRANSACTION_PREFIX
                + foreign_cutover_id
            ),
        )
    with pytest.raises(TypeError, match="another cutover"):
        replace(
            identity,
            mount_transaction_id=(
                module.BUSINESS_CUTOVER_MOUNT_TRANSACTION_PREFIX
                + foreign_cutover_id
            ),
        )


def test_detached_identity_rejects_string_like_runtime_generation(
    tmp_path: Path,
) -> None:
    fixture = _create_empty_cutover_install_fixture(
        tmp_path / "identity-generation.sqlite3"
    )
    identity = SQLiteCutoverDurableAuditor(_install(fixture)).inspect().identity
    assert identity is not None

    class StringLike:
        def encode(self, *_args):
            return b"looks-canonical"

    with pytest.raises(TypeError, match="exact string"):
        replace(identity, runtime_generation=StringLike())


def test_partial_database_is_corrupt_even_with_foreign_expected(
    tmp_path: Path,
) -> None:
    fixture = _create_empty_cutover_install_fixture(
        tmp_path / "partial.sqlite3"
    )
    database = _install(fixture)
    auditor = SQLiteCutoverDurableAuditor(database)
    exact = auditor.inspect()
    assert exact.identity is not None
    connection, _filesystem = database._open_unvalidated()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("PRAGMA defer_foreign_keys=ON")
        _drop_restore_trigger(
            connection,
            "business_cutover_install_closures_no_update",
            lambda: connection.execute(
                "UPDATE business_cutover_install_closures "
                "SET manifest_sha256=zeroblob(32)"
            ),
        )
        connection.commit()
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()
    result = auditor.inspect(
        expected=replace(exact.identity, repository_revision=2)
    )
    assert result.status is SQLiteCutoverDurableAuditStatus.CORRUPT


def test_equal_length_artifact_tamper_is_corrupt(tmp_path: Path) -> None:
    fixture = _create_empty_cutover_install_fixture(
        tmp_path / "artifact-tamper.sqlite3"
    )
    database = _install(fixture)
    connection, _filesystem = database._open_unvalidated()
    try:
        connection.execute("BEGIN IMMEDIATE")

        def mutate():
            row = connection.execute(
                "SELECT rowid,chunk_bytes FROM business_cutover_install_artifact_chunks "
                "ORDER BY artifact_kind,ordinal LIMIT 1"
            ).fetchone()
            changed = bytes([row[1][0] ^ 1]) + row[1][1:]
            connection.execute(
                "UPDATE business_cutover_install_artifact_chunks "
                "SET chunk_bytes=? WHERE rowid=?",
                (changed, row[0]),
            )

        _drop_restore_trigger(
            connection,
            "business_cutover_install_artifact_chunks_no_update",
            mutate,
        )
        connection.commit()
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()
    result = SQLiteCutoverDurableAuditor(database).inspect()
    assert result.status is SQLiteCutoverDurableAuditStatus.CORRUPT


def test_reserved_receipt_blob_pollution_is_corrupt(tmp_path: Path) -> None:
    fixture = _create_empty_cutover_install_fixture(
        tmp_path / "blob-pollution.sqlite3"
    )
    database = _install(fixture)
    connection, _filesystem = database._open_unvalidated()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO transaction_receipts(transaction_id,kind,committed_at) "
            "VALUES (CAST(? AS BLOB),?,?)",
            (
                "_business-cutover-join:" + "a" * 64,
                "ordinary-kind",
                fixture.install_committed_at,
            ),
        )
        connection.commit()
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()
    result = SQLiteCutoverDurableAuditor(database).inspect()
    assert result.status is SQLiteCutoverDurableAuditStatus.CORRUPT
    assert result.code == "cutover_durable_audit_reserved_receipt_blob_pollution"


def test_head_digest_tamper_is_corrupt(tmp_path: Path) -> None:
    database = create_canonical_cutover_phase_database(
        tmp_path / "head-tamper.sqlite3",
        phase_revision=2,
    )
    connection, _filesystem = database._open_unvalidated()
    try:
        connection.execute("BEGIN IMMEDIATE")
        _drop_restore_trigger(
            connection,
            (
                "business_cutover_head_cas_update_guard",
                "business_cutover_head_phase_guard",
                "business_cutover_head_update_binding_guard",
            ),
            lambda: connection.execute(
                "UPDATE business_cutover_head SET head_sha256=?",
                (b"x" * 32,),
            ),
        )
        connection.commit()
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()
    assert SQLiteCutoverDurableAuditor(database).inspect().status is (
        SQLiteCutoverDurableAuditStatus.CORRUPT
    )


def test_audit_tail_tamper_is_corrupt(tmp_path: Path) -> None:
    database = create_canonical_cutover_phase_database(
        tmp_path / "audit-tail.sqlite3",
        phase_revision=3,
    )
    connection, _filesystem = database._open_unvalidated()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("PRAGMA defer_foreign_keys=ON")
        _drop_restore_trigger(
            connection,
            (
                "repository_audit_head_cas_update_guard",
                "repository_audit_head_sequence_guard",
                "repository_audit_head_advance_guard",
                "repository_audit_certificates_no_update",
            ),
            lambda: (
                connection.execute(
                    "UPDATE repository_audit_certificates "
                    "SET certificate_tail_sha256=?",
                    (b"y" * 32,),
                ),
                connection.execute(
                    "UPDATE repository_audit_head SET tail_sha256=?",
                    (b"y" * 32,),
                ),
            ),
        )
        connection.commit()
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()
    assert SQLiteCutoverDurableAuditor(database).inspect().status is (
        SQLiteCutoverDurableAuditStatus.CORRUPT
    )


def test_mounted_scheduler_tamper_is_corrupt(tmp_path: Path) -> None:
    database = create_canonical_cutover_phase_database(
        tmp_path / "mounted-scheduler.sqlite3",
        phase_revision=5,
    )
    connection, _filesystem = database._open_unvalidated()
    try:
        connection.execute("BEGIN IMMEDIATE")
        _drop_restore_trigger(
            connection,
            (
                "worker_ledger_head_cas_update_guard",
                "worker_ledger_head_sequence_guard",
            ),
            lambda: connection.execute(
                "UPDATE worker_ledger_head SET state_revision=7 WHERE singleton=1"
            ),
        )
        connection.commit()
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()
    assert SQLiteCutoverDurableAuditor(database).inspect().status is (
        SQLiteCutoverDurableAuditStatus.CORRUPT
    )


def test_mounted_extra_repository_row_is_corrupt_without_suffix(
    tmp_path: Path,
) -> None:
    database = create_canonical_cutover_phase_database(
        tmp_path / "mounted-extra-row.sqlite3",
        phase_revision=5,
    )
    connection, _filesystem = database._open_unvalidated()
    try:
        connection.execute("BEGIN IMMEDIATE")
        committed_at = connection.execute(
            "SELECT committed_at FROM business_cutover_mount_receipts"
        ).fetchone()[0]
        _drop_restore_table_triggers(
            connection,
            "repository_projects",
            lambda: connection.execute(
                "INSERT INTO repository_projects("
                "repository_singleton,project_id,generation,project_revision,status,"
                "current_snapshot_sha256,created_at,updated_at,deleted_at) "
                "VALUES (1,'unbound-project',1,1,'deleted',NULL,?,?,?)",
                (committed_at, committed_at, committed_at),
            ),
        )
        connection.commit()
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()
    assert SQLiteCutoverDurableAuditor(database).inspect().status is (
        SQLiteCutoverDurableAuditStatus.CORRUPT
    )


def test_future_runtime_time_is_corrupt(tmp_path: Path) -> None:
    database = create_canonical_cutover_phase_database(
        tmp_path / "future-runtime.sqlite3",
        phase_revision=5,
    )
    connection, _filesystem = database._open_unvalidated()
    try:
        connection.execute("BEGIN IMMEDIATE")
        _drop_restore_table_triggers(
            connection,
            "repository_runtime_control",
            lambda: connection.execute(
                "UPDATE repository_runtime_control "
                "SET updated_at='2099-01-01T00:00:00.000Z' WHERE singleton=1"
            ),
        )
        connection.commit()
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()
    assert SQLiteCutoverDurableAuditor(database).inspect().status is (
        SQLiteCutoverDurableAuditStatus.CORRUPT
    )


def test_future_audit_or_mount_time_is_corrupt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database = create_canonical_cutover_phase_database(
        tmp_path / "future-phase-time.sqlite3",
        phase_revision=5,
    )
    monkeypatch.setattr(
        module,
        "_database_time",
        lambda _connection: "2000-01-01T00:00:00.000Z",
    )
    assert SQLiteCutoverDurableAuditor(database).inspect().status is (
        SQLiteCutoverDurableAuditStatus.CORRUPT
    )


def test_first_install_audit_receipt_root_is_rebuilt_from_causal_rows(
    tmp_path: Path,
) -> None:
    database = create_canonical_cutover_phase_database(
        tmp_path / "install-audit-root.sqlite3",
        phase_revision=5,
    )
    connection, _filesystem = database._open_unvalidated()
    try:
        connection.execute("BEGIN IMMEDIATE")
        now = connection.execute(
            "SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')"
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO transaction_receipts(transaction_id,kind,committed_at) "
            "VALUES ('offline-after-install-audit','ordinary-kernel',?)",
            (now,),
        )
        connection.commit()
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()
    result = SQLiteCutoverDurableAuditor(database).inspect()
    assert result.status is SQLiteCutoverDurableAuditStatus.CORRUPT
    assert result.code == "cutover_durable_audit_install_receipt_coverage_invalid"


@pytest.mark.parametrize("cause", (OSError("io"), MemoryError("memory")))
def test_closure_storage_failure_is_unknown(
    tmp_path: Path,
    monkeypatch,
    cause: BaseException,
) -> None:
    fixture = _create_empty_cutover_install_fixture(
        tmp_path / (type(cause).__name__ + ".sqlite3")
    )
    database = _install(fixture)

    def unavailable(*_args, **_kwargs):
        try:
            raise cause
        except BaseException as exc:
            raise module.SQLiteCutoverInstallClosureError(
                "cutover_install_closure_unavailable"
            ) from exc

    monkeypatch.setattr(module, "_read_business_cutover_install_closure", unavailable)
    result = SQLiteCutoverDurableAuditor(database).inspect()
    assert result.status is SQLiteCutoverDurableAuditStatus.UNKNOWN


def test_stable_closure_failure_is_corrupt(tmp_path: Path, monkeypatch) -> None:
    fixture = _create_empty_cutover_install_fixture(
        tmp_path / "stable-closure.sqlite3"
    )
    database = _install(fixture)

    def malformed(*_args, **_kwargs):
        raise module.SQLiteCutoverInstallClosureError(
            "cutover_install_closure_manifest_mismatch"
        )

    monkeypatch.setattr(module, "_read_business_cutover_install_closure", malformed)
    assert SQLiteCutoverDurableAuditor(database).inspect().status is (
        SQLiteCutoverDurableAuditStatus.CORRUPT
    )


def test_valid_periodic_audit_suffix_is_unknown_after_full_chain_proof(
    tmp_path: Path,
) -> None:
    database = create_canonical_cutover_phase_database(
        tmp_path / "periodic-audit.sqlite3",
        phase_revision=5,
    )
    _append_periodic_audit(database)
    result = SQLiteCutoverDurableAuditor(database).inspect()
    assert result.status is SQLiteCutoverDurableAuditStatus.UNKNOWN
    assert result.code == "cutover_durable_audit_certificate_suffix_unsupported"


def test_periodic_audit_suffix_cannot_hide_mount_tamper(tmp_path: Path) -> None:
    database = create_canonical_cutover_phase_database(
        tmp_path / "periodic-mount-tamper.sqlite3",
        phase_revision=5,
    )
    _append_periodic_audit(database)
    connection, _filesystem = database._open_unvalidated()
    try:
        connection.execute("BEGIN IMMEDIATE")

        def mutate() -> None:
            row = connection.execute(
                "SELECT manifest_bytes FROM business_cutover_mount_receipts"
            ).fetchone()
            changed = bytes([row[0][0] ^ 1]) + row[0][1:]
            connection.execute(
                "UPDATE business_cutover_mount_receipts SET manifest_bytes=?",
                (changed,),
            )

        _drop_restore_trigger(
            connection,
            "business_cutover_mount_receipts_no_update",
            mutate,
        )
        connection.commit()
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()
    assert SQLiteCutoverDurableAuditor(database).inspect().status is (
        SQLiteCutoverDurableAuditStatus.CORRUPT
    )


def test_periodic_audit_arbitrary_receipt_root_remains_unknown(tmp_path: Path) -> None:
    database = create_canonical_cutover_phase_database(
        tmp_path / "periodic-receipt-root.sqlite3",
        phase_revision=5,
    )
    _append_periodic_audit(database, receipt_root_override=b"q" * 32)
    result = SQLiteCutoverDurableAuditor(database).inspect()
    assert result.status is SQLiteCutoverDurableAuditStatus.UNKNOWN
    assert result.code == "cutover_durable_audit_certificate_suffix_unsupported"


def test_periodic_audit_cannot_claim_future_current_high_water(
    tmp_path: Path,
) -> None:
    database = create_canonical_cutover_phase_database(
        tmp_path / "periodic-future-high-water.sqlite3",
        phase_revision=5,
    )
    _append_periodic_audit(database, high_water_delta=100)
    result = SQLiteCutoverDurableAuditor(database).inspect()
    assert result.status is SQLiteCutoverDurableAuditStatus.CORRUPT
    assert result.code == "cutover_durable_audit_certificate_snapshot_invalid"


def test_legal_worker_and_periodic_suffix_is_unknown_not_corrupt(
    tmp_path: Path,
) -> None:
    baseline = create_canonical_mounted_business_database(
        tmp_path / "worker-periodic.sqlite3"
    )
    plan = _production_plan(baseline)
    SQLiteProductionAtomicWriter(baseline.database).commit(plan)
    _advance_outbox_to_effectless_dead(baseline, plan)
    _append_periodic_audit(baseline.database)
    result = SQLiteCutoverDurableAuditor(baseline.database).inspect()
    assert result.status is SQLiteCutoverDurableAuditStatus.UNKNOWN
    assert result.code == "cutover_durable_audit_post_mount_suffix_unsupported"


def test_post_mount_business_suffix_with_periodic_audit_is_unknown(
    tmp_path: Path,
) -> None:
    baseline = create_canonical_mounted_business_database(
        tmp_path / "post-mount-periodic.sqlite3"
    )
    _append_periodic_audit(baseline.database)
    result = SQLiteCutoverDurableAuditor(baseline.database).inspect()
    assert result.status is SQLiteCutoverDurableAuditStatus.UNKNOWN
    assert result.code == "cutover_durable_audit_post_mount_suffix_unsupported"


def test_malformed_second_audit_certificate_is_corrupt(tmp_path: Path) -> None:
    database = create_canonical_cutover_phase_database(
        tmp_path / "bad-second-audit.sqlite3",
        phase_revision=5,
    )
    connection, _filesystem = database._open_unvalidated()
    try:
        connection.execute("BEGIN IMMEDIATE")
        first = connection.execute(
            "SELECT " + ",".join(module._AUDIT_ROW_COLUMNS)
            + " FROM repository_audit_certificates WHERE sequence=1"
        ).fetchone()
        values = list(first)
        values[0] = "e" * 32
        values[2] = 2
        values[4] = "_repository-audit:" + "e" * 32
        values[7] = first[0]
        values[8] = first[19]
        values[9] = first[20]
        values[19] = b"y" * 32
        values[20] = b"z" * 32
        connection.execute(
            "INSERT INTO transaction_receipts(transaction_id,kind,committed_at) "
            "VALUES (?,?,?)",
            (values[4], values[5], values[6]),
        )
        _drop_restore_table_triggers(
            connection,
            "repository_audit_certificates",
            lambda: connection.execute(
                "INSERT INTO repository_audit_certificates("
                + ",".join(module._AUDIT_ROW_COLUMNS)
                + ") VALUES ("
                + ",".join("?" for _ in module._AUDIT_ROW_COLUMNS)
                + ")",
                tuple(values),
            ),
        )
        _drop_restore_table_triggers(
            connection,
            "repository_audit_head",
            lambda: connection.execute(
                "UPDATE repository_audit_head SET state_revision=2,sequence=2,"
                "certificate_id=?,previous_tail_sha256=?,tail_sha256=?,updated_at=? "
                "WHERE singleton=1",
                (values[0], first[20], values[20], values[6]),
            ),
        )
        connection.commit()
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()
    assert SQLiteCutoverDurableAuditor(database).inspect().status is (
        SQLiteCutoverDurableAuditStatus.CORRUPT
    )


def test_budget_overrun_is_unknown(tmp_path: Path, monkeypatch) -> None:
    fixture = _create_empty_cutover_install_fixture(
        tmp_path / "budget.sqlite3"
    )
    database = _install(fixture)
    monkeypatch.setattr(
        verification_module,
        "MAX_CUTOVER_DURABLE_AUDIT_ENTRIES",
        0,
    )
    result = SQLiteCutoverDurableAuditor(database).inspect()
    assert result.status is SQLiteCutoverDurableAuditStatus.UNKNOWN
    assert result.code == "cutover_durable_audit_entry_budget_exceeded"
    assert result.budget is not None and result.budget.entries > 0


def test_text_payload_is_included_in_byte_budget(tmp_path: Path, monkeypatch) -> None:
    database = SqliteDatabase((tmp_path / "text-budget.sqlite3").resolve())
    database.initialize()
    connection, _filesystem = database._open_unvalidated()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO transaction_receipts(transaction_id,kind,committed_at) "
            "VALUES (?,?,?)",
            ("ordinary-text-budget", "x" * 128, "2026-01-01T00:00:00.000Z"),
        )
        connection.commit()
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()
    monkeypatch.setattr(
        verification_module,
        "MAX_CUTOVER_DURABLE_AUDIT_BYTES",
        1,
    )
    result = SQLiteCutoverDurableAuditor(database).inspect()
    assert result.status is SQLiteCutoverDurableAuditStatus.UNKNOWN
    assert result.code == "cutover_durable_audit_byte_budget_exceeded"
    assert result.budget is not None and result.budget.payload_bytes >= 128


def test_transient_structural_failure_is_not_reported_as_corrupt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fixture = _create_empty_cutover_install_fixture(
        tmp_path / "transient-structure.sqlite3"
    )
    database = _install(fixture)
    original = module._verify_schema_and_marker
    calls = 0

    def transient(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise module._AuditCorrupt("injected_transient_structure")
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "_verify_schema_and_marker", transient)
    result = SQLiteCutoverDurableAuditor(database).inspect()
    assert calls >= 2
    assert result.status is SQLiteCutoverDurableAuditStatus.UNKNOWN
    assert result.code == "cutover_durable_audit_corrupt_fence_changed"


def test_repeated_structural_failure_is_corrupt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fixture = _create_empty_cutover_install_fixture(
        tmp_path / "stable-structure.sqlite3"
    )
    database = _install(fixture)
    calls = 0

    def stable(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise module._AuditCorrupt("injected_stable_structure")

    monkeypatch.setattr(module, "_verify_schema_and_marker", stable)
    result = SQLiteCutoverDurableAuditor(database).inspect()
    assert calls == 2
    assert result.status is SQLiteCutoverDurableAuditStatus.CORRUPT
    assert result.code == "cutover_durable_audit_structural_corrupt"


def test_structural_failure_with_cleanup_failure_is_unknown(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fixture = _create_empty_cutover_install_fixture(
        tmp_path / "structure-cleanup.sqlite3"
    )
    database = _install(fixture)

    def corrupt_structure(*_args, **_kwargs):
        raise module._AuditCorrupt("injected_structure")

    monkeypatch.setattr(module, "_verify_schema_and_marker", corrupt_structure)
    monkeypatch.setattr(module, "_cleanup", lambda _connection: False)
    result = SQLiteCutoverDurableAuditor(database).inspect()
    assert result.status is SQLiteCutoverDurableAuditStatus.UNKNOWN
    assert result.code == "cutover_durable_audit_unavailable"


def test_publish_fence_change_is_unknown(tmp_path: Path, monkeypatch) -> None:
    fixture = _create_empty_cutover_install_fixture(
        tmp_path / "fence.sqlite3"
    )
    auditor = SQLiteCutoverDurableAuditor(_install(fixture))
    original = module.SQLiteCutoverDurableAuditor._fresh_fence

    def changed(self):
        fence = original(self)
        return fence._replace(receipt_identity=("changed",))

    monkeypatch.setattr(module.SQLiteCutoverDurableAuditor, "_fresh_fence", changed)
    result = auditor.inspect()
    assert result.status is SQLiteCutoverDurableAuditStatus.UNKNOWN
    assert result.code == "cutover_durable_audit_publish_fence_changed"


def test_cleanup_failure_is_unknown(tmp_path: Path, monkeypatch) -> None:
    fixture = _create_empty_cutover_install_fixture(
        tmp_path / "cleanup.sqlite3"
    )
    auditor = SQLiteCutoverDurableAuditor(_install(fixture))
    monkeypatch.setattr(module, "_cleanup", lambda _connection: False)
    result = auditor.inspect()
    assert result.status is SQLiteCutoverDurableAuditStatus.UNKNOWN


def test_durability_failure_is_unknown(tmp_path: Path, monkeypatch) -> None:
    fixture = _create_empty_cutover_install_fixture(
        tmp_path / "durability.sqlite3"
    )
    database = _install(fixture)

    def unavailable(_self):
        raise OSError("injected durability failure")

    monkeypatch.setattr(SqliteDatabase, "_finalize_readonly_storage", unavailable)
    result = SQLiteCutoverDurableAuditor(database).inspect()
    assert result.status is SQLiteCutoverDurableAuditStatus.UNKNOWN
    assert result.code == "cutover_durable_audit_durability_barrier_failed"


def test_process_control_is_never_swallowed(tmp_path: Path, monkeypatch) -> None:
    fixture = _create_empty_cutover_install_fixture(
        tmp_path / "process-control.sqlite3"
    )
    auditor = SQLiteCutoverDurableAuditor(_install(fixture))

    def interrupt(*_args, **_kwargs):
        raise KeyboardInterrupt("injected")

    monkeypatch.setattr(module, "_snapshot_audit", interrupt)
    with pytest.raises(KeyboardInterrupt, match="injected"):
        auditor.inspect()


def test_cleanup_process_control_closes_snapshot_and_propagates(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fixture = _create_empty_cutover_install_fixture(
        tmp_path / "cleanup-process-control.sqlite3"
    )
    auditor = SQLiteCutoverDurableAuditor(_install(fixture))
    captured = []

    def interrupt_cleanup(connection):
        captured.append(connection)
        raise KeyboardInterrupt("cleanup-injected")

    monkeypatch.setattr(module, "_cleanup", interrupt_cleanup)
    with pytest.raises(KeyboardInterrupt, match="cleanup-injected"):
        auditor.inspect()
    assert captured
    with pytest.raises(module.SQLiteKernelError) as closed:
        captured[0].execute("SELECT 1")
    assert closed.value.code == "sqlite_connection_closed"


def test_progress_callback_process_control_is_restored(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database = create_canonical_cutover_phase_database(
        tmp_path / "progress-process-control.sqlite3",
        phase_revision=5,
    )
    original = module._install_progress_budget
    states = []

    class InterruptOnDecrement:
        def __sub__(self, _value):
            raise KeyboardInterrupt("progress-injected")

    def interrupting_budget(connection):
        state = original(connection)
        state.remaining = InterruptOnDecrement()
        states.append(state)
        return state

    monkeypatch.setattr(module, "_install_progress_budget", interrupting_budget)
    with pytest.raises(KeyboardInterrupt, match="progress-injected"):
        SQLiteCutoverDurableAuditor(database).inspect()
    assert states and isinstance(states[0].failure, KeyboardInterrupt)


def test_missing_database_is_unknown(tmp_path: Path) -> None:
    database = SqliteDatabase((tmp_path / "missing.sqlite3").resolve())
    result = SQLiteCutoverDurableAuditor(database).inspect()
    assert result.status is SQLiteCutoverDurableAuditStatus.UNKNOWN


def test_non_sqlite_database_is_corrupt(tmp_path: Path) -> None:
    path = (tmp_path / "not-sqlite.sqlite3").resolve()
    path.write_bytes(b"not a sqlite database")
    path.chmod(0o600)
    result = SQLiteCutoverDurableAuditor(SqliteDatabase(path)).inspect()
    assert result.status is SQLiteCutoverDurableAuditStatus.CORRUPT


def test_unsupported_post_mount_suffix_is_unknown(tmp_path: Path) -> None:
    baseline = create_canonical_mounted_business_database(
        tmp_path / "post-mount.sqlite3"
    )
    result = SQLiteCutoverDurableAuditor(baseline.database).inspect()
    assert result.status is SQLiteCutoverDurableAuditStatus.UNKNOWN
    assert result.code == "cutover_durable_audit_post_mount_suffix_unsupported"


def test_api_rejects_forged_expected_before_open(tmp_path: Path) -> None:
    database = SqliteDatabase((tmp_path / "never-open.sqlite3").resolve())
    with pytest.raises(TypeError, match="expected"):
        SQLiteCutoverDurableAuditor(database).inspect(expected=object())
