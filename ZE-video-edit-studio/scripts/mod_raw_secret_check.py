#!/usr/bin/env python3
"""mod-raw-secret(ZE 最终执行规划 §5.3 硬门):mod 禁在**实体 schema** 声明 raw secret 字段。

§5.3「mod 不得声明 raw secret 字段」+「复用 System CredentialStore,删除并禁止第二套明文 credential
entity」。实体数据(document)是**明文持久化 + 协作共享**的:密钥绝不能作实体字段落进文档。凭据一律
走 System CredentialStore(密封存储 + reveal_secret 出),mod 侧永不自持明文密钥字段。

检测(正则,precise、零误报):mod .py 里**schema 属性声明** `"<secret-name>": { ... }`——键是无歧义
的密钥名(secret/apiKey/api_key/apikey/password/passwd/access_token/refresh_token/private_key/
client_secret/secret_key),右接 `{`(= 字段 spec dict)。放行普通用词(如 token 单独太泛不列)。

只扫 02-mod/**/*.py(排 test_/__pycache__)。foundry 现无(凭据全走 CredentialStore)→ 绿。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCAN_DIR = ROOT / "02-mod"

# 无歧义密钥字段名(作 schema 属性键 → 明文实体字段 = 违规)。
_SECRET_NAMES = (
    "secret", "apiKey", "api_key", "apikey", "password", "passwd",
    "access_token", "refresh_token", "private_key", "client_secret", "secret_key",
)
# `"<secret-name>": {`  —— schema 属性声明(键→字段 spec dict)。
_RX = re.compile(
    r"""["'](%s)["']\s*:\s*\{""" % "|".join(re.escape(n) for n in _SECRET_NAMES),
    re.IGNORECASE,
)


def _scan_file(p: Path) -> bool:
    if p.suffix != ".py":
        return False
    if p.name.startswith("test_") or "__pycache__" in str(p):
        return False
    return True


def main() -> int:
    if not SCAN_DIR.exists():
        print("✓ mod-raw-secret: 无 02-mod(非业务 skill)")
        return 0
    violations: list[tuple[str, int, str]] = []
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
            m = _RX.search(line)
            if m:
                violations.append((str(p.relative_to(ROOT)), lineno, m.group(1)))

    if violations:
        print(f"✗ mod-raw-secret: {len(violations)} 处 mod 实体 schema 声明 raw secret 字段(扫 {scanned} 文件)")
        for rel, ln, name in violations[:40]:
            print(f"  {rel}:{ln}  字段「{name}」= 明文密钥入实体文档(§5.3:凭据须走 System CredentialStore,禁自持明文)")
        return 1
    print(f"✓ mod-raw-secret: mod 实体 schema 无 raw secret 字段(凭据走 CredentialStore;扫 {scanned} 文件)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
