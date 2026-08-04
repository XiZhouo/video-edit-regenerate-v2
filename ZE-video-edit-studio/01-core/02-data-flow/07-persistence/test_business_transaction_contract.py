"""Pure typed input and retry-disposition contract tests."""

from __future__ import annotations

import dataclasses
import hashlib
import sys
from pathlib import Path

import pytest


_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from business_transaction_contract import (  # noqa: E402
    ATOMIC_COMMIT_STATUS_ABORTED,
    ATOMIC_COMMIT_STATUS_INDETERMINATE,
    ActionReplayPolicy,
    AtomicCommitFailure,
    AtomicCommitResult,
    AtomicFailureKind,
    AtomicRetryDisposition,
    AuthorityBinding,
    BeforeBusinessHead,
    BusinessTransactionContractError,
    ConfirmationConsumption,
    DurableActionIntent,
    IdempotencyPolicy,
    InboxClaim,
    InboxOutcomeKind,
    PipelineIntent,
    PipelineIntentKind,
    PipelineJobStatus,
    PipelineReplayPolicy,
    freeze_json_payload,
)


def _payload():
    return freeze_json_payload({"z": [1, True], "a": "中"}, schema="test.payload.v1")


def test_payload_is_canonical_immutable_and_tamper_evident() -> None:
    source = {"z": [1, True], "a": "中"}
    payload = freeze_json_payload(source, schema="test.payload.v1")
    source["z"].append(2)
    assert payload.payload_bytes == b'{"a":"\xe4\xb8\xad","z":[1,true]}'
    with pytest.raises(dataclasses.FrozenInstanceError):
        payload.schema = "changed"
    with pytest.raises(BusinessTransactionContractError):
        dataclasses.replace(payload, payload_sha256=b"x" * 32)
    with pytest.raises(BusinessTransactionContractError) as rejected:
        freeze_json_payload(lambda: None, schema="test.payload.v1")
    assert rejected.value.__cause__ is None
    assert rejected.value.__context__ is None


def test_authority_before_and_ids_use_exact_repository_widths() -> None:
    AuthorityBinding(
        authority="sqlite",
        authority_state="active",
        authority_epoch="e" * 64,
        source_admission_revision=1,
        authority_revision=2,
        control_revision=3,
        cutover_id="c" * 64,
        runtime_generation="runtime-1",
        code_digest=b"d" * 32,
    )
    before = BeforeBusinessHead(
        project_id="project-1",
        generation=1,
        document_id="document-1",
        document_version=1,
        project_revision=2,
        head_revision=3,
        expected_last_commit_id="a" * 32,
        business_root_sha256=b"r" * 32,
        operation_tail_hash="0" * 64,
    )
    assert (before.project_revision, before.head_revision) == (2, 3)
    with pytest.raises(BusinessTransactionContractError):
        dataclasses.replace(before, project_revision=0)
    with pytest.raises(BusinessTransactionContractError):
        dataclasses.replace(before, head_revision=0)
    with pytest.raises(BusinessTransactionContractError):
        AuthorityBinding(
            "sqlite", "active", "e" * 32, 1, 2, 3,
            "c" * 64, "r", b"d" * 32,
        )
    with pytest.raises(BusinessTransactionContractError):
        AuthorityBinding(
            "sqlite", "active", "e" * 64, 0, 2, 3,
            "c" * 64, "r", b"d" * 32,
        )
    with pytest.raises(BusinessTransactionContractError):
        BeforeBusinessHead(
            "p", 1, "d", 0, 1, 1, "a" * 32, b"r" * 32, "0" * 64
        )


@pytest.mark.parametrize(
    "kind,retry",
    [
        (AtomicFailureKind.EXACT_NOT_COMMITTED, AtomicRetryDisposition.EXACT_RETRY),
        (AtomicFailureKind.HEAD_CONFLICT, AtomicRetryDisposition.RELOAD_AND_REAPPLY),
        (AtomicFailureKind.OUTCOME_UNKNOWN, AtomicRetryDisposition.NEVER),
        (AtomicFailureKind.INPUT_REJECTED, AtomicRetryDisposition.NEVER),
    ],
)
def test_retry_disposition_is_derived_not_caller_selected(kind, retry) -> None:
    failure = AtomicCommitFailure(kind=kind, code="classified")
    assert failure.retry is retry
    status = (
        ATOMIC_COMMIT_STATUS_INDETERMINATE
        if kind is AtomicFailureKind.OUTCOME_UNKNOWN
        else ATOMIC_COMMIT_STATUS_ABORTED
    )
    result = AtomicCommitResult("a" * 32, status, b"m" * 32, failure)
    assert result.retry is retry


def test_inbox_and_confirmation_bind_claim_fences_without_plain_nonce() -> None:
    request = _payload()
    claim = InboxClaim(
        scope="project/project-1",
        inbox_key="client-request-1",
        actor_id="user-1",
        claim_owner_id="2" * 32,
        claim_fence=3,
        state_revision=4,
        outcome_kind=InboxOutcomeKind.COMMIT,
        request_sha256=request.payload_sha256,
        request=request,
        response=freeze_json_payload(
            {"result": "commit"}, schema="test.response.v1"
        ),
    )
    assert claim.outcome_kind is InboxOutcomeKind.COMMIT
    confirmation = ConfirmationConsumption(
        confirmation_id="4" * 32,
        nonce_hmac=hashlib.sha256(b"nonce/key/domain").digest(),
        nonce_key_id="confirmation-hmac-2026-01",
        reservation_fence=5,
        reservation_owner_id="6" * 32,
        expected_revision=7,
        expected_request_sha256=request.payload_sha256,
        actor_id="user-1",
        resolution=freeze_json_payload(
            {"decision": "confirmed"},
            schema="test.confirmation-resolution.v1",
        ),
    )
    assert confirmation.decision == "confirmed"
    assert confirmation.expected_request_sha256 == request.payload_sha256
    assert confirmation.resolution.schema == "test.confirmation-resolution.v1"
    assert not hasattr(confirmation, "token")
    assert not hasattr(confirmation, "nonce")
    with pytest.raises(BusinessTransactionContractError):
        dataclasses.replace(confirmation, decision="approved")
    with pytest.raises(BusinessTransactionContractError):
        dataclasses.replace(confirmation, expected_request_sha256=b"short")
    with pytest.raises(BusinessTransactionContractError):
        dataclasses.replace(confirmation, resolution=object())


def test_action_and_pipeline_replay_policies_are_explicit_and_consistent() -> None:
    payload = _payload()
    action = DurableActionIntent(
        ordinal=0,
        action_id="a" * 32,
        action_type="notify.push",
        idempotency_key="provider-key",
        payload=payload,
        replay_policy=ActionReplayPolicy.RECONCILE_BEFORE_RETRY,
        idempotency_policy=IdempotencyPolicy.DURABLE_RECEIPT_RECONCILE,
        idempotency_scope="provider/account-1",
    )
    assert action.max_attempts == 10
    transition = PipelineIntent(
        ordinal=0,
        intent_id="b" * 32,
        job_id="c" * 32,
        pipeline_id="render-pipeline",
        intent_type="pause",
        idempotency_key="pipeline-transition-1",
        payload=payload,
        intent_kind=PipelineIntentKind.TRANSITION,
        expected_status=PipelineJobStatus.QUEUED,
        target_status=PipelineJobStatus.PAUSED,
        replay_policy=PipelineReplayPolicy.RELOAD_AND_REPLAN,
        expected_state_revision=8,
    )
    assert transition.expected_state_revision == 8
    with pytest.raises(BusinessTransactionContractError):
        DurableActionIntent(
            0,
            "d" * 32,
            "notify.push",
            "key",
            payload,
            replay_policy=ActionReplayPolicy.NEVER_REPLAY,
            max_attempts=2,
        )
    with pytest.raises(BusinessTransactionContractError):
        PipelineIntent(
            0,
            "e" * 32,
            "f" * 32,
            "pipeline",
            "create",
            "key",
            payload,
            intent_kind=PipelineIntentKind.CREATE,
            expected_state_revision=1,
        )


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
def test_atomic_pipeline_transition_accepts_only_control_intent_edges(
    expected_status, target_status
) -> None:
    transition = PipelineIntent(
        ordinal=0,
        intent_id="1" * 32,
        job_id="2" * 32,
        pipeline_id="pipeline",
        intent_type="control",
        idempotency_key="pipeline-control-transition",
        payload=_payload(),
        intent_kind=PipelineIntentKind.TRANSITION,
        expected_status=expected_status,
        target_status=target_status,
        expected_state_revision=4,
    )
    assert transition.expected_status is expected_status
    assert transition.target_status is target_status


@pytest.mark.parametrize(
    ("expected_status", "target_status"),
    (
        (PipelineJobStatus.QUEUED, PipelineJobStatus.RUNNING),
        (PipelineJobStatus.PAUSED, PipelineJobStatus.RUNNING),
        (PipelineJobStatus.RUNNING, PipelineJobStatus.PAUSED),
        (PipelineJobStatus.RUNNING, PipelineJobStatus.SUCCEEDED),
        (PipelineJobStatus.RUNNING, PipelineJobStatus.FAILED),
        (PipelineJobStatus.CANCEL_REQUESTED, PipelineJobStatus.CANCELLED),
        (PipelineJobStatus.QUEUED, PipelineJobStatus.CANCEL_REQUESTED),
        (PipelineJobStatus.PAUSED, PipelineJobStatus.CANCEL_REQUESTED),
    ),
)
def test_atomic_pipeline_transition_rejects_worker_attempt_edges(
    expected_status, target_status
) -> None:
    with pytest.raises(BusinessTransactionContractError) as rejected:
        PipelineIntent(
            ordinal=0,
            intent_id="3" * 32,
            job_id="4" * 32,
            pipeline_id="pipeline",
            intent_type="worker-attempt",
            idempotency_key="pipeline-worker-transition",
            payload=_payload(),
            intent_kind=PipelineIntentKind.TRANSITION,
            expected_status=expected_status,
            target_status=target_status,
            expected_state_revision=4,
        )
    assert rejected.value.code == "business_transaction_pipeline_status_invalid"
    assert rejected.value.path == "$.pipelines.target_status"


@pytest.mark.parametrize(
    "expected_status",
    (PipelineJobStatus.RUNNING, PipelineJobStatus.CANCEL_REQUESTED),
)
def test_normal_pipeline_intent_cannot_enter_repair_mode(expected_status) -> None:
    with pytest.raises(BusinessTransactionContractError):
        PipelineIntent(
            ordinal=0,
            intent_id="e" * 32,
            job_id="f" * 32,
            pipeline_id="pipeline",
            intent_type="repair",
            idempotency_key="repair-transition",
            payload=_payload(),
            intent_kind=PipelineIntentKind.TRANSITION,
            expected_status=expected_status,
            target_status=PipelineJobStatus.REPAIR_REQUIRED,
            expected_state_revision=1,
        )
