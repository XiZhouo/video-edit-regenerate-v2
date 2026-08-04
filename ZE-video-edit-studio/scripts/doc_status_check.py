#!/usr/bin/env python3
"""Doc.Status.Check — 防文档状态漂移哨兵(状态纪律见 scripts/STATUS_DISCIPLINE.md)。

完成状态的真相在机器可读源(EDITOR_MANIFEST.json / introspect.__all__ / _contract.json);
prose 一旦把"已建成"的能力写成"未来/待补",文档就成了"乱"的来源。本哨兵把可锚定的那部分钉成红绿:

  硬失败 STALE_EDITOR_STATUS — `EDITOR_MANIFEST.json` 里 status=="adopted" 的 editor class,
    若在任一 SPEC 的**表格行**里被冠以前瞻词(待补/未来/pending/⛏/🟡/null 占位)→ 违规
    (adopted 的编辑器不该被说成没做)。只查表格行 + 反引号类名 + 前瞻词三者同行,保守、低误报。

  报告 FORWARD_TOKENS(非致命,仿 dead_atom_report)— 扫全 skill .md 的前瞻状态词,列 file:line,
    供每次 verify 时人复核"这条是真未来(by-design)还是漂移"。不影响 exit code。

用法:
  python scripts/doc_status_check.py            # 有硬失败 exit 1
  python scripts/doc_status_check.py --json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

BUILD_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = BUILD_ROOT / "03-css-rules" / "EDITOR_MANIFEST.json"
_SKIP = {"node_modules", "dist", "build", ".venv", "venv", ".data", ".git", "__pycache__"}

# 严格"未做"词(硬失败用):明确表示能力没做。不含 "未接"/"styled-unwired"(那是合法的精确细分)。
_FORWARD_STRICT = ("待补", "未来", "pending", "⛏", "🟡", "null 占位", "尚未实现", "未建")
# 宽口径前瞻词(仅报告用):供人复核漂移候选
_FORWARD_LOOSE = _FORWARD_STRICT + ("TODO", "styled-unwired", "styled, unwired",
                                    "无驱动", "not implemented", "未接", "stub", "待接", "待 mod")
# "已做"标记:同行有它 → 该行的前瞻词多是子功能注脚(如 "✅ 真(图层待补)"),不判编辑器未做,降误报
_DONE_MARK = ("✅", "已建", "adopted", "已接", "已落地")


def _iter_md(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.md") if not (_SKIP & set(p.parts)))


def _adopted_classes() -> list[str]:
    if not MANIFEST.is_file():
        return []
    m = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return [e["class"] for e in m.get("editors", []) if e.get("status") == "adopted" and e.get("class")]


def stale_editor_in_line(line: str, adopted: list[str]) -> str | None:
    """纯函数(可测):该行是否在表格里把某 adopted 编辑器(反引号类名)冠以严格未做词。
    三者必须同行:① 表格行(strip 后以 | 开头)② 反引号包裹的类名 ③ 严格未做词。否则 None。"""
    s = line.strip()
    if not s.startswith("|") or not any(t in line for t in _FORWARD_STRICT):
        return None
    if any(d in line for d in _DONE_MARK):  # 同行已有"已做"标记 → 前瞻词系子功能注脚,不误判
        return None
    for c in adopted:
        if re.search(r"`" + re.escape(c) + r"`", line):
            return c
    return None


def first_forward_token(line: str) -> str | None:
    """该行命中的第一个宽口径前瞻词(报告用)。"""
    for tok in _FORWARD_LOOSE:
        if tok in line:
            return tok
    return None


def run() -> dict:
    violations: list[dict] = []
    report: list[dict] = []
    adopted = _adopted_classes()

    for p in _iter_md(BUILD_ROOT):
        rel = str(p.relative_to(BUILD_ROOT))
        for i, line in enumerate(p.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            tok = first_forward_token(line)
            if tok:
                report.append({"file": rel, "line": i, "token": tok, "text": line.strip()[:120]})
            c = stale_editor_in_line(line, adopted)
            if c:
                violations.append({
                    "file": rel, "line": i, "kind": "STALE_EDITOR_STATUS",
                    "detail": f"`{c}` 在表格行被标前瞻词", "text": line.strip()[:140],
                    "hint": f"{c} 在 EDITOR_MANIFEST 是 adopted;别在 SPEC 表里说成未做(状态真相走 manifest)",
                })
    by_kind: dict[str, int] = {}
    for v in violations:
        by_kind[v["kind"]] = by_kind.get(v["kind"], 0) + 1
    return {"ok": not violations, "adopted": adopted, "violation_count": len(violations),
            "by_kind": by_kind, "violations": violations,
            "report_count": len(report), "report": report}


def main() -> int:
    ap = argparse.ArgumentParser(description="文档状态漂移哨兵")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--report", action="store_true", help="打印前瞻词复核清单")
    args = ap.parse_args()
    res = run()
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res["ok"] else 1
    print(f"adopted 编辑器 {len(res['adopted'])};SPEC 表格状态硬校验")
    if res["ok"]:
        print("✓ 无 adopted 编辑器在 SPEC 表里被标未做(机器源=manifest 与 prose 一致)")
    else:
        print(f"✗ {res['violation_count']} 处 STALE_EDITOR_STATUS")
        for v in res["violations"]:
            print(f"  [{v['kind']}] {v['file']}:{v['line']}  {v['detail']}  — {v['hint']}")
    if args.report:
        print(f"\n前瞻词复核清单({res['report_count']} 条,人判断真未来 vs 漂移):")
        for r in res["report"]:
            print(f"  {r['file']}:{r['line']}  [{r['token']}]  {r['text']}")
    else:
        print(f"(前瞻词复核清单 {res['report_count']} 条,--report 查看)")
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
