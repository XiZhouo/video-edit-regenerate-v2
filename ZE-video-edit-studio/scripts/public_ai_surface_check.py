#!/usr/bin/env python3
"""public-ai-surface-audit(ZE 最终执行规划 §4.3/§5.3):C 端前端**显示表面**禁现内部品牌 / 内网地址。

规划红线:public bundle / public profile 不得出现 Hupo、琥珀网关、api.ai.hupo.games、内网 IP、内网
服务端口、内部渠道模型别名。内部资产本身可保留(后端 provider descriptor / gateway 默认 / allowlist /
凭据 provider id 都合法),但不能作为 C 端品牌或公网上游默认值。

因此本门只扫**前端显示层**(01-core + webapp/src 的 .ts/.tsx + 05-i18n 词表),不扫后端
(server.py / credentials.py / 04-ops config / *.py):后端合法持有内部标识。禁串是**用户可见泄露**,
不禁 provider 技术 id(hupo_gateway_key)——那是凭据匹配用的内部标识,不显示给用户。

F1/F2 已把 C 端可见串中性化(组织AI网关);本门是回归护栏,防新漂移。加性、零误报(琥珀 amber 颜色
已在 fixtures 改名蜜蜡,前端仅剩品牌义)。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCAN_DIRS = [ROOT / "01-core", ROOT / "webapp" / "src", ROOT / "05-i18n"]

# (正则, 说明)。只列**用户可见**的内部品牌/内网泄露;provider 技术 id(hupo_gateway_key)不在内。
FORBIDDEN: list[tuple[re.Pattern, str]] = [
    (re.compile(r"琥珀"), "内部品牌「琥珀」(网关品牌;amber 颜色请用别名如蜜蜡)"),
    (re.compile(r"api\.ai\.hupo\.games"), "内网网关 URL(不得作 C 端默认/显示)"),
    (re.compile(r"[Hh]upo[ _-]?gateway", re.IGNORECASE), "内部品牌标签 Hupo gateway(C 端用「组织AI网关」)"),
    (re.compile(r"\b10\.0\.0\.\d"), "内网 IP(10.0.0.x)"),
    (re.compile(r"\b192\.168\.\d"), "内网 IP(192.168.x)"),
    (re.compile(r"hupogames\.com"), "内网域名 hupogames.com"),
]
# provider 技术 id 是合法内部标识(凭据匹配),含在源里不算泄露 —— 命中含此子串的行放行。
ALLOW_IF_LINE_HAS = ("hupo_gateway_key", "hupo_gateway_", "PROVIDER_HUPO")


def _scan_file(p: Path) -> bool:
    if p.suffix not in (".ts", ".tsx"):
        return False
    s = str(p)
    if p.name.endswith((".test.ts", ".test.tsx")) or "node_modules" in s or "/dist/" in s:
        return False
    return True


def main() -> int:
    violations: list[tuple[str, int, str, str]] = []
    scanned = 0
    for d in SCAN_DIRS:
        if not d.exists():
            continue
        for p in sorted(d.rglob("*")):
            if not _scan_file(p):
                continue
            scanned += 1
            try:
                text = p.read_text(encoding="utf-8")
            except Exception:  # noqa: BLE001 — 读不了就跳过,不阻塞门
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                if any(tok in line for tok in ALLOW_IF_LINE_HAS):
                    continue
                for rx, why in FORBIDDEN:
                    if rx.search(line):
                        violations.append((str(p.relative_to(ROOT)), lineno, why, line.strip()[:90]))

    if violations:
        print(f"✗ public-ai-surface: {len(violations)} 处 C 端前端显示层内部品牌/内网泄露(扫 {scanned} 文件)")
        for rel, ln, why, snip in violations[:40]:
            print(f"  {rel}:{ln}  {why}")
            print(f"    {snip}")
        return 1
    print(f"✓ public-ai-surface: 前端显示层无内部品牌/内网泄露(扫 {scanned} 文件)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
