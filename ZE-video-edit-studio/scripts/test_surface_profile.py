from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from surface_profile import (  # noqa: E402
    is_service_surface,
    load_surface_profile,
    surface_summary,
    validate_managed_surface_paths,
    validate_surface_profile,
)


# surface_profile_cases.json 由 ZE-skill-system 拥有(系统级 surface 契约);build-v3 测试消费它。
# 独立 checkout / worktree / 单 skill 部署时无 ZE-skill-system 兄弟 → 支持 env 显式覆盖,
# 都找不到则测试优雅 skip(而非 FileNotFoundError 误红整个 pytest-fast;C1·大整合反哺)。
_ENV_CASES = os.environ.get("ZE_SURFACE_PROFILE_CASES")
if _ENV_CASES:
    FIXTURES = Path(_ENV_CASES)
else:
    FIXTURES = Path(__file__).resolve().parents[2] / "ZE-skill-system" / "references" / "surface_profile_cases.json"
    if not FIXTURES.is_file():
        FIXTURES = Path(__file__).resolve().parents[3] / "ZE-skill-system" / "references" / "surface_profile_cases.json"


def write_fixture_file(root: Path, rel: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.name.endswith(".json"):
        path.write_text("{}\n", encoding="utf-8")
    else:
        path.write_text("# fixture\n", encoding="utf-8")


def _write_web_profile(root: Path, web_dir: str, product_dir: str) -> dict:
    (root / product_dir).mkdir(parents=True, exist_ok=True)
    profile = {
        "schema": "ze.surface_profile.v1",
        "primary": "web",
        "surfaces": ["web"],
        "human_control": "web_workbench",
        "entrypoints": {"web": web_dir},
        "cli": None,
        "api": None,
        "web": {"dir": web_dir, "product_dir": product_dir},
    }
    (root / "ZE_SURFACE.json").write_text(json.dumps(profile), encoding="utf-8")
    return profile


@pytest.mark.skipif(
    not FIXTURES.is_file(),
    reason="surface_profile_cases.json 不可用(独立 checkout 无 ZE-skill-system 兄弟;"
    "设 ZE_SURFACE_PROFILE_CASES 指向 cases 即可跑)——权威全量跑在 skills monorepo",
)
def test_surface_profile_canonical_fixtures(tmp_path):
    cases = json.loads(FIXTURES.read_text(encoding="utf-8"))["cases"]
    for case in cases:
        root = tmp_path / case["id"]
        root.mkdir()
        for rel in case.get("files", []):
            if rel == "ZE_SURFACE.json":
                continue
            write_fixture_file(root, rel)
        if case.get("profile"):
            (root / "ZE_SURFACE.json").write_text(json.dumps(case["profile"]), encoding="utf-8")
        if case.get("deploy"):
            (root / "deploy.json").write_text(json.dumps(case["deploy"]), encoding="utf-8")
        if case.get("commands"):
            commands_path = root / "commands" / "registry.json"
            commands_path.parent.mkdir(parents=True, exist_ok=True)
            commands_path.write_text(json.dumps(case["commands"]), encoding="utf-8")
        profile = load_surface_profile(root)
        errors = validate_surface_profile(root, profile)
        expect = case.get("expect", {})
        if expect.get("valid") is False:
            assert errors, case["id"]
            continue
        assert not errors, (case["id"], errors)
        if "primary" in expect:
            assert profile["primary"] == expect["primary"]
        if "surfaces" in expect:
            assert profile["surfaces"] == expect["surfaces"]
        if "service" in expect:
            assert is_service_surface(profile, case.get("deploy") or {}) is expect["service"]
        if "web" in expect:
            assert ("web" in profile["surfaces"]) is expect["web"]


def test_explicit_cli_surface_cannot_be_revived_by_stale_deploy_metadata(tmp_path):
    write_fixture_file(tmp_path, "scripts/ze_command.py")
    profile = {
        "schema": "ze.surface_profile.v1",
        "primary": "cli",
        "surfaces": ["cli"],
        "human_control": "cli_confirm",
        "entrypoints": {"cli": "scripts/ze_command.py"},
        "cli": {"entrypoint": "scripts/ze_command.py"},
        "api": None,
        "web": None,
    }
    (tmp_path / "ZE_SURFACE.json").write_text(json.dumps(profile), encoding="utf-8")
    stale_deploy = {"service_kind": "api"}
    (tmp_path / "deploy.json").write_text(json.dumps(stale_deploy), encoding="utf-8")

    loaded = load_surface_profile(tmp_path)

    assert loaded.get("_inferred") is not True
    assert is_service_surface(loaded, stale_deploy) is False
    assert surface_summary(tmp_path)["service"] is False


@pytest.mark.parametrize(
    ("web_dir", "product_dir"),
    [
        ("webapp", "webapp/src/product"),
        ("web", "web/src/product"),  # ZE-skill-deploy compatibility
        ("webapp", "webapp/src/product-packer"),
    ],
)
def test_web_product_dir_accepts_standard_and_reserved_custom_names(
    tmp_path, web_dir, product_dir
):
    profile = _write_web_profile(tmp_path, web_dir, product_dir)

    assert validate_surface_profile(tmp_path, profile) == []


@pytest.mark.parametrize("name", ["lib", "App", "components", "packer"])
def test_web_product_dir_rejects_core_or_unreserved_names(tmp_path, name):
    profile = _write_web_profile(tmp_path, "webapp", f"webapp/src/{name}")

    errors = validate_surface_profile(tmp_path, profile)

    assert any("'product' or a reserved 'product-<slug>'" in error for error in errors)


def test_web_product_dir_must_be_direct_child_of_web_src(tmp_path):
    profile = _write_web_profile(tmp_path, "webapp", "webapp/src/product/nested")

    errors = validate_surface_profile(tmp_path, profile)

    assert any("direct child of webapp/src" in error for error in errors)


def test_explicit_invalid_surface_never_falls_back_to_inference(tmp_path):
    (tmp_path / "webapp" / "src" / "product").mkdir(parents=True)
    (tmp_path / "ZE_SURFACE.json").write_text("{broken", encoding="utf-8")

    profile = load_surface_profile(tmp_path)
    errors = validate_surface_profile(tmp_path, profile)

    assert profile["_inferred"] is False
    assert any("invalid JSON" in error for error in errors)


def test_explicit_surface_rejects_duplicate_json_keys(tmp_path):
    (tmp_path / "ZE_SURFACE.json").write_text(
        '{"schema":"ze.surface_profile.v1","surfaces":["cli"],'
        '"surfaces":["web"]}',
        encoding="utf-8",
    )

    profile = load_surface_profile(tmp_path)
    errors = validate_surface_profile(tmp_path, profile)

    assert any("duplicate JSON key: surfaces" in error for error in errors)


def test_surface_authority_file_must_not_be_a_symlink(tmp_path):
    source = tmp_path / "surface-source.json"
    source.write_text("{}", encoding="utf-8")
    (tmp_path / "ZE_SURFACE.json").symlink_to(source)

    profile = load_surface_profile(tmp_path)

    assert profile["_load_error"] == "ZE_SURFACE.json must not be a symlink"


def test_web_product_dir_rejects_a_symlinked_parent_component(tmp_path):
    (tmp_path / "webapp").mkdir()
    (tmp_path / ".data" / "src" / "product").mkdir(parents=True)
    (tmp_path / "webapp" / "src").symlink_to(
        "../.data/src", target_is_directory=True,
    )
    profile = {
        "schema": "ze.surface_profile.v1",
        "primary": "web",
        "surfaces": ["web"],
        "human_control": "web_workbench",
        "entrypoints": {"web": "webapp"},
        "cli": None,
        "api": None,
        "web": {"dir": "webapp", "product_dir": "webapp/src/product"},
    }

    errors = validate_surface_profile(tmp_path, profile)

    assert "web.product_dir must not traverse a symlink: webapp/src/product" in errors


def test_headless_surface_cannot_declare_a_product_dir(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "ze_command.py").write_text("# fixture\n", encoding="utf-8")
    profile = {
        "schema": "ze.surface_profile.v1",
        "primary": "cli",
        "surfaces": ["cli"],
        "human_control": "cli_confirm",
        "entrypoints": {"cli": "scripts/ze_command.py"},
        "cli": {"entrypoint": "scripts/ze_command.py", "supports": ["list"]},
        "api": None,
        "web": {"dir": "webapp", "product_dir": "webapp/src/product"},
    }

    errors = validate_surface_profile(tmp_path, profile)

    assert "web.product_dir must be empty when web is not in surfaces" in errors


@pytest.mark.parametrize(
    ("surface", "detail", "entrypoint", "expected"),
    [
        ("cli", {"entrypoint": "bin/tool.py"}, "bin/tool.py", "scripts/ze_command.py"),
        ("api", {"server": "service/app.py"}, "service/app.py", "server.py"),
        (
            "web",
            {"dir": "web", "product_dir": "web/src/product"},
            "web",
            "webapp",
        ),
    ],
)
def test_direct_copy_surface_paths_are_canonical(
        surface, detail, entrypoint, expected):
    profile = {
        "surfaces": [surface],
        "entrypoints": {surface: entrypoint},
        surface: detail,
    }

    errors = validate_managed_surface_paths(profile)

    assert len(errors) == 1
    assert expected in errors[0]


def test_surface_rejects_entrypoint_escape_and_dual_authority(tmp_path):
    outside = tmp_path.parent / "outside-command.py"
    outside.write_text("# outside\n", encoding="utf-8")
    profile = {
        "schema": "ze.surface_profile.v1",
        "primary": "cli",
        "surfaces": ["cli"],
        "human_control": "cli_confirm",
        "entrypoints": {"cli": "../../outside-command.py"},
        "cli": {"entrypoint": "scripts/ze_command.py"},
        "api": None,
        "web": None,
    }
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "ze_command.py").write_text("# cli\n", encoding="utf-8")

    errors = validate_surface_profile(tmp_path, profile)

    assert any("entrypoints.cli must be a canonical relative path" in error for error in errors)


def test_surface_rejects_nested_and_top_level_entrypoint_disagreement(tmp_path):
    for rel in ("scripts/one.py", "scripts/two.py"):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# cli\n", encoding="utf-8")
    profile = {
        "schema": "ze.surface_profile.v1",
        "primary": "cli",
        "surfaces": ["cli"],
        "human_control": "cli_confirm",
        "entrypoints": {"cli": "scripts/two.py"},
        "cli": {"entrypoint": "scripts/one.py"},
        "api": None,
        "web": None,
    }

    errors = validate_surface_profile(tmp_path, profile)

    assert "cli.entrypoint and entrypoints.cli must be identical" in errors
