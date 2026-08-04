"""DiscoveredModelStore(ProviderHub 回收 P4:/models 动态发现结果的**独立分层**落库)。

原型 provider.discover 从 provider 端点拉 /models 动态发现模型。忠实回收的**铁律**(教义①「模型真相
唯一权威」):发现的模型**绝不写进 descriptors 的 canonical 静态表**、**不喂 resolver 候选**
(models_for_capability 只读静态表)——只落**独立分层** store,标 source='discovered'、caps=[]
(即便被 surface 也满足不了任何 require,天然不参与自动拾取)。这是「用户自填 base_url 的兼容端点」
唯一能知道自己有哪些模型的方式,同时不污染 Build 拥有的模型真相。

keyed by (user_id, credential_id) -> [{model_id, source:'discovered', caps:[], discovered_at}]。
JSON 原子写、线程安全、访客(空/'__' uid)no-op(同 preference/lane 访客门)。复用 preference_store 原子写。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Optional

from .preference_store import _atomic_write_json


class DiscoveredModelStore:
    """动态发现模型的用户级分层持久化。record 覆盖某 credential 的发现集(新发现替换旧);
    list 读回;clear 删。访客 no-op/空。发现集与 canonical 静态表物理隔离,不参与 resolver 拾取。"""

    def __init__(self, path: "str | Path") -> None:
        self._path = Path(path)
        self._lock = threading.RLock()

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> dict:
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — 缺文件/坏 JSON -> 空(发现是缓存态,非关键,不阻断)
            return {"users": {}}

    @staticmethod
    def _is_guest(user_id: str) -> bool:
        return not user_id or user_id.startswith("__")

    def record(self, user_id: str, credential_id: str, model_ids: "list[str] | tuple[str, ...]",
               *, discovered_at: Optional[float] = None) -> list[dict]:
        """覆盖某 credential 的发现集(去重保序;每模型 → source='discovered'/caps=[])。
        空 model_ids -> clear。访客 no-op(返 [])。返回落库后的条目列表。"""
        if self._is_guest(user_id) or not credential_id:
            return []
        seen: set[str] = set()
        ts = time.time() if discovered_at is None else discovered_at
        entries = [{"model_id": m, "source": "discovered", "caps": [], "discovered_at": ts}
                   for m in model_ids
                   if isinstance(m, str) and m and not (m in seen or seen.add(m))]
        if not entries:
            self.clear(user_id, credential_id)
            return []
        with self._lock:
            state = self._load()
            state.setdefault("users", {}).setdefault(user_id, {})[credential_id] = entries
            _atomic_write_json(self._path, state)
        return entries

    def list_for_credential(self, user_id: str, credential_id: str) -> list[dict]:
        """取某 credential 的发现集;无 -> []。访客 -> []。"""
        if self._is_guest(user_id):
            return []
        with self._lock:
            got = (self._load().get("users", {}).get(user_id, {}) or {}).get(credential_id)
            return list(got) if isinstance(got, list) else []

    def list_for_user(self, user_id: str) -> dict:
        """列出用户全部发现集 {credential_id: [entry...]}(设置/诊断页用)。访客 -> {}。"""
        if self._is_guest(user_id):
            return {}
        with self._lock:
            return {k: list(v) for k, v in (self._load().get("users", {}).get(user_id, {}) or {}).items()}

    def clear(self, user_id: str, credential_id: str) -> None:
        """删某 credential 的发现集(如凭据删除/重登记时)。访客 no-op。"""
        if self._is_guest(user_id):
            return
        with self._lock:
            state = self._load()
            umap = state.get("users", {}).get(user_id, {})
            if umap.pop(credential_id, None) is not None:
                _atomic_write_json(self._path, state)
