#!/usr/bin/env python3
"""Build, sign and verify environment-neutral ZE Base Artifacts.

This is the executable half of the Build standard.  ``deploy.json`` declares
capabilities, ``ZE_DEPENDENCIES.json`` declares reproducible environments, and
``ZE_ARTIFACT_BUILD.json`` selects target-neutral variants.  This command
materializes build-only dependencies through the Environment Broker, runs the
declared build step without a shell, creates deterministic ZIP/SBOM/signature
outputs, writes ``ZE_ARTIFACT_VARIANT.json`` last, and re-validates the entire
closure before reporting success.

It never accepts a Series, Organization, target host, port, provider account or
credential value.  The signing private key must remain outside the Skill root.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

from artifact_variant import (
    MANIFEST_NAME,
    MANIFEST_SCHEMA,
    RUNTIME_PACKAGE_MEMBER,
    RUNTIME_PACKAGE_SCHEMA,
    _schema_errors,
    base_build_key,
    build_source_inventory,
    canonical_digest,
    canonical_json_bytes,
    evaluate_artifact_variant,
    file_digest,
    sign_artifact_digest,
    variant_inputs_digest,
)
from dependency_manifest import MANIFEST_NAME as DEPENDENCY_MANIFEST_NAME
from deployment_profile import RECIPE_SCHEMA, evaluate_deployment_profile, load_recipe
from environment_broker import (
    BrokerError,
    EnvironmentBroker,
    LeaseHeartbeat,
    default_state_root,
)


BUILD_DECLARATION_NAME = "ZE_ARTIFACT_BUILD.json"
BUILD_DECLARATION_SCHEMA = "ze.artifact-build.v1"
BUILD_DECLARATION_SCHEMA_PATH = (
    Path(__file__).resolve().parents[1]
    / "contracts"
    / "artifact"
    / f"{BUILD_DECLARATION_SCHEMA}.schema.json"
)
SOURCE_INVENTORY_PATH = "build-inputs/source-inventory.json"
_CHANNEL_ORDER = {"stable": 0, "beta": 1, "pinned": 2}
_MAX_MEMBER_BYTES = 32 * 1024 * 1024
_MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
_EXECUTABLE_ROLES = {"runtime", "server", "local-adapter"}
_CLOUDFLARE_WORKERS = {
    "worker-js", "worker-typescript", "worker-python",
    "cloudflare-worker-js", "cloudflare-worker-python",
}
_CLOUDFLARE_STATIC = {"static-web", "cloudflare-static-assets"}
_BROAD_SKIP_DIRS = {
    ".git", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".venv",
    ".vite-temp", "__pycache__", "node_modules", "artifacts", "catalog",
    "coverage", "dist", "release", "build-output", ".data",
}
_FORBIDDEN_PARTS = {
    ".git", ".venv", "__pycache__", "node_modules", "artifacts", "catalog",
    ".data", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".vite-temp",
}
_FORBIDDEN_FILES = {MANIFEST_NAME, "ZE_BUILD_EVIDENCE.json", ".DS_Store"}
_SECRET_KEY = re.compile(
    r"(?:^|_)(?:password|passwd|token|secret|private_key|client_secret|api_key)(?:$|_)",
    re.IGNORECASE,
)
_SHELL_META = re.compile(r"[;&|`$<>\n\r]")
_MODE_ROLES: dict[str, frozenset[str]] = {
    "local-full": frozenset({"runtime"}),
    "self-hosted-full": frozenset({"server"}),
    "creator-hosted-api": frozenset({"server", "client"}),
    "frontend-only": frozenset({"client"}),
    "hybrid": frozenset({"server", "local-adapter"}),
    "evidence-only": frozenset({"evidence"}),
}


class ReleaseBuildError(RuntimeError):
    def __init__(self, code: str, detail: str | None = None) -> None:
        self.code = code
        self.detail = detail or code
        super().__init__(self.detail)


class DuplicateKeyError(ValueError):
    pass


def _pairs(rows: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in rows:
        if key in result:
            raise DuplicateKeyError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_json(path: Path, *, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ReleaseBuildError(f"{label}_missing", f"{label} must be a regular file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_pairs)
    except (OSError, UnicodeError, json.JSONDecodeError, DuplicateKeyError) as exc:
        raise ReleaseBuildError(f"{label}_invalid", f"invalid {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise ReleaseBuildError(f"{label}_invalid", f"{label} must contain an object")
    return value


def _canonical_relative(value: Any, *, allow_dot: bool = False) -> str:
    if not isinstance(value, str):
        raise ReleaseBuildError("path_invalid", "path must be a string")
    raw = value.strip().replace("\\", "/").rstrip("/")
    if allow_dot and raw == ".":
        return raw
    pure = PurePosixPath(raw)
    if (
        not raw
        or raw.startswith("/")
        or pure.is_absolute()
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise ReleaseBuildError("path_invalid", f"path must be canonical and relative: {value}")
    return pure.as_posix()


def _inside(root: Path, relative: str, *, required: bool = True) -> Path:
    candidate = root if relative == "." else root.joinpath(*PurePosixPath(relative).parts)
    current = root
    for part in (() if relative == "." else PurePosixPath(relative).parts):
        current = current / part
        if current.is_symlink():
            raise ReleaseBuildError("symlink_forbidden", f"path traverses a symlink: {relative}")
    try:
        resolved = candidate.resolve(strict=required)
        resolved.relative_to(root.resolve(strict=True))
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise ReleaseBuildError("path_outside_root", f"path is missing or outside Skill root: {relative}") from exc
    return resolved


def _atomic_bytes(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    if path.is_symlink():
        raise ReleaseBuildError("output_symlink_forbidden", f"output must not be a symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _write_output(path: Path, payload: bytes, *, force: bool, mode: int = 0o600) -> None:
    if path.exists() and not path.is_symlink():
        try:
            if path.is_file() and path.read_bytes() == payload:
                return
        except OSError:
            pass
        if not force:
            raise ReleaseBuildError(
                "output_conflict",
                f"output already exists with different bytes; pass --force: {path}",
            )
    _atomic_bytes(path, payload, mode=mode)


def _restore(path: Path, previous: bytes | None) -> None:
    if previous is None:
        if path.is_file() and not path.is_symlink():
            path.unlink()
        return
    _atomic_bytes(path, previous)


def _validate_build_declaration(
    value: Mapping[str, Any], recipe: Mapping[str, Any] | None = None
) -> None:
    errors = _schema_errors(value, BUILD_DECLARATION_SCHEMA_PATH)
    if errors:
        raise ReleaseBuildError("artifact_build_declaration_invalid", "; ".join(errors))
    variants = value["variants"]
    variant_ids = [row["variant_id"] for row in variants]
    if variant_ids != sorted(variant_ids) or len(variant_ids) != len(set(variant_ids)):
        raise ReleaseBuildError(
            "artifact_build_variant_order_invalid",
            "variants must be unique and sorted by variant_id",
        )
    channels = value["channel_eligibility"]
    if channels != sorted(channels, key=lambda item: _CHANNEL_ORDER[item]):
        raise ReleaseBuildError(
            "artifact_build_channel_order_invalid",
            "channel_eligibility must use stable, beta, pinned canonical order",
        )
    for row in variants:
        if row["feature_set"] != sorted(row["feature_set"]):
            raise ReleaseBuildError("artifact_build_feature_order_invalid")
        if row["payload_paths"] != sorted(row["payload_paths"]):
            raise ReleaseBuildError("artifact_build_payload_order_invalid")
        package = row.get("runtime_package")
        runtime_variant = row["runtime_variant"]
        requires_package = runtime_variant in _CLOUDFLARE_WORKERS | _CLOUDFLARE_STATIC
        if requires_package != isinstance(package, Mapping):
            raise ReleaseBuildError(
                "artifact_build_runtime_package_invalid",
                f"{row['variant_id']} runtime_package presence disagrees with runtime_variant",
            )
        if isinstance(package, Mapping):
            bindings = package["bindings"]
            keys = [(item["kind"], item["name"]) for item in bindings]
            if keys != sorted(keys) or len(keys) != len(set(keys)):
                raise ReleaseBuildError("artifact_build_binding_order_invalid")
    if recipe is None:
        return
    declared_modes = set(recipe.get("delivery_modes") or [])
    roles_by_mode: dict[str, set[str]] = {}
    allowed_providers = set(
        ((recipe.get("provider_requirements") or {}).get("allowed") or [])
    )
    for row in variants:
        mode = str(row["delivery_mode"])
        role = str(row["artifact_role"])
        if mode not in declared_modes:
            raise ReleaseBuildError(
                "artifact_build_delivery_mode_undeclared",
                f"variant {row['variant_id']} uses undeclared delivery mode {mode}",
            )
        if role not in _MODE_ROLES.get(mode, frozenset()):
            raise ReleaseBuildError(
                "artifact_build_role_invalid",
                f"artifact role {role} is invalid for {mode}",
            )
        roles_by_mode.setdefault(mode, set()).add(role)
        runtime_variant = str(row["runtime_variant"])
        if runtime_variant in _CLOUDFLARE_WORKERS and "cloudflare-workers" not in allowed_providers:
            raise ReleaseBuildError("artifact_build_worker_provider_undeclared")
        if runtime_variant in _CLOUDFLARE_STATIC and "cloudflare-pages" not in allowed_providers:
            raise ReleaseBuildError("artifact_build_pages_provider_undeclared")
    for mode in sorted(declared_modes):
        missing = sorted(_MODE_ROLES.get(mode, frozenset()) - roles_by_mode.get(mode, set()))
        if missing:
            raise ReleaseBuildError(
                "artifact_build_delivery_matrix_incomplete",
                f"{mode} misses artifact role(s): {', '.join(missing)}",
            )


def evaluate_build_declaration(
    root: Path, recipe: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Validate the declared build matrix without installing or building."""

    resolved = root.expanduser().resolve()
    loaded_recipe = dict(recipe) if recipe is not None else load_recipe(resolved)
    path = resolved / BUILD_DECLARATION_NAME
    if not path.exists():
        if loaded_recipe.get("schema") == RECIPE_SCHEMA:
            return {
                "valid": False,
                "errors": [f"portable recipe v3 requires {BUILD_DECLARATION_NAME}"],
                "artifact_build": {"schema": BUILD_DECLARATION_SCHEMA, "status": "required"},
            }
        return {
            "valid": True,
            "errors": [],
            "warnings": [f"{BUILD_DECLARATION_NAME} absent: legacy/v2 Build remains readable"],
            "artifact_build": {"schema": BUILD_DECLARATION_SCHEMA, "status": "legacy-unscoped"},
        }
    try:
        value = _load_json(path, label="artifact_build_declaration")
        _validate_build_declaration(value, loaded_recipe)
    except ReleaseBuildError as exc:
        return {
            "valid": False,
            "errors": [exc.detail],
            "artifact_build": {"schema": BUILD_DECLARATION_SCHEMA, "status": "invalid"},
        }
    return {
        "valid": True,
        "errors": [],
        "warnings": [],
        "artifact_build": {
            "schema": BUILD_DECLARATION_SCHEMA,
            "status": "ready",
            "declaration_digest": file_digest(path),
            "version": value["version"],
            "variant_count": len(value["variants"]),
            "variants": [
                {
                    "variant_id": row["variant_id"],
                    "runtime_variant": row["runtime_variant"],
                    "delivery_mode": row["delivery_mode"],
                    "artifact_role": row["artifact_role"],
                }
                for row in value["variants"]
            ],
        },
    }


def _recursive_secret_key(value: Any, *, path: str = "$") -> str | None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if _SECRET_KEY.search(str(key)):
                return f"{path}.{key}"
            finding = _recursive_secret_key(child, path=f"{path}.{key}")
            if finding:
                return finding
    elif isinstance(value, list):
        for index, child in enumerate(value):
            finding = _recursive_secret_key(child, path=f"{path}[{index}]")
            if finding:
                return finding
    return None


def _load_ready_dependencies(root: Path, recipe: Mapping[str, Any]) -> dict[str, Any]:
    path = root / DEPENDENCY_MANIFEST_NAME
    manifest = _load_json(path, label="dependency_manifest")
    if manifest.get("schema") != "ze.dependency-manifest.v1":
        raise ReleaseBuildError("dependency_manifest_schema_invalid")
    if manifest.get("status") != "ready":
        raise ReleaseBuildError(
            "dependency_manifest_not_ready",
            "release build requires a ready ZE_DEPENDENCIES.json with immutable locks",
        )
    if manifest.get("generated_by") != "ZE-skill-build":
        raise ReleaseBuildError("dependency_manifest_producer_invalid")
    if manifest.get("skill_id") != recipe.get("skill_id"):
        raise ReleaseBuildError("dependency_manifest_skill_id_mismatch")
    if manifest.get("skill_uid") not in {None, recipe.get("skill_uid")}:
        raise ReleaseBuildError("dependency_manifest_skill_uid_mismatch")
    secret_path = _recursive_secret_key(manifest)
    if secret_path:
        raise ReleaseBuildError(
            "dependency_manifest_secret_field_forbidden",
            f"dependency manifest contains a credential-shaped key: {secret_path}",
        )
    rows = manifest.get("environments")
    if not isinstance(rows, list):
        raise ReleaseBuildError("dependency_manifest_environments_invalid")
    identifiers: list[str] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise ReleaseBuildError("dependency_manifest_environment_invalid")
        identifier = row.get("environment_id")
        if not isinstance(identifier, str):
            raise ReleaseBuildError("dependency_manifest_environment_invalid")
        identifiers.append(identifier)
        ecosystem = row.get("ecosystem")
        for field in ("descriptor", "lock"):
            reference = row.get(field)
            if reference is None:
                if field == "lock" and ecosystem == "provider-managed":
                    continue
                raise ReleaseBuildError(
                    "dependency_manifest_lock_missing",
                    f"ready environment {identifier} requires {field}",
                )
            if not isinstance(reference, Mapping):
                raise ReleaseBuildError("dependency_manifest_file_ref_invalid")
            relative = _canonical_relative(reference.get("path"), allow_dot=False)
            file_path = _inside(root, relative)
            if not file_path.is_file() or file_digest(file_path) != reference.get("digest"):
                raise ReleaseBuildError(
                    "dependency_manifest_file_digest_mismatch",
                    f"dependency input changed: {relative}",
                )
        materialization = row.get("materialization")
        if not isinstance(materialization, Mapping) or materialization.get("strategy") != "adapter-selected":
            raise ReleaseBuildError("dependency_materialization_contract_invalid")
    if identifiers != sorted(identifiers) or len(identifiers) != len(set(identifiers)):
        raise ReleaseBuildError("dependency_manifest_environment_order_invalid")
    return manifest


def _dependency_locks(root: Path, manifest: Mapping[str, Any]) -> list[dict[str, str]]:
    refs: dict[str, dict[str, str]] = {}
    for row in manifest.get("environments") or []:
        if not isinstance(row, Mapping) or not isinstance(row.get("lock"), Mapping):
            continue
        lock = row["lock"]
        relative = _canonical_relative(lock.get("path"))
        path = _inside(root, relative)
        refs[relative] = {"path": relative, "digest": file_digest(path)}
    return [refs[key] for key in sorted(refs)]


def _run_build_step(
    root: Path,
    recipe: Mapping[str, Any],
    manifest: Mapping[str, Any],
    *,
    cache_root: Path | None,
    skip: bool,
) -> None:
    command = (recipe.get("runtime") or {}).get("build")
    if not isinstance(command, str) or not command.strip() or skip:
        return
    if _SHELL_META.search(command):
        raise ReleaseBuildError(
            "artifact_build_shell_syntax_forbidden",
            "runtime.build must be one shell-free argv command",
        )
    try:
        argv = shlex.split(command, posix=True)
    except ValueError as exc:
        raise ReleaseBuildError("artifact_build_command_invalid", str(exc)) from exc
    if not argv:
        raise ReleaseBuildError("artifact_build_command_invalid")
    build_environments = [
        row for row in manifest.get("environments") or []
        if isinstance(row, Mapping) and "build" in row.get("purposes", [])
    ]
    broker = (
        EnvironmentBroker(cache_root or default_state_root())
        if build_environments
        else None
    )
    prepared = []
    try:
        for row in build_environments:
            assert broker is not None
            prepared.append(broker.prepare(
                manifest_path=root / DEPENDENCY_MANIFEST_NAME,
                project_root=root,
                environment_id=str(row["environment_id"]),
                purpose="build",
                consumer_kind="build",
            ))
        # A release build must not inherit deployment credentials or unrelated
        # user-session secrets. Dependency installation already happened in
        # the Broker; the build step receives only the minimum host runtime
        # environment plus the prepared interpreter/bin directories.
        inherited = (
            "PATH", "HOME", "USERPROFILE", "TMPDIR", "TEMP", "TMP",
            "LANG", "LC_ALL", "SYSTEMROOT", "COMSPEC", "PATHEXT",
        )
        environment = {
            key: value
            for key in inherited
            if isinstance((value := os.environ.get(key)), str) and value
        }
        environment["ZE_BUILD_ISOLATED"] = "1"
        python_bins = [
            item.environment_path / ("Scripts" if os.name == "nt" else "bin")
            for item in prepared
            if (item.environment_path / ("Scripts" if os.name == "nt" else "bin")).is_dir()
        ]
        if python_bins:
            environment["PATH"] = os.pathsep.join(
                [*(str(path) for path in python_bins), environment.get("PATH", "")]
            )
        heartbeat = LeaseHeartbeat(broker, prepared) if broker is not None else None
        context = heartbeat if heartbeat is not None else _NullContext()
        with context:
            try:
                subprocess.run(argv, cwd=root, env=environment, check=True, shell=False)
            except FileNotFoundError as exc:
                raise ReleaseBuildError("artifact_build_program_missing", argv[0]) from exc
            except subprocess.CalledProcessError as exc:
                raise ReleaseBuildError(
                    "artifact_build_step_failed", f"build command exited {exc.returncode}"
                ) from exc
    except BrokerError as exc:
        raise ReleaseBuildError(exc.code, str(exc)) from exc
    finally:
        for item in reversed(prepared):
            assert broker is not None
            try:
                broker.release(item)
            except BrokerError:
                pass


class _NullContext:
    def __enter__(self) -> "_NullContext":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None


def _recipe_excluded(relative: str, patterns: Sequence[str]) -> bool:
    for raw in patterns:
        pattern = str(raw).replace("\\", "/")
        prefix = pattern.rstrip("/")
        if any(char in pattern for char in "*?["):
            if fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(Path(relative).name, pattern):
                return True
        elif relative == prefix or relative.startswith(prefix + "/"):
            return True
    return False


def _payload_files(
    root: Path,
    selections: Sequence[str],
    *,
    recipe_excludes: Sequence[str],
    executable: bool,
) -> dict[str, Path]:
    files: dict[str, Path] = {}

    def add(path: Path) -> None:
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError as exc:
            raise ReleaseBuildError("payload_outside_root") from exc
        parts = PurePosixPath(relative).parts
        if any(part in _FORBIDDEN_PARTS for part in parts):
            return
        if path.name in _FORBIDDEN_FILES or path.name == SOURCE_INVENTORY_PATH.rsplit("/", 1)[-1]:
            return
        if path.name == ".env" or path.name.startswith(".env.") or path.suffix in {".pyc", ".pyo"}:
            return
        if _recipe_excluded(relative, recipe_excludes):
            return
        if path.is_symlink():
            raise ReleaseBuildError("payload_symlink_forbidden", relative)
        if path.is_file():
            size = path.stat().st_size
            if size > _MAX_MEMBER_BYTES:
                raise ReleaseBuildError("payload_member_too_large", relative)
            files[relative] = path

    for raw_selection in selections:
        selection = _canonical_relative(raw_selection, allow_dot=True)
        selected = _inside(root, selection)
        if selected.is_file():
            add(selected)
            continue
        broad = selection == "."
        for current, dirnames, filenames in os.walk(selected, followlinks=False):
            current_path = Path(current)
            kept: list[str] = []
            for dirname in sorted(dirnames):
                child = current_path / dirname
                relative = child.relative_to(root).as_posix()
                if child.is_symlink():
                    raise ReleaseBuildError("payload_symlink_forbidden", relative)
                if dirname in _FORBIDDEN_PARTS or _recipe_excluded(relative, recipe_excludes):
                    continue
                if broad and dirname in _BROAD_SKIP_DIRS:
                    continue
                kept.append(dirname)
            dirnames[:] = kept
            for filename in sorted(filenames):
                add(current_path / filename)
    if executable:
        dependency = root / DEPENDENCY_MANIFEST_NAME
        if dependency.is_symlink() or not dependency.is_file():
            raise ReleaseBuildError("dependency_manifest_missing_from_payload")
        files[DEPENDENCY_MANIFEST_NAME] = dependency
    total = sum(path.stat().st_size for path in files.values())
    if not files:
        raise ReleaseBuildError("artifact_payload_empty")
    if total > _MAX_ARCHIVE_BYTES:
        raise ReleaseBuildError("artifact_payload_too_large")
    return files


def _zip_bytes(
    files: Mapping[str, Path],
    *,
    runtime_package: Mapping[str, Any] | None,
) -> bytes:
    descriptor, temporary = tempfile.mkstemp(prefix="ze-base-", suffix=".zip")
    os.close(descriptor)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            members: list[tuple[str, bytes, int]] = []
            if runtime_package is not None:
                members.append((
                    RUNTIME_PACKAGE_MEMBER,
                    canonical_json_bytes(runtime_package) + b"\n",
                    0o644,
                ))
            for relative, path in files.items():
                if runtime_package is not None and relative != DEPENDENCY_MANIFEST_NAME:
                    archive_name = f"payload/{relative}"
                else:
                    archive_name = relative
                mode = stat.S_IMODE(path.stat().st_mode)
                members.append((archive_name, path.read_bytes(), 0o755 if mode & 0o111 else 0o644))
            names = [name for name, _, _ in members]
            if len(names) != len(set(names)):
                raise ReleaseBuildError("artifact_member_collision")
            for name, payload, mode in sorted(members, key=lambda row: row[0]):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (stat.S_IFREG | mode) << 16
                archive.writestr(info, payload)
        return Path(temporary).read_bytes()
    finally:
        Path(temporary).unlink(missing_ok=True)


def _runtime_package(
    variant: Mapping[str, Any],
    *,
    skill_uid: str,
    artifact_id: str,
    recipe: Mapping[str, Any],
) -> dict[str, Any] | None:
    raw = variant.get("runtime_package")
    if not isinstance(raw, Mapping):
        return None
    runtime_variant = str(variant["runtime_variant"])
    if runtime_variant in _CLOUDFLARE_WORKERS:
        family = "cloudflare-worker"
        visibility = "backend-not-browser-delivered"
    elif runtime_variant in _CLOUDFLARE_STATIC:
        family = "cloudflare-static-assets"
        visibility = "browser-visible"
    else:
        raise ReleaseBuildError("runtime_package_variant_unsupported")
    health_path = (recipe.get("runtime") or {}).get("healthcheck_path")
    if not isinstance(health_path, str) or not health_path.startswith("/"):
        raise ReleaseBuildError("runtime_package_healthcheck_missing")
    required_environment_names = sorted(
        str(row["name"])
        for row in recipe.get("secrets") or []
        if isinstance(row, Mapping) and row.get("required") is True
    )
    return {
        "schema": RUNTIME_PACKAGE_SCHEMA,
        "skill_uid": skill_uid,
        "artifact_id": artifact_id,
        "runtime_variant": runtime_variant,
        "delivery_mode": variant["delivery_mode"],
        "artifact_role": variant["artifact_role"],
        "runtime_family": family,
        "module_format": raw["module_format"],
        "payload": {
            "root": "payload",
            "entrypoint": raw["entrypoint"],
            "assets_directory": raw["assets_directory"],
        },
        "bindings": list(raw["bindings"]),
        "required_environment_names": required_environment_names,
        "healthcheck": {"path": health_path, "expected_statuses": [200]},
        "source_visibility": visibility,
    }


def _sbom(
    manifest: Mapping[str, Any],
    *,
    skill_id: str,
    version: str,
    artifact_role: str,
) -> dict[str, Any]:
    components = []
    for row in manifest.get("environments") or []:
        if not isinstance(row, Mapping):
            continue
        roles = row.get("artifact_roles")
        if isinstance(roles, list) and artifact_role not in roles:
            continue
        properties = [
            {"name": "ze:ecosystem", "value": str(row.get("ecosystem"))},
            {"name": "ze:runtime-constraint", "value": str((row.get("runtime") or {}).get("version_constraint"))},
        ]
        lock = row.get("lock")
        if isinstance(lock, Mapping):
            properties.append({"name": "ze:lock-digest", "value": str(lock.get("digest"))})
        components.append({
            "type": "library",
            "name": str(row["environment_id"]),
            "version": str((row.get("runtime") or {}).get("version_constraint")),
            "properties": sorted(properties, key=lambda item: item["name"]),
        })
    components.sort(key=lambda item: item["name"])
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": "urn:uuid:" + str(uuid.uuid5(uuid.NAMESPACE_URL, f"ze:{skill_id}:{version}:{artifact_role}")),
        "version": 1,
        "metadata": {
            "component": {"type": "application", "name": skill_id, "version": version}
        },
        "components": components,
    }


def _public_key(private_key_pem: bytes, *, password: bytes | None) -> bytes:
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import (
            Encoding,
            PublicFormat,
            load_pem_private_key,
        )
    except ImportError as exc:
        raise ReleaseBuildError("cryptography_unavailable") from exc
    try:
        private_key = load_pem_private_key(private_key_pem, password=password)
    except (TypeError, ValueError) as exc:
        raise ReleaseBuildError("release_private_key_invalid") from exc
    if not isinstance(private_key, Ed25519PrivateKey):
        raise ReleaseBuildError("release_private_key_not_ed25519")
    return private_key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)


def _signature_record(
    root: Path,
    *,
    private_key_pem: bytes,
    password: bytes | None,
    key_id: str,
    public_path: Path,
    signature_path: Path,
    signed_digest: str,
    force: bool,
) -> dict[str, Any]:
    signature = sign_artifact_digest(private_key_pem, signed_digest, password=password)
    _write_output(signature_path, signature, force=force)
    return {
        "algorithm": "ed25519",
        "key_id": key_id,
        "signed_digest": signed_digest,
        "public_key": {
            "path": public_path.relative_to(root).as_posix(),
            "digest": file_digest(public_path),
        },
        "detached_signature": {
            "path": signature_path.relative_to(root).as_posix(),
            "digest": file_digest(signature_path),
        },
    }


def build_release(
    root: Path,
    *,
    config_path: Path,
    private_key_path: Path,
    key_id: str,
    password_env: str | None = None,
    cache_root: Path | None = None,
    skip_build: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    root = root.expanduser().resolve(strict=True)
    if not root.is_dir() or root.is_symlink():
        raise ReleaseBuildError("skill_root_invalid")
    if re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", key_id) is None:
        raise ReleaseBuildError("release_key_id_invalid")
    private_input = private_key_path.expanduser()
    if private_input.is_symlink():
        raise ReleaseBuildError("release_private_key_symlink_forbidden")
    try:
        private_path = private_input.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise ReleaseBuildError("release_private_key_missing") from exc
    if not private_path.is_file():
        raise ReleaseBuildError("release_private_key_invalid")
    try:
        private_path.relative_to(root)
    except ValueError:
        pass
    else:
        raise ReleaseBuildError(
            "release_private_key_inside_skill",
            "release private keys must remain outside source, artifacts and Git",
        )

    recipe = load_recipe(root)
    if recipe.get("schema") != RECIPE_SCHEMA:
        raise ReleaseBuildError("portable_deploy_recipe_required")
    deployment = evaluate_deployment_profile(root, recipe)
    if not deployment["valid"]:
        raise ReleaseBuildError("deploy_recipe_invalid", "; ".join(deployment["errors"]))
    compatibility = deployment["deployment_profile"]
    compatibility_digest = canonical_digest(compatibility)

    config_file = config_path if config_path.is_absolute() else root / config_path
    try:
        config_file.resolve(strict=True).relative_to(root)
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise ReleaseBuildError("artifact_build_declaration_outside_skill") from exc
    config = _load_json(config_file, label="artifact_build_declaration")
    _validate_build_declaration(config, recipe)
    dependencies = _load_ready_dependencies(root, recipe)
    password: bytes | None = None
    if password_env:
        raw = os.environ.get(password_env)
        if raw is None:
            raise ReleaseBuildError("release_key_password_missing")
        password = raw.encode("utf-8")
    private_key_pem = private_path.read_bytes()
    public_key_pem = _public_key(private_key_pem, password=password)
    _run_build_step(
        root, recipe, dependencies, cache_root=cache_root, skip=skip_build
    )

    inventory = build_source_inventory(
        root,
        roots=["."],
        excludes=[
            ".data",
            MANIFEST_NAME,
            "ZE_BUILD_EVIDENCE.json",
            "artifacts",
            SOURCE_INVENTORY_PATH,
            "catalog",
            "dist",
        ],
        inventory_path=SOURCE_INVENTORY_PATH,
    )
    inventory_path = root / SOURCE_INVENTORY_PATH
    previous_inventory = (
        inventory_path.read_bytes()
        if inventory_path.is_file() and not inventory_path.is_symlink()
        else None
    )
    inventory_payload = canonical_json_bytes(inventory) + b"\n"
    _atomic_bytes(inventory_path, inventory_payload)
    source_digest = canonical_digest(inventory)
    inventory_paths = {row["path"] for row in inventory["files"]}

    release_dir = root / "artifacts" / "releases" / (
        f"{config['version']}-{source_digest.removeprefix('sha256:')[:12]}-{key_id}"
    )
    public_path = release_dir / "keys" / f"{key_id}.public.pem"
    _write_output(public_path, public_key_pem, force=force, mode=0o644)
    surface_path = _inside(root, "ZE_SURFACE.json")
    dependency_locks = _dependency_locks(root, dependencies)
    recipe_excludes = list((recipe.get("artifact") or {}).get("exclude") or [])
    variants: list[dict[str, Any]] = []
    try:
        for declaration in config["variants"]:
            variant_id = str(declaration["variant_id"])
            artifact_id = f"base-{recipe['skill_id']}-{variant_id}"
            if len(artifact_id) > 160:
                raise ReleaseBuildError("artifact_id_too_long", artifact_id)
            package = _runtime_package(
                declaration,
                skill_uid=str(recipe["skill_uid"]),
                artifact_id=artifact_id,
                recipe=recipe,
            )
            payload_files = _payload_files(
                root,
                declaration["payload_paths"],
                recipe_excludes=recipe_excludes,
                executable=declaration["artifact_role"] in _EXECUTABLE_ROLES,
            )
            code_assets = [
                {"path": relative, "digest": file_digest(path)}
                for relative, path in sorted(payload_files.items())
                if relative not in inventory_paths
            ]
            inputs = {
                "runtime_variant": declaration["runtime_variant"],
                "delivery_mode": declaration["delivery_mode"],
                "artifact_role": declaration["artifact_role"],
                "surface_profile_digest": file_digest(surface_path),
                "compatibility_profile_digest": compatibility_digest,
                "dependency_locks": dependency_locks,
                "feature_set": list(declaration["feature_set"]),
                "code_assets": code_assets,
                "build_options_digest": canonical_digest({
                    "schema": BUILD_DECLARATION_SCHEMA,
                    "variant": declaration,
                }),
            }
            inputs_digest = variant_inputs_digest(inputs)
            archive_payload = _zip_bytes(payload_files, runtime_package=package)
            archive_path = release_dir / "base" / f"{artifact_id}.zip"
            _write_output(archive_path, archive_payload, force=force)
            archive_digest = file_digest(archive_path)

            sbom_value = _sbom(
                dependencies,
                skill_id=str(recipe["skill_id"]),
                version=str(config["version"]),
                artifact_role=str(declaration["artifact_role"]),
            )
            sbom_path = release_dir / "sbom" / f"{variant_id}.cdx.json"
            _write_output(
                sbom_path, canonical_json_bytes(sbom_value) + b"\n", force=force
            )
            signature_path = release_dir / "signatures" / f"{variant_id}.sig"
            signature = _signature_record(
                root,
                private_key_pem=private_key_pem,
                password=password,
                key_id=key_id,
                public_path=public_path,
                signature_path=signature_path,
                signed_digest=archive_digest,
                force=force,
            )
            base: dict[str, Any] = {
                "artifact_id": artifact_id,
                "artifact_role": declaration["artifact_role"],
                "path": archive_path.relative_to(root).as_posix(),
                "format": "zip",
                "digest": archive_digest,
                "size_bytes": archive_path.stat().st_size,
                "delivery_mode": declaration["delivery_mode"],
                "sbom": {
                    "path": sbom_path.relative_to(root).as_posix(),
                    "digest": file_digest(sbom_path),
                    "format": "cyclonedx-json",
                },
                "signature": signature,
                "compatibility_profile_digest": compatibility_digest,
            }
            if package is not None:
                raw_runtime = canonical_json_bytes(package) + b"\n"
                base["runtime_package"] = {
                    "schema": RUNTIME_PACKAGE_SCHEMA,
                    "manifest_path": RUNTIME_PACKAGE_MEMBER,
                    "manifest_digest": "sha256:" + hashlib.sha256(raw_runtime).hexdigest(),
                }
            variants.append({
                "variant_id": variant_id,
                "variant_inputs": inputs,
                "variant_inputs_digest": inputs_digest,
                "build_key": base_build_key(
                    str(recipe["skill_uid"]), source_digest, inputs_digest
                ),
                "base_artifact": base,
            })

        update_candidate: dict[str, Any] = {
            "version": config["version"],
            "channel_eligibility": list(config["channel_eligibility"]),
            "minimum_launcher_version": config["minimum_launcher_version"],
            "migration": config["migration"],
            "rollback": config["rollback"],
        }
        release_notes = config.get("release_notes")
        if isinstance(release_notes, str):
            update_candidate["release_notes_digest"] = file_digest(
                _inside(root, _canonical_relative(release_notes))
            )
        manifest = {
            "schema": MANIFEST_SCHEMA,
            "skill_uid": recipe["skill_uid"],
            "current_skill_id": recipe["skill_id"],
            "source_digest": source_digest,
            "source_inventory": {
                "path": SOURCE_INVENTORY_PATH,
                "digest": file_digest(inventory_path),
            },
            "variants": variants,
            "series_overlays": [],
            "update_candidate": update_candidate,
            "isolation": {
                "base_scope": "global",
                "overlay_scope": "series-organization-target",
                "scan_policy": "structured-scope-markers-v1",
                "cross_series_asset_refs": [],
            },
        }
        manifest_path = root / MANIFEST_NAME
        previous_manifest = (
            manifest_path.read_bytes()
            if manifest_path.is_file() and not manifest_path.is_symlink()
            else None
        )
        _atomic_bytes(manifest_path, canonical_json_bytes(manifest) + b"\n")
        result = evaluate_artifact_variant(root, recipe, compatibility)
        if not result["valid"]:
            _restore(manifest_path, previous_manifest)
            _restore(inventory_path, previous_inventory)
            raise ReleaseBuildError(
                "artifact_closure_validation_failed", "; ".join(result["errors"])
            )
        return {
            "ok": True,
            "schema": "ze.release-build-result.v1",
            "skill_uid": recipe["skill_uid"],
            "skill_id": recipe["skill_id"],
            "version": config["version"],
            "source_digest": source_digest,
            "manifest": str(manifest_path),
            "manifest_digest": file_digest(manifest_path),
            "variants": [
                {
                    "variant_id": row["variant_id"],
                    "artifact_id": row["base_artifact"]["artifact_id"],
                    "artifact_digest": row["base_artifact"]["digest"],
                    "path": row["base_artifact"]["path"],
                }
                for row in variants
            ],
            "validation": result["artifact_variant"]["status"],
        }
    except Exception:
        if not (root / MANIFEST_NAME).is_file():
            _restore(inventory_path, previous_inventory)
        raise


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", default=".")
    parser.add_argument("--config", default=BUILD_DECLARATION_NAME)
    parser.add_argument("--check", action="store_true", help="validate declaration and delivery matrix only")
    parser.add_argument("--private-key")
    parser.add_argument("--key-id")
    parser.add_argument("--password-env")
    parser.add_argument("--cache-root")
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.check:
        result = evaluate_build_declaration(Path(args.root))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["valid"] else 1
    if not args.private_key or not args.key_id:
        parser.error("release build requires --private-key and --key-id")
    try:
        result = build_release(
            Path(args.root),
            config_path=Path(args.config),
            private_key_path=Path(args.private_key),
            key_id=args.key_id,
            password_env=args.password_env,
            cache_root=Path(args.cache_root).expanduser().absolute() if args.cache_root else None,
            skip_build=args.skip_build,
            force=args.force,
        )
    except ReleaseBuildError as exc:
        print(json.dumps({
            "ok": False,
            "error": exc.code,
            "detail": exc.detail,
        }, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
