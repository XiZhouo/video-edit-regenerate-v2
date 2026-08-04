#!/usr/bin/env python3
"""I18N.Contract.Check — UI 代码区**默认**零裸中文(字串全走 t()/05-i18n 字典)。

第三姊妹门禁(css_contract_check"看" / ops_contract_check"配" / 本"说"):
  - css:JSX className 必须在 css 词表定义
  - ops:core 读的 env 必须在 config_schema 声明
  - i18n:UI 代码区必须零 CJK 字面量(字串全走 `t('ns.key')`,CJK 只活在 05-i18n 字典)

**默认全扫策略**(反转自旧 opt-in 白名单):扫 SCAN_ROOTS 下所有 .ts/.tsx,代码区(剥注释后)
零裸 CJK —— **新写的组件默认就被守**,不必记得登记。还没迁的历史文件列进 LEGACY_EXEMPT
(收缩式:每迁完一个删一行,清空即全量生效)。

**豁免**(EXEMPT_PREFIXES):
  - 词表文件(05-i18n/*.ts、02-mod/09-i18n):CJK 的合法家(译文)。
  - mod 业务 seed 数据(02-mod/04-layout.config):新建实体的默认 name 会写进 Document 成为
    **实体数据**(非 UI chrome),按「Document≠UI」边界不翻译。

**已知盲区**(同 css/ops 门禁):只剥 `//`+`/* */`(含 JSX `{/* */}`)注释后扫 CJK;字符串里
含 `//` 的边角可能误剥(罕见)。几何符号(◐ ⚙ ✓ …)非 CJK 范围,允许。

用法:
  python scripts/i18n_contract_check.py          # 有裸 CJK exit 1(可做部署门禁)
  python scripts/i18n_contract_check.py --json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

BUILD_ROOT = Path(__file__).resolve().parents[1]

# 默认全扫这些根下的 .ts/.tsx(框架 UI + webapp + mod 自定义 UI 代码)。
SCAN_ROOTS = [
    "01-core/04-user-facing",
    "webapp/src",
    "02-mod/05-code",
]

# 未迁移的历史文件(收缩式豁免;每迁一个删一行,迁完为空 → 全量生效)。
# 注:本文件随 toolchain 同步层下发;Skill.Sync 会整文件覆盖,把 LEGACY_EXEMPT 重置为 []。
# 从 v3 scaffold 出生的 skill 是全量迁移的(无历史包袱),故 [] 正确;只有把 v3 retrofit 到旧 skill
# 才需在 sync 后重新补回本地豁免项(属一次性迁移工作,不是常态)。
LEGACY_EXEMPT: list[str] = []

# CJK 合法家:词表文件 + mod 业务 seed 数据(写进 Document 的实体默认名,非 UI)+ 交互式 gallery
# demo harness(webapp/src/gallery:验证/展示用样本数据 + chrome,不是产品可译 UI,隔离于 core)。
# 04-layout.config 不再整文件豁免:改由 check_layout_config 专项扫 action chrome 键(C1)。
# name(实体默认名=Document 数据)与 newEntityDefaults 区的 CJK 仍放行;gallery demo harness 仍豁免。
EXEMPT_PREFIXES = ["05-i18n/", "02-mod/09-i18n", "webapp/src/gallery/"]

# CJK 汉字 + CJK/全角标点(几何符号 ◐⚙✓ 等不在内,允许作语言中立符号)
_CJK_RE = re.compile(r"[㐀-鿿　-〿＀-￯]")

# C1:action chrome 精确键 —— 其值含 CJK = 裸中文 chrome,必须改走 *Key 可译通路(labelKey/confirmKey…)。
# 负向后顾 (?<![A-Za-z]) 保证只命中精确键名:labelField:/titleKey:/labelKey:/confirmKey: 等不误中;
# name:(实体默认名=Document 数据)与 newEntityDefaults 区不在键集内 → CJK 放行。
_ACTION_CHROME_KEYS = ("label", "confirm", "title", "actionLabel", "inputLabel", "outputLabel")
_ACTION_CHROME_RE = re.compile(
    r"(?<![A-Za-z])(" + "|".join(_ACTION_CHROME_KEYS) + r")\s*:\s*(['\"`])([^'\"`]*?)\2"
)
# 专项扫描文件(非 SCAN_ROOT;只查 action chrome 键的 CJK 值)。
LAYOUT_CONFIG_REL = "02-mod/04-layout.config.ts"


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(BUILD_ROOT))
    except ValueError:
        return path.name


def _is_exempt(rel: str) -> bool:
    return any(rel.startswith(p) for p in EXEMPT_PREFIXES)


def _strip_block_comments(text: str) -> str:
    """去 /* ... */(含 JSX {/* */});注释里的 CJK 不算违规。"""
    return re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)


def _strip_line_comment(line: str) -> str:
    """剥行尾 `//` 注释(引号外的第一个 //);避免注释里的 CJK 误报。"""
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


def check_file(path: Path) -> list:
    """扫一个文件,返回代码区(剥注释后)的裸 CJK 违规。"""
    violations = []
    text = _strip_block_comments(path.read_text(encoding="utf-8"))
    for i, line in enumerate(text.splitlines(), 1):
        stripped = line.lstrip()
        if stripped.startswith("//") or stripped.startswith("*"):
            continue  # 整行注释 / 块注释残行
        code = _strip_line_comment(line)
        m = _CJK_RE.search(code)
        if m:
            snippet = code.strip()[:60]
            violations.append({
                "file": _rel(path), "line": i, "char": m.group(0), "snippet": snippet,
                "hint": "UI 代码出现裸中文;请改走 t('ns.key')(key 在 05-i18n/zh.ts)",
            })
    return violations


def check_layout_config(path: Path) -> list:
    """C1:02-mod/04-layout.config.ts —— 只查 action chrome 精确键(label/confirm/title/
    actionLabel/inputLabel/outputLabel)的 CJK 字面量值 → 违规(改 *Key + mod.* 词表)。
    newEntityDefaults 区(实体默认 name/data 字段=写进 Document 的实体数据,非 UI chrome)整块跳过;
    skillName 常量(=产品名 fallback,真正显示走 mod.product.name)不在键集内,CJK 放行。"""
    violations = []
    text = _strip_block_comments(path.read_text(encoding="utf-8"))
    in_defaults = False  # newEntityDefaults 块内(按花括号深度跟踪)
    depth = 0
    for i, line in enumerate(text.splitlines(), 1):
        stripped = line.lstrip()
        if stripped.startswith("//") or stripped.startswith("*"):
            continue
        code = _strip_line_comment(line)
        if not in_defaults and re.search(r"\bnewEntityDefaults\b", code):
            in_defaults = True
            depth = 0
        if in_defaults:
            depth += code.count("{") - code.count("}")
            if depth <= 0:
                in_defaults = False
            continue  # 实体默认数据区:title/name/label 等是 Document 数据,CJK 放行
        for m in _ACTION_CHROME_RE.finditer(code):
            if _CJK_RE.search(m.group(3)):
                violations.append({
                    "file": _rel(path), "line": i, "char": m.group(1),
                    "snippet": code.strip()[:60],
                    "hint": f"动作 chrome 键 {m.group(1)}: 含裸中文;改走 {m.group(1)}Key + mod.* 词表",
                })
    return violations


def _iter_targets():
    """SCAN_ROOTS 下所有 .ts/.tsx,跳过豁免前缀。"""
    for root in SCAN_ROOTS:
        base = BUILD_ROOT / root
        if not base.exists():
            continue
        for p in sorted(base.rglob("*")):
            if p.suffix not in (".ts", ".tsx"):
                continue
            if "__pycache__" in p.parts or "node_modules" in p.parts:
                continue
            rel = _rel(p)
            if _is_exempt(rel):
                continue
            yield rel, p


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
    # C1:专项扫 04-layout.config.ts 的 action chrome 键(非 SCAN_ROOT,单独纳入)。
    lc = BUILD_ROOT / LAYOUT_CONFIG_REL
    if lc.exists() and LAYOUT_CONFIG_REL not in legacy:
        checked.append(LAYOUT_CONFIG_REL)
        violations.extend(check_layout_config(lc))
    return {
        "ok": len(violations) == 0,
        "scanned_files": len(checked),
        "legacy_skipped": len(legacy_skipped),
        "violation_count": len(violations),
        "violations": violations,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="UI 代码裸中文门禁(默认全扫)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    res = run()
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        legacy = f"(+{res['legacy_skipped']} 历史豁免)" if res["legacy_skipped"] else ""
        print(f"扫描 {res['scanned_files']} 个 UI 文件 {legacy}")
        if res["ok"]:
            print("✓ UI 代码零裸中文,i18n 契约一致")
        else:
            print(f"✗ {res['violation_count']} 处裸中文")
            for v in res["violations"]:
                print(f"  {v['file']}:{v['line']}  「{v['char']}」  {v['snippet']}  — {v['hint']}")
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
