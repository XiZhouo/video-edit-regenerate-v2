"""Canonical issuer for the schema-v8 production atomic-write closure.

This outer protocol preserves the frozen atomic-v1 and worker-v3 vectors.  It
adds only the production facts those protocols deliberately do not own:
mounted authorization, worker routing, genesis current state, target-set root,
and global outbox allocation order.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any, NamedTuple, Tuple

from business_atomic_write_integrity import (
    AtomicBusinessWriteIntegrityError,
    AtomicBusinessWritePlan,
    _require_atomic_business_write_plan,
)
from business_codec import BusinessCodecError, encode_payload_bounded
from business_transaction_contract import PipelineIntentKind
from production_atomic_write_contract import (
    MAX_PRODUCTION_ATOMIC_MANIFEST_BYTES,
    MAX_PRODUCTION_ATOMIC_MANIFEST_NODES,
    MAX_PRODUCTION_ATOMIC_TARGETS,
    PRODUCTION_ATOMIC_TARGET_GROUP_ORDER,
    PRODUCTION_ATOMIC_WRITE_MANIFEST_SCHEMA,
    PRODUCTION_ATOMIC_WRITE_PROTOCOL,
    PRODUCTION_ATOMIC_WRITE_STATUS,
    PRODUCTION_OUTBOX_IDEMPOTENCY_POLICY,
    PRODUCTION_OUTBOX_MAX_ATTEMPTS,
    PRODUCTION_OUTBOX_REPLAY_POLICY,
    ProductionAtomicTargetGroupEvidence,
    ProductionAtomicWriteContractError,
    ProductionAtomicWriteEvidence,
    ProductionAtomicWriteReceiptEvidence,
    ProductionMountedAuthorityEvidence,
    ProductionOutboxDispatchHeadEvidence,
    ProductionOutboxAllocationEvidence,
    ProductionOutboxAllocationHead,
    ProductionWorkerLedgerHeadEvidence,
    ProductionWorkerTarget,
)
from sqlite_production_authority import (
    ProductionMountedAuthorityBinding,
    ProductionOutboxDispatchHeadBinding,
    ProductionWorkerLedgerHeadBinding,
    SQLiteProductionAuthorityError,
    _require_mounted_binding,
    _require_outbox_dispatch_head_binding,
    _require_worker_ledger_head_binding,
)
from worker_execution_contract import (
    PIPELINE_BUSINESS_CONTROL_TRANSACTION_PREFIX,
    PipelineBusinessControlOperation,
    WorkerExecutionContractError,
    WorkerExecutionStatus,
    WorkerLedgerHead,
    WorkerOutboxOrder,
    WorkerOutboxDispatchCursor,
    WorkerOutboxDispatchState,
    WorkerTarget,
    WorkerTargetKind,
)
from worker_execution_integrity import WORKER_EXECUTION_AFTER_STATE_HASH_DOMAIN


PRODUCTION_OUTBOX_ALLOCATION_TAIL_HASH_DOMAIN = (
    b"ze.production-outbox-allocation.tail.v1"
)
PRODUCTION_ATOMIC_CONTROL_MEMBER_HASH_DOMAIN = (
    b"ze.production-atomic-control-member.v1"
)
PRODUCTION_WORKER_GENESIS_STATE_HASH_DOMAIN = (
    b"ze.production-worker-genesis-state.v1"
)
PRODUCTION_ATOMIC_TARGET_MEMBER_HASH_DOMAIN = (
    b"ze.production-atomic-target-member.v1"
)
PRODUCTION_ATOMIC_TARGET_GROUP_HASH_DOMAIN = (
    b"ze.production-atomic-target-group.v1"
)
PRODUCTION_ATOMIC_TARGETS_HASH_DOMAIN = (
    b"ze.production-atomic-targets.v1"
)
PRODUCTION_ATOMIC_MANIFEST_HASH_DOMAIN = (
    b"ze.production-atomic-write.manifest-digest.v1"
)
PRODUCTION_ATOMIC_RECEIPT_HASH_DOMAIN = (
    b"ze.production-atomic-write.receipt.v1"
)

_PLAN_ISSUER = object()
_ZERO_DIGEST = b"\0" * 32
_LOWER_HEX = frozenset("0123456789abcdef")
_MAX_SQLITE_INTEGER = 2**63 - 1


class ProductionAtomicWriteIntegrityError(ValueError):
    """Stable, payload-free production-plan integrity failure."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        if type(code) is not str or not code:
            raise TypeError("code must be a non-empty exact string")
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise ProductionAtomicWriteIntegrityError(code)


def _frame(hasher: Any, value: bytes) -> None:
    hasher.update(len(value).to_bytes(8, "big"))
    hasher.update(value)


def _domain_digest(domain: bytes, values: Tuple[bytes, ...]) -> bytes:
    hasher = hashlib.sha256()
    _frame(hasher, domain)
    for value in values:
        _frame(hasher, value)
    return hasher.digest()


def _hash_integer(value: Any, *, minimum: int = 0, maximum: int = _MAX_SQLITE_INTEGER) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _fail("production_hash_input_invalid")
    return value


def _hash_identifier(value: Any) -> str:
    if (
        type(value) is not str
        or len(value) != 32
        or any(character not in _LOWER_HEX for character in value)
    ):
        _fail("production_hash_input_invalid")
    return value


def _hash_digest(value: Any) -> bytes:
    if type(value) is not bytes or len(value) != 32:
        _fail("production_hash_input_invalid")
    return value


def _encode(value: Any, code: str) -> bytes:
    try:
        return encode_payload_bounded(
            value,
            MAX_PRODUCTION_ATOMIC_MANIFEST_BYTES,
            MAX_PRODUCTION_ATOMIC_MANIFEST_NODES,
        )
    except BusinessCodecError as exc:
        raise ProductionAtomicWriteIntegrityError(code) from exc


def _production_outbox_allocation_tail(
    *,
    sequence: int,
    event_id: str,
    commit_id: str,
    repository_revision: int,
    ordinal: int,
    previous_tail_sha256: bytes,
) -> bytes:
    """Shared fixed tail formula used by planner, writer and auditor."""

    sequence = _hash_integer(sequence, minimum=1)
    event_id = _hash_identifier(event_id)
    commit_id = _hash_identifier(commit_id)
    repository_revision = _hash_integer(repository_revision, minimum=1)
    ordinal = _hash_integer(ordinal, maximum=9_999)
    previous_tail_sha256 = _hash_digest(previous_tail_sha256)
    if (sequence == 1) != hmac.compare_digest(
        previous_tail_sha256,
        _ZERO_DIGEST,
    ):
        _fail("production_outbox_allocation_invalid")
    return _domain_digest(
        PRODUCTION_OUTBOX_ALLOCATION_TAIL_HASH_DOMAIN,
        (
            sequence.to_bytes(8, "big"),
            previous_tail_sha256,
            event_id.encode("ascii"),
            commit_id.encode("ascii"),
            repository_revision.to_bytes(8, "big"),
            ordinal.to_bytes(8, "big"),
        ),
    )


def _production_atomic_control_member_sha256(
    *,
    commit_id: str,
    ordinal: int,
    intent_id: str,
    control_transaction_id: str,
    sequence: int,
    ledger_tail_sha256: bytes,
) -> bytes:
    """Shared binding formula for ``atomic_business_control_members``."""

    commit_id = _hash_identifier(commit_id)
    ordinal = _hash_integer(ordinal, maximum=9_999)
    intent_id = _hash_identifier(intent_id)
    if (
        type(control_transaction_id) is not str
        or control_transaction_id
        != PIPELINE_BUSINESS_CONTROL_TRANSACTION_PREFIX + intent_id
    ):
        _fail("production_atomic_control_member_invalid")
    sequence = _hash_integer(sequence, minimum=1)
    ledger_tail_sha256 = _hash_digest(ledger_tail_sha256)
    return _domain_digest(
        PRODUCTION_ATOMIC_CONTROL_MEMBER_HASH_DOMAIN,
        (
            commit_id.encode("ascii"),
            ordinal.to_bytes(8, "big"),
            intent_id.encode("ascii"),
            control_transaction_id.encode("utf-8"),
            sequence.to_bytes(8, "big"),
            ledger_tail_sha256,
        ),
    )


def _zero_current_manifest(value: ProductionWorkerTarget) -> dict:
    return {
        "target_kind": value.target.target_kind.value,
        "target_id": value.target.target_id,
        "status": value.genesis_status.value,
        "state_revision": 1,
        "attempt_count": 0,
        "last_attempt_id": None,
        "last_attempt_number": None,
        "current_attempt_id": None,
        "current_attempt_number": None,
        "fence_high_water": 0,
        "cancellation_pending": False,
        "next_attempt_at": None,
        "lease_owner": None,
        "lease_fence": None,
        "lease_expires_at": None,
        "current_effect_id": None,
        "unresolved_effect_id": None,
        "previous_state_sha256": _ZERO_DIGEST.hex(),
    }


def _production_worker_genesis_state_sha256(
    target: WorkerTarget,
    *,
    status: WorkerExecutionStatus,
) -> bytes:
    """Target-bound genesis digest for action/outbox atomic creation."""

    try:
        shell = ProductionWorkerTarget(
            source_ordinal=0,
            target=target,
            genesis_status=status,
            genesis_state_sha256=b"x" * 32,
            action_type=(
                "placeholder"
                if target.target_kind is WorkerTargetKind.ACTION
                else None
            ),
            outbox_topic=(
                "placeholder"
                if target.target_kind is WorkerTargetKind.OUTBOX
                else None
            ),
            outbox_partition_key=(
                "placeholder"
                if target.target_kind is WorkerTargetKind.OUTBOX
                else None
            ),
        )
    except (ProductionAtomicWriteContractError, TypeError):
        _fail("production_worker_genesis_invalid")
    if target.target_kind is WorkerTargetKind.PIPELINE:
        _fail("production_pipeline_genesis_requires_control_receipt")
    return _domain_digest(
        PRODUCTION_WORKER_GENESIS_STATE_HASH_DOMAIN,
        (_encode(_zero_current_manifest(shell), "production_worker_genesis_invalid"),),
    )


def _payload_manifest(value: Any) -> dict:
    return {
        "schema": value.schema,
        "codec_id": value.codec_id,
        "codec_version": value.codec_version,
        "bytes": len(value.payload_bytes),
        "sha256": value.payload_sha256.hex(),
    }


def _outbox_order_manifest(value: Any) -> Any:
    if value is None:
        return None
    return {
        "sequence": value.sequence,
        "repository_revision": value.repository_revision,
        "ordinal": value.ordinal,
    }


def _base_target_manifest(value: WorkerTarget) -> dict:
    return {
        "kind": value.target_kind.value,
        "id": value.target_id,
        "project_id": value.project_id,
        "generation": value.generation,
        "document_id": value.document_id,
        "origin_commit_id": value.origin_commit_id,
        "payload": _payload_manifest(value.payload),
        "replay_policy": value.replay_policy.value,
        "idempotency_policy": value.idempotency_policy.value,
        "idempotency_scope": value.idempotency_scope,
        "idempotency_key": value.idempotency_key,
        "max_attempts": value.max_attempts,
        "outbox_order": _outbox_order_manifest(value.outbox_order),
        "pipeline_id": value.pipeline_id,
        "intent_type": value.intent_type,
    }


def _production_target_member_manifest(value: ProductionWorkerTarget) -> dict:
    genesis_protocol = (
        WORKER_EXECUTION_AFTER_STATE_HASH_DOMAIN.decode("ascii")
        if value.target.target_kind is WorkerTargetKind.PIPELINE
        else PRODUCTION_WORKER_GENESIS_STATE_HASH_DOMAIN.decode("ascii")
    )
    return {
        "source_ordinal": value.source_ordinal,
        "base_worker_target": _base_target_manifest(value.target),
        "routing": {
            "action_type": value.action_type,
            "outbox_topic": value.outbox_topic,
            "outbox_partition_key": value.outbox_partition_key,
        },
        "genesis": {
            "status": value.genesis_status.value,
            "state_sha256": value.genesis_state_sha256.hex(),
            "hash_protocol": genesis_protocol,
        },
    }


def _production_target_member_sha256(value: ProductionWorkerTarget) -> bytes:
    return _domain_digest(
        PRODUCTION_ATOMIC_TARGET_MEMBER_HASH_DOMAIN,
        (_encode(
            _production_target_member_manifest(value),
            "production_target_member_invalid",
        ),),
    )


def _production_target_group_sha256(
    target_kind: WorkerTargetKind,
    members: Tuple[ProductionWorkerTarget, ...],
) -> bytes:
    values = (
        target_kind.value.encode("ascii"),
        len(members).to_bytes(8, "big"),
        *tuple(_production_target_member_sha256(value) for value in members),
    )
    return _domain_digest(PRODUCTION_ATOMIC_TARGET_GROUP_HASH_DOMAIN, values)


def _production_targets_sha256(
    groups: Tuple[ProductionAtomicTargetGroupEvidence, ...],
) -> bytes:
    values = [sum(len(value.members) for value in groups).to_bytes(8, "big")]
    for group in groups:
        values.extend((
            group.target_kind.value.encode("ascii"),
            len(group.members).to_bytes(8, "big"),
            group.members_sha256,
        ))
    return _domain_digest(PRODUCTION_ATOMIC_TARGETS_HASH_DOMAIN, tuple(values))


def _canonical_target(value: Any) -> WorkerTarget:
    try:
        rebuilt = WorkerTarget(
            target_kind=value.target_kind,
            target_id=value.target_id,
            project_id=value.project_id,
            generation=value.generation,
            document_id=value.document_id,
            origin_commit_id=value.origin_commit_id,
            payload=value.payload,
            replay_policy=value.replay_policy,
            idempotency_policy=value.idempotency_policy,
            idempotency_scope=value.idempotency_scope,
            idempotency_key=value.idempotency_key,
            max_attempts=value.max_attempts,
            outbox_order=(
                None
                if value.outbox_order is None
                else WorkerOutboxOrder(
                    value.outbox_order.sequence,
                    value.outbox_order.repository_revision,
                    value.outbox_order.ordinal,
                )
            ),
            pipeline_id=value.pipeline_id,
            intent_type=value.intent_type,
        )
    except (AttributeError, TypeError, WorkerExecutionContractError):
        _fail("production_atomic_target_invalid")
    if type(value) is not WorkerTarget or rebuilt != value:
        _fail("production_atomic_target_changed")
    return rebuilt


def _canonical_production_target(value: Any) -> ProductionWorkerTarget:
    try:
        rebuilt = ProductionWorkerTarget(
            source_ordinal=value.source_ordinal,
            target=_canonical_target(value.target),
            genesis_status=value.genesis_status,
            genesis_state_sha256=value.genesis_state_sha256,
            action_type=value.action_type,
            outbox_topic=value.outbox_topic,
            outbox_partition_key=value.outbox_partition_key,
        )
    except (
        AttributeError,
        ProductionAtomicWriteContractError,
        TypeError,
    ):
        _fail("production_atomic_target_invalid")
    if type(value) is not ProductionWorkerTarget or rebuilt != value:
        _fail("production_atomic_target_changed")
    return rebuilt


def _mount_manifest(value: ProductionMountedAuthorityEvidence) -> dict:
    return {
        "authority": value.authority,
        "authority_state": value.authority_state,
        "repository_epoch": value.repository_epoch,
        "cutover_id": value.cutover_id,
        "source_admission_revision": value.source_admission_revision,
        "repository_revision": value.repository_revision,
        "control_revision": value.control_revision,
        "runtime_generation": value.runtime_generation,
        "code_digest": value.code_digest.hex(),
        "target_schema_version": value.target_schema_version,
        "target_schema_fingerprint": value.target_schema_fingerprint,
        "mount_transaction_id": value.mount_transaction_id,
        "mount_manifest_sha256": value.mount_manifest_sha256.hex(),
        "mounted_at": value.mounted_at,
    }


def _allocation_head_manifest(value: ProductionOutboxAllocationHead) -> dict:
    return {
        "state_revision": value.state_revision,
        "sequence": value.sequence,
        "previous_tail_sha256": value.previous_tail_sha256.hex(),
        "tail_sha256": value.tail_sha256.hex(),
        "updated_at": value.updated_at,
    }


def _worker_ledger_head_manifest(
    value: ProductionWorkerLedgerHeadEvidence,
) -> dict:
    return {
        "state_revision": value.state_revision,
        "sequence": value.ledger_head.sequence,
        "previous_tail_sha256": value.previous_tail_sha256.hex(),
        "tail_sha256": value.ledger_head.tail_sha256.hex(),
        "updated_at": value.updated_at,
    }


def _outbox_dispatch_head_manifest(
    value: ProductionOutboxDispatchHeadEvidence,
) -> dict:
    state = value.dispatch_state
    return {
        "state_revision": state.control_revision,
        "published": {
            "sequence": state.published.sequence,
            "repository_revision": state.published.repository_revision,
            "ordinal": state.published.ordinal,
        },
        "inflight": (
            None
            if state.inflight is None
            else {
                "sequence": state.inflight.sequence,
                "repository_revision": state.inflight.repository_revision,
                "ordinal": state.inflight.ordinal,
            }
        ),
        "previous_head_sha256": value.previous_head_sha256.hex(),
        "head_sha256": value.head_sha256.hex(),
        "updated_at": value.updated_at,
    }


def _build_production_manifest(
    *,
    atomic: AtomicBusinessWritePlan,
    mounted: ProductionMountedAuthorityEvidence,
    target_count: int,
    targets_sha256: bytes,
    before_worker_ledger: ProductionWorkerLedgerHeadEvidence,
    before_outbox_dispatch: ProductionOutboxDispatchHeadEvidence,
    before_allocation: ProductionOutboxAllocationHead,
    allocation_count: int,
    after_allocation: ProductionOutboxAllocationHead,
) -> bytes:
    """Build the bounded summary; target payloads remain in worker_targets."""

    receipt = atomic.expected_receipt
    value = {
        "schema": PRODUCTION_ATOMIC_WRITE_MANIFEST_SCHEMA,
        "protocol": PRODUCTION_ATOMIC_WRITE_PROTOCOL,
        "status": PRODUCTION_ATOMIC_WRITE_STATUS,
        "hash_domains": {
            "manifest": PRODUCTION_ATOMIC_MANIFEST_HASH_DOMAIN.decode("ascii"),
            "receipt": PRODUCTION_ATOMIC_RECEIPT_HASH_DOMAIN.decode("ascii"),
            "targets": PRODUCTION_ATOMIC_TARGETS_HASH_DOMAIN.decode("ascii"),
            "target_group": PRODUCTION_ATOMIC_TARGET_GROUP_HASH_DOMAIN.decode("ascii"),
            "target_member": PRODUCTION_ATOMIC_TARGET_MEMBER_HASH_DOMAIN.decode("ascii"),
            "genesis": PRODUCTION_WORKER_GENESIS_STATE_HASH_DOMAIN.decode("ascii"),
            "outbox_allocation_tail": (
                PRODUCTION_OUTBOX_ALLOCATION_TAIL_HASH_DOMAIN.decode("ascii")
            ),
            "control_member": (
                PRODUCTION_ATOMIC_CONTROL_MEMBER_HASH_DOMAIN.decode("ascii")
            ),
        },
        "pure_atomic": {
            "commit_id": atomic.atomic_plan.commit_id,
            "transaction_id": receipt.receipt_parent.transaction_id,
            "receipt_kind": receipt.receipt_parent.kind,
            "committed_at": receipt.receipt_parent.committed_at,
            "manifest_sha256": receipt.manifest_sha256.hex(),
            "receipt_sha256": receipt.receipt_sha256.hex(),
        },
        "mounted_authority": _mount_manifest(mounted),
        "scheduler_heads": {
            "worker_ledger": _worker_ledger_head_manifest(
                before_worker_ledger
            ),
            "outbox_dispatch": _outbox_dispatch_head_manifest(
                before_outbox_dispatch
            ),
        },
        "outbox_allocation": {
            "count": allocation_count,
            "before": _allocation_head_manifest(before_allocation),
            "after": _allocation_head_manifest(after_allocation),
        },
        "targets": {
            "group_order": [value.value for value in PRODUCTION_ATOMIC_TARGET_GROUP_ORDER],
            "count": target_count,
            "sha256": targets_sha256.hex(),
        },
    }
    return _encode(value, "production_atomic_manifest_invalid")


def _production_manifest_sha256(value: bytes) -> bytes:
    return _domain_digest(PRODUCTION_ATOMIC_MANIFEST_HASH_DOMAIN, (value,))


def _production_receipt_sha256(
    *,
    atomic: AtomicBusinessWritePlan,
    production_manifest_sha256: bytes,
    target_count: int,
    targets_sha256: bytes,
) -> bytes:
    receipt = atomic.expected_receipt
    return _domain_digest(
        PRODUCTION_ATOMIC_RECEIPT_HASH_DOMAIN,
        (
            atomic.atomic_plan.commit_id.encode("ascii"),
            receipt.receipt_parent.transaction_id.encode("utf-8"),
            receipt.receipt_parent.kind.encode("utf-8"),
            receipt.receipt_parent.committed_at.encode("ascii"),
            receipt.receipt_sha256,
            production_manifest_sha256,
            target_count.to_bytes(8, "big"),
            targets_sha256,
        ),
    )


def _groups(
    targets: Tuple[ProductionWorkerTarget, ...],
) -> Tuple[ProductionAtomicTargetGroupEvidence, ...]:
    grouped = []
    canonical_flat = []
    for kind in PRODUCTION_ATOMIC_TARGET_GROUP_ORDER:
        members = tuple(value for value in targets if value.target.target_kind is kind)
        canonical_flat.extend(members)
        try:
            grouped.append(ProductionAtomicTargetGroupEvidence(
                target_kind=kind,
                members=members,
                members_sha256=_production_target_group_sha256(kind, members),
            ))
        except ProductionAtomicWriteContractError:
            _fail("production_atomic_target_order_invalid")
    if tuple(canonical_flat) != targets:
        _fail("production_atomic_target_order_invalid")
    return tuple(grouped)


def _advance_allocations(
    atomic: AtomicBusinessWritePlan,
    outbox_targets: Tuple[ProductionWorkerTarget, ...],
    before: ProductionOutboxAllocationHead,
) -> Tuple[
    Tuple[ProductionOutboxAllocationEvidence, ...],
    ProductionOutboxAllocationHead,
]:
    allocations = []
    previous = before.tail_sha256
    base = atomic.atomic_plan
    for offset, (wrapped, event) in enumerate(
        zip(outbox_targets, base.outbox),
        start=1,
    ):
        sequence = before.sequence + offset
        tail = _production_outbox_allocation_tail(
            sequence=sequence,
            event_id=event.event_id,
            commit_id=base.commit_id,
            repository_revision=base.candidate.authority_revision,
            ordinal=event.ordinal,
            previous_tail_sha256=previous,
        )
        allocations.append(ProductionOutboxAllocationEvidence(
            sequence=sequence,
            event_id=event.event_id,
            commit_id=base.commit_id,
            repository_revision=base.candidate.authority_revision,
            ordinal=event.ordinal,
            previous_tail_sha256=previous,
            allocation_tail_sha256=tail,
            allocated_at=atomic.evidence.committed_at,
        ))
        previous = tail
    if not allocations:
        return (), before
    return tuple(allocations), ProductionOutboxAllocationHead(
        state_revision=before.state_revision + 1,
        sequence=before.sequence + len(allocations),
        previous_tail_sha256=before.tail_sha256,
        tail_sha256=previous,
        updated_at=atomic.evidence.committed_at,
    )


def _validate_mount(
    atomic: AtomicBusinessWritePlan,
    mounted: ProductionMountedAuthorityEvidence,
) -> None:
    authority = atomic.atomic_plan.authority
    if (
        mounted.authority != authority.authority
        or mounted.authority_state != authority.authority_state
        or mounted.repository_epoch != authority.authority_epoch
        or mounted.cutover_id != authority.cutover_id
        or mounted.source_admission_revision != authority.source_admission_revision
        or mounted.repository_revision != authority.authority_revision
        or mounted.control_revision != authority.control_revision
        or mounted.runtime_generation != authority.runtime_generation
        or not hmac.compare_digest(mounted.code_digest, authority.code_digest)
    ):
        _fail("production_atomic_mount_authority_mismatch")


def _validate_pipeline_targets(
    atomic: AtomicBusinessWritePlan,
    values: Tuple[ProductionWorkerTarget, ...],
) -> None:
    creates = tuple(
        (intent, control)
        for intent, control in zip(
            atomic.atomic_plan.pipeline_intents,
            atomic.control_plans,
        )
        if intent.intent_kind is PipelineIntentKind.CREATE
    )
    if len(values) != len(creates):
        _fail("production_pipeline_target_count_invalid")
    for wrapped, (intent, control) in zip(values, creates):
        if (
            control.operation is not PipelineBusinessControlOperation.CREATE
            or wrapped.source_ordinal != intent.ordinal
            or wrapped.target != control.target
            or wrapped.genesis_status is not WorkerExecutionStatus.QUEUED
            or not hmac.compare_digest(
                wrapped.genesis_state_sha256,
                control.closure.after_state_sha256,
            )
        ):
            _fail("production_pipeline_target_mapping_invalid")


def _validate_action_targets(
    atomic: AtomicBusinessWritePlan,
    values: Tuple[ProductionWorkerTarget, ...],
) -> None:
    base = atomic.atomic_plan
    if len(values) != len(base.durable_actions):
        _fail("production_action_target_count_invalid")
    for wrapped, action in zip(values, base.durable_actions):
        target = wrapped.target
        expected_genesis = _production_worker_genesis_state_sha256(
            target,
            status=WorkerExecutionStatus.QUEUED,
        )
        if (
            wrapped.source_ordinal != action.ordinal
            or wrapped.action_type != action.action_type
            or target.target_kind is not WorkerTargetKind.ACTION
            or target.target_id != action.action_id
            or target.project_id != base.before.project_id
            or target.generation != base.before.generation
            or target.document_id != base.before.document_id
            or target.origin_commit_id != base.commit_id
            or target.payload != action.payload
            or target.replay_policy.value != action.replay_policy.value
            or target.idempotency_policy.value != action.idempotency_policy.value
            or target.idempotency_scope != action.idempotency_scope
            or target.idempotency_key != action.idempotency_key
            or target.max_attempts != action.max_attempts
            or not hmac.compare_digest(
                wrapped.genesis_state_sha256,
                expected_genesis,
            )
        ):
            _fail("production_action_target_mapping_invalid")


def _validate_outbox_targets(
    atomic: AtomicBusinessWritePlan,
    values: Tuple[ProductionWorkerTarget, ...],
    before: ProductionOutboxAllocationHead,
) -> None:
    base = atomic.atomic_plan
    if len(values) != len(base.outbox):
        _fail("production_outbox_target_count_invalid")
    for offset, (wrapped, event) in enumerate(zip(values, base.outbox), start=1):
        target = wrapped.target
        expected_genesis = _production_worker_genesis_state_sha256(
            target,
            status=WorkerExecutionStatus.PENDING,
        )
        if (
            wrapped.source_ordinal != event.ordinal
            or wrapped.outbox_topic != event.topic
            or wrapped.outbox_partition_key != event.partition_key
            or target.target_kind is not WorkerTargetKind.OUTBOX
            or target.target_id != event.event_id
            or target.project_id != base.before.project_id
            or target.generation != base.before.generation
            or target.document_id != base.before.document_id
            or target.origin_commit_id != base.commit_id
            or target.payload != event.payload
            or target.replay_policy is not PRODUCTION_OUTBOX_REPLAY_POLICY
            or target.idempotency_policy
            is not PRODUCTION_OUTBOX_IDEMPOTENCY_POLICY
            or target.idempotency_scope
            != "outbox/%s/%d" % (
                base.before.project_id,
                base.before.generation,
            )
            or target.idempotency_key != event.event_id
            or target.max_attempts != PRODUCTION_OUTBOX_MAX_ATTEMPTS
            or target.outbox_order is None
            or target.outbox_order.sequence != before.sequence + offset
            or target.outbox_order.repository_revision
            != base.candidate.authority_revision
            or target.outbox_order.ordinal != event.ordinal
            or not hmac.compare_digest(
                wrapped.genesis_state_sha256,
                expected_genesis,
            )
        ):
            _fail("production_outbox_target_mapping_invalid")


class _PreparedProductionAtomicWrite(NamedTuple):
    atomic_write: AtomicBusinessWritePlan
    mounted_authority: ProductionMountedAuthorityBinding
    worker_ledger_head: ProductionWorkerLedgerHeadBinding
    outbox_dispatch_head: ProductionOutboxDispatchHeadBinding
    targets: Tuple[ProductionWorkerTarget, ...]
    before_outbox_allocation: ProductionOutboxAllocationHead
    evidence: ProductionAtomicWriteEvidence


def _prepare(
    atomic_write: Any,
    mounted_authority: Any,
    worker_ledger_head: Any,
    outbox_dispatch_head: Any,
    targets: Any,
    before_outbox_allocation: Any,
) -> _PreparedProductionAtomicWrite:
    try:
        atomic = _require_atomic_business_write_plan(atomic_write)
    except (AtomicBusinessWriteIntegrityError, TypeError, ValueError):
        _fail("production_atomic_inner_plan_invalid")
    try:
        mounted_binding = _require_mounted_binding(mounted_authority)
    except (SQLiteProductionAuthorityError, TypeError, ValueError):
        _fail("production_atomic_mount_binding_invalid")
    mounted = mounted_binding.evidence
    _validate_mount(atomic, mounted)
    try:
        worker_binding = _require_worker_ledger_head_binding(
            worker_ledger_head
        )
        dispatch_binding = _require_outbox_dispatch_head_binding(
            outbox_dispatch_head
        )
    except (SQLiteProductionAuthorityError, TypeError, ValueError):
        _fail("production_scheduler_head_binding_invalid")
    worker_head = worker_binding.evidence
    dispatch_head = dispatch_binding.evidence
    try:
        exact_worker_head = ProductionWorkerLedgerHeadEvidence(
            state_revision=worker_head.state_revision,
            ledger_head=WorkerLedgerHead(
                worker_head.ledger_head.sequence,
                worker_head.ledger_head.tail_sha256,
            ),
            previous_tail_sha256=worker_head.previous_tail_sha256,
            updated_at=worker_head.updated_at,
        )
        dispatch_state = dispatch_head.dispatch_state
        exact_dispatch_head = ProductionOutboxDispatchHeadEvidence(
            dispatch_state=WorkerOutboxDispatchState(
                dispatch_state.control_revision,
                WorkerOutboxDispatchCursor(
                    dispatch_state.published.sequence,
                    dispatch_state.published.repository_revision,
                    dispatch_state.published.ordinal,
                ),
                (
                    None
                    if dispatch_state.inflight is None
                    else WorkerOutboxOrder(
                        dispatch_state.inflight.sequence,
                        dispatch_state.inflight.repository_revision,
                        dispatch_state.inflight.ordinal,
                    )
                ),
            ),
            previous_head_sha256=dispatch_head.previous_head_sha256,
            head_sha256=dispatch_head.head_sha256,
            updated_at=dispatch_head.updated_at,
        )
    except (
        AttributeError,
        ProductionAtomicWriteContractError,
        TypeError,
        ValueError,
    ):
        _fail("production_scheduler_head_invalid")
    if (
        exact_worker_head != worker_head
        or exact_dispatch_head != dispatch_head
        or worker_head.ledger_head != atomic.evidence.before_worker_ledger
    ):
        _fail("production_scheduler_head_changed")
    if type(targets) is not tuple or len(targets) > MAX_PRODUCTION_ATOMIC_TARGETS:
        _fail("production_atomic_targets_invalid")
    exact_targets = tuple(_canonical_production_target(value) for value in targets)
    if exact_targets != targets:
        _fail("production_atomic_targets_changed")
    try:
        exact_before = ProductionOutboxAllocationHead(
            state_revision=before_outbox_allocation.state_revision,
            sequence=before_outbox_allocation.sequence,
            previous_tail_sha256=before_outbox_allocation.previous_tail_sha256,
            tail_sha256=before_outbox_allocation.tail_sha256,
            updated_at=before_outbox_allocation.updated_at,
        )
    except (
        AttributeError,
        ProductionAtomicWriteContractError,
        TypeError,
    ):
        _fail("production_outbox_allocation_head_invalid")
    if (
        type(before_outbox_allocation) is not ProductionOutboxAllocationHead
        or exact_before != before_outbox_allocation
    ):
        _fail("production_outbox_allocation_head_changed")
    if max(
        mounted.mounted_at,
        worker_head.updated_at,
        dispatch_head.updated_at,
        exact_before.updated_at,
    ) > atomic.evidence.committed_at:
        _fail("production_committed_time_stale")

    groups = _groups(exact_targets)
    pipeline, actions, outbox = tuple(value.members for value in groups)
    _validate_pipeline_targets(atomic, pipeline)
    _validate_action_targets(atomic, actions)
    _validate_outbox_targets(atomic, outbox, exact_before)
    target_ids = tuple(value.target.target_id for value in exact_targets)
    if len(target_ids) != len(set(target_ids)):
        _fail("production_atomic_target_identity_overlap")

    targets_sha256 = _production_targets_sha256(groups)
    allocations, after = _advance_allocations(atomic, outbox, exact_before)
    manifest_bytes = _build_production_manifest(
        atomic=atomic,
        mounted=mounted,
        target_count=len(exact_targets),
        targets_sha256=targets_sha256,
        before_worker_ledger=worker_head,
        before_outbox_dispatch=dispatch_head,
        before_allocation=exact_before,
        allocation_count=len(allocations),
        after_allocation=after,
    )
    manifest_sha256 = _production_manifest_sha256(manifest_bytes)
    production_receipt_sha256 = _production_receipt_sha256(
        atomic=atomic,
        production_manifest_sha256=manifest_sha256,
        target_count=len(exact_targets),
        targets_sha256=targets_sha256,
    )
    expected_receipt = ProductionAtomicWriteReceiptEvidence(
        commit_id=atomic.atomic_plan.commit_id,
        receipt_parent=atomic.expected_receipt.receipt_parent,
        pure_receipt_sha256=atomic.expected_receipt.receipt_sha256,
        production_manifest_sha256=manifest_sha256,
        target_count=len(exact_targets),
        targets_sha256=targets_sha256,
        production_receipt_sha256=production_receipt_sha256,
    )
    evidence = ProductionAtomicWriteEvidence(
        commit_id=atomic.atomic_plan.commit_id,
        committed_at=atomic.evidence.committed_at,
        pure_receipt_sha256=atomic.expected_receipt.receipt_sha256,
        mounted_authority=mounted,
        target_groups=groups,
        target_count=len(exact_targets),
        targets_sha256=targets_sha256,
        before_worker_ledger_head=worker_head,
        before_outbox_dispatch_head=dispatch_head,
        before_outbox_allocation=exact_before,
        allocations=allocations,
        after_outbox_allocation=after,
        production_manifest_bytes=manifest_bytes,
        production_manifest_sha256=manifest_sha256,
        expected_receipt=expected_receipt,
    )
    return _PreparedProductionAtomicWrite(
        atomic,
        mounted_binding,
        worker_binding,
        dispatch_binding,
        exact_targets,
        exact_before,
        evidence,
    )


class ProductionAtomicWritePlan:
    """Issuer-stamped SQL-ready closure above the frozen pure atomic plan."""

    __slots__ = (
        "atomic_write",
        "mounted_authority",
        "worker_ledger_head",
        "outbox_dispatch_head",
        "targets",
        "evidence",
        "expected_receipt",
        "protocol",
        "status",
        "_before_outbox_allocation",
        "_issuer",
    )

    def __init__(
        self,
        prepared: _PreparedProductionAtomicWrite,
        *,
        _issuer: Any,
    ) -> None:
        if _issuer is not _PLAN_ISSUER:
            _fail("production_atomic_plan_issuer_invalid")
        object.__setattr__(self, "atomic_write", prepared.atomic_write)
        object.__setattr__(self, "mounted_authority", prepared.mounted_authority)
        object.__setattr__(self, "worker_ledger_head", prepared.worker_ledger_head)
        object.__setattr__(
            self,
            "outbox_dispatch_head",
            prepared.outbox_dispatch_head,
        )
        object.__setattr__(self, "targets", prepared.targets)
        object.__setattr__(self, "evidence", prepared.evidence)
        object.__setattr__(self, "expected_receipt", prepared.evidence.expected_receipt)
        object.__setattr__(self, "protocol", PRODUCTION_ATOMIC_WRITE_PROTOCOL)
        object.__setattr__(self, "status", PRODUCTION_ATOMIC_WRITE_STATUS)
        object.__setattr__(
            self,
            "_before_outbox_allocation",
            prepared.before_outbox_allocation,
        )
        object.__setattr__(self, "_issuer", _issuer)

    def __setattr__(self, name: str, value: Any) -> None:
        del name, value
        raise AttributeError("ProductionAtomicWritePlan is immutable")


def _issue_production_atomic_write_plan(
    *,
    atomic_write: AtomicBusinessWritePlan,
    mounted_authority: ProductionMountedAuthorityBinding,
    worker_ledger_head: ProductionWorkerLedgerHeadBinding,
    outbox_dispatch_head: ProductionOutboxDispatchHeadBinding,
    targets: Tuple[ProductionWorkerTarget, ...],
    before_outbox_allocation: ProductionOutboxAllocationHead,
) -> ProductionAtomicWritePlan:
    return ProductionAtomicWritePlan(
        _prepare(
            atomic_write,
            mounted_authority,
            worker_ledger_head,
            outbox_dispatch_head,
            targets,
            before_outbox_allocation,
        ),
        _issuer=_PLAN_ISSUER,
    )


def _require_production_atomic_write_plan(value: Any) -> ProductionAtomicWritePlan:
    if (
        type(value) is not ProductionAtomicWritePlan
        or getattr(value, "_issuer", None) is not _PLAN_ISSUER
        or getattr(value, "protocol", None) != PRODUCTION_ATOMIC_WRITE_PROTOCOL
        or getattr(value, "status", None) != PRODUCTION_ATOMIC_WRITE_STATUS
    ):
        _fail("production_atomic_plan_invalid")
    prepared = _prepare(
        value.atomic_write,
        value.mounted_authority,
        value.worker_ledger_head,
        value.outbox_dispatch_head,
        value.targets,
        value._before_outbox_allocation,
    )
    if (
        prepared.atomic_write != value.atomic_write
        or prepared.mounted_authority is not value.mounted_authority
        or prepared.worker_ledger_head is not value.worker_ledger_head
        or prepared.outbox_dispatch_head is not value.outbox_dispatch_head
        or prepared.targets != value.targets
        or prepared.evidence != value.evidence
        or prepared.evidence.expected_receipt != value.expected_receipt
    ):
        _fail("production_atomic_plan_changed")
    return value


__all__ = ()
