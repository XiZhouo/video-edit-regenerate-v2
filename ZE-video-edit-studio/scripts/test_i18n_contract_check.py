"""test_i18n_contract_check — UI 代码裸中文门禁回归测试(默认全扫策略)。

跑法(从 scripts/):python -m pytest test_i18n_contract_check.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import i18n_contract_check as ic  # noqa: E402


def test_default_scan_clean():
    """默认全扫:当前 UI 代码应零裸中文(门禁基线;迁移已完成)。"""
    res = ic.run()
    assert res["ok"], f"裸中文:{res['violations']}"


def test_actually_scans_files():
    """全扫策略应真的扫到一批文件(不是空跑)。"""
    res = ic.run()
    assert res["scanned_files"] >= 10


def test_new_file_guarded_by_default(tmp_path, monkeypatch):
    """新写的 UI 文件(不在任何豁免名单)默认被守 —— 出现裸中文即违规。"""
    root = tmp_path
    (root / "01-core" / "04-user-facing").mkdir(parents=True)
    f = root / "01-core" / "04-user-facing" / "NewPanel.tsx"
    f.write_text("export const P = () => <div>未配置</div>\n", encoding="utf-8")
    monkeypatch.setattr(ic, "BUILD_ROOT", root)
    monkeypatch.setattr(ic, "SCAN_ROOTS", ["01-core/04-user-facing"])
    monkeypatch.setattr(ic, "LEGACY_EXEMPT", [])
    res = ic.run()
    assert not res["ok"]
    assert any(v["file"].endswith("NewPanel.tsx") for v in res["violations"])


def test_legacy_exempt_skips(tmp_path, monkeypatch):
    """列进 LEGACY_EXEMPT 的历史文件暂不卡(收缩式豁免)。"""
    root = tmp_path
    (root / "01-core" / "04-user-facing").mkdir(parents=True)
    f = root / "01-core" / "04-user-facing" / "Old.tsx"
    f.write_text("export const O = () => <div>旧文件</div>\n", encoding="utf-8")
    monkeypatch.setattr(ic, "BUILD_ROOT", root)
    monkeypatch.setattr(ic, "SCAN_ROOTS", ["01-core/04-user-facing"])
    monkeypatch.setattr(ic, "LEGACY_EXEMPT", ["01-core/04-user-facing/Old.tsx"])
    res = ic.run()
    assert res["ok"]
    assert res["legacy_skipped"] == 1


def test_detects_cjk_in_code(tmp_path):
    f = tmp_path / "X.tsx"
    f.write_text("export const X = () => <div>设置</div>\n", encoding="utf-8")
    vio = ic.check_file(f)
    assert any(v["char"] == "设" for v in vio)


def test_detects_cjk_in_string(tmp_path):
    f = tmp_path / "Y.tsx"
    f.write_text("const label = '保存'\n", encoding="utf-8")
    assert ic.check_file(f)


def test_ignores_line_comment_cjk(tmp_path):
    f = tmp_path / "C.tsx"
    f.write_text("const x = 1  // 这是中文注释\n", encoding="utf-8")
    assert not ic.check_file(f)


def test_ignores_block_comment_cjk(tmp_path):
    f = tmp_path / "B.tsx"
    f.write_text("/* 整块中文注释\n * 第二行 */\nconst x = 1\n", encoding="utf-8")
    assert not ic.check_file(f)


def test_allows_geometric_symbols(tmp_path):
    """几何符号 ◐ ⚙ ✓ 非 CJK,允许作语言中立图标。"""
    f = tmp_path / "S.tsx"
    f.write_text("const icon = '◐'; const gear = '⚙'; const check = '✓'\n", encoding="utf-8")
    assert not ic.check_file(f)


def test_dict_and_seed_files_exempt():
    """词表文件豁免(CJK 合法家);04-layout.config 不再整文件豁免(C1:改专项扫 action chrome)。"""
    assert ic._is_exempt("05-i18n/zh.ts")
    assert ic._is_exempt("02-mod/09-i18n.ts")
    assert not ic._is_exempt("02-mod/04-layout.config.ts")  # C1:改 check_layout_config 专项扫
    assert not ic._is_exempt("01-core/04-user-facing/03-authentication/LoginPage.tsx")


def test_layout_config_action_chrome(tmp_path):
    """C1:04-layout.config —— action chrome 精确键(label/confirm/title/…)的 CJK 值违规;
    labelKey/confirmKey/labelField(精确键之外)与 newEntityDefaults 块的 CJK 放行。"""
    f = tmp_path / "04-layout.config.ts"
    f.write_text(
        "export const modLayout = {\n"
        "  actions: [\n"
        "    { command: 'x.run', label: '运行' },\n"            # 违规:裸中文 action label
        "    { command: 'x.del', confirmKey: 'mod.confirm.del' },\n"  # 放行:confirmKey
        "    { command: 'x.cp', labelKey: 'mod.action.cp' },\n"      # 放行:labelKey
        "  ],\n"
        "  view: { labelField: '标题' },\n"                     # 放行:labelField 非 action 精确键
        "}\n"
        "export const newEntityDefaults = {\n"
        "  note: { name: '新条目', data: { title: '新标题', label: '新标签' } },\n"  # 放行:实体数据块
        "}\n",
        encoding="utf-8",
    )
    viols = ic.check_layout_config(f)
    assert len(viols) == 1
    assert viols[0]["char"] == "label" and ic._CJK_RE.search("运行")
