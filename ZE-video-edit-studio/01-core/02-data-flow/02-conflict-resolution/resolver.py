"""Conflict Resolution — 4 策略 resolver (纯函数协议层).

完整 SPEC 见同目录 SPEC.md.

已实装:
  - resolve(strategy, base, current, incoming, crdt_merge=None)
  - 4 策略: reject / last-write-wins / three-way-merge / crdt
  - 别名兼容: last_write_wins / three_way_merge (下划线变体)
  - crdt 策略: 传 crdt_merge fn 用 caller 自定义; 否则抛 NotImplementedError

待补 (跨叶子):
  - CRDT 引擎 (LWW-Register / G-Counter / OR-Set / RGA)  [03-offline-first]
  - Vector Clock                                          [01-realtime-sync]
  - 自动 conflict detection hook                          [01-command dispatch]
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable

# 共享 ZeError: 从 02-entity 兄弟目录 import
_ENTITY_DIR = Path(__file__).resolve().parent.parent.parent / "01-data-structure" / "02-entity"
if str(_ENTITY_DIR) not in sys.path:
    sys.path.insert(0, str(_ENTITY_DIR))

from entity import ZeError  # noqa: E402


# 4 个规范 strategy 名 + 下划线别名 (caller 友好)
_REJECT = "reject"
_LWW = "last-write-wins"
_LWW_ALIASES = (_LWW, "last_write_wins")
_THREE_WAY = "three-way-merge"
_THREE_WAY_ALIASES = (_THREE_WAY, "three_way_merge")
_CRDT = "crdt"

ALL_STRATEGIES = (_REJECT, _LWW, _THREE_WAY, _CRDT)


def resolve(
    *,
    strategy: str,
    base: Any,
    current: Any,
    incoming: Any,
    crdt_merge: Callable[[Any, Any], Any] | None = None,
) -> Any:
    """统一入口. 根据 strategy 路由到对应分支.

    base:     双方分叉时的共同祖先值
    current:  store 当前值 (别人 update 过的)
    incoming: 你要写入的新值

    返回: 解决后的值 (caller 拿这个值去 entity.update)
    抛错: cas_version_mismatch / validation_failed / NotImplementedError
    """
    # reject
    if strategy == _REJECT:
        if current != base:
            raise ZeError(
                "cas_version_mismatch",
                "value changed since base; reject strategy",
                context={"base": base, "current": current, "incoming": incoming,
                         "strategy": strategy},
            )
        return incoming

    # last-write-wins
    if strategy in _LWW_ALIASES:
        return incoming

    # three-way-merge
    if strategy in _THREE_WAY_ALIASES:
        return _three_way_merge(base, current, incoming)

    # crdt
    if strategy == _CRDT:
        if crdt_merge is not None:
            return crdt_merge(current, incoming)
        raise NotImplementedError(
            "CRDT strategy requires crdt_merge fn (built-in CRDT engine 待补; "
            "等 03-offline-first 叶子启动)"
        )

    # 未知 strategy
    raise ZeError(
        "validation_failed",
        f"unknown conflict strategy {strategy!r}; expected one of {ALL_STRATEGIES}",
        context={"strategy": strategy},
    )


def _three_way_merge(base: Any, current: Any, incoming: Any) -> Any:
    """经典 3-way merge:
    - 双方都没改 → base
    - 只 current 改 → current
    - 只 incoming 改 → incoming
    - 双方改且值相同 → current (碰巧改一样, 不算冲突)
    - 双方改且值不同 → 抛 cas_version_mismatch
    """
    cur_changed = current != base
    inc_changed = incoming != base

    if not cur_changed and not inc_changed:
        return base
    if cur_changed and not inc_changed:
        return current
    if not cur_changed and inc_changed:
        return incoming
    # 双方都改了
    if current == incoming:
        return current
    raise ZeError(
        "cas_version_mismatch",
        "three-way merge conflict: both sides changed to different values",
        context={"base": base, "current": current, "incoming": incoming,
                 "strategy": _THREE_WAY},
    )
