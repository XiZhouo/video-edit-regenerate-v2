"""Adversarial evidence for the private SDK-to-SQLite shadow bridge."""

from __future__ import annotations

import hashlib
import inspect
import sys
from pathlib import Path
from typing import Any

import pytest

import sdk
import sdk._legacy_import_sqlite_adapter as adapter
import sdk._legacy_import_state as state_module
import sdk.legacy_import as legacy_module
from business_snapshot import capture_business_snapshot
from sdk import Document, Entity
from sdk.legacy_import import LegacyImportError, prepare_legacy_import
import sqlite_kernel as kernel
from sqlite_kernel import FilesystemInfo, SqliteDatabase


CARD_SCHEMA = {
    "type": "object",
    "properties": {"title": {"type": "string", "required": True}},
}


def _local_probe(path: Path) -> FilesystemInfo:
    return FilesystemInfo("test-local", str(path), True)


def _database(tmp_path: Path) -> SqliteDatabase:
    return SqliteDatabase(
        tmp_path.resolve() / "private" / "repository.sqlite3",
        _test_filesystem_probe=_local_probe,
    )


def _prepared(import_id: str, project_id: str, title: str):
    document = Document(
        id="document-" + project_id,
        schema_version="1",
        collaboration_mode="live_collab",
        save_policy="auto_commit",
        schema={"card": CARD_SCHEMA},
    )
    document.add(Entity(type="card", id="row", data={"title": title}))
    document._created_at = 10.0
    document._updated_at = 20.0
    document._v = 7
    entity = document.get("card", "row")
    entity._created_at = 11.0
    entity._updated_at = 19.0
    entity._version = 3
    snapshot = capture_business_snapshot(project_id, 1, document).snapshot
    source = ("source-head:" + project_id + ":" + title).encode("utf-8")
    handle = state_module._issue_scan_handle(
        snapshot,
        source_head_sha256=hashlib.sha256(source).digest(),
        source_head_bytes=len(source),
        storage_version="v2",
    )
    return prepare_legacy_import(import_id, (handle,))


def _import(database: SqliteDatabase, package: Any, receipt: Any):
    return adapter._import_prepared_legacy_sqlite_shadow(
        database,
        package,
        receipt,
        _attempt_token=object(),
        _capability=adapter._LEGACY_SQLITE_SHADOW_ADAPTER_CAPABILITY,
    )


def test_private_bridge_imports_exact_shadow_and_preserves_legacy_authority(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    package, receipt = _prepared("1" * 64, "project-a", "alpha")

    result = _import(database, package, receipt)

    assert result.import_id == "1" * 64
    assert result.transaction_id == "legacy-import:" + "1" * 64
    assert not result.already_imported
    assert not result.recovered_after_ack_loss
    connection, _filesystem = database._open_unvalidated()
    try:
        assert connection.execute(
            "SELECT authority FROM kernel_repository WHERE singleton = 1"
        ).fetchone()[0] == "legacy"
        assert connection.execute("SELECT count(*) FROM projects").fetchone()[0] == 1
        assert connection.execute(
            "SELECT value FROM kernel_metadata WHERE key = 'legacy_import_state'"
        ).fetchone()[0] == "terminal_shadow_v1"
    finally:
        connection.close()


def test_new_process_lane_exact_retry_keeps_original_commit_time(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    first_package, first_receipt = _prepared("2" * 64, "project-a", "alpha")
    first = _import(database, first_package, first_receipt)

    retry_package, retry_receipt = _prepared("2" * 64, "project-a", "alpha")
    retry = _import(database, retry_package, retry_receipt)

    assert retry.already_imported
    assert not retry.recovered_after_ack_loss
    assert retry.receipt.committed_at == first.receipt.committed_at


def test_same_id_changed_payload_is_quarantined_after_fresh_conflict(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    first_package, first_receipt = _prepared("3" * 64, "project-a", "alpha")
    _import(database, first_package, first_receipt)
    changed_package, changed_receipt = _prepared(
        "3" * 64, "project-a", "changed"
    )

    with pytest.raises(adapter.LegacySqliteShadowAdapterError) as conflict:
        _import(database, changed_package, changed_receipt)
    assert conflict.value.status == "conflict"

    with pytest.raises(LegacyImportError) as quarantined:
        legacy_module._reserve_prepared_legacy_import(
            changed_package,
            changed_receipt,
            _attempt_token=object(),
            _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
        )
    assert quarantined.value.reason == "adapter_lane_unavailable"


def test_not_imported_fresh_state_releases_lane_for_safe_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    package, receipt = _prepared("4" * 64, "project-a", "alpha")
    original_import = adapter.SqliteLegacyImporter.import_plan

    def fail_before_write(_self: Any, _plan: Any) -> None:
        raise RuntimeError("before write")

    monkeypatch.setattr(adapter.SqliteLegacyImporter, "import_plan", fail_before_write)
    with pytest.raises(adapter.LegacySqliteShadowAdapterError) as missing:
        _import(database, package, receipt)
    assert missing.value.status == "not_imported"

    monkeypatch.setattr(adapter.SqliteLegacyImporter, "import_plan", original_import)
    assert _import(database, package, receipt).import_id == "4" * 64


def test_reserve_return_edge_is_recovered_with_exact_attempt_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    package, receipt = _prepared("5" * 64, "project-a", "alpha")
    original_reserve = adapter._reserve_prepared_legacy_import
    calls = 0

    def reserve_then_lose_ack(*args: Any, **kwargs: Any):
        nonlocal calls
        calls += 1
        original_reserve(*args, **kwargs)
        raise RuntimeError("reserve acknowledgement lost")

    monkeypatch.setattr(
        adapter, "_reserve_prepared_legacy_import", reserve_then_lose_ack
    )
    result = _import(database, package, receipt)
    assert calls == 1
    assert result.import_id == "5" * 64


def test_reserve_async_interrupt_releases_exact_lane_and_preserves_cancel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    package, receipt = _prepared("e" * 64, "project-a", "alpha")
    original_reserve = adapter._reserve_prepared_legacy_import

    def reserve_then_interrupt(*args: Any, **kwargs: Any):
        original_reserve(*args, **kwargs)
        raise KeyboardInterrupt()

    monkeypatch.setattr(adapter, "_reserve_prepared_legacy_import", reserve_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        _import(database, package, receipt)
    connection, _filesystem = database._open_unvalidated()
    try:
        assert connection.execute("SELECT count(*) FROM projects").fetchone()[0] == 0
    finally:
        connection.close()
    # A successful retry of the same package proves exact recovery released
    # the active lane instead of swallowing cancellation or leaving it stuck.
    monkeypatch.setattr(adapter, "_reserve_prepared_legacy_import", original_reserve)
    assert _import(database, package, receipt).import_id == "e" * 64


@pytest.mark.parametrize("interrupt_after_recover", (False, True))
def test_recovery_interrupt_is_not_hidden_and_exact_lane_is_released(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interrupt_after_recover: bool,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    package, receipt = _prepared("a" * 64, "project-a", "alpha")
    original_reserve = adapter._reserve_prepared_legacy_import
    original_recover = adapter._recover_prepared_legacy_import
    recovery_calls = 0

    def reserve_then_lose_ack(*args: Any, **kwargs: Any):
        original_reserve(*args, **kwargs)
        raise RuntimeError("reserve acknowledgement lost")

    def interrupt_first_recovery(*args: Any, **kwargs: Any):
        nonlocal recovery_calls
        recovery_calls += 1
        if recovery_calls == 1:
            if interrupt_after_recover:
                original_recover(*args, **kwargs)
            raise KeyboardInterrupt("recovery interrupted")
        return original_recover(*args, **kwargs)

    monkeypatch.setattr(
        adapter, "_reserve_prepared_legacy_import", reserve_then_lose_ack,
    )
    monkeypatch.setattr(
        adapter, "_recover_prepared_legacy_import", interrupt_first_recovery,
    )
    with pytest.raises(KeyboardInterrupt, match="recovery interrupted"):
        _import(database, package, receipt)
    assert recovery_calls == 2

    # The successful exact-token retry provided the handle needed to release
    # the lane before cancellation escaped.  A later ordinary attempt can use
    # this same prepared package instead of seeing adapter_lane_unavailable.
    monkeypatch.setattr(
        adapter, "_reserve_prepared_legacy_import", original_reserve,
    )
    monkeypatch.setattr(
        adapter, "_recover_prepared_legacy_import", original_recover,
    )
    assert _import(database, package, receipt).import_id == "a" * 64


def test_second_recovery_process_control_is_never_replaced_by_reserve_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    package, receipt = _prepared("c" * 64, "project-a", "alpha")
    original_reserve = adapter._reserve_prepared_legacy_import
    recovery_calls = 0

    def reserve_then_lose_ack(*args: Any, **kwargs: Any):
        original_reserve(*args, **kwargs)
        raise RuntimeError("reserve acknowledgement lost")

    def interrupt_both_recoveries(*_args: Any, **_kwargs: Any):
        nonlocal recovery_calls
        recovery_calls += 1
        if recovery_calls == 1:
            raise KeyboardInterrupt("first recovery interrupted")
        raise SystemExit("second recovery interrupted")

    monkeypatch.setattr(
        adapter, "_reserve_prepared_legacy_import", reserve_then_lose_ack,
    )
    monkeypatch.setattr(
        adapter, "_recover_prepared_legacy_import", interrupt_both_recoveries,
    )
    with pytest.raises(SystemExit, match="second recovery interrupted"):
        _import(database, package, receipt)
    assert recovery_calls == 2


def test_finish_ack_loss_retries_same_terminal_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    package, receipt = _prepared("6" * 64, "project-a", "alpha")
    original_finish = adapter._finish_prepared_legacy_import
    calls = 0

    def finish_then_lose_ack(*args: Any, **kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        original_finish(*args, **kwargs)
        if calls == 1:
            raise RuntimeError("finish acknowledgement lost")

    monkeypatch.setattr(adapter, "_finish_prepared_legacy_import", finish_then_lose_ack)
    result = _import(database, package, receipt)
    assert calls == 2
    assert result.import_id == "6" * 64


def test_finish_async_interrupt_keeps_terminal_lane_but_propagates_cancel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    package, receipt = _prepared("f" * 64, "project-a", "alpha")
    original_finish = adapter._finish_prepared_legacy_import
    calls = 0

    def finish_then_interrupt(*args: Any, **kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        original_finish(*args, **kwargs)
        if calls == 1:
            raise KeyboardInterrupt()

    monkeypatch.setattr(adapter, "_finish_prepared_legacy_import", finish_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        _import(database, package, receipt)
    assert calls == 2

    retry_package, retry_receipt = _prepared("f" * 64, "project-a", "alpha")
    monkeypatch.setattr(adapter, "_finish_prepared_legacy_import", original_finish)
    assert _import(database, retry_package, retry_receipt).already_imported


def test_database_commit_ack_loss_returns_only_after_fresh_reconcile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    package, receipt = _prepared("9" * 64, "project-a", "alpha")
    original_commit = kernel._OwnedConnection.commit

    def commit_then_lose_ack(connection: Any) -> None:
        original_commit(connection)
        raise RuntimeError("database commit acknowledgement lost")

    monkeypatch.setattr(kernel._OwnedConnection, "commit", commit_then_lose_ack)
    result = _import(database, package, receipt)
    assert result.recovered_after_ack_loss
    assert not result.already_imported


def test_database_commit_async_interrupt_consumes_lane_then_propagates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    package, receipt = _prepared("b" * 64, "project-a", "alpha")
    original_commit = kernel._OwnedConnection.commit

    def commit_then_interrupt(connection: Any) -> None:
        original_commit(connection)
        raise KeyboardInterrupt()

    monkeypatch.setattr(kernel._OwnedConnection, "commit", commit_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        _import(database, package, receipt)

    retry_package, retry_receipt = _prepared("b" * 64, "project-a", "alpha")
    monkeypatch.setattr(kernel._OwnedConnection, "commit", original_commit)
    assert _import(database, retry_package, retry_receipt).already_imported


def test_async_interrupt_after_import_return_quarantines_uncertain_lane(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    package, receipt = _prepared("d" * 64, "project-a", "alpha")
    source_lines, first_line = inspect.getsourcelines(
        adapter._import_prepared_legacy_sqlite_shadow
    )
    reconcile_try_offset = next(
        index
        for index, line in enumerate(source_lines)
        if line.strip() == "try:"
        and index + 1 < len(source_lines)
        and "reconciled = importer.reconcile(plan)" in source_lines[index + 1]
    )
    interrupt_line = first_line + reconcile_try_offset
    interrupted = False

    def trace(frame: Any, event: str, _argument: Any):
        nonlocal interrupted
        if (
            not interrupted
            and event == "line"
            and frame.f_code is adapter._import_prepared_legacy_sqlite_shadow.__code__
            and frame.f_lineno == interrupt_line
        ):
            interrupted = True
            raise KeyboardInterrupt()
        return trace

    previous_trace = sys.gettrace()
    sys.settrace(trace)
    try:
        with pytest.raises(KeyboardInterrupt):
            _import(database, package, receipt)
    finally:
        sys.settrace(previous_trace)
    assert interrupted

    with pytest.raises(LegacyImportError) as quarantined:
        legacy_module._reserve_prepared_legacy_import(
            package,
            receipt,
            _attempt_token=object(),
            _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
        )
    assert quarantined.value.reason == "adapter_lane_unavailable"


def test_durability_barrier_failure_quarantines_committed_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    package, receipt = _prepared("c" * 64, "project-a", "alpha")

    def fail_barrier(_database: Any) -> None:
        raise kernel.SQLiteKernelError(
            "sqlite_file_stat_failed", "simulated durability barrier failure"
        )

    monkeypatch.setattr(
        kernel.SqliteDatabase, "_finalize_committed_storage", fail_barrier
    )
    with pytest.raises(adapter.LegacySqliteShadowAdapterError) as unknown:
        _import(database, package, receipt)
    assert unknown.value.status == "unknown"

    with pytest.raises(LegacyImportError) as quarantined:
        legacy_module._reserve_prepared_legacy_import(
            package,
            receipt,
            _attempt_token=object(),
            _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
        )
    assert quarantined.value.reason == "adapter_lane_unavailable"


def test_tampered_committed_shadow_is_quarantined_as_corrupt(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    package, receipt = _prepared("a" * 64, "project-a", "alpha")
    _import(database, package, receipt)
    connection, _filesystem = database._open_unvalidated()
    try:
        row = connection.execute(
            "SELECT snapshot_bytes FROM entities WHERE project_id = ?",
            ("project-a",),
        ).fetchone()
        changed = bytes((row[0][0] ^ 1,)) + row[0][1:]
        connection.execute(
            "UPDATE entities SET snapshot_bytes = ? WHERE project_id = ?",
            (changed, "project-a"),
        )
        connection.commit()
    finally:
        connection.close()

    retry_package, retry_receipt = _prepared("a" * 64, "project-a", "alpha")
    with pytest.raises(adapter.LegacySqliteShadowAdapterError) as corrupt:
        _import(database, retry_package, retry_receipt)
    assert corrupt.value.status == "corrupt"


def test_reconcile_async_interrupt_quarantines_lane_and_preserves_cancel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    package, receipt = _prepared("0" * 64, "project-a", "alpha")
    original_reconcile = adapter.SqliteLegacyImporter.reconcile

    def interrupt_reconcile(_importer: Any, _plan: Any):
        raise KeyboardInterrupt()

    monkeypatch.setattr(adapter.SqliteLegacyImporter, "reconcile", interrupt_reconcile)
    with pytest.raises(KeyboardInterrupt):
        _import(database, package, receipt)

    # import_plan already committed, but an interrupted fresh classification
    # cannot consume this process lane.  A newly prepared exact pair can prove
    # the durable target and complete normally.
    retry_package, retry_receipt = _prepared("0" * 64, "project-a", "alpha")
    monkeypatch.setattr(adapter.SqliteLegacyImporter, "reconcile", original_reconcile)
    assert _import(database, retry_package, retry_receipt).already_imported


def test_post_write_prepared_binding_drift_quarantines_committed_shadow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    package, receipt = _prepared("7" * 64, "project-a", "alpha")
    record = state_module._LEDGER.prepared_package(package)
    original_import = adapter.SqliteLegacyImporter.import_plan

    def import_then_drift(importer: Any, plan: Any):
        result = original_import(importer, plan)
        record.manifest_bytes += b" "
        return result

    monkeypatch.setattr(adapter.SqliteLegacyImporter, "import_plan", import_then_drift)
    with pytest.raises(LegacyImportError) as changed:
        _import(database, package, receipt)
    assert changed.value.reason == "prepared_record_changed"

    fresh_package, fresh_receipt = _prepared("7" * 64, "project-a", "alpha")
    monkeypatch.setattr(adapter.SqliteLegacyImporter, "import_plan", original_import)
    assert _import(database, fresh_package, fresh_receipt).already_imported


def test_capability_public_surface_and_runtime_mount_remain_closed(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    database.initialize()
    package, receipt = _prepared("8" * 64, "project-a", "alpha")
    with pytest.raises(adapter.LegacySqliteShadowAdapterError) as forbidden:
        adapter._import_prepared_legacy_sqlite_shadow(
            database,
            package,
            receipt,
            _attempt_token=object(),
            _capability=object(),
        )
    assert forbidden.value.status == "forbidden"
    assert not set(adapter.__dict__).intersection(sdk.__all__)
    assert "_import_prepared_legacy_sqlite_shadow" not in sdk.__all__
