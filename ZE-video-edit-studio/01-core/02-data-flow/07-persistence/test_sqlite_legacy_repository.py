"""Durable evidence for the private v5 legacy-repository proof sealer."""

from __future__ import annotations

import hashlib
import multiprocessing
import sqlite3
import sys
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

import legacy_repository_contract as contract  # noqa: E402
import sqlite_kernel as kernel  # noqa: E402
import sqlite_legacy_catalog as catalog  # noqa: E402
import sqlite_legacy_import as legacy_import  # noqa: E402
import sqlite_legacy_repository as repository  # noqa: E402
import sqlite_legacy_repository_exact as exact  # noqa: E402
import test_sqlite_legacy_catalog as catalog_support  # noqa: E402
from legacy_repository_integrity import (  # noqa: E402
    LegacyRepositoryIntegrityMember,
)


_EMPTY_DIGEST = hashlib.sha256(b"").digest()


def _database(tmp_path: Path) -> kernel.SqliteDatabase:
    return catalog_support._database(tmp_path)


def _raw(database: kernel.SqliteDatabase) -> sqlite3.Connection:
    return catalog_support._raw(database)


def _catalog_sealer(
    database: kernel.SqliteDatabase,
) -> catalog.SqliteLegacyCatalogSealer:
    return catalog_support._sealer(database)


def _repository_sealer(
    database: kernel.SqliteDatabase,
) -> repository.SqliteLegacyRepositorySealer:
    return repository.SqliteLegacyRepositorySealer(
        database,
        _capability=repository._LEGACY_REPOSITORY_SEAL_CAPABILITY,
    )


def _live_member(project: Any) -> LegacyRepositoryIntegrityMember:
    return LegacyRepositoryIntegrityMember(
        project_id=project.project_id,
        generation=project.generation,
        status=contract.LEGACY_REPOSITORY_MEMBER_LIVE,
        operation_id=contract.LEGACY_REPOSITORY_LEGACY_ADOPT_OPERATION_ID,
        source_state_sha256=project.source_head_sha256,
        source_state_bytes=project.source_head_bytes,
        catalog_ordinal=0,
        import_id="a" * 64,
        document_id=project.document_id,
        document_version=project.document_version,
        storage_version=project.storage_version,
        business_root_sha256=project.business_root_sha256,
        entity_count=project.entity_count,
        row_snapshot_bytes=project.row_snapshot_bytes,
    )


def _deleted_member(project_id: str = "project-z") -> LegacyRepositoryIntegrityMember:
    return LegacyRepositoryIntegrityMember(
        project_id=project_id,
        generation=4,
        status=contract.LEGACY_REPOSITORY_MEMBER_DELETED,
        operation_id="1" * 32,
        source_state_sha256=_EMPTY_DIGEST,
        source_state_bytes=0,
        catalog_ordinal=None,
        import_id=None,
        document_id=None,
        document_version=None,
        storage_version=None,
        business_root_sha256=None,
        entity_count=None,
        row_snapshot_bytes=None,
    )


def _projection(catalog_projection, members):
    return exact._issue_repository_projection(
        catalog_projection,
        tuple(members),
        repository_epoch="e" * 64,
        repository_revision=7,
        catalog_revision=7,
        runtime_generation="core-3.11.0+test",
        code_digest=b"d" * 32,
    )


def _prepare_empty(tmp_path: Path):
    database = _database(tmp_path)
    database.initialize()
    _catalog_sealer(database).seal_empty()
    return database, catalog._project_empty()


def _prepare_live(tmp_path: Path):
    database = _database(tmp_path)
    database.initialize()
    plan = catalog_support._plan()
    legacy_import.SqliteLegacyImporter(database).import_plan(plan)
    _catalog_sealer(database).seal(plan)
    return database, catalog._project_nonempty(plan)


def _tamper_append_only(
    database: kernel.SqliteDatabase,
    trigger_prefix: str,
    sql: str,
    values=(),
) -> None:
    """Test-only mutation that restores the immutable trigger definitions."""

    connection = _raw(database)
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        triggers = connection.execute(
            "SELECT name, sql FROM sqlite_schema "
            "WHERE type='trigger' AND name GLOB ? ORDER BY name",
            (trigger_prefix + "*",),
        ).fetchall()
        assert triggers
        for trigger in triggers:
            connection.execute("DROP TRIGGER " + trigger["name"])
        connection.execute(sql, values)
        for trigger in triggers:
            assert type(trigger["sql"]) is str
            connection.execute(trigger["sql"])
    finally:
        connection.close()


def _fork_seal_worker(database, projection, start, queue) -> None:
    try:
        start.wait()
        result = _repository_sealer(database).seal(projection)
        queue.put(("ok", result.already_sealed, result.transaction_id))
    except BaseException as exc:  # pragma: no cover - asserted in parent
        queue.put(("error", type(exc).__name__, str(exc)))


@pytest.mark.parametrize("shape", ("empty", "deleted_only", "live", "mixed"))
def test_repository_shapes_seal_byte_exact_and_never_claim_cutover(
    tmp_path: Path,
    shape: str,
) -> None:
    if shape in ("empty", "deleted_only"):
        database, catalog_projection = _prepare_empty(tmp_path)
        members = () if shape == "empty" else (_deleted_member(),)
    else:
        database, catalog_projection = _prepare_live(tmp_path)
        live = _live_member(catalog_projection.projects[0])
        members = (live,) if shape == "live" else (live, _deleted_member())
    projection = _projection(catalog_projection, members)
    sealer = _repository_sealer(database)

    assert sealer.reconcile(projection).status is (
        repository.LegacyRepositoryReconcileStatus.NOT_SEALED
    )
    first = sealer.seal(projection)
    retry = sealer.seal(projection)

    assert first.authority == "legacy"
    assert first.cutover_ready is False
    assert not first.already_sealed
    assert retry.already_sealed
    assert retry.receipt == first.receipt
    assert first.receipt.kind == contract.LEGACY_REPOSITORY_TRANSACTION_KIND
    with _raw(database) as connection:
        assert connection.execute(
            "SELECT authority FROM kernel_repository"
        ).fetchone()[0] == "legacy"
        assert connection.execute(
            "SELECT cutover_ready FROM legacy_repository_proof_control"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT count(*) FROM legacy_repository_members"
        ).fetchone()[0] == len(members)


def test_projection_rejects_raw_or_catalog_mismatched_members(tmp_path: Path) -> None:
    _database_value, catalog_projection = _prepare_live(tmp_path)
    live = _live_member(catalog_projection.projects[0])
    with pytest.raises(exact.RepositoryExactProjectionError) as raw:
        exact._issue_repository_projection(  # type: ignore[arg-type]
            catalog_projection,
            [live],
            repository_epoch="e" * 64,
            repository_revision=7,
            catalog_revision=7,
            runtime_generation="test",
            code_digest=b"d" * 32,
        )
    assert raw.value.code == "repository_members_invalid"

    wrong = live._replace(source_state_sha256=b"x" * 32)
    with pytest.raises(exact.RepositoryExactProjectionError) as mismatch:
        _projection(catalog_projection, (wrong,))
    assert mismatch.value.code == "catalog_member_mismatch"


def test_projection_maps_canonical_rebuild_failures_to_stable_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _database_value, catalog_projection = _prepare_empty(tmp_path)

    def fail_catalog_rebuild():
        raise RuntimeError("untrusted rebuild detail")

    monkeypatch.setattr(exact, "project_empty_catalog", fail_catalog_rebuild)
    with pytest.raises(exact.RepositoryExactProjectionError) as catalog_error:
        _projection(catalog_projection, ())
    assert catalog_error.value.code == "catalog_projection_invalid"
    assert isinstance(catalog_error.value.__cause__, RuntimeError)


def test_every_control_seal_and_member_column_matches_projection(
    tmp_path: Path,
) -> None:
    database, catalog_projection = _prepare_live(tmp_path)
    projection = _projection(
        catalog_projection,
        (_live_member(catalog_projection.projects[0]), _deleted_member()),
    )
    committed = _repository_sealer(database).seal(projection)
    with _raw(database) as connection:
        control = connection.execute(exact.SELECT_CONTROL_SQL).fetchone()
        seal = connection.execute(exact.SELECT_SEAL_SQL).fetchone()
        members = connection.execute(exact.SELECT_MEMBERS_SQL).fetchall()
    assert tuple(control) == exact.control_values(projection)
    assert tuple(seal) == exact.seal_values(projection)
    assert [tuple(row) for row in members] == [
        exact.member_values(projection, ordinal, member)
        for ordinal, member in enumerate(projection.members)
    ]
    assert committed.cutover_id == projection.integrity.cutover_id


def test_different_valid_projection_is_conflict_and_same_id_tamper_is_corrupt(
    tmp_path: Path,
) -> None:
    database, catalog_projection = _prepare_empty(tmp_path)
    projection = _projection(catalog_projection, ())
    _repository_sealer(database).seal(projection)

    different = exact._issue_repository_projection(
        catalog_projection,
        (),
        repository_epoch="e" * 64,
        repository_revision=7,
        catalog_revision=7,
        runtime_generation="core-3.11.0+test",
        code_digest=b"x" * 32,
    )
    assert _repository_sealer(database).reconcile(different).status is (
        repository.LegacyRepositoryReconcileStatus.CONFLICT
    )

    original = projection.integrity.manifest_sha256
    replacement = bytes((original[0] ^ 1,)) + original[1:]
    _tamper_append_only(
        database,
        "legacy_repository_*",
        "UPDATE legacy_repository_seals SET manifest_sha256 = ?",
        (replacement,),
    )
    database.inspect_profile()
    assert _repository_sealer(database).reconcile(projection).status is (
        repository.LegacyRepositoryReconcileStatus.CORRUPT
    )


def test_partial_namespace_and_reserved_receipt_fail_closed_as_corrupt(
    tmp_path: Path,
) -> None:
    database, catalog_projection = _prepare_empty(tmp_path)
    projection = _projection(catalog_projection, ())
    with _raw(database) as connection:
        connection.execute(repository._INSERT_CONTROL_SQL, exact.control_values(projection))
    assert _repository_sealer(database).reconcile(projection).status is (
        repository.LegacyRepositoryReconcileStatus.CORRUPT
    )

    other_path = tmp_path / "receipt-only"
    other_path.mkdir()
    other, other_catalog = _prepare_empty(other_path)
    other_projection = _projection(other_catalog, ())
    with _raw(other) as connection:
        connection.execute(
            "INSERT INTO transaction_receipts(transaction_id, kind, committed_at) "
            "VALUES (?, ?, '2026-07-12T00:00:00.000Z')",
            (
                other_projection.integrity.transaction_id,
                contract.LEGACY_REPOSITORY_TRANSACTION_KIND,
            ),
        )
    assert _repository_sealer(other).reconcile(other_projection).status is (
        repository.LegacyRepositoryReconcileStatus.CORRUPT
    )


def test_namespace_probe_routes_only_absent_present_corrupt_or_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    sealer = _repository_sealer(database)
    assert sealer.probe_namespace().status is (
        repository.LegacyRepositoryNamespaceStatus.ABSENT
    )

    # A fresh ABSENT probe cannot require the v4 catalog: it is the signal
    # that tells the SDK whether the old v4 writer still has to run.
    _catalog_sealer(database).seal_empty()
    projection = _projection(catalog._project_empty(), ())
    sealer.seal(projection)
    assert sealer.probe_namespace().status is (
        repository.LegacyRepositoryNamespaceStatus.PRESENT
    )

    corrupt_path = tmp_path / "probe-corrupt"
    corrupt_path.mkdir()
    corrupt, corrupt_catalog = _prepare_empty(corrupt_path)
    corrupt_projection = _projection(corrupt_catalog, ())
    with _raw(corrupt) as connection:
        connection.execute(
            repository._INSERT_CONTROL_SQL,
            exact.control_values(corrupt_projection),
        )
    assert _repository_sealer(corrupt).probe_namespace().status is (
        repository.LegacyRepositoryNamespaceStatus.CORRUPT
    )

    def fail_open(_database):
        raise kernel.SQLiteKernelError("sqlite_open_failed", "simulated")

    monkeypatch.setattr(kernel.SqliteDatabase, "_open_unvalidated", fail_open)
    unknown = sealer.probe_namespace()
    assert unknown.status is repository.LegacyRepositoryNamespaceStatus.UNKNOWN
    assert unknown.error_code == "sqlite_open_failed"


def test_v4_catalog_and_business_staleness_cannot_leave_repository_green(
    tmp_path: Path,
) -> None:
    database, catalog_projection = _prepare_live(tmp_path)
    projection = _projection(
        catalog_projection,
        (_live_member(catalog_projection.projects[0]),),
    )
    _repository_sealer(database).seal(projection)
    original = catalog_projection.integrity.manifest_sha256
    replacement = bytes((original[0] ^ 1,)) + original[1:]
    catalog_support._tamper_append_only_catalog(
        database,
        "UPDATE legacy_catalog_seals SET manifest_sha256 = ?",
        (replacement,),
    )
    database.inspect_profile()
    assert _repository_sealer(database).reconcile(projection).status is (
        repository.LegacyRepositoryReconcileStatus.CORRUPT
    )

    business_path = tmp_path / "business-stale"
    business_path.mkdir()
    business, business_catalog = _prepare_live(business_path)
    business_projection = _projection(
        business_catalog,
        (_live_member(business_catalog.projects[0]),),
    )
    _repository_sealer(business).seal(business_projection)
    with _raw(business) as connection:
        snapshot = bytes(
            connection.execute("SELECT snapshot_bytes FROM entities").fetchone()[0]
        )
        replacement = bytes((snapshot[0] ^ 1,)) + snapshot[1:]
        connection.execute("UPDATE entities SET snapshot_bytes = ?", (replacement,))
    assert _repository_sealer(business).reconcile(business_projection).status is (
        repository.LegacyRepositoryReconcileStatus.CORRUPT
    )


def test_fixed_sql_gate_and_authorizer_reject_non_insert_mutations(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    connection, _filesystem = database._open_unvalidated()
    try:
        with pytest.raises(repository.SQLiteLegacyRepositoryError) as fixed:
            repository._execute_closed(
                connection,
                "UPDATE kernel_metadata SET value = value",
                (),
            )
        assert fixed.value.code == "sqlite_legacy_repository_statement_forbidden"

        connection.execute("BEGIN IMMEDIATE")
        authorizer = repository._RepositoryAuthorizer()
        connection.set_authorizer(authorizer)
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute("DELETE FROM kernel_metadata")
        assert authorizer.denied
    finally:
        connection.set_authorizer(repository._allow_authorizer)
        if connection.in_transaction:
            connection.rollback()
        connection.close()

    normalized = " ".join(" ".join(repository._FIXED_INSERT_SQL).lower().split())
    assert " update " not in " " + normalized + " "
    assert " delete " not in " " + normalized + " "
    assert " replace " not in " " + normalized + " "
    assert " upsert " not in " " + normalized + " "
    assert len(repository._FIXED_INSERT_SQL) == 4


def test_failure_after_first_insert_rolls_back_the_whole_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, catalog_projection = _prepare_empty(tmp_path)
    projection = _projection(catalog_projection, ())
    original_execute = repository._execute_closed

    def insert_then_crash(connection, sql, values) -> None:
        original_execute(connection, sql, values)
        if sql == repository._INSERT_CONTROL_SQL:
            raise RuntimeError("simulated writer failure")

    monkeypatch.setattr(repository, "_execute_closed", insert_then_crash)
    with pytest.raises(repository.SQLiteLegacyRepositoryError) as caught:
        _repository_sealer(database).seal(projection)
    assert caught.value.code == "sqlite_legacy_repository_not_committed"
    assert _repository_sealer(database).reconcile(projection).status is (
        repository.LegacyRepositoryReconcileStatus.NOT_SEALED
    )
    with _raw(database) as connection:
        assert connection.execute(
            "SELECT count(*) FROM legacy_repository_proof_control"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT count(*) FROM legacy_repository_seals"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT count(*) FROM transaction_receipts WHERE kind = ?",
            (contract.LEGACY_REPOSITORY_TRANSACTION_KIND,),
        ).fetchone()[0] == 0


def test_commit_ack_loss_recovers_only_after_fresh_exact_reconcile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, catalog_projection = _prepare_empty(tmp_path)
    projection = _projection(catalog_projection, ())
    original_commit = kernel._OwnedConnection.commit

    def commit_then_lose_ack(connection) -> None:
        original_commit(connection)
        raise RuntimeError("simulated acknowledgement loss")

    monkeypatch.setattr(kernel._OwnedConnection, "commit", commit_then_lose_ack)
    result = _repository_sealer(database).seal(projection)
    assert result.recovered_after_ack_loss
    assert _repository_sealer(database).reconcile(projection).status is (
        repository.LegacyRepositoryReconcileStatus.SEALED
    )


def test_async_before_and_after_commit_is_preserved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, catalog_projection = _prepare_empty(tmp_path)
    projection = _projection(catalog_projection, ())
    original_execute = repository._execute_closed

    def insert_then_interrupt(connection, sql, values) -> None:
        original_execute(connection, sql, values)
        if sql == repository._INSERT_CONTROL_SQL:
            raise KeyboardInterrupt()

    monkeypatch.setattr(repository, "_execute_closed", insert_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        _repository_sealer(database).seal(projection)
    assert _repository_sealer(database).reconcile(projection).status is (
        repository.LegacyRepositoryReconcileStatus.NOT_SEALED
    )

    monkeypatch.setattr(repository, "_execute_closed", original_execute)
    original_commit = kernel._OwnedConnection.commit

    def commit_then_interrupt(connection) -> None:
        original_commit(connection)
        raise KeyboardInterrupt()

    monkeypatch.setattr(kernel._OwnedConnection, "commit", commit_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        _repository_sealer(database).seal(projection)
    assert _repository_sealer(database).reconcile(projection).status is (
        repository.LegacyRepositoryReconcileStatus.SEALED
    )


def test_durability_barrier_failure_keeps_positive_outcome_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, catalog_projection = _prepare_empty(tmp_path)
    projection = _projection(catalog_projection, ())

    def fail_barrier(_database) -> None:
        raise kernel.SQLiteKernelError(
            "sqlite_file_stat_failed", "simulated durability failure"
        )

    monkeypatch.setattr(kernel.SqliteDatabase, "_finalize_committed_storage", fail_barrier)
    with pytest.raises(repository.SQLiteLegacyRepositoryError) as caught:
        _repository_sealer(database).seal(projection)
    assert caught.value.code == "sqlite_legacy_repository_unknown"
    reconciled = _repository_sealer(database).reconcile(projection)
    assert reconciled.status is repository.LegacyRepositoryReconcileStatus.UNKNOWN
    assert reconciled.error_code == "sqlite_file_stat_failed"


@pytest.mark.parametrize(
    "sqlite_errorcode",
    (
        getattr(sqlite3, "SQLITE_BUSY", 5),
        getattr(sqlite3, "SQLITE_IOERR", 10),
    ),
)
def test_busy_and_io_read_failures_are_unknown_not_corruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sqlite_errorcode: int,
) -> None:
    database, catalog_projection = _prepare_empty(tmp_path)
    projection = _projection(catalog_projection, ())

    def fail_reconcile(_connection, _projection):
        error = sqlite3.OperationalError("simulated indeterminate read")
        error.sqlite_errorcode = sqlite_errorcode
        raise error

    monkeypatch.setattr(repository, "_reconcile_connection", fail_reconcile)
    result = _repository_sealer(database).reconcile(projection)
    assert result.status is repository.LegacyRepositoryReconcileStatus.UNKNOWN
    assert result.error_code == "sqlite_legacy_repository_read_indeterminate"


def test_two_processes_converge_on_one_exact_proof(tmp_path: Path) -> None:
    try:
        context = multiprocessing.get_context("fork")
    except ValueError:  # pragma: no cover - platform evidence
        pytest.skip("fork start method unavailable")
    database, catalog_projection = _prepare_empty(tmp_path)
    projection = _projection(catalog_projection, ())
    start = context.Event()
    queue = context.Queue()
    processes = tuple(
        context.Process(
            target=_fork_seal_worker,
            args=(database, projection, start, queue),
        )
        for _index in range(2)
    )
    for process in processes:
        process.start()
    start.set()
    results = tuple(queue.get(timeout=15) for _process in processes)
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0
    assert all(result[0] == "ok" for result in results)
    assert {result[2] for result in results} == {projection.integrity.transaction_id}
    with _raw(database) as connection:
        assert connection.execute(
            "SELECT count(*) FROM legacy_repository_seals"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT count(*) FROM transaction_receipts WHERE kind = ?",
            (contract.LEGACY_REPOSITORY_TRANSACTION_KIND,),
        ).fetchone()[0] == 1
