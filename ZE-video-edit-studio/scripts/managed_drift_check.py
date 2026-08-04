#!/usr/bin/env python3
"""Check ZE managed-layer drift without touching product-owned paths."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path, PurePosixPath
from typing import Any

import layer_manifest
import skill_sync

def _is_under(rel: str, parent: str) -> bool:
    r = PurePosixPath(rel)
    p = PurePosixPath(parent)
    return r == p or p in r.parents


def _is_product_path(root: Path, rel: str) -> bool:
    return any(_is_under(rel, p) for p in layer_manifest.product_dirs(root))


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _managed_files(root: Path, layer: str) -> dict[str, str]:
    files: dict[str, str] = {}
    copy_ignore = skill_sync._copy_ignore_for_root(root)
    for rel_dir in skill_sync.LAYER_DIRS.get(layer, []):
        base = root / rel_dir
        if not base.is_dir():
            continue
        for path in skill_sync._copytree_regular_files(base, copy_ignore):
            rel = path.relative_to(root).as_posix()
            if _is_product_path(root, rel):
                continue
            files[rel] = _hash_file(path)
    for rel in skill_sync.LAYER_FILES.get(layer, []):
        path = root / rel
        if not path.is_file():
            continue
        if _is_product_path(root, rel):
            continue
        files[rel] = _hash_file(path)
    return files


def _managed_target_error(exc: Exception) -> dict[str, Any]:
    return {
        "status": "invalid",
        "kind": type(exc).__name__,
        "errors": list(getattr(exc, "errors", [str(exc)])),
    }


def _core_inventory(root: Path, *, target_root: Path) -> dict[str, str]:
    inventory: dict[str, str] = {}
    for row in skill_sync._core_managed_file_fingerprint(root, target_root=target_root):
        body, digest = row.rsplit(":", 1)
        prefix, rel = body.split(":", 1)
        if prefix != "CORE":  # pragma: no cover - internal contract corruption
            raise ValueError(f"unexpected core inventory row: {row!r}")
        inventory[rel] = digest
    return inventory


def compare_to_upstream(target: Path, upstream: Path, layers: list[str]) -> dict[str, Any]:
    try:
        shape = skill_sync.audit_managed_target(target, layers)
    except (skill_sync.SurfaceProfileError, skill_sync.LayerManifestError,
            skill_sync.ManagedTargetError) as exc:
        return {
            "ok": False,
            "upstream": str(upstream),
            "target": str(target),
            "managed_target": _managed_target_error(exc),
            "per_layer": {},
        }
    per_layer: dict[str, Any] = {}
    ok = True
    for layer in layers:
        if layer == "core":
            src = _core_inventory(upstream, target_root=target)
            dst = _core_inventory(target, target_root=target)
        else:
            src = _managed_files(upstream, layer)
            dst = _managed_files(target, layer)
        changed = sorted(rel for rel in src.keys() & dst.keys() if src[rel] != dst[rel])
        missing = sorted(src.keys() - dst.keys())
        extra = sorted(dst.keys() - src.keys())
        layer_ok = not (changed or missing or extra)
        ok = ok and layer_ok
        per_layer[layer] = {
            "status": "same" if layer_ok else "drift",
            "changed": changed[:50],
            "missing": missing[:50],
            "extra": extra[:50],
            "counts": {"changed": len(changed), "missing": len(missing), "extra": len(extra)},
        }
    return {
        "ok": ok,
        "upstream": str(upstream),
        "target": str(target),
        "managed_target": shape,
        "per_layer": per_layer,
    }


def _read_lock(root: Path, layer: str) -> dict[str, Any]:
    lock = skill_sync._read_lock(root, layer)
    return lock if isinstance(lock, dict) else {}


def lock_consistency(
        root: Path,
        layers: list[str],
        *,
        source_root: Path | None = None,
) -> dict[str, Any]:
    """Validate locks with this trusted auditor, never target-owned verifier code."""
    try:
        shape = skill_sync.audit_managed_target(root, layers)
    except (skill_sync.SurfaceProfileError, skill_sync.LayerManifestError,
            skill_sync.ManagedTargetError) as exc:
        return {
            "ok": False,
            "drift": [],
            "managed_target": _managed_target_error(exc),
            "hint": "Repair the managed target shape before trusting its locks.",
        }

    drift = []
    for layer in layers:
        lock = _read_lock(root, layer)
        lock_error = lock.get("_error") or ("lock missing" if not lock else None)
        if lock_error:
            drift.append({
                "layer": layer,
                "source": {"status": "not evaluated because lock authority is invalid"},
                "lock": {
                    "interface_version": lock.get("interface_version"),
                    "checksum": (lock.get("checksum") or "")[:16] + "...",
                    "error": lock_error,
                },
            })
            continue
        if source_root is None:
            version, checksum = skill_sync.source_contract(layer, root)
        else:
            version, checksum = skill_sync.source_contract(
                layer,
                source_root,
                target_root=root,
            )
        if lock.get("interface_version") != version or lock.get("checksum") != checksum:
            drift.append({
                "layer": layer,
                "source": {"interface_version": version, "checksum": checksum[:16] + "..."},
                "lock": {
                    "interface_version": lock.get("interface_version"),
                    "checksum": (lock.get("checksum") or "")[:16] + "...",
                    **({"error": lock_error} if lock_error else {}),
                },
            })
    return {
        "ok": not drift,
        "drift": drift,
        "managed_target": shape,
        "source_root": str(source_root or root),
        "hint": "" if not drift else "Run scripts/skill_sync.py --init-locks after intentional build-v3 changes.",
    }


def check_manifest(root: Path, *, write: bool, refresh: bool) -> dict[str, Any]:
    expected = skill_sync.LAYER_FILES["toolchain"]
    if refresh:
        return layer_manifest.reconcile_toolchain_files(
            root,
            toolchain_files=expected,
        )
    if write:
        return layer_manifest.write_layer_manifest(
            root,
            toolchain_files=expected,
            overwrite=False,
        )
    path = root / layer_manifest.MANIFEST_FILE
    if not path.exists() and not path.is_symlink():
        return {
            "status": "missing",
            "path": str(path),
            "errors": ["ZE_LAYER_MANIFEST.json missing; run scripts/managed_drift_check.py --write-manifest"],
        }
    manifest = layer_manifest.load_layer_manifest(root)
    errors = (
        layer_manifest.validate_layer_manifest(root, manifest)
        + layer_manifest.toolchain_inventory_errors(manifest, expected)
    )
    return {
        "status": "inferred" if manifest.get("_inferred") else "existing",
        "path": str(root / layer_manifest.MANIFEST_FILE),
        "errors": errors,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="ZE managed-layer drift check")
    ap.add_argument("--target", default=".", help="Target ZE skill root (default: cwd)")
    ap.add_argument("--upstream", default=None, help="Optional upstream build-v3 root for file-by-file compare")
    ap.add_argument("--layers", default=",".join(skill_sync.LAYERS), help="Comma-separated managed layers")
    ap.add_argument("--write-manifest", action="store_true", help="Create ZE_LAYER_MANIFEST.json if missing")
    ap.add_argument(
        "--refresh-manifest",
        action="store_true",
        help="Reconcile managed.files.toolchain while preserving target-owned manifest fields",
    )
    ap.add_argument("--manifest-only", action="store_true", help="Only validate/write ZE_LAYER_MANIFEST.json")
    args = ap.parse_args()

    target = Path(args.target).expanduser().resolve()
    layers = [item.strip() for item in args.layers.split(",") if item.strip() in skill_sync.LAYER_DIRS]
    try:
        managed_target = skill_sync.audit_managed_target(target, layers)
    except (skill_sync.SurfaceProfileError, skill_sync.LayerManifestError,
            skill_sync.ManagedTargetError) as exc:
        # Even manifest write/refresh modes are not allowed to mutate a target
        # whose current ownership shape is unsafe.
        manifest = check_manifest(target, write=False, refresh=False)
        result = {
            "manifest": manifest,
            "managed_target": _managed_target_error(exc),
            "ok": False,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1
    manifest = check_manifest(target, write=args.write_manifest or args.refresh_manifest, refresh=args.refresh_manifest)
    result: dict[str, Any] = {"manifest": manifest, "managed_target": managed_target}
    ok = not manifest.get("errors")

    if not args.manifest_only:
        upstream = Path(args.upstream).expanduser().resolve() if args.upstream else None
        locks = lock_consistency(
            target,
            [layer for layer in layers if layer in skill_sync.LAYERS],
            source_root=upstream,
        )
        result["lock_consistency"] = locks
        ok = ok and locks["ok"]
        if args.upstream:
            assert upstream is not None
            cmp_result = compare_to_upstream(target, upstream, layers)
            result["upstream_compare"] = cmp_result
            ok = ok and cmp_result["ok"]

    result["ok"] = ok
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
