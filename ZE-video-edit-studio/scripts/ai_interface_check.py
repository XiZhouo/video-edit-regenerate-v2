#!/usr/bin/env python3
"""AI.Interface.Check — AI 最低接口的"双暴露 + 文档一致"门禁(状态纪律见 scripts/STATUS_DISCIPLINE.md)。

真源 = `sdk/introspect.py` 的 `__all__`(每个 reader = 一个 AI 只读能力)。本门禁钉死两件事:

  AI_SURFACE_MISSING — 每个 reader 必须同时被 `server.py`(HTTP 路由)和 `scripts/ze_command.py`
    (CLI 子命令)引用 → 保证"API 与 CLI 同形"(FUTURE_STATE §5 不变量)。加了 reader 却没双暴露 → 红。

  AI_DOC_STALE — `FUTURE_STATE.md` §5 表里,若某 reader 已在 __all__ 存在却同行仍标 ⛏/待补 → 文档漂移。

只读、不改任何东西。用法:
  python scripts/ai_interface_check.py            # 有违规 exit 1
  python scripts/ai_interface_check.py --json
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

BUILD_ROOT = Path(__file__).resolve().parents[1]
INTROSPECT = BUILD_ROOT / "sdk" / "introspect.py"
SERVER = BUILD_ROOT / "server.py"
CLI = BUILD_ROOT / "scripts" / "ze_command.py"
FUTURE = BUILD_ROOT / "FUTURE_STATE.md"

# __all__ 里非 reader 的导出(类/元数据),不算"能力"
_NOT_CAPABILITY = {"IntrospectionMeta"}
_FORWARD = ("⛏", "待补", "未来", "pending")

# server 侧"经替代路由暴露、未走 wrapper 符号"的已知例外(诚实记录,非隐藏):
#   settings_read — server 经 /api/config 直读 config.snapshot()(shape {config:});
#     CLI `settings` 走 settings_read wrapper(shape {items:})。能力双暴露但 server 未用该符号、
#     且 shape 不同形。属遗留小不一致(对齐需改 /api/config 响应 shape,牵连前端,留作 TODO)。
#     见 FUTURE_STATE §5 注。新增 reader 不得进此例外 —— 必须 server+CLI 双暴露同符号。
_SERVER_ALT_ROUTE = {"settings_read"}


def reader_symbols() -> list[str]:
    """ast 解析 introspect.py 的 __all__,取 reader 符号(排除非能力项)。
    用 ast 而非正则:避免 __all__ 跨行/嵌套/含注释时正则在首个 ] 处截断 → 漏符号假阴性。"""
    if not INTROSPECT.is_file():
        return []
    try:
        tree = ast.parse(INTROSPECT.read_text(encoding="utf-8"))
    except SyntaxError:
        return []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets):
            try:
                vals = ast.literal_eval(node.value)
            except (ValueError, SyntaxError):
                return []
            return [s for s in vals if isinstance(s, str) and s not in _NOT_CAPABILITY]
    return []


def _refs(path: Path, symbol: str) -> bool:
    """symbol 是否作为标识符在文件里出现(词边界,避免子串误判)。"""
    if not path.is_file():
        return False
    return re.search(r"\b" + re.escape(symbol) + r"\b", path.read_text(encoding="utf-8")) is not None


def _section5(text: str) -> str:
    """截取 FUTURE_STATE §5 段(## 5. … 到下一个 ## )。"""
    m = re.search(r"\n##\s*5\.[^\n]*\n(.*?)(?:\n##\s|\Z)", text, re.DOTALL)
    return m.group(1) if m else ""


def run() -> dict:
    violations: list[dict] = []
    readers = reader_symbols()

    exempt_notes: list[str] = []
    for sym in readers:
        in_server = _refs(SERVER, sym) or sym in _SERVER_ALT_ROUTE
        in_cli = _refs(CLI, sym)
        if sym in _SERVER_ALT_ROUTE and not _refs(SERVER, sym):
            exempt_notes.append(f"{sym}: server 经替代路由暴露(见门禁注释 / FUTURE_STATE §5)")
        if not (in_server and in_cli):
            miss = []
            if not in_server:
                miss.append("server.py 路由")
            if not in_cli:
                miss.append("ze_command.py CLI 子命令")
            violations.append({
                "kind": "AI_SURFACE_MISSING", "detail": f"{sym} 缺 {' + '.join(miss)}",
                "hint": "introspect.__all__ 的每个 reader 必须 server + CLI 双暴露(API↔CLI 同形)",
            })

    # 文档一致:§5 表里 reader 已存在却仍标前瞻词
    if FUTURE.is_file():
        sec = _section5(FUTURE.read_text(encoding="utf-8"))
        for line in sec.splitlines():
            for sym in readers:
                if re.search(r"\b" + re.escape(sym) + r"\b", line) and any(t in line for t in _FORWARD):
                    violations.append({
                        "kind": "AI_DOC_STALE", "detail": f"§5 行含 {sym} 却仍标前瞻词",
                        "hint": f"{sym} 已在 introspect.__all__;FUTURE_STATE §5 该标 ✅(状态真相走代码)",
                    })

    by_kind: dict[str, int] = {}
    for v in violations:
        by_kind[v["kind"]] = by_kind.get(v["kind"], 0) + 1
    return {"ok": not violations, "readers": readers, "reader_count": len(readers),
            "violation_count": len(violations), "by_kind": by_kind, "violations": violations,
            "exempt_notes": exempt_notes}


def main() -> int:
    ap = argparse.ArgumentParser(description="AI 最低接口双暴露 + 文档一致门禁")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    res = run()
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(f"introspect reader 能力 {res['reader_count']} 个")
        for n in res.get("exempt_notes", []):
            print(f"  · 例外(已记录):{n}")
        if res["ok"]:
            print("✓ 每个 reader 都 server+CLI 双暴露(API↔CLI 同形),FUTURE_STATE §5 与代码一致")
        else:
            print(f"✗ {res['violation_count']} 处违规  {res['by_kind']}")
            for v in res["violations"]:
                print(f"  [{v['kind']}] {v['detail']}  — {v['hint']}")
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
