"""test_operation.py — Patch / Operation / new_operation / OpLog 行为测试.

覆盖:
  - Patch: 构造 / frozen / to_dict
  - Operation: frozen / to_dict / patches 是 tuple / 默认值
  - new_operation: 校验 source / kind / 自动填 id+ts+trace_id
  - OpLog: append / hash chain / verify_chain 检测篡改 / by_entity / by_source
"""

from __future__ import annotations

import os
import pickle
import stat
import threading
import time
from dataclasses import asdict, fields, replace

import pytest

import operation as operation_module
from operation import (
    KINDS,
    OPS,
    SOURCES,
    ZERO_HASH,
    OpLog,
    Operation,
    Patch,
    new_operation,
)
from entity import ZeError


# ==============================================================
# Patch
# ==============================================================

def test_patch_construct():
    p = Patch(entity_type="obj", entity_id="e1", field="name", op="set",
              before="old", after="new")
    assert p.entity_type == "obj"
    assert p.entity_id == "e1"
    assert p.field == "name"
    assert p.op == "set"
    assert p.before == "old"
    assert p.after == "new"


def test_patch_frozen():
    p = Patch(entity_type="t", entity_id="i", field="f", op="set")
    with pytest.raises(Exception):
        p.entity_type = "x"  # type: ignore[misc]


def test_patch_defaults():
    p = Patch(entity_type="t", entity_id="i", field="f", op="set")
    assert p.before is None
    assert p.after is None


def test_patch_to_dict():
    p = Patch(entity_type="obj", entity_id="e1", field="x", op="set",
              before=1, after=2)
    assert p.to_dict() == {
        "entity_type": "obj", "entity_id": "e1", "field": "x", "op": "set",
        "before": 1, "after": 2,
    }


def test_patch_to_dict_with_meta_field():
    """field='__entity__' for create/delete/restore meta ops."""
    p = Patch(entity_type="obj", entity_id="e1", field="__entity__", op="create",
              before=None, after={"id": "e1", "type": "obj"})
    d = p.to_dict()
    assert d["field"] == "__entity__"
    assert d["op"] == "create"


def test_patch_keeps_public_dataclass_reflection_and_detached_values():
    before = {"nested": [1]}
    after = {"nested": [2]}
    provenance = {"field": {"actor": "u"}}
    ancestry = {"before": {"present": False}}
    patch = Patch(
        "obj", "e1", "x", "set", before, after,
        provenance=provenance, position=3, ancestry=ancestry,
    )
    before["nested"].append(99)
    after["nested"].append(99)
    provenance["field"]["actor"] = "forged"
    ancestry["before"]["present"] = True

    expected_fields = (
        "entity_type", "entity_id", "field", "op", "before", "after",
        "provenance", "position", "ancestry",
    )
    assert tuple(item.name for item in fields(Patch)) == expected_fields
    assert tuple(patch.__dict__) == expected_fields
    assert asdict(patch) == {
        "entity_type": "obj",
        "entity_id": "e1",
        "field": "x",
        "op": "set",
        "before": {"nested": [1]},
        "after": {"nested": [2]},
        "provenance": {"field": {"actor": "u"}},
        "position": 3,
        "ancestry": {"before": {"present": False}},
    }
    assert repr(patch).startswith(
        "Patch(entity_type='obj', entity_id='e1', field='x', op='set',"
    )
    replaced = replace(patch, after={"nested": [3]})
    assert replaced.after == {"nested": [3]}
    assert patch.after == {"nested": [2]}
    assert pickle.loads(pickle.dumps(patch)) == patch
    if hasattr(Patch, "__match_args__"):
        assert Patch.__match_args__ == expected_fields

    leaked = patch.before
    leaked["nested"].append(88)
    assert patch.before == {"nested": [1]}

    reflected_state = patch.__dict__
    reflected_state["before"]["nested"].append(77)
    reflected_state["after"]["nested"].append(77)
    assert patch.to_dict()["before"] == {"nested": [1]}
    assert patch.to_dict()["after"] == {"nested": [2]}


def test_patch_ownership_never_invokes_caller_deepcopy_hook():
    calls = []

    class HostileValue:
        def __deepcopy__(self, _memo):
            calls.append("called")
            raise AssertionError("caller hook must not execute")

    patch = Patch("obj", "e1", "x", "set", HostileValue(), None)
    assert calls == []
    operation = _make_op(patches=[patch], reverse_patches=[])
    log = OpLog()
    with pytest.raises(ZeError) as captured:
        log.append(operation)
    assert captured.value.code == "oplog_entry_invalid"
    assert calls == []
    assert len(log) == 0


def test_patch_rejects_behavioral_scalar_subclasses_without_running_hooks():
    calls = []

    class HostileStr(str):
        def __eq__(self, _other):
            calls.append("eq")
            raise AssertionError("scalar callback must not execute")

        def __bool__(self):
            calls.append("bool")
            raise AssertionError("scalar callback must not execute")

    with pytest.raises(TypeError, match="exact string"):
        Patch(HostileStr("obj"), "e1", "x", "set")
    assert calls == []


# ==============================================================
# Operation
# ==============================================================

def _make_op(**overrides):
    """Helper: 构造一个测试用 op."""
    defaults = dict(
        command_id="entity.update",
        source="user",
        actor={"user_id": "u1", "source": "user"},
        patches=[Patch("obj", "e1", "name", "set", "old", "new")],
        reverse_patches=[Patch("obj", "e1", "name", "set", "new", "old")],
    )
    defaults.update(overrides)
    return new_operation(**defaults)


def test_operation_frozen():
    op = _make_op()
    with pytest.raises(Exception):
        op.id = "fake"  # type: ignore[misc]


def test_operation_defaults():
    op = _make_op()
    assert op.group_id == ""
    assert op.kind == "native"
    assert op.error is None
    assert op.pipeline is None
    assert op.trace_id  # 自动填了 uuid
    assert len(op.trace_id) == 32
    assert len(op.__reduce_ex__(pickle.HIGHEST_PROTOCOL)[1]) == 14


@pytest.mark.parametrize(
    "schema",
    ("ze.pipeline-start.v2", "ze.pipeline-control.v2"),
)
def test_generic_operation_factory_rejects_authority_v2(schema):
    with pytest.raises(TypeError, match="sealed authority factory"):
        _make_op(pipeline={"schema": schema})


def test_operation_patches_are_tuples():
    op = _make_op()
    assert isinstance(op.patches, tuple)
    assert isinstance(op.reverse_patches, tuple)


def test_operation_to_dict():
    op = _make_op()
    d = op.to_dict()
    assert d["command_id"] == "entity.update"
    assert d["source"] == "user"
    assert d["patches"][0]["field"] == "name"
    assert d["reverse_patches"][0]["before"] == "new"
    assert d["error"] is None


def test_operation_to_dict_with_error():
    op = _make_op(error={"code": "validation_failed", "message": "bad arg"})
    d = op.to_dict()
    assert d["error"] == {"code": "validation_failed", "message": "bad arg"}


def test_operation_to_dict_distinguishes_empty_optional_metadata():
    default = _make_op()
    empty_error = _make_op(error={})
    empty_external = _make_op(external={})

    assert default.to_dict()["error"] is None
    assert "external" not in default.to_dict()
    assert "pipeline" not in default.to_dict()
    assert empty_error.to_dict()["error"] == {}
    assert empty_external.to_dict()["external"] == {}


def test_operation_omits_empty_document_and_session_for_backcompat():
    op = _make_op()
    d = op.to_dict()
    assert "document_id" not in d
    assert "session_id" not in d


def test_operation_includes_document_and_session_when_present():
    op = _make_op(document_id="doc1", session_id="session1")
    d = op.to_dict()
    assert d["document_id"] == "doc1"
    assert d["session_id"] == "session1"


def test_operation_to_dict_actor_is_copy():
    """to_dict 里的 actor 应该是 copy, 不暴露内部 dict."""
    actor = {"user_id": "u1", "source": "user"}
    op = _make_op(actor=actor)
    d = op.to_dict()
    d["actor"]["user_id"] = "tampered"
    assert op.actor["user_id"] == "u1"


def test_operation_keeps_public_dataclass_reflection_and_detached_values():
    patch = Patch("obj", "e1", "x", "set", {"n": [1]}, {"n": [2]})
    actor = {"user_id": "u1", "roles": ["editor"]}
    error = {"code": "x", "context": {"retry": False}}
    external = {"kind": "api", "receipts": ["r1"]}
    pipeline = {
        "schema": "ze.pipeline-start.v1",
        "run_id": "run-1",
        "stages": [{"id": "compose"}],
    }
    operation = Operation(
        id="op-1",
        command_id="entity.update",
        source="user",
        actor=actor,
        ts=1.5,
        patches=[patch],
        reverse_patches=[],
        error=error,
        external=external,
        pipeline=pipeline,
    )
    actor["roles"].append("owner")
    error["context"]["retry"] = True
    external["receipts"].append("forged")
    pipeline["stages"][0]["id"] = "forged"

    expected_fields = (
        "id", "command_id", "source", "actor", "ts", "patches",
        "reverse_patches", "group_id", "trace_id", "kind", "error",
        "document_id", "session_id", "external", "pipeline",
    )
    assert tuple(item.name for item in fields(Operation)) == expected_fields
    assert tuple(operation.__dict__) == expected_fields
    reflected = asdict(operation)
    assert reflected["actor"] == {"user_id": "u1", "roles": ["editor"]}
    assert reflected["patches"][0]["before"] == {"n": [1]}
    assert reflected["error"] == {"code": "x", "context": {"retry": False}}
    assert reflected["external"] == {"kind": "api", "receipts": ["r1"]}
    assert reflected["pipeline"] == {
        "schema": "ze.pipeline-start.v1",
        "run_id": "run-1",
        "stages": [{"id": "compose"}],
    }
    assert isinstance(operation.patches, tuple)
    assert repr(operation).startswith(
        "Operation(id='op-1', command_id='entity.update', source='user',"
    )
    replaced = replace(operation, command_id="entity.rename")
    assert replaced.command_id == "entity.rename"
    assert operation.command_id == "entity.update"
    assert pickle.loads(pickle.dumps(operation)) == operation
    if hasattr(Operation, "__match_args__"):
        assert Operation.__match_args__ == expected_fields

    leaked = operation.actor
    leaked["roles"].append("forged")
    assert operation.actor == {"user_id": "u1", "roles": ["editor"]}

    reflected_state = operation.__dict__
    reflected_state["actor"]["roles"].append("forged-through-dict")
    reflected_state["error"]["context"]["retry"] = True
    reflected_state["external"]["receipts"].append("forged-through-dict")
    reflected_state["pipeline"]["stages"][0]["id"] = "forged-through-dict"
    assert operation.to_dict()["actor"] == {
        "user_id": "u1", "roles": ["editor"],
    }
    assert operation.to_dict()["error"] == {
        "code": "x", "context": {"retry": False},
    }
    assert operation.to_dict()["external"] == {
        "kind": "api", "receipts": ["r1"],
    }
    assert operation.to_dict()["pipeline"] == {
        "schema": "ze.pipeline-start.v1",
        "run_id": "run-1",
        "stages": [{"id": "compose"}],
    }


def test_operation_pipeline_carrier_is_known_owned_and_hash_visible():
    descriptor = {
        "schema": "ze.pipeline-control.v1",
        "run_id": "run-1",
        "action": "pause",
        "params": {"items": ["item-1"]},
    }
    operation = _make_op(pipeline=descriptor)
    descriptor["params"]["items"].append("forged")

    reflected = operation.pipeline
    reflected["params"]["items"].append("leaked")
    assert operation.pipeline == {
        "schema": "ze.pipeline-control.v1",
        "run_id": "run-1",
        "action": "pause",
        "params": {"items": ["item-1"]},
    }
    assert operation.to_dict()["pipeline"] == operation.pipeline
    assert "item-1" not in repr(operation)
    assert "ze.pipeline-control.v1" not in repr(operation)

    without = _make_op()
    assert OpLog._hash(ZERO_HASH, without.to_dict()) != OpLog._hash(
        ZERO_HASH, operation.to_dict(),
    )


def test_operation_pipeline_carrier_persists_and_is_hash_protected(tmp_path):
    path = tmp_path / "oplog.ndjson"
    operation = _make_op(pipeline={
        "schema": "ze.pipeline-start.v1",
        "run_id": "run-1",
        "operation_ids": ["op-start", "op-placeholder"],
    })
    log = OpLog(path)
    log.append(operation)

    recovered = OpLog(path)
    assert recovered.verify_chain()
    assert recovered.entries()[0]["op"]["pipeline"]["run_id"] == "run-1"
    recovered._entries[0]["op"]["pipeline"]["run_id"] = "forged"
    assert not recovered.verify_chain()


@pytest.mark.parametrize(
    "pipeline",
    [
        {"schema": "ze.pipeline-future.v9"},
        {
            "schema": "ze.pipeline-control.v1",
            **{f"chunk_{index}": "x" * 14_000 for index in range(5)},
        },
    ],
    ids=["unknown-schema", "control-byte-budget"],
)
def test_oplog_cold_reader_rejects_resigned_invalid_pipeline_carrier(
    tmp_path, pipeline,
):
    path = tmp_path / "oplog.ndjson"
    body = _make_op().to_dict()
    body["pipeline"] = pipeline
    entry = {
        "op": body,
        "prev_hash": ZERO_HASH,
        # The compatibility hash is not a signature. Even a correctly
        # recomputed hash cannot authorize an invalid descriptor.
        "self_hash": OpLog._hash(ZERO_HASH, body),
    }
    path.write_bytes((
        operation_module.json.dumps(
            entry,
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
        ) + "\n"
    ).encode("utf-8"))

    with pytest.raises(ZeError) as raised:
        OpLog(path)
    assert raised.value.code == "oplog_corrupt"


@pytest.mark.parametrize(
    "pipeline",
    [
        {},
        {"schema": "ze.pipeline-unknown.v1"},
        {"schema": "ze.pipeline-control.v1", "value": "x" * (16 * 1024 + 1)},
        {"schema": "ze.pipeline-start.v1", "items": ("tuple",)},
    ],
    ids=["missing-schema", "unknown-schema", "control-text-budget", "tuple"],
)
def test_operation_pipeline_carrier_fails_closed(pipeline):
    with pytest.raises(TypeError, match="valid known Pipeline descriptor"):
        _make_op(pipeline=pipeline)


def test_operation_rejects_behavioral_inputs_without_running_hooks():
    calls = []

    class HostileStr(str):
        def __eq__(self, _other):
            calls.append("eq")
            raise AssertionError("scalar callback must not execute")

        def __bool__(self):
            calls.append("bool")
            raise AssertionError("scalar callback must not execute")

    class HostileList(list):
        def __iter__(self):
            calls.append("iter")
            raise AssertionError("container callback must not execute")

    class HostileDict(dict):
        def items(self):
            calls.append("items")
            raise AssertionError("container callback must not execute")

    patch = Patch("obj", "e1", "x", "set", 1, 2)
    with pytest.raises(TypeError, match="Operation.command_id"):
        Operation(
            id="op-1", command_id=HostileStr("entity.update"), source="user",
            actor={}, ts=1.0, patches=[patch], reverse_patches=[],
        )
    with pytest.raises(TypeError, match="exact list or tuple"):
        Operation(
            id="op-1", command_id="entity.update", source="user",
            actor={}, ts=1.0, patches=HostileList([patch]), reverse_patches=[],
        )
    with pytest.raises(TypeError, match="actor"):
        Operation(
            id="op-1", command_id="entity.update", source="user",
            actor=HostileDict(user_id="u1"), ts=1.0,
            patches=[patch], reverse_patches=[],
        )
    with pytest.raises(ZeError) as captured:
        _make_op(source=HostileStr("user"))
    assert captured.value.code == "validation_failed"
    assert calls == []


# ==============================================================
# new_operation 工厂
# ==============================================================

def test_new_operation_auto_id():
    op1 = _make_op()
    op2 = _make_op()
    assert op1.id != op2.id
    assert len(op1.id) == 32


def test_new_operation_auto_ts():
    before = time.time()
    op = _make_op()
    after = time.time()
    assert before <= op.ts <= after


def test_new_operation_auto_trace_id():
    """没传 trace_id → 自动填 uuid."""
    op = _make_op()
    assert op.trace_id
    assert len(op.trace_id) == 32


def test_new_operation_explicit_trace_id():
    """传了 trace_id 用 caller 的."""
    op = _make_op(trace_id="my-trace-123")
    assert op.trace_id == "my-trace-123"


def test_new_operation_invalid_source_raises():
    with pytest.raises(ZeError) as exc:
        _make_op(source="hacker")
    assert exc.value.code == "validation_failed"


def test_new_operation_all_valid_sources():
    for src in SOURCES:
        op = _make_op(source=src)
        assert op.source == src


def test_new_operation_invalid_kind_raises():
    with pytest.raises(ZeError) as exc:
        _make_op(kind="weird")
    assert exc.value.code == "validation_failed"


def test_new_operation_all_valid_kinds():
    for kind in KINDS:
        op = _make_op(kind=kind)
        assert op.kind == kind


def test_new_operation_actor_is_copied():
    """工厂应该 dict(actor) 而非引用; caller 后续 mutate 不影响 op."""
    actor = {"user_id": "u1", "source": "user"}
    op = _make_op(actor=actor)
    actor["user_id"] = "tampered"
    assert op.actor["user_id"] == "u1"


# ==============================================================
# OpLog 基础
# ==============================================================

def test_oplog_empty():
    log = OpLog()
    assert log.entries() == []
    assert log.verify_chain() is True  # 空链有效
    assert len(log) == 0


def test_oplog_append_returns_entry():
    log = OpLog()
    op = _make_op()
    entry = log.append(op)
    assert entry["op"] == op.to_dict()
    assert entry["prev_hash"] == ZERO_HASH
    assert len(entry["self_hash"]) == 64
    assert len(log) == 1


def test_oplog_chain_links():
    """连续 append 链头 prev_hash = 上条 self_hash."""
    log = OpLog()
    e1 = log.append(_make_op())
    e2 = log.append(_make_op())
    e3 = log.append(_make_op())
    assert e1["prev_hash"] == ZERO_HASH
    assert e2["prev_hash"] == e1["self_hash"]
    assert e3["prev_hash"] == e2["self_hash"]


def test_oplog_verify_clean_chain():
    log = OpLog()
    for _ in range(5):
        log.append(_make_op())
    assert log.verify_chain() is True


def test_oplog_entries_returns_copy():
    log = OpLog()
    log.append(_make_op())
    entries = log.entries()
    entries[0]["op"]["command_id"] = "TAMPERED"
    # 内部 entries 不应被改
    assert log.entries()[0]["op"]["command_id"] == "entity.update"


@pytest.mark.parametrize("persistent", [False, True])
def test_oplog_append_detaches_nested_operation_values(tmp_path, persistent):
    """caller 后续改嵌套 actor/error/external，不能篡改 live 审计链。"""
    path = tmp_path / "oplog.ndjson" if persistent else None
    actor = {"nested": {"role": "user"}}
    error = {"detail": {"retry": False}}
    external = {"meta": {"request_id": "original"}}
    op = _make_op(actor=actor, error=error, external=external)
    log = OpLog(path)
    returned = log.append(op)

    actor["nested"]["role"] = "admin"
    error["detail"]["retry"] = True
    external["meta"]["request_id"] = "tampered"
    returned["op"]["actor"]["nested"]["role"] = "returned-tamper"

    stored = log.entries()[0]["op"]
    assert stored["actor"]["nested"]["role"] == "user"
    assert stored["error"]["detail"]["retry"] is False
    assert stored["external"]["meta"]["request_id"] == "original"
    assert log.verify_chain()
    if persistent:
        fresh = OpLog(path)
        assert fresh.entries() == log.entries()
        assert fresh.verify_chain()


@pytest.mark.parametrize("persistent", [False, True])
def test_oplog_freezes_body_before_hash_and_record_serialization(
    monkeypatch, tmp_path, persistent,
):
    """caller 在首次 dumps 后并发 mutate，也只能影响后续 caller 对象，不能打断链。"""
    path = tmp_path / "oplog.ndjson" if persistent else None
    actor = {"nested": {"role": "user"}}
    op = _make_op(actor=actor)
    real_dumps = operation_module.json.dumps
    dump_calls = 0

    def mutate_after_first_dump(*args, **kwargs):
        nonlocal dump_calls
        encoded = real_dumps(*args, **kwargs)
        dump_calls += 1
        if dump_calls == 1:
            actor["nested"]["role"] = "admin"
        return encoded

    monkeypatch.setattr(operation_module.json, "dumps", mutate_after_first_dump)
    log = OpLog(path)
    returned = log.append(op)

    assert dump_calls >= 3  # freeze body、hash body、完整 entry
    assert returned["op"]["actor"]["nested"]["role"] == "user"
    assert log.entries()[0]["op"]["actor"]["nested"]["role"] == "user"
    assert log.verify_chain()
    if persistent:
        fresh = OpLog(path)
        assert fresh.verify_chain()
        assert fresh.entries() == log.entries()


@pytest.mark.parametrize("persistent", [False, True])
def test_oplog_normalizes_concurrent_projection_runtime_error(tmp_path, persistent):
    class UnstableMapping(dict):
        def __deepcopy__(self, _memo):
            raise RuntimeError("dictionary changed size during iteration: secret")

    path = tmp_path / "oplog.ndjson" if persistent else None
    log = OpLog(path)
    with pytest.raises(ZeError) as caught:
        log.append(_make_op(actor={"nested": UnstableMapping()}))
    _assert_safe_ze_error(caught.value, "oplog_entry_invalid", "secret")
    assert len(log) == 0
    if persistent:
        assert path.read_bytes() == b""


# ==============================================================
# OpLog hash chain 防篡改
# ==============================================================

def test_oplog_detects_tampered_op_body():
    """直接改内部 entry 的 op body → verify 应该崩."""
    log = OpLog()
    log.append(_make_op())
    log.append(_make_op())
    # 暴力篡改 (实际不允许; 测试用)
    log._entries[0]["op"]["command_id"] = "fake.command"
    assert log.verify_chain() is False


def test_oplog_detects_tampered_self_hash():
    log = OpLog()
    log.append(_make_op())
    log.append(_make_op())
    log._entries[0]["self_hash"] = "0" * 64
    assert log.verify_chain() is False


def test_oplog_detects_tampered_prev_hash():
    log = OpLog()
    log.append(_make_op())
    log.append(_make_op())
    log._entries[1]["prev_hash"] = "0" * 64
    assert log.verify_chain() is False


def test_oplog_first_entry_prev_hash_is_zero():
    log = OpLog()
    e = log.append(_make_op())
    assert e["prev_hash"] == ZERO_HASH


def test_oplog_hash_is_deterministic():
    """相同输入 → 相同 hash."""
    body = {"a": 1, "b": "x"}
    h1 = OpLog._hash(ZERO_HASH, body)
    h2 = OpLog._hash(ZERO_HASH, body)
    assert h1 == h2


def test_oplog_hash_field_order_invariant():
    """sort_keys=True → 字段顺序不影响 hash."""
    h1 = OpLog._hash(ZERO_HASH, {"a": 1, "b": 2})
    h2 = OpLog._hash(ZERO_HASH, {"b": 2, "a": 1})
    assert h1 == h2


def test_oplog_finite_hash_format_backcompat_golden():
    """安全收紧不能把历史有限 JSON 改成 compact JSON 或其他 codec。"""
    body = {"a": 1, "b": "中文", "nested": {"ok": True}}
    assert OpLog._hash(ZERO_HASH, body) == (
        "2ebd3e831a0577dd639563705bec90842d1a045ff9cff6c7d95093148b7520eb"
    )


# ==============================================================
# OpLog 过滤查询
# ==============================================================

def test_by_entity_finds_matching():
    log = OpLog()
    log.append(_make_op(patches=[Patch("obj", "e1", "name", "set", "a", "b")],
                       reverse_patches=[Patch("obj", "e1", "name", "set", "b", "a")]))
    log.append(_make_op(patches=[Patch("obj", "e2", "name", "set", "c", "d")],
                       reverse_patches=[Patch("obj", "e2", "name", "set", "d", "c")]))
    log.append(_make_op(patches=[Patch("obj", "e1", "value", "set", 1, 2)],
                       reverse_patches=[Patch("obj", "e1", "value", "set", 2, 1)]))
    results = log.by_entity("obj", "e1")
    assert len(results) == 2


def test_by_entity_empty_when_no_match():
    log = OpLog()
    log.append(_make_op())
    assert log.by_entity("nope", "nope") == []


def test_by_entity_matches_if_any_patch_hits():
    """多 entity 的 op (patches 涉及多个 entity) 都应被命中查询."""
    log = OpLog()
    op = _make_op(
        patches=[
            Patch("a", "1", "x", "set", 1, 2),
            Patch("b", "2", "y", "set", 3, 4),
        ],
        reverse_patches=[
            Patch("a", "1", "x", "set", 2, 1),
            Patch("b", "2", "y", "set", 4, 3),
        ],
    )
    log.append(op)
    assert len(log.by_entity("a", "1")) == 1
    assert len(log.by_entity("b", "2")) == 1


def test_by_source_filters():
    log = OpLog()
    log.append(_make_op(source="user"))
    log.append(_make_op(source="ai"))
    log.append(_make_op(source="ai"))
    log.append(_make_op(source="system"))
    assert len(log.by_source("user")) == 1
    assert len(log.by_source("ai")) == 2
    assert len(log.by_source("system")) == 1
    assert len(log.by_source("sync")) == 0


def test_by_document_session_and_group_filters():
    log = OpLog()
    log.append(_make_op(document_id="doc1", session_id="s1", group_id="g1"))
    log.append(_make_op(document_id="doc2", session_id="s1", group_id="g2"))
    log.append(_make_op(document_id="doc1", session_id="s2", group_id="g1"))
    assert len(log.by_document("doc1")) == 2
    assert len(log.by_session("s1")) == 2
    assert len(log.by_group("g1")) == 2


# ==============================================================
# OpLog 持久化(path 可选;path=None 保持纯内存)
# ==============================================================

def test_oplog_memory_mode_unchanged():
    """path=None: 纯内存,不落盘(向后兼容)。"""
    log = OpLog()
    log.append(_make_op())
    assert len(log) == 1 and log.verify_chain()


def test_oplog_persist_writes_ndjson(tmp_path):
    p = tmp_path / "oplog.ndjson"
    log = OpLog(p)
    log.append(_make_op())
    log.append(_make_op())
    assert p.exists()
    assert len(p.read_text().strip().splitlines()) == 2
    assert p.read_bytes().endswith(b"\n")


def test_oplog_reload_roundtrip(tmp_path):
    """落盘 → 新实例载回 → 验链 + entries 一致(模拟进程重启)。"""
    p = tmp_path / "oplog.ndjson"
    a = OpLog(p)
    for _ in range(3):
        a.append(_make_op())
    b = OpLog(p)                              # 新实例 = 重启
    assert len(b) == 3 and b.verify_chain()
    assert [e["self_hash"] for e in b.entries()] == [e["self_hash"] for e in a.entries()]


def test_oplog_reload_then_append_continues_chain(tmp_path):
    p = tmp_path / "oplog.ndjson"
    OpLog(p).append(_make_op())              # 写 1 条后丢弃实例
    b = OpLog(p)                              # 重新载回
    b.append(_make_op())                     # 续写
    assert len(b) == 2 and b.verify_chain()
    assert len(OpLog(p)) == 2                # 三次载回仍 2 条


def test_oplog_corrupt_chain_raises(tmp_path):
    p = tmp_path / "oplog.ndjson"
    OpLog(p).append(_make_op())
    p.write_text(p.read_text() + '{"op":{},"prev_hash":"x","self_hash":"y"}\n')
    with pytest.raises(ZeError) as exc:
        OpLog(p)
    assert exc.value.code == "oplog_corrupt"


def test_oplog_corrupt_json_raises(tmp_path):
    p = tmp_path / "oplog.ndjson"
    p.write_text("{ not json\n")
    with pytest.raises(ZeError) as exc:
        OpLog(p)
    assert exc.value.code == "oplog_corrupt"


def test_oplog_truncated_tail_soft_recovers(tmp_path):
    """崩在写一行途中留下不完整末行 → 软恢复:丢弃截断尾,保留前面完好的 op(簇 C-3)。"""
    p = tmp_path / "oplog.ndjson"
    log = OpLog(p)
    log.append(_make_op())
    log.append(_make_op())
    # 模拟 _persist_line 写到一半崩溃:补一个不完整的 JSON 末行(无换行)
    with open(p, "a", encoding="utf-8") as f:
        f.write('{"op": {"command_id": "entity.upda')
    reloaded = OpLog(p)
    assert len(reloaded) == 2                         # 不抛;丢弃截断尾,保留前 2 条
    assert reloaded.verify_chain()
    assert reloaded._truncated_tail_dropped is True
    assert p.read_bytes().endswith(b"\n")             # 必须物理 truncate，不是只在内存忽略
    reloaded.append(_make_op())                        # 软恢复后仍可续写,链不断
    assert len(reloaded) == 3 and reloaded.verify_chain()
    second = OpLog(p)
    third = OpLog(p)
    assert len(second) == len(third) == 3
    assert second.verify_chain() and third.verify_chain()


def test_oplog_live_instance_repairs_external_partial_before_append(tmp_path):
    """活实例创建后外部进程留下 partial tail，append 也必须在锁内修复再续链。"""
    p = tmp_path / "oplog.ndjson"
    log = OpLog(p)
    first = log.append(_make_op())
    with open(p, "ab") as f:
        f.write(b'{"op":{"actor":"crashed')

    second = log.append(_make_op())
    assert second["prev_hash"] == first["self_hash"]
    assert p.read_bytes().endswith(b"\n")
    assert log._truncated_tail_dropped is True
    assert len(OpLog(p)) == 2


def test_oplog_repair_fact_survives_following_append_failure(tmp_path):
    """尾片修复已 fsync 后，即使新 record 序列化失败，修复事实也不能丢。"""
    p = tmp_path / "oplog.ndjson"
    log = OpLog(p)
    log.append(_make_op())
    with open(p, "ab") as f:
        f.write(b'{"partial":')

    with pytest.raises(ZeError) as exc:
        log.append(_make_op(actor={"user_id": "\ud800"}))
    assert exc.value.code == "oplog_entry_invalid"
    assert exc.value.context == {}
    assert exc.value.__cause__ is None
    assert log._truncated_tail_dropped is True
    assert p.read_bytes().endswith(b"\n")
    assert len(OpLog(p)) == 1


def test_oplog_durable_append_updates_memory_before_fast_path_stat(monkeypatch, tmp_path):
    """fsync 后异步异常不能留下“文件新、内存旧、stat 却匹配”的分叉窗口。"""
    p = tmp_path / "oplog.ndjson"
    log = OpLog(p)
    log.append(_make_op())
    real_persist = log._persist_line

    def persist_then_interrupt(body):
        real_persist(body)
        raise KeyboardInterrupt("injected after durable helper")

    monkeypatch.setattr(log, "_persist_line", persist_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        log.append(_make_op())
    assert len(log) == 2 and len(OpLog(p)) == 2

    monkeypatch.setattr(log, "_persist_line", real_persist)
    log.append(_make_op())
    fresh = OpLog(p)
    assert len(log) == len(fresh) == 3
    assert log.verify_chain() and fresh.verify_chain()


def test_oplog_append_fsync_failure_stays_unknown_but_next_append_rescans(
    monkeypatch, tmp_path,
):
    p = tmp_path / "oplog.ndjson"
    log = OpLog(p)
    log.append(_make_op())
    real_fsync = operation_module.os.fsync

    monkeypatch.setattr(
        operation_module.os,
        "fsync",
        lambda _fd: (_ for _ in ()).throw(OSError("injected append fsync")),
    )
    with pytest.raises(OSError, match="append fsync"):
        log.append(_make_op())
    assert len(log) == 1

    monkeypatch.setattr(operation_module.os, "fsync", real_fsync)
    assert len(OpLog(p)) == 2  # 完整 LF 可能已落盘，故结果是 unknown 而非 not-durable
    log.append(_make_op())
    fresh = OpLog(p)
    assert len(log) == len(fresh) == 3
    assert log.verify_chain() and fresh.verify_chain()


def test_oplog_repair_fsync_failure_blocks_append_then_retry_is_safe(
    monkeypatch, tmp_path,
):
    p = tmp_path / "oplog.ndjson"
    log = OpLog(p)
    log.append(_make_op())
    with open(p, "ab") as stream:
        stream.write(b'{"partial":')
    real_fsync = operation_module.os.fsync

    monkeypatch.setattr(
        operation_module.os,
        "fsync",
        lambda _fd: (_ for _ in ()).throw(OSError("injected repair fsync")),
    )
    with pytest.raises(OSError, match="repair fsync"):
        log.append(_make_op())
    assert len(log) == 1

    monkeypatch.setattr(operation_module.os, "fsync", real_fsync)
    log.append(_make_op())
    fresh = OpLog(p)
    assert len(log) == len(fresh) == 2
    assert log.verify_chain() and fresh.verify_chain()


def test_oplog_truncate_failure_does_not_append_past_partial(monkeypatch, tmp_path):
    p = tmp_path / "oplog.ndjson"
    OpLog(p).append(_make_op())
    with open(p, "ab") as stream:
        stream.write(b'{"partial":')
    before = p.read_bytes()
    real_ftruncate = operation_module.os.ftruncate

    monkeypatch.setattr(
        operation_module.os,
        "ftruncate",
        lambda _fd, _size: (_ for _ in ()).throw(OSError("injected truncate")),
    )
    with pytest.raises(OSError, match="injected truncate"):
        OpLog(p)
    assert p.read_bytes() == before

    monkeypatch.setattr(operation_module.os, "ftruncate", real_ftruncate)
    repaired = OpLog(p)
    repaired.append(_make_op())
    assert len(OpLog(p)) == 2


def test_oplog_repairs_tail_cut_inside_utf8_codepoint(tmp_path):
    """UTF-8 多字节字符截在中间时按 byte offset 截尾，不先解码整文件。"""
    p = tmp_path / "oplog.ndjson"
    log = OpLog(p)
    log.append(_make_op())
    with open(p, "ab") as f:
        f.write('{“中文”:“崩'.encode("utf-8")[:-1])

    repaired = OpLog(p)
    repaired.append(_make_op())
    assert len(OpLog(p)) == 2
    assert p.read_bytes().endswith(b"\n")


def test_oplog_unterminated_valid_first_record_is_uncommitted(tmp_path):
    """完整 JSON 但无 LF 也没有 commit marker：丢弃并 fsync，不当成已提交 op。"""
    p = tmp_path / "oplog.ndjson"
    OpLog(p).append(_make_op())
    p.write_bytes(p.read_bytes()[:-1])
    repaired = OpLog(p)
    assert len(repaired) == 0
    assert repaired._truncated_tail_dropped is True
    assert p.read_bytes() == b""
    assert len(OpLog(p)) == 0


def test_oplog_unterminated_garbage_without_prefix_is_uncommitted(tmp_path):
    p = tmp_path / "oplog.ndjson"
    p.write_bytes(b'{"op":')
    repaired = OpLog(p)
    assert len(repaired) == 0
    assert repaired._truncated_tail_dropped is True
    assert p.read_bytes() == b""
    assert len(OpLog(p)) == 0


def test_oplog_unterminated_split_utf8_without_prefix_is_uncommitted(tmp_path):
    p = tmp_path / "oplog.ndjson"
    p.write_bytes(b'{"name":"\xe4\xb8')
    repaired = OpLog(p)
    assert len(repaired) == 0
    assert repaired._truncated_tail_dropped is True
    assert p.read_bytes() == b""
    assert len(OpLog(p)) == 0


def test_oplog_newline_terminated_bad_json_at_tail_fails_closed(tmp_path):
    p = tmp_path / "oplog.ndjson"
    OpLog(p).append(_make_op())
    with open(p, "ab") as f:
        f.write(b"{bad json}\n")
    with pytest.raises(ZeError) as exc:
        OpLog(p)
    assert exc.value.code == "oplog_corrupt"


def test_oplog_newline_terminated_invalid_utf8_fails_closed(tmp_path):
    p = tmp_path / "oplog.ndjson"
    OpLog(p).append(_make_op())
    with open(p, "ab") as f:
        f.write(b'{"bad":"\xe4\xb8"}\n')
    with pytest.raises(ZeError) as exc:
        OpLog(p)
    assert exc.value.code == "oplog_corrupt"


def test_oplog_crlf_records_load_and_continue(tmp_path):
    p = tmp_path / "oplog.ndjson"
    OpLog(p).append(_make_op())
    p.write_bytes(p.read_bytes().replace(b"\n", b"\r\n"))
    loaded = OpLog(p)
    assert len(loaded) == 1 and loaded.verify_chain()
    loaded.append(_make_op())
    assert len(OpLog(p)) == 2


def test_oplog_whitespace_tail_requires_lf_commit_boundary(tmp_path):
    p = tmp_path / "oplog.ndjson"
    p.write_bytes(b"  \t")
    repaired = OpLog(p)
    assert len(repaired) == 0
    assert repaired._truncated_tail_dropped is True
    assert p.read_bytes() == b""

    p.write_bytes(b"  \t\n")
    with pytest.raises(ZeError) as exc:
        OpLog(p)
    assert exc.value.code == "oplog_corrupt"


def test_oplog_non_tail_json_corruption_still_raises(tmp_path):
    """非末行的 JSON 损坏 = 真损坏 → 仍抛 oplog_corrupt(不软恢复)。"""
    p = tmp_path / "oplog.ndjson"
    log = OpLog(p)
    for _ in range(3):
        log.append(_make_op())
    lines = p.read_text(encoding="utf-8").splitlines()
    lines[1] = "{ garbage not json"                    # 损坏中间行(非末行)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(ZeError) as exc:
        OpLog(p)
    assert exc.value.code == "oplog_corrupt"


def test_oplog_missing_path_starts_empty(tmp_path):
    """path 不存在 → 空 log(首次启动)。"""
    log = OpLog(tmp_path / "nonexistent.ndjson")
    assert len(log) == 0 and log.verify_chain()


def test_oplog_concurrent_appends_dont_fork_chain(tmp_path):
    """A2 回归:两个 OpLog 实例(模拟 CLI+server 两进程)从同一空文件起步、交错 append,文件 hash 链
    仍连续 —— 第三次载回不抛 oplog_corrupt。旧无锁实现两实例内存 tail 都停在 ZERO/各自值 → 各自 chain
    到同一 prev → 文件出现两条同 prev 的 entry → 链分叉 → 载回全 API 500。flock 在锁内读文件真实尾续链根治。"""
    p = tmp_path / "oplog.ndjson"
    a = OpLog(p)
    b = OpLog(p)                                  # 两实例都从空文件起步,内存 tail 都是 ZERO(并发进程真实写照)
    a.append(_make_op())                          # c1: prev=ZERO
    b.append(_make_op())                          # 旧实现 prev 仍 ZERO 与 c1 撞 → 分叉;新实现 flock 内读文件尾 → prev=c1.self
    a.append(_make_op())
    b.append(_make_op())
    reloaded = OpLog(p)                           # 不抛 oplog_corrupt
    assert reloaded.verify_chain()
    assert len(reloaded) == 4                     # 四条都在、链完整


def test_oplog_two_live_instances_converge_after_partial_tail_repair(tmp_path):
    """两个活实例交错：先修复外部 partial，再各写一条，多次 reload 不丢新 op。"""
    p = tmp_path / "oplog.ndjson"
    a = OpLog(p)
    b = OpLog(p)
    a.append(_make_op())
    with open(p, "ab") as f:
        f.write(b'{"half":"record')
    b.append(_make_op())
    a.append(_make_op())

    first_reload = OpLog(p)
    second_reload = OpLog(p)
    assert len(first_reload) == len(second_reload) == 3
    assert first_reload.verify_chain() and second_reload.verify_chain()
    assert len(a) == 3 and a.verify_chain()


def test_oplog_parallel_live_writers_keep_one_chain(tmp_path):
    """真实线程交错两个独立实例，flock 内重读的文件链不分叉。"""
    p = tmp_path / "oplog.ndjson"
    a = OpLog(p)
    b = OpLog(p)
    barrier = threading.Barrier(2)
    failures = []

    def _write(log):
        try:
            barrier.wait()
            for _ in range(20):
                log.append(_make_op())
        except BaseException as exc:  # 线程内异常必须回传给测试主线程
            failures.append(exc)

    threads = [threading.Thread(target=_write, args=(a,)), threading.Thread(target=_write, args=(b,))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == []
    reloaded = OpLog(p)
    assert len(reloaded) == 40
    assert reloaded.verify_chain()
    assert p.read_bytes().endswith(b"\n")


def test_oplog_fast_path_single_writer_chain_valid(tmp_path):
    """单写者(常态)多次 append 走快路(size 未变 → 用内存 tail),链仍完整、载回一致。"""
    p = tmp_path / "oplog.ndjson"
    log = OpLog(p)
    for _ in range(5):
        log.append(_make_op())
    assert log.verify_chain() and len(log) == 5
    assert OpLog(p).verify_chain() and len(OpLog(p)) == 5   # 载回完整


def test_oplog_secure_reopen_does_not_invalidate_single_writer_fast_path(monkeypatch, tmp_path):
    """已是 0600 时不得重复 chmod 改 ctime，否则每次 append 都退化成全量扫盘。"""
    p = tmp_path / "oplog.ndjson"
    log = OpLog(p)
    log.append(_make_op())
    scans = 0
    real_scan = log._scan_and_repair_locked

    def counted_scan(stream):
        nonlocal scans
        scans += 1
        return real_scan(stream)

    monkeypatch.setattr(log, "_scan_and_repair_locked", counted_scan)
    log.append(_make_op())
    assert scans == 0


# ==============================================================
# OpLog 持久化安全边界
# ==============================================================

def _assert_safe_ze_error(exc: ZeError, code: str, secret: str = ""):
    assert exc.code == code
    assert exc.context == {}
    assert exc.__cause__ is None
    if secret:
        assert secret not in exc.message
        assert secret not in str(exc)


def test_oplog_new_file_is_private_and_existing_file_is_tightened(tmp_path):
    created = tmp_path / "created.ndjson"
    OpLog(created).append(_make_op())
    assert stat.S_IMODE(created.stat().st_mode) == 0o600

    existing = tmp_path / "existing.ndjson"
    existing.write_bytes(b"")
    existing.chmod(0o666)
    OpLog(existing)
    assert stat.S_IMODE(existing.stat().st_mode) == 0o600


@pytest.mark.parametrize("broken", [False, True])
def test_oplog_rejects_final_symlink_without_touching_target(tmp_path, broken):
    target = tmp_path / "target.ndjson"
    if not broken:
        target.write_bytes(b"sentinel")
    link = tmp_path / "oplog.ndjson"
    link.symlink_to(target if not broken else tmp_path / "missing.ndjson")

    with pytest.raises(ZeError) as caught:
        OpLog(link)
    _assert_safe_ze_error(caught.value, "oplog_path_unsafe")
    if not broken:
        assert target.read_bytes() == b"sentinel"


def test_oplog_rejects_hardlink_without_chmod_or_write(tmp_path):
    target = tmp_path / "target.ndjson"
    target.write_bytes(b"")
    target.chmod(0o644)
    link = tmp_path / "oplog.ndjson"
    os.link(target, link)

    with pytest.raises(ZeError) as caught:
        OpLog(link)
    _assert_safe_ze_error(caught.value, "oplog_path_unsafe")
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    assert target.stat().st_nlink == 2


def test_oplog_rejects_directory_and_fifo_without_blocking(tmp_path):
    directory = tmp_path / "as-directory"
    directory.mkdir()
    with pytest.raises(ZeError) as directory_error:
        OpLog(directory)
    _assert_safe_ze_error(directory_error.value, "oplog_path_unsafe")

    fifo = tmp_path / "as-fifo"
    os.mkfifo(fifo)
    started = time.monotonic()
    with pytest.raises(ZeError) as fifo_error:
        OpLog(fifo)
    assert time.monotonic() - started < 1.0
    _assert_safe_ze_error(fifo_error.value, "oplog_path_unsafe")


def test_oplog_rejects_direct_parent_symlink_even_when_file_is_missing(tmp_path):
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(ZeError) as caught:
        OpLog(linked_parent / "oplog.ndjson")
    _assert_safe_ze_error(caught.value, "oplog_path_unsafe")
    assert not (real_parent / "oplog.ndjson").exists()


@pytest.mark.parametrize(
    "record",
    [
        b'{"op":{"nested":{"secret":"first","secret":"second"}},'
        b'"prev_hash":"x","self_hash":"y"}\n',
        b'{"op":{"value":NaN},"prev_hash":"x","self_hash":"y"}\n',
        b'{"op":{"value":Infinity},"prev_hash":"x","self_hash":"y"}\n',
        b'{"op":{"value":-Infinity},"prev_hash":"x","self_hash":"y"}\n',
        b'{"op":{"value":1e9999},"prev_hash":"x","self_hash":"y"}\n',
    ],
)
def test_oplog_reader_rejects_non_strict_json_without_payload_leak(tmp_path, record):
    p = tmp_path / "oplog.ndjson"
    p.write_bytes(record)
    with pytest.raises(ZeError) as caught:
        OpLog(p)
    _assert_safe_ze_error(caught.value, "oplog_corrupt", "secret")


def test_oplog_reader_rejects_deep_json_without_recursion_error(tmp_path):
    p = tmp_path / "oplog.ndjson"
    p.write_bytes(("[" * 200 + "0" + "]" * 200 + "\n").encode("ascii"))
    with pytest.raises(ZeError) as caught:
        OpLog(p)
    _assert_safe_ze_error(caught.value, "oplog_corrupt")


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_oplog_writer_rejects_non_finite_values_before_publish(tmp_path, value):
    p = tmp_path / "oplog.ndjson"
    log = OpLog(p)
    before = p.read_bytes() if p.exists() else b""
    with pytest.raises(ZeError) as caught:
        log.append(_make_op(actor={"score": value}))
    _assert_safe_ze_error(caught.value, "oplog_entry_invalid")
    assert len(log) == 0
    assert p.read_bytes() == before


def test_oplog_writer_rejects_deep_json_before_publish(tmp_path):
    nested = {}
    cursor = nested
    for _ in range(140):
        child = {}
        cursor["child"] = child
        cursor = child
    p = tmp_path / "oplog.ndjson"
    log = OpLog(p)
    with pytest.raises(ZeError) as caught:
        log.append(_make_op(actor=nested))
    _assert_safe_ze_error(caught.value, "oplog_entry_invalid")
    assert len(log) == 0
    assert p.read_bytes() == b""


def test_oplog_writer_rejects_huge_integer_before_json_serialization(monkeypatch):
    log = OpLog()

    def serialization_must_not_start(*_args, **_kwargs):
        raise AssertionError("huge integer reached json.dumps")

    monkeypatch.setattr(operation_module.json, "dumps", serialization_must_not_start)
    with pytest.raises(ZeError) as caught:
        log.append(_make_op(actor={"value": 10 ** 5000}))
    _assert_safe_ze_error(caught.value, "oplog_entry_invalid")
    assert len(log) == 0


def test_oplog_reader_enforces_total_size_before_scan(monkeypatch, tmp_path):
    p = tmp_path / "oplog.ndjson"
    p.write_bytes(b"x" * 65)
    monkeypatch.setattr(operation_module, "_MAX_OPLOG_BYTES", 64)
    with pytest.raises(ZeError) as caught:
        OpLog(p)
    _assert_safe_ze_error(caught.value, "oplog_limit_exceeded")


def test_oplog_reader_enforces_single_record_size(monkeypatch, tmp_path):
    p = tmp_path / "oplog.ndjson"
    p.write_bytes(b"{" + b" " * 64 + b"}\n")
    monkeypatch.setattr(operation_module, "_MAX_OPLOG_BYTES", 1024)
    monkeypatch.setattr(operation_module, "_MAX_OPLOG_RECORD_BYTES", 64)
    with pytest.raises(ZeError) as caught:
        OpLog(p)
    _assert_safe_ze_error(caught.value, "oplog_limit_exceeded")


def test_oplog_writer_enforces_single_record_size_before_publish(monkeypatch, tmp_path):
    p = tmp_path / "oplog.ndjson"
    log = OpLog(p)
    monkeypatch.setattr(operation_module, "_MAX_OPLOG_RECORD_BYTES", 64)
    with pytest.raises(ZeError) as caught:
        log.append(_make_op())
    _assert_safe_ze_error(caught.value, "oplog_entry_invalid")
    assert len(log) == 0
    assert p.read_bytes() == b""


def test_oplog_writer_enforces_total_file_size_before_append(monkeypatch, tmp_path):
    p = tmp_path / "oplog.ndjson"
    log = OpLog(p)
    log.append(_make_op())
    before = p.read_bytes()
    monkeypatch.setattr(operation_module, "_MAX_OPLOG_BYTES", len(before) + 1)

    with pytest.raises(ZeError) as caught:
        log.append(_make_op())
    _assert_safe_ze_error(caught.value, "oplog_limit_exceeded")
    assert p.read_bytes() == before
    assert len(log) == 1
