"""test_css_contract_check — 前端-CSS 契约检查器回归测试。

跑法(从 scripts/):python -m pytest test_css_contract_check.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import css_contract_check as cc  # noqa: E402


def test_current_tree_is_clean():
    """build-v3 当前前端应 0 违规(门禁基线)。"""
    res = cc.run()
    assert res["ok"], f"违规:{res['by_kind']} —— {res['violations'][:5]}"


def test_css_allow_sets_nonempty():
    classes, tokens = cc.css_allow_sets()
    assert len(classes) > 100, f"class 集异常少:{len(classes)}"
    assert len(tokens) > 50, f"token 集异常少:{len(tokens)}"
    # webapp 自身的 .ze-app 容器类也纳入(检查器盲区修复)
    assert "ze-app" in classes
    assert "--ze-accent-500" in tokens


def test_detects_undefined_class(tmp_path):
    f = tmp_path / "X.tsx"
    f.write_text('export const X = () => <div className="ze-not-a-real-class">x</div>\n')
    vio = cc.check_file(f, {"ze-real"}, {"--ze-accent-500"})
    kinds = {v["kind"] for v in vio}
    assert "UNDEFINED_CLASS" in kinds


def test_passes_bem_modifier(tmp_path):
    """ze-X--Y modifier:基类 ze-X 已定义则放行(BEM 约定 modifier 不逐个定义)。"""
    f = tmp_path / "X.tsx"
    f.write_text('export const X = () => <div className="ze-editor ze-editor--media">x</div>\n')
    vio = cc.check_file(f, {"ze-editor"}, {"--ze-accent-500"})
    assert not any(v["kind"] == "UNDEFINED_CLASS" for v in vio)


def test_detects_raw_hex(tmp_path):
    f = tmp_path / "X.tsx"
    f.write_text("const s = { color: '#ff0000' }\n")
    vio = cc.check_file(f, set(), set())
    assert any(v["kind"] == "RAW_HEX" and v["detail"] == "#ff0000" for v in vio)


def test_detects_undefined_token(tmp_path):
    f = tmp_path / "X.tsx"
    f.write_text("const s = { color: 'var(--ze-nonexistent)' }\n")
    vio = cc.check_file(f, set(), {"--ze-accent-500"})
    assert any(v["kind"] == "UNDEFINED_TOKEN" and v["detail"] == "--ze-nonexistent" for v in vio)


def test_passes_defined_token(tmp_path):
    f = tmp_path / "X.tsx"
    f.write_text("const s = { color: 'var(--ze-accent-500)' }\n")
    vio = cc.check_file(f, set(), {"--ze-accent-500"})
    assert not vio


def test_ignores_block_comment_examples(tmp_path):
    """/* ... */ 注释里的示例 hex 不算违规。"""
    f = tmp_path / "X.tsx"
    f.write_text("/* 示例:color: #abcdef 仅注释 */\nconst s = { padding: 4 }\n")
    vio = cc.check_file(f, set(), set())
    assert not any(v["kind"] == "RAW_HEX" for v in vio)


def test_ignores_line_comment_hex(tmp_path):
    """行尾 // 注释里的 hex 不算违规。"""
    f = tmp_path / "X.tsx"
    f.write_text("const s = { padding: 4 }  // 旧值 #ff0000 仅注释\n")
    vio = cc.check_file(f, set(), set())
    assert not any(v["kind"] == "RAW_HEX" for v in vio)


def test_line_comment_keeps_real_hex_before_it(tmp_path):
    """注释前的真 hex 仍要报(只剥注释部分)。"""
    f = tmp_path / "X.tsx"
    f.write_text("const s = { color: '#abc' }  // 这是违规\n")
    vio = cc.check_file(f, set(), set())
    assert any(v["kind"] == "RAW_HEX" and v["detail"] == "#abc" for v in vio)


def test_detects_card_that_only_wraps_kv_list(tmp_path):
    f = tmp_path / "X.tsx"
    f.write_text(
        "export const X = () => (\n"
        "  <div className=\"ze-card\">\n"
        "    <div className=\"ze-kv-list\">\n"
        "      <div className=\"ze-kv-list__row\"><span className=\"ze-kv-list__key\">A</span><strong className=\"ze-kv-list__value\">B</strong></div>\n"
        "    </div>\n"
        "  </div>\n"
        ")\n",
        encoding="utf-8",
    )
    classes = {"ze-card", "ze-kv-list", "ze-kv-list__row", "ze-kv-list__key", "ze-kv-list__value"}
    vio = cc.check_file(f, classes, set())
    assert any(v["kind"] == "NO_REDUNDANT_VISUAL_CONTAINER" for v in vio)


def test_detects_content_that_only_wraps_unit(tmp_path):
    f = tmp_path / "X.tsx"
    f.write_text(
        "export const X = () => (\n"
        "  <div className=\"ze-editor__content\">\n"
        "    <div className=\"ze-editor__unit\" />\n"
        "  </div>\n"
        ")\n",
        encoding="utf-8",
    )
    vio = cc.check_file(f, {"ze-editor__content", "ze-editor__unit"}, set())
    assert any(v["kind"] == "NO_REDUNDANT_VISUAL_CONTAINER" for v in vio)


def test_passes_pass_through_content_wrapping_unit(tmp_path):
    f = tmp_path / "X.tsx"
    f.write_text(
        "export const X = () => (\n"
        "  <div className=\"ze-editor__content\" data-content-mode=\"pass-through\">\n"
        "    <div className=\"ze-editor__unit\" />\n"
        "  </div>\n"
        ")\n",
        encoding="utf-8",
    )
    vio = cc.check_file(f, {"ze-editor__content", "ze-editor__unit"}, set())
    assert not any(v["kind"] == "NO_REDUNDANT_VISUAL_CONTAINER" for v in vio)


def test_passes_control_zone_with_single_button(tmp_path):
    f = tmp_path / "X.tsx"
    f.write_text(
        "export const X = () => <div className=\"ze-editor__zone\"><button className=\"ze-button\">Run</button></div>\n",
        encoding="utf-8",
    )
    vio = cc.check_file(f, {"ze-editor__zone", "ze-button"}, set())
    assert not any(v["kind"] == "NO_REDUNDANT_VISUAL_CONTAINER" for v in vio)
