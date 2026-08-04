"""test_entity.py — Entity 全实装行为测试.

覆盖:
  - 构造 + 默认值
  - CAS update / delete / restore
  - read_only / 软删 后续动作语义
  - Validator 协议 (默认 no-op / 注入真校验器)
  - to_dict / from_dict 完整 round-trip
  - 读取助手: get / provenance / actor_log
"""

from __future__ import annotations

import time

import pytest

from entity import Actor, AiProvenance, Entity, ZeError, _clone_json_value


# ==============================================================
# 构造
# ==============================================================

def test_construct_minimal():
    e = Entity(type="object", data={"name": "x"})
    assert e.type == "object"
    assert e.data == {"name": "x"}
    assert len(e.id) == 32  # uuid4 hex
    assert e.version == 1
    assert e.deleted is False
    assert e.read_only is False
    assert e.name == ""
    assert e._document_owner is None


def test_construct_with_all_fields():
    e = Entity(
        type="object",
        data={"a": 1},
        name="x",
        id="fixed-id",
        schema={"a": {"type": "number"}},
        read_only=True,
    )
    assert e.id == "fixed-id"
    assert e.name == "x"
    assert e.read_only is True


def test_construct_type_required():
    with pytest.raises(ZeError) as exc:
        Entity(type="", data={})  # type: ignore[arg-type]
    assert exc.value.code == "validation_failed"


def test_construct_auto_uuid():
    e1 = Entity(type="t", data={})
    e2 = Entity(type="t", data={})
    assert e1.id != e2.id


def test_construct_runs_validator_on_data():
    called = {"n": 0}
    def v(data, schema, path):
        called["n"] += 1
        assert data == {"a": 1}
        assert schema == {"a": {"type": "number"}}
    Entity(type="t", data={"a": 1}, schema={"a": {"type": "number"}}, validator=v)
    assert called["n"] == 1


def test_construct_no_validator_skipped_when_no_schema():
    """无 schema 不应跑 validator (即使传了)."""
    called = {"n": 0}
    def v(data, schema, path):
        called["n"] += 1
    Entity(type="t", data={"a": 1}, validator=v)  # schema 默认 {}
    assert called["n"] == 0


# ==============================================================
# property 返回 copy
# ==============================================================

def test_data_returns_copy():
    e = Entity(type="t", data={"a": 1})
    d = e.data
    d["b"] = 2
    assert e.data == {"a": 1}


def test_actor_log_returns_copy():
    e = Entity(type="t", data={})
    actor = Actor(user_id="u1")
    e.update({"x": 1}, expected_version=1, actor=actor)
    log = e.actor_log()
    log.append({"fake": True})
    assert len(e.actor_log()) == 1


# ==============================================================
# CAS update
# ==============================================================

def test_update_basic():
    e = Entity(type="t", data={"a": 1})
    actor = Actor(user_id="u1")
    e.update({"a": 2, "b": 3}, expected_version=1, actor=actor)
    assert e.data == {"a": 2, "b": 3}
    assert e.version == 2


def test_update_version_mismatch():
    e = Entity(type="t", data={})
    actor = Actor(user_id="u1")
    with pytest.raises(ZeError) as exc:
        e.update({"a": 1}, expected_version=99, actor=actor)
    assert exc.value.code == "cas_version_mismatch"
    assert e.version == 1  # 不 bump


@pytest.mark.parametrize("bad", [True, 1.0, "1", 0, -1, 2**63])
def test_all_entity_cas_entrypoints_require_exact_sqlite_positive_int(bad):
    actor = Actor(user_id="u1")
    for mutate in (
        lambda entity: entity.update({}, expected_version=bad, actor=actor),
        lambda entity: entity.delete(expected_version=bad, actor=actor),
        lambda entity: entity.restore(expected_version=bad, actor=actor),
    ):
        entity = Entity(type="t", data={})
        with pytest.raises(ZeError) as exc:
            mutate(entity)
        assert exc.value.code == "validation_failed"
        assert exc.value.context["reason"] == "expected_version_invalid"
        assert entity.version == 1


def test_entity_expected_version_rejects_hostile_equality_without_hook():
    class Hostile:
        calls = 0

        def __eq__(self, other):
            type(self).calls += 1
            raise AssertionError("equality hook must not run")

    entity = Entity(type="t", data={})
    with pytest.raises(ZeError) as exc:
        entity.update(
            {}, expected_version=Hostile(), actor=Actor(user_id="u1"),
        )
    assert exc.value.context["reason"] == "expected_version_invalid"
    assert Hostile.calls == 0
    assert entity.version == 1


def test_entity_mutations_reject_version_ceiling_without_partial_state():
    actor = Actor(user_id="u1")
    cases = []
    update_entity = Entity(type="t", data={"x": 1})
    cases.append((update_entity, lambda e: e.update(
        {"x": 2}, expected_version=2**63 - 1, actor=actor,
    )))
    delete_entity = Entity(type="t", data={})
    cases.append((delete_entity, lambda e: e.delete(
        expected_version=2**63 - 1, actor=actor,
    )))
    restore_entity = Entity(type="t", data={})
    restore_entity._deleted = True
    cases.append((restore_entity, lambda e: e.restore(
        expected_version=2**63 - 1, actor=actor,
    )))

    for entity, mutate in cases:
        entity._version = 2**63 - 1
        before = entity.to_dict()
        with pytest.raises(ZeError) as exc:
            mutate(entity)
        assert exc.value.code == "version_overflow"
        assert entity.to_dict() == before


def test_update_merges_patches():
    e = Entity(type="t", data={"a": 1, "b": 2})
    actor = Actor(user_id="u1")
    e.update({"b": 22, "c": 33}, expected_version=1, actor=actor)
    assert e.data == {"a": 1, "b": 22, "c": 33}


def test_update_name_change():
    e = Entity(type="t", data={}, name="old")
    actor = Actor(user_id="u1")
    e.update({}, expected_version=1, actor=actor, name="new")
    assert e.name == "new"


def test_update_records_actor_log():
    e = Entity(type="t", data={})
    actor = Actor(user_id="u1", device_id="d1", source="user")
    e.update({"a": 1, "b": 2}, expected_version=1, actor=actor)
    log = e.actor_log()
    assert len(log) == 1
    assert log[0]["version"] == 2
    assert log[0]["fields"] == ["a", "b"]  # sorted
    assert log[0]["actor"] == {"user_id": "u1", "device_id": "d1", "source": "user"}


def test_update_attaches_ai_provenance():
    e = Entity(type="t", data={})
    actor = Actor(user_id="u1", source="ai")
    prov = AiProvenance(actor="u1", model="gpt-4", cost_usd=0.01, token_in=100, token_out=50)
    e.update(
        {"description": "AI generated text"},
        expected_version=1,
        actor=actor,
        ai_provenance={"description": prov},
    )
    assert e.provenance("description") == prov
    assert e.provenance("other") is None


def test_update_multiple_bumps_version():
    e = Entity(type="t", data={})
    actor = Actor(user_id="u1")
    e.update({"a": 1}, expected_version=1, actor=actor)
    e.update({"a": 2}, expected_version=2, actor=actor)
    e.update({"a": 3}, expected_version=3, actor=actor)
    assert e.version == 4


def test_update_runs_validator_once_on_complete_candidate():
    """update 必须把完整候选 data 交给 validator，而不是 patch 子集。"""
    seen = []
    def v(data, schema, path):
        seen.append((dict(data), dict(schema), path))
    e = Entity(
        type="t",
        data={"a": 1},
        schema={"a": {"type": "number"}, "b": {"type": "string"}},
        validator=v,
    )
    seen.clear()  # 跳过构造时的校验
    actor = Actor(user_id="u1")
    e.update({"b": "hello"}, expected_version=1, actor=actor)
    assert len(seen) == 1
    assert seen[0] == (
        {"a": 1, "b": "hello"},
        {"a": {"type": "number"}, "b": {"type": "string"}},
        "$t",
    )


def test_update_rejects_per_call_validator_authority_override_atomically():
    """签名保留，但不同 identity 不能在单次 update 绕过已安装 authority。"""
    calls = []
    installed = lambda data, schema, path: calls.append(dict(data))
    override = lambda data, schema, path: None
    e = Entity(
        type="t",
        data={"before": 1},
        name="before",
        schema={"a": {"type": "number"}},
        validator=installed,
    )
    calls.clear()
    before = e.to_dict()
    actor = Actor(user_id="u1")

    with pytest.raises(ZeError) as exc:
        e.update(
            {"a": 1},
            expected_version=1,
            actor=actor,
            validator=override,
            name="after",
        )

    assert exc.value.code == "validation_failed"
    assert calls == []
    assert e.to_dict() == before

    # Repeating the installed object is compatible and does not change policy.
    e.update({"a": 1}, expected_version=1, actor=actor, validator=installed)
    assert calls == [{"before": 1, "a": 1}]


def test_update_validator_failure_propagates():
    """update 时 validator 抛错应传播给 caller."""
    def v(data, schema, path):
        # 只在含 "bad" 值时抛错; 构造时 data 是干净的, 跑通
        if any(val == "bad" for val in data.values()):
            raise ZeError("schema_violation", "bad value detected")
    e = Entity(type="t", data={"a": 1}, schema={"a": {"type": "number"}}, validator=v)
    actor = Actor(user_id="u1")
    with pytest.raises(ZeError) as exc:
        e.update({"a": "bad"}, expected_version=1, actor=actor)
    assert exc.value.code == "schema_violation"


# ==============================================================
# delete
# ==============================================================

def test_delete_soft():
    e = Entity(type="t", data={})
    actor = Actor(user_id="u1")
    e.delete(expected_version=1, actor=actor)
    assert e.deleted is True
    assert e.version == 2


def test_delete_version_mismatch():
    e = Entity(type="t", data={})
    actor = Actor(user_id="u1")
    with pytest.raises(ZeError) as exc:
        e.delete(expected_version=99, actor=actor)
    assert exc.value.code == "cas_version_mismatch"


def test_delete_records_actor_log():
    e = Entity(type="t", data={})
    actor = Actor(user_id="u1")
    e.delete(expected_version=1, actor=actor)
    log = e.actor_log()
    assert log[-1]["fields"] == ["__deleted__"]
    assert log[-1]["version"] == 2


def test_update_after_delete_raises():
    """软删后 update 抛 entity_soft_deleted."""
    e = Entity(type="t", data={})
    actor = Actor(user_id="u1")
    e.delete(expected_version=1, actor=actor)
    with pytest.raises(ZeError) as exc:
        e.update({"a": 1}, expected_version=2, actor=actor)
    assert exc.value.code == "entity_soft_deleted"


# ==============================================================
# restore
# ==============================================================

def test_restore_undeletes():
    e = Entity(type="t", data={})
    actor = Actor(user_id="u1")
    e.delete(expected_version=1, actor=actor)
    e.restore(expected_version=2, actor=actor)
    assert e.deleted is False
    assert e.version == 3


def test_restore_idempotent_when_not_deleted():
    """未删的 entity restore 应 idempotent (不抛错, 不 bump)."""
    e = Entity(type="t", data={})
    actor = Actor(user_id="u1")
    e.restore(expected_version=1, actor=actor)
    assert e.version == 1  # 未 bump
    assert e.deleted is False


def test_restore_records_actor_log():
    e = Entity(type="t", data={})
    actor = Actor(user_id="u1")
    e.delete(expected_version=1, actor=actor)
    e.restore(expected_version=2, actor=actor)
    log = e.actor_log()
    assert log[-1]["fields"] == ["__restored__"]
    assert log[-1]["version"] == 3


def test_restore_version_mismatch():
    e = Entity(type="t", data={})
    actor = Actor(user_id="u1")
    e.delete(expected_version=1, actor=actor)
    with pytest.raises(ZeError) as exc:
        e.restore(expected_version=99, actor=actor)
    assert exc.value.code == "cas_version_mismatch"


def test_update_after_restore_works():
    """删 → 恢复 → update 应该能继续."""
    e = Entity(type="t", data={"a": 1})
    actor = Actor(user_id="u1")
    e.delete(expected_version=1, actor=actor)
    e.restore(expected_version=2, actor=actor)
    e.update({"a": 99}, expected_version=3, actor=actor)
    assert e.data == {"a": 99}
    assert e.version == 4


# ==============================================================
# read_only entity
# ==============================================================

def test_read_only_update_rejected():
    e = Entity(type="t", data={}, read_only=True)
    actor = Actor(user_id="u1")
    with pytest.raises(ZeError) as exc:
        e.update({"a": 1}, expected_version=1, actor=actor)
    assert exc.value.code == "read_only_violation"


def test_read_only_delete_rejected():
    e = Entity(type="t", data={}, read_only=True)
    actor = Actor(user_id="u1")
    with pytest.raises(ZeError) as exc:
        e.delete(expected_version=1, actor=actor)
    assert exc.value.code == "read_only_violation"


def test_read_only_restore_rejected():
    """read_only entity 即使没删 restore 也拒 (保持一致, 不偷偷 mutate)."""
    e = Entity(type="t", data={}, read_only=True)
    actor = Actor(user_id="u1")
    with pytest.raises(ZeError) as exc:
        e.restore(expected_version=1, actor=actor)
    assert exc.value.code == "read_only_violation"


# ==============================================================
# 读取助手
# ==============================================================

def test_get_existing():
    e = Entity(type="t", data={"a": 1, "b": "x"})
    assert e.get("a") == 1
    assert e.get("b") == "x"


def test_get_missing_returns_default():
    e = Entity(type="t", data={})
    assert e.get("none") is None
    assert e.get("none", "fallback") == "fallback"


def test_provenance_missing_returns_none():
    e = Entity(type="t", data={})
    assert e.provenance("nope") is None


def test_actor_log_initial_empty():
    e = Entity(type="t", data={})
    assert e.actor_log() == []


# ==============================================================
# to_dict / from_dict round-trip
# ==============================================================

def test_to_dict_basic():
    e = Entity(type="t", data={"a": 1}, name="x")
    d = e.to_dict()
    assert d["id"] == e.id
    assert d["type"] == "t"
    assert d["name"] == "x"
    assert d["version"] == 1
    assert d["deleted"] is False
    assert d["read_only"] is False
    assert d["data"] == {"a": 1}
    assert d["provenance"] == {}
    assert d["actor_log"] == []


def test_to_dict_includes_provenance():
    e = Entity(type="t", data={})
    actor = Actor(user_id="u1", source="ai")
    prov = AiProvenance(actor="u1", model="gpt-4", cost_usd=0.01)
    e.update({"x": "ai"}, expected_version=1, actor=actor, ai_provenance={"x": prov})
    d = e.to_dict()
    assert d["provenance"]["x"]["model"] == "gpt-4"
    assert d["provenance"]["x"]["cost_usd"] == 0.01


def test_from_dict_basic():
    original = Entity(type="t", data={"a": 1}, name="x", id="fixed-id")
    d = original.to_dict()
    restored = Entity.from_dict(d)
    assert restored.id == "fixed-id"
    assert restored.type == "t"
    assert restored.name == "x"
    assert restored.data == {"a": 1}
    assert restored.version == 1


def test_from_dict_preserves_version_no_bump():
    """from_dict 不应走 update, 不应 bump version."""
    e = Entity(type="t", data={})
    actor = Actor(user_id="u1")
    for _ in range(5):
        e.update({"a": e.version}, expected_version=e.version, actor=actor)
    # 现在 version=6
    d = e.to_dict()
    restored = Entity.from_dict(d)
    assert restored.version == 6


def test_from_dict_preserves_actor_log():
    e = Entity(type="t", data={})
    actor = Actor(user_id="u1")
    e.update({"a": 1}, expected_version=1, actor=actor)
    e.update({"b": 2}, expected_version=2, actor=actor)
    d = e.to_dict()
    restored = Entity.from_dict(d)
    assert len(restored.actor_log()) == 2
    assert restored.actor_log()[0]["fields"] == ["a"]


def test_from_dict_preserves_provenance():
    e = Entity(type="t", data={})
    actor = Actor(user_id="u1", source="ai")
    prov = AiProvenance(actor="u1", model="gpt-4", cost_usd=0.5, token_in=10)
    e.update({"x": "v"}, expected_version=1, actor=actor, ai_provenance={"x": prov})
    d = e.to_dict()
    restored = Entity.from_dict(d)
    p = restored.provenance("x")
    assert p is not None
    assert p.model == "gpt-4"
    assert p.cost_usd == 0.5
    assert p.token_in == 10


def test_from_dict_preserves_timestamps():
    e = Entity(type="t", data={})
    original_created = e.created_at
    original_updated = e.updated_at
    d = e.to_dict()
    time.sleep(0.01)
    restored = Entity.from_dict(d)
    assert restored.created_at == original_created
    assert restored.updated_at == original_updated


def test_from_dict_preserves_deleted_state():
    e = Entity(type="t", data={})
    actor = Actor(user_id="u1")
    e.delete(expected_version=1, actor=actor)
    d = e.to_dict()
    restored = Entity.from_dict(d)
    assert restored.deleted is True
    assert restored.version == 2


def test_from_dict_preserves_read_only():
    e = Entity(type="t", data={}, read_only=True)
    d = e.to_dict()
    restored = Entity.from_dict(d)
    assert restored.read_only is True


def test_from_dict_tolerates_unknown_provenance_keys():
    """forward-compat: provenance dict 有未知字段不应崩, 应丢弃该条."""
    d = {
        "id": "id1",
        "type": "t",
        "name": "",
        "version": 1,
        "deleted": False,
        "read_only": False,
        "data": {},
        "provenance": {
            "x": {"actor": "u1", "model": "m", "unknown_key": "drops"},
        },
        "actor_log": [],
        "created_at": 1.0,
        "updated_at": 1.0,
    }
    restored = Entity.from_dict(d)
    # 应该丢弃带未知 key 的 provenance, restored.provenance("x") = None
    assert restored.provenance("x") is None


# ==============================================================
# Actor / AiProvenance 值类型
# ==============================================================

def test_actor_immutable():
    actor = Actor(user_id="u1")
    with pytest.raises(Exception):
        actor.user_id = "u2"  # type: ignore[misc]


def test_actor_default_source():
    actor = Actor(user_id="u1")
    assert actor.source == "user"
    assert actor.device_id == "default"


def test_ai_provenance_default_fields():
    p = AiProvenance(actor="u1", model="gpt-4")
    assert p.cost_usd == 0.0
    assert p.token_in == 0
    assert p.prompt_args == {}


def test_ai_provenance_to_dict():
    p = AiProvenance(actor="u1", model="gpt-4", cost_usd=0.1)
    d = p.to_dict()
    assert d["actor"] == "u1"
    assert d["model"] == "gpt-4"
    assert d["cost_usd"] == 0.1


# ==============================================================
# install_validator (framework 内部 hook)
# ==============================================================

def test_install_validator_sets_schema_and_validator():
    e = Entity(type="t", data={"a": 1})
    assert not e.has_validator    # 默认 no-op
    called = []
    def v(data, schema, path):
        called.append((dict(data), dict(schema), path))
    e.install_validator({"a": 1}, v)
    assert e.has_validator
    assert len(called) == 1       # install 时立即跑一次
    assert called[0][0] == {"a": 1}


def test_install_validator_fails_when_current_data_invalid():
    """install 时立即校验; 现有 data 不通过应抛."""
    e = Entity(type="t", data={"bad": True})
    def v(data, schema, path):
        if "bad" in data:
            raise ZeError("schema_violation", "bad")
    with pytest.raises(ZeError) as exc:
        e.install_validator({"some": "schema"}, v)
    assert exc.value.code == "schema_violation"


def test_install_validator_with_empty_schema_no_op():
    """schema 是空 dict 时不跑校验."""
    e = Entity(type="t", data={"bad": True})
    called = []
    e.install_validator({}, lambda d, s, p: called.append(p))
    assert called == []           # 空 schema 不跑


def test_install_validator_subsequent_update_uses_new_validator():
    """install 后, 后续 update 应该用新 validator."""
    e = Entity(type="t", data={"a": 1})

    # install 一个会拒"bad"值的 validator
    def check_a(data, schema, path):
        if data.get("a") == "bad":
            raise ZeError("schema_violation", "a must not be 'bad'")
    e.install_validator({"a": "schema"}, check_a)

    # 合法 update 通过
    e.update({"a": 2}, expected_version=1, actor=Actor(user_id="u1"))

    # 非法 update 失败
    with pytest.raises(ZeError) as exc:
        e.update({"a": "bad"}, expected_version=2, actor=Actor(user_id="u1"))
    assert exc.value.code == "schema_violation"


def test_attach_validator_does_not_revalidate_current_data():
    """attach_validator 与 install_validator 的关键区别: 挂上校验器但**不**立即校验现有 data.

    给 Document.open 的持久化恢复路径用: 历史脏值不能让 open 抛, 但未来 update 要受校验.
    """
    e = Entity(type="t", data={"bad": True})
    def v(data, schema, path):
        if "bad" in data:
            raise ZeError("schema_violation", "bad")
    # install 会立即抛; attach 不会
    schema = {"type": "object", "properties": {"bad": {"type": "boolean"}}}
    with pytest.raises(ZeError):
        Entity(type="t", data={"bad": True}).install_validator(schema, v)
    e.attach_validator(schema, v)                # 不抛
    assert e.has_validator                       # 校验器已挂
    # 但后续 update 生效: 写脏值被拒 (update 按 patches 的顶层字段查 properties 跑校验)
    with pytest.raises(ZeError) as exc:
        e.update({"bad": True}, expected_version=1, actor=Actor(user_id="u1"))
    assert exc.value.code == "schema_violation"


def test_attach_validator_non_callable_failure_is_atomic():
    """新 schema 可拥有化也不能在 validator 失败时留下半次安装。"""
    old_validator = lambda data, schema, path: None
    e = Entity(
        type="t",
        data={"x": 1},
        schema={"x": {"type": "integer"}},
        validator=old_validator,
    )
    old_schema = e._schema

    with pytest.raises(ZeError) as exc:
        e.attach_validator(
            {"replacement": {"type": "string"}},
            object(),  # type: ignore[arg-type]
        )

    assert exc.value.code == "validation_failed"
    assert e._schema is old_schema
    assert e._validator is old_validator


def test_has_validator_default_false():
    e = Entity(type="t", data={})
    assert e.has_validator is False


def test_has_validator_true_after_construct_with_validator():
    """构造时传 validator 应该 has_validator=True."""
    e = Entity(type="t", data={"a": 1}, validator=lambda d, s, p: None)
    assert e.has_validator is True


# ==============================================================
# strict JSON value ownership
# ==============================================================

def test_clone_json_accepts_exact_scalars_and_int_digit_boundary():
    max_positive = (10 ** 4300) - 1
    max_negative = -max_positive
    values = [None, False, True, 0, 1.25, "中文", max_positive, max_negative]

    for value in values:
        assert _clone_json_value(value) == value

    for value in (10 ** 4300, -(10 ** 4300)):
        with pytest.raises(ZeError) as exc:
            _clone_json_value(value)
        assert exc.value.code == "validation_failed"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_clone_json_rejects_non_finite_float(value):
    with pytest.raises(ZeError) as exc:
        _clone_json_value(value)
    assert exc.value.code == "validation_failed"


def test_clone_json_rejects_non_utf8_string_values_and_keys():
    lone_surrogate = "\ud800"

    for value in (lone_surrogate, {lone_surrogate: 1}, {"x": lone_surrogate}):
        with pytest.raises(ZeError) as exc:
            _clone_json_value(value)
        assert exc.value.code == "validation_failed"
        assert exc.value.__cause__ is None
        assert exc.value.__context__ is None


def test_clone_json_rejects_subclasses_without_invoking_copy_or_repr_hooks():
    hooks_called = []

    class DictSubclass(dict):
        def items(self):
            hooks_called.append("items")
            raise AssertionError("must not iterate rejected subclass")

    class ListSubclass(list):
        def __iter__(self):
            hooks_called.append("iter")
            raise AssertionError("must not iterate rejected subclass")

    class IntSubclass(int):
        pass

    class FloatSubclass(float):
        pass

    class StrSubclass(str):
        pass

    class CopyTrap:
        def __deepcopy__(self, memo):
            hooks_called.append("deepcopy")
            raise AssertionError("deepcopy must not run")

        def __repr__(self):
            hooks_called.append("repr")
            raise AssertionError("repr must not run")

    rejected = [
        DictSubclass({"x": 1}),
        ListSubclass([1]),
        IntSubclass(1),
        FloatSubclass(1.0),
        StrSubclass("value"),
        {StrSubclass("key"): 1},
        {"nested": CopyTrap()},
    ]
    for value in rejected:
        with pytest.raises(ZeError) as exc:
            _clone_json_value(value)
        assert exc.value.code == "validation_failed"
        assert exc.value.message == "value must be strict JSON data"
        assert exc.value.context == {}

    assert hooks_called == []


def test_clone_json_rejects_cycles_with_stable_payload_free_error():
    list_cycle = []
    list_cycle.append(list_cycle)
    dict_cycle = {}
    dict_cycle["self"] = dict_cycle

    for value in (list_cycle, dict_cycle):
        with pytest.raises(ZeError) as exc:
            _clone_json_value(value)
        assert str(exc.value) == "[validation_failed] value must be strict JSON data"
        assert exc.value.context == {}


def test_clone_json_container_depth_boundary():
    allowed = 0
    for _ in range(120):
        allowed = [allowed]

    cloned = _clone_json_value(allowed)
    for _ in range(120):
        assert type(cloned) is list
        cloned = cloned[0]
    assert cloned == 0

    too_deep = [allowed]
    with pytest.raises(ZeError) as exc:
        _clone_json_value(too_deep)
    assert exc.value.code == "validation_failed"


def test_clone_json_rejects_exponentially_expanding_shared_dag():
    shared = []
    # Only O(depth) source containers, but naive alias expansion would create
    # over a million output nodes.  The shared expanded-node budget must stop it.
    for _ in range(20):
        shared = [shared, shared]

    with pytest.raises(ZeError) as exc:
        _clone_json_value(shared)
    assert exc.value.code == "validation_failed"


def test_constructor_owns_nested_data_schema_and_breaks_input_aliases():
    shared_data = {"items": [1]}
    source_data = {"left": shared_data, "right": shared_data}
    shared_schema = {"type": "array"}
    source_schema = {"left": shared_schema, "right": shared_schema}
    validator_views = []

    def validator(data, schema, path):
        validator_views.append((data, schema, path))

    e = Entity(
        type="t",
        data=source_data,
        schema=source_schema,
        validator=validator,
    )

    shared_data["items"].append(2)
    shared_schema["type"] = "string"
    validator_views[0][0]["left"]["items"].append(3)
    validator_views[0][1]["left"]["type"] = "number"

    detached = e.data
    assert detached == {"left": {"items": [1]}, "right": {"items": [1]}}
    assert detached["left"] is not detached["right"]
    assert e._schema == {"left": {"type": "array"}, "right": {"type": "array"}}


def test_data_get_and_to_dict_are_deep_detached():
    e = Entity(type="t", data={"nested": {"items": [1]}})

    data_view = e.data
    get_view = e.get("nested")
    serialized = e.to_dict()
    data_view["nested"]["items"].append(2)
    get_view["items"].append(3)
    serialized["data"]["nested"]["items"].append(4)

    assert e.data == {"nested": {"items": [1]}}


def test_update_owns_nested_patches_name_and_provenance_before_apply():
    e = Entity(type="t", data={})
    shared = {"items": [1]}
    patches = {"left": shared, "right": shared}
    prompt_args = {"messages": [{"text": "hello"}]}
    prov = AiProvenance(actor="u", model="m", prompt_args=prompt_args)
    provenance_input = {"left": prov}

    e.update(
        patches,
        expected_version=1,
        actor=Actor(user_id="u", source="ai"),
        name="owned-name",
        ai_provenance=provenance_input,
    )

    shared["items"].append(2)
    patches["extra"] = True
    provenance_input.clear()
    prov.prompt_args["messages"][0]["text"] = "mutated"

    assert e.name == "owned-name"
    assert e.data == {"left": {"items": [1]}, "right": {"items": [1]}}
    stored = e.provenance("left")
    assert stored is not None
    assert stored.prompt_args == {"messages": [{"text": "hello"}]}


def test_update_rejection_is_atomic_for_cycle_name_subclass_and_provenance_subclass():
    class StrSubclass(str):
        pass

    class ProvenanceMapSubclass(dict):
        pass

    actor = Actor(user_id="u")
    e = Entity(type="t", data={"before": 1}, name="before")

    cyclic = []
    cyclic.append(cyclic)
    attempts = [
        ({"bad": cyclic}, None, None),
        ({"ok": 2}, StrSubclass("bad"), None),
        ({"ok": 2}, None, ProvenanceMapSubclass()),
    ]
    for patches, name, provenance in attempts:
        with pytest.raises(ZeError) as exc:
            e.update(
                patches,
                expected_version=1,
                actor=actor,
                name=name,
                ai_provenance=provenance,
            )
        assert exc.value.code == "validation_failed"
        assert e.version == 1
        assert e.name == "before"
        assert e.data == {"before": 1}


def test_ai_provenance_owns_prompt_args_and_all_outputs_are_detached():
    source = {"nested": {"items": [1]}}
    p = AiProvenance(actor="u", model="m", prompt_args=source)
    source["nested"]["items"].append(2)

    serialized = p.to_dict()
    serialized["prompt_args"]["nested"]["items"].append(3)
    p.prompt_args["nested"]["items"].append(4)

    # Mutating the dataclass's public nested dict is allowed, but each output
    # owns a fresh tree and no constructor input alias survived.
    assert source == {"nested": {"items": [1, 2]}}
    assert serialized["prompt_args"] == {"nested": {"items": [1, 3]}}
    assert p.prompt_args == {"nested": {"items": [1, 4]}}


def test_entity_provenance_and_actor_log_outputs_are_deep_detached():
    e = Entity(type="t", data={})
    prov = AiProvenance(actor="u", model="m", prompt_args={"nested": {"x": 1}})
    e.update(
        {"x": 1},
        expected_version=1,
        actor=Actor(user_id="u", device_id="d", source="ai"),
        ai_provenance={"x": prov},
    )

    provenance_view = e.provenance("x")
    assert provenance_view is not None
    provenance_view.prompt_args["nested"]["x"] = 99
    log_view = e.actor_log()
    log_view[0]["actor"]["user_id"] = "mutated"
    log_view[0]["fields"].append("fake")

    assert e.provenance("x").prompt_args == {"nested": {"x": 1}}
    assert e.actor_log()[0]["actor"]["user_id"] == "u"
    assert e.actor_log()[0]["fields"] == ["x"]


def test_from_dict_owns_nested_input_and_breaks_aliases():
    shared = {"items": [1]}
    payload = {
        "id": "id1",
        "type": "t",
        "name": "n",
        "version": 2,
        "deleted": False,
        "read_only": False,
        "data": {"left": shared, "right": shared},
        "provenance": {
            "left": {
                "actor": "u",
                "model": "m",
                "prompt_args": {"nested": {"x": 1}},
            },
        },
        "actor_log": [{"actor": {"user_id": "u"}, "fields": ["left"]}],
        "created_at": 1.0,
        "updated_at": 2.0,
    }
    e = Entity.from_dict(payload)

    shared["items"].append(2)
    payload["provenance"]["left"]["prompt_args"]["nested"]["x"] = 99
    payload["actor_log"][0]["actor"]["user_id"] = "mutated"

    detached = e.data
    assert detached == {"left": {"items": [1]}, "right": {"items": [1]}}
    assert detached["left"] is not detached["right"]
    assert e.provenance("left").prompt_args == {"nested": {"x": 1}}
    assert e.actor_log()[0]["actor"]["user_id"] == "u"


def test_callable_validator_identity_is_preserved_without_truth_or_copy_hooks():
    class ValidatorTrap:
        def __init__(self):
            self.calls = 0

        def __bool__(self):
            raise AssertionError("validator truth hook must not run")

        def __deepcopy__(self, memo):
            raise AssertionError("validator deepcopy hook must not run")

        def __call__(self, data, schema, path):
            self.calls += 1

    validator = ValidatorTrap()
    e = Entity(
        type="t",
        data={"x": 1},
        schema={"x": {"type": "integer"}},
        validator=validator,
    )
    assert e._validator is validator
    assert validator.calls == 1

    e.update(
        {"x": 2},
        expected_version=1,
        actor=Actor(user_id="u"),
        validator=validator,
    )
    assert e._validator is validator
    assert validator.calls == 2


@pytest.mark.parametrize(
    "kwargs",
    [
        {"user_id": []},
        {"user_id": "u", "device_id": {}},
        {"user_id": "u", "source": "evil"},
    ],
)
def test_actor_rejects_values_outside_persisted_semantic_domain(kwargs):
    with pytest.raises(ZeError) as exc:
        Actor(**kwargs)
    assert exc.value.code == "validation_failed"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"actor": [], "model": "m"},
        {"actor": "u", "model": {}},
        {"actor": "u", "model": "m", "cost_usd": -1},
        {"actor": "u", "model": "m", "token_in": -1},
        {"actor": "u", "model": "m", "token_out": 1.5},
        {"actor": "u", "model": "m", "ts": float("inf")},
    ],
)
def test_ai_provenance_rejects_values_outside_persisted_semantic_domain(kwargs):
    with pytest.raises(ZeError) as exc:
        AiProvenance(**kwargs)
    assert exc.value.code == "validation_failed"


def test_update_rejects_dot_path_that_would_escape_owned_depth_domain():
    entity = Entity(type="t", data={})
    actor = Actor(user_id="u")
    too_deep_path = ".".join(["a"] * 121)

    with pytest.raises(ZeError) as exc:
        entity.update(
            {too_deep_path: 1}, expected_version=1, actor=actor,
        )
    assert exc.value.code == "validation_failed"
    assert entity.version == 1
    assert entity.data == {}


def test_constructor_rejects_empty_explicit_id_and_non_exact_read_only():
    class TruthTrap:
        def __bool__(self):
            raise AssertionError("read_only truth hook must not run")

    for kwargs in ({"id": ""}, {"read_only": 1}, {"read_only": TruthTrap()}):
        with pytest.raises(ZeError) as exc:
            Entity(type="t", data={}, **kwargs)
        assert exc.value.code == "validation_failed"
        assert exc.value.__cause__ is None
        assert exc.value.__context__ is None


def test_non_callable_validator_rejected_by_all_ingress_paths():
    actor = Actor(user_id="u")
    with pytest.raises(ZeError) as exc:
        Entity(type="t", data={}, validator=object())
    assert exc.value.code == "validation_failed"

    e = Entity(type="t", data={"before": 1})
    with pytest.raises(ZeError) as exc:
        e.update(
            {"after": 2},
            expected_version=1,
            actor=actor,
            validator=object(),
        )
    assert exc.value.code == "validation_failed"
    assert e.version == 1
    assert e.data == {"before": 1}

    for method in (e.install_validator, e.attach_validator):
        with pytest.raises(ZeError) as exc:
            method({}, object())
        assert exc.value.code == "validation_failed"


def test_provenance_clone_rejects_extra_or_missing_instance_state():
    actor = Actor(user_id="u", source="ai")

    with_extra = AiProvenance(actor="u", model="m")
    object.__setattr__(with_extra, "future_field", "must-not-drop")
    with pytest.raises(ZeError) as exc:
        Entity(type="t", data={}).update(
            {"x": 1},
            expected_version=1,
            actor=actor,
            ai_provenance={"x": with_extra},
        )
    assert exc.value.code == "validation_failed"

    with_missing = AiProvenance(actor="u", model="m")
    del with_missing.__dict__["trace_id"]
    with pytest.raises(ZeError) as exc:
        with_missing.to_dict()
    assert exc.value.code == "validation_failed"


def test_from_dict_legacy_scalar_coercion_uses_only_exact_json_scalars():
    payload = {
        "id": "id1",
        "type": "t",
        "data": {},
        "version": "2",
        "deleted": 0,
        "read_only": 1,
        "actor_log": [],
        "provenance": {},
        "created_at": "1.25",
        "updated_at": 2,
    }
    e = Entity.from_dict(payload)
    assert e.version == 2
    assert e.deleted is False
    assert e.read_only is True
    assert e.created_at == 1.25
    assert e.updated_at == 2.0


def test_from_dict_rejects_hostile_metadata_without_running_conversion_hooks():
    hooks_called = []

    class ConversionTrap:
        def __bool__(self):
            hooks_called.append("bool")
            raise AssertionError("bool hook must not run")

        def __int__(self):
            hooks_called.append("int")
            raise AssertionError("int hook must not run")

        def __float__(self):
            hooks_called.append("float")
            raise AssertionError("float hook must not run")

        def __repr__(self):
            hooks_called.append("repr")
            raise AssertionError("repr hook must not run")

    base = {
        "id": "id1",
        "type": "t",
        "data": {},
        "actor_log": [],
        "provenance": {},
    }
    for field_name in ("version", "deleted", "read_only", "created_at", "updated_at"):
        payload = dict(base)
        payload[field_name] = ConversionTrap()
        with pytest.raises(ZeError) as exc:
            Entity.from_dict(payload)
        assert exc.value.code == "validation_failed"
        assert exc.value.__cause__ is None
        assert exc.value.__context__ is None
    assert hooks_called == []


def test_from_dict_failed_builtin_conversion_has_no_chained_payload_exception():
    payload = {
        "id": "id1",
        "type": "t",
        "data": {},
        "version": "not-an-integer",
        "actor_log": [],
        "provenance": {},
    }
    with pytest.raises(ZeError) as exc:
        Entity.from_dict(payload)
    assert exc.value.code == "validation_failed"
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None


def test_update_rejects_combined_provenance_node_overflow_atomically():
    """两条各自合法的 provenance 合并后超出单 Entity envelope 也必须拒绝。"""
    e = Entity(type="t", data={"before": 1}, name="before")
    before = e.to_dict()
    first = AiProvenance(
        actor="u", model="m", prompt_args={"items": [0] * 131_000},
    )
    second = AiProvenance(
        actor="u", model="m", prompt_args={"items": [0] * 131_000},
    )

    with pytest.raises(ZeError) as exc:
        e.update(
            {"after": 2},
            expected_version=1,
            actor=Actor(user_id="u", source="ai"),
            ai_provenance={"after": first, "other": second},
            name="after",
        )

    assert exc.value.code == "validation_failed"
    assert e.to_dict() == before


def test_update_rejects_provenance_that_only_overflows_with_existing_state():
    """单条 ingress 可通过时，最终 envelope 仍须和既有 provenance 合并预算。"""
    e = Entity(type="t", data={})
    actor = Actor(user_id="u", source="ai")
    first = AiProvenance(
        actor="u", model="m", prompt_args={"items": [0] * 131_000},
    )
    second = AiProvenance(
        actor="u", model="m", prompt_args={"items": [0] * 131_000},
    )
    e.update(
        {"first": 1},
        expected_version=1,
        actor=actor,
        ai_provenance={"first": first},
    )
    before = e.to_dict()

    with pytest.raises(ZeError) as exc:
        e.update(
            {"second": 2},
            expected_version=2,
            actor=actor,
            ai_provenance={"second": second},
            name="must-not-stick",
        )

    assert exc.value.code == "validation_failed"
    assert e.to_dict() == before


@pytest.mark.parametrize("mutation", ["update", "delete", "restore"])
def test_mutation_log_that_would_overflow_durable_node_domain_is_atomic(mutation):
    """data 仍可持有时，新增一条 log 若越界，三种写路径都不能先改后验。"""
    data = {"items": [0] * 249_965}
    if mutation == "restore":
        e = Entity.from_dict({
            "id": "near-limit",
            "type": "t",
            "name": "before",
            "version": 1,
            "deleted": True,
            "data": data,
        })
    else:
        e = Entity(type="t", id="near-limit", data=data, name="before")
    before = e.to_dict()
    actor = Actor(user_id="u")

    with pytest.raises(ZeError) as exc:
        if mutation == "update":
            e.update({}, expected_version=1, actor=actor, name="after")
        elif mutation == "delete":
            e.delete(expected_version=1, actor=actor)
        else:
            e.restore(expected_version=1, actor=actor)

    assert exc.value.code == "validation_failed"
    assert e.to_dict() == before


def test_constructor_and_from_dict_reject_payload_only_aggregate_metadata_pushes_over_limit():
    """入口必须预算最终 Entity envelope，不只预算传入的 data 子树。"""
    near_limit_data = {"items": [0] * 249_980}

    with pytest.raises(ZeError) as exc:
        Entity(type="t", data=near_limit_data)
    assert exc.value.code == "validation_failed"

    # 这个精简输入 envelope 本身仍在 250k 内；补齐 Entity 默认 metadata 后才越界。
    with pytest.raises(ZeError) as exc:
        Entity.from_dict({"id": "e", "type": "t", "data": near_limit_data})
    assert exc.value.code == "validation_failed"
