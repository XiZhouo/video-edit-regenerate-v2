"""Typed worker execution closure, state-machine and tamper tests."""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path
from typing import Optional

import pytest


_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from business_transaction_contract import AuthorityBinding, freeze_json_payload  # noqa: E402
from worker_execution_contract import (  # noqa: E402
    EMPTY_WORKER_LEDGER_HEAD,
    PIPELINE_BUSINESS_CONTROL_TRANSACTION_KIND,
    PIPELINE_BUSINESS_CONTROL_TRANSACTION_PREFIX,
    PIPELINE_BUSINESS_CONTROL_PROTOCOL,
    WORKER_EXECUTION_TRANSACTION_KIND,
    WORKER_EXECUTION_TRANSACTION_PREFIX,
    WORKER_EXECUTION_MANIFEST_SCHEMA,
    WORKER_EXECUTION_PROTOCOL,
    MAX_PIPELINE_IDEMPOTENCY_SCOPE_BYTES,
    PipelineBusinessControlPersistedObservation,
    PipelineBusinessControlIntentBinding,
    PipelineBusinessControlOperation,
    PipelineBusinessControlRequest,
    PipelineBusinessControlFreshObservation,
    WorkerAcknowledgeCancelRequest,
    WorkerAttemptStart,
    WorkerAuthorizeEffectRequest,
    WorkerBeforeState,
    WorkerClaimRequest,
    WorkerCompleteRequest,
    WorkerDurableResultProof,
    WorkerEffectAuthorization,
    WorkerEffectBinding,
    WorkerEffectKind,
    WorkerEffectLink,
    WorkerExecutionContractError,
    WorkerExecutionFreshObservation,
    WorkerExecutionOperation,
    WorkerExecutionPersistedObservation,
    WorkerExecutionStatus,
    WorkerExpireRequest,
    WorkerIdempotencyPolicy,
    WorkerLeaseToken,
    WorkerLedgerHead,
    WorkerLedgerMembershipEvidence,
    WorkerOutboxDispatchCursor,
    WorkerOutboxDispatchFence,
    WorkerOutboxDispatchState,
    WorkerOutboxOrder,
    WorkerReconcileProof,
    WorkerReconcileProofKind,
    WorkerReconcileRequest,
    WorkerPersistedReadStatus,
    WorkerReceiptReconcileStatus,
    WorkerRenewRequest,
    WorkerReplayPolicy,
    WorkerRequestHeader,
    WorkerTarget,
    WorkerTargetKind,
    WorkerTerminalProof,
    WorkerTerminalProofKind,
    _PIPELINE_BUSINESS_CONTROL_DURABLE_READER_CAPABILITY,
    _WORKER_EXECUTION_DURABLE_READER_CAPABILITY,
    _stamp_pipeline_business_control_fresh_observation,
    _stamp_worker_execution_fresh_observation,
)
from worker_execution_integrity import (  # noqa: E402
    PipelineBusinessControlPlan,
    PIPELINE_BUSINESS_CONTROL_MANIFEST_HASH_DOMAIN,
    PIPELINE_BUSINESS_CONTROL_RECEIPT_HASH_DOMAIN,
    WORKER_EXECUTION_AFTER_STATE_HASH_DOMAIN,
    WORKER_EXECUTION_RECEIPT_HASH_DOMAIN,
    WORKER_EXECUTION_MANIFEST_HASH_DOMAIN,
    WORKER_LEDGER_TAIL_HASH_DOMAIN,
    WorkerExecutionIntegrityError,
    WorkerExecutionPlan,
    _classify_pipeline_business_control_receipt,
    _classify_worker_execution_receipt,
    _issue_pipeline_business_control_plan,
    _issue_worker_execution_plan,
    _require_pipeline_business_control_plan,
    _require_worker_execution_plan,
)


T0 = "2026-07-12T00:00:00.000Z"
T1 = "2026-07-12T00:00:10.000Z"
T2 = "2026-07-12T00:00:20.000Z"
T3 = "2026-07-12T00:00:30.000Z"
ATTEMPT_ID = "a" * 32
EFFECT_ID = "f" * 32


def _authority() -> AuthorityBinding:
    return AuthorityBinding(
        "sqlite", "active", "e" * 64, 7, 11, 13,
        "c" * 64, "runtime-1", b"d" * 32,
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
            {"kind": kind.value, "input": "exact"},
            schema="test.worker-target.v1",
        ),
        replay_policy=replay,
        idempotency_policy=idempotency,
        idempotency_scope=(
            "pipeline/project-1/2/pipeline-1"
            if kind is WorkerTargetKind.PIPELINE
            else "provider/account-1"
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


def _control_intent(
    target: WorkerTarget,
    *,
    origin_commit_id: Optional[str] = None,
    intent_type: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> PipelineBusinessControlIntentBinding:
    return PipelineBusinessControlIntentBinding(
        origin_commit_id=(
            target.origin_commit_id
            if origin_commit_id is None
            else origin_commit_id
        ),
        intent_type=(target.intent_type if intent_type is None else intent_type),
        idempotency_key=(
            target.idempotency_key
            if idempotency_key is None
            else idempotency_key
        ),
        payload=target.payload,
        replay_policy=target.replay_policy,
        idempotency_policy=target.idempotency_policy,
        max_attempts=target.max_attempts,
    )


def _queued(kind: WorkerTargetKind) -> WorkerBeforeState:
    return WorkerBeforeState(
        status=(
            WorkerExecutionStatus.PENDING
            if kind is WorkerTargetKind.OUTBOX
            else WorkerExecutionStatus.QUEUED
        ),
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


def _active(kind: WorkerTargetKind, *, effect: bool = False) -> WorkerBeforeState:
    status = {
        WorkerTargetKind.PIPELINE: WorkerExecutionStatus.RUNNING,
        WorkerTargetKind.ACTION: WorkerExecutionStatus.RUNNING,
        WorkerTargetKind.OUTBOX: WorkerExecutionStatus.LEASED,
    }[kind]
    current_effect = _effect_binding(kind) if effect else None
    return WorkerBeforeState(
        status=status,
        state_revision=2,
        attempt_count=1,
        last_attempt_id=None,
        last_attempt_number=None,
        current_attempt_id=ATTEMPT_ID,
        current_attempt_number=1,
        lease_owner="worker-1",
        lease_fence=1,
        lease_expires_at=T2,
        fence_high_water=1,
        current_effect=current_effect,
    )


def _effect_kind(kind: WorkerTargetKind) -> WorkerEffectKind:
    return {
        WorkerTargetKind.PIPELINE: WorkerEffectKind.PIPELINE_DOCUMENT_COMMIT,
        WorkerTargetKind.ACTION: WorkerEffectKind.ACTION_PROVIDER,
        WorkerTargetKind.OUTBOX: WorkerEffectKind.OUTBOX_PUBLISH,
    }[kind]


def _effect_binding(kind: WorkerTargetKind) -> WorkerEffectBinding:
    request_sha256 = freeze_json_payload(
        {"external": "request"}, schema="test.effect-request.v1"
    ).payload_sha256
    return WorkerEffectBinding(
        ATTEMPT_ID,
        1,
        EFFECT_ID,
        _effect_kind(kind),
        request_sha256,
        (
            "pipeline/project-1/2/pipeline-1"
            if kind is WorkerTargetKind.PIPELINE
            else "provider/account-1"
        ),
        "external-key-1",
    )


def _unknown(kind: WorkerTargetKind) -> WorkerBeforeState:
    return WorkerBeforeState(
        status=WorkerExecutionStatus.OUTCOME_UNKNOWN,
        state_revision=3,
        attempt_count=1,
        last_attempt_id=ATTEMPT_ID,
        last_attempt_number=1,
        current_attempt_id=None,
        current_attempt_number=None,
        lease_owner=None,
        lease_fence=None,
        lease_expires_at=None,
        fence_high_water=1,
        unresolved_effect=_effect_binding(kind),
    )


def _before_from_after(after) -> WorkerBeforeState:
    return WorkerBeforeState(
        status=after.status,
        state_revision=after.state_revision,
        attempt_count=after.attempt_count,
        last_attempt_id=after.last_attempt_id,
        last_attempt_number=after.last_attempt_number,
        current_attempt_id=after.current_attempt_id,
        current_attempt_number=after.current_attempt_number,
        lease_owner=after.lease_owner,
        lease_fence=after.lease_fence,
        lease_expires_at=after.lease_expires_at,
        fence_high_water=after.fence_high_water,
        current_effect=after.current_effect,
        unresolved_effect=after.unresolved_effect,
        next_attempt_at=after.next_attempt_at,
        cancellation_pending=after.cancellation_pending,
    )


def _header(
    kind: WorkerTargetKind,
    before: WorkerBeforeState,
    occurred_at: str,
    tx: str = "9" * 32,
) -> WorkerRequestHeader:
    return WorkerRequestHeader(tx, _authority(), _target(kind), before, occurred_at)


def _lease() -> WorkerLeaseToken:
    return WorkerLeaseToken(ATTEMPT_ID, 1, "worker-1", 1, T2)


def _effect_link(
    kind: WorkerTargetKind,
    *,
    result_commit: bool = False,
    provider_receipt: bool = False,
) -> WorkerEffectLink:
    binding = _effect_binding(kind)
    return WorkerEffectLink(
        attempt_id=binding.attempt_id,
        attempt_number=binding.attempt_number,
        effect_id=binding.effect_id,
        effect_kind=binding.effect_kind,
        request_sha256=binding.request_sha256,
        result_commit_id="4" * 32 if result_commit else None,
        provider_receipt=(
            freeze_json_payload(
                {"receipt": "ok"}, schema="test.provider-receipt.v1"
            )
            if provider_receipt
            else None
        ),
    )


def _claim(kind: WorkerTargetKind) -> WorkerClaimRequest:
    return WorkerClaimRequest(
        _header(kind, _queued(kind), T0),
        WorkerAttemptStart(ATTEMPT_ID, 1, None, None, "worker-1", 1, T0, T2),
        (
            _first_outbox_claim_fence()
            if kind is WorkerTargetKind.OUTBOX
            else None
        ),
    )


def _first_outbox_claim_fence() -> WorkerOutboxDispatchFence:
    published = WorkerOutboxDispatchCursor(0, 0, -1)
    return WorkerOutboxDispatchFence(
        WorkerOutboxDispatchState(1, published, None),
        WorkerOutboxDispatchState(
            2,
            published,
            WorkerOutboxOrder(1, 7, 0),
        ),
    )


def _preserve_outbox_inflight_fence(
    *,
    sequence: int = 1,
    repository_revision: int = 7,
    ordinal: int = 0,
    control_revision: int = 2,
) -> WorkerOutboxDispatchFence:
    published = WorkerOutboxDispatchCursor(
        sequence - 1,
        0 if sequence == 1 else repository_revision,
        -1 if sequence == 1 else ordinal - 1,
    )
    inflight = WorkerOutboxOrder(sequence, repository_revision, ordinal)
    state = WorkerOutboxDispatchState(
        control_revision,
        published,
        inflight,
    )
    return WorkerOutboxDispatchFence(state, state)


def _advance_outbox_published_fence() -> WorkerOutboxDispatchFence:
    published = WorkerOutboxDispatchCursor(0, 0, -1)
    target = WorkerOutboxOrder(1, 7, 0)
    return WorkerOutboxDispatchFence(
        WorkerOutboxDispatchState(2, published, target),
        WorkerOutboxDispatchState(
            3,
            WorkerOutboxDispatchCursor(1, 7, 0),
            None,
        ),
    )


def _cancel_requested_before(*, effect: bool = False) -> WorkerBeforeState:
    running = _active(WorkerTargetKind.PIPELINE, effect=effect)
    target = _target(WorkerTargetKind.PIPELINE)
    request = PipelineBusinessControlRequest(
        "6" * 32,
        _authority(),
        target,
        _control_intent(target),
        PipelineBusinessControlOperation.APPLY_CONTROL_INTENT,
        running,
        WorkerExecutionStatus.CANCEL_REQUESTED,
        T1,
    )
    plan = _issue_pipeline_business_control_plan(request)
    return _before_from_after(plan.closure.after_state)


@pytest.mark.parametrize(
    ("kind", "after"),
    (
        (WorkerTargetKind.PIPELINE, WorkerExecutionStatus.RUNNING),
        (WorkerTargetKind.ACTION, WorkerExecutionStatus.RUNNING),
        (WorkerTargetKind.OUTBOX, WorkerExecutionStatus.LEASED),
    ),
)
def test_claim_for_all_target_kinds_has_exact_cas_manifest(kind, after) -> None:
    plan = _issue_worker_execution_plan(_claim(kind))
    assert plan.closure.before_state_revision == 1
    assert plan.closure.after_state_revision == 2
    assert plan.closure.after_status is after
    assert len(plan.closure.request_sha256) == 32
    assert _require_worker_execution_plan(plan) is plan
    manifest = json.loads(plan.closure.manifest_bytes)
    assert manifest["header"]["authority"]["epoch"] == "e" * 64
    assert manifest["header"]["authority"]["cutover_id"] == "c" * 64
    assert manifest["header"]["authority"]["runtime_generation"] == "runtime-1"
    assert manifest["header"]["authority"]["code_digest"] == (b"d" * 32).hex()
    assert manifest["header"]["request_transaction_id"] == "9" * 32
    assert manifest["header"]["target"]["payload"]["sha256"]
    assert manifest["header"]["target"]["idempotency_key"] == "external-key-1"
    assert manifest["request"]["execution_fence"] == 1
    assert manifest["protocol"] == WORKER_EXECUTION_PROTOCOL
    assert manifest["schema"] == WORKER_EXECUTION_MANIFEST_SCHEMA
    assert WORKER_EXECUTION_PROTOCOL.endswith(".v3")
    assert WORKER_EXECUTION_MANIFEST_SCHEMA.endswith(".v3")
    assert WORKER_EXECUTION_MANIFEST_HASH_DOMAIN.endswith(b".v3")
    assert WORKER_EXECUTION_RECEIPT_HASH_DOMAIN.endswith(b".v3")
    assert WORKER_EXECUTION_AFTER_STATE_HASH_DOMAIN.endswith(b".v3")
    assert WORKER_LEDGER_TAIL_HASH_DOMAIN.endswith(b".v1")
    assert plan.before_ledger == EMPTY_WORKER_LEDGER_HEAD
    assert plan.expected_receipt.sequence == 1
    assert plan.expected_receipt.previous_ledger_tail_sha256 == b"\0" * 32
    assert plan.expected_receipt.request_sha256 == plan.closure.request_sha256
    assert plan.expected_receipt.request_class == "worker_execution"
    assert plan.expected_receipt.operation is WorkerExecutionOperation.CLAIM
    assert plan.expected_receipt.target_kind is kind
    assert plan.expected_receipt.target_id == plan.target.target_id
    assert plan.expected_receipt.before_status is plan.closure.before_status
    assert plan.expected_receipt.after_status is plan.closure.after_status
    assert plan.expected_receipt.before_state_revision == 1
    assert plan.expected_receipt.after_state_revision == 2
    assert plan.after_ledger.tail_sha256 == (
        plan.expected_receipt.ledger_tail_sha256
    )
    if kind is WorkerTargetKind.OUTBOX:
        assert manifest["header"]["target"]["outbox_order"] == {
            "sequence": 1,
            "repository_revision": 7,
            "ordinal": 0,
        }
        assert manifest["request"]["dispatch_fence"]["before"] == {
            "control_revision": 1,
            "published": {
                "sequence": 0,
                "repository_revision": 0,
                "ordinal": -1,
            },
            "inflight": None,
        }
        assert manifest["request"]["dispatch_fence"]["after"]["published"] == (
            manifest["request"]["dispatch_fence"]["before"]["published"]
        )
        assert manifest["request"]["dispatch_fence"]["after"]["inflight"] == {
            "sequence": 1,
            "repository_revision": 7,
            "ordinal": 0,
        }
    else:
        assert manifest["header"]["target"]["outbox_order"] is None
        assert manifest["request"]["dispatch_fence"] is None


def test_claim_manifest_digest_has_a_fixed_domain_separated_vector() -> None:
    plan = _issue_worker_execution_plan(_claim(WorkerTargetKind.PIPELINE))
    assert len(plan.closure.manifest_bytes) == 2479
    assert plan.closure.request_sha256.hex() == (
        "161c066b30dc960fe3d48d9786e582b1eb193e5e6555c22d7c5d914e9b1c84a0"
    )
    assert plan.closure.after_state_sha256.hex() == (
        "d5d8ab07af6dd59574993bebeb5d646d25dcf6272d3836de3739564e6ca29d7d"
    )
    assert plan.expected_receipt.receipt_sha256.hex() == (
        "feb26da58be829a775e7a8177bb67a100eef775ea3cb1f51f461a6c9ce1248ed"
    )
    assert plan.expected_receipt.ledger_tail_sha256.hex() == (
        "d265b354f371c2a36c454276c0dbb7d799c13a34288c604266d16bb796da88e7"
    )


def test_pipeline_never_replay_derives_single_attempt_without_changing_scope() -> None:
    request = _claim(WorkerTargetKind.PIPELINE)
    single_target = dataclasses.replace(
        request.header.target,
        replay_policy=WorkerReplayPolicy.NEVER_REPLAY,
        idempotency_policy=WorkerIdempotencyPolicy.SINGLE_ATTEMPT,
        max_attempts=1,
    )
    plan = _issue_worker_execution_plan(dataclasses.replace(
        request,
        header=dataclasses.replace(request.header, target=single_target),
    ))
    target = json.loads(plan.closure.manifest_bytes)["header"]["target"]
    assert target["idempotency_scope"] == "pipeline/project-1/2/pipeline-1"
    assert target["replay_policy"] == "never_replay"
    assert target["idempotency_policy"] == "single_attempt"
    assert target["max_attempts"] == 1


def test_pipeline_scope_budget_covers_every_legal_component_at_the_boundary() -> None:
    project_id = "\U0001f600" * 160
    pipeline_id = "p" * 1024
    generation = 2**63 - 1
    scope = "pipeline/%s/%d/%s" % (project_id, generation, pipeline_id)
    assert len(project_id.encode("utf-8")) == 640
    assert len(scope.encode("utf-8")) == (
        MAX_PIPELINE_IDEMPOTENCY_SCOPE_BYTES
    )

    target = dataclasses.replace(
        _target(WorkerTargetKind.PIPELINE),
        project_id=project_id,
        generation=generation,
        pipeline_id=pipeline_id,
        idempotency_scope=scope,
    )
    create = PipelineBusinessControlRequest(
        "6" * 32,
        _authority(),
        target,
        _control_intent(target),
        PipelineBusinessControlOperation.CREATE,
        None,
        WorkerExecutionStatus.QUEUED,
        T0,
    )
    created = _issue_pipeline_business_control_plan(create)
    assert created.closure.after_status is WorkerExecutionStatus.QUEUED

    effect_payload = freeze_json_payload(
        {"document": "commit"},
        schema="test.pipeline-effect.v1",
    )
    authorize = WorkerAuthorizeEffectRequest(
        dataclasses.replace(
            _header(
                WorkerTargetKind.PIPELINE,
                _active(WorkerTargetKind.PIPELINE),
                T1,
            ),
            target=target,
        ),
        _lease(),
        WorkerEffectAuthorization(
            EFFECT_ID,
            WorkerEffectKind.PIPELINE_DOCUMENT_COMMIT,
            scope,
            target.idempotency_key,
            effect_payload,
            T1,
        ),
    )
    authorized = _issue_worker_execution_plan(authorize)
    assert authorized.closure.after_state.current_effect.idempotency_scope == scope


def test_pipeline_scope_expansion_does_not_unbound_other_identifiers() -> None:
    with pytest.raises(WorkerExecutionContractError) as rejected:
        dataclasses.replace(
            _target(WorkerTargetKind.PIPELINE),
            pipeline_id="p" * 1025,
            idempotency_scope="pipeline/project-1/2/" + "p" * 1025,
        )
    assert rejected.value.code == "worker_execution_text_budget_exceeded"
    assert rejected.value.path == "$.target.pipeline_id"

    with pytest.raises(WorkerExecutionContractError) as rejected:
        dataclasses.replace(
            _target(WorkerTargetKind.ACTION),
            idempotency_scope="s" * 1025,
        )
    assert rejected.value.code == "worker_execution_text_budget_exceeded"
    assert rejected.value.path == "$.target.idempotency_scope"


def test_claim_cannot_run_before_the_persisted_retry_time() -> None:
    kind = WorkerTargetKind.ACTION
    before = WorkerBeforeState(
        status=WorkerExecutionStatus.RETRY,
        state_revision=3,
        attempt_count=1,
        last_attempt_id=ATTEMPT_ID,
        last_attempt_number=1,
        current_attempt_id=None,
        current_attempt_number=None,
        lease_owner=None,
        lease_fence=None,
        lease_expires_at=None,
        fence_high_water=1,
        next_attempt_at=T2,
    )
    early = WorkerClaimRequest(
        _header(kind, before, T1),
        WorkerAttemptStart("b" * 32, 2, ATTEMPT_ID, 1, "worker-2", 2, T1, T3),
    )
    with pytest.raises(WorkerExecutionIntegrityError) as rejected:
        _issue_worker_execution_plan(early)
    assert rejected.value.code == "worker_execution_claim_invalid"

    on_time = dataclasses.replace(
        early,
        header=_header(kind, before, T2),
        attempt=dataclasses.replace(early.attempt, started_at=T2),
    )
    plan = _issue_worker_execution_plan(on_time)
    assert plan.closure.after_state.next_attempt_at is None
    manifest = json.loads(plan.closure.manifest_bytes)
    assert manifest["header"]["before"]["next_attempt_at"] == T2


def test_outbox_claim_rejects_missing_mismatched_or_foreign_dispatch_fence() -> None:
    outbox = _claim(WorkerTargetKind.OUTBOX)
    with pytest.raises(WorkerExecutionIntegrityError) as rejected:
        _issue_worker_execution_plan(dataclasses.replace(outbox, dispatch_fence=None))
    assert rejected.value.code == "worker_execution_dispatch_fence_invalid"

    wrong_direct_successor = WorkerOutboxDispatchFence(
        WorkerOutboxDispatchState(
            2,
            WorkerOutboxDispatchCursor(1, 6, 0),
            None,
        ),
        WorkerOutboxDispatchState(
            3,
            WorkerOutboxDispatchCursor(1, 6, 0),
            WorkerOutboxOrder(2, 7, 0),
        ),
    )
    with pytest.raises(WorkerExecutionIntegrityError) as rejected:
        _issue_worker_execution_plan(dataclasses.replace(
            outbox,
            dispatch_fence=wrong_direct_successor,
        ))
    assert rejected.value.code == "worker_execution_dispatch_fence_invalid"

    wrong_physical_target = WorkerOutboxDispatchFence(
        WorkerOutboxDispatchState(
            1,
            WorkerOutboxDispatchCursor(0, 0, -1),
            None,
        ),
        WorkerOutboxDispatchState(
            2,
            WorkerOutboxDispatchCursor(0, 0, -1),
            WorkerOutboxOrder(1, 8, 0),
        ),
    )
    with pytest.raises(WorkerExecutionIntegrityError) as rejected:
        _issue_worker_execution_plan(dataclasses.replace(
            outbox,
            dispatch_fence=wrong_physical_target,
        ))
    assert rejected.value.code == "worker_execution_dispatch_fence_invalid"

    action = _claim(WorkerTargetKind.ACTION)
    with pytest.raises(WorkerExecutionIntegrityError) as rejected:
        _issue_worker_execution_plan(dataclasses.replace(
            action,
            dispatch_fence=outbox.dispatch_fence,
        ))
    assert rejected.value.code == "worker_execution_dispatch_fence_unexpected"


def test_outbox_cross_revision_fence_requires_future_writer_same_snapshot_absence_query() -> None:
    """Pure v2 binds the candidate; only the future writer proves no gap."""

    target = dataclasses.replace(
        _target(WorkerTargetKind.OUTBOX),
        outbox_order=WorkerOutboxOrder(6, 9, 0),
    )
    request = WorkerClaimRequest(
        dataclasses.replace(
            _header(WorkerTargetKind.OUTBOX, _queued(WorkerTargetKind.OUTBOX), T0),
            target=target,
        ),
        WorkerAttemptStart(ATTEMPT_ID, 1, None, None, "worker-1", 1, T0, T2),
        WorkerOutboxDispatchFence(
            WorkerOutboxDispatchState(
                4,
                WorkerOutboxDispatchCursor(5, 7, 2),
                None,
            ),
            WorkerOutboxDispatchState(
                5,
                WorkerOutboxDispatchCursor(5, 7, 2),
                WorkerOutboxOrder(6, 9, 0),
            ),
        ),
    )
    plan = _issue_worker_execution_plan(request)
    detail = json.loads(plan.closure.manifest_bytes)["request"]["dispatch_fence"]
    assert detail["before"]["published"]["repository_revision"] == 7
    assert detail["after"]["inflight"]["repository_revision"] == 9
    assert "same snapshot" in WorkerOutboxDispatchFence.__doc__
    assert "first event strictly after" in WorkerOutboxDispatchFence.__doc__


def test_outbox_dispatch_fence_is_rebuilt_before_writer_trust() -> None:
    plan = _issue_worker_execution_plan(_claim(WorkerTargetKind.OUTBOX))
    object.__setattr__(
        plan.request.dispatch_fence.after.inflight,
        "sequence",
        2,
    )
    with pytest.raises(WorkerExecutionIntegrityError) as rejected:
        _require_worker_execution_plan(plan)
    assert rejected.value.code == "worker_execution_request_changed"


def test_outbox_retry_keeps_inflight_and_terminal_publish_advances_only_once() -> None:
    kind = WorkerTargetKind.OUTBOX
    first = _issue_worker_execution_plan(_claim(kind))
    first_fence = first.request.dispatch_fence
    assert first_fence.before.published == first_fence.after.published
    assert first_fence.before.inflight is None
    assert first_fence.after.inflight == first.target.outbox_order

    retry_before = WorkerBeforeState(
        status=WorkerExecutionStatus.RETRY,
        state_revision=3,
        attempt_count=1,
        last_attempt_id=ATTEMPT_ID,
        last_attempt_number=1,
        current_attempt_id=None,
        current_attempt_number=None,
        lease_owner=None,
        lease_fence=None,
        lease_expires_at=None,
        fence_high_water=1,
        next_attempt_at=T1,
    )
    retry_claim = WorkerClaimRequest(
        _header(kind, retry_before, T1, "8" * 32),
        WorkerAttemptStart(
            "b" * 32,
            2,
            ATTEMPT_ID,
            1,
            "worker-2",
            2,
            T1,
            T3,
        ),
        _preserve_outbox_inflight_fence(),
    )
    retry_plan = _issue_worker_execution_plan(
        retry_claim,
        before_ledger=first.after_ledger,
    )
    assert retry_plan.request.dispatch_fence.before == (
        retry_plan.request.dispatch_fence.after
    )

    before = _active(kind, effect=True)
    published = WorkerCompleteRequest(
        _header(kind, before, T1, "7" * 32),
        _lease(),
        WorkerExecutionStatus.PUBLISHED,
        T1,
        effect_link=_effect_link(kind, provider_receipt=True),
        dispatch_fence=_advance_outbox_published_fence(),
    )
    terminal = _issue_worker_execution_plan(
        published,
        before_ledger=retry_plan.after_ledger,
    )
    fence = terminal.request.dispatch_fence
    assert fence.after.published.sequence == 1
    assert fence.after.inflight is None

    unknown = dataclasses.replace(
        published,
        header=_header(kind, before, T1, "6" * 32),
        target_status=WorkerExecutionStatus.OUTCOME_UNKNOWN,
        effect_link=None,
        dispatch_fence=_preserve_outbox_inflight_fence(),
    )
    blocked = _issue_worker_execution_plan(unknown)
    assert blocked.closure.after_status is WorkerExecutionStatus.OUTCOME_UNKNOWN
    assert blocked.request.dispatch_fence.before == blocked.request.dispatch_fence.after


def test_outbox_dispatch_transition_matrix_rejects_premature_or_missing_advance() -> None:
    kind = WorkerTargetKind.OUTBOX
    zero = WorkerOutboxDispatchCursor(0, 0, -1)
    premature_first_advance = WorkerOutboxDispatchFence(
        WorkerOutboxDispatchState(1, zero, None),
        WorkerOutboxDispatchState(
            2,
            WorkerOutboxDispatchCursor(1, 7, 0),
            None,
        ),
    )
    with pytest.raises(WorkerExecutionIntegrityError) as rejected:
        _issue_worker_execution_plan(dataclasses.replace(
            _claim(kind),
            dispatch_fence=premature_first_advance,
        ))
    assert rejected.value.code == "worker_execution_dispatch_fence_invalid"

    retry_before = WorkerBeforeState(
        status=WorkerExecutionStatus.RETRY,
        state_revision=3,
        attempt_count=1,
        last_attempt_id=ATTEMPT_ID,
        last_attempt_number=1,
        current_attempt_id=None,
        current_attempt_number=None,
        lease_owner=None,
        lease_fence=None,
        lease_expires_at=None,
        fence_high_water=1,
        next_attempt_at=T1,
    )
    retry_that_advances = WorkerClaimRequest(
        _header(kind, retry_before, T1),
        WorkerAttemptStart(
            "b" * 32,
            2,
            ATTEMPT_ID,
            1,
            "worker-2",
            2,
            T1,
            T3,
        ),
        _advance_outbox_published_fence(),
    )
    with pytest.raises(WorkerExecutionIntegrityError) as rejected:
        _issue_worker_execution_plan(retry_that_advances)
    assert rejected.value.code == "worker_execution_dispatch_fence_invalid"

    retry_that_recreates_inflight = dataclasses.replace(
        retry_that_advances,
        dispatch_fence=_first_outbox_claim_fence(),
    )
    with pytest.raises(WorkerExecutionIntegrityError) as rejected:
        _issue_worker_execution_plan(retry_that_recreates_inflight)
    assert rejected.value.code == "worker_execution_dispatch_fence_invalid"

    before = _active(kind, effect=True)
    published = WorkerCompleteRequest(
        _header(kind, before, T1),
        _lease(),
        WorkerExecutionStatus.PUBLISHED,
        T1,
        effect_link=_effect_link(kind, provider_receipt=True),
        dispatch_fence=_preserve_outbox_inflight_fence(),
    )
    with pytest.raises(WorkerExecutionIntegrityError) as rejected:
        _issue_worker_execution_plan(published)
    assert rejected.value.code == "worker_execution_dispatch_fence_invalid"

    unknown_that_advances = dataclasses.replace(
        published,
        target_status=WorkerExecutionStatus.OUTCOME_UNKNOWN,
        effect_link=None,
        dispatch_fence=_advance_outbox_published_fence(),
    )
    with pytest.raises(WorkerExecutionIntegrityError) as rejected:
        _issue_worker_execution_plan(unknown_that_advances)
    assert rejected.value.code == "worker_execution_dispatch_fence_invalid"

    evidence = freeze_json_payload(
        {"terminal": True}, schema="test.outbox-terminal.v1"
    )
    dead_without_advance = dataclasses.replace(
        published,
        target_status=WorkerExecutionStatus.DEAD,
        effect_link=None,
        error=evidence,
        terminal_proof=WorkerTerminalProof(
            WorkerTerminalProofKind.TERMINAL_FAILURE,
            before.current_effect,
            evidence,
        ),
    )
    with pytest.raises(WorkerExecutionIntegrityError) as rejected:
        _issue_worker_execution_plan(dead_without_advance)
    assert rejected.value.code == "worker_execution_dispatch_fence_invalid"


def test_outbox_dead_requires_strong_effect_proof_and_advances_cursor() -> None:
    kind = WorkerTargetKind.OUTBOX
    before = _active(kind, effect=True)
    evidence = freeze_json_payload(
        {"terminal": True}, schema="test.outbox-terminal.v1"
    )
    without_proof = WorkerCompleteRequest(
        _header(kind, before, T1),
        _lease(),
        WorkerExecutionStatus.DEAD,
        T1,
        error=evidence,
        dispatch_fence=_advance_outbox_published_fence(),
    )
    with pytest.raises(WorkerExecutionIntegrityError) as rejected:
        _issue_worker_execution_plan(without_proof)
    assert rejected.value.code == "worker_execution_terminal_proof_required"
    no_effect = dataclasses.replace(
        without_proof,
        header=_header(kind, _active(kind), T1, "8" * 32),
    )
    with pytest.raises(WorkerExecutionIntegrityError) as rejected:
        _issue_worker_execution_plan(no_effect)
    assert rejected.value.code == "worker_execution_terminal_proof_required"
    proof = WorkerTerminalProof(
        WorkerTerminalProofKind.TERMINAL_FAILURE,
        before.current_effect,
        evidence,
    )
    plan = _issue_worker_execution_plan(dataclasses.replace(
        without_proof,
        terminal_proof=proof,
    ))
    assert plan.request.dispatch_fence.after.published.sequence == 1
    assert plan.request.dispatch_fence.after.inflight is None


def test_outbox_terminal_proof_rejects_result_commit_in_provider_effect_link() -> None:
    kind = WorkerTargetKind.OUTBOX
    before = _active(kind, effect=True)
    evidence = freeze_json_payload(
        {"terminal": True}, schema="test.outbox-terminal.v1"
    )
    proof = WorkerTerminalProof(
        WorkerTerminalProofKind.TERMINAL_FAILURE,
        before.current_effect,
        evidence,
        _effect_link(
            kind,
            result_commit=True,
            provider_receipt=True,
        ),
    )
    request = WorkerCompleteRequest(
        _header(kind, before, T1),
        _lease(),
        WorkerExecutionStatus.DEAD,
        T1,
        error=evidence,
        terminal_proof=proof,
        dispatch_fence=_advance_outbox_published_fence(),
    )
    with pytest.raises(WorkerExecutionIntegrityError) as rejected:
        _issue_worker_execution_plan(request)
    assert rejected.value.code == "worker_execution_terminal_proof_invalid"


def test_outbox_exhausted_effectless_lease_expires_dead_and_advances_cursor() -> None:
    kind = WorkerTargetKind.OUTBOX
    before = _active(kind)
    target = dataclasses.replace(_target(kind), max_attempts=1)
    request = WorkerExpireRequest(
        dataclasses.replace(
            _header(kind, before, T2),
            target=target,
        ),
        _lease(),
        T2,
        WorkerExecutionStatus.DEAD,
        dispatch_fence=_advance_outbox_published_fence(),
    )

    plan = _issue_worker_execution_plan(request)

    assert plan.operation is WorkerExecutionOperation.EXPIRE
    assert plan.closure.after_status is WorkerExecutionStatus.DEAD
    assert plan.closure.after_state.attempt_count == target.max_attempts
    assert plan.closure.after_state.current_attempt_id is None
    assert plan.closure.after_state.current_effect is None
    assert plan.closure.after_state.unresolved_effect is None
    assert plan.request.dispatch_fence.after.published.sequence == 1
    assert plan.request.dispatch_fence.after.inflight is None
    manifest = json.loads(plan.closure.manifest_bytes)
    assert manifest["operation"] == "expire"
    assert manifest["request"]["expired_at"] == T2
    assert manifest["request"]["lease"]["lease_expires_at"] == T2
    assert manifest["request"]["target_status"] == "dead"


def test_outbox_effectless_expiry_cannot_skip_before_attempt_budget_exhaustion() -> None:
    kind = WorkerTargetKind.OUTBOX
    request = WorkerExpireRequest(
        _header(kind, _active(kind), T2),
        _lease(),
        T2,
        WorkerExecutionStatus.DEAD,
        dispatch_fence=_advance_outbox_published_fence(),
    )
    with pytest.raises(WorkerExecutionIntegrityError) as rejected:
        _issue_worker_execution_plan(request)
    assert rejected.value.code == "worker_execution_expiry_invalid"

    exhausted_target = dataclasses.replace(_target(kind), max_attempts=1)
    exhausted_header = dataclasses.replace(
        request.header,
        target=exhausted_target,
    )
    with_effect = dataclasses.replace(
        request,
        header=dataclasses.replace(
            exhausted_header,
            before=_active(kind, effect=True),
        ),
    )
    with pytest.raises(WorkerExecutionIntegrityError) as rejected:
        _issue_worker_execution_plan(with_effect)
    assert rejected.value.code == "worker_execution_expiry_invalid"

    stale_time = dataclasses.replace(
        request,
        header=dataclasses.replace(exhausted_header, occurred_at=T1),
        expired_at=T1,
    )
    with pytest.raises(WorkerExecutionIntegrityError) as rejected:
        _issue_worker_execution_plan(stale_time)
    assert rejected.value.code == "worker_execution_expiry_invalid"

    missing_cursor_advance = dataclasses.replace(
        request,
        header=exhausted_header,
        dispatch_fence=_preserve_outbox_inflight_fence(),
    )
    with pytest.raises(WorkerExecutionIntegrityError) as rejected:
        _issue_worker_execution_plan(missing_cursor_advance)
    assert rejected.value.code == "worker_execution_dispatch_fence_invalid"


def test_renew_and_effect_authorization_bind_current_lease_and_external_key() -> None:
    kind = WorkerTargetKind.ACTION
    renew = WorkerRenewRequest(_header(kind, _active(kind), T1), _lease(), T3)
    renewed = _issue_worker_execution_plan(renew)
    assert renewed.closure.after_status is WorkerExecutionStatus.RUNNING
    assert renewed.closure.after_state.lease_expires_at == T3

    effect_payload = freeze_json_payload(
        {"external": "request"}, schema="test.effect-request.v1"
    )
    authorize = WorkerAuthorizeEffectRequest(
        _header(kind, _active(kind), T1, "8" * 32),
        _lease(),
        WorkerEffectAuthorization(
            EFFECT_ID,
            WorkerEffectKind.ACTION_PROVIDER,
            "provider/account-1",
            "external-key-1",
            effect_payload,
            T1,
        ),
    )
    authorized = _issue_worker_execution_plan(authorize)
    assert authorized.closure.after_state.current_effect == _effect_binding(kind)
    manifest = json.loads(authorized.closure.manifest_bytes)
    assert manifest["request"]["effect"]["request"]["sha256"] == (
        effect_payload.payload_sha256.hex()
    )


def test_cancel_vs_authorize_linearization_blocks_new_effect_after_cancel() -> None:
    kind = WorkerTargetKind.PIPELINE
    running = _active(kind)
    effect_payload = freeze_json_payload(
        {"document": "commit"}, schema="test.pipeline-effect.v1"
    )
    authorize = WorkerAuthorizeEffectRequest(
        _header(kind, running, T1),
        _lease(),
        WorkerEffectAuthorization(
            EFFECT_ID,
            WorkerEffectKind.PIPELINE_DOCUMENT_COMMIT,
            "pipeline/project-1/2/pipeline-1",
            "external-key-1",
            effect_payload,
            T1,
        ),
    )
    authorize_plan = _issue_worker_execution_plan(authorize)
    target = _target(kind)
    cancel = PipelineBusinessControlRequest(
        "6" * 32,
        _authority(),
        target,
        _control_intent(target),
        PipelineBusinessControlOperation.APPLY_CONTROL_INTENT,
        running,
        WorkerExecutionStatus.CANCEL_REQUESTED,
        T1,
    )
    cancel_plan = _issue_pipeline_business_control_plan(cancel)
    assert authorize_plan.closure.before_state_revision == 2
    assert cancel_plan.closure.before_state_revision == 2
    assert authorize_plan.closure.after_state_revision == 3
    assert cancel_plan.closure.after_state_revision == 3

    cancelled = _before_from_after(cancel_plan.closure.after_state)
    with pytest.raises(WorkerExecutionIntegrityError) as rejected:
        _issue_worker_execution_plan(dataclasses.replace(
            authorize,
            header=_header(kind, cancelled, T1, "5" * 32),
        ))
    assert rejected.value.code == "worker_execution_status_edge_invalid"

    renewed = _issue_worker_execution_plan(WorkerRenewRequest(
        _header(kind, cancelled, T1, "4" * 32),
        _lease(),
        T3,
    ))
    assert renewed.closure.after_state.cancellation_pending is True


def test_cancel_requested_complete_cannot_retry_and_must_converge_terminal() -> None:
    before = _cancel_requested_before()
    retry = WorkerCompleteRequest(
        _header(WorkerTargetKind.PIPELINE, before, T1),
        _lease(),
        WorkerExecutionStatus.RETRY,
        T1,
        next_attempt_at=T2,
    )
    with pytest.raises(WorkerExecutionIntegrityError) as rejected:
        _issue_worker_execution_plan(retry)
    assert rejected.value.code == "worker_execution_cancellation_retry_forbidden"

    cancelled = _issue_worker_execution_plan(dataclasses.replace(
        retry,
        target_status=WorkerExecutionStatus.CANCELLED,
        next_attempt_at=None,
    ))
    assert cancelled.closure.after_status is WorkerExecutionStatus.CANCELLED
    assert cancelled.closure.after_state.cancellation_pending is False


def test_historical_atomic_lease_free_cancel_is_reachable_only_through_ack() -> None:
    legacy_cancel = WorkerBeforeState(
        status=WorkerExecutionStatus.CANCEL_REQUESTED,
        state_revision=2,
        attempt_count=0,
        last_attempt_id=None,
        last_attempt_number=None,
        current_attempt_id=None,
        current_attempt_number=None,
        lease_owner=None,
        lease_fence=None,
        lease_expires_at=None,
        fence_high_water=0,
        cancellation_pending=True,
    )
    request = WorkerAcknowledgeCancelRequest(
        _header(WorkerTargetKind.PIPELINE, legacy_cancel, T1),
        T1,
    )
    plan = _issue_worker_execution_plan(request)
    assert plan.operation is WorkerExecutionOperation.ACKNOWLEDGE_CANCEL
    assert plan.closure.after_status is WorkerExecutionStatus.CANCELLED
    assert plan.closure.after_state.current_attempt_id is None
    assert plan.closure.after_state.cancellation_pending is False

    leased_cancel = _cancel_requested_before()
    with pytest.raises(WorkerExecutionIntegrityError) as rejected:
        _issue_worker_execution_plan(dataclasses.replace(
            request,
            header=_header(
                WorkerTargetKind.PIPELINE,
                leased_cancel,
                T1,
                "3" * 32,
            ),
        ))
    assert rejected.value.code == "worker_execution_cancel_acknowledgement_invalid"


def test_cancel_expiry_and_reconcile_cannot_restore_retry_eligibility() -> None:
    no_effect = _cancel_requested_before()
    retry_expiry = WorkerExpireRequest(
        _header(WorkerTargetKind.PIPELINE, no_effect, T2),
        _lease(),
        T2,
        WorkerExecutionStatus.RETRY,
        T3,
    )
    with pytest.raises(WorkerExecutionIntegrityError) as rejected:
        _issue_worker_execution_plan(retry_expiry)
    assert rejected.value.code == "worker_execution_cancellation_retry_forbidden"
    cancelled = _issue_worker_execution_plan(dataclasses.replace(
        retry_expiry,
        target_status=WorkerExecutionStatus.CANCELLED,
        next_attempt_at=None,
    ))
    assert cancelled.closure.after_status is WorkerExecutionStatus.CANCELLED

    with_effect = _cancel_requested_before(effect=True)
    expired = _issue_worker_execution_plan(WorkerExpireRequest(
        _header(WorkerTargetKind.PIPELINE, with_effect, T2, "8" * 32),
        _lease(),
        T2,
        WorkerExecutionStatus.OUTCOME_UNKNOWN,
    ))
    unknown = _before_from_after(expired.closure.after_state)
    assert unknown.cancellation_pending is True
    assert unknown.unresolved_effect == _effect_binding(WorkerTargetKind.PIPELINE)

    absence = WorkerReconcileProof(
        WorkerReconcileProofKind.PROVEN_NOT_EXECUTED,
        unknown.unresolved_effect,
        freeze_json_payload({"absent": True}, schema="test.effect-absence.v1"),
    )
    retry_reconcile = WorkerReconcileRequest(
        _header(WorkerTargetKind.PIPELINE, unknown, T3, "7" * 32),
        ATTEMPT_ID,
        1,
        1,
        WorkerExecutionStatus.RETRY,
        absence,
        T3,
        T3,
    )
    with pytest.raises(WorkerExecutionIntegrityError) as rejected:
        _issue_worker_execution_plan(retry_reconcile)
    assert rejected.value.code == "worker_execution_cancellation_retry_forbidden"

    terminal = _issue_worker_execution_plan(dataclasses.replace(
        retry_reconcile,
        target_status=WorkerExecutionStatus.CANCELLED,
        next_attempt_at=None,
    ))
    assert terminal.closure.after_status is WorkerExecutionStatus.CANCELLED
    assert terminal.closure.after_state.cancellation_pending is False


@pytest.mark.parametrize(
    ("kind", "terminal"),
    (
        (WorkerTargetKind.PIPELINE, WorkerExecutionStatus.SUCCEEDED),
        (WorkerTargetKind.ACTION, WorkerExecutionStatus.SUCCEEDED),
        (WorkerTargetKind.OUTBOX, WorkerExecutionStatus.PUBLISHED),
    ),
)
def test_complete_has_target_specific_terminal_receipt(kind, terminal) -> None:
    before = _active(kind, effect=True)
    link = _effect_link(
        kind,
        result_commit=kind is WorkerTargetKind.PIPELINE,
        provider_receipt=kind is not WorkerTargetKind.PIPELINE,
    )
    request = WorkerCompleteRequest(
        _header(kind, before, T1),
        _lease(),
        terminal,
        T1,
        result=freeze_json_payload({"ok": True}, schema="test.result.v1"),
        effect_link=link,
        dispatch_fence=(
            _advance_outbox_published_fence()
            if kind is WorkerTargetKind.OUTBOX
            else None
        ),
    )
    plan = _issue_worker_execution_plan(request)
    assert plan.closure.after_status is terminal
    assert plan.closure.after_state.result_effect_link == link
    manifest = json.loads(plan.closure.manifest_bytes)
    assert manifest["after"]["current_attempt_id"] is None
    if kind is WorkerTargetKind.PIPELINE:
        assert manifest["request"]["effect_link"]["result_commit_id"] == "4" * 32
    else:
        assert manifest["request"]["effect_link"]["provider_receipt"]["sha256"]


def test_outbox_success_rejects_result_commit_in_provider_effect_link() -> None:
    kind = WorkerTargetKind.OUTBOX
    request = WorkerCompleteRequest(
        _header(kind, _active(kind, effect=True), T1),
        _lease(),
        WorkerExecutionStatus.PUBLISHED,
        T1,
        effect_link=_effect_link(
            kind,
            result_commit=True,
            provider_receipt=True,
        ),
        dispatch_fence=_advance_outbox_published_fence(),
    )
    with pytest.raises(WorkerExecutionIntegrityError) as rejected:
        _issue_worker_execution_plan(request)
    assert rejected.value.code == "worker_execution_outbox_publish_proof_required"


def test_expiry_with_authorized_effect_is_outcome_unknown_and_retains_binding() -> None:
    request = WorkerExpireRequest(
        _header(
            WorkerTargetKind.ACTION,
            _active(WorkerTargetKind.ACTION, effect=True),
            T2,
        ),
        _lease(),
        T2,
        WorkerExecutionStatus.OUTCOME_UNKNOWN,
    )
    plan = _issue_worker_execution_plan(request)
    assert plan.closure.after_status is WorkerExecutionStatus.OUTCOME_UNKNOWN
    assert plan.closure.after_state.unresolved_effect == _effect_binding(
        WorkerTargetKind.ACTION
    )


def test_claim_crash_before_effect_authorization_can_expire_to_safe_retry() -> None:
    request = WorkerExpireRequest(
        _header(WorkerTargetKind.ACTION, _active(WorkerTargetKind.ACTION), T2),
        _lease(),
        T2,
        WorkerExecutionStatus.RETRY,
        T3,
    )
    plan = _issue_worker_execution_plan(request)
    assert plan.closure.after_status is WorkerExecutionStatus.RETRY
    assert plan.closure.after_state.unresolved_effect is None
    assert plan.closure.after_state.next_attempt_at == T3


def _reconcile_proof(
    kind: WorkerTargetKind,
    target_status: WorkerExecutionStatus,
) -> WorkerReconcileProof:
    if kind is WorkerTargetKind.PIPELINE:
        proof_kind = WorkerReconcileProofKind.RESULT_COMMIT
        link = _effect_link(kind, result_commit=True)
    elif kind is WorkerTargetKind.OUTBOX:
        proof_kind = WorkerReconcileProofKind.PROVIDER_RECEIPT
        link = _effect_link(kind, provider_receipt=True)
    else:
        assert target_status is WorkerExecutionStatus.RETRY
        proof_kind = WorkerReconcileProofKind.PROVIDER_ABSENCE
        link = None
    return WorkerReconcileProof(
        proof_kind,
        _effect_binding(kind),
        freeze_json_payload({"proof": "exact"}, schema="test.reconcile.v1"),
        effect_link=link,
    )


@pytest.mark.parametrize(
    ("kind", "target_status"),
    (
        (WorkerTargetKind.PIPELINE, WorkerExecutionStatus.SUCCEEDED),
        (WorkerTargetKind.ACTION, WorkerExecutionStatus.RETRY),
        (WorkerTargetKind.OUTBOX, WorkerExecutionStatus.PUBLISHED),
    ),
)
def test_reconcile_requires_terminal_tail_and_evidence(kind, target_status) -> None:
    request = WorkerReconcileRequest(
        _header(kind, _unknown(kind), T3),
        ATTEMPT_ID,
        1,
        1,
        target_status,
        _reconcile_proof(kind, target_status),
        T3,
        T3 if target_status is WorkerExecutionStatus.RETRY else None,
        dispatch_fence=(
            _advance_outbox_published_fence()
            if kind is WorkerTargetKind.OUTBOX
            else None
        ),
    )
    plan = _issue_worker_execution_plan(request)
    assert plan.closure.after_status is target_status
    assert json.loads(plan.closure.manifest_bytes)["request"]["proof"]["evidence"]["sha256"]


def test_wrong_state_edges_stale_fence_and_late_completion_are_rejected() -> None:
    with pytest.raises(WorkerExecutionIntegrityError) as rejected:
        _issue_worker_execution_plan(
            WorkerCompleteRequest(
                _header(
                    WorkerTargetKind.OUTBOX,
                    _active(WorkerTargetKind.OUTBOX),
                    T1,
                ),
                _lease(),
                WorkerExecutionStatus.SUCCEEDED,
                T1,
            )
        )
    assert rejected.value.code == "worker_execution_completion_invalid"

    stale = dataclasses.replace(_lease(), execution_fence=2)
    with pytest.raises(WorkerExecutionIntegrityError) as rejected:
        _issue_worker_execution_plan(
            WorkerRenewRequest(
                _header(
                    WorkerTargetKind.PIPELINE,
                    _active(WorkerTargetKind.PIPELINE),
                    T1,
                ),
                stale,
                T3,
            )
        )
    assert rejected.value.code == "worker_execution_lease_binding_invalid"

    with pytest.raises(WorkerExecutionIntegrityError) as rejected:
        _issue_worker_execution_plan(
            WorkerCompleteRequest(
                _header(
                    WorkerTargetKind.ACTION,
                    _active(WorkerTargetKind.ACTION),
                    T3,
                ),
                _lease(),
                WorkerExecutionStatus.SUCCEEDED,
                T3,
            )
        )
    assert rejected.value.code == "worker_execution_completion_invalid"


def test_effect_started_completion_cannot_drop_or_change_effect_link() -> None:
    before = _active(WorkerTargetKind.ACTION, effect=True)
    request = WorkerCompleteRequest(
        _header(WorkerTargetKind.ACTION, before, T1),
        _lease(),
        WorkerExecutionStatus.SUCCEEDED,
        T1,
    )
    with pytest.raises(WorkerExecutionIntegrityError) as rejected:
        _issue_worker_execution_plan(request)
    assert rejected.value.code == "worker_execution_action_success_proof_required"


def test_target_specific_success_proof_matrix_rejects_lookalikes() -> None:
    pipeline = _active(WorkerTargetKind.PIPELINE, effect=True)
    with pytest.raises(WorkerExecutionIntegrityError) as rejected:
        _issue_worker_execution_plan(WorkerCompleteRequest(
            _header(WorkerTargetKind.PIPELINE, pipeline, T1),
            _lease(),
            WorkerExecutionStatus.SUCCEEDED,
            T1,
            effect_link=_effect_link(
                WorkerTargetKind.PIPELINE,
                provider_receipt=True,
            ),
        ))
    assert rejected.value.code == "worker_execution_pipeline_success_proof_required"

    outbox = _active(WorkerTargetKind.OUTBOX, effect=True)
    with pytest.raises(WorkerExecutionIntegrityError) as rejected:
        _issue_worker_execution_plan(WorkerCompleteRequest(
            _header(WorkerTargetKind.OUTBOX, outbox, T1),
            _lease(),
            WorkerExecutionStatus.PUBLISHED,
            T1,
            effect_link=_effect_link(
                WorkerTargetKind.OUTBOX,
                result_commit=True,
            ),
            dispatch_fence=_advance_outbox_published_fence(),
        ))
    assert rejected.value.code == "worker_execution_outbox_publish_proof_required"

    durable = WorkerDurableResultProof(
        "6" * 32,
        freeze_json_payload({"durable": True}, schema="test.durable-result.v1"),
        T1,
    )
    action = _issue_worker_execution_plan(WorkerCompleteRequest(
        _header(
            WorkerTargetKind.ACTION,
            _active(WorkerTargetKind.ACTION),
            T1,
        ),
        _lease(),
        WorkerExecutionStatus.SUCCEEDED,
        T1,
        durable_result=durable,
    ))
    assert action.closure.after_state.durable_result == durable


def test_completion_at_exact_lease_expiry_is_rejected() -> None:
    durable = WorkerDurableResultProof(
        "6" * 32,
        freeze_json_payload({"durable": True}, schema="test.durable-result.v1"),
        T1,
    )
    with pytest.raises(WorkerExecutionIntegrityError) as rejected:
        _issue_worker_execution_plan(WorkerCompleteRequest(
            _header(
                WorkerTargetKind.ACTION,
                _active(WorkerTargetKind.ACTION),
                T2,
            ),
            _lease(),
            WorkerExecutionStatus.SUCCEEDED,
            T2,
            durable_result=durable,
        ))
    assert rejected.value.code == "worker_execution_completion_invalid"


def test_authorized_effect_failure_or_cancel_requires_typed_terminal_proof() -> None:
    kind = WorkerTargetKind.ACTION
    before = _active(kind, effect=True)
    error = freeze_json_payload({"failed": True}, schema="test.failure.v1")
    request = WorkerCompleteRequest(
        _header(kind, before, T1),
        _lease(),
        WorkerExecutionStatus.FAILED_TERMINAL,
        T1,
        error=error,
    )
    with pytest.raises(WorkerExecutionIntegrityError) as rejected:
        _issue_worker_execution_plan(request)
    assert rejected.value.code == "worker_execution_terminal_proof_required"

    proof = WorkerTerminalProof(
        WorkerTerminalProofKind.TERMINAL_FAILURE,
        _effect_binding(kind),
        error,
        _effect_link(kind, provider_receipt=True),
    )
    plan = _issue_worker_execution_plan(dataclasses.replace(
        request,
        terminal_proof=proof,
    ))
    assert plan.closure.after_status is WorkerExecutionStatus.FAILED_TERMINAL
    assert plan.closure.after_state.result_effect_link == proof.effect_link


def test_effect_kind_matrix_is_frozen_per_target() -> None:
    effect_payload = freeze_json_payload(
        {"external": "request"}, schema="test.effect-request.v1"
    )
    request = WorkerAuthorizeEffectRequest(
        _header(
            WorkerTargetKind.ACTION,
            _active(WorkerTargetKind.ACTION),
            T1,
        ),
        _lease(),
        WorkerEffectAuthorization(
            EFFECT_ID,
            WorkerEffectKind.OUTBOX_PUBLISH,
            "provider/account-1",
            "external-key-1",
            effect_payload,
            T1,
        ),
    )
    with pytest.raises(WorkerExecutionIntegrityError) as rejected:
        _issue_worker_execution_plan(request)
    assert rejected.value.code == "worker_execution_effect_authorization_invalid"


def test_unknown_and_repair_heads_require_exact_unresolved_effect_binding() -> None:
    with pytest.raises(WorkerExecutionContractError) as rejected:
        WorkerBeforeState(
            status=WorkerExecutionStatus.OUTCOME_UNKNOWN,
            state_revision=3,
            attempt_count=1,
            last_attempt_id=ATTEMPT_ID,
            last_attempt_number=1,
            current_attempt_id=None,
            current_attempt_number=None,
            lease_owner=None,
            lease_fence=None,
            lease_expires_at=None,
            fence_high_water=1,
        )
    assert rejected.value.code == "worker_execution_unresolved_effect_required"

    before = _unknown(WorkerTargetKind.OUTBOX)
    wrong_effect = dataclasses.replace(before.unresolved_effect, effect_id="7" * 32)
    proof = WorkerReconcileProof(
        WorkerReconcileProofKind.PROVIDER_RECEIPT,
        wrong_effect,
        freeze_json_payload({"proof": "exact"}, schema="test.reconcile.v1"),
        effect_link=_effect_link(WorkerTargetKind.OUTBOX, provider_receipt=True),
    )
    request = WorkerReconcileRequest(
        _header(WorkerTargetKind.OUTBOX, before, T3),
        ATTEMPT_ID,
        1,
        1,
        WorkerExecutionStatus.PUBLISHED,
        proof,
        T3,
        dispatch_fence=_advance_outbox_published_fence(),
    )
    with pytest.raises(WorkerExecutionIntegrityError) as rejected:
        _issue_worker_execution_plan(request)
    assert rejected.value.code == "worker_execution_reconcile_effect_mismatch"


def test_reconcile_retry_requires_absence_proof_budget_and_replay_permission() -> None:
    kind = WorkerTargetKind.ACTION
    before = _unknown(kind)
    base_header = _header(kind, before, T3)
    absence = WorkerReconcileProof(
        WorkerReconcileProofKind.PROVIDER_ABSENCE,
        before.unresolved_effect,
        freeze_json_payload({"absent": True}, schema="test.provider-absence.v1"),
    )

    reconcile_target = dataclasses.replace(
        base_header.target,
        replay_policy=WorkerReplayPolicy.RECONCILE_BEFORE_RETRY,
        idempotency_policy=WorkerIdempotencyPolicy.DURABLE_RECEIPT_RECONCILE,
    )
    accepted = WorkerReconcileRequest(
        dataclasses.replace(base_header, target=reconcile_target),
        ATTEMPT_ID,
        1,
        1,
        WorkerExecutionStatus.RETRY,
        absence,
        T3,
        T3,
    )
    plan = _issue_worker_execution_plan(accepted)
    assert plan.closure.after_state.unresolved_effect is None

    arbitrary = dataclasses.replace(
        absence,
        proof_kind=WorkerReconcileProofKind.TERMINAL_FAILURE,
    )
    with pytest.raises(WorkerExecutionIntegrityError) as rejected:
        _issue_worker_execution_plan(dataclasses.replace(accepted, proof=arbitrary))
    assert rejected.value.code == "worker_execution_reconcile_retry_forbidden"

    exhausted_target = dataclasses.replace(reconcile_target, max_attempts=1)
    exhausted = dataclasses.replace(
        accepted,
        header=dataclasses.replace(base_header, target=exhausted_target),
    )
    with pytest.raises(WorkerExecutionIntegrityError) as rejected:
        _issue_worker_execution_plan(exhausted)
    assert rejected.value.code == "worker_execution_reconcile_retry_forbidden"

    never_target = dataclasses.replace(
        base_header.target,
        replay_policy=WorkerReplayPolicy.NEVER_REPLAY,
        idempotency_policy=WorkerIdempotencyPolicy.SINGLE_ATTEMPT,
        max_attempts=1,
    )
    never = dataclasses.replace(
        accepted,
        header=dataclasses.replace(base_header, target=never_target),
    )
    with pytest.raises(WorkerExecutionIntegrityError) as rejected:
        _issue_worker_execution_plan(never)
    assert rejected.value.code == "worker_execution_reconcile_retry_forbidden"


def test_expired_authorized_effect_cannot_be_retried_or_dropped() -> None:
    kind = WorkerTargetKind.ACTION
    request = WorkerExpireRequest(
        _header(kind, _active(kind, effect=True), T2),
        _lease(),
        T2,
        WorkerExecutionStatus.RETRY,
        T3,
    )
    with pytest.raises(WorkerExecutionIntegrityError) as rejected:
        _issue_worker_execution_plan(request)
    assert rejected.value.code == "worker_execution_expiry_invalid"


def _fresh_worker_observation(
    persisted: WorkerExecutionPersistedObservation,
) -> WorkerExecutionFreshObservation:
    return _stamp_worker_execution_fresh_observation(
        _WORKER_EXECUTION_DURABLE_READER_CAPABILITY,
        persisted,
    )


def _fresh_control_observation(
    persisted: PipelineBusinessControlPersistedObservation,
) -> PipelineBusinessControlFreshObservation:
    return _stamp_pipeline_business_control_fresh_observation(
        _PIPELINE_BUSINESS_CONTROL_DURABLE_READER_CAPABILITY,
        persisted,
    )


def test_worker_receipt_fresh_classifier_handles_absent_exact_and_conflict() -> None:
    plan = _issue_worker_execution_plan(_claim(WorkerTargetKind.PIPELINE))
    absent = _classify_worker_execution_receipt(
        plan,
        _fresh_worker_observation(WorkerExecutionPersistedObservation(
            WorkerPersistedReadStatus.COMPLETE,
            ledger_head=plan.before_ledger,
        )),
    )
    assert absent.status is WorkerReceiptReconcileStatus.ABSENT

    exact = _classify_worker_execution_receipt(
        plan,
        _fresh_worker_observation(WorkerExecutionPersistedObservation(
            WorkerPersistedReadStatus.COMPLETE,
            receipt=plan.expected_receipt.receipt_parent,
            execution_receipt=plan.expected_receipt,
            ledger_head=plan.after_ledger,
        )),
    )
    assert exact.status is WorkerReceiptReconcileStatus.EXACT
    assert exact.evidence == plan.expected_receipt

    other = _issue_worker_execution_plan(_claim(WorkerTargetKind.ACTION))
    conflict = _classify_worker_execution_receipt(
        plan,
        _fresh_worker_observation(WorkerExecutionPersistedObservation(
            WorkerPersistedReadStatus.COMPLETE,
            receipt=other.expected_receipt.receipt_parent,
            execution_receipt=other.expected_receipt,
            ledger_head=other.after_ledger,
        )),
    )
    assert conflict.status is WorkerReceiptReconcileStatus.CONFLICT


def test_worker_receipt_later_ledger_and_current_progress_preserve_exact() -> None:
    first = _issue_worker_execution_plan(_claim(WorkerTargetKind.PIPELINE))
    second = _issue_worker_execution_plan(
        dataclasses.replace(
            _claim(WorkerTargetKind.ACTION),
            header=dataclasses.replace(
                _claim(WorkerTargetKind.ACTION).header,
                request_transaction_id="8" * 32,
            ),
        ),
        before_ledger=first.after_ledger,
    )
    assert second.expected_receipt.sequence == 2
    assert second.expected_receipt.previous_ledger_tail_sha256 == (
        first.expected_receipt.ledger_tail_sha256
    )
    classified = _classify_worker_execution_receipt(
        first,
        _fresh_worker_observation(WorkerExecutionPersistedObservation(
            WorkerPersistedReadStatus.COMPLETE,
            receipt=first.expected_receipt.receipt_parent,
            execution_receipt=first.expected_receipt,
            ledger_head=second.after_ledger,
            ledger_membership=WorkerLedgerMembershipEvidence(
                first.expected_receipt.sequence,
                first.expected_receipt.ledger_tail_sha256,
                second.after_ledger,
            ),
        )),
    )
    assert classified.status is WorkerReceiptReconcileStatus.EXACT

    unrelated_later_head = WorkerLedgerHead(
        second.after_ledger.sequence,
        b"x" * 32,
    )
    missing_membership = _classify_worker_execution_receipt(
        first,
        _fresh_worker_observation(WorkerExecutionPersistedObservation(
            WorkerPersistedReadStatus.COMPLETE,
            receipt=first.expected_receipt.receipt_parent,
            execution_receipt=first.expected_receipt,
            ledger_head=unrelated_later_head,
        )),
    )
    assert missing_membership.status is WorkerReceiptReconcileStatus.CORRUPT


def test_worker_receipt_partial_unknown_and_raw_evidence_fail_closed() -> None:
    plan = _issue_worker_execution_plan(_claim(WorkerTargetKind.PIPELINE))
    partial = _classify_worker_execution_receipt(
        plan,
        _fresh_worker_observation(WorkerExecutionPersistedObservation(
            WorkerPersistedReadStatus.COMPLETE,
            receipt=plan.expected_receipt.receipt_parent,
            ledger_head=plan.after_ledger,
        )),
    )
    assert partial.status is WorkerReceiptReconcileStatus.CORRUPT
    unknown = _classify_worker_execution_receipt(
        plan,
        _fresh_worker_observation(WorkerExecutionPersistedObservation(
            WorkerPersistedReadStatus.UNKNOWN,
            error_code="worker_receipt_unavailable",
        )),
    )
    assert unknown.status is WorkerReceiptReconcileStatus.UNKNOWN
    tampered_receipt = dataclasses.replace(
        plan.expected_receipt,
        request_sha256=b"z" * 32,
    )
    corrupt = _classify_worker_execution_receipt(
        plan,
        _fresh_worker_observation(WorkerExecutionPersistedObservation(
            WorkerPersistedReadStatus.COMPLETE,
            receipt=tampered_receipt.receipt_parent,
            execution_receipt=tampered_receipt,
            ledger_head=plan.after_ledger,
        )),
    )
    assert corrupt.status is WorkerReceiptReconcileStatus.CORRUPT
    with pytest.raises(WorkerExecutionIntegrityError):
        _classify_worker_execution_receipt(plan, plan.expected_receipt)
    forged_fresh = WorkerExecutionFreshObservation.__new__(
        WorkerExecutionFreshObservation
    )
    with pytest.raises(WorkerExecutionIntegrityError):
        _classify_worker_execution_receipt(plan, forged_fresh)


def test_pipeline_business_create_is_a_distinct_zero_attempt_control_plan() -> None:
    target = _target(WorkerTargetKind.PIPELINE)
    request = PipelineBusinessControlRequest(
        "8" * 32,
        _authority(),
        target,
        _control_intent(target),
        PipelineBusinessControlOperation.CREATE,
        None,
        WorkerExecutionStatus.QUEUED,
        T0,
    )
    plan = _issue_pipeline_business_control_plan(request)
    assert type(plan) is PipelineBusinessControlPlan
    assert not isinstance(plan, WorkerExecutionPlan)
    assert plan.protocol == PIPELINE_BUSINESS_CONTROL_PROTOCOL
    assert plan.operation is PipelineBusinessControlOperation.CREATE
    assert not isinstance(plan.operation, WorkerExecutionOperation)
    assert plan.closure.after_state.attempt_count == 0
    assert plan.closure.after_state.current_attempt_id is None
    assert plan.closure.after_state.current_effect is None
    assert plan.closure.after_state.unresolved_effect is None
    assert plan.before_ledger == EMPTY_WORKER_LEDGER_HEAD
    assert plan.expected_receipt.receipt_parent.transaction_id == (
        PIPELINE_BUSINESS_CONTROL_TRANSACTION_PREFIX + "8" * 32
    )
    assert plan.expected_receipt.receipt_parent.kind == (
        PIPELINE_BUSINESS_CONTROL_TRANSACTION_KIND
    )
    assert plan.expected_receipt.request_class == "pipeline_business_control"
    assert plan.expected_receipt.operation is PipelineBusinessControlOperation.CREATE
    assert plan.expected_receipt.target_kind is WorkerTargetKind.PIPELINE
    assert plan.expected_receipt.target_id == plan.target.target_id
    assert plan.expected_receipt.before_status is None
    assert plan.expected_receipt.after_status is WorkerExecutionStatus.QUEUED
    assert plan.expected_receipt.before_state_revision is None
    assert plan.expected_receipt.after_state_revision == 1
    assert plan.expected_receipt.previous_ledger_tail_sha256 == b"\0" * 32
    assert _require_pipeline_business_control_plan(plan) is plan
    manifest = json.loads(plan.closure.manifest_bytes)
    assert manifest["protocol"] == PIPELINE_BUSINESS_CONTROL_PROTOCOL
    assert manifest["before"] is None
    assert manifest["after"]["status"] == "queued"
    assert manifest["control_intent"] == {
        "origin_commit_id": target.origin_commit_id,
        "intent_type": target.intent_type,
        "idempotency_key": target.idempotency_key,
        "payload": {
            "schema": target.payload.schema,
            "codec_id": target.payload.codec_id,
            "codec_version": target.payload.codec_version,
            "bytes": len(target.payload.payload_bytes),
            "sha256": target.payload.payload_sha256.hex(),
        },
        "replay_policy": target.replay_policy.value,
        "idempotency_policy": target.idempotency_policy.value,
        "max_attempts": target.max_attempts,
    }
    assert PIPELINE_BUSINESS_CONTROL_PROTOCOL.endswith(".v2")
    assert PIPELINE_BUSINESS_CONTROL_MANIFEST_HASH_DOMAIN.endswith(b".v2")
    assert PIPELINE_BUSINESS_CONTROL_RECEIPT_HASH_DOMAIN.endswith(b".v2")
    assert "Unmounted V8" in PipelineBusinessControlPlan.__doc__
    assert "No current Atomic writer" in PipelineBusinessControlRequest.__doc__


def test_pipeline_business_control_digest_has_a_fixed_separate_vector() -> None:
    target = _target(WorkerTargetKind.PIPELINE)
    request = PipelineBusinessControlRequest(
        "8" * 32,
        _authority(),
        target,
        _control_intent(target),
        PipelineBusinessControlOperation.CREATE,
        None,
        WorkerExecutionStatus.QUEUED,
        T0,
    )
    plan = _issue_pipeline_business_control_plan(request)
    assert len(plan.closure.manifest_bytes) == 2220
    assert plan.closure.request_sha256.hex() == (
        "2d11cffb1835e919960107575ebf0cdba09c67696159357e3b924445d4cc3577"
    )
    assert plan.closure.after_state_sha256.hex() == (
        "875ce8f97408743788ef72b7c1df9e437ab560f4023f30d20492764d68182044"
    )
    assert plan.expected_receipt.receipt_sha256.hex() == (
        "1ed0d179abee169385e0ee07d829ecd42e5c6ba6a33836089fc7bb5a0fe40b2d"
    )
    assert plan.expected_receipt.ledger_tail_sha256.hex() == (
        "df775c4cbe8abe5e0324c00625247f3785e4a6dec5216a97a0a4252ca8ebc472"
    )


def test_pipeline_business_control_changes_eligibility_and_preserves_work() -> None:
    queued = _queued(WorkerTargetKind.PIPELINE)
    target = _target(WorkerTargetKind.PIPELINE)
    pause = PipelineBusinessControlRequest(
        "7" * 32,
        _authority(),
        target,
        _control_intent(target, intent_type="pause"),
        PipelineBusinessControlOperation.APPLY_CONTROL_INTENT,
        queued,
        WorkerExecutionStatus.PAUSED,
        T1,
    )
    paused = _issue_pipeline_business_control_plan(pause)
    assert paused.closure.after_state.status is WorkerExecutionStatus.PAUSED
    assert paused.closure.after_state.attempt_count == queued.attempt_count

    paused_before = WorkerBeforeState(
        status=WorkerExecutionStatus.PAUSED,
        state_revision=2,
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
    direct_claim = WorkerClaimRequest(
        _header(WorkerTargetKind.PIPELINE, paused_before, T1),
        WorkerAttemptStart(ATTEMPT_ID, 1, None, None, "worker-1", 1, T1, T2),
    )
    with pytest.raises(WorkerExecutionIntegrityError) as rejected:
        _issue_worker_execution_plan(direct_claim)
    assert rejected.value.code == "worker_execution_claim_invalid"

    resume = dataclasses.replace(
        pause,
        request_transaction_id="3" * 32,
        before=paused_before,
        target_status=WorkerExecutionStatus.QUEUED,
        occurred_at=T2,
    )
    resumed = _issue_pipeline_business_control_plan(resume)
    assert resumed.closure.after_state.status is WorkerExecutionStatus.QUEUED
    assert resumed.closure.after_state.attempt_count == 0
    assert resumed.closure.after_state.current_attempt_id is None

    for index, idle in enumerate((queued, paused_before), start=1):
        immediate_cancel = dataclasses.replace(
            pause,
            request_transaction_id=str(index) * 32,
            before=idle,
            target_status=WorkerExecutionStatus.CANCELLED,
        )
        terminal = _issue_pipeline_business_control_plan(immediate_cancel)
        assert terminal.closure.after_state.status is WorkerExecutionStatus.CANCELLED
        assert terminal.closure.after_state.current_attempt_id is None
        assert terminal.closure.after_state.cancellation_pending is False

        stuck_shape = dataclasses.replace(
            immediate_cancel,
            request_transaction_id=str(index + 7) * 32,
            target_status=WorkerExecutionStatus.CANCEL_REQUESTED,
        )
        with pytest.raises(WorkerExecutionIntegrityError) as rejected:
            _issue_pipeline_business_control_plan(stuck_shape)
        assert rejected.value.code == "pipeline_business_control_edge_invalid"

    running = _active(WorkerTargetKind.PIPELINE, effect=True)
    cancel = dataclasses.replace(
        pause,
        request_transaction_id="6" * 32,
        before=running,
        target_status=WorkerExecutionStatus.CANCEL_REQUESTED,
    )
    cancelled_requested = _issue_pipeline_business_control_plan(cancel)
    after = cancelled_requested.closure.after_state
    assert after.current_attempt_id == running.current_attempt_id
    assert after.current_attempt_number == running.current_attempt_number
    assert after.lease_fence == running.lease_fence
    assert after.current_effect == running.current_effect
    assert after.cancellation_pending is True


@pytest.mark.parametrize(
    "forbidden_status",
    (
        WorkerExecutionStatus.RUNNING,
        WorkerExecutionStatus.RETRY,
        WorkerExecutionStatus.SUCCEEDED,
        WorkerExecutionStatus.FAILED,
        WorkerExecutionStatus.CANCEL_REQUESTED,
        WorkerExecutionStatus.OUTCOME_UNKNOWN,
        WorkerExecutionStatus.REPAIR_REQUIRED,
    ),
)
def test_pipeline_business_control_cannot_claim_complete_or_reconcile(
    forbidden_status,
) -> None:
    target = _target(WorkerTargetKind.PIPELINE)
    request = PipelineBusinessControlRequest(
        "5" * 32,
        _authority(),
        target,
        _control_intent(target),
        PipelineBusinessControlOperation.APPLY_CONTROL_INTENT,
        _queued(WorkerTargetKind.PIPELINE),
        forbidden_status,
        T1,
    )
    with pytest.raises(WorkerExecutionIntegrityError) as rejected:
        _issue_pipeline_business_control_plan(request)
    assert rejected.value.code == "pipeline_business_control_edge_invalid"


def test_pipeline_business_and_worker_issuers_reject_each_others_requests_and_plans() -> None:
    target = _target(WorkerTargetKind.PIPELINE)
    control_request = PipelineBusinessControlRequest(
        "4" * 32,
        _authority(),
        target,
        _control_intent(target),
        PipelineBusinessControlOperation.CREATE,
        None,
        WorkerExecutionStatus.QUEUED,
        T0,
    )
    control_plan = _issue_pipeline_business_control_plan(control_request)
    worker_plan = _issue_worker_execution_plan(_claim(WorkerTargetKind.PIPELINE))
    with pytest.raises(WorkerExecutionIntegrityError):
        _issue_worker_execution_plan(control_request)
    with pytest.raises(WorkerExecutionIntegrityError):
        _issue_pipeline_business_control_plan(_claim(WorkerTargetKind.PIPELINE))
    with pytest.raises(WorkerExecutionIntegrityError) as rejected:
        _require_worker_execution_plan(control_plan)
    assert rejected.value.code == "worker_execution_plan_invalid"
    with pytest.raises(WorkerExecutionIntegrityError) as rejected:
        _require_pipeline_business_control_plan(worker_plan)
    assert rejected.value.code == "pipeline_business_control_plan_invalid"

    absent = _classify_pipeline_business_control_receipt(
        control_plan,
        _fresh_control_observation(PipelineBusinessControlPersistedObservation(
            WorkerPersistedReadStatus.COMPLETE,
            ledger_head=control_plan.before_ledger,
        )),
    )
    assert absent.status is WorkerReceiptReconcileStatus.ABSENT
    exact = _classify_pipeline_business_control_receipt(
        control_plan,
        _fresh_control_observation(PipelineBusinessControlPersistedObservation(
            WorkerPersistedReadStatus.COMPLETE,
            receipt=control_plan.expected_receipt.receipt_parent,
            control_receipt=control_plan.expected_receipt,
            ledger_head=control_plan.after_ledger,
        )),
    )
    assert exact.status is WorkerReceiptReconcileStatus.EXACT
    conflicting_request = PipelineBusinessControlRequest(
        control_request.request_transaction_id,
        control_request.authority,
        control_request.target,
        dataclasses.replace(
            control_request.control_intent,
            intent_type="pause",
        ),
        PipelineBusinessControlOperation.APPLY_CONTROL_INTENT,
        _queued(WorkerTargetKind.PIPELINE),
        WorkerExecutionStatus.PAUSED,
        T1,
    )
    conflicting_plan = _issue_pipeline_business_control_plan(
        conflicting_request
    )
    conflict = _classify_pipeline_business_control_receipt(
        control_plan,
        _fresh_control_observation(PipelineBusinessControlPersistedObservation(
            WorkerPersistedReadStatus.COMPLETE,
            receipt=conflicting_plan.expected_receipt.receipt_parent,
            control_receipt=conflicting_plan.expected_receipt,
            ledger_head=conflicting_plan.after_ledger,
        )),
    )
    assert conflict.status is WorkerReceiptReconcileStatus.CONFLICT
    with pytest.raises(WorkerExecutionIntegrityError) as rejected:
        _classify_pipeline_business_control_receipt(
            worker_plan,
            _fresh_control_observation(PipelineBusinessControlPersistedObservation(
                WorkerPersistedReadStatus.COMPLETE,
                ledger_head=control_plan.before_ledger,
            )),
        )
    assert rejected.value.code == "pipeline_business_control_plan_invalid"
    with pytest.raises(WorkerExecutionIntegrityError) as rejected:
        _classify_worker_execution_receipt(
            control_plan,
            _fresh_worker_observation(WorkerExecutionPersistedObservation(
                WorkerPersistedReadStatus.COMPLETE,
                ledger_head=worker_plan.before_ledger,
            )),
        )
    assert rejected.value.code == "worker_execution_plan_invalid"


def test_worker_and_control_receipts_share_one_global_ledger_without_identity_overlap() -> None:
    worker = _issue_worker_execution_plan(_claim(WorkerTargetKind.ACTION))
    target = _target(WorkerTargetKind.PIPELINE)
    control_request = PipelineBusinessControlRequest(
        "5" * 32,
        _authority(),
        target,
        _control_intent(target),
        PipelineBusinessControlOperation.CREATE,
        None,
        WorkerExecutionStatus.QUEUED,
        T1,
    )
    control = _issue_pipeline_business_control_plan(
        control_request,
        before_ledger=worker.after_ledger,
    )
    assert control.expected_receipt.sequence == 2
    assert control.expected_receipt.previous_ledger_tail_sha256 == (
        worker.expected_receipt.ledger_tail_sha256
    )
    assert worker.expected_receipt.receipt_parent.transaction_id.startswith(
        WORKER_EXECUTION_TRANSACTION_PREFIX
    )
    assert worker.expected_receipt.receipt_parent.kind == (
        WORKER_EXECUTION_TRANSACTION_KIND
    )
    assert control.expected_receipt.receipt_parent.transaction_id.startswith(
        PIPELINE_BUSINESS_CONTROL_TRANSACTION_PREFIX
    )
    assert control.expected_receipt.receipt_parent.kind == (
        PIPELINE_BUSINESS_CONTROL_TRANSACTION_KIND
    )


def test_pipeline_business_control_deep_tamper_is_rebuilt_and_rejected() -> None:
    target = _target(WorkerTargetKind.PIPELINE)
    request = PipelineBusinessControlRequest(
        "2" * 32,
        _authority(),
        target,
        _control_intent(target),
        PipelineBusinessControlOperation.CREATE,
        None,
        WorkerExecutionStatus.QUEUED,
        T0,
    )
    plan = _issue_pipeline_business_control_plan(request)
    object.__setattr__(plan.request.target.payload, "payload_bytes", b"{}")
    with pytest.raises(WorkerExecutionIntegrityError) as rejected:
        _require_pipeline_business_control_plan(plan)
    assert rejected.value.code == "pipeline_business_control_request_changed"

    fresh_target = _target(WorkerTargetKind.PIPELINE)
    fresh_request = PipelineBusinessControlRequest(
        "3" * 32,
        _authority(),
        fresh_target,
        _control_intent(fresh_target),
        PipelineBusinessControlOperation.CREATE,
        None,
        WorkerExecutionStatus.QUEUED,
        T0,
    )
    fresh_plan = _issue_pipeline_business_control_plan(fresh_request)
    object.__setattr__(
        fresh_plan.control_intent.payload,
        "payload_bytes",
        b"{}",
    )
    with pytest.raises(WorkerExecutionIntegrityError) as rejected:
        _require_pipeline_business_control_plan(fresh_plan)
    assert rejected.value.code == "pipeline_business_control_request_changed"


def test_plan_is_issuer_stamped_rebuilt_and_tamper_evident() -> None:
    plan = _issue_worker_execution_plan(_claim(WorkerTargetKind.PIPELINE))
    with pytest.raises(AttributeError):
        plan.status = "committed"

    object.__setattr__(plan.target, "target_id", "7" * 32)
    with pytest.raises(WorkerExecutionIntegrityError) as rejected:
        _require_worker_execution_plan(plan)
    assert rejected.value.code == "worker_execution_plan_changed"

    forged = WorkerExecutionPlan.__new__(WorkerExecutionPlan)
    with pytest.raises(WorkerExecutionIntegrityError) as rejected:
        _require_worker_execution_plan(forged)
    assert rejected.value.code == "worker_execution_plan_invalid"


def test_raw_dict_callback_and_path_like_values_never_become_plans() -> None:
    for value in (
        {"operation": "claim"},
        lambda: None,
        Path("repository.sqlite3"),
    ):
        with pytest.raises(WorkerExecutionIntegrityError):
            _issue_worker_execution_plan(value)
    _require_pipeline_business_control_plan,
