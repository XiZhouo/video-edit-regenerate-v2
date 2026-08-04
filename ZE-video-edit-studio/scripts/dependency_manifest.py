#!/usr/bin/env python3
from __future__ import annotations

"""Produce the environment-neutral dependency declaration for one Skill.

The producer never installs packages and never records an absolute path,
credential, registry token, server address or Series identity.  ``draft`` is a
usable local-development state; release/deployment gates require ``ready``.
"""

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping


MANIFEST_NAME = "ZE_DEPENDENCIES.json"
SCHEMA = "ze.dependency-manifest.v1"
REGENERABLE_DIRECTORY_NAMES = {
    "node_modules", ".venv", "dist", ".vite-temp", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache",
}
REGENERABLE_FILE_NAMES = {".DS_Store"}
_HASH = re.compile(r"--hash=sha256:[0-9a-fA-F]{16,}")
_PIN = re.compile(r"^[A-Za-z0-9_.-]+(?:\[[A-Za-z0-9_,.-]+\])?==[^\s;]+")


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _file_ref(root: Path, path: Path, format_name: str) -> dict[str, str]:
    return {"path": _relative(root, path), "digest": file_digest(path), "format": format_name}


def _json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JSON dependency descriptor: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"dependency descriptor must be an object: {path}")
    return value


def _npm_lock_ready(path: Path) -> bool:
    value = _json_object(path)
    version = value.get("lockfileVersion")
    packages = value.get("packages")
    if type(version) is not int or version < 2 or not isinstance(packages, Mapping):
        return False
    for package_path, raw in packages.items():
        if not package_path or not isinstance(raw, Mapping):
            continue
        resolved = raw.get("resolved")
        if isinstance(resolved, str) and resolved.startswith(("http://", "https://")):
            integrity = raw.get("integrity")
            if not isinstance(integrity, str) or not integrity:
                return False
    return True


def _hashed_requirements_ready(path: Path) -> bool:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return False
    logical: list[str] = []
    current = ""
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        current = (current + " " + line.rstrip("\\")).strip()
        if line.endswith("\\"):
            continue
        logical.append(current)
        current = ""
    if current:
        logical.append(current)
    requirements = [line for line in logical if not line.startswith(("-", "--"))]
    return bool(requirements) and all(_PIN.search(line) and _HASH.search(line) for line in requirements)


def _python_constraint(root: Path) -> str:
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file() or pyproject.is_symlink():
        return ">=3.9,<3.14"
    try:
        import tomllib

        value = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        constraint = (value.get("project") or {}).get("requires-python")
    except (OSError, UnicodeError, ValueError):
        return ">=3.9,<3.14"
    return constraint if isinstance(constraint, str) and constraint else ">=3.9,<3.14"


def _node_constraint(package: Mapping[str, Any]) -> str:
    engines = package.get("engines")
    value = engines.get("node") if isinstance(engines, Mapping) else None
    return value if isinstance(value, str) and value else ">=20"


def _python_environment(root: Path) -> tuple[dict[str, Any] | None, bool]:
    descriptor = root / "requirements.txt"
    if not descriptor.is_file() or descriptor.is_symlink():
        return None, True
    lock: tuple[Path, str, str] | None = None
    candidates = (
        (root / "requirements.lock", "pip-hashed-requirements", "hashes"),
    )
    for candidate, format_name, integrity in candidates:
        if not candidate.is_file() or candidate.is_symlink():
            continue
        if format_name == "pip-hashed-requirements" and not _hashed_requirements_ready(candidate):
            continue
        lock = (candidate, format_name, integrity)
        break
    ready = lock is not None
    environment: dict[str, Any] = {
        "environment_id": "python-runtime",
        "ecosystem": "python",
        "purposes": ["development", "runtime", "test"],
        "artifact_roles": ["runtime", "server"],
        "working_directory": ".",
        "descriptor": _file_ref(root, descriptor, "python-requirements"),
        "runtime": {"family": "python", "version_constraint": _python_constraint(root)},
        "platform": {
            "os": ["darwin", "linux", "windows"],
            "architecture": ["arm64", "x86_64"],
            "accelerator": "none",
        },
        "materialization": {
            "strategy": "adapter-selected",
            "supported_strategies": [
                "content-addressed-link", "provider-bundled", "target-managed"
            ],
            "cacheable": True,
            "link_path": ".venv",
        },
        "install_policy": {
            "network_on_cache_miss": True,
            "lifecycle_scripts": False,
            "integrity": lock[2] if lock else "descriptor-only",
            "dependency_scope": "all",
        },
        "retention_class": "large",
    }
    if lock:
        environment["lock"] = _file_ref(root, lock[0], lock[1])
    return environment, ready


def _node_environments(root: Path) -> tuple[list[dict[str, Any]], bool]:
    rows: list[dict[str, Any]] = []
    all_ready = True
    candidates = [root]
    webapp = root / "webapp"
    if webapp.is_dir() and not webapp.is_symlink():
        candidates.append(webapp)
    for working in candidates:
        descriptor = working / "package.json"
        if not descriptor.is_file() or descriptor.is_symlink():
            continue
        package = _json_object(descriptor)
        has_node_work = any(
            isinstance(package.get(field), Mapping) and package.get(field)
            for field in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies", "scripts")
        )
        if not has_node_work:
            continue
        lock_path = working / "package-lock.json"
        ready = lock_path.is_file() and not lock_path.is_symlink() and _npm_lock_ready(lock_path)
        all_ready = all_ready and ready
        relative_working = _relative(root, working) if working != root else "."
        prefix = "root" if working == root else relative_working.replace("/", "-")
        environment: dict[str, Any] = {
            "environment_id": f"{prefix}-node",
            "ecosystem": "node",
            "purposes": sorted({
                "build", "development", "test",
                *(
                    ["runtime"]
                    if working == root
                    and isinstance(package.get("dependencies"), Mapping)
                    and package.get("dependencies")
                    else []
                ),
            }),
            "artifact_roles": (
                ["runtime", "server"]
                if working == root
                else ["client", "runtime", "server"]
            ),
            "working_directory": relative_working,
            "descriptor": _file_ref(root, descriptor, "package-json"),
            "runtime": {"family": "node", "version_constraint": _node_constraint(package)},
            "platform": {
                "os": ["darwin", "linux", "windows"],
                "architecture": ["arm64", "x86_64"],
                "accelerator": "none",
            },
            "materialization": {
                "strategy": "adapter-selected",
                "supported_strategies": [
                    "content-addressed-link", "provider-bundled", "target-managed"
                ],
                "cacheable": True,
                "link_path": "node_modules" if working == root else f"{relative_working}/node_modules",
            },
            "install_policy": {
                "network_on_cache_miss": True,
                "lifecycle_scripts": False,
                "integrity": "lockfile" if ready else "descriptor-only",
                "dependency_scope": "all",
            },
            "retention_class": "large",
        }
        if ready:
            environment["lock"] = _file_ref(root, lock_path, "npm-package-lock")
        rows.append(environment)
    return rows, all_ready


def build_manifest(project_root: Path, *, skill_id: str | None = None) -> dict[str, Any]:
    root = project_root.expanduser().resolve()
    if not root.is_dir() or root.is_symlink():
        raise ValueError("project root must be a regular directory")
    python_environment, python_ready = _python_environment(root)
    node_environments, node_ready = _node_environments(root)
    environments = node_environments
    if python_environment is not None:
        environments.append(python_environment)
    environments.sort(key=lambda row: str(row["environment_id"]))
    identifier = skill_id or root.name
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,127}", identifier):
        raise ValueError("skill_id is invalid")
    ready = python_ready and node_ready
    return {
        "schema": SCHEMA,
        "skill_id": identifier,
        "status": "ready" if ready else "draft",
        "generated_by": "ZE-skill-build",
        "environments": environments,
    }


def audit_regenerable_paths(project_root: Path) -> list[dict[str, Any]]:
    root = project_root.expanduser().resolve()
    rows: list[dict[str, Any]] = []
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        kept: list[str] = []
        for name in sorted(dirnames):
            path = current_path / name
            if name in REGENERABLE_DIRECTORY_NAMES:
                size = 0
                if not path.is_symlink():
                    for child in path.rglob("*"):
                        try:
                            if child.is_file() and not child.is_symlink():
                                size += child.stat().st_size
                        except OSError:
                            continue
                rows.append({
                    "path": _relative(root, path),
                    "kind": "managed-link" if path.is_symlink() else "unmanaged-regenerable",
                    "size_bytes": size,
                })
                continue
            kept.append(name)
        dirnames[:] = kept
        for name in filenames:
            if name in REGENERABLE_FILE_NAMES or name.endswith((".pyc", ".log")):
                path = current_path / name
                try:
                    size = path.stat().st_size if not path.is_symlink() else 0
                except OSError:
                    size = 0
                rows.append({"path": _relative(root, path), "kind": "regenerable-file", "size_bytes": size})
    return sorted(rows, key=lambda row: str(row["path"]))


def write_manifest(path: Path, manifest: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8") + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", nargs="?", default=".")
    parser.add_argument("--write", action="store_true", help=f"write {MANIFEST_NAME}")
    parser.add_argument("--audit", action="store_true", help="report regenerable paths without deleting")
    args = parser.parse_args(list(argv) if argv is not None else None)
    root = Path(args.project).expanduser().resolve()
    try:
        manifest = build_manifest(root)
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    result: dict[str, Any] = {"ok": True, "manifest": manifest}
    if args.audit:
        result["regenerable_paths"] = audit_regenerable_paths(root)
    if args.write:
        output = root / MANIFEST_NAME
        write_manifest(output, manifest)
        result["written"] = str(output)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
