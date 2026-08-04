from __future__ import annotations

import copy
import pickle
import subprocess
import sys
import textwrap
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sdk import (  # noqa: E402
    Actor,
    CommandRegistry,
    CommandSpec,
    CommitCoordinator,
    DocumentRegistry,
    FileCommitStore,
    OpLog,
    PipelineSupervisor,
    StageSpec,
    UndoController,
    WorkItem,
    WorkList,
    new_operation,
    register_builtin_commands,
)

import commit_coordinator as coordinator_module  # noqa: E402
import history_authority as history_module  # noqa: E402
import pipeline_authority_commit as authority_module  # noqa: E402
import pipeline_delegated_mutation as delegated_module  # noqa: E402
from pipeline_delegated_mutation import (  # noqa: E402
    PipelineDelegatedMutationError,
    consume_dormant_pipeline_delegated_mutation_request,
    prepare_dormant_pipeline_delegated_mutation_request,
)
from undo import _verify_history_replay_derivation  # noqa: E402


ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "done": {"type": "boolean"},
        "meta": {
            "type": "object",
            "properties": {"label": {"type": "string"}},
        },
    },
}
_HISTORY_BOOTSTRAP_OWNER = object()


class _Broker:
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    def publish_change(self, payload: dict[str, Any]) -> int:
        self.payloads.append(dict(payload))
        return len(self.payloads)


class _Publisher:
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    def publish(self, payload: dict[str, Any]) -> None:
        self.payloads.append(dict(payload))


def _runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    worker_actor_source: str = "ai",
) -> dict[str, Any]:
    declarations = DocumentRegistry()
    declarations.declare_entity_type(
        name="item",
        schema=ITEM_SCHEMA,
        mutation_policy={
            "schema": "ze.entity-mutation-policy.v1",
            "lifecycle": {
                "create": ["item.batch_generate"],
                "rename": ["item.batch_generate"],
                "soft_delete": [],
                "restore": [],
                "hard_remove": [],
            },
            "fields": {
                "title": ["item.batch_generate"],
                "done": ["item.batch_generate"],
                "meta": ["item.batch_generate"],
            },
            "cardinality": {"max_total": 2},
        },
    )
    declarations.set_collaboration_mode("with_save")
    declarations.set_save_policy("auto_commit")
    document = declarations.build_document(
        schema_version="1.0",
        id="doc-delegated",
    )
    registry = CommandRegistry()
    register_builtin_commands(registry)
    op_log = OpLog(tmp_path / "operations.ndjson")
    store = FileCommitStore(tmp_path / "document.json", op_log)
    publisher = _Publisher()
    broker = _Broker()
    lock = threading.RLock()
    coordinator = CommitCoordinator(
        document=document,
        store=store,
        publisher=publisher,
        lock=lock,
    )
    worker_actor = Actor(
        user_id="__system__",
        device_id="worker-1",
        source=worker_actor_source,
    )
    supervisor = PipelineSupervisor(
        doc=document,
        cmd_reg=registry,
        op_log=op_log,
        broker=broker,
        data_dir=tmp_path,
        actor=worker_actor,
        lock=lock,
        commit_coordinator=coordinator,
    )
    supervisor._spawn = lambda *_args, **_kwargs: pytest.fail(
        "dormant protected authority must not spawn a worker"
    )

    work_list = WorkList(
        run_id="run-delegated",
        name="Delegated check",
        items=[
            WorkItem(
                item_id=f"item-{index}",
                entity_type="item",
                entity_id=f"item-{index}",
                artifacts=(),
                payload={
                    "name": "Generating",
                    "seed_data": {
                        "title": "Generating",
                        "done": False,
                        "meta": {"label": f"item-{index}"},
                    },
                },
            )
            for index in range(2)
        ],
    )

    def _start(*, args, actor, source, doc):
        mutation, _state = supervisor.prepare_start(
            work_list=work_list,
            item_stage=StageSpec(
                id="generate",
                label="Generate",
                run=lambda ctx, values: {},
            ),
            trace_id="run-delegated",
            pipeline_id="item.batch_generate",
            start_command_id="item.batch_generate",
        )
        return mutation

    registry.register(CommandSpec(
        id="item.batch_generate",
        label="Generate items",
        kind="stage",
        touches=["item"],
        mutation_authority=True,
        execute=_start,
    ))
    document._validate_mutation_policy_commands(registry)
    undo = UndoController(op_log, document)
    history_authority = history_module.bind_history_authority(
        history_module._issue_history_bind_capability(
            bootstrap_owner=_HISTORY_BOOTSTRAP_OWNER,
        ),
        bootstrap_owner=_HISTORY_BOOTSTRAP_OWNER,
        document=document,
        op_log=op_log,
        undo_controller=undo,
        coordinator=coordinator,
        store=store,
        project_id=document.id,
        generation=1,
        runtime_revision="delegated-test-runtime",
        replay_verifier=_verify_history_replay_derivation,
    )

    captured: list[Any] = []
    real_reconcile = authority_module.reconcile_durable_pipeline_authority

    def _capture_reconcile(current_store, permit):
        result = real_reconcile(current_store, permit)
        captured.extend(result)
        return result

    with monkeypatch.context() as scoped:
        scoped.setattr(
            coordinator_module,
            "reconcile_durable_pipeline_authority",
            _capture_reconcile,
        )
        execution = coordinator.execute(
            lambda: registry.dispatch(
                "item.batch_generate",
                {},
                actor=Actor(user_id="business", device_id="browser", source="user"),
                source="user",
                doc=document,
            ),
            trace_id="run-delegated",
        )
    assert execution.is_committed
    assert execution.actions == ()
    assert len(captured) == 1
    assert supervisor._runs == {}
    assert not (tmp_path / ".pipelines/run-delegated/run.json").exists()
    start = execution.operations[0]
    start_hash = op_log.entries()[0]["self_hash"]
    return {
        "document": document,
        "registry": registry,
        "op_log": op_log,
        "store": store,
        "publisher": publisher,
        "broker": broker,
        "supervisor": supervisor,
        "worker_actor": worker_actor,
        "worker_source": "ai",
        "undo": undo,
        "history_authority": history_authority,
        "execution": execution,
        "attestation": captured[0],
        "start": start,
        "start_hash": start_hash,
        "tmp_path": tmp_path,
    }


def _prepare(runtime: dict[str, Any], **overrides: Any):
    entity = runtime["document"].find("item", "item-0")
    kwargs = {
        "attestation": runtime["attestation"],
        "store": runtime["store"],
        "document": runtime["document"],
        "registry": runtime["registry"],
        "run_id": "run-delegated",
        "pipeline_id": "item.batch_generate",
        "start_operation_id": runtime["start"].id,
        "start_operation_hash": runtime["start_hash"],
        "generation": 0,
        "item_id": "item-0",
        "stage_id": "generate",
        "operation_command_id": "item.batch_generate",
        "entity_type": "item",
        "entity_id": "item-0",
        "worker_actor": runtime["worker_actor"],
        "worker_source": runtime["worker_source"],
        "fields": ("done",),
        "lifecycle": (),
        "expected_entity_version": entity.version,
    }
    kwargs.update(overrides)
    return prepare_dormant_pipeline_delegated_mutation_request(**kwargs)


def _consume(runtime: dict[str, Any], request, **overrides: Any) -> None:
    kwargs = {
        "request": request,
        "attestation": runtime["attestation"],
        "store": runtime["store"],
        "document": runtime["document"],
        "registry": runtime["registry"],
        "worker_actor": runtime["worker_actor"],
        "worker_source": runtime["worker_source"],
    }
    kwargs.update(overrides)
    consume_dormant_pipeline_delegated_mutation_request(**kwargs)


def _document_state(document: Any) -> dict[str, Any]:
    return {
        "meta": document.meta_dict(),
        "schema": document.schema,
        "mutation_policies": document.mutation_policies,
        "config": document.config,
        "harvest_rules": document.harvest_rules,
        "created_at": document.created_at,
        "updated_at": document.updated_at,
        "entities": {
            entity_type: tuple(
                entity.to_dict()
                for entity in document.list(entity_type, include_deleted=True)
            )
            for entity_type in document.types()
        },
    }


def _commit_unrelated_operation(runtime: dict[str, Any]) -> None:
    operation = new_operation(
        command_id="audit.note",
        source="user",
        actor={
            "user_id": "business",
            "device_id": "browser",
            "source": "user",
        },
        document_id=runtime["document"].id,
        patches=(),
        reverse_patches=(),
    )
    execution = runtime["supervisor"]._commit_coordinator.execute(
        lambda: operation,
    )
    assert execution.is_committed


def test_dormant_request_rechecks_exact_scope_without_any_side_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path, monkeypatch)
    before_document = _document_state(runtime["document"])
    before_entries = runtime["op_log"].entries()
    before_events = list(runtime["publisher"].payloads)
    before_status = list(runtime["broker"].payloads)

    request = _prepare(runtime, fields=("meta.label",))
    assert request.protocol == "ze.pipeline-delegated-mutation.v1"
    assert request.execution_authority is False
    assert (request.item_id, request.entity_id, request.fields) == (
        "item-0",
        "item-0",
        ("meta.label",),
    )
    _consume(runtime, request)

    assert _document_state(runtime["document"]) == before_document
    assert runtime["op_log"].entries() == before_entries
    assert runtime["publisher"].payloads == before_events
    assert runtime["broker"].payloads == before_status
    assert runtime["supervisor"]._runs == {}
    assert not (tmp_path / ".pipelines/run-delegated/run.json").exists()
    with pytest.raises(PipelineDelegatedMutationError) as reused:
        _consume(runtime, request)
    assert reused.value.reason == "pipeline_delegated_request_consumed"


def test_new_request_fresh_reduces_history_after_unrelated_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path, monkeypatch)
    stale_request = _prepare(runtime)

    _commit_unrelated_operation(runtime)

    with pytest.raises(PipelineDelegatedMutationError) as stale:
        _consume(runtime, stale_request)
    assert stale.value.reason == "pipeline_delegated_attestation_facts_changed"

    fresh_request = _prepare(runtime)
    _consume(runtime, fresh_request)


def test_history_snapshot_failure_is_typed_and_freezes_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path, monkeypatch)
    (tmp_path / "operations.ndjson").unlink()

    with pytest.raises(PipelineDelegatedMutationError) as unavailable:
        _prepare(runtime)

    assert unavailable.value.code == "pipeline_authority_stale"
    assert unavailable.value.reason == "history_runtime_snapshot_unavailable"
    assert (
        history_module._authority_record(runtime["history_authority"]).state
        == "FROZEN"
    )


@pytest.mark.parametrize(
    "exception_factory",
    (
        lambda: KeyboardInterrupt("snapshot interrupted"),
        lambda: SystemExit(17),
        lambda: BaseException("snapshot aborted"),
    ),
    ids=("keyboard_interrupt", "system_exit", "base_exception"),
)
def test_history_snapshot_process_control_failure_is_typed_and_freezes_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exception_factory: Any,
) -> None:
    runtime = _runtime(tmp_path, monkeypatch)

    def fail_snapshot(_op_log: Any) -> Any:
        raise exception_factory()

    monkeypatch.setattr(history_module, "_oplog_snapshot", fail_snapshot)

    with pytest.raises(PipelineDelegatedMutationError) as unavailable:
        _prepare(runtime)

    assert unavailable.value.code == "pipeline_authority_stale"
    assert unavailable.value.reason == "history_runtime_snapshot_unavailable"
    typed_cause = unavailable.value.__cause__
    assert isinstance(typed_cause, history_module.HistoryAuthorityError)
    assert typed_cause.reason == "history_runtime_snapshot_unavailable"
    assert type(typed_cause.__cause__) is type(exception_factory())
    record = history_module._authority_record(runtime["history_authority"])
    assert record.state == "FROZEN"
    assert record.freeze_reason == "history_runtime_snapshot_unavailable"


def test_runtime_verifier_does_not_invert_registry_and_oplog_locks() -> None:
    protocol_dir = Path(__file__).resolve().parent
    script = textwrap.dedent(
        f"""
        import pathlib
        import sys
        import tempfile
        import threading

        import pytest

        sys.path.insert(0, {str(protocol_dir)!r})
        import test_pipeline_delegated_mutation as fixtures
        import history_authority as history_module
        import operation as operation_module

        monkeypatch = pytest.MonkeyPatch()
        runtime = fixtures._runtime(
            pathlib.Path(tempfile.mkdtemp()),
            monkeypatch,
        )
        append_reached_authorizer = threading.Event()
        verifier_reached_snapshot = threading.Event()
        append_errors = []
        verify_errors = []
        real_authorize = operation_module.authorize_history_append
        real_snapshot = history_module._oplog_snapshot

        def authorize(op_log, operation):
            append_reached_authorizer.set()
            if not verifier_reached_snapshot.wait(2):
                raise RuntimeError("verifier did not reach its snapshot")
            return real_authorize(op_log, operation)

        def snapshot(op_log):
            verifier_reached_snapshot.set()
            return real_snapshot(op_log)

        operation_module.authorize_history_append = authorize
        history_module._oplog_snapshot = snapshot
        operation = fixtures.new_operation(
            command_id="audit.note",
            source="user",
            actor={{
                "user_id": "business",
                "device_id": "browser",
                "source": "user",
            }},
            document_id=runtime["document"].id,
            patches=(),
            reverse_patches=(),
        )

        def append_directly():
            try:
                runtime["op_log"].append(operation)
            except BaseException as exc:
                append_errors.append(exc)

        def verify_runtime():
            try:
                fixtures._prepare(runtime)
            except BaseException as exc:
                verify_errors.append(exc)

        append_thread = threading.Thread(target=append_directly, daemon=True)
        verify_thread = threading.Thread(target=verify_runtime, daemon=True)
        append_thread.start()
        if not append_reached_authorizer.wait(2):
            raise SystemExit(21)
        verify_thread.start()
        append_thread.join(2)
        verify_thread.join(2)
        if append_thread.is_alive() or verify_thread.is_alive():
            raise SystemExit(22)
        if (
            len(append_errors) != 1
            or getattr(append_errors[0], "reason", "")
            != "history_append_outside_store"
        ):
            raise SystemExit(23)
        if verify_errors:
            raise SystemExit(24)
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, (
        completed.stdout + completed.stderr
    )


def test_request_is_noncopyable_nonserializable_and_concurrent_one_shot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path, monkeypatch)
    request = _prepare(runtime)
    for action in (
        lambda: copy.copy(request),
        lambda: copy.deepcopy(request),
        lambda: pickle.loads(pickle.dumps(request)),
    ):
        with pytest.raises(TypeError):
            action()

    def consume_once() -> str:
        try:
            _consume(runtime, request)
        except PipelineDelegatedMutationError as exc:
            return exc.reason
        return "ok"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = sorted(pool.map(lambda _index: consume_once(), range(2)))
    assert results == ["ok", "pipeline_delegated_request_consumed"]


def test_consumed_request_cannot_be_rearmed_by_mutating_detached_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path, monkeypatch)
    request = _prepare(runtime)
    entry = delegated_module._REQUESTS[id(request)]

    _consume(runtime, request)
    assert id(request) not in delegated_module._REQUESTS
    object.__setattr__(entry, "state", "prepared")

    with pytest.raises(PipelineDelegatedMutationError) as reused:
        _consume(runtime, request)
    assert reused.value.reason == "pipeline_delegated_request_consumed"


@pytest.mark.parametrize(
    ("overrides", "reason"),
    (
        ({"operation_command_id": "entity.update"},
         "pipeline_delegated_generic_command_forbidden"),
        ({"fields": ("unknown",)}, "pipeline_delegated_scope_exceeded"),
        ({"lifecycle": ("hard_remove",)}, "pipeline_delegated_scope_exceeded"),
        ({"lifecycle": ("create",)}, "pipeline_delegated_dynamic_creation_dormant"),
        ({"stage_id": "publish"}, "pipeline_delegated_stage_denied"),
        ({"item_id": "item-1"}, "pipeline_delegated_item_target_mismatch"),
        ({"entity_id": "item-1"}, "pipeline_delegated_item_target_mismatch"),
    ),
)
def test_scope_attacks_are_rejected_before_request_issuance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, Any],
    reason: str,
) -> None:
    runtime = _runtime(tmp_path, monkeypatch)
    before = runtime["op_log"].entries()
    with pytest.raises(PipelineDelegatedMutationError) as raised:
        _prepare(runtime, **overrides)
    assert raised.value.reason == reason
    assert runtime["op_log"].entries() == before
    assert runtime["supervisor"]._runs == {}


def test_public_reducer_projection_and_worker_source_mismatch_are_not_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path / "projection", monkeypatch)
    facts = authority_module._attestation_facts(runtime["attestation"])
    with pytest.raises(PipelineDelegatedMutationError) as projection:
        prepare_dormant_pipeline_delegated_mutation_request(
            facts.reduction,
            store=runtime["store"],
            document=runtime["document"],
            registry=runtime["registry"],
            run_id="run-delegated",
            pipeline_id="item.batch_generate",
            start_operation_id=runtime["start"].id,
            start_operation_hash=runtime["start_hash"],
            generation=0,
            item_id="item-0",
            stage_id="generate",
            operation_command_id="item.batch_generate",
            entity_type="item",
            entity_id="item-0",
            worker_actor=runtime["worker_actor"],
            worker_source=runtime["worker_source"],
            fields=("done",),
            expected_entity_version=runtime["document"].find("item", "item-0").version,
        )
    assert projection.value.reason == "pipeline_delegated_attestation_type_invalid"

    mismatch = _runtime(
        tmp_path / "source-mismatch",
        monkeypatch,
        worker_actor_source="system",
    )
    with pytest.raises(PipelineDelegatedMutationError) as source_error:
        _prepare(mismatch)
    assert source_error.value.reason == "pipeline_delegated_worker_source_mismatch"

    with pytest.raises(PipelineDelegatedMutationError) as actual_actor:
        _prepare(
            runtime,
            worker_actor=Actor(
                user_id="other-worker",
                device_id="worker-1",
                source="ai",
            ),
        )
    assert actual_actor.value.reason == "pipeline_delegated_worker_identity_mismatch"

    with pytest.raises(PipelineDelegatedMutationError) as actual_source:
        _prepare(runtime, worker_source="system")
    assert actual_source.value.reason == "pipeline_delegated_worker_identity_mismatch"

    request = _prepare(runtime)
    with pytest.raises(PipelineDelegatedMutationError) as changed_after_prepare:
        _consume(
            runtime,
            request,
            worker_actor=Actor(
                user_id="replacement-worker",
                device_id="worker-1",
                source="ai",
            ),
        )
    assert (
        changed_after_prepare.value.reason
        == "pipeline_delegated_worker_identity_mismatch"
    )


def test_issued_request_facts_cannot_be_rewritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path, monkeypatch)
    request = _prepare(runtime, fields=("done",))
    entry = delegated_module._REQUESTS[id(request)]
    object.__setattr__(entry.facts, "fields", ("title",))
    object.__setattr__(entry.facts, "field_roots", ("title",))

    with pytest.raises(PipelineDelegatedMutationError) as changed:
        _consume(runtime, request)
    assert changed.value.reason == "pipeline_delegated_request_facts_changed"

    malformed = _prepare(runtime, fields=("done",))
    object.__setattr__(
        delegated_module._REQUESTS[id(malformed)],
        "facts_integrity_hmac_sha256",
        7,
    )
    with pytest.raises(PipelineDelegatedMutationError) as malformed_tag:
        _consume(runtime, malformed)
    assert malformed_tag.value.reason == "pipeline_delegated_request_facts_changed"


def test_prepare_rejects_foreign_runtime_and_inactive_history_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path / "primary", monkeypatch)
    foreign = _runtime(tmp_path / "foreign", monkeypatch)

    with pytest.raises(PipelineDelegatedMutationError) as wrong_document:
        _prepare(runtime, document=foreign["document"])
    assert wrong_document.value.reason == "pipeline_delegated_runtime_binding_mismatch"

    try:
        history_module._authority_record(runtime["history_authority"]).freeze(
            "history_test_frozen",
            "injected frozen authority",
        )
    except history_module.HistoryAuthorityError:
        pass
    with pytest.raises(PipelineDelegatedMutationError) as frozen:
        _prepare(runtime)
    assert frozen.value.reason == "history_authority_frozen"


def test_prepare_rejects_jointly_replaced_history_epoch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path, monkeypatch)
    forged_epoch = object()
    object.__setattr__(
        runtime["document"],
        "_history_authority_epoch",
        forged_epoch,
    )
    object.__setattr__(
        runtime["undo"],
        "_history_authority_epoch",
        forged_epoch,
    )

    with pytest.raises(PipelineDelegatedMutationError) as rejected:
        _prepare(runtime)
    assert rejected.value.reason == "history_runtime_graph_mismatch"


def test_malformed_lifecycle_is_one_stable_typed_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path, monkeypatch)
    with pytest.raises(PipelineDelegatedMutationError) as malformed:
        _prepare(runtime, lifecycle=([],))
    assert malformed.value.reason == "pipeline_delegated_lifecycle_invalid"


def test_request_rechecks_entity_head_and_current_policy_on_consume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path / "entity", monkeypatch)
    request = _prepare(runtime)
    entity = runtime["document"].find("item", "item-0")
    object.__setattr__(entity, "_version", entity.version + 1)
    with pytest.raises(PipelineDelegatedMutationError) as changed:
        _consume(runtime, request)
    assert changed.value.reason == "pipeline_delegated_entity_head_changed"

    policy_runtime = _runtime(tmp_path / "policy", monkeypatch)
    policy_request = _prepare(policy_runtime)
    object.__getattribute__(
        policy_runtime["document"], "_mutation_policies"
    )["item"]["fields"]["done"].append("item.other")
    with pytest.raises(PipelineDelegatedMutationError) as policy_changed:
        _consume(policy_runtime, policy_request)
    assert policy_changed.value.reason == "mutation_policy_registry_seal_incomplete"


def test_cross_store_document_registry_and_stale_attestation_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path / "primary", monkeypatch)
    foreign = _runtime(tmp_path / "foreign", monkeypatch)

    request = _prepare(runtime)
    with pytest.raises(PipelineDelegatedMutationError) as wrong_store:
        _consume(runtime, request, store=foreign["store"])
    assert wrong_store.value.reason == "pipeline_delegated_request_binding_mismatch"

    request = _prepare(runtime)
    with pytest.raises(PipelineDelegatedMutationError) as wrong_document:
        _consume(runtime, request, document=foreign["document"])
    assert wrong_document.value.reason == "pipeline_delegated_request_binding_mismatch"

    request = _prepare(runtime)
    with pytest.raises(PipelineDelegatedMutationError) as wrong_registry:
        _consume(runtime, request, registry=foreign["registry"])
    assert wrong_registry.value.reason == "pipeline_delegated_request_binding_mismatch"

    request = _prepare(runtime)
    authority_module._reset_after_fork()
    with pytest.raises(PipelineDelegatedMutationError) as stale:
        _consume(runtime, request)
    assert stale.value.code == "pipeline_authority_stale"


def test_request_itself_is_invalid_after_fork_reset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path, monkeypatch)
    request = _prepare(runtime)
    delegated_module._reset_after_fork()
    with pytest.raises(PipelineDelegatedMutationError) as stale:
        _consume(runtime, request)
    assert stale.value.reason == "pipeline_delegated_request_stale"
