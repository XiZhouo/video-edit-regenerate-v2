from __future__ import annotations

from contextlib import nullcontext

import pytest

import commit_coordinator as coordinator_module
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
from commit_coordinator import CommitCoordinator
from history_authority import HistoryAuthorityError
from operation import new_operation
from ports import ActionStatus


@pytest.fixture(autouse=True)
def _mock_permit_invalidation(monkeypatch):
    """Most seam tests use opaque sentinels instead of framework permits."""
    monkeypatch.setattr(
        coordinator_module,
        "invalidate_pipeline_authority_attestations",
        lambda _permit: None,
    )


class _Session:
    def __init__(self, document: "_Document", log: list[str]) -> None:
        self.document = document
        self.log = log
        self.closed = False

    def commit(self) -> None:
        assert not self.closed
        self.log.append("session-commit")
        self.closed = True

    def rollback(self) -> None:
        assert not self.closed
        self.log.append("session-rollback")
        self.closed = True


class _Document:
    def __init__(self, log: list[str]) -> None:
        self.id = "doc-authority"
        self.version = 1
        self.effective_save_policy = "auto_commit"
        self.log = log

    def begin_mutation_session(self) -> _Session:
        return _Session(self, self.log)

    def find(self, _entity_type, _entity_id):
        return None


class _LegacyDocument:
    def __init__(self) -> None:
        self.id = "doc-authority"
        self.version = 1
        self.effective_save_policy = "auto_commit"

    def find(self, _entity_type, _entity_id):
        return None


class _Store:
    def __init__(
        self,
        document,
        log: list[str],
        *,
        outcome: str = "durable",
    ) -> None:
        self.document = document
        self.log = log
        self.outcome = outcome
        self.calls = 0

    def commit(self, intent):
        self.calls += 1
        self.log.append("store")
        if self.outcome == "durable":
            self.document.version += 1
            return CommitReceipt.committed(
                commit_id=intent.commit_id,
                document_id=intent.document_id,
                operation_ids=intent.operation_ids,
                trace_id=intent.trace_id,
                save_policy=intent.save_policy,
                document_version=self.document.version,
            )

        if self.outcome == "indeterminate":
            self.document.version += 1
            stage = CommitStage.OPLOG
            facts = DurabilityFacts(
                mutation_applied=True,
                document=DurabilityState.DURABLE,
                oplog=DurabilityState.NOT_DURABLE,
            )
            document_version = self.document.version
        elif self.outcome == "not_durable":
            stage = CommitStage.DOCUMENT
            facts = DurabilityFacts(
                mutation_applied=True,
                document=DurabilityState.NOT_DURABLE,
                oplog=DurabilityState.NOT_DURABLE,
            )
            document_version = None
        else:  # pragma: no cover - test helper guard
            raise AssertionError(f"unsupported outcome {self.outcome!r}")

        failure = CommitFailure(
            stage=stage,
            code=f"{self.outcome}_failure",
            message="injected store outcome",
            facts=facts,
        )
        return CommitReceipt.from_failure(
            commit_id=intent.commit_id,
            document_id=intent.document_id,
            operation_ids=intent.operation_ids,
            trace_id=intent.trace_id,
            save_policy=intent.save_policy,
            failure=failure,
            document_version=document_version,
        )


class _HistoryTransaction:
    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.state = "OPEN"

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        self.log.append("history-exit")
        return False

    def prepare_store(self, _operations) -> None:
        self.log.append("history-prepare")
        self.state = "PREPARED"

    def write_scope(self):
        return nullcontext()

    def confirm_durable(self) -> None:
        self.log.append("history-confirm")
        self.state = "DURABLE"


class _HistoryPort:
    def __init__(self, transaction: _HistoryTransaction) -> None:
        self.transaction = transaction

    def open(self, **_kwargs):
        return self.transaction


def _operation():
    return new_operation(
        command_id="test.pipeline-authority",
        source="user",
        actor={"user_id": "u1"},
        patches=[],
        reverse_patches=[],
        document_id="doc-authority",
        trace_id="trace-authority",
    )


def _install_policy_probe(monkeypatch, log: list[str]) -> None:
    monkeypatch.setattr(
        coordinator_module,
        "_validate_policy_coverage",
        lambda _document, _session, _operations: log.append("policy"),
    )


def _prepare_result_for_document(
    expected_document,
    result,
    *,
    log: list[str] | None = None,
):
    """Build a seam double that preserves the exact-Document contract."""

    def prepare(_store, _operations, *, document):
        assert document is expected_document
        if log is not None:
            log.append("prepare")
        return result

    return prepare


def test_pre_store_hook_precedes_intent_history_store_and_fresh_reconcile(
    monkeypatch,
):
    log: list[str] = []
    document = _Document(log)
    store = _Store(document, log)
    permit = object()
    attestation = object()
    _install_policy_probe(monkeypatch, log)

    real_intent = coordinator_module.CommitIntent

    def recording_intent(**kwargs):
        log.append("intent")
        return real_intent(**kwargs)

    def prepare(current_store, operations, **kwargs):
        assert current_store is store
        assert operations == (_operation_value,)
        assert kwargs == {"document": document}
        log.append("prepare")
        return permit

    def reconcile(current_store, current_permit):
        assert current_store is store
        assert current_permit is permit
        log.append("reconcile")
        return (attestation,)

    class EventFactory:
        def build(self, _operation, _document, _receipt):
            log.append("event-build")
            return {"type": "entity_change", "changes": []}

    _operation_value = _operation()
    transaction = _HistoryTransaction(log)
    monkeypatch.setattr(coordinator_module, "CommitIntent", recording_intent)
    monkeypatch.setattr(coordinator_module, "prepare_pipeline_authority_commit", prepare)
    monkeypatch.setattr(
        coordinator_module,
        "reconcile_durable_pipeline_authority",
        reconcile,
    )
    monkeypatch.setattr(
        coordinator_module,
        "invalidate_pipeline_authority_attestations",
        lambda current_permit: (
            pytest.fail("wrong permit")
            if current_permit is not permit
            else log.append("invalidate")
        ),
    )
    monkeypatch.setattr(
        coordinator_module,
        "discard_pipeline_authority_commit",
        lambda _permit: pytest.fail("a reconciled durable permit must not be discarded"),
    )
    monkeypatch.setattr(
        coordinator_module,
        "_history_coordinator_port",
        lambda *_args, **_kwargs: _HistoryPort(transaction),
    )

    execution = CommitCoordinator(
        document,
        store,
        event_factory=EventFactory(),
    ).execute(lambda: _operation_value)

    assert log == [
        "policy",
        "prepare",
        "intent",
        "history-prepare",
        "store",
        "invalidate",
        "history-confirm",
        "session-commit",
        "history-exit",
        "reconcile",
        "event-build",
    ]
    assert execution.receipt is not None
    assert execution.receipt.status is CommitStatus.COMMITTED
    assert "attestation" not in execution.receipt.to_dict()
    assert not hasattr(execution, "pipeline_authority_attestation")


def test_pre_store_error_rolls_back_and_never_builds_intent_or_touches_store(
    monkeypatch,
):
    log: list[str] = []
    document = _Document(log)
    store = _Store(document, log)
    error = RuntimeError("pre-store closure rejected")
    _install_policy_probe(monkeypatch, log)

    def reject(_store, _operations, **kwargs):
        assert kwargs == {"document": document}
        log.append("prepare")
        raise error

    monkeypatch.setattr(coordinator_module, "prepare_pipeline_authority_commit", reject)

    with pytest.raises(RuntimeError) as caught:
        CommitCoordinator(document, store).execute(_operation)

    assert caught.value is error
    assert log == ["policy", "prepare", "session-rollback"]
    assert store.calls == 0


def test_permit_is_discarded_when_a_later_pre_store_step_fails(monkeypatch):
    log: list[str] = []
    document = _Document(log)
    store = _Store(document, log)
    permit = object()
    error = RuntimeError("intent construction rejected")
    _install_policy_probe(monkeypatch, log)

    monkeypatch.setattr(
        coordinator_module,
        "prepare_pipeline_authority_commit",
        _prepare_result_for_document(document, permit, log=log),
    )
    monkeypatch.setattr(
        coordinator_module,
        "CommitIntent",
        lambda **_kwargs: (log.append("intent"), (_ for _ in ()).throw(error))[1],
    )
    monkeypatch.setattr(
        coordinator_module,
        "discard_pipeline_authority_commit",
        lambda current_permit: (
            pytest.fail("wrong permit")
            if current_permit is not permit
            else log.append("discard")
        ),
    )

    with pytest.raises(RuntimeError) as caught:
        CommitCoordinator(document, store).execute(_operation)

    assert caught.value is error
    assert log == ["policy", "prepare", "intent", "session-rollback", "discard"]
    assert store.calls == 0


@pytest.mark.parametrize(
    ("outcome", "document_factory", "expected_status", "session_event"),
    [
        ("not_durable", _Document, CommitStatus.ABORTED, "session-rollback"),
        ("not_durable", lambda _log: _LegacyDocument(), CommitStatus.DIRTY, None),
        ("indeterminate", _Document, CommitStatus.INDETERMINATE, "session-commit"),
    ],
)
def test_non_durable_outcomes_discard_without_reconcile(
    monkeypatch,
    outcome,
    document_factory,
    expected_status,
    session_event,
):
    log: list[str] = []
    document = document_factory(log)
    store = _Store(document, log, outcome=outcome)
    permit = object()
    invalidated: list[object] = []
    monkeypatch.setattr(
        coordinator_module,
        "prepare_pipeline_authority_commit",
        _prepare_result_for_document(document, permit),
    )
    monkeypatch.setattr(
        coordinator_module,
        "discard_pipeline_authority_commit",
        lambda current_permit: (
            pytest.fail("wrong permit")
            if current_permit is not permit
            else log.append("discard")
        ),
    )
    monkeypatch.setattr(
        coordinator_module,
        "reconcile_durable_pipeline_authority",
        lambda *_args: pytest.fail("non-durable receipt must not reconcile"),
    )
    monkeypatch.setattr(
        coordinator_module,
        "invalidate_pipeline_authority_attestations",
        lambda current_permit: invalidated.append(current_permit),
    )

    execution = CommitCoordinator(document, store).execute(_operation)

    assert execution.receipt is not None
    assert execution.receipt.status is expected_status
    if session_event is not None:
        assert log == ["store", session_event, "discard"]
    else:
        assert log == ["store", "discard"]
    assert execution.events == ()
    assert invalidated == ([permit] if outcome == "indeterminate" else [])


def test_store_base_exception_becomes_unknown_and_revokes_before_clean_discard(
    monkeypatch,
):
    log: list[str] = []
    document = _Document(log)
    permit = object()
    interruption = KeyboardInterrupt("interrupted after storage began")

    class InterruptedStore:
        def commit(self, _intent):
            log.append("store")
            raise interruption

    monkeypatch.setattr(
        coordinator_module,
        "prepare_pipeline_authority_commit",
        _prepare_result_for_document(document, permit),
    )
    monkeypatch.setattr(
        coordinator_module,
        "invalidate_pipeline_authority_attestations",
        lambda current_permit: (
            pytest.fail("wrong permit")
            if current_permit is not permit
            else log.append("invalidate")
        ),
    )
    monkeypatch.setattr(
        coordinator_module,
        "discard_pipeline_authority_commit",
        lambda current_permit: (
            pytest.fail("wrong permit")
            if current_permit is not permit
            else log.append("discard")
        ),
    )

    execution = CommitCoordinator(document, InterruptedStore()).execute(_operation)

    assert execution.receipt is not None
    assert execution.receipt.status is CommitStatus.INDETERMINATE
    assert execution.receipt.retry_safe is False
    assert execution.receipt.failure is not None
    assert execution.receipt.failure.facts.document is DurabilityState.UNKNOWN
    assert execution.receipt.failure.facts.oplog is DurabilityState.UNKNOWN
    assert log == ["store", "invalidate", "session-commit", "discard"]


def test_cleanup_failure_after_indeterminate_receipt_remains_replay_unsafe(
    monkeypatch,
):
    log: list[str] = []
    document = _Document(log)
    permit = object()
    cleanup_error = RuntimeError("discard failed")
    store = _Store(document, log, outcome="indeterminate")

    monkeypatch.setattr(
        coordinator_module,
        "prepare_pipeline_authority_commit",
        _prepare_result_for_document(document, permit),
    )
    monkeypatch.setattr(
        coordinator_module,
        "discard_pipeline_authority_commit",
        lambda _permit: (_ for _ in ()).throw(cleanup_error),
    )

    with pytest.raises(HistoryAuthorityError) as caught:
        CommitCoordinator(document, store).execute(_operation)

    error = caught.value
    assert error.reason == "pipeline_authority_cleanup_failed"
    assert error.outcome_unknown is True
    assert error.durability_phase == "post_write_verification"
    assert error.commit_facts["outcome"] == "indeterminate"
    assert error.commit_facts["document_durable"] is True
    assert error.commit_facts["oplog_durable"] is False
    assert error.__cause__ is cleanup_error


def test_durable_session_finalizer_error_discards_without_reconcile(monkeypatch):
    log: list[str] = []
    finalization_error = RuntimeError("session finalizer failed")

    class FailingSession(_Session):
        def commit(self) -> None:
            self.log.append("session-commit")
            raise finalization_error

    class FailingDocument(_Document):
        def begin_mutation_session(self):
            return FailingSession(self, self.log)

    document = FailingDocument(log)
    store = _Store(document, log)
    permit = object()
    monkeypatch.setattr(
        coordinator_module,
        "prepare_pipeline_authority_commit",
        _prepare_result_for_document(document, permit),
    )
    monkeypatch.setattr(
        coordinator_module,
        "discard_pipeline_authority_commit",
        lambda current_permit: (
            pytest.fail("wrong permit")
            if current_permit is not permit
            else log.append("discard")
        ),
    )
    monkeypatch.setattr(
        coordinator_module,
        "reconcile_durable_pipeline_authority",
        lambda *_args: pytest.fail("session must finalize before fresh reconcile"),
    )

    with pytest.raises(HistoryAuthorityError) as caught:
        CommitCoordinator(document, store).execute(_operation)

    assert caught.value.reason == "history_session_finalize_failed"
    assert caught.value.outcome_unknown is True
    assert caught.value.durability_phase == "post_write_verification"
    assert caught.value.commit_facts["outcome"] == "committed"
    assert caught.value.commit_facts["document_durable"] is True
    assert caught.value.commit_facts["oplog_durable"] is True
    assert caught.value.__cause__ is finalization_error
    assert log == ["store", "session-commit", "discard"]


def test_post_durable_authority_error_gets_commit_facts_and_prevents_events(
    monkeypatch,
):
    log: list[str] = []
    document = _Document(log)
    store = _Store(document, log)
    permit = object()

    error = coordinator_module.PipelineAuthorityCommitError(
        "pipeline_authority_unavailable",
        "pipeline_authority_fresh_scan_failed",
        "fresh OpLog unavailable",
        outcome_unknown=True,
        durability_phase="post_write_verification",
    )
    monkeypatch.setattr(
        coordinator_module,
        "prepare_pipeline_authority_commit",
        _prepare_result_for_document(document, permit),
    )

    def reconcile(_store, current_permit):
        assert current_permit is permit
        log.append("reconcile")
        raise error

    monkeypatch.setattr(
        coordinator_module,
        "reconcile_durable_pipeline_authority",
        reconcile,
    )
    monkeypatch.setattr(
        coordinator_module,
        "discard_pipeline_authority_commit",
        lambda current_permit: (
            pytest.fail("wrong permit")
            if current_permit is not permit
            else log.append("discard")
        ),
    )

    class EventFactory:
        def build(self, *_args):
            pytest.fail("fresh reconcile failure must precede event build")

    with pytest.raises(coordinator_module.PipelineAuthorityCommitError) as caught:
        CommitCoordinator(
            document,
            store,
            event_factory=EventFactory(),
        ).execute(_operation)

    assert caught.value is error
    assert log == ["store", "session-commit", "reconcile", "discard"]
    assert error.commit_facts == {
        "phase": "oplog",
        "outcome": "committed",
        "commit_id": error.commit_facts["commit_id"],
        "retry_safe": False,
        "document_durable": True,
        "oplog_durable": True,
    }
    assert len(error.commit_facts["commit_id"]) == 32


def test_unexpected_reconcile_base_exception_is_stable_and_repair_required(
    monkeypatch,
):
    log: list[str] = []
    document = _Document(log)
    store = _Store(document, log)
    permit = object()
    unexpected = KeyboardInterrupt("injected interruption")

    monkeypatch.setattr(
        coordinator_module,
        "prepare_pipeline_authority_commit",
        _prepare_result_for_document(document, permit),
    )

    def reconcile(_store, _permit):
        log.append("reconcile")
        raise unexpected

    monkeypatch.setattr(
        coordinator_module,
        "reconcile_durable_pipeline_authority",
        reconcile,
    )
    monkeypatch.setattr(
        coordinator_module,
        "discard_pipeline_authority_commit",
        lambda _permit: log.append("discard"),
    )

    with pytest.raises(coordinator_module.PipelineAuthorityCommitError) as caught:
        CommitCoordinator(document, store).execute(_operation)

    error = caught.value
    assert error.code == "pipeline_authority_unavailable"
    assert error.reason == "pipeline_authority_reconcile_unexpected"
    assert error.outcome_unknown is True
    assert error.durability_phase == "post_write_verification"
    assert error.__cause__ is unexpected
    assert error.commit_facts["outcome"] == "committed"
    assert error.commit_facts["retry_safe"] is False
    assert log == ["store", "session-commit", "reconcile", "discard"]


def test_history_finalizer_failure_discards_before_any_attestation(monkeypatch):
    log: list[str] = []
    document = _Document(log)
    store = _Store(document, log)
    permit = object()
    finalization_error = RuntimeError("history finalizer failed")

    class FailingHistoryTransaction(_HistoryTransaction):
        def __exit__(self, _exc_type, _exc, _tb):
            self.log.append("history-exit")
            raise finalization_error

    transaction = FailingHistoryTransaction(log)
    monkeypatch.setattr(
        coordinator_module,
        "_history_coordinator_port",
        lambda *_args, **_kwargs: _HistoryPort(transaction),
    )
    monkeypatch.setattr(
        coordinator_module,
        "prepare_pipeline_authority_commit",
        _prepare_result_for_document(document, permit, log=log),
    )
    monkeypatch.setattr(
        coordinator_module,
        "reconcile_durable_pipeline_authority",
        lambda *_args: pytest.fail("history must finalize before fresh reconcile"),
    )
    monkeypatch.setattr(
        coordinator_module,
        "discard_pipeline_authority_commit",
        lambda current_permit: (
            pytest.fail("wrong permit")
            if current_permit is not permit
            else log.append("discard")
        ),
    )

    with pytest.raises(coordinator_module.PipelineAuthorityCommitError) as caught:
        CommitCoordinator(document, store).execute(_operation)

    error = caught.value
    assert error.code == "pipeline_authority_unavailable"
    assert error.reason == "pipeline_authority_history_finalize_failed"
    assert error.outcome_unknown is True
    assert error.durability_phase == "post_write_verification"
    assert error.__cause__ is finalization_error
    assert error.commit_facts["outcome"] == "committed"
    assert error.commit_facts["document_durable"] is True
    assert error.commit_facts["oplog_durable"] is True
    assert log == [
        "prepare",
        "history-prepare",
        "store",
        "history-confirm",
        "session-commit",
        "history-exit",
        "discard",
    ]


@pytest.mark.parametrize(
    ("failure_phase", "expected_reason"),
    [
        ("confirm", "history_durable_verification_failed"),
        ("finalize", "pipeline_authority_history_finalize_failed"),
    ],
)
def test_preflight_history_error_after_durable_store_is_wrapped_replay_unsafe(
    monkeypatch,
    failure_phase,
    expected_reason,
):
    log: list[str] = []
    document = _Document(log)
    store = _Store(document, log)
    permit = object()
    preflight_error = HistoryAuthorityError(
        "history_authority_retired",
        "injected stale history owner",
    )

    class FailingHistoryTransaction(_HistoryTransaction):
        def confirm_durable(self) -> None:
            self.log.append("history-confirm")
            if failure_phase == "confirm":
                raise preflight_error
            self.state = "DURABLE"

        def __exit__(self, _exc_type, _exc, _tb):
            self.log.append("history-exit")
            if failure_phase == "finalize":
                raise preflight_error
            return False

    transaction = FailingHistoryTransaction(log)
    monkeypatch.setattr(
        coordinator_module,
        "_history_coordinator_port",
        lambda *_args, **_kwargs: _HistoryPort(transaction),
    )
    monkeypatch.setattr(
        coordinator_module,
        "prepare_pipeline_authority_commit",
        _prepare_result_for_document(document, permit),
    )
    monkeypatch.setattr(
        coordinator_module,
        "reconcile_durable_pipeline_authority",
        lambda *_args: pytest.fail("history failure must precede reconcile"),
    )
    monkeypatch.setattr(
        coordinator_module,
        "discard_pipeline_authority_commit",
        lambda _permit: log.append("discard"),
    )

    with pytest.raises(HistoryAuthorityError) as caught:
        CommitCoordinator(document, store).execute(_operation)

    error = caught.value
    assert error is not preflight_error
    assert error.reason == expected_reason
    assert error.outcome_unknown is True
    assert error.durability_phase == "post_write_verification"
    assert error.commit_facts["outcome"] == "committed"
    assert error.commit_facts["document_durable"] is True
    assert error.commit_facts["oplog_durable"] is True
    assert error.__cause__ is preflight_error


@pytest.mark.parametrize("failure_phase", ["build", "publish"])
def test_event_failure_occurs_after_reconcile_and_skips_actions(
    monkeypatch,
    failure_phase,
):
    log: list[str] = []
    document = _Document(log)
    store = _Store(document, log)
    permit = object()
    ran: list[str] = []
    monkeypatch.setattr(
        coordinator_module,
        "prepare_pipeline_authority_commit",
        _prepare_result_for_document(document, permit),
    )
    monkeypatch.setattr(
        coordinator_module,
        "reconcile_durable_pipeline_authority",
        lambda _store, current_permit: (
            pytest.fail("wrong permit")
            if current_permit is not permit
            else (log.append("reconcile"), (object(),))[1]
        ),
    )
    monkeypatch.setattr(
        coordinator_module,
        "discard_pipeline_authority_commit",
        lambda _permit: pytest.fail("successful reconcile consumes the permit"),
    )

    class EventFactory:
        def build(self, *_args):
            log.append("event-build")
            if failure_phase == "build":
                raise RuntimeError("event build failed")
            return {"type": "entity_change", "changes": []}

    class Publisher:
        def publish(self, _payload):
            log.append("event-publish")
            raise RuntimeError("event publish failed")

    action = DeferredAction(
        id="start-worker",
        name="start worker",
        run=lambda _commit_id: ran.append("ran"),
    )
    produced = CommandMutation((_operation(),), (action,))
    execution = CommitCoordinator(
        document,
        store,
        publisher=Publisher(),
        event_factory=EventFactory(),
    ).execute(lambda: produced)

    assert execution.receipt is not None
    assert execution.receipt.status is CommitStatus.COMMITTED
    assert execution.receipt.event_status is EventStatus.FAILED
    assert execution.receipt.retry_safe is False
    assert log[:3] == ["store", "session-commit", "reconcile"]
    assert log[3:] == (
        ["event-build"]
        if failure_phase == "build"
        else ["event-build", "event-publish"]
    )
    assert ran == []
    assert len(execution.actions) == 1
    assert execution.actions[0].status is ActionStatus.SKIPPED


def test_non_authority_commit_runs_prepare_but_never_reconcile_or_discard(
    monkeypatch,
):
    log: list[str] = []
    document = _Document(log)
    store = _Store(document, log)

    def prepare(_store, _operations, **kwargs):
        assert kwargs == {"document": document}
        log.append("prepare")
        return None

    monkeypatch.setattr(coordinator_module, "prepare_pipeline_authority_commit", prepare)
    monkeypatch.setattr(
        coordinator_module,
        "reconcile_durable_pipeline_authority",
        lambda *_args: pytest.fail("None permit must not reconcile"),
    )
    monkeypatch.setattr(
        coordinator_module,
        "discard_pipeline_authority_commit",
        lambda *_args: pytest.fail("None permit must not be discarded"),
    )

    execution = CommitCoordinator(document, store).execute(_operation)

    assert execution.is_committed is True
    assert log == ["prepare", "store", "session-commit"]
