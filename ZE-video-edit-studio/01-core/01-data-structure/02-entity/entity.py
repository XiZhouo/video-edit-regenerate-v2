"""Entity — 项目里可独立选中的数据原子.

完整 SPEC 见同目录 SPEC.md.

已实装 (无跨叶子依赖):
  - Entity: id/type/name/version/data + CAS update/delete/restore
  - 软删 / restore (CAS)
  - read_only entity 拒任何写
  - 软删后 update 拒 (但 restore 可恢复)
  - Actor / AiProvenance 值类型
  - Validator 协议注入 (默认 no-op; 04-field-types 完成后由 caller 注入真校验器)
  - to_dict / from_dict 完整 round-trip (不走 update, 保真)
  - get(key, default) / provenance(field) / actor_log() 读取助手

待补 (真依赖别的叶子):
  - 真 schema 校验器       [04-field-types 完成后由 Document 注入]
  - asset_ref 识别          [03-asset 启动后]
  - Operation 共享 Actor    [02-operation 启动后 from entity import Actor]
"""

from __future__ import annotations

import math
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, NoReturn


# ==============================================================
# ZeError (M1 占位, 后续抽到独立 errors 模块统一)
# ==============================================================

class ZeError(Exception):
    """M1 占位异常."""

    def __init__(self, code: str, message: str = "", *, context: dict | None = None) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message   # 纯人读消息(不含 [code] 前缀);错误信封序列化用它,前端 toast 直显
        self.context = context or {}


# ==============================================================
# Strict JSON value ownership
# ==============================================================

_MAX_JSON_CONTAINER_DEPTH = 120
_MAX_JSON_CLONE_NODES = 250_000
_MAX_JSON_INT_ABS_EXCLUSIVE = 10 ** 4300
_JSON_VALIDATION_MESSAGE = "value must be strict JSON data"


def _monotonic_entity_timestamp(created_at: float, updated_at: float) -> float:
    """Return a finite wall-clock timestamp that never moves an Entity backward."""
    now = time.time()
    if type(now) not in (int, float) or type(now) is bool or not math.isfinite(now):
        now = updated_at
    return max(float(now), float(created_at), float(updated_at))


def _raise_json_validation_failed() -> NoReturn:
    """Raise one stable, payload-free error for every strict-JSON rejection."""
    raise ZeError("validation_failed", _JSON_VALIDATION_MESSAGE)


def _require_json_string(value: Any, *, non_empty: bool = False) -> str:
    """Return an exact, UTF-8 encodable builtin string or raise stably."""
    if type(value) is not str or (non_empty and not value):
        _raise_json_validation_failed()
    # A Python ``str`` fails strict UTF-8 encoding exactly when it contains a
    # surrogate code point.  Scan in place instead of first allocating a second
    # byte string proportional to an otherwise-valid, potentially large value.
    for character in value:
        codepoint = ord(character)
        if 0xD800 <= codepoint <= 0xDFFF:
            _raise_json_validation_failed()
    return value


def _require_json_bool(value: Any) -> bool:
    """Return an exact builtin bool without invoking a truth-value hook."""
    if type(value) is not bool:
        _raise_json_validation_failed()
    return value


def _clone_json_value(value: Any) -> Any:
    """Validate and detach one strict JSON tree without invoking copy hooks.

    Accepted nodes are exact builtin ``None`` / ``bool`` / bounded ``int`` /
    finite ``float`` / UTF-8 ``str`` / ``list`` / ``dict`` values.  Dict keys
    must be exact UTF-8 strings.  Container aliases are expanded into an owned
    tree; active-path cycles, a 121st nested container, and graphs whose
    expanded representation exceeds 250,000 value/key nodes are rejected.
    The 120-level business limit leaves explicit room for outer deterministic
    envelope structure; it is not a byte-size or persistence-capacity promise.

    This private helper is shared with ``04-field-types`` so framework-owned
    schemas use exactly the same ownership and validation boundary.
    """
    return _clone_json_value_with_budget(value, [_MAX_JSON_CLONE_NODES])


def _clone_json_value_with_budget(value: Any, remaining_nodes: list[int]) -> Any:
    """Clone one value while sharing an expanded-node budget with its owner."""
    active_containers: set[int] = set()

    def clone(node: Any, container_depth: int) -> Any:
        if remaining_nodes[0] <= 0:
            _raise_json_validation_failed()
        remaining_nodes[0] -= 1
        node_type = type(node)

        if node is None:
            return None
        if node_type is bool:
            return node
        if node_type is int:
            if not (-_MAX_JSON_INT_ABS_EXCLUSIVE < node < _MAX_JSON_INT_ABS_EXCLUSIVE):
                _raise_json_validation_failed()
            return node
        if node_type is float:
            if not math.isfinite(node):
                _raise_json_validation_failed()
            return node
        if node_type is str:
            return _require_json_string(node)

        if node_type is not list and node_type is not dict:
            _raise_json_validation_failed()
        if container_depth >= _MAX_JSON_CONTAINER_DEPTH:
            _raise_json_validation_failed()

        node_id = id(node)
        if node_id in active_containers:
            _raise_json_validation_failed()
        active_containers.add(node_id)
        try:
            if node_type is list:
                return [clone(item, container_depth + 1) for item in node]

            out: dict[str, Any] = {}
            for key, item in node.items():
                if remaining_nodes[0] <= 0:
                    _raise_json_validation_failed()
                remaining_nodes[0] -= 1
                owned_key = _require_json_string(key)
                out[owned_key] = clone(item, container_depth + 1)
            return out
        finally:
            active_containers.remove(node_id)

    return clone(value, 0)


def _clone_json_object(value: Any) -> dict[str, Any]:
    """Clone a strict JSON object, rejecting all other top-level shapes."""
    if type(value) is not dict:
        _raise_json_validation_failed()
    cloned = _clone_json_value(value)
    # The exact-type check above and clone implementation make this invariant
    # unconditional; the annotation keeps call sites precise on Python 3.9.
    return cloned


def _clone_json_envelope(value: Any) -> dict[str, Any]:
    """Clone an object envelope while applying the depth budget per field.

    Persistence and value-object envelopes add structural wrappers around
    already-owned JSON trees.  Depth resets per field, while one shared node
    budget covers the entire envelope so many individually-small fields cannot
    bypass the expansion ceiling.  Cycles (including a value pointing back to
    the envelope) are still rejected.
    """
    if type(value) is not dict:
        _raise_json_validation_failed()
    remaining_nodes = [_MAX_JSON_CLONE_NODES]
    remaining_nodes[0] -= 1  # envelope object
    out: dict[str, Any] = {}
    for key, item in value.items():
        if remaining_nodes[0] <= 0:
            _raise_json_validation_failed()
        remaining_nodes[0] -= 1
        owned_key = _require_json_string(key)
        out[owned_key] = _clone_json_value_with_budget(item, remaining_nodes)
    return out


_JSON_SCALAR_TYPES = (type(None), bool, int, float, str)


def _coerce_legacy_bool(value: Any) -> bool:
    """Apply legacy bool conversion to an already strict exact JSON scalar."""
    if type(value) not in _JSON_SCALAR_TYPES:
        _raise_json_validation_failed()
    return bool(value)


def _coerce_legacy_int(value: Any) -> int:
    """Apply legacy int conversion without permitting hostile object hooks."""
    if type(value) not in _JSON_SCALAR_TYPES:
        _raise_json_validation_failed()
    conversion_failed = False
    converted = 0
    try:
        converted = int(value)
    except (TypeError, ValueError, OverflowError):
        conversion_failed = True
    if conversion_failed:
        _raise_json_validation_failed()
    # Conversion from a string on Python 3.9 can exceed the JSON integer cap.
    return _clone_json_value(converted)


def _coerce_legacy_float(value: Any) -> float:
    """Apply legacy float conversion without permitting hostile object hooks."""
    if type(value) not in _JSON_SCALAR_TYPES:
        _raise_json_validation_failed()
    conversion_failed = False
    converted = 0.0
    try:
        converted = float(value)
    except (TypeError, ValueError, OverflowError):
        conversion_failed = True
    if conversion_failed:
        _raise_json_validation_failed()
    return _clone_json_value(converted)


def _select_validator(validator: Any) -> Validator:
    """Preserve a validator by identity while rejecting non-callable values."""
    if validator is None:
        return _no_op_validator
    if not callable(validator):
        _raise_json_validation_failed()
    return validator


def _require_nonnegative_json_int(value: Any) -> int:
    """Return an exact non-negative codec-bounded integer."""
    if type(value) is not int or value < 0:
        _raise_json_validation_failed()
    return _clone_json_value(value)


def _require_nonnegative_json_number(value: Any) -> int | float:
    """Return an exact finite, non-negative JSON number (never bool)."""
    if type(value) is int:
        return _require_nonnegative_json_int(value)
    if type(value) is float and math.isfinite(value) and value >= 0:
        return value
    _raise_json_validation_failed()


# ==============================================================
# 值类型: Actor / AiProvenance
# ==============================================================

_VALID_ACTOR_SOURCES = ("user", "ai", "sync", "system")
_AI_PROVENANCE_FIELDS = (
    "actor",
    "model",
    "model_version",
    "prompt_template_id",
    "prompt_args",
    "cost_usd",
    "token_in",
    "token_out",
    "ts",
    "trace_id",
)


@dataclass(frozen=True)
class Actor:
    """变更主体. source 四态: user | ai | sync | system."""
    user_id: str
    device_id: str = "default"
    source: str = "user"

    def __post_init__(self) -> None:
        _actor_payload(self)

    def to_dict(self) -> dict[str, Any]:
        return _actor_payload(self)


def _actor_payload(value: Any) -> dict[str, Any]:
    """Validate the exact Actor semantic domain used by persisted actor logs."""
    if type(value) is not Actor:
        _raise_json_validation_failed()
    state = object.__getattribute__(value, "__dict__")
    expected = ("user_id", "device_id", "source")
    if (
        type(state) is not dict
        or len(state) != len(expected)
        or any(type(key) is not str for key in state)
        or any(field_name not in state for field_name in expected)
    ):
        _raise_json_validation_failed()
    user_id = _require_json_string(state["user_id"])
    device_id = _require_json_string(state["device_id"])
    source = _require_json_string(state["source"])
    if source not in _VALID_ACTOR_SOURCES:
        _raise_json_validation_failed()
    return {"user_id": user_id, "device_id": device_id, "source": source}


@dataclass(frozen=True)
class AiProvenance:
    """AI 字段溯源信息 (挂在 entity._provenance[field_name])."""
    actor: str                       # user_id (谁触发的)
    model: str
    model_version: str = ""
    prompt_template_id: str = ""
    prompt_args: dict[str, Any] = field(default_factory=dict)
    cost_usd: float = 0.0
    token_in: int = 0
    token_out: int = 0
    ts: float = 0.0
    trace_id: str = ""

    def __post_init__(self) -> None:
        payload = _ai_provenance_payload(self)
        object.__setattr__(self, "prompt_args", payload["prompt_args"])

    def to_dict(self) -> dict[str, Any]:
        return _ai_provenance_payload(self)


def _ai_provenance_payload_refs(value: Any) -> dict[str, Any]:
    """Validate scalar/state shape and return a passive prompt_args reference.

    The caller owns the aggregate clone budget.  In particular, a provenance
    map must not clone each large ``prompt_args`` under a fresh 250k allowance
    before discovering that their combined representation is too large.
    """
    if type(value) is not AiProvenance:
        _raise_json_validation_failed()
    state = object.__getattribute__(value, "__dict__")
    if type(state) is not dict or len(state) != len(_AI_PROVENANCE_FIELDS):
        _raise_json_validation_failed()
    if any(type(key) is not str for key in state):
        _raise_json_validation_failed()
    if any(field_name not in state for field_name in _AI_PROVENANCE_FIELDS):
        _raise_json_validation_failed()
    if type(state["prompt_args"]) is not dict:
        _raise_json_validation_failed()
    return {
        "actor": _require_json_string(state["actor"]),
        "model": _require_json_string(state["model"]),
        "model_version": _require_json_string(state["model_version"]),
        "prompt_template_id": _require_json_string(state["prompt_template_id"]),
        "prompt_args": state["prompt_args"],
        "cost_usd": _require_nonnegative_json_number(state["cost_usd"]),
        "token_in": _require_nonnegative_json_int(state["token_in"]),
        "token_out": _require_nonnegative_json_int(state["token_out"]),
        "ts": _require_nonnegative_json_number(state["ts"]),
        "trace_id": _require_json_string(state["trace_id"]),
    }


def _ai_provenance_payload(value: Any) -> dict[str, Any]:
    """Return one detached provenance payload under one aggregate budget."""
    return _clone_json_envelope(_ai_provenance_payload_refs(value))


def _clone_ai_provenance(value: Any) -> AiProvenance:
    """Return a detached exact AiProvenance without calling instance hooks."""
    payload = _ai_provenance_payload(value)
    return AiProvenance(**payload)


def _clone_ai_provenance_map(value: Any) -> dict[str, AiProvenance]:
    """Own an update provenance mapping and every nested prompt_args tree."""
    if type(value) is not dict:
        _raise_json_validation_failed()
    payload_refs: dict[str, Any] = {}
    for field_name, provenance in value.items():
        owned_field_name = _require_json_string(field_name)
        payload_refs[owned_field_name] = _ai_provenance_payload_refs(provenance)
    # Reject aggregate overflow before constructing/cloning any output item.
    owned_payloads = _clone_json_envelope(payload_refs)
    return {
        field_name: AiProvenance(**payload)
        for field_name, payload in owned_payloads.items()
    }


def _clone_entity_durable_payload(
    *,
    entity_id: Any,
    entity_type: Any,
    name: Any,
    version: Any,
    deleted: Any,
    read_only: Any,
    data: Any,
    provenance: Any,
    actor_log: Any,
    created_at: Any,
    updated_at: Any,
) -> dict[str, Any]:
    """Validate and detach the exact payload emitted by ``Entity.to_dict``.

    This is the single aggregate node-budget boundary for one Entity's durable
    value.  Individual data/provenance values may each fit their own ingress
    boundary while their combined representation does not; every constructor,
    restore, and mutation therefore prepares this complete envelope before it
    publishes state.  The limit is a value-node limit, not a UTF-8 byte quota or
    a cross-Entity/Document capacity promise.
    """
    if type(provenance) is not dict:
        _raise_json_validation_failed()
    provenance_payload_refs: dict[str, Any] = {}
    for field_name, value in provenance.items():
        owned_field_name = _require_json_string(field_name)
        provenance_payload_refs[owned_field_name] = _ai_provenance_payload_refs(value)

    return _clone_json_envelope({
        "id": entity_id,
        "type": entity_type,
        "name": name,
        "version": version,
        "deleted": deleted,
        "read_only": read_only,
        "data": data,
        "provenance": provenance_payload_refs,
        "actor_log": actor_log,
        "created_at": created_at,
        "updated_at": updated_at,
    })


# ==============================================================
# Validator 协议 (决策 1: 协议注入, 04-field-types 完成后传真校验器)
# ==============================================================

Validator = Callable[[dict[str, Any], dict[str, Any], str], None]
"""(data, schema, path) -> None; 不通过抛 ZeError('schema_violation')."""


def _no_op_validator(data: dict[str, Any], schema: dict[str, Any], path: str) -> None:
    """默认 validator: 不校验任何东西. 04-field-types 完成后由 caller 注入真校验器."""
    return None


# Private capability imported by Document.  Public install/attach remain useful
# for standalone Entity values, while an admitted Entity can only be rebound by
# its exact owning Document through the private seam below.
_DOCUMENT_AUTHORITY_CAPABILITY = object()
_MAX_SQLITE_INTEGER = 2**63 - 1


# ==============================================================
# dot-path 嵌套补丁助手 (P2 决定1; 纯函数, 不可变写)
# ==============================================================
# 「字段级」= 任意深度 path 级. patches 的 key 含 "." → 沿 path 不可变嵌套写
# (copy-on-write: 复制路径各层、只换末端、缺层补 {}); 不含 "." → 顶层 set (与改前一致).

def get_path(data: dict[str, Any], dotted: str) -> Any:
    """读 dot-path 子值; 中途缺层或非 dict 返回 None."""
    node: Any = data
    for seg in dotted.split("."):
        if not isinstance(node, dict) or seg not in node:
            return None
        node = node[seg]
    return node


def apply_patches(base: dict[str, Any], patches: dict[str, Any]) -> dict[str, Any]:
    """把 patches 折叠进 base 的不可变副本并返回新 dict (base 不被改).

    顶层 key (无 ".") → 整字段 set; dot-path key → copy-on-write 嵌套写.
    同一调用里多条同前缀 dot-path 顺序折叠到同一新 parent (路径层只在 out 内复制一次后复用)。
    """
    out = dict(base)  # 浅拷贝顶层
    copied: set[int] = set()  # out 内已复制(可安全原地写)的 dict id, 防重复 copy 丢失先前折叠
    copied.add(id(out))
    for k, v in patches.items():
        if "." not in k:
            out[k] = v
            continue
        segs = k.split(".")
        node = out
        for seg in segs[:-1]:
            child = node.get(seg)
            if not isinstance(child, dict):
                child = {}
            elif id(child) not in copied:
                child = dict(child)  # 复制 base 的嵌套层 (首次), 不改 base
            node[seg] = child
            copied.add(id(child))
            node = child
        node[segs[-1]] = v
    return out


# ==============================================================
# Entity
# ==============================================================

class Entity:
    """版本化的数据原子.

    所有变更必须走 CAS 协议 (传 expected_version, 不匹配抛 cas_version_mismatch).
    禁止直接 mutate _data; 走 update(patches, expected_version, actor) 唯一路径.

    read_only=True 的 entity 任何写操作都抛 read_only_violation.
    软删后 (deleted=True) 不能 update; 可以 restore.
    """

    def __init__(
        self,
        *,
        type: str,
        data: dict[str, Any] | None = None,
        name: str = "",
        id: str | None = None,
        schema: dict[str, Any] | None = None,
        validator: Validator | None = None,
        read_only: bool = False,
    ) -> None:
        owned_type = _require_json_string(type, non_empty=True)
        owned_name = _require_json_string(name)
        owned_id = None if id is None else _require_json_string(id, non_empty=True)
        owned_data = _clone_json_object({} if data is None else data)
        owned_schema = _clone_json_object({} if schema is None else schema)
        owned_read_only = _require_json_bool(read_only)
        owned_validator = _select_validator(validator)
        entity_id = uuid.uuid4().hex if owned_id is None else owned_id
        created_at = time.time()
        # 构造时校验整 data (schema 非空才跑)
        if owned_schema:
            owned_validator(
                _clone_json_object(owned_data),
                _clone_json_object(owned_schema),
                f"${owned_type}",
            )
        durable = _clone_entity_durable_payload(
            entity_id=entity_id,
            entity_type=owned_type,
            name=owned_name,
            version=1,
            deleted=False,
            read_only=owned_read_only,
            data=owned_data,
            provenance={},
            actor_log=[],
            created_at=created_at,
            updated_at=created_at,
        )

        self._id = durable["id"]
        self._type = durable["type"]
        self._name = durable["name"]
        self._data = durable["data"]
        self._version = durable["version"]
        self._deleted = durable["deleted"]
        self._read_only = durable["read_only"]
        self._schema = owned_schema
        self._validator: Validator = owned_validator
        self._document_owner: Any | None = None
        self._document_admission_state: tuple[dict[str, Any], Validator] | None = None
        self._provenance: dict[str, AiProvenance] = {}
        self._actor_log: list[dict[str, Any]] = durable["actor_log"]
        self._created_at = durable["created_at"]
        self._updated_at = durable["updated_at"]

    # ---- framework 内部: install validator (doc.add 自动注入用) ----

    def install_validator(
        self,
        schema: dict[str, Any],
        validator: Validator,
    ) -> None:
        """Framework 内部用: 给已构造的 entity 注入 schema + validator.

        立即按当前 data 跑一次校验; 不通过抛 ZeError('schema_violation').
        典型场景: Document.add(entity) 时按 entity.type 取 doc.schema[type] 注入.
        """
        self._guard_standalone_validator_mutation()
        owned_schema = _clone_json_object({} if schema is None else schema)
        owned_validator = _select_validator(validator)
        if owned_schema:
            owned_validator(
                _clone_json_object(self._data),
                _clone_json_object(owned_schema),
                f"${self._type}",
            )
        self._schema = owned_schema
        self._validator = owned_validator

    def attach_validator(
        self,
        schema: dict[str, Any],
        validator: Validator,
    ) -> None:
        """注入 schema + validator, **但不立即校验当前 data**.

        与 install_validator 的唯一区别: 不跑构造/注入时的整 data 校验, 只把校验器挂上,
        让**后续** update() 生效. 给持久化恢复路径用 (Document.open): 已落盘的 data 可能
        早于当前 schema 或本就带历史脏值, 立即校验会让 open 直接抛 → 文档打不开.
        attach 保证「未来写入受校验、历史数据容忍」, 避免把静默数据问题升级成加载不可用.
        """
        self._guard_standalone_validator_mutation()
        owned_schema = _clone_json_object({} if schema is None else schema)
        owned_validator = _select_validator(validator)
        self._schema = owned_schema
        self._validator = owned_validator

    def _bind_document_authority(
        self,
        owner: Any,
        schema: dict[str, Any],
        validator: Validator,
        *,
        validate_current: bool,
        capability: Any,
    ) -> None:
        """Atomically bind/rebind validator authority to one Document identity."""
        if capability is not _DOCUMENT_AUTHORITY_CAPABILITY or owner is None:
            raise ZeError(
                "validation_failed",
                "document authority binding is framework-private",
                context={"reason": "document_authority_capability_invalid"},
            )
        if type(validate_current) is not bool:
            _raise_json_validation_failed()
        current_owner = self._document_owner
        if current_owner is not None and current_owner is not owner:
            raise ZeError(
                "validation_failed",
                "an Entity cannot belong to more than one Document",
                context={"reason": "entity_document_owner_mismatch"},
            )

        # Complete preparation (including optional current-data validation)
        # precedes all three assignments, so a failed migration leaves owner,
        # schema, and validator identities untouched.
        owned_schema = _clone_json_object({} if schema is None else schema)
        owned_validator = _select_validator(validator)
        if validate_current and owned_schema:
            owned_validator(
                _clone_json_object(self._data),
                _clone_json_object(owned_schema),
                f"${self._type}",
            )
        admission_state = self._document_admission_state
        if current_owner is None:
            if admission_state is not None:
                _raise_json_validation_failed()
            admission_state = (self._schema, self._validator)
        elif admission_state is None:
            _raise_json_validation_failed()
        self._schema = owned_schema
        self._validator = owned_validator
        self._document_owner = owner
        self._document_admission_state = admission_state

    def _prepare_document_unbind(
        self,
        owner: Any,
        *,
        capability: Any,
    ) -> tuple[dict[str, Any], Validator]:
        if (
            capability is not _DOCUMENT_AUTHORITY_CAPABILITY
            or owner is None
            or self._document_owner is not owner
        ):
            raise ZeError(
                "validation_failed",
                "only the exact owning Document can release an Entity",
                context={"reason": "entity_document_owner_mismatch"},
            )
        admission_state = self._document_admission_state
        if (
            type(admission_state) is not tuple
            or len(admission_state) != 2
            or type(admission_state[0]) is not dict
            or not callable(admission_state[1])
        ):
            _raise_json_validation_failed()
        return admission_state

    def _unbind_document_authority(
        self,
        owner: Any,
        *,
        capability: Any,
    ) -> None:
        schema, validator = self._prepare_document_unbind(
            owner, capability=capability,
        )
        self._schema = schema
        self._validator = validator
        self._document_owner = None
        self._document_admission_state = None

    def _guard_standalone_validator_mutation(self) -> None:
        if self._document_owner is not None:
            raise ZeError(
                "validation_failed",
                "Document-owned validator authority cannot be replaced publicly",
                context={"reason": "document_validator_authority_owned"},
            )

    @property
    def has_validator(self) -> bool:
        """是否注入过真校验器 (非 no-op)."""
        return self._validator is not _no_op_validator

    # ---- property (只读, JSON 容器返回深层 detached tree) ----

    @property
    def id(self) -> str: return self._id
    @property
    def type(self) -> str: return self._type
    @property
    def name(self) -> str: return self._name
    @property
    def version(self) -> int: return self._version
    @property
    def deleted(self) -> bool: return self._deleted
    @property
    def read_only(self) -> bool: return self._read_only
    @property
    def data(self) -> dict[str, Any]: return _clone_json_object(self._data)
    @property
    def created_at(self) -> float: return self._created_at
    @property
    def updated_at(self) -> float: return self._updated_at

    # ---- 读取助手 ----

    def get(self, key: str, default: Any = None) -> Any:
        owned_key = _require_json_string(key)
        if owned_key not in self._data:
            return default
        return _clone_json_value(self._data[owned_key])

    def provenance(self, field_name: str) -> AiProvenance | None:
        owned_field_name = _require_json_string(field_name)
        provenance = self._provenance.get(owned_field_name)
        return None if provenance is None else _clone_ai_provenance(provenance)

    def actor_log(self) -> list[dict[str, Any]]:
        return _clone_json_value(self._actor_log)

    # ---- 变更 (CAS) ----

    def update(
        self,
        patches: dict[str, Any],
        *,
        expected_version: int,
        actor: Actor,
        ai_provenance: dict[str, AiProvenance] | None = None,
        name: str | None = None,
        validator: Validator | None = None,
    ) -> "Entity":
        """合并 patches 进 data; 必须传 expected_version 做 CAS.

        read_only / 软删 / 版本不匹配都抛 ZeError.
        validator 不传则用已安装 authority；显式传入只允许同一对象，不能 per-call override.
        """
        return self._update_impl(
            patches,
            expected_version=expected_version,
            actor=actor,
            ai_provenance=ai_provenance,
            name=name,
            validator=validator,
            enforce_schema=True,
        )

    def _update_for_replay(
        self,
        patches: dict[str, Any],
        *,
        expected_version: int,
        actor: Actor,
        name: str | None = None,
    ) -> "Entity":
        """Framework replay seam: restore history without schema revalidation.

        Undo/redo may need to restore a value written before today's schema.
        Keeping this as a separate internal method prevents the public
        ``validator=`` compatibility parameter from becoming a bypass token;
        strict ownership, CAS, complete-envelope quota, and audit log still run.
        """
        return self._update_impl(
            patches,
            expected_version=expected_version,
            actor=actor,
            ai_provenance=None,
            name=name,
            validator=None,
            enforce_schema=False,
        )

    def _update_impl(
        self,
        patches: dict[str, Any],
        *,
        expected_version: int,
        actor: Actor,
        ai_provenance: dict[str, AiProvenance] | None,
        name: str | None,
        validator: Validator | None,
        enforce_schema: bool,
    ) -> "Entity":
        self._guard_document_mutation_entry()
        self._guard_writable()
        if self._deleted:
            raise ZeError(
                "entity_soft_deleted",
                f"entity {self._id!r} is soft-deleted; restore() first",
                context={"entity_id": self._id, "entity_type": self._type},
            )
        self._guard_version(expected_version)

        # Keep the legacy parameter for signature compatibility, but never let
        # one call replace the installed authority.  Passing the same object is
        # harmless; a different callable (including a no-op) is a stable,
        # pre-commit validation failure.
        if not enforce_schema:
            active_validator = self._validator
        elif validator is None:
            active_validator = self._validator
        else:
            requested_validator = _select_validator(validator)
            if requested_validator is not self._validator:
                _raise_json_validation_failed()
            active_validator = self._validator

        # Own every caller-controlled value before building or validating the
        # candidate.  Nothing below retains a container supplied by the caller.
        owned_patches = _clone_json_object(patches)
        owned_name = None if name is None else _require_json_string(name)
        owned_provenance = (
            {}
            if ai_provenance is None
            else _clone_ai_provenance_map(ai_provenance)
        )
        if type(actor) is not Actor:
            _raise_json_validation_failed()
        owned_actor = Actor.to_dict(actor)

        # 候选新 data (不可变折叠): 顶层 key 整字段 set; dot-path key 走 copy-on-write 嵌套写.
        # 校验后才提交 → 原子 (校验抛则 self._data 不变, 不 bump 版本).
        # Dot-path application can create substantially deeper/more expansive
        # structure than the shallow patch envelope itself.  Re-own the final
        # candidate before validation/commit so every successful mutation stays
        # inside the same getter/persistence value domain.
        new_data = _clone_json_object(apply_patches(self._data, owned_patches))

        # Schema is a whole-Entity invariant.  Validate the complete candidate
        # exactly once so root required/additionalProperties/composites and
        # constraints spanning changed and unchanged fields cannot be bypassed
        # by a patch-shaped validation view.
        if enforce_schema and self._schema:
            active_validator(
                _clone_json_object(new_data),
                _clone_json_object(self._schema),
                f"${self._type}",
            )

        # Prepare the entire post-mutation durable envelope before publishing
        # any field.  This closes the aggregate node budget across data,
        # provenance, actor log, and metadata rather than only checking each
        # caller input separately.
        candidate_name = self._name if owned_name is None else owned_name
        candidate_provenance = self._provenance.copy()
        candidate_provenance.update(owned_provenance)
        candidate_version = self._next_version()
        candidate_updated_at = _monotonic_entity_timestamp(
            self._created_at, self._updated_at,
        )
        candidate_actor_log = list(self._actor_log)
        candidate_actor_log.append({
            "version": candidate_version,
            "actor": owned_actor,
            "ts": candidate_updated_at,
            "fields": sorted(owned_patches.keys()),
        })
        durable = _clone_entity_durable_payload(
            entity_id=self._id,
            entity_type=self._type,
            name=candidate_name,
            version=candidate_version,
            deleted=self._deleted,
            read_only=self._read_only,
            data=new_data,
            provenance=candidate_provenance,
            actor_log=candidate_actor_log,
            created_at=self._created_at,
            updated_at=candidate_updated_at,
        )

        # apply (schema + complete durable-envelope validation passed)
        self._data = durable["data"]
        self._name = durable["name"]
        self._provenance = candidate_provenance
        self._version = durable["version"]
        self._updated_at = durable["updated_at"]
        self._actor_log = durable["actor_log"]
        return self

    def delete(self, *, expected_version: int, actor: Actor) -> "Entity":
        """软删 (CAS). _deleted=True; version bump; actor_log 记 __deleted__."""
        self._guard_document_mutation_entry()
        self._guard_writable()
        self._guard_version(expected_version)
        if type(actor) is not Actor:
            _raise_json_validation_failed()
        owned_actor = Actor.to_dict(actor)
        candidate_version = self._next_version()
        candidate_updated_at = _monotonic_entity_timestamp(
            self._created_at, self._updated_at,
        )
        candidate_actor_log = list(self._actor_log)
        candidate_actor_log.append({
            "version": candidate_version,
            "actor": owned_actor,
            "ts": candidate_updated_at,
            "fields": ["__deleted__"],
        })
        durable = _clone_entity_durable_payload(
            entity_id=self._id,
            entity_type=self._type,
            name=self._name,
            version=candidate_version,
            deleted=True,
            read_only=self._read_only,
            data=self._data,
            provenance=self._provenance,
            actor_log=candidate_actor_log,
            created_at=self._created_at,
            updated_at=candidate_updated_at,
        )
        self._data = durable["data"]
        self._deleted = durable["deleted"]
        self._version = durable["version"]
        self._updated_at = durable["updated_at"]
        self._actor_log = durable["actor_log"]
        return self

    def restore(self, *, expected_version: int, actor: Actor) -> "Entity":
        """恢复软删的 entity (CAS).

        未删则 idempotent (不抛错, 不 bump version).
        read_only 仍拒 (即使没删, 保持一致).
        """
        self._guard_document_mutation_entry()
        self._guard_writable()
        self._guard_version(expected_version)
        if not self._deleted:
            return self
        if type(actor) is not Actor:
            _raise_json_validation_failed()
        owned_actor = Actor.to_dict(actor)
        candidate_version = self._next_version()
        candidate_updated_at = _monotonic_entity_timestamp(
            self._created_at, self._updated_at,
        )
        candidate_actor_log = list(self._actor_log)
        candidate_actor_log.append({
            "version": candidate_version,
            "actor": owned_actor,
            "ts": candidate_updated_at,
            "fields": ["__restored__"],
        })
        durable = _clone_entity_durable_payload(
            entity_id=self._id,
            entity_type=self._type,
            name=self._name,
            version=candidate_version,
            deleted=False,
            read_only=self._read_only,
            data=self._data,
            provenance=self._provenance,
            actor_log=candidate_actor_log,
            created_at=self._created_at,
            updated_at=candidate_updated_at,
        )
        self._data = durable["data"]
        self._deleted = durable["deleted"]
        self._version = durable["version"]
        self._updated_at = durable["updated_at"]
        self._actor_log = durable["actor_log"]
        return self

    # ---- 内部 guard ----

    def _guard_document_mutation_entry(self) -> None:
        owner = self._document_owner
        if owner is None:
            return
        guard = getattr(owner, "_guard_owned_entity_mutation", None)
        if not callable(guard):
            raise ZeError(
                "validation_failed",
                "Document-owned Entity has no mutation authority guard",
                context={"reason": "entity_document_owner_invalid"},
            )
        guard(self)

    def _guard_writable(self) -> None:
        if self._read_only:
            raise ZeError(
                "read_only_violation",
                f"entity {self._id!r} is read-only",
                context={"entity_id": self._id, "entity_type": self._type},
            )

    def _guard_version(self, expected: int) -> None:
        if (
            type(expected) is not int
            or expected < 1
            or expected > _MAX_SQLITE_INTEGER
        ):
            raise ZeError(
                "validation_failed",
                "expected_version must be an exact SQLite positive integer",
                context={"reason": "expected_version_invalid"},
            )
        if (
            type(self._version) is not int
            or self._version < 1
            or self._version > _MAX_SQLITE_INTEGER
        ):
            raise ZeError(
                "validation_failed",
                "entity version state is outside the durable integer domain",
                context={"reason": "entity_version_state_invalid"},
            )
        if expected != self._version:
            raise ZeError(
                "cas_version_mismatch",
                f"expected v{expected} got v{self._version}",
                context={
                    "entity_id": self._id,
                    "entity_type": self._type,
                    "expected_version": expected,
                    "current_version": self._version,
                },
            )

    def _next_version(self) -> int:
        if (
            type(self._version) is not int
            or self._version < 1
            or self._version > _MAX_SQLITE_INTEGER
        ):
            raise ZeError(
                "validation_failed",
                "entity version state is outside the durable integer domain",
                context={"reason": "entity_version_state_invalid"},
            )
        if self._version == _MAX_SQLITE_INTEGER:
            raise ZeError(
                "version_overflow",
                "entity version has reached the durable integer ceiling",
                context={"reason": "entity_version_exhausted"},
            )
        return self._version + 1

    # ---- 持久化: 完整 round-trip ----

    def to_dict(self) -> dict[str, Any]:
        return _clone_entity_durable_payload(
            entity_id=self._id,
            entity_type=self._type,
            name=self._name,
            version=self._version,
            deleted=self._deleted,
            read_only=self._read_only,
            data=self._data,
            provenance=self._provenance,
            actor_log=self._actor_log,
            created_at=self._created_at,
            updated_at=self._updated_at,
        )

    @classmethod
    def from_dict(
        cls,
        d: dict[str, Any],
        *,
        schema: dict[str, Any] | None = None,
        validator: Validator | None = None,
    ) -> "Entity":
        """从 to_dict() 输出恢复 Entity. 不走 update; version/log/timestamps 保真.

        schema / validator 可选; 不传则恢复后没有校验能力 (一般用于纯持久化加载).
        """
        owned = _clone_json_envelope(d)
        raw_data = owned.get("data")
        if "type" not in owned or "id" not in owned:
            _raise_json_validation_failed()
        e = cls(
            type=owned["type"],
            data={} if raw_data is None else raw_data,
            name=owned.get("name", ""),
            id=owned["id"],
            schema=schema,
            validator=validator,
            read_only=_coerce_legacy_bool(owned.get("read_only", False)),
        )
        candidate_version = _coerce_legacy_int(owned.get("version", 1))
        candidate_deleted = _coerce_legacy_bool(owned.get("deleted", False))
        candidate_created_at = _coerce_legacy_float(
            owned.get("created_at", e._created_at)
        )
        candidate_updated_at = _coerce_legacy_float(
            owned.get("updated_at", e._updated_at)
        )

        raw_actor_log = owned.get("actor_log")
        if raw_actor_log is None:
            raw_actor_log = []
        if type(raw_actor_log) is not list or any(
            type(entry) is not dict for entry in raw_actor_log
        ):
            _raise_json_validation_failed()
        candidate_actor_log = _clone_json_value(raw_actor_log)

        prov_in = owned.get("provenance")
        if prov_in is None:
            prov_in = {}
        if type(prov_in) is not dict:
            _raise_json_validation_failed()
        candidate_provenance: dict[str, AiProvenance] = {}
        for fld, prov_dict in prov_in.items():
            if type(prov_dict) is not dict:
                _raise_json_validation_failed()
            try:
                candidate_provenance[fld] = AiProvenance(**prov_dict)
            except TypeError:
                # forward-compat: 额外 key 容错, 丢弃该条
                pass

        durable = _clone_entity_durable_payload(
            entity_id=e._id,
            entity_type=e._type,
            name=e._name,
            version=candidate_version,
            deleted=candidate_deleted,
            read_only=e._read_only,
            data=e._data,
            provenance=candidate_provenance,
            actor_log=candidate_actor_log,
            created_at=candidate_created_at,
            updated_at=candidate_updated_at,
        )
        e._data = durable["data"]
        e._version = durable["version"]
        e._deleted = durable["deleted"]
        e._provenance = candidate_provenance
        e._actor_log = durable["actor_log"]
        e._created_at = durable["created_at"]
        e._updated_at = durable["updated_at"]
        return e
