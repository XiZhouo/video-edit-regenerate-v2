"""test_i18n_audit — missing-key / parity / registry / purity / slot-name 门禁回归(monkeypatch dump+scan)。

跑法(从 scripts/):.venv/bin/python -m pytest test_i18n_audit.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import i18n_keys_lib as lib  # noqa: E402
import i18n_audit as au  # noqa: E402


def _dump(zh_fw, en_fw, zh_mod=None, en_mod=None, codes=("zh", "en"), meta=("zh", "en"),
          zh_mod_values=None, en_mod_values=None, slots=None, layout_error=None):
    d = {
        "locale_codes": list(codes), "default_locale": "zh", "meta_codes": list(meta),
        "framework": {"zh": zh_fw, "en": en_fw},
        "mod": {"zh": zh_mod or [], "en": en_mod or []},
        "framework_values": {"zh": {}, "en": {}},
        "mod_values": {"zh": zh_mod_values or {}, "en": en_mod_values or {}},
        "layout_slots": slots,
    }
    if layout_error:
        d["layout_error"] = layout_error
    return d


def _stub_dicts(tmp_path: Path, codes=("zh", "en")):
    (tmp_path / "05-i18n").mkdir(parents=True, exist_ok=True)
    for c in codes:
        (tmp_path / "05-i18n" / f"{c}.ts").write_text("x", encoding="utf-8")


def _wire(monkeypatch, tmp_path, dumped, used):
    monkeypatch.setattr(lib, "dump", lambda *a, **k: dumped)
    monkeypatch.setattr(lib, "scan_used_keys", lambda *a, **k: used)
    monkeypatch.setattr(au, "BUILD_ROOT", tmp_path)


def test_clean_passes(monkeypatch, tmp_path):
    _stub_dicts(tmp_path)
    _wire(monkeypatch, tmp_path, _dump(["a.b"], ["a.b"], ["m"], ["m"]), {"a.b": ["X:1"], "mod.m": ["Y:2"]})
    res = au.run()
    assert res["ok"], res["problems"]


def test_missing_key_detected(monkeypatch, tmp_path):
    _stub_dicts(tmp_path)
    _wire(monkeypatch, tmp_path, _dump(["a.b"], ["a.b"]), {"a.typo": ["X:1"]})
    res = au.run()
    assert not res["ok"]
    assert any(p["kind"] == "missing-key" and p["key"] == "a.typo" for p in res["problems"])


def test_framework_parity_gap_detected(monkeypatch, tmp_path):
    _stub_dicts(tmp_path)
    _wire(monkeypatch, tmp_path, _dump(["a.b", "a.c"], ["a.b"]), {})  # en 缺 a.c
    res = au.run()
    assert any(p["kind"] == "parity" and p["locale"] == "en" and p["key"] == "a.c"
               for p in res["problems"])


def test_mod_parity_gap_detected(monkeypatch, tmp_path):
    _stub_dicts(tmp_path)
    _wire(monkeypatch, tmp_path, _dump(["a.b"], ["a.b"], ["m1", "m2"], ["m1"]), {})  # en mod 缺 m2
    res = au.run()
    assert any(p["kind"] == "parity" and p["layer"] == "mod" and p["key"] == "m2"
               for p in res["problems"])


def test_registry_codes_mismatch(monkeypatch, tmp_path):
    _stub_dicts(tmp_path, codes=("zh", "en"))
    _wire(monkeypatch, tmp_path, _dump(["a.b"], ["a.b"], meta=("zh",)), {})  # META 少 en
    res = au.run()
    assert any(p["kind"] == "registry" for p in res["problems"])


def test_registry_missing_dict_file(monkeypatch, tmp_path):
    _stub_dicts(tmp_path, codes=("zh",))  # 只建 zh.ts,缺 en.ts
    _wire(monkeypatch, tmp_path, _dump(["a.b"], ["a.b"]), {})
    res = au.run()
    assert any(p["kind"] == "registry" and "en.ts" in p["detail"] for p in res["problems"])


# ── 检查 4:purity(词表值语言纯度)────────────────────────────────────────────

def _kinds(res, kind):
    return [p for p in res["problems"] if p["kind"] == kind]


def test_purity_zh_latin_detected(monkeypatch, tmp_path):
    _stub_dicts(tmp_path)
    _wire(monkeypatch, tmp_path,
          _dump(["a.b"], ["a.b"], ["title"], ["title"],
                zh_mod_values={"title": "Bot Workbench"}, en_mod_values={"title": "Bot Workbench"}),
          {})
    res = au.run()
    hits = _kinds(res, "purity")
    assert any(p["key"] == "title" and p["locale"] == "zh" for p in hits), res["problems"]


def test_purity_allowlist_and_identifiers_pass(monkeypatch, tmp_path):
    _stub_dicts(tmp_path)
    ok_values = {
        "a": "AI 参数",                    # 白名单缩写
        "b": "导出 PSD",                   # 白名单格式
        "c": "撤销 (⌘Z / Ctrl+Z)",         # 键盘键名+单字母段
        "d": "按 doc_id 直接打开",          # snake_case 标识符
        "e": "路径在 02-mod/04-layout.md",  # 文件路径
        "f": "调用 getDisplayMedia 采集",   # camelCase 标识符
        "g": "有 {count} 个节点",           # 插值占位
        "h": "配置 `manual` 字段",          # 反引号代码
        "i": "导出 .ze-pack 信封",          # 连字符白名单整词
        "j": "只会记录 NotifyLog",           # PascalCase 标识符
        "k": "App ID 与 PID",               # 平台字段/进程号缩写
        "l": "dry-run(不真发)",             # CLI 术语
    }
    _wire(monkeypatch, tmp_path,
          _dump(["a.b"], ["a.b"], list(ok_values), list(ok_values),
                zh_mod_values=ok_values, en_mod_values={k: "x" for k in ok_values}),
          {})
    res = au.run()
    assert not _kinds(res, "purity"), _kinds(res, "purity")


def test_purity_en_cjk_detected(monkeypatch, tmp_path):
    _stub_dicts(tmp_path)
    _wire(monkeypatch, tmp_path,
          _dump(["a.b"], ["a.b"], ["title"], ["title"],
                zh_mod_values={"title": "操作说明"}, en_mod_values={"title": "操作说明"}),
          {})
    res = au.run()
    assert any(p["locale"] == "en" and "CJK" in p["detail"] for p in _kinds(res, "purity"))


def test_purity_series_prefix_detected_both_locales(monkeypatch, tmp_path):
    _stub_dicts(tmp_path)
    _wire(monkeypatch, tmp_path,
          _dump(["a.b"], ["a.b"], ["name"], ["name"],
                zh_mod_values={"name": "ZE 示例工作台"}, en_mod_values={"name": "ZE Demo Workbench"}),
          {})
    res = au.run()
    series_hits = [p for p in _kinds(res, "purity") if "系列名" in p["detail"]]
    assert {p["locale"] for p in series_hits} == {"zh", "en"}, res["problems"]


def test_series_prefix_cjk_adjacent_detected():
    """CJK 紧贴 ZE 无空格也要命中(Python \\b 把 CJK 当 word 字符,不能用 \\bZE\\b)。"""
    assert lib.has_series_prefix("导出ZE信封")
    assert lib.has_series_prefix("ZE 工作台")
    assert not lib.has_series_prefix("导出 .ze-pack 信封")
    assert not lib.has_series_prefix("配置 ZE_DATA_DIR")
    assert not lib.has_series_prefix("FROZEN 状态")  # ZE 是单词内部子串,不算


def test_purity_ze_pack_and_env_not_series_hit(monkeypatch, tmp_path):
    _stub_dicts(tmp_path)
    vals = {"a": "导出 .ze-pack 信封", "b": "配置 ZE_DATA_DIR 后重启"}
    _wire(monkeypatch, tmp_path,
          _dump(["a.b"], ["a.b"], list(vals), list(vals),
                zh_mod_values=vals, en_mod_values={k: "x" for k in vals}),
          {})
    res = au.run()
    assert not [p for p in _kinds(res, "purity") if "系列名" in p["detail"]], res["problems"]


# ── 检查 5:slot-name(槽位必须可译命名)──────────────────────────────────────

def _slot(sid, nameKey=None, name=None):
    return {"id": sid, "editor_class": "table", "nameKey": nameKey, "name": name}


def test_slot_missing_namekey_fails(monkeypatch, tmp_path):
    _stub_dicts(tmp_path)
    _wire(monkeypatch, tmp_path, _dump(["a.b"], ["a.b"], slots=[_slot("s1")]), {})
    res = au.run()
    assert any(p["kind"] == "slot-name" and p.get("slot") == "s1" for p in res["problems"])


def test_slot_hardcoded_name_warns_not_fails(monkeypatch, tmp_path):
    _stub_dicts(tmp_path)
    _wire(monkeypatch, tmp_path, _dump(["a.b"], ["a.b"], slots=[_slot("s1", name="硬编码名")]), {})
    res = au.run()
    assert res["ok"], res["problems"]
    assert any(w["kind"] == "slot-name" and w.get("slot") == "s1" for w in res["warnings"])


def test_slot_namekey_with_translations_passes(monkeypatch, tmp_path):
    _stub_dicts(tmp_path)
    _wire(monkeypatch, tmp_path,
          _dump(["a.b"], ["a.b"], ["slot.canvas"], ["slot.canvas"],
                zh_mod_values={"slot.canvas": "画布"}, en_mod_values={"slot.canvas": "Canvas"},
                slots=[_slot("s1", nameKey="mod.slot.canvas")]),
          {})
    res = au.run()
    assert res["ok"], res["problems"]


def test_slot_namekey_missing_locale_value_fails(monkeypatch, tmp_path):
    _stub_dicts(tmp_path)
    _wire(monkeypatch, tmp_path,
          _dump(["a.b"], ["a.b"], ["slot.canvas"], ["slot.canvas"],
                zh_mod_values={"slot.canvas": "画布"}, en_mod_values={},  # en 缺译值
                slots=[_slot("s1", nameKey="mod.slot.canvas")]),
          {})
    res = au.run()
    assert any(p["kind"] == "slot-name" and "'en'" in p["detail"] for p in res["problems"])


def test_layout_error_reported(monkeypatch, tmp_path):
    _stub_dicts(tmp_path)
    _wire(monkeypatch, tmp_path,
          _dump(["a.b"], ["a.b"], slots=[], layout_error="Could not resolve import"), {})
    res = au.run()
    assert any(p["kind"] == "slot-name" and "解析失败" in p["detail"] for p in res["problems"])


def test_no_layout_skips_slot_check(monkeypatch, tmp_path):
    """cli/api-only skill(无 04-layout.config)= layout_slots null → 不报 slot 问题。"""
    _stub_dicts(tmp_path)
    _wire(monkeypatch, tmp_path, _dump(["a.b"], ["a.b"], slots=None), {})
    res = au.run()
    assert res["ok"], res["problems"]


def test_real_tree_passes():
    """真实 build-v3 树跑一遍(需 node+esbuild;失败则说明真有问题)。"""
    res = au.run()
    assert res["ok"], res["problems"]
