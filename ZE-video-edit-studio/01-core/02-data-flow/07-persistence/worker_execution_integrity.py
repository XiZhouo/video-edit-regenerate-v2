"""Pure private integrity issuer for durable worker execution requests.

The issuer rebuilds every frozen carrier, validates the target-specific state
machine, and binds the complete request into canonical, domain-separated
manifest bytes.  It performs no database, filesystem, clock, Document, or
provider I/O.

Pipeline business create/control requests use a second issuer and hash domain.
They can never enter WorkerExecutionPlan or manufacture an attempt, effect,
completion, expiry or reconciliation transition.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any, NamedTuple, Optional, Tuple

from business_codec import BusinessCodecError, encode_payload_bounded
from business_transaction_contract import (
    AuthorityBinding,
    BusinessTransactionContractError,
    FrozenJsonPayload,
)
from worker_execution_contract import (
    EMPTY_WORKER_LEDGER_HEAD,
    EMPTY_WORKER_LEDGER_TAIL_SHA256,
    PIPELINE_BUSINESS_CONTROL_MANIFEST_SCHEMA,
    PIPELINE_BUSINESS_CONTROL_PROTOCOL,
    PIPELINE_BUSINESS_CONTROL_REQUEST_CLASS,
    PIPELINE_BUSINESS_CONTROL_STATUS_PREPARED,
    PIPELINE_BUSINESS_CONTROL_TRANSACTION_KIND,
    PIPELINE_BUSINESS_CONTROL_TRANSACTION_PREFIX,
    WORKER_EXECUTION_MANIFEST_SCHEMA,
    WORKER_EXECUTION_PROTOCOL,
    WORKER_EXECUTION_REQUEST_CLASS,
    WORKER_EXECUTION_STATUS_PREPARED,
    WORKER_EXECUTION_TRANSACTION_KIND,
    WORKER_EXECUTION_TRANSACTION_PREFIX,
    PipelineBusinessControlFreshObservation,
    PipelineBusinessControlIntentBinding,
    PipelineBusinessControlOperation,
    PipelineBusinessControlPersistedObservation,
    PipelineBusinessControlReceiptClassification,
    PipelineBusinessControlReceiptEvidence,
    PipelineBusinessControlRequest,
    PipelineBusinessControlRequestIdentityStatus,
    WorkerAfterState,
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
    WorkerExecutionGenericReceiptEvidence,
    WorkerExecutionOperation,
    WorkerExecutionPersistedObservation,
    WorkerExecutionReceiptClassification,
    WorkerExecutionReceiptEvidence,
    WorkerExecutionStatus,
    WorkerExpireRequest,
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
    WorkerRequestIdentityStatus,
    WorkerPersistedReadStatus,
    WorkerReceiptReconcileStatus,
    WorkerRenewRequest,
    WorkerRequestHeader,
    WorkerTarget,
    WorkerTargetKind,
    WorkerTerminalProof,
    WorkerTerminalProofKind,
    _unwrap_pipeline_business_control_fresh_observation,
    _unwrap_worker_execution_fresh_observation,
)


WORKER_EXECUTION_MANIFEST_HASH_DOMAIN = b"ze.worker-execution.manifest-digest.v3"
PIPELINE_BUSINESS_CONTROL_MANIFEST_HASH_DOMAIN = (
    b"ze.pipeline-business-control.manifest-digest.v2"
)
WORKER_EXECUTION_AFTER_STATE_HASH_DOMAIN = b"ze.worker-execution.after-state.v3"
WORKER_EXECUTION_RECEIPT_HASH_DOMAIN = b"ze.worker-execution.receipt.v3"
PIPELINE_BUSINESS_CONTROL_RECEIPT_HASH_DOMAIN = (
    b"ze.pipeline-business-control.receipt.v2"
)
WORKER_LEDGER_TAIL_HASH_DOMAIN = b"ze.worker-ledger.tail.v1"
MAX_WORKER_EXECUTION_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_WORKER_EXECUTION_MANIFEST_NODES = 100_000

_PLAN_ISSUER = object()
_CONTROL_PLAN_ISSUER = object()


class WorkerExecutionIntegrityError(ValueError):
    """Stable, payload-free worker closure failure."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        if type(code) is not str or not code:
            raise TypeError("code must be a non-empty exact string")
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise WorkerExecutionIntegrityError(code)


class WorkerExecutionClosure(NamedTuple):
    operation: WorkerExecutionOperation
    request_transaction_id: str
    before_state_revision: int
    after_state_revision: int
    before_status: WorkerExecutionStatus
    after_status: WorkerExecutionStatus
    after_state: WorkerAfterState
    after_state_sha256: bytes
    manifest_bytes: bytes
    request_sha256: bytes


class _PreparedWorkerExecution(NamedTuple):
    request: Any
    operation: WorkerExecutionOperation
    after_state: WorkerAfterState
    closure: WorkerExecutionClosure
    before_ledger: WorkerLedgerHead
    after_ledger: WorkerLedgerHead
    expected_receipt: WorkerExecutionReceiptEvidence


class WorkerExecutionPlan:
    """Issuer-stamped immutable carrier accepted by a future worker writer."""

    __slots__ = (
        "request",
        "authority",
        "target",
        "before",
        "operation",
        "closure",
        "before_ledger",
        "after_ledger",
        "expected_receipt",
        "protocol",
        "status",
        "_issuer",
    )

    def __init__(self, prepared: _PreparedWorkerExecution, *, _issuer: Any) -> None:
        if _issuer is not _PLAN_ISSUER:
            _fail("worker_execution_plan_issuer_invalid")
        object.__setattr__(self, "request", prepared.request)
        object.__setattr__(self, "authority", prepared.request.header.authority)
        object.__setattr__(self, "target", prepared.request.header.target)
        object.__setattr__(self, "before", prepared.request.header.before)
        object.__setattr__(self, "operation", prepared.operation)
        object.__setattr__(self, "closure", prepared.closure)
        object.__setattr__(self, "before_ledger", prepared.before_ledger)
        object.__setattr__(self, "after_ledger", prepared.after_ledger)
        object.__setattr__(self, "expected_receipt", prepared.expected_receipt)
        object.__setattr__(self, "protocol", WORKER_EXECUTION_PROTOCOL)
        object.__setattr__(self, "status", WORKER_EXECUTION_STATUS_PREPARED)
        object.__setattr__(self, "_issuer", _issuer)

    def __setattr__(self, name: str, value: Any) -> None:
        del name, value
        raise AttributeError("WorkerExecutionPlan is immutable")


class PipelineBusinessControlClosure(NamedTuple):
    operation: PipelineBusinessControlOperation
    request_transaction_id: str
    before_state_revision: Optional[int]
    after_state_revision: int
    before_status: Optional[WorkerExecutionStatus]
    after_status: WorkerExecutionStatus
    after_state: WorkerAfterState
    after_state_sha256: bytes
    manifest_bytes: bytes
    request_sha256: bytes


class _PreparedPipelineBusinessControl(NamedTuple):
    request: PipelineBusinessControlRequest
    after_state: WorkerAfterState
    closure: PipelineBusinessControlClosure
    before_ledger: WorkerLedgerHead
    after_ledger: WorkerLedgerHead
    expected_receipt: PipelineBusinessControlReceiptEvidence


class PipelineBusinessControlPlan:
    """Unmounted V8 business control projection, never worker work."""

    __slots__ = (
        "request",
        "authority",
        "target",
        "control_intent",
        "operation",
        "closure",
        "before_ledger",
        "after_ledger",
        "expected_receipt",
        "protocol",
        "status",
        "_issuer",
    )

    def __init__(
        self,
        prepared: _PreparedPipelineBusinessControl,
        *,
        _issuer: Any,
    ) -> None:
        if _issuer is not _CONTROL_PLAN_ISSUER:
            _fail("pipeline_business_control_plan_issuer_invalid")
        object.__setattr__(self, "request", prepared.request)
        object.__setattr__(self, "authority", prepared.request.authority)
        object.__setattr__(self, "target", prepared.request.target)
        object.__setattr__(
            self,
            "control_intent",
            prepared.request.control_intent,
        )
        object.__setattr__(self, "operation", prepared.request.operation)
        object.__setattr__(self, "closure", prepared.closure)
        object.__setattr__(self, "before_ledger", prepared.before_ledger)
        object.__setattr__(self, "after_ledger", prepared.after_ledger)
        object.__setattr__(self, "expected_receipt", prepared.expected_receipt)
        object.__setattr__(self, "protocol", PIPELINE_BUSINESS_CONTROL_PROTOCOL)
        object.__setattr__(
            self,
            "status",
            PIPELINE_BUSINESS_CONTROL_STATUS_PREPARED,
        )
        object.__setattr__(self, "_issuer", _issuer)

    def __setattr__(self, name: str, value: Any) -> None:
        del name, value
        raise AttributeError("PipelineBusinessControlPlan is immutable")


_TARGET_STATUSES = {
    WorkerTargetKind.PIPELINE: frozenset((
        WorkerExecutionStatus.QUEUED,
        WorkerExecutionStatus.RETRY,
        WorkerExecutionStatus.RUNNING,
        WorkerExecutionStatus.PAUSED,
        WorkerExecutionStatus.CANCEL_REQUESTED,
        WorkerExecutionStatus.SUCCEEDED,
        WorkerExecutionStatus.FAILED,
        WorkerExecutionStatus.CANCELLED,
        WorkerExecutionStatus.OUTCOME_UNKNOWN,
        WorkerExecutionStatus.REPAIR_REQUIRED,
    )),
    WorkerTargetKind.ACTION: frozenset((
        WorkerExecutionStatus.QUEUED,
        WorkerExecutionStatus.RETRY,
        WorkerExecutionStatus.RUNNING,
        WorkerExecutionStatus.SUCCEEDED,
        WorkerExecutionStatus.FAILED_TERMINAL,
        WorkerExecutionStatus.CANCELLED,
        WorkerExecutionStatus.OUTCOME_UNKNOWN,
        WorkerExecutionStatus.REPAIR_REQUIRED,
    )),
    WorkerTargetKind.OUTBOX: frozenset((
        WorkerExecutionStatus.PENDING,
        WorkerExecutionStatus.RETRY,
        WorkerExecutionStatus.LEASED,
        WorkerExecutionStatus.PUBLISHED,
        WorkerExecutionStatus.DEAD,
        WorkerExecutionStatus.OUTCOME_UNKNOWN,
    )),
}

_CLAIM_BEFORE = {
    WorkerTargetKind.PIPELINE: frozenset((
        WorkerExecutionStatus.QUEUED,
        WorkerExecutionStatus.RETRY,
    )),
    WorkerTargetKind.ACTION: frozenset((
        WorkerExecutionStatus.QUEUED,
        WorkerExecutionStatus.RETRY,
    )),
    WorkerTargetKind.OUTBOX: frozenset((
        WorkerExecutionStatus.PENDING,
        WorkerExecutionStatus.RETRY,
    )),
}

_ACTIVE_STATUSES_BY_OPERATION = {
    WorkerExecutionOperation.RENEW: {
        WorkerTargetKind.PIPELINE: frozenset((
            WorkerExecutionStatus.RUNNING,
            WorkerExecutionStatus.CANCEL_REQUESTED,
        )),
        WorkerTargetKind.ACTION: frozenset((WorkerExecutionStatus.RUNNING,)),
        WorkerTargetKind.OUTBOX: frozenset((WorkerExecutionStatus.LEASED,)),
    },
    WorkerExecutionOperation.AUTHORIZE_EFFECT: {
        WorkerTargetKind.PIPELINE: frozenset((WorkerExecutionStatus.RUNNING,)),
        WorkerTargetKind.ACTION: frozenset((WorkerExecutionStatus.RUNNING,)),
        WorkerTargetKind.OUTBOX: frozenset((WorkerExecutionStatus.LEASED,)),
    },
    WorkerExecutionOperation.COMPLETE: {
        WorkerTargetKind.PIPELINE: frozenset((
            WorkerExecutionStatus.RUNNING,
            WorkerExecutionStatus.CANCEL_REQUESTED,
        )),
        WorkerTargetKind.ACTION: frozenset((WorkerExecutionStatus.RUNNING,)),
        WorkerTargetKind.OUTBOX: frozenset((WorkerExecutionStatus.LEASED,)),
    },
    WorkerExecutionOperation.EXPIRE: {
        WorkerTargetKind.PIPELINE: frozenset((
            WorkerExecutionStatus.RUNNING,
            WorkerExecutionStatus.CANCEL_REQUESTED,
        )),
        WorkerTargetKind.ACTION: frozenset((WorkerExecutionStatus.RUNNING,)),
        WorkerTargetKind.OUTBOX: frozenset((WorkerExecutionStatus.LEASED,)),
    },
}

_CLAIM_AFTER = {
    WorkerTargetKind.PIPELINE: WorkerExecutionStatus.RUNNING,
    WorkerTargetKind.ACTION: WorkerExecutionStatus.RUNNING,
    WorkerTargetKind.OUTBOX: WorkerExecutionStatus.LEASED,
}

_TARGET_EFFECT_KIND = {
    WorkerTargetKind.PIPELINE: WorkerEffectKind.PIPELINE_DOCUMENT_COMMIT,
    WorkerTargetKind.ACTION: WorkerEffectKind.ACTION_PROVIDER,
    WorkerTargetKind.OUTBOX: WorkerEffectKind.OUTBOX_PUBLISH,
}

_COMPLETE_AFTER = {
    WorkerTargetKind.PIPELINE: frozenset((
        WorkerExecutionStatus.SUCCEEDED,
        WorkerExecutionStatus.FAILED,
        WorkerExecutionStatus.CANCELLED,
        WorkerExecutionStatus.RETRY,
        WorkerExecutionStatus.OUTCOME_UNKNOWN,
    )),
    WorkerTargetKind.ACTION: frozenset((
        WorkerExecutionStatus.SUCCEEDED,
        WorkerExecutionStatus.FAILED_TERMINAL,
        WorkerExecutionStatus.CANCELLED,
        WorkerExecutionStatus.RETRY,
        WorkerExecutionStatus.OUTCOME_UNKNOWN,
    )),
    WorkerTargetKind.OUTBOX: frozenset((
        WorkerExecutionStatus.PUBLISHED,
        WorkerExecutionStatus.RETRY,
        WorkerExecutionStatus.OUTCOME_UNKNOWN,
        WorkerExecutionStatus.DEAD,
    )),
}

_CANCELLATION_COMPLETE_AFTER = frozenset((
    WorkerExecutionStatus.SUCCEEDED,
    WorkerExecutionStatus.FAILED,
    WorkerExecutionStatus.CANCELLED,
    WorkerExecutionStatus.OUTCOME_UNKNOWN,
))

_RECONCILE_BEFORE = {
    WorkerTargetKind.PIPELINE: frozenset((
        WorkerExecutionStatus.OUTCOME_UNKNOWN,
        WorkerExecutionStatus.REPAIR_REQUIRED,
    )),
    WorkerTargetKind.ACTION: frozenset((
        WorkerExecutionStatus.OUTCOME_UNKNOWN,
        WorkerExecutionStatus.REPAIR_REQUIRED,
    )),
    WorkerTargetKind.OUTBOX: frozenset((WorkerExecutionStatus.OUTCOME_UNKNOWN,)),
}

_RECONCILE_AFTER = {
    WorkerTargetKind.PIPELINE: frozenset((
        WorkerExecutionStatus.SUCCEEDED,
        WorkerExecutionStatus.FAILED,
        WorkerExecutionStatus.CANCELLED,
        WorkerExecutionStatus.RETRY,
        WorkerExecutionStatus.REPAIR_REQUIRED,
    )),
    WorkerTargetKind.ACTION: frozenset((
        WorkerExecutionStatus.SUCCEEDED,
        WorkerExecutionStatus.FAILED_TERMINAL,
        WorkerExecutionStatus.CANCELLED,
        WorkerExecutionStatus.RETRY,
        WorkerExecutionStatus.REPAIR_REQUIRED,
    )),
    WorkerTargetKind.OUTBOX: frozenset((
        WorkerExecutionStatus.PUBLISHED,
        WorkerExecutionStatus.RETRY,
        WorkerExecutionStatus.DEAD,
    )),
}

_EXPIRE_NO_EFFECT_TERMINAL = {
    WorkerTargetKind.PIPELINE: frozenset((
        WorkerExecutionStatus.FAILED,
        WorkerExecutionStatus.CANCELLED,
    )),
    WorkerTargetKind.ACTION: frozenset((
        WorkerExecutionStatus.FAILED_TERMINAL,
        WorkerExecutionStatus.CANCELLED,
    )),
    WorkerTargetKind.OUTBOX: frozenset((WorkerExecutionStatus.DEAD,)),
}


def _frame(hasher: Any, value: bytes) -> None:
    hasher.update(len(value).to_bytes(8, "big"))
    hasher.update(value)


def _manifest_digest(value: bytes) -> bytes:
    hasher = hashlib.sha256()
    _frame(hasher, WORKER_EXECUTION_MANIFEST_HASH_DOMAIN)
    _frame(hasher, value)
    return hasher.digest()


def _control_manifest_digest(value: bytes) -> bytes:
    hasher = hashlib.sha256()
    _frame(hasher, PIPELINE_BUSINESS_CONTROL_MANIFEST_HASH_DOMAIN)
    _frame(hasher, value)
    return hasher.digest()


def _domain_digest(domain: bytes, *parts: bytes) -> bytes:
    hasher = hashlib.sha256()
    _frame(hasher, domain)
    for part in parts:
        _frame(hasher, part)
    return hasher.digest()


def _canonical_ledger_head(value: Any) -> WorkerLedgerHead:
    return WorkerLedgerHead(
        sequence=value.sequence,
        tail_sha256=value.tail_sha256,
    )


def _canonical_ledger_membership(value: Any) -> Any:
    if value is None:
        return None
    return WorkerLedgerMembershipEvidence(
        receipt_sequence=value.receipt_sequence,
        receipt_tail_sha256=value.receipt_tail_sha256,
        observed_head=_canonical_ledger_head(value.observed_head),
    )


def _canonical_payload(value: FrozenJsonPayload) -> FrozenJsonPayload:
    return FrozenJsonPayload(
        schema=value.schema,
        codec_id=value.codec_id,
        codec_version=value.codec_version,
        payload_bytes=value.payload_bytes,
        payload_sha256=value.payload_sha256,
    )


def _canonical_authority(value: AuthorityBinding) -> AuthorityBinding:
    return AuthorityBinding(
        authority=value.authority,
        authority_state=value.authority_state,
        authority_epoch=value.authority_epoch,
        source_admission_revision=value.source_admission_revision,
        authority_revision=value.authority_revision,
        control_revision=value.control_revision,
        cutover_id=value.cutover_id,
        runtime_generation=value.runtime_generation,
        code_digest=value.code_digest,
    )


def _canonical_outbox_order(value: Any) -> Any:
    if value is None:
        return None
    return WorkerOutboxOrder(
        sequence=value.sequence,
        repository_revision=value.repository_revision,
        ordinal=value.ordinal,
    )


def _canonical_dispatch_fence(value: Any) -> Any:
    if value is None:
        return None
    def cursor(candidate: Any) -> WorkerOutboxDispatchCursor:
        return WorkerOutboxDispatchCursor(
            sequence=candidate.sequence,
            repository_revision=candidate.repository_revision,
            ordinal=candidate.ordinal,
        )

    def state(candidate: Any) -> WorkerOutboxDispatchState:
        return WorkerOutboxDispatchState(
            control_revision=candidate.control_revision,
            published=cursor(candidate.published),
            inflight=_canonical_outbox_order(candidate.inflight),
        )

    return WorkerOutboxDispatchFence(
        before=state(value.before),
        after=state(value.after),
    )


def _canonical_target(value: WorkerTarget) -> WorkerTarget:
    return WorkerTarget(
        target_kind=value.target_kind,
        target_id=value.target_id,
        project_id=value.project_id,
        generation=value.generation,
        document_id=value.document_id,
        origin_commit_id=value.origin_commit_id,
        payload=_canonical_payload(value.payload),
        replay_policy=value.replay_policy,
        idempotency_policy=value.idempotency_policy,
        idempotency_scope=value.idempotency_scope,
        idempotency_key=value.idempotency_key,
        max_attempts=value.max_attempts,
        outbox_order=_canonical_outbox_order(value.outbox_order),
        pipeline_id=value.pipeline_id,
        intent_type=value.intent_type,
    )


def _canonical_before(value: WorkerBeforeState) -> WorkerBeforeState:
    return WorkerBeforeState(
        status=value.status,
        state_revision=value.state_revision,
        attempt_count=value.attempt_count,
        last_attempt_id=value.last_attempt_id,
        last_attempt_number=value.last_attempt_number,
        current_attempt_id=value.current_attempt_id,
        current_attempt_number=value.current_attempt_number,
        lease_owner=value.lease_owner,
        lease_fence=value.lease_fence,
        lease_expires_at=value.lease_expires_at,
        fence_high_water=value.fence_high_water,
        current_effect=_canonical_effect_binding(value.current_effect),
        unresolved_effect=_canonical_effect_binding(value.unresolved_effect),
        next_attempt_at=value.next_attempt_at,
        cancellation_pending=value.cancellation_pending,
    )


def _canonical_header(value: WorkerRequestHeader) -> WorkerRequestHeader:
    return WorkerRequestHeader(
        request_transaction_id=value.request_transaction_id,
        authority=_canonical_authority(value.authority),
        target=_canonical_target(value.target),
        before=_canonical_before(value.before),
        occurred_at=value.occurred_at,
    )


def _canonical_attempt(value: WorkerAttemptStart) -> WorkerAttemptStart:
    return WorkerAttemptStart(
        attempt_id=value.attempt_id,
        attempt_number=value.attempt_number,
        previous_attempt_id=value.previous_attempt_id,
        previous_attempt_number=value.previous_attempt_number,
        lease_owner=value.lease_owner,
        execution_fence=value.execution_fence,
        started_at=value.started_at,
        lease_expires_at=value.lease_expires_at,
    )


def _canonical_lease(value: WorkerLeaseToken) -> WorkerLeaseToken:
    return WorkerLeaseToken(
        attempt_id=value.attempt_id,
        attempt_number=value.attempt_number,
        lease_owner=value.lease_owner,
        execution_fence=value.execution_fence,
        lease_expires_at=value.lease_expires_at,
    )


def _canonical_effect(value: WorkerEffectAuthorization) -> WorkerEffectAuthorization:
    return WorkerEffectAuthorization(
        effect_id=value.effect_id,
        effect_kind=value.effect_kind,
        idempotency_scope=value.idempotency_scope,
        idempotency_key=value.idempotency_key,
        request=_canonical_payload(value.request),
        authorized_at=value.authorized_at,
    )


def _canonical_effect_binding(value: Any) -> Any:
    if value is None:
        return None
    return WorkerEffectBinding(
        attempt_id=value.attempt_id,
        attempt_number=value.attempt_number,
        effect_id=value.effect_id,
        effect_kind=value.effect_kind,
        request_sha256=value.request_sha256,
        idempotency_scope=value.idempotency_scope,
        idempotency_key=value.idempotency_key,
    )


def _canonical_effect_link(value: Any) -> Any:
    if value is None:
        return None
    return WorkerEffectLink(
        attempt_id=value.attempt_id,
        attempt_number=value.attempt_number,
        effect_id=value.effect_id,
        effect_kind=value.effect_kind,
        request_sha256=value.request_sha256,
        result_commit_id=value.result_commit_id,
        provider_receipt=(
            None
            if value.provider_receipt is None
            else _canonical_payload(value.provider_receipt)
        ),
    )


def _canonical_durable_result(value: Any) -> Any:
    if value is None:
        return None
    return WorkerDurableResultProof(
        result_id=value.result_id,
        result=_canonical_payload(value.result),
        recorded_at=value.recorded_at,
        effect=_canonical_effect_binding(value.effect),
    )


def _canonical_reconcile_proof(value: WorkerReconcileProof) -> WorkerReconcileProof:
    return WorkerReconcileProof(
        proof_kind=value.proof_kind,
        unresolved_effect=_canonical_effect_binding(value.unresolved_effect),
        evidence=_canonical_payload(value.evidence),
        effect_link=_canonical_effect_link(value.effect_link),
        durable_result=_canonical_durable_result(value.durable_result),
    )


def _canonical_terminal_proof(value: Any) -> Any:
    if value is None:
        return None
    return WorkerTerminalProof(
        proof_kind=value.proof_kind,
        effect=_canonical_effect_binding(value.effect),
        evidence=_canonical_payload(value.evidence),
        effect_link=_canonical_effect_link(value.effect_link),
    )


def _canonical_request(value: Any) -> Tuple[Any, WorkerExecutionOperation]:
    if type(value) is WorkerClaimRequest:
        return WorkerClaimRequest(
            header=_canonical_header(value.header),
            attempt=_canonical_attempt(value.attempt),
            dispatch_fence=_canonical_dispatch_fence(value.dispatch_fence),
        ), WorkerExecutionOperation.CLAIM
    if type(value) is WorkerAcknowledgeCancelRequest:
        return WorkerAcknowledgeCancelRequest(
            header=_canonical_header(value.header),
            acknowledged_at=value.acknowledged_at,
        ), WorkerExecutionOperation.ACKNOWLEDGE_CANCEL
    if type(value) is WorkerRenewRequest:
        return WorkerRenewRequest(
            header=_canonical_header(value.header),
            lease=_canonical_lease(value.lease),
            new_lease_expires_at=value.new_lease_expires_at,
        ), WorkerExecutionOperation.RENEW
    if type(value) is WorkerAuthorizeEffectRequest:
        return WorkerAuthorizeEffectRequest(
            header=_canonical_header(value.header),
            lease=_canonical_lease(value.lease),
            effect=_canonical_effect(value.effect),
        ), WorkerExecutionOperation.AUTHORIZE_EFFECT
    if type(value) is WorkerCompleteRequest:
        return WorkerCompleteRequest(
            header=_canonical_header(value.header),
            lease=_canonical_lease(value.lease),
            target_status=value.target_status,
            finished_at=value.finished_at,
            result=(None if value.result is None else _canonical_payload(value.result)),
            error=(None if value.error is None else _canonical_payload(value.error)),
            effect_link=_canonical_effect_link(value.effect_link),
            durable_result=_canonical_durable_result(value.durable_result),
            terminal_proof=_canonical_terminal_proof(value.terminal_proof),
            next_attempt_at=value.next_attempt_at,
            dispatch_fence=_canonical_dispatch_fence(value.dispatch_fence),
        ), WorkerExecutionOperation.COMPLETE
    if type(value) is WorkerExpireRequest:
        return WorkerExpireRequest(
            header=_canonical_header(value.header),
            lease=_canonical_lease(value.lease),
            expired_at=value.expired_at,
            target_status=value.target_status,
            next_attempt_at=value.next_attempt_at,
            dispatch_fence=_canonical_dispatch_fence(value.dispatch_fence),
        ), WorkerExecutionOperation.EXPIRE
    if type(value) is WorkerReconcileRequest:
        return WorkerReconcileRequest(
            header=_canonical_header(value.header),
            attempt_id=value.attempt_id,
            attempt_number=value.attempt_number,
            execution_fence=value.execution_fence,
            target_status=value.target_status,
            proof=_canonical_reconcile_proof(value.proof),
            reconciled_at=value.reconciled_at,
            next_attempt_at=value.next_attempt_at,
            dispatch_fence=_canonical_dispatch_fence(value.dispatch_fence),
        ), WorkerExecutionOperation.RECONCILE
    _fail("worker_execution_request_type_invalid")


def _canonical_control_request(value: Any) -> PipelineBusinessControlRequest:
    if type(value) is not PipelineBusinessControlRequest:
        _fail("pipeline_business_control_request_type_invalid")
    return PipelineBusinessControlRequest(
        request_transaction_id=value.request_transaction_id,
        authority=_canonical_authority(value.authority),
        target=_canonical_target(value.target),
        control_intent=PipelineBusinessControlIntentBinding(
            origin_commit_id=value.control_intent.origin_commit_id,
            intent_type=value.control_intent.intent_type,
            idempotency_key=value.control_intent.idempotency_key,
            payload=_canonical_payload(value.control_intent.payload),
            replay_policy=value.control_intent.replay_policy,
            idempotency_policy=value.control_intent.idempotency_policy,
            max_attempts=value.control_intent.max_attempts,
        ),
        operation=value.operation,
        before=(
            None if value.before is None else _canonical_before(value.before)
        ),
        target_status=value.target_status,
        occurred_at=value.occurred_at,
    )


def _require_target_before(header: WorkerRequestHeader) -> None:
    kind = header.target.target_kind
    before = header.before
    if before.status not in _TARGET_STATUSES[kind]:
        _fail("worker_execution_target_status_invalid")
    if before.attempt_count > header.target.max_attempts:
        _fail("worker_execution_attempt_budget_invalid")
    if before.cancellation_pending and kind is not WorkerTargetKind.PIPELINE:
        _fail("worker_execution_cancellation_invalid")


def _require_outbox_claim_fence(
    target: WorkerTarget,
    before: WorkerBeforeState,
    dispatch_fence: Any,
) -> None:
    if target.target_kind is not WorkerTargetKind.OUTBOX:
        if dispatch_fence is not None:
            _fail("worker_execution_dispatch_fence_unexpected")
        return
    order = target.outbox_order
    if order is None or dispatch_fence is None:
        _fail("worker_execution_dispatch_fence_invalid")
    prior = dispatch_fence.before
    after = dispatch_fence.after
    first_claim = bool(
        before.status is WorkerExecutionStatus.PENDING
        and prior.inflight is None
        and after.published == prior.published
        and after.inflight == order
        and after.control_revision == prior.control_revision + 1
    )
    retry_claim = bool(
        before.status is WorkerExecutionStatus.RETRY
        and prior.inflight == order
        and after == prior
    )
    if not (first_claim or retry_claim):
        _fail("worker_execution_dispatch_fence_invalid")


def _cursor_matches_order(
    cursor: WorkerOutboxDispatchCursor,
    order: WorkerOutboxOrder,
) -> bool:
    return bool(
        cursor.sequence == order.sequence
        and cursor.repository_revision == order.repository_revision
        and cursor.ordinal == order.ordinal
    )


def _require_outbox_result_fence(
    target: WorkerTarget,
    target_status: WorkerExecutionStatus,
    dispatch_fence: Any,
    *,
    operation: WorkerExecutionOperation,
) -> None:
    if target.target_kind is not WorkerTargetKind.OUTBOX:
        if dispatch_fence is not None:
            _fail("worker_execution_dispatch_fence_unexpected")
        return
    order = target.outbox_order
    if order is None or dispatch_fence is None:
        _fail("worker_execution_dispatch_fence_invalid")
    prior = dispatch_fence.before
    after = dispatch_fence.after
    if prior.inflight != order:
        _fail("worker_execution_dispatch_fence_invalid")
    if target_status in (
        WorkerExecutionStatus.PUBLISHED,
        WorkerExecutionStatus.DEAD,
    ):
        effect_terminal_operation = operation in (
            WorkerExecutionOperation.COMPLETE,
            WorkerExecutionOperation.RECONCILE,
        )
        exhausted_without_effect_operation = bool(
            target_status is WorkerExecutionStatus.DEAD
            and operation is WorkerExecutionOperation.EXPIRE
        )
        if not (
            effect_terminal_operation or exhausted_without_effect_operation
        ) or not (
            after.control_revision == prior.control_revision + 1
            and _cursor_matches_order(after.published, order)
            and after.inflight is None
        ):
            _fail("worker_execution_dispatch_fence_invalid")
        return
    if after != prior:
        _fail("worker_execution_dispatch_fence_invalid")


def _require_active_lease(
    header: WorkerRequestHeader,
    lease: WorkerLeaseToken,
    operation: WorkerExecutionOperation,
) -> None:
    before = header.before
    allowed = _ACTIVE_STATUSES_BY_OPERATION.get(operation)
    if (
        allowed is None
        or before.status not in allowed[header.target.target_kind]
    ):
        _fail("worker_execution_status_edge_invalid")
    if (
        before.current_attempt_id != lease.attempt_id
        or before.current_attempt_number != lease.attempt_number
        or before.lease_owner != lease.lease_owner
        or before.lease_fence != lease.execution_fence
        or before.lease_expires_at != lease.lease_expires_at
        or before.fence_high_water != lease.execution_fence
    ):
        _fail("worker_execution_lease_binding_invalid")


def _effect_binding_from_authorization(
    before: WorkerBeforeState,
    effect: WorkerEffectAuthorization,
) -> WorkerEffectBinding:
    return WorkerEffectBinding(
        attempt_id=before.current_attempt_id,
        attempt_number=before.current_attempt_number,
        effect_id=effect.effect_id,
        effect_kind=effect.effect_kind,
        request_sha256=effect.request.payload_sha256,
        idempotency_scope=effect.idempotency_scope,
        idempotency_key=effect.idempotency_key,
    )


def _link_matches_effect(link: WorkerEffectLink, effect: WorkerEffectBinding) -> bool:
    return (
        link.attempt_id == effect.attempt_id
        and link.attempt_number == effect.attempt_number
        and link.effect_id == effect.effect_id
        and link.effect_kind is effect.effect_kind
        and hmac.compare_digest(link.request_sha256, effect.request_sha256)
    )


def _require_effect_link(before: WorkerBeforeState, link: Any) -> None:
    if before.current_effect is None:
        if link is not None:
            _fail("worker_execution_effect_link_unexpected")
        return
    if link is None or not _link_matches_effect(link, before.current_effect):
        _fail("worker_execution_effect_link_invalid")


def _retry_allowed(target: WorkerTarget, attempt_count: int) -> bool:
    return bool(
        attempt_count < target.max_attempts
        and target.replay_policy.value != "never_replay"
        and target.idempotency_policy.value != "single_attempt"
    )


def _active_after(
    before: WorkerBeforeState,
    *,
    status: WorkerExecutionStatus,
    lease_expires_at: str,
    current_effect: Any,
) -> WorkerAfterState:
    return WorkerAfterState(
        status=status,
        state_revision=before.state_revision + 1,
        attempt_count=before.attempt_count,
        last_attempt_id=before.last_attempt_id,
        last_attempt_number=before.last_attempt_number,
        current_attempt_id=before.current_attempt_id,
        current_attempt_number=before.current_attempt_number,
        lease_owner=before.lease_owner,
        lease_fence=before.lease_fence,
        lease_expires_at=lease_expires_at,
        fence_high_water=before.fence_high_water,
        current_effect=current_effect,
        cancellation_pending=before.cancellation_pending,
    )


def _completed_after(
    before: WorkerBeforeState,
    *,
    status: WorkerExecutionStatus,
    next_attempt_at: Any = None,
    unresolved_effect: Any = None,
    result_effect_link: Any = None,
    durable_result: Any = None,
    cancellation_pending: bool = False,
) -> WorkerAfterState:
    return WorkerAfterState(
        status=status,
        state_revision=before.state_revision + 1,
        attempt_count=before.attempt_count,
        last_attempt_id=before.current_attempt_id or before.last_attempt_id,
        last_attempt_number=(
            before.current_attempt_number or before.last_attempt_number
        ),
        current_attempt_id=None,
        current_attempt_number=None,
        lease_owner=None,
        lease_fence=None,
        lease_expires_at=None,
        fence_high_water=before.fence_high_water,
        unresolved_effect=unresolved_effect,
        next_attempt_at=next_attempt_at,
        cancellation_pending=cancellation_pending,
        result_effect_link=result_effect_link,
        durable_result=durable_result,
    )


def _require_success_proof(
    kind: WorkerTargetKind,
    effect: Any,
    effect_link: Any,
    durable_result: Any,
) -> None:
    if kind is WorkerTargetKind.PIPELINE:
        if (
            effect is None
            or effect.effect_kind is not WorkerEffectKind.PIPELINE_DOCUMENT_COMMIT
            or effect_link is None
            or effect_link.result_commit_id is None
            or durable_result is not None
        ):
            _fail("worker_execution_pipeline_success_proof_required")
        return
    if kind is WorkerTargetKind.OUTBOX:
        if (
            effect is None
            or effect.effect_kind is not WorkerEffectKind.OUTBOX_PUBLISH
            or effect_link is None
            or effect_link.result_commit_id is not None
            or effect_link.provider_receipt is None
            or durable_result is not None
        ):
            _fail("worker_execution_outbox_publish_proof_required")
        return
    provider_success = bool(
        effect is not None
        and effect.effect_kind is WorkerEffectKind.ACTION_PROVIDER
        and effect_link is not None
        and effect_link.provider_receipt is not None
        and durable_result is None
    )
    durable_success = bool(
        effect_link is None
        and durable_result is not None
        and durable_result.effect == effect
    )
    if not (provider_success or durable_success):
        _fail("worker_execution_action_success_proof_required")


def _require_terminal_proof(
    before: WorkerBeforeState,
    target_status: WorkerExecutionStatus,
    proof: Any,
) -> None:
    effect = before.current_effect
    terminal_status = target_status in (
        WorkerExecutionStatus.FAILED,
        WorkerExecutionStatus.FAILED_TERMINAL,
        WorkerExecutionStatus.CANCELLED,
        WorkerExecutionStatus.DEAD,
    )
    if target_status is WorkerExecutionStatus.DEAD and effect is None:
        _fail("worker_execution_terminal_proof_required")
    if not terminal_status or effect is None:
        if proof is not None:
            _fail("worker_execution_terminal_proof_unexpected")
        return
    if proof is None or proof.effect != effect:
        _fail("worker_execution_terminal_proof_required")
    if proof.effect_link is not None and not _link_matches_effect(
        proof.effect_link,
        effect,
    ):
        _fail("worker_execution_terminal_proof_invalid")
    if (
        effect.effect_kind is WorkerEffectKind.OUTBOX_PUBLISH
        and proof.effect_link is not None
        and (
            proof.effect_link.result_commit_id is not None
            or proof.effect_link.provider_receipt is None
        )
    ):
        _fail("worker_execution_terminal_proof_invalid")
    if target_status is WorkerExecutionStatus.CANCELLED:
        if proof.proof_kind not in (
            WorkerTerminalProofKind.CANCELLED,
            WorkerTerminalProofKind.PROVEN_NOT_EXECUTED,
        ):
            _fail("worker_execution_terminal_proof_invalid")
    elif proof.proof_kind is not WorkerTerminalProofKind.TERMINAL_FAILURE:
        _fail("worker_execution_terminal_proof_invalid")


def _require_reconcile_proof(
    request: WorkerReconcileRequest,
    target: WorkerTarget,
    before: WorkerBeforeState,
) -> None:
    proof = request.proof
    unresolved = before.unresolved_effect
    if unresolved is None or proof.unresolved_effect != unresolved:
        _fail("worker_execution_reconcile_effect_mismatch")
    if proof.effect_link is not None and not _link_matches_effect(
        proof.effect_link,
        unresolved,
    ):
        _fail("worker_execution_reconcile_effect_mismatch")
    if proof.durable_result is not None and proof.durable_result.effect != unresolved:
        _fail("worker_execution_reconcile_effect_mismatch")

    status = request.target_status
    kind = target.target_kind
    if status is WorkerExecutionStatus.RETRY:
        if before.cancellation_pending:
            _fail("worker_execution_cancellation_retry_forbidden")
        if (
            not _retry_allowed(target, before.attempt_count)
            or proof.proof_kind not in (
                WorkerReconcileProofKind.PROVEN_NOT_EXECUTED,
                WorkerReconcileProofKind.PROVIDER_ABSENCE,
            )
        ):
            _fail("worker_execution_reconcile_retry_forbidden")
        return
    if status in (
        WorkerExecutionStatus.SUCCEEDED,
        WorkerExecutionStatus.PUBLISHED,
    ):
        expected_proof = {
            WorkerTargetKind.PIPELINE: WorkerReconcileProofKind.RESULT_COMMIT,
            WorkerTargetKind.OUTBOX: WorkerReconcileProofKind.PROVIDER_RECEIPT,
        }.get(kind)
        if expected_proof is not None and proof.proof_kind is not expected_proof:
            _fail("worker_execution_reconcile_decision_invalid")
        if kind is WorkerTargetKind.ACTION and proof.proof_kind not in (
            WorkerReconcileProofKind.PROVIDER_RECEIPT,
            WorkerReconcileProofKind.DURABLE_RESULT,
        ):
            _fail("worker_execution_reconcile_decision_invalid")
        _require_success_proof(
            kind,
            unresolved,
            proof.effect_link,
            proof.durable_result,
        )
        return
    if status is WorkerExecutionStatus.REPAIR_REQUIRED:
        if proof.proof_kind is not WorkerReconcileProofKind.STILL_UNKNOWN:
            _fail("worker_execution_reconcile_decision_invalid")
        return
    if proof.proof_kind not in (
        WorkerReconcileProofKind.TERMINAL_FAILURE,
        WorkerReconcileProofKind.PROVEN_NOT_EXECUTED,
        WorkerReconcileProofKind.PROVIDER_ABSENCE,
    ):
        _fail("worker_execution_reconcile_decision_invalid")


_PIPELINE_BUSINESS_CONTROL_EDGES = frozenset((
    (WorkerExecutionStatus.QUEUED, WorkerExecutionStatus.PAUSED),
    (WorkerExecutionStatus.QUEUED, WorkerExecutionStatus.CANCELLED),
    (WorkerExecutionStatus.RUNNING, WorkerExecutionStatus.CANCEL_REQUESTED),
    # Resume only restores eligibility.  A later worker CLAIM still owns the
    # QUEUED -> RUNNING attempt/lease transition.
    (WorkerExecutionStatus.PAUSED, WorkerExecutionStatus.QUEUED),
    (WorkerExecutionStatus.PAUSED, WorkerExecutionStatus.CANCELLED),
))


def _validate_control_request(
    request: PipelineBusinessControlRequest,
) -> WorkerAfterState:
    if request.operation is PipelineBusinessControlOperation.CREATE:
        return WorkerAfterState(
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

    before = request.before
    if before is None:
        _fail("pipeline_business_control_before_required")
    if (
        (before.status, request.target_status)
        not in _PIPELINE_BUSINESS_CONTROL_EDGES
        or before.attempt_count > request.target.max_attempts
        or (
            before.status is WorkerExecutionStatus.RUNNING
            and before.current_attempt_id is None
        )
        or (
            before.status is not WorkerExecutionStatus.RUNNING
            and (
                before.current_attempt_id is not None
                or before.current_effect is not None
            )
        )
    ):
        _fail("pipeline_business_control_edge_invalid")
    return WorkerAfterState(
        status=request.target_status,
        state_revision=before.state_revision + 1,
        attempt_count=before.attempt_count,
        last_attempt_id=before.last_attempt_id,
        last_attempt_number=before.last_attempt_number,
        current_attempt_id=before.current_attempt_id,
        current_attempt_number=before.current_attempt_number,
        lease_owner=before.lease_owner,
        lease_fence=before.lease_fence,
        lease_expires_at=before.lease_expires_at,
        fence_high_water=before.fence_high_water,
        current_effect=before.current_effect,
        unresolved_effect=before.unresolved_effect,
        next_attempt_at=before.next_attempt_at,
        cancellation_pending=(
            request.target_status is WorkerExecutionStatus.CANCEL_REQUESTED
        ),
    )


def _validate_request(
    request: Any,
    operation: WorkerExecutionOperation,
) -> WorkerAfterState:
    header = request.header
    target = header.target
    before = header.before
    kind = target.target_kind
    _require_target_before(header)

    if operation is WorkerExecutionOperation.CLAIM:
        attempt = request.attempt
        _require_outbox_claim_fence(target, before, request.dispatch_fence)
        if (
            before.status not in _CLAIM_BEFORE[kind]
            or before.current_attempt_id is not None
            or before.current_effect is not None
            or before.unresolved_effect is not None
            or before.attempt_count >= target.max_attempts
            or attempt.attempt_number != before.attempt_count + 1
            or attempt.previous_attempt_id != before.last_attempt_id
            or attempt.previous_attempt_number != before.last_attempt_number
            or attempt.execution_fence != before.fence_high_water + 1
            or attempt.started_at != header.occurred_at
            or (
                before.next_attempt_at is not None
                and header.occurred_at < before.next_attempt_at
            )
        ):
            _fail("worker_execution_claim_invalid")
        return WorkerAfterState(
            status=_CLAIM_AFTER[kind],
            state_revision=before.state_revision + 1,
            attempt_count=attempt.attempt_number,
            last_attempt_id=before.last_attempt_id,
            last_attempt_number=before.last_attempt_number,
            current_attempt_id=attempt.attempt_id,
            current_attempt_number=attempt.attempt_number,
            lease_owner=attempt.lease_owner,
            lease_fence=attempt.execution_fence,
            lease_expires_at=attempt.lease_expires_at,
            fence_high_water=attempt.execution_fence,
        )

    if operation is WorkerExecutionOperation.ACKNOWLEDGE_CANCEL:
        if (
            kind is not WorkerTargetKind.PIPELINE
            or before.status is not WorkerExecutionStatus.CANCEL_REQUESTED
            or not before.cancellation_pending
            or before.current_attempt_id is not None
            or before.current_effect is not None
            or before.unresolved_effect is not None
            or request.acknowledged_at != header.occurred_at
        ):
            _fail("worker_execution_cancel_acknowledgement_invalid")
        return _completed_after(
            before,
            status=WorkerExecutionStatus.CANCELLED,
        )

    if operation is WorkerExecutionOperation.RENEW:
        _require_active_lease(header, request.lease, operation)
        if (
            header.occurred_at >= request.lease.lease_expires_at
            or request.new_lease_expires_at <= request.lease.lease_expires_at
        ):
            _fail("worker_execution_lease_renewal_invalid")
        return _active_after(
            before,
            status=before.status,
            lease_expires_at=request.new_lease_expires_at,
            current_effect=before.current_effect,
        )

    if operation is WorkerExecutionOperation.AUTHORIZE_EFFECT:
        _require_active_lease(header, request.lease, operation)
        effect = request.effect
        if (
            header.occurred_at >= request.lease.lease_expires_at
            or before.current_effect is not None
            or effect.authorized_at != header.occurred_at
            or effect.idempotency_scope != target.idempotency_scope
            or effect.idempotency_key != target.idempotency_key
            or effect.effect_kind is not _TARGET_EFFECT_KIND[kind]
        ):
            _fail("worker_execution_effect_authorization_invalid")
        return _active_after(
            before,
            status=before.status,
            lease_expires_at=before.lease_expires_at,
            current_effect=_effect_binding_from_authorization(before, effect),
        )

    if operation is WorkerExecutionOperation.COMPLETE:
        _require_active_lease(header, request.lease, operation)
        if (
            request.target_status not in _COMPLETE_AFTER[kind]
            or request.finished_at != header.occurred_at
            or request.finished_at >= request.lease.lease_expires_at
        ):
            _fail("worker_execution_completion_invalid")
        _require_outbox_result_fence(
            target,
            request.target_status,
            request.dispatch_fence,
            operation=operation,
        )
        if (
            before.cancellation_pending
            and request.target_status not in _CANCELLATION_COMPLETE_AFTER
        ):
            _fail("worker_execution_cancellation_retry_forbidden")
        if request.effect_link is not None:
            _require_effect_link(before, request.effect_link)
        if request.durable_result is not None and (
            request.durable_result.recorded_at > request.finished_at
            or request.durable_result.effect != before.current_effect
        ):
            _fail("worker_execution_durable_result_invalid")
        if request.effect_link is not None and request.durable_result is not None:
            _fail("worker_execution_terminal_payload_invalid")
        if (
            request.terminal_proof is not None
            and request.effect_link is not None
        ):
            _fail("worker_execution_terminal_payload_invalid")
        if request.target_status is WorkerExecutionStatus.RETRY:
            if (
                request.next_attempt_at is None
                or request.next_attempt_at < request.finished_at
                or not _retry_allowed(target, before.attempt_count)
                or before.current_effect is not None
                or request.effect_link is not None
                or request.durable_result is not None
            ):
                _fail("worker_execution_retry_invalid")
        elif request.next_attempt_at is not None:
            _fail("worker_execution_retry_invalid")
        if request.target_status in (
            WorkerExecutionStatus.SUCCEEDED,
            WorkerExecutionStatus.PUBLISHED,
        ):
            _require_success_proof(
                kind,
                before.current_effect,
                request.effect_link,
                request.durable_result,
            )
        _require_terminal_proof(
            before,
            request.target_status,
            request.terminal_proof,
        )
        unresolved = None
        if request.target_status is WorkerExecutionStatus.OUTCOME_UNKNOWN:
            if (
                before.current_effect is None
                or request.effect_link is not None
                or request.durable_result is not None
            ):
                _fail("worker_execution_unresolved_effect_required")
            unresolved = before.current_effect
        return _completed_after(
            before,
            status=request.target_status,
            next_attempt_at=request.next_attempt_at,
            unresolved_effect=unresolved,
            result_effect_link=(
                request.effect_link
                if request.terminal_proof is None
                else request.terminal_proof.effect_link
            ),
            durable_result=request.durable_result,
            cancellation_pending=bool(
                before.cancellation_pending
                and request.target_status is WorkerExecutionStatus.OUTCOME_UNKNOWN
            ),
        )

    if operation is WorkerExecutionOperation.EXPIRE:
        _require_active_lease(header, request.lease, operation)
        if (
            request.expired_at != header.occurred_at
            or request.expired_at < request.lease.lease_expires_at
        ):
            _fail("worker_execution_expiry_invalid")
        _require_outbox_result_fence(
            target,
            request.target_status,
            request.dispatch_fence,
            operation=operation,
        )
        if (
            before.cancellation_pending
            and request.target_status is WorkerExecutionStatus.RETRY
        ):
            _fail("worker_execution_cancellation_retry_forbidden")
        if before.current_effect is None:
            if request.target_status is WorkerExecutionStatus.RETRY:
                if (
                    request.next_attempt_at is None
                    or request.next_attempt_at < request.expired_at
                    or not _retry_allowed(target, before.attempt_count)
                ):
                    _fail("worker_execution_retry_invalid")
            elif (
                request.target_status not in _EXPIRE_NO_EFFECT_TERMINAL[kind]
                or request.next_attempt_at is not None
                or (
                    kind is WorkerTargetKind.OUTBOX
                    and request.target_status is WorkerExecutionStatus.DEAD
                    and before.attempt_count != target.max_attempts
                )
            ):
                _fail("worker_execution_expiry_invalid")
            return _completed_after(
                before,
                status=request.target_status,
                next_attempt_at=request.next_attempt_at,
            )
        if (
            request.target_status is not WorkerExecutionStatus.OUTCOME_UNKNOWN
            or request.next_attempt_at is not None
        ):
            _fail("worker_execution_expiry_invalid")
        return _completed_after(
            before,
            status=WorkerExecutionStatus.OUTCOME_UNKNOWN,
            unresolved_effect=before.current_effect,
            cancellation_pending=before.cancellation_pending,
        )

    if operation is WorkerExecutionOperation.RECONCILE:
        if (
            before.status not in _RECONCILE_BEFORE[kind]
            or before.current_attempt_id is not None
            or before.unresolved_effect is None
            or request.target_status not in _RECONCILE_AFTER[kind]
            or request.attempt_id != before.last_attempt_id
            or request.attempt_number != before.last_attempt_number
            or request.execution_fence != before.fence_high_water
            or request.reconciled_at != header.occurred_at
        ):
            _fail("worker_execution_reconcile_invalid")
        _require_outbox_result_fence(
            target,
            request.target_status,
            request.dispatch_fence,
            operation=operation,
        )
        _require_reconcile_proof(request, target, before)
        if request.target_status is WorkerExecutionStatus.RETRY:
            if (
                request.next_attempt_at is None
                or request.next_attempt_at < request.reconciled_at
            ):
                _fail("worker_execution_retry_invalid")
        elif request.next_attempt_at is not None:
            _fail("worker_execution_retry_invalid")
        retain_unresolved = (
            before.unresolved_effect
            if request.target_status is WorkerExecutionStatus.REPAIR_REQUIRED
            else None
        )
        return _completed_after(
            before,
            status=request.target_status,
            next_attempt_at=request.next_attempt_at,
            unresolved_effect=retain_unresolved,
            result_effect_link=request.proof.effect_link,
            durable_result=request.proof.durable_result,
            cancellation_pending=bool(
                before.cancellation_pending
                and request.target_status is WorkerExecutionStatus.REPAIR_REQUIRED
            ),
        )

    _fail("worker_execution_operation_invalid")


def _payload_manifest(value: Any) -> Any:
    if value is None:
        return None
    return {
        "schema": value.schema,
        "codec_id": value.codec_id,
        "codec_version": value.codec_version,
        "bytes": len(value.payload_bytes),
        "sha256": value.payload_sha256.hex(),
    }


def _effect_link_manifest(value: Any) -> Any:
    if value is None:
        return None
    return {
        "attempt_id": value.attempt_id,
        "attempt_number": value.attempt_number,
        "effect_id": value.effect_id,
        "effect_kind": value.effect_kind.value,
        "request_sha256": value.request_sha256.hex(),
        "result_commit_id": value.result_commit_id,
        "provider_receipt": _payload_manifest(value.provider_receipt),
    }


def _effect_binding_manifest(value: Any) -> Any:
    if value is None:
        return None
    return {
        "attempt_id": value.attempt_id,
        "attempt_number": value.attempt_number,
        "effect_id": value.effect_id,
        "effect_kind": value.effect_kind.value,
        "request_sha256": value.request_sha256.hex(),
        "idempotency_scope": value.idempotency_scope,
        "idempotency_key": value.idempotency_key,
    }


def _durable_result_manifest(value: Any) -> Any:
    if value is None:
        return None
    return {
        "result_id": value.result_id,
        "result": _payload_manifest(value.result),
        "recorded_at": value.recorded_at,
        "effect": _effect_binding_manifest(value.effect),
    }


def _reconcile_proof_manifest(value: WorkerReconcileProof) -> dict:
    return {
        "proof_kind": value.proof_kind.value,
        "unresolved_effect": _effect_binding_manifest(value.unresolved_effect),
        "evidence": _payload_manifest(value.evidence),
        "effect_link": _effect_link_manifest(value.effect_link),
        "durable_result": _durable_result_manifest(value.durable_result),
    }


def _terminal_proof_manifest(value: Any) -> Any:
    if value is None:
        return None
    return {
        "proof_kind": value.proof_kind.value,
        "effect": _effect_binding_manifest(value.effect),
        "evidence": _payload_manifest(value.evidence),
        "effect_link": _effect_link_manifest(value.effect_link),
    }


def _outbox_order_manifest(value: Any) -> Any:
    if value is None:
        return None
    return {
        "sequence": value.sequence,
        "repository_revision": value.repository_revision,
        "ordinal": value.ordinal,
    }


def _dispatch_fence_manifest(value: Any) -> Any:
    if value is None:
        return None
    def cursor(candidate: WorkerOutboxDispatchCursor) -> dict:
        return {
            "sequence": candidate.sequence,
            "repository_revision": candidate.repository_revision,
            "ordinal": candidate.ordinal,
        }

    def state(candidate: WorkerOutboxDispatchState) -> dict:
        return {
            "control_revision": candidate.control_revision,
            "published": cursor(candidate.published),
            "inflight": _outbox_order_manifest(candidate.inflight),
        }

    return {"before": state(value.before), "after": state(value.after)}


def _after_manifest(value: WorkerAfterState) -> dict:
    return {
        "status": value.status.value,
        "state_revision": value.state_revision,
        "attempt_count": value.attempt_count,
        "last_attempt_id": value.last_attempt_id,
        "last_attempt_number": value.last_attempt_number,
        "current_attempt_id": value.current_attempt_id,
        "current_attempt_number": value.current_attempt_number,
        "lease_owner": value.lease_owner,
        "lease_fence": value.lease_fence,
        "lease_expires_at": value.lease_expires_at,
        "fence_high_water": value.fence_high_water,
        "current_effect": _effect_binding_manifest(value.current_effect),
        "unresolved_effect": _effect_binding_manifest(value.unresolved_effect),
        "next_attempt_at": value.next_attempt_at,
        "cancellation_pending": value.cancellation_pending,
        "result_effect_link": _effect_link_manifest(value.result_effect_link),
        "durable_result": _durable_result_manifest(value.durable_result),
    }


def _authority_manifest(authority: AuthorityBinding) -> dict:
    return {
        "authority": authority.authority,
        "authority_state": authority.authority_state,
        "epoch": authority.authority_epoch,
        "source_admission_revision": authority.source_admission_revision,
        "revision": authority.authority_revision,
        "control_revision": authority.control_revision,
        "cutover_id": authority.cutover_id,
        "runtime_generation": authority.runtime_generation,
        "code_digest": authority.code_digest.hex(),
    }


def _target_manifest(target: WorkerTarget) -> dict:
    return {
        "kind": target.target_kind.value,
        "id": target.target_id,
        "project_id": target.project_id,
        "generation": target.generation,
        "document_id": target.document_id,
        "origin_commit_id": target.origin_commit_id,
        "payload": _payload_manifest(target.payload),
        "replay_policy": target.replay_policy.value,
        "idempotency_policy": target.idempotency_policy.value,
        "idempotency_scope": target.idempotency_scope,
        "idempotency_key": target.idempotency_key,
        "max_attempts": target.max_attempts,
        "outbox_order": _outbox_order_manifest(target.outbox_order),
        "pipeline_id": target.pipeline_id,
        "intent_type": target.intent_type,
    }


def _control_intent_manifest(
    value: PipelineBusinessControlIntentBinding,
) -> dict:
    return {
        "origin_commit_id": value.origin_commit_id,
        "intent_type": value.intent_type,
        "idempotency_key": value.idempotency_key,
        "payload": _payload_manifest(value.payload),
        "replay_policy": value.replay_policy.value,
        "idempotency_policy": value.idempotency_policy.value,
        "max_attempts": value.max_attempts,
    }


def _before_manifest(before: Any) -> Any:
    if before is None:
        return None
    return {
        "status": before.status.value,
        "state_revision": before.state_revision,
        "attempt_count": before.attempt_count,
        "last_attempt_id": before.last_attempt_id,
        "last_attempt_number": before.last_attempt_number,
        "current_attempt_id": before.current_attempt_id,
        "current_attempt_number": before.current_attempt_number,
        "lease_owner": before.lease_owner,
        "lease_fence": before.lease_fence,
        "lease_expires_at": before.lease_expires_at,
        "fence_high_water": before.fence_high_water,
        "current_effect": _effect_binding_manifest(before.current_effect),
        "unresolved_effect": _effect_binding_manifest(before.unresolved_effect),
        "next_attempt_at": before.next_attempt_at,
        "cancellation_pending": before.cancellation_pending,
    }


def _header_manifest(header: WorkerRequestHeader) -> dict:
    return {
        "request_transaction_id": header.request_transaction_id,
        "occurred_at": header.occurred_at,
        "authority": _authority_manifest(header.authority),
        "target": _target_manifest(header.target),
        "before": _before_manifest(header.before),
    }


def _request_detail(request: Any, operation: WorkerExecutionOperation) -> dict:
    if operation is WorkerExecutionOperation.CLAIM:
        attempt = request.attempt
        return {
            "attempt_id": attempt.attempt_id,
            "attempt_number": attempt.attempt_number,
            "previous_attempt_id": attempt.previous_attempt_id,
            "previous_attempt_number": attempt.previous_attempt_number,
            "lease_owner": attempt.lease_owner,
            "execution_fence": attempt.execution_fence,
            "started_at": attempt.started_at,
            "lease_expires_at": attempt.lease_expires_at,
            "dispatch_fence": _dispatch_fence_manifest(request.dispatch_fence),
        }
    if operation is WorkerExecutionOperation.ACKNOWLEDGE_CANCEL:
        return {"acknowledged_at": request.acknowledged_at}
    if operation is WorkerExecutionOperation.RENEW:
        return {
            "lease": _lease_manifest(request.lease),
            "new_lease_expires_at": request.new_lease_expires_at,
        }
    if operation is WorkerExecutionOperation.AUTHORIZE_EFFECT:
        effect = request.effect
        return {
            "lease": _lease_manifest(request.lease),
            "effect": {
                "effect_id": effect.effect_id,
                "effect_kind": effect.effect_kind.value,
                "idempotency_scope": effect.idempotency_scope,
                "idempotency_key": effect.idempotency_key,
                "request": _payload_manifest(effect.request),
                "authorized_at": effect.authorized_at,
            },
        }
    if operation is WorkerExecutionOperation.COMPLETE:
        return {
            "lease": _lease_manifest(request.lease),
            "target_status": request.target_status.value,
            "finished_at": request.finished_at,
            "result": _payload_manifest(request.result),
            "error": _payload_manifest(request.error),
            "effect_link": _effect_link_manifest(request.effect_link),
            "durable_result": _durable_result_manifest(request.durable_result),
            "terminal_proof": _terminal_proof_manifest(request.terminal_proof),
            "next_attempt_at": request.next_attempt_at,
            "dispatch_fence": _dispatch_fence_manifest(request.dispatch_fence),
        }
    if operation is WorkerExecutionOperation.EXPIRE:
        return {
            "lease": _lease_manifest(request.lease),
            "expired_at": request.expired_at,
            "target_status": request.target_status.value,
            "next_attempt_at": request.next_attempt_at,
            "dispatch_fence": _dispatch_fence_manifest(request.dispatch_fence),
        }
    return {
        "attempt_id": request.attempt_id,
        "attempt_number": request.attempt_number,
        "execution_fence": request.execution_fence,
        "target_status": request.target_status.value,
        "proof": _reconcile_proof_manifest(request.proof),
        "reconciled_at": request.reconciled_at,
        "next_attempt_at": request.next_attempt_at,
        "dispatch_fence": _dispatch_fence_manifest(request.dispatch_fence),
    }


def _lease_manifest(value: WorkerLeaseToken) -> dict:
    return {
        "attempt_id": value.attempt_id,
        "attempt_number": value.attempt_number,
        "lease_owner": value.lease_owner,
        "execution_fence": value.execution_fence,
        "lease_expires_at": value.lease_expires_at,
    }


def _build_manifest(
    request: Any,
    operation: WorkerExecutionOperation,
    after_state: WorkerAfterState,
) -> bytes:
    manifest = {
        "schema": WORKER_EXECUTION_MANIFEST_SCHEMA,
        "protocol": WORKER_EXECUTION_PROTOCOL,
        "status": WORKER_EXECUTION_STATUS_PREPARED,
        "operation": operation.value,
        "header": _header_manifest(request.header),
        "transition": {
            "before_state_revision": request.header.before.state_revision,
            "after_state_revision": request.header.before.state_revision + 1,
            "before_status": request.header.before.status.value,
            "after_status": after_state.status.value,
        },
        "after": _after_manifest(after_state),
        "request": _request_detail(request, operation),
    }
    try:
        return encode_payload_bounded(
            manifest,
            MAX_WORKER_EXECUTION_MANIFEST_BYTES,
            MAX_WORKER_EXECUTION_MANIFEST_NODES,
        )
    except BusinessCodecError:
        _fail("worker_execution_manifest_invalid")


def _build_control_manifest(
    request: PipelineBusinessControlRequest,
    after_state: WorkerAfterState,
) -> bytes:
    before = request.before
    manifest = {
        "schema": PIPELINE_BUSINESS_CONTROL_MANIFEST_SCHEMA,
        "protocol": PIPELINE_BUSINESS_CONTROL_PROTOCOL,
        "status": PIPELINE_BUSINESS_CONTROL_STATUS_PREPARED,
        "operation": request.operation.value,
        "request_transaction_id": request.request_transaction_id,
        "occurred_at": request.occurred_at,
        "authority": _authority_manifest(request.authority),
        "target": _target_manifest(request.target),
        "control_intent": _control_intent_manifest(request.control_intent),
        "transition": {
            "before_state_revision": (
                None if before is None else before.state_revision
            ),
            "after_state_revision": after_state.state_revision,
            "before_status": None if before is None else before.status.value,
            "after_status": after_state.status.value,
        },
        "before": _before_manifest(before),
        "after": _after_manifest(after_state),
    }
    try:
        return encode_payload_bounded(
            manifest,
            MAX_WORKER_EXECUTION_MANIFEST_BYTES,
            MAX_WORKER_EXECUTION_MANIFEST_NODES,
        )
    except BusinessCodecError:
        _fail("pipeline_business_control_manifest_invalid")


def _encoded_digest_manifest(value: Any, code: str) -> bytes:
    try:
        return encode_payload_bounded(
            value,
            MAX_WORKER_EXECUTION_MANIFEST_BYTES,
            MAX_WORKER_EXECUTION_MANIFEST_NODES,
        )
    except BusinessCodecError:
        _fail(code)


def _after_state_digest(request: Any, after_state: WorkerAfterState) -> bytes:
    dispatch_fence = getattr(request, "dispatch_fence", None)
    projection = {
        "worker_after": _after_manifest(after_state),
        "dispatch_after": (
            None
            if dispatch_fence is None
            else _dispatch_fence_manifest(dispatch_fence)["after"]
        ),
    }
    return _domain_digest(
        WORKER_EXECUTION_AFTER_STATE_HASH_DOMAIN,
        _encoded_digest_manifest(
            projection,
            "worker_execution_after_state_invalid",
        ),
    )


def _receipt_digest(
    *,
    domain: bytes,
    request_class: str,
    operation: str,
    target_kind: WorkerTargetKind,
    target_id: str,
    before_status: Optional[WorkerExecutionStatus],
    after_status: WorkerExecutionStatus,
    before_state_revision: Optional[int],
    after_state_revision: int,
    sequence: int,
    previous_tail: bytes,
    request_transaction_id: str,
    request_sha256: bytes,
    after_state_sha256: bytes,
    occurred_at: str,
) -> bytes:
    payload = {
        "request_class": request_class,
        "operation": operation,
        "target_kind": target_kind.value,
        "target_id": target_id,
        "before_status": (
            None if before_status is None else before_status.value
        ),
        "after_status": after_status.value,
        "before_state_revision": before_state_revision,
        "after_state_revision": after_state_revision,
        "sequence": sequence,
        "previous_ledger_tail_sha256": previous_tail.hex(),
        "request_transaction_id": request_transaction_id,
        "request_sha256": request_sha256.hex(),
        "after_state_sha256": after_state_sha256.hex(),
        "occurred_at": occurred_at,
    }
    return _domain_digest(
        domain,
        _encoded_digest_manifest(payload, "worker_receipt_manifest_invalid"),
    )


def _ledger_tail_digest(
    sequence: int,
    previous_tail: bytes,
    receipt_sha256: bytes,
) -> bytes:
    return _domain_digest(
        WORKER_LEDGER_TAIL_HASH_DOMAIN,
        sequence.to_bytes(8, "big"),
        previous_tail,
        receipt_sha256,
    )


def _build_worker_receipt(
    request: Any,
    operation: WorkerExecutionOperation,
    closure: WorkerExecutionClosure,
    before_ledger: WorkerLedgerHead,
) -> Tuple[WorkerExecutionReceiptEvidence, WorkerLedgerHead]:
    sequence = before_ledger.sequence + 1
    transaction_id = request.header.request_transaction_id
    occurred_at = request.header.occurred_at
    receipt_sha256 = _receipt_digest(
        domain=WORKER_EXECUTION_RECEIPT_HASH_DOMAIN,
        request_class=WORKER_EXECUTION_REQUEST_CLASS,
        operation=operation.value,
        target_kind=request.header.target.target_kind,
        target_id=request.header.target.target_id,
        before_status=closure.before_status,
        after_status=closure.after_status,
        before_state_revision=closure.before_state_revision,
        after_state_revision=closure.after_state_revision,
        sequence=sequence,
        previous_tail=before_ledger.tail_sha256,
        request_transaction_id=transaction_id,
        request_sha256=closure.request_sha256,
        after_state_sha256=closure.after_state_sha256,
        occurred_at=occurred_at,
    )
    tail = _ledger_tail_digest(
        sequence,
        before_ledger.tail_sha256,
        receipt_sha256,
    )
    evidence = WorkerExecutionReceiptEvidence(
        request_transaction_id=transaction_id,
        receipt_parent=WorkerExecutionGenericReceiptEvidence(
            WORKER_EXECUTION_TRANSACTION_PREFIX + transaction_id,
            WORKER_EXECUTION_TRANSACTION_KIND,
            occurred_at,
        ),
        request_class=WORKER_EXECUTION_REQUEST_CLASS,
        operation=operation,
        target_kind=request.header.target.target_kind,
        target_id=request.header.target.target_id,
        before_status=closure.before_status,
        after_status=closure.after_status,
        before_state_revision=closure.before_state_revision,
        after_state_revision=closure.after_state_revision,
        sequence=sequence,
        previous_ledger_tail_sha256=before_ledger.tail_sha256,
        ledger_tail_sha256=tail,
        request_sha256=closure.request_sha256,
        after_state_sha256=closure.after_state_sha256,
        occurred_at=occurred_at,
        receipt_sha256=receipt_sha256,
    )
    return evidence, WorkerLedgerHead(sequence, tail)


def _build_control_receipt(
    request: PipelineBusinessControlRequest,
    closure: PipelineBusinessControlClosure,
    before_ledger: WorkerLedgerHead,
) -> Tuple[PipelineBusinessControlReceiptEvidence, WorkerLedgerHead]:
    sequence = before_ledger.sequence + 1
    transaction_id = request.request_transaction_id
    receipt_sha256 = _receipt_digest(
        domain=PIPELINE_BUSINESS_CONTROL_RECEIPT_HASH_DOMAIN,
        request_class=PIPELINE_BUSINESS_CONTROL_REQUEST_CLASS,
        operation=request.operation.value,
        target_kind=request.target.target_kind,
        target_id=request.target.target_id,
        before_status=closure.before_status,
        after_status=closure.after_status,
        before_state_revision=closure.before_state_revision,
        after_state_revision=closure.after_state_revision,
        sequence=sequence,
        previous_tail=before_ledger.tail_sha256,
        request_transaction_id=transaction_id,
        request_sha256=closure.request_sha256,
        after_state_sha256=closure.after_state_sha256,
        occurred_at=request.occurred_at,
    )
    tail = _ledger_tail_digest(
        sequence,
        before_ledger.tail_sha256,
        receipt_sha256,
    )
    evidence = PipelineBusinessControlReceiptEvidence(
        request_transaction_id=transaction_id,
        receipt_parent=WorkerExecutionGenericReceiptEvidence(
            PIPELINE_BUSINESS_CONTROL_TRANSACTION_PREFIX + transaction_id,
            PIPELINE_BUSINESS_CONTROL_TRANSACTION_KIND,
            request.occurred_at,
        ),
        request_class=PIPELINE_BUSINESS_CONTROL_REQUEST_CLASS,
        operation=request.operation,
        target_kind=request.target.target_kind,
        target_id=request.target.target_id,
        before_status=closure.before_status,
        after_status=closure.after_status,
        before_state_revision=closure.before_state_revision,
        after_state_revision=closure.after_state_revision,
        sequence=sequence,
        previous_ledger_tail_sha256=before_ledger.tail_sha256,
        ledger_tail_sha256=tail,
        request_sha256=closure.request_sha256,
        after_state_sha256=closure.after_state_sha256,
        occurred_at=request.occurred_at,
        receipt_sha256=receipt_sha256,
    )
    return evidence, WorkerLedgerHead(sequence, tail)


def _prepare(
    value: Any,
    before_ledger: Any = EMPTY_WORKER_LEDGER_HEAD,
) -> _PreparedWorkerExecution:
    try:
        request, operation = _canonical_request(value)
    except (
        AttributeError,
        BusinessTransactionContractError,
        TypeError,
        WorkerExecutionContractError,
    ):
        _fail("worker_execution_request_changed")
    if request != value:
        _fail("worker_execution_request_changed")
    try:
        exact_before_ledger = _canonical_ledger_head(before_ledger)
    except (AttributeError, TypeError, WorkerExecutionContractError):
        _fail("worker_ledger_head_invalid")
    if exact_before_ledger != before_ledger:
        _fail("worker_ledger_head_changed")
    after_state = _validate_request(request, operation)
    manifest_bytes = _build_manifest(request, operation, after_state)
    before = request.header.before
    after_state_sha256 = _after_state_digest(request, after_state)
    closure = WorkerExecutionClosure(
        operation=operation,
        request_transaction_id=request.header.request_transaction_id,
        before_state_revision=before.state_revision,
        after_state_revision=before.state_revision + 1,
        before_status=before.status,
        after_status=after_state.status,
        after_state=after_state,
        after_state_sha256=after_state_sha256,
        manifest_bytes=manifest_bytes,
        request_sha256=_manifest_digest(manifest_bytes),
    )
    receipt, after_ledger = _build_worker_receipt(
        request,
        operation,
        closure,
        exact_before_ledger,
    )
    return _PreparedWorkerExecution(
        request,
        operation,
        after_state,
        closure,
        exact_before_ledger,
        after_ledger,
        receipt,
    )


def _prepare_control(
    value: Any,
    before_ledger: Any = EMPTY_WORKER_LEDGER_HEAD,
) -> _PreparedPipelineBusinessControl:
    try:
        request = _canonical_control_request(value)
    except (
        AttributeError,
        BusinessTransactionContractError,
        TypeError,
        WorkerExecutionContractError,
    ):
        _fail("pipeline_business_control_request_changed")
    if request != value:
        _fail("pipeline_business_control_request_changed")
    try:
        exact_before_ledger = _canonical_ledger_head(before_ledger)
    except (AttributeError, TypeError, WorkerExecutionContractError):
        _fail("worker_ledger_head_invalid")
    if exact_before_ledger != before_ledger:
        _fail("worker_ledger_head_changed")
    after_state = _validate_control_request(request)
    manifest_bytes = _build_control_manifest(request, after_state)
    before = request.before
    after_state_sha256 = _after_state_digest(request, after_state)
    closure = PipelineBusinessControlClosure(
        operation=request.operation,
        request_transaction_id=request.request_transaction_id,
        before_state_revision=(
            None if before is None else before.state_revision
        ),
        after_state_revision=after_state.state_revision,
        before_status=None if before is None else before.status,
        after_status=after_state.status,
        after_state=after_state,
        after_state_sha256=after_state_sha256,
        manifest_bytes=manifest_bytes,
        request_sha256=_control_manifest_digest(manifest_bytes),
    )
    receipt, after_ledger = _build_control_receipt(
        request,
        closure,
        exact_before_ledger,
    )
    return _PreparedPipelineBusinessControl(
        request,
        after_state,
        closure,
        exact_before_ledger,
        after_ledger,
        receipt,
    )


def _issue_worker_execution_plan(
    request: Any,
    *,
    before_ledger: WorkerLedgerHead = EMPTY_WORKER_LEDGER_HEAD,
) -> WorkerExecutionPlan:
    """Private typed issuer; raw dictionaries and callbacks are rejected."""

    return WorkerExecutionPlan(
        _prepare(request, before_ledger),
        _issuer=_PLAN_ISSUER,
    )


def _require_worker_execution_plan(value: Any) -> WorkerExecutionPlan:
    """Rebuild the complete request and digest before a writer may trust it."""

    if (
        type(value) is not WorkerExecutionPlan
        or getattr(value, "_issuer", None) is not _PLAN_ISSUER
        or getattr(value, "protocol", None) != WORKER_EXECUTION_PROTOCOL
        or getattr(value, "status", None) != WORKER_EXECUTION_STATUS_PREPARED
    ):
        _fail("worker_execution_plan_invalid")
    prepared = _prepare(value.request, value.before_ledger)
    if (
        prepared.request != value.request
        or prepared.request.header.authority != value.authority
        or prepared.request.header.target != value.target
        or prepared.request.header.before != value.before
        or prepared.operation is not value.operation
        or prepared.closure != value.closure
        or prepared.before_ledger != value.before_ledger
        or prepared.after_ledger != value.after_ledger
        or prepared.expected_receipt != value.expected_receipt
        or not hmac.compare_digest(
            prepared.closure.request_sha256,
            value.closure.request_sha256,
        )
    ):
        _fail("worker_execution_plan_changed")
    return value


def _issue_pipeline_business_control_plan(
    request: PipelineBusinessControlRequest,
    *,
    before_ledger: WorkerLedgerHead = EMPTY_WORKER_LEDGER_HEAD,
) -> PipelineBusinessControlPlan:
    """Private business issuer; it cannot issue worker execution operations."""

    return PipelineBusinessControlPlan(
        _prepare_control(request, before_ledger),
        _issuer=_CONTROL_PLAN_ISSUER,
    )


def _require_pipeline_business_control_plan(
    value: Any,
) -> PipelineBusinessControlPlan:
    if (
        type(value) is not PipelineBusinessControlPlan
        or getattr(value, "_issuer", None) is not _CONTROL_PLAN_ISSUER
        or getattr(value, "protocol", None) != PIPELINE_BUSINESS_CONTROL_PROTOCOL
        or getattr(value, "status", None)
        != PIPELINE_BUSINESS_CONTROL_STATUS_PREPARED
    ):
        _fail("pipeline_business_control_plan_invalid")
    prepared = _prepare_control(value.request, value.before_ledger)
    if (
        prepared.request != value.request
        or prepared.request.authority != value.authority
        or prepared.request.target != value.target
        or prepared.request.control_intent != value.control_intent
        or prepared.request.operation is not value.operation
        or prepared.closure != value.closure
        or prepared.before_ledger != value.before_ledger
        or prepared.after_ledger != value.after_ledger
        or prepared.expected_receipt != value.expected_receipt
        or not hmac.compare_digest(
            prepared.closure.request_sha256,
            value.closure.request_sha256,
        )
    ):
        _fail("pipeline_business_control_plan_changed")
    return value


def _canonical_generic_receipt(
    value: Any,
) -> WorkerExecutionGenericReceiptEvidence:
    return WorkerExecutionGenericReceiptEvidence(
        transaction_id=value.transaction_id,
        kind=value.kind,
        committed_at=value.committed_at,
    )


def _rebuild_worker_receipt(
    value: Any,
) -> WorkerExecutionReceiptEvidence:
    if type(value) is not WorkerExecutionReceiptEvidence:
        _fail("worker_receipt_type_invalid")
    try:
        parent = _canonical_generic_receipt(value.receipt_parent)
        receipt_sha256 = _receipt_digest(
            domain=WORKER_EXECUTION_RECEIPT_HASH_DOMAIN,
            request_class=value.request_class,
            operation=value.operation.value,
            target_kind=value.target_kind,
            target_id=value.target_id,
            before_status=value.before_status,
            after_status=value.after_status,
            before_state_revision=value.before_state_revision,
            after_state_revision=value.after_state_revision,
            sequence=value.sequence,
            previous_tail=value.previous_ledger_tail_sha256,
            request_transaction_id=value.request_transaction_id,
            request_sha256=value.request_sha256,
            after_state_sha256=value.after_state_sha256,
            occurred_at=value.occurred_at,
        )
        tail = _ledger_tail_digest(
            value.sequence,
            value.previous_ledger_tail_sha256,
            receipt_sha256,
        )
        rebuilt = WorkerExecutionReceiptEvidence(
            request_transaction_id=value.request_transaction_id,
            receipt_parent=parent,
            request_class=value.request_class,
            operation=value.operation,
            target_kind=value.target_kind,
            target_id=value.target_id,
            before_status=value.before_status,
            after_status=value.after_status,
            before_state_revision=value.before_state_revision,
            after_state_revision=value.after_state_revision,
            sequence=value.sequence,
            previous_ledger_tail_sha256=value.previous_ledger_tail_sha256,
            ledger_tail_sha256=tail,
            request_sha256=value.request_sha256,
            after_state_sha256=value.after_state_sha256,
            occurred_at=value.occurred_at,
            receipt_sha256=receipt_sha256,
        )
    except (
        AttributeError,
        TypeError,
        WorkerExecutionContractError,
        WorkerExecutionIntegrityError,
    ):
        _fail("worker_receipt_changed")
    if (
        rebuilt != value
        or not hmac.compare_digest(rebuilt.receipt_sha256, value.receipt_sha256)
        or not hmac.compare_digest(
            rebuilt.ledger_tail_sha256,
            value.ledger_tail_sha256,
        )
    ):
        _fail("worker_receipt_changed")
    return rebuilt


def _rebuild_control_receipt(
    value: Any,
) -> PipelineBusinessControlReceiptEvidence:
    if type(value) is not PipelineBusinessControlReceiptEvidence:
        _fail("pipeline_business_control_receipt_type_invalid")
    try:
        parent = _canonical_generic_receipt(value.receipt_parent)
        receipt_sha256 = _receipt_digest(
            domain=PIPELINE_BUSINESS_CONTROL_RECEIPT_HASH_DOMAIN,
            request_class=value.request_class,
            operation=value.operation.value,
            target_kind=value.target_kind,
            target_id=value.target_id,
            before_status=value.before_status,
            after_status=value.after_status,
            before_state_revision=value.before_state_revision,
            after_state_revision=value.after_state_revision,
            sequence=value.sequence,
            previous_tail=value.previous_ledger_tail_sha256,
            request_transaction_id=value.request_transaction_id,
            request_sha256=value.request_sha256,
            after_state_sha256=value.after_state_sha256,
            occurred_at=value.occurred_at,
        )
        tail = _ledger_tail_digest(
            value.sequence,
            value.previous_ledger_tail_sha256,
            receipt_sha256,
        )
        rebuilt = PipelineBusinessControlReceiptEvidence(
            request_transaction_id=value.request_transaction_id,
            receipt_parent=parent,
            request_class=value.request_class,
            operation=value.operation,
            target_kind=value.target_kind,
            target_id=value.target_id,
            before_status=value.before_status,
            after_status=value.after_status,
            before_state_revision=value.before_state_revision,
            after_state_revision=value.after_state_revision,
            sequence=value.sequence,
            previous_ledger_tail_sha256=value.previous_ledger_tail_sha256,
            ledger_tail_sha256=tail,
            request_sha256=value.request_sha256,
            after_state_sha256=value.after_state_sha256,
            occurred_at=value.occurred_at,
            receipt_sha256=receipt_sha256,
        )
    except (
        AttributeError,
        TypeError,
        WorkerExecutionContractError,
        WorkerExecutionIntegrityError,
    ):
        _fail("pipeline_business_control_receipt_changed")
    if (
        rebuilt != value
        or not hmac.compare_digest(rebuilt.receipt_sha256, value.receipt_sha256)
        or not hmac.compare_digest(
            rebuilt.ledger_tail_sha256,
            value.ledger_tail_sha256,
        )
    ):
        _fail("pipeline_business_control_receipt_changed")
    return rebuilt


def _worker_classification(
    status: WorkerReceiptReconcileStatus,
    plan: WorkerExecutionPlan,
    *,
    evidence: Optional[WorkerExecutionReceiptEvidence] = None,
    error_code: str = "",
) -> WorkerExecutionReceiptClassification:
    return WorkerExecutionReceiptClassification(
        status,
        plan.closure.request_transaction_id,
        evidence,
        error_code,
    )


def _control_classification(
    status: WorkerReceiptReconcileStatus,
    plan: PipelineBusinessControlPlan,
    *,
    evidence: Optional[PipelineBusinessControlReceiptEvidence] = None,
    error_code: str = "",
) -> PipelineBusinessControlReceiptClassification:
    return PipelineBusinessControlReceiptClassification(
        status,
        plan.closure.request_transaction_id,
        evidence,
        error_code,
    )


def _classify_worker_execution_receipt(
    plan: WorkerExecutionPlan,
    fresh: WorkerExecutionFreshObservation,
) -> WorkerExecutionReceiptClassification:
    """Classify a capability-stamped receipt/head read, never naked evidence."""

    plan = _require_worker_execution_plan(plan)
    try:
        persisted = _unwrap_worker_execution_fresh_observation(fresh)
        canonical = WorkerExecutionPersistedObservation(
            read_status=persisted.read_status,
            receipt=(
                None
                if persisted.receipt is None
                else _canonical_generic_receipt(persisted.receipt)
            ),
            execution_receipt=persisted.execution_receipt,
            ledger_head=(
                None
                if persisted.ledger_head is None
                else _canonical_ledger_head(persisted.ledger_head)
            ),
            ledger_membership=_canonical_ledger_membership(
                persisted.ledger_membership
            ),
            error_code=persisted.error_code,
        )
    except (
        AttributeError,
        TypeError,
        WorkerExecutionContractError,
    ):
        _fail("worker_persisted_observation_changed")
    if canonical != persisted:
        _fail("worker_persisted_observation_changed")
    if canonical.read_status is WorkerPersistedReadStatus.UNKNOWN:
        return _worker_classification(
            WorkerReceiptReconcileStatus.UNKNOWN,
            plan,
            error_code=canonical.error_code,
        )
    receipt = canonical.receipt
    specialised = canonical.execution_receipt
    head = canonical.ledger_head
    membership = canonical.ledger_membership
    assert head is not None
    if receipt is None and specialised is None:
        if head == plan.before_ledger:
            return _worker_classification(
                WorkerReceiptReconcileStatus.ABSENT,
                plan,
            )
        return _worker_classification(
            WorkerReceiptReconcileStatus.CONFLICT,
            plan,
            error_code="worker_receipt_ledger_head_conflict",
        )
    if receipt is None or specialised is None:
        return _worker_classification(
            WorkerReceiptReconcileStatus.CORRUPT,
            plan,
            error_code="worker_receipt_pair_incomplete",
        )
    try:
        rebuilt = _rebuild_worker_receipt(specialised)
    except WorkerExecutionIntegrityError:
        return _worker_classification(
            WorkerReceiptReconcileStatus.CORRUPT,
            plan,
            error_code="worker_receipt_corrupt",
        )
    if receipt != rebuilt.receipt_parent:
        status = (
            WorkerReceiptReconcileStatus.CONFLICT
            if receipt.transaction_id
            == plan.expected_receipt.receipt_parent.transaction_id
            and receipt.kind != WORKER_EXECUTION_TRANSACTION_KIND
            else WorkerReceiptReconcileStatus.CORRUPT
        )
        return _worker_classification(
            status,
            plan,
            error_code=(
                "worker_receipt_identity_conflict"
                if status is WorkerReceiptReconcileStatus.CONFLICT
                else "worker_receipt_parent_mismatch"
            ),
        )
    if (
        rebuilt.request_transaction_id
        != plan.expected_receipt.request_transaction_id
        or receipt.transaction_id
        != plan.expected_receipt.receipt_parent.transaction_id
    ):
        return _worker_classification(
            WorkerReceiptReconcileStatus.CONFLICT,
            plan,
            error_code="worker_receipt_identity_conflict",
        )
    if rebuilt != plan.expected_receipt:
        return _worker_classification(
            WorkerReceiptReconcileStatus.CONFLICT,
            plan,
            error_code="worker_receipt_content_conflict",
        )
    same_head = bool(
        head.sequence == rebuilt.sequence
        and hmac.compare_digest(
            head.tail_sha256,
            rebuilt.ledger_tail_sha256,
        )
    )
    later_member = bool(
        head.sequence > rebuilt.sequence
        and membership
        == WorkerLedgerMembershipEvidence(
            rebuilt.sequence,
            rebuilt.ledger_tail_sha256,
            head,
        )
    )
    if not (same_head or later_member):
        return _worker_classification(
            WorkerReceiptReconcileStatus.CORRUPT,
            plan,
            error_code="worker_receipt_ledger_membership_invalid",
        )
    return _worker_classification(
        WorkerReceiptReconcileStatus.EXACT,
        plan,
        evidence=rebuilt,
    )


def _classify_pipeline_business_control_receipt(
    plan: PipelineBusinessControlPlan,
    fresh: PipelineBusinessControlFreshObservation,
) -> PipelineBusinessControlReceiptClassification:
    plan = _require_pipeline_business_control_plan(plan)
    try:
        persisted = _unwrap_pipeline_business_control_fresh_observation(fresh)
        canonical = PipelineBusinessControlPersistedObservation(
            read_status=persisted.read_status,
            receipt=(
                None
                if persisted.receipt is None
                else _canonical_generic_receipt(persisted.receipt)
            ),
            control_receipt=persisted.control_receipt,
            ledger_head=(
                None
                if persisted.ledger_head is None
                else _canonical_ledger_head(persisted.ledger_head)
            ),
            ledger_membership=_canonical_ledger_membership(
                persisted.ledger_membership
            ),
            error_code=persisted.error_code,
        )
    except (
        AttributeError,
        TypeError,
        WorkerExecutionContractError,
    ):
        _fail("pipeline_business_control_persisted_observation_changed")
    if canonical != persisted:
        _fail("pipeline_business_control_persisted_observation_changed")
    if canonical.read_status is WorkerPersistedReadStatus.UNKNOWN:
        return _control_classification(
            WorkerReceiptReconcileStatus.UNKNOWN,
            plan,
            error_code=canonical.error_code,
        )
    receipt = canonical.receipt
    specialised = canonical.control_receipt
    head = canonical.ledger_head
    membership = canonical.ledger_membership
    assert head is not None
    if receipt is None and specialised is None:
        if head == plan.before_ledger:
            return _control_classification(
                WorkerReceiptReconcileStatus.ABSENT,
                plan,
            )
        return _control_classification(
            WorkerReceiptReconcileStatus.CONFLICT,
            plan,
            error_code="pipeline_business_control_ledger_head_conflict",
        )
    if receipt is None or specialised is None:
        return _control_classification(
            WorkerReceiptReconcileStatus.CORRUPT,
            plan,
            error_code="pipeline_business_control_receipt_pair_incomplete",
        )
    try:
        rebuilt = _rebuild_control_receipt(specialised)
    except WorkerExecutionIntegrityError:
        return _control_classification(
            WorkerReceiptReconcileStatus.CORRUPT,
            plan,
            error_code="pipeline_business_control_receipt_corrupt",
        )
    if receipt != rebuilt.receipt_parent:
        status = (
            WorkerReceiptReconcileStatus.CONFLICT
            if receipt.transaction_id
            == plan.expected_receipt.receipt_parent.transaction_id
            and receipt.kind != PIPELINE_BUSINESS_CONTROL_TRANSACTION_KIND
            else WorkerReceiptReconcileStatus.CORRUPT
        )
        return _control_classification(
            status,
            plan,
            error_code=(
                "pipeline_business_control_receipt_identity_conflict"
                if status is WorkerReceiptReconcileStatus.CONFLICT
                else "pipeline_business_control_receipt_parent_mismatch"
            ),
        )
    if (
        rebuilt.request_transaction_id
        != plan.expected_receipt.request_transaction_id
        or receipt.transaction_id
        != plan.expected_receipt.receipt_parent.transaction_id
    ):
        return _control_classification(
            WorkerReceiptReconcileStatus.CONFLICT,
            plan,
            error_code="pipeline_business_control_receipt_identity_conflict",
        )
    if rebuilt != plan.expected_receipt:
        return _control_classification(
            WorkerReceiptReconcileStatus.CONFLICT,
            plan,
            error_code="pipeline_business_control_receipt_content_conflict",
        )
    same_head = bool(
        head.sequence == rebuilt.sequence
        and hmac.compare_digest(
            head.tail_sha256,
            rebuilt.ledger_tail_sha256,
        )
    )
    later_member = bool(
        head.sequence > rebuilt.sequence
        and membership
        == WorkerLedgerMembershipEvidence(
            rebuilt.sequence,
            rebuilt.ledger_tail_sha256,
            head,
        )
    )
    if not (same_head or later_member):
        return _control_classification(
            WorkerReceiptReconcileStatus.CORRUPT,
            plan,
            error_code="pipeline_business_control_ledger_membership_invalid",
        )
    return _control_classification(
        WorkerReceiptReconcileStatus.EXACT,
        plan,
        evidence=rebuilt,
    )


def _classify_pipeline_business_control_request_identity(
    plan: PipelineBusinessControlPlan,
    observed_transaction_id: Any,
    observed_request_sha256: Any,
) -> PipelineBusinessControlRequestIdentityStatus:
    """Classify control request identity without accepting a worker plan."""

    plan = _require_pipeline_business_control_plan(plan)
    if observed_transaction_id is None and observed_request_sha256 is None:
        return PipelineBusinessControlRequestIdentityStatus.ABSENT
    if (
        type(observed_transaction_id) is not str
        or type(observed_request_sha256) is not bytes
        or len(observed_request_sha256) != 32
    ):
        return PipelineBusinessControlRequestIdentityStatus.CONFLICT
    if (
        observed_transaction_id == plan.closure.request_transaction_id
        and hmac.compare_digest(
            observed_request_sha256,
            plan.closure.request_sha256,
        )
    ):
        return PipelineBusinessControlRequestIdentityStatus.EXACT_RETRY
    return PipelineBusinessControlRequestIdentityStatus.CONFLICT


def _classify_worker_request_identity(
    plan: WorkerExecutionPlan,
    observed_transaction_id: Any,
    observed_request_sha256: Any,
) -> WorkerRequestIdentityStatus:
    """Classify only request identity; this is not a durable outcome receipt.

    An absent pair permits the future writer to attempt its CAS.  The same
    transaction id and same canonical digest is an exact retry.  Any partial,
    differently typed, differently identified, or differently digested pair
    is a conflict and must never apply another transition under that id.
    """

    plan = _require_worker_execution_plan(plan)
    if observed_transaction_id is None and observed_request_sha256 is None:
        return WorkerRequestIdentityStatus.ABSENT
    if (
        type(observed_transaction_id) is not str
        or type(observed_request_sha256) is not bytes
        or len(observed_request_sha256) != 32
    ):
        return WorkerRequestIdentityStatus.CONFLICT
    if (
        observed_transaction_id == plan.closure.request_transaction_id
        and hmac.compare_digest(
            observed_request_sha256,
            plan.closure.request_sha256,
        )
    ):
        return WorkerRequestIdentityStatus.EXACT_RETRY
    return WorkerRequestIdentityStatus.CONFLICT


__all__ = ()
