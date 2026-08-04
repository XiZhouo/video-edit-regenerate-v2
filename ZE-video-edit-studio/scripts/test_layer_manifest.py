from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import layer_manifest as lm  # noqa: E402


def _write_surface(
    root: Path,
    product_dir: str = "webapp/src/product",
    web_dir: str = "webapp",
) -> None:
    (root / product_dir).mkdir(parents=True, exist_ok=True)
    (root / "ZE_SURFACE.json").write_text(json.dumps({
        "schema": "ze.surface_profile.v1",
        "primary": "web",
        "surfaces": ["web"],
        "human_control": "web_workbench",
        "entrypoints": {"web": web_dir},
        "cli": None,
        "api": None,
        "web": {"dir": web_dir, "product_dir": product_dir},
    }), encoding="utf-8")


def test_default_manifest_tracks_surface_product_dir(tmp_path):
    _write_surface(tmp_path, "webapp/src/product-packer")

    manifest = lm.default_manifest(tmp_path, toolchain_files=["scripts/verify_all.py"])

    assert manifest["product"]["dirs"] == ["webapp/src/product-packer"]
    assert manifest["managed"]["merge_dirs"]["core"][0]["preserve"] == ["webapp/src/product-packer"]
    assert lm.validate_layer_manifest(tmp_path, manifest) == []


def test_direct_copy_manifest_rejects_noncanonical_web_src_layout(tmp_path):
    _write_surface(tmp_path, "web/src/product", web_dir="web")

    with pytest.raises(ValueError, match="direct-copy V3 requires"):
        lm.default_manifest(tmp_path)


def test_write_layer_manifest_creates_valid_file(tmp_path):
    _write_surface(tmp_path)

    res = lm.write_layer_manifest(tmp_path, toolchain_files=["scripts/verify_all.py"])

    assert res["status"] == "written"
    data = json.loads((tmp_path / lm.MANIFEST_FILE).read_text(encoding="utf-8"))
    assert data["schema"] == lm.SCHEMA
    assert lm.product_dirs(tmp_path, data) == ["webapp/src/product"]
    assert res["errors"] == []


def test_manifest_rejects_path_escape(tmp_path):
    _write_surface(tmp_path)
    manifest = lm.default_manifest(tmp_path)
    manifest["product"]["dirs"] = ["../outside"]

    errors = lm.validate_layer_manifest(tmp_path, manifest)

    assert any("invalid product.dirs path" in err for err in errors)


def test_headless_manifest_has_no_product_dir(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "ze_command.py").write_text("# fixture\n", encoding="utf-8")
    (tmp_path / "ZE_SURFACE.json").write_text(json.dumps({
        "schema": "ze.surface_profile.v1",
        "primary": "cli",
        "surfaces": ["cli"],
        "human_control": "cli_confirm",
        "entrypoints": {"cli": "scripts/ze_command.py"},
        "cli": {"entrypoint": "scripts/ze_command.py", "supports": ["list"]},
        "api": None,
        "web": None,
    }), encoding="utf-8")

    manifest = lm.default_manifest(tmp_path)

    assert manifest["product"]["dirs"] == []
    assert manifest["managed"]["merge_dirs"]["core"] == []
    assert lm.validate_layer_manifest(tmp_path, manifest) == []


def test_manifest_product_dirs_must_exactly_match_verified_surface(tmp_path):
    _write_surface(tmp_path, "webapp/src/product-packer")
    manifest = lm.default_manifest(tmp_path)
    manifest["product"]["dirs"] = ["webapp/src/product"]

    errors = lm.validate_layer_manifest(tmp_path, manifest)

    assert any("product.dirs must exactly match" in error for error in errors)
    with pytest.raises(ValueError, match="product.dirs must exactly match"):
        lm.product_dirs(tmp_path, manifest)


def test_manifest_cannot_add_an_extra_product_dir(tmp_path):
    _write_surface(tmp_path)
    (tmp_path / "webapp" / "src" / "product-extra").mkdir()
    manifest = lm.default_manifest(tmp_path)
    manifest["product"]["dirs"].append("webapp/src/product-extra")
    manifest["managed"]["merge_dirs"]["core"][0]["preserve"].append(
        "webapp/src/product-extra"
    )

    errors = lm.validate_layer_manifest(tmp_path, manifest)

    assert any("product.dirs must exactly match" in error for error in errors)
    assert any("preserve paths must exactly match" in error for error in errors)


def test_manifest_merge_preserve_cannot_widen_beyond_surface(tmp_path):
    _write_surface(tmp_path)
    manifest = lm.default_manifest(tmp_path)
    manifest["managed"]["merge_dirs"]["core"][0]["preserve"].append(
        "webapp/src/lib"
    )

    errors = lm.validate_layer_manifest(tmp_path, manifest)

    assert any("preserve paths must exactly match" in error for error in errors)


def test_surface_merge_entry_itself_must_own_the_exact_preserve_list(tmp_path):
    _write_surface(tmp_path)
    manifest = lm.default_manifest(tmp_path)
    manifest["managed"]["merge_dirs"]["core"][0]["preserve"] = []
    manifest["managed"]["merge_dirs"]["ops"] = [{
        "path": "04-ops",
        "preserve": ["webapp/src/product"],
        "reason": "attempt to move authority to another layer",
    }]

    errors = lm.validate_layer_manifest(tmp_path, manifest)

    assert any("surface merge entry preserve list" in error for error in errors)


def test_headless_manifest_rejects_product_and_web_merge_boundaries(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "ze_command.py").write_text("# fixture\n", encoding="utf-8")
    (tmp_path / "ZE_SURFACE.json").write_text(json.dumps({
        "schema": "ze.surface_profile.v1",
        "primary": "cli",
        "surfaces": ["cli"],
        "human_control": "cli_confirm",
        "entrypoints": {"cli": "scripts/ze_command.py"},
        "cli": {"entrypoint": "scripts/ze_command.py", "supports": ["list"]},
        "api": None,
        "web": None,
    }), encoding="utf-8")
    manifest = lm.default_manifest(tmp_path)
    manifest["product"]["dirs"] = ["webapp/src/product"]
    manifest["managed"]["merge_dirs"]["core"] = [{
        "path": "webapp/src",
        "preserve": ["webapp/src/product"],
        "reason": "poisoned headless boundary",
    }]

    errors = lm.validate_layer_manifest(tmp_path, manifest)

    assert any("expected [], got ['webapp/src/product']" in error for error in errors)
    assert "managed.merge_dirs.core must be empty for a headless surface" in errors


def test_invalid_explicit_surface_cannot_fallback_to_existing_product_dir(tmp_path):
    (tmp_path / "webapp" / "src" / "product").mkdir(parents=True)
    (tmp_path / "ZE_SURFACE.json").write_text("{broken", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid ZE_SURFACE.json"):
        lm.default_manifest(tmp_path)


def test_invalid_surface_product_name_cannot_fallback_to_standard_directory(tmp_path):
    (tmp_path / "webapp" / "src" / "product").mkdir(parents=True)
    _write_surface(tmp_path, "webapp/src/lib")

    with pytest.raises(ValueError, match="'product' or a reserved 'product-<slug>'"):
        lm.default_manifest(tmp_path)


def test_explicit_malformed_manifest_is_not_replaced_by_inference(tmp_path):
    _write_surface(tmp_path)
    (tmp_path / lm.MANIFEST_FILE).write_text("{broken", encoding="utf-8")

    manifest = lm.load_layer_manifest(tmp_path)
    errors = lm.validate_layer_manifest(tmp_path, manifest)

    assert manifest["_inferred"] is False
    assert any("invalid JSON" in error for error in errors)


def test_explicit_manifest_rejects_duplicate_json_keys(tmp_path):
    _write_surface(tmp_path)
    (tmp_path / lm.MANIFEST_FILE).write_text(
        '{"schema":"ze.layer_manifest.v1","schema":"shadow"}',
        encoding="utf-8",
    )

    manifest = lm.load_layer_manifest(tmp_path)
    errors = lm.validate_layer_manifest(tmp_path, manifest)

    assert any("duplicate JSON key: schema" in error for error in errors)


def test_manifest_authority_file_must_not_be_a_symlink(tmp_path):
    _write_surface(tmp_path)
    source = tmp_path / "manifest-source.json"
    source.write_text("{}", encoding="utf-8")
    (tmp_path / lm.MANIFEST_FILE).symlink_to(source)

    manifest = lm.load_layer_manifest(tmp_path)
    errors = lm.validate_layer_manifest(tmp_path, manifest)

    assert any("ZE_LAYER_MANIFEST.json must not be a symlink" in error for error in errors)


@pytest.mark.parametrize("dangling", [False, True])
def test_manifest_symlink_fails_closed_across_all_entrypoints(tmp_path, dangling):
    _write_surface(tmp_path)
    valid_manifest = lm.default_manifest(
        tmp_path,
        toolchain_files=["scripts/old.py"],
    )
    link = tmp_path / lm.MANIFEST_FILE
    target = tmp_path / ("missing-manifest.json" if dangling else "outside-manifest.json")
    if not dangling:
        target.write_text(json.dumps(valid_manifest), encoding="utf-8")
    link.symlink_to(target.name)
    original_target = target.read_bytes() if target.exists() else None

    loaded = lm.load_layer_manifest(tmp_path)
    assert loaded == {
        "_inferred": False,
        "_load_error": "ZE_LAYER_MANIFEST.json must not be a symlink",
    }

    # Supplying an already-parsed manifest must not bypass authority-path
    # validation.
    errors = lm.validate_layer_manifest(tmp_path, valid_manifest)
    assert "ZE_LAYER_MANIFEST.json must not be a symlink" in errors

    reconciled = lm.reconcile_toolchain_files(
        tmp_path,
        toolchain_files=["scripts/new.py"],
    )
    assert reconciled["status"] == "invalid"
    assert reconciled["errors"] == ["ZE_LAYER_MANIFEST.json must not be a symlink"]

    ensured = lm.write_layer_manifest(
        tmp_path,
        toolchain_files=["scripts/new.py"],
        overwrite=True,
    )
    assert ensured["status"] == "invalid"
    assert ensured["errors"] == ["ZE_LAYER_MANIFEST.json must not be a symlink"]

    assert link.is_symlink()
    assert (target.read_bytes() if target.exists() else None) == original_target


def test_toolchain_inventory_reports_missing_extra_and_order(tmp_path):
    _write_surface(tmp_path)
    manifest = lm.default_manifest(tmp_path, toolchain_files=["scripts/a.py", "scripts/extra.py"])

    errors = lm.toolchain_inventory_errors(manifest, ["scripts/a.py", "scripts/b.py"])

    assert errors == [
        "managed.files.toolchain missing: scripts/b.py",
        "managed.files.toolchain has unexpected entries: scripts/extra.py",
    ]
    manifest["managed"]["files"]["toolchain"] = ["scripts/b.py", "scripts/a.py"]
    assert lm.toolchain_inventory_errors(manifest, ["scripts/a.py", "scripts/b.py"]) == [
        "managed.files.toolchain order differs from the canonical toolchain inventory"
    ]


def test_reconcile_toolchain_preserves_target_owned_manifest_fields(tmp_path):
    _write_surface(tmp_path)
    manifest = lm.default_manifest(tmp_path, toolchain_files=["scripts/old.py"])
    manifest["product"]["runtime_dirs"].append("runtime/custom")
    manifest["preserve"].append("customer-owned")
    manifest["x_downstream_policy"] = {"owner": "product-team"}
    (tmp_path / lm.MANIFEST_FILE).write_text(json.dumps(manifest), encoding="utf-8")

    result = lm.reconcile_toolchain_files(
        tmp_path,
        toolchain_files=["scripts/new.py", "scripts/test_new.py"],
    )

    assert result["status"] == "reconciled"
    updated = json.loads((tmp_path / lm.MANIFEST_FILE).read_text(encoding="utf-8"))
    assert updated["managed"]["files"]["toolchain"] == ["scripts/new.py", "scripts/test_new.py"]
    assert updated["product"]["runtime_dirs"] == [".data", "runtime/custom"]
    assert "customer-owned" in updated["preserve"]
    assert updated["x_downstream_policy"] == {"owner": "product-team"}


def test_reconcile_refuses_to_replace_malformed_existing_manifest(tmp_path):
    _write_surface(tmp_path)
    path = tmp_path / lm.MANIFEST_FILE
    path.write_text("{broken", encoding="utf-8")

    result = lm.reconcile_toolchain_files(tmp_path, toolchain_files=["scripts/new.py"])

    assert result["status"] == "invalid"
    assert result["errors"]
    assert path.read_text(encoding="utf-8") == "{broken"
