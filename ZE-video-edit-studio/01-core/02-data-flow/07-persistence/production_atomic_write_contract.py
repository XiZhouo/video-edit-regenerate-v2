"""Pure outer closure for a schema-v8 production atomic write.

The frozen ``atomic-business-write.v1`` receipt proves the Document commit and
pipeline-control ledger.  It intentionally says nothing about production
worker routing, action/outbox targets, genesis scheduler state, or the global
outbox allocation order.  This module defines that missing outer evidence
without changing the meaning or fixed vectors of the frozen inner protocol.

These values perform no I/O and grant no SQLite authority.  The integrity
issuer builds them from an already-issued inner atomic plan plus a freshly
read mounted-authority binding.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional, Tuple

from business_atomic_write_contract import (
    ATOMIC_BUSINESS_WRITE_TRANSACTION_KIND,
    ATOMIC_BUSINESS_WRITE_TRANSACTION_PREFIX,
    AtomicBusinessWriteGenericReceiptEvidence,
)
from worker_execution_contract import (
    WorkerIdempotencyPolicy,
    WorkerReplayPolicy,
    WorkerExecutionStatus,
    WorkerLedgerHead,
    WorkerOutboxDispatchState,
    WorkerTarget,
    WorkerTargetKind,
)


PRODUCTION_ATOMIC_WRITE_PROTOCOL = "ze.production-atomic-write.v1"
PRODUCTION_ATOMIC_WRITE_MANIFEST_SCHEMA = (
    "ze.production-atomic-write.manifest.v1"
)
PRODUCTION_ATOMIC_WRITE_STATUS = "prepared"
PRODUCTION_ATOMIC_TARGET_GROUP_ORDER = (
    WorkerTargetKind.PIPELINE,
    WorkerTargetKind.ACTION,
    WorkerTargetKind.OUTBOX,
)
PRODUCTION_OUTBOX_REPLAY_POLICY = WorkerReplayPolicy.RECONCILE_BEFORE_RETRY
PRODUCTION_OUTBOX_IDEMPOTENCY_POLICY = (
    WorkerIdempotencyPolicy.DURABLE_RECEIPT_RECONCILE
)
PRODUCTION_OUTBOX_MAX_ATTEMPTS = 10

MAX_PRODUCTION_ATOMIC_TARGETS_PER_GROUP = 10_000
MAX_PRODUCTION_ATOMIC_TARGETS = 30_000
MAX_PRODUCTION_ATOMIC_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_PRODUCTION_ATOMIC_MANIFEST_NODES = 500_000
MAX_PRODUCTION_ATOMIC_TEXT_BYTES = 1024 * 1024
MAX_SQLITE_INTEGER = 2**63 - 1

EMPTY_PRODUCTION_DIGEST = b"\0" * 32
# Canonical hash of pipeline/action/outbox as three present-but-empty groups.
# Baseline sidecars reuse this published fixed vector; they must not hash only
# an aggregate target_count of zero.
EMPTY_PRODUCTION_TARGETS_SHA256 = bytes.fromhex(
    "edc9bee4937fa3cc223a52ca677f0e6825dc6ef503000bfabd4fc24941ca153f"
)
_LOWER_HEX = frozenset("0123456789abcdef")


class ProductionAtomicWriteContractError(ValueError):
    """Stable, payload-free failure at the production outer boundary."""

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
    raise ProductionAtomicWriteContractError(code, path)


def _text(value: Any, path: str, *, maximum: int = MAX_PRODUCTION_ATOMIC_TEXT_BYTES) -> str:
    if type(value) is not str or not value:
        _fail("production_atomic_text_invalid", path)
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeEncodeError:
        _fail("production_atomic_text_invalid", path)
        raise AssertionError("unreachable")
    if len(encoded) > maximum:
        _fail("production_atomic_text_budget_exceeded", path)
    return value


def _lower_hex(value: Any, path: str, length: int) -> str:
    if (
        type(value) is not str
        or len(value) != length
        or any(character not in _LOWER_HEX for character in value)
    ):
        _fail("production_atomic_hex_invalid", path)
    return value


def _digest(value: Any, path: str, *, nonzero: bool = False) -> bytes:
    if (
        type(value) is not bytes
        or len(value) != 32
        or (nonzero and hmac.compare_digest(value, EMPTY_PRODUCTION_DIGEST))
    ):
        _fail("production_atomic_digest_invalid", path)
    return value


def _integer(
    value: Any,
    path: str,
    *,
    minimum: int = 0,
    maximum: int = MAX_SQLITE_INTEGER,
) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _fail("production_atomic_integer_invalid", path)
    return value


def _timestamp(value: Any, path: str) -> str:
    if type(value) is not str or len(value) != 24:
        _fail("production_atomic_timestamp_invalid", path)
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except (TypeError, ValueError):
        _fail("production_atomic_timestamp_invalid", path)
        raise AssertionError("unreachable")
    canonical = parsed.strftime("%Y-%m-%dT%H:%M:%S.") + (
        "%03dZ" % (parsed.microsecond // 1000)
    )
    if canonical != value:
        _fail("production_atomic_timestamp_invalid", path)
    return value


@dataclass(frozen=True)
class ProductionMountedAuthorityEvidence:
    """Pure snapshot sealed by the private mounted-authority reader."""

    authority: str
    authority_state: str
    repository_epoch: str
    cutover_id: str
    source_admission_revision: int
    repository_revision: int
    control_revision: int
    runtime_generation: str
    code_digest: bytes = field(repr=False)
    target_schema_version: int
    target_schema_fingerprint: str
    mount_transaction_id: str
    mount_manifest_sha256: bytes = field(repr=False)
    mounted_at: str

    def __post_init__(self) -> None:
        _text(self.authority, "$.mount.authority")
        _text(self.authority_state, "$.mount.authority_state")
        if self.authority != "sqlite" or self.authority_state != "active":
            _fail("production_mount_authority_invalid", "$.mount.authority")
        _lower_hex(self.repository_epoch, "$.mount.repository_epoch", 64)
        _lower_hex(self.cutover_id, "$.mount.cutover_id", 64)
        _integer(
            self.source_admission_revision,
            "$.mount.source_admission_revision",
            minimum=1,
        )
        _integer(
            self.repository_revision,
            "$.mount.repository_revision",
            minimum=1,
        )
        _integer(self.control_revision, "$.mount.control_revision", minimum=1)
        _text(self.runtime_generation, "$.mount.runtime_generation")
        _digest(self.code_digest, "$.mount.code_digest")
        _integer(
            self.target_schema_version,
            "$.mount.target_schema_version",
            minimum=1,
        )
        _lower_hex(
            self.target_schema_fingerprint,
            "$.mount.target_schema_fingerprint",
            64,
        )
        _text(self.mount_transaction_id, "$.mount.transaction_id")
        if self.mount_transaction_id != "_business-cutover-mount:" + self.cutover_id:
            _fail("production_mount_identity_invalid", "$.mount.transaction_id")
        _digest(self.mount_manifest_sha256, "$.mount.manifest_sha256")
        _timestamp(self.mounted_at, "$.mount.mounted_at")


@dataclass(frozen=True)
class ProductionOutboxAllocationHead:
    """Exact CAS image of ``outbox_allocation_head``."""

    state_revision: int
    sequence: int
    previous_tail_sha256: bytes = field(repr=False)
    tail_sha256: bytes = field(repr=False)
    updated_at: str

    def __post_init__(self) -> None:
        _integer(self.state_revision, "$.allocation_head.state_revision", minimum=1)
        _integer(self.sequence, "$.allocation_head.sequence")
        _digest(
            self.previous_tail_sha256,
            "$.allocation_head.previous_tail_sha256",
        )
        _digest(self.tail_sha256, "$.allocation_head.tail_sha256")
        _timestamp(self.updated_at, "$.allocation_head.updated_at")
        if self.sequence == 0:
            if not (
                hmac.compare_digest(
                    self.previous_tail_sha256,
                    EMPTY_PRODUCTION_DIGEST,
                )
                and hmac.compare_digest(
                    self.tail_sha256,
                    EMPTY_PRODUCTION_DIGEST,
                )
            ):
                _fail(
                    "production_outbox_allocation_head_invalid",
                    "$.allocation_head",
                )
        elif hmac.compare_digest(self.tail_sha256, EMPTY_PRODUCTION_DIGEST):
            _fail(
                "production_outbox_allocation_head_invalid",
                "$.allocation_head.tail_sha256",
            )


@dataclass(frozen=True)
class ProductionWorkerLedgerHeadEvidence:
    """Exact same-snapshot image of ``worker_ledger_head``."""

    state_revision: int
    ledger_head: WorkerLedgerHead
    previous_tail_sha256: bytes = field(repr=False)
    updated_at: str

    def __post_init__(self) -> None:
        _integer(
            self.state_revision,
            "$.worker_ledger_head.state_revision",
            minimum=1,
        )
        if type(self.ledger_head) is not WorkerLedgerHead:
            _fail(
                "production_worker_ledger_head_invalid",
                "$.worker_ledger_head.ledger_head",
            )
        _digest(
            self.previous_tail_sha256,
            "$.worker_ledger_head.previous_tail_sha256",
        )
        _timestamp(self.updated_at, "$.worker_ledger_head.updated_at")
        if self.ledger_head.sequence == 0 and not hmac.compare_digest(
            self.previous_tail_sha256,
            EMPTY_PRODUCTION_DIGEST,
        ):
            _fail(
                "production_worker_ledger_head_invalid",
                "$.worker_ledger_head.previous_tail_sha256",
            )


@dataclass(frozen=True)
class ProductionOutboxDispatchHeadEvidence:
    """Exact same-snapshot image of ``outbox_dispatch_head``."""

    dispatch_state: WorkerOutboxDispatchState
    previous_head_sha256: bytes = field(repr=False)
    head_sha256: bytes = field(repr=False)
    updated_at: str

    def __post_init__(self) -> None:
        if type(self.dispatch_state) is not WorkerOutboxDispatchState:
            _fail(
                "production_outbox_dispatch_head_invalid",
                "$.outbox_dispatch_head.dispatch_state",
            )
        _digest(
            self.previous_head_sha256,
            "$.outbox_dispatch_head.previous_head_sha256",
        )
        _digest(
            self.head_sha256,
            "$.outbox_dispatch_head.head_sha256",
        )
        _timestamp(self.updated_at, "$.outbox_dispatch_head.updated_at")
        if self.dispatch_state.control_revision == 1:
            if not (
                self.dispatch_state.published.sequence == 0
                and self.dispatch_state.inflight is None
                and hmac.compare_digest(
                    self.previous_head_sha256,
                    EMPTY_PRODUCTION_DIGEST,
                )
                and hmac.compare_digest(
                    self.head_sha256,
                    EMPTY_PRODUCTION_DIGEST,
                )
            ):
                _fail(
                    "production_outbox_dispatch_head_invalid",
                    "$.outbox_dispatch_head",
                )
        elif hmac.compare_digest(self.head_sha256, EMPTY_PRODUCTION_DIGEST):
            _fail(
                "production_outbox_dispatch_head_invalid",
                "$.outbox_dispatch_head.head_sha256",
            )


EMPTY_PRODUCTION_OUTBOX_ALLOCATION_HEAD = ProductionOutboxAllocationHead(
    state_revision=1,
    sequence=0,
    previous_tail_sha256=EMPTY_PRODUCTION_DIGEST,
    tail_sha256=EMPTY_PRODUCTION_DIGEST,
    updated_at="1970-01-01T00:00:00.000Z",
)


@dataclass(frozen=True)
class ProductionOutboxAllocationEvidence:
    sequence: int
    event_id: str
    commit_id: str
    repository_revision: int
    ordinal: int
    previous_tail_sha256: bytes = field(repr=False)
    allocation_tail_sha256: bytes = field(repr=False)
    allocated_at: str

    def __post_init__(self) -> None:
        _integer(self.sequence, "$.allocation.sequence", minimum=1)
        _lower_hex(self.event_id, "$.allocation.event_id", 32)
        _lower_hex(self.commit_id, "$.allocation.commit_id", 32)
        _integer(
            self.repository_revision,
            "$.allocation.repository_revision",
            minimum=1,
        )
        _integer(
            self.ordinal,
            "$.allocation.ordinal",
            maximum=MAX_PRODUCTION_ATOMIC_TARGETS_PER_GROUP - 1,
        )
        _digest(
            self.previous_tail_sha256,
            "$.allocation.previous_tail_sha256",
        )
        _digest(
            self.allocation_tail_sha256,
            "$.allocation.allocation_tail_sha256",
            nonzero=True,
        )
        _timestamp(self.allocated_at, "$.allocation.allocated_at")


@dataclass(frozen=True)
class ProductionWorkerTarget:
    """Frozen worker-v3 target plus production-only routing and genesis."""

    source_ordinal: int
    target: WorkerTarget
    genesis_status: WorkerExecutionStatus
    genesis_state_sha256: bytes = field(repr=False)
    action_type: Optional[str] = None
    outbox_topic: Optional[str] = None
    outbox_partition_key: Optional[str] = None

    def __post_init__(self) -> None:
        _integer(
            self.source_ordinal,
            "$.target.source_ordinal",
            maximum=MAX_PRODUCTION_ATOMIC_TARGETS_PER_GROUP - 1,
        )
        if type(self.target) is not WorkerTarget:
            _fail("production_worker_target_invalid", "$.target.base")
        if type(self.genesis_status) is not WorkerExecutionStatus:
            _fail("production_worker_genesis_invalid", "$.target.genesis_status")
        _digest(
            self.genesis_state_sha256,
            "$.target.genesis_state_sha256",
            nonzero=True,
        )
        kind = self.target.target_kind
        if kind is WorkerTargetKind.PIPELINE:
            valid = (
                self.genesis_status is WorkerExecutionStatus.QUEUED
                and self.action_type is None
                and self.outbox_topic is None
                and self.outbox_partition_key is None
            )
        elif kind is WorkerTargetKind.ACTION:
            valid = (
                self.genesis_status is WorkerExecutionStatus.QUEUED
                and self.action_type is not None
                and self.outbox_topic is None
                and self.outbox_partition_key is None
            )
        elif kind is WorkerTargetKind.OUTBOX:
            valid = (
                self.genesis_status is WorkerExecutionStatus.PENDING
                and self.action_type is None
                and self.outbox_topic is not None
                and self.outbox_partition_key is not None
            )
        else:
            valid = False
        if not valid:
            _fail("production_worker_target_routing_invalid", "$.target")
        if self.action_type is not None:
            _text(self.action_type, "$.target.action_type")
        if self.outbox_topic is not None:
            _text(self.outbox_topic, "$.target.outbox_topic")
        if self.outbox_partition_key is not None:
            _text(
                self.outbox_partition_key,
                "$.target.outbox_partition_key",
            )


@dataclass(frozen=True)
class ProductionAtomicTargetGroupEvidence:
    target_kind: WorkerTargetKind
    members: Tuple[ProductionWorkerTarget, ...]
    members_sha256: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.target_kind) is not WorkerTargetKind:
            _fail("production_target_group_invalid", "$.target_group.kind")
        if (
            type(self.members) is not tuple
            or len(self.members) > MAX_PRODUCTION_ATOMIC_TARGETS_PER_GROUP
            or any(type(value) is not ProductionWorkerTarget for value in self.members)
            or any(value.target.target_kind is not self.target_kind for value in self.members)
            or any(
                left.source_ordinal >= right.source_ordinal
                for left, right in zip(self.members, self.members[1:])
            )
        ):
            _fail("production_target_group_invalid", "$.target_group.members")
        _digest(self.members_sha256, "$.target_group.members_sha256")


@dataclass(frozen=True)
class ProductionAtomicWriteReceiptEvidence:
    """Outer digest stored beside, but never replacing, the pure receipt."""

    commit_id: str
    receipt_parent: AtomicBusinessWriteGenericReceiptEvidence
    pure_receipt_sha256: bytes = field(repr=False)
    production_manifest_sha256: bytes = field(repr=False)
    target_count: int
    targets_sha256: bytes = field(repr=False)
    production_receipt_sha256: bytes = field(repr=False)

    def __post_init__(self) -> None:
        _lower_hex(self.commit_id, "$.receipt.commit_id", 32)
        if type(self.receipt_parent) is not AtomicBusinessWriteGenericReceiptEvidence:
            _fail("production_receipt_invalid", "$.receipt.parent")
        if (
            self.receipt_parent.transaction_id
            != ATOMIC_BUSINESS_WRITE_TRANSACTION_PREFIX + self.commit_id
            or self.receipt_parent.kind != ATOMIC_BUSINESS_WRITE_TRANSACTION_KIND
        ):
            _fail("production_receipt_invalid", "$.receipt.parent")
        _digest(self.pure_receipt_sha256, "$.receipt.pure_receipt_sha256")
        _digest(
            self.production_manifest_sha256,
            "$.receipt.production_manifest_sha256",
        )
        _integer(
            self.target_count,
            "$.receipt.target_count",
            maximum=MAX_PRODUCTION_ATOMIC_TARGETS,
        )
        _digest(self.targets_sha256, "$.receipt.targets_sha256")
        _digest(
            self.production_receipt_sha256,
            "$.receipt.production_receipt_sha256",
        )


@dataclass(frozen=True)
class ProductionAtomicWriteEvidence:
    commit_id: str
    committed_at: str
    pure_receipt_sha256: bytes = field(repr=False)
    mounted_authority: ProductionMountedAuthorityEvidence
    target_groups: Tuple[ProductionAtomicTargetGroupEvidence, ...]
    target_count: int
    targets_sha256: bytes = field(repr=False)
    before_worker_ledger_head: ProductionWorkerLedgerHeadEvidence
    before_outbox_dispatch_head: ProductionOutboxDispatchHeadEvidence
    before_outbox_allocation: ProductionOutboxAllocationHead
    allocations: Tuple[ProductionOutboxAllocationEvidence, ...]
    after_outbox_allocation: ProductionOutboxAllocationHead
    production_manifest_bytes: bytes = field(repr=False)
    production_manifest_sha256: bytes = field(repr=False)
    expected_receipt: ProductionAtomicWriteReceiptEvidence
    protocol: str = PRODUCTION_ATOMIC_WRITE_PROTOCOL
    status: str = PRODUCTION_ATOMIC_WRITE_STATUS

    def __post_init__(self) -> None:
        _lower_hex(self.commit_id, "$.commit_id", 32)
        _timestamp(self.committed_at, "$.committed_at")
        _digest(self.pure_receipt_sha256, "$.pure_receipt_sha256")
        if type(self.mounted_authority) is not ProductionMountedAuthorityEvidence:
            _fail("production_mount_evidence_invalid", "$.mounted_authority")
        if (
            type(self.target_groups) is not tuple
            or tuple(value.target_kind for value in self.target_groups)
            != PRODUCTION_ATOMIC_TARGET_GROUP_ORDER
            or any(
                type(value) is not ProductionAtomicTargetGroupEvidence
                for value in self.target_groups
            )
        ):
            _fail("production_target_groups_invalid", "$.target_groups")
        _integer(
            self.target_count,
            "$.target_count",
            maximum=MAX_PRODUCTION_ATOMIC_TARGETS,
        )
        if self.target_count != sum(len(value.members) for value in self.target_groups):
            _fail("production_target_count_invalid", "$.target_count")
        targets = tuple(
            member
            for group in self.target_groups
            for member in group.members
        )
        target_ids = tuple(value.target.target_id for value in targets)
        if (
            len(target_ids) != len(set(target_ids))
            or any(
                value.target.origin_commit_id != self.commit_id
                for value in targets
            )
        ):
            _fail("production_targets_identity_invalid", "$.target_groups")
        _digest(self.targets_sha256, "$.targets_sha256")
        if type(
            self.before_worker_ledger_head
        ) is not ProductionWorkerLedgerHeadEvidence:
            _fail(
                "production_worker_ledger_head_invalid",
                "$.worker_ledger_head",
            )
        if type(
            self.before_outbox_dispatch_head
        ) is not ProductionOutboxDispatchHeadEvidence:
            _fail(
                "production_outbox_dispatch_head_invalid",
                "$.outbox_dispatch_head",
            )
        if type(self.before_outbox_allocation) is not ProductionOutboxAllocationHead:
            _fail("production_allocation_head_invalid", "$.allocation.before")
        if max(
            self.mounted_authority.mounted_at,
            self.before_worker_ledger_head.updated_at,
            self.before_outbox_dispatch_head.updated_at,
            self.before_outbox_allocation.updated_at,
        ) > self.committed_at:
            _fail("production_committed_time_stale", "$.committed_at")
        if (
            type(self.allocations) is not tuple
            or len(self.allocations) > MAX_PRODUCTION_ATOMIC_TARGETS_PER_GROUP
            or any(
                type(value) is not ProductionOutboxAllocationEvidence
                for value in self.allocations
            )
        ):
            _fail("production_allocations_invalid", "$.allocations")
        if type(self.after_outbox_allocation) is not ProductionOutboxAllocationHead:
            _fail("production_allocation_head_invalid", "$.allocation.after")
        outbox_targets = self.target_groups[2].members
        if len(outbox_targets) != len(self.allocations):
            _fail("production_allocations_invalid", "$.allocations")
        previous_tail = self.before_outbox_allocation.tail_sha256
        for offset, (allocation, wrapped) in enumerate(
            zip(self.allocations, outbox_targets),
            start=1,
        ):
            order = wrapped.target.outbox_order
            if (
                allocation.sequence
                != self.before_outbox_allocation.sequence + offset
                or allocation.event_id != wrapped.target.target_id
                or allocation.commit_id != self.commit_id
                or allocation.ordinal != wrapped.source_ordinal
                or allocation.allocated_at != self.committed_at
                or order is None
                or order.sequence != allocation.sequence
                or order.repository_revision != allocation.repository_revision
                or order.ordinal != allocation.ordinal
                or not hmac.compare_digest(
                    allocation.previous_tail_sha256,
                    previous_tail,
                )
            ):
                _fail("production_allocations_invalid", "$.allocations")
            previous_tail = allocation.allocation_tail_sha256
        if not self.allocations:
            exact_after = self.before_outbox_allocation
        else:
            exact_after = ProductionOutboxAllocationHead(
                state_revision=self.before_outbox_allocation.state_revision + 1,
                sequence=self.allocations[-1].sequence,
                previous_tail_sha256=self.before_outbox_allocation.tail_sha256,
                tail_sha256=self.allocations[-1].allocation_tail_sha256,
                updated_at=self.committed_at,
            )
        if self.after_outbox_allocation != exact_after:
            _fail("production_allocation_head_invalid", "$.allocation.after")
        if (
            type(self.production_manifest_bytes) is not bytes
            or not self.production_manifest_bytes
            or len(self.production_manifest_bytes) > MAX_PRODUCTION_ATOMIC_MANIFEST_BYTES
        ):
            _fail("production_manifest_invalid", "$.production_manifest_bytes")
        _digest(
            self.production_manifest_sha256,
            "$.production_manifest_sha256",
        )
        if type(self.expected_receipt) is not ProductionAtomicWriteReceiptEvidence:
            _fail("production_receipt_invalid", "$.expected_receipt")
        if (
            self.expected_receipt.commit_id != self.commit_id
            or self.expected_receipt.receipt_parent.committed_at
            != self.committed_at
            or not hmac.compare_digest(
                self.expected_receipt.pure_receipt_sha256,
                self.pure_receipt_sha256,
            )
            or not hmac.compare_digest(
                self.expected_receipt.production_manifest_sha256,
                self.production_manifest_sha256,
            )
            or self.expected_receipt.target_count != self.target_count
            or not hmac.compare_digest(
                self.expected_receipt.targets_sha256,
                self.targets_sha256,
            )
        ):
            _fail("production_receipt_invalid", "$.expected_receipt")
        if self.protocol != PRODUCTION_ATOMIC_WRITE_PROTOCOL:
            _fail("production_protocol_invalid", "$.protocol")
        if self.status != PRODUCTION_ATOMIC_WRITE_STATUS:
            _fail("production_status_invalid", "$.status")


class ProductionWriteStatus(str, Enum):
    COMMITTED = "committed"
    NOT_COMMITTED = "not_committed"
    CONFLICT = "conflict"
    CORRUPT = "corrupt"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ProductionWriteResult:
    status: ProductionWriteStatus
    commit_id: str
    code: str
    repository_revision: Optional[int] = None
    recovered_after_ack_loss: bool = False

    def __post_init__(self) -> None:
        if type(self.status) is not ProductionWriteStatus:
            _fail("production_write_result_invalid", "$.result.status")
        _lower_hex(self.commit_id, "$.result.commit_id", 32)
        _text(self.code, "$.result.code")
        if self.repository_revision is not None:
            _integer(
                self.repository_revision,
                "$.result.repository_revision",
                minimum=1,
            )
        if type(self.recovered_after_ack_loss) is not bool:
            _fail(
                "production_write_result_invalid",
                "$.result.recovered_after_ack_loss",
            )


class ProductionAtomicWriteTestPoint(str, Enum):
    BEFORE_COMMIT = "before_commit"
    AFTER_COMMIT_BEFORE_ACK = "after_commit_before_ack"


__all__ = ()
