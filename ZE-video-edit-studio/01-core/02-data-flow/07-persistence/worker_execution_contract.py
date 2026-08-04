"""Pure private contracts for one durable worker execution transaction.

These frozen carriers describe scheduler facts only.  They do not open a
database, inspect a Document, choose the current time, invoke a provider,
acquire a lease, or claim that any transition committed.  The private
integrity issuer stamps a complete content closure, not a one-shot transaction
identity; a future durable writer must still classify the transaction id and
request digest against its ledger before applying the plan.

The small pipeline business-control carrier at the end is intentionally not a
worker request.  It can describe only zero-attempt creation or a
pause/resume-eligibility/cancel control CAS; it has a different protocol,
operation enum and integrity issuer, so a Document commit cannot fabricate
claim/effect/terminal worker facts.  This control projection is an unmounted
V8 design value; the frozen Atomic writer does not consume it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from business_transaction_contract import (
    AuthorityBinding,
    BusinessTransactionContractError,
    FrozenJsonPayload,
    MAX_BUSINESS_ID_BYTES,
    MAX_PROJECT_ID_BYTES,
    MAX_PROJECT_ID_CHARS,
    MAX_RETRY_ATTEMPTS,
    MAX_SQLITE_INTEGER,
    verify_frozen_json_payload,
)


WORKER_EXECUTION_PROTOCOL = "ze.worker-execution.v3"
WORKER_EXECUTION_MANIFEST_SCHEMA = "ze.worker-execution.manifest.v3"
WORKER_EXECUTION_STATUS_PREPARED = "prepared"
WORKER_EXECUTION_REQUEST_CLASS = "worker_execution"
WORKER_EXECUTION_TRANSACTION_PREFIX = "_worker-execution:"
WORKER_EXECUTION_TRANSACTION_KIND = "_worker_execution_v3"
PIPELINE_BUSINESS_CONTROL_PROTOCOL = "ze.pipeline-business-control.v2"
PIPELINE_BUSINESS_CONTROL_MANIFEST_SCHEMA = (
    "ze.pipeline-business-control.manifest.v2"
)
PIPELINE_BUSINESS_CONTROL_STATUS_PREPARED = "prepared"
PIPELINE_BUSINESS_CONTROL_REQUEST_CLASS = "pipeline_business_control"
PIPELINE_BUSINESS_CONTROL_TRANSACTION_PREFIX = "_pipeline-business-control:"
PIPELINE_BUSINESS_CONTROL_TRANSACTION_KIND = "_pipeline_business_control_v2"
MAX_WORKER_TEXT_BYTES = 1024 * 1024
# ``pipeline/<project>/<generation>/<pipeline>`` is an exact, derived
# idempotency identity.  Each source component is already independently
# bounded, so the derived field needs the sum of those budgets rather than the
# ordinary one-identifier budget.  19 is the decimal width of MAX_SQLITE_INTEGER.
MAX_PIPELINE_IDEMPOTENCY_SCOPE_BYTES = (
    len("pipeline/".encode("ascii"))
    + MAX_PROJECT_ID_BYTES
    + len("/".encode("ascii"))
    + len(str(MAX_SQLITE_INTEGER).encode("ascii"))
    + len("/".encode("ascii"))
    + MAX_BUSINESS_ID_BYTES
)

EMPTY_WORKER_LEDGER_TAIL_SHA256 = b"\0" * 32

_LOWER_HEX = frozenset("0123456789abcdef")


class WorkerExecutionContractError(ValueError):
    """Stable, payload-free worker contract failure."""

    __slots__ = ("code", "path")

    def __init__(self, code: str, path: str) -> None:
        if type(code) is not str or not code:
            raise TypeError("code must be a non-empty exact string")
        if type(path) is not str or not path:
            raise TypeError("path must be a non-empty exact string")
        self.code = code
        self.path = path
        super().__init__(code)


def _fail(code: str, path: str) -> None:
    raise WorkerExecutionContractError(code, path)


def _text(
    value: Any,
    path: str,
    *,
    nonempty: bool = True,
    maximum: int = MAX_WORKER_TEXT_BYTES,
) -> str:
    if type(value) is not str or (nonempty and not value):
        _fail("worker_execution_text_invalid", path)
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeEncodeError:
        _fail("worker_execution_text_invalid", path)
    if len(encoded) > maximum:
        _fail("worker_execution_text_budget_exceeded", path)
    return value


def _identifier(value: Any, path: str) -> str:
    return _text(value, path, maximum=MAX_BUSINESS_ID_BYTES)


def _idempotency_scope(value: Any, path: str, *, pipeline: bool) -> str:
    return _text(
        value,
        path,
        maximum=(
            MAX_PIPELINE_IDEMPOTENCY_SCOPE_BYTES
            if pipeline
            else MAX_BUSINESS_ID_BYTES
        ),
    )


def _project_id(value: Any, path: str) -> str:
    value = _text(value, path, maximum=MAX_PROJECT_ID_BYTES)
    if len(value) > MAX_PROJECT_ID_CHARS:
        _fail("worker_execution_project_id_invalid", path)
    return value


def _integer(
    value: Any,
    path: str,
    *,
    minimum: int = 0,
    maximum: int = MAX_SQLITE_INTEGER,
) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _fail("worker_execution_integer_invalid", path)
    return value


def _lower_hex(value: Any, path: str, length: int) -> str:
    if (
        type(value) is not str
        or len(value) != length
        or any(character not in _LOWER_HEX for character in value)
    ):
        _fail("worker_execution_hex_invalid", path)
    return value


def _digest(value: Any, path: str) -> bytes:
    if type(value) is not bytes or len(value) != 32:
        _fail("worker_execution_digest_invalid", path)
    return value


def _timestamp(value: Any, path: str) -> str:
    if type(value) is not str or len(value) != 24:
        _fail("worker_execution_timestamp_invalid", path)
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except (TypeError, ValueError):
        _fail("worker_execution_timestamp_invalid", path)
    if parsed.strftime("%Y-%m-%dT%H:%M:%S.%fZ")[:-4] + "Z" != value:
        _fail("worker_execution_timestamp_invalid", path)
    return value


def _payload(value: Any, path: str) -> FrozenJsonPayload:
    try:
        return verify_frozen_json_payload(value)
    except BusinessTransactionContractError:
        _fail("worker_execution_payload_invalid", path)


class WorkerTargetKind(str, Enum):
    PIPELINE = "pipeline"
    ACTION = "action"
    OUTBOX = "outbox"


class WorkerReplayPolicy(str, Enum):
    EXACT_CAS_RETRY = "exact_cas_retry"
    RELOAD_AND_REPLAN = "reload_and_replan"
    PROVIDER_IDEMPOTENT = "provider_idempotent"
    RECONCILE_BEFORE_RETRY = "reconcile_before_retry"
    NEVER_REPLAY = "never_replay"


class WorkerIdempotencyPolicy(str, Enum):
    REQUIRED_EXTERNAL_KEY = "required_external_key"
    DURABLE_RECEIPT_RECONCILE = "durable_receipt_reconcile"
    SINGLE_ATTEMPT = "single_attempt"


class WorkerExecutionStatus(str, Enum):
    PENDING = "pending"
    QUEUED = "queued"
    RETRY = "retry"
    RUNNING = "running"
    LEASED = "leased"
    PAUSED = "paused"
    CANCEL_REQUESTED = "cancel_requested"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    FAILED_TERMINAL = "failed_terminal"
    CANCELLED = "cancelled"
    PUBLISHED = "published"
    DEAD = "dead"
    OUTCOME_UNKNOWN = "outcome_unknown"
    REPAIR_REQUIRED = "repair_required"


class WorkerExecutionOperation(str, Enum):
    CLAIM = "claim"
    ACKNOWLEDGE_CANCEL = "acknowledge_cancel"
    RENEW = "renew"
    AUTHORIZE_EFFECT = "authorize_effect"
    COMPLETE = "complete"
    EXPIRE = "expire"
    RECONCILE = "reconcile"


class PipelineBusinessControlOperation(str, Enum):
    CREATE = "create"
    APPLY_CONTROL_INTENT = "apply_control_intent"


class WorkerEffectKind(str, Enum):
    PIPELINE_DOCUMENT_COMMIT = "pipeline_document_commit"
    ACTION_PROVIDER = "action_provider"
    OUTBOX_PUBLISH = "outbox_publish"


class WorkerReconcileProofKind(str, Enum):
    RESULT_COMMIT = "result_commit"
    PROVIDER_RECEIPT = "provider_receipt"
    DURABLE_RESULT = "durable_result"
    PROVEN_NOT_EXECUTED = "proven_not_executed"
    PROVIDER_ABSENCE = "provider_absence"
    TERMINAL_FAILURE = "terminal_failure"
    STILL_UNKNOWN = "still_unknown"


class WorkerTerminalProofKind(str, Enum):
    TERMINAL_FAILURE = "terminal_failure"
    CANCELLED = "cancelled"
    PROVEN_NOT_EXECUTED = "proven_not_executed"


class WorkerRequestIdentityStatus(str, Enum):
    """Deprecated identity-only view retained for private compatibility."""

    ABSENT = "absent"
    EXACT_RETRY = "exact_retry"
    CONFLICT = "conflict"


class PipelineBusinessControlRequestIdentityStatus(str, Enum):
    """Deprecated identity-only view retained for private compatibility."""

    ABSENT = "absent"
    EXACT_RETRY = "exact_retry"
    CONFLICT = "conflict"


class WorkerPersistedReadStatus(str, Enum):
    COMPLETE = "complete"
    UNKNOWN = "unknown"


class WorkerReceiptReconcileStatus(str, Enum):
    ABSENT = "absent"
    EXACT = "exact"
    CONFLICT = "conflict"
    CORRUPT = "corrupt"
    UNKNOWN = "unknown"


class WorkerReceiptRetryAction(str, Enum):
    APPLY_EXACT = "apply_exact"
    RETURN_EXACT = "return_exact"
    STOP = "stop"
    RECONCILE_REQUIRED = "reconcile_required"


_TARGET_REPLAY_POLICIES = {
    WorkerTargetKind.PIPELINE: frozenset((
        WorkerReplayPolicy.EXACT_CAS_RETRY,
        WorkerReplayPolicy.RELOAD_AND_REPLAN,
        WorkerReplayPolicy.NEVER_REPLAY,
    )),
    WorkerTargetKind.ACTION: frozenset((
        WorkerReplayPolicy.PROVIDER_IDEMPOTENT,
        WorkerReplayPolicy.RECONCILE_BEFORE_RETRY,
        WorkerReplayPolicy.NEVER_REPLAY,
    )),
    WorkerTargetKind.OUTBOX: frozenset((
        WorkerReplayPolicy.PROVIDER_IDEMPOTENT,
        WorkerReplayPolicy.RECONCILE_BEFORE_RETRY,
        WorkerReplayPolicy.NEVER_REPLAY,
    )),
}

_TARGET_POLICY_PAIRS = {
    WorkerTargetKind.PIPELINE: frozenset((
        (
            WorkerReplayPolicy.EXACT_CAS_RETRY,
            WorkerIdempotencyPolicy.DURABLE_RECEIPT_RECONCILE,
        ),
        (
            WorkerReplayPolicy.RELOAD_AND_REPLAN,
            WorkerIdempotencyPolicy.DURABLE_RECEIPT_RECONCILE,
        ),
        (
            WorkerReplayPolicy.NEVER_REPLAY,
            WorkerIdempotencyPolicy.SINGLE_ATTEMPT,
        ),
    )),
    WorkerTargetKind.ACTION: frozenset((
        (
            WorkerReplayPolicy.PROVIDER_IDEMPOTENT,
            WorkerIdempotencyPolicy.REQUIRED_EXTERNAL_KEY,
        ),
        (
            WorkerReplayPolicy.RECONCILE_BEFORE_RETRY,
            WorkerIdempotencyPolicy.DURABLE_RECEIPT_RECONCILE,
        ),
        (
            WorkerReplayPolicy.NEVER_REPLAY,
            WorkerIdempotencyPolicy.SINGLE_ATTEMPT,
        ),
    )),
    WorkerTargetKind.OUTBOX: frozenset((
        (
            WorkerReplayPolicy.PROVIDER_IDEMPOTENT,
            WorkerIdempotencyPolicy.REQUIRED_EXTERNAL_KEY,
        ),
        (
            WorkerReplayPolicy.RECONCILE_BEFORE_RETRY,
            WorkerIdempotencyPolicy.DURABLE_RECEIPT_RECONCILE,
        ),
        (
            WorkerReplayPolicy.NEVER_REPLAY,
            WorkerIdempotencyPolicy.SINGLE_ATTEMPT,
        ),
    )),
}


@dataclass(frozen=True)
class WorkerOutboxOrder:
    """Immutable position of one event in the repository outbox stream."""

    sequence: int
    repository_revision: int
    ordinal: int

    def __post_init__(self) -> None:
        _integer(self.sequence, "$.target.outbox_order.sequence", minimum=1)
        _integer(
            self.repository_revision,
            "$.target.outbox_order.repository_revision",
            minimum=1,
        )
        _integer(
            self.ordinal,
            "$.target.outbox_order.ordinal",
            maximum=9999,
        )


@dataclass(frozen=True)
class WorkerOutboxDispatchCursor:
    """The last terminally published/dead position, or the empty zero cursor."""

    sequence: int
    repository_revision: int
    ordinal: int

    def __post_init__(self) -> None:
        _integer(self.sequence, "$.dispatch.cursor.sequence")
        _integer(
            self.repository_revision,
            "$.dispatch.cursor.repository_revision",
        )
        _integer(
            self.ordinal,
            "$.dispatch.cursor.ordinal",
            minimum=-1,
            maximum=9999,
        )
        empty = (
            self.sequence == 0
            and self.repository_revision == 0
            and self.ordinal == -1
        )
        durable = (
            self.sequence >= 1
            and self.repository_revision >= 1
            and self.ordinal >= 0
        )
        if not (empty or durable):
            _fail("worker_execution_dispatch_fence_invalid", "$.dispatch.cursor")


def _outbox_direct_successor(
    published: WorkerOutboxDispatchCursor,
    candidate: WorkerOutboxOrder,
) -> bool:
    if candidate.sequence != published.sequence + 1:
        return False
    if published.sequence == 0:
        return candidate.ordinal == 0
    return bool(
        (
            candidate.repository_revision == published.repository_revision
            and candidate.ordinal == published.ordinal + 1
        )
        or (
            candidate.repository_revision > published.repository_revision
            and candidate.ordinal == 0
        )
    )


@dataclass(frozen=True)
class WorkerOutboxDispatchState:
    """One exact dispatch-control image with published and inflight split."""

    control_revision: int
    published: WorkerOutboxDispatchCursor
    inflight: Optional[WorkerOutboxOrder]

    def __post_init__(self) -> None:
        _integer(
            self.control_revision,
            "$.dispatch.control_revision",
            minimum=1,
        )
        if type(self.published) is not WorkerOutboxDispatchCursor:
            _fail("worker_execution_dispatch_fence_invalid", "$.dispatch.published")
        if self.inflight is not None:
            if type(self.inflight) is not WorkerOutboxOrder or not (
                _outbox_direct_successor(self.published, self.inflight)
            ):
                _fail(
                    "worker_execution_dispatch_fence_invalid",
                    "$.dispatch.inflight",
                )


@dataclass(frozen=True)
class WorkerOutboxDispatchFence:
    """Exact published/inflight transition for one outbox worker operation.

    A first claim occupies ``inflight`` without advancing ``published``.  A
    retry may preserve that exact inflight image.  Only a proven PUBLISHED or
    strong DEAD completion/reconcile may advance ``published`` and clear
    ``inflight``.  Across repository revisions these values still cannot prove
    absence of an intervening event: the fixed writer must use the same snapshot
    as its CAS to query the first event strictly after the published pair and
    require that row to equal the target.
    """

    before: WorkerOutboxDispatchState
    after: WorkerOutboxDispatchState

    def __post_init__(self) -> None:
        if (
            type(self.before) is not WorkerOutboxDispatchState
            or type(self.after) is not WorkerOutboxDispatchState
        ):
            _fail("worker_execution_dispatch_fence_invalid", "$.dispatch_fence")


@dataclass(frozen=True)
class WorkerTarget:
    target_kind: WorkerTargetKind
    target_id: str
    project_id: str
    generation: int
    document_id: str
    origin_commit_id: str
    payload: FrozenJsonPayload = field(repr=False)
    replay_policy: WorkerReplayPolicy
    idempotency_policy: WorkerIdempotencyPolicy
    idempotency_scope: str
    idempotency_key: str
    max_attempts: int
    outbox_order: Optional[WorkerOutboxOrder] = None
    pipeline_id: Optional[str] = None
    intent_type: Optional[str] = None

    def __post_init__(self) -> None:
        if type(self.target_kind) is not WorkerTargetKind:
            _fail("worker_execution_target_kind_invalid", "$.target.kind")
        _lower_hex(self.target_id, "$.target.id", 32)
        _project_id(self.project_id, "$.target.project_id")
        _integer(self.generation, "$.target.generation", minimum=1)
        _identifier(self.document_id, "$.target.document_id")
        _lower_hex(self.origin_commit_id, "$.target.origin_commit_id", 32)
        _payload(self.payload, "$.target.payload")
        if (
            type(self.replay_policy) is not WorkerReplayPolicy
            or self.replay_policy not in _TARGET_REPLAY_POLICIES[self.target_kind]
        ):
            _fail("worker_execution_replay_policy_invalid", "$.target.replay_policy")
        if type(self.idempotency_policy) is not WorkerIdempotencyPolicy:
            _fail(
                "worker_execution_idempotency_policy_invalid",
                "$.target.idempotency_policy",
            )
        if (
            self.replay_policy,
            self.idempotency_policy,
        ) not in _TARGET_POLICY_PAIRS[self.target_kind]:
            _fail(
                "worker_execution_policy_pair_invalid",
                "$.target.idempotency_policy",
            )
        _idempotency_scope(
            self.idempotency_scope,
            "$.target.idempotency_scope",
            pipeline=self.target_kind is WorkerTargetKind.PIPELINE,
        )
        _identifier(self.idempotency_key, "$.target.idempotency_key")
        _integer(
            self.max_attempts,
            "$.target.max_attempts",
            minimum=1,
            maximum=MAX_RETRY_ATTEMPTS,
        )
        if (
            self.replay_policy is WorkerReplayPolicy.NEVER_REPLAY
            or self.idempotency_policy is WorkerIdempotencyPolicy.SINGLE_ATTEMPT
        ) and self.max_attempts != 1:
            _fail("worker_execution_retry_policy_inconsistent", "$.target.max_attempts")
        if self.target_kind is WorkerTargetKind.OUTBOX:
            if type(self.outbox_order) is not WorkerOutboxOrder:
                _fail("worker_execution_outbox_order_required", "$.target.outbox_order")
        elif self.outbox_order is not None:
            _fail("worker_execution_outbox_order_invalid", "$.target.outbox_order")
        if self.target_kind is WorkerTargetKind.PIPELINE:
            if self.pipeline_id is None or self.intent_type is None:
                _fail("worker_execution_pipeline_identity_invalid", "$.target")
            _identifier(self.pipeline_id, "$.target.pipeline_id")
            _identifier(self.intent_type, "$.target.intent_type")
            expected_scope = "pipeline/%s/%d/%s" % (
                self.project_id,
                self.generation,
                self.pipeline_id,
            )
            expected_policy = (
                WorkerIdempotencyPolicy.SINGLE_ATTEMPT
                if self.replay_policy is WorkerReplayPolicy.NEVER_REPLAY
                else WorkerIdempotencyPolicy.DURABLE_RECEIPT_RECONCILE
            )
            if (
                self.idempotency_scope != expected_scope
                or self.idempotency_policy is not expected_policy
            ):
                _fail("worker_execution_pipeline_identity_invalid", "$.target")
        elif self.pipeline_id is not None or self.intent_type is not None:
            _fail("worker_execution_pipeline_identity_invalid", "$.target")


@dataclass(frozen=True)
class WorkerEffectBinding:
    attempt_id: str
    attempt_number: int
    effect_id: str
    effect_kind: WorkerEffectKind
    request_sha256: bytes = field(repr=False)
    idempotency_scope: str = ""
    idempotency_key: str = ""

    def __post_init__(self) -> None:
        _lower_hex(self.attempt_id, "$.effect_binding.attempt_id", 32)
        _integer(
            self.attempt_number,
            "$.effect_binding.attempt_number",
            minimum=1,
            maximum=MAX_RETRY_ATTEMPTS,
        )
        _lower_hex(self.effect_id, "$.effect_binding.effect_id", 32)
        if type(self.effect_kind) is not WorkerEffectKind:
            _fail("worker_execution_effect_kind_invalid", "$.effect_binding.kind")
        _digest(self.request_sha256, "$.effect_binding.request_sha256")
        _idempotency_scope(
            self.idempotency_scope,
            "$.effect_binding.idempotency_scope",
            pipeline=(
                self.effect_kind is WorkerEffectKind.PIPELINE_DOCUMENT_COMMIT
            ),
        )
        _identifier(self.idempotency_key, "$.effect_binding.idempotency_key")


@dataclass(frozen=True)
class WorkerBeforeState:
    status: WorkerExecutionStatus
    state_revision: int
    attempt_count: int
    last_attempt_id: Optional[str]
    last_attempt_number: Optional[int]
    current_attempt_id: Optional[str]
    current_attempt_number: Optional[int]
    lease_owner: Optional[str]
    lease_fence: Optional[int]
    lease_expires_at: Optional[str]
    fence_high_water: int
    current_effect: Optional[WorkerEffectBinding] = None
    unresolved_effect: Optional[WorkerEffectBinding] = None
    next_attempt_at: Optional[str] = None
    cancellation_pending: bool = False

    def __post_init__(self) -> None:
        if type(self.status) is not WorkerExecutionStatus:
            _fail("worker_execution_status_invalid", "$.before.status")
        _integer(self.state_revision, "$.before.state_revision", minimum=1)
        _integer(
            self.attempt_count,
            "$.before.attempt_count",
            maximum=MAX_RETRY_ATTEMPTS,
        )
        _integer(self.fence_high_water, "$.before.fence_high_water")
        if self.last_attempt_id is not None:
            _lower_hex(self.last_attempt_id, "$.before.last_attempt_id", 32)
        if self.last_attempt_number is not None:
            _integer(
                self.last_attempt_number,
                "$.before.last_attempt_number",
                minimum=1,
                maximum=MAX_RETRY_ATTEMPTS,
            )
        if self.current_attempt_id is not None:
            _lower_hex(self.current_attempt_id, "$.before.current_attempt_id", 32)
        if self.current_attempt_number is not None:
            _integer(
                self.current_attempt_number,
                "$.before.current_attempt_number",
                minimum=1,
                maximum=MAX_RETRY_ATTEMPTS,
            )
        if self.lease_owner is not None:
            _identifier(self.lease_owner, "$.before.lease_owner")
        if self.lease_fence is not None:
            _integer(self.lease_fence, "$.before.lease_fence", minimum=1)
        if self.lease_expires_at is not None:
            _timestamp(self.lease_expires_at, "$.before.lease_expires_at")
        if self.current_effect is not None and type(self.current_effect) is not WorkerEffectBinding:
            _fail("worker_execution_effect_pointer_invalid", "$.before.current_effect")
        if (
            self.unresolved_effect is not None
            and type(self.unresolved_effect) is not WorkerEffectBinding
        ):
            _fail("worker_execution_effect_pointer_invalid", "$.before.unresolved_effect")
        if self.next_attempt_at is not None:
            _timestamp(self.next_attempt_at, "$.before.next_attempt_at")
        if self.status is WorkerExecutionStatus.RETRY:
            if self.next_attempt_at is None:
                _fail("worker_execution_retry_invalid", "$.before.next_attempt_at")
        elif self.next_attempt_at is not None:
            _fail("worker_execution_retry_invalid", "$.before.next_attempt_at")
        if type(self.cancellation_pending) is not bool:
            _fail(
                "worker_execution_cancellation_invalid",
                "$.before.cancellation_pending",
            )
        if self.status is WorkerExecutionStatus.CANCEL_REQUESTED:
            if not self.cancellation_pending:
                _fail(
                    "worker_execution_cancellation_invalid",
                    "$.before.cancellation_pending",
                )
        elif self.cancellation_pending and self.status not in (
            WorkerExecutionStatus.OUTCOME_UNKNOWN,
            WorkerExecutionStatus.REPAIR_REQUIRED,
        ):
            _fail(
                "worker_execution_cancellation_invalid",
                "$.before.cancellation_pending",
            )

        current = (
            self.current_attempt_id,
            self.current_attempt_number,
            self.lease_owner,
            self.lease_fence,
            self.lease_expires_at,
        )
        if any(value is None for value in current) and any(
            value is not None for value in current
        ):
            _fail("worker_execution_attempt_pointer_invalid", "$.before")
        if self.current_attempt_id is None and self.current_effect is not None:
            _fail("worker_execution_effect_pointer_invalid", "$.before")
        if self.current_attempt_id is not None and self.unresolved_effect is not None:
            _fail("worker_execution_effect_pointer_invalid", "$.before")
        if self.current_effect is not None and (
            self.current_effect.attempt_id != self.current_attempt_id
            or self.current_effect.attempt_number != self.current_attempt_number
        ):
            _fail("worker_execution_effect_pointer_invalid", "$.before")
        if self.unresolved_effect is not None and (
            self.unresolved_effect.attempt_id != self.last_attempt_id
            or self.unresolved_effect.attempt_number != self.last_attempt_number
        ):
            _fail("worker_execution_effect_pointer_invalid", "$.before")

        if self.current_attempt_id is not None:
            if (
                self.attempt_count < 1
                or self.current_attempt_number != self.attempt_count
                or self.lease_fence != self.fence_high_water
                or (
                    self.attempt_count == 1
                    and (
                        self.last_attempt_id is not None
                        or self.last_attempt_number is not None
                    )
                )
                or (
                    self.attempt_count > 1
                    and (
                        self.last_attempt_id is None
                        or self.last_attempt_number != self.attempt_count - 1
                    )
                )
            ):
                _fail("worker_execution_attempt_pointer_invalid", "$.before")
        elif (
            (self.attempt_count == 0 and (
                self.last_attempt_id is not None
                or self.last_attempt_number is not None
            ))
            or (self.attempt_count > 0 and (
                self.last_attempt_id is None
                or self.last_attempt_number != self.attempt_count
            ))
        ):
            _fail("worker_execution_attempt_pointer_invalid", "$.before")
        if self.status in (
            WorkerExecutionStatus.OUTCOME_UNKNOWN,
            WorkerExecutionStatus.REPAIR_REQUIRED,
        ):
            if self.current_attempt_id is not None or self.unresolved_effect is None:
                _fail("worker_execution_unresolved_effect_required", "$.before")
        elif self.unresolved_effect is not None:
            _fail("worker_execution_unresolved_effect_invalid", "$.before")


@dataclass(frozen=True)
class WorkerAttemptStart:
    attempt_id: str
    attempt_number: int
    previous_attempt_id: Optional[str]
    previous_attempt_number: Optional[int]
    lease_owner: str
    execution_fence: int
    started_at: str
    lease_expires_at: str

    def __post_init__(self) -> None:
        _lower_hex(self.attempt_id, "$.attempt.id", 32)
        _integer(
            self.attempt_number,
            "$.attempt.number",
            minimum=1,
            maximum=MAX_RETRY_ATTEMPTS,
        )
        if self.previous_attempt_id is not None:
            _lower_hex(self.previous_attempt_id, "$.attempt.previous_id", 32)
        if self.previous_attempt_number is not None:
            _integer(
                self.previous_attempt_number,
                "$.attempt.previous_number",
                minimum=1,
                maximum=MAX_RETRY_ATTEMPTS - 1,
            )
        if (self.previous_attempt_id is None) != (
            self.previous_attempt_number is None
        ):
            _fail("worker_execution_attempt_chain_invalid", "$.attempt")
        if (
            (self.attempt_number == 1 and self.previous_attempt_id is not None)
            or (
                self.attempt_number > 1
                and (
                    self.previous_attempt_id is None
                    or self.previous_attempt_id == self.attempt_id
                    or self.previous_attempt_number != self.attempt_number - 1
                )
            )
        ):
            _fail("worker_execution_attempt_chain_invalid", "$.attempt")
        _identifier(self.lease_owner, "$.attempt.lease_owner")
        _integer(self.execution_fence, "$.attempt.execution_fence", minimum=1)
        _timestamp(self.started_at, "$.attempt.started_at")
        _timestamp(self.lease_expires_at, "$.attempt.lease_expires_at")
        if self.lease_expires_at <= self.started_at:
            _fail("worker_execution_lease_invalid", "$.attempt.lease_expires_at")


@dataclass(frozen=True)
class WorkerLeaseToken:
    attempt_id: str
    attempt_number: int
    lease_owner: str
    execution_fence: int
    lease_expires_at: str

    def __post_init__(self) -> None:
        _lower_hex(self.attempt_id, "$.lease.attempt_id", 32)
        _integer(
            self.attempt_number,
            "$.lease.attempt_number",
            minimum=1,
            maximum=MAX_RETRY_ATTEMPTS,
        )
        _identifier(self.lease_owner, "$.lease.owner")
        _integer(self.execution_fence, "$.lease.fence", minimum=1)
        _timestamp(self.lease_expires_at, "$.lease.expires_at")


@dataclass(frozen=True)
class WorkerEffectAuthorization:
    effect_id: str
    effect_kind: WorkerEffectKind
    idempotency_scope: str
    idempotency_key: str
    request: FrozenJsonPayload = field(repr=False)
    authorized_at: str = ""

    def __post_init__(self) -> None:
        _lower_hex(self.effect_id, "$.effect.id", 32)
        if type(self.effect_kind) is not WorkerEffectKind:
            _fail("worker_execution_effect_kind_invalid", "$.effect.kind")
        _idempotency_scope(
            self.idempotency_scope,
            "$.effect.idempotency_scope",
            pipeline=(
                self.effect_kind is WorkerEffectKind.PIPELINE_DOCUMENT_COMMIT
            ),
        )
        _identifier(self.idempotency_key, "$.effect.idempotency_key")
        _payload(self.request, "$.effect.request")
        _timestamp(self.authorized_at, "$.effect.authorized_at")


@dataclass(frozen=True)
class WorkerEffectLink:
    attempt_id: str
    attempt_number: int
    effect_id: str
    effect_kind: WorkerEffectKind
    request_sha256: bytes = field(repr=False)
    result_commit_id: Optional[str] = None
    provider_receipt: Optional[FrozenJsonPayload] = field(default=None, repr=False)

    def __post_init__(self) -> None:
        _lower_hex(self.attempt_id, "$.effect_link.attempt_id", 32)
        _integer(
            self.attempt_number,
            "$.effect_link.attempt_number",
            minimum=1,
            maximum=MAX_RETRY_ATTEMPTS,
        )
        _lower_hex(self.effect_id, "$.effect_link.id", 32)
        if type(self.effect_kind) is not WorkerEffectKind:
            _fail("worker_execution_effect_kind_invalid", "$.effect_link.kind")
        _digest(self.request_sha256, "$.effect_link.request_sha256")
        if self.result_commit_id is not None:
            _lower_hex(self.result_commit_id, "$.effect_link.result_commit_id", 32)
        if self.provider_receipt is not None:
            _payload(self.provider_receipt, "$.effect_link.provider_receipt")
        if self.result_commit_id is None and self.provider_receipt is None:
            _fail("worker_execution_effect_link_empty", "$.effect_link")

    def binding(self, *, idempotency_scope: str, idempotency_key: str) -> WorkerEffectBinding:
        return WorkerEffectBinding(
            attempt_id=self.attempt_id,
            attempt_number=self.attempt_number,
            effect_id=self.effect_id,
            effect_kind=self.effect_kind,
            request_sha256=self.request_sha256,
            idempotency_scope=idempotency_scope,
            idempotency_key=idempotency_key,
        )


@dataclass(frozen=True)
class WorkerDurableResultProof:
    result_id: str
    result: FrozenJsonPayload = field(repr=False)
    recorded_at: str = ""
    effect: Optional[WorkerEffectBinding] = None

    def __post_init__(self) -> None:
        _lower_hex(self.result_id, "$.durable_result.id", 32)
        _payload(self.result, "$.durable_result.result")
        _timestamp(self.recorded_at, "$.durable_result.recorded_at")
        if self.effect is not None and type(self.effect) is not WorkerEffectBinding:
            _fail("worker_execution_effect_pointer_invalid", "$.durable_result.effect")


@dataclass(frozen=True)
class WorkerReconcileProof:
    proof_kind: WorkerReconcileProofKind
    unresolved_effect: WorkerEffectBinding
    evidence: FrozenJsonPayload = field(repr=False)
    effect_link: Optional[WorkerEffectLink] = None
    durable_result: Optional[WorkerDurableResultProof] = None

    def __post_init__(self) -> None:
        if type(self.proof_kind) is not WorkerReconcileProofKind:
            _fail("worker_execution_reconcile_proof_kind_invalid", "$.proof.kind")
        if type(self.unresolved_effect) is not WorkerEffectBinding:
            _fail("worker_execution_effect_pointer_invalid", "$.proof.unresolved_effect")
        _payload(self.evidence, "$.proof.evidence")
        if self.effect_link is not None and type(self.effect_link) is not WorkerEffectLink:
            _fail("worker_execution_effect_link_invalid", "$.proof.effect_link")
        if (
            self.durable_result is not None
            and type(self.durable_result) is not WorkerDurableResultProof
        ):
            _fail("worker_execution_durable_result_invalid", "$.proof.durable_result")
        if self.proof_kind in (
            WorkerReconcileProofKind.RESULT_COMMIT,
            WorkerReconcileProofKind.PROVIDER_RECEIPT,
        ):
            if self.effect_link is None or self.durable_result is not None:
                _fail("worker_execution_reconcile_proof_invalid", "$.proof")
        elif self.proof_kind is WorkerReconcileProofKind.DURABLE_RESULT:
            if self.durable_result is None or self.effect_link is not None:
                _fail("worker_execution_reconcile_proof_invalid", "$.proof")
        elif self.effect_link is not None or self.durable_result is not None:
            _fail("worker_execution_reconcile_proof_invalid", "$.proof")


@dataclass(frozen=True)
class WorkerTerminalProof:
    proof_kind: WorkerTerminalProofKind
    effect: WorkerEffectBinding
    evidence: FrozenJsonPayload = field(repr=False)
    effect_link: Optional[WorkerEffectLink] = None

    def __post_init__(self) -> None:
        if type(self.proof_kind) is not WorkerTerminalProofKind:
            _fail("worker_execution_terminal_proof_kind_invalid", "$.terminal_proof.kind")
        if type(self.effect) is not WorkerEffectBinding:
            _fail("worker_execution_effect_pointer_invalid", "$.terminal_proof.effect")
        _payload(self.evidence, "$.terminal_proof.evidence")
        if self.effect_link is not None and type(self.effect_link) is not WorkerEffectLink:
            _fail("worker_execution_effect_link_invalid", "$.terminal_proof.effect_link")


@dataclass(frozen=True)
class WorkerAfterState:
    status: WorkerExecutionStatus
    state_revision: int
    attempt_count: int
    last_attempt_id: Optional[str]
    last_attempt_number: Optional[int]
    current_attempt_id: Optional[str]
    current_attempt_number: Optional[int]
    lease_owner: Optional[str]
    lease_fence: Optional[int]
    lease_expires_at: Optional[str]
    fence_high_water: int
    current_effect: Optional[WorkerEffectBinding] = None
    unresolved_effect: Optional[WorkerEffectBinding] = None
    next_attempt_at: Optional[str] = None
    cancellation_pending: bool = False
    result_effect_link: Optional[WorkerEffectLink] = None
    durable_result: Optional[WorkerDurableResultProof] = None

    def __post_init__(self) -> None:
        # Reuse the exact pointer/tail/effect validation on the after image.
        WorkerBeforeState(
            status=self.status,
            state_revision=self.state_revision,
            attempt_count=self.attempt_count,
            last_attempt_id=self.last_attempt_id,
            last_attempt_number=self.last_attempt_number,
            current_attempt_id=self.current_attempt_id,
            current_attempt_number=self.current_attempt_number,
            lease_owner=self.lease_owner,
            lease_fence=self.lease_fence,
            lease_expires_at=self.lease_expires_at,
            fence_high_water=self.fence_high_water,
            current_effect=self.current_effect,
            unresolved_effect=self.unresolved_effect,
            next_attempt_at=self.next_attempt_at,
            cancellation_pending=self.cancellation_pending,
        )
        if self.next_attempt_at is not None:
            _timestamp(self.next_attempt_at, "$.after.next_attempt_at")
        if self.status is WorkerExecutionStatus.RETRY:
            if self.next_attempt_at is None:
                _fail("worker_execution_retry_invalid", "$.after.next_attempt_at")
        elif self.next_attempt_at is not None:
            _fail("worker_execution_retry_invalid", "$.after.next_attempt_at")
        if (
            self.result_effect_link is not None
            and type(self.result_effect_link) is not WorkerEffectLink
        ):
            _fail("worker_execution_effect_link_invalid", "$.after.result_effect_link")
        if self.durable_result is not None and type(
            self.durable_result
        ) is not WorkerDurableResultProof:
            _fail("worker_execution_durable_result_invalid", "$.after.durable_result")
        if self.current_attempt_id is not None and (
            self.result_effect_link is not None or self.durable_result is not None
        ):
            _fail("worker_execution_terminal_result_invalid", "$.after")


@dataclass(frozen=True)
class WorkerRequestHeader:
    request_transaction_id: str
    authority: AuthorityBinding
    target: WorkerTarget
    before: WorkerBeforeState
    occurred_at: str

    def __post_init__(self) -> None:
        _lower_hex(
            self.request_transaction_id,
            "$.request_transaction_id",
            32,
        )
        if type(self.authority) is not AuthorityBinding:
            _fail("worker_execution_authority_invalid", "$.authority")
        if type(self.target) is not WorkerTarget:
            _fail("worker_execution_target_invalid", "$.target")
        if type(self.before) is not WorkerBeforeState:
            _fail("worker_execution_before_invalid", "$.before")
        _timestamp(self.occurred_at, "$.occurred_at")


@dataclass(frozen=True)
class WorkerClaimRequest:
    header: WorkerRequestHeader
    attempt: WorkerAttemptStart
    dispatch_fence: Optional[WorkerOutboxDispatchFence] = None

    def __post_init__(self) -> None:
        if type(self.header) is not WorkerRequestHeader:
            _fail("worker_execution_header_invalid", "$.header")
        if type(self.attempt) is not WorkerAttemptStart:
            _fail("worker_execution_attempt_invalid", "$.attempt")
        if (
            self.dispatch_fence is not None
            and type(self.dispatch_fence) is not WorkerOutboxDispatchFence
        ):
            _fail("worker_execution_dispatch_fence_invalid", "$.dispatch_fence")


@dataclass(frozen=True)
class WorkerAcknowledgeCancelRequest:
    """Bridge a lease-free historical v6 cancellation directly to terminal."""

    header: WorkerRequestHeader
    acknowledged_at: str

    def __post_init__(self) -> None:
        if type(self.header) is not WorkerRequestHeader:
            _fail("worker_execution_header_invalid", "$.header")
        _timestamp(self.acknowledged_at, "$.acknowledged_at")


@dataclass(frozen=True)
class WorkerRenewRequest:
    header: WorkerRequestHeader
    lease: WorkerLeaseToken
    new_lease_expires_at: str

    def __post_init__(self) -> None:
        if type(self.header) is not WorkerRequestHeader:
            _fail("worker_execution_header_invalid", "$.header")
        if type(self.lease) is not WorkerLeaseToken:
            _fail("worker_execution_lease_invalid", "$.lease")
        _timestamp(self.new_lease_expires_at, "$.new_lease_expires_at")


@dataclass(frozen=True)
class WorkerAuthorizeEffectRequest:
    header: WorkerRequestHeader
    lease: WorkerLeaseToken
    effect: WorkerEffectAuthorization

    def __post_init__(self) -> None:
        if type(self.header) is not WorkerRequestHeader:
            _fail("worker_execution_header_invalid", "$.header")
        if type(self.lease) is not WorkerLeaseToken:
            _fail("worker_execution_lease_invalid", "$.lease")
        if type(self.effect) is not WorkerEffectAuthorization:
            _fail("worker_execution_effect_invalid", "$.effect")


@dataclass(frozen=True)
class WorkerCompleteRequest:
    header: WorkerRequestHeader
    lease: WorkerLeaseToken
    target_status: WorkerExecutionStatus
    finished_at: str
    result: Optional[FrozenJsonPayload] = field(default=None, repr=False)
    error: Optional[FrozenJsonPayload] = field(default=None, repr=False)
    effect_link: Optional[WorkerEffectLink] = None
    durable_result: Optional[WorkerDurableResultProof] = None
    terminal_proof: Optional[WorkerTerminalProof] = None
    next_attempt_at: Optional[str] = None
    dispatch_fence: Optional[WorkerOutboxDispatchFence] = None

    def __post_init__(self) -> None:
        if type(self.header) is not WorkerRequestHeader:
            _fail("worker_execution_header_invalid", "$.header")
        if type(self.lease) is not WorkerLeaseToken:
            _fail("worker_execution_lease_invalid", "$.lease")
        if type(self.target_status) is not WorkerExecutionStatus:
            _fail("worker_execution_status_invalid", "$.target_status")
        _timestamp(self.finished_at, "$.finished_at")
        if self.result is not None:
            _payload(self.result, "$.result")
        if self.error is not None:
            _payload(self.error, "$.error")
        if self.result is not None and self.error is not None:
            _fail("worker_execution_terminal_payload_invalid", "$.complete")
        if self.effect_link is not None and type(self.effect_link) is not WorkerEffectLink:
            _fail("worker_execution_effect_link_invalid", "$.effect_link")
        if self.durable_result is not None and type(
            self.durable_result
        ) is not WorkerDurableResultProof:
            _fail("worker_execution_durable_result_invalid", "$.durable_result")
        if self.terminal_proof is not None and type(
            self.terminal_proof
        ) is not WorkerTerminalProof:
            _fail("worker_execution_terminal_proof_invalid", "$.terminal_proof")
        if self.next_attempt_at is not None:
            _timestamp(self.next_attempt_at, "$.next_attempt_at")
        if self.dispatch_fence is not None and type(
            self.dispatch_fence
        ) is not WorkerOutboxDispatchFence:
            _fail("worker_execution_dispatch_fence_invalid", "$.dispatch_fence")


@dataclass(frozen=True)
class WorkerExpireRequest:
    header: WorkerRequestHeader
    lease: WorkerLeaseToken
    expired_at: str
    target_status: WorkerExecutionStatus
    next_attempt_at: Optional[str] = None
    dispatch_fence: Optional[WorkerOutboxDispatchFence] = None

    def __post_init__(self) -> None:
        if type(self.header) is not WorkerRequestHeader:
            _fail("worker_execution_header_invalid", "$.header")
        if type(self.lease) is not WorkerLeaseToken:
            _fail("worker_execution_lease_invalid", "$.lease")
        _timestamp(self.expired_at, "$.expired_at")
        if type(self.target_status) is not WorkerExecutionStatus:
            _fail("worker_execution_status_invalid", "$.target_status")
        if self.next_attempt_at is not None:
            _timestamp(self.next_attempt_at, "$.next_attempt_at")
        if self.dispatch_fence is not None and type(
            self.dispatch_fence
        ) is not WorkerOutboxDispatchFence:
            _fail("worker_execution_dispatch_fence_invalid", "$.dispatch_fence")


@dataclass(frozen=True)
class WorkerReconcileRequest:
    header: WorkerRequestHeader
    attempt_id: str
    attempt_number: int
    execution_fence: int
    target_status: WorkerExecutionStatus
    proof: WorkerReconcileProof
    reconciled_at: str = ""
    next_attempt_at: Optional[str] = None
    dispatch_fence: Optional[WorkerOutboxDispatchFence] = None

    def __post_init__(self) -> None:
        if type(self.header) is not WorkerRequestHeader:
            _fail("worker_execution_header_invalid", "$.header")
        _lower_hex(self.attempt_id, "$.reconcile.attempt_id", 32)
        _integer(
            self.attempt_number,
            "$.reconcile.attempt_number",
            minimum=1,
            maximum=MAX_RETRY_ATTEMPTS,
        )
        _integer(self.execution_fence, "$.reconcile.execution_fence", minimum=1)
        if type(self.target_status) is not WorkerExecutionStatus:
            _fail("worker_execution_status_invalid", "$.target_status")
        if type(self.proof) is not WorkerReconcileProof:
            _fail("worker_execution_reconcile_proof_invalid", "$.reconcile.proof")
        _timestamp(self.reconciled_at, "$.reconcile.reconciled_at")
        if self.next_attempt_at is not None:
            _timestamp(self.next_attempt_at, "$.reconcile.next_attempt_at")
        if self.dispatch_fence is not None and type(
            self.dispatch_fence
        ) is not WorkerOutboxDispatchFence:
            _fail("worker_execution_dispatch_fence_invalid", "$.dispatch_fence")


@dataclass(frozen=True)
class PipelineBusinessControlIntentBinding:
    """This commit's control intent, separate from durable job static data.

    ``WorkerTarget`` describes the durable job row a writer must compare in
    its SQL snapshot.  This value describes the new CREATE/TRANSITION intent
    being appended.  CREATE requires both projections to coincide; a later
    control intent must never rewrite the job's original payload, key or
    execution policy merely because its own retry policy differs.
    """

    origin_commit_id: str
    intent_type: str
    idempotency_key: str
    payload: FrozenJsonPayload = field(repr=False)
    replay_policy: WorkerReplayPolicy
    idempotency_policy: WorkerIdempotencyPolicy
    max_attempts: int

    def __post_init__(self) -> None:
        _lower_hex(
            self.origin_commit_id,
            "$.control.intent.origin_commit_id",
            32,
        )
        _identifier(self.intent_type, "$.control.intent.intent_type")
        _identifier(self.idempotency_key, "$.control.intent.idempotency_key")
        _payload(self.payload, "$.control.intent.payload")
        if (
            type(self.replay_policy) is not WorkerReplayPolicy
            or self.replay_policy
            not in _TARGET_REPLAY_POLICIES[WorkerTargetKind.PIPELINE]
        ):
            _fail(
                "pipeline_business_control_replay_policy_invalid",
                "$.control.intent.replay_policy",
            )
        expected_policy = (
            WorkerIdempotencyPolicy.SINGLE_ATTEMPT
            if self.replay_policy is WorkerReplayPolicy.NEVER_REPLAY
            else WorkerIdempotencyPolicy.DURABLE_RECEIPT_RECONCILE
        )
        if self.idempotency_policy is not expected_policy:
            _fail(
                "pipeline_business_control_idempotency_policy_invalid",
                "$.control.intent.idempotency_policy",
            )
        _integer(
            self.max_attempts,
            "$.control.intent.max_attempts",
            minimum=1,
            maximum=MAX_RETRY_ATTEMPTS,
        )
        if (
            self.replay_policy is WorkerReplayPolicy.NEVER_REPLAY
            and self.max_attempts != 1
        ):
            _fail(
                "pipeline_business_control_retry_policy_inconsistent",
                "$.control.intent.max_attempts",
            )


@dataclass(frozen=True)
class PipelineBusinessControlRequest:
    """Unmounted V8 business control projection, never a worker operation.

    No current Atomic writer consumes this value.  In particular,
    PAUSED -> QUEUED is a future business-owned eligibility CAS, not permission
    for a worker to claim PAUSED directly.
    """

    request_transaction_id: str
    authority: AuthorityBinding
    target: WorkerTarget
    control_intent: PipelineBusinessControlIntentBinding
    operation: PipelineBusinessControlOperation
    before: Optional[WorkerBeforeState]
    target_status: WorkerExecutionStatus
    occurred_at: str

    def __post_init__(self) -> None:
        _lower_hex(
            self.request_transaction_id,
            "$.control.request_transaction_id",
            32,
        )
        if type(self.authority) is not AuthorityBinding:
            _fail("worker_execution_authority_invalid", "$.control.authority")
        if (
            type(self.target) is not WorkerTarget
            or self.target.target_kind is not WorkerTargetKind.PIPELINE
        ):
            _fail("worker_execution_target_invalid", "$.control.target")
        if type(self.control_intent) is not PipelineBusinessControlIntentBinding:
            _fail(
                "pipeline_business_control_intent_invalid",
                "$.control.intent",
            )
        if type(self.operation) is not PipelineBusinessControlOperation:
            _fail("pipeline_business_control_operation_invalid", "$.control.operation")
        if self.before is not None and type(self.before) is not WorkerBeforeState:
            _fail("worker_execution_before_invalid", "$.control.before")
        if type(self.target_status) is not WorkerExecutionStatus:
            _fail("worker_execution_status_invalid", "$.control.target_status")
        _timestamp(self.occurred_at, "$.control.occurred_at")
        if self.operation is PipelineBusinessControlOperation.CREATE:
            if (
                self.before is not None
                or self.target_status is not WorkerExecutionStatus.QUEUED
                or self.target.origin_commit_id
                != self.control_intent.origin_commit_id
                or self.target.intent_type != self.control_intent.intent_type
                or self.target.idempotency_key
                != self.control_intent.idempotency_key
                or self.target.payload != self.control_intent.payload
                or self.target.replay_policy
                is not self.control_intent.replay_policy
                or self.target.idempotency_policy
                is not self.control_intent.idempotency_policy
                or self.target.max_attempts != self.control_intent.max_attempts
            ):
                _fail("pipeline_business_control_create_invalid", "$.control")
        elif self.before is None:
            _fail("pipeline_business_control_before_required", "$.control.before")


@dataclass(frozen=True)
class WorkerLedgerHead:
    """Exact singleton head of the shared worker/control receipt ledger."""

    sequence: int
    tail_sha256: bytes = field(repr=False)

    def __post_init__(self) -> None:
        _integer(self.sequence, "$.worker_ledger.sequence")
        tail = _digest(self.tail_sha256, "$.worker_ledger.tail_sha256")
        if (self.sequence == 0) != (
            tail == EMPTY_WORKER_LEDGER_TAIL_SHA256
        ):
            _fail("worker_ledger_head_invalid", "$.worker_ledger")


EMPTY_WORKER_LEDGER_HEAD = WorkerLedgerHead(
    0,
    EMPTY_WORKER_LEDGER_TAIL_SHA256,
)


@dataclass(frozen=True)
class WorkerLedgerMembershipEvidence:
    """Fresh-reader assertion that one old receipt remains on this head's chain.

    Hash-chain tails cannot prove forward ancestry by themselves. A durable
    reader verifies the receipt row and current head in one snapshot before
    placing this exact relationship in its capability-stamped observation.
    """

    receipt_sequence: int
    receipt_tail_sha256: bytes = field(repr=False)
    observed_head: WorkerLedgerHead

    def __post_init__(self) -> None:
        _integer(self.receipt_sequence, "$.worker_membership.sequence", minimum=1)
        tail = _digest(
            self.receipt_tail_sha256,
            "$.worker_membership.receipt_tail_sha256",
        )
        if tail == EMPTY_WORKER_LEDGER_TAIL_SHA256:
            _fail("worker_ledger_membership_invalid", "$.worker_membership")
        if (
            type(self.observed_head) is not WorkerLedgerHead
            or self.observed_head.sequence <= self.receipt_sequence
        ):
            _fail("worker_ledger_membership_invalid", "$.worker_membership")


@dataclass(frozen=True)
class WorkerExecutionGenericReceiptEvidence:
    """Generic transaction parent loaded from the durable receipt namespace."""

    transaction_id: str
    kind: str
    committed_at: str

    def __post_init__(self) -> None:
        _text(self.transaction_id, "$.receipt.transaction_id")
        _text(self.kind, "$.receipt.kind")
        _timestamp(self.committed_at, "$.receipt.committed_at")


def _validate_specialised_receipt(
    value: Any,
    *,
    path: str,
    transaction_prefix: str,
    transaction_kind: str,
    request_class: str,
    operation_type: type,
) -> None:
    _lower_hex(value.request_transaction_id, path + ".request_transaction_id", 32)
    if type(value.receipt_parent) is not WorkerExecutionGenericReceiptEvidence:
        _fail("worker_receipt_parent_invalid", path + ".receipt_parent")
    if (
        value.receipt_parent.transaction_id
        != transaction_prefix + value.request_transaction_id
        or value.receipt_parent.kind != transaction_kind
        or value.receipt_parent.committed_at != value.occurred_at
    ):
        _fail("worker_receipt_parent_invalid", path + ".receipt_parent")
    if value.request_class != request_class or type(
        value.operation
    ) is not operation_type:
        _fail("worker_receipt_identity_invalid", path)
    if type(value.target_kind) is not WorkerTargetKind:
        _fail("worker_receipt_identity_invalid", path + ".target_kind")
    if (
        request_class == PIPELINE_BUSINESS_CONTROL_REQUEST_CLASS
        and value.target_kind is not WorkerTargetKind.PIPELINE
    ):
        _fail("worker_receipt_identity_invalid", path + ".target_kind")
    _lower_hex(value.target_id, path + ".target_id", 32)
    if value.before_status is not None and type(
        value.before_status
    ) is not WorkerExecutionStatus:
        _fail("worker_receipt_transition_invalid", path + ".before_status")
    if type(value.after_status) is not WorkerExecutionStatus:
        _fail("worker_receipt_transition_invalid", path + ".after_status")
    if request_class == WORKER_EXECUTION_REQUEST_CLASS:
        if (
            value.before_status is None
            or value.before_state_revision is None
        ):
            _fail("worker_receipt_transition_invalid", path)
    else:
        create = value.operation is PipelineBusinessControlOperation.CREATE
        empty_before = (
            value.before_status is None
            and value.before_state_revision is None
        )
        if create != empty_before:
            _fail("worker_receipt_transition_invalid", path)
    if value.before_state_revision is not None:
        _integer(
            value.before_state_revision,
            path + ".before_state_revision",
            minimum=1,
        )
    _integer(
        value.after_state_revision,
        path + ".after_state_revision",
        minimum=1,
    )
    if value.after_state_revision != (
        1
        if value.before_state_revision is None
        else value.before_state_revision + 1
    ):
        _fail("worker_receipt_transition_invalid", path)
    _integer(value.sequence, path + ".sequence", minimum=1)
    previous = _digest(
        value.previous_ledger_tail_sha256,
        path + ".previous_ledger_tail_sha256",
    )
    tail = _digest(value.ledger_tail_sha256, path + ".ledger_tail_sha256")
    if tail == EMPTY_WORKER_LEDGER_TAIL_SHA256 or (
        value.sequence == 1
    ) != (previous == EMPTY_WORKER_LEDGER_TAIL_SHA256):
        _fail("worker_receipt_ledger_invalid", path)
    _digest(value.request_sha256, path + ".request_sha256")
    _digest(value.after_state_sha256, path + ".after_state_sha256")
    _timestamp(value.occurred_at, path + ".occurred_at")
    _digest(value.receipt_sha256, path + ".receipt_sha256")


@dataclass(frozen=True)
class WorkerExecutionReceiptEvidence:
    """Expected specialised worker receipt; construction is not persistence."""

    request_transaction_id: str
    receipt_parent: WorkerExecutionGenericReceiptEvidence
    request_class: str
    operation: WorkerExecutionOperation
    target_kind: WorkerTargetKind
    target_id: str
    before_status: WorkerExecutionStatus
    after_status: WorkerExecutionStatus
    before_state_revision: int
    after_state_revision: int
    sequence: int
    previous_ledger_tail_sha256: bytes = field(repr=False)
    ledger_tail_sha256: bytes = field(repr=False)
    request_sha256: bytes = field(repr=False)
    after_state_sha256: bytes = field(repr=False)
    occurred_at: str
    receipt_sha256: bytes = field(repr=False)

    def __post_init__(self) -> None:
        _validate_specialised_receipt(
            self,
            path="$.worker_receipt",
            transaction_prefix=WORKER_EXECUTION_TRANSACTION_PREFIX,
            transaction_kind=WORKER_EXECUTION_TRANSACTION_KIND,
            request_class=WORKER_EXECUTION_REQUEST_CLASS,
            operation_type=WorkerExecutionOperation,
        )


@dataclass(frozen=True)
class PipelineBusinessControlReceiptEvidence:
    """Expected specialised business-control receipt on the shared ledger."""

    request_transaction_id: str
    receipt_parent: WorkerExecutionGenericReceiptEvidence
    request_class: str
    operation: PipelineBusinessControlOperation
    target_kind: WorkerTargetKind
    target_id: str
    before_status: Optional[WorkerExecutionStatus]
    after_status: WorkerExecutionStatus
    before_state_revision: Optional[int]
    after_state_revision: int
    sequence: int
    previous_ledger_tail_sha256: bytes = field(repr=False)
    ledger_tail_sha256: bytes = field(repr=False)
    request_sha256: bytes = field(repr=False)
    after_state_sha256: bytes = field(repr=False)
    occurred_at: str
    receipt_sha256: bytes = field(repr=False)

    def __post_init__(self) -> None:
        _validate_specialised_receipt(
            self,
            path="$.control_receipt",
            transaction_prefix=PIPELINE_BUSINESS_CONTROL_TRANSACTION_PREFIX,
            transaction_kind=PIPELINE_BUSINESS_CONTROL_TRANSACTION_KIND,
            request_class=PIPELINE_BUSINESS_CONTROL_REQUEST_CLASS,
            operation_type=PipelineBusinessControlOperation,
        )


@dataclass(frozen=True)
class WorkerExecutionPersistedObservation:
    """Raw same-snapshot read; only the durable-reader stamp makes it fresh."""

    read_status: WorkerPersistedReadStatus
    receipt: Optional[WorkerExecutionGenericReceiptEvidence] = None
    execution_receipt: Optional[WorkerExecutionReceiptEvidence] = None
    ledger_head: Optional[WorkerLedgerHead] = None
    ledger_membership: Optional[WorkerLedgerMembershipEvidence] = None
    error_code: str = ""

    def __post_init__(self) -> None:
        if type(self.read_status) is not WorkerPersistedReadStatus:
            _fail("worker_persisted_read_invalid", "$.persisted.read_status")
        if self.read_status is WorkerPersistedReadStatus.UNKNOWN:
            if (
                self.receipt is not None
                or self.execution_receipt is not None
                or self.ledger_head is not None
                or self.ledger_membership is not None
                or type(self.error_code) is not str
                or not self.error_code
            ):
                _fail("worker_persisted_read_invalid", "$.persisted")
            _text(self.error_code, "$.persisted.error_code", maximum=1024)
            return
        if (
            self.error_code != ""
            or self.ledger_head is None
            or type(self.ledger_head) is not WorkerLedgerHead
            or (
                self.ledger_membership is not None
                and (
                    type(self.ledger_membership)
                    is not WorkerLedgerMembershipEvidence
                    or self.receipt is None
                    or self.execution_receipt is None
                    or self.ledger_membership.observed_head != self.ledger_head
                    or self.ledger_membership.receipt_sequence
                    != self.execution_receipt.sequence
                    or self.ledger_membership.receipt_tail_sha256
                    != self.execution_receipt.ledger_tail_sha256
                )
            )
            or (
                self.receipt is not None
                and type(self.receipt) is not WorkerExecutionGenericReceiptEvidence
            )
            or (
                self.execution_receipt is not None
                and type(self.execution_receipt)
                is not WorkerExecutionReceiptEvidence
            )
        ):
            _fail("worker_persisted_read_invalid", "$.persisted")


@dataclass(frozen=True)
class PipelineBusinessControlPersistedObservation:
    """Raw control receipt/head read; not fresh without its reader stamp."""

    read_status: WorkerPersistedReadStatus
    receipt: Optional[WorkerExecutionGenericReceiptEvidence] = None
    control_receipt: Optional[PipelineBusinessControlReceiptEvidence] = None
    ledger_head: Optional[WorkerLedgerHead] = None
    ledger_membership: Optional[WorkerLedgerMembershipEvidence] = None
    error_code: str = ""

    def __post_init__(self) -> None:
        if type(self.read_status) is not WorkerPersistedReadStatus:
            _fail("worker_persisted_read_invalid", "$.control_persisted.read_status")
        if self.read_status is WorkerPersistedReadStatus.UNKNOWN:
            if (
                self.receipt is not None
                or self.control_receipt is not None
                or self.ledger_head is not None
                or self.ledger_membership is not None
                or type(self.error_code) is not str
                or not self.error_code
            ):
                _fail("worker_persisted_read_invalid", "$.control_persisted")
            _text(self.error_code, "$.control_persisted.error_code", maximum=1024)
            return
        if (
            self.error_code != ""
            or self.ledger_head is None
            or type(self.ledger_head) is not WorkerLedgerHead
            or (
                self.ledger_membership is not None
                and (
                    type(self.ledger_membership)
                    is not WorkerLedgerMembershipEvidence
                    or self.receipt is None
                    or self.control_receipt is None
                    or self.ledger_membership.observed_head != self.ledger_head
                    or self.ledger_membership.receipt_sequence
                    != self.control_receipt.sequence
                    or self.ledger_membership.receipt_tail_sha256
                    != self.control_receipt.ledger_tail_sha256
                )
            )
            or (
                self.receipt is not None
                and type(self.receipt) is not WorkerExecutionGenericReceiptEvidence
            )
            or (
                self.control_receipt is not None
                and type(self.control_receipt)
                is not PipelineBusinessControlReceiptEvidence
            )
        ):
            _fail("worker_persisted_read_invalid", "$.control_persisted")


_WORKER_EXECUTION_READER_CAPABILITY_ISSUER = object()
_WORKER_EXECUTION_FRESH_OBSERVATION_ISSUER = object()
_PIPELINE_BUSINESS_CONTROL_READER_CAPABILITY_ISSUER = object()
_PIPELINE_BUSINESS_CONTROL_FRESH_OBSERVATION_ISSUER = object()


class WorkerExecutionDurableReaderCapability:
    __slots__ = ("_issuer",)

    def __init__(self, *, _issuer: Any) -> None:
        if _issuer is not _WORKER_EXECUTION_READER_CAPABILITY_ISSUER:
            _fail("worker_reader_capability_invalid", "$.reader")
        object.__setattr__(self, "_issuer", _issuer)

    def __setattr__(self, name: str, value: Any) -> None:
        del name, value
        raise AttributeError("WorkerExecutionDurableReaderCapability is immutable")


class PipelineBusinessControlDurableReaderCapability:
    __slots__ = ("_issuer",)

    def __init__(self, *, _issuer: Any) -> None:
        if _issuer is not _PIPELINE_BUSINESS_CONTROL_READER_CAPABILITY_ISSUER:
            _fail("worker_reader_capability_invalid", "$.control_reader")
        object.__setattr__(self, "_issuer", _issuer)

    def __setattr__(self, name: str, value: Any) -> None:
        del name, value
        raise AttributeError(
            "PipelineBusinessControlDurableReaderCapability is immutable"
        )


_WORKER_EXECUTION_DURABLE_READER_CAPABILITY = (
    WorkerExecutionDurableReaderCapability(
        _issuer=_WORKER_EXECUTION_READER_CAPABILITY_ISSUER
    )
)
_PIPELINE_BUSINESS_CONTROL_DURABLE_READER_CAPABILITY = (
    PipelineBusinessControlDurableReaderCapability(
        _issuer=_PIPELINE_BUSINESS_CONTROL_READER_CAPABILITY_ISSUER
    )
)


class WorkerExecutionFreshObservation:
    __slots__ = ("persisted", "_reader", "_issuer")

    def __init__(
        self,
        persisted: WorkerExecutionPersistedObservation,
        *,
        _reader: WorkerExecutionDurableReaderCapability,
        _issuer: Any,
    ) -> None:
        if (
            _issuer is not _WORKER_EXECUTION_FRESH_OBSERVATION_ISSUER
            or _reader is not _WORKER_EXECUTION_DURABLE_READER_CAPABILITY
            or type(persisted) is not WorkerExecutionPersistedObservation
        ):
            _fail("worker_fresh_observation_invalid", "$.fresh")
        object.__setattr__(self, "persisted", persisted)
        object.__setattr__(self, "_reader", _reader)
        object.__setattr__(self, "_issuer", _issuer)

    def __setattr__(self, name: str, value: Any) -> None:
        del name, value
        raise AttributeError("WorkerExecutionFreshObservation is immutable")


class PipelineBusinessControlFreshObservation:
    __slots__ = ("persisted", "_reader", "_issuer")

    def __init__(
        self,
        persisted: PipelineBusinessControlPersistedObservation,
        *,
        _reader: PipelineBusinessControlDurableReaderCapability,
        _issuer: Any,
    ) -> None:
        if (
            _issuer is not _PIPELINE_BUSINESS_CONTROL_FRESH_OBSERVATION_ISSUER
            or _reader
            is not _PIPELINE_BUSINESS_CONTROL_DURABLE_READER_CAPABILITY
            or type(persisted)
            is not PipelineBusinessControlPersistedObservation
        ):
            _fail("worker_fresh_observation_invalid", "$.control_fresh")
        object.__setattr__(self, "persisted", persisted)
        object.__setattr__(self, "_reader", _reader)
        object.__setattr__(self, "_issuer", _issuer)

    def __setattr__(self, name: str, value: Any) -> None:
        del name, value
        raise AttributeError(
            "PipelineBusinessControlFreshObservation is immutable"
        )


def _stamp_worker_execution_fresh_observation(
    reader: WorkerExecutionDurableReaderCapability,
    persisted: WorkerExecutionPersistedObservation,
) -> WorkerExecutionFreshObservation:
    if reader is not _WORKER_EXECUTION_DURABLE_READER_CAPABILITY:
        _fail("worker_reader_capability_invalid", "$.reader")
    return WorkerExecutionFreshObservation(
        persisted,
        _reader=reader,
        _issuer=_WORKER_EXECUTION_FRESH_OBSERVATION_ISSUER,
    )


def _stamp_pipeline_business_control_fresh_observation(
    reader: PipelineBusinessControlDurableReaderCapability,
    persisted: PipelineBusinessControlPersistedObservation,
) -> PipelineBusinessControlFreshObservation:
    if reader is not _PIPELINE_BUSINESS_CONTROL_DURABLE_READER_CAPABILITY:
        _fail("worker_reader_capability_invalid", "$.control_reader")
    return PipelineBusinessControlFreshObservation(
        persisted,
        _reader=reader,
        _issuer=_PIPELINE_BUSINESS_CONTROL_FRESH_OBSERVATION_ISSUER,
    )


def _unwrap_worker_execution_fresh_observation(
    value: Any,
) -> WorkerExecutionPersistedObservation:
    if (
        type(value) is not WorkerExecutionFreshObservation
        or getattr(value, "_reader", None)
        is not _WORKER_EXECUTION_DURABLE_READER_CAPABILITY
        or getattr(value, "_issuer", None)
        is not _WORKER_EXECUTION_FRESH_OBSERVATION_ISSUER
        or type(getattr(value, "persisted", None))
        is not WorkerExecutionPersistedObservation
    ):
        _fail("worker_fresh_observation_invalid", "$.fresh")
    return value.persisted


def _unwrap_pipeline_business_control_fresh_observation(
    value: Any,
) -> PipelineBusinessControlPersistedObservation:
    if (
        type(value) is not PipelineBusinessControlFreshObservation
        or getattr(value, "_reader", None)
        is not _PIPELINE_BUSINESS_CONTROL_DURABLE_READER_CAPABILITY
        or getattr(value, "_issuer", None)
        is not _PIPELINE_BUSINESS_CONTROL_FRESH_OBSERVATION_ISSUER
        or type(getattr(value, "persisted", None))
        is not PipelineBusinessControlPersistedObservation
    ):
        _fail("worker_fresh_observation_invalid", "$.control_fresh")
    return value.persisted


@dataclass(frozen=True)
class WorkerExecutionReceiptClassification:
    status: WorkerReceiptReconcileStatus
    request_transaction_id: str
    evidence: Optional[WorkerExecutionReceiptEvidence] = None
    error_code: str = ""

    def __post_init__(self) -> None:
        _validate_receipt_classification(self, WorkerExecutionReceiptEvidence)

    @property
    def retry_action(self) -> WorkerReceiptRetryAction:
        return _receipt_retry_action(self.status)


@dataclass(frozen=True)
class PipelineBusinessControlReceiptClassification:
    status: WorkerReceiptReconcileStatus
    request_transaction_id: str
    evidence: Optional[PipelineBusinessControlReceiptEvidence] = None
    error_code: str = ""

    def __post_init__(self) -> None:
        _validate_receipt_classification(
            self,
            PipelineBusinessControlReceiptEvidence,
        )

    @property
    def retry_action(self) -> WorkerReceiptRetryAction:
        return _receipt_retry_action(self.status)


def _validate_receipt_classification(value: Any, evidence_type: type) -> None:
    if type(value.status) is not WorkerReceiptReconcileStatus:
        _fail("worker_receipt_classification_invalid", "$.classification.status")
    _lower_hex(
        value.request_transaction_id,
        "$.classification.request_transaction_id",
        32,
    )
    exact = value.status is WorkerReceiptReconcileStatus.EXACT
    failed = value.status in (
        WorkerReceiptReconcileStatus.CONFLICT,
        WorkerReceiptReconcileStatus.CORRUPT,
        WorkerReceiptReconcileStatus.UNKNOWN,
    )
    if (
        (exact and type(value.evidence) is not evidence_type)
        or (not exact and value.evidence is not None)
        or (
            exact
            and value.evidence is not None
            and value.evidence.request_transaction_id
            != value.request_transaction_id
        )
        or (failed and (type(value.error_code) is not str or not value.error_code))
        or (not failed and value.error_code != "")
    ):
        _fail("worker_receipt_classification_invalid", "$.classification")


def _receipt_retry_action(
    status: WorkerReceiptReconcileStatus,
) -> WorkerReceiptRetryAction:
    if status is WorkerReceiptReconcileStatus.ABSENT:
        return WorkerReceiptRetryAction.APPLY_EXACT
    if status is WorkerReceiptReconcileStatus.EXACT:
        return WorkerReceiptRetryAction.RETURN_EXACT
    if status is WorkerReceiptReconcileStatus.UNKNOWN:
        return WorkerReceiptRetryAction.RECONCILE_REQUIRED
    return WorkerReceiptRetryAction.STOP


__all__ = ()
