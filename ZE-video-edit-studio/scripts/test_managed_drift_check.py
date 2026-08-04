from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import managed_drift_check as md  # noqa: E402


def _write_web_surface(root: Path) -> None:
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "scripts" / "ze_command.py").write_text("# cli\n", encoding="utf-8")
    (root / "webapp" / "src").mkdir(parents=True, exist_ok=True)
    (root / "webapp" / "src" / "product").mkdir(parents=True, exist_ok=True)
    (root / "ZE_SURFACE.json").write_text(json.dumps({
        "schema": "ze.surface_profile.v1",
        "primary": "web",
        "surfaces": ["cli", "web"],
        "human_control": "web_workbench",
        "entrypoints": {"cli": "scripts/ze_command.py", "web": "webapp"},
        "cli": {"entrypoint": "scripts/ze_command.py", "supports": ["list"]},
        "api": None,
        "web": {"dir": "webapp", "product_dir": "webapp/src/product"},
    }), encoding="utf-8")


def _write_cli_surface(root: Path) -> None:
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "scripts" / "ze_command.py").write_text("# cli\n", encoding="utf-8")
    (root / "ZE_SURFACE.json").write_text(json.dumps({
        "schema": "ze.surface_profile.v1",
        "primary": "cli",
        "surfaces": ["cli"],
        "human_control": "cli_confirm",
        "entrypoints": {"cli": "scripts/ze_command.py"},
        "cli": {"entrypoint": "scripts/ze_command.py"},
        "api": None,
        "web": None,
    }), encoding="utf-8")


def test_upstream_compare_ignores_product_dir(tmp_path):
    upstream = tmp_path / "up"
    target = tmp_path / "target"
    _write_web_surface(upstream)
    _write_web_surface(target)
    (upstream / "webapp" / "src" / "App.tsx").write_text("export const app = 1\n", encoding="utf-8")
    (target / "webapp" / "src" / "App.tsx").write_text("export const app = 1\n", encoding="utf-8")
    (upstream / "webapp" / "src" / "product").mkdir(parents=True, exist_ok=True)
    (target / "webapp" / "src" / "product").mkdir(parents=True, exist_ok=True)
    (upstream / "webapp" / "src" / "product" / "demo.ts").write_text("source product\n", encoding="utf-8")
    (target / "webapp" / "src" / "product" / "demo.ts").write_text("target business\n", encoding="utf-8")

    res = md.compare_to_upstream(target, upstream, ["core"])

    assert res["ok"], res
    assert res["per_layer"]["core"]["counts"] == {"changed": 0, "missing": 0, "extra": 0}


def test_upstream_compare_reports_managed_drift(tmp_path):
    upstream = tmp_path / "up"
    target = tmp_path / "target"
    _write_web_surface(upstream)
    _write_web_surface(target)
    (upstream / "webapp" / "src" / "App.tsx").write_text("export const app = 1\n", encoding="utf-8")
    (target / "webapp" / "src" / "App.tsx").write_text("export const app = 2\n", encoding="utf-8")

    res = md.compare_to_upstream(target, upstream, ["core"])

    assert not res["ok"]
    assert res["per_layer"]["core"]["changed"] == ["webapp/src/App.tsx"]


def test_upstream_compare_ignores_mypy_runtime_cache_for_managed_tree(tmp_path):
    upstream = tmp_path / "up"
    target = tmp_path / "target"
    _write_cli_surface(upstream)
    _write_cli_surface(target)
    for root in (upstream, target):
        (root / "04-ops").mkdir(parents=True)
        (root / "04-ops" / "README.md").write_text("managed\n", encoding="utf-8")
    cache = target / "04-ops" / ".mypy_cache"
    cache.mkdir()
    (cache / ".gitignore").write_text("*\n", encoding="utf-8")
    (cache / "cache.data.json").write_text("runtime cache\n", encoding="utf-8")

    result = md.compare_to_upstream(target, upstream, ["ops"])

    assert result["ok"], result
    assert result["per_layer"]["ops"]["counts"] == {
        "changed": 0, "missing": 0, "extra": 0,
    }


def test_upstream_compare_rejects_retired_surface_artifacts_before_same(tmp_path):
    target = tmp_path / "target"
    _write_cli_surface(target)
    (target / "deploy.json").write_text(json.dumps({"service_kind": "api"}), encoding="utf-8")
    (target / "webapp" / "src").mkdir(parents=True)
    (target / "webapp" / "src" / "App.tsx").write_text("// stale\n", encoding="utf-8")
    (target / "server.py").write_text("# stale\n", encoding="utf-8")

    result = md.compare_to_upstream(target, md.skill_sync.BUILD_ROOT, ["core"])

    assert result["ok"] is False
    assert result["per_layer"] == {}
    assert result["managed_target"]["kind"] == "ManagedTargetError"
    assert any("inactive web Surface" in error
               for error in result["managed_target"]["errors"])


def test_manifest_check_requires_real_file(tmp_path):
    res = md.check_manifest(tmp_path, write=False, refresh=False)

    assert res["status"] == "missing"
    assert res["errors"]


def test_manifest_check_reports_dangling_authority_symlink_as_invalid(tmp_path):
    _write_cli_surface(tmp_path)
    (tmp_path / "ZE_LAYER_MANIFEST.json").symlink_to("missing-manifest.json")

    result = md.check_manifest(tmp_path, write=False, refresh=False)

    assert result["status"] == "existing"
    assert any("must not be a symlink" in error for error in result["errors"])


def test_upstream_mode_still_uses_trusted_strict_lock_parser(
        tmp_path, monkeypatch, capsys):
    target = tmp_path / "target"
    _write_cli_surface(target)
    manifest = md.layer_manifest.default_manifest(
        target,
        toolchain_files=md.skill_sync.LAYER_FILES["toolchain"],
    )
    (target / "ZE_LAYER_MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
    lock_dir = target / md.skill_sync.LOCK_DIR
    lock_dir.mkdir(parents=True)
    (lock_dir / "core.lock.json").write_text(json.dumps({
        "schema": "wrong",
        "layer": "ops",
        "interface_version": "3.3.3",
        "checksum": "0" * 64,
        "locked_at": "2026-01-01T00:00:00+00:00",
        "unknown": True,
    }), encoding="utf-8")
    # A target-owned verifier is untrusted input and must never override the
    # current auditor's strict parser.
    (target / "scripts" / "skill_sync.py").write_text(
        "import json; print(json.dumps({'ok': True}))\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "argv", [
        "managed_drift_check.py",
        "--target", str(target),
        "--upstream", str(md.skill_sync.BUILD_ROOT),
        "--layers", "core",
    ])

    exit_code = md.main()
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["ok"] is False
    assert "lock_consistency" in payload
    lock_error = payload["lock_consistency"]["drift"][0]["lock"]["error"]
    assert "schema must be" in lock_error
    assert "layer must be core" in lock_error
    assert "unknown fields" in lock_error
    assert "delegate" not in payload["lock_consistency"]


@pytest.mark.parametrize(
    "mutation",
    ("wrong_schema", "wrong_layer", "unknown_field", "duplicate_key", "symlink"),
)
def test_trusted_lock_preflight_rejects_every_authority_mutation(
        tmp_path, mutation):
    target = tmp_path / mutation
    _write_cli_surface(target)
    lock_dir = target / md.skill_sync.LOCK_DIR
    lock_dir.mkdir(parents=True)
    path = lock_dir / "core.lock.json"
    version, checksum = md.skill_sync.source_contract(
        "core", md.skill_sync.BUILD_ROOT, target_root=target,
    )
    lock = {
        "schema": "ze.interface_lock.v2",
        "layer": "core",
        "interface_version": version,
        "checksum": checksum,
        "locked_at": "2026-01-01T00:00:00+00:00",
    }
    if mutation == "wrong_schema":
        lock["schema"] = "wrong"
    elif mutation == "wrong_layer":
        lock["layer"] = "ops"
    elif mutation == "unknown_field":
        lock["unknown"] = True
    if mutation == "duplicate_key":
        path.write_text(
            json.dumps(lock)[:-1] + f',"checksum":"{checksum}"}}',
            encoding="utf-8",
        )
    elif mutation == "symlink":
        external = tmp_path / "external-lock.json"
        external.write_text(json.dumps(lock), encoding="utf-8")
        path.symlink_to(external)
    else:
        path.write_text(json.dumps(lock), encoding="utf-8")

    result = md.lock_consistency(
        target,
        ["core"],
        source_root=md.skill_sync.BUILD_ROOT,
    )

    assert result["ok"] is False
    assert result["drift"][0]["lock"]["error"]


def test_manifest_check_can_write_manifest(tmp_path):
    _write_web_surface(tmp_path)

    res = md.check_manifest(tmp_path, write=True, refresh=False)

    assert res["status"] == "written"
    assert not res["errors"]
    assert (tmp_path / "ZE_LAYER_MANIFEST.json").is_file()


def test_manifest_check_reports_stale_toolchain_inventory(tmp_path):
    _write_web_surface(tmp_path)
    expected = list(md.skill_sync.LAYER_FILES["toolchain"])
    manifest = md.layer_manifest.default_manifest(tmp_path, toolchain_files=expected[:-1])
    (tmp_path / "ZE_LAYER_MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")

    res = md.check_manifest(tmp_path, write=False, refresh=False)

    assert any(expected[-1] in error and "missing" in error for error in res["errors"])


def test_manifest_refresh_only_reconciles_toolchain_inventory(tmp_path):
    _write_web_surface(tmp_path)
    manifest = md.layer_manifest.default_manifest(tmp_path, toolchain_files=["scripts/old.py"])
    manifest["preserve"].append("downstream-owned")
    manifest["x_policy"] = "keep-me"
    (tmp_path / "ZE_LAYER_MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")

    res = md.check_manifest(tmp_path, write=True, refresh=True)

    assert res["status"] == "reconciled"
    updated = json.loads((tmp_path / "ZE_LAYER_MANIFEST.json").read_text(encoding="utf-8"))
    assert updated["managed"]["files"]["toolchain"] == md.skill_sync.LAYER_FILES["toolchain"]
    assert "downstream-owned" in updated["preserve"]
    assert updated["x_policy"] == "keep-me"


def test_current_tree_manifest_inventory_matches_toolchain_contract():
    res = md.check_manifest(md.skill_sync.BUILD_ROOT, write=False, refresh=False)

    assert res["errors"] == []
