"""Durable evidence for the private historical legacy-catalog sealer."""

from __future__ import annotations

import inspect
import sqlite3
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

import legacy_catalog_contract as contract  # noqa: E402
import sqlite_kernel as kernel  # noqa: E402
import sqlite_legacy_catalog as catalog  # noqa: E402
import sqlite_legacy_import as legacy_import  # noqa: E402
import test_sqlite_legacy_import as import_support  # noqa: E402


def _database(tmp_path: Path) -> kernel.SqliteDatabase:
    return import_support._database(tmp_path)


def _plan(
    import_id: str = "a" * 64,
    project_id: str = "project-a",
    title: str = "alpha",
):
    return import_support._plan(import_id, project_id, title)


def _raw(database: kernel.SqliteDatabase) -> sqlite3.Connection:
    return import_support._raw(database)


def _sealer(database: kernel.SqliteDatabase) -> catalog.SqliteLegacyCatalogSealer:
    return catalog.SqliteLegacyCatalogSealer(
        database,
        _capability=catalog._LEGACY_CATALOG_SEAL_CAPABILITY,
    )


def _import(database: kernel.SqliteDatabase, plan) -> None:
    legacy_import.SqliteLegacyImporter(database).import_plan(plan)


def _tamper_append_only_catalog(
    database: kernel.SqliteDatabase,
    sql: str,
    values=(),
) -> None:
    """Test-only raw tamper that restores the immutable trigger schema."""

    connection = _raw(database)
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        triggers = connection.execute(
            "SELECT name, sql FROM sqlite_schema "
            "WHERE type='trigger' AND name IN (?, ?, ?, ?) ORDER BY name",
            (
                "legacy_catalog_members_no_delete",
                "legacy_catalog_members_no_update",
                "legacy_catalog_seals_no_delete",
                "legacy_catalog_seals_no_update",
            ),
        ).fetchall()
        assert len(triggers) == 4
        for trigger in triggers:
            connection.execute("DROP TRIGGER " + trigger["name"])
        connection.execute(sql, values)
        for trigger in triggers:
            assert type(trigger["sql"]) is str
            connection.execute(trigger["sql"])
    finally:
        connection.close()


def test_empty_catalog_seals_exactly_and_never_claims_cutover(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    sealer = _sealer(database)
    assert sealer.reconcile_empty().status is (
        catalog.LegacyCatalogReconcileStatus.NOT_SEALED
    )

    result = sealer.seal_empty()
    assert result.import_id is None
    assert result.authority == "legacy"
    assert result.cutover_ready is False
    assert not result.already_sealed
    assert not result.recovered_after_ack_loss
    assert result.transaction_id == (
        contract.LEGACY_CATALOG_TRANSACTION_PREFIX + result.catalog_id
    )
    assert result.receipt.kind == contract.LEGACY_CATALOG_TRANSACTION_KIND

    with _raw(database) as connection:
        assert connection.execute("SELECT authority FROM kernel_repository").fetchone()[
            0
        ] == "legacy"
        assert connection.execute("SELECT count(*) FROM legacy_catalog_members").fetchone()[
            0
        ] == 0
        assert connection.execute("SELECT cutover_ready FROM legacy_catalog_seals").fetchone()[
            0
        ] == 0


def test_nonempty_seal_and_fresh_exact_retry_preserve_receipt(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    plan = _plan()
    _import(database, plan)
    sealer = _sealer(database)

    assert sealer.reconcile(plan).status is (
        catalog.LegacyCatalogReconcileStatus.NOT_SEALED
    )
    first = sealer.seal(plan)
    retried = sealer.seal(plan)
    fresh = _sealer(_database(tmp_path)).reconcile(plan)

    assert first.import_id == plan.import_id
    assert not first.already_sealed
    assert retried.already_sealed
    assert retried.receipt == first.receipt
    assert fresh.status is catalog.LegacyCatalogReconcileStatus.SEALED
    assert fresh.receipt == first.receipt

    import_retry = legacy_import.SqliteLegacyImporter(database).import_plan(plan)
    assert import_retry.already_imported
    assert import_retry.receipt.transaction_id == plan.transaction_id


def test_sealed_importer_rejects_a_different_plan(tmp_path: Path) -> None:
    database = _database(tmp_path)
    database.initialize()
    first = _plan("a" * 64, "project-a", "alpha")
    different = _plan("b" * 64, "project-b", "beta")
    _import(database, first)
    _sealer(database).seal(first)

    reconciled = legacy_import.SqliteLegacyImporter(database).reconcile(different)
    assert reconciled.status is legacy_import.LegacyImportReconcileStatus.CONFLICT
    with pytest.raises(legacy_import.SQLiteLegacyImportError) as caught:
        legacy_import.SqliteLegacyImporter(database).import_plan(different)
    assert caught.value.code == "sqlite_legacy_import_conflict"


@pytest.mark.parametrize(
    "column",
    (
        "manifest_bytes",
        "manifest_sha256",
        "source_catalog_sha256",
        "target_coverage_sha256",
        "seal_sha256",
    ),
)
def test_equal_length_seal_tamper_makes_catalog_and_importer_corrupt(
    tmp_path: Path,
    column: str,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    plan = _plan()
    _import(database, plan)
    _sealer(database).seal(plan)

    with _raw(database) as connection:
        row = connection.execute(
            "SELECT " + column + " FROM legacy_catalog_seals"
        ).fetchone()
    assert row is not None
    original = bytes(row[0])
    replacement = bytes((original[0] ^ 1,)) + original[1:]
    assert len(replacement) == len(original)
    _tamper_append_only_catalog(
        database,
        "UPDATE legacy_catalog_seals SET " + column + " = ?",
        (replacement,),
    )

    # The generic kernel intentionally checks only BLOB shape/relations.  The
    # shared catalog-exact layer must therefore be what closes this gap.
    database.inspect_profile()
    catalog_result = _sealer(database).reconcile(plan)
    import_result = legacy_import.SqliteLegacyImporter(database).reconcile(plan)
    assert catalog_result.status is catalog.LegacyCatalogReconcileStatus.CORRUPT
    assert import_result.status is legacy_import.LegacyImportReconcileStatus.CORRUPT
    with pytest.raises(legacy_import.SQLiteLegacyImportError) as caught:
        legacy_import.SqliteLegacyImporter(database).import_plan(plan)
    assert caught.value.code == "sqlite_legacy_import_corrupt"


def test_equal_length_member_tamper_makes_both_reconcilers_corrupt(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    plan = _plan()
    _import(database, plan)
    _sealer(database).seal(plan)
    original = plan.projects[0].source_head_sha256
    replacement = bytes((original[0] ^ 1,)) + original[1:]
    _tamper_append_only_catalog(
        database,
        "UPDATE legacy_import_projects SET source_head_sha256 = ?",
        (replacement,),
    )
    _tamper_append_only_catalog(
        database,
        "UPDATE legacy_catalog_members SET source_head_sha256 = ?",
        (replacement,),
    )

    database.inspect_profile()
    assert _sealer(database).reconcile(plan).status is (
        catalog.LegacyCatalogReconcileStatus.CORRUPT
    )
    assert legacy_import.SqliteLegacyImporter(database).reconcile(plan).status is (
        legacy_import.LegacyImportReconcileStatus.CORRUPT
    )


def test_every_manifest_and_member_column_matches_the_canonical_projection(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    plan = _plan()
    _import(database, plan)
    sealer = _sealer(database)
    committed = sealer.seal(plan)
    projection = catalog._project_nonempty(plan)

    with _raw(database) as connection:
        seal = connection.execute(catalog._SELECT_SEAL_SQL).fetchone()
        members = connection.execute(catalog._SELECT_MEMBERS_SQL).fetchall()
    assert seal is not None
    assert tuple(seal) == catalog._seal_values(projection)
    assert [tuple(row) for row in members] == [
        catalog._member_values(projection, ordinal, project)
        for ordinal, project in enumerate(projection.projects)
    ]
    assert seal[1] == committed.catalog_id
    assert seal[14] == projection.integrity.manifest_bytes
    assert seal[15] == projection.integrity.manifest_sha256
    assert seal[19] == contract.LEGACY_CATALOG_TARGET_SCHEMA_VERSION
    assert seal[20] == kernel._expected_schema_fingerprint(4)


def test_existing_different_catalog_is_a_conflict(tmp_path: Path) -> None:
    database = _database(tmp_path)
    database.initialize()
    plan = _plan()
    _import(database, plan)
    _sealer(database).seal(plan)

    empty = _sealer(database).reconcile_empty()
    assert empty.status is catalog.LegacyCatalogReconcileStatus.CONFLICT
    with pytest.raises(catalog.SQLiteLegacyCatalogError) as caught:
        _sealer(database).seal_empty()
    assert caught.value.code == "sqlite_legacy_catalog_conflict"


def test_complete_target_rejects_a_second_valid_import_batch(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    first = _plan("a" * 64, "project-a", "alpha")
    second = _plan("b" * 64, "project-b", "beta")
    _import(database, first)
    _import(database, second)

    reconciled = _sealer(database).reconcile(first)
    assert reconciled.status is catalog.LegacyCatalogReconcileStatus.CONFLICT
    with pytest.raises(catalog.SQLiteLegacyCatalogError) as caught:
        _sealer(database).seal(first)
    assert caught.value.code == "sqlite_legacy_catalog_conflict"


def test_empty_catalog_rejects_prior_generic_receipt(tmp_path: Path) -> None:
    database = _database(tmp_path)
    database.initialize()
    database.commit("prior-tx", "prior-kind", lambda _uow: None)

    reconciled = _sealer(database).reconcile_empty()
    assert reconciled.status is catalog.LegacyCatalogReconcileStatus.CONFLICT


@pytest.mark.parametrize("mutation", ["missing", "same_length_blob"])
def test_target_tamper_is_corrupt(
    tmp_path: Path,
    mutation: str,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    plan = _plan()
    _import(database, plan)
    with _raw(database) as connection:
        if mutation == "missing":
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("DELETE FROM entities")
        else:
            row = connection.execute("SELECT snapshot_bytes FROM entities").fetchone()
            assert row is not None
            original = bytes(row[0])
            replacement = bytes((original[0] ^ 1,)) + original[1:]
            assert len(replacement) == len(original)
            connection.execute(
                "UPDATE entities SET snapshot_bytes = ?", (replacement,)
            )

    reconciled = _sealer(database).reconcile(plan)
    assert reconciled.status is catalog.LegacyCatalogReconcileStatus.CORRUPT
    with pytest.raises(catalog.SQLiteLegacyCatalogError) as caught:
        _sealer(database).seal(plan)
    assert caught.value.code == "sqlite_legacy_catalog_corrupt"


def test_fixed_sql_gate_and_authorizer_reject_non_insert_mutations(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    connection, _filesystem = database._open_unvalidated()
    try:
        with pytest.raises(catalog.SQLiteLegacyCatalogError) as fixed:
            catalog._execute_closed(
                connection,
                "UPDATE kernel_metadata SET value = value",
                (),
            )
        assert fixed.value.code == "sqlite_legacy_catalog_statement_forbidden"

        connection.execute("BEGIN IMMEDIATE")
        authorizer = catalog._CatalogAuthorizer()
        connection.set_authorizer(authorizer)
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute("DELETE FROM kernel_metadata")
        assert authorizer.denied
    finally:
        connection.set_authorizer(catalog._allow_authorizer)
        if connection.in_transaction:
            connection.rollback()
        connection.close()

    normalized = " ".join(" ".join(catalog._FIXED_INSERT_SQL).lower().split())
    assert " update " not in " " + normalized + " "
    assert " delete " not in " " + normalized + " "
    assert " replace " not in " " + normalized + " "
    assert " upsert " not in " " + normalized + " "


def test_failure_after_first_insert_rolls_back_every_catalog_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    original_execute = catalog._execute_closed

    def insert_then_crash(connection, sql, values) -> None:
        original_execute(connection, sql, values)
        if sql == catalog._INSERT_MARKER_SQL:
            raise RuntimeError("simulated writer failure")

    monkeypatch.setattr(catalog, "_execute_closed", insert_then_crash)
    with pytest.raises(catalog.SQLiteLegacyCatalogError) as caught:
        _sealer(database).seal_empty()
    assert caught.value.code == "sqlite_legacy_catalog_not_committed"
    assert _sealer(database).reconcile_empty().status is (
        catalog.LegacyCatalogReconcileStatus.NOT_SEALED
    )
    with _raw(database) as connection:
        assert connection.execute(
            "SELECT count(*) FROM kernel_metadata WHERE key = ?",
            (contract.SQLITE_CATALOG_STATE_KEY,),
        ).fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM legacy_catalog_seals").fetchone()[
            0
        ] == 0
        assert connection.execute("SELECT count(*) FROM transaction_receipts").fetchone()[
            0
        ] == 0


def test_commit_ack_loss_recovers_only_after_fresh_exact_reconcile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    original_commit = kernel._OwnedConnection.commit

    def commit_then_lose_ack(connection) -> None:
        original_commit(connection)
        raise RuntimeError("simulated acknowledgement loss")

    monkeypatch.setattr(kernel._OwnedConnection, "commit", commit_then_lose_ack)
    result = _sealer(database).seal_empty()
    assert result.recovered_after_ack_loss
    assert _sealer(database).reconcile_empty().status is (
        catalog.LegacyCatalogReconcileStatus.SEALED
    )


def test_writer_close_ack_loss_recovers_after_fresh_exact_reconcile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    original_close = kernel._OwnedConnection.close
    calls = []

    def second_close_loses_ack(connection) -> None:
        calls.append(1)
        original_close(connection)
        if len(calls) == 2:
            raise RuntimeError("simulated close acknowledgement loss")

    monkeypatch.setattr(kernel._OwnedConnection, "close", second_close_loses_ack)
    result = _sealer(database).seal_empty()
    assert result.recovered_after_ack_loss
    assert len(calls) >= 3


def test_durability_barrier_failure_keeps_positive_outcome_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    database.initialize()

    def fail_barrier(_database) -> None:
        raise kernel.SQLiteKernelError(
            "sqlite_file_stat_failed", "simulated durability failure"
        )

    monkeypatch.setattr(
        kernel.SqliteDatabase, "_finalize_committed_storage", fail_barrier
    )
    with pytest.raises(catalog.SQLiteLegacyCatalogError) as caught:
        _sealer(database).seal_empty()
    assert caught.value.code == "sqlite_legacy_catalog_unknown"
    reconciled = _sealer(database).reconcile_empty()
    assert reconciled.status is catalog.LegacyCatalogReconcileStatus.UNKNOWN
    assert reconciled.error_code == "sqlite_file_stat_failed"


def test_async_before_write_is_preserved_and_target_remains_unsealed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    original_execute = kernel._OwnedConnection.execute

    def interrupt_before_writer(connection, sql, parameters=()):
        if " ".join(sql.split()).upper() == "BEGIN IMMEDIATE":
            raise KeyboardInterrupt()
        return original_execute(connection, sql, parameters)

    monkeypatch.setattr(kernel._OwnedConnection, "execute", interrupt_before_writer)
    with pytest.raises(KeyboardInterrupt):
        _sealer(database).seal_empty()
    assert _sealer(database).reconcile_empty().status is (
        catalog.LegacyCatalogReconcileStatus.NOT_SEALED
    )


def test_async_after_first_insert_rolls_back_and_is_preserved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    original_execute = catalog._execute_closed

    def insert_then_interrupt(connection, sql, values) -> None:
        original_execute(connection, sql, values)
        if sql == catalog._INSERT_MARKER_SQL:
            raise KeyboardInterrupt()

    monkeypatch.setattr(catalog, "_execute_closed", insert_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        _sealer(database).seal_empty()
    assert _sealer(database).reconcile_empty().status is (
        catalog.LegacyCatalogReconcileStatus.NOT_SEALED
    )


def test_async_after_commit_is_classified_then_preserved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    original_commit = kernel._OwnedConnection.commit

    def commit_then_interrupt(connection) -> None:
        original_commit(connection)
        raise KeyboardInterrupt()

    monkeypatch.setattr(kernel._OwnedConnection, "commit", commit_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        _sealer(database).seal_empty()
    assert _sealer(database).reconcile_empty().status is (
        catalog.LegacyCatalogReconcileStatus.SEALED
    )


def test_positive_first_seal_and_retry_share_the_kernel_barrier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    original = kernel.SqliteDatabase._finalize_committed_storage
    calls = []

    def record_barrier(target) -> None:
        calls.append(1)
        original(target)

    monkeypatch.setattr(
        kernel.SqliteDatabase, "_finalize_committed_storage", record_barrier
    )
    sealer = _sealer(database)
    assert not sealer.seal_empty().already_sealed
    assert sealer.seal_empty().already_sealed
    assert calls == [1, 1]


def test_capability_and_exact_database_type_are_required(tmp_path: Path) -> None:
    database = _database(tmp_path)
    database.initialize()
    with pytest.raises(catalog.SQLiteLegacyCatalogError) as missing:
        catalog.SqliteLegacyCatalogSealer(database)
    assert missing.value.code == "sqlite_legacy_catalog_forbidden"
    with pytest.raises(catalog.SQLiteLegacyCatalogError) as forged:
        catalog.SqliteLegacyCatalogSealer(database, _capability=object())
    assert forged.value.reason == "adapter_capability_required"

    class DatabaseSubclass(kernel.SqliteDatabase):
        pass

    with pytest.raises(TypeError):
        catalog.SqliteLegacyCatalogSealer(
            DatabaseSubclass(
                database.path,
                _test_filesystem_probe=import_support._local_probe,
            ),
            _capability=catalog._LEGACY_CATALOG_SEAL_CAPABILITY,
        )


def test_production_seal_api_has_no_callback_or_crash_hook(tmp_path: Path) -> None:
    assert tuple(inspect.signature(catalog.SqliteLegacyCatalogSealer.seal).parameters) == (
        "self",
        "plan",
    )
    assert tuple(
        inspect.signature(catalog.SqliteLegacyCatalogSealer.seal_empty).parameters
    ) == ("self",)
    assert not hasattr(catalog, "_CatalogWritePoint")

    database = _database(tmp_path)
    database.initialize()
    with pytest.raises(TypeError):
        _sealer(database).seal_empty(_test_crash_hook=lambda _point: None)
    assert _sealer(database).reconcile_empty().status is (
        catalog.LegacyCatalogReconcileStatus.NOT_SEALED
    )


def test_result_does_not_expose_projection_integrity_or_capability(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    result = _sealer(database).seal_empty()
    assert set(result.__dict__) == {
        "catalog_id",
        "import_id",
        "transaction_id",
        "receipt",
        "already_sealed",
        "recovered_after_ack_loss",
    }
    assert not hasattr(result, "plan")
    assert not hasattr(result, "integrity")
    assert not hasattr(result, "capability")
    with pytest.raises(Exception):
        result.authority = "sqlite"
