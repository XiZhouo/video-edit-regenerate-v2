"""Repository 项目复制/删除测试(header-v2 §5.9/5.10)+ server 端点双保险校验。

护:复制=整目录 copy + id/name/acl 重写(新 owner=操作者,协作者清空);
删除=逐出内存 Context + 物理删目录;路径穿越护栏;server 层 owner-only
与 confirm_name 红线(TestClient 直打端点)。
"""

from __future__ import annotations

import json
import hashlib
import multiprocessing as mp
import os
import sys
from pathlib import Path
import re
import stat
import threading

import pytest


def _repo(tmp_path, monkeypatch):
    monkeypatch.setenv("ZE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ZE_USERS_PATH", str(tmp_path / "users.json"))
    from sdk.bootstrap import bootstrap
    return bootstrap()


def test_repository_topology_open_installs_admission_for_exact_pre_v5_store(
    tmp_path,
):
    """The real bootstrap seam upgrades a closed v4 store, not just tests."""

    import importlib

    from sdk import Document
    from sdk.project_topology import ProjectTopologyCoordinator

    projects = tmp_path / "data" / "projects"
    old = ProjectTopologyCoordinator(projects)
    old.initialize(validate=lambda root: Document.open(root / "document.json"))
    old.repository_admission_path.unlink()
    old._repository_admission_install_path.unlink()

    bootstrap_module = importlib.import_module("sdk.bootstrap")
    upgraded = bootstrap_module._open_project_topology_for_repository(
        projects,
        validate=lambda root: Document.open(root / "document.json"),
    )

    admission = upgraded._read_repository_admission()
    marker = upgraded._read_repository_admission_install_marker()
    assert admission is not None
    assert admission.authority_state == "legacy"
    assert admission.revision == 1
    assert marker is not None
    assert marker.phase == "committed"
    assert marker.install_mode == "legacy_bootstrap"
    assert marker.authority_epoch == admission.authority_epoch


def test_repository_topology_open_never_resurrects_deleted_installed_fence(
    tmp_path,
):
    import importlib

    from sdk import ZeError
    from sdk.project_topology import ProjectTopologyCoordinator

    projects = tmp_path / "data" / "projects"
    installed = ProjectTopologyCoordinator(projects)
    installed.repository_admission_path.unlink()

    bootstrap_module = importlib.import_module("sdk.bootstrap")
    with pytest.raises(ZeError) as captured:
        bootstrap_module._open_project_topology_for_repository(
            projects,
            validate=lambda _root: None,
        )
    assert captured.value.code == "project_recovery_required"
    assert captured.value.context == {"reason": "admission_missing"}
    assert not installed.repository_admission_path.exists()


def _legacy_layout(tmp_path, *, project_id="legacy-project"):
    """Create one genuine pre-``projects/`` document/entities/assets root."""
    from sdk import Document, Entity
    from sdk.project_topology import ProjectTopologyCoordinator

    data = tmp_path / "data"
    data.mkdir()
    doc = Document(
        schema_version="1.0.0",
        collaboration_mode="with_save",
        id=project_id,
        name="legacy source",
    )
    doc.add(Entity(type="card", id="row-1", data={"value": "kept"}))
    doc.save(data / "document.json")
    asset = data / "assets" / "nested" / "blob.bin"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"legacy-asset")
    topology = ProjectTopologyCoordinator(data / "projects")
    topology.initialize(validate=lambda root: Document.open(root / "document.json"))
    return data, topology


def _assert_legacy_source_not_published(data, topology, project_id="legacy-project"):
    assert (data / "document.json").exists() or (data / "document.json").is_symlink()
    assert not topology.path_for(project_id).exists()
    assert topology.list_live_ids() == []
    assert list(topology.staging_root.iterdir()) == []


def _concurrent_legacy_migrate_worker(data_path, barrier, results):
    from sdk import Document
    from sdk.bootstrap import _migrate_legacy_layout
    from sdk.project_topology import ProjectTopologyCoordinator

    data = Path(data_path)
    topology = ProjectTopologyCoordinator(data / "projects", lock_timeout=5.0)
    topology.initialize(validate=lambda root: Document.open(root / "document.json"))
    try:
        barrier.wait(10)
        _migrate_legacy_layout(data, topology)
        results.put(("ok", topology.list_live_ids()))
    except BaseException as exc:
        results.put((getattr(exc, "code", type(exc).__name__), str(exc)))


def _same_generation_document_drift_worker(
    data_path,
    users_path,
    project_id,
    results,
):
    """Advance one project head from an independent repository process."""
    os.environ["ZE_DATA_DIR"] = data_path
    os.environ["ZE_USERS_PATH"] = users_path
    # spawn 会把父进程的 sys.path 一并继承。全量套件里,前面的测试可能把自己的**临时 mod 目录**
    # (pytest tmp 下的 05-code)留在 sys.path 上;本进程随后 import document_register 就会命中
    # 别人的副本而不是本 skill 的 —— 实测报 "cannot import name 'MODEL_ENUM' from document_register
    # (/tmp/pytest-of-*/…/05-code/document_register.py)",于是 worker 写不成、外面看到版本没 +1。
    # 独立进程应当自带干净的解析环境:剔掉继承来的 pytest 临时路径。
    sys.path[:] = [p for p in sys.path if "pytest-of-" not in p]

    from document import read_document_metadata
    from sdk.bootstrap import bootstrap

    try:
        repository = bootstrap()
        writer = repository.get_or_load(project_id)
        with writer.lifecycle_guard():
            with writer.lock:
                writer.doc.rename(
                    "worker-b",
                    expected_version=writer.doc.version,
                )
                writer.doc.save(
                    writer.doc_path,
                    expected_version=writer.doc.version,
                )
        durable = read_document_metadata(writer.doc_path)
        results.put(
            (
                "ok",
                durable.version,
                durable.name,
                repository.project_meta(project_id)["document_version"],
            )
        )
    except BaseException as exc:
        results.put(
            (
                "error",
                getattr(exc, "code", type(exc).__name__),
                str(exc),
            )
        )


# ============================================================
# Legacy root-layout migration trust boundary
# ============================================================

def test_legacy_layout_migrates_normal_document_entities_and_assets(tmp_path):
    from sdk import Document
    from sdk.bootstrap import _migrate_legacy_layout

    data, topology = _legacy_layout(tmp_path)
    _migrate_legacy_layout(data, topology)

    migrated = topology.path_for("legacy-project")
    opened = Document.open(migrated / "document.json")
    assert opened.get("card", "row-1").data == {"value": "kept"}
    assert (migrated / "assets" / "nested" / "blob.bin").read_bytes() == b"legacy-asset"
    assert not (data / "document.json").exists()
    assert not (data / "entities").exists()
    assert not (data / "assets").exists()


def test_legacy_document_with_path_identity_requires_recovery(tmp_path):
    from sdk import ZeError
    from sdk.bootstrap import _migrate_legacy_layout

    data, topology = _legacy_layout(tmp_path, project_id="../escape")
    with pytest.raises(ZeError) as captured:
        _migrate_legacy_layout(data, topology)
    assert captured.value.code == "project_recovery_required"
    assert captured.value.context == {"reason": "legacy_project_id_invalid"}
    assert (data / "document.json").exists()
    assert topology.list_live_ids() == []
    assert not (tmp_path / "escape").exists()


def test_two_bootstraps_migrate_one_legacy_root_exactly_once(tmp_path):
    data, _topology = _legacy_layout(tmp_path)
    ctx = mp.get_context("spawn")
    barrier = ctx.Barrier(2)
    results = ctx.Queue()
    workers = [
        ctx.Process(
            target=_concurrent_legacy_migrate_worker,
            args=(str(data), barrier, results),
        )
        for _ in range(2)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(15)
        if worker.is_alive():
            worker.terminate()
            worker.join(5)
            pytest.fail("legacy migration worker did not terminate")
        assert worker.exitcode == 0

    outcomes = [results.get(timeout=2) for _ in workers]
    assert outcomes == [("ok", ["legacy-project"]), ("ok", ["legacy-project"])]

    from sdk import Document
    from sdk.project_topology import OUTCOME_SCHEMA, ProjectTopologyCoordinator

    recovered = ProjectTopologyCoordinator(data / "projects")
    recovered.initialize(validate=lambda root: Document.open(root / "document.json"))
    with recovered.open_live("legacy-project") as handle:
        assert handle.generation == 1
    receipts = [json.loads(path.read_text()) for path in recovered.outcome_root.glob("*.json")]
    legacy_receipts = [row for row in receipts if row.get("kind") == "legacy_migrate"]
    assert len(legacy_receipts) == 1
    assert legacy_receipts[0]["schema"] == OUTCOME_SCHEMA
    assert not (data / "document.json").exists()
    assert not (data / "entities").exists()
    assert not (data / "assets").exists()


@pytest.mark.parametrize(
    "storage_code",
    ["document_lock_unavailable", "document_storage_unsupported"],
)
def test_legacy_migration_preserves_pre_yield_storage_availability_error(
    tmp_path,
    monkeypatch,
    storage_code,
):
    from contextlib import contextmanager
    import importlib

    from sdk import ZeError

    bootstrap_module = importlib.import_module("sdk.bootstrap")
    data, topology = _legacy_layout(tmp_path)
    original = ZeError(
        storage_code,
        "original storage availability diagnosis",
        context={"reason": "sentinel", "commit_point_reached": False},
    )

    @contextmanager
    def fail_before_yield(*_args, **_kwargs):
        raise original
        yield  # pragma: no cover - keeps this a generator context manager

    monkeypatch.setattr(
        bootstrap_module,
        "open_document_head",
        fail_before_yield,
    )

    with pytest.raises(ZeError) as captured:
        bootstrap_module._migrate_legacy_layout(data, topology)
    assert captured.value is original
    assert captured.value.code == storage_code
    assert captured.value.context == {
        "reason": "sentinel",
        "commit_point_reached": False,
    }
    assert (data / "document.json").exists()
    assert topology.list_live_ids() == []


def test_legacy_document_root_symlink_is_never_read_or_cleaned(tmp_path):
    from sdk import ZeError
    from sdk.bootstrap import _migrate_legacy_layout
    from sdk.project_topology import ProjectTopologyCoordinator

    data = tmp_path / "data"
    data.mkdir()
    outside = tmp_path / "outside-document.json"
    outside.write_bytes(b'{"id":"outside-project","sentinel":"outside"}')
    try:
        (data / "document.json").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable on this platform")
    topology = ProjectTopologyCoordinator(data / "projects")

    with pytest.raises(ZeError) as captured:
        _migrate_legacy_layout(data, topology)
    assert captured.value.code == "project_recovery_required"
    assert (data / "document.json").is_symlink()
    assert outside.read_bytes() == b'{"id":"outside-project","sentinel":"outside"}'
    assert topology.list_live_ids() == []


def test_legacy_root_directory_symlink_fails_before_publish_or_cleanup(tmp_path):
    from sdk import ZeError
    from sdk.bootstrap import _migrate_legacy_layout

    data, topology = _legacy_layout(tmp_path)
    original = data / "entities"
    outside = tmp_path / "outside-entities"
    original.rename(outside)
    try:
        original.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        outside.rename(original)
        pytest.skip("symlinks are unavailable on this platform")

    with pytest.raises(ZeError) as captured:
        _migrate_legacy_layout(data, topology)
    assert captured.value.code == "project_topology_write_failed"
    _assert_legacy_source_not_published(data, topology)
    assert original.is_symlink()
    assert outside.exists()


def test_legacy_nested_symlink_fails_before_publish_or_cleanup(tmp_path):
    from sdk import ZeError
    from sdk.bootstrap import _migrate_legacy_layout

    data, topology = _legacy_layout(tmp_path)
    outside = tmp_path / "outside-asset.bin"
    outside.write_bytes(b"outside")
    link = data / "assets" / "nested" / "linked.bin"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable on this platform")

    with pytest.raises(ZeError) as captured:
        _migrate_legacy_layout(data, topology)
    assert captured.value.code == "project_topology_write_failed"
    _assert_legacy_source_not_published(data, topology)
    assert link.is_symlink()
    assert outside.read_bytes() == b"outside"


def test_legacy_hardlinked_document_is_never_read_or_cleaned(tmp_path):
    from sdk import ZeError
    from sdk.bootstrap import _migrate_legacy_layout
    from sdk.project_topology import ProjectTopologyCoordinator

    data = tmp_path / "data"
    data.mkdir()
    outside = tmp_path / "outside-document.json"
    outside.write_bytes(b'{"id":"legacy-project"}')
    try:
        os.link(outside, data / "document.json")
    except (OSError, NotImplementedError):
        pytest.skip("hard links are unavailable on this platform")
    topology = ProjectTopologyCoordinator(data / "projects")

    with pytest.raises(ZeError) as captured:
        _migrate_legacy_layout(data, topology)
    assert captured.value.code == "project_recovery_required"
    assert (data / "document.json").exists()
    assert outside.exists()
    assert os.stat(outside).st_nlink == 2
    assert topology.list_live_ids() == []


@pytest.mark.parametrize("unsafe_kind", ["hardlink", "fifo"])
def test_legacy_nested_hardlink_or_fifo_fails_closed(tmp_path, unsafe_kind):
    from sdk import ZeError
    from sdk.bootstrap import _migrate_legacy_layout

    data, topology = _legacy_layout(tmp_path)
    unsafe = data / "assets" / "nested" / f"unsafe-{unsafe_kind}"
    if unsafe_kind == "hardlink":
        outside = tmp_path / "outside-hardlink.bin"
        outside.write_bytes(b"outside-hardlink")
        try:
            os.link(outside, unsafe)
        except (OSError, NotImplementedError):
            pytest.skip("hard links are unavailable on this platform")
    else:
        if not hasattr(os, "mkfifo"):
            pytest.skip("FIFOs are unavailable on this platform")
        try:
            os.mkfifo(unsafe)
        except (OSError, NotImplementedError):
            pytest.skip("FIFOs are unavailable on this platform")

    with pytest.raises(ZeError) as captured:
        _migrate_legacy_layout(data, topology)
    assert captured.value.code == "project_topology_write_failed"
    _assert_legacy_source_not_published(data, topology)
    if unsafe_kind == "hardlink":
        assert unsafe.exists()
        assert os.stat(unsafe).st_nlink == 2
    else:
        assert stat.S_ISFIFO(unsafe.lstat().st_mode)


# ============================================================
# Repository.duplicate_project
# ============================================================

def test_duplicate_copies_entities_and_rewrites_meta(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    src = repo.create_project(name="原件", owner="alice", seed=True)
    # 加个协作者,验证副本 acl 清空重写
    raw_path = tmp_path / "data" / "projects" / src / "document.json"
    raw = json.loads(raw_path.read_text())
    raw["acl"]["collaborators"] = ["bob"]
    raw_path.write_text(json.dumps(raw, ensure_ascii=False))

    new = repo.duplicate_project(src, owner="bob")
    assert new != src
    meta = repo.project_meta(new)
    assert meta["name"] == "原件 副本"
    assert meta["acl"] == {"owner": "bob", "collaborators": []}
    # 实体随目录整体复制(seed 的 demo 实体在副本里可见)
    src_ents = repo.get_or_load(src).doc.types()
    dst_ents = repo.get_or_load(new).doc.types()
    assert dst_ents == src_ents
    # 副本以空操作历史起步(不带走源项目 oplog;review MINOR 修复)
    assert not (tmp_path / "data" / "projects" / new / "oplog.ndjson").exists()


def test_duplicate_custom_name_and_id(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    src = repo.create_project(name="原件", owner="alice", seed=False)
    new = repo.duplicate_project(src, owner="alice", name="另存目标", project_id="my-copy-1")
    assert new == "my-copy-1"
    assert repo.project_meta(new)["name"] == "另存目标"


def test_duplicate_missing_source_raises(tmp_path, monkeypatch):
    from sdk import ZeError
    repo = _repo(tmp_path, monkeypatch)
    with pytest.raises(ZeError) as ei:
        repo.duplicate_project("nope", owner="alice")
    assert ei.value.code == "project_not_found"


def test_duplicate_existing_target_raises(tmp_path, monkeypatch):
    from sdk import ZeError
    repo = _repo(tmp_path, monkeypatch)
    src = repo.create_project(name="A", owner="alice", seed=False)
    other = repo.create_project(name="B", owner="alice", seed=False)
    with pytest.raises(ZeError) as ei:
        repo.duplicate_project(src, owner="alice", project_id=other)
    assert ei.value.code == "project_topology_conflict"


def test_duplicate_path_traversal_guarded(tmp_path, monkeypatch):
    from sdk import ZeError
    repo = _repo(tmp_path, monkeypatch)
    with pytest.raises(ZeError):
        repo.duplicate_project("../escape", owner="alice")


def test_duplicate_cleans_dst_when_copy_invalid(tmp_path, monkeypatch):
    """副本校验失败(Document.open 报缺实体文件等)→ 删掉残留副本目录并报错,不留半残副本。"""
    from sdk import bootstrap as _bs
    repo = _repo(tmp_path, monkeypatch)
    src = repo.create_project(name="原件", owner="alice", seed=True)

    def boom_open(*a, **k):
        raise _bs.ZeError("entity_file_missing", "simulated broken copy")
    monkeypatch.setattr(_bs.Document, "open", boom_open)

    with pytest.raises(_bs.ZeError):
        repo.duplicate_project(src, owner="bob", project_id="copy-x")
    assert not (tmp_path / "data" / "projects" / "copy-x").exists()   # 残留副本已清


def test_two_repositories_fail_closed_on_same_generation_document_drift(
    tmp_path,
    monkeypatch,
):
    """A cached worker never blesses new Document bytes with its older OpLog."""
    from document import read_document_metadata
    from sdk import ZeError

    repo_a = _repo(tmp_path, monkeypatch)
    project_id = repo_a.create_project(name="worker-a", owner="alice", seed=False)
    cached = repo_a.get_or_load(project_id)
    cached_version = cached.doc.version

    process_context = mp.get_context("spawn")
    results = process_context.Queue()
    worker = process_context.Process(
        target=_same_generation_document_drift_worker,
        args=(
            str(tmp_path / "data"),
            str(tmp_path / "users.json"),
            project_id,
            results,
        ),
    )
    worker.start()
    worker.join(15)
    if worker.is_alive():
        worker.terminate()
        worker.join(5)
        pytest.fail("independent repository worker did not terminate")
    assert worker.exitcode == 0

    # 先取 worker 的自述结果:它整个包在 except BaseException 里(出错也正常退出、exitcode 0),
    # 若把 durable.version 的断言放在前面,worker 真正报的错会被版本断言掩盖、日志里永远看不到。
    outcome = results.get(timeout=2)
    assert outcome[0] == "ok", f"independent repository worker failed: {outcome}"

    durable = read_document_metadata(cached.doc_path)
    assert durable.version == cached_version + 1
    assert durable.name == "worker-b"
    assert outcome == (
        "ok",
        durable.version,
        durable.name,
        durable.version,
    )

    for _ in range(2):
        with pytest.raises(ZeError) as captured:
            repo_a.get_or_load(project_id)
        assert captured.value.code == "commit_repair_required"
        assert captured.value.context == {
            "project_id": project_id,
            "cached_document_version": cached_version,
            "durable_document_version": durable.version,
            "reason": "same_generation_document_drift",
        }
        assert repo_a._contexts[project_id] is cached
        assert cached.doc.name == "worker-a"


def test_cache_miss_rejects_head_advanced_while_context_was_building(
    tmp_path,
    monkeypatch,
):
    """A first load must fence again after its potentially long runtime build."""
    import importlib

    from sdk import Document, ZeError

    bootstrap_module = importlib.import_module("sdk.bootstrap")
    repo = _repo(tmp_path, monkeypatch)
    project_id = repo.create_project(name="before", owner="alice", seed=False)
    real_build = bootstrap_module.build_project_context
    built_contexts = []
    retired_contexts = []

    def racing_build(*args, **kwargs):
        ctx = real_build(*args, **kwargs)
        built_contexts.append(ctx)
        real_shutdown = ctx.supervisor.shutdown

        def observed_shutdown(*, join_timeout=5.0):
            retired_contexts.append(ctx)
            return real_shutdown(join_timeout=join_timeout)

        ctx.supervisor.shutdown = observed_shutdown
        remote = Document.open(ctx.doc_path)
        remote.rename("advanced", expected_version=remote.version)
        remote.save(ctx.doc_path, expected_version=remote.version)
        return ctx

    monkeypatch.setattr(bootstrap_module, "build_project_context", racing_build)

    with pytest.raises(ZeError) as captured:
        repo.get_or_load(project_id)
    assert captured.value.code == "commit_repair_required"
    assert captured.value.context == {
        "project_id": project_id,
        "cached_document_version": 2,
        "durable_document_version": 3,
        "reason": "same_generation_document_drift",
    }
    assert project_id not in repo._contexts
    assert retired_contexts == built_contexts
    assert len(built_contexts) == 1
    assert built_contexts[0].doc.name == "before"


def test_cached_context_probe_waits_for_local_commit_coherence(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    project_id = repo.create_project(name="before", owner="alice", seed=False)
    ctx = repo.get_or_load(project_id)
    committed_while_locked = threading.Event()
    release_commit = threading.Event()
    writer_errors = []
    reader_results = []

    def writer():
        try:
            with ctx.lifecycle_guard():
                with ctx.lock:
                    ctx.doc.rename("after", expected_version=ctx.doc.version)
                    ctx.doc.save(ctx.doc_path, expected_version=ctx.doc.version)
                    committed_while_locked.set()
                    if not release_commit.wait(10):
                        raise TimeoutError("test commit release timed out")
        except BaseException as exc:
            writer_errors.append(exc)

    def reader():
        try:
            reader_results.append(repo.get_or_load(project_id))
        except BaseException as exc:
            reader_results.append(exc)

    writer_thread = threading.Thread(target=writer)
    writer_thread.start()
    assert committed_while_locked.wait(5)
    reader_thread = threading.Thread(target=reader)
    reader_thread.start()
    reader_thread.join(0.2)
    reader_was_blocked = reader_thread.is_alive()
    release_commit.set()
    writer_thread.join(5)
    reader_thread.join(5)

    assert reader_was_blocked is True
    assert not writer_thread.is_alive()
    assert not reader_thread.is_alive()
    assert writer_errors == []
    assert reader_results == [ctx]
    assert ctx.doc.name == "after"


def test_production_document_consumers_use_the_strict_metadata_boundary():
    root = Path(__file__).resolve().parents[1]
    targets = [root / "server.py", root / "sdk" / "bootstrap.py"]
    forbidden = (
        re.compile(r"document\.json[^\n]{0,160}read_(?:text|bytes)"),
        re.compile(r"json\.loads\([^\n]*(?:doc_path|document_path)"),
        re.compile(r"json\.loads\(payload\.decode"),
    )
    for path in targets:
        source = path.read_text(encoding="utf-8")
        for pattern in forbidden:
            assert pattern.search(source) is None, (path.name, pattern.pattern)

    # The capability and private writer belong only to ProjectTopologyCoordinator.
    public_consumers = [*targets, root / "sdk" / "__init__.py"]
    for path in public_consumers:
        source = path.read_text(encoding="utf-8")
        assert "_TOPOLOGY_STAGING_METADATA_CAPABILITY" not in source
        assert "_rewrite_document_metadata_for_topology_staging" not in source


def test_direct_project_load_rejects_malformed_durable_acl(tmp_path, monkeypatch):
    """A malformed ACL must not become a public project on a direct URL."""
    from sdk import ZeError

    repo = _repo(tmp_path, monkeypatch)
    pid = repo.create_project(name="私有项目", owner="alice", seed=False)
    doc_path = tmp_path / "data" / "projects" / pid / "document.json"
    raw = json.loads(doc_path.read_text())
    raw["acl"] = []
    doc_path.write_text(json.dumps(raw))

    with pytest.raises(ZeError) as load_error:
        repo.get_or_load(pid)
    assert load_error.value.code == "project_recovery_required"
    with pytest.raises(ZeError) as meta_error:
        repo.project_meta(pid)
    assert meta_error.value.code == "project_recovery_required"


def test_direct_project_load_rejects_namespace_document_id_mismatch(tmp_path, monkeypatch):
    from sdk import ZeError

    repo = _repo(tmp_path, monkeypatch)
    pid = repo.create_project(name="identity", owner="alice", seed=False)
    doc_path = tmp_path / "data" / "projects" / pid / "document.json"
    raw = json.loads(doc_path.read_text())
    raw["id"] = "different-project"
    doc_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ZeError) as captured:
        repo.get_or_load(pid)
    assert captured.value.code == "project_recovery_required"
    assert captured.value.context == {
        "project_id": pid,
        "reason": "document_id_mismatch",
    }
    assert pid not in repo._contexts


# ============================================================
# Repository.delete_project
# ============================================================

def test_delete_removes_dir_and_evicts_context(tmp_path, monkeypatch):
    from sdk import ZeError
    repo = _repo(tmp_path, monkeypatch)
    pid = repo.create_project(name="将删", owner="alice", seed=False)
    repo.get_or_load(pid)                      # 先加载进内存
    repo.delete_project(pid)
    assert not (tmp_path / "data" / "projects" / pid).exists()
    assert pid not in repo._contexts           # 内存 Context 已逐出
    with pytest.raises(ZeError) as ei:
        repo.delete_project(pid)
    assert ei.value.code == "project_not_found"


def test_delete_loaded_project_shuts_down_supervisor(tmp_path, monkeypatch):
    """删已加载项目:先 supervisor.shutdown()(停背景 worker,杜绝 rmtree 后又写回重建 orphan),
    再逐出 Context + 删目录。验证 shutdown 被调用一次。"""
    repo = _repo(tmp_path, monkeypatch)
    pid = repo.create_project(name="将删", owner="alice", seed=False)
    ctx = repo.get_or_load(pid)
    assert pid in repo._contexts
    called = {"n": 0}
    real = ctx.supervisor.shutdown
    def spy(*a, **k):
        called["n"] += 1
        return real(*a, **k)
    monkeypatch.setattr(ctx.supervisor, "shutdown", spy)
    repo.delete_project(pid)
    assert called["n"] == 1                     # 删除前确有序停机
    assert pid not in repo._contexts
    assert not (tmp_path / "data" / "projects" / pid).exists()


def test_delete_path_traversal_guarded(tmp_path, monkeypatch):
    from sdk import ZeError
    repo = _repo(tmp_path, monkeypatch)
    with pytest.raises(ZeError):
        repo.delete_project("../../etc")


def test_repository_intent_reconciliation_is_kind_aware(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    create_intent = "a" * 32
    pid = repo.create_project(
        name="可对账", owner="alice", project_id="receipt-project",
        intent_id=create_intent,
    )

    committed = repo.project_intent_status(pid, create_intent, kind="create")
    assert committed["status"] == "committed"
    assert committed["generation"] == 1
    assert committed["proof_revision"] == 2
    assert committed["principal_scoped"] is False
    assert committed["intent_reserved"] is False
    assert repo.project_intent_status(
        pid, create_intent, kind="delete",
    )["status"] == "conflict"
    assert repo.project_intent_status(
        pid, "b" * 32, kind="delete",
    )["status"] == "not_committed"


# ============================================================
# server 端点双保险(owner-only + confirm_name 红线)
# ============================================================

def _load_app(tmp_path, monkeypatch):
    monkeypatch.setenv("ZE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ZE_USERS_PATH", str(tmp_path / "users.json"))
    monkeypatch.setenv("ZE_POLICIES_PATH", str(tmp_path / "policies.json"))
    monkeypatch.setenv("ZE_SUPER_ADMIN_USERS", "alice,bob")
    import importlib
    import server as server_mod
    importlib.reload(server_mod)               # 让 _repo 按本测试的 env 重建
    return server_mod.app


def _run_scenario(app, steps):
    """单 asyncio 事件循环内顺序打 ASGI app。

    坑(verify_all 的 pytest 路径注入):01-core/02-data-flow/05-concurrency/ 在 sys.path
    上,其 queue.py 会遮蔽 stdlib `queue`,导致 anyio asyncio 后端
    `from queue import Queue` ImportError。此处作用域化排雷:临时摘掉该路径与被劫持的
    queue 模块,跑完恢复(不影响其他叶子测试的注入依赖)。
    """
    import asyncio
    import sys

    shadow_paths = [p for p in sys.path if p.replace("\\", "/").endswith("05-concurrency")]
    saved_queue = sys.modules.get("queue")
    leaf_queue = saved_queue is not None and "05-concurrency" in str(
        getattr(saved_queue, "__file__", ""))
    try:
        for p in shadow_paths:
            sys.path.remove(p)
        if leaf_queue:
            sys.modules.pop("queue", None)
        # anyio 后端按需 import;确保此刻解析到 stdlib queue
        import httpx

        async def _main():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
                return await steps(client)

        return asyncio.run(_main())
    finally:
        for p in shadow_paths:
            if p not in sys.path:
                sys.path.insert(0, p)
        if leaf_queue and saved_queue is not None:
            sys.modules["queue"] = saved_queue


def test_endpoint_minimal_local_auth_config_and_email_login(tmp_path, monkeypatch):
    monkeypatch.setenv("ZE_AUTH_MODE", "minimal-local")
    monkeypatch.setenv("ZE_AUTH_REGISTRATION", "open")
    app = _load_app(tmp_path, monkeypatch)

    async def steps(client):
        config_response = await client.get("/api/auth/config")
        assert config_response.status_code == 200
        config = config_response.json()
        assert config == {
            "schema_version": "ze.auth-runtime-config.v1",
            "mode": "minimal-local",
            "methods": ["local"],
            "preferred_method": "local",
            "registration": "open",
            "login_name_format": "email",
            "email_verification_required": False,
        }
        assert config_response.headers["cache-control"] == "no-store"

        invalid = await client.post(
            "/api/auth/register",
            json={"username": "alice", "password": "alice-pass-1"},
        )
        assert invalid.status_code == 422

        registered = await client.post(
            "/api/auth/register",
            json={"username": " Alice@Example.COM ", "password": "alice-pass-1"},
        )
        assert registered.status_code == 200
        assert registered.json()["user"]["id"] == "alice@example.com"

        logged_in = await client.post(
            "/api/auth/login",
            json={"username": "ALICE@EXAMPLE.COM", "password": "alice-pass-1"},
        )
        assert logged_in.status_code == 200
        assert logged_in.json()["user"]["id"] == "alice@example.com"

        disabled = await client.get("/api/auth/lark/sse")
        assert disabled.status_code == 403
        assert disabled.json()["error"]["code"] == "auth_method_disabled"

    _run_scenario(app, steps)


def test_endpoint_closed_registration_is_enforced_server_side(tmp_path, monkeypatch):
    monkeypatch.setenv("ZE_AUTH_MODE", "hybrid")
    monkeypatch.setenv("ZE_AUTH_REGISTRATION", "closed")
    monkeypatch.setenv("ZE_AUTH_PREFERRED_METHOD", "local")
    app = _load_app(tmp_path, monkeypatch)

    async def steps(client):
        config = (await client.get("/api/auth/config")).json()
        assert config["registration"] == "closed"
        response = await client.post(
            "/api/auth/register",
            json={"username": "alice", "password": "alice-pass-1"},
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "registration_closed"

    _run_scenario(app, steps)


def test_endpoint_federated_mode_rejects_local_password_routes(tmp_path, monkeypatch):
    monkeypatch.setenv("ZE_AUTH_MODE", "federated")
    monkeypatch.setenv("ZE_AUTH_REGISTRATION", "open")
    app = _load_app(tmp_path, monkeypatch)

    async def steps(client):
        config = (await client.get("/api/auth/config")).json()
        assert config["methods"] == ["feishu"]
        assert config["registration"] == "closed"
        for path in ("/api/auth/register", "/api/auth/login"):
            response = await client.post(
                path,
                json={"username": "alice", "password": "alice-pass-1"},
            )
            assert response.status_code == 403
            assert response.json()["error"]["code"] == "auth_method_disabled"

    _run_scenario(app, steps)


def _auth(tok):
    # All projects created in these endpoint tests begin at topology generation 1.
    return {"Authorization": f"Bearer {tok}", "X-Project-Generation": "1"}


def _intent(tag: str) -> str:
    return hashlib.sha256(tag.encode("utf-8")).hexdigest()[:32]


def _create_request(tag: str, *, name: str) -> tuple[dict, dict]:
    intent = _intent(f"intent:{tag}")
    body = {
        "name": name,
        "project_id": f"p-{_intent(f'project:{tag}')[:12]}",
        "intent_id": intent,
    }
    return body, {"Idempotency-Key": f"test:create:{tag}:{intent}"}


def _topology_headers(token: str, tag: str) -> dict:
    return {**_auth(token), "Idempotency-Key": f"test:topology:{tag}"}


def test_endpoint_delete_owner_only_and_confirm_name(tmp_path, monkeypatch):
    app = _load_app(tmp_path, monkeypatch)

    async def steps(client):
        tokens = {}
        for u in ("alice", "bob"):
            r = await client.post("/api/auth/register",
                                  json={"username": u, "password": f"{u}-pass-1"})
            tokens[u] = r.json()["token"]
        client.cookies.clear()
        body, extra = _create_request("delete-owner-source", name="甲的项目")
        r = await client.post("/api/projects", json=body,
                              headers={**_auth(tokens["alice"]), **extra})
        pid = r.json()["project_id"]

        # 非 owner → 403
        r = await client.request("DELETE", f"/api/projects/{pid}",
                                 json={"confirm_name": "甲的项目", "intent_id": _intent("delete-bob")},
                                 headers=_topology_headers(tokens["bob"], "delete-bob"))
        assert r.status_code == 403

        # owner 但 confirm_name 不匹配 → 422(红线不只靠前端)
        r = await client.request("DELETE", f"/api/projects/{pid}",
                                 json={"confirm_name": "打错了", "intent_id": _intent("delete-wrong")},
                                 headers=_topology_headers(tokens["alice"], "delete-wrong"))
        assert r.status_code == 422

        # owner + 全名匹配 → 删除成功
        r = await client.request("DELETE", f"/api/projects/{pid}",
                                 json={"confirm_name": "甲的项目", "intent_id": _intent("delete-right")},
                                 headers=_topology_headers(tokens["alice"], "delete-right"))
        assert r.status_code == 200
        r = await client.get("/api/projects", headers=_auth(tokens["alice"]))
        assert all(p["id"] != pid for p in r.json()["projects"])

    _run_scenario(app, steps)


def test_endpoint_duplicate_member_gets_ownership(tmp_path, monkeypatch):
    app = _load_app(tmp_path, monkeypatch)

    async def steps(client):
        tokens = {}
        for u in ("alice", "bob"):
            r = await client.post("/api/auth/register",
                                  json={"username": u, "password": f"{u}-pass-1"})
            tokens[u] = r.json()["token"]
        client.cookies.clear()
        body, extra = _create_request("duplicate-source", name="共享源")
        r = await client.post("/api/projects", json=body,
                              headers={**_auth(tokens["alice"]), **extra})
        pid = r.json()["project_id"]

        # 非成员 bob 复制 → 403(有主项目,bob 不在 acl)
        r = await client.post(f"/api/projects/{pid}/duplicate", json={
            "project_id": "p-bob-denied", "intent_id": _intent("duplicate-bob"),
        }, headers=_topology_headers(tokens["bob"], "duplicate-bob"))
        assert r.status_code == 403

        # owner 复制 → 新副本 owner=操作者
        r = await client.post(f"/api/projects/{pid}/duplicate", json={
            "name": "我的副本",
            "project_id": "p-owner-copy",
            "intent_id": _intent("duplicate-owner"),
        }, headers=_topology_headers(tokens["alice"], "duplicate-owner"))
        assert r.status_code == 200
        assert r.json()["kind"] == "duplicate"
        assert r.json()["generation"] == r.json()["project_generation"] == 1
        assert r.json()["source_generation"] == 1
        new_pid = r.json()["project_id"]
        r = await client.get("/api/projects", headers=_auth(tokens["alice"]))
        mine = {p["id"]: p for p in r.json()["projects"]}
        assert mine[new_pid]["name"] == "我的副本"
        assert mine[new_pid]["is_owner"] is True

    _run_scenario(app, steps)


def test_endpoint_create_project_idempotency_replays_and_rejects_mismatch(tmp_path, monkeypatch):
    app = _load_app(tmp_path, monkeypatch)

    async def steps(client):
        r = await client.post("/api/auth/register",
                              json={"username": "alice", "password": "alice-pass-1"})
        token = r.json()["token"]
        headers = {**_auth(token), "Idempotency-Key": "project-create-1"}
        intent = _intent("project-create-idempotent")
        body = {
            "name": "幂等项目",
            "project_id": "p-idempotent",
            "intent_id": intent,
        }

        r1 = await client.post("/api/projects", json=body, headers=headers)
        r2 = await client.post("/api/projects", json=body, headers=headers)
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r2.json() == r1.json()
        assert r1.json()["kind"] == "create"
        assert r1.json()["generation"] == r1.json()["project_generation"] == 1

        r3 = await client.post("/api/projects", json={**body, "name": "另一个项目"}, headers=headers)
        assert r3.status_code == 409
        assert r3.json()["error"]["code"] == "idempotency_key_reused"

        r = await client.get("/api/projects", headers=_auth(token))
        projects = [p for p in r.json()["projects"] if p["name"] == "幂等项目"]
        assert len(projects) == 1

    _run_scenario(app, steps)


def test_endpoint_topology_requires_target_intent_and_key_then_reconciles(tmp_path, monkeypatch):
    app = _load_app(tmp_path, monkeypatch)

    async def steps(client):
        r = await client.post(
            "/api/auth/register",
            json={"username": "alice", "password": "alice-pass-1"},
        )
        token = r.json()["token"]

        missing = await client.post(
            "/api/projects", json={"name": "不安全请求"}, headers=_auth(token),
        )
        assert missing.status_code == 422
        assert missing.json()["error"]["code"] == "validation_failed"

        intent = _intent("required-contract")
        body = {"name": "可对账项目", "project_id": "p-reconcile", "intent_id": intent}
        no_key = await client.post("/api/projects", json=body, headers=_auth(token))
        assert no_key.status_code == 422
        assert no_key.json()["error"]["code"] == "idempotency_key_required"

        created = await client.post(
            "/api/projects",
            json=body,
            headers=_topology_headers(token, "required-contract"),
        )
        assert created.status_code == 200
        proof = await client.get(
            f"/api/projects/p-reconcile/intents/{intent}?kind=create", headers=_auth(token),
        )
        assert proof.status_code == 200
        assert proof.json()["status"] == "committed"
        assert proof.json()["generation"] == 1

        # A delete intent that was never sent sees the existing live project as
        # its untouched baseline, not as a conflicting create.
        unsent_delete = _intent("unsent-delete")
        untouched = await client.get(
            f"/api/projects/p-reconcile/intents/{unsent_delete}?kind=delete",
            headers=_auth(token),
        )
        assert untouched.status_code == 200
        assert untouched.json()["status"] == "not_committed"

        invalid_kind = await client.get(
            f"/api/projects/p-reconcile/intents/{unsent_delete}?kind=replace",
            headers=_auth(token),
        )
        assert invalid_kind.status_code == 422

        other_intent = _intent("same-target-other-intent")
        conflict = await client.post(
            "/api/projects",
            json={**body, "intent_id": other_intent},
            headers=_topology_headers(token, "same-target-other-intent"),
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "project_topology_conflict"
        other_proof = await client.get(
            f"/api/projects/p-reconcile/intents/{other_intent}?kind=create",
            headers=_auth(token),
        )
        assert other_proof.json()["status"] == "conflict"

    _run_scenario(app, steps)


def test_endpoint_intent_scope_reservation_and_authoritative_gets_are_no_store(
    tmp_path, monkeypatch,
):
    app = _load_app(tmp_path, monkeypatch)

    async def steps(client):
        tokens = {}
        for username in ("alice", "bob"):
            registered = await client.post(
                "/api/auth/register",
                json={"username": username, "password": f"{username}-pass-1"},
            )
            tokens[username] = registered.json()["token"]
        client.cookies.clear()

        target = "p-principal-claim"
        intent = _intent("principal-bound-intent")
        status_url = f"/api/projects/{target}/intents/{intent}?kind=create"
        alice_status = await client.get(status_url, headers=_auth(tokens["alice"]))
        assert alice_status.status_code == 200
        assert alice_status.json()["status"] == "not_committed"
        assert alice_status.json()["kind"] == "create"
        assert alice_status.json()["proof_revision"] == 2
        assert alice_status.json()["principal_scoped"] is True
        assert alice_status.json()["intent_reserved"] is False
        assert "principal_bound" not in alice_status.json()
        assert alice_status.headers["cache-control"] == "no-store"

        body = {"name": "绑定项目", "project_id": target, "intent_id": intent}
        created = await client.post(
            "/api/projects", json=body,
            headers={
                **_auth(tokens["alice"]),
                "Idempotency-Key": "principal-bound-alice",
            },
        )
        assert created.status_code == 200

        committed_status = await client.get(
            status_url, headers=_auth(tokens["alice"]),
        )
        assert committed_status.status_code == 200
        assert committed_status.json()["status"] == "committed"
        assert committed_status.json()["proof_revision"] == 2
        assert committed_status.json()["principal_scoped"] is True
        assert committed_status.json()["intent_reserved"] is True

        foreign_status = await client.get(status_url, headers=_auth(tokens["bob"]))
        assert foreign_status.status_code == 403
        assert foreign_status.json()["error"]["code"] == "acl_denied"
        foreign_create = await client.post(
            "/api/projects", json=body,
            headers={
                **_auth(tokens["bob"]),
                "Idempotency-Key": "principal-bound-bob",
            },
        )
        assert foreign_create.status_code == 403
        assert created.json()["kind"] == "create"

        listed = await client.get("/api/projects", headers=_auth(tokens["alice"]))
        assert listed.headers["cache-control"] == "no-store"
        ids = [row["id"] for row in listed.json()["projects"]]
        assert len(ids) == len(set(ids))
        document = await client.get(
            f"/api/document?project_id={target}", headers=_auth(tokens["alice"]),
        )
        assert document.headers["cache-control"] == "no-store"

    _run_scenario(app, steps)


def test_topology_error_envelope_defaults_unclassified_outcome_to_unknown(
    tmp_path, monkeypatch,
):
    app = _load_app(tmp_path, monkeypatch)
    import server as server_mod
    from sdk import ZeError

    def uncertain_create(**_kwargs):
        raise ZeError(
            "project_topology_durability_unknown",
            "injected namespace fsync uncertainty",
        )

    monkeypatch.setattr(server_mod._repo, "create_project", uncertain_create)
    monkeypatch.setattr(
        server_mod._repo, "project_intent_status",
        lambda *_args, **_kwargs: {"status": "repair_required"},
    )

    async def steps(client):
        registered = await client.post(
            "/api/auth/register",
            json={"username": "alice", "password": "alice-pass-1"},
        )
        token = registered.json()["token"]
        intent = _intent("uncertain-create")
        target = "p-uncertain-create"
        response = await client.post(
            "/api/projects",
            json={"name": "未知结果", "project_id": target, "intent_id": intent},
            headers={
                **_auth(token),
                "Idempotency-Key": "uncertain-create-key",
            },
        )
        assert response.status_code == 500
        error = response.json()["error"]
        assert error["mutation_outcome"] == "unknown"
        assert error["context"] == {
            "proof_status": "repair_required",
            "intent_id": intent,
            "kind": "create",
            "target_id": target,
            "mutation_outcome": "unknown",
        }

        definitive = await client.post(
            "/api/projects",
            json={"name": "缺字段"},
            headers={**_auth(token), "Idempotency-Key": "definitive-invalid"},
        )
        assert definitive.status_code == 422
        assert definitive.json()["error"]["mutation_outcome"] == "unknown"

    _run_scenario(app, steps)


def test_endpoint_delete_project_idempotency_replays_after_project_removed(tmp_path, monkeypatch):
    app = _load_app(tmp_path, monkeypatch)

    async def steps(client):
        r = await client.post("/api/auth/register",
                              json={"username": "alice", "password": "alice-pass-1"})
        token = r.json()["token"]
        create_body, extra = _create_request("delete-idempotent-source", name="要删除")
        r = await client.post("/api/projects", json=create_body,
                              headers={**_auth(token), **extra})
        pid = r.json()["project_id"]
        headers = {**_auth(token), "Idempotency-Key": "project-delete-1"}
        body = {"confirm_name": "要删除", "intent_id": _intent("project-delete-idempotent")}

        r1 = await client.request("DELETE", f"/api/projects/{pid}", json=body, headers=headers)
        r2 = await client.request("DELETE", f"/api/projects/{pid}", json=body, headers=headers)
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r2.json() == r1.json()
        assert r1.json()["kind"] == "delete"
        assert r1.json()["generation"] == r1.json()["project_generation"] == 2

        # Simulate loss of the HTTP replay ledger after the durable topology
        # outcome exists.  project_not_found on re-execution must reconcile to
        # committed proof, never be mislabeled not_started.
        import server as server_mod
        from sdk import IdempotencyStore
        server_mod._repo.idempotency_store = IdempotencyStore(
            tmp_path / "lost-delete-ledger.json",
        )
        reconciled_retry = await client.request(
            "DELETE", f"/api/projects/{pid}", json=body, headers=headers,
        )
        assert reconciled_retry.status_code == 404
        error = reconciled_retry.json()["error"]
        assert error["mutation_outcome"] == "committed_proof_via_reconcile"
        assert error["context"]["proof_status"] == "deleted"
        assert error["context"]["proof_generation"] == 2

        proof = await client.get(
            f"/api/projects/{pid}/intents/{body['intent_id']}?kind=delete",
            headers=_auth(token),
        )
        assert proof.status_code == 200
        assert proof.json()["status"] == "deleted"

        r = await client.get("/api/projects", headers=_auth(token))
        assert all(p["id"] != pid for p in r.json()["projects"])

    _run_scenario(app, steps)


def test_endpoint_command_idempotency_replays_without_duplicate_oplog(tmp_path, monkeypatch):
    app = _load_app(tmp_path, monkeypatch)

    async def steps(client):
        r = await client.post("/api/auth/register",
                              json={"username": "alice", "password": "alice-pass-1"})
        token = r.json()["token"]
        create_body, extra = _create_request("command-idempotent-source", name="命令幂等")
        r = await client.post("/api/projects", json=create_body,
                              headers={**_auth(token), **extra})
        pid = r.json()["project_id"]
        payload = {
            "type": "obj",
            "id": "idem-obj-1",
            "name": "幂等对象",
            "data": {"name": "A"},
        }
        headers = {**_auth(token), "Idempotency-Key": "command-add-1"}

        r1 = await client.post(f"/api/command/entity.add?project_id={pid}",
                               json=payload, headers=headers)
        r2 = await client.post(f"/api/command/entity.add?project_id={pid}",
                               json=payload, headers=headers)
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r2.json() == r1.json()
        assert r1.json()["project_generation"] == 1

        r3 = await client.post(f"/api/command/entity.add?project_id={pid}",
                               json={**payload, "id": "idem-obj-2"}, headers=headers)
        assert r3.status_code == 409
        assert r3.json()["error"]["code"] == "idempotency_key_reused"

        r = await client.get(f"/api/history?project_id={pid}", headers=_auth(token))
        entries = [
            e for e in r.json()["entries"]
            if e["op"]["command_id"] == "entity.add"
            and e["op"]["patches"][0]["entity_id"] == "idem-obj-1"
        ]
        assert len(entries) == 1

    _run_scenario(app, steps)


def test_endpoint_update_idempotency_replays_after_cas_version_changes(tmp_path, monkeypatch):
    app = _load_app(tmp_path, monkeypatch)

    async def steps(client):
        r = await client.post("/api/auth/register",
                              json={"username": "alice", "password": "alice-pass-1"})
        token = r.json()["token"]
        create_body, extra = _create_request("cas-idempotent-source", name="CAS 幂等")
        r = await client.post("/api/projects", json=create_body,
                              headers={**_auth(token), **extra})
        pid = r.json()["project_id"]
        add_payload = {
            "type": "obj",
            "id": "cas-obj-1",
            "name": "CAS 对象",
            "data": {"name": "A"},
        }
        r = await client.post(f"/api/command/entity.add?project_id={pid}",
                              json=add_payload, headers=_auth(token))
        assert r.status_code == 200

        update_payload = {
            "type": "obj",
            "id": "cas-obj-1",
            "expected_version": 1,
            "patches": {"name": "B"},
        }
        headers = {**_auth(token), "Idempotency-Key": "command-update-1"}
        r1 = await client.post(f"/api/command/entity.update?project_id={pid}",
                               json=update_payload, headers=headers)
        r2 = await client.post(f"/api/command/entity.update?project_id={pid}",
                               json=update_payload, headers=headers)
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r2.json() == r1.json()

        r3 = await client.post(f"/api/command/entity.update?project_id={pid}",
                               json=update_payload,
                               headers={**_auth(token), "Idempotency-Key": "command-update-2"})
        assert r3.status_code == 409
        assert r3.json()["error"]["code"] == "cas_version_mismatch"

        r = await client.get(f"/api/history?project_id={pid}", headers=_auth(token))
        entries = [
            e for e in r.json()["entries"]
            if e["op"]["command_id"] == "entity.update"
            and e["op"]["patches"][0]["entity_id"] == "cas-obj-1"
        ]
        assert len(entries) == 1

    _run_scenario(app, steps)


def test_endpoint_acl_command_idempotency_replays_after_actor_loses_access(tmp_path, monkeypatch):
    app = _load_app(tmp_path, monkeypatch)

    async def steps(client):
        tokens = {}
        for u in ("alice", "bob"):
            r = await client.post("/api/auth/register",
                                  json={"username": u, "password": f"{u}-pass-1"})
            tokens[u] = r.json()["token"]
        client.cookies.clear()
        create_body, extra = _create_request("acl-idempotent-source", name="ACL 幂等")
        r = await client.post("/api/projects", json=create_body,
                              headers={**_auth(tokens["alice"]), **extra})
        pid = r.json()["project_id"]
        canonical = await client.get(
            f"/api/document?project_id={pid}", headers=_auth(tokens["alice"]),
        )
        payload = {
            "acl": {"owner": "bob", "collaborators": []},
            "expected_version": canonical.json()["meta"]["version"],
        }
        replay_headers = {**_auth(tokens["alice"]), "Idempotency-Key": "acl-transfer-1"}

        r1 = await client.post(f"/api/command/document.set_acl?project_id={pid}",
                               json=payload, headers=replay_headers)
        assert r1.status_code == 200

        # alice 已被移出成员,普通读写应失败。
        r = await client.get(f"/api/document?project_id={pid}", headers=_auth(tokens["alice"]))
        assert r.status_code == 403

        # 但同一个幂等键代表同一次已成功请求,必须先由账本回放。
        r2 = await client.post(f"/api/command/document.set_acl?project_id={pid}",
                               json=payload, headers=replay_headers)
        assert r2.status_code == 200
        assert r2.json() == r1.json()

        # 新 key 是新请求,仍要走当前 ACL 校验。
        r3 = await client.post(f"/api/command/document.set_acl?project_id={pid}",
                               json=payload,
                               headers={**_auth(tokens["alice"]), "Idempotency-Key": "acl-transfer-2"})
        assert r3.status_code == 403

    _run_scenario(app, steps)


def test_asset_upload_enforces_declared_streamed_and_project_limits(tmp_path, monkeypatch):
    monkeypatch.setenv("ZE_HTTP_ASSET_UPLOAD_MAX_BYTES", "4")
    monkeypatch.setenv("ZE_CAPACITY_ASSET_BLOB_SIZE_MAX", "4")
    monkeypatch.setenv("ZE_CAPACITY_DOC_TOTAL_ASSETS_SIZE_MAX", "4")
    app = _load_app(tmp_path, monkeypatch)

    async def steps(client):
        registered = await client.post(
            "/api/auth/register",
            json={"username": "asset-owner", "password": "asset-pass-1"},
        )
        token = registered.json()["token"]
        body, extra = _create_request("asset-capacity", name="资产容量")
        created = await client.post(
            "/api/projects", json=body,
            headers={**_auth(token), **extra},
        )
        pid = created.json()["project_id"]
        headers = {
            **_auth(token),
            "Content-Type": "application/octet-stream",
        }

        declared = await client.post(
            f"/api/asset?project_id={pid}&ext=.bin",
            content=b"12345", headers=headers,
        )
        assert declared.status_code == 413
        assert declared.json()["error"]["code"] == "asset_upload_too_large"

        async def oversized_chunks():
            yield b"12"
            yield b"345"

        streamed = await client.post(
            f"/api/asset?project_id={pid}&ext=.bin",
            content=oversized_chunks(), headers=headers,
        )
        assert streamed.status_code == 413
        assert streamed.json()["error"]["code"] == "asset_upload_too_large"

        first = await client.post(
            f"/api/asset?project_id={pid}&ext=.bin",
            content=b"1234", headers=headers,
        )
        assert first.status_code == 200
        repeated = await client.post(
            f"/api/asset?project_id={pid}&ext=.other",
            content=b"1234", headers=headers,
        )
        assert repeated.status_code == 200
        assert repeated.json()["sha256"] == first.json()["sha256"]
        assert repeated.json()["ext"] == first.json()["ext"] == ".bin"

        over_total = await client.post(
            f"/api/asset?project_id={pid}&ext=.bin",
            content=b"x", headers=headers,
        )
        assert over_total.status_code == 413
        assert over_total.json()["error"]["code"] == "capacity_exceeded"

        sha = first.json()["sha256"]
        direct = await client.get(
            f"/api/asset/{sha}?project_id={pid}", headers=_auth(token),
        )
        assert direct.status_code == 200 and direct.content == b"1234"
        assert "private" in direct.headers["cache-control"]
        assert "no-store" in direct.headers["cache-control"]

        minted = await client.post(
            f"/api/asset-tickets?project_id={pid}",
            json={"sha256s": [sha], "expected_generation": 1},
            headers=_auth(token),
        )
        assert minted.status_code == 200, minted.text
        ticket = minted.json()["tickets"][sha]
        assert ticket.startswith("mt1.")
        assert token not in ticket and "asset-owner" not in ticket

        missing_sha = "f" * 64
        all_missing = await client.post(
            f"/api/asset-tickets?project_id={pid}",
            json={"sha256s": [missing_sha], "expected_generation": 1},
            headers=_auth(token),
        )
        assert all_missing.status_code == 200, all_missing.text
        assert all_missing.json()["tickets"] == {}
        assert all_missing.json()["missing"] == [missing_sha]
        assert all_missing.json()["expires_at"] > 0
        ticket_url = (
            f"/api/asset/{sha}?project_id={pid}&generation=1&ticket={ticket}"
        )

        # A media ticket is sufficient after the login cookie/header are gone;
        # an old main-session query URL is deliberately no longer accepted.
        client.cookies.clear()
        ticketed = await client.get(ticket_url)
        assert ticketed.status_code == 200 and ticketed.content == b"1234"
        assert "private" in ticketed.headers["cache-control"]
        assert "no-store" in ticketed.headers["cache-control"]
        legacy_query = await client.get(
            f"/api/asset/{sha}?project_id={pid}&token={token}",
        )
        assert legacy_query.status_code == 401

        tampered = await client.get(ticket_url[:-1] + ("A" if ticket_url[-1] != "A" else "B"))
        assert tampered.status_code == 403
        assert tampered.json()["error"]["code"] == "media_ticket_invalid"
        wrong_generation = await client.get(ticket_url.replace("generation=1", "generation=2"))
        assert wrong_generation.status_code == 403
        assert wrong_generation.json()["error"]["code"] == "media_ticket_invalid"

        # Durable ACL is authoritative even if this worker's Context still has
        # the old owner cached. Revocation invalidates an otherwise valid ticket.
        doc_path = tmp_path / "data" / "projects" / pid / "document.json"
        durable = json.loads(doc_path.read_text())
        durable["acl"] = {"owner": "someone-else", "collaborators": []}
        doc_path.write_text(json.dumps(durable))
        revoked = await client.get(ticket_url)
        assert revoked.status_code == 403
        assert revoked.json()["error"]["code"] == "acl_denied"

    _run_scenario(app, steps)


def test_asset_get_serves_byte_ranges_for_media_seek(tmp_path, monkeypatch):
    """<video> seek 依赖 Range:无 Range 返 200+Accept-Ranges,单段返 206,越界返 416。"""
    app = _load_app(tmp_path, monkeypatch)

    async def steps(client):
        registered = await client.post(
            "/api/auth/register",
            json={"username": "range-owner", "password": "range-pass-1"},
        )
        token = registered.json()["token"]
        body, extra = _create_request("asset-range", name="Range 资产")
        created = await client.post(
            "/api/projects", json=body, headers={**_auth(token), **extra},
        )
        pid = created.json()["project_id"]
        blob = b"0123456789abcdef"
        uploaded = await client.post(
            f"/api/asset?project_id={pid}&ext=.mp4",
            content=blob, headers={**_auth(token), "Content-Type": "video/mp4"},
        )
        assert uploaded.status_code == 200, uploaded.text
        sha = uploaded.json()["sha256"]
        url = f"/api/asset/{sha}?project_id={pid}"

        full = await client.get(url, headers=_auth(token))
        assert full.status_code == 200 and full.content == blob
        assert full.headers["accept-ranges"] == "bytes"

        prefix = await client.get(url, headers={**_auth(token), "Range": "bytes=0-"})
        assert prefix.status_code == 206 and prefix.content == blob
        assert prefix.headers["content-range"] == "bytes 0-15/16"
        assert prefix.headers["accept-ranges"] == "bytes"
        assert prefix.headers["content-type"] == "video/mp4"

        middle = await client.get(url, headers={**_auth(token), "Range": "bytes=4-7"})
        assert middle.status_code == 206 and middle.content == b"4567"
        assert middle.headers["content-range"] == "bytes 4-7/16"
        assert middle.headers["content-length"] == "4"

        suffix = await client.get(url, headers={**_auth(token), "Range": "bytes=-4"})
        assert suffix.status_code == 206 and suffix.content == b"cdef"
        assert suffix.headers["content-range"] == "bytes 12-15/16"

        clamped = await client.get(url, headers={**_auth(token), "Range": "bytes=8-999"})
        assert clamped.status_code == 206 and clamped.content == b"89abcdef"
        assert clamped.headers["content-range"] == "bytes 8-15/16"

        beyond = await client.get(url, headers={**_auth(token), "Range": "bytes=16-"})
        assert beyond.status_code == 416
        assert beyond.headers["content-range"] == "bytes */16"

        # 不可解析/倒序/非 bytes 单位/多段:按 RFC 7233 忽略 Range,整体 200
        for ignored in ("bytes=abc", "bytes=5-2", "items=0-1", "bytes=0-1,4-5", "bytes=-0"):
            resp = await client.get(url, headers={**_auth(token), "Range": ignored})
            if ignored == "bytes=-0":
                assert resp.status_code == 416, ignored   # 空 suffix 语法合法但不可满足
            else:
                assert resp.status_code == 200 and resp.content == blob, ignored

        # media_ticket 鉴权路径同样透传 Range(<video> 实际用 ticket URL 发 Range)
        minted = await client.post(
            f"/api/asset-tickets?project_id={pid}",
            json={"sha256s": [sha], "expected_generation": 1},
            headers=_auth(token),
        )
        ticket = minted.json()["tickets"][sha]
        client.cookies.clear()
        ticketed = await client.get(
            f"/api/asset/{sha}?project_id={pid}&generation=1&ticket={ticket}",
            headers={"Range": "bytes=4-7"},
        )
        assert ticketed.status_code == 206 and ticketed.content == b"4567"
        assert ticketed.headers["content-range"] == "bytes 4-7/16"

    _run_scenario(app, steps)
