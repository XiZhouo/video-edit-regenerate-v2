"""change_set — Operation.patches → entity 级 EntityChange 聚合.

完整 SPEC 见同目录 CHANGE-FLOW.md §4.

已实装 (无跨叶子依赖):
  - EntityChange: entity_type / entity_id / change_kind / changed_fields / patches / snapshot
  - CHANGE_KINDS = (created | updated | soft_deleted | restored | hard_removed)
  - affected_entities(op, doc) -> list[EntityChange]
  - to_broadcast_payload(op, changes) -> dict (前端消息契约格式)

待补 (跨叶子):
  - 真 broadcast 落 channel    [01-realtime-sync]
  - 前端订阅 API 实施          [04-user-facing]
"""

from __future__ import annotations

import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# 共享: 从兄弟目录 import operation (Operation, Patch, ENTITY_META_FIELD)
_OPERATION_DIR = Path(__file__).resolve().parent.parent / "03-commands-operations" / "02-operation"
if str(_OPERATION_DIR) not in sys.path:
    sys.path.insert(0, str(_OPERATION_DIR))

from operation import Operation, Patch, ENTITY_META_FIELD, DOCUMENT_TARGET  # noqa: E402


CHANGE_KINDS = ("created", "updated", "soft_deleted", "restored", "hard_removed")


# ==============================================================
# EntityChange
# ==============================================================

@dataclass(frozen=True)
class EntityChange:
    """entity 级变更通知 — 前端按 (entity_type, entity_id) 路由到渲染单元.

    snapshot: entity after 状态 (hard_removed 时为 None)
    changed_fields: data 字段名清单; 元操作时含 "__entity__"
    patches: 字段级原始 patches (前端可选用做更细粒度优化)
    """
    entity_type: str
    entity_id: str
    change_kind: str
    changed_fields: tuple[str, ...]
    patches: tuple[Patch, ...]
    snapshot: dict[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "change_kind": self.change_kind,
            "changed_fields": list(self.changed_fields),
            "patches": [p.to_dict() for p in self.patches],
            "snapshot": dict(self.snapshot) if self.snapshot is not None else None,
        }


# ==============================================================
# 推导 change_kind
# ==============================================================

def _derive_change_kind(patches: list[Patch]) -> str:
    """从一组同 (entity_type, entity_id) 的 patches 推导 change_kind.

    优先级 (出现即取):
      create > delete > soft_delete > restore > (其他都视为 updated)
    """
    ops = [p.op for p in patches]
    if "create" in ops:
        return "created"
    if "delete" in ops:
        return "hard_removed"
    if "soft_delete" in ops:
        return "soft_deleted"
    if "restore" in ops:
        return "restored"
    return "updated"


def _collect_changed_fields(patches: list[Patch]) -> tuple[str, ...]:
    """收集这组 patches 涉及的字段名清单 (去重 + sorted; 含 __entity__)."""
    fields = {p.field for p in patches}
    return tuple(sorted(fields))


# ==============================================================
# affected_entities
# ==============================================================

def affected_entities(op: Operation, doc: Any) -> list[EntityChange]:
    """从 Operation.patches 聚合出 entity 级变更.

    doc: Document 实例; 用于取 entity 当前 snapshot.
         hard_removed 的 entity 在 doc 里已经不存在, snapshot 用 patch.before 兜底.

    返回顺序: 按 (entity_type, entity_id) sorted (确定性, 测试友好).
    """
    # 按 (entity_type, entity_id) 分组
    groups: dict[tuple[str, str], list[Patch]] = defaultdict(list)
    for p in op.patches:
        groups[(p.entity_type, p.entity_id)].append(p)

    changes: list[EntityChange] = []
    for (et, eid), patches in sorted(groups.items()):
        change_kind = _derive_change_kind(patches)
        changed_fields = _collect_changed_fields(patches)
        snapshot = _resolve_snapshot(change_kind, et, eid, patches, doc)
        changes.append(EntityChange(
            entity_type=et,
            entity_id=eid,
            change_kind=change_kind,
            changed_fields=changed_fields,
            patches=tuple(patches),
            snapshot=snapshot,
        ))
    return changes


def _resolve_snapshot(
    change_kind: str,
    entity_type: str,
    entity_id: str,
    patches: list[Patch],
    doc: Any,
) -> dict[str, Any] | None:
    """根据 change_kind 决定 snapshot 怎么拿.

    - hard_removed: None (entity 已不存在; caller 用 patch.before 自取)
    - 其他: doc.find(type, id).to_dict() 取当前 after 状态
            doc 找不到时 fallback 到 patch 的 after 字段 (元操作场景)
    """
    if change_kind == "hard_removed":
        return None

    # 文档级变更(__document__):snapshot = doc 元数据(name/version/...),前端据此更新 meta
    if entity_type == DOCUMENT_TARGET:
        if doc is not None and hasattr(doc, "meta_dict"):
            return doc.meta_dict()
        # fallback:从 patch.after 兜底
        for p in patches:
            if isinstance(p.after, dict):
                return dict(p.after)
        return None

    # 从 doc 取当前 entity 状态
    if doc is not None and hasattr(doc, "find"):
        e = doc.find(entity_type, entity_id)
        if e is not None:
            return e.to_dict() if hasattr(e, "to_dict") else dict(e)

    # fallback: 从元操作 patch.after 取 (create / soft_delete / restore)
    for p in patches:
        if p.field == ENTITY_META_FIELD and isinstance(p.after, dict):
            return dict(p.after)
    return None


# ==============================================================
# broadcast payload (前端消息契约)
# ==============================================================

def to_broadcast_payload(
    op: Operation,
    changes: list[EntityChange],
    *,
    doc_id: str | None = None,
) -> dict[str, Any]:
    """组装给前端的 broadcast 消息. type='entity_change'.

    格式见 CHANGE-FLOW.md §4.5.
    """
    return {
        "type": "entity_change",
        "doc_id": doc_id,
        "op_id": op.id,
        "trace_id": op.trace_id,
        "source": op.source,
        "actor": dict(op.actor),
        "ts": op.ts,
        "command_id": op.command_id,
        "changes": [c.to_dict() for c in changes],
    }


# ==============================================================
# Block1 加载 / 管线 / 占位:两个新 broadcast envelope
#
# 跟 entity_change 走同一条 broker / SSE 通道(不另开 channel)。两者都不带
# `changes` 键 —— broker 对空 changes 的 payload 走"听一切 / doc 订阅"分发,
# server.py 的 SSE 订阅(全 None 过滤)因此都能收到。前端按 payload.type 分支。
# 格式见 CHANGE-FLOW.md §4.6 / §4.7。
# ==============================================================

def to_status_payload(
    statuses: list[dict[str, Any]],
    *,
    doc_id: str | None = None,
    run_id: str | None = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """entity 级运行态变更(占位 / 进度 / 错误)。statuses = [EntityRunStatus.to_dict(), ...]。"""
    return {
        "type": "entity_status",
        "doc_id": doc_id,
        "run_id": run_id,
        "trace_id": trace_id,
        "statuses": list(statuses),
    }


def to_pipeline_status_payload(
    runs: list[dict[str, Any]],
    *,
    doc_id: str | None = None,
) -> dict[str, Any]:
    """聚合 pipeline run 状态(footer pipeline 面板)。runs = [PipelineRun 形态 dict, ...]。"""
    return {
        "type": "pipeline_status",
        "doc_id": doc_id,
        "runs": list(runs),
    }
