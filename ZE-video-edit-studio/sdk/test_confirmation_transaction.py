"""确认令牌必须跟随 CommitReceipt，而不是在 producer 开始前被弹出。"""
from __future__ import annotations

import pytest

from sdk import (
    Actor,
    CommitFailure,
    CommitReceipt,
    CommitStage,
    CommitStatus,
    CommandRegistry,
    CommandSpec,
    CommitCoordinator,
    DurabilityFacts,
    DurabilityState,
    Entity,
    EventStatus,
    Patch,
    SavePolicy,
    ZeError,
    new_operation,
)
from sdk.persistence import execute_confirmation
from history_authority import HistoryAuthorityError


class _Store:
    def __init__(self, mode: str = "committed") -> None:
        self.mode = mode

    def commit(self, intent):
        if self.mode == "committed":
            return CommitReceipt.committed(
                commit_id=intent.commit_id,
                document_id=intent.document_id,
                operation_ids=intent.operation_ids,
                trace_id=intent.trace_id,
                save_policy=intent.save_policy,
                document_version=2,
            )
        state = (
            DurabilityState.UNKNOWN
            if self.mode == "indeterminate"
            else DurabilityState.NOT_DURABLE
        )
        failure = CommitFailure(
            stage=CommitStage.DOCUMENT,
            code="injected_store_failure",
            message="injected store failure",
            facts=DurabilityFacts(
                mutation_applied=True,
                document=state,
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
        )


class _Publisher:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    def publish(self, _payload):
        if self.fail:
            raise RuntimeError("delivery failed")
        return EventStatus.PUBLISHED


def _surface(*, store_mode: str = "committed", publisher_fail: bool = False):
    from sdk import Document

    actor = Actor(user_id="u1")
    doc = Document(schema_version="1", collaboration_mode="with_save")
    doc.add(Entity(type="item", id="e1", data={"value": 1}))
    registry = CommandRegistry()
    poll = {"open": True}

    def execute(args, actor, source, doc):
        entity = doc.get("item", "e1")
        before = entity.data["value"]
        after = int(args["value"])
        entity.update({"value": after}, expected_version=entity.version, actor=actor)
        return new_operation(
            command_id="item.set",
            source=source,
            actor=actor.to_dict(),
            patches=[Patch("item", "e1", "value", "set", before, after)],
            reverse_patches=[Patch("item", "e1", "value", "set", after, before)],
        )

    registry.register(CommandSpec(
        id="item.set",
        label="set",
        requires_confirm=True,
        poll=lambda _args: poll["open"],
        execute=execute,
    ))
    store = _Store(store_mode)
    coordinator = CommitCoordinator(
        doc,
        store,
        publisher=_Publisher(publisher_fail),
    )
    pending = coordinator.execute(lambda: registry.dispatch(
        "item.set", {"value": 2}, actor=actor, source="user", doc=doc,
    )).pending_confirmation
    assert pending is not None
    return actor, doc, registry, coordinator, pending, store, poll


def _confirm(actor, doc, registry, coordinator, pending):
    return execute_confirmation(
        registry=registry,
        coordinator=coordinator,
        token=pending.token,
        actor=actor,
        document=doc,
        approval_channel="human_http",
        approval_nonce=pending.approval_nonce,
    )


def test_committed_confirmation_consumes_token():
    actor, doc, registry, coordinator, pending, _store, _poll = _surface()

    execution = _confirm(actor, doc, registry, coordinator, pending)

    assert execution.receipt.status is CommitStatus.COMMITTED
    assert execution.receipt.trace_id == pending.trace_id
    assert registry.pending_store.state(pending.token) is None
    assert doc.get("item", "e1").data["value"] == 2
    with pytest.raises(ZeError) as caught:
        _confirm(actor, doc, registry, coordinator, pending)
    assert caught.value.code == "confirm_token_unknown"


def test_ai_channel_cannot_self_approve_even_for_the_same_user():
    actor, doc, registry, coordinator, pending, _store, _poll = _surface()

    with pytest.raises(ZeError) as caught:
        execute_confirmation(
            registry=registry,
            coordinator=coordinator,
            token=pending.token,
            actor=actor,
            document=doc,
            approval_channel="mcp_ai",
            approval_nonce="",
        )

    assert caught.value.code == "confirmation_human_required"
    assert registry.pending_store.state(pending.token) == "pending"
    assert doc.get("item", "e1").data["value"] == 1


def test_event_failure_still_consumes_because_business_commit_is_durable():
    actor, doc, registry, coordinator, pending, _store, _poll = _surface(
        publisher_fail=True,
    )

    execution = _confirm(actor, doc, registry, coordinator, pending)

    assert execution.receipt.status is CommitStatus.COMMITTED
    assert execution.receipt.event_status is EventStatus.FAILED
    assert registry.pending_store.state(pending.token) is None


def test_proven_aborted_commit_releases_token_and_allows_retry():
    actor, doc, registry, coordinator, pending, store, _poll = _surface(
        store_mode="aborted",
    )

    execution = _confirm(actor, doc, registry, coordinator, pending)

    assert execution.receipt.status is CommitStatus.ABORTED
    assert execution.receipt.failure.facts.rollback_succeeded is True
    assert registry.pending_store.state(pending.token) == "pending"
    assert doc.get("item", "e1").data["value"] == 1

    store.mode = "committed"
    retry = _confirm(actor, doc, registry, coordinator, pending)
    assert retry.receipt.status is CommitStatus.COMMITTED
    assert registry.pending_store.state(pending.token) is None


def test_indeterminate_commit_freezes_token_instead_of_replaying():
    actor, doc, registry, coordinator, pending, _store, _poll = _surface(
        store_mode="indeterminate",
    )

    execution = _confirm(actor, doc, registry, coordinator, pending)

    assert execution.receipt.status is CommitStatus.INDETERMINATE
    assert registry.pending_store.state(pending.token) == "frozen"
    with pytest.raises(ZeError) as caught:
        _confirm(actor, doc, registry, coordinator, pending)
    assert caught.value.code == "confirmation_repair_required"


def test_pre_storage_validation_failure_releases_token():
    actor, doc, registry, coordinator, pending, _store, poll = _surface()
    poll["open"] = False

    with pytest.raises(ZeError) as caught:
        _confirm(actor, doc, registry, coordinator, pending)

    assert caught.value.code == "poll_failed"
    assert registry.pending_store.state(pending.token) == "pending"


def test_rollback_failure_freezes_token():
    actor, doc, registry, coordinator, pending, _store, _poll = _surface()

    def fail(_producer, trace_id=""):
        raise ZeError("mutation_rollback_failed", "rollback failed")

    coordinator.execute = fail
    with pytest.raises(ZeError) as caught:
        _confirm(actor, doc, registry, coordinator, pending)
    assert caught.value.code == "mutation_rollback_failed"
    assert registry.pending_store.state(pending.token) == "frozen"


def test_postwrite_history_unknown_freezes_confirmation_token():
    actor, doc, registry, coordinator, pending, _store, _poll = _surface()

    def fail(_producer, trace_id=""):
        raise HistoryAuthorityError(
            "history_append_outcome_indeterminate",
            "post-write verification failed",
        )

    coordinator.execute = fail
    with pytest.raises(HistoryAuthorityError) as caught:
        _confirm(actor, doc, registry, coordinator, pending)

    assert caught.value.outcome_unknown is True
    assert registry.pending_store.state(pending.token) == "frozen"
    with pytest.raises(ZeError) as retry:
        _confirm(actor, doc, registry, coordinator, pending)
    assert retry.value.code == "confirmation_repair_required"


def test_preflight_history_mismatch_releases_confirmation_token():
    actor, doc, registry, coordinator, pending, _store, _poll = _surface()

    def fail(_producer, trace_id=""):
        raise HistoryAuthorityError(
            "history_head_changed_before_store",
            "reload required",
        )

    coordinator.execute = fail
    with pytest.raises(HistoryAuthorityError) as caught:
        _confirm(actor, doc, registry, coordinator, pending)

    assert caught.value.outcome_unknown is False
    assert registry.pending_store.state(pending.token) == "pending"
