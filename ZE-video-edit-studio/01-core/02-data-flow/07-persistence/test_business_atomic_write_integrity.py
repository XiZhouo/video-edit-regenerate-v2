from __future__ import annotations

import ast
import dataclasses
from pathlib import Path
import sys

import pytest


_HERE = Path(__file__).resolve().parent
_DATA_STRUCTURE = _HERE.parents[1] / "01-data-structure"
for _path in (
    _HERE,
    _DATA_STRUCTURE / "01-document",
    _DATA_STRUCTURE / "02-entity",
    _DATA_STRUCTURE / "04-field-types",
):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import business_atomic_write_contract as contract_module
import business_atomic_write_integrity as integrity_module
from business_atomic_write_integrity import (
    ATOMIC_BUSINESS_WRITE_RECEIPT_HASH_DOMAIN,
    AtomicBusinessWritePlan,
    AtomicBusinessWriteIntegrityError,
    _classify_atomic_business_write_receipt,
    _issue_atomic_business_write_plan,
    _require_atomic_business_write_plan,
)
from business_atomic_write_contract import (
    AtomicBusinessWriteContractError,
    AtomicBusinessWriteDurableReaderCapability,
    AtomicBusinessWriteFreshObservation,
    AtomicBusinessWritePersistedObservation,
    AtomicBusinessWritePersistedReadStatus,
    AtomicBusinessWriteReconcileStatus,
    AtomicBusinessWriteRetryAction,
    _ATOMIC_BUSINESS_WRITE_DURABLE_READER_CAPABILITY,
    _stamp_atomic_business_write_fresh_observation,
)
from business_transaction_contract import (
    PipelineIntent,
    PipelineIntentKind,
    PipelineJobStatus,
    PipelineReplayPolicy,
    freeze_json_payload,
)
from business_transaction_integrity import _issue_atomic_commit_plan
from test_business_transaction_integrity import _parts
from worker_execution_contract import (
    EMPTY_WORKER_LEDGER_HEAD,
    PipelineBusinessControlIntentBinding,
    PipelineBusinessControlOperation,
    PipelineBusinessControlRequest,
    WorkerBeforeState,
    WorkerExecutionStatus,
    WorkerIdempotencyPolicy,
    WorkerLedgerHead,
    WorkerLedgerMembershipEvidence,
    WorkerReplayPolicy,
    WorkerTarget,
    WorkerTargetKind,
)
from worker_execution_integrity import _issue_pipeline_business_control_plan


COMMITTED_AT = "2026-07-12T01:02:03.004Z"


def _atomic(*, intent=None):
    parts = _parts()
    if intent is not None:
        parts["pipeline_intents"] = (intent,)
    return _issue_atomic_commit_plan(**parts)


def _idempotency_policy(replay):
    return (
        WorkerIdempotencyPolicy.SINGLE_ATTEMPT
        if replay is WorkerReplayPolicy.NEVER_REPLAY
        else WorkerIdempotencyPolicy.DURABLE_RECEIPT_RECONCILE
    )


def _target(atomic, intent, *, origin_commit_id=None, payload=None, key=None):
    static = (
        intent
        if intent.intent_kind is PipelineIntentKind.CREATE
        else _parts()["pipeline_intents"][0]
    )
    replay = WorkerReplayPolicy(static.replay_policy.value)
    policy = (
        _idempotency_policy(replay)
    )
    return WorkerTarget(
        target_kind=WorkerTargetKind.PIPELINE,
        target_id=intent.job_id,
        project_id=atomic.before.project_id,
        generation=atomic.before.generation,
        document_id=atomic.before.document_id,
        origin_commit_id=(
            (
                atomic.commit_id
                if intent.intent_kind is PipelineIntentKind.CREATE
                else "f" * 32
            )
            if origin_commit_id is None
            else origin_commit_id
        ),
        payload=static.payload if payload is None else payload,
        replay_policy=replay,
        idempotency_policy=policy,
        idempotency_scope="pipeline/%s/%d/%s" % (
            atomic.before.project_id,
            atomic.before.generation,
            intent.pipeline_id,
        ),
        idempotency_key=static.idempotency_key if key is None else key,
        max_attempts=static.max_attempts,
        pipeline_id=intent.pipeline_id,
        intent_type=static.intent_type,
    )


def _control_intent(atomic, intent):
    replay = WorkerReplayPolicy(intent.replay_policy.value)
    return PipelineBusinessControlIntentBinding(
        origin_commit_id=atomic.commit_id,
        intent_type=intent.intent_type,
        idempotency_key=intent.idempotency_key,
        payload=intent.payload,
        replay_policy=replay,
        idempotency_policy=_idempotency_policy(replay),
        max_attempts=intent.max_attempts,
    )


def _control(atomic, intent, *, before_ledger=EMPTY_WORKER_LEDGER_HEAD, target=None):
    if intent.intent_kind is PipelineIntentKind.CREATE:
        operation = PipelineBusinessControlOperation.CREATE
        before = None
    else:
        operation = PipelineBusinessControlOperation.APPLY_CONTROL_INTENT
        if intent.expected_status is PipelineJobStatus.RUNNING:
            before = WorkerBeforeState(
                status=WorkerExecutionStatus.RUNNING,
                state_revision=intent.expected_state_revision,
                attempt_count=1,
                last_attempt_id=None,
                last_attempt_number=None,
                current_attempt_id="a" * 32,
                current_attempt_number=1,
                lease_owner="worker-1",
                lease_fence=1,
                lease_expires_at="2026-07-12T02:02:03.004Z",
                fence_high_water=1,
            )
        else:
            before = WorkerBeforeState(
                status=WorkerExecutionStatus(intent.expected_status.value),
                state_revision=intent.expected_state_revision,
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
    exact_target = _target(atomic, intent) if target is None else target
    request = PipelineBusinessControlRequest(
        request_transaction_id=intent.intent_id,
        authority=atomic.authority,
        target=exact_target,
        control_intent=_control_intent(atomic, intent),
        operation=operation,
        before=before,
        target_status=WorkerExecutionStatus(intent.target_status.value),
        occurred_at=COMMITTED_AT,
    )
    return _issue_pipeline_business_control_plan(
        request,
        before_ledger=before_ledger,
    )


def _fresh_atomic_observation(
    persisted: AtomicBusinessWritePersistedObservation,
) -> AtomicBusinessWriteFreshObservation:
    return _stamp_atomic_business_write_fresh_observation(
        _ATOMIC_BUSINESS_WRITE_DURABLE_READER_CAPABILITY,
        persisted,
    )


def _one_control_write():
    atomic = _atomic()
    control = _control(atomic, atomic.pipeline_intents[0])
    return _issue_atomic_business_write_plan(
        atomic_plan=atomic,
        control_plans=(control,),
        committed_at=COMMITTED_AT,
    )


def test_create_control_is_one_to_one_and_has_a_fixed_vector() -> None:
    atomic = _atomic()
    intent = atomic.pipeline_intents[0]
    control = _control(atomic, intent)
    plan = _issue_atomic_business_write_plan(
        atomic_plan=atomic,
        control_plans=(control,),
        committed_at=COMMITTED_AT,
    )
    assert _require_atomic_business_write_plan(plan) is plan
    assert plan.evidence.before_worker_ledger == EMPTY_WORKER_LEDGER_HEAD
    assert plan.evidence.after_worker_ledger == control.after_ledger
    assert plan.evidence.controls[0].intent_id == intent.intent_id
    assert plan.evidence.controls[0].receipt_sha256 == (
        control.expected_receipt.receipt_sha256
    )
    assert len(plan.evidence.manifest_bytes) == 1175
    assert plan.evidence.controls_sha256.hex() == (
        "012a4ef167f913c87bf37acf01013ade3ece40e51ef02653a65a69c09300ba22"
    )
    assert plan.evidence.manifest_sha256.hex() == (
        "a54cfe8a0d5a246ce3cfb9c73e52088bc713994205b03447373f18ce7d7c9bcb"
    )
    assert ATOMIC_BUSINESS_WRITE_RECEIPT_HASH_DOMAIN == (
        b"ze.atomic-business-write.receipt.v1"
    )
    assert plan.expected_receipt.receipt_parent.transaction_id == (
        "ze-business:" + atomic.commit_id
    )
    assert plan.expected_receipt.receipt_parent.kind == (
        "ze_business_commit_v1"
    )
    assert plan.expected_receipt.control_count == 1
    assert plan.expected_receipt.before_worker_ledger == EMPTY_WORKER_LEDGER_HEAD
    assert plan.expected_receipt.after_worker_ledger == control.after_ledger
    assert plan.expected_receipt.receipt_sha256.hex() == (
        "c6349aeb518b49c98699ebe264ca885c84594eeea65518433ac2d5bcf21fc63b"
    )
    assert control.closure.request_sha256.hex() == (
        "a5d41049dbb3a8cd70af6735cfabffa1678ffaf9a4e820e2f914351c8b87bb49"
    )
    assert control.expected_receipt.receipt_sha256.hex() == (
        "234464c24a5c998216c69f94036b962e2c1478b7f063253dc73dda96655ec2fb"
    )
    assert control.expected_receipt.ledger_tail_sha256.hex() == (
        "8daea5f26bffc32592942b0d196daf672a40274b854fe99da4bb1f1a8f011008"
    )


def test_zero_pipeline_intents_keep_the_exact_supplied_ledger() -> None:
    parts = _parts()
    parts["pipeline_intents"] = ()
    atomic = _issue_atomic_commit_plan(**parts)
    before = WorkerLedgerHead(7, b"x" * 32)
    plan = _issue_atomic_business_write_plan(
        atomic_plan=atomic,
        control_plans=(),
        committed_at=COMMITTED_AT,
        before_worker_ledger=before,
    )
    assert plan.evidence.controls == ()
    assert plan.evidence.before_worker_ledger == before
    assert plan.evidence.after_worker_ledger == before

    corrupted = WorkerLedgerHead(7, b"x" * 32)
    object.__setattr__(corrupted, "tail_sha256", b"\0" * 32)
    with pytest.raises(AtomicBusinessWriteIntegrityError) as rejected:
        _issue_atomic_business_write_plan(
            atomic_plan=atomic,
            control_plans=(),
            committed_at=COMMITTED_AT,
            before_worker_ledger=corrupted,
        )
    assert rejected.value.code == "atomic_business_write_worker_ledger_invalid"


def test_multiple_controls_continue_one_nonempty_worker_ledger_exactly() -> None:
    first_intent = _parts()["pipeline_intents"][0]
    second_intent = dataclasses.replace(
        first_intent,
        ordinal=1,
        intent_id="6" * 32,
        job_id="6" * 32,
        pipeline_id="render-pipeline-2",
        idempotency_key="pipeline-create-2",
    )
    parts = _parts()
    parts["pipeline_intents"] = (first_intent, second_intent)
    atomic = _issue_atomic_commit_plan(**parts)
    before = WorkerLedgerHead(7, b"x" * 32)
    first = _control(atomic, first_intent, before_ledger=before)
    second = _control(
        atomic,
        second_intent,
        before_ledger=first.after_ledger,
    )
    plan = _issue_atomic_business_write_plan(
        atomic_plan=atomic,
        control_plans=(first, second),
        committed_at=COMMITTED_AT,
        before_worker_ledger=before,
    )
    assert plan.evidence.before_worker_ledger == before
    assert first.expected_receipt.sequence == 8
    assert second.expected_receipt.sequence == 9
    assert second.expected_receipt.previous_ledger_tail_sha256 == (
        first.expected_receipt.ledger_tail_sha256
    )
    assert plan.evidence.after_worker_ledger == second.after_ledger

    with pytest.raises(AtomicBusinessWriteIntegrityError):
        _issue_atomic_business_write_plan(
            atomic_plan=atomic,
            control_plans=(second, first),
            committed_at=COMMITTED_AT,
            before_worker_ledger=before,
        )


def test_transition_maps_status_revision_but_preserves_existing_static_target() -> None:
    original = _parts()["pipeline_intents"][0]
    intent = PipelineIntent(
        ordinal=0,
        intent_id="6" * 32,
        job_id=original.job_id,
        pipeline_id=original.pipeline_id,
        intent_type="pause",
        idempotency_key="pause-request-1",
        payload=original.payload,
        intent_kind=PipelineIntentKind.TRANSITION,
        expected_status=PipelineJobStatus.QUEUED,
        target_status=PipelineJobStatus.PAUSED,
        replay_policy=PipelineReplayPolicy.EXACT_CAS_RETRY,
        expected_state_revision=3,
        max_attempts=10,
    )
    atomic = _atomic(intent=intent)
    target = _target(
        atomic,
        intent,
        origin_commit_id="f" * 32,
        key="original-job-key",
    )
    control = _control(atomic, intent, target=target)
    plan = _issue_atomic_business_write_plan(
        atomic_plan=atomic,
        control_plans=(control,),
        committed_at=COMMITTED_AT,
    )
    assert plan.evidence.controls[0].job_id == intent.job_id
    assert control.target.origin_commit_id == "f" * 32
    assert control.target.intent_type == "create"
    assert control.target.idempotency_key == "original-job-key"
    assert control.control_intent.origin_commit_id == atomic.commit_id
    assert control.control_intent.intent_type == "pause"
    assert control.control_intent.idempotency_key == "pause-request-1"


@pytest.mark.parametrize(
    ("expected_status", "target_status"),
    (
        (PipelineJobStatus.QUEUED, PipelineJobStatus.PAUSED),
        (PipelineJobStatus.QUEUED, PipelineJobStatus.CANCELLED),
        (PipelineJobStatus.RUNNING, PipelineJobStatus.CANCEL_REQUESTED),
        (PipelineJobStatus.PAUSED, PipelineJobStatus.QUEUED),
        (PipelineJobStatus.PAUSED, PipelineJobStatus.CANCELLED),
    ),
)
def test_atomic_and_worker_control_share_one_transition_matrix(
    expected_status,
    target_status,
) -> None:
    original = _parts()["pipeline_intents"][0]
    intent = PipelineIntent(
        ordinal=0,
        intent_id="6" * 32,
        job_id=original.job_id,
        pipeline_id=original.pipeline_id,
        intent_type="control",
        idempotency_key="control-request-1",
        payload=original.payload,
        intent_kind=PipelineIntentKind.TRANSITION,
        expected_status=expected_status,
        target_status=target_status,
        expected_state_revision=3,
    )
    atomic = _atomic(intent=intent)
    control = _control(atomic, intent)
    plan = _issue_atomic_business_write_plan(
        atomic_plan=atomic,
        control_plans=(control,),
        committed_at=COMMITTED_AT,
    )
    assert plan.control_plans[0].closure.before_status.value == (
        expected_status.value
    )
    assert plan.control_plans[0].closure.after_status.value == (
        target_status.value
    )


def test_transition_control_intent_is_fully_bound_but_job_static_is_preserved() -> None:
    original = _parts()["pipeline_intents"][0]
    intent = PipelineIntent(
        ordinal=0,
        intent_id="6" * 32,
        job_id=original.job_id,
        pipeline_id=original.pipeline_id,
        intent_type="pause",
        idempotency_key="pause-request-1",
        payload=freeze_json_payload(
            {"reason": "operator"},
            schema="test.pipeline-control.v1",
        ),
        intent_kind=PipelineIntentKind.TRANSITION,
        expected_status=PipelineJobStatus.QUEUED,
        target_status=PipelineJobStatus.PAUSED,
        replay_policy=PipelineReplayPolicy.NEVER_REPLAY,
        expected_state_revision=3,
        max_attempts=1,
    )
    atomic = _atomic(intent=intent)
    control = _control(atomic, intent)
    assert control.target.replay_policy is WorkerReplayPolicy.EXACT_CAS_RETRY
    assert control.target.max_attempts == 10
    assert control.target.payload == original.payload
    assert control.control_intent.replay_policy is WorkerReplayPolicy.NEVER_REPLAY
    assert control.control_intent.max_attempts == 1
    assert control.control_intent.payload == intent.payload
    assert _issue_atomic_business_write_plan(
        atomic_plan=atomic,
        control_plans=(control,),
        committed_at=COMMITTED_AT,
    )

    mutations = (
        {"origin_commit_id": "c" * 32},
        {"idempotency_key": "different-control-key"},
        {
            "payload": freeze_json_payload(
                {"reason": "different"},
                schema="test.pipeline-control.v1",
            )
        },
        {
            "replay_policy": WorkerReplayPolicy.EXACT_CAS_RETRY,
            "idempotency_policy": (
                WorkerIdempotencyPolicy.DURABLE_RECEIPT_RECONCILE
            ),
            "max_attempts": 10,
        },
    )
    for index, mutation in enumerate(mutations, start=1):
        changed_request = dataclasses.replace(
            control.request,
            request_transaction_id=str(index) * 32,
            control_intent=dataclasses.replace(
                control.control_intent,
                **mutation,
            ),
        )
        changed_control = _issue_pipeline_business_control_plan(
            changed_request
        )
        changed_intent = dataclasses.replace(
            intent,
            intent_id=str(index) * 32,
        )
        changed_atomic = _atomic(intent=changed_intent)
        with pytest.raises(AtomicBusinessWriteIntegrityError) as rejected:
            _issue_atomic_business_write_plan(
                atomic_plan=changed_atomic,
                control_plans=(changed_control,),
                committed_at=COMMITTED_AT,
            )
        assert rejected.value.code == "atomic_business_write_control_mapping_invalid"


def test_never_replay_create_maps_to_single_attempt() -> None:
    original = _parts()["pipeline_intents"][0]
    intent = dataclasses.replace(
        original,
        replay_policy=PipelineReplayPolicy.NEVER_REPLAY,
        max_attempts=1,
    )
    atomic = _atomic(intent=intent)
    control = _control(atomic, intent)
    assert control.target.idempotency_policy is WorkerIdempotencyPolicy.SINGLE_ATTEMPT
    assert _issue_atomic_business_write_plan(
        atomic_plan=atomic,
        control_plans=(control,),
        committed_at=COMMITTED_AT,
    )


def test_missing_extra_wrong_target_and_stale_ledger_are_rejected() -> None:
    atomic = _atomic()
    intent = atomic.pipeline_intents[0]
    control = _control(atomic, intent)
    with pytest.raises(AtomicBusinessWriteIntegrityError) as rejected:
        _issue_atomic_business_write_plan(
            atomic_plan=atomic,
            control_plans=(),
            committed_at=COMMITTED_AT,
        )
    assert rejected.value.code == "atomic_business_write_control_count_invalid"

    wrong_target = dataclasses.replace(
        _target(atomic, intent),
        target_id="7" * 32,
    )
    wrong = _control(atomic, intent, target=wrong_target)
    with pytest.raises(AtomicBusinessWriteIntegrityError) as rejected:
        _issue_atomic_business_write_plan(
            atomic_plan=atomic,
            control_plans=(wrong,),
            committed_at=COMMITTED_AT,
        )
    assert rejected.value.code == "atomic_business_write_control_mapping_invalid"

    with pytest.raises(AtomicBusinessWriteIntegrityError) as rejected:
        _issue_atomic_business_write_plan(
            atomic_plan=atomic,
            control_plans=(control,),
            committed_at=COMMITTED_AT,
            before_worker_ledger=WorkerLedgerHead(1, b"q" * 32),
        )
    assert rejected.value.code == "atomic_business_write_worker_ledger_invalid"


def test_control_plan_and_composite_evidence_deep_tamper_fail_closed() -> None:
    atomic = _atomic()
    control = _control(atomic, atomic.pipeline_intents[0])
    plan = _issue_atomic_business_write_plan(
        atomic_plan=atomic,
        control_plans=(control,),
        committed_at=COMMITTED_AT,
    )
    object.__setattr__(control.expected_receipt, "receipt_sha256", b"z" * 32)
    with pytest.raises(AtomicBusinessWriteIntegrityError):
        _require_atomic_business_write_plan(plan)

    fresh_atomic = _atomic()
    fresh = _issue_atomic_business_write_plan(
        atomic_plan=fresh_atomic,
        control_plans=(_control(fresh_atomic, fresh_atomic.pipeline_intents[0]),),
        committed_at=COMMITTED_AT,
    )
    object.__setattr__(fresh.evidence, "manifest_sha256", b"z" * 32)
    with pytest.raises(AtomicBusinessWriteIntegrityError):
        _require_atomic_business_write_plan(fresh)

    tail_atomic = _atomic()
    tail_control = _control(
        tail_atomic,
        tail_atomic.pipeline_intents[0],
    )
    tail_plan = _issue_atomic_business_write_plan(
        atomic_plan=tail_atomic,
        control_plans=(tail_control,),
        committed_at=COMMITTED_AT,
    )
    object.__setattr__(
        tail_control.expected_receipt,
        "previous_ledger_tail_sha256",
        b"q" * 32,
    )
    with pytest.raises(AtomicBusinessWriteIntegrityError):
        _require_atomic_business_write_plan(tail_plan)

    forged = AtomicBusinessWritePlan.__new__(AtomicBusinessWritePlan)
    with pytest.raises(AtomicBusinessWriteIntegrityError) as rejected:
        _require_atomic_business_write_plan(forged)
    assert rejected.value.code == "atomic_business_write_plan_invalid"


def test_atomic_receipt_classifier_covers_all_five_retry_states() -> None:
    plan = _one_control_write()

    absent = _classify_atomic_business_write_receipt(
        plan,
        _fresh_atomic_observation(AtomicBusinessWritePersistedObservation(
            AtomicBusinessWritePersistedReadStatus.COMPLETE,
            current_worker_ledger=plan.evidence.before_worker_ledger,
        )),
    )
    assert absent.status is AtomicBusinessWriteReconcileStatus.ABSENT
    assert absent.retry_action is AtomicBusinessWriteRetryAction.APPLY_EXACT

    exact = _classify_atomic_business_write_receipt(
        plan,
        _fresh_atomic_observation(AtomicBusinessWritePersistedObservation(
            AtomicBusinessWritePersistedReadStatus.COMPLETE,
            receipt=plan.expected_receipt.receipt_parent,
            atomic_receipt=plan.expected_receipt,
            control_receipts=tuple(
                value.expected_receipt for value in plan.control_plans
            ),
            current_worker_ledger=plan.evidence.after_worker_ledger,
        )),
    )
    assert exact.status is AtomicBusinessWriteReconcileStatus.EXACT
    assert exact.retry_action is AtomicBusinessWriteRetryAction.RETURN_EXACT
    assert exact.evidence == plan.expected_receipt

    occupied_parent = dataclasses.replace(
        plan.expected_receipt.receipt_parent,
        kind="occupied_by_another_transaction_kind",
    )
    conflict = _classify_atomic_business_write_receipt(
        plan,
        _fresh_atomic_observation(AtomicBusinessWritePersistedObservation(
            AtomicBusinessWritePersistedReadStatus.COMPLETE,
            receipt=occupied_parent,
            current_worker_ledger=plan.evidence.before_worker_ledger,
        )),
    )
    assert conflict.status is AtomicBusinessWriteReconcileStatus.CONFLICT
    assert conflict.retry_action is AtomicBusinessWriteRetryAction.STOP

    changed_digest = dataclasses.replace(
        plan.expected_receipt,
        receipt_sha256=b"z" * 32,
    )
    corrupt = _classify_atomic_business_write_receipt(
        plan,
        _fresh_atomic_observation(AtomicBusinessWritePersistedObservation(
            AtomicBusinessWritePersistedReadStatus.COMPLETE,
            receipt=changed_digest.receipt_parent,
            atomic_receipt=changed_digest,
            control_receipts=tuple(
                value.expected_receipt for value in plan.control_plans
            ),
            current_worker_ledger=plan.evidence.after_worker_ledger,
        )),
    )
    assert corrupt.status is AtomicBusinessWriteReconcileStatus.CORRUPT
    assert corrupt.retry_action is AtomicBusinessWriteRetryAction.STOP

    unknown = _classify_atomic_business_write_receipt(
        plan,
        _fresh_atomic_observation(AtomicBusinessWritePersistedObservation(
            AtomicBusinessWritePersistedReadStatus.UNKNOWN,
            error_code="atomic_business_write_snapshot_unavailable",
        )),
    )
    assert unknown.status is AtomicBusinessWriteReconcileStatus.UNKNOWN
    assert unknown.retry_action is (
        AtomicBusinessWriteRetryAction.RECONCILE_REQUIRED
    )


def test_atomic_receipt_ack_loss_accepts_a_proven_later_worker_ledger() -> None:
    plan = _one_control_write()
    original = _parts()["pipeline_intents"][0]
    later_intent = dataclasses.replace(
        original,
        intent_id="6" * 32,
        job_id="6" * 32,
        pipeline_id="later-pipeline",
        idempotency_key="later-pipeline-create",
    )
    later_atomic = _atomic(intent=later_intent)
    later_control = _control(
        later_atomic,
        later_intent,
        before_ledger=plan.evidence.after_worker_ledger,
    )
    membership = WorkerLedgerMembershipEvidence(
        plan.evidence.after_worker_ledger.sequence,
        plan.evidence.after_worker_ledger.tail_sha256,
        later_control.after_ledger,
    )
    persisted = dict(
        receipt=plan.expected_receipt.receipt_parent,
        atomic_receipt=plan.expected_receipt,
        control_receipts=tuple(
            value.expected_receipt for value in plan.control_plans
        ),
        current_worker_ledger=later_control.after_ledger,
    )

    exact = _classify_atomic_business_write_receipt(
        plan,
        _fresh_atomic_observation(AtomicBusinessWritePersistedObservation(
            AtomicBusinessWritePersistedReadStatus.COMPLETE,
            ledger_membership=membership,
            **persisted,
        )),
    )
    assert exact.status is AtomicBusinessWriteReconcileStatus.EXACT

    missing_membership = _classify_atomic_business_write_receipt(
        plan,
        _fresh_atomic_observation(AtomicBusinessWritePersistedObservation(
            AtomicBusinessWritePersistedReadStatus.COMPLETE,
            **persisted,
        )),
    )
    assert missing_membership.status is AtomicBusinessWriteReconcileStatus.CORRUPT
    assert missing_membership.error_code == (
        "atomic_business_write_ledger_membership_invalid"
    )


def test_zero_control_genesis_write_is_exact_after_the_ledger_advances() -> None:
    parts = _parts()
    parts["pipeline_intents"] = ()
    atomic = _issue_atomic_commit_plan(**parts)
    plan = _issue_atomic_business_write_plan(
        atomic_plan=atomic,
        control_plans=(),
        committed_at=COMMITTED_AT,
    )
    later_head = _one_control_write().evidence.after_worker_ledger

    classified = _classify_atomic_business_write_receipt(
        plan,
        _fresh_atomic_observation(AtomicBusinessWritePersistedObservation(
            AtomicBusinessWritePersistedReadStatus.COMPLETE,
            receipt=plan.expected_receipt.receipt_parent,
            atomic_receipt=plan.expected_receipt,
            current_worker_ledger=later_head,
        )),
    )
    assert plan.evidence.after_worker_ledger == EMPTY_WORKER_LEDGER_HEAD
    assert later_head.sequence > 0
    assert classified.status is AtomicBusinessWriteReconcileStatus.EXACT


def test_zero_control_nonzero_head_still_requires_membership_when_later() -> None:
    parts = _parts()
    parts["pipeline_intents"] = ()
    atomic = _issue_atomic_commit_plan(**parts)
    before = WorkerLedgerHead(7, b"x" * 32)
    plan = _issue_atomic_business_write_plan(
        atomic_plan=atomic,
        control_plans=(),
        committed_at=COMMITTED_AT,
        before_worker_ledger=before,
    )
    later_atomic = _atomic()
    later_control = _control(
        later_atomic,
        later_atomic.pipeline_intents[0],
        before_ledger=before,
    )
    persisted = dict(
        receipt=plan.expected_receipt.receipt_parent,
        atomic_receipt=plan.expected_receipt,
        current_worker_ledger=later_control.after_ledger,
    )

    missing = _classify_atomic_business_write_receipt(
        plan,
        _fresh_atomic_observation(AtomicBusinessWritePersistedObservation(
            AtomicBusinessWritePersistedReadStatus.COMPLETE,
            **persisted,
        )),
    )
    assert missing.status is AtomicBusinessWriteReconcileStatus.CORRUPT

    membership = WorkerLedgerMembershipEvidence(
        before.sequence,
        before.tail_sha256,
        later_control.after_ledger,
    )
    exact = _classify_atomic_business_write_receipt(
        plan,
        _fresh_atomic_observation(AtomicBusinessWritePersistedObservation(
            AtomicBusinessWritePersistedReadStatus.COMPLETE,
            ledger_membership=membership,
            **persisted,
        )),
    )
    assert exact.status is AtomicBusinessWriteReconcileStatus.EXACT


def test_atomic_receipt_rejects_invalid_control_sets_and_ledger_shapes() -> None:
    first_intent = _parts()["pipeline_intents"][0]
    second_intent = dataclasses.replace(
        first_intent,
        ordinal=1,
        intent_id="6" * 32,
        job_id="6" * 32,
        pipeline_id="render-pipeline-2",
        idempotency_key="pipeline-create-2",
    )
    parts = _parts()
    parts["pipeline_intents"] = (first_intent, second_intent)
    atomic = _issue_atomic_commit_plan(**parts)
    first = _control(atomic, first_intent)
    second = _control(
        atomic,
        second_intent,
        before_ledger=first.after_ledger,
    )
    third = _control(
        atomic,
        second_intent,
        before_ledger=second.after_ledger,
    )
    broken_second = _control(
        atomic,
        second_intent,
        before_ledger=WorkerLedgerHead(
            first.after_ledger.sequence,
            b"q" * 32,
        ),
    )
    plan = _issue_atomic_business_write_plan(
        atomic_plan=atomic,
        control_plans=(first, second),
        committed_at=COMMITTED_AT,
    )
    expected_controls = (
        first.expected_receipt,
        second.expected_receipt,
    )

    def classify(control_receipts):
        return _classify_atomic_business_write_receipt(
            plan,
            _fresh_atomic_observation(AtomicBusinessWritePersistedObservation(
                AtomicBusinessWritePersistedReadStatus.COMPLETE,
                receipt=plan.expected_receipt.receipt_parent,
                atomic_receipt=plan.expected_receipt,
                control_receipts=control_receipts,
                current_worker_ledger=plan.evidence.after_worker_ledger,
            )),
        )

    assert classify(expected_controls[:-1]).status is (
        AtomicBusinessWriteReconcileStatus.CORRUPT
    )
    assert classify(expected_controls + (second.expected_receipt,)).status is (
        AtomicBusinessWriteReconcileStatus.CORRUPT
    )
    structurally_corrupt = (
        (
            (first.expected_receipt, first.expected_receipt),
            "atomic_business_write_control_sequence_corrupt",
        ),
        (
            (first.expected_receipt, third.expected_receipt),
            "atomic_business_write_control_sequence_corrupt",
        ),
        (
            tuple(reversed(expected_controls)),
            "atomic_business_write_control_sequence_corrupt",
        ),
        (
            (first.expected_receipt, broken_second.expected_receipt),
            "atomic_business_write_control_chain_corrupt",
        ),
    )
    for observed_controls, error_code in structurally_corrupt:
        classified = classify(observed_controls)
        assert classified.status is AtomicBusinessWriteReconcileStatus.CORRUPT
        assert classified.error_code == error_code
    changed_control = dataclasses.replace(
        first.expected_receipt,
        receipt_sha256=b"z" * 32,
    )
    assert classify((changed_control, second.expected_receipt)).status is (
        AtomicBusinessWriteReconcileStatus.CORRUPT
    )


def test_atomic_receipt_classifier_requires_the_private_fresh_reader_stamp() -> None:
    plan = _one_control_write()
    persisted = AtomicBusinessWritePersistedObservation(
        AtomicBusinessWritePersistedReadStatus.COMPLETE,
        current_worker_ledger=plan.evidence.before_worker_ledger,
    )
    with pytest.raises(AtomicBusinessWriteIntegrityError):
        _classify_atomic_business_write_receipt(plan, persisted)

    forged = AtomicBusinessWriteFreshObservation.__new__(
        AtomicBusinessWriteFreshObservation
    )
    with pytest.raises(AtomicBusinessWriteIntegrityError):
        _classify_atomic_business_write_receipt(plan, forged)

    with pytest.raises(AtomicBusinessWriteContractError):
        AtomicBusinessWriteDurableReaderCapability(_issuer=object())
    with pytest.raises(AtomicBusinessWriteContractError):
        _stamp_atomic_business_write_fresh_observation(object(), persisted)


def test_modules_are_private_pure_python39_compatible_leaves() -> None:
    root = Path(__file__).resolve().parent
    for name in ("business_atomic_write_contract.py", "business_atomic_write_integrity.py"):
        source = (root / name).read_text(encoding="utf-8")
        tree = ast.parse(source, feature_version=(3, 9))
        for node in ast.walk(tree):
            assert type(node).__name__ != "Match"
        assert " | " not in source
        assert "sqlite3" not in source
    assert contract_module.__all__ == ()
    assert integrity_module.__all__ == ()
