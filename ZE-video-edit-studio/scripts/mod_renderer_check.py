#!/usr/bin/env python3
"""Mod.Renderer.Check — 下游 custom renderer 的层级/功能区消费门禁。

`css_layer_guard.py` 校验「皮肤/核心如何被创作」,只在 build-v3 上游跑;下游 skill 真正会手写
业务 UI 的位置是 `02-mod/05-renderers.ts(x)`。本门禁补这个空档:它随 toolchain 下发,只扫
mod renderer,确保业务注入层消费 build-v3 的编辑器/功能区语法,不在 mod 里重造壳。

硬规则:

  1. MOD_RENDERER_INLINE_VISUAL
     `02-mod/05-renderers.ts(x)` 禁内联视觉样式:color/background/fontSize/fontWeight/
     borderRadius/opacity 等。style={{…}} **块级扫描、值形态不限**(字面量/变量/三元/多行
     全拦;1.28.0 前的行级正则只认字面量形态值,`color: someVar` 与三元值可逃逸)。
     结构性布局(display/flex/grid/position/overflow/width/height/transform/clipPath)放行。
     视觉差异应进入 build-v3 的 L1/L2/L3/L4 CSS,mod 只贴 class/data。

  2. MOD_RENDERER_UNIT_BOUNDARY
     custom renderer 禁直接使用 `.ze-editor__unit`。unit 是 property/pipeline 的信息控制单元,
     不是普通摘要、日志、列表 section 的默认外壳。普通管理面应使用 rows/list/card flow 或
     ManageZone/ToolZone/content 边界;确有通用需求则回灌 core primitive。

  3. MOD_RENDERER_FORM_BODY
     custom renderer 禁把 `.ze-editor__form-body` 当通用布局容器。表单/属性栏应使用 `property`
     editor 或 FormZone/受控 field 组件,不要在业务 renderer 里伪造 form editor 内部结构。

  4. MOD_RENDERER_CHROME_CLASS
     custom renderer 禁使用 `.ze-editor__header`/`__statusbar`/`__title`/`__count` 等 editor chrome
     内部类。标题、计数、状态属于 EditorFrame 的 chrome;业务区用 `.ze-text`/`.ze-badge`/rows。

软提示:

  - MOD_LAYOUT_MANY_EDITORS_REVIEW
    `04-layout.config.ts` 单个工作台通常 1-3 个 editor,超过 4 个必须有业务说明。此项只 warning,
    因 build-v3 demo 与少数生产工作台可以合法超过 4,但迁移评审必须看一眼。

  - MOD_RENDERER_SELECTION_CONTRACT(F8)
    CustomPaneProps 面板直读 `props.selection` / 直调 `props.onSelectEntity`:二者是已删旧契约,
    运行时 undefined / not a function(audit-v3 两次真实回归)。跨类型选择读写唯一入口是
    `useEditorSelection()`(01-layout 导出)。只在文件引用了 CustomPaneProps 且未接
    useSlotContext/useEditorSelection 时提示(scene3d renderer 信封合法带 onSelectEntity,不误伤);
    只 warning 不红,因静态无类型无法区分 mod 自定义 props 同名字段。

逃生口:
  在同一行加 `mod-renderer-allow-inline-visual` / `mod-renderer-allow-unit` /
  `mod-renderer-allow-form-body` / `mod-renderer-allow-chrome-class` /
  `mod-renderer-allow-selection-props`。逃生只能用于真运行时语义,
  不能用于规避普通视觉/加壳债。

  形态契约(design coresync 线上事故教训,107 处注释渲染成 UI 文字):标记在 JSX
  **文本位**写 `// …` 会成为 JSXText 真被渲染 —— 只能用两种安全形态:①属性位行尾
  `// 标记`(开标签未闭合的行);②children 位 `{/* 标记 */}`。豁免判定读剥注释前的
  raw 行,两种形态均生效。

用法:
  python scripts/mod_renderer_check.py
  python scripts/mod_renderer_check.py --json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MOD_DIR = ROOT / "02-mod"

ALLOW_INLINE_VISUAL = "mod-renderer-allow-inline-visual"
ALLOW_UNIT = "mod-renderer-allow-unit"
ALLOW_FORM_BODY = "mod-renderer-allow-form-body"
ALLOW_CHROME_CLASS = "mod-renderer-allow-chrome-class"
ALLOW_MANY_EDITORS = "mod-renderer-allow-many-editors"
ALLOW_SELECTION_PROPS = "mod-renderer-allow-selection-props"

_INLINE_VISUAL_PROPS = (
    "color", "backgroundColor", "backgroundImage", "background", "fontSize", "fontWeight",
    "fontFamily", "boxShadow", "borderColor", "textShadow", "textDecoration", "letterSpacing",
    "borderRadius", "outline", "filter", "backdropFilter", "mixBlendMode", "transition",
    "animation", "opacity", "border",
)
# 块内命中禁用属性名即拦,值形态不限(长名在前防 border 抢先截断 borderRadius)。
_INLINE_PROP_RE = re.compile(
    r"\b(" + "|".join(sorted(_INLINE_VISUAL_PROPS, key=len, reverse=True)) + r")\s*:"
)
_STYLE_OPEN_RE = re.compile(r"\bstyle\s*=\s*\{\s*\{")
_CLASS_ATTR_RE = re.compile(
    r'className\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|\{`([^`]*)`\}|\{"([^"]*)"\}|\{\'([^\']*)\'\})',
    re.DOTALL,
)
_ZE_WORD_RE = re.compile(r"ze-[a-z0-9_-]+")
_SLOT_CALL_RE = re.compile(r"\bslot\s*\(")
# F8 选择契约软提示:已删旧契约的两种直用形态(读 props.selection / 调 props.onSelectEntity)。
_SELECTION_FOOTGUN_RE = re.compile(r"\b(?:props|rawProps)\s*\.\s*(selection\b|onSelectEntity\b)")
_SELECTION_SOURCE_RE = re.compile(r"\buseSlotContext\b|\buseEditorSelection\b")
_CUSTOM_PANE_RE = re.compile(r"\bCustomPaneProps\b")


@dataclass(frozen=True)
class _ClassRule:
    css_class: str
    kind: str
    allow_mark: str
    hint: str


_CLASS_RULES = [
    _ClassRule(
        "ze-editor__unit",
        "MOD_RENDERER_UNIT_BOUNDARY",
        ALLOW_UNIT,
        "unit 只用于 property/pipeline 信息控制单元;普通摘要/日志/列表改用 rows/content/zone 组织",
    ),
    _ClassRule(
        "ze-editor__form-body",
        "MOD_RENDERER_FORM_BODY",
        ALLOW_FORM_BODY,
        "form-body 是 property/form editor 内部结构;业务 renderer 不应拿它做通用布局",
    ),
    _ClassRule(
        "ze-editor__header",
        "MOD_RENDERER_CHROME_CLASS",
        ALLOW_CHROME_CLASS,
        "header 是 EditorFrame chrome;业务区不要伪造编辑器横栏",
    ),
    _ClassRule(
        "ze-editor__statusbar",
        "MOD_RENDERER_CHROME_CLASS",
        ALLOW_CHROME_CLASS,
        "statusbar 是 EditorFrame chrome;业务区状态应进 zone 或内容行",
    ),
    _ClassRule(
        "ze-editor__title",
        "MOD_RENDERER_CHROME_CLASS",
        ALLOW_CHROME_CLASS,
        "title 是 editor chrome 文本;业务 section 标题用 ze-text/badge/rows",
    ),
    _ClassRule(
        "ze-editor__count",
        "MOD_RENDERER_CHROME_CLASS",
        ALLOW_CHROME_CLASS,
        "count 是 editor chrome 计数;业务摘要用 ze-badge 或状态行",
    ),
]


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return path.name


def _strip_block_comments_keep_lines(text: str) -> str:
    return re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.DOTALL)


def _strip_line_comment(line: str) -> str:
    quote = ""
    j = 0
    while j < len(line) - 1:
        c = line[j]
        if quote:
            if c == quote:
                quote = ""
            elif c == "\\":
                j += 1
        elif c in "'\"`":
            quote = c
        elif c == "/" and line[j + 1] == "/":
            return line[:j]
        j += 1
    return line


def _static_classes(line: str) -> set[str]:
    out: set[str] = set()
    for m in _CLASS_ATTR_RE.finditer(line):
        value = next((g for g in m.groups() if g is not None), "")
        out.update(_ZE_WORD_RE.findall(value))
    return out


def _style_blocks(text: str) -> list[tuple[int, str]]:
    """提取平衡的 style={{…}} 块 → [(块起始行号, 块文本)];引号/模板串里的花括号不计深度,
    引号外的 // 行注释整行跳过(注释里的撇号不得让引号跟踪失步、块边界跑飞)。"""
    blocks: list[tuple[int, str]] = []
    for m in _STYLE_OPEN_RE.finditer(text):
        depth = 2
        quote = ""
        j = m.end()
        while j < len(text) and depth > 0:
            c = text[j]
            if quote:
                if c == "\\":
                    j += 1
                elif c == quote:
                    quote = ""
            elif c == "/" and text[j + 1 : j + 2] == "/":
                nxt = text.find("\n", j)
                if nxt < 0:
                    j = len(text)
                    break
                j = nxt  # 停在换行符,循环尾 j += 1 越过
            elif c in "'\"`":
                quote = c
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            j += 1
        blocks.append((text[: m.start()].count("\n") + 1, text[m.start():j]))
    return blocks


def _renderer_files(root: Path = ROOT) -> list[Path]:
    base = root / "02-mod"
    files: list[Path] = []
    for name in ("05-renderers.ts", "05-renderers.tsx"):
        p = base / name
        if p.is_file():
            files.append(p)
    subdir = base / "05-renderers"
    if subdir.is_dir():
        files.extend(p for p in subdir.rglob("*") if p.suffix in (".ts", ".tsx"))
    # 真 renderer 实现按 05-renderers.ts 文档约定写在 05-code/renderers/(05-renderers.ts 只是注册表 seam,
    # `import { X } from './05-code/renderers/X'`)。不扫它 = 真 3D/自定义 renderer 的内联样式/手写 class 双盲。
    code_renderers = base / "05-code" / "renderers"
    if code_renderers.is_dir():
        files.extend(p for p in code_renderers.rglob("*") if p.suffix in (".ts", ".tsx"))
    return sorted(set(files))


def check_renderer_file(path: Path) -> list[dict]:
    raw = path.read_text(encoding="utf-8")
    text = _strip_block_comments_keep_lines(raw)
    lines = text.splitlines()
    # 豁免判定用 raw 行(剥注释前):JSX 里 // 落文本位会渲染成 UI 文字(design coresync
    # 线上事故 107 处),安全形态是属性位 // 或 {/* */} —— 后者若用剥后行判定会被吃掉失效。
    raw_lines = raw.splitlines()
    violations: list[dict] = []
    # 内联视觉:style={{…}} 块级扫描,值形态不限(1.28.0 起;行级 allow 标记仍逐行豁免)。
    for block_line, block in _style_blocks(text):
        block_clean = "\n".join(_strip_line_comment(bl) for bl in block.splitlines())
        for m in _INLINE_PROP_RE.finditer(block_clean):
            line_no = block_line + block_clean[: m.start()].count("\n")
            marked = raw_lines[line_no - 1] if 0 < line_no <= len(raw_lines) else ""
            if ALLOW_INLINE_VISUAL in marked:
                continue
            violations.append({
                "file": _rel(path),
                "line": line_no,
                "kind": "MOD_RENDERER_INLINE_VISUAL",
                "detail": m.group(1),
                "hint": "mod renderer 只允许结构性布局内联;视觉样式回 build-v3 CSS/atom/data-*",
            })
    for i, original in enumerate(lines, 1):
        if original.lstrip().startswith("//") or original.lstrip().startswith("*"):
            continue
        line = _strip_line_comment(original)
        classes = _static_classes(line)
        marked = raw_lines[i - 1] if 0 < i <= len(raw_lines) else ""
        for rule in _CLASS_RULES:
            if rule.css_class in classes and rule.allow_mark not in marked:
                violations.append({
                    "file": _rel(path),
                    "line": i,
                    "kind": rule.kind,
                    "detail": rule.css_class,
                    "hint": rule.hint,
                })
    return violations


def check_selection_contract(path: Path) -> list[dict]:
    """软提示(F8):CustomPaneProps 面板直用已删旧契约 props.selection / props.onSelectEntity。

    三重护栏防误伤:只看引用了 CustomPaneProps 的文件(scene3d renderer 信封合法带
    onSelectEntity/selection,无 CustomPaneProps 即跳过);文件已接 useSlotContext /
    useEditorSelection 视为已收束(audit-v3 修复后的影子注入形态合法);行尾
    mod-renderer-allow-selection-props 可逐行豁免。只 warning 不红。
    """
    raw = path.read_text(encoding="utf-8")
    text = _strip_block_comments_keep_lines(raw)
    if not _CUSTOM_PANE_RE.search(text) or _SELECTION_SOURCE_RE.search(text):
        return []
    raw_lines = raw.splitlines()
    warnings: list[dict] = []
    for i, original in enumerate(text.splitlines(), 1):
        stripped = original.lstrip()
        if stripped.startswith("//") or stripped.startswith("*"):
            continue
        if ALLOW_SELECTION_PROPS in (raw_lines[i - 1] if 0 < i <= len(raw_lines) else ""):
            continue
        line = _strip_line_comment(original)
        for m in _SELECTION_FOOTGUN_RE.finditer(line):
            warnings.append({
                "file": _rel(path),
                "line": i,
                "kind": "MOD_RENDERER_SELECTION_CONTRACT",
                "detail": f"props.{m.group(1)}",
                "hint": "CustomPaneProps 无 selection/onSelectEntity(旧契约已删,运行时崩溃);跨类型选择读写走 useEditorSelection()(01-layout 导出);合法同名字段在该行加 mod-renderer-allow-selection-props",
            })
    return warnings


def check_layout_file(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    raw = path.read_text(encoding="utf-8")
    if ALLOW_MANY_EDITORS in raw:
        return []
    code = _strip_block_comments_keep_lines(raw)
    # 这是软提示,不做 AST 求值:它只提醒迁移评审检查「是否把功能区拆成 editor」。
    slot_count = len(_SLOT_CALL_RE.findall(code))
    if slot_count <= 4:
        return []
    return [{
        "file": _rel(path),
        "line": 1,
        "kind": "MOD_LAYOUT_MANY_EDITORS_REVIEW",
        "detail": f"{slot_count} slot() calls",
        "hint": "默认工作台 1-3 个 editor;超过 4 个需说明每个都是独立工作面,不要把功能区拆成 editor",
    }]


def run(root: Path = ROOT) -> dict:
    files = _renderer_files(root)
    violations: list[dict] = []
    warnings: list[dict] = []
    for f in files:
        violations.extend(check_renderer_file(f))
        warnings.extend(check_selection_contract(f))
    warnings.extend(check_layout_file(root / "02-mod" / "04-layout.config.ts"))
    by_kind: dict[str, int] = {}
    for v in violations:
        by_kind[v["kind"]] = by_kind.get(v["kind"], 0) + 1
    warning_by_kind: dict[str, int] = {}
    for w in warnings:
        warning_by_kind[w["kind"]] = warning_by_kind.get(w["kind"], 0) + 1
    return {
        "ok": len(violations) == 0,
        "scanned_files": len(files),
        "violation_count": len(violations),
        "warning_count": len(warnings),
        "by_kind": by_kind,
        "warning_by_kind": warning_by_kind,
        "violations": violations,
        "warnings": warnings,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="检查 02-mod custom renderer 是否遵守 build-v3 层级/功能区语法")
    ap.add_argument("--json", action="store_true", help="机器可读 JSON 输出")
    args = ap.parse_args()
    res = run()
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(f"扫描 {res['scanned_files']} 个 mod renderer 文件")
        if res["ok"]:
            print("✓ mod renderer 层级/功能区消费无硬违规")
        else:
            print(f"✗ {res['violation_count']} 处硬违规  {res['by_kind']}")
            for v in res["violations"]:
                print(f"  [{v['kind']}] {v['file']}:{v['line']}  {v['detail']}  — {v['hint']}")
        if res["warnings"]:
            print(f"! {res['warning_count']} 条迁移评审提示  {res['warning_by_kind']}")
            for w in res["warnings"]:
                print(f"  [{w['kind']}] {w['file']}:{w['line']}  {w['detail']}  — {w['hint']}")
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
