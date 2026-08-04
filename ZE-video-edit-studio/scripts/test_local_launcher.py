from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import local_launcher as launcher  # noqa: E402


def _write_profile(root: Path, surface: str) -> None:
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "scripts" / "ze_command.py").write_text("", encoding="utf-8")
    if surface == "web":
        (root / "webapp" / "src" / "product").mkdir(parents=True)
        profile = {
            "schema": "ze.surface_profile.v1",
            "primary": "web",
            "surfaces": ["cli", "web"],
            "human_control": "web_workbench",
            "entrypoints": {"cli": "scripts/ze_command.py", "web": "webapp"},
            "cli": {"entrypoint": "scripts/ze_command.py", "supports": ["list"]},
            "api": None,
            "web": {
                "dir": "webapp",
                "product_dir": "webapp/src/product",
                "healthcheck_path": "/health",
            },
        }
        (root / "server.py").write_text("", encoding="utf-8")
    elif surface == "api":
        profile = {
            "schema": "ze.surface_profile.v1",
            "primary": "api",
            "surfaces": ["cli", "api"],
            "human_control": "cli_confirm",
            "entrypoints": {"cli": "scripts/ze_command.py", "api": "server.py"},
            "cli": {"entrypoint": "scripts/ze_command.py", "supports": ["list"]},
            "api": {
                "server": "server.py",
                "healthcheck_path": "/health",
                "port_required": True,
            },
            "web": None,
        }
        (root / "server.py").write_text("", encoding="utf-8")
    else:
        profile = {
            "schema": "ze.surface_profile.v1",
            "primary": "cli",
            "surfaces": ["cli"],
            "human_control": "cli_confirm",
            "entrypoints": {"cli": "scripts/ze_command.py"},
            "cli": {"entrypoint": "scripts/ze_command.py", "supports": ["list"]},
            "api": None,
            "web": None,
        }
    (root / "ZE_SURFACE.json").write_text(
        json.dumps(profile, ensure_ascii=False), encoding="utf-8"
    )


@pytest.mark.parametrize("surface,has_web", [("web", True), ("api", False)])
def test_local_runtime_uses_declared_service_surface(tmp_path, surface, has_web):
    _write_profile(tmp_path, surface)
    runtime = launcher.load_local_runtime(tmp_path)
    assert runtime.has_web is has_web
    assert runtime.health_path == "/health"


def test_headless_surface_refuses_to_invent_a_server(tmp_path):
    _write_profile(tmp_path, "cli")
    with pytest.raises(launcher.LauncherError, match="CLI"):
        launcher.load_local_runtime(tmp_path)


def test_unsafe_health_path_is_rejected(tmp_path):
    _write_profile(tmp_path, "web")
    profile_path = tmp_path / "ZE_SURFACE.json"
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    profile["web"]["healthcheck_path"] = "//outside.example/health"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    with pytest.raises(launcher.LauncherError, match="healthcheck_path"):
        launcher.load_local_runtime(tmp_path)


def test_explicit_port_never_kills_or_silently_changes_occupied_port(tmp_path):
    with pytest.raises(launcher.LauncherError, match="不会终止其他程序"):
        launcher.choose_local_port(
            tmp_path,
            environ={"PORT": "24567"},
            available=lambda _port: False,
        )


def test_deploy_port_is_authoritative_when_present(tmp_path):
    (tmp_path / "deploy.json").write_text('{"port": 25432}', encoding="utf-8")
    choice = launcher.choose_local_port(tmp_path, environ={}, available=lambda _port: True)
    assert choice == launcher.PortChoice(25432, "deploy.json")


def test_generated_local_port_is_stable_and_skips_collision(tmp_path):
    (tmp_path / "deploy.json").write_text('{"name":"ZE-example"}', encoding="utf-8")
    first = launcher._generated_port_start("ZE-example")
    seen = []

    def available(port):
        seen.append(port)
        return port != first

    choice = launcher.choose_local_port(tmp_path, environ={}, available=available)
    assert choice.source == "generated-local"
    assert seen[:2] == [first, first + 1]
    assert choice.port == first + 1


def test_invalid_boolean_deploy_port_is_rejected(tmp_path):
    (tmp_path / "deploy.json").write_text('{"port": true}', encoding="utf-8")
    with pytest.raises(launcher.LauncherError, match="不是有效整数"):
        launcher.choose_local_port(tmp_path, environ={}, available=lambda _port: True)


def test_local_environment_forces_loopback_account_mode(monkeypatch):
    monkeypatch.setenv("ZE_SERVER_MODE", "1")
    monkeypatch.setenv("ZE_DEV", "1")
    env = launcher.local_server_environment(22334)
    assert env["PORT"] == "22334"
    assert env["ZE_SERVER_MODE"] == "0"
    assert "ZE_DEV" not in env


def test_health_requires_small_json_status_ok(monkeypatch):
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return b'{"status":"ok"}'

    monkeypatch.setattr(launcher.urllib.request, "urlopen", lambda *_args, **_kwargs: Response())
    assert launcher._health_ok("http://127.0.0.1:20000/health") is True


def test_supported_python_range_is_explicit():
    launcher.validate_python_version((3, 9))
    launcher.validate_python_version((3, 13))
    with pytest.raises(launcher.LauncherError):
        launcher.validate_python_version((3, 8))
    with pytest.raises(launcher.LauncherError):
        launcher.validate_python_version((3, 14))


def test_venv_python_symlink_is_a_supported_posix_layout(tmp_path, monkeypatch):
    if launcher.os.name == "nt":
        pytest.skip("Windows venv uses python.exe")
    (tmp_path / "requirements.txt").write_text("", encoding="utf-8")
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").symlink_to(Path(sys.executable))
    monkeypatch.setattr(launcher, "_interpreter_version", lambda *_args, **_kwargs: (3, 11))
    monkeypatch.setattr(launcher, "_read_marker", lambda _path: "already-current")
    monkeypatch.setattr(launcher, "_digest_files", lambda *_args, **_kwargs: "already-current")
    assert launcher.prepare_python_environment(tmp_path) == venv_bin / "python"


def test_missing_dependency_manifest_keeps_legacy_launcher_path(tmp_path):
    runtime = launcher.LocalRuntime(has_web=False, health_path="/health")
    assert launcher.prepare_managed_dependencies(tmp_path, runtime) is None


def test_managed_dependency_manifest_selects_python_and_webapp_environments(tmp_path):
    (tmp_path / "ZE_DEPENDENCIES.json").write_text(
        json.dumps({
            "schema": "ze.dependency-manifest.v1",
            "environments": [
                {
                    "environment_id": "python-runtime",
                    "ecosystem": "python",
                    "working_directory": ".",
                    "purposes": ["development"],
                },
                {
                    "environment_id": "webapp-node",
                    "ecosystem": "node",
                    "working_directory": "webapp",
                    "purposes": ["development"],
                },
            ],
        }),
        encoding="utf-8",
    )
    (tmp_path / "webapp").mkdir()

    class FakeBroker:
        def __init__(self):
            self.calls = []
            self.released = []
            self.policy = {"leases": {"heartbeat_seconds": 60}}

        def prepare(self, **kwargs):
            environment_id = kwargs["environment_id"]
            self.calls.append((environment_id, kwargs["purpose"]))
            target = tmp_path / "cache" / environment_id
            target.mkdir(parents=True)
            if environment_id == "python-runtime":
                executable = target / ("Scripts/python.exe" if launcher.os.name == "nt" else "bin/python")
                executable.parent.mkdir(parents=True)
                executable.write_text("", encoding="utf-8")
                link = tmp_path / ".venv"
            else:
                link = tmp_path / "webapp" / "node_modules"
            link.symlink_to(target, target_is_directory=True)
            return launcher.PreparedEnvironment(
                environment_id,
                "sha256:" + "a" * 64,
                "lease_" + ("a" if environment_id == "python-runtime" else "b") * 32,
                link,
                target,
                False,
            )

        def release(self, prepared):
            self.released.append(prepared.environment_id)
            prepared.link_path.unlink(missing_ok=True)

    broker = FakeBroker()
    managed = launcher.prepare_managed_dependencies(
        tmp_path,
        launcher.LocalRuntime(has_web=True, health_path="/health"),
        broker=broker,
    )

    assert managed is not None
    assert broker.calls == [("python-runtime", "development"), ("webapp-node", "development")]
    assert managed.python.is_file()
    launcher._release_managed_dependencies(managed)
    assert broker.released == ["webapp-node", "python-runtime"]
    assert not (tmp_path / ".venv").exists()
    assert not (tmp_path / "webapp" / "node_modules").exists()
