#!/usr/bin/env python3
"""Validate reproducible Base Artifacts and scoped Series overlays.

``ZE_ARTIFACT_VARIANT.json`` is intentionally separate from ``deploy.json``:
the portable recipe describes runtime needs, while this manifest proves which
bytes were built.  A Base Artifact is scope-free and reusable.  Each optional
Series overlay is derived from one System Catalog snapshot and is bound to one
Series + Organization + Target without granting runtime authorization.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import stat
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

from deployment_profile import RECIPE_SCHEMA, evaluate_deployment_profile, load_recipe


MANIFEST_NAME = "ZE_ARTIFACT_VARIANT.json"
MANIFEST_SCHEMA = "ze.artifact-variant-manifest.v1"
OVERLAY_SCHEMA = "ze.series-overlay.v1"
SOURCE_INVENTORY_SCHEMA = "ze.source-inventory.v1"
RUNTIME_PACKAGE_SCHEMA = "ze.runtime-package.v1"
RUNTIME_PACKAGE_MEMBER = "ze-runtime.json"
DEPENDENCY_MANIFEST_MEMBER = "ZE_DEPENDENCIES.json"
SCHEMA_DIR = Path(__file__).resolve().parents[1] / "contracts" / "artifact"
MANIFEST_SCHEMA_PATH = SCHEMA_DIR / f"{MANIFEST_SCHEMA}.schema.json"
OVERLAY_SCHEMA_PATH = SCHEMA_DIR / f"{OVERLAY_SCHEMA}.schema.json"
SOURCE_INVENTORY_SCHEMA_PATH = SCHEMA_DIR / f"{SOURCE_INVENTORY_SCHEMA}.schema.json"
RUNTIME_PACKAGE_SCHEMA_PATH = SCHEMA_DIR / f"{RUNTIME_PACKAGE_SCHEMA}.schema.json"

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_JSON_BYTES = 8 * 1024 * 1024
_MAX_ARCHIVE_MEMBER_BYTES = 32 * 1024 * 1024
_MAX_ARCHIVE_SCAN_BYTES = 256 * 1024 * 1024

_CLOUDFLARE_WORKER_VARIANTS = {
    "worker-js", "worker-typescript", "worker-python",
    "cloudflare-worker-js", "cloudflare-worker-python",
}
_CLOUDFLARE_STATIC_VARIANTS = {
    "static-web", "cloudflare-static-assets",
}
_CLOUDFLARE_RUNTIME_VARIANTS = (
    _CLOUDFLARE_WORKER_VARIANTS | _CLOUDFLARE_STATIC_VARIANTS
)
_CLOUDFLARE_BINDING_KIND_BY_CAPABILITY = {
    "sql": "d1-database",
    "object_storage": "r2-bucket",
    "kv": "kv-namespace",
    "queues": "queue",
    "static_assets": "static-assets",
}


class DuplicateKeyError(ValueError):
    pass


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateKeyError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def variant_inputs_digest(value: Mapping[str, Any]) -> str:
    return canonical_digest(dict(value))


def base_build_key(skill_uid: str, source_digest: str, inputs_digest: str) -> str:
    return canonical_digest({
        "schema": "ze.base-artifact-build-key.v1",
        "skill_uid": skill_uid,
        "source_digest": source_digest,
        "variant_inputs_digest": inputs_digest,
    })


_SOURCE_AUTO_IGNORED_DIRS = {
    ".git", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".venv",
    ".vite-temp", "__pycache__", "node_modules",
}
_SOURCE_AUTO_IGNORED_FILES = {".DS_Store"}
_SOURCE_ALLOWED_OUTPUT_DIRS = {
    ".data", ".venv", "artifacts", "build-output", "catalog", "coverage",
    "dist", "release",
}


def _canonical_selection_path(value: Any, *, allow_dot: bool = False) -> str | None:
    if not isinstance(value, str):
        return None
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
        return None
    return pure.as_posix()


def _source_exclusion_allowed(value: str, *, inventory_path: str) -> bool:
    if value in {MANIFEST_NAME, "ZE_BUILD_EVIDENCE.json", inventory_path}:
        return True
    parts = PurePosixPath(value).parts
    return bool(
        parts
        and (
            parts[0] in _SOURCE_ALLOWED_OUTPUT_DIRS
            or any(part in _SOURCE_AUTO_IGNORED_DIRS | {"dist"} for part in parts)
        )
    )


def _source_path_excluded(relative: str, excludes: Iterable[str]) -> bool:
    return any(relative == item or relative.startswith(item + "/") for item in excludes)


def _enumerate_source_files(
    root: Path,
    *,
    roots: Iterable[str],
    excludes: Iterable[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    excluded = tuple(excludes)
    files: dict[str, Path] = {}
    errors: list[str] = []

    def add_file(path: Path) -> None:
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:
            errors.append(f"source selection escapes Skill root: {path}")
            return
        if _source_path_excluded(relative, excluded):
            return
        if path.name in _SOURCE_AUTO_IGNORED_FILES or path.suffix in {".pyc", ".pyo"}:
            return
        if path.is_symlink():
            errors.append(f"source selection contains symlink: {relative}")
            return
        if path.is_file():
            files[relative] = path

    for selection_root in roots:
        selected = root if selection_root == "." else root.joinpath(*PurePosixPath(selection_root).parts)
        if selected.is_symlink():
            errors.append(f"source root must not be a symlink: {selection_root}")
            continue
        if not selected.exists():
            errors.append(f"source root is missing: {selection_root}")
            continue
        if selected.is_file():
            add_file(selected)
            continue
        for current, dirnames, filenames in os.walk(selected, followlinks=False):
            current_path = Path(current)
            kept_dirs: list[str] = []
            for dirname in sorted(dirnames):
                path = current_path / dirname
                relative = path.relative_to(root).as_posix()
                if dirname in _SOURCE_AUTO_IGNORED_DIRS or _source_path_excluded(relative, excluded):
                    continue
                if path.is_symlink():
                    errors.append(f"source selection contains symlink directory: {relative}")
                    continue
                kept_dirs.append(dirname)
            dirnames[:] = kept_dirs
            for filename in sorted(filenames):
                add_file(current_path / filename)
    inventory = [
        {"path": relative, "digest": file_digest(path), "size_bytes": path.stat().st_size}
        for relative, path in sorted(files.items())
    ]
    if not inventory:
        errors.append("source selection contains no files")
    return inventory, errors


def build_source_inventory(
    root: Path,
    *,
    roots: Iterable[str],
    excludes: Iterable[str],
    inventory_path: str,
) -> dict[str, Any]:
    """Build the deterministic source inventory used by ``source_digest``."""

    root = root.resolve()
    normalized_roots = [
        _canonical_selection_path(value, allow_dot=True) for value in roots
    ]
    normalized_excludes = [_canonical_selection_path(value) for value in excludes]
    if any(value is None for value in normalized_roots + normalized_excludes):
        raise ValueError("source inventory roots/excludes must be canonical relative paths")
    clean_roots = [str(value) for value in normalized_roots]
    clean_excludes = [str(value) for value in normalized_excludes]
    if clean_roots != sorted(set(clean_roots)):
        raise ValueError("source inventory roots must be sorted and unique")
    if clean_excludes != sorted(set(clean_excludes)):
        raise ValueError("source inventory excludes must be sorted and unique")
    for value in clean_excludes:
        if not _source_exclusion_allowed(value, inventory_path=inventory_path):
            raise ValueError(f"source inventory exclusion is outside generated/runtime policy: {value}")
    for required in (MANIFEST_NAME, "ZE_BUILD_EVIDENCE.json", inventory_path):
        if required not in clean_excludes:
            raise ValueError(f"source inventory must exclude generated file: {required}")
    files, errors = _enumerate_source_files(
        root, roots=clean_roots, excludes=clean_excludes
    )
    if errors:
        raise ValueError("; ".join(errors))
    return {
        "schema": SOURCE_INVENTORY_SCHEMA,
        "selection": {
            "policy": "ze-source-selection-v1",
            "roots": clean_roots,
            "excludes": clean_excludes,
        },
        "files": files,
    }


def _load_json(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise ValueError(f"JSON input must not be a symlink: {path}")
    try:
        if path.stat().st_size > _MAX_JSON_BYTES:
            raise ValueError(f"JSON input exceeds {_MAX_JSON_BYTES} bytes: {path}")
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_pairs)
    except FileNotFoundError as exc:
        raise ValueError(f"JSON input is missing: {path}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError, DuplicateKeyError) as exc:
        raise ValueError(f"invalid JSON input {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"JSON input must contain an object: {path}")
    return value


def _load_json_bytes(raw: bytes, *, label: str) -> dict[str, Any]:
    if len(raw) > _MAX_JSON_BYTES:
        raise ValueError(f"JSON input exceeds {_MAX_JSON_BYTES} bytes: {label}")
    try:
        value = json.loads(raw.decode("utf-8", "strict"), object_pairs_hook=_pairs)
    except (UnicodeError, json.JSONDecodeError, DuplicateKeyError) as exc:
        raise ValueError(f"invalid JSON input {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"JSON input must contain an object: {label}")
    return value


def _schema_errors(value: Mapping[str, Any], schema_path: Path) -> list[str]:
    try:
        from jsonschema import Draft202012Validator
    except ImportError:
        return ["jsonschema is required to validate artifact contracts"]
    try:
        schema = _load_json(schema_path)
        Draft202012Validator.check_schema(schema)
    except Exception as exc:  # noqa: BLE001 - contract load failure must be evidence
        return [f"artifact schema unavailable: {type(exc).__name__}: {exc}"]
    validator = Draft202012Validator(schema)
    errors: list[str] = []
    for error in sorted(
        validator.iter_errors(value),
        key=lambda item: tuple(str(part) for part in item.absolute_path),
    ):
        path = "$" + "".join(
            f"[{part}]" if isinstance(part, int) else f".{part}"
            for part in error.absolute_path
        )
        errors.append(f"{path}: {error.message}")
    return errors


def _safe_file(root: Path, relative: Any, label: str) -> tuple[Path | None, list[str]]:
    if not isinstance(relative, str):
        return None, [f"{label} must be a relative path"]
    raw = relative.strip().replace("\\", "/")
    pure = PurePosixPath(raw)
    if (
        not raw
        or raw.startswith("/")
        or pure.is_absolute()
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        return None, [f"{label} must be a canonical relative path"]
    candidate = root.joinpath(*pure.parts)
    current = root
    for part in pure.parts:
        current = current / part
        if current.is_symlink():
            return None, [f"{label} must not traverse a symlink: {relative}"]
    try:
        resolved_root = root.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(resolved_root)
    except (FileNotFoundError, OSError, ValueError):
        return None, [f"{label} is missing or escapes the Skill root: {relative}"]
    if not resolved.is_file():
        return None, [f"{label} must reference a regular file: {relative}"]
    return resolved, []


def _check_file_ref(
    root: Path,
    record: Mapping[str, Any],
    label: str,
) -> tuple[Path | None, list[str]]:
    path, errors = _safe_file(root, record.get("path"), f"{label}.path")
    if path is None:
        return None, errors
    expected_digest = record.get("digest")
    actual_digest = file_digest(path)
    if actual_digest != expected_digest:
        errors.append(f"{label}.digest mismatch")
    if "size_bytes" in record and path.stat().st_size != record.get("size_bytes"):
        errors.append(f"{label}.size_bytes mismatch")
    return path, errors


def artifact_signature_message(digest: str) -> bytes:
    if _DIGEST.fullmatch(digest) is None:
        raise ValueError("artifact signature digest is invalid")
    return b"ZE-ARTIFACT-SIGNATURE-V1\0" + digest.encode("ascii")


def sign_artifact_digest(
    private_key_pem: bytes,
    digest: str,
    *,
    password: bytes | None = None,
) -> bytes:
    """Create a detached Ed25519 signature without persisting private material."""

    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
    except ImportError as exc:  # pragma: no cover - build environment contract
        raise ValueError("cryptography is required for Ed25519 signing") from exc
    try:
        key = load_pem_private_key(private_key_pem, password=password)
    except (TypeError, ValueError) as exc:
        raise ValueError("private key cannot be loaded") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("private key must be Ed25519")
    return key.sign(artifact_signature_message(digest))


def _verify_artifact_signature(
    root: Path,
    record: Mapping[str, Any],
    *,
    artifact_digest: str,
    label: str,
) -> tuple[dict[str, Any], dict[str, Path | None], list[str]]:
    errors: list[str] = []
    if record.get("signed_digest") != artifact_digest:
        errors.append(f"{label}.signed_digest mismatch")
    public_path, public_errors = _check_file_ref(
        root, record.get("public_key") or {}, f"{label}.public_key"
    )
    signature_path, signature_errors = _check_file_ref(
        root, record.get("detached_signature") or {}, f"{label}.detached_signature"
    )
    errors.extend(public_errors)
    errors.extend(signature_errors)
    if public_path is not None and signature_path is not None and not errors:
        try:
            from cryptography.exceptions import InvalidSignature
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
            from cryptography.hazmat.primitives.serialization import load_pem_public_key

            key = load_pem_public_key(public_path.read_bytes())
            if not isinstance(key, Ed25519PublicKey):
                errors.append(f"{label}.public_key must contain an Ed25519 public key")
            else:
                try:
                    key.verify(
                        signature_path.read_bytes(),
                        artifact_signature_message(artifact_digest),
                    )
                except InvalidSignature:
                    errors.append(f"{label} verification failed")
        except (ImportError, OSError, TypeError, ValueError) as exc:
            errors.append(f"{label} cannot be verified: {type(exc).__name__}: {exc}")
    evidence = {
        "algorithm": record.get("algorithm"),
        "key_id": record.get("key_id"),
        "public_key_digest": (record.get("public_key") or {}).get("digest")
        if isinstance(record.get("public_key"), Mapping)
        else None,
        "signature_digest": (record.get("detached_signature") or {}).get("digest")
        if isinstance(record.get("detached_signature"), Mapping)
        else None,
        "status": "verified" if not errors else "invalid",
        "publisher_trust": "requires-deploy-trust-policy",
    }
    return evidence, {
        "public_key": public_path,
        "detached_signature": signature_path,
    }, errors


def _validate_sbom(path: Path) -> list[str]:
    try:
        sbom = _load_json(path)
    except ValueError as exc:
        return [str(exc)]
    errors: list[str] = []
    if sbom.get("bomFormat") != "CycloneDX":
        errors.append("base_artifact.sbom must be CycloneDX JSON")
    if not isinstance(sbom.get("specVersion"), str) or not sbom.get("specVersion"):
        errors.append("base_artifact.sbom requires specVersion")
    if not isinstance(sbom.get("components"), list):
        errors.append("base_artifact.sbom requires components array")
    return errors


def _archive_members(path: Path, archive_format: str) -> tuple[list[tuple[str, bytes]], list[str]]:
    members: list[tuple[str, bytes]] = []
    errors: list[str] = []
    total = 0
    accepted_names: set[str] = set()

    def accept(name: str, size: int) -> bool:
        nonlocal total
        if "\\" in name:
            errors.append(f"base artifact contains non-POSIX archive path: {name}")
            return False
        pure = PurePosixPath(name.replace("\\", "/"))
        if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
            errors.append(f"base artifact contains unsafe archive path: {name}")
            return False
        forbidden_environment_parts = {
            ".mypy_cache", ".pytest_cache", ".ruff_cache", ".venv",
            ".vite-temp", "__pycache__", "node_modules",
        }
        if any(part in forbidden_environment_parts for part in pure.parts):
            errors.append(
                f"base artifact contains regenerable dependency/cache path: {name}"
            )
            return False
        canonical_name = pure.as_posix()
        if canonical_name in accepted_names:
            errors.append(f"base artifact contains duplicate archive path: {canonical_name}")
            return False
        accepted_names.add(canonical_name)
        if size > _MAX_ARCHIVE_MEMBER_BYTES:
            errors.append(f"base artifact member exceeds scan limit: {name}")
            return False
        total += size
        if total > _MAX_ARCHIVE_SCAN_BYTES:
            errors.append("base artifact exceeds total scope-scan limit")
            return False
        return True

    try:
        if archive_format == "zip":
            if not zipfile.is_zipfile(path):
                return [], ["base_artifact.format is zip but bytes are not a ZIP archive"]
            with zipfile.ZipFile(path) as zip_archive:
                for zip_info in zip_archive.infolist():
                    if zip_info.is_dir():
                        continue
                    mode = (zip_info.external_attr >> 16) & 0o170000
                    if mode == stat.S_IFLNK:
                        errors.append(f"base artifact contains symlink member: {zip_info.filename}")
                        continue
                    if not accept(zip_info.filename, zip_info.file_size):
                        continue
                    members.append((zip_info.filename, zip_archive.read(zip_info)))
        elif archive_format == "tar":
            if not tarfile.is_tarfile(path):
                return [], ["base_artifact.format is tar but bytes are not a TAR archive"]
            with tarfile.open(path, mode="r:*") as tar_archive:
                for tar_info in tar_archive.getmembers():
                    if tar_info.isdir():
                        continue
                    if not tar_info.isfile():
                        errors.append(f"base artifact contains non-regular member: {tar_info.name}")
                        continue
                    if not accept(tar_info.name, tar_info.size):
                        continue
                    stream = tar_archive.extractfile(tar_info)
                    if stream is None:
                        errors.append(f"base artifact member cannot be read: {tar_info.name}")
                        continue
                    members.append((tar_info.name, stream.read()))
        else:
            return [], [f"unsupported base artifact format: {archive_format}"]
    except (OSError, zipfile.BadZipFile, tarfile.TarError) as exc:
        errors.append(f"base artifact archive cannot be inspected: {exc}")
    if not members and not errors:
        errors.append("base artifact archive contains no regular files")
    return members, errors


def _dependency_manifest_archive_validation(
    *,
    root: Path,
    members: Iterable[tuple[str, bytes]],
    inputs: Mapping[str, Any],
    label: str,
) -> tuple[dict[str, Any], list[str]]:
    """Close executable Base artifacts over the root dependency declaration."""

    artifact_role = inputs.get("artifact_role")
    required = artifact_role in {"server", "runtime", "local-adapter"}
    evidence: dict[str, Any] = {
        "required": required,
        "artifact_role": artifact_role,
        "member": DEPENDENCY_MANIFEST_MEMBER if required else None,
        "status": "not-required" if not required else "invalid",
    }
    if not required:
        return evidence, []

    errors: list[str] = []
    root_manifest = root / DEPENDENCY_MANIFEST_MEMBER
    if root_manifest.is_symlink() or not root_manifest.is_file():
        errors.append(
            f"{label}: executable artifact requires a regular root "
            f"{DEPENDENCY_MANIFEST_MEMBER}"
        )
        return evidence, errors
    try:
        expected = root_manifest.read_bytes()
    except OSError as exc:
        errors.append(f"{label}: cannot read root {DEPENDENCY_MANIFEST_MEMBER}: {exc}")
        return evidence, errors

    rows = [raw for name, raw in members if name == DEPENDENCY_MANIFEST_MEMBER]
    if len(rows) != 1:
        errors.append(
            f"{label}: executable Base artifact must contain exactly one root-level "
            f"{DEPENDENCY_MANIFEST_MEMBER}"
        )
        return evidence, errors
    actual = rows[0]
    if actual != expected:
        errors.append(
            f"{label}: archived {DEPENDENCY_MANIFEST_MEMBER} does not exactly match "
            "the verified root declaration"
        )
        return evidence, errors

    evidence.update({
        "status": "verified",
        "manifest_digest": "sha256:" + hashlib.sha256(actual).hexdigest(),
    })
    return evidence, []


def _runtime_package_validation(
    *,
    members: Iterable[tuple[str, bytes]],
    base: Mapping[str, Any],
    inputs: Mapping[str, Any],
    recipe: Mapping[str, Any],
    skill_uid: str,
    label: str,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Validate the provider-runnable manifest inside one signed Base archive."""

    errors: list[str] = []
    runtime_variant = inputs.get("runtime_variant")
    reference = base.get("runtime_package")
    required = runtime_variant in _CLOUDFLARE_RUNTIME_VARIANTS
    if reference is None:
        if required:
            errors.append(
                f"{label}.base_artifact.runtime_package is required for "
                f"Cloudflare runtime variant {runtime_variant}"
            )
        return None, errors
    if not isinstance(reference, Mapping):
        return None, [f"{label}.base_artifact.runtime_package must be an object"]

    member_rows = [(name, raw) for name, raw in members if name == RUNTIME_PACKAGE_MEMBER]
    if len(member_rows) != 1:
        errors.append(
            f"{label}.base_artifact must contain exactly one {RUNTIME_PACKAGE_MEMBER}"
        )
        return None, errors
    raw = member_rows[0][1]
    raw_digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    if reference.get("schema") != RUNTIME_PACKAGE_SCHEMA:
        errors.append(f"{label}.base_artifact.runtime_package.schema mismatch")
    if reference.get("manifest_path") != RUNTIME_PACKAGE_MEMBER:
        errors.append(f"{label}.base_artifact.runtime_package.manifest_path mismatch")
    if reference.get("manifest_digest") != raw_digest:
        errors.append(f"{label}.base_artifact.runtime_package.manifest_digest mismatch")
    try:
        runtime_package = _load_json_bytes(
            raw, label=f"{label}.base_artifact/{RUNTIME_PACKAGE_MEMBER}"
        )
    except ValueError as exc:
        errors.append(str(exc))
        return None, errors
    schema_errors = [
        f"{label}.base_artifact/{RUNTIME_PACKAGE_MEMBER}: {error}"
        for error in _schema_errors(runtime_package, RUNTIME_PACKAGE_SCHEMA_PATH)
    ]
    errors.extend(schema_errors)
    if schema_errors:
        return runtime_package, errors

    expected = {
        "skill_uid": skill_uid,
        "artifact_id": base.get("artifact_id"),
        "runtime_variant": runtime_variant,
        "delivery_mode": inputs.get("delivery_mode"),
        "artifact_role": inputs.get("artifact_role"),
    }
    for field, value in expected.items():
        if runtime_package.get(field) != value:
            errors.append(f"{label}.runtime_package.{field} mismatch")

    family = runtime_package.get("runtime_family")
    if runtime_variant in _CLOUDFLARE_WORKER_VARIANTS:
        expected_family = "cloudflare-worker"
    elif runtime_variant in _CLOUDFLARE_STATIC_VARIANTS:
        expected_family = "cloudflare-static-assets"
    else:
        expected_family = None
        errors.append(
            f"{label}.base_artifact.runtime_package is unsupported for "
            f"runtime variant {runtime_variant}"
        )
    if expected_family is not None and family != expected_family:
        errors.append(f"{label}.runtime_package.runtime_family mismatch")
    module_format = runtime_package.get("module_format")
    if runtime_variant in {"worker-python", "cloudflare-worker-python"}:
        if module_format != "python-module":
            errors.append(f"{label}.runtime_package.module_format must be python-module")
    elif runtime_variant in _CLOUDFLARE_WORKER_VARIANTS:
        if module_format not in {"es-module", "service-worker"}:
            errors.append(f"{label}.runtime_package.module_format must be a Worker module")
    elif runtime_variant in _CLOUDFLARE_STATIC_VARIANTS and module_format != "static":
        errors.append(f"{label}.runtime_package.module_format must be static")

    payload = runtime_package.get("payload")
    payload = payload if isinstance(payload, Mapping) else {}
    root = payload.get("root")
    names = {name for name, _ in members}
    if isinstance(root, str):
        prefix = root + "/"
        allowed_outside_payload = {RUNTIME_PACKAGE_MEMBER}
        if inputs.get("artifact_role") in {"server", "runtime", "local-adapter"}:
            # Executable artifacts close over the exact dependency declaration
            # at archive root.  It is control metadata for Deploy, not a Worker
            # payload file, so it intentionally sits beside ze-runtime.json.
            allowed_outside_payload.add(DEPENDENCY_MANIFEST_MEMBER)
        outside = sorted(
            name for name in names
            if name not in allowed_outside_payload and not name.startswith(prefix)
        )
        if outside:
            errors.append(
                f"{label}.runtime_package payload has files outside {root}: "
                + ", ".join(outside[:8])
            )
        entrypoint = payload.get("entrypoint")
        if isinstance(entrypoint, str) and prefix + entrypoint not in names:
            errors.append(f"{label}.runtime_package entrypoint is missing from archive")
        assets = payload.get("assets_directory")
        if isinstance(assets, str):
            assets_prefix = prefix + assets + "/"
            if not any(name.startswith(assets_prefix) for name in names):
                errors.append(f"{label}.runtime_package assets directory is empty or missing")

    bindings = runtime_package.get("bindings")
    bindings = bindings if isinstance(bindings, list) else []
    binding_keys = [
        (str(row.get("kind")), str(row.get("name")))
        for row in bindings if isinstance(row, Mapping)
    ]
    if binding_keys != sorted(binding_keys):
        errors.append(f"{label}.runtime_package.bindings must be sorted by kind and name")
    if len(binding_keys) != len(set(binding_keys)):
        errors.append(f"{label}.runtime_package.bindings must not repeat kind + name")
    requirements = recipe.get("provider_requirements")
    requirements = requirements if isinstance(requirements, Mapping) else {}
    capabilities = set(requirements.get("required_capabilities") or [])
    expected_binding_kinds = sorted(
        _CLOUDFLARE_BINDING_KIND_BY_CAPABILITY[capability]
        for capability in capabilities
        if capability in _CLOUDFLARE_BINDING_KIND_BY_CAPABILITY
        and not (
            family == "cloudflare-static-assets"
            and capability == "static_assets"
        )
    )
    actual_binding_kinds = sorted(kind for kind, _ in binding_keys)
    if actual_binding_kinds != expected_binding_kinds:
        errors.append(
            f"{label}.runtime_package binding kinds disagree with deploy recipe"
        )

    secrets = recipe.get("secrets")
    secrets = secrets if isinstance(secrets, list) else []
    expected_environment_names = sorted(
        str(row.get("name"))
        for row in secrets
        if isinstance(row, Mapping) and row.get("required") is True
    )
    actual_environment_names = runtime_package.get("required_environment_names")
    if actual_environment_names != sorted(actual_environment_names or []):
        errors.append(
            f"{label}.runtime_package.required_environment_names must be sorted"
        )
    if actual_environment_names != expected_environment_names:
        errors.append(
            f"{label}.runtime_package environment names disagree with deploy recipe"
        )
    runtime = recipe.get("runtime")
    runtime = runtime if isinstance(runtime, Mapping) else {}
    healthcheck = runtime_package.get("healthcheck")
    healthcheck = healthcheck if isinstance(healthcheck, Mapping) else {}
    if healthcheck.get("path") != runtime.get("healthcheck_path"):
        errors.append(f"{label}.runtime_package healthcheck path disagrees with deploy recipe")

    evidence = {
        "schema": runtime_package.get("schema"),
        "manifest_digest": raw_digest,
        "runtime_family": family,
        "module_format": module_format,
        "payload_root": root,
        "entrypoint": payload.get("entrypoint"),
        "assets_directory": payload.get("assets_directory"),
        "binding_contract_digest": canonical_digest(bindings),
        "environment_contract_digest": canonical_digest(actual_environment_names or []),
        "healthcheck_digest": canonical_digest(healthcheck),
        "source_visibility": runtime_package.get("source_visibility"),
    }
    return evidence, errors


def _marker_pattern(field: str, value: str) -> re.Pattern[str]:
    return re.compile(
        rf"(?i)(?:[\"']?{re.escape(field)}[\"']?\s*[:=]\s*[\"']?"
        rf"{re.escape(value)}(?:[\"']|\s|,|\}}|$))"
    )


def _scoped_marker_findings(
    members: Iterable[tuple[str, bytes]],
    overlay_inputs: Iterable[tuple[Mapping[str, Any], Mapping[str, Any]]],
) -> list[dict[str, str]]:
    markers: list[tuple[str, str, str]] = []
    aliases = {
        "series_id": ("series_id", "series-id", "ZE_SERIES_ID"),
        "organization_id": ("organization_id", "organization-id", "ZE_ORGANIZATION_ID"),
        "target_id": ("target_id", "target-id", "ZE_TARGET_ID"),
        "display_name": ("display_name", "display-name"),
        "theme_ref": ("theme_ref", "theme-ref"),
        "icon_asset_ref": ("icon_asset_ref", "icon-asset-ref"),
    }
    for scope, item in overlay_inputs:
        values: dict[str, Any] = {
            "series_id": scope.get("series_id"),
            "organization_id": scope.get("organization_id"),
            "target_id": scope.get("target_id"),
            "display_name": item.get("display_name"),
            "theme_ref": (item.get("theme") or {}).get("theme_ref")
            if isinstance(item.get("theme"), Mapping)
            else None,
            "icon_asset_ref": item.get("icon_asset_ref"),
        }
        for kind, value in values.items():
            if isinstance(value, str) and value:
                for field in aliases[kind]:
                    markers.append((kind, field, value))

    generic_fields = {
        "series_id": ("series_id", "series-id", "ZE_SERIES_ID"),
        "organization_id": ("organization_id", "organization-id", "ZE_ORGANIZATION_ID"),
        "target_id": ("target_id", "target-id", "ZE_TARGET_ID"),
        "display_name": ("display_name", "display-name"),
        "theme_ref": ("theme_ref", "theme-ref"),
        "icon_asset_ref": ("icon_asset_ref", "icon-asset-ref"),
        "runtime_binding": (
            "api_base_url", "production_url", "account_id", "database_id",
            "namespace_id", "bucket_name",
        ),
        "credential_literal": (
            "access_token", "refresh_token", "api_token", "bot_token",
            "auth_token", "api_key", "client_secret", "private_key", "password",
        ),
    }
    generic_patterns: list[tuple[str, re.Pattern[str]]] = []
    for kind, fields in generic_fields.items():
        for field in fields:
            # credential 类值收窄为「无空白且 >=12 字符」:真实 token 的形态特征。
            # 否则前端 dist bundle 里的 i18n 词表(password:"密码(至少 8 位)")与测试
            # 假密码('w-pass-123')都会假阳性,v3 发布链在任何含 server/webapp 的
            # skill 上都走不通(2026-07-21 console 首通实测)。scope 标记类保持原宽度。
            if kind == "credential_literal":
                value_re = r"(?:\"[^\"\s\r\n]{12,4096}\"|'[^'\s\r\n]{12,4096}')"
            else:
                value_re = r"(?:\"[^\"\r\n]{1,4096}\"|'[^'\r\n]{1,4096}')"
            generic_patterns.append((
                kind,
                re.compile(
                    rf"(?i)(?:[\"']?{re.escape(field)}[\"']?\s*[:=]\s*)" + value_re
                ),
            ))
    generic_patterns.append((
        "private_key",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    ))

    findings: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    compiled = [(kind, _marker_pattern(field, value)) for kind, field, value in markers]
    # W3C autocomplete 标准 token:表单可访问性属性(autoComplete="current-password")会被
    # 前端 bundle 编进产物,是词表值不是凭据字面量(2026-07-21 console v3 首通实测假阳性)。
    benign_credential_values = {"current-password", "new-password", "one-time-code"}
    for member, payload in members:
        text = payload.decode("utf-8", errors="ignore")
        for kind, pattern in [*generic_patterns, *compiled]:
            if (member, kind) in seen:
                continue
            hit = False
            for m in pattern.finditer(text):
                value = m.group(0).split(":", 1)[-1].split("=", 1)[-1].strip().strip("\"'")
                if kind == "credential_literal" and value in benign_credential_values:
                    continue
                hit = True
                break
            if hit:
                seen.add((member, kind))
                findings.append({"member": member, "marker": kind})
    return sorted(findings, key=lambda item: (item["member"], item["marker"]))


def _catalog_snapshot(
    value: Mapping[str, Any],
    *,
    skill_uid: str,
) -> tuple[dict[str, Any] | None, list[str]]:
    errors: list[str] = []
    if value.get("schema") != "ze.catalog-snapshot.v1":
        return None, ["catalog snapshot schema must be ze.catalog-snapshot.v1"]
    required = {
        "schema", "snapshot_id", "projection_kind", "generated_at", "scope",
        "etag", "source_fingerprint", "items",
    }
    unknown = sorted(set(value) - required)
    missing = sorted(required - set(value))
    if unknown:
        errors.append("catalog snapshot contains unknown fields: " + ", ".join(unknown))
    if missing:
        errors.append("catalog snapshot misses fields: " + ", ".join(missing))
        return None, errors
    if value.get("projection_kind") != "control-plane":
        errors.append("catalog snapshot must be a control-plane projection")
    scope = value.get("scope")
    if not isinstance(scope, dict) or set(scope) != {"series_id", "organization_id", "target_id"}:
        errors.append("catalog snapshot scope is invalid")
    source_fingerprint = value.get("source_fingerprint")
    if not isinstance(source_fingerprint, str) or _DIGEST.fullmatch(source_fingerprint) is None:
        errors.append("catalog snapshot source_fingerprint is invalid")
    items = value.get("items")
    if not isinstance(items, list):
        errors.append("catalog snapshot items must be an array")
        items = []
    if not errors:
        expected_etag = canonical_digest({
            "scope": scope,
            "source_fingerprint": source_fingerprint,
            "items": items,
        })
        if value.get("etag") != expected_etag:
            errors.append("catalog snapshot etag mismatch")
        snapshot_id = value.get("snapshot_id")
        if not isinstance(snapshot_id, str) or not snapshot_id.endswith(
            "-" + expected_etag.removeprefix("sha256:")[:12]
        ):
            errors.append("catalog snapshot_id does not close over etag")
    matches = [item for item in items if isinstance(item, dict) and item.get("skill_uid") == skill_uid]
    if len(matches) != 1:
        errors.append("catalog snapshot must contain exactly one item for manifest skill_uid")
        return None, errors
    item = copy.deepcopy(matches[0])
    probe = {
        "schema": OVERLAY_SCHEMA,
        "scope": scope,
        "skill_uid": skill_uid,
        "current_skill_id": item.get("skill_id"),
        "base_artifact_digest": "sha256:" + "0" * 64,
        "catalog_snapshot": {
            "snapshot_id": value.get("snapshot_id"),
            "etag": value.get("etag"),
            "digest": "sha256:" + "0" * 64,
        },
        "catalog": item,
    }
    errors.extend(_schema_errors(probe, OVERLAY_SCHEMA_PATH))
    update = item.get("update_policy") if isinstance(item, dict) else None
    if isinstance(update, Mapping):
        if update.get("channel") == "pinned" and not update.get("pinned_version"):
            errors.append("pinned catalog update requires pinned_version")
        if update.get("channel") != "pinned" and "pinned_version" in update:
            errors.append("non-pinned catalog update cannot carry pinned_version")
    return (item if not errors else None), errors


def build_series_overlay(
    catalog_snapshot: Mapping[str, Any],
    *,
    skill_uid: str,
    base_artifact_digest: str,
    catalog_snapshot_digest: str,
) -> dict[str, Any]:
    """Project exactly one System Catalog item into an offline overlay."""

    item, errors = _catalog_snapshot(catalog_snapshot, skill_uid=skill_uid)
    if errors or item is None:
        raise ValueError("; ".join(errors or ["catalog item unavailable"]))
    if _DIGEST.fullmatch(base_artifact_digest) is None:
        raise ValueError("base_artifact_digest is invalid")
    if _DIGEST.fullmatch(catalog_snapshot_digest) is None:
        raise ValueError("catalog_snapshot_digest is invalid")
    overlay = {
        "schema": OVERLAY_SCHEMA,
        "scope": copy.deepcopy(catalog_snapshot["scope"]),
        "skill_uid": skill_uid,
        "current_skill_id": item["skill_id"],
        "base_artifact_digest": base_artifact_digest,
        "catalog_snapshot": {
            "snapshot_id": catalog_snapshot["snapshot_id"],
            "etag": catalog_snapshot["etag"],
            "digest": catalog_snapshot_digest,
        },
        "catalog": item,
    }
    schema_errors = _schema_errors(overlay, OVERLAY_SCHEMA_PATH)
    if schema_errors:
        raise ValueError("; ".join(schema_errors))
    return overlay


def _sorted_list_errors(value: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    features = value.get("feature_set")
    if isinstance(features, list) and features != sorted(features):
        errors.append("$.variant_inputs.feature_set must be sorted for reproducible identity")
    for field in ("dependency_locks", "code_assets"):
        rows = value.get(field)
        if not isinstance(rows, list):
            continue
        paths = [
            path
            for row in rows
            if isinstance(row, Mapping)
            for path in [row.get("path")]
            if isinstance(path, str)
        ]
        if paths != sorted(paths):
            errors.append(f"$.variant_inputs.{field} must be sorted by path")
        if len(paths) != len(set(paths)):
            errors.append(f"$.variant_inputs.{field} must not repeat a path")
    return errors


_MODE_ROLES: dict[str, frozenset[str]] = {
    "local-full": frozenset({"runtime"}),
    "self-hosted-full": frozenset({"server"}),
    "creator-hosted-api": frozenset({"server", "client"}),
    "frontend-only": frozenset({"client"}),
    "hybrid": frozenset({"server", "local-adapter"}),
    "evidence-only": frozenset({"evidence"}),
}
_OVERLAY_ROLES = frozenset({"runtime", "client", "local-adapter"})
_CHANNEL_ORDER = {"stable": 0, "beta": 1, "pinned": 2}


def _delivery_matrix_errors(
    recipe: Mapping[str, Any], variants: Iterable[Mapping[str, Any]]
) -> list[str]:
    errors: list[str] = []
    declared = set(recipe.get("delivery_modes") or [])
    roles_by_mode: dict[str, set[str]] = {}
    for variant in variants:
        inputs = variant.get("variant_inputs")
        if not isinstance(inputs, Mapping):
            continue
        mode = inputs.get("delivery_mode")
        role = inputs.get("artifact_role")
        if not isinstance(mode, str) or not isinstance(role, str):
            continue
        if mode not in declared:
            errors.append(f"variant delivery_mode is not declared by deploy recipe: {mode}")
        allowed_roles = _MODE_ROLES.get(mode, frozenset())
        if role not in allowed_roles:
            errors.append(f"artifact_role {role} is invalid for delivery_mode {mode}")
        roles_by_mode.setdefault(mode, set()).add(role)
    for mode in sorted(declared):
        required = _MODE_ROLES.get(mode)
        if required is None:
            continue
        missing = sorted(required - roles_by_mode.get(mode, set()))
        if missing:
            errors.append(
                f"delivery matrix for {mode} misses artifact role(s): {', '.join(missing)}"
            )
    return errors


def _source_inventory_validation(
    root: Path,
    reference: Mapping[str, Any],
    recipe: Mapping[str, Any],
) -> tuple[Path | None, dict[str, Any] | None, list[str]]:
    path, errors = _check_file_ref(root, reference, "source_inventory")
    if path is None:
        return None, None, errors
    try:
        inventory = _load_json(path)
    except ValueError as exc:
        return path, None, [*errors, str(exc)]
    errors.extend(_schema_errors(inventory, SOURCE_INVENTORY_SCHEMA_PATH))
    if errors:
        return path, inventory, errors
    selection = inventory["selection"]
    roots = selection["roots"]
    excludes = selection["excludes"]
    relative_inventory_path = path.relative_to(root).as_posix()
    try:
        expected = build_source_inventory(
            root,
            roots=roots,
            excludes=excludes,
            inventory_path=relative_inventory_path,
        )
    except ValueError as exc:
        errors.append(str(exc))
        return path, inventory, errors
    if inventory != expected:
        expected_by_path = {row["path"]: row for row in expected["files"]}
        actual_by_path = {row["path"]: row for row in inventory["files"]}
        missing = sorted(set(expected_by_path) - set(actual_by_path))
        extra = sorted(set(actual_by_path) - set(expected_by_path))
        changed = sorted(
            path_key
            for path_key in set(expected_by_path) & set(actual_by_path)
            if expected_by_path[path_key] != actual_by_path[path_key]
        )
        errors.append(
            "source inventory does not match selected files"
            + (f"; missing={missing[:8]}" if missing else "")
            + (f"; extra={extra[:8]}" if extra else "")
            + (f"; changed={changed[:8]}" if changed else "")
        )
    raw_artifact = recipe.get("artifact")
    artifact: Mapping[str, Any] = raw_artifact if isinstance(raw_artifact, Mapping) else {}
    raw_includes = artifact.get("include")
    includes: list[Any] = raw_includes if isinstance(raw_includes, list) else []
    for include in includes:
        if not isinstance(include, str):
            continue
        literal = include.replace("\\", "/").split("*", 1)[0].split("?", 1)[0].rstrip("/") or "."
        if not any(
            selected == "."
            or literal == selected
            or literal.startswith(selected + "/")
            for selected in roots
        ):
            errors.append(f"deploy artifact include is outside source inventory roots: {include}")
    return path, inventory, errors


def validate_artifact_variant(
    root: Path,
    manifest: Mapping[str, Any],
    *,
    recipe: Mapping[str, Any],
    compatibility_profile: Mapping[str, Any],
    manifest_path: Path | None = None,
) -> dict[str, Any]:
    root = root.resolve()
    errors = _schema_errors(manifest, MANIFEST_SCHEMA_PATH)
    warnings: list[str] = []
    if errors:
        return {
            "valid": False,
            "errors": errors,
            "warnings": warnings,
            "artifact_variant": {"schema": MANIFEST_SCHEMA, "status": "invalid"},
        }

    if recipe.get("schema") != RECIPE_SCHEMA:
        errors.append("artifact variant manifest requires ze.deploy-recipe.v3")
    if manifest.get("skill_uid") != recipe.get("skill_uid"):
        errors.append("manifest skill_uid disagrees with deploy recipe")
    if manifest.get("current_skill_id") != recipe.get("skill_id"):
        errors.append("manifest current_skill_id disagrees with deploy recipe")
    source_inventory_path, source_inventory, source_errors = _source_inventory_validation(
        root, manifest["source_inventory"], recipe
    )
    errors.extend(source_errors)
    computed_source_digest = (
        canonical_digest(source_inventory) if source_inventory is not None else None
    )
    if computed_source_digest != manifest.get("source_digest"):
        errors.append("source_digest mismatch")
    variant_ids = [variant["variant_id"] for variant in manifest["variants"]]
    if variant_ids != sorted(variant_ids):
        errors.append("$.variants must be sorted by variant_id")
    overlay_order = [
        (
            entry["scope"]["series_id"],
            entry["scope"]["organization_id"],
            entry["scope"]["target_id"],
            entry["base_artifact_id"],
        )
        for entry in manifest["series_overlays"]
    ]
    if overlay_order != sorted(overlay_order):
        errors.append("$.series_overlays must be sorted by scope and base_artifact_id")
    channels = manifest["update_candidate"]["channel_eligibility"]
    if channels != sorted(channels, key=lambda value: _CHANNEL_ORDER[value]):
        errors.append("$.update_candidate.channel_eligibility is not in canonical order")
    compatibility_digest = canonical_digest(dict(compatibility_profile))
    surface_path, surface_errors = _safe_file(
        root, "ZE_SURFACE.json", "variant_inputs.surface_profile"
    )
    errors.extend(surface_errors)

    variants = manifest["variants"]
    errors.extend(_delivery_matrix_errors(recipe, variants))
    variant_evidence: list[dict[str, Any]] = []
    base_by_id: dict[str, dict[str, Any]] = {}
    used_variant_ids: set[str] = set()
    used_build_keys: set[str] = set()
    used_artifact_ids: set[str] = set()
    reserved_output_paths: set[Path] = set()
    all_input_paths: set[Path] = set()
    signature_public_keys: dict[Path, str] = {}
    signature_output_paths: set[Path] = set()

    def register_signature_paths(
        paths: Mapping[str, Path | None],
        record: Mapping[str, Any],
        label: str,
    ) -> None:
        public_path = paths.get("public_key")
        signature_path = paths.get("detached_signature")
        public_ref = record.get("public_key")
        public_digest = (
            str(public_ref.get("digest"))
            if isinstance(public_ref, Mapping)
            else ""
        )
        if public_path is not None:
            if public_path in reserved_output_paths or public_path in signature_output_paths:
                errors.append(f"{label}.public_key cannot alias an artifact output")
            if public_path == source_inventory_path:
                errors.append(f"{label}.public_key cannot alias the source inventory")
            previous_digest = signature_public_keys.get(public_path)
            if previous_digest is not None and previous_digest != public_digest:
                errors.append(f"{label}.public_key path has inconsistent digests")
            signature_public_keys[public_path] = public_digest
        if signature_path is not None:
            if signature_path in signature_output_paths:
                errors.append(f"{label}.detached_signature duplicates another signature output")
            if signature_path in signature_public_keys:
                errors.append(f"{label}.detached_signature cannot alias a public key")
            if signature_path in reserved_output_paths or signature_path in all_input_paths:
                errors.append(f"{label}.detached_signature cannot alias an input or artifact output")
            if signature_path == source_inventory_path:
                errors.append(f"{label}.detached_signature cannot alias the source inventory")
            signature_output_paths.add(signature_path)
            reserved_output_paths.add(signature_path)

    for variant_index, variant in enumerate(variants):
        label = f"variants[{variant_index}]"
        variant_id = variant["variant_id"]
        if variant_id in used_variant_ids:
            errors.append(f"{label}.variant_id duplicates another variant")
        used_variant_ids.add(variant_id)

        inputs = variant["variant_inputs"]
        errors.extend(_sorted_list_errors(inputs))
        input_paths: set[Path] = set()
        if surface_path is not None:
            input_paths.add(surface_path)
            if inputs.get("surface_profile_digest") != file_digest(surface_path):
                errors.append(f"{label}.variant_inputs.surface_profile_digest mismatch")
        if inputs.get("compatibility_profile_digest") != compatibility_digest:
            errors.append(f"{label}.variant_inputs.compatibility_profile_digest mismatch")
        for field in ("dependency_locks", "code_assets"):
            for file_index, file_ref in enumerate(inputs.get(field) or []):
                path, file_errors = _check_file_ref(
                    root,
                    file_ref,
                    f"{label}.variant_inputs.{field}[{file_index}]",
                )
                errors.extend(file_errors)
                if path is not None and path in input_paths:
                    errors.append(
                        f"{label}.variant_inputs.{field}[{file_index}].path aliases another input"
                    )
                if path is not None:
                    input_paths.add(path)
                    all_input_paths.add(path)
        all_input_paths.update(input_paths)

        computed_inputs_digest = variant_inputs_digest(inputs)
        if variant.get("variant_inputs_digest") != computed_inputs_digest:
            errors.append(f"{label}.variant_inputs_digest mismatch")
        computed_build_key = base_build_key(
            str(manifest.get("skill_uid")),
            str(manifest.get("source_digest")),
            computed_inputs_digest,
        )
        if variant.get("build_key") != computed_build_key:
            errors.append(f"{label}.build_key mismatch")
        if computed_build_key in used_build_keys:
            errors.append(f"{label}.build_key duplicates another variant")
        used_build_keys.add(computed_build_key)

        base = variant["base_artifact"]
        artifact_id = base["artifact_id"]
        if artifact_id in used_artifact_ids:
            errors.append(f"{label}.base_artifact.artifact_id duplicates another variant")
        used_artifact_ids.add(artifact_id)
        if base.get("compatibility_profile_digest") != compatibility_digest:
            errors.append(f"{label}.base_artifact.compatibility_profile_digest mismatch")
        if base.get("delivery_mode") != inputs.get("delivery_mode"):
            errors.append(f"{label}.base_artifact.delivery_mode disagrees with variant inputs")
        if base.get("artifact_role") != inputs.get("artifact_role"):
            errors.append(f"{label}.base_artifact.artifact_role disagrees with variant inputs")

        base_path, base_errors = _check_file_ref(root, base, f"{label}.base_artifact")
        errors.extend(base_errors)
        sbom_path, sbom_errors = _check_file_ref(
            root, base["sbom"], f"{label}.base_artifact.sbom"
        )
        errors.extend(sbom_errors)
        if sbom_path is not None:
            errors.extend(_validate_sbom(sbom_path))
        for path, field in (
            (base_path, f"{label}.base_artifact.path"),
            (sbom_path, f"{label}.base_artifact.sbom.path"),
        ):
            if path is not None and path in input_paths:
                errors.append(f"{field} cannot also be a variant input")
            if path is not None and path in reserved_output_paths:
                errors.append(f"{field} aliases another variant output")
            if path is not None and path in signature_public_keys:
                errors.append(f"{field} cannot alias a signature public key")
            if path is not None:
                reserved_output_paths.add(path)
        signature_evidence, signature_paths, signature_errors = _verify_artifact_signature(
            root,
            base["signature"],
            artifact_digest=str(base["digest"]),
            label=f"{label}.base_artifact.signature",
        )
        errors.extend(signature_errors)
        register_signature_paths(
            signature_paths,
            base["signature"],
            f"{label}.base_artifact.signature",
        )
        archive_members: list[tuple[str, bytes]] = []
        if base_path is not None:
            archive_members, archive_errors = _archive_members(
                base_path, str(base.get("format"))
            )
            errors.extend(f"{label}: {error}" for error in archive_errors)
        runtime_package_evidence, runtime_package_errors = _runtime_package_validation(
            members=archive_members,
            base=base,
            inputs=inputs,
            recipe=recipe,
            skill_uid=str(manifest["skill_uid"]),
            label=label,
        )
        errors.extend(runtime_package_errors)
        dependency_manifest_evidence, dependency_manifest_errors = (
            _dependency_manifest_archive_validation(
                root=root,
                members=archive_members,
                inputs=inputs,
                label=label,
            )
        )
        errors.extend(dependency_manifest_errors)
        base_by_id[artifact_id] = {
            "record": base,
            "path": base_path,
            "members": archive_members,
        }
        variant_evidence.append({
            "variant_id": variant_id,
            "runtime_variant": inputs["runtime_variant"],
            "variant_inputs_digest": computed_inputs_digest,
            "build_key": computed_build_key,
            "artifact_id": artifact_id,
            "artifact_role": base["artifact_role"],
            "delivery_mode": base["delivery_mode"],
            "artifact_digest": base["digest"],
            "sbom_digest": base["sbom"]["digest"],
            "signature": signature_evidence,
            "compatibility_profile_digest": compatibility_digest,
            "runtime_package": runtime_package_evidence,
            "dependency_manifest": dependency_manifest_evidence,
        })

    for path in sorted(all_input_paths):
        if path in reserved_output_paths:
            errors.append(f"variant input aliases a Base/SBOM output: {path.relative_to(root)}")
    if source_inventory_path is not None and (
        source_inventory_path in reserved_output_paths
        or source_inventory_path in all_input_paths
    ):
        errors.append("source_inventory.path cannot alias a variant input or output")

    overlay_inputs: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    overlay_evidence: list[dict[str, Any]] = []
    used_overlay_bindings: set[tuple[str, str, str, str]] = set()
    used_overlay_paths: set[Path] = set()
    catalog_paths: dict[Path, str] = {}
    for index, entry in enumerate(manifest["series_overlays"]):
        label = f"series_overlays[{index}]"
        scope = entry["scope"]
        binding_key = (
            scope["series_id"], scope["organization_id"], scope["target_id"],
            entry["base_artifact_id"],
        )
        if binding_key in used_overlay_bindings:
            errors.append(f"{label} duplicates another scope + Base binding")
        used_overlay_bindings.add(binding_key)

        base_info = base_by_id.get(entry["base_artifact_id"])
        if base_info is None:
            errors.append(f"{label}.base_artifact_id is unknown")
        else:
            selected_base = base_info["record"]
            if entry.get("base_artifact_digest") != selected_base.get("digest"):
                errors.append(f"{label}.base_artifact_digest mismatch")
            if selected_base.get("artifact_role") not in _OVERLAY_ROLES:
                errors.append(f"{label} cannot attach presentation data to a server/evidence artifact")

        artifact_path, artifact_errors = _check_file_ref(
            root, entry["artifact"], f"{label}.artifact"
        )
        errors.extend(artifact_errors)
        snapshot_path, snapshot_errors = _check_file_ref(
            root, entry["catalog_snapshot"], f"{label}.catalog_snapshot"
        )
        errors.extend(snapshot_errors)
        if artifact_path is not None:
            if artifact_path in reserved_output_paths or artifact_path in all_input_paths:
                errors.append(f"{label}.artifact.path aliases a Base/SBOM/input file")
            if artifact_path in signature_public_keys:
                errors.append(f"{label}.artifact.path aliases a signature public key")
            if artifact_path in used_overlay_paths:
                errors.append(f"{label}.artifact.path duplicates another overlay")
            used_overlay_paths.add(artifact_path)
            reserved_output_paths.add(artifact_path)
        signature_evidence, signature_paths, signature_errors = _verify_artifact_signature(
            root,
            entry["artifact"]["signature"],
            artifact_digest=str(entry["artifact"]["digest"]),
            label=f"{label}.artifact.signature",
        )
        errors.extend(signature_errors)
        register_signature_paths(
            signature_paths,
            entry["artifact"]["signature"],
            f"{label}.artifact.signature",
        )
        if snapshot_path is not None:
            prior_digest = catalog_paths.get(snapshot_path)
            current_digest = str(entry["catalog_snapshot"]["digest"])
            if prior_digest is not None and prior_digest != current_digest:
                errors.append(f"{label}.catalog_snapshot.path has inconsistent digests")
            catalog_paths[snapshot_path] = current_digest
            if snapshot_path in reserved_output_paths or snapshot_path in all_input_paths:
                errors.append(f"{label}.catalog_snapshot.path aliases a build input/output")
            if snapshot_path in signature_public_keys:
                errors.append(f"{label}.catalog_snapshot.path aliases a signature public key")
        if artifact_path is None or snapshot_path is None:
            continue
        try:
            snapshot = _load_json(snapshot_path)
            overlay = _load_json(artifact_path)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        item, snapshot_contract_errors = _catalog_snapshot(
            snapshot, skill_uid=str(manifest["skill_uid"])
        )
        errors.extend(f"{label}: {error}" for error in snapshot_contract_errors)
        if item is None:
            continue
        if snapshot.get("scope") != scope:
            errors.append(f"{label}.scope disagrees with catalog snapshot")
        snapshot_ref = entry["catalog_snapshot"]
        if snapshot_ref.get("snapshot_id") != snapshot.get("snapshot_id"):
            errors.append(f"{label}.catalog_snapshot.snapshot_id mismatch")
        if snapshot_ref.get("etag") != snapshot.get("etag"):
            errors.append(f"{label}.catalog_snapshot.etag mismatch")
        if base_info is None:
            continue
        try:
            expected_overlay = build_series_overlay(
                snapshot,
                skill_uid=str(manifest["skill_uid"]),
                base_artifact_digest=str(entry["base_artifact_digest"]),
                catalog_snapshot_digest=str(snapshot_ref["digest"]),
            )
        except ValueError as exc:
            errors.append(f"{label}: {exc}")
            continue
        if overlay != expected_overlay:
            errors.append(f"{label}.artifact is not the exact Catalog-derived overlay")
        overlay_inputs.append((scope, item))
        overlay_evidence.append({
            "scope": copy.deepcopy(scope),
            "base_artifact_id": entry["base_artifact_id"],
            "base_artifact_digest": entry["base_artifact_digest"],
            "artifact_digest": entry["artifact"]["digest"],
            "signature": signature_evidence,
            "catalog_snapshot_digest": snapshot_ref["digest"],
            "catalog_etag": snapshot_ref["etag"],
            "authorization": "runtime-check-required",
        })

    for path in sorted(set(catalog_paths) & set(signature_public_keys)):
        errors.append(
            "signature public key aliases a Catalog snapshot: "
            f"{path.relative_to(root)}"
        )
    for path in sorted(set(catalog_paths) & signature_output_paths):
        errors.append(
            "detached signature aliases a Catalog snapshot: "
            f"{path.relative_to(root)}"
        )

    scope_findings: list[dict[str, str]] = []
    inspected_members = 0
    for artifact_id, base_info in sorted(base_by_id.items()):
        members = base_info["members"]
        inspected_members += len(members)
        findings = _scoped_marker_findings(members, overlay_inputs)
        for finding in findings:
            scope_findings.append({"artifact_id": artifact_id, **finding})
    if scope_findings:
        errors.append("Base Artifact contains scoped presentation, runtime resource, or credential bindings")

    manifest_digest = (
        file_digest(manifest_path)
        if manifest_path is not None and manifest_path.is_file()
        else canonical_digest(dict(manifest))
    )
    evidence = {
        "schema": MANIFEST_SCHEMA,
        "status": "ready" if not errors else "invalid",
        "manifest_digest": manifest_digest,
        "skill_uid": manifest["skill_uid"],
        "current_skill_id": manifest["current_skill_id"],
        "source_digest": manifest["source_digest"],
        "source_inventory": {
            "digest": manifest["source_inventory"]["digest"],
            "file_count": len(source_inventory.get("files") or [])
            if source_inventory is not None
            else 0,
            "selection_policy": (
                source_inventory.get("selection") or {}
            ).get("policy")
            if source_inventory is not None
            else None,
        },
        "variants": variant_evidence,
        "series_overlays": overlay_evidence,
        "update_candidate": copy.deepcopy(manifest["update_candidate"]),
        "isolation_evidence": {
            "scan_policy": manifest["isolation"]["scan_policy"],
            "base_scope": manifest["isolation"]["base_scope"],
            "overlay_scope": manifest["isolation"]["overlay_scope"],
            "inspected_members": inspected_members,
            "scope_scan": "passed" if not scope_findings else "failed",
            "findings": scope_findings,
            "base_artifact_count": len(base_by_id),
            "overlay_binding_count": len(used_overlay_bindings),
            "overlay_scope_count": len({binding[:3] for binding in used_overlay_bindings}),
            "signature_count": len(signature_output_paths),
            "signing_key_count": len(signature_public_keys),
        },
    }
    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "artifact_variant": evidence,
    }


def evaluate_artifact_variant(
    root: Path,
    recipe: Mapping[str, Any] | None = None,
    compatibility_profile: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    root = root.resolve()
    loaded_recipe = dict(recipe) if recipe is not None else load_recipe(root)
    path = root / MANIFEST_NAME
    if not path.exists():
        if loaded_recipe.get("schema") == RECIPE_SCHEMA:
            return {
                "valid": False,
                "errors": [f"portable recipe v3 requires {MANIFEST_NAME}"],
                "warnings": [],
                "artifact_variant": {
                    "schema": MANIFEST_SCHEMA,
                    "status": "required",
                },
            }
        return {
            "valid": True,
            "errors": [],
            "warnings": [
                f"{MANIFEST_NAME} absent: legacy/v2 Skill remains readable but is not scoped-release ready"
            ],
            "artifact_variant": {
                "schema": MANIFEST_SCHEMA,
                "status": "legacy-unscoped",
            },
        }
    try:
        manifest = _load_json(path)
    except ValueError as exc:
        return {
            "valid": False,
            "errors": [str(exc)],
            "warnings": [],
            "artifact_variant": {"schema": MANIFEST_SCHEMA, "status": "invalid"},
        }
    if compatibility_profile is None:
        compatibility_profile = evaluate_deployment_profile(
            root, loaded_recipe
        )["deployment_profile"]
    return validate_artifact_variant(
        root,
        manifest,
        recipe=loaded_recipe,
        compatibility_profile=compatibility_profile,
        manifest_path=path,
    )


def _write_overlay(args: argparse.Namespace) -> int:
    snapshot_path = Path(args.catalog).resolve()
    try:
        snapshot = _load_json(snapshot_path)
        overlay = build_series_overlay(
            snapshot,
            skill_uid=args.skill_uid,
            base_artifact_digest=args.base_digest,
            catalog_snapshot_digest=file_digest(snapshot_path),
        )
    except ValueError as exc:
        print(json.dumps({"valid": False, "errors": [str(exc)]}, ensure_ascii=False, indent=2))
        return 1
    output = Path(args.out).resolve()
    if output.is_symlink():
        print(json.dumps({"valid": False, "errors": ["output must not be a symlink"]}, ensure_ascii=False, indent=2))
        return 1
    if output.exists() and not args.force:
        print(json.dumps({"valid": False, "errors": ["output exists; pass --force to replace"]}, ensure_ascii=False, indent=2))
        return 1
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = canonical_json_bytes(overlay) + b"\n"
    fd, tmp_name = tempfile.mkstemp(prefix=output.name + ".", dir=output.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, output)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
    print(json.dumps({
        "valid": True,
        "output": str(output),
        "digest": file_digest(output),
        "scope": overlay["scope"],
    }, ensure_ascii=False, indent=2))
    return 0


def _write_source_inventory(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    relative_output = _canonical_selection_path(args.out)
    if relative_output is None:
        print(json.dumps({"valid": False, "errors": ["--out must be a relative path inside root"]}, ensure_ascii=False, indent=2))
        return 1
    output = root.joinpath(*PurePosixPath(relative_output).parts)
    if output.is_symlink():
        print(json.dumps({"valid": False, "errors": ["output must not be a symlink"]}, ensure_ascii=False, indent=2))
        return 1
    if output.exists() and not args.force:
        print(json.dumps({"valid": False, "errors": ["output exists; pass --force to replace"]}, ensure_ascii=False, indent=2))
        return 1
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        output.parent.resolve(strict=True).relative_to(root)
    except (OSError, ValueError):
        print(json.dumps({"valid": False, "errors": ["output parent escapes root"]}, ensure_ascii=False, indent=2))
        return 1
    excludes = sorted(set([
        MANIFEST_NAME,
        "ZE_BUILD_EVIDENCE.json",
        "artifacts",
        "catalog",
        relative_output,
        *args.exclude,
    ]))
    roots = sorted(set(args.selection_root or ["."]))
    try:
        inventory = build_source_inventory(
            root,
            roots=roots,
            excludes=excludes,
            inventory_path=relative_output,
        )
    except ValueError as exc:
        print(json.dumps({"valid": False, "errors": [str(exc)]}, ensure_ascii=False, indent=2))
        return 1
    payload = canonical_json_bytes(inventory) + b"\n"
    fd, tmp_name = tempfile.mkstemp(prefix=output.name + ".", dir=output.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, output)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
    print(json.dumps({
        "valid": True,
        "output": str(output),
        "inventory_digest": file_digest(output),
        "source_digest": canonical_digest(inventory),
        "file_count": len(inventory["files"]),
    }, ensure_ascii=False, indent=2))
    return 0


def _write_detached_signature(args: argparse.Namespace) -> int:
    private_input = Path(args.private_key)
    if private_input.is_symlink():
        print(json.dumps({"valid": False, "errors": ["private key must not be a symlink"]}, ensure_ascii=False, indent=2))
        return 1
    try:
        private_path = private_input.resolve(strict=True)
    except (FileNotFoundError, OSError):
        print(json.dumps({"valid": False, "errors": ["private key is missing"]}, ensure_ascii=False, indent=2))
        return 1
    if not private_path.is_file():
        print(json.dumps({"valid": False, "errors": ["private key must be a regular file"]}, ensure_ascii=False, indent=2))
        return 1
    output = Path(args.out).resolve()
    if output == private_path:
        print(json.dumps({"valid": False, "errors": ["signature output cannot replace the private key"]}, ensure_ascii=False, indent=2))
        return 1
    if output.is_symlink():
        print(json.dumps({"valid": False, "errors": ["output must not be a symlink"]}, ensure_ascii=False, indent=2))
        return 1
    if output.exists() and not args.force:
        print(json.dumps({"valid": False, "errors": ["output exists; pass --force to replace"]}, ensure_ascii=False, indent=2))
        return 1
    password: bytes | None = None
    if args.password_env:
        raw_password = os.environ.get(args.password_env)
        if raw_password is None:
            print(json.dumps({"valid": False, "errors": [f"password environment variable is missing: {args.password_env}"]}, ensure_ascii=False, indent=2))
            return 1
        password = raw_password.encode("utf-8")
    try:
        signature = sign_artifact_digest(
            private_path.read_bytes(),
            args.digest,
            password=password,
        )
    except (OSError, ValueError) as exc:
        print(json.dumps({"valid": False, "errors": [str(exc)]}, ensure_ascii=False, indent=2))
        return 1
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=output.name + ".", dir=output.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(signature)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, output)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
    print(json.dumps({
        "valid": True,
        "output": str(output),
        "signed_digest": args.digest,
        "signature_digest": file_digest(output),
        "algorithm": "ed25519",
    }, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if args and args[0] == "source-inventory":
        parser = argparse.ArgumentParser(description="Generate a deterministic ZE build source inventory")
        parser.add_argument("source-inventory", nargs="?")
        parser.add_argument("--root", default=".")
        parser.add_argument("--out", default="build-inputs/source-inventory.json")
        parser.add_argument("--selection-root", action="append", default=[])
        parser.add_argument("--exclude", action="append", default=[])
        parser.add_argument("--force", action="store_true")
        return _write_source_inventory(parser.parse_args(args))
    if args and args[0] == "project-overlay":
        parser = argparse.ArgumentParser(description="Project one System Catalog item into a Series overlay")
        parser.add_argument("project-overlay", nargs="?")
        parser.add_argument("--catalog", required=True)
        parser.add_argument("--skill-uid", required=True)
        parser.add_argument("--base-digest", required=True)
        parser.add_argument("--out", required=True)
        parser.add_argument("--force", action="store_true")
        return _write_overlay(parser.parse_args(args))
    if args and args[0] == "sign-digest":
        parser = argparse.ArgumentParser(description="Create an Ed25519 detached artifact signature")
        parser.add_argument("sign-digest", nargs="?")
        parser.add_argument("--private-key", required=True)
        parser.add_argument("--digest", required=True)
        parser.add_argument("--out", required=True)
        parser.add_argument("--password-env")
        parser.add_argument("--force", action="store_true")
        return _write_detached_signature(parser.parse_args(args))

    parser = argparse.ArgumentParser(description="Validate ZE Base Artifact and scoped overlays")
    parser.add_argument("root", nargs="?", default=".")
    parsed = parser.parse_args(args)
    result = evaluate_artifact_variant(Path(parsed.root))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    sys.exit(main())
