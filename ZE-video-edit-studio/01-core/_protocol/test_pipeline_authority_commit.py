from __future__ import annotations

import copy
import pickle
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


HERE = Path(__file__).resolve().parent
OPERATION_DIR = HERE.parent / "03-commands-operations" / "02-operation"
COMMAND_DIR = HERE.parent / "03-commands-operations" / "01-command"
PERSISTENCE_DIR = HERE.parent / "02-data-flow" / "07-persistence"
for directory in (HERE, OPERATION_DIR, COMMAND_DIR, PERSISTENCE_DIR):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

import command as command_module
import operation as operation_module
import pipeline_authority_commit as commit_module
from file_store import FileCommitStore
from operation import OpLog, Operation, Patch, new_operation
from pipeline_authority_commit import (
    PipelineAuthorityCommitError,
    discard_pipeline_authority_commit,
    invalidate_pipeline_authority_attestations,
    is_pipeline_authority_attestation,
    issue_sealed_pipeline_control_operation,
    issue_sealed_pipeline_start_operation,
    prepare_pipeline_authority_commit,
    reconcile_durable_pipeline_authority,
)


DOCUMENT_ID = "doc-1"
RUN_ID = "run-1"
PIPELINE_ID = "asset-pipeline"
ROOT_ACTOR = {"user_id": "user-1", "device_id": "browser-1", "source": "user"}
WORKER_ACTOR = {"user_id": "__pipeline__", "device_id": "worker-1", "source": "ai"}
ROOT = object()


def _policy() -> dict[str, Any]:
    return {
        "schema": "ze.pipeline-document-policy.v1",
        "entity_policies": [{
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
        }],
    }


def _install_root_verifier(monkeypatch: pytest.MonkeyPatch) -> None:
    def consume(
        snapshot: object,
        *,
        document: Any,
        command_id: str,
        document_id: str,
        run_id: str,
        pipeline_id: str,
        entity_types: tuple[str, ...],
    ) -> Any:
        assert snapshot is ROOT
        assert document_id == document.id == DOCUMENT_ID
        assert run_id == RUN_ID and pipeline_id == PIPELINE_ID
        assert entity_types in ((), ("asset",))
        return SimpleNamespace(
            actor=copy.deepcopy(ROOT_ACTOR),
            source="user",
            command_id=command_id,
            document_id=document_id,
            root_command_digest="a" * 64,
            document_policy=_policy() if entity_types else {
                "schema": "ze.pipeline-document-policy.v1",
                "entity_policies": [],
            },
        )

    monkeypatch.setattr(command_module, "_consume_pipeline_authority_root", consume)


def _snapshot() -> dict[str, Any]:
    return {
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


def _initial_add() -> Operation:
    snapshot = _snapshot()
    return new_operation(
        command_id="entity.add",
        source="user",
        actor=ROOT_ACTOR,
        document_id=DOCUMENT_ID,
        patches=[Patch(
            "asset", "asset-1", "__entity__", "create",
            None, snapshot, position=0,
        )],
        reverse_patches=[Patch(
            "asset", "asset-1", "__entity__", "delete",
            snapshot, None, position=0,
        )],
    )


def _sealed_start(
    monkeypatch: pytest.MonkeyPatch,
    *,
    document: Any = None,
) -> tuple[Any, Operation, Operation]:
    _install_root_verifier(monkeypatch)
    if document is None:
        document = SimpleNamespace(id=DOCUMENT_ID)
    add = _initial_add()
    start = issue_sealed_pipeline_start_operation(
        _root_authority=ROOT,
        document=document,
        run_id=RUN_ID,
        pipeline_id=PIPELINE_ID,
        start_command_id="asset.generate",
        initial_operations=(add,),
        work_list={
            "run_id": RUN_ID,
            "name": "Generate",
            "items": [{
                "item_id": "item-1",
                "entity_type": "asset",
                "entity_id": "asset-1",
                "artifacts": [],
                "payload": {},
            }],
        },
        stages=[{
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
        }],
        placeholder_bindings=[{
            "item_id": "item-1",
            "entity_type": "asset",
            "entity_id": "asset-1",
            "operation_id": add.id,
        }],
        concurrency=1,
        max_retries=0,
        parameters={},
        worker_actor=WORKER_ACTOR,
        worker_source="ai",
        targets=[{
            "entity_type": "asset",
            "entity_id": "asset-1",
            "fields": ["status"],
            "lifecycle": ["create"],
        }],
    )
    return document, start, add


def _store(tmp_path: Path, log: OpLog | None = None) -> FileCommitStore:
    durable_log = log if log is not None else OpLog(tmp_path / "oplog.ndjson")
    return FileCommitStore(tmp_path / "document.json", durable_log)


def _forged_operation(operation: Operation) -> Operation:
    forged = object.__new__(Operation)
    object.__setattr__(
        forged,
        "__dict__",
        copy.deepcopy(object.__getattribute__(operation, "__dict__")),
    )
    return forged


def test_sealed_start_reserves_id_and_generic_copy_paths_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _document, start, _add = _sealed_start(monkeypatch)
    assert re.fullmatch(r"[0-9a-f]{32}", start.id)
    assert start.kind == "stage" and start.patches == ()
    assert start.pipeline["start_operation_id"] == start.id

    for action in (
        lambda: copy.copy(start),
        lambda: copy.deepcopy(start),
        lambda: replace(start),
        lambda: pickle.loads(pickle.dumps(start)),
    ):
        with pytest.raises(TypeError, match="sealed authority factory"):
            action()


@pytest.mark.parametrize("persistent", (False, True))
def test_raw_forged_v2_append_is_rejected_before_memory_or_disk_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    persistent: bool,
) -> None:
    _document, start, _add = _sealed_start(monkeypatch)
    path = tmp_path / "raw-forged.ndjson"
    op_log = OpLog(path if persistent else None)

    with pytest.raises(PipelineAuthorityCommitError) as raised:
        op_log.append(_forged_operation(start))
    assert raised.value.code == "pipeline_authority_seal_required"
    assert raised.value.reason == "pipeline_authority_operation_unsealed"
    assert op_log.entries() == []
    assert not path.exists() or path.read_bytes() == b""


@pytest.mark.parametrize("persistent", (False, True))
def test_sealed_v2_without_prepared_permit_cannot_append(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    persistent: bool,
) -> None:
    _document, start, _add = _sealed_start(monkeypatch)
    path = tmp_path / "sealed-unbound.ndjson"
    op_log = OpLog(path if persistent else None)

    with pytest.raises(PipelineAuthorityCommitError) as raised:
        op_log.append(start)
    assert raised.value.code == "pipeline_authority_append_required"
    assert raised.value.reason == "pipeline_authority_append_permit_missing"
    assert op_log.entries() == []
    assert not path.exists() or path.read_bytes() == b""


def test_v2_append_fails_closed_when_private_hooks_are_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _document, start, _add = _sealed_start(monkeypatch)
    monkeypatch.setattr(operation_module, "_PIPELINE_AUTHORITY_APPEND_HOOKS", None)

    with pytest.raises(Exception) as raised:
        OpLog().append(start)
    assert getattr(raised.value, "code", "") == "pipeline_authority_append_required"
    assert raised.value.context["reason"] == "pipeline_authority_append_hooks_missing"


def test_pre_store_and_durable_fresh_reconcile_issue_noncopyable_attestation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _document, start, add = _sealed_start(monkeypatch)
    store = _store(tmp_path)
    permit = prepare_pipeline_authority_commit(
        store, (start, add), document=_document,
    )
    assert permit is not None and permit.expected_head_count == 2
    with pytest.raises(TypeError, match="cannot be copied"):
        copy.copy(permit)

    store.op_log.append(start)
    store.op_log.append(add)
    (attestation,) = reconcile_durable_pipeline_authority(store, permit)
    assert is_pipeline_authority_attestation(attestation)
    assert (attestation.document_id, attestation.run_id, attestation.pipeline_id) == (
        DOCUMENT_ID, RUN_ID, PIPELINE_ID,
    )
    assert attestation.state == "paused" and attestation.generation == 0
    assert attestation.history_head_count == 2
    with pytest.raises(TypeError, match="cannot be copied"):
        copy.deepcopy(attestation)
    with pytest.raises(PipelineAuthorityCommitError) as reused:
        reconcile_durable_pipeline_authority(store, permit)
    assert reused.value.outcome_unknown is True
    assert reused.value.durability_phase == "post_write_verification"
    discard_pipeline_authority_commit(permit)  # reconciled cleanup is idempotent/no-throw


def test_sealed_operation_cannot_move_to_same_id_foreign_document(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document, start, add = _sealed_start(monkeypatch)
    same_id_foreign = SimpleNamespace(id=document.id)

    with pytest.raises(PipelineAuthorityCommitError) as rejected:
        prepare_pipeline_authority_commit(
            _store(tmp_path),
            (start, add),
            document=same_id_foreign,
        )
    assert rejected.value.code == "pipeline_authority_commit_invalid"
    assert rejected.value.reason == "pipeline_authority_document_binding_mismatch"

    seal = commit_module._OPERATION_SEALS[id(start)]
    seal.document = same_id_foreign
    with pytest.raises(PipelineAuthorityCommitError) as changed:
        prepare_pipeline_authority_commit(
            _store(tmp_path / "changed"),
            (start, add),
            document=document,
        )
    assert changed.value.code == "pipeline_authority_seal_invalid"
    assert changed.value.reason == "pipeline_authority_operation_changed"


def test_append_authorization_binds_exact_log_object_and_batch_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _document, start, add = _sealed_start(monkeypatch)
    store = _store(tmp_path / "primary")
    foreign = _store(tmp_path / "foreign")
    permit = prepare_pipeline_authority_commit(
        store, (start, add), document=_document,
    )
    extra = new_operation(
        command_id="item.audit",
        source="user",
        actor=ROOT_ACTOR,
        document_id=DOCUMENT_ID,
        patches=[],
        reverse_patches=[],
    )

    with pytest.raises(PipelineAuthorityCommitError) as wrong_log:
        foreign.op_log.append(start)
    assert wrong_log.value.reason == "pipeline_authority_append_log_mismatch"
    with pytest.raises(PipelineAuthorityCommitError) as wrong_order:
        store.op_log.append(add)
    assert wrong_order.value.reason == "pipeline_authority_append_order_mismatch"
    with pytest.raises(PipelineAuthorityCommitError) as interleaved:
        store.op_log.append(extra)
    assert interleaved.value.reason == "pipeline_authority_append_order_mismatch"

    store.op_log.append(start)
    with pytest.raises(PipelineAuthorityCommitError) as duplicate:
        store.op_log.append(start)
    assert duplicate.value.reason == "pipeline_authority_append_permit_consumed"
    store.op_log.append(add)
    (attestation,) = reconcile_durable_pipeline_authority(store, permit)
    assert is_pipeline_authority_attestation(attestation)
    assert foreign.op_log.entries() == []


@pytest.mark.parametrize(
    "tamper",
    ("document", "reduction", "integrity_tag", "active_key"),
)
def test_attestation_rechecks_runtime_binding_and_reduction_facts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
) -> None:
    document, start, add = _sealed_start(monkeypatch)
    store = _store(tmp_path)
    permit = prepare_pipeline_authority_commit(
        store,
        (start, add),
        document=document,
    )
    store.op_log.append(start)
    store.op_log.append(add)
    (attestation,) = reconcile_durable_pipeline_authority(store, permit)
    facts = commit_module._attestation_facts(attestation)

    if tamper == "document":
        object.__setattr__(
            facts,
            "document",
            SimpleNamespace(id=DOCUMENT_ID),
        )
    elif tamper == "reduction":
        object.__setattr__(facts.reduction, "state", "running")
        object.__setattr__(
            facts,
            "reduction_fingerprint_sha256",
            commit_module._reduction_fingerprint_digest(facts.reduction),
        )
    elif tamper == "integrity_tag":
        object.__setattr__(
            commit_module._ATTESTATIONS[id(attestation)],
            "integrity_hmac_sha256",
            7,
        )
    else:
        object.__setattr__(
            commit_module._ATTESTATIONS[id(attestation)],
            "active_key",
            (id(store), DOCUMENT_ID, RUN_ID, "forged-pipeline"),
        )

    assert is_pipeline_authority_attestation(attestation) is False
    assert (
        id(store),
        DOCUMENT_ID,
        RUN_ID,
        PIPELINE_ID,
    ) not in commit_module._ACTIVE_ATTESTATIONS
    with pytest.raises(PipelineAuthorityCommitError) as changed:
        commit_module._attestation_facts(attestation)
    assert changed.value.reason in {
        "pipeline_authority_attestation_facts_changed",
        "pipeline_authority_attestation_stale",
    }


def test_clean_discard_releases_exact_seal_for_same_intent_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _document, start, add = _sealed_start(monkeypatch)
    store = _store(tmp_path)
    first = prepare_pipeline_authority_commit(
        store, (start, add), document=_document,
    )
    discard_pipeline_authority_commit(first)

    retry = prepare_pipeline_authority_commit(
        store, (start, add), document=_document,
    )
    store.op_log.append(start)
    store.op_log.append(add)
    (attestation,) = reconcile_durable_pipeline_authority(store, retry)
    assert is_pipeline_authority_attestation(attestation)


def test_reconcile_requires_every_exact_append_authorization_consumed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _document, start, add = _sealed_start(monkeypatch)
    store = _store(tmp_path)
    permit = prepare_pipeline_authority_commit(
        store, (start, add), document=_document,
    )

    with pytest.raises(PipelineAuthorityCommitError) as incomplete:
        reconcile_durable_pipeline_authority(store, permit)
    assert incomplete.value.reason == "pipeline_authority_append_incomplete"
    assert incomplete.value.outcome_unknown is True
    with pytest.raises(PipelineAuthorityCommitError) as cannot_reuse:
        store.op_log.append(start)
    assert cannot_reuse.value.reason == "pipeline_authority_append_permit_missing"


def test_consumed_append_authorization_is_never_restored_after_writer_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _document, start, add = _sealed_start(monkeypatch)
    store = _store(tmp_path)
    permit = prepare_pipeline_authority_commit(
        store, (start, add), document=_document,
    )

    def fail_before_write(_op_log: OpLog, _body: dict[str, Any]):
        raise OSError("injected append failure")

    monkeypatch.setattr(OpLog, "_persist_line", fail_before_write)
    with pytest.raises(OSError):
        store.op_log.append(start)
    with pytest.raises(PipelineAuthorityCommitError) as reused:
        store.op_log.append(start)
    assert reused.value.reason == "pipeline_authority_append_permit_missing"
    discard_pipeline_authority_commit(permit)
    with pytest.raises(PipelineAuthorityCommitError) as reprepare:
        prepare_pipeline_authority_commit(
            _store(tmp_path / "retry"),
            (start, add),
            document=_document,
        )
    assert reprepare.value.reason == "pipeline_authority_seal_spent"


def test_one_seal_cannot_prepare_to_two_stores_or_two_concurrent_permits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _document, start, add = _sealed_start(monkeypatch)
    first_store = _store(tmp_path / "first")
    second_store = _store(tmp_path / "second")
    first = prepare_pipeline_authority_commit(
        first_store, (start, add), document=_document,
    )
    with pytest.raises(PipelineAuthorityCommitError) as second:
        prepare_pipeline_authority_commit(
            second_store, (start, add), document=_document,
        )
    assert second.value.reason == "pipeline_authority_seal_already_claimed"
    discard_pipeline_authority_commit(first)

    barrier = threading.Barrier(2)

    def prepare(store: FileCommitStore):
        barrier.wait()
        try:
            return prepare_pipeline_authority_commit(
                store, (start, add), document=_document,
            )
        except PipelineAuthorityCommitError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(prepare, (first_store, second_store)))
    permits = [value for value in results if not isinstance(value, Exception)]
    failures = [value for value in results if isinstance(value, PipelineAuthorityCommitError)]
    assert len(permits) == 1
    assert len(failures) == 1
    assert failures[0].reason == "pipeline_authority_seal_already_claimed"
    discard_pipeline_authority_commit(permits[0])


def test_start_commit_rejects_operations_before_or_after_its_declared_tuple(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _document, start, add = _sealed_start(monkeypatch)
    extra = new_operation(
        command_id="item.audit",
        source="user",
        actor=ROOT_ACTOR,
        document_id=DOCUMENT_ID,
        patches=[],
        reverse_patches=[],
    )
    store = _store(tmp_path)

    with pytest.raises(PipelineAuthorityCommitError) as before:
        prepare_pipeline_authority_commit(
            store, (extra, start, add), document=_document,
        )
    assert before.value.code == "pipeline_start_prefix_incomplete"
    assert before.value.reason == "pipeline_start_not_commit_head"
    assert before.value.outcome_unknown is False
    assert before.value.durability_phase == "preflight"

    with pytest.raises(PipelineAuthorityCommitError) as after:
        prepare_pipeline_authority_commit(
            store, (start, add, extra), document=_document,
        )
    assert after.value.code == "pipeline_start_prefix_incomplete"
    assert after.value.reason == "pipeline_start_commit_members_mismatch"
    assert after.value.outcome_unknown is False
    assert after.value.durability_phase == "preflight"


def test_one_commit_cannot_introduce_multiple_pipeline_starts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _document, first_start, first_add = _sealed_start(monkeypatch)
    _document, second_start, second_add = _sealed_start(
        monkeypatch,
        document=_document,
    )

    with pytest.raises(PipelineAuthorityCommitError) as raised:
        prepare_pipeline_authority_commit(
            _store(tmp_path),
            (first_start, first_add, second_start, second_add),
            document=_document,
        )
    assert raised.value.code == "pipeline_start_prefix_incomplete"
    assert raised.value.reason == "pipeline_start_multiple_in_commit"
    assert raised.value.outcome_unknown is False
    assert raised.value.durability_phase == "preflight"


def test_control_is_sealed_and_reduces_only_after_exact_durable_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document, start, add = _sealed_start(monkeypatch)
    store = _store(tmp_path)
    start_permit = prepare_pipeline_authority_commit(
        store, (start, add), document=document,
    )
    store.op_log.append(start)
    store.op_log.append(add)
    reconcile_durable_pipeline_authority(store, start_permit)
    start_hash = store.op_log.entries()[0]["self_hash"]

    control = issue_sealed_pipeline_control_operation(
        _root_authority=ROOT,
        document=document,
        run_id=RUN_ID,
        pipeline_id=PIPELINE_ID,
        start_operation_id=start.id,
        start_operation_hash=start_hash,
        action="cancel",
        parameters={},
        expected_generation=0,
        expected_state="paused",
    )
    assert control.pipeline["control_operation_id"] == control.id
    permit = prepare_pipeline_authority_commit(
        store, (control,), document=document,
    )
    store.op_log.append(control)
    (attestation,) = reconcile_durable_pipeline_authority(store, permit)
    assert attestation.terminal is True
    assert attestation.state == "terminal" and attestation.generation == 1


def test_control_reconcile_revokes_old_attestation_before_fresh_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document, start, add = _sealed_start(monkeypatch)
    store = _store(tmp_path)
    start_permit = prepare_pipeline_authority_commit(
        store, (start, add), document=document,
    )
    store.op_log.append(start)
    store.op_log.append(add)
    (old_attestation,) = reconcile_durable_pipeline_authority(store, start_permit)
    assert is_pipeline_authority_attestation(old_attestation)
    start_hash = store.op_log.entries()[0]["self_hash"]

    control = issue_sealed_pipeline_control_operation(
        _root_authority=ROOT,
        document=document,
        run_id=RUN_ID,
        pipeline_id=PIPELINE_ID,
        start_operation_id=start.id,
        start_operation_hash=start_hash,
        action="cancel",
        parameters={},
        expected_generation=0,
        expected_state="paused",
    )
    control_permit = prepare_pipeline_authority_commit(
        store, (control,), document=document,
    )
    store.op_log.append(control)

    def fresh_scan_unavailable(_op_log: OpLog):
        raise OSError("injected fresh scan failure")

    monkeypatch.setattr(OpLog, "_canonical_history_snapshot", fresh_scan_unavailable)
    with pytest.raises(PipelineAuthorityCommitError) as failed:
        reconcile_durable_pipeline_authority(store, control_permit)
    assert failed.value.outcome_unknown is True
    assert failed.value.durability_phase == "post_write_verification"
    assert is_pipeline_authority_attestation(old_attestation) is False
    active_key = (id(store), DOCUMENT_ID, RUN_ID, PIPELINE_ID)
    assert active_key not in commit_module._ACTIVE_ATTESTATIONS
    assert all(
        facts.store is not store
        or (
            facts.reduction.document_id,
            facts.reduction.run_id,
            facts.reduction.pipeline_id,
        ) != (DOCUMENT_ID, RUN_ID, PIPELINE_ID)
        for facts in (
            entry.facts for entry in commit_module._ATTESTATIONS.values()
        )
    )


def test_multi_attestation_install_interrupt_rolls_back_every_new_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document, start, add = _sealed_start(monkeypatch)
    store = _store(tmp_path)
    start_permit = prepare_pipeline_authority_commit(
        store, (start, add), document=document,
    )
    store.op_log.append(start)
    store.op_log.append(add)
    (old_attestation,) = reconcile_durable_pipeline_authority(store, start_permit)
    start_hash = store.op_log.entries()[0]["self_hash"]

    control = issue_sealed_pipeline_control_operation(
        _root_authority=ROOT,
        document=document,
        run_id=RUN_ID,
        pipeline_id=PIPELINE_ID,
        start_operation_id=start.id,
        start_operation_hash=start_hash,
        action="cancel",
        parameters={},
        expected_generation=0,
        expected_state="paused",
    )
    control_permit = prepare_pipeline_authority_commit(
        store, (control,), document=document,
    )
    store.op_log.append(control)
    permit_entry = commit_module._claim_permit_for_reconcile(
        store,
        control_permit,
    )
    first = permit_entry.facts.reductions[0].reduction
    second = replace(first, run_id="run-2", pipeline_id="pipeline-2")

    class InterruptingAttestationLedger(dict):
        def __init__(self, source: dict) -> None:
            super().__init__(source)
            self.writes = 0

        def __setitem__(self, key: object, value: object) -> None:
            super().__setitem__(key, value)
            self.writes += 1
            if self.writes == 2:
                raise KeyboardInterrupt("injected multi-install interruption")

    ledger = InterruptingAttestationLedger(commit_module._ATTESTATIONS)
    monkeypatch.setattr(commit_module, "_ATTESTATIONS", ledger)

    with pytest.raises(KeyboardInterrupt):
        commit_module._register_attestations(
            store,
            (first, second),
            permit_entry,
        )

    affected = {
        (first.document_id, first.run_id, first.pipeline_id),
        (second.document_id, second.run_id, second.pipeline_id),
    }
    assert permit_entry.state == "repair_required"
    assert is_pipeline_authority_attestation(old_attestation) is False
    assert all(
        (id(store), *key) not in commit_module._ACTIVE_ATTESTATIONS
        for key in affected
    )
    assert all(
        facts.store is not store
        or (
            facts.reduction.document_id,
            facts.reduction.run_id,
            facts.reduction.pipeline_id,
        ) not in affected
        for facts in (entry.facts for entry in ledger.values())
    )


def test_storage_possible_invalidation_revokes_old_proof_but_allows_reconcile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document, start, add = _sealed_start(monkeypatch)
    store = _store(tmp_path)
    start_permit = prepare_pipeline_authority_commit(
        store, (start, add), document=document,
    )
    store.op_log.append(start)
    store.op_log.append(add)
    (old_attestation,) = reconcile_durable_pipeline_authority(store, start_permit)
    start_hash = store.op_log.entries()[0]["self_hash"]

    control = issue_sealed_pipeline_control_operation(
        _root_authority=ROOT,
        document=document,
        run_id=RUN_ID,
        pipeline_id=PIPELINE_ID,
        start_operation_id=start.id,
        start_operation_hash=start_hash,
        action="cancel",
        parameters={},
        expected_generation=0,
        expected_state="paused",
    )
    control_permit = prepare_pipeline_authority_commit(
        store, (control,), document=document,
    )
    invalidate_pipeline_authority_attestations(control_permit)
    invalidate_pipeline_authority_attestations(control_permit)
    assert is_pipeline_authority_attestation(old_attestation) is False
    assert (id(store), DOCUMENT_ID, RUN_ID, PIPELINE_ID) not in (
        commit_module._ACTIVE_ATTESTATIONS
    )

    store.op_log.append(control)
    (current_attestation,) = reconcile_durable_pipeline_authority(
        store,
        control_permit,
    )
    assert is_pipeline_authority_attestation(current_attestation)
    assert current_attestation.state == "terminal"


def test_process_control_after_durable_control_append_revokes_old_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document, start, add = _sealed_start(monkeypatch)
    store = _store(tmp_path)
    start_permit = prepare_pipeline_authority_commit(
        store, (start, add), document=document,
    )
    store.op_log.append(start)
    store.op_log.append(add)
    (old_attestation,) = reconcile_durable_pipeline_authority(store, start_permit)
    start_hash = store.op_log.entries()[0]["self_hash"]

    control = issue_sealed_pipeline_control_operation(
        _root_authority=ROOT,
        document=document,
        run_id=RUN_ID,
        pipeline_id=PIPELINE_ID,
        start_operation_id=start.id,
        start_operation_hash=start_hash,
        action="cancel",
        parameters={},
        expected_generation=0,
        expected_state="paused",
    )
    control_permit = prepare_pipeline_authority_commit(
        store, (control,), document=document,
    )
    persist = OpLog._persist_line

    def durable_then_interrupt(op_log: OpLog, body: dict[str, Any]):
        persist(op_log, body)
        raise KeyboardInterrupt("injected after durable append")

    monkeypatch.setattr(OpLog, "_persist_line", durable_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        store.op_log.append(control)
    assert store.op_log.entries()[-1]["op"]["id"] == control.id
    assert is_pipeline_authority_attestation(old_attestation) is False
    discard_pipeline_authority_commit(control_permit)
    with pytest.raises(PipelineAuthorityCommitError) as reused:
        store.op_log.append(control)
    assert reused.value.reason == "pipeline_authority_append_permit_missing"


def test_unsealed_exact_object_forgery_and_v1_never_receive_permit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _document, start, add = _sealed_start(monkeypatch)
    forged = object.__new__(Operation)
    object.__setattr__(
        forged,
        "__dict__",
        copy.deepcopy(object.__getattribute__(start, "__dict__")),
    )
    with pytest.raises(PipelineAuthorityCommitError) as rejected:
        prepare_pipeline_authority_commit(
            _store(tmp_path), (forged, add), document=_document,
        )
    assert rejected.value.code == "pipeline_authority_seal_required"

    v1 = new_operation(
        command_id="pipeline.start",
        source="system",
        actor=ROOT_ACTOR,
        patches=[],
        reverse_patches=[],
        kind="stage",
        pipeline={"schema": "ze.pipeline-start.v1"},
    )
    assert prepare_pipeline_authority_commit(object(), (v1,)) is None


def test_failed_or_concurrent_reconcile_is_single_consumer_and_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _document, start, add = _sealed_start(monkeypatch)
    store = _store(tmp_path)
    permit = prepare_pipeline_authority_commit(
        store, (start, add), document=_document,
    )
    store.op_log.append(start)
    store.op_log.append(add)
    barrier = threading.Barrier(2)

    def reconcile():
        barrier.wait()
        try:
            return reconcile_durable_pipeline_authority(store, permit)
        except PipelineAuthorityCommitError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(lambda _index: reconcile(), range(2)))
    assert sum(type(value) is tuple for value in results) == 1
    failures = [value for value in results if isinstance(value, PipelineAuthorityCommitError)]
    assert len(failures) == 1 and failures[0].outcome_unknown is True


def test_fork_reset_invalidates_seals_permits_and_attestations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _document, start, add = _sealed_start(monkeypatch)
    store = _store(tmp_path)
    permit = prepare_pipeline_authority_commit(
        store, (start, add), document=_document,
    )
    store.op_log.append(start)
    store.op_log.append(add)
    (attestation,) = reconcile_durable_pipeline_authority(store, permit)
    commit_module._reset_after_fork()
    assert is_pipeline_authority_attestation(attestation) is False
    with pytest.raises(PipelineAuthorityCommitError):
        prepare_pipeline_authority_commit(
            store, (start, add), document=_document,
        )


def test_commit_error_projects_safe_unknown_facts() -> None:
    error = PipelineAuthorityCommitError(
        "pipeline_authority_unavailable",
        "pipeline_authority_reconcile_unexpected",
        "durable authority reconciliation failed",
        outcome_unknown=True,
        durability_phase="post_write_verification",
    ).with_commit_facts({"commit_id": "c1", "retry_safe": False})
    assert error.message == str(error)
    assert error.context["commit_facts"] == {
        "commit_id": "c1", "retry_safe": False,
    }
    assert error.outcome_unknown is True
