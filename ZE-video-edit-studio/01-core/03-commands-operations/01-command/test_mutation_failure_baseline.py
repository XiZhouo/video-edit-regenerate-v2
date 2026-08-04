"""W04 hard gates for handler failures after an in-memory mutation.

These started as W00 strict xfails.  A handler may mutate before it discovers an
error, but CommandRegistry must now restore the exact pre-dispatch state.
"""
from __future__ import annotations

from copy import deepcopy

import pytest

from command import CommandRegistry, CommandSpec
from document import Document
from entity import Actor, Entity, ZeError


def _doc() -> Document:
    doc = Document(schema_version="1.0.0", collaboration_mode="with_save")
    doc.add(Entity(type="t", data={"x": 1}, id="e1"))
    return doc


def test_handler_exception_rolls_back_document_state():
    doc = _doc()
    before = deepcopy(doc.get("t", "e1").to_dict())
    reg = CommandRegistry()

    def execute(args, actor, source, doc):
        ent = doc.get("t", "e1")
        ent.update({"x": 2}, expected_version=ent.version, actor=actor)
        raise RuntimeError("failed after mutation")

    reg.register(CommandSpec(id="baseline.fail", label="fail", execute=execute))
    with pytest.raises(RuntimeError, match="failed after mutation"):
        reg.dispatch(
            "baseline.fail", {}, actor=Actor(user_id="u"), source="user", doc=doc,
        )

    assert doc.get("t", "e1").to_dict() == before

def test_invalid_operation_result_rolls_back_document_state():
    doc = _doc()
    before = deepcopy(doc.get("t", "e1").to_dict())
    reg = CommandRegistry()

    def execute(args, actor, source, doc):
        ent = doc.get("t", "e1")
        ent.update({"x": 2}, expected_version=ent.version, actor=actor)
        return {"not": "an operation"}

    reg.register(CommandSpec(id="baseline.invalid", label="invalid", execute=execute))
    with pytest.raises(ZeError) as exc:
        reg.dispatch(
            "baseline.invalid", {}, actor=Actor(user_id="u"), source="user", doc=doc,
        )
    assert exc.value.code == "validation_failed"

    assert doc.get("t", "e1").to_dict() == before
