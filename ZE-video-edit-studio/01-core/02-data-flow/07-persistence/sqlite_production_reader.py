"""Plan-free rooted reader for one exact production atomic commit.

This boundary deliberately accepts no ``ProductionAtomicWritePlan``.  It
pins one SQLite snapshot, reuses the historical business-head proof on that
same connection, and rebuilds the production receipt, targets, pipeline
controls, worker-ledger membership and outbox-allocation inclusion from
durable rows alone.

The first version is intentionally an *exact-at-commit* scheduler reader.
Outbox-allocation heads may have legal descendants and are proved by walking
their hash chain.  Worker current/ledger/dispatch progress is not yet replayed
into a descendant proof: a valid later worker state is reported with the
stable ``sqlite_production_read_progress_unsupported`` code, never as
corruption.
"""

from __future__ import annotations

import hmac
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Sequence, Tuple

from business_atomic_write_contract import (
    ATOMIC_BUSINESS_WRITE_MANIFEST_SCHEMA,
    ATOMIC_BUSINESS_WRITE_PLAN_STATUS,
    ATOMIC_BUSINESS_WRITE_PROTOCOL,
    AtomicBusinessWriteGenericReceiptEvidence,
)
from business_atomic_write_integrity import (
    ATOMIC_BUSINESS_WRITE_CONTROLS_HASH_DOMAIN,
    _atomic_receipt_digest,
    _digest as _atomic_digest,
    _manifest_digest as _atomic_manifest_digest,
)
from business_codec import (
    BusinessCodecError,
    decode_payload_bounded,
    encode_payload_bounded,
)
from business_transaction_contract import FrozenJsonPayload
from business_transaction_contract import MAX_ATOMIC_TOTAL_AUXILIARY_BYTES
from business_transaction_integrity import (
    _component_digest as _business_component_digest,
    _component_part as _business_component_part,
)
from production_atomic_write_contract import (
    MAX_PRODUCTION_ATOMIC_TARGETS,
    MAX_PRODUCTION_ATOMIC_MANIFEST_BYTES,
    MAX_PRODUCTION_ATOMIC_MANIFEST_NODES,
    PRODUCTION_OUTBOX_IDEMPOTENCY_POLICY,
    PRODUCTION_OUTBOX_MAX_ATTEMPTS,
    PRODUCTION_OUTBOX_REPLAY_POLICY,
    PRODUCTION_ATOMIC_TARGET_GROUP_ORDER,
    PRODUCTION_ATOMIC_WRITE_MANIFEST_SCHEMA,
    PRODUCTION_ATOMIC_WRITE_PROTOCOL,
    PRODUCTION_ATOMIC_WRITE_STATUS,
    ProductionMountedAuthorityEvidence,
    ProductionOutboxAllocationHead,
    ProductionOutboxDispatchHeadEvidence,
    ProductionWorkerLedgerHeadEvidence,
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
    _allocation_head_manifest,
    _base_target_manifest,
    _domain_digest as _production_domain_digest,
    _groups,
    _mount_manifest,
    _outbox_dispatch_head_manifest,
    _production_atomic_control_member_sha256,
    _production_outbox_allocation_tail,
    _production_worker_genesis_state_sha256,
    _worker_ledger_head_manifest,
)
from sqlite_business_reader import (
    SQLiteBusinessHead,
    SQLiteBusinessReadError,
    _ReadOnlyAuthorizer,
    _finish_read_connection,
    _publish_fence_is_compatible,
    _read_actions,
    _manifest_digest as _business_manifest_digest,
    _read_outbox,
    _read_pinned,
    _read_pipelines,
)
from sqlite_business_runtime import (
    BusinessAuthorityState,
    BusinessRuntimeAccess,
    BusinessRuntimeProfile,
    _validate_business_runtime_connection,
)
from sqlite_kernel import SQLiteKernelError, SqliteDatabase
from sqlite_production_authority import _binding_from_snapshot, _mounted_row
from sqlite_worker_durable_audit import (
    SQLiteWorkerDurableAuditStatus,
    _AuditBudget,
    _AuditConflict,
    _AuditCorrupt,
    _AuditUnsupported,
    _audit_snapshot as _audit_worker_snapshot,
)
from worker_execution_contract import (
    PIPELINE_BUSINESS_CONTROL_MANIFEST_SCHEMA,
    PIPELINE_BUSINESS_CONTROL_PROTOCOL,
    PIPELINE_BUSINESS_CONTROL_REQUEST_CLASS,
    PIPELINE_BUSINESS_CONTROL_STATUS_PREPARED,
    WORKER_EXECUTION_MANIFEST_SCHEMA,
    WORKER_EXECUTION_PROTOCOL,
    WORKER_EXECUTION_REQUEST_CLASS,
    WORKER_EXECUTION_STATUS_PREPARED,
    WorkerExecutionStatus,
    WorkerIdempotencyPolicy,
    WorkerLedgerHead,
    WorkerOutboxDispatchCursor,
    WorkerOutboxDispatchState,
    WorkerOutboxOrder,
    WorkerReplayPolicy,
    WorkerTarget,
    WorkerTargetKind,
)
from worker_execution_integrity import (
    MAX_WORKER_EXECUTION_MANIFEST_BYTES,
    MAX_WORKER_EXECUTION_MANIFEST_NODES,
    PIPELINE_BUSINESS_CONTROL_RECEIPT_HASH_DOMAIN,
    WORKER_EXECUTION_AFTER_STATE_HASH_DOMAIN,
    WORKER_EXECUTION_RECEIPT_HASH_DOMAIN,
    _control_manifest_digest,
    _domain_digest as _worker_domain_digest,
    _ledger_tail_digest,
    _manifest_digest as _worker_manifest_digest,
    _receipt_digest,
)


_ZERO = b"\0" * 32
_LOWER_HEX = frozenset("0123456789abcdef")
_MAX_INTEGER = 2**63 - 1
_MAX_DESCENDANT_ALLOCATION_ROWS = 100_000
_MAX_DESCENDANT_ALLOCATION_BATCHES = 10_000
_MAX_WORKER_PROGRESS_ROWS = 100_000
_UNAVAILABLE_SQLITE_PRIMARY_CODES = frozenset(
    getattr(sqlite3, name)
    for name in (
        "SQLITE_BUSY",
        "SQLITE_LOCKED",
        "SQLITE_IOERR",
        "SQLITE_CANTOPEN",
        "SQLITE_FULL",
        "SQLITE_NOMEM",
        "SQLITE_INTERRUPT",
    )
    if hasattr(sqlite3, name)
)
_UNAVAILABLE_KERNEL_CODES = frozenset((
    "sqlite_busy",
    "sqlite_locked",
    "sqlite_io_error",
    "sqlite_open_failed",
    "sqlite_database_missing",
    "sqlite_business_runtime_unreadable",
))


class SQLiteProductionReadError(RuntimeError):
    """Stable, payload-free failure at the production rooted-read boundary."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        if type(code) is not str or not code:
            raise TypeError("code must be a non-empty exact string")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class SQLiteProductionHead:
    """Detached proof carrier for one exact-at-commit production head."""

    business: SQLiteBusinessHead
    commit_id: str
    target_count: int
    pipeline_target_count: int
    action_target_count: int
    outbox_target_count: int
    control_count: int
    allocation_count: int
    allocation_head_sequence: int
    exact_at_commit: bool = True
    targets_sha256: bytes = field(repr=False, default=_ZERO)
    production_manifest_sha256: bytes = field(repr=False, default=_ZERO)
    production_receipt_sha256: bytes = field(repr=False, default=_ZERO)


@dataclass(frozen=True)
class _ControlProof:
    ordinal: int
    intent_id: str
    target_id: str
    transaction_id: str
    sequence: int
    previous_tail_sha256: bytes = field(repr=False)
    ledger_tail_sha256: bytes = field(repr=False)
    request_sha256: bytes = field(repr=False)
    after_state_sha256: bytes = field(repr=False)
    receipt_sha256: bytes = field(repr=False)
    after: Dict[str, Any] = field(repr=False)
    before: Optional[Dict[str, Any]] = field(repr=False)


@dataclass(frozen=True)
class _ProductionPublishFence:
    target_ids: Tuple[str, ...]
    worker_head: Tuple[Any, ...] = field(repr=False)
    dispatch_head: Tuple[Any, ...] = field(repr=False)
    currents: Tuple[Tuple[Any, ...], ...] = field(repr=False)


@dataclass(frozen=True)
class _PinnedProductionHead:
    result: SQLiteProductionHead
    fence: _ProductionPublishFence = field(repr=False)


def _fail(code: str) -> None:
    raise SQLiteProductionReadError(code)


def _storage_unavailable(error: BaseException) -> bool:
    """Separate inability to observe storage from a disproved invariant."""

    current: Optional[BaseException] = error
    observed = set()
    while current is not None and id(current) not in observed:
        observed.add(id(current))
        if isinstance(current, (OSError, MemoryError)):
            return True
        if (
            isinstance(current, SQLiteKernelError)
            and current.code in _UNAVAILABLE_KERNEL_CODES
        ):
            return True
        if isinstance(current, sqlite3.Error):
            code = getattr(current, "sqlite_errorcode", None)
            if (
                type(code) is int
                and (code & 0xFF) in _UNAVAILABLE_SQLITE_PRIMARY_CODES
            ):
                return True
        cause = current.__cause__
        current = cause if isinstance(cause, BaseException) else None
    return False


def _same(left: Any, right: Any) -> bool:
    if type(left) is bytes and type(right) is bytes:
        return hmac.compare_digest(left, right)
    return type(left) is type(right) and left == right


def _row_is(row: Any, expected: Sequence[Any]) -> bool:
    return row is not None and len(row) == len(expected) and all(
        _same(row[index], value) for index, value in enumerate(expected)
    )


def _one(connection: Any, sql: str, parameters: Sequence[Any]) -> Any:
    # Never allow a supposedly unique lookup to materialize an attacker-sized
    # result set before we can reject it.
    rows = connection.execute(sql, parameters).fetchmany(2)
    if len(rows) != 1:
        _fail("sqlite_production_read_corrupt")
    return rows[0]


def _input_text(value: Any) -> str:
    if type(value) is not str or not value or len(value) > 160:
        _fail("sqlite_production_read_input_invalid")
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeEncodeError:
        _fail("sqlite_production_read_input_invalid")
        raise AssertionError("unreachable")
    if len(encoded) > 640:
        _fail("sqlite_production_read_input_invalid")
    return value


def _positive(value: Any) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_INTEGER:
        _fail("sqlite_production_read_input_invalid")
    return value


def _commit_identity(value: Any) -> str:
    if (
        type(value) is not str
        or len(value) != 32
        or any(character not in _LOWER_HEX for character in value)
    ):
        _fail("sqlite_production_read_input_invalid")
    return value


def _digest(value: Any) -> bytes:
    if type(value) is not bytes or len(value) != 32:
        _fail("sqlite_production_read_corrupt")
    return value


def _canonical_json(raw: Any, *, worker: bool = False) -> Dict[str, Any]:
    if type(raw) is not bytes or not raw:
        _fail("sqlite_production_read_corrupt")
    maximum_bytes = (
        MAX_WORKER_EXECUTION_MANIFEST_BYTES
        if worker
        else MAX_PRODUCTION_ATOMIC_MANIFEST_BYTES
    )
    maximum_nodes = (
        MAX_WORKER_EXECUTION_MANIFEST_NODES
        if worker
        else MAX_PRODUCTION_ATOMIC_MANIFEST_NODES
    )
    rejected = False
    value = None
    encoded = b""
    try:
        value = decode_payload_bounded(raw, maximum_bytes, maximum_nodes)
        encoded = encode_payload_bounded(value, maximum_bytes, maximum_nodes)
    except BusinessCodecError:
        rejected = True
    if rejected or type(value) is not dict or encoded != raw:
        _fail("sqlite_production_read_corrupt")
    return value


def _payload_from_target_row(row: Any) -> FrozenJsonPayload:
    try:
        return FrozenJsonPayload(
            schema=row[8],
            codec_id=row[9],
            codec_version=row[10],
            payload_bytes=row[11],
            payload_sha256=row[12],
        )
    except (TypeError, ValueError):
        _fail("sqlite_production_read_corrupt")
    raise AssertionError("unreachable")


def _target_from_row(row: Any) -> ProductionWorkerTarget:
    try:
        kind = WorkerTargetKind(row[1])
        order = (
            None
            if row[23] is None
            else WorkerOutboxOrder(row[23], row[24], row[25])
        )
        target = WorkerTarget(
            target_kind=kind,
            target_id=row[0],
            project_id=row[4],
            generation=row[5],
            document_id=row[6],
            origin_commit_id=row[7],
            payload=_payload_from_target_row(row),
            replay_policy=WorkerReplayPolicy(row[14]),
            idempotency_policy=WorkerIdempotencyPolicy(row[15]),
            idempotency_scope=row[13],
            idempotency_key=row[16],
            max_attempts=row[17],
            outbox_order=order,
            pipeline_id=row[19],
            intent_type=row[20],
        )
        return ProductionWorkerTarget(
            source_ordinal=row[3],
            target=target,
            genesis_status=WorkerExecutionStatus(row[2]),
            genesis_state_sha256=row[27],
            action_type=row[18],
            outbox_topic=row[21],
            outbox_partition_key=row[22],
        )
    except (TypeError, ValueError):
        _fail("sqlite_production_read_corrupt")
    raise AssertionError("unreachable")


def _target_rows(connection: Any, commit_id: str) -> Tuple[Any, ...]:
    cursor = connection.execute(
        "SELECT target_id,target_kind,genesis_status,source_ordinal,"
        "project_id,generation,document_id,origin_commit_id,payload_schema,"
        "payload_codec_id,payload_codec_version,payload_bytes,payload_sha256,"
        "idempotency_scope,replay_policy,idempotency_policy,idempotency_key,"
        "max_attempts,action_type,pipeline_id,intent_type,outbox_topic,"
        "outbox_partition_key,outbox_sequence,outbox_repository_revision,"
        "outbox_ordinal,created_at,genesis_state_sha256 "
        "FROM worker_targets WHERE origin_commit_id=? "
        "ORDER BY CASE target_kind WHEN 'pipeline' THEN 0 WHEN 'action' THEN 1 "
        "ELSE 2 END,source_ordinal",
        (commit_id,),
    )
    rows = []
    payload_bytes = 0
    for row in cursor:
        if len(rows) >= MAX_PRODUCTION_ATOMIC_TARGETS:
            _fail("sqlite_production_read_corrupt")
        raw = row[11]
        if type(raw) is not bytes:
            _fail("sqlite_production_read_corrupt")
        payload_bytes += len(raw)
        if payload_bytes > MAX_ATOMIC_TOTAL_AUXILIARY_BYTES:
            _fail("sqlite_production_read_corrupt")
        rows.append(row)
    return tuple(rows)


def _payload_projection(value: ProductionWorkerTarget) -> Dict[str, Any]:
    payload = value.target.payload
    return {
        "schema": payload.schema,
        "codec_id": payload.codec_id,
        "codec_version": payload.codec_version,
        "bytes": len(payload.payload_bytes),
        "sha256": payload.payload_sha256.hex(),
    }


def _verify_target_mapping(
    targets: Tuple[ProductionWorkerTarget, ...],
    pipelines: Tuple[Dict[str, Any], ...],
    actions: Tuple[Dict[str, Any], ...],
    outbox: Tuple[Dict[str, Any], ...],
    commit: Any,
) -> Tuple[Any, ...]:
    try:
        groups = _groups(targets)
    except (TypeError, ValueError):
        _fail("sqlite_production_read_corrupt")
    pipeline_targets, action_targets, outbox_targets = tuple(
        group.members for group in groups
    )
    creates = tuple(
        value for value in pipelines if value["intent_kind"] == "create"
    )
    if (
        len(pipeline_targets) != len(creates)
        or len(action_targets) != len(actions)
        or len(outbox_targets) != len(outbox)
    ):
        _fail("sqlite_production_read_corrupt")
    for wrapped, intent in zip(pipeline_targets, creates):
        target = wrapped.target
        expected_idempotency = (
            "single_attempt"
            if intent["replay_policy"] == "never_replay"
            else "durable_receipt_reconcile"
        )
        if (
            wrapped.source_ordinal != intent["ordinal"]
            or target.target_id != intent["job_id"]
            or target.project_id != commit["project_id"]
            or target.generation != commit["generation"]
            or target.document_id != commit["document_id"]
            or target.origin_commit_id != commit["commit_id"]
            or target.pipeline_id != intent["pipeline_id"]
            or target.intent_type != intent["intent_type"]
            or target.idempotency_scope != "pipeline/%s/%d/%s" % (
                commit["project_id"],
                commit["generation"],
                intent["pipeline_id"],
            )
            or target.idempotency_key != intent["idempotency_key"]
            or target.replay_policy.value != intent["replay_policy"]
            or target.idempotency_policy.value != expected_idempotency
            or target.max_attempts != intent["max_attempts"]
            or wrapped.genesis_status is not WorkerExecutionStatus.QUEUED
            or _payload_projection(wrapped) != intent["payload"]
        ):
            _fail("sqlite_production_read_corrupt")
    for wrapped, action in zip(action_targets, actions):
        target = wrapped.target
        try:
            expected_genesis = _production_worker_genesis_state_sha256(
                target,
                status=WorkerExecutionStatus.QUEUED,
            )
        except (TypeError, ValueError):
            _fail("sqlite_production_read_corrupt")
        if (
            wrapped.source_ordinal != action["ordinal"]
            or target.target_id != action["action_id"]
            or target.project_id != commit["project_id"]
            or target.generation != commit["generation"]
            or target.document_id != commit["document_id"]
            or target.origin_commit_id != commit["commit_id"]
            or wrapped.action_type != action["action_type"]
            or target.idempotency_key != action["idempotency_key"]
            or target.idempotency_scope != action["idempotency_scope"]
            or target.replay_policy.value != action["replay_policy"]
            or target.idempotency_policy.value != action["idempotency_policy"]
            or target.max_attempts != action["max_attempts"]
            or wrapped.genesis_status is not WorkerExecutionStatus.QUEUED
            or not _same(wrapped.genesis_state_sha256, expected_genesis)
            or _payload_projection(wrapped) != action["payload"]
        ):
            _fail("sqlite_production_read_corrupt")
    for wrapped, event in zip(outbox_targets, outbox):
        target = wrapped.target
        order = target.outbox_order
        try:
            expected_genesis = _production_worker_genesis_state_sha256(
                target,
                status=WorkerExecutionStatus.PENDING,
            )
        except (TypeError, ValueError):
            _fail("sqlite_production_read_corrupt")
        if (
            wrapped.source_ordinal != event["ordinal"]
            or target.target_id != event["event_id"]
            or target.project_id != commit["project_id"]
            or target.generation != commit["generation"]
            or target.document_id != commit["document_id"]
            or target.origin_commit_id != commit["commit_id"]
            or wrapped.outbox_topic != event["topic"]
            or wrapped.outbox_partition_key != event["partition_key"]
            or target.replay_policy is not PRODUCTION_OUTBOX_REPLAY_POLICY
            or target.idempotency_policy is not PRODUCTION_OUTBOX_IDEMPOTENCY_POLICY
            or target.idempotency_scope != "outbox/%s/%d" % (
                commit["project_id"],
                commit["generation"],
            )
            or target.idempotency_key != event["event_id"]
            or target.max_attempts != PRODUCTION_OUTBOX_MAX_ATTEMPTS
            or order is None
            or order.ordinal != event["ordinal"]
            or wrapped.genesis_status is not WorkerExecutionStatus.PENDING
            or not _same(wrapped.genesis_state_sha256, expected_genesis)
            or _payload_projection(wrapped) != event["payload"]
        ):
            _fail("sqlite_production_read_corrupt")
    return groups


def _verify_pipeline_genesis(
    pipeline_targets: Tuple[ProductionWorkerTarget, ...],
    pipelines: Tuple[Dict[str, Any], ...],
    controls: Tuple[_ControlProof, ...],
) -> None:
    creates = tuple(
        (intent, proof)
        for intent, proof in zip(pipelines, controls)
        if intent["intent_kind"] == "create"
    )
    if len(pipeline_targets) != len(creates):
        _fail("sqlite_production_read_corrupt")
    for wrapped, (intent, proof) in zip(pipeline_targets, creates):
        if (
            wrapped.target.target_id != intent["job_id"]
            or proof.intent_id != intent["intent_id"]
            or proof.target_id != wrapped.target.target_id
            or wrapped.genesis_status is not WorkerExecutionStatus.QUEUED
            or not _same(
                wrapped.genesis_state_sha256,
                proof.after_state_sha256,
            )
        ):
            _fail("sqlite_production_read_corrupt")


def _pipeline_digest(pipelines: Tuple[Dict[str, Any], ...]) -> bytes:
    parts = []
    for intent in pipelines:
        parts.append(_business_component_part({
            "ordinal": intent["ordinal"],
            "intent_id": intent["intent_id"],
            "job_id": intent["job_id"],
            "pipeline_id": intent["pipeline_id"],
            "intent_type": intent["intent_type"],
            "intent_kind": intent["intent_kind"],
            "expected_status": intent["expected_status"],
            "target_status": intent["target_status"],
            "expected_state_revision": intent["expected_state_revision"],
            "idempotency_key": intent["idempotency_key"],
            "replay_policy": intent["replay_policy"],
            "max_attempts": intent["max_attempts"],
            "status": intent["status"],
            "payload_sha256": intent["payload"]["sha256"],
        }))
    return _business_component_digest(
        b"ze.atomic-business-commit.pipelines.v1",
        tuple(parts),
    )


def _control_after_digest(after: Dict[str, Any]) -> bytes:
    try:
        encoded = encode_payload_bounded(
            {"worker_after": after, "dispatch_after": None},
            MAX_WORKER_EXECUTION_MANIFEST_BYTES,
            MAX_WORKER_EXECUTION_MANIFEST_NODES,
        )
    except BusinessCodecError:
        _fail("sqlite_production_read_corrupt")
    return _worker_domain_digest(WORKER_EXECUTION_AFTER_STATE_HASH_DOMAIN, encoded)


def _read_controls(
    connection: Any,
    commit: Any,
    commit_manifest: Dict[str, Any],
    pipelines: Tuple[Dict[str, Any], ...],
    committed_at: str,
) -> Tuple[_ControlProof, ...]:
    member_count = connection.execute(
        "SELECT count(*) FROM atomic_business_control_members WHERE commit_id=?",
        (commit["commit_id"],),
    ).fetchone()[0]
    if member_count != len(pipelines):
        _fail("sqlite_production_read_corrupt")
    proofs = []
    for pipeline in pipelines:
        ordinal = pipeline["ordinal"]
        intent = _one(
            connection,
            "SELECT intent_id,origin_commit_id,ordinal,target_id,target_kind,"
            "operation,intent_type,idempotency_key,payload_schema,"
            "payload_codec_id,payload_codec_version,payload_bytes,payload_sha256,"
            "replay_policy,idempotency_policy,max_attempts,before_status,"
            "after_status,before_state_revision,after_state_revision,"
            "request_sha256,committed_at FROM pipeline_control_intents "
            "WHERE origin_commit_id=? AND ordinal=?",
            (commit["commit_id"], ordinal),
        )
        receipt = _one(
            connection,
            "SELECT intent_id,transaction_id,entry_kind,receipt_kind,committed_at,"
            "operation,intent_type,target_id,before_status,after_status,"
            "before_state_revision,after_state_revision,sequence,"
            "previous_ledger_tail_sha256,ledger_tail_sha256,manifest_bytes,"
            "request_sha256,after_state_sha256,receipt_sha256 "
            "FROM pipeline_control_receipts WHERE intent_id=?",
            (intent[0],),
        )
        member = _one(
            connection,
            "SELECT commit_id,ordinal,intent_id,control_transaction_id,sequence,"
            "ledger_tail_sha256,binding_sha256 "
            "FROM atomic_business_control_members WHERE commit_id=? AND ordinal=?",
            (commit["commit_id"], ordinal),
        )
        ledger = _one(
            connection,
            "SELECT sequence,entry_kind,transaction_id,receipt_kind,committed_at,"
            "previous_tail_sha256,ledger_tail_sha256,request_sha256,"
            "after_state_sha256,receipt_sha256,occurred_at "
            "FROM worker_ledger_entries WHERE sequence=?",
            (receipt[12],),
        )
        parent = _one(
            connection,
            "SELECT transaction_id,kind,committed_at FROM transaction_receipts "
            "WHERE transaction_id=?",
            (receipt[1],),
        )
        manifest = _canonical_json(receipt[15], worker=True)
        expected_operation = (
            "create"
            if pipeline["intent_kind"] == "create"
            else "apply_control_intent"
        )
        expected_idempotency = (
            "single_attempt"
            if pipeline["replay_policy"] == "never_replay"
            else "durable_receipt_reconcile"
        )
        target_row = _one(
            connection,
            "SELECT target_id,target_kind,genesis_status,source_ordinal,"
            "project_id,generation,document_id,origin_commit_id,payload_schema,"
            "payload_codec_id,payload_codec_version,payload_bytes,payload_sha256,"
            "idempotency_scope,replay_policy,idempotency_policy,idempotency_key,"
            "max_attempts,action_type,pipeline_id,intent_type,outbox_topic,"
            "outbox_partition_key,outbox_sequence,outbox_repository_revision,"
            "outbox_ordinal,created_at,genesis_state_sha256 "
            "FROM worker_targets WHERE target_id=?",
            (intent[3],),
        )
        target = _target_from_row(target_row).target
        payload = _payload_projection(ProductionWorkerTarget(
            source_ordinal=target_row[3],
            target=target,
            genesis_status=WorkerExecutionStatus(target_row[2]),
            genesis_state_sha256=target_row[27],
        ))
        try:
            intent_payload = FrozenJsonPayload(
                schema=intent[8],
                codec_id=intent[9],
                codec_version=intent[10],
                payload_bytes=intent[11],
                payload_sha256=intent[12],
            )
        except (TypeError, ValueError):
            _fail("sqlite_production_read_corrupt")
        intent_payload_projection = {
            "schema": intent_payload.schema,
            "codec_id": intent_payload.codec_id,
            "codec_version": intent_payload.codec_version,
            "bytes": len(intent_payload.payload_bytes),
            "sha256": intent_payload.payload_sha256.hex(),
        }
        control_intent = {
            "origin_commit_id": commit["commit_id"],
            "intent_type": pipeline["intent_type"],
            "idempotency_key": pipeline["idempotency_key"],
            "payload": pipeline["payload"],
            "replay_policy": pipeline["replay_policy"],
            "idempotency_policy": expected_idempotency,
            "max_attempts": pipeline["max_attempts"],
        }
        expected_keys = frozenset((
            "schema", "protocol", "status", "operation",
            "request_transaction_id", "occurred_at", "authority", "target",
            "control_intent", "transition", "before", "after",
        ))
        if (
            frozenset(manifest) != expected_keys
            or manifest["schema"] != PIPELINE_BUSINESS_CONTROL_MANIFEST_SCHEMA
            or manifest["protocol"] != PIPELINE_BUSINESS_CONTROL_PROTOCOL
            or manifest["status"] != PIPELINE_BUSINESS_CONTROL_STATUS_PREPARED
            or manifest["operation"] != expected_operation
            or manifest["request_transaction_id"] != pipeline["intent_id"]
            or manifest["occurred_at"] != committed_at
            or manifest["authority"] != commit_manifest["authority"]
            or manifest["target"] != _base_target_manifest(target)
            or manifest["control_intent"] != control_intent
            or (
                pipeline["intent_kind"] == "create"
                and payload != pipeline["payload"]
            )
            or manifest["transition"] != {
                "before_state_revision": pipeline["expected_state_revision"],
                "after_state_revision": intent[19],
                "before_status": pipeline["expected_status"],
                "after_status": pipeline["target_status"],
            }
            or (pipeline["intent_kind"] == "create") != (manifest["before"] is None)
            or type(manifest["after"]) is not dict
            or intent[0] != pipeline["intent_id"]
            or intent[1] != commit["commit_id"]
            or intent[2] != ordinal
            or intent[3] != pipeline["job_id"]
            or intent[4] != "pipeline"
            or intent[5] != expected_operation
            or intent[6] != pipeline["intent_type"]
            or intent[7] != pipeline["idempotency_key"]
            or intent_payload_projection != pipeline["payload"]
            or intent_payload_projection != manifest["control_intent"]["payload"]
            or intent[13] != pipeline["replay_policy"]
            or intent[14] != expected_idempotency
            or intent[15] != pipeline["max_attempts"]
            or intent[16] != pipeline["expected_status"]
            or intent[17] != pipeline["target_status"]
            or intent[18] != pipeline["expected_state_revision"]
            or intent[21] != committed_at
        ):
            _fail("sqlite_production_read_corrupt")
        request_sha = _control_manifest_digest(receipt[15])
        after_sha = _control_after_digest(manifest["after"])
        try:
            before_status = (
                None if receipt[8] is None else WorkerExecutionStatus(receipt[8])
            )
            after_status = WorkerExecutionStatus(receipt[9])
            receipt_sha = _receipt_digest(
                domain=PIPELINE_BUSINESS_CONTROL_RECEIPT_HASH_DOMAIN,
                request_class=PIPELINE_BUSINESS_CONTROL_REQUEST_CLASS,
                operation=receipt[5],
                target_kind=WorkerTargetKind.PIPELINE,
                target_id=receipt[7],
                before_status=before_status,
                after_status=after_status,
                before_state_revision=receipt[10],
                after_state_revision=receipt[11],
                sequence=receipt[12],
                previous_tail=receipt[13],
                request_transaction_id=receipt[0],
                request_sha256=request_sha,
                after_state_sha256=after_sha,
                occurred_at=receipt[4],
            )
            tail = _ledger_tail_digest(receipt[12], receipt[13], receipt_sha)
        except (TypeError, ValueError, OverflowError):
            _fail("sqlite_production_read_corrupt")
        binding = _production_atomic_control_member_sha256(
            commit_id=commit["commit_id"],
            ordinal=ordinal,
            intent_id=intent[0],
            control_transaction_id=receipt[1],
            sequence=receipt[12],
            ledger_tail_sha256=tail,
        )
        if (
            not _row_is(parent, (receipt[1], receipt[3], receipt[4]))
            or not _row_is(ledger, (
                receipt[12], "pipeline_control", receipt[1], receipt[3],
                receipt[4], receipt[13], tail, request_sha, after_sha,
                receipt_sha, receipt[4],
            ))
            or not _row_is(member, (
                commit["commit_id"], ordinal, intent[0], receipt[1],
                receipt[12], tail, binding,
            ))
            or receipt[0] != intent[0]
            or receipt[2] != "pipeline_control"
            or receipt[4] != committed_at
            or receipt[5] != intent[5]
            or receipt[6] != intent[6]
            or receipt[7] != intent[3]
            or receipt[8] != intent[16]
            or receipt[9] != intent[17]
            or receipt[10] != intent[18]
            or receipt[11] != intent[19]
            or not _same(receipt[16], request_sha)
            or not _same(receipt[17], after_sha)
            or not _same(receipt[18], receipt_sha)
            or not _same(intent[20], request_sha)
        ):
            _fail("sqlite_production_read_corrupt")
        proofs.append(_ControlProof(
            ordinal=ordinal,
            intent_id=intent[0],
            target_id=intent[3],
            transaction_id=receipt[1],
            sequence=receipt[12],
            previous_tail_sha256=receipt[13],
            ledger_tail_sha256=tail,
            request_sha256=request_sha,
            after_state_sha256=after_sha,
            receipt_sha256=receipt_sha,
            after=manifest["after"],
            before=manifest["before"],
        ))
    return tuple(proofs)


def _read_atomic_receipt(
    connection: Any,
    commit: Any,
    pipelines: Tuple[Dict[str, Any], ...],
    controls: Tuple[_ControlProof, ...],
) -> Tuple[Any, Dict[str, Any]]:
    row = _one(
        connection,
        "SELECT transaction_id,receipt_kind,committed_at,atomic_manifest_sha256,"
        "pipeline_intents_sha256,controls_sha256,control_count,"
        "before_worker_sequence,before_worker_tail_sha256,after_worker_sequence,"
        "after_worker_tail_sha256,manifest_bytes,manifest_sha256,receipt_sha256,"
        "production_manifest_bytes,production_manifest_sha256,target_count,"
        "targets_sha256,production_receipt_sha256 "
        "FROM atomic_business_write_receipts WHERE commit_id=?",
        (commit["commit_id"],),
    )
    manifest = _canonical_json(row[11])
    pipeline_sha = _pipeline_digest(pipelines)
    controls_sha = _atomic_digest(
        ATOMIC_BUSINESS_WRITE_CONTROLS_HASH_DOMAIN,
        tuple(
            value.request_sha256
            + value.after_state_sha256
            + value.receipt_sha256
            + value.sequence.to_bytes(8, "big")
            for value in controls
        ),
    )
    before_tail = _digest(row[8])
    after_tail = _digest(row[10])
    expected_controls = [
        {
            "ordinal": value.ordinal,
            "intent_id": value.intent_id,
            "job_id": pipelines[index]["job_id"],
            "control_transaction_id": value.intent_id,
            "request_sha256": value.request_sha256.hex(),
            "after_state_sha256": value.after_state_sha256.hex(),
            "receipt_sha256": value.receipt_sha256.hex(),
            "receipt_sequence": value.sequence,
        }
        for index, value in enumerate(controls)
    ]
    expected_manifest = {
        "schema": ATOMIC_BUSINESS_WRITE_MANIFEST_SCHEMA,
        "protocol": ATOMIC_BUSINESS_WRITE_PROTOCOL,
        "status": ATOMIC_BUSINESS_WRITE_PLAN_STATUS,
        "commit": {
            "commit_id": commit["commit_id"],
            "committed_at": row[2],
            "atomic_manifest_sha256": commit["manifest_sha256"].hex(),
            "pipeline_intents_sha256": pipeline_sha.hex(),
        },
        "worker_ledger": {
            "before": {"sequence": row[7], "tail_sha256": before_tail.hex()},
            "after": {"sequence": row[9], "tail_sha256": after_tail.hex()},
        },
        "controls": {
            "count": len(controls),
            "sha256": controls_sha.hex(),
            "members": expected_controls,
        },
    }
    inner_manifest_sha = _atomic_manifest_digest(row[11])
    try:
        parent = AtomicBusinessWriteGenericReceiptEvidence(row[0], row[1], row[2])
        receipt_sha = _atomic_receipt_digest(
            commit_id=commit["commit_id"],
            receipt_parent=parent,
            atomic_manifest_sha256=commit["manifest_sha256"],
            pipeline_intents_sha256=pipeline_sha,
            controls_sha256=controls_sha,
            control_count=len(controls),
            before_worker_ledger=WorkerLedgerHead(row[7], before_tail),
            after_worker_ledger=WorkerLedgerHead(row[9], after_tail),
            manifest_sha256=inner_manifest_sha,
        )
    except (TypeError, ValueError, OverflowError):
        _fail("sqlite_production_read_corrupt")
    if (
        manifest != expected_manifest
        or row[0] != commit["transaction_id"]
        or row[1] != commit["receipt_kind"]
        or not _same(row[3], commit["manifest_sha256"])
        or not _same(row[4], pipeline_sha)
        or not _same(row[5], controls_sha)
        or row[6] != len(controls)
        or row[9] != row[7] + len(controls)
        or (controls and not _same(controls[0].previous_tail_sha256, before_tail))
        or any(
            not _same(left.ledger_tail_sha256, right.previous_tail_sha256)
            for left, right in zip(controls, controls[1:])
        )
        or (controls and not _same(controls[-1].ledger_tail_sha256, after_tail))
        or (not controls and not _same(before_tail, after_tail))
        or not _same(row[12], inner_manifest_sha)
        or not _same(row[13], receipt_sha)
    ):
        _fail("sqlite_production_read_corrupt")
    return row, manifest


def _mounted_from_outer(
    connection: Any,
    profile: BusinessRuntimeProfile,
    commit: Any,
    commit_manifest: Dict[str, Any],
    value: Dict[str, Any],
) -> ProductionMountedAuthorityEvidence:
    current = _binding_from_snapshot(
        connection,
        profile,
        _mounted_row(connection),
    ).evidence
    try:
        rebuilt = ProductionMountedAuthorityEvidence(
            authority=value["authority"],
            authority_state=value["authority_state"],
            repository_epoch=value["repository_epoch"],
            cutover_id=value["cutover_id"],
            source_admission_revision=value["source_admission_revision"],
            repository_revision=value["repository_revision"],
            control_revision=value["control_revision"],
            runtime_generation=value["runtime_generation"],
            code_digest=bytes.fromhex(value["code_digest"]),
            target_schema_version=value["target_schema_version"],
            target_schema_fingerprint=value["target_schema_fingerprint"],
            mount_transaction_id=value["mount_transaction_id"],
            mount_manifest_sha256=bytes.fromhex(value["mount_manifest_sha256"]),
            mounted_at=value["mounted_at"],
        )
    except (KeyError, TypeError, ValueError):
        _fail("sqlite_production_read_corrupt")
    authority = commit_manifest["authority"]
    if (
        rebuilt.authority != current.authority
        or rebuilt.authority_state != "active"
        or rebuilt.repository_epoch != commit["repository_epoch"]
        or rebuilt.cutover_id != commit["cutover_id"]
        or rebuilt.source_admission_revision != commit["source_admission_revision"]
        or rebuilt.repository_revision != commit["repository_revision"] - 1
        or rebuilt.control_revision != authority["control_revision"]
        or rebuilt.runtime_generation != current.runtime_generation
        or not _same(rebuilt.code_digest, current.code_digest)
        or rebuilt.target_schema_version != current.target_schema_version
        or rebuilt.target_schema_fingerprint != current.target_schema_fingerprint
        or rebuilt.mount_transaction_id != current.mount_transaction_id
        or not _same(rebuilt.mount_manifest_sha256, current.mount_manifest_sha256)
        or rebuilt.mounted_at != current.mounted_at
    ):
        _fail("sqlite_production_read_corrupt")
    return rebuilt


def _worker_before(value: Dict[str, Any]) -> ProductionWorkerLedgerHeadEvidence:
    try:
        return ProductionWorkerLedgerHeadEvidence(
            state_revision=value["state_revision"],
            ledger_head=WorkerLedgerHead(value["sequence"], bytes.fromhex(value["tail_sha256"])),
            previous_tail_sha256=bytes.fromhex(value["previous_tail_sha256"]),
            updated_at=value["updated_at"],
        )
    except (KeyError, TypeError, ValueError):
        _fail("sqlite_production_read_corrupt")
    raise AssertionError("unreachable")


def _dispatch_before(value: Dict[str, Any]) -> ProductionOutboxDispatchHeadEvidence:
    try:
        published = value["published"]
        inflight = value["inflight"]
        state = WorkerOutboxDispatchState(
            value["state_revision"],
            WorkerOutboxDispatchCursor(
                published["sequence"],
                published["repository_revision"],
                published["ordinal"],
            ),
            None if inflight is None else WorkerOutboxOrder(
                inflight["sequence"],
                inflight["repository_revision"],
                inflight["ordinal"],
            ),
        )
        return ProductionOutboxDispatchHeadEvidence(
            dispatch_state=state,
            previous_head_sha256=bytes.fromhex(value["previous_head_sha256"]),
            head_sha256=bytes.fromhex(value["head_sha256"]),
            updated_at=value["updated_at"],
        )
    except (KeyError, TypeError, ValueError):
        _fail("sqlite_production_read_corrupt")
    raise AssertionError("unreachable")


def _allocation_head(value: Dict[str, Any]) -> ProductionOutboxAllocationHead:
    try:
        return ProductionOutboxAllocationHead(
            state_revision=value["state_revision"],
            sequence=value["sequence"],
            previous_tail_sha256=bytes.fromhex(value["previous_tail_sha256"]),
            tail_sha256=bytes.fromhex(value["tail_sha256"]),
            updated_at=value["updated_at"],
        )
    except (KeyError, TypeError, ValueError):
        _fail("sqlite_production_read_corrupt")
    raise AssertionError("unreachable")


def _verify_outer_manifest(
    connection: Any,
    profile: BusinessRuntimeProfile,
    commit: Any,
    commit_manifest: Dict[str, Any],
    receipt: Any,
    groups: Tuple[Any, ...],
) -> Tuple[
    ProductionWorkerLedgerHeadEvidence,
    ProductionOutboxDispatchHeadEvidence,
    ProductionOutboxAllocationHead,
    ProductionOutboxAllocationHead,
]:
    raw = receipt[14]
    manifest = _canonical_json(raw)
    manifest_sha = _production_domain_digest(
        PRODUCTION_ATOMIC_MANIFEST_HASH_DOMAIN,
        (raw,),
    )
    # production integrity's domain helper accepts a tuple payload for this
    # protocol; retain the frozen formula rather than a generic SHA shortcut.
    if not _same(manifest_sha, receipt[15]):
        _fail("sqlite_production_read_corrupt")
    expected_keys = frozenset((
        "schema", "protocol", "status", "hash_domains", "pure_atomic",
        "mounted_authority", "scheduler_heads", "outbox_allocation", "targets",
    ))
    if frozenset(manifest) != expected_keys:
        _fail("sqlite_production_read_corrupt")
    mounted = _mounted_from_outer(
        connection, profile, commit, commit_manifest, manifest["mounted_authority"]
    )
    before_worker = _worker_before(manifest["scheduler_heads"]["worker_ledger"])
    before_dispatch = _dispatch_before(
        manifest["scheduler_heads"]["outbox_dispatch"]
    )
    before_allocation = _allocation_head(manifest["outbox_allocation"]["before"])
    after_allocation = _allocation_head(manifest["outbox_allocation"]["after"])
    expected_domains = {
        "manifest": PRODUCTION_ATOMIC_MANIFEST_HASH_DOMAIN.decode("ascii"),
        "receipt": PRODUCTION_ATOMIC_RECEIPT_HASH_DOMAIN.decode("ascii"),
        "targets": PRODUCTION_ATOMIC_TARGETS_HASH_DOMAIN.decode("ascii"),
        "target_group": PRODUCTION_ATOMIC_TARGET_GROUP_HASH_DOMAIN.decode("ascii"),
        "target_member": PRODUCTION_ATOMIC_TARGET_MEMBER_HASH_DOMAIN.decode("ascii"),
        "genesis": PRODUCTION_WORKER_GENESIS_STATE_HASH_DOMAIN.decode("ascii"),
        "outbox_allocation_tail": PRODUCTION_OUTBOX_ALLOCATION_TAIL_HASH_DOMAIN.decode("ascii"),
        "control_member": PRODUCTION_ATOMIC_CONTROL_MEMBER_HASH_DOMAIN.decode("ascii"),
    }
    expected = {
        "schema": PRODUCTION_ATOMIC_WRITE_MANIFEST_SCHEMA,
        "protocol": PRODUCTION_ATOMIC_WRITE_PROTOCOL,
        "status": PRODUCTION_ATOMIC_WRITE_STATUS,
        "hash_domains": expected_domains,
        "pure_atomic": {
            "commit_id": commit["commit_id"],
            "transaction_id": receipt[0],
            "receipt_kind": receipt[1],
            "committed_at": receipt[2],
            "manifest_sha256": receipt[12].hex(),
            "receipt_sha256": receipt[13].hex(),
        },
        "mounted_authority": _mount_manifest(mounted),
        "scheduler_heads": {
            "worker_ledger": _worker_ledger_head_manifest(before_worker),
            "outbox_dispatch": _outbox_dispatch_head_manifest(before_dispatch),
        },
        "outbox_allocation": {
            "count": len(groups[2].members),
            "before": _allocation_head_manifest(before_allocation),
            "after": _allocation_head_manifest(after_allocation),
        },
        "targets": {
            "group_order": [value.value for value in PRODUCTION_ATOMIC_TARGET_GROUP_ORDER],
            "count": receipt[16],
            "sha256": receipt[17].hex(),
        },
    }
    receipt_sha = _production_domain_digest(
        PRODUCTION_ATOMIC_RECEIPT_HASH_DOMAIN,
        (
            commit["commit_id"].encode("ascii"),
            receipt[0].encode("utf-8"),
            receipt[1].encode("utf-8"),
            receipt[2].encode("ascii"),
            receipt[13],
            receipt[15],
            receipt[16].to_bytes(8, "big"),
            receipt[17],
        ),
    )
    if manifest != expected or not _same(receipt_sha, receipt[18]):
        _fail("sqlite_production_read_corrupt")
    return before_worker, before_dispatch, before_allocation, after_allocation


def _worker_head_row(connection: Any) -> Tuple[Any, ...]:
    row = _one(
        connection,
        "SELECT state_revision,sequence,previous_tail_sha256,tail_sha256,updated_at "
        "FROM worker_ledger_head WHERE singleton=1",
        (),
    )
    return tuple(row)


def _dispatch_head_row(connection: Any) -> Tuple[Any, ...]:
    row = _one(
        connection,
        "SELECT state_revision,published_sequence,published_repository_revision,"
        "published_ordinal,inflight_sequence,inflight_repository_revision,"
        "inflight_ordinal,previous_head_sha256,head_sha256,updated_at "
        "FROM outbox_dispatch_head WHERE singleton=1",
        (),
    )
    return tuple(row)


def _current_identity_row(connection: Any, target_id: str) -> Tuple[Any, ...]:
    rows = connection.execute(
        "SELECT target_id,target_kind,status,state_revision,attempt_count,"
        "last_attempt_id,last_attempt_number,current_attempt_id,"
        "current_attempt_number,fence_high_water,cancellation_pending,"
        "next_attempt_at,lease_owner,lease_fence,lease_expires_at,"
        "current_effect_id,unresolved_effect_id,last_receipt_transaction_id,"
        "previous_state_sha256,state_sha256,updated_at "
        "FROM worker_target_current WHERE target_id=?",
        (target_id,),
    ).fetchmany(2)
    if len(rows) != 1:
        _fail("sqlite_production_read_corrupt")
    return tuple(rows[0])


def _publish_fence(
    connection: Any,
    target_ids: Sequence[str],
) -> _ProductionPublishFence:
    exact_ids = tuple(sorted(set(target_ids)))
    return _ProductionPublishFence(
        target_ids=exact_ids,
        worker_head=_worker_head_row(connection),
        dispatch_head=_dispatch_head_row(connection),
        currents=tuple(
            _current_identity_row(connection, target_id)
            for target_id in exact_ids
        ),
    )


def _worker_after_digest(manifest: Dict[str, Any]) -> bytes:
    request = manifest.get("request")
    dispatch = request.get("dispatch_fence") if type(request) is dict else None
    try:
        encoded = encode_payload_bounded(
            {
                "worker_after": manifest["after"],
                "dispatch_after": (
                    None if dispatch is None else dispatch["after"]
                ),
            },
            MAX_WORKER_EXECUTION_MANIFEST_BYTES,
            MAX_WORKER_EXECUTION_MANIFEST_NODES,
        )
    except (BusinessCodecError, KeyError, TypeError):
        _fail("sqlite_production_read_corrupt")
    return _worker_domain_digest(WORKER_EXECUTION_AFTER_STATE_HASH_DOMAIN, encoded)


def _worker_progress_receipts_closed(
    connection: Any,
    expected_worker: Tuple[Any, ...],
    before_dispatch: ProductionOutboxDispatchHeadEvidence,
) -> bool:
    actual_head = _worker_head_row(connection)
    progress_count = actual_head[1] - expected_worker[1]
    if progress_count <= 0:
        return False
    if progress_count > _MAX_WORKER_PROGRESS_ROWS:
        _fail("sqlite_production_read_unavailable")
    if connection.execute(
        "SELECT 1 FROM worker_ledger_entries WHERE sequence>? LIMIT 1",
        (actual_head[1],),
    ).fetchone() is not None:
        return False
    entries = connection.execute(
        "SELECT sequence,entry_kind,transaction_id,receipt_kind,committed_at,"
        "previous_tail_sha256,ledger_tail_sha256,request_sha256,"
        "after_state_sha256,receipt_sha256,occurred_at "
        "FROM worker_ledger_entries WHERE sequence>? AND sequence<=? "
        "ORDER BY sequence",
        (expected_worker[1], actual_head[1]),
    ).fetchall()
    if not entries or len(entries) != progress_count:
        return False
    previous = expected_worker[3]
    latest_by_target: Dict[str, Tuple[Any, Dict[str, Any], bytes]] = {}
    dispatch_state = before_dispatch.dispatch_state
    dispatch_previous = before_dispatch.previous_head_sha256
    dispatch_hash = before_dispatch.head_sha256
    dispatch_updated_at = before_dispatch.updated_at
    for offset, entry in enumerate(entries, start=1):
        if (
            entry[0] != expected_worker[1] + offset
            or entry[1] != "worker_execution"
            or not _same(entry[5], previous)
        ):
            return False
        receipt = connection.execute(
            "SELECT request_id,transaction_id,entry_kind,receipt_kind,committed_at,"
            "operation,target_kind,target_id,before_status,after_status,"
            "before_state_revision,after_state_revision,sequence,"
            "previous_ledger_tail_sha256,ledger_tail_sha256,manifest_bytes,"
            "request_sha256,after_state_sha256,receipt_sha256 "
            "FROM worker_execution_receipts WHERE transaction_id=?",
            (entry[2],),
        ).fetchone()
        if receipt is None or len(receipt) != 19:
            return False
        parent = connection.execute(
            "SELECT transaction_id,kind,committed_at FROM transaction_receipts "
            "WHERE transaction_id=?",
            (receipt[1],),
        ).fetchone()
        manifest = _canonical_json(receipt[15], worker=True)
        header = manifest.get("header")
        transition = manifest.get("transition")
        after = manifest.get("after")
        if (
            type(header) is not dict
            or type(transition) is not dict
            or type(after) is not dict
            or type(header.get("target")) is not dict
            or manifest.get("schema") != WORKER_EXECUTION_MANIFEST_SCHEMA
            or manifest.get("protocol") != WORKER_EXECUTION_PROTOCOL
            or manifest.get("status") != WORKER_EXECUTION_STATUS_PREPARED
            or manifest.get("operation") != receipt[5]
            or header.get("request_transaction_id") != receipt[0]
            or header.get("occurred_at") != receipt[4]
            or header["target"].get("kind") != receipt[6]
            or header["target"].get("id") != receipt[7]
            or transition != {
                "before_state_revision": receipt[10],
                "after_state_revision": receipt[11],
                "before_status": receipt[8],
                "after_status": receipt[9],
            }
            or after.get("state_revision") != receipt[11]
            or after.get("status") != receipt[9]
        ):
            return False
        request_sha = _worker_manifest_digest(receipt[15])
        after_sha = _worker_after_digest(manifest)
        try:
            receipt_sha = _receipt_digest(
                domain=WORKER_EXECUTION_RECEIPT_HASH_DOMAIN,
                request_class=WORKER_EXECUTION_REQUEST_CLASS,
                operation=receipt[5],
                target_kind=WorkerTargetKind(receipt[6]),
                target_id=receipt[7],
                before_status=WorkerExecutionStatus(receipt[8]),
                after_status=WorkerExecutionStatus(receipt[9]),
                before_state_revision=receipt[10],
                after_state_revision=receipt[11],
                sequence=receipt[12],
                previous_tail=receipt[13],
                request_transaction_id=receipt[0],
                request_sha256=request_sha,
                after_state_sha256=after_sha,
                occurred_at=receipt[4],
            )
            tail = _ledger_tail_digest(receipt[12], receipt[13], receipt_sha)
        except (TypeError, ValueError, OverflowError):
            return False
        if (
            not _row_is(parent, (receipt[1], receipt[3], receipt[4]))
            or not _row_is(entry, (
                receipt[12], "worker_execution", receipt[1], receipt[3],
                receipt[4], receipt[13], tail, request_sha, after_sha,
                receipt_sha, receipt[4],
            ))
            or receipt[1] != "_worker-execution:" + receipt[0]
            or receipt[2] != "worker_execution"
            or not _same(receipt[16], request_sha)
            or not _same(receipt[17], after_sha)
            or not _same(receipt[18], receipt_sha)
        ):
            return False
        if receipt[5] == "claim":
            request = manifest.get("request")
            attempt = request if type(request) is dict else None
            if type(attempt) is not dict:
                return False
            attempt_row = connection.execute(
                "SELECT attempt_id,target_id,attempt_number,previous_attempt_id,"
                "execution_fence,owner_id,started_at,receipt_transaction_id "
                "FROM worker_attempts WHERE receipt_transaction_id=?",
                (receipt[1],),
            ).fetchone()
            if not _row_is(attempt_row, (
                attempt.get("attempt_id"), receipt[7],
                attempt.get("attempt_number"), attempt.get("previous_attempt_id"),
                attempt.get("execution_fence"), attempt.get("lease_owner"),
                attempt.get("started_at"), receipt[1],
            )):
                return False
        fence = manifest.get("request", {}).get("dispatch_fence")
        if fence is not None:
            if type(fence) is not dict or fence.get("before") != {
                "control_revision": dispatch_state.control_revision,
                "published": {
                    "sequence": dispatch_state.published.sequence,
                    "repository_revision": dispatch_state.published.repository_revision,
                    "ordinal": dispatch_state.published.ordinal,
                },
                "inflight": (
                    None if dispatch_state.inflight is None else {
                        "sequence": dispatch_state.inflight.sequence,
                        "repository_revision": dispatch_state.inflight.repository_revision,
                        "ordinal": dispatch_state.inflight.ordinal,
                    }
                ),
            }:
                return False
            try:
                after_dispatch = fence["after"]
                published = after_dispatch["published"]
                inflight = after_dispatch["inflight"]
                next_dispatch = WorkerOutboxDispatchState(
                    after_dispatch["control_revision"],
                    WorkerOutboxDispatchCursor(
                        published["sequence"],
                        published["repository_revision"],
                        published["ordinal"],
                    ),
                    None if inflight is None else WorkerOutboxOrder(
                        inflight["sequence"],
                        inflight["repository_revision"],
                        inflight["ordinal"],
                    ),
                )
            except (KeyError, TypeError, ValueError):
                return False
            if next_dispatch != dispatch_state:
                dispatch_previous = dispatch_hash
                dispatch_hash = after_sha
                dispatch_updated_at = receipt[4]
                dispatch_state = next_dispatch
        latest_by_target[receipt[7]] = (receipt, manifest, after_sha)
        previous = tail
    if not _row_is(actual_head, (
        expected_worker[0] + len(entries),
        entries[-1][0],
        entries[-1][5],
        previous,
        entries[-1][10],
    )):
        return False
    for target_id, (receipt, manifest, after_sha) in latest_by_target.items():
        after = manifest["after"]
        current_effect = after.get("current_effect")
        unresolved_effect = after.get("unresolved_effect")
        prior_sha = _state_digest_at(connection, target_id, receipt[10])
        current = connection.execute(
            "SELECT target_kind,status,state_revision,attempt_count,last_attempt_id,"
            "last_attempt_number,current_attempt_id,current_attempt_number,"
            "fence_high_water,cancellation_pending,next_attempt_at,lease_owner,"
            "lease_fence,lease_expires_at,current_effect_id,unresolved_effect_id,"
            "last_receipt_transaction_id,previous_state_sha256,state_sha256,updated_at "
            "FROM worker_target_current WHERE target_id=?",
            (target_id,),
        ).fetchone()
        if not _row_is(current, (
            receipt[6], after.get("status"), after.get("state_revision"),
            after.get("attempt_count"), after.get("last_attempt_id"),
            after.get("last_attempt_number"), after.get("current_attempt_id"),
            after.get("current_attempt_number"), after.get("fence_high_water"),
            int(after.get("cancellation_pending")), after.get("next_attempt_at"),
            after.get("lease_owner"), after.get("lease_fence"),
            after.get("lease_expires_at"),
            None if current_effect is None else current_effect.get("effect_id"),
            None if unresolved_effect is None else unresolved_effect.get("effect_id"),
            receipt[1], prior_sha, after_sha, receipt[4],
        )):
            return False
    state = dispatch_state
    expected_dispatch = (
        state.control_revision,
        state.published.sequence,
        None if state.published.sequence == 0 else state.published.repository_revision,
        None if state.published.sequence == 0 else state.published.ordinal,
        None if state.inflight is None else state.inflight.sequence,
        None if state.inflight is None else state.inflight.repository_revision,
        None if state.inflight is None else state.inflight.ordinal,
        dispatch_previous,
        dispatch_hash,
        dispatch_updated_at,
    )
    return _row_is(_dispatch_head_row(connection), expected_dispatch)


def _closed_worker_progress(
    connection: Any,
    profile: BusinessRuntimeProfile,
    expected_sequence: int,
    expected_worker: Tuple[Any, ...],
    before_dispatch: ProductionOutboxDispatchHeadEvidence,
) -> bool:
    entry = connection.execute(
        "SELECT entry_kind,transaction_id FROM worker_ledger_entries "
        "WHERE sequence>? ORDER BY sequence DESC LIMIT 1",
        (expected_sequence,),
    ).fetchone()
    if entry is None:
        return False
    if entry[0] == "worker_execution":
        identity = connection.execute(
            "SELECT request_id FROM worker_execution_receipts "
            "WHERE transaction_id=?",
            (entry[1],),
        ).fetchone()
    elif entry[0] == "pipeline_control":
        identity = connection.execute(
            "SELECT intent_id FROM pipeline_control_receipts "
            "WHERE transaction_id=?",
            (entry[1],),
        ).fetchone()
    else:
        return False
    if identity is None or len(identity) != 1:
        return False
    try:
        mounted = _binding_from_snapshot(
            connection,
            profile,
            _mounted_row(connection),
        )
        audited = _audit_worker_snapshot(
            connection,
            profile,
            mounted,
            identity[0],
            None,
        )
    except _AuditCorrupt as exc:
        if str(exc) != "worker_durable_audit_atomic_inner_root_corrupt":
            return False
        return _worker_progress_receipts_closed(
            connection,
            expected_worker,
            before_dispatch,
        )
    except _AuditConflict:
        return False
    except _AuditUnsupported:
        _fail("sqlite_production_read_progress_unsupported")
    except _AuditBudget:
        _fail("sqlite_production_read_unavailable")
    return audited.result.status is SQLiteWorkerDurableAuditStatus.EXACT


def _verify_exact_worker_heads(
    connection: Any,
    profile: BusinessRuntimeProfile,
    before: ProductionWorkerLedgerHeadEvidence,
    dispatch: ProductionOutboxDispatchHeadEvidence,
    controls: Tuple[_ControlProof, ...],
    committed_at: str,
) -> None:
    worker = _worker_head_row(connection)
    if controls:
        expected_worker = (
            before.state_revision + 1,
            controls[-1].sequence,
            before.ledger_head.tail_sha256,
            controls[-1].ledger_tail_sha256,
            committed_at,
        )
    else:
        expected_worker = (
            before.state_revision,
            before.ledger_head.sequence,
            before.previous_tail_sha256,
            before.ledger_head.tail_sha256,
            before.updated_at,
        )
    dispatch_row = _dispatch_head_row(connection)
    state = dispatch.dispatch_state
    expected_dispatch = (
        state.control_revision,
        state.published.sequence,
        None if state.published.sequence == 0 else state.published.repository_revision,
        None if state.published.sequence == 0 else state.published.ordinal,
        None if state.inflight is None else state.inflight.sequence,
        None if state.inflight is None else state.inflight.repository_revision,
        None if state.inflight is None else state.inflight.ordinal,
        dispatch.previous_head_sha256,
        dispatch.head_sha256,
        dispatch.updated_at,
    )
    if not _row_is(worker, expected_worker) or not _row_is(
        dispatch_row,
        expected_dispatch,
    ):
        if _closed_worker_progress(
            connection,
            profile,
            expected_worker[1],
            expected_worker,
            dispatch,
        ):
            _fail("sqlite_production_read_progress_unsupported")
        _fail("sqlite_production_read_corrupt")


def _state_digest_at(
    connection: Any,
    target_id: str,
    revision: int,
) -> bytes:
    rows = connection.execute(
        "SELECT state_sha256 FROM ("
        "SELECT genesis_state_sha256 AS state_sha256,1 AS revision "
        "FROM worker_targets WHERE target_id=? UNION ALL "
        "SELECT after_state_sha256,after_state_revision "
        "FROM worker_execution_receipts WHERE target_id=? UNION ALL "
        "SELECT after_state_sha256,after_state_revision "
        "FROM pipeline_control_receipts WHERE target_id=?"
        ") WHERE revision=?",
        (target_id, target_id, target_id, revision),
    ).fetchmany(3)
    if (
        not rows
        or len(rows) > 2
        or type(rows[0][0]) is not bytes
        or any(not _same(row[0], rows[0][0]) for row in rows)
    ):
        _fail("sqlite_production_read_corrupt")
    return _digest(rows[0][0])


def _current_expected_from_control(
    connection: Any,
    proof: _ControlProof,
) -> Tuple[Any, ...]:
    after = proof.after
    current_effect = after.get("current_effect")
    unresolved_effect = after.get("unresolved_effect")
    previous = (
        _ZERO
        if proof.before is None
        else _state_digest_at(connection, proof.target_id, proof.before["state_revision"])
    )
    return (
        "pipeline", after.get("status"), after.get("state_revision"),
        after.get("attempt_count"), after.get("last_attempt_id"),
        after.get("last_attempt_number"), after.get("current_attempt_id"),
        after.get("current_attempt_number"), after.get("fence_high_water"),
        int(after.get("cancellation_pending")), after.get("next_attempt_at"),
        after.get("lease_owner"), after.get("lease_fence"),
        after.get("lease_expires_at"),
        None if current_effect is None else current_effect.get("effect_id"),
        None if unresolved_effect is None else unresolved_effect.get("effect_id"),
        proof.transaction_id, previous, proof.after_state_sha256,
    )


def _verify_exact_currents(
    connection: Any,
    targets: Tuple[ProductionWorkerTarget, ...],
    controls: Tuple[_ControlProof, ...],
    committed_at: str,
    atomic_transaction_id: str,
) -> None:
    controlled = {value.target_id: value for value in controls}
    target_ids = {value.target.target_id for value in targets}
    if len(controlled) != len(controls):
        _fail("sqlite_production_read_corrupt")
    for wrapped in targets:
        target = wrapped.target
        proof = controlled.get(target.target_id)
        row = _one(
            connection,
            "SELECT target_kind,status,state_revision,attempt_count,last_attempt_id,"
            "last_attempt_number,current_attempt_id,current_attempt_number,"
            "fence_high_water,cancellation_pending,next_attempt_at,lease_owner,"
            "lease_fence,lease_expires_at,current_effect_id,unresolved_effect_id,"
            "last_receipt_transaction_id,previous_state_sha256,state_sha256,updated_at "
            "FROM worker_target_current WHERE target_id=?",
            (target.target_id,),
        )
        if proof is not None:
            expected = _current_expected_from_control(connection, proof) + (committed_at,)
        else:
            expected = (
                target.target_kind.value, wrapped.genesis_status.value, 1, 0,
                None, None, None, None, 0, 0, None, None, None, None,
                None, None, atomic_transaction_id, _ZERO,
                wrapped.genesis_state_sha256, committed_at,
            )
        if not _row_is(row, expected):
            _fail("sqlite_production_read_corrupt")
    # Transition controls may target a pipeline created by an earlier commit.
    for proof in controls:
        if proof.target_id in target_ids:
            continue
        row = _one(
            connection,
            "SELECT target_kind,status,state_revision,attempt_count,last_attempt_id,"
            "last_attempt_number,current_attempt_id,current_attempt_number,"
            "fence_high_water,cancellation_pending,next_attempt_at,lease_owner,"
            "lease_fence,lease_expires_at,current_effect_id,unresolved_effect_id,"
            "last_receipt_transaction_id,previous_state_sha256,state_sha256,updated_at "
            "FROM worker_target_current WHERE target_id=?",
            (proof.target_id,),
        )
        if not _row_is(
            row,
            _current_expected_from_control(connection, proof) + (committed_at,),
        ):
            _fail("sqlite_production_read_corrupt")


def _targets_sha256(
    targets: Tuple[ProductionWorkerTarget, ...],
    groups: Tuple[Any, ...],
) -> bytes:
    return _production_domain_digest(
        PRODUCTION_ATOMIC_TARGETS_HASH_DOMAIN,
        tuple(
            [len(targets).to_bytes(8, "big")]
            + [
                item
                for group in groups
                for item in (
                    group.target_kind.value.encode("ascii"),
                    len(group.members).to_bytes(8, "big"),
                    group.members_sha256,
                )
            ]
        ),
    )


def _verify_descendant_allocation_batch(
    connection: Any,
    profile: BusinessRuntimeProfile,
    rows: Tuple[Any, ...],
) -> int:
    if not rows:
        _fail("sqlite_production_read_corrupt")
    commit_id = rows[0][2]
    repository_revision = rows[0][3]
    committed_at = rows[0][7]
    if any(
        row[2] != commit_id
        or row[3] != repository_revision
        or row[4] != ordinal
        or row[7] != committed_at
        for ordinal, row in enumerate(rows)
    ):
        _fail("sqlite_production_read_corrupt")
    commit = _one(
        connection,
        "SELECT * FROM business_commits WHERE commit_id=?",
        (commit_id,),
    )
    if (
        commit["commit_kind"] != "change"
        or commit["repository_revision"] != repository_revision
        or commit["outbox_event_count"] != len(rows)
        or not _same(
            _business_manifest_digest(commit["manifest_bytes"]),
            commit["manifest_sha256"],
        )
    ):
        _fail("sqlite_production_read_corrupt")
    commit_manifest = _canonical_json(commit["manifest_bytes"])
    outbox, _ = _read_outbox(connection, commit)
    actions, _ = _read_actions(connection, commit)
    pipelines, _ = _read_pipelines(connection, commit)
    if (
        commit_manifest.get("outbox") != list(outbox)
        or commit_manifest.get("durable_actions") != list(actions)
        or commit_manifest.get("pipeline_intents") != list(pipelines)
        or type(commit_manifest.get("authority")) is not dict
        or commit_manifest["authority"].get("revision")
        != repository_revision - 1
        or type(commit_manifest.get("after")) is not dict
        or commit_manifest["after"].get("repository_revision")
        != repository_revision
        or type(commit_manifest.get("commit")) is not dict
        or commit_manifest["commit"].get("commit_id") != commit_id
    ):
        _fail("sqlite_production_read_corrupt")

    target_rows = _target_rows(connection, commit_id)
    targets = tuple(_target_from_row(row) for row in target_rows)
    groups = _verify_target_mapping(targets, pipelines, actions, outbox, commit)
    controls = _read_controls(
        connection,
        commit,
        commit_manifest,
        pipelines,
        committed_at,
    )
    _verify_pipeline_genesis(groups[0].members, pipelines, controls)
    receipt, _inner = _read_atomic_receipt(
        connection,
        commit,
        pipelines,
        controls,
    )
    target_sha = _targets_sha256(targets, groups)
    if (
        receipt[2] != committed_at
        or receipt[16] != len(targets)
        or not _same(receipt[17], target_sha)
        or any(row[26] != committed_at for row in target_rows)
    ):
        _fail("sqlite_production_read_corrupt")
    _worker, _dispatch, before, after = _verify_outer_manifest(
        connection,
        profile,
        commit,
        commit_manifest,
        receipt,
        groups,
    )
    outbox_targets = groups[2].members
    if (
        len(outbox_targets) != len(rows)
        or before.sequence + len(rows) != after.sequence
        or before.sequence + 1 != rows[0][0]
        or not _same(before.tail_sha256, rows[0][5])
        or after.sequence != rows[-1][0]
        or not _same(after.tail_sha256, rows[-1][6])
        or after.state_revision != before.state_revision + 1
        or after.updated_at != committed_at
    ):
        _fail("sqlite_production_read_corrupt")
    for row, wrapped, event in zip(rows, outbox_targets, outbox):
        order = wrapped.target.outbox_order
        if (
            row[1] != event["event_id"]
            or wrapped.target.target_id != event["event_id"]
            or order is None
            or order.sequence != row[0]
            or order.repository_revision != repository_revision
            or order.ordinal != row[4]
        ):
            _fail("sqlite_production_read_corrupt")
    return repository_revision


def _verify_allocations(
    connection: Any,
    profile: BusinessRuntimeProfile,
    commit: Any,
    outbox_targets: Tuple[ProductionWorkerTarget, ...],
    before: ProductionOutboxAllocationHead,
    after: ProductionOutboxAllocationHead,
    committed_at: str,
) -> int:
    rows = connection.execute(
        "SELECT sequence,event_id,commit_id,repository_revision,ordinal,"
        "previous_tail_sha256,allocation_tail_sha256,allocated_at "
        "FROM outbox_allocations WHERE commit_id=? ORDER BY sequence",
        (commit["commit_id"],),
    ).fetchall()
    if len(rows) != len(outbox_targets):
        _fail("sqlite_production_read_corrupt")
    previous = before.tail_sha256
    for offset, (row, wrapped) in enumerate(zip(rows, outbox_targets), start=1):
        order = wrapped.target.outbox_order
        try:
            tail = _production_outbox_allocation_tail(
                sequence=before.sequence + offset,
                event_id=wrapped.target.target_id,
                commit_id=commit["commit_id"],
                repository_revision=commit["repository_revision"],
                ordinal=wrapped.source_ordinal,
                previous_tail_sha256=previous,
            )
        except (TypeError, ValueError):
            _fail("sqlite_production_read_corrupt")
        if (
            order is None
            or not _row_is(row, (
                before.sequence + offset, wrapped.target.target_id,
                commit["commit_id"], commit["repository_revision"],
                wrapped.source_ordinal, previous, tail, committed_at,
            ))
            or order.sequence != row[0]
            or order.repository_revision != row[3]
            or order.ordinal != row[4]
        ):
            _fail("sqlite_production_read_corrupt")
        previous = tail
    expected_after = before if not rows else ProductionOutboxAllocationHead(
        state_revision=before.state_revision + 1,
        sequence=rows[-1][0],
        previous_tail_sha256=before.tail_sha256,
        tail_sha256=rows[-1][6],
        updated_at=committed_at,
    )
    if expected_after != after:
        _fail("sqlite_production_read_corrupt")

    head = _one(
        connection,
        "SELECT state_revision,sequence,previous_tail_sha256,tail_sha256,updated_at "
        "FROM outbox_allocation_head WHERE singleton=1",
        (),
    )
    if head[1] < after.sequence:
        _fail("sqlite_production_read_corrupt")
    descendant_count = head[1] - after.sequence
    if descendant_count > _MAX_DESCENDANT_ALLOCATION_ROWS:
        _fail("sqlite_production_read_unavailable")
    descendant = connection.execute(
        "SELECT sequence,event_id,commit_id,repository_revision,ordinal,"
        "previous_tail_sha256,allocation_tail_sha256,allocated_at "
        "FROM outbox_allocations WHERE sequence>? AND sequence<=? ORDER BY sequence",
        (after.sequence, head[1]),
    ).fetchall()
    if len(descendant) != head[1] - after.sequence:
        _fail("sqlite_production_read_corrupt")
    previous = after.tail_sha256
    batches = []
    current_batch = []
    seen_commits = set()
    for expected_sequence, row in enumerate(descendant, start=after.sequence + 1):
        try:
            tail = _production_outbox_allocation_tail(
                sequence=expected_sequence,
                event_id=row[1],
                commit_id=row[2],
                repository_revision=row[3],
                ordinal=row[4],
                previous_tail_sha256=previous,
            )
        except (TypeError, ValueError):
            _fail("sqlite_production_read_corrupt")
        if row[0] != expected_sequence or not _same(row[5], previous) or not _same(row[6], tail):
            _fail("sqlite_production_read_corrupt")
        if current_batch and current_batch[-1][2] != row[2]:
            batch = tuple(current_batch)
            if batch[0][2] in seen_commits:
                _fail("sqlite_production_read_corrupt")
            seen_commits.add(batch[0][2])
            batches.append(batch)
            current_batch = []
        current_batch.append(row)
        previous = tail
    if current_batch:
        batch = tuple(current_batch)
        if batch[0][2] in seen_commits:
            _fail("sqlite_production_read_corrupt")
        batches.append(batch)
    if len(batches) > _MAX_DESCENDANT_ALLOCATION_BATCHES:
        _fail("sqlite_production_read_unavailable")
    previous_revision = commit["repository_revision"]
    for batch in batches:
        revision = _verify_descendant_allocation_batch(
            connection,
            profile,
            batch,
        )
        if revision <= previous_revision:
            _fail("sqlite_production_read_corrupt")
        previous_revision = revision
    if not _same(head[3], previous):
        _fail("sqlite_production_read_corrupt")
    latest = connection.execute(
        "SELECT commit_id,allocated_at FROM outbox_allocations WHERE sequence=?",
        (head[1],),
    ).fetchone()
    if head[1] == 0:
        if latest is not None or head[0] != 1 or not _same(head[2], _ZERO):
            _fail("sqlite_production_read_corrupt")
    else:
        if latest is None or head[4] != latest[1]:
            _fail("sqlite_production_read_corrupt")
        first_latest = _one(
            connection,
            "SELECT previous_tail_sha256 FROM outbox_allocations "
            "WHERE commit_id=? ORDER BY sequence LIMIT 1",
            (latest[0],),
        )
        if not _same(head[2], first_latest[0]):
            _fail("sqlite_production_read_corrupt")
        if head[0] != after.state_revision + len(batches):
            _fail("sqlite_production_read_corrupt")
    return head[1]


def _read_production_pinned(
    connection: Any,
    profile: BusinessRuntimeProfile,
    business: SQLiteBusinessHead,
) -> _PinnedProductionHead:
    commit = _one(connection, "SELECT * FROM business_commits WHERE commit_id=?", (business.commit_id,))
    if commit["commit_kind"] != "change":
        _fail("sqlite_production_read_not_committed")
    commit_manifest = _canonical_json(commit["manifest_bytes"])
    outbox, _ = _read_outbox(connection, commit)
    actions, _ = _read_actions(connection, commit)
    pipelines, _ = _read_pipelines(connection, commit)
    target_rows = _target_rows(connection, business.commit_id)
    targets = tuple(_target_from_row(row) for row in target_rows)
    committed_at = _one(
        connection,
        "SELECT committed_at FROM atomic_business_write_receipts WHERE commit_id=?",
        (business.commit_id,),
    )[0]
    if any(row[26] != committed_at for row in target_rows):
        _fail("sqlite_production_read_corrupt")
    groups = _verify_target_mapping(targets, pipelines, actions, outbox, commit)
    controls = _read_controls(
        connection, commit, commit_manifest, pipelines,
        committed_at,
    )
    _verify_pipeline_genesis(groups[0].members, pipelines, controls)
    receipt, _inner = _read_atomic_receipt(
        connection, commit, pipelines, controls
    )
    target_sha = _targets_sha256(targets, groups)
    if receipt[16] != len(targets) or not _same(receipt[17], target_sha):
        _fail("sqlite_production_read_corrupt")
    before_worker, before_dispatch, before_allocation, after_allocation = (
        _verify_outer_manifest(
            connection, profile, commit, commit_manifest, receipt, groups
        )
    )
    if (
        before_worker.ledger_head.sequence != receipt[7]
        or not _same(before_worker.ledger_head.tail_sha256, receipt[8])
    ):
        _fail("sqlite_production_read_corrupt")
    _verify_exact_worker_heads(
        connection,
        profile,
        before_worker,
        before_dispatch,
        controls,
        receipt[2],
    )
    _verify_exact_currents(
        connection, targets, controls, receipt[2], receipt[0]
    )
    allocation_head_sequence = _verify_allocations(
        connection, profile, commit, groups[2].members,
        before_allocation, after_allocation, receipt[2],
    )
    result = SQLiteProductionHead(
        business=business,
        commit_id=business.commit_id,
        target_count=len(targets),
        pipeline_target_count=len(groups[0].members),
        action_target_count=len(groups[1].members),
        outbox_target_count=len(groups[2].members),
        control_count=len(controls),
        allocation_count=len(groups[2].members),
        allocation_head_sequence=allocation_head_sequence,
        targets_sha256=target_sha,
        production_manifest_sha256=receipt[15],
        production_receipt_sha256=receipt[18],
    )
    fence = _publish_fence(
        connection,
        tuple(value.target.target_id for value in targets)
        + tuple(value.target_id for value in controls),
    )
    return _PinnedProductionHead(result, fence)


class SQLiteProductionReader:
    """Read one active production head without accepting prior plan evidence."""

    __slots__ = ("_database",)

    def __init__(self, database: SqliteDatabase) -> None:
        if type(database) is not SqliteDatabase:
            raise TypeError("database must be an exact SqliteDatabase")
        self._database = database

    def read_current_head(
        self,
        project_id: str,
        generation: int,
        *,
        expected_commit_id: Optional[str] = None,
    ) -> SQLiteProductionHead:
        project_id = _input_text(project_id)
        generation = _positive(generation)
        if expected_commit_id is not None:
            expected_commit_id = _commit_identity(expected_commit_id)
        return self._read(
            project_id=project_id,
            generation=generation,
            commit_id=expected_commit_id,
        )

    def read_commit(self, commit_id: str) -> SQLiteProductionHead:
        return self._read(commit_id=_commit_identity(commit_id))

    def _read(
        self,
        *,
        project_id: Optional[str] = None,
        generation: Optional[int] = None,
        commit_id: Optional[str] = None,
    ) -> SQLiteProductionHead:
        connection = None
        cleanup_complete = True
        unexpected = False
        unavailable = False
        profile = None
        pinned = None
        try:
            connection, _filesystem = self._database._open_unvalidated()
            connection.execute("BEGIN")
            profile = _validate_business_runtime_connection(
                self._database,
                connection,
                access=BusinessRuntimeAccess.READER,
            )
            if profile.authority_state is BusinessAuthorityState.READ_ONLY:
                # A business snapshot remains readable while the repository is
                # deliberately read-only, but this strict production boundary
                # cannot publish an active scheduler head from that authority.
                _fail("sqlite_production_read_authority_inactive")
            if profile.authority_state is not BusinessAuthorityState.ACTIVE:
                _fail("sqlite_production_read_repair_required")
            connection.set_authorizer(_ReadOnlyAuthorizer())
            if project_id is None:
                root = connection.execute(
                    "SELECT project_id,generation FROM business_commits WHERE commit_id=?",
                    (commit_id,),
                ).fetchall()
                if not root:
                    _fail("sqlite_production_read_not_committed")
                if len(root) != 1:
                    _fail("sqlite_production_read_corrupt")
                project_id, generation = root[0]
            assert project_id is not None and generation is not None
            try:
                business = _read_pinned(
                    connection,
                    profile,
                    project_id,
                    generation,
                    None,
                    None,
                    commit_id,
                )
            except SQLiteBusinessReadError as exc:
                if exc.code.endswith("revision_stale"):
                    _fail("sqlite_production_read_head_stale")
                _fail("sqlite_production_read_base_corrupt")
            pinned = _read_production_pinned(connection, profile, business)
        except SQLiteProductionReadError:
            raise
        except BaseException as exc:
            if not isinstance(exc, Exception):
                raise
            if _storage_unavailable(exc):
                unavailable = True
            else:
                unexpected = True
        finally:
            cleanup_complete = _finish_read_connection(
                connection,
                reset_authorizer=True,
            )
        if unavailable or not cleanup_complete:
            _fail("sqlite_production_read_unavailable")
        if unexpected or pinned is None or profile is None:
            _fail("sqlite_production_read_corrupt")

        confirmation = None
        cleanup_complete = True
        unexpected = False
        unavailable = False
        try:
            confirmation, _filesystem = self._database._open_unvalidated()
            confirmation.execute("BEGIN")
            confirmed = _validate_business_runtime_connection(
                self._database,
                confirmation,
                access=BusinessRuntimeAccess.READER,
            )
            if not _publish_fence_is_compatible(profile, confirmed):
                _fail("sqlite_production_read_authority_changed")
            confirmation.set_authorizer(_ReadOnlyAuthorizer())
            confirmed_fence = _publish_fence(
                confirmation,
                pinned.fence.target_ids,
            )
            if confirmed_fence != pinned.fence:
                _fail("sqlite_production_read_head_changed")
        except SQLiteProductionReadError:
            raise
        except BaseException as exc:
            if not isinstance(exc, Exception):
                raise
            if _storage_unavailable(exc):
                unavailable = True
            else:
                unexpected = True
        finally:
            cleanup_complete = _finish_read_connection(
                confirmation,
                reset_authorizer=True,
            )
        if unavailable or not cleanup_complete:
            _fail("sqlite_production_read_unavailable")
        if unexpected:
            _fail("sqlite_production_read_corrupt")
        return pinned.result


__all__ = [
    "SQLiteProductionHead",
    "SQLiteProductionReadError",
    "SQLiteProductionReader",
]
