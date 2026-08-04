from __future__ import annotations

import copy
import hashlib
import json
import sys
import zipfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


sys.path.insert(0, str(Path(__file__).resolve().parent))

from artifact_variant import (  # noqa: E402
    MANIFEST_NAME,
    RUNTIME_PACKAGE_MEMBER,
    _archive_members,
    _delivery_matrix_errors,
    _runtime_package_validation,
    artifact_signature_message,
    base_build_key,
    build_source_inventory,
    build_series_overlay,
    canonical_digest,
    canonical_json_bytes,
    evaluate_artifact_variant,
    file_digest,
    main,
    variant_inputs_digest,
)
from deployment_profile import RECIPE_SCHEMA, evaluate_deployment_profile  # noqa: E402


UID = "skill_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(value) + b"\n")


def _surface(root: Path) -> None:
    (root / "server.py").write_text("# server\n", encoding="utf-8")
    _write_json(root / "openapi.json", {
        "openapi": "3.1.0",
        "info": {"title": "Example", "version": "1.0.0"},
        "paths": {},
    })
    _write_json(root / "ZE_SURFACE.json", {
        "schema": "ze.surface_profile.v1",
        "primary": "api",
        "surfaces": ["api"],
        "human_control": "external_review",
        "entrypoints": {"api": "server.py"},
        "cli": None,
        "api": {
            "server": "server.py",
            "healthcheck_path": "/health",
            "port_required": True,
            "exposure": "licensed-team",
            "authentication": "bearer",
            "contract": "openapi.json",
            "versioning": "path",
        },
        "web": None,
    })


def _recipe() -> dict:
    return {
        "schema": RECIPE_SCHEMA,
        "skill_uid": UID,
        "skill_id": "ZE-example",
        "service_kind": "api",
        "delivery_modes": ["local-full"],
        "runtime": {
            "install": "ze-dependency-adapter",
            "start": "python3 server.py",
            "healthcheck_path": "/health",
        },
        "artifact": {
            "include": ["."],
            "exclude": [
                ".data/", ".env", ".git/", ".venv/", "node_modules/",
                "__pycache__/", "*.pyc",
            ],
        },
        "data": {
            "roots": [".data"],
            "persistence": "local-filesystem",
            "local_project_access": "browser-file-picker",
        },
        "secrets": [
            {"name": "ZE_API_SIGNING_KEY", "required": True, "scope": "runtime"}
        ],
        "provider_requirements": {
            "allowed": ["local-machine", "ssh-baremetal"],
            "required_capabilities": ["secrets"],
        },
        "api": {
            "exposure": "licensed-team",
            "authentication": "bearer",
            "contract": "openapi.json",
            "versioning": "path",
        },
    }


def _tokens(accent: str) -> dict[str, str]:
    return {
        "primary": accent,
        "accent": accent,
        "surface": "#FFFFFF",
        "surface_muted": "#F4F4F4",
        "text": "#111111",
        "text_muted": "#666666",
        "border": "#CCCCCC",
        "focus": accent,
        "success": "#218739",
        "warning": "#A86500",
        "danger": "#B42318",
    }


def _catalog(scope: dict[str, str], *, display_name: str, accent: str) -> dict:
    item = {
        "skill_uid": UID,
        "skill_id": "ZE-example",
        "display_name": display_name,
        "visibility": "listed" if scope["series_id"] == "ze" else "hidden",
        "enabled": True,
        "surfaces": ["launcher", "portal"],
        "audiences": ["member"],
        "theme": {
            "theme_ref": f"{scope['series_id']}-default@1.0.0",
            "tokens": _tokens(accent),
        },
        "update_policy": {
            "channel": "stable" if scope["series_id"] == "ze" else "beta",
            "user_choice": "prompt" if scope["series_id"] == "ze" else "manual-only",
        },
        "authorization": {
            "required": True,
            "evaluation": "runtime-check-required",
        },
    }
    source_fingerprint = canonical_digest({"scope": scope, "item": item})
    etag = canonical_digest({
        "scope": scope,
        "source_fingerprint": source_fingerprint,
        "items": [item],
    })
    scope_slug = "-".join(scope.values())
    return {
        "schema": "ze.catalog-snapshot.v1",
        "snapshot_id": f"catalog-{scope_slug}-{etag.removeprefix('sha256:')[:12]}",
        "projection_kind": "control-plane",
        "generated_at": "2026-07-19T12:00:00+08:00",
        "scope": scope,
        "etag": etag,
        "source_fingerprint": source_fingerprint,
        "items": [item],
    }


def _base_zip(
    path: Path,
    payload: str = "export const run = () => 'ok';\n",
    *,
    dependency_manifest: bytes | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    info = zipfile.ZipInfo("app/main.js", date_time=(2020, 1, 1, 0, 0, 0))
    info.external_attr = 0o100644 << 16
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(info, payload)
        if dependency_manifest is not None:
            dependency_info = zipfile.ZipInfo(
                "ZE_DEPENDENCIES.json", date_time=(2020, 1, 1, 0, 0, 0)
            )
            dependency_info.external_attr = 0o100644 << 16
            archive.writestr(dependency_info, dependency_manifest)


def test_archive_rejects_bundled_dependency_environment(tmp_path: Path) -> None:
    path = tmp_path / "base.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("app/main.js", "export default {};\n")
        archive.writestr("app/node_modules/example/index.js", "module.exports = {};\n")

    members, errors = _archive_members(path, "zip")

    assert [name for name, _ in members] == ["app/main.js"]
    assert any("regenerable dependency/cache path" in error for error in errors)


def test_cloudflare_runtime_package_closes_over_entrypoint_bindings_and_health() -> None:
    runtime_package = {
        "schema": "ze.runtime-package.v1",
        "skill_uid": UID,
        "artifact_id": "base-ze-example-worker",
        "runtime_variant": "worker-js",
        "delivery_mode": "self-hosted-full",
        "artifact_role": "server",
        "runtime_family": "cloudflare-worker",
        "module_format": "es-module",
        "payload": {
            "root": "app",
            "entrypoint": "main.js",
            "assets_directory": "public",
        },
        "bindings": [
            {"kind": "d1-database", "name": "DB"},
            {"kind": "static-assets", "name": "ASSETS"},
        ],
        "required_environment_names": ["ZE_API_TOKEN"],
        "healthcheck": {"path": "/health", "expected_statuses": [200]},
        "source_visibility": "backend-not-browser-delivered",
    }
    raw = canonical_json_bytes(runtime_package)
    manifest_digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    evidence, errors = _runtime_package_validation(
        members=[
            (RUNTIME_PACKAGE_MEMBER, raw),
            ("app/main.js", b"export default { fetch() {} };\n"),
            ("app/public/index.html", b"<!doctype html>\n"),
        ],
        base={
            "artifact_id": "base-ze-example-worker",
            "runtime_package": {
                "schema": "ze.runtime-package.v1",
                "manifest_path": RUNTIME_PACKAGE_MEMBER,
                "manifest_digest": manifest_digest,
            },
        },
        inputs={
            "runtime_variant": "worker-js",
            "delivery_mode": "self-hosted-full",
            "artifact_role": "server",
        },
        recipe={
            "runtime": {"healthcheck_path": "/health"},
            "secrets": [
                {"name": "ZE_API_TOKEN", "required": True, "scope": "runtime"}
            ],
            "provider_requirements": {
                "required_capabilities": ["secrets", "sql", "static_assets"]
            },
        },
        skill_uid=UID,
        label="variants[0]",
    )

    assert errors == []
    assert evidence is not None
    assert evidence["manifest_digest"] == manifest_digest
    assert evidence["entrypoint"] == "main.js"
    assert evidence["binding_contract_digest"].startswith("sha256:")


def test_cloudflare_runtime_package_rejects_digest_mismatch_and_outside_payload() -> None:
    raw = canonical_json_bytes({
        "schema": "ze.runtime-package.v1",
        "skill_uid": UID,
        "artifact_id": "base-ze-example-worker",
        "runtime_variant": "worker-js",
        "delivery_mode": "self-hosted-full",
        "artifact_role": "server",
        "runtime_family": "cloudflare-worker",
        "module_format": "es-module",
        "payload": {"root": "app", "entrypoint": "main.js", "assets_directory": None},
        "bindings": [],
        "required_environment_names": [],
        "healthcheck": {"path": "/health", "expected_statuses": [200]},
        "source_visibility": "backend-not-browser-delivered",
    })
    _, errors = _runtime_package_validation(
        members=[
            (RUNTIME_PACKAGE_MEMBER, raw),
            ("app/main.js", b"export default {};\n"),
            ("unexpected.txt", b"not payload\n"),
        ],
        base={
            "artifact_id": "base-ze-example-worker",
            "runtime_package": {
                "schema": "ze.runtime-package.v1",
                "manifest_path": RUNTIME_PACKAGE_MEMBER,
                "manifest_digest": "sha256:" + "0" * 64,
            },
        },
        inputs={
            "runtime_variant": "worker-js",
            "delivery_mode": "self-hosted-full",
            "artifact_role": "server",
        },
        recipe={
            "runtime": {"healthcheck_path": "/health"},
            "secrets": [],
            "provider_requirements": {"required_capabilities": []},
        },
        skill_uid=UID,
        label="variants[0]",
    )

    assert any("manifest_digest mismatch" in error for error in errors)
    assert any("files outside app" in error for error in errors)


def test_archive_member_reader_rejects_duplicate_paths(tmp_path: Path) -> None:
    archive_path = tmp_path / "duplicate.zip"
    with pytest.warns(UserWarning, match="Duplicate name"):
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("app/main.js", "one")
            archive.writestr("app/main.js", "two")
    _, errors = _archive_members(archive_path, "zip")
    assert any("duplicate archive path" in error for error in errors)


def test_ready_evidence_requires_runtime_package_for_cloudflare_variant(
    tmp_path: Path,
) -> None:
    recipe, manifest, _ = _case(tmp_path)
    variant = manifest["variants"][0]
    variant["variant_inputs"]["runtime_variant"] = "worker-js"
    variant["variant_inputs_digest"] = variant_inputs_digest(
        variant["variant_inputs"]
    )
    variant["build_key"] = base_build_key(
        UID, manifest["source_digest"], variant["variant_inputs_digest"]
    )
    _write_json(tmp_path / MANIFEST_NAME, manifest)

    result = evaluate_artifact_variant(tmp_path, recipe)

    assert result["valid"] is False
    assert any(
        "runtime_package is required for Cloudflare runtime variant worker-js"
        in error
        for error in result["errors"]
    )


def _signature_record(
    root: Path,
    private_key: Ed25519PrivateKey,
    artifact_path: Path,
    *,
    signature_name: str,
) -> dict:
    artifact_digest = file_digest(artifact_path)
    signature_path = root / "artifacts" / f"{signature_name}.sig"
    signature_path.write_bytes(
        private_key.sign(artifact_signature_message(artifact_digest))
    )
    public_path = root / "build-inputs" / "signing-public.pem"
    return {
        "algorithm": "ed25519",
        "key_id": "test-release-key",
        "signed_digest": artifact_digest,
        "public_key": {
            "path": str(public_path.relative_to(root)),
            "digest": file_digest(public_path),
        },
        "detached_signature": {
            "path": str(signature_path.relative_to(root)),
            "digest": file_digest(signature_path),
        },
    }


def _refresh_signature(
    root: Path,
    private_key: Ed25519PrivateKey,
    artifact: dict,
) -> None:
    artifact_path = root / artifact["path"]
    signature = artifact["signature"]
    signature_path = root / signature["detached_signature"]["path"]
    artifact_digest = file_digest(artifact_path)
    signature_path.write_bytes(
        private_key.sign(artifact_signature_message(artifact_digest))
    )
    signature["signed_digest"] = artifact_digest
    signature["detached_signature"]["digest"] = file_digest(signature_path)


def _case(
    tmp_path: Path,
    *,
    base_payload: str | None = None,
) -> tuple[dict, dict, Ed25519PrivateKey]:
    _surface(tmp_path)
    recipe = _recipe()
    _write_json(tmp_path / "deploy.json", recipe)

    private_key = Ed25519PrivateKey.generate()
    public_path = tmp_path / "build-inputs" / "signing-public.pem"
    public_path.parent.mkdir(parents=True, exist_ok=True)
    public_path.write_bytes(private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ))

    _write_json(tmp_path / "ZE_DEPENDENCIES.json", {
        "schema": "ze.dependency-manifest.v1",
        "skill_id": "ZE-example",
        "skill_uid": UID,
        "status": "ready",
        "generated_by": "ZE-skill-build",
        "environments": [],
    })

    base_path = tmp_path / "artifacts" / "base.zip"
    _base_zip(
        base_path,
        base_payload or "export const run = () => 'ok';\n",
        dependency_manifest=(tmp_path / "ZE_DEPENDENCIES.json").read_bytes(),
    )
    sbom_path = tmp_path / "artifacts" / "base.cdx.json"
    _write_json(sbom_path, {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "version": 1,
        "components": [],
    })

    profile = evaluate_deployment_profile(tmp_path, recipe)["deployment_profile"]
    lock_path = tmp_path / "requirements.lock"
    lock_path.write_text("example==1.0.0 --hash=sha256:abc\n", encoding="utf-8")
    code_asset_path = tmp_path / "build-inputs" / "model.bin"
    code_asset_path.parent.mkdir(parents=True, exist_ok=True)
    code_asset_path.write_bytes(b"code-affecting-model-input\n")
    inputs = {
        "runtime_variant": "python-api",
        "delivery_mode": "local-full",
        "artifact_role": "runtime",
        "surface_profile_digest": file_digest(tmp_path / "ZE_SURFACE.json"),
        "compatibility_profile_digest": canonical_digest(profile),
        "dependency_locks": [{
            "path": str(lock_path.relative_to(tmp_path)),
            "digest": file_digest(lock_path),
        }],
        "feature_set": ["api", "local-project-picker"],
        "code_assets": [{
            "path": str(code_asset_path.relative_to(tmp_path)),
            "digest": file_digest(code_asset_path),
        }],
    }
    input_digest = variant_inputs_digest(inputs)
    base_digest = file_digest(base_path)
    overlays: list[dict] = []
    fixtures = [
        (
            {"series_id": "ze", "organization_id": "example-company", "target_id": "company-prod"},
            "对白编辑器",
            "#267A72",
        ),
        (
            {"series_id": "rs", "organization_id": "indie-team", "target_id": "rs-staging"},
            "Rune Dialogue",
            "#9A6518",
        ),
    ]
    for scope, display_name, accent in fixtures:
        key = scope["series_id"]
        snapshot_path = tmp_path / "catalog" / f"{key}.json"
        snapshot = _catalog(scope, display_name=display_name, accent=accent)
        _write_json(snapshot_path, snapshot)
        snapshot_digest = file_digest(snapshot_path)
        overlay = build_series_overlay(
            snapshot,
            skill_uid=UID,
            base_artifact_digest=base_digest,
            catalog_snapshot_digest=snapshot_digest,
        )
        overlay_path = tmp_path / "artifacts" / f"overlay-{key}.json"
        _write_json(overlay_path, overlay)
        overlay_signature = _signature_record(
            tmp_path,
            private_key,
            overlay_path,
            signature_name=f"overlay-{key}",
        )
        overlays.append({
            "scope": scope,
            "base_artifact_id": "base-ze-example-python-api",
            "base_artifact_digest": base_digest,
            "artifact": {
                "path": str(overlay_path.relative_to(tmp_path)),
                "digest": file_digest(overlay_path),
                "size_bytes": overlay_path.stat().st_size,
                "signature": overlay_signature,
            },
            "catalog_snapshot": {
                "path": str(snapshot_path.relative_to(tmp_path)),
                "digest": snapshot_digest,
                "snapshot_id": snapshot["snapshot_id"],
                "etag": snapshot["etag"],
            },
        })
    overlays.sort(key=lambda entry: (
        entry["scope"]["series_id"],
        entry["scope"]["organization_id"],
        entry["scope"]["target_id"],
        entry["base_artifact_id"],
    ))

    inventory_path = tmp_path / "build-inputs" / "source-inventory.json"
    inventory = build_source_inventory(
        tmp_path,
        roots=["."],
        excludes=[
            "ZE_ARTIFACT_VARIANT.json",
            "ZE_BUILD_EVIDENCE.json",
            "artifacts",
            "build-inputs/source-inventory.json",
            "catalog",
        ],
        inventory_path="build-inputs/source-inventory.json",
    )
    _write_json(inventory_path, inventory)
    source_digest = canonical_digest(inventory)
    manifest = {
        "schema": "ze.artifact-variant-manifest.v1",
        "skill_uid": UID,
        "current_skill_id": "ZE-example",
        "source_digest": source_digest,
        "source_inventory": {
            "path": str(inventory_path.relative_to(tmp_path)),
            "digest": file_digest(inventory_path),
        },
        "variants": [{
            "variant_id": "python-api-runtime",
            "variant_inputs": inputs,
            "variant_inputs_digest": input_digest,
            "build_key": base_build_key(UID, source_digest, input_digest),
            "base_artifact": {
                "artifact_id": "base-ze-example-python-api",
                "artifact_role": "runtime",
                "path": str(base_path.relative_to(tmp_path)),
                "format": "zip",
                "digest": base_digest,
                "size_bytes": base_path.stat().st_size,
                "delivery_mode": "local-full",
                "sbom": {
                    "path": str(sbom_path.relative_to(tmp_path)),
                    "digest": file_digest(sbom_path),
                    "format": "cyclonedx-json",
                },
                "signature": _signature_record(
                    tmp_path,
                    private_key,
                    base_path,
                    signature_name="base",
                ),
                "compatibility_profile_digest": canonical_digest(profile),
            },
        }],
        "series_overlays": overlays,
        "update_candidate": {
            "version": "1.2.0",
            "channel_eligibility": ["stable", "beta"],
            "minimum_launcher_version": "1.0.0",
            "migration": "none",
            "rollback": "supported",
        },
        "isolation": {
            "base_scope": "global",
            "overlay_scope": "series-organization-target",
            "scan_policy": "structured-scope-markers-v1",
            "cross_series_asset_refs": [],
        },
    }
    _write_json(tmp_path / MANIFEST_NAME, manifest)
    return recipe, manifest, private_key


def _replace_overlay(
    tmp_path: Path,
    manifest: dict,
    private_key: Ed25519PrivateKey,
    index: int,
    *,
    display_name: str,
) -> None:
    entry = manifest["series_overlays"][index]
    snapshot_path = tmp_path / entry["catalog_snapshot"]["path"]
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot["items"][0]["display_name"] = display_name
    snapshot["source_fingerprint"] = canonical_digest({
        "scope": snapshot["scope"], "item": snapshot["items"][0],
    })
    snapshot["etag"] = canonical_digest({
        "scope": snapshot["scope"],
        "source_fingerprint": snapshot["source_fingerprint"],
        "items": snapshot["items"],
    })
    snapshot["snapshot_id"] = (
        "catalog-" + "-".join(snapshot["scope"].values()) + "-"
        + snapshot["etag"].removeprefix("sha256:")[:12]
    )
    _write_json(snapshot_path, snapshot)
    snapshot_digest = file_digest(snapshot_path)
    overlay = build_series_overlay(
        snapshot,
        skill_uid=UID,
        base_artifact_digest=manifest["variants"][0]["base_artifact"]["digest"],
        catalog_snapshot_digest=snapshot_digest,
    )
    overlay_path = tmp_path / entry["artifact"]["path"]
    _write_json(overlay_path, overlay)
    entry["catalog_snapshot"].update({
        "digest": snapshot_digest,
        "snapshot_id": snapshot["snapshot_id"],
        "etag": snapshot["etag"],
    })
    entry["artifact"].update({
        "digest": file_digest(overlay_path),
        "size_bytes": overlay_path.stat().st_size,
    })
    _refresh_signature(tmp_path, private_key, entry["artifact"])
    _write_json(tmp_path / MANIFEST_NAME, manifest)


def test_dual_series_manifest_reuses_base_and_keeps_overlays_scoped(tmp_path: Path) -> None:
    _case(tmp_path)
    result = evaluate_artifact_variant(tmp_path)
    assert result["valid"] is True, result["errors"]
    evidence = result["artifact_variant"]
    assert evidence["status"] == "ready"
    assert evidence["isolation_evidence"]["scope_scan"] == "passed"
    assert evidence["isolation_evidence"]["overlay_scope_count"] == 2
    assert evidence["isolation_evidence"]["signature_count"] == 3
    assert evidence["isolation_evidence"]["signing_key_count"] == 1
    assert evidence["variants"][0]["runtime_variant"] == "python-api"
    assert evidence["variants"][0]["signature"]["status"] == "verified"
    assert {row["scope"]["series_id"] for row in evidence["series_overlays"]} == {"ze", "rs"}
    assert all(row["authorization"] == "runtime-check-required" for row in evidence["series_overlays"])
    assert all(row["signature"]["status"] == "verified" for row in evidence["series_overlays"])


def test_rs_catalog_change_does_not_change_base_build_key_or_ze_overlay(tmp_path: Path) -> None:
    _, manifest, private_key = _case(tmp_path)
    before = copy.deepcopy(manifest)
    rs_index = next(
        index
        for index, entry in enumerate(manifest["series_overlays"])
        if entry["scope"]["series_id"] == "rs"
    )
    _replace_overlay(
        tmp_path,
        manifest,
        private_key,
        rs_index,
        display_name="Rune Dialogue Revised",
    )
    result = evaluate_artifact_variant(tmp_path)
    assert result["valid"] is True, result["errors"]
    assert manifest["variants"][0]["build_key"] == before["variants"][0]["build_key"]
    assert manifest["variants"][0]["base_artifact"]["digest"] == before["variants"][0]["base_artifact"]["digest"]
    before_by_series = {
        entry["scope"]["series_id"]: entry["artifact"]["digest"]
        for entry in before["series_overlays"]
    }
    after_by_series = {
        entry["scope"]["series_id"]: entry["artifact"]["digest"]
        for entry in manifest["series_overlays"]
    }
    assert after_by_series["ze"] == before_by_series["ze"]
    assert after_by_series["rs"] != before_by_series["rs"]


def test_stale_variant_inputs_digest_and_build_key_are_rejected(tmp_path: Path) -> None:
    _, manifest, _ = _case(tmp_path)
    manifest["variants"][0]["variant_inputs"]["feature_set"].append("new-feature")
    _write_json(tmp_path / MANIFEST_NAME, manifest)
    result = evaluate_artifact_variant(tmp_path)
    assert result["valid"] is False
    assert any("variant_inputs_digest mismatch" in error for error in result["errors"])
    assert any("build_key mismatch" in error for error in result["errors"])


def test_base_artifact_scope_binding_is_detected_even_when_digest_is_current(tmp_path: Path) -> None:
    _, _, _ = _case(
        tmp_path,
        base_payload='export const config = {"organization_id":"indie-team","target_id":"rs-staging"};\n',
    )
    result = evaluate_artifact_variant(tmp_path)
    assert result["valid"] is False
    assert any("scoped presentation" in error for error in result["errors"])
    assert {item["marker"] for item in result["artifact_variant"]["isolation_evidence"]["findings"]} >= {
        "organization_id", "target_id",
    }


def test_overlay_must_be_exact_projection_of_catalog_snapshot(tmp_path: Path) -> None:
    _, manifest, private_key = _case(tmp_path)
    entry = manifest["series_overlays"][0]
    overlay_path = tmp_path / entry["artifact"]["path"]
    overlay = json.loads(overlay_path.read_text(encoding="utf-8"))
    overlay["catalog"]["display_name"] = "Hand-edited"
    _write_json(overlay_path, overlay)
    entry["artifact"]["digest"] = file_digest(overlay_path)
    entry["artifact"]["size_bytes"] = overlay_path.stat().st_size
    _refresh_signature(tmp_path, private_key, entry["artifact"])
    _write_json(tmp_path / MANIFEST_NAME, manifest)
    result = evaluate_artifact_variant(tmp_path)
    assert result["valid"] is False
    assert any("exact Catalog-derived overlay" in error for error in result["errors"])


def test_v3_requires_manifest_while_legacy_remains_readable(tmp_path: Path) -> None:
    _surface(tmp_path)
    recipe = _recipe()
    _write_json(tmp_path / "deploy.json", recipe)
    current = evaluate_artifact_variant(tmp_path)
    assert current["valid"] is False
    assert current["artifact_variant"]["status"] == "required"

    legacy = evaluate_artifact_variant(
        tmp_path,
        {"service_kind": "web", "start": "bash start.sh", "port": 8000},
    )
    assert legacy["valid"] is True
    assert legacy["artifact_variant"]["status"] == "legacy-unscoped"


def test_project_overlay_cli_writes_catalog_derived_bytes(tmp_path: Path) -> None:
    base_digest = "sha256:" + "e" * 64
    scope = {
        "series_id": "rs", "organization_id": "indie-team", "target_id": "rs-staging",
    }
    catalog_path = tmp_path / "catalog.json"
    _write_json(catalog_path, _catalog(scope, display_name="Rune Dialogue", accent="#9A6518"))
    output = tmp_path / "overlay.json"
    assert main([
        "project-overlay", "--catalog", str(catalog_path), "--skill-uid", UID,
        "--base-digest", base_digest, "--out", str(output),
    ]) == 0
    value = json.loads(output.read_text(encoding="utf-8"))
    assert value["scope"] == scope
    assert value["catalog"]["theme"]["tokens"]["accent"] == "#9A6518"
    assert value["catalog"]["authorization"]["evaluation"] == "runtime-check-required"


def test_sign_digest_cli_writes_verifiable_detached_signature(tmp_path: Path) -> None:
    private_key = Ed25519PrivateKey.generate()
    private_path = tmp_path / "private.pem"
    private_path.write_bytes(private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    digest = "sha256:" + "a" * 64
    output = tmp_path / "artifact.sig"
    assert main([
        "sign-digest",
        "--private-key", str(private_path),
        "--digest", digest,
        "--out", str(output),
    ]) == 0
    private_key.public_key().verify(
        output.read_bytes(),
        artifact_signature_message(digest),
    )


def test_tampered_detached_signature_is_rejected(tmp_path: Path) -> None:
    _, manifest, _ = _case(tmp_path)
    signature = manifest["variants"][0]["base_artifact"]["signature"]
    signature_path = tmp_path / signature["detached_signature"]["path"]
    signature_path.write_bytes(b"\x00" * 64)
    signature["detached_signature"]["digest"] = file_digest(signature_path)
    _write_json(tmp_path / MANIFEST_NAME, manifest)
    result = evaluate_artifact_variant(tmp_path)
    assert result["valid"] is False
    assert any("verification failed" in error for error in result["errors"])


def test_delivery_matrix_requires_both_api_and_hybrid_halves() -> None:
    api_variants = [
        {"variant_inputs": {"delivery_mode": "creator-hosted-api", "artifact_role": "server"}},
        {"variant_inputs": {"delivery_mode": "creator-hosted-api", "artifact_role": "client"}},
    ]
    assert _delivery_matrix_errors(
        {"delivery_modes": ["creator-hosted-api"]}, api_variants
    ) == []
    missing_api_client = _delivery_matrix_errors(
        {"delivery_modes": ["creator-hosted-api"]}, api_variants[:1]
    )
    assert any("misses artifact role(s): client" in error for error in missing_api_client)

    hybrid_variants = [
        {"variant_inputs": {"delivery_mode": "hybrid", "artifact_role": "server"}},
        {"variant_inputs": {"delivery_mode": "hybrid", "artifact_role": "local-adapter"}},
    ]
    assert _delivery_matrix_errors({"delivery_modes": ["hybrid"]}, hybrid_variants) == []


def test_variant_input_symlink_is_rejected(tmp_path: Path) -> None:
    _, manifest, _ = _case(tmp_path)
    asset_ref = manifest["variants"][0]["variant_inputs"]["code_assets"][0]
    asset_path = tmp_path / asset_ref["path"]
    asset_path.unlink()
    asset_path.symlink_to(tmp_path / "requirements.lock")
    result = evaluate_artifact_variant(tmp_path)
    assert result["valid"] is False
    assert any("must not traverse a symlink" in error for error in result["errors"])


def test_unsafe_archive_member_is_rejected_before_any_extraction(tmp_path: Path) -> None:
    _, manifest, private_key = _case(tmp_path)
    manifest["series_overlays"] = []
    base = manifest["variants"][0]["base_artifact"]
    base_path = tmp_path / base["path"]
    with zipfile.ZipFile(base_path, "w") as archive:
        archive.writestr("../escape.txt", "never extract this")
    base["digest"] = file_digest(base_path)
    base["size_bytes"] = base_path.stat().st_size
    _refresh_signature(tmp_path, private_key, base)
    _write_json(tmp_path / MANIFEST_NAME, manifest)
    result = evaluate_artifact_variant(tmp_path)
    assert result["valid"] is False
    assert any("unsafe archive path" in error for error in result["errors"])


@pytest.mark.parametrize(
    ("archived_manifest", "expected_error"),
    [
        (None, "must contain exactly one root-level ZE_DEPENDENCIES.json"),
        (b'{"schema":"stale"}\n', "does not exactly match the verified root declaration"),
    ],
)
def test_executable_artifact_closes_over_dependency_manifest(
    tmp_path: Path,
    archived_manifest: bytes | None,
    expected_error: str,
) -> None:
    _, manifest, private_key = _case(tmp_path)
    manifest["series_overlays"] = []
    base = manifest["variants"][0]["base_artifact"]
    base_path = tmp_path / base["path"]
    _base_zip(base_path, dependency_manifest=archived_manifest)
    base["digest"] = file_digest(base_path)
    base["size_bytes"] = base_path.stat().st_size
    _refresh_signature(tmp_path, private_key, base)
    _write_json(tmp_path / MANIFEST_NAME, manifest)

    result = evaluate_artifact_variant(tmp_path)

    assert result["valid"] is False
    assert any(expected_error in error for error in result["errors"])


def test_duplicate_manifest_key_is_rejected(tmp_path: Path) -> None:
    _surface(tmp_path)
    _write_json(tmp_path / "deploy.json", _recipe())
    (tmp_path / MANIFEST_NAME).write_text(
        '{"schema":"ze.artifact-variant-manifest.v1","schema":"duplicate"}',
        encoding="utf-8",
    )
    result = evaluate_artifact_variant(tmp_path)
    assert result["valid"] is False
    assert any("duplicate JSON key" in error for error in result["errors"])


def test_scope_scan_detects_unknown_team_binding_without_any_overlay(tmp_path: Path) -> None:
    _, manifest, _ = _case(
        tmp_path,
        base_payload='export const config = {"organization_id":"third-team"};\n',
    )
    manifest["series_overlays"] = []
    _write_json(tmp_path / MANIFEST_NAME, manifest)
    result = evaluate_artifact_variant(tmp_path)
    assert result["valid"] is False
    assert any(
        item["marker"] == "organization_id"
        for item in result["artifact_variant"]["isolation_evidence"]["findings"]
    )


def test_source_inventory_detects_source_mutation_after_build(tmp_path: Path) -> None:
    _case(tmp_path)
    (tmp_path / "server.py").write_text("# changed after source inventory\n", encoding="utf-8")
    result = evaluate_artifact_variant(tmp_path)
    assert result["valid"] is False
    assert any("source inventory does not match" in error for error in result["errors"])


def test_source_inventory_cannot_exclude_arbitrary_business_source(tmp_path: Path) -> None:
    (tmp_path / "02-mod").mkdir()
    (tmp_path / "02-mod" / "business.py").write_text("VALUE = 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="outside generated/runtime policy"):
        build_source_inventory(
            tmp_path,
            roots=["."],
            excludes=[
                "02-mod",
                "ZE_ARTIFACT_VARIANT.json",
                "ZE_BUILD_EVIDENCE.json",
                "build-inputs/source-inventory.json",
            ],
            inventory_path="build-inputs/source-inventory.json",
        )


def test_source_inventory_cli_generates_revalidatable_inventory(tmp_path: Path) -> None:
    (tmp_path / "source.py").write_text("VALUE = 1\n", encoding="utf-8")
    assert main([
        "source-inventory", "--root", str(tmp_path),
        "--out", "build-inputs/source-inventory.json",
    ]) == 0
    inventory = json.loads(
        (tmp_path / "build-inputs" / "source-inventory.json").read_text(encoding="utf-8")
    )
    assert inventory["schema"] == "ze.source-inventory.v1"
    assert inventory["files"] == [{
        "path": "source.py",
        "digest": file_digest(tmp_path / "source.py"),
        "size_bytes": (tmp_path / "source.py").stat().st_size,
    }]
