"""test_mod_renderer_check — mod renderer 层级/功能区消费门禁回归测试。

跑法(从 scripts/):python -m pytest test_mod_renderer_check.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mod_renderer_check as mc  # noqa: E402


def test_current_tree_has_no_hard_violations():
    res = mc.run()
    assert res["ok"], f"违规:{res['by_kind']} —— {res['violations'][:5]}"


def test_allows_structural_inline_layout(tmp_path):
    f = tmp_path / "R.tsx"
    f.write_text(
        "export const R = () => <div style={{ display: 'grid', minHeight: 0, overflow: 'auto' }} />\n",
        encoding="utf-8",
    )
    assert mc.check_renderer_file(f) == []


def test_detects_inline_visual_style(tmp_path):
    f = tmp_path / "R.tsx"
    f.write_text(
        "export const R = () => <span style={{ fontWeight: 700, color: 'var(--ze-ink-muted)' }} />\n",
        encoding="utf-8",
    )
    vio = mc.check_renderer_file(f)
    kinds = [v["kind"] for v in vio]
    assert kinds.count("MOD_RENDERER_INLINE_VISUAL") == 2


def test_detects_inline_visual_identifier_and_ternary_values(tmp_path):
    # 1.28.0 块级扫描:变量值与三元值不再逃逸(旧行级正则只认字面量形态值)。
    f = tmp_path / "R.tsx"
    f.write_text(
        "const secondaryColor = 'var(--ze-ink-inverse)'\n"
        "export const R = ({ selected }: { selected: boolean }) => (\n"
        "  <span style={{ color: secondaryColor, opacity: selected ? 0.86 : undefined }} />\n"
        ")\n",
        encoding="utf-8",
    )
    vio = [v for v in mc.check_renderer_file(f) if v["kind"] == "MOD_RENDERER_INLINE_VISUAL"]
    assert {v["detail"] for v in vio} == {"color", "opacity"}


def test_detects_inline_visual_in_multiline_block(tmp_path):
    f = tmp_path / "R.tsx"
    f.write_text(
        "export const R = ({ on }: { on: boolean }) => (\n"
        "  <div\n"
        "    style={{\n"
        "      width: '100%',\n"
        "      border: on ? '2px solid var(--ze-accent-500)' : 'none',\n"
        "      boxShadow: on ? 'var(--ze-shadow-floating)' : undefined,\n"
        "    }}\n"
        "  />\n"
        ")\n",
        encoding="utf-8",
    )
    vio = [v for v in mc.check_renderer_file(f) if v["kind"] == "MOD_RENDERER_INLINE_VISUAL"]
    assert {v["detail"] for v in vio} == {"border", "boxShadow"}
    assert {v["line"] for v in vio} == {5, 6}


def test_inline_visual_allow_mark_is_line_scoped_in_block(tmp_path):
    f = tmp_path / "R.tsx"
    f.write_text(
        "export const R = () => (\n"
        "  <div\n"
        "    style={{\n"
        "      fontSize: 49, // mod-renderer-allow-inline-visual:实机 HUD 字号\n"
        "      color: 'red',\n"
        "    }}\n"
        "  />\n"
        ")\n",
        encoding="utf-8",
    )
    vio = [v for v in mc.check_renderer_file(f) if v["kind"] == "MOD_RENDERER_INLINE_VISUAL"]
    assert {v["detail"] for v in vio} == {"color"}


def test_style_block_walker_ignores_line_comments_with_quotes(tmp_path):
    # 块内 // 注释里的撇号不得让引号跟踪失步(否则块边界跑飞:后续块被吞并、违规重复计)。
    f = tmp_path / "R.tsx"
    f.write_text(
        "export const R = () => (\n"
        "  <div\n"
        "    style={{\n"
        "      width: '100%', // don't treat this apostrophe as a string opener\n"
        "      color: 'red',\n"
        "    }}\n"
        "  />\n"
        ")\n"
        "export const S = () => <span style={{ opacity: 0.5 }} />\n",
        encoding="utf-8",
    )
    vio = [v for v in mc.check_renderer_file(f) if v["kind"] == "MOD_RENDERER_INLINE_VISUAL"]
    assert sorted((v["detail"], v["line"]) for v in vio) == [("color", 5), ("opacity", 9)]


def test_structural_only_multiline_block_stays_green(tmp_path):
    f = tmp_path / "R.tsx"
    f.write_text(
        "export const R = () => (\n"
        "  <div style={{\n"
        "    display: 'grid', gridTemplateColumns: '56px minmax(0, 1fr) auto',\n"
        "    gap: 8, minHeight: 0, overflowY: 'auto', transform: 'translate(-50%, -50%)',\n"
        "  }} />\n"
        ")\n",
        encoding="utf-8",
    )
    assert not [v for v in mc.check_renderer_file(f) if v["kind"] == "MOD_RENDERER_INLINE_VISUAL"]


def test_detects_unit_form_body_and_chrome_classes(tmp_path):
    f = tmp_path / "R.tsx"
    f.write_text(
        "export const R = () => (\n"
        "  <div className=\"ze-editor__form-body\">\n"
        "    <div className=\"ze-editor__unit\"><span className=\"ze-editor__title\">A</span></div>\n"
        "  </div>\n"
        ")\n",
        encoding="utf-8",
    )
    kinds = {v["kind"] for v in mc.check_renderer_file(f)}
    assert "MOD_RENDERER_FORM_BODY" in kinds
    assert "MOD_RENDERER_UNIT_BOUNDARY" in kinds
    assert "MOD_RENDERER_CHROME_CLASS" in kinds


def test_allow_mark_is_line_scoped(tmp_path):
    f = tmp_path / "R.tsx"
    f.write_text(
        "export const R = () => (\n"
        "  <div className=\"ze-editor__unit\">allowed</div> // mod-renderer-allow-unit\n"
        ")\n",
        encoding="utf-8",
    )
    assert not any(v["kind"] == "MOD_RENDERER_UNIT_BOUNDARY" for v in mc.check_renderer_file(f))


def test_selection_contract_warns_on_deleted_props_channel(tmp_path):
    f = tmp_path / "R.tsx"
    f.write_text(
        "import type { CustomPaneProps } from '@ze/core/04-user-facing/01-layout/03-editor'\n"
        "export function Pane(props: CustomPaneProps) {\n"
        "  const rule = props.selection.rule\n"
        "  return <div onClick={() => props.onSelectEntity('rule', rule)} />\n"
        "}\n",
        encoding="utf-8",
    )
    warnings = mc.check_selection_contract(f)
    kinds = [w["kind"] for w in warnings]
    assert kinds.count("MOD_RENDERER_SELECTION_CONTRACT") == 2
    assert {w["detail"] for w in warnings} == {"props.selection", "props.onSelectEntity"}


def test_selection_contract_skips_when_slot_context_wired(tmp_path):
    f = tmp_path / "R.tsx"
    f.write_text(
        "import type { CustomPaneProps } from '@ze/core/04-user-facing/01-layout/03-editor'\n"
        "import { useSlotContext } from '@ze/core/04-user-facing/01-layout'\n"
        "export function Pane(rawProps: CustomPaneProps) {\n"
        "  const { selection, onSelect } = useSlotContext()\n"
        "  const props = { ...rawProps, selection, onSelectEntity: onSelect }\n"
        "  return <div onClick={() => props.onSelectEntity('rule', null)} />\n"
        "}\n",
        encoding="utf-8",
    )
    assert mc.check_selection_contract(f) == []


def test_selection_contract_skips_non_custom_pane_files(tmp_path):
    # scene3d renderer 信封合法带 selection/onSelectEntity(无 CustomPaneProps 引用)→ 不提示
    f = tmp_path / "R.tsx"
    f.write_text(
        "export function Scene(props: { onSelectEntity?: (t: string, id: string) => void }) {\n"
        "  return <div onClick={() => props.onSelectEntity?.('node', 'n1')} />\n"
        "}\n",
        encoding="utf-8",
    )
    assert mc.check_selection_contract(f) == []


def test_selection_contract_allow_mark_is_line_scoped(tmp_path):
    f = tmp_path / "R.tsx"
    f.write_text(
        "import type { CustomPaneProps } from '@ze/core/04-user-facing/01-layout/03-editor'\n"
        "export function Pane(props: CustomPaneProps & { selection: string[] }) {\n"
        "  return <div>{props.selection.length} selected</div> // mod-renderer-allow-selection-props\n"
        "}\n",
        encoding="utf-8",
    )
    assert mc.check_selection_contract(f) == []


def test_layout_many_editors_is_warning_only(tmp_path):
    layout = tmp_path / "04-layout.config.ts"
    layout.write_text(
        "const x = hsplit('r', 0.5,\n"
        "  slot('a','cardlist',['a']), slot('b','property',['b']), slot('c','table',['c']),\n"
        "  slot('d','canvas2d',['d']), slot('e','timeline',['e']))\n",
        encoding="utf-8",
    )
    warnings = mc.check_layout_file(layout)
    assert warnings and warnings[0]["kind"] == "MOD_LAYOUT_MANY_EDITORS_REVIEW"


def test_inline_visual_allow_mark_jsx_comment_form(tmp_path):
    """{/* mod-renderer-allow-inline-visual */} 形态(JSX children 位安全形态)同样豁免:
    豁免判定读 raw 行,不被块注释剥离吃掉(design coresync 线上事故根治)。"""
    f = tmp_path / "R.tsx"
    f.write_text(
        "export const R = () => (\n"
        "  <div>\n"
        "    <span style={{ color: 'red' }}>x</span>  {/* mod-renderer-allow-inline-visual(过渡) */}\n"
        "    <span style={{ background: 'blue' }}>y</span>\n"
        "  </div>\n"
        ")\n",
        encoding="utf-8",
    )
    vio = [v for v in mc.check_renderer_file(f) if v["kind"] == "MOD_RENDERER_INLINE_VISUAL"]
    assert {v["detail"] for v in vio} == {"background"}, vio
    assert {v["line"] for v in vio} == {4}
