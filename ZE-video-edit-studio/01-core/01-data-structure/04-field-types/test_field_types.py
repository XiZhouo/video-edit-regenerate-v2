"""test_field_types.py — JSON Schema 驼峰校验测试.

覆盖:
  - 6 基础类型 happy + sad
  - 各类型约束
  - 复合 oneOf / anyOf / allOf
  - required 双风格兼容
  - asset_ref / entity_ref 形态校验
  - null 类型 + nullable
  - bool 不视为 number
  - make_validator 工厂
"""

from __future__ import annotations

import pytest

from field_types import (
    KNOWN_TYPES,
    canonicalize_entity_schema,
    describe_schema,
    diagnose,
    is_framework_validator,
    make_validator,
    minimal_example,
    sanitize,
    validate,
)
from entity import ZeError


def test_sanitize_strips_control_keeps_whitespace():
    assert sanitize("a\x00b\x07c\td\ne\rf") == "abc\td\ne\rf"


def test_sanitize_maxlength_truncates():
    assert sanitize("hello world", {"type": "string", "maxLength": 5}) == "hello"


def test_sanitize_recurses_and_immutable():
    src = {"name": "x\x00y", "tags": ["a\x01", "b"]}
    out = sanitize(src, {"type": "object", "properties": {
        "name": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}}}})
    assert out == {"name": "xy", "tags": ["a", "b"]}
    assert src == {"name": "x\x00y", "tags": ["a\x01", "b"]}  # 原值不变


def test_sanitize_passthrough_non_string():
    assert sanitize(42) == 42
    assert sanitize(True) is True
    assert sanitize(None) is None


def _bad(value, schema, path="$"):
    """断言 validate 抛 schema_violation."""
    with pytest.raises(ZeError) as exc:
        validate(value, schema, path)
    assert exc.value.code == "schema_violation"
    return exc.value


# ==============================================================
# string
# ==============================================================

def test_string_ok():
    validate("hello", {"type": "string"})


def test_string_non_string_rejected():
    _bad(123, {"type": "string"})
    _bad(None, {"type": "string"})
    _bad(["x"], {"type": "string"})


def test_string_min_max_length():
    validate("ab", {"type": "string", "minLength": 2})
    validate("ab", {"type": "string", "maxLength": 2})
    _bad("a", {"type": "string", "minLength": 2})
    _bad("abc", {"type": "string", "maxLength": 2})


def test_string_pattern():
    validate("abc123", {"type": "string", "pattern": r"[a-z]+\d+"})
    _bad("ABC", {"type": "string", "pattern": r"[a-z]+\d+"})


def test_string_enum():
    s = {"type": "string", "enum": ["red", "green", "blue"]}
    validate("red", s)
    _bad("yellow", s)


def test_string_format_email():
    s = {"type": "string", "format": "email"}
    validate("x@y.z", s)
    _bad("not-email", s)


def test_string_format_uri():
    s = {"type": "string", "format": "uri"}
    validate("https://example.com/x", s)
    _bad("just-text", s)


def test_string_format_uuid():
    s = {"type": "string", "format": "uuid"}
    validate("abc123def456abc123def456abc12345", s)              # 32 hex
    validate("12345678-1234-1234-1234-123456789012", s)          # 36 with hyphens
    _bad("not-a-uuid", s)


def test_string_format_iso_datetime():
    s = {"type": "string", "format": "iso_datetime"}
    validate("2026-01-15T12:30:00", s)
    validate("2026-01-15T12:30:00.123Z", s)
    validate("2026-01-15T12:30:00+08:00", s)
    _bad("2026-01-15", s)


def test_string_format_unknown_raises():
    _bad("anything", {"type": "string", "format": "weird_format"})


# ==============================================================
# number / integer
# ==============================================================

def test_number_ok():
    validate(3, {"type": "number"})
    validate(3.14, {"type": "number"})


def test_number_bool_rejected():
    """bool 不视为 number (Python 坑修复)."""
    _bad(True, {"type": "number"})
    _bad(False, {"type": "number"})


def test_integer_ok():
    validate(3, {"type": "integer"})


def test_integer_float_rejected():
    _bad(3.5, {"type": "integer"})


def test_number_bounds():
    s = {"type": "number", "minimum": 0, "maximum": 10}
    validate(0, s); validate(10, s); validate(5, s)
    _bad(-1, s); _bad(11, s)


def test_number_exclusive_bounds():
    s = {"type": "number", "exclusiveMinimum": 0, "exclusiveMaximum": 10}
    validate(1, s); validate(9, s)
    _bad(0, s); _bad(10, s)


def test_number_multiple_of():
    s = {"type": "number", "multipleOf": 5}
    validate(0, s); validate(5, s); validate(10, s)
    _bad(3, s); _bad(7, s)


# ==============================================================
# boolean
# ==============================================================

def test_boolean_ok():
    validate(True, {"type": "boolean"})
    validate(False, {"type": "boolean"})


def test_boolean_rejects_int():
    _bad(1, {"type": "boolean"})
    _bad(0, {"type": "boolean"})
    _bad("true", {"type": "boolean"})


# ==============================================================
# null
# ==============================================================

def test_null_ok():
    validate(None, {"type": "null"})


def test_null_rejects_others():
    _bad(0, {"type": "null"})
    _bad("", {"type": "null"})
    _bad(False, {"type": "null"})


def test_nullable_via_oneof():
    """nullable string pattern."""
    s = {"oneOf": [{"type": "string"}, {"type": "null"}]}
    validate("hello", s)
    validate(None, s)
    _bad(123, s)


# ==============================================================
# array
# ==============================================================

def test_array_ok():
    validate([1, 2, 3], {"type": "array"})


def test_array_non_list_rejected():
    _bad({}, {"type": "array"})
    _bad("not list", {"type": "array"})


def test_array_min_max_items():
    s = {"type": "array", "minItems": 2, "maxItems": 4}
    validate([1, 2], s); validate([1, 2, 3, 4], s)
    _bad([1], s); _bad([1, 2, 3, 4, 5], s)


def test_array_unique_items():
    s = {"type": "array", "uniqueItems": True}
    validate([1, 2, 3], s)
    _bad([1, 2, 1], s)


def test_array_items_schema():
    s = {"type": "array", "items": {"type": "string"}}
    validate(["a", "b"], s)
    _bad(["a", 1], s)


def test_array_itemSchema_alias_v2_compat():
    """v2 用 itemSchema 别名, 也接受."""
    s = {"type": "array", "itemSchema": {"type": "integer"}}
    validate([1, 2], s)
    _bad([1, "x"], s)


def test_array_nested():
    s = {
        "type": "array",
        "items": {"type": "array", "items": {"type": "number"}},
    }
    validate([[1, 2], [3]], s)
    _bad([[1, "bad"]], s)


# ==============================================================
# object
# ==============================================================

def test_object_ok():
    validate({"a": 1}, {"type": "object"})


def test_object_non_dict_rejected():
    _bad([], {"type": "object"})
    _bad("str", {"type": "object"})


def test_object_properties_validated():
    s = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "age": {"type": "integer"},
        },
    }
    validate({"name": "x", "age": 30}, s)
    _bad({"name": "x", "age": "30"}, s)


def test_object_required_array_style():
    """required 是父对象数组 (JSON Schema 标准)."""
    s = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    }
    validate({"name": "x"}, s)
    _bad({}, s)


def test_object_required_field_style_v2_compat():
    """required: True 在字段上 (v2 / mod 模板风格)."""
    s = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "required": True},
        },
    }
    validate({"name": "x"}, s)
    _bad({}, s)


def test_object_required_both_styles_compat():
    """两种风格混用应该 OK."""
    s = {
        "type": "object",
        "properties": {
            "a": {"type": "string", "required": True},
            "b": {"type": "string"},
        },
        "required": ["b"],
    }
    validate({"a": "x", "b": "y"}, s)
    _bad({"a": "x"}, s)
    _bad({"b": "y"}, s)


def test_object_additional_properties_false():
    s = {
        "type": "object",
        "properties": {"x": {"type": "string"}},
        "additionalProperties": False,
    }
    validate({"x": "ok"}, s)
    _bad({"x": "ok", "y": "extra"}, s)


def test_object_additional_properties_default_true():
    """默认 additionalProperties=True."""
    s = {
        "type": "object",
        "properties": {"x": {"type": "string"}},
    }
    validate({"x": "ok", "y": "extra"}, s)


# ==============================================================
# 复合
# ==============================================================

def test_oneof_exactly_one():
    s = {"oneOf": [{"type": "string"}, {"type": "number"}]}
    validate("hello", s); validate(123, s)
    _bad(True, s)              # 都不匹配 (bool 非 number)
    _bad([1], s)


def test_anyof_at_least_one():
    s = {"anyOf": [{"type": "string"}, {"type": "number"}]}
    validate("x", s); validate(1, s)
    _bad([], s)


def test_allof_all():
    s = {"allOf": [
        {"type": "string"},
        {"type": "string", "minLength": 3},
    ]}
    validate("hello", s)
    _bad("x", s)               # 长度不够
    _bad(123, s)               # 类型不对


# ==============================================================
# asset_ref
# ==============================================================

def test_asset_ref_minimum_ok():
    s = {"type": "asset_ref"}
    validate({"sha256": "a" * 64}, s)


def test_asset_ref_full_ok():
    s = {"type": "asset_ref"}
    validate({
        "sha256": "f" * 64,
        "mime": "image/png",
        "size": 12345,
        "ext": ".png",
    }, s)


def test_asset_ref_missing_sha_rejected():
    s = {"type": "asset_ref"}
    _bad({}, s)
    _bad({"mime": "image/png"}, s)


def test_asset_ref_bad_sha256():
    s = {"type": "asset_ref"}
    _bad({"sha256": "too-short"}, s)
    _bad({"sha256": "x" * 64}, s)          # 非 hex
    _bad({"sha256": "A" * 64}, s)          # 大写 hex 不接受 (规范要小写)


def test_asset_ref_bad_size():
    s = {"type": "asset_ref"}
    base = {"sha256": "a" * 64}
    _bad({**base, "size": -1}, s)
    _bad({**base, "size": 3.14}, s)
    _bad({**base, "size": True}, s)        # bool 也不行


def test_asset_ref_bad_mime():
    s = {"type": "asset_ref"}
    _bad({"sha256": "a" * 64, "mime": 123}, s)


def test_asset_ref_non_dict_rejected():
    s = {"type": "asset_ref"}
    _bad("not-a-dict", s)
    _bad(["a", "b"], s)


# ==============================================================
# entity_ref
# ==============================================================

def test_entity_ref_ok():
    s = {"type": "entity_ref"}
    validate({"$ref": "obj/e1"}, s)


def test_entity_ref_to_restriction():
    s = {"type": "entity_ref", "to": "obj"}
    validate({"$ref": "obj/e1"}, s)
    _bad({"$ref": "other/e1"}, s)


def test_entity_ref_bad_format():
    s = {"type": "entity_ref"}
    _bad({"$ref": "no-slash"}, s)
    _bad({"$ref": ""}, s)
    _bad({}, s)
    _bad("not-dict", s)


# ==============================================================
# untyped
# ==============================================================

def test_untyped_accepts_anything():
    """空 schema 接受任意值."""
    validate("anything", {})
    validate(123, {})
    validate(None, {})
    validate({"a": 1}, {})
    validate([1, 2, 3], {})


def test_no_type_field_accepts_anything():
    """没 type 字段也接受任意."""
    validate("anything", {"description": "no type"})


# ==============================================================
# unknown type
# ==============================================================

def test_unknown_type_raises():
    _bad("x", {"type": "weird_type"})


def test_known_types_constant():
    expected = {
        "string", "number", "integer", "boolean",
        "array", "object", "null", "asset_ref", "entity_ref",
    }
    assert set(KNOWN_TYPES) == expected


# ==============================================================
# make_validator 工厂
# ==============================================================

def test_make_validator_returns_callable():
    v = make_validator({"type": "string"})
    assert callable(v)
    assert is_framework_validator(v) is True


def test_framework_validator_identity_is_exact_not_callable_shaped():
    custom = lambda data, schema, path: None
    assert callable(custom)
    assert is_framework_validator(custom) is False

    wrapper_type = type(make_validator({}))

    class Lookalike(wrapper_type):
        pass

    lookalike = Lookalike({})
    assert callable(lookalike)
    assert is_framework_validator(lookalike) is False


def test_framework_validator_identity_can_bind_expected_schema_authority():
    flat = {"name": {"type": "string", "required": True}}
    canonical = {
        "type": "object",
        "properties": {"name": {"type": "string", "required": True}},
    }
    validator = make_validator(flat)

    assert is_framework_validator(validator, flat) is True
    assert is_framework_validator(validator, canonical) is True
    assert is_framework_validator(
        validator, {"name": {"type": "integer"}},
    ) is False
    assert is_framework_validator(validator, {"name": "not-a-schema"}) is False


def test_framework_validator_authority_cannot_be_cleared_or_reassigned():
    schema = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    }
    validator = make_validator(schema)

    with pytest.raises(AttributeError):
        validator._entity_schema.clear()
    with pytest.raises(AttributeError):
        validator._entity_schema = {}
    with pytest.raises(AttributeError):
        validator._FrameworkValidator__authority_bytes = b"{}"

    with pytest.raises(ZeError):
        validator({}, {}, "$entry")
    assert is_framework_validator(validator, schema)


def test_framework_validator_expected_schema_rejects_dict_subclass_without_eq_hooks():
    hooks = []

    class DictSubclass(dict):
        def __eq__(self, other):
            hooks.append("eq")
            raise AssertionError("untrusted schema equality must not run")

        def items(self):
            hooks.append("items")
            raise AssertionError("untrusted schema iteration must not run")

    validator = make_validator({"name": {"type": "string"}})
    assert is_framework_validator(
        validator, DictSubclass({"name": {"type": "string"}})
    ) is False
    assert hooks == []


def test_make_validator_runs_check():
    v = make_validator({"type": "object",
                         "properties": {"name": {"type": "string"}},
                         "required": ["name"]})
    v({"name": "x"}, {}, "$obj")            # ok
    with pytest.raises(ZeError):
        v({}, {}, "$obj")                    # 缺 name


def test_make_validator_fallback_to_entity_schema():
    """make_validator 如果 caller 传空 schema, 用初始化时的 entity_schema."""
    entity_schema = {"type": "object", "required": ["name"]}
    v = make_validator(entity_schema)
    with pytest.raises(ZeError):
        v({}, {}, "$obj")                    # schema 空 → fallback to entity_schema


def test_make_validator_detaches_nested_schema_aliases():
    entity_schema = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    }
    validator = make_validator(entity_schema)
    entity_schema["required"].clear()
    entity_schema["properties"]["name"]["type"] = "integer"
    with pytest.raises(ZeError):
        validator({}, {}, "$obj")
    validator({"name": "still-a-string"}, {}, "$obj")


def test_make_validator_caller_schema_cannot_override_held_authority():
    """协议中的 schema 参数不能把 framework wrapper 降级成宽松规则。"""
    entity_schema = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    }
    v = make_validator(entity_schema)
    with pytest.raises(ZeError):
        v({}, {"type": "object"}, "$")


def test_make_validator_normalizes_legacy_flat_field_map_once():
    """legacy shorthand 只在工厂边界转为 canonical object/properties。"""
    legacy = {
        "title": {"type": "string", "required": True},
        "count": {"type": "integer"},
    }
    v = make_validator(legacy)

    v({"title": "ok", "count": 1}, legacy, "$entry")
    with pytest.raises(ZeError):
        v({"count": 1}, legacy, "$entry")
    with pytest.raises(ZeError):
        v({"title": "ok", "count": "bad"}, legacy, "$entry")


def test_legacy_fields_named_like_root_keywords_are_not_misclassified():
    legacy = {
        "required": {"type": "string", "required": True},
        "oneOf": {"type": "integer"},
        "type": {"type": "boolean"},
        "properties": {"type": "string"},
    }
    validator = make_validator(legacy)
    validator({
        "required": "yes",
        "oneOf": 1,
        "type": True,
        "properties": "business-value",
    }, {}, "$entry")

    with pytest.raises(ZeError):
        validator({
            "required": "yes",
            "oneOf": "bad",
            "type": True,
            "properties": "business-value",
        }, {}, "$entry")


@pytest.mark.parametrize("keyword", ["properties", "additionalProperties"])
def test_root_like_business_field_collision_remains_legacy_flat(keyword):
    legacy = {
        keyword: {},
        "name": {"type": "string", "required": True},
    }
    canonical = canonicalize_entity_schema(legacy)
    assert canonical == {
        "type": "object",
        "properties": legacy,
    }
    validator = make_validator(legacy)
    validator({keyword: {"anything": True}, "name": "ok"}, {}, "$entry")
    with pytest.raises(ZeError):
        validator({keyword: {}, "name": 42}, {}, "$entry")


def test_make_validator_normalizes_object_keywords_without_explicit_type():
    v = make_validator({
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
        "additionalProperties": False,
    })
    v({"name": "ok"}, {}, "$entry")
    with pytest.raises(ZeError):
        v({"name": "ok", "extra": True}, {}, "$entry")


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "properties": []},
        {"type": "object", "properties": [1]},
        {"type": "object", "required": "name"},
        {"type": "object", "required": ["name", "name"]},
        {"type": "object", "additionalProperties": {"type": "string"}},
        {"type": "number", "multipleOf": 0},
        {"type": "array", "items": []},
        {"type": "array", "uniqueItems": 1},
        {"oneOf": []},
        {"oneOf": [{"type": "string"}], "anyOf": [{"type": "number"}]},
        {"type": "string", "pattern": "["},
        {"type": "string", "unknownConstraint": 1},
        {"type": "string", "minLength": -1},
    ],
)
def test_make_validator_rejects_malformed_meta_schema_at_compile_time(schema):
    with pytest.raises(ZeError) as exc:
        make_validator(schema)
    assert exc.value.code == "validation_failed"
    assert exc.value.context["reason"] == "validator_schema_invalid"


def test_compiled_object_property_count_constraints_are_not_silently_ignored():
    validator = make_validator({
        "type": "object",
        "minProperties": 1,
        "maxProperties": 2,
    })
    with pytest.raises(ZeError):
        validator({}, {}, "$entry")
    validator({"a": 1}, {}, "$entry")
    with pytest.raises(ZeError):
        validator({"a": 1, "b": 2, "c": 3}, {}, "$entry")


def test_meta_schema_accepts_declared_ui_annotations_and_x_extensions():
    validator = make_validator({
        "type": "object",
        "label": "Card",
        "description": "Editor card",
        "default": {},
        "x-ze-archetype": "card@1",
        "properties": {
            "title": {
                "type": "string",
                "label": "Title",
                "accept": "text/plain",
                "units": "characters",
                "x-ze-ref": "asset",
            }
        },
    })
    validator({"title": "ok"}, {}, "$entry")


def test_meta_schema_accepts_x_ze_enum_labels_flat_and_bilingual():
    """x-ze-enum-labels 双形态(扁平串 / 双语 dict)通过 schema 校验且原样保留。"""
    schema = {
        "type": "object",
        "properties": {
            "ratio": {
                "type": "string",
                "enum": ["16:9", "9:16"],
                "x-ze-enum-labels": {
                    "16:9": "横屏 16:9",                       # flat
                    "9:16": {"zh": "竖屏 9:16", "en": "Portrait 9:16"},  # bilingual
                },
            }
        },
    }
    canonical = canonicalize_entity_schema(schema)
    # 键与映射未被剥离,前端可消费。
    assert canonical["properties"]["ratio"]["x-ze-enum-labels"]["16:9"] == "横屏 16:9"
    # 校验器仍只按 enum 约束校验数据值(标签映射不干扰数据校验)。
    make_validator(schema)({"ratio": "16:9"}, {}, "$entry")


@pytest.mark.parametrize(
    "labels",
    [
        "not-a-dict",
        [],
        {},                              # 空映射
        {"a": None},                     # 值非串非 dict
        {"a": 1},                        # 值非串非 dict
        {"a": ""},                       # 空串
        {"a": {}},                       # 空语言表
        {"a": {"zh": ""}},               # 语言文本空
        {"a": {"zh": 1}},                # 语言文本非串
    ],
)
def test_meta_schema_rejects_malformed_x_ze_enum_labels(labels):
    schema = {
        "type": "object",
        "properties": {"ratio": {"type": "string", "x-ze-enum-labels": labels}},
    }
    with pytest.raises(ZeError) as exc:
        make_validator(schema)
    assert exc.value.code == "validation_failed"
    assert exc.value.context["reason"] == "validator_schema_invalid"


def test_other_x_keys_still_pass_when_enum_labels_gate_added():
    """回归:新增 enum-labels 门禁不影响其它 x- 键的通用放行(消融基准)。"""
    make_validator({
        "type": "object",
        "properties": {"m": {"type": "string", "x-ze-anything": [1, {"k": "v"}]}},
    })({"m": "ok"}, {}, "$entry")


def test_meta_schema_accepts_numeric_editor_step_without_widening_unknown_keys():
    schema = {
        "type": "object",
        "properties": {
            "rotation": {"type": "number", "minimum": -180, "step": 1},
            "order": {"type": "integer", "step": 0.5},
        },
    }
    canonical = canonicalize_entity_schema(schema)
    assert canonical["properties"]["rotation"]["step"] == 1
    make_validator(schema)({"rotation": 15, "order": 2}, {}, "$entry")

    for invalid in (
        {"type": "string", "step": 1},
        {"type": "number", "step": 0},
        {"type": "number", "step": True},
        {"type": "number", "step": float("inf")},
    ):
        with pytest.raises(ZeError) as exc:
            make_validator(invalid)
        assert exc.value.code == "validation_failed"


# ==============================================================
# 复杂场景 (实际 entity schema)
# ==============================================================

def test_realistic_entity_schema():
    """真实 entity 风格的 schema 完整校验."""
    s = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "required": True,
                      "minLength": 1, "maxLength": 200},
            "tags": {"type": "array", "items": {"type": "string"}},
            "image": {"type": "asset_ref"},
            "parent": {"type": "entity_ref", "to": "category"},
            "age": {"type": "integer", "minimum": 0},
            "deleted": {"type": "boolean"},
        },
    }
    validate({
        "title": "Hello",
        "tags": ["a", "b"],
        "image": {"sha256": "a" * 64, "mime": "image/png"},
        "parent": {"$ref": "category/c1"},
        "age": 30,
        "deleted": False,
    }, s)


def test_realistic_entity_schema_violations():
    s = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "required": True, "minLength": 1},
            "image": {"type": "asset_ref"},
        },
    }
    # title 太短
    _bad({"title": "", "image": {"sha256": "a" * 64}}, s)
    # image sha 不对
    _bad({"title": "OK", "image": {"sha256": "bad"}}, s)
    # 缺 title
    _bad({"image": {"sha256": "a" * 64}}, s)


# ==============================================================
# path 错误信息
# ==============================================================

def test_path_in_error_message():
    """嵌套校验错误信息应含完整 path."""
    s = {
        "type": "object",
        "properties": {
            "items": {"type": "array", "items": {"type": "integer"}},
        },
    }
    with pytest.raises(ZeError) as exc:
        validate({"items": [1, "bad", 3]}, s, path="$obj")
    assert "$obj.items[1]" in str(exc.value.args[0])


# ==============================================================
# schema doctor (R-C18): describe_schema / minimal_example / diagnose
# ==============================================================

# 语料: 覆盖各类型 + 复合 + ref + 约束 + 嵌套(防漂移 property gate 用)
_DOCTOR_FIXTURES = [
    {"type": "string"},
    {"type": "string", "minLength": 2, "maxLength": 5},
    {"type": "string", "enum": ["a", "b"]},
    {"type": "string", "format": "email"},
    {"type": "integer", "minimum": 1, "maximum": 4096},
    {"type": "number", "exclusiveMinimum": 0},
    {"type": "boolean"},
    {"type": "null"},
    {"type": "array", "items": {"type": "integer"}, "minItems": 1},
    {"type": "asset_ref"},
    {"type": "entity_ref", "to": "shot"},
    {"oneOf": [{"type": "string"}, {"type": "null"}]},
    {"type": "object", "properties": {
        "name": {"type": "string", "required": True},
        "tags": {"type": "array", "items": {"type": "string"}},
        "kind": {"type": "string", "enum": ["x", "y"]},
    }, "required": ["kind"]},
]


def test_describe_schema_shape_and_annotations():
    s = {"type": "object", "label": "生图任务", "properties": {
        "prompt": {"type": "string", "label": "提示词"},
        "disposition": {"type": "string", "enum": ["a", "b"]},
        "genStatus": {"type": "string", "required": True},
    }}
    d = describe_schema(s)
    assert d["type"] == "object" and d["label"] == "生图任务"
    assert set(d["fields"]) == {"prompt", "disposition", "genStatus"}
    assert d["fields"]["disposition"]["enum"] == ["a", "b"]
    assert d["fields"]["genStatus"]["required"] is True
    assert d["fields"]["prompt"]["label"] == "提示词"


def test_minimal_example_is_valid_for_every_fixture():
    """样例合法性 property: 每个不标 _incomplete 的样例必须过 validate。"""
    for s in _DOCTOR_FIXTURES:
        node = describe_schema(s)
        if node.get("_incomplete"):
            continue
        validate(minimal_example(s), s)   # 不抛即通过


def test_minimal_example_incomplete_is_honest():
    """无法保证合法(enum 含空串撞 minLength) → 必须标 _incomplete, 绝不假装合法。"""
    trap = {"type": "string", "enum": ["", "abc"], "minLength": 1}
    assert describe_schema(trap).get("_incomplete") is True


def test_diagnose_collects_all_violations():
    """diagnose 汇总全部不符, 而 validate 只 raise 第一个。"""
    s = {"type": "object", "properties": {
        "prompt": {"type": "string"},
        "disposition": {"type": "string", "enum": ["unreviewed", "ready"]},
        "order": {"type": "number"},
        "genStatus": {"type": "string", "required": True},
    }}
    bad = {"prompt": 123, "disposition": "done", "order": "x"}
    findings = diagnose(bad, s)
    paths = {f["path"] for f in findings}
    assert "$.prompt" in paths and "$.disposition" in paths and "$.order" in paths
    assert any("genStatus" in f["reason"] for f in findings)  # 缺必填也报
    assert len(findings) >= 4


def test_diagnose_superset_of_validate_first_error():
    """防漂移 gate ①: diagnose 的路径集合必须 ⊇ validate 首个 raise 的路径。"""
    cases = [
        ({"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]}, {}),
        ({"type": "object", "properties": {"a": {"type": "string"}}}, {"a": 5}),
        ({"type": "array", "items": {"type": "integer"}}, [1, "b", 3]),
    ]
    for s, v in cases:
        try:
            validate(v, s)
            first_path = None
        except ZeError as e:
            first_path = (e.context or {}).get("path")
        diag_paths = {f["path"] for f in diagnose(v, s)}
        if first_path is not None:
            assert first_path in diag_paths, f"validate path {first_path} missing from diagnose {diag_paths}"


def test_diagnose_findings_are_all_real_violations():
    """防漂移 gate ②: 每条 finding 的子路径单独 validate 必 raise(无误报)。"""
    s = {"type": "object", "properties": {
        "a": {"type": "integer"}, "b": {"type": "string", "enum": ["x"]},
    }}
    v = {"a": "no", "b": "wrong"}
    for f in diagnose(v, s):
        # 取该 path 对应的子 schema + 子 value, 重新 validate 必失败
        field = f["path"].split(".")[-1]
        if field in s["properties"]:
            with pytest.raises(ZeError):
                validate(v[field], {k: val for k, val in s["properties"][field].items()
                                    if k != "required"})


def test_diagnose_never_raises_and_caps_findings():
    """diagnose 永不 raise; 大数组不爆炸(条数上限)。"""
    s = {"type": "array", "items": {"type": "integer"}}
    findings = diagnose(["x"] * 5000, s)   # 5000 个类型错元素
    assert 0 < len(findings) <= 200


def test_diagnose_empty_when_valid():
    s = {"type": "object", "properties": {"a": {"type": "string", "required": True}}}
    assert diagnose({"a": "ok"}, s) == []


def test_diagnose_does_not_mutate_inputs():
    s = {"type": "object", "properties": {"a": {"type": "integer"}}}
    v = {"a": "bad"}
    import copy
    s_before, v_before = copy.deepcopy(s), copy.deepcopy(v)
    diagnose(v, s)
    describe_schema(s)
    minimal_example(s)
    assert s == s_before and v == v_before
