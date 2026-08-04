"""Focused durable evidence for the strict legacy terminal-shadow adapter."""

from __future__ import annotations

import hashlib
import sqlite3
import sys
import threading
from pathlib import Path
from typing import NamedTuple, Optional, Tuple

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

import legacy_catalog_contract as catalog_contract  # noqa: E402
import sqlite_kernel as kernel  # noqa: E402
import sqlite_legacy_catalog as catalog_adapter  # noqa: E402
import sqlite_legacy_import as adapter  # noqa: E402
from business_snapshot import capture_business_snapshot  # noqa: E402
from document import Document  # noqa: E402
from entity import Entity  # noqa: E402
from legacy_import_contract import (  # noqa: E402
    LEGACY_BATCH_PROTOCOL,
    LEGACY_IMPORT_PLAN_PROTOCOL,
    LEGACY_MANIFEST_SCHEMA,
    LEGACY_PAYLOAD_PROTOCOL,
    SQLITE_IMPORT_RECEIPT_PROTOCOL,
    SQLITE_IMPORT_TERMINAL_STATUS,
    SQLITE_IMPORT_TRANSACTION_KIND,
    SQLITE_IMPORT_TRANSACTION_PREFIX,
)
from legacy_import_integrity import (  # noqa: E402
    LegacyImportIntegrityProject,
    build_legacy_import_integrity,
)


_SCHEMA_FINGERPRINT_V4 = kernel._expected_schema_fingerprint(4)


class ProjectRow(NamedTuple):
    project_id: str
    generation: int
    codec_id: str
    codec_version: int
    snapshot_format_version: int
    snapshot_bytes: bytes
    snapshot_sha256: bytes


class DocumentRow(NamedTuple):
    project_id: str
    document_id: str
    document_version: int
    schema_version: str
    collaboration_mode: str
    save_policy: Optional[str]
    codec_id: str
    codec_version: int
    snapshot_format_version: int
    snapshot_bytes: bytes
    snapshot_sha256: bytes


class EntityRow(NamedTuple):
    project_id: str
    document_id: str
    entity_type: str
    entity_id: str
    entity_version: int
    deleted: int
    codec_id: str
    codec_version: int
    snapshot_format_version: int
    snapshot_bytes: bytes
    snapshot_sha256: bytes


class HookedEntityRow(EntityRow):
    """Adversarial tuple row whose Python properties must never be consulted."""

    __slots__ = ()

    @property
    def snapshot_bytes(self) -> bytes:
        raise AssertionError("entity row property hook executed")

    @property
    def snapshot_sha256(self) -> bytes:
        raise AssertionError("entity row property hook executed")


class HookedEntityRows(tuple):
    """Adversarial tuple container; only base tuple storage is trustworthy."""

    __slots__ = ()

    def __iter__(self):
        raise AssertionError("entity row container iterator hook executed")

    def __len__(self) -> int:
        raise AssertionError("entity row container length hook executed")

    def __getitem__(self, _index):
        raise AssertionError("entity row container item hook executed")


class ProjectPlan(NamedTuple):
    ordinal: int
    project_id: str
    generation: int
    document_id: str
    document_version: int
    storage_version: str
    source_head_sha256: bytes
    source_head_bytes: int
    business_root_sha256: bytes
    entity_count: int
    row_snapshot_bytes: int
    project_row: ProjectRow
    document_row: DocumentRow
    entity_rows: Tuple[EntityRow, ...]


class ImportPlan(NamedTuple):
    plan_protocol: str
    import_id: str
    transaction_id: str
    receipt_kind: str
    receipt_protocol: str
    status: str
    manifest_schema: str
    batch_protocol: str
    payload_protocol: str
    selection: str
    project_count: int
    entity_count: int
    row_snapshot_bytes: int
    framed_payload_bytes: int
    manifest_bytes: bytes
    manifest_sha256: bytes
    payload_sha256: bytes
    batch_sha256: bytes
    projects: Tuple[ProjectPlan, ...]


def _local_probe(path: Path) -> kernel.FilesystemInfo:
    return kernel.FilesystemInfo("test-local", str(path), True)


def _database(tmp_path: Path) -> kernel.SqliteDatabase:
    return kernel.SqliteDatabase(
        tmp_path.resolve() / "private" / "repository.sqlite3",
        _test_filesystem_probe=_local_probe,
    )


def _plan(import_id: str, project_id: str, title: str) -> ImportPlan:
    document_id = "document-" + project_id
    document = Document(
        id=document_id,
        name="Document",
        schema_version="1",
        collaboration_mode="live_collab",
        save_policy="auto_commit",
        schema={
            "card": {
                "type": "object",
                "properties": {"title": {"type": "string"}},
            }
        },
        config={},
        acl={},
    )
    document.add(
        Entity(
            type="card",
            id="entity-1",
            name="Card",
            data={"title": title},
        )
    )
    snapshot = capture_business_snapshot(project_id, 1, document).snapshot
    project_row = ProjectRow(
        snapshot.project.project_id,
        snapshot.project.generation,
        snapshot.project.codec_id,
        snapshot.project.codec_version,
        snapshot.project.snapshot_format_version,
        snapshot.project.snapshot_bytes,
        snapshot.project.snapshot_sha256,
    )
    document_row = DocumentRow(
        snapshot.document.project_id,
        snapshot.document.document_id,
        snapshot.document.document_version,
        snapshot.document.schema_version,
        snapshot.document.collaboration_mode,
        snapshot.document.save_policy,
        snapshot.document.codec_id,
        snapshot.document.codec_version,
        snapshot.document.snapshot_format_version,
        snapshot.document.snapshot_bytes,
        snapshot.document.snapshot_sha256,
    )
    entity_rows = tuple(
        EntityRow(
            row.project_id,
            row.document_id,
            row.entity_type,
            row.entity_id,
            row.entity_version,
            row.deleted,
            row.codec_id,
            row.codec_version,
            row.snapshot_format_version,
            row.snapshot_bytes,
            row.snapshot_sha256,
        )
        for row in snapshot.entities
    )
    source = ("source:" + project_id + ":" + title).encode("utf-8")
    row_bytes = (
        len(project_row.snapshot_bytes)
        + len(document_row.snapshot_bytes)
        + sum(len(row.snapshot_bytes) for row in entity_rows)
    )
    project = ProjectPlan(
        ordinal=0,
        project_id=project_id,
        generation=1,
        document_id=document_id,
        document_version=document_row.document_version,
        storage_version="v2",
        source_head_sha256=hashlib.sha256(source).digest(),
        source_head_bytes=len(source),
        business_root_sha256=project_row.snapshot_sha256,
        entity_count=len(entity_rows),
        row_snapshot_bytes=row_bytes,
        project_row=project_row,
        document_row=document_row,
        entity_rows=entity_rows,
    )
    integrity = build_legacy_import_integrity(
        import_id,
        (
            LegacyImportIntegrityProject(
                project_id=project.project_id,
                generation=project.generation,
                document_id=project.document_id,
                document_version=project.document_version,
                storage_version=project.storage_version,
                source_head_sha256=project.source_head_sha256,
                source_head_bytes=project.source_head_bytes,
                business_root_sha256=project.business_root_sha256,
                entity_count=project.entity_count,
                row_snapshot_bytes=project.row_snapshot_bytes,
                project_snapshot_bytes=project_row.snapshot_bytes,
                document_snapshot_bytes=document_row.snapshot_bytes,
                entity_snapshot_bytes=tuple(row.snapshot_bytes for row in entity_rows),
            ),
        ),
    )
    return ImportPlan(
        plan_protocol=LEGACY_IMPORT_PLAN_PROTOCOL,
        import_id=import_id,
        transaction_id=SQLITE_IMPORT_TRANSACTION_PREFIX + import_id,
        receipt_kind=SQLITE_IMPORT_TRANSACTION_KIND,
        receipt_protocol=SQLITE_IMPORT_RECEIPT_PROTOCOL,
        status=SQLITE_IMPORT_TERMINAL_STATUS,
        manifest_schema=LEGACY_MANIFEST_SCHEMA,
        batch_protocol=LEGACY_BATCH_PROTOCOL,
        payload_protocol=LEGACY_PAYLOAD_PROTOCOL,
        selection="explicit_projects",
        project_count=1,
        entity_count=integrity.entity_count,
        row_snapshot_bytes=integrity.row_snapshot_bytes,
        framed_payload_bytes=integrity.framed_payload_bytes,
        manifest_bytes=integrity.manifest_bytes,
        manifest_sha256=integrity.manifest_sha256,
        payload_sha256=integrity.payload_sha256,
        batch_sha256=integrity.batch_sha256,
        projects=(project,),
    )


def _raw(database: kernel.SqliteDatabase) -> sqlite3.Connection:
    connection = sqlite3.connect(str(database.path), isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _seal_import(database: kernel.SqliteDatabase, import_id: Optional[str]) -> None:
    catalog_id = "c" * 64
    with _raw(database) as connection:
        if import_id is None:
            project_count = entity_count = row_snapshot_bytes = source_head_bytes = 0
        else:
            batch = connection.execute(
                "SELECT project_count, entity_count, row_snapshot_bytes "
                "FROM legacy_import_batches WHERE import_id=?",
                (import_id,),
            ).fetchone()
            descriptor = connection.execute(
                "SELECT source_head_bytes FROM legacy_import_projects "
                "WHERE import_id=?",
                (import_id,),
            ).fetchone()
            assert batch is not None and descriptor is not None
            project_count = batch["project_count"]
            entity_count = batch["entity_count"]
            row_snapshot_bytes = batch["row_snapshot_bytes"]
            source_head_bytes = descriptor["source_head_bytes"]
        connection.execute("BEGIN")
        connection.execute(
            "INSERT INTO transaction_receipts(transaction_id, kind, committed_at) "
            "VALUES (?, ?, '2026-07-11T00:00:00.000Z')",
            (
                catalog_contract.LEGACY_CATALOG_TRANSACTION_PREFIX + catalog_id,
                catalog_contract.LEGACY_CATALOG_TRANSACTION_KIND,
            ),
        )
        connection.execute(
            """
            INSERT INTO legacy_catalog_seals(
                repository_singleton, catalog_id, transaction_id, receipt_kind,
                receipt_protocol, import_id, seal_protocol, manifest_schema,
                status, selection, project_count, entity_count,
                source_head_bytes, row_snapshot_bytes, manifest_bytes,
                manifest_sha256, source_catalog_sha256,
                target_coverage_sha256, seal_sha256, target_schema_version,
                schema_fingerprint, cutover_ready
            ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 4, ?, 0)
            """,
            (
                catalog_id,
                catalog_contract.LEGACY_CATALOG_TRANSACTION_PREFIX + catalog_id,
                catalog_contract.LEGACY_CATALOG_TRANSACTION_KIND,
                catalog_contract.LEGACY_CATALOG_RECEIPT_PROTOCOL,
                import_id,
                catalog_contract.LEGACY_CATALOG_SEAL_PROTOCOL,
                catalog_contract.LEGACY_CATALOG_MANIFEST_SCHEMA,
                catalog_contract.LEGACY_CATALOG_STATUS,
                catalog_contract.LEGACY_CATALOG_SELECTION,
                project_count,
                entity_count,
                source_head_bytes,
                row_snapshot_bytes,
                b"{}",
                b"m" * 32,
                b"s" * 32,
                b"t" * 32,
                b"z" * 32,
                _SCHEMA_FINGERPRINT_V4,
            ),
        )
        if import_id is not None:
            connection.execute(
                """
                INSERT INTO legacy_catalog_members(
                    catalog_id, ordinal, import_id, project_id, generation,
                    document_id, document_version, storage_version,
                    source_head_sha256, source_head_bytes, business_root_sha256,
                    entity_count, row_snapshot_bytes
                )
                SELECT ?, ordinal, import_id, project_id, generation,
                       document_id, document_version, storage_version,
                       source_head_sha256, source_head_bytes, business_root_sha256,
                       entity_count, row_snapshot_bytes
                FROM legacy_import_projects WHERE import_id=?
                """,
                (catalog_id, import_id),
            )
        connection.execute(
            "INSERT INTO kernel_metadata(key, value) VALUES (?, ?)",
            (
                catalog_contract.SQLITE_CATALOG_STATE_KEY,
                catalog_contract.SQLITE_CATALOG_STATE_HISTORICAL_FENCED,
            ),
        )
        connection.execute("COMMIT")


def test_empty_import_fresh_reconcile_and_exact_retry_keep_receipt(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    plan = _plan("1" * 64, "project-a", "alpha")
    importer = adapter.SqliteLegacyImporter(database)
    assert importer.reconcile(plan).status is adapter.LegacyImportReconcileStatus.NOT_IMPORTED

    committed = importer.import_plan(plan)
    assert not committed.already_imported
    fresh = adapter.SqliteLegacyImporter(_database(tmp_path)).reconcile(plan)
    assert fresh.status is adapter.LegacyImportReconcileStatus.IMPORTED
    assert fresh.receipt == committed.receipt

    retried = adapter.SqliteLegacyImporter(_database(tmp_path)).import_plan(plan)
    assert retried.already_imported
    assert retried.receipt.committed_at == committed.receipt.committed_at
    with _raw(database) as connection:
        assert connection.execute(
            "SELECT authority FROM kernel_repository WHERE singleton = 1"
        ).fetchone()[0] == "legacy"

    generic = database.reconcile(plan.transaction_id)
    assert generic.status is kernel.ReconcileStatus.INDETERMINATE
    assert generic.error_code == "sqlite_transaction_namespace_reserved"
    assert importer.reconcile(plan).status is adapter.LegacyImportReconcileStatus.IMPORTED


def test_historical_catalog_allows_exact_retry_but_rejects_disjoint_append(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    importer = adapter.SqliteLegacyImporter(database)
    imported = _plan("f" * 64, "project-a", "alpha")
    first = importer.import_plan(imported)
    catalog_adapter.SqliteLegacyCatalogSealer(
        database,
        _capability=catalog_adapter._LEGACY_CATALOG_SEAL_CAPABILITY,
    ).seal(imported)

    assert database.inspect_profile().user_version == kernel.MIGRATIONS[-1].version
    exact = importer.reconcile(imported)
    assert exact.status is adapter.LegacyImportReconcileStatus.IMPORTED
    retried = importer.import_plan(imported)
    assert retried.already_imported
    assert retried.receipt.committed_at == first.receipt.committed_at

    disjoint = _plan("e" * 64, "project-b", "beta")
    rejected = importer.reconcile(disjoint)
    assert rejected.status is adapter.LegacyImportReconcileStatus.CONFLICT
    assert rejected.error_code == "sqlite_legacy_import_conflict"
    with pytest.raises(adapter.SQLiteLegacyImportError) as caught:
        importer.import_plan(disjoint)
    assert caught.value.code == "sqlite_legacy_import_conflict"
    with _raw(database) as connection:
        assert connection.execute("SELECT count(*) FROM legacy_import_batches").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM projects").fetchone()[0] == 1


def test_historical_empty_catalog_rejects_first_import(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    catalog_adapter.SqliteLegacyCatalogSealer(
        database,
        _capability=catalog_adapter._LEGACY_CATALOG_SEAL_CAPABILITY,
    ).seal_empty()
    plan = _plan("d" * 64, "project-a", "alpha")
    importer = adapter.SqliteLegacyImporter(database)

    reconciled = importer.reconcile(plan)
    assert reconciled.status is adapter.LegacyImportReconcileStatus.CONFLICT
    assert reconciled.error_code == "sqlite_legacy_import_conflict"
    with pytest.raises(adapter.SQLiteLegacyImportError) as caught:
        importer.import_plan(plan)
    assert caught.value.code == "sqlite_legacy_import_conflict"
    with _raw(database) as connection:
        assert connection.execute("SELECT count(*) FROM legacy_import_batches").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM projects").fetchone()[0] == 0


def test_same_id_mismatch_and_cross_id_target_overlap_are_conflicts(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    importer = adapter.SqliteLegacyImporter(database)
    importer.import_plan(_plan("2" * 64, "project-a", "alpha"))

    same_id_changed = _plan("2" * 64, "project-a", "changed")
    assert importer.reconcile(same_id_changed).status is adapter.LegacyImportReconcileStatus.CONFLICT
    with pytest.raises(adapter.SQLiteLegacyImportError) as same_id:
        importer.import_plan(same_id_changed)
    assert same_id.value.code == "sqlite_legacy_import_conflict"

    overlapping = _plan("3" * 64, "project-a", "alpha")
    assert importer.reconcile(overlapping).status is adapter.LegacyImportReconcileStatus.CONFLICT


def test_disjoint_second_batch_is_imported_and_each_batch_reconciles(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    importer = adapter.SqliteLegacyImporter(database)
    first = _plan("4" * 64, "project-a", "alpha")
    second = _plan("5" * 64, "project-b", "beta")
    importer.import_plan(first)
    importer.import_plan(second)

    assert importer.reconcile(first).status is adapter.LegacyImportReconcileStatus.IMPORTED
    assert importer.reconcile(second).status is adapter.LegacyImportReconcileStatus.IMPORTED
    with _raw(database) as connection:
        assert connection.execute("SELECT count(*) FROM legacy_import_batches").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM projects").fetchone()[0] == 2


@pytest.mark.parametrize("private_table", tuple(sorted(kernel.PRIVATE_TABLES)))
def test_generic_uow_cannot_read_private_tables_on_empty_substrate(
    tmp_path: Path,
    private_table: str,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    transaction_id = "private-read-" + private_table.replace("_", "-")

    def catch_denial(uow) -> None:
        try:
            uow.execute("SELECT * FROM %s LIMIT 1" % private_table)
        except sqlite3.DatabaseError:
            pass

    with pytest.raises(kernel.SQLiteKernelError) as denied:
        database.commit(transaction_id, "private-read-test", catch_denial)
    assert denied.value.code == "sqlite_uow_statement_forbidden"
    assert database.reconcile(transaction_id).status is kernel.ReconcileStatus.NOT_COMMITTED


@pytest.mark.parametrize("attack", ("disable_authorizer", "private_update", "commit"))
def test_terminal_shadow_closes_generic_callback_before_reflection_attack(
    tmp_path: Path,
    attack: str,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    plan = _plan("f" * 64, "project-a", "alpha")
    importer = adapter.SqliteLegacyImporter(database)
    importer.import_plan(plan)
    transaction_id = "uow-reflection-" + attack.replace("_", "-")
    callback_calls = []

    def try_attack(uow) -> None:
        callback_calls.append(1)
        connection = object.__getattribute__(uow, "_UnitOfWork__connection")
        if attack == "disable_authorizer":
            connection.set_authorizer(lambda *_args: sqlite3.SQLITE_OK)
        elif attack == "private_update":
            connection.execute(
                "UPDATE entities SET snapshot_bytes = zeroblob(length(snapshot_bytes))"
            )
        else:
            connection.commit()

    with pytest.raises(kernel.SQLiteKernelError) as closed:
        database.commit(transaction_id, "reflection-test", try_attack)
    assert closed.value.code == "sqlite_terminal_shadow_read_only"
    assert callback_calls == []
    assert database.reconcile(transaction_id).status is kernel.ReconcileStatus.NOT_COMMITTED
    assert importer.reconcile(plan).status is adapter.LegacyImportReconcileStatus.IMPORTED


def test_exact_batch_row_tamper_is_corrupt_not_imported(tmp_path: Path) -> None:
    database = _database(tmp_path)
    database.initialize()
    plan = _plan("6" * 64, "project-a", "alpha")
    importer = adapter.SqliteLegacyImporter(database)
    importer.import_plan(plan)
    with _raw(database) as connection:
        payload = connection.execute(
            "SELECT snapshot_bytes FROM entities WHERE project_id = 'project-a'"
        ).fetchone()[0]
        changed = bytes((payload[0] ^ 1,)) + payload[1:]
        connection.execute(
            "UPDATE entities SET snapshot_bytes = ? WHERE project_id = 'project-a'",
            (changed,),
        )
    assert importer.reconcile(plan).status is adapter.LegacyImportReconcileStatus.CORRUPT


@pytest.mark.parametrize(
    "forbidden_sql",
    (
        "UPDATE projects SET generation = generation",
        "INSERT OR REPLACE INTO projects(project_id) VALUES ('forbidden')",
    ),
)
def test_fixed_statement_allowlist_and_authorizer_roll_back_to_not_imported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    forbidden_sql: str,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    plan = _plan("7" * 64, "project-a", "alpha")
    importer = adapter.SqliteLegacyImporter(database)
    monkeypatch.setattr(
        adapter,
        "_INSERT_PROJECT_SQL",
        forbidden_sql,
    )
    with pytest.raises(adapter.SQLiteLegacyImportError) as denied:
        importer.import_plan(plan)
    assert denied.value.code == "sqlite_legacy_import_statement_forbidden"
    assert importer.reconcile(plan).status is adapter.LegacyImportReconcileStatus.NOT_IMPORTED


def test_entity_count_mismatch_is_rejected_before_row_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    plan = _plan("7" * 64, "project-a", "alpha")
    project = plan.projects[0]._replace(entity_count=0)
    broken = plan._replace(projects=(project,), entity_count=0)
    monkeypatch.setattr(
        adapter,
        "_entity_row",
        lambda _row: pytest.fail("entity row projected before count preflight"),
    )
    with pytest.raises(adapter.SQLiteLegacyImportError) as rejected:
        adapter.SqliteLegacyImporter(database).reconcile(broken)
    assert rejected.value.code == "sqlite_legacy_import_plan_invalid"


def test_tuple_subclass_hooks_cannot_split_integrity_from_written_entity_rows(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    plan = _plan("d" * 64, "project-a", "alpha")
    original = plan.projects[0].entity_rows[0]
    hooked = HookedEntityRow(*tuple(original))
    project = plan.projects[0]._replace(
        entity_rows=HookedEntityRows((hooked,))
    )
    adversarial = plan._replace(projects=(project,))

    importer = adapter.SqliteLegacyImporter(database)
    importer.import_plan(adversarial)
    assert importer.reconcile(adversarial).status is (
        adapter.LegacyImportReconcileStatus.IMPORTED
    )
    with _raw(database) as connection:
        written = connection.execute(
            "SELECT snapshot_bytes, snapshot_sha256 FROM entities"
        ).fetchone()
    assert written[0] == tuple.__getitem__(hooked, 9)
    assert written[1] == tuple.__getitem__(hooked, 10)


def test_commit_ack_loss_is_recovered_only_by_fresh_exact_reconcile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    plan = _plan("8" * 64, "project-a", "alpha")
    original_commit = kernel._OwnedConnection.commit

    def commit_then_lose_ack(connection) -> None:
        original_commit(connection)
        raise RuntimeError("simulated acknowledgement loss")

    monkeypatch.setattr(kernel._OwnedConnection, "commit", commit_then_lose_ack)
    result = adapter.SqliteLegacyImporter(database).import_plan(plan)
    assert result.recovered_after_ack_loss
    assert adapter.SqliteLegacyImporter(_database(tmp_path)).reconcile(plan).status is (
        adapter.LegacyImportReconcileStatus.IMPORTED
    )


def test_commit_async_interrupt_is_propagated_after_durable_reconcile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    plan = _plan("8" * 64, "project-a", "alpha")
    original_commit = kernel._OwnedConnection.commit

    def commit_then_interrupt(connection) -> None:
        original_commit(connection)
        raise KeyboardInterrupt()

    monkeypatch.setattr(kernel._OwnedConnection, "commit", commit_then_interrupt)
    importer = adapter.SqliteLegacyImporter(database)
    with pytest.raises(KeyboardInterrupt):
        importer.import_plan(plan)
    assert importer.reconcile(plan).status is adapter.LegacyImportReconcileStatus.IMPORTED


def test_first_import_and_exact_retry_share_kernel_durability_barrier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    plan = _plan("b" * 64, "project-a", "alpha")
    original_barrier = kernel.SqliteDatabase._finalize_committed_storage
    barrier_calls = []

    def record_barrier(target) -> None:
        barrier_calls.append(1)
        original_barrier(target)

    monkeypatch.setattr(
        kernel.SqliteDatabase, "_finalize_committed_storage", record_barrier
    )
    importer = adapter.SqliteLegacyImporter(database)
    assert not importer.import_plan(plan).already_imported
    assert importer.import_plan(plan).already_imported
    assert barrier_calls == [1, 1]


def test_import_durability_barrier_failure_classifies_target_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    plan = _plan("c" * 64, "project-a", "alpha")

    def fail_barrier(_database) -> None:
        raise kernel.SQLiteKernelError(
            "sqlite_file_stat_failed", "simulated durability barrier failure"
        )

    monkeypatch.setattr(
        kernel.SqliteDatabase, "_finalize_committed_storage", fail_barrier
    )
    importer = adapter.SqliteLegacyImporter(database)
    with pytest.raises(adapter.SQLiteLegacyImportError) as caught:
        importer.import_plan(plan)
    assert caught.value.code == "sqlite_legacy_import_unknown"
    reconciled = importer.reconcile(plan)
    assert reconciled.status is adapter.LegacyImportReconcileStatus.UNKNOWN
    assert reconciled.error_code == "sqlite_file_stat_failed"


def test_kernel_validation_uses_one_wal_snapshot_during_disjoint_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    importer = adapter.SqliteLegacyImporter(database)
    importer.import_plan(_plan("9" * 64, "project-a", "alpha"))
    second = _plan("a" * 64, "project-b", "beta")
    paused = threading.Event()
    release = threading.Event()
    errors = []
    reader_ident = []
    original_execute = kernel._OwnedConnection.execute

    def pause_after_batch_select(connection, sql, parameters=()):
        cursor = original_execute(connection, sql, parameters)
        if (
            reader_ident
            and threading.get_ident() == reader_ident[0]
            and "FROM legacy_import_batches" in " ".join(sql.split())
            and not paused.is_set()
        ):
            paused.set()
            if not release.wait(10):
                raise RuntimeError("validation concurrency test timed out")
        return cursor

    monkeypatch.setattr(kernel._OwnedConnection, "execute", pause_after_batch_select)

    def inspect() -> None:
        reader_ident.append(threading.get_ident())
        try:
            database.inspect_profile()
        except BaseException as exc:
            errors.append(exc)

    reader = threading.Thread(target=inspect)
    reader.start()
    assert paused.wait(10)
    try:
        importer.import_plan(second)
    finally:
        release.set()
    reader.join(10)
    assert not reader.is_alive()
    assert errors == []
    assert importer.reconcile(second).status is adapter.LegacyImportReconcileStatus.IMPORTED


def test_adapter_reconcile_keeps_validation_and_exact_rows_in_one_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    importer = adapter.SqliteLegacyImporter(database)
    importer.import_plan(_plan("b" * 64, "project-a", "alpha"))
    second = _plan("c" * 64, "project-b", "beta")
    validated = threading.Event()
    release = threading.Event()
    result = []
    errors = []
    reader_ident = []
    original_validate = kernel.SqliteDatabase._validate_initialized

    def validate_then_pause(owner, connection) -> None:
        original_validate(owner, connection)
        if reader_ident and threading.get_ident() == reader_ident[0]:
            validated.set()
            if not release.wait(10):
                raise RuntimeError("reconcile concurrency test timed out")

    monkeypatch.setattr(
        kernel.SqliteDatabase, "_validate_initialized", validate_then_pause
    )

    def reconcile() -> None:
        reader_ident.append(threading.get_ident())
        try:
            result.append(importer.reconcile(second))
        except BaseException as exc:
            errors.append(exc)

    reader = threading.Thread(target=reconcile)
    reader.start()
    assert validated.wait(10)
    try:
        importer.import_plan(second)
    finally:
        release.set()
    reader.join(10)
    assert not reader.is_alive()
    assert errors == []
    assert result[0].status is adapter.LegacyImportReconcileStatus.NOT_IMPORTED
    assert importer.reconcile(second).status is adapter.LegacyImportReconcileStatus.IMPORTED


def test_async_base_exception_is_not_rewritten_when_target_is_not_imported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    plan = _plan("d" * 64, "project-a", "alpha")
    original_execute = kernel._OwnedConnection.execute

    def interrupt_before_writer(connection, sql, parameters=()):
        if " ".join(sql.split()).upper() == "BEGIN IMMEDIATE":
            raise KeyboardInterrupt()
        return original_execute(connection, sql, parameters)

    monkeypatch.setattr(kernel._OwnedConnection, "execute", interrupt_before_writer)
    importer = adapter.SqliteLegacyImporter(database)
    with pytest.raises(KeyboardInterrupt):
        importer.import_plan(plan)
    assert importer.reconcile(plan).status is adapter.LegacyImportReconcileStatus.NOT_IMPORTED
