"""test_entity_dotpath.py — entity.update 的 dot-path 嵌套补丁(P2 决定1).

设计铁律:严格加性 —— 顶层 key(无 ".")行为与改造前逐字节一致;只有含 "." 的 key
走嵌套不可变写。校验对受影响的顶层字段做(dot-path 子键改触发其顶层字段的完整 schema 校验)。
"""

from __future__ import annotations

import pytest

from entity import Actor, Entity, ZeError


def _actor() -> Actor:
    return Actor(user_id="u1")


# ============================================================ 嵌套写
def test_update_dotpath_single():
    e = Entity(type="t", data={"transform2d": {"x": 1, "y": 2}})
    e.update({"transform2d.x": 100}, expected_version=1, actor=_actor())
    assert e.data["transform2d"] == {"x": 100, "y": 2}
    assert e.version == 2


def test_update_dotpath_multi_fold_same_parent():
    """同一 update 里两条同前缀 dot-path 落到同一个新 parent 对象。"""
    e = Entity(type="t", data={"t": {"x": 0, "y": 0, "z": 9}})
    e.update({"t.x": 10, "t.y": 20}, expected_version=1, actor=_actor())
    assert e.data["t"] == {"x": 10, "y": 20, "z": 9}


def test_update_dotpath_creates_missing_layers():
    e = Entity(type="t", data={})
    e.update({"a.b.c": 5}, expected_version=1, actor=_actor())
    assert e.data == {"a": {"b": {"c": 5}}}


def test_update_dotpath_immutable_does_not_mutate_old():
    e = Entity(type="t", data={"t": {"x": 1, "y": 2}})
    old_t = e.data["t"]  # data property 返回 copy,但内部 _data["t"] 是原对象
    # 直接抓内部引用验证 copy-on-write:更新后内部 t 应是新对象、旧对象不被改
    inner_before = e._data["t"]
    e.update({"t.x": 99}, expected_version=1, actor=_actor())
    assert inner_before == {"x": 1, "y": 2}      # 旧 parent 对象未被原地改
    assert e._data["t"] is not inner_before       # 是新对象
    assert old_t == {"x": 1, "y": 2}


def test_update_dotpath_mixed_with_toplevel():
    e = Entity(type="t", data={"color": "#000", "t": {"x": 1}})
    e.update({"color": "#fff", "t.x": 7}, expected_version=1, actor=_actor())
    assert e.data == {"color": "#fff", "t": {"x": 7}}


# ============================================================ 校验(受影响顶层字段)
def test_update_dotpath_validates_affected_toplevel_field():
    """dot-path 子键改 → validator 收到整个顶层字段(含改后子值),不再静默跳过。"""
    seen: dict = {}

    def v(data, schema, path):
        seen.clear()
        seen.update(data)
        t = data.get("transform2d")
        if t is not None and not isinstance(t.get("x"), (int, float)):
            raise ZeError("validation_failed", "x must be number", context={})

    e = Entity(
        type="t",
        data={"transform2d": {"x": 1, "y": 2}},
        schema={"properties": {"transform2d": {"type": "object"}}},
        validator=v,
    )
    # 合法:validator 看到整个 transform2d(x 已是 100)
    e.update({"transform2d.x": 100}, expected_version=1, actor=_actor())
    assert seen.get("transform2d") == {"x": 100, "y": 2}
    # 非法:校验抛 → _data 不变、版本不 bump(原子性)
    with pytest.raises(ZeError):
        e.update({"transform2d.x": "bad"}, expected_version=2, actor=_actor())
    assert e.data["transform2d"]["x"] == 100
    assert e.version == 2


# ============================================================ 顶层加性回归(必须与改前一致)
def test_update_toplevel_unchanged_regression():
    e = Entity(type="t", data={"a": 1, "b": 2})
    e.update({"b": 22, "c": 33}, expected_version=1, actor=_actor())
    assert e.data == {"a": 1, "b": 22, "c": 33}
    assert e.version == 2
