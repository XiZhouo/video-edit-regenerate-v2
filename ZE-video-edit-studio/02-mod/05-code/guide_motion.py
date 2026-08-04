"""矩形与图片引导的位置关键帧。

关键帧时间相对引导自己的 ``start``，这样移动整条时间范围时不需要重写所有点。
区间内做线性插值，区间外保持最近一帧；没有关键帧时完全保持旧项目的静态几何。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def _number(value: Any, fallback: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _geometry(frame: Mapping[str, Any]) -> dict[str, float]:
    return {
        "x": _number(frame.get("bboxX")),
        "y": _number(frame.get("bboxY")),
        "w": _number(frame.get("bboxW")),
        "h": _number(frame.get("bboxH")),
    }


def resolve_guide_geometry(
    base: Mapping[str, Any],
    keyframes: Sequence[Mapping[str, Any]],
    local_time: float,
) -> dict[str, float]:
    """返回 ``local_time`` 时刻的 ``{x,y,w,h}`` 几何。"""
    if not keyframes:
        return {
            "x": _number(base.get("x")),
            "y": _number(base.get("y")),
            "w": _number(base.get("w")),
            "h": _number(base.get("h")),
        }
    ordered = sorted(keyframes, key=lambda frame: _number(frame.get("time")))
    if local_time <= _number(ordered[0].get("time")):
        return _geometry(ordered[0])
    if local_time >= _number(ordered[-1].get("time")):
        return _geometry(ordered[-1])
    for index, right in enumerate(ordered[1:], start=1):
        right_time = _number(right.get("time"))
        if local_time > right_time:
            continue
        left = ordered[index - 1]
        left_time = _number(left.get("time"))
        progress = (local_time - left_time) / max(1e-12, right_time - left_time)
        before = _geometry(left)
        after = _geometry(right)
        return {
            key: before[key] + (after[key] - before[key]) * progress
            for key in ("x", "y", "w", "h")
        }
    return _geometry(ordered[-1])
