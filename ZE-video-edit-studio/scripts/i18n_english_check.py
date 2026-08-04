#!/usr/bin/env python3
"""I18N.English.Check — UI **chrome** 区零裸英文(对位 i18n_contract 的反向门禁)。

i18n_contract_check 守一向(UI 代码零裸 **中文**,逼 zh 进字典),但裸 **英文** 不触发它 →
footer 这类整段写死英文的 chrome 能悄悄漏网(本轮起因)。本门禁补另一向:**chrome 字串必须
走 `t('ns.key')`,裸英文同样违规**。两向合起来 = 任何语言的硬编码 UI 文案都被守。

**规则**(与 README『校验标准·双向门禁』一致):
  扫描 SCAN_ROOTS 下的 .tsx,在剥注释后查两类**字面量**:
    1. JSX 文本节点 `>…<`(节点内不含 `{}` 插值 → 是纯字面量);
    2. `title=` / `aria-label=` / `placeholder=` 三个面向用户的属性的字面量值
       (`"…"` / `'…'` / `{'…'}`);`={t(...)}`、`={变量}` 等表达式不算字面量,放行。
  字面量里出现连续 ≥2 个 ASCII 字母且未被 allowlist 命中 → 违规,应改走 `t('ns.key')`。

**扫整个 `01-core/04-user-facing`(含 editors)**:旧版只扫 header/footer/auth、漏掉 editors 的
chrome(列头 / 空态 / aria-label)。data 标识(entity id / 字段 key / entity.name)走 `{绑定}`
而非字面量,本门禁只查字面量,故天然不碰 data;editors 里唯一的噪声是 TS 泛型(`): Record<`)被
误读成文本节点 —— 由 `_TS_SHAPE_RE` 收敛(只跳识别出 TS 形状的「文本节点」,含标点的真文案仍捕获)。已实测:扫 111 文件
仅 3 处真漏(editors 的 missing/hint 标签 + 一个裸 aria-label),零泛型误报。

**allowlist**:语言中立的单位 / 缩写 / 符号(ms、px、id、3d、% 等)。几何符号(▶ ⚠ — # ↶ ↷
⏸ ↻ 等)非 ASCII 字母,天然放行。新增 chrome 组件默认即被本门禁守(无需登记)。

**已知盲区**(同 css/ops/i18n_contract 门禁):只剥 `//`+`/* */` 注释后扫;正则识别 JSX 文本节点
与属性字面量,不建完整 AST —— 罕见的复杂泛型 / 三元里的 `>…<` 可能误报,落进 allowlist 收敛。

用法:
  python scripts/i18n_english_check.py          # 有裸英文 exit 1(部署门禁)
  python scripts/i18n_english_check.py --json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

BUILD_ROOT = Path(__file__).resolve().parents[1]

# 扫整个框架 user-facing 树(menu / 状态条 / 登录 / editors / 偏好)。data 走 {绑定} 不入字面量,
# 故扫 editors 安全;TS 泛型噪声由 _TS_SHAPE_RE 收敛。业务串在 02-mod(mod.* 词表),不在本门禁范围。
SCAN_ROOTS = [
    "01-core/04-user-facing",
]

# 未迁移的历史文件(收缩式豁免;每迁一个删一行,迁完为空 → 全量生效)。
# 随 toolchain 同步层下发;Skill.Sync 整文件覆盖会重置为 []。
LEGACY_EXEMPT: list[str] = []

# 语言中立的单位 / 缩写(小写比对)。单字母已被「≥2 字母」规则天然放行,无需列。
ALLOWLIST = {
    "ms", "px", "em", "rem", "id", "url", "uri", "3d", "2d", "ai",
    "json", "zip", "css", "html", "svg", "sam", "ok", "kb", "mb", "gb",
}

# 连续 ASCII 字母片段(用于把字面量拆成「词」,逐词比对 allowlist)
_WORD_RE = re.compile(r"[A-Za-z]{2,}")

# snake_case 代码标识符(含至少一个下划线):doc_id / trace_id / pending_op_count …(数据边界,豁免)
_IDENT_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+")

# JSX 文本节点:`>…<`,节点内不含尖括号 / 花括号 → 纯字面量(含 `{` 即有插值,不算字面量)
_TEXT_NODE_RE = re.compile(r">([^<>{}]+)<")

# TS 形状识别:把「被误读成 JSX 文本节点的 TS 泛型 / 表达式」与「含标点的真 prose」区分开。
# 只在出现明确代码形状时跳过:箭头 `=>` / 逻辑 `&&`·`||` / `): 类型` / `名: 类型`标注 / `.方法(`。
# 单纯含 `&` `()` `[]` 的人类文案(`Save & Exit` / `Undo (⌘Z)` / `Filter by [type]`)**不**跳过,
# 仍被门禁捕获 —— 避免「blunt 标点过滤」在 text-node 路径留下漏检盲区(code review HIGH)。
_TS_SHAPE_RE = re.compile(r"=>|&&|\|\||\)\s*:|[a-z_]\w*\s*:\s*[A-Za-z]|\.\w+\(")

# 面向用户的属性 = 字符串字面量(双 / 单引号)
_ATTR_STR_RE = re.compile(r"""\b(?:title|aria-label|placeholder)\s*=\s*("[^"]*"|'[^']*')""")
# 面向用户的属性 = `{ '…' }` / `{ "…" }`(花括号里直接裹引号 = 字面量;`{t(...)}`/`{变量}` 不匹配)
_ATTR_BRACE_STR_RE = re.compile(
    r"""\b(?:title|aria-label|placeholder)\s*=\s*\{\s*("[^"]*"|'[^']*')\s*\}"""
)


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(BUILD_ROOT))
    except ValueError:
        return path.name


def _strip_block_comments(text: str) -> str:
    # 用等行数换行替换块注释,**保留行号**:否则多行 /* */ 被吃掉会让后续违规行号整体上移
    # (扫 editors 这类多块注释文件时,报告的「变更位置」会错位上百行)。
    return re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.DOTALL)


def _strip_line_comment(line: str) -> str:
    """剥行尾 `//` 注释(引号外的第一个 //)。"""
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


def _has_unallowed_english(literal: str) -> bool:
    """字面量里有连续 ≥2 ASCII 字母、且至少一个词不在 allowlist → 真违规。

    先剔除 snake_case 代码标识符(如 `doc_id` / `trace_id`):带下划线的 token 是数据 / 标识符
    (按 README『chrome vs 数据 边界』属数据,literally 透传、两语言一致,如 zh 字典里的 '新 doc_id'),
    不是可译 chrome 文案;留下的才是 prose 英文。
    """
    cleaned = _IDENT_RE.sub(" ", literal)
    words = _WORD_RE.findall(cleaned)
    if not words:
        return False
    return any(w.lower() not in ALLOWLIST for w in words)


def check_file(path: Path) -> list:
    violations = []
    text = _strip_block_comments(path.read_text(encoding="utf-8"))
    for i, line in enumerate(text.splitlines(), 1):
        stripped = line.lstrip()
        if stripped.startswith("//") or stripped.startswith("*"):
            continue
        code = _strip_line_comment(line)

        for m in _TEXT_NODE_RE.finditer(code):
            # 开启 `>` 必须是 JSX 标签收尾,而非箭头 `=>` 或泛型/比较符 —— 否则 `=> Promise<void>`、
            # `Map<A, B>(x)` 这类纯 TS 会被误当文本节点。标签收尾前一字符是字母/数字/引号/`}`/`/`/`"`。
            prev = code[m.start() - 1] if m.start() > 0 else ""
            if prev in "=<>!&|+-*/%?:,;({[ \t":
                continue
            literal = m.group(1)
            # TS 泛型/表达式收敛:只在识别出明确 TS 代码形状时跳过(`): Record<`、`id: string`、`=>`…);
            # 含标点的真 prose 不跳过,仍捕获(attr 路径本就高精度、不受影响)。见 _TS_SHAPE_RE。
            if _TS_SHAPE_RE.search(literal):
                continue
            if _has_unallowed_english(literal):
                violations.append({
                    "file": _rel(path), "line": i, "kind": "text",
                    "snippet": literal.strip()[:60],
                    "hint": "JSX 文本出现裸英文;请改走 t('ns.key')(key 在 05-i18n/zh.ts)",
                })

        for rx in (_ATTR_STR_RE, _ATTR_BRACE_STR_RE):
            for m in rx.finditer(code):
                literal = m.group(1)[1:-1]  # 去引号
                if _has_unallowed_english(literal):
                    violations.append({
                        "file": _rel(path), "line": i, "kind": "attr",
                        "snippet": m.group(0).strip()[:60],
                        "hint": "title/aria-label/placeholder 出现裸英文;请改走 t('ns.key')",
                    })
    return violations


def _iter_targets():
    for root in SCAN_ROOTS:
        base = BUILD_ROOT / root
        if not base.exists():
            continue
        for p in sorted(base.rglob("*.tsx")):
            if "__pycache__" in p.parts or "node_modules" in p.parts:
                continue
            yield _rel(p), p


def run() -> dict:
    violations = []
    checked = []
    legacy = set(LEGACY_EXEMPT)
    legacy_skipped = []
    for rel, p in _iter_targets():
        if rel in legacy:
            legacy_skipped.append(rel)
            continue
        checked.append(rel)
        violations.extend(check_file(p))
    return {
        "ok": len(violations) == 0,
        "scanned_files": len(checked),
        "legacy_skipped": len(legacy_skipped),
        "violation_count": len(violations),
        "violations": violations,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="UI chrome 裸英文门禁(扫整个 01-core/04-user-facing)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    res = run()
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        legacy = f"(+{res['legacy_skipped']} 历史豁免)" if res["legacy_skipped"] else ""
        print(f"扫描 {res['scanned_files']} 个 chrome 文件 {legacy}")
        if res["ok"]:
            print("✓ chrome 区零裸英文,i18n 双向契约一致")
        else:
            print(f"✗ {res['violation_count']} 处裸英文")
            for v in res["violations"]:
                print(f"  {v['file']}:{v['line']}  [{v['kind']}]  {v['snippet']}  — {v['hint']}")
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
