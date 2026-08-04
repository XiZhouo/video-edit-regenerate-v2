"""ConfigRegistry 单进程原子 PUT / fail-closed load 契约。"""
from __future__ import annotations

import errno
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import registry as registry_mod
from registry import (
    ConfigDurabilityUnknown,
    ConfigLoadError,
    ConfigRegistry,
    ConfigVersionConflict,
    ConfigWriteError,
)


def _registry(*keys: str) -> ConfigRegistry:
    reg = ConfigRegistry()
    for key in keys or ("demo.value",):
        reg.declare(key, "int", default=0)
    return reg


def test_redeclare_cannot_downgrade_secret_or_readonly_contract():
    reg = ConfigRegistry()
    reg.declare(
        "demo.secret", "secret", default="TOP", secret=True,
        scope="secret", readonly=True,
    )
    with pytest.raises(ValueError, match="override_default"):
        reg.declare("demo.secret", "text", default="TOP")
    item = {row["key"]: row for row in reg.snapshot()}["demo.secret"]
    assert item["secret"] is True and item["readonly"] is True
    assert item["value"] == "***"


def test_override_default_preserves_declaration_contract():
    reg = ConfigRegistry()
    reg.declare(
        "demo.secret", "secret", default="old", secret=True,
        scope="secret", readonly=True,
    )
    reg.override_default("demo.secret", "new")
    item = {row["key"]: row for row in reg.snapshot()}["demo.secret"]
    assert item["secret"] is True and item["readonly"] is True
    assert reg.get("demo.secret") == "new"


@pytest.mark.parametrize(
    "value",
    ("file:///etc/passwd", "https://user:pass@example.com", "http://example.com/#x"),
)
def test_endpoint_rejects_non_http_userinfo_and_fragment(value):
    reg = ConfigRegistry()
    reg.declare("demo.endpoint", "endpoint", default="https://example.com")
    candidate, rejected = reg._candidate({"demo.endpoint": value})
    assert rejected and candidate == {}


def test_endpoint_host_allowlist_blocks_credential_redirect():
    reg = ConfigRegistry()
    reg.declare(
        "demo.endpoint", "endpoint", default="https://api.example.com/v1",
        allowed_hosts=("api.example.com",),
    )
    candidate, rejected = reg._candidate({
        "demo.endpoint": "https://127.0.0.1/internal",
    })
    assert rejected and candidate == {}


def test_missing_file_is_empty_but_corrupt_existing_file_fails_closed(tmp_path):
    path = tmp_path / "config.json"
    reg = _registry()
    reg.set_values({"demo.value": 7})

    missing = tmp_path / "missing.json"
    reg.load_values(missing)
    assert reg.get("demo.value") == 0

    reg.set_values({"demo.value": 7})
    for malformed in ("{not-json", "[]", '{"unknown.key": 1}'):
        path.write_text(malformed, encoding="utf-8")
        with pytest.raises(ConfigLoadError) as caught:
            reg.load_values(path)
        assert caught.value.code == "config_load_invalid"
        assert reg.get("demo.value") == 7

    path.write_bytes(b"\xff\xfe\x00")
    with pytest.raises(ConfigLoadError) as caught:
        reg.load_values(path)
    assert caught.value.context["stage"] == "decode"
    assert reg.get("demo.value") == 7


def test_atomic_update_persists_then_swaps_and_restarts_on_complete_new_value(tmp_path):
    path = tmp_path / "config.json"
    reg = _registry()
    before = reg.version

    assert reg.update_values(path, {"demo.value": "8"}, expected_version=before) == []
    assert reg.get("demo.value") == 8
    assert reg.version == before + 1
    assert json.loads(path.read_text(encoding="utf-8")) == {"demo.value": 8}

    restarted = _registry()
    restarted.load_values(path)
    assert restarted.get("demo.value") == 8


@pytest.mark.parametrize(
    ("target", "stage"),
    [
        ("_write_stream", "write"),
        ("_flush_stream", "flush"),
        ("_fsync_stream", "file_fsync"),
        ("os.replace", "replace"),
    ],
)
def test_every_pre_replace_failure_keeps_old_memory_and_old_complete_file(
    tmp_path, monkeypatch, target, stage,
):
    path = tmp_path / "config.json"
    reg = _registry()
    reg.update_values(path, {"demo.value": 1})
    old_version = reg.version

    def fail(*_args, **_kwargs):
        raise OSError(errno.EIO, f"injected {stage}")

    if target == "os.replace":
        monkeypatch.setattr(registry_mod.os, "replace", fail)
    else:
        monkeypatch.setattr(registry_mod, target, fail)
    with pytest.raises(ConfigWriteError) as caught:
        reg.update_values(path, {"demo.value": 2})

    assert caught.value.code == "config_write_failed"
    assert caught.value.context == {
        "stage": stage, "path": str(path), "replaced": False,
    }
    assert reg.get("demo.value") == 1
    assert reg.version == old_version
    assert json.loads(path.read_text(encoding="utf-8")) == {"demo.value": 1}
    assert list(tmp_path.glob(".config.json.*.tmp")) == []

    restarted = _registry()
    restarted.load_values(path)
    assert restarted.get("demo.value") == 1


def test_temp_creation_failure_keeps_old_state_and_leaves_no_temp(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    reg = _registry()
    reg.update_values(path, {"demo.value": 1})

    def fail(*_args, **_kwargs):
        raise OSError(errno.ENOSPC, "no space")

    monkeypatch.setattr(registry_mod.tempfile, "mkstemp", fail)
    with pytest.raises(ConfigWriteError) as caught:
        reg.update_values(path, {"demo.value": 2})
    assert caught.value.context["stage"] == "temp_create"
    assert reg.get("demo.value") == 1
    assert json.loads(path.read_text(encoding="utf-8")) == {"demo.value": 1}
    assert list(tmp_path.glob(".config.json.*.tmp")) == []


def test_directory_fsync_eio_is_honest_unknown_and_memory_follows_complete_new_file(
    tmp_path, monkeypatch,
):
    path = tmp_path / "config.json"
    reg = _registry()
    reg.update_values(path, {"demo.value": 1})

    def fail(_path):
        raise OSError(errno.EIO, "directory device error")

    monkeypatch.setattr(registry_mod, "_fsync_parent", fail)
    with pytest.raises(ConfigDurabilityUnknown) as caught:
        reg.update_values(path, {"demo.value": 2})

    assert caught.value.code == "config_durability_unknown"
    assert caught.value.context == {
        "stage": "directory_fsync", "path": str(path), "replaced": True,
    }
    assert reg.get("demo.value") == 2
    assert json.loads(path.read_text(encoding="utf-8")) == {"demo.value": 2}
    assert list(tmp_path.glob(".config.json.*.tmp")) == []

    restarted = _registry()
    restarted.load_values(path)
    assert restarted.get("demo.value") == 2


def test_unsupported_directory_fsync_is_best_effort_but_observable(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    reg = _registry()
    monkeypatch.setattr(registry_mod, "_fsync_parent", lambda _path: False)

    reg.update_values(path, {"demo.value": 3})

    assert reg.directory_fsync_supported is False
    assert reg.get("demo.value") == 3


def test_invalid_value_is_rejected_before_any_disk_write(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    reg = ConfigRegistry()
    reg.declare("demo.ratio", "float", default=1.0)
    reg.update_values(path, {"demo.ratio": 2.0})
    replace_calls = 0
    real_replace = registry_mod.os.replace

    def counted(*args, **kwargs):
        nonlocal replace_calls
        replace_calls += 1
        return real_replace(*args, **kwargs)

    monkeypatch.setattr(registry_mod.os, "replace", counted)
    rejected = reg.update_values(path, {"demo.ratio": "nan"})

    assert rejected and rejected[0][0] == "demo.ratio"
    assert replace_calls == 0
    assert reg.get("demo.ratio") == 2.0
    assert json.loads(path.read_text(encoding="utf-8")) == {"demo.ratio": 2.0}


def test_rejected_shape_stays_compatible_while_valid_siblings_commit(tmp_path):
    path = tmp_path / "config.json"
    reg = _registry("demo.good")

    rejected = reg.update_values(path, {"demo.good": 4, "demo.unknown": 9})

    assert rejected == [("demo.unknown", "未声明")]
    assert reg.get("demo.good") == 4
    assert json.loads(path.read_text(encoding="utf-8")) == {"demo.good": 4}


def test_process_local_cas_rejects_stale_writer_before_disk_write(tmp_path):
    path = tmp_path / "config.json"
    reg = _registry()
    stale = reg.version
    reg.update_values(path, {"demo.value": 1}, expected_version=stale)

    with pytest.raises(ConfigVersionConflict) as caught:
        reg.update_values(path, {"demo.value": 2}, expected_version=stale)

    assert caught.value.code == "config_version_conflict"
    assert reg.get("demo.value") == 1
    assert json.loads(path.read_text(encoding="utf-8")) == {"demo.value": 1}


def test_concurrent_thread_updates_are_serialized_without_lost_keys(tmp_path):
    path = tmp_path / "config.json"
    keys = tuple(f"demo.value_{index}" for index in range(12))
    reg = _registry(*keys)

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(
            lambda pair: reg.update_values(path, {pair[1]: pair[0]}),
            enumerate(keys, start=1),
        ))

    assert results == [[] for _ in keys]
    assert json.loads(path.read_text(encoding="utf-8")) == {
        key: index for index, key in enumerate(keys, start=1)
    }
    restarted = _registry(*keys)
    restarted.load_values(path)
    assert {key: restarted.get(key) for key in keys} == {
        key: index for index, key in enumerate(keys, start=1)
    }


def test_each_commit_uses_a_unique_temp_name_and_cleans_it(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    reg = _registry()
    seen = []
    real_mkstemp = registry_mod.tempfile.mkstemp

    def capturing(*args, **kwargs):
        fd, name = real_mkstemp(*args, **kwargs)
        seen.append(name)
        return fd, name

    monkeypatch.setattr(registry_mod.tempfile, "mkstemp", capturing)
    reg.update_values(path, {"demo.value": 1})
    reg.update_values(path, {"demo.value": 2})

    assert len(seen) == 2
    assert len(set(seen)) == 2
    assert list(tmp_path.glob(".config.json.*.tmp")) == []


def test_override_empty_string_wins_for_text_and_endpoint(tmp_path):
    """C 端档案注入回归(2026-07-21):显式 override 空串对 text/endpoint 必须生效,
    不被 coerce 顶回声明默认(否则 service_url 置空回落内网默认,击穿不变式 2)。"""
    import json
    from registry import ConfigRegistry
    import config_schema as cs

    reg = ConfigRegistry()
    reg.declare("t.endpoint", cs.KIND_ENDPOINT, default="http://10.0.0.252:12018",
                label="端点", desc="空=关闭", group="测试")
    reg.declare("t.text", cs.KIND_TEXT, default="默认值", label="文本", desc="", group="测试")
    reg.declare("t.num", cs.KIND_INT, default=7, label="数", desc="", group="测试")

    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"t.endpoint": "", "t.text": "", "t.num": ""}),
                   encoding="utf-8")
    reg.load_values(cfg)
    assert reg.get("t.endpoint") == ""          # 显式空覆盖生效
    assert reg.get("t.text") == ""
    assert reg.get("t.num") == 7                # 非 text/endpoint:空串仍回默认(原语义)
