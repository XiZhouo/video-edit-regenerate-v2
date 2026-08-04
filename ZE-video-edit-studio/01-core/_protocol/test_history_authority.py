"""Adversarial runtime tests for the canonical history authority.

The authority is deliberately process-local.  These tests treat the durable
OpLog as the only history source and assert that runtime capabilities cannot
be manufactured, promoted, or reused around the canonical coordinator.
"""

from __future__ import annotations

import dataclasses
import gc
import os
import sys
import threading
import weakref
from pathlib import Path
from typing import Any, Callable

import pytest


HERE = Path(__file__).resolve().parent
CORE = HERE.parent
OPERATION_DIR = CORE / "03-commands-operations" / "02-operation"
for directory in (HERE, OPERATION_DIR):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

import history_authority as authority_protocol  # noqa: E402
from operation import ZERO_HASH, OpLog, Operation, Patch, new_operation  # noqa: E402


class _Owner:
    pass


class _Document:
    def __init__(self, document_id: str = "project-history") -> None:
        self.id = document_id


class _Store:
    def __init__(self, op_log: OpLog) -> None:
        self.op_log = op_log


class _Coordinator:
    def __init__(self, document: _Document, store: _Store) -> None:
        self.document = document
        self.store = store


class _Controller:
    def __init__(self, op_log: OpLog, document: _Document) -> None:
        self._op_log = op_log
        self._doc = document


def _operation(
    label: str,
    *,
    group_id: str = "",
    kind: str = "native",
    before: str = "before",
    after: str = "after",
) -> Operation:
    patch = Patch("item", "item-1", "value", "set", before, after)
    reverse = Patch("item", "item-1", "value", "set", after, before)
    return new_operation(
        command_id=label,
        source="user",
        actor={"user_id": "tester"},
        patches=(patch,),
        reverse_patches=(reverse,),
        group_id=group_id,
        kind=kind,
    )


def _rollback_for(label: str, sources: tuple[Operation, ...]) -> Operation:
    patches = tuple(
        patch
        for source in reversed(sources)
        for patch in source.reverse_patches
    )
    reverse = tuple(
        patch
        for source in sources
        for patch in source.patches
    )
    return new_operation(
        command_id=label,
        source="user",
        actor={"user_id": "tester"},
        patches=patches,
        reverse_patches=reverse,
        kind="rollback",
    )


def _verify_replay_derivation(
    purpose: str,
    source_entries: tuple[dict[str, Any], ...],
    operation: Operation,
) -> bool:
    sources = tuple(entry["op"] for entry in source_entries)
    if purpose == "undo":
        expected_patches = tuple(
            patch for source in reversed(sources)
            for patch in source.get("reverse_patches", ())
        )
        expected_reverse = tuple(
            patch for source in sources for patch in source.get("patches", ())
        )
    elif purpose == "redo" and len(sources) == 1:
        expected_patches = tuple(sources[0].get("reverse_patches", ()))
        expected_reverse = tuple(sources[0].get("patches", ()))
    else:
        return False
    return (
        operation.kind == "rollback"
        and tuple(patch.to_dict() for patch in operation.patches) == expected_patches
        and tuple(patch.to_dict() for patch in operation.reverse_patches)
        == expected_reverse
    )
def _issue_capability(owner: _Owner) -> Any:
    # The factory is intentionally private in the finished protocol.  Keep the
    # fallback while the implementation migrates so behavioral failures remain
    # test-local instead of stopping collection.
    issue = getattr(
        authority_protocol,
        "_issue_history_bind_capability",
        getattr(authority_protocol, "issue_history_bind_capability", None),
    )
    assert callable(issue), "bootstrap has no one-shot history bind capability"
    return issue(bootstrap_owner=owner)


def _retire(authority: Any) -> None:
    retire = getattr(authority_protocol, "retire_history_authority", None)
    assert callable(retire), "history authority has no exact retire/unbind seam"
    retire(
        authority,
        _capability=authority_protocol._HISTORY_RETIRE_CAPABILITY,
    )


@pytest.mark.parametrize(
    ("reason", "outcome_unknown", "durability_phase"),
    (
        ("history_head_changed_before_store", False, "preflight"),
        ("history_suffix_incomplete", True, "post_write_verification"),
        ("history_suffix_verification_failed", True, "post_write_verification"),
        ("history_append_outcome_indeterminate", True, "post_write_verification"),
        ("history_commit_outcome_indeterminate", True, "commit_outcome"),
    ),
)
def test_history_authority_error_has_typed_durability_classification(
    reason: str,
    outcome_unknown: bool,
    durability_phase: str,
) -> None:
    error = authority_protocol.HistoryAuthorityError(reason, "history failed")

    assert error.reason == reason
    assert error.outcome_unknown is outcome_unknown
    assert error.durability_phase == durability_phase
    assert error.context == {
        "reason": reason,
        "outcome_unknown": outcome_unknown,
        "durability_phase": durability_phase,
    }


def test_history_authority_error_copies_neutral_commit_facts() -> None:
    error = authority_protocol.HistoryAuthorityError(
        "history_commit_outcome_indeterminate",
        "history failed",
    )
    facts = {
        "phase": "oplog",
        "outcome": "indeterminate",
        "commit_id": "commit-1",
        "retry_safe": False,
        "document_durable": True,
        "oplog_durable": False,
        "failure_facts": {"document": "durable", "oplog": "not_durable"},
    }

    assert error.with_commit_facts(facts) is error
    facts["failure_facts"]["oplog"] = "forged"

    assert error.commit_facts["failure_facts"]["oplog"] == "not_durable"
    assert error.context["commit_facts"] == error.commit_facts


def _open_transaction(coordinator: Any, store: Any):
    port = authority_protocol._history_coordinator_port(
        coordinator,
        _capability=authority_protocol._HISTORY_COORDINATOR_CAPABILITY,
    )
    assert port is not None
    return port.open(
        coordinator=coordinator,
        store=store,
        _capability=authority_protocol._HISTORY_COORDINATOR_CAPABILITY,
    )


def _runtime(
    tmp_path: Path,
    *,
    name: str = "oplog.ndjson",
    seed: tuple[Operation, ...] = (),
    capability: Any = None,
    owner: _Owner | None = None,
) -> tuple[Any, _Owner, _Document, OpLog, _Controller, _Store, _Coordinator]:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600, exist_ok=True)
    os.chmod(path, 0o600)
    op_log = OpLog(path)
    for operation in seed:
        op_log.append(operation)
    document = _Document()
    store = _Store(op_log)
    coordinator = _Coordinator(document, store)
    controller = _Controller(op_log, document)
    owner = owner or _Owner()
    capability = capability or _issue_capability(owner)
    authority = authority_protocol.bind_history_authority(
        capability,
        bootstrap_owner=owner,
        document=document,
        op_log=op_log,
        undo_controller=controller,
        coordinator=coordinator,
        store=store,
        project_id=document.id,
        generation=1,
        runtime_revision="runtime-1",
        replay_verifier=_verify_replay_derivation,
    )
    return authority, owner, document, op_log, controller, store, coordinator


def _write_large_history(path: Path, entry_count: int) -> None:
    """Build a valid persistent prefix without paying one fsync per fixture row."""
    path.parent.mkdir(parents=True, exist_ok=True)
    encoder = OpLog()
    template = _operation("performance.seed").to_dict()
    previous = ZERO_HASH
    with path.open("wb") as stream:
        for index in range(entry_count):
            body = dict(template)
            body["id"] = f"{index + 1:032x}"
            entry, line = encoder._encode_entry(previous, body)
            stream.write(line)
            previous = entry["self_hash"]
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(path, 0o600)


def _reason(exc: BaseException) -> str:
    context = getattr(exc, "context", {})
    return context.get("reason", "") if isinstance(context, dict) else ""


def test_unbound_oplog_keeps_legacy_append_contract(tmp_path: Path) -> None:
    path = tmp_path / "legacy.ndjson"
    log = OpLog(path)
    operation = _operation("legacy.update")

    entry = log.append(operation)

    assert entry["op"] == operation.to_dict()
    assert len(log) == 1
    assert log.verify_chain() is True


def test_bind_capability_is_not_public_and_authority_cannot_be_constructed() -> None:
    assert "issue_history_bind_capability" not in authority_protocol.__all__
    constructor = authority_protocol.HistoryAuthority
    with pytest.raises(TypeError):
        constructor(
            document=object(),
            op_log=object(),
            undo_controller=object(),
            coordinator=object(),
            store=object(),
            project_id="forged",
            generation=1,
            runtime_revision="forged",
            trusted_head=authority_protocol.HistoryHead(0, "0" * 64, "", None),
        )


def test_failed_graph_bind_does_not_consume_one_shot_capability(tmp_path: Path) -> None:
    owner = _Owner()
    capability = _issue_capability(owner)
    path = tmp_path / "retry.ndjson"
    path.touch(mode=0o600)
    log = OpLog(path)
    document = _Document()
    store = _Store(log)
    coordinator = _Coordinator(document, store)
    wrong_controller = _Controller(log, _Document("wrong"))

    with pytest.raises(authority_protocol.HistoryAuthorityError):
        authority_protocol.bind_history_authority(
            capability,
            bootstrap_owner=owner,
            document=document,
            op_log=log,
            undo_controller=wrong_controller,
            coordinator=coordinator,
            store=store,
            project_id=document.id,
            generation=1,
            runtime_revision="runtime-1",
            replay_verifier=_verify_replay_derivation,
        )

    controller = _Controller(log, document)
    authority = authority_protocol.bind_history_authority(
        capability,
        bootstrap_owner=owner,
        document=document,
        op_log=log,
        undo_controller=controller,
        coordinator=coordinator,
        store=store,
        project_id=document.id,
        generation=1,
        runtime_revision="runtime-1",
        replay_verifier=_verify_replay_derivation,
    )
    assert authority.state == "ACTIVE"


def test_duplicate_path_failure_is_atomic_and_capability_can_retry_after_retire(
    tmp_path: Path,
) -> None:
    first = _runtime(tmp_path, name="canonical.ndjson")
    first_authority = first[0]
    path = tmp_path / "canonical.ndjson"
    second_log = OpLog(path)
    second_doc = _Document()
    second_store = _Store(second_log)
    second_coordinator = _Coordinator(second_doc, second_store)
    second_controller = _Controller(second_log, second_doc)
    second_owner = _Owner()
    second_capability = _issue_capability(second_owner)

    with pytest.raises(authority_protocol.HistoryAuthorityError) as caught:
        authority_protocol.bind_history_authority(
            second_capability,
            bootstrap_owner=second_owner,
            document=second_doc,
            op_log=second_log,
            undo_controller=second_controller,
            coordinator=second_coordinator,
            store=second_store,
            project_id=second_doc.id,
            generation=1,
            runtime_revision="runtime-1",
            replay_verifier=_verify_replay_derivation,
        )
    assert _reason(caught.value) == "history_path_already_bound"
    assert authority_protocol.history_authority_for_coordinator(second_coordinator) is None
    assert authority_protocol.history_authority_for_controller(second_controller) is None

    _retire(first_authority)
    rebound = authority_protocol.bind_history_authority(
        second_capability,
        bootstrap_owner=second_owner,
        document=second_doc,
        op_log=second_log,
        undo_controller=second_controller,
        coordinator=second_coordinator,
        store=second_store,
        project_id=second_doc.id,
        generation=1,
        runtime_revision="runtime-1",
        replay_verifier=_verify_replay_derivation,
    )
    assert rebound.state == "ACTIVE"


def test_returned_authority_cannot_be_used_as_a_coordinator_bypass(tmp_path: Path) -> None:
    """Possessing a diagnostic handle must not grant the private store port.

    This is the exact dangerous sequence: no Document.save/CommitStore.commit,
    only a caller-created transaction followed by OpLog.append.  A valid
    design rejects before append; it must not merely produce a plausible head.
    """
    authority, _, _, log, _, store, coordinator = _runtime(tmp_path)
    operation = _operation("bypass.without.store")
    before = (tmp_path / "oplog.ndjson").read_bytes()

    try:
        transaction = authority.begin_transaction(
            coordinator=coordinator,
            store=store,
        )
        with transaction:
            transaction.prepare_store((operation,))
            with transaction.write_scope():
                log.append(operation)
            transaction.confirm_durable()
    except (AttributeError, TypeError, authority_protocol.HistoryAuthorityError):
        pass
    else:
        pytest.fail("ordinary authority holder bypassed CommitCoordinator/CommitStore")

    assert (tmp_path / "oplog.ndjson").read_bytes() == before


def test_runtime_lease_survives_dropped_diagnostic_handle(tmp_path: Path) -> None:
    authority, _, _, log, controller, _, _ = _runtime(tmp_path)
    authority_ref = weakref.ref(authority)
    del authority
    gc.collect()

    retained = authority_ref()
    assert retained is not None
    assert authority_protocol.history_authority_for_controller(controller) is retained
    with pytest.raises(authority_protocol.HistoryAuthorityError):
        log.append(_operation("blocked.after.handle.drop"))


def test_undo_receives_only_an_opaque_replay_port(tmp_path: Path) -> None:
    source = _operation("native.source")
    _, _, _, _, controller, store, coordinator = _runtime(
        tmp_path, seed=(source,),
    )

    assert "current_history_transaction" not in authority_protocol.__all__
    assert not hasattr(authority_protocol, "current_history_transaction")
    with _open_transaction(coordinator, store):
        replay_port = authority_protocol._history_replay_port(
            controller,
            _capability=authority_protocol._HISTORY_REPLAY_CONSUMER_CAPABILITY,
        )
        assert replay_port is not None
        assert not any(
            hasattr(replay_port, name)
            for name in (
                "write_scope", "prepare_store", "confirm_durable", "state",
                "authority", "expected_operations", "append_cursor",
            )
        )
        assert len(replay_port.fresh_entries) == 1


def test_bound_log_rejects_direct_and_same_path_second_object(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    authority, _owner, _document, log, _controller, _store, _coordinator = runtime
    operation = _operation("blocked.direct")
    before = (tmp_path / "oplog.ndjson").read_bytes()

    with pytest.raises(authority_protocol.HistoryAuthorityError):
        log.append(operation)
    second = OpLog(tmp_path / "oplog.ndjson")
    with pytest.raises(authority_protocol.HistoryAuthorityError):
        second.append(operation)

    assert authority.state == "ACTIVE"
    assert (tmp_path / "oplog.ndjson").read_bytes() == before


def test_same_path_alias_is_rejected_inside_legitimate_write_scope(tmp_path: Path) -> None:
    authority, _, _, log, _, store, coordinator = _runtime(tmp_path)
    alias = OpLog(tmp_path / "oplog.ndjson")
    operation = _operation("exact.writer.only")

    with _open_transaction(coordinator, store) as transaction:
        transaction.prepare_store((operation,))
        with transaction.write_scope():
            with pytest.raises(authority_protocol.HistoryAuthorityError) as caught:
                alias.append(operation)
            assert _reason(caught.value) == "history_oplog_alias"
            log.append(operation)
        transaction.confirm_durable()

    assert authority.trusted_head.count == 1


def test_exact_transaction_scope_appends_and_advances_trusted_head(tmp_path: Path) -> None:
    authority, _, _, log, _, store, coordinator = _runtime(tmp_path)
    operation = _operation("allowed.coordinator")
    original = authority.trusted_head

    with _open_transaction(coordinator, store) as transaction:
        transaction.prepare_store((operation,))
        with transaction.write_scope():
            log.append(operation)
        transaction.confirm_durable()

    assert transaction.state == "DURABLE"
    assert authority.trusted_head.count == original.count + 1
    assert authority.trusted_head.tail_hash != original.tail_hash


def test_transaction_rejects_wrong_store_and_coordinator_before_write(tmp_path: Path) -> None:
    authority, _, document, log, _, store, coordinator = _runtime(tmp_path)
    other_store = _Store(log)
    other_coordinator = _Coordinator(document, store)

    port = authority_protocol._history_coordinator_port(
        coordinator,
        _capability=authority_protocol._HISTORY_COORDINATOR_CAPABILITY,
    )
    assert port is not None
    with pytest.raises(authority_protocol.HistoryAuthorityError):
        port.open(
            coordinator=coordinator,
            store=other_store,
            _capability=authority_protocol._HISTORY_COORDINATOR_CAPABILITY,
        )
    with pytest.raises(authority_protocol.HistoryAuthorityError):
        port.open(
            coordinator=other_coordinator,
            store=store,
            _capability=authority_protocol._HISTORY_COORDINATOR_CAPABILITY,
        )


def test_retire_rejects_cross_thread_active_transaction_without_unbinding(
    tmp_path: Path,
) -> None:
    authority, _, _, log, _, store, coordinator = _runtime(tmp_path)
    operation = _operation("cross.thread.retire")
    errors: list[BaseException] = []

    with _open_transaction(coordinator, store) as transaction:
        def retire_from_other_thread() -> None:
            try:
                _retire(authority)
            except BaseException as exc:  # noqa: BLE001 - protocol result crosses thread
                errors.append(exc)

        worker = threading.Thread(target=retire_from_other_thread)
        worker.start()
        worker.join(timeout=5)
        assert not worker.is_alive()
        transaction.prepare_store((operation,))
        with transaction.write_scope():
            log.append(operation)
        transaction.confirm_durable()

    assert len(errors) == 1
    assert _reason(errors[0]) == "history_retire_active"
    assert authority.state == "ACTIVE"
    assert len(log) == 1


def test_retire_rejects_minted_transaction_before_context_enter(tmp_path: Path) -> None:
    authority, _, _, log, _, store, coordinator = _runtime(tmp_path)
    operation = _operation("minted.before.enter")
    transaction = _open_transaction(coordinator, store)

    with pytest.raises(authority_protocol.HistoryAuthorityError) as caught:
        _retire(authority)
    assert _reason(caught.value) == "history_retire_active"

    with transaction:
        transaction.prepare_store((operation,))
        with transaction.write_scope():
            log.append(operation)
        transaction.confirm_durable()
    assert authority.state == "ACTIVE"


def test_source_indexes_are_ordered_group_complete_and_claimed_once(tmp_path: Path) -> None:
    first = _operation("group.first", group_id="group-a", before="0", after="1")
    second = _operation("group.second", group_id="group-a", before="1", after="2")
    outsider = _operation("group.other", group_id="group-b", before="2", after="3")
    authority, _, _, _, controller, store, coordinator = _runtime(
        tmp_path, seed=(first, second, outsider),
    )

    with _open_transaction(coordinator, store) as transaction:
        with pytest.raises(authority_protocol.HistoryAuthorityError):
            transaction.select_history_source(
                controller=controller,
                purpose="undo",
                entry_index=0,
                entry_indexes=(0, 1),
            )
        with pytest.raises(authority_protocol.HistoryAuthorityError):
            transaction.select_history_source(
                controller=controller, purpose="undo", entry_indexes=(1, 0),
            )
        with pytest.raises(authority_protocol.HistoryAuthorityError):
            transaction.select_history_source(
                controller=controller, purpose="undo", entry_indexes=(0, 2),
            )

        ticket = transaction.select_history_source(
            controller=controller, purpose="undo", entry_indexes=(0, 1),
        )
        assert len(ticket.source_entries) == 2
        with pytest.raises(authority_protocol.HistoryAuthorityError):
            transaction.select_history_source(
                controller=controller, purpose="undo", entry_index=0,
            )


def test_one_replay_operation_cannot_satisfy_two_source_tickets(tmp_path: Path) -> None:
    first = _operation("source.first", before="0", after="1")
    second = _operation("source.second", before="0", after="1")
    _, _, _, _, controller, store, coordinator = _runtime(
        tmp_path, seed=(first, second),
    )
    replay = _rollback_for("history.undo", (first,))

    with _open_transaction(coordinator, store) as transaction:
        first_ticket = transaction.select_history_source(
            controller=controller, purpose="undo", entry_index=0,
        )
        second_ticket = transaction.select_history_source(
            controller=controller, purpose="undo", entry_index=1,
        )
        transaction.bind_replay(first_ticket, replay)
        with pytest.raises(authority_protocol.HistoryAuthorityError) as caught:
            transaction.bind_replay(second_ticket, replay)
        assert _reason(caught.value) == "history_replay_operation_claimed"


def test_source_rejects_wrong_purpose_and_non_derived_replay(tmp_path: Path) -> None:
    source = _operation("native.source", group_id="group-a", before="0", after="1")
    authority, _, _, _, controller, store, coordinator = _runtime(
        tmp_path, seed=(source,),
    )

    with _open_transaction(coordinator, store) as transaction:
        with pytest.raises((ValueError, authority_protocol.HistoryAuthorityError)):
            transaction.select_history_source(
                controller=controller, purpose="erase", entry_index=0,
            )
        ticket = transaction.select_history_source(
            controller=controller, purpose="undo", entry_index=0,
        )
        forged = _operation(
            "history.undo", kind="rollback", before="unrelated", after="forged",
        )
        with pytest.raises(authority_protocol.HistoryAuthorityError):
            transaction.bind_replay(ticket, forged)


def test_ticket_private_state_cannot_self_promote(tmp_path: Path) -> None:
    source = _operation("native.source", group_id="group-a")
    authority, _, _, _, controller, store, coordinator = _runtime(
        tmp_path, seed=(source,),
    )
    replay = _rollback_for("history.undo", (source,))

    with _open_transaction(coordinator, store) as transaction:
        ticket = transaction.select_history_source(
            controller=controller, purpose="undo", entry_index=0,
        )
        transaction.bind_replay(ticket, replay)
        try:
            record = object.__getattribute__(ticket, "_record")
            record.state = "REPLAYED"
        except (AttributeError, TypeError):
            return
        with pytest.raises(authority_protocol.HistoryAuthorityError):
            transaction.prepare_store((replay,))


def test_active_transaction_and_ticket_are_thread_bound(tmp_path: Path) -> None:
    source = _operation("native.source")
    authority, _, _, _, controller, store, coordinator = _runtime(
        tmp_path, seed=(source,),
    )
    errors: list[BaseException] = []

    with _open_transaction(coordinator, store) as transaction:
        def use_from_other_thread() -> None:
            try:
                transaction.select_history_source(
                    controller=controller, purpose="undo", entry_index=0,
                )
            except BaseException as exc:  # noqa: BLE001 - protocol result crosses thread
                errors.append(exc)

        worker = threading.Thread(target=use_from_other_thread)
        worker.start()
        worker.join(timeout=5)
        assert not worker.is_alive()

    assert len(errors) == 1
    assert isinstance(errors[0], authority_protocol.HistoryAuthorityError)


def test_durable_ticket_confirmation_is_thread_bound_and_one_shot(
    tmp_path: Path,
) -> None:
    source = _operation("native.source")
    authority, _, document, log, controller, store, coordinator = _runtime(
        tmp_path, seed=(source,),
    )
    replay = _rollback_for("history.undo", (source,))

    with _open_transaction(coordinator, store) as transaction:
        ticket = transaction.select_history_source(
            controller=controller, purpose="undo", entry_index=0,
        )
        transaction.bind_replay(ticket, replay)
        authority_protocol.mark_history_replay_ticket(
            ticket,
            document=document,
            operation=replay,
            controller=controller,
        )
        transaction.prepare_store((replay,))
        with transaction.write_scope():
            log.append(replay)
        transaction.confirm_durable()

    errors: list[BaseException] = []

    def confirm_from_other_thread() -> None:
        try:
            authority_protocol.confirm_history_source_ticket(
                ticket, controller=controller, operation=replay,
            )
        except BaseException as exc:  # noqa: BLE001 - protocol result crosses thread
            errors.append(exc)

    worker = threading.Thread(target=confirm_from_other_thread)
    worker.start()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert len(errors) == 1
    assert _reason(errors[0]) == "history_ticket_owner_mismatch"

    authority_protocol.confirm_history_source_ticket(
        ticket, controller=controller, operation=replay,
    )
    with pytest.raises(authority_protocol.HistoryAuthorityError):
        authority_protocol.confirm_history_source_ticket(
            ticket, controller=controller, operation=replay,
        )
    assert authority.state == "ACTIVE"


def test_durable_ticket_confirmation_rejects_pid_change(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = _operation("native.source")
    _, _, document, log, controller, store, coordinator = _runtime(
        tmp_path, seed=(source,),
    )
    replay = _rollback_for("history.undo", (source,))
    with _open_transaction(coordinator, store) as transaction:
        ticket = transaction.select_history_source(
            controller=controller, purpose="undo", entry_index=0,
        )
        transaction.bind_replay(ticket, replay)
        authority_protocol.mark_history_replay_ticket(
            ticket,
            document=document,
            operation=replay,
            controller=controller,
        )
        transaction.prepare_store((replay,))
        with transaction.write_scope():
            log.append(replay)
        transaction.confirm_durable()

    parent_pid = os.getpid()
    with monkeypatch.context() as scoped:
        scoped.setattr(authority_protocol.os, "getpid", lambda: parent_pid + 1)
        with pytest.raises(authority_protocol.HistoryAuthorityError) as caught:
            authority_protocol.confirm_history_source_ticket(
                ticket, controller=controller, operation=replay,
            )
        assert _reason(caught.value) == "history_authority_pid_mismatch"

    authority_protocol.confirm_history_source_ticket(
        ticket, controller=controller, operation=replay,
    )


def test_clean_append_failure_does_not_consume_intent_or_freeze(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    authority, _, _, log, _, store, coordinator = _runtime(tmp_path)
    operation = _operation("append.before.write.failure")

    with _open_transaction(coordinator, store) as transaction:
        transaction.prepare_store((operation,))
        with transaction.write_scope():
            with monkeypatch.context() as scoped:
                def fail_before_write(_body: dict[str, Any]) -> dict[str, Any]:
                    raise RuntimeError("injected before write")

                scoped.setattr(log, "_persist_line", fail_before_write)
                with pytest.raises(RuntimeError, match="injected before write"):
                    log.append(operation)
        assert transaction.append_cursor == 0

    assert transaction.state == "DISCARDED"
    assert authority.state == "ACTIVE"
    assert len(log) == 0


def test_post_publish_append_failure_freezes_without_consuming_intent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    authority, _, _, log, _, store, coordinator = _runtime(tmp_path)
    operation = _operation("append.after.publish.failure")
    persist = log._persist_line

    def fail_after_publish(body: dict[str, Any]) -> dict[str, Any]:
        persist(body)
        raise RuntimeError("injected after publish")

    monkeypatch.setattr(log, "_persist_line", fail_after_publish)
    with pytest.raises(authority_protocol.HistoryAuthorityError) as caught:
        with _open_transaction(coordinator, store) as transaction:
            transaction.prepare_store((operation,))
            with transaction.write_scope():
                log.append(operation)

    assert _reason(caught.value) == "history_append_outcome_indeterminate"
    assert transaction.append_cursor == 0
    assert transaction.state == "FROZEN"
    assert authority.state == "FROZEN"
    assert len(log) == 1


def test_post_publish_head_probe_failure_releases_permit_and_runtime_graph(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    authority, owner, document, log, controller, store, coordinator = _runtime(
        tmp_path,
    )
    operation = _operation("append.publish.probe.failure")

    with monkeypatch.context() as scoped:
        with pytest.raises(authority_protocol.HistoryAuthorityError) as caught:
            with _open_transaction(coordinator, store) as transaction:
                transaction.prepare_store((operation,))
                with transaction.write_scope():
                    def fail_post_publish_probe():
                        raise OSError("injected post-publish head probe failure")

                    scoped.setattr(
                        log,
                        "_canonical_history_head_probe",
                        fail_post_publish_probe,
                    )
                    log.append(operation)

    assert _reason(caught.value) == "history_append_outcome_indeterminate"
    assert isinstance(caught.value.__cause__, OSError)
    assert transaction.state == "FROZEN"
    assert authority.state == "FROZEN"
    assert len(log) == 1
    assert len(authority_protocol._APPEND_PERMITS) == 0
    assert transaction._append_permit is None

    # Retirement must leave no hidden registry value that keeps the exact
    # runtime graph alive after the caller releases its normal references.
    _retire(authority)
    refs = tuple(
        weakref.ref(value)
        for value in (
            authority,
            owner,
            document,
            log,
            controller,
            store,
            coordinator,
        )
    )
    del caught, authority, owner, document, log, controller, store, coordinator
    del transaction
    gc.collect()

    assert len(authority_protocol._APPEND_PERMITS) == 0
    assert all(reference() is None for reference in refs)


def test_post_publish_body_verifier_failure_is_typed_unknown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    authority, _, _, log, _, store, coordinator = _runtime(tmp_path)
    operation = _operation("append.publish.body.verifier.failure")
    real_to_dict = Operation.to_dict
    projections = 0

    def fail_only_post_publish_projection(self):
        nonlocal projections
        if self is operation:
            projections += 1
            if projections == 4:
                raise ValueError("injected post-publish body verifier failure")
        return real_to_dict(self)

    with monkeypatch.context() as scoped:
        scoped.setattr(Operation, "to_dict", fail_only_post_publish_projection)
        with pytest.raises(authority_protocol.HistoryAuthorityError) as caught:
            with _open_transaction(coordinator, store) as transaction:
                transaction.prepare_store((operation,))
                with transaction.write_scope():
                    log.append(operation)

    assert _reason(caught.value) == "history_append_outcome_indeterminate"
    assert caught.value.outcome_unknown is True
    assert isinstance(caught.value.__cause__, ValueError)
    assert "body verifier failure" in str(caught.value.__cause__)
    assert transaction.state == "FROZEN"
    assert authority.state == "FROZEN"
    assert len(log) == 1
    assert len(authority_protocol._APPEND_PERMITS) == 0


def test_authority_rejects_pid_change(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    authority, _, _, _, _, _, _ = _runtime(tmp_path)
    parent_pid = os.getpid()
    monkeypatch.setattr(authority_protocol.os, "getpid", lambda: parent_pid + 1)

    with pytest.raises(authority_protocol.HistoryAuthorityError) as caught:
        authority.fresh_head()
    assert _reason(caught.value) == "history_authority_pid_mismatch"


@pytest.mark.skipif(not hasattr(os, "fork"), reason="fork is unavailable")
def test_fork_child_loses_ticket_but_parent_keeps_it(tmp_path: Path) -> None:
    source = _operation("native.source")
    authority, _, document, _, controller, store, coordinator = _runtime(
        tmp_path, seed=(source,),
    )
    replay = _rollback_for("history.undo", (source,))

    with _open_transaction(coordinator, store) as transaction:
        ticket = transaction.select_history_source(
            controller=controller, purpose="undo", entry_index=0,
        )
        transaction.bind_replay(ticket, replay)
        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:  # pragma: no cover - asserted by parent through pipe
            os.close(read_fd)
            try:
                authority_protocol.validate_history_replay_ticket(
                    ticket,
                    document=document,
                    operation=replay,
                    controller=controller,
                )
            except BaseException:
                result = b"rejected"
            else:
                result = b"accepted"
            os.write(write_fd, result)
            os.close(write_fd)
            os._exit(0)

        os.close(write_fd)
        try:
            child_result = os.read(read_fd, 32)
        finally:
            os.close(read_fd)
            waited, status = os.waitpid(pid, 0)
        assert waited == pid and os.waitstatus_to_exitcode(status) == 0
        assert child_result == b"rejected"

        authority_protocol.validate_history_replay_ticket(
            ticket,
            document=document,
            operation=replay,
            controller=controller,
        )


@pytest.mark.skipif(not hasattr(os, "fork"), reason="fork is unavailable")
def test_fork_child_cannot_write_inherited_or_same_path_oplog(tmp_path: Path) -> None:
    _, _, _, log, _, _, _ = _runtime(tmp_path)
    path = tmp_path / "oplog.ndjson"
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - asserted by parent through pipe
        os.close(read_fd)
        results: list[str] = []
        for candidate in (log, OpLog(path)):
            try:
                candidate.append(_operation("fork.child.write"))
            except authority_protocol.HistoryAuthorityError:
                results.append("rejected")
            else:
                results.append("accepted")
        os.write(write_fd, ",".join(results).encode("ascii"))
        os.close(write_fd)
        os._exit(0)

    os.close(write_fd)
    try:
        child_result = os.read(read_fd, 64)
    finally:
        os.close(read_fd)
        waited, status = os.waitpid(pid, 0)
    assert waited == pid and os.waitstatus_to_exitcode(status) == 0
    assert child_result == b"rejected,rejected"
    assert len(log) == 0


def _external_append(path: Path, log: OpLog, operation: Operation) -> None:
    _entry, line = log._encode_entry(log._tail_hash, operation.to_dict())
    with path.open("ab") as stream:
        stream.write(line)
        stream.flush()
        os.fsync(stream.fileno())


def _external_truncate(path: Path, _log: OpLog, _operation: Operation) -> None:
    records = path.read_bytes().splitlines(keepends=True)
    assert len(records) >= 2
    with path.open("r+b") as stream:
        stream.truncate(len(records[0]))
        stream.flush()
        os.fsync(stream.fileno())


def _external_inode_replace(path: Path, _log: OpLog, operation: Operation) -> None:
    replacement_path = path.with_suffix(".replacement")
    replacement = OpLog(replacement_path)
    replacement.append(operation)
    os.replace(replacement_path, path)


@pytest.mark.parametrize(
    "mutate",
    [_external_append, _external_truncate, _external_inode_replace],
    ids=["append", "truncate", "inode-replace"],
)
def test_fresh_external_file_change_freezes_authority(
    tmp_path: Path,
    mutate: Callable[[Path, OpLog, Operation], None],
) -> None:
    first = _operation("seed.first")
    second = _operation("seed.second")
    authority, _, _, log, _, store, coordinator = _runtime(
        tmp_path, seed=(first, second),
    )
    mutate(tmp_path / "oplog.ndjson", log, _operation("external.write"))

    with pytest.raises(authority_protocol.HistoryAuthorityError):
        _open_transaction(coordinator, store)
    assert authority.state == "FROZEN"


def test_frozen_authority_is_terminal_until_exact_retire(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    authority, _owner, _document, log, _controller, store, coordinator = runtime
    _external_append(
        tmp_path / "oplog.ndjson", log, _operation("external.freeze"),
    )
    with pytest.raises(authority_protocol.HistoryAuthorityError):
        _open_transaction(coordinator, store)

    assert authority.state == "FROZEN"
    with pytest.raises(authority_protocol.HistoryAuthorityError):
        _open_transaction(coordinator, store)
    with pytest.raises(authority_protocol.HistoryAuthorityError):
        log.append(_operation("frozen.direct"))


def test_binding_uses_weak_exact_objects_and_does_not_mutate_public_shapes(
    tmp_path: Path,
) -> None:
    operation_fields = tuple(field.name for field in dataclasses.fields(Operation))
    expected_fields = (
        "id", "command_id", "source", "actor", "ts", "patches",
        "reverse_patches", "group_id", "trace_id", "kind", "error",
        "document_id", "session_id", "external", "pipeline",
    )
    owner = _Owner()
    authority, _, document, log, controller, store, coordinator = _runtime(
        tmp_path, owner=owner,
    )
    public_before = {
        "document": set(vars(document)),
        "controller": set(vars(controller)),
        "store": set(vars(store)),
        "coordinator": set(vars(coordinator)),
    }
    assert operation_fields == expected_fields
    assert "history_authority" not in _operation("shape").to_dict()
    assert public_before == {
        "document": set(vars(document)),
        "controller": set(vars(controller)),
        "store": set(vars(store)),
        "coordinator": set(vars(coordinator)),
    }

    refs = tuple(weakref.ref(value) for value in (document, log, controller, store, coordinator))
    del document, log, controller, store, coordinator
    gc.collect()
    assert all(reference() is None for reference in refs), (
        "HistoryAuthority must bind runtime objects by exact weak identity, not own them"
    )
    assert authority.state == "FROZEN"


def test_terminal_ticket_does_not_keep_runtime_graph_alive(tmp_path: Path) -> None:
    source = _operation("gc.source")
    authority, owner, document, log, controller, store, coordinator = _runtime(
        tmp_path, seed=(source,),
    )
    del authority, owner
    with _open_transaction(coordinator, store) as transaction:
        ticket = transaction.select_history_source(
            controller=controller, purpose="undo", entry_index=0,
        )

    authority_protocol.discard_history_source_ticket(ticket, controller=controller)
    ticket_ref = weakref.ref(ticket)
    refs = tuple(
        weakref.ref(value)
        for value in (document, log, controller, store, coordinator)
    )
    del ticket, transaction, document, log, controller, store, coordinator
    gc.collect()

    assert ticket_ref() is None
    assert all(reference() is None for reference in refs)


@pytest.mark.parametrize("entry_count", (2_000, 50_000))
def test_persistent_durable_confirmation_never_rescans_history_prefix(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    entry_count: int,
) -> None:
    _write_large_history(tmp_path / "oplog.ndjson", entry_count)
    authority, _, _, log, _, store, coordinator = _runtime(tmp_path)
    scan_calls = 0
    original_scan = log._scan_and_repair_locked

    def counted_scan(stream):
        nonlocal scan_calls
        scan_calls += 1
        return original_scan(stream)

    monkeypatch.setattr(log, "_scan_and_repair_locked", counted_scan)
    operation = _operation(f"performance.append.{entry_count}")

    with _open_transaction(coordinator, store) as transaction:
        transaction.prepare_store((operation,))
        with transaction.write_scope():
            log.append(operation)
        transaction.confirm_durable()

    assert scan_calls == 0
    assert transaction.state == "DURABLE"
    assert authority.trusted_head.count == entry_count + 1


def test_external_stat_drift_after_append_freezes_as_unknown(
    tmp_path: Path,
) -> None:
    authority, _, _, log, _, store, coordinator = _runtime(tmp_path)
    operation = _operation("durable.external-stat-drift")
    path = tmp_path / "oplog.ndjson"

    with _open_transaction(coordinator, store) as transaction:
        transaction.prepare_store((operation,))
        with transaction.write_scope():
            log.append(operation)
        stat_before = path.stat()
        os.utime(
            path,
            ns=(stat_before.st_atime_ns, stat_before.st_mtime_ns + 1_000_000),
        )

        with pytest.raises(authority_protocol.HistoryAuthorityError) as caught:
            transaction.confirm_durable()

    assert caught.value.reason == "history_suffix_verification_failed"
    assert caught.value.outcome_unknown is True
    assert caught.value.durability_phase == "post_write_verification"
    assert transaction.state == "FROZEN"
    assert authority.state == "FROZEN"
    assert len(log) == 1


@pytest.mark.parametrize("failure_site", ("suffix_probe", "body_verifier"))
def test_post_append_suffix_verifier_failure_is_typed_unknown_and_not_masked(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure_site: str,
) -> None:
    authority, _, _, log, _, store, coordinator = _runtime(tmp_path)
    operation = _operation(f"durable.{failure_site}")
    injected_type: type[BaseException]

    with _open_transaction(coordinator, store) as transaction:
        transaction.prepare_store((operation,))
        with transaction.write_scope():
            log.append(operation)
        if failure_site == "suffix_probe":
            injected_type = OSError

            def fail_suffix(_start_count: int):
                raise OSError("injected suffix probe failure")

            monkeypatch.setattr(
                log, "_canonical_history_suffix_snapshot", fail_suffix,
            )
        else:
            injected_type = ValueError

            def fail_body(_value: Any):
                raise ValueError("injected suffix body failure")

            monkeypatch.setattr(authority_protocol, "_canonical_body", fail_body)

        with pytest.raises(authority_protocol.HistoryAuthorityError) as caught:
            transaction.confirm_durable()

    assert caught.value.reason == "history_suffix_verification_failed"
    assert caught.value.outcome_unknown is True
    assert caught.value.durability_phase == "post_write_verification"
    assert isinstance(caught.value.__cause__, injected_type)
    assert transaction.state == "FROZEN"
    assert authority.state == "FROZEN"
    assert len(authority_protocol._APPEND_PERMITS) == 0


def test_history_snapshot_ports_have_bounded_call_counts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    authority, _, _, log, controller, store, coordinator = _runtime(tmp_path)
    del authority
    counts = {"head": 0, "rows": 0, "suffix": 0}
    head_probe = log._canonical_history_head_probe
    rows_snapshot = log._canonical_history_rows_snapshot
    suffix_snapshot = log._canonical_history_suffix_snapshot

    def counted_head():
        counts["head"] += 1
        return head_probe()

    def counted_rows():
        counts["rows"] += 1
        return rows_snapshot()

    def counted_suffix(start_count: int):
        counts["suffix"] += 1
        return suffix_snapshot(start_count)

    monkeypatch.setattr(log, "_canonical_history_head_probe", counted_head)
    monkeypatch.setattr(log, "_canonical_history_rows_snapshot", counted_rows)
    monkeypatch.setattr(log, "_canonical_history_suffix_snapshot", counted_suffix)
    operation = _operation("performance.normal")
    with _open_transaction(coordinator, store) as transaction:
        transaction.prepare_store((operation,))
        with transaction.write_scope():
            log.append(operation)
        transaction.confirm_durable()
    assert counts == {"head": 3, "rows": 0, "suffix": 1}

    counts.update(head=0, rows=0, suffix=0)
    with _open_transaction(coordinator, store) as transaction:
        assert len(transaction.fresh_entries) == 1
        assert len(transaction.fresh_entries) == 1
    assert counts == {"head": 2, "rows": 1, "suffix": 0}

    unbound = OpLog(tmp_path / "unbound.ndjson")
    monkeypatch.setattr(
        unbound,
        "_canonical_history_rows_snapshot",
        lambda: pytest.fail("unbound append scanned history authority rows"),
    )
    monkeypatch.setattr(
        unbound,
        "_canonical_history_head_probe",
        lambda: pytest.fail("unbound append probed history authority head"),
    )
    unbound.append(_operation("performance.unbound"))
