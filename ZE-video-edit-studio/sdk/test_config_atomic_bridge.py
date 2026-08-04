"""Repository 配置门面必须消费 ConfigRegistry 的单次原子更新。"""
from __future__ import annotations

import threading

import pytest

from sdk import ConfigRegistry, ZeError
from sdk.bootstrap import Repository


def _repository(tmp_path):
    repo = Repository.__new__(Repository)
    repo.config = ConfigRegistry()
    repo.config.declare("demo.value", "int", default=0)
    repo.config_path = tmp_path / "config.json"
    repo._config_lock = threading.RLock()
    return repo


def test_repository_save_config_uses_atomic_update_and_process_local_cas(tmp_path):
    repo = _repository(tmp_path)
    version = repo.config.version

    assert repo.save_config({"demo.value": 1}, expected_version=version) == []
    assert repo.config.get("demo.value") == 1

    with pytest.raises(ZeError) as caught:
        repo.save_config({"demo.value": 2}, expected_version=version)
    assert caught.value.code == "config_version_conflict"
    assert repo.config.get("demo.value") == 1


def test_repository_translates_only_stable_config_errors(tmp_path, monkeypatch):
    repo = _repository(tmp_path)

    class ConfigFailure(RuntimeError):
        code = "config_durability_unknown"
        context = {"stage": "directory_fsync", "replaced": True}

    def fail(*_args, **_kwargs):
        raise ConfigFailure("rename durability is unknown")

    monkeypatch.setattr(repo.config, "update_values", fail)
    with pytest.raises(ZeError) as caught:
        repo.save_config({"demo.value": 1}, expected_version=repo.config.version)
    assert caught.value.code == "config_durability_unknown"
    assert caught.value.context == ConfigFailure.context
