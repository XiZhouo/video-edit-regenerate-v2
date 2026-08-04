"""Audit-only runtime evidence for the private V12 install writer.

This module deliberately lives under ``_protocol`` and imports the dedicated
test-support constructor.  It is neither a production adapter nor a pytest
fixture.  ``GATE-COMMIT-12`` uses it to exercise the real fixed-SQL writer
without making the writer capability reachable from server/bootstrap code.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3
import tempfile
import threading
from typing import Tuple

import sqlite_cutover_install_writer as writer_module
from sqlite_cutover_install_test_support import (
    _create_empty_cutover_install_fixture,
    _make_cutover_install_writer,
    _project_empty_cutover_install,
)
from sqlite_cutover_install_writer import (
    SQLiteCutoverInstallStatus,
    SQLiteCutoverInstallTestPoint,
    SQLiteCutoverInstallWriter,
    _CutoverInstallAuthorizer,
    _execute_fixed,
    _reset_authorizer,
)
from sqlite_kernel import (
    SQLiteKernelError,
    SQLiteSettings,
    SqliteDatabase,
    _OwnedConnection,
)


@dataclass(frozen=True)
class CutoverInstallWriterEvidence:
    authorizer_rejected_terminal_shadow_insert: bool
    fixed_sql_rejected_terminal_shadow_insert: bool
    initial_status: str
    rollback_status: str
    rollback_code: str
    rollback_state: Tuple[str, int, int, int, int, int]
    committed_status: str
    committed_code: str
    committed_state: Tuple[str, int, int, int, int, int]
    semantic_readback_calls: int
    retry_status: str
    retry_recovered: bool
    ack_status: str
    ack_recovered: bool
    ack_state: Tuple[str, int, int, int, int, int]
    barrier_status: str
    barrier_code: str
    barrier_fresh_status: str
    busy_status: str
    busy_code: str
    blob_reserved_status: str
    blob_reserved_code: str
    blob_legacy_status: str
    blob_legacy_code: str
    cleanup_uncertain_status: str
    cleanup_uncertain_code: str
    commit_uncertain_status: str
    commit_uncertain_code: str
    stable_kernel_status: str
    stable_kernel_code: str
    process_control_preserved: bool
    same_projection_race_statuses: Tuple[str, str]
    same_projection_race_recovered_count: int
    different_projection_race_statuses: Tuple[str, str]
    different_projection_race_recovered_count: int


def _state(database: SqliteDatabase) -> Tuple[str, int, int, int, int, int]:
    connection = None
    try:
        connection, _filesystem = database._open_unvalidated()
        marker = connection.execute(
            "SELECT authority FROM kernel_repository WHERE singleton=1"
        ).fetchone()
        if marker is None:
            raise AssertionError("cutover install probe lost the repository marker")
        counts = tuple(
            int(connection.execute("SELECT count(*) FROM " + table).fetchone()[0])
            for table in (
                "business_cutover_install_receipts",
                "business_cutover_install_artifacts",
                "business_cutover_install_closures",
                "repository_runtime_control",
                "business_cutover_head",
            )
        )
        return (str(marker[0]),) + counts
    finally:
        if connection is not None:
            connection.close()


def _terminal_shadow_write_guards(database: SqliteDatabase) -> Tuple[bool, bool]:
    connection = None
    authorizer_rejected = False
    fixed_sql_rejected = False
    try:
        connection, _filesystem = database._open_unvalidated()
        connection.execute("BEGIN IMMEDIATE")
        authorizer = _CutoverInstallAuthorizer()
        connection.set_authorizer(authorizer)
        try:
            connection.execute("INSERT INTO projects DEFAULT VALUES")
        except sqlite3.DatabaseError:
            authorizer_rejected = authorizer.denied
        finally:
            _reset_authorizer(connection)
        try:
            _execute_fixed(connection, "INSERT INTO projects DEFAULT VALUES", ())
        except RuntimeError as exc:
            fixed_sql_rejected = str(exc) == "cutover_install_fixed_sql_invalid"
        connection.rollback()
        return authorizer_rejected, fixed_sql_rejected
    finally:
        if connection is not None:
            if connection.in_transaction:
                connection.rollback()
            connection.close()


def _race_install(
    fixture,
    left_projection,
    right_projection,
) -> tuple[Tuple[str, str], int]:
    barrier = threading.Barrier(2)
    lock = threading.Lock()
    results = []
    failures = []

    def compete(projection) -> None:
        try:
            writer = _make_cutover_install_writer(fixture)
            barrier.wait(timeout=15.0)
            result = writer.install(projection)
        except BaseException as exc:
            with lock:
                failures.append(exc)
            return
        with lock:
            results.append(result)

    threads = (
        threading.Thread(target=compete, args=(left_projection,), daemon=True),
        threading.Thread(target=compete, args=(right_projection,), daemon=True),
    )
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30.0)
    if any(thread.is_alive() for thread in threads):
        raise AssertionError("cutover install race probe did not terminate")
    if failures:
        failure = failures[0]
        raise AssertionError(
            "cutover install race probe failed: %s:%s"
            % (type(failure).__name__, str(failure))
        ) from failure
    if len(results) != 2:
        raise AssertionError("cutover install race probe lost a result")
    return (
        tuple(sorted(result.status.value for result in results)),
        sum(bool(result.recovered_after_ack_loss) for result in results),
    )


def _different_projection(fixture):
    connection = None
    try:
        connection, _filesystem = fixture.database._open_unvalidated()
        row = connection.execute(
            "SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')"
        ).fetchone()
        if row is None or type(row[0]) is not str:
            raise AssertionError("cutover install race probe lost database time")
        committed_at = row[0]
    finally:
        if connection is not None:
            connection.close()
    if committed_at == fixture.install_committed_at:
        raise AssertionError("cutover install race projections were not distinct")
    return _project_empty_cutover_install(
        repository_epoch=fixture.plan.legacy_proof.repository_epoch,
        source_admission_revision=(fixture.plan.legacy_proof.source_admission_revision),
        runtime_generation=fixture.plan._runtime_generation,
        code_digest=fixture.plan._code_digest,
        install_committed_at=committed_at,
    )[-1]


def run_cutover_install_writer_probe() -> CutoverInstallWriterEvidence:
    """Execute rollback, exact retry, ack-loss and durability-unknown paths."""

    with tempfile.TemporaryDirectory(prefix="ze-cutover-install-audit-") as raw:
        root = Path(raw)
        fixture = _create_empty_cutover_install_fixture(root / "normal.sqlite3")
        writer = _make_cutover_install_writer(fixture)
        authorizer_rejected, fixed_sql_rejected = _terminal_shadow_write_guards(
            fixture.database
        )
        initial = writer.reconcile(fixture.projection)

        def fail_before(point: SQLiteCutoverInstallTestPoint) -> None:
            if point is SQLiteCutoverInstallTestPoint.BEFORE_COMMIT:
                raise RuntimeError("audit-before-commit")

        rolled_back = writer.install(fixture.projection, _test_hook=fail_before)
        rollback_state = _state(fixture.database)

        original_readback = SQLiteCutoverInstallWriter._readback_exact
        readback_calls = 0

        def counted_readback(self, connection, projection):
            nonlocal readback_calls
            readback_calls += 1
            return original_readback(self, connection, projection)

        SQLiteCutoverInstallWriter._readback_exact = counted_readback
        try:
            committed = writer.install(fixture.projection)
        finally:
            SQLiteCutoverInstallWriter._readback_exact = original_readback
        committed_state = _state(fixture.database)
        retried = writer.install(fixture.projection)

        ack_fixture = _create_empty_cutover_install_fixture(root / "ack.sqlite3")
        ack_writer = _make_cutover_install_writer(ack_fixture)

        def fail_after(point: SQLiteCutoverInstallTestPoint) -> None:
            if point is SQLiteCutoverInstallTestPoint.AFTER_COMMIT_BEFORE_ACK:
                raise RuntimeError("audit-after-commit")

        ack = ack_writer.install(ack_fixture.projection, _test_hook=fail_after)
        ack_state = _state(ack_fixture.database)

        barrier_fixture = _create_empty_cutover_install_fixture(
            root / "barrier.sqlite3"
        )
        barrier_writer = _make_cutover_install_writer(barrier_fixture)
        original_finalize = SqliteDatabase._finalize_committed_storage

        def fail_finalize(self) -> None:
            del self
            raise OSError("audit-durability-barrier")

        SqliteDatabase._finalize_committed_storage = fail_finalize
        try:
            barrier = barrier_writer.install(barrier_fixture.projection)
        finally:
            SqliteDatabase._finalize_committed_storage = original_finalize
        barrier_fresh = barrier_writer.reconcile(barrier_fixture.projection)

        busy_fixture = _create_empty_cutover_install_fixture(root / "busy.sqlite3")
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
            busy = _make_cutover_install_writer(busy_fixture).install(
                busy_fixture.projection
            )
        finally:
            if blocker.in_transaction:
                blocker.rollback()
            blocker.close()
        connection = None
        try:
            connection, _filesystem = busy_fixture.database._open_unvalidated()
            evidence = busy_fixture.coverage_plan.expected_evidence
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO transaction_receipts(transaction_id,kind,committed_at) "
                "VALUES (?,?,?)",
                (
                    evidence.transaction_id.encode("utf-8"),
                    evidence.receipt_kind.encode("utf-8"),
                    busy_fixture.source_committed_at,
                ),
            )
            connection.commit()
        finally:
            if connection is not None:
                if connection.in_transaction:
                    connection.rollback()
                connection.close()
        blob_reserved = _make_cutover_install_writer(busy_fixture).reconcile(
            busy_fixture.projection
        )

        legacy_blob_fixture = _create_empty_cutover_install_fixture(
            root / "legacy-blob.sqlite3"
        )
        connection = None
        try:
            connection, _filesystem = legacy_blob_fixture.database._open_unvalidated()
            proof = legacy_blob_fixture.plan.legacy_proof
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO transaction_receipts(transaction_id,kind,committed_at) "
                "VALUES (?,?,?)",
                (
                    proof.transaction_id.encode("utf-8"),
                    legacy_blob_fixture.projection.closure.source_receipt_kind.encode(
                        "utf-8"
                    ),
                    legacy_blob_fixture.source_committed_at,
                ),
            )
            connection.commit()
        finally:
            if connection is not None:
                if connection.in_transaction:
                    connection.rollback()
                connection.close()
        blob_legacy = _make_cutover_install_writer(legacy_blob_fixture).reconcile(
            legacy_blob_fixture.projection
        )

        cleanup_fixture = _create_empty_cutover_install_fixture(
            root / "cleanup-uncertain.sqlite3"
        )
        original_rollback_close = writer_module._rollback_close
        cleanup_calls = 0

        def incomplete_second_cleanup(connection) -> bool:
            nonlocal cleanup_calls
            cleanup_calls += 1
            complete = original_rollback_close(connection)
            return False if cleanup_calls == 2 else complete

        writer_module._rollback_close = incomplete_second_cleanup
        try:
            cleanup_uncertain = _make_cutover_install_writer(cleanup_fixture).install(
                cleanup_fixture.projection, _test_hook=fail_before
            )
        finally:
            writer_module._rollback_close = original_rollback_close
        if cleanup_calls != 3:
            raise AssertionError("cleanup uncertainty probe missed its fresh read")

        commit_uncertain_fixture = _create_empty_cutover_install_fixture(
            root / "commit-uncertain.sqlite3"
        )
        original_commit = _OwnedConnection.commit
        original_close_quietly = writer_module._close_quietly

        def fail_commit(self) -> None:
            del self
            raise sqlite3.OperationalError("audit-commit-uncertainty")

        def incomplete_close(connection) -> bool:
            original_close_quietly(connection)
            return False

        _OwnedConnection.commit = fail_commit
        writer_module._close_quietly = incomplete_close
        try:
            commit_uncertain = _make_cutover_install_writer(
                commit_uncertain_fixture
            ).install(commit_uncertain_fixture.projection)
        finally:
            _OwnedConnection.commit = original_commit
            writer_module._close_quietly = original_close_quietly

        stable_kernel_fixture = _create_empty_cutover_install_fixture(
            root / "stable-kernel.sqlite3"
        )
        original_runtime_validation = (
            writer_module._validate_business_runtime_connection
        )

        def stable_runtime_failure(database, connection, *, access) -> None:
            del database, connection, access
            raise SQLiteKernelError(
                "sqlite_business_runtime_control_invalid",
                "audit-stable-runtime-failure",
            )

        writer_module._validate_business_runtime_connection = stable_runtime_failure
        try:
            stable_kernel = _make_cutover_install_writer(stable_kernel_fixture).install(
                stable_kernel_fixture.projection
            )
        finally:
            writer_module._validate_business_runtime_connection = (
                original_runtime_validation
            )

        process_fixture = _create_empty_cutover_install_fixture(
            root / "process-control.sqlite3"
        )
        process_control_preserved = False

        def cleanup_interrupt(connection) -> bool:
            del connection
            raise KeyboardInterrupt()

        def process_interrupt(point: SQLiteCutoverInstallTestPoint) -> None:
            if point is SQLiteCutoverInstallTestPoint.BEFORE_COMMIT:
                writer_module._rollback_close = cleanup_interrupt
                raise SystemExit(37)

        try:
            try:
                _make_cutover_install_writer(process_fixture).install(
                    process_fixture.projection,
                    _test_hook=process_interrupt,
                )
            except BaseException as exc:
                process_control_preserved = (
                    isinstance(exc, SystemExit) and exc.code == 37
                )
        finally:
            writer_module._rollback_close = original_rollback_close

        same_fixture = _create_empty_cutover_install_fixture(root / "same-race.sqlite3")
        same_statuses, same_recovered = _race_install(
            same_fixture,
            same_fixture.projection,
            same_fixture.projection,
        )

        different_fixture = _create_empty_cutover_install_fixture(
            root / "different-race.sqlite3"
        )
        different_statuses, different_recovered = _race_install(
            different_fixture,
            different_fixture.projection,
            _different_projection(different_fixture),
        )

        return CutoverInstallWriterEvidence(
            authorizer_rejected_terminal_shadow_insert=authorizer_rejected,
            fixed_sql_rejected_terminal_shadow_insert=fixed_sql_rejected,
            initial_status=initial.status.value,
            rollback_status=rolled_back.status.value,
            rollback_code=rolled_back.code,
            rollback_state=rollback_state,
            committed_status=committed.status.value,
            committed_code=committed.code,
            committed_state=committed_state,
            semantic_readback_calls=readback_calls,
            retry_status=retried.status.value,
            retry_recovered=retried.recovered_after_ack_loss,
            ack_status=ack.status.value,
            ack_recovered=ack.recovered_after_ack_loss,
            ack_state=ack_state,
            barrier_status=barrier.status.value,
            barrier_code=barrier.code,
            barrier_fresh_status=barrier_fresh.status.value,
            busy_status=busy.status.value,
            busy_code=busy.code,
            blob_reserved_status=blob_reserved.status.value,
            blob_reserved_code=blob_reserved.code,
            blob_legacy_status=blob_legacy.status.value,
            blob_legacy_code=blob_legacy.code,
            cleanup_uncertain_status=cleanup_uncertain.status.value,
            cleanup_uncertain_code=cleanup_uncertain.code,
            commit_uncertain_status=commit_uncertain.status.value,
            commit_uncertain_code=commit_uncertain.code,
            stable_kernel_status=stable_kernel.status.value,
            stable_kernel_code=stable_kernel.code,
            process_control_preserved=process_control_preserved,
            same_projection_race_statuses=same_statuses,
            same_projection_race_recovered_count=same_recovered,
            different_projection_race_statuses=different_statuses,
            different_projection_race_recovered_count=different_recovered,
        )


__all__ = ()
