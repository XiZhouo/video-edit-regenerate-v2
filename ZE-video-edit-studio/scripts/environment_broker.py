#!/usr/bin/env python3
from __future__ import annotations

"""Content-addressed, lease-protected environments for local ZE tool use.

Only directories below a broker root carrying the ZE ownership marker can be
quarantined or deleted. Existing project environments and project data are
never adopted, replaced or cleaned implicitly.
"""

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator, Mapping, Sequence


MANIFEST_NAME = "ZE_DEPENDENCIES.json"
ROOT_MARKER = ".ze-dependency-cache-root.json"
ENTRY_MARKER = "ZE_ENVIRONMENT_ENTRY.json"
QUARANTINE_MARKER = "ZE_QUARANTINE.json"
ROOT_SCHEMA = "ze.dependency-cache-root.v1"
ENTRY_SCHEMA = "ze.dependency-cache-entry.v1"
_CACHE_KEY = "sha256:"
_MAX_JSON_BYTES = 8 * 1024 * 1024


class BrokerError(RuntimeError):
    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class PreparedEnvironment:
    environment_id: str
    cache_key: str
    lease_id: str
    link_path: Path
    environment_path: Path
    reused: bool


Runner = Callable[[Sequence[str], Path, Mapping[str, str]], None]
Clock = Callable[[], datetime]


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def canonical_digest(value: Any) -> str:
    return _CACHE_KEY + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return _CACHE_KEY + digest.hexdigest()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise BrokerError("dependency_timestamp_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BrokerError("dependency_timestamp_invalid") from exc
    if parsed.tzinfo is None:
        raise BrokerError("dependency_timestamp_invalid")
    return parsed.astimezone(timezone.utc)


def default_policy() -> dict[str, Any]:
    return {
        "schema": "ze.dependency-policy.v1",
        "policy_id": "local-managed-default",
        "automatic_sweep": True,
        "install": {
            "allowed_ecosystems": ["node", "python", "provider-managed"],
            "allow_network_on_cache_miss": True,
            "allow_lifecycle_scripts": False,
            "allow_draft_development": True,
        },
        "leases": {"heartbeat_seconds": 60, "expiry_seconds": 300},
        "retention": {
            "standard_idle_seconds": 2592000,
            "large_idle_seconds": 604800,
            "accelerator_idle_seconds": 259200,
            "quarantine_seconds": 86400,
        },
        "budget": {"max_cache_bytes": 21474836480, "minimum_free_bytes": 2147483648},
        "safety": {
            "cleanup_scope": "broker-owned-cache-only",
            "project_data": "never-touch",
            "deletion_mode": "quarantine-then-delete",
        },
    }


def default_state_root(environment: Mapping[str, str] | None = None) -> Path:
    source = environment if environment is not None else os.environ
    explicit = source.get("ZE_DEPENDENCY_CACHE_ROOT")
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_absolute():
            raise BrokerError("dependency_cache_root_must_be_absolute")
        return path
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "ZE" / "skill-environments"
    if os.name == "nt":
        base = source.get("LOCALAPPDATA")
        if not base:
            raise BrokerError("dependency_cache_root_unavailable")
        return Path(base) / "ZE" / "skill-environments"
    base = source.get("XDG_CACHE_HOME")
    return (Path(base) if base else Path.home() / ".cache") / "ze" / "skill-environments"


def _json_file(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise BrokerError("dependency_json_invalid")
    try:
        if path.stat().st_size > _MAX_JSON_BYTES:
            raise BrokerError("dependency_json_invalid")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BrokerError("dependency_json_invalid") from exc
    if not isinstance(value, dict):
        raise BrokerError("dependency_json_invalid")
    return value


def _atomic_json(path: Path, value: Mapping[str, Any], *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, mode)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8") + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _safe_relative(root: Path, raw: Any, *, file_required: bool = False) -> Path:
    if not isinstance(raw, str):
        raise BrokerError("dependency_path_invalid")
    if raw == ".":
        return root
    pure = PurePosixPath(raw)
    if (
        not raw or raw.startswith("/") or pure.is_absolute()
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise BrokerError("dependency_path_invalid")
    current = root
    for part in pure.parts:
        current = current / part
        if current.is_symlink():
            raise BrokerError("dependency_path_symlink_forbidden")
    try:
        current.resolve(strict=file_required).relative_to(root.resolve(strict=True))
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise BrokerError("dependency_path_invalid") from exc
    if file_required and not current.is_file():
        raise BrokerError("dependency_file_missing")
    return current


def _safe_consumer_link(root: Path, raw: Any) -> Path:
    """Resolve a declared consumer link while allowing only its final symlink.

    Parent symlinks would let a manifest redirect writes outside the project.
    The final component may already be the exact broker-managed link when a
    second lease for the same project/environment is acquired.
    """
    if not isinstance(raw, str):
        raise BrokerError("dependency_path_invalid")
    pure = PurePosixPath(raw)
    if (
        not raw
        or raw.startswith("/")
        or pure.is_absolute()
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise BrokerError("dependency_path_invalid")
    current = root
    for part in pure.parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise BrokerError("dependency_path_symlink_forbidden")
    candidate = current / pure.parts[-1]
    try:
        candidate.parent.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise BrokerError("dependency_path_invalid") from exc
    return candidate


def _manifest_environment(
    manifest: Mapping[str, Any], project_root: Path, environment_id: str, *, purpose: str
) -> dict[str, Any]:
    if manifest.get("schema") != "ze.dependency-manifest.v1":
        raise BrokerError("dependency_manifest_schema_invalid")
    if manifest.get("status") not in {"draft", "ready"}:
        raise BrokerError("dependency_manifest_status_invalid")
    rows = manifest.get("environments")
    if not isinstance(rows, list):
        raise BrokerError("dependency_manifest_invalid")
    matches = [row for row in rows if isinstance(row, Mapping) and row.get("environment_id") == environment_id]
    if len(matches) != 1:
        raise BrokerError("dependency_environment_selection_invalid")
    environment = dict(matches[0])
    if purpose not in environment.get("purposes", []):
        raise BrokerError("dependency_environment_purpose_invalid")
    ecosystem = environment.get("ecosystem")
    if ecosystem not in {"node", "python"}:
        raise BrokerError("dependency_environment_not_host_materializable")
    materialization = environment.get("materialization")
    if (
        not isinstance(materialization, Mapping)
        or materialization.get("strategy") != "adapter-selected"
        or "content-addressed-link" not in materialization.get("supported_strategies", [])
    ):
        raise BrokerError("dependency_materialization_strategy_invalid")
    expected_leaf = "node_modules" if ecosystem == "node" else ".venv"
    working = environment.get("working_directory")
    expected_link = expected_leaf if working == "." else f"{working}/{expected_leaf}"
    if materialization.get("link_path") != expected_link:
        raise BrokerError("dependency_link_contract_invalid")
    for field in ("descriptor", "lock"):
        reference = environment.get(field)
        if reference is None:
            if field == "lock" and manifest.get("status") == "draft":
                continue
            raise BrokerError("dependency_manifest_file_ref_missing")
        if not isinstance(reference, Mapping):
            raise BrokerError("dependency_manifest_file_ref_invalid")
        path = _safe_relative(project_root, reference.get("path"), file_required=True)
        if file_digest(path) != reference.get("digest"):
            raise BrokerError("dependency_manifest_file_digest_mismatch")
    return environment


def _policy_check(policy: Mapping[str, Any], manifest: Mapping[str, Any], environment: Mapping[str, Any], purpose: str) -> None:
    if policy.get("schema") != "ze.dependency-policy.v1":
        raise BrokerError("dependency_policy_schema_invalid")
    install_policy = policy.get("install")
    leases = policy.get("leases")
    retention = policy.get("retention")
    safety = policy.get("safety")
    if not all(isinstance(value, Mapping) for value in (install_policy, leases, retention, safety)):
        raise BrokerError("dependency_policy_invalid")
    if safety.get("cleanup_scope") != "broker-owned-cache-only" or safety.get("project_data") != "never-touch" or safety.get("deletion_mode") != "quarantine-then-delete":
        raise BrokerError("dependency_policy_safety_invalid")
    heartbeat = leases.get("heartbeat_seconds")
    expiry = leases.get("expiry_seconds")
    quarantine = retention.get("quarantine_seconds")
    if type(heartbeat) is not int or type(expiry) is not int or type(quarantine) is not int or expiry < heartbeat * 2 or quarantine < expiry:
        raise BrokerError("dependency_policy_timing_invalid")
    if environment.get("ecosystem") not in install_policy.get("allowed_ecosystems", []):
        raise BrokerError("dependency_ecosystem_policy_denied")
    requested = environment.get("install_policy")
    if not isinstance(requested, Mapping):
        raise BrokerError("dependency_install_policy_invalid")
    if requested.get("network_on_cache_miss") and not install_policy.get("allow_network_on_cache_miss"):
        raise BrokerError("dependency_network_install_denied")
    if requested.get("lifecycle_scripts") and not install_policy.get("allow_lifecycle_scripts"):
        raise BrokerError("dependency_lifecycle_scripts_denied")
    if manifest.get("status") == "draft" and (
        purpose != "development" or not install_policy.get("allow_draft_development")
    ):
        raise BrokerError("dependency_ready_manifest_required")


def _runtime_fingerprint(environment: Mapping[str, Any]) -> dict[str, str]:
    ecosystem = str(environment.get("ecosystem"))
    if ecosystem == "python":
        runtime = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    elif ecosystem == "node":
        node = shutil.which("node")
        if not node:
            raise BrokerError("dependency_node_runtime_unavailable")
        try:
            completed = subprocess.run(
                [node, "--version"], check=True, capture_output=True, text=True, timeout=15
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise BrokerError("dependency_node_runtime_unavailable") from exc
        runtime = completed.stdout.strip().removeprefix("v")
    else:
        raise BrokerError("dependency_ecosystem_unsupported")
    architecture = platform.machine().lower()
    architecture = {"aarch64": "arm64", "amd64": "x86_64"}.get(
        architecture, architecture
    )
    fingerprint = {
        "ecosystem": ecosystem,
        "runtime": runtime,
        "os": platform.system().lower(),
        "architecture": architecture,
        "accelerator": str((environment.get("platform") or {}).get("accelerator") or "none"),
    }
    declared_platform = environment.get("platform")
    if not isinstance(declared_platform, Mapping):
        raise BrokerError("dependency_platform_invalid")
    declared_os = declared_platform.get("os", [])
    declared_architecture = declared_platform.get("architecture", [])
    if "any" not in declared_os and fingerprint["os"] not in declared_os:
        raise BrokerError("dependency_platform_os_incompatible")
    if "any" not in declared_architecture and fingerprint["architecture"] not in declared_architecture:
        raise BrokerError("dependency_platform_architecture_incompatible")
    accelerator = str(declared_platform.get("accelerator") or "none")
    if accelerator != "none":
        raise BrokerError("dependency_platform_accelerator_unavailable")
    constraint = str((environment.get("runtime") or {}).get("version_constraint") or "")
    if not _version_satisfies(runtime, constraint):
        raise BrokerError("dependency_runtime_version_incompatible")
    return fingerprint


def _version_tuple(value: str) -> tuple[int, ...] | None:
    match = re.fullmatch(r"v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:[-+].*)?", value.strip())
    if match is None:
        return None
    return tuple(int(part or 0) for part in match.groups())


def _version_satisfies(version: str, constraint: str) -> bool:
    """Evaluate the deliberately small runtime constraint dialect Build emits.

    Supported clauses are comma/space separated comparisons and ``||``
    alternatives, plus caret/tilde ranges. Unknown syntax fails closed instead
    of silently selecting an incompatible runtime.
    """
    actual = _version_tuple(version)
    if actual is None or not constraint.strip():
        return False
    for alternative in constraint.split("||"):
        tokens = [token for token in re.split(r"[\s,]+", alternative.strip()) if token]
        if not tokens:
            continue
        accepted = True
        for token in tokens:
            if token in {"*", "x", "X"}:
                continue
            match = re.fullmatch(r"(>=|<=|==|=|>|<|\^|~)?(v?\d+(?:\.\d+){0,2})", token)
            if match is None:
                accepted = False
                break
            operator = match.group(1) or "=="
            expected_text = match.group(2)
            expected = _version_tuple(expected_text)
            assert expected is not None
            components = len(expected_text.lstrip("v").split("."))
            if operator in {"==", "="}:
                ok = actual[:components] == expected[:components]
            elif operator == ">=":
                ok = actual >= expected
            elif operator == ">":
                ok = actual > expected
            elif operator == "<=":
                ok = actual <= expected
            elif operator == "<":
                ok = actual < expected
            elif operator == "^":
                upper = (expected[0] + 1, 0, 0)
                ok = actual >= expected and actual < upper
            else:  # ~
                upper = (
                    (expected[0], expected[1] + 1, 0)
                    if components > 1
                    else (expected[0] + 1, 0, 0)
                )
                ok = actual >= expected and actual < upper
            if not ok:
                accepted = False
                break
        if accepted:
            return True
    return False


def _cache_key(environment: Mapping[str, Any], fingerprint: Mapping[str, str]) -> str:
    return canonical_digest({
        "schema": "ze.dependency-cache-key.v1",
        "environment": dict(environment),
        "runtime_fingerprint": dict(sorted(fingerprint.items())),
    })


def _default_runner(command: Sequence[str], cwd: Path, environment: Mapping[str, str]) -> None:
    try:
        subprocess.run(
            list(command), cwd=str(cwd), env=dict(environment), check=True, shell=False
        )
    except FileNotFoundError as exc:
        raise BrokerError("dependency_installer_unavailable") from exc
    except subprocess.CalledProcessError as exc:
        raise BrokerError("dependency_install_failed") from exc


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _tree_size(root: Path) -> int:
    total = 0
    if root.is_symlink() or not root.exists():
        return 0
    for path in root.rglob("*"):
        try:
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
        except OSError:
            continue
    return total


class EnvironmentBroker:
    def __init__(
        self,
        state_root: Path,
        *,
        policy: Mapping[str, Any] | None = None,
        runner: Runner | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.root = state_root.expanduser().absolute()
        self.policy = dict(policy or default_policy())
        self.runner = runner or _default_runner
        self.clock = clock or utc_now
        self._initialize_root()

    def _initialize_root(self) -> None:
        if self.root.exists() and (self.root.is_symlink() or not self.root.is_dir()):
            raise BrokerError("dependency_cache_root_invalid")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        marker = self.root / ROOT_MARKER
        expected = {"schema": ROOT_SCHEMA, "owner": "ZE-skill-build", "cleanup_scope": "this-directory-only"}
        if marker.exists():
            if _json_file(marker) != expected:
                raise BrokerError("dependency_cache_root_marker_invalid")
        else:
            _atomic_json(marker, expected)
        for name in ("entries", "leases", "locks", "quarantine"):
            path = self.root / name
            if path.exists() and (path.is_symlink() or not path.is_dir()):
                raise BrokerError("dependency_cache_layout_invalid")
            path.mkdir(mode=0o700, exist_ok=True)
            path.chmod(0o700)

    def _entry(self, cache_key: str) -> Path:
        if not cache_key.startswith(_CACHE_KEY) or len(cache_key) != 71:
            raise BrokerError("dependency_cache_key_invalid")
        return self.root / "entries" / cache_key.removeprefix(_CACHE_KEY)

    def _recover_quarantine(self, cache_key: str) -> bool:
        destination = self._entry(cache_key)
        if destination.exists():
            return False
        candidates: list[tuple[datetime, Path]] = []
        for path in (self.root / "quarantine").iterdir():
            if path.is_symlink() or not path.is_dir():
                continue
            marker_path = path / QUARANTINE_MARKER
            try:
                marker = _json_file(marker_path)
                if (
                    marker.get("cache_key") == cache_key
                    and marker.get("reason") in {"idle", "capacity"}
                ):
                    candidates.append((_parse_timestamp(marker.get("quarantined_at")), path))
            except BrokerError:
                continue
        if not candidates:
            return False
        _, source = max(candidates, key=lambda item: item[0])
        (source / QUARANTINE_MARKER).unlink()
        os.replace(source, destination)
        return True

    def _ready_marker(self, entry: Path, cache_key: str) -> dict[str, Any] | None:
        marker_path = entry / ENTRY_MARKER
        if not marker_path.is_file() or marker_path.is_symlink():
            return None
        try:
            marker = _json_file(marker_path)
        except BrokerError:
            return None
        if marker.get("schema") != ENTRY_SCHEMA or marker.get("cache_key") != cache_key or marker.get("state") != "ready":
            return None
        environment = entry / "environment"
        return marker if environment.is_dir() and not environment.is_symlink() else None

    def _install_node(self, entry: Path, project_root: Path, environment: Mapping[str, Any]) -> None:
        npm = shutil.which("npm")
        if not npm:
            raise BrokerError("dependency_npm_unavailable")
        workspace = entry / "install-workspace"
        workspace.mkdir(mode=0o700)
        descriptor = environment["descriptor"]
        shutil.copy2(_safe_relative(project_root, descriptor["path"], file_required=True), workspace / "package.json")
        lock = environment.get("lock")
        command = [npm, "install", "--ignore-scripts", "--no-audit", "--no-fund"]
        if isinstance(lock, Mapping):
            if lock.get("format") != "npm-package-lock":
                raise BrokerError("dependency_node_lock_adapter_unavailable")
            shutil.copy2(_safe_relative(project_root, lock["path"], file_required=True), workspace / "package-lock.json")
            command[1] = "ci"
        if environment["install_policy"].get("dependency_scope") == "production":
            command.append("--omit=dev")
        process_environment = dict(os.environ)
        process_environment.update({
            "npm_config_ignore_scripts": "true", "npm_config_audit": "false", "npm_config_fund": "false"
        })
        self.runner(command, workspace, process_environment)
        installed = workspace / "node_modules"
        if not installed.is_dir() or installed.is_symlink():
            raise BrokerError("dependency_install_output_missing")
        os.replace(installed, entry / "environment")
        shutil.rmtree(workspace)

    def _install_python(self, entry: Path, project_root: Path, environment: Mapping[str, Any]) -> None:
        target = entry / "environment"
        self.runner([sys.executable, "-m", "venv", str(target)], entry, dict(os.environ))
        python = target / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if not python.is_file():
            raise BrokerError("dependency_install_output_missing")
        lock = environment.get("lock")
        source = lock if isinstance(lock, Mapping) else environment["descriptor"]
        if isinstance(lock, Mapping) and lock.get("format") not in {"pip-hashed-requirements"}:
            raise BrokerError("dependency_python_lock_adapter_unavailable")
        requirement = _safe_relative(project_root, source["path"], file_required=True)
        command = [
            str(python), "-m", "pip", "install", "--disable-pip-version-check", "--no-input"
        ]
        if environment["install_policy"].get("integrity") == "hashes":
            command.append("--require-hashes")
        command.extend(["-r", str(requirement)])
        self.runner(command, project_root, dict(os.environ))

    def _materialize(
        self,
        entry: Path,
        cache_key: str,
        manifest_digest: str,
        project_root: Path,
        environment: Mapping[str, Any],
        fingerprint: Mapping[str, str],
    ) -> None:
        if entry.exists():
            marker = self._ready_marker(entry, cache_key)
            if marker is None:
                failed = self.root / "quarantine" / f"failed-{cache_key.removeprefix(_CACHE_KEY)}-{uuid.uuid4().hex}"
                os.replace(entry, failed)
                _atomic_json(failed / QUARANTINE_MARKER, {
                    "schema": "ze.dependency-quarantine.v1",
                    "cache_key": cache_key,
                    "quarantined_at": _timestamp(self.clock()),
                    "reason": "incomplete-install",
                })
            else:
                return
        entry.mkdir(mode=0o700)
        installing = {
            "schema": ENTRY_SCHEMA,
            "cache_key": cache_key,
            "state": "installing",
            "manifest_digest": manifest_digest,
            "environment_id": environment["environment_id"],
            "runtime_fingerprint": dict(fingerprint),
            "retention_class": environment["retention_class"],
            "created_at": _timestamp(self.clock()),
            "last_used_at": _timestamp(self.clock()),
            "size_bytes": 0,
        }
        _atomic_json(entry / ENTRY_MARKER, installing)
        try:
            if environment["ecosystem"] == "node":
                self._install_node(entry, project_root, environment)
            elif environment["ecosystem"] == "python":
                self._install_python(entry, project_root, environment)
            else:
                raise BrokerError("dependency_ecosystem_unsupported")
            installing.update({
                "state": "ready",
                "last_used_at": _timestamp(self.clock()),
                "size_bytes": _tree_size(entry / "environment"),
            })
            _atomic_json(entry / ENTRY_MARKER, installing)
        except Exception:
            failed = self.root / "quarantine" / f"failed-{cache_key.removeprefix(_CACHE_KEY)}-{uuid.uuid4().hex}"
            if entry.exists():
                os.replace(entry, failed)
                _atomic_json(failed / QUARANTINE_MARKER, {
                    "schema": "ze.dependency-quarantine.v1",
                    "cache_key": cache_key,
                    "quarantined_at": _timestamp(self.clock()),
                    "reason": "install-failed",
                })
            raise

    def prepare(
        self,
        *,
        manifest_path: Path,
        project_root: Path,
        environment_id: str,
        purpose: str = "development",
        lease_id: str | None = None,
        consumer_kind: str = "local-session",
        scope: Mapping[str, str] | None = None,
    ) -> PreparedEnvironment:
        root = project_root.expanduser().resolve()
        if consumer_kind not in {"local-session", "build", "deployment"}:
            raise BrokerError("dependency_consumer_kind_invalid")
        manifest_file = manifest_path.expanduser().absolute()
        if manifest_file.is_symlink():
            raise BrokerError("dependency_manifest_symlink_forbidden")
        try:
            manifest_file.relative_to(root)
        except ValueError as exc:
            raise BrokerError("dependency_manifest_outside_project") from exc
        manifest = _json_file(manifest_file)
        environment = _manifest_environment(manifest, root, environment_id, purpose=purpose)
        _policy_check(self.policy, manifest, environment, purpose)
        fingerprint = _runtime_fingerprint(environment)
        cache_key = _cache_key(environment, fingerprint)
        manifest_digest = canonical_digest(manifest)
        key_lock = self.root / "locks" / (cache_key.removeprefix(_CACHE_KEY) + ".lock")
        reused = False
        # Sweep owns the global lifecycle lock. Holding it until the lease is
        # durable closes the install-ready-but-not-yet-leased race.
        with _exclusive_lock(self.root / "locks" / "sweep.lock"):
            with _exclusive_lock(key_lock):
                entry = self._entry(cache_key)
                if self._ready_marker(entry, cache_key) is None:
                    reused = self._recover_quarantine(cache_key)
                else:
                    reused = True
                if self._ready_marker(entry, cache_key) is None:
                    self._materialize(entry, cache_key, manifest_digest, root, environment, fingerprint)
                marker = self._ready_marker(entry, cache_key)
                if marker is None:
                    raise BrokerError("dependency_entry_not_ready")
                marker["last_used_at"] = _timestamp(self.clock())
                _atomic_json(entry / ENTRY_MARKER, marker)
                link = _safe_consumer_link(root, environment["materialization"]["link_path"])
                link.parent.mkdir(parents=True, exist_ok=True)
                target = entry / "environment"
                if link.is_symlink():
                    try:
                        if link.resolve(strict=True) != target.resolve(strict=True):
                            raise BrokerError("dependency_consumer_path_occupied")
                    except OSError as exc:
                        raise BrokerError("dependency_consumer_path_occupied") from exc
                elif link.exists():
                    raise BrokerError("dependency_consumer_path_occupied")
                else:
                    link.symlink_to(target, target_is_directory=True)
                identifier = lease_id or "lease_" + uuid.uuid4().hex
                if not isinstance(identifier, str) or not re_fullmatch_lease(identifier):
                    raise BrokerError("dependency_lease_id_invalid")
                now = self.clock()
                expiry = now + timedelta(seconds=int(self.policy["leases"]["expiry_seconds"]))
                consumer_id = canonical_digest({"root": str(root), "link": str(link.relative_to(root))})
                lease: dict[str, Any] = {
                    "schema": "ze.dependency-lease.v1",
                    "lease_id": identifier,
                    "cache_key": cache_key,
                    "manifest_digest": manifest_digest,
                    "environment_id": environment_id,
                    "consumer": {"kind": consumer_kind, "id": consumer_id},
                    "state": "active",
                    "acquired_at": _timestamp(now),
                    "heartbeat_at": _timestamp(now),
                    "expires_at": _timestamp(expiry),
                }
                if scope is not None:
                    lease["scope"] = dict(scope)
                _atomic_json(self.root / "leases" / f"{identifier}.json", lease)
        if self.policy.get("automatic_sweep") is True:
            self.sweep(confirm=True)
        return PreparedEnvironment(environment_id, cache_key, identifier, link, self._entry(cache_key) / "environment", reused)

    def heartbeat(self, lease_id: str) -> None:
        path = self.root / "leases" / f"{lease_id}.json"
        with _exclusive_lock(self.root / "locks" / "sweep.lock"):
            lease = _json_file(path)
            if lease.get("lease_id") != lease_id or lease.get("state") != "active":
                raise BrokerError("dependency_lease_invalid")
            now = self.clock()
            lease["heartbeat_at"] = _timestamp(now)
            lease["expires_at"] = _timestamp(
                now + timedelta(seconds=int(self.policy["leases"]["expiry_seconds"]))
            )
            _atomic_json(path, lease)

    def release(self, prepared: PreparedEnvironment, *, unlink: bool = True) -> None:
        lease_path = self.root / "leases" / f"{prepared.lease_id}.json"
        with _exclusive_lock(self.root / "locks" / "sweep.lock"):
            lease = _json_file(lease_path)
            if lease.get("cache_key") != prepared.cache_key:
                raise BrokerError("dependency_lease_cache_mismatch")
            consumer = lease.get("consumer")
            other_consumer_lease = False
            if isinstance(consumer, Mapping):
                now = self.clock()
                for candidate in (self.root / "leases").glob("lease_*.json"):
                    if candidate == lease_path:
                        continue
                    try:
                        other = _json_file(candidate)
                        if (
                            other.get("state") == "active"
                            and other.get("cache_key") == prepared.cache_key
                            and other.get("consumer") == consumer
                            and _parse_timestamp(other.get("expires_at")) > now
                        ):
                            other_consumer_lease = True
                            break
                    except BrokerError:
                        continue
            if unlink and not other_consumer_lease and prepared.link_path.is_symlink():
                try:
                    target = prepared.link_path.resolve(strict=True)
                    target.relative_to((self.root / "entries").resolve(strict=True))
                except (OSError, ValueError) as exc:
                    raise BrokerError("dependency_consumer_link_unmanaged") from exc
                prepared.link_path.unlink()
            lease["state"] = "released"
            lease["heartbeat_at"] = _timestamp(self.clock())
            lease["expires_at"] = _timestamp(self.clock())
            _atomic_json(lease_path, lease)
            entry = self._entry(prepared.cache_key)
            marker = self._ready_marker(entry, prepared.cache_key)
            if marker is not None:
                marker["last_used_at"] = _timestamp(self.clock())
                _atomic_json(entry / ENTRY_MARKER, marker)
        if self.policy.get("automatic_sweep") is True:
            self.sweep(confirm=True)

    def _active_keys(self, now: datetime, *, confirm: bool) -> tuple[set[str], int]:
        active: set[str] = set()
        expired = 0
        for path in (self.root / "leases").glob("lease_*.json"):
            try:
                lease = _json_file(path)
                is_active = lease.get("state") == "active" and _parse_timestamp(lease.get("expires_at")) > now
                if is_active:
                    active.add(str(lease.get("cache_key")))
                else:
                    expired += 1
                    if confirm:
                        path.unlink()
            except (BrokerError, OSError):
                expired += 1
        return active, expired

    def _quarantine(self, entry: Path, marker: Mapping[str, Any], *, reason: str, confirm: bool) -> str:
        cache_key = str(marker["cache_key"])
        destination = self.root / "quarantine" / (
            cache_key.removeprefix(_CACHE_KEY) + "-" + self.clock().strftime("%Y%m%dT%H%M%S")
        )
        if confirm:
            os.replace(entry, destination)
            _atomic_json(destination / QUARANTINE_MARKER, {
                "schema": "ze.dependency-quarantine.v1",
                "cache_key": cache_key,
                "quarantined_at": _timestamp(self.clock()),
                "reason": reason,
            })
        return str(destination)

    def sweep(self, *, confirm: bool = False) -> dict[str, Any]:
        with _exclusive_lock(self.root / "locks" / "sweep.lock"):
            now = self.clock()
            active, expired_lease_count = self._active_keys(now, confirm=confirm)
            entries: list[tuple[datetime, Path, dict[str, Any]]] = []
            invalid_entries = 0
            for entry in (self.root / "entries").iterdir():
                if entry.is_symlink() or not entry.is_dir():
                    invalid_entries += 1
                    continue
                marker = self._ready_marker(entry, _CACHE_KEY + entry.name)
                if marker is None:
                    invalid_entries += 1
                    continue
                try:
                    last_used = _parse_timestamp(marker.get("last_used_at"))
                except BrokerError:
                    invalid_entries += 1
                    continue
                entries.append((last_used, entry, marker))
            total_bytes = sum(int(marker.get("size_bytes") or 0) for _, _, marker in entries)
            max_bytes = int(self.policy["budget"]["max_cache_bytes"])
            try:
                free_bytes = shutil.disk_usage(self.root).free
            except OSError:
                free_bytes = max_bytes
            minimum_free = int(self.policy["budget"]["minimum_free_bytes"])
            pressure = total_bytes > max_bytes or free_bytes < minimum_free
            quarantined: list[dict[str, Any]] = []
            retained_bytes = total_bytes
            idle_fields = {
                "standard": "standard_idle_seconds",
                "large": "large_idle_seconds",
                "accelerator": "accelerator_idle_seconds",
            }
            for last_used, entry, marker in sorted(entries, key=lambda row: row[0]):
                cache_key = str(marker["cache_key"])
                if cache_key in active:
                    continue
                retention_class = str(marker.get("retention_class") or "standard")
                idle_seconds = int(self.policy["retention"][idle_fields.get(retention_class, "standard_idle_seconds")])
                idle = (now - last_used).total_seconds() >= idle_seconds
                budget_needed = retained_bytes > max_bytes or free_bytes < minimum_free
                if not idle and not budget_needed:
                    continue
                reason = "idle" if idle else "capacity"
                destination = self._quarantine(entry, marker, reason=reason, confirm=confirm)
                size = int(marker.get("size_bytes") or 0)
                retained_bytes = max(0, retained_bytes - size)
                free_bytes += size
                quarantined.append({"cache_key": cache_key, "reason": reason, "size_bytes": size, "destination": destination})
            deleted: list[dict[str, Any]] = []
            recovery_seconds = int(self.policy["retention"]["quarantine_seconds"])
            for path in (self.root / "quarantine").iterdir():
                if path.is_symlink() or not path.is_dir():
                    continue
                marker_path = path / QUARANTINE_MARKER
                try:
                    marker = _json_file(marker_path)
                    quarantined_at = _parse_timestamp(marker.get("quarantined_at"))
                except BrokerError:
                    continue
                if (now - quarantined_at).total_seconds() < recovery_seconds:
                    continue
                cache_key = str(marker.get("cache_key") or "")
                if cache_key in active:
                    continue
                deleted.append({"cache_key": cache_key, "path": str(path)})
                if confirm:
                    # Marker + direct child of the owned quarantine root are
                    # both required immediately before deletion.
                    path.resolve(strict=True).relative_to((self.root / "quarantine").resolve(strict=True))
                    marker_path.unlink()
                    shutil.rmtree(path)
            return {
                "schema": "ze.dependency-sweep-result.v1",
                "confirmed": confirm,
                "active_cache_keys": len(active),
                "expired_leases": expired_lease_count,
                "invalid_entries_ignored": invalid_entries,
                "cache_bytes_before": total_bytes,
                "capacity_pressure": pressure,
                "quarantined": quarantined,
                "deleted": deleted,
            }

    def status(self) -> dict[str, Any]:
        return self.sweep(confirm=False)


def re_fullmatch_lease(value: str) -> bool:
    return len(value) == 38 and value.startswith("lease_") and all(
        char in "0123456789abcdef" for char in value[6:]
    )


class LeaseHeartbeat:
    def __init__(self, broker: EnvironmentBroker, leases: Sequence[PreparedEnvironment]) -> None:
        self.broker = broker
        self.leases = list(leases)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "LeaseHeartbeat":
        interval = int(self.broker.policy["leases"]["heartbeat_seconds"])

        def pulse() -> None:
            while not self._stop.wait(interval):
                for prepared in self.leases:
                    try:
                        self.broker.heartbeat(prepared.lease_id)
                    except BrokerError:
                        self._stop.set()
                        return

        self._thread = threading.Thread(target=pulse, name="ze-dependency-heartbeat", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)


def load_policy(path: Path | None) -> dict[str, Any]:
    if path is None:
        return default_policy()
    expanded = path.expanduser().absolute()
    if expanded.is_symlink():
        raise BrokerError("dependency_policy_symlink_forbidden")
    return _json_file(expanded)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-root", help="private broker root; defaults to the OS user cache")
    parser.add_argument("--policy", help="optional ze.dependency-policy.v1 JSON")
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--project", required=True)
    prepare.add_argument("--manifest", default=MANIFEST_NAME)
    prepare.add_argument("--environment", required=True)
    prepare.add_argument("--purpose", default="development", choices=["development", "build", "test", "runtime"])
    sweep = sub.add_parser("sweep")
    sweep.add_argument("--confirm", action="store_true")
    sub.add_parser("status")
    args = parser.parse_args()
    try:
        state_root = Path(args.cache_root).expanduser().absolute() if args.cache_root else default_state_root()
        broker = EnvironmentBroker(state_root, policy=load_policy(Path(args.policy) if args.policy else None))
        if args.command == "prepare":
            project = Path(args.project).expanduser().resolve()
            manifest = Path(args.manifest)
            if not manifest.is_absolute():
                manifest = project / manifest
            result = broker.prepare(
                manifest_path=manifest,
                project_root=project,
                environment_id=args.environment,
                purpose=args.purpose,
            )
            payload: Any = {
                "environment_id": result.environment_id,
                "cache_key": result.cache_key,
                "lease_id": result.lease_id,
                "link_path": str(result.link_path),
                "reused": result.reused,
            }
        elif args.command == "sweep":
            payload = broker.sweep(confirm=args.confirm)
        else:
            payload = broker.status()
    except BrokerError as exc:
        print(json.dumps({"ok": False, "error": exc.code}, ensure_ascii=False))
        return 2
    print(json.dumps({"ok": True, "result": payload}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
