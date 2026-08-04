#!/usr/bin/env python3
"""topology_doctor.py — 项目拓扑身份医生(诊断 + 显式修复)。

背景(2026-07-20 根治):项目目录要求四样身份一致 —— 目录名 == document.json 的 durable id
== 目录内 marker(.ze-project.json)的 project_id == 目录账本行(.ze-topology/state/<sha256>.json)
的 project_id。用 raw copy/改名搬运项目会打破一致性,旧核心启动对账遇到即冻结
(recovery_required/adopt-mismatch),UI 只见「requires topology repair」死胡同。

新核心(core >= 3.62.0)启动会自动自愈「记录残缺/矛盾但文档完好且 id 相符」的情况;
本脚本处理自愈救不了的病例 —— 主要是**目录被改名**(doc id != 目录名),需要显式决定
「以目录名为准」重写 doc id。修复策略对任何核心版本兼容:备份 → 对齐 doc id → 清掉
marker 与账本行(两侧全无) → 下次启动走"合法遗留目录收养"干净重建身份。

用法:
    python3 scripts/topology_doctor.py scan --data <skill>/.data
    python3 scripts/topology_doctor.py heal --data <skill>/.data --yes \
        [--rename-id] [--evict-demos] [--project <pid>]

heal 不带 --yes 只打印将做什么(dry-run)。所有写操作前先整目录级备份到
.data/_topology-doctor-backup-<UTC时间戳>/,可整体回滚。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path

MARKER = ".ze-project.json"
CONTROL = {".ze-topology", ".ze-staging", ".ze-trash"}


def _state_path(data: Path, pid: str) -> Path:
    return data / ".ze-topology" / "state" / (
        hashlib.sha256(pid.encode("utf-8")).hexdigest() + ".json")


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def _diagnose(data: Path):
    """逐项目目录读四样身份,产出诊断行。"""
    projects = data / "projects"
    rows = []
    if not projects.is_dir():
        return rows
    for root in sorted(projects.iterdir()):
        if not root.is_dir() or root.name in CONTROL:
            continue
        pid = root.name
        doc = _read_json(root / "document.json")
        doc_id = (doc or {}).get("id")
        marker = _read_json(root / MARKER)
        state = _read_json(_state_path(data, pid))
        if doc is None:
            verdict = "invalid-document"      # 文档缺失/坏 → 只能从备份恢复
        elif doc_id != pid:
            verdict = "needs-rename-id"       # 目录被改名 → 需显式 --rename-id
        elif marker is None and state is None:
            verdict = "will-adopt"            # 两侧全无 → 启动即干净收养
        elif marker is None or state is None or (
            marker.get("project_id") != pid
            or state.get("project_id") != pid
            or marker.get("generation") != state.get("generation")
            or marker.get("operation_id") != state.get("operation_id")
        ):
            verdict = "heals-on-restart"      # 残缺/矛盾但 id 相符 → 新核心自愈;或 heal 清记录
        elif state.get("status") != "live":
            verdict = "frozen-but-consistent"  # 记录一致但被冻过 → heal 清记录即可
        else:
            verdict = "ok"
        rows.append({
            "pid": pid, "doc_id": doc_id, "verdict": verdict,
            "is_demo": pid.startswith("__demo__"),
            "marker": marker is not None, "state": (state or {}).get("status"),
        })
    return rows


def _hoist_bool_required(obj_schema: dict) -> bool:
    """一个对象节点内:子字段(对象类型)上的布尔 required → 提升到本级 required 名单。

    旧语法允许任意字段写 required: true;3.x 编译器在**对象类型字段**上拒绝布尔
    required(与对象语法的 required 名单撞车)。语义无损的翻译:摘布尔、进父名单。
    """
    props = obj_schema.get("properties")
    if not isinstance(props, dict):
        return False
    changed = False
    req = obj_schema.get("required")
    req_list = list(req) if isinstance(req, list) else []
    for name, sub in props.items():
        if (isinstance(sub, dict) and sub.get("type") == "object"
                and sub.get("required") is True):
            del sub["required"]
            if name not in req_list:
                req_list.append(name)
            changed = True
    if changed:
        obj_schema["required"] = req_list
    return changed


def _translate_legacy_schema_grammar(data_schema) -> set:
    """就地翻译 document.data_schema 里的旧语法;返回被改动的实体类型名集合。"""
    touched: set = set()
    if not isinstance(data_schema, dict):
        return touched

    def walk(node, entity_type: str) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" and _hoist_bool_required(node):
                touched.add(entity_type)
            for value in node.values():
                walk(value, entity_type)
        elif isinstance(node, list):
            for item in node:
                walk(item, entity_type)

    for entity_type, schema in data_schema.items():
        walk(schema, entity_type)
    return touched


def _heal(data: Path, rows, *, rename_id: bool, evict_demos: bool,
          only: str | None, apply: bool) -> int:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    backup = data / f"_topology-doctor-backup-{stamp}"
    planned, skipped = [], []
    for r in rows:
        if only and r["pid"] != only:
            continue
        if r["verdict"] == "ok" or r["verdict"] == "will-adopt":
            continue
        if r["verdict"] == "invalid-document":
            skipped.append((r["pid"], "文档缺失/损坏,只能从备份恢复,医生不动它"))
            continue
        if r["is_demo"] and evict_demos:
            planned.append((r["pid"], "evict-demo"))
            continue
        if r["verdict"] == "needs-rename-id" and not rename_id:
            skipped.append((r["pid"], "目录名与文档 id 不符;需 --rename-id 显式授权以目录名为准"))
            continue
        planned.append((r["pid"], "align-and-clear"))

    for pid, action in planned:
        root = data / "projects" / pid
        spath = _state_path(data, pid)
        print(f"{'[dry-run] ' if not apply else ''}heal {pid}: {action}")
        if not apply:
            continue
        dst = backup / pid
        dst.mkdir(parents=True, exist_ok=True)
        if action == "evict-demo":
            # demo 沙盒可再生:整目录移入备份,账本行也移走;下次打开示例即全新重建
            shutil.move(str(root), str(dst / "project"))
            if spath.exists():
                shutil.move(str(spath), str(dst / spath.name))
            continue
        # align-and-clear:备份三样 → 对齐 id → 清 marker+账本行(交给启动收养重建)
        if (root / "document.json").exists():
            shutil.copy2(root / "document.json", dst / "document.json")
        if (root / MARKER).exists():
            shutil.move(str(root / MARKER), str(dst / MARKER))
        if spath.exists():
            shutil.move(str(spath), str(dst / spath.name))
        doc = _read_json(root / "document.json") or {}
        changed = False
        if doc.get("id") != pid:
            doc["id"] = pid
            if "project_id" in doc:
                doc["project_id"] = pid
            changed = True
        translated = _translate_legacy_schema_grammar(doc.get("data_schema"))
        if translated:
            changed = True
            print(f"  · 旧语法翻译: {', '.join(sorted(translated))}")
        if changed:
            tmp = root / "document.json.doctor-tmp"
            tmp.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
            tmp.replace(root / "document.json")

    for pid, why in skipped:
        print(f"skip {pid}: {why}")
    if apply and planned:
        print(f"\n备份在: {backup}")
        print("修复完成;重启该工具的服务,启动对账会重建干净身份(状态应变 live)。")
    return 0 if not skipped else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sc = sub.add_parser("scan", help="只读诊断每个项目的身份一致性")
    sc.add_argument("--data", required=True)
    he = sub.add_parser("heal", help="修复(默认 dry-run;--yes 才写)")
    he.add_argument("--data", required=True)
    he.add_argument("--yes", action="store_true", help="真正执行(否则只打印计划)")
    he.add_argument("--rename-id", action="store_true",
                    help="目录名与文档 id 不符时,以目录名为准重写文档 id")
    he.add_argument("--evict-demos", action="store_true",
                    help="示例沙盒整目录移入备份(可再生,下次打开自动重建)")
    he.add_argument("--project", default=None, help="只处理这一个项目")
    args = ap.parse_args()

    data = Path(args.data).expanduser().resolve()
    if not (data / "projects").is_dir():
        print(f"未找到 {data}/projects;--data 应指向 skill 的 .data 目录", file=sys.stderr)
        return 2
    rows = _diagnose(data)
    if args.cmd == "scan":
        for r in rows:
            print(f"{r['verdict']:>22}  {r['pid']}"
                  + (f"  (doc id={r['doc_id']})" if r["verdict"] == "needs-rename-id" else "")
                  + (f"  [demo]" if r["is_demo"] else ""))
        bad = [r for r in rows if r["verdict"] not in ("ok", "will-adopt")]
        print(f"\n共 {len(rows)} 个项目;{len(bad)} 个需要处理" if bad
              else f"\n共 {len(rows)} 个项目;身份全部一致")
        return 0
    return _heal(data, rows, rename_id=args.rename_id, evict_demos=args.evict_demos,
                 only=args.project, apply=args.yes)


if __name__ == "__main__":
    raise SystemExit(main())
