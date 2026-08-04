"""test_capacity.py — CapacityLimits / CapacityChecker 行为测试.

覆盖:
  - CapacityLimits: 默认值 / frozen / from_env (happy + sad + 注入)
  - CapacityChecker:
      check_add_entity   — happy / boundary / 超限 / 含软删
      check_entity_data  — happy / data_size 超 / field_count 超 / 递归字段数
      check_asset_blob   — happy / 超限 / 非法 size
      check_doc_total_assets — current + proposed bytes quota
      measure_entity     — 字段齐 + 数值准确
  - DEFAULT_LIMITS 值跟 SPEC §2 一致
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# v3 平铺: 注入 document / entity 兄弟目录
_HERE = Path(__file__).resolve().parent
_DS_DIR = _HERE.parent
for sub in ("01-document", "02-entity"):
    p = _DS_DIR / sub
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from capacity import (
    CapacityChecker,
    CapacityLimits,
    DEFAULT_LIMITS,
)
from document import Document
from entity import Actor, Entity, ZeError


# ==============================================================
# 辅助
# ==============================================================

def _doc() -> Document:
    return Document(schema_version="1.0.0", collaboration_mode="with_save")


def _actor() -> Actor:
    return Actor(user_id="u1")


# ==============================================================
# CapacityLimits 默认值 + frozen
# ==============================================================

def test_default_limits_values_match_spec():
    """SPEC §2 数值."""
    assert DEFAULT_LIMITS.entity_count_max == 10_000
    assert DEFAULT_LIMITS.entity_data_size_max == 1_000_000
    assert DEFAULT_LIMITS.entity_field_count_max == 100
    assert DEFAULT_LIMITS.asset_blob_size_max == 512 * 1024 * 1024
    assert DEFAULT_LIMITS.doc_total_assets_size_max == 512 * 1024 * 1024


def test_capacity_limits_frozen():
    lim = CapacityLimits()
    with pytest.raises(Exception):
        lim.entity_count_max = 5  # type: ignore[misc]


def test_capacity_limits_custom_construct():
    lim = CapacityLimits(
        entity_count_max=5,
        entity_data_size_max=100,
        entity_field_count_max=3,
        asset_blob_size_max=1024,
        doc_total_assets_size_max=2048,
    )
    assert lim.entity_count_max == 5
    assert lim.entity_data_size_max == 100
    assert lim.entity_field_count_max == 3
    assert lim.asset_blob_size_max == 1024
    assert lim.doc_total_assets_size_max == 2048


def test_capacity_limits_hashable():
    """frozen → hashable; 可作 dict key / set 成员."""
    a = CapacityLimits()
    b = CapacityLimits()
    assert hash(a) == hash(b)
    assert {a, b} == {a}


# ==============================================================
# from_env
# ==============================================================

def test_from_env_empty_returns_default():
    lim = CapacityLimits.from_env(env={})
    assert lim == CapacityLimits()


def test_from_env_no_env_arg_reads_os_environ(monkeypatch):
    """env=None 时读 os.environ."""
    monkeypatch.setenv("ZE_CAPACITY_ENTITY_COUNT_MAX", "42")
    lim = CapacityLimits.from_env()
    assert lim.entity_count_max == 42


def test_from_env_all_fields():
    env = {
        "ZE_CAPACITY_ENTITY_COUNT_MAX": "100",
        "ZE_CAPACITY_ENTITY_DATA_SIZE_MAX": "2048",
        "ZE_CAPACITY_ENTITY_FIELD_COUNT_MAX": "50",
        "ZE_CAPACITY_ASSET_BLOB_SIZE_MAX": "67108864",
        "ZE_CAPACITY_DOC_TOTAL_ASSETS_SIZE_MAX": "134217728",
    }
    lim = CapacityLimits.from_env(env=env)
    assert lim.entity_count_max == 100
    assert lim.entity_data_size_max == 2048
    assert lim.entity_field_count_max == 50
    assert lim.asset_blob_size_max == 67108864
    assert lim.doc_total_assets_size_max == 134217728


def test_from_env_partial_falls_back_to_default():
    env = {"ZE_CAPACITY_ENTITY_COUNT_MAX": "500"}
    lim = CapacityLimits.from_env(env=env)
    assert lim.entity_count_max == 500
    # 其他保持 default
    assert lim.entity_data_size_max == DEFAULT_LIMITS.entity_data_size_max
    assert lim.entity_field_count_max == DEFAULT_LIMITS.entity_field_count_max
    assert lim.asset_blob_size_max == DEFAULT_LIMITS.asset_blob_size_max
    assert lim.doc_total_assets_size_max == DEFAULT_LIMITS.doc_total_assets_size_max


def test_from_env_empty_string_treated_as_missing():
    env = {"ZE_CAPACITY_ENTITY_COUNT_MAX": ""}
    lim = CapacityLimits.from_env(env=env)
    assert lim.entity_count_max == DEFAULT_LIMITS.entity_count_max


def test_from_env_invalid_int_raises():
    env = {"ZE_CAPACITY_ENTITY_COUNT_MAX": "not-an-int"}
    with pytest.raises(ZeError) as ex:
        CapacityLimits.from_env(env=env)
    assert ex.value.code == "capacity_config_invalid"
    assert ex.value.context["field"] == "entity_count_max"
    assert ex.value.context["value"] == "not-an-int"


def test_from_env_zero_raises():
    env = {"ZE_CAPACITY_ENTITY_DATA_SIZE_MAX": "0"}
    with pytest.raises(ZeError) as ex:
        CapacityLimits.from_env(env=env)
    assert ex.value.code == "capacity_config_invalid"
    assert ex.value.context["value"] == 0


def test_from_env_negative_raises():
    env = {"ZE_CAPACITY_ASSET_BLOB_SIZE_MAX": "-1"}
    with pytest.raises(ZeError) as ex:
        CapacityLimits.from_env(env=env)
    assert ex.value.code == "capacity_config_invalid"


# ==============================================================
# CapacityChecker — limits 访问
# ==============================================================

def test_checker_default_limits():
    c = CapacityChecker()
    assert c.limits is DEFAULT_LIMITS


def test_checker_custom_limits():
    lim = CapacityLimits(entity_count_max=5)
    c = CapacityChecker(lim)
    assert c.limits is lim
    assert c.limits.entity_count_max == 5


# ==============================================================
# check_add_entity
# ==============================================================

def test_check_add_entity_under_limit_passes():
    c = CapacityChecker(CapacityLimits(entity_count_max=10))
    doc = _doc()
    for i in range(5):
        doc.add(Entity(type="t", data={}, id=f"e{i}"))
    c.check_add_entity(doc)  # 6 ≤ 10, OK


def test_check_add_entity_at_boundary_passes():
    """当前 9 + 1 = 10 = limit, 不抛."""
    c = CapacityChecker(CapacityLimits(entity_count_max=10))
    doc = _doc()
    for i in range(9):
        doc.add(Entity(type="t", data={}, id=f"e{i}"))
    c.check_add_entity(doc)  # 10 == 10, OK


def test_check_add_entity_exceeds_raises():
    c = CapacityChecker(CapacityLimits(entity_count_max=3))
    doc = _doc()
    for i in range(3):
        doc.add(Entity(type="t", data={}, id=f"e{i}"))
    with pytest.raises(ZeError) as ex:
        c.check_add_entity(doc)
    assert ex.value.code == "capacity_exceeded"
    assert ex.value.context["limit_name"] == "entity_count_max"
    assert ex.value.context["limit"] == 3
    assert ex.value.context["actual"] == 4


def test_check_add_entity_counts_soft_deleted():
    """软删的 entity 仍占容量."""
    c = CapacityChecker(CapacityLimits(entity_count_max=2))
    doc = _doc()
    e1 = Entity(type="t", data={}, id="e1")
    doc.add(e1)
    e2 = Entity(type="t", data={}, id="e2")
    doc.add(e2)
    e1.delete(expected_version=1, actor=_actor())  # 软删
    # 仍是 2 entity, 加 1 应超
    with pytest.raises(ZeError) as ex:
        c.check_add_entity(doc)
    assert ex.value.code == "capacity_exceeded"


def test_check_add_entity_multi_type():
    """跨多 type 总数算."""
    c = CapacityChecker(CapacityLimits(entity_count_max=5))
    doc = _doc()
    doc.add(Entity(type="a", data={}, id="a1"))
    doc.add(Entity(type="a", data={}, id="a2"))
    doc.add(Entity(type="b", data={}, id="b1"))
    doc.add(Entity(type="c", data={}, id="c1"))
    c.check_add_entity(doc)   # 4 + 1 = 5, OK
    doc.add(Entity(type="c", data={}, id="c2"))
    with pytest.raises(ZeError):
        c.check_add_entity(doc)  # 5 + 1 = 6, 抛


# ==============================================================
# check_entity_data
# ==============================================================

def test_check_entity_data_under_limit_passes():
    c = CapacityChecker(CapacityLimits(
        entity_data_size_max=1000,
        entity_field_count_max=10,
    ))
    e = Entity(type="t", data={"name": "hi"})
    c.check_entity_data(e)  # OK


def test_check_entity_data_size_exceeds_raises():
    c = CapacityChecker(CapacityLimits(
        entity_data_size_max=50,
        entity_field_count_max=100,
    ))
    e = Entity(type="t", data={"big": "x" * 200})
    with pytest.raises(ZeError) as ex:
        c.check_entity_data(e)
    assert ex.value.code == "capacity_exceeded"
    assert ex.value.context["limit_name"] == "entity_data_size_max"
    assert ex.value.context["limit"] == 50
    assert ex.value.context["actual"] > 50


def test_check_entity_data_field_count_exceeds_raises():
    c = CapacityChecker(CapacityLimits(
        entity_data_size_max=1_000_000,
        entity_field_count_max=3,
    ))
    e = Entity(type="t", data={"a": 1, "b": 2, "c": 3, "d": 4})
    with pytest.raises(ZeError) as ex:
        c.check_entity_data(e)
    assert ex.value.code == "capacity_exceeded"
    assert ex.value.context["limit_name"] == "entity_field_count_max"
    assert ex.value.context["limit"] == 3
    assert ex.value.context["actual"] == 4


def test_check_entity_data_recursive_field_count():
    """嵌套 dict 字段递归计数."""
    c = CapacityChecker(CapacityLimits(
        entity_data_size_max=1_000_000,
        entity_field_count_max=3,
    ))
    # {"a": {"b": 1, "c": 2}} → 3 fields (a / a.b / a.c)
    e = Entity(type="t", data={"a": {"b": 1, "c": 2}})
    c.check_entity_data(e)  # 3 == 3, OK
    # 加一个就超
    e2 = Entity(type="t", data={"a": {"b": 1, "c": 2}, "d": 3})
    with pytest.raises(ZeError):
        c.check_entity_data(e2)


def test_check_entity_data_list_of_dict_field_count():
    """list 内 dict 也算字段."""
    c = CapacityChecker(CapacityLimits(
        entity_data_size_max=1_000_000,
        entity_field_count_max=5,
    ))
    # {"xs": [{"a": 1}, {"b": 2}]} → 3 fields (xs / xs[0].a / xs[1].b)
    e = Entity(type="t", data={"xs": [{"a": 1}, {"b": 2}]})
    c.check_entity_data(e)  # 3 ≤ 5, OK


def test_check_entity_data_size_priority():
    """data_size 跟 field_count 都超时, 先报 data_size."""
    c = CapacityChecker(CapacityLimits(
        entity_data_size_max=10,
        entity_field_count_max=1,
    ))
    e = Entity(type="t", data={"a": "long_value_string", "b": "another"})
    with pytest.raises(ZeError) as ex:
        c.check_entity_data(e)
    assert ex.value.context["limit_name"] == "entity_data_size_max"


# ==============================================================
# check_asset_blob
# ==============================================================

def test_check_asset_blob_under_limit_passes():
    c = CapacityChecker(CapacityLimits(asset_blob_size_max=1024))
    c.check_asset_blob(500)
    c.check_asset_blob(1024)   # boundary OK


def test_check_asset_blob_exceeds_raises():
    c = CapacityChecker(CapacityLimits(asset_blob_size_max=1024))
    with pytest.raises(ZeError) as ex:
        c.check_asset_blob(1025)
    assert ex.value.code == "capacity_exceeded"
    assert ex.value.context["limit_name"] == "asset_blob_size_max"
    assert ex.value.context["limit"] == 1024
    assert ex.value.context["actual"] == 1025


def test_check_asset_blob_zero_passes():
    """空 blob 合法; 由 03-asset 自身决定要不要拒."""
    c = CapacityChecker()
    c.check_asset_blob(0)


def test_check_asset_blob_negative_raises_validation():
    c = CapacityChecker()
    with pytest.raises(ZeError) as ex:
        c.check_asset_blob(-1)
    assert ex.value.code == "validation_failed"


def test_check_doc_total_assets_includes_proposed_new_blob():
    store = SimpleNamespace(total_size=lambda: 90)
    checker = CapacityChecker(CapacityLimits(doc_total_assets_size_max=100))
    checker.check_doc_total_assets(store, additional_size=10)
    with pytest.raises(ZeError) as ex:
        checker.check_doc_total_assets(store, additional_size=11)
    assert ex.value.code == "capacity_exceeded"
    assert ex.value.context == {
        "limit_name": "doc_total_assets_size_max", "limit": 100, "actual": 101,
    }


def test_check_doc_total_assets_rejects_missing_measurement_or_bad_addition():
    checker = CapacityChecker()
    with pytest.raises(ZeError, match="total_size"):
        checker.check_doc_total_assets(object())
    with pytest.raises(ZeError) as ex:
        checker.check_doc_total_assets(SimpleNamespace(total_size=lambda: 0), additional_size=-1)
    assert ex.value.code == "validation_failed"


def test_check_asset_blob_non_int_raises_validation():
    c = CapacityChecker()
    with pytest.raises(ZeError) as ex:
        c.check_asset_blob(1.5)  # type: ignore[arg-type]
    assert ex.value.code == "validation_failed"


# ==============================================================
# measure_entity
# ==============================================================

def test_measure_entity_returns_dict_with_required_keys():
    c = CapacityChecker()
    e = Entity(type="t", data={"name": "hello"})
    m = c.measure_entity(e)
    assert "data_size" in m
    assert "field_count" in m
    assert isinstance(m["data_size"], int)
    assert isinstance(m["field_count"], int)


def test_measure_entity_data_size_matches_json_utf8():
    c = CapacityChecker()
    data = {"name": "你好", "n": 42}
    e = Entity(type="t", data=data)
    m = c.measure_entity(e)
    expected = len(json.dumps(data, ensure_ascii=False).encode("utf-8"))
    assert m["data_size"] == expected


def test_measure_entity_field_count_flat():
    c = CapacityChecker()
    e = Entity(type="t", data={"a": 1, "b": 2, "c": 3})
    m = c.measure_entity(e)
    assert m["field_count"] == 3


def test_measure_entity_field_count_nested():
    c = CapacityChecker()
    # {"a": {"b": 1, "c": 2}, "d": 3} → 4 fields
    e = Entity(type="t", data={"a": {"b": 1, "c": 2}, "d": 3})
    m = c.measure_entity(e)
    assert m["field_count"] == 4


def test_measure_entity_empty_data():
    c = CapacityChecker()
    e = Entity(type="t", data={})
    m = c.measure_entity(e)
    assert m["field_count"] == 0
    # 空 dict JSON = "{}" → 2 字节
    assert m["data_size"] == 2


def test_measure_entity_does_not_raise_even_when_over_limit():
    """measure 是诊断路径, 永不抛 capacity_exceeded."""
    c = CapacityChecker(CapacityLimits(
        entity_data_size_max=1,
        entity_field_count_max=1,
    ))
    e = Entity(type="t", data={"a": "huge" * 100, "b": "more"})
    m = c.measure_entity(e)
    # 数值远超 limit, 但 measure 不抛
    assert m["data_size"] > 1
    assert m["field_count"] > 1


# ==============================================================
# 集成: caller 显式调路径
# ==============================================================

def test_integration_command_handler_pattern():
    """SPEC §6 集成示例: command handler 入口主动校验."""
    checker = CapacityChecker(CapacityLimits(
        entity_count_max=2,
        entity_data_size_max=10_000,
        entity_field_count_max=10,
    ))
    doc = _doc()

    def add_entity(payload: dict) -> Entity:
        new_entity = Entity(type=payload["type"], data=payload["data"])
        checker.check_add_entity(doc)
        checker.check_entity_data(new_entity)
        doc.add(new_entity)
        return new_entity

    # 第 1, 2 个 OK
    add_entity({"type": "t", "data": {"name": "a"}})
    add_entity({"type": "t", "data": {"name": "b"}})
    # 第 3 个超 entity_count_max
    with pytest.raises(ZeError) as ex:
        add_entity({"type": "t", "data": {"name": "c"}})
    assert ex.value.code == "capacity_exceeded"
    assert ex.value.context["limit_name"] == "entity_count_max"


def test_integration_capacity_check_does_not_modify_doc():
    """check_* 不应改 doc 状态 (无副作用)."""
    c = CapacityChecker(CapacityLimits(entity_count_max=1))
    doc = _doc()
    doc.add(Entity(type="t", data={}, id="e1"))
    v_before = doc._v
    n_before = len(doc.list("t", include_deleted=True))
    with pytest.raises(ZeError):
        c.check_add_entity(doc)
    assert doc._v == v_before
    assert len(doc.list("t", include_deleted=True)) == n_before
