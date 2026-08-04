from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dependency_manifest import build_manifest, write_manifest
from environment_broker import BrokerError, EnvironmentBroker, default_policy


NOW = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)


class MutableClock:
    def __init__(self) -> None:
        self.value = NOW

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


def _project(root: Path) -> Path:
    (root / "package.json").write_text(
        json.dumps({"name": "example", "scripts": {"build": "example-build"}}),
        encoding="utf-8",
    )
    (root / "package-lock.json").write_text(
        json.dumps({"name": "example", "lockfileVersion": 3, "packages": {"": {"name": "example"}}}),
        encoding="utf-8",
    )
    write_manifest(root / "ZE_DEPENDENCIES.json", build_manifest(root, skill_id="ZE-example"))
    return root


def _runner(command: list[str] | tuple[str, ...], cwd: Path, environment: object) -> None:
    del environment
    assert "--ignore-scripts" in command
    installed = cwd / "node_modules"
    installed.mkdir()
    (installed / "package.json").write_text("{}", encoding="utf-8")


def _policy() -> dict:
    value = default_policy()
    value["automatic_sweep"] = False
    value["leases"] = {"heartbeat_seconds": 30, "expiry_seconds": 120}
    value["retention"] = {
        "standard_idle_seconds": 120,
        "large_idle_seconds": 60,
        "accelerator_idle_seconds": 60,
        "quarantine_seconds": 120,
    }
    value["budget"] = {"max_cache_bytes": 1024 * 1024 * 1024, "minimum_free_bytes": 0}
    return value


def test_prepare_release_and_reuse(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _project(project)
    broker = EnvironmentBroker(tmp_path / "cache", policy=_policy(), runner=_runner, clock=lambda: NOW)

    first = broker.prepare(
        manifest_path=project / "ZE_DEPENDENCIES.json",
        project_root=project,
        environment_id="root-node",
    )
    assert first.link_path.is_symlink()
    assert first.reused is False
    assert first.environment_path.is_dir()
    broker.release(first)
    assert not first.link_path.exists()
    assert first.environment_path.is_dir()

    second = broker.prepare(
        manifest_path=project / "ZE_DEPENDENCIES.json",
        project_root=project,
        environment_id="root-node",
    )
    assert second.reused is True
    assert second.cache_key == first.cache_key


def test_concurrent_consumer_lease_keeps_link_until_last_release(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _project(project)
    broker = EnvironmentBroker(tmp_path / "cache", policy=_policy(), runner=_runner, clock=lambda: NOW)

    first = broker.prepare(
        manifest_path=project / "ZE_DEPENDENCIES.json",
        project_root=project,
        environment_id="root-node",
    )
    second = broker.prepare(
        manifest_path=project / "ZE_DEPENDENCIES.json",
        project_root=project,
        environment_id="root-node",
    )
    broker.release(first)
    assert second.link_path.is_symlink()
    broker.release(second)
    assert not second.link_path.exists()


def test_existing_unmanaged_environment_is_never_replaced_or_deleted(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _project(project)
    unmanaged = project / "node_modules"
    unmanaged.mkdir()
    sentinel = unmanaged / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    broker = EnvironmentBroker(tmp_path / "cache", policy=_policy(), runner=_runner, clock=lambda: NOW)

    with pytest.raises(BrokerError, match="dependency_consumer_path_occupied"):
        broker.prepare(
            manifest_path=project / "ZE_DEPENDENCIES.json",
            project_root=project,
            environment_id="root-node",
        )

    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_unknown_consumer_kind_is_rejected_before_materialization(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _project(project)
    broker = EnvironmentBroker(
        tmp_path / "cache", policy=_policy(), runner=_runner, clock=lambda: NOW
    )

    with pytest.raises(BrokerError, match="dependency_consumer_kind_invalid"):
        broker.prepare(
            manifest_path=project / "ZE_DEPENDENCIES.json",
            project_root=project,
            environment_id="root-node",
            consumer_kind="release-build",
        )

    assert not (project / "node_modules").exists()


def test_active_lease_blocks_sweep_then_release_quarantines_and_recovers(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _project(project)
    clock = MutableClock()
    broker = EnvironmentBroker(tmp_path / "cache", policy=_policy(), runner=_runner, clock=clock)
    prepared = broker.prepare(
        manifest_path=project / "ZE_DEPENDENCIES.json",
        project_root=project,
        environment_id="root-node",
    )
    clock.advance(61)
    assert broker.sweep(confirm=True)["quarantined"] == []

    broker.release(prepared)
    clock.advance(61)
    result = broker.sweep(confirm=True)
    assert len(result["quarantined"]) == 1
    assert not prepared.environment_path.exists()

    recovered = broker.prepare(
        manifest_path=project / "ZE_DEPENDENCIES.json",
        project_root=project,
        environment_id="root-node",
    )
    assert recovered.reused is True
    assert recovered.environment_path.is_dir()


def test_quarantine_deletes_only_after_recovery_window(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _project(project)
    clock = MutableClock()
    broker = EnvironmentBroker(tmp_path / "cache", policy=_policy(), runner=_runner, clock=clock)
    prepared = broker.prepare(
        manifest_path=project / "ZE_DEPENDENCIES.json",
        project_root=project,
        environment_id="root-node",
    )
    broker.release(prepared)
    clock.advance(61)
    broker.sweep(confirm=True)
    assert list((tmp_path / "cache" / "quarantine").iterdir())
    clock.advance(121)
    result = broker.sweep(confirm=True)
    assert len(result["deleted"]) == 1
    assert list((tmp_path / "cache" / "quarantine").iterdir()) == []


def test_broker_rejects_manifest_symlink_and_never_touches_project_data(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _project(project)
    data = project / ".data"
    data.mkdir()
    sentinel = data / "project.json"
    sentinel.write_text("{}", encoding="utf-8")
    link = project / "linked-manifest.json"
    link.symlink_to(project / "ZE_DEPENDENCIES.json")
    broker = EnvironmentBroker(tmp_path / "cache", policy=_policy(), runner=_runner, clock=lambda: NOW)

    with pytest.raises(BrokerError, match="dependency_manifest_symlink_forbidden"):
        broker.prepare(
            manifest_path=link,
            project_root=project,
            environment_id="root-node",
        )

    broker.sweep(confirm=True)
    assert sentinel.is_file()
