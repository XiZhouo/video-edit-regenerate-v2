"""test_ops_contract_check — core/sdk env 读取 ↔ OPS 配置词表 门禁回归测试。

跑法(从 scripts/):python -m pytest test_ops_contract_check.py -q
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
import ops_contract_check as oc  # noqa: E402
sys.path.insert(0, str(oc.CONFIG_DIR))
import config_schema as cs  # noqa: E402


def test_current_tree_is_clean():
    """core/sdk 当前的 ZE_ env 读取应全部已声明(门禁基线)。"""
    res = oc.run()
    assert res["ok"], f"未声明 env 读取:{res['violations']}"


def test_schema_declares_knobs():
    exact = cs.exact_env_vars()
    assert len(exact) >= 8, f"精确键异常少:{exact}"
    assert "ZE_DATA_DIR" in exact and "ZE_USERS_PATH" in exact
    prefixes = cs.env_prefixes()
    assert "ZE_RB_" in prefixes and "ZE_CAPACITY_" in prefixes


# ---- D4-R1 secret 贯通(19.0.0 轮反向定向断言)----

def test_secret_masking_clean():
    """门禁基线:schema secret 键 seed→snapshot 全贯通脱敏。"""
    res = oc.check_secret_masking()
    assert res["schema_secret_count"] >= 1, "schema 里应至少有一个 secret 键(飞书 App Secret 等)"
    assert res["problems"] == [], res["problems"]


def test_secret_masking_red_when_seed_drops_secret_flag(monkeypatch):
    """自证红:复现 b2e55b4bc 同型 bug——seed 后 secret 标志丢失必须被抓。"""
    import dataclasses

    import registry as reg_mod

    real_seed = reg_mod.seed_framework_config

    def bad_seed(reg):
        real_seed(reg)
        key = next(k.key for k in cs.FRAMEWORK_KNOBS if k.secret)
        reg._items[key] = dataclasses.replace(reg._items[key], secret=False)

    monkeypatch.setattr(reg_mod, "seed_framework_config", bad_seed)
    res = oc.check_secret_masking()
    assert any("丢了 secret=True" in p["why"] for p in res["problems"]), res["problems"]


# ---- R2 配置写入原子门面 ----

def test_config_atomic_wiring_clean():
    res = oc.check_config_atomic_wiring()
    assert res["problems"] == [], res
    assert "update_values" in res["calls"]
    assert "set_values" not in res["calls"]
    assert "save_values" not in res["calls"]


def test_config_atomic_wiring_red_on_legacy_two_step(tmp_path):
    """自证红:门面退回先改内存、后落盘的两步写时必须被抓。"""
    target = tmp_path / "bootstrap.py"
    target.write_text(
        "class Repository:\n"
        "    def save_config(self, updates):\n"
        "        rejected = self.config.set_values(updates)\n"
        "        self.config.save_values('config.json')\n"
        "        return rejected\n",
        encoding="utf-8",
    )
    res = oc.check_config_atomic_wiring(target)
    assert any("未调用原子 update_values" in p["why"] for p in res["problems"]), res
    assert any("两步写" in p["why"] for p in res["problems"]), res


def test_config_surface_wiring_clean():
    res = oc.check_config_surface_wiring()
    assert res["problems"] == [], res
    assert "_require_super_admin" in res["calls"]


def test_config_surface_wiring_red_without_admin_or_cas(tmp_path):
    target = tmp_path / "server.py"
    target.write_text(
        "async def put_config(request, actor):\n"
        "    _require_edit(actor)\n"
        "    return _repo.save_config({})\n",
        encoding="utf-8",
    )
    res = oc.check_config_surface_wiring(target)
    assert len(res["problems"]) == 2, res


def test_config_client_wiring_clean():
    res = oc.check_config_client_wiring()
    assert res["problems"] == [], res


def test_config_client_wiring_red_when_cas_and_idempotency_are_optional(tmp_path):
    target = tmp_path / "authApi.ts"
    target.write_text(
        "export async function apiPutConfig(\n"
        "  updates: Record<string, unknown>, idempotencyKey?: string, expectedVersion?: number,\n"
        "): Promise<any> {\n"
        "  const body: Record<string, unknown> = { updates }\n"
        "  if (expectedVersion !== undefined) body.expected_version = expectedVersion\n"
        "  return body\n"
        "}\n",
        encoding="utf-8",
    )

    res = oc.check_config_client_wiring(target)

    assert any("idempotencyKey" in p["why"] and "省略" in p["why"] for p in res["problems"]), res
    assert any("expectedVersion" in p["why"] and "省略" in p["why"] for p in res["problems"]), res
    assert any("条件发送" in p["why"] for p in res["problems"]), res


def test_detects_undeclared_env(tmp_path):
    f = tmp_path / "bad.py"
    f.write_text('import os\nv = os.environ.get("ZE_NOPE_NOT_DECLARED")\n')
    vio = oc.check_file(f)
    assert any(v["env"] == "ZE_NOPE_NOT_DECLARED" for v in vio)


def test_prefix_covers_dynamic_scope():
    """ZE_RB_<SCOPE>_* 动态 scope 被前缀族覆盖,不算违规。"""
    assert cs.is_covered("ZE_RB_GPU_ESRGAN_MAX_CONCURRENCY")
    assert cs.is_covered("ZE_CAPACITY_ENTITY_COUNT_MAX")


def test_skips_comment_line(tmp_path):
    """整行 # 注释里的 ZE_ 不算违规。"""
    f = tmp_path / "c.py"
    f.write_text('# 旧值 ZE_REMOVED_VAR 仅注释\nx = 1\n')
    assert not oc.check_file(f)


def test_does_not_truncate_ze_prefixed_filename_into_env_var(tmp_path):
    f = tmp_path / "profile.py"
    f.write_text('path = "ZE_SURFACE.json"\n', encoding="utf-8")
    assert not oc.check_file(f)


def test_declared_env_covered_by_self():
    """schema 自声明的精确键自己必被 is_covered(自洽)。"""
    for v in cs.exact_env_vars():
        assert cs.is_covered(v)


def test_default_consistency_with_capacity():
    """schema 里 ZE_CAPACITY_* 默认值必须 == core CapacityLimits 的真实默认(防漂移)。"""
    # 注入 capacity 叶子(仿 test_capacity.py)
    core = oc.BUILD_ROOT / "01-core" / "01-data-structure"
    for sub in ("02-entity", "05-capacity-limit"):
        p = str((core / sub).resolve())
        if p not in sys.path:
            sys.path.insert(0, p)
    from capacity import CapacityLimits  # noqa: E402

    lim = CapacityLimits()
    want = {
        "ze.capacity.entity_count_max": lim.entity_count_max,
        "ze.capacity.entity_data_size_max": lim.entity_data_size_max,
        "ze.capacity.entity_field_count_max": lim.entity_field_count_max,
        "ze.capacity.asset_blob_size_max": lim.asset_blob_size_max,
        "ze.capacity.doc_total_assets_size_max": lim.doc_total_assets_size_max,
    }
    by_key = {s.key: s.default for s in cs.FRAMEWORK_KNOBS}
    for key, expected in want.items():
        assert by_key[key] == expected, f"{key} schema 默认 {by_key[key]} ≠ 代码 {expected}"


def _int_constant_expr(node: ast.expr) -> int:
    """Evaluate the tiny integer-arithmetic subset used by source defaults."""
    if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult)):
        left = _int_constant_expr(node.left)
        right = _int_constant_expr(node.right)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        return left * right
    raise AssertionError(f"HTTP upload default is not literal integer arithmetic: {ast.dump(node)}")


def test_http_asset_upload_default_matches_ops_schema():
    """HTTP streaming fence and its unique ops knob must not drift apart."""
    server_path = oc.BUILD_ROOT / "server.py"
    tree = ast.parse(server_path.read_text(encoding="utf-8"), filename=str(server_path))
    defaults = {
        target.id: _int_constant_expr(node.value)
        for node in tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        for target in (
            node.targets if isinstance(node, ast.Assign) else [node.target]
        )
        if isinstance(target, ast.Name)
        and target.id == "_DEFAULT_HTTP_ASSET_UPLOAD_MAX_BYTES"
    }
    assert set(defaults) == {"_DEFAULT_HTTP_ASSET_UPLOAD_MAX_BYTES"}
    by_key = {s.key: s for s in cs.FRAMEWORK_KNOBS}
    spec = by_key["ze.http.asset_upload_max_bytes"]
    assert spec.env_var == "ZE_HTTP_ASSET_UPLOAD_MAX_BYTES"
    assert spec.default == defaults["_DEFAULT_HTTP_ASSET_UPLOAD_MAX_BYTES"]
    assert sum(
        1 for item in cs.FRAMEWORK_KNOBS
        if item.env_var == "ZE_HTTP_ASSET_UPLOAD_MAX_BYTES"
    ) == 1, "HTTP asset upload env must have one ops owner"
