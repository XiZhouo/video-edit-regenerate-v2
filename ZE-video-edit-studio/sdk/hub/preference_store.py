"""FeaturePreference 持久化(ProviderHub §3.4 内核对象:用户私有默认模型)。

§3.4/§3.7:用户在某工具×能力下的**私有默认模型**(FeaturePreference)是**用户级**偏好,不进项目
文档(共享项目里各人读各自偏好)。resolver 的 `preference` 私有覆盖(优先级 pin>preference>intent>auto)
读它。keyed by (user_id, feature_id, capability) -> model_id。JSON 落盘、线程安全、原子写。

与凭据/项目分离:这里只存「偏好哪个逻辑 model」的字符串,**不存 connection_id、无 secret**——即使
偏好的模型当前不可用,resolver 也会按其规则处理(不静默、如实置灰),偏好只是「想优先用它」的声明。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Optional


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _key(feature_id: str, capability: str) -> str:
    # (feature, capability) 复合键;空格分隔(feature_id/capability 均无空格,不歧义)。
    return f"{feature_id} {capability}"


class PreferenceStore:
    """用户级 AI 模型偏好持久化。get/set/clear 按 (user, feature, capability);访客(空/'__' 前缀 uid)
    永不落盘(返 None / no-op),与凭据/turn 访客门同口径。"""

    def __init__(self, path: "str | Path") -> None:
        self._path = Path(path)
        self._lock = threading.RLock()

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> dict:
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — 缺文件/坏 JSON -> 空(偏好非关键态,不阻断)
            return {"users": {}}

    @staticmethod
    def _is_guest(user_id: str) -> bool:
        return not user_id or user_id.startswith("__")

    def get(self, user_id: str, feature_id: str, capability: str) -> Optional[str]:
        """取用户在 (feature, capability) 下的私有默认 model_id;无 -> None。"""
        if self._is_guest(user_id):
            return None
        with self._lock:
            return (self._load().get("users", {}).get(user_id, {}) or {}).get(_key(feature_id, capability))

    def set(self, user_id: str, feature_id: str, capability: str, model_id: str) -> None:
        """设私有默认(空 model_id -> 等价 clear)。访客 no-op。"""
        if self._is_guest(user_id):
            return
        if not model_id:
            return self.clear(user_id, feature_id, capability)
        with self._lock:
            state = self._load()
            state.setdefault("users", {}).setdefault(user_id, {})[_key(feature_id, capability)] = model_id
            _atomic_write_json(self._path, state)

    def clear(self, user_id: str, feature_id: str, capability: str) -> None:
        """清除某 (feature, capability) 偏好(回到 auto/意图)。访客 no-op。"""
        if self._is_guest(user_id):
            return
        with self._lock:
            state = self._load()
            umap = state.get("users", {}).get(user_id, {})
            if umap.pop(_key(feature_id, capability), None) is not None:
                _atomic_write_json(self._path, state)

    def list_for_user(self, user_id: str) -> dict:
        """列出用户全部偏好 {"feature_id capability": model_id}(诊断/设置页用)。访客 -> {}。"""
        if self._is_guest(user_id):
            return {}
        with self._lock:
            return dict(self._load().get("users", {}).get(user_id, {}) or {})
