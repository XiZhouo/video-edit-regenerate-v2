"""Pure worker execution carrier tests."""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

import pytest


_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from business_transaction_contract import AuthorityBinding, freeze_json_payload  # noqa: E402
from worker_execution_contract import (  # noqa: E402
    EMPTY_WORKER_LEDGER_HEAD,
    PipelineBusinessControlIntentBinding,
    PipelineBusinessControlOperation,
    PipelineBusinessControlReceiptEvidence,
    PipelineBusinessControlRequest,
    WorkerExecutionGenericReceiptEvidence,
    WorkerExecutionReceiptEvidence,
    WorkerAttemptStart,
    WorkerBeforeState,
    WorkerClaimRequest,
    WorkerExecutionContractError,
    WorkerExecutionOperation,
    WorkerExecutionStatus,
    WorkerIdempotencyPolicy,
    WorkerLedgerHead,
    WorkerOutboxDispatchCursor,
    WorkerOutboxDispatchFence,
    WorkerOutboxDispatchState,
    WorkerOutboxOrder,
    WorkerReplayPolicy,
    WorkerRequestHeader,
    WorkerTarget,
    WorkerTargetKind,
)


T0 = "2026-07-12T00:00:00.000Z"
T1 = "2026-07-12T00:00:10.000Z"


def _authority() -> AuthorityBinding:
    return AuthorityBinding(
        authority="sqlite",
        authority_state="active",
        authority_epoch="e" * 64,
        source_admission_revision=7,
        authority_revision=11,
        control_revision=13,
        cutover_id="c" * 64,
        runtime_generation="runtime-1",
        code_digest=b"d" * 32,
    )


def _target(kind: WorkerTargetKind) -> WorkerTarget:
    replay = {
        WorkerTargetKind.PIPELINE: WorkerReplayPolicy.EXACT_CAS_RETRY,
        WorkerTargetKind.ACTION: WorkerReplayPolicy.PROVIDER_IDEMPOTENT,
        WorkerTargetKind.OUTBOX: WorkerReplayPolicy.RECONCILE_BEFORE_RETRY,
    }[kind]
    idempotency = {
        WorkerTargetKind.PIPELINE: WorkerIdempotencyPolicy.DURABLE_RECEIPT_RECONCILE,
        WorkerTargetKind.ACTION: WorkerIdempotencyPolicy.REQUIRED_EXTERNAL_KEY,
        WorkerTargetKind.OUTBOX: WorkerIdempotencyPolicy.DURABLE_RECEIPT_RECONCILE,
    }[kind]
    return WorkerTarget(
        target_kind=kind,
        target_id={
            WorkerTargetKind.PIPELINE: "1" * 32,
            WorkerTargetKind.ACTION: "2" * 32,
            WorkerTargetKind.OUTBOX: "3" * 32,
        }[kind],
        project_id="project-1",
        generation=2,
        document_id="document-1",
        origin_commit_id="b" * 32,
        payload=freeze_json_payload(
            {"kind": kind.value, "input": [1, True]},
            schema="test.worker-target.v1",
        ),
        replay_policy=replay,
        idempotency_policy=idempotency,
        idempotency_scope=(
            "pipeline/project-1/2/pipeline-1"
            if kind is WorkerTargetKind.PIPELINE
            else "project/project-1"
        ),
        idempotency_key="external-key-1",
        max_attempts=3,
        pipeline_id=(
            "pipeline-1" if kind is WorkerTargetKind.PIPELINE else None
        ),
        intent_type=(
            "generate" if kind is WorkerTargetKind.PIPELINE else None
        ),
        outbox_order=(
            WorkerOutboxOrder(1, 7, 0)
            if kind is WorkerTargetKind.OUTBOX
            else None
        ),
    )


def _before(kind: WorkerTargetKind) -> WorkerBeforeState:
    status = (
        WorkerExecutionStatus.PENDING
        if kind is WorkerTargetKind.OUTBOX
        else WorkerExecutionStatus.QUEUED
    )
    return WorkerBeforeState(
        status=status,
        state_revision=1,
        attempt_count=0,
        last_attempt_id=None,
        last_attempt_number=None,
        current_attempt_id=None,
        current_attempt_number=None,
        lease_owner=None,
        lease_fence=None,
        lease_expires_at=None,
        fence_high_water=0,
    )


@pytest.mark.parametrize("kind", tuple(WorkerTargetKind))
def test_target_kinds_are_frozen_and_bind_payload_and_policies(kind) -> None:
    target = _target(kind)
    assert target.target_kind is kind
    assert target.payload.payload_sha256
    with pytest.raises(dataclasses.FrozenInstanceError):
        target.target_id = "f" * 32


def test_target_rejects_cross_kind_replay_and_inconsistent_single_attempt() -> None:
    with pytest.raises(WorkerExecutionContractError) as rejected:
        dataclasses.replace(
            _target(WorkerTargetKind.PIPELINE),
            replay_policy=WorkerReplayPolicy.PROVIDER_IDEMPOTENT,
        )
    assert rejected.value.code == "worker_execution_replay_policy_invalid"
    with pytest.raises(WorkerExecutionContractError) as rejected:
        dataclasses.replace(
            _target(WorkerTargetKind.ACTION),
            idempotency_policy=WorkerIdempotencyPolicy.SINGLE_ATTEMPT,
        )
    assert rejected.value.code == "worker_execution_policy_pair_invalid"


def test_before_state_enforces_exact_attempt_lease_and_effect_pointer_shape() -> None:
    active = WorkerBeforeState(
        status=WorkerExecutionStatus.RUNNING,
        state_revision=2,
        attempt_count=1,
        last_attempt_id=None,
        last_attempt_number=None,
        current_attempt_id="a" * 32,
        current_attempt_number=1,
        lease_owner="worker-1",
        lease_fence=1,
        lease_expires_at=T1,
        fence_high_water=1,
    )
    assert active.current_attempt_number == active.attempt_count
    with pytest.raises(WorkerExecutionContractError) as rejected:
        dataclasses.replace(active, lease_fence=2)
    assert rejected.value.code == "worker_execution_attempt_pointer_invalid"


def test_claim_request_is_typed_frozen_and_contains_no_io_or_callback_surface() -> None:
    kind = WorkerTargetKind.PIPELINE
    header = WorkerRequestHeader(
        request_transaction_id="1" * 32,
        authority=_authority(),
        target=_target(kind),
        before=_before(kind),
        occurred_at=T0,
    )
    request = WorkerClaimRequest(
        header=header,
        attempt=WorkerAttemptStart(
            attempt_id="a" * 32,
            attempt_number=1,
            previous_attempt_id=None,
            previous_attempt_number=None,
            lease_owner="worker-1",
            execution_fence=1,
            started_at=T0,
            lease_expires_at=T1,
        ),
    )
    assert request.attempt.execution_fence == 1
    assert not hasattr(request, "connection")
    assert not hasattr(request, "path")
    assert not hasattr(request, "callback")
    with pytest.raises(dataclasses.FrozenInstanceError):
        request.attempt = object()


def test_timestamp_and_attempt_chain_fail_closed() -> None:
    with pytest.raises(WorkerExecutionContractError):
        dataclasses.replace(_before(WorkerTargetKind.ACTION), lease_expires_at="now")
    with pytest.raises(WorkerExecutionContractError) as rejected:
        WorkerAttemptStart(
            attempt_id="a" * 32,
            attempt_number=2,
            previous_attempt_id=None,
            previous_attempt_number=None,
            lease_owner="worker-1",
            execution_fence=2,
            started_at=T0,
            lease_expires_at=T1,
        )
    assert rejected.value.code == "worker_execution_attempt_chain_invalid"


def test_before_retry_time_is_an_authoritative_state_field() -> None:
    before = _before(WorkerTargetKind.ACTION)
    with pytest.raises(WorkerExecutionContractError) as rejected:
        dataclasses.replace(before, status=WorkerExecutionStatus.RETRY)
    assert rejected.value.code == "worker_execution_retry_invalid"
    retry = dataclasses.replace(
        before,
        status=WorkerExecutionStatus.RETRY,
        next_attempt_at=T1,
    )
    assert retry.next_attempt_at == T1
    with pytest.raises(WorkerExecutionContractError) as rejected:
        dataclasses.replace(before, next_attempt_at=T1)
    assert rejected.value.code == "worker_execution_retry_invalid"


def test_cancellation_latch_survives_only_unresolved_pipeline_states() -> None:
    active = WorkerBeforeState(
        status=WorkerExecutionStatus.CANCEL_REQUESTED,
        state_revision=3,
        attempt_count=1,
        last_attempt_id=None,
        last_attempt_number=None,
        current_attempt_id="a" * 32,
        current_attempt_number=1,
        lease_owner="worker-1",
        lease_fence=1,
        lease_expires_at=T1,
        fence_high_water=1,
        cancellation_pending=True,
    )
    assert active.cancellation_pending is True
    with pytest.raises(WorkerExecutionContractError) as rejected:
        dataclasses.replace(active, cancellation_pending=False)
    assert rejected.value.code == "worker_execution_cancellation_invalid"
    with pytest.raises(WorkerExecutionContractError) as rejected:
        dataclasses.replace(
            _before(WorkerTargetKind.PIPELINE),
            cancellation_pending=True,
        )
    assert rejected.value.code == "worker_execution_cancellation_invalid"


def test_outbox_order_and_dispatch_cursor_shapes_are_target_specific() -> None:
    assert _target(WorkerTargetKind.OUTBOX).outbox_order == WorkerOutboxOrder(1, 7, 0)
    with pytest.raises(WorkerExecutionContractError) as rejected:
        dataclasses.replace(_target(WorkerTargetKind.OUTBOX), outbox_order=None)
    assert rejected.value.code == "worker_execution_outbox_order_required"
    with pytest.raises(WorkerExecutionContractError) as rejected:
        dataclasses.replace(
            _target(WorkerTargetKind.ACTION),
            outbox_order=WorkerOutboxOrder(1, 7, 0),
        )
    assert rejected.value.code == "worker_execution_outbox_order_invalid"

    published = WorkerOutboxDispatchCursor(0, 0, -1)
    target = WorkerOutboxOrder(1, 7, 0)
    fence = WorkerOutboxDispatchFence(
        WorkerOutboxDispatchState(1, published, None),
        WorkerOutboxDispatchState(2, published, target),
    )
    assert fence.after.inflight == target
    assert fence.after.published == fence.before.published
    with pytest.raises(WorkerExecutionContractError) as rejected:
        WorkerOutboxDispatchState(1, published, WorkerOutboxOrder(2, 7, 1))
    assert rejected.value.code == "worker_execution_dispatch_fence_invalid"
    with pytest.raises(WorkerExecutionContractError) as rejected:
        WorkerOutboxDispatchCursor(0, 1, -1)
    assert rejected.value.code == "worker_execution_dispatch_fence_invalid"
    with pytest.raises(WorkerExecutionContractError) as rejected:
        WorkerOutboxDispatchState(1, published, WorkerOutboxOrder(1, 9, 1))
    assert rejected.value.code == "worker_execution_dispatch_fence_invalid"


def test_pipeline_target_binds_composite_identity_without_payload_guessing() -> None:
    target = _target(WorkerTargetKind.PIPELINE)
    assert target.pipeline_id == "pipeline-1"
    assert target.intent_type == "generate"
    assert target.idempotency_scope == "pipeline/project-1/2/pipeline-1"
    assert target.idempotency_policy is (
        WorkerIdempotencyPolicy.DURABLE_RECEIPT_RECONCILE
    )
    for field, value in (
        ("pipeline_id", None),
        ("intent_type", None),
        ("idempotency_scope", "provider/account-1"),
    ):
        with pytest.raises(WorkerExecutionContractError):
            dataclasses.replace(target, **{field: value})
    with pytest.raises(WorkerExecutionContractError):
        dataclasses.replace(
            _target(WorkerTargetKind.ACTION),
            pipeline_id="pipeline-1",
            intent_type="generate",
        )

    single_attempt = dataclasses.replace(
        target,
        replay_policy=WorkerReplayPolicy.NEVER_REPLAY,
        idempotency_policy=WorkerIdempotencyPolicy.SINGLE_ATTEMPT,
        max_attempts=1,
    )
    assert single_attempt.idempotency_scope == target.idempotency_scope
    assert single_attempt.idempotency_policy is (
        WorkerIdempotencyPolicy.SINGLE_ATTEMPT
    )
    with pytest.raises(WorkerExecutionContractError):
        dataclasses.replace(
            single_attempt,
            idempotency_policy=WorkerIdempotencyPolicy.DURABLE_RECEIPT_RECONCILE,
        )
    with pytest.raises(WorkerExecutionContractError):
        dataclasses.replace(
            target,
            idempotency_policy=WorkerIdempotencyPolicy.SINGLE_ATTEMPT,
        )


def test_worker_ledger_and_receipt_carriers_are_closed_and_domain_specific() -> None:
    assert EMPTY_WORKER_LEDGER_HEAD.sequence == 0
    assert EMPTY_WORKER_LEDGER_HEAD.tail_sha256 == b"\0" * 32
    with pytest.raises(WorkerExecutionContractError):
        WorkerLedgerHead(0, b"x" * 32)

    parent = WorkerExecutionGenericReceiptEvidence(
        "_worker-execution:" + "1" * 32,
        "_worker_execution_v3",
        T0,
    )
    receipt = WorkerExecutionReceiptEvidence(
        request_transaction_id="1" * 32,
        receipt_parent=parent,
        request_class="worker_execution",
        operation=WorkerExecutionOperation.CLAIM,
        target_kind=WorkerTargetKind.PIPELINE,
        target_id="1" * 32,
        before_status=WorkerExecutionStatus.QUEUED,
        after_status=WorkerExecutionStatus.RUNNING,
        before_state_revision=1,
        after_state_revision=2,
        sequence=1,
        previous_ledger_tail_sha256=EMPTY_WORKER_LEDGER_HEAD.tail_sha256,
        ledger_tail_sha256=b"t" * 32,
        request_sha256=b"r" * 32,
        after_state_sha256=b"a" * 32,
        occurred_at=T0,
        receipt_sha256=b"q" * 32,
    )
    assert receipt.receipt_parent is parent
    assert receipt.sequence == 1
    assert receipt.previous_ledger_tail_sha256 == b"\0" * 32
    with pytest.raises(WorkerExecutionContractError):
        dataclasses.replace(receipt, before_status=None)

    with pytest.raises(WorkerExecutionContractError):
        PipelineBusinessControlReceiptEvidence(
            request_transaction_id="1" * 32,
            receipt_parent=parent,
            request_class="pipeline_business_control",
            operation=PipelineBusinessControlOperation.CREATE,
            target_kind=WorkerTargetKind.PIPELINE,
            target_id="1" * 32,
            before_status=None,
            after_status=WorkerExecutionStatus.QUEUED,
            before_state_revision=None,
            after_state_revision=1,
            sequence=1,
            previous_ledger_tail_sha256=EMPTY_WORKER_LEDGER_HEAD.tail_sha256,
            ledger_tail_sha256=b"t" * 32,
            request_sha256=b"r" * 32,
            after_state_sha256=b"a" * 32,
            occurred_at=T0,
            receipt_sha256=b"q" * 32,
        )

    control_parent = WorkerExecutionGenericReceiptEvidence(
        "_pipeline-business-control:" + "1" * 32,
        "_pipeline_business_control_v2",
        T0,
    )
    control_receipt = PipelineBusinessControlReceiptEvidence(
        request_transaction_id="1" * 32,
        receipt_parent=control_parent,
        request_class="pipeline_business_control",
        operation=PipelineBusinessControlOperation.CREATE,
        target_kind=WorkerTargetKind.PIPELINE,
        target_id="1" * 32,
        before_status=None,
        after_status=WorkerExecutionStatus.QUEUED,
        before_state_revision=None,
        after_state_revision=1,
        sequence=1,
        previous_ledger_tail_sha256=EMPTY_WORKER_LEDGER_HEAD.tail_sha256,
        ledger_tail_sha256=b"t" * 32,
        request_sha256=b"r" * 32,
        after_state_sha256=b"a" * 32,
        occurred_at=T0,
        receipt_sha256=b"q" * 32,
    )
    with pytest.raises(WorkerExecutionContractError):
        dataclasses.replace(
            control_receipt,
            before_status=WorkerExecutionStatus.QUEUED,
            before_state_revision=1,
            after_state_revision=2,
        )
    with pytest.raises(WorkerExecutionContractError):
        dataclasses.replace(
            control_receipt,
            operation=PipelineBusinessControlOperation.APPLY_CONTROL_INTENT,
        )
    transition_receipt = dataclasses.replace(
        control_receipt,
        operation=PipelineBusinessControlOperation.APPLY_CONTROL_INTENT,
        before_status=WorkerExecutionStatus.QUEUED,
        after_status=WorkerExecutionStatus.PAUSED,
        before_state_revision=1,
        after_state_revision=2,
    )
    assert transition_receipt.before_state_revision == 1


def test_pipeline_business_control_request_is_not_a_worker_request_shape() -> None:
    target = _target(WorkerTargetKind.PIPELINE)
    control_intent = PipelineBusinessControlIntentBinding(
        origin_commit_id=target.origin_commit_id,
        intent_type=target.intent_type,
        idempotency_key=target.idempotency_key,
        payload=target.payload,
        replay_policy=target.replay_policy,
        idempotency_policy=target.idempotency_policy,
        max_attempts=target.max_attempts,
    )
    request = PipelineBusinessControlRequest(
        request_transaction_id="8" * 32,
        authority=_authority(),
        target=target,
        control_intent=control_intent,
        operation=PipelineBusinessControlOperation.CREATE,
        before=None,
        target_status=WorkerExecutionStatus.QUEUED,
        occurred_at=T0,
    )
    assert request.operation is PipelineBusinessControlOperation.CREATE
    assert not hasattr(request, "attempt")
    assert not hasattr(request, "effect")
    assert not hasattr(request, "proof")
    with pytest.raises(WorkerExecutionContractError) as rejected:
        dataclasses.replace(request, target_status=WorkerExecutionStatus.SUCCEEDED)
    assert rejected.value.code == "pipeline_business_control_create_invalid"
    with pytest.raises(WorkerExecutionContractError) as rejected:
        dataclasses.replace(request, target=_target(WorkerTargetKind.ACTION))
    assert rejected.value.code == "worker_execution_target_invalid"
    with pytest.raises(WorkerExecutionContractError) as rejected:
        dataclasses.replace(
            request,
            control_intent=dataclasses.replace(
                control_intent,
                idempotency_key="different-create-key",
            ),
        )
    assert rejected.value.code == "pipeline_business_control_create_invalid"

    transition = dataclasses.replace(
        request,
        control_intent=dataclasses.replace(
            control_intent,
            origin_commit_id="c" * 32,
            intent_type="pause",
            idempotency_key="pause-request-1",
        ),
        operation=PipelineBusinessControlOperation.APPLY_CONTROL_INTENT,
        before=_before(WorkerTargetKind.PIPELINE),
        target_status=WorkerExecutionStatus.PAUSED,
        occurred_at=T1,
    )
    assert transition.target.idempotency_key == "external-key-1"
    assert transition.control_intent.idempotency_key == "pause-request-1"
