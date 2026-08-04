"""Strict, owned codec for dormant Pipeline authority v2 descriptors.

This module deliberately has no writer, Operation factory, runtime grant, I/O,
or Supervisor dependency.  Batch 2 can parse and reduce v2 facts, while the
generic Operation carrier continues to reject v2 until Batch 3 installs the
sealed pre-store hook in the same commit that makes the schema writable.

The v1 schema names were accepted as loose, resource-bounded carriers before
their semantic contract existed.  They are permanently untrusted and are
never reinterpreted here as authority.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
from dataclasses import dataclass, field
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any


PIPELINE_AUTHORITY_CODEC_PROTOCOL = "ze.pipeline-authority-codec.v1"
PIPELINE_START_SCHEMA = "ze.pipeline-start.v2"
PIPELINE_CONTROL_SCHEMA = "ze.pipeline-control.v2"
PIPELINE_DOCUMENT_POLICY_SCHEMA = "ze.pipeline-document-policy.v1"
ENTITY_MUTATION_POLICY_SCHEMA = "ze.entity-mutation-policy.v1"

MAX_START_BYTES = 4 * 1024 * 1024
MAX_START_DEPTH = 32
MAX_START_NODES = 100_000
MAX_START_TEXT_BYTES = 256 * 1024
MAX_WORK_ITEMS = 4_096
MAX_STAGES = 64
MAX_INITIAL_OPERATION_IDS = 8_192
MAX_REFERENCES = 16_384
MAX_IDENTIFIER_BYTES = 256
MAX_CONCURRENCY = 64
MAX_RETRIES = 100

MAX_CONTROL_ITEM_IDS = 4_096
# A retry request must be able to carry the advertised maximum number of
# legal 256-byte item identifiers.  Path-safe identifiers may contain quotes,
# which double their canonical JSON footprint, so the old 64 KiB envelope was
# not a coherent contract.  Keep one 64 KiB envelope allowance in addition to
# the worst-case escaped item list.
MAX_CONTROL_BYTES = (64 * 1024) + MAX_CONTROL_ITEM_IDS * (
    (2 * MAX_IDENTIFIER_BYTES) + 4
)
MAX_CONTROL_DEPTH = 12
MAX_CONTROL_NODES = 4_123
MAX_CONTROL_TEXT_BYTES = 16 * 1024

_MAX_INTEGER_MAGNITUDE = 10**4_300
_MAX_SQLITE_INTEGER = 2**63 - 1
_LOWER_HEX = frozenset("0123456789abcdef")
_JSON_HEX = frozenset("0123456789abcdefABCDEF")
_SOURCES = frozenset(("user", "ai", "sync", "system"))
_LIFECYCLE = (
    "create",
    "rename",
    "soft_delete",
    "restore",
    "hard_remove",
)
_LIFECYCLE_SET = frozenset(_LIFECYCLE)
_START_KEYS = frozenset(
    (
        "schema",
        "document_id",
        "run_id",
        "pipeline_id",
        "start_command_id",
        "start_operation_id",
        "work_list",
        "stages",
        "placeholder_bindings",
        "initial_operation_ids",
        "execution",
        "delegation",
    )
)
_WORK_LIST_KEYS = frozenset(("run_id", "name", "items"))
_WORK_ITEM_KEYS = frozenset(
    ("item_id", "entity_type", "entity_id", "artifacts", "payload")
)
_STAGE_KEYS = frozenset(
    (
        "id",
        "label",
        "command_id",
        "produces",
        "resource_scopes",
        "resource_costs",
        "retry_safe",
        "interrupt_safe",
        "pause_after",
        "input_ports",
        "output_ports",
    )
)
_PLACEHOLDER_KEYS = frozenset(("item_id", "entity_type", "entity_id", "operation_id"))
_EXECUTION_KEYS = frozenset(("concurrency", "max_retries", "parameters"))
_DELEGATION_KEYS = frozenset(
    (
        "root_command_digest",
        "document_policy",
        "document_policy_digest",
        "worker_actor",
        "worker_source",
        "targets",
        "creation_scopes",
    )
)
_ACTOR_KEYS = frozenset(("user_id", "device_id", "source"))
_DOCUMENT_POLICY_KEYS = frozenset(("schema", "entity_policies"))
_DOCUMENT_POLICY_ENTITY_KEYS = frozenset(
    ("entity_type", "entity_schema_digest", "policy")
)
_ENTITY_POLICY_REQUIRED_KEYS = frozenset(("schema", "lifecycle", "fields"))
_ENTITY_POLICY_KEYS = _ENTITY_POLICY_REQUIRED_KEYS | frozenset(("cardinality",))
_RESERVED_POLICY_FIELDS = frozenset(("__entity__", "__deleted__", "__restored__"))
_TARGET_KEYS = frozenset(("entity_type", "entity_id", "fields", "lifecycle"))
_CREATION_SCOPE_KEYS = frozenset(
    ("entity_type", "fields", "max_per_operation", "max_per_run")
)
_CONTROL_KEYS = frozenset(
    (
        "schema",
        "document_id",
        "run_id",
        "pipeline_id",
        "control_operation_id",
        "start_operation_id",
        "start_operation_hash",
        "action",
        "parameters",
        "expected_generation",
        "generation",
        "expected_state",
    )
)
_ACTIONS = frozenset(
    (
        "pause",
        "interrupt",
        "cancel",
        "discard",
        "resume",
        "retry_failed",
        "rerun_step",
        "run_from_step",
        "set_breakpoint",
    )
)
_STATES = frozenset(("paused", "interrupted", "reconcile_required", "terminal"))


class PipelineAuthorityCodecError(ValueError):
    """Stable, payload-free descriptor failure."""

    __slots__ = ("code", "reason", "context")

    def __init__(self, code: str, reason: str) -> None:
        if type(code) is not str or not code:
            raise TypeError("code must be a non-empty exact string")
        if type(reason) is not str or not reason:
            raise TypeError("reason must be a non-empty exact string")
        self.code = code
        self.reason = reason
        self.context = {"reason": reason}
        super().__init__(code)


def _fail(reason: str) -> None:
    raise PipelineAuthorityCodecError("pipeline_authority_descriptor_invalid", reason)


class _DetachedFrozenDict:
    """Keep Python 3.9 compatibility without exposing a live field mapping."""

    def __getattribute__(self, name: str) -> Any:
        if name != "__dict__":
            return object.__getattribute__(self, name)
        if type(self) is FrozenPipelineStart:
            fields = (
                "protocol",
                "schema",
                "document_id",
                "run_id",
                "pipeline_id",
                "start_command_id",
                "start_operation_id",
                "initial_operation_ids",
                "descriptor_bytes",
                "descriptor_digest",
            )
        elif type(self) is FrozenPipelineControl:
            fields = (
                "protocol",
                "schema",
                "document_id",
                "run_id",
                "pipeline_id",
                "control_operation_id",
                "start_operation_id",
                "start_operation_hash",
                "action",
                "expected_generation",
                "generation",
                "expected_state",
                "parameters_bytes",
                "descriptor_bytes",
                "descriptor_digest",
            )
        else:
            fields = ()
        detached: dict[str, Any] = {}
        for field_name in fields:
            missing = False
            try:
                member = object.__getattribute__(self, field_name)
            except AttributeError:
                missing = True
                member = None
            if not missing:
                detached[field_name] = member
        return detached


@dataclass(frozen=True)
class FrozenPipelineStart(_DetachedFrozenDict):
    protocol: str
    schema: str
    document_id: str
    run_id: str
    pipeline_id: str
    start_command_id: str
    start_operation_id: str
    initial_operation_ids: tuple[str, ...] = field(repr=False)
    descriptor_bytes: bytes = field(repr=False)
    descriptor_digest: bytes = field(repr=False)


@dataclass(frozen=True)
class FrozenPipelineControl(_DetachedFrozenDict):
    protocol: str
    schema: str
    document_id: str
    run_id: str
    pipeline_id: str
    control_operation_id: str
    start_operation_id: str
    start_operation_hash: str
    action: str
    expected_generation: int
    generation: int
    expected_state: str
    parameters_bytes: bytes = field(repr=False)
    descriptor_bytes: bytes = field(repr=False)
    descriptor_digest: bytes = field(repr=False)

    @property
    def parameters(self) -> dict[str, Any]:
        value = materialize_pipeline_descriptor(self).get("parameters")
        if type(value) is not dict:
            _fail("control_parameters_frozen_invalid")
        return value


def _utf8_size(value: str, maximum: int) -> int:
    total = 0
    for character in value:
        point = ord(character)
        if 0xD800 <= point <= 0xDFFF:
            _fail("text_utf8_invalid")
        if point <= 0x7F:
            total += 1
        elif point <= 0x7FF:
            total += 2
        elif point <= 0xFFFF:
            total += 3
        else:
            total += 4
        if total > maximum:
            _fail("text_budget_exceeded")
    return total


def _text(value: Any, *, maximum: int, nonempty: bool = False) -> str:
    if type(value) is not str or (nonempty and not value):
        _fail("text_invalid")
    _utf8_size(value, maximum)
    return value


def _identifier(value: Any, *, path_safe: bool = False) -> str:
    value = _text(value, maximum=MAX_IDENTIFIER_BYTES, nonempty=True)
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        _fail("identifier_invalid")
    if path_safe and (
        value in (".", "..")
        or any(character in value for character in ("/", "\\", "\x00"))
    ):
        _fail("path_identifier_invalid")
    return value


def _operation_id(value: Any) -> str:
    if (
        type(value) is not str
        or len(value) != 32
        or any(character not in _LOWER_HEX for character in value)
    ):
        _fail("operation_id_invalid")
    return value


def _hash(value: Any, *, reason: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in _LOWER_HEX for character in value)
    ):
        _fail(reason)
    return value


def _own_json(
    value: Any,
    *,
    maximum_bytes: int,
    maximum_depth: int,
    maximum_nodes: int,
    maximum_text_bytes: int,
) -> Any:
    active: set[int] = set()
    nodes = 0
    encoded_bytes = 0

    def add_encoded_bytes(amount: int) -> None:
        nonlocal encoded_bytes
        encoded_bytes += amount
        if encoded_bytes > maximum_bytes:
            _fail("byte_budget_exceeded")

    def own_string(member: str) -> str:
        text_bytes = 0
        add_encoded_bytes(2)
        for character in member:
            point = ord(character)
            if point <= 0x7F:
                raw_bytes = 1
            elif point <= 0x7FF:
                raw_bytes = 2
            elif 0xD800 <= point <= 0xDFFF:
                _fail("text_utf8_invalid")
            elif point <= 0xFFFF:
                raw_bytes = 3
            else:
                raw_bytes = 4
            text_bytes += raw_bytes
            if text_bytes > maximum_text_bytes:
                _fail("text_budget_exceeded")
            if character in {'"', "\\", "\b", "\f", "\n", "\r", "\t"}:
                add_encoded_bytes(2)
            elif point < 0x20:
                add_encoded_bytes(6)
            else:
                add_encoded_bytes(raw_bytes)
        return member

    def own(member: Any, depth: int) -> Any:
        nonlocal nodes
        nodes += 1
        if nodes > maximum_nodes:
            _fail("node_budget_exceeded")
        if depth > maximum_depth:
            _fail("depth_budget_exceeded")
        if type(member) is dict:
            identity = id(member)
            if identity in active:
                _fail("json_cycle_invalid")
            active.add(identity)
            owned: dict[str, Any] = {}
            add_encoded_bytes(2)
            iteration_failed = False
            try:
                count = 0
                for key, item in member.items():
                    if type(key) is not str:
                        _fail("json_key_invalid")
                    if count:
                        add_encoded_bytes(2)
                    add_encoded_bytes(2)
                    owned_key = own(key, depth + 1)
                    owned[owned_key] = own(item, depth + 1)
                    count += 1
            except RuntimeError:
                iteration_failed = True
            finally:
                active.remove(identity)
            if iteration_failed:
                _fail("json_graph_unstable")
            return owned
        if type(member) is list:
            identity = id(member)
            if identity in active:
                _fail("json_cycle_invalid")
            active.add(identity)
            owned_list: list[Any] = []
            add_encoded_bytes(2)
            iteration_failed = False
            try:
                for index, item in enumerate(member):
                    if index:
                        add_encoded_bytes(2)
                    owned_list.append(own(item, depth + 1))
            except RuntimeError:
                iteration_failed = True
            finally:
                active.remove(identity)
            if iteration_failed:
                _fail("json_graph_unstable")
            return owned_list
        if type(member) is int:
            if not -_MAX_INTEGER_MAGNITUDE < member < _MAX_INTEGER_MAGNITUDE:
                _fail("integer_budget_exceeded")
            add_encoded_bytes(len(str(member)))
            return member
        if type(member) is float:
            if not math.isfinite(member):
                _fail("number_invalid")
            add_encoded_bytes(len(repr(member)))
            return member
        if type(member) is str:
            return own_string(member)
        if member is None:
            add_encoded_bytes(4)
            return None
        if type(member) is bool:
            add_encoded_bytes(4 if member else 5)
            return member
        _fail("json_type_invalid")
        raise AssertionError("unreachable")

    return own(value, 1)


def _canonical_bytes(value: Any, *, maximum: int) -> bytes:
    failed = False
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8", "strict")
    except (
        TypeError,
        ValueError,
        OverflowError,
        UnicodeError,
        RecursionError,
        RuntimeError,
    ):
        failed = True
        encoded = b""
    if failed:
        _fail("canonical_encoding_failed")
    if len(encoded) > maximum:
        _fail("byte_budget_exceeded")
    return encoded


def _pipeline_document_policy_digest(value: Any) -> str:
    """Return the one codec-owned digest used by Start and later rechecks."""

    return hashlib.sha256(
        _canonical_bytes(value, maximum=MAX_START_BYTES)
    ).hexdigest()


def _scan_json_budgets(
    value: str,
    *,
    maximum_depth: int,
    maximum_nodes: int,
    maximum_text_bytes: int,
) -> None:
    """Validate structure and budgets without constructing the JSON graph.

    ``json.loads`` necessarily allocates the complete object tree.  Frozen
    authority bytes are therefore scanned first so an attacker cannot turn a
    small-ish byte envelope into hundreds of thousands of Python objects
    before the node/depth limits run.  The scanner uses the same root-depth-1
    and object-key-as-node accounting as :func:`_own_json`.
    """

    index = 0
    length = len(value)
    nodes = 0

    def invalid() -> None:
        _fail("frozen_bytes_invalid")

    def skip_whitespace() -> None:
        nonlocal index
        while index < length and value[index] in " \t\r\n":
            index += 1

    def add_node(depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > maximum_nodes:
            _fail("node_budget_exceeded")
        if depth > maximum_depth:
            _fail("depth_budget_exceeded")

    def parse_hex_quad() -> int:
        nonlocal index
        end = index + 4
        if end > length or any(
            character not in _JSON_HEX for character in value[index:end]
        ):
            invalid()
        raw = value[index:end]
        index = end
        return int(raw, 16)

    def parse_string(*, capture: bool) -> str | None:
        nonlocal index
        if index >= length or value[index] != '"':
            invalid()
        index += 1
        text_bytes = 0
        captured: list[str] | None = [] if capture else None
        while index < length:
            character = value[index]
            index += 1
            if character == '"':
                return "".join(captured) if captured is not None else None
            if character == "\\":
                if index >= length:
                    invalid()
                escape = value[index]
                index += 1
                if escape == '"':
                    point = 0x22
                elif escape == "\\":
                    point = 0x5C
                elif escape == "/":
                    point = 0x2F
                elif escape == "b":
                    point = 0x08
                elif escape == "f":
                    point = 0x0C
                elif escape == "n":
                    point = 0x0A
                elif escape == "r":
                    point = 0x0D
                elif escape == "t":
                    point = 0x09
                elif escape == "u":
                    point = parse_hex_quad()
                    if 0xD800 <= point <= 0xDBFF:
                        if index + 2 > length or value[index : index + 2] != "\\u":
                            _fail("text_utf8_invalid")
                        index += 2
                        low = parse_hex_quad()
                        if not 0xDC00 <= low <= 0xDFFF:
                            _fail("text_utf8_invalid")
                        point = 0x10000 + ((point - 0xD800) << 10) + (low - 0xDC00)
                    elif 0xDC00 <= point <= 0xDFFF:
                        _fail("text_utf8_invalid")
                else:
                    invalid()
            else:
                point = ord(character)
                if point < 0x20:
                    invalid()
                if 0xD800 <= point <= 0xDFFF:
                    _fail("text_utf8_invalid")

            if point <= 0x7F:
                text_bytes += 1
            elif point <= 0x7FF:
                text_bytes += 2
            elif point <= 0xFFFF:
                text_bytes += 3
            else:
                text_bytes += 4
            if text_bytes > maximum_text_bytes:
                _fail("text_budget_exceeded")
            if captured is not None:
                captured.append(chr(point))
        invalid()
        raise AssertionError("unreachable")

    def parse_number() -> None:
        nonlocal index
        if value[index] == "-":
            index += 1
            if index >= length:
                invalid()
        integer_start = index
        if value[index] == "0":
            index += 1
            if index < length and value[index].isdigit():
                invalid()
        elif "1" <= value[index] <= "9":
            index += 1
            while index < length and "0" <= value[index] <= "9":
                index += 1
        else:
            invalid()
        integer_digits = index - integer_start
        is_float = False
        if index < length and value[index] == ".":
            is_float = True
            index += 1
            fraction_start = index
            while index < length and "0" <= value[index] <= "9":
                index += 1
            if index == fraction_start:
                invalid()
        if index < length and value[index] in "eE":
            is_float = True
            index += 1
            if index < length and value[index] in "+-":
                index += 1
            exponent_start = index
            while index < length and "0" <= value[index] <= "9":
                index += 1
            if index == exponent_start:
                invalid()
        if not is_float and integer_digits > 4_300:
            _fail("integer_budget_exceeded")

    def parse_value(depth: int) -> None:
        nonlocal index
        add_node(depth)
        if index >= length:
            invalid()
        character = value[index]
        if character == "{":
            index += 1
            skip_whitespace()
            keys: set[str] = set()
            if index < length and value[index] == "}":
                index += 1
                return
            while True:
                add_node(depth + 1)
                key = parse_string(capture=True)
                if key in keys:
                    _fail("json_duplicate_key_invalid")
                keys.add(key)
                skip_whitespace()
                if index >= length or value[index] != ":":
                    invalid()
                index += 1
                skip_whitespace()
                parse_value(depth + 1)
                skip_whitespace()
                if index < length and value[index] == "}":
                    index += 1
                    return
                if index >= length or value[index] != ",":
                    invalid()
                index += 1
                skip_whitespace()
                if index >= length or value[index] != '"':
                    invalid()
        elif character == "[":
            index += 1
            skip_whitespace()
            if index < length and value[index] == "]":
                index += 1
                return
            while True:
                parse_value(depth + 1)
                skip_whitespace()
                if index < length and value[index] == "]":
                    index += 1
                    return
                if index >= length or value[index] != ",":
                    invalid()
                index += 1
                skip_whitespace()
        elif character == '"':
            parse_string(capture=False)
        elif character == "t" and value.startswith("true", index):
            index += 4
        elif character == "f" and value.startswith("false", index):
            index += 5
        elif character == "n" and value.startswith("null", index):
            index += 4
        elif character == "-" or "0" <= character <= "9":
            parse_number()
        else:
            invalid()

    skip_whitespace()
    parse_value(1)
    skip_whitespace()
    if index != length:
        invalid()


def _decode_owned(
    value: bytes,
    *,
    maximum: int,
    maximum_depth: int,
    maximum_nodes: int,
    maximum_text_bytes: int,
) -> Any:
    if type(value) is not bytes or len(value) > maximum:
        _fail("frozen_bytes_invalid")
    decode_failed = False
    try:
        source = value.decode("utf-8", "strict")
    except UnicodeError:
        decode_failed = True
        source = ""
    if decode_failed:
        _fail("frozen_bytes_invalid")
    _scan_json_budgets(
        source,
        maximum_depth=maximum_depth,
        maximum_nodes=maximum_nodes,
        maximum_text_bytes=maximum_text_bytes,
    )
    loads_failed = False
    try:
        decoded = json.loads(source)
    except (
        json.JSONDecodeError,
        ValueError,
        OverflowError,
        RecursionError,
    ):
        loads_failed = True
        decoded = None
    if loads_failed:
        _fail("frozen_bytes_invalid")
    return decoded


def _exact_keys(value: Any, keys: frozenset[str], reason: str) -> dict[str, Any]:
    if type(value) is not dict or frozenset(value) != keys:
        _fail(reason)
    return value


def _exact_list(value: Any, *, reason: str) -> list[Any]:
    if type(value) is not list:
        _fail(reason)
    return value


def _unique(values: list[str], *, reason: str, sorted_required: bool = False) -> None:
    if len(set(values)) != len(values):
        _fail(reason)
    if sorted_required and values != sorted(values):
        _fail(reason)


def _exact_value_equal(left: Any, right: Any) -> bool:
    """Compare frozen projections without Python's bool/int coercion."""

    if type(left) is not type(right):
        return False
    if type(left) is dict:
        return frozenset(left) == frozenset(right) and all(
            _exact_value_equal(left[key], right[key]) for key in left
        )
    if type(left) in (list, tuple):
        return len(left) == len(right) and all(
            _exact_value_equal(left_item, right_item)
            for left_item, right_item in zip(left, right)
        )
    return bool(left == right)


def _safe_artifact(value: Any) -> str:
    value = _text(value, maximum=MAX_START_TEXT_BYTES, nonempty=True)
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if (
        value == "."
        or "\\" in value
        or "\x00" in value
        or posix.is_absolute()
        or windows.is_absolute()
        or bool(windows.drive)
        or any(part in ("", ".", "..") for part in value.split("/"))
        or posix.as_posix() != value
    ):
        _fail("artifact_path_invalid")
    return value


def _actor(value: Any) -> tuple[str, str, str]:
    actor = _exact_keys(value, _ACTOR_KEYS, "worker_actor_invalid")
    user_id = _identifier(actor["user_id"])
    device_id = _identifier(actor["device_id"])
    source = actor["source"]
    if type(source) is not str or source not in _SOURCES:
        _fail("worker_actor_invalid")
    return user_id, device_id, source


def _validate_port(value: Any) -> int:
    # StageSpec deliberately treats ports as extension JSON bags.  Authority
    # binds every byte of that bag but only assigns framework semantics to its
    # stable non-empty ``id``.  Closing over a guessed port dialect here would
    # reject existing core and real-editor StageSpecs or, worse, normalize
    # away fields that are part of the approved execution intent.
    if type(value) is not dict or "id" not in value:
        _fail("stage_port_invalid")
    _identifier(value["id"])
    return len(value)


def _validate_work_list(
    value: Any, *, run_id: str
) -> tuple[set[str], set[tuple[str, str]], int]:
    work_list = _exact_keys(value, _WORK_LIST_KEYS, "work_list_shape_invalid")
    if _identifier(work_list["run_id"], path_safe=True) != run_id:
        _fail("work_list_run_mismatch")
    _text(work_list["name"], maximum=MAX_START_TEXT_BYTES)
    items = _exact_list(work_list["items"], reason="work_items_invalid")
    if len(items) > MAX_WORK_ITEMS:
        _fail("work_item_count_exceeded")
    item_ids: list[str] = []
    targets: list[tuple[str, str]] = []
    artifacts: list[str] = []
    references = 0
    for raw in items:
        item = _exact_keys(raw, _WORK_ITEM_KEYS, "work_item_shape_invalid")
        item_id = _identifier(item["item_id"], path_safe=True)
        entity_type = _identifier(item["entity_type"])
        entity_id = _identifier(item["entity_id"])
        item_artifacts = _exact_list(
            item["artifacts"], reason="work_item_artifacts_invalid"
        )
        normalized = [_safe_artifact(path) for path in item_artifacts]
        _unique(normalized, reason="work_item_artifact_duplicate")
        if type(item["payload"]) is not dict:
            _fail("work_item_payload_invalid")
        item_ids.append(item_id)
        targets.append((entity_type, entity_id))
        artifacts.extend(normalized)
        references += 3 + len(normalized)
    _unique(item_ids, reason="work_item_id_duplicate")
    if len(set(targets)) != len(targets):
        _fail("work_item_target_duplicate")
    _unique(artifacts, reason="work_list_artifact_duplicate")
    return set(item_ids), set(targets), references


def _validate_stages(value: Any) -> tuple[set[str], int]:
    stages = _exact_list(value, reason="stages_invalid")
    if not stages or len(stages) > MAX_STAGES:
        _fail("stage_count_invalid")
    stage_ids: list[str] = []
    references = 0
    for raw in stages:
        stage = _exact_keys(raw, _STAGE_KEYS, "stage_shape_invalid")
        stage_id = _identifier(stage["id"], path_safe=True)
        stage_ids.append(stage_id)
        _text(stage["label"], maximum=MAX_START_TEXT_BYTES)
        _identifier(stage["command_id"])
        produces = [
            _safe_artifact(item)
            for item in _exact_list(stage["produces"], reason="stage_produces_invalid")
        ]
        scopes = [
            _identifier(item)
            for item in _exact_list(
                stage["resource_scopes"], reason="stage_resource_scopes_invalid"
            )
        ]
        _unique(produces, reason="stage_produces_duplicate")
        _unique(scopes, reason="stage_resource_scope_duplicate")
        costs = stage["resource_costs"]
        if type(costs) is not dict or any(type(key) is not str for key in costs):
            _fail("stage_resource_costs_invalid")
        if not set(costs).issubset(scopes):
            _fail("stage_resource_costs_invalid")
        for scope, cost in costs.items():
            _identifier(scope)
            if type(cost) is not int or not 1 <= cost <= _MAX_SQLITE_INTEGER:
                _fail("stage_resource_costs_invalid")
        for name in ("retry_safe", "interrupt_safe", "pause_after"):
            if type(stage[name]) is not bool:
                _fail("stage_boolean_invalid")
        for name in ("input_ports", "output_ports"):
            ports = _exact_list(stage[name], reason="stage_ports_invalid")
            port_ids: list[str] = []
            for port in ports:
                references += _validate_port(port)
                port_ids.append(port["id"])
            _unique(port_ids, reason="stage_port_id_duplicate")
        references += 3 + len(produces) + len(scopes) + len(costs)
    _unique(stage_ids, reason="stage_id_duplicate")
    return set(stage_ids), references


def _validate_entity_policy(value: Any, *, command_id: str) -> int:
    if type(value) is not dict or frozenset(value) not in (
        _ENTITY_POLICY_REQUIRED_KEYS,
        _ENTITY_POLICY_KEYS,
    ):
        _fail("entity_policy_shape_invalid")
    policy = value
    if policy["schema"] != ENTITY_MUTATION_POLICY_SCHEMA:
        _fail("entity_policy_schema_invalid")
    lifecycle = policy["lifecycle"]
    if type(lifecycle) is not dict or frozenset(lifecycle) != _LIFECYCLE_SET:
        _fail("entity_policy_lifecycle_invalid")
    fields = policy["fields"]
    if type(fields) is not dict or any(type(key) is not str for key in fields):
        _fail("entity_policy_fields_invalid")
    references = 0
    for action in _LIFECYCLE:
        commands = _exact_list(
            lifecycle[action], reason="entity_policy_commands_invalid"
        )
        normalized = [_identifier(item) for item in commands]
        _unique(normalized, reason="entity_policy_command_duplicate")
        references += len(normalized)
    for field_name, commands in fields.items():
        _identifier(field_name)
        if field_name in _RESERVED_POLICY_FIELDS or "." in field_name:
            _fail("entity_policy_field_reserved")
        normalized = [
            _identifier(item)
            for item in _exact_list(commands, reason="entity_policy_commands_invalid")
        ]
        if not normalized:
            _fail("entity_policy_commands_invalid")
        _unique(normalized, reason="entity_policy_command_duplicate")
        references += 1 + len(normalized)
    if "cardinality" in policy:
        cardinality = policy["cardinality"]
        if (
            type(cardinality) is not dict
            or frozenset(cardinality) != frozenset(("max_total",))
            or type(cardinality["max_total"]) is not int
            or not 1 <= cardinality["max_total"] <= _MAX_SQLITE_INTEGER
        ):
            _fail("entity_policy_cardinality_invalid")
    if not any(command_id in lifecycle[action] for action in _LIFECYCLE) and not any(
        command_id in commands for commands in fields.values()
    ):
        _fail("entity_policy_root_command_absent")
    return references


def _validate_document_policy(
    value: Any,
    *,
    command_id: str,
) -> tuple[dict[str, dict[str, Any]], int]:
    policy = _exact_keys(value, _DOCUMENT_POLICY_KEYS, "document_policy_shape_invalid")
    if policy["schema"] != PIPELINE_DOCUMENT_POLICY_SCHEMA:
        _fail("document_policy_schema_invalid")
    entries = _exact_list(
        policy["entity_policies"], reason="document_policy_entities_invalid"
    )
    entity_types: list[str] = []
    by_type: dict[str, dict[str, Any]] = {}
    references = 0
    for raw in entries:
        entry = _exact_keys(
            raw, _DOCUMENT_POLICY_ENTITY_KEYS, "document_policy_entity_invalid"
        )
        entity_type = _identifier(entry["entity_type"])
        _hash(entry["entity_schema_digest"], reason="entity_schema_digest_invalid")
        references += 2 + _validate_entity_policy(
            entry["policy"], command_id=command_id
        )
        entity_types.append(entity_type)
        by_type[entity_type] = entry["policy"]
    _unique(
        entity_types, reason="document_policy_entity_duplicate", sorted_required=True
    )
    return by_type, references


def _validate_targets(
    value: Any,
    *,
    command_id: str,
    policies: dict[str, dict[str, Any]],
) -> tuple[set[tuple[str, str]], int]:
    targets = _exact_list(value, reason="delegation_targets_invalid")
    if not targets:
        _fail("delegation_targets_invalid")
    identities: list[tuple[str, str]] = []
    references = 0
    for raw in targets:
        target = _exact_keys(raw, _TARGET_KEYS, "delegation_target_shape_invalid")
        entity_type = _identifier(target["entity_type"])
        entity_id = _identifier(target["entity_id"])
        if any(character in entity_id for character in ("*", "?", "[", "]")):
            _fail("delegation_target_wildcard_invalid")
        fields = [
            _identifier(item)
            for item in _exact_list(
                target["fields"], reason="delegation_target_fields_invalid"
            )
        ]
        lifecycle = [
            _identifier(item)
            for item in _exact_list(
                target["lifecycle"], reason="delegation_target_lifecycle_invalid"
            )
        ]
        _unique(
            fields, reason="delegation_target_field_duplicate", sorted_required=True
        )
        _unique(
            lifecycle,
            reason="delegation_target_lifecycle_duplicate",
            sorted_required=True,
        )
        if not fields and not lifecycle:
            _fail("delegation_target_empty")
        if any(action not in _LIFECYCLE_SET for action in lifecycle):
            _fail("delegation_target_lifecycle_invalid")
        policy = policies.get(entity_type)
        if policy is None:
            _fail("delegation_target_policy_missing")
        if any(
            field_name not in policy["fields"]
            or command_id not in policy["fields"][field_name]
            for field_name in fields
        ) or any(command_id not in policy["lifecycle"][action] for action in lifecycle):
            _fail("delegation_target_not_authorized")
        identities.append((entity_type, entity_id))
        references += 2 + len(fields) + len(lifecycle)
    if len(set(identities)) != len(identities) or identities != sorted(identities):
        _fail("delegation_target_duplicate_or_unsorted")
    return set(identities), references


def _validate_creation_scopes(
    value: Any,
    *,
    command_id: str,
    policies: dict[str, dict[str, Any]],
) -> tuple[set[str], int]:
    scopes = _exact_list(value, reason="creation_scopes_invalid")
    entity_types: list[str] = []
    references = 0
    total_per_run = 0
    for raw in scopes:
        scope = _exact_keys(raw, _CREATION_SCOPE_KEYS, "creation_scope_shape_invalid")
        entity_type = _identifier(scope["entity_type"])
        if any(character in entity_type for character in ("*", "?", "[", "]")):
            _fail("creation_scope_wildcard_invalid")
        fields = [
            _identifier(item)
            for item in _exact_list(
                scope["fields"], reason="creation_scope_fields_invalid"
            )
        ]
        _unique(
            fields,
            reason="creation_scope_field_duplicate_or_unsorted",
            sorted_required=True,
        )
        max_per_operation = scope["max_per_operation"]
        max_per_run = scope["max_per_run"]
        if (
            type(max_per_operation) is not int
            or type(max_per_run) is not int
            or not 1 <= max_per_operation <= MAX_WORK_ITEMS
            or not 1 <= max_per_run <= MAX_WORK_ITEMS
            or max_per_operation > max_per_run
        ):
            _fail("creation_scope_limit_invalid")
        total_per_run += max_per_run
        if total_per_run > MAX_WORK_ITEMS:
            _fail("creation_scope_total_limit_exceeded")
        policy = policies.get(entity_type)
        if policy is None:
            _fail("creation_scope_policy_missing")
        if command_id not in policy["lifecycle"]["create"]:
            _fail("creation_scope_create_not_authorized")
        if any(
            field_name not in policy["fields"]
            or command_id not in policy["fields"][field_name]
            for field_name in fields
        ):
            _fail("creation_scope_field_not_authorized")
        entity_types.append(entity_type)
        # Bind the type, every writable field and both declared quantity axes
        # into the same semantic reference budget as exact targets.
        references += 3 + len(fields)
    if len(set(entity_types)) != len(entity_types) or entity_types != sorted(
        entity_types
    ):
        _fail("creation_scope_duplicate_or_unsorted")
    return set(entity_types), references


def _validate_start(value: dict[str, Any]) -> None:
    _exact_keys(value, _START_KEYS, "start_shape_invalid")
    if value["schema"] != PIPELINE_START_SCHEMA:
        _fail("start_schema_untrusted")
    document_id = _identifier(value["document_id"])
    del document_id
    run_id = _identifier(value["run_id"], path_safe=True)
    _identifier(value["pipeline_id"], path_safe=True)
    start_command_id = _identifier(value["start_command_id"])
    start_operation_id = _operation_id(value["start_operation_id"])
    item_ids, work_targets, references = _validate_work_list(
        value["work_list"], run_id=run_id
    )
    _stage_ids, stage_references = _validate_stages(value["stages"])
    references += stage_references

    initial_ids = [
        _operation_id(item)
        for item in _exact_list(
            value["initial_operation_ids"], reason="initial_operation_ids_invalid"
        )
    ]
    if (
        not initial_ids
        or len(initial_ids) > MAX_INITIAL_OPERATION_IDS
        or initial_ids[0] != start_operation_id
    ):
        _fail("initial_operation_ids_invalid")
    _unique(initial_ids, reason="initial_operation_id_duplicate")
    references += len(initial_ids)

    bindings = _exact_list(
        value["placeholder_bindings"], reason="placeholder_bindings_invalid"
    )
    item_order = {
        item["item_id"]: index for index, item in enumerate(value["work_list"]["items"])
    }
    previous_index = -1
    binding_items: list[str] = []
    binding_targets: list[tuple[str, str]] = []
    binding_operations: list[str] = []
    for raw in bindings:
        binding = _exact_keys(raw, _PLACEHOLDER_KEYS, "placeholder_binding_invalid")
        item_id = _identifier(binding["item_id"], path_safe=True)
        entity_type = _identifier(binding["entity_type"])
        entity_id = _identifier(binding["entity_id"])
        operation_id = _operation_id(binding["operation_id"])
        index = item_order.get(item_id)
        item = value["work_list"]["items"][index] if index is not None else None
        if (
            item is None
            or index is None
            or index <= previous_index
            or (entity_type, entity_id) != (item["entity_type"], item["entity_id"])
            or operation_id not in initial_ids[1:]
        ):
            _fail("placeholder_binding_mismatch")
        previous_index = index
        binding_items.append(item_id)
        binding_targets.append((entity_type, entity_id))
        binding_operations.append(operation_id)
        references += 4
    _unique(binding_items, reason="placeholder_binding_duplicate")
    if len(set(binding_targets)) != len(binding_targets):
        _fail("placeholder_binding_duplicate")
    _unique(binding_operations, reason="placeholder_binding_duplicate")
    if not set(binding_items).issubset(item_ids) or not set(binding_targets).issubset(
        work_targets
    ):
        _fail("placeholder_binding_mismatch")

    execution = _exact_keys(value["execution"], _EXECUTION_KEYS, "execution_invalid")
    concurrency = execution["concurrency"]
    max_retries = execution["max_retries"]
    if type(concurrency) is not int or not 1 <= concurrency <= MAX_CONCURRENCY:
        _fail("execution_concurrency_invalid")
    if type(max_retries) is not int or not 0 <= max_retries <= MAX_RETRIES:
        _fail("execution_retries_invalid")
    if type(execution["parameters"]) is not dict:
        _fail("execution_parameters_invalid")

    delegation = _exact_keys(
        value["delegation"], _DELEGATION_KEYS, "delegation_shape_invalid"
    )
    _hash(delegation["root_command_digest"], reason="root_command_digest_invalid")
    worker_actor = _actor(delegation["worker_actor"])
    worker_source = delegation["worker_source"]
    if type(worker_source) is not str or worker_source not in _SOURCES:
        _fail("worker_source_invalid")
    # Actor.source and dispatch/Operation.source are independent authority axes.
    del worker_actor
    policies, policy_references = _validate_document_policy(
        delegation["document_policy"], command_id=start_command_id
    )
    references += policy_references
    policy_digest = _pipeline_document_policy_digest(
        delegation["document_policy"]
    )
    supplied_policy_digest = _hash(
        delegation["document_policy_digest"], reason="document_policy_digest_invalid"
    )
    if not hmac.compare_digest(policy_digest, supplied_policy_digest):
        _fail("document_policy_digest_mismatch")
    targets, target_references = _validate_targets(
        delegation["targets"], command_id=start_command_id, policies=policies
    )
    if targets != work_targets:
        _fail("delegation_work_target_mismatch")
    references += target_references
    creation_types, creation_references = _validate_creation_scopes(
        delegation["creation_scopes"],
        command_id=start_command_id,
        policies=policies,
    )
    target_types = {entity_type for entity_type, _entity_id in targets}
    if set(policies) != target_types | creation_types:
        _fail("document_policy_scope_mismatch")
    references += creation_references
    if references > MAX_REFERENCES:
        _fail("reference_budget_exceeded")


def _validate_control_parameters(action: str, parameters: Any) -> None:
    if type(parameters) is not dict:
        _fail("control_parameters_invalid")
    if action in ("pause", "interrupt", "cancel", "discard", "resume"):
        expected = frozenset()
    elif action == "retry_failed":
        expected = frozenset(("item_ids",))
    elif action in ("rerun_step", "run_from_step"):
        expected = frozenset(("stage_id", "item_id"))
    elif action == "set_breakpoint":
        expected = frozenset(("stage_id", "halt"))
    else:
        _fail("control_action_invalid")
        return
    if frozenset(parameters) != expected:
        _fail("control_parameters_invalid")
    if action == "retry_failed":
        item_ids = [
            _identifier(item, path_safe=True)
            for item in _exact_list(
                parameters["item_ids"], reason="control_item_ids_invalid"
            )
        ]
        if not item_ids or len(item_ids) > MAX_CONTROL_ITEM_IDS:
            _fail("control_item_count_exceeded")
        _unique(item_ids, reason="control_item_id_duplicate", sorted_required=True)
    elif action in ("rerun_step", "run_from_step"):
        _identifier(parameters["stage_id"], path_safe=True)
        if parameters["item_id"] is not None:
            _identifier(parameters["item_id"], path_safe=True)
    elif action == "set_breakpoint":
        _identifier(parameters["stage_id"], path_safe=True)
        if type(parameters["halt"]) is not bool:
            _fail("control_halt_invalid")


def _validate_control(value: dict[str, Any]) -> None:
    _exact_keys(value, _CONTROL_KEYS, "control_shape_invalid")
    if value["schema"] != PIPELINE_CONTROL_SCHEMA:
        _fail("control_schema_untrusted")
    _identifier(value["document_id"])
    _identifier(value["run_id"], path_safe=True)
    _identifier(value["pipeline_id"], path_safe=True)
    _operation_id(value["control_operation_id"])
    _operation_id(value["start_operation_id"])
    _hash(value["start_operation_hash"], reason="start_operation_hash_invalid")
    action = value["action"]
    if type(action) is not str or action not in _ACTIONS:
        _fail("control_action_invalid")
    _validate_control_parameters(action, value["parameters"])
    expected_generation = value["expected_generation"]
    generation = value["generation"]
    if (
        type(expected_generation) is not int
        or expected_generation < 0
        or expected_generation >= _MAX_SQLITE_INTEGER
        or type(generation) is not int
        or generation != expected_generation + 1
    ):
        _fail("control_generation_invalid")
    if (
        type(value["expected_state"]) is not str
        or value["expected_state"] not in _STATES
    ):
        _fail("control_state_invalid")


def _parse_owned(value: Any, *, start: bool) -> tuple[dict[str, Any], bytes, bytes]:
    maximum_bytes = MAX_START_BYTES if start else MAX_CONTROL_BYTES
    maximum_depth = MAX_START_DEPTH if start else MAX_CONTROL_DEPTH
    maximum_nodes = MAX_START_NODES if start else MAX_CONTROL_NODES
    maximum_text_bytes = MAX_START_TEXT_BYTES if start else MAX_CONTROL_TEXT_BYTES
    owned = _own_json(
        value,
        maximum_bytes=maximum_bytes,
        maximum_depth=maximum_depth,
        maximum_nodes=maximum_nodes,
        maximum_text_bytes=maximum_text_bytes,
    )
    if type(owned) is not dict:
        _fail("descriptor_shape_invalid")
    encoded = _canonical_bytes(owned, maximum=maximum_bytes)
    # Parse the owned bytes again so validation never retains aliases to input.
    decoded = _decode_owned(
        encoded,
        maximum=maximum_bytes,
        maximum_depth=maximum_depth,
        maximum_nodes=maximum_nodes,
        maximum_text_bytes=maximum_text_bytes,
    )
    if type(decoded) is not dict:
        _fail("descriptor_shape_invalid")
    if start:
        _validate_start(decoded)
    else:
        _validate_control(decoded)
    canonical = _canonical_bytes(decoded, maximum=maximum_bytes)
    if canonical != encoded:
        _fail("descriptor_not_canonical")
    return decoded, canonical, hashlib.sha256(canonical).digest()


def parse_pipeline_start(value: Any) -> FrozenPipelineStart:
    decoded, encoded, digest = _parse_owned(value, start=True)
    return FrozenPipelineStart(
        protocol=PIPELINE_AUTHORITY_CODEC_PROTOCOL,
        schema=PIPELINE_START_SCHEMA,
        document_id=decoded["document_id"],
        run_id=decoded["run_id"],
        pipeline_id=decoded["pipeline_id"],
        start_command_id=decoded["start_command_id"],
        start_operation_id=decoded["start_operation_id"],
        initial_operation_ids=tuple(decoded["initial_operation_ids"]),
        descriptor_bytes=encoded,
        descriptor_digest=digest,
    )


def parse_pipeline_control(value: Any) -> FrozenPipelineControl:
    decoded, encoded, digest = _parse_owned(value, start=False)
    parameters_bytes = _canonical_bytes(
        decoded["parameters"], maximum=MAX_CONTROL_BYTES
    )
    return FrozenPipelineControl(
        protocol=PIPELINE_AUTHORITY_CODEC_PROTOCOL,
        schema=PIPELINE_CONTROL_SCHEMA,
        document_id=decoded["document_id"],
        run_id=decoded["run_id"],
        pipeline_id=decoded["pipeline_id"],
        control_operation_id=decoded["control_operation_id"],
        start_operation_id=decoded["start_operation_id"],
        start_operation_hash=decoded["start_operation_hash"],
        action=decoded["action"],
        expected_generation=decoded["expected_generation"],
        generation=decoded["generation"],
        expected_state=decoded["expected_state"],
        parameters_bytes=parameters_bytes,
        descriptor_bytes=encoded,
        descriptor_digest=digest,
    )


def _frozen_projection(
    value: FrozenPipelineStart | FrozenPipelineControl,
    *,
    start: bool,
) -> tuple[Any, ...]:
    capture_failed = False
    try:
        if start:
            projection = (
                value.protocol,
                value.schema,
                value.document_id,
                value.run_id,
                value.pipeline_id,
                value.start_command_id,
                value.start_operation_id,
                value.initial_operation_ids,
                value.descriptor_bytes,
                value.descriptor_digest,
            )
        else:
            projection = (
                value.protocol,
                value.schema,
                value.document_id,
                value.run_id,
                value.pipeline_id,
                value.control_operation_id,
                value.start_operation_id,
                value.start_operation_hash,
                value.action,
                value.expected_generation,
                value.generation,
                value.expected_state,
                value.parameters_bytes,
                value.descriptor_bytes,
                value.descriptor_digest,
            )
    except AttributeError:
        capture_failed = True
        projection = ()
    if capture_failed:
        _fail("frozen_descriptor_invalid")
    return projection


def materialize_pipeline_descriptor(
    value: FrozenPipelineStart | FrozenPipelineControl,
) -> dict[str, Any]:
    if type(value) is FrozenPipelineStart:
        start = True
    elif type(value) is FrozenPipelineControl:
        start = False
    else:
        _fail("frozen_descriptor_type_invalid")
    maximum_bytes = MAX_START_BYTES if start else MAX_CONTROL_BYTES
    maximum_depth = MAX_START_DEPTH if start else MAX_CONTROL_DEPTH
    maximum_nodes = MAX_START_NODES if start else MAX_CONTROL_NODES
    maximum_text_bytes = MAX_START_TEXT_BYTES if start else MAX_CONTROL_TEXT_BYTES
    projection = _frozen_projection(value, start=start)
    descriptor_bytes = projection[-2]
    descriptor_digest = projection[-1]
    if (
        type(projection[0]) is not str
        or projection[0] != PIPELINE_AUTHORITY_CODEC_PROTOCOL
        or type(descriptor_bytes) is not bytes
        or len(descriptor_bytes) > maximum_bytes
        or type(descriptor_digest) is not bytes
        or len(descriptor_digest) != 32
    ):
        _fail("frozen_descriptor_invalid")
    actual_digest = hashlib.sha256(descriptor_bytes).digest()
    if not hmac.compare_digest(actual_digest, descriptor_digest):
        _fail("frozen_descriptor_invalid")
    decoded = _decode_owned(
        descriptor_bytes,
        maximum=maximum_bytes,
        maximum_depth=maximum_depth,
        maximum_nodes=maximum_nodes,
        maximum_text_bytes=maximum_text_bytes,
    )
    rebuilt = (
        parse_pipeline_start(decoded) if start else parse_pipeline_control(decoded)
    )
    rebuilt_projection = _frozen_projection(rebuilt, start=start)
    if not _exact_value_equal(rebuilt_projection, projection):
        _fail("frozen_descriptor_changed")
    if type(decoded) is not dict:
        _fail("frozen_descriptor_invalid")
    return decoded


def build_pipeline_start_descriptor(
    *,
    document_id: str,
    run_id: str,
    pipeline_id: str,
    start_command_id: str,
    start_operation_id: str,
    work_list: dict[str, Any],
    stages: list[dict[str, Any]],
    placeholder_bindings: list[dict[str, Any]],
    initial_operation_ids: list[str],
    concurrency: int,
    max_retries: int,
    parameters: dict[str, Any],
    root_command_digest: str,
    document_policy: dict[str, Any],
    worker_actor: dict[str, Any],
    worker_source: str,
    targets: list[dict[str, Any]],
    creation_scopes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build one detached dormant descriptor; this does not make it writable."""

    owned_policy = _own_json(
        document_policy,
        maximum_bytes=MAX_START_BYTES,
        maximum_depth=MAX_START_DEPTH,
        maximum_nodes=MAX_START_NODES,
        maximum_text_bytes=MAX_START_TEXT_BYTES,
    )
    policy_digest = _pipeline_document_policy_digest(owned_policy)
    candidate = {
        "schema": PIPELINE_START_SCHEMA,
        "document_id": document_id,
        "run_id": run_id,
        "pipeline_id": pipeline_id,
        "start_command_id": start_command_id,
        "start_operation_id": start_operation_id,
        "work_list": work_list,
        "stages": stages,
        "placeholder_bindings": placeholder_bindings,
        "initial_operation_ids": initial_operation_ids,
        "execution": {
            "concurrency": concurrency,
            "max_retries": max_retries,
            "parameters": parameters,
        },
        "delegation": {
            "root_command_digest": root_command_digest,
            "document_policy": owned_policy,
            "document_policy_digest": policy_digest,
            "worker_actor": worker_actor,
            "worker_source": worker_source,
            "targets": targets,
            "creation_scopes": [] if creation_scopes is None else creation_scopes,
        },
    }
    return materialize_pipeline_descriptor(parse_pipeline_start(candidate))


def build_pipeline_control_descriptor(
    *,
    document_id: str,
    run_id: str,
    pipeline_id: str,
    control_operation_id: str,
    start_operation_id: str,
    start_operation_hash: str,
    action: str,
    parameters: dict[str, Any],
    expected_generation: int,
    expected_state: str,
) -> dict[str, Any]:
    """Build one detached dormant Control; this does not make it writable."""

    candidate = {
        "schema": PIPELINE_CONTROL_SCHEMA,
        "document_id": document_id,
        "run_id": run_id,
        "pipeline_id": pipeline_id,
        "control_operation_id": control_operation_id,
        "start_operation_id": start_operation_id,
        "start_operation_hash": start_operation_hash,
        "action": action,
        "parameters": parameters,
        "expected_generation": expected_generation,
        "generation": (
            expected_generation + 1
            if type(expected_generation) is int
            else expected_generation
        ),
        "expected_state": expected_state,
    }
    return materialize_pipeline_descriptor(parse_pipeline_control(candidate))


__all__ = (
    "ENTITY_MUTATION_POLICY_SCHEMA",
    "FrozenPipelineControl",
    "FrozenPipelineStart",
    "PIPELINE_AUTHORITY_CODEC_PROTOCOL",
    "PIPELINE_CONTROL_SCHEMA",
    "PIPELINE_DOCUMENT_POLICY_SCHEMA",
    "PIPELINE_START_SCHEMA",
    "PipelineAuthorityCodecError",
    "build_pipeline_control_descriptor",
    "build_pipeline_start_descriptor",
    "materialize_pipeline_descriptor",
    "parse_pipeline_control",
    "parse_pipeline_start",
)
