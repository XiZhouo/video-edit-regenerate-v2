"""Compatibility and tamper tests for the single Operation byte algorithm."""

from __future__ import annotations

import dataclasses
import copy
import json
import sys
import tracemalloc
from pathlib import Path

import pytest


_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import operation_record_codec as codec_module  # noqa: E402
from operation_record_codec import (  # noqa: E402
    FrozenOperationRecord,
    OperationRecordCodecError,
    ZERO_OPERATION_HASH,
    build_frozen_operation_chain,
    decode_strict_json,
    encode_operation_chain_entry,
    freeze_operation_body,
    freeze_operation_record,
    materialize_operation_body,
    operation_body_hash,
    operation_entries_form_chain,
    project_operation_targets,
)


def _body() -> dict:
    return {
        "id": "1" * 32,
        "command_id": "entity.update",
        "source": "user",
        "actor": {"source": "user", "user_id": "u-1"},
        "ts": 1.25,
        "patches": [
            {
                "entity_type": "card",
                "entity_id": "card-1",
                "field": "title",
                "op": "set",
                "before": "old",
                "after": "新",
            }
        ],
        "reverse_patches": [
            {
                "entity_type": "card",
                "entity_id": "card-1",
                "field": "title",
                "op": "set",
                "before": "新",
                "after": "old",
            }
        ],
        "group_id": "",
        "trace_id": "2" * 32,
        "kind": "native",
        "error": None,
        "document_id": "document-1",
    }


def test_historical_body_bytes_and_hash_fixed_vector() -> None:
    body = _body()
    expected_body = json.dumps(
        body,
        ensure_ascii=False,
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    frozen = freeze_operation_record(body)
    assert frozen.body_bytes == expected_body
    assert operation_body_hash(ZERO_OPERATION_HASH, body) == (
        "40ac8fdf98605d56e78f37b96a3b4ff813c22a49cce17f6c5e43a575936c287c"
    )
    entry, line = encode_operation_chain_entry(ZERO_OPERATION_HASH, body)
    assert line == (
        json.dumps(entry, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")
    assert entry["self_hash"] == operation_body_hash(ZERO_OPERATION_HASH, body)
    assert operation_entries_form_chain([entry])


def test_optional_pipeline_carrier_round_trip_and_old_hash_compatibility() -> None:
    historical = _body()
    expected_historical = freeze_operation_record(historical)
    with_pipeline = copy.deepcopy(historical)
    with_pipeline["pipeline"] = {
        "schema": "ze.pipeline-start.v1",
        "document_id": "document-1",
        "run_id": "run-1",
        "stages": [{"id": "compose"}, {"id": "publish"}],
    }

    pipeline_record = freeze_operation_record(with_pipeline)
    materialized = decode_strict_json(pipeline_record.body_bytes)
    assert materialized["pipeline"] == with_pipeline["pipeline"]
    assert pipeline_record.body_sha256 != expected_historical.body_sha256

    # Merely introducing the optional carrier must not add a null/default key
    # to historical bytes or change the fixed vector above.
    assert "pipeline" not in decode_strict_json(expected_historical.body_bytes)
    assert expected_historical == freeze_operation_record(_body())


@pytest.mark.parametrize(
    "schema",
    ["ze.pipeline-start.v2", "ze.pipeline-control.v2"],
)
def test_pipeline_authority_v2_durable_codec_is_active_for_sealed_writer(
    schema: str,
) -> None:
    """The raw durable codec knows v2; Operation construction owns the seal."""
    body = _body()
    body["pipeline"] = {"schema": schema}
    frozen = freeze_operation_record(body)
    assert decode_strict_json(frozen.body_bytes)["pipeline"] == {"schema": schema}


@pytest.mark.parametrize(
    "pipeline,code",
    [
        ({}, "operation_record_pipeline_schema_invalid"),
        (
            {"schema": "ze.pipeline-future.v9"},
            "operation_record_pipeline_schema_invalid",
        ),
        (
            {
                "schema": "ze.pipeline-control.v1",
                "text": "x" * (16 * 1024 + 1),
            },
            "operation_record_pipeline_text_budget_exceeded",
        ),
        (
            {
                "schema": "ze.pipeline-control.v1",
                "values": [0] * 2_044,
            },
            "operation_record_pipeline_node_budget_exceeded",
        ),
        (
            {
                "schema": "ze.pipeline-control.v1",
                **{f"v{index}": "x" * 14_000 for index in range(5)},
            },
            "operation_record_pipeline_byte_budget_exceeded",
        ),
    ],
    ids=["missing-schema", "unknown-schema", "text", "nodes", "bytes"],
)
def test_pipeline_carrier_resource_and_schema_failures_are_stable(
    pipeline, code,
) -> None:
    body = _body()
    body["pipeline"] = pipeline
    with pytest.raises(OperationRecordCodecError) as raised:
        freeze_operation_record(body)
    assert raised.value.code == code
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_pipeline_carrier_depth_budget_and_node_boundary() -> None:
    within = {"schema": "ze.pipeline-control.v1", "values": [0] * 2_043}
    body = _body()
    body["pipeline"] = within
    assert decode_strict_json(freeze_operation_record(body).body_bytes)[
        "pipeline"
    ] == within

    nested: object = "leaf"
    for _ in range(12):
        nested = [nested]
    body["pipeline"] = {
        "schema": "ze.pipeline-control.v1",
        "nested": nested,
    }
    with pytest.raises(OperationRecordCodecError) as raised:
        freeze_operation_record(body)
    assert raised.value.code == "operation_record_pipeline_depth_exceeded"


def test_pipeline_start_has_its_own_four_mibibyte_budget() -> None:
    body = _body()
    body["pipeline"] = {
        "schema": "ze.pipeline-start.v1",
        **{f"chunk_{index}": "x" * 250_000 for index in range(17)},
    }
    with pytest.raises(OperationRecordCodecError) as raised:
        freeze_operation_record(body)
    assert raised.value.code == "operation_record_pipeline_byte_budget_exceeded"


def test_pipeline_carrier_rejects_behavioral_container_before_copy_hook() -> None:
    calls = []

    class HostileDict(dict):
        def __deepcopy__(self, _memo):
            calls.append("deepcopy")
            raise AssertionError("copy hook must not execute")

        def get(self, *_args, **_kwargs):
            calls.append("get")
            raise AssertionError("mapping hook must not execute")

    body = _body()
    body["pipeline"] = HostileDict(schema="ze.pipeline-start.v1")
    with pytest.raises(OperationRecordCodecError) as raised:
        freeze_operation_record(body)
    assert raised.value.code == "operation_record_pipeline_invalid"
    assert calls == []


@pytest.mark.parametrize(
    "payload",
    [
        ("tuple-is-not-wire-json",),
        {1: "non-string-key"},
        float("nan"),
        10 ** 4_301,
    ],
    ids=["tuple", "key", "non-finite", "huge-integer"],
)
def test_pipeline_carrier_acceptance_domain_is_exact_json(payload) -> None:
    body = _body()
    body["pipeline"] = {
        "schema": "ze.pipeline-start.v1",
        "payload": payload,
    }
    with pytest.raises(OperationRecordCodecError) as raised:
        freeze_operation_record(body)
    assert raised.value.code == "operation_record_pipeline_invalid"


def test_pipeline_carrier_distinguishes_shared_dag_from_cycle() -> None:
    shared = ["one"]
    body = _body()
    body["pipeline"] = {
        "schema": "ze.pipeline-control.v1",
        "left": shared,
        "right": shared,
        "enabled": True,
        "ratio": -0.0,
    }
    frozen = freeze_operation_record(body)
    projected = decode_strict_json(frozen.body_bytes)["pipeline"]
    assert projected["left"] == projected["right"] == ["one"]
    assert projected["enabled"] is True
    assert projected["ratio"] == -0.0

    cycle = []
    cycle.append(cycle)
    body["pipeline"] = {
        "schema": "ze.pipeline-control.v1",
        "cycle": cycle,
    }
    with pytest.raises(OperationRecordCodecError) as raised:
        freeze_operation_record(body)
    assert raised.value.code == "operation_record_pipeline_invalid"


def test_over_budget_pipeline_fails_before_full_json_encoding(monkeypatch) -> None:
    body = _body()
    body["pipeline"] = {
        "schema": "ze.pipeline-control.v1",
        **{f"v{index}": "x" * 14_000 for index in range(5)},
    }

    def forbidden_encoder(*_args, **_kwargs):
        raise AssertionError("over-budget metadata must fail before full JSON encoding")

    monkeypatch.setattr(codec_module, "_json_bytes", forbidden_encoder)
    with pytest.raises(OperationRecordCodecError) as raised:
        freeze_operation_record(body)
    assert raised.value.code == "operation_record_pipeline_byte_budget_exceeded"


def test_pipeline_encoding_uses_bounded_owned_snapshot_not_live_caller(monkeypatch) -> None:
    body = _body()
    caller_pipeline = {
        "schema": "ze.pipeline-start.v1",
        "payload": {"items": ["original"]},
    }
    body["pipeline"] = caller_pipeline
    real_encoder = codec_module._json_bytes
    calls = 0

    def mutate_caller_before_encode(value, *, json_dumps):
        nonlocal calls
        calls += 1
        caller_pipeline["payload"]["items"].append(f"forged-{calls}")
        assert value is not caller_pipeline
        return real_encoder(value, json_dumps=json_dumps)

    monkeypatch.setattr(codec_module, "_json_bytes", mutate_caller_before_encode)
    frozen = freeze_operation_record(body)
    persisted = decode_strict_json(frozen.body_bytes)["pipeline"]
    assert persisted["payload"]["items"] == ["original"]
    assert caller_pipeline["payload"]["items"] != ["original"]


def test_pipeline_byte_counter_matches_legacy_json_at_exact_boundary() -> None:
    value = {
        "schema": "ze.pipeline-control.v1",
        "escaped": "quote=\" slash=\\ line=\n tab=\t control=\u0001",
        "unicode": "雪🐈",
        "float": -1.25e-19,
        "negative_zero": -0.0,
        "enabled": True,
        "nested": [None, 7, {"ok": False}],
    }
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")

    owned = codec_module._own_pipeline_json_graph(
        value,
        maximum_bytes=len(encoded),
        maximum_depth=12,
        maximum_nodes=2_048,
        maximum_text_bytes=16 * 1024,
    )
    assert owned == value
    with pytest.raises(OperationRecordCodecError) as raised:
        codec_module._own_pipeline_json_graph(
            value,
            maximum_bytes=len(encoded) - 1,
            maximum_depth=12,
            maximum_nodes=2_048,
            maximum_text_bytes=16 * 1024,
        )
    assert raised.value.code == "operation_record_pipeline_byte_budget_exceeded"


def test_pipeline_text_budget_fails_without_allocating_the_whole_string() -> None:
    oversized = "x" * 10_000_000
    value = {
        "schema": "ze.pipeline-control.v1",
        "text": oversized,
    }
    tracemalloc.start()
    try:
        with pytest.raises(OperationRecordCodecError) as raised:
            codec_module._normalize_pipeline_operation_metadata(value)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert raised.value.code == "operation_record_pipeline_text_budget_exceeded"
    assert peak < 2 * 1024 * 1024


def test_chain_encoder_owns_raw_pipeline_before_hash_and_line() -> None:
    body = _body()
    body["pipeline"] = {
        "schema": "ze.pipeline-start.v1",
        "run_id": "run-1",
    }
    real_dumps = json.dumps
    calls = 0

    def mutate_caller_after_encode(value, **kwargs):
        nonlocal calls
        rendered = real_dumps(value, **kwargs)
        calls += 1
        body["pipeline"]["schema"] = "ze.pipeline-future.v9"
        body["pipeline"]["forged"] = calls
        return rendered

    entry, line = encode_operation_chain_entry(
        ZERO_OPERATION_HASH,
        body,
        json_dumps=mutate_caller_after_encode,
    )
    persisted = decode_strict_json(line[:-1])
    assert entry == persisted
    assert entry["op"]["pipeline"] == {
        "schema": "ze.pipeline-start.v1",
        "run_id": "run-1",
    }
    assert operation_entries_form_chain([entry])


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
def test_resigned_pipeline_carrier_cannot_form_a_verified_chain(pipeline) -> None:
    body = _body()
    body["pipeline"] = pipeline
    entry = {
        "op": body,
        "prev_hash": ZERO_OPERATION_HASH,
        # Simulate an attacker who can recompute the unkeyed compatibility
        # hash. Semantic carrier validation must still reject the row.
        "self_hash": operation_body_hash(ZERO_OPERATION_HASH, body),
    }
    assert not operation_entries_form_chain([entry])


def test_optional_patch_provenance_position_and_ancestry_round_trip() -> None:
    body = _body()
    body["patches"][0].update({
        "provenance": {
            "field": "box.a",
            "before": {"present": False, "value": None},
            "after": {
                "present": True,
                "value": {"actor": "u-1", "model": "m"},
            },
        },
        "position": 2,
        "ancestry": {
            "field": "box",
            "before": {"present": True, "value": None},
            "after": {"present": True, "value": {"a": 2}},
        },
    })

    frozen = freeze_operation_record(body)
    materialized = decode_strict_json(frozen.body_bytes)
    assert materialized == body
    assert materialized["patches"][0]["position"] == 2
    assert materialized["patches"][0]["provenance"]["field"] == "box.a"
    assert materialized["patches"][0]["ancestry"]["field"] == "box"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda patch: patch.update(position=True),
        lambda patch: patch.update(position=-1),
        lambda patch: patch.update(position=2**63),
        lambda patch: patch.update(provenance={
            "field": "x",
            "before": {"present": False, "value": {"forged": True}},
            "after": {"present": True, "value": {"actor": "u"}},
        }),
        lambda patch: patch.update(ancestry={
            "field": "x",
            "before": {"present": False},
            "after": {"present": True, "value": 1},
        }),
    ],
    ids=[
        "bool-position", "negative-position", "oversized-position",
        "bad-provenance-endpoint", "bad-ancestry-endpoint",
    ],
)
def test_optional_patch_envelopes_fail_closed(mutate) -> None:
    body = _body()
    mutate(body["patches"][0])
    with pytest.raises(OperationRecordCodecError) as raised:
        freeze_operation_record(body)
    assert raised.value.code == "operation_record_patch_invalid"


def test_tuple_values_keep_historical_list_canonical_bytes_and_hash() -> None:
    list_body = _body()
    list_body["patches"][0]["before"] = [1, {"nested": [2, 3]}]
    tuple_body = copy.deepcopy(list_body)
    tuple_body["patches"][0]["before"] = (1, {"nested": (2, 3)})

    frozen_list = freeze_operation_record(list_body)
    frozen_tuple = freeze_operation_record(tuple_body)
    assert frozen_tuple.body_bytes == frozen_list.body_bytes
    assert operation_body_hash(ZERO_OPERATION_HASH, tuple_body) == (
        operation_body_hash(ZERO_OPERATION_HASH, list_body)
    )
    assert operation_body_hash(ZERO_OPERATION_HASH, tuple_body) == (
        "6a92b63dc90b6cc1b230a7145378b8af2126dbc51fc8b460e943299885b9d367"
    )
    assert decode_strict_json(frozen_tuple.body_bytes)["patches"][0]["before"] == [
        1, {"nested": [2, 3]},
    ]


@pytest.mark.parametrize(
    "key,encoded_key",
    [(1, "1"), (True, "true"), (None, "null"), (1.5, "1.5")],
)
def test_scalar_dict_keys_keep_historical_json_normalization(key, encoded_key) -> None:
    frozen = freeze_operation_body({"value": {key: "kept"}})
    assert materialize_operation_body(frozen) == {
        "value": {encoded_key: "kept"},
    }


def test_freeze_detaches_mutable_input_and_chain_is_immutable() -> None:
    body = _body()
    frozen = freeze_operation_record(body)
    body["actor"]["user_id"] = "changed"
    body["patches"][0]["after"] = "changed"
    assert b'"user_id": "u-1"' in frozen.body_bytes
    chain = build_frozen_operation_chain((frozen,), first_prev_hash=ZERO_OPERATION_HASH)
    assert chain[0].operation_id == "1" * 32
    with pytest.raises(dataclasses.FrozenInstanceError):
        chain[0].self_hash = "0" * 64


def test_record_semantics_reject_raw_shape_and_tampered_carrier() -> None:
    with pytest.raises(OperationRecordCodecError):
        freeze_operation_record({"id": "1" * 32})
    frozen = freeze_operation_record(_body())
    tampered = dataclasses.replace(frozen, body_sha256=b"x" * 32)
    with pytest.raises(OperationRecordCodecError):
        build_frozen_operation_chain((tampered,), first_prev_hash=ZERO_OPERATION_HASH)
    with pytest.raises(OperationRecordCodecError):
        freeze_operation_body({"callback": lambda: None})


def test_atomic_record_ids_match_v6_indexed_column_contract() -> None:
    invalid_id = _body()
    invalid_id["id"] = "operation-not-hex"
    with pytest.raises(OperationRecordCodecError):
        freeze_operation_record(invalid_id)

    oversized = _body()
    oversized["command_id"] = "c" * 1025
    with pytest.raises(OperationRecordCodecError):
        freeze_operation_record(oversized)

    oversized_patch = _body()
    oversized_patch["patches"][0]["field"] = "f" * 1025
    with pytest.raises(OperationRecordCodecError):
        freeze_operation_record(oversized_patch)

    unformattable_time = _body()
    unformattable_time["ts"] = 1e308
    with pytest.raises(OperationRecordCodecError):
        freeze_operation_record(unformattable_time)


def test_atomic_target_projection_is_forward_exact_and_schema_bounded(monkeypatch) -> None:
    body = _body()
    body["reverse_patches"][0]["entity_id"] = "reverse-only"
    record = freeze_operation_record(body)
    targets = project_operation_targets(record)
    assert [dataclasses.asdict(target) for target in targets] == [{
        "operation_id": "1" * 32,
        "ordinal": 0,
        "entity_type": "card",
        "entity_id": "card-1",
        "field": "title",
        "patch_kind": "set",
    }]

    body["patches"].append(dict(body["patches"][0], field="subtitle"))
    oversized = freeze_operation_record(body)
    monkeypatch.setattr(codec_module, "MAX_OPERATION_TARGETS_PER_RECORD", 1)
    with pytest.raises(OperationRecordCodecError) as raised:
        project_operation_targets(oversized)
    assert raised.value.code == "operation_target_count_exceeded"


def test_oplog_compatibility_budget_keeps_large_text_and_high_node_records() -> None:
    large = freeze_operation_body({"text": "x" * (2 * 1024 * 1024)})
    assert len(large.body_bytes) > 2 * 1024 * 1024
    high_nodes = freeze_operation_body({"values": [0] * 300_000})
    assert len(decode_strict_json(high_nodes.body_bytes)["values"]) == 300_000


def test_duplicate_keys_nonfinite_and_broken_chain_fail_closed() -> None:
    for raw in (b'{"x":1,"x":2}', b'{"x":NaN}'):
        with pytest.raises(OperationRecordCodecError):
            decode_strict_json(raw)
    record = freeze_operation_record(_body())
    entry, _line = encode_operation_chain_entry(ZERO_OPERATION_HASH, record)
    entry["self_hash"] = "f" * 64
    assert not operation_entries_form_chain([entry])


def test_codec_error_does_not_retain_rejected_payload_exception() -> None:
    secret = "secret-operation-payload"
    with pytest.raises(OperationRecordCodecError) as raised:
        decode_strict_json((f'{{"value":"{secret}"').encode())
    error = raised.value
    assert error.__cause__ is None
    assert error.__context__ is None
    assert secret not in repr(error)
