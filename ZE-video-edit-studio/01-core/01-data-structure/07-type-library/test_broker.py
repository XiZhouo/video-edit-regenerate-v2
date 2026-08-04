"""test_broker — GATE-TYPE-10 详尽套件(自描述包协商层:preview proposal + opportunistic import)。

对齐设计 plans/cross-skill-interop-broker-design.md §13:
  L2 整原型精确匹配(asset_item)/ 锚子物丢弃(track·clip foreign_anchor_child)/ 单例锚 undeclared /
  必填 ref 闭包满足时整簇收 / 闭包不满足时整体丢弃且**不半导入**(§3.3 自毁防护)/
  opportunistic 落地恰好 accept 集 + 一键 undo 等价 / 严格 mode 零回归。
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_DS = _HERE.parent
for _d in (_HERE, _DS / "04-field-types", _DS / "01-document", _DS / "03-asset",
           _DS.parent / "03-commands-operations" / "01-command",
           _DS.parent / "03-commands-operations" / "02-operation"):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

import pytest  # noqa: E402

import envelope as env  # noqa: E402
import type_library as zt  # noqa: E402
from asset import AssetStore  # noqa: E402
from document import DocumentRegistry  # noqa: E402
from entity import Entity, ZeError  # noqa: E402


# ---- 构造器 ----

def _timeline_doc(store: AssetStore):
    """一条成片(sequence 锚 + asset_item + track + clip)——典型「异类」源包。"""
    reg = DocumentRegistry()
    zt.apply_preset(reg, "timeline")
    doc = reg.build_document(schema_version="1.0.0", name="成片")
    img = store.put(b"PNG-FRAME", ext=".png")
    doc.add(Entity(type="sequence", name="片", id="seq1",
                   data={"width": 1920, "height": 1080, "fps": 24}))
    doc.add(Entity(type="asset_item", name="素材", id="a1",
                   data={"media": {"sha256": img.sha256, "ext": ".png"}, "kind": "image"}))
    doc.add(Entity(type="track", name="主轨", id="t1", data={"order": 0, "kind": "visual"}))
    doc.add(Entity(type="clip", name="开场", id="c1",
                   data={"track": 0, "start": 0, "duration": 4,
                         "asset": {"$ref": "asset_item/a1"}}))
    return doc


def _anno_target():
    """标注工具目标 doc:声明 asset_item + annotation(无 preset,手 declare)。"""
    reg = DocumentRegistry()
    reg.set_collaboration_mode("with_save")
    reg.declare_entity_type(name="asset_item", schema=zt.archetype("asset_item"),
                            collaboration_unit=True)
    reg.declare_entity_type(name="annotation", schema=zt.archetype("annotation"),
                            collaboration_unit=False)
    return reg.build_document(schema_version="1.0.0", name="标注台")


def _anno_source(store: AssetStore):
    """标注源 doc:asset_item a1 + annotation an1(an1.target 必填 → a1)。"""
    reg = DocumentRegistry()
    reg.set_collaboration_mode("with_save")
    reg.declare_entity_type(name="asset_item", schema=zt.archetype("asset_item"),
                            collaboration_unit=True)
    reg.declare_entity_type(name="annotation", schema=zt.archetype("annotation"),
                            collaboration_unit=False)
    doc = reg.build_document(schema_version="1.0.0", name="标注源")
    img = store.put(b"PNG-IMG", ext=".png")
    doc.add(Entity(type="asset_item", name="图", id="a1",
                   data={"media": {"sha256": img.sha256, "ext": ".png"}, "kind": "image"}))
    doc.add(Entity(type="annotation", name="框", id="an1",
                   data={"target": {"$ref": "asset_item/a1"}, "label": "apple",
                         "bbox_xyxy": [10, 20, 110, 220]}))
    return doc


def _by_reason(drop: list[dict]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for d in drop:
        out.setdefault(d["reason"], set()).add(f'{d["src_bucket"]}/{d["src_id"]}')
    return out


# ==============================================================
# preview:窄 L2 撮合分类
# ==============================================================

def test_preview_timeline_into_annotation_tool(tmp_path):
    """异类源:timeline 包喂标注工具 → asset_item 走 L2,sequence/track/clip 各按理由丢。"""
    store = AssetStore(tmp_path / "s")
    src = _timeline_doc(store)
    manifest, _ = env.export_manifest(src, store)
    assert manifest["schema"] == "ze.manifest.timeline.v1"

    proposal = env.compute_ingest_proposal(manifest, _anno_target())
    assert proposal["level"] == "L2"
    assert [a["src_bucket"] for a in proposal["accept"]] == ["asset_item"]
    assert proposal["accept"][0]["match"] == "archetype_exact"
    assert proposal["accept"][0]["into_bucket"] == "asset_item"

    reasons = _by_reason(proposal["drop"])
    assert reasons.get("foreign_anchor_child") == {"track/t1", "clip/c1"}   # 锚子物(AP-03)
    assert reasons.get("undeclared") == {"sequence/seq1"}                    # 单例锚无对应桶
    assert len(proposal["assets"]) == 1                                      # L0 资产永远列出


def test_preview_unannotated_source_is_L0_only(tmp_path):
    """手写 schema 源(零注解)→ 只 L0,无 L2 accept。"""
    store = AssetStore(tmp_path / "s")
    reg = DocumentRegistry()
    reg.set_collaboration_mode("with_save")
    reg.declare_entity_type(name="image",
                            schema={"type": "object", "properties": {
                                "media": {"type": "asset_ref"}}},
                            collaboration_unit=True)
    src = reg.build_document(schema_version="1.0.0", name="手写")
    img = store.put(b"X", ext=".png")
    src.add(Entity(type="image", name="i", id="i1",
                   data={"media": {"sha256": img.sha256, "ext": ".png"}}))
    manifest, _ = env.export_manifest(src, store, domain="vision")   # 零注解须显式 domain
    proposal = env.compute_ingest_proposal(manifest, _anno_target())
    assert proposal["level"] == "L0"
    assert proposal["accept"] == []
    assert _by_reason(proposal["drop"]).get("unannotated_source") == {"image/i1"}


# ==============================================================
# 必填 ref 闭包(设计 §3.3)
# ==============================================================

def test_required_ref_closure_satisfied(tmp_path):
    """annotation + 其 asset_item target 同包,目标收两者 → 整簇 accept,closure 指向 asset_item。"""
    store = AssetStore(tmp_path / "s")
    src = _anno_source(store)
    manifest, _ = env.export_manifest(src, store)
    proposal = env.compute_ingest_proposal(manifest, _anno_target())
    accepted = {a["src_bucket"] for a in proposal["accept"]}
    assert accepted == {"asset_item", "annotation"}
    an = next(a for a in proposal["accept"] if a["src_bucket"] == "annotation")
    assert an["required_closure"] == ["asset_item/a1"]


def test_required_ref_closure_self_destruct_prevented(tmp_path):
    """目标只声明 annotation(不收 asset_item)→ annotation 必填 target 悬空 → 整体丢弃 required_field_unmet,
    accept 空(只剩 L0),**绝不半导入**(§3.3 自毁防护:预测,不是导入到一半才发现)。"""
    store = AssetStore(tmp_path / "s")
    src = _anno_source(store)
    manifest, _ = env.export_manifest(src, store)

    reg = DocumentRegistry()
    reg.set_collaboration_mode("with_save")
    reg.declare_entity_type(name="annotation", schema=zt.archetype("annotation"),
                            collaboration_unit=False)
    target = reg.build_document(schema_version="1.0.0", name="只标注")

    proposal = env.compute_ingest_proposal(manifest, target)
    assert proposal["level"] == "L0"
    assert proposal["accept"] == []
    reasons = _by_reason(proposal["drop"])
    assert "annotation/an1" in reasons.get("required_field_unmet", set())
    assert "asset_item/a1" in reasons.get("undeclared", set())

    # 真导入也不半落:opportunistic 导入后目标零实体
    dst = AssetStore(tmp_path / "d")
    sub_manifest, sub_blobs = env.filter_manifest_to_proposal(manifest, {}, proposal)
    created, _ = env.import_manifest(target, dst, sub_manifest, sub_blobs)
    assert created == []
    assert target.list("annotation") == []


# ==============================================================
# opportunistic 导入:落地恰好 accept 集
# ==============================================================

def test_opportunistic_import_lands_exactly_accept_set(tmp_path):
    store = AssetStore(tmp_path / "s")
    src = _timeline_doc(store)
    manifest, blobs = env.export_manifest(src, store)
    target = _anno_target()
    proposal = env.compute_ingest_proposal(manifest, target)

    sub_manifest, sub_blobs = env.filter_manifest_to_proposal(manifest, blobs, proposal)
    dst = AssetStore(tmp_path / "d")
    created, warnings = env.import_manifest(target, dst, sub_manifest, sub_blobs)

    # 恰好落 accept 集:1 个 asset_item,别的桶零
    assert {(e.type, e.id) for e in created} == {("asset_item", "a1")}
    assert target.list("asset_item") and not target.list("annotation")
    # 资产入库可读(L0 随 accept 闭包带过来)
    from asset import AssetRef
    sha = proposal["assets"][0]
    assert dst.get(AssetRef(sha256=sha, ext=".png")) == b"PNG-FRAME"


def test_strict_mode_unchanged_regression(tmp_path):
    """严格 mode(默认)行为零回归:timeline 包导入 timeline 目标仍全收 4 实体。"""
    store = AssetStore(tmp_path / "s")
    src = _timeline_doc(store)
    manifest, blobs = env.export_manifest(src, store)
    reg = DocumentRegistry()
    zt.apply_preset(reg, "timeline")
    target = reg.build_document(schema_version="1.0.0", name="成片2")
    dst = AssetStore(tmp_path / "d")
    created, warnings = env.import_manifest(target, dst, manifest, blobs)   # 无 mode → 严格
    assert len(created) == 4
    assert warnings == []


def test_derive_accept_profile_shape():
    prof = env.derive_accept_profile(_anno_target())
    assert set(prof["buckets"]) == {"asset_item", "annotation"}
    ai = prof["buckets"]["asset_item"]
    assert ai["archetype"] == "ze.archetype.asset_item@1"
    assert "media" in ai["required"] and "kind" in ai["required"]
    assert ai["singleton"] is False
    # annotation 必填含 target(ref)+ label
    an = prof["buckets"]["annotation"]
    assert "target" in an["required"] and "label" in an["required"]


# ==============================================================
# P1:harvest_rules(L1 机会式收割)+ 合法性 fail-fast + proposal_for_pack
# ==============================================================

def _scene_with_media(store: AssetStore):
    """场景源:scene 锚 + placed_object(携带 media)—— placed_object 是锚子物(L2不收)但 media 可收割。"""
    reg = DocumentRegistry()
    zt.apply_preset(reg, "scene")
    doc = reg.build_document(schema_version="1.0.0", name="关卡")
    img = store.put(b"OBJIMG", ext=".png")
    doc.add(Entity(type="scene", name="关", id="s1", data={"width": 800, "height": 600}))
    doc.add(Entity(type="placed_object", name="苹果", id="p1",
                   data={"x": 1, "y": 2, "w": 3, "h": 4,
                         "media": {"sha256": img.sha256, "ext": ".png"}}))
    return doc, img


def _asset_target_with_harvest():
    """素材库目标:声明 asset_item + 收割规则「任何 media 角色 → asset_item,补 kind=image」。"""
    reg = DocumentRegistry()
    reg.set_collaboration_mode("with_save")
    reg.declare_entity_type(name="asset_item", schema=zt.archetype("asset_item"),
                            collaboration_unit=True)
    reg.declare_harvest_rule(when_role="ze.field.media@1", into_bucket="asset_item",
                             supply_required={"kind": "image"})
    return reg.build_document(schema_version="1.0.0", name="素材库")


def test_harvest_media_from_foreign_anchor_child(tmp_path):
    """placed_object 是 foreign_anchor_child(L2不收),但携带 media → 被 harvest 收进 asset_item(L1)。"""
    store = AssetStore(tmp_path / "s")
    src, img = _scene_with_media(store)
    manifest, blobs = env.export_manifest(src, store)
    target = _asset_target_with_harvest()

    proposal = env.compute_ingest_proposal(manifest, target)
    harv = [a for a in proposal["accept"] if a["match"] == "harvest_rule"]
    assert len(harv) == 1
    assert harv[0]["src_bucket"] == "placed_object" and harv[0]["into_bucket"] == "asset_item"
    assert proposal["level"] == "L1"   # 无 archetype_exact(源无 asset_item 实体)
    # placed_object 不在 drop(被收割救起);scene 在 drop
    dropped = {f'{d["src_bucket"]}/{d["src_id"]}' for d in proposal["drop"]}
    assert "placed_object/p1" not in dropped and "scene/s1" in dropped

    # opportunistic 导入:落地 1 个 asset_item(media + 补的 kind=image),过 validator,资产可读
    sub_m, sub_b = env.filter_manifest_to_proposal(manifest, blobs, proposal)
    dst = AssetStore(tmp_path / "d")
    created, _ = env.import_manifest(target, dst, sub_m, sub_b)
    assert {(e.type, e.id) for e in created} == {("asset_item", "p1")}
    got = target.get("asset_item", "p1")
    assert got.data["kind"] == "image" and got.data["media"]["sha256"] == img.sha256
    from asset import AssetRef
    assert dst.get(AssetRef(sha256=img.sha256, ext=".png")) == b"OBJIMG"


def test_harvest_rule_illegal_required_ref_rejected():
    """into_bucket 含必填 entity_ref(annotation.target)→ build_document fail-fast(设计§14 修正)。"""
    reg = DocumentRegistry()
    reg.set_collaboration_mode("with_save")
    reg.declare_entity_type(name="annotation", schema=zt.archetype("annotation"),
                            collaboration_unit=False)
    reg.declare_harvest_rule(when_role="ze.field.bbox_xyxy@1", into_bucket="annotation")
    with pytest.raises(ZeError) as ei:
        reg.build_document(schema_version="1.0.0", name="x")
    assert ei.value.code == "validation_failed"


def test_harvest_rule_uncovered_required_rejected():
    """media→asset_item 但不补 kind → 必填未覆盖 → fail-fast。"""
    reg = DocumentRegistry()
    reg.set_collaboration_mode("with_save")
    reg.declare_entity_type(name="asset_item", schema=zt.archetype("asset_item"),
                            collaboration_unit=True)
    reg.declare_harvest_rule(when_role="ze.field.media@1", into_bucket="asset_item")
    with pytest.raises(ZeError) as ei:
        reg.build_document(schema_version="1.0.0", name="x")
    assert ei.value.code == "validation_failed"


def test_harvest_rule_bad_role_and_undeclared_bucket_rejected():
    reg = DocumentRegistry()
    reg.set_collaboration_mode("with_save")
    reg.declare_entity_type(name="asset_item", schema=zt.archetype("asset_item"),
                            collaboration_unit=True)
    reg.declare_harvest_rule(when_role="not-a-role", into_bucket="asset_item",
                             supply_required={"kind": "image"})
    with pytest.raises(ZeError):
        reg.build_document(schema_version="1.0.0", name="x")


def test_harvest_id_collision_with_l2_reids(tmp_path):
    """asset_item/a1(L2)+ placed_object/a1(harvest→asset_item)同 (桶,id) → 收割物换新 id,两者都落地不硬错。"""
    store = AssetStore(tmp_path / "s")
    reg = DocumentRegistry()
    zt.apply_preset(reg, "scene")
    src = reg.build_document(schema_version="1.0.0", name="关")
    img1 = store.put(b"L2IMG", ext=".png")
    img2 = store.put(b"HARVIMG", ext=".png")
    src.add(Entity(type="scene", name="关", id="s1", data={"width": 100, "height": 100}))
    src.add(Entity(type="asset_item", name="图1", id="a1",
                   data={"media": {"sha256": img1.sha256, "ext": ".png"}, "kind": "image"}))
    src.add(Entity(type="placed_object", name="物", id="a1",
                   data={"x": 0, "y": 0, "w": 1, "h": 1,
                         "media": {"sha256": img2.sha256, "ext": ".png"}}))
    manifest, blobs = env.export_manifest(src, store)
    target = _asset_target_with_harvest()
    proposal = env.compute_ingest_proposal(manifest, target)
    assert any(a["match"] == "archetype_exact" and a["src_id"] == "a1" for a in proposal["accept"])
    assert any(a["match"] == "harvest_rule" and a["src_bucket"] == "placed_object"
               for a in proposal["accept"])

    sub_m, sub_b = env.filter_manifest_to_proposal(manifest, blobs, proposal)
    dst = AssetStore(tmp_path / "d")
    created, _ = env.import_manifest(target, dst, sub_m, sub_b)
    assert len(created) == 2 and len(target.list("asset_item")) == 2   # 收割物换 id,不硬错


def test_proposal_for_pack_reads_disk(tmp_path):
    store = AssetStore(tmp_path / "s")
    src = _timeline_doc(store)
    manifest, blobs = env.export_manifest(src, store)
    pack = env.write_pack(manifest, blobs, tmp_path / "x.ze-pack")
    prop = env.proposal_for_pack(pack, _anno_target())
    assert prop["level"] == "L2"
    assert [a["src_bucket"] for a in prop["accept"]] == ["asset_item"]
