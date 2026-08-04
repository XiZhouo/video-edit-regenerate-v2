"""Pure canonical codec for durable Operation records and hash-chain entries.

This leaf owns the byte algorithm that used to live inside ``OpLog``.  It has
no dependency on Operation, OpLog, Document, SQLite, the SDK, clocks or I/O.
The legacy wire format is intentionally preserved byte-for-byte::

    sha256(prev_hash + json.dumps(body, ensure_ascii=False,
                                  sort_keys=True, allow_nan=False))

The public persistence contract consumes :class:`FrozenOperationRecord`, not a
caller-owned mapping.  ``OpLog`` retains a small compatibility wrapper around
the same functions so historical NDJSON records and future transaction rows
cannot silently acquire different canonicalization rules.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import math
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Tuple


OPERATION_RECORD_PROTOCOL = "ze.operation-record.v1"
OPERATION_CHAIN_PROTOCOL = "ze.operation-hash-chain.v1"
ZERO_OPERATION_HASH = "0" * 64

MAX_OPERATION_RECORD_BYTES = 16 * 1024 * 1024
MAX_OPERATION_JSON_DEPTH = 128
MAX_OPERATION_INTEGER_DIGITS = 4300
# Compatibility budgets are derived from the historical 16 MiB record cap.
# They therefore bound work without rejecting a body that the old OpLog byte
# and depth contract could represent (for example a 2 MiB string or >250k
# tiny scalar values).
MAX_OPERATION_VALUE_NODES = MAX_OPERATION_RECORD_BYTES + 1
MAX_OPERATION_TEXT_BYTES = MAX_OPERATION_RECORD_BYTES
MAX_OPERATION_INDEXED_ID_BYTES = 1024
# ``business_operation_targets.ordinal`` is a zero-based six-digit domain.
# Keep this projection separate from legacy OpLog freezing so the historical
# 16 MiB file format remains compatible while atomic SQL commits fail closed.
MAX_OPERATION_TARGETS_PER_RECORD = 1_000_000
# Last whole UTC second representable by the schema's four-digit canonical
# ``YYYY-MM-DDTHH:MM:SS.mmmZ`` timestamp contract.
MAX_OPERATION_TIMESTAMP_SECONDS = 253_402_300_799

_MAX_INTEGER_MAGNITUDE = 10 ** MAX_OPERATION_INTEGER_DIGITS
_MAX_PATCH_POSITION = 2**63 - 1
_LOWER_HEX = frozenset("0123456789abcdef")
_SOURCES = frozenset(("user", "ai", "sync", "system"))
_KINDS = frozenset(("native", "stage", "compose", "rollback"))
_PATCH_OPS = frozenset(("set", "delete", "create", "soft_delete", "restore", "name"))
_OPERATION_KEYS = frozenset(
    (
        "id",
        "command_id",
        "source",
        "actor",
        "ts",
        "patches",
        "reverse_patches",
        "group_id",
        "trace_id",
        "kind",
        "error",
    )
)
_OPTIONAL_OPERATION_KEYS = frozenset(
    ("document_id", "session_id", "external", "pipeline")
)
_PATCH_KEYS = frozenset(("entity_type", "entity_id", "field", "op", "before", "after"))
_OPTIONAL_PATCH_KEYS = frozenset(("provenance", "position", "ancestry"))
_PATCH_PROVENANCE_KEYS = frozenset(("field", "before", "after"))
_PATCH_PROVENANCE_STATE_KEYS = frozenset(("present", "value"))
_PATCH_ANCESTRY_KEYS = frozenset(("field", "before", "after"))

# Pipeline authority metadata is part of the durable Operation body, but it
# has a substantially tighter resource envelope than the legacy 16 MiB
# Operation record.  Keep these limits private: callers may not relax the
# authority boundary through configuration.
_PIPELINE_START_SCHEMA = "ze.pipeline-start.v1"
_PIPELINE_CONTROL_SCHEMA = "ze.pipeline-control.v1"
_PIPELINE_START_AUTHORITY_SCHEMA = "ze.pipeline-start.v2"
_PIPELINE_CONTROL_AUTHORITY_SCHEMA = "ze.pipeline-control.v2"
_PIPELINE_RESOURCE_PROFILES = {
    _PIPELINE_START_SCHEMA: (4 * 1024 * 1024, 32, 100_000, 256 * 1024),
    _PIPELINE_CONTROL_SCHEMA: (64 * 1024, 12, 2_048, 16 * 1024),
    # v2 is writable only through the exact-object sealed Operation factory in
    # ``pipeline_authority_commit``.  The durable codec must nevertheless know
    # its resource profile so the factory, OpLog writer, cold reader, and
    # in-memory verifier all encode the same bytes.
    _PIPELINE_START_AUTHORITY_SCHEMA: (
        4 * 1024 * 1024,
        32,
        100_000,
        256 * 1024,
    ),
    _PIPELINE_CONTROL_AUTHORITY_SCHEMA: (
        2_179_072,
        12,
        4_123,
        16 * 1024,
    ),
}


class OperationRecordCodecError(ValueError):
    """Stable, payload-free failure at the operation byte boundary."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        if type(code) is not str or not code:
            raise TypeError("code must be a non-empty exact string")
        self.code = code
        super().__init__(code)


class _DecodedObject:
    __slots__ = ("pairs",)

    def __init__(self, pairs: List[Tuple[str, Any]]) -> None:
        self.pairs = tuple(pairs)


class _NonFinite:
    __slots__ = ()


class _IntegerTooLarge:
    __slots__ = ()


_NON_FINITE = _NonFinite()
_INTEGER_TOO_LARGE = _IntegerTooLarge()


@dataclass(frozen=True)
class FrozenOperationBody:
    """One detached legacy-compatible canonical Operation body."""

    protocol: str
    body_bytes: bytes = field(repr=False)
    body_sha256: bytes = field(repr=False)


@dataclass(frozen=True)
class FrozenOperationRecord:
    """Semantically checked Operation payload accepted by an atomic plan."""

    protocol: str
    operation_id: str
    command_id: str
    source: str
    trace_id: str
    document_id: str
    body_bytes: bytes = field(repr=False)
    body_sha256: bytes = field(repr=False)


@dataclass(frozen=True)
class FrozenOperationChainEntry:
    """One immutable NDJSON-compatible chain entry."""

    protocol: str
    operation_id: str
    ordinal: int
    prev_hash: str
    self_hash: str
    record_sha256: bytes = field(repr=False)
    entry_bytes: bytes = field(repr=False)


@dataclass(frozen=True)
class FrozenOperationTarget:
    """One forward-patch target ready for ``business_operation_targets``."""

    operation_id: str
    ordinal: int
    entity_type: str
    entity_id: str
    field: str
    patch_kind: str


def _fail(code: str) -> None:
    raise OperationRecordCodecError(code)


def _parse_int(raw: str) -> Any:
    digits = len(raw) - (1 if raw.startswith("-") else 0)
    if digits > MAX_OPERATION_INTEGER_DIGITS:
        return _INTEGER_TOO_LARGE
    try:
        return int(raw)
    except (ValueError, OverflowError):
        return _INTEGER_TOO_LARGE


def _parse_float(raw: str) -> Any:
    try:
        value = float(raw)
    except (ValueError, OverflowError):
        return _NON_FINITE
    return value if math.isfinite(value) else _NON_FINITE


def _parse_constant(_raw: str) -> Any:
    return _NON_FINITE


def _normalize_decoded(value: Any, *, depth: int, nodes: List[int]) -> Any:
    nodes[0] += 1
    if nodes[0] > MAX_OPERATION_VALUE_NODES:
        _fail("operation_record_node_budget_exceeded")
    if value is None or type(value) in (bool, str):
        if type(value) is str:
            invalid_utf8 = False
            try:
                encoded = value.encode("utf-8", "strict")
            except UnicodeEncodeError:
                invalid_utf8 = True
                encoded = b""
            if invalid_utf8:
                _fail("operation_record_utf8_invalid")
            if len(encoded) > MAX_OPERATION_TEXT_BYTES:
                _fail("operation_record_text_budget_exceeded")
        return value
    if type(value) is int:
        if value >= _MAX_INTEGER_MAGNITUDE or value <= -_MAX_INTEGER_MAGNITUDE:
            _fail("operation_record_integer_too_large")
        return value
    if type(value) is float:
        if not math.isfinite(value):
            _fail("operation_record_number_non_finite")
        return value
    if value is _INTEGER_TOO_LARGE:
        _fail("operation_record_integer_too_large")
    if value is _NON_FINITE:
        _fail("operation_record_number_non_finite")
    if depth >= MAX_OPERATION_JSON_DEPTH:
        _fail("operation_record_depth_exceeded")
    if type(value) is _DecodedObject:
        output: Dict[str, Any] = {}
        for key, child in value.pairs:
            if type(key) is not str or key in output:
                _fail("operation_record_object_invalid")
            output[key] = _normalize_decoded(child, depth=depth + 1, nodes=nodes)
        return output
    if type(value) is list:
        return [
            _normalize_decoded(child, depth=depth + 1, nodes=nodes)
            for child in value
        ]
    _fail("operation_record_json_invalid")
    raise AssertionError("unreachable")


def decode_strict_json(raw: bytes, *, maximum_bytes: int = MAX_OPERATION_RECORD_BYTES) -> Any:
    """Decode exact UTF-8 JSON with duplicate/depth/value/number budgets."""
    if (
        type(raw) is not bytes
        or type(maximum_bytes) is not int
        or maximum_bytes < 1
        or len(raw) > maximum_bytes
    ):
        _fail("operation_record_byte_budget_exceeded")
    decode_failed = False
    try:
        decoded = json.loads(
            raw.decode("utf-8", "strict"),
            object_pairs_hook=_DecodedObject,
            parse_int=_parse_int,
            parse_float=_parse_float,
            parse_constant=_parse_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, OverflowError, RecursionError):
        decode_failed = True
        decoded = None
    if decode_failed:
        _fail("operation_record_json_invalid")
    return _normalize_decoded(decoded, depth=0, nodes=[0])


def validate_json_graph(value: Any) -> None:
    """Preflight a caller graph before any potentially unbounded serialization."""
    stack: List[Tuple[Any, int, bool]] = [(value, 1, False)]
    active = set()
    nodes = 0
    while stack:
        current, depth, leaving = stack.pop()
        if leaving:
            active.remove(id(current))
            continue
        nodes += 1
        if nodes > MAX_OPERATION_VALUE_NODES:
            _fail("operation_record_node_budget_exceeded")
        if type(current) in (dict, list, tuple):
            if depth > MAX_OPERATION_JSON_DEPTH:
                _fail("operation_record_depth_exceeded")
            identity = id(current)
            if identity in active:
                _fail("operation_record_cycle")
            active.add(identity)
            stack.append((current, depth, True))
            if type(current) is dict:
                for key, child in current.items():
                    if type(key) not in (str, int, float, bool) and key is not None:
                        _fail("operation_record_key_invalid")
                    stack.append((key, depth + 1, False))
                    stack.append((child, depth + 1, False))
            else:
                stack.extend((child, depth + 1, False) for child in current)
            continue
        if type(current) is int:
            if current >= _MAX_INTEGER_MAGNITUDE or current <= -_MAX_INTEGER_MAGNITUDE:
                _fail("operation_record_integer_too_large")
        elif type(current) is float:
            if not math.isfinite(current):
                _fail("operation_record_number_non_finite")
        elif type(current) is str:
            invalid_utf8 = False
            try:
                encoded = current.encode("utf-8", "strict")
            except UnicodeEncodeError:
                invalid_utf8 = True
                encoded = b""
            if invalid_utf8:
                _fail("operation_record_utf8_invalid")
            if len(encoded) > MAX_OPERATION_TEXT_BYTES:
                _fail("operation_record_text_budget_exceeded")
        elif current is not None and type(current) is not bool:
            _fail("operation_record_json_invalid")


def _json_bytes(
    value: Any,
    *,
    json_dumps: Callable[..., str],
) -> bytes:
    failure = ""
    try:
        rendered = json_dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
        )
        if type(rendered) is not str:
            _fail("operation_record_encoder_invalid")
        return rendered.encode("utf-8", "strict")
    except OperationRecordCodecError:
        raise
    except (TypeError, ValueError, OverflowError, UnicodeError, RecursionError, RuntimeError):
        failure = "operation_record_encoding_failed"
    _fail(failure)
    raise AssertionError("unreachable")


def _own_pipeline_json_graph(
    value: Any,
    *,
    maximum_bytes: int,
    maximum_depth: int,
    maximum_nodes: int,
    maximum_text_bytes: int,
) -> Any:
    """Boundedly own an exact JSON graph while computing its encoded budget.

    Encoding only consumes the returned graph, never the caller-owned input.
    Concurrent mutation can therefore invalidate or influence the snapshot it
    offers, but cannot grow the later full JSON allocation beyond these limits
    or retain an alias into the durable Operation.
    """
    active = set()
    nodes = 0
    encoded_bytes = 0

    def add_encoded_bytes(amount: int) -> None:
        nonlocal encoded_bytes
        encoded_bytes += amount
        if encoded_bytes > maximum_bytes:
            _fail("operation_record_pipeline_byte_budget_exceeded")

    def own_string(value: str) -> str:
        # Count raw UTF-8 and escaped JSON bytes incrementally.  Encoding the
        # whole caller string before checking the 16/256 KiB text budget would
        # itself allocate attacker-sized memory.  Exact code-point accounting
        # matches UTF-8 strict + json.dumps(ensure_ascii=False).
        text_bytes = 0
        add_encoded_bytes(2)  # opening and closing quote
        for character in value:
            codepoint = ord(character)
            if codepoint <= 0x7F:
                raw_bytes = 1
            elif codepoint <= 0x7FF:
                raw_bytes = 2
            elif 0xD800 <= codepoint <= 0xDFFF:
                _fail("operation_record_pipeline_invalid")
            elif codepoint <= 0xFFFF:
                raw_bytes = 3
            else:
                raw_bytes = 4
            text_bytes += raw_bytes
            if text_bytes > maximum_text_bytes:
                _fail("operation_record_pipeline_text_budget_exceeded")
            if character in {'"', "\\", "\b", "\f", "\n", "\r", "\t"}:
                add_encoded_bytes(2)
            elif codepoint < 0x20:
                add_encoded_bytes(6)
            else:
                add_encoded_bytes(raw_bytes)
        return value

    def own(current: Any, depth: int) -> Any:
        nonlocal nodes
        nodes += 1
        if nodes > maximum_nodes:
            _fail("operation_record_pipeline_node_budget_exceeded")
        if depth > maximum_depth:
            _fail("operation_record_pipeline_depth_exceeded")
        if type(current) is dict:
            identity = id(current)
            if identity in active:
                _fail("operation_record_pipeline_invalid")
            active.add(identity)
            output: Dict[str, Any] = {}
            add_encoded_bytes(2)
            try:
                count = 0
                for key, child in current.items():
                    if type(key) is not str:
                        _fail("operation_record_pipeline_invalid")
                    if count:
                        add_encoded_bytes(2)  # legacy `, ` separator
                    add_encoded_bytes(2)  # legacy `: ` separator
                    owned_key = own(key, depth + 1)
                    output[owned_key] = own(child, depth + 1)
                    count += 1
            except RuntimeError:
                _fail("operation_record_pipeline_invalid")
            finally:
                active.remove(identity)
            return output
        if type(current) is list:
            identity = id(current)
            if identity in active:
                _fail("operation_record_pipeline_invalid")
            active.add(identity)
            output_list: List[Any] = []
            add_encoded_bytes(2)
            try:
                for index, child in enumerate(current):
                    if index:
                        add_encoded_bytes(2)  # legacy `, ` separator
                    output_list.append(own(child, depth + 1))
            except RuntimeError:
                _fail("operation_record_pipeline_invalid")
            finally:
                active.remove(identity)
            return output_list
        if type(current) is int:
            if current >= _MAX_INTEGER_MAGNITUDE or current <= -_MAX_INTEGER_MAGNITUDE:
                _fail("operation_record_pipeline_invalid")
            add_encoded_bytes(len(str(current)))
            return current
        elif type(current) is float:
            if not math.isfinite(current):
                _fail("operation_record_pipeline_invalid")
            add_encoded_bytes(len(repr(current)))
            return current
        elif type(current) is str:
            return own_string(current)
        elif current is None:
            add_encoded_bytes(4)
            return None
        elif type(current) is bool:
            add_encoded_bytes(4 if current else 5)
            return current
        _fail("operation_record_pipeline_invalid")
        raise AssertionError("unreachable")

    return own(value, 1)


def _pipeline_operation_schema(value: Any) -> str | None:
    """Read an exact-string ``schema`` member without invoking key hooks."""

    if type(value) is not dict:
        return None
    schema = None
    try:
        for key, member in value.items():
            if type(key) is not str:
                _fail("operation_record_pipeline_invalid")
            if key == "schema":
                schema = member
    except RuntimeError:
        _fail("operation_record_pipeline_invalid")
    return schema if type(schema) is str else None


def _normalize_pipeline_operation_metadata(value: Any) -> Dict[str, Any]:
    """Return detached canonical Pipeline metadata or fail payload-free.

    Batch 1 intentionally validates only the carrier schema identity and
    resource envelope.  Start/Control field semantics and sealed issuance are
    owned by the Pipeline authority codec added in the following batch.
    """
    if type(value) is not dict:
        _fail("operation_record_pipeline_invalid")
    schema = _pipeline_operation_schema(value)
    if schema not in _PIPELINE_RESOURCE_PROFILES:
        _fail("operation_record_pipeline_schema_invalid")
    maximum_bytes, maximum_depth, maximum_nodes, maximum_text_bytes = (
        _PIPELINE_RESOURCE_PROFILES[schema]
    )
    owned = _own_pipeline_json_graph(
        value,
        maximum_bytes=maximum_bytes,
        maximum_depth=maximum_depth,
        maximum_nodes=maximum_nodes,
        maximum_text_bytes=maximum_text_bytes,
    )
    if type(owned) is not dict or owned.get("schema") != schema:
        _fail("operation_record_pipeline_schema_invalid")
    encoded = _json_bytes(owned, json_dumps=json.dumps)
    if len(encoded) > maximum_bytes:
        _fail("operation_record_pipeline_byte_budget_exceeded")
    decoded = decode_strict_json(encoded, maximum_bytes=maximum_bytes)
    if type(decoded) is not dict:
        _fail("operation_record_pipeline_invalid")
    if decoded.get("schema") != schema:
        _fail("operation_record_pipeline_schema_invalid")
    normalized = _own_pipeline_json_graph(
        decoded,
        maximum_bytes=maximum_bytes,
        maximum_depth=maximum_depth,
        maximum_nodes=maximum_nodes,
        maximum_text_bytes=maximum_text_bytes,
    )
    if type(normalized) is not dict or normalized.get("schema") != schema:
        _fail("operation_record_pipeline_schema_invalid")
    if _json_bytes(normalized, json_dumps=json.dumps) != encoded:
        _fail("operation_record_pipeline_not_canonical")
    return normalized


def _validate_pipeline_operation_metadata(body: Any) -> None:
    """Validate only the optional Pipeline carrier on an Operation body.

    Durable legacy history predates the full semantic Operation validator, so
    cold readers must not retroactively apply ``freeze_operation_record`` to
    every historical row.  This narrow seam gives new Pipeline metadata the
    same schema/resource checks on write, reload, and in-memory chain verify
    while leaving pipeline-free legacy bodies byte-for-byte compatible.
    """
    if type(body) is not dict:
        _fail("operation_record_body_invalid")
    if "pipeline" in body:
        _normalize_pipeline_operation_metadata(body["pipeline"])


def _freeze_operation_body_with_pipeline(
    body: Any,
    *,
    maximum_bytes: int,
    json_dumps: Callable[..., str],
) -> FrozenOperationBody:
    """Own one Operation body with a pre-budgeted Pipeline carrier.

    The returned canonical bytes are the only source used by later hashing and
    line encoding.  A raw mapping can therefore never change between carrier
    validation, ``self_hash`` calculation, and the persisted entry.
    """
    if type(body) is not dict:
        _fail("operation_record_body_invalid")
    try:
        frozen_input = dict(body)
    except RuntimeError:
        _fail("operation_record_copy_failed")
    if "pipeline" in frozen_input:
        frozen_input["pipeline"] = _normalize_pipeline_operation_metadata(
            frozen_input["pipeline"]
        )
    return freeze_operation_body(
        frozen_input,
        maximum_bytes=maximum_bytes,
        json_dumps=json_dumps,
    )


def freeze_operation_body(
    body: Any,
    *,
    maximum_bytes: int = MAX_OPERATION_RECORD_BYTES,
    json_dumps: Callable[..., str] = json.dumps,
) -> FrozenOperationBody:
    """Detach a caller body once, then publish canonical immutable bytes."""
    if type(body) is not dict:
        _fail("operation_record_body_invalid")
    copy_failed = False
    try:
        detached = copy.deepcopy(body)
    except (TypeError, ValueError, OverflowError, RecursionError, RuntimeError):
        copy_failed = True
        detached = None
    if copy_failed:
        _fail("operation_record_copy_failed")
    validate_json_graph(
        {"op": detached, "prev_hash": ZERO_OPERATION_HASH, "self_hash": ZERO_OPERATION_HASH}
    )
    encoded = _json_bytes(detached, json_dumps=json_dumps)
    if len(encoded) > maximum_bytes:
        _fail("operation_record_byte_budget_exceeded")
    frozen = decode_strict_json(encoded, maximum_bytes=maximum_bytes)
    if type(frozen) is not dict:
        _fail("operation_record_body_invalid")
    # Re-encode the owned graph.  This rejects lossy key coercions such as
    # ``{1: ...}`` instead of allowing the transaction representation to differ
    # from the object the caller believed it submitted.
    canonical = _json_bytes(frozen, json_dumps=json_dumps)
    if canonical != encoded:
        _fail("operation_record_not_canonical")
    return FrozenOperationBody(
        protocol=OPERATION_RECORD_PROTOCOL,
        body_bytes=canonical,
        body_sha256=hashlib.sha256(canonical).digest(),
    )


def materialize_operation_body(value: FrozenOperationBody) -> Dict[str, Any]:
    if type(value) is not FrozenOperationBody or value.protocol != OPERATION_RECORD_PROTOCOL:
        _fail("operation_record_frozen_body_invalid")
    if (
        type(value.body_bytes) is not bytes
        or type(value.body_sha256) is not bytes
        or len(value.body_sha256) != 32
        or not hmac.compare_digest(hashlib.sha256(value.body_bytes).digest(), value.body_sha256)
    ):
        _fail("operation_record_frozen_body_invalid")
    decoded = decode_strict_json(value.body_bytes)
    if type(decoded) is not dict:
        _fail("operation_record_frozen_body_invalid")
    return decoded


def _text(value: Any, *, nonempty: bool = False) -> str:
    if type(value) is not str or (nonempty and not value):
        _fail("operation_record_shape_invalid")
    invalid_utf8 = False
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeEncodeError:
        invalid_utf8 = True
        encoded = b""
    if invalid_utf8:
        _fail("operation_record_shape_invalid")
    if len(encoded) > MAX_OPERATION_TEXT_BYTES:
        _fail("operation_record_text_budget_exceeded")
    return value


def _indexed_id(value: Any, *, nonempty: bool = True) -> str:
    value = _text(value, nonempty=nonempty)
    if len(value.encode("utf-8", "strict")) > MAX_OPERATION_INDEXED_ID_BYTES:
        _fail("operation_record_indexed_id_too_large")
    return value


def _validate_patch(value: Any) -> None:
    if type(value) is not dict:
        _fail("operation_record_patch_invalid")
    keys = frozenset(value)
    if not _PATCH_KEYS.issubset(keys) or not keys.issubset(
        _PATCH_KEYS | _OPTIONAL_PATCH_KEYS
    ):
        _fail("operation_record_patch_invalid")
    _indexed_id(value["entity_type"])
    _indexed_id(value["entity_id"])
    _indexed_id(value["field"])
    if type(value["op"]) is not str or value["op"] not in _PATCH_OPS:
        _fail("operation_record_patch_invalid")
    if "position" in value and (
        type(value["position"]) is not int
        or value["position"] < 0
        or value["position"] > _MAX_PATCH_POSITION
    ):
        _fail("operation_record_patch_invalid")
    if "provenance" in value:
        provenance = value["provenance"]
        if (
            type(provenance) is not dict
            or frozenset(provenance) != _PATCH_PROVENANCE_KEYS
        ):
            _fail("operation_record_patch_invalid")
        _indexed_id(provenance["field"])
        for endpoint_name in ("before", "after"):
            endpoint = provenance[endpoint_name]
            if (
                type(endpoint) is not dict
                or frozenset(endpoint) != _PATCH_PROVENANCE_STATE_KEYS
                or type(endpoint["present"]) is not bool
                or (
                    endpoint["present"]
                    and type(endpoint["value"]) is not dict
                )
                or (
                    not endpoint["present"]
                    and endpoint["value"] is not None
                )
            ):
                _fail("operation_record_patch_invalid")
    if "ancestry" in value:
        ancestry = value["ancestry"]
        if (
            type(ancestry) is not dict
            or frozenset(ancestry) != _PATCH_ANCESTRY_KEYS
        ):
            _fail("operation_record_patch_invalid")
        _indexed_id(ancestry["field"])
        for endpoint_name in ("before", "after"):
            endpoint = ancestry[endpoint_name]
            if (
                type(endpoint) is not dict
                or frozenset(endpoint) != _PATCH_PROVENANCE_STATE_KEYS
                or type(endpoint["present"]) is not bool
                or (
                    not endpoint["present"]
                    and endpoint["value"] is not None
                )
            ):
                _fail("operation_record_patch_invalid")


def freeze_operation_record(
    body: Any,
    *,
    maximum_bytes: int = MAX_OPERATION_RECORD_BYTES,
    json_dumps: Callable[..., str] = json.dumps,
) -> FrozenOperationRecord:
    """Freeze and semantically validate the exact current Operation envelope."""
    frozen = _freeze_operation_body_with_pipeline(
        body,
        maximum_bytes=maximum_bytes,
        json_dumps=json_dumps,
    )
    owned = materialize_operation_body(frozen)
    keys = frozenset(owned)
    if not _OPERATION_KEYS.issubset(keys) or not keys.issubset(
        _OPERATION_KEYS | _OPTIONAL_OPERATION_KEYS
    ):
        _fail("operation_record_shape_invalid")
    operation_id = _indexed_id(owned["id"])
    if (
        len(operation_id) != 32
        or any(character not in _LOWER_HEX for character in operation_id)
    ):
        _fail("operation_record_id_invalid")
    command_id = _indexed_id(owned["command_id"])
    if type(owned["source"]) is not str or owned["source"] not in _SOURCES:
        _fail("operation_record_shape_invalid")
    if type(owned["kind"]) is not str or owned["kind"] not in _KINDS:
        _fail("operation_record_shape_invalid")
    _indexed_id(owned["group_id"], nonempty=False)
    trace_id = _indexed_id(owned["trace_id"])
    document_id = _indexed_id(owned.get("document_id", ""))
    _indexed_id(owned.get("session_id", ""), nonempty=False)
    if type(owned["actor"]) is not dict:
        _fail("operation_record_shape_invalid")
    timestamp = owned["ts"]
    if (
        type(timestamp) not in (int, float)
        or (type(timestamp) is float and not math.isfinite(timestamp))
        or not 0 <= timestamp <= MAX_OPERATION_TIMESTAMP_SECONDS
    ):
        _fail("operation_record_shape_invalid")
    for name in ("patches", "reverse_patches"):
        patches = owned[name]
        if type(patches) is not list:
            _fail("operation_record_shape_invalid")
        for patch in patches:
            _validate_patch(patch)
    if owned["error"] is not None and type(owned["error"]) is not dict:
        _fail("operation_record_shape_invalid")
    if "external" in owned and type(owned["external"]) is not dict:
        _fail("operation_record_shape_invalid")
    _validate_pipeline_operation_metadata(owned)
    return FrozenOperationRecord(
        protocol=OPERATION_RECORD_PROTOCOL,
        operation_id=operation_id,
        command_id=command_id,
        source=owned["source"],
        trace_id=trace_id,
        document_id=document_id,
        body_bytes=frozen.body_bytes,
        body_sha256=frozen.body_sha256,
    )


def project_operation_targets(
    record: FrozenOperationRecord,
) -> Tuple[FrozenOperationTarget, ...]:
    """Project the complete ordered forward-patch target set.

    Reverse patches are recovery material, not additional mutation targets.
    Rebuilding the carrier first ensures a replaced or forged frozen record
    cannot make validation and SQL projection observe different bodies.
    """

    if type(record) is not FrozenOperationRecord:
        _fail("operation_target_record_invalid")
    owned = materialize_operation_body(
        FrozenOperationBody(record.protocol, record.body_bytes, record.body_sha256)
    )
    rebuilt = freeze_operation_record(owned)
    if rebuilt != record:
        _fail("operation_target_record_changed")
    patches = owned["patches"]
    if len(patches) > MAX_OPERATION_TARGETS_PER_RECORD:
        _fail("operation_target_count_exceeded")
    return tuple(
        FrozenOperationTarget(
            operation_id=record.operation_id,
            ordinal=ordinal,
            entity_type=patch["entity_type"],
            entity_id=patch["entity_id"],
            field=patch["field"],
            patch_kind=patch["op"],
        )
        for ordinal, patch in enumerate(patches)
    )


def _hash_text(value: Any) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in _LOWER_HEX for character in value)
    ):
        _fail("operation_chain_hash_invalid")
    return value


def operation_body_hash(
    prev_hash: str,
    body: Any,
    *,
    json_dumps: Callable[..., str] = json.dumps,
) -> str:
    """Return the historical OpLog hash without changing its JSON spacing."""
    _hash_text(prev_hash)
    if type(body) is FrozenOperationBody:
        body = materialize_operation_body(body)
    if type(body) is FrozenOperationRecord:
        body = materialize_operation_body(
            FrozenOperationBody(body.protocol, body.body_bytes, body.body_sha256)
        )
    if type(body) is not dict:
        _fail("operation_record_body_invalid")
    validate_json_graph(body)
    body_bytes = _json_bytes(body, json_dumps=json_dumps)
    return hashlib.sha256(prev_hash.encode("ascii") + body_bytes).hexdigest()


def encode_operation_chain_entry(
    prev_hash: str,
    body: Any,
    *,
    maximum_bytes: int = MAX_OPERATION_RECORD_BYTES,
    json_dumps: Callable[..., str] = json.dumps,
) -> Tuple[Dict[str, Any], bytes]:
    """Encode one exact historical ``{op, prev_hash, self_hash}`` line."""
    _hash_text(prev_hash)
    if type(body) is FrozenOperationBody:
        owned = materialize_operation_body(body)
    elif type(body) is FrozenOperationRecord:
        owned = materialize_operation_body(
            FrozenOperationBody(body.protocol, body.body_bytes, body.body_sha256)
        )
    elif type(body) is dict:
        owned = materialize_operation_body(
            _freeze_operation_body_with_pipeline(
                body,
                maximum_bytes=maximum_bytes,
                json_dumps=json_dumps,
            )
        )
    else:
        _fail("operation_record_body_invalid")
    _validate_pipeline_operation_metadata(owned)
    entry = {
        "op": owned,
        "prev_hash": prev_hash,
        "self_hash": operation_body_hash(prev_hash, owned, json_dumps=json_dumps),
    }
    validate_json_graph(entry)
    line = _json_bytes(entry, json_dumps=json_dumps) + b"\n"
    if len(line) > maximum_bytes:
        _fail("operation_record_byte_budget_exceeded")
    detached = decode_strict_json(line[:-1], maximum_bytes=maximum_bytes)
    if type(detached) is not dict:
        _fail("operation_chain_entry_invalid")
    return detached, line


def operation_entries_form_chain(
    entries: Iterable[Any],
    *,
    json_dumps: Callable[..., str] = json.dumps,
) -> bool:
    """Verify a materialized chain without raising or retaining payload data."""
    if type(entries) not in (list, tuple):
        return False
    previous = ZERO_OPERATION_HASH
    try:
        for entry in entries:
            if type(entry) is not dict or frozenset(entry) != frozenset(
                ("op", "prev_hash", "self_hash")
            ):
                return False
            body = entry["op"]
            if type(body) is not dict or entry["prev_hash"] != previous:
                return False
            _validate_pipeline_operation_metadata(body)
            self_hash = entry["self_hash"]
            if type(self_hash) is not str or not hmac.compare_digest(
                self_hash,
                operation_body_hash(previous, body, json_dumps=json_dumps),
            ):
                return False
            previous = self_hash
    except (OperationRecordCodecError, TypeError, ValueError, OverflowError, UnicodeError):
        return False
    return True


def build_frozen_operation_chain(
    records: Tuple[FrozenOperationRecord, ...],
    *,
    first_prev_hash: str,
) -> Tuple[FrozenOperationChainEntry, ...]:
    """Build an immutable commit-local chain from already-frozen records."""
    if type(records) is not tuple:
        _fail("operation_record_tuple_required")
    previous = _hash_text(first_prev_hash)
    output = []
    for ordinal, record in enumerate(records):
        if type(record) is not FrozenOperationRecord:
            _fail("operation_record_type_invalid")
        # Revalidation catches dataclass replacement/tampering before hashing.
        verified = freeze_operation_record(materialize_operation_body(
            FrozenOperationBody(record.protocol, record.body_bytes, record.body_sha256)
        ))
        if verified != record:
            _fail("operation_record_changed")
        _entry, line = encode_operation_chain_entry(previous, record)
        decoded = decode_strict_json(line[:-1])
        current = decoded["self_hash"]
        output.append(
            FrozenOperationChainEntry(
                protocol=OPERATION_CHAIN_PROTOCOL,
                operation_id=record.operation_id,
                ordinal=ordinal,
                prev_hash=previous,
                self_hash=current,
                record_sha256=record.body_sha256,
                entry_bytes=line[:-1],
            )
        )
        previous = current
    return tuple(output)


__all__ = [
    "FrozenOperationBody",
    "FrozenOperationChainEntry",
    "FrozenOperationRecord",
    "FrozenOperationTarget",
    "MAX_OPERATION_INTEGER_DIGITS",
    "MAX_OPERATION_INDEXED_ID_BYTES",
    "MAX_OPERATION_JSON_DEPTH",
    "MAX_OPERATION_RECORD_BYTES",
    "MAX_OPERATION_TARGETS_PER_RECORD",
    "MAX_OPERATION_TEXT_BYTES",
    "MAX_OPERATION_TIMESTAMP_SECONDS",
    "MAX_OPERATION_VALUE_NODES",
    "OPERATION_CHAIN_PROTOCOL",
    "OPERATION_RECORD_PROTOCOL",
    "OperationRecordCodecError",
    "ZERO_OPERATION_HASH",
    "build_frozen_operation_chain",
    "decode_strict_json",
    "encode_operation_chain_entry",
    "freeze_operation_body",
    "freeze_operation_record",
    "materialize_operation_body",
    "operation_body_hash",
    "operation_entries_form_chain",
    "project_operation_targets",
    "validate_json_graph",
]
