"""Asset 可达性与 builtin entity 命令的事务边界。

W05 后 command producer 只改 Document 并产 Operation，绝不 add_ref/release 写
AssetStore meta。add/batch/update/import/duplicate/undo/redo/hard_remove 最终都由
完整 Document 多重集扫描统一对账，因而 W04 内存回滚不会留下外部引用账漂移。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ASSET_DIR = (Path(__file__).resolve().parent.parent.parent
              / "01-data-structure" / "03-asset")
if str(_ASSET_DIR) not in sys.path:
    sys.path.insert(0, str(_ASSET_DIR))

from asset import AssetRef, AssetStore
from builtin_commands import _execute_entity_add, register_builtin_commands
from command import CommandRegistry, CommandSpec
from document import Document, _HISTORY_REPLAY_CAPABILITY
from entity import Actor


def _actor() -> Actor:
    return Actor(user_id="u1")


def _doc() -> Document:
    return Document(schema_version="1.0.0", collaboration_mode="with_save")


def _reg_with_store(tmp_path: Path) -> tuple[CommandRegistry, AssetStore]:
    registry = CommandRegistry()
    register_builtin_commands(registry)
    store = AssetStore(tmp_path / "assets")
    # bootstrap 仍挂载 store 供读端点/维护任务消费；producer 不得据此写 meta。
    registry.asset_store = store  # type: ignore[attr-defined]
    return registry, store


def _put(store: AssetStore, data: bytes) -> AssetRef:
    return store.put(data, mime="image/png", ext=".png")


def _dispatch(
    registry: CommandRegistry,
    command_id: str,
    args: dict,
    doc: Document,
    *,
    source: str = "user",
):
    return registry.dispatch(command_id, args, actor=_actor(), source=source, doc=doc)


def _meta_snapshot(store: AssetStore) -> dict[str, tuple[bytes, int, int]]:
    """内容 + inode + mtime；即便重写同内容也能被检测。"""
    out: dict[str, tuple[bytes, int, int]] = {}
    for path in sorted(store.meta_dir.glob("*.json")):
        stat = path.stat()
        out[path.name] = (path.read_bytes(), stat.st_ino, stat.st_mtime_ns)
    return out


def test_all_builtin_entity_producers_leave_asset_meta_untouched(tmp_path: Path):
    """add/batch/update/hard_remove 只改 Document，直到显式 reconcile 才写派生观测。"""
    registry, store = _reg_with_store(tmp_path)
    doc = _doc()
    ref_a = _put(store, b"asset-a")
    ref_b = _put(store, b"asset-b")
    before = _meta_snapshot(store)

    _dispatch(registry, "entity.add", {
        "type": "img", "id": "e1", "data": {"pic": ref_a.to_dict()},
    }, doc)
    _dispatch(registry, "entity.batch_add", {"items": [
        {"type": "img", "id": "e2", "data": {
            "pic": ref_a.to_dict(), "thumbnail": ref_a.to_dict(),
        }},
        {"type": "img", "id": "e3", "data": {"pic": ref_b.to_dict()}},
    ]}, doc)
    _dispatch(registry, "entity.update", {
        "type": "img", "id": "e1", "patches": {"pic": ref_b.to_dict()},
        "expected_version": doc.get("img", "e1").version,
    }, doc)
    _dispatch(registry, "entity.hard_remove", {"type": "img", "id": "e3"}, doc)

    assert _meta_snapshot(store) == before
    assert store.ref_count(ref_a) == 0
    assert store.ref_count(ref_b) == 0

    # final Document: e2 对 A 两槽，e1 对 B 一槽。
    assert store.reconcile_document_refs(doc) == {
        ref_a.sha256: 2,
        ref_b.sha256: 1,
    }


def test_w04_handler_rollback_cannot_drift_asset_meta(tmp_path: Path):
    """handler 先 add entity 再抛错：Document 原位回滚，外部 meta 从未写过。"""
    # Fault injection is fixed at registration time.  Mutating a registered
    # public CommandSpec.execute used to work only because that mutable object
    # was also the execution authority; the sealed registration snapshot now
    # deliberately closes that production bypass.
    registry = CommandRegistry()

    def mutate_then_fail(args, actor, source, doc):
        _execute_entity_add(args, actor, source, doc)
        raise RuntimeError("fail after Document mutation")

    registry.register(CommandSpec(
        id="entity.add",
        label="fault-injected entity add",
        execute=mutate_then_fail,
    ))
    store = AssetStore(tmp_path / "assets")
    registry.asset_store = store  # type: ignore[attr-defined]
    doc = _doc()
    ref = _put(store, b"rollback")
    before = _meta_snapshot(store)
    with pytest.raises(RuntimeError, match="fail after"):
        _dispatch(registry, "entity.add", {
            "type": "img", "id": "rolled-back", "data": {"pic": ref.to_dict()},
        }, doc)

    assert doc.find("img", "rolled-back") is None
    assert _meta_snapshot(store) == before
    assert store.ref_count(ref) == 0


def test_reconcile_follows_undo_redo_without_special_asset_hooks(tmp_path: Path):
    """undo/redo 走 direct patches 也会按 Document 终态收敛，不需反向计数接线。"""
    registry, store = _reg_with_store(tmp_path)
    doc = _doc()
    ref = _put(store, b"undo-redo")
    operation = _dispatch(registry, "entity.add", {
        "type": "img", "id": "e1", "data": {"pic": ref.to_dict()},
    }, doc)
    assert store.reconcile_document_refs(doc)[ref.sha256] == 1

    before_undo = _meta_snapshot(store)
    doc.apply_patches_atomically(
        operation.reverse_patches,
        actor=_actor(),
        _replay_capability=_HISTORY_REPLAY_CAPABILITY,
    )
    assert doc.find("img", "e1") is None
    assert _meta_snapshot(store) == before_undo  # direct undo 也不写文件
    assert store.reconcile_document_refs(doc)[ref.sha256] == 0

    before_redo = _meta_snapshot(store)
    doc.apply_patches_atomically(
        operation.patches,
        actor=_actor(),
        _replay_capability=_HISTORY_REPLAY_CAPABILITY,
    )
    assert doc.find("img", "e1") is not None
    assert _meta_snapshot(store) == before_redo
    assert store.reconcile_document_refs(doc)[ref.sha256] == 1


def test_soft_delete_remains_reachable_and_gc_safe(tmp_path: Path):
    """软删仍可 restore，所以 reconcile 与 GC 必须保留它引用的 blob。"""
    registry, store = _reg_with_store(tmp_path)
    doc = _doc()
    ref = _put(store, b"soft-delete")
    _dispatch(registry, "entity.add", {
        "type": "img", "id": "e1", "data": {"pic": ref.to_dict()},
    }, doc)
    _dispatch(registry, "entity.delete", {
        "type": "img", "id": "e1", "expected_version": doc.get("img", "e1").version,
    }, doc)

    assert store.reconcile_document_refs(doc)[ref.sha256] == 1
    assert store.gc(doc) == []
    assert store.has(ref)


def test_external_or_shared_sha_does_not_require_local_meta(tmp_path: Path):
    """Document 可引用非本地 sha；本地 reconcile 不凭空创建其生命周期记录。"""
    registry, store = _reg_with_store(tmp_path)
    doc = _doc()
    fake = {"sha256": "a" * 64, "mime": "image/png", "size": 1, "ext": ".png"}

    operation = _dispatch(registry, "entity.add", {
        "type": "img", "id": "e1", "data": {"pic": fake},
    }, doc)

    assert operation is not None
    assert doc.find("img", "e1") is not None
    assert store.reconcile_document_refs(doc) == {}
    assert store.ref_count(AssetRef.from_dict(fake)) == 0
