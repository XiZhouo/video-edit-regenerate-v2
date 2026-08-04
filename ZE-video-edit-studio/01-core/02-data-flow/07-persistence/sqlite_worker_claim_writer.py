"""Fixed-SQL first worker vertical: one OUTBOX PENDING -> LEASED claim.

Schema-v8 intentionally has no complete durable projection for effect-bearing
or result-bearing worker requests.  This writer therefore accepts exactly the
one transition that can already be rebuilt without inventing data: the first
claim of a pending outbox target.  Every durable fact is written in one SQLite
transaction and a fresh reader revalidates the same closure for retries and
ack-loss recovery.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import hmac
import sqlite3
from typing import Any, Callable, NamedTuple, Optional

from business_codec import (
    BusinessCodecError,
    decode_payload_bounded,
    encode_payload_bounded,
)
from business_transaction_contract import FrozenJsonPayload
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
    _digest as _atomic_business_component_sha256,
    _manifest_digest as _atomic_business_manifest_sha256,
)
from business_transaction_contract import (
    ATOMIC_COMMIT_MANIFEST_SCHEMA,
    ATOMIC_COMMIT_PROTOCOL,
    ATOMIC_COMMIT_STATUS_PREPARED,
    MAX_ATOMIC_MANIFEST_BYTES,
    MAX_ATOMIC_MANIFEST_NODES,
    MAX_ATOMIC_TOTAL_AUXILIARY_BYTES,
)
from business_transaction_integrity import (
    _component_digest as _atomic_commit_component_sha256,
    _manifest_digest as _atomic_commit_manifest_sha256,
)
from production_atomic_write_contract import (
    MAX_PRODUCTION_ATOMIC_MANIFEST_BYTES,
    MAX_PRODUCTION_ATOMIC_MANIFEST_NODES,
    PRODUCTION_ATOMIC_TARGET_GROUP_ORDER,
    PRODUCTION_ATOMIC_WRITE_MANIFEST_SCHEMA,
    PRODUCTION_ATOMIC_WRITE_PROTOCOL,
    PRODUCTION_ATOMIC_WRITE_STATUS,
    ProductionWorkerTarget,
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
    _domain_digest,
    _production_manifest_sha256,
    _production_outbox_allocation_tail,
    _production_target_member_sha256,
    _production_worker_genesis_state_sha256,
)
from sqlite_business_runtime import (
    BusinessRuntimeAccess,
    _validate_business_runtime_connection,
)
from sqlite_business_transaction import _rollback_close, _row_is
from sqlite_kernel import SQLiteKernelError, SqliteDatabase, _allow_authorizer
from sqlite_production_authority import (
    SQLiteProductionAuthorityError,
    _binding_from_snapshot,
    _mounted_row,
)
from sqlite_worker_durable_audit import (
    SQLiteWorkerDurableAuditor,
    SQLiteWorkerDurableAuditStatus,
    WorkerDurableIdentity,
)
from sqlite_production_writer import SQLiteProductionAtomicWriter
from worker_execution_contract import (
    WORKER_EXECUTION_REQUEST_CLASS,
    WorkerClaimRequest,
    WorkerExecutionGenericReceiptEvidence,
    WorkerExecutionOperation,
    WorkerExecutionPersistedObservation,
    WorkerExecutionReceiptEvidence,
    WorkerExecutionStatus,
    WorkerIdempotencyPolicy,
    WorkerLedgerHead,
    WorkerLedgerMembershipEvidence,
    WorkerOutboxDispatchCursor,
    WorkerOutboxDispatchState,
    WorkerOutboxOrder,
    WorkerPersistedReadStatus,
    WorkerReceiptReconcileStatus,
    WorkerReplayPolicy,
    WorkerTarget,
    WorkerTargetKind,
    _WORKER_EXECUTION_DURABLE_READER_CAPABILITY,
    _stamp_worker_execution_fresh_observation,
)
from worker_execution_integrity import (
    WorkerExecutionIntegrityError,
    WorkerExecutionPlan,
    _classify_worker_execution_receipt,
    _ledger_tail_digest,
    _require_worker_execution_plan,
)


_ZERO_SHA256 = b"\0" * 32


class SQLiteWorkerClaimStatus(str, Enum):
    COMMITTED = "committed"
    NOT_COMMITTED = "not_committed"
    CONFLICT = "conflict"
    CORRUPT = "corrupt"
    UNKNOWN = "unknown"


class SQLiteWorkerClaimTestPoint(str, Enum):
    BEFORE_COMMIT = "before_commit"
    AFTER_COMMIT_BEFORE_ACK = "after_commit_before_ack"


@dataclass(frozen=True)
class SQLiteWorkerClaimResult:
    status: SQLiteWorkerClaimStatus
    request_id: str
    code: str
    recovered_after_ack_loss: bool = False

    def __post_init__(self) -> None:
        if type(self.status) is not SQLiteWorkerClaimStatus:
            raise TypeError("status must be an exact SQLiteWorkerClaimStatus")
        if (
            type(self.request_id) is not str
            or len(self.request_id) != 32
            or any(value not in "0123456789abcdef" for value in self.request_id)
        ):
            raise TypeError("request_id must be 32 lowercase hexadecimal characters")
        if type(self.code) is not str or not self.code:
            raise TypeError("code must be a non-empty exact string")
        if type(self.recovered_after_ack_loss) is not bool or (
            self.recovered_after_ack_loss
            and self.status is not SQLiteWorkerClaimStatus.COMMITTED
        ):
            raise TypeError("recovered_after_ack_loss is invalid")


class SQLiteWorkerClaimError(ValueError):
    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        if type(code) is not str or not code:
            raise TypeError("code must be a non-empty exact string")
        self.code = code
        super().__init__(code)


class _ClaimConflict(RuntimeError):
    pass


class _ClaimCorrupt(RuntimeError):
    pass


def _same(left: Any, right: Any) -> bool:
    if type(left) is bytes and type(right) is bytes:
        return hmac.compare_digest(left, right)
    return type(left) is type(right) and left == right


def _frame_hash(hasher: Any, value: bytes) -> None:
    hasher.update(len(value).to_bytes(8, "big"))
    hasher.update(value)


def _domain_hasher(domain: bytes) -> Any:
    hasher = hashlib.sha256()
    _frame_hash(hasher, domain)
    return hasher


def _close_quietly(connection: Any) -> None:
    try:
        connection.close()
    except Exception:
        pass


_INSERT_TABLES = frozenset((
    "transaction_receipts",
    "worker_ledger_entries",
    "worker_execution_receipts",
    "worker_attempts",
))
_UPDATE_TABLES = frozenset((
    "worker_target_current",
    "event_outbox",
    "outbox_dispatch_head",
    "worker_ledger_head",
))
_UPDATE_COLUMNS = {
    "worker_target_current": frozenset((
        "status",
        "state_revision",
        "attempt_count",
        "current_attempt_id",
        "current_attempt_number",
        "fence_high_water",
        "lease_owner",
        "lease_fence",
        "lease_expires_at",
        "last_receipt_transaction_id",
        "previous_state_sha256",
        "state_sha256",
        "updated_at",
    )),
    "event_outbox": frozenset((
        "status",
        "state_revision",
        "attempt_count",
        "lease_owner",
        "lease_fence",
        "lease_expires_at",
        "updated_at",
    )),
    "outbox_dispatch_head": frozenset((
        "state_revision",
        "published_sequence",
        "published_repository_revision",
        "published_ordinal",
        "inflight_sequence",
        "inflight_repository_revision",
        "inflight_ordinal",
        "previous_head_sha256",
        "head_sha256",
        "updated_at",
    )),
    "worker_ledger_head": frozenset((
        "state_revision",
        "sequence",
        "previous_tail_sha256",
        "tail_sha256",
        "updated_at",
    )),
}


class _WorkerClaimAuthorizer:
    """Deny every mutation outside the fixed first-claim statement set."""

    __slots__ = ("denied",)

    def __init__(self) -> None:
        self.denied = False

    def __call__(
        self,
        action: int,
        arg1: Optional[str],
        arg2: Optional[str],
        database_name: Optional[str],
        trigger_name: Optional[str],
    ) -> int:
        del database_name, trigger_name
        forbidden = {
            getattr(sqlite3, name)
            for name in (
                "SQLITE_ALTER_TABLE",
                "SQLITE_ANALYZE",
                "SQLITE_ATTACH",
                "SQLITE_CREATE_INDEX",
                "SQLITE_CREATE_TABLE",
                "SQLITE_CREATE_TEMP_INDEX",
                "SQLITE_CREATE_TEMP_TABLE",
                "SQLITE_CREATE_TEMP_TRIGGER",
                "SQLITE_CREATE_TEMP_VIEW",
                "SQLITE_CREATE_TRIGGER",
                "SQLITE_CREATE_VIEW",
                "SQLITE_DETACH",
                "SQLITE_DROP_INDEX",
                "SQLITE_DROP_TABLE",
                "SQLITE_DROP_TEMP_INDEX",
                "SQLITE_DROP_TEMP_TABLE",
                "SQLITE_DROP_TEMP_TRIGGER",
                "SQLITE_DROP_TEMP_VIEW",
                "SQLITE_DROP_TRIGGER",
                "SQLITE_DROP_VIEW",
                "SQLITE_REINDEX",
            )
            if hasattr(sqlite3, name)
        }
        table = (arg1 or "").lower()
        column = (arg2 or "").lower()
        insert = getattr(sqlite3, "SQLITE_INSERT", -1)
        update = getattr(sqlite3, "SQLITE_UPDATE", -1)
        delete = getattr(sqlite3, "SQLITE_DELETE", -1)
        denied = bool(
            action in forbidden
            or action == getattr(sqlite3, "SQLITE_PRAGMA", -1)
            or (action == insert and table not in _INSERT_TABLES)
            or (
                action == update
                and (
                    table not in _UPDATE_TABLES
                    or column not in _UPDATE_COLUMNS[table]
                )
            )
            or action == delete
        )
        if denied:
            self.denied = True
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK


@dataclass(frozen=True)
class _WorkerHeadSnapshot:
    state_revision: int
    sequence: int
    previous_tail_sha256: bytes
    tail_sha256: bytes
    updated_at: str


@dataclass(frozen=True)
class _DispatchHeadSnapshot:
    state_revision: int
    published_sequence: int
    published_repository_revision: Optional[int]
    published_ordinal: Optional[int]
    inflight_sequence: Optional[int]
    inflight_repository_revision: Optional[int]
    inflight_ordinal: Optional[int]
    previous_head_sha256: bytes
    head_sha256: bytes
    updated_at: str

    def state(self) -> WorkerOutboxDispatchState:
        published = (
            WorkerOutboxDispatchCursor(0, 0, -1)
            if self.published_sequence == 0
            else WorkerOutboxDispatchCursor(
                self.published_sequence,
                self.published_repository_revision,
                self.published_ordinal,
            )
        )
        inflight = None
        if self.inflight_sequence is not None:
            inflight = WorkerOutboxOrder(
                self.inflight_sequence,
                self.inflight_repository_revision,
                self.inflight_ordinal,
            )
        return WorkerOutboxDispatchState(
            self.state_revision,
            published,
            inflight,
        )


class _OriginTargetRow(NamedTuple):
    target_kind: str
    target_id: str
    genesis_status: str
    genesis_state_sha256: bytes
    project_id: str
    generation: int
    document_id: str
    origin_commit_id: str
    source_ordinal: int
    payload_schema: str
    payload_codec_id: str
    payload_codec_version: int
    payload_bytes: bytes
    payload_sha256: bytes
    idempotency_scope: str
    idempotency_key: str
    replay_policy: str
    idempotency_policy: str
    max_attempts: int
    action_type: Optional[str]
    pipeline_id: Optional[str]
    intent_type: Optional[str]
    outbox_topic: Optional[str]
    outbox_partition_key: Optional[str]
    outbox_sequence: Optional[int]
    outbox_repository_revision: Optional[int]
    outbox_ordinal: Optional[int]
    created_at: str


class _OriginEventRow(NamedTuple):
    event_id: str
    previous_event_id: Optional[str]
    previous_ordinal: Optional[int]
    commit_id: str
    repository_revision: int
    commit_event_count: int
    operation_id: str
    ordinal: int
    topic: str
    partition_key: str
    event_bytes: bytes
    event_sha256: bytes
    payload_schema: str
    codec_id: str
    codec_version: int
    status: str
    state_revision: int
    attempt_count: int
    available_at: str
    lease_owner: Optional[str]
    lease_fence: Optional[int]
    lease_expires_at: Optional[str]
    published_at: Optional[str]
    outcome_unknown_at: Optional[str]
    dead_at: Optional[str]
    last_error_bytes: Optional[bytes]
    last_error_sha256: Optional[bytes]
    created_at: str
    updated_at: str


class SQLiteWorkerClaimWriter:
    """Commit and freshly reconcile only a first OUTBOX claim."""

    __slots__ = ("_database",)

    def __init__(self, database: SqliteDatabase) -> None:
        if type(database) is not SqliteDatabase:
            raise TypeError("database must be an exact SqliteDatabase")
        self._database = database

    @staticmethod
    def _result(
        plan: WorkerExecutionPlan,
        status: SQLiteWorkerClaimStatus,
        code: str,
        *,
        recovered: bool = False,
    ) -> SQLiteWorkerClaimResult:
        return SQLiteWorkerClaimResult(
            status,
            plan.closure.request_transaction_id,
            code,
            recovered,
        )

    @staticmethod
    def _durable_identity(plan: WorkerExecutionPlan) -> WorkerDurableIdentity:
        return WorkerDurableIdentity(
            plan.closure.request_transaction_id,
            plan.operation.value,
            plan.target.target_kind.value,
            plan.target.target_id,
            plan.closure.request_sha256,
            plan.closure.after_state_sha256,
            plan.expected_receipt.receipt_sha256,
            None,
        )

    def _reconcile_ledger_suffix(
        self,
        plan: WorkerExecutionPlan,
    ) -> SQLiteWorkerClaimResult:
        audited = SQLiteWorkerDurableAuditor(self._database).inspect(
            plan.closure.request_transaction_id,
            expected=self._durable_identity(plan),
        )
        if audited.status is SQLiteWorkerDurableAuditStatus.EXACT:
            return self._result(
                plan,
                SQLiteWorkerClaimStatus.COMMITTED,
                "worker_claim_exact_via_durable_audit",
            )
        if audited.status is SQLiteWorkerDurableAuditStatus.CONFLICT:
            status = SQLiteWorkerClaimStatus.CONFLICT
        elif audited.status is SQLiteWorkerDurableAuditStatus.CORRUPT:
            status = SQLiteWorkerClaimStatus.CORRUPT
        elif audited.status is SQLiteWorkerDurableAuditStatus.ABSENT:
            # The original snapshot had already proved this immutable receipt
            # existed.  Losing it across the fresh audit fence is an authority
            # or file-replacement race, not enough evidence to label either
            # snapshot corrupt.
            status = SQLiteWorkerClaimStatus.UNKNOWN
        else:
            status = SQLiteWorkerClaimStatus.UNKNOWN
        return self._result(
            plan,
            status,
            (
                "worker_claim_durable_audit_receipt_disappeared"
                if audited.status is SQLiteWorkerDurableAuditStatus.ABSENT
                else audited.code
            ),
        )

    @staticmethod
    def _require_vertical(plan: WorkerExecutionPlan) -> None:
        request = plan.request
        target = plan.target
        before = plan.before
        after = plan.closure.after_state
        if (
            type(request) is not WorkerClaimRequest
            or plan.operation is not WorkerExecutionOperation.CLAIM
            or target.target_kind is not WorkerTargetKind.OUTBOX
            or target.outbox_order is None
            or before.status is not WorkerExecutionStatus.PENDING
            or before.state_revision != 1
            or before.attempt_count != 0
            or before.current_attempt_id is not None
            or before.last_attempt_id is not None
            or before.current_effect is not None
            or before.unresolved_effect is not None
            or request.attempt.attempt_number != 1
            or request.attempt.previous_attempt_id is not None
            or request.attempt.previous_attempt_number is not None
            or request.attempt.execution_fence != 1
            or request.attempt.started_at != request.header.occurred_at
            or request.dispatch_fence is None
            or request.dispatch_fence.before.inflight is not None
            or request.dispatch_fence.after.inflight != target.outbox_order
            or request.dispatch_fence.after.control_revision
            != request.dispatch_fence.before.control_revision + 1
            or plan.before_ledger.sequence != 0
            or not _same(plan.before_ledger.tail_sha256, _ZERO_SHA256)
            or plan.after_ledger.sequence != 1
            or plan.expected_receipt.sequence != 1
            or after.status is not WorkerExecutionStatus.LEASED
            or after.state_revision != 2
            or after.attempt_count != 1
            or after.current_attempt_id != request.attempt.attempt_id
            or after.current_attempt_number != 1
        ):
            raise SQLiteWorkerClaimError("worker_claim_vertical_unsupported")

    @staticmethod
    def _read_worker_head(connection: Any) -> _WorkerHeadSnapshot:
        row = connection.execute(
            "SELECT state_revision,sequence,previous_tail_sha256,tail_sha256,"
            "updated_at FROM worker_ledger_head WHERE singleton=1"
        ).fetchone()
        if row is None or len(row) != 5:
            raise _ClaimCorrupt("worker_claim_worker_head_corrupt")
        try:
            value = _WorkerHeadSnapshot(*tuple(row))
            WorkerLedgerHead(value.sequence, value.tail_sha256)
        except (TypeError, ValueError) as exc:
            raise _ClaimCorrupt("worker_claim_worker_head_corrupt") from exc
        return value

    @staticmethod
    def _read_dispatch_head(connection: Any) -> _DispatchHeadSnapshot:
        row = connection.execute(
            "SELECT state_revision,published_sequence,"
            "published_repository_revision,published_ordinal,inflight_sequence,"
            "inflight_repository_revision,inflight_ordinal,previous_head_sha256,"
            "head_sha256,updated_at FROM outbox_dispatch_head WHERE singleton=1"
        ).fetchone()
        if row is None or len(row) != 10:
            raise _ClaimCorrupt("worker_claim_dispatch_head_corrupt")
        try:
            value = _DispatchHeadSnapshot(*tuple(row))
            value.state()
        except (TypeError, ValueError) as exc:
            raise _ClaimCorrupt("worker_claim_dispatch_head_corrupt") from exc
        return value

    @staticmethod
    def _authority_compatible(
        profile: Any,
        mounted: Any,
        plan: WorkerExecutionPlan,
        *,
        exact_revisions: bool,
    ) -> bool:
        authority = plan.authority
        evidence = mounted.evidence
        revision_exact = bool(
            authority.authority_revision
            == profile.repository_revision
            == evidence.repository_revision
            and authority.control_revision
            == profile.state_revision
            == evidence.control_revision
        )
        revision_descendant = bool(
            profile.repository_revision == evidence.repository_revision
            and profile.state_revision == evidence.control_revision
            and profile.repository_revision >= authority.authority_revision
            and profile.state_revision >= authority.control_revision
        )
        return bool(
            authority.authority == profile.authority == evidence.authority
            and authority.authority_state
            == profile.authority_state.value
            == evidence.authority_state
            and authority.authority_epoch
            == profile.repository_epoch
            == evidence.repository_epoch
            and authority.cutover_id == profile.cutover_id == evidence.cutover_id
            and authority.source_admission_revision
            == profile.source_admission_revision
            == evidence.source_admission_revision
            and (revision_exact if exact_revisions else revision_descendant)
            and authority.runtime_generation
            == profile.runtime_generation
            == evidence.runtime_generation
            and _same(authority.code_digest, profile.code_digest)
            and _same(authority.code_digest, evidence.code_digest)
        )

    @classmethod
    def _authority_exact(
        cls,
        profile: Any,
        mounted: Any,
        plan: WorkerExecutionPlan,
    ) -> bool:
        return cls._authority_compatible(
            profile,
            mounted,
            plan,
            exact_revisions=True,
        )

    @classmethod
    def _authority_descends(
        cls,
        profile: Any,
        mounted: Any,
        plan: WorkerExecutionPlan,
    ) -> bool:
        return cls._authority_compatible(
            profile,
            mounted,
            plan,
            exact_revisions=False,
        )

    @staticmethod
    def _exact_mapping(value: Any, keys: tuple[str, ...]) -> dict[str, Any]:
        if type(value) is not dict or set(value) != set(keys):
            raise _ClaimCorrupt("worker_claim_origin_manifest_corrupt")
        return value

    @staticmethod
    def _manifest_digest(value: Any) -> bytes:
        if type(value) is not str or len(value) != 64:
            raise _ClaimCorrupt("worker_claim_origin_manifest_corrupt")
        try:
            result = bytes.fromhex(value)
        except ValueError as exc:
            raise _ClaimCorrupt(
                "worker_claim_origin_manifest_corrupt"
            ) from exc
        if len(result) != 32:
            raise _ClaimCorrupt("worker_claim_origin_manifest_corrupt")
        return result

    @classmethod
    def _origin_event_projection_exact(
        cls,
        connection: Any,
        plan: WorkerExecutionPlan,
        *,
        target_event: _OriginEventRow,
    ) -> None:
        """Rebuild every immutable event member from the pure atomic closure."""

        commit_id = plan.target.origin_commit_id
        business = connection.execute(
            """
            SELECT transaction_id,receipt_kind,project_id,generation,document_id,
                   repository_revision,operation_count,outbox_event_count,
                   last_outbox_event_id,last_outbox_ordinal,manifest_bytes,
                   manifest_sha256,committed_at
            FROM business_commits WHERE commit_id=?
            """,
            (commit_id,),
        ).fetchone()
        atomic = connection.execute(
            """
            SELECT transaction_id,receipt_kind,committed_at,
                   atomic_manifest_sha256,pipeline_intents_sha256,
                   controls_sha256,control_count,before_worker_sequence,
                   before_worker_tail_sha256,after_worker_sequence,
                   after_worker_tail_sha256,manifest_bytes,manifest_sha256,
                   receipt_sha256,
                   EXISTS(
                     SELECT 1 FROM transaction_receipts AS parent
                     WHERE parent.transaction_id=atomic.transaction_id AND
                           parent.kind=atomic.receipt_kind AND
                           parent.committed_at=atomic.committed_at)
            FROM atomic_business_write_receipts AS atomic WHERE commit_id=?
            """,
            (commit_id,),
        ).fetchone()
        if (
            business is None
            or len(business) != 13
            or atomic is None
            or len(atomic) != 15
            or atomic[14] != 1
            or not _row_is(business[:5], (
                atomic[0],
                atomic[1],
                plan.target.project_id,
                plan.target.generation,
                plan.target.document_id,
            ))
            or business[12] != atomic[2]
            or not _same(business[11], atomic[3])
            or not _same(
                business[11],
                _atomic_commit_manifest_sha256(business[10]),
            )
            or not _same(
                atomic[12],
                _atomic_business_manifest_sha256(atomic[11]),
            )
        ):
            raise _ClaimCorrupt("worker_claim_origin_atomic_receipt_corrupt")

        try:
            atomic_root = decode_payload_bounded(
                atomic[11],
                MAX_ATOMIC_BUSINESS_WRITE_MANIFEST_BYTES,
                MAX_ATOMIC_BUSINESS_WRITE_MANIFEST_NODES,
            )
            atomic_canonical = encode_payload_bounded(
                atomic_root,
                MAX_ATOMIC_BUSINESS_WRITE_MANIFEST_BYTES,
                MAX_ATOMIC_BUSINESS_WRITE_MANIFEST_NODES,
            )
        except BusinessCodecError as exc:
            raise _ClaimCorrupt(
                "worker_claim_origin_atomic_manifest_corrupt"
            ) from exc
        atomic_root = cls._exact_mapping(atomic_root, (
            "schema",
            "protocol",
            "status",
            "commit",
            "worker_ledger",
            "controls",
        ))
        atomic_commit = cls._exact_mapping(atomic_root["commit"], (
            "commit_id",
            "committed_at",
            "atomic_manifest_sha256",
            "pipeline_intents_sha256",
        ))
        atomic_ledger = cls._exact_mapping(atomic_root["worker_ledger"], (
            "before",
            "after",
        ))
        atomic_before = cls._exact_mapping(atomic_ledger["before"], (
            "sequence",
            "tail_sha256",
        ))
        atomic_after = cls._exact_mapping(atomic_ledger["after"], (
            "sequence",
            "tail_sha256",
        ))
        atomic_controls = cls._exact_mapping(atomic_root["controls"], (
            "count",
            "sha256",
            "members",
        ))
        if (
            atomic_canonical != atomic[11]
            or atomic_root["schema"] != ATOMIC_BUSINESS_WRITE_MANIFEST_SCHEMA
            or atomic_root["protocol"] != ATOMIC_BUSINESS_WRITE_PROTOCOL
            or atomic_root["status"] != ATOMIC_BUSINESS_WRITE_PLAN_STATUS
            or atomic_commit != {
                "commit_id": commit_id,
                "committed_at": atomic[2],
                "atomic_manifest_sha256": atomic[3].hex(),
                "pipeline_intents_sha256": atomic[4].hex(),
            }
            or atomic_before != {
                "sequence": atomic[7],
                "tail_sha256": atomic[8].hex(),
            }
            or atomic_after != {
                "sequence": atomic[9],
                "tail_sha256": atomic[10].hex(),
            }
            or type(atomic_controls["members"]) is not list
            or atomic_controls["count"] != atomic[6]
            or len(atomic_controls["members"]) != atomic[6]
            or atomic_controls["sha256"] != atomic[5].hex()
        ):
            raise _ClaimCorrupt("worker_claim_origin_atomic_manifest_corrupt")
        try:
            parent = AtomicBusinessWriteGenericReceiptEvidence(
                atomic[0],
                atomic[1],
                atomic[2],
            )
            atomic_receipt_sha256 = _atomic_receipt_digest(
                commit_id=commit_id,
                receipt_parent=parent,
                atomic_manifest_sha256=atomic[3],
                pipeline_intents_sha256=atomic[4],
                controls_sha256=atomic[5],
                control_count=atomic[6],
                before_worker_ledger=WorkerLedgerHead(atomic[7], atomic[8]),
                after_worker_ledger=WorkerLedgerHead(atomic[9], atomic[10]),
                manifest_sha256=atomic[12],
            )
        except (
            AttributeError,
            IndexError,
            OverflowError,
            TypeError,
            ValueError,
        ) as exc:
            raise _ClaimCorrupt(
                "worker_claim_origin_atomic_receipt_corrupt"
            ) from exc
        if not _same(atomic[13], atomic_receipt_sha256):
            raise _ClaimCorrupt("worker_claim_origin_atomic_receipt_corrupt")

        try:
            manifest = decode_payload_bounded(
                business[10],
                MAX_ATOMIC_MANIFEST_BYTES,
                MAX_ATOMIC_MANIFEST_NODES,
            )
            canonical = encode_payload_bounded(
                manifest,
                MAX_ATOMIC_MANIFEST_BYTES,
                MAX_ATOMIC_MANIFEST_NODES,
            )
        except BusinessCodecError as exc:
            raise _ClaimCorrupt(
                "worker_claim_origin_business_manifest_corrupt"
            ) from exc
        root = cls._exact_mapping(manifest, (
            "schema",
            "protocol",
            "status",
            "authority",
            "commit",
            "before",
            "after",
            "operations",
            "operation_targets",
            "outbox",
            "durable_actions",
            "pipeline_intents",
            "asset_references",
            "inbox",
            "confirmation",
            "closure",
        ))
        manifest_commit = cls._exact_mapping(root["commit"], (
            "commit_id",
            "commit_kind",
            "trace_id",
            "project_id",
            "generation",
            "document_id",
            "save_policy",
        ))
        manifest_after = cls._exact_mapping(root["after"], (
            "document_version",
            "repository_revision",
            "project_revision",
            "head_revision",
            "commit_id",
            "business_root_sha256",
            "document_sha256",
            "entity_count",
        ))
        closure = cls._exact_mapping(root["closure"], (
            "operation_count",
            "operation_target_count",
            "outbox_count",
            "durable_action_count",
            "pipeline_intent_count",
            "asset_reference_count",
            "auxiliary_payload_bytes",
            "operation_tail_hash",
        ))
        expected_empty_pipeline_sha256 = _atomic_commit_component_sha256(
            b"ze.atomic-business-commit.pipelines.v1",
            (),
        )
        expected_empty_controls_sha256 = _atomic_business_component_sha256(
            ATOMIC_BUSINESS_WRITE_CONTROLS_HASH_DOMAIN,
            (),
        )
        if (
            canonical != business[10]
            or root["schema"] != ATOMIC_COMMIT_MANIFEST_SCHEMA
            or root["protocol"] != ATOMIC_COMMIT_PROTOCOL
            or root["status"] != ATOMIC_COMMIT_STATUS_PREPARED
            or manifest_commit["commit_id"] != commit_id
            or manifest_commit["commit_kind"] != "change"
            or manifest_commit["project_id"] != business[2]
            or manifest_commit["generation"] != business[3]
            or manifest_commit["document_id"] != business[4]
            or manifest_after["commit_id"] != commit_id
            or manifest_after["repository_revision"] != business[5]
            or closure["operation_count"] != business[6]
            or closure["outbox_count"] != business[7]
            or closure["pipeline_intent_count"] != 0
            or type(root["pipeline_intents"]) is not list
            or root["pipeline_intents"]
            or not _same(atomic[4], expected_empty_pipeline_sha256)
            or not _same(atomic[5], expected_empty_controls_sha256)
            or atomic[6] != 0
            or atomic_controls["members"]
            or atomic[7] != 0
            or not _same(atomic[8], _ZERO_SHA256)
            or atomic[9] != 0
            or not _same(atomic[10], _ZERO_SHA256)
            or type(root["operations"]) is not list
            or len(root["operations"]) != business[6]
            or type(root["outbox"]) is not list
            or len(root["outbox"]) != business[7]
            or len(root["operations"]) != len(root["outbox"])
        ):
            raise _ClaimCorrupt("worker_claim_origin_business_manifest_corrupt")

        operation_cursor = connection.execute(
            "SELECT operation_id,ordinal FROM business_operations "
            "WHERE commit_id=? ORDER BY ordinal",
            (commit_id,),
        )
        event_cursor = connection.execute(
            """
            SELECT event_id,previous_event_id,previous_ordinal,commit_id,
                   repository_revision,commit_event_count,operation_id,ordinal,
                   topic,partition_key,event_bytes,event_sha256,payload_schema,
                   codec_id,codec_version,status,state_revision,attempt_count,
                   available_at,lease_owner,lease_fence,lease_expires_at,
                   published_at,outcome_unknown_at,dead_at,last_error_bytes,
                   last_error_sha256,created_at,updated_at
            FROM event_outbox
            WHERE commit_id=? AND event_id<>? ORDER BY ordinal
            """,
            (commit_id, target_event.event_id),
        )

        previous_event_id = None
        target_seen = False
        event_payload_bytes = 0
        declared_event_payload_bytes = closure["auxiliary_payload_bytes"]
        if (
            type(declared_event_payload_bytes) is not int
            or declared_event_payload_bytes < 0
            or declared_event_payload_bytes > MAX_ATOMIC_TOTAL_AUXILIARY_BYTES
        ):
            raise _ClaimCorrupt("worker_claim_origin_event_members_corrupt")
        for ordinal, (operation_value, event_value) in enumerate(
            zip(root["operations"], root["outbox"])
        ):
            operation_row = operation_cursor.fetchone()
            if ordinal == target_event.ordinal:
                event_row = target_event
            else:
                raw_event = event_cursor.fetchone()
                if raw_event is None or len(raw_event) != 29:
                    raise _ClaimCorrupt(
                        "worker_claim_origin_event_members_corrupt"
                    )
                event_row = _OriginEventRow(*tuple(raw_event))
            operation_value = cls._exact_mapping(operation_value, (
                "ordinal",
                "operation_id",
                "record_bytes",
                "record_sha256",
                "prev_hash",
                "self_hash",
            ))
            event_value = cls._exact_mapping(event_value, (
                "ordinal",
                "event_id",
                "operation_id",
                "topic",
                "partition_key",
                "status",
                "payload",
            ))
            payload = cls._exact_mapping(event_value["payload"], (
                "schema",
                "codec_id",
                "codec_version",
                "bytes",
                "sha256",
            ))
            payload_sha256 = cls._manifest_digest(payload["sha256"])
            if type(event_row.event_bytes) is not bytes:
                raise _ClaimCorrupt("worker_claim_origin_event_members_corrupt")
            event_payload_bytes += len(event_row.event_bytes)
            if event_payload_bytes > declared_event_payload_bytes:
                raise _ClaimCorrupt("worker_claim_origin_event_members_corrupt")
            if (
                operation_row is None
                or not _row_is(operation_row, (
                    operation_value["operation_id"],
                    ordinal,
                ))
                or operation_value["ordinal"] != ordinal
                or event_value["ordinal"] != ordinal
                or event_value["operation_id"]
                != operation_value["operation_id"]
                or event_value["status"] != "pending"
                or not _row_is(event_row[:10], (
                    event_value["event_id"],
                    previous_event_id,
                    None if ordinal == 0 else ordinal - 1,
                    commit_id,
                    business[5],
                    business[7],
                    event_value["operation_id"],
                    ordinal,
                    event_value["topic"],
                    event_value["partition_key"],
                ))
                or len(event_row.event_bytes) != payload["bytes"]
                or not _same(event_row.event_sha256, payload_sha256)
                or not _same(
                    hashlib.sha256(event_row.event_bytes).digest(),
                    payload_sha256,
                )
                or not _row_is((
                    event_row.payload_schema,
                    event_row.codec_id,
                    event_row.codec_version,
                    event_row.created_at,
                ), (
                    payload["schema"],
                    payload["codec_id"],
                    payload["codec_version"],
                    business[12],
                ))
            ):
                raise _ClaimCorrupt("worker_claim_origin_event_members_corrupt")
            if event_row.event_id == plan.target.target_id:
                target_seen = True
            previous_event_id = event_row.event_id
        if (
            not target_seen
            or business[8] != previous_event_id
            or business[9] != business[7] - 1
            or operation_cursor.fetchone() is not None
            or event_cursor.fetchone() is not None
            or event_payload_bytes > declared_event_payload_bytes
        ):
            raise _ClaimCorrupt("worker_claim_origin_event_members_corrupt")

    @staticmethod
    def _production_target_from_row(row: Any) -> ProductionWorkerTarget:
        if row is None or len(row) != 28:
            raise _ClaimCorrupt("worker_claim_origin_target_set_corrupt")
        try:
            kind = WorkerTargetKind(row[0])
            order = (
                WorkerOutboxOrder(row[24], row[25], row[26])
                if kind is WorkerTargetKind.OUTBOX
                else None
            )
            target = WorkerTarget(
                target_kind=kind,
                target_id=row[1],
                project_id=row[4],
                generation=row[5],
                document_id=row[6],
                origin_commit_id=row[7],
                payload=FrozenJsonPayload(
                    row[9],
                    row[10],
                    row[11],
                    row[12],
                    row[13],
                ),
                replay_policy=WorkerReplayPolicy(row[16]),
                idempotency_policy=WorkerIdempotencyPolicy(row[17]),
                idempotency_scope=row[14],
                idempotency_key=row[15],
                max_attempts=row[18],
                outbox_order=order,
                pipeline_id=row[20],
                intent_type=row[21],
            )
            return ProductionWorkerTarget(
                source_ordinal=row[8],
                target=target,
                genesis_status=WorkerExecutionStatus(row[2]),
                genesis_state_sha256=row[3],
                action_type=row[19],
                outbox_topic=row[22],
                outbox_partition_key=row[23],
            )
        except (
            AttributeError,
            IndexError,
            OverflowError,
            TypeError,
            UnicodeError,
            ValueError,
        ) as exc:
            raise _ClaimCorrupt(
                "worker_claim_origin_target_set_corrupt"
            ) from exc

    @classmethod
    def _origin_production_target_exact(
        cls,
        connection: Any,
        plan: WorkerExecutionPlan,
        *,
        target_row: _OriginTargetRow,
        event_topic: str,
        event_partition_key: str,
        created_at: str,
    ) -> None:
        """Rebuild the origin target-set root, not merely two mutable mirrors."""

        receipt = connection.execute(
            """
            SELECT transaction_id,receipt_kind,committed_at,manifest_sha256,
                   receipt_sha256,production_manifest_bytes,
                   production_manifest_sha256,target_count,targets_sha256,
                   production_receipt_sha256,
                   EXISTS(
                     SELECT 1 FROM transaction_receipts AS parent
                     WHERE parent.transaction_id=atomic.transaction_id AND
                           parent.kind=atomic.receipt_kind AND
                           parent.committed_at=atomic.committed_at)
            FROM atomic_business_write_receipts AS atomic WHERE commit_id=?
            """,
            (plan.target.origin_commit_id,),
        ).fetchone()
        if receipt is None or len(receipt) != 11 or receipt[10] != 1:
            raise _ClaimCorrupt("worker_claim_origin_receipt_corrupt")
        if receipt[2] != created_at:
            raise _ClaimCorrupt("worker_claim_origin_receipt_corrupt")

        counts = {kind.value: (0, 0) for kind in PRODUCTION_ATOMIC_TARGET_GROUP_ORDER}
        count_cursor = connection.execute(
            "SELECT target_kind,count(*),coalesce(sum(length(payload_bytes)),0) "
            "FROM worker_targets WHERE origin_commit_id=? GROUP BY target_kind",
            (plan.target.origin_commit_id,),
        )
        for count_row in count_cursor:
            if (
                count_row is None
                or len(count_row) != 3
                or count_row[0] not in counts
                or type(count_row[1]) is not int
                or type(count_row[2]) is not int
                or count_row[1] < 0
                or count_row[2] < 0
            ):
                raise _ClaimCorrupt("worker_claim_origin_target_set_corrupt")
            counts[count_row[0]] = (count_row[1], count_row[2])
        target_count = sum(value[0] for value in counts.values())
        declared_payload_bytes = sum(value[1] for value in counts.values())
        if (
            target_count == 0
            or target_count != receipt[7]
            or declared_payload_bytes > MAX_ATOMIC_TOTAL_AUXILIARY_BYTES
        ):
            raise _ClaimCorrupt("worker_claim_origin_target_set_corrupt")

        group_digests: dict[str, bytes] = {}
        target_payload_bytes = 0
        current_seen = False
        order = plan.target.outbox_order
        assert order is not None
        for kind in PRODUCTION_ATOMIC_TARGET_GROUP_ORDER:
            expected_count = counts[kind.value][0]
            group_hasher = _domain_hasher(
                PRODUCTION_ATOMIC_TARGET_GROUP_HASH_DOMAIN
            )
            _frame_hash(group_hasher, kind.value.encode("ascii"))
            _frame_hash(group_hasher, expected_count.to_bytes(8, "big"))
            cursor = connection.execute(
                """
                SELECT target_kind,target_id,genesis_status,genesis_state_sha256,
                       project_id,generation,document_id,origin_commit_id,
                       source_ordinal,payload_schema,payload_codec_id,
                       payload_codec_version,payload_bytes,payload_sha256,
                       idempotency_scope,idempotency_key,replay_policy,
                       idempotency_policy,max_attempts,action_type,pipeline_id,
                       intent_type,outbox_topic,outbox_partition_key,
                       outbox_sequence,outbox_repository_revision,outbox_ordinal,
                       created_at
                FROM worker_targets
                WHERE origin_commit_id=? AND target_kind=? AND target_id<>?
                ORDER BY source_ordinal,target_id
                """,
                (plan.target.origin_commit_id, kind.value, target_row.target_id),
            )
            other_raw = cursor.fetchone()
            pending_current = target_row if target_row.target_kind == kind.value else None
            previous_source_ordinal = -1
            processed = 0
            while other_raw is not None or pending_current is not None:
                other = None
                if other_raw is not None:
                    if len(other_raw) != 28:
                        raise _ClaimCorrupt(
                            "worker_claim_origin_target_set_corrupt"
                        )
                    other = _OriginTargetRow(*tuple(other_raw))
                if pending_current is not None and (
                    other is None
                    or (
                        pending_current.source_ordinal,
                        pending_current.target_id,
                    ) < (other.source_ordinal, other.target_id)
                ):
                    member_row = pending_current
                    pending_current = None
                else:
                    assert other is not None
                    member_row = other
                    other_raw = cursor.fetchone()
                if (
                    member_row.target_kind != kind.value
                    or member_row.origin_commit_id != plan.target.origin_commit_id
                    or member_row.created_at != receipt[2]
                    or member_row.source_ordinal <= previous_source_ordinal
                ):
                    raise _ClaimCorrupt(
                        "worker_claim_origin_target_set_corrupt"
                    )
                is_current = member_row.target_id == plan.target.target_id
                if is_current:
                    # The fixed target query already byte-compared the full
                    # payload five-tuple with this sealed carrier.  Reusing it
                    # preserves that proof without another 16 MiB canonical
                    # decode/encode on every precommit closure pass.
                    wrapped = ProductionWorkerTarget(
                        source_ordinal=member_row.source_ordinal,
                        target=plan.target,
                        genesis_status=WorkerExecutionStatus(
                            member_row.genesis_status
                        ),
                        genesis_state_sha256=member_row.genesis_state_sha256,
                        action_type=member_row.action_type,
                        outbox_topic=member_row.outbox_topic,
                        outbox_partition_key=member_row.outbox_partition_key,
                    )
                else:
                    wrapped = cls._production_target_from_row(member_row)
                target_payload_bytes += len(member_row.payload_bytes)
                if target_payload_bytes > MAX_ATOMIC_TOTAL_AUXILIARY_BYTES:
                    raise _ClaimCorrupt(
                        "worker_claim_origin_target_set_corrupt"
                    )
                _frame_hash(
                    group_hasher,
                    _production_target_member_sha256(wrapped),
                )
                if is_current:
                    if current_seen or (
                        wrapped.target != plan.target
                        or wrapped.source_ordinal != order.ordinal
                        or wrapped.genesis_status
                        is not WorkerExecutionStatus.PENDING
                        or not _same(
                            wrapped.genesis_state_sha256,
                            _production_worker_genesis_state_sha256(
                                plan.target,
                                status=WorkerExecutionStatus.PENDING,
                            ),
                        )
                        or wrapped.outbox_topic != event_topic
                        or wrapped.outbox_partition_key != event_partition_key
                    ):
                        raise _ClaimCorrupt(
                            "worker_claim_origin_target_binding_corrupt"
                        )
                    current_seen = True
                previous_source_ordinal = member_row.source_ordinal
                processed += 1
            if processed != expected_count or cursor.fetchone() is not None:
                raise _ClaimCorrupt("worker_claim_origin_target_set_corrupt")
            group_digests[kind.value] = group_hasher.digest()
        if (
            not current_seen
            or target_payload_bytes != declared_payload_bytes
        ):
            raise _ClaimCorrupt("worker_claim_origin_target_set_corrupt")

        targets_hasher = _domain_hasher(PRODUCTION_ATOMIC_TARGETS_HASH_DOMAIN)
        _frame_hash(targets_hasher, target_count.to_bytes(8, "big"))
        for kind in PRODUCTION_ATOMIC_TARGET_GROUP_ORDER:
            _frame_hash(targets_hasher, kind.value.encode("ascii"))
            _frame_hash(
                targets_hasher,
                counts[kind.value][0].to_bytes(8, "big"),
            )
            _frame_hash(targets_hasher, group_digests[kind.value])
        targets_sha256 = targets_hasher.digest()
        if not _same(receipt[8], targets_sha256):
            raise _ClaimCorrupt("worker_claim_origin_target_set_corrupt")

        manifest_bytes = receipt[5]
        try:
            manifest = decode_payload_bounded(
                manifest_bytes,
                MAX_PRODUCTION_ATOMIC_MANIFEST_BYTES,
                MAX_PRODUCTION_ATOMIC_MANIFEST_NODES,
            )
            canonical = encode_payload_bounded(
                manifest,
                MAX_PRODUCTION_ATOMIC_MANIFEST_BYTES,
                MAX_PRODUCTION_ATOMIC_MANIFEST_NODES,
            )
        except BusinessCodecError as exc:
            raise _ClaimCorrupt(
                "worker_claim_origin_manifest_corrupt"
            ) from exc
        root = cls._exact_mapping(manifest, (
            "schema",
            "protocol",
            "status",
            "hash_domains",
            "pure_atomic",
            "mounted_authority",
            "scheduler_heads",
            "outbox_allocation",
            "targets",
        ))
        if (
            canonical != manifest_bytes
            or root["schema"] != PRODUCTION_ATOMIC_WRITE_MANIFEST_SCHEMA
            or root["protocol"] != PRODUCTION_ATOMIC_WRITE_PROTOCOL
            or root["status"] != PRODUCTION_ATOMIC_WRITE_STATUS
            or not _same(
                receipt[6],
                _production_manifest_sha256(manifest_bytes),
            )
        ):
            raise _ClaimCorrupt("worker_claim_origin_manifest_corrupt")

        domains = cls._exact_mapping(root["hash_domains"], (
            "manifest",
            "receipt",
            "targets",
            "target_group",
            "target_member",
            "genesis",
            "outbox_allocation_tail",
            "control_member",
        ))
        expected_domains = {
            "manifest": PRODUCTION_ATOMIC_MANIFEST_HASH_DOMAIN.decode("ascii"),
            "receipt": PRODUCTION_ATOMIC_RECEIPT_HASH_DOMAIN.decode("ascii"),
            "targets": PRODUCTION_ATOMIC_TARGETS_HASH_DOMAIN.decode("ascii"),
            "target_group": (
                PRODUCTION_ATOMIC_TARGET_GROUP_HASH_DOMAIN.decode("ascii")
            ),
            "target_member": (
                PRODUCTION_ATOMIC_TARGET_MEMBER_HASH_DOMAIN.decode("ascii")
            ),
            "genesis": PRODUCTION_WORKER_GENESIS_STATE_HASH_DOMAIN.decode("ascii"),
            "outbox_allocation_tail": (
                PRODUCTION_OUTBOX_ALLOCATION_TAIL_HASH_DOMAIN.decode("ascii")
            ),
            "control_member": (
                PRODUCTION_ATOMIC_CONTROL_MEMBER_HASH_DOMAIN.decode("ascii")
            ),
        }
        if domains != expected_domains:
            raise _ClaimCorrupt("worker_claim_origin_manifest_corrupt")

        pure = cls._exact_mapping(root["pure_atomic"], (
            "commit_id",
            "transaction_id",
            "receipt_kind",
            "committed_at",
            "manifest_sha256",
            "receipt_sha256",
        ))
        if pure != {
            "commit_id": plan.target.origin_commit_id,
            "transaction_id": receipt[0],
            "receipt_kind": receipt[1],
            "committed_at": receipt[2],
            "manifest_sha256": receipt[3].hex(),
            "receipt_sha256": receipt[4].hex(),
        }:
            raise _ClaimCorrupt("worker_claim_origin_manifest_corrupt")
        manifest_targets = cls._exact_mapping(root["targets"], (
            "group_order",
            "count",
            "sha256",
        ))
        if manifest_targets != {
            "group_order": [value.value for value in PRODUCTION_ATOMIC_TARGET_GROUP_ORDER],
            "count": target_count,
            "sha256": targets_sha256.hex(),
        }:
            raise _ClaimCorrupt("worker_claim_origin_manifest_corrupt")

        allocation = cls._exact_mapping(root["outbox_allocation"], (
            "count",
            "before",
            "after",
        ))
        before_allocation = cls._exact_mapping(allocation["before"], (
            "state_revision",
            "sequence",
            "previous_tail_sha256",
            "tail_sha256",
            "updated_at",
        ))
        after_allocation = cls._exact_mapping(allocation["after"], (
            "state_revision",
            "sequence",
            "previous_tail_sha256",
            "tail_sha256",
            "updated_at",
        ))
        outbox_count = counts[WorkerTargetKind.OUTBOX.value][0]
        origin_allocations = tuple(connection.execute(
            "SELECT sequence,previous_tail_sha256,allocation_tail_sha256,"
            "allocated_at FROM outbox_allocations WHERE commit_id=? "
            "ORDER BY sequence",
            (plan.target.origin_commit_id,),
        ).fetchall())
        if (
            allocation["count"] != outbox_count
            or len(origin_allocations) != outbox_count
            or before_allocation["sequence"] + outbox_count
            != after_allocation["sequence"]
            or after_allocation["state_revision"]
            != before_allocation["state_revision"] + (1 if outbox_count else 0)
            or after_allocation["updated_at"] != receipt[2]
            or (
                outbox_count > 0
                and (
                    origin_allocations[0][0]
                    != before_allocation["sequence"] + 1
                    or origin_allocations[-1][0] != after_allocation["sequence"]
                    or not _same(
                        origin_allocations[0][1],
                        cls._manifest_digest(before_allocation["tail_sha256"]),
                    )
                    or not _same(
                        origin_allocations[-1][2],
                        cls._manifest_digest(after_allocation["tail_sha256"]),
                    )
                    or not _same(
                        origin_allocations[0][1],
                        cls._manifest_digest(
                            after_allocation["previous_tail_sha256"]
                        ),
                    )
                    or any(row[3] != receipt[2] for row in origin_allocations)
                )
            )
        ):
            raise _ClaimCorrupt("worker_claim_origin_manifest_corrupt")

        expected_production_receipt = _domain_digest(
            PRODUCTION_ATOMIC_RECEIPT_HASH_DOMAIN,
            (
                plan.target.origin_commit_id.encode("ascii"),
                receipt[0].encode("utf-8"),
                receipt[1].encode("utf-8"),
                receipt[2].encode("ascii"),
                receipt[4],
                receipt[6],
                receipt[7].to_bytes(8, "big"),
                receipt[8],
            ),
        )
        if not _same(receipt[9], expected_production_receipt):
            raise _ClaimCorrupt("worker_claim_origin_receipt_corrupt")

    @staticmethod
    def _target_and_genesis_exact(
        connection: Any,
        plan: WorkerExecutionPlan,
        *,
        expect_pending_event: bool,
    ) -> str:
        target = plan.target
        order = target.outbox_order
        assert order is not None
        raw_target = connection.execute(
            """
            SELECT target_kind,target_id,genesis_status,genesis_state_sha256,
                   project_id,generation,document_id,origin_commit_id,source_ordinal,
                   payload_schema,payload_codec_id,payload_codec_version,
                   payload_bytes,payload_sha256,idempotency_scope,idempotency_key,
                   replay_policy,idempotency_policy,max_attempts,action_type,
                   pipeline_id,intent_type,outbox_topic,outbox_partition_key,
                   outbox_sequence,outbox_repository_revision,outbox_ordinal,
                   created_at
            FROM worker_targets WHERE target_id=?
            """,
            (target.target_id,),
        ).fetchone()
        if raw_target is None or len(raw_target) != 28:
            raise _ClaimCorrupt("worker_claim_target_missing")
        row = _OriginTargetRow(*tuple(raw_target))
        genesis = _production_worker_genesis_state_sha256(
            target,
            status=WorkerExecutionStatus.PENDING,
        )
        expected_prefix = (
            "outbox",
            target.target_id,
            "pending",
            genesis,
            target.project_id,
            target.generation,
            target.document_id,
            target.origin_commit_id,
            order.ordinal,
            target.payload.schema,
            target.payload.codec_id,
            target.payload.codec_version,
            target.payload.payload_bytes,
            target.payload.payload_sha256,
            target.idempotency_scope,
            target.idempotency_key,
            target.replay_policy.value,
            target.idempotency_policy.value,
            target.max_attempts,
            None,
            None,
            None,
        )
        if not _row_is(row[:22], expected_prefix) or not _row_is(row[24:27], (
            order.sequence,
            order.repository_revision,
            order.ordinal,
        )):
            raise _ClaimCorrupt("worker_claim_target_corrupt")
        created_at = row.created_at
        raw_event = connection.execute(
            """
            SELECT event_id,previous_event_id,previous_ordinal,commit_id,
                   repository_revision,commit_event_count,operation_id,ordinal,
                   topic,partition_key,event_bytes,event_sha256,payload_schema,
                   codec_id,codec_version,status,state_revision,attempt_count,
                   available_at,lease_owner,lease_fence,lease_expires_at,
                   published_at,outcome_unknown_at,dead_at,last_error_bytes,
                   last_error_sha256,created_at,updated_at
            FROM event_outbox WHERE event_id=?
            """,
            (target.target_id,),
        ).fetchone()
        if raw_event is None or len(raw_event) != 29:
            raise _ClaimCorrupt("worker_claim_event_corrupt")
        event = _OriginEventRow(*tuple(raw_event))
        if not _row_is((
            event.event_id,
            event.commit_id,
            event.repository_revision,
            event.ordinal,
            event.topic,
            event.partition_key,
            event.event_bytes,
            event.event_sha256,
            event.payload_schema,
            event.codec_id,
            event.codec_version,
        ), (
            target.target_id,
            target.origin_commit_id,
            order.repository_revision,
            order.ordinal,
            row.outbox_topic,
            row.outbox_partition_key,
            target.payload.payload_bytes,
            target.payload.payload_sha256,
            target.payload.schema,
            target.payload.codec_id,
            target.payload.codec_version,
        )):
            raise _ClaimCorrupt("worker_claim_event_corrupt")
        if event.created_at != created_at or (
            expect_pending_event
            and not _row_is((
                event.status,
                event.state_revision,
                event.attempt_count,
                event.available_at,
                event.lease_owner,
                event.lease_fence,
                event.lease_expires_at,
                event.published_at,
                event.outcome_unknown_at,
                event.dead_at,
                event.last_error_bytes,
                event.last_error_sha256,
                event.created_at,
                event.updated_at,
            ), (
                "pending",
                1,
                0,
                created_at,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                created_at,
                created_at,
            ))
        ):
            raise _ClaimCorrupt("worker_claim_event_corrupt")
        SQLiteWorkerClaimWriter._origin_event_projection_exact(
            connection,
            plan,
            target_event=event,
        )
        SQLiteWorkerClaimWriter._origin_production_target_exact(
            connection,
            plan,
            target_row=row,
            event_topic=event.topic,
            event_partition_key=event.partition_key,
            created_at=created_at,
        )
        allocation = connection.execute(
            """
            SELECT event_id,commit_id,repository_revision,ordinal,
                   previous_tail_sha256,allocation_tail_sha256,allocated_at
            FROM outbox_allocations WHERE sequence=?
            """,
            (order.sequence,),
        ).fetchone()
        if allocation is None:
            raise _ClaimCorrupt("worker_claim_allocation_missing")
        expected_tail = _production_outbox_allocation_tail(
            sequence=order.sequence,
            event_id=target.target_id,
            commit_id=target.origin_commit_id,
            repository_revision=order.repository_revision,
            ordinal=order.ordinal,
            previous_tail_sha256=allocation[4],
        )
        if not _row_is(allocation, (
            target.target_id,
            target.origin_commit_id,
            order.repository_revision,
            order.ordinal,
            allocation[4],
            expected_tail,
            created_at,
        )):
            raise _ClaimCorrupt("worker_claim_allocation_corrupt")
        return created_at

    @staticmethod
    def _allocation_chain_exact(connection: Any) -> bool:
        head = connection.execute(
            "SELECT state_revision,sequence,previous_tail_sha256,tail_sha256,"
            "updated_at FROM outbox_allocation_head WHERE singleton=1"
        ).fetchone()
        rows = tuple(connection.execute(
            "SELECT sequence,event_id,commit_id,repository_revision,ordinal,"
            "previous_tail_sha256,allocation_tail_sha256,allocated_at "
            "FROM outbox_allocations ORDER BY sequence"
        ).fetchall())
        if (
            head is None
            or len(rows) != head[1]
            or head[0] != head[1] + 1
        ):
            return False
        if not rows:
            return bool(
                head[0] == 1
                and _same(head[2], _ZERO_SHA256)
                and _same(head[3], _ZERO_SHA256)
            )
        previous = _ZERO_SHA256
        penultimate = _ZERO_SHA256
        for sequence, row in enumerate(rows, start=1):
            if row[0] != sequence or not _same(row[5], previous):
                return False
            expected = _production_outbox_allocation_tail(
                sequence=row[0],
                event_id=row[1],
                commit_id=row[2],
                repository_revision=row[3],
                ordinal=row[4],
                previous_tail_sha256=row[5],
            )
            if not _same(expected, row[6]):
                return False
            penultimate = previous
            previous = row[6]
        return bool(
            _same(head[2], penultimate)
            and _same(head[3], previous)
            and head[4] == rows[-1][7]
        )

    @staticmethod
    def _current_before_exact(connection: Any, plan: WorkerExecutionPlan) -> Any:
        row = connection.execute(
            """
            SELECT target_kind,status,state_revision,attempt_count,last_attempt_id,
                   last_attempt_number,current_attempt_id,current_attempt_number,
                   fence_high_water,cancellation_pending,next_attempt_at,lease_owner,
                   lease_fence,lease_expires_at,current_effect_id,
                   unresolved_effect_id,last_receipt_transaction_id,
                   previous_state_sha256,state_sha256,updated_at
            FROM worker_target_current WHERE target_id=?
            """,
            (plan.target.target_id,),
        ).fetchone()
        if row is None:
            raise _ClaimCorrupt("worker_claim_current_missing")
        if not _row_is(row[:16], (
            "outbox", "pending", 1, 0, None, None, None, None, 0, 0,
            None, None, None, None, None, None,
        )) or not _same(row[17], _ZERO_SHA256):
            raise _ClaimCorrupt("worker_claim_current_corrupt")
        genesis = _production_worker_genesis_state_sha256(
            plan.target,
            status=WorkerExecutionStatus.PENDING,
        )
        if not _same(row[18], genesis):
            raise _ClaimCorrupt("worker_claim_current_corrupt")
        return row

    @staticmethod
    def _direct_successor_exact(
        connection: Any,
        plan: WorkerExecutionPlan,
        dispatch: _DispatchHeadSnapshot,
    ) -> bool:
        order = plan.target.outbox_order
        assert order is not None
        row = connection.execute(
            "SELECT sequence,repository_revision,ordinal,event_id "
            "FROM outbox_allocations WHERE sequence>? ORDER BY sequence LIMIT 1",
            (dispatch.published_sequence,),
        ).fetchone()
        return _row_is(row, (
            order.sequence,
            order.repository_revision,
            order.ordinal,
            plan.target.target_id,
        ))

    @staticmethod
    def _require_live_database_time(
        connection: Any,
        plan: WorkerExecutionPlan,
    ) -> str:
        row = connection.execute(
            "SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')"
        ).fetchone()
        if (
            row is None
            or len(row) != 1
            or type(row[0]) is not str
            or len(row[0]) != 24
        ):
            raise _ClaimCorrupt("worker_claim_database_clock_corrupt")
        now = row[0]
        if plan.request.header.occurred_at > now:
            raise _ClaimConflict("worker_claim_time_future")
        if plan.request.attempt.lease_expires_at <= now:
            raise _ClaimConflict("worker_claim_lease_expired")
        return now

    @classmethod
    def _require_fresh_before(
        cls,
        connection: Any,
        profile: Any,
        plan: WorkerExecutionPlan,
    ) -> tuple[_WorkerHeadSnapshot, _DispatchHeadSnapshot, Any, str]:
        try:
            mounted = _binding_from_snapshot(
                connection,
                profile,
                _mounted_row(connection),
            )
        except SQLiteProductionAuthorityError as exc:
            raise _ClaimCorrupt("worker_claim_mount_corrupt") from exc
        if not cls._authority_exact(profile, mounted, plan):
            raise _ClaimConflict("worker_claim_authority_conflict")
        cls._require_live_database_time(connection, plan)
        created_at = cls._target_and_genesis_exact(
            connection,
            plan,
            expect_pending_event=True,
        )
        if not cls._allocation_chain_exact(connection):
            raise _ClaimCorrupt("worker_claim_allocation_head_corrupt")
        current = cls._current_before_exact(connection, plan)
        origin = connection.execute(
            "SELECT transaction_id,committed_at FROM atomic_business_write_receipts "
            "WHERE commit_id=?",
            (plan.target.origin_commit_id,),
        ).fetchone()
        if origin is None or not _row_is(origin, (current[16], created_at)):
            raise _ClaimCorrupt("worker_claim_genesis_receipt_corrupt")
        head = cls._read_worker_head(connection)
        dispatch = cls._read_dispatch_head(connection)
        exact_head, _entries = cls._ledger_chain(connection)
        if exact_head is None:
            raise _ClaimCorrupt("worker_claim_worker_ledger_corrupt")
        if not SQLiteProductionAtomicWriter._outbox_dispatch_head_exact(
            connection
        ):
            raise _ClaimCorrupt("worker_claim_dispatch_head_corrupt")
        if (
            head.sequence != plan.before_ledger.sequence
            or not _same(head.tail_sha256, plan.before_ledger.tail_sha256)
        ):
            raise _ClaimConflict("worker_claim_ledger_head_conflict")
        fence = plan.request.dispatch_fence
        assert fence is not None
        if dispatch.state() != fence.before:
            raise _ClaimConflict("worker_claim_dispatch_head_conflict")
        if not cls._direct_successor_exact(connection, plan, dispatch):
            raise _ClaimConflict("worker_claim_outbox_order_conflict")
        if plan.before != plan.request.header.before:
            raise _ClaimCorrupt("worker_claim_plan_before_corrupt")
        latest = max(
            profile.updated_at,
            mounted.evidence.mounted_at,
            created_at,
            current[19],
            head.updated_at,
            dispatch.updated_at,
        )
        if plan.request.header.occurred_at < latest:
            raise _ClaimConflict("worker_claim_time_stale")
        return head, dispatch, current, created_at

    @staticmethod
    def _identity_present(connection: Any, plan: WorkerExecutionPlan) -> bool:
        transaction_id = plan.expected_receipt.receipt_parent.transaction_id
        request_id = plan.closure.request_transaction_id
        row = connection.execute(
            "SELECT EXISTS(SELECT 1 FROM transaction_receipts "
            "WHERE transaction_id=?),"
            "EXISTS(SELECT 1 FROM worker_execution_receipts WHERE request_id=?),"
            "EXISTS(SELECT 1 FROM worker_ledger_entries WHERE transaction_id=?)",
            (transaction_id, request_id, transaction_id),
        ).fetchone()
        return row is not None and any(bool(value) for value in row)

    @staticmethod
    def _write(
        connection: Any,
        plan: WorkerExecutionPlan,
        head: _WorkerHeadSnapshot,
        dispatch: _DispatchHeadSnapshot,
        current: Any,
    ) -> None:
        request = plan.request
        receipt = plan.expected_receipt
        parent = receipt.receipt_parent
        after = plan.closure.after_state
        attempt = request.attempt
        fence = request.dispatch_fence
        assert fence is not None and fence.after.inflight is not None
        connection.execute(
            "INSERT INTO transaction_receipts(transaction_id,kind,committed_at) "
            "VALUES (?,?,?)",
            (parent.transaction_id, parent.kind, parent.committed_at),
        )
        connection.execute(
            """
            INSERT INTO worker_ledger_entries(
                sequence,entry_kind,transaction_id,receipt_kind,committed_at,
                previous_tail_sha256,ledger_tail_sha256,request_sha256,
                after_state_sha256,receipt_sha256,occurred_at
            ) VALUES (?,'worker_execution',?,?,?,?,?,?,?,?,?)
            """,
            (
                receipt.sequence,
                parent.transaction_id,
                parent.kind,
                parent.committed_at,
                receipt.previous_ledger_tail_sha256,
                receipt.ledger_tail_sha256,
                receipt.request_sha256,
                receipt.after_state_sha256,
                receipt.receipt_sha256,
                receipt.occurred_at,
            ),
        )
        connection.execute(
            """
            INSERT INTO worker_execution_receipts(
                request_id,transaction_id,entry_kind,receipt_kind,committed_at,
                operation,target_kind,target_id,before_status,after_status,
                before_state_revision,after_state_revision,sequence,
                previous_ledger_tail_sha256,ledger_tail_sha256,manifest_bytes,
                request_sha256,after_state_sha256,receipt_sha256
            ) VALUES (?,?,'worker_execution',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                receipt.request_transaction_id,
                parent.transaction_id,
                parent.kind,
                parent.committed_at,
                receipt.operation.value,
                receipt.target_kind.value,
                receipt.target_id,
                receipt.before_status.value,
                receipt.after_status.value,
                receipt.before_state_revision,
                receipt.after_state_revision,
                receipt.sequence,
                receipt.previous_ledger_tail_sha256,
                receipt.ledger_tail_sha256,
                plan.closure.manifest_bytes,
                receipt.request_sha256,
                receipt.after_state_sha256,
                receipt.receipt_sha256,
            ),
        )
        connection.execute(
            """
            INSERT INTO worker_attempts(
                attempt_id,target_id,attempt_number,previous_attempt_id,
                execution_fence,owner_id,started_at,receipt_transaction_id
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                attempt.attempt_id,
                plan.target.target_id,
                attempt.attempt_number,
                attempt.previous_attempt_id,
                attempt.execution_fence,
                attempt.lease_owner,
                attempt.started_at,
                parent.transaction_id,
            ),
        )
        changed = connection.execute(
            """
            UPDATE event_outbox
            SET status='leased',state_revision=2,attempt_count=1,
                lease_owner=?,lease_fence=?,lease_expires_at=?,updated_at=?
            WHERE event_id=? AND status='pending' AND state_revision=1 AND
                  attempt_count=0 AND lease_owner IS NULL AND
                  lease_fence IS NULL AND lease_expires_at IS NULL AND
                  published_at IS NULL AND outcome_unknown_at IS NULL AND
                  dead_at IS NULL AND last_error_bytes IS NULL AND
                  last_error_sha256 IS NULL AND updated_at=?
            """,
            (
                after.lease_owner,
                after.lease_fence,
                after.lease_expires_at,
                receipt.occurred_at,
                plan.target.target_id,
                current[19],
            ),
        ).rowcount
        if changed != 1:
            raise _ClaimConflict("worker_claim_event_cas_conflict")
        changed = connection.execute(
            """
            UPDATE worker_target_current
            SET status='leased',state_revision=2,attempt_count=1,
                current_attempt_id=?,current_attempt_number=1,
                fence_high_water=?,lease_owner=?,lease_fence=?,
                lease_expires_at=?,last_receipt_transaction_id=?,
                previous_state_sha256=state_sha256,state_sha256=?,updated_at=?
            WHERE target_id=? AND target_kind='outbox' AND status='pending' AND
                  state_revision=1 AND attempt_count=0 AND
                  current_attempt_id IS NULL AND current_attempt_number IS NULL AND
                  fence_high_water=0 AND cancellation_pending=0 AND
                  next_attempt_at IS NULL AND lease_owner IS NULL AND
                  lease_fence IS NULL AND lease_expires_at IS NULL AND
                  current_effect_id IS NULL AND unresolved_effect_id IS NULL AND
                  last_receipt_transaction_id=? AND previous_state_sha256=? AND
                  state_sha256=? AND updated_at=?
            """,
            (
                after.current_attempt_id,
                after.fence_high_water,
                after.lease_owner,
                after.lease_fence,
                after.lease_expires_at,
                parent.transaction_id,
                receipt.after_state_sha256,
                receipt.occurred_at,
                plan.target.target_id,
                current[16],
                current[17],
                current[18],
                current[19],
            ),
        ).rowcount
        if changed != 1:
            raise _ClaimConflict("worker_claim_current_cas_conflict")
        after_dispatch = fence.after
        inflight = after_dispatch.inflight
        changed = connection.execute(
            """
            UPDATE outbox_dispatch_head
            SET state_revision=?,published_sequence=?,
                published_repository_revision=?,published_ordinal=?,
                inflight_sequence=?,inflight_repository_revision=?,
                inflight_ordinal=?,previous_head_sha256=head_sha256,
                head_sha256=?,updated_at=?
            WHERE singleton=1 AND state_revision=? AND published_sequence=? AND
                  published_repository_revision IS ? AND published_ordinal IS ? AND
                  inflight_sequence IS NULL AND inflight_repository_revision IS NULL AND
                  inflight_ordinal IS NULL AND previous_head_sha256=? AND
                  head_sha256=? AND updated_at=?
            """,
            (
                after_dispatch.control_revision,
                after_dispatch.published.sequence,
                (
                    None
                    if after_dispatch.published.sequence == 0
                    else after_dispatch.published.repository_revision
                ),
                (
                    None
                    if after_dispatch.published.sequence == 0
                    else after_dispatch.published.ordinal
                ),
                inflight.sequence,
                inflight.repository_revision,
                inflight.ordinal,
                receipt.after_state_sha256,
                receipt.occurred_at,
                dispatch.state_revision,
                dispatch.published_sequence,
                dispatch.published_repository_revision,
                dispatch.published_ordinal,
                dispatch.previous_head_sha256,
                dispatch.head_sha256,
                dispatch.updated_at,
            ),
        ).rowcount
        if changed != 1:
            raise _ClaimConflict("worker_claim_dispatch_cas_conflict")
        changed = connection.execute(
            """
            UPDATE worker_ledger_head
            SET state_revision=?,sequence=?,previous_tail_sha256=tail_sha256,
                tail_sha256=?,updated_at=?
            WHERE singleton=1 AND state_revision=? AND sequence=? AND
                  previous_tail_sha256=? AND tail_sha256=? AND updated_at=?
            """,
            (
                head.state_revision + 1,
                plan.after_ledger.sequence,
                plan.after_ledger.tail_sha256,
                receipt.occurred_at,
                head.state_revision,
                head.sequence,
                head.previous_tail_sha256,
                head.tail_sha256,
                head.updated_at,
            ),
        ).rowcount
        if changed != 1:
            raise _ClaimConflict("worker_claim_ledger_cas_conflict")

    @staticmethod
    def _ledger_chain(
        connection: Any,
    ) -> tuple[Optional[_WorkerHeadSnapshot], tuple[Any, ...]]:
        try:
            head = SQLiteWorkerClaimWriter._read_worker_head(connection)
        except _ClaimCorrupt:
            return None, ()
        rows = tuple(connection.execute(
            """
            SELECT entry.sequence,entry.entry_kind,entry.transaction_id,
                   entry.receipt_kind,entry.committed_at,
                   entry.previous_tail_sha256,entry.ledger_tail_sha256,
                   entry.request_sha256,entry.after_state_sha256,
                   entry.receipt_sha256,entry.occurred_at,
                   EXISTS(SELECT 1 FROM transaction_receipts AS parent
                          WHERE parent.transaction_id=entry.transaction_id AND
                                parent.kind=entry.receipt_kind AND
                                parent.committed_at=entry.committed_at),
                   CASE entry.entry_kind
                     WHEN 'worker_execution' THEN EXISTS(
                       SELECT 1 FROM worker_execution_receipts AS child
                       WHERE child.sequence=entry.sequence AND
                             child.transaction_id=entry.transaction_id AND
                             child.receipt_kind=entry.receipt_kind AND
                             child.committed_at=entry.committed_at AND
                             child.previous_ledger_tail_sha256=entry.previous_tail_sha256 AND
                             child.ledger_tail_sha256=entry.ledger_tail_sha256 AND
                             child.request_sha256=entry.request_sha256 AND
                             child.after_state_sha256=entry.after_state_sha256 AND
                             child.receipt_sha256=entry.receipt_sha256)
                     WHEN 'pipeline_control' THEN EXISTS(
                       SELECT 1 FROM pipeline_control_receipts AS child
                       WHERE child.sequence=entry.sequence AND
                             child.transaction_id=entry.transaction_id AND
                             child.receipt_kind=entry.receipt_kind AND
                             child.committed_at=entry.committed_at AND
                             child.previous_ledger_tail_sha256=entry.previous_tail_sha256 AND
                             child.ledger_tail_sha256=entry.ledger_tail_sha256 AND
                             child.request_sha256=entry.request_sha256 AND
                             child.after_state_sha256=entry.after_state_sha256 AND
                             child.receipt_sha256=entry.receipt_sha256)
                     ELSE 0 END
            FROM worker_ledger_entries AS entry ORDER BY entry.sequence
            """
        ).fetchall())
        if len(rows) != head.sequence:
            return None, rows
        previous = _ZERO_SHA256
        for sequence, row in enumerate(rows, start=1):
            if (
                row[0] != sequence
                or row[4] != row[10]
                or row[11] != 1
                or row[12] != 1
                or not _same(row[5], previous)
                or not _same(
                    _ledger_tail_digest(sequence, previous, row[9]),
                    row[6],
                )
            ):
                return None, rows
            previous = row[6]
        if not _same(previous, head.tail_sha256):
            return None, rows
        if not rows and not (
            head.state_revision == 1
            and _same(head.previous_tail_sha256, _ZERO_SHA256)
            and _same(head.tail_sha256, _ZERO_SHA256)
        ):
            return None, rows
        if rows:
            direct_previous = _ZERO_SHA256 if len(rows) == 1 else rows[-2][6]
            if (
                head.state_revision != head.sequence + 1
                or head.updated_at != rows[-1][10]
                or not _same(head.previous_tail_sha256, direct_previous)
                or _same(head.previous_tail_sha256, head.tail_sha256)
            ):
                return None, rows
        return head, rows

    @classmethod
    def _receipt_classification(
        cls,
        connection: Any,
        plan: WorkerExecutionPlan,
    ) -> tuple[WorkerReceiptReconcileStatus, str]:
        parent_id = plan.expected_receipt.receipt_parent.transaction_id
        parent_row = connection.execute(
            "SELECT kind,committed_at FROM transaction_receipts "
            "WHERE transaction_id=?",
            (parent_id,),
        ).fetchone()
        row = connection.execute(
            """
            SELECT request_id,transaction_id,receipt_kind,committed_at,operation,
                   target_kind,target_id,before_status,after_status,
                   before_state_revision,after_state_revision,sequence,
                   previous_ledger_tail_sha256,ledger_tail_sha256,manifest_bytes,
                   request_sha256,after_state_sha256,receipt_sha256
            FROM worker_execution_receipts WHERE request_id=?
            """,
            (plan.closure.request_transaction_id,),
        ).fetchone()
        head, entries = cls._ledger_chain(connection)
        if head is None:
            return WorkerReceiptReconcileStatus.CORRUPT, "worker_claim_ledger_corrupt"
        if parent_row is None and row is None:
            occupied = connection.execute(
                "SELECT 1 FROM worker_ledger_entries WHERE transaction_id=?",
                (parent_id,),
            ).fetchone()
            return (
                (WorkerReceiptReconcileStatus.ABSENT, "worker_claim_absent")
                if occupied is None
                else (
                    WorkerReceiptReconcileStatus.CORRUPT,
                    "worker_claim_receipt_pair_incomplete",
                )
            )
        if parent_row is not None and parent_row[0] != plan.expected_receipt.receipt_parent.kind:
            return WorkerReceiptReconcileStatus.CONFLICT, "worker_claim_identity_conflict"
        if parent_row is None or row is None:
            return WorkerReceiptReconcileStatus.CORRUPT, "worker_claim_receipt_pair_incomplete"
        if (
            row[1] != parent_id
            or row[2] != parent_row[0]
            or row[3] != parent_row[1]
        ):
            return WorkerReceiptReconcileStatus.CORRUPT, "worker_claim_receipt_parent_mismatch"
        try:
            parent = WorkerExecutionGenericReceiptEvidence(
                parent_id,
                parent_row[0],
                parent_row[1],
            )
            specialised = WorkerExecutionReceiptEvidence(
                request_transaction_id=row[0],
                receipt_parent=parent,
                request_class=WORKER_EXECUTION_REQUEST_CLASS,
                operation=WorkerExecutionOperation(row[4]),
                target_kind=WorkerTargetKind(row[5]),
                target_id=row[6],
                before_status=WorkerExecutionStatus(row[7]),
                after_status=WorkerExecutionStatus(row[8]),
                before_state_revision=row[9],
                after_state_revision=row[10],
                sequence=row[11],
                previous_ledger_tail_sha256=row[12],
                ledger_tail_sha256=row[13],
                request_sha256=row[15],
                after_state_sha256=row[16],
                occurred_at=row[3],
                receipt_sha256=row[17],
            )
            membership = None
            if head.sequence > specialised.sequence:
                matching = tuple(
                    value
                    for value in entries
                    if value[0] == specialised.sequence
                    and value[2] == parent_id
                    and _same(value[6], specialised.ledger_tail_sha256)
                )
                if len(matching) != 1:
                    raise ValueError("worker receipt is not a ledger member")
                membership = WorkerLedgerMembershipEvidence(
                    specialised.sequence,
                    specialised.ledger_tail_sha256,
                    WorkerLedgerHead(head.sequence, head.tail_sha256),
                )
            observed = WorkerExecutionPersistedObservation(
                WorkerPersistedReadStatus.COMPLETE,
                receipt=parent,
                execution_receipt=specialised,
                ledger_head=WorkerLedgerHead(head.sequence, head.tail_sha256),
                ledger_membership=membership,
            )
            fresh = _stamp_worker_execution_fresh_observation(
                _WORKER_EXECUTION_DURABLE_READER_CAPABILITY,
                observed,
            )
            classified = _classify_worker_execution_receipt(plan, fresh)
        except (TypeError, ValueError, WorkerExecutionIntegrityError):
            return WorkerReceiptReconcileStatus.CORRUPT, "worker_claim_receipt_corrupt"
        if classified.status is WorkerReceiptReconcileStatus.EXACT:
            if row[14] != plan.closure.manifest_bytes:
                return (
                    WorkerReceiptReconcileStatus.CONFLICT,
                    "worker_claim_manifest_conflict",
                )
            return classified.status, "worker_claim_receipt_exact"
        return classified.status, classified.error_code

    @classmethod
    def _after_closure_exact(
        cls,
        connection: Any,
        plan: WorkerExecutionPlan,
    ) -> bool:
        request = plan.request
        attempt = request.attempt
        receipt = plan.expected_receipt
        after = plan.closure.after_state
        attempt_row = connection.execute(
            "SELECT target_id,attempt_number,previous_attempt_id,execution_fence,"
            "owner_id,started_at,receipt_transaction_id FROM worker_attempts "
            "WHERE attempt_id=?",
            (attempt.attempt_id,),
        ).fetchone()
        if not _row_is(attempt_row, (
            plan.target.target_id,
            1,
            None,
            attempt.execution_fence,
            attempt.lease_owner,
            receipt.occurred_at,
            receipt.receipt_parent.transaction_id,
        )):
            return False
        current = connection.execute(
            """
            SELECT target_kind,status,state_revision,attempt_count,last_attempt_id,
                   last_attempt_number,current_attempt_id,current_attempt_number,
                   fence_high_water,cancellation_pending,next_attempt_at,lease_owner,
                   lease_fence,lease_expires_at,current_effect_id,
                   unresolved_effect_id,last_receipt_transaction_id,
                   previous_state_sha256,state_sha256,updated_at
            FROM worker_target_current WHERE target_id=?
            """,
            (plan.target.target_id,),
        ).fetchone()
        genesis = _production_worker_genesis_state_sha256(
            plan.target,
            status=WorkerExecutionStatus.PENDING,
        )
        if not _row_is(current, (
            "outbox",
            "leased",
            2,
            1,
            None,
            None,
            attempt.attempt_id,
            1,
            after.fence_high_water,
            0,
            None,
            after.lease_owner,
            after.lease_fence,
            after.lease_expires_at,
            None,
            None,
            receipt.receipt_parent.transaction_id,
            genesis,
            receipt.after_state_sha256,
            receipt.occurred_at,
        )):
            return False
        event = connection.execute(
            "SELECT status,state_revision,attempt_count,available_at,lease_owner,"
            "lease_fence,lease_expires_at,published_at,outcome_unknown_at,dead_at,"
            "last_error_bytes,last_error_sha256,created_at,updated_at "
            "FROM event_outbox WHERE event_id=?",
            (plan.target.target_id,),
        ).fetchone()
        if event is None or not _row_is(event, (
            "leased",
            2,
            1,
            event[12],
            after.lease_owner,
            after.lease_fence,
            after.lease_expires_at,
            None,
            None,
            None,
            None,
            None,
            event[12],
            receipt.occurred_at,
        )):
            return False
        dispatch = cls._read_dispatch_head(connection)
        fence = request.dispatch_fence
        assert fence is not None
        if (
            dispatch.state() != fence.after
            or not _same(dispatch.head_sha256, receipt.after_state_sha256)
            or dispatch.updated_at != receipt.occurred_at
            or not SQLiteProductionAtomicWriter._outbox_dispatch_head_exact(
                connection
            )
        ):
            return False
        head = cls._read_worker_head(connection)
        if (
            head.sequence != plan.after_ledger.sequence
            or not _same(
                head.previous_tail_sha256,
                plan.before_ledger.tail_sha256,
            )
            or not _same(head.tail_sha256, plan.after_ledger.tail_sha256)
            or head.updated_at != receipt.occurred_at
        ):
            return False
        counts = connection.execute(
            "SELECT (SELECT count(*) FROM worker_attempts WHERE target_id=?),"
            "(SELECT count(*) FROM worker_effects WHERE target_id=?),"
            "(SELECT count(*) FROM worker_results WHERE target_id=?),"
            "(SELECT count(*) FROM worker_reconcile_proofs WHERE target_id=?)",
            (plan.target.target_id,) * 4,
        ).fetchone()
        return _row_is(counts, (1, 0, 0, 0))

    @classmethod
    def _absent_closure_status(
        cls,
        connection: Any,
        plan: WorkerExecutionPlan,
    ) -> tuple[SQLiteWorkerClaimStatus, str]:
        """ABSENT is final only while every owned surface is still genesis."""

        attempt_id = plan.request.attempt.attempt_id
        receipt_transaction_id = (
            plan.expected_receipt.receipt_parent.transaction_id
        )
        occupied = connection.execute(
            """
            SELECT
              EXISTS(
                SELECT 1 FROM worker_attempts
                WHERE attempt_id=? OR previous_attempt_id=?),
              EXISTS(
                SELECT 1 FROM worker_target_current
                WHERE last_attempt_id=? OR current_attempt_id=?),
              EXISTS(
                SELECT 1 FROM worker_effects WHERE attempt_id=?),
              EXISTS(
                SELECT 1 FROM worker_results WHERE attempt_id=?),
              EXISTS(
                SELECT 1 FROM worker_attempts WHERE receipt_transaction_id=?),
              EXISTS(
                SELECT 1 FROM worker_target_current
                WHERE last_receipt_transaction_id=?),
              EXISTS(
                SELECT 1 FROM worker_effects
                WHERE authorization_transaction_id=?),
              EXISTS(
                SELECT 1 FROM worker_results WHERE receipt_transaction_id=?),
              EXISTS(
                SELECT 1 FROM worker_reconcile_proofs
                WHERE receipt_transaction_id=?),
              EXISTS(
                SELECT 1 FROM worker_execution_receipts
                WHERE transaction_id=? OR request_id=?),
              EXISTS(
                SELECT 1 FROM worker_ledger_entries WHERE transaction_id=?),
              EXISTS(
                SELECT 1 FROM transaction_receipts WHERE transaction_id=?)
            """,
            (
                attempt_id,
                attempt_id,
                attempt_id,
                attempt_id,
                attempt_id,
                attempt_id,
                receipt_transaction_id,
                receipt_transaction_id,
                receipt_transaction_id,
                receipt_transaction_id,
                receipt_transaction_id,
                receipt_transaction_id,
                plan.closure.request_transaction_id,
                receipt_transaction_id,
                receipt_transaction_id,
            ),
        ).fetchone()
        if occupied is None or len(occupied) != 12:
            raise _ClaimCorrupt("worker_claim_absent_identity_unreadable")
        if any(value != 0 for value in occupied):
            raise _ClaimCorrupt("worker_claim_absent_identity_occupied")

        head, _entries = cls._ledger_chain(connection)
        if head is None:
            raise _ClaimCorrupt("worker_claim_ledger_corrupt")
        if head.sequence > 0:
            # Another durable worker fact now owns the global chronology.  V8
            # cannot plan-free rebuild that suffix, so absence is not final.
            return (
                SQLiteWorkerClaimStatus.UNKNOWN,
                "worker_claim_absent_ledger_advanced",
            )
        if (
            head.state_revision != 1
            or not _same(head.previous_tail_sha256, _ZERO_SHA256)
            or not _same(head.tail_sha256, _ZERO_SHA256)
        ):
            raise _ClaimCorrupt("worker_claim_ledger_corrupt")

        created_at = cls._target_and_genesis_exact(
            connection,
            plan,
            expect_pending_event=True,
        )
        if not cls._allocation_chain_exact(connection):
            raise _ClaimCorrupt("worker_claim_allocation_head_corrupt")
        current = cls._current_before_exact(connection, plan)
        origin = connection.execute(
            "SELECT transaction_id,committed_at "
            "FROM atomic_business_write_receipts WHERE commit_id=?",
            (plan.target.origin_commit_id,),
        ).fetchone()
        if origin is None or not _row_is(origin, (current[16], created_at)):
            raise _ClaimCorrupt("worker_claim_genesis_receipt_corrupt")

        dispatch = cls._read_dispatch_head(connection)
        fence = plan.request.dispatch_fence
        assert fence is not None
        if (
            dispatch.state() != fence.before
            or not SQLiteProductionAtomicWriter._outbox_dispatch_head_exact(
                connection
            )
            or not cls._direct_successor_exact(connection, plan, dispatch)
        ):
            raise _ClaimCorrupt("worker_claim_absent_dispatch_changed")
        counts = connection.execute(
            "SELECT "
            "(SELECT count(*) FROM worker_attempts WHERE target_id=?),"
            "(SELECT count(*) FROM worker_effects WHERE target_id=?),"
            "(SELECT count(*) FROM worker_results WHERE target_id=?),"
            "(SELECT count(*) FROM worker_reconcile_proofs WHERE target_id=?)",
            (plan.target.target_id,) * 4,
        ).fetchone()
        if not _row_is(counts, (0, 0, 0, 0)):
            raise _ClaimCorrupt("worker_claim_absent_partial_state")
        return SQLiteWorkerClaimStatus.NOT_COMMITTED, "worker_claim_absent"

    def _reconcile_snapshot(
        self,
        connection: Any,
        plan: WorkerExecutionPlan,
        *,
        validate_runtime: bool = True,
    ) -> SQLiteWorkerClaimResult:
        if validate_runtime:
            try:
                profile = _validate_business_runtime_connection(
                    self._database,
                    connection,
                    access=BusinessRuntimeAccess.READER,
                )
                mounted = _binding_from_snapshot(
                    connection,
                    profile,
                    _mounted_row(connection),
                )
            except SQLiteProductionAuthorityError:
                return self._result(
                    plan,
                    SQLiteWorkerClaimStatus.CORRUPT,
                    "worker_claim_mount_corrupt",
                )
            except SQLiteKernelError as exc:
                if exc.code == "sqlite_business_runtime_unreadable":
                    return self._result(
                        plan,
                        SQLiteWorkerClaimStatus.UNKNOWN,
                        "worker_claim_runtime_unavailable",
                    )
                return self._result(
                    plan,
                    SQLiteWorkerClaimStatus.CORRUPT,
                    "worker_claim_runtime_corrupt",
                )
            except (OSError, sqlite3.Error):
                return self._result(
                    plan,
                    SQLiteWorkerClaimStatus.UNKNOWN,
                    "worker_claim_runtime_or_mount_unavailable",
                )
            except Exception:
                return self._result(
                    plan,
                    SQLiteWorkerClaimStatus.UNKNOWN,
                    "worker_claim_runtime_or_mount_unavailable",
                )
            if not self._authority_descends(profile, mounted, plan):
                return self._result(
                    plan,
                    SQLiteWorkerClaimStatus.CONFLICT,
                    "worker_claim_authority_conflict",
                )
        status, code = self._receipt_classification(connection, plan)
        if status is WorkerReceiptReconcileStatus.ABSENT:
            try:
                absent_status, absent_code = self._absent_closure_status(
                    connection,
                    plan,
                )
            except _ClaimCorrupt as exc:
                return self._result(
                    plan,
                    SQLiteWorkerClaimStatus.CORRUPT,
                    str(exc) or "worker_claim_absent_closure_corrupt",
                )
            except (TypeError, ValueError, IndexError, OverflowError):
                return self._result(
                    plan,
                    SQLiteWorkerClaimStatus.CORRUPT,
                    "worker_claim_absent_closure_corrupt",
                )
            return self._result(plan, absent_status, absent_code)
        if status is WorkerReceiptReconcileStatus.CONFLICT:
            return self._result(plan, SQLiteWorkerClaimStatus.CONFLICT, code)
        if status is WorkerReceiptReconcileStatus.UNKNOWN:
            return self._result(plan, SQLiteWorkerClaimStatus.UNKNOWN, code)
        if status is not WorkerReceiptReconcileStatus.EXACT:
            return self._result(plan, SQLiteWorkerClaimStatus.CORRUPT, code)
        try:
            self._target_and_genesis_exact(
                connection,
                plan,
                expect_pending_event=False,
            )
            allocation_exact = self._allocation_chain_exact(connection)
            head = self._read_worker_head(connection)
            if head.sequence > plan.after_ledger.sequence:
                # The receipt classifier proved this claim is a ledger member,
                # but schema-v8 cannot rebuild the suffix request semantics.
                return self._result(
                    plan,
                    SQLiteWorkerClaimStatus.UNKNOWN,
                    "worker_claim_ledger_suffix_unreconstructable",
                )
            after_exact = self._after_closure_exact(connection, plan)
        except _ClaimCorrupt as exc:
            return self._result(
                plan,
                SQLiteWorkerClaimStatus.CORRUPT,
                str(exc) or "worker_claim_durable_closure_corrupt",
            )
        except (TypeError, ValueError, IndexError, OverflowError):
            return self._result(
                plan,
                SQLiteWorkerClaimStatus.CORRUPT,
                "worker_claim_durable_closure_corrupt",
            )
        if not allocation_exact or not after_exact:
            return self._result(
                plan,
                SQLiteWorkerClaimStatus.CORRUPT,
                "worker_claim_durable_closure_corrupt",
            )
        return self._result(
            plan,
            SQLiteWorkerClaimStatus.COMMITTED,
            "worker_claim_exact",
        )

    def reconcile(self, plan: WorkerExecutionPlan) -> SQLiteWorkerClaimResult:
        plan = _require_worker_execution_plan(plan)
        self._require_vertical(plan)
        connection = None
        try:
            connection, _filesystem = self._database._open_unvalidated()
            connection.execute("BEGIN")
            result = self._reconcile_snapshot(connection, plan)
        except Exception:
            if connection is not None:
                _rollback_close(connection)
            return self._result(
                plan,
                SQLiteWorkerClaimStatus.UNKNOWN,
                "worker_claim_reconcile_unavailable",
            )
        else:
            _rollback_close(connection)
        if (
            result.status is SQLiteWorkerClaimStatus.UNKNOWN
            and result.code == "worker_claim_ledger_suffix_unreconstructable"
        ):
            result = self._reconcile_ledger_suffix(plan)
        if result.status is SQLiteWorkerClaimStatus.COMMITTED:
            try:
                self._database._finalize_committed_storage()
            except Exception:
                return self._result(
                    plan,
                    SQLiteWorkerClaimStatus.UNKNOWN,
                    "worker_claim_durability_barrier_failed",
                )
        return result

    def commit(
        self,
        plan: WorkerExecutionPlan,
        *,
        _test_hook: Optional[Callable[[SQLiteWorkerClaimTestPoint], None]] = None,
    ) -> SQLiteWorkerClaimResult:
        plan = _require_worker_execution_plan(plan)
        self._require_vertical(plan)
        if _test_hook is not None and not callable(_test_hook):
            raise TypeError("_test_hook must be callable")
        connection = None
        committed_started = False
        identity_absent = False
        try:
            connection, _filesystem = self._database._open_unvalidated()
            connection.execute("BEGIN IMMEDIATE")
            profile = _validate_business_runtime_connection(
                self._database,
                connection,
                access=BusinessRuntimeAccess.WRITER,
            )
            if self._identity_present(connection, plan):
                _rollback_close(connection)
                connection = None
                result = self.reconcile(plan)
                if result.status is SQLiteWorkerClaimStatus.COMMITTED:
                    return SQLiteWorkerClaimResult(
                        result.status,
                        result.request_id,
                        result.code,
                        True,
                    )
                return result
            identity_absent = True
            head, dispatch, current, _created_at = self._require_fresh_before(
                connection,
                profile,
                plan,
            )
            authorizer = _WorkerClaimAuthorizer()
            connection.set_authorizer(authorizer)
            self._write(connection, plan, head, dispatch, current)
            if authorizer.denied:
                raise _ClaimCorrupt("worker_claim_authorizer_denied")
            verified = self._reconcile_snapshot(
                connection,
                plan,
                validate_runtime=False,
            )
            if verified.status is not SQLiteWorkerClaimStatus.COMMITTED:
                raise _ClaimCorrupt("worker_claim_precommit_closure_mismatch")
            if _test_hook is not None:
                _test_hook(SQLiteWorkerClaimTestPoint.BEFORE_COMMIT)
            plan = _require_worker_execution_plan(plan)
            if authorizer.denied:
                raise _ClaimCorrupt("worker_claim_authorizer_denied")
            final_verified = self._reconcile_snapshot(
                connection,
                plan,
                validate_runtime=False,
            )
            if final_verified.status is not SQLiteWorkerClaimStatus.COMMITTED:
                raise _ClaimCorrupt("worker_claim_final_closure_mismatch")
            # SQLite's transaction lock protects rows, not the passage of
            # wall-clock time.  Re-read database time after the final closure
            # and immediately before COMMIT so hook/precommit work cannot
            # knowingly publish an already-expired lease.
            self._require_live_database_time(connection, plan)
            if authorizer.denied:
                raise _ClaimCorrupt("worker_claim_authorizer_denied")
            connection.set_authorizer(_allow_authorizer)
            committed_started = True
            connection.commit()
            if _test_hook is not None:
                _test_hook(SQLiteWorkerClaimTestPoint.AFTER_COMMIT_BEFORE_ACK)
            connection.close()
            connection = None
            self._database._finalize_committed_storage()
            return self._result(
                plan,
                SQLiteWorkerClaimStatus.COMMITTED,
                "worker_claim_committed",
            )
        except (_ClaimConflict,) as failure:
            rollback_complete = True
            if connection is not None:
                rollback_complete = _rollback_close(connection)
                connection = None
            if not rollback_complete:
                reconciled = self.reconcile(plan)
                if reconciled.status is SQLiteWorkerClaimStatus.COMMITTED:
                    return SQLiteWorkerClaimResult(
                        reconciled.status,
                        reconciled.request_id,
                        reconciled.code,
                        True,
                    )
                return reconciled
            return self._result(
                plan,
                SQLiteWorkerClaimStatus.CONFLICT,
                str(failure) or "worker_claim_conflict",
            )
        except _ClaimCorrupt as failure:
            rollback_complete = True
            if connection is not None:
                rollback_complete = _rollback_close(connection)
                connection = None
            if not rollback_complete:
                reconciled = self.reconcile(plan)
                if reconciled.status is SQLiteWorkerClaimStatus.COMMITTED:
                    return SQLiteWorkerClaimResult(
                        reconciled.status,
                        reconciled.request_id,
                        reconciled.code,
                        True,
                    )
                return reconciled
            return self._result(
                plan,
                SQLiteWorkerClaimStatus.CORRUPT,
                str(failure) or "worker_claim_corrupt",
            )
        except BaseException as failure:
            process_control = not isinstance(failure, Exception)
            if not committed_started:
                if connection is not None:
                    rollback_complete = _rollback_close(connection)
                    connection = None
                else:
                    rollback_complete = True
                if process_control:
                    raise
                if rollback_complete and identity_absent:
                    reconciled = self.reconcile(plan)
                    if reconciled.status is SQLiteWorkerClaimStatus.NOT_COMMITTED:
                        return self._result(
                            plan,
                            SQLiteWorkerClaimStatus.NOT_COMMITTED,
                            "worker_claim_rolled_back",
                        )
                    return reconciled
            else:
                if connection is not None:
                    _close_quietly(connection)
                    connection = None
            if process_control:
                raise
            reconciled = self.reconcile(plan)
            if reconciled.status is SQLiteWorkerClaimStatus.COMMITTED:
                return SQLiteWorkerClaimResult(
                    reconciled.status,
                    reconciled.request_id,
                    reconciled.code,
                    True,
                )
            return reconciled


__all__ = ()
