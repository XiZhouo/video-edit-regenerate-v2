from __future__ import annotations

import pytest

from candidate import (
    CANDIDATE_ENTITY_TYPE,
    CandidateRecord,
    candidate_adopt_operation,
    candidate_reject_operation,
    new_candidate_record,
)
from entity import Actor, Entity, ZeError


def _actor(source: str = "user") -> Actor:
    return Actor(user_id="u1", source=source)


def test_candidate_record_to_entity_roundtrip():
    rec = new_candidate_record(
        parent_entity_id="obj1",
        document_id="level1",
        kind="object_png",
        asset_ids=["asset-a"],
        source_task_id="task1",
        score=0.8,
        candidate_id="cand1",
    )
    entity = rec.to_entity(name="Candidate A")
    assert entity.type == CANDIDATE_ENTITY_TYPE
    assert entity.id == "cand1"
    assert CandidateRecord.from_entity(entity) == rec


def test_candidate_record_rejects_wrong_entity_type():
    with pytest.raises(ZeError) as exc:
        CandidateRecord.from_entity(Entity(type="object", id="x", data={}))
    assert exc.value.code == "validation_failed"


def test_candidate_adopt_operation_updates_target_assets_and_candidate_status():
    candidate = new_candidate_record(
        parent_entity_id="obj1",
        document_id="level1",
        kind="object_png",
        asset_ids=["new-object"],
        candidate_id="cand1",
    ).to_entity()
    target = Entity(type="object", id="obj1", data={"adopted_assets": {"object": "old"}})

    op = candidate_adopt_operation(
        candidate_entity=candidate,
        target_entity=target,
        adopted_assets={"object": "new-object"},
        actor=_actor(),
        session_id="s1",
    )
    assert op.command_id == "candidate.adopt"
    assert op.document_id == "level1"
    assert op.session_id == "s1"
    assert op.patches[0].entity_type == "object"
    assert op.patches[0].field == "adopted_assets"
    assert op.patches[0].after == {"object": "new-object"}
    assert op.patches[1].entity_type == CANDIDATE_ENTITY_TYPE
    assert op.patches[1].after == "adopted"


def test_candidate_adopt_refuses_rejected_candidate():
    candidate = Entity(
        type=CANDIDATE_ENTITY_TYPE,
        id="cand1",
        data={
            "candidate_id": "cand1",
            "parent_entity_id": "obj1",
            "document_id": "level1",
            "kind": "object_png",
            "status": "rejected",
        },
    )
    target = Entity(type="object", id="obj1", data={})
    with pytest.raises(ZeError) as exc:
        candidate_adopt_operation(
            candidate_entity=candidate,
            target_entity=target,
            adopted_assets={},
            actor=_actor(),
        )
    assert exc.value.code == "candidate_not_adoptable"


def test_candidate_reject_operation():
    candidate = new_candidate_record(
        parent_entity_id="obj1",
        document_id="level1",
        kind="object_png",
        candidate_id="cand1",
    ).to_entity()
    op = candidate_reject_operation(candidate_entity=candidate, actor=_actor())
    assert op.command_id == "candidate.reject"
    assert op.document_id == "level1"
    assert op.patches[0].field == "status"
    assert op.patches[0].after == "rejected"


def test_candidate_reject_refuses_adopted_candidate():
    candidate = Entity(
        type=CANDIDATE_ENTITY_TYPE,
        id="cand1",
        data={
            "candidate_id": "cand1",
            "parent_entity_id": "obj1",
            "document_id": "level1",
            "kind": "object_png",
            "status": "adopted",
        },
    )
    with pytest.raises(ZeError) as exc:
        candidate_reject_operation(candidate_entity=candidate, actor=_actor())
    assert exc.value.code == "candidate_already_adopted"
