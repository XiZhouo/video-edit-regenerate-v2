"""Pure composite evidence for one atomic commit plus pipeline controls.

The frozen atomic-v2 plan and pipeline-control-v2/worker-ledger-v3 plan are
independently valid objects. A SQL writer must not accept them side by side
without proving a one-to-one mapping. This contract is the immutable result
of that mapping; it does not write SQLite or claim that either receipt is
durable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional, Tuple

from worker_execution_contract import (
    PipelineBusinessControlReceiptEvidence,
    WorkerExecutionContractError,
    WorkerLedgerHead,
    WorkerLedgerMembershipEvidence,
)


ATOMIC_BUSINESS_WRITE_PROTOCOL = "ze.atomic-business-write.v1"
ATOMIC_BUSINESS_WRITE_MANIFEST_SCHEMA = "ze.atomic-business-write.manifest.v1"
ATOMIC_BUSINESS_WRITE_PLAN_STATUS = "prepared"
ATOMIC_BUSINESS_WRITE_TRANSACTION_PREFIX = "ze-business:"
ATOMIC_BUSINESS_WRITE_TRANSACTION_KIND = "ze_business_commit_v1"
MAX_ATOMIC_BUSINESS_CONTROL_BINDINGS = 10_000
MAX_ATOMIC_BUSINESS_WRITE_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_ATOMIC_BUSINESS_WRITE_MANIFEST_NODES = 250_000

_LOWER_HEX = frozenset("0123456789abcdef")


class AtomicBusinessWriteContractError(ValueError):
    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        if type(code) is not str or not code:
            raise TypeError("code must be a non-empty exact string")
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise AtomicBusinessWriteContractError(code)


def _hex(value: Any, length: int, code: str) -> str:
    if (
        type(value) is not str
        or len(value) != length
        or any(character not in _LOWER_HEX for character in value)
    ):
        _fail(code)
    return value


def _digest(value: Any, code: str) -> bytes:
    if type(value) is not bytes or len(value) != 32:
        _fail(code)
    return value


def _text(value: Any, code: str, *, maximum: int = 1024 * 1024) -> str:
    if type(value) is not str or not value:
        _fail(code)
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeEncodeError:
        _fail(code)
        raise AssertionError("unreachable")
    if len(encoded) > maximum:
        _fail(code)
    return value


def _timestamp(value: Any, code: str) -> str:
    if type(value) is not str or len(value) != 24:
        _fail(code)
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except (TypeError, ValueError):
        _fail(code)
        raise AssertionError("unreachable")
    canonical = parsed.strftime("%Y-%m-%dT%H:%M:%S.") + (
        "%03dZ" % (parsed.microsecond // 1000)
    )
    if canonical != value:
        _fail(code)
    return value


@dataclass(frozen=True)
class AtomicBusinessControlBinding:
    ordinal: int
    intent_id: str
    job_id: str
    control_transaction_id: str
    request_sha256: bytes = field(repr=False)
    after_state_sha256: bytes = field(repr=False)
    receipt_sha256: bytes = field(repr=False)
    receipt_sequence: int

    def __post_init__(self) -> None:
        if (
            type(self.ordinal) is not int
            or self.ordinal < 0
            or self.ordinal >= MAX_ATOMIC_BUSINESS_CONTROL_BINDINGS
        ):
            _fail("atomic_business_control_ordinal_invalid")
        _hex(self.intent_id, 32, "atomic_business_control_intent_invalid")
        _hex(self.job_id, 32, "atomic_business_control_job_invalid")
        _hex(
            self.control_transaction_id,
            32,
            "atomic_business_control_transaction_invalid",
        )
        _digest(self.request_sha256, "atomic_business_control_digest_invalid")
        _digest(self.after_state_sha256, "atomic_business_control_digest_invalid")
        _digest(self.receipt_sha256, "atomic_business_control_digest_invalid")
        if type(self.receipt_sequence) is not int or self.receipt_sequence < 1:
            _fail("atomic_business_control_receipt_sequence_invalid")


class AtomicBusinessWritePersistedReadStatus(str, Enum):
    COMPLETE = "complete"
    UNKNOWN = "unknown"


class AtomicBusinessWriteReconcileStatus(str, Enum):
    ABSENT = "absent"
    EXACT = "exact"
    CONFLICT = "conflict"
    CORRUPT = "corrupt"
    UNKNOWN = "unknown"


class AtomicBusinessWriteRetryAction(str, Enum):
    APPLY_EXACT = "apply_exact"
    RETURN_EXACT = "return_exact"
    STOP = "stop"
    RECONCILE_REQUIRED = "reconcile_required"


@dataclass(frozen=True)
class AtomicBusinessWriteEvidence:
    commit_id: str
    committed_at: str
    atomic_manifest_sha256: bytes = field(repr=False)
    pipeline_intents_sha256: bytes = field(repr=False)
    controls: Tuple[AtomicBusinessControlBinding, ...]
    controls_sha256: bytes = field(repr=False)
    before_worker_ledger: WorkerLedgerHead
    after_worker_ledger: WorkerLedgerHead
    manifest_bytes: bytes = field(repr=False)
    manifest_sha256: bytes = field(repr=False)
    protocol: str = ATOMIC_BUSINESS_WRITE_PROTOCOL
    status: str = ATOMIC_BUSINESS_WRITE_PLAN_STATUS

    def __post_init__(self) -> None:
        _hex(self.commit_id, 32, "atomic_business_write_commit_invalid")
        _timestamp(self.committed_at, "atomic_business_write_time_invalid")
        _digest(self.atomic_manifest_sha256, "atomic_business_write_digest_invalid")
        _digest(self.pipeline_intents_sha256, "atomic_business_write_digest_invalid")
        if (
            type(self.controls) is not tuple
            or len(self.controls) > MAX_ATOMIC_BUSINESS_CONTROL_BINDINGS
            or any(type(value) is not AtomicBusinessControlBinding for value in self.controls)
            or any(value.ordinal != ordinal for ordinal, value in enumerate(self.controls))
        ):
            _fail("atomic_business_write_controls_invalid")
        _digest(self.controls_sha256, "atomic_business_write_digest_invalid")
        try:
            exact_before = WorkerLedgerHead(
                self.before_worker_ledger.sequence,
                self.before_worker_ledger.tail_sha256,
            )
            exact_after = WorkerLedgerHead(
                self.after_worker_ledger.sequence,
                self.after_worker_ledger.tail_sha256,
            )
        except (
            AttributeError,
            TypeError,
            WorkerExecutionContractError,
        ):
            _fail("atomic_business_write_worker_ledger_invalid")
        if (
            type(self.before_worker_ledger) is not WorkerLedgerHead
            or type(self.after_worker_ledger) is not WorkerLedgerHead
            or exact_before != self.before_worker_ledger
            or exact_after != self.after_worker_ledger
            or self.after_worker_ledger.sequence
            != self.before_worker_ledger.sequence + len(self.controls)
        ):
            _fail("atomic_business_write_worker_ledger_invalid")
        if (
            type(self.manifest_bytes) is not bytes
            or not self.manifest_bytes
            or len(self.manifest_bytes) > MAX_ATOMIC_BUSINESS_WRITE_MANIFEST_BYTES
        ):
            _fail("atomic_business_write_manifest_invalid")
        _digest(self.manifest_sha256, "atomic_business_write_digest_invalid")
        if self.protocol != ATOMIC_BUSINESS_WRITE_PROTOCOL:
            _fail("atomic_business_write_protocol_invalid")
        if self.status != ATOMIC_BUSINESS_WRITE_PLAN_STATUS:
            _fail("atomic_business_write_status_invalid")


@dataclass(frozen=True)
class AtomicBusinessWriteGenericReceiptEvidence:
    transaction_id: str
    kind: str
    committed_at: str

    def __post_init__(self) -> None:
        _text(self.transaction_id, "atomic_business_write_receipt_invalid")
        _text(self.kind, "atomic_business_write_receipt_invalid")
        _timestamp(self.committed_at, "atomic_business_write_receipt_invalid")


@dataclass(frozen=True)
class AtomicBusinessWriteReceiptEvidence:
    """Expected specialised row parented by the business commit receipt."""

    commit_id: str
    receipt_parent: AtomicBusinessWriteGenericReceiptEvidence
    atomic_manifest_sha256: bytes = field(repr=False)
    pipeline_intents_sha256: bytes = field(repr=False)
    controls_sha256: bytes = field(repr=False)
    control_count: int
    before_worker_ledger: WorkerLedgerHead
    after_worker_ledger: WorkerLedgerHead
    manifest_sha256: bytes = field(repr=False)
    committed_at: str
    receipt_sha256: bytes = field(repr=False)

    def __post_init__(self) -> None:
        _hex(self.commit_id, 32, "atomic_business_write_receipt_invalid")
        if type(self.receipt_parent) is not AtomicBusinessWriteGenericReceiptEvidence:
            _fail("atomic_business_write_receipt_invalid")
        if (
            self.receipt_parent.transaction_id
            != ATOMIC_BUSINESS_WRITE_TRANSACTION_PREFIX + self.commit_id
            or self.receipt_parent.kind != ATOMIC_BUSINESS_WRITE_TRANSACTION_KIND
            or self.receipt_parent.committed_at != self.committed_at
        ):
            _fail("atomic_business_write_receipt_invalid")
        _digest(
            self.atomic_manifest_sha256,
            "atomic_business_write_receipt_invalid",
        )
        _digest(
            self.pipeline_intents_sha256,
            "atomic_business_write_receipt_invalid",
        )
        _digest(self.controls_sha256, "atomic_business_write_receipt_invalid")
        if (
            type(self.control_count) is not int
            or not 0 <= self.control_count <= MAX_ATOMIC_BUSINESS_CONTROL_BINDINGS
            or type(self.before_worker_ledger) is not WorkerLedgerHead
            or type(self.after_worker_ledger) is not WorkerLedgerHead
            or self.after_worker_ledger.sequence
            != self.before_worker_ledger.sequence + self.control_count
        ):
            _fail("atomic_business_write_receipt_invalid")
        _digest(self.manifest_sha256, "atomic_business_write_receipt_invalid")
        _timestamp(self.committed_at, "atomic_business_write_receipt_invalid")
        _digest(self.receipt_sha256, "atomic_business_write_receipt_invalid")


@dataclass(frozen=True)
class AtomicBusinessWritePersistedObservation:
    """Raw same-snapshot receipt/control/head projection; not fresh alone."""

    read_status: AtomicBusinessWritePersistedReadStatus
    receipt: Optional[AtomicBusinessWriteGenericReceiptEvidence] = None
    atomic_receipt: Optional[AtomicBusinessWriteReceiptEvidence] = None
    control_receipts: Tuple[PipelineBusinessControlReceiptEvidence, ...] = ()
    current_worker_ledger: Optional[WorkerLedgerHead] = None
    ledger_membership: Optional[WorkerLedgerMembershipEvidence] = None
    error_code: str = ""

    def __post_init__(self) -> None:
        if type(self.read_status) is not AtomicBusinessWritePersistedReadStatus:
            _fail("atomic_business_write_persisted_read_invalid")
        if self.read_status is AtomicBusinessWritePersistedReadStatus.UNKNOWN:
            if (
                self.receipt is not None
                or self.atomic_receipt is not None
                or self.control_receipts != ()
                or self.current_worker_ledger is not None
                or self.ledger_membership is not None
                or type(self.error_code) is not str
                or not self.error_code
            ):
                _fail("atomic_business_write_persisted_read_invalid")
            _text(
                self.error_code,
                "atomic_business_write_persisted_read_invalid",
                maximum=1024,
            )
            return
        if (
            self.error_code != ""
            or (
                self.receipt is not None
                and type(self.receipt)
                is not AtomicBusinessWriteGenericReceiptEvidence
            )
            or (
                self.atomic_receipt is not None
                and type(self.atomic_receipt)
                is not AtomicBusinessWriteReceiptEvidence
            )
            or type(self.control_receipts) is not tuple
            or len(self.control_receipts) > MAX_ATOMIC_BUSINESS_CONTROL_BINDINGS
            or any(
                type(value) is not PipelineBusinessControlReceiptEvidence
                for value in self.control_receipts
            )
            or type(self.current_worker_ledger) is not WorkerLedgerHead
        ):
            _fail("atomic_business_write_persisted_read_invalid")
        if self.ledger_membership is not None:
            if (
                type(self.ledger_membership)
                is not WorkerLedgerMembershipEvidence
                or self.atomic_receipt is None
                or self.ledger_membership.observed_head
                != self.current_worker_ledger
                or self.ledger_membership.receipt_sequence
                != self.atomic_receipt.after_worker_ledger.sequence
                or self.ledger_membership.receipt_tail_sha256
                != self.atomic_receipt.after_worker_ledger.tail_sha256
            ):
                _fail("atomic_business_write_persisted_read_invalid")


_DURABLE_READER_CAPABILITY_ISSUER = object()
_FRESH_OBSERVATION_ISSUER = object()


class AtomicBusinessWriteDurableReaderCapability:
    __slots__ = ("_issuer",)

    def __init__(self, *, _issuer: Any) -> None:
        if _issuer is not _DURABLE_READER_CAPABILITY_ISSUER:
            _fail("atomic_business_write_reader_capability_invalid")
        object.__setattr__(self, "_issuer", _issuer)

    def __setattr__(self, name: str, value: Any) -> None:
        del name, value
        raise AttributeError(
            "AtomicBusinessWriteDurableReaderCapability is immutable"
        )


_ATOMIC_BUSINESS_WRITE_DURABLE_READER_CAPABILITY = (
    AtomicBusinessWriteDurableReaderCapability(
        _issuer=_DURABLE_READER_CAPABILITY_ISSUER
    )
)


class AtomicBusinessWriteFreshObservation:
    __slots__ = ("persisted", "_reader", "_issuer")

    def __init__(
        self,
        persisted: AtomicBusinessWritePersistedObservation,
        *,
        _reader: AtomicBusinessWriteDurableReaderCapability,
        _issuer: Any,
    ) -> None:
        if (
            _issuer is not _FRESH_OBSERVATION_ISSUER
            or _reader is not _ATOMIC_BUSINESS_WRITE_DURABLE_READER_CAPABILITY
            or type(persisted) is not AtomicBusinessWritePersistedObservation
        ):
            _fail("atomic_business_write_fresh_observation_invalid")
        object.__setattr__(self, "persisted", persisted)
        object.__setattr__(self, "_reader", _reader)
        object.__setattr__(self, "_issuer", _issuer)

    def __setattr__(self, name: str, value: Any) -> None:
        del name, value
        raise AttributeError("AtomicBusinessWriteFreshObservation is immutable")


def _stamp_atomic_business_write_fresh_observation(
    reader: AtomicBusinessWriteDurableReaderCapability,
    persisted: AtomicBusinessWritePersistedObservation,
) -> AtomicBusinessWriteFreshObservation:
    if reader is not _ATOMIC_BUSINESS_WRITE_DURABLE_READER_CAPABILITY:
        _fail("atomic_business_write_reader_capability_invalid")
    return AtomicBusinessWriteFreshObservation(
        persisted,
        _reader=reader,
        _issuer=_FRESH_OBSERVATION_ISSUER,
    )


def _unwrap_atomic_business_write_fresh_observation(
    value: Any,
) -> AtomicBusinessWritePersistedObservation:
    if (
        type(value) is not AtomicBusinessWriteFreshObservation
        or getattr(value, "_reader", None)
        is not _ATOMIC_BUSINESS_WRITE_DURABLE_READER_CAPABILITY
        or getattr(value, "_issuer", None) is not _FRESH_OBSERVATION_ISSUER
        or type(getattr(value, "persisted", None))
        is not AtomicBusinessWritePersistedObservation
    ):
        _fail("atomic_business_write_fresh_observation_invalid")
    return value.persisted


@dataclass(frozen=True)
class AtomicBusinessWriteClassification:
    status: AtomicBusinessWriteReconcileStatus
    commit_id: str
    evidence: Optional[AtomicBusinessWriteReceiptEvidence] = None
    error_code: str = ""

    def __post_init__(self) -> None:
        if type(self.status) is not AtomicBusinessWriteReconcileStatus:
            _fail("atomic_business_write_classification_invalid")
        _hex(self.commit_id, 32, "atomic_business_write_classification_invalid")
        exact = self.status is AtomicBusinessWriteReconcileStatus.EXACT
        failed = self.status in (
            AtomicBusinessWriteReconcileStatus.CONFLICT,
            AtomicBusinessWriteReconcileStatus.CORRUPT,
            AtomicBusinessWriteReconcileStatus.UNKNOWN,
        )
        if (
            (exact and type(self.evidence) is not AtomicBusinessWriteReceiptEvidence)
            or (not exact and self.evidence is not None)
            or (
                exact
                and self.evidence is not None
                and self.evidence.commit_id != self.commit_id
            )
            or (failed and (type(self.error_code) is not str or not self.error_code))
            or (not failed and self.error_code != "")
        ):
            _fail("atomic_business_write_classification_invalid")

    @property
    def retry_action(self) -> AtomicBusinessWriteRetryAction:
        if self.status is AtomicBusinessWriteReconcileStatus.ABSENT:
            return AtomicBusinessWriteRetryAction.APPLY_EXACT
        if self.status is AtomicBusinessWriteReconcileStatus.EXACT:
            return AtomicBusinessWriteRetryAction.RETURN_EXACT
        if self.status is AtomicBusinessWriteReconcileStatus.UNKNOWN:
            return AtomicBusinessWriteRetryAction.RECONCILE_REQUIRED
        return AtomicBusinessWriteRetryAction.STOP


__all__ = ()
