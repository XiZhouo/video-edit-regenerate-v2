"""Plan-free durable audit for the current SQLite worker ledger.

The auditor never accepts a ``WorkerExecutionPlan``.  It pins one SQLite read
snapshot, rebuilds supported OUTBOX worker requests from durable rows and the
schema-v9 payload sidecars, reissues the pure worker/projection plans, and then
compares every digest and mirror with the database.  A second fresh snapshot is
used as a publish fence so a result is never assembled from two authorities.

The supported worker surface is intentionally narrow: OUTBOX CLAIM,
AUTHORIZE_EFFECT, COMPLETE, and RECONCILE.  The global root also reconstructs
multi-allocation batches, pipeline CREATE/TRANSITION control semantics and
ACTION/PIPELINE genesis.  Other otherwise legal worker operations or target
progress return UNKNOWN/unsupported, never CORRUPT merely because this reader
has not implemented their legacy-mirror reconstruction.  Schema-v10 cannot
independently prove the unique commit-time scheduler endpoint of a historical
zero-control commit; the auditor proves that the referenced endpoint is real
and closed, while a future authenticated checkpoint/global order must close
that remaining as-of boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import hmac
import sqlite3
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

from business_codec import (
    BusinessCodecError,
    decode_payload_bounded,
    encode_payload_bounded,
)
from business_atomic_write_contract import (
    ATOMIC_BUSINESS_WRITE_MANIFEST_SCHEMA,
    ATOMIC_BUSINESS_WRITE_PLAN_STATUS,
    ATOMIC_BUSINESS_WRITE_PROTOCOL,
    MAX_ATOMIC_BUSINESS_WRITE_MANIFEST_BYTES,
    MAX_ATOMIC_BUSINESS_WRITE_MANIFEST_NODES,
    AtomicBusinessWriteGenericReceiptEvidence,
)
from business_atomic_write_integrity import (
    ATOMIC_BUSINESS_WRITE_CONTROLS_HASH_DOMAIN,
    _atomic_receipt_digest,
    _digest as _atomic_digest,
    _manifest_digest as _atomic_manifest_digest,
)
from business_baseline_contract import digest_business_baseline_manifest
from business_transaction_contract import (
    AuthorityBinding,
    BusinessTransactionContractError,
    FrozenJsonPayload,
    MAX_ATOMIC_MANIFEST_BYTES,
    MAX_ATOMIC_MANIFEST_NODES,
    verify_frozen_json_payload,
)
from business_transaction_integrity import (
    _component_digest as _business_component_digest,
    _component_part as _business_component_part,
    _manifest_digest as _business_manifest_digest,
)
from sqlite_business_reader import (
    SQLiteBusinessReadError,
    _read_actions as _read_business_actions,
    _read_outbox as _read_business_outbox,
    _read_pipelines as _read_business_pipelines,
)
from sqlite_business_runtime import (
    BusinessRuntimeAccess,
    _validate_business_runtime_connection,
)
from sqlite_kernel import SQLiteKernelError, SqliteDatabase
from sqlite_production_authority import (
    SQLiteProductionAuthorityError,
    _binding_from_snapshot,
    _mount_evidence_from_row,
    _mounted_row,
)
from production_atomic_write_integrity import (
    PRODUCTION_ATOMIC_CONTROL_MEMBER_HASH_DOMAIN,
    PRODUCTION_ATOMIC_MANIFEST_HASH_DOMAIN,
    PRODUCTION_ATOMIC_RECEIPT_HASH_DOMAIN,
    PRODUCTION_ATOMIC_TARGET_GROUP_HASH_DOMAIN,
    PRODUCTION_ATOMIC_TARGET_MEMBER_HASH_DOMAIN,
    PRODUCTION_ATOMIC_TARGETS_HASH_DOMAIN,
    PRODUCTION_OUTBOX_ALLOCATION_TAIL_HASH_DOMAIN,
    PRODUCTION_WORKER_GENESIS_STATE_HASH_DOMAIN,
    _domain_digest as _production_domain_digest,
    _groups,
    _mount_manifest,
    _production_atomic_control_member_sha256,
    _production_outbox_allocation_tail,
    _production_targets_sha256,
    _production_worker_genesis_state_sha256,
)
from production_atomic_write_contract import (
    MAX_PRODUCTION_ATOMIC_MANIFEST_BYTES,
    MAX_PRODUCTION_ATOMIC_MANIFEST_NODES,
    PRODUCTION_ATOMIC_TARGET_GROUP_ORDER,
    PRODUCTION_ATOMIC_WRITE_MANIFEST_SCHEMA,
    PRODUCTION_ATOMIC_WRITE_PROTOCOL,
    PRODUCTION_ATOMIC_WRITE_STATUS,
    PRODUCTION_OUTBOX_IDEMPOTENCY_POLICY,
    PRODUCTION_OUTBOX_MAX_ATTEMPTS,
    PRODUCTION_OUTBOX_REPLAY_POLICY,
    ProductionWorkerTarget,
)
from worker_execution_contract import (
    EMPTY_WORKER_LEDGER_HEAD,
    PIPELINE_BUSINESS_CONTROL_MANIFEST_SCHEMA,
    PIPELINE_BUSINESS_CONTROL_PROTOCOL,
    PIPELINE_BUSINESS_CONTROL_REQUEST_CLASS,
    PIPELINE_BUSINESS_CONTROL_STATUS_PREPARED,
    WORKER_EXECUTION_REQUEST_CLASS,
    WORKER_EXECUTION_TRANSACTION_KIND,
    WORKER_EXECUTION_TRANSACTION_PREFIX,
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
    WorkerIdempotencyPolicy,
    WorkerLeaseToken,
    WorkerLedgerHead,
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
    PipelineBusinessControlIntentBinding,
    PipelineBusinessControlOperation,
    PipelineBusinessControlRequest,
)
from worker_execution_integrity import (
    MAX_WORKER_EXECUTION_MANIFEST_BYTES,
    MAX_WORKER_EXECUTION_MANIFEST_NODES,
    WORKER_EXECUTION_RECEIPT_HASH_DOMAIN,
    WorkerExecutionIntegrityError,
    _control_manifest_digest,
    _issue_pipeline_business_control_plan,
    _issue_worker_execution_plan,
)
from worker_result_projection_contract import (
    EMPTY_WORKER_PROOF_PROJECTION_SHA256,
)
from worker_result_projection_integrity import (
    WorkerResultProjectionIntegrityError,
    _issue_worker_result_projection_plan,
    _require_worker_result_projection_plan,
    _v8_worker_result_mirror_proof_sha256,
)


# A full audit is intentionally bounded.  A future checkpoint/accumulator can
# raise these ceilings without changing EXACT semantics.  Exceeding a local
# resource ceiling is UNKNOWN, not evidence that durable bytes are corrupt.
MAX_WORKER_DURABLE_AUDIT_ENTRIES = 100_000
MAX_WORKER_DURABLE_AUDIT_BYTES = 256 * 1024 * 1024

_ZERO_SHA256 = b"\0" * 32
_SUPPORTED_OPERATIONS = frozenset((
    WorkerExecutionOperation.CLAIM,
    WorkerExecutionOperation.AUTHORIZE_EFFECT,
    WorkerExecutionOperation.COMPLETE,
    WorkerExecutionOperation.RECONCILE,
))


class SQLiteWorkerDurableAuditStatus(str, Enum):
    EXACT = "exact"
    ABSENT = "absent"
    CONFLICT = "conflict"
    CORRUPT = "corrupt"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class WorkerDurableIdentity:
    """Small optional expectation; no source plan or payload is retained."""

    request_id: str
    operation: str
    target_kind: str
    target_id: str
    request_sha256: bytes
    after_state_sha256: bytes
    receipt_sha256: bytes
    projection_manifest_sha256: Optional[bytes]

    def __post_init__(self) -> None:
        _request_id(self.request_id)
        if type(self.operation) is not str or not self.operation:
            raise TypeError("operation must be a non-empty exact string")
        if type(self.target_kind) is not str or not self.target_kind:
            raise TypeError("target_kind must be a non-empty exact string")
        _request_id(self.target_id)
        for value in (
            self.request_sha256,
            self.after_state_sha256,
            self.receipt_sha256,
        ):
            if type(value) is not bytes or len(value) != 32:
                raise TypeError("identity digests must be exact 32-byte values")
        if self.projection_manifest_sha256 is not None and (
            type(self.projection_manifest_sha256) is not bytes
            or len(self.projection_manifest_sha256) != 32
        ):
            raise TypeError("projection digest must be None or 32 bytes")


@dataclass(frozen=True)
class SQLiteWorkerDurableAuditResult:
    status: SQLiteWorkerDurableAuditStatus
    request_id: str
    code: str
    identity: Optional[WorkerDurableIdentity] = None
    ledger_sequence: Optional[int] = None
    ledger_tail_sha256: Optional[bytes] = None

    def __post_init__(self) -> None:
        if type(self.status) is not SQLiteWorkerDurableAuditStatus:
            raise TypeError("status must be an exact audit status")
        _request_id(self.request_id)
        if type(self.code) is not str or not self.code:
            raise TypeError("code must be a non-empty exact string")
        if self.identity is not None and (
            type(self.identity) is not WorkerDurableIdentity
            or self.identity.request_id != self.request_id
        ):
            raise TypeError("identity does not match request_id")
        if (self.ledger_sequence is None) != (self.ledger_tail_sha256 is None):
            raise TypeError("ledger head must be wholly present or absent")
        if self.ledger_sequence is not None and (
            type(self.ledger_sequence) is not int
            or self.ledger_sequence < 0
            or type(self.ledger_tail_sha256) is not bytes
            or len(self.ledger_tail_sha256) != 32
        ):
            raise TypeError("ledger head is invalid")


class _AuditCorrupt(RuntimeError):
    pass


class _AuditUnsupported(RuntimeError):
    pass


class _AuditBudget(RuntimeError):
    pass


class _AuditConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class _SnapshotFence:
    repository_epoch: str
    cutover_id: str
    runtime_generation: str
    code_digest: bytes
    repository_revision: int
    control_revision: int
    ledger_state_revision: int
    ledger_sequence: int
    ledger_tail_sha256: bytes


@dataclass(frozen=True)
class _SnapshotAudit:
    result: SQLiteWorkerDurableAuditResult
    fence: _SnapshotFence


@dataclass(frozen=True)
class _OutboxReplay:
    after: Any
    state_sha256: bytes
    previous_state_sha256: bytes
    receipt: Any
    created_at: str
    available_at: str
    last_error: Optional[FrozenJsonPayload]


class _Budget:
    __slots__ = ("entries", "bytes", "_byte_owners")

    def __init__(self) -> None:
        self.entries = 0
        self.bytes = 0
        self._byte_owners = set()

    def add_entry(self) -> None:
        self.entries += 1
        if self.entries > MAX_WORKER_DURABLE_AUDIT_ENTRIES:
            raise _AuditBudget("worker_durable_audit_entry_budget_exceeded")

    def add_bytes(self, *values: Any, owner: Any = None) -> None:
        if owner is not None:
            if owner in self._byte_owners:
                return
            self._byte_owners.add(owner)
        for value in values:
            if type(value) is bytes:
                self.bytes += len(value)
        if self.bytes > MAX_WORKER_DURABLE_AUDIT_BYTES:
            raise _AuditBudget("worker_durable_audit_byte_budget_exceeded")


def _request_id(value: Any) -> str:
    if (
        type(value) is not str
        or len(value) != 32
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise TypeError("request_id must be 32 lowercase hexadecimal characters")
    return value


def _same(left: Any, right: Any) -> bool:
    if type(left) is bytes and type(right) is bytes:
        return hmac.compare_digest(left, right)
    return type(left) is type(right) and left == right


def _row_is(row: Any, expected: Iterable[Any]) -> bool:
    if row is None:
        return False
    values = tuple(row)
    expected_values = tuple(expected)
    return len(values) == len(expected_values) and all(
        _same(values[index], expected_values[index])
        for index in range(len(values))
    )


def _mapping(value: Any, keys: Iterable[str], code: str) -> Dict[str, Any]:
    expected = set(keys)
    if type(value) is not dict or set(value) != expected:
        raise _AuditCorrupt(code)
    return value


def _digest_hex(value: Any, code: str) -> bytes:
    if type(value) is not str or len(value) != 64:
        raise _AuditCorrupt(code)
    try:
        result = bytes.fromhex(value)
    except ValueError as exc:
        raise _AuditCorrupt(code) from exc
    if len(result) != 32:
        raise _AuditCorrupt(code)
    return result


def _canonical_manifest(
    value: Any,
    budget: _Budget,
    code: str,
    *,
    owner: Any = None,
) -> Dict[str, Any]:
    if type(value) is not bytes:
        raise _AuditCorrupt(code)
    budget.add_bytes(value, owner=owner)
    try:
        decoded = decode_payload_bounded(
            value,
            MAX_WORKER_EXECUTION_MANIFEST_BYTES,
            MAX_WORKER_EXECUTION_MANIFEST_NODES,
        )
        canonical = encode_payload_bounded(
            decoded,
            MAX_WORKER_EXECUTION_MANIFEST_BYTES,
            MAX_WORKER_EXECUTION_MANIFEST_NODES,
        )
    except BusinessCodecError as exc:
        raise _AuditCorrupt(code) from exc
    if canonical != value or type(decoded) is not dict:
        raise _AuditCorrupt(code)
    return decoded


def _payload(
    schema: Any,
    codec_id: Any,
    codec_version: Any,
    payload_bytes: Any,
    payload_sha256: Any,
    budget: _Budget,
    code: str,
    *,
    owner: Any = None,
) -> FrozenJsonPayload:
    try:
        value = FrozenJsonPayload(
            schema,
            codec_id,
            codec_version,
            payload_bytes,
            payload_sha256,
        )
        value = verify_frozen_json_payload(value)
    except (BusinessTransactionContractError, TypeError, ValueError) as exc:
        raise _AuditCorrupt(code) from exc
    budget.add_bytes(value.payload_bytes, owner=owner)
    return value


def _optional_payload(
    row: Mapping[str, Any],
    prefix: str,
    budget: _Budget,
    *,
    owner: Any = None,
) -> Optional[FrozenJsonPayload]:
    values = (
        row[prefix + "_schema"],
        row[prefix + "_codec_id"],
        row[prefix + "_codec_version"],
        row[prefix + "_payload_bytes"],
        row[prefix + "_payload_sha256"],
    )
    if all(value is None for value in values):
        return None
    if any(value is None for value in values):
        raise _AuditCorrupt("worker_durable_audit_partial_payload")
    return _payload(
        *values,
        budget,
        "worker_durable_audit_payload_corrupt",
        owner=owner,
    )


def _effect_binding(value: Any) -> Optional[WorkerEffectBinding]:
    if value is None:
        return None
    item = _mapping(
        value,
        (
            "attempt_id", "attempt_number", "effect_id", "effect_kind",
            "request_sha256", "idempotency_scope", "idempotency_key",
        ),
        "worker_durable_audit_effect_binding_corrupt",
    )
    try:
        return WorkerEffectBinding(
            item["attempt_id"],
            item["attempt_number"],
            item["effect_id"],
            WorkerEffectKind(item["effect_kind"]),
            _digest_hex(item["request_sha256"], "worker_durable_audit_effect_binding_corrupt"),
            item["idempotency_scope"],
            item["idempotency_key"],
        )
    except (TypeError, ValueError) as exc:
        raise _AuditCorrupt("worker_durable_audit_effect_binding_corrupt") from exc


def _before_state(value: Any) -> WorkerBeforeState:
    item = _mapping(
        value,
        (
            "status", "state_revision", "attempt_count", "last_attempt_id",
            "last_attempt_number", "current_attempt_id", "current_attempt_number",
            "lease_owner", "lease_fence", "lease_expires_at", "fence_high_water",
            "current_effect", "unresolved_effect", "next_attempt_at",
            "cancellation_pending",
        ),
        "worker_durable_audit_before_corrupt",
    )
    try:
        return WorkerBeforeState(
            WorkerExecutionStatus(item["status"]),
            item["state_revision"],
            item["attempt_count"],
            item["last_attempt_id"],
            item["last_attempt_number"],
            item["current_attempt_id"],
            item["current_attempt_number"],
            item["lease_owner"],
            item["lease_fence"],
            item["lease_expires_at"],
            item["fence_high_water"],
            _effect_binding(item["current_effect"]),
            _effect_binding(item["unresolved_effect"]),
            item["next_attempt_at"],
            item["cancellation_pending"],
        )
    except (TypeError, ValueError) as exc:
        raise _AuditCorrupt("worker_durable_audit_before_corrupt") from exc


def _state_image(value: Any) -> Tuple[Any, ...]:
    """The exact state carried from one receipt into the next request."""

    return (
        value.status,
        value.state_revision,
        value.attempt_count,
        value.last_attempt_id,
        value.last_attempt_number,
        value.current_attempt_id,
        value.current_attempt_number,
        value.lease_owner,
        value.lease_fence,
        value.lease_expires_at,
        value.fence_high_water,
        value.current_effect,
        value.unresolved_effect,
        value.next_attempt_at,
        value.cancellation_pending,
    )


def _outbox_genesis_before() -> WorkerBeforeState:
    return WorkerBeforeState(
        WorkerExecutionStatus.PENDING,
        1,
        0,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        0,
    )


def _authority(value: Any) -> AuthorityBinding:
    item = _mapping(
        value,
        (
            "authority", "authority_state", "epoch",
            "source_admission_revision", "revision", "control_revision",
            "cutover_id", "runtime_generation", "code_digest",
        ),
        "worker_durable_audit_authority_corrupt",
    )
    try:
        return AuthorityBinding(
            item["authority"],
            item["authority_state"],
            item["epoch"],
            item["source_admission_revision"],
            item["revision"],
            item["control_revision"],
            item["cutover_id"],
            item["runtime_generation"],
            _digest_hex(item["code_digest"], "worker_durable_audit_authority_corrupt"),
        )
    except (BusinessTransactionContractError, TypeError, ValueError) as exc:
        raise _AuditCorrupt("worker_durable_audit_authority_corrupt") from exc


def _outbox_order(value: Any) -> Optional[WorkerOutboxOrder]:
    if value is None:
        return None
    item = _mapping(
        value,
        ("sequence", "repository_revision", "ordinal"),
        "worker_durable_audit_dispatch_corrupt",
    )
    try:
        return WorkerOutboxOrder(
            item["sequence"], item["repository_revision"], item["ordinal"]
        )
    except (TypeError, ValueError) as exc:
        raise _AuditCorrupt("worker_durable_audit_dispatch_corrupt") from exc


def _dispatch_cursor(value: Any) -> WorkerOutboxDispatchCursor:
    item = _mapping(
        value,
        ("sequence", "repository_revision", "ordinal"),
        "worker_durable_audit_dispatch_corrupt",
    )
    try:
        return WorkerOutboxDispatchCursor(
            item["sequence"], item["repository_revision"], item["ordinal"]
        )
    except (TypeError, ValueError) as exc:
        raise _AuditCorrupt("worker_durable_audit_dispatch_corrupt") from exc


def _dispatch_state(value: Any) -> WorkerOutboxDispatchState:
    item = _mapping(
        value,
        ("control_revision", "published", "inflight"),
        "worker_durable_audit_dispatch_corrupt",
    )
    try:
        return WorkerOutboxDispatchState(
            item["control_revision"],
            _dispatch_cursor(item["published"]),
            _outbox_order(item["inflight"]),
        )
    except (TypeError, ValueError) as exc:
        raise _AuditCorrupt("worker_durable_audit_dispatch_corrupt") from exc


def _dispatch_fence(value: Any) -> Optional[WorkerOutboxDispatchFence]:
    if value is None:
        return None
    item = _mapping(
        value,
        ("before", "after"),
        "worker_durable_audit_dispatch_corrupt",
    )
    try:
        return WorkerOutboxDispatchFence(
            _dispatch_state(item["before"]),
            _dispatch_state(item["after"]),
        )
    except (TypeError, ValueError) as exc:
        raise _AuditCorrupt("worker_durable_audit_dispatch_corrupt") from exc


def _lease(value: Any) -> WorkerLeaseToken:
    item = _mapping(
        value,
        (
            "attempt_id", "attempt_number", "lease_owner",
            "execution_fence", "lease_expires_at",
        ),
        "worker_durable_audit_lease_corrupt",
    )
    try:
        return WorkerLeaseToken(
            item["attempt_id"], item["attempt_number"], item["lease_owner"],
            item["execution_fence"], item["lease_expires_at"],
        )
    except (TypeError, ValueError) as exc:
        raise _AuditCorrupt("worker_durable_audit_lease_corrupt") from exc


def _target(connection: Any, target_id: str, budget: _Budget) -> WorkerTarget:
    row = connection.execute(
        """
        SELECT target_kind,target_id,project_id,generation,document_id,
               origin_commit_id,payload_schema,payload_codec_id,
               payload_codec_version,payload_bytes,payload_sha256,
               replay_policy,idempotency_policy,idempotency_scope,
               idempotency_key,max_attempts,outbox_sequence,
               outbox_repository_revision,outbox_ordinal,pipeline_id,intent_type
        FROM worker_targets WHERE target_id=?
        """,
        (target_id,),
    ).fetchone()
    if row is None or len(row) != 21:
        raise _AuditCorrupt("worker_durable_audit_target_missing")
    payload = _payload(
        row[6], row[7], row[8], row[9], row[10], budget,
        "worker_durable_audit_target_payload_corrupt",
        owner=("worker_targets", target_id, "payload"),
    )
    try:
        order_values = row[16:19]
        if all(value is None for value in order_values):
            order = None
        elif any(value is None for value in order_values):
            raise ValueError("partial outbox order")
        else:
            order = WorkerOutboxOrder(*order_values)
        return WorkerTarget(
            WorkerTargetKind(row[0]), row[1], row[2], row[3], row[4], row[5],
            payload, WorkerReplayPolicy(row[11]), WorkerIdempotencyPolicy(row[12]),
            row[13], row[14], row[15],
            order, row[19], row[20],
        )
    except (TypeError, ValueError) as exc:
        raise _AuditCorrupt("worker_durable_audit_target_corrupt") from exc


def _target_business_origin_exact(connection: Any, target: WorkerTarget) -> Any:
    row = connection.execute(
        "SELECT project_id,generation,document_id,commit_kind,"
        "repository_revision,committed_at FROM business_commits WHERE commit_id=?",
        (target.origin_commit_id,),
    ).fetchone()
    if (
        row is None
        or len(row) != 6
        or row[0] != target.project_id
        or row[1] != target.generation
        or row[2] != target.document_id
        or row[3] != "change"
        or (
            target.outbox_order is not None
            and row[4] != target.outbox_order.repository_revision
        )
    ):
        raise _AuditCorrupt("worker_durable_audit_target_business_origin_corrupt")
    return row


def _target_origin_exact(connection: Any, target: WorkerTarget) -> str:
    order = target.outbox_order
    assert order is not None
    origin = _target_business_origin_exact(connection, target)
    row = connection.execute(
        """
        SELECT event.topic,event.partition_key,event.event_bytes,event.event_sha256,
               event.payload_schema,event.codec_id,event.codec_version,
               allocation.event_id,allocation.commit_id,allocation.sequence,
               allocation.repository_revision,allocation.ordinal,
               event.created_at,origin.committed_at,
               durable.outbox_topic,durable.outbox_partition_key,
               durable.genesis_status,durable.genesis_state_sha256,
               durable.source_ordinal,durable.created_at
        FROM event_outbox AS event
        JOIN outbox_allocations AS allocation ON allocation.event_id=event.event_id
        JOIN business_commits AS origin ON origin.commit_id=event.commit_id
        JOIN worker_targets AS durable ON durable.target_id=event.event_id
        WHERE event.event_id=?
        """,
        (target.target_id,),
    ).fetchone()
    try:
        genesis_sha256 = _production_worker_genesis_state_sha256(
            target,
            status=WorkerExecutionStatus.PENDING,
        )
    except Exception as exc:
        raise _AuditCorrupt(
            "worker_durable_audit_target_genesis_corrupt"
        ) from exc
    if (
        target.replay_policy is not PRODUCTION_OUTBOX_REPLAY_POLICY
        or target.idempotency_policy is not PRODUCTION_OUTBOX_IDEMPOTENCY_POLICY
        or target.idempotency_scope != "outbox/%s/%d" % (
            target.project_id,
            target.generation,
        )
        or target.idempotency_key != target.target_id
        or target.max_attempts != PRODUCTION_OUTBOX_MAX_ATTEMPTS
        or row is None
        or len(row) != 20
        or not _row_is(
        row[2:12],
        (
            target.payload.payload_bytes, target.payload.payload_sha256,
            target.payload.schema, target.payload.codec_id,
            target.payload.codec_version, target.target_id,
            target.origin_commit_id, order.sequence,
            order.repository_revision, order.ordinal,
        ),
        )
        or row[12] != row[13]
        or row[12] != origin[5]
        or not _row_is(row[:2], row[14:16])
        or not _row_is(
            row[16:20],
            ("pending", genesis_sha256, target.outbox_order.ordinal, row[12]),
        )
    ):
        raise _AuditCorrupt("worker_durable_audit_origin_mirror_corrupt")
    return row[12]


def _unconsumed_outbox_exact(
    connection: Any,
    target: WorkerTarget,
    created_at: str,
) -> None:
    try:
        genesis_sha256 = _production_worker_genesis_state_sha256(
            target,
            status=WorkerExecutionStatus.PENDING,
        )
    except Exception as exc:
        raise _AuditCorrupt(
            "worker_durable_audit_target_genesis_corrupt"
        ) from exc
    origin = connection.execute(
        "SELECT transaction_id FROM atomic_business_write_receipts "
        "WHERE commit_id=?",
        (target.origin_commit_id,),
    ).fetchone()
    current = connection.execute(
        """
        SELECT target_kind,status,state_revision,attempt_count,last_attempt_id,
               last_attempt_number,current_attempt_id,current_attempt_number,
               fence_high_water,cancellation_pending,next_attempt_at,lease_owner,
               lease_fence,lease_expires_at,current_effect_id,unresolved_effect_id,
               last_receipt_transaction_id,previous_state_sha256,state_sha256,
               updated_at
        FROM worker_target_current WHERE target_id=?
        """,
        (target.target_id,),
    ).fetchone()
    event = connection.execute(
        """
        SELECT status,state_revision,attempt_count,available_at,lease_owner,
               lease_fence,lease_expires_at,published_at,outcome_unknown_at,
               dead_at,last_error_bytes,last_error_sha256,created_at,updated_at
        FROM event_outbox WHERE event_id=?
        """,
        (target.target_id,),
    ).fetchone()
    if not _row_is(origin, (current[16] if current is not None else None,)) or not _row_is(
        current,
        (
            "outbox", "pending", 1, 0, None, None, None, None, 0, 0,
            None, None, None, None, None, None, origin[0], _ZERO_SHA256,
            genesis_sha256, created_at,
        ),
    ) or not _row_is(
        event,
        (
            "pending", 1, 0, created_at, None, None, None, None, None,
            None, None, None, created_at, created_at,
        ),
    ):
        raise _AuditCorrupt("worker_durable_audit_unconsumed_target_corrupt")


def _genesis_current_exact(
    connection: Any,
    target: WorkerTarget,
    *,
    status: WorkerExecutionStatus,
    state_sha256: bytes,
    receipt_transaction_id: str,
    created_at: str,
) -> None:
    row = connection.execute(
        """
        SELECT target_kind,status,state_revision,attempt_count,last_attempt_id,
               last_attempt_number,current_attempt_id,current_attempt_number,
               fence_high_water,cancellation_pending,next_attempt_at,lease_owner,
               lease_fence,lease_expires_at,current_effect_id,unresolved_effect_id,
               last_receipt_transaction_id,previous_state_sha256,state_sha256,
               updated_at
        FROM worker_target_current WHERE target_id=?
        """,
        (target.target_id,),
    ).fetchone()
    if not _row_is(row, (
        target.target_kind.value, status.value, 1, 0, None, None, None, None,
        0, 0, None, None, None, None, None, None, receipt_transaction_id,
        _ZERO_SHA256, state_sha256, created_at,
    )):
        raise _AuditCorrupt("worker_durable_audit_non_outbox_genesis_corrupt")


def _action_genesis_exact(
    connection: Any,
    target: WorkerTarget,
) -> None:
    _target_business_origin_exact(connection, target)
    row = connection.execute(
        """
        SELECT action.project_id,action.generation,action.document_id,
               action.origin_commit_id,action.commit_action_count,
               action.ordinal,action.action_kind,
               action.action_bytes,action.action_sha256,action.payload_schema,
               action.codec_id,action.codec_version,action.idempotency_scope,
               action.idempotency_key,action.replay_policy,
               action.idempotency_policy,action.max_attempts,action.status,
               action.state_revision,action.attempt_count,
               action.last_attempt_id,action.last_attempt_number,
               action.next_attempt_at,action.lease_owner,action.lease_fence,
               action.lease_expires_at,action.result_commit_id,
               action.current_attempt_id,action.current_attempt_started_at,
               action.created_at,action.updated_at,
               durable.action_type,durable.genesis_status,
               durable.genesis_state_sha256,durable.source_ordinal,
               durable.created_at,atomic.transaction_id,atomic.committed_at,
               business.durable_action_count
        FROM deferred_actions AS action
        JOIN worker_targets AS durable ON durable.target_id=action.action_id
        JOIN atomic_business_write_receipts AS atomic
          ON atomic.commit_id=action.origin_commit_id
        JOIN business_commits AS business
          ON business.commit_id=action.origin_commit_id
        WHERE action.action_id=?
        """,
        (target.target_id,),
    ).fetchone()
    try:
        genesis = _production_worker_genesis_state_sha256(
            target,
            status=WorkerExecutionStatus.QUEUED,
        )
    except Exception as exc:
        raise _AuditCorrupt(
            "worker_durable_audit_non_outbox_genesis_corrupt"
        ) from exc
    if row is None or len(row) != 39 or not _row_is(row, (
        target.project_id, target.generation, target.document_id,
        target.origin_commit_id, row[38], row[34], row[31],
        target.payload.payload_bytes, target.payload.payload_sha256,
        target.payload.schema, target.payload.codec_id,
        target.payload.codec_version, target.idempotency_scope,
        target.idempotency_key, target.replay_policy.value,
        target.idempotency_policy.value, target.max_attempts,
        "queued", 1, 0, None, None, row[29], None, None, None, None,
        None, None, row[29], row[29], row[6], "queued", genesis,
        row[5], row[29], row[36], row[29], row[4],
    )):
        raise _AuditCorrupt("worker_durable_audit_action_origin_corrupt")
    _genesis_current_exact(
        connection,
        target,
        status=WorkerExecutionStatus.QUEUED,
        state_sha256=genesis,
        receipt_transaction_id=row[36],
        created_at=row[29],
    )


def _pipeline_create_genesis_exact(
    connection: Any,
    target: WorkerTarget,
) -> None:
    _target_business_origin_exact(connection, target)
    row = connection.execute(
        """
        SELECT intent.origin_commit_id,intent.ordinal,intent.target_id,
               intent.intent_type,intent.idempotency_key,intent.payload_schema,
               intent.payload_codec_id,intent.payload_codec_version,
               intent.payload_bytes,intent.payload_sha256,intent.replay_policy,
               intent.idempotency_policy,intent.max_attempts,intent.operation,
               intent.before_status,intent.after_status,
               intent.before_state_revision,intent.after_state_revision,
               intent.committed_at,receipt.transaction_id,
               receipt.after_state_sha256,durable.genesis_status,
               durable.genesis_state_sha256,durable.source_ordinal,
               durable.created_at,job.pipeline_id,job.intent_type
        FROM pipeline_control_intents AS intent
        JOIN pipeline_control_receipts AS receipt
          ON receipt.intent_id=intent.intent_id
        JOIN worker_targets AS durable ON durable.target_id=intent.target_id
        JOIN pipeline_jobs AS job ON job.job_id=intent.target_id
        WHERE intent.target_id=? AND intent.operation='create'
        ORDER BY intent.ordinal
        """,
        (target.target_id,),
    ).fetchall()
    if len(row) != 1:
        raise _AuditCorrupt("worker_durable_audit_pipeline_origin_corrupt")
    value = row[0]
    if len(value) != 27 or not _row_is(value[:19], (
        target.origin_commit_id, value[23], target.target_id,
        target.intent_type, target.idempotency_key, target.payload.schema,
        target.payload.codec_id, target.payload.codec_version,
        target.payload.payload_bytes, target.payload.payload_sha256,
        target.replay_policy.value, target.idempotency_policy.value,
        target.max_attempts, "create", None, "queued", None, 1, value[24],
    )) or not _row_is(value[21:27], (
        "queued", value[20], value[1], value[18], target.pipeline_id,
        target.intent_type,
    )):
        raise _AuditCorrupt("worker_durable_audit_pipeline_origin_corrupt")
    _genesis_current_exact(
        connection,
        target,
        status=WorkerExecutionStatus.QUEUED,
        state_sha256=value[20],
        receipt_transaction_id=value[19],
        created_at=value[18],
    )


def _non_outbox_genesis_scope_exact(
    connection: Any,
    budget: _Budget,
) -> None:
    """Root ACTION/PIPELINE targets that have no execution/control progress.

    Legal progress is handled by the ledger replay below.  Until every worker
    operation for ACTION/PIPELINE is reconstructable, the presence of such a
    suffix remains UNKNOWN rather than being mislabeled corruption.
    """

    reverse = connection.execute(
        """
        SELECT
          EXISTS(SELECT 1 FROM worker_targets AS target
                 WHERE target.target_kind IN ('action','pipeline') AND NOT EXISTS(
                   SELECT 1 FROM worker_target_current AS current
                   WHERE current.target_id=target.target_id AND
                         current.target_kind=target.target_kind)),
          EXISTS(SELECT 1 FROM worker_target_current AS current
                 WHERE current.target_kind IN ('action','pipeline') AND NOT EXISTS(
                   SELECT 1 FROM worker_targets AS target
                   WHERE target.target_id=current.target_id AND
                         target.target_kind=current.target_kind)),
          EXISTS(SELECT 1 FROM deferred_actions AS action WHERE NOT EXISTS(
                   SELECT 1 FROM worker_targets AS target
                   WHERE target.target_id=action.action_id AND
                         target.target_kind='action')),
          EXISTS(SELECT 1 FROM pipeline_jobs AS job WHERE NOT EXISTS(
                   SELECT 1 FROM worker_targets AS target
                   WHERE target.target_id=job.job_id AND
                         target.target_kind='pipeline'))
        """
    ).fetchone()
    if reverse is None or len(reverse) != 4:
        raise _AuditCorrupt("worker_durable_audit_non_outbox_scope_unreadable")
    if any(bool(value) for value in reverse):
        raise _AuditCorrupt("worker_durable_audit_non_outbox_scope_not_closed")

    rows = connection.execute(
        "SELECT target_id,target_kind FROM worker_targets "
        "WHERE target_kind IN ('action','pipeline') "
        "ORDER BY target_kind,target_id"
    )
    for row in rows:
        budget.add_entry()
        target = _target(connection, row[0], budget)
        if target.target_kind.value != row[1]:
            raise _AuditCorrupt("worker_durable_audit_non_outbox_target_corrupt")
        worker_progress = connection.execute(
            "SELECT count(*) FROM worker_execution_receipts WHERE target_id=?",
            (target.target_id,),
        ).fetchone()
        if worker_progress is None or len(worker_progress) != 1:
            raise _AuditCorrupt("worker_durable_audit_non_outbox_scope_unreadable")
        if worker_progress[0] != 0:
            continue
        if target.target_kind is WorkerTargetKind.ACTION:
            _action_genesis_exact(connection, target)
            continue
        control_count = connection.execute(
            "SELECT count(*) FROM pipeline_control_receipts WHERE target_id=?",
            (target.target_id,),
        ).fetchone()
        if control_count is None or len(control_count) != 1 or control_count[0] < 1:
            raise _AuditCorrupt("worker_durable_audit_pipeline_origin_corrupt")
        if control_count[0] == 1:
            _pipeline_create_genesis_exact(connection, target)


def _allocation_chain_exact(
    connection: Any,
    budget: _Budget,
    target_cache: Dict[str, Tuple[WorkerTarget, str]],
    initial_updated_at: str,
) -> Dict[str, Tuple[Dict[str, Any], Dict[str, Any]]]:
    """Rebuild the complete OUTBOX allocation accumulator and batched head.

    Allocation ``sequence`` advances once per event, while the mutable head's
    ``state_revision`` advances once per production commit.  A single commit
    may therefore own a contiguous run of allocations with one repository
    revision and ordinals ``0..event_count-1``.  Treating every event as its
    own head revision was valid only for the original one-event vertical.
    """

    reverse_closure = connection.execute(
        """
        SELECT
          EXISTS(SELECT 1 FROM worker_targets AS target
                 WHERE target.target_kind='outbox' AND NOT EXISTS(
                   SELECT 1 FROM outbox_allocations AS allocation
                   WHERE allocation.event_id=target.target_id)),
          EXISTS(SELECT 1 FROM outbox_allocations AS allocation
                 WHERE NOT EXISTS(SELECT 1 FROM worker_targets AS target
                   WHERE target.target_kind='outbox' AND
                         target.target_id=allocation.event_id)),
          EXISTS(SELECT 1 FROM worker_target_current AS current
                 WHERE current.target_kind='outbox' AND NOT EXISTS(
                   SELECT 1 FROM outbox_allocations AS allocation
                   WHERE allocation.event_id=current.target_id)),
          EXISTS(SELECT 1 FROM outbox_allocations AS allocation
                 WHERE NOT EXISTS(SELECT 1 FROM worker_target_current AS current
                   WHERE current.target_kind='outbox' AND
                         current.target_id=allocation.event_id)),
          EXISTS(SELECT 1 FROM event_outbox AS event
                 WHERE NOT EXISTS(SELECT 1 FROM outbox_allocations AS allocation
                   WHERE allocation.event_id=event.event_id)),
          EXISTS(SELECT 1 FROM outbox_allocations AS allocation
                 WHERE NOT EXISTS(SELECT 1 FROM event_outbox AS event
                   WHERE event.event_id=allocation.event_id))
        """
    ).fetchone()
    if reverse_closure is None or len(reverse_closure) != 6:
        raise _AuditCorrupt("worker_durable_audit_outbox_scope_unreadable")
    if any(bool(value) for value in reverse_closure):
        raise _AuditCorrupt("worker_durable_audit_outbox_scope_not_closed")

    head = connection.execute(
        "SELECT state_revision,sequence,previous_tail_sha256,tail_sha256,"
        "updated_at FROM outbox_allocation_head WHERE singleton=1"
    ).fetchone()
    if head is None or len(head) != 5:
        raise _AuditCorrupt("worker_durable_audit_allocation_head_missing")
    rows = connection.execute(
        """
        SELECT allocation.sequence,allocation.event_id,allocation.commit_id,
               allocation.repository_revision,allocation.ordinal,
               allocation.previous_tail_sha256,
               allocation.allocation_tail_sha256,allocation.allocated_at,
               EXISTS(
                 SELECT 1
                 FROM event_outbox AS event
                 JOIN business_commits AS commit_row
                   ON commit_row.commit_id=event.commit_id AND
                      commit_row.repository_revision=event.repository_revision
                 JOIN atomic_business_write_receipts AS atomic
                   ON atomic.commit_id=event.commit_id
                 JOIN worker_targets AS target
                   ON target.target_id=event.event_id AND
                      target.target_kind='outbox'
                 WHERE event.event_id=allocation.event_id AND
                       event.commit_id=allocation.commit_id AND
                       event.repository_revision=allocation.repository_revision AND
                       event.ordinal=allocation.ordinal AND
                       event.commit_event_count=commit_row.outbox_event_count AND
                       (SELECT count(*) FROM outbox_allocations AS sibling
                        WHERE sibling.commit_id=allocation.commit_id)=
                           commit_row.outbox_event_count AND
                       (SELECT count(*) FROM worker_targets AS sibling_target
                        WHERE sibling_target.origin_commit_id=allocation.commit_id AND
                              sibling_target.target_kind='outbox')=
                           commit_row.outbox_event_count AND
                       atomic.target_count=(
                         SELECT count(*) FROM worker_targets AS atomic_target
                         WHERE atomic_target.origin_commit_id=allocation.commit_id
                       ) AND
                       target.origin_commit_id=allocation.commit_id AND
                       target.source_ordinal=allocation.ordinal AND
                       target.outbox_sequence=allocation.sequence AND
                       target.outbox_repository_revision=
                           allocation.repository_revision AND
                       target.outbox_ordinal=allocation.ordinal)
        FROM outbox_allocations AS allocation ORDER BY allocation.sequence
        """
    )
    previous = _ZERO_SHA256
    latest_batch_previous_tail = _ZERO_SHA256
    previous_batch_repository_revision = 0
    seen_commits = set()
    current_commit: Optional[str] = None
    current_repository_revision: Optional[int] = None
    current_event_count = 0
    current_ordinal = -1
    current_allocated_at: Optional[str] = None
    batch_count = 0
    count = 0
    last_allocated_at: Optional[str] = None
    current_head = {
        "state_revision": 1,
        "sequence": 0,
        "previous_tail_sha256": _ZERO_SHA256.hex(),
        "tail_sha256": _ZERO_SHA256.hex(),
        "updated_at": initial_updated_at,
    }
    batch_before = current_head
    batches: Dict[str, Tuple[Dict[str, Any], Dict[str, Any]]] = {}

    def close_batch(last_sequence: int, tail: bytes) -> None:
        nonlocal current_head
        assert current_commit is not None and current_allocated_at is not None
        after = {
            "state_revision": batch_before["state_revision"] + 1,
            "sequence": last_sequence,
            "previous_tail_sha256": batch_before["tail_sha256"],
            "tail_sha256": tail.hex(),
            "updated_at": current_allocated_at,
        }
        batches[current_commit] = (dict(batch_before), after)
        current_head = after

    for row in rows:
        budget.add_entry()
        count += 1
        new_batch = row[2] != current_commit
        if new_batch:
            if current_commit is not None and current_ordinal + 1 != current_event_count:
                raise _AuditCorrupt(
                    "worker_durable_audit_allocation_chain_corrupt"
                )
            if current_commit is not None:
                close_batch(count - 1, previous)
            if (
                row[2] in seen_commits
                or row[4] != 0
                or type(row[3]) is not int
                or row[3] <= previous_batch_repository_revision
            ):
                raise _AuditCorrupt(
                    "worker_durable_audit_allocation_chain_corrupt"
                )
            event_count_row = connection.execute(
                "SELECT outbox_event_count FROM business_commits WHERE commit_id=?",
                (row[2],),
            ).fetchone()
            if (
                event_count_row is None
                or len(event_count_row) != 1
                or type(event_count_row[0]) is not int
                or event_count_row[0] < 1
            ):
                raise _AuditCorrupt(
                    "worker_durable_audit_allocation_chain_corrupt"
                )
            seen_commits.add(row[2])
            current_commit = row[2]
            current_repository_revision = row[3]
            current_event_count = event_count_row[0]
            current_ordinal = 0
            current_allocated_at = row[7]
            previous_batch_repository_revision = row[3]
            latest_batch_previous_tail = row[5]
            batch_count += 1
            batch_before = current_head
        else:
            current_ordinal += 1
            if (
                row[3] != current_repository_revision
                or row[4] != current_ordinal
                or row[7] != current_allocated_at
            ):
                raise _AuditCorrupt(
                    "worker_durable_audit_allocation_chain_corrupt"
                )
        if (
            row[0] != count
            or row[4] != current_ordinal
            or row[8] != 1
            or not _same(row[5], previous)
        ):
            raise _AuditCorrupt("worker_durable_audit_allocation_chain_corrupt")
        try:
            digest = _production_outbox_allocation_tail(
                sequence=row[0],
                event_id=row[1],
                commit_id=row[2],
                repository_revision=row[3],
                ordinal=row[4],
                previous_tail_sha256=row[5],
            )
        except Exception as exc:
            raise _AuditCorrupt(
                "worker_durable_audit_allocation_chain_corrupt"
            ) from exc
        if not _same(digest, row[6]):
            raise _AuditCorrupt("worker_durable_audit_allocation_chain_corrupt")
        target = _target(connection, row[1], budget)
        created_at = _target_origin_exact(connection, target)
        target_cache[row[1]] = (target, created_at)
        receipt_exists = connection.execute(
            "SELECT EXISTS(SELECT 1 FROM worker_execution_receipts "
            "WHERE target_id=?)",
            (row[1],),
        ).fetchone()
        if not _row_is(receipt_exists, (0,)) and not _row_is(
            receipt_exists,
            (1,),
        ):
            raise _AuditCorrupt("worker_durable_audit_target_receipt_unreadable")
        if receipt_exists[0] == 0:
            _unconsumed_outbox_exact(connection, target, created_at)
        previous = row[6]
        last_allocated_at = row[7]
    if current_commit is not None and current_ordinal + 1 != current_event_count:
        raise _AuditCorrupt("worker_durable_audit_allocation_chain_corrupt")
    if current_commit is not None:
        close_batch(count, previous)
    if (
        head[0] != batch_count + 1
        or head[1] != count
        or not _same(head[2], latest_batch_previous_tail)
        or not _same(head[3], previous)
        or (last_allocated_at is not None and head[4] != last_allocated_at)
        or (count == 0 and not _same(head[2], _ZERO_SHA256))
    ):
        raise _AuditCorrupt("worker_durable_audit_allocation_head_corrupt")
    if not _row_is(head, (
        current_head["state_revision"],
        current_head["sequence"],
        bytes.fromhex(current_head["previous_tail_sha256"]),
        bytes.fromhex(current_head["tail_sha256"]),
        current_head["updated_at"],
    )):
        raise _AuditCorrupt("worker_durable_audit_allocation_head_corrupt")
    return batches


def _provider_payload(
    row: Optional[Mapping[str, Any]],
    budget: _Budget,
    transaction_id: str,
) -> Optional[FrozenJsonPayload]:
    if row is None:
        return None
    return _optional_payload(
        row,
        "provider_receipt",
        budget,
        owner=("worker_proof_payload_projections", transaction_id, "provider_receipt"),
    )


def _effect_link(value: Any, provider: Optional[FrozenJsonPayload]) -> Optional[WorkerEffectLink]:
    if value is None:
        if provider is not None:
            raise _AuditCorrupt("worker_durable_audit_effect_link_corrupt")
        return None
    item = _mapping(
        value,
        (
            "attempt_id", "attempt_number", "effect_id", "effect_kind",
            "request_sha256", "result_commit_id", "provider_receipt",
        ),
        "worker_durable_audit_effect_link_corrupt",
    )
    try:
        return WorkerEffectLink(
            item["attempt_id"], item["attempt_number"], item["effect_id"],
            WorkerEffectKind(item["effect_kind"]),
            _digest_hex(item["request_sha256"], "worker_durable_audit_effect_link_corrupt"),
            item["result_commit_id"], provider,
        )
    except (TypeError, ValueError) as exc:
        raise _AuditCorrupt("worker_durable_audit_effect_link_corrupt") from exc


def _durable_result(
    value: Any,
    payload: Optional[FrozenJsonPayload],
) -> Optional[WorkerDurableResultProof]:
    if value is None:
        if payload is not None:
            raise _AuditCorrupt("worker_durable_audit_durable_result_corrupt")
        return None
    item = _mapping(
        value,
        ("result_id", "result", "recorded_at", "effect"),
        "worker_durable_audit_durable_result_corrupt",
    )
    if payload is None:
        raise _AuditCorrupt("worker_durable_audit_durable_result_corrupt")
    try:
        return WorkerDurableResultProof(
            item["result_id"], payload, item["recorded_at"],
            _effect_binding(item["effect"]),
        )
    except (TypeError, ValueError) as exc:
        raise _AuditCorrupt("worker_durable_audit_durable_result_corrupt") from exc


def _current_row_exact(
    connection: Any,
    target_id: str,
    replay: _OutboxReplay,
) -> None:
    after = replay.after
    receipt = replay.receipt
    row = connection.execute(
        """
        SELECT target_kind,status,state_revision,attempt_count,last_attempt_id,
               last_attempt_number,current_attempt_id,current_attempt_number,
               fence_high_water,cancellation_pending,next_attempt_at,lease_owner,
               lease_fence,lease_expires_at,current_effect_id,unresolved_effect_id,
               last_receipt_transaction_id,previous_state_sha256,state_sha256,
               updated_at
        FROM worker_target_current WHERE target_id=?
        """,
        (target_id,),
    ).fetchone()
    if not _row_is(row, (
        "outbox", after.status.value, after.state_revision, after.attempt_count,
        after.last_attempt_id, after.last_attempt_number,
        after.current_attempt_id, after.current_attempt_number,
        after.fence_high_water, int(after.cancellation_pending),
        after.next_attempt_at, after.lease_owner, after.lease_fence,
        after.lease_expires_at,
        None if after.current_effect is None else after.current_effect.effect_id,
        None if after.unresolved_effect is None else after.unresolved_effect.effect_id,
        receipt.receipt_parent.transaction_id, replay.previous_state_sha256,
        receipt.after_state_sha256,
        receipt.occurred_at,
    )):
        raise _AuditCorrupt("worker_durable_audit_current_corrupt")


def _legacy_outbox_exact(
    connection: Any,
    target_id: str,
    replay: _OutboxReplay,
) -> None:
    after = replay.after
    receipt = replay.receipt
    row = connection.execute(
        """
        SELECT status,state_revision,attempt_count,available_at,lease_owner,
               lease_fence,lease_expires_at,published_at,outcome_unknown_at,
               dead_at,last_error_bytes,last_error_sha256,created_at,updated_at
        FROM event_outbox WHERE event_id=?
        """,
        (target_id,),
    ).fetchone()
    if row is None or len(row) != 14:
        raise _AuditCorrupt("worker_durable_audit_legacy_mirror_missing")
    expected_published = (
        receipt.occurred_at
        if after.status is WorkerExecutionStatus.PUBLISHED else None
    )
    expected_unknown = (
        receipt.occurred_at
        if after.status is WorkerExecutionStatus.OUTCOME_UNKNOWN else None
    )
    expected_dead = (
        receipt.occurred_at if after.status is WorkerExecutionStatus.DEAD else None
    )
    error = replay.last_error
    if not _row_is(row, (
        after.status.value, after.state_revision, after.attempt_count,
        replay.available_at, after.lease_owner, after.lease_fence,
        after.lease_expires_at, expected_published, expected_unknown,
        expected_dead,
        None if error is None else error.payload_bytes,
        None if error is None else error.payload_sha256,
        replay.created_at, receipt.occurred_at,
    )):
        raise _AuditCorrupt("worker_durable_audit_legacy_mirror_corrupt")


def _dispatch_head(connection: Any) -> Tuple[int, WorkerOutboxDispatchState, bytes, bytes, str]:
    row = connection.execute(
        """
        SELECT state_revision,published_sequence,published_repository_revision,
               published_ordinal,inflight_sequence,inflight_repository_revision,
               inflight_ordinal,previous_head_sha256,head_sha256,updated_at
        FROM outbox_dispatch_head WHERE singleton=1
        """
    ).fetchone()
    if row is None or len(row) != 10:
        raise _AuditCorrupt("worker_durable_audit_dispatch_head_missing")
    try:
        published = (
            WorkerOutboxDispatchCursor(0, 0, -1)
            if row[1] == 0
            else WorkerOutboxDispatchCursor(row[1], row[2], row[3])
        )
        inflight = (
            None if row[4] is None
            else WorkerOutboxOrder(row[4], row[5], row[6])
        )
        state = WorkerOutboxDispatchState(row[0], published, inflight)
    except (TypeError, ValueError) as exc:
        raise _AuditCorrupt("worker_durable_audit_dispatch_head_corrupt") from exc
    return row[0], state, row[7], row[8], row[9]


def _authority_descends(profile: Any, mounted: Any, authority: AuthorityBinding) -> bool:
    evidence = mounted.evidence
    return bool(
        authority.authority == profile.authority == evidence.authority
        and authority.authority_state
        == profile.authority_state.value == evidence.authority_state
        and authority.authority_epoch
        == profile.repository_epoch == evidence.repository_epoch
        and authority.cutover_id == profile.cutover_id == evidence.cutover_id
        and authority.source_admission_revision
        == profile.source_admission_revision
        == evidence.source_admission_revision
        and profile.repository_revision == evidence.repository_revision
        and profile.state_revision == evidence.control_revision
        and profile.repository_revision >= authority.authority_revision
        and profile.state_revision >= authority.control_revision
        and authority.runtime_generation
        == profile.runtime_generation == evidence.runtime_generation
        and _same(authority.code_digest, profile.code_digest)
        and _same(authority.code_digest, evidence.code_digest)
    )


def _specialised_row(connection: Any, transaction_id: str) -> Any:
    return connection.execute(
        """
        SELECT request_id,transaction_id,entry_kind,receipt_kind,committed_at,
               operation,target_kind,target_id,before_status,after_status,
               before_state_revision,after_state_revision,sequence,
               previous_ledger_tail_sha256,ledger_tail_sha256,manifest_bytes,
               request_sha256,after_state_sha256,receipt_sha256
        FROM worker_execution_receipts WHERE transaction_id=?
        """,
        (transaction_id,),
    ).fetchone()


def _generic_parent_exact(connection: Any, transaction_id: str, kind: str, committed_at: str) -> None:
    rows = connection.execute(
        "SELECT kind,committed_at FROM transaction_receipts WHERE transaction_id=?",
        (transaction_id,),
    ).fetchall()
    if len(rows) != 1 or not _row_is(rows[0], (kind, committed_at)):
        raise _AuditCorrupt("worker_durable_audit_parent_receipt_corrupt")


def _effect_sidecar(connection: Any, transaction_id: str) -> Any:
    return connection.execute(
        "SELECT * FROM worker_effect_request_projections "
        "WHERE authorization_transaction_id=?",
        (transaction_id,),
    ).fetchone()


def _result_sidecar(connection: Any, transaction_id: str) -> Any:
    return connection.execute(
        "SELECT * FROM worker_result_payload_projections "
        "WHERE receipt_transaction_id=?",
        (transaction_id,),
    ).fetchone()


def _proof_sidecar(connection: Any, transaction_id: str) -> Any:
    return connection.execute(
        "SELECT * FROM worker_proof_payload_projections "
        "WHERE receipt_transaction_id=?",
        (transaction_id,),
    ).fetchone()


def _projection_manifest_sha256(plan: Any) -> bytes:
    projection = _require_worker_result_projection_plan(
        _issue_worker_result_projection_plan(plan)
    )
    return projection.closure.manifest_sha256


def _identity(plan: Any) -> WorkerDurableIdentity:
    projection_sha = (
        None
        if plan.operation is WorkerExecutionOperation.CLAIM
        else _projection_manifest_sha256(plan)
    )
    return WorkerDurableIdentity(
        plan.closure.request_transaction_id,
        plan.operation.value,
        plan.target.target_kind.value,
        plan.target.target_id,
        plan.closure.request_sha256,
        plan.closure.after_state_sha256,
        plan.expected_receipt.receipt_sha256,
        projection_sha,
    )


def _compare_effect_projection(connection: Any, plan: Any, budget: _Budget) -> None:
    transaction_id = plan.expected_receipt.receipt_parent.transaction_id
    row = _effect_sidecar(connection, transaction_id)
    if row is None:
        raise _AuditCorrupt("worker_durable_audit_effect_sidecar_missing")
    request_payload = _payload(
        row["request_schema"], row["request_codec_id"],
        row["request_codec_version"], row["request_payload_bytes"],
        row["request_payload_sha256"], budget,
        "worker_durable_audit_effect_payload_corrupt",
        owner=("worker_effect_request_projections", transaction_id, "request"),
    )
    request = plan.request.effect
    if request_payload != request.request:
        raise _AuditCorrupt("worker_durable_audit_effect_payload_corrupt")
    projection = _require_worker_result_projection_plan(
        _issue_worker_result_projection_plan(plan)
    )
    expected = projection.closure.effect_request
    assert expected is not None
    effect = expected.binding.effect
    assert effect is not None
    if not _row_is(row, (
        effect.effect_id, plan.target.target_id, plan.target.target_kind.value,
        effect.attempt_id, effect.attempt_number, effect.effect_kind.value,
        transaction_id, projection.protocol,
        "ze.worker-effect-request-projection.v1",
        expected.idempotency_scope, expected.idempotency_key,
        request_payload.schema, request_payload.codec_id,
        request_payload.codec_version, request_payload.payload_bytes,
        request_payload.payload_sha256, expected.projection_manifest_bytes,
        expected.projection_sha256, projection.closure.manifest_bytes,
        projection.closure.manifest_sha256, plan.expected_receipt.occurred_at,
    )):
        raise _AuditCorrupt("worker_durable_audit_effect_sidecar_corrupt")
    mirror = connection.execute(
        "SELECT effect_id,target_id,attempt_id,effect_kind,"
        "authorization_transaction_id,idempotency_key,authorized_at "
        "FROM worker_effects WHERE authorization_transaction_id=?",
        (transaction_id,),
    ).fetchone()
    if not _row_is(mirror, (
        effect.effect_id, plan.target.target_id, effect.attempt_id,
        effect.effect_kind.value, transaction_id, effect.idempotency_key,
        plan.expected_receipt.occurred_at,
    )):
        raise _AuditCorrupt("worker_durable_audit_effect_mirror_corrupt")


def _compare_result_projection(connection: Any, plan: Any, budget: _Budget) -> None:
    transaction_id = plan.expected_receipt.receipt_parent.transaction_id
    row = _result_sidecar(connection, transaction_id)
    if row is None:
        raise _AuditCorrupt("worker_durable_audit_result_sidecar_missing")
    result_payload = _optional_payload(
        row,
        "result",
        budget,
        owner=("worker_result_payload_projections", transaction_id, "result"),
    )
    error_payload = _optional_payload(
        row,
        "error",
        budget,
        owner=("worker_result_payload_projections", transaction_id, "error"),
    )
    projection = _require_worker_result_projection_plan(
        _issue_worker_result_projection_plan(plan)
    )
    expected = projection.closure.result
    assert expected is not None
    if result_payload != expected.result_payload or error_payload != expected.error_payload:
        raise _AuditCorrupt("worker_durable_audit_result_payload_corrupt")
    effect = expected.binding.effect
    if not _row_is(row, (
        expected.result_id, plan.target.target_id, plan.target.target_kind.value,
        expected.binding.attempt_id, expected.binding.attempt_number,
        None if effect is None else effect.effect_id,
        transaction_id, plan.operation.value, projection.protocol,
        "ze.worker-result-payload-projection.v1", expected.after_status.value,
        expected.committed_at,
        None if result_payload is None else result_payload.schema,
        None if result_payload is None else result_payload.codec_id,
        None if result_payload is None else result_payload.codec_version,
        None if result_payload is None else result_payload.payload_bytes,
        None if result_payload is None else result_payload.payload_sha256,
        None if error_payload is None else error_payload.schema,
        None if error_payload is None else error_payload.codec_id,
        None if error_payload is None else error_payload.codec_version,
        None if error_payload is None else error_payload.payload_bytes,
        None if error_payload is None else error_payload.payload_sha256,
        expected.projection_manifest_bytes, expected.result_sha256,
        expected.proof_sha256, projection.closure.manifest_bytes,
        projection.closure.manifest_sha256,
    )):
        raise _AuditCorrupt("worker_durable_audit_result_sidecar_corrupt")
    mirror = connection.execute(
        "SELECT result_id,target_id,attempt_id,effect_id,terminal_status,"
        "result_sha256,proof_sha256,receipt_transaction_id,completed_at "
        "FROM worker_results WHERE receipt_transaction_id=?",
        (transaction_id,),
    ).fetchone()
    if not _row_is(mirror, (
        expected.result_id, plan.target.target_id, expected.binding.attempt_id,
        None if effect is None else effect.effect_id, expected.after_status.value,
        expected.result_sha256, _v8_worker_result_mirror_proof_sha256(projection),
        transaction_id, expected.committed_at,
    )):
        raise _AuditCorrupt("worker_durable_audit_result_mirror_corrupt")

    proof_row = _proof_sidecar(connection, transaction_id)
    expected_proof = projection.closure.proof
    if expected_proof is None:
        if proof_row is not None:
            raise _AuditCorrupt("worker_durable_audit_extra_proof_sidecar")
        if plan.operation is WorkerExecutionOperation.RECONCILE:
            raise _AuditCorrupt("worker_durable_audit_reconcile_proof_missing")
        return
    if proof_row is None:
        raise _AuditCorrupt("worker_durable_audit_proof_sidecar_missing")
    evidence = _optional_payload(
        proof_row,
        "evidence",
        budget,
        owner=("worker_proof_payload_projections", transaction_id, "evidence"),
    )
    provider = _optional_payload(
        proof_row,
        "provider_receipt",
        budget,
        owner=("worker_proof_payload_projections", transaction_id, "provider_receipt"),
    )
    durable = _optional_payload(
        proof_row,
        "durable_result",
        budget,
        owner=("worker_proof_payload_projections", transaction_id, "durable_result"),
    )
    if (
        evidence != expected_proof.evidence_payload
        or provider != expected_proof.provider_receipt_payload
        or durable != expected_proof.durable_result_payload
    ):
        raise _AuditCorrupt("worker_durable_audit_proof_payload_corrupt")
    if not _row_is(proof_row, (
        expected_proof.proof_id, expected_proof.result_id,
        plan.target.target_id, plan.target.target_kind.value,
        expected_proof.binding.attempt_id, expected_proof.binding.attempt_number,
        None if effect is None else effect.effect_id,
        transaction_id, plan.operation.value, projection.protocol,
        "ze.worker-proof-payload-projection.v1",
        expected_proof.proof_class.value, expected_proof.proof_kind,
        expected_proof.observed_at,
        None if evidence is None else evidence.schema,
        None if evidence is None else evidence.codec_id,
        None if evidence is None else evidence.codec_version,
        None if evidence is None else evidence.payload_bytes,
        None if evidence is None else evidence.payload_sha256,
        None if provider is None else provider.schema,
        None if provider is None else provider.codec_id,
        None if provider is None else provider.codec_version,
        None if provider is None else provider.payload_bytes,
        None if provider is None else provider.payload_sha256,
        expected_proof.durable_result_id,
        None if durable is None else durable.schema,
        None if durable is None else durable.codec_id,
        None if durable is None else durable.codec_version,
        None if durable is None else durable.payload_bytes,
        None if durable is None else durable.payload_sha256,
        expected_proof.durable_result_recorded_at,
        expected_proof.result_commit_id,
        expected_proof.projection_manifest_bytes, expected_proof.proof_sha256,
        projection.closure.manifest_bytes, projection.closure.manifest_sha256,
    )):
        raise _AuditCorrupt("worker_durable_audit_proof_sidecar_corrupt")
    reconcile = connection.execute(
        "SELECT proof_id,target_id,effect_id,result_id,proof_kind,proof_bytes,"
        "proof_sha256,receipt_transaction_id,observed_at "
        "FROM worker_reconcile_proofs WHERE receipt_transaction_id=?",
        (transaction_id,),
    ).fetchone()
    if plan.operation is WorkerExecutionOperation.RECONCILE:
        expected_result_id = (
            expected_proof.result_id
            if expected_proof.proof_kind == WorkerReconcileProofKind.DURABLE_RESULT.value
            else None
        )
        if not _row_is(reconcile, (
            expected_proof.proof_id, plan.target.target_id,
            effect.effect_id if effect is not None else None,
            expected_result_id, expected_proof.proof_kind,
            expected_proof.projection_manifest_bytes, expected_proof.proof_sha256,
            transaction_id, expected_proof.observed_at,
        )):
            raise _AuditCorrupt("worker_durable_audit_reconcile_mirror_corrupt")
    elif reconcile is not None:
        raise _AuditCorrupt("worker_durable_audit_extra_reconcile_mirror")


def _raw_projection_rows(
    connection: Any,
    transaction_id: str,
) -> Tuple[Any, Any, Any]:
    return (
        _effect_sidecar(connection, transaction_id),
        _result_sidecar(connection, transaction_id),
        _proof_sidecar(connection, transaction_id),
    )


def _rebuild_request(
    connection: Any,
    receipt_row: Any,
    before_ledger: WorkerLedgerHead,
    profile: Any,
    mounted: Any,
    budget: _Budget,
    target_cache: Dict[str, Tuple[WorkerTarget, str]],
) -> Any:
    manifest = _canonical_manifest(
        receipt_row[15],
        budget,
        "worker_durable_audit_manifest_corrupt",
        owner=("worker_execution_receipts", receipt_row[1], "manifest"),
    )
    root = _mapping(
        manifest,
        ("schema", "protocol", "status", "operation", "header", "transition", "after", "request"),
        "worker_durable_audit_manifest_corrupt",
    )
    header_value = _mapping(
        root["header"],
        ("request_transaction_id", "occurred_at", "authority", "target", "before"),
        "worker_durable_audit_manifest_corrupt",
    )
    try:
        operation = WorkerExecutionOperation(root["operation"])
    except (TypeError, ValueError) as exc:
        raise _AuditCorrupt(
            "worker_durable_audit_operation_corrupt"
        ) from exc
    if operation not in _SUPPORTED_OPERATIONS:
        raise _AuditUnsupported("worker_durable_audit_operation_unsupported")
    cached_target = target_cache.get(receipt_row[7])
    if cached_target is None:
        target = _target(connection, receipt_row[7], budget)
        origin_created_at = _target_origin_exact(connection, target)
        target_cache[receipt_row[7]] = (target, origin_created_at)
    else:
        target, origin_created_at = cached_target
    before = _before_state(header_value["before"])
    authority = _authority(header_value["authority"])
    if not _authority_descends(profile, mounted, authority):
        raise _AuditConflict("worker_durable_audit_authority_conflict")
    header = WorkerRequestHeader(
        header_value["request_transaction_id"], authority, target, before,
        header_value["occurred_at"],
    )
    detail = root["request"]
    transaction_id = receipt_row[1]
    effect_row, result_row, proof_row = _raw_projection_rows(
        connection, transaction_id
    )
    try:
        if operation is WorkerExecutionOperation.CLAIM:
            if any(value is not None for value in (effect_row, result_row, proof_row)):
                raise _AuditCorrupt("worker_durable_audit_claim_projection_unexpected")
            item = _mapping(
                detail,
                (
                    "attempt_id", "attempt_number", "previous_attempt_id",
                    "previous_attempt_number", "lease_owner", "execution_fence",
                    "started_at", "lease_expires_at", "dispatch_fence",
                ),
                "worker_durable_audit_claim_corrupt",
            )
            request = WorkerClaimRequest(
                header,
                WorkerAttemptStart(
                    item["attempt_id"], item["attempt_number"],
                    item["previous_attempt_id"], item["previous_attempt_number"],
                    item["lease_owner"], item["execution_fence"],
                    item["started_at"], item["lease_expires_at"],
                ),
                _dispatch_fence(item["dispatch_fence"]),
            )
        elif operation is WorkerExecutionOperation.AUTHORIZE_EFFECT:
            if effect_row is None or result_row is not None or proof_row is not None:
                raise _AuditCorrupt("worker_durable_audit_effect_projection_shape")
            item = _mapping(
                detail, ("lease", "effect"),
                "worker_durable_audit_effect_request_corrupt",
            )
            effect_value = _mapping(
                item["effect"],
                (
                    "effect_id", "effect_kind", "idempotency_scope",
                    "idempotency_key", "request", "authorized_at",
                ),
                "worker_durable_audit_effect_request_corrupt",
            )
            raw = _payload(
                effect_row["request_schema"], effect_row["request_codec_id"],
                effect_row["request_codec_version"], effect_row["request_payload_bytes"],
                effect_row["request_payload_sha256"], budget,
                "worker_durable_audit_effect_payload_corrupt",
                owner=(
                    "worker_effect_request_projections",
                    transaction_id,
                    "request",
                ),
            )
            request = WorkerAuthorizeEffectRequest(
                header, _lease(item["lease"]),
                WorkerEffectAuthorization(
                    effect_value["effect_id"],
                    WorkerEffectKind(effect_value["effect_kind"]),
                    effect_value["idempotency_scope"], effect_value["idempotency_key"],
                    raw, effect_value["authorized_at"],
                ),
            )
        elif operation is WorkerExecutionOperation.COMPLETE:
            if result_row is None:
                raise _AuditCorrupt("worker_durable_audit_result_sidecar_missing")
            item = _mapping(
                detail,
                (
                    "lease", "target_status", "finished_at", "result", "error",
                    "effect_link", "durable_result", "terminal_proof",
                    "next_attempt_at", "dispatch_fence",
                ),
                "worker_durable_audit_complete_corrupt",
            )
            result_payload = _optional_payload(
                result_row,
                "result",
                budget,
                owner=(
                    "worker_result_payload_projections",
                    transaction_id,
                    "result",
                ),
            )
            error_payload = _optional_payload(
                result_row,
                "error",
                budget,
                owner=(
                    "worker_result_payload_projections",
                    transaction_id,
                    "error",
                ),
            )
            provider = _provider_payload(proof_row, budget, transaction_id)
            durable_payload = (
                None if proof_row is None
                else _optional_payload(
                    proof_row,
                    "durable_result",
                    budget,
                    owner=(
                        "worker_proof_payload_projections",
                        transaction_id,
                        "durable_result",
                    ),
                )
            )
            terminal_value = None
            if item["terminal_proof"] is not None:
                terminal_value = _mapping(
                    item["terminal_proof"],
                    ("proof_kind", "effect", "evidence", "effect_link"),
                    "worker_durable_audit_terminal_proof_corrupt",
                )
            top_link_value = item["effect_link"]
            terminal_link_value = (
                None if terminal_value is None
                else terminal_value["effect_link"]
            )

            def owns_provider(link_value: Any) -> bool:
                return bool(
                    type(link_value) is dict
                    and link_value.get("provider_receipt") is not None
                )

            top_owns_provider = owns_provider(top_link_value)
            terminal_owns_provider = owns_provider(terminal_link_value)
            if (
                int(top_owns_provider) + int(terminal_owns_provider) > 1
                or (provider is not None)
                != (top_owns_provider or terminal_owns_provider)
            ):
                raise _AuditCorrupt(
                    "worker_durable_audit_provider_owner_corrupt"
                )
            link = _effect_link(
                top_link_value,
                provider if top_owns_provider else None,
            )
            durable = _durable_result(item["durable_result"], durable_payload)
            terminal = None
            if terminal_value is not None:
                evidence = (
                    None if proof_row is None
                    else _optional_payload(
                        proof_row,
                        "evidence",
                        budget,
                        owner=(
                            "worker_proof_payload_projections",
                            transaction_id,
                            "evidence",
                        ),
                    )
                )
                if evidence is None:
                    raise _AuditCorrupt("worker_durable_audit_terminal_proof_corrupt")
                terminal = WorkerTerminalProof(
                    WorkerTerminalProofKind(terminal_value["proof_kind"]),
                    _effect_binding(terminal_value["effect"]),
                    evidence,
                    _effect_link(
                        terminal_link_value,
                        provider if terminal_owns_provider else None,
                    ),
                )
                # The link, when terminal-owned, must not also appear at the
                # top-level COMPLETE field.
                link = None
            request = WorkerCompleteRequest(
                header, _lease(item["lease"]),
                WorkerExecutionStatus(item["target_status"]),
                item["finished_at"], result_payload, error_payload, link,
                durable, terminal, item["next_attempt_at"],
                _dispatch_fence(item["dispatch_fence"]),
            )
        else:
            if result_row is None or proof_row is None:
                raise _AuditCorrupt("worker_durable_audit_reconcile_projection_missing")
            item = _mapping(
                detail,
                (
                    "attempt_id", "attempt_number", "execution_fence",
                    "target_status", "proof", "reconciled_at",
                    "next_attempt_at", "dispatch_fence",
                ),
                "worker_durable_audit_reconcile_corrupt",
            )
            proof_value = _mapping(
                item["proof"],
                (
                    "proof_kind", "unresolved_effect", "evidence",
                    "effect_link", "durable_result",
                ),
                "worker_durable_audit_reconcile_corrupt",
            )
            evidence = _optional_payload(
                proof_row,
                "evidence",
                budget,
                owner=(
                    "worker_proof_payload_projections",
                    transaction_id,
                    "evidence",
                ),
            )
            if evidence is None:
                raise _AuditCorrupt("worker_durable_audit_reconcile_evidence_missing")
            provider = _optional_payload(
                proof_row,
                "provider_receipt",
                budget,
                owner=(
                    "worker_proof_payload_projections",
                    transaction_id,
                    "provider_receipt",
                ),
            )
            durable_payload = _optional_payload(
                proof_row,
                "durable_result",
                budget,
                owner=(
                    "worker_proof_payload_projections",
                    transaction_id,
                    "durable_result",
                ),
            )
            proof = WorkerReconcileProof(
                WorkerReconcileProofKind(proof_value["proof_kind"]),
                _effect_binding(proof_value["unresolved_effect"]), evidence,
                _effect_link(proof_value["effect_link"], provider),
                _durable_result(proof_value["durable_result"], durable_payload),
            )
            request = WorkerReconcileRequest(
                header, item["attempt_id"], item["attempt_number"],
                item["execution_fence"],
                WorkerExecutionStatus(item["target_status"]), proof,
                item["reconciled_at"], item["next_attempt_at"],
                _dispatch_fence(item["dispatch_fence"]),
            )
        plan = _issue_worker_execution_plan(request, before_ledger=before_ledger)
    except _AuditCorrupt:
        raise
    except (BusinessTransactionContractError, TypeError, ValueError, WorkerExecutionIntegrityError) as exc:
        raise _AuditCorrupt("worker_durable_audit_plan_rebuild_failed") from exc
    if plan.closure.manifest_bytes != receipt_row[15]:
        raise _AuditCorrupt("worker_durable_audit_manifest_changed")
    return plan, origin_created_at


def _worker_receipt_exact(entry: Any, row: Any, plan: Any) -> None:
    receipt = plan.expected_receipt
    parent = receipt.receipt_parent
    expected_row = (
        receipt.request_transaction_id, parent.transaction_id,
        "worker_execution", parent.kind, parent.committed_at,
        receipt.operation.value, receipt.target_kind.value, receipt.target_id,
        receipt.before_status.value, receipt.after_status.value,
        receipt.before_state_revision, receipt.after_state_revision,
        receipt.sequence, receipt.previous_ledger_tail_sha256,
        receipt.ledger_tail_sha256, plan.closure.manifest_bytes,
        receipt.request_sha256, receipt.after_state_sha256,
        receipt.receipt_sha256,
    )
    if not _row_is(row, expected_row):
        raise _AuditCorrupt("worker_durable_audit_specialised_receipt_corrupt")
    if not _row_is(entry, (
        receipt.sequence, "worker_execution", parent.transaction_id,
        parent.kind, parent.committed_at, receipt.previous_ledger_tail_sha256,
        receipt.ledger_tail_sha256, receipt.request_sha256,
        receipt.after_state_sha256, receipt.receipt_sha256,
        receipt.occurred_at,
    )):
        raise _AuditCorrupt("worker_durable_audit_ledger_entry_corrupt")


def _claim_attempt_exact(connection: Any, plan: Any) -> None:
    attempt = plan.request.attempt
    row = connection.execute(
        "SELECT attempt_id,target_id,attempt_number,previous_attempt_id,"
        "execution_fence,owner_id,started_at,receipt_transaction_id "
        "FROM worker_attempts WHERE receipt_transaction_id=?",
        (plan.expected_receipt.receipt_parent.transaction_id,),
    ).fetchone()
    if not _row_is(row, (
        attempt.attempt_id, plan.target.target_id, attempt.attempt_number,
        attempt.previous_attempt_id, attempt.execution_fence,
        attempt.lease_owner, attempt.started_at,
        plan.expected_receipt.receipt_parent.transaction_id,
    )):
        raise _AuditCorrupt("worker_durable_audit_attempt_mirror_corrupt")


def _pipeline_membership_exact(
    connection: Any,
    entry: Any,
    before_ledger: WorkerLedgerHead,
    profile: Any,
    mounted: Any,
    budget: _Budget,
) -> Tuple[str, Any, int]:
    row = connection.execute(
        """
        SELECT intent_id,transaction_id,entry_kind,receipt_kind,committed_at,
               operation,intent_type,target_id,before_status,after_status,
               before_state_revision,after_state_revision,sequence,
               previous_ledger_tail_sha256,ledger_tail_sha256,manifest_bytes,
               request_sha256,after_state_sha256,receipt_sha256
        FROM pipeline_control_receipts WHERE transaction_id=?
        """,
        (entry[2],),
    ).fetchone()
    if row is None or len(row) != 19:
        raise _AuditCorrupt("worker_durable_audit_pipeline_membership_missing")
    _generic_parent_exact(connection, row[1], row[3], row[4])
    manifest = _canonical_manifest(
        row[15],
        budget,
        "worker_durable_audit_pipeline_manifest_corrupt",
        owner=("pipeline_control_receipts", row[1], "manifest"),
    )
    if not _same(_control_manifest_digest(row[15]), row[16]):
        raise _AuditCorrupt("worker_durable_audit_pipeline_manifest_corrupt")
    intent = connection.execute(
        """
        SELECT origin_commit_id,ordinal,target_id,target_kind,operation,
               intent_type,idempotency_key,payload_schema,payload_codec_id,
               payload_codec_version,payload_bytes,payload_sha256,
               replay_policy,idempotency_policy,max_attempts,before_status,
               after_status,before_state_revision,after_state_revision,
               request_sha256,committed_at
        FROM pipeline_control_intents WHERE intent_id=?
        """,
        (row[0],),
    ).fetchone()
    if intent is None or len(intent) != 21:
        raise _AuditCorrupt("worker_durable_audit_pipeline_membership_missing")
    target = _target(connection, intent[2], budget)
    _target_business_origin_exact(connection, target)
    payload = _payload(
        intent[7], intent[8], intent[9], intent[10], intent[11], budget,
        "worker_durable_audit_pipeline_payload_corrupt",
        owner=("pipeline_control_intents", row[0], "payload"),
    )
    try:
        authority = _authority(manifest["authority"])
        operation = PipelineBusinessControlOperation(intent[4])
        before = None if operation is PipelineBusinessControlOperation.CREATE else (
            _before_state(manifest["before"])
        )
        binding = PipelineBusinessControlIntentBinding(
            origin_commit_id=intent[0],
            intent_type=intent[5],
            idempotency_key=intent[6],
            payload=payload,
            replay_policy=WorkerReplayPolicy(intent[12]),
            idempotency_policy=WorkerIdempotencyPolicy(intent[13]),
            max_attempts=intent[14],
        )
        request = PipelineBusinessControlRequest(
            request_transaction_id=row[0],
            authority=authority,
            target=target,
            control_intent=binding,
            operation=operation,
            before=before,
            target_status=WorkerExecutionStatus(intent[16]),
            occurred_at=intent[20],
        )
        plan = _issue_pipeline_business_control_plan(
            request,
            before_ledger=before_ledger,
        )
    except (
        KeyError,
        TypeError,
        ValueError,
        WorkerExecutionIntegrityError,
    ) as exc:
        raise _AuditCorrupt(
            "worker_durable_audit_pipeline_plan_rebuild_failed"
        ) from exc
    if not _authority_descends(profile, mounted, authority):
        raise _AuditConflict("worker_durable_audit_authority_conflict")
    closure = plan.closure
    receipt = plan.expected_receipt
    parent = receipt.receipt_parent
    if (
        manifest.get("schema") != PIPELINE_BUSINESS_CONTROL_MANIFEST_SCHEMA
        or manifest.get("protocol") != PIPELINE_BUSINESS_CONTROL_PROTOCOL
        or manifest.get("status") != PIPELINE_BUSINESS_CONTROL_STATUS_PREPARED
        or closure.manifest_bytes != row[15]
        or intent[3] != WorkerTargetKind.PIPELINE.value
        or intent[4] != row[5]
        or intent[5] != row[6]
        or intent[2] != row[7]
        or intent[15] != row[8]
        or intent[16] != row[9]
        or intent[17] != row[10]
        or intent[18] != row[11]
        or intent[19] != row[16]
        or intent[20] != row[4]
        or parent.transaction_id != row[1]
        or parent.kind != row[3]
        or parent.committed_at != row[4]
        or receipt.operation.value != row[5]
        or receipt.target_id != row[7]
        or (None if receipt.before_status is None else receipt.before_status.value)
           != row[8]
        or receipt.after_status.value != row[9]
        or receipt.before_state_revision != row[10]
        or receipt.after_state_revision != row[11]
        or receipt.sequence != row[12]
        or not _same(receipt.previous_ledger_tail_sha256, row[13])
        or not _same(receipt.ledger_tail_sha256, row[14])
        or not _same(receipt.request_sha256, row[16])
        or not _same(receipt.after_state_sha256, row[17])
        or not _same(receipt.receipt_sha256, row[18])
        or not _row_is(entry, (
            receipt.sequence, "pipeline_control", parent.transaction_id,
            parent.kind, parent.committed_at,
            receipt.previous_ledger_tail_sha256, receipt.ledger_tail_sha256,
            receipt.request_sha256, receipt.after_state_sha256,
            receipt.receipt_sha256, receipt.occurred_at,
        ))
    ):
        raise _AuditCorrupt("worker_durable_audit_pipeline_receipt_corrupt")

    legacy = connection.execute(
        """
        SELECT project_id,generation,document_id,origin_commit_id,ordinal,
               job_id,intent_type,idempotency_key,intent_kind,
               before_state_revision,after_state_revision,before_status,
               after_status,intent_bytes,intent_sha256,payload_schema,
               codec_id,codec_version,replay_policy,max_attempts,committed_at
        FROM pipeline_job_transitions WHERE intent_id=?
        """,
        (row[0],),
    ).fetchone()
    if not _row_is(legacy, (
        target.project_id, target.generation, target.document_id, intent[0],
        intent[1], target.target_id, intent[5], intent[6],
        "create" if operation is PipelineBusinessControlOperation.CREATE
        else "transition",
        intent[17], intent[18], intent[15], intent[16], intent[10], intent[11],
        intent[7], intent[8], intent[9], intent[12], intent[14], intent[20],
    )):
        raise _AuditCorrupt("worker_durable_audit_pipeline_legacy_corrupt")

    member = connection.execute(
        """
        SELECT commit_id,ordinal,intent_id,control_transaction_id,sequence,
               ledger_tail_sha256,binding_sha256
        FROM atomic_business_control_members WHERE intent_id=?
        """,
        (row[0],),
    ).fetchone()
    try:
        member_sha256 = _production_atomic_control_member_sha256(
            commit_id=intent[0], ordinal=intent[1], intent_id=row[0],
            control_transaction_id=parent.transaction_id,
            sequence=receipt.sequence,
            ledger_tail_sha256=receipt.ledger_tail_sha256,
        )
    except Exception as exc:
        raise _AuditCorrupt(
            "worker_durable_audit_pipeline_member_corrupt"
        ) from exc
    if not _row_is(member, (
        intent[0], intent[1], row[0], parent.transaction_id, receipt.sequence,
        receipt.ledger_tail_sha256, member_sha256,
    )):
        raise _AuditCorrupt("worker_durable_audit_pipeline_member_corrupt")
    return intent[0], plan, intent[1]


def _pipeline_current_exact(
    connection: Any,
    plan: Any,
    previous_state_sha256: bytes,
) -> None:
    after = plan.closure.after_state
    receipt = plan.expected_receipt
    target = plan.target
    row = connection.execute(
        """
        SELECT target_kind,status,state_revision,attempt_count,last_attempt_id,
               last_attempt_number,current_attempt_id,current_attempt_number,
               fence_high_water,cancellation_pending,next_attempt_at,lease_owner,
               lease_fence,lease_expires_at,current_effect_id,unresolved_effect_id,
               last_receipt_transaction_id,previous_state_sha256,state_sha256,
               updated_at
        FROM worker_target_current WHERE target_id=?
        """,
        (target.target_id,),
    ).fetchone()
    if not _row_is(row, (
        WorkerTargetKind.PIPELINE.value, after.status.value,
        after.state_revision, after.attempt_count, after.last_attempt_id,
        after.last_attempt_number, after.current_attempt_id,
        after.current_attempt_number, after.fence_high_water,
        int(after.cancellation_pending), after.next_attempt_at,
        after.lease_owner, after.lease_fence, after.lease_expires_at,
        None if after.current_effect is None else after.current_effect.effect_id,
        None if after.unresolved_effect is None else after.unresolved_effect.effect_id,
        receipt.receipt_parent.transaction_id, previous_state_sha256,
        receipt.after_state_sha256, receipt.occurred_at,
    )):
        raise _AuditCorrupt("worker_durable_audit_pipeline_current_corrupt")
    job = connection.execute(
        """
        SELECT last_intent_id,head_commit_id,status,state_revision,attempt_count,
               last_attempt_id,last_attempt_number,lease_owner,lease_fence,
               lease_expires_at,result_commit_id,current_attempt_id,
               current_attempt_started_at,updated_at
        FROM pipeline_jobs WHERE job_id=?
        """,
        (target.target_id,),
    ).fetchone()
    if not _row_is(job, (
        plan.request.request_transaction_id,
        plan.control_intent.origin_commit_id,
        after.status.value,
        after.state_revision,
        after.attempt_count,
        after.last_attempt_id,
        after.last_attempt_number,
        after.lease_owner,
        after.lease_fence,
        after.lease_expires_at,
        None,
        after.current_attempt_id,
        None,
        receipt.occurred_at,
    )):
        raise _AuditCorrupt("worker_durable_audit_pipeline_legacy_current_corrupt")


def _identity_absent(connection: Any, request_id: str) -> bool:
    transaction_id = WORKER_EXECUTION_TRANSACTION_PREFIX + request_id
    row = connection.execute(
        """
        SELECT
          EXISTS(SELECT 1 FROM transaction_receipts WHERE transaction_id=?),
          EXISTS(SELECT 1 FROM worker_execution_receipts
                 WHERE request_id=? OR transaction_id=?),
          EXISTS(SELECT 1 FROM worker_ledger_entries WHERE transaction_id=?),
          EXISTS(SELECT 1 FROM worker_effects
                 WHERE authorization_transaction_id=?),
          EXISTS(SELECT 1 FROM worker_results WHERE receipt_transaction_id=?),
          EXISTS(SELECT 1 FROM worker_reconcile_proofs
                 WHERE receipt_transaction_id=?),
          EXISTS(SELECT 1 FROM worker_effect_request_projections
                 WHERE authorization_transaction_id=?),
          EXISTS(SELECT 1 FROM worker_result_payload_projections
                 WHERE receipt_transaction_id=?),
          EXISTS(SELECT 1 FROM worker_proof_payload_projections
                 WHERE receipt_transaction_id=?)
        """,
        (
            transaction_id, request_id, transaction_id, transaction_id,
            transaction_id, transaction_id, transaction_id, transaction_id,
            transaction_id, transaction_id,
        ),
    ).fetchone()
    if row is None or len(row) != 9:
        raise _AuditCorrupt("worker_durable_audit_identity_unreadable")
    return not any(bool(value) for value in row)


def _worker_parent_namespace_exact(connection: Any) -> None:
    row = connection.execute(
        """
        SELECT count(*) FROM transaction_receipts AS parent
        WHERE (
          parent.transaction_id GLOB '_worker-execution:*' OR
          parent.kind='_worker_execution_v3'
        ) AND NOT EXISTS(
          SELECT 1 FROM worker_execution_receipts AS receipt
          WHERE receipt.transaction_id=parent.transaction_id AND
                receipt.receipt_kind=parent.kind AND
                receipt.committed_at=parent.committed_at)
        """
    ).fetchone()
    if not _row_is(row, (0,)):
        raise _AuditCorrupt("worker_durable_audit_orphan_parent_receipt")


def _pipeline_control_scope_exact(connection: Any) -> None:
    row = connection.execute(
        """
        SELECT
          EXISTS(SELECT 1 FROM pipeline_control_intents AS intent
                 WHERE NOT EXISTS(SELECT 1 FROM pipeline_control_receipts AS receipt
                   WHERE receipt.intent_id=intent.intent_id) OR NOT EXISTS(
                   SELECT 1 FROM atomic_business_control_members AS member
                   WHERE member.intent_id=intent.intent_id)),
          EXISTS(SELECT 1 FROM pipeline_control_receipts AS receipt
                 WHERE NOT EXISTS(SELECT 1 FROM pipeline_control_intents AS intent
                   WHERE intent.intent_id=receipt.intent_id) OR NOT EXISTS(
                   SELECT 1 FROM atomic_business_control_members AS member
                   WHERE member.intent_id=receipt.intent_id)),
          EXISTS(SELECT 1 FROM atomic_business_control_members AS member
                 WHERE NOT EXISTS(SELECT 1 FROM pipeline_control_intents AS intent
                   WHERE intent.intent_id=member.intent_id AND
                         intent.origin_commit_id=member.commit_id AND
                         intent.ordinal=member.ordinal) OR NOT EXISTS(
                   SELECT 1 FROM pipeline_control_receipts AS receipt
                   WHERE receipt.intent_id=member.intent_id AND
                         receipt.transaction_id=member.control_transaction_id AND
                         receipt.sequence=member.sequence AND
                         receipt.ledger_tail_sha256=member.ledger_tail_sha256)),
          EXISTS(
            SELECT 1 FROM atomic_business_write_receipts AS atomic
            JOIN business_commits AS business ON business.commit_id=atomic.commit_id
            WHERE atomic.control_count != business.pipeline_intent_count OR
                  atomic.control_count != (SELECT count(*)
                    FROM pipeline_control_intents AS intent
                    WHERE intent.origin_commit_id=atomic.commit_id) OR
                  atomic.control_count != (SELECT count(*)
                    FROM atomic_business_control_members AS member
                    WHERE member.commit_id=atomic.commit_id)
          )
        """
    ).fetchone()
    if row is None or len(row) != 4:
        raise _AuditCorrupt("worker_durable_audit_pipeline_scope_unreadable")
    if any(bool(value) for value in row):
        raise _AuditCorrupt("worker_durable_audit_pipeline_scope_not_closed")


def _pipeline_intents_sha256(
    connection: Any,
    commit_id: str,
    expected_count: int,
    budget: _Budget,
) -> bytes:
    rows = connection.execute(
        """
        SELECT transition.ordinal,transition.intent_id,transition.job_id,
               job.pipeline_id,transition.intent_type,transition.intent_kind,
               transition.before_status,transition.after_status,
               transition.before_state_revision,transition.idempotency_key,
               transition.replay_policy,transition.max_attempts,
               transition.intent_bytes,transition.intent_sha256,
               transition.payload_schema,transition.codec_id,
               transition.codec_version
        FROM pipeline_job_transitions AS transition
        JOIN pipeline_jobs AS job ON job.job_id=transition.job_id
        WHERE transition.origin_commit_id=? ORDER BY transition.ordinal
        """,
        (commit_id,),
    ).fetchall()
    if len(rows) != expected_count:
        raise _AuditCorrupt("worker_durable_audit_atomic_pipeline_root_corrupt")
    parts = []
    for ordinal, row in enumerate(rows):
        if row[0] != ordinal:
            raise _AuditCorrupt(
                "worker_durable_audit_atomic_pipeline_root_corrupt"
            )
        payload = _payload(
            row[14], row[15], row[16], row[12], row[13], budget,
            "worker_durable_audit_atomic_pipeline_root_corrupt",
            owner=("pipeline_job_transitions", row[1], "payload"),
        )
        try:
            parts.append(_business_component_part({
                "ordinal": ordinal,
                "intent_id": row[1],
                "job_id": row[2],
                "pipeline_id": row[3],
                "intent_type": row[4],
                "intent_kind": row[5],
                "expected_status": row[6],
                "target_status": row[7],
                "expected_state_revision": row[8],
                "idempotency_key": row[9],
                "replay_policy": row[10],
                "max_attempts": row[11],
                "status": "queued",
                "payload_sha256": payload.payload_sha256.hex(),
            }))
        except Exception as exc:
            raise _AuditCorrupt(
                "worker_durable_audit_atomic_pipeline_root_corrupt"
            ) from exc
    try:
        return _business_component_digest(
            b"ze.atomic-business-commit.pipelines.v1",
            tuple(parts),
        )
    except Exception as exc:
        raise _AuditCorrupt(
            "worker_durable_audit_atomic_pipeline_root_corrupt"
        ) from exc


def _production_targets_root(
    connection: Any,
    commit_id: str,
    budget: _Budget,
) -> Tuple[int, bytes]:
    rows = connection.execute(
        """
        SELECT target_id,source_ordinal,genesis_status,genesis_state_sha256,
               action_type,outbox_topic,outbox_partition_key
        FROM worker_targets WHERE origin_commit_id=?
        ORDER BY CASE target_kind WHEN 'pipeline' THEN 0 WHEN 'action' THEN 1
                 ELSE 2 END,source_ordinal
        """,
        (commit_id,),
    ).fetchall()
    wrapped = []
    for row in rows:
        try:
            wrapped.append(ProductionWorkerTarget(
                source_ordinal=row[1],
                target=_target(connection, row[0], budget),
                genesis_status=WorkerExecutionStatus(row[2]),
                genesis_state_sha256=row[3],
                action_type=row[4],
                outbox_topic=row[5],
                outbox_partition_key=row[6],
            ))
        except (TypeError, ValueError) as exc:
            raise _AuditCorrupt(
                "worker_durable_audit_atomic_target_root_corrupt"
            ) from exc
    try:
        groups = _groups(tuple(wrapped))
        root = _production_targets_sha256(groups)
    except Exception as exc:
        raise _AuditCorrupt(
            "worker_durable_audit_atomic_target_root_corrupt"
        ) from exc
    return len(wrapped), root


def _production_manifest(
    raw: Any,
    budget: _Budget,
    commit_id: str,
) -> Dict[str, Any]:
    if type(raw) is not bytes:
        raise _AuditCorrupt("worker_durable_audit_atomic_outer_root_corrupt")
    budget.add_bytes(
        raw,
        owner=("atomic_business_write_receipts", commit_id, "production_manifest"),
    )
    try:
        decoded = decode_payload_bounded(
            raw,
            MAX_PRODUCTION_ATOMIC_MANIFEST_BYTES,
            MAX_PRODUCTION_ATOMIC_MANIFEST_NODES,
        )
        canonical = encode_payload_bounded(
            decoded,
            MAX_PRODUCTION_ATOMIC_MANIFEST_BYTES,
            MAX_PRODUCTION_ATOMIC_MANIFEST_NODES,
        )
    except BusinessCodecError as exc:
        raise _AuditCorrupt(
            "worker_durable_audit_atomic_outer_root_corrupt"
        ) from exc
    if type(decoded) is not dict or canonical != raw:
        raise _AuditCorrupt("worker_durable_audit_atomic_outer_root_corrupt")
    return decoded


def _atomic_business_roots_exact(
    connection: Any,
    controls_by_commit: Dict[str, list[Tuple[int, Any]]],
    budget: _Budget,
    profile: Any,
    mounted: Any,
    allocation_batches: Dict[str, Tuple[Dict[str, Any], Dict[str, Any]]],
    worker_heads: Dict[int, Dict[str, Any]],
    dispatch_heads: Dict[int, Dict[str, Any]],
) -> None:
    rows = connection.execute(
        """
        SELECT atomic.commit_id,atomic.transaction_id,atomic.receipt_kind,
               atomic.committed_at,atomic.atomic_manifest_sha256,
               atomic.pipeline_intents_sha256,atomic.controls_sha256,
               atomic.control_count,atomic.before_worker_sequence,
               atomic.before_worker_tail_sha256,atomic.after_worker_sequence,
               atomic.after_worker_tail_sha256,atomic.manifest_bytes,
               atomic.manifest_sha256,atomic.receipt_sha256,
               atomic.production_manifest_bytes,
               atomic.production_manifest_sha256,atomic.target_count,
               atomic.targets_sha256,atomic.production_receipt_sha256,
               business.manifest_sha256,business.pipeline_intent_count,
               business.transaction_id,business.receipt_kind,
               business.committed_at,business.durable_action_count,
               business.outbox_event_count,business.manifest_bytes,
               business.repository_revision,business.commit_kind
        FROM atomic_business_write_receipts AS atomic
        JOIN business_commits AS business ON business.commit_id=atomic.commit_id
        ORDER BY business.repository_revision
        """
    )
    observed_commits = set()
    allocation_current = {
        "state_revision": 1,
        "sequence": 0,
        "previous_tail_sha256": _ZERO_SHA256.hex(),
        "tail_sha256": _ZERO_SHA256.hex(),
        "updated_at": mounted.mounted_at,
    }
    mount_origin = _mount_evidence_from_row(_mounted_row(connection)).runtime
    if profile.state_revision != mount_origin.state_revision:
        raise _AuditUnsupported(
            "worker_durable_audit_control_history_unsupported"
        )
    for row in rows:
        budget.add_entry()
        commit_id = row[0]
        observed_commits.add(commit_id)
        raw_business_manifest = row[27]
        if type(raw_business_manifest) is not bytes:
            raise _AuditCorrupt(
                "worker_durable_audit_business_manifest_corrupt"
            )
        budget.add_bytes(
            raw_business_manifest,
            owner=("business_commits", commit_id, "manifest"),
        )
        try:
            business_manifest = decode_payload_bounded(
                raw_business_manifest,
                MAX_ATOMIC_MANIFEST_BYTES,
                MAX_ATOMIC_MANIFEST_NODES,
            )
            canonical_business_manifest = encode_payload_bounded(
                business_manifest,
                MAX_ATOMIC_MANIFEST_BYTES,
                MAX_ATOMIC_MANIFEST_NODES,
            )
            business_manifest_sha256 = (
                digest_business_baseline_manifest(raw_business_manifest)
                if row[29] == "baseline"
                else _business_manifest_digest(raw_business_manifest)
            )
        except (BusinessCodecError, TypeError, ValueError) as exc:
            raise _AuditCorrupt(
                "worker_durable_audit_business_manifest_corrupt"
            ) from exc
        if (
            type(business_manifest) is not dict
            or canonical_business_manifest != raw_business_manifest
            or not _same(business_manifest_sha256, row[20])
        ):
            raise _AuditCorrupt(
                "worker_durable_audit_business_manifest_corrupt"
            )
        business_commit = connection.execute(
            "SELECT * FROM business_commits WHERE commit_id=?",
            (commit_id,),
        ).fetchone()
        if business_commit is None:
            raise _AuditCorrupt(
                "worker_durable_audit_business_manifest_corrupt"
            )
        if row[29] == "change":
            try:
                outbox, _outbox_bytes = _read_business_outbox(
                    connection,
                    business_commit,
                )
                actions, _action_bytes = _read_business_actions(
                    connection,
                    business_commit,
                )
                pipelines, _pipeline_bytes = _read_business_pipelines(
                    connection,
                    business_commit,
                )
            except SQLiteBusinessReadError as exc:
                raise _AuditCorrupt(
                    "worker_durable_audit_business_manifest_corrupt"
                ) from exc
            if (
                business_manifest.get("outbox") != list(outbox)
                or business_manifest.get("durable_actions") != list(actions)
                or business_manifest.get("pipeline_intents") != list(pipelines)
            ):
                raise _AuditCorrupt(
                    "worker_durable_audit_business_manifest_corrupt"
                )
        worker_before = worker_heads.get(row[8])
        worker_after = worker_heads.get(row[10])
        dispatch_before = dispatch_heads.get(row[8])
        if (
            worker_before is None
            or worker_after is None
            or dispatch_before is None
            or worker_before["sequence"] != row[8]
            or not _same(bytes.fromhex(worker_before["tail_sha256"]), row[9])
            or worker_after["sequence"] != row[10]
            or not _same(bytes.fromhex(worker_after["tail_sha256"]), row[11])
        ):
            raise _AuditCorrupt(
                "worker_durable_audit_atomic_scheduler_root_corrupt"
            )

        allocation_pair = allocation_batches.get(commit_id)
        if allocation_pair is None:
            allocation_before = dict(allocation_current)
            allocation_after = dict(allocation_current)
        else:
            allocation_before, allocation_after = allocation_pair
            if allocation_before != allocation_current:
                raise _AuditCorrupt(
                    "worker_durable_audit_atomic_allocation_root_corrupt"
                )
            allocation_current = dict(allocation_after)
        allocation_count = (
            allocation_after["sequence"] - allocation_before["sequence"]
        )
        if allocation_count != row[26]:
            raise _AuditCorrupt(
                "worker_durable_audit_atomic_allocation_root_corrupt"
            )
        controls = sorted(
            controls_by_commit.get(commit_id, ()),
            key=lambda value: value[0],
        )
        if tuple(value[0] for value in controls) != tuple(range(len(controls))):
            raise _AuditCorrupt(
                "worker_durable_audit_atomic_control_root_corrupt"
            )
        plans = tuple(value[1] for value in controls)
        pipeline_sha256 = _pipeline_intents_sha256(
            connection,
            commit_id,
            row[21],
            budget,
        )
        controls_sha256 = _atomic_digest(
            ATOMIC_BUSINESS_WRITE_CONTROLS_HASH_DOMAIN,
            tuple(
                plan.expected_receipt.request_sha256
                + plan.expected_receipt.after_state_sha256
                + plan.expected_receipt.receipt_sha256
                + plan.expected_receipt.sequence.to_bytes(8, "big")
                for plan in plans
            ),
        )
        expected_members = [
            {
                "ordinal": ordinal,
                "intent_id": plan.request.request_transaction_id,
                "job_id": plan.target.target_id,
                "control_transaction_id": plan.request.request_transaction_id,
                "request_sha256": plan.expected_receipt.request_sha256.hex(),
                "after_state_sha256": (
                    plan.expected_receipt.after_state_sha256.hex()
                ),
                "receipt_sha256": plan.expected_receipt.receipt_sha256.hex(),
                "receipt_sequence": plan.expected_receipt.sequence,
            }
            for ordinal, plan in controls
        ]
        try:
            before = WorkerLedgerHead(row[8], row[9])
            after = WorkerLedgerHead(row[10], row[11])
            expected_manifest = {
                "schema": ATOMIC_BUSINESS_WRITE_MANIFEST_SCHEMA,
                "protocol": ATOMIC_BUSINESS_WRITE_PROTOCOL,
                "status": ATOMIC_BUSINESS_WRITE_PLAN_STATUS,
                "commit": {
                    "commit_id": commit_id,
                    "committed_at": row[3],
                    "atomic_manifest_sha256": row[20].hex(),
                    "pipeline_intents_sha256": pipeline_sha256.hex(),
                },
                "worker_ledger": {
                    "before": {
                        "sequence": before.sequence,
                        "tail_sha256": before.tail_sha256.hex(),
                    },
                    "after": {
                        "sequence": after.sequence,
                        "tail_sha256": after.tail_sha256.hex(),
                    },
                },
                "controls": {
                    "count": len(plans),
                    "sha256": controls_sha256.hex(),
                    "members": expected_members,
                },
            }
            expected_manifest_bytes = encode_payload_bounded(
                expected_manifest,
                MAX_ATOMIC_BUSINESS_WRITE_MANIFEST_BYTES,
                MAX_ATOMIC_BUSINESS_WRITE_MANIFEST_NODES,
            )
            manifest_sha256 = _atomic_manifest_digest(expected_manifest_bytes)
            parent = AtomicBusinessWriteGenericReceiptEvidence(
                row[1], row[2], row[3]
            )
            receipt_sha256 = _atomic_receipt_digest(
                commit_id=commit_id,
                receipt_parent=parent,
                atomic_manifest_sha256=row[20],
                pipeline_intents_sha256=pipeline_sha256,
                controls_sha256=controls_sha256,
                control_count=len(plans),
                before_worker_ledger=before,
                after_worker_ledger=after,
                manifest_sha256=manifest_sha256,
            )
        except Exception as exc:
            raise _AuditCorrupt(
                "worker_durable_audit_atomic_inner_root_corrupt"
            ) from exc
        budget.add_bytes(
            row[12],
            owner=("atomic_business_write_receipts", commit_id, "manifest"),
        )
        _generic_parent_exact(connection, row[1], row[2], row[3])
        if (
            row[1] != row[22]
            or row[2] != row[23]
            or row[3] != row[24]
            or not _same(row[4], row[20])
            or not _same(row[5], pipeline_sha256)
            or not _same(row[6], controls_sha256)
            or row[7] != len(plans)
            or row[7] != row[21]
            or row[10] != row[8] + len(plans)
            or (plans and plans[0].before_ledger != before)
            or any(
                left.after_ledger != right.before_ledger
                for left, right in zip(plans, plans[1:])
            )
            or (plans and plans[-1].after_ledger != after)
            or (not plans and before != after)
            or row[12] != expected_manifest_bytes
            or not _same(row[13], manifest_sha256)
            or not _same(row[14], receipt_sha256)
        ):
            raise _AuditCorrupt(
                "worker_durable_audit_atomic_inner_root_corrupt"
            )

        target_count, targets_sha256 = _production_targets_root(
            connection,
            commit_id,
            budget,
        )
        create_count = sum(
            plan.operation is PipelineBusinessControlOperation.CREATE
            for plan in plans
        )
        outer = _production_manifest(row[15], budget, commit_id)
        production_manifest_sha256 = _production_domain_digest(
            PRODUCTION_ATOMIC_MANIFEST_HASH_DOMAIN,
            (row[15],),
        )
        try:
            production_receipt_sha256 = _production_domain_digest(
                PRODUCTION_ATOMIC_RECEIPT_HASH_DOMAIN,
                (
                    commit_id.encode("ascii"),
                    row[1].encode("utf-8"),
                    row[2].encode("utf-8"),
                    row[3].encode("ascii"),
                    receipt_sha256,
                    production_manifest_sha256,
                    target_count.to_bytes(8, "big"),
                    targets_sha256,
                ),
            )
        except Exception as exc:
            raise _AuditCorrupt(
                "worker_durable_audit_atomic_outer_root_corrupt"
            ) from exc
        authority = business_manifest.get("authority")
        if type(authority) is not dict:
            raise _AuditCorrupt(
                "worker_durable_audit_atomic_outer_root_corrupt"
            )
        if row[29] == "change":
            mounted_repository_revision = authority.get("revision")
            mounted_control_revision = mount_origin.state_revision
            expected_authority = {
                "authority": mounted.authority,
                "authority_state": "active",
                "epoch": mounted.repository_epoch,
                "cutover_id": mounted.cutover_id,
                "source_admission_revision": mounted.source_admission_revision,
                "revision": row[28] - 1,
                "control_revision": mount_origin.state_revision,
                "runtime_generation": mounted.runtime_generation,
                "code_digest": mounted.code_digest.hex(),
            }
            if authority != expected_authority:
                raise _AuditCorrupt(
                    "worker_durable_audit_atomic_outer_root_corrupt"
                )
        elif row[29] == "baseline" and row[28] == 1:
            mounted_repository_revision = mount_origin.repository_revision
            mounted_control_revision = mount_origin.state_revision
        elif row[29] == "baseline":
            mounted_repository_revision = authority.get("repository_revision")
            if type(mounted_repository_revision) is int:
                mounted_repository_revision -= 1
            mounted_control_revision = authority.get("state_revision")
        else:
            raise _AuditCorrupt(
                "worker_durable_audit_atomic_outer_root_corrupt"
            )
        expected_mount = _mount_manifest(mounted)
        expected_mount["repository_revision"] = mounted_repository_revision
        expected_mount["control_revision"] = mounted_control_revision
        expected_outer = {
            "schema": PRODUCTION_ATOMIC_WRITE_MANIFEST_SCHEMA,
            "protocol": PRODUCTION_ATOMIC_WRITE_PROTOCOL,
            "status": PRODUCTION_ATOMIC_WRITE_STATUS,
            "hash_domains": {
                "manifest": PRODUCTION_ATOMIC_MANIFEST_HASH_DOMAIN.decode("ascii"),
                "receipt": PRODUCTION_ATOMIC_RECEIPT_HASH_DOMAIN.decode("ascii"),
                "targets": PRODUCTION_ATOMIC_TARGETS_HASH_DOMAIN.decode("ascii"),
                "target_group": PRODUCTION_ATOMIC_TARGET_GROUP_HASH_DOMAIN.decode(
                    "ascii"
                ),
                "target_member": PRODUCTION_ATOMIC_TARGET_MEMBER_HASH_DOMAIN.decode(
                    "ascii"
                ),
                "genesis": PRODUCTION_WORKER_GENESIS_STATE_HASH_DOMAIN.decode(
                    "ascii"
                ),
                "outbox_allocation_tail": (
                    PRODUCTION_OUTBOX_ALLOCATION_TAIL_HASH_DOMAIN.decode("ascii")
                ),
                "control_member": PRODUCTION_ATOMIC_CONTROL_MEMBER_HASH_DOMAIN.decode(
                    "ascii"
                ),
            },
            "pure_atomic": {
                "commit_id": commit_id,
                "transaction_id": row[1],
                "receipt_kind": row[2],
                "committed_at": row[3],
                "manifest_sha256": manifest_sha256.hex(),
                "receipt_sha256": receipt_sha256.hex(),
            },
            "mounted_authority": expected_mount,
            "scheduler_heads": {
                "worker_ledger": worker_before,
                "outbox_dispatch": dispatch_before,
            },
            "outbox_allocation": {
                "count": allocation_count,
                "before": allocation_before,
                "after": allocation_after,
            },
            "targets": {
                "group_order": [
                    value.value for value in PRODUCTION_ATOMIC_TARGET_GROUP_ORDER
                ],
                "count": target_count,
                "sha256": targets_sha256.hex(),
            },
        }
        if (
            outer != expected_outer
            or target_count != row[25] + row[26] + create_count
            or row[17] != target_count
            or not _same(row[18], targets_sha256)
            or not _same(row[16], production_manifest_sha256)
            or not _same(row[19], production_receipt_sha256)
        ):
            raise _AuditCorrupt(
                "worker_durable_audit_atomic_outer_root_corrupt"
            )
    if set(controls_by_commit) - observed_commits:
        raise _AuditCorrupt("worker_durable_audit_atomic_control_root_corrupt")


def _worker_head_manifest_value(
    state_revision: int,
    sequence: int,
    previous_tail_sha256: bytes,
    tail_sha256: bytes,
    updated_at: str,
) -> Dict[str, Any]:
    return {
        "state_revision": state_revision,
        "sequence": sequence,
        "previous_tail_sha256": previous_tail_sha256.hex(),
        "tail_sha256": tail_sha256.hex(),
        "updated_at": updated_at,
    }


def _dispatch_head_manifest_value(
    state: WorkerOutboxDispatchState,
    previous_head_sha256: bytes,
    head_sha256: bytes,
    updated_at: str,
) -> Dict[str, Any]:
    return {
        "state_revision": state.control_revision,
        "published": {
            "sequence": state.published.sequence,
            "repository_revision": state.published.repository_revision,
            "ordinal": state.published.ordinal,
        },
        "inflight": None if state.inflight is None else {
            "sequence": state.inflight.sequence,
            "repository_revision": state.inflight.repository_revision,
            "ordinal": state.inflight.ordinal,
        },
        "previous_head_sha256": previous_head_sha256.hex(),
        "head_sha256": head_sha256.hex(),
        "updated_at": updated_at,
    }


def _audit_snapshot(
    connection: Any,
    profile: Any,
    mounted: Any,
    request_id: str,
    expected: Optional[WorkerDurableIdentity],
) -> _SnapshotAudit:
    budget = _Budget()
    target_cache: Dict[str, Tuple[WorkerTarget, str]] = {}
    mounted_evidence = mounted.evidence
    _worker_parent_namespace_exact(connection)
    _pipeline_control_scope_exact(connection)
    allocation_batches = _allocation_chain_exact(
        connection,
        budget,
        target_cache,
        mounted_evidence.mounted_at,
    )
    _non_outbox_genesis_scope_exact(connection, budget)
    head = connection.execute(
        "SELECT state_revision,sequence,previous_tail_sha256,tail_sha256,"
        "updated_at FROM worker_ledger_head WHERE singleton=1"
    ).fetchone()
    if head is None or len(head) != 5:
        raise _AuditCorrupt("worker_durable_audit_head_missing")
    if (
        type(head[0]) is not int or type(head[1]) is not int
        or type(head[2]) is not bytes or len(head[2]) != 32
        or type(head[3]) is not bytes or len(head[3]) != 32
        or head[0] < 1 or head[1] < 0
    ):
        raise _AuditCorrupt("worker_durable_audit_head_corrupt")

    before_ledger = EMPTY_WORKER_LEDGER_HEAD
    found_identity: Optional[WorkerDurableIdentity] = None
    last_outbox: Dict[str, _OutboxReplay] = {}
    last_pipeline: Dict[str, Tuple[Any, bytes]] = {}
    dispatch_expected = WorkerOutboxDispatchState(
        1,
        WorkerOutboxDispatchCursor(0, 0, -1),
        None,
    )
    dispatch_previous_sha256 = _ZERO_SHA256
    dispatch_head_sha256 = _ZERO_SHA256
    dispatch_updated_at: Optional[str] = mounted_evidence.mounted_at
    worker_heads: Dict[int, Dict[str, Any]] = {
        0: _worker_head_manifest_value(
            1,
            0,
            _ZERO_SHA256,
            _ZERO_SHA256,
            mounted_evidence.mounted_at,
        )
    }
    dispatch_heads: Dict[int, Dict[str, Any]] = {
        0: _dispatch_head_manifest_value(
            dispatch_expected,
            dispatch_previous_sha256,
            dispatch_head_sha256,
            mounted_evidence.mounted_at,
        )
    }
    expected_counts = {
        "attempts": 0,
        "effects": 0,
        "results": 0,
        "reconcile": 0,
        "effect_sidecars": 0,
        "result_sidecars": 0,
        "proof_sidecars": 0,
    }
    cursor = connection.execute(
        """
        SELECT sequence,entry_kind,transaction_id,receipt_kind,committed_at,
               previous_tail_sha256,ledger_tail_sha256,request_sha256,
               after_state_sha256,receipt_sha256,occurred_at
        FROM worker_ledger_entries ORDER BY sequence
        """
    )
    processed = 0
    last_entry: Any = None
    seen_head_batches = set()
    current_head_batch: Any = None
    last_batch_previous = _ZERO_SHA256
    control_ordinals: Dict[str, int] = {}
    controls_by_commit: Dict[str, list[Tuple[int, Any]]] = {}
    for entry in cursor:
        budget.add_entry()
        processed += 1
        if entry[0] != processed or entry[0] > head[1]:
            raise _AuditCorrupt("worker_durable_audit_ledger_sequence_corrupt")
        if (
            entry[0] != before_ledger.sequence + 1
            or not _same(entry[5], before_ledger.tail_sha256)
        ):
            raise _AuditCorrupt("worker_durable_audit_ledger_chain_corrupt")
        _generic_parent_exact(connection, entry[2], entry[3], entry[4])
        if entry[1] == "pipeline_control":
            origin_commit_id, control, control_ordinal = _pipeline_membership_exact(
                connection, entry, before_ledger, profile, mounted, budget
            )
            target_id = control.target.target_id
            prior_control = last_pipeline.get(target_id)
            if control.operation is PipelineBusinessControlOperation.CREATE:
                if prior_control is not None or control.request.before is not None:
                    raise _AuditCorrupt(
                        "worker_durable_audit_pipeline_state_chain_corrupt"
                    )
                previous_control_state_sha256 = _ZERO_SHA256
                genesis = connection.execute(
                    "SELECT genesis_status,genesis_state_sha256,source_ordinal,"
                    "created_at FROM worker_targets WHERE target_id=? AND "
                    "target_kind='pipeline'",
                    (target_id,),
                ).fetchone()
                if not _row_is(genesis, (
                    WorkerExecutionStatus.QUEUED.value,
                    control.expected_receipt.after_state_sha256,
                    control_ordinal,
                    control.expected_receipt.occurred_at,
                )):
                    raise _AuditCorrupt(
                        "worker_durable_audit_pipeline_genesis_corrupt"
                    )
            else:
                if prior_control is None or control.request.before is None:
                    raise _AuditCorrupt(
                        "worker_durable_audit_pipeline_state_chain_corrupt"
                    )
                prior_plan = prior_control[0]
                if _state_image(control.request.before) != _state_image(
                    prior_plan.closure.after_state
                ):
                    raise _AuditCorrupt(
                        "worker_durable_audit_pipeline_state_chain_corrupt"
                    )
                previous_control_state_sha256 = (
                    prior_plan.expected_receipt.after_state_sha256
                )
            last_pipeline[target_id] = (
                control,
                previous_control_state_sha256,
            )
            batch_key = ("pipeline_control", origin_commit_id)
            expected_control_ordinal = control_ordinals.get(origin_commit_id, -1) + 1
            if control_ordinal != expected_control_ordinal:
                raise _AuditCorrupt(
                    "worker_durable_audit_pipeline_order_corrupt"
                )
            control_ordinals[origin_commit_id] = control_ordinal
            controls_by_commit.setdefault(origin_commit_id, []).append(
                (control_ordinal, control)
            )
            if batch_key != current_head_batch:
                if batch_key in seen_head_batches:
                    raise _AuditCorrupt(
                        "worker_durable_audit_head_batch_corrupt"
                    )
                seen_head_batches.add(batch_key)
                current_head_batch = batch_key
                last_batch_previous = entry[5]
            before_ledger = WorkerLedgerHead(entry[0], entry[6])
            last_entry = entry
            worker_heads[entry[0]] = _worker_head_manifest_value(
                len(seen_head_batches) + 1,
                entry[0],
                last_batch_previous,
                entry[6],
                entry[10],
            )
            dispatch_heads[entry[0]] = _dispatch_head_manifest_value(
                dispatch_expected,
                dispatch_previous_sha256,
                dispatch_head_sha256,
                dispatch_updated_at,
            )
            continue
        if entry[1] != "worker_execution":
            raise _AuditCorrupt("worker_durable_audit_ledger_kind_corrupt")
        row = _specialised_row(connection, entry[2])
        if row is None:
            raise _AuditCorrupt("worker_durable_audit_specialised_receipt_missing")
        if row[6] != WorkerTargetKind.OUTBOX.value:
            raise _AuditUnsupported("worker_durable_audit_target_kind_unsupported")
        try:
            operation = WorkerExecutionOperation(row[5])
        except ValueError as exc:
            raise _AuditCorrupt("worker_durable_audit_operation_corrupt") from exc
        if operation not in _SUPPORTED_OPERATIONS:
            raise _AuditUnsupported("worker_durable_audit_operation_unsupported")
        batch_key = ("worker_execution", entry[2])
        if batch_key in seen_head_batches:
            raise _AuditCorrupt("worker_durable_audit_head_batch_corrupt")
        seen_head_batches.add(batch_key)
        current_head_batch = batch_key
        last_batch_previous = entry[5]
        plan, origin_created_at = _rebuild_request(
            connection,
            row,
            before_ledger,
            profile,
            mounted,
            budget,
            target_cache,
        )
        prior = last_outbox.get(plan.target.target_id)
        if prior is None:
            if (
                plan.operation is not WorkerExecutionOperation.CLAIM
                or _state_image(plan.before)
                != _state_image(_outbox_genesis_before())
            ):
                raise _AuditCorrupt(
                    "worker_durable_audit_target_state_chain_corrupt"
                )
            try:
                previous_state_sha256 = _production_worker_genesis_state_sha256(
                    plan.target,
                    status=WorkerExecutionStatus.PENDING,
                )
            except Exception as exc:
                raise _AuditCorrupt(
                    "worker_durable_audit_target_genesis_corrupt"
                ) from exc
            available_at = origin_created_at
            last_error = None
        else:
            if (
                prior.created_at != origin_created_at
                or _state_image(plan.before) != _state_image(prior.after)
            ):
                raise _AuditCorrupt(
                    "worker_durable_audit_target_state_chain_corrupt"
                )
            previous_state_sha256 = prior.state_sha256
            available_at = prior.available_at
            last_error = prior.last_error
        _worker_receipt_exact(entry, row, plan)
        if operation is WorkerExecutionOperation.CLAIM:
            _claim_attempt_exact(connection, plan)
            expected_counts["attempts"] += 1
        elif operation is WorkerExecutionOperation.AUTHORIZE_EFFECT:
            _compare_effect_projection(connection, plan, budget)
            expected_counts["effects"] += 1
            expected_counts["effect_sidecars"] += 1
        else:
            _compare_result_projection(connection, plan, budget)
            projection = _require_worker_result_projection_plan(
                _issue_worker_result_projection_plan(plan)
            )
            expected_counts["results"] += 1
            expected_counts["result_sidecars"] += 1
            if projection.closure.proof is not None:
                expected_counts["proof_sidecars"] += 1
            if operation is WorkerExecutionOperation.RECONCILE:
                expected_counts["reconcile"] += 1

        fence = getattr(plan.request, "dispatch_fence", None)
        if fence is not None:
            if fence.before != dispatch_expected:
                raise _AuditCorrupt("worker_durable_audit_dispatch_chain_corrupt")
            if fence.after != fence.before:
                if (
                    fence.after.control_revision
                    != fence.before.control_revision + 1
                ):
                    raise _AuditCorrupt(
                        "worker_durable_audit_dispatch_chain_corrupt"
                    )
                dispatch_previous_sha256 = dispatch_head_sha256
                dispatch_head_sha256 = plan.expected_receipt.after_state_sha256
                dispatch_updated_at = plan.expected_receipt.occurred_at
                dispatch_expected = fence.after
        before_ledger = plan.after_ledger
        last_entry = entry
        worker_heads[entry[0]] = _worker_head_manifest_value(
            len(seen_head_batches) + 1,
            entry[0],
            last_batch_previous,
            plan.after_ledger.tail_sha256,
            entry[10],
        )
        dispatch_heads[entry[0]] = _dispatch_head_manifest_value(
            dispatch_expected,
            dispatch_previous_sha256,
            dispatch_head_sha256,
            dispatch_updated_at,
        )
        if plan.closure.after_state.status is WorkerExecutionStatus.RETRY:
            available_at = plan.closure.after_state.next_attempt_at
        if operation is WorkerExecutionOperation.COMPLETE:
            # COMPLETE owns the legacy diagnostic slot.  AUTHORIZE and
            # RECONCILE leave the prior diagnostic untouched; their new bytes
            # are owned by the V9 payload/proof sidecars.
            last_error = plan.request.error
        last_outbox[plan.target.target_id] = _OutboxReplay(
            plan.closure.after_state,
            plan.expected_receipt.after_state_sha256,
            previous_state_sha256,
            plan.expected_receipt,
            origin_created_at,
            available_at,
            last_error,
        )
        actual_identity = _identity(plan)
        if actual_identity.request_id == request_id:
            if found_identity is not None:
                raise _AuditCorrupt("worker_durable_audit_request_duplicate")
            found_identity = actual_identity

    expected_head_previous = last_batch_previous
    if (
        processed != head[1]
        or head[0] != len(seen_head_batches) + 1
        or before_ledger.sequence != head[1]
        or not _same(head[2], expected_head_previous)
        or not _same(before_ledger.tail_sha256, head[3])
        or (head[1] == 0 and not _same(head[3], _ZERO_SHA256))
        or (
            last_entry is not None
            and head[4] != last_entry[10]
        )
    ):
        raise _AuditCorrupt("worker_durable_audit_head_closure_corrupt")
    totals = connection.execute(
        """
        SELECT
          (SELECT count(*) FROM worker_attempts),
          (SELECT count(*) FROM worker_effects),
          (SELECT count(*) FROM worker_results),
          (SELECT count(*) FROM worker_reconcile_proofs),
          (SELECT count(*) FROM worker_effect_request_projections),
          (SELECT count(*) FROM worker_result_payload_projections),
          (SELECT count(*) FROM worker_proof_payload_projections),
          (SELECT count(*) FROM worker_execution_receipts),
          (SELECT count(*) FROM pipeline_control_receipts)
        """
    ).fetchone()
    worker_entries = connection.execute(
        "SELECT count(*) FROM worker_ledger_entries "
        "WHERE entry_kind='worker_execution'"
    ).fetchone()[0]
    pipeline_entries = processed - worker_entries
    if not _row_is(totals, (
        expected_counts["attempts"], expected_counts["effects"],
        expected_counts["results"], expected_counts["reconcile"],
        expected_counts["effect_sidecars"], expected_counts["result_sidecars"],
        expected_counts["proof_sidecars"], worker_entries, pipeline_entries,
    )):
        raise _AuditCorrupt("worker_durable_audit_orphan_or_extra_rows")

    for controls in controls_by_commit.values():
        sequences = tuple(plan.expected_receipt.sequence for _ordinal, plan in controls)
        for sequence in sequences[:-1]:
            worker_heads.pop(sequence, None)
            dispatch_heads.pop(sequence, None)

    _atomic_business_roots_exact(
        connection,
        controls_by_commit,
        budget,
        profile,
        mounted_evidence,
        allocation_batches,
        worker_heads,
        dispatch_heads,
    )

    for target_id, replay in last_outbox.items():
        _current_row_exact(connection, target_id, replay)
        _legacy_outbox_exact(connection, target_id, replay)
    for control, previous_state_sha256 in last_pipeline.values():
        _pipeline_current_exact(
            connection,
            control,
            previous_state_sha256,
        )
    _revision, durable_dispatch, durable_previous, durable_hash, updated_at = (
        _dispatch_head(connection)
    )
    if (
        durable_dispatch != dispatch_expected
        or not _same(durable_previous, dispatch_previous_sha256)
        or not _same(durable_hash, dispatch_head_sha256)
        or (
            dispatch_updated_at is not None
            and updated_at != dispatch_updated_at
        )
    ):
        raise _AuditCorrupt("worker_durable_audit_dispatch_head_corrupt")

    if found_identity is None:
        if not _identity_absent(connection, request_id):
            raise _AuditCorrupt("worker_durable_audit_partial_identity")
        status = SQLiteWorkerDurableAuditStatus.ABSENT
        code = "worker_durable_audit_absent"
    elif expected is not None and expected != found_identity:
        status = SQLiteWorkerDurableAuditStatus.CONFLICT
        code = "worker_durable_audit_identity_conflict"
    else:
        status = SQLiteWorkerDurableAuditStatus.EXACT
        code = "worker_durable_audit_exact"
    result = SQLiteWorkerDurableAuditResult(
        status, request_id, code, found_identity, head[1], head[3]
    )
    fence = _SnapshotFence(
        profile.repository_epoch, profile.cutover_id, profile.runtime_generation,
        profile.code_digest, profile.repository_revision, profile.state_revision,
        head[0], head[1], head[3],
    )
    return _SnapshotAudit(result, fence)


class SQLiteWorkerDurableAuditor:
    """Fresh plan-free reader for one request identity and the whole ledger."""

    __slots__ = ("_database",)

    def __init__(self, database: SqliteDatabase) -> None:
        if type(database) is not SqliteDatabase:
            raise TypeError("database must be an exact SqliteDatabase")
        self._database = database

    @staticmethod
    def _result(
        status: SQLiteWorkerDurableAuditStatus,
        request_id: str,
        code: str,
    ) -> SQLiteWorkerDurableAuditResult:
        return SQLiteWorkerDurableAuditResult(status, request_id, code)

    def _read_snapshot(
        self,
        request_id: str,
        expected: Optional[WorkerDurableIdentity],
    ) -> _SnapshotAudit:
        connection = None
        try:
            connection, _filesystem = self._database._open_unvalidated()
            connection.execute("BEGIN")
            profile = _validate_business_runtime_connection(
                self._database, connection, access=BusinessRuntimeAccess.READER
            )
            mounted = _binding_from_snapshot(
                connection, profile, _mounted_row(connection)
            )
            result = _audit_snapshot(
                connection, profile, mounted, request_id, expected
            )
            connection.rollback()
            connection.close()
            return result
        except BaseException:
            if connection is not None:
                try:
                    if connection.in_transaction:
                        connection.rollback()
                except Exception:
                    pass
                try:
                    connection.close()
                except Exception:
                    pass
            raise

    def _publish_fence(self, expected: _SnapshotFence) -> bool:
        connection = None
        try:
            connection, _filesystem = self._database._open_unvalidated()
            connection.execute("BEGIN")
            profile = _validate_business_runtime_connection(
                self._database, connection, access=BusinessRuntimeAccess.READER
            )
            _binding_from_snapshot(connection, profile, _mounted_row(connection))
            head = connection.execute(
                "SELECT state_revision,sequence,tail_sha256 "
                "FROM worker_ledger_head WHERE singleton=1"
            ).fetchone()
            exact = bool(
                profile.repository_epoch == expected.repository_epoch
                and profile.cutover_id == expected.cutover_id
                and profile.runtime_generation == expected.runtime_generation
                and _same(profile.code_digest, expected.code_digest)
                and profile.repository_revision == expected.repository_revision
                and profile.state_revision == expected.control_revision
                and _row_is(head, (
                    expected.ledger_state_revision,
                    expected.ledger_sequence,
                    expected.ledger_tail_sha256,
                ))
            )
            connection.rollback()
            connection.close()
            return exact
        except Exception:
            if connection is not None:
                try:
                    if connection.in_transaction:
                        connection.rollback()
                except Exception:
                    pass
                try:
                    connection.close()
                except Exception:
                    pass
            return False

    def inspect(
        self,
        request_id: str,
        *,
        expected: Optional[WorkerDurableIdentity] = None,
    ) -> SQLiteWorkerDurableAuditResult:
        request_id = _request_id(request_id)
        if expected is not None and (
            type(expected) is not WorkerDurableIdentity
            or expected.request_id != request_id
        ):
            raise TypeError("expected must match request_id")
        try:
            snapshot = self._read_snapshot(request_id, expected)
        except _AuditConflict as exc:
            return self._result(
                SQLiteWorkerDurableAuditStatus.CONFLICT,
                request_id,
                str(exc) or "worker_durable_audit_conflict",
            )
        except _AuditCorrupt as exc:
            return self._result(
                SQLiteWorkerDurableAuditStatus.CORRUPT,
                request_id,
                str(exc) or "worker_durable_audit_corrupt",
            )
        except (_AuditUnsupported, _AuditBudget) as exc:
            return self._result(
                SQLiteWorkerDurableAuditStatus.UNKNOWN,
                request_id,
                str(exc) or "worker_durable_audit_unsupported",
            )
        except SQLiteProductionAuthorityError:
            return self._result(
                SQLiteWorkerDurableAuditStatus.CORRUPT,
                request_id,
                "worker_durable_audit_mount_corrupt",
            )
        except SQLiteKernelError as exc:
            status = (
                SQLiteWorkerDurableAuditStatus.UNKNOWN
                if exc.code in (
                    "sqlite_business_runtime_unreadable",
                    "sqlite_busy", "sqlite_locked", "sqlite_io_error",
                )
                else SQLiteWorkerDurableAuditStatus.CORRUPT
            )
            return self._result(
                status, request_id,
                "worker_durable_audit_runtime_unavailable"
                if status is SQLiteWorkerDurableAuditStatus.UNKNOWN
                else "worker_durable_audit_runtime_corrupt",
            )
        except (OSError, sqlite3.Error):
            return self._result(
                SQLiteWorkerDurableAuditStatus.UNKNOWN,
                request_id,
                "worker_durable_audit_unavailable",
            )
        except Exception:
            return self._result(
                SQLiteWorkerDurableAuditStatus.UNKNOWN,
                request_id,
                "worker_durable_audit_unavailable",
            )
        if not self._publish_fence(snapshot.fence):
            return self._result(
                SQLiteWorkerDurableAuditStatus.UNKNOWN,
                request_id,
                "worker_durable_audit_publish_fence_changed",
            )
        if snapshot.result.status in (
            SQLiteWorkerDurableAuditStatus.EXACT,
            SQLiteWorkerDurableAuditStatus.ABSENT,
        ):
            try:
                self._database._finalize_committed_storage()
            except Exception:
                return self._result(
                    SQLiteWorkerDurableAuditStatus.UNKNOWN,
                    request_id,
                    "worker_durable_audit_durability_barrier_failed",
                )
        return snapshot.result


__all__ = ()
