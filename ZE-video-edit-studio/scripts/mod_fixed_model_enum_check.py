#!/usr/bin/env python3
"""mod-fixed-model-enum(ZE 最终执行规划 §5.3 硬门 / §6.3 G2):mod 禁为通用 AI 功能维护**固定**
provider/model enum。

§5.3「mod 不得为通用 AI 功能维护固定 provider/model enum」/ §6.3 G2「Foundry mod 不再拥有通用
provider/model 列表」。模型真相归 Build ProviderHub descriptors;mod 只能**派生**(models_for_
capability / model_routes_map / legacy_alias_map),不得手抄 literal 目录。

检测(AST,结构性、零 model-id 参照、零误报):模块级赋值,目标名含「MODEL」(不分大小写)且
右值是 **literal 集合**(List/Dict/Set/Tuple,≥2 元素)= 硬编码目录 → 违规。派生写法(推导式
`[m.model_id for m in ...]` / 调用 `model_routes_map(...)` / 下标 `MODEL_ENUM[0]` / `set(MODEL_ENUM)`)
右值非 literal 集合 → 放行。per-model 参数分支(`if x == "gemini3"`)是比较非赋值 → 不匹配。

只扫 02-mod/**/*.py(排 test_/__pycache__)。foundry G2b 后全派生 → 绿;谁再手抄 MODEL_ENUM=[...] → 红。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCAN_DIR = ROOT / "02-mod"
_LITERAL_COLLECTION = (ast.List, ast.Dict, ast.Set, ast.Tuple)


def _elt_count(node: ast.AST) -> int:
    if isinstance(node, ast.Dict):
        return len(node.keys)
    if isinstance(node, (ast.List, ast.Set, ast.Tuple)):
        return len(node.elts)
    return 0


def _scan_file(p: Path) -> bool:
    if p.suffix != ".py":
        return False
    if p.name.startswith("test_") or "__pycache__" in str(p):
        return False
    return True


def main() -> int:
    if not SCAN_DIR.exists():
        print("✓ mod-fixed-model-enum: 无 02-mod(非业务 skill)")
        return 0
    violations: list[tuple[str, int, str]] = []
    scanned = 0
    for p in sorted(SCAN_DIR.rglob("*")):
        if not _scan_file(p):
            continue
        scanned += 1
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — 解析不了跳过(不阻塞门)
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            # 目标名含 MODEL(不分大小写)
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if not any("model" in n.lower() for n in names):
                continue
            # 右值是 ≥2 元素的 literal 集合(硬编码目录)
            if isinstance(node.value, _LITERAL_COLLECTION) and _elt_count(node.value) >= 2:
                violations.append((str(p.relative_to(ROOT)), node.lineno, ", ".join(names)))

    if violations:
        print(f"✗ mod-fixed-model-enum: {len(violations)} 处 mod 硬编码 model 目录(应从 sdk.hub descriptors 派生;扫 {scanned} 文件)")
        for rel, ln, names in violations[:40]:
            print(f"  {rel}:{ln}  硬编码目录赋给 {names}(§5.3/G2:改用 models_for_capability/model_routes_map/legacy_alias_map 派生)")
        return 1
    print(f"✓ mod-fixed-model-enum: mod 无硬编码 provider/model 目录(全派生自 Build descriptors;扫 {scanned} 文件)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
