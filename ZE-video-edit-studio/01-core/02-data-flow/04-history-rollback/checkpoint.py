"""Checkpoint — named document snapshots for realtime_checkpoint collaboration.

完整 SPEC 见同目录 SPEC.md §6.

当前实现是可用的整文档快照:
  - create(doc, name, actor=...) 序列化 Document meta + 全部 Entity
  - restore(name) 从快照恢复一个新的 Document 实例
  - list() 扫 checkpoints/*.json
  - fork(name, new_name) 复制快照元数据与 snapshot

后续可把 snapshot payload 换成 AssetStore 内容寻址增量,但上层 API 不需要变。
"""

from __future__ import annotations

import copy
import json
import re
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_CORE = _HERE.parent.parent
_PATHS = [
    _CORE / "01-data-structure" / "01-document",
    _CORE / "01-data-structure" / "02-entity",
]
for _p in _PATHS:
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from document import Document  # noqa: E402
from entity import Actor, Entity, ZeError  # noqa: E402


_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


@dataclass
class CheckpointMeta:
    """命名快照元数据.

    name 是人/CLI 可读 id; checkpoint_id 是不可变 uuid。op_log_position 表示创建
    快照时调用方看到的 OpLog 长度。summary 给 UI/AI 快速理解快照范围。
    """

    name: str
    created_at: float
    actor: dict[str, Any]
    op_log_position: int
    summary: dict[str, Any] = field(default_factory=dict)
    checkpoint_id: str = ""
    document_id: str = ""
    document_version: int = 0
    reason: str = "manual"
    session_id: str = ""
    group_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CheckpointMeta":
        return cls(
            name=d["name"],
            created_at=float(d["created_at"]),
            actor=dict(d.get("actor") or {}),
            op_log_position=int(d.get("op_log_position", 0)),
            summary=dict(d.get("summary") or {}),
            checkpoint_id=d.get("checkpoint_id", ""),
            document_id=d.get("document_id", ""),
            document_version=int(d.get("document_version", 0)),
            reason=d.get("reason", "manual"),
            session_id=d.get("session_id", ""),
            group_id=d.get("group_id", ""),
        )


class CheckpointManager:
    """命名快照管理器.

    doc_dir 指 Document 目录,快照写在 `<doc_dir>/checkpoints/<name>.json`。
    """

    def __init__(self, doc_dir: str | Path | None = None) -> None:
        self._doc_dir = Path(doc_dir) if doc_dir else None

    def create(
        self,
        doc: Document,
        name: str,
        *,
        actor: Actor,
        op_log_position: int = 0,
        reason: str = "manual",
        session_id: str = "",
        group_id: str = "",
    ) -> CheckpointMeta:
        """创建命名快照."""
        path = self._path_for(name)
        if path.exists():
            raise ZeError(
                "checkpoint_exists",
                f"checkpoint {name!r} already exists",
                context={"name": name},
            )
        meta = CheckpointMeta(
            name=name,
            checkpoint_id=uuid.uuid4().hex,
            created_at=time.time(),
            actor=actor.to_dict(),
            op_log_position=op_log_position,
            document_id=doc.id,
            document_version=doc.version,
            reason=reason,
            session_id=session_id,
            group_id=group_id,
            summary=_summarize_doc(doc),
        )
        payload = {"meta": meta.to_dict(), "snapshot": _snapshot_doc(doc)}
        self._write_json(path, payload)
        return meta

    def restore(self, name: str) -> Document:
        """从快照恢复 Document. 返回新的 Document 实例,不自动覆盖磁盘当前文档."""
        payload = self._read_payload(name)
        return _restore_doc(payload["snapshot"])

    def list(self) -> list[CheckpointMeta]:
        """列出全部命名快照,按创建时间升序."""
        root = self._checkpoint_dir()
        if not root.exists():
            return []
        out: list[CheckpointMeta] = []
        for p in sorted(root.glob("*.json")):
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
                out.append(CheckpointMeta.from_dict(raw["meta"]))
            except Exception:
                raise ZeError(
                    "checkpoint_corrupt",
                    f"checkpoint file is not readable: {p}",
                    context={"path": str(p)},
                )
        return sorted(out, key=lambda m: (m.created_at, m.name))

    def fork(self, name: str, new_name: str) -> CheckpointMeta:
        """从已有 checkpoint 派生一份新 checkpoint."""
        src = self._read_payload(name)
        path = self._path_for(new_name)
        if path.exists():
            raise ZeError(
                "checkpoint_exists",
                f"checkpoint {new_name!r} already exists",
                context={"name": new_name},
            )
        meta = CheckpointMeta.from_dict(src["meta"])
        forked = copy.deepcopy(src)
        meta.name = new_name
        meta.checkpoint_id = uuid.uuid4().hex
        meta.created_at = time.time()
        meta.reason = f"fork:{name}"
        forked["meta"] = meta.to_dict()
        self._write_json(path, forked)
        return meta

    def _checkpoint_dir(self) -> Path:
        if self._doc_dir is None:
            raise ZeError(
                "validation_failed",
                "CheckpointManager requires doc_dir for persistent checkpoints",
            )
        return self._doc_dir / "checkpoints"

    def _path_for(self, name: str) -> Path:
        _validate_name(name)
        return self._checkpoint_dir() / f"{name}.json"

    def _read_payload(self, name: str) -> dict[str, Any]:
        path = self._path_for(name)
        if not path.exists():
            raise ZeError(
                "checkpoint_not_found",
                f"checkpoint {name!r} not found",
                context={"name": name},
            )
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            raise ZeError(
                "checkpoint_corrupt",
                f"checkpoint file is not valid JSON: {path}",
                context={"path": str(path)},
            )

    @staticmethod
    def _write_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        tmp.replace(path)


def _validate_name(name: str) -> None:
    if not isinstance(name, str) or not name or not _SAFE_NAME.match(name):
        raise ZeError(
            "validation_failed",
            "checkpoint name must match [A-Za-z0-9._-]+",
            context={"name": repr(name)},
        )


def _snapshot_doc(doc: Document) -> dict[str, Any]:
    entities: dict[str, list[dict[str, Any]]] = {}
    for entity_type in doc.types():
        entities[entity_type] = [
            e.to_dict() for e in doc.list(entity_type, include_deleted=True)
        ]
    return {
        "schema_version": doc.schema_version,
        "collaboration_mode": doc.collaboration_mode,
        # 保存策略是文档语义，不是运行时偏好；checkpoint restore / fork 必须保留。
        # 旧 checkpoint 不含该键时仍按 Document 的兼容映射打开。
        "save_policy": doc.save_policy,
        "id": doc.id,
        "name": doc.name,
        "data_schema": doc.schema,
        "config": doc.config,
        "acl": doc.acl,
        "version": doc.version,
        "created_at": doc.created_at,
        "updated_at": doc.updated_at,
        "entities": entities,
    }


def _restore_doc(snapshot: dict[str, Any]) -> Document:
    doc = Document(
        schema_version=snapshot["schema_version"],
        collaboration_mode=snapshot["collaboration_mode"],
        name=snapshot.get("name", ""),
        id=snapshot.get("id"),
        schema=snapshot.get("data_schema") or {},
        config=snapshot.get("config") or {},
        acl=snapshot.get("acl") or {},
        save_policy=snapshot.get("save_policy"),
    )
    doc._v = int(snapshot.get("version", 1))
    doc._created_at = float(snapshot.get("created_at", doc.created_at))
    doc._updated_at = float(snapshot.get("updated_at", doc.updated_at))
    for entity_type, rows in (snapshot.get("entities") or {}).items():
        schema = doc.schema.get(entity_type) if isinstance(doc.schema.get(entity_type), dict) else None
        for raw in rows:
            doc._add_entity_internal(Entity.from_dict(raw, schema=schema))
    return doc


def _summarize_doc(doc: Document) -> dict[str, Any]:
    counts = {
        entity_type: len(doc.list(entity_type, include_deleted=True))
        for entity_type in doc.types()
    }
    return {
        "entity_types": sorted(counts.keys()),
        "entity_count": sum(counts.values()),
        "entity_counts": counts,
    }


__all__ = [
    "CheckpointMeta",
    "CheckpointManager",
]
