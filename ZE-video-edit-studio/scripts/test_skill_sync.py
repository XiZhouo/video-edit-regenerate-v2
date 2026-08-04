"""test_skill_sync — Skill.Sync 分类 + 复制隔离回归测试。

跑法(从 scripts/ 目录): python -m pytest test_skill_sync.py -q
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import skill_sync as ss  # noqa: E402

BUILD_ROOT = ss.BUILD_ROOT
IGNORE = shutil.ignore_patterns(
    "__pycache__", ".mypy_cache", "*.pyc", "node_modules", "dist", ".venv",
)


def _make_target(tmp: Path) -> Path:
    """造一个 v3 形态的目标 skill:复制源的各层 + 含哨兵/lock 的 02-mod + 皮肤哨兵 + .data 哨兵。

    注:03-css-rules 是 skill 自有的"皮肤",**不在 sync 内** —— 这里照样拷一份(是合法 v3 skill 的
    必要组成 + is_v3_skill 检查项),并埋一个皮肤哨兵,验证 sync 永不碰它。
    """
    T = tmp / "skill"
    for rel in ("01-core", "sdk", "03-css-rules", "04-ops", "05-i18n", "webapp/src"):
        src = BUILD_ROOT / rel
        dst = T / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dst, ignore=IGNORE)
    shutil.copy2(BUILD_ROOT / "server.py", T / "server.py")
    (T / "scripts").mkdir(parents=True, exist_ok=True)
    shutil.copy2(BUILD_ROOT / "scripts" / "ze_command.py", T / "scripts" / "ze_command.py")
    # 02-mod:业务哨兵 + locks(从 source_contract 现算 → 初始 same,不依赖 BUILD_ROOT lock 是否新鲜)。
    (T / "02-mod" / "_locks").mkdir(parents=True, exist_ok=True)
    (T / "02-mod" / "MY_BUSINESS.txt").write_text("DO NOT TOUCH — 业务自有")
    for layer in ss.LAYERS:
        ver, sm = ss.source_contract(layer)
        ss._write_lock(T, layer, version=ver, checksum=sm, base=None)
    # 皮肤哨兵:03-css-rules 归 skill 自有,sync 永不碰
    (T / "03-css-rules" / "SKIN_SENTINEL.txt").write_text("MY SKIN — sync 不该碰")
    # .data 运行态哨兵
    (T / ".data").mkdir(parents=True, exist_ok=True)
    (T / ".data" / "runtime.txt").write_text("RUNTIME STATE")
    return T


def _set_lock(T: Path, layer: str, **patch) -> None:
    p = T / "02-mod" / "_locks" / f"{layer}.lock.json"
    d = json.loads(p.read_text())
    d.update(patch)
    p.write_text(json.dumps(d))


def test_same_when_locks_match_source(tmp_path):
    T = _make_target(tmp_path)
    res = ss.sync(str(T), layers=list(ss.LAYERS), apply=False)
    assert res["ok"]
    assert all(r["status"] == "same" for r in res["per_layer"].values())


def test_toolchain_layer_registered():
    """工具链是受管同步层(校验门禁随它下发到每个 skill)。"""
    assert "toolchain" in ss.LAYERS
    assert "toolchain" in ss.LAYER_DIRS and "toolchain" in ss._CONTRACT
    ver, sm = ss.source_contract("toolchain")
    assert ver != "0.0.0" and len(sm) == 64
    # 受管文件含关键门禁 + 同步引擎自身
    files = ss.LAYER_FILES["toolchain"]
    for must in ("scripts/i18n_audit.py", "scripts/verify_all.py", "scripts/skill_sync.py",
                 "scripts/local_launcher.py", "scripts/test_local_launcher.py",
                 "scripts/dependency_manifest.py", "scripts/environment_broker.py",
                 "scripts/test_dependency_manifest.py", "scripts/test_environment_broker.py",
                 "scripts/release_builder.py", "scripts/test_release_builder.py",
                 "contracts/artifact/ze.artifact-build.v1.schema.json",
                 "start.command", "start.bat", "start.ps1",
                 "scripts/mod_renderer_check.py",
                 "scripts/i18n_contract_check.py", "scripts/layer_manifest.py",
                 "scripts/managed_drift_check.py", "scripts/test_layer_manifest.py",
                 "scripts/test_managed_drift_check.py", "scripts/test_mod_renderer_check.py"):
        assert must in files


def test_toolchain_contract_excluded_from_checksum(tmp_path, monkeypatch):
    """_toolchain_contract.json 只承载版本,改它不应动 checksum(同 ops/i18n 排 _contract.json)。"""
    before = ss.source_contract("toolchain")[1]
    # 改版本字段不影响 checksum(用真实文件读改还原成本高 → 直接断言函数跳过该文件)
    import hashlib
    h = hashlib.sha256()
    for rel in ss.LAYER_FILES["toolchain"]:
        if rel.endswith("_toolchain_contract.json"):
            continue
        f = BUILD_ROOT / rel
        if f.is_file():
            h.update(rel.encode()); h.update(b"\0"); h.update(f.read_bytes())
    assert h.hexdigest() == before


def test_toolchain_initialize_brings_gates(tmp_path):
    """目标无 toolchain.lock → initialize → 把门禁脚本拷进 target/scripts + 写 lock。"""
    T = _make_target(tmp_path)
    (T / "02-mod" / "_locks" / "toolchain.lock.json").unlink()
    res = ss.sync(str(T), layers=["toolchain"], apply=True)
    assert res["ok"] and res["per_layer"]["toolchain"]["status"] == "initialize"
    for f in ("i18n_audit.py", "i18n_sync.py", "i18n_keys_lib.py", "i18n_dump_keys.mjs",
              "verify_all.py", "skill_sync.py", "layer_manifest.py", "managed_drift_check.py",
              "mod_renderer_check.py", "local_launcher.py", "test_local_launcher.py",
              "dependency_manifest.py", "environment_broker.py",
              "test_dependency_manifest.py", "test_environment_broker.py",
              "release_builder.py", "test_release_builder.py"):
        assert (T / "scripts" / f).is_file(), f"toolchain 未带 {f}"
    for f in ("start.command", "start.bat", "start.ps1"):
        assert (T / f).is_file(), f"toolchain 未带 {f}"
    assert (T / "02-mod" / "_locks" / "toolchain.lock.json").is_file()
    # 红线:业务 + 皮肤未被碰
    assert (T / "02-mod" / "MY_BUSINESS.txt").read_text() == "DO NOT TOUCH — 业务自有"


def test_apply_writes_layer_manifest_for_legacy_target(tmp_path):
    """旧 v3 skill 缺 ZE_LAYER_MANIFEST.json 时,正式 sync 会先补机器归属契约。"""
    T = _make_target(tmp_path)
    assert not (T / "ZE_LAYER_MANIFEST.json").exists()
    _set_lock(T, "toolchain", checksum="0" * 64)

    res = ss.sync(str(T), layers=["toolchain"], apply=True)

    assert res["ok"]
    assert res["layer_manifest"]["status"] == "written"
    manifest = json.loads((T / "ZE_LAYER_MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest["product"]["dirs"] == ["webapp/src/product"]


def test_toolchain_apply_reconciles_manifest_without_erasing_product_policy(tmp_path):
    T = _make_target(tmp_path)
    manifest = ss.layer_manifest.default_manifest(T, toolchain_files=ss.LAYER_FILES["toolchain"][:-1])
    manifest["preserve"].append("product/private")
    manifest["x_product_policy"] = {"upgrade": "manual"}
    (T / "ZE_LAYER_MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")

    res = ss.sync(str(T), layers=["toolchain"], apply=True)

    assert res["ok"]
    assert res["layer_manifest"]["status"] == "reconciled"
    updated = json.loads((T / "ZE_LAYER_MANIFEST.json").read_text(encoding="utf-8"))
    assert updated["managed"]["files"]["toolchain"] == ss.LAYER_FILES["toolchain"]
    assert "product/private" in updated["preserve"]
    assert updated["x_product_policy"] == {"upgrade": "manual"}


def test_manifest_symlink_is_not_reconciled_before_managed_audit(tmp_path):
    target = _make_target(tmp_path)
    external = tmp_path / "external-manifest.json"
    external.write_text(json.dumps(ss.layer_manifest.default_manifest(
        target, toolchain_files=[],
    )), encoding="utf-8")
    external_before = external.read_bytes()
    manifest_path = target / "ZE_LAYER_MANIFEST.json"
    manifest_path.symlink_to(external)
    _set_lock(target, "toolchain", checksum="0" * 64)

    result = ss.sync(str(target), layers=["toolchain"], apply=True)

    assert result["ok"] is False
    assert result["layer_manifest"]["status"] == "invalid"
    assert any("must not be a symlink" in error
               for error in result["layer_manifest"]["errors"])
    assert manifest_path.is_symlink()
    assert external.read_bytes() == external_before
    assert not (target / "scripts" / "verify_all.py").exists()


def test_compat_bump_copies_and_preserves_mod_data_and_skin(tmp_path):
    T = _make_target(tmp_path)
    _set_lock(T, "i18n", checksum="0" * 64)   # 同版本、checksum 变 → compat_bump
    dry = ss.sync(str(T), layers=["i18n"], apply=False)
    assert dry["per_layer"]["i18n"]["status"] == "compat_bump"

    res = ss.sync(str(T), layers=["i18n"], apply=True)
    assert res["ok"] and res["per_layer"]["i18n"]["status"] == "compat_bump"
    # 复制后:lock 被 bump 到源 checksum
    src_sum = ss.source_contract("i18n")[1]
    assert json.loads((T / "02-mod" / "_locks" / "i18n.lock.json").read_text())["checksum"] == src_sum
    # 红线:02-mod 业务 + 皮肤(03-css-rules)+ .data 运行态 一律没被碰
    assert (T / "02-mod" / "MY_BUSINESS.txt").read_text() == "DO NOT TOUCH — 业务自有"
    assert (T / "03-css-rules" / "SKIN_SENTINEL.txt").read_text() == "MY SKIN — sync 不该碰"
    assert (T / ".data" / "runtime.txt").read_text() == "RUNTIME STATE"
    # i18n 层真被复制(05-i18n 存在且有 zh.ts)
    assert (T / "05-i18n" / "zh.ts").is_file()


def test_core_sync_preserves_webapp_product_dir(tmp_path):
    """webapp/src/product 是下游业务前端,core sync 不能 rmtree 掉。

    回归:旧 copy_layer 对 webapp/src 整树 rmtree+copytree,会删除 seek-find-level-packer
    这类工具的 PSD reader/writer、产品资产和 Vitest 业务测试。
    """
    T = _make_target(tmp_path)
    product = T / "webapp" / "src" / "product"
    (product / "custom").mkdir(parents=True, exist_ok=True)
    (product / "custom" / "reader.ts").write_text("export const sentinel = 1\n")
    _set_lock(T, "core", checksum="0" * 64)

    res = ss.sync(str(T), layers=["core"], apply=True)

    assert res["ok"] and res["per_layer"]["core"]["status"] == "compat_bump"
    assert (product / "custom" / "reader.ts").read_text() == "export const sentinel = 1\n"
    assert (T / "webapp" / "src" / "App.tsx").is_file(), "core shell files should still be refreshed"
    assert not (T / "webapp" / "src" / "gallery").exists(), "gallery dev harness still must not ship"


def test_core_sync_preserves_surface_profile_product_dir(tmp_path):
    """Custom web.product_dir under webapp/src is also target-owned."""
    T = _make_target(tmp_path)
    shutil.rmtree(T / "webapp" / "src" / "product")
    custom_product = T / "webapp" / "src" / "product-packer"
    custom_product.mkdir(parents=True, exist_ok=True)
    (custom_product / "writer.ts").write_text("export const packer = true\n")
    (T / "ZE_SURFACE.json").write_text(json.dumps({
        "schema": "ze.surface_profile.v1",
        "primary": "web",
        "surfaces": ["web"],
        "human_control": "web_workbench",
        "entrypoints": {"web": "webapp"},
        "web": {"dir": "webapp", "product_dir": "webapp/src/product-packer"},
    }))
    _set_lock(T, "core", checksum="0" * 64)

    res = ss.sync(str(T), layers=["core"], apply=True)

    assert res["ok"] and res["per_layer"]["core"]["status"] == "compat_bump"
    assert (custom_product / "writer.ts").read_text() == "export const packer = true\n"
    assert not (T / "webapp" / "src" / "product").exists(), "source product dir must not leak into target"


def test_custom_product_dir_does_not_implicitly_preserve_old_default_product(tmp_path):
    target = _make_target(tmp_path)
    stale_default = target / "webapp" / "src" / "product"
    (stale_default / "stale.ts").write_text("must be removed\n", encoding="utf-8")
    custom_product = target / "webapp" / "src" / "product-packer"
    custom_product.mkdir()
    (custom_product / "owned.ts").write_text("must survive\n", encoding="utf-8")
    (target / "ZE_SURFACE.json").write_text(json.dumps({
        "schema": "ze.surface_profile.v1",
        "primary": "web",
        "surfaces": ["web"],
        "human_control": "web_workbench",
        "entrypoints": {"web": "webapp"},
        "web": {"dir": "webapp", "product_dir": "webapp/src/product-packer"},
    }), encoding="utf-8")

    ss.copy_layer("core", BUILD_ROOT, target)

    assert not stale_default.exists()
    assert (custom_product / "owned.ts").read_text(encoding="utf-8") == "must survive\n"

    checksum_before = ss._core_checksum(target)
    stale_default.mkdir()
    (stale_default / "rogue.ts").write_text("must be detected\n", encoding="utf-8")
    assert ss._core_checksum(target) != checksum_before


def test_core_managed_fingerprint_excludes_product_dir(tmp_path):
    """Product code changes are business changes, not core drift."""
    T = _make_target(tmp_path)
    fp1 = ss._core_managed_file_fingerprint(T)
    p = T / "webapp" / "src" / "product" / "business.ts"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("export const business = 1\n")
    assert ss._core_managed_file_fingerprint(T) == fp1
    (T / "webapp" / "src" / "App.tsx").write_text((T / "webapp" / "src" / "App.tsx").read_text() + "\n// shell change\n")
    assert ss._core_managed_file_fingerprint(T) != fp1


def test_core_managed_fingerprint_excludes_surface_profile_product_dir(tmp_path):
    """Custom product_dir changes are also business changes, not core drift."""
    T = _make_target(tmp_path)
    shutil.rmtree(T / "webapp" / "src" / "product")
    custom_product = T / "webapp" / "src" / "product-packer"
    custom_product.mkdir(parents=True, exist_ok=True)
    (T / "ZE_SURFACE.json").write_text(json.dumps({
        "schema": "ze.surface_profile.v1",
        "primary": "web",
        "surfaces": ["web"],
        "human_control": "web_workbench",
        "entrypoints": {"web": "webapp"},
        "web": {"dir": "webapp", "product_dir": "webapp/src/product-packer"},
    }))
    fp1 = ss._core_managed_file_fingerprint(T)
    (custom_product / "business.ts").write_text("export const business = 1\n")
    assert ss._core_managed_file_fingerprint(T) == fp1
    (T / "webapp" / "src" / "App.tsx").write_text((T / "webapp" / "src" / "App.tsx").read_text() + "\n// shell change\n")
    assert ss._core_managed_file_fingerprint(T) != fp1


def test_incompat_refuses_copy(tmp_path):
    T = _make_target(tmp_path)
    _set_lock(T, "ops", interface_version="0.0.0")   # MAJOR 不同 → incompat
    res = ss.sync(str(T), layers=["ops"], apply=True)
    assert not res["ok"]
    assert res["per_layer"]["ops"]["status"] == "incompat"
    # lock 不动(仍 0.0.0),不复制
    assert json.loads((T / "02-mod" / "_locks" / "ops.lock.json").read_text())["interface_version"] == "0.0.0"


def test_initialize_when_lock_missing(tmp_path):
    T = _make_target(tmp_path)
    (T / "02-mod" / "_locks" / "i18n.lock.json").unlink()
    res = ss.sync(str(T), layers=["i18n"], apply=True)
    assert res["per_layer"]["i18n"]["status"] == "initialize"
    assert (T / "02-mod" / "_locks" / "i18n.lock.json").is_file()


def test_corrupt_lock_aborts_layer_no_copy(tmp_path):
    T = _make_target(tmp_path)
    (T / "02-mod" / "_locks" / "i18n.lock.json").write_text("{ not json")
    res = ss.sync(str(T), layers=["i18n"], apply=True)
    assert not res["ok"]
    assert res["per_layer"]["i18n"]["status"] == "lock_error"
    # 损坏 lock 没被覆写、层没被复制(护栏:不据 fallback 瞎分类去 rmtree)
    assert (T / "02-mod" / "_locks" / "i18n.lock.json").read_text() == "{ not json"


def test_interface_lock_authority_is_strict_and_duplicate_safe(tmp_path):
    target = _make_target(tmp_path)
    path = target / "02-mod" / "_locks" / "core.lock.json"
    version, checksum = ss.source_contract("core")
    valid = {
        "schema": "ze.interface_lock.v2",
        "layer": "core",
        "interface_version": version,
        "checksum": checksum,
        "locked_at": "2026-01-01T00:00:00+00:00",
    }
    invalid_values = []
    for key, value in (
        ("schema", "wrong"),
        ("layer", "ops"),
        ("interface_version", ""),
        ("checksum", checksum.upper()),
        ("locked_at", ""),
    ):
        candidate = dict(valid)
        candidate[key] = value
        invalid_values.append(candidate)
    missing = dict(valid)
    missing.pop("locked_at")
    invalid_values.append(missing)
    unknown = dict(valid)
    unknown["trust_me"] = True
    invalid_values.append(unknown)

    for candidate in invalid_values:
        path.write_text(json.dumps(candidate), encoding="utf-8")
        parsed = ss._read_lock(target, "core")
        assert parsed and parsed.get("_error"), candidate

    duplicate = (
        '{"schema":"ze.interface_lock.v2","layer":"core",'
        f'"interface_version":"{version}","checksum":"{checksum}",'
        f'"checksum":"{checksum}","locked_at":"2026-01-01T00:00:00+00:00"}}'
    )
    path.write_text(duplicate, encoding="utf-8")
    before = (target / "01-core" / "README.md").read_bytes()

    result = ss.sync(str(target), layers=["core"], apply=True)

    assert result["ok"] is False
    assert result["per_layer"]["core"]["status"] == "lock_error"
    assert "duplicate JSON key" in ss._read_lock(target, "core")["_error"]
    assert (target / "01-core" / "README.md").read_bytes() == before


def test_self_lock_verify_and_init_refuse_to_repair_invalid_authority(
        tmp_path, monkeypatch):
    target = _make_target(tmp_path)
    path = target / "02-mod" / "_locks" / "core.lock.json"
    invalid = json.loads(path.read_text(encoding="utf-8"))
    invalid["schema"] = "wrong"
    path.write_text(json.dumps(invalid), encoding="utf-8")
    before = path.read_bytes()
    monkeypatch.setattr(ss, "BUILD_ROOT", target)
    monkeypatch.setattr(ss, "LAYERS", ("core",))

    verified = ss.verify_self_locks()
    initialized = ss.init_self_locks()

    assert verified["ok"] is False
    assert "schema must be" in verified["drift"][0]["lock"]["error"]
    assert initialized["ok"] is False
    assert "schema must be" in initialized["lock_errors"]["core"]
    assert path.read_bytes() == before


def test_css_skin_opt_in_not_default(tmp_path):
    """皮肤:css 是 **opt-in** 同步层 —— 在 LAYER_DIRS/_CONTRACT(可按需 --layers css 同步),
    但**不在默认 LAYERS**(故 --init-locks/--verify-locks 不强制它 == lock,皮肤定制不被判漂移)。
    默认同步(core/ops/i18n)永不碰 03-css-rules,也不写 css.lock。"""
    assert "css" in ss.LAYER_DIRS and "css" in ss._CONTRACT   # 可按需同步
    assert "css" not in ss.LAYERS                              # 但不自动受管(皮肤定制安全)
    T = _make_target(tmp_path)
    ss.sync(str(T), layers=["core", "ops", "i18n"], apply=True)   # 默认层不含 css
    assert (T / "03-css-rules" / "SKIN_SENTINEL.txt").read_text() == "MY SKIN — sync 不该碰"
    assert not (T / "02-mod" / "_locks" / "css.lock.json").exists()


def test_css_opt_in_sync_copies_and_locks(tmp_path):
    """显式 `--layers css`:整目录复制 03-css-rules(覆盖目标皮肤)+ 写 css.lock(供下次 classify)。"""
    T = _make_target(tmp_path)
    res = ss.sync(str(T), layers=["css"], apply=True)
    assert res["ok"] and res["per_layer"]["css"]["status"] in ("initialize", "compat_bump")
    # 目标皮肤被 build-v3 当前皮肤覆盖(sentinel 没了),且写了 css.lock
    assert not (T / "03-css-rules" / "SKIN_SENTINEL.txt").exists()
    assert (T / "02-mod" / "_locks" / "css.lock.json").is_file()


def test_guard_blocks_escape(tmp_path):
    T = _make_target(tmp_path)
    # 直接打 copy_layer 越界护栏:dst_root 给一个不含目标层的 root,relpath 解析在外
    import pytest as _pt
    with _pt.raises(RuntimeError):
        ss._guard_inside(tmp_path / "elsewhere" / "01-core", T)


def test_refuse_self_sync():
    res = ss.sync(str(BUILD_ROOT), layers=["core"], apply=True)
    assert not res["ok"] and "自身" in res["error"]


def test_reject_non_v3_target(tmp_path):
    (tmp_path / "notv3").mkdir()
    res = ss.sync(str(tmp_path / "notv3"), layers=["core"], apply=False)
    assert not res["ok"] and "v3 skill" in res["error"]


def test_explicit_invalid_surface_blocks_sync_before_copy_manifest_or_lock_write(tmp_path):
    """显式 profile 是投影权威；无效时不能按目录猜成 headless 后自洽变绿。"""
    target = _make_target(tmp_path)
    invalid = {
        "schema": "ze.surface_profile.v1",
        "primary": "cli",
        "surfaces": [],  # 可解析，但违反非空 + primary inclusion 合同。
        "human_control": "cli_confirm",
        "entrypoints": {"cli": "scripts/ze_command.py"},
        "cli": {"entrypoint": "scripts/ze_command.py"},
    }
    (target / "ZE_SURFACE.json").write_text(json.dumps(invalid), encoding="utf-8")
    _set_lock(target, "ops", checksum="0" * 64)
    lock_before = (target / "02-mod" / "_locks" / "ops.lock.json").read_bytes()
    managed_before = (target / "04-ops" / "README.md").read_bytes()

    result = ss.sync(str(target), layers=["ops"], apply=True)

    assert result["ok"] is False
    assert "ZE_SURFACE.json" in result["error"]
    assert result["surface_profile"]["status"] == "invalid"
    assert any("non-empty" in error for error in result["surface_profile"]["errors"])
    assert not (target / "ZE_LAYER_MANIFEST.json").exists(), "preflight must precede manifest write"
    assert (target / "02-mod" / "_locks" / "ops.lock.json").read_bytes() == lock_before
    assert (target / "04-ops" / "README.md").read_bytes() == managed_before


def test_surface_retirement_cannot_hide_old_optional_core_artifacts(tmp_path):
    target = _make_target(tmp_path)
    target.joinpath("ZE_SURFACE.json").write_text(json.dumps({
        "schema": "ze.surface_profile.v1",
        "primary": "cli",
        "surfaces": ["cli"],
        "human_control": "cli_confirm",
        "entrypoints": {"cli": "scripts/ze_command.py"},
        "cli": {"entrypoint": "scripts/ze_command.py"},
        "api": None,
        "web": None,
    }), encoding="utf-8")
    # A stale deploy hint is not allowed to override the explicit CLI Surface.
    target.joinpath("deploy.json").write_text(json.dumps({
        "service_kind": "api",
    }), encoding="utf-8")
    lock_before = (target / "02-mod" / "_locks" / "core.lock.json").read_bytes()

    with pytest.raises(ss.ManagedTargetError, match="inactive web Surface"):
        ss._core_checksum(target)
    result = ss.sync(str(target), layers=["core"], apply=True)

    assert result["ok"] is False
    assert result["managed_target"]["kind"] == "ManagedTargetError"
    assert any("inactive web Surface" in error
               for error in result["managed_target"]["errors"])
    assert any("server.py" in error for error in result["managed_target"]["errors"])
    assert (target / "webapp" / "src" / "App.tsx").is_file()
    assert (target / "server.py").is_file()
    assert (target / "02-mod" / "_locks" / "core.lock.json").read_bytes() == lock_before
    assert not (target / "ZE_LAYER_MANIFEST.json").exists()


def test_explicit_invalid_surface_blocks_projection_and_direct_copy(tmp_path):
    target = _make_target(tmp_path)
    (target / "ZE_SURFACE.json").write_text(json.dumps({
        "schema": "ze.surface_profile.v1",
        "primary": "cli",
        "surfaces": ["cli", "unknown"],
        "human_control": "cli_confirm",
        "entrypoints": {"cli": "scripts/ze_command.py"},
        "cli": {"entrypoint": "scripts/ze_command.py"},
    }), encoding="utf-8")
    sentinel = target / "01-core" / "README.md"
    before = sentinel.read_bytes()

    with pytest.raises(ss.SurfaceProfileError, match="unknown surface"):
        ss.source_contract("core", target_root=target)
    with pytest.raises(ss.SurfaceProfileError, match="unknown surface"):
        ss.copy_layer("core", BUILD_ROOT, target)

    assert sentinel.read_bytes() == before


def test_named_file_symlink_into_mod_blocks_entire_layer_before_mutation(tmp_path):
    target = _make_target(tmp_path)
    business = target / "02-mod" / "MY_BUSINESS.txt"
    managed_sentinel = target / "scripts" / "skill_sync.py"
    managed_sentinel.write_text("# target-only managed sentinel\n", encoding="utf-8")
    gate = target / "scripts" / "verify_all.py"
    gate.symlink_to("../02-mod/MY_BUSINESS.txt")

    with pytest.raises(RuntimeError, match="symlink in managed destination"):
        ss.copy_layer("toolchain", BUILD_ROOT, target)

    assert business.read_text(encoding="utf-8") == "DO NOT TOUCH — 业务自有"
    assert managed_sentinel.read_text(encoding="utf-8") == "# target-only managed sentinel\n"
    assert gate.is_symlink()


def test_named_file_parent_symlink_into_mod_is_rejected(tmp_path):
    target = _make_target(tmp_path)
    shutil.rmtree(target / "scripts")
    (target / "scripts").symlink_to("02-mod", target_is_directory=True)

    with pytest.raises(RuntimeError, match="symlink in managed destination"):
        ss.copy_layer("core", BUILD_ROOT, target)

    assert not (target / "02-mod" / "ze_command.py").exists()


def test_late_web_merge_directory_symlink_blocks_before_any_mutation(tmp_path):
    target = _make_target(tmp_path)
    ambient = target / "webapp" / "src" / "AmbientField.tsx"
    ambient.write_text("target-only ambient sentinel\n", encoding="utf-8")
    runtime_link = target / "webapp" / "src" / "zz-runtime-link"
    runtime_link.symlink_to(target / ".data", target_is_directory=True)

    with pytest.raises(RuntimeError, match="symlink in webapp/src managed merge"):
        ss.copy_layer("core", BUILD_ROOT, target)

    assert ambient.read_text(encoding="utf-8") == "target-only ambient sentinel\n"
    assert (target / ".data" / "runtime.txt").read_text(encoding="utf-8") == "RUNTIME STATE"
    assert runtime_link.is_symlink()


def test_same_checksum_cannot_hide_a_managed_directory_symlink(tmp_path):
    target = _make_target(tmp_path)
    relocated = target / ".data" / "core"
    shutil.move(str(target / "01-core"), relocated)
    (target / "01-core").symlink_to(relocated, target_is_directory=True)

    result = ss.sync(str(target), layers=["core"], apply=False)

    assert result["ok"] is False
    assert result["managed_target"]["kind"] == "ManagedTargetError"
    assert any("01-core" in error and "symlink" in error
               for error in result["managed_target"]["errors"])


def test_nested_managed_symlink_cannot_feed_runtime_bytes_to_checksum(tmp_path):
    target = _make_target(tmp_path)
    nested = target / "01-core" / "nested-runtime-link"
    nested.symlink_to(target / ".data", target_is_directory=True)

    result = ss.sync(str(target), layers=["core"], apply=False)

    assert result["ok"] is False
    assert result["managed_target"]["kind"] == "ManagedTargetError"
    assert any("nested symlink" in error and "nested-runtime-link" in error
               for error in result["managed_target"]["errors"])
    assert nested.is_symlink()
    assert (target / ".data" / "runtime.txt").read_text(encoding="utf-8") == "RUNTIME STATE"


def test_named_file_hardlink_is_atomically_replaced_without_touching_business(tmp_path):
    target = _make_target(tmp_path)
    business = target / "02-mod" / "MY_BUSINESS.txt"
    command = target / "scripts" / "ze_command.py"
    command.unlink()
    os.link(business, command)
    assert business.stat().st_ino == command.stat().st_ino

    ss.copy_layer("core", BUILD_ROOT, target)

    assert business.read_text(encoding="utf-8") == "DO NOT TOUCH — 业务自有"
    assert command.read_bytes() == (BUILD_ROOT / "scripts" / "ze_command.py").read_bytes()
    assert business.stat().st_ino != command.stat().st_ino


def test_lock_symlink_cannot_overwrite_mod_business_file(tmp_path):
    target = _make_target(tmp_path)
    business = target / "02-mod" / "MY_BUSINESS.txt"
    lock = target / "02-mod" / "_locks" / "core.lock.json"
    lock.unlink()
    lock.symlink_to("../MY_BUSINESS.txt")

    result = ss.sync(str(target), layers=["core"], apply=True)

    assert result["ok"] is False
    assert result["per_layer"]["core"]["status"] == "lock_error"
    assert business.read_text(encoding="utf-8") == "DO NOT TOUCH — 业务自有"
    assert lock.is_symlink()


def test_lock_writer_normalizes_a_symlinked_or_aliased_target_root(tmp_path):
    real_root = tmp_path / "real-skill"
    (real_root / "02-mod" / "_locks").mkdir(parents=True)
    alias_root = tmp_path / "skill-alias"
    alias_root.symlink_to(real_root, target_is_directory=True)

    ss._write_lock(
        alias_root,
        "core",
        version="1.2.3",
        checksum="0" * 64,
        base=None,
    )

    lock = json.loads(
        (real_root / "02-mod" / "_locks" / "core.lock.json").read_text(encoding="utf-8")
    )
    assert lock["interface_version"] == "1.2.3"


def test_missing_surface_profile_retains_legacy_inference(tmp_path):
    target = _make_target(tmp_path)
    assert not (target / "ZE_SURFACE.json").exists()
    state = ss.surface_profile_state(target)
    assert state["explicit"] is False
    assert state["profile"]["_inferred"] is True
    assert ss._target_has_surface(target, "web") is True
    assert len(ss.source_contract("core", target_root=target)[1]) == 64


def test_core_managed_inventory_covers_every_copied_file_kind():
    """Core inventory 不按扩展名猜测，而是覆盖完整受管复制面。"""
    import hashlib
    fingerprint = ss._core_managed_file_fingerprint(BUILD_ROOT)
    assert fingerprint
    expected = (
        "01-core/README.md",                         # docs
        "01-core/_protocol/PUBLIC_SURFACE.lock.json",  # compatibility baseline
        "sdk/bootstrap.py",                         # backend
        "webapp/src/App.tsx",                       # frontend
        "webapp/src/vite-env.d.ts",                 # declarations
        "server.py",                                # named core file
        "scripts/ze_command.py",                    # named core file
    )
    for rel in expected:
        file_path = BUILD_ROOT / rel
        assert any(
            line == f"CORE:{rel}:{hashlib.sha256(file_path.read_bytes()).hexdigest()}"
            for line in fingerprint
        ), rel
    assert all("\\" not in line.split(":", 2)[1] for line in fingerprint)
    assert not any("webapp/src/product/" in line for line in fingerprint)
    assert not any("webapp/src/gallery/" in line for line in fingerprint)


def test_core_checksum_is_pure_file_digest_without_sdk_import(monkeypatch):
    """Sync checksum 不 import/introspect sdk，因而不受 Python 版本或导入副作用影响。"""
    import builtins

    real_import = builtins.__import__

    def reject_sdk_import(name, *args, **kwargs):
        if name == "sdk" or name.startswith("sdk."):
            raise AssertionError("core checksum 不应 import sdk")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", reject_sdk_import)
    path_before = sys.path[:]
    checksum = ss._core_checksum(BUILD_ROOT)
    assert len(checksum) == 64
    assert sys.path == path_before


def test_core_checksum_scheme_and_file_inventory_are_explicit(monkeypatch):
    """Scheme token 防止新旧算法摘要被当成同一种合同误比较。"""
    import hashlib

    monkeypatch.setattr(
        ss, "_core_managed_file_fingerprint",
        lambda _root, target_root=None: ["CORE:one"],
    )
    expected = hashlib.sha256(
        "SCHEME:ze.core-content.v1\nCORE:one".encode("utf-8")
    ).hexdigest()
    assert ss._core_checksum(BUILD_ROOT) == expected


def test_every_managed_file_kind_changes_core_checksum(tmp_path):
    """Docs/tests/baselines/backend/frontend 都是实际复制物，改动必须传播。"""
    target = _make_target(tmp_path)
    mutations = (
        "01-core/README.md",
        "01-core/test_checksum_inventory.py",
        "01-core/_protocol/PUBLIC_SURFACE.lock.json",
        "sdk/bootstrap.py",
        "webapp/src/App.tsx",
    )
    before = ss._core_checksum(target)
    for index, rel in enumerate(mutations):
        file_path = target / rel
        file_path.parent.mkdir(parents=True, exist_ok=True)
        with file_path.open("a", encoding="utf-8") as handle:
            handle.write(f"\n# managed checksum mutation {index}\n")
        after = ss._core_checksum(target)
        assert after != before, rel
        before = after


def test_non_managed_product_dev_and_build_files_do_not_change_core_checksum(tmp_path):
    """Product/gallery/build trash 不会被 core copy，也不应污染 checksum。"""
    target = _make_target(tmp_path)
    before = ss._core_checksum(target)
    ignored = {
        "webapp/src/product/business.ts": "export const product = true\n",
        "webapp/src/gallery/dev.ts": "export const gallery = true\n",
        "sdk/dist/generated.py": "GENERATED = True\n",
        "sdk/.DS_Store": "finder metadata\n",
        "01-core/02-data-flow/07-persistence/.mypy_cache/3.13/cache.data.json": "runtime cache\n",
    }
    for rel, content in ignored.items():
        file_path = target / rel
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
    assert ss._core_checksum(target) == before


@pytest.mark.parametrize(
    ("layer", "relative_dir"),
    (("core", "01-core"), ("ops", "04-ops"), ("i18n", "05-i18n"), ("css", "03-css-rules")),
)
def test_mypy_runtime_cache_is_outside_copy_and_every_tree_checksum(
        tmp_path, layer, relative_dir):
    """类型检查缓存既不下发，也不能让干净 clone 与共享检出得到不同摘要。"""
    target = _make_target(tmp_path)
    before = ss.source_contract(layer, target)[1]
    cache = target / relative_dir / ".mypy_cache"
    cache.mkdir(parents=True)
    (cache / ".gitignore").write_text("*\n", encoding="utf-8")
    (cache / "cache.data.json").write_text("runtime cache\n", encoding="utf-8")

    assert ss.source_contract(layer, target)[1] == before
    ignored = ss._copy_ignore_for_root(target)(
        str(target / relative_dir), [".mypy_cache"],
    )
    assert ".mypy_cache" in ignored


def test_managed_inventory_matches_actual_core_copy(tmp_path):
    """Fingerprint inventory 与 copy_layer 共用 ignore/product 边界，不会再各自演化。"""
    target = tmp_path / "copied"
    (target / "webapp" / "src" / "product").mkdir(parents=True)
    (target / "webapp" / "src" / "product" / "sentinel.ts").write_text("owned\n")
    (target / "scripts").mkdir(parents=True)
    (target / "scripts" / "ze_command.py").write_text("# cli\n", encoding="utf-8")
    (target / "server.py").write_text("# api\n", encoding="utf-8")
    (target / "ZE_SURFACE.json").write_text((BUILD_ROOT / "ZE_SURFACE.json").read_text())

    copied = ss.copy_layer("core", BUILD_ROOT, target)

    assert copied
    assert (target / "webapp" / "src" / "product" / "sentinel.ts").is_file()
    assert ss._core_managed_file_fingerprint(target) == ss._core_managed_file_fingerprint(BUILD_ROOT)


@pytest.mark.parametrize(
    ("surface", "profile", "expects_web", "expects_server"),
    [
        ("web", {
            "schema": "ze.surface_profile.v1", "primary": "web", "surfaces": ["web"],
            "human_control": "web_workbench", "entrypoints": {"web": "webapp"},
            "web": {"dir": "webapp", "product_dir": "webapp/src/product"},
        }, True, True),
        ("headless", {
            "schema": "ze.surface_profile.v1", "primary": "cli", "surfaces": ["cli"],
            "human_control": "cli_confirm", "entrypoints": {"cli": "scripts/ze_command.py"},
            "cli": {"entrypoint": "scripts/ze_command.py"},
        }, False, False),
        ("api", {
            "schema": "ze.surface_profile.v1", "primary": "api", "surfaces": ["api"],
            "human_control": "external_review", "entrypoints": {"api": "server.py"},
            "api": {"server": "server.py"},
        }, False, True),
    ],
)
def test_projected_source_checksum_matches_web_headless_and_api_copy(
        tmp_path, surface, profile, expects_web, expects_server):
    """Source lock projection must describe exactly what each surface receives."""
    target = tmp_path / surface
    target.mkdir()
    (target / "ZE_SURFACE.json").write_text(json.dumps(profile), encoding="utf-8")
    if "cli" in profile["surfaces"]:
        entrypoint = target / "scripts" / "ze_command.py"
        entrypoint.parent.mkdir(parents=True, exist_ok=True)
        entrypoint.write_text("# cli\n", encoding="utf-8")
    if "api" in profile["surfaces"]:
        (target / "server.py").write_text("# api\n", encoding="utf-8")
    if expects_web:
        product = target / "webapp" / "src" / "product"
        product.mkdir(parents=True)
        (product / "sentinel.ts").write_text("owned\n", encoding="utf-8")

    projected = ss.source_contract("core", target_root=target)[1]
    ss.copy_layer("core", BUILD_ROOT, target)
    copied_own_checksum = ss.source_contract("core", root=target)[1]

    assert copied_own_checksum == projected
    assert (target / "webapp" / "src" / "App.tsx").is_file() is expects_web
    assert (target / "server.py").is_file() is expects_server
    assert (target / "scripts" / "ze_command.py").is_file()


def test_backend_only_change_is_compat_bump(tmp_path):
    """后端实现改(不动签名)→ checksum 变 → classify=compat_bump(后端可见性闭环)。"""
    T = _make_target(tmp_path)
    ver = ss._core_version(T)
    base_sum = ss._core_checksum(T)
    bp = T / "sdk" / "bootstrap.py"
    bp.write_text(bp.read_text() + "\n# backend-only change for D2 test\n")
    new_sum = ss._core_checksum(T)
    assert new_sum != base_sum, "后端改应改变 core checksum"
    lock = {"interface_version": ver, "checksum": base_sum}
    assert ss.classify(ver, new_sum, lock) == "compat_bump"


# ==============================================================
# 三方协议(Base / Target / Candidate)测试
# ==============================================================

def _fake_ops_source(tmp: Path, T: Path) -> Path:
    """A minimal upstream source carrying only the ops layer, seeded from T (== base)."""
    src = tmp / "fake-src"
    shutil.copytree(T / "04-ops", src / "04-ops", ignore=IGNORE)
    return src


def _ops_file(root: Path) -> Path:
    """A real managed ops file that the ops checksum inventory covers."""
    f = root / "04-ops" / "README.md"
    assert f.is_file()
    return f


def test_classify_three_way_matrix():
    """纯判定:Base/Target/Candidate 六种结局(单元级,不落盘)。"""
    base = {"interface_version": "1.2.0", "checksum": "b" * 64}
    C = lambda cs, ts, present=True: ss.classify_three_way(  # noqa: E731
        base, "1.2.0", cs, "1.2.0", ts, tgt_present=present)
    assert ss.classify_three_way(None, "1.0.0", "x", "1.0.0", "y", tgt_present=True) == "initialize"
    assert C("b" * 64, "z" * 64) == "skip"          # 上游没变(Candidate==Base)
    assert ss.classify_three_way(base, "2.0.0", "c" * 64, "1.2.0", "b" * 64,
                                 tgt_present=True) == "incompat"
    assert C("c" * 64, "b" * 64) == "update"         # 上游改、下游==Base
    assert C("c" * 64, "c" * 64) == "already"        # 下游已到 Candidate
    assert C("c" * 64, "d" * 64) == "conflict"       # 上游改 + 下游偏第三态
    assert C("c" * 64, "d" * 64, present=False) == "update"  # 下游无内容 → 视作初始化式更新


def test_three_way_clean_downstream_updates(tmp_path):
    """上游改了、下游没改(Target==Base)→ update,安全覆盖到上游内容。"""
    T = _make_target(tmp_path)
    src = _fake_ops_source(tmp_path, T)
    up = _ops_file(src)
    up.write_text(up.read_text() + "\n# upstream change\n")

    res = ss.sync(str(T), layers=["ops"], apply=True, source_root=src)

    assert res["ok"]
    entry = res["per_layer"]["ops"]
    assert entry["status"] == "compat_bump" and entry["three_way"] == "update"
    assert "# upstream change" in (T / up.relative_to(src)).read_text()


def test_three_way_conflict_refuses_overwrite_by_default(tmp_path):
    """上游改 + 下游也改(偏离第三态)→ conflict,默认不覆盖、不写 lock(闭掉盲拷抹定制)。"""
    T = _make_target(tmp_path)
    src = _fake_ops_source(tmp_path, T)
    up = _ops_file(src)
    up.write_text(up.read_text() + "\n# upstream change\n")
    down = T / up.relative_to(src)
    down.write_text(down.read_text() + "\n# DOWNSTREAM FORK\n")
    lock_before = (T / "02-mod" / "_locks" / "ops.lock.json").read_bytes()

    res = ss.sync(str(T), layers=["ops"], apply=True, source_root=src)

    assert res["ok"] is False
    entry = res["per_layer"]["ops"]
    assert entry["status"] == "conflict" and entry["three_way"] == "conflict"
    assert "conflict" in entry["hint"] or "冲突" in entry["hint"]
    # 下游定制未被覆盖,上游内容未落,lock 未动
    assert "# DOWNSTREAM FORK" in down.read_text()
    assert "# upstream change" not in down.read_text()
    assert (T / "02-mod" / "_locks" / "ops.lock.json").read_bytes() == lock_before


def test_dry_run_receipt_is_machine_readable_per_file(tmp_path):
    """dry-run 出机器可读三方计划:schema + 逐文件 {path,layer,action,owner,base/target/candidate,decision}。"""
    T = _make_target(tmp_path)
    src = _fake_ops_source(tmp_path, T)
    up = _ops_file(src)
    up.write_text(up.read_text() + "\n# upstream change\n")

    res = ss.sync(str(T), layers=["ops"], apply=False, source_root=src)

    receipt = res["receipt"]
    assert receipt["schema"] == ss.SYNC_RECEIPT_SCHEMA
    plan = receipt["layers"]["ops"]
    assert plan["verdict"] == "update"
    assert plan["base"]["checksum"] and plan["candidate"]["checksum"] and plan["target"]["checksum"]
    files = plan["files"]
    assert files
    for entry in files:
        assert set(entry) == {"path", "layer", "action", "owner", "base",
                              "target", "candidate", "decision"}
        assert entry["action"] in {"add", "update", "delete", "conflict", "skip"}
        assert entry["owner"] in {"upstream", "downstream", "both"}
        assert entry["base"] is None   # per-file base 不入 lock,层级 base 在 verdict 里裁定
    changed = next(e for e in files if e["path"] == up.relative_to(src).as_posix())
    assert changed["action"] == "update" and changed["owner"] == "upstream"
    assert changed["candidate"] != changed["target"]


def test_apply_receipt_rejects_when_target_changed_after_freeze(tmp_path):
    """冻结 receipt 后下游又被改 → apply 二验 Target 不符 → receipt_stale 拒绝、不复制。"""
    T = _make_target(tmp_path)
    src = _fake_ops_source(tmp_path, T)
    up = _ops_file(src)
    up.write_text(up.read_text() + "\n# upstream change\n")
    frozen = ss.sync(str(T), layers=["ops"], apply=False, source_root=src)["receipt"]
    down = T / up.relative_to(src)
    down.write_text(down.read_text() + "\n# late downstream edit\n")

    res = ss.sync(str(T), layers=["ops"], apply=True, source_root=src, receipt=frozen)

    assert res["ok"] is False
    assert res["per_layer"]["ops"]["status"] == "receipt_stale"
    assert "# late downstream edit" in down.read_text()
    assert "# upstream change" not in down.read_text()


def test_force_overwrite_requires_receipt(tmp_path):
    """无冻结 receipt 不许 --force-overwrite(强覆盖必须先冻结复核)。"""
    T = _make_target(tmp_path)
    res = ss.sync(str(T), layers=["ops"], apply=True, force_overwrite=True)
    assert res["ok"] is False and "receipt" in res["error"]


def test_force_overwrite_consumes_receipt_overwrites_and_records(tmp_path):
    """--force-overwrite 配冻结 receipt(二验通过)→ 覆盖冲突并记 forced_overwrite 账。"""
    T = _make_target(tmp_path)
    src = _fake_ops_source(tmp_path, T)
    up = _ops_file(src)
    up.write_text(up.read_text() + "\n# upstream change\n")
    down = T / up.relative_to(src)
    down.write_text(down.read_text() + "\n# DOWNSTREAM FORK\n")
    frozen = ss.sync(str(T), layers=["ops"], apply=False, source_root=src)["receipt"]
    assert frozen["layers"]["ops"]["verdict"] == "conflict"

    res = ss.sync(str(T), layers=["ops"], apply=True, source_root=src,
                  receipt=frozen, force_overwrite=True)

    assert res["ok"]
    entry = res["per_layer"]["ops"]
    assert entry.get("forced_overwrite") is True and entry["lock"] == "written"
    assert "# upstream change" in down.read_text()
    assert "# DOWNSTREAM FORK" not in down.read_text()


# ==============================================================
# 同步四步闸(先理解 / 等价确认 / 反哺 / 金样硬门)测试
# ==============================================================

_UNDERSTANDING_DOC = {
    "schema": "ze.tool-understanding.v1",
    "key_flows": [{"id": "flow-1"}, {"id": "flow-2"}],
}


def _write_understanding(T: Path, doc: dict | None = None) -> bytes:
    payload = json.dumps(doc if doc is not None else _UNDERSTANDING_DOC,
                         ensure_ascii=False, indent=2).encode("utf-8")
    (T / "tool_understanding.json").write_bytes(payload)
    return payload


def _hard_policy(tmp: Path, skill_name: str) -> Path:
    p = tmp / "gate-policy-hard.json"
    p.write_text(json.dumps({
        "schema": "ze.sync_gate_policy.v1",
        "default_mode": "soft",
        "skills": {skill_name: {"mode": "hard"}},
    }), encoding="utf-8")
    return p


def test_tool_understanding_is_protected_target_path(tmp_path):
    """第 1 步证据物归下游自有:入保护路径,受管写不得覆盖。"""
    assert "tool_understanding.json" in ss._BASE_PROTECTED_TARGET_PATHS
    T = _make_target(tmp_path)
    with pytest.raises(RuntimeError, match="protected target path"):
        ss._guard_managed_destination(T / "tool_understanding.json", T)


def test_receipt_records_missing_tool_understanding(tmp_path):
    T = _make_target(tmp_path)
    res = ss.sync(str(T), layers=["ops"], apply=False)
    understanding = res["receipt"]["understanding"]
    assert understanding["present"] is False
    assert "warning" in understanding


def test_receipt_summarizes_tool_understanding(tmp_path):
    import hashlib
    T = _make_target(tmp_path)
    payload = _write_understanding(T)
    res = ss.sync(str(T), layers=["ops"], apply=False)
    understanding = res["receipt"]["understanding"]
    assert understanding["present"] is True
    assert understanding["sha256"] == hashlib.sha256(payload).hexdigest()
    assert understanding["key_flows_count"] == 2
    assert understanding["schema"] == "ze.tool-understanding.v1"


def test_symlinked_tool_understanding_counts_as_missing(tmp_path):
    T = _make_target(tmp_path)
    external = tmp_path / "outside.json"
    external.write_text(json.dumps(_UNDERSTANDING_DOC), encoding="utf-8")
    (T / "tool_understanding.json").symlink_to(external)
    res = ss.sync(str(T), layers=["ops"], apply=False)
    assert res["receipt"]["understanding"]["present"] is False


def test_upstream_gaps_recorded_in_receipt(tmp_path):
    T = _make_target(tmp_path)
    gaps = [{"code": "missing-editor-hook", "note": "core 缺 X 注入点", "file": "01-core/x.py"},
            {"code": "css-token-gap", "note": "缺 token", "file": None}]
    res = ss.sync(str(T), layers=["ops"], apply=False, upstream_gaps=gaps)
    assert res["receipt"]["upstream_gaps"] == gaps


def test_invalid_upstream_gap_rejected(tmp_path):
    T = _make_target(tmp_path)
    res = ss.sync(str(T), layers=["ops"], apply=False,
                  upstream_gaps=[{"code": "", "note": "x", "file": None}])
    assert res["ok"] is False and "upstream_gap" in res["error"]


def test_gate_policy_defaults_soft_and_gold_samples_listed():
    """金样五家在册且模式合法;默认 soft(闸随流量逐家收紧,不制造全红噪音)。

    _sync_gate_policy.json 是上游-only 文件(不进受管 toolchain 清单):下游拷贝无此文件,
    策略正确回落全 soft —— 金样名单断言只在上游跑(1.23.0 同类教训:受管测不得耦合上游-only 物)。
    """
    policy = ss._load_sync_gate_policy(BUILD_ROOT)
    assert not policy.get("error")
    assert policy["default_mode"] == "soft"
    if policy.get("source") is None:
        assert policy["skills"] == {}   # 下游:缺文件 = 未采用,全 soft
        pytest.skip("no scripts/_sync_gate_policy.json (downstream copy; upstream-only file)")
    for gold in ("ZE-image-cleanup-v3", "ZE-seek-find-level-audit-v3",
                 "ZE-playable-scene-engine-v3", "ZE-batch-image-foundry",
                 "ZE-seek-find-level-packer"):
        assert gold in policy["skills"], gold
        assert policy["skills"][gold] in ("soft", "hard")


def test_corrupt_gate_policy_fails_closed(tmp_path):
    T = _make_target(tmp_path)
    bad = tmp_path / "bad-policy.json"
    bad.write_text("{ not json", encoding="utf-8")
    res = ss.sync(str(T), layers=["ops"], apply=False, gate_policy_path=bad)
    assert res["ok"] is False and "四步闸策略无效" in res["error"]


def test_hard_gate_blocks_apply_without_understanding_zero_mutation(tmp_path):
    """hard 模式 + 缺 tool_understanding.json → --apply 复制前拒绝:零变更。"""
    T = _make_target(tmp_path)
    _set_lock(T, "ops", checksum="0" * 64)   # 造一个待复制的 compat_bump
    policy = _hard_policy(tmp_path, T.name)
    ops_before = (T / "04-ops" / "README.md").read_bytes()
    lock_before = (T / "02-mod" / "_locks" / "ops.lock.json").read_bytes()

    res = ss.sync(str(T), layers=["ops"], apply=True, gate_policy_path=policy)

    assert res["ok"] is False
    assert res["gate"]["mode"] == "hard"
    blocked = res["receipt"]["blocked"]
    assert blocked and blocked[0]["code"] == "understanding_missing"
    # 零变更:层未复制、lock 未动、manifest 未写、receipt 文件未落
    assert (T / "04-ops" / "README.md").read_bytes() == ops_before
    assert (T / "02-mod" / "_locks" / "ops.lock.json").read_bytes() == lock_before
    assert not (T / "ZE_LAYER_MANIFEST.json").exists()
    assert not (T / "ZE_SYNC_RECEIPT.json").exists()


def test_hard_gate_apply_proceeds_with_understanding(tmp_path):
    T = _make_target(tmp_path)
    _set_lock(T, "ops", checksum="0" * 64)
    _write_understanding(T)
    policy = _hard_policy(tmp_path, T.name)

    res = ss.sync(str(T), layers=["ops"], apply=True, gate_policy_path=policy)

    assert res["ok"] is True
    assert res["per_layer"]["ops"]["status"] == "compat_bump"
    assert res["receipt"]["understanding"]["present"] is True
    # apply 落账:receipt 写 target 根(反哺聚合源)
    receipt_file = json.loads((T / "ZE_SYNC_RECEIPT.json").read_text(encoding="utf-8"))
    assert receipt_file["schema"] == ss.SYNC_RECEIPT_SCHEMA
    assert receipt_file["understanding"]["present"] is True


def test_soft_mode_apply_warns_but_proceeds_without_understanding(tmp_path):
    """默认 soft:缺证据物只 WARN 记录,不拦(渐进硬化,不把欠账变全红)。"""
    T = _make_target(tmp_path)
    _set_lock(T, "ops", checksum="0" * 64)

    res = ss.sync(str(T), layers=["ops"], apply=True)

    assert res["ok"] is True
    assert res["gate"]["mode"] == "soft"
    assert any("tool_understanding" in w for w in res.get("warnings", []))
    assert res["receipt"]["understanding"]["present"] is False


def _fake_eval_payload(status: str = "passed", errors: list | None = None) -> dict:
    case = {"id": "case-1", "command": "item.toggle", "status": status, "latency_ms": 3}
    if errors:
        case["errors"] = errors
    return {"ok": status == "passed", "schema": "ze.eval_suite.result.v1",
            "case_count": 1, "cases": [case]}


def test_equivalence_skipped_when_eval_cases_undeclared(tmp_path, monkeypatch):
    T = _make_target(tmp_path)
    _set_lock(T, "ops", checksum="0" * 64)
    monkeypatch.setattr(ss, "_run_target_eval",
                        lambda _dst: {"ok": True, "skipped": "mod 未声明 registry.eval_cases"})

    res = ss.sync(str(T), layers=["ops"], apply=True)

    assert res["ok"] is True
    eq = res["receipt"]["equivalence"]
    assert eq["skipped"] is True and eq["cases"] == 0


def test_equivalence_diff_soft_warns_and_records(tmp_path, monkeypatch):
    T = _make_target(tmp_path)
    _set_lock(T, "ops", checksum="0" * 64)
    _write_understanding(T)
    calls = {"n": 0}

    def fake_eval(_dst):
        calls["n"] += 1
        if calls["n"] == 1:
            return _fake_eval_payload("passed")
        return _fake_eval_payload("failed", ["error_code mismatch: expected a, got b"])

    monkeypatch.setattr(ss, "_run_target_eval", fake_eval)

    res = ss.sync(str(T), layers=["ops"], apply=True)

    assert res["ok"] is True    # soft:记录不拦
    eq = res["receipt"]["equivalence"]
    assert eq["mode"] == "pre_post" and len(eq["diff"]) == 1
    assert eq["diff"][0]["case"] == "case-1" and eq["diff"][0]["kind"] == "changed"
    assert any("等价对照" in w for w in res.get("warnings", []))
    assert calls["n"] == 2      # 前后各一次


def test_equivalence_diff_hard_blocks_with_nonzero_exit(tmp_path, monkeypatch):
    """hard 模式 + apply 前后 eval diff 非空 → ok False + receipt 记 blocked(受管层已复制)。"""
    T = _make_target(tmp_path)
    _set_lock(T, "ops", checksum="0" * 64)
    _write_understanding(T)
    policy = _hard_policy(tmp_path, T.name)
    calls = {"n": 0}

    def fake_eval(_dst):
        calls["n"] += 1
        if calls["n"] == 1:
            return _fake_eval_payload("passed")
        return _fake_eval_payload("failed", ["behavior changed"])

    monkeypatch.setattr(ss, "_run_target_eval", fake_eval)

    res = ss.sync(str(T), layers=["ops"], apply=True, gate_policy_path=policy)

    assert res["ok"] is False
    blocked = res["receipt"]["blocked"]
    assert blocked and blocked[0]["code"] == "equivalence_diff"
    assert res["receipt"]["equivalence"]["diff"]
    # 落账的 receipt 文件同样记 blocked(审计可追溯)
    receipt_file = json.loads((T / "ZE_SYNC_RECEIPT.json").read_text(encoding="utf-8"))
    assert receipt_file["blocked"][0]["code"] == "equivalence_diff"


def test_equivalence_no_diff_hard_passes(tmp_path, monkeypatch):
    T = _make_target(tmp_path)
    _set_lock(T, "ops", checksum="0" * 64)
    _write_understanding(T)
    policy = _hard_policy(tmp_path, T.name)
    monkeypatch.setattr(ss, "_run_target_eval", lambda _dst: _fake_eval_payload("passed"))

    res = ss.sync(str(T), layers=["ops"], apply=True, gate_policy_path=policy)

    assert res["ok"] is True
    assert res["receipt"]["equivalence"]["diff"] == []


def test_latency_jitter_does_not_flip_equivalence(tmp_path, monkeypatch):
    """时延预算抖动不算行为差异(等价对照只比 status + 非 latency errors)。"""
    T = _make_target(tmp_path)
    _set_lock(T, "ops", checksum="0" * 64)
    calls = {"n": 0}

    def fake_eval(_dst):
        calls["n"] += 1
        if calls["n"] == 1:
            return _fake_eval_payload("failed", ["latency 210ms > budget 200ms"])
        return _fake_eval_payload("failed", ["latency 350ms > budget 200ms"])

    monkeypatch.setattr(ss, "_run_target_eval", fake_eval)

    res = ss.sync(str(T), layers=["ops"], apply=True)

    assert res["receipt"]["equivalence"]["diff"] == []


def test_dry_run_never_writes_sync_receipt_file(tmp_path):
    T = _make_target(tmp_path)
    ss.sync(str(T), layers=["ops"], apply=False)
    assert not (T / "ZE_SYNC_RECEIPT.json").exists()


def test_interface_lock_underscore_annotations_are_legal(tmp_path):
    """`_` 前缀 = 人类附注命名空间(MAJOR 迁移声明等):任意个数合法,非空字符串强制。"""
    target = _make_target(tmp_path)
    path = target / "02-mod" / "_locks" / "core.lock.json"
    version, checksum = ss.source_contract("core")
    lock = {
        "schema": "ze.interface_lock.v2",
        "layer": "core",
        "interface_version": version,
        "checksum": checksum,
        "locked_at": "2026-01-01T00:00:00+00:00",
        "_comment": "MAJOR 迁移声明 1.102.0->3.54.0",
        "_migration": "按 MIGRATIONS 适配 02-mod(coresync 首航)",
    }
    path.write_text(json.dumps(lock), encoding="utf-8")
    parsed = ss._read_lock(target, "core")
    assert parsed and not parsed.get("_error"), parsed
    # 附注必须非空字符串;空串仍拒
    lock["_note"] = ""
    path.write_text(json.dumps(lock), encoding="utf-8")
    parsed = ss._read_lock(target, "core")
    assert parsed and parsed.get("_error")


def test_core_py_deps_check_reports_missing(tmp_path):
    """CORE_PY_DEPS 对照:缺包报 missing;齐了 ok;上游无声明 → None(3.55.0 反哺)。"""
    src = tmp_path / "src"; dst = tmp_path / "dst"
    (src / "01-core" / "_protocol").mkdir(parents=True)
    dst.mkdir()
    (src / "01-core" / "_protocol" / "CORE_PY_DEPS.json").write_text(
        json.dumps({"schema": "ze.core-py-deps.v1",
                    "requires": ["httpx>=0.27", "cryptography>=42"]}), encoding="utf-8")
    (dst / "requirements.txt").write_text("fastapi>=0.115\nhttpx>=0.27\n", encoding="utf-8")
    out = ss._core_py_deps_check(src, dst)
    assert out["status"] == "missing" and out["missing"] == ["cryptography>=42"]
    (dst / "requirements.txt").write_text("httpx>=0.27\nCryptography>=42  # aead\n", encoding="utf-8")
    assert ss._core_py_deps_check(src, dst)["status"] == "ok"   # 大小写/注释不敏感
    assert ss._core_py_deps_check(dst, dst) is None             # 无声明文件 → 跳过


def test_toolchain_py_deps_check_reports_missing(tmp_path):
    """Toolchain dependencies are checked even when core is not synced."""
    src = tmp_path / "src"; dst = tmp_path / "dst"
    (src / "scripts").mkdir(parents=True)
    dst.mkdir()
    (src / "scripts" / "TOOLCHAIN_PY_DEPS.json").write_text(
        json.dumps({
            "schema": "ze.toolchain-py-deps.v1",
            "requires": ["cryptography>=42", "jsonschema>=4.23"],
        }),
        encoding="utf-8",
    )
    (dst / "requirements.txt").write_text(
        "cryptography>=42\n",
        encoding="utf-8",
    )
    out = ss._toolchain_py_deps_check(src, dst)
    assert out["status"] == "missing"
    assert out["missing"] == ["jsonschema>=4.23"]
    (dst / "requirements.txt").write_text(
        "cryptography>=42\njsonschema>=4.23\n",
        encoding="utf-8",
    )
    assert ss._toolchain_py_deps_check(src, dst)["status"] == "ok"


def test_web_config_drift_reports_missing_deps(tmp_path):
    """webapp 配置漂移:deps 键差/tsconfig 键值差/vite dedupe 差;齐了 aligned(3.55.0 反哺)。"""
    src = tmp_path / "s"; dst = tmp_path / "d"
    for root, deps in ((src, {"react": "^18", "dompurify": "^3"}), (dst, {"react": "^18"})):
        (root / "webapp").mkdir(parents=True)
        (root / "webapp" / "package.json").write_text(
            json.dumps({"dependencies": deps}), encoding="utf-8")
    (src / "webapp" / "tsconfig.json").write_text(
        json.dumps({"compilerOptions": {"strict": True, "jsx": "react-jsx"}}), encoding="utf-8")
    (dst / "webapp" / "tsconfig.json").write_text(
        json.dumps({"compilerOptions": {"strict": False, "jsx": "react-jsx"}}), encoding="utf-8")
    (src / "webapp" / "vite.config.ts").write_text(
        "export default { resolve: { dedupe: ['react', 'dompurify'] } }", encoding="utf-8")
    (dst / "webapp" / "vite.config.ts").write_text(
        "export default { resolve: { dedupe: ['react'] } }", encoding="utf-8")
    out = ss._web_config_drift(src, dst)
    assert out["missing_dependencies"] == ["dompurify"]
    assert out["tsconfig_compiler_option_drift"] == ["strict"]
    assert out["vite_dedupe_missing"] == ["dompurify"]
    out2 = ss._web_config_drift(src, src)
    assert out2 == {"status": "aligned"}
