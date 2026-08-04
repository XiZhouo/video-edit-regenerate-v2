"""Pure V13 contracts for four direct-predecessor cutover bindings.

The canonical phase head proves the order of the five cutover states, but a
head hash alone does not prove that the external asset provider, repository
audit, file admission, runtime and worker cursors observed by a transition
were the exact durable facts used by that transition.  These values make all
of those facts explicit.  They contain no SQLite handle, filesystem path,
clock, callback, SDK type or authority capability.

``BusinessCutoverPhaseBindingProjection`` is an unissued observation-shaped
value.  The integrity module is the only constructor of an issued
``BusinessCutoverPhaseBinding``.  A storage adapter must still obtain every
field under its own transaction and maintenance barrier; these pure values do
not claim freshness or persistence by themselves.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional, Union

from business_cutover_head_integrity import (
    BusinessCutoverHeadIntegrityError,
    BusinessCutoverHeadPointer,
    BusinessCutoverHeadPointerRole,
    BusinessCutoverHeadState,
)


BUSINESS_CUTOVER_PHASE_BINDING_PROTOCOL = "ze.business-cutover.phase-binding.v1"
BUSINESS_CUTOVER_PHASE_BINDING_MANIFEST_SCHEMA = (
    "ze.business-cutover.phase-binding-manifest.v1"
)
BUSINESS_CUTOVER_PHASE_BINDING_STATUS = "bound_exact"
BUSINESS_CUTOVER_PROVIDER_RECEIPT_CODEC = "ze.deterministic-json"
BUSINESS_CUTOVER_PROVIDER_RECEIPT_VERSION = 1
BUSINESS_CUTOVER_PROVIDER_RECEIPT_SCHEMA = (
    "ze.business-cutover.asset-provider-receipt.v1"
)

MAX_BUSINESS_CUTOVER_PHASE_BINDING_TEXT_BYTES = 1024 * 1024
MAX_BUSINESS_CUTOVER_PHASE_BINDING_RUNTIME_GENERATION_BYTES = 256
MAX_BUSINESS_CUTOVER_PHASE_BINDING_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_BUSINESS_CUTOVER_PHASE_BINDING_MANIFEST_NODES = 100_000
MAX_BUSINESS_CUTOVER_PROVIDER_PAYLOAD_BYTES = 16 * 1024 * 1024
MAX_SQLITE_INTEGER = 2**63 - 1
EMPTY_PHASE_BINDING_DIGEST = b"\0" * 32
_LOWER_HEX = frozenset("0123456789abcdef")
_BINDING_ISSUER = object()


class BusinessCutoverPhaseBindingContractError(ValueError):
    """Stable payload-free failure at the V13 binding contract boundary."""

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
    raise BusinessCutoverPhaseBindingContractError(code, path)


def _text(
    value: Any,
    path: str,
    *,
    maximum: int = MAX_BUSINESS_CUTOVER_PHASE_BINDING_TEXT_BYTES,
    allow_empty: bool = False,
) -> str:
    if type(value) is not str or (not allow_empty and not value):
        _fail("business_cutover_phase_binding_text_invalid", path)
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeEncodeError:
        _fail("business_cutover_phase_binding_text_invalid", path)
        raise AssertionError("unreachable")
    if len(encoded) > maximum:
        _fail("business_cutover_phase_binding_text_budget_exceeded", path)
    return value


def _integer(
    value: Any,
    path: str,
    *,
    minimum: int = 0,
    maximum: int = MAX_SQLITE_INTEGER,
) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _fail("business_cutover_phase_binding_integer_invalid", path)
    return value


def _digest(value: Any, path: str, *, allow_zero: bool = True) -> bytes:
    if type(value) is not bytes or len(value) != 32:
        _fail("business_cutover_phase_binding_digest_invalid", path)
    if not allow_zero and hmac.compare_digest(value, EMPTY_PHASE_BINDING_DIGEST):
        _fail("business_cutover_phase_binding_digest_invalid", path)
    return value


def _lower_hex(value: Any, path: str, length: int) -> str:
    if (
        type(value) is not str
        or len(value) != length
        or any(character not in _LOWER_HEX for character in value)
    ):
        _fail("business_cutover_phase_binding_hex_invalid", path)
    return value


def _timestamp(value: Any, path: str) -> str:
    if type(value) is not str or len(value) != 24:
        _fail("business_cutover_phase_binding_timestamp_invalid", path)
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except (TypeError, ValueError):
        _fail("business_cutover_phase_binding_timestamp_invalid", path)
        raise AssertionError("unreachable")
    canonical = parsed.strftime("%Y-%m-%dT%H:%M:%S.") + (
        "%03dZ" % (parsed.microsecond // 1000)
    )
    if canonical != value:
        _fail("business_cutover_phase_binding_timestamp_invalid", path)
    return value


class BusinessCutoverPhaseBindingStage(str, Enum):
    ASSET_PROVIDER = "asset_provider"
    AUDIT = "audit"
    ACTIVATION = "activation"
    MOUNT = "mount"


BUSINESS_CUTOVER_PHASE_BINDING_DOMAINS = {
    BusinessCutoverPhaseBindingStage.ASSET_PROVIDER: (
        "ze.business-cutover.asset-provider-phase-binding.v1"
    ),
    BusinessCutoverPhaseBindingStage.AUDIT: (
        "ze.business-cutover.audit-phase-binding.v1"
    ),
    BusinessCutoverPhaseBindingStage.ACTIVATION: (
        "ze.business-cutover.activation-phase-binding.v1"
    ),
    BusinessCutoverPhaseBindingStage.MOUNT: (
        "ze.business-cutover.mount-phase-binding.v1"
    ),
}

_STAGE_TRANSITIONS = {
    BusinessCutoverPhaseBindingStage.ASSET_PROVIDER: (
        1,
        "installed",
        "read_only",
        2,
        "assets_covered",
        "read_only",
        "cutover_pending",
    ),
    BusinessCutoverPhaseBindingStage.AUDIT: (
        2,
        "assets_covered",
        "read_only",
        3,
        "audited",
        "read_only",
        "cutover_pending",
    ),
    BusinessCutoverPhaseBindingStage.ACTIVATION: (
        3,
        "audited",
        "read_only",
        4,
        "activated",
        "active",
        "sqlite",
    ),
    BusinessCutoverPhaseBindingStage.MOUNT: (
        4,
        "activated",
        "active",
        5,
        "mounted",
        "active",
        "sqlite",
    ),
}


@dataclass(frozen=True)
class BusinessCutoverPhaseBindingIdentity:
    cutover_id: str
    repository_epoch: str
    runtime_generation: str
    code_digest: bytes = field(repr=False)

    def __post_init__(self) -> None:
        _lower_hex(self.cutover_id, "$.identity.cutover_id", 64)
        _lower_hex(self.repository_epoch, "$.identity.repository_epoch", 64)
        _text(
            self.runtime_generation,
            "$.identity.runtime_generation",
            maximum=MAX_BUSINESS_CUTOVER_PHASE_BINDING_RUNTIME_GENERATION_BYTES,
        )
        _digest(self.code_digest, "$.identity.code_digest")


@dataclass(frozen=True)
class BusinessCutoverPhaseBindingHead:
    state_revision: int
    phase_revision: int
    phase: str
    authority_state: str
    head_sha256: bytes = field(repr=False)
    updated_at: str
    previous_head_sha256: bytes = field(repr=False)
    install: BusinessCutoverReceiptPointer
    coverage: Optional[BusinessCutoverReceiptPointer] = None
    audit: Optional[BusinessCutoverReceiptPointer] = None
    activation: Optional[BusinessCutoverReceiptPointer] = None
    mount: Optional[BusinessCutoverReceiptPointer] = None

    def __post_init__(self) -> None:
        _integer(self.state_revision, "$.head.state_revision", minimum=1, maximum=5)
        _integer(self.phase_revision, "$.head.phase_revision", minimum=1, maximum=5)
        if self.state_revision != self.phase_revision:
            _fail("business_cutover_phase_binding_head_invalid", "$.head")
        _text(self.phase, "$.head.phase")
        if self.authority_state not in ("read_only", "active"):
            _fail("business_cutover_phase_binding_head_invalid", "$.head.authority_state")
        _digest(self.head_sha256, "$.head.head_sha256", allow_zero=False)
        _timestamp(self.updated_at, "$.head.updated_at")
        previous = _digest(
            self.previous_head_sha256,
            "$.head.previous_head_sha256",
        )
        if (self.phase_revision == 1) != hmac.compare_digest(
            previous,
            EMPTY_PHASE_BINDING_DIGEST,
        ):
            _fail("business_cutover_phase_binding_head_invalid", "$.head.previous_head_sha256")
        expected_state = {
            1: ("installed", "read_only"),
            2: ("assets_covered", "read_only"),
            3: ("audited", "read_only"),
            4: ("activated", "active"),
            5: ("mounted", "active"),
        }[self.phase_revision]
        if (self.phase, self.authority_state) != expected_state:
            _fail("business_cutover_phase_binding_head_invalid", "$.head.phase")
        if type(self.install) is not BusinessCutoverReceiptPointer:
            _fail("business_cutover_phase_binding_head_invalid", "$.head.install")
        cumulative = (self.coverage, self.audit, self.activation, self.mount)
        expected_present = tuple(
            self.phase_revision >= revision for revision in range(2, 6)
        )
        if tuple(value is not None for value in cumulative) != expected_present:
            _fail("business_cutover_phase_binding_head_invalid", "$.head.pointers")
        if any(
            value is not None and type(value) is not BusinessCutoverReceiptPointer
            for value in cumulative
        ):
            _fail("business_cutover_phase_binding_head_invalid", "$.head.pointers")
        chronology = tuple(
            value.committed_at
            for value in (self.coverage, self.install, self.audit, self.activation, self.mount)
            if value is not None
        )
        if chronology != tuple(sorted(chronology)):
            _fail("business_cutover_phase_binding_head_invalid", "$.head.chronology")


@dataclass(frozen=True)
class BusinessCutoverAdmissionCheckpoint:
    source_revision: int
    source_admission_sha256: bytes = field(repr=False)
    source_freeze_sha256: bytes = field(repr=False)
    file_authority_state: str
    file_authority_epoch: str
    file_revision: int
    file_cutover_id: str
    file_admission_sha256: bytes = field(repr=False)

    def __post_init__(self) -> None:
        _integer(self.source_revision, "$.admission.source_revision", minimum=1)
        _digest(self.source_admission_sha256, "$.admission.source_admission_sha256")
        _digest(self.source_freeze_sha256, "$.admission.source_freeze_sha256")
        if self.file_authority_state not in ("cutover_pending", "sqlite"):
            _fail("business_cutover_phase_binding_admission_invalid", "$.admission.file_authority_state")
        _lower_hex(self.file_authority_epoch, "$.admission.file_authority_epoch", 64)
        _integer(self.file_revision, "$.admission.file_revision", minimum=1)
        _lower_hex(self.file_cutover_id, "$.admission.file_cutover_id", 64)
        _digest(self.file_admission_sha256, "$.admission.file_admission_sha256")


@dataclass(frozen=True)
class BusinessCutoverMaintenanceCheckpoint:
    scope: str
    proof_sha256: bytes = field(repr=False)
    observed_at: str

    def __post_init__(self) -> None:
        if self.scope != "catalog_ex_repository_ex":
            _fail(
                "business_cutover_phase_binding_maintenance_invalid",
                "$.maintenance.scope",
            )
        _digest(self.proof_sha256, "$.maintenance.proof_sha256", allow_zero=False)
        _timestamp(self.observed_at, "$.maintenance.observed_at")


@dataclass(frozen=True)
class BusinessCutoverRuntimeImage:
    authority: str
    authority_state: str
    source_admission_revision: int
    repository_revision: int
    state_revision: int
    schema_version: int
    updated_at: str

    def __post_init__(self) -> None:
        if self.authority != "sqlite" or self.authority_state not in ("read_only", "active"):
            _fail("business_cutover_phase_binding_runtime_invalid", "$.runtime")
        _integer(self.source_admission_revision, "$.runtime.source_admission_revision", minimum=1)
        _integer(self.repository_revision, "$.runtime.repository_revision", minimum=1)
        _integer(self.state_revision, "$.runtime.state_revision", minimum=1)
        if self.schema_version != 6:
            _fail("business_cutover_phase_binding_runtime_invalid", "$.runtime.schema_version")
        expected_state_revision = 1 if self.authority_state == "read_only" else 2
        if self.state_revision != expected_state_revision:
            _fail("business_cutover_phase_binding_runtime_invalid", "$.runtime.state_revision")
        _timestamp(self.updated_at, "$.runtime.updated_at")


@dataclass(frozen=True)
class BusinessCutoverWorkerHighWater:
    state_revision: int
    sequence: int
    tail_sha256: bytes = field(repr=False)
    updated_at: str

    def __post_init__(self) -> None:
        _integer(self.state_revision, "$.worker.state_revision", minimum=1)
        _integer(self.sequence, "$.worker.sequence")
        tail = _digest(self.tail_sha256, "$.worker.tail_sha256")
        _timestamp(self.updated_at, "$.worker.updated_at")
        if (
            self.state_revision != 1
            or self.sequence != 0
            or not hmac.compare_digest(tail, EMPTY_PHASE_BINDING_DIGEST)
        ):
            _fail("business_cutover_phase_binding_worker_invalid", "$.worker")


@dataclass(frozen=True)
class BusinessCutoverAllocationHighWater:
    state_revision: int
    sequence: int
    tail_sha256: bytes = field(repr=False)
    updated_at: str

    def __post_init__(self) -> None:
        _integer(self.state_revision, "$.allocation.state_revision", minimum=1)
        _integer(self.sequence, "$.allocation.sequence")
        tail = _digest(self.tail_sha256, "$.allocation.tail_sha256")
        _timestamp(self.updated_at, "$.allocation.updated_at")
        if (
            self.state_revision != 1
            or self.sequence != 0
            or not hmac.compare_digest(tail, EMPTY_PHASE_BINDING_DIGEST)
        ):
            _fail("business_cutover_phase_binding_allocation_invalid", "$.allocation")


@dataclass(frozen=True)
class BusinessCutoverDispatchHighWater:
    state_revision: int
    published_sequence: int
    published_repository_revision: Optional[int]
    published_ordinal: Optional[int]
    inflight_sequence: Optional[int]
    inflight_repository_revision: Optional[int]
    inflight_ordinal: Optional[int]
    head_sha256: bytes = field(repr=False)
    updated_at: str

    def __post_init__(self) -> None:
        _integer(self.state_revision, "$.dispatch.state_revision", minimum=1)
        _integer(self.published_sequence, "$.dispatch.published_sequence")
        if not (
            self.state_revision == 1
            and self.published_sequence == 0
            and self.published_repository_revision is None
            and self.published_ordinal is None
        ):
            _fail("business_cutover_phase_binding_dispatch_invalid", "$.dispatch.published")
        inflight = (
            self.inflight_sequence,
            self.inflight_repository_revision,
            self.inflight_ordinal,
        )
        if any(value is not None for value in inflight):
            _fail("business_cutover_phase_binding_dispatch_invalid", "$.dispatch.inflight")
        head = _digest(self.head_sha256, "$.dispatch.head_sha256")
        _timestamp(self.updated_at, "$.dispatch.updated_at")
        if not hmac.compare_digest(head, EMPTY_PHASE_BINDING_DIGEST):
            _fail("business_cutover_phase_binding_dispatch_invalid", "$.dispatch.head_sha256")


@dataclass(frozen=True)
class BusinessCutoverDurableCheckpoint:
    runtime: BusinessCutoverRuntimeImage
    receipt_before_count: int
    receipt_before_sha256: bytes = field(repr=False)
    receipt_after_count: int
    receipt_after_sha256: bytes = field(repr=False)
    worker: BusinessCutoverWorkerHighWater
    allocation: BusinessCutoverAllocationHighWater
    dispatch: BusinessCutoverDispatchHighWater

    def __post_init__(self) -> None:
        if type(self.runtime) is not BusinessCutoverRuntimeImage:
            _fail("business_cutover_phase_binding_checkpoint_invalid", "$.checkpoint.runtime")
        before = _integer(self.receipt_before_count, "$.checkpoint.receipt_before_count")
        before_sha = _digest(self.receipt_before_sha256, "$.checkpoint.receipt_before_sha256")
        after = _integer(self.receipt_after_count, "$.checkpoint.receipt_after_count")
        after_sha = _digest(self.receipt_after_sha256, "$.checkpoint.receipt_after_sha256")
        if (before == 0) != hmac.compare_digest(before_sha, EMPTY_PHASE_BINDING_DIGEST):
            _fail("business_cutover_phase_binding_receipt_checkpoint_invalid", "$.checkpoint.before")
        if (
            after not in (before, before + 1)
            or hmac.compare_digest(before_sha, after_sha) != (after == before)
        ):
            _fail("business_cutover_phase_binding_receipt_checkpoint_invalid", "$.checkpoint.after")
        if type(self.worker) is not BusinessCutoverWorkerHighWater:
            _fail("business_cutover_phase_binding_checkpoint_invalid", "$.checkpoint.worker")
        if type(self.allocation) is not BusinessCutoverAllocationHighWater:
            _fail("business_cutover_phase_binding_checkpoint_invalid", "$.checkpoint.allocation")
        if type(self.dispatch) is not BusinessCutoverDispatchHighWater:
            _fail("business_cutover_phase_binding_checkpoint_invalid", "$.checkpoint.dispatch")


@dataclass(frozen=True)
class BusinessCutoverReceiptPointer:
    transaction_id: str
    receipt_kind: str
    committed_at: str
    manifest_sha256: bytes = field(repr=False)

    def __post_init__(self) -> None:
        _text(self.transaction_id, "$.pointer.transaction_id")
        _text(self.receipt_kind, "$.pointer.receipt_kind")
        _timestamp(self.committed_at, "$.pointer.committed_at")
        _digest(self.manifest_sha256, "$.pointer.manifest_sha256", allow_zero=False)


@dataclass(frozen=True)
class BusinessCutoverAssetCoverageCheckpoint:
    pointer: BusinessCutoverReceiptPointer
    coverage_sha256: bytes = field(repr=False)
    member_root_sha256: bytes = field(repr=False)
    blob_root_sha256: bytes = field(repr=False)
    batch_count: int = 0
    repository_member_count: int = 0
    live_project_count: int = 0
    deleted_project_count: int = 0
    project_count: int = 0
    reference_count: int = 0
    unique_blob_count: int = 0
    unique_blob_bytes: int = 0
    referenced_bytes: int = 0
    observed_occurrence_count: int = 0
    observed_row_count: int = 0
    observed_manifest_bytes: int = 0

    def __post_init__(self) -> None:
        if type(self.pointer) is not BusinessCutoverReceiptPointer:
            _fail("business_cutover_phase_binding_coverage_invalid", "$.coverage.pointer")
        _digest(self.coverage_sha256, "$.coverage.coverage_sha256", allow_zero=False)
        _digest(self.member_root_sha256, "$.coverage.member_root_sha256", allow_zero=False)
        _digest(self.blob_root_sha256, "$.coverage.blob_root_sha256", allow_zero=False)
        for name in (
            "batch_count", "repository_member_count", "live_project_count",
            "deleted_project_count", "project_count", "reference_count",
            "unique_blob_count", "unique_blob_bytes", "referenced_bytes",
            "observed_occurrence_count", "observed_row_count",
            "observed_manifest_bytes",
        ):
            _integer(getattr(self, name), "$.coverage." + name)
        if self.repository_member_count != self.live_project_count + self.deleted_project_count:
            _fail("business_cutover_phase_binding_coverage_invalid", "$.coverage.project_count")
        if self.project_count != self.live_project_count:
            _fail("business_cutover_phase_binding_coverage_invalid", "$.coverage.repository_member_count")
        if self.unique_blob_count > self.reference_count:
            _fail("business_cutover_phase_binding_coverage_invalid", "$.coverage.unique_blob_count")
        if (
            self.unique_blob_bytes > self.referenced_bytes
            or self.observed_occurrence_count < self.reference_count
            or self.observed_row_count < self.observed_occurrence_count
            or (self.batch_count == 0) != (self.reference_count == 0)
            or (self.batch_count == 0) != (self.observed_manifest_bytes == 0)
        ):
            _fail("business_cutover_phase_binding_coverage_invalid", "$.coverage")


@dataclass(frozen=True)
class BusinessCutoverAssetProviderEvidence:
    coverage: BusinessCutoverAssetCoverageCheckpoint
    provider_protocol: str
    provider_id: str
    provider_generation: str
    provider_sequence: int
    checked_at: str
    object_count: int
    total_bytes: int
    blob_root_sha256: bytes = field(repr=False)
    receipt_schema: str = BUSINESS_CUTOVER_PROVIDER_RECEIPT_SCHEMA
    receipt_codec: str = BUSINESS_CUTOVER_PROVIDER_RECEIPT_CODEC
    receipt_version: int = BUSINESS_CUTOVER_PROVIDER_RECEIPT_VERSION
    receipt_payload: bytes = field(default=b"", repr=False)
    receipt_payload_sha256: bytes = field(default=EMPTY_PHASE_BINDING_DIGEST, repr=False)
    checkpoint_sha256: bytes = field(default=EMPTY_PHASE_BINDING_DIGEST, repr=False)

    def __post_init__(self) -> None:
        if type(self.coverage) is not BusinessCutoverAssetCoverageCheckpoint:
            _fail("business_cutover_phase_binding_asset_provider_invalid", "$.asset_provider.coverage")
        if self.provider_protocol != "ze.business-cutover.asset-provider-checkpoint.v1":
            _fail("business_cutover_phase_binding_asset_provider_invalid", "$.asset_provider.provider_protocol")
        for name in ("provider_id", "provider_generation"):
            _text(getattr(self, name), "$.asset_provider." + name, maximum=1024)
        _integer(self.provider_sequence, "$.asset_provider.provider_sequence", minimum=1)
        _timestamp(self.checked_at, "$.asset_provider.checked_at")
        _integer(self.object_count, "$.asset_provider.object_count")
        _integer(self.total_bytes, "$.asset_provider.total_bytes")
        _digest(self.blob_root_sha256, "$.asset_provider.blob_root_sha256", allow_zero=False)
        if (
            self.object_count != self.coverage.unique_blob_count
            or self.total_bytes != self.coverage.unique_blob_bytes
            or not hmac.compare_digest(
                self.blob_root_sha256,
                self.coverage.blob_root_sha256,
            )
        ):
            _fail(
                "business_cutover_phase_binding_provider_coverage_mismatch",
                "$.asset_provider",
            )
        if self.receipt_schema != BUSINESS_CUTOVER_PROVIDER_RECEIPT_SCHEMA:
            _fail("business_cutover_phase_binding_provider_receipt_invalid", "$.asset_provider.receipt_schema")
        if self.receipt_codec != BUSINESS_CUTOVER_PROVIDER_RECEIPT_CODEC:
            _fail("business_cutover_phase_binding_provider_receipt_invalid", "$.asset_provider.receipt_codec")
        if self.receipt_version != BUSINESS_CUTOVER_PROVIDER_RECEIPT_VERSION:
            _fail("business_cutover_phase_binding_provider_receipt_invalid", "$.asset_provider.receipt_version")
        if type(self.receipt_payload) is not bytes or not 1 <= len(self.receipt_payload) <= MAX_BUSINESS_CUTOVER_PROVIDER_PAYLOAD_BYTES:
            _fail("business_cutover_phase_binding_provider_receipt_invalid", "$.asset_provider.receipt_payload")
        _digest(self.receipt_payload_sha256, "$.asset_provider.receipt_payload_sha256", allow_zero=False)
        _digest(self.checkpoint_sha256, "$.asset_provider.checkpoint_sha256", allow_zero=False)


@dataclass(frozen=True)
class BusinessCutoverAuditEvidence:
    certificate_id: str
    certificate_sequence: int
    purpose: str
    receipt_parent: BusinessCutoverReceiptPointer
    previous_certificate_id: Optional[str]
    previous_manifest_sha256: Optional[bytes] = field(default=None, repr=False)
    previous_tail_sha256: bytes = field(default=EMPTY_PHASE_BINDING_DIGEST, repr=False)
    certificate_tail_sha256: bytes = field(default=EMPTY_PHASE_BINDING_DIGEST, repr=False)
    findings_sha256: bytes = field(default=EMPTY_PHASE_BINDING_DIGEST, repr=False)
    schema_version: int = 0
    schema_fingerprint: str = ""
    migration_ledger_count: int = 0
    migration_ledger_sha256: bytes = field(default=EMPTY_PHASE_BINDING_DIGEST, repr=False)
    install_manifest_sha256: bytes = field(default=EMPTY_PHASE_BINDING_DIGEST, repr=False)
    covered_business_revision: int = 0
    started_at: str = ""
    completed_at: str = ""
    page_integrity: str = "ok"
    foreign_key_violation_count: int = 0
    namespace_violation_count: int = 0
    audit_head_state_revision: int = 0
    audit_head_sequence: int = 0
    audit_head_certificate_id: str = ""
    audit_head_previous_tail_sha256: bytes = field(default=EMPTY_PHASE_BINDING_DIGEST, repr=False)
    audit_head_tail_sha256: bytes = field(default=EMPTY_PHASE_BINDING_DIGEST, repr=False)
    audit_head_updated_at: str = ""

    def __post_init__(self) -> None:
        _lower_hex(self.certificate_id, "$.audit.certificate_id", 32)
        _integer(self.certificate_sequence, "$.audit.certificate_sequence", minimum=1)
        if self.purpose != "install":
            _fail("business_cutover_phase_binding_audit_invalid", "$.audit.purpose")
        if type(self.receipt_parent) is not BusinessCutoverReceiptPointer:
            _fail("business_cutover_phase_binding_audit_invalid", "$.audit.receipt_parent")
        previous = (self.previous_certificate_id, self.previous_manifest_sha256)
        _digest(self.previous_tail_sha256, "$.audit.previous_tail_sha256")
        if any(value is None for value in previous):
            if any(value is not None for value in previous):
                _fail("business_cutover_phase_binding_audit_invalid", "$.audit.previous")
            if self.certificate_sequence != 1 or not hmac.compare_digest(
                self.previous_tail_sha256,
                EMPTY_PHASE_BINDING_DIGEST,
            ):
                _fail("business_cutover_phase_binding_audit_invalid", "$.audit.previous")
        else:
            _lower_hex(self.previous_certificate_id, "$.audit.previous_certificate_id", 32)
            _digest(self.previous_manifest_sha256, "$.audit.previous_manifest_sha256", allow_zero=False)
            _digest(self.previous_tail_sha256, "$.audit.previous_tail_sha256", allow_zero=False)
            if self.certificate_sequence <= 1:
                _fail("business_cutover_phase_binding_audit_invalid", "$.audit.previous")
        for name in (
            "certificate_tail_sha256", "findings_sha256", "migration_ledger_sha256",
            "install_manifest_sha256", "audit_head_tail_sha256",
        ):
            _digest(getattr(self, name), "$.audit." + name, allow_zero=False)
        _integer(self.schema_version, "$.audit.schema_version", minimum=1)
        _lower_hex(self.schema_fingerprint, "$.audit.schema_fingerprint", 64)
        _integer(self.migration_ledger_count, "$.audit.migration_ledger_count", minimum=1)
        if self.schema_version != 13 or self.migration_ledger_count != 13:
            _fail("business_cutover_phase_binding_audit_invalid", "$.audit.migration_ledger_count")
        _integer(self.covered_business_revision, "$.audit.covered_business_revision", minimum=1)
        _timestamp(self.started_at, "$.audit.started_at")
        _timestamp(self.completed_at, "$.audit.completed_at")
        if self.completed_at < self.started_at or self.receipt_parent.committed_at != self.completed_at:
            _fail("business_cutover_phase_binding_audit_invalid", "$.audit.completed_at")
        if self.page_integrity != "ok" or self.foreign_key_violation_count != 0 or self.namespace_violation_count != 0:
            _fail("business_cutover_phase_binding_audit_findings_invalid", "$.audit.findings")
        _integer(self.audit_head_state_revision, "$.audit.audit_head_state_revision", minimum=1)
        _integer(self.audit_head_sequence, "$.audit.audit_head_sequence", minimum=1)
        _lower_hex(self.audit_head_certificate_id, "$.audit.audit_head_certificate_id", 32)
        _digest(self.audit_head_previous_tail_sha256, "$.audit.audit_head_previous_tail_sha256")
        _timestamp(self.audit_head_updated_at, "$.audit.audit_head_updated_at")
        if (
            self.audit_head_sequence != self.certificate_sequence
            or self.audit_head_certificate_id != self.certificate_id
            or not hmac.compare_digest(
                self.audit_head_previous_tail_sha256,
                self.previous_tail_sha256,
            )
            or not hmac.compare_digest(
                self.audit_head_tail_sha256,
                self.certificate_tail_sha256,
            )
            or self.audit_head_updated_at != self.completed_at
        ):
            _fail("business_cutover_phase_binding_audit_head_invalid", "$.audit.head")


@dataclass(frozen=True)
class BusinessCutoverActivationEvidence:
    activation: BusinessCutoverReceiptPointer
    install: BusinessCutoverReceiptPointer
    audit_certificate_id: str
    audit_certificate_sequence: int
    audit: BusinessCutoverReceiptPointer
    audit_binding: BusinessCutoverAuditEvidence
    audit_certificate_tail_sha256: bytes = field(repr=False)
    audit_completed_at: str
    expected_migration_ledger_count: int
    expected_migration_ledger_sha256: bytes = field(repr=False)
    actual_schema_version: int
    actual_schema_fingerprint: str
    final_admission_state: str
    final_admission_epoch: str
    final_admission_revision: int
    final_admission_cutover_id: str
    final_admission_sha256: bytes = field(repr=False)
    before_runtime: BusinessCutoverRuntimeImage
    after_runtime: BusinessCutoverRuntimeImage

    def __post_init__(self) -> None:
        for name in ("activation", "install", "audit"):
            if type(getattr(self, name)) is not BusinessCutoverReceiptPointer:
                _fail("business_cutover_phase_binding_activation_invalid", "$.activation." + name)
        if type(self.audit_binding) is not BusinessCutoverAuditEvidence:
            _fail(
                "business_cutover_phase_binding_activation_invalid",
                "$.activation.audit_binding",
            )
        _lower_hex(self.audit_certificate_id, "$.activation.audit_certificate_id", 32)
        _integer(self.audit_certificate_sequence, "$.activation.audit_certificate_sequence", minimum=1)
        _digest(self.audit_certificate_tail_sha256, "$.activation.audit_certificate_tail_sha256", allow_zero=False)
        _timestamp(self.audit_completed_at, "$.activation.audit_completed_at")
        _integer(self.expected_migration_ledger_count, "$.activation.expected_migration_ledger_count", minimum=1)
        _digest(self.expected_migration_ledger_sha256, "$.activation.expected_migration_ledger_sha256")
        _integer(self.actual_schema_version, "$.activation.actual_schema_version", minimum=1)
        _lower_hex(self.actual_schema_fingerprint, "$.activation.actual_schema_fingerprint", 64)
        if self.final_admission_state != "sqlite":
            _fail("business_cutover_phase_binding_activation_invalid", "$.activation.final_admission_state")
        _lower_hex(self.final_admission_epoch, "$.activation.final_admission_epoch", 64)
        _integer(self.final_admission_revision, "$.activation.final_admission_revision", minimum=1)
        _lower_hex(self.final_admission_cutover_id, "$.activation.final_admission_cutover_id", 64)
        _digest(self.final_admission_sha256, "$.activation.final_admission_sha256")
        if type(self.before_runtime) is not BusinessCutoverRuntimeImage or type(self.after_runtime) is not BusinessCutoverRuntimeImage:
            _fail("business_cutover_phase_binding_activation_invalid", "$.activation.runtime")
        if (
            self.before_runtime.authority_state != "read_only"
            or self.after_runtime.authority_state != "active"
            or self.before_runtime.source_admission_revision
            != self.after_runtime.source_admission_revision
            or self.before_runtime.repository_revision != self.after_runtime.repository_revision
            or self.after_runtime.state_revision != self.before_runtime.state_revision + 1
            or self.after_runtime.updated_at < self.before_runtime.updated_at
            or self.expected_migration_ledger_count != self.actual_schema_version
            or self.actual_schema_version != 13
            or self.audit_completed_at != self.audit.committed_at
            or self.activation.committed_at < self.audit_completed_at
            or self.audit_certificate_id != self.audit_binding.certificate_id
            or self.audit_certificate_sequence
            != self.audit_binding.certificate_sequence
            or self.audit != self.audit_binding.receipt_parent
            or not hmac.compare_digest(
                self.audit_certificate_tail_sha256,
                self.audit_binding.certificate_tail_sha256,
            )
            or self.audit_completed_at != self.audit_binding.completed_at
            or self.expected_migration_ledger_count
            != self.audit_binding.migration_ledger_count
            or not hmac.compare_digest(
                self.expected_migration_ledger_sha256,
                self.audit_binding.migration_ledger_sha256,
            )
            or self.actual_schema_version != self.audit_binding.schema_version
            or self.actual_schema_fingerprint
            != self.audit_binding.schema_fingerprint
            or not hmac.compare_digest(
                self.install.manifest_sha256,
                self.audit_binding.install_manifest_sha256,
            )
        ):
            _fail("business_cutover_phase_binding_activation_invalid", "$.activation")


@dataclass(frozen=True)
class BusinessCutoverMountEvidence:
    mount: BusinessCutoverReceiptPointer
    source_admission_sha256: bytes = field(repr=False)
    final_admission_sha256: bytes = field(repr=False)
    install: BusinessCutoverReceiptPointer
    coverage: BusinessCutoverAssetCoverageCheckpoint
    activation: BusinessCutoverReceiptPointer
    activation_binding: BusinessCutoverActivationEvidence
    audit_certificate_id: str
    audit_certificate_sequence: int
    audit: BusinessCutoverReceiptPointer
    audit_certificate_tail_sha256: bytes = field(repr=False)
    audit_completed_at: str
    activated_at: str
    target_schema_version: int
    target_schema_fingerprint: str

    def __post_init__(self) -> None:
        for name in ("mount", "install", "activation", "audit"):
            if type(getattr(self, name)) is not BusinessCutoverReceiptPointer:
                _fail("business_cutover_phase_binding_mount_invalid", "$.mount." + name)
        if type(self.coverage) is not BusinessCutoverAssetCoverageCheckpoint:
            _fail("business_cutover_phase_binding_mount_invalid", "$.mount.coverage")
        if type(self.activation_binding) is not BusinessCutoverActivationEvidence:
            _fail(
                "business_cutover_phase_binding_mount_invalid",
                "$.mount.activation_binding",
            )
        _digest(self.source_admission_sha256, "$.mount.source_admission_sha256")
        _digest(self.final_admission_sha256, "$.mount.final_admission_sha256")
        _lower_hex(self.audit_certificate_id, "$.mount.audit_certificate_id", 32)
        _integer(self.audit_certificate_sequence, "$.mount.audit_certificate_sequence", minimum=1)
        _digest(self.audit_certificate_tail_sha256, "$.mount.audit_certificate_tail_sha256", allow_zero=False)
        _timestamp(self.audit_completed_at, "$.mount.audit_completed_at")
        _timestamp(self.activated_at, "$.mount.activated_at")
        _integer(self.target_schema_version, "$.mount.target_schema_version", minimum=1)
        _lower_hex(self.target_schema_fingerprint, "$.mount.target_schema_fingerprint", 64)
        if not (
            self.coverage.pointer.committed_at <= self.install.committed_at
            <= self.audit_completed_at == self.audit.committed_at
            <= self.activated_at == self.activation.committed_at
            <= self.mount.committed_at
            and self.activation == self.activation_binding.activation
            and self.install == self.activation_binding.install
            and self.audit_certificate_id
            == self.activation_binding.audit_certificate_id
            and self.audit_certificate_sequence
            == self.activation_binding.audit_certificate_sequence
            and self.audit == self.activation_binding.audit
            and hmac.compare_digest(
                self.audit_certificate_tail_sha256,
                self.activation_binding.audit_certificate_tail_sha256,
            )
            and self.audit_completed_at
            == self.activation_binding.audit_completed_at
            and self.activated_at
            == self.activation_binding.activation.committed_at
            and self.target_schema_version
            == self.activation_binding.actual_schema_version
            and self.target_schema_fingerprint
            == self.activation_binding.actual_schema_fingerprint
            and hmac.compare_digest(
                self.final_admission_sha256,
                self.activation_binding.final_admission_sha256,
            )
        ):
            _fail("business_cutover_phase_binding_mount_invalid", "$.mount.chronology")


BusinessCutoverPhaseDirectEvidence = Union[
    BusinessCutoverAssetProviderEvidence,
    BusinessCutoverAuditEvidence,
    BusinessCutoverActivationEvidence,
    BusinessCutoverMountEvidence,
]

_STAGE_EVIDENCE_TYPES = {
    BusinessCutoverPhaseBindingStage.ASSET_PROVIDER: BusinessCutoverAssetProviderEvidence,
    BusinessCutoverPhaseBindingStage.AUDIT: BusinessCutoverAuditEvidence,
    BusinessCutoverPhaseBindingStage.ACTIVATION: BusinessCutoverActivationEvidence,
    BusinessCutoverPhaseBindingStage.MOUNT: BusinessCutoverMountEvidence,
}

_INSTALL_PREFIX = "_business-cutover-install:"
_INSTALL_KIND = "_business_cutover_install_v1"
_COVERAGE_PREFIX = "_business-cutover-asset-coverage:"
_COVERAGE_KIND = "_business_cutover_asset_coverage_v1"
_AUDIT_PREFIX = "_repository-audit:"
_AUDIT_KIND = "_repository_audit_certificate_v1"
_ACTIVATION_PREFIX = "_business-cutover-activation:"
_ACTIVATION_KIND = "_business_cutover_activation_v2"
_MOUNT_PREFIX = "_business-cutover-mount:"
_MOUNT_KIND = "_business_cutover_mount_v2"


def _pointer_matches(
    pointer: BusinessCutoverReceiptPointer,
    prefix: str,
    suffix: str,
    kind: str,
) -> bool:
    return bool(
        pointer.transaction_id == prefix + suffix
        and pointer.receipt_kind == kind
    )


def _head_pointers_match_identity(
    head: BusinessCutoverPhaseBindingHead,
    cutover_id: str,
) -> bool:
    if not _pointer_matches(head.install, _INSTALL_PREFIX, cutover_id, _INSTALL_KIND):
        return False
    if head.coverage is not None and not _pointer_matches(
        head.coverage,
        _COVERAGE_PREFIX,
        cutover_id,
        _COVERAGE_KIND,
    ):
        return False
    if head.activation is not None and not _pointer_matches(
        head.activation,
        _ACTIVATION_PREFIX,
        cutover_id,
        _ACTIVATION_KIND,
    ):
        return False
    if head.mount is not None and not _pointer_matches(
        head.mount,
        _MOUNT_PREFIX,
        cutover_id,
        _MOUNT_KIND,
    ):
        return False
    if head.audit is not None:
        prefix_bytes = len(_AUDIT_PREFIX)
        certificate_id = head.audit.transaction_id[prefix_bytes:]
        if (
            len(certificate_id) != 32
            or any(character not in _LOWER_HEX for character in certificate_id)
            or not _pointer_matches(
                head.audit,
                _AUDIT_PREFIX,
                certificate_id,
                _AUDIT_KIND,
            )
        ):
            return False
    return True


def _head_is_canonical(
    head: BusinessCutoverPhaseBindingHead,
    identity: BusinessCutoverPhaseBindingIdentity,
) -> bool:
    """Rebuild the frozen canonical phase-head digest, not only its shape."""

    by_role = (
        (BusinessCutoverHeadPointerRole.INSTALL, head.install),
        (BusinessCutoverHeadPointerRole.COVERAGE, head.coverage),
        (BusinessCutoverHeadPointerRole.AUDIT, head.audit),
        (BusinessCutoverHeadPointerRole.ACTIVATION, head.activation),
        (BusinessCutoverHeadPointerRole.MOUNT, head.mount),
    )
    try:
        pointers = tuple(
            BusinessCutoverHeadPointer(
                role=role,
                transaction_id=pointer.transaction_id,
                receipt_kind=pointer.receipt_kind,
                occurred_at=pointer.committed_at,
                manifest_sha256=pointer.manifest_sha256,
            )
            for role, pointer in by_role
            if pointer is not None
        )
        BusinessCutoverHeadState(
            cutover_id=identity.cutover_id,
            state_revision=head.state_revision,
            phase_revision=head.phase_revision,
            phase=head.phase,
            authority_state=head.authority_state,
            repository_epoch=identity.repository_epoch,
            runtime_generation=identity.runtime_generation,
            code_digest=identity.code_digest,
            pointers=pointers,
            previous_head_sha256=head.previous_head_sha256,
            head_sha256=head.head_sha256,
            updated_at=head.updated_at,
        )
    except (BusinessCutoverHeadIntegrityError, TypeError, ValueError):
        return False
    return True


@dataclass(frozen=True)
class BusinessCutoverPhaseBindingProjection:
    stage: BusinessCutoverPhaseBindingStage
    identity: BusinessCutoverPhaseBindingIdentity
    old_head: BusinessCutoverPhaseBindingHead
    new_head: BusinessCutoverPhaseBindingHead
    admission: BusinessCutoverAdmissionCheckpoint
    maintenance: BusinessCutoverMaintenanceCheckpoint
    checkpoint: BusinessCutoverDurableCheckpoint
    direct_evidence: BusinessCutoverPhaseDirectEvidence

    def __post_init__(self) -> None:
        if type(self.stage) is not BusinessCutoverPhaseBindingStage:
            _fail("business_cutover_phase_binding_stage_invalid", "$.stage")
        if type(self.identity) is not BusinessCutoverPhaseBindingIdentity:
            _fail("business_cutover_phase_binding_identity_invalid", "$.identity")
        if type(self.old_head) is not BusinessCutoverPhaseBindingHead or type(self.new_head) is not BusinessCutoverPhaseBindingHead:
            _fail("business_cutover_phase_binding_head_invalid", "$.head")
        if type(self.admission) is not BusinessCutoverAdmissionCheckpoint:
            _fail("business_cutover_phase_binding_admission_invalid", "$.admission")
        if type(self.maintenance) is not BusinessCutoverMaintenanceCheckpoint:
            _fail("business_cutover_phase_binding_maintenance_invalid", "$.maintenance")
        if type(self.checkpoint) is not BusinessCutoverDurableCheckpoint:
            _fail("business_cutover_phase_binding_checkpoint_invalid", "$.checkpoint")
        if type(self.direct_evidence) is not _STAGE_EVIDENCE_TYPES[self.stage]:
            _fail("business_cutover_phase_binding_evidence_invalid", "$.direct_evidence")
        old_revision, old_phase, old_authority, new_revision, new_phase, new_authority, file_state = _STAGE_TRANSITIONS[self.stage]
        if (
            (self.old_head.phase_revision, self.old_head.phase, self.old_head.authority_state)
            != (old_revision, old_phase, old_authority)
            or (self.new_head.phase_revision, self.new_head.phase, self.new_head.authority_state)
            != (new_revision, new_phase, new_authority)
            or not hmac.compare_digest(self.new_head.previous_head_sha256, self.old_head.head_sha256)
            or not _head_pointers_match_identity(
                self.old_head,
                self.identity.cutover_id,
            )
            or not _head_pointers_match_identity(
                self.new_head,
                self.identity.cutover_id,
            )
            or not _head_is_canonical(self.old_head, self.identity)
            or not _head_is_canonical(self.new_head, self.identity)
            or self.new_head.install != self.old_head.install
            or self.admission.file_authority_state != file_state
            or self.admission.file_cutover_id != self.identity.cutover_id
            or self.admission.file_authority_epoch != self.identity.repository_epoch
            or self.admission.file_revision != self.admission.source_revision + (
                1 if self.stage in (
                    BusinessCutoverPhaseBindingStage.ASSET_PROVIDER,
                    BusinessCutoverPhaseBindingStage.AUDIT,
                ) else 2
            )
            or self.checkpoint.runtime.source_admission_revision != self.admission.source_revision
            or self.checkpoint.runtime.authority_state != old_authority
            or self.maintenance.observed_at < self.old_head.updated_at
            or self.new_head.updated_at < self.old_head.updated_at
            or self.new_head.updated_at > self.maintenance.observed_at
            or self.checkpoint.runtime.updated_at > self.new_head.updated_at
            or self.checkpoint.worker.updated_at > self.old_head.updated_at
            or self.checkpoint.allocation.updated_at > self.old_head.updated_at
            or self.checkpoint.dispatch.updated_at > self.old_head.updated_at
            or self.checkpoint.receipt_after_count
            != self.checkpoint.receipt_before_count + (
                0 if self.stage is BusinessCutoverPhaseBindingStage.ASSET_PROVIDER else 1
            )
        ):
            _fail("business_cutover_phase_binding_transition_invalid", "$.transition")
        evidence = self.direct_evidence
        cutover_id = self.identity.cutover_id
        if self.stage is BusinessCutoverPhaseBindingStage.ASSET_PROVIDER:
            direct_valid = bool(
                _pointer_matches(
                    evidence.coverage.pointer,
                    _COVERAGE_PREFIX,
                    cutover_id,
                    _COVERAGE_KIND,
                )
                and self.old_head.coverage is None
                and self.new_head.coverage == evidence.coverage.pointer
                and evidence.coverage.pointer.committed_at <= self.old_head.updated_at
                and evidence.checked_at >= self.old_head.updated_at
                and self.new_head.updated_at == self.old_head.updated_at
            )
        elif self.stage is BusinessCutoverPhaseBindingStage.AUDIT:
            direct_valid = bool(
                _pointer_matches(
                    evidence.receipt_parent,
                    _AUDIT_PREFIX,
                    evidence.certificate_id,
                    _AUDIT_KIND,
                )
                and self.new_head.coverage == self.old_head.coverage
                and self.old_head.audit is None
                and self.new_head.audit == evidence.receipt_parent
                and hmac.compare_digest(
                    self.new_head.install.manifest_sha256,
                    evidence.install_manifest_sha256,
                )
                and self.new_head.updated_at == evidence.completed_at
                and evidence.started_at >= self.checkpoint.runtime.updated_at
                and evidence.covered_business_revision
                == self.checkpoint.runtime.repository_revision
            )
        elif self.stage is BusinessCutoverPhaseBindingStage.ACTIVATION:
            direct_valid = bool(
                _pointer_matches(evidence.activation, _ACTIVATION_PREFIX, cutover_id, _ACTIVATION_KIND)
                and _pointer_matches(evidence.install, _INSTALL_PREFIX, cutover_id, _INSTALL_KIND)
                and _pointer_matches(evidence.audit, _AUDIT_PREFIX, evidence.audit_certificate_id, _AUDIT_KIND)
                and self.new_head.coverage == self.old_head.coverage
                and self.new_head.audit == self.old_head.audit == evidence.audit
                and self.new_head.install == evidence.install
                and self.old_head.activation is None
                and self.new_head.activation == evidence.activation
                and self.new_head.updated_at == evidence.activation.committed_at
                and evidence.before_runtime == self.checkpoint.runtime
                and evidence.after_runtime.updated_at == self.new_head.updated_at
                and hmac.compare_digest(
                    evidence.final_admission_sha256,
                    self.admission.file_admission_sha256,
                )
                and evidence.final_admission_state == self.admission.file_authority_state
                and evidence.final_admission_epoch == self.admission.file_authority_epoch
                and evidence.final_admission_revision == self.admission.file_revision
                and evidence.final_admission_cutover_id == self.admission.file_cutover_id
            )
        else:
            direct_valid = bool(
                _pointer_matches(evidence.mount, _MOUNT_PREFIX, cutover_id, _MOUNT_KIND)
                and _pointer_matches(evidence.install, _INSTALL_PREFIX, cutover_id, _INSTALL_KIND)
                and _pointer_matches(evidence.coverage.pointer, _COVERAGE_PREFIX, cutover_id, _COVERAGE_KIND)
                and _pointer_matches(evidence.activation, _ACTIVATION_PREFIX, cutover_id, _ACTIVATION_KIND)
                and _pointer_matches(evidence.audit, _AUDIT_PREFIX, evidence.audit_certificate_id, _AUDIT_KIND)
                and self.new_head.install == self.old_head.install == evidence.install
                and self.new_head.coverage == self.old_head.coverage == evidence.coverage.pointer
                and self.new_head.audit == self.old_head.audit == evidence.audit
                and self.new_head.activation == self.old_head.activation == evidence.activation
                and self.old_head.mount is None
                and self.new_head.mount == evidence.mount
                and self.new_head.updated_at == evidence.mount.committed_at
                and hmac.compare_digest(
                    evidence.source_admission_sha256,
                    self.admission.source_admission_sha256,
                )
                and hmac.compare_digest(
                    evidence.final_admission_sha256,
                    self.admission.file_admission_sha256,
                )
                and evidence.target_schema_version == 13
            )
        if not direct_valid:
            _fail("business_cutover_phase_binding_direct_evidence_invalid", "$.direct_evidence")


@dataclass(frozen=True)
class BusinessCutoverPhaseBinding:
    projection: BusinessCutoverPhaseBindingProjection
    binding_domain: str
    manifest_bytes: bytes = field(repr=False)
    manifest_sha256: bytes = field(repr=False)
    protocol: str = BUSINESS_CUTOVER_PHASE_BINDING_PROTOCOL
    status: str = BUSINESS_CUTOVER_PHASE_BINDING_STATUS
    _issuer: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._issuer is not _BINDING_ISSUER:
            _fail("business_cutover_phase_binding_unissued", "$.binding")
        if type(self.projection) is not BusinessCutoverPhaseBindingProjection:
            _fail("business_cutover_phase_binding_projection_invalid", "$.binding.projection")
        if self.binding_domain != BUSINESS_CUTOVER_PHASE_BINDING_DOMAINS[self.projection.stage]:
            _fail("business_cutover_phase_binding_domain_invalid", "$.binding.binding_domain")
        if type(self.manifest_bytes) is not bytes or not 1 <= len(self.manifest_bytes) <= MAX_BUSINESS_CUTOVER_PHASE_BINDING_MANIFEST_BYTES:
            _fail("business_cutover_phase_binding_manifest_invalid", "$.binding.manifest_bytes")
        _digest(self.manifest_sha256, "$.binding.manifest_sha256", allow_zero=False)
        if self.protocol != BUSINESS_CUTOVER_PHASE_BINDING_PROTOCOL or self.status != BUSINESS_CUTOVER_PHASE_BINDING_STATUS:
            _fail("business_cutover_phase_binding_protocol_invalid", "$.binding")


__all__ = ()
