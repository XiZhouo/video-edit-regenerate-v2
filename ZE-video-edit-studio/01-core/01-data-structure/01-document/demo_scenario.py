"""demo_scenario.py — 数据化 demo 模板(scenario pack)的实例化器.

demo 是 build-v3 的一等文件系统能力。模板有两种来源(见 DocumentRegistry.register_demo_template):
  - bootstrap(doc):Python 函数 seed(兼容老路;build-v3 自带 default 模板走这条)。
  - scenario_path:指向 `demo/scenarios/<id>/manifest.json` 的数据化模板(新工具优先)。

本模块负责后者的实例化:把 manifest 里的实体声明 + `{"$asset": "<相对路径>"}` 占位,
导入成真 Entity + 真 asset_ref。**真数据真图**是硬约束 —— 资产占位解析成 AssetStore 里
内容寻址的真 blob;缺文件**显式报错**(asset_missing),绝不静默生成空引用。

安全:`$asset` 只允许 scenario 目录内的相对路径,杜绝引用运行时机器的随意绝对路径
(保证 demo pack 可移植、可被 Skill.Sync 下发)。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

# v3 平铺布局:注入 02-entity 兄弟目录(独立 import 本模块时也成立;import sdk 时已注入)。
_ENTITY_DIR = Path(__file__).resolve().parent.parent / "02-entity"
if str(_ENTITY_DIR) not in sys.path:
    sys.path.insert(0, str(_ENTITY_DIR))

from entity import Entity, ZeError  # noqa: E402


def load_scenario_manifest(scenario_path: str | os.PathLike) -> dict[str, Any]:
    """读 scenario manifest.json;缺失/损坏显式抛(demo 是产品能力,不静默装作没 demo)。"""
    p = Path(scenario_path)
    if not p.exists():
        raise ZeError(
            "asset_missing",
            f"scenario manifest 缺失: {scenario_path}",
            context={"path": str(scenario_path)},
        )
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise ZeError(
            "validation_failed",
            f"scenario manifest 解析失败: {scenario_path} ({e})",
            context={"path": str(scenario_path)},
        ) from e


def scenario_config(manifest: dict[str, Any]) -> dict[str, Any]:
    """取 manifest 的可选 document 级 config(沙盒建库时并进 demo_session config)。"""
    cfg = manifest.get("config")
    return dict(cfg) if isinstance(cfg, dict) else {}


def instantiate_scenario(
    doc: Any,
    manifest: dict[str, Any],
    *,
    scenario_dir: str | os.PathLike,
    asset_store: Any,
) -> None:
    """把 manifest 的实体声明灌进已建好的 doc;`{"$asset": rel}` 解析为真 asset_ref。

    doc 须已 build_document(schema 就位),调用方负责保存。每条实体 = doc.add(Entity(...)),
    走标准校验通道(schema 不符即报错,demo 数据也必须合法)。
    """
    base = Path(scenario_dir).resolve()
    for raw in manifest.get("entities", []):
        if not isinstance(raw, dict):
            raise ZeError("validation_failed", "scenario entity 须为对象", context={"entity": raw})
        etype = raw.get("type")
        if not etype:
            raise ZeError("validation_failed", "scenario entity 缺 type", context={"entity": raw})
        data = _resolve(raw.get("data", {}), scenario_dir=base, asset_store=asset_store)
        doc.add(Entity(
            type=etype,
            id=raw.get("id"),
            name=raw.get("name", ""),
            data=data if isinstance(data, dict) else {},
        ))


def _resolve(value: Any, *, scenario_dir: Path, asset_store: Any) -> Any:
    """递归走实体 data:遇 {"$asset": rel} 替换为真 asset_ref dict,其余原样。"""
    if isinstance(value, dict):
        if "$asset" in value:
            if len(value) != 1:
                raise ZeError(
                    "validation_failed",
                    "$asset 占位须独占一个对象 {\"$asset\": \"<相对路径>\"}",
                    context={"got": value},
                )
            return _resolve_asset_ref(value["$asset"], scenario_dir=scenario_dir, asset_store=asset_store)
        return {k: _resolve(v, scenario_dir=scenario_dir, asset_store=asset_store) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve(v, scenario_dir=scenario_dir, asset_store=asset_store) for v in value]
    return value


def _resolve_asset_ref(rel: Any, *, scenario_dir: Path, asset_store: Any) -> dict[str, Any]:
    """rel(scenario 目录内相对路径)→ AssetStore.put_file → asset_ref dict。"""
    if not isinstance(rel, str) or not rel:
        raise ZeError("validation_failed", "$asset 须为非空相对路径字符串", context={"rel": rel})
    p = Path(rel)
    if p.is_absolute():
        raise ZeError(
            "validation_failed",
            f"$asset 只许 scenario 目录内相对路径(禁绝对路径): {rel!r}",
            context={"rel": rel},
        )
    target = (scenario_dir / p).resolve()
    # 穿越护栏:resolve 后必须仍在 scenario_dir 内(防 ../ 逃逸)。
    if target != scenario_dir and scenario_dir not in target.parents:
        raise ZeError(
            "validation_failed",
            f"$asset 路径越出 scenario 目录: {rel!r}",
            context={"rel": rel},
        )
    if not target.exists():
        raise ZeError(
            "asset_missing",
            f"demo 资产缺失(真图必须存在,不静默生成占位): {rel}",
            context={"rel": rel, "path": str(target)},
        )
    return asset_store.put_file(target).to_dict()
