"""CapabilityLane 持久化测试(§3.4:用户全局能力泳道)。"""
from __future__ import annotations
from sdk.hub.capability_lane_store import CapabilityLaneStore


def _s(tmp_path):
    return CapabilityLaneStore(tmp_path / "lanes.json")


def test_set_get_roundtrip_ordered(tmp_path):
    s = _s(tmp_path)
    s.set("u1", "image", ["gemini3", "fal-gpt-image-2"])
    assert s.get("u1", "image") == ("gemini3", "fal-gpt-image-2")   # 有序
    assert _s(tmp_path).get("u1", "image") == ("gemini3", "fal-gpt-image-2")  # 跨重启


def test_dedup_preserves_order(tmp_path):
    s = _s(tmp_path)
    s.set("u1", "image", ["a", "b", "a", "c", "b"])
    assert s.get("u1", "image") == ("a", "b", "c")


def test_scoped_by_user_capability(tmp_path):
    s = _s(tmp_path)
    s.set("u1", "image", ["a"])
    s.set("u2", "image", ["b"])
    s.set("u1", "video", ["v"])
    assert s.get("u1", "image") == ("a",) and s.get("u2", "image") == ("b",)
    assert s.get("u1", "video") == ("v",) and s.get("u1", "audio") == ()


def test_empty_set_and_clear(tmp_path):
    s = _s(tmp_path)
    s.set("u1", "image", ["a"])
    s.set("u1", "image", [])                # 空 = clear
    assert s.get("u1", "image") == ()
    s.set("u1", "image", ["a"])
    s.clear("u1", "image")
    assert s.get("u1", "image") == ()


def test_guest_never_persists(tmp_path):
    s = _s(tmp_path)
    s.set("", "image", ["a"])
    s.set("__demo", "image", ["a"])
    assert s.get("", "image") == () and s.get("__demo", "image") == ()
    assert s.list_for_user("__demo") == {}


def test_list_and_corrupt_tolerated(tmp_path):
    (tmp_path / "lanes.json").write_text("{bad", encoding="utf-8")
    s = _s(tmp_path)
    assert s.get("u1", "image") == ()       # 坏 JSON → 空
    s.set("u1", "image", ["a", "b"])
    assert s.list_for_user("u1") == {"image": ["a", "b"]}
