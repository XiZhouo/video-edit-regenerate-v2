"""DocumentIndex — a lightweight index over many Documents.

用于 seek-find 这类"一个项目里有很多关卡/背景"的工作台:
Index 只保存 document 引用、缩略图、状态和聚合信息;每个关卡/背景自己的
entities/oplog/checkpoints/tasks 都留在独立 Document 内。
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class DocumentIndexEntry:
    """Index 中的一行 Document 引用."""

    document_id: str
    kind: str
    label: str
    thumbnail_asset_id: str = ""
    source_path: str = ""
    status: str = "ready"
    updated_at: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.document_id:
            raise ValueError("document_id is required")
        if not self.kind:
            raise ValueError("kind is required")
        if not self.label:
            raise ValueError("label is required")
        if self.status not in ("empty", "ready", "running", "error", "archived"):
            raise ValueError(f"invalid document index status: {self.status}")
        if not self.updated_at:
            object.__setattr__(self, "updated_at", time.time())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DocumentIndexEntry":
        return cls(
            document_id=d["document_id"],
            kind=d["kind"],
            label=d["label"],
            thumbnail_asset_id=d.get("thumbnail_asset_id", ""),
            source_path=d.get("source_path", ""),
            status=d.get("status", "ready"),
            updated_at=float(d.get("updated_at", 0.0)),
            meta=dict(d.get("meta") or {}),
        )


class DocumentIndex:
    """一组 Documents 的可持久化索引."""

    def __init__(
        self,
        *,
        kind: str,
        title: str,
        index_id: str | None = None,
        aggregate_status: dict[str, Any] | None = None,
    ) -> None:
        if not kind:
            raise ValueError("kind is required")
        if not title:
            raise ValueError("title is required")
        self._index_id = index_id or uuid.uuid4().hex
        self._kind = kind
        self._title = title
        self._aggregate_status = dict(aggregate_status or {})
        self._entries: dict[str, DocumentIndexEntry] = {}
        self._created_at = time.time()
        self._updated_at = self._created_at

    @property
    def index_id(self) -> str:
        return self._index_id

    @property
    def kind(self) -> str:
        return self._kind

    @property
    def title(self) -> str:
        return self._title

    @property
    def aggregate_status(self) -> dict[str, Any]:
        return dict(self._aggregate_status)

    @property
    def created_at(self) -> float:
        return self._created_at

    @property
    def updated_at(self) -> float:
        return self._updated_at

    def add(self, entry: DocumentIndexEntry) -> DocumentIndexEntry:
        if entry.document_id in self._entries:
            raise ValueError(f"document {entry.document_id!r} already exists in index")
        self._entries[entry.document_id] = entry
        self._touch()
        return entry

    def upsert(self, entry: DocumentIndexEntry) -> DocumentIndexEntry:
        self._entries[entry.document_id] = entry
        self._touch()
        return entry

    def remove(self, document_id: str) -> DocumentIndexEntry | None:
        old = self._entries.pop(document_id, None)
        if old is not None:
            self._touch()
        return old

    def get(self, document_id: str) -> DocumentIndexEntry | None:
        return self._entries.get(document_id)

    def list(self, *, include_archived: bool = False) -> list[DocumentIndexEntry]:
        rows = list(self._entries.values())
        if not include_archived:
            rows = [r for r in rows if r.status != "archived"]
        return sorted(rows, key=lambda r: (r.label, r.document_id))

    def set_aggregate_status(self, status: dict[str, Any]) -> None:
        self._aggregate_status = dict(status)
        self._touch()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "ze-document-index",
            "index_id": self._index_id,
            "kind": self._kind,
            "title": self._title,
            "aggregate_status": dict(self._aggregate_status),
            "documents": [e.to_dict() for e in self.list(include_archived=True)],
            "created_at": self._created_at,
            "updated_at": self._updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DocumentIndex":
        idx = cls(
            kind=d["kind"],
            title=d["title"],
            index_id=d.get("index_id"),
            aggregate_status=d.get("aggregate_status") or {},
        )
        idx._created_at = float(d.get("created_at", idx._created_at))
        idx._updated_at = float(d.get("updated_at", idx._updated_at))
        for row in d.get("documents") or []:
            entry = DocumentIndexEntry.from_dict(row)
            idx._entries[entry.document_id] = entry
        return idx

    def save(self, path: str | os.PathLike) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
        os.replace(tmp, p)

    @classmethod
    def open(cls, path: str | os.PathLike) -> "DocumentIndex":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if raw.get("schema") != "ze-document-index":
            raise ValueError("not a ze-document-index file")
        return cls.from_dict(raw)

    def _touch(self) -> None:
        self._updated_at = time.time()


__all__ = [
    "DocumentIndex",
    "DocumentIndexEntry",
]
