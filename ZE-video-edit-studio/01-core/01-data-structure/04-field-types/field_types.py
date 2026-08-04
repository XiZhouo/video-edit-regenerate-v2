"""Field Types — JSON Schema 驼峰风格校验器.

完整 SPEC 见同目录 SPEC.md.

已实装 (无跨叶子依赖):
  - validate(value, schema, path) — 跟 v3 Validator 协议签名一致
  - 6 基础类型: string / number / integer / boolean / array / object
  - null 类型 (配合 oneOf 做 nullable)
  - asset_ref / entity_ref (ZE 专有)
  - 复合: oneOf / anyOf / allOf
  - required 双风格兼容 (父对象 required 数组 / 字段上 required: True)
  - make_validator(schema) — 工厂, 返回 Validator callable
"""

from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path
from typing import Any, Callable

# 共享 ZeError: 从兄弟目录 02-entity import
_ENTITY_DIR = Path(__file__).resolve().parent.parent / "02-entity"
if str(_ENTITY_DIR) not in sys.path:
    sys.path.insert(0, str(_ENTITY_DIR))

from entity import ZeError, _clone_json_value  # noqa: E402


# Validator 协议类型 (跟 entity.py 定义一致)
Validator = Callable[[dict[str, Any], dict[str, Any], str], None]

# 支持的 type 字符串
KNOWN_TYPES = (
    "string", "number", "integer", "boolean",
    "array", "object", "null",
    "asset_ref", "entity_ref",
)


# ==============================================================
# validate (入口)
# ==============================================================

def validate(value: Any, schema: dict[str, Any], path: str = "$") -> None:
    """按 schema 校验 value. 不通过抛 ZeError('schema_violation').

    跟 v3 entity Validator 协议签名一致: (data, schema, path) -> None.
    """
    if not schema:
        # untyped: 任意值
        return

    # 复合 (优先于 type)
    if "oneOf" in schema:
        _validate_composite(value, schema["oneOf"], path, mode="oneOf")
        return
    if "anyOf" in schema:
        _validate_composite(value, schema["anyOf"], path, mode="anyOf")
        return
    if "allOf" in schema:
        _validate_composite(value, schema["allOf"], path, mode="allOf")
        return

    t = schema.get("type")
    if t is None:
        return  # untyped: 任意

    if t == "string":
        _validate_string(value, schema, path)
    elif t == "integer":
        _validate_number(value, schema, path, integer=True)
    elif t == "number":
        _validate_number(value, schema, path, integer=False)
    elif t == "boolean":
        _validate_boolean(value, path)
    elif t == "null":
        _validate_null(value, path)
    elif t == "array":
        _validate_array(value, schema, path)
    elif t == "object":
        _validate_object(value, schema, path)
    elif t == "asset_ref":
        _validate_asset_ref(value, path)
    elif t == "entity_ref":
        _validate_entity_ref(value, schema, path)
    else:
        raise ZeError(
            "schema_violation",
            f"{path}: unknown schema type {t!r}",
            context={"path": path, "type": t},
        )


def _fail(path: str, reason: str, value: Any) -> None:
    raise ZeError(
        "schema_violation",
        f"{path}: {reason} (got {value!r})",
        context={"path": path, "reason": reason, "value": value},
    )


# ==============================================================
# 基础类型
# ==============================================================

def _validate_string(v: Any, s: dict[str, Any], p: str) -> None:
    if not isinstance(v, str):
        _fail(p, "expected string", v)
    if (m := s.get("minLength")) is not None and len(v) < m:
        _fail(p, f"minLength {m}", v)
    if (m := s.get("maxLength")) is not None and len(v) > m:
        _fail(p, f"maxLength {m}", v)
    if (pat := s.get("pattern")) and not re.fullmatch(pat, v):
        _fail(p, f"pattern {pat!r}", v)
    if (enum := s.get("enum")) and v not in enum:
        _fail(p, f"enum {enum!r}", v)
    if (fmt := s.get("format")):
        _validate_format(v, fmt, p)


_FORMAT_PATTERNS: dict[str, str] = {
    "email": r"[^@\s]+@[^@\s]+\.[^@\s]+",
    "uri": r"https?://[^\s]+",
    "uuid": r"[0-9a-fA-F]{32}|[0-9a-fA-F-]{36}",
    "iso_datetime": r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})?",
    # color:#rrggbb 十六进制色(FormEditor format:'color' → 原生取色器,emit #rrggbb)
    "color": r"#[0-9a-fA-F]{6}",
}


def _validate_format(v: str, fmt: str, p: str) -> None:
    pat = _FORMAT_PATTERNS.get(fmt)
    if pat is None:
        _fail(p, f"unknown format {fmt!r}", v)
    if not re.fullmatch(pat, v):
        _fail(p, f"format {fmt}", v)


def _validate_number(v: Any, s: dict[str, Any], p: str, *, integer: bool) -> None:
    # bool 不视为 number (Python bool 是 int 子类的坑)
    if isinstance(v, bool):
        _fail(p, "expected number, got boolean", v)
    if integer:
        if not isinstance(v, int):
            _fail(p, "expected integer", v)
    else:
        if not isinstance(v, (int, float)):
            _fail(p, "expected number", v)
    if (m := s.get("minimum")) is not None and v < m:
        _fail(p, f"minimum {m}", v)
    if (m := s.get("maximum")) is not None and v > m:
        _fail(p, f"maximum {m}", v)
    if (m := s.get("exclusiveMinimum")) is not None and v <= m:
        _fail(p, f"exclusiveMinimum {m}", v)
    if (m := s.get("exclusiveMaximum")) is not None and v >= m:
        _fail(p, f"exclusiveMaximum {m}", v)
    if (m := s.get("multipleOf")) is not None and v % m != 0:
        _fail(p, f"multipleOf {m}", v)


def _validate_boolean(v: Any, p: str) -> None:
    if not isinstance(v, bool):
        _fail(p, "expected boolean", v)


def _validate_null(v: Any, p: str) -> None:
    if v is not None:
        _fail(p, "expected null", v)


def _validate_array(v: Any, s: dict[str, Any], p: str) -> None:
    if not isinstance(v, list):
        _fail(p, "expected array", v)
    if (m := s.get("minItems")) is not None and len(v) < m:
        _fail(p, f"minItems {m}", v)
    if (m := s.get("maxItems")) is not None and len(v) > m:
        _fail(p, f"maxItems {m}", v)
    if s.get("uniqueItems"):
        seen = []
        for item in v:
            if item in seen:
                _fail(p, "uniqueItems", v)
            seen.append(item)
    # items / itemSchema (alias for v2 兼容)
    item_schema = s.get("items") or s.get("itemSchema")
    if item_schema:
        for i, item in enumerate(v):
            validate(item, item_schema, f"{p}[{i}]")


def _validate_object(v: Any, s: dict[str, Any], p: str) -> None:
    if not isinstance(v, dict):
        _fail(p, "expected object", v)
    if (minimum := s.get("minProperties")) is not None and len(v) < minimum:
        _fail(p, f"minProperties {minimum}", v)
    if (maximum := s.get("maxProperties")) is not None and len(v) > maximum:
        _fail(p, f"maxProperties {maximum}", v)

    properties = s.get("properties") or {}
    # required 双风格融合(单一真源:validate / describe_schema / diagnose 共用 _required_fields)
    required = _required_fields(s)

    # 必填校验
    for key in required:
        if key not in v:
            _fail(p, f"missing required field {key!r}", v)

    # 子字段递归校验
    for key, sub in properties.items():
        if key in v:
            # 校验前剥掉字段级 required (避免 sub-schema 把 required: True 当 v 校验)
            sub_clean = {k: val for k, val in sub.items() if k != "required"}
            validate(v[key], sub_clean, f"{p}.{key}")

    # additionalProperties
    additional = s.get("additionalProperties", True)
    if additional is False:
        extra = set(v.keys()) - set(properties.keys())
        if extra:
            _fail(p, f"additionalProperties not allowed: {sorted(extra)}", v)


# ==============================================================
# 复合
# ==============================================================

def _validate_composite(v: Any, alts: list[dict[str, Any]], p: str, *, mode: str) -> None:
    matches = 0
    last_err: ZeError | None = None
    for i, sub in enumerate(alts):
        try:
            validate(v, sub, f"{p}({mode}[{i}])")
            matches += 1
        except ZeError as e:
            last_err = e
    if mode == "oneOf" and matches != 1:
        _fail(p, f"oneOf: matched {matches}/{len(alts)}", v)
    if mode == "anyOf" and matches < 1:
        _fail(p, f"anyOf: matched 0/{len(alts)}", v)
    if mode == "allOf" and matches != len(alts):
        last_msg = str(last_err.args[0]) if last_err and last_err.args else ""
        _fail(p, f"allOf: matched {matches}/{len(alts)} ({last_msg})", v)


# ==============================================================
# ZE 专有: asset_ref / entity_ref
# ==============================================================

_SHA256_HEX_RE = re.compile(r"[0-9a-f]{64}")


def _validate_asset_ref(v: Any, p: str) -> None:
    """校验 {sha256, mime, size, ext} 形态; 不查 blob 真存在."""
    if not isinstance(v, dict):
        _fail(p, "expected asset_ref dict", v)

    sha = v.get("sha256")
    if not isinstance(sha, str):
        _fail(p, "asset_ref.sha256 missing or not string", v)
    if not _SHA256_HEX_RE.fullmatch(sha):
        _fail(p, "asset_ref.sha256 must be 64 hex chars", v)

    # 可选字段类型校验
    if "mime" in v and not isinstance(v["mime"], str):
        _fail(p, "asset_ref.mime must be string", v)
    if "size" in v:
        sz = v["size"]
        if not isinstance(sz, int) or isinstance(sz, bool) or sz < 0:
            _fail(p, "asset_ref.size must be non-negative integer", v)
    if "ext" in v and not isinstance(v["ext"], str):
        _fail(p, "asset_ref.ext must be string", v)


_ENTITY_REF_RE = re.compile(r"[^/]+/[^/]+")


def _validate_entity_ref(v: Any, s: dict[str, Any], p: str) -> None:
    """校验 {"$ref": "type/id"} 形态; 不查 entity 真存在."""
    if not isinstance(v, dict):
        _fail(p, "expected entity_ref dict", v)
    ref = v.get("$ref")
    if not isinstance(ref, str):
        _fail(p, "entity_ref.$ref missing or not string", v)
    if not _ENTITY_REF_RE.fullmatch(ref):
        _fail(p, "entity_ref.$ref must be 'type/id' format", v)

    # 可选 to 限制
    expected_type = s.get("to")
    if expected_type:
        actual_type = ref.split("/", 1)[0]
        if actual_type != expected_type:
            _fail(p, f"entity_ref.$ref expected type={expected_type!r}, got {actual_type!r}", v)


# ==============================================================
# sanitize (入库前消毒;与 validate 互补)
# ==============================================================

# 控制字符(C0/C1 中的不可见控制 + DEL),保留 \t(09)\n(0a)\r(0d)。
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def sanitize(value: Any, schema: dict[str, Any] | None = None, path: str = "$") -> Any:
    """入库前消毒,**返回新值不改原值**(immutable;coding-style)。

    与 validate 互补:validate 拒非法(raise),sanitize 清可疑(返回干净值)。
    - string:剔除控制字符(保留 \\t\\n\\r);schema 有 maxLength 则截断。
    - object/array:按 schema 递归(无 schema 也照结构走)。其余类型原样返回。
    建议入口:entity.add/update 在 validate 前先 sanitize(防注入/脏控制字符落库)。
    """
    if isinstance(value, str):
        out = _CONTROL_CHARS_RE.sub("", value)
        if schema:
            ml = schema.get("maxLength")
            if isinstance(ml, int) and ml >= 0 and len(out) > ml:
                out = out[:ml]
        return out
    if isinstance(value, dict):
        props = schema.get("properties", {}) if isinstance(schema, dict) else {}
        props = props if isinstance(props, dict) else {}
        return {k: sanitize(v, props.get(k), f"{path}.{k}") for k, v in value.items()}
    if isinstance(value, list):
        items = schema.get("items") if isinstance(schema, dict) else None
        items = items if isinstance(items, dict) else None
        return [sanitize(v, items, f"{path}[{i}]") for i, v in enumerate(value)]
    return value


# ==============================================================
# make_validator (工厂)
# ==============================================================

_ROOT_COMPOSITE_KEYWORDS = ("oneOf", "anyOf", "allOf")
_ROOT_STRING_ANNOTATIONS = frozenset(("$id", "$schema", "title", "description"))
_ROOT_BOOL_ANNOTATIONS = frozenset(("readOnly", "writeOnly", "deprecated"))
_ROOT_LIST_ANNOTATIONS = frozenset(("examples",))
_ROOT_DICT_KEYWORDS = frozenset(
    ("$defs", "definitions")
)
_ROOT_INT_KEYWORDS = frozenset(("minProperties", "maxProperties"))
_SCHEMA_STRING_ANNOTATIONS = frozenset(
    (
        "$id", "$schema", "title", "description", "label", "labelKey",
        "placeholder", "help", "accept", "units", "widget",
    )
)
_SCHEMA_BOOL_ANNOTATIONS = frozenset(
    ("readOnly", "writeOnly", "deprecated", "hidden")
)
_SCHEMA_FREE_ANNOTATIONS = frozenset(("default",))
_SCHEMA_LIST_ANNOTATIONS = frozenset(("examples",))
_SCHEMA_DICT_ANNOTATIONS = frozenset(("ui", "$defs", "definitions"))
_SCHEMA_NUMBER_ANNOTATIONS = frozenset(("step",))
_STRING_KEYWORDS = frozenset(("minLength", "maxLength", "pattern", "enum", "format"))
_NUMBER_KEYWORDS = frozenset(
    ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf")
)
_ARRAY_KEYWORDS = frozenset(
    ("minItems", "maxItems", "uniqueItems", "items", "itemSchema")
)
_OBJECT_KEYWORDS = frozenset(
    (
        "properties", "required", "additionalProperties", "minProperties",
        "maxProperties",
    )
)
_KNOWN_SCHEMA_KEYS = (
    frozenset(("type", "oneOf", "anyOf", "allOf", "to"))
    | _STRING_KEYWORDS
    | _NUMBER_KEYWORDS
    | _ARRAY_KEYWORDS
    | _OBJECT_KEYWORDS
    | _SCHEMA_STRING_ANNOTATIONS
    | _SCHEMA_BOOL_ANNOTATIONS
    | _SCHEMA_FREE_ANNOTATIONS
    | _SCHEMA_LIST_ANNOTATIONS
    | _SCHEMA_DICT_ANNOTATIONS
    | _SCHEMA_NUMBER_ANNOTATIONS
)


def _schema_invalid() -> None:
    raise ZeError(
        "validation_failed",
        "framework validator schema is malformed",
        context={"reason": "validator_schema_invalid"},
    )


# mod 声明枚举选项【人话名】的字段 schema 键(x- 前缀,过通用扩展通路;此处只加形状门禁)。
# 前端 enumOptionLabel 消费它:值形态 {enumValue: 人话名} 或双语 {enumValue: {zh, en}}。
X_ENUM_LABELS_KEY = "x-ze-enum-labels"


def _validate_x_enum_labels(value: Any) -> None:
    """形状门禁 x-ze-enum-labels: {enumValue(str): str | {locale(str): str}}。

    非法形状(非 dict / 空 / 值非串非 dict / 空串 / 语言表非串)→ 拒。
    其余 x- 键仍走通用放行(不受此约束)。
    """
    if type(value) is not dict or not value:
        _schema_invalid()
    for enum_value, label in value.items():
        if type(enum_value) is not str:
            _schema_invalid()
        if type(label) is str:
            if not label:
                _schema_invalid()
        elif type(label) is dict:
            if not label:
                _schema_invalid()
            for locale_key, text in label.items():
                if type(locale_key) is not str or type(text) is not str or not text:
                    _schema_invalid()
        else:
            _schema_invalid()


def _is_exact_nonnegative_int(value: Any) -> bool:
    return type(value) is int and value >= 0


def _is_exact_finite_number(value: Any) -> bool:
    return (
        (type(value) is int)
        or (type(value) is float and math.isfinite(value))
    )


def _compile_schema_node(schema: Any, *, field_context: bool) -> dict[str, Any]:
    """Recursively compile one exact schema node or fail before runtime use."""
    if type(schema) is not dict:
        _schema_invalid()
    out = dict(schema)
    for key in out:
        if type(key) is not str:
            _schema_invalid()
        if key not in _KNOWN_SCHEMA_KEYS and not key.startswith("x-"):
            _schema_invalid()

    # 通用 x- 键放行,但对 x-ze-enum-labels 追加形状门禁(非法映射编译期即拒)。
    if X_ENUM_LABELS_KEY in out:
        _validate_x_enum_labels(out[X_ENUM_LABELS_KEY])

    composite_keys = [
        key for key in _ROOT_COMPOSITE_KEYWORDS if key in out
    ]
    if len(composite_keys) > 1:
        _schema_invalid()
    if composite_keys:
        composite_key = composite_keys[0]
        alternatives = out[composite_key]
        if type(alternatives) is not list or not alternatives or "type" in out:
            _schema_invalid()
        structural_siblings = (
            set(out)
            - {composite_key}
            - set(_SCHEMA_STRING_ANNOTATIONS)
            - set(_SCHEMA_BOOL_ANNOTATIONS)
            - set(_SCHEMA_FREE_ANNOTATIONS)
            - set(_SCHEMA_LIST_ANNOTATIONS)
            - set(_SCHEMA_DICT_ANNOTATIONS)
        )
        if any(not key.startswith("x-") for key in structural_siblings):
            _schema_invalid()
        out[composite_key] = [
            _compile_schema_node(item, field_context=False)
            for item in alternatives
        ]
    else:
        schema_type = out.get("type")
        if schema_type is not None and (
            type(schema_type) is not str or schema_type not in KNOWN_TYPES
        ):
            _schema_invalid()

        if "properties" in out:
            properties = out["properties"]
            if type(properties) is not dict:
                _schema_invalid()
            out["properties"] = {
                key: _compile_schema_node(item, field_context=True)
                for key, item in properties.items()
            }
            if schema_type is None:
                schema_type = "object"
                out["type"] = "object"
            elif schema_type != "object":
                _schema_invalid()

        if "required" in out:
            required = out["required"]
            if schema_type == "object" or "properties" in out:
                if (
                    type(required) is not list
                    or any(type(item) is not str or not item for item in required)
                    or len(set(required)) != len(required)
                ):
                    _schema_invalid()
            elif not field_context or type(required) is not bool:
                _schema_invalid()

        if "additionalProperties" in out and type(out["additionalProperties"]) is not bool:
            # Schema-valued additionalProperties is not implemented by validate.
            _schema_invalid()

        for pair in (("minLength", "maxLength"), ("minItems", "maxItems"),
                     ("minProperties", "maxProperties")):
            for key in pair:
                if key in out and not _is_exact_nonnegative_int(out[key]):
                    _schema_invalid()
            if pair[0] in out and pair[1] in out and out[pair[0]] > out[pair[1]]:
                _schema_invalid()

        for key in _NUMBER_KEYWORDS:
            if key in out and not _is_exact_finite_number(out[key]):
                _schema_invalid()
        if "multipleOf" in out and out["multipleOf"] <= 0:
            _schema_invalid()
        if "uniqueItems" in out and type(out["uniqueItems"]) is not bool:
            _schema_invalid()
        if "items" in out and "itemSchema" in out:
            _schema_invalid()
        for key in ("items", "itemSchema"):
            if key in out:
                out[key] = _compile_schema_node(out[key], field_context=False)
        if "pattern" in out:
            if type(out["pattern"]) is not str:
                _schema_invalid()
            pattern_invalid = False
            try:
                re.compile(out["pattern"])
            except re.error:
                pattern_invalid = True
            if pattern_invalid:
                _schema_invalid()
        if "format" in out and (
            type(out["format"]) is not str
            or out["format"] not in _FORMAT_PATTERNS
        ):
            _schema_invalid()
        if "enum" in out and (
            type(out["enum"]) is not list or not out["enum"]
        ):
            _schema_invalid()
        if "to" in out and (
            type(out["to"]) is not str or not out["to"]
        ):
            _schema_invalid()

        if any(key in out for key in _STRING_KEYWORDS) and schema_type != "string":
            _schema_invalid()
        if any(key in out for key in _NUMBER_KEYWORDS) and schema_type not in (
            "number", "integer"
        ):
            _schema_invalid()
        if any(key in out for key in _ARRAY_KEYWORDS) and schema_type != "array":
            _schema_invalid()
        object_constraints = set(_OBJECT_KEYWORDS) - {"required"}
        if any(key in out for key in object_constraints) and schema_type != "object":
            _schema_invalid()
        if "required" in out and type(out["required"]) is list and schema_type != "object":
            _schema_invalid()
        if "to" in out and schema_type != "entity_ref":
            _schema_invalid()
        if "step" in out and schema_type not in ("number", "integer"):
            _schema_invalid()

    for key in _SCHEMA_STRING_ANNOTATIONS:
        if key in out and type(out[key]) is not str:
            _schema_invalid()
    for key in _SCHEMA_BOOL_ANNOTATIONS:
        if key in out and type(out[key]) is not bool:
            _schema_invalid()
    for key in _SCHEMA_LIST_ANNOTATIONS:
        if key in out and type(out[key]) is not list:
            _schema_invalid()
    for key in _SCHEMA_DICT_ANNOTATIONS:
        if key in out and type(out[key]) is not dict:
            _schema_invalid()
    for key in _SCHEMA_NUMBER_ANNOTATIONS:
        if (
            key in out
            and (
                not _is_exact_finite_number(out[key])
                or out[key] <= 0
            )
        ):
            _schema_invalid()
    return out


def _looks_like_properties_keyword(value: Any) -> bool:
    """Disambiguate root ``properties`` from a legacy field of that name."""
    return type(value) is dict and all(
        type(field_name) is str and type(field_schema) is dict
        for field_name, field_schema in value.items()
    )


def _is_historical_root_member(key: str, value: Any) -> bool:
    if key == "properties":
        return _looks_like_properties_keyword(value)
    if key == "required":
        return type(value) is list
    if key == "additionalProperties":
        return type(value) in (bool, dict)
    if key in _ROOT_STRING_ANNOTATIONS:
        return type(value) is str
    if key in _ROOT_BOOL_ANNOTATIONS:
        return type(value) is bool
    if key in _ROOT_LIST_ANNOTATIONS:
        return type(value) is list
    if key in _ROOT_DICT_KEYWORDS:
        return type(value) is dict
    if key in _ROOT_INT_KEYWORDS:
        return type(value) is int and type(value) is not bool and value >= 0
    if key.startswith("x-"):
        return True
    return False


def canonicalize_entity_schema(entity_schema: Any) -> dict[str, Any]:
    """Own one schema and normalize legacy field maps at the factory edge.

    The validator's sole internal Entity shape is a canonical root schema.  A
    legacy map such as ``{"title": {"type": "string"}}`` is accepted only
    here and deterministically wrapped as ``object.properties``.  Canonical
    typed schemas and root composites remain unchanged; object-keyword schemas
    that omitted ``type`` receive ``type: object``.  Document storage keeps its
    original representation, avoiding a persistence migration.
    """
    cloned = _clone_json_value(entity_schema)
    if type(cloned) is not dict:
        raise ZeError(
            "validation_failed",
            "framework validator schema must be an exact JSON object",
            context={"reason": "validator_schema_invalid"},
        )

    if not cloned:
        return {}
    if any(
        type(cloned.get(keyword)) is list
        for keyword in _ROOT_COMPOSITE_KEYWORDS
    ):
        return _compile_schema_node(cloned, field_context=False)
    if type(cloned.get("type")) is str:
        return _compile_schema_node(cloned, field_context=False)
    has_object_marker = (
        _looks_like_properties_keyword(cloned.get("properties"))
        or type(cloned.get("required")) is list
        or type(cloned.get("additionalProperties")) in (bool, dict)
        or any(keyword in cloned for keyword in _ROOT_INT_KEYWORDS)
    )
    if has_object_marker and all(
        _is_historical_root_member(key, value)
        for key, value in cloned.items()
    ):
        canonical = dict(cloned)
        canonical["type"] = "object"
        return _compile_schema_node(canonical, field_context=False)
    if any(type(field_schema) is not dict for field_schema in cloned.values()):
        raise ZeError(
            "validation_failed",
            "legacy entity field schemas must be exact JSON objects",
            context={"reason": "validator_schema_invalid"},
        )
    return _compile_schema_node(
        {"type": "object", "properties": cloned},
        field_context=False,
    )


def _canonical_schema_bytes(entity_schema: Any) -> bytes:
    canonical = canonicalize_entity_schema(entity_schema)
    # Input is already an exact strict-JSON tree.  Canonical JSON is therefore
    # deterministic and hook-free; bytes are immutable and preserve -0.0.
    return json.dumps(
        canonical,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8", "strict")

class _FrameworkValidator:
    """Exact callable identity for validators produced by this framework leaf.

    The public contract remains the ``Validator`` callable protocol.  The exact
    private wrapper lets trusted persistence code distinguish a validator that
    can be deterministically reinstalled from Document schema from an arbitrary
    extension callable whose semantics would be lost during restore.
    """

    __slots__ = ("__authority_bytes",)

    def __init__(self, entity_schema: dict[str, Any]) -> None:
        object.__setattr__(
            self,
            "_FrameworkValidator__authority_bytes",
            _canonical_schema_bytes(entity_schema),
        )

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("framework validator authority is immutable")

    def __call__(
        self,
        data: dict[str, Any],
        schema: dict[str, Any],
        path: str,
    ) -> None:
        # The wrapper owns one normalized authority.  ``schema`` remains in the
        # stable Validator protocol for custom callables, but cannot override a
        # framework validator with a raw flat or permissive caller view.
        authority = json.loads(
            object.__getattribute__(
                self, "_FrameworkValidator__authority_bytes"
            )
        )
        validate(data, authority, path)


def is_framework_validator(
    value: Any,
    expected_schema: dict[str, Any] | None = None,
) -> bool:
    """Check exact wrapper identity and, optionally, its bound authority.

    ``expected_schema`` is normalized through the same factory boundary, so a
    legacy flat declaration and its canonical object equivalent match.  An
    invalid expected schema returns ``False`` instead of widening authority.
    """
    if type(value) is not _FrameworkValidator:
        return False
    try:
        authority_bytes = object.__getattribute__(
            value, "_FrameworkValidator__authority_bytes"
        )
    except AttributeError:
        return False
    if type(authority_bytes) is not bytes:
        return False
    if expected_schema is None:
        return True
    try:
        expected_bytes = _canonical_schema_bytes(expected_schema)
    except ZeError:
        return False
    return authority_bytes == expected_bytes


def make_validator(entity_schema: dict[str, Any]) -> Validator:
    """返回持有规范化 Entity schema 的 Validator callable.

    用法 (Document.add 自动注入):
        v = make_validator(doc.schema[entity.type])
        entity.install_validator(doc.schema[entity.type], v)
    """
    return _FrameworkValidator(entity_schema)


# ==============================================================
# schema doctor (R-C18): 期望结构导出 + 汇总诊断
# ==============================================================
# 三个纯只读函数, 全部只吃 canonical schema / value, 绝不改入参、绝不 raise
# (diagnose)、绝不产生 Operation。schema 是"期望结构"的唯一真源: describe_schema /
# minimal_example / diagnose 都从同一份 schema 派生, 零重复定义。与 validate 的 raise
# 契约互补而不替代(现有 add/update/composite 全依赖 validate 遇错即 raise)。

# 约束键(供 describe_schema 摘录, 剔除结构键: items/itemSchema/properties/required, enum 单列)
_CONSTRAINT_KEYWORDS = (
    _STRING_KEYWORDS
    | _NUMBER_KEYWORDS
    | (_ARRAY_KEYWORDS - frozenset(("items", "itemSchema")))
    | (_OBJECT_KEYWORDS - frozenset(("properties", "required")))
) - frozenset(("enum",))

# 各 format 的最小合法样例(对齐 _FORMAT_PATTERNS)
_FORMAT_SAMPLE: dict[str, str] = {
    "email": "a@b.co",
    "uri": "https://example.com",
    "uuid": "0" * 32,
    "iso_datetime": "2000-01-01T00:00:00",
    "color": "#000000",
}

# 深度 / 条数护栏(对抗审 B3: 防大数组 / 深嵌套爆炸与 RecursionError)
_MAX_DIAGNOSE_DEPTH = 64
_MAX_DESCRIBE_DEPTH = 40
_MAX_EXAMPLE_DEPTH = 40
_MAX_FINDINGS = 200


def _required_fields(schema: dict[str, Any]) -> set[str]:
    """融合父 required 数组 + 字段级 required:True。单一真源, 三处共用。"""
    if not isinstance(schema, dict):
        return set()
    required = set(schema.get("required") or [])
    for key, sub in (schema.get("properties") or {}).items():
        if isinstance(sub, dict) and sub.get("required") is True:
            required.add(key)
    return required


# -------- describe_schema: schema → 人类/AI 可读结构树 --------

def _attach_annotations(schema: dict[str, Any], node: dict[str, Any]) -> None:
    if "label" in schema:
        node["label"] = schema["label"]
    elif "labelKey" in schema:
        node["labelKey"] = schema["labelKey"]
    if "description" in schema:
        node["description"] = schema["description"]


def describe_schema(schema: Any, *, required: bool = False, depth: int = 0) -> dict[str, Any]:
    """把 canonical schema 递归描述成结构树(不吃 value)。含 label/约束/enum/字段/样例。"""
    node: dict[str, Any] = {}
    if not isinstance(schema, dict) or not schema:
        node["type"] = "any"
        if required:
            node["required"] = True
        return node
    if depth > _MAX_DESCRIBE_DEPTH:
        node["type"] = schema.get("type", "any")
        node["_truncated"] = True
        if required:
            node["required"] = True
        return node

    for mode in ("oneOf", "anyOf", "allOf"):
        if mode in schema:
            node["type"] = mode
            if required:
                node["required"] = True
            _attach_annotations(schema, node)
            node["alternatives"] = [
                describe_schema(alt, depth=depth + 1) for alt in (schema[mode] or [])
            ]
            _attach_example(schema, node)
            return node

    t = schema.get("type", "any")
    node["type"] = t
    if required:
        node["required"] = True
    _attach_annotations(schema, node)
    constraints = {k: schema[k] for k in _CONSTRAINT_KEYWORDS if k in schema}
    if constraints:
        node["constraints"] = constraints
    if "enum" in schema:
        node["enum"] = schema["enum"]
    if "to" in schema:
        node["to"] = schema["to"]

    if t == "object":
        props = schema.get("properties") or {}
        req = _required_fields(schema)
        node["fields"] = {
            name: describe_schema(
                {k: v for k, v in sub.items() if k != "required"}
                if isinstance(sub, dict) else sub,
                required=(name in req), depth=depth + 1,
            )
            for name, sub in props.items()
        }
    elif t == "array":
        item_schema = schema.get("items") or schema.get("itemSchema")
        if item_schema:
            node["items"] = describe_schema(item_schema, depth=depth + 1)

    _attach_example(schema, node)
    return node


def _attach_example(schema: dict[str, Any], node: dict[str, Any]) -> None:
    value, complete = _example_for(schema)
    node["example"] = value
    if not complete:
        node["_incomplete"] = True   # 样例不保证合法(复合/pattern 等), 诚实标注(no-fake-spec)


# -------- minimal_example: 最小合法样例(生成后回验, 不合法即标 incomplete) --------

def _example_scalar(t: str, schema: dict[str, Any]) -> Any:
    if "default" in schema:
        return schema["default"]
    enum = schema.get("enum")
    if enum:
        return enum[0]
    if t == "string":
        fmt = schema.get("format")
        if fmt:
            return _FORMAT_SAMPLE.get(fmt, "")
        ml = schema.get("minLength")
        return "x" * ml if isinstance(ml, int) and ml > 0 else ""
    if t in ("integer", "number"):
        if (mn := schema.get("minimum")) is not None:
            return mn
        if (exmn := schema.get("exclusiveMinimum")) is not None:
            return exmn + 1
        return 0
    if t == "boolean":
        return False
    if t == "null":
        return None
    if t == "asset_ref":
        return {"sha256": "0" * 64}
    if t == "entity_ref":
        return {"$ref": f"{schema.get('to') or 'type'}/id"}
    return None


def _verified(candidate: Any, schema: dict[str, Any]) -> tuple[Any, bool]:
    """候选样例回验: 过 validate → complete=True; 否则诚实返回 complete=False。"""
    try:
        validate(candidate, schema)
        return candidate, True
    except ZeError:
        return candidate, False


def _example_for(schema: Any, depth: int = 0) -> tuple[Any, bool]:
    if not isinstance(schema, dict) or not schema:
        return None, True   # untyped: 任意值, None 合法
    if depth > _MAX_EXAMPLE_DEPTH:
        return None, False

    for mode in ("oneOf", "anyOf", "allOf"):
        if mode in schema:
            alts = schema[mode] or []
            candidate = _example_for(alts[0], depth + 1)[0] if alts else None
            return _verified(candidate, schema)

    t = schema.get("type")
    if t is None:
        return None, True
    if t == "object":
        props = schema.get("properties") or {}
        candidate: dict[str, Any] = {}
        for key in _required_fields(schema):
            sub = props.get(key, {})
            sub_clean = ({k: v for k, v in sub.items() if k != "required"}
                         if isinstance(sub, dict) else {})
            candidate[key] = _example_for(sub_clean, depth + 1)[0]
        return _verified(candidate, schema)
    if t == "array":
        item_schema = schema.get("items") or schema.get("itemSchema")
        minitems = schema.get("minItems") or 0
        if item_schema and minitems:
            item_val = _example_for(item_schema, depth + 1)[0]
            return _verified([item_val for _ in range(minitems)], schema)
        return _verified([], schema)
    return _verified(_example_scalar(t, schema), schema)


def minimal_example(schema: Any) -> Any:
    """生成能过 validate 的最小样例; 复合/pattern 等无法保证时返回占位(见 describe 的 _incomplete)。"""
    return _example_for(schema)[0]


# -------- diagnose: 汇总全部不符(永不 raise, 复用 validate 的约束判定) --------

def _make_finding(path: str, reason: str, value: Any, subschema: Any) -> dict[str, Any]:
    return {
        "path": path,
        "reason": reason,
        "got": value,                              # 原始值; 脱敏在 sdk transport 层做
        "got_type": type(value).__name__,
        "expected": describe_schema(subschema if isinstance(subschema, dict) else {}),
        "minimal_example": minimal_example(subschema),
    }


def _finding_from(err: ZeError, subschema: Any) -> dict[str, Any]:
    ctx = getattr(err, "context", None) or {}
    return _make_finding(
        ctx.get("path", "$"),
        ctx.get("reason") or (str(err.args[0]) if err.args else "schema violation"),
        ctx.get("value"),
        subschema,
    )


def _object_local_findings(value: dict, schema: dict, path: str, out: list) -> None:
    """object 本级约束(不递归子字段), 逐字对齐 _validate_object。"""
    if (mn := schema.get("minProperties")) is not None and len(value) < mn:
        out.append(_make_finding(path, f"minProperties {mn}", value, schema))
    if (mx := schema.get("maxProperties")) is not None and len(value) > mx:
        out.append(_make_finding(path, f"maxProperties {mx}", value, schema))
    for key in _required_fields(schema):
        if key not in value:
            out.append(_make_finding(path, f"missing required field {key!r}", value, schema))
    if schema.get("additionalProperties", True) is False:
        extra = set(value.keys()) - set(schema.get("properties") or {})
        if extra:
            out.append(_make_finding(
                path, f"additionalProperties not allowed: {sorted(extra)}", value, schema))


def _array_local_findings(value: list, schema: dict, path: str, out: list) -> None:
    """array 本级约束(不递归元素), 逐字对齐 _validate_array。"""
    if (mn := schema.get("minItems")) is not None and len(value) < mn:
        out.append(_make_finding(path, f"minItems {mn}", value, schema))
    if (mx := schema.get("maxItems")) is not None and len(value) > mx:
        out.append(_make_finding(path, f"maxItems {mx}", value, schema))
    if schema.get("uniqueItems"):
        seen: list = []
        for item in value:
            if item in seen:
                out.append(_make_finding(path, "uniqueItems", value, schema))
                break
            seen.append(item)


def _diagnose_node(value: Any, schema: Any, path: str, out: list, depth: int) -> None:
    if len(out) >= _MAX_FINDINGS:
        return
    if depth > _MAX_DIAGNOSE_DEPTH:
        out.append({"path": path, "reason": "structure too deep to diagnose",
                    "got": None, "got_type": "", "expected": {}, "minimal_example": None})
        return
    if not isinstance(schema, dict) or not schema:
        return

    composite = any(m in schema for m in ("oneOf", "anyOf", "allOf"))
    t = schema.get("type")

    # object / array: 逐兄弟下降收集; 其余(复合/叶子/ref/untyped/类型不符)走 validate 捕获
    if not composite and t == "object":
        if not isinstance(value, dict):
            out.append(_make_finding(path, "expected object", value, schema))
            return
        _object_local_findings(value, schema, path, out)
        for key, sub in (schema.get("properties") or {}).items():
            if len(out) >= _MAX_FINDINGS:
                return
            if key in value:
                sub_clean = ({k: v for k, v in sub.items() if k != "required"}
                             if isinstance(sub, dict) else sub)
                _diagnose_node(value[key], sub_clean, f"{path}.{key}", out, depth + 1)
        return
    if not composite and t == "array":
        if not isinstance(value, list):
            out.append(_make_finding(path, "expected array", value, schema))
            return
        _array_local_findings(value, schema, path, out)
        item_schema = schema.get("items") or schema.get("itemSchema")
        if item_schema:
            for i, item in enumerate(value):
                if len(out) >= _MAX_FINDINGS:
                    return
                _diagnose_node(item, item_schema, f"{path}[{i}]", out, depth + 1)
        return

    try:
        validate(value, schema, path)
    except ZeError as err:
        out.append(_finding_from(err, schema))
    except RecursionError:
        out.append({"path": path, "reason": "structure too deeply nested to validate",
                    "got": None, "got_type": "", "expected": {}, "minimal_example": None})


def diagnose(value: Any, schema: Any, path: str = "$") -> list[dict[str, Any]]:
    """汇总 value 对 schema 的**全部**不符(空 = 完全合法)。永不 raise; 不改入参。

    与 validate 互补: validate 遇第一个错即 raise(活写路径依赖它), diagnose 收集全部
    供诊断报告。约束判定全部复用 validate/_validate_* 本体(叶子经 validate 捕获, object/
    array 本级约束逐字对齐), 保证两者对同一 schema 判定一致(property gate 守护)。
    """
    out: list[dict[str, Any]] = []
    _diagnose_node(value, schema, path, out, 0)
    return out
