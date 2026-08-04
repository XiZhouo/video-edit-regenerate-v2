"""Adversarial evidence for the private exact repository SQLite coordinator."""

from __future__ import annotations

import hashlib
import inspect
import threading
from pathlib import Path
from typing import Any

import pytest

import sdk
import sdk._legacy_import_sqlite_adapter as catalog_adapter
import sdk._legacy_repository_sqlite_adapter as adapter
from sdk import Document, Entity
from sdk.project_topology import ProjectTopologyCoordinator
from sqlite_kernel import FilesystemInfo, SqliteDatabase


CARD_SCHEMA = {
    "type": "object",
    "properties": {"title": {"type": "string", "required": True}},
}
RUNTIME_GENERATION = "sdk-test-runtime-v1"
CODE_DIGEST = hashlib.sha256(b"sdk-test-code").digest()


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
    live: tuple[str, ...] = (),
    deleted: tuple[str, ...] = (),
) -> ProjectTopologyCoordinator:
    topology = ProjectTopologyCoordinator(
        tmp_path.resolve() / "legacy" / "projects", lock_timeout=2.0,
    )
    topology.initialize(validate=lambda _root: None)
    for project_id in live + deleted:
        def build(root: Path, *, _project_id: str = project_id) -> None:
            _document(_project_id, "title:" + _project_id).save(
                Path(root) / "document.json"
            )

        topology.create(
            project_id,
            build=build,
            validate=lambda _root: None,
            operation_id=hashlib.sha256(
                ("create:" + project_id).encode()
            ).hexdigest()[:32],
        )
    for project_id in deleted:
        topology.delete(
            project_id,
            expected_generation=1,
            operation_id=hashlib.sha256(
                ("delete:" + project_id).encode()
            ).hexdigest()[:32],
        )
    return topology


def _seal(
    topology: ProjectTopologyCoordinator,
    database: SqliteDatabase,
) -> adapter.LegacyRepositoryShadowSealResult:
    return adapter._seal_strict_legacy_repository_sqlite_shadow(
        topology,
        database,
        runtime_generation=RUNTIME_GENERATION,
        code_digest=CODE_DIGEST,
        _capability=adapter._LEGACY_REPOSITORY_SQLITE_ADAPTER_CAPABILITY,
    )


@pytest.mark.parametrize(
    ("live", "deleted"),
    (
        ((), ()),
        ((), ("old",)),
        (("current",), ()),
        (("current",), ("old",)),
    ),
)
def test_exact_repository_seals_empty_deleted_live_and_mixed_without_cutover(
    tmp_path: Path,
    live: tuple[str, ...],
    deleted: tuple[str, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    topology = _topology(tmp_path, live, deleted)
    database = _database(tmp_path)
    admission_before = topology._read_repository_admission()
    assert admission_before is not None
    monkeypatch.setattr(
        topology,
        "_compare_and_swap_repository_admission",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("repository proof must not change admission")
        ),
    )

    first = _seal(topology, database)
    monkeypatch.setattr(
        adapter,
        "_seal_strict_legacy_catalog_sqlite_shadow_locked",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("PRESENT v5 namespace re-entered old v4 writers")
        ),
    )
    second = _seal(topology, database)

    assert first.cutover_id == second.cutover_id
    assert first.receipt == second.receipt
    assert first.project_count == len(live) + len(deleted)
    assert first.live_project_count == len(live)
    assert first.deleted_project_count == len(deleted)
    assert first.authority == "legacy"
    assert first.cutover_ready is False
    assert first.mounted is False
    assert first.production_callers == 0
    assert not hasattr(first, "projection")
    assert not hasattr(first, "descriptors")
    assert second.already_sealed is True
    assert topology._read_repository_admission() == admission_before

    connection, _filesystem = database._open_unvalidated()
    try:
        rows = tuple(connection.execute(
            "SELECT project_id, status, source_state_bytes, catalog_ordinal "
            "FROM legacy_repository_members ORDER BY project_id"
        ))
    finally:
        connection.close()
    assert tuple(row[0] for row in rows) == tuple(sorted(live + deleted))
    by_id = {row[0]: (row[1], row[2], row[3]) for row in rows}
    for ordinal, project_id in enumerate(sorted(live)):
        status, source_bytes, catalog_ordinal = by_id[project_id]
        assert status == "live"
        assert source_bytes > 0
        assert catalog_ordinal == ordinal
    for project_id in deleted:
        assert by_id[project_id] == ("deleted", 0, None)
    assert all(
        row[2] > 0 if row[1] == "live" else row[2] == 0
        for row in rows
    )


def test_outer_repository_barrier_blocks_ordinary_users_until_s2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    topology = _topology(tmp_path, ("project",))
    database = _database(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []
    real_seal = adapter.SqliteLegacyRepositorySealer.seal

    def paused_seal(sealer: Any, projection: Any):
        result = real_seal(sealer, projection)
        entered.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(adapter.SqliteLegacyRepositorySealer, "seal", paused_seal)

    def worker() -> None:
        try:
            _seal(topology, database)
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    assert entered.wait(5)
    topology.lock_timeout = 0.15
    with pytest.raises(Exception) as blocked:
        topology.list_live_ids()
    assert getattr(blocked.value, "code", "") == "project_busy"
    release.set()
    thread.join(5)
    assert not thread.is_alive()
    assert errors == []


def test_present_namespace_projection_releases_both_active_v4_lanes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    topology = _topology(tmp_path, ("project",))
    database = _database(tmp_path)
    _seal(topology, database)
    outcomes: list[str] = []
    active: set[int] = set()
    peak_active = 0
    real_finish = catalog_adapter._finish_exact_outcome
    real_reserve = catalog_adapter._reserve_or_recover

    def record_reserve(*args: Any, **kwargs: Any):
        nonlocal peak_active
        reserved = real_reserve(*args, **kwargs)
        active.add(id(reserved[0]))
        peak_active = max(peak_active, len(active))
        return reserved

    def record_finish(reservation: Any, outcome: str) -> None:
        outcomes.append(outcome)
        try:
            real_finish(reservation, outcome)
        finally:
            active.discard(id(reservation))

    monkeypatch.setattr(catalog_adapter, "_reserve_or_recover", record_reserve)
    monkeypatch.setattr(catalog_adapter, "_finish_exact_outcome", record_finish)
    retried = _seal(topology, database)
    assert retried.already_sealed is True
    assert outcomes == ["release", "release"]
    assert peak_active == 1
    assert active == set()


def test_present_namespace_projection_cancellation_still_resolves_both_lanes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    topology = _topology(tmp_path, ("project",))
    database = _database(tmp_path)
    _seal(topology, database)
    outcomes: list[str] = []
    real_finish = catalog_adapter._finish_exact_outcome

    def record_finish(reservation: Any, outcome: str) -> None:
        outcomes.append(outcome)
        real_finish(reservation, outcome)

    def cancel_projection(_plan: Any):
        raise KeyboardInterrupt("injected active-plan cancellation")

    monkeypatch.setattr(catalog_adapter, "_finish_exact_outcome", record_finish)
    monkeypatch.setattr(catalog_adapter, "_project_plan_graph", cancel_projection)
    with pytest.raises(KeyboardInterrupt):
        _seal(topology, database)
    assert outcomes == ["release", "release"]


def test_s1_and_s2_revalidation_detect_source_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    topology = _topology(tmp_path, ("project",))
    database = _database(tmp_path)
    document_path = topology.path_for("project") / "document.json"
    real_revalidate = topology._revalidate_strict_repository_snapshot
    mutated = False

    def drift_before_s1(expected: Any, *, _capability: Any = None):
        nonlocal mutated
        if not mutated:
            mutated = True
            document_path.write_bytes(document_path.read_bytes() + b"\n")
        return real_revalidate(expected, _capability=_capability)

    monkeypatch.setattr(
        topology, "_revalidate_strict_repository_snapshot", drift_before_s1,
    )
    with pytest.raises(Exception) as s1:
        _seal(topology, database)
    assert getattr(s1.value, "code", "") == "project_topology_conflict"

    # Use a fresh pair: the first database now intentionally contains only the
    # v4 seal for its pre-drift source and is correctly no longer reusable.
    second_root = tmp_path / "s2"
    topology = _topology(second_root, ("project",))
    database = _database(second_root)
    document_path = topology.path_for("project") / "document.json"
    real_reconcile = adapter.SqliteLegacyRepositorySealer.reconcile

    def drift_after_proof(sealer: Any, projection: Any):
        result = real_reconcile(sealer, projection)
        document_path.write_bytes(document_path.read_bytes() + b"\n")
        return result

    monkeypatch.setattr(
        adapter.SqliteLegacyRepositorySealer, "reconcile", drift_after_proof,
    )
    with pytest.raises(Exception) as s2:
        _seal(topology, database)
    assert getattr(s2.value, "code", "") == "project_topology_conflict"


def test_repository_seal_recovers_ack_loss_and_preserves_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    topology = _topology(tmp_path, ("project",))
    database = _database(tmp_path)
    real_seal = adapter.SqliteLegacyRepositorySealer.seal

    def lose_ack(sealer: Any, projection: Any):
        real_seal(sealer, projection)
        raise OSError("injected return-edge loss")

    monkeypatch.setattr(adapter.SqliteLegacyRepositorySealer, "seal", lose_ack)
    recovered = _seal(topology, database)
    assert recovered.recovered_after_ack_loss is True

    cancelled_root = tmp_path / "cancelled"
    cancelled_topology = _topology(cancelled_root, ("project",))
    cancelled_database = _database(cancelled_root)
    revalidations = 0
    real_revalidate = cancelled_topology._revalidate_strict_repository_snapshot

    def counted_revalidate(expected: Any, *, _capability: Any = None):
        nonlocal revalidations
        revalidations += 1
        return real_revalidate(expected, _capability=_capability)

    def cancel_return_edge(sealer: Any, projection: Any):
        real_seal(sealer, projection)
        raise KeyboardInterrupt("injected cancellation")

    monkeypatch.setattr(
        cancelled_topology,
        "_revalidate_strict_repository_snapshot",
        counted_revalidate,
    )
    monkeypatch.setattr(
        adapter.SqliteLegacyRepositorySealer, "seal", cancel_return_edge,
    )
    with pytest.raises(KeyboardInterrupt):
        _seal(cancelled_topology, cancelled_database)
    assert revalidations == 2  # S1 and mandatory S2 both completed


def test_stale_v4_catalog_is_a_conflict_not_a_new_repository_proof(
    tmp_path: Path,
) -> None:
    topology = _topology(tmp_path, ("project",))
    database = _database(tmp_path)
    catalog_adapter._seal_strict_legacy_catalog_sqlite_shadow(
        topology,
        database,
        _capability=catalog_adapter._LEGACY_CATALOG_SQLITE_ADAPTER_CAPABILITY,
    )

    def replacement(root: Path) -> None:
        _document("project", "replacement").save(Path(root) / "document.json")

    topology.replace(
        "project",
        expected_generation=1,
        build=replacement,
        validate=lambda _root: None,
        operation_id="f" * 32,
    )
    with pytest.raises(adapter.LegacyRepositorySqliteAdapterError) as caught:
        _seal(topology, database)
    assert caught.value.status == "conflict"


def test_repository_adapter_capability_runtime_identity_and_public_surface(
    tmp_path: Path,
) -> None:
    topology = _topology(tmp_path)
    database = _database(tmp_path)
    with pytest.raises(adapter.LegacyRepositorySqliteAdapterError) as forbidden:
        adapter._seal_strict_legacy_repository_sqlite_shadow(
            topology,
            database,
            runtime_generation=RUNTIME_GENERATION,
            code_digest=CODE_DIGEST,
        )
    assert forbidden.value.status == "forbidden"
    with pytest.raises(TypeError):
        adapter._seal_strict_legacy_repository_sqlite_shadow(
            topology,
            database,
            runtime_generation="",
            code_digest=CODE_DIGEST,
            _capability=adapter._LEGACY_REPOSITORY_SQLITE_ADAPTER_CAPABILITY,
        )
    signature = inspect.signature(
        adapter._seal_strict_legacy_repository_sqlite_shadow
    )
    assert signature.parameters["runtime_generation"].default is inspect.Parameter.empty
    assert signature.parameters["code_digest"].default is inspect.Parameter.empty
    assert "_seal_strict_legacy_repository_sqlite_shadow" not in sdk.__all__
    assert "_LEGACY_REPOSITORY_SQLITE_ADAPTER_CAPABILITY" not in sdk.__all__
