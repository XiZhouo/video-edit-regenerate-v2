"""Pure immutable inputs for one future atomic business transaction.

The types in this module are data contracts only.  They do not open a
database, inspect a Document, call a DeferredAction, publish an event, choose a
clock, or claim that a transaction committed.  A writer may accept only these
exact frozen carriers and must persist their fixed statuses in the same SQL
transaction as the business head.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from business_codec import (
    BusinessCodecError,
    CODEC_ID,
    CODEC_VERSION,
    decode_payload_bounded,
    encode_payload_bounded,
)


ATOMIC_COMMIT_PROTOCOL = "ze.atomic-business-commit.v2"
ATOMIC_COMMIT_MANIFEST_SCHEMA = "ze.atomic-business-commit.manifest.v2"
ATOMIC_COMMIT_STATUS_PREPARED = "prepared"
ATOMIC_COMMIT_STATUS_COMMITTED = "committed"
ATOMIC_COMMIT_STATUS_ABORTED = "aborted"
ATOMIC_COMMIT_STATUS_INDETERMINATE = "indeterminate"

OUTBOX_STATUS_PENDING = "pending"
ACTION_STATUS_QUEUED = "queued"
PIPELINE_STATUS_QUEUED = "queued"
INBOX_EXPECTED_STATUS_CLAIMED = "claimed"
INBOX_TARGET_STATUS_COMMITTED = "committed"
CONFIRMATION_EXPECTED_STATUS_RESERVED = "reserved"
CONFIRMATION_TARGET_STATUS_CONSUMED = "consumed"

SQLITE_AUTHORITY = "sqlite"
SQLITE_AUTHORITY_STATE_ACTIVE = "active"

MAX_ATOMIC_OPERATIONS = 10_000
MAX_ATOMIC_OPERATION_TARGETS = 1_000_000
MAX_ATOMIC_OUTBOX_EVENTS = MAX_ATOMIC_OPERATIONS
MAX_ATOMIC_DURABLE_ACTIONS = 10_000
MAX_ATOMIC_PIPELINE_INTENTS = 10_000
MAX_ATOMIC_ASSET_REFERENCES = 1_000_000
MAX_ATOMIC_JSON_PAYLOAD_BYTES = 16 * 1024 * 1024
MAX_ATOMIC_JSON_PAYLOAD_NODES = 250_000
MAX_ATOMIC_TOTAL_AUXILIARY_BYTES = 256 * 1024 * 1024
MAX_ATOMIC_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_ATOMIC_MANIFEST_NODES = 1_000_000
MAX_ATOMIC_TEXT_BYTES = 1024 * 1024
MAX_BUSINESS_ID_BYTES = 1024
MAX_PROJECT_ID_BYTES = 640
MAX_PROJECT_ID_CHARS = 160
MAX_RUNTIME_GENERATION_BYTES = 256
MAX_RETRY_ATTEMPTS = 100
MAX_SQLITE_INTEGER = 2**63 - 1

_LOWER_HEX = frozenset("0123456789abcdef")


class BusinessTransactionContractError(ValueError):
    """Stable, payload-free contract failure."""

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
    raise BusinessTransactionContractError(code, path)


def _text(value: Any, path: str, *, nonempty: bool = True, maximum: int = MAX_ATOMIC_TEXT_BYTES) -> str:
    if type(value) is not str or (nonempty and not value):
        _fail("business_transaction_text_invalid", path)
    invalid_utf8 = False
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeEncodeError:
        invalid_utf8 = True
        encoded = b""
    if invalid_utf8:
        _fail("business_transaction_text_invalid", path)
    if len(encoded) > maximum:
        _fail("business_transaction_text_budget_exceeded", path)
    return value


def _integer(value: Any, path: str, *, minimum: int = 0, maximum: int = MAX_SQLITE_INTEGER) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _fail("business_transaction_integer_invalid", path)
    return value


def _identifier(value: Any, path: str) -> str:
    return _text(value, path, maximum=MAX_BUSINESS_ID_BYTES)


def _project_id(value: Any, path: str) -> str:
    value = _text(value, path, maximum=MAX_PROJECT_ID_BYTES)
    if len(value) > MAX_PROJECT_ID_CHARS:
        _fail("business_transaction_project_id_invalid", path)
    return value


def _digest(value: Any, path: str) -> bytes:
    if type(value) is not bytes or len(value) != 32:
        _fail("business_transaction_digest_invalid", path)
    return value


def _lower_hex(value: Any, path: str, length: int) -> str:
    if (
        type(value) is not str
        or len(value) != length
        or any(character not in _LOWER_HEX for character in value)
    ):
        _fail("business_transaction_hex_invalid", path)
    return value


@dataclass(frozen=True)
class FrozenJsonPayload:
    """Canonical strict-JSON bytes; never retains the caller's mutable graph."""

    schema: str
    codec_id: str
    codec_version: int
    payload_bytes: bytes = field(repr=False)
    payload_sha256: bytes = field(repr=False)

    def __post_init__(self) -> None:
        _text(self.schema, "$.payload.schema", maximum=1024)
        if self.codec_id != CODEC_ID or type(self.codec_id) is not str:
            _fail("business_transaction_codec_invalid", "$.payload.codec_id")
        if type(self.codec_version) is not int or self.codec_version != CODEC_VERSION:
            _fail("business_transaction_codec_invalid", "$.payload.codec_version")
        if type(self.payload_bytes) is not bytes or len(self.payload_bytes) > MAX_ATOMIC_JSON_PAYLOAD_BYTES:
            _fail("business_transaction_payload_invalid", "$.payload.bytes")
        _digest(self.payload_sha256, "$.payload.sha256")
        if not hmac.compare_digest(hashlib.sha256(self.payload_bytes).digest(), self.payload_sha256):
            _fail("business_transaction_payload_digest_mismatch", "$.payload")
        invalid_payload = False
        try:
            decoded = decode_payload_bounded(
                self.payload_bytes,
                MAX_ATOMIC_JSON_PAYLOAD_BYTES,
                MAX_ATOMIC_JSON_PAYLOAD_NODES,
            )
            canonical = encode_payload_bounded(
                decoded,
                MAX_ATOMIC_JSON_PAYLOAD_BYTES,
                MAX_ATOMIC_JSON_PAYLOAD_NODES,
            )
        except BusinessCodecError:
            invalid_payload = True
            canonical = b""
        if invalid_payload:
            _fail("business_transaction_payload_invalid", "$.payload")
        if canonical != self.payload_bytes:
            _fail("business_transaction_payload_not_canonical", "$.payload")


def freeze_json_payload(value: Any, *, schema: str) -> FrozenJsonPayload:
    """Validate, detach and canonically encode one action/event/request payload."""
    _text(schema, "$.payload.schema", maximum=1024)
    invalid_payload = False
    try:
        payload = encode_payload_bounded(
            value,
            MAX_ATOMIC_JSON_PAYLOAD_BYTES,
            MAX_ATOMIC_JSON_PAYLOAD_NODES,
        )
    except BusinessCodecError:
        invalid_payload = True
        payload = b""
    if invalid_payload:
        _fail("business_transaction_payload_invalid", "$.payload")
    return FrozenJsonPayload(
        schema=schema,
        codec_id=CODEC_ID,
        codec_version=CODEC_VERSION,
        payload_bytes=payload,
        payload_sha256=hashlib.sha256(payload).digest(),
    )


def verify_frozen_json_payload(value: Any) -> FrozenJsonPayload:
    if type(value) is not FrozenJsonPayload:
        _fail("business_transaction_payload_type_invalid", "$.payload")
    rebuilt = FrozenJsonPayload(
        schema=value.schema,
        codec_id=value.codec_id,
        codec_version=value.codec_version,
        payload_bytes=value.payload_bytes,
        payload_sha256=value.payload_sha256,
    )
    if rebuilt != value:
        _fail("business_transaction_payload_changed", "$.payload")
    return value


class AtomicRetryDisposition(str, Enum):
    """The only retry advice a transaction adapter may emit."""

    EXACT_RETRY = "exact_retry"
    RELOAD_AND_REAPPLY = "reload_and_reapply"
    NEVER = "never"


class AtomicFailureKind(str, Enum):
    """Fact-based failure class; exception text must never choose retry policy."""

    EXACT_NOT_COMMITTED = "exact_not_committed"
    HEAD_CONFLICT = "head_conflict"
    IDENTITY_CONFLICT = "identity_conflict"
    DURABLE_CORRUPT = "durable_corrupt"
    OUTCOME_UNKNOWN = "outcome_unknown"
    INPUT_REJECTED = "input_rejected"


_FAILURE_RETRY = {
    AtomicFailureKind.EXACT_NOT_COMMITTED: AtomicRetryDisposition.EXACT_RETRY,
    AtomicFailureKind.HEAD_CONFLICT: AtomicRetryDisposition.RELOAD_AND_REAPPLY,
    AtomicFailureKind.IDENTITY_CONFLICT: AtomicRetryDisposition.NEVER,
    AtomicFailureKind.DURABLE_CORRUPT: AtomicRetryDisposition.NEVER,
    AtomicFailureKind.OUTCOME_UNKNOWN: AtomicRetryDisposition.NEVER,
    AtomicFailureKind.INPUT_REJECTED: AtomicRetryDisposition.NEVER,
}


class InboxOutcomeKind(str, Enum):
    COMMIT = "commit"
    CONFIRMATION = "confirmation"
    NOOP = "noop"


class ActionReplayPolicy(str, Enum):
    PROVIDER_IDEMPOTENT = "provider_idempotent"
    RECONCILE_BEFORE_RETRY = "reconcile_before_retry"
    NEVER_REPLAY = "never_replay"


class IdempotencyPolicy(str, Enum):
    REQUIRED_EXTERNAL_KEY = "required_external_key"
    DURABLE_RECEIPT_RECONCILE = "durable_receipt_reconcile"
    SINGLE_ATTEMPT = "single_attempt"


class PipelineIntentKind(str, Enum):
    CREATE = "create"
    TRANSITION = "transition"


class PipelineReplayPolicy(str, Enum):
    EXACT_CAS_RETRY = "exact_cas_retry"
    RELOAD_AND_REPLAN = "reload_and_replan"
    NEVER_REPLAY = "never_replay"


class PipelineJobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REPAIR_REQUIRED = "repair_required"


# Atomic business commits may change eligibility or request cancellation, but
# they do not own a worker execution lease or attempt lifecycle.  Claiming
# QUEUED work and finishing/abandoning an attempt require the separate typed
# worker contract carrying lease, fence, attempt identity, outcome and time.
_PIPELINE_TRANSITIONS = frozenset(
    (
        (PipelineJobStatus.QUEUED, PipelineJobStatus.PAUSED),
        (PipelineJobStatus.QUEUED, PipelineJobStatus.CANCELLED),
        (PipelineJobStatus.RUNNING, PipelineJobStatus.CANCEL_REQUESTED),
        (PipelineJobStatus.PAUSED, PipelineJobStatus.QUEUED),
        (PipelineJobStatus.PAUSED, PipelineJobStatus.CANCELLED),
    )
)


@dataclass(frozen=True)
class AtomicCommitFailure:
    """One classified failure with a mechanically fixed retry disposition."""

    kind: AtomicFailureKind
    code: str
    retry: AtomicRetryDisposition = field(init=False)

    def __post_init__(self) -> None:
        if type(self.kind) is not AtomicFailureKind:
            _fail("business_transaction_failure_kind_invalid", "$.failure.kind")
        _text(self.code, "$.failure.code", maximum=1024)
        object.__setattr__(self, "retry", _FAILURE_RETRY[self.kind])


@dataclass(frozen=True)
class AtomicCommitResult:
    """Adapter result that cannot collapse conflict and UNKNOWN into one bool."""

    commit_id: str
    status: str
    manifest_sha256: bytes = field(repr=False)
    failure: Optional[AtomicCommitFailure] = None
    repository_revision: Optional[int] = None
    recovered_after_ack_loss: bool = False

    def __post_init__(self) -> None:
        _lower_hex(self.commit_id, "$.result.commit_id", 32)
        _digest(self.manifest_sha256, "$.result.manifest_sha256")
        if type(self.recovered_after_ack_loss) is not bool:
            _fail("business_transaction_result_invalid", "$.result.recovered_after_ack_loss")
        if self.status == ATOMIC_COMMIT_STATUS_COMMITTED:
            if self.failure is not None or self.repository_revision is None:
                _fail("business_transaction_result_invalid", "$.result.failure")
            _integer(
                self.repository_revision,
                "$.result.repository_revision",
                minimum=1,
            )
            return
        if self.status not in (
            ATOMIC_COMMIT_STATUS_ABORTED,
            ATOMIC_COMMIT_STATUS_INDETERMINATE,
        ) or type(self.failure) is not AtomicCommitFailure:
            _fail("business_transaction_result_invalid", "$.result")
        if self.repository_revision is not None or self.recovered_after_ack_loss:
            _fail("business_transaction_result_invalid", "$.result.repository_revision")
        if (
            self.status == ATOMIC_COMMIT_STATUS_INDETERMINATE
            and self.failure.kind not in (
                AtomicFailureKind.OUTCOME_UNKNOWN,
                AtomicFailureKind.DURABLE_CORRUPT,
            )
        ):
            _fail("business_transaction_result_invalid", "$.result.status")
        if (
            self.status == ATOMIC_COMMIT_STATUS_ABORTED
            and self.failure.kind in (
                AtomicFailureKind.OUTCOME_UNKNOWN,
                AtomicFailureKind.DURABLE_CORRUPT,
            )
        ):
            _fail("business_transaction_result_invalid", "$.result.status")

    @property
    def retry(self) -> AtomicRetryDisposition:
        return (
            AtomicRetryDisposition.NEVER
            if self.failure is None
            else self.failure.retry
        )


@dataclass(frozen=True)
class AuthorityBinding:
    authority: str
    authority_state: str
    authority_epoch: str
    source_admission_revision: int
    authority_revision: int
    control_revision: int
    cutover_id: str
    runtime_generation: str
    code_digest: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.authority) is not str or self.authority != SQLITE_AUTHORITY:
            _fail("business_transaction_authority_invalid", "$.authority.authority")
        if (
            type(self.authority_state) is not str
            or self.authority_state != SQLITE_AUTHORITY_STATE_ACTIVE
        ):
            _fail("business_transaction_authority_invalid", "$.authority.state")
        _lower_hex(self.authority_epoch, "$.authority.epoch", 64)
        _integer(
            self.source_admission_revision,
            "$.authority.source_admission_revision",
            minimum=1,
        )
        _integer(self.authority_revision, "$.authority.revision", minimum=1)
        _integer(self.control_revision, "$.authority.control_revision", minimum=1)
        _lower_hex(self.cutover_id, "$.authority.cutover_id", 64)
        _text(
            self.runtime_generation,
            "$.authority.runtime_generation",
            maximum=MAX_RUNTIME_GENERATION_BYTES,
        )
        _digest(self.code_digest, "$.authority.code_digest")


@dataclass(frozen=True)
class BeforeBusinessHead:
    project_id: str
    generation: int
    document_id: str
    document_version: int
    project_revision: int
    head_revision: int
    expected_last_commit_id: str
    business_root_sha256: bytes = field(repr=False)
    operation_tail_hash: str = field(repr=False)

    def __post_init__(self) -> None:
        _project_id(self.project_id, "$.before.project_id")
        _integer(self.generation, "$.before.generation", minimum=1)
        _identifier(self.document_id, "$.before.document_id")
        _integer(self.document_version, "$.before.document_version", minimum=1)
        _integer(self.project_revision, "$.before.project_revision", minimum=1)
        _integer(self.head_revision, "$.before.head_revision", minimum=1)
        _lower_hex(
            self.expected_last_commit_id,
            "$.before.expected_last_commit_id",
            32,
        )
        _digest(self.business_root_sha256, "$.before.business_root_sha256")
        _lower_hex(self.operation_tail_hash, "$.before.operation_tail_hash", 64)


@dataclass(frozen=True)
class OutboxEventIntent:
    ordinal: int
    event_id: str
    operation_id: str
    topic: str
    partition_key: str
    payload: FrozenJsonPayload = field(repr=False)
    status: str = field(init=False, default=OUTBOX_STATUS_PENDING)

    def __post_init__(self) -> None:
        _integer(self.ordinal, "$.outbox.ordinal", maximum=MAX_ATOMIC_OUTBOX_EVENTS - 1)
        _lower_hex(self.event_id, "$.outbox.event_id", 32)
        _lower_hex(self.operation_id, "$.outbox.operation_id", 32)
        _identifier(self.topic, "$.outbox.topic")
        _identifier(self.partition_key, "$.outbox.partition_key")
        if type(self.payload) is not FrozenJsonPayload:
            _fail("business_transaction_payload_type_invalid", "$.outbox.payload")


@dataclass(frozen=True)
class DurableActionIntent:
    ordinal: int
    action_id: str
    action_type: str
    idempotency_key: str
    payload: FrozenJsonPayload = field(repr=False)
    replay_policy: ActionReplayPolicy = ActionReplayPolicy.PROVIDER_IDEMPOTENT
    idempotency_policy: IdempotencyPolicy = IdempotencyPolicy.REQUIRED_EXTERNAL_KEY
    idempotency_scope: str = "global"
    max_attempts: int = 10
    status: str = field(init=False, default=ACTION_STATUS_QUEUED)

    def __post_init__(self) -> None:
        _integer(self.ordinal, "$.actions.ordinal", maximum=MAX_ATOMIC_DURABLE_ACTIONS - 1)
        _lower_hex(self.action_id, "$.actions.action_id", 32)
        _identifier(self.action_type, "$.actions.action_type")
        _identifier(self.idempotency_key, "$.actions.idempotency_key")
        _identifier(self.idempotency_scope, "$.actions.idempotency_scope")
        if type(self.payload) is not FrozenJsonPayload:
            _fail("business_transaction_payload_type_invalid", "$.actions.payload")
        if type(self.replay_policy) is not ActionReplayPolicy:
            _fail("business_transaction_replay_policy_invalid", "$.actions.replay_policy")
        if type(self.idempotency_policy) is not IdempotencyPolicy:
            _fail("business_transaction_idempotency_policy_invalid", "$.actions.idempotency_policy")
        _integer(self.max_attempts, "$.actions.max_attempts", minimum=1, maximum=MAX_RETRY_ATTEMPTS)
        if (
            self.replay_policy is ActionReplayPolicy.NEVER_REPLAY
            or self.idempotency_policy is IdempotencyPolicy.SINGLE_ATTEMPT
        ) and self.max_attempts != 1:
            _fail("business_transaction_retry_policy_inconsistent", "$.actions.max_attempts")


@dataclass(frozen=True)
class PipelineIntent:
    ordinal: int
    intent_id: str
    job_id: str
    pipeline_id: str
    intent_type: str
    idempotency_key: str
    payload: FrozenJsonPayload = field(repr=False)
    intent_kind: PipelineIntentKind = PipelineIntentKind.CREATE
    expected_status: Optional[PipelineJobStatus] = None
    target_status: PipelineJobStatus = PipelineJobStatus.QUEUED
    replay_policy: PipelineReplayPolicy = PipelineReplayPolicy.EXACT_CAS_RETRY
    expected_state_revision: Optional[int] = None
    max_attempts: int = 10
    status: str = field(init=False, default=PIPELINE_STATUS_QUEUED)

    def __post_init__(self) -> None:
        _integer(self.ordinal, "$.pipelines.ordinal", maximum=MAX_ATOMIC_PIPELINE_INTENTS - 1)
        _lower_hex(self.intent_id, "$.pipelines.intent_id", 32)
        _lower_hex(self.job_id, "$.pipelines.job_id", 32)
        _identifier(self.pipeline_id, "$.pipelines.pipeline_id")
        _identifier(self.intent_type, "$.pipelines.intent_type")
        _identifier(self.idempotency_key, "$.pipelines.idempotency_key")
        if type(self.payload) is not FrozenJsonPayload:
            _fail("business_transaction_payload_type_invalid", "$.pipelines.payload")
        if type(self.intent_kind) is not PipelineIntentKind:
            _fail("business_transaction_pipeline_kind_invalid", "$.pipelines.intent_kind")
        if type(self.target_status) is not PipelineJobStatus:
            _fail("business_transaction_pipeline_status_invalid", "$.pipelines.target_status")
        if type(self.replay_policy) is not PipelineReplayPolicy:
            _fail("business_transaction_replay_policy_invalid", "$.pipelines.replay_policy")
        if self.intent_kind is PipelineIntentKind.CREATE:
            if (
                self.expected_state_revision is not None
                or self.expected_status is not None
                or self.target_status is not PipelineJobStatus.QUEUED
                or self.intent_id != self.job_id
            ):
                _fail("business_transaction_pipeline_revision_invalid", "$.pipelines.expected_state_revision")
        else:
            if (
                type(self.expected_status) is not PipelineJobStatus
                or (self.expected_status, self.target_status)
                not in _PIPELINE_TRANSITIONS
            ):
                _fail("business_transaction_pipeline_status_invalid", "$.pipelines.target_status")
            _integer(
                self.expected_state_revision,
                "$.pipelines.expected_state_revision",
                minimum=1,
            )
        _integer(self.max_attempts, "$.pipelines.max_attempts", minimum=1, maximum=MAX_RETRY_ATTEMPTS)
        if self.replay_policy is PipelineReplayPolicy.NEVER_REPLAY and self.max_attempts != 1:
            _fail("business_transaction_retry_policy_inconsistent", "$.pipelines.max_attempts")


@dataclass(frozen=True)
class InboxClaim:
    scope: str
    inbox_key: str
    actor_id: str
    claim_owner_id: str
    claim_fence: int
    state_revision: int
    outcome_kind: InboxOutcomeKind
    request_sha256: bytes = field(repr=False)
    request: FrozenJsonPayload = field(repr=False)
    response: FrozenJsonPayload = field(repr=False)
    expected_status: str = field(init=False, default=INBOX_EXPECTED_STATUS_CLAIMED)
    target_status: str = field(init=False, default=INBOX_TARGET_STATUS_COMMITTED)

    def __post_init__(self) -> None:
        _identifier(self.scope, "$.inbox.scope")
        _identifier(self.inbox_key, "$.inbox.key")
        _identifier(self.actor_id, "$.inbox.actor_id")
        _lower_hex(self.claim_owner_id, "$.inbox.claim_owner_id", 32)
        _integer(self.claim_fence, "$.inbox.claim_fence", minimum=1)
        _integer(
            self.state_revision,
            "$.inbox.state_revision",
            minimum=1,
        )
        if type(self.outcome_kind) is not InboxOutcomeKind:
            _fail("business_transaction_inbox_outcome_invalid", "$.inbox.outcome_kind")
        _digest(self.request_sha256, "$.inbox.request_sha256")
        if type(self.request) is not FrozenJsonPayload:
            _fail("business_transaction_payload_type_invalid", "$.inbox.request")
        if type(self.response) is not FrozenJsonPayload:
            _fail("business_transaction_payload_type_invalid", "$.inbox.response")
        if not hmac.compare_digest(self.request_sha256, self.request.payload_sha256):
            _fail("business_transaction_inbox_digest_mismatch", "$.inbox")


@dataclass(frozen=True)
class ConfirmationConsumption:
    confirmation_id: str
    nonce_hmac: bytes = field(repr=False)
    nonce_key_id: str
    reservation_fence: int
    reservation_owner_id: str
    expected_revision: int
    expected_request_sha256: bytes = field(repr=False)
    actor_id: str
    resolution: FrozenJsonPayload = field(repr=False)
    decision: str = "confirmed"
    expected_status: str = field(
        init=False,
        default=CONFIRMATION_EXPECTED_STATUS_RESERVED,
    )
    target_status: str = field(
        init=False,
        default=CONFIRMATION_TARGET_STATUS_CONSUMED,
    )

    def __post_init__(self) -> None:
        _lower_hex(self.confirmation_id, "$.confirmation.confirmation_id", 32)
        _digest(self.nonce_hmac, "$.confirmation.nonce_hmac")
        _identifier(self.nonce_key_id, "$.confirmation.nonce_key_id")
        _integer(self.reservation_fence, "$.confirmation.reservation_fence", minimum=1)
        _lower_hex(self.reservation_owner_id, "$.confirmation.reservation_owner_id", 32)
        _integer(self.expected_revision, "$.confirmation.expected_revision", minimum=1)
        _digest(
            self.expected_request_sha256,
            "$.confirmation.expected_request_sha256",
        )
        _identifier(self.actor_id, "$.confirmation.actor_id")
        if type(self.resolution) is not FrozenJsonPayload:
            _fail(
                "business_transaction_payload_type_invalid",
                "$.confirmation.resolution",
            )
        if type(self.decision) is not str or self.decision != "confirmed":
            _fail("business_transaction_confirmation_invalid", "$.confirmation.decision")


@dataclass(frozen=True)
class CandidateBusinessHead:
    project_id: str
    generation: int
    document_id: str
    document_version: int
    project_revision: int
    head_revision: int
    authority_revision: int
    commit_id: str
    business_root_sha256: bytes = field(repr=False)
    operation_tail_hash: str = field(repr=False)

    def __post_init__(self) -> None:
        _project_id(self.project_id, "$.candidate.project_id")
        _integer(self.generation, "$.candidate.generation", minimum=1)
        _identifier(self.document_id, "$.candidate.document_id")
        _integer(self.document_version, "$.candidate.document_version", minimum=1)
        _integer(
            self.project_revision,
            "$.candidate.project_revision",
            minimum=1,
        )
        _integer(self.head_revision, "$.candidate.head_revision", minimum=1)
        _integer(self.authority_revision, "$.candidate.authority_revision", minimum=1)
        _lower_hex(self.commit_id, "$.candidate.commit_id", 32)
        _digest(self.business_root_sha256, "$.candidate.business_root_sha256")
        _lower_hex(self.operation_tail_hash, "$.candidate.operation_tail_hash", 64)


__all__ = [
    "ACTION_STATUS_QUEUED",
    "ATOMIC_COMMIT_MANIFEST_SCHEMA",
    "ATOMIC_COMMIT_PROTOCOL",
    "ATOMIC_COMMIT_STATUS_ABORTED",
    "ATOMIC_COMMIT_STATUS_COMMITTED",
    "ATOMIC_COMMIT_STATUS_INDETERMINATE",
    "ATOMIC_COMMIT_STATUS_PREPARED",
    "AuthorityBinding",
    "AtomicCommitFailure",
    "AtomicCommitResult",
    "AtomicFailureKind",
    "AtomicRetryDisposition",
    "ActionReplayPolicy",
    "CandidateBusinessHead",
    "BeforeBusinessHead",
    "BusinessTransactionContractError",
    "CONFIRMATION_EXPECTED_STATUS_RESERVED",
    "CONFIRMATION_TARGET_STATUS_CONSUMED",
    "ConfirmationConsumption",
    "DurableActionIntent",
    "FrozenJsonPayload",
    "IdempotencyPolicy",
    "INBOX_EXPECTED_STATUS_CLAIMED",
    "INBOX_TARGET_STATUS_COMMITTED",
    "InboxClaim",
    "InboxOutcomeKind",
    "MAX_ATOMIC_ASSET_REFERENCES",
    "MAX_ATOMIC_DURABLE_ACTIONS",
    "MAX_ATOMIC_JSON_PAYLOAD_BYTES",
    "MAX_ATOMIC_JSON_PAYLOAD_NODES",
    "MAX_ATOMIC_MANIFEST_BYTES",
    "MAX_ATOMIC_MANIFEST_NODES",
    "MAX_ATOMIC_OPERATIONS",
    "MAX_ATOMIC_OPERATION_TARGETS",
    "MAX_ATOMIC_OUTBOX_EVENTS",
    "MAX_ATOMIC_PIPELINE_INTENTS",
    "MAX_ATOMIC_TOTAL_AUXILIARY_BYTES",
    "MAX_BUSINESS_ID_BYTES",
    "MAX_PROJECT_ID_BYTES",
    "MAX_PROJECT_ID_CHARS",
    "MAX_SQLITE_INTEGER",
    "OUTBOX_STATUS_PENDING",
    "OutboxEventIntent",
    "PIPELINE_STATUS_QUEUED",
    "PipelineIntent",
    "PipelineIntentKind",
    "PipelineJobStatus",
    "PipelineReplayPolicy",
    "SQLITE_AUTHORITY",
    "SQLITE_AUTHORITY_STATE_ACTIVE",
    "freeze_json_payload",
    "verify_frozen_json_payload",
]
