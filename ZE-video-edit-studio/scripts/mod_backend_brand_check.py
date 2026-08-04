#!/usr/bin/env python3
"""mod-backend-brand(ZE 最终执行规划 §5.3 硬门):mod **后端代码**禁硬编码内部 endpoint / 品牌。

§5.3「mod 不得硬编码内部 endpoint/provider 品牌」。public-ai-surface 扫的是**前端显示层**
(01-core/webapp/src/05-i18n),本门补**后端** 02-mod 业务代码(.py):mod 不得在代码里写死
内网服务地址、组织品牌或内网域名——上游 provider 由 sdk.hub descriptors + endpoint policy 拥有,
真实 endpoint 由 04-ops config(ze.image.service_url 等,部署侧配)提供,mod 只经 image_call/
llm_call 间接调用,绝不自持内网地址。

扫 02-mod/**/*.py(排 test_/__pycache__);**只扫 .py 代码**——mod 内 .md 开发文档可描述配置
默认(如 08-config.md 记 ze.image.service_url 默认值),那是文档不是硬编码调用,且不进 C 端 bundle。
禁串同 public-ai-surface;放行 provider 技术 id(hupo_gateway_key,凭据匹配用)。

加性硬门,零误报(两 mod 现均绿)。防 mod 作者把内网地址写死进业务代码(绕开 config/descriptor)。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCAN_DIR = ROOT / "02-mod"

FORBIDDEN: list[tuple[re.Pattern, str]] = [
    (re.compile(r"api\.ai\.hupo\.games"), "内网网关 URL(应由 04-ops config / descriptor 提供,非硬编码)"),
    (re.compile(r"\b10\.0\.0\.\d"), "内网 IP(10.0.0.x;真实 endpoint 走 ze.image.service_url 配置)"),
    (re.compile(r"\b192\.168\.\d"), "内网 IP(192.168.x)"),
    (re.compile(r"hupogames\.com"), "内网域名 hupogames.com"),
    (re.compile(r"琥珀"), "内部品牌「琥珀」"),
    (re.compile(r"[Hh]upo[ _-]?gateway", re.IGNORECASE), "内部品牌标签 Hupo gateway"),
]
ALLOW_IF_LINE_HAS = ("hupo_gateway_key", "hupo_gateway_", "PROVIDER_HUPO")


def _scan_file(p: Path) -> bool:
    if p.suffix != ".py":
        return False
    if p.name.startswith("test_") or "__pycache__" in str(p):
        return False
    return True


def main() -> int:
    if not SCAN_DIR.exists():
        print("✓ mod-backend-brand: 无 02-mod(非业务 skill)")
        return 0
    violations: list[tuple[str, int, str, str]] = []
    scanned = 0
    for p in sorted(SCAN_DIR.rglob("*")):
        if not _scan_file(p):
            continue
        scanned += 1
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if any(tok in line for tok in ALLOW_IF_LINE_HAS):
                continue
            for rx, why in FORBIDDEN:
                if rx.search(line):
                    violations.append((str(p.relative_to(ROOT)), lineno, why, line.strip()[:90]))

    if violations:
        print(f"✗ mod-backend-brand: {len(violations)} 处 mod 后端代码硬编码内部 endpoint/品牌(扫 {scanned} 文件)")
        for rel, ln, why, snip in violations[:40]:
            print(f"  {rel}:{ln}  {why}")
            print(f"    {snip}")
        return 1
    print(f"✓ mod-backend-brand: mod 后端代码无硬编码内部 endpoint/品牌(扫 {scanned} 文件)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
