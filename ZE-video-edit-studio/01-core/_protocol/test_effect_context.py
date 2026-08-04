from __future__ import annotations

import asyncio

import pytest

from commit_contracts import CommitReceipt, EventStatus
from commit_coordinator import CommitCoordinator
from command import CommandMutation, DeferredAction
from effect_context import (
    EffectPhase,
    after_commit_effect_phase,
    assert_external_effect_allowed,
    current_effect_phase,
    effect_phase,
    in_producer_phase,
    producer_effect_phase,
)
from entity import ZeError
from operation import new_operation


class _Document:
    id = "effect-phase-doc"
    version = 1
    effective_save_policy = "auto_commit"

    def find(self, entity_type, entity_id):
        return None


def _operation():
    return new_operation(
        command_id="effect.phase",
        source="system",
        actor={"user_id": "__test__"},
        patches=[],
        reverse_patches=[],
        document_id=_Document.id,
    )


def test_nested_contexts_restore_exact_outer_phase_and_guard_producer():
    assert current_effect_phase() is EffectPhase.IDLE
    assert in_producer_phase() is False
    with after_commit_effect_phase():
        assert current_effect_phase() is EffectPhase.AFTER_COMMIT
        assert_external_effect_allowed("notify.send")
        with producer_effect_phase():
            assert in_producer_phase() is True
            with pytest.raises(ZeError) as exc:
                assert_external_effect_allowed("notify.send")
            assert exc.value.code == "producer_effect_forbidden"
        assert current_effect_phase() is EffectPhase.AFTER_COMMIT
    assert current_effect_phase() is EffectPhase.IDLE


@pytest.mark.parametrize("error", [RuntimeError("boom"), KeyboardInterrupt("stop")])
def test_context_resets_after_exception_and_base_exception(error):
    with pytest.raises(type(error)):
        with producer_effect_phase():
            raise error
    assert current_effect_phase() is EffectPhase.IDLE


def test_coordinator_marks_only_producer_and_action_as_effect_phases():
    observed = []
    document = _Document()

    class Store:
        def commit(self, intent):
            observed.append(("store", current_effect_phase()))
            document.version += 1
            return CommitReceipt.committed(
                commit_id=intent.commit_id,
                document_id=intent.document_id,
                operation_ids=intent.operation_ids,
                trace_id=intent.trace_id,
                save_policy=intent.save_policy,
                document_version=document.version,
            )

    class Factory:
        def build(self, operation, current_document, receipt):
            observed.append(("event_build", current_effect_phase()))
            return {"type": "entity_change", "changes": []}

    class Publisher:
        def publish(self, payload):
            observed.append(("publish", current_effect_phase()))
            return EventStatus.PUBLISHED

    operation = _operation()

    def producer():
        observed.append(("producer", current_effect_phase()))
        return CommandMutation(
            operations=(operation,),
            after_commit=(DeferredAction(
                id="effect-action",
                name="effect action",
                run=lambda commit_id: observed.append(("action", current_effect_phase())),
            ),),
        )

    execution = CommitCoordinator(
        document,
        Store(),
        event_factory=Factory(),
        publisher=Publisher(),
    ).execute(producer)

    assert execution.is_committed is True
    assert observed == [
        ("producer", EffectPhase.PRODUCER),
        ("store", EffectPhase.IDLE),
        ("event_build", EffectPhase.IDLE),
        ("publish", EffectPhase.IDLE),
        ("action", EffectPhase.AFTER_COMMIT),
    ]
    assert current_effect_phase() is EffectPhase.IDLE


def test_coordinator_resets_phase_when_producer_is_interrupted():
    coordinator = CommitCoordinator(_Document(), object())

    def producer():
        assert current_effect_phase() is EffectPhase.PRODUCER
        raise KeyboardInterrupt("cancel")

    with pytest.raises(KeyboardInterrupt):
        coordinator.execute(producer)
    assert current_effect_phase() is EffectPhase.IDLE


def test_contextvar_isolates_concurrent_async_tasks():
    async def worker(phase):
        with effect_phase(phase):
            await asyncio.sleep(0)
            return current_effect_phase()

    async def run():
        return await asyncio.gather(
            worker(EffectPhase.PRODUCER),
            worker(EffectPhase.AFTER_COMMIT),
        )

    assert asyncio.run(run()) == [EffectPhase.PRODUCER, EffectPhase.AFTER_COMMIT]
    assert current_effect_phase() is EffectPhase.IDLE
