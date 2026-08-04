"""DiscoveredModelStore 测试(P4:分层落库、教义①不污染 canonical、访客 no-op、幂等覆盖)。"""

from __future__ import annotations

from sdk.hub import DiscoveredModelStore


def test_record_layers_as_discovered_caps_empty(tmp_path):
    s = DiscoveredModelStore(tmp_path / "disc.json")
    out = s.record("u1", "cred-1", ["gpt-4o", "gpt-4o-mini"], discovered_at=100.0)
    assert [e["model_id"] for e in out] == ["gpt-4o", "gpt-4o-mini"]
    # 教义①:每条 source=discovered + caps=[](满足不了任何 require,不参与自动拾取)
    assert all(e["source"] == "discovered" and e["caps"] == [] for e in out)
    assert all(e["discovered_at"] == 100.0 for e in out)
    assert s.list_for_credential("u1", "cred-1") == out


def test_record_dedups_preserve_order(tmp_path):
    s = DiscoveredModelStore(tmp_path / "disc.json")
    out = s.record("u1", "cred-1", ["a", "b", "a", "c", "b"], discovered_at=1.0)
    assert [e["model_id"] for e in out] == ["a", "b", "c"]


def test_record_replaces_previous_set(tmp_path):
    s = DiscoveredModelStore(tmp_path / "disc.json")
    s.record("u1", "cred-1", ["old1", "old2"], discovered_at=1.0)
    s.record("u1", "cred-1", ["new1"], discovered_at=2.0)          # 新发现覆盖旧
    assert [e["model_id"] for e in s.list_for_credential("u1", "cred-1")] == ["new1"]


def test_empty_record_clears(tmp_path):
    s = DiscoveredModelStore(tmp_path / "disc.json")
    s.record("u1", "cred-1", ["x"], discovered_at=1.0)
    s.record("u1", "cred-1", [], discovered_at=2.0)
    assert s.list_for_credential("u1", "cred-1") == []


def test_per_credential_isolation_and_list_for_user(tmp_path):
    s = DiscoveredModelStore(tmp_path / "disc.json")
    s.record("u1", "cred-1", ["a"], discovered_at=1.0)
    s.record("u1", "cred-2", ["b"], discovered_at=1.0)
    got = s.list_for_user("u1")
    assert set(got) == {"cred-1", "cred-2"}
    assert [e["model_id"] for e in got["cred-1"]] == ["a"]


def test_guest_is_noop(tmp_path):
    s = DiscoveredModelStore(tmp_path / "disc.json")
    assert s.record("", "cred-1", ["a"]) == []
    assert s.record("__demo", "cred-1", ["a"]) == []
    assert s.list_for_user("__demo") == {}
    assert not (tmp_path / "disc.json").exists()          # 访客零落盘


def test_clear_removes_credential(tmp_path):
    s = DiscoveredModelStore(tmp_path / "disc.json")
    s.record("u1", "cred-1", ["a"], discovered_at=1.0)
    s.clear("u1", "cred-1")
    assert s.list_for_credential("u1", "cred-1") == []
