"""Adversarial evidence for the private strict-catalog SQLite coordinator."""

from __future__ import annotations

import hashlib
import inspect
from pathlib import Path
from typing import Any

import pytest

import sdk
import sdk._legacy_import_sqlite_adapter as adapter
import sdk._legacy_import_state as state_module
import sdk.legacy_import as legacy_module
from business_snapshot import capture_business_snapshot
from sdk import Document, Entity
from sdk.project_topology import ProjectTopologyCoordinator
from sqlite_kernel import FilesystemInfo, SqliteDatabase


CARD_SCHEMA = {
    "type": "object",
    "properties": {"title": {"type": "string", "required": True}},
}


def _local_probe(path: Path) -> FilesystemInfo:
    return FilesystemInfo("test-local", str(path), True)


def _database(tmp_path: Path) -> SqliteDatabase:
    database = SqliteDatabase(
        tmp_path.resolve() / "private" / "repository.sqlite3",
        _test_filesystem_probe=_local_probe,
    )
    database.initialize()
    return database


def _document(project_id: str, title: str) -> Document:
    document = Document(
        id=project_id,
        schema_version="1",
        collaboration_mode="live_collab",
        save_policy="auto_commit",
        schema={"card": CARD_SCHEMA},
    )
    document.add(Entity(type="card", id="row", data={"title": title}))
    return document


def _topology(
    tmp_path: Path,
    projects: tuple[tuple[str, str], ...] = (),
) -> ProjectTopologyCoordinator:
    topology = ProjectTopologyCoordinator(
        tmp_path.resolve() / "legacy" / "projects", lock_timeout=2.0,
    )
    topology.initialize(validate=lambda _root: None)
    for project_id, title in projects:
        def build(
            root: Path,
            *,
            _project_id: str = project_id,
            _title: str = title,
        ) -> None:
            _document(_project_id, _title).save(Path(root) / "document.json")

        topology.create(
            project_id,
            build=build,
            validate=lambda _root: None,
        )
    return topology


def _seal(
    topology: ProjectTopologyCoordinator,
    database: SqliteDatabase,
) -> adapter.LegacyCatalogShadowSealResult:
    return adapter._seal_strict_legacy_catalog_sqlite_shadow(
        topology,
        database,
        _capability=adapter._LEGACY_CATALOG_SQLITE_ADAPTER_CAPABILITY,
    )


def _prepared(import_id: str, project_id: str = "project-a"):
    document = _document(project_id, "alpha")
    document._created_at = 10.0
    document._updated_at = 20.0
    document._v = 7
    entity = document.get("card", "row")
    entity._created_at = 11.0
    entity._updated_at = 19.0
    entity._version = 3
    snapshot = capture_business_snapshot(project_id, 1, document).snapshot
    source = ("source-head:" + project_id).encode("utf-8")
    handle = state_module._issue_scan_handle(
        snapshot,
        source_head_sha256=hashlib.sha256(source).digest(),
        source_head_bytes=len(source),
        storage_version="v2",
    )
    return legacy_module.prepare_legacy_import(import_id, (handle,))


def _import_prepared(database: SqliteDatabase, package: Any, receipt: Any):
    return adapter._import_prepared_legacy_sqlite_shadow(
        database,
        package,
        receipt,
        _attempt_token=object(),
        _capability=adapter._LEGACY_SQLITE_SHADOW_ADAPTER_CAPABILITY,
    )


def _reserve_fails(package: Any, receipt: Any) -> str:
    with pytest.raises(legacy_module.LegacyImportError) as caught:
        legacy_module._reserve_prepared_legacy_import(
            package,
            receipt,
            _attempt_token=object(),
            _capability=legacy_module._LEGACY_IMPORT_ADAPTER_CAPABILITY,
        )
    return caught.value.reason


def test_complete_catalog_scans_twice_with_fresh_handles_in_canonical_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    topology = _topology(tmp_path, (("project-b", "beta"), ("project-a", "alpha")))
    database = _database(tmp_path)
    events: list[str] = []
    scans: list[Any] = []
    descriptors: list[Any] = []
    original_scan = adapter.scan_legacy_project
    original_import = adapter._import_prepared_legacy_sqlite_shadow
    original_seal = adapter.SqliteLegacyCatalogSealer.seal

    def scan(*args: Any, **kwargs: Any):
        handle = original_scan(*args, **kwargs)
        events.append("scan:" + handle.project_id)
        scans.append(handle)
        descriptors.append(adapter._scan_descriptor(handle))
        return handle

    def import_shadow(*args: Any, **kwargs: Any):
        events.append("import")
        return original_import(*args, **kwargs)

    def seal_catalog(sealer: Any, plan: Any):
        events.append("seal")
        return original_seal(sealer, plan)

    monkeypatch.setattr(adapter, "scan_legacy_project", scan)
    monkeypatch.setattr(
        adapter, "_import_prepared_legacy_sqlite_shadow", import_shadow,
    )
    monkeypatch.setattr(adapter.SqliteLegacyCatalogSealer, "seal", seal_catalog)

    result = _seal(topology, database)

    assert events == [
        "scan:project-a", "scan:project-b", "import",
        "scan:project-a", "scan:project-b", "seal",
    ]
    assert len(scans) == 4
    assert scans[0] is not scans[2]
    assert scans[1] is not scans[3]
    assert tuple(descriptors[:2]) == tuple(descriptors[2:])
    assert result.import_id == adapter.derive_legacy_catalog_import_id(
        tuple(descriptors[:2])
    )
    assert result.live_project_count == 2
    assert result.authority == "legacy"
    assert result.historical_fenced
    assert not result.cutover_ready
    assert not result.already_imported
    assert not result.already_sealed


def test_empty_catalog_uses_no_scan_prepare_or_import_and_retries_exactly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    topology = _topology(tmp_path)
    database = _database(tmp_path)

    def forbidden(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("empty catalog entered a non-empty lane")

    monkeypatch.setattr(adapter, "scan_legacy_project", forbidden)
    monkeypatch.setattr(adapter, "prepare_legacy_import", forbidden)
    monkeypatch.setattr(
        adapter, "_import_prepared_legacy_sqlite_shadow", forbidden,
    )

    first = _seal(topology, database)
    retry = _seal(topology, database)

    assert first.import_id is None
    assert first.import_receipt is None
    assert first.live_project_count == 0
    assert not first.already_sealed
    assert retry.already_sealed
    assert retry.catalog_id == first.catalog_id
    assert retry.catalog_receipt.committed_at == first.catalog_receipt.committed_at
    assert retry.authority == "legacy"
    assert retry.historical_fenced and not retry.cutover_ready


def test_nonempty_exact_retry_preserves_both_receipts_and_stable_identity(
    tmp_path: Path,
) -> None:
    topology = _topology(tmp_path, (("project-a", "alpha"),))
    database = _database(tmp_path)

    first = _seal(topology, database)
    retry = _seal(topology, database)

    assert retry.import_id == first.import_id
    assert retry.catalog_id == first.catalog_id
    assert retry.catalog_transaction_id == first.catalog_transaction_id
    assert retry.already_imported
    assert retry.already_sealed
    assert retry.import_receipt is not None
    assert first.import_receipt is not None
    assert retry.import_receipt.committed_at == first.import_receipt.committed_at
    assert retry.catalog_receipt.committed_at == first.catalog_receipt.committed_at


def test_second_scan_descriptor_drift_never_writes_catalog_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    topology = _topology(tmp_path, (("project-a", "alpha"),))
    database = _database(tmp_path)
    original_scan = adapter.scan_legacy_project
    calls = 0

    def drift_on_second_scan(*args: Any, **kwargs: Any):
        nonlocal calls
        calls += 1
        handle = original_scan(*args, **kwargs)
        if calls == 2:
            record = state_module._LEDGER.scan(handle)
            record.source_head_sha256 = b"\xff" * 32
        return handle

    monkeypatch.setattr(adapter, "scan_legacy_project", drift_on_second_scan)
    with pytest.raises(adapter.LegacyCatalogSqliteAdapterError) as caught:
        _seal(topology, database)
    assert caught.value.status == "conflict"
    assert caught.value.detail == "second_scan_descriptor_mismatch"
    assert calls == 2

    connection, _filesystem = database._open_unvalidated()
    try:
        assert connection.execute(
            "SELECT count(*) FROM legacy_catalog_seals"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT count(*) FROM transaction_receipts "
            "WHERE kind = '_legacy_catalog_seal_v1'"
        ).fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM projects").fetchone()[0] == 1
    finally:
        connection.close()


def test_catalog_prewrite_failure_and_async_interrupt_release_exact_lane(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    import_id = "1" * 64
    import_package, import_receipt = _prepared(import_id)
    _import_prepared(database, import_package, import_receipt)
    package, receipt = _prepared(import_id)
    original_seal = adapter.SqliteLegacyCatalogSealer.seal

    def fail_before_write(_self: Any, _plan: Any) -> None:
        raise RuntimeError("before write")

    monkeypatch.setattr(adapter.SqliteLegacyCatalogSealer, "seal", fail_before_write)
    with pytest.raises(adapter.LegacyCatalogSqliteAdapterError) as missing:
        adapter._seal_prepared_legacy_catalog(
            database, package, receipt, _attempt_token=object(),
            _capability=adapter._LEGACY_CATALOG_SQLITE_ADAPTER_CAPABILITY,
        )
    assert missing.value.status == "not_sealed"

    def interrupt_before_write(_self: Any, _plan: Any) -> None:
        raise KeyboardInterrupt()

    monkeypatch.setattr(
        adapter.SqliteLegacyCatalogSealer, "seal", interrupt_before_write,
    )
    with pytest.raises(KeyboardInterrupt):
        adapter._seal_prepared_legacy_catalog(
            database, package, receipt, _attempt_token=object(),
            _capability=adapter._LEGACY_CATALOG_SQLITE_ADAPTER_CAPABILITY,
        )

    monkeypatch.setattr(adapter.SqliteLegacyCatalogSealer, "seal", original_seal)
    assert adapter._seal_prepared_legacy_catalog(
        database, package, receipt, _attempt_token=object(),
        _capability=adapter._LEGACY_CATALOG_SQLITE_ADAPTER_CAPABILITY,
    ).catalog_id


def test_catalog_write_ack_loss_consumes_lane_only_after_fresh_sealed_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    import_id = "2" * 64
    import_package, import_receipt = _prepared(import_id)
    _import_prepared(database, import_package, import_receipt)
    package, receipt = _prepared(import_id)
    original_seal = adapter.SqliteLegacyCatalogSealer.seal

    def seal_then_lose_ack(sealer: Any, plan: Any):
        original_seal(sealer, plan)
        raise RuntimeError("seal acknowledgement lost")

    monkeypatch.setattr(
        adapter.SqliteLegacyCatalogSealer, "seal", seal_then_lose_ack,
    )
    outcome = adapter._seal_prepared_legacy_catalog(
        database, package, receipt, _attempt_token=object(),
        _capability=adapter._LEGACY_CATALOG_SQLITE_ADAPTER_CAPABILITY,
    )
    assert outcome.recovered_after_ack_loss
    assert _reserve_fails(package, receipt) == "adapter_lane_unavailable"


def test_catalog_postwrite_async_consumes_lane_and_preserves_cancel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    import_id = "3" * 64
    import_package, import_receipt = _prepared(import_id)
    _import_prepared(database, import_package, import_receipt)
    package, receipt = _prepared(import_id)
    original_seal = adapter.SqliteLegacyCatalogSealer.seal

    def seal_then_interrupt(sealer: Any, plan: Any):
        original_seal(sealer, plan)
        raise KeyboardInterrupt()

    monkeypatch.setattr(
        adapter.SqliteLegacyCatalogSealer, "seal", seal_then_interrupt,
    )
    with pytest.raises(KeyboardInterrupt):
        adapter._seal_prepared_legacy_catalog(
            database, package, receipt, _attempt_token=object(),
            _capability=adapter._LEGACY_CATALOG_SQLITE_ADAPTER_CAPABILITY,
        )
    assert _reserve_fails(package, receipt) == "adapter_lane_unavailable"


def test_unknown_fresh_classification_quarantines_second_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    import_id = "4" * 64
    import_package, import_receipt = _prepared(import_id)
    _import_prepared(database, import_package, import_receipt)
    package, receipt = _prepared(import_id)

    def fail_seal(_self: Any, _plan: Any) -> None:
        raise RuntimeError("before write")

    def fail_reconcile(_self: Any, _plan: Any) -> None:
        raise RuntimeError("fresh state unavailable")

    monkeypatch.setattr(adapter.SqliteLegacyCatalogSealer, "seal", fail_seal)
    monkeypatch.setattr(
        adapter.SqliteLegacyCatalogSealer, "reconcile", fail_reconcile,
    )
    with pytest.raises(adapter.LegacyCatalogSqliteAdapterError) as unknown:
        adapter._seal_prepared_legacy_catalog(
            database, package, receipt, _attempt_token=object(),
            _capability=adapter._LEGACY_CATALOG_SQLITE_ADAPTER_CAPABILITY,
        )
    assert unknown.value.status == "unknown"
    assert _reserve_fails(package, receipt) == "adapter_lane_unavailable"


def test_private_result_and_capability_graph_do_not_leak_authority(
    tmp_path: Path,
) -> None:
    topology = _topology(tmp_path)
    database = _database(tmp_path)
    with pytest.raises(adapter.LegacyCatalogSqliteAdapterError) as forbidden:
        adapter._seal_strict_legacy_catalog_sqlite_shadow(
            topology, database, _capability=object(),
        )
    assert forbidden.value.status == "forbidden"
    with pytest.raises(adapter.LegacyCatalogSqliteAdapterError) as leaf_forbidden:
        adapter._catalog_sealer(database, _capability=object())
    assert leaf_forbidden.value.status == "forbidden"

    result = _seal(topology, database)
    for leaked in (
        "plan", "integrity", "capability", "package", "reservation",
        "manifest_bytes", "projects",
    ):
        assert not hasattr(result, leaked)
    assert result.authority == "legacy"
    assert result.historical_fenced and not result.cutover_ready
    assert "_seal_strict_legacy_catalog_sqlite_shadow" not in sdk.__all__
    assert not set(adapter.__dict__).intersection(sdk.__all__)

    adapter_source = inspect.getsource(adapter)
    assert adapter_source.count(
        "_capability=_LEGACY_CATALOG_SEAL_CAPABILITY"
    ) == 1
    sdk_root = Path(adapter.__file__).resolve().parent
    consumers = []
    for source_path in sdk_root.glob("*.py"):
        if source_path.name.startswith("test_"):
            continue
        if "_LEGACY_CATALOG_SEAL_CAPABILITY" in source_path.read_text(
            encoding="utf-8"
        ):
            consumers.append(source_path.name)
    assert consumers == ["_legacy_import_sqlite_adapter.py"]
