from __future__ import annotations

from contextlib import nullcontext
import threading

import pytest

import commit_coordinator as commit_coordinator_module
from commit_coordinator import CommitCoordinator
from command import CommandMutation, DeferredAction
from commit_contracts import (
    CommitFailure,
    CommitReceipt,
    CommitStage,
    CommitStatus,
    DurabilityFacts,
    DurabilityState,
    EventStatus,
)
from entity import ZeError
from history_authority import HistoryAuthorityError
from operation import new_operation, new_pending_confirmation
from ports import (
    ActionStatus,
    EventDeliveryError,
    MutationBatch,
    MutationRollbackError,
    NestedCommitError,
)


class FakeDocument:
    def __init__(self) -> None:
        self.id = "doc-1"
        self.version = 1
        self.effective_save_policy = "auto_commit"

    def find(self, entity_type, entity_id):
        return None


class FakeMutationSession:
    def __init__(self, document: "SessionDocument") -> None:
        self.document = document
        self.snapshot = dict(document.state)
        self.version = document.version
        self.closed = False

    def commit(self) -> None:
        assert self.closed is False
        self.document.session_events.append("commit")
        self.closed = True

    def rollback(self) -> None:
        assert self.closed is False
        self.document.session_events.append("rollback")
        if self.document.rollback_error is not None:
            raise self.document.rollback_error
        # Restore in place: references held by editors keep their identity.
        self.document.state.clear()
        self.document.state.update(self.snapshot)
        self.document.version = self.version
        self.closed = True


class SessionDocument(FakeDocument):
    def __init__(self, *, rollback_error: Exception | None = None) -> None:
        super().__init__()
        self.state = {"value": 0, "nested": {"keep": True}}
        self.rollback_error = rollback_error
        self.session_events: list[str] = []

    def begin_mutation_session(self):
        self.session_events.append("begin")
        return FakeMutationSession(self)


class TrackingLock:
    def __init__(self, log: list[str] | None = None) -> None:
        self._lock = threading.RLock()
        self._local = threading.local()
        self.log = log
        self.attempted_by_b = threading.Event()

    @property
    def active(self) -> bool:
        return bool(getattr(self._local, "depth", 0))

    def __enter__(self):
        if threading.current_thread().name == "commit-b":
            self.attempted_by_b.set()
        self._lock.acquire()
        self._local.depth = getattr(self._local, "depth", 0) + 1
        if self.log is not None:
            self.log.append("lock-enter")
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.log is not None:
            self.log.append("lock-exit")
        self._local.depth -= 1
        self._lock.release()


class DurableStore:
    def __init__(self, document, log: list[str] | None = None, lock=None) -> None:
        self.document = document
        self.log = log
        self.lock = lock
        self.calls = 0

    def commit(self, intent):
        if self.lock is not None:
            assert self.lock.active
        if self.log is not None:
            self.log.append("store")
        self.calls += 1
        self.document.version += 1
        return CommitReceipt.committed(
            commit_id=intent.commit_id,
            document_id=intent.document_id,
            operation_ids=intent.operation_ids,
            trace_id=intent.trace_id,
            save_policy=intent.save_policy,
            document_version=self.document.version,
        )


class IndeterminateStore:
    def __init__(self) -> None:
        self.calls = 0

    def commit(self, intent):
        self.calls += 1
        failure = CommitFailure(
            stage=CommitStage.OPLOG,
            code="oplog_failed",
            message="document durable, oplog not durable",
            facts=DurabilityFacts(
                mutation_applied=True,
                document=DurabilityState.DURABLE,
                oplog=DurabilityState.NOT_DURABLE,
            ),
        )
        return CommitReceipt.from_failure(
            commit_id=intent.commit_id,
            document_id=intent.document_id,
            operation_ids=intent.operation_ids,
            trace_id=intent.trace_id,
            save_policy=intent.save_policy,
            failure=failure,
            document_version=2,
        )


class NonDurableStore:
    """Store proves neither canonical resource was touched."""

    def __init__(self) -> None:
        self.calls = 0
        self.last_receipt = None

    def commit(self, intent):
        self.calls += 1
        failure = CommitFailure(
            stage=CommitStage.DOCUMENT,
            code="document_write_rejected",
            message="document write was rejected before persistence",
            facts=DurabilityFacts(
                mutation_applied=True,
                document=DurabilityState.NOT_DURABLE,
                oplog=DurabilityState.NOT_DURABLE,
            ),
        )
        self.last_receipt = CommitReceipt.from_failure(
            commit_id=intent.commit_id,
            document_id=intent.document_id,
            operation_ids=intent.operation_ids,
            trace_id=intent.trace_id,
            save_policy=intent.save_policy,
            failure=failure,
        )
        return self.last_receipt


class RecordingFactory:
    def __init__(self, log: list[str] | None = None, lock=None) -> None:
        self.log = log
        self.lock = lock

    def build(self, operation, document, receipt):
        if self.lock is not None:
            assert self.lock.active
        if self.log is not None:
            self.log.append(f"build:{operation.command_id}")
        return {
            "type": "entity_change",
            "doc_id": document.id,
            "command_id": operation.command_id,
            "commit_id": receipt.commit_id,
            "document_version": receipt.document_version,
            "changes": [],
        }


class RecordingPublisher:
    def __init__(self, statuses=None, log: list[str] | None = None, lock=None) -> None:
        self.statuses = list(statuses or [])
        self.log = log
        self.lock = lock
        self.payloads = []

    def publish(self, payload):
        if self.lock is not None:
            assert self.lock.active
        self.payloads.append(dict(payload))
        if self.log is not None:
            self.log.append(f"publish:{payload['command_id']}")
        if self.statuses:
            status = self.statuses.pop(0)
            if isinstance(status, Exception):
                raise status
            return status
        return EventStatus.PUBLISHED


def _operation(label: str = "one"):
    return new_operation(
        command_id=f"test.{label}",
        source="user",
        actor={"user_id": "u1"},
        patches=[],
        reverse_patches=[],
        document_id="doc-1",
        trace_id=f"trace-{label}",
    )


def test_noop_and_pending_confirmation_never_touch_store_or_events():
    document = FakeDocument()
    store = DurableStore(document)
    publisher = RecordingPublisher()
    coordinator = CommitCoordinator(document=document, store=store, publisher=publisher)

    noop = coordinator.execute(lambda: None)
    pending_value = new_pending_confirmation(
        command_id="dangerous.delete",
        args={"id": "x"},
        source="user",
        actor={"user_id": "u1"},
    )
    pending = coordinator.execute(lambda: pending_value)

    assert noop.is_noop is True
    assert noop.receipt is None
    assert noop.events == ()
    assert pending.pending_confirmation is pending_value
    assert pending.receipt is None
    assert pending.events == ()
    assert store.calls == 0
    assert publisher.payloads == []


def test_session_rolls_back_accidental_noop_and_pending_mutations():
    document = SessionDocument()
    state_identity = document.state
    store = DurableStore(document)
    coordinator = CommitCoordinator(document, store)

    def accidental_noop():
        document.state["value"] = 41
        return None

    noop = coordinator.execute(accidental_noop)
    assert noop.is_noop is True
    assert document.state is state_identity
    assert document.state["value"] == 0

    pending_value = new_pending_confirmation(
        command_id="dangerous.delete",
        args={"id": "x"},
        source="user",
        actor={"user_id": "u1"},
    )

    def accidental_pending():
        document.state["value"] = 42
        return pending_value

    pending = coordinator.execute(accidental_pending)
    assert pending.pending_confirmation is pending_value
    assert document.state is state_identity
    assert document.state["value"] == 0
    assert document.session_events == ["begin", "rollback", "begin", "rollback"]
    assert store.calls == 0


def test_producer_exception_restores_object_state_and_is_rethrown_unchanged():
    document = SessionDocument()
    state_identity = document.state
    producer_error = ValueError("invalid command arguments")
    store = DurableStore(document)

    def producer():
        document.state["value"] = 99
        document.version = 8
        raise producer_error

    with pytest.raises(ValueError) as caught:
        CommitCoordinator(document, store).execute(producer)

    assert caught.value is producer_error
    assert document.state is state_identity
    assert document.state["value"] == 0
    assert document.version == 1
    assert document.session_events == ["begin", "rollback"]
    assert store.calls == 0


def test_base_exception_restores_session_and_does_not_poison_next_commit():
    document = SessionDocument()
    store = DurableStore(document)
    coordinator = CommitCoordinator(document, store)

    def interrupted_producer():
        document.state["value"] = 99
        raise KeyboardInterrupt("cancelled by operator")

    with pytest.raises(KeyboardInterrupt, match="cancelled by operator"):
        coordinator.execute(interrupted_producer)

    assert document.state["value"] == 0
    assert document.session_events == ["begin", "rollback"]
    assert store.calls == 0

    execution = coordinator.execute(lambda: _operation())
    assert execution.is_committed is True
    assert document.session_events == ["begin", "rollback", "begin", "commit"]
    assert store.calls == 1


def test_invalid_producer_type_is_rolled_back_before_type_error_escapes():
    document = SessionDocument()
    store = DurableStore(document)

    def producer():
        document.state["value"] = 7
        return {"not": "a mutation"}

    with pytest.raises(TypeError, match="producer must return"):
        CommitCoordinator(document, store).execute(producer)

    assert document.state["value"] == 0
    assert document.session_events == ["begin", "rollback"]
    assert store.calls == 0


def test_rollback_failure_is_stable_ze_error_with_producer_as_cause():
    rollback_error = RuntimeError("snapshot restore failed")
    producer_error = ValueError("producer exploded")
    document = SessionDocument(rollback_error=rollback_error)

    def producer():
        document.state["value"] = 9
        raise producer_error

    with pytest.raises(MutationRollbackError) as caught:
        CommitCoordinator(document, DurableStore(document)).execute(producer)

    assert caught.value.code == "mutation_rollback_failed"
    assert isinstance(caught.value, ZeError)
    assert caught.value.original_error is producer_error
    assert caught.value.rollback_error is rollback_error
    assert caught.value.__cause__ is producer_error
    assert caught.value.context["rollback_error_type"] == "RuntimeError"
    assert document.state["value"] == 9


def test_proven_non_durable_store_failure_rolls_back_and_becomes_aborted():
    document = SessionDocument()
    state_identity = document.state
    store = NonDurableStore()

    def producer():
        document.state["value"] = 5
        document.version = 3
        return _operation()

    execution = CommitCoordinator(document, store).execute(producer)

    assert document.state is state_identity
    assert document.state["value"] == 0
    assert document.version == 1
    assert document.session_events == ["begin", "rollback"]
    assert execution.receipt is not None
    assert execution.receipt.status is CommitStatus.ABORTED
    assert execution.receipt.retry_safe is True
    assert execution.receipt.document_version is None
    assert execution.receipt.failure is not None
    assert execution.receipt.failure.code == "document_write_rejected"
    assert execution.receipt.failure.facts.rollback_succeeded is True
    assert execution.receipt.failure.facts.document is DurabilityState.NOT_DURABLE
    assert execution.receipt.failure.facts.oplog is DurabilityState.NOT_DURABLE
    assert execution.events == ()


def test_document_without_session_keeps_w03_non_durable_compatibility():
    document = FakeDocument()
    document.state = {"value": 0}

    def producer():
        document.state["value"] = 5
        return _operation()

    execution = CommitCoordinator(document, NonDurableStore()).execute(producer)

    # Fake/legacy documents have no rollback port, so the coordinator neither
    # invents a successful rollback fact nor breaks their previous behavior.
    assert document.state["value"] == 5
    assert execution.receipt is not None
    assert execution.receipt.status is CommitStatus.DIRTY
    assert execution.receipt.retry_safe is False
    assert execution.receipt.failure is not None
    assert execution.receipt.failure.facts.rollback_succeeded is False


@pytest.mark.parametrize("reported_facts", ["already_rolled_back", "no_mutation"])
def test_double_not_durable_always_restores_coordinator_session(reported_facts):
    document = SessionDocument()

    class PreclassifiedStore:
        def commit(self, intent):
            if reported_facts == "already_rolled_back":
                stage = CommitStage.APPLY
                facts = DurabilityFacts(
                    mutation_applied=True,
                    rollback_succeeded=True,
                )
            else:
                stage = CommitStage.VALIDATE
                facts = DurabilityFacts()
            failure = CommitFailure(
                stage=stage,
                code="preclassified_abort",
                message="store reports no durable resource",
                facts=facts,
            )
            return CommitReceipt.from_failure(
                commit_id=intent.commit_id,
                document_id=intent.document_id,
                operation_ids=intent.operation_ids,
                trace_id=intent.trace_id,
                save_policy=intent.save_policy,
                failure=failure,
            )

    def producer():
        document.state["value"] = 33
        return _operation()

    execution = CommitCoordinator(document, PreclassifiedStore()).execute(producer)

    assert document.state["value"] == 0
    assert document.session_events == ["begin", "rollback"]
    assert execution.receipt is not None
    assert execution.receipt.status is CommitStatus.ABORTED


def test_non_durable_store_rollback_failure_exposes_original_receipt():
    rollback_error = RuntimeError("restore unavailable")
    document = SessionDocument(rollback_error=rollback_error)
    store = NonDurableStore()

    with pytest.raises(MutationRollbackError) as caught:
        CommitCoordinator(document, store).execute(lambda: _operation())

    assert caught.value.code == "mutation_rollback_failed"
    assert caught.value.receipt is store.last_receipt
    assert caught.value.original_error is None
    assert caught.value.__cause__ is rollback_error


@pytest.mark.parametrize("store_kind", ["unknown", "document_durable"])
def test_unknown_or_durable_storage_facts_never_restore_the_old_snapshot(store_kind):
    document = SessionDocument()

    class UnknownStore:
        def commit(self, intent):
            raise OSError("write acknowledgement lost")

    store = UnknownStore() if store_kind == "unknown" else IndeterminateStore()

    def producer():
        document.state["value"] = 12
        return _operation()

    execution = CommitCoordinator(document, store).execute(producer)

    assert document.state["value"] == 12
    assert document.session_events == ["begin", "commit"]
    assert execution.receipt is not None
    assert execution.receipt.status is CommitStatus.INDETERMINATE
    assert execution.receipt.retry_safe is False


def test_event_failure_happens_after_session_commit_and_never_rolls_back():
    document = SessionDocument()
    observed = []

    class FailingPublisher:
        def publish(self, payload):
            observed.extend(document.session_events)
            raise RuntimeError("broker unavailable")

    def producer():
        document.state["value"] = 21
        return _operation()

    execution = CommitCoordinator(
        document,
        DurableStore(document),
        publisher=FailingPublisher(),
        event_factory=RecordingFactory(),
    ).execute(producer)

    assert observed == ["begin", "commit"]
    assert document.state["value"] == 21
    assert document.session_events == ["begin", "commit"]
    assert execution.receipt is not None
    assert execution.receipt.status is CommitStatus.COMMITTED
    assert execution.receipt.event_status is EventStatus.FAILED


def test_event_build_failure_also_never_restores_a_durable_mutation():
    document = SessionDocument()

    class FailingFactory:
        def build(self, operation, current_document, receipt):
            assert current_document.session_events == ["begin", "commit"]
            raise RuntimeError("payload derivation failed")

    def producer():
        document.state["value"] = 22
        return _operation()

    execution = CommitCoordinator(
        document,
        DurableStore(document),
        event_factory=FailingFactory(),
    ).execute(producer)

    assert document.state["value"] == 22
    assert document.session_events == ["begin", "commit"]
    assert execution.receipt is not None
    assert execution.receipt.status is CommitStatus.COMMITTED
    assert execution.receipt.event_status is EventStatus.FAILED
    assert execution.receipt.failure is not None
    assert execution.receipt.failure.code == "event_build_failed"


def test_commit_build_and_publish_all_happen_in_the_shared_lock():
    log: list[str] = []
    document = FakeDocument()
    lock = TrackingLock(log)
    store = DurableStore(document, log, lock)
    factory = RecordingFactory(log, lock)
    publisher = RecordingPublisher(log=log, lock=lock)
    coordinator = CommitCoordinator(
        document=document,
        store=store,
        publisher=publisher,
        lock=lock,
        event_factory=factory,
    )

    def produce():
        assert lock.active
        log.append("producer")
        return _operation()

    execution = coordinator.execute(produce)

    assert execution.receipt is not None
    assert execution.receipt.status is CommitStatus.COMMITTED
    assert execution.receipt.event_status is EventStatus.PUBLISHED
    assert log == [
        "lock-enter",
        "producer",
        "store",
        "build:test.one",
        "publish:test.one",
        "lock-exit",
    ]
    assert len(execution.events) == 1
    assert execution.events[0] == publisher.payloads[0]


def test_indeterminate_store_result_has_no_event_and_is_never_published():
    document = FakeDocument()
    store = IndeterminateStore()
    publisher = RecordingPublisher()

    execution = CommitCoordinator(
        document=document,
        store=store,
        publisher=publisher,
        event_factory=RecordingFactory(),
    ).execute(lambda: _operation())

    assert execution.receipt is not None
    assert execution.receipt.status is CommitStatus.INDETERMINATE
    assert execution.events == ()
    assert publisher.payloads == []


def test_history_freeze_preserves_indeterminate_commit_receipt_facts(monkeypatch):
    document = SessionDocument()

    class HistoryTransaction:
        state = "OPEN"

        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _tb):
            return False

        def prepare_store(self, _operations):
            self.state = "PREPARED"

        def write_scope(self):
            return self

        def freeze(self, reason, message):
            self.state = "FROZEN"
            raise HistoryAuthorityError(reason, message)

    transaction = HistoryTransaction()

    class HistoryPort:
        def open(self, **_kwargs):
            return transaction

    monkeypatch.setattr(
        commit_coordinator_module,
        "_history_coordinator_port",
        lambda *_args, **_kwargs: HistoryPort(),
    )

    def producer():
        document.state["value"] = 12
        return _operation()

    with pytest.raises(HistoryAuthorityError) as caught:
        CommitCoordinator(document, IndeterminateStore()).execute(producer)

    error = caught.value
    assert error.reason == "history_commit_outcome_indeterminate"
    assert error.outcome_unknown is True
    assert error.durability_phase == "commit_outcome"
    assert error.commit_facts == {
        "phase": "oplog",
        "outcome": "indeterminate",
        "commit_id": error.commit_facts["commit_id"],
        "retry_safe": False,
        "document_durable": True,
        "oplog_durable": False,
        "failure_facts": {
            "mutation_applied": True,
            "rollback_succeeded": False,
            "document": "durable",
            "oplog": "not_durable",
        },
    }
    assert len(error.commit_facts["commit_id"]) == 32
    assert error.context["commit_facts"] == error.commit_facts
    assert document.state["value"] == 12
    assert document.session_events == ["begin", "commit"]


def test_unexpected_history_freeze_failure_is_typed_replay_unsafe(monkeypatch):
    document = SessionDocument()
    freeze_error = RuntimeError("repair fence interrupted")

    class HistoryTransaction:
        state = "OPEN"

        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _tb):
            return False

        def prepare_store(self, _operations):
            self.state = "PREPARED"

        def write_scope(self):
            return self

        def freeze(self, _reason, _message):
            raise freeze_error

    class HistoryPort:
        def open(self, **_kwargs):
            return HistoryTransaction()

    monkeypatch.setattr(
        commit_coordinator_module,
        "_history_coordinator_port",
        lambda *_args, **_kwargs: HistoryPort(),
    )

    with pytest.raises(HistoryAuthorityError) as caught:
        CommitCoordinator(document, IndeterminateStore()).execute(
            lambda: _operation(),
        )

    error = caught.value
    assert error.reason == "history_commit_freeze_failed"
    assert error.outcome_unknown is True
    assert error.commit_facts["outcome"] == "indeterminate"
    assert error.commit_facts["document_durable"] is True
    assert error.commit_facts["oplog_durable"] is False
    assert error.__cause__ is freeze_error
    assert document.session_events == ["begin", "commit"]


def test_postwrite_history_verifier_unknown_closes_session_and_keeps_receipt(
    monkeypatch,
):
    document = SessionDocument()

    class HistoryTransaction:
        state = "OPEN"

        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _tb):
            return False

        def prepare_store(self, _operations):
            self.state = "PREPARED"

        def write_scope(self):
            return self

        def confirm_durable(self):
            self.state = "FROZEN"
            raise HistoryAuthorityError(
                "history_suffix_verification_failed",
                "injected postwrite verifier failure",
            )

    transaction = HistoryTransaction()

    class HistoryPort:
        def open(self, **_kwargs):
            return transaction

    monkeypatch.setattr(
        commit_coordinator_module,
        "_history_coordinator_port",
        lambda *_args, **_kwargs: HistoryPort(),
    )

    def producer():
        document.state["value"] = 17
        return _operation()

    with pytest.raises(HistoryAuthorityError) as caught:
        CommitCoordinator(document, DurableStore(document)).execute(producer)

    error = caught.value
    assert error.reason == "history_suffix_verification_failed"
    assert error.outcome_unknown is True
    assert error.durability_phase == "post_write_verification"
    assert error.commit_facts == {
        "phase": "oplog",
        "outcome": "committed",
        "commit_id": error.commit_facts["commit_id"],
        "retry_safe": False,
        "document_durable": True,
        "oplog_durable": True,
    }
    assert len(error.commit_facts["commit_id"]) == 32
    assert document.state["value"] == 17
    assert document.session_events == ["begin", "commit"]


def test_unexpected_postwrite_history_verifier_error_is_typed_replay_unsafe(
    monkeypatch,
):
    document = SessionDocument()
    verifier_error = RuntimeError("verifier implementation interrupted")

    class HistoryTransaction:
        state = "OPEN"

        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _tb):
            return False

        def prepare_store(self, _operations):
            self.state = "PREPARED"

        def write_scope(self):
            return self

        def confirm_durable(self):
            raise verifier_error

    class HistoryPort:
        def open(self, **_kwargs):
            return HistoryTransaction()

    monkeypatch.setattr(
        commit_coordinator_module,
        "_history_coordinator_port",
        lambda *_args, **_kwargs: HistoryPort(),
    )

    def producer():
        document.state["value"] = 18
        return _operation()

    with pytest.raises(HistoryAuthorityError) as caught:
        CommitCoordinator(document, DurableStore(document)).execute(producer)

    error = caught.value
    assert error.reason == "history_durable_verification_interrupted"
    assert error.outcome_unknown is True
    assert error.durability_phase == "post_write_verification"
    assert error.commit_facts["outcome"] == "committed"
    assert error.commit_facts["document_durable"] is True
    assert error.commit_facts["oplog_durable"] is True
    assert error.__cause__ is verifier_error
    assert document.state["value"] == 18
    assert document.session_events == ["begin", "commit"]


def test_write_scope_exit_after_durable_store_is_typed_replay_unsafe(monkeypatch):
    document = SessionDocument()
    scope_error = RuntimeError("write scope exit interrupted")

    class WriteScope:
        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _tb):
            raise scope_error

    class HistoryTransaction:
        state = "OPEN"

        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _tb):
            return False

        def prepare_store(self, _operations):
            self.state = "PREPARED"

        def write_scope(self):
            return WriteScope()

    class HistoryPort:
        def open(self, **_kwargs):
            return HistoryTransaction()

    monkeypatch.setattr(
        commit_coordinator_module,
        "_history_coordinator_port",
        lambda *_args, **_kwargs: HistoryPort(),
    )

    with pytest.raises(HistoryAuthorityError) as caught:
        CommitCoordinator(document, DurableStore(document)).execute(
            lambda: _operation(),
        )

    error = caught.value
    assert error.reason == "history_write_scope_finalize_failed"
    assert error.outcome_unknown is True
    assert error.commit_facts["outcome"] == "committed"
    assert error.__cause__ is scope_error
    assert document.session_events == ["begin", "commit"]


def test_outer_history_context_exit_after_commit_is_typed_replay_unsafe(monkeypatch):
    document = SessionDocument()
    context_error = RuntimeError("history context exit interrupted")

    class HistoryTransaction:
        state = "OPEN"

        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _tb):
            raise context_error

        def prepare_store(self, _operations):
            self.state = "PREPARED"

        def write_scope(self):
            return nullcontext()

        def confirm_durable(self):
            self.state = "DURABLE"

    class HistoryPort:
        def open(self, **_kwargs):
            return HistoryTransaction()

    monkeypatch.setattr(
        commit_coordinator_module,
        "_history_coordinator_port",
        lambda *_args, **_kwargs: HistoryPort(),
    )

    with pytest.raises(HistoryAuthorityError) as caught:
        CommitCoordinator(document, DurableStore(document)).execute(
            lambda: _operation(),
        )

    error = caught.value
    assert error.reason == "history_context_finalize_failed"
    assert error.outcome_unknown is True
    assert error.commit_facts["outcome"] == "committed"
    assert error.__cause__ is context_error
    assert document.session_events == ["begin", "commit"]


def test_opaque_store_exception_becomes_unknown_receipt_and_never_publishes():
    class BrokenStore:
        def commit(self, intent):
            raise OSError("connection dropped during commit")

    publisher = RecordingPublisher()
    execution = CommitCoordinator(
        document=FakeDocument(),
        store=BrokenStore(),
        publisher=publisher,
    ).execute(lambda: _operation())

    assert execution.receipt is not None
    assert execution.receipt.status is CommitStatus.INDETERMINATE
    assert execution.receipt.retry_safe is False
    assert execution.receipt.failure is not None
    assert execution.receipt.failure.facts.document is DurabilityState.UNKNOWN
    assert execution.receipt.failure.facts.oplog is DurabilityState.UNKNOWN
    assert execution.events == ()
    assert publisher.payloads == []


def test_event_failure_does_not_reverse_business_commit():
    document = FakeDocument()
    publisher = RecordingPublisher(statuses=[OSError("broker unavailable")])

    execution = CommitCoordinator(
        document=document,
        store=DurableStore(document),
        publisher=publisher,
        event_factory=RecordingFactory(),
    ).execute(lambda: _operation())

    assert execution.receipt is not None
    assert execution.receipt.status is CommitStatus.COMMITTED
    assert execution.receipt.event_status is EventStatus.FAILED
    assert execution.receipt.stage is CommitStage.EVENT
    assert execution.receipt.retry_safe is False
    assert execution.receipt.document_durable is True
    assert execution.receipt.oplog_durable is True
    assert len(execution.events) == 1


def test_batch_pending_status_accepts_every_event_before_returning_pending():
    document = FakeDocument()
    first, second = _operation("first"), _operation("second")
    publisher = RecordingPublisher(
        statuses=[EventStatus.PENDING, EventStatus.PUBLISHED]
    )

    execution = CommitCoordinator(
        document=document,
        store=DurableStore(document),
        publisher=publisher,
        event_factory=RecordingFactory(),
    ).execute(lambda: MutationBatch((first, second)))

    assert [payload["command_id"] for payload in publisher.payloads] == [
        "test.first",
        "test.second",
    ]
    assert len(execution.events) == 2
    assert execution.receipt is not None
    assert execution.receipt.status is CommitStatus.COMMITTED
    assert execution.receipt.event_status is EventStatus.PENDING
    assert execution.receipt.operation_ids == (first.id, second.id)


def test_pending_delivery_exception_also_continues_through_batch():
    document = FakeDocument()
    publisher = RecordingPublisher(
        statuses=[
            EventDeliveryError(
                "accepted by outbox, live delivery deferred",
                status=EventStatus.PENDING,
            ),
            EventStatus.PUBLISHED,
        ]
    )

    execution = CommitCoordinator(
        document=document,
        store=DurableStore(document),
        publisher=publisher,
        event_factory=RecordingFactory(),
    ).execute(lambda: MutationBatch((_operation("first"), _operation("second"))))

    assert len(publisher.payloads) == 2
    assert execution.receipt is not None
    assert execution.receipt.event_status is EventStatus.PENDING


def test_canonical_events_are_built_once_and_exposed_to_surfaces():
    document = FakeDocument()
    operation = _operation()

    execution = CommitCoordinator(
        document=document,
        store=DurableStore(document),
    ).execute(lambda: operation, trace_id="surface-trace")

    assert execution.receipt is not None
    assert execution.receipt.event_status is EventStatus.NOT_REQUIRED
    assert len(execution.events) == 1
    event = execution.events[0]
    assert event["type"] == "entity_change"
    assert event["doc_id"] == "doc-1"
    assert event["op_id"] == operation.id
    assert event["command_id"] == operation.command_id
    assert event["commit_id"] == execution.receipt.commit_id
    assert event["document_version"] == 2
    assert event["trace_id"] == "surface-trace"
    assert event["changes"] == []


def test_publisher_cannot_mutate_the_canonical_events_returned_to_surfaces():
    class MutatingPublisher:
        def publish(self, payload):
            payload["command_id"] = "corrupted"
            payload["changes"].append({"corrupted": True})
            return EventStatus.PUBLISHED

    document = FakeDocument()
    execution = CommitCoordinator(
        document,
        DurableStore(document),
        publisher=MutatingPublisher(),
        event_factory=RecordingFactory(),
    ).execute(lambda: _operation())

    assert execution.events[0]["command_id"] == "test.one"
    assert execution.events[0]["changes"] == []


def test_invalid_producer_result_never_reaches_store():
    document = FakeDocument()
    store = DurableStore(document)
    coordinator = CommitCoordinator(document=document, store=store)

    with pytest.raises(TypeError, match="producer must return"):
        coordinator.execute(lambda: {"not": "an operation"})

    assert store.calls == 0


def test_invalid_save_policy_is_rejected_before_producer_mutates():
    document = FakeDocument()
    document.effective_save_policy = "manual_save"
    produced = False

    def producer():
        nonlocal produced
        produced = True
        return _operation()

    with pytest.raises(ValueError, match="unsupported document save policy"):
        CommitCoordinator(document=document, store=DurableStore(document)).execute(producer)

    assert produced is False


def test_concurrent_commit_cannot_publish_a_later_version_first():
    document = FakeDocument()
    lock = TrackingLock()
    store = DurableStore(document, lock=lock)
    first_publish_entered = threading.Event()
    release_first_publish = threading.Event()
    second_producer_started = threading.Event()
    published: list[tuple[str, int]] = []

    class BlockingPublisher:
        def publish(self, payload):
            assert lock.active
            if payload["command_id"] == "test.first":
                first_publish_entered.set()
                assert release_first_publish.wait(timeout=2)
            published.append((payload["command_id"], payload["document_version"]))
            return EventStatus.PUBLISHED

    coordinator = CommitCoordinator(
        document=document,
        store=store,
        publisher=BlockingPublisher(),
        lock=lock,
        event_factory=RecordingFactory(lock=lock),
    )

    thread_a = threading.Thread(
        target=lambda: coordinator.execute(lambda: _operation("first")),
        name="commit-a",
    )

    def produce_second():
        second_producer_started.set()
        return _operation("second")

    thread_b = threading.Thread(
        target=lambda: coordinator.execute(produce_second),
        name="commit-b",
    )

    thread_a.start()
    assert first_publish_entered.wait(timeout=2)
    thread_b.start()
    assert lock.attempted_by_b.wait(timeout=2)
    assert second_producer_started.is_set() is False

    release_first_publish.set()
    thread_a.join(timeout=2)
    thread_b.join(timeout=2)

    assert not thread_a.is_alive()
    assert not thread_b.is_alive()
    assert second_producer_started.is_set() is True
    assert published == [("test.first", 2), ("test.second", 3)]


def test_command_mutation_is_one_commit_and_action_runs_after_publish():
    log: list[str] = []
    document = FakeDocument()
    lock = TrackingLock(log)
    store = DurableStore(document, log, lock)
    coordinator = CommitCoordinator(
        document=document,
        store=store,
        publisher=RecordingPublisher(log=log, lock=lock),
        lock=lock,
        event_factory=RecordingFactory(log, lock),
    )
    mutation = CommandMutation(
        operations=(_operation("start"), _operation("placeholder")),
        after_commit=(DeferredAction(
            id="start-run-1",
            name="pipeline.start",
            run=lambda commit_id: log.append(f"action:{bool(commit_id)}"),
        ),),
    )

    execution = coordinator.execute(lambda: mutation)

    assert store.calls == 1
    assert isinstance(execution.result, MutationBatch)
    assert execution.receipt.operation_ids == tuple(op.id for op in mutation.operations)
    assert [action.status for action in execution.actions] == [ActionStatus.SUCCEEDED]
    assert log == [
        "lock-enter",
        "store",
        "build:test.start",
        "build:test.placeholder",
        "publish:test.start",
        "publish:test.placeholder",
        "action:True",
        "lock-exit",
    ]


def test_lifecycle_guard_covers_producer_store_publisher_and_actions():
    """The topology lease is outermost and remains held through every effect phase."""
    log: list[str] = []

    class LifecycleLease:
        active = False

        def __call__(self):
            return self

        def __enter__(self):
            assert self.active is False
            self.active = True
            log.append("lease-enter")
            return self

        def __exit__(self, exc_type, exc, tb):
            assert self.active is True
            log.append("lease-exit")
            self.active = False

    lease = LifecycleLease()
    document = FakeDocument()
    lock = TrackingLock(log)

    class GuardedStore(DurableStore):
        def commit(self, intent):
            assert lease.active is True
            return super().commit(intent)

    class GuardedFactory:
        def build(self, operation, current_document, receipt):
            assert lease.active is True
            assert lock.active is True
            log.append("factory")
            return {
                "type": "entity_change",
                "doc_id": current_document.id,
                "command_id": operation.command_id,
                "commit_id": receipt.commit_id,
                "document_version": receipt.document_version,
                "changes": [],
            }

    class GuardedPublisher:
        def publish(self, payload):
            assert lease.active is True
            assert lock.active is True
            log.append("publisher")
            return EventStatus.PUBLISHED

    store = GuardedStore(document, log=log, lock=lock)
    coordinator = CommitCoordinator(
        document=document,
        store=store,
        publisher=GuardedPublisher(),
        lock=lock,
        event_factory=GuardedFactory(),
        lifecycle_guard=lease,
    )

    def action(_commit_id):
        assert lease.active is True
        assert lock.active is True
        log.append("action")

    def producer():
        assert lease.active is True
        assert lock.active is True
        log.append("producer")
        return CommandMutation(
            operations=(_operation("guarded"),),
            after_commit=(DeferredAction(id="wake", name="wake", run=action),),
        )

    execution = coordinator.execute(producer)

    assert execution.is_committed is True
    assert [item.status for item in execution.actions] == [ActionStatus.SUCCEEDED]
    assert log == [
        "lease-enter",
        "lock-enter",
        "producer",
        "store",
        "factory",
        "publisher",
        "action",
        "lock-exit",
        "lease-exit",
    ]
    assert lease.active is False


def test_lifecycle_guard_rejection_prevents_producer_and_store():
    """A stale/deleted generation is rejected before any in-memory mutation starts."""
    log: list[str] = []
    produced = False
    document = FakeDocument()
    store = DurableStore(document)
    lock = TrackingLock(log)

    class RejectingLease:
        def __call__(self):
            return self

        def __enter__(self):
            log.append("lease-enter")
            raise ZeError(
                "project_generation_stale",
                "project context belongs to an obsolete generation",
            )

        def __exit__(self, exc_type, exc, tb):
            pytest.fail("a guard whose __enter__ rejects must not be exited")

    def producer():
        nonlocal produced
        produced = True
        return _operation("must-not-run")

    coordinator = CommitCoordinator(
        document=document,
        store=store,
        lock=lock,
        lifecycle_guard=RejectingLease(),
    )
    with pytest.raises(ZeError) as captured:
        coordinator.execute(producer)

    assert captured.value.code == "project_generation_stale"
    assert produced is False
    assert store.calls == 0
    assert log == ["lease-enter"]


def test_after_commit_failure_does_not_reclassify_or_replay_business_commit():
    document = FakeDocument()
    store = DurableStore(document)

    def fail(_commit_id):
        raise RuntimeError("worker unavailable")

    execution = CommitCoordinator(document=document, store=store).execute(lambda: CommandMutation(
        operations=(_operation(),),
        after_commit=(DeferredAction(id="wake", name="worker.wake", run=fail),),
    ))

    assert execution.is_committed is True
    assert store.calls == 1
    assert execution.action_failed is True
    assert execution.actions[0].status is ActionStatus.FAILED
    assert execution.actions[0].code == "after_commit_action_failed"


def test_after_commit_chain_stops_and_marks_later_actions_skipped():
    document = FakeDocument()
    calls: list[str] = []

    def fail(_commit_id):
        calls.append("first")
        raise RuntimeError("stop")

    execution = CommitCoordinator(
        document=document,
        store=DurableStore(document),
    ).execute(lambda: CommandMutation(
        operations=(_operation(),),
        after_commit=(
            DeferredAction(id="first", name="first", run=fail),
            DeferredAction(id="second", name="second", run=lambda _: calls.append("second")),
        ),
    ))

    assert calls == ["first"]
    assert [action.status for action in execution.actions] == [
        ActionStatus.FAILED, ActionStatus.SKIPPED,
    ]


def test_noncommitted_business_result_skips_after_commit_action():
    document = FakeDocument()
    called = False

    def action(_commit_id):
        nonlocal called
        called = True

    execution = CommitCoordinator(
        document=document,
        store=IndeterminateStore(),
    ).execute(lambda: CommandMutation(
        operations=(_operation(),),
        after_commit=(DeferredAction(id="unsafe", name="unsafe", run=action),),
    ))

    assert called is False
    assert execution.actions[0].status is ActionStatus.SKIPPED


def test_nested_execute_is_rejected_before_inner_or_outer_store():
    document = FakeDocument()
    store = DurableStore(document)
    coordinator = CommitCoordinator(document=document, store=store)

    with pytest.raises(NestedCommitError) as caught:
        coordinator.execute(lambda: coordinator.execute(lambda: _operation("inner")))

    assert caught.value.code == "nested_commit_forbidden"
    assert store.calls == 0
