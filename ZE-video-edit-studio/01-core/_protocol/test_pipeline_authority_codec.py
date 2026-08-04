from __future__ import annotations

import hashlib
import json
import sys
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest


HERE = Path(__file__).resolve().parent
PIPELINE_DIR = HERE.parent / "03-commands-operations" / "03-pipeline"
for directory in (HERE, PIPELINE_DIR):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

import pipeline_authority_codec as codec
from pipeline_authority_codec import (
    PIPELINE_AUTHORITY_CODEC_PROTOCOL,
    PIPELINE_CONTROL_SCHEMA,
    PIPELINE_START_SCHEMA,
    FrozenPipelineControl,
    FrozenPipelineStart,
    PipelineAuthorityCodecError,
    build_pipeline_control_descriptor,
    build_pipeline_start_descriptor,
    materialize_pipeline_descriptor,
    parse_pipeline_control,
    parse_pipeline_start,
)
from stage import StageSpec  # noqa: E402
from stage_plan import StagePlan  # noqa: E402


DOCUMENT_ID = "doc-1"
RUN_ID = "run-1"
PIPELINE_ID = "asset-pipeline"
START_ID = "1" * 32
ADD_ID = "2" * 32
CONTROL_ID = "3" * 32


def _policy(*, cardinality: int | None = None) -> dict[str, Any]:
    mutation_policy: dict[str, Any] = {
        "schema": "ze.entity-mutation-policy.v1",
        "lifecycle": {
            "create": ["asset.generate"],
            "rename": [],
            "soft_delete": [],
            "restore": [],
            "hard_remove": [],
        },
        "fields": {"status": ["asset.generate"]},
    }
    if cardinality is not None:
        mutation_policy["cardinality"] = {"max_total": cardinality}
    return {
        "schema": "ze.pipeline-document-policy.v1",
        "entity_policies": [
            {
                "entity_type": "asset",
                "entity_schema_digest": "b" * 64,
                "policy": mutation_policy,
            }
        ],
    }


def _policy_with_variant(
    *,
    variant_fields: tuple[str, ...] = ("status",),
    variant_create: bool = True,
) -> dict[str, Any]:
    policy = _policy()
    policy["entity_policies"].append(
        {
            "entity_type": "variant",
            "entity_schema_digest": "d" * 64,
            "policy": {
                "schema": "ze.entity-mutation-policy.v1",
                "lifecycle": {
                    "create": ["asset.generate"] if variant_create else [],
                    "rename": [],
                    "soft_delete": [],
                    "restore": [],
                    "hard_remove": [],
                },
                "fields": {
                    field_name: ["asset.generate"] for field_name in variant_fields
                },
            },
        }
    )
    return policy


def _creation_scope(
    *,
    entity_type: str = "variant",
    fields: list[str] | None = None,
    max_per_operation: int = 2,
    max_per_run: int = 8,
) -> dict[str, Any]:
    return {
        "entity_type": entity_type,
        "fields": ["status"] if fields is None else fields,
        "max_per_operation": max_per_operation,
        "max_per_run": max_per_run,
    }


def _work_list() -> dict[str, Any]:
    return {
        "run_id": RUN_ID,
        "name": "Generate",
        "items": [
            {
                "item_id": "item-1",
                "entity_type": "asset",
                "entity_id": "asset-1",
                "artifacts": ["preview/image.png"],
                "payload": {"prompt": "tree"},
            }
        ],
    }


def _stages() -> list[dict[str, Any]]:
    return [
        {
            "id": "generate",
            "label": "Generate",
            "command_id": "asset.generate",
            "produces": ["preview/image.png"],
            "resource_scopes": ["gpu"],
            "resource_costs": {"gpu": 2},
            "retry_safe": True,
            "interrupt_safe": True,
            "pause_after": False,
            "input_ports": [{"id": "prompt"}],
            "output_ports": [
                {
                    "id": "video",
                    "kind": "payload",
                    "index": 0,
                    "type": "video/mp4",
                    "extension": {"timeline": True},
                }
            ],
        }
    ]


def _start(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "document_id": DOCUMENT_ID,
        "run_id": RUN_ID,
        "pipeline_id": PIPELINE_ID,
        "start_command_id": "asset.generate",
        "start_operation_id": START_ID,
        "work_list": _work_list(),
        "stages": _stages(),
        "placeholder_bindings": [
            {
                "item_id": "item-1",
                "entity_type": "asset",
                "entity_id": "asset-1",
                "operation_id": ADD_ID,
            }
        ],
        "initial_operation_ids": [START_ID, ADD_ID],
        "concurrency": 1,
        "max_retries": 2,
        "parameters": {"quality": "draft"},
        "root_command_digest": "a" * 64,
        "document_policy": _policy(),
        "worker_actor": {
            "user_id": "__pipeline__",
            "device_id": "worker-1",
            "source": "ai",
        },
        # Actor.source and dispatch/Operation.source are separate axes.
        "worker_source": "system",
        "targets": [
            {
                "entity_type": "asset",
                "entity_id": "asset-1",
                "fields": ["status"],
                "lifecycle": ["create"],
            }
        ],
    }
    values.update(overrides)
    return build_pipeline_start_descriptor(**values)


def _control(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "document_id": DOCUMENT_ID,
        "run_id": RUN_ID,
        "pipeline_id": PIPELINE_ID,
        "control_operation_id": CONTROL_ID,
        "start_operation_id": START_ID,
        "start_operation_hash": "c" * 64,
        "action": "pause",
        "parameters": {},
        "expected_generation": 0,
        "expected_state": "paused",
    }
    values.update(overrides)
    return build_pipeline_control_descriptor(**values)


def _assert_invalid(value: Any, *, start: bool = True) -> PipelineAuthorityCodecError:
    with pytest.raises(PipelineAuthorityCodecError) as raised:
        (parse_pipeline_start if start else parse_pipeline_control)(value)
    assert raised.value.code == "pipeline_authority_descriptor_invalid"
    assert raised.value.context == {"reason": raised.value.reason}
    return raised.value


def test_start_and_control_round_trip_as_detached_frozen_facts() -> None:
    start = _start()
    frozen_start = parse_pipeline_start(start)
    assert isinstance(frozen_start, FrozenPipelineStart)
    assert frozen_start.protocol == PIPELINE_AUTHORITY_CODEC_PROTOCOL
    assert frozen_start.schema == PIPELINE_START_SCHEMA
    assert frozen_start.initial_operation_ids == (START_ID, ADD_ID)
    assert materialize_pipeline_descriptor(frozen_start) == start
    assert "tree" not in repr(frozen_start)

    control = _control()
    frozen_control = parse_pipeline_control(control)
    assert isinstance(frozen_control, FrozenPipelineControl)
    assert frozen_control.schema == PIPELINE_CONTROL_SCHEMA
    assert frozen_control.parameters == {}
    assert materialize_pipeline_descriptor(frozen_control) == control

    start["work_list"]["items"][0]["payload"]["prompt"] = "changed"
    control["parameters"]["later"] = True
    assert (
        materialize_pipeline_descriptor(frozen_start)["work_list"]["items"][0][
            "payload"
        ]["prompt"]
        == "tree"
    )
    assert frozen_control.parameters == {}


def test_builder_owns_all_inputs_and_policy_digest_is_reproducible() -> None:
    work_list = _work_list()
    stages = _stages()
    policy = _policy(cardinality=1)
    descriptor = _start(work_list=work_list, stages=stages, document_policy=policy)
    policy_bytes = json.dumps(
        descriptor["delegation"]["document_policy"],
        ensure_ascii=False,
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    assert (
        descriptor["delegation"]["document_policy_digest"]
        == hashlib.sha256(policy_bytes).hexdigest()
    )

    work_list["items"][0]["payload"]["prompt"] = "mutated"
    stages[0]["resource_costs"]["gpu"] = 99
    policy["entity_policies"][0]["policy"]["fields"].clear()
    assert descriptor["work_list"]["items"][0]["payload"]["prompt"] == "tree"
    assert descriptor["stages"][0]["resource_costs"] == {"gpu": 2}
    assert descriptor["delegation"]["document_policy"]["entity_policies"][0]["policy"][
        "fields"
    ] == {"status": ["asset.generate"]}


@pytest.mark.parametrize("schema", ["ze.pipeline-start.v1", "ze.pipeline-control.v1"])
def test_legacy_carrier_schemas_are_never_reinterpreted_as_authority(
    schema: str,
) -> None:
    candidate = _start()
    candidate["schema"] = schema
    _assert_invalid(candidate)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(extra=True),
        lambda value: value.pop("execution"),
        lambda value: value.update(start_operation_id="not-an-operation-id"),
        lambda value: value["initial_operation_ids"].__setitem__(0, "A" * 32),
        lambda value: value["placeholder_bindings"][0].update(operation_id="f" * 31),
        lambda value: value["execution"].update(concurrency=True),
        lambda value: value["execution"].update(max_retries=1.0),
    ],
)
def test_start_rejects_closed_shape_identity_and_exact_integer_attacks(mutate) -> None:
    candidate = _start()
    mutate(candidate)
    _assert_invalid(candidate)


def test_work_list_and_delegation_must_authorize_the_exact_same_targets() -> None:
    candidate = _start()
    candidate["delegation"]["targets"][0]["entity_id"] = "asset-outside-work-list"
    raised = _assert_invalid(candidate)
    assert raised.reason == "delegation_work_target_mismatch"


def test_creation_scopes_preserve_bounded_dynamic_creation_without_wildcards() -> None:
    scopes = [_creation_scope()]
    descriptor = _start(
        document_policy=_policy_with_variant(),
        creation_scopes=scopes,
    )
    frozen = parse_pipeline_start(descriptor)
    materialized = materialize_pipeline_descriptor(frozen)
    assert materialized["delegation"]["targets"] == descriptor["delegation"]["targets"]
    assert materialized["delegation"]["creation_scopes"] == scopes

    scopes[0]["fields"].append("caller-mutation")
    assert materialize_pipeline_descriptor(frozen)["delegation"]["creation_scopes"] == [
        _creation_scope()
    ]

    boundary = _start(
        document_policy=_policy_with_variant(),
        creation_scopes=[
            _creation_scope(
                max_per_operation=codec.MAX_WORK_ITEMS,
                max_per_run=codec.MAX_WORK_ITEMS,
            )
        ],
    )
    assert parse_pipeline_start(boundary).run_id == RUN_ID

    with pytest.raises(PipelineAuthorityCodecError) as wildcard:
        _start(
            document_policy=_policy_with_variant(),
            creation_scopes=[_creation_scope(entity_type="variant*")],
        )
    assert wildcard.value.reason == "creation_scope_wildcard_invalid"

    missing_required_scope = _start()
    missing_required_scope["delegation"].pop("creation_scopes")
    raised = _assert_invalid(missing_required_scope)
    assert raised.reason == "delegation_shape_invalid"


def test_creation_scopes_require_exact_policy_type_create_and_field_authority() -> None:
    with pytest.raises(PipelineAuthorityCodecError) as missing_type:
        _start(
            document_policy=_policy(),
            creation_scopes=[_creation_scope()],
        )
    assert missing_type.value.reason == "creation_scope_policy_missing"

    with pytest.raises(PipelineAuthorityCodecError) as missing_field:
        _start(
            document_policy=_policy_with_variant(variant_fields=("title",)),
            creation_scopes=[_creation_scope(fields=["status"])],
        )
    assert missing_field.value.reason == "creation_scope_field_not_authorized"

    with pytest.raises(PipelineAuthorityCodecError) as missing_create:
        _start(
            document_policy=_policy_with_variant(variant_create=False),
            creation_scopes=[_creation_scope()],
        )
    assert missing_create.value.reason == "creation_scope_create_not_authorized"

    unused_policy = _policy_with_variant()
    with pytest.raises(PipelineAuthorityCodecError) as unused_type:
        _start(document_policy=unused_policy, creation_scopes=[])
    assert unused_type.value.reason == "document_policy_scope_mismatch"


@pytest.mark.parametrize(
    "scope,reason",
    [
        (_creation_scope(max_per_operation=True), "creation_scope_limit_invalid"),
        (_creation_scope(max_per_operation=0), "creation_scope_limit_invalid"),
        (
            _creation_scope(max_per_operation=9, max_per_run=8),
            "creation_scope_limit_invalid",
        ),
        (
            _creation_scope(max_per_run=codec.MAX_WORK_ITEMS + 1),
            "creation_scope_limit_invalid",
        ),
        (
            _creation_scope(fields=["z", "a"]),
            "creation_scope_field_duplicate_or_unsorted",
        ),
        (
            _creation_scope(fields=["status", "status"]),
            "creation_scope_field_duplicate_or_unsorted",
        ),
    ],
)
def test_creation_scope_shape_order_and_exact_quantity_limits(
    scope: dict[str, Any], reason: str
) -> None:
    policy = _policy_with_variant(variant_fields=("a", "status", "z"))
    with pytest.raises(PipelineAuthorityCodecError) as raised:
        _start(document_policy=policy, creation_scopes=[scope])
    assert raised.value.reason == reason


def test_creation_scope_order_and_total_run_limit_are_bounded() -> None:
    policy = _policy_with_variant()
    unsorted_scopes = [
        _creation_scope(entity_type="variant"),
        _creation_scope(entity_type="asset", fields=["status"]),
    ]
    with pytest.raises(PipelineAuthorityCodecError) as unsorted:
        _start(document_policy=policy, creation_scopes=unsorted_scopes)
    assert unsorted.value.reason == "creation_scope_duplicate_or_unsorted"

    excessive_total = [
        _creation_scope(
            entity_type="asset",
            fields=["status"],
            max_per_operation=1,
            max_per_run=2_049,
        ),
        _creation_scope(
            entity_type="variant",
            max_per_operation=1,
            max_per_run=2_048,
        ),
    ]
    with pytest.raises(PipelineAuthorityCodecError) as excessive:
        _start(document_policy=policy, creation_scopes=excessive_total)
    assert excessive.value.reason == "creation_scope_total_limit_exceeded"


def test_placeholder_bindings_may_be_a_strict_subset_but_not_escape_work_list() -> None:
    candidate = _start(placeholder_bindings=[])
    assert materialize_pipeline_descriptor(parse_pipeline_start(candidate)) == candidate

    candidate = _start()
    candidate["placeholder_bindings"][0]["entity_id"] = "asset-elsewhere"
    raised = _assert_invalid(candidate)
    assert raised.reason == "placeholder_binding_mismatch"


def test_ports_bind_real_extension_bags_without_inventing_a_closed_dialect() -> None:
    candidate = _start()
    ports = candidate["stages"][0]["output_ports"]
    assert ports == [
        {
            "id": "video",
            "kind": "payload",
            "index": 0,
            "type": "video/mp4",
            "extension": {"timeline": True},
        }
    ]
    assert (
        materialize_pipeline_descriptor(parse_pipeline_start(candidate))["stages"][0][
            "output_ports"
        ]
        == ports
    )

    candidate["stages"][0]["input_ports"] = [{}]
    raised = _assert_invalid(candidate)
    assert raised.reason == "stage_port_invalid"


def test_stageplan_is_the_single_complete_serializer_for_authority_stage_metadata() -> (
    None
):
    stage = StageSpec(
        id="generate",
        label="Generate",
        run=lambda _context, _args: {},
        command_id="asset.generate",
        produces=("preview/image.png",),
        resource_scopes=("gpu",),
        resource_costs={"gpu": 2},
        retry_safe=True,
        interrupt_safe=True,
        pause_after=True,
        input_ports=({"id": "prompt"},),
        output_ports=(
            {"id": "video", "kind": "payload", "index": 0, "type": "video/mp4"},
        ),
    )
    descriptor = _start(stages=StagePlan.from_single(stage).to_meta())
    frozen = parse_pipeline_start(descriptor)
    stage_meta = materialize_pipeline_descriptor(frozen)["stages"][0]
    assert stage_meta["resource_costs"] == {"gpu": 2}
    assert stage_meta["pause_after"] is True
    assert stage_meta["output_ports"][0]["kind"] == "payload"


def test_policy_matches_core_336_shape_and_binds_entity_schema_identity() -> None:
    assert parse_pipeline_start(_start()).document_id == DOCUMENT_ID
    assert (
        parse_pipeline_start(_start(document_policy=_policy(cardinality=1))).run_id
        == RUN_ID
    )

    candidate = _start()
    entry = candidate["delegation"]["document_policy"]["entity_policies"][0]
    entry["entity_schema_digest"] = "B" * 64
    _assert_invalid(candidate)

    candidate = _start()
    policy = candidate["delegation"]["document_policy"]["entity_policies"][0]["policy"]
    policy["cardinality"] = None
    _assert_invalid(candidate)

    for forbidden in ("__entity__", "nested.field"):
        candidate = _start()
        policy = candidate["delegation"]["document_policy"]["entity_policies"][0][
            "policy"
        ]
        policy["fields"] = {forbidden: ["asset.generate"]}
        raised = _assert_invalid(candidate)
        assert raised.reason == "entity_policy_field_reserved"


def test_policy_digest_tamper_and_command_scope_fail_closed() -> None:
    candidate = _start()
    candidate["delegation"]["document_policy_digest"] = "0" * 64
    raised = _assert_invalid(candidate)
    assert raised.reason == "document_policy_digest_mismatch"

    candidate = _start()
    candidate["delegation"]["document_policy"]["entity_policies"][0]["policy"][
        "fields"
    ]["status"] = ["asset.other"]
    candidate["delegation"]["document_policy"]["entity_policies"][0]["policy"][
        "lifecycle"
    ]["create"] = []
    # Rebuilding recalculates the outer digest but must still reject a policy
    # that does not contain the real root command.
    with pytest.raises(PipelineAuthorityCodecError) as raised_build:
        _start(document_policy=candidate["delegation"]["document_policy"])
    assert raised_build.value.reason == "entity_policy_root_command_absent"


@pytest.mark.parametrize(
    "action,parameters",
    [
        ("retry_failed", {"item_ids": []}),
        ("retry_failed", {"item_ids": ["item-2", "item-1"]}),
        ("set_breakpoint", {"stage_id": "generate", "halt": 1}),
    ],
)
def test_control_parameters_reject_ambiguous_or_type_coerced_requests(
    action: str, parameters: dict[str, Any]
) -> None:
    with pytest.raises(PipelineAuthorityCodecError):
        _control(action=action, parameters=parameters)


def test_retry_failed_accepts_4096_worst_case_identifiers_with_coherent_budgets() -> (
    None
):
    item_ids = [
        f"{index:04x}" + ('"' * (codec.MAX_IDENTIFIER_BYTES - 4))
        for index in range(codec.MAX_CONTROL_ITEM_IDS)
    ]
    descriptor = _control(
        action="retry_failed",
        parameters={"item_ids": item_ids},
    )
    encoded = json.dumps(
        descriptor,
        ensure_ascii=False,
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    assert len(encoded) > 64 * 1024
    assert len(encoded) <= codec.MAX_CONTROL_BYTES
    frozen = parse_pipeline_control(descriptor)
    assert frozen.parameters["item_ids"] == item_ids


@pytest.mark.parametrize(
    "field,value",
    [
        ("control_operation_id", "3" * 31),
        ("start_operation_id", "A" * 32),
        ("expected_generation", False),
        ("expected_generation", 1.0),
        ("action", []),
        ("expected_state", {}),
    ],
)
def test_control_rejects_identity_enum_and_generation_attacks(
    field: str, value: Any
) -> None:
    candidate = _control()
    candidate[field] = value
    if field == "expected_generation":
        candidate["generation"] = 1 if value is False else 2.0
    _assert_invalid(candidate, start=False)


def test_frozen_projection_is_type_exact_and_parameters_cover_full_descriptor() -> None:
    frozen = parse_pipeline_control(_control())
    for forged in (
        replace(frozen, expected_generation=False),
        replace(frozen, generation=1.0),
        replace(frozen, parameters_bytes=b'{"x": 1}'),
        replace(frozen, action="resume"),
    ):
        with pytest.raises(PipelineAuthorityCodecError) as raised:
            materialize_pipeline_descriptor(forged)
        assert raised.value.reason == "frozen_descriptor_changed"


def test_frozen_public_dict_is_detached_and_missing_fields_fail_without_attribute_leak() -> (
    None
):
    frozen = parse_pipeline_control(_control())
    public_fields = frozen.__dict__
    public_fields["action"] = "resume"
    public_fields["descriptor_bytes"] = b"attacker"
    assert frozen.action == "pause"
    assert materialize_pipeline_descriptor(frozen)["action"] == "pause"

    # Even a caller deliberately bypassing the public detached projection can
    # only make the fact invalid; explicit field projection prevents a forged
    # in-memory view from inheriting the descriptor's authority.
    backing = object.__getattribute__(frozen, "__dict__")
    backing["action"] = "resume"
    with pytest.raises(PipelineAuthorityCodecError) as changed:
        materialize_pipeline_descriptor(frozen)
    assert changed.value.reason == "frozen_descriptor_changed"

    secret = "SECRET_MISSING_SLOT_PAYLOAD"
    incomplete = object.__new__(FrozenPipelineStart)
    object.__setattr__(incomplete, "protocol", secret)
    with pytest.raises(PipelineAuthorityCodecError) as missing:
        materialize_pipeline_descriptor(incomplete)
    error = missing.value
    assert error.reason == "frozen_descriptor_invalid"
    assert secret not in str(error)
    assert secret not in repr(error)
    assert error.__cause__ is None
    assert error.__context__ is None


def test_frozen_materialize_concurrent_mutation_is_valid_snapshot_or_stable_rejection() -> (
    None
):
    pause_descriptor = _control()
    resume_descriptor = _control(action="resume")
    pause = parse_pipeline_control(pause_descriptor)
    resume = parse_pipeline_control(resume_descriptor)
    target = replace(pause)
    stop = threading.Event()
    fields = tuple(pause.__dict__)

    def mutate() -> None:
        source = resume
        while not stop.is_set():
            for field_name in fields:
                object.__setattr__(target, field_name, getattr(source, field_name))
            source = pause if source is resume else resume

    thread = threading.Thread(target=mutate)
    thread.start()
    try:
        for _index in range(200):
            try:
                materialized = materialize_pipeline_descriptor(target)
            except PipelineAuthorityCodecError as error:
                assert error.reason in (
                    "frozen_descriptor_invalid",
                    "frozen_descriptor_changed",
                )
                assert error.__cause__ is None
                assert error.__context__ is None
            else:
                assert materialized in (pause_descriptor, resume_descriptor)
    finally:
        stop.set()
        thread.join(timeout=5)
    assert not thread.is_alive()


def test_frozen_bytes_are_bounded_before_hash_or_decode_and_errors_are_payload_free() -> (
    None
):
    frozen = parse_pipeline_start(_start())
    oversized = replace(
        frozen,
        descriptor_bytes=b"x" * (codec.MAX_START_BYTES + 1),
        descriptor_digest=hashlib.sha256(b"x" * (codec.MAX_START_BYTES + 1)).digest(),
    )
    with pytest.raises(PipelineAuthorityCodecError) as raised:
        materialize_pipeline_descriptor(oversized)
    assert raised.value.reason == "frozen_descriptor_invalid"

    secret = "SECRET_PIPELINE_PAYLOAD"
    malformed_bytes = ('{"secret":"' + secret + '"').encode()
    malformed = replace(
        frozen,
        descriptor_bytes=malformed_bytes,
        descriptor_digest=hashlib.sha256(malformed_bytes).digest(),
    )
    with pytest.raises(PipelineAuthorityCodecError) as malformed_raised:
        materialize_pipeline_descriptor(malformed)
    error = malformed_raised.value
    assert secret not in str(error)
    assert secret not in repr(error)
    assert error.__cause__ is None
    assert error.__context__ is None


@pytest.mark.parametrize(
    "raw,limits,reason",
    [
        (
            b"[0,0,0]",
            {"maximum_depth": 4, "maximum_nodes": 3, "maximum_text_bytes": 8},
            "node_budget_exceeded",
        ),
        (
            b"[[0]]",
            {"maximum_depth": 2, "maximum_nodes": 8, "maximum_text_bytes": 8},
            "depth_budget_exceeded",
        ),
        (
            b'"\\ud83d\\ude00"',
            {"maximum_depth": 2, "maximum_nodes": 1, "maximum_text_bytes": 3},
            "text_budget_exceeded",
        ),
        (
            b'{"a":1,"\\u0061":2}',
            {"maximum_depth": 2, "maximum_nodes": 8, "maximum_text_bytes": 8},
            "json_duplicate_key_invalid",
        ),
    ],
)
def test_frozen_scanner_rejects_structure_before_full_json_decode(
    monkeypatch: pytest.MonkeyPatch,
    raw: bytes,
    limits: dict[str, int],
    reason: str,
) -> None:
    entered_loads = False

    def forbidden_loads(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal entered_loads
        entered_loads = True
        raise AssertionError("full decode must not run before budget rejection")

    monkeypatch.setattr(codec.json, "loads", forbidden_loads)
    with pytest.raises(PipelineAuthorityCodecError) as raised:
        codec._decode_owned(raw, maximum=len(raw), **limits)
    assert raised.value.reason == reason
    assert entered_loads is False


def test_frozen_byte_budget_rejects_before_utf8_or_json_decode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered_loads = False

    def forbidden_loads(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal entered_loads
        entered_loads = True
        raise AssertionError("oversized bytes must not reach full decode")

    monkeypatch.setattr(codec.json, "loads", forbidden_loads)
    with pytest.raises(PipelineAuthorityCodecError) as raised:
        codec._decode_owned(
            b"[]",
            maximum=1,
            maximum_depth=1,
            maximum_nodes=1,
            maximum_text_bytes=1,
        )
    assert raised.value.reason == "frozen_bytes_invalid"
    assert entered_loads is False


def test_frozen_scanner_matches_owner_key_node_depth_and_surrogate_text_boundaries() -> (
    None
):
    raw = b'{"\\ud83d\\ude00":"\\u96ea"}'
    decoded = codec._decode_owned(
        raw,
        maximum=len(raw),
        maximum_depth=2,
        maximum_nodes=3,
        maximum_text_bytes=4,
    )
    assert decoded == {"😀": "雪"}

    for limits, reason in (
        (
            {"maximum_depth": 1, "maximum_nodes": 3, "maximum_text_bytes": 4},
            "depth_budget_exceeded",
        ),
        (
            {"maximum_depth": 2, "maximum_nodes": 2, "maximum_text_bytes": 4},
            "node_budget_exceeded",
        ),
        (
            {"maximum_depth": 2, "maximum_nodes": 3, "maximum_text_bytes": 3},
            "text_budget_exceeded",
        ),
    ):
        with pytest.raises(PipelineAuthorityCodecError) as raised:
            codec._decode_owned(raw, maximum=len(raw), **limits)
        assert raised.value.reason == reason

    lone_surrogate = b'"\\ud800"'
    with pytest.raises(PipelineAuthorityCodecError) as raised_surrogate:
        codec._decode_owned(
            lone_surrogate,
            maximum=len(lone_surrogate),
            maximum_depth=1,
            maximum_nodes=1,
            maximum_text_bytes=4,
        )
    assert raised_surrogate.value.reason == "text_utf8_invalid"


def test_incremental_owner_matches_legacy_spacing_at_exact_boundaries() -> None:
    graph = {"a": [1, True, None, "\n雪"], "b": {"c": -0.0}}
    encoded = json.dumps(
        graph, ensure_ascii=False, sort_keys=True, allow_nan=False
    ).encode("utf-8")
    owned = codec._own_json(
        graph,
        maximum_bytes=len(encoded),
        maximum_depth=6,
        maximum_nodes=12,
        maximum_text_bytes=8,
    )
    assert codec._canonical_bytes(owned, maximum=len(encoded)) == encoded
    with pytest.raises(PipelineAuthorityCodecError) as raised:
        codec._own_json(
            graph,
            maximum_bytes=len(encoded) - 1,
            maximum_depth=6,
            maximum_nodes=12,
            maximum_text_bytes=8,
        )
    assert raised.value.reason == "byte_budget_exceeded"


def test_incremental_owner_counts_keys_nodes_depth_text_and_rejects_cycles() -> None:
    graph = {"a": 1}
    assert (
        codec._own_json(
            graph,
            maximum_bytes=8,
            maximum_depth=2,
            maximum_nodes=3,
            maximum_text_bytes=1,
        )
        == graph
    )
    for kwargs, reason in (
        ({"maximum_nodes": 2}, "node_budget_exceeded"),
        ({"maximum_depth": 1}, "depth_budget_exceeded"),
        ({"maximum_text_bytes": 0}, "text_budget_exceeded"),
    ):
        limits = {
            "maximum_bytes": 8,
            "maximum_depth": 2,
            "maximum_nodes": 3,
            "maximum_text_bytes": 1,
        }
        limits.update(kwargs)
        with pytest.raises(PipelineAuthorityCodecError) as raised:
            codec._own_json(graph, **limits)
        assert raised.value.reason == reason

    cycle: list[Any] = []
    cycle.append(cycle)
    with pytest.raises(PipelineAuthorityCodecError) as raised_cycle:
        codec._own_json(
            cycle,
            maximum_bytes=128,
            maximum_depth=8,
            maximum_nodes=8,
            maximum_text_bytes=8,
        )
    assert raised_cycle.value.reason == "json_cycle_invalid"


def test_owner_rejects_behavioral_containers_and_aggregate_over_budget_preflight() -> (
    None
):
    class BehavioralDict(dict):
        def __deepcopy__(self, _memo):
            raise AssertionError("behavioral deepcopy hook must never run")

    with pytest.raises(PipelineAuthorityCodecError) as raised:
        codec._own_json(
            BehavioralDict(a=1),
            maximum_bytes=128,
            maximum_depth=8,
            maximum_nodes=8,
            maximum_text_bytes=8,
        )
    assert raised.value.reason == "json_type_invalid"

    # Each member is legal on its own; the aggregate exceeds Start's 4 MiB
    # budget and must fail while the owner is incrementally walking it, before
    # canonical json.dumps is allowed to allocate another whole representation.
    candidate = _start()
    candidate["execution"]["parameters"] = {
        f"part-{index}": "x" * codec.MAX_START_TEXT_BYTES for index in range(17)
    }
    raised = _assert_invalid(candidate)
    assert raised.reason == "byte_budget_exceeded"


def test_forged_frozen_descriptor_cannot_hide_noncanonical_or_duplicate_json() -> None:
    frozen = parse_pipeline_control(_control())
    raw = frozen.descriptor_bytes.replace(
        b'{"action": "pause",', b'{"action": "pause", "action": "pause",', 1
    )
    forged = replace(
        frozen,
        descriptor_bytes=raw,
        descriptor_digest=hashlib.sha256(raw).digest(),
    )
    with pytest.raises(PipelineAuthorityCodecError):
        materialize_pipeline_descriptor(forged)


def test_control_parameters_property_returns_a_fresh_exact_copy() -> None:
    frozen = parse_pipeline_control(
        _control(
            action="retry_failed",
            parameters={"item_ids": ["item-1"]},
        )
    )
    first = frozen.parameters
    first["item_ids"].append("item-2")
    assert frozen.parameters == {"item_ids": ["item-1"]}
