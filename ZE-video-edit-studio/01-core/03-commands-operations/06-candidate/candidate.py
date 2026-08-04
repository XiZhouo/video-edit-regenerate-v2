"""Candidate protocol — generated results that must be adopted explicitly.

AI-heavy tools often produce several possible outputs. Core should not know what
an object/background/crop means, but it can standardize the lifecycle:
generated -> adopted/rejected/stale, with adopt/reject represented as Operations.
"""

from __future__ import annotations

import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_CORE = _HERE.parent.parent
_PATHS = [
    _CORE / "01-data-structure" / "02-entity",
    _CORE / "03-commands-operations" / "02-operation",
]
for _p in _PATHS:
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from entity import Actor, Entity, ZeError  # noqa: E402
from operation import Patch, new_operation  # noqa: E402


CANDIDATE_ENTITY_TYPE = "__candidate__"
CANDIDATE_STATUSES = ("generated", "adopted", "rejected", "stale")


@dataclass(frozen=True)
class CandidateRecord:
    """Generic candidate data stored inside an Entity(type='__candidate__')."""

    candidate_id: str
    parent_entity_id: str
    document_id: str
    kind: str
    asset_ids: list[str] = field(default_factory=list)
    status: str = "generated"
    source_task_id: str = ""
    score: float | None = None
    created_at: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.candidate_id:
            raise ValueError("candidate_id is required")
        if not self.parent_entity_id:
            raise ValueError("parent_entity_id is required")
        if not self.document_id:
            raise ValueError("document_id is required")
        if not self.kind:
            raise ValueError("kind is required")
        if self.status not in CANDIDATE_STATUSES:
            raise ValueError(f"invalid candidate status: {self.status}")
        if not self.created_at:
            object.__setattr__(self, "created_at", time.time())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_entity(self, *, name: str = "") -> Entity:
        return Entity(
            type=CANDIDATE_ENTITY_TYPE,
            id=self.candidate_id,
            name=name or self.kind,
            data=self.to_dict(),
        )

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CandidateRecord":
        return cls(
            candidate_id=d["candidate_id"],
            parent_entity_id=d["parent_entity_id"],
            document_id=d["document_id"],
            kind=d["kind"],
            asset_ids=list(d.get("asset_ids") or []),
            status=d.get("status", "generated"),
            source_task_id=d.get("source_task_id", ""),
            score=d.get("score"),
            created_at=float(d.get("created_at", 0.0)),
            meta=dict(d.get("meta") or {}),
        )

    @classmethod
    def from_entity(cls, entity: Entity) -> "CandidateRecord":
        if entity.type != CANDIDATE_ENTITY_TYPE:
            raise ZeError(
                "validation_failed",
                f"expected {CANDIDATE_ENTITY_TYPE} entity",
                context={"entity_type": entity.type, "entity_id": entity.id},
            )
        return cls.from_dict(entity.data)


def new_candidate_record(
    *,
    parent_entity_id: str,
    document_id: str,
    kind: str,
    asset_ids: list[str] | None = None,
    source_task_id: str = "",
    score: float | None = None,
    meta: dict[str, Any] | None = None,
    candidate_id: str | None = None,
) -> CandidateRecord:
    return CandidateRecord(
        candidate_id=candidate_id or uuid.uuid4().hex,
        parent_entity_id=parent_entity_id,
        document_id=document_id,
        kind=kind,
        asset_ids=list(asset_ids or []),
        source_task_id=source_task_id,
        score=score,
        meta=dict(meta or {}),
    )


def candidate_adopt_operation(
    *,
    candidate_entity: Entity,
    target_entity: Entity,
    adopted_assets: dict[str, Any],
    actor: Actor,
    adopted_assets_field: str = "adopted_assets",
    command_id: str = "candidate.adopt",
    group_id: str = "",
    trace_id: str = "",
    document_id: str = "",
    session_id: str = "",
):
    """Build an Operation that adopts a candidate into a target entity.

    Caller applies patches via Document.apply_patch_directly or command handler,
    then appends the returned Operation to OpLog.
    """
    record = CandidateRecord.from_entity(candidate_entity)
    if record.status not in ("generated", "stale"):
        raise ZeError(
            "candidate_not_adoptable",
            f"candidate status {record.status!r} cannot be adopted",
            context={"candidate_id": record.candidate_id, "status": record.status},
        )
    before_assets = target_entity.get(adopted_assets_field, {})
    before_candidate = candidate_entity.data
    patches = [
        Patch(
            target_entity.type,
            target_entity.id,
            adopted_assets_field,
            "set",
            before_assets,
            dict(adopted_assets),
        ),
        Patch(
            CANDIDATE_ENTITY_TYPE,
            candidate_entity.id,
            "status",
            "set",
            before_candidate.get("status"),
            "adopted",
        ),
    ]
    reverse = [
        Patch(
            CANDIDATE_ENTITY_TYPE,
            candidate_entity.id,
            "status",
            "set",
            "adopted",
            before_candidate.get("status"),
        ),
        Patch(
            target_entity.type,
            target_entity.id,
            adopted_assets_field,
            "set",
            dict(adopted_assets),
            before_assets,
        ),
    ]
    # Store richer adopted_at/meta through status patch's sibling field when callers want it.
    # Keep the mandatory operation minimal and field-level so generic rollback stays simple.
    return new_operation(
        command_id=command_id,
        source=actor.source,
        actor=actor.to_dict(),
        patches=patches,
        reverse_patches=reverse,
        group_id=group_id,
        trace_id=trace_id,
        document_id=document_id or record.document_id,
        session_id=session_id,
    )


def candidate_reject_operation(
    *,
    candidate_entity: Entity,
    actor: Actor,
    command_id: str = "candidate.reject",
    group_id: str = "",
    trace_id: str = "",
    document_id: str = "",
    session_id: str = "",
):
    record = CandidateRecord.from_entity(candidate_entity)
    before_status = record.status
    if before_status == "adopted":
        raise ZeError(
            "candidate_already_adopted",
            f"adopted candidate {record.candidate_id!r} cannot be rejected",
            context={"candidate_id": record.candidate_id},
        )
    patch = Patch(
        CANDIDATE_ENTITY_TYPE,
        candidate_entity.id,
        "status",
        "set",
        before_status,
        "rejected",
    )
    reverse = Patch(
        CANDIDATE_ENTITY_TYPE,
        candidate_entity.id,
        "status",
        "set",
        "rejected",
        before_status,
    )
    return new_operation(
        command_id=command_id,
        source=actor.source,
        actor=actor.to_dict(),
        patches=[patch],
        reverse_patches=[reverse],
        group_id=group_id,
        trace_id=trace_id,
        document_id=document_id or record.document_id,
        session_id=session_id,
    )


__all__ = [
    "CANDIDATE_ENTITY_TYPE",
    "CANDIDATE_STATUSES",
    "CandidateRecord",
    "new_candidate_record",
    "candidate_adopt_operation",
    "candidate_reject_operation",
]
