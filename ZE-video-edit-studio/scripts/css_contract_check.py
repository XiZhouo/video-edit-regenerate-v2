#!/usr/bin/env python3
"""CSS.Contract.Check — 前端(.tsx)与 CSS 库(03-css-rules)契约一致性门禁。

CSS 契约(对齐 skill_sync.py 的 css checksum 词表):
  - `.ze-*` class 词表 = 03-css-rules 里所有 CSS 定义的 class
  - `--ze-*` token 词表 = 03-css-rules 里所有定义的 design token

前端只允许"发这套词汇":className 只能用 css 定义过的 ze-* class;颜色/字号只能走
`var(--ze-*)` token,**不准写死字面量**(hex / rgb)。本检查器静态扫所有 .tsx,报五类违规:

  1. UNDEFINED_CLASS  — className 里的 ze-* class 在 CSS 没定义(会导致样式丢失)。
  2. RAW_HEX          — 内联出现 `#rgb`/`#rrggbb` 字面量(即便带 var fallback;ze 纪律只用 token)。
  3. RAW_COLOR_LITERAL— 内联 style 的 color/background 用 rgb()/hsl() 等非 var 字面量。
  4. UNDEFINED_TOKEN  — JSX 内 `var(--ze-*)` 引用了皮肤里不存在的 token。
  5. NO_REDUNDANT_VISUAL_CONTAINER
       — 视觉容器/功能块的唯一可见子元素也是视觉容器/数据面,且父级没有自己的标题、动作、
         状态、语义分区或多子项。典型反例:`.ze-card > .ze-kv-list`、
         `.ze-editor__content > .ze-editor__unit`。正例:`.ze-editor__zone > button`、
         `.ze-select > select`、`.ze-editor__content[data-content-mode="pass-through"] > .ze-editor__unit`。

**放行**:结构性布局内联(position/display/flex/overflow/gap 数字等)—— 那不是 token 能管的,
合理内联布局,不算违规。

**已知盲区**(门禁不全覆盖,维护者勿误信):
  - 只扫静态 `className="..."` / `{`...`}` / `{"..."}`;动态拼接(三元 / clsx / join)不扫。
  - BEM modifier `ze-X--Y` 只校基类 `ze-X` 在不在,不校 modifier 本身(css 约定 modifier
    不逐个定义)—— 故 element 误写成 modifier(`ze-X--y` 本应 `ze-X__y`)漏报。
  当前 codebase 实测 0 漏报;新增上述写法时需人工复核。

用法:
  python scripts/css_contract_check.py            # 扫描 + 报告;有违规 exit 1(可做部署门禁)
  python scripts/css_contract_check.py --json      # 机器可读输出
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

BUILD_ROOT = Path(__file__).resolve().parents[1]
# CSS 词表来源:03-css-rules(皮肤,含 .ze-app* 外壳类于 04-editors/app-shell.css)。
# webapp/src 保留为根仅作前向兼容(现已无 .css —— 外壳样式已迁入皮肤文件夹)。
CSS_ROOTS = [BUILD_ROOT / "03-css-rules", BUILD_ROOT / "webapp" / "src"]
# 前端扫描范围:core 用户面 + mod 消费层 + webapp 装配壳。
# 02-mod 必须纳入:下游真正写业务 JSX 的地方在 mod,否则 class/容器结构门禁会漏掉消费层违规。
TSX_ROOTS = [BUILD_ROOT / "01-core" / "04-user-facing", BUILD_ROOT / "02-mod", BUILD_ROOT / "webapp" / "src"]

# CSS 词表提取(.ze-* 类名)
_CLS_DEF_RE = re.compile(r"\.ze-[a-z0-9_-]+")
_TOK_DEF_RE = re.compile(r"--ze-[a-z0-9-]+")
# 前端用法
_CLASSNAME_RE = re.compile(r'className=(?:"([^"]*)"|\{`([^`]*)`\}|\{"([^"]*)"\})')
_CLASS_ATTR_RE = re.compile(
    r'className\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|\{`([^`]*)`\}|\{"([^"]*)"\}|\{\'([^\']*)\'\})',
    re.DOTALL,
)
_ZE_WORD_RE = re.compile(r"ze-[a-z0-9_-]+")
_HEX_RE = re.compile(r"#[0-9a-fA-F]{3,8}\b")
# 内联 style 里 color/background 用 rgb()/hsl() 字面量(非 var)
_RAW_FN_COLOR_RE = re.compile(
    r"(?:color|background|backgroundColor|borderColor|fill|stroke)\s*:\s*['\"]?\s*(rgb|rgba|hsl|hsla)\("
)
_JSX_TAG_RE = re.compile(r"<(/?)([A-Za-z][A-Za-z0-9_.]*)\b([^<>]*?)(/?)>", re.DOTALL)
_ATTR_VALUE_RE = r'{name}\s*=\s*(?:"{value}"|\'{value}\'|\{{"{value}"\}}|\{{\'{value}\'\}})'

_VISUAL_CONTAINER_CLASSES = {
    "ze-editor__content",
    "ze-editor__zone",
    "ze-editor__unit",
    "ze-card",
    "ze-row-card",
    "ze-state-frame",
    "ze-thumb",
    "ze-table",
    "ze-status-tile",
}
_VISUAL_CHILD_CLASSES = _VISUAL_CONTAINER_CLASSES | {"ze-kv-list"}
_LAYOUT_CONTAINER_CLASSES = {"ze-stack", "ze-row", "ze-grid", "ze-cluster", "ze-bar"}
_VISUAL_UTILITY_CLASSES = {"ze-edge", "ze-surface", "ze-elevate"}


@dataclass
class _JsxNode:
    tag: str
    attrs: str
    classes: set[str]
    line: int
    children: list["_JsxNode"] = field(default_factory=list)
    has_text: bool = False


def css_allow_sets() -> tuple[set[str], set[str]]:
    """从 CSS_ROOTS 提取 (class 名集, token 名集)。class 名去掉前导点。"""
    classes, tokens = set(), set()
    for root in CSS_ROOTS:
        if not root.is_dir():
            continue
        for css in sorted(root.rglob("*.css")):
            if "node_modules" in css.parts:
                continue
            t = css.read_text(encoding="utf-8")
            classes.update(m.lstrip(".") for m in _CLS_DEF_RE.findall(t))
            tokens.update(_TOK_DEF_RE.findall(t))
    return classes, tokens


def _iter_tsx() -> list[Path]:
    out: list[Path] = []
    for root in TSX_ROOTS:
        if root.is_dir():
            out.extend(p for p in root.rglob("*.tsx") if "node_modules" not in p.parts)
    return sorted(out)


def _strip_block_comments(text: str) -> str:
    """去掉 /* ... */ 注释(注释里的示例 hex / class 不算违规)。"""
    return re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)


def _static_classes(attrs: str) -> set[str]:
    """提取 JSX tag 上静态 className 的 ze-* class;动态表达式不猜。"""
    out: set[str] = set()
    for m in _CLASS_ATTR_RE.finditer(attrs):
        value = next((g for g in m.groups() if g is not None), "")
        out.update(_ZE_WORD_RE.findall(value))
    return out


def _attr_is(attrs: str, name: str, value: str) -> bool:
    return re.search(_ATTR_VALUE_RE.format(name=re.escape(name), value=re.escape(value)), attrs) is not None


def _has_significant_jsx_text(fragment: str) -> bool:
    """判断两个 JSX tag 之间是否有真实文本;纯空白/简单表达式不算父级自有内容。"""
    without_simple_expr = re.sub(r"\{[^{}]*\}", "", fragment, flags=re.DOTALL)
    return bool(without_simple_expr.strip())


def _parse_jsx_tree(text: str) -> _JsxNode:
    root = _JsxNode("ROOT", "", set(), 1)
    stack = [root]
    last = 0
    for m in _JSX_TAG_RE.finditer(text):
        if stack and _has_significant_jsx_text(text[last:m.start()]):
            stack[-1].has_text = True
        closing, tag, attrs, selfclose = m.groups()
        if closing:
            for i in range(len(stack) - 1, 0, -1):
                if stack[i].tag == tag:
                    del stack[i:]
                    break
        else:
            node = _JsxNode(tag, attrs, _static_classes(attrs), text[:m.start()].count("\n") + 1)
            stack[-1].children.append(node)
            if not selfclose and not attrs.rstrip().endswith("/"):
                stack.append(node)
        last = m.end()
    return root


def _is_visual_container(classes: set[str], attrs: str) -> bool:
    if "ze-editor__content" in classes and _attr_is(attrs, "data-content-mode", "pass-through"):
        return False
    if "ze-status-tile" in classes and _attr_is(attrs, "data-variant", "inline"):
        return False
    if classes & _VISUAL_CONTAINER_CLASSES:
        return True
    if (classes & _LAYOUT_CONTAINER_CLASSES) and (
        classes & _VISUAL_UTILITY_CLASSES
        or _attr_is(attrs, "data-surface", "raised")
        or _attr_is(attrs, "data-surface", "sunken")
        or _attr_is(attrs, "data-surface", "canvas")
    ):
        return True
    return False


def _is_visual_child(classes: set[str], attrs: str) -> bool:
    if "ze-status-tile" in classes and _attr_is(attrs, "data-variant", "inline"):
        return False
    return bool(classes & _VISUAL_CHILD_CLASSES)


def _class_label(classes: set[str]) -> str:
    relevant = sorted(classes & (_VISUAL_CHILD_CLASSES | _LAYOUT_CONTAINER_CLASSES | _VISUAL_UTILITY_CLASSES))
    return "." + ".".join(relevant) if relevant else "(unknown)"


def _check_redundant_visual_containers(path: Path, raw: str) -> list[dict]:
    """扫静态 JSX 树:视觉容器只有一个视觉子容器且没有自有文本/多子项 → 冗余壳。"""
    root = _parse_jsx_tree(_strip_block_comments(raw))
    violations: list[dict] = []

    def walk(node: _JsxNode) -> None:
        if _is_visual_container(node.classes, node.attrs) and len(node.children) == 1 and not node.has_text:
            child = node.children[0]
            if _is_visual_child(child.classes, child.attrs):
                violations.append({
                    "file": _rel(path), "line": node.line,
                    "kind": "NO_REDUNDANT_VISUAL_CONTAINER",
                    "detail": f"{_class_label(node.classes)} > {_class_label(child.classes)}",
                    "hint": "父级视觉容器只有一个视觉子容器/数据面且无自有语义;合并、改为 pass-through 或删掉外壳",
                })
        for child in node.children:
            walk(child)

    walk(root)
    return violations


def _strip_line_comment(line: str) -> str:
    """剥掉行尾 `//` 注释(粗略:不在字符串字面量内的第一个 //)。

    避免 `const x = ... // 旧值 #ff0000` 里注释的 hex 被误报。简化处理:逐字符扫,
    记录是否在 ' " ` 引号内,遇到引号外的 // 即截断。
    """
    quote = ""
    j = 0
    while j < len(line) - 1:
        c = line[j]
        if quote:
            if c == quote:
                quote = ""
            elif c == "\\":
                j += 1  # 跳过转义字符
        elif c in "'\"`":
            quote = c
        elif c == "/" and line[j + 1] == "/":
            return line[:j]
        j += 1
    return line


def _rel(path: Path) -> str:
    """相对 BUILD_ROOT 的显示路径;在外(如测试 tmp)则回退到文件名。"""
    try:
        return str(path.relative_to(BUILD_ROOT))
    except ValueError:
        return path.name


def check_file(path: Path, classes: set[str], tokens: set[str]) -> list[dict]:
    """扫一个 tsx,返回违规列表。"""
    raw = path.read_text(encoding="utf-8")
    violations: list[dict] = []
    lines = raw.splitlines()

    for i, line in enumerate(lines, 1):
        code = _strip_block_comments(line)
        if code.lstrip().startswith("//") or code.lstrip().startswith("*"):
            continue  # 整行注释
        code = _strip_line_comment(code)   # 剥行尾 // 注释(避免注释里的示例 hex 误报)

        # 1. className 里的 ze-* class 是否都定义。
        #    放行 BEM modifier(`ze-X--Y`):css 约定 modifier 不逐个定义(变体靠父类 +
        #    data-* 属性驱动,如 .ze-editor + [data-editor-class]),`--` 后只是标记钩子。
        #    校验时剥掉 `--modifier` 取 block/element 基类(`ze-X` 或 `ze-X__Y`)判定义。
        for m in _CLASSNAME_RE.finditer(code):
            value = next(g for g in m.groups() if g is not None)
            for word in _ZE_WORD_RE.findall(value):
                base = word.split("--", 1)[0]      # 剥 BEM modifier
                if base not in classes:
                    violations.append({
                        "file": _rel(path), "line": i,
                        "kind": "UNDEFINED_CLASS", "detail": word,
                        "hint": "className 用了 CSS 未定义的 class(样式会丢失)",
                    })

        # 2. 裸 hex 颜色字面量(含 var fallback 里的)
        for hx in _HEX_RE.findall(code):
            violations.append({
                "file": _rel(path), "line": i,
                "kind": "RAW_HEX", "detail": hx,
                "hint": "颜色写死字面量;改用 var(--ze-* token)",
            })

        # 3. color/background 用 rgb()/hsl() 字面量
        for m in _RAW_FN_COLOR_RE.finditer(code):
            violations.append({
                "file": _rel(path), "line": i,
                "kind": "RAW_COLOR_LITERAL", "detail": m.group(1) + "(...)",
                "hint": "颜色用函数字面量;改用 var(--ze-* token)",
            })

        # 顺带:var(--ze-xxx) 引用了 CSS 未定义的 token(用错 token 名 = 渲染失败)
        for tok in re.findall(r"var\((--ze-[a-z0-9-]+)", code):
            if tok not in tokens:
                violations.append({
                    "file": _rel(path), "line": i,
                    "kind": "UNDEFINED_TOKEN", "detail": tok,
                    "hint": "var() 引用了 CSS 未定义的 token(颜色/尺寸不生效)",
                })

    violations.extend(_check_redundant_visual_containers(path, raw))
    return violations


def run() -> dict:
    classes, tokens = css_allow_sets()
    all_violations: list[dict] = []
    files = _iter_tsx()
    for f in files:
        all_violations.extend(check_file(f, classes, tokens))
    by_kind: dict[str, int] = {}
    for v in all_violations:
        by_kind[v["kind"]] = by_kind.get(v["kind"], 0) + 1
    return {
        "ok": len(all_violations) == 0,
        "scanned_files": len(files),
        "css_classes": len(classes), "css_tokens": len(tokens),
        "violation_count": len(all_violations),
        "by_kind": by_kind,
        "violations": all_violations,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="前端 .tsx 与 CSS 库契约一致性检查")
    ap.add_argument("--json", action="store_true", help="机器可读 JSON 输出")
    args = ap.parse_args()
    res = run()
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(f"扫描 {res['scanned_files']} 个 tsx;CSS 词表 {res['css_classes']} class / "
              f"{res['css_tokens']} token")
        if res["ok"]:
            print("✓ 前端-CSS 契约一致,无违规")
        else:
            print(f"✗ {res['violation_count']} 处违规  {res['by_kind']}")
            for v in res["violations"]:
                print(f"  [{v['kind']}] {v['file']}:{v['line']}  {v['detail']}  — {v['hint']}")
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
