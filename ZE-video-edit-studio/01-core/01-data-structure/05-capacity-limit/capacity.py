"""Capacity Limit — 容量上限护栏 (CapacityLimits + CapacityChecker).

完整 SPEC 见同目录 SPEC.md.

已实装 (无跨叶子依赖):
  - CapacityLimits frozen dataclass: 4 个 int 字段 + from_env(env=None)
  - DEFAULT_LIMITS = CapacityLimits()
  - CapacityChecker:
      check_add_entity(doc)       — 当前 entity 数 + 1 超 entity_count_max 抛
      check_entity_data(entity)   — data_size / field_count 任一超抛
      check_asset_blob(size)      — size > asset_blob_size_max 抛
      check_doc_total_assets(store, additional_size=0) — project local bytes quota
      measure_entity(entity)      — 返 {data_size, field_count}
  - 超限统一抛 ZeError("capacity_exceeded", context={limit_name, limit, actual})
  - ZeError 共享: 从 02-entity 兄弟目录 import

待补 (真依赖):
  - 自动 hook 到所有 command handler  [03-commands-operations]

设计 (见 SPEC §6):
  - 不嵌入 hot path: Document.add / entity.update / store.put 默认不调
  - caller 显式调; 保留 admin / batch 绕路
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

# 共享 ZeError: 从 02-entity 兄弟目录 import (v3 平铺布局, sys.path 注入)
_ENTITY_DIR = Path(__file__).resolve().parent.parent / "02-entity"
if str(_ENTITY_DIR) not in sys.path:
    sys.path.insert(0, str(_ENTITY_DIR))

from entity import ZeError  # noqa: E402


# ==============================================================
# 限额值类型
# ==============================================================

_ENV_PREFIX = "ZE_CAPACITY_"

_ENV_FIELD_MAP = {
    "entity_count_max":       _ENV_PREFIX + "ENTITY_COUNT_MAX",
    "entity_data_size_max":   _ENV_PREFIX + "ENTITY_DATA_SIZE_MAX",
    "entity_field_count_max": _ENV_PREFIX + "ENTITY_FIELD_COUNT_MAX",
    "asset_blob_size_max":    _ENV_PREFIX + "ASSET_BLOB_SIZE_MAX",
    "doc_total_assets_size_max": _ENV_PREFIX + "DOC_TOTAL_ASSETS_SIZE_MAX",
}


@dataclass(frozen=True)
class CapacityLimits:
    """单 Document 数据规模上限. frozen → 防误改 + hash 稳定.

    默认值见 SPEC §2; 选型理由同节.
    """

    entity_count_max: int = 10_000
    entity_data_size_max: int = 1_000_000           # 1 MB (JSON UTF-8 字节数)
    entity_field_count_max: int = 100               # 递归键总数
    asset_blob_size_max: int = 512 * 1024 * 1024    # 512 MB
    doc_total_assets_size_max: int = 512 * 1024 * 1024  # 512 MB local/project

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "CapacityLimits":
        """从环境变量读, 缺失 fallback default; 非法值抛 capacity_config_invalid.

        识别变量:
          ZE_CAPACITY_ENTITY_COUNT_MAX
          ZE_CAPACITY_ENTITY_DATA_SIZE_MAX
          ZE_CAPACITY_ENTITY_FIELD_COUNT_MAX
          ZE_CAPACITY_ASSET_BLOB_SIZE_MAX
          ZE_CAPACITY_DOC_TOTAL_ASSETS_SIZE_MAX

        env=None 时读 os.environ; 测试可注入 dict 不污染进程 env.
        所有值要正整数 (> 0).
        """
        src = env if env is not None else os.environ
        defaults = cls()
        kwargs: dict[str, int] = {}
        for field_name, env_name in _ENV_FIELD_MAP.items():
            raw = src.get(env_name)
            if raw is None or raw == "":
                continue
            try:
                value = int(raw)
            except (TypeError, ValueError):
                raise ZeError(
                    "capacity_config_invalid",
                    f"{env_name}={raw!r} not an integer",
                    context={"field": field_name, "env": env_name, "value": raw},
                )
            if value <= 0:
                raise ZeError(
                    "capacity_config_invalid",
                    f"{env_name}={value} must be > 0",
                    context={"field": field_name, "env": env_name, "value": value},
                )
            kwargs[field_name] = value
        # 缺失字段用 default
        merged = {
            "entity_count_max":       kwargs.get("entity_count_max",       defaults.entity_count_max),
            "entity_data_size_max":   kwargs.get("entity_data_size_max",   defaults.entity_data_size_max),
            "entity_field_count_max": kwargs.get("entity_field_count_max", defaults.entity_field_count_max),
            "asset_blob_size_max":    kwargs.get("asset_blob_size_max",    defaults.asset_blob_size_max),
            "doc_total_assets_size_max": kwargs.get(
                "doc_total_assets_size_max", defaults.doc_total_assets_size_max,
            ),
        }
        return cls(**merged)


# 模块顶层常量: caller 不传时默认用这个
DEFAULT_LIMITS: CapacityLimits = CapacityLimits()


# ==============================================================
# 内部辅助: 计算 entity 测量值
# ==============================================================

def _data_size_bytes(data: dict[str, Any]) -> int:
    """JSON UTF-8 字节数 (跟落盘成本对齐, 不用 sys.getsizeof)."""
    return len(json.dumps(data, ensure_ascii=False).encode("utf-8"))


def _field_count_recursive(data: dict[str, Any]) -> int:
    """递归数所有 dict key 总数. list 内含 dict 也算.

    示例: {"a": {"b": 1, "c": 2}, "d": 3}
      → 4 (a / a.b / a.c / d)
    """
    if not isinstance(data, dict):
        return 0
    total = 0
    for key, value in data.items():
        total += 1
        if isinstance(value, dict):
            total += _field_count_recursive(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    total += _field_count_recursive(item)
    return total


def _count_entities_in_doc(doc: Any) -> int:
    """走 doc.types() + doc.list(t, include_deleted=True) 数 entity 总数."""
    total = 0
    for entity_type in doc.types():
        total += len(doc.list(entity_type, include_deleted=True))
    return total


def _raise_exceeded(limit_name: str, limit: int, actual: int) -> None:
    raise ZeError(
        "capacity_exceeded",
        f"{limit_name} exceeded: limit={limit}, actual={actual}",
        context={"limit_name": limit_name, "limit": limit, "actual": actual},
    )


# ==============================================================
# CapacityChecker
# ==============================================================

class CapacityChecker:
    """容量校验器. caller 在 command handler 入口主动调.

    见 SPEC §6: 不嵌入 framework hot path; admin / batch 可绕路.
    """

    def __init__(self, limits: CapacityLimits = DEFAULT_LIMITS) -> None:
        self._limits = limits

    @property
    def limits(self) -> CapacityLimits:
        """只读访问当前 limits."""
        return self._limits

    # ──────── 拒绝路径 (超限抛 capacity_exceeded) ────────

    def check_add_entity(self, doc: Any) -> None:
        """当前 doc 中 entity 总数 + 1 > entity_count_max 时抛.

        含软删 entity (include_deleted=True); 不含 hard_removed.
        """
        current = _count_entities_in_doc(doc)
        next_count = current + 1
        if next_count > self._limits.entity_count_max:
            _raise_exceeded(
                "entity_count_max", self._limits.entity_count_max, next_count,
            )

    def check_entity_data(self, entity: Any) -> None:
        """同时校 data_size + field_count; 任一超就抛 (data_size 优先)."""
        data = entity.data        # property: 返回 copy dict
        size = _data_size_bytes(data)
        if size > self._limits.entity_data_size_max:
            _raise_exceeded(
                "entity_data_size_max", self._limits.entity_data_size_max, size,
            )
        field_count = _field_count_recursive(data)
        if field_count > self._limits.entity_field_count_max:
            _raise_exceeded(
                "entity_field_count_max",
                self._limits.entity_field_count_max,
                field_count,
            )

    def check_asset_blob(self, size: int) -> None:
        """blob 字节数 > asset_blob_size_max 抛."""
        if not isinstance(size, int) or size < 0:
            raise ZeError(
                "validation_failed",
                f"check_asset_blob expects non-negative int, got {size!r}",
                context={"value": size},
            )
        if size > self._limits.asset_blob_size_max:
            _raise_exceeded(
                "asset_blob_size_max", self._limits.asset_blob_size_max, size,
            )

    def check_doc_total_assets(self, store: Any, *, additional_size: int = 0) -> None:
        """Reject a local project asset set that would exceed its byte quota."""
        if not isinstance(additional_size, int) or additional_size < 0:
            raise ZeError(
                "validation_failed",
                f"additional_size must be a non-negative int, got {additional_size!r}",
                context={"value": additional_size},
            )
        total_size = getattr(store, "total_size", None)
        if not callable(total_size):
            raise ZeError(
                "validation_failed", "asset store must provide total_size()",
                context={"required": "total_size()"},
            )
        actual = total_size() + additional_size
        if actual > self._limits.doc_total_assets_size_max:
            _raise_exceeded(
                "doc_total_assets_size_max",
                self._limits.doc_total_assets_size_max,
                actual,
            )

    # ──────── 诊断路径 (只测量, 不抛) ────────

    def measure_entity(self, entity: Any) -> dict[str, int]:
        """返回 {'data_size': int, 'field_count': int}.

        diagnostic 用; 不抛错. caller 可比较 limits 自行决定动作.
        """
        data = entity.data
        return {
            "data_size": _data_size_bytes(data),
            "field_count": _field_count_recursive(data),
        }


__all__ = [
    "CapacityLimits",
    "CapacityChecker",
    "DEFAULT_LIMITS",
]
