#!/usr/bin/env python3
"""Type.Registry.Check — 类型库词表门禁(GATE-TYPE-07)+ mod archetype 防漂移门禁(GATE-TYPE-09)。

双职责(随 toolchain 下发到每个 skill;两半边的下游意义不同):

  **GATE-TYPE-07(词表双向同步)**:TYPE-REGISTRY.md fenced 表 ⇄ type_library.list_fragments()
  双向一一对应 + 版本/标记不失真——「文档撒谎 / 加 fragment 不登记 / 删 fragment 不撤登记」
  结构性不可能(镜像 rule_registry_check 的 keystone 角色)。词表只在 build-v3 上游创作,
  下游的叶子与注册表是同包 sync 产物 → 本半边下游恒绿。

  **GATE-TYPE-09(mod archetype 防漂移)**:scratch DocumentRegistry 跑 mod register() 后比对——
  带 `x-ze-archetype` 注解的 schema,其库属字段必须是库内 fragment@version 的**合法收紧**
  (sealed 字段须全等,剔外观键),库外新字段必须 x_ 前缀,pinned 落点名不得改挂——防下游
  手改 resolved schema 后注解变谎言。**本半边在下游真把关**(mod 是下游自己的,能修)。

手写 schema(无注解)= 逃生舱,不受管(SPEC §6 纪律④)。

用法:
  python3 scripts/type_registry_check.py          # 全过 exit 0;违规 exit 1 并列出
  python3 scripts/type_registry_check.py --json   # 机器可读
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEAF = ROOT / "01-core" / "01-data-structure" / "07-type-library"
REGISTRY_MD = LEAF / "TYPE-REGISTRY.md"
MOD_REGISTER = ROOT / "02-mod" / "05-code" / "document_register.py"
BEGIN, END = "TYPE-REGISTRY:BEGIN", "TYPE-REGISTRY:END"

# 机器区表格行: | `ze.<tier>.<name>` | @N | 名 | 标记 |
_ROW_RE = re.compile(
    r"^\|\s*`(ze\.(?:field|cluster|archetype|doc)\.[a-z][a-z0-9_]*)`\s*"
    r"\|\s*@(\d+)\s*\|\s*[^|]*\|\s*([^|]*)\|"
)


def _load_lib():
    """注入叶子路径并 import type_library;叶子缺失(异常裁剪的下游)返回 None → 跳过。"""
    if not (LEAF / "type_library.py").is_file():
        return None
    ds = LEAF.parent
    for d in (LEAF, ds / "02-entity", ds / "04-field-types", ds / "01-document"):
        if str(d) not in sys.path:
            sys.path.insert(0, str(d))
    import type_library  # noqa: E402
    return type_library


def _expected_tokens(f: dict) -> set[str]:
    toks: set[str] = set()
    if f["closed"]:
        toks.add("closed")
    if f["pinned_name"]:
        toks.add(f"pinned={f['pinned_name']}")
    if f["tier"] == "archetype":
        toks.add(f"sealed={len(f['sealed'])}")
        toks.add(f"collab={'true' if f['collab'] else 'false'}")
    if f["singleton"]:
        toks.add("singleton")
    return toks


# ==============================================================
# GATE-TYPE-07 — 词表双向同步
# ==============================================================

def check_registry_doc(problems: list[str]) -> str:
    lib = _load_lib()
    if lib is None:
        return "skipped(类型库叶子缺失)"
    if not REGISTRY_MD.is_file():
        problems.append(f"TYPE_REGISTRY_MISSING: {REGISTRY_MD.relative_to(ROOT)} 不存在")
        return "checked"
    text = REGISTRY_MD.read_text(encoding="utf-8")
    if BEGIN not in text or END not in text:
        problems.append("TYPE_REGISTRY_FENCE_MISSING: 缺 <!-- TYPE-REGISTRY:BEGIN/END --> 机器区")
        return "checked"
    fenced = text.split(BEGIN, 1)[1].split(END, 1)[0]
    doc_rows: dict[str, tuple[int, set[str]]] = {}
    for line in fenced.splitlines():
        m = _ROW_RE.match(line.strip())
        if m:
            toks = set(m.group(3).split()) - {"—"}
            doc_rows[m.group(1)] = (int(m.group(2)), toks)

    code_rows = {f["id"]: f for f in lib.list_fragments()}

    for fid in sorted(code_rows.keys() - doc_rows.keys()):
        problems.append(f"TYPE_NOT_REGISTERED: {fid} 在库内但 TYPE-REGISTRY.md 未登记")
    for fid in sorted(doc_rows.keys() - code_rows.keys()):
        problems.append(f"TYPE_REGISTERED_BUT_ABSENT: {fid} 登记了但库内不存在")
    for fid in sorted(code_rows.keys() & doc_rows.keys()):
        ver, toks = doc_rows[fid]
        f = code_rows[fid]
        if ver != f["version"]:
            problems.append(f"TYPE_VERSION_DRIFT: {fid} 库 @{f['version']} ≠ 登记 @{ver}")
        expect = _expected_tokens(f)
        if toks != expect:
            problems.append(
                f"TYPE_FLAG_DRIFT: {fid} 登记标记 {sorted(toks) or '—'} ≠ 库实况 {sorted(expect) or '—'}"
            )
    return "checked"


# ==============================================================
# GATE-TYPE-09 — mod archetype 防漂移
# ==============================================================

def _import_mod_register():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))   # document_register 里 `from sdk import ...`
    # 02-mod/05-code 也入 path:与 bootstrap._load_mod 一致,支持 document_register 里
    # `from <sibling> import ...`(把默认值/schema 拆成兄弟模块是 many-small-files 合法惯例;
    # 否则任何用兄弟导入的 mod 在本门禁必 MOD_REGISTER_BROKEN,而 server/CLI 却正常)。
    mod_code = MOD_REGISTER.parent
    if str(mod_code) not in sys.path:
        sys.path.insert(0, str(mod_code))
    spec = importlib.util.spec_from_file_location("ze_trc_document_register", MOD_REGISTER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check_mod_drift(problems: list[str]) -> str:
    lib = _load_lib()
    if lib is None:
        return "skipped(类型库叶子缺失)"
    if not MOD_REGISTER.is_file():
        return "skipped(无 mod document_register)"
    from document import DocumentRegistry  # noqa: E402 (叶子路径已注入)

    try:
        mod = _import_mod_register()
        reg = DocumentRegistry()
        mod.register(reg)
    except Exception as e:   # mod import/register 自身炸 → 结构化失败而非裸 traceback
        problems.append(f"MOD_REGISTER_BROKEN: document_register 导入/执行失败: {e}")
        return "checked(register 失败)"
    annotated = _check_registry_entity_types(reg, lib, problems)
    return f"checked({annotated} 个注解 schema)"


def _check_registry_entity_types(reg, lib, problems: list[str]) -> int:
    """比对一个已灌声明的 DocumentRegistry(测试可直接注入篡改场景);返回注解 schema 数。"""
    annotated = 0
    for name, decl in sorted(reg.entity_types.items()):
        schema = decl["schema"] or {}
        aid = schema.get("x-ze-archetype")
        if not aid:
            continue   # 手写逃生舱,不受管
        annotated += 1
        m = re.match(r"^ze\.archetype\.([a-z][a-z0-9_]*)@(\d+)$", aid)
        if not m:
            problems.append(f"MOD_ANNOTATION_MALFORMED: {name} 的 x-ze-archetype={aid!r}")
            continue
        short, ver = m.group(1), int(m.group(2))
        from fragments import ARCHETYPES, FIELDS  # noqa: E402
        entry = ARCHETYPES.get(short)
        if entry is None:
            problems.append(f"MOD_ARCHETYPE_UNKNOWN: {name} 注解 {aid} 不在库内")
            continue
        if entry["version"] != ver:
            problems.append(
                f"MOD_ARCHETYPE_VERSION_DRIFT: {name} 注解 @{ver},库已 @{entry['version']}(走迁移)"
            )
            continue
        baseline = lib.archetype(short)
        base_props = baseline["properties"]
        decl_props = schema.get("properties") or {}
        sealed = set(baseline["x-ze-sealed"])

        for fname, base in base_props.items():
            if fname.startswith("x_"):
                continue
            got = decl_props.get(fname)
            if got is None:
                kind = "MOD_SEALED_FIELD_MISSING" if fname in sealed else "MOD_LIBRARY_FIELD_REMOVED"
                problems.append(f"{kind}: {name}.{fname}(库属字段不得删——删=放松)")
                continue
            if fname in sealed:
                if lib.strip_appearance(got) != lib.strip_appearance(base):
                    problems.append(
                        f"MOD_SEALED_FIELD_DRIFT: {name}.{fname} 与库内不全等(sealed 连 narrow 都不行)"
                    )
            else:
                try:
                    lib.assert_tightens(base, got, path=f"{name}.{fname}")
                except Exception as e:   # ZeError;import 路径差异下宽接
                    problems.append(f"MOD_LIBRARY_FIELD_LOOSENED: {name}.{fname} {e}")

        for fname, got in decl_props.items():
            if fname in base_props or fname.startswith("x_"):
                # pinned 落点名不得改挂(x_ 自由区豁免)
                use = (got or {}).get("x-ze-use", "")
                fm = re.match(r"^ze\.field\.([a-z][a-z0-9_]*)@\d+$", use)
                pinned = (FIELDS.get(fm.group(1)) or {}).get("pinned_name") if fm else None
                if pinned and fname != pinned and not fname.startswith("x_"):
                    problems.append(
                        f"MOD_PINNED_NAME_DRIFT: {name}.{fname} 用了 {use}(落点名钉死 {pinned!r})"
                    )
                continue
            problems.append(
                f"MOD_FIELD_NOT_X_PREFIXED: {name}.{fname} 不在库 archetype 内且无 x_ 前缀(加性 only)"
            )
    return annotated


# ==============================================================
# CLI
# ==============================================================

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="type registry + mod drift gate")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    problems: list[str] = []
    s7 = check_registry_doc(problems)
    n7 = len(problems)
    s9 = check_mod_drift(problems)

    if args.json:
        print(json.dumps({"ok": not problems, "registry": s7, "mod": s9,
                          "problems": problems}, ensure_ascii=False, indent=2))
    else:
        for p in problems:
            print(f"✗ {p}")
        if not problems:
            print(f"✓ type-registry: GATE-TYPE-07 {s7}; GATE-TYPE-09 {s9}")
        else:
            print(f"✗ type-registry: {n7} 词表问题 + {len(problems) - n7} mod 漂移问题")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
