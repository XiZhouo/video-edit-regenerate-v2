"""test_ref_xzeref.py — _default_ref_extractor 的 x-ze-ref 标注边(P2 决定2,正向边).

裸 id 引用字段(spriteId/parentId 等)此前对引用图隐形。给 schema 加 x-ze-ref:<type>
标注后,_default_ref_extractor 把它当正向边 → detect_cycles / cascade_delete 自动覆盖,
**数据形态不变**(仍裸字符串 id)。反向清空(删 X 清空指向 X 的字段)是另一机制,不在此。
"""

from __future__ import annotations

from document import Document
from entity import Actor, Entity


def _doc() -> Document:
    # Once an Entity enters a Document, the Document owns its schema/validator
    # authority.  Reference annotations therefore belong in the declaration,
    # not on a standalone Entity that Document.add must deliberately override.
    return Document(
        schema_version="1.0.0",
        collaboration_mode="with_save",
        schema={"node": _NODE_SCHEMA, "asset": _ASSET_SCHEMA},
    )


def _actor() -> Actor:
    return Actor(user_id="u1")


# scene_node 风格:裸 id 字段 + schema x-ze-ref 标注
_NODE_SCHEMA = {"properties": {"spriteId": {"type": "string", "x-ze-ref": "asset"}}}
_ASSET_SCHEMA = {"properties": {"ownerId": {"type": "string", "x-ze-ref": "node"}}}


def test_xzeref_bare_id_is_visible_to_cycle_detect():
    """裸 id 经 x-ze-ref 标注 → 参与环检测(此前隐形)。"""
    doc = _doc()
    doc.add(Entity(type="node", id="n1", data={"spriteId": "s1"}, schema=_NODE_SCHEMA))
    doc.add(Entity(type="asset", id="s1", data={"ownerId": "n1"}, schema=_ASSET_SCHEMA))
    cycles = doc.detect_cycles()
    assert len(cycles) == 1
    assert "node/n1" in cycles[0] and "asset/s1" in cycles[0]


def test_xzeref_bare_id_participates_in_forward_cascade():
    """删引用方 → 正向级联删被引用的裸 id 目标(与 $ref 同机)。"""
    doc = _doc()
    doc.add(Entity(type="node", id="n1", data={"spriteId": "s1"}, schema=_NODE_SCHEMA))
    doc.add(Entity(type="asset", id="s1", data={}))
    removed = doc.cascade_delete("node", "n1", actor=_actor())
    assert ("node", "n1") in removed
    assert ("asset", "s1") in removed   # 裸 id 引用的目标被正向级联删
    assert doc.find("asset", "s1").deleted is True


def test_xzeref_empty_or_missing_value_no_edge():
    doc = _doc()
    doc.add(Entity(type="node", id="n1", data={"spriteId": ""}, schema=_NODE_SCHEMA))  # 空
    doc.add(Entity(type="node", id="n2", data={}, schema=_NODE_SCHEMA))                # 缺
    doc.add(Entity(type="asset", id="s1", data={}))
    assert doc.detect_cycles() == []
    # 删 n1 不级联(无有效边)
    removed = doc.cascade_delete("node", "n1", actor=_actor())
    assert removed == [("node", "n1")]


def test_xzeref_does_not_break_dollar_ref_scan():
    """加 x-ze-ref 不影响既有 {$ref} 边扫描(无 schema 时仍只走 $ref)。"""
    doc = _doc()
    doc.add(Entity(type="a", id="1", data={"r": {"$ref": "b/2"}}))   # 无 schema
    doc.add(Entity(type="b", id="2", data={"r": {"$ref": "a/1"}}))
    cycles = doc.detect_cycles()
    assert len(cycles) == 1
    assert "a/1" in cycles[0] and "b/2" in cycles[0]


def test_xzeref_and_dollar_ref_coexist_on_same_entity():
    """同一实体既有 $ref 又有 x-ze-ref 裸 id → 两类边都出。"""
    doc = _doc()
    schema = {"properties": {"spriteId": {"type": "string", "x-ze-ref": "asset"}}}
    doc.add(Entity(type="node", id="n1",
                   data={"spriteId": "s1", "meta": {"$ref": "tag/t1"}}, schema=schema))
    doc.add(Entity(type="asset", id="s1", data={}))
    doc.add(Entity(type="tag", id="t1", data={}))
    removed = doc.cascade_delete("node", "n1", actor=_actor())
    assert ("asset", "s1") in removed   # x-ze-ref 边
    assert ("tag", "t1") in removed     # $ref 边


def test_legacy_business_field_named_properties_keeps_real_xzeref_edges():
    """业务字段名碰撞不能把整张 flat field map 误判成 schema 根。"""
    schema = {
        "properties": {"type": "object", "additionalProperties": True},
        "spriteId": {"type": "string", "x-ze-ref": "asset"},
    }
    doc = Document(
        schema_version="1.0.0",
        collaboration_mode="with_save",
        schema={"node": schema, "asset": {}},
    )
    doc.add(Entity(
        type="node",
        id="n1",
        data={"properties": {"business": 1}, "spriteId": "s1"},
    ))
    doc.add(Entity(type="asset", id="s1", data={}))

    canonical = doc.schema["node"]
    assert set(canonical["properties"]) == {"properties", "spriteId"}
    removed = doc.cascade_delete("node", "n1", actor=_actor())
    assert ("asset", "s1") in removed
