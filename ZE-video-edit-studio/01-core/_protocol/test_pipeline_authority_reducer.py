from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest


HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import pipeline_authority_reducer as reducer_module
from operation_record_codec import ZERO_OPERATION_HASH
from pipeline_authority_codec import (
    MAX_CONTROL_BYTES,
    MAX_START_BYTES,
    build_pipeline_control_descriptor,
    build_pipeline_start_descriptor,
)
from pipeline_authority_reducer import (
    PIPELINE_AUTHORITY_REDUCTION_PROTOCOL,
    PipelineAuthorityReductionError,
    reduce_fresh_pipeline_history,
)


DOCUMENT_ID = "doc-1"
RUN_ID = "run-1"
PIPELINE_ID = "asset-pipeline"
START_ID = "1" * 32
ADD_ID = "2" * 32
ACTOR = {"user_id": "user-1", "device_id": "browser-1", "source": "user"}
SYSTEM_ACTOR = {
    "user_id": "__pipeline__",
    "device_id": "worker-1",
    "source": "ai",
}


def _policy() -> dict[str, Any]:
    return {
        "schema": "ze.pipeline-document-policy.v1",
        "entity_policies": [
            {
                "entity_type": "asset",
                "entity_schema_digest": "b" * 64,
                "policy": {
                    "schema": "ze.entity-mutation-policy.v1",
                    "lifecycle": {
                        "create": ["asset.generate"],
                        "rename": [],
                        "soft_delete": [],
                        "restore": [],
                        "hard_remove": [],
                    },
                    "fields": {"status": ["asset.generate"]},
                },
            }
        ],
    }


def _start_descriptor() -> dict[str, Any]:
    return build_pipeline_start_descriptor(
        document_id=DOCUMENT_ID,
        run_id=RUN_ID,
        pipeline_id=PIPELINE_ID,
        start_command_id="asset.generate",
        start_operation_id=START_ID,
        work_list={
            "run_id": RUN_ID,
            "name": "Generate",
            "items": [
                {
                    "item_id": "item-1",
                    "entity_type": "asset",
                    "entity_id": "asset-1",
                    "artifacts": [],
                    "payload": {},
                }
            ],
        },
        stages=[
            {
                "id": "generate",
                "label": "Generate",
                "command_id": "asset.generate",
                "produces": [],
                "resource_scopes": [],
                "resource_costs": {},
                "retry_safe": True,
                "interrupt_safe": True,
                "pause_after": False,
                "input_ports": [],
                "output_ports": [],
            }
        ],
        placeholder_bindings=[
            {
                "item_id": "item-1",
                "entity_type": "asset",
                "entity_id": "asset-1",
                "operation_id": ADD_ID,
            }
        ],
        initial_operation_ids=[START_ID, ADD_ID],
        concurrency=1,
        max_retries=0,
        parameters={},
        root_command_digest="a" * 64,
        document_policy=_policy(),
        worker_actor=SYSTEM_ACTOR,
        worker_source="ai",
        targets=[
            {
                "entity_type": "asset",
                "entity_id": "asset-1",
                "fields": ["status"],
                "lifecycle": ["create"],
            }
        ],
    )


def _operation(
    operation_id: str,
    command_id: str,
    *,
    kind: str = "native",
    source: str = "user",
    actor: dict[str, Any] | None = None,
    patches: list[dict[str, Any]] | None = None,
    reverse_patches: list[dict[str, Any]] | None = None,
    pipeline: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": operation_id,
        "command_id": command_id,
        "source": source,
        "actor": dict(ACTOR if actor is None else actor),
        "ts": 1.0,
        "patches": list(patches or []),
        "reverse_patches": list(reverse_patches or []),
        "group_id": "",
        "trace_id": "f" * 32,
        "kind": kind,
        "error": None,
        "document_id": DOCUMENT_ID,
    }
    if pipeline is not None:
        body["pipeline"] = pipeline
    return body


def _start_body(*, kind: str = "stage") -> dict[str, Any]:
    return _operation(
        START_ID,
        "asset.generate",
        kind=kind,
        pipeline=_start_descriptor(),
    )


def _add_body(*, reverse_op: str = "delete", kind: str = "native") -> dict[str, Any]:
    snapshot = {
        "id": "asset-1",
        "type": "asset",
        "name": "",
        "data": {"status": "pending"},
        "version": 1,
        "deleted": False,
        "read_only": False,
        "schema": None,
        "provenance": {},
        "created_at": 1.0,
        "updated_at": 1.0,
    }
    create = {
        "entity_type": "asset",
        "entity_id": "asset-1",
        "field": "__entity__",
        "op": "create",
        "before": None,
        "after": snapshot,
        "position": 0,
    }
    delete = {
        "entity_type": "asset",
        "entity_id": "asset-1",
        "field": "__entity__",
        "op": reverse_op,
        "before": snapshot,
        "after": None,
        "position": 0,
    }
    return _operation(
        ADD_ID,
        "entity.add",
        kind=kind,
        patches=[create],
        reverse_patches=[delete],
    )


def _control_body(
    operation_id: str,
    *,
    start_hash: str,
    action: str,
    parameters: dict[str, Any],
    expected_generation: int,
    expected_state: str,
    command_id: str | None = None,
) -> dict[str, Any]:
    command_ids = {
        "pause": "pipeline.pause",
        "interrupt": "pipeline.interrupt",
        "cancel": "pipeline.cancel",
        "discard": "pipeline.discard",
        "resume": "pipeline.resume",
        "retry_failed": "pipeline.retry_failed",
        "rerun_step": "pipeline.rerun_step",
        "run_from_step": "pipeline.run_from_step",
        "set_breakpoint": "pipeline.set_breakpoint",
    }
    descriptor = build_pipeline_control_descriptor(
        document_id=DOCUMENT_ID,
        run_id=RUN_ID,
        pipeline_id=PIPELINE_ID,
        control_operation_id=operation_id,
        start_operation_id=START_ID,
        start_operation_hash=start_hash,
        action=action,
        parameters=parameters,
        expected_generation=expected_generation,
        expected_state=expected_state,
    )
    return _operation(
        operation_id,
        command_id or command_ids[action],
        kind="stage",
        source="system",
        actor=SYSTEM_ACTOR,
        pipeline=descriptor,
    )


def _chain(*bodies: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    previous = ZERO_OPERATION_HASH
    rows = []
    for body in bodies:
        encoded = json.dumps(
            body,
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
        self_hash = hashlib.sha256(previous.encode("ascii") + encoded).hexdigest()
        rows.append({"op": body, "prev_hash": previous, "self_hash": self_hash})
        previous = self_hash
    return tuple(rows)


def _reduce(entries: tuple[dict[str, Any], ...]):
    return reduce_fresh_pipeline_history(
        entries,
        head_count=len(entries),
        head_hash=entries[-1]["self_hash"] if entries else ZERO_OPERATION_HASH,
        document_id=DOCUMENT_ID,
        run_id=RUN_ID,
        pipeline_id=PIPELINE_ID,
    )


def _code(entries: tuple[dict[str, Any], ...]) -> str:
    with pytest.raises(PipelineAuthorityReductionError) as caught:
        _reduce(entries)
    return caught.value.code


def test_reduces_v2_start_after_legacy_prefix_without_reinterpreting_prefix() -> None:
    legacy = _operation("0" * 32, "legacy.edit")
    entries = _chain(legacy, _start_body(), _add_body())

    result = _reduce(entries)

    assert result.protocol == PIPELINE_AUTHORITY_REDUCTION_PROTOCOL
    assert result.start_operation_id == START_ID
    assert result.start_operation_hash == entries[1]["self_hash"]
    assert result.initial_operation_ids == (START_ID, ADD_ID)
    assert result.generation == 0
    assert result.state == "paused"
    assert result.reconciliation_pending is False
    assert result.terminal is False
    assert result.start_command_id == "asset.generate"
    assert result.root_actor == ("user-1", "browser-1", "user")
    assert result.root_source == "user"
    assert result.root_command_digest == "a" * 64
    assert len(result.document_policy_digest) == 64
    assert result.worker_actor == ("__pipeline__", "worker-1", "ai")
    assert result.worker_source == "ai"
    assert result.work_item_ids == ("item-1",)
    assert (
        result.work_item_bindings[0].item_id,
        result.work_item_bindings[0].entity_type,
        result.work_item_bindings[0].entity_id,
    ) == ("item-1", "asset", "asset-1")
    assert result.allowed_stage_ids == ("generate",)
    assert result.placeholder_bindings[0].operation_id == ADD_ID
    assert result.delegated_targets[0].fields == ("status",)
    assert result.delegated_targets[0].lifecycle == ("create",)
    assert result.delegated_creation_scopes == ()
    assert result.active_breakpoint_stage_ids == ()


def test_reduced_authority_and_nested_facts_expose_only_detached_dicts() -> None:
    result = _reduce(_chain(_start_body(), _add_body()))
    result_dict = result.__dict__
    result_dict["state"] = "terminal"
    assert result.state == "paused"
    assert result.__dict__["state"] == "paused"

    placeholder = result.placeholder_bindings[0]
    placeholder_dict = placeholder.__dict__
    placeholder_dict["entity_id"] = "attacker-controlled"
    assert placeholder.entity_id == "asset-1"
    assert placeholder.__dict__["entity_id"] == "asset-1"

    work_item = result.work_item_bindings[0]
    work_item_dict = work_item.__dict__
    work_item_dict["entity_id"] = "attacker-controlled"
    assert work_item.entity_id == "asset-1"
    assert work_item.__dict__["entity_id"] == "asset-1"

    target = result.delegated_targets[0]
    target_dict = target.__dict__
    target_dict["fields"] = ("attacker-controlled",)
    assert target.fields == ("status",)
    assert target.__dict__["fields"] == ("status",)

    vars(result)["state"] = "interrupted"
    assert result.state == "paused"

    with pytest.raises((AttributeError, TypeError)):
        result.state = "terminal"


def test_delegated_creation_scope_is_projected_as_a_detached_frozen_fact() -> None:
    start = _start_body()
    start["pipeline"]["delegation"]["creation_scopes"] = [
        {
            "entity_type": "asset",
            "fields": ["status"],
            "max_per_operation": 1,
            "max_per_run": 2,
        }
    ]

    result = _reduce(_chain(start, _add_body()))

    assert len(result.delegated_creation_scopes) == 1
    scope = result.delegated_creation_scopes[0]
    assert scope.entity_type == "asset"
    assert scope.fields == ("status",)
    assert scope.max_per_operation == 1
    assert scope.max_per_run == 2
    detached = scope.__dict__
    detached["max_per_run"] = 999
    assert scope.max_per_run == 2
    assert scope.__dict__["max_per_run"] == 2


def test_v1_carrier_is_permanently_untrusted_authority() -> None:
    legacy_start = _operation(
        START_ID,
        "asset.generate",
        kind="stage",
        pipeline={
            "schema": "ze.pipeline-start.v1",
            "document_id": DOCUMENT_ID,
            "run_id": RUN_ID,
            "pipeline_id": PIPELINE_ID,
        },
    )
    assert _code(_chain(legacy_start)) == "pipeline_start_missing"

    legacy_control = _operation(
        "3" * 32,
        "pipeline.resume",
        kind="stage",
        pipeline={
            "schema": "ze.pipeline-control.v1",
            "document_id": DOCUMENT_ID,
            "run_id": RUN_ID,
            "pipeline_id": PIPELINE_ID,
        },
    )
    result = _reduce(_chain(_start_body(), _add_body(), legacy_control))
    assert result.generation == 0
    assert result.state == "paused"


def test_start_must_be_unique_and_its_members_a_continuous_prefix() -> None:
    duplicate = _chain(_start_body(), _add_body(), _start_body(), _add_body())
    assert _code(duplicate) == "pipeline_start_ambiguous"

    inserted = _operation("4" * 32, "legacy.edit")
    discontinuous = _chain(_start_body(), inserted, _add_body())
    assert _code(discontinuous) == "pipeline_start_prefix_incomplete"

    truncated = _chain(_start_body())
    assert _code(truncated) == "pipeline_start_prefix_incomplete"


def test_start_and_placeholder_rows_require_current_exact_operation_semantics() -> None:
    invalid_descriptor = _start_descriptor()
    invalid_descriptor["unexpected"] = True
    invalid_start = _operation(
        START_ID,
        "asset.generate",
        kind="stage",
        pipeline=invalid_descriptor,
    )
    assert _code(_chain(invalid_start, _add_body())) == (
        "pipeline_start_identity_mismatch"
    )
    assert _code(_chain(_start_body(kind="native"), _add_body())) == (
        "pipeline_start_identity_mismatch"
    )
    assert _code(_chain(_start_body(), _add_body(reverse_op="soft_delete"))) == (
        "pipeline_start_identity_mismatch"
    )
    assert _code(_chain(_start_body(), _add_body(kind="rollback"))) == (
        "pipeline_start_identity_mismatch"
    )

    missing_position = _add_body()
    del missing_position["patches"][0]["position"]
    del missing_position["reverse_patches"][0]["position"]
    assert _code(_chain(_start_body(), missing_position)) == (
        "pipeline_start_identity_mismatch"
    )

    boolean_position = _add_body()
    boolean_position["patches"][0]["position"] = True
    boolean_position["reverse_patches"][0]["position"] = True
    assert _code(_chain(_start_body(), boolean_position)) == (
        "pipeline_start_prefix_incomplete"
    )


@pytest.mark.parametrize(
    ("field", "replacement"),
    (("deleted", 0), ("version", 1.0)),
)
def test_placeholder_reverse_snapshot_uses_json_type_exact_identity(
    field: str,
    replacement: Any,
) -> None:
    add = _add_body()
    add["reverse_patches"][0]["before"] = dict(
        add["reverse_patches"][0]["before"]
    )
    add["reverse_patches"][0]["before"][field] = replacement

    assert _code(_chain(_start_body(), add)) == (
        "pipeline_start_identity_mismatch"
    )


def test_start_closure_binds_actor_and_dispatch_source_as_independent_axes() -> None:
    # Actor.source and Operation.source are intentionally different on a legal
    # framework path; each closure member must preserve both exact values.
    start = _start_body()
    start["source"] = "system"
    start["actor"] = dict(SYSTEM_ACTOR)
    add = _add_body()
    add["source"] = "system"
    add["actor"] = dict(SYSTEM_ACTOR)
    result = _reduce(_chain(start, add))
    assert result.state == "paused"

    mismatched = _add_body()
    mismatched["source"] = "ai"
    mismatched["actor"] = dict(SYSTEM_ACTOR)
    assert _code(_chain(start, mismatched)) == "pipeline_start_identity_mismatch"


def test_rejects_unstable_head_and_tampered_chain_before_reduction() -> None:
    entries = _chain(_start_body(), _add_body())
    with pytest.raises(PipelineAuthorityReductionError) as caught:
        reduce_fresh_pipeline_history(
            entries,
            head_count=len(entries),
            head_hash="f" * 64,
            document_id=DOCUMENT_ID,
            run_id=RUN_ID,
            pipeline_id=PIPELINE_ID,
        )
    assert caught.value.code == "pipeline_authority_unavailable"

    tampered = [dict(row) for row in entries]
    tampered[1]["self_hash"] = "e" * 64
    assert _code(tuple(tampered)) == "pipeline_authority_unavailable"


def test_unowned_schema_and_hashes_never_run_comparison_hooks() -> None:
    class HostileScalar:
        def __init__(self) -> None:
            self.comparisons = 0

        def __eq__(self, _other: Any) -> bool:
            self.comparisons += 1
            raise RuntimeError("caller-controlled-equality")

        def __ne__(self, _other: Any) -> bool:
            self.comparisons += 1
            raise RuntimeError("caller-controlled-inequality")

    schema = HostileScalar()
    schema_entry = {
        "op": {"pipeline": {"schema": schema}},
        "prev_hash": ZERO_OPERATION_HASH,
        "self_hash": ZERO_OPERATION_HASH,
    }
    with pytest.raises(PipelineAuthorityReductionError) as schema_error:
        _reduce((schema_entry,))
    assert schema_error.value.code == "pipeline_authority_unavailable"
    assert schema.comparisons == 0

    previous_hash = HostileScalar()
    previous_entry = {
        "op": {},
        "prev_hash": previous_hash,
        "self_hash": ZERO_OPERATION_HASH,
    }
    with pytest.raises(PipelineAuthorityReductionError) as previous_error:
        _reduce((previous_entry,))
    assert previous_error.value.code == "pipeline_authority_unavailable"
    assert previous_error.value.reason == "history_previous_hash_invalid"
    assert previous_hash.comparisons == 0

    self_hash = HostileScalar()
    self_entry = {
        "op": {},
        "prev_hash": ZERO_OPERATION_HASH,
        "self_hash": self_hash,
    }
    with pytest.raises(PipelineAuthorityReductionError) as self_error:
        reduce_fresh_pipeline_history(
            (self_entry,),
            head_count=1,
            head_hash=ZERO_OPERATION_HASH,
            document_id=DOCUMENT_ID,
            run_id=RUN_ID,
            pipeline_id=PIPELINE_ID,
        )
    assert self_error.value.code == "pipeline_authority_unavailable"
    assert self_error.value.reason == "history_self_hash_invalid"
    assert self_hash.comparisons == 0


def test_unowned_mapping_keys_are_rejected_without_hash_or_equality_hooks() -> None:
    class HostileKey:
        def __init__(self) -> None:
            self.hash_calls = 0
            self.equality_calls = 0

        def __hash__(self) -> int:
            self.hash_calls += 1
            return hash("pipeline")

        def __eq__(self, _other: Any) -> bool:
            self.equality_calls += 1
            raise RuntimeError("caller-controlled-key-equality")

    for nested, expected_reason in (
        (False, "history_operation_body_invalid"),
        (True, "pipeline_descriptor_shape_invalid"),
    ):
        key = HostileKey()
        if nested:
            body = {"pipeline": {key: "value"}}
        else:
            body = {key: "value"}
        # Ignore the unavoidable hash used by the caller while constructing
        # its own dict.  The reducer must not invoke either hook afterwards.
        key.hash_calls = 0
        entry = {
            "op": body,
            "prev_hash": ZERO_OPERATION_HASH,
            "self_hash": ZERO_OPERATION_HASH,
        }
        with pytest.raises(PipelineAuthorityReductionError) as caught:
            _reduce((entry,))
        assert caught.value.code == "pipeline_authority_unavailable"
        assert caught.value.reason == expected_reason
        assert key.hash_calls == 0
        assert key.equality_calls == 0


@pytest.mark.parametrize(
    ("kind", "maximum_bytes", "expected_code"),
    (
        ("start", MAX_START_BYTES, "pipeline_start_identity_mismatch"),
        ("control", MAX_CONTROL_BYTES, "pipeline_control_invalid"),
    ),
)
def test_v2_descriptor_budget_runs_before_operation_body_deepcopy(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    maximum_bytes: int,
    expected_code: str,
) -> None:
    if kind == "start":
        body = _start_body()
        body["pipeline"]["execution"]["parameters"] = {
            "oversized": "x" * (maximum_bytes + 1)
        }
    else:
        body = _control_body(
            "3" * 32,
            start_hash="a" * 64,
            action="pause",
            parameters={},
            expected_generation=0,
            expected_state="paused",
        )
        body["pipeline"]["parameters"] = {
            "oversized": "x" * (maximum_bytes + 1)
        }
    entries = _chain(body)

    def fail_if_operation_copy_starts(_body: Any):
        raise AssertionError("Operation deepcopy ran before authority preflight")

    monkeypatch.setattr(
        reducer_module,
        "freeze_operation_body",
        fail_if_operation_copy_starts,
    )
    with pytest.raises(PipelineAuthorityReductionError) as caught:
        _reduce(entries)

    assert caught.value.code == expected_code


def test_control_generation_state_command_and_start_hash_are_bound() -> None:
    base = _chain(_start_body(), _add_body())
    pause = _control_body(
        "3" * 32,
        start_hash=base[0]["self_hash"],
        action="pause",
        parameters={},
        expected_generation=0,
        expected_state="paused",
    )
    result = _reduce(_chain(_start_body(), _add_body(), pause))
    assert result.generation == 1
    assert result.state == "paused"

    stale = _control_body(
        "4" * 32,
        start_hash=base[0]["self_hash"],
        action="pause",
        parameters={},
        expected_generation=1,
        expected_state="paused",
    )
    assert _code(_chain(_start_body(), _add_body(), stale)) == (
        "pipeline_control_invalid"
    )

    wrong_command = _control_body(
        "5" * 32,
        start_hash=base[0]["self_hash"],
        action="pause",
        parameters={},
        expected_generation=0,
        expected_state="paused",
        command_id="pipeline.resume",
    )
    assert _code(_chain(_start_body(), _add_body(), wrong_command)) == (
        "pipeline_control_invalid"
    )

    wrong_hash = _control_body(
        "6" * 32,
        start_hash="b" * 64,
        action="pause",
        parameters={},
        expected_generation=0,
        expected_state="paused",
    )
    assert _code(_chain(_start_body(), _add_body(), wrong_hash)) == (
        "pipeline_start_digest_mismatch"
    )


def test_control_scope_cannot_escape_start_items_or_stages() -> None:
    base = _chain(_start_body(), _add_body())
    unknown_item = _control_body(
        "3" * 32,
        start_hash=base[0]["self_hash"],
        action="retry_failed",
        parameters={"item_ids": ["item-outside"]},
        expected_generation=0,
        expected_state="paused",
    )
    assert _code(_chain(_start_body(), _add_body(), unknown_item)) == (
        "pipeline_control_invalid"
    )

    unknown_stage = _control_body(
        "4" * 32,
        start_hash=base[0]["self_hash"],
        action="run_from_step",
        parameters={"stage_id": "outside", "item_id": "item-1"},
        expected_generation=0,
        expected_state="paused",
    )
    assert _code(_chain(_start_body(), _add_body(), unknown_stage)) == (
        "pipeline_control_invalid"
    )


def test_breakpoints_reduce_as_ordered_durable_restrictions_without_changing_state() -> None:
    start = _start_body()
    second_stage = dict(start["pipeline"]["stages"][0])
    second_stage.update(id="publish", label="Publish")
    start["pipeline"]["stages"].append(second_stage)
    start_hash = _chain(start, _add_body())[0]["self_hash"]

    set_generate = _control_body(
        "3" * 32,
        start_hash=start_hash,
        action="set_breakpoint",
        parameters={"stage_id": "generate", "halt": True},
        expected_generation=1,
        expected_state="paused",
    )
    set_publish = _control_body(
        "4" * 32,
        start_hash=start_hash,
        action="set_breakpoint",
        parameters={"stage_id": "publish", "halt": True},
        expected_generation=0,
        expected_state="paused",
    )
    clear_generate = _control_body(
        "5" * 32,
        start_hash=start_hash,
        action="set_breakpoint",
        parameters={"stage_id": "generate", "halt": False},
        expected_generation=2,
        expected_state="paused",
    )
    rearm_generate = _control_body(
        "6" * 32,
        start_hash=start_hash,
        action="set_breakpoint",
        parameters={"stage_id": "generate", "halt": True},
        expected_generation=3,
        expected_state="paused",
    )

    result = _reduce(
        _chain(
            start,
            _add_body(),
            set_publish,
            set_generate,
            clear_generate,
            rearm_generate,
        )
    )

    assert result.generation == 4
    assert result.state == "paused"
    assert result.reconciliation_pending is True
    assert result.active_breakpoint_stage_ids == ("generate", "publish")


def test_clearing_breakpoint_preserves_state_and_requires_reconciliation() -> None:
    base = _chain(_start_body(), _add_body())
    start_hash = base[0]["self_hash"]
    arm = _control_body(
        "3" * 32,
        start_hash=start_hash,
        action="set_breakpoint",
        parameters={"stage_id": "generate", "halt": True},
        expected_generation=0,
        expected_state="paused",
    )
    clear = _control_body(
        "4" * 32,
        start_hash=start_hash,
        action="set_breakpoint",
        parameters={"stage_id": "generate", "halt": False},
        expected_generation=1,
        expected_state="paused",
    )

    result = _reduce(_chain(_start_body(), _add_body(), arm, clear))

    assert result.generation == 2
    assert result.state == "paused"
    assert result.reconciliation_pending is True
    assert result.active_breakpoint_stage_ids == ()


@pytest.mark.parametrize("terminal_action", ["cancel", "discard"])
def test_terminal_control_clears_durable_breakpoints(terminal_action: str) -> None:
    base = _chain(_start_body(), _add_body())
    start_hash = base[0]["self_hash"]
    breakpoint = _control_body(
        "3" * 32,
        start_hash=start_hash,
        action="set_breakpoint",
        parameters={"stage_id": "generate", "halt": True},
        expected_generation=0,
        expected_state="paused",
    )
    terminal = _control_body(
        "4" * 32,
        start_hash=start_hash,
        action=terminal_action,
        parameters={},
        expected_generation=1,
        expected_state="paused",
    )

    result = _reduce(_chain(_start_body(), _add_body(), breakpoint, terminal))

    assert result.terminal is True
    assert result.active_breakpoint_stage_ids == ()


def test_positive_control_uncertainty_is_sticky_across_restrictive_controls() -> None:
    base = _chain(_start_body(), _add_body())
    start_hash = base[0]["self_hash"]
    resume = _control_body(
        "3" * 32,
        start_hash=start_hash,
        action="resume",
        parameters={},
        expected_generation=0,
        expected_state="paused",
    )
    pause = _control_body(
        "4" * 32,
        start_hash=start_hash,
        action="pause",
        parameters={},
        expected_generation=1,
        expected_state="reconcile_required",
    )
    interrupt = _control_body(
        "5" * 32,
        start_hash=start_hash,
        action="interrupt",
        parameters={},
        expected_generation=2,
        expected_state="paused",
    )

    result = _reduce(_chain(_start_body(), _add_body(), resume, pause, interrupt))
    assert result.state == "interrupted"
    assert result.generation == 3
    assert result.reconciliation_pending is True
    assert result.reconcile_required is True


def test_explicit_later_positive_control_can_continue_without_erasing_taint() -> None:
    base = _chain(_start_body(), _add_body())
    start_hash = base[0]["self_hash"]
    first = _control_body(
        "3" * 32,
        start_hash=start_hash,
        action="resume",
        parameters={},
        expected_generation=0,
        expected_state="paused",
    )
    pause = _control_body(
        "4" * 32,
        start_hash=start_hash,
        action="pause",
        parameters={},
        expected_generation=1,
        expected_state="reconcile_required",
    )
    explicit_again = _control_body(
        "5" * 32,
        start_hash=start_hash,
        action="resume",
        parameters={},
        expected_generation=2,
        expected_state="paused",
    )
    result = _reduce(
        _chain(_start_body(), _add_body(), first, pause, explicit_again)
    )
    assert result.generation == 3
    assert result.state == "reconcile_required"
    assert result.reconciliation_pending is True


@pytest.mark.parametrize("terminal_action", ["cancel", "discard"])
def test_terminal_control_dominates_positive_uncertainty(terminal_action: str) -> None:
    base = _chain(_start_body(), _add_body())
    start_hash = base[0]["self_hash"]
    resume = _control_body(
        "3" * 32,
        start_hash=start_hash,
        action="resume",
        parameters={},
        expected_generation=0,
        expected_state="paused",
    )
    terminal = _control_body(
        "4" * 32,
        start_hash=start_hash,
        action=terminal_action,
        parameters={},
        expected_generation=1,
        expected_state="reconcile_required",
    )
    result = _reduce(_chain(_start_body(), _add_body(), resume, terminal))
    assert result.terminal is True
    assert result.terminal_action == terminal_action
    assert result.reconciliation_pending is False


def test_control_after_terminal_and_reused_control_identity_fail_closed() -> None:
    base = _chain(_start_body(), _add_body())
    start_hash = base[0]["self_hash"]
    cancel = _control_body(
        "3" * 32,
        start_hash=start_hash,
        action="cancel",
        parameters={},
        expected_generation=0,
        expected_state="paused",
    )
    after = _control_body(
        "4" * 32,
        start_hash=start_hash,
        action="pause",
        parameters={},
        expected_generation=1,
        expected_state="terminal",
    )
    assert _code(_chain(_start_body(), _add_body(), cancel, after)) == (
        "pipeline_control_invalid"
    )

    first = _control_body(
        "5" * 32,
        start_hash=start_hash,
        action="pause",
        parameters={},
        expected_generation=0,
        expected_state="paused",
    )
    reused = _control_body(
        "5" * 32,
        start_hash=start_hash,
        action="pause",
        parameters={},
        expected_generation=1,
        expected_state="paused",
    )
    assert _code(_chain(_start_body(), _add_body(), first, reused)) == (
        "pipeline_control_invalid"
    )
