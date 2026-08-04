#!/usr/bin/env python3
"""Layer ownership manifest for ZE direct-copy sync.

`ZE_LAYER_MANIFEST.json` is the machine-readable boundary between:
  - ZE Skill Build managed layers that may be copied over, and
  - downstream product/runtime paths that must be preserved.

The manifest is a projection of the already-validated
`ZE_SURFACE.json.web.product_dir`.  It may never invent or widen that boundary:
legacy `webapp/src/product`, `web/src/product`, and reserved `product-*`
directories remain directly copyable without letting core directories masquerade
as downstream-owned product code.
"""
from __future__ import annotations

import copy
import json
import os
import stat
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

try:
    from surface_profile import (
        load_surface_profile,
        validate_managed_surface_paths,
        validate_surface_profile,
    )
except Exception:  # pragma: no cover - old partially-synced targets
    load_surface_profile = None
    validate_managed_surface_paths = None
    validate_surface_profile = None

SCHEMA = "ze.layer_manifest.v1"
MANIFEST_FILE = "ZE_LAYER_MANIFEST.json"

REQUIRED_PRESERVE = (
    "02-mod", "02-mod/_locks", ".data", "ZE_SURFACE.json", MANIFEST_FILE,
    "ZE_DEPENDENCIES.json", "ZE_ARTIFACT_BUILD.json", "ZE_ARTIFACT_VARIANT.json",
)


def _reject_duplicate_keys(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate JSON key: {key}")
        out[key] = value
    return out


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeError, ValueError):
        return default


def _manifest_path_error(path: Path) -> str | None:
    """Inspect the authority file itself without following a symbolic link."""
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"cannot safely inspect {MANIFEST_FILE}: {type(exc).__name__}"
    if stat.S_ISLNK(metadata.st_mode):
        return f"{MANIFEST_FILE} must not be a symlink"
    return None


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    """Replace a manifest without exposing readers to a partial JSON file."""
    path_error = _manifest_path_error(path)
    if path_error:
        raise ValueError(path_error)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        # Re-check after writing the temporary file.  A manifest symlink must
        # never be treated as something atomic replacement can "repair".
        path_error = _manifest_path_error(path)
        if path_error:
            raise ValueError(path_error)
        os.replace(tmp_path, path)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def _clean_rel_path(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    raw = value.strip().replace("\\", "/").rstrip("/")
    if not raw:
        return None
    p = PurePosixPath(raw)
    if p.is_absolute() or ".." in p.parts:
        return None
    return p.as_posix()


def _is_under(rel: str, parent: str) -> bool:
    r = PurePosixPath(rel)
    p = PurePosixPath(parent)
    return r == p or p in r.parents


def _surface_product_boundary(root: Path) -> tuple[list[str], str | None]:
    """Return the verified product directory and its managed merge parent.

    An unavailable/broken/invalid Surface Profile is a hard error.  Directory
    existence is deliberately not a fallback authority.
    """
    if (load_surface_profile is None or validate_surface_profile is None
            or validate_managed_surface_paths is None):
        raise ValueError("Surface Profile validator unavailable; product boundary cannot be inferred")
    try:
        profile = load_surface_profile(root)
        # Scaffolding writes authority before copying its declared paths.  The
        # declaration must already be semantically valid, but path existence is
        # proved by the normal post-copy Surface gate.
        surface_errors = validate_surface_profile(root, profile, require_paths=False)
        surface_errors += validate_managed_surface_paths(profile)
    except Exception as exc:
        raise ValueError(
            f"ZE_SURFACE.json validation failed: {type(exc).__name__}"
        ) from exc
    if surface_errors:
        raise ValueError("invalid ZE_SURFACE.json: " + "; ".join(surface_errors))

    surfaces = profile.get("surfaces")
    if not isinstance(surfaces, list) or "web" not in surfaces:
        return [], None
    web = profile.get("web")
    if not isinstance(web, dict):  # validated above; defensive against drift
        raise ValueError("invalid ZE_SURFACE.json: web surface requires a web object")
    product_dir = _clean_rel_path(web.get("product_dir"))
    if product_dir is None:
        raise ValueError("invalid ZE_SURFACE.json: web.product_dir is not canonical")
    return [product_dir], PurePosixPath(product_dir).parent.as_posix()


def _surface_product_dirs(root: Path) -> list[str]:
    product_dirs, _merge_path = _surface_product_boundary(root)
    return product_dirs


def default_manifest(root: Path, *, toolchain_files: list[str] | tuple[str, ...] | None = None) -> dict[str, Any]:
    """Build a deterministic default manifest for a current ZE Skill root."""
    product_dirs, merge_path = _surface_product_boundary(root)
    toolchain = list(toolchain_files or [])
    return {
        "schema": SCHEMA,
        "generated_by": "ZE-skill-build/scripts/layer_manifest.py",
        "managed": {
            "replace_dirs": {
                "core": ["01-core", "sdk"],
                "ops": ["04-ops"],
                "i18n": ["05-i18n"],
                "css_opt_in": ["03-css-rules"],
            },
            "merge_dirs": {
                "core": [
                    {
                        "path": merge_path,
                        "preserve": product_dirs,
                        "reason": "surface-declared product frontend is target-owned",
                    }
                ] if product_dirs else []
            },
            "files": {
                "core": ["server.py", "scripts/ze_command.py"],
                "toolchain": toolchain,
            },
        },
        "product": {
            "dirs": product_dirs,
            "mod_dirs": ["02-mod"],
            "runtime_dirs": [".data"],
            "metadata_files": [
                "ZE_SURFACE.json", "deploy.json", "ZE_DEPENDENCIES.json",
                "ZE_ARTIFACT_BUILD.json", "ZE_ARTIFACT_VARIANT.json",
            ],
        },
        "preserve": list(REQUIRED_PRESERVE) + ["deploy.json", "commands/registry.json", "demo"],
        "rules": [
            "Managed replace paths may be deleted and recopied from ZE Skill Build.",
            "Managed merge paths must preserve every product.dirs entry.",
            "Product, runtime, surface, deploy, dependency, artifact-build, release-manifest, and lock paths are target-owned.",
            "Local changes under managed paths must move to ZE Skill Build or to a product/mod path before sync.",
        ],
    }


def load_layer_manifest(root: Path) -> dict[str, Any]:
    path = root / MANIFEST_FILE
    path_error = _manifest_path_error(path)
    if path_error:
        return {
            "_inferred": False,
            "_load_error": path_error,
        }
    if path.exists():
        try:
            data = json.loads(
                path.read_text(encoding="utf-8"),
                object_pairs_hook=_reject_duplicate_keys,
            )
        except json.JSONDecodeError as exc:
            return {
                "_inferred": False,
                "_load_error": f"invalid JSON at line {exc.lineno} column {exc.colno}",
            }
        except ValueError as exc:
            return {"_inferred": False, "_load_error": str(exc)}
        except (OSError, UnicodeError) as exc:
            return {
                "_inferred": False,
                "_load_error": f"unreadable {MANIFEST_FILE}: {type(exc).__name__}",
            }
        if not isinstance(data, dict):
            return {
                "_inferred": False,
                "_load_error": f"{MANIFEST_FILE} must contain a JSON object",
            }
        return data
    inferred = default_manifest(root)
    inferred["_inferred"] = True
    return inferred


def write_layer_manifest(
    root: Path,
    *,
    toolchain_files: list[str] | tuple[str, ...] | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Write `ZE_LAYER_MANIFEST.json` if missing or overwrite is requested."""
    path = root / MANIFEST_FILE
    path_error = _manifest_path_error(path)
    if path_error:
        return {
            "status": "invalid",
            "path": str(path),
            "errors": [path_error],
        }
    if path.exists() and not overwrite:
        manifest = load_layer_manifest(root)
        errors = validate_layer_manifest(root, manifest)
        if toolchain_files is not None:
            errors += toolchain_inventory_errors(manifest, toolchain_files)
        return {"status": "existing", "path": str(path), "errors": errors}
    manifest = default_manifest(root, toolchain_files=toolchain_files)
    _write_json_atomic(path, manifest)
    return {"status": "written", "path": str(path), "errors": validate_layer_manifest(root, manifest)}


def toolchain_inventory_errors(
    manifest: dict[str, Any],
    expected_files: list[str] | tuple[str, ...],
) -> list[str]:
    """Require the ownership manifest to name the exact managed toolchain set."""
    managed = manifest.get("managed") if isinstance(manifest, dict) else None
    files = managed.get("files") if isinstance(managed, dict) else None
    actual_raw = files.get("toolchain") if isinstance(files, dict) else None
    if not isinstance(actual_raw, list):
        return ["managed.files.toolchain must be a list"]

    errors: list[str] = []
    actual: list[str] = []
    for item in actual_raw:
        cleaned = _clean_rel_path(item)
        if cleaned is None:
            errors.append(f"invalid managed.files.toolchain path: {item!r}")
        else:
            actual.append(cleaned)
    expected = [cleaned for item in expected_files if (cleaned := _clean_rel_path(item))]
    if len(actual) != len(set(actual)):
        errors.append("managed.files.toolchain contains duplicate paths")
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    if missing:
        errors.append("managed.files.toolchain missing: " + ", ".join(missing))
    if extra:
        errors.append("managed.files.toolchain has unexpected entries: " + ", ".join(extra))
    if not missing and not extra and actual != expected:
        errors.append("managed.files.toolchain order differs from the canonical toolchain inventory")
    return errors


def reconcile_toolchain_files(
    root: Path,
    *,
    toolchain_files: list[str] | tuple[str, ...],
) -> dict[str, Any]:
    """Update only toolchain ownership, preserving every downstream-owned field.

    Existing malformed manifests are never replaced with inferred defaults: doing
    so could silently erase custom product/preserve boundaries.
    """
    path = root / MANIFEST_FILE
    path_error = _manifest_path_error(path)
    if path_error:
        return {
            "status": "invalid",
            "path": str(path),
            "errors": [path_error],
        }
    if not path.exists():
        return write_layer_manifest(root, toolchain_files=toolchain_files)
    raw = _read_json(path, None)
    if not isinstance(raw, dict):
        return {
            "status": "invalid",
            "path": str(path),
            "errors": ["ZE_LAYER_MANIFEST.json must contain a JSON object"],
        }
    structural_errors = validate_layer_manifest(root, raw)
    managed = raw.get("managed")
    files = managed.get("files") if isinstance(managed, dict) else None
    if structural_errors or not isinstance(files, dict):
        errors = list(structural_errors)
        if not isinstance(files, dict):
            errors.append("managed.files must be an object")
        return {"status": "invalid", "path": str(path), "errors": errors}

    expected = list(toolchain_files)
    if files.get("toolchain") == expected:
        return {
            "status": "existing",
            "path": str(path),
            "errors": toolchain_inventory_errors(raw, expected),
        }
    updated = copy.deepcopy(raw)
    updated["managed"]["files"]["toolchain"] = expected
    errors = validate_layer_manifest(root, updated) + toolchain_inventory_errors(updated, expected)
    if errors:
        return {"status": "invalid", "path": str(path), "errors": errors}
    _write_json_atomic(path, updated)
    return {"status": "reconciled", "path": str(path), "errors": []}


def product_dirs(root: Path, manifest: dict[str, Any] | None = None) -> list[str]:
    if manifest is None:
        manifest = load_layer_manifest(root)
    expected = _surface_product_dirs(root)
    product = manifest.get("product") if isinstance(manifest, dict) else None
    dirs = (product or {}).get("dirs") if isinstance(product, dict) else None
    if not isinstance(dirs, list):
        raise ValueError("product.dirs must be a list matching ZE_SURFACE.json.web.product_dir")
    cleaned = [_clean_rel_path(item) for item in dirs]
    if any(item is None for item in cleaned) or cleaned != dirs:
        raise ValueError("product.dirs must contain canonical relative POSIX paths")
    if cleaned != expected:
        raise ValueError(
            "product.dirs must exactly match verified ZE_SURFACE.json.web.product_dir: "
            f"expected {expected!r}, got {cleaned!r}"
        )
    return expected


def validate_layer_manifest(root: Path, manifest: dict[str, Any] | None = None) -> list[str]:
    errors: list[str] = []
    path_error = _manifest_path_error(root / MANIFEST_FILE)
    if path_error:
        errors.append(path_error)
    if manifest is None:
        manifest = load_layer_manifest(root)
    if not isinstance(manifest, dict):
        return errors + [f"{MANIFEST_FILE} must contain a JSON object"]
    load_error = manifest.get("_load_error") if isinstance(manifest, dict) else None
    if isinstance(load_error, str) and load_error and load_error not in errors:
        errors.append(load_error)
    if manifest.get("schema") != SCHEMA:
        errors.append(f"schema must be {SCHEMA}")

    preserve_raw = manifest.get("preserve")
    preserve = [_clean_rel_path(item) for item in preserve_raw] if isinstance(preserve_raw, list) else []
    preserve = [item for item in preserve if item]
    for rel in REQUIRED_PRESERVE:
        if rel not in preserve:
            errors.append(f"preserve must include {rel}")

    product = manifest.get("product") if isinstance(manifest, dict) else None
    dirs = (product or {}).get("dirs") if isinstance(product, dict) else None
    pdirs: list[str] = []
    if isinstance(dirs, list):
        for item in dirs:
            cleaned = _clean_rel_path(item)
            if cleaned is None:
                errors.append(f"invalid product.dirs path: {item!r}")
            elif item != cleaned:
                errors.append(f"product.dirs path must be canonical POSIX form: {item!r}")
            else:
                pdirs.append(cleaned)
        if len(pdirs) != len(set(pdirs)):
            errors.append("product.dirs contains duplicate paths")
    else:
        errors.append("product.dirs must be a list")

    expected_pdirs: list[str] | None
    expected_merge_path: str | None
    try:
        expected_pdirs, expected_merge_path = _surface_product_boundary(root)
    except ValueError as exc:
        errors.append(str(exc))
        expected_pdirs, expected_merge_path = None, None
    if expected_pdirs is not None and pdirs != expected_pdirs:
        errors.append(
            "product.dirs must exactly match verified ZE_SURFACE.json.web.product_dir: "
            f"expected {expected_pdirs!r}, got {pdirs!r}"
        )

    managed = manifest.get("managed")
    if not isinstance(managed, dict):
        errors.append("managed must be an object")
        return errors

    def iter_paths(value: Any) -> list[str]:
        out: list[str] = []
        if isinstance(value, str):
            cleaned = _clean_rel_path(value)
            if cleaned:
                out.append(cleaned)
            else:
                errors.append(f"invalid relative path: {value!r}")
        elif isinstance(value, list):
            for item in value:
                out.extend(iter_paths(item))
        elif isinstance(value, dict):
            for item in value.values():
                out.extend(iter_paths(item))
        return out

    replace_dirs = iter_paths(managed.get("replace_dirs", {}))
    file_paths = iter_paths(managed.get("files", {}))
    merge_entries = managed.get("merge_dirs", {})
    merge_preserve: list[str] = []
    core_merge_entries: list[Any] | None = None
    if isinstance(merge_entries, dict):
        raw_core_entries = merge_entries.get("core")
        core_merge_entries = raw_core_entries if isinstance(raw_core_entries, list) else None
        if raw_core_entries is not None and not isinstance(raw_core_entries, list):
            errors.append("managed.merge_dirs.core must be a list")
        for entries in merge_entries.values():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    errors.append(f"managed.merge_dirs entry must be an object: {entry!r}")
                    continue
                path = _clean_rel_path(entry.get("path"))
                if not path:
                    errors.append(f"invalid managed.merge_dirs path: {entry.get('path')!r}")
                preserves = entry.get("preserve")
                if isinstance(preserves, list):
                    for item in preserves:
                        cleaned = _clean_rel_path(item)
                        if cleaned:
                            if item != cleaned:
                                errors.append(
                                    f"merge preserve path must be canonical POSIX form: {item!r}"
                                )
                            merge_preserve.append(cleaned)
                        else:
                            errors.append(f"invalid merge preserve path: {item!r}")
                else:
                    errors.append("managed.merge_dirs entry preserve must be a list")
    else:
        errors.append("managed.merge_dirs must be an object")

    if expected_pdirs is not None:
        if merge_preserve != expected_pdirs:
            errors.append(
                "managed.merge_dirs preserve paths must exactly match verified "
                "ZE_SURFACE.json.web.product_dir: "
                f"expected {expected_pdirs!r}, got {merge_preserve!r}"
            )
        if expected_pdirs:
            matching_entries = []
            for entry in core_merge_entries or []:
                if isinstance(entry, dict) and _clean_rel_path(entry.get("path")) == expected_merge_path:
                    matching_entries.append(entry)
            if len(matching_entries) != 1:
                errors.append(
                    "managed.merge_dirs.core must declare exactly one merge entry for "
                    f"{expected_merge_path}"
                )
            else:
                matching_preserve = matching_entries[0].get("preserve")
                if matching_preserve != expected_pdirs:
                    errors.append(
                        "the surface merge entry preserve list must exactly match "
                        "ZE_SURFACE.json.web.product_dir"
                    )
        elif core_merge_entries not in (None, []):
            errors.append("managed.merge_dirs.core must be empty for a headless surface")

    for rel in replace_dirs + file_paths:
        protected_product_dirs = pdirs + (expected_pdirs or [])
        for protected in preserve + protected_product_dirs:
            if _is_under(rel, protected) or _is_under(protected, rel):
                errors.append(f"managed path overlaps preserved/product path: {rel} vs {protected}")

    return errors


def main() -> int:
    root = Path.cwd()
    manifest = load_layer_manifest(root)
    result = {
        "ok": not validate_layer_manifest(root, manifest),
        "path": str(root / MANIFEST_FILE),
        "manifest": manifest,
        "errors": validate_layer_manifest(root, manifest),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
