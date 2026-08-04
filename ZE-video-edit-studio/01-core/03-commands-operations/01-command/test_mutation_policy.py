"""Runtime mutation policy: declarations authorize the real Document delta."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

_HERE = Path(__file__).resolve().parent
_CORE = _HERE.parent.parent
for _path in (
    _HERE,
    _CORE / "02-data-flow" / "07-persistence",
    _CORE / "02-data-flow" / "04-history-rollback",
):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import command as command_module
import commit_coordinator as commit_coordinator_module
import entity as entity_module
import mutation_operation_audit as mutation_operation_audit_module
from builtin_commands import register_builtin_commands
from command import CommandMutation, CommandRegistry, CommandSpec
from commit_contracts import CommitReceipt
from commit_coordinator import CommitCoordinator
from document import (
    DocumentMutationSession,
    DocumentRegistry,
    _HISTORY_REPLAY_CAPABILITY,
    _history_operation_scope,
)
from entity import Actor, AiProvenance, Entity, ZeError
from operation import ENTITY_META_FIELD, OpLog, Patch, new_operation
from ports import MutationBatch
from history_authority import (
    HistoryAuthorityError,
    _HISTORY_RETIRE_CAPABILITY,
    _authority_record,
    _issue_history_bind_capability,
    bind_history_authority,
    retire_history_authority,
)
from undo import (
    UndoController,
    _verify_history_replay_derivation,
    rollback_operation_group,
)


ACTOR = Actor(user_id="u1")
_TEST_HISTORY_BOOTSTRAP_OWNER = object()
SCHEMA = {
    "type": "object",
    "properties": {
        "x": {"type": "integer"},
        "y": {"type": "integer"},
        "order": {"type": "integer"},
        "box": {
            "type": "object",
            "properties": {
                "a": {"type": "integer"},
                "b": {"type": "integer"},
            },
            "additionalProperties": False,
        },
        "maybe": {},
    },
    "additionalProperties": False,
}


def _policy(
    *,
    create=(),
    rename=(),
    soft_delete=(),
    restore=(),
    hard_remove=(),
    fields=None,
    max_total=None,
):
    value = {
        "schema": "ze.entity-mutation-policy.v1",
        "lifecycle": {
            "create": list(create),
            "rename": list(rename),
            "soft_delete": list(soft_delete),
            "restore": list(restore),
            "hard_remove": list(hard_remove),
        },
        "fields": {key: list(ids) for key, ids in (fields or {}).items()},
    }
    if max_total is not None:
        value["cardinality"] = {"max_total": max_total}
    return value


def _doc(policy):
    registry = DocumentRegistry()
    registry.declare_entity_type(
        name="t", schema=SCHEMA, collaboration_unit=True,
        mutation_policy=policy,
    )
    registry.set_collaboration_mode("with_save")
    return registry, registry.build_document(schema_version="1.0", id="doc-1")


def _unprotected_doc():
    registry = DocumentRegistry()
    registry.declare_entity_type(
        name="t", schema=SCHEMA, collaboration_unit=True,
    )
    registry.set_collaboration_mode("with_save")
    return registry, registry.build_document(schema_version="1.0", id="doc-1")


def _seal_commands(doc, commands):
    """Complete a test registry, then install the production-style seal."""
    referenced = set()
    for policy in doc._mutation_policies.values():
        for command_ids in policy["lifecycle"].values():
            referenced.update(command_ids)
        for command_ids in policy["fields"].values():
            referenced.update(command_ids)
    for command_id in sorted(referenced):
        if commands.has(command_id):
            continue
        commands.register(CommandSpec(
            id=command_id,
            label=command_id,
            mutation_authority=True,
            execute=lambda *, _command_id=command_id, **_kwargs: _op(_command_id),
        ))
    doc._validate_mutation_policy_commands(commands)


def _op(command_id, *, patches=(), reverse_patches=()):
    return new_operation(
        command_id=command_id,
        source="user",
        actor=ACTOR.to_dict(),
        patches=tuple(patches),
        reverse_patches=tuple(reverse_patches),
    )


def _field_op(command_id, before, after, *, field="x", entity_id="e1"):
    return _op(
        command_id,
        patches=(Patch("t", entity_id, field, "set", before, after),),
        reverse_patches=(Patch("t", entity_id, field, "set", after, before),),
    )


def _meta_op(command_id, action, *, before=None, after=None, entity_id="e1"):
    reverse = {
        "create": "delete",
        "delete": "create",
        "soft_delete": "restore",
        "restore": "soft_delete",
        "name": "name",
    }[action]
    return _op(
        command_id,
        patches=(Patch("t", entity_id, ENTITY_META_FIELD, action, before, after),),
        reverse_patches=(
            Patch("t", entity_id, ENTITY_META_FIELD, reverse, after, before),
        ),
    )


class _DurableStore:
    def __init__(self, document):
        self.document = document
        self.calls = 0

    def commit(self, intent):
        self.calls += 1
        self.document._v += 1
        return CommitReceipt.committed(
            commit_id=intent.commit_id,
            document_id=intent.document_id,
            operation_ids=intent.operation_ids,
            trace_id=intent.trace_id,
            save_policy=intent.save_policy,
            document_version=self.document.version,
        )


class _HistoryDurableStore(_DurableStore):
    """Test commit boundary that advances Document and its exact OpLog together."""

    def __init__(self, document, op_log):
        super().__init__(document)
        self.op_log = op_log

    def commit(self, intent):
        receipt = super().commit(intent)
        for operation in intent.operations:
            self.op_log.append(operation)
        return receipt


def _bind_history_runtime(doc, op_log, controller):
    store = _HistoryDurableStore(doc, op_log)
    coordinator = CommitCoordinator(doc, store)
    authority = bind_history_authority(
        _issue_history_bind_capability(
            bootstrap_owner=_TEST_HISTORY_BOOTSTRAP_OWNER,
        ),
        bootstrap_owner=_TEST_HISTORY_BOOTSTRAP_OWNER,
        document=doc,
        op_log=op_log,
        undo_controller=controller,
        coordinator=coordinator,
        store=store,
        project_id=doc.id,
        generation=1,
        runtime_revision="test-runtime",
        replay_verifier=_verify_history_replay_derivation,
    )
    return store, coordinator, authority


@pytest.fixture
def _history_runtime_factory():
    """Bind one production-shaped canonical runtime per test Document."""

    live = []

    def create(doc):
        assert all(runtime.doc is not doc for runtime in live)
        op_log = OpLog()
        controller = UndoController(op_log, doc)
        store, coordinator, authority = _bind_history_runtime(
            doc,
            op_log,
            controller,
        )
        runtime = SimpleNamespace(
            doc=doc,
            op_log=op_log,
            controller=controller,
            store=store,
            coordinator=coordinator,
            authority=authority,
        )
        live.append(runtime)
        return runtime

    yield create

    for runtime in reversed(live):
        retire_history_authority(
            runtime.authority,
            _capability=_HISTORY_RETIRE_CAPABILITY,
        )


def test_history_bind_installs_exact_document_controller_runtime_graph():
    _, doc = _doc(_policy(fields={"x": ["Business.Update"]}))
    _seal_commands(doc, CommandRegistry())
    op_log = OpLog()
    controller = UndoController(op_log, doc)
    store, coordinator, authority = _bind_history_runtime(
        doc,
        op_log,
        controller,
    )
    try:
        assert doc._history_op_log_authority is op_log
        assert doc._history_controller_authority is controller
        assert doc._history_authority_epoch is controller._history_authority_epoch
        assert doc._history_authority_pid == controller._history_authority_pid
        assert doc._history_authority_pid == os.getpid()
        assert coordinator.document is doc
        assert coordinator.store is store
        assert store.op_log is op_log
    finally:
        retire_history_authority(
            authority,
            _capability=_HISTORY_RETIRE_CAPABILITY,
        )


def test_retired_document_generation_rejects_second_history_runtime():
    _, doc = _doc(_policy(fields={"x": ["Business.Update"]}))
    _seal_commands(doc, CommandRegistry())
    first_log = OpLog()
    first_controller = UndoController(first_log, doc)
    _store, _coordinator, first_authority = _bind_history_runtime(
        doc,
        first_log,
        first_controller,
    )
    retire_history_authority(
        first_authority,
        _capability=_HISTORY_RETIRE_CAPABILITY,
    )

    second_log = OpLog()
    second_controller = UndoController(second_log, doc)
    second_store = _HistoryDurableStore(doc, second_log)
    second_coordinator = CommitCoordinator(doc, second_store)
    with pytest.raises(HistoryAuthorityError) as rejected:
        bind_history_authority(
            _issue_history_bind_capability(
                bootstrap_owner=_TEST_HISTORY_BOOTSTRAP_OWNER,
            ),
            bootstrap_owner=_TEST_HISTORY_BOOTSTRAP_OWNER,
            document=doc,
            op_log=second_log,
            undo_controller=second_controller,
            coordinator=second_coordinator,
            store=second_store,
            project_id=doc.id,
            generation=1,
            runtime_revision="test-runtime-rebind",
            replay_verifier=_verify_history_replay_derivation,
        )
    assert rejected.value.reason == "history_binding_runtime_install_failed"
    assert doc._history_op_log_authority is first_log
    assert doc._history_controller_authority is first_controller
    assert second_controller._history_authority_epoch is None
    assert second_controller._history_authority_pid is None


def test_history_bind_partial_document_install_rolls_back_and_can_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, doc = _doc(_policy(fields={"x": ["Business.Update"]}))
    _seal_commands(doc, CommandRegistry())
    op_log = OpLog()
    controller = UndoController(op_log, doc)
    store = _HistoryDurableStore(doc, op_log)
    coordinator = CommitCoordinator(doc, store)
    capability = _issue_history_bind_capability(
        bootstrap_owner=_TEST_HISTORY_BOOTSTRAP_OWNER,
    )
    original = type(doc)._install_canonical_history_authority

    def partial_install(self, **kwargs):
        self._history_op_log_authority = kwargs["op_log"]
        raise KeyboardInterrupt("injected partial Document install")

    with monkeypatch.context() as scoped:
        scoped.setattr(
            type(doc),
            "_install_canonical_history_authority",
            partial_install,
        )
        with pytest.raises(HistoryAuthorityError) as failed:
            bind_history_authority(
                capability,
                bootstrap_owner=_TEST_HISTORY_BOOTSTRAP_OWNER,
                document=doc,
                op_log=op_log,
                undo_controller=controller,
                coordinator=coordinator,
                store=store,
                project_id=doc.id,
                generation=1,
                runtime_revision="test-runtime",
                replay_verifier=_verify_history_replay_derivation,
            )
        assert failed.value.reason == "history_binding_runtime_install_failed"

    assert type(doc)._install_canonical_history_authority is original
    assert doc._history_op_log_authority is None
    assert doc._history_controller_authority is None
    assert doc._history_authority_epoch is None
    assert doc._history_authority_pid is None
    assert controller._history_authority_epoch is None
    assert controller._history_authority_pid is None

    authority = bind_history_authority(
        capability,
        bootstrap_owner=_TEST_HISTORY_BOOTSTRAP_OWNER,
        document=doc,
        op_log=op_log,
        undo_controller=controller,
        coordinator=coordinator,
        store=store,
        project_id=doc.id,
        generation=1,
        runtime_revision="test-runtime",
        replay_verifier=_verify_history_replay_derivation,
    )
    retire_history_authority(
        authority,
        _capability=_HISTORY_RETIRE_CAPABILITY,
    )


def test_history_bind_partial_controller_install_rolls_back_and_can_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, doc = _doc(_policy(fields={"x": ["Business.Update"]}))
    _seal_commands(doc, CommandRegistry())
    op_log = OpLog()
    controller = UndoController(op_log, doc)
    store = _HistoryDurableStore(doc, op_log)
    coordinator = CommitCoordinator(doc, store)
    capability = _issue_history_bind_capability(
        bootstrap_owner=_TEST_HISTORY_BOOTSTRAP_OWNER,
    )
    original = type(controller)._install_canonical_history_authority

    def partial_install(self, **kwargs):
        self._history_authority_epoch = kwargs["authority_epoch"]
        raise KeyboardInterrupt("injected partial controller install")

    with monkeypatch.context() as scoped:
        scoped.setattr(
            type(controller),
            "_install_canonical_history_authority",
            partial_install,
        )
        with pytest.raises(HistoryAuthorityError) as failed:
            bind_history_authority(
                capability,
                bootstrap_owner=_TEST_HISTORY_BOOTSTRAP_OWNER,
                document=doc,
                op_log=op_log,
                undo_controller=controller,
                coordinator=coordinator,
                store=store,
                project_id=doc.id,
                generation=1,
                runtime_revision="test-runtime",
                replay_verifier=_verify_history_replay_derivation,
            )
        assert failed.value.reason == "history_binding_runtime_install_failed"

    assert type(controller)._install_canonical_history_authority is original
    assert controller._history_authority_epoch is None
    assert controller._history_authority_pid is None
    assert doc._history_op_log_authority is None
    assert doc._history_controller_authority is None
    authority = bind_history_authority(
        capability,
        bootstrap_owner=_TEST_HISTORY_BOOTSTRAP_OWNER,
        document=doc,
        op_log=op_log,
        undo_controller=controller,
        coordinator=coordinator,
        store=store,
        project_id=doc.id,
        generation=1,
        runtime_revision="test-runtime",
        replay_verifier=_verify_history_replay_derivation,
    )
    retire_history_authority(
        authority,
        _capability=_HISTORY_RETIRE_CAPABILITY,
    )


def _coordinated_dispatch(
    doc,
    commands,
    command_id,
    args=None,
    *,
    coordinator=None,
):
    boundary = coordinator
    if boundary is None:
        boundary = CommitCoordinator(doc, _DurableStore(doc))
    execution = boundary.execute(
        lambda: commands.dispatch(
            command_id,
            {} if args is None else args,
            actor=ACTOR,
            source="user",
            doc=doc,
        )
    )
    assert execution.is_committed
    return execution.result


def test_unprotected_snapshot_skips_policy_durable_serialization(monkeypatch):
    _, doc = _unprotected_doc()
    doc.add(Entity(type="t", id="e1", data={"x": 1}))

    def forbidden_to_dict(_entity):
        raise AssertionError("unprotected snapshot must not serialize Entity")

    monkeypatch.setattr(Entity, "to_dict", forbidden_to_dict)
    snapshot = doc.capture_mutation_snapshot()

    assert len(snapshot._entities) == 1
    assert snapshot._entities[0].durable_bytes == b""


def test_mutation_operation_audit_imports_from_its_leaf_alone(tmp_path):
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(_HERE)

    completed = subprocess.run(
        [sys.executable, "-c", "import mutation_operation_audit"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize(
    "field_name,forged_value",
    (
        ("id", "forged-operation-id"),
        ("command_id", "Forged.Command"),
        ("source", "sync"),
        ("actor", {"user_id": "forged", "source": "sync"}),
        ("ts", 0),
        ("patches", ()),
        ("reverse_patches", ()),
        ("group_id", "forged-group"),
        ("trace_id", "forged-trace"),
        ("kind", "stage"),
        ("error", {}),
        # These two values deliberately collide with the legacy empty-string
        # omission when a serializer tests truthiness instead of the exact
        # compatibility sentinel.
        ("document_id", None),
        ("session_id", {}),
        ("external", {}),
    ),
)
def test_mutation_operation_audit_seals_every_operation_field_during_replay(
    monkeypatch, field_name, forged_value,
):
    _, doc = _doc(_policy(fields={"x": ["Business.Update"]}))
    entity = doc.add(Entity(type="t", id="e1", data={"x": 1}))
    snapshot = doc.capture_mutation_snapshot()
    entity.update({"x": 2}, expected_version=1, actor=ACTOR)
    operation = _field_op("Business.Update", 1, 2)
    original_apply = mutation_operation_audit_module._apply_patch_sequence
    apply_calls = 0

    def mutate_during_replay(*args, **kwargs):
        nonlocal apply_calls
        apply_calls += 1
        if apply_calls == 1:
            object.__setattr__(operation, field_name, forged_value)
        return original_apply(*args, **kwargs)

    monkeypatch.setattr(
        mutation_operation_audit_module,
        "_apply_patch_sequence",
        mutate_during_replay,
    )

    with pytest.raises(ZeError) as captured:
        mutation_operation_audit_module.validate_mutation_operation_coverage(
            doc,
            snapshot,
            frozenset({("t", "e1", "field", "x")}),
            (operation,),
        )

    assert captured.value.code == "mutation_operation_patch_mismatch"
    assert captured.value.context["reason"] == "mutation_operation_body_changed"


def test_snapshot_rejects_non_string_entity_state_key_without_hashing_it():
    _, doc = _unprotected_doc()
    entity = doc.add(Entity(type="t", id="e1", data={"x": 1}))
    calls = []

    class HostileKey:
        armed = False

        def __hash__(self):
            if self.armed:
                calls.append("hash")
                raise AssertionError("untrusted key must be type-checked first")
            return 1

    key = HostileKey()
    entity.__dict__[key] = "forged"
    key.armed = True

    with pytest.raises(ZeError) as captured:
        doc.capture_mutation_snapshot()

    assert captured.value.code == "mutation_snapshot_failed"
    assert calls == []


def test_plain_unprotected_command_skips_preflight_scan_but_poll_keeps_it(monkeypatch):
    _, doc = _unprotected_doc()
    entity = doc.add(Entity(type="t", id="e1", data={"x": 1}))
    calls = []
    original = DocumentMutationSession.assert_unchanged

    def observed(self, *, stage):
        calls.append(stage)
        return original(self, stage=stage)

    monkeypatch.setattr(DocumentMutationSession, "assert_unchanged", observed)

    def execute(*, args, actor, source, doc):
        before = entity.data["x"]
        entity.update(
            {"x": before + 1}, expected_version=entity.version, actor=actor,
        )
        return _field_op("Plain.Update", before, before + 1)

    commands = CommandRegistry()
    commands.register(CommandSpec(
        id="Plain.Update", label="plain", execute=execute,
    ))
    commands.dispatch(
        "Plain.Update", {}, actor=ACTOR, source="user", doc=doc,
    )
    assert calls == []

    commands.register(CommandSpec(
        id="Polled.Update", label="polled", poll=lambda _args: True,
        execute=lambda **kwargs: _op("Polled.Update"),
    ))
    commands.dispatch(
        "Polled.Update", {}, actor=ACTOR, source="user", doc=doc,
    )
    assert calls == ["poll_governor"]


def _replay_via_coordinator(runtime, source_operation, *, redo=False):
    """Replay one real durable source through its one canonical runtime."""

    prepared = {}
    before_calls = runtime.store.calls
    if not redo:
        assert runtime.op_log.entries()[-1]["op"]["id"] == source_operation.id

    def producer():
        prepare = (
            runtime.controller.prepare_redo
            if redo
            else runtime.controller.prepare_undo
        )
        record = prepare(actor=ACTOR)
        assert record is not None
        prepared["record"] = record
        operation = record.operations[0]
        expected_patches = (
            source_operation.patches
            if redo
            else source_operation.reverse_patches
        )
        expected_reverse = (
            source_operation.reverse_patches
            if redo
            else source_operation.patches
        )
        assert operation.patches == expected_patches
        assert operation.reverse_patches == expected_reverse
        return operation

    execution = runtime.coordinator.execute(producer)
    assert execution.receipt is not None
    assert execution.receipt.status.value == "committed"
    runtime.controller.confirm_deferred(prepared["record"])
    assert runtime.store.calls == before_calls + 1
    assert _authority_record(runtime.authority).trusted_head.count == len(
        runtime.op_log.entries()
    )
    return prepared["record"].operations[0]


def _protected_history_fixture():
    _, doc = _doc(_policy(fields={"x": ["Business.Update"]}))
    entity = doc.add(Entity(type="t", id="e1", data={"x": 2}))
    operation = new_operation(
        command_id="Business.Update",
        source="user",
        actor=ACTOR.to_dict(),
        patches=(Patch("t", "e1", "x", "set", 1, 2),),
        reverse_patches=(Patch("t", "e1", "x", "set", 2, 1),),
        group_id="group-1",
    )
    op_log = OpLog()
    op_log.append(operation)
    _seal_commands(doc, CommandRegistry())
    controller = UndoController(op_log, doc)
    controller._test_history_runtime = _bind_history_runtime(
        doc, op_log, controller,
    )
    return doc, entity, op_log, controller


def test_protected_legacy_undo_requires_coordinator_before_any_mutation_or_append():
    doc, entity, op_log, controller = _protected_history_fixture()
    before_entries = op_log.entries()

    with pytest.raises(ZeError) as captured:
        controller.undo(actor=ACTOR)

    assert captured.value.code == "history_replay_coordinator_required"
    assert entity.data["x"] == 2
    assert op_log.entries() == before_entries


def test_protected_direct_group_rollback_requires_coordinator_before_apply_or_append():
    doc, entity, op_log, _controller = _protected_history_fixture()
    before_entries = op_log.entries()

    with pytest.raises(ZeError) as captured:
        rollback_operation_group(
            op_log,
            doc,
            actor=ACTOR,
            group_id="group-1",
            conflict_policy="force",
        )

    assert captured.value.code == "history_replay_coordinator_required"
    assert entity.data["x"] == 2
    assert op_log.entries() == before_entries


def _protected_history_phase(phase):
    doc, entity, op_log, controller = _protected_history_fixture()
    if phase == "undo":
        return doc, entity, op_log, controller

    prepared = {}

    def producer():
        record = controller.prepare_undo(actor=ACTOR)
        assert record is not None
        prepared["record"] = record
        return record.operations[0]

    _store, coordinator, _authority = controller._test_history_runtime
    execution = coordinator.execute(producer)
    assert execution.is_committed
    controller.confirm_deferred(prepared["record"])
    assert entity.data["x"] == 1
    assert controller.can_redo() is True
    return doc, entity, op_log, controller


def _replace_history_operation(operation, field):
    changes = {
        "reverse_patches": {"reverse_patches": ()},
        "actor": {
            "actor": {
                "user_id": "forged",
                "device_id": "default",
                "source": "user",
            },
        },
        "source": {"source": "ai"},
        "kind": {"kind": "native"},
        "command_id": {"command_id": "Forged.History"},
        "group_id": {"group_id": "forged-group"},
        "trace_id": {"trace_id": "forged-trace"},
    }[field]
    return replace(operation, **changes)


@pytest.mark.parametrize("phase", ("undo", "redo"))
@pytest.mark.parametrize(
    "field",
    (
        "reverse_patches",
        "actor",
        "source",
        "kind",
        "command_id",
        "group_id",
        "trace_id",
    ),
)
def test_protected_history_rejects_replaced_issued_operation_before_store(
    phase, field,
):
    doc, entity, op_log, controller = _protected_history_phase(phase)
    before_entity = entity.to_dict()
    before_entries = op_log.entries()
    before_redo = tuple(controller._redo_stack)
    store, coordinator, _authority = controller._test_history_runtime
    before_calls = store.calls
    prepared = {}

    def producer():
        prepare = (
            controller.prepare_undo
            if phase == "undo"
            else controller.prepare_redo
        )
        record = prepare(actor=ACTOR)
        assert record is not None
        prepared["record"] = record
        return _replace_history_operation(record.operations[0], field)

    with pytest.raises(ZeError) as captured:
        coordinator.execute(producer)

    assert captured.value.code == "mutation_operation_ledger_mismatch"
    assert store.calls == before_calls
    assert entity.to_dict() == before_entity
    assert op_log.entries() == before_entries
    assert tuple(controller._redo_stack) == before_redo
    controller.discard_deferred(prepared["record"])
    assert controller._pending_deferred == {}
    assert controller._pending_operation_bodies == {}


@pytest.mark.parametrize("phase", ("undo", "redo"))
def test_protected_history_rejects_in_place_issued_body_change_before_store(phase):
    doc, entity, op_log, controller = _protected_history_phase(phase)
    before_entity = entity.to_dict()
    before_entries = op_log.entries()
    before_redo = tuple(controller._redo_stack)
    store, coordinator, _authority = controller._test_history_runtime
    before_calls = store.calls
    prepared = {}

    def producer():
        prepare = (
            controller.prepare_undo
            if phase == "undo"
            else controller.prepare_redo
        )
        record = prepare(actor=ACTOR)
        assert record is not None
        prepared["record"] = record
        issued = record.operations[0]
        object.__setattr__(issued, "command_id", "Forged.InPlace")
        return issued

    with pytest.raises(ZeError) as captured:
        coordinator.execute(producer)

    assert captured.value.code == "mutation_operation_ledger_mismatch"
    assert captured.value.context["reason"] == "history_operation_body_mismatch"
    assert store.calls == before_calls
    assert entity.to_dict() == before_entity
    assert op_log.entries() == before_entries
    assert tuple(controller._redo_stack) == before_redo
    controller.discard_deferred(prepared["record"])
    assert controller._pending_deferred == {}
    assert controller._pending_operation_bodies == {}


def test_declaration_is_versioned_owned_and_detached():
    policy = _policy(fields={"x": ["Allowed"]}, max_total=1)
    registry, doc = _doc(policy)

    policy["fields"]["x"].append("Forged")
    registry.mutation_policies["t"]["fields"]["x"].append("GetterForged")
    registry.entity_types["t"]["schema"]["properties"]["x"]["type"] = "string"

    assert doc.mutation_policies["t"]["fields"]["x"] == ["Allowed"]
    assert registry.entity_types["t"]["schema"]["properties"]["x"]["type"] == "integer"


def test_policy_authority_primitives_are_not_public_document_methods():
    _, doc = _doc(_policy(fields={"x": ["Business.Update"]}))

    for public_name in (
        "validate_mutation_policy_commands",
        "validate_mutation_policy_cardinality",
        "authorize_command_mutation",
    ):
        assert not hasattr(doc, public_name)
    assert callable(doc._validate_mutation_policy_commands)
    assert callable(doc._validate_mutation_policy_cardinality)
    assert callable(doc._authorize_command_mutation)


@pytest.mark.parametrize(
    "mutator,reason",
    [
        (lambda p: p.update(schema="unsupported"), "mutation_policy_schema_unsupported"),
        (lambda p: p["fields"].update(z=["Allowed"]), "mutation_policy_field_undeclared"),
        (lambda p: p.update(cardinality={"max_total": True}), "mutation_policy_max_total_invalid"),
        (lambda p: p["fields"].update(x=["Allowed", "Allowed"]), "mutation_policy_command_id_duplicate"),
    ],
)
def test_invalid_declarations_fail_closed(mutator, reason):
    policy = _policy(fields={"x": ["Allowed"]})
    mutator(policy)
    registry = DocumentRegistry()
    with pytest.raises(ZeError) as captured:
        registry.declare_entity_type(name="t", schema=SCHEMA, mutation_policy=policy)
    assert captured.value.code == "mutation_policy_invalid"
    assert captured.value.context["reason"] == reason


def test_actual_unauthorized_field_rolls_back_even_when_operation_claims_allowed_id():
    _, doc = _doc(_policy(fields={"x": ["Allowed"]}))
    entity = doc.add(Entity(type="t", id="e1", data={"x": 1, "y": 2}))
    before = entity.to_dict()
    commands = CommandRegistry()

    def execute(*, args, actor, source, doc):
        entity.update({"y": 3}, expected_version=entity.version, actor=actor)
        return _op("Allowed")

    commands.register(CommandSpec(id="Denied", label="Denied", execute=execute))
    _seal_commands(doc, commands)
    with pytest.raises(ZeError) as captured:
        _coordinated_dispatch(doc, commands, "Denied")
    assert captured.value.code == "mutation_policy_denied"
    assert entity.to_dict() == before


def test_hidden_second_entity_delta_is_rejected_before_store_and_rolled_back():
    _, doc = _doc(_policy(fields={"x": ["Business.Update"]}))
    first = doc.add(Entity(type="t", id="e1", data={"x": 1}))
    second = doc.add(Entity(type="t", id="e2", data={"x": 10}))
    commands = CommandRegistry()

    def execute(*, args, actor, source, doc):
        del args, doc
        first.update({"x": 2}, expected_version=1, actor=actor)
        second.update({"x": 11}, expected_version=1, actor=actor)
        return _field_op("Business.Update", 1, 2, entity_id="e1")

    commands.register(CommandSpec(
        id="Business.Update",
        label="update",
        mutation_authority=True,
        execute=execute,
    ))
    doc._validate_mutation_policy_commands(commands)
    store = _DurableStore(doc)

    with pytest.raises(ZeError) as captured:
        CommitCoordinator(doc, store).execute(
            lambda: commands.dispatch(
                "Business.Update", {}, actor=ACTOR, source="user", doc=doc,
            )
        )

    assert captured.value.code == "mutation_operation_patch_mismatch"
    assert first.data["x"] == 1
    assert second.data["x"] == 10
    assert store.calls == 0


def test_returned_operation_command_id_cannot_grant_permission():
    _, doc = _doc(_policy(fields={"x": ["Allowed"]}))
    entity = doc.add(Entity(type="t", id="e1", data={"x": 1}))
    commands = CommandRegistry()

    def execute(*, args, actor, source, doc):
        entity.update({"x": 2}, expected_version=entity.version, actor=actor)
        return _op("Allowed")

    commands.register(CommandSpec(id="Denied", label="Denied", execute=execute))
    _seal_commands(doc, commands)
    with pytest.raises(ZeError) as captured:
        _coordinated_dispatch(doc, commands, "Denied")
    assert captured.value.context["command_id"] == "Denied"
    assert entity.data["x"] == 1


def test_nested_generic_helper_inherits_outer_business_command():
    _, doc = _doc(_policy(fields={"x": ["Business.Update"]}))
    entity = doc.add(Entity(type="t", id="e1", data={"x": 1}))
    commands = CommandRegistry()
    register_builtin_commands(commands)

    def execute(*, args, actor, source, doc):
        helper = commands.dispatch(
            "entity.update",
            {"type": "t", "id": "e1", "patches": {"x": 2}, "expected_version": 1},
            actor=actor,
            source=source,
            doc=doc,
        )
        # The root Operation carries business identity only; the exact child
        # Operation remains the unique durable audit of the nested delta.
        primary = _op("Business.Update")
        return CommandMutation((primary, helper))

    commands.register(CommandSpec(
        id="Business.Update", label="Business", mutation_authority=True,
        execute=execute,
    ))
    _seal_commands(doc, commands)
    _coordinated_dispatch(doc, commands, "Business.Update")
    assert entity.data["x"] == 2


def test_nested_helper_delta_cannot_be_dropped_from_durable_operations():
    _, doc = _doc(_policy(fields={"x": ["Business.Update"]}))
    entity = doc.add(Entity(type="t", id="e1", data={"x": 1}))
    commands = CommandRegistry()
    register_builtin_commands(commands)

    def execute(*, args, actor, source, doc):
        commands.dispatch(
            "entity.update",
            {"type": "t", "id": "e1", "patches": {"x": 2}, "expected_version": 1},
            actor=actor,
            source=source,
            doc=doc,
        )
        return _op("Business.Update")

    commands.register(CommandSpec(
        id="Business.Update", label="Business", mutation_authority=True,
        execute=execute,
    ))
    _seal_commands(doc, commands)
    with pytest.raises(ZeError) as captured:
        _coordinated_dispatch(doc, commands, "Business.Update")
    assert captured.value.code == "mutation_operation_ledger_mismatch"
    assert entity.data["x"] == 1


def test_caught_nested_failure_preserves_protected_authority_for_root_retry():
    _, doc = _doc(_policy(fields={"x": ["Business.Update"]}))
    entity = doc.add(Entity(type="t", id="e1", data={"x": 1}))
    commands = CommandRegistry()
    register_builtin_commands(commands)

    def failing(*, args, actor, source, doc):
        del args, source, doc
        entity.update({"x": 2}, expected_version=1, actor=actor)
        raise RuntimeError("nested failure")

    commands.register(CommandSpec(
        id="Failing.Helper", label="failing", execute=failing,
    ))

    def business(*, args, actor, source, doc):
        del args
        try:
            commands.dispatch(
                "Failing.Helper", {}, actor=actor, source=source, doc=doc,
            )
        except RuntimeError:
            pass
        helper = commands.dispatch(
            "entity.update",
            {
                "type": "t", "id": "e1", "patches": {"x": 3},
                "expected_version": 1,
            },
            actor=actor,
            source=source,
            doc=doc,
        )
        return CommandMutation((_op("Business.Update"), helper))

    commands.register(CommandSpec(
        id="Business.Update", label="business", mutation_authority=True,
        execute=business,
    ))
    _seal_commands(doc, commands)

    result = _coordinated_dispatch(doc, commands, "Business.Update")

    assert isinstance(result, MutationBatch)
    assert entity.data["x"] == 3


def test_three_level_nested_command_cannot_drop_deep_child_operation():
    _, doc = _doc(_policy(fields={"x": ["Business.Update"]}))
    entity = doc.add(Entity(type="t", id="e1", data={"x": 1}))
    commands = CommandRegistry()
    register_builtin_commands(commands)

    def middle(*, args, actor, source, doc):
        del args
        commands.dispatch(
            "entity.update",
            {
                "type": "t", "id": "e1", "patches": {"x": 2},
                "expected_version": 1,
            },
            actor=actor,
            source=source,
            doc=doc,
        )
        return _op("Middle")

    commands.register(CommandSpec(
        id="Middle", label="middle", execute=middle,
    ))
    commands.register(CommandSpec(
        id="Business.Update",
        label="business",
        mutation_authority=True,
        execute=lambda **kwargs: commands.dispatch(
            "Middle", {}, actor=kwargs["actor"], source=kwargs["source"],
            doc=kwargs["doc"],
        ),
    ))
    _seal_commands(doc, commands)

    with pytest.raises(ZeError) as captured:
        _coordinated_dispatch(doc, commands, "Business.Update")

    assert captured.value.code == "mutation_operation_ledger_mismatch"
    assert entity.data["x"] == 1


def test_nested_high_privilege_command_cannot_escalate_outer_identity():
    _, doc = _doc(_policy(fields={"x": ["Privileged"]}))
    entity = doc.add(Entity(type="t", id="e1", data={"x": 1}))
    commands = CommandRegistry()

    def privileged(*, args, actor, source, doc):
        entity.update({"x": 2}, expected_version=entity.version, actor=actor)
        return _op("Privileged")

    commands.register(CommandSpec(
        id="Privileged", label="P", mutation_authority=True,
        execute=privileged,
    ))
    commands.register(CommandSpec(
        id="Unprivileged",
        label="U",
        execute=lambda **kwargs: commands.dispatch(
            "Privileged", {}, actor=kwargs["actor"], source=kwargs["source"], doc=kwargs["doc"],
        ),
    ))
    _seal_commands(doc, commands)
    with pytest.raises(ZeError) as captured:
        _coordinated_dispatch(doc, commands, "Unprivileged")
    assert captured.value.context["command_id"] == "Unprivileged"
    assert entity.data["x"] == 1


def test_cross_document_nested_confirmation_is_rejected_before_pending_side_effect():
    _, doc = _doc(_policy())
    _, other = _doc(_policy())
    commands = CommandRegistry()
    commands.register(CommandSpec(
        id="Confirm.Other",
        label="confirm",
        requires_confirm=True,
        execute=lambda **kwargs: _op("Confirm.Other"),
    ))

    def outer(*, args, actor, source, doc):
        return commands.dispatch(
            "Confirm.Other", {}, actor=actor, source=source, doc=other,
        )

    commands.register(CommandSpec(id="Outer", label="outer", execute=outer))
    _seal_commands(doc, commands)
    other._validate_mutation_policy_commands(commands)
    with pytest.raises(ZeError) as captured:
        commands.dispatch("Outer", {}, actor=ACTOR, source="user", doc=doc)
    assert captured.value.code == "mutation_command_scope_violation"
    assert commands.pending_store._entries == {}


def test_dispatch_stack_reset_after_fork_discards_inherited_authority():
    stack = command_module._command_dispatch_stack()
    stack.append((object(), object(), "Parent"))
    command_module._reset_command_dispatch_state_after_fork()
    assert command_module._command_dispatch_stack() == []


def test_create_checks_lifecycle_name_and_every_initial_field():
    _, doc = _doc(_policy(create=["Create"], fields={"x": ["Create"]}))
    commands = CommandRegistry()

    def execute(*, args, actor, source, doc):
        doc.add(Entity(type="t", id="e1", name="named", data={"x": 1, "y": 2}))
        return _op("Create")

    commands.register(CommandSpec(
        id="Create", label="create", mutation_authority=True, execute=execute,
    ))
    _seal_commands(doc, commands)
    with pytest.raises(ZeError) as captured:
        _coordinated_dispatch(doc, commands, "Create")
    assert captured.value.code == "mutation_policy_denied"
    assert doc.find("t", "e1") is None


def test_max_total_counts_deleted_canonical_rows_but_not_read_only_overlay():
    _, doc = _doc(_policy(max_total=1))
    canonical = doc.add(Entity(type="t", id="canonical", data={"x": 1}))
    canonical.delete(expected_version=1, actor=ACTOR)
    doc.add(Entity(type="t", id="overlay", read_only=True, data={"x": 1}))
    doc._validate_mutation_policy_cardinality()
    doc.add(Entity(type="t", id="second", data={"x": 2}))
    with pytest.raises(ZeError) as captured:
        doc._validate_mutation_policy_cardinality()
    assert captured.value.code == "mutation_cardinality_violation"


def test_policy_command_references_are_checked_after_registration():
    _, doc = _doc(_policy(fields={"x": ["Missing"]}))
    with pytest.raises(ZeError) as captured:
        doc._validate_mutation_policy_commands(CommandRegistry())
    assert captured.value.context["reason"] == "mutation_policy_command_unregistered"


def _register_business_helper(commands, command_id, helper_id, helper_args):
    def execute(*, args, actor, source, doc):
        helper = commands.dispatch(
            helper_id,
            helper_args(doc),
            actor=actor,
            source=source,
            doc=doc,
        )
        return CommandMutation((_op(command_id), helper))

    commands.register(CommandSpec(
        id=command_id,
        label=command_id,
        mutation_authority=True,
        execute=execute,
    ))


def _dispatch_business_helper(
    doc,
    commands,
    command_id,
    *,
    coordinator=None,
):
    boundary = coordinator
    if boundary is None:
        boundary = CommitCoordinator(doc, _DurableStore(doc))
    execution = boundary.execute(
        lambda: commands.dispatch(
            command_id, {}, actor=ACTOR, source="user", doc=doc,
        )
    )
    assert execution.is_committed
    assert isinstance(execution.result, MutationBatch)
    assert len(execution.result.operations) == 2
    return execution.result.operations[1]


def test_protected_builtin_add_and_batch_add_succeed_and_reverse_exactly(
    _history_runtime_factory,
):
    _, doc = _doc(_policy(
        create=["Business.Add", "Business.BatchAdd"],
        fields={"x": ["Business.Add", "Business.BatchAdd"]},
    ))
    commands = CommandRegistry()
    register_builtin_commands(commands)
    _register_business_helper(
        commands,
        "Business.Add",
        "entity.add",
        lambda _current: {
            "type": "t", "id": "single", "data": {"x": 1},
        },
    )
    _register_business_helper(
        commands,
        "Business.BatchAdd",
        "entity.batch_add",
        lambda _current: {
            "items": [
                {"type": "t", "id": "batch-1", "data": {"x": 2}},
                {"type": "t", "id": "batch-2", "data": {"x": 3}},
            ],
        },
    )
    doc._validate_mutation_policy_commands(commands)
    runtime = _history_runtime_factory(doc)

    single = _dispatch_business_helper(
        doc,
        commands,
        "Business.Add",
        coordinator=runtime.coordinator,
    )
    assert [entity.id for entity in doc.list("t")] == ["single"]
    _replay_via_coordinator(runtime, single)
    assert doc.find("t", "single") is None
    _replay_via_coordinator(runtime, single, redo=True)
    assert doc.get("t", "single").data["x"] == 1

    batch = _dispatch_business_helper(
        doc,
        commands,
        "Business.BatchAdd",
        coordinator=runtime.coordinator,
    )
    assert [entity.id for entity in doc.list("t")] == [
        "single", "batch-1", "batch-2",
    ]
    _replay_via_coordinator(runtime, batch)
    assert [entity.id for entity in doc.list("t")] == ["single"]
    _replay_via_coordinator(runtime, batch, redo=True)
    assert [entity.id for entity in doc.list("t")] == [
        "single", "batch-1", "batch-2",
    ]


def test_protected_builtin_update_batch_update_and_reorder_reverse_exactly(
    _history_runtime_factory,
):
    _, doc = _doc(_policy(
        rename=["Business.Update"],
        fields={
            "x": ["Business.Update", "Business.BatchUpdate"],
            "order": ["Business.Reorder"],
        },
    ))
    first = doc.add(Entity(
        type="t", id="e1", name="old", data={"x": 1, "order": 0},
    ))
    second = doc.add(Entity(
        type="t", id="e2", data={"x": 10, "order": 1},
    ))
    third = doc.add(Entity(
        type="t", id="e3", data={"x": 100, "order": 2},
    ))
    commands = CommandRegistry()
    register_builtin_commands(commands)
    _register_business_helper(
        commands,
        "Business.Update",
        "entity.update",
        lambda current: {
            "type": "t", "id": "e1", "patches": {"x": 2},
            "name": "renamed",
            "expected_version": current.get("t", "e1").version,
        },
    )
    _register_business_helper(
        commands,
        "Business.BatchUpdate",
        "entity.batch_update",
        lambda current: {
            "type": "t",
            "items": [
                {
                    "id": "e1", "patches": {"x": 20},
                    "expected_version": current.get("t", "e1").version,
                },
                {
                    "id": "e2", "patches": {"x": 30},
                    "expected_version": current.get("t", "e2").version,
                },
            ],
        },
    )
    _register_business_helper(
        commands,
        "Business.Reorder",
        "entity.reorder",
        lambda _current: {"type": "t", "id": "e3", "before_id": "e1"},
    )
    doc._validate_mutation_policy_commands(commands)
    runtime = _history_runtime_factory(doc)

    update = _dispatch_business_helper(
        doc,
        commands,
        "Business.Update",
        coordinator=runtime.coordinator,
    )
    assert (first.name, first.data["x"]) == ("renamed", 2)
    _replay_via_coordinator(runtime, update)
    assert (first.name, first.data["x"]) == ("old", 1)
    _replay_via_coordinator(runtime, update, redo=True)
    assert (first.name, first.data["x"]) == ("renamed", 2)

    batch = _dispatch_business_helper(
        doc,
        commands,
        "Business.BatchUpdate",
        coordinator=runtime.coordinator,
    )
    assert (first.data["x"], second.data["x"]) == (20, 30)
    _replay_via_coordinator(runtime, batch)
    assert (first.data["x"], second.data["x"]) == (2, 10)
    _replay_via_coordinator(runtime, batch, redo=True)
    assert (first.data["x"], second.data["x"]) == (20, 30)

    reorder = _dispatch_business_helper(
        doc,
        commands,
        "Business.Reorder",
        coordinator=runtime.coordinator,
    )
    assert [
        first.data["order"], second.data["order"], third.data["order"],
    ] == [1, 2, 0]
    _replay_via_coordinator(runtime, reorder)
    assert [
        first.data["order"], second.data["order"], third.data["order"],
    ] == [0, 1, 2]
    _replay_via_coordinator(runtime, reorder, redo=True)
    assert [
        first.data["order"], second.data["order"], third.data["order"],
    ] == [1, 2, 0]


def test_protected_builtin_delete_restore_and_hard_remove_reverse_exactly(
    _history_runtime_factory,
):
    _, doc = _doc(_policy(
        soft_delete=["Business.Delete"],
        restore=["Business.Restore"],
        hard_remove=["Business.Remove"],
    ))
    deleted_target = doc.add(Entity(type="t", id="delete", data={"x": 1}))
    restore_target = doc.add(Entity(type="t", id="restore", data={"x": 2}))
    restore_target.delete(expected_version=1, actor=ACTOR)
    remove_target = doc.add(Entity(type="t", id="remove", data={"x": 3}))
    commands = CommandRegistry()
    register_builtin_commands(commands)
    _register_business_helper(
        commands,
        "Business.Delete",
        "entity.delete",
        lambda current: {
            "type": "t", "id": "delete",
            "expected_version": current.get("t", "delete").version,
        },
    )
    _register_business_helper(
        commands,
        "Business.Restore",
        "entity.restore",
        lambda current: {
            "type": "t", "id": "restore",
            "expected_version": current.get("t", "restore").version,
        },
    )
    _register_business_helper(
        commands,
        "Business.Remove",
        "entity.hard_remove",
        lambda _current: {"type": "t", "id": "remove"},
    )
    doc._validate_mutation_policy_commands(commands)
    runtime = _history_runtime_factory(doc)

    delete = _dispatch_business_helper(
        doc,
        commands,
        "Business.Delete",
        coordinator=runtime.coordinator,
    )
    assert deleted_target.deleted is True
    _replay_via_coordinator(runtime, delete)
    assert deleted_target.deleted is False
    _replay_via_coordinator(runtime, delete, redo=True)
    assert deleted_target.deleted is True

    restore = _dispatch_business_helper(
        doc,
        commands,
        "Business.Restore",
        coordinator=runtime.coordinator,
    )
    assert restore_target.deleted is False
    _replay_via_coordinator(runtime, restore)
    assert restore_target.deleted is True
    _replay_via_coordinator(runtime, restore, redo=True)
    assert restore_target.deleted is False

    remove = _dispatch_business_helper(
        doc,
        commands,
        "Business.Remove",
        coordinator=runtime.coordinator,
    )
    assert doc.find("t", "remove") is None
    _replay_via_coordinator(runtime, remove)
    assert doc.get("t", "remove").to_dict() == remove_target.to_dict()
    _replay_via_coordinator(runtime, remove, redo=True)
    assert doc.find("t", "remove") is None


def test_protected_builtin_cascade_delete_succeeds_and_reverses_chain(
    _history_runtime_factory,
):
    ref_schema = {
        "type": "object",
        "properties": {
            "r": {
                "type": "object",
                "properties": {"$ref": {"type": "string"}},
                "required": ["$ref"],
                "additionalProperties": False,
            },
        },
        "additionalProperties": False,
    }
    registry = DocumentRegistry()
    cascade_policy = _policy(soft_delete=["Business.Cascade"])
    registry.declare_entity_type(
        name="a", schema=ref_schema, mutation_policy=cascade_policy,
    )
    registry.declare_entity_type(
        name="b", schema=ref_schema, mutation_policy=cascade_policy,
    )
    registry.set_collaboration_mode("with_save")
    doc = registry.build_document(schema_version="1.0", id="cascade")
    parent = doc.add(Entity(
        type="a", id="parent", data={"r": {"$ref": "b/child"}},
    ))
    child = doc.add(Entity(type="b", id="child", data={}))
    commands = CommandRegistry()
    register_builtin_commands(commands)
    _register_business_helper(
        commands,
        "Business.Cascade",
        "entity.cascade_delete",
        lambda _current: {"type": "a", "id": "parent"},
    )
    doc._validate_mutation_policy_commands(commands)
    runtime = _history_runtime_factory(doc)

    cascade = _dispatch_business_helper(
        doc,
        commands,
        "Business.Cascade",
        coordinator=runtime.coordinator,
    )
    assert parent.deleted is True
    assert child.deleted is True
    assert len(cascade.patches) == 2
    _replay_via_coordinator(runtime, cascade)
    assert parent.deleted is False
    assert child.deleted is False
    _replay_via_coordinator(runtime, cascade, redo=True)
    assert parent.deleted is True
    assert child.deleted is True


@pytest.mark.parametrize("initial", ["missing", None, 7])
def test_dot_path_ancestry_round_trips_missing_null_and_scalar_parent(
    initial,
    _history_runtime_factory,
):
    _, doc = _doc(_policy(fields={"maybe": ["Business.Dot"]}))
    data = {} if initial == "missing" else {"maybe": initial}
    entity = doc.add(Entity(type="t", id="e1", data=data))
    commands = CommandRegistry()
    register_builtin_commands(commands)
    _register_business_helper(
        commands,
        "Business.Dot",
        "entity.update",
        lambda current: {
            "type": "t",
            "id": "e1",
            "patches": {"maybe.a": 2},
            "expected_version": current.get("t", "e1").version,
        },
    )
    _seal_commands(doc, commands)
    runtime = _history_runtime_factory(doc)

    result = _coordinated_dispatch(
        doc,
        commands,
        "Business.Dot",
        coordinator=runtime.coordinator,
    )
    helper = result.operations[1]
    assert entity.data["maybe"] == {"a": 2}
    assert helper.patches[0].ancestry is not None

    _replay_via_coordinator(runtime, helper)
    if initial == "missing":
        assert "maybe" not in entity.data
    else:
        assert entity.data["maybe"] == initial
    _replay_via_coordinator(runtime, helper, redo=True)
    assert entity.data["maybe"] == {"a": 2}


def test_dot_path_ai_provenance_uses_raw_path_but_policy_uses_root(
    _history_runtime_factory,
):
    _, doc = _doc(_policy(fields={"box": ["Business.AI"]}))
    entity = doc.add(Entity(type="t", id="e1", data={"box": {"a": 1}}))
    provenance = AiProvenance(actor="u1", model="m", trace_id="trace")
    commands = CommandRegistry()
    register_builtin_commands(commands)
    _register_business_helper(
        commands,
        "Business.AI",
        "entity.update",
        lambda current: {
            "type": "t",
            "id": "e1",
            "patches": {"box.a": 2},
            "expected_version": current.get("t", "e1").version,
            "ai_provenance": {"box.a": provenance},
        },
    )
    _seal_commands(doc, commands)
    runtime = _history_runtime_factory(doc)

    result = _coordinated_dispatch(
        doc,
        commands,
        "Business.AI",
        coordinator=runtime.coordinator,
    )
    helper = result.operations[1]
    assert entity.provenance("box.a").to_dict() == provenance.to_dict()
    assert helper.patches[0].provenance["field"] == "box.a"

    _replay_via_coordinator(runtime, helper)
    assert entity.data["box"]["a"] == 1
    assert entity.provenance("box.a") is None
    _replay_via_coordinator(runtime, helper, redo=True)
    assert entity.provenance("box.a").to_dict() == provenance.to_dict()


def test_hard_remove_reverse_restores_middle_collection_position_with_overlay(
    _history_runtime_factory,
):
    _, doc = _doc(_policy(hard_remove=["Business.Remove"]))
    first = doc.add(Entity(type="t", id="e1", data={"x": 1}))
    middle = doc.add(Entity(type="t", id="e2", data={"x": 2}))
    last = doc.add(Entity(type="t", id="e3", data={"x": 3}))
    overlay = doc.add(Entity(
        type="t", id="overlay", data={"x": 4}, read_only=True,
    ))
    commands = CommandRegistry()
    register_builtin_commands(commands)
    _register_business_helper(
        commands,
        "Business.Remove",
        "entity.hard_remove",
        lambda current: {"type": "t", "id": "e2"},
    )
    _seal_commands(doc, commands)
    runtime = _history_runtime_factory(doc)

    result = _coordinated_dispatch(
        doc,
        commands,
        "Business.Remove",
        coordinator=runtime.coordinator,
    )
    helper = result.operations[1]
    assert [item.id for item in doc.list("t")] == ["e1", "e3", "overlay"]
    assert helper.patches[0].position == 1

    _replay_via_coordinator(runtime, helper)
    assert [item.id for item in doc.list("t")] == [
        first.id, middle.id, last.id, overlay.id,
    ]


def _same_id_replacement_fixture(*, forge: str = ""):
    _, doc = _doc(_policy(
        create=["Business.Replace"],
        hard_remove=["Business.Replace"],
        fields={"x": ["Business.Replace"]},
    ))
    original = doc.add(Entity(type="t", id="e1", data={"x": 1}))
    original_payload = original.to_dict()
    commands = CommandRegistry()

    def execute(*, actor, source, doc, **_kwargs):
        old_position = doc._entity_collection_position(original)
        doc.hard_remove("t", original.id)
        replacement = doc.add(Entity(type="t", id="e1", data={"x": 2}))
        new_position = doc._entity_collection_position(replacement)
        new_payload = replacement.to_dict()
        create_payload = new_payload
        create_position = new_position
        if forge == "payload":
            create_payload = {
                **new_payload,
                "data": {**new_payload["data"], "x": 999},
            }
        elif forge == "position":
            create_position = new_position + 1
        forward = [
            Patch(
                "t", "e1", ENTITY_META_FIELD, "delete",
                original_payload, None, position=old_position,
            ),
            Patch(
                "t", "e1", ENTITY_META_FIELD, "create",
                None, create_payload, position=create_position,
            ),
        ]
        reverse = [
            Patch(
                "t", "e1", ENTITY_META_FIELD, "delete",
                new_payload, None, position=new_position,
            ),
            Patch(
                "t", "e1", ENTITY_META_FIELD, "create",
                None, original_payload, position=old_position,
            ),
        ]
        if forge == "forward_order":
            forward.reverse()
        elif forge == "reverse_order":
            reverse.reverse()
        return new_operation(
            command_id="Business.Replace",
            source=source,
            actor=actor.to_dict(),
            patches=tuple(forward),
            reverse_patches=tuple(reverse),
        )

    commands.register(CommandSpec(
        id="Business.Replace",
        label="replace",
        mutation_authority=True,
        execute=execute,
    ))
    _seal_commands(doc, commands)
    return doc, original, original_payload, commands


def test_same_id_hard_remove_then_create_replays_both_intermediate_states(
    _history_runtime_factory,
):
    doc, original, original_payload, commands = _same_id_replacement_fixture()
    runtime = _history_runtime_factory(doc)

    operation = _coordinated_dispatch(
        doc,
        commands,
        "Business.Replace",
        coordinator=runtime.coordinator,
    )
    replacement_payload = doc.get("t", "e1").to_dict()
    assert doc.get("t", "e1") is not original
    assert replacement_payload["data"]["x"] == 2

    _replay_via_coordinator(runtime, operation)
    assert doc.get("t", "e1").to_dict() == original_payload
    _replay_via_coordinator(runtime, operation, redo=True)
    assert doc.get("t", "e1").to_dict() == replacement_payload


@pytest.mark.parametrize(
    "forge",
    ("payload", "position", "forward_order", "reverse_order"),
)
def test_same_id_replacement_rejects_forged_intermediate_program(forge):
    doc, original, original_payload, commands = _same_id_replacement_fixture(
        forge=forge,
    )
    store = _DurableStore(doc)

    with pytest.raises(ZeError) as captured:
        CommitCoordinator(doc, store).execute(
            lambda: commands.dispatch(
                "Business.Replace", {}, actor=ACTOR, source="user", doc=doc,
            )
        )

    assert captured.value.code == "mutation_operation_patch_mismatch"
    assert store.calls == 0
    assert doc.get("t", "e1") is original
    assert original.to_dict() == original_payload


def test_unprotected_type_keeps_legacy_primary_operation_identity():
    registry = DocumentRegistry()
    registry.declare_entity_type(
        name="t", schema=SCHEMA, mutation_policy=_policy(fields={"x": ["T"]}),
    )
    registry.declare_entity_type(name="u", schema=SCHEMA)
    registry.set_collaboration_mode("with_save")
    doc = registry.build_document(schema_version="1.0", id="mixed")
    entity = doc.add(Entity(type="u", id="u1", data={"x": 1}))
    commands = CommandRegistry()

    def execute(*, args, actor, source, doc):
        entity.update({"x": 2}, expected_version=1, actor=actor)
        return new_operation(
            command_id="entity.update",
            source=source,
            actor=actor.to_dict(),
            patches=[Patch("u", "u1", "x", "set", 1, 2)],
            reverse_patches=[Patch("u", "u1", "x", "set", 2, 1)],
        )

    commands.register(CommandSpec(id="Legacy", label="Legacy", execute=execute))
    _seal_commands(doc, commands)
    commands.dispatch("Legacy", {}, actor=ACTOR, source="user", doc=doc)
    assert entity.data["x"] == 2


def test_normal_command_rejects_forged_hard_remove_entity_envelope():
    _, doc = _doc(_policy(hard_remove=["Business.Remove"]))
    entity = doc.add(Entity(type="t", id="e1", data={"x": 1}))
    before = entity.to_dict()
    commands = CommandRegistry()

    def execute(*, args, actor, source, doc):
        position = doc._entity_collection_position(entity)
        doc.hard_remove("t", "e1")
        forged = dict(before)
        forged["version"] = 99
        return new_operation(
            command_id="Business.Remove",
            source=source,
            actor=actor.to_dict(),
            patches=[Patch(
                "t", "e1", ENTITY_META_FIELD, "delete", forged, None,
                position=position,
            )],
            reverse_patches=[Patch(
                "t", "e1", ENTITY_META_FIELD, "create", None, forged,
                position=position,
            )],
        )

    commands.register(CommandSpec(
        id="Business.Remove", label="remove", mutation_authority=True,
        execute=execute,
    ))
    _seal_commands(doc, commands)
    with pytest.raises(ZeError) as captured:
        _coordinated_dispatch(doc, commands, "Business.Remove")
    assert captured.value.code == "mutation_operation_patch_mismatch"
    assert doc.get("t", "e1") is entity
    assert entity.to_dict() == before


def test_entity_clock_rollback_remains_a_valid_protected_command(monkeypatch):
    _, doc = _doc(_policy(fields={"x": ["Business.Clock"]}))
    entity = doc.add(Entity(type="t", id="e1", data={"x": 1}))
    commands = CommandRegistry()
    register_builtin_commands(commands)
    _register_business_helper(
        commands,
        "Business.Clock",
        "entity.update",
        lambda current: {
            "type": "t",
            "id": "e1",
            "patches": {"x": current.get("t", "e1").data["x"] + 1},
            "expected_version": current.get("t", "e1").version,
        },
    )
    _seal_commands(doc, commands)
    baseline = entity.updated_at
    monkeypatch.setattr(entity_module.time, "time", lambda: baseline + 100)
    _coordinated_dispatch(doc, commands, "Business.Clock")
    future = entity.updated_at
    monkeypatch.setattr(entity_module.time, "time", lambda: baseline + 50)
    _coordinated_dispatch(doc, commands, "Business.Clock")
    assert entity.updated_at == future
    assert [entry["ts"] for entry in entity.actor_log()] == [future, future]


def test_builtin_lifecycle_noops_preserve_cas_and_read_only_guards():
    _, doc = _doc(_policy(
        soft_delete=["Business.Delete"],
        restore=["Business.Restore"],
    ))
    entity = doc.add(Entity(type="t", id="e1", data={"x": 1}))
    entity.delete(expected_version=1, actor=ACTOR)
    deleted_version = entity.version
    overlay = doc.add(Entity(
        type="t", id="overlay", data={"x": 2}, read_only=True,
    ))
    commands = CommandRegistry()
    register_builtin_commands(commands)
    _register_business_helper(
        commands,
        "Business.Delete",
        "entity.delete",
        lambda current: {
            "type": "t", "id": "e1", "expected_version": deleted_version,
        },
    )
    _register_business_helper(
        commands,
        "Business.Restore",
        "entity.restore",
        lambda current: {
            "type": "t", "id": "overlay", "expected_version": overlay.version,
        },
    )
    _seal_commands(doc, commands)

    result = _coordinated_dispatch(doc, commands, "Business.Delete")
    assert result.operations[1].patches == ()
    assert entity.version == deleted_version

    with pytest.raises(ZeError) as stale:
        _coordinated_dispatch(
            doc,
            commands,
            "entity.delete",
            {"type": "t", "id": "e1", "expected_version": deleted_version - 1},
        )
    assert stale.value.code == "cas_version_mismatch"

    with pytest.raises(ZeError) as read_only:
        _coordinated_dispatch(doc, commands, "Business.Restore")
    assert read_only.value.code == "read_only_violation"


def test_sealed_policy_bit_is_runtime_authority_and_rolls_back_before_laundering():
    _, doc = _doc(_policy(fields={"x": ["Business.Seal"]}))
    entity = doc.add(Entity(type="t", id="e1", data={"x": 1}))
    commands = CommandRegistry()

    def execute(*, args, actor, source, doc):
        doc._mutation_policy_sealed = False
        return _op("Business.Seal")

    commands.register(CommandSpec(
        id="Business.Seal",
        label="seal",
        mutation_authority=True,
        execute=execute,
    ))
    doc._validate_mutation_policy_commands(commands)
    assert doc._mutation_policy_sealed is True

    store = _DurableStore(doc)
    with pytest.raises(ZeError) as captured:
        CommitCoordinator(doc, store).execute(
            lambda: commands.dispatch(
                "Business.Seal", {}, actor=ACTOR, source="user", doc=doc,
            )
        )

    assert captured.value.code == "mutation_policy_authority_mutated"
    assert doc._mutation_policy_sealed is True
    assert store.calls == 0
    with pytest.raises(ZeError) as direct_write:
        entity.update({"x": 2}, expected_version=1, actor=ACTOR)
    assert direct_write.value.code == "mutation_policy_coordinator_required"
    assert entity.data["x"] == 1


def _sealed_command_fixture():
    _, doc = _doc(_policy(
        create=["Business.Update"],
        rename=["Business.Update"],
        soft_delete=["Business.Update"],
        restore=["Business.Update"],
        hard_remove=["Business.Update"],
        fields={"x": ["Business.Update"]},
    ))
    active = doc.add(Entity(type="t", id="active", name="old", data={"x": 1}))
    deleted = doc.add(Entity(type="t", id="deleted", data={"x": 1}))
    deleted.delete(expected_version=1, actor=ACTOR)
    removable = doc.add(Entity(type="t", id="removable", data={"x": 1}))
    commands = CommandRegistry()

    def execute(*, args, actor, source, doc):
        before = active.data["x"]
        active.update(
            {"x": before + 1}, expected_version=active.version, actor=actor,
        )
        return _field_op(
            "Business.Update", before, before + 1, entity_id="active",
        )

    commands.register(CommandSpec(
        id="Business.Update",
        label="update",
        mutation_authority=True,
        execute=execute,
    ))
    doc._validate_mutation_policy_commands(commands)
    return doc, active, deleted, removable, commands


@pytest.mark.parametrize(
    "mutate",
    [
        lambda doc, active, deleted, removable: active.update(
            {"x": 2}, expected_version=active.version, actor=ACTOR,
        ),
        lambda doc, active, deleted, removable: active.update(
            {}, expected_version=active.version, actor=ACTOR, name="new",
        ),
        lambda doc, active, deleted, removable: active.delete(
            expected_version=active.version, actor=ACTOR,
        ),
        lambda doc, active, deleted, removable: deleted.restore(
            expected_version=deleted.version, actor=ACTOR,
        ),
        lambda doc, active, deleted, removable: doc.add(
            Entity(type="t", id="new", data={"x": 1}),
        ),
        lambda doc, active, deleted, removable: doc.hard_remove(
            "t", removable.id,
        ),
    ],
    ids=["update", "rename", "soft_delete", "restore", "add", "hard_remove"],
)
def test_sealed_protected_public_mutators_require_unified_runtime_authority(mutate):
    doc, active, deleted, removable, _ = _sealed_command_fixture()
    before = tuple(
        (entity.id, entity.to_dict())
        for entity in doc.list("t", include_deleted=True)
    )

    with pytest.raises(ZeError) as captured:
        mutate(doc, active, deleted, removable)

    assert captured.value.code == "mutation_policy_coordinator_required"
    assert tuple(
        (entity.id, entity.to_dict())
        for entity in doc.list("t", include_deleted=True)
    ) == before


def test_sealed_direct_dispatch_cannot_mutate_without_coordinator_and_rolls_back():
    doc, active, _, _, commands = _sealed_command_fixture()
    before = active.to_dict()

    with pytest.raises(ZeError) as captured:
        commands.dispatch(
            "Business.Update", {}, actor=ACTOR, source="user", doc=doc,
        )

    assert captured.value.code == "mutation_policy_coordinator_required"
    assert active.to_dict() == before


def test_failed_direct_write_cannot_be_laundered_by_later_noop_commit():
    doc, active, _, _, _ = _sealed_command_fixture()
    store = _DurableStore(doc)

    with pytest.raises(ZeError) as captured:
        active.update({"x": 99}, expected_version=1, actor=ACTOR)
    assert captured.value.code == "mutation_policy_coordinator_required"

    execution = CommitCoordinator(doc, store).execute(
        lambda: _op("Unprotected.Noop"),
    )
    assert execution.receipt is not None
    assert execution.receipt.status.value == "committed"
    assert store.calls == 1
    assert active.data["x"] == 1


def test_manual_mutation_session_does_not_grant_protected_command_authority():
    doc, active, _, _, _ = _sealed_command_fixture()
    session = doc.begin_mutation_session()
    try:
        with pytest.raises(ZeError) as captured:
            active.update({"x": 2}, expected_version=1, actor=ACTOR)
        assert captured.value.code == "mutation_policy_coordinator_required"
        assert active.data["x"] == 1
    finally:
        session.rollback()


def test_sealed_protected_write_succeeds_only_through_coordinator_and_command():
    doc, active, _, _, commands = _sealed_command_fixture()
    store = _DurableStore(doc)

    execution = CommitCoordinator(doc, store).execute(
        lambda: commands.dispatch(
            "Business.Update", {}, actor=ACTOR, source="user", doc=doc,
        )
    )

    assert execution.receipt is not None
    assert execution.receipt.status.value == "committed"
    assert store.calls == 1
    assert active.data["x"] == 2


def test_store_public_dict_projection_cannot_change_validated_operation():
    doc, active, _, _, commands = _sealed_command_fixture()

    class ReflectingStore(_DurableStore):
        def __init__(self, document):
            super().__init__(document)
            self.body = None

        def commit(self, intent):
            operation = intent.operations[0]
            reflected = operation.__dict__
            reflected["actor"]["user_id"] = "forged"
            reflected["patches"][0].__dict__["after"] = 999
            self.body = operation.to_dict()
            return super().commit(intent)

    store = ReflectingStore(doc)
    execution = CommitCoordinator(doc, store).execute(
        lambda: commands.dispatch(
            "Business.Update", {}, actor=ACTOR, source="user", doc=doc,
        )
    )

    assert execution.is_committed
    assert active.data["x"] == 2
    assert store.body["actor"] == ACTOR.to_dict()
    assert store.body["patches"][0]["after"] == 2


def test_coordinator_consumes_exact_segment_proof_without_full_graph_reaudit(
    monkeypatch,
):
    doc, active, _, _, commands = _sealed_command_fixture()

    def duplicate_audit(*_args, **_kwargs):
        raise AssertionError("validated segment ledger must not be replayed again")

    monkeypatch.setattr(
        commit_coordinator_module,
        "validate_mutation_operation_coverage",
        duplicate_audit,
    )
    execution = CommitCoordinator(doc, _DurableStore(doc)).execute(
        lambda: commands.dispatch(
            "Business.Update", {}, actor=ACTOR, source="user", doc=doc,
        )
    )

    assert execution.is_committed
    assert active.data["x"] == 2


def test_external_thread_cannot_reuse_sealed_runtime_authority():
    doc, active, _, _, _ = _sealed_command_fixture()
    outcome = []

    def worker():
        try:
            active.update({"x": 2}, expected_version=1, actor=ACTOR)
        except BaseException as exc:
            outcome.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert len(outcome) == 1
    assert isinstance(outcome[0], ZeError)
    assert outcome[0].code == "mutation_policy_coordinator_required"
    assert active.data["x"] == 1
