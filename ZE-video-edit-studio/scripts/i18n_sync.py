#!/usr/bin/env python3
"""i18n_sync — 让「写一句新文案」结构上不漏配语言。

开发循环:组件里写 `t('editor.foo')` → 跑 `--fill` → `editor.foo` 自动出现在每本词表里
(占位 `TODO:editor.foo`),开发者只填译文。

  --check(默认,CI 友好):报「用了但未定义」+「各语言 key 集相对默认 locale 漂移」,有问题 exit 1。
  --fill(开发):把缺失 key 按嵌套结构补进每本词表(只补不删,孤儿仅报告)。

写哪本词表按 key 前缀路由:
  - `mod.*` → 02-mod/09-i18n.ts 的 messages map(作者拥有的业务译文)。
  - 其余 → 05-i18n/<code>.ts 框架词表(同步层)。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import i18n_keys_lib as lib  # noqa: E402

BUILD_ROOT = Path(__file__).resolve().parents[1]


def analyze(build_root: Path = BUILD_ROOT) -> dict:
    """算出 used-undefined + 各 locale 相对默认的 framework/mod key 漂移。"""
    dumped = lib.dump(build_root)
    codes = dumped["locale_codes"]
    default = dumped["default_locale"]
    used = lib.scan_used_keys(build_root)

    defined_default = lib.defined_keys(dumped, default)
    undefined = sorted(k for k in used if k not in defined_default)

    drift = []  # 相对默认 locale 缺/多的 key
    for layer in ("framework", "mod"):
        base = set(dumped.get(layer, {}).get(default, []))
        for code in codes:
            if code == default:
                continue
            cur = set(dumped.get(layer, {}).get(code, []))
            for k in sorted(base - cur):
                drift.append({"layer": layer, "locale": code, "key": k, "detail": "missing"})
            for k in sorted(cur - base):
                drift.append({"layer": layer, "locale": code, "key": k, "detail": "extra"})
    return {"dumped": dumped, "used": used, "undefined": undefined, "drift": drift}


# ── --fill ────────────────────────────────────────────────────────────────

def _fw_file(build_root: Path, code: str) -> Path:
    return build_root / "05-i18n" / f"{code}.ts"


def _mod_file(build_root: Path) -> Path:
    return build_root / "02-mod" / "09-i18n.ts"


def fill(build_root: Path = BUILD_ROOT) -> dict:
    """把缺失 key 补进每本词表(union 对齐 + 新增 used key)。返回新增清单。"""
    dumped = lib.dump(build_root)
    codes = dumped["locale_codes"]
    if not codes:
        return {"added": [], "count": 0, "skipped": []}
    used = lib.scan_used_keys(build_root)
    defined_default = lib.defined_keys(dumped, dumped["default_locale"])
    undefined = [k for k in used if k not in defined_default]

    fw = dumped.get("framework", {})
    mod = dumped.get("mod", {})

    # 各层目标 key 集 = 所有 locale 的并集 ∪ 新用到的未定义 key
    fw_target = set().union(*(set(fw.get(c, [])) for c in codes)) if codes else set()
    mod_target = set().union(*(set(mod.get(c, [])) for c in codes)) if codes else set()
    for k in undefined:
        if k.startswith("mod."):
            mod_target.add(k[len("mod."):])
        else:
            fw_target.add(k)

    added: list[str] = []
    skipped: list[str] = []   # 叶子被当 namespace 等冲突 key:跳过不破坏文件,交人工

    # 框架词表:每本 05-i18n/<code>.ts 补到 fw_target
    for code in codes:
        path = _fw_file(build_root, code)
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        changed = False
        for key in sorted(fw_target - set(fw.get(code, []))):
            try:
                text = lib.insert_key(text, key, f"TODO:{key}", rf"export const {code}\b")
                added.append(f"{code}.ts: {key}")
                changed = True
            except ValueError as e:
                skipped.append(f"{code}.ts: {key} — {e}")
        if changed:
            path.write_text(text, encoding="utf-8")

    # mod 词表:02-mod/09-i18n.ts 的 messages.<code> 补到 mod_target
    mpath = _mod_file(build_root)
    if mpath.is_file() and mod_target:
        text = mpath.read_text(encoding="utf-8")
        changed = False
        for code in codes:
            for key in sorted(mod_target - set(mod.get(code, []))):
                try:
                    text = lib.insert_key(text, f"{code}.{key}", f"TODO:mod.{key}",
                                          r"export const messages\b")
                    added.append(f"09-i18n.ts: {code}.{key}")
                    changed = True
                except ValueError as e:
                    skipped.append(f"09-i18n.ts: {code}.{key} — {e}")
        if changed:
            mpath.write_text(text, encoding="utf-8")

    return {"added": added, "count": len(added), "skipped": skipped}


def main() -> int:
    ap = argparse.ArgumentParser(description="i18n key 同步器")
    ap.add_argument("--fill", action="store_true", help="把缺失 key 补进词表(默认只 --check)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.fill:
        res = fill()
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            print(f"补入 {res['count']} 个 key:")
            for a in res["added"]:
                print(f"  + {a}")
            if res["count"]:
                print("(占位 TODO:<key> —— 请填真实译文)")
            for s in res.get("skipped", []):
                print(f"  ! 跳过(人工处理):{s}")
        return 0

    a = analyze()
    issues = len(a["undefined"]) + len(a["drift"])
    if args.json:
        print(json.dumps({"ok": issues == 0, "undefined": a["undefined"], "drift": a["drift"]},
                         ensure_ascii=False, indent=2))
    else:
        if issues == 0:
            print("✓ i18n key 同步一致(无 used-undefined、无 locale 漂移)")
        else:
            if a["undefined"]:
                print(f"✗ {len(a['undefined'])} 个 key 用了但未定义(跑 --fill 补占位):")
                for k in a["undefined"]:
                    print(f"  ? {k}  @ {a['used'][k][:2]}")
            for d in a["drift"]:
                print(f"  [{d['layer']}/{d['locale']}] {d['key']} — {d['detail']}")
    return 0 if issues == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
