"""CapabilityLane 持久化(ProviderHub §3.4 内核对象:用户对某能力的**全局优先序**)。

§3.4:CapabilityLane = 用户对某能力(如 image)显式的**模型优先顺序** + auto fallback。与 FeaturePreference
(per-工具默认)区别:lane 是**跨工具、按能力**的全局偏好序。resolver 五级优先(pin>preference>lane>intent>auto)
里的第三层:按 lane 声明序取首个可用模型。keyed by (user_id, capability) -> [model_id...]。

只存逻辑 model 序(无 connection_id、无 secret);访客(空/'__' uid)永不落盘;lane 里不可用的模型
resolver 自然跳过(取首个可用),不静默、不报错。复用 PreferenceStore 的原子写/访客门口径。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from .preference_store import _atomic_write_json


class CapabilityLaneStore:
    """用户级能力泳道持久化。get 返有序 tuple;set 覆盖整条泳道;clear 删。访客 no-op/空。"""

    def __init__(self, path: "str | Path") -> None:
        self._path = Path(path)
        self._lock = threading.RLock()

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> dict:
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — 缺文件/坏 JSON -> 空(非关键态,不阻断)
            return {"users": {}}

    @staticmethod
    def _is_guest(user_id: str) -> bool:
        return not user_id or user_id.startswith("__")

    def get(self, user_id: str, capability: str) -> "tuple[str, ...]":
        """取用户在某能力下的泳道(有序 model_id tuple);无 -> ()。"""
        if self._is_guest(user_id):
            return ()
        with self._lock:
            lane = (self._load().get("users", {}).get(user_id, {}) or {}).get(capability)
            return tuple(lane) if isinstance(lane, list) else ()

    def set(self, user_id: str, capability: str, models: "list[str] | tuple[str, ...]") -> None:
        """覆盖整条泳道(去重保序;空 -> clear)。访客 no-op。"""
        if self._is_guest(user_id):
            return
        seen: set[str] = set()
        ordered = [m for m in models if isinstance(m, str) and m and not (m in seen or seen.add(m))]
        if not ordered:
            return self.clear(user_id, capability)
        with self._lock:
            state = self._load()
            state.setdefault("users", {}).setdefault(user_id, {})[capability] = ordered
            _atomic_write_json(self._path, state)

    def clear(self, user_id: str, capability: str) -> None:
        """删某能力泳道(回到 preference/intent/auto)。访客 no-op。"""
        if self._is_guest(user_id):
            return
        with self._lock:
            state = self._load()
            umap = state.get("users", {}).get(user_id, {})
            if umap.pop(capability, None) is not None:
                _atomic_write_json(self._path, state)

    def list_for_user(self, user_id: str) -> dict:
        """列出用户全部能力泳道 {capability: [model...]}(设置页/诊断用)。访客 -> {}。"""
        if self._is_guest(user_id):
            return {}
        with self._lock:
            return {k: list(v) for k, v in (self._load().get("users", {}).get(user_id, {}) or {}).items()}
