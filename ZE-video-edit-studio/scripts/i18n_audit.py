#!/usr/bin/env python3
"""i18n_audit — i18n key 一致性 + 词表值语言纯度 + 槽位命名门禁。

五项检查(problems 任一失败 exit 1;warnings 只提示不拦):
  1. missing-key:UI 代码里**静态**调用的每个 key 必须在默认 locale 已定义。
     (补 tsc 盲区:TKey 含 `string & {}`,打错的 key 编译期不报、运行时静默回落 key 串。)
  2. parity:每门语言(框架词表 + mod.*)与默认 locale 的 key 集**完全一致**。
     (框架 zh↔en 镜像本由 tsc `as Dict` 保;此处覆盖 tsc 管不到的 mod 与运行时合并。)
  3. registry:locales.ts 的 LOCALE_CODES ↔ LOCALE_META ↔ 05-i18n/<code>.ts 文件三方对齐。
  4. purity:词表**值**语言纯度(命名治理)——zh 值除 PURITY_ALLOW 白名单外零拉丁词;
     en 值零 CJK。防「中文模式冒英文标题 / en 词表抄中文」。白名单见 i18n_keys_lib.PURITY_ALLOW。
  5. slot-name:04-layout.config 每个 slot 必须有 nameKey 且各 locale 词表有非空值
     (否则编辑器横栏回退实体/类名=「瞎起的名字」);`name:` 硬编码槽位名先记 warning(过渡),
     后续版本升报错。无布局文件(cli/api-only)自动跳过;布局解析失败按问题上报,不静默。

用法:
  python scripts/i18n_audit.py          # 有问题 exit 1(部署门禁)
  python scripts/i18n_audit.py --json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import i18n_keys_lib as lib  # noqa: E402

BUILD_ROOT = Path(__file__).resolve().parents[1]


def run() -> dict:
    dumped = lib.dump(BUILD_ROOT)
    codes = dumped["locale_codes"]
    default = dumped["default_locale"]
    problems: list[dict] = []

    # 1. missing-key:用了但默认 locale 未定义
    defined_default = lib.defined_keys(dumped, default)
    used = lib.scan_used_keys(BUILD_ROOT)
    for key, locs in sorted(used.items()):
        if key not in defined_default:
            problems.append({
                "kind": "missing-key", "key": key,
                "detail": f"used but undefined in '{default}'",
                "where": locs[:3],
            })

    # 2. parity:各 locale 相对默认 locale 的 key 集差异(框架 + mod 分别比)
    for layer in ("framework", "mod"):
        base = set(dumped.get(layer, {}).get(default, []))
        for code in codes:
            if code == default:
                continue
            cur = set(dumped.get(layer, {}).get(code, []))
            missing = sorted(base - cur)
            extra = sorted(cur - base)
            for k in missing:
                problems.append({"kind": "parity", "layer": layer, "locale": code,
                                  "key": k, "detail": f"missing in '{code}'"})
            for k in extra:
                problems.append({"kind": "parity", "layer": layer, "locale": code,
                                 "key": k, "detail": f"extra in '{code}' (not in '{default}')"})

    # 3. registry:LOCALE_CODES ↔ LOCALE_META ↔ <code>.ts
    meta_codes = dumped.get("meta_codes", [])
    if set(codes) != set(meta_codes):
        problems.append({"kind": "registry", "detail":
                         f"LOCALE_CODES {sorted(codes)} != LOCALE_META keys {sorted(meta_codes)}"})
    for code in codes:
        if not (BUILD_ROOT / "05-i18n" / f"{code}.ts").is_file():
            problems.append({"kind": "registry", "detail": f"missing dict file 05-i18n/{code}.ts"})

    warnings: list[dict] = []

    # 4. purity:词表值语言纯度(zh 零白名单外拉丁词;非 CJK locale 零 CJK;任何语言零系列名 ZE)
    for layer, values_field in (("framework", "framework_values"), ("mod", "mod_values")):
        values = dumped.get(values_field, {})
        for code in codes:
            for key, val in sorted(values.get(code, {}).items()):
                if code == "zh":
                    bad = lib.zh_latin_violations(val)
                    if bad:
                        problems.append({
                            "kind": "purity", "layer": layer, "locale": code, "key": key,
                            "detail": f"zh 值含白名单外拉丁词 {bad}: {val[:60]!r}",
                        })
                elif lib.has_cjk(val):
                    problems.append({
                        "kind": "purity", "layer": layer, "locale": code, "key": key,
                        "detail": f"'{code}' 值含 CJK: {val[:60]!r}",
                    })
                if lib.has_series_prefix(val):
                    problems.append({
                        "kind": "purity", "layer": layer, "locale": code, "key": key,
                        "detail": f"值含系列名 ZE(生效文案不出现系列前缀): {val[:60]!r}",
                    })

    # 5. slot-name:每个布局槽位必须有 nameKey 且各 locale 有非空译值
    slots = dumped.get("layout_slots")
    if dumped.get("layout_error"):
        problems.append({"kind": "slot-name",
                         "detail": f"04-layout.config 解析失败: {dumped['layout_error']}"})
    if slots:
        fw_values = dumped.get("framework_values", {})
        mod_values = dumped.get("mod_values", {})

        def _lookup(key: str, code: str) -> str | None:
            if key.startswith("mod."):
                return mod_values.get(code, {}).get(key[len("mod."):])
            return fw_values.get(code, {}).get(key)

        for s in slots:
            slot_id = s.get("id", "?")
            name_key = s.get("nameKey")
            hard_name = s.get("name")
            if not name_key:
                if hard_name:
                    warnings.append({"kind": "slot-name", "slot": slot_id,
                                     "detail": f"slot 用 name: {hard_name!r} 硬编码(过渡允许,后续版本报错);请改 nameKey"})
                else:
                    problems.append({"kind": "slot-name", "slot": slot_id,
                                     "detail": "slot 缺 nameKey(编辑器无名,横栏回退实体/类名)"})
                continue
            for code in codes:
                val = _lookup(name_key, code)
                if not val or not val.strip():
                    problems.append({"kind": "slot-name", "slot": slot_id,
                                     "detail": f"nameKey '{name_key}' 在 '{code}' 词表缺失或为空"})

    return {
        "ok": len(problems) == 0,
        "locales": codes,
        "used_keys": len(used),
        "problem_count": len(problems),
        "problems": problems,
        "warnings": warnings,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="i18n key 一致性门禁")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    res = run()
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(f"locales={res['locales']}  用到 key {res['used_keys']} 个")
        if res["ok"]:
            print("✓ i18n 五项通过(missing/parity/registry/purity/slot-name)")
        else:
            print(f"✗ {res['problem_count']} 个问题")
            for p in res["problems"]:
                extra = f"  @ {p['where']}" if p.get("where") else ""
                slot = f" slot={p['slot']}" if p.get("slot") else ""
                print(f"  [{p['kind']}]{slot} {p.get('key', '')} — {p['detail']}{extra}")
        for w in res.get("warnings", []):
            print(f"  ⚠ [{w['kind']}] slot={w.get('slot', '')} — {w['detail']}")
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
