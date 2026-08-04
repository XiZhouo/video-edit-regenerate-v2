"""Deterministic, strict JSON codec for future SQLite business snapshots.

This module deliberately knows nothing about Document, Entity, repositories or
SQLite connections.  It defines only the byte-level value boundary that those
later adapters may use.  The accepted model is smaller than Python's standard
``json`` encoder: only exact JSON value types are valid, integers have a
codec-owned decimal digit bound, all floating-point numbers are finite, object
keys are strings, and every decoded payload must already be the unique canonical
byte representation produced by :func:`encode_payload`.

The ``*_bounded`` variants keep that byte format unchanged while adding caller-
supplied byte and serialized-node budgets before allocating canonical output or
the decoder's text/value tree.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Dict, List, Set, Tuple


CODEC_ID = "ze.deterministic-json"
CODEC_VERSION = 1
MAX_NESTING_DEPTH = 128
# Codec-owned CPU boundary: identical on runtimes with or without CPython's
# process-global int/string digit limit.
MAX_INTEGER_DIGITS = 4300
_MAX_INTEGER_ABS_EXCLUSIVE = 10**MAX_INTEGER_DIGITS


class BusinessCodecError(ValueError):
    """Stable codec failure without serializing the rejected value in its message."""

    def __init__(self, code: str, path: str, value_type: str) -> None:
        if type(code) is not str or not code:
            raise TypeError("code must be a non-empty string")
        if type(path) is not str or not path:
            raise TypeError("path must be a non-empty string")
        if type(value_type) is not str or not value_type:
            raise TypeError("value_type must be a non-empty string")
        self.code = code
        self.path = path
        self.value_type = value_type
        # Do not include ``path`` here: object keys are data and may contain
        # secrets or attacker-controlled log formatting.  Structured callers
        # can inspect the separate path attribute when it is safe to do so.
        super().__init__("%s (%s)" % (code, value_type))


class _ObjectPairs:
    """Marker returned by json.object_pairs_hook before duplicate-key folding."""

    __slots__ = ("pairs",)

    def __init__(self, pairs: List[Tuple[str, Any]]) -> None:
        self.pairs = tuple(pairs)


class _NonFiniteNumber:
    """Internal parse sentinel for NaN/Infinity and overflowing exponents."""

    __slots__ = ()


_NON_FINITE = _NonFiniteNumber()


class _IntegerTooLarge:
    """Internal parse sentinel that avoids constructing an oversized integer."""

    __slots__ = ()


_INTEGER_TOO_LARGE = _IntegerTooLarge()


_SAFE_TYPE_NAMES = {
    type(None): "NoneType",
    bool: "bool",
    int: "int",
    float: "float",
    str: "str",
    list: "list",
    dict: "dict",
    tuple: "tuple",
    set: "set",
    bytes: "bytes",
    bytearray: "bytearray",
    memoryview: "memoryview",
    object: "object",
}


def _type_name(value: Any) -> str:
    # Class names are mutable attacker-controlled data.  Never copy an unknown
    # ``__name__`` into an error message or telemetry field.
    return _SAFE_TYPE_NAMES.get(type(value), "unsupported")


def _key_path(path: str, key: str) -> str:
    # JSON-style quoting makes control characters unambiguous in the structured
    # attribute.  It is intentionally not included in the exception message.
    return "%s[%s]" % (path, json.dumps(key, ensure_ascii=True))


def _index_path(path: str, index: int) -> str:
    return "%s[%d]" % (path, index)


def _validate_string(value: str, path: str) -> str:
    invalid = False
    try:
        value.encode("utf-8", "strict")
    except UnicodeEncodeError:
        invalid = True
    if invalid:
        # Raise outside the handler so neither __cause__ nor __context__ keeps
        # the rejected string alive in logs or exception telemetry.
        raise BusinessCodecError(
            "business_codec_utf8_invalid", path, "str"
        )
    return value


def _enter_container(
    value: Any,
    *,
    path: str,
    depth: int,
    active: Set[int],
) -> int:
    identity = id(value)
    if identity in active:
        raise BusinessCodecError(
            "business_codec_cycle", path, _type_name(value)
        )
    if depth >= MAX_NESTING_DEPTH:
        raise BusinessCodecError(
            "business_codec_depth_exceeded", path, _type_name(value)
        )
    active.add(identity)
    return identity


def _normalize_value(
    value: Any,
    *,
    path: str,
    depth: int,
    active: Set[int],
    decoded: bool,
) -> Any:
    """Validate and recursively copy one value into exact built-in JSON types."""
    if value is None:
        return None
    if type(value) is bool:
        return value
    if type(value) is int:
        if (
            value >= _MAX_INTEGER_ABS_EXCLUSIVE
            or value <= -_MAX_INTEGER_ABS_EXCLUSIVE
        ):
            raise BusinessCodecError(
                "business_codec_integer_too_large", path, "int"
            )
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise BusinessCodecError(
                "business_codec_number_non_finite", path, "float"
            )
        return value
    if type(value) is str:
        return _validate_string(value, path)
    if value is _NON_FINITE:
        raise BusinessCodecError(
            "business_codec_number_non_finite", path, "float"
        )
    if value is _INTEGER_TOO_LARGE:
        raise BusinessCodecError(
            "business_codec_integer_too_large", path, "int"
        )

    if decoded and type(value) is _ObjectPairs:
        identity = _enter_container(value, path=path, depth=depth, active=active)
        try:
            output: Dict[str, Any] = {}
            for key, child in value.pairs:
                # CPython's decoder returns exact strings, but keep this check
                # explicit so the accepted value model does not depend on that
                # implementation detail.
                if type(key) is not str:
                    raise BusinessCodecError(
                        "business_codec_key_type_invalid", path, _type_name(key)
                    )
                child_path = _key_path(path, key)
                _validate_string(key, child_path)
                if key in output:
                    raise BusinessCodecError(
                        "business_codec_duplicate_key", child_path, "str"
                    )
                output[key] = _normalize_value(
                    child,
                    path=child_path,
                    depth=depth + 1,
                    active=active,
                    decoded=True,
                )
            return output
        finally:
            active.remove(identity)

    if type(value) is list:
        identity = _enter_container(value, path=path, depth=depth, active=active)
        try:
            return [
                _normalize_value(
                    child,
                    path=_index_path(path, index),
                    depth=depth + 1,
                    active=active,
                    decoded=decoded,
                )
                for index, child in enumerate(value)
            ]
        finally:
            active.remove(identity)

    if type(value) is dict:
        identity = _enter_container(value, path=path, depth=depth, active=active)
        try:
            output = {}
            for key, child in value.items():
                if type(key) is not str:
                    raise BusinessCodecError(
                        "business_codec_key_type_invalid", path, _type_name(key)
                    )
                child_path = _key_path(path, key)
                _validate_string(key, child_path)
                output[key] = _normalize_value(
                    child,
                    path=child_path,
                    depth=depth + 1,
                    active=active,
                    decoded=decoded,
                )
            return output
        finally:
            active.remove(identity)

    raise BusinessCodecError(
        "business_codec_type_unsupported", path, _type_name(value)
    )


def _integer_text(value: int) -> str:
    """Render a codec-bounded integer without CPython's digit-limit policy.

    Python 3.11 added a process-global limit to the usual decimal int/string
    conversion while older supported runtimes have no such limit.  A codec
    cannot inherit that ambient policy without changing its accepted value set
    between runtimes, and it must not mutate the process-global setting.  Base
    1e9 chunks keep each individual conversion permanently below that limit.
    """
    if value == 0:
        return "0"
    negative = value < 0
    remaining = -value if negative else value
    chunks: List[int] = []
    while remaining:
        remaining, chunk = divmod(remaining, 1_000_000_000)
        chunks.append(chunk)
    parts = [str(chunks.pop())]
    parts.extend("%09d" % chunk for chunk in reversed(chunks))
    text = "".join(parts)
    return "-" + text if negative else text


def _encode_value(value: Any) -> str:
    if value is None:
        return "null"
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) is int:
        return _integer_text(value)
    if type(value) is float:
        return json.dumps(value, allow_nan=False)
    if type(value) is str:
        return json.dumps(value, ensure_ascii=False)
    if type(value) is list:
        return "[" + ",".join(_encode_value(child) for child in value) + "]"
    if type(value) is dict:
        return "{" + ",".join(
            _encode_value(key) + ":" + _encode_value(value[key])
            for key in sorted(value)
        ) + "}"
    raise TypeError("normalized value has an unsupported type")


def _encode_normalized(value: Any) -> bytes:
    failure = ""
    try:
        return _encode_value(value).encode("utf-8", "strict")
    except UnicodeEncodeError:
        failure = "utf8"
    except (TypeError, ValueError, OverflowError, RecursionError):
        failure = "encoding"
    if failure == "utf8":
        # Normalization already checks every string.  Keep a stable fail-closed
        # boundary if a different JSON runtime ever violates that assumption.
        raise BusinessCodecError(
            "business_codec_utf8_invalid", "$", "str"
        )
    raise BusinessCodecError(
        "business_codec_encoding_failed", "$", _type_name(value)
    )


class _ByteBudget:
    """Incremental canonical-size counter used before allocating output bytes."""

    __slots__ = ("maximum", "maximum_nodes", "nodes", "total")

    def __init__(self, maximum: int, maximum_nodes: int) -> None:
        self.maximum = maximum
        self.maximum_nodes = maximum_nodes
        self.nodes = 0
        self.total = 0

    def add(self, amount: int, path: str, value_type: str) -> None:
        if amount < 0 or self.total > self.maximum - amount:
            raise BusinessCodecError(
                "business_codec_payload_too_large", path, value_type
            )
        self.total += amount

    def visit(self, path: str, value_type: str) -> None:
        if self.nodes >= self.maximum_nodes:
            raise BusinessCodecError(
                "business_codec_node_limit_exceeded", path, value_type
            )
        self.nodes += 1


def _measure_string(value: str, *, path: str, budget: _ByteBudget) -> None:
    """Count ``json.dumps(..., ensure_ascii=False)`` bytes without encoding it."""
    budget.add(2, path, "str")  # surrounding quotes
    for character in value:
        codepoint = ord(character)
        if character in ('"', "\\"):
            width = 2
        elif codepoint in (8, 9, 10, 12, 13):
            width = 2
        elif codepoint < 0x20:
            width = 6
        elif codepoint < 0x80:
            width = 1
        elif codepoint < 0x800:
            width = 2
        elif 0xD800 <= codepoint <= 0xDFFF:
            raise BusinessCodecError(
                "business_codec_utf8_invalid", path, "str"
            )
        elif codepoint < 0x10000:
            width = 3
        else:
            width = 4
        budget.add(width, path, "str")


def _measure_value(
    value: Any,
    *,
    path: str,
    depth: int,
    active: Set[int],
    budget: _ByteBudget,
) -> None:
    """Measure the existing canonical format with the codec's exact value rules."""
    budget.visit(path, _type_name(value))
    if value is None:
        budget.add(4, path, "NoneType")
        return
    if type(value) is bool:
        budget.add(4 if value else 5, path, "bool")
        return
    if type(value) is int:
        if (
            value >= _MAX_INTEGER_ABS_EXCLUSIVE
            or value <= -_MAX_INTEGER_ABS_EXCLUSIVE
        ):
            raise BusinessCodecError(
                "business_codec_integer_too_large", path, "int"
            )
        budget.add(len(_integer_text(value)), path, "int")
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise BusinessCodecError(
                "business_codec_number_non_finite", path, "float"
            )
        # A JSON float scalar is permanently small; use the exact formatter
        # owned by the encoder so measurement cannot drift from its spelling.
        budget.add(len(json.dumps(value, allow_nan=False)), path, "float")
        return
    if type(value) is str:
        _measure_string(value, path=path, budget=budget)
        return

    if type(value) is list:
        identity = _enter_container(value, path=path, depth=depth, active=active)
        try:
            budget.add(2, path, "list")
            for index, child in enumerate(value):
                if index:
                    budget.add(1, path, "list")
                _measure_value(
                    child,
                    path=_index_path(path, index),
                    depth=depth + 1,
                    active=active,
                    budget=budget,
                )
        finally:
            active.remove(identity)
        return

    if type(value) is dict:
        identity = _enter_container(value, path=path, depth=depth, active=active)
        try:
            budget.add(2, path, "dict")
            for index, (key, child) in enumerate(value.items()):
                if type(key) is not str:
                    raise BusinessCodecError(
                        "business_codec_key_type_invalid", path, _type_name(key)
                    )
                if index:
                    budget.add(1, path, "dict")
                # Avoid _key_path here: quoting a rejected giant key would itself
                # allocate the very string this preflight exists to prevent.
                child_path = path + "[<key>]"
                budget.visit(child_path, "str")
                _measure_string(key, path=child_path, budget=budget)
                budget.add(1, child_path, "dict")  # colon
                _measure_value(
                    child,
                    path=child_path,
                    depth=depth + 1,
                    active=active,
                    budget=budget,
                )
        finally:
            active.remove(identity)
        return

    raise BusinessCodecError(
        "business_codec_type_unsupported", path, _type_name(value)
    )


def encode_payload(value: Any) -> bytes:
    """Return the unique compact UTF-8 representation of one strict JSON value."""
    normalized = _normalize_value(
        value,
        path="$",
        depth=0,
        active=set(),
        decoded=False,
    )
    return _encode_normalized(normalized)


def encode_payload_bounded(
    value: Any,
    maximum_bytes: int,
    maximum_nodes: int,
) -> bytes:
    """Encode only after canonical byte/node size is within caller budgets.

    The preflight walks strict built-in JSON values with the same cycle, depth,
    integer, float and UTF-8 rules as :func:`encode_payload`.  In particular it
    counts JSON string escaping one codepoint at a time, so an oversized live
    string is rejected without first allocating its UTF-8/JSON representation.
    A node is every serialized value plus every object key.
    """
    if type(maximum_bytes) is not int:
        raise TypeError("maximum_bytes must be an exact integer")
    if maximum_bytes < 0:
        raise ValueError("maximum_bytes must be non-negative")
    if type(maximum_nodes) is not int:
        raise TypeError("maximum_nodes must be an exact integer")
    if maximum_nodes < 0:
        raise ValueError("maximum_nodes must be non-negative")
    budget = _ByteBudget(maximum_bytes, maximum_nodes)
    _measure_value(
        value,
        path="$",
        depth=0,
        active=set(),
        budget=budget,
    )
    payload = encode_payload(value)
    output_nodes = _preflight_json_nodes(payload, maximum_nodes)
    if (
        len(payload) != budget.total
        or len(payload) > maximum_bytes
        or output_nodes != budget.nodes
    ):
        # A mismatch means the measuring and encoding definitions diverged or
        # the caller concurrently mutated its exact built-in value graph.
        raise BusinessCodecError(
            "business_codec_measurement_mismatch", "$", _type_name(value)
        )
    return payload


def _parse_float(value: str) -> Any:
    parsed = float(value)
    return parsed if math.isfinite(parsed) else _NON_FINITE


def _parse_int(value: str) -> Any:
    """Parse bounded JSON integers independent of runtime digit-limit policy."""
    negative = value.startswith("-")
    digits = value[1:] if negative else value
    if len(digits) > MAX_INTEGER_DIGITS:
        return _INTEGER_TOO_LARGE
    result = 0
    first_width = len(digits) % 9 or 9
    result = int(digits[:first_width])
    for offset in range(first_width, len(digits), 9):
        result = result * 1_000_000_000 + int(digits[offset : offset + 9])
    return -result if negative else result


def _parse_constant(_value: str) -> Any:
    return _NON_FINITE


def decode_payload(payload: bytes) -> Any:
    """Decode one canonical payload, rejecting all alternate JSON spellings."""
    if type(payload) is not bytes:
        raise BusinessCodecError(
            "business_codec_payload_type_invalid", "$", _type_name(payload)
        )
    if payload.startswith(b"\xef\xbb\xbf"):
        raise BusinessCodecError(
            "business_codec_utf8_invalid", "$", "bytes"
        )
    utf8_invalid = False
    try:
        text = payload.decode("utf-8", "strict")
    except UnicodeDecodeError:
        utf8_invalid = True
        text = ""
    if utf8_invalid:
        raise BusinessCodecError(
            "business_codec_utf8_invalid", "$", "bytes"
        )

    parse_failure = ""
    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_ObjectPairs,
            parse_int=_parse_int,
            parse_float=_parse_float,
            parse_constant=_parse_constant,
        )
    except RecursionError:
        parse_failure = "depth"
        parsed = None
    except (json.JSONDecodeError, UnicodeError, ValueError):
        parse_failure = "json"
        parsed = None
    if parse_failure == "depth":
        raise BusinessCodecError(
            "business_codec_depth_exceeded", "$", "bytes"
        )
    if parse_failure:
        raise BusinessCodecError(
            "business_codec_json_invalid", "$", "bytes"
        )

    normalized = _normalize_value(
        parsed,
        path="$",
        depth=0,
        active=set(),
        decoded=True,
    )
    canonical = _encode_normalized(normalized)
    if canonical != payload:
        raise BusinessCodecError(
            "business_codec_noncanonical", "$", "bytes"
        )
    return normalized


def _preflight_json_nodes(payload: bytes, maximum_nodes: int) -> int:
    """Count JSON value/key tokens without decoding text or allocating a tree."""
    nodes = 0
    index = 0
    length = len(payload)

    def visit() -> None:
        nonlocal nodes
        if nodes >= maximum_nodes:
            raise BusinessCodecError(
                "business_codec_node_limit_exceeded", "$", "bytes"
            )
        nodes += 1

    while index < length:
        byte = payload[index]
        if byte in (ord("{"), ord("[")):
            visit()
            index += 1
            continue
        if byte == ord('"'):
            # Every JSON string token is exactly one node: either a value or an
            # object key.  Skip its escaped content so punctuation/keywords in
            # a string cannot inflate or evade the count.
            visit()
            index += 1
            while index < length:
                inner = payload[index]
                if inner == ord("\\"):
                    index += 2
                    continue
                index += 1
                if inner == ord('"'):
                    break
            continue
        if byte == ord("t"):
            visit()
            index += 4
            continue
        if byte == ord("f"):
            visit()
            index += 5
            continue
        if byte == ord("n"):
            visit()
            index += 4
            continue
        # Python's JSON parser routes these three non-standard constants through
        # parse_constant before the strict decoder rejects them.  Count them so
        # a giant invalid array cannot allocate a parse tree before rejection.
        if byte == ord("N"):
            visit()
            index += 3  # NaN
            continue
        if byte == ord("I"):
            visit()
            index += 8  # Infinity
            continue
        if byte == ord("-") and payload[index : index + 9] == b"-Infinity":
            visit()
            index += 9
            continue
        if byte == ord("-") or ord("0") <= byte <= ord("9"):
            visit()
            index += 1
            while index < length and payload[index] in b"0123456789+-.eE":
                index += 1
            continue
        index += 1
    return nodes


def decode_payload_bounded(
    payload: bytes,
    maximum_bytes: int,
    maximum_nodes: int,
) -> Any:
    """Apply byte/node budgets before the strict decoder allocates text/tree."""
    if type(maximum_bytes) is not int:
        raise TypeError("maximum_bytes must be an exact integer")
    if maximum_bytes < 0:
        raise ValueError("maximum_bytes must be non-negative")
    if type(maximum_nodes) is not int:
        raise TypeError("maximum_nodes must be an exact integer")
    if maximum_nodes < 0:
        raise ValueError("maximum_nodes must be non-negative")
    if type(payload) is not bytes:
        raise BusinessCodecError(
            "business_codec_payload_type_invalid", "$", _type_name(payload)
        )
    if len(payload) > maximum_bytes:
        raise BusinessCodecError(
            "business_codec_payload_too_large", "$", "bytes"
        )
    _preflight_json_nodes(payload, maximum_nodes)
    return decode_payload(payload)


def payload_sha256(payload: bytes) -> bytes:
    """Return the 32-byte SHA-256 digest of an already encoded payload."""
    if type(payload) is not bytes:
        raise BusinessCodecError(
            "business_codec_payload_type_invalid", "$", _type_name(payload)
        )
    return hashlib.sha256(payload).digest()


__all__ = [
    "BusinessCodecError",
    "CODEC_ID",
    "CODEC_VERSION",
    "MAX_INTEGER_DIGITS",
    "MAX_NESTING_DEPTH",
    "decode_payload",
    "decode_payload_bounded",
    "encode_payload",
    "encode_payload_bounded",
    "payload_sha256",
]
