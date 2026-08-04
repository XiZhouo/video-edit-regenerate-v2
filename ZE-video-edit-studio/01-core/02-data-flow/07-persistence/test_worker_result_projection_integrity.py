"""Pure worker effect/result projection vectors and tamper tests."""

from __future__ import annotations

import dataclasses
import ast
import inspect
import json
import sys
from pathlib import Path

import pytest


_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from business_codec import encode_payload_bounded  # noqa: E402
from business_transaction_contract import (  # noqa: E402
    AuthorityBinding,
    freeze_json_payload,
)
from worker_execution_contract import (  # noqa: E402
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
    WorkerExecutionOperation,
    WorkerExecutionStatus,
    WorkerExpireRequest,
    WorkerIdempotencyPolicy,
    WorkerLeaseToken,
    WorkerOutboxDispatchCursor,
    WorkerOutboxDispatchFence,
    WorkerOutboxDispatchState,
    WorkerOutboxOrder,
    WorkerReconcileProof,
    WorkerReconcileProofKind,
    WorkerReconcileRequest,
    WorkerReplayPolicy,
    WorkerRequestHeader,
    WorkerTarget,
    WorkerTargetKind,
    WorkerTerminalProof,
    WorkerTerminalProofKind,
)
from worker_execution_integrity import (  # noqa: E402
    WorkerExecutionPlan,
    _issue_worker_execution_plan,
)
import worker_result_projection_integrity as projection_integrity  # noqa: E402
from worker_result_projection_contract import (  # noqa: E402
    EMPTY_WORKER_PROOF_PROJECTION_SHA256,
    MAX_WORKER_PROJECTION_COMPONENT_BYTES,
    MAX_WORKER_PROJECTION_MANIFEST_BYTES,
    MAX_WORKER_PROJECTION_NESTED_PAYLOAD_BYTES,
    WORKER_EFFECT_REQUEST_PROJECTION_SCHEMA,
    WORKER_PROOF_PAYLOAD_PROJECTION_SCHEMA,
    WORKER_RESULT_PAYLOAD_PROJECTION_SCHEMA,
    WORKER_RESULT_PROJECTION_MANIFEST_SCHEMA,
    WORKER_RESULT_PROJECTION_PROTOCOL,
    WorkerProofProjectionClass,
    WorkerResultProjectionContractError,
)
from worker_result_projection_integrity import (  # noqa: E402
    WORKER_EMPTY_PROOF_PROJECTION_HASH_DOMAIN,
    WORKER_EFFECT_REQUEST_PROJECTION_HASH_DOMAIN,
    WORKER_PROOF_PAYLOAD_PROJECTION_HASH_DOMAIN,
    WORKER_PROOF_PROJECTION_ID_HASH_DOMAIN,
    WORKER_RESULT_PAYLOAD_PROJECTION_HASH_DOMAIN,
    WORKER_RESULT_PROJECTION_ID_HASH_DOMAIN,
    WORKER_RESULT_PROJECTION_MANIFEST_HASH_DOMAIN,
    WorkerResultProjectionIntegrityError,
    WorkerResultProjectionPlan,
    _issue_worker_result_projection_plan,
    _require_worker_result_projection_plan,
    _v8_worker_result_mirror_proof_sha256,
)


T0 = "2026-07-12T00:00:00.000Z"
T1 = "2026-07-12T00:00:10.000Z"
T2 = "2026-07-12T00:00:20.000Z"
T3 = "2026-07-12T00:00:30.000Z"
ATTEMPT_ID = "a" * 32
EFFECT_ID = "f" * 32


def _authority() -> AuthorityBinding:
    return AuthorityBinding(
        "sqlite",
        "active",
        "e" * 64,
        7,
        11,
        13,
        "c" * 64,
        "runtime-1",
        b"d" * 32,
    )


def _target(kind: WorkerTargetKind) -> WorkerTarget:
    replay = {
        WorkerTargetKind.PIPELINE: WorkerReplayPolicy.EXACT_CAS_RETRY,
        WorkerTargetKind.ACTION: WorkerReplayPolicy.PROVIDER_IDEMPOTENT,
        WorkerTargetKind.OUTBOX: WorkerReplayPolicy.RECONCILE_BEFORE_RETRY,
    }[kind]
    idempotency = {
        WorkerTargetKind.PIPELINE: (
            WorkerIdempotencyPolicy.DURABLE_RECEIPT_RECONCILE
        ),
        WorkerTargetKind.ACTION: WorkerIdempotencyPolicy.REQUIRED_EXTERNAL_KEY,
        WorkerTargetKind.OUTBOX: (
            WorkerIdempotencyPolicy.DURABLE_RECEIPT_RECONCILE
        ),
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


def _effect_payload():
    return freeze_json_payload(
        {"external": "request"},
        schema="test.effect-request.v1",
    )


def _effect_kind(kind: WorkerTargetKind) -> WorkerEffectKind:
    return {
        WorkerTargetKind.PIPELINE: WorkerEffectKind.PIPELINE_DOCUMENT_COMMIT,
        WorkerTargetKind.ACTION: WorkerEffectKind.ACTION_PROVIDER,
        WorkerTargetKind.OUTBOX: WorkerEffectKind.OUTBOX_PUBLISH,
    }[kind]


def _effect_binding(kind: WorkerTargetKind) -> WorkerEffectBinding:
    return WorkerEffectBinding(
        attempt_id=ATTEMPT_ID,
        attempt_number=1,
        effect_id=EFFECT_ID,
        effect_kind=_effect_kind(kind),
        request_sha256=_effect_payload().payload_sha256,
        idempotency_scope=(
            "pipeline/project-1/2/pipeline-1"
            if kind is WorkerTargetKind.PIPELINE
            else "provider/account-1"
        ),
        idempotency_key="external-key-1",
    )


def _active(kind: WorkerTargetKind, *, effect: bool = False) -> WorkerBeforeState:
    return WorkerBeforeState(
        status=(
            WorkerExecutionStatus.LEASED
            if kind is WorkerTargetKind.OUTBOX
            else WorkerExecutionStatus.RUNNING
        ),
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
        current_effect=(_effect_binding(kind) if effect else None),
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


def _queued(kind: WorkerTargetKind) -> WorkerBeforeState:
    return WorkerBeforeState(
        status=WorkerExecutionStatus.QUEUED,
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


def _header(
    kind: WorkerTargetKind,
    before: WorkerBeforeState,
    occurred_at: str,
    transaction_id: str,
) -> WorkerRequestHeader:
    return WorkerRequestHeader(
        transaction_id,
        _authority(),
        _target(kind),
        before,
        occurred_at,
    )


def _lease() -> WorkerLeaseToken:
    return WorkerLeaseToken(ATTEMPT_ID, 1, "worker-1", 1, T2)


def _effect_link(
    kind: WorkerTargetKind,
    *,
    result_commit: bool = False,
    provider_receipt: bool = False,
) -> WorkerEffectLink:
    effect = _effect_binding(kind)
    return WorkerEffectLink(
        attempt_id=effect.attempt_id,
        attempt_number=effect.attempt_number,
        effect_id=effect.effect_id,
        effect_kind=effect.effect_kind,
        request_sha256=effect.request_sha256,
        result_commit_id=("4" * 32 if result_commit else None),
        provider_receipt=(
            freeze_json_payload(
                {"receipt": "ok"},
                schema="test.provider-receipt.v1",
            )
            if provider_receipt
            else None
        ),
    )


def _outbox_publish_fence() -> WorkerOutboxDispatchFence:
    empty = WorkerOutboxDispatchCursor(0, 0, -1)
    order = WorkerOutboxOrder(1, 7, 0)
    return WorkerOutboxDispatchFence(
        WorkerOutboxDispatchState(2, empty, order),
        WorkerOutboxDispatchState(
            3,
            WorkerOutboxDispatchCursor(1, 7, 0),
            None,
        ),
    )


def _dual_link_complete_plan(kind: WorkerTargetKind) -> WorkerExecutionPlan:
    status = (
        WorkerExecutionStatus.PUBLISHED
        if kind is WorkerTargetKind.OUTBOX
        else WorkerExecutionStatus.SUCCEEDED
    )
    return _issue_worker_execution_plan(
        WorkerCompleteRequest(
            _header(kind, _active(kind, effect=True), T1, {
                WorkerTargetKind.PIPELINE: "1" * 32,
                WorkerTargetKind.ACTION: "2" * 32,
                WorkerTargetKind.OUTBOX: "3" * 32,
            }[kind]),
            _lease(),
            status,
            T1,
            effect_link=_effect_link(
                kind,
                result_commit=True,
                provider_receipt=True,
            ),
            dispatch_fence=(
                _outbox_publish_fence()
                if kind is WorkerTargetKind.OUTBOX
                else None
            ),
        )
    )


def _authorize_plan() -> WorkerExecutionPlan:
    kind = WorkerTargetKind.ACTION
    return _issue_worker_execution_plan(
        WorkerAuthorizeEffectRequest(
            _header(kind, _active(kind), T1, "8" * 32),
            _lease(),
            WorkerEffectAuthorization(
                EFFECT_ID,
                WorkerEffectKind.ACTION_PROVIDER,
                "provider/account-1",
                "external-key-1",
                _effect_payload(),
                T1,
            ),
        )
    )


def _effectless_complete_plan() -> WorkerExecutionPlan:
    kind = WorkerTargetKind.ACTION
    return _issue_worker_execution_plan(
        WorkerCompleteRequest(
            _header(kind, _active(kind), T1, "9" * 32),
            _lease(),
            WorkerExecutionStatus.FAILED_TERMINAL,
            T1,
            error=freeze_json_payload(
                {"error": "failed"},
                schema="test.error.v1",
            ),
        )
    )


def _pipeline_complete_plan() -> WorkerExecutionPlan:
    kind = WorkerTargetKind.PIPELINE
    return _issue_worker_execution_plan(
        WorkerCompleteRequest(
            _header(kind, _active(kind, effect=True), T1, "7" * 32),
            _lease(),
            WorkerExecutionStatus.SUCCEEDED,
            T1,
            effect_link=_effect_link(kind, result_commit=True),
        )
    )


def _durable_complete_plan() -> WorkerExecutionPlan:
    kind = WorkerTargetKind.ACTION
    durable = WorkerDurableResultProof(
        "6" * 32,
        freeze_json_payload({"ok": 1}, schema="test.result.v1"),
        T1,
        _effect_binding(kind),
    )
    return _issue_worker_execution_plan(
        WorkerCompleteRequest(
            _header(kind, _active(kind, effect=True), T1, "5" * 32),
            _lease(),
            WorkerExecutionStatus.SUCCEEDED,
            T1,
            durable_result=durable,
        )
    )


def _reconcile_plan() -> WorkerExecutionPlan:
    kind = WorkerTargetKind.ACTION
    proof = WorkerReconcileProof(
        WorkerReconcileProofKind.PROVIDER_ABSENCE,
        _effect_binding(kind),
        freeze_json_payload(
            {"proof": "exact"},
            schema="test.reconcile.v1",
        ),
    )
    return _issue_worker_execution_plan(
        WorkerReconcileRequest(
            _header(kind, _unknown(kind), T3, "4" * 32),
            ATTEMPT_ID,
            1,
            1,
            WorkerExecutionStatus.RETRY,
            proof,
            T3,
            T3,
        )
    )


def _terminal_complete_plan() -> WorkerExecutionPlan:
    kind = WorkerTargetKind.ACTION
    evidence = freeze_json_payload(
        {"provider": "terminal-failure"},
        schema="test.terminal-proof.v1",
    )
    terminal = WorkerTerminalProof(
        WorkerTerminalProofKind.TERMINAL_FAILURE,
        _effect_binding(kind),
        evidence,
        _effect_link(kind, provider_receipt=True),
    )
    return _issue_worker_execution_plan(
        WorkerCompleteRequest(
            _header(kind, _active(kind, effect=True), T1, "d" * 32),
            _lease(),
            WorkerExecutionStatus.FAILED_TERMINAL,
            T1,
            error=freeze_json_payload(
                {"error": "provider-failed"},
                schema="test.error.v1",
            ),
            terminal_proof=terminal,
        )
    )


def _reconcile_variant(
    kind: WorkerTargetKind,
    status: WorkerExecutionStatus,
    proof_kind: WorkerReconcileProofKind,
    *,
    effect_link: WorkerEffectLink | None = None,
    durable_result: WorkerDurableResultProof | None = None,
    transaction_id: str,
) -> WorkerExecutionPlan:
    proof = WorkerReconcileProof(
        proof_kind,
        _effect_binding(kind),
        freeze_json_payload(
            {"proof": proof_kind.value},
            schema="test.reconcile.v1",
        ),
        effect_link=effect_link,
        durable_result=durable_result,
    )
    return _issue_worker_execution_plan(
        WorkerReconcileRequest(
            _header(kind, _unknown(kind), T3, transaction_id),
            ATTEMPT_ID,
            1,
            1,
            status,
            proof,
            T3,
        )
    )


def test_effect_request_projection_has_fixed_bytes_hashes_and_receipt_join() -> None:
    projected = _issue_worker_result_projection_plan(_authorize_plan())
    effect = projected.closure.effect_request
    assert effect is not None
    assert projected.closure.result is None
    assert projected.closure.proof is None
    assert effect.binding.operation is WorkerExecutionOperation.AUTHORIZE_EFFECT
    assert effect.binding.receipt_parent == effect.binding.execution_receipt.receipt_parent
    assert effect.binding.target_id == "2" * 32
    assert effect.binding.attempt_id == ATTEMPT_ID
    assert effect.binding.effect.effect_id == EFFECT_ID
    assert effect.request_payload.payload_bytes == b'{"external":"request"}'
    metadata = json.loads(effect.projection_manifest_bytes)
    assert metadata["schema"] == WORKER_EFFECT_REQUEST_PROJECTION_SCHEMA
    assert metadata["request_payload"]["sha256"] == (
        effect.request_payload.payload_sha256.hex()
    )
    assert effect.request_payload.payload_bytes not in effect.projection_manifest_bytes
    assert len(effect.projection_manifest_bytes) == 1787
    assert effect.projection_sha256.hex() == (
        "4c5954997dd3ed885c924e087ca40bb3f2c295b59c0621c22cf2887d048bf00e"
    )
    assert len(projected.closure.manifest_bytes) == 2132
    assert projected.closure.manifest_sha256.hex() == (
        "84fdb1965c6b0c52d5bfee9d550561b1ecc81eae657833586c806062b1eb2a1d"
    )
    assert _require_worker_result_projection_plan(projected) is projected


def test_effectless_complete_has_one_result_and_no_invented_proof() -> None:
    projected = _issue_worker_result_projection_plan(_effectless_complete_plan())
    result = projected.closure.result
    assert result is not None
    assert projected.closure.effect_request is None
    assert projected.closure.proof is None
    assert result.after_status is WorkerExecutionStatus.FAILED_TERMINAL
    assert result.committed_at == T1
    assert not hasattr(result, "terminal_status")
    assert not hasattr(result, "completed_at")
    assert result.result_payload is None
    assert result.error_payload.payload_bytes == b'{"error":"failed"}'
    assert result.error_payload.payload_bytes not in result.projection_manifest_bytes
    assert result.proof_sha256 is None
    assert result.result_id == "faaa58b0f86a855295ee2e86b4c1d62c"
    assert len(result.projection_manifest_bytes) == 1561
    assert result.result_sha256.hex() == (
        "eba88ac8c88f5ff227ed1bc0f3c7ae4c552e173e62775f4255d836a720e42628"
    )
    assert projected.closure.manifest_sha256.hex() == (
        "84f4f774f5971cd1517bb4972625f22217f9693d2cd84d5f0cd3e3e46264addf"
    )


def test_empty_proof_sentinel_is_only_the_frozen_v8_result_mirror_value() -> None:
    projected = _issue_worker_result_projection_plan(_effectless_complete_plan())
    assert projected.closure.result.proof_sha256 is None
    assert projected.closure.proof is None
    assert _v8_worker_result_mirror_proof_sha256(projected) == (
        EMPTY_WORKER_PROOF_PROJECTION_SHA256
    )
    assert EMPTY_WORKER_PROOF_PROJECTION_SHA256 != b"\0" * 32
    assert EMPTY_WORKER_PROOF_PROJECTION_SHA256.hex() == (
        "f14fd90bc57f6d54c068aff36091f91a2853b2b4ffa48c148a3a46d8854a3a47"
    )
    assert EMPTY_WORKER_PROOF_PROJECTION_SHA256 == (
        projection_integrity._domain_digest(
            WORKER_EMPTY_PROOF_PROJECTION_HASH_DOMAIN,
            WORKER_RESULT_PROJECTION_PROTOCOL.encode("ascii"),
            b"v8.worker_results.proof_sha256",
            b"no-proof",
        )
    )


def test_v8_result_mirror_uses_real_proof_digest_when_one_exists() -> None:
    projected = _issue_worker_result_projection_plan(_pipeline_complete_plan())
    proof_sha256 = projected.closure.proof.proof_sha256
    assert proof_sha256 != EMPTY_WORKER_PROOF_PROJECTION_SHA256
    assert projected.closure.result.proof_sha256 == proof_sha256
    assert _v8_worker_result_mirror_proof_sha256(projected) == proof_sha256


def test_empty_proof_sentinel_cannot_masquerade_as_a_v9_proof() -> None:
    absent = _issue_worker_result_projection_plan(_effectless_complete_plan())
    with pytest.raises(WorkerResultProjectionContractError) as rejected:
        dataclasses.replace(
            absent.closure.result,
            proof_sha256=EMPTY_WORKER_PROOF_PROJECTION_SHA256,
        )
    assert rejected.value.code == (
        "worker_result_projection_empty_proof_sentinel_reserved"
    )

    present = _issue_worker_result_projection_plan(_pipeline_complete_plan())
    with pytest.raises(WorkerResultProjectionContractError) as rejected:
        dataclasses.replace(
            present.closure.proof,
            proof_sha256=EMPTY_WORKER_PROOF_PROJECTION_SHA256,
        )
    assert rejected.value.code == (
        "worker_result_projection_empty_proof_sentinel_reserved"
    )

    effect_only = _issue_worker_result_projection_plan(_authorize_plan())
    with pytest.raises(WorkerResultProjectionIntegrityError) as rejected:
        _v8_worker_result_mirror_proof_sha256(effect_only)
    assert rejected.value.code == (
        "worker_result_projection_v8_mirror_result_required"
    )


def test_complete_effect_link_has_fixed_result_and_existing_proof_kind() -> None:
    projected = _issue_worker_result_projection_plan(_pipeline_complete_plan())
    result = projected.closure.result
    proof = projected.closure.proof
    assert result is not None and proof is not None
    assert result.result_id == "e41aa7d7cc6aa1aa3c510255abe9e51c"
    assert result.result_payload is None
    assert result.error_payload is None
    assert proof.proof_class is WorkerProofProjectionClass.COMPLETE
    assert proof.proof_kind == WorkerReconcileProofKind.RESULT_COMMIT.value
    assert proof.result_commit_id == "4" * 32
    assert proof.evidence_payload is None
    assert proof.provider_receipt_payload is None
    assert proof.durable_result_payload is None
    assert proof.proof_id == "664d8bd408a00fd3b4f9848e12611946"
    assert json.loads(proof.projection_manifest_bytes)["proof_id"] == proof.proof_id
    assert result.proof_sha256 == proof.proof_sha256
    assert result.result_sha256.hex() == (
        "3b0dd04622b1646b7e112b0d10bbd1397d456abd4ad18db4a68d917433701f66"
    )
    assert proof.proof_sha256.hex() == (
        "989bf6600f39915f7e7bf477fb648a179f4a5b8db7d637cf3345937dc5d6b15b"
    )
    assert projected.closure.manifest_sha256.hex() == (
        "8f5e1bdacab52a4562e0d25053ece5bfd33a0e1743867b0d49dbdbf11e2c0637"
    )


@pytest.mark.parametrize(
    ("kind", "expected_proof_kind"),
    (
        (WorkerTargetKind.PIPELINE, WorkerReconcileProofKind.RESULT_COMMIT),
        (WorkerTargetKind.ACTION, WorkerReconcileProofKind.PROVIDER_RECEIPT),
    ),
)
def test_dual_field_effect_link_proof_kind_comes_from_sealed_target_kind(
    kind: WorkerTargetKind,
    expected_proof_kind: WorkerReconcileProofKind,
) -> None:
    projected = _issue_worker_result_projection_plan(
        _dual_link_complete_plan(kind)
    )
    proof = projected.closure.proof
    assert proof.proof_kind == expected_proof_kind.value
    assert proof.result_commit_id == "4" * 32
    assert proof.provider_receipt_payload.payload_bytes == b'{"receipt":"ok"}'

    if kind is WorkerTargetKind.ACTION:
        assert proof.proof_id == "0c4f4e58141e2d8997e3151511a5281b"
        assert proof.proof_sha256.hex() == (
            "5512daf1ccc5f93f57e51b5559213cf5d8d7bfb3dd83171173ed89f3e79ac848"
        )
        assert projected.closure.result.result_sha256.hex() == (
            "e613a11dec69963680f8856e57e4185dbb5be4721c46d14de2ab5e05fbe2b215"
        )
        assert projected.closure.manifest_sha256.hex() == (
            "e84e18d63b7da909eea90ed1c7a5497dc6ac7d61fca88604b6c72c84fb72ef58"
        )


def test_durable_result_id_is_reused_and_payload_is_owned_only_by_proof() -> None:
    projected = _issue_worker_result_projection_plan(_durable_complete_plan())
    result = projected.closure.result
    proof = projected.closure.proof
    assert result is not None and proof is not None
    assert result.result_id == "6" * 32
    assert result.result_payload is None
    assert result.error_payload is None
    assert not hasattr(result, "durable_result_payload")
    assert proof.proof_kind == WorkerReconcileProofKind.DURABLE_RESULT.value
    assert proof.durable_result_id == "6" * 32
    assert proof.durable_result_recorded_at == T1
    assert proof.durable_result_payload.payload_bytes == b'{"ok":1}'
    assert proof.durable_result_payload.payload_bytes not in proof.projection_manifest_bytes


def test_reconcile_always_projects_result_and_original_evidence_proof() -> None:
    projected = _issue_worker_result_projection_plan(_reconcile_plan())
    result = projected.closure.result
    proof = projected.closure.proof
    assert result is not None and proof is not None
    assert result.after_status is WorkerExecutionStatus.RETRY
    assert result.committed_at == T3
    assert result.result_id == "1b7ab033a6b206ad8380c3fb0f42e3ac"
    assert proof.proof_class is WorkerProofProjectionClass.RECONCILE
    assert proof.proof_kind == WorkerReconcileProofKind.PROVIDER_ABSENCE.value
    assert proof.evidence_payload.payload_bytes == b'{"proof":"exact"}'
    assert proof.provider_receipt_payload is None
    assert proof.durable_result_payload is None
    assert json.loads(proof.projection_manifest_bytes)["evidence_payload"]["sha256"] == (
        proof.evidence_payload.payload_sha256.hex()
    )
    assert result.proof_sha256 == proof.proof_sha256


def test_complete_terminal_proof_owns_evidence_and_provider_payload_once() -> None:
    projected = _issue_worker_result_projection_plan(_terminal_complete_plan())
    result = projected.closure.result
    proof = projected.closure.proof
    assert result is not None and proof is not None
    assert result.error_payload.payload_bytes == b'{"error":"provider-failed"}'
    assert not hasattr(result, "effect_provider_receipt")
    assert not hasattr(result, "durable_result_payload")
    assert proof.proof_class is WorkerProofProjectionClass.COMPLETE
    assert proof.proof_kind == WorkerTerminalProofKind.TERMINAL_FAILURE.value
    assert proof.evidence_payload.payload_bytes == (
        b'{"provider":"terminal-failure"}'
    )
    assert proof.provider_receipt_payload.payload_bytes == b'{"receipt":"ok"}'
    assert proof.durable_result_payload is None
    assert proof.evidence_payload.payload_bytes not in proof.projection_manifest_bytes
    assert proof.provider_receipt_payload.payload_bytes not in proof.projection_manifest_bytes


def test_complete_outcome_unknown_and_retry_have_results_without_fake_proofs() -> None:
    kind = WorkerTargetKind.ACTION
    requests = (
        WorkerCompleteRequest(
            _header(kind, _active(kind, effect=True), T1, "e" * 32),
            _lease(),
            WorkerExecutionStatus.OUTCOME_UNKNOWN,
            T1,
        ),
        WorkerCompleteRequest(
            _header(kind, _active(kind), T1, "c" * 32),
            _lease(),
            WorkerExecutionStatus.RETRY,
            T1,
            next_attempt_at=T1,
        ),
    )
    for request in requests:
        projected = _issue_worker_result_projection_plan(
            _issue_worker_execution_plan(request)
        )
        assert projected.closure.result is not None
        assert projected.closure.result.after_status is request.target_status
        assert projected.closure.result.proof_sha256 is None
        assert projected.closure.proof is None


def test_result_owns_top_level_payload_while_effect_link_is_proof_owned() -> None:
    kind = WorkerTargetKind.PIPELINE
    top_level = freeze_json_payload(
        {"document": "committed"},
        schema="test.pipeline-result.v1",
    )
    plan = _issue_worker_execution_plan(
        WorkerCompleteRequest(
            _header(kind, _active(kind, effect=True), T1, "b" * 32),
            _lease(),
            WorkerExecutionStatus.SUCCEEDED,
            T1,
            result=top_level,
            effect_link=_effect_link(kind, result_commit=True),
        )
    )
    projected = _issue_worker_result_projection_plan(plan)
    result = projected.closure.result
    proof = projected.closure.proof
    assert result.result_payload == top_level
    assert result.result_payload is plan.request.result
    assert result.error_payload is None
    assert proof.result_commit_id == "4" * 32
    assert top_level.payload_bytes not in result.projection_manifest_bytes
    assert "effect_link" not in json.loads(result.projection_manifest_bytes)


@pytest.mark.parametrize(
    ("plan", "status", "proof_kind", "durable_id"),
    (
        (
            _reconcile_variant(
                WorkerTargetKind.PIPELINE,
                WorkerExecutionStatus.SUCCEEDED,
                WorkerReconcileProofKind.RESULT_COMMIT,
                effect_link=_effect_link(
                    WorkerTargetKind.PIPELINE,
                    result_commit=True,
                ),
                transaction_id="1" * 32,
            ),
            WorkerExecutionStatus.SUCCEEDED,
            WorkerReconcileProofKind.RESULT_COMMIT,
            None,
        ),
        (
            _reconcile_variant(
                WorkerTargetKind.ACTION,
                WorkerExecutionStatus.SUCCEEDED,
                WorkerReconcileProofKind.DURABLE_RESULT,
                durable_result=WorkerDurableResultProof(
                    "0" * 32,
                    freeze_json_payload(
                        {"result": "durable"},
                        schema="test.durable-result.v1",
                    ),
                    T1,
                    _effect_binding(WorkerTargetKind.ACTION),
                ),
                transaction_id="0" * 32,
            ),
            WorkerExecutionStatus.SUCCEEDED,
            WorkerReconcileProofKind.DURABLE_RESULT,
            "0" * 32,
        ),
        (
            _reconcile_variant(
                WorkerTargetKind.ACTION,
                WorkerExecutionStatus.REPAIR_REQUIRED,
                WorkerReconcileProofKind.STILL_UNKNOWN,
                transaction_id="6" * 32,
            ),
            WorkerExecutionStatus.REPAIR_REQUIRED,
            WorkerReconcileProofKind.STILL_UNKNOWN,
            None,
        ),
        (
            _reconcile_variant(
                WorkerTargetKind.ACTION,
                WorkerExecutionStatus.FAILED_TERMINAL,
                WorkerReconcileProofKind.TERMINAL_FAILURE,
                transaction_id="a" * 32,
            ),
            WorkerExecutionStatus.FAILED_TERMINAL,
            WorkerReconcileProofKind.TERMINAL_FAILURE,
            None,
        ),
    ),
)
def test_reconcile_projection_matrix_is_owned_by_the_sealed_plan(
    plan: WorkerExecutionPlan,
    status: WorkerExecutionStatus,
    proof_kind: WorkerReconcileProofKind,
    durable_id: str | None,
) -> None:
    projected = _issue_worker_result_projection_plan(plan)
    result = projected.closure.result
    proof = projected.closure.proof
    assert result is not None and proof is not None
    assert result.after_status is status
    assert proof.proof_class is WorkerProofProjectionClass.RECONCILE
    assert proof.proof_kind == proof_kind.value
    assert proof.evidence_payload is plan.request.proof.evidence
    assert proof.durable_result_id == durable_id
    if durable_id is not None:
        assert result.result_id == durable_id
        assert proof.durable_result_payload is plan.request.proof.durable_result.result
    assert _require_worker_result_projection_plan(projected) is projected


def test_derived_result_identity_ignores_payload_but_digest_does_not() -> None:
    first = _effectless_complete_plan()
    changed_request = dataclasses.replace(
        first.request,
        error=freeze_json_payload(
            {"error": "different"},
            schema="test.error.v1",
        ),
    )
    changed = _issue_worker_execution_plan(changed_request)
    first_projection = _issue_worker_result_projection_plan(first).closure.result
    changed_projection = _issue_worker_result_projection_plan(changed).closure.result
    assert first_projection.result_id == changed_projection.result_id
    assert first_projection.result_sha256 != changed_projection.result_sha256


def test_projection_metadata_bytes_use_the_canonical_codec() -> None:
    projected = _issue_worker_result_projection_plan(_terminal_complete_plan())
    components = (
        projected.closure.result.projection_manifest_bytes,
        projected.closure.proof.projection_manifest_bytes,
        projected.closure.manifest_bytes,
    )
    for component in components:
        assert encode_payload_bounded(
            json.loads(component),
            MAX_WORKER_PROJECTION_MANIFEST_BYTES,
            100_000,
        ) == component


def test_nonprojection_operations_including_effectful_expire_are_rejected() -> None:
    kind = WorkerTargetKind.ACTION
    requests = (
        WorkerClaimRequest(
            _header(kind, _queued(kind), T0, "3" * 32),
            WorkerAttemptStart(
                ATTEMPT_ID,
                1,
                None,
                None,
                "worker-1",
                1,
                T0,
                T2,
            ),
        ),
        WorkerExpireRequest(
            _header(kind, _active(kind, effect=True), T2, "2" * 32),
            _lease(),
            T2,
            WorkerExecutionStatus.OUTCOME_UNKNOWN,
        ),
    )
    for request in requests:
        worker_plan = _issue_worker_execution_plan(request)
        with pytest.raises(WorkerResultProjectionIntegrityError) as rejected:
            _issue_worker_result_projection_plan(worker_plan)
        assert rejected.value.code == "worker_result_projection_not_applicable"


def test_projection_issuer_rejects_changed_worker_plan_before_copying_payload() -> None:
    worker_plan = _authorize_plan()
    object.__setattr__(
        worker_plan.request.effect.request,
        "payload_bytes",
        b'{"external":"tampered"}',
    )
    with pytest.raises(WorkerResultProjectionIntegrityError) as rejected:
        _issue_worker_result_projection_plan(worker_plan)
    assert rejected.value.code == "worker_result_projection_worker_plan_invalid"


@pytest.mark.parametrize(
    "field",
    ("result_id", "result_sha256", "projection_manifest_bytes", "proof_sha256"),
)
def test_projection_revalidation_rejects_bottom_layer_result_tamper(field: str) -> None:
    projected = _issue_worker_result_projection_plan(_pipeline_complete_plan())
    result = projected.closure.result
    replacement = {
        "result_id": "0" * 32,
        "result_sha256": b"x" * 32,
        "projection_manifest_bytes": b"{}",
        "proof_sha256": b"y" * 32,
    }[field]
    object.__setattr__(result, field, replacement)
    with pytest.raises(WorkerResultProjectionIntegrityError) as rejected:
        _require_worker_result_projection_plan(projected)
    assert rejected.value.code == "worker_result_projection_plan_changed"


def test_projection_revalidation_rejects_receipt_target_and_payload_alias_tamper() -> None:
    projected = _issue_worker_result_projection_plan(_effectless_complete_plan())
    object.__setattr__(
        projected.closure.result,
        "error_payload",
        freeze_json_payload({"error": "other"}, schema="test.error.v1"),
    )
    with pytest.raises(WorkerResultProjectionIntegrityError) as rejected:
        _require_worker_result_projection_plan(projected)
    assert rejected.value.code == "worker_result_projection_plan_changed"

    projected = _issue_worker_result_projection_plan(_pipeline_complete_plan())
    object.__setattr__(projected.closure.proof, "proof_id", "0" * 32)
    with pytest.raises(WorkerResultProjectionIntegrityError) as rejected:
        _require_worker_result_projection_plan(projected)
    assert rejected.value.code == "worker_result_projection_plan_changed"

    projected = _issue_worker_result_projection_plan(_pipeline_complete_plan())
    object.__setattr__(projected.binding, "target_id", "0" * 32)
    with pytest.raises(WorkerResultProjectionIntegrityError) as rejected:
        _require_worker_result_projection_plan(projected)
    assert rejected.value.code == "worker_result_projection_plan_changed"

    projected = _issue_worker_result_projection_plan(_pipeline_complete_plan())
    object.__setattr__(
        projected.binding,
        "receipt_parent",
        dataclasses.replace(
            projected.binding.receipt_parent,
            committed_at=T0,
        ),
    )
    with pytest.raises(WorkerResultProjectionIntegrityError) as rejected:
        _require_worker_result_projection_plan(projected)
    assert rejected.value.code == "worker_result_projection_plan_changed"


def test_nested_payload_budget_is_enforced_without_allocating_a_huge_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        projection_integrity,
        "MAX_WORKER_PROJECTION_NESTED_PAYLOAD_BYTES",
        1,
    )
    with pytest.raises(WorkerResultProjectionIntegrityError) as rejected:
        _issue_worker_result_projection_plan(_authorize_plan())
    assert rejected.value.code == (
        "worker_result_projection_nested_payload_budget_exceeded"
    )


def test_protocol_domains_schemas_and_budgets_are_explicitly_versioned() -> None:
    assert WORKER_RESULT_PROJECTION_PROTOCOL.endswith(".v1")
    assert WORKER_RESULT_PROJECTION_MANIFEST_SCHEMA.endswith(".v1")
    assert WORKER_RESULT_PAYLOAD_PROJECTION_SCHEMA.endswith(".v1")
    assert WORKER_PROOF_PAYLOAD_PROJECTION_SCHEMA.endswith(".v1")
    for domain in (
        WORKER_EFFECT_REQUEST_PROJECTION_HASH_DOMAIN,
        WORKER_RESULT_PAYLOAD_PROJECTION_HASH_DOMAIN,
        WORKER_PROOF_PAYLOAD_PROJECTION_HASH_DOMAIN,
        WORKER_RESULT_PROJECTION_MANIFEST_HASH_DOMAIN,
        WORKER_RESULT_PROJECTION_ID_HASH_DOMAIN,
        WORKER_PROOF_PROJECTION_ID_HASH_DOMAIN,
        WORKER_EMPTY_PROOF_PROJECTION_HASH_DOMAIN,
    ):
        assert domain.endswith(b".v1")
    assert MAX_WORKER_PROJECTION_MANIFEST_BYTES == (
        MAX_WORKER_PROJECTION_COMPONENT_BYTES
    )
    assert MAX_WORKER_PROJECTION_NESTED_PAYLOAD_BYTES > (
        MAX_WORKER_PROJECTION_COMPONENT_BYTES
    )


def test_projection_plan_cannot_be_forged_or_mutated() -> None:
    with pytest.raises(WorkerResultProjectionIntegrityError) as rejected:
        WorkerResultProjectionPlan(None, _issuer=object())
    assert rejected.value.code == "worker_result_projection_plan_issuer_invalid"

    projected = _issue_worker_result_projection_plan(_authorize_plan())
    with pytest.raises(AttributeError):
        projected.status = "committed"


def test_projection_issuer_has_one_sealed_plan_gate_and_no_second_state_machine() -> None:
    tree = ast.parse(inspect.getsource(projection_integrity))
    require_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_require_worker_execution_plan"
    ]
    assert len(require_calls) == 1
    source = inspect.getsource(projection_integrity)
    for forbidden in (
        "def _frame_bytes",
        "_issue_worker_execution_plan",
        "_validate_request",
        "_COMPLETE_AFTER",
        "_RECONCILE_AFTER",
        "_require_success_proof",
        "sqlite3",
        "open(",
    ):
        assert forbidden not in source

    update_frame = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_update_frame"
    )
    updates = [
        node
        for node in ast.walk(update_frame)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "hasher"
        and node.func.attr == "update"
    ]
    assert len(updates) == 2
    assert not any(
        isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add)
        for node in ast.walk(update_frame)
    )


def test_contract_rejects_receipt_time_and_proof_pointer_mismatch() -> None:
    projected = _issue_worker_result_projection_plan(_pipeline_complete_plan())
    with pytest.raises(WorkerResultProjectionContractError) as rejected:
        dataclasses.replace(projected.closure.result, committed_at=T0)
    assert rejected.value.code == "worker_result_projection_receipt_binding_invalid"

    with pytest.raises(WorkerResultProjectionContractError) as rejected:
        dataclasses.replace(projected.closure.result, proof_sha256=b"z" * 31)
    assert rejected.value.code == "worker_result_projection_digest_invalid"

    with pytest.raises(WorkerResultProjectionContractError) as rejected:
        dataclasses.replace(
            projected.closure.result,
            projection_manifest_bytes=b'{"not": "canonical"}',
        )
    assert rejected.value.code == "worker_result_projection_metadata_invalid"
