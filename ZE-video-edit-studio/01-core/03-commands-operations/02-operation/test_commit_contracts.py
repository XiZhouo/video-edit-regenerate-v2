"""Commit Flow 线协议：失败边界、retry_safe 与稳定 JSON。"""

from __future__ import annotations

import json

import pytest

from commit_contracts import (
    CommitFailure,
    CommitReceipt,
    CommitStage,
    CommitStatus,
    DurabilityState,
    DurabilityFacts,
    EventStatus,
    SavePolicy,
    classify_commit_failure,
)


def _failure(stage: CommitStage, facts: DurabilityFacts, **kwargs) -> CommitFailure:
    return CommitFailure(stage, "commit_failed", "提交失败", facts, **kwargs)


def test_save_policy_has_no_manual_save_mode():
    assert [policy.value for policy in SavePolicy] == [
        "auto_commit",
        "checkpoint_commit",
        "legacy",
    ]


@pytest.mark.parametrize(
    ("stage", "facts"),
    [
        (CommitStage.VALIDATE, DurabilityFacts()),
        (CommitStage.APPLY, DurabilityFacts(mutation_applied=True, rollback_succeeded=True)),
        (
            CommitStage.DOCUMENT,
            DurabilityFacts(mutation_applied=True, rollback_succeeded=True),
        ),
    ],
)
def test_pre_durability_failures_are_aborted_and_safe_to_retry(stage, facts):
    failure = _failure(stage, facts)
    assert failure.status is CommitStatus.ABORTED
    assert failure.event_status is EventStatus.NOT_REQUIRED
    assert failure.retry_safe is True


def test_unrolled_in_memory_mutation_is_dirty_and_not_retry_safe():
    failure = _failure(CommitStage.APPLY, DurabilityFacts(mutation_applied=True))
    assert failure.status is CommitStatus.DIRTY
    assert failure.retry_safe is False


def test_document_durable_but_oplog_not_durable_is_indeterminate():
    facts = DurabilityFacts(mutation_applied=True, document=DurabilityState.DURABLE)
    for stage in (CommitStage.DOCUMENT, CommitStage.OPLOG):
        failure = _failure(stage, facts)
        assert failure.status is CommitStatus.INDETERMINATE
        assert failure.retry_safe is False


def test_oplog_durable_ack_failure_is_already_committed():
    failure = _failure(
        CommitStage.OPLOG,
        DurabilityFacts(
            mutation_applied=True,
            document=DurabilityState.DURABLE,
            oplog=DurabilityState.DURABLE,
        ),
    )
    assert failure.status is CommitStatus.COMMITTED
    assert failure.event_status is EventStatus.NOT_REQUIRED
    assert failure.retry_safe is False


@pytest.mark.parametrize("event_status", [EventStatus.PENDING, EventStatus.FAILED])
def test_event_failure_does_not_undo_a_committed_command(event_status):
    failure = _failure(
        CommitStage.EVENT,
        DurabilityFacts(
            mutation_applied=True,
            document=DurabilityState.DURABLE,
            oplog=DurabilityState.DURABLE,
        ),
        event_status=event_status,
    )
    assert failure.status is CommitStatus.COMMITTED
    assert failure.event_status is event_status
    assert failure.retry_safe is False


def test_every_failure_stage_has_an_explicit_mapping():
    samples = {
        CommitStage.VALIDATE: (DurabilityFacts(), {}),
        CommitStage.APPLY: (
            DurabilityFacts(mutation_applied=True, rollback_succeeded=True),
            {},
        ),
        CommitStage.DOCUMENT: (
            DurabilityFacts(mutation_applied=True, rollback_succeeded=True),
            {},
        ),
        CommitStage.OPLOG: (
            DurabilityFacts(mutation_applied=True, document=DurabilityState.DURABLE),
            {},
        ),
        CommitStage.EVENT: (
            DurabilityFacts(
                mutation_applied=True,
                document=DurabilityState.DURABLE,
                oplog=DurabilityState.DURABLE,
            ),
            {"event_status": EventStatus.FAILED},
        ),
    }
    assert set(samples) == set(CommitStage)
    for stage, (facts, kwargs) in samples.items():
        assert classify_commit_failure(stage, facts, **kwargs)


def test_impossible_durability_facts_and_stage_combinations_are_rejected():
    with pytest.raises(ValueError, match="requires a durable or unknown document"):
        DurabilityFacts(oplog=DurabilityState.DURABLE)
    with pytest.raises(ValueError, match="requires a durable or unknown document"):
        _failure(CommitStage.OPLOG, DurabilityFacts())
    with pytest.raises(ValueError, match="pending or failed"):
        _failure(
            CommitStage.EVENT,
            DurabilityFacts(
                mutation_applied=True,
                document=DurabilityState.DURABLE,
                oplog=DurabilityState.DURABLE,
            ),
            event_status=EventStatus.PUBLISHED,
        )


def test_commit_receipt_has_fixed_wire_shape_and_canonical_json():
    receipt = CommitReceipt.committed(
        commit_id="commit-1",
        document_id="doc-1",
        operation_ids=("op-1", "op-2"),
        trace_id="trace-1",
        save_policy=SavePolicy.AUTO_COMMIT,
        document_version=7,
        event_status=EventStatus.PUBLISHED,
    )
    expected = {
        "schema_version": "ze.commit-receipt/1",
        "commit_id": "commit-1",
        "document_id": "doc-1",
        "operation_ids": ["op-1", "op-2"],
        "trace_id": "trace-1",
        "save_policy": "auto_commit",
        "status": "committed",
        "event_status": "published",
        "stage": "event",
        "retry_safe": False,
        "document_version": 7,
        "document_durable": True,
        "oplog_durable": True,
        "failure": None,
    }
    assert receipt.to_dict() == expected
    assert receipt.to_json() == json.dumps(
        expected,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    assert CommitReceipt.from_json(receipt.to_json()) == receipt


def test_failure_receipt_round_trips_without_losing_boundary_facts():
    failure = _failure(
        CommitStage.OPLOG,
        DurabilityFacts(mutation_applied=True, document=DurabilityState.DURABLE),
    )
    receipt = CommitReceipt.from_failure(
        commit_id="commit-2",
        document_id="doc-2",
        operation_ids=("op-2",),
        trace_id="trace-2",
        save_policy=SavePolicy.CHECKPOINT_COMMIT,
        failure=failure,
        document_version=3,
    )
    restored = CommitReceipt.from_json(receipt.to_json())
    assert restored == receipt
    assert restored.status is CommitStatus.INDETERMINATE
    assert restored.failure is not None
    assert restored.failure.facts.document_durable is True
    assert restored.failure.facts.oplog_durable is False


def test_receipt_rejects_tampered_derived_status():
    receipt = CommitReceipt.from_failure(
        commit_id="commit-3",
        document_id="doc-3",
        operation_ids=(),
        trace_id="trace-3",
        save_policy=SavePolicy.LEGACY,
        failure=_failure(CommitStage.VALIDATE, DurabilityFacts()),
    )
    payload = receipt.to_dict()
    payload["retry_safe"] = False
    with pytest.raises(ValueError, match="must match"):
        CommitReceipt.from_dict(payload)


@pytest.mark.parametrize("unknown_field", ["document", "oplog"])
def test_unknown_write_outcome_is_indeterminate_and_never_replayed(unknown_field):
    facts = DurabilityFacts(
        mutation_applied=True,
        document=DurabilityState.UNKNOWN if unknown_field == "document" else DurabilityState.DURABLE,
        oplog=DurabilityState.UNKNOWN if unknown_field == "oplog" else DurabilityState.NOT_DURABLE,
    )
    stage = CommitStage.DOCUMENT if unknown_field == "document" else CommitStage.OPLOG
    failure = _failure(stage, facts)
    assert failure.status is CommitStatus.INDETERMINATE
    assert failure.retry_safe is False


def test_committed_receipt_requires_durable_document_and_oplog():
    with pytest.raises(ValueError, match="durable document and oplog"):
        CommitReceipt(
            commit_id="c", document_id="d", operation_ids=("o",), trace_id="t",
            save_policy=SavePolicy.AUTO_COMMIT, status=CommitStatus.COMMITTED,
            event_status=EventStatus.NOT_REQUIRED, stage=CommitStage.OPLOG,
            retry_safe=False, document_version=None,
            document_durable=False, oplog_durable=False,
        )
