"""Audit-only real-SQL evidence for the private V12 phase-1 head writer.

This is deliberately a protocol probe rather than production wiring.  It may
use the private install fixture to construct durable databases, but it never
exports the phase-head capability to a runtime/bootstrap surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3
import tempfile
import threading
from typing import Tuple

import sqlite_cutover_phase_head_writer as writer_module
from business_cutover_head_integrity import _issue_initial_business_cutover_head
from sqlite_cutover_install_test_support import (
    _create_empty_cutover_install_fixture,
    _make_cutover_install_writer,
)
from sqlite_cutover_phase_head_writer import (
    SQLiteCutoverPhaseHeadTestPoint,
    SQLiteCutoverPhaseHeadWriter,
    _CUTOVER_PHASE_HEAD_WRITER_CAPABILITY,
    _PhaseHeadAuthorizer,
    _execute_fixed,
    _reset_authorizer,
)
from sqlite_kernel import SQLiteSettings, SqliteDatabase
from sqlite_production_authority import _CUTOVER_HEAD_ROW_COLUMNS


@dataclass(frozen=True)
class CutoverPhaseHeadWriterEvidence:
    no_install_status: str
    no_install_code: str
    ready_status: str
    ready_code: str
    ready_phase: str
    committed_status: str
    committed_code: str
    committed_phase: str
    canonical_row_exact: bool
    head_row_count: int
    retry_status: str
    retry_code: str
    retry_recovered: bool
    rollback_status: str
    rollback_code: str
    rollback_phase: str
    rollback_head_count: int
    ack_status: str
    ack_phase: str
    ack_recovered: bool
    barrier_status: str
    barrier_code: str
    barrier_fresh_status: str
    barrier_fresh_phase: str
    busy_status: str
    busy_code: str
    race_statuses: Tuple[str, str]
    race_phases: Tuple[str, str]
    race_recovered_count: int
    race_head_count: int
    authorizer_rejected_other_insert: bool
    fixed_sql_rejected_other_insert: bool
    preproof_dml_blocked: bool
    postproof_dml_blocked: bool
    detached_status: str
    detached_head_count: int
    cleanup_status: str
    cleanup_code: str
    cleanup_head_count: int
    process_control_preserved: bool
    process_control_head_count: int


def _writer(fixture) -> SQLiteCutoverPhaseHeadWriter:
    return SQLiteCutoverPhaseHeadWriter(
        fixture.database,
        _capability=_CUTOVER_PHASE_HEAD_WRITER_CAPABILITY,
    )


def _head_rows(fixture) -> tuple[tuple, ...]:
    connection = None
    try:
        connection, _filesystem = fixture.database._open_unvalidated()
        rows = connection.execute(
            "SELECT " + ",".join(_CUTOVER_HEAD_ROW_COLUMNS)
            + " FROM business_cutover_head ORDER BY singleton"
        ).fetchall()
        return tuple(tuple(row[index] for index in range(len(row))) for row in rows)
    finally:
        if connection is not None:
            connection.close()


def _write_guards(database: SqliteDatabase) -> tuple[bool, bool]:
    connection = None
    authorizer_rejected = False
    fixed_sql_rejected = False
    try:
        connection, _filesystem = database._open_unvalidated()
        connection.execute("BEGIN IMMEDIATE")
        authorizer = _PhaseHeadAuthorizer()
        connection.set_authorizer(authorizer)
        try:
            connection.execute(
                "INSERT INTO repository_runtime_control DEFAULT VALUES"
            )
        except sqlite3.DatabaseError:
            authorizer_rejected = authorizer.denied
        finally:
            _reset_authorizer(connection)
        try:
            _execute_fixed(
                connection,
                "INSERT INTO repository_runtime_control DEFAULT VALUES",
                (),
            )
        except RuntimeError as exc:
            fixed_sql_rejected = str(exc) == "cutover_phase_head_fixed_sql_invalid"
        connection.rollback()
        return authorizer_rejected, fixed_sql_rejected
    finally:
        if connection is not None:
            if connection.in_transaction:
                connection.rollback()
            connection.close()


def _race(fixture) -> tuple[Tuple[str, str], Tuple[str, str], int]:
    barrier = threading.Barrier(2)
    lock = threading.Lock()
    results = []
    failures = []

    def compete() -> None:
        try:
            contender = _writer(fixture)
            barrier.wait(timeout=15.0)
            result = contender.finalize()
        except BaseException as exc:
            with lock:
                failures.append(exc)
            return
        with lock:
            results.append(result)

    threads = tuple(
        threading.Thread(target=compete, daemon=True) for _index in range(2)
    )
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30.0)
    if any(thread.is_alive() for thread in threads):
        raise AssertionError("cutover phase-head race did not terminate")
    if failures:
        failure = failures[0]
        raise AssertionError(
            "cutover phase-head race failed: %s:%s"
            % (type(failure).__name__, str(failure))
        ) from failure
    if len(results) != 2:
        raise AssertionError("cutover phase-head race lost a result")
    return (
        tuple(sorted(result.status.value for result in results)),
        tuple(sorted(result.phase.value for result in results)),
        sum(bool(result.recovered_after_ack_loss) for result in results),
    )


def _proof_dml_blocked(root: Path, target: int) -> bool:
    fixture = _create_empty_cutover_install_fixture(
        root / ("proof-dml-%d.sqlite3" % target)
    )
    _make_cutover_install_writer(fixture).install(fixture.projection)
    original = SQLiteCutoverPhaseHeadWriter._prove_snapshot

    def injected(self, connection, *, expected_phase_revision: int):
        if expected_phase_revision == target:
            connection.execute(
                "UPDATE kernel_repository SET authority=authority "
                "WHERE singleton=1"
            )
        return original(
            self,
            connection,
            expected_phase_revision=expected_phase_revision,
        )

    SQLiteCutoverPhaseHeadWriter._prove_snapshot = injected
    try:
        result = _writer(fixture).finalize()
    finally:
        SQLiteCutoverPhaseHeadWriter._prove_snapshot = original
    return bool(
        result.status.value == "corrupt"
        and result.code == "cutover_phase_head_authorizer_denied"
        and not _head_rows(fixture)
    )


def run_cutover_phase_head_writer_probe() -> CutoverPhaseHeadWriterEvidence:
    """Exercise the finalizer's real transaction and recovery matrix."""

    with tempfile.TemporaryDirectory(prefix="ze-cutover-phase-head-audit-") as raw:
        root = Path(raw)

        no_install_database = SqliteDatabase((root / "no-install.sqlite3").resolve())
        no_install_database.initialize()
        no_install = SQLiteCutoverPhaseHeadWriter(
            no_install_database,
            _capability=_CUTOVER_PHASE_HEAD_WRITER_CAPABILITY,
        ).reconcile()

        normal = _create_empty_cutover_install_fixture(root / "normal.sqlite3")
        normal_writer = _writer(normal)
        installed = _make_cutover_install_writer(normal).install(normal.projection)
        if installed.status.value != "installed_read_only":
            raise AssertionError("phase-head probe could not install its fixture")
        ready = normal_writer.reconcile()
        authorizer_rejected, fixed_sql_rejected = _write_guards(normal.database)
        preproof_dml_blocked = _proof_dml_blocked(root, 0)
        postproof_dml_blocked = _proof_dml_blocked(root, 1)
        committed = normal_writer.finalize()
        rows = _head_rows(normal)
        install = normal.plan.install
        expected = _issue_initial_business_cutover_head(
            cutover_id=install.cutover_id,
            repository_epoch=install.runtime.repository_epoch,
            runtime_generation=install.runtime.runtime_generation,
            code_digest=install.runtime.code_digest,
            install=writer_module.BusinessCutoverHeadPointer(
                writer_module.BusinessCutoverHeadPointerRole.INSTALL,
                install.transaction_id,
                install.receipt_kind,
                normal.install_committed_at,
                install.manifest_sha256,
            ),
        )
        expected_values = expected.row_values()
        expected_row = tuple(
            expected_values[name] for name in _CUTOVER_HEAD_ROW_COLUMNS
        )
        canonical_row_exact = rows == (expected_row,)
        retry = normal_writer.finalize()

        rollback_fixture = _create_empty_cutover_install_fixture(
            root / "rollback.sqlite3"
        )
        _make_cutover_install_writer(rollback_fixture).install(
            rollback_fixture.projection
        )

        def fail_before(point: SQLiteCutoverPhaseHeadTestPoint) -> None:
            if point is SQLiteCutoverPhaseHeadTestPoint.BEFORE_COMMIT:
                raise RuntimeError("audit-before-commit")

        rollback = _writer(rollback_fixture).finalize(_test_hook=fail_before)

        ack_fixture = _create_empty_cutover_install_fixture(root / "ack.sqlite3")
        _make_cutover_install_writer(ack_fixture).install(ack_fixture.projection)

        def fail_after(point: SQLiteCutoverPhaseHeadTestPoint) -> None:
            if point is SQLiteCutoverPhaseHeadTestPoint.AFTER_COMMIT_BEFORE_ACK:
                raise RuntimeError("audit-after-commit")

        ack = _writer(ack_fixture).finalize(_test_hook=fail_after)

        barrier_fixture = _create_empty_cutover_install_fixture(
            root / "barrier.sqlite3"
        )
        _make_cutover_install_writer(barrier_fixture).install(
            barrier_fixture.projection
        )
        barrier_writer = _writer(barrier_fixture)
        original_finalize = SqliteDatabase._finalize_committed_storage

        def fail_finalize(self) -> None:
            del self
            raise OSError("audit-durability-barrier")

        SqliteDatabase._finalize_committed_storage = fail_finalize
        try:
            barrier_result = barrier_writer.finalize()
        finally:
            SqliteDatabase._finalize_committed_storage = original_finalize
        barrier_fresh = barrier_writer.reconcile()

        busy_fixture = _create_empty_cutover_install_fixture(root / "busy.sqlite3")
        _make_cutover_install_writer(busy_fixture).install(busy_fixture.projection)
        busy_fixture.database.settings = SQLiteSettings(busy_timeout_ms=25)
        blocker = sqlite3.connect(
            str(busy_fixture.database.path), isolation_level=None, timeout=0.1
        )
        try:
            blocker.execute("PRAGMA busy_timeout=100")
            blocker.execute("BEGIN IMMEDIATE")
            blocker.execute(
                "UPDATE kernel_repository SET authority=authority WHERE singleton=1"
            )
            busy = _writer(busy_fixture).finalize()
        finally:
            if blocker.in_transaction:
                blocker.rollback()
            blocker.close()

        race_fixture = _create_empty_cutover_install_fixture(root / "race.sqlite3")
        _make_cutover_install_writer(race_fixture).install(race_fixture.projection)
        race_statuses, race_phases, race_recovered = _race(race_fixture)

        detached_fixture = _create_empty_cutover_install_fixture(
            root / "detached-source.sqlite3"
        )
        _make_cutover_install_writer(detached_fixture).install(
            detached_fixture.projection
        )
        detached = _writer(detached_fixture).reconcile()
        target = _create_empty_cutover_install_fixture(root / "detached-target.sqlite3")
        original_reconcile = SQLiteCutoverPhaseHeadWriter.reconcile
        SQLiteCutoverPhaseHeadWriter.reconcile = lambda self: detached
        try:
            detached_result = _writer(target).finalize()
        finally:
            SQLiteCutoverPhaseHeadWriter.reconcile = original_reconcile

        cleanup_fixture = _create_empty_cutover_install_fixture(
            root / "cleanup.sqlite3"
        )
        _make_cutover_install_writer(cleanup_fixture).install(
            cleanup_fixture.projection
        )
        original_rollback_close = writer_module._rollback_close

        def incomplete_rollback_close(connection) -> bool:
            original_rollback_close(connection)
            return False

        writer_module._rollback_close = incomplete_rollback_close
        try:
            cleanup = _writer(cleanup_fixture).finalize(_test_hook=fail_before)
        finally:
            writer_module._rollback_close = original_rollback_close

        process_fixture = _create_empty_cutover_install_fixture(
            root / "process.sqlite3"
        )
        _make_cutover_install_writer(process_fixture).install(
            process_fixture.projection
        )
        process_control_preserved = False

        def cleanup_interrupt(connection) -> bool:
            del connection
            raise KeyboardInterrupt("audit-cleanup-interrupt")

        def process_interrupt(point: SQLiteCutoverPhaseHeadTestPoint) -> None:
            if point is SQLiteCutoverPhaseHeadTestPoint.BEFORE_COMMIT:
                writer_module._rollback_close = cleanup_interrupt
                raise SystemExit(41)

        try:
            try:
                _writer(process_fixture).finalize(_test_hook=process_interrupt)
            except BaseException as exc:
                process_control_preserved = (
                    isinstance(exc, SystemExit) and exc.code == 41
                )
        finally:
            writer_module._rollback_close = original_rollback_close

        return CutoverPhaseHeadWriterEvidence(
            no_install_status=no_install.status.value,
            no_install_code=no_install.code,
            ready_status=ready.status.value,
            ready_code=ready.code,
            ready_phase=ready.phase.value,
            committed_status=committed.status.value,
            committed_code=committed.code,
            committed_phase=committed.phase.value,
            canonical_row_exact=canonical_row_exact,
            head_row_count=len(rows),
            retry_status=retry.status.value,
            retry_code=retry.code,
            retry_recovered=retry.recovered_after_ack_loss,
            rollback_status=rollback.status.value,
            rollback_code=rollback.code,
            rollback_phase=rollback.phase.value,
            rollback_head_count=len(_head_rows(rollback_fixture)),
            ack_status=ack.status.value,
            ack_phase=ack.phase.value,
            ack_recovered=ack.recovered_after_ack_loss,
            barrier_status=barrier_result.status.value,
            barrier_code=barrier_result.code,
            barrier_fresh_status=barrier_fresh.status.value,
            barrier_fresh_phase=barrier_fresh.phase.value,
            busy_status=busy.status.value,
            busy_code=busy.code,
            race_statuses=race_statuses,
            race_phases=race_phases,
            race_recovered_count=race_recovered,
            race_head_count=len(_head_rows(race_fixture)),
            authorizer_rejected_other_insert=authorizer_rejected,
            fixed_sql_rejected_other_insert=fixed_sql_rejected,
            preproof_dml_blocked=preproof_dml_blocked,
            postproof_dml_blocked=postproof_dml_blocked,
            detached_status=detached_result.status.value,
            detached_head_count=len(_head_rows(target)),
            cleanup_status=cleanup.status.value,
            cleanup_code=cleanup.code,
            cleanup_head_count=len(_head_rows(cleanup_fixture)),
            process_control_preserved=process_control_preserved,
            process_control_head_count=len(_head_rows(process_fixture)),
        )


__all__ = ()
