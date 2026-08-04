"""Adversarial contract tests for the standalone deterministic snapshot codec."""

from __future__ import annotations

import hashlib
import math
import sys
from decimal import Decimal
from enum import IntEnum
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import business_codec as codec_module
from business_codec import (
    BusinessCodecError,
    CODEC_ID,
    CODEC_VERSION,
    MAX_INTEGER_DIGITS,
    MAX_NESTING_DEPTH,
    decode_payload,
    decode_payload_bounded,
    encode_payload,
    encode_payload_bounded,
    payload_sha256,
)


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        -10**100,
        -0.0,
        "quote:\" slash:\\ control:\x00 中 😀",
        [],
        {},
        {"中": [None, False, 1.25, {"line\n": "\u2028"}]},
    ],
)
def test_bounded_encode_measurement_matches_canonical_bytes(value) -> None:
    expected = encode_payload(value)
    assert encode_payload_bounded(value, len(expected), 100) == expected
    _assert_error(
        lambda: encode_payload_bounded(value, len(expected) - 1, 100),
        "business_codec_payload_too_large",
    )


def test_bounded_encode_rejects_before_unbounded_encoder(monkeypatch) -> None:
    called = []

    def forbidden(_value):
        called.append(True)
        raise AssertionError("unbounded encoder was called")

    monkeypatch.setattr(codec_module, "encode_payload", forbidden)
    _assert_error(
        lambda: codec_module.encode_payload_bounded("x" * 10_000, 32, 100),
        "business_codec_payload_too_large",
    )
    assert called == []


def test_bounded_encode_rechecks_equal_length_output_node_shape(monkeypatch) -> None:
    # ``null`` and ``[[]]`` are both four bytes, but have one vs two nodes.
    monkeypatch.setattr(codec_module, "encode_payload", lambda _value: b"[[]]")
    _assert_error(
        lambda: codec_module.encode_payload_bounded(None, 4, 2),
        "business_codec_measurement_mismatch",
    )


def test_bounded_node_definition_matches_encode_and_decode() -> None:
    value = {"x": "[NaN] {escaped}", "y": [None]}
    payload = encode_payload(value)
    # root object + two keys + string value + list value + null value
    assert encode_payload_bounded(value, len(payload), 6) == payload
    assert decode_payload_bounded(payload, len(payload), 6) == value
    _assert_error(
        lambda: encode_payload_bounded(value, len(payload), 5),
        "business_codec_node_limit_exceeded",
    )
    _assert_error(
        lambda: decode_payload_bounded(payload, len(payload), 5),
        "business_codec_node_limit_exceeded",
    )


def test_bounded_decode_rejects_node_flood_before_json_loads(monkeypatch) -> None:
    payload = b"[" + b",".join([b"[]"] * 1000) + b"]"

    def forbidden(*_args, **_kwargs):
        raise AssertionError("json.loads was called")

    monkeypatch.setattr(codec_module.json, "loads", forbidden)
    _assert_error(
        lambda: codec_module.decode_payload_bounded(payload, len(payload), 10),
        "business_codec_node_limit_exceeded",
    )


def test_bounded_decode_preserves_strict_malformed_error_below_limits() -> None:
    _assert_error(
        lambda: decode_payload_bounded(b'{"x":[}', 64, 20),
        "business_codec_json_invalid",
    )


@pytest.mark.parametrize("constant", [b"NaN", b"Infinity", b"-Infinity"])
def test_bounded_decode_counts_nonstandard_json_constants_before_parse(
    monkeypatch, constant: bytes
) -> None:
    payload = b"[" + b",".join([constant] * 1000) + b"]"

    def forbidden(*_args, **_kwargs):
        raise AssertionError("json.loads was called")

    monkeypatch.setattr(codec_module.json, "loads", forbidden)
    _assert_error(
        lambda: codec_module.decode_payload_bounded(payload, len(payload), 10),
        "business_codec_node_limit_exceeded",
    )


@pytest.mark.parametrize("name", ["maximum_bytes", "maximum_nodes"])
@pytest.mark.parametrize("invalid", [True, 1.0, "1", None])
def test_bounded_codec_limits_require_exact_integers(name: str, invalid) -> None:
    byte_limit = invalid if name == "maximum_bytes" else 16
    node_limit = invalid if name == "maximum_nodes" else 4
    with pytest.raises(TypeError):
        encode_payload_bounded(None, byte_limit, node_limit)
    with pytest.raises(TypeError):
        decode_payload_bounded(b"null", byte_limit, node_limit)


@pytest.mark.parametrize("limits", [(-1, 1), (4, -1)])
def test_bounded_codec_limits_reject_negative_values(limits) -> None:
    with pytest.raises(ValueError):
        encode_payload_bounded(None, *limits)
    with pytest.raises(ValueError):
        decode_payload_bounded(b"null", *limits)


def test_bounded_codec_preserves_cycle_depth_and_exact_type_rules() -> None:
    cycle = []
    cycle.append(cycle)
    _assert_error(
        lambda: encode_payload_bounded(cycle, 1024, 1024),
        "business_codec_cycle",
    )
    at_limit = _nested_list(MAX_NESTING_DEPTH)
    payload = encode_payload(at_limit)
    assert encode_payload_bounded(
        at_limit, len(payload), MAX_NESTING_DEPTH + 1
    ) == payload
    _assert_error(
        lambda: encode_payload_bounded([at_limit], len(payload) + 2, 1024),
        "business_codec_depth_exceeded",
    )

    class ListSubclass(list):
        pass

    _assert_error(
        lambda: encode_payload_bounded(ListSubclass(), 16, 16),
        "business_codec_type_unsupported",
    )


def _nested_list(container_count: int):
    value = None
    for _ in range(container_count):
        value = [value]
    return value


def _assert_error(
    call,
    code: str,
    *,
    path: str | None = None,
    value_type: str | None = None,
) -> BusinessCodecError:
    with pytest.raises(BusinessCodecError) as raised:
        call()
    error = raised.value
    assert error.code == code
    if path is not None:
        assert error.path == path
    if value_type is not None:
        assert error.value_type == value_type
    return error


def test_public_identity_and_primitive_corpus_are_exact() -> None:
    assert CODEC_ID == "ze.deterministic-json"
    assert CODEC_VERSION == 1
    assert MAX_NESTING_DEPTH == 128
    assert MAX_INTEGER_DIGITS == 4300

    corpus = [
        (None, b"null"),
        (True, b"true"),
        (False, b"false"),
        (0, b"0"),
        (-42, b"-42"),
        (1.5, b"1.5"),
        (-0.0, b"-0.0"),
        (5e-324, b"5e-324"),
        (sys.float_info.min, b"2.2250738585072014e-308"),
        (sys.float_info.max, b"1.7976931348623157e+308"),
        ("中文😀e\u0301", "\"中文😀é\"".encode("utf-8")),
        ("line\n\t\u0000", b'"line\\n\\t\\u0000"'),
    ]
    for value, expected in corpus:
        encoded = encode_payload(value)
        assert encoded == expected
        decoded = decode_payload(encoded)
        assert decoded == value

    negative_zero = decode_payload(b"-0.0")
    assert math.copysign(1.0, negative_zero) == -1.0


def test_objects_have_one_order_independent_utf8_representation() -> None:
    left = {"z": 1, "中": [True, {"b": 2, "a": 1}], "a": "😀"}
    right = {"a": "😀", "中": [True, {"a": 1, "b": 2}], "z": 1}

    expected = '{"a":"😀","z":1,"中":[true,{"a":1,"b":2}]}'.encode("utf-8")
    assert encode_payload(left) == expected
    assert encode_payload(right) == expected
    assert encode_payload(left) == encode_payload(left)
    assert decode_payload(expected) == left


def test_integer_boundary_is_codec_owned_not_runtime_owned() -> None:
    # The accepted boundary matches on runtimes with and without CPython's
    # process-global decimal conversion policy.
    positive = 10**4299 + 123456789
    negative = -positive

    positive_bytes = b"1" + (b"0" * 4290) + b"123456789"
    assert len(positive_bytes) == MAX_INTEGER_DIGITS
    assert encode_payload(positive) == positive_bytes
    assert encode_payload(negative) == b"-" + positive_bytes
    assert decode_payload(positive_bytes) == positive
    assert decode_payload(b"-" + positive_bytes) == negative
    assert encode_payload_bounded(positive, len(positive_bytes), 1) == positive_bytes
    assert decode_payload_bounded(positive_bytes, len(positive_bytes), 1) == positive
    assert encode_payload_bounded(
        negative, len(positive_bytes) + 1, 1
    ) == b"-" + positive_bytes
    assert decode_payload_bounded(
        b"-" + positive_bytes, len(positive_bytes) + 1, 1
    ) == negative

    too_large = 10**MAX_INTEGER_DIGITS
    _assert_error(
        lambda: encode_payload({"nested": too_large}),
        "business_codec_integer_too_large",
        path='$["nested"]',
        value_type="int",
    )
    oversized_bytes = b"1" + (b"0" * MAX_INTEGER_DIGITS)
    _assert_error(
        lambda: decode_payload(b"[" + oversized_bytes + b"]"),
        "business_codec_integer_too_large",
        path="$[0]",
        value_type="int",
    )


def test_repeated_alias_is_not_a_cycle_and_decode_returns_independent_values() -> None:
    shared = {"items": [1, 2]}
    payload = encode_payload({"a": shared, "b": shared})
    decoded = decode_payload(payload)

    assert decoded == {"a": shared, "b": shared}
    assert decoded["a"] is not decoded["b"]
    assert decoded["a"]["items"] is not decoded["b"]["items"]
    decoded["a"]["items"].append(3)
    assert decoded["b"]["items"] == [1, 2]
    decoded_again = decode_payload(payload)
    assert decoded_again is not decoded
    assert decoded_again["a"] is not decoded["a"]


@pytest.mark.parametrize(
    ("value", "value_type"),
    [
        ((1, 2), "tuple"),
        ({1, 2}, "set"),
        (b"secret", "bytes"),
        (bytearray(b"secret"), "bytearray"),
        (Decimal("1.2"), "unsupported"),
        (Path("secret"), "unsupported"),
        (object(), "object"),
    ],
)
def test_unsupported_python_types_fail_closed(value, value_type: str) -> None:
    _assert_error(
        lambda: encode_payload(value),
        "business_codec_type_unsupported",
        path="$",
        value_type=value_type,
    )


def test_builtin_subclasses_and_non_string_object_keys_are_rejected() -> None:
    class IntSubclass(int):
        pass

    class StringSubclass(str):
        pass

    class ListSubclass(list):
        pass

    class DictSubclass(dict):
        pass

    class Number(IntEnum):
        ONE = 1

    for value in [
        IntSubclass(1),
        StringSubclass("x"),
        ListSubclass(),
        DictSubclass(),
        Number.ONE,
    ]:
        _assert_error(
            lambda value=value: encode_payload(value),
            "business_codec_type_unsupported",
            path="$",
            value_type="unsupported",
        )

    error = _assert_error(
        lambda: encode_payload({1: "secret-value"}),
        "business_codec_key_type_invalid",
        path="$",
        value_type="int",
    )
    assert "secret-value" not in str(error)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_python_numbers_are_rejected(value: float) -> None:
    _assert_error(
        lambda: encode_payload({"number": value}),
        "business_codec_number_non_finite",
        path='$["number"]',
        value_type="float",
    )


def test_cycles_fail_but_maximum_depth_is_precise() -> None:
    cyclic_list = []
    cyclic_list.append(cyclic_list)
    _assert_error(
        lambda: encode_payload(cyclic_list),
        "business_codec_cycle",
        path="$[0]",
        value_type="list",
    )

    cyclic_dict = {}
    cyclic_dict["self"] = cyclic_dict
    _assert_error(
        lambda: encode_payload(cyclic_dict),
        "business_codec_cycle",
        path='$["self"]',
        value_type="dict",
    )

    mutual_left = []
    mutual_right = [mutual_left]
    mutual_left.append(mutual_right)
    _assert_error(
        lambda: encode_payload(mutual_left),
        "business_codec_cycle",
        path="$[0][0]",
        value_type="list",
    )

    at_limit = _nested_list(MAX_NESTING_DEPTH)
    payload = encode_payload(at_limit)
    assert decode_payload(payload) == at_limit

    over_limit = _nested_list(MAX_NESTING_DEPTH + 1)
    _assert_error(
        lambda: encode_payload(over_limit),
        "business_codec_depth_exceeded",
        value_type="list",
    )
    _assert_error(
        lambda: decode_payload((b"[" * 129) + b"null" + (b"]" * 129)),
        "business_codec_depth_exceeded",
        value_type="list",
    )


@pytest.mark.parametrize(
    "value",
    ["\ud800", "\udfff", {"\ud800": 1}, ["ok", "\ud800"]],
)
def test_lone_surrogates_are_rejected_before_encoding(value) -> None:
    _assert_error(lambda: encode_payload(value), "business_codec_utf8_invalid")


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (b"", "business_codec_json_invalid"),
        (b"nul", "business_codec_json_invalid"),
        (b"01", "business_codec_json_invalid"),
        (b"[1,]", "business_codec_json_invalid"),
        (b'"unterminated', "business_codec_json_invalid"),
        (b'"\xff"', "business_codec_utf8_invalid"),
        (b"\xef\xbb\xbfnull", "business_codec_utf8_invalid"),
    ],
)
def test_invalid_json_or_utf8_is_rejected(payload: bytes, code: str) -> None:
    _assert_error(lambda: decode_payload(payload), code)


@pytest.mark.parametrize(
    "payload",
    [
        b"NaN",
        b"Infinity",
        b"-Infinity",
        b"1e400",
        b"-1e400",
        b"[0,1e400]",
    ],
)
def test_non_finite_json_numbers_are_rejected(payload: bytes) -> None:
    _assert_error(
        lambda: decode_payload(payload),
        "business_codec_number_non_finite",
        value_type="float",
    )


@pytest.mark.parametrize(
    ("payload", "path"),
    [
        (b'{"x":1,"x":2}', '$["x"]'),
        (b'{"a":1,"\\u0061":2}', '$["a"]'),
        (b'{"outer":{"ok":1,"x":2,"x":3}}', '$["outer"]["x"]'),
        (b'[{"x":1,"x":2}]', '$[0]["x"]'),
    ],
)
def test_duplicate_keys_are_detected_at_every_depth(payload: bytes, path: str) -> None:
    _assert_error(
        lambda: decode_payload(payload),
        "business_codec_duplicate_key",
        path=path,
        value_type="str",
    )


@pytest.mark.parametrize(
    "payload",
    [
        b" null",
        b"null\n",
        b"1.0 ",
        b"1e0",
        b"1.00",
        b"-0",
        b'{"a": 1}',
        b'{"b":2,"a":1}',
        b'"\\u4e2d"',
        b'"\\ud83d\\ude00"',
        b'"\\u000a"',
        b'"\\/"',
    ],
)
def test_valid_but_noncanonical_json_is_rejected(payload: bytes) -> None:
    _assert_error(
        lambda: decode_payload(payload),
        "business_codec_noncanonical",
        path="$",
        value_type="bytes",
    )


@pytest.mark.parametrize("payload", [bytearray(b"null"), memoryview(b"null"), "null"])
def test_decode_requires_exact_bytes(payload) -> None:
    _assert_error(
        lambda: decode_payload(payload),
        "business_codec_payload_type_invalid",
        path="$",
        value_type=type(payload).__name__,
    )


def test_errors_expose_stable_structure_without_echoing_values() -> None:
    sensitive_value = "customer-password-123"
    error = _assert_error(
        lambda: encode_payload({"credential": [sensitive_value.encode()]}),
        "business_codec_type_unsupported",
        path='$["credential"][0]',
        value_type="bytes",
    )
    assert error.args == ("business_codec_type_unsupported (bytes)",)
    assert sensitive_value not in str(error)
    assert "credential" not in str(error)

    class AttackerNamedType:
        pass

    AttackerNamedType.__name__ = "TOPSECRET\nforged-log-entry"
    poisoned = _assert_error(
        lambda: encode_payload(AttackerNamedType()),
        "business_codec_type_unsupported",
        path="$",
        value_type="unsupported",
    )
    assert poisoned.args == ("business_codec_type_unsupported (unsupported)",)
    assert "TOPSECRET" not in str(poisoned)
    assert "forged-log-entry" not in repr(poisoned)


@pytest.mark.parametrize(
    ("call", "code"),
    [
        (
            lambda: decode_payload(b'{"secret":"TOPSECRET",oops}'),
            "business_codec_json_invalid",
        ),
        (
            lambda: decode_payload(b'"TOPSECRET\xff"'),
            "business_codec_utf8_invalid",
        ),
        (
            lambda: encode_payload("TOPSECRET\ud800"),
            "business_codec_utf8_invalid",
        ),
    ],
)
def test_errors_do_not_retain_sensitive_parser_or_unicode_exceptions(call, code: str) -> None:
    error = _assert_error(call, code)
    assert error.__cause__ is None
    assert error.__context__ is None
    assert "TOPSECRET" not in str(error)
    assert "TOPSECRET" not in repr(error)


def test_payload_sha256_is_binary_exact_and_type_strict() -> None:
    payload = b'{"a":1}'
    digest = payload_sha256(payload)
    assert digest == hashlib.sha256(payload).digest()
    assert len(digest) == 32
    assert payload_sha256(payload) == digest

    for invalid in [bytearray(payload), memoryview(payload), payload.decode()]:
        _assert_error(
            lambda invalid=invalid: payload_sha256(invalid),
            "business_codec_payload_type_invalid",
            path="$",
            value_type=type(invalid).__name__,
        )
