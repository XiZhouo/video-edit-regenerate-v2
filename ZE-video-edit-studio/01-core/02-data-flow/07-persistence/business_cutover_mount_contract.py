"""Pure values for the final cutover production-mount authorization.

This protocol composes, but does not replace, the frozen cutover-v1, asset
coverage-v1, and activation-v2 protocols.  A mount row is the final durable
statement that the exact installed repository, complete asset closure, and
audited activation all name one repository identity.

The values in this module perform no I/O and grant no authority by ordinary
construction.  A plan remains an expectation.  Only a durable-reader stamped
observation may be reconciled as persisted mount evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from business_cutover_activation_contract import (
    BUSINESS_CUTOVER_ACTIVATION_TRANSACTION_KIND,
    BUSINESS_CUTOVER_ACTIVATION_TRANSACTION_PREFIX,
)
from business_cutover_asset_contract import (
    BUSINESS_CUTOVER_ASSET_COVERAGE_TRANSACTION_KIND,
    BUSINESS_CUTOVER_ASSET_COVERAGE_TRANSACTION_PREFIX,
)
from business_cutover_contract import (
    BUSINESS_CUTOVER_INSTALL_TRANSACTION_KIND,
    BUSINESS_CUTOVER_INSTALL_TRANSACTION_PREFIX,
)
from repository_audit_contract import (
    REPOSITORY_AUDIT_TRANSACTION_KIND,
    REPOSITORY_AUDIT_TRANSACTION_PREFIX,
)


BUSINESS_CUTOVER_MOUNT_PROTOCOL = "ze.business-cutover.mount-authorization.v2"
BUSINESS_CUTOVER_MOUNT_MANIFEST_SCHEMA = (
    "ze.business-cutover.mount-authorization-manifest.v2"
)
BUSINESS_CUTOVER_MOUNT_PLAN_STATUS = "prepared"
BUSINESS_CUTOVER_MOUNT_STATUS = "mounted_exact"
BUSINESS_CUTOVER_MOUNT_TRANSACTION_PREFIX = "_business-cutover-mount:"
BUSINESS_CUTOVER_MOUNT_TRANSACTION_KIND = "_business_cutover_mount_v2"

MAX_BUSINESS_CUTOVER_MOUNT_TEXT_BYTES = 1024 * 1024
MAX_BUSINESS_CUTOVER_MOUNT_RUNTIME_GENERATION_BYTES = 256
MAX_BUSINESS_CUTOVER_MOUNT_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_BUSINESS_CUTOVER_MOUNT_MANIFEST_NODES = 100_000
MAX_SQLITE_INTEGER = 2**63 - 1

_LOWER_HEX = frozenset("0123456789abcdef")
_DURABLE_READER_CAPABILITY_ISSUER = object()
_FRESH_OBSERVATION_ISSUER = object()


class BusinessCutoverMountContractError(ValueError):
    """Stable, payload-free failure at the mount contract boundary."""

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
    raise BusinessCutoverMountContractError(code, path)


def _text(
    value: Any,
    path: str,
    *,
    maximum: int = MAX_BUSINESS_CUTOVER_MOUNT_TEXT_BYTES,
) -> str:
    if type(value) is not str or not value:
        _fail("business_cutover_mount_text_invalid", path)
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeEncodeError:
        _fail("business_cutover_mount_text_invalid", path)
        raise AssertionError("unreachable")
    if len(encoded) > maximum:
        _fail("business_cutover_mount_text_budget_exceeded", path)
    return value


def _integer(
    value: Any,
    path: str,
    *,
    minimum: int = 0,
    maximum: int = MAX_SQLITE_INTEGER,
) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _fail("business_cutover_mount_integer_invalid", path)
    return value


def _lower_hex(value: Any, path: str, length: int) -> str:
    if (
        type(value) is not str
        or len(value) != length
        or any(character not in _LOWER_HEX for character in value)
    ):
        _fail("business_cutover_mount_hex_invalid", path)
    return value


def _digest(value: Any, path: str) -> bytes:
    if type(value) is not bytes or len(value) != 32:
        _fail("business_cutover_mount_digest_invalid", path)
    return value


def _timestamp(value: Any, path: str) -> str:
    if type(value) is not str or len(value) != 24:
        _fail("business_cutover_mount_timestamp_invalid", path)
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except (TypeError, ValueError):
        _fail("business_cutover_mount_timestamp_invalid", path)
        raise AssertionError("unreachable")
    canonical = parsed.strftime("%Y-%m-%dT%H:%M:%S.") + (
        "%03dZ" % (parsed.microsecond // 1000)
    )
    if canonical != value:
        _fail("business_cutover_mount_timestamp_invalid", path)
    return value


class BusinessCutoverMountPersistedReadStatus(str, Enum):
    COMPLETE = "complete"
    UNKNOWN = "unknown"


class BusinessCutoverMountReconcileStatus(str, Enum):
    ABSENT = "absent"
    EXACT = "exact"
    CONFLICT = "conflict"
    CORRUPT = "corrupt"
    UNKNOWN = "unknown"


class BusinessCutoverMountRetryAction(str, Enum):
    APPLY_EXACT = "apply_exact"
    RETURN_EXACT = "return_exact"
    STOP = "stop"
    RECONCILE_REQUIRED = "reconcile_required"


@dataclass(frozen=True)
class BusinessCutoverMountRuntimeIdentity:
    authority: str
    authority_state: str
    repository_epoch: str
    source_admission_revision: int
    repository_revision: int
    state_revision: int
    runtime_generation: str
    code_digest: bytes = field(repr=False)
    runtime_schema_version: int = 0
    target_schema_version: int = 0
    target_schema_fingerprint: str = ""

    def __post_init__(self) -> None:
        _text(self.authority, "$.runtime.authority")
        _text(self.authority_state, "$.runtime.authority_state")
        _lower_hex(self.repository_epoch, "$.runtime.repository_epoch", 64)
        _integer(
            self.source_admission_revision,
            "$.runtime.source_admission_revision",
            minimum=1,
        )
        _integer(
            self.repository_revision,
            "$.runtime.repository_revision",
            minimum=1,
        )
        _integer(self.state_revision, "$.runtime.state_revision", minimum=1)
        _text(
            self.runtime_generation,
            "$.runtime.runtime_generation",
            maximum=MAX_BUSINESS_CUTOVER_MOUNT_RUNTIME_GENERATION_BYTES,
        )
        _digest(self.code_digest, "$.runtime.code_digest")
        _integer(
            self.runtime_schema_version,
            "$.runtime.runtime_schema_version",
            minimum=1,
        )
        _integer(
            self.target_schema_version,
            "$.runtime.target_schema_version",
            minimum=1,
        )
        _lower_hex(
            self.target_schema_fingerprint,
            "$.runtime.target_schema_fingerprint",
            64,
        )


@dataclass(frozen=True)
class BusinessCutoverMountInstallBinding:
    transaction_id: str
    receipt_kind: str
    committed_at: str
    manifest_sha256: bytes = field(repr=False)

    def __post_init__(self) -> None:
        _text(self.transaction_id, "$.install.transaction_id")
        _text(self.receipt_kind, "$.install.receipt_kind")
        _timestamp(self.committed_at, "$.install.committed_at")
        _digest(self.manifest_sha256, "$.install.manifest_sha256")


@dataclass(frozen=True)
class BusinessCutoverMountCoverageBinding:
    transaction_id: str
    receipt_kind: str
    committed_at: str
    manifest_sha256: bytes = field(repr=False)
    coverage_sha256: bytes = field(repr=False)
    member_root_sha256: bytes = field(repr=False)
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
        _text(self.transaction_id, "$.coverage.transaction_id")
        _text(self.receipt_kind, "$.coverage.receipt_kind")
        _timestamp(self.committed_at, "$.coverage.committed_at")
        _digest(self.manifest_sha256, "$.coverage.manifest_sha256")
        _digest(self.coverage_sha256, "$.coverage.coverage_sha256")
        _digest(self.member_root_sha256, "$.coverage.member_root_sha256")
        for name in (
            "batch_count",
            "repository_member_count",
            "live_project_count",
            "deleted_project_count",
            "project_count",
            "reference_count",
            "unique_blob_count",
            "unique_blob_bytes",
            "referenced_bytes",
            "observed_occurrence_count",
            "observed_row_count",
            "observed_manifest_bytes",
        ):
            _integer(getattr(self, name), "$.coverage." + name)
        if (
            self.repository_member_count
            != self.live_project_count + self.deleted_project_count
            or self.project_count != self.live_project_count
            or self.unique_blob_count > self.reference_count
            or self.unique_blob_bytes > self.referenced_bytes
            or self.observed_occurrence_count < self.reference_count
            or self.observed_row_count < self.observed_occurrence_count
            or (self.batch_count == 0) != (self.reference_count == 0)
            or (self.batch_count == 0) != (self.observed_manifest_bytes == 0)
        ):
            _fail(
                "business_cutover_mount_coverage_counts_invalid",
                "$.coverage",
            )


@dataclass(frozen=True)
class BusinessCutoverMountActivationBinding:
    transaction_id: str
    receipt_kind: str
    manifest_sha256: bytes = field(repr=False)
    audit_certificate_id: str = ""
    audit_certificate_sequence: int = 0
    audit_transaction_id: str = ""
    audit_receipt_kind: str = ""
    audit_manifest_sha256: bytes = field(default=b"", repr=False)
    audit_certificate_tail_sha256: bytes = field(default=b"", repr=False)
    expected_migration_ledger_count: int = 0
    expected_migration_ledger_sha256: bytes = field(default=b"", repr=False)
    audit_completed_at: str = ""
    activated_at: str = ""

    def __post_init__(self) -> None:
        _text(self.transaction_id, "$.activation.transaction_id")
        _text(self.receipt_kind, "$.activation.receipt_kind")
        _digest(self.manifest_sha256, "$.activation.manifest_sha256")
        _lower_hex(
            self.audit_certificate_id,
            "$.activation.audit_certificate_id",
            32,
        )
        _integer(
            self.audit_certificate_sequence,
            "$.activation.audit_certificate_sequence",
            minimum=1,
        )
        _text(self.audit_transaction_id, "$.activation.audit_transaction_id")
        _text(self.audit_receipt_kind, "$.activation.audit_receipt_kind")
        _digest(
            self.audit_manifest_sha256,
            "$.activation.audit_manifest_sha256",
        )
        _digest(
            self.audit_certificate_tail_sha256,
            "$.activation.audit_certificate_tail_sha256",
        )
        _integer(
            self.expected_migration_ledger_count,
            "$.activation.expected_migration_ledger_count",
            minimum=1,
        )
        _digest(
            self.expected_migration_ledger_sha256,
            "$.activation.expected_migration_ledger_sha256",
        )
        _timestamp(self.audit_completed_at, "$.activation.audit_completed_at")
        _timestamp(self.activated_at, "$.activation.activated_at")
        if self.audit_completed_at > self.activated_at:
            _fail(
                "business_cutover_mount_activation_time_invalid",
                "$.activation.audit_completed_at",
            )


@dataclass(frozen=True)
class BusinessCutoverMountGenericReceiptEvidence:
    transaction_id: str
    kind: str
    committed_at: str

    def __post_init__(self) -> None:
        _text(self.transaction_id, "$.receipt.transaction_id")
        _text(self.kind, "$.receipt.kind")
        _timestamp(self.committed_at, "$.receipt.committed_at")


@dataclass(frozen=True)
class BusinessCutoverMountEvidence:
    cutover_id: str
    receipt_parent: BusinessCutoverMountGenericReceiptEvidence
    runtime: BusinessCutoverMountRuntimeIdentity
    source_admission_sha256: bytes = field(repr=False)
    final_admission_sha256: bytes = field(repr=False)
    install: BusinessCutoverMountInstallBinding
    coverage: BusinessCutoverMountCoverageBinding
    activation: BusinessCutoverMountActivationBinding
    mounted_at: str
    manifest_bytes: bytes = field(repr=False)
    manifest_sha256: bytes = field(repr=False)
    protocol: str = BUSINESS_CUTOVER_MOUNT_PROTOCOL
    status: str = BUSINESS_CUTOVER_MOUNT_STATUS

    def __post_init__(self) -> None:
        _lower_hex(self.cutover_id, "$.mount.cutover_id", 64)
        if type(self.receipt_parent) is not BusinessCutoverMountGenericReceiptEvidence:
            _fail("business_cutover_mount_receipt_invalid", "$.mount.receipt_parent")
        if (
            self.receipt_parent.transaction_id
            != BUSINESS_CUTOVER_MOUNT_TRANSACTION_PREFIX + self.cutover_id
            or self.receipt_parent.kind != BUSINESS_CUTOVER_MOUNT_TRANSACTION_KIND
        ):
            _fail(
                "business_cutover_mount_receipt_binding_invalid",
                "$.mount.receipt_parent",
            )
        if type(self.runtime) is not BusinessCutoverMountRuntimeIdentity:
            _fail("business_cutover_mount_runtime_invalid", "$.mount.runtime")
        if type(self.install) is not BusinessCutoverMountInstallBinding:
            _fail("business_cutover_mount_install_invalid", "$.mount.install")
        if type(self.coverage) is not BusinessCutoverMountCoverageBinding:
            _fail("business_cutover_mount_coverage_invalid", "$.mount.coverage")
        if type(self.activation) is not BusinessCutoverMountActivationBinding:
            _fail("business_cutover_mount_activation_invalid", "$.mount.activation")
        if (
            self.install.transaction_id
            != BUSINESS_CUTOVER_INSTALL_TRANSACTION_PREFIX + self.cutover_id
            or self.install.receipt_kind != BUSINESS_CUTOVER_INSTALL_TRANSACTION_KIND
            or self.coverage.transaction_id
            != BUSINESS_CUTOVER_ASSET_COVERAGE_TRANSACTION_PREFIX + self.cutover_id
            or self.coverage.receipt_kind
            != BUSINESS_CUTOVER_ASSET_COVERAGE_TRANSACTION_KIND
            or self.activation.transaction_id
            != BUSINESS_CUTOVER_ACTIVATION_TRANSACTION_PREFIX + self.cutover_id
            or self.activation.receipt_kind
            != BUSINESS_CUTOVER_ACTIVATION_TRANSACTION_KIND
            or self.activation.audit_transaction_id
            != REPOSITORY_AUDIT_TRANSACTION_PREFIX
            + self.activation.audit_certificate_id
            or self.activation.audit_receipt_kind
            != REPOSITORY_AUDIT_TRANSACTION_KIND
            or self.activation.expected_migration_ledger_count
            != self.runtime.target_schema_version
        ):
            _fail(
                "business_cutover_mount_component_binding_invalid",
                "$.mount",
            )
        _digest(
            self.source_admission_sha256,
            "$.mount.source_admission_sha256",
        )
        _digest(
            self.final_admission_sha256,
            "$.mount.final_admission_sha256",
        )
        _timestamp(self.mounted_at, "$.mount.mounted_at")
        if (
            self.receipt_parent.committed_at != self.mounted_at
            or self.coverage.committed_at > self.install.committed_at
            or self.install.committed_at > self.activation.audit_completed_at
            or self.mounted_at < self.activation.activated_at
        ):
            _fail("business_cutover_mount_time_invalid", "$.mount.mounted_at")
        if (
            type(self.manifest_bytes) is not bytes
            or not 1 <= len(self.manifest_bytes) <= MAX_BUSINESS_CUTOVER_MOUNT_MANIFEST_BYTES
        ):
            _fail("business_cutover_mount_manifest_invalid", "$.mount.manifest_bytes")
        _digest(self.manifest_sha256, "$.mount.manifest_sha256")
        if self.protocol != BUSINESS_CUTOVER_MOUNT_PROTOCOL:
            _fail("business_cutover_mount_protocol_invalid", "$.mount.protocol")
        if self.status != BUSINESS_CUTOVER_MOUNT_STATUS:
            _fail("business_cutover_mount_status_invalid", "$.mount.status")

    @property
    def transaction_id(self) -> str:
        return self.receipt_parent.transaction_id

    @property
    def receipt_kind(self) -> str:
        return self.receipt_parent.kind


@dataclass(frozen=True)
class BusinessCutoverMountPersistedObservation:
    read_status: BusinessCutoverMountPersistedReadStatus
    receipt: Optional[BusinessCutoverMountGenericReceiptEvidence] = None
    mount: Optional[BusinessCutoverMountEvidence] = None
    error_code: str = ""

    def __post_init__(self) -> None:
        if type(self.read_status) is not BusinessCutoverMountPersistedReadStatus:
            _fail("business_cutover_mount_persisted_read_invalid", "$.persisted")
        if self.read_status is BusinessCutoverMountPersistedReadStatus.UNKNOWN:
            if (
                self.receipt is not None
                or self.mount is not None
                or type(self.error_code) is not str
                or not self.error_code
            ):
                _fail("business_cutover_mount_persisted_read_invalid", "$.persisted")
            _text(self.error_code, "$.persisted.error_code", maximum=1024)
            return
        if self.error_code != "":
            _fail(
                "business_cutover_mount_persisted_read_invalid",
                "$.persisted.error_code",
            )
        if self.receipt is not None and type(
            self.receipt
        ) is not BusinessCutoverMountGenericReceiptEvidence:
            _fail("business_cutover_mount_persisted_read_invalid", "$.persisted.receipt")
        if self.mount is not None and type(self.mount) is not BusinessCutoverMountEvidence:
            _fail("business_cutover_mount_persisted_read_invalid", "$.persisted.mount")


class BusinessCutoverMountDurableReaderCapability:
    """Trusted-process authority held only by the fixed durable mount reader."""

    __slots__ = ("_issuer",)

    def __init__(self, *, _issuer: Any) -> None:
        if _issuer is not _DURABLE_READER_CAPABILITY_ISSUER:
            _fail("business_cutover_mount_reader_invalid", "$.reader")
        object.__setattr__(self, "_issuer", _issuer)

    def __setattr__(self, name: str, value: Any) -> None:
        del name, value
        raise AttributeError("BusinessCutoverMountDurableReaderCapability is immutable")


_BUSINESS_CUTOVER_MOUNT_DURABLE_READER_CAPABILITY = (
    BusinessCutoverMountDurableReaderCapability(
        _issuer=_DURABLE_READER_CAPABILITY_ISSUER
    )
)


class BusinessCutoverMountFreshObservation:
    """Capability-stamped exact-id durable observation for reconciliation."""

    __slots__ = ("persisted", "_reader", "_issuer")

    def __init__(
        self,
        persisted: BusinessCutoverMountPersistedObservation,
        *,
        _reader: BusinessCutoverMountDurableReaderCapability,
        _issuer: Any,
    ) -> None:
        if (
            _issuer is not _FRESH_OBSERVATION_ISSUER
            or _reader is not _BUSINESS_CUTOVER_MOUNT_DURABLE_READER_CAPABILITY
            or type(persisted) is not BusinessCutoverMountPersistedObservation
        ):
            _fail("business_cutover_mount_fresh_observation_invalid", "$.fresh")
        object.__setattr__(self, "persisted", persisted)
        object.__setattr__(self, "_reader", _reader)
        object.__setattr__(self, "_issuer", _issuer)

    def __setattr__(self, name: str, value: Any) -> None:
        del name, value
        raise AttributeError("BusinessCutoverMountFreshObservation is immutable")


def _stamp_business_cutover_mount_fresh_observation(
    reader: BusinessCutoverMountDurableReaderCapability,
    persisted: BusinessCutoverMountPersistedObservation,
) -> BusinessCutoverMountFreshObservation:
    if reader is not _BUSINESS_CUTOVER_MOUNT_DURABLE_READER_CAPABILITY:
        _fail("business_cutover_mount_reader_invalid", "$.reader")
    return BusinessCutoverMountFreshObservation(
        persisted,
        _reader=reader,
        _issuer=_FRESH_OBSERVATION_ISSUER,
    )


def _unwrap_business_cutover_mount_fresh_observation(
    value: Any,
) -> BusinessCutoverMountPersistedObservation:
    if (
        type(value) is not BusinessCutoverMountFreshObservation
        or getattr(value, "_reader", None)
        is not _BUSINESS_CUTOVER_MOUNT_DURABLE_READER_CAPABILITY
        or getattr(value, "_issuer", None) is not _FRESH_OBSERVATION_ISSUER
        or type(getattr(value, "persisted", None))
        is not BusinessCutoverMountPersistedObservation
    ):
        _fail("business_cutover_mount_fresh_observation_invalid", "$.fresh")
    return value.persisted


@dataclass(frozen=True)
class BusinessCutoverMountClassification:
    status: BusinessCutoverMountReconcileStatus
    cutover_id: str
    evidence: Optional[BusinessCutoverMountEvidence] = None
    error_code: str = ""

    def __post_init__(self) -> None:
        if type(self.status) is not BusinessCutoverMountReconcileStatus:
            _fail("business_cutover_mount_classification_invalid", "$.classification")
        _lower_hex(self.cutover_id, "$.classification.cutover_id", 64)
        exact = self.status is BusinessCutoverMountReconcileStatus.EXACT
        failed = self.status in (
            BusinessCutoverMountReconcileStatus.CONFLICT,
            BusinessCutoverMountReconcileStatus.CORRUPT,
            BusinessCutoverMountReconcileStatus.UNKNOWN,
        )
        if (
            (exact and type(self.evidence) is not BusinessCutoverMountEvidence)
            or (not exact and self.evidence is not None)
            or (
                exact
                and self.evidence is not None
                and self.evidence.cutover_id != self.cutover_id
            )
            or (failed and (type(self.error_code) is not str or not self.error_code))
            or (not failed and self.error_code != "")
        ):
            _fail("business_cutover_mount_classification_invalid", "$.classification")

    @property
    def retry_action(self) -> BusinessCutoverMountRetryAction:
        if self.status is BusinessCutoverMountReconcileStatus.ABSENT:
            return BusinessCutoverMountRetryAction.APPLY_EXACT
        if self.status is BusinessCutoverMountReconcileStatus.EXACT:
            return BusinessCutoverMountRetryAction.RETURN_EXACT
        if self.status is BusinessCutoverMountReconcileStatus.UNKNOWN:
            return BusinessCutoverMountRetryAction.RECONCILE_REQUIRED
        return BusinessCutoverMountRetryAction.STOP


__all__ = ()
