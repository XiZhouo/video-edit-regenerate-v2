#!/usr/bin/env python3
"""ai-requirements(ZE 最终执行规划 §5.2 Design / §5.3 Build):AI 工具必须声明 capability manifest。

§5.3「新工具 capability manifest,例如 ZE_AI_REQUIREMENTS.json」+「AI 命令必须声明 capability
requirement、preflight 和结构化错误」;§5.2 Design 强制产物「AI capability requirements」+「AI access
states」。tool 用了 AI(schema 里出现 x-ze-feature-id)就必须有一份有效 ZE_AI_REQUIREMENTS.json,
且覆盖所有用到的 feature_id —— 让 Deploy 能做 capability readiness 检查、Design 能做「无连接可理解」
QA、消费方能发现工具的 AI 需求。

manifest schema `ze.ai-requirements.v1`:
    { "schema": "ze.ai-requirements.v1",
      "features": [ { "feature_id": str, "capability": str, "require": [str],
                      "label": str?, "sync": bool?, "fallback": bool?,
                      "access_states": [str]? } ] }

检测:①扫 02-mod/**/*.py 收集 x-ze-feature-id 值(tool 用到的 AI 功能);②无则 manifest 可选(绿);
③有则 skill 根须有 ZE_AI_REQUIREMENTS.json,schema 版本对、features 结构对、且**覆盖每个用到的
feature_id**(缺声明 → 红)。access_states 若声明须是 §4.2 六态子集。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCAN_DIR = ROOT / "02-mod"
MANIFEST = ROOT / "ZE_AI_REQUIREMENTS.json"
SCHEMA_ID = "ze.ai-requirements.v1"
# §4.2 AI access states 词表(声明的 access_states 须是其子集)。
_ACCESS_STATES = {"unconfigured", "missing_capability", "expired", "quota_low", "unreachable", "ok"}
_FEATURE_RX = re.compile(r"""["']x-ze-feature-id["']\s*:\s*["']([^"']+)["']""")


def _used_feature_ids() -> set[str]:
    used: set[str] = set()
    if not SCAN_DIR.exists():
        return used
    for p in sorted(SCAN_DIR.rglob("*.py")):
        if p.name.startswith("test_") or "__pycache__" in str(p):
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001
            continue
        used.update(_FEATURE_RX.findall(text))
    return used


def _validate_manifest(raw: dict) -> list[str]:
    errs: list[str] = []
    if raw.get("schema") != SCHEMA_ID:
        errs.append(f"schema 须为 {SCHEMA_ID!r},得 {raw.get('schema')!r}")
    feats = raw.get("features")
    if not isinstance(feats, list):
        errs.append("features 须为数组")
        return errs
    for i, f in enumerate(feats):
        if not isinstance(f, dict):
            errs.append(f"features[{i}] 须为对象")
            continue
        if not isinstance(f.get("feature_id"), str) or not f["feature_id"]:
            errs.append(f"features[{i}].feature_id 缺/非串")
        if not isinstance(f.get("capability"), str) or not f["capability"]:
            errs.append(f"features[{i}].capability 缺/非串")
        req = f.get("require", [])
        if not isinstance(req, list) or any(not isinstance(x, str) for x in req):
            errs.append(f"features[{i}].require 须为字符串数组")
        st = f.get("access_states")
        if st is not None and (not isinstance(st, list) or set(st) - _ACCESS_STATES):
            errs.append(f"features[{i}].access_states 须是 §4.2 六态子集: {sorted(_ACCESS_STATES)}")
    return errs


def _servable_fine_caps() -> "set[str] | None":
    """Build descriptors 下所有**可服务**的细能力(某 ModelDefinition 声明了的)。§5.4 capability
    readiness:manifest 的 require 须落在此集合内(有模型能服务)。拿不到 descriptors(import 失败/
    非 Build 环境)→ None,跳过 servability(best-effort,不阻塞门)。"""
    import sys
    for p in (str(ROOT / "sdk"), str(ROOT / "01-core" / "04-user-facing" / "03-authentication"), str(ROOT)):
        if p not in sys.path:
            sys.path.insert(0, p)
    try:
        from sdk.hub import all_model_definitions  # type: ignore
        caps: set[str] = set()
        for m in all_model_definitions():
            caps.update(m.capabilities)
        return caps
    except Exception:  # noqa: BLE001 — 拿不到 descriptors 就跳过 servability
        return None


def main() -> int:
    used = _used_feature_ids()
    if not used:
        print("✓ ai-requirements: 本工具未用 AI(无 x-ze-feature-id),manifest 可选")
        return 0
    if not MANIFEST.exists():
        print(f"✗ ai-requirements: 工具用了 AI 功能 {sorted(used)} 但缺 ZE_AI_REQUIREMENTS.json(§5.3:AI 工具须声明 capability manifest)")
        return 1
    try:
        raw = json.loads(MANIFEST.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        print(f"✗ ai-requirements: ZE_AI_REQUIREMENTS.json 解析失败: {e}")
        return 1
    errs = _validate_manifest(raw) if isinstance(raw, dict) else ["顶层须为对象"]
    declared = {f.get("feature_id") for f in raw.get("features", []) if isinstance(f, dict)} if isinstance(raw, dict) else set()
    missing = sorted(used - declared)
    if missing:
        errs.append(f"用到但 manifest 未声明的 feature_id: {missing}(每个 AI 命令须声明 capability requirement)")
    # §5.4 capability readiness:声明的 require 细能力须有 Build descriptor 模型可服务(best-effort)。
    servable = _servable_fine_caps()
    if servable is not None and isinstance(raw, dict):
        for f in raw.get("features", []):
            if not isinstance(f, dict):
                continue
            for cap in (f.get("require") or []):
                if isinstance(cap, str) and cap and cap not in servable:
                    errs.append(f"feature {f.get('feature_id')!r} 声明能力 {cap!r} 无 Build descriptor 模型可服务(readiness 缺口)")
    if errs:
        print(f"✗ ai-requirements: ZE_AI_REQUIREMENTS.json {len(errs)} 处问题")
        for e in errs[:20]:
            print(f"  - {e}")
        return 1
    print(f"✓ ai-requirements: {len(declared)} feature 声明齐,覆盖 {len(used)} 个用到的 AI 功能")
    return 0


if __name__ == "__main__":
    sys.exit(main())
