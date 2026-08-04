"""Independent runtime evidence for the plan-free V12 cutover auditor.

The probe uses the real install writer and real SQLite files.  It is not a
production caller and exposes no writer/auditor capability outside the gate.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
import sqlite3
import tempfile
from typing import Optional, Tuple

import sqlite_cutover_durable_audit as audit_module
import sqlite_cutover_verification as verification_module
from receipt_namespace_contract import ReceiptNamespaceEntry
from receipt_namespace_integrity import _receipt_namespace_sha256
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
)
from sqlite_business_audit import (
    _main_storage_identity,
    _read_receipt_namespace_identity,
)
from sqlite_cutover_durable_audit_test_support import (
    create_canonical_cutover_phase_database,
)
from sqlite_cutover_install_test_support import (
    _create_empty_cutover_install_fixture,
    _create_live_cutover_install_fixture,
    _make_cutover_install_writer,
)
from sqlite_kernel import SqliteDatabase
from sqlite_production_authority import _CUTOVER_HEAD_ROW_COLUMNS
from sqlite_schema import MIGRATIONS, _expected_migration_ledger_sha256
from sqlite_worker_claim_audit_probe import _claim_fixture
from sqlite_worker_claim_writer import SQLiteWorkerClaimWriter


@dataclass(frozen=True)
class CutoverDurableAuditEvidence:
    absent_status: str
    journal_drift_status: str
    journal_mode_preserved: bool
    preinstall_status: str
    preinstall_code: str
    stage_only_status: str
    stage_only_code: str
    mixed_preinstall_status: str
    installed_status: str
    installed_phase: str
    installed_head_count: int
    installed_identity_exact: bool
    expected_status: str
    conflict_status: str
    phase_statuses: Tuple[str, ...]
    phase_names: Tuple[str, ...]
    phase_identity_exact: Tuple[bool, ...]
    mounted_fence_populated: bool
    mounted_fence_exact: bool
    tamper_status: str
    blob_status: str
    scheduler_tamper_status: str
    extra_row_status: str
    future_runtime_status: str
    closure_io_status: str
    bad_audit_status: str
    install_root_tamper_status: str
    periodic_status: str
    periodic_code: str
    periodic_arbitrary_root_status: str
    periodic_arbitrary_root_code: str
    periodic_future_high_water_status: str
    periodic_future_high_water_code: str
    worker_periodic_status: str
    worker_periodic_code: str
    worker_head_rollback_status: str
    transient_structural_status: str
    transient_structural_code: str
    transient_structural_calls: int
    stable_structural_status: str
    stable_structural_code: str
    stable_structural_calls: int
    structural_cleanup_status: str
    structural_cleanup_code: str
    budget_status: str
    budget_code: str
    text_budget_status: str
    text_budget_code: str
    progress_budget_status: str
    fence_status: str
    fence_code: str
    fence_changed_fields: Tuple[str, ...]
    durability_call_count: int
    durability_statuses: Tuple[str, str]
    durability_failure_statuses: Tuple[str, str]
    durability_failure_codes: Tuple[str, str]
    process_control_preserved: bool
    cleanup_process_control_preserved: bool
    cleanup_process_control_closed: bool
    progress_process_control_preserved: bool


def _head_count(database: SqliteDatabase) -> int:
    connection = None
    try:
        connection, _filesystem = database._open_unvalidated()
        return int(
            connection.execute(
                "SELECT count(*) FROM business_cutover_head"
            ).fetchone()[0]
        )
    finally:
        if connection is not None:
            connection.close()


def _identity_matches_database(database: SqliteDatabase, result) -> bool:
    identity = result.identity
    if identity is None:
        return False
    connection, _filesystem = database._open_unvalidated()
    try:
        connection.execute("BEGIN")

        def optional(sql: str):
            rows = connection.execute(sql).fetchall()
            if len(rows) > 1:
                return False
            return None if not rows else rows[0]

        runtime = connection.execute(
            "SELECT authority_state,repository_epoch,cutover_id,"
            "source_admission_revision,repository_revision,state_revision,"
            "runtime_generation,code_digest FROM repository_runtime_control "
            "WHERE singleton=1"
        ).fetchone()
        install = connection.execute(
            "SELECT transaction_id,manifest_sha256 "
            "FROM business_cutover_install_receipts"
        ).fetchone()
        closure = connection.execute(
            "SELECT manifest_sha256,source_proof_sha256,source_admission_sha256,"
            "baseline_outer_receipt_root_sha256 "
            "FROM business_cutover_install_closures"
        ).fetchone()
        coverage = connection.execute(
            "SELECT transaction_id,manifest_sha256,coverage_sha256 "
            "FROM business_asset_coverage_receipts"
        ).fetchone()
        head = optional("SELECT head_sha256 FROM business_cutover_head")
        audit = optional(
            "SELECT transaction_id,manifest_sha256 "
            "FROM repository_audit_certificates ORDER BY sequence LIMIT 1"
        )
        activation = optional(
            "SELECT transaction_id,manifest_sha256 "
            "FROM business_cutover_activation_receipts"
        )
        mount = optional(
            "SELECT transaction_id,manifest_sha256 "
            "FROM business_cutover_mount_receipts"
        )
        receipt_count, receipt_root = _read_receipt_namespace_identity(connection)
        if any(value is None for value in (runtime, install, closure, coverage)):
            return False
        return (
            identity.cutover_id == runtime[2]
            and identity.repository_epoch == runtime[1]
            and identity.source_admission_revision == runtime[3]
            and identity.runtime_generation == runtime[6]
            and identity.code_digest == runtime[7]
            and identity.authority_state == runtime[0]
            and identity.repository_revision == runtime[4]
            and identity.runtime_state_revision == runtime[5]
            and identity.install_transaction_id == install[0]
            and identity.install_manifest_sha256 == install[1]
            and identity.closure_manifest_sha256 == closure[0]
            and identity.source_proof_sha256 == closure[1]
            and identity.source_admission_sha256 == closure[2]
            and identity.baseline_receipt_root_sha256 == closure[3]
            and identity.coverage_transaction_id == coverage[0]
            and identity.coverage_manifest_sha256 == coverage[1]
            and identity.coverage_sha256 == coverage[2]
            and identity.head_sha256 == (None if head is None else head[0])
            and identity.audit_transaction_id
            == (None if audit is None else audit[0])
            and identity.audit_manifest_sha256
            == (None if audit is None else audit[1])
            and identity.activation_transaction_id
            == (None if activation is None else activation[0])
            and identity.activation_manifest_sha256
            == (None if activation is None else activation[1])
            and identity.mount_transaction_id
            == (None if mount is None else mount[0])
            and identity.mount_manifest_sha256
            == (None if mount is None else mount[1])
            and identity.schema_fingerprint
            == audit_module._expected_schema_fingerprint()
            and identity.migration_ledger_sha256
            == _expected_migration_ledger_sha256()
            and identity.receipt_namespace_count == receipt_count
            and identity.receipt_namespace_sha256 == receipt_root
        )
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()


def _fence_matches_database(database: SqliteDatabase, fence) -> bool:
    storage_identity = _main_storage_identity(database, readonly=True)
    connection, _filesystem = database._open_readonly_unvalidated()
    try:
        connection.execute("BEGIN")

        def rows(sql: str):
            return tuple(tuple(row) for row in connection.execute(sql))

        def optional(sql: str):
            value = rows(sql)
            return None if not value else value[0] if len(value) == 1 else False

        receipt_count, receipt_root = _read_receipt_namespace_identity(connection)
        business_high = connection.execute(
            "SELECT count(*),max(repository_revision),"
            "(SELECT manifest_sha256 FROM business_commits "
            "ORDER BY repository_revision DESC LIMIT 1) FROM business_commits"
        ).fetchone()
        return (
            fence.storage_identity == storage_identity
            and fence.schema_fingerprint
            == rows(
                "SELECT value FROM kernel_metadata "
                "WHERE key='schema_fingerprint'"
            )
            and fence.marker
            == rows(
                "SELECT singleton,authority,created_at FROM kernel_repository "
                "ORDER BY singleton"
            )
            and fence.runtime
            == rows(
                "SELECT singleton,authority,authority_state,repository_epoch,"
                "cutover_id,source_admission_revision,repository_revision,"
                "state_revision,runtime_generation,code_digest,schema_version,"
                "updated_at FROM repository_runtime_control ORDER BY singleton"
            )
            and fence.install
            == rows(
                "SELECT cutover_id,transaction_id,committed_at,manifest_sha256 "
                "FROM business_cutover_install_receipts ORDER BY cutover_id"
            )
            and fence.closure
            == rows(
                "SELECT cutover_id,install_transaction_id,install_manifest_sha256,"
                "coverage_transaction_id,coverage_manifest_sha256,manifest_sha256 "
                "FROM business_cutover_install_closures ORDER BY cutover_id"
            )
            and fence.cutover_head
            == optional(
                "SELECT "
                + ",".join(_CUTOVER_HEAD_ROW_COLUMNS)
                + " FROM business_cutover_head"
            )
            and fence.audit_head
            == optional(
                "SELECT singleton,state_revision,sequence,certificate_id,"
                "previous_tail_sha256,tail_sha256,updated_at "
                "FROM repository_audit_head"
            )
            and fence.worker_head
            == optional(
                "SELECT singleton,state_revision,sequence,previous_tail_sha256,"
                "tail_sha256,updated_at FROM worker_ledger_head"
            )
            and fence.allocation_head
            == optional(
                "SELECT singleton,state_revision,sequence,previous_tail_sha256,"
                "tail_sha256,updated_at FROM outbox_allocation_head"
            )
            and fence.dispatch_head
            == optional(
                "SELECT singleton,state_revision,published_sequence,"
                "published_repository_revision,published_ordinal,inflight_sequence,"
                "inflight_repository_revision,inflight_ordinal,previous_head_sha256,"
                "head_sha256,updated_at FROM outbox_dispatch_head"
            )
            and fence.receipt_identity
            == ("canonical", receipt_count, receipt_root)
            and business_high is not None
            and fence.business_high_water == tuple(business_high)
        )
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()


def _tamper_artifact(database: SqliteDatabase) -> None:
    connection = None
    try:
        connection, _filesystem = database._open_unvalidated()
        connection.execute("BEGIN IMMEDIATE")
        trigger = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND "
            "name='business_cutover_install_artifact_chunks_no_update'"
        ).fetchone()
        if trigger is None or type(trigger[0]) is not str:
            raise AssertionError("cutover audit probe trigger missing")
        connection.execute(
            "DROP TRIGGER business_cutover_install_artifact_chunks_no_update"
        )
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
        connection.execute(trigger[0])
        connection.commit()
    finally:
        if connection is not None:
            if connection.in_transaction:
                connection.rollback()
            connection.close()


def _restore_table_triggers(connection, table: str, mutate) -> None:
    rows = connection.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name=? "
        "ORDER BY name",
        (table,),
    ).fetchall()
    if not rows or any(type(row[0]) is not str or type(row[1]) is not str for row in rows):
        raise AssertionError("cutover audit probe trigger set missing")
    for row in rows:
        connection.execute('DROP TRIGGER "' + row[0] + '"')
    mutate()
    for row in rows:
        connection.execute(row[1])


def _make_stage_only(database: SqliteDatabase) -> None:
    connection, _filesystem = database._open_unvalidated()
    try:
        connection.execute("BEGIN IMMEDIATE")
        coverage_transaction_id = connection.execute(
            "SELECT transaction_id FROM business_asset_coverage_receipts"
        ).fetchone()[0]
        _restore_table_triggers(
            connection,
            "business_asset_coverage_receipts",
            lambda: connection.execute(
                "DELETE FROM business_asset_coverage_receipts"
            ),
        )
        _restore_table_triggers(
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


def _make_mixed_preinstall(database: SqliteDatabase) -> None:
    connection, _filesystem = database._open_unvalidated()
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT cutover_id,transaction_id FROM business_asset_stage_receipts"
        ).fetchone()
        if row is None:
            raise AssertionError("mixed preinstall probe requires one stage")
        foreign_cutover_id = "0" * 64 if row[0] != "0" * 64 else "1" * 64
        suffix = row[1].rsplit(":", 1)[1]
        transaction_id = (
            "_business-cutover-asset-stage:"
            + foreign_cutover_id
            + ":"
            + suffix
        )
        _restore_table_triggers(
            connection,
            "transaction_receipts",
            lambda: connection.execute(
                "UPDATE transaction_receipts SET transaction_id=? "
                "WHERE transaction_id=?",
                (transaction_id, row[1]),
            ),
        )
        _restore_table_triggers(
            connection,
            "business_asset_stage_receipts",
            lambda: connection.execute(
                "UPDATE business_asset_stage_receipts "
                "SET cutover_id=?,transaction_id=?",
                (foreign_cutover_id, transaction_id),
            ),
        )
        _restore_table_triggers(
            connection,
            "business_asset_stage_members",
            lambda: connection.execute(
                "UPDATE business_asset_stage_members SET cutover_id=?",
                (foreign_cutover_id,),
            ),
        )
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise AssertionError("mixed preinstall probe lost relational closure")
        connection.commit()
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()


def _append_periodic_audit(
    database: SqliteDatabase,
    *,
    receipt_root_override: Optional[bytes] = None,
    high_water_delta: int = 0,
) -> None:
    connection, _filesystem = database._open_unvalidated()
    try:
        connection.execute("BEGIN IMMEDIATE")
        first_row = connection.execute(
            "SELECT manifest_bytes FROM repository_audit_certificates "
            "WHERE sequence=1"
        ).fetchone()
        if first_row is None:
            raise AssertionError("periodic probe requires install audit")
        first = audit_module._audit_evidence_from_manifest(first_row[0]).evidence
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
            ReceiptNamespaceEntry(row[0], row[1], row[2])
            for row in receipt_rows
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
                schema_fingerprint=audit_module._expected_schema_fingerprint(),
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
            + ",".join(audit_module._AUDIT_ROW_COLUMNS)
            + ") VALUES ("
            + ",".join("?" for _ in audit_module._AUDIT_ROW_COLUMNS)
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


def _add_noncausal_generic_receipt(database: SqliteDatabase) -> None:
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


def _tamper_mounted_scheduler(database: SqliteDatabase) -> None:
    connection, _filesystem = database._open_unvalidated()
    try:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name IN "
            "('worker_ledger_head_cas_update_guard',"
            "'worker_ledger_head_sequence_guard') ORDER BY name"
        ).fetchall()
        if len(rows) != 2:
            raise AssertionError("scheduler probe trigger missing")
        for row in rows:
            connection.execute('DROP TRIGGER "' + row[0] + '"')
        connection.execute(
            "UPDATE worker_ledger_head SET state_revision=7 WHERE singleton=1"
        )
        for row in rows:
            connection.execute(row[1])
        connection.commit()
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()


def _add_unbound_repository_row(database: SqliteDatabase) -> None:
    connection, _filesystem = database._open_unvalidated()
    try:
        connection.execute("BEGIN IMMEDIATE")
        committed_at = connection.execute(
            "SELECT committed_at FROM business_cutover_mount_receipts"
        ).fetchone()[0]
        _restore_table_triggers(
            connection,
            "repository_projects",
            lambda: connection.execute(
                "INSERT INTO repository_projects("
                "repository_singleton,project_id,generation,project_revision,status,"
                "current_snapshot_sha256,created_at,updated_at,deleted_at) "
                "VALUES (1,'probe-unbound',1,1,'deleted',NULL,?,?,?)",
                (committed_at, committed_at, committed_at),
            ),
        )
        connection.commit()
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()


def _tamper_runtime_time(database: SqliteDatabase) -> None:
    connection, _filesystem = database._open_unvalidated()
    try:
        connection.execute("BEGIN IMMEDIATE")
        _restore_table_triggers(
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


def _append_malformed_audit(database: SqliteDatabase) -> None:
    connection, _filesystem = database._open_unvalidated()
    try:
        connection.execute("BEGIN IMMEDIATE")
        columns = audit_module._AUDIT_ROW_COLUMNS
        first = connection.execute(
            "SELECT " + ",".join(columns)
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
        _restore_table_triggers(
            connection,
            "repository_audit_certificates",
            lambda: connection.execute(
                "INSERT INTO repository_audit_certificates("
                + ",".join(columns)
                + ") VALUES ("
                + ",".join("?" for _ in columns)
                + ")",
                tuple(values),
            ),
        )
        _restore_table_triggers(
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


def run_cutover_durable_audit_probe() -> CutoverDurableAuditEvidence:
    with tempfile.TemporaryDirectory(prefix="ze-cutover-durable-audit-") as tmp:
        root = Path(tmp).resolve()
        pristine = SqliteDatabase(root / "absent.sqlite3")
        pristine.initialize()
        absent = SQLiteCutoverDurableAuditor(pristine).inspect()

        journal_drift_database = SqliteDatabase(root / "journal-drift.sqlite3")
        journal_drift_database.initialize()
        raw = sqlite3.connect(str(journal_drift_database.path))
        try:
            if raw.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                raise AssertionError("journal drift probe could not install drift")
        finally:
            raw.close()
        journal_drift = SQLiteCutoverDurableAuditor(
            journal_drift_database
        ).inspect()
        raw = sqlite3.connect(str(journal_drift_database.path))
        try:
            journal_mode_preserved = (
                raw.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
            )
        finally:
            raw.close()

        fixture = _create_empty_cutover_install_fixture(root / "installed.sqlite3")
        auditor = SQLiteCutoverDurableAuditor(fixture.database)
        preinstall = auditor.inspect()

        stage_only_fixture = _create_live_cutover_install_fixture(
            root / "stage-only.sqlite3",
            asset_blob=b"stage-only-probe",
        )
        _make_stage_only(stage_only_fixture.database)
        stage_only = SQLiteCutoverDurableAuditor(
            stage_only_fixture.database
        ).inspect()

        mixed_fixture = _create_live_cutover_install_fixture(
            root / "mixed-preinstall.sqlite3",
            asset_blob=b"mixed-preinstall-probe",
        )
        _make_mixed_preinstall(mixed_fixture.database)
        mixed_preinstall = SQLiteCutoverDurableAuditor(
            mixed_fixture.database
        ).inspect()

        installed_write = _make_cutover_install_writer(fixture).install(
            fixture.projection
        )
        if installed_write.status.value != "installed_read_only":
            raise AssertionError("real install writer did not reach read-only state")
        installed = auditor.inspect()
        if installed.identity is None:
            raise AssertionError("installed audit did not return an identity")
        expected = auditor.inspect(expected=installed.identity)
        conflict = auditor.inspect(
            expected=replace(
                installed.identity,
                repository_revision=installed.identity.repository_revision + 1,
            )
        )

        original_durability = SqliteDatabase._finalize_readonly_storage
        durability_calls = 0
        try:
            def counted_durability(self):
                nonlocal durability_calls
                durability_calls += 1
                return original_durability(self)

            SqliteDatabase._finalize_readonly_storage = counted_durability
            durability_statuses = (
                auditor.inspect().status.value,
                SQLiteCutoverDurableAuditor(pristine).inspect().status.value,
            )
        finally:
            SqliteDatabase._finalize_readonly_storage = original_durability
        try:
            def failed_durability(_self):
                raise OSError("cutover durable audit probe durability")

            SqliteDatabase._finalize_readonly_storage = failed_durability
            durability_failures = (
                auditor.inspect(),
                SQLiteCutoverDurableAuditor(pristine).inspect(),
            )
        finally:
            SqliteDatabase._finalize_readonly_storage = original_durability

        phase_databases = tuple(
            create_canonical_cutover_phase_database(
                root / ("phase-" + str(revision) + ".sqlite3"),
                phase_revision=revision,
            )
            for revision in range(1, 6)
        )
        phase_results = tuple(
            SQLiteCutoverDurableAuditor(database).inspect()
            for database in phase_databases
        )
        if any(value.identity is None for value in phase_results):
            raise AssertionError("phase audit lost a durable identity")
        mounted_fence = SQLiteCutoverDurableAuditor(
            phase_databases[-1]
        )._fresh_fence()
        mounted_fence_populated = (
            bool(mounted_fence.storage_identity)
            and bool(mounted_fence.schema_fingerprint)
            and bool(mounted_fence.marker)
            and mounted_fence.runtime is not None
            and mounted_fence.install is not None
            and mounted_fence.closure is not None
            and mounted_fence.cutover_head is not None
            and mounted_fence.audit_head is not None
            and mounted_fence.worker_head is not None
            and mounted_fence.allocation_head is not None
            and mounted_fence.dispatch_head is not None
            and mounted_fence.receipt_identity[0] == "canonical"
            and len(mounted_fence.business_high_water) == 3
        )
        mounted_fence_exact = _fence_matches_database(
            phase_databases[-1],
            mounted_fence,
        )

        tampered_fixture = _create_empty_cutover_install_fixture(
            root / "tampered.sqlite3"
        )
        _make_cutover_install_writer(tampered_fixture).install(
            tampered_fixture.projection
        )
        _tamper_artifact(tampered_fixture.database)
        tampered = SQLiteCutoverDurableAuditor(
            tampered_fixture.database
        ).inspect()

        blob_fixture = _create_empty_cutover_install_fixture(root / "blob.sqlite3")
        _make_cutover_install_writer(blob_fixture).install(blob_fixture.projection)
        connection, _filesystem = blob_fixture.database._open_unvalidated()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO transaction_receipts(transaction_id,kind,committed_at) "
                "VALUES (CAST(? AS BLOB),?,?)",
                (
                    "_business-cutover-join:" + "a" * 64,
                    "ordinary-kind",
                    blob_fixture.install_committed_at,
                ),
            )
            connection.commit()
        finally:
            if connection.in_transaction:
                connection.rollback()
            connection.close()
        blob = SQLiteCutoverDurableAuditor(blob_fixture.database).inspect()

        scheduler_database = create_canonical_cutover_phase_database(
            root / "scheduler-tamper.sqlite3",
            phase_revision=5,
        )
        _tamper_mounted_scheduler(scheduler_database)
        scheduler_tamper = SQLiteCutoverDurableAuditor(
            scheduler_database
        ).inspect()

        extra_row_database = create_canonical_cutover_phase_database(
            root / "extra-row.sqlite3",
            phase_revision=5,
        )
        _add_unbound_repository_row(extra_row_database)
        extra_row = SQLiteCutoverDurableAuditor(extra_row_database).inspect()

        future_runtime_database = create_canonical_cutover_phase_database(
            root / "future-runtime.sqlite3",
            phase_revision=5,
        )
        _tamper_runtime_time(future_runtime_database)
        future_runtime = SQLiteCutoverDurableAuditor(
            future_runtime_database
        ).inspect()

        original_closure_reader = audit_module._read_business_cutover_install_closure
        try:
            def closure_unavailable(*_args, **_kwargs):
                try:
                    raise OSError("cutover durable audit probe I/O")
                except OSError as exc:
                    raise audit_module.SQLiteCutoverInstallClosureError(
                        "cutover_install_closure_unavailable"
                    ) from exc

            audit_module._read_business_cutover_install_closure = closure_unavailable
            closure_io = auditor.inspect()
        finally:
            audit_module._read_business_cutover_install_closure = original_closure_reader

        bad_audit_database = create_canonical_cutover_phase_database(
            root / "bad-audit.sqlite3",
            phase_revision=5,
        )
        _append_malformed_audit(bad_audit_database)
        bad_audit = SQLiteCutoverDurableAuditor(bad_audit_database).inspect()

        install_root_database = create_canonical_cutover_phase_database(
            root / "install-root-tamper.sqlite3",
            phase_revision=5,
        )
        _add_noncausal_generic_receipt(install_root_database)
        install_root_tamper = SQLiteCutoverDurableAuditor(
            install_root_database
        ).inspect()

        periodic_database = create_canonical_cutover_phase_database(
            root / "periodic.sqlite3",
            phase_revision=5,
        )
        _append_periodic_audit(periodic_database)
        periodic = SQLiteCutoverDurableAuditor(periodic_database).inspect()

        periodic_arbitrary_root_database = create_canonical_cutover_phase_database(
            root / "periodic-arbitrary-root.sqlite3",
            phase_revision=5,
        )
        _append_periodic_audit(
            periodic_arbitrary_root_database,
            receipt_root_override=b"q" * 32,
        )
        periodic_arbitrary_root = SQLiteCutoverDurableAuditor(
            periodic_arbitrary_root_database
        ).inspect()

        periodic_future_high_water_database = (
            create_canonical_cutover_phase_database(
                root / "periodic-future-high-water.sqlite3",
                phase_revision=5,
            )
        )
        _append_periodic_audit(
            periodic_future_high_water_database,
            high_water_delta=100,
        )
        periodic_future_high_water = SQLiteCutoverDurableAuditor(
            periodic_future_high_water_database
        ).inspect()

        worker_baseline, worker_claim = _claim_fixture(
            root,
            "worker-periodic.sqlite3",
        )
        connection, _filesystem = worker_baseline.database._open_unvalidated()
        try:
            worker_genesis = tuple(connection.execute(
                "SELECT singleton,state_revision,sequence,previous_tail_sha256,"
                "tail_sha256,updated_at FROM worker_ledger_head WHERE singleton=1"
            ).fetchone())
        finally:
            connection.close()
        worker_claim_result = SQLiteWorkerClaimWriter(
            worker_baseline.database
        ).commit(worker_claim)
        if worker_claim_result.status.value != "committed":
            raise AssertionError("worker periodic probe did not commit its claim")
        _append_periodic_audit(worker_baseline.database)
        worker_periodic = SQLiteCutoverDurableAuditor(
            worker_baseline.database
        ).inspect()
        connection, _filesystem = worker_baseline.database._open_unvalidated()
        try:
            connection.execute("BEGIN IMMEDIATE")
            _restore_table_triggers(
                connection,
                "worker_ledger_head",
                lambda: connection.execute(
                    "UPDATE worker_ledger_head SET state_revision=?,sequence=?,"
                    "previous_tail_sha256=?,tail_sha256=?,updated_at=? "
                    "WHERE singleton=?",
                    (
                        worker_genesis[1],
                        worker_genesis[2],
                        worker_genesis[3],
                        worker_genesis[4],
                        worker_genesis[5],
                        worker_genesis[0],
                    ),
                ),
            )
            connection.commit()
        finally:
            if connection.in_transaction:
                connection.rollback()
            connection.close()
        worker_head_rollback = SQLiteCutoverDurableAuditor(
            worker_baseline.database
        ).inspect()

        original_structure = audit_module._verify_schema_and_marker
        transient_calls = 0
        try:
            def transient_structure(*args, **kwargs):
                nonlocal transient_calls
                transient_calls += 1
                if transient_calls == 1:
                    raise audit_module._AuditCorrupt(
                        "cutover_durable_audit_probe_transient_structure"
                    )
                return original_structure(*args, **kwargs)

            audit_module._verify_schema_and_marker = transient_structure
            transient_structural = auditor.inspect()
        finally:
            audit_module._verify_schema_and_marker = original_structure

        stable_calls = 0
        try:
            def stable_structure(*_args, **_kwargs):
                nonlocal stable_calls
                stable_calls += 1
                raise audit_module._AuditCorrupt(
                    "cutover_durable_audit_probe_stable_structure"
                )

            audit_module._verify_schema_and_marker = stable_structure
            stable_structural = auditor.inspect()
        finally:
            audit_module._verify_schema_and_marker = original_structure

        original_cleanup = audit_module._cleanup
        try:
            def failed_cleanup(connection):
                original_cleanup(connection)
                return False

            def cleanup_structure(*_args, **_kwargs):
                raise audit_module._AuditCorrupt(
                    "cutover_durable_audit_probe_cleanup_structure"
                )

            audit_module._verify_schema_and_marker = cleanup_structure
            audit_module._cleanup = failed_cleanup
            structural_cleanup = auditor.inspect()
        finally:
            audit_module._verify_schema_and_marker = original_structure
            audit_module._cleanup = original_cleanup

        original_budget = verification_module.MAX_CUTOVER_DURABLE_AUDIT_ENTRIES
        try:
            verification_module.MAX_CUTOVER_DURABLE_AUDIT_ENTRIES = 0
            budget = auditor.inspect()
        finally:
            verification_module.MAX_CUTOVER_DURABLE_AUDIT_ENTRIES = original_budget

        text_budget_database = SqliteDatabase(root / "text-budget.sqlite3")
        text_budget_database.initialize()
        connection, _filesystem = text_budget_database._open_unvalidated()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO transaction_receipts(transaction_id,kind,committed_at) "
                "VALUES (?,?,?)",
                (
                    "ordinary-text-budget",
                    "x" * 128,
                    "2026-01-01T00:00:00.000Z",
                ),
            )
            connection.commit()
        finally:
            if connection.in_transaction:
                connection.rollback()
            connection.close()
        original_byte_budget = verification_module.MAX_CUTOVER_DURABLE_AUDIT_BYTES
        try:
            verification_module.MAX_CUTOVER_DURABLE_AUDIT_BYTES = 1
            text_budget = SQLiteCutoverDurableAuditor(
                text_budget_database
            ).inspect()
        finally:
            verification_module.MAX_CUTOVER_DURABLE_AUDIT_BYTES = original_byte_budget

        original_vm_budget = verification_module.MAX_CUTOVER_DURABLE_AUDIT_VM_STEPS
        try:
            verification_module.MAX_CUTOVER_DURABLE_AUDIT_VM_STEPS = 0
            progress_budget = auditor.inspect()
        finally:
            verification_module.MAX_CUTOVER_DURABLE_AUDIT_VM_STEPS = original_vm_budget

        original_fence = audit_module.SQLiteCutoverDurableAuditor._fresh_fence
        fence_results = []
        try:
            for field in audit_module._AuditFence._fields:
                def changed_fence(self, field=field):
                    value = original_fence(self)
                    return value._replace(**{field: ("changed", field)})

                audit_module.SQLiteCutoverDurableAuditor._fresh_fence = (
                    changed_fence
                )
                fence_results.append((field, auditor.inspect()))
        finally:
            audit_module.SQLiteCutoverDurableAuditor._fresh_fence = original_fence
        fenced = fence_results[0][1]
        fence_changed_fields = tuple(
            field
            for field, result in fence_results
            if result.status.value == "unknown"
            and result.code == "cutover_durable_audit_publish_fence_changed"
        )

        original_snapshot = audit_module._snapshot_audit
        process_control = False
        try:
            def interrupt(*_args, **_kwargs):
                raise KeyboardInterrupt("cutover durable audit probe")

            audit_module._snapshot_audit = interrupt
            try:
                auditor.inspect()
            except KeyboardInterrupt as exc:
                process_control = str(exc) == "cutover durable audit probe"
        finally:
            audit_module._snapshot_audit = original_snapshot

        original_cleanup = audit_module._cleanup
        cleanup_process_control = False
        cleanup_process_control_closed = False
        cleanup_connections = []
        try:
            def interrupt_cleanup(connection):
                cleanup_connections.append(connection)
                raise KeyboardInterrupt(
                    "cutover durable audit probe cleanup process-control"
                )

            audit_module._cleanup = interrupt_cleanup
            try:
                auditor.inspect()
            except KeyboardInterrupt as exc:
                cleanup_process_control = str(exc) == (
                    "cutover durable audit probe cleanup process-control"
                )
        finally:
            audit_module._cleanup = original_cleanup
        if cleanup_connections:
            try:
                cleanup_connections[0].execute("SELECT 1")
            except Exception as exc:
                cleanup_process_control_closed = (
                    getattr(exc, "code", None) == "sqlite_connection_closed"
                )

        original_progress_budget = audit_module._install_progress_budget
        progress_process_control = False
        try:
            class InterruptOnDecrement:
                def __sub__(self, _value):
                    raise KeyboardInterrupt(
                        "cutover durable audit probe progress process-control"
                    )

            def interrupting_progress(connection):
                state = original_progress_budget(connection)
                state.remaining = InterruptOnDecrement()
                return state

            audit_module._install_progress_budget = interrupting_progress
            try:
                auditor.inspect()
            except KeyboardInterrupt as exc:
                progress_process_control = str(exc) == (
                    "cutover durable audit probe progress process-control"
                )
        finally:
            audit_module._install_progress_budget = original_progress_budget

        return CutoverDurableAuditEvidence(
            absent_status=absent.status.value,
            journal_drift_status=journal_drift.status.value,
            journal_mode_preserved=journal_mode_preserved,
            preinstall_status=preinstall.status.value,
            preinstall_code=preinstall.code,
            stage_only_status=stage_only.status.value,
            stage_only_code=stage_only.code,
            mixed_preinstall_status=mixed_preinstall.status.value,
            installed_status=installed.status.value,
            installed_phase=installed.identity.phase.value,
            installed_head_count=_head_count(fixture.database),
            installed_identity_exact=_identity_matches_database(
                fixture.database,
                installed,
            ),
            expected_status=expected.status.value,
            conflict_status=conflict.status.value,
            phase_statuses=tuple(value.status.value for value in phase_results),
            phase_names=tuple(value.identity.phase.value for value in phase_results),
            phase_identity_exact=tuple(
                _identity_matches_database(database, result)
                for database, result in zip(phase_databases, phase_results)
            ),
            mounted_fence_populated=mounted_fence_populated,
            mounted_fence_exact=mounted_fence_exact,
            tamper_status=tampered.status.value,
            blob_status=blob.status.value,
            scheduler_tamper_status=scheduler_tamper.status.value,
            extra_row_status=extra_row.status.value,
            future_runtime_status=future_runtime.status.value,
            closure_io_status=closure_io.status.value,
            bad_audit_status=bad_audit.status.value,
            install_root_tamper_status=install_root_tamper.status.value,
            periodic_status=periodic.status.value,
            periodic_code=periodic.code,
            periodic_arbitrary_root_status=periodic_arbitrary_root.status.value,
            periodic_arbitrary_root_code=periodic_arbitrary_root.code,
            periodic_future_high_water_status=(
                periodic_future_high_water.status.value
            ),
            periodic_future_high_water_code=periodic_future_high_water.code,
            worker_periodic_status=worker_periodic.status.value,
            worker_periodic_code=worker_periodic.code,
            worker_head_rollback_status=worker_head_rollback.status.value,
            transient_structural_status=transient_structural.status.value,
            transient_structural_code=transient_structural.code,
            transient_structural_calls=transient_calls,
            stable_structural_status=stable_structural.status.value,
            stable_structural_code=stable_structural.code,
            stable_structural_calls=stable_calls,
            structural_cleanup_status=structural_cleanup.status.value,
            structural_cleanup_code=structural_cleanup.code,
            budget_status=budget.status.value,
            budget_code=budget.code,
            text_budget_status=text_budget.status.value,
            text_budget_code=text_budget.code,
            progress_budget_status=progress_budget.status.value,
            fence_status=fenced.status.value,
            fence_code=fenced.code,
            fence_changed_fields=fence_changed_fields,
            durability_call_count=durability_calls,
            durability_statuses=durability_statuses,
            durability_failure_statuses=tuple(
                value.status.value for value in durability_failures
            ),
            durability_failure_codes=tuple(
                value.code for value in durability_failures
            ),
            process_control_preserved=process_control,
            cleanup_process_control_preserved=cleanup_process_control,
            cleanup_process_control_closed=cleanup_process_control_closed,
            progress_process_control_preserved=progress_process_control,
        )


__all__ = ()
