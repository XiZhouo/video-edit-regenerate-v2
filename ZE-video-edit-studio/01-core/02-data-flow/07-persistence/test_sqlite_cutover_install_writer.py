"""Real-SQL evidence for the private fixed V12 install writer."""

from __future__ import annotations

import hashlib
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

import sqlite_cutover_install_writer as writer_module
from business_cutover_baseline_projection import (
    ASSET_BLOBS_COLUMNS,
    BUSINESS_COMMITS_COLUMNS,
    DOCUMENT_ASSET_REFS_COLUMNS,
    DOCUMENT_COMMIT_HEADS_COLUMNS,
    DOCUMENTS_COLUMNS,
    ENTITIES_COLUMNS,
    PROJECTS_COLUMNS,
    REPOSITORY_PROJECTS_COLUMNS,
)
from business_cutover_install_closure import (
    ATOMIC_BUSINESS_WRITE_RECEIPT_COLUMNS,
    BASELINE_ATOMIC_RECEIPT_COLUMNS,
)
from sqlite_business_test_support import TEST_CUTOVER_COMMITTED_AT
from sqlite_cutover_install_test_support import (
    SQLiteCutoverInstallFixture,
    TEST_LIVE_ASSET_BYTES,
    _create_deleted_cutover_install_fixture,
    _create_empty_cutover_install_fixture,
    _create_live_cutover_install_fixture,
    _insert_coverage_receipt,
    _make_cutover_install_writer,
    _project_empty_cutover_install,
)
from sqlite_cutover_install_closure_reader import SQLiteCutoverInstallClosureError
from sqlite_cutover_install_writer import (
    SQLiteCutoverInstallError,
    SQLiteCutoverInstallStatus,
    SQLiteCutoverInstallTestPoint,
    SQLiteCutoverInstallWriter,
    _CUTOVER_INSTALL_WRITER_CAPABILITY,
    _CutoverInstallAuthorizer,
)
from sqlite_kernel import SQLiteKernelError, SQLiteSettings, SqliteDatabase
from sqlite_kernel import _OwnedConnection
from sqlite_production_test_support import (
    create_canonical_mounted_business_database,
)


@pytest.fixture
def prepared(tmp_path: Path) -> SQLiteCutoverInstallFixture:
    return _create_empty_cutover_install_fixture(tmp_path / "cutover.sqlite3")


def _snapshot(fixture: SQLiteCutoverInstallFixture) -> dict[str, Any]:
    connection, _filesystem = fixture.database._open_unvalidated()
    try:
        connection.execute("BEGIN")
        marker = connection.execute(
            "SELECT authority FROM kernel_repository WHERE singleton=1"
        ).fetchone()
        runtime = connection.execute(
            "SELECT authority_state,cutover_id FROM repository_runtime_control "
            "WHERE singleton=1"
        ).fetchone()
        counts = {
            table: connection.execute("SELECT count(*) FROM " + table).fetchone()[0]
            for table in (
                "legacy_repository_seals",
                "business_asset_coverage_receipts",
                "business_cutover_install_receipts",
                "worker_ledger_head",
                "outbox_allocation_head",
                "outbox_dispatch_head",
                "business_cutover_install_artifacts",
                "business_cutover_install_artifact_chunks",
                "business_cutover_install_closures",
                "business_cutover_head",
                "projects",
                "documents",
                "entities",
            )
        }
        foreign_key_failure = connection.execute("PRAGMA foreign_key_check").fetchone()
        return {
            "marker": None if marker is None else marker[0],
            "runtime": None if runtime is None else tuple(runtime),
            "counts": counts,
            "foreign_key_failure": foreign_key_failure,
            "foreign_keys": connection.execute("PRAGMA foreign_keys").fetchone()[0],
            "trusted_schema": connection.execute("PRAGMA trusted_schema").fetchone()[0],
        }
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()


def _commit_sql(fixture: SQLiteCutoverInstallFixture, sql: str, values=()) -> None:
    connection, _filesystem = fixture.database._open_unvalidated()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(sql, values)
        connection.commit()
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()


def _exact_rows(
    database: SqliteDatabase,
    table: str,
    columns: tuple[str, ...],
    order_by: str,
) -> tuple[tuple[Any, ...], ...]:
    connection, _filesystem = database._open_unvalidated()
    try:
        return tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT "
                + ",".join(columns)
                + " FROM "
                + table
                + " ORDER BY "
                + order_by
            ).fetchall()
        )
    finally:
        connection.close()


def test_fixture_is_real_guarded_empty_preinstall(
    prepared: SQLiteCutoverInstallFixture,
) -> None:
    state = _snapshot(prepared)
    assert state["marker"] == "legacy"
    assert state["runtime"] is None
    assert state["foreign_key_failure"] is None
    assert state["foreign_keys"] == 1
    assert state["trusted_schema"] == 0
    assert state["counts"]["legacy_repository_seals"] == 1
    assert state["counts"]["business_asset_coverage_receipts"] == 1
    assert all(
        state["counts"][table] == 0
        for table in (
            "business_cutover_install_receipts",
            "worker_ledger_head",
            "outbox_allocation_head",
            "outbox_dispatch_head",
            "business_cutover_install_artifacts",
            "business_cutover_install_artifact_chunks",
            "business_cutover_install_closures",
            "business_cutover_head",
            "projects",
            "documents",
            "entities",
        )
    )


def test_writer_requires_exact_database_and_module_capability(tmp_path: Path) -> None:
    database = SqliteDatabase((tmp_path / "not-opened.sqlite3").resolve())
    with pytest.raises(TypeError):
        SQLiteCutoverInstallWriter(database, _capability=object())
    with pytest.raises(TypeError):
        SQLiteCutoverInstallWriter(
            object(), _capability=_CUTOVER_INSTALL_WRITER_CAPABILITY
        )


def test_reconcile_classifies_exact_preinstall_as_absent(
    prepared: SQLiteCutoverInstallFixture,
) -> None:
    result = _make_cutover_install_writer(prepared).reconcile(prepared.projection)
    assert result.status is SQLiteCutoverInstallStatus.NOT_INSTALLED
    assert result.cutover_id == prepared.plan.cutover_id
    assert result.evidence is None
    assert result.recovered_after_ack_loss is False


def test_reconcile_open_failure_is_unknown(
    prepared: SQLiteCutoverInstallFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_open(self, *, allow_create=False):
        raise SQLiteKernelError("sqlite_open_failed", "injected-open-failure")

    monkeypatch.setattr(SqliteDatabase, "_open_unvalidated", fail_open)
    result = _make_cutover_install_writer(prepared).reconcile(prepared.projection)
    assert result.status is SQLiteCutoverInstallStatus.UNKNOWN
    assert result.code == "cutover_install_reconcile_unavailable"


def test_reconcile_missing_database_is_unknown(
    prepared: SQLiteCutoverInstallFixture,
) -> None:
    prepared.database.path.unlink()
    result = _make_cutover_install_writer(prepared).reconcile(prepared.projection)
    assert result.status is SQLiteCutoverInstallStatus.UNKNOWN
    assert result.code == "cutover_install_reconcile_unavailable"


def test_reconcile_non_sqlite_database_is_corrupt(
    prepared: SQLiteCutoverInstallFixture,
    tmp_path: Path,
) -> None:
    path = tmp_path / "wrong-database.sqlite3"
    path.write_bytes(b"this is not a sqlite database")
    path.chmod(0o600)
    writer = SQLiteCutoverInstallWriter(
        SqliteDatabase(path),
        _capability=_CUTOVER_INSTALL_WRITER_CAPABILITY,
    )
    result = writer.reconcile(prepared.projection)
    assert result.status is SQLiteCutoverInstallStatus.CORRUPT
    assert result.code == "cutover_install_schema_corrupt"


def test_reconcile_real_busy_wrapper_is_unknown(
    prepared: SQLiteCutoverInstallFixture,
) -> None:
    prepared.database.settings = SQLiteSettings(busy_timeout_ms=100)
    blocker = sqlite3.connect(
        str(prepared.database.path), isolation_level=None, timeout=0.1
    )
    try:
        blocker.execute("PRAGMA locking_mode=EXCLUSIVE")
        blocker.execute("BEGIN EXCLUSIVE")
        blocker.execute(
            "UPDATE kernel_repository SET created_at=created_at WHERE singleton=1"
        )
        result = _make_cutover_install_writer(prepared).reconcile(prepared.projection)
    finally:
        if blocker.in_transaction:
            blocker.rollback()
        blocker.close()
    assert result.status is SQLiteCutoverInstallStatus.UNKNOWN
    assert result.code == "cutover_install_reconcile_unavailable"


def test_reconcile_cleanup_failure_cannot_publish_definitive_state(
    prepared: SQLiteCutoverInstallFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = []
    original_open = SqliteDatabase._open_unvalidated
    original_cleanup = writer_module._rollback_close

    def capture_open(self, *, allow_create=False):
        opened = original_open(self, allow_create=allow_create)
        captured.append(opened[0])
        return opened

    monkeypatch.setattr(SqliteDatabase, "_open_unvalidated", capture_open)
    monkeypatch.setattr(writer_module, "_rollback_close", lambda connection: False)
    result = _make_cutover_install_writer(prepared).reconcile(prepared.projection)
    assert result.status is SQLiteCutoverInstallStatus.UNKNOWN
    assert result.code == "cutover_install_reconcile_cleanup_failed"
    assert len(captured) == 1
    monkeypatch.setattr(writer_module, "_rollback_close", original_cleanup)
    assert original_cleanup(captured[0]) is True


def test_inner_race_cleanup_failure_is_unknown(
    prepared: SQLiteCutoverInstallFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_snapshot = SQLiteCutoverInstallWriter._snapshot_result
    original_cleanup = writer_module._rollback_close
    snapshot_calls = 0
    cleanup_calls = 0

    def raced_snapshot(self, connection, projection):
        nonlocal snapshot_calls
        snapshot_calls += 1
        if snapshot_calls == 2:
            return self._result(
                projection,
                SQLiteCutoverInstallStatus.CONFLICT,
                "injected-inner-race",
            )
        return original_snapshot(self, connection, projection)

    def fail_second_cleanup(connection):
        nonlocal cleanup_calls
        cleanup_calls += 1
        complete = original_cleanup(connection)
        return complete if cleanup_calls == 1 else False

    monkeypatch.setattr(
        SQLiteCutoverInstallWriter,
        "_snapshot_result",
        raced_snapshot,
    )
    monkeypatch.setattr(writer_module, "_rollback_close", fail_second_cleanup)
    result = _make_cutover_install_writer(prepared).install(prepared.projection)
    assert result.status is SQLiteCutoverInstallStatus.UNKNOWN
    assert result.code == "cutover_install_race_cleanup_failed"
    assert snapshot_calls == cleanup_calls == 2


@pytest.mark.parametrize("failure_kind", ("conflict", "ordinary"))
def test_precommit_cleanup_failure_is_unknown(
    prepared: SQLiteCutoverInstallFixture,
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
) -> None:
    original_cleanup = writer_module._rollback_close
    cleanup_calls = 0

    def fail_second_cleanup(connection):
        nonlocal cleanup_calls
        cleanup_calls += 1
        complete = original_cleanup(connection)
        return False if cleanup_calls == 2 else complete

    monkeypatch.setattr(writer_module, "_rollback_close", fail_second_cleanup)
    projection = prepared.projection
    hook = None
    if failure_kind == "conflict":
        runtime = prepared.plan.install.runtime
        projection = _project_empty_cutover_install(
            repository_epoch=runtime.repository_epoch,
            source_admission_revision=runtime.source_admission_revision,
            runtime_generation=runtime.runtime_generation,
            code_digest=runtime.code_digest,
            install_committed_at="2999-01-01T00:00:00.000Z",
        )[-1]
    else:

        def hook(point: SQLiteCutoverInstallTestPoint) -> None:
            if point is SQLiteCutoverInstallTestPoint.BEFORE_COMMIT:
                raise RuntimeError("injected-precommit-failure")

    result = _make_cutover_install_writer(prepared).install(
        projection,
        _test_hook=hook,
    )
    assert result.status is SQLiteCutoverInstallStatus.UNKNOWN
    assert result.code == "cutover_install_cleanup_failed"
    assert cleanup_calls == 3


def test_commit_uncertainty_and_close_failure_is_unknown(
    prepared: SQLiteCutoverInstallFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_close = writer_module._close_quietly

    def fail_commit(self):
        raise sqlite3.OperationalError("injected-commit-uncertainty")

    def incomplete_close(connection):
        original_close(connection)
        return False

    monkeypatch.setattr(_OwnedConnection, "commit", fail_commit)
    monkeypatch.setattr(writer_module, "_close_quietly", incomplete_close)
    result = _make_cutover_install_writer(prepared).install(prepared.projection)
    assert result.status is SQLiteCutoverInstallStatus.UNKNOWN
    assert result.code == "cutover_install_cleanup_failed"


def test_reconcile_process_control_rolls_back_and_closes_then_propagates(
    prepared: SQLiteCutoverInstallFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = []
    original_open = SqliteDatabase._open_unvalidated

    def capture_open(self, *, allow_create=False):
        opened = original_open(self, allow_create=allow_create)
        captured.append(opened[0])
        return opened

    def interrupt(self, connection, projection):
        raise KeyboardInterrupt()

    monkeypatch.setattr(SqliteDatabase, "_open_unvalidated", capture_open)
    monkeypatch.setattr(SQLiteCutoverInstallWriter, "_snapshot_result", interrupt)
    with pytest.raises(KeyboardInterrupt):
        _make_cutover_install_writer(prepared).reconcile(prepared.projection)
    assert len(captured) == 1
    assert captured[0]._closed is True


def test_reconcile_cleanup_process_control_cannot_replace_original_signal(
    prepared: SQLiteCutoverInstallFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def interrupt(self, connection, projection):
        raise SystemExit(31)

    def cleanup_interrupt(connection):
        raise KeyboardInterrupt()

    monkeypatch.setattr(SQLiteCutoverInstallWriter, "_snapshot_result", interrupt)
    monkeypatch.setattr(writer_module, "_rollback_close", cleanup_interrupt)
    with pytest.raises(SystemExit, match="31"):
        _make_cutover_install_writer(prepared).reconcile(prepared.projection)


def test_future_install_time_is_rejected_before_dml(
    prepared: SQLiteCutoverInstallFixture,
) -> None:
    runtime = prepared.plan.install.runtime
    (
        _catalog,
        _repository,
        _plan,
        _coverage,
        _baseline,
        future,
    ) = _project_empty_cutover_install(
        repository_epoch=runtime.repository_epoch,
        source_admission_revision=runtime.source_admission_revision,
        runtime_generation=runtime.runtime_generation,
        code_digest=runtime.code_digest,
        install_committed_at="2999-01-01T00:00:00.000Z",
    )
    result = _make_cutover_install_writer(prepared).install(future)
    assert result.status is SQLiteCutoverInstallStatus.CONFLICT
    assert result.code == "cutover_install_time_future"
    assert _snapshot(prepared)["marker"] == "legacy"


def test_install_commits_closure_last_and_exact_retry_recovers(
    prepared: SQLiteCutoverInstallFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writes = []
    original_execute = writer_module._execute_fixed
    original_many = writer_module._executemany_fixed

    def recording_execute(connection, sql, values):
        writes.append(sql)
        return original_execute(connection, sql, values)

    def recording_many(connection, sql, rows):
        writes.append(sql)
        return original_many(connection, sql, rows)

    monkeypatch.setattr(writer_module, "_execute_fixed", recording_execute)
    monkeypatch.setattr(writer_module, "_executemany_fixed", recording_many)
    writer = _make_cutover_install_writer(prepared)
    result = writer.install(prepared.projection)
    assert result.status is SQLiteCutoverInstallStatus.INSTALLED_READ_ONLY
    assert result.evidence == prepared.plan.install
    assert result.recovered_after_ack_loss is False
    assert writes[-1] == writer_module._INSERT_INSTALL_CLOSURE_SQL
    assert writes.index(writer_module._CAS_REPOSITORY_MARKER_SQL) < writes.index(
        writer_module._INSERT_RUNTIME_SQL
    )
    assert not any(
        fragment in sql
        for sql in writer_module._FIXED_WRITE_SQL
        for fragment in (
            "INSERT INTO projects",
            "INSERT INTO documents",
            "INSERT INTO entities",
        )
    )

    state = _snapshot(prepared)
    assert state["marker"] == "sqlite"
    assert state["runtime"] == ("read_only", prepared.plan.cutover_id)
    assert state["counts"]["business_cutover_install_closures"] == 1
    assert state["counts"]["business_cutover_head"] == 0
    assert state["foreign_key_failure"] is None

    reconciled = writer.reconcile(prepared.projection)
    assert reconciled.status is SQLiteCutoverInstallStatus.INSTALLED_READ_ONLY
    retried = writer.install(prepared.projection)
    assert retried.status is SQLiteCutoverInstallStatus.INSTALLED_READ_ONLY
    assert retried.recovered_after_ack_loss is True


def test_real_v3_to_v8_live_install_preserves_shadow_and_writes_exact_rows(
    tmp_path: Path,
) -> None:
    fixture = _create_live_cutover_install_fixture(tmp_path / "live-cutover.sqlite3")
    baseline = fixture.baseline
    projection = fixture.projection
    shadow_specs = (
        ("projects", PROJECTS_COLUMNS, "project_id"),
        ("documents", DOCUMENTS_COLUMNS, "project_id,document_id"),
        (
            "entities",
            ENTITIES_COLUMNS,
            "project_id,document_id,entity_type,entity_id",
        ),
    )
    before_shadow = {
        table: _exact_rows(fixture.database, table, columns, order_by)
        for table, columns, order_by in shadow_specs
    }
    assert before_shadow == {
        "projects": tuple(row.project.sql_values() for row in baseline.live),
        "documents": tuple(row.document.sql_values() for row in baseline.live),
        "entities": tuple(
            entity.sql_values() for live in baseline.live for entity in live.entities
        ),
    }

    writer = _make_cutover_install_writer(fixture)
    assert (
        writer.reconcile(projection).status is SQLiteCutoverInstallStatus.NOT_INSTALLED
    )
    installed = writer.install(projection)
    assert installed.status is SQLiteCutoverInstallStatus.INSTALLED_READ_ONLY
    assert installed.evidence == fixture.plan.install
    assert {
        table: _exact_rows(fixture.database, table, columns, order_by)
        for table, columns, order_by in shadow_specs
    } == before_shadow

    assert _exact_rows(
        fixture.database,
        "business_commits",
        BUSINESS_COMMITS_COLUMNS,
        "repository_revision",
    ) == tuple(row.commit.sql_values() for row in baseline.live)
    assert _exact_rows(
        fixture.database,
        "repository_projects",
        REPOSITORY_PROJECTS_COLUMNS,
        "project_id",
    ) == tuple(row.repository_project.sql_values() for row in baseline.live)
    assert _exact_rows(
        fixture.database,
        "document_commit_heads",
        DOCUMENT_COMMIT_HEADS_COLUMNS,
        "project_id",
    ) == tuple(row.document_head.sql_values() for row in baseline.live)
    assert _exact_rows(
        fixture.database,
        "atomic_business_write_receipts",
        ATOMIC_BUSINESS_WRITE_RECEIPT_COLUMNS,
        "commit_id",
    ) == tuple(row.sql_values() for row in projection.atomic_receipts)
    assert _exact_rows(
        fixture.database,
        "business_cutover_baseline_atomic_receipts",
        BASELINE_ATOMIC_RECEIPT_COLUMNS,
        "repository_revision",
    ) == tuple(row.sql_values() for row in projection.baseline_receipts)

    connection, _filesystem = fixture.database._open_unvalidated()
    try:
        assert connection.execute("PRAGMA foreign_key_check").fetchone() is None
        assert (
            connection.execute("SELECT count(*) FROM business_cutover_head").fetchone()[
                0
            ]
            == 0
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM business_cutover_install_closures"
            ).fetchone()[0]
            == 1
        )
    finally:
        connection.close()
    assert (
        writer.reconcile(projection).status
        is SQLiteCutoverInstallStatus.INSTALLED_READ_ONLY
    )


def test_real_mixed_cutover_materializes_deleted_high_water_exactly(
    tmp_path: Path,
) -> None:
    fixture = _create_live_cutover_install_fixture(
        tmp_path / "mixed-cutover.sqlite3",
        include_deleted=True,
    )
    baseline = fixture.baseline
    assert len(baseline.live) == len(baseline.deleted) == 1
    result = _make_cutover_install_writer(fixture).install(fixture.projection)
    assert result.status is SQLiteCutoverInstallStatus.INSTALLED_READ_ONLY
    expected_projects = tuple(
        (
            member.live.repository_project.sql_values()
            if member.live is not None
            else member.deleted.repository_project.sql_values()
        )
        for member in baseline.members
    )
    assert (
        _exact_rows(
            fixture.database,
            "repository_projects",
            REPOSITORY_PROJECTS_COLUMNS,
            "project_id",
        )
        == expected_projects
    )
    assert _exact_rows(
        fixture.database,
        "business_commits",
        BUSINESS_COMMITS_COLUMNS,
        "repository_revision",
    ) == tuple(row.commit.sql_values() for row in baseline.live)
    assert fixture.projection.closure.project_count == 2
    assert fixture.projection.closure.live_project_count == 1
    assert fixture.projection.closure.deleted_project_count == 1
    connection, _filesystem = fixture.database._open_unvalidated()
    try:
        assert connection.execute("PRAGMA foreign_key_check").fetchone() is None
        assert connection.execute("SELECT count(*) FROM projects").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM documents").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM entities").fetchone()[0] == 1
    finally:
        connection.close()


def test_real_deleted_only_cutover_uses_empty_catalog_and_no_baseline_commit(
    tmp_path: Path,
) -> None:
    fixture = _create_deleted_cutover_install_fixture(
        tmp_path / "deleted-cutover.sqlite3"
    )
    baseline = fixture.baseline
    assert not baseline.live
    assert len(baseline.deleted) == 1
    result = _make_cutover_install_writer(fixture).install(fixture.projection)
    assert result.status is SQLiteCutoverInstallStatus.INSTALLED_READ_ONLY
    assert _exact_rows(
        fixture.database,
        "repository_projects",
        REPOSITORY_PROJECTS_COLUMNS,
        "project_id",
    ) == (baseline.deleted[0].repository_project.sql_values(),)
    connection, _filesystem = fixture.database._open_unvalidated()
    try:
        for table in (
            "projects",
            "documents",
            "entities",
            "business_commits",
            "document_commit_heads",
            "atomic_business_write_receipts",
            "business_cutover_baseline_atomic_receipts",
        ):
            assert (
                connection.execute("SELECT count(*) FROM " + table).fetchone()[0] == 0
            )
        runtime = connection.execute(
            "SELECT repository_revision,state_revision,authority_state "
            "FROM repository_runtime_control WHERE singleton=1"
        ).fetchone()
        assert tuple(runtime) == (1, 1, "read_only")
        assert connection.execute("PRAGMA foreign_key_check").fetchone() is None
    finally:
        connection.close()
    assert fixture.projection.closure.project_count == 1
    assert fixture.projection.closure.live_project_count == 0
    assert fixture.projection.closure.deleted_project_count == 1
    assert fixture.projection.closure.baseline_commit_count == 0


def test_before_commit_failure_rolls_back_every_install_owned_row(
    prepared: SQLiteCutoverInstallFixture,
) -> None:
    observed = []

    def fail(point: SQLiteCutoverInstallTestPoint) -> None:
        observed.append(point)
        if point is SQLiteCutoverInstallTestPoint.BEFORE_COMMIT:
            raise RuntimeError("injected-before-commit")

    result = _make_cutover_install_writer(prepared).install(
        prepared.projection,
        _test_hook=fail,
    )
    assert observed == [SQLiteCutoverInstallTestPoint.BEFORE_COMMIT]
    assert result.status is SQLiteCutoverInstallStatus.NOT_INSTALLED
    assert result.code == "cutover_install_rolled_back"
    state = _snapshot(prepared)
    assert state["marker"] == "legacy"
    assert state["counts"]["legacy_repository_seals"] == 1
    assert state["counts"]["business_asset_coverage_receipts"] == 1
    assert state["counts"]["business_cutover_install_receipts"] == 0
    assert state["counts"]["business_cutover_install_closures"] == 0
    assert state["foreign_key_failure"] is None


def test_after_commit_ack_loss_returns_fresh_exact_recovery(
    prepared: SQLiteCutoverInstallFixture,
) -> None:
    observed = []

    def lose_ack(point: SQLiteCutoverInstallTestPoint) -> None:
        observed.append(point)
        if point is SQLiteCutoverInstallTestPoint.AFTER_COMMIT_BEFORE_ACK:
            raise RuntimeError("injected-ack-loss")

    result = _make_cutover_install_writer(prepared).install(
        prepared.projection,
        _test_hook=lose_ack,
    )
    assert observed == [
        SQLiteCutoverInstallTestPoint.BEFORE_COMMIT,
        SQLiteCutoverInstallTestPoint.AFTER_COMMIT_BEFORE_ACK,
    ]
    assert result.status is SQLiteCutoverInstallStatus.INSTALLED_READ_ONLY
    assert result.recovered_after_ack_loss is True
    assert _snapshot(prepared)["counts"]["business_cutover_install_closures"] == 1


def test_precommit_closure_read_unavailable_is_unknown_and_rolls_back(
    prepared: SQLiteCutoverInstallFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = writer_module._read_business_cutover_install_closure

    def unavailable(*args, **kwargs):
        raise SQLiteCutoverInstallClosureError("cutover_install_closure_unavailable")

    monkeypatch.setattr(
        writer_module, "_read_business_cutover_install_closure", unavailable
    )
    writer = _make_cutover_install_writer(prepared)
    result = writer.install(prepared.projection)
    assert result.status is SQLiteCutoverInstallStatus.UNKNOWN
    assert result.code == "cutover_install_storage_unavailable"
    monkeypatch.setattr(
        writer_module, "_read_business_cutover_install_closure", original
    )
    assert writer.reconcile(prepared.projection).status is (
        SQLiteCutoverInstallStatus.NOT_INSTALLED
    )
    assert _snapshot(prepared)["marker"] == "legacy"


def test_installed_closure_read_unavailable_reconciles_as_unknown(
    prepared: SQLiteCutoverInstallFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer = _make_cutover_install_writer(prepared)
    assert writer.install(prepared.projection).status is (
        SQLiteCutoverInstallStatus.INSTALLED_READ_ONLY
    )

    def unavailable(*args, **kwargs):
        raise SQLiteCutoverInstallClosureError("cutover_install_closure_unavailable")

    monkeypatch.setattr(
        writer_module, "_read_business_cutover_install_closure", unavailable
    )
    result = writer.reconcile(prepared.projection)
    assert result.status is SQLiteCutoverInstallStatus.UNKNOWN
    assert result.code == "cutover_install_reconcile_unavailable"


def test_stable_runtime_validation_failure_is_corrupt_not_unknown(
    prepared: SQLiteCutoverInstallFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def structurally_invalid(database, connection, *, access):
        raise SQLiteKernelError(
            "sqlite_business_runtime_control_invalid",
            "injected-structural-runtime-failure",
        )

    monkeypatch.setattr(
        writer_module,
        "_validate_business_runtime_connection",
        structurally_invalid,
    )
    result = _make_cutover_install_writer(prepared).install(prepared.projection)
    assert result.status is SQLiteCutoverInstallStatus.CORRUPT
    assert result.code == "cutover_install_schema_corrupt"
    assert _snapshot(prepared)["marker"] == "legacy"


def test_process_control_before_commit_rolls_back_then_propagates(
    prepared: SQLiteCutoverInstallFixture,
) -> None:
    def interrupt(point: SQLiteCutoverInstallTestPoint) -> None:
        if point is SQLiteCutoverInstallTestPoint.BEFORE_COMMIT:
            raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        _make_cutover_install_writer(prepared).install(
            prepared.projection,
            _test_hook=interrupt,
        )
    assert _snapshot(prepared)["marker"] == "legacy"


def test_process_control_after_commit_reconciles_then_propagates(
    prepared: SQLiteCutoverInstallFixture,
) -> None:
    def interrupt(point: SQLiteCutoverInstallTestPoint) -> None:
        if point is SQLiteCutoverInstallTestPoint.AFTER_COMMIT_BEFORE_ACK:
            raise SystemExit(17)

    with pytest.raises(SystemExit, match="17"):
        _make_cutover_install_writer(prepared).install(
            prepared.projection,
            _test_hook=interrupt,
        )
    writer = _make_cutover_install_writer(prepared)
    assert (
        writer.reconcile(prepared.projection).status
        is SQLiteCutoverInstallStatus.INSTALLED_READ_ONLY
    )


def test_best_effort_reconcile_cannot_replace_original_process_control(
    prepared: SQLiteCutoverInstallFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer = _make_cutover_install_writer(prepared)
    original_reconcile = SQLiteCutoverInstallWriter.reconcile

    def second_process_control(self, projection):
        raise KeyboardInterrupt()

    def interrupt(point: SQLiteCutoverInstallTestPoint) -> None:
        if point is SQLiteCutoverInstallTestPoint.BEFORE_COMMIT:
            monkeypatch.setattr(
                SQLiteCutoverInstallWriter,
                "reconcile",
                second_process_control,
            )
            raise SystemExit(29)

    with pytest.raises(SystemExit, match="29"):
        writer.install(prepared.projection, _test_hook=interrupt)
    monkeypatch.setattr(
        SQLiteCutoverInstallWriter,
        "reconcile",
        original_reconcile,
    )
    assert _snapshot(prepared)["marker"] == "legacy"


def test_install_cleanup_process_control_cannot_replace_original_signal(
    prepared: SQLiteCutoverInstallFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def cleanup_interrupt(connection):
        raise KeyboardInterrupt()

    def interrupt(point: SQLiteCutoverInstallTestPoint) -> None:
        if point is SQLiteCutoverInstallTestPoint.BEFORE_COMMIT:
            monkeypatch.setattr(
                writer_module,
                "_rollback_close",
                cleanup_interrupt,
            )
            raise SystemExit(37)

    with pytest.raises(SystemExit, match="37"):
        _make_cutover_install_writer(prepared).install(
            prepared.projection,
            _test_hook=interrupt,
        )


def test_partial_reserved_install_namespace_is_corrupt(
    prepared: SQLiteCutoverInstallFixture,
) -> None:
    _commit_sql(
        prepared,
        "INSERT INTO transaction_receipts(transaction_id,kind,committed_at) "
        "VALUES (?,?,?)",
        (
            prepared.plan.install.transaction_id,
            prepared.plan.install.receipt_kind,
            prepared.install_committed_at,
        ),
    )
    result = _make_cutover_install_writer(prepared).reconcile(prepared.projection)
    assert result.status is SQLiteCutoverInstallStatus.CORRUPT
    assert result.code == "cutover_install_partial_namespace"


@pytest.mark.parametrize(
    ("transaction_id", "kind"),
    (
        ("_worker-execution:" + "a" * 64, "_worker_execution_v3"),
        (
            "_pipeline-business-control:" + "a" * 64,
            "_pipeline_business_control_v2",
        ),
        ("_repository-audit:" + "a" * 64, "_repository_audit_certificate_v1"),
        (
            "_business-cutover-activation:" + "a" * 64,
            "_business_cutover_activation_v2",
        ),
        (
            "_business-cutover-mount:" + "a" * 64,
            "_business_cutover_mount_v2",
        ),
        ("ze-business:" + "a" * 64, "ze_business_commit_v1"),
        (
            "_business-cutover-join:" + "a" * 64,
            "_business_cutover_join_v1",
        ),
        ("_business-cutover-join:" + "b" * 64, "unrelated-kind"),
        ("unrelated-join-kind", "_business_cutover_join_v1"),
        (
            "_business-cutover-asset-stage:" + "a" * 64,
            "unrelated-kind",
        ),
        ("unrelated-reserved-kind", "_business_cutover_mount_v2"),
    ),
)
def test_foreign_production_receipt_blocks_install_before_dml(
    prepared: SQLiteCutoverInstallFixture,
    transaction_id: str,
    kind: str,
) -> None:
    _commit_sql(
        prepared,
        "INSERT INTO transaction_receipts(transaction_id,kind,committed_at) "
        "VALUES (?,?,?)",
        (transaction_id, kind, prepared.install_committed_at),
    )
    writer = _make_cutover_install_writer(prepared)
    reconciled = writer.reconcile(prepared.projection)
    assert reconciled.status is SQLiteCutoverInstallStatus.CORRUPT
    assert reconciled.code == "cutover_install_reserved_receipt_namespace_corrupt"
    installed = writer.install(prepared.projection)
    assert installed.status is SQLiteCutoverInstallStatus.CORRUPT
    assert _snapshot(prepared)["marker"] == "legacy"


@pytest.mark.parametrize(
    ("transaction_id", "kind"),
    (
        (
            b"_worker-execution:" + b"a" * 64,
            b"_worker_execution_v3",
        ),
        (b"_worker-execution:" + b"b" * 64, b"unrelated-kind"),
        (b"unrelated-blob-identity", b"_worker_execution_v3"),
    ),
)
def test_blob_affinity_cannot_hide_reserved_receipt(
    prepared: SQLiteCutoverInstallFixture,
    transaction_id: bytes,
    kind: bytes,
) -> None:
    _commit_sql(
        prepared,
        "INSERT INTO transaction_receipts(transaction_id,kind,committed_at) "
        "VALUES (?,?,?)",
        (transaction_id, kind, prepared.install_committed_at),
    )
    result = _make_cutover_install_writer(prepared).reconcile(prepared.projection)
    assert result.status is SQLiteCutoverInstallStatus.CORRUPT
    assert result.code == "cutover_install_reserved_receipt_namespace_corrupt"


def test_blob_affinity_duplicate_of_expected_coverage_is_corrupt(
    prepared: SQLiteCutoverInstallFixture,
) -> None:
    evidence = prepared.coverage_plan.expected_evidence
    _commit_sql(
        prepared,
        "INSERT INTO transaction_receipts(transaction_id,kind,committed_at) "
        "VALUES (?,?,?)",
        (
            evidence.transaction_id.encode("utf-8"),
            evidence.receipt_kind.encode("utf-8"),
            prepared.install_committed_at,
        ),
    )
    result = _make_cutover_install_writer(prepared).reconcile(prepared.projection)
    assert result.status is SQLiteCutoverInstallStatus.CORRUPT
    assert result.code == "cutover_install_reserved_receipt_namespace_corrupt"


@pytest.mark.parametrize("mutation", ("duplicate", "prefix", "kind"))
def test_blob_affinity_cannot_hide_legacy_source_receipt(
    prepared: SQLiteCutoverInstallFixture,
    mutation: str,
) -> None:
    proof = prepared.plan.legacy_proof
    values = {
        "duplicate": (
            proof.transaction_id.encode("utf-8"),
            b"_legacy_repository_seal_v1",
        ),
        "prefix": (
            b"_legacy-repository-seal:" + b"a" * 64,
            b"unrelated-kind",
        ),
        "kind": (
            b"unrelated-source-receipt",
            b"_legacy_repository_seal_v1",
        ),
    }[mutation]
    _commit_sql(
        prepared,
        "INSERT INTO transaction_receipts(transaction_id,kind,committed_at) "
        "VALUES (?,?,?)",
        values + (prepared.source_committed_at,),
    )
    writer = _make_cutover_install_writer(prepared)
    result = writer.reconcile(prepared.projection)
    assert result.status is SQLiteCutoverInstallStatus.CORRUPT
    assert result.code == "cutover_install_legacy_receipt_namespace_corrupt"
    assert writer.install(prepared.projection).status is (
        SQLiteCutoverInstallStatus.CORRUPT
    )
    assert _snapshot(prepared)["marker"] == "legacy"


def test_blob_affinity_cannot_duplicate_legacy_catalog_receipt(
    prepared: SQLiteCutoverInstallFixture,
) -> None:
    transaction_id = "_legacy-catalog-seal:" + prepared.plan.legacy_proof.catalog_id
    _commit_sql(
        prepared,
        "INSERT INTO transaction_receipts(transaction_id,kind,committed_at) "
        "VALUES (?,?,?)",
        (
            transaction_id.encode("utf-8"),
            b"_legacy_catalog_seal_v1",
            prepared.source_committed_at,
        ),
    )
    result = _make_cutover_install_writer(prepared).reconcile(prepared.projection)
    assert result.status is SQLiteCutoverInstallStatus.CORRUPT
    assert result.code == "cutover_install_legacy_receipt_namespace_corrupt"


def test_blob_affinity_cannot_duplicate_legacy_import_receipt(
    tmp_path: Path,
) -> None:
    fixture = _create_live_cutover_install_fixture(
        tmp_path / "legacy-import-blob.sqlite3"
    )
    connection, _filesystem = fixture.database._open_unvalidated()
    try:
        row = connection.execute(
            "SELECT transaction_id,kind,committed_at FROM transaction_receipts "
            "WHERE substr(CAST(transaction_id AS TEXT),1,14)='legacy-import:'"
        ).fetchone()
        assert row is not None
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO transaction_receipts(transaction_id,kind,committed_at) "
            "VALUES (?,?,?)",
            (row[0].encode("utf-8"), row[1].encode("utf-8"), row[2]),
        )
        connection.commit()
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()
    result = _make_cutover_install_writer(fixture).reconcile(fixture.projection)
    assert result.status is SQLiteCutoverInstallStatus.CORRUPT
    assert result.code == "cutover_install_legacy_receipt_namespace_corrupt"


def test_unrelated_nonproduction_kernel_receipt_is_preserved(
    prepared: SQLiteCutoverInstallFixture,
) -> None:
    _commit_sql(
        prepared,
        "INSERT INTO transaction_receipts(transaction_id,kind,committed_at) "
        "VALUES (?,?,?)",
        ("user-kernel-receipt", "user-kernel-kind", prepared.install_committed_at),
    )
    result = _make_cutover_install_writer(prepared).install(prepared.projection)
    assert result.status is SQLiteCutoverInstallStatus.INSTALLED_READ_ONLY
    connection, _filesystem = prepared.database._open_unvalidated()
    try:
        row = connection.execute(
            "SELECT kind,committed_at FROM transaction_receipts "
            "WHERE transaction_id='user-kernel-receipt'"
        ).fetchone()
        assert tuple(row) == ("user-kernel-kind", prepared.install_committed_at)
    finally:
        connection.close()


def test_complete_foreign_preinstall_predecessor_is_conflict_not_corrupt(
    prepared: SQLiteCutoverInstallFixture,
) -> None:
    runtime = prepared.plan.install.runtime
    (
        _catalog,
        _repository,
        _plan,
        _coverage,
        _baseline,
        foreign,
    ) = _project_empty_cutover_install(
        repository_epoch="f" * 64,
        source_admission_revision=runtime.source_admission_revision,
        runtime_generation=runtime.runtime_generation,
        code_digest=runtime.code_digest,
        install_committed_at=prepared.install_committed_at,
    )
    result = _make_cutover_install_writer(prepared).reconcile(foreign)
    assert result.status is SQLiteCutoverInstallStatus.CONFLICT
    assert result.code == "cutover_install_reserved_receipt_conflict"


def test_expected_predecessor_plus_foreign_coverage_is_corrupt(
    prepared: SQLiteCutoverInstallFixture,
) -> None:
    runtime = prepared.plan.install.runtime
    (
        _catalog,
        _repository,
        foreign_plan,
        foreign_coverage,
        _baseline,
        _projection,
    ) = _project_empty_cutover_install(
        repository_epoch="f" * 64,
        source_admission_revision=runtime.source_admission_revision,
        runtime_generation=runtime.runtime_generation,
        code_digest=runtime.code_digest,
        install_committed_at=prepared.install_committed_at,
    )
    connection, _filesystem = prepared.database._open_unvalidated()
    try:
        connection.execute("BEGIN IMMEDIATE")
        _insert_coverage_receipt(
            connection,
            plan=foreign_plan,
            coverage_plan=foreign_coverage,
            committed_at=prepared.install_committed_at,
        )
        connection.commit()
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()
    result = _make_cutover_install_writer(prepared).reconcile(prepared.projection)
    assert result.status is SQLiteCutoverInstallStatus.CORRUPT
    assert result.code == "cutover_install_reserved_receipt_namespace_corrupt"
    (
        _third_catalog,
        _third_repository,
        _third_plan,
        _third_coverage,
        _third_baseline,
        third,
    ) = _project_empty_cutover_install(
        repository_epoch="d" * 64,
        source_admission_revision=runtime.source_admission_revision,
        runtime_generation=runtime.runtime_generation,
        code_digest=runtime.code_digest,
        install_committed_at=prepared.install_committed_at,
    )
    foreign_result = _make_cutover_install_writer(prepared).reconcile(third)
    assert foreign_result.status is SQLiteCutoverInstallStatus.CORRUPT
    assert foreign_result.code == "cutover_install_reserved_receipt_namespace_corrupt"


def test_foreign_request_cannot_relabel_partial_sqlite_runtime_as_conflict(
    prepared: SQLiteCutoverInstallFixture,
) -> None:
    writer = _make_cutover_install_writer(prepared)
    connection, _filesystem = prepared.database._open_unvalidated()
    try:
        connection.execute("BEGIN IMMEDIATE")
        authorizer = _CutoverInstallAuthorizer()
        connection.set_authorizer(authorizer)
        writer._write_install_rows_before_closure(connection, prepared.projection)
        writer_module._reset_authorizer(connection)
        connection.commit()
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()

    assert writer.reconcile(prepared.projection).status is (
        SQLiteCutoverInstallStatus.CORRUPT
    )
    runtime = prepared.plan.install.runtime
    (
        _catalog,
        _repository,
        _plan,
        _coverage,
        _baseline,
        foreign,
    ) = _project_empty_cutover_install(
        repository_epoch="f" * 64,
        source_admission_revision=runtime.source_admission_revision,
        runtime_generation=runtime.runtime_generation,
        code_digest=runtime.code_digest,
        install_committed_at=prepared.install_committed_at,
    )
    assert foreign.cutover_id != prepared.projection.cutover_id
    result = writer.reconcile(foreign)
    assert result.status is SQLiteCutoverInstallStatus.CORRUPT
    assert result.code == "cutover_install_closure_corrupt"


def test_valid_stored_install_with_different_projection_is_conflict(
    prepared: SQLiteCutoverInstallFixture,
) -> None:
    writer = _make_cutover_install_writer(prepared)
    assert (
        writer.install(prepared.projection).status
        is SQLiteCutoverInstallStatus.INSTALLED_READ_ONLY
    )
    runtime = prepared.plan.install.runtime
    (
        _catalog,
        _repository,
        _plan,
        _coverage,
        _baseline,
        different,
    ) = _project_empty_cutover_install(
        repository_epoch=runtime.repository_epoch,
        source_admission_revision=runtime.source_admission_revision,
        runtime_generation=runtime.runtime_generation,
        code_digest=runtime.code_digest,
        install_committed_at="2026-07-12T00:00:00.001Z",
    )
    assert different.cutover_id == prepared.projection.cutover_id
    assert (
        different.closure.manifest_sha256 != prepared.projection.closure.manifest_sha256
    )
    result = writer.reconcile(different)
    assert result.status is SQLiteCutoverInstallStatus.CONFLICT
    assert result.code == "cutover_install_projection_conflict"


def test_stored_future_time_is_corrupt_before_foreign_projection_conflict(
    prepared: SQLiteCutoverInstallFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer = _make_cutover_install_writer(prepared)
    assert writer.install(prepared.projection).status is (
        SQLiteCutoverInstallStatus.INSTALLED_READ_ONLY
    )
    runtime = prepared.plan.install.runtime
    (
        _catalog,
        _repository,
        _plan,
        _coverage,
        _baseline,
        foreign,
    ) = _project_empty_cutover_install(
        repository_epoch="f" * 64,
        source_admission_revision=runtime.source_admission_revision,
        runtime_generation=runtime.runtime_generation,
        code_digest=runtime.code_digest,
        install_committed_at=prepared.install_committed_at,
    )
    monkeypatch.setattr(
        SQLiteCutoverInstallWriter,
        "_database_time",
        staticmethod(lambda connection: "2000-01-01T00:00:00.000Z"),
    )
    result = writer.reconcile(foreign)
    assert result.status is SQLiteCutoverInstallStatus.CORRUPT
    assert result.code == "cutover_install_closure_corrupt"


def test_extra_terminal_shadow_row_is_a_conflict(
    prepared: SQLiteCutoverInstallFixture,
) -> None:
    snapshot_bytes = b"{}"
    _commit_sql(
        prepared,
        "INSERT INTO projects(repository_singleton,project_id,generation,"
        "codec_id,codec_version,snapshot_format_version,snapshot_bytes,"
        "snapshot_sha256) VALUES (1,?,?,?,?,?,?,?)",
        (
            "unexpected-project",
            1,
            "ze.business-json",
            1,
            1,
            snapshot_bytes,
            hashlib.sha256(snapshot_bytes).digest(),
        ),
    )
    result = _make_cutover_install_writer(prepared).reconcile(prepared.projection)
    assert result.status is SQLiteCutoverInstallStatus.CONFLICT
    assert result.code == "cutover_install_terminal_shadow_conflict"


def test_authorizer_denies_terminal_shadow_writes_and_all_other_mutation() -> None:
    authorizer = _CutoverInstallAuthorizer()
    assert (
        authorizer(sqlite3.SQLITE_INSERT, "projects", None, "main", None)
        == sqlite3.SQLITE_DENY
    )
    assert (
        authorizer(sqlite3.SQLITE_INSERT, "business_commits", None, "main", None)
        == sqlite3.SQLITE_OK
    )
    assert (
        authorizer(
            sqlite3.SQLITE_UPDATE, "kernel_repository", "authority", "main", None
        )
        == sqlite3.SQLITE_OK
    )
    assert (
        authorizer(
            sqlite3.SQLITE_UPDATE, "kernel_repository", "created_at", "main", None
        )
        == sqlite3.SQLITE_DENY
    )
    assert (
        authorizer(sqlite3.SQLITE_DELETE, "business_commits", None, "main", None)
        == sqlite3.SQLITE_DENY
    )
    assert (
        authorizer(sqlite3.SQLITE_INSERT, "business_commits", None, "temp", None)
        == sqlite3.SQLITE_DENY
    )
    assert (
        authorizer(
            sqlite3.SQLITE_INSERT, "business_commits", None, "main", "hostile_trigger"
        )
        == sqlite3.SQLITE_DENY
    )
    assert (
        authorizer(sqlite3.SQLITE_PRAGMA, "foreign_keys", None, None, None)
        == sqlite3.SQLITE_DENY
    )
    if hasattr(sqlite3, "SQLITE_TRANSACTION"):
        assert (
            authorizer(sqlite3.SQLITE_TRANSACTION, "COMMIT", None, None, None)
            == sqlite3.SQLITE_DENY
        )
    for action_name in ("SQLITE_CREATE_VTABLE", "SQLITE_DROP_VTABLE"):
        if hasattr(sqlite3, action_name):
            assert (
                authorizer(getattr(sqlite3, action_name), "virtual", None, "main", None)
                == sqlite3.SQLITE_DENY
            )
    assert authorizer.denied is True


def test_real_asset_install_uses_fresh_coverage_metadata_end_to_end(
    tmp_path: Path,
) -> None:
    fixture = _create_live_cutover_install_fixture(
        tmp_path / "asset-cutover.sqlite3",
        asset_blob=TEST_LIVE_ASSET_BYTES,
    )
    baseline = fixture.baseline
    projection = fixture.projection
    assert baseline.asset_blobs[0].media_type is None
    assert baseline.asset_blobs[0].extension is None
    assert len(fixture.coverage_plan.batch_evidences) == 1
    installed = _make_cutover_install_writer(fixture).install(projection)
    assert installed.status is SQLiteCutoverInstallStatus.INSTALLED_READ_ONLY
    expected_assets = writer_module._v12_asset_blob_rows(projection)
    assert (
        _exact_rows(
            fixture.database,
            "asset_blobs",
            ASSET_BLOBS_COLUMNS,
            "asset_sha256",
        )
        == expected_assets
    )
    asset_row = dict(zip(ASSET_BLOBS_COLUMNS, expected_assets[0]))
    assert asset_row["media_type"] == "image/png"
    assert asset_row["extension"] == ".png"
    assert asset_row["byte_length"] == len(TEST_LIVE_ASSET_BYTES)
    assert _exact_rows(
        fixture.database,
        "document_asset_refs",
        DOCUMENT_ASSET_REFS_COLUMNS,
        "commit_id,ordinal",
    ) == tuple(
        row.sql_values() for live in baseline.live for row in live.asset_references
    )
    connection, _filesystem = fixture.database._open_unvalidated()
    try:
        assert (
            connection.execute(
                "SELECT count(*) FROM business_asset_stage_receipts"
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM business_asset_stage_members"
            ).fetchone()[0]
            == 3
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchone() is None
    finally:
        connection.close()
    assert (
        _make_cutover_install_writer(fixture).reconcile(projection).status
        is SQLiteCutoverInstallStatus.INSTALLED_READ_ONLY
    )


def test_invalid_projection_is_rejected_before_io(
    prepared: SQLiteCutoverInstallFixture,
) -> None:
    writer = _make_cutover_install_writer(prepared)
    with pytest.raises(SQLiteCutoverInstallError) as failure:
        writer.reconcile(object())
    assert failure.value.code == "cutover_install_projection_invalid"
    with pytest.raises(TypeError):
        writer.install(prepared.projection, _test_hook=object())


def test_postcommit_durability_failure_is_unknown_but_remains_reconcilable(
    prepared: SQLiteCutoverInstallFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = SqliteDatabase._finalize_committed_storage

    def fail_barrier(self) -> None:
        raise OSError("injected-durability-failure")

    monkeypatch.setattr(SqliteDatabase, "_finalize_committed_storage", fail_barrier)
    writer = _make_cutover_install_writer(prepared)
    result = writer.install(prepared.projection)
    assert result.status is SQLiteCutoverInstallStatus.UNKNOWN
    assert result.code == "cutover_install_durability_barrier_failed"
    monkeypatch.setattr(SqliteDatabase, "_finalize_committed_storage", original)
    assert (
        writer.reconcile(prepared.projection).status
        is SQLiteCutoverInstallStatus.INSTALLED_READ_ONLY
    )


def test_reconcile_separates_schema_drift_from_transient_unknown(
    prepared: SQLiteCutoverInstallFixture,
) -> None:
    _commit_sql(prepared, "CREATE TABLE writer_test_schema_drift(value INTEGER)")
    result = _make_cutover_install_writer(prepared).reconcile(prepared.projection)
    assert result.status is SQLiteCutoverInstallStatus.CORRUPT
    assert result.code == "cutover_install_schema_corrupt"


def test_reconcile_current_version_conflict_is_not_unknown(
    prepared: SQLiteCutoverInstallFixture,
) -> None:
    _commit_sql(prepared, "PRAGMA user_version=11")
    result = _make_cutover_install_writer(prepared).reconcile(prepared.projection)
    assert result.status is SQLiteCutoverInstallStatus.CONFLICT
    assert result.code == "cutover_install_schema_version_conflict"


def test_active_mounted_suffix_still_reconciles_historical_install_exact(
    tmp_path: Path,
) -> None:
    repository_epoch = "e" * 64
    runtime_generation = "runtime-production-v8-test"
    code_digest = hashlib.sha256(b"ze.business-runtime.test.v1").digest()
    mounted = create_canonical_mounted_business_database(
        tmp_path / "mounted.sqlite3",
        repository_epoch=repository_epoch,
        runtime_generation=runtime_generation,
        code_digest=code_digest,
    )
    (
        _catalog,
        _repository,
        _plan,
        _coverage,
        _baseline,
        projection,
    ) = _project_empty_cutover_install(
        repository_epoch=repository_epoch,
        source_admission_revision=1,
        runtime_generation=runtime_generation,
        code_digest=code_digest,
        install_committed_at=TEST_CUTOVER_COMMITTED_AT,
    )
    writer = SQLiteCutoverInstallWriter(
        mounted.database,
        _capability=_CUTOVER_INSTALL_WRITER_CAPABILITY,
    )
    reconciled = writer.reconcile(projection)
    assert reconciled.status is SQLiteCutoverInstallStatus.INSTALLED_READ_ONLY
    retried = writer.install(projection)
    assert retried.status is SQLiteCutoverInstallStatus.INSTALLED_READ_ONLY
    assert retried.recovered_after_ack_loss is True


def test_only_the_requested_module_surface_is_private() -> None:
    assert writer_module.__all__ == ()
    assert writer_module._FIXED_WRITE_SQL
    assert writer_module._INSERT_INSTALL_CLOSURE_SQL in writer_module._FIXED_WRITE_SQL
