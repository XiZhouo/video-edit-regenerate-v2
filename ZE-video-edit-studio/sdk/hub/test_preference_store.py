"""FeaturePreference 持久化测试(§3.4:用户私有默认模型)。"""

from __future__ import annotations

from sdk.hub.preference_store import PreferenceStore


def _store(tmp_path):
    return PreferenceStore(tmp_path / "prefs.json")


def test_set_get_roundtrip_persists(tmp_path):
    s = _store(tmp_path)
    s.set("u1", "foundry.image.generate", "image", "fal-gpt-image-2")
    assert s.get("u1", "foundry.image.generate", "image") == "fal-gpt-image-2"
    # 新实例从盘载回(跨重启)
    assert _store(tmp_path).get("u1", "foundry.image.generate", "image") == "fal-gpt-image-2"


def test_scoped_by_user_feature_capability(tmp_path):
    s = _store(tmp_path)
    s.set("u1", "f1", "image", "gemini3")
    s.set("u2", "f1", "image", "fal-gpt-image-2")     # 他人不串
    s.set("u1", "f1", "video", "veo")                 # 不同能力不串
    assert s.get("u1", "f1", "image") == "gemini3"
    assert s.get("u2", "f1", "image") == "fal-gpt-image-2"
    assert s.get("u1", "f1", "video") == "veo"
    assert s.get("u1", "f2", "image") is None         # 不同 feature 无


def test_clear_and_empty_set_clears(tmp_path):
    s = _store(tmp_path)
    s.set("u1", "f1", "image", "gemini3")
    s.set("u1", "f1", "image", "")                    # 空 = clear
    assert s.get("u1", "f1", "image") is None
    s.set("u1", "f1", "image", "gemini3")
    s.clear("u1", "f1", "image")
    assert s.get("u1", "f1", "image") is None


def test_guest_never_persists(tmp_path):
    s = _store(tmp_path)
    s.set("", "f1", "image", "gemini3")               # 空 uid
    s.set("__demo_x", "f1", "image", "gemini3")        # __ 前缀
    assert s.get("", "f1", "image") is None
    assert s.get("__demo_x", "f1", "image") is None
    assert s.list_for_user("__demo_x") == {}


def test_list_for_user(tmp_path):
    s = _store(tmp_path)
    s.set("u1", "f1", "image", "gemini3")
    s.set("u1", "f1", "video", "veo")
    got = s.list_for_user("u1")
    assert got == {"f1 image": "gemini3", "f1 video": "veo"}
    assert s.list_for_user("nobody") == {}


def test_corrupt_file_tolerated(tmp_path):
    (tmp_path / "prefs.json").write_text("{ broken", encoding="utf-8")
    s = _store(tmp_path)
    assert s.get("u1", "f1", "image") is None          # 坏 JSON → 空,不抛
    s.set("u1", "f1", "image", "gemini3")              # 仍可写(覆盖坏文件)
    assert s.get("u1", "f1", "image") == "gemini3"
