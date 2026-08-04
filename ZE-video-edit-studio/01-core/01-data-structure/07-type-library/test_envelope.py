"""test_envelope — GATE-TYPE-08(信封 round-trip 保真 + 五步导入 normative 全谱)。

覆盖:
  round-trip 逐字段保真 + sha 集相等 + zip 布局与 AssetStore 同构 + 裸 manifest 合法
  版本闸 / blob 篡改 / 未声明类型 / 单例锚守恒 / id 冲突重映射 + $ref/deps 重写 /
  包外悬挂引用置 null / rename 桶名映射(Pilot B 形状) / layers 域展开折回 /
  导入单 Operation 一键 undo / 导出 pending Operation + 提交后确定性落盘 / 路径能力隔离
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_DS = _HERE.parent
for _d in (_HERE, _DS / "04-field-types", _DS / "01-document", _DS / "03-asset",
           _DS.parent / "03-commands-operations" / "01-command",
           _DS.parent / "03-commands-operations" / "02-operation",
           _DS.parent / "02-data-flow" / "07-persistence"):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

import envelope as env  # noqa: E402
import type_library as zt  # noqa: E402
from asset import AssetStore  # noqa: E402
from command import CommandMutation  # noqa: E402
from commit_contracts import CommitStatus, DurabilityState, EventStatus  # noqa: E402
from commit_coordinator import CommitCoordinator  # noqa: E402
from document import (  # noqa: E402
    DocumentRegistry,
    ReadOnlySourceSpec,
    _HISTORY_REPLAY_CAPABILITY,
)
from entity import Actor, Entity, ZeError  # noqa: E402
from file_store import FileCommitStore  # noqa: E402
from operation import OpLog  # noqa: E402
from ports import ActionStatus, WriteOutcomeError  # noqa: E402

ACTOR = Actor(user_id="u1")


def _make_doc(preset: str, **kw):
    reg = DocumentRegistry()
    zt.apply_preset(reg, preset, **kw)
    return reg.build_document(schema_version="1.0.0", name=f"{preset} 档")


def _scene_doc(store: AssetStore):
    doc = _make_doc("scene")
    bg = store.put(b"PNG-BG", ext=".png")
    obj_img = store.put(b"PNG-APPLE", ext=".png")
    doc.add(Entity(type="scene", name="关卡 1", id="s1",
                   data={"width": 1920, "height": 1080, "status": "draft",
                         "background": {"sha256": bg.sha256, "ext": ".png"}}))
    doc.add(Entity(type="placed_object", name="苹果", id="p1",
                   data={"x": 10, "y": 20, "w": 50, "h": 60, "label": "apple",
                         "media": {"sha256": obj_img.sha256, "ext": ".png"}}))
    doc.add(Entity(type="placed_object", name="梨", id="p2",
                   data={"x": 100, "y": 200, "w": 40, "h": 40, "label": "pear"}))
    return doc


# ==============================================================
# round-trip 保真
# ==============================================================

def test_round_trip_field_fidelity_and_sha_set(tmp_path):
    src_store = AssetStore(tmp_path / "src")
    doc = _scene_doc(src_store)
    manifest, blobs = env.export_manifest(doc, src_store, skill_id="a", core_version="1.24.0")
    assert manifest["schema"] == "ze.manifest.scene.v1"
    assert {a["sha256"] for a in manifest["assets"]} == set(blobs)
    assert len(manifest["entities"]) == 3

    dst_store = AssetStore(tmp_path / "dst")
    doc2 = _make_doc("scene")
    created, warnings = env.import_manifest(doc2, dst_store, manifest, blobs)
    assert warnings == []
    assert len(created) == 3
    # 逐字段保真(目标空档无 id 冲突 → id/name/data 全等)
    for ent in doc.list("placed_object"):
        got = doc2.get("placed_object", ent.id)
        assert got.data == ent.data and got.name == ent.name
    assert doc2.get("scene", "s1").data == doc.get("scene", "s1").data
    # sha 集相等且内容可读
    for sha, data in blobs.items():
        from asset import AssetRef
        assert dst_store.get(AssetRef(sha256=sha, ext=".png")) == data


def test_pack_zip_layout_mirrors_asset_store(tmp_path):
    store = AssetStore(tmp_path / "s")
    doc = _scene_doc(store)
    manifest, blobs = env.export_manifest(doc, store)
    pack = env.write_pack(manifest, blobs, tmp_path / "out.ze-pack")
    with zipfile.ZipFile(pack) as zf:
        names = set(zf.namelist())
    assert "manifest.json" in names
    for sha in blobs:
        assert f"blobs/{sha[:2]}/{sha}.png" in names
    # read_pack 还原
    manifest2, blobs2 = env.read_pack(pack)
    assert manifest2["schema"] == manifest["schema"]
    assert blobs2 == blobs


def test_bare_manifest_json_is_legal(tmp_path):
    import json
    doc = _scene_doc(AssetStore(tmp_path / "s"))
    # 无资产实体也能走裸 JSON
    manifest, _ = env.export_manifest(doc, AssetStore(tmp_path / "s2"))
    raw = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
    m2, blobs = env.read_pack(raw)
    assert m2["schema"] == "ze.manifest.scene.v1" and blobs == {}


def test_export_skips_read_only_and_deleted(tmp_path):
    store = AssetStore(tmp_path / "s")
    doc = _scene_doc(store)
    doc.add(Entity(type="placed_object", name="只读", id="ro1", read_only=True,
                   data={"x": 0, "y": 0, "w": 1, "h": 1}))
    doc.get("placed_object", "p2").delete(expected_version=1, actor=ACTOR)
    manifest, _ = env.export_manifest(doc, store)
    ids = {e["id"] for e in manifest["entities"]}
    assert "ro1" not in ids and "p2" not in ids and "p1" in ids


# ==============================================================
# 五步 normative:闸 / blob / 重映射 / 重写 / 校验
# ==============================================================

def test_version_gate_rejects_structurally(tmp_path):
    doc = _make_doc("scene")
    store = AssetStore(tmp_path / "s")
    with pytest.raises(ZeError) as ei:
        env.import_manifest(doc, store, {"schema": "ze.manifest.scene.v2", "entities": []})
    assert ei.value.code == "manifest_version_unsupported"
    with pytest.raises(ZeError) as ei2:
        env.import_manifest(doc, store, {"schema": "bogus"})
    assert ei2.value.code == "manifest_malformed"


def test_tampered_blob_rejected(tmp_path):
    store = AssetStore(tmp_path / "s")
    doc = _scene_doc(store)
    manifest, blobs = env.export_manifest(doc, store)
    sha = next(iter(blobs))
    blobs[sha] = b"TAMPERED"
    doc2 = _make_doc("scene")
    with pytest.raises(ZeError) as ei:
        env.import_manifest(doc2, AssetStore(tmp_path / "d"), manifest, blobs)
    assert ei.value.code == "manifest_blob_corrupt"


def test_undeclared_type_rejected(tmp_path):
    store = AssetStore(tmp_path / "s")
    doc = _scene_doc(store)
    manifest, blobs = env.export_manifest(doc, store)
    doc2 = _make_doc("node_graph")   # 没声明 scene/placed_object
    with pytest.raises(ZeError) as ei:
        env.import_manifest(doc2, AssetStore(tmp_path / "d"), manifest, blobs)
    assert ei.value.code == "manifest_type_undeclared"


def test_singleton_anchor_conservation(tmp_path):
    store = AssetStore(tmp_path / "s")
    doc = _scene_doc(store)
    manifest, blobs = env.export_manifest(doc, store)
    doc2 = _make_doc("scene")
    env.import_manifest(doc2, AssetStore(tmp_path / "d"), manifest, blobs)
    with pytest.raises(ZeError) as ei:   # 第二次导入:scene 锚 1+1>1
        env.import_manifest(doc2, AssetStore(tmp_path / "d"), manifest, blobs)
    assert ei.value.code == "manifest_singleton_conflict"


def test_id_conflict_remap_rewrites_entity_refs(tmp_path):
    """review_queue 无单例锚:同包导两次,第二次 id 换新且 review.target $ref 跟着重写。"""
    store = AssetStore(tmp_path / "s")
    doc = _make_doc("review_queue")
    doc.add(Entity(type="asset_item", name="图", id="a1",
                   data={"media": {"sha256": store.put(b"IMG").sha256}, "kind": "image"}))
    doc.add(Entity(type="review", name="审", id="r1",
                   data={"target": {"$ref": "asset_item/a1"}, "status": "approved"}))
    manifest, blobs = env.export_manifest(doc, store, domain="vision")

    doc2 = _make_doc("review_queue")
    dst = AssetStore(tmp_path / "d")
    first, _ = env.import_manifest(doc2, dst, manifest, blobs)
    second, _ = env.import_manifest(doc2, dst, manifest, blobs)
    new_asset = next(e for e in second if e.type == "asset_item")
    new_review = next(e for e in second if e.type == "review")
    assert new_asset.id != "a1"
    assert new_review.data["target"] == {"$ref": f"asset_item/{new_asset.id}"}
    # 第一次导入 id 原样保留
    assert {e.id for e in first} == {"a1", "r1"}


def test_dangling_optional_ref_cleared_with_warning(tmp_path):
    """可选 entity_ref(chapter.parent)悬挂 → 键移除 + warning,实体保留。"""
    doc = _make_doc("knowledge")
    doc.add(Entity(type="chapter", name="第一章", id="c1",
                   data={"body": "正文", "parent": {"$ref": "chapter/ghost"}}))
    store = AssetStore(tmp_path / "s")
    manifest, blobs = env.export_manifest(doc, store)
    assert manifest["schema"] == "ze.manifest.text.v1"
    doc2 = _make_doc("knowledge")
    created, warnings = env.import_manifest(doc2, AssetStore(tmp_path / "d"), manifest, blobs)
    assert len(created) == 1
    assert "parent" not in created[0].data            # 清除=键移除(validator 不容裸 null)
    assert created[0].data["body"] == "正文"
    assert any("悬挂" in w for w in warnings)


def test_dangling_required_ref_drops_entity_with_warning(tmp_path):
    """必填 entity_ref(review.target)悬挂 → 置 null 过不了校验 → 实体降级丢弃,导入不拖垮。"""
    doc = _make_doc("review_queue")
    doc.add(Entity(type="review", name="审", id="r1",
                   data={"target": {"$ref": "asset_item/ghost"}, "status": "draft"}))
    doc.add(Entity(type="task", name="活", id="t1", data={"status": "pending"}))
    store = AssetStore(tmp_path / "s")
    manifest, blobs = env.export_manifest(doc, store, domain="vision")
    doc2 = _make_doc("review_queue")
    created, warnings = env.import_manifest(doc2, AssetStore(tmp_path / "d"), manifest, blobs)
    assert {e.id for e in created} == {"t1"}          # review 丢弃,task 照导
    assert any("丢弃" in w for w in warnings)
    assert doc2.find("review", "r1") is None


def test_graph_deps_bare_ids_remapped(tmp_path):
    doc = _make_doc("node_graph")
    doc.add(Entity(type="graph_node", name="n1", id="n1", data={"x": 0, "y": 0}))
    doc.add(Entity(type="graph_node", name="n2", id="n2",
                   data={"x": 10, "y": 0, "deps": ["n1", "outside"]}))
    store = AssetStore(tmp_path / "s")
    manifest, blobs = env.export_manifest(doc, store)
    assert manifest["schema"] == "ze.manifest.graph.v1"   # graph_node 注解 → 第七域 graph
    doc2 = _make_doc("node_graph")
    dst = AssetStore(tmp_path / "d")
    env.import_manifest(doc2, dst, manifest, blobs)
    second, _ = env.import_manifest(doc2, dst, manifest, blobs)   # 触发重映射
    by_name = {e.name: e for e in second}
    assert by_name["n2"].data["deps"][0] == by_name["n1"].id      # 包内 dep 跟随新 id
    assert by_name["n2"].data["deps"][1] == "outside"             # 包外裸 id 原样(deps 非 $ref,语义归 mod)


def test_rename_mapping_pilot_b_shape(tmp_path):
    """Pilot B 形状:源桶 image(asset_item 原型)→ 目标桶 source,一行 rename。"""
    store = AssetStore(tmp_path / "s")
    src_reg = DocumentRegistry()
    src_reg.declare_entity_type(name="image", schema=zt.archetype("asset_item"),
                                collaboration_unit=True)
    src_reg.set_collaboration_mode("with_save")
    src = src_reg.build_document(schema_version="1.0.0", name="清理")
    src.add(Entity(type="image", name="清理后", id="i1",
                   data={"media": {"sha256": store.put(b"CLEAN").sha256}, "kind": "image"}))
    manifest, blobs = env.export_manifest(src, store, domain="scene")

    dst_reg = DocumentRegistry()
    dst_reg.declare_entity_type(name="source", schema=zt.archetype("asset_item"),
                                collaboration_unit=True)
    dst_reg.set_collaboration_mode("with_save")
    dst = dst_reg.build_document(schema_version="1.0.0", name="重建")
    created, _ = env.import_manifest(dst, AssetStore(tmp_path / "d"), manifest, blobs,
                                     rename={"image": "source"})
    assert created[0].type == "source"
    assert dst.get("source", "i1").data["kind"] == "image"


# ==============================================================
# layers 域:内联栈 ⇄ layer 实体 1:1
# ==============================================================

def test_layers_domain_expand_and_fold(tmp_path):
    store = AssetStore(tmp_path / "s")
    doc = _make_doc("layered_image")
    stack = [
        {"id": "L0", "name": "底图", "kind": "base-image", "opacity": 1},
        {"id": "L1", "name": "笔刷", "kind": "raster", "opacity": 0.8},
    ]
    doc.add(Entity(type="scene", name="画布", id="s1",
                   data={"width": 512, "height": 512, "layers": stack, "active_layer": "L1"}))
    manifest, blobs = env.export_manifest(doc, store, domain="layers")
    types = {e["type"] for e in manifest["entities"]}
    assert types == {"scene", "layer"}
    layers = sorted([e for e in manifest["entities"] if e["type"] == "layer"],
                    key=lambda e: e["data"]["order"])
    assert [e["id"] for e in layers] == ["L0", "L1"]
    anchor = next(e for e in manifest["entities"] if e["type"] == "scene")
    assert "layers" not in anchor["data"]

    # 导入:layered_image preset 自 M2 起带 layer 桶 → **直收实体**(不折回内联栈;
    # 折回只对无 layer 桶的 legacy 目标,见 test_legacy_inline_stack_still_folds_on_legacy_target)
    doc2 = _make_doc("layered_image")
    created, warnings = env.import_manifest(doc2, AssetStore(tmp_path / "d"), manifest, blobs)
    got = sorted(doc2.list("layer"), key=lambda e: e.data.get("order", 0))
    assert [x.id for x in got] == ["L0", "L1"]
    assert got[1].data["kind"] == "raster" and got[1].name == "笔刷"
    assert "layers" not in doc2.get("scene", "s1").data


# ==============================================================
# 命令层:单 Operation 一键 undo + 导出提交后边界
# ==============================================================

def _cmd_env(tmp_path):
    from command import CommandRegistry
    store = AssetStore(tmp_path / "store")
    doc = _scene_doc(store)
    reg = CommandRegistry()
    env.register_manifest_commands(reg, asset_store=store,
                                   exports_dir=tmp_path / "exports",
                                   skill_id="t", core_version="1.24.0")
    return reg, doc, store


def test_export_command_zero_patch_with_audit_envelope(tmp_path):
    import re
    reg, doc, store = _cmd_env(tmp_path)
    mutation = reg.dispatch("manifest.export", {}, actor=ACTOR, source="user", doc=doc)
    assert isinstance(mutation, CommandMutation)
    assert len(mutation.operations) == 1 and len(mutation.after_commit) == 1
    op = mutation.operations[0]
    assert op.patches == () and op.reverse_patches == ()
    assert op.external["kind"] == "outbound_api"
    assert op.external["status"] == "pending"
    assert set(op.external["target_ref"]) == {"file", "schema", "entities", "assets"}
    assert str(tmp_path) not in repr(op.external)
    assert "苹果" not in repr(op.external) and "PNG-APPLE" not in repr(op.external)
    fname = op.external["target_ref"]["file"]
    # producer 只冻结 snapshot 和预分配文件名；尚未提交时最终文件绝不能出现。
    assert not (tmp_path / "exports" / fname).exists()
    # 产名与 server _PACK_NAME_RE 白名单同形(uuid6 后缀防同秒覆盖)
    assert re.fullmatch(r"[0-9]{8}-[0-9]{6}-scene-[0-9a-f]{6}\.ze-pack", fname), fname


class _ManifestPublisher:
    def __init__(self, status: EventStatus) -> None:
        self.status = status
        self.payloads = []

    def publish(self, payload):
        self.payloads.append(dict(payload))
        return self.status


def _execute_export(tmp_path, *, event_status=EventStatus.PUBLISHED, producer=None):
    reg, doc, store = _cmd_env(tmp_path)
    op_log = OpLog(tmp_path / "operations.ndjson")
    publisher = _ManifestPublisher(event_status)
    coordinator = CommitCoordinator(
        doc,
        FileCommitStore(tmp_path / "document.json", op_log),
        publisher=publisher,
    )
    captured = {}

    def _producer():
        mutation = producer(reg, doc) if producer is not None else reg.dispatch(
            "manifest.export", {}, actor=ACTOR, source="user", doc=doc,
        )
        captured["mutation"] = mutation
        return mutation

    execution = coordinator.execute(_producer)
    mutation = captured["mutation"]
    op = mutation.operations[0]
    path = tmp_path / "exports" / op.external["target_ref"]["file"]
    return execution, mutation, path, doc, publisher


@pytest.mark.parametrize("event_status", [EventStatus.PUBLISHED, EventStatus.PENDING])
def test_export_writes_only_after_real_commit_and_event_acceptance(tmp_path, event_status):
    execution, mutation, path, doc, publisher = _execute_export(
        tmp_path, event_status=event_status,
    )
    assert execution.receipt.status is CommitStatus.COMMITTED
    assert execution.receipt.event_status is event_status
    assert execution.actions[0].status is ActionStatus.SUCCEEDED
    assert len(publisher.payloads) == 1
    assert path.is_file()
    assert not list(path.parent.glob("*.partial"))

    # snapshot 与 action 都可重放：Document 后续变化不会改变已计划包，同一 action
    # 重跑仍落同一路径、产生逐字节相同内容。
    before = path.read_bytes()
    entity = doc.get("placed_object", "p1")
    entity.update({"label": "提交后的新名字"}, expected_version=entity.version, actor=ACTOR)
    mutation.after_commit[0].run(execution.receipt.commit_id)
    assert path.read_bytes() == before


def test_export_event_failure_keeps_business_commit_but_never_writes_file(tmp_path):
    execution, _, path, _, publisher = _execute_export(
        tmp_path, event_status=EventStatus.FAILED,
    )
    assert execution.receipt.status is CommitStatus.COMMITTED
    assert execution.receipt.event_status is EventStatus.FAILED
    assert execution.actions[0].status is ActionStatus.SKIPPED
    assert len(publisher.payloads) == 1
    assert not path.exists()


def test_export_not_durable_store_receipt_skips_final_file(tmp_path, monkeypatch):
    reg, doc, store = _cmd_env(tmp_path)
    op_log = OpLog(tmp_path / "operations.ndjson")

    def reject_before_write(_self, _path):
        raise WriteOutcomeError(
            "document storage rejected the write",
            outcome=DurabilityState.NOT_DURABLE,
            code="document_write_rejected",
        )

    monkeypatch.setattr(type(doc), "save", reject_before_write)
    captured = {}

    def producer():
        captured["mutation"] = reg.dispatch(
            "manifest.export", {}, actor=ACTOR, source="user", doc=doc,
        )
        return captured["mutation"]

    execution = CommitCoordinator(
        doc,
        FileCommitStore(tmp_path / "document.json", op_log),
        publisher=_ManifestPublisher(EventStatus.PUBLISHED),
    ).execute(producer)
    operation = captured["mutation"].operations[0]
    path = tmp_path / "exports" / operation.external["target_ref"]["file"]
    assert execution.receipt.status is CommitStatus.ABORTED
    assert execution.actions[0].status is ActionStatus.SKIPPED
    assert not path.exists()
    assert len(op_log) == 0


def test_export_action_failure_does_not_rewrite_committed_business_result(tmp_path, monkeypatch):
    def fail_write(*_args, **_kwargs):
        raise OSError("export volume unavailable")

    monkeypatch.setattr(env, "write_pack", fail_write)
    execution, _, path, _, _ = _execute_export(tmp_path)
    assert execution.receipt.status is CommitStatus.COMMITTED
    assert execution.receipt.event_status is EventStatus.PUBLISHED
    assert execution.actions[0].status is ActionStatus.FAILED
    assert execution.actions[0].code == "after_commit_action_failed"
    assert "export volume unavailable" in execution.actions[0].message
    assert not path.exists()


def test_write_pack_port_rejects_direct_producer_effect(tmp_path):
    reg, doc, store = _cmd_env(tmp_path)
    manifest, blobs = env.export_manifest(doc, store)
    output = tmp_path / "forbidden.ze-pack"
    coordinator = CommitCoordinator(
        doc,
        FileCommitStore(tmp_path / "document.json", OpLog(tmp_path / "operations.ndjson")),
    )

    with pytest.raises(ZeError) as ei:
        coordinator.execute(lambda: env.write_pack(manifest, blobs, output))
    assert ei.value.code == "producer_effect_forbidden"
    assert not output.exists()


def test_import_command_single_operation_one_click_undo(tmp_path):
    reg, doc, store = _cmd_env(tmp_path)
    manifest, blobs = env.export_manifest(doc, store)
    pack = env.write_pack(manifest, blobs, tmp_path / "exports" / "source.ze-pack")

    doc2 = _make_doc("scene")
    store2 = AssetStore(tmp_path / "store2")
    reg2 = __import__("command").CommandRegistry()
    env.register_manifest_commands(reg2, asset_store=store2,
                                   exports_dir=tmp_path / "exports2")
    op = reg2.dispatch("manifest.import", {"path": str(pack)},
                       actor=ACTOR, source="system", doc=doc2)
    assert len(op.patches) == 3 and len(op.reverse_patches) == 3
    assert len(doc2.list("placed_object")) == 2
    # 一键 undo:应用对称 reverse → 档归零
    for p in op.reverse_patches:
        doc2.apply_patch_directly(
            p, actor=ACTOR,
            _replay_capability=_HISTORY_REPLAY_CAPABILITY,
        )
    assert doc2.list("placed_object") == [] and doc2.list("scene") == []


def test_import_command_source_acl(tmp_path):
    reg, doc, store = _cmd_env(tmp_path)
    with pytest.raises(ZeError) as ei:
        reg.dispatch("manifest.import", {"path": "/nonexistent"},
                     actor=ACTOR, source="ai", doc=doc)
    assert ei.value.code == "acl_denied"   # sources_allowed=(user,system)


@pytest.mark.parametrize("command_id", ["manifest.preview", "manifest.import"])
@pytest.mark.parametrize("source", ["user", "ai"])
def test_interactive_sources_cannot_read_server_paths(tmp_path, command_id, source):
    reg, doc, store = _cmd_env(tmp_path)
    # manifest.import 的命令 ACL 会更早拒绝 ai；preview 可证明 ai 也命中同一路径能力闸。
    if command_id == "manifest.import" and source == "ai":
        expected = "acl_denied"
    else:
        expected = "manifest_path_forbidden"
    with pytest.raises(ZeError) as ei:
        reg.dispatch(command_id, {"path": "/etc/passwd"},
                     actor=ACTOR, source=source, doc=doc)
    assert ei.value.code == expected


def test_interactive_import_accepts_uploaded_asset_instead_of_server_path(tmp_path):
    reg, doc, store = _cmd_env(tmp_path)
    manifest, blobs = env.export_manifest(doc, store)
    pack = env.write_pack(manifest, blobs, tmp_path / "upload.ze-pack")
    uploaded = store.put(pack.read_bytes(), ext=".ze-pack")

    target = _make_doc("scene")
    op = reg.dispatch(
        "manifest.import",
        {"asset": {"sha256": uploaded.sha256, "ext": ".ze-pack"}},
        actor=ACTOR,
        source="user",
        doc=target,
    )
    assert len(op.patches) == 3


# ==============================================================
# P1 只读源物化(make_pack_loader + materialize_read_only_sources)
# ==============================================================

def _asset_pack(tmp_path, name, items):
    """造一个 asset_item 包(items=[(id,label)]),返回 pack 路径。"""
    store = AssetStore(tmp_path / f"src-{name}")
    reg = DocumentRegistry()
    reg.declare_entity_type(name="asset_item", schema=zt.archetype("asset_item"),
                            collaboration_unit=True)
    reg.set_collaboration_mode("with_save")
    doc = reg.build_document(schema_version="1.0.0", name=name)
    for eid, label in items:
        doc.add(Entity(type="asset_item", name=label, id=eid,
                       data={"media": {"sha256": store.put(label.encode()).sha256},
                             "kind": "image"}))
    manifest, blobs = env.export_manifest(doc, store, domain="scene")
    return env.write_pack(manifest, blobs, tmp_path / name)


def test_pack_loader_file_dir_latest_and_missing(tmp_path):
    d = tmp_path / "exports"
    d.mkdir()
    _asset_pack(tmp_path, "x", [("a1", "旧")]).rename(d / "20260611-000001-scene.ze-pack")
    _asset_pack(tmp_path, "y", [("a2", "新")]).rename(d / "20260611-000002-scene.ze-pack")
    store = AssetStore(tmp_path / "dst")
    # 目录 → 最新一包
    loader = env.make_pack_loader(d, "asset_item", asset_store=store)
    got = loader()
    assert [e["id"] for e in got] == ["a2"]
    # blob 已灌
    sha = got[0]["data"]["media"]["sha256"]
    from asset import AssetRef
    assert store.get(AssetRef(sha256=sha)) == "新".encode()
    # 缺失 → 空视图不炸
    assert env.make_pack_loader(tmp_path / "nope.ze-pack", "asset_item")() == []
    assert env.make_pack_loader(tmp_path / "empty-dir", "asset_item")() == []


def test_materialize_read_only_sources_session_view(tmp_path):
    """声明→物化→只读保护→save/reopen→源更新→重物化刷新(过期项购销)。"""
    pack_dir = tmp_path / "exports"
    pack_dir.mkdir()
    _asset_pack(tmp_path, "v1", [("a1", "甲"), ("a2", "乙")]).rename(
        pack_dir / "20260611-000001-scene.ze-pack")

    reg = DocumentRegistry()
    reg.declare_entity_type(name="asset_item", schema=zt.archetype("asset_item"),
                            collaboration_unit=True)
    reg.set_collaboration_mode("with_save")
    reg.declare_read_only_source(name="对方导出", entity_type="asset_item",
                                 loader_path="exports")
    doc = reg.build_document(schema_version="1.0.0", name="消费端")
    store = AssetStore(tmp_path / "shared")
    done = env.materialize_read_only_sources(doc, reg.read_only_sources,
                                             base_dir=tmp_path, asset_store=store)
    assert done == ["对方导出"]
    ents = doc.list("asset_item")
    assert {e.id for e in ents} == {"a1", "a2"}
    assert all(e.read_only for e in ents)
    with pytest.raises(ZeError) as ei:   # 只读保护由 Document 机器保证
        ents[0].update({"kind": "file"}, expected_version=ents[0].version, actor=ACTOR)
    assert ei.value.code == "read_only_violation"

    # save → reopen(只保留 source 声明,不偷存 overlay)→ 源更新→ 重物化 = 视图跟随
    from document import Document
    doc.save(tmp_path / "proj" / "document.json")
    doc2 = Document.open(tmp_path / "proj" / "document.json")
    assert doc2.list("asset_item") == []                    # read_only 只是会话 overlay
    _asset_pack(tmp_path, "v2", [("a1", "甲改")]).rename(
        pack_dir / "20260611-000002-scene.ze-pack")
    env.materialize_read_only_sources(doc2, reg.read_only_sources,
                                      base_dir=tmp_path, asset_store=store)
    after = doc2.list("asset_item")
    assert {e.id for e in after} == {"a1"}            # a2 过期购销
    assert after[0].name == "甲改" and after[0].read_only


def test_materialize_idempotent_on_same_doc_and_rejects_bucket_conflict(tmp_path):
    """同 doc 实例重物化幂等(unregister 先行);两源同桶 → 结构化拒绝不静默丢数据。"""
    pack_dir = tmp_path / "exports"
    pack_dir.mkdir()
    _asset_pack(tmp_path, "v1", [("a1", "甲")]).rename(
        pack_dir / "20260611-000001-scene.ze-pack")
    reg = DocumentRegistry()
    reg.declare_entity_type(name="asset_item", schema=zt.archetype("asset_item"),
                            collaboration_unit=True)
    reg.set_collaboration_mode("with_save")
    reg.declare_read_only_source(name="源A", entity_type="asset_item", loader_path="exports")
    doc = reg.build_document(schema_version="1.0.0", name="t")
    store = AssetStore(tmp_path / "shared")
    env.materialize_read_only_sources(doc, reg.read_only_sources, base_dir=tmp_path,
                                      asset_store=store)
    # 同一 doc 实例第二次物化:不抛 already-registered,实体不重复
    env.materialize_read_only_sources(doc, reg.read_only_sources, base_dir=tmp_path,
                                      asset_store=store)
    assert {e.id for e in doc.list("asset_item")} == {"a1"}
    assert doc.read_only_sources() == ["源A"]

    # 两源同桶:后者购销会吞前者 → 拒绝
    declared = list(reg.read_only_sources) + [
        {"name": "源B", "entity_type": "asset_item", "loader_path": "exports"}]
    with pytest.raises(ZeError) as ei:
        env.materialize_read_only_sources(doc, declared, base_dir=tmp_path, asset_store=store)
    assert "桶冲突" in str(ei.value)


def test_materialize_purges_soft_deleted_overlay_before_refresh(tmp_path):
    pack_dir = tmp_path / "exports"
    pack_dir.mkdir()
    _asset_pack(tmp_path, "v1", [("same", "新")]).rename(
        pack_dir / "20260611-000001-scene.ze-pack")
    reg = DocumentRegistry()
    reg.declare_entity_type(name="asset_item", schema=zt.archetype("asset_item"),
                            collaboration_unit=True)
    reg.set_collaboration_mode("with_save")
    reg.declare_read_only_source(name="源", entity_type="asset_item", loader_path="exports")
    doc = reg.build_document(schema_version="1.0.0", name="t")
    store = AssetStore(tmp_path / "dst")
    env.materialize_read_only_sources(
        doc, reg.read_only_sources, base_dir=tmp_path, asset_store=store,
    )
    stale = doc.get("asset_item", "same")
    stale._deleted = True

    env.materialize_read_only_sources(
        doc, reg.read_only_sources, base_dir=tmp_path, asset_store=store,
    )

    refreshed = doc.get("asset_item", "same")
    assert refreshed is not stale
    assert refreshed.deleted is False
    assert refreshed.name == "新"


def test_materialize_rolls_back_entire_document_batch_on_late_failure(tmp_path, monkeypatch):
    deepcopy_calls: list[str] = []

    class LoaderTrap:
        def __call__(self):
            return [{"id": "old", "data": {"value": "old"}}]

        def __deepcopy__(self, memo):
            deepcopy_calls.append("loader")
            raise AssertionError("loader deepcopy hook must not execute")

    class ValidatorTrap:
        def __call__(self, data, schema, path):
            return None

        def __deepcopy__(self, memo):
            deepcopy_calls.append("validator")
            raise AssertionError("validator deepcopy hook must not execute")

    reg = DocumentRegistry()
    reg.declare_entity_type(name="a", schema={}, collaboration_unit=True)
    reg.declare_entity_type(name="b", schema={}, collaboration_unit=True)
    reg.set_collaboration_mode("with_save")
    doc = reg.build_document(schema_version="1.0.0", name="t")
    validator = ValidatorTrap()
    canonical = Entity(type="b", id="canonical", data={}, validator=validator)
    doc.add(canonical)
    # Document.add correctly replaces a standalone custom validator with its
    # own (empty-schema) no-op authority.  Inject private corrupt/runtime state
    # only after admission so this rollback test still proves snapshot capture
    # preserves an executable port by identity without calling deepcopy hooks.
    canonical._validator = validator
    loader = LoaderTrap()
    doc.register_read_only_source(ReadOnlySourceSpec(
        name="old-source", entity_type="a", loader=loader,
    ))
    original = doc.get("a", "old")

    def fake_pack_loader(path, entity_type, *, asset_store=None):
        if entity_type == "a":
            return lambda: [{"id": "new", "data": {"value": "new"}}]
        return lambda: ["not-a-dict"]

    monkeypatch.setattr(env, "make_pack_loader", fake_pack_loader)
    declared = [
        {"name": "new-a", "entity_type": "a", "loader_path": "a"},
        {"name": "broken-b", "entity_type": "b", "loader_path": "b"},
    ]

    with pytest.raises(ZeError) as captured:
        env.materialize_read_only_sources(doc, declared, base_dir=tmp_path)

    assert captured.value.context["reason"] == "read_only_source_row_invalid"
    assert doc.get("a", "old") is original
    assert doc.find("a", "new") is None
    assert doc.list("b", include_deleted=True) == [canonical]
    assert doc.read_only_sources() == ["old-source"]
    assert doc._read_only_sources["old-source"].loader is loader
    assert doc.get("b", "canonical")._validator is validator
    assert deepcopy_calls == []


def test_materialize_projects_rollback_failure_without_losing_original_cause(
    tmp_path, monkeypatch,
):
    reg = DocumentRegistry()
    reg.declare_entity_type(name="a", schema={}, collaboration_unit=True)
    reg.set_collaboration_mode("with_save")
    doc = reg.build_document(schema_version="1.0.0", name="t")

    class BrokenSession:
        def rollback(self):
            raise ZeError(
                "mutation_rollback_failed",
                "restore failed",
                context={"restore_error_code": "restore_broken"},
            )

        def commit(self):
            raise AssertionError("failed refresh must not commit")

    monkeypatch.setattr(
        type(doc), "begin_mutation_session", lambda _self: BrokenSession(),
    )
    monkeypatch.setattr(
        env, "make_pack_loader", lambda *args, **kwargs: (lambda: ["invalid-row"]),
    )
    declared = [{"name": "broken", "entity_type": "a", "loader_path": "a"}]

    with pytest.raises(ZeError) as captured:
        env.materialize_read_only_sources(doc, declared, base_dir=tmp_path)

    top = captured.value
    assert top.code == "mutation_rollback_failed"
    assert top.context == {
        "original_error_type": "ZeError",
        "original_error_code": "validation_failed",
        "rollback_error_type": "ZeError",
        "rollback_error_code": "mutation_rollback_failed",
        "restore_error_code": "restore_broken",
    }
    assert isinstance(top.__cause__, ZeError)
    assert top.__cause__.context["reason"] == "read_only_source_row_invalid"


# ==============================================================
# 图层实体化(M2):entity form 为画布主形态,信封双路径 + order 归一
# ==============================================================

def _layered_doc_entity_form(tmp_path):
    """桶感知 layered_image 档(scene 锚 + layer 实体——2026-06-11 后的主形态)。"""
    store = AssetStore(tmp_path / "lsrc")
    reg = DocumentRegistry()
    zt.apply_preset(reg, "layered_image")
    doc = reg.build_document(schema_version="1.0.0", name="画布")
    doc.add(Entity(type="scene", name="画布", id="s1", data={"width": 512, "height": 512}))
    ref = store.put(b"RASTER", ext=".png")
    # order 留重复/空洞(7,7,3),验证导出归一
    doc.add(Entity(type="layer", name="底图", id="L0",
                   data={"kind": "base-image", "order": 3,
                         "assetRef": {"sha256": ref.sha256, "ext": ".png"}}))
    doc.add(Entity(type="layer", name="笔刷", id="L1", data={"kind": "raster", "order": 7}))
    doc.add(Entity(type="layer", name="文字", id="L2", data={"kind": "text", "order": 7, "text": "hi"}))
    return doc, store


def test_layers_entity_form_exports_directly_with_order_normalized(tmp_path):
    doc, store = _layered_doc_entity_form(tmp_path)
    manifest, blobs = env.export_manifest(doc, store, domain="layers")
    layers = sorted([e for e in manifest["entities"] if e["type"] == "layer"],
                    key=lambda e: e["data"]["order"])
    # 实体直出(无折展)+ order 连续整数归一(3,7,7 → 0,1,2;同值按 id 兜底:L1<L2)
    assert [e["id"] for e in layers] == ["L0", "L1", "L2"]
    assert [e["data"]["order"] for e in layers] == [0, 1, 2]
    # 锚不带内联栈
    anchor = next(e for e in manifest["entities"] if e["type"] == "scene")
    assert "layers" not in anchor["data"]


def test_layers_pack_imports_into_bucket_aware_doc_directly(tmp_path):
    doc, store = _layered_doc_entity_form(tmp_path)
    manifest, blobs = env.export_manifest(doc, store, domain="layers")
    reg2 = DocumentRegistry()
    zt.apply_preset(reg2, "layered_image")
    doc2 = reg2.build_document(schema_version="1.0.0", name="目标")
    created, warnings = env.import_manifest(doc2, AssetStore(tmp_path / "ldst"), manifest, blobs)
    assert warnings == []
    assert len(doc2.list("layer")) == 3                       # 直收实体,不折回锚
    assert "layers" not in doc2.get("scene", "s1").data


def test_legacy_inline_stack_still_folds_on_legacy_target(tmp_path):
    """legacy 档(锚上内联栈,无 layer 桶)折展回归:导出展开/导入折回不受 M2 影响。"""
    store = AssetStore(tmp_path / "gsrc")
    reg = DocumentRegistry()
    reg.declare_entity_type(name="scene", schema=zt.archetype("scene"), collaboration_unit=True)
    reg.declare_entity_type(name="asset_item", schema=zt.archetype("asset_item"),
                            collaboration_unit=True)
    reg.set_collaboration_mode("with_save")
    doc = reg.build_document(schema_version="1.0.0", name="旧档")
    doc.add(Entity(type="scene", name="画布", id="s1",
                   data={"width": 256, "height": 256,
                         "layers": [{"id": "a", "kind": "base-image"},
                                    {"id": "b", "kind": "raster"}]}))
    manifest, blobs = env.export_manifest(doc, store, domain="layers")
    assert sorted(e["type"] for e in manifest["entities"]) == ["layer", "layer", "scene"]

    reg2 = DocumentRegistry()
    reg2.declare_entity_type(name="scene", schema=zt.archetype("scene"), collaboration_unit=True)
    reg2.declare_entity_type(name="asset_item", schema=zt.archetype("asset_item"),
                             collaboration_unit=True)
    reg2.set_collaboration_mode("with_save")
    doc2 = reg2.build_document(schema_version="1.0.0", name="旧目标")
    env.import_manifest(doc2, AssetStore(tmp_path / "gdst"), manifest, blobs)
    got = doc2.get("scene", "s1").data["layers"]
    assert [x["id"] for x in got] == ["a", "b"]               # 折回内联栈
