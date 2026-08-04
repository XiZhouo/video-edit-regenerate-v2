"""W03 pipeline 提交边界：start batch、生命周期 deferred action 与重放护栏。"""

from __future__ import annotations

import json
import sys
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sdk import (  # noqa: E402
    ActionStatus,
    Actor,
    CommandMutation,
    CommandRegistry,
    CommandSpec,
    CommitCoordinator,
    CommitFailure,
    CommitReceipt,
    CommitStage,
    CommitStatus,
    Document,
    DocumentRegistry,
    DurabilityFacts,
    DurabilityState,
    EventStatus,
    FileCommitStore,
    OpLog,
    PipelineSupervisor,
    StageSpec,
    WorkItem,
    WorkList,
    ZeError,
    new_operation,
    register_builtin_commands,
    register_pipeline_commands,
)
from pipeline_authority_reducer import reduce_fresh_pipeline_history  # noqa: E402


ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "done": {"type": "boolean"},
    },
}


class RecordingStore:
    """只记录一次 logical store 调用，可返回 committed 或 indeterminate。"""

    def __init__(self, log: list[str], *, outcome: str = "committed") -> None:
        self.log = log
        self.outcome = outcome
        self.intents: list[Any] = []
        self.receipts: list[CommitReceipt] = []

    def commit(self, intent):
        self.log.append("store")
        self.intents.append(intent)
        # 测试 store 用版本推进模拟 Document 已经形成一次 durability acknowledgement。
        intent.document._v += 1
        if self.outcome == "committed":
            receipt = CommitReceipt.committed(
                commit_id=intent.commit_id,
                document_id=intent.document_id,
                operation_ids=intent.operation_ids,
                trace_id=intent.trace_id,
                save_policy=intent.save_policy,
                document_version=intent.document.version,
            )
        else:
            if self.outcome != "indeterminate":
                raise AssertionError(f"unsupported test outcome: {self.outcome}")
            failure = CommitFailure(
                stage=CommitStage.OPLOG,
                code="oplog_outcome_unknown",
                message="document durable but oplog outcome unknown",
                facts=DurabilityFacts(
                    mutation_applied=True,
                    document=DurabilityState.DURABLE,
                    oplog=DurabilityState.UNKNOWN,
                ),
            )
            receipt = CommitReceipt.from_failure(
                commit_id=intent.commit_id,
                document_id=intent.document_id,
                operation_ids=intent.operation_ids,
                trace_id=intent.trace_id,
                save_policy=intent.save_policy,
                failure=failure,
                document_version=intent.document.version,
            )
        self.receipts.append(receipt)
        return receipt


class RecordingPublisher:
    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.payloads: list[dict[str, Any]] = []

    def publish(self, payload):
        copied = dict(payload)
        self.payloads.append(copied)
        self.log.append(f"publish:{copied['command_id']}")
        return EventStatus.PUBLISHED


class RecordingStatusBroker:
    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.payloads: list[dict[str, Any]] = []

    def publish_change(self, payload):
        copied = dict(payload)
        self.payloads.append(copied)
        self.log.append(f"status:{copied.get('type')}")
        return len(self.payloads)


class RecordingRuns(dict):
    def __init__(self, log: list[str]) -> None:
        super().__init__()
        self.log = log

    def __setitem__(self, key, value):
        self.log.append("run")
        super().__setitem__(key, value)


def _production_supervisor(tmp_path: Path, *, outcome: str = "committed"):
    log: list[str] = []
    document = Document(
        schema_version="1.0",
        collaboration_mode="with_save",
        save_policy="auto_commit",
        name="pipeline-boundary",
        schema={"item": ITEM_SCHEMA},
    )
    registry = CommandRegistry()
    register_builtin_commands(registry)
    status_broker = RecordingStatusBroker(log)
    store = RecordingStore(log, outcome=outcome)
    publisher = RecordingPublisher(log)
    lock = threading.RLock()
    coordinator = CommitCoordinator(
        document=document,
        store=store,
        publisher=publisher,
        lock=lock,
    )
    supervisor = PipelineSupervisor(
        doc=document,
        cmd_reg=registry,
        op_log=OpLog(),
        broker=status_broker,
        data_dir=tmp_path,
        actor=Actor(user_id="__system__", source="system"),
        lock=lock,
        commit_coordinator=coordinator,
    )
    supervisor._runs = RecordingRuns(log)
    real_write_manifest = supervisor._write_manifest

    def _record_manifest(state):
        log.append("manifest")
        real_write_manifest(state)

    supervisor._write_manifest = _record_manifest
    supervisor._spawn = lambda state, items: log.append("spawn")
    return document, registry, supervisor, coordinator, store, publisher, status_broker, log


def _start_work_list(run_id: str = "run-start", count: int = 2) -> WorkList:
    return WorkList(
        run_id=run_id,
        name="start boundary",
        items=[
            WorkItem(
                item_id=f"item-{index}",
                entity_type="item",
                entity_id=f"item-{index}",
                artifacts=(f"art/{run_id}/{index}.txt",),
                payload={
                    "name": "生成中…",
                    "seed_data": {"title": "生成中…", "done": False},
                },
            )
            for index in range(count)
        ],
    )


def _protected_pipeline_document_and_registry():
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
            },
        },
    )
    declarations.set_collaboration_mode("with_save")
    declarations.set_save_policy("auto_commit")
    document = declarations.build_document(
        schema_version="1.0",
        id="doc-protected-pipeline",
    )
    registry = CommandRegistry()
    register_builtin_commands(registry)
    return document, registry


def _prepared_start(supervisor: PipelineSupervisor, run_id: str = "run-start"):
    work_list = _start_work_list(run_id)
    start_operation = new_operation(
        command_id="item.batch_generate",
        source="user",
        actor={"user_id": "u1"},
        patches=[],
        reverse_patches=[],
        trace_id=run_id,
        kind="stage",
    )
    return supervisor.prepare_start(
        work_list=work_list,
        item_stage=StageSpec(id="generate", label="生成", run=lambda ctx, args: {}),
        trace_id=run_id,
        start_command_id="item.batch_generate",
        start_operation=start_operation,
    )


def test_production_start_is_one_commit_and_activates_only_after_all_publishes(tmp_path):
    (
        _document,
        _registry,
        supervisor,
        coordinator,
        store,
        publisher,
        status_broker,
        log,
    ) = _production_supervisor(tmp_path)
    mutation, state = _prepared_start(supervisor)

    # prepare 只 apply Document mutation；运行态、文件、广播和线程都必须仍为空。
    assert list(supervisor._runs) == []
    assert not (tmp_path / ".pipelines/run-start/run.json").exists()
    assert status_broker.payloads == []
    assert "spawn" not in log

    execution = coordinator.execute(lambda: mutation, trace_id=state.trace_id)

    assert len(store.intents) == 1
    intent = store.intents[0]
    assert [operation.command_id for operation in intent.operations] == [
        "item.batch_generate",
        "entity.add",
        "entity.add",
    ]
    assert execution.receipt is not None
    assert execution.receipt.status is CommitStatus.COMMITTED
    assert execution.receipt.operation_ids == intent.operation_ids
    assert execution.receipt.trace_id == "run-start"
    assert len(execution.actions) == 1
    assert execution.actions[0].status is ActionStatus.SUCCEEDED
    assert [payload["command_id"] for payload in publisher.payloads] == [
        "item.batch_generate",
        "entity.add",
        "entity.add",
    ]

    # 所有 canonical event 都接受后，才允许 manifest/run/status/spawn 出现。
    last_publish = max(index for index, entry in enumerate(log) if entry.startswith("publish:"))
    for entry in ("manifest", "run", "status:entity_status", "status:pipeline_status", "spawn"):
        assert log.index(entry) > last_publish, log
    assert list(supervisor._runs) == ["run-start"]
    assert (tmp_path / ".pipelines/run-start/run.json").exists()


def test_prepare_start_marks_only_real_new_entities_and_transports_placeholder_identity(tmp_path):
    (
        document,
        registry,
        supervisor,
        coordinator,
        _store,
        _publisher,
        status_broker,
        _log,
    ) = _production_supervisor(tmp_path)

    # 真实 CommandRegistry dispatch 预置一个「看起来像空占位」的实体。
    # 身份只看本次 prepare 是否新建，不看 name/data 内容。
    registry.dispatch(
        "entity.add",
        {"type": "item", "id": "item-0", "name": "", "data": {}},
        actor=Actor(user_id="seed"), source="system", doc=document, op_log=None,
    )
    assert document.find("item", "item-0") is not None

    mutation, state = _prepared_start(supervisor)

    assert [operation.command_id for operation in mutation.operations] == [
        "item.batch_generate",
        "entity.add",
    ]
    assert state.placeholder_item_ids == {"item-1"}
    assert state.item_status["item-0"].placeholder is False
    assert "placeholder" not in state.item_status["item-0"].to_dict()
    assert state.item_status["item-1"].placeholder is True

    execution = coordinator.execute(lambda: mutation, trace_id=state.trace_id)
    assert execution.is_committed is True
    manifest = json.loads((tmp_path / ".pipelines/run-start/run.json").read_text())
    assert manifest["placeholder_item_ids"] == ["item-1"]

    status_payload = next(
        payload for payload in status_broker.payloads if payload["type"] == "entity_status"
    )
    by_id = {status["entity_id"]: status for status in status_payload["statuses"]}
    assert "placeholder" not in by_id["item-0"]
    assert by_id["item-1"]["placeholder"] is True


def test_protected_prepare_start_inherits_exact_root_identity_and_seals_full_prefix(tmp_path):
    document, registry = _protected_pipeline_document_and_registry()
    log: list[str] = []
    lock = threading.RLock()
    op_log = OpLog(tmp_path / "operations.ndjson")
    coordinator = CommitCoordinator(
        document=document,
        store=FileCommitStore(tmp_path / "document.json", op_log),
        publisher=RecordingPublisher(log),
        lock=lock,
    )
    supervisor = PipelineSupervisor(
        doc=document,
        cmd_reg=registry,
        op_log=op_log,
        broker=RecordingStatusBroker(log),
        data_dir=tmp_path,
        actor=Actor(user_id="__system__", source="system"),
        lock=lock,
        commit_coordinator=coordinator,
    )
    def _forbidden_spawn(*_args, **_kwargs):
        pytest.fail("protected v2 Start cannot spawn before the grant lifecycle exists")

    supervisor._spawn = _forbidden_spawn

    def _start(*, args, actor, source, doc):
        mutation, _state = supervisor.prepare_start(
            work_list=_start_work_list("protected-start"),
            item_stage=StageSpec(
                id="generate",
                label="Generate",
                run=lambda ctx, values: {},
            ),
            trace_id="protected-start",
            pipeline_id="item.batch_generate",
            start_command_id="item.batch_generate",
            concurrency=2,
            max_retries=3,
            parameters={"quality": "high"},
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
    actor = Actor(user_id="u-business", device_id="web-1", source="user")

    execution = coordinator.execute(
        lambda: registry.dispatch(
            "item.batch_generate",
            {},
            actor=actor,
            source="user",
            doc=document,
        ),
        trace_id="protected-start",
    )

    assert execution.is_committed is True
    assert execution.actions == ()
    assert list(supervisor._runs) == []
    assert not (tmp_path / ".pipelines/protected-start/run.json").exists()
    operations = execution.operations
    assert [operation.command_id for operation in operations] == [
        "item.batch_generate",
        "entity.add",
        "entity.add",
    ]
    assert all(operation.document_id == document.id for operation in operations)
    assert all(operation.actor == actor.to_dict() for operation in operations)
    assert all(operation.source == "user" for operation in operations)
    start = operations[0]
    descriptor = start.pipeline
    assert descriptor["schema"] == "ze.pipeline-start.v2"
    assert descriptor["document_id"] == document.id
    assert descriptor["run_id"] == "protected-start"
    assert descriptor["pipeline_id"] == "item.batch_generate"
    assert descriptor["start_command_id"] == "item.batch_generate"
    assert descriptor["start_operation_id"] == start.id
    assert descriptor["initial_operation_ids"] == [
        operation.id for operation in operations
    ]
    assert descriptor["execution"] == {
        "concurrency": 2,
        "max_retries": 3,
        "parameters": {"quality": "high"},
    }
    assert descriptor["placeholder_bindings"] == [
        {
            "item_id": f"item-{index}",
            "entity_type": "item",
            "entity_id": f"item-{index}",
            "operation_id": operations[index + 1].id,
        }
        for index in range(2)
    ]

    # Re-open the durable history through its fresh snapshot protocol.  The
    # reducer must reproduce the exact business root and committed closure;
    # neither the handler return nor Supervisor memory is used as authority.
    rows, head_count, head_hash = OpLog(
        tmp_path / "operations.ndjson",
    )._canonical_history_snapshot()
    reduced = reduce_fresh_pipeline_history(
        rows,
        head_count=head_count,
        head_hash=head_hash,
        document_id=document.id,
        run_id="protected-start",
        pipeline_id="item.batch_generate",
    )
    assert reduced.start_operation_id == start.id
    assert reduced.initial_operation_ids == tuple(operation.id for operation in operations)
    assert reduced.root_actor == ("u-business", "web-1", "user")
    assert reduced.root_source == "user"
    assert reduced.worker_actor == ("__system__", "default", "system")
    assert reduced.worker_source == "ai"
    assert reduced.work_item_ids == ("item-0", "item-1")
    assert reduced.allowed_stage_ids == ("generate",)
    assert tuple(
        (target.entity_type, target.entity_id, target.fields, target.lifecycle)
        for target in reduced.delegated_targets
    ) == (
        ("item", "item-0", ("done", "title"), ("create", "rename")),
        ("item", "item-1", ("done", "title"), ("create", "rename")),
    )
    assert tuple(
        (binding.item_id, binding.entity_type, binding.entity_id, binding.operation_id)
        for binding in reduced.placeholder_bindings
    ) == tuple(
        (
            f"item-{index}",
            "item",
            f"item-{index}",
            operations[index + 1].id,
        )
        for index in range(2)
    )


@pytest.mark.parametrize(
    "overrides,expected_code",
    (
        ({"pipeline_id": ""}, "pipeline_start_identity_mismatch"),
        ({"pipeline_id": "item.batch_generate", "concurrency": True},
         "pipeline_start_identity_mismatch"),
        ({"pipeline_id": "item.batch_generate", "max_retries": 1.5},
         "pipeline_start_identity_mismatch"),
    ),
)
def test_protected_prepare_start_rejects_lossy_or_missing_identity_before_placeholder_mutation(
    tmp_path,
    overrides,
    expected_code,
):
    document, registry = _protected_pipeline_document_and_registry()
    lock = threading.RLock()
    supervisor = PipelineSupervisor(
        doc=document,
        cmd_reg=registry,
        op_log=OpLog(),
        broker=RecordingStatusBroker([]),
        data_dir=tmp_path,
        actor=Actor(user_id="__system__", source="system"),
        lock=lock,
        commit_coordinator=CommitCoordinator(
            document=document,
            store=RecordingStore([]),
            publisher=RecordingPublisher([]),
            lock=lock,
        ),
    )

    def _start(*, actor, source, doc, **_kwargs):
        kwargs = {
            "pipeline_id": "item.batch_generate",
            "concurrency": 1,
            "max_retries": 0,
        }
        kwargs.update(overrides)
        mutation, _state = supervisor.prepare_start(
            work_list=_start_work_list("protected-invalid"),
            item_stage=StageSpec(
                id="generate",
                label="Generate",
                run=lambda ctx, values: {},
            ),
            start_command_id="item.batch_generate",
            **kwargs,
        )
        return mutation

    registry.register(CommandSpec(
        id="item.batch_generate",
        label="Generate items",
        mutation_authority=True,
        execute=_start,
    ))
    document._validate_mutation_policy_commands(registry)

    with pytest.raises(ZeError) as exc_info:
        registry.dispatch(
            "item.batch_generate",
            {},
            actor=Actor(user_id="u-business"),
            source="user",
            doc=document,
        )
    assert exc_info.value.code == expected_code
    assert document.list("item") == []


def test_protected_prepare_start_rejects_caller_supplied_start_operation(tmp_path):
    document, registry = _protected_pipeline_document_and_registry()
    lock = threading.RLock()
    supervisor = PipelineSupervisor(
        doc=document,
        cmd_reg=registry,
        op_log=OpLog(),
        broker=RecordingStatusBroker([]),
        data_dir=tmp_path,
        actor=Actor(user_id="__system__", source="system"),
        lock=lock,
        commit_coordinator=CommitCoordinator(
            document=document,
            store=RecordingStore([]),
            publisher=RecordingPublisher([]),
            lock=lock,
        ),
    )

    def _start(*, actor, source, doc, **_kwargs):
        forged = new_operation(
            command_id="item.batch_generate",
            source=source,
            actor=actor.to_dict(),
            patches=(),
            reverse_patches=(),
            document_id=doc.id,
        )
        mutation, _state = supervisor.prepare_start(
            work_list=_start_work_list("protected-forged"),
            item_stage=StageSpec(
                id="generate",
                label="Generate",
                run=lambda ctx, values: {},
            ),
            pipeline_id="item.batch_generate",
            start_command_id="item.batch_generate",
            start_operation=forged,
        )
        return mutation

    registry.register(CommandSpec(
        id="item.batch_generate",
        label="Generate items",
        mutation_authority=True,
        execute=_start,
    ))
    document._validate_mutation_policy_commands(registry)

    with pytest.raises(ZeError) as exc_info:
        registry.dispatch(
            "item.batch_generate",
            {},
            actor=Actor(user_id="u-business"),
            source="user",
            doc=document,
        )
    assert exc_info.value.code == "pipeline_start_operation_forbidden"
    assert document.list("item") == []


def test_direct_supervisor_start_prepares_inside_one_commit_with_start_first(tmp_path):
    (
        _document,
        _registry,
        supervisor,
        coordinator,
        store,
        publisher,
        status_broker,
        log,
    ) = _production_supervisor(tmp_path)
    work_list = _start_work_list("direct-start")
    real_prepare_start = supervisor.prepare_start

    def _asserted_prepare_start(**kwargs):
        assert getattr(coordinator._local, "producer_active", False) is True
        return real_prepare_start(**kwargs)

    supervisor.prepare_start = _asserted_prepare_start

    state = supervisor.start(
        work_list=work_list,
        item_stage=StageSpec(id="generate", label="生成", run=lambda ctx, args: {}),
        trace_id="direct-start",
        start_command_id="pipeline.direct_start",
    )

    assert state.run_id == "direct-start"
    assert len(store.intents) == 1
    assert len(store.receipts) == 1
    intent = store.intents[0]
    receipt = store.receipts[0]
    assert [operation.command_id for operation in intent.operations] == [
        "pipeline.direct_start",
        "entity.add",
        "entity.add",
    ]
    assert receipt.status is CommitStatus.COMMITTED
    assert receipt.operation_ids == intent.operation_ids
    assert receipt.trace_id == "direct-start"
    assert [payload["command_id"] for payload in publisher.payloads] == [
        "pipeline.direct_start",
        "entity.add",
        "entity.add",
    ]
    last_publish = max(index for index, entry in enumerate(log) if entry.startswith("publish:"))
    assert log.index("manifest") > last_publish
    assert log.index("run") > last_publish
    assert log.index("status:entity_status") > last_publish
    assert log.index("status:pipeline_status") > last_publish
    assert log.index("spawn") > last_publish
    assert [payload["type"] for payload in status_broker.payloads] == [
        "entity_status",
        "pipeline_status",
    ]


def test_indeterminate_start_has_no_run_manifest_spawn_or_status(tmp_path):
    (
        _document,
        _registry,
        supervisor,
        coordinator,
        store,
        publisher,
        status_broker,
        log,
    ) = _production_supervisor(tmp_path, outcome="indeterminate")
    mutation, state = _prepared_start(supervisor)

    execution = coordinator.execute(lambda: mutation, trace_id=state.trace_id)

    assert len(store.intents) == 1
    assert execution.receipt is not None
    assert execution.receipt.status is CommitStatus.INDETERMINATE
    assert execution.events == ()
    assert len(execution.actions) == 1
    assert execution.actions[0].status is ActionStatus.SKIPPED
    assert publisher.payloads == []
    assert status_broker.payloads == []
    assert list(supervisor._runs) == []
    assert not (tmp_path / ".pipelines/run-start/run.json").exists()
    assert all(
        entry not in log
        for entry in ("manifest", "run", "status:entity_status", "status:pipeline_status", "spawn")
    )


LIFECYCLE_CASES = (
    ("pipeline.pause", {"pipeline_id": "run-1"}, "pause"),
    ("pipeline.interrupt", {"pipeline_id": "run-1"}, "interrupt"),
    ("pipeline.resume", {"pipeline_id": "run-1"}, "resume"),
    ("pipeline.cancel", {"pipeline_id": "run-1"}, "cancel"),
    (
        "pipeline.retry_failed",
        {"pipeline_id": "run-1", "item_ids": ["item-1"]},
        "retry_failed",
    ),
    (
        "pipeline.rerun_step",
        {"pipeline_id": "run-1", "stage_id": "generate", "item_id": "item-1"},
        "rerun_step",
    ),
    (
        "pipeline.run_from_step",
        {"pipeline_id": "run-1", "stage_id": "generate", "item_id": "item-1"},
        "run_from_step",
    ),
    (
        "pipeline.set_breakpoint",
        {"pipeline_id": "run-1", "stage_id": "generate", "halt": True},
        "set_breakpoint",
    ),
)


class RecordingLifecycleSupervisor:
    """只隔离验证 pipeline_commands 的 producer/deferred-action 时序。"""

    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.known_runs = {"run-1"}
        self.state_version = 0

    def validate_lifecycle(self, command_id, args):
        self.log.append(f"validate:{command_id}")
        if args.get("pipeline_id") not in self.known_runs:
            raise ZeError("pipeline_not_found", "pipeline run not found")

    def _apply(self, name: str) -> None:
        self.log.append(f"action:{name}")
        self.state_version += 1

    def pause(self, run_id):
        self._apply("pause")

    def interrupt(self, run_id):
        self._apply("interrupt")

    def resume(self, run_id):
        self._apply("resume")

    def cancel(self, run_id):
        self._apply("cancel")

    def retry_failed(self, run_id, item_ids=None):
        self._apply("retry_failed")

    def rerun_step(self, run_id, *, stage_id, item_id=None):
        self._apply("rerun_step")

    def run_from_step(self, run_id, *, stage_id, item_id=None):
        self._apply("run_from_step")

    def set_breakpoint(self, run_id, *, stage_id, halt):
        self._apply("set_breakpoint")


@pytest.mark.parametrize("command_id,args,action_name", LIFECYCLE_CASES)
def test_every_lifecycle_action_runs_only_after_successful_publish(
    command_id,
    args,
    action_name,
):
    log: list[str] = []
    document = Document(
        schema_version="1.0", collaboration_mode="with_save", save_policy="auto_commit",
    )
    store = RecordingStore(log)
    publisher = RecordingPublisher(log)
    coordinator = CommitCoordinator(document, store, publisher=publisher)
    supervisor = RecordingLifecycleSupervisor(log)
    registry = CommandRegistry()
    register_pipeline_commands(registry, supervisor)
    actor = Actor(user_id="u1")

    execution = coordinator.execute(lambda: registry.dispatch(
        command_id,
        dict(args),
        actor=actor,
        source="user",
        doc=document,
        op_log=None,
    ))

    assert log == [
        f"validate:{command_id}",
        "store",
        f"publish:{command_id}",
        f"action:{action_name}",
    ]
    assert supervisor.state_version == 1
    assert len(store.intents) == 1
    assert len(execution.operations) == 1
    assert execution.operations[0].command_id == command_id
    assert execution.operations[0].document_id == document.id
    assert execution.actions[0].status is ActionStatus.SUCCEEDED


@pytest.mark.parametrize("command_id,args,action_name", LIFECYCLE_CASES)
def test_every_lifecycle_action_is_skipped_when_store_is_indeterminate(
    command_id,
    args,
    action_name,
):
    log: list[str] = []
    document = Document(
        schema_version="1.0", collaboration_mode="with_save", save_policy="auto_commit",
    )
    store = RecordingStore(log, outcome="indeterminate")
    publisher = RecordingPublisher(log)
    coordinator = CommitCoordinator(document, store, publisher=publisher)
    supervisor = RecordingLifecycleSupervisor(log)
    registry = CommandRegistry()
    register_pipeline_commands(registry, supervisor)
    actor = Actor(user_id="u1")

    execution = coordinator.execute(lambda: registry.dispatch(
        command_id,
        dict(args),
        actor=actor,
        source="user",
        doc=document,
        op_log=None,
    ))

    assert log == [f"validate:{command_id}", "store"]
    assert f"action:{action_name}" not in log
    assert supervisor.state_version == 0
    assert publisher.payloads == []
    assert len(store.intents) == 1
    assert execution.receipt is not None
    assert execution.receipt.status is CommitStatus.INDETERMINATE
    assert execution.actions[0].status is ActionStatus.SKIPPED


def test_unknown_pipeline_run_fails_validation_without_store_or_publish(tmp_path):
    (
        document,
        registry,
        supervisor,
        coordinator,
        store,
        publisher,
        status_broker,
        log,
    ) = _production_supervisor(tmp_path)
    register_pipeline_commands(registry, supervisor)

    with pytest.raises(ZeError) as exc_info:
        coordinator.execute(lambda: registry.dispatch(
            "pipeline.pause",
            {"pipeline_id": "missing-run"},
            actor=Actor(user_id="u1"),
            source="user",
            doc=document,
            op_log=None,
        ))

    assert exc_info.value.code == "pipeline_not_found"
    assert store.intents == []
    assert publisher.payloads == []
    assert status_broker.payloads == []
    assert "store" not in log


def test_retry_none_freezes_sorted_failed_scope_and_empty_never_means_all(tmp_path):
    (
        document,
        registry,
        supervisor,
        _coordinator,
        _store,
        _publisher,
        _status_broker,
        _log,
    ) = _production_supervisor(tmp_path)
    register_pipeline_commands(registry, supervisor)
    _mutation, state = _prepared_start(supervisor, run_id="retry-scope")
    supervisor._runs[state.run_id] = state
    for item_id in ("item-1", "item-0"):
        supervisor._write_error_marker(
            state.run_id,
            item_id,
            {"code": "stage_failed", "message": "retry me"},
        )

    args = {"pipeline_id": state.run_id}
    prepared = registry.dispatch(
        "pipeline.retry_failed",
        args,
        actor=Actor(user_id="u1"),
        source="user",
        doc=document,
    )
    assert isinstance(prepared, CommandMutation)

    captured: list[tuple[str, list[str] | None]] = []
    supervisor.retry_failed = lambda run_id, item_ids=None: captured.append(
        (run_id, item_ids),
    )
    # Changing the caller's args after preparation cannot widen or erase the
    # deferred action's exact, lock-time expansion.
    args["pipeline_id"] = "redirected-run"
    args["item_ids"] = []
    prepared.after_commit[0].run("commit-1")
    assert captured == [(state.run_id, ["item-0", "item-1"])]

    with pytest.raises(ZeError) as explicit_empty:
        registry.dispatch(
            "pipeline.retry_failed",
            {"pipeline_id": state.run_id, "item_ids": []},
            actor=Actor(user_id="u1"),
            source="user",
            doc=document,
        )
    assert explicit_empty.value.code == "pipeline_retry_scope_empty"


def test_retry_rejects_no_failed_items_and_nonfailed_explicit_members(tmp_path):
    (
        document,
        registry,
        supervisor,
        _coordinator,
        _store,
        _publisher,
        _status_broker,
        _log,
    ) = _production_supervisor(tmp_path)
    register_pipeline_commands(registry, supervisor)
    _mutation, state = _prepared_start(supervisor, run_id="retry-empty")
    supervisor._runs[state.run_id] = state

    with pytest.raises(ZeError) as no_failed:
        registry.dispatch(
            "pipeline.retry_failed",
            {"pipeline_id": state.run_id},
            actor=Actor(user_id="u1"),
            source="user",
            doc=document,
        )
    assert no_failed.value.code == "pipeline_retry_scope_empty"

    supervisor._write_error_marker(
        state.run_id,
        "item-0",
        {"code": "stage_failed", "message": "retry me"},
    )
    with pytest.raises(ZeError) as nonfailed_member:
        registry.dispatch(
            "pipeline.retry_failed",
            {"pipeline_id": state.run_id, "item_ids": ["item-0", "item-1"]},
            actor=Actor(user_id="u1"),
            source="user",
            doc=document,
        )
    assert nonfailed_member.value.code == "pipeline_retry_scope_invalid"


@pytest.mark.parametrize("error_code", ("commit_dirty", "commit_indeterminate"))
@pytest.mark.parametrize("replay", ("retry_failed", "rerun_step", "run_from_step"))
def test_uncertain_commit_blocks_every_replay_without_clearing_marker_or_spawning(
    tmp_path,
    error_code,
    replay,
):
    (
        _document,
        _registry,
        supervisor,
        _coordinator,
        _store,
        _publisher,
        status_broker,
        _log,
    ) = _production_supervisor(tmp_path)
    run_id = f"unsafe-{error_code}-{replay}"
    mutation, state = supervisor.prepare_start(
        work_list=WorkList(
            run_id=run_id,
            name="unsafe replay",
            items=[WorkItem(
                item_id="item-1",
                entity_type="item",
                entity_id="item-1",
                artifacts=(f"art/{run_id}/item-1.txt",),
                payload={"seed_data": {"title": "seed", "done": False}},
            )],
        ),
        item_stage=StageSpec(id="generate", label="生成", run=lambda ctx, args: {}),
        trace_id=run_id,
        start_command_id="item.batch_generate",
    )
    assert mutation.operations
    error = {"code": error_code, "message": "repair first", "trace_id": run_id}
    current = state.item_status["item-1"]
    state.item_status["item-1"] = replace(current, phase="error", error=error)
    state.status = "error"
    supervisor._runs[run_id] = state
    supervisor._write_error_marker(run_id, "item-1", error)
    marker = tmp_path / ".pipelines" / run_id / "status" / "item-1.error.json"
    marker_before = marker.read_bytes()

    def _forbidden_spawn(*_args, **_kwargs):
        pytest.fail("unsafe replay must not spawn a worker")

    supervisor._spawn = _forbidden_spawn

    with pytest.raises(ZeError) as exc_info:
        if replay == "retry_failed":
            supervisor.retry_failed(run_id)
        elif replay == "rerun_step":
            supervisor.rerun_step(run_id, stage_id="generate", item_id="item-1")
        else:
            supervisor.run_from_step(run_id, stage_id="generate", item_id="item-1")

    assert exc_info.value.code == "commit_repair_required"
    assert marker.exists()
    assert marker.read_bytes() == marker_before
    assert state.threads == []
    assert state.item_status["item-1"].phase == "error"
    assert state.item_status["item-1"].error == error
    assert status_broker.payloads == []
