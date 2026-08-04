#!/usr/bin/env python3
"""Cross-platform local product launcher for a ZE Skill.

This is deliberately separate from ``start.sh``.  ``start.sh`` is the
deployment entrypoint and enables server mode; this launcher binds only to
loopback, keeps user/project state on the current computer, prepares pinned
dependencies, waits for the real health endpoint, and opens the Web surface.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import webbrowser
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence, Tuple

from surface_profile import load_surface_profile, validate_surface_profile
from environment_broker import (
    MANIFEST_NAME as DEPENDENCY_MANIFEST_NAME,
    BrokerError,
    EnvironmentBroker,
    LeaseHeartbeat,
    PreparedEnvironment,
    default_state_root,
    load_policy,
)


MIN_PYTHON = (3, 9)
MAX_PYTHON_EXCLUSIVE = (3, 14)
LOCAL_PORT_MIN = 20_000
LOCAL_PORT_SPAN = 30_000
LOCAL_PORT_PROBES = 256
HEALTH_TIMEOUT_SECONDS = 60.0


class LauncherError(RuntimeError):
    """A product-facing startup failure with a safe, actionable message."""


@dataclass(frozen=True)
class LocalRuntime:
    has_web: bool
    health_path: str


@dataclass(frozen=True)
class PortChoice:
    port: int
    source: str


@dataclass(frozen=True)
class ManagedDependencies:
    broker: EnvironmentBroker
    prepared: tuple[PreparedEnvironment, ...]
    python: Path


def _reject_duplicate_keys(pairs: Sequence[Tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate JSON key: {key}")
        out[key] = value
    return out


def _read_json_object(path: Path, *, optional: bool = False) -> dict[str, Any]:
    if optional and not path.exists() and not path.is_symlink():
        return {}
    if path.is_symlink():
        raise LauncherError(f"配置文件不能是符号链接：{path.name}")
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except FileNotFoundError as exc:
        raise LauncherError(f"缺少配置文件：{path.name}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise LauncherError(f"无法安全读取 {path.name}：{exc}") from exc
    if not isinstance(value, dict):
        raise LauncherError(f"{path.name} 必须是 JSON 对象")
    return value


def _validated_health_path(value: Any) -> str:
    path = value if isinstance(value, str) else "/health"
    if (
        not path.startswith("/")
        or path.startswith("//")
        or "?" in path
        or "#" in path
        or "\\" in path
        or ".." in path.split("/")
        or len(path) > 128
        or any(ord(char) < 0x20 or ord(char) > 0x7E for char in path)
    ):
        raise LauncherError("Surface Profile 的 healthcheck_path 不安全")
    return path


def load_local_runtime(root: Path) -> LocalRuntime:
    profile = load_surface_profile(root)
    errors = validate_surface_profile(root, profile)
    if errors:
        raise LauncherError("Surface Profile 无效：" + "; ".join(errors))
    surfaces = profile.get("surfaces")
    active = set(surfaces if isinstance(surfaces, list) else [])
    has_web = "web" in active
    if not ({"web", "api"} & active):
        raise LauncherError(
            "这个工具没有本地服务界面；请使用 scripts/ze_command.py 的 CLI 入口"
        )
    server = root / "server.py"
    if not server.is_file() or server.is_symlink():
        raise LauncherError("本地服务入口 server.py 缺失或不安全")
    detail_name = "web" if has_web else "api"
    detail = profile.get(detail_name)
    detail = detail if isinstance(detail, dict) else {}
    return LocalRuntime(
        has_web=has_web,
        health_path=_validated_health_path(detail.get("healthcheck_path", "/health")),
    )


def _parse_port(value: Any, source: str) -> int:
    if isinstance(value, bool):
        raise LauncherError(f"{source} 端口不是有效整数")
    try:
        text = str(value).strip()
        if not text or any(char not in "0123456789" for char in text):
            raise ValueError
        port = int(text, 10)
    except (TypeError, ValueError) as exc:
        raise LauncherError(f"{source} 端口不是有效整数") from exc
    if not 1024 <= port <= 65535:
        raise LauncherError(f"{source} 端口必须在 1024–65535 之间")
    return port


def port_is_available(port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _generated_port_start(identity: str) -> int:
    digest = hashlib.sha256(identity.encode("utf-8")).digest()
    return LOCAL_PORT_MIN + int.from_bytes(digest[:4], "big") % LOCAL_PORT_SPAN


def choose_local_port(
    root: Path,
    *,
    requested: Optional[Any] = None,
    environ: Optional[Mapping[str, str]] = None,
    available: Callable[[int], bool] = port_is_available,
) -> PortChoice:
    env = os.environ if environ is None else environ
    if requested is not None:
        port = _parse_port(requested, "--port")
        if not available(port):
            raise LauncherError(f"端口 {port} 已被占用；本启动器不会终止其他程序")
        return PortChoice(port, "command-line")
    if str(env.get("PORT") or "").strip():
        port = _parse_port(env.get("PORT"), "PORT")
        if not available(port):
            raise LauncherError(f"端口 {port} 已被占用；本启动器不会终止其他程序")
        return PortChoice(port, "environment")

    deploy = _read_json_object(root / "deploy.json", optional=True)
    if deploy.get("port") is not None:
        port = _parse_port(deploy.get("port"), "deploy.json")
        if not available(port):
            raise LauncherError(
                f"deploy.json 端口 {port} 已被占用；可设置 PORT 选择本机端口"
            )
        return PortChoice(port, "deploy.json")

    identity = deploy.get("skill") or deploy.get("name") or root.name
    identity = identity if isinstance(identity, str) and identity.strip() else root.name
    start = _generated_port_start(identity)
    for offset in range(LOCAL_PORT_PROBES):
        port = LOCAL_PORT_MIN + ((start - LOCAL_PORT_MIN + offset) % LOCAL_PORT_SPAN)
        if available(port):
            return PortChoice(port, "generated-local")
    raise LauncherError("没有找到可用的本机端口；请设置 PORT 后重试")


def validate_python_version(version: Tuple[int, int]) -> None:
    if version < MIN_PYTHON or version >= MAX_PYTHON_EXCLUSIVE:
        raise LauncherError("需要 Python 3.9–3.13")


def _venv_python(root: Path) -> Path:
    if os.name == "nt":
        return root / ".venv" / "Scripts" / "python.exe"
    return root / ".venv" / "bin" / "python"


def _digest_files(root: Path, paths: Sequence[Path], extra: str = "") -> str:
    digest = hashlib.sha256()
    digest.update(extra.encode("utf-8"))
    digest.update(b"\0")
    for path in paths:
        if not path.is_file() or path.is_symlink():
            raise LauncherError(f"依赖清单缺失或不安全：{path.relative_to(root)}")
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _read_marker(path: Path) -> str:
    try:
        if path.is_symlink():
            return ""
        return path.read_text(encoding="ascii").strip()
    except (FileNotFoundError, OSError, UnicodeError):
        return ""


def _write_marker(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(value + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _run_checked(command: Sequence[str], *, cwd: Path, stage: str) -> None:
    print(f"[ZE] {stage}")
    try:
        subprocess.run(list(command), cwd=str(cwd), check=True)
    except FileNotFoundError as exc:
        raise LauncherError(f"{stage}失败：找不到 {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        raise LauncherError(f"{stage}失败，退出码 {exc.returncode}") from exc


def _interpreter_version(executable: Path, *, cwd: Path) -> Tuple[int, int]:
    try:
        result = subprocess.run(
            [
                str(executable),
                "-c",
                "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')",
            ],
            cwd=str(cwd),
            check=True,
            capture_output=True,
            text=True,
        )
        major_text, minor_text = result.stdout.strip().split(".", 1)
        return int(major_text), int(minor_text)
    except (FileNotFoundError, OSError, ValueError, subprocess.CalledProcessError) as exc:
        raise LauncherError("无法验证 .venv 中的 Python 解释器") from exc


def prepare_python_environment(root: Path) -> Path:
    validate_python_version((sys.version_info.major, sys.version_info.minor))
    venv = root / ".venv"
    if venv.is_symlink():
        raise LauncherError(".venv 不能是符号链接")
    venv_python = _venv_python(root)
    if not venv.exists():
        _run_checked(
            [sys.executable, "-m", "venv", str(venv)],
            cwd=root,
            stage="创建本地 Python 环境",
        )
    # venv/bin/python 在 POSIX 上通常是指向创建它的系统解释器的合法符号链接；
    # 真正需要拒绝的是整个 .venv 被链接到项目外，而不是 Python 自身的标准布局。
    if not venv_python.is_file():
        raise LauncherError("本地 Python 环境不完整；请移走 .venv 后重试")
    venv_version = _interpreter_version(venv_python, cwd=root)
    validate_python_version(venv_version)

    requirements = root / "requirements.txt"
    wanted = _digest_files(
        root,
        [requirements],
        extra=f"python={venv_version[0]}.{venv_version[1]}",
    )
    marker = venv / ".ze-requirements.sha256"
    if _read_marker(marker) != wanted:
        _run_checked(
            [
                str(venv_python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "-r",
                str(requirements),
            ],
            cwd=root,
            stage="安装 Python 依赖",
        )
        _write_marker(marker, wanted)
    return venv_python


def prepare_web_environment(root: Path) -> None:
    webapp = root / "webapp"
    if not webapp.is_dir() or webapp.is_symlink():
        raise LauncherError("Web Surface 目录 webapp 缺失或不安全")
    npm = shutil.which("npm")
    if not npm:
        raise LauncherError("Web Surface 需要 Node.js 与 npm")
    package_json = webapp / "package.json"
    package_lock = webapp / "package-lock.json"
    dependency_files = [package_json]
    install_command = [npm, "install", "--no-audit", "--no-fund"]
    if package_lock.is_file() and not package_lock.is_symlink():
        dependency_files.append(package_lock)
        install_command = [npm, "ci", "--no-audit", "--no-fund"]
    elif package_lock.exists() or package_lock.is_symlink():
        raise LauncherError("webapp/package-lock.json 不是安全的普通文件")

    node_modules = webapp / "node_modules"
    if node_modules.is_symlink():
        raise LauncherError("webapp/node_modules 不能是符号链接")
    wanted = _digest_files(root, dependency_files, extra="npm-local-v1")
    marker = node_modules / ".ze-dependencies.sha256"
    if _read_marker(marker) != wanted:
        _run_checked(install_command, cwd=webapp, stage="安装网页依赖")
        _write_marker(marker, wanted)
    _run_checked([npm, "run", "build"], cwd=webapp, stage="构建本地网页")


def _select_managed_environment(
    manifest: Mapping[str, Any], *, ecosystem: str, working_directory: str
) -> str:
    rows = manifest.get("environments")
    if not isinstance(rows, list):
        raise LauncherError("ZE_DEPENDENCIES.json 缺少 environments")
    matches = [
        row
        for row in rows
        if isinstance(row, Mapping)
        and row.get("ecosystem") == ecosystem
        and row.get("working_directory") == working_directory
        and "development" in (row.get("purposes") or [])
    ]
    if len(matches) != 1 or not isinstance(matches[0].get("environment_id"), str):
        raise LauncherError(
            f"ZE_DEPENDENCIES.json 必须为 {working_directory} 声明一个 {ecosystem} 开发环境"
        )
    return str(matches[0]["environment_id"])


def _release_managed_dependencies(managed: ManagedDependencies) -> None:
    for prepared in reversed(managed.prepared):
        try:
            managed.broker.release(prepared)
        except BrokerError as exc:
            print(f"[ZE][WARN] 依赖环境租约释放失败：{exc.code}", file=sys.stderr)


def prepare_managed_dependencies(
    root: Path,
    runtime: LocalRuntime,
    *,
    broker: EnvironmentBroker | None = None,
) -> ManagedDependencies | None:
    """Prepare broker-owned environments for migrated tools.

    A missing manifest deliberately selects the legacy path. Once a manifest
    exists, every declared environment is fail-closed: ordinary project
    ``.venv``/``node_modules`` directories are never adopted or replaced.
    """
    manifest_path = root / DEPENDENCY_MANIFEST_NAME
    if not manifest_path.exists() and not manifest_path.is_symlink():
        return None
    manifest = _read_json_object(manifest_path)
    python_id = _select_managed_environment(
        manifest, ecosystem="python", working_directory="."
    )
    selections = [python_id]
    if runtime.has_web:
        selections.append(
            _select_managed_environment(
                manifest, ecosystem="node", working_directory="webapp"
            )
        )
    if broker is None:
        policy_path = root / "ZE_DEPENDENCY_POLICY.json"
        try:
            policy = load_policy(
                policy_path if policy_path.exists() or policy_path.is_symlink() else None
            )
            broker = EnvironmentBroker(default_state_root(), policy=policy)
        except BrokerError as exc:
            raise LauncherError(f"依赖环境策略不可用：{exc.code}") from exc
    prepared: list[PreparedEnvironment] = []
    try:
        for environment_id in selections:
            prepared.append(
                broker.prepare(
                    manifest_path=manifest_path,
                    project_root=root,
                    environment_id=environment_id,
                    purpose="development",
                )
            )
    except BrokerError as exc:
        partial = ManagedDependencies(broker, tuple(prepared), root / ".venv")
        _release_managed_dependencies(partial)
        raise LauncherError(f"依赖环境准备失败：{exc.code}") from exc
    python = _venv_python(root)
    if not python.is_file():
        managed = ManagedDependencies(broker, tuple(prepared), python)
        _release_managed_dependencies(managed)
        raise LauncherError("受管 Python 环境不完整")
    return ManagedDependencies(broker, tuple(prepared), python)


def build_managed_web(root: Path) -> None:
    webapp = root / "webapp"
    modules = webapp / "node_modules"
    if not modules.is_symlink():
        raise LauncherError("受管网页依赖链接缺失")
    npm = shutil.which("npm")
    if not npm:
        raise LauncherError("Web Surface 需要 Node.js 与 npm")
    _run_checked([npm, "run", "build"], cwd=webapp, stage="构建本地网页")


def local_server_environment(port: int) -> dict[str, str]:
    env = dict(os.environ)
    env["PORT"] = str(port)
    env["ZE_SERVER_MODE"] = "0"
    env["PYTHONUNBUFFERED"] = "1"
    env.pop("ZE_DEV", None)
    return env


def _health_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=1.5) as response:
            if getattr(response, "status", 200) != 200:
                return False
            body = response.read(4097)
        if len(body) > 4096:
            return False
        payload = json.loads(body.decode("utf-8"))
        return isinstance(payload, dict) and payload.get("status") == "ok"
    except (OSError, UnicodeError, ValueError, urllib.error.URLError):
        return False


def wait_for_health(
    process: subprocess.Popen[Any],
    url: str,
    *,
    timeout: float = HEALTH_TIMEOUT_SECONDS,
    checker: Callable[[str], bool] = _health_ok,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        code = process.poll()
        if code is not None:
            raise LauncherError(f"本地服务启动后立即退出，退出码 {code}")
        if checker(url):
            return
        time.sleep(0.25)
    raise LauncherError("本地服务健康检查超时")


def _stop_process(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
    except OSError:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
            process.wait(timeout=5)
        except OSError:
            return


def launch(root: Path, *, requested_port: Optional[Any], open_browser: bool) -> int:
    runtime = load_local_runtime(root)
    port_choice = choose_local_port(root, requested=requested_port)
    print(
        f"[ZE] 本机模式：127.0.0.1:{port_choice.port} "
        f"(端口来源：{port_choice.source})"
    )
    print("[ZE] 用户账号与项目文件由本机环境持有；未启用部署服务器账号模式")
    managed = prepare_managed_dependencies(root, runtime)
    if managed is None:
        venv_python = prepare_python_environment(root)
        if runtime.has_web:
            prepare_web_environment(root)
    else:
        venv_python = managed.python
        print("[ZE] 依赖由受管缓存按锁文件准备；项目数据不进入缓存")
        if runtime.has_web:
            build_managed_web(root)

    command = [
        str(venv_python),
        "-m",
        "uvicorn",
        "server:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port_choice.port),
    ]
    health_url = f"http://127.0.0.1:{port_choice.port}{runtime.health_path}"
    product_url = f"http://127.0.0.1:{port_choice.port}/"
    process: subprocess.Popen[Any] | None = None
    try:
        heartbeat = (
            LeaseHeartbeat(managed.broker, managed.prepared)
            if managed is not None
            else nullcontext()
        )
        with heartbeat:
            try:
                process = subprocess.Popen(
                    command,
                    cwd=str(root),
                    env=local_server_environment(port_choice.port),
                )
            except OSError as exc:
                raise LauncherError(f"无法启动本地服务：{exc}") from exc
            wait_for_health(process, health_url)
            print(f"[ZE] 本地服务已就绪：{product_url if runtime.has_web else health_url}")
            if runtime.has_web and open_browser:
                try:
                    if not webbrowser.open(product_url, new=2):
                        print(f"[ZE][WARN] 浏览器未自动打开，请手动访问 {product_url}")
                except (OSError, webbrowser.Error):
                    print(f"[ZE][WARN] 浏览器未自动打开，请手动访问 {product_url}")
            try:
                return process.wait()
            except KeyboardInterrupt:
                print("\n[ZE] 正在停止本地服务…")
                return 130
    finally:
        if process is not None:
            _stop_process(process)
        if managed is not None:
            _release_managed_dependencies(managed)


def main() -> int:
    parser = argparse.ArgumentParser(description="ZE Skill 本机双击启动器")
    parser.add_argument("--port", default=None, help="本机端口；也可通过 PORT 环境变量指定")
    parser.add_argument("--no-browser", action="store_true", help="服务就绪后不自动打开浏览器")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if os.name != "nt":
        os.umask(0o077)
    try:
        return launch(root, requested_port=args.port, open_browser=not args.no_browser)
    except LauncherError as exc:
        print(f"[ZE][FATAL] {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n[ZE] 本机启动已取消", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
