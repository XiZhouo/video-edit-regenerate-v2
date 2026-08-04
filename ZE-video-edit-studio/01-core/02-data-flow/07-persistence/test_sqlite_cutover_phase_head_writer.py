"""Real-SQL evidence for the private V12 phase-1 head finalizer."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
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

import sqlite_cutover_phase_head_writer as writer_module
import sqlite_cutover_verification as verification_module
from business_cutover_head_integrity import _issue_initial_business_cutover_head
from sqlite_cutover_durable_audit import SQLiteCutoverDurablePhase
from sqlite_cutover_durable_audit_test_support import (
    create_canonical_cutover_phase_database,
)
from sqlite_cutover_install_closure_reader import SQLiteCutoverInstallClosureError
from sqlite_cutover_install_test_support import (
    SQLiteCutoverInstallFixture,
    _create_empty_cutover_install_fixture,
    _create_live_cutover_install_fixture,
    _make_cutover_install_writer,
)
from sqlite_cutover_phase_head_writer import (
    SQLiteCutoverPhaseHeadResult,
    SQLiteCutoverPhaseHeadStatus,
    SQLiteCutoverPhaseHeadTestPoint,
    SQLiteCutoverPhaseHeadWriter,
    _CUTOVER_PHASE_HEAD_WRITER_CAPABILITY,
    _PhaseHeadAuthorizer,
)
from sqlite_kernel import SqliteDatabase
from sqlite_production_authority import _CUTOVER_HEAD_ROW_COLUMNS


@pytest.fixture(params=("empty", "live"))
def installed(
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> SQLiteCutoverInstallFixture:
    path = tmp_path / (str(request.param) + ".sqlite3")
    fixture = (
        _create_empty_cutover_install_fixture(path)
        if request.param == "empty"
        else _create_live_cutover_install_fixture(path)
    )
    result = _make_cutover_install_writer(fixture).install(fixture.projection)
    assert result.status.value == "installed_read_only"
    return fixture


def _writer(fixture: SQLiteCutoverInstallFixture) -> SQLiteCutoverPhaseHeadWriter:
    return SQLiteCutoverPhaseHeadWriter(
        fixture.database,
        _capability=_CUTOVER_PHASE_HEAD_WRITER_CAPABILITY,
    )


def _head_rows(fixture: SQLiteCutoverInstallFixture) -> tuple[tuple, ...]:
    return _database_head_rows(fixture.database)


def _database_head_rows(database: SqliteDatabase) -> tuple[tuple, ...]:
    connection, _filesystem = database._open_unvalidated()
    try:
        rows = connection.execute(
            "SELECT " + ",".join(_CUTOVER_HEAD_ROW_COLUMNS)
            + " FROM business_cutover_head ORDER BY singleton"
        ).fetchall()
        return tuple(tuple(row[index] for index in range(len(row))) for row in rows)
    finally:
        connection.close()


def test_writer_consumes_neutral_cutover_verification_leaf() -> None:
    assert (
        writer_module._read_cutover_verification_budget
        is verification_module._read_cutover_verification_budget
    )
    assert (
        writer_module._install_cutover_verification_progress_budget
        is verification_module._install_cutover_verification_progress_budget
    )
    assert (
        writer_module._verify_legacy_receipt_namespace
        is verification_module._verify_legacy_receipt_namespace
    )
    assert (
        writer_module._verify_production_receipt_namespace
        is verification_module._verify_production_receipt_namespace
    )
    assert (
        writer_module._failure_is_unavailable
        is verification_module._failure_is_unavailable
    )


def test_constructor_requires_exact_database_and_private_capability(tmp_path: Path) -> None:
    database = SqliteDatabase((tmp_path / "unused.sqlite3").resolve())
    with pytest.raises(TypeError):
        SQLiteCutoverPhaseHeadWriter(database, _capability=object())
    with pytest.raises(TypeError):
        SQLiteCutoverPhaseHeadWriter(
            object(),
            _capability=_CUTOVER_PHASE_HEAD_WRITER_CAPABILITY,
        )


def test_result_uses_five_state_classification() -> None:
    assert tuple(value.value for value in SQLiteCutoverPhaseHeadStatus) == (
        "exact",
        "absent",
        "conflict",
        "corrupt",
        "unknown",
    )
    with pytest.raises(TypeError):
        SQLiteCutoverPhaseHeadResult(
            SQLiteCutoverPhaseHeadStatus.EXACT,
            "missing-identity",
        )


def test_reconcile_reports_exact_closure_as_ready_absence(
    installed: SQLiteCutoverInstallFixture,
) -> None:
    result = _writer(installed).reconcile()
    assert result.status is SQLiteCutoverPhaseHeadStatus.ABSENT
    assert result.code == "cutover_phase_head_installed_closure_ready"
    assert result.phase is SQLiteCutoverDurablePhase.INSTALLED_CLOSURE
    assert result.identity is not None
    assert result.recovered_after_ack_loss is False
    assert _head_rows(installed) == ()
    with pytest.raises(TypeError):
        SQLiteCutoverPhaseHeadResult(
            SQLiteCutoverPhaseHeadStatus.EXACT,
            "closure-is-not-finalized",
            result.identity,
        )


def test_finalize_persists_exact_canonical_phase_one_row(
    installed: SQLiteCutoverInstallFixture,
) -> None:
    result = _writer(installed).finalize()
    assert result.status is SQLiteCutoverPhaseHeadStatus.EXACT
    assert result.code == "cutover_phase_head_committed"
    assert result.phase is SQLiteCutoverDurablePhase.INSTALLED_HEAD
    assert result.identity is not None
    assert result.identity.head_sha256 is not None
    with pytest.raises(TypeError):
        SQLiteCutoverPhaseHeadResult(
            SQLiteCutoverPhaseHeadStatus.ABSENT,
            "head-is-not-absent",
            result.identity,
        )

    install = installed.plan.install
    expected = _issue_initial_business_cutover_head(
        cutover_id=install.cutover_id,
        repository_epoch=install.runtime.repository_epoch,
        runtime_generation=install.runtime.runtime_generation,
        code_digest=install.runtime.code_digest,
        install=writer_module.BusinessCutoverHeadPointer(
            writer_module.BusinessCutoverHeadPointerRole.INSTALL,
            install.transaction_id,
            install.receipt_kind,
            installed.install_committed_at,
            install.manifest_sha256,
        ),
    )
    values = expected.row_values()
    assert _head_rows(installed) == (
        tuple(values[name] for name in _CUTOVER_HEAD_ROW_COLUMNS),
    )


def test_finalize_is_idempotent_and_reports_recovered_existing_head(
    installed: SQLiteCutoverInstallFixture,
) -> None:
    writer = _writer(installed)
    assert writer.finalize().status is SQLiteCutoverPhaseHeadStatus.EXACT
    retry = writer.finalize()
    assert retry.status is SQLiteCutoverPhaseHeadStatus.EXACT
    assert retry.code == "cutover_phase_head_installed_head"
    assert retry.phase is SQLiteCutoverDurablePhase.INSTALLED_HEAD
    assert retry.recovered_after_ack_loss is True
    assert len(_head_rows(installed)) == 1


@pytest.mark.parametrize(
    ("phase_revision", "expected_phase", "expected_code"),
    (
        (
            1,
            SQLiteCutoverDurablePhase.INSTALLED_HEAD,
            "cutover_phase_head_installed_head",
        ),
        (
            2,
            SQLiteCutoverDurablePhase.ASSETS_COVERED,
            "cutover_phase_head_already_advanced",
        ),
        (
            3,
            SQLiteCutoverDurablePhase.AUDITED,
            "cutover_phase_head_already_advanced",
        ),
        (
            4,
            SQLiteCutoverDurablePhase.ACTIVATED,
            "cutover_phase_head_already_advanced",
        ),
        (
            5,
            SQLiteCutoverDurablePhase.MOUNTED,
            "cutover_phase_head_already_advanced",
        ),
    ),
)
def test_finalize_never_downgrades_an_existing_canonical_phase(
    tmp_path: Path,
    phase_revision: int,
    expected_phase: SQLiteCutoverDurablePhase,
    expected_code: str,
) -> None:
    database = create_canonical_cutover_phase_database(
        tmp_path / ("phase-%d.sqlite3" % phase_revision),
        phase_revision=phase_revision,
    )
    before = _database_head_rows(database)
    result = SQLiteCutoverPhaseHeadWriter(
        database,
        _capability=_CUTOVER_PHASE_HEAD_WRITER_CAPABILITY,
    ).finalize()
    assert result.status is SQLiteCutoverPhaseHeadStatus.EXACT
    assert result.phase is expected_phase
    assert result.code == expected_code
    assert result.recovered_after_ack_loss is True
    assert _database_head_rows(database) == before


def test_before_commit_failure_rolls_back_to_exact_closure(
    installed: SQLiteCutoverInstallFixture,
) -> None:
    def fail(point: SQLiteCutoverPhaseHeadTestPoint) -> None:
        assert point is SQLiteCutoverPhaseHeadTestPoint.BEFORE_COMMIT
        raise RuntimeError("injected-before-commit")

    result = _writer(installed).finalize(_test_hook=fail)
    assert result.status is SQLiteCutoverPhaseHeadStatus.ABSENT
    assert result.code == "cutover_phase_head_rolled_back"
    assert result.phase is SQLiteCutoverDurablePhase.INSTALLED_CLOSURE
    assert _head_rows(installed) == ()


def test_after_commit_ack_loss_freshly_recovers_exact_head(
    installed: SQLiteCutoverInstallFixture,
) -> None:
    def fail(point: SQLiteCutoverPhaseHeadTestPoint) -> None:
        if point is SQLiteCutoverPhaseHeadTestPoint.AFTER_COMMIT_BEFORE_ACK:
            raise RuntimeError("injected-ack-loss")

    result = _writer(installed).finalize(_test_hook=fail)
    assert result.status is SQLiteCutoverPhaseHeadStatus.EXACT
    assert result.phase is SQLiteCutoverDurablePhase.INSTALLED_HEAD
    assert result.recovered_after_ack_loss is True
    assert len(_head_rows(installed)) == 1


def test_transaction_reproves_full_snapshot_before_and_after_insert(
    installed: SQLiteCutoverInstallFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = []
    original = SQLiteCutoverPhaseHeadWriter._prove_snapshot

    def wrapped(self, connection, *, expected_phase_revision):
        observed.append(expected_phase_revision)
        return original(
            self,
            connection,
            expected_phase_revision=expected_phase_revision,
        )

    monkeypatch.setattr(SQLiteCutoverPhaseHeadWriter, "_prove_snapshot", wrapped)
    assert _writer(installed).finalize().status is SQLiteCutoverPhaseHeadStatus.EXACT
    assert observed == [0, 1]


@pytest.mark.parametrize("target_proof", (0, 1))
def test_transaction_authorizer_blocks_dml_inside_each_snapshot_proof(
    installed: SQLiteCutoverInstallFixture,
    monkeypatch: pytest.MonkeyPatch,
    target_proof: int,
) -> None:
    original = SQLiteCutoverPhaseHeadWriter._prove_snapshot

    def wrapped(self, connection, *, expected_phase_revision):
        if expected_phase_revision == target_proof:
            connection.execute(
                "UPDATE kernel_repository SET authority=authority "
                "WHERE singleton=1"
            )
        return original(
            self,
            connection,
            expected_phase_revision=expected_phase_revision,
        )

    monkeypatch.setattr(SQLiteCutoverPhaseHeadWriter, "_prove_snapshot", wrapped)
    result = _writer(installed).finalize()
    assert result.status is SQLiteCutoverPhaseHeadStatus.CORRUPT
    assert result.code == "cutover_phase_head_authorizer_denied"
    assert _head_rows(installed) == ()


def test_detached_ready_identity_cannot_authorize_an_uninstalled_database(
    installed: SQLiteCutoverInstallFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    detached = _writer(installed).reconcile()
    target = _create_empty_cutover_install_fixture(tmp_path / "target.sqlite3")
    target_writer = _writer(target)
    monkeypatch.setattr(
        SQLiteCutoverPhaseHeadWriter,
        "reconcile",
        lambda self: detached,
    )
    result = target_writer.finalize()
    assert result.status in (
        SQLiteCutoverPhaseHeadStatus.CORRUPT,
        SQLiteCutoverPhaseHeadStatus.UNKNOWN,
    )
    assert _head_rows(target) == ()


def test_initial_detached_receipt_identity_cannot_hide_namespace_drift(
    installed: SQLiteCutoverInstallFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer = _writer(installed)
    original = SQLiteCutoverPhaseHeadWriter.reconcile
    calls = 0

    def drifting_reconcile(self):
        nonlocal calls
        result = original(self)
        calls += 1
        if calls == 1:
            connection, _filesystem = installed.database._open_unvalidated()
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT INTO transaction_receipts("
                    "transaction_id,kind,committed_at) VALUES (?,?,?)",
                    (
                        "phase-head-freshness-drift",
                        "phase-head-test-receipt",
                        installed.install_committed_at,
                    ),
                )
                connection.commit()
            finally:
                connection.close()
            installed.database._finalize_committed_storage()
        return result

    monkeypatch.setattr(
        SQLiteCutoverPhaseHeadWriter,
        "reconcile",
        drifting_reconcile,
    )
    result = writer.finalize()
    assert result.status is SQLiteCutoverPhaseHeadStatus.CONFLICT
    assert result.code == "cutover_phase_head_identity_changed"
    assert _head_rows(installed) == ()


def test_transaction_closure_reader_failure_cannot_write_head(
    installed: SQLiteCutoverInstallFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args, **kwargs):
        raise SQLiteCutoverInstallClosureError("injected-closure-failure")

    monkeypatch.setattr(writer_module, "_read_business_cutover_install_closure", fail)
    result = _writer(installed).finalize()
    assert result.status is SQLiteCutoverPhaseHeadStatus.CORRUPT
    assert _head_rows(installed) == ()


def test_authorizer_denies_every_mutation_except_main_head_insert() -> None:
    authorizer = _PhaseHeadAuthorizer()
    assert authorizer(
        sqlite3.SQLITE_INSERT,
        "business_cutover_head",
        None,
        "main",
        None,
    ) == sqlite3.SQLITE_DENY
    authorizer = _PhaseHeadAuthorizer()
    authorizer.open_head_insert()
    assert authorizer(
        sqlite3.SQLITE_INSERT,
        "business_cutover_head",
        None,
        "main",
        None,
    ) == sqlite3.SQLITE_OK
    authorizer.close_head_insert()
    assert authorizer(
        sqlite3.SQLITE_INSERT,
        "business_cutover_head",
        None,
        "main",
        None,
    ) == sqlite3.SQLITE_DENY
    assert authorizer(
        sqlite3.SQLITE_INSERT,
        "repository_runtime_control",
        None,
        "main",
        None,
    ) == sqlite3.SQLITE_DENY
    assert authorizer(
        sqlite3.SQLITE_UPDATE,
        "business_cutover_head",
        "phase_revision",
        "main",
        None,
    ) == sqlite3.SQLITE_DENY
    assert authorizer.denied is True


def test_authorizer_denial_rolls_back_without_runtime_change(
    installed: SQLiteCutoverInstallFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = SQLiteCutoverPhaseHeadWriter._insert_state

    def forbidden(connection, state):
        connection.execute(
            "UPDATE repository_runtime_control SET state_revision=2 WHERE singleton=1"
        )
        original(connection, state)

    monkeypatch.setattr(
        SQLiteCutoverPhaseHeadWriter,
        "_insert_state",
        staticmethod(forbidden),
    )
    result = _writer(installed).finalize()
    assert result.status is SQLiteCutoverPhaseHeadStatus.CORRUPT
    assert result.code == "cutover_phase_head_authorizer_denied"
    assert _head_rows(installed) == ()


def test_committed_storage_barrier_failure_is_unknown_but_retry_recovers(
    installed: SQLiteCutoverInstallFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = SqliteDatabase._finalize_committed_storage

    def fail(self):
        raise OSError("injected-durability-failure")

    monkeypatch.setattr(SqliteDatabase, "_finalize_committed_storage", fail)
    result = _writer(installed).finalize()
    assert result.status is SQLiteCutoverPhaseHeadStatus.UNKNOWN
    assert result.code == "cutover_phase_head_durability_barrier_failed"
    assert len(_head_rows(installed)) == 1
    monkeypatch.setattr(SqliteDatabase, "_finalize_committed_storage", original)
    retry = _writer(installed).finalize()
    assert retry.status is SQLiteCutoverPhaseHeadStatus.EXACT
    assert retry.recovered_after_ack_loss is True


@pytest.mark.parametrize("signal", (KeyboardInterrupt, SystemExit))
def test_process_control_before_commit_is_preserved_and_rolls_back(
    installed: SQLiteCutoverInstallFixture,
    signal: type[BaseException],
) -> None:
    def interrupt(point: SQLiteCutoverPhaseHeadTestPoint) -> None:
        assert point is SQLiteCutoverPhaseHeadTestPoint.BEFORE_COMMIT
        raise signal("stop")

    with pytest.raises(signal):
        _writer(installed).finalize(_test_hook=interrupt)
    assert _head_rows(installed) == ()


def test_process_control_after_commit_is_preserved_and_head_recovers(
    installed: SQLiteCutoverInstallFixture,
) -> None:
    def interrupt(point: SQLiteCutoverPhaseHeadTestPoint) -> None:
        if point is SQLiteCutoverPhaseHeadTestPoint.AFTER_COMMIT_BEFORE_ACK:
            raise KeyboardInterrupt("stop-after-commit")

    with pytest.raises(KeyboardInterrupt):
        _writer(installed).finalize(_test_hook=interrupt)
    result = _writer(installed).reconcile()
    assert result.status is SQLiteCutoverPhaseHeadStatus.EXACT
    assert result.phase is SQLiteCutoverDurablePhase.INSTALLED_HEAD


def test_two_concurrent_finalizers_converge_on_one_exact_head(
    installed: SQLiteCutoverInstallFixture,
) -> None:
    def finalize() -> SQLiteCutoverPhaseHeadResult:
        return _writer(installed).finalize()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(lambda _value: finalize(), range(2)))
    assert all(result.status is SQLiteCutoverPhaseHeadStatus.EXACT for result in results)
    assert all(
        result.phase is SQLiteCutoverDurablePhase.INSTALLED_HEAD
        for result in results
    )
    assert len(_head_rows(installed)) == 1


def test_fixed_insert_is_the_only_declared_write_statement() -> None:
    assert writer_module._FIXED_WRITE_SQL == frozenset(
        (writer_module._INSERT_PHASE_HEAD_SQL,)
    )
    normalized = " ".join(writer_module._INSERT_PHASE_HEAD_SQL.split()).upper()
    assert normalized.startswith("INSERT INTO BUSINESS_CUTOVER_HEAD(")
    assert "UPDATE " not in normalized
    assert "DELETE " not in normalized


def test_module_is_private() -> None:
    assert writer_module.__all__ == ()
