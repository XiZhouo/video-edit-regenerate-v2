"""Pure values for one durable repository full-audit certificate.

This module does not open SQLite, inspect a path, choose a clock, invoke a
callback, sign content, or claim that a certificate was persisted.  A storage
adapter must obtain every :class:`RepositoryAuditObservedInput` field from one
exact, transaction-scoped full-audit snapshot.  In particular it must project
the exact ``BusinessRuntimeProfile`` returned by the runtime validator, prove
the actual schema and migration ledger, scan the complete receipt namespace,
and load the exact durable predecessor rather than constructing one from
untrusted caller data.

The resulting evidence is corruption-evident under the trusted-writer model;
it is deliberately not a signature and cannot resist a malicious offline
rewrite of both database content and digests.  The durable-reader capability
below is likewise only a Python trusted-process boundary: it prevents ordinary
values and a plan's in-memory evidence from masquerading as a fresh read, but
it is not cryptographic and cannot defend against hostile code in this process.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional, Tuple

from receipt_namespace_contract import EMPTY_RECEIPT_NAMESPACE_SHA256


REPOSITORY_AUDIT_PROTOCOL = "ze.repository-audit.v1"
REPOSITORY_AUDIT_MANIFEST_SCHEMA = (
    "ze.repository-audit.certificate-manifest.v1"
)
REPOSITORY_AUDIT_PLAN_STATUS = "prepared"
REPOSITORY_AUDIT_CERTIFICATE_STATUS = "passed"
REPOSITORY_AUDIT_TRANSACTION_PREFIX = "_repository-audit:"
REPOSITORY_AUDIT_TRANSACTION_KIND = "_repository_audit_certificate_v1"
REPOSITORY_AUDIT_RUNTIME_SCHEMA_VERSION = 6
REPOSITORY_AUDIT_PAGE_INTEGRITY_OK = "ok"

MAX_REPOSITORY_AUDIT_TEXT_BYTES = 1024 * 1024
MAX_REPOSITORY_AUDIT_RUNTIME_GENERATION_BYTES = 256
MAX_REPOSITORY_AUDIT_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_REPOSITORY_AUDIT_MANIFEST_NODES = 100_000
MAX_REPOSITORY_AUDIT_MEMBERSHIP_HOPS = 256
MAX_REPOSITORY_AUDIT_MEMBERSHIP_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_SQLITE_INTEGER = 2**63 - 1

EMPTY_WORKER_LEDGER_TAIL_SHA256 = b"\0" * 32
EMPTY_REPOSITORY_AUDIT_CERTIFICATE_TAIL_SHA256 = b"\0" * 32

_LOWER_HEX = frozenset("0123456789abcdef")
_FULL_AUDIT_ADAPTER_CAPABILITY_ISSUER = object()
_FRESH_AUDIT_OBSERVATION_ISSUER = object()
_DURABLE_READER_CAPABILITY_ISSUER = object()
_FRESH_OBSERVATION_ISSUER = object()
_LEDGER_READER_CAPABILITY_ISSUER = object()
_FRESH_MEMBERSHIP_OBSERVATION_ISSUER = object()


class RepositoryAuditContractError(ValueError):
    """Stable, payload-free failure at the repository-audit boundary."""

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
    raise RepositoryAuditContractError(code, path)


def _text(
    value: Any,
    path: str,
    *,
    maximum: int = MAX_REPOSITORY_AUDIT_TEXT_BYTES,
) -> str:
    if type(value) is not str or not value:
        _fail("repository_audit_text_invalid", path)
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeEncodeError:
        _fail("repository_audit_text_invalid", path)
    if len(encoded) > maximum:
        _fail("repository_audit_text_budget_exceeded", path)
    return value


def _integer(
    value: Any,
    path: str,
    *,
    minimum: int = 0,
    maximum: int = MAX_SQLITE_INTEGER,
) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _fail("repository_audit_integer_invalid", path)
    return value


def _lower_hex(value: Any, path: str, length: int) -> str:
    if (
        type(value) is not str
        or len(value) != length
        or any(character not in _LOWER_HEX for character in value)
    ):
        _fail("repository_audit_hex_invalid", path)
    return value


def _digest(value: Any, path: str) -> bytes:
    if type(value) is not bytes or len(value) != 32:
        _fail("repository_audit_digest_invalid", path)
    return value


def _timestamp(value: Any, path: str) -> str:
    if type(value) is not str or len(value) != 24:
        _fail("repository_audit_timestamp_invalid", path)
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except (TypeError, ValueError):
        _fail("repository_audit_timestamp_invalid", path)
    canonical = parsed.strftime("%Y-%m-%dT%H:%M:%S.") + (
        "%03dZ" % (parsed.microsecond // 1000)
    )
    if canonical != value:
        _fail("repository_audit_timestamp_invalid", path)
    return value


class RepositoryAuditAuthorityState(str, Enum):
    ACTIVE = "active"
    READ_ONLY = "read_only"
    REPAIR_REQUIRED = "repair_required"


@dataclass(frozen=True)
class RepositoryAuditRuntimeBinding:
    """Pure field-for-field projection of one validated runtime profile.

    The future adapter, not this carrier, must prove that these fields came
    from the exact ``BusinessRuntimeProfile`` observed in the audited snapshot.
    """

    authority: str
    authority_state: RepositoryAuditAuthorityState
    repository_epoch: str
    cutover_id: str
    source_admission_revision: int
    repository_revision: int
    state_revision: int
    runtime_generation: str
    code_digest: bytes = field(repr=False)
    schema_version: int = REPOSITORY_AUDIT_RUNTIME_SCHEMA_VERSION
    updated_at: str = ""

    def __post_init__(self) -> None:
        _validate_runtime_binding(self, "$.runtime")


def _validate_runtime_binding(
    value: Any,
    path: str,
) -> RepositoryAuditRuntimeBinding:
    if type(value) is not RepositoryAuditRuntimeBinding:
        _fail("repository_audit_runtime_type_invalid", path)
    if type(value.authority) is not str or value.authority != "sqlite":
        _fail("repository_audit_runtime_identity_invalid", path + ".authority")
    if type(value.authority_state) is not RepositoryAuditAuthorityState:
        _fail("repository_audit_runtime_identity_invalid", path + ".authority_state")
    _lower_hex(value.repository_epoch, path + ".repository_epoch", 64)
    _lower_hex(value.cutover_id, path + ".cutover_id", 64)
    _integer(
        value.source_admission_revision,
        path + ".source_admission_revision",
        minimum=1,
    )
    _integer(value.repository_revision, path + ".repository_revision", minimum=1)
    _integer(value.state_revision, path + ".state_revision", minimum=1)
    _text(
        value.runtime_generation,
        path + ".runtime_generation",
        maximum=MAX_REPOSITORY_AUDIT_RUNTIME_GENERATION_BYTES,
    )
    _digest(value.code_digest, path + ".code_digest")
    if (
        type(value.schema_version) is not int
        or value.schema_version != REPOSITORY_AUDIT_RUNTIME_SCHEMA_VERSION
    ):
        _fail("repository_audit_runtime_schema_invalid", path + ".schema_version")
    _timestamp(value.updated_at, path + ".updated_at")
    return value


class RepositoryAuditPurpose(str, Enum):
    STARTUP = "startup"
    PERIODIC = "periodic"
    INSTALL = "install"
    REPAIR_EXIT = "repair_exit"


class RepositoryAuditPersistedReadStatus(str, Enum):
    COMPLETE = "complete"
    UNKNOWN = "unknown"


class RepositoryAuditReconcileStatus(str, Enum):
    ABSENT = "absent"
    EXACT = "exact"
    CONFLICT = "conflict"
    CORRUPT = "corrupt"
    UNKNOWN = "unknown"


class RepositoryAuditRetryAction(str, Enum):
    APPLY_EXACT = "apply_exact"
    RETURN_EXACT = "return_exact"
    STOP = "stop"
    RECONCILE_REQUIRED = "reconcile_required"


@dataclass(frozen=True)
class RepositoryAuditCertificateLedgerHead:
    """Exact singleton certificate-ledger head read from the audited snapshot."""

    sequence: int
    certificate_id: Optional[str] = None
    manifest_sha256: Optional[bytes] = field(default=None, repr=False)
    certificate_tail_sha256: Optional[bytes] = field(default=None, repr=False)

    def __post_init__(self) -> None:
        _integer(self.sequence, "$.certificate_head.sequence")
        values = (
            self.certificate_id,
            self.manifest_sha256,
            self.certificate_tail_sha256,
        )
        if self.sequence == 0:
            if any(value is not None for value in values):
                _fail(
                    "repository_audit_certificate_head_invalid",
                    "$.certificate_head",
                )
            return
        if any(value is None for value in values):
            _fail(
                "repository_audit_certificate_head_invalid",
                "$.certificate_head",
            )
        _lower_hex(
            self.certificate_id,
            "$.certificate_head.certificate_id",
            32,
        )
        _digest(
            self.manifest_sha256,
            "$.certificate_head.manifest_sha256",
        )
        tail = _digest(
            self.certificate_tail_sha256,
            "$.certificate_head.certificate_tail_sha256",
        )
        if tail == EMPTY_REPOSITORY_AUDIT_CERTIFICATE_TAIL_SHA256:
            _fail(
                "repository_audit_certificate_head_invalid",
                "$.certificate_head.certificate_tail_sha256",
            )


@dataclass(frozen=True)
class RepositoryAuditSchemaObservation:
    """Actual schema facts observed from the audited SQLite snapshot."""

    user_version: int
    schema_fingerprint: str
    migration_ledger_count: int
    migration_ledger_sha256: bytes = field(repr=False)

    def __post_init__(self) -> None:
        _integer(self.user_version, "$.schema.user_version", minimum=1)
        _lower_hex(
            self.schema_fingerprint,
            "$.schema.schema_fingerprint",
            64,
        )
        _integer(
            self.migration_ledger_count,
            "$.schema.migration_ledger_count",
            minimum=1,
        )
        _digest(
            self.migration_ledger_sha256,
            "$.schema.migration_ledger_sha256",
        )
        if self.migration_ledger_count != self.user_version:
            _fail("repository_audit_migration_ledger_invalid", "$.schema")


@dataclass(frozen=True)
class RepositoryAuditCoverage:
    """History high-water facts covered by the full audit.

    ``receipt_namespace_*`` covers the complete reserved namespace in the
    audited snapshot before this prospective certificate receipt is inserted.
    It includes prior audit receipts, so a successor must advance the count and
    digest by at least its predecessor's generic parent receipt.  Counts and
    digests are trusted only inside a ``RepositoryFreshAuditObservation``
    stamped by the independent full-audit adapter after an actual complete
    namespace scan in the same snapshot; arbitrary caller values prove nothing.
    """

    covered_business_revision: int
    worker_ledger_sequence: int
    worker_ledger_tail_sha256: bytes = field(repr=False)
    receipt_namespace_count: int = 0
    receipt_namespace_sha256: bytes = field(
        default=EMPTY_RECEIPT_NAMESPACE_SHA256,
        repr=False,
    )
    cutover_install_manifest_sha256: Optional[bytes] = field(
        default=None,
        repr=False,
    )

    def __post_init__(self) -> None:
        _integer(
            self.covered_business_revision,
            "$.coverage.covered_business_revision",
            minimum=1,
        )
        _integer(
            self.worker_ledger_sequence,
            "$.coverage.worker_ledger_sequence",
        )
        worker_tail = _digest(
            self.worker_ledger_tail_sha256,
            "$.coverage.worker_ledger_tail_sha256",
        )
        if (self.worker_ledger_sequence == 0) != (
            worker_tail == EMPTY_WORKER_LEDGER_TAIL_SHA256
        ):
            _fail("repository_audit_worker_ledger_invalid", "$.coverage")
        _integer(
            self.receipt_namespace_count,
            "$.coverage.receipt_namespace_count",
        )
        receipt_digest = _digest(
            self.receipt_namespace_sha256,
            "$.coverage.receipt_namespace_sha256",
        )
        if (self.receipt_namespace_count == 0) != (
            receipt_digest == EMPTY_RECEIPT_NAMESPACE_SHA256
        ):
            _fail("repository_audit_receipt_namespace_invalid", "$.coverage")
        if self.cutover_install_manifest_sha256 is not None:
            _digest(
                self.cutover_install_manifest_sha256,
                "$.coverage.cutover_install_manifest_sha256",
            )


@dataclass(frozen=True)
class RepositoryAuditFindings:
    """Closed success findings; any non-zero violation is unrepresentable."""

    page_integrity: str
    foreign_key_violation_count: int
    namespace_violation_count: int
    started_at: str
    completed_at: str

    def __post_init__(self) -> None:
        if (
            type(self.page_integrity) is not str
            or self.page_integrity != REPOSITORY_AUDIT_PAGE_INTEGRITY_OK
        ):
            _fail("repository_audit_page_integrity_failed", "$.findings.page_integrity")
        if (
            type(self.foreign_key_violation_count) is not int
            or self.foreign_key_violation_count != 0
        ):
            _fail(
                "repository_audit_foreign_key_violation",
                "$.findings.foreign_key_violation_count",
            )
        if (
            type(self.namespace_violation_count) is not int
            or self.namespace_violation_count != 0
        ):
            _fail(
                "repository_audit_namespace_violation",
                "$.findings.namespace_violation_count",
            )
        _timestamp(self.started_at, "$.findings.started_at")
        _timestamp(self.completed_at, "$.findings.completed_at")
        if self.completed_at < self.started_at:
            _fail("repository_audit_time_window_invalid", "$.findings")


@dataclass(frozen=True)
class RepositoryAuditObservedInput:
    """Typed facts an adapter observed; this value is not a certificate."""

    certificate_id: str
    purpose: RepositoryAuditPurpose
    runtime: RepositoryAuditRuntimeBinding
    schema: RepositoryAuditSchemaObservation
    coverage: RepositoryAuditCoverage
    findings: RepositoryAuditFindings

    def __post_init__(self) -> None:
        _lower_hex(self.certificate_id, "$.certificate_id", 32)
        if type(self.purpose) is not RepositoryAuditPurpose:
            _fail("repository_audit_purpose_invalid", "$.purpose")
        runtime = _validate_runtime_binding(self.runtime, "$.runtime")
        if type(self.schema) is not RepositoryAuditSchemaObservation:
            _fail("repository_audit_schema_observation_invalid", "$.schema")
        if type(self.coverage) is not RepositoryAuditCoverage:
            _fail("repository_audit_coverage_invalid", "$.coverage")
        if type(self.findings) is not RepositoryAuditFindings:
            _fail("repository_audit_findings_invalid", "$.findings")
        if self.coverage.covered_business_revision != runtime.repository_revision:
            _fail("repository_audit_business_revision_mismatch", "$.coverage")
        if self.findings.started_at < runtime.updated_at:
            _fail("repository_audit_runtime_time_invalid", "$.runtime.updated_at")

        state = runtime.authority_state
        if self.purpose is RepositoryAuditPurpose.INSTALL:
            if (
                state is not RepositoryAuditAuthorityState.READ_ONLY
                or self.coverage.cutover_install_manifest_sha256 is None
            ):
                _fail("repository_audit_install_binding_invalid", "$.purpose")
        elif self.purpose is RepositoryAuditPurpose.REPAIR_EXIT:
            if state is not RepositoryAuditAuthorityState.REPAIR_REQUIRED:
                _fail("repository_audit_repair_exit_binding_invalid", "$.purpose")
        elif state not in (
            RepositoryAuditAuthorityState.ACTIVE,
            RepositoryAuditAuthorityState.READ_ONLY,
        ):
            _fail("repository_audit_runtime_state_invalid", "$.runtime.authority_state")


@dataclass(frozen=True)
class RepositoryAuditGenericReceiptEvidence:
    """The exact generic transaction receipt that parents one certificate."""

    transaction_id: str
    kind: str
    committed_at: str

    def __post_init__(self) -> None:
        # Persisted observations must be able to represent an exact transaction
        # id occupied by the wrong kind.  The specialised certificate below
        # tightens this carrier to the reserved audit prefix and kind.
        _text(self.transaction_id, "$.receipt.transaction_id")
        _text(self.kind, "$.receipt.kind")
        _timestamp(self.committed_at, "$.receipt.committed_at")


@dataclass(frozen=True)
class RepositoryAuditCertificateEvidence:
    """Exact specialised value a future writer must durably persist."""

    certificate_id: str
    purpose: RepositoryAuditPurpose
    sequence: int
    runtime: RepositoryAuditRuntimeBinding
    schema: RepositoryAuditSchemaObservation
    coverage: RepositoryAuditCoverage
    findings: RepositoryAuditFindings
    receipt_parent: RepositoryAuditGenericReceiptEvidence
    previous_certificate_id: Optional[str]
    previous_manifest_sha256: Optional[bytes] = field(repr=False)
    previous_certificate_tail_sha256: Optional[bytes] = field(repr=False)
    manifest_bytes: bytes = field(repr=False)
    manifest_sha256: bytes = field(repr=False)
    certificate_tail_sha256: bytes = field(repr=False)
    protocol: str = REPOSITORY_AUDIT_PROTOCOL
    status: str = REPOSITORY_AUDIT_CERTIFICATE_STATUS

    def __post_init__(self) -> None:
        RepositoryAuditObservedInput(
            certificate_id=self.certificate_id,
            purpose=self.purpose,
            runtime=self.runtime,
            schema=self.schema,
            coverage=self.coverage,
            findings=self.findings,
        )
        _integer(self.sequence, "$.evidence.sequence", minimum=1)
        if type(self.receipt_parent) is not RepositoryAuditGenericReceiptEvidence:
            _fail("repository_audit_receipt_evidence_invalid", "$.evidence.receipt_parent")
        expected_transaction_id = REPOSITORY_AUDIT_TRANSACTION_PREFIX + self.certificate_id
        if (
            self.receipt_parent.transaction_id != expected_transaction_id
            or self.receipt_parent.kind != REPOSITORY_AUDIT_TRANSACTION_KIND
            or self.receipt_parent.committed_at != self.findings.completed_at
        ):
            _fail("repository_audit_receipt_binding_invalid", "$.evidence.receipt_parent")
        previous_values = (
            self.previous_certificate_id,
            self.previous_manifest_sha256,
            self.previous_certificate_tail_sha256,
        )
        if self.sequence == 1:
            if any(value is not None for value in previous_values):
                _fail("repository_audit_predecessor_invalid", "$.evidence.predecessor")
        else:
            if any(value is None for value in previous_values):
                _fail("repository_audit_predecessor_invalid", "$.evidence.predecessor")
            _lower_hex(
                self.previous_certificate_id,
                "$.evidence.previous_certificate_id",
                32,
            )
            if self.previous_certificate_id == self.certificate_id:
                _fail("repository_audit_predecessor_invalid", "$.evidence.predecessor")
            _digest(
                self.previous_manifest_sha256,
                "$.evidence.previous_manifest_sha256",
            )
            _digest(
                self.previous_certificate_tail_sha256,
                "$.evidence.previous_certificate_tail_sha256",
            )
            if (
                self.previous_certificate_tail_sha256
                == EMPTY_REPOSITORY_AUDIT_CERTIFICATE_TAIL_SHA256
            ):
                _fail("repository_audit_predecessor_invalid", "$.evidence.predecessor")
        if (
            type(self.manifest_bytes) is not bytes
            or not 1 <= len(self.manifest_bytes) <= MAX_REPOSITORY_AUDIT_MANIFEST_BYTES
        ):
            _fail("repository_audit_manifest_invalid", "$.evidence.manifest_bytes")
        _digest(self.manifest_sha256, "$.evidence.manifest_sha256")
        _digest(
            self.certificate_tail_sha256,
            "$.evidence.certificate_tail_sha256",
        )
        if (
            self.certificate_tail_sha256
            == EMPTY_REPOSITORY_AUDIT_CERTIFICATE_TAIL_SHA256
        ):
            _fail("repository_audit_certificate_tail_invalid", "$.evidence")
        if type(self.protocol) is not str or self.protocol != REPOSITORY_AUDIT_PROTOCOL:
            _fail("repository_audit_protocol_invalid", "$.evidence.protocol")
        if (
            type(self.status) is not str
            or self.status != REPOSITORY_AUDIT_CERTIFICATE_STATUS
        ):
            _fail("repository_audit_status_invalid", "$.evidence.status")

    @property
    def transaction_id(self) -> str:
        return self.receipt_parent.transaction_id

    @property
    def receipt_kind(self) -> str:
        return self.receipt_parent.kind


@dataclass(frozen=True)
class RepositoryAuditPersistedObservation:
    """Raw same-snapshot read of exact-id rows and singleton ledger head."""

    read_status: RepositoryAuditPersistedReadStatus
    receipt: Optional[RepositoryAuditGenericReceiptEvidence] = None
    certificate: Optional[RepositoryAuditCertificateEvidence] = None
    certificate_head: Optional[RepositoryAuditCertificateLedgerHead] = None
    error_code: str = ""

    def __post_init__(self) -> None:
        if type(self.read_status) is not RepositoryAuditPersistedReadStatus:
            _fail("repository_audit_persisted_read_invalid", "$.persisted.read_status")
        if self.read_status is RepositoryAuditPersistedReadStatus.UNKNOWN:
            if (
                self.receipt is not None
                or self.certificate is not None
                or self.certificate_head is not None
                or type(self.error_code) is not str
                or not self.error_code
            ):
                _fail("repository_audit_persisted_read_invalid", "$.persisted")
            _text(self.error_code, "$.persisted.error_code", maximum=1024)
            return
        if self.error_code != "":
            _fail("repository_audit_persisted_read_invalid", "$.persisted.error_code")
        if self.receipt is not None and type(
            self.receipt
        ) is not RepositoryAuditGenericReceiptEvidence:
            _fail("repository_audit_persisted_read_invalid", "$.persisted.receipt")
        if self.certificate is not None and type(
            self.certificate
        ) is not RepositoryAuditCertificateEvidence:
            _fail("repository_audit_persisted_read_invalid", "$.persisted.certificate")
        if self.certificate_head is not None and type(
            self.certificate_head
        ) is not RepositoryAuditCertificateLedgerHead:
            _fail(
                "repository_audit_persisted_read_invalid",
                "$.persisted.certificate_head",
            )


class RepositoryFullAuditAdapterCapability:
    """Independent authority to attest one complete same-snapshot audit.

    The future ``sqlite_business_audit`` adapter owns this capability and must
    obtain runtime profile, schema registry/fingerprint, migration ledger,
    receipt namespace, install row, findings, and certificate head from one
    protected SQLite snapshot.  The activation writer and durable-id reader
    must not own it.  This is trusted-process isolation, not cryptography.
    """

    __slots__ = ("_issuer",)

    def __init__(self, *, _issuer: Any) -> None:
        if _issuer is not _FULL_AUDIT_ADAPTER_CAPABILITY_ISSUER:
            _fail("repository_full_audit_capability_invalid", "$.full_audit")
        object.__setattr__(self, "_issuer", _issuer)

    def __setattr__(self, name: str, value: Any) -> None:
        del name, value
        raise AttributeError("RepositoryFullAuditAdapterCapability is immutable")


_REPOSITORY_FULL_AUDIT_ADAPTER_CAPABILITY = (
    RepositoryFullAuditAdapterCapability(
        _issuer=_FULL_AUDIT_ADAPTER_CAPABILITY_ISSUER
    )
)


class RepositoryFreshAuditObservation:
    """Full-audit-adapter stamped facts, head, and exact predecessor."""

    __slots__ = (
        "observed",
        "certificate_head",
        "previous_certificate",
        "_adapter",
        "_issuer",
    )

    def __init__(
        self,
        observed: RepositoryAuditObservedInput,
        certificate_head: RepositoryAuditCertificateLedgerHead,
        previous_certificate: Optional[RepositoryAuditCertificateEvidence],
        *,
        _adapter: RepositoryFullAuditAdapterCapability,
        _issuer: Any,
    ) -> None:
        if (
            _issuer is not _FRESH_AUDIT_OBSERVATION_ISSUER
            or _adapter is not _REPOSITORY_FULL_AUDIT_ADAPTER_CAPABILITY
            or type(observed) is not RepositoryAuditObservedInput
            or type(certificate_head) is not RepositoryAuditCertificateLedgerHead
            or (
                previous_certificate is not None
                and type(previous_certificate)
                is not RepositoryAuditCertificateEvidence
            )
            or (certificate_head.sequence == 0) != (previous_certificate is None)
        ):
            _fail("repository_fresh_audit_observation_invalid", "$.fresh_audit")
        object.__setattr__(self, "observed", observed)
        object.__setattr__(self, "certificate_head", certificate_head)
        object.__setattr__(self, "previous_certificate", previous_certificate)
        object.__setattr__(self, "_adapter", _adapter)
        object.__setattr__(self, "_issuer", _issuer)

    def __setattr__(self, name: str, value: Any) -> None:
        del name, value
        raise AttributeError("RepositoryFreshAuditObservation is immutable")


def _stamp_repository_fresh_audit_observation(
    adapter: RepositoryFullAuditAdapterCapability,
    observed: RepositoryAuditObservedInput,
    certificate_head: RepositoryAuditCertificateLedgerHead,
    previous_certificate: Optional[RepositoryAuditCertificateEvidence] = None,
) -> RepositoryFreshAuditObservation:
    """Stamp typed facts only after the adapter completes its full snapshot."""

    if (
        adapter is not _REPOSITORY_FULL_AUDIT_ADAPTER_CAPABILITY
    ):
        _fail("repository_full_audit_capability_invalid", "$.full_audit")
    return RepositoryFreshAuditObservation(
        observed,
        certificate_head,
        previous_certificate,
        _adapter=adapter,
        _issuer=_FRESH_AUDIT_OBSERVATION_ISSUER,
    )


def _unwrap_repository_fresh_audit_observation(
    value: Any,
) -> tuple:
    """Reject raw typed facts and forged full-audit wrappers."""

    if (
        type(value) is not RepositoryFreshAuditObservation
        or getattr(value, "_issuer", None) is not _FRESH_AUDIT_OBSERVATION_ISSUER
        or getattr(value, "_adapter", None)
        is not _REPOSITORY_FULL_AUDIT_ADAPTER_CAPABILITY
        or type(getattr(value, "observed", None))
        is not RepositoryAuditObservedInput
        or type(getattr(value, "certificate_head", None))
        is not RepositoryAuditCertificateLedgerHead
        or (
            getattr(value, "previous_certificate", None) is not None
            and type(value.previous_certificate)
            is not RepositoryAuditCertificateEvidence
        )
        or (value.certificate_head.sequence == 0)
        != (value.previous_certificate is None)
    ):
        _fail("repository_fresh_audit_observation_invalid", "$.fresh_audit")
    return (
        value.observed,
        value.certificate_head,
        value.previous_certificate,
    )


class RepositoryAuditDurableReaderCapability:
    """Opaque authority held only by the trusted durable-read adapter.

    Python privacy is the intended boundary.  Possession means the caller is
    trusted to have populated generic receipt, specialised certificate, and
    singleton head from one fresh storage snapshot.  This object is neither
    serialisable proof nor a security token for hostile same-process code.
    """

    __slots__ = ("_issuer",)

    def __init__(self, *, _issuer: Any) -> None:
        if _issuer is not _DURABLE_READER_CAPABILITY_ISSUER:
            _fail("repository_audit_reader_capability_invalid", "$.reader")
        object.__setattr__(self, "_issuer", _issuer)

    def __setattr__(self, name: str, value: Any) -> None:
        del name, value
        raise AttributeError("RepositoryAuditDurableReaderCapability is immutable")


_REPOSITORY_AUDIT_DURABLE_READER_CAPABILITY = (
    RepositoryAuditDurableReaderCapability(
        _issuer=_DURABLE_READER_CAPABILITY_ISSUER
    )
)


class RepositoryAuditFreshObservation:
    """Reader-stamped wrapper required by the fresh classifier."""

    __slots__ = ("persisted", "_reader", "_issuer")

    def __init__(
        self,
        persisted: RepositoryAuditPersistedObservation,
        *,
        _reader: RepositoryAuditDurableReaderCapability,
        _issuer: Any,
    ) -> None:
        if (
            _issuer is not _FRESH_OBSERVATION_ISSUER
            or _reader is not _REPOSITORY_AUDIT_DURABLE_READER_CAPABILITY
            or type(persisted) is not RepositoryAuditPersistedObservation
        ):
            _fail("repository_audit_fresh_observation_invalid", "$.fresh")
        object.__setattr__(self, "persisted", persisted)
        object.__setattr__(self, "_reader", _reader)
        object.__setattr__(self, "_issuer", _issuer)

    def __setattr__(self, name: str, value: Any) -> None:
        del name, value
        raise AttributeError("RepositoryAuditFreshObservation is immutable")


def _stamp_repository_audit_fresh_observation(
    reader: RepositoryAuditDurableReaderCapability,
    persisted: RepositoryAuditPersistedObservation,
) -> RepositoryAuditFreshObservation:
    """Stamp a carrier after the capability owner completed a durable read."""

    if (
        reader is not _REPOSITORY_AUDIT_DURABLE_READER_CAPABILITY
    ):
        _fail("repository_audit_reader_capability_invalid", "$.reader")
    return RepositoryAuditFreshObservation(
        persisted,
        _reader=reader,
        _issuer=_FRESH_OBSERVATION_ISSUER,
    )


def _unwrap_repository_audit_fresh_observation(
    value: Any,
) -> RepositoryAuditPersistedObservation:
    """Reject ordinary carriers and forged wrappers at the consumer edge."""

    if (
        type(value) is not RepositoryAuditFreshObservation
        or getattr(value, "_issuer", None) is not _FRESH_OBSERVATION_ISSUER
        or getattr(value, "_reader", None)
        is not _REPOSITORY_AUDIT_DURABLE_READER_CAPABILITY
        or type(getattr(value, "persisted", None))
        is not RepositoryAuditPersistedObservation
    ):
        _fail("repository_audit_fresh_observation_invalid", "$.fresh")
    return value.persisted


@dataclass(frozen=True)
class RepositoryAuditCertificateLedgerEntry:
    """Actual generic/specialised pair for one historical successor hop."""

    receipt: Optional[RepositoryAuditGenericReceiptEvidence]
    certificate: RepositoryAuditCertificateEvidence

    def __post_init__(self) -> None:
        if self.receipt is not None and type(
            self.receipt
        ) is not RepositoryAuditGenericReceiptEvidence:
            _fail("repository_audit_ledger_entry_invalid", "$.ledger_entry.receipt")
        if type(self.certificate) is not RepositoryAuditCertificateEvidence:
            _fail(
                "repository_audit_ledger_entry_invalid",
                "$.ledger_entry.certificate",
            )


@dataclass(frozen=True)
class RepositoryAuditMembershipObservation:
    """Raw bounded ledger path from one certificate to the current head.

    v1 intentionally uses a bounded linear proof.  A schema-v8+ evolution must
    add durable checkpoints, authenticated skip links, or a Merkle accumulator
    before deployments need historical proofs beyond these budgets.
    """

    read_status: RepositoryAuditPersistedReadStatus
    receipt: Optional[RepositoryAuditGenericReceiptEvidence] = None
    certificate: Optional[RepositoryAuditCertificateEvidence] = None
    current_head: Optional[RepositoryAuditCertificateLedgerHead] = None
    successor_chain: Tuple[RepositoryAuditCertificateLedgerEntry, ...] = ()
    error_code: str = ""

    def __post_init__(self) -> None:
        if type(self.read_status) is not RepositoryAuditPersistedReadStatus:
            _fail("repository_audit_membership_read_invalid", "$.membership")
        if type(self.successor_chain) is not tuple or any(
            type(value) is not RepositoryAuditCertificateLedgerEntry
            for value in self.successor_chain
        ):
            _fail("repository_audit_membership_read_invalid", "$.membership.chain")
        if len(self.successor_chain) > MAX_REPOSITORY_AUDIT_MEMBERSHIP_HOPS:
            _fail(
                "repository_audit_membership_budget_exceeded",
                "$.membership.chain",
            )
        manifest_bytes = sum(
            len(value.certificate.manifest_bytes)
            for value in self.successor_chain
        ) + (
            0
            if type(self.certificate) is not RepositoryAuditCertificateEvidence
            else len(self.certificate.manifest_bytes)
        )
        if manifest_bytes > MAX_REPOSITORY_AUDIT_MEMBERSHIP_MANIFEST_BYTES:
            _fail(
                "repository_audit_membership_budget_exceeded",
                "$.membership.chain",
            )
        if self.read_status is RepositoryAuditPersistedReadStatus.UNKNOWN:
            if (
                self.receipt is not None
                or self.certificate is not None
                or self.current_head is not None
                or self.successor_chain
                or type(self.error_code) is not str
                or not self.error_code
            ):
                _fail("repository_audit_membership_read_invalid", "$.membership")
            _text(self.error_code, "$.membership.error_code", maximum=1024)
            return
        if self.error_code != "":
            _fail(
                "repository_audit_membership_read_invalid",
                "$.membership.error_code",
            )
        if self.receipt is not None and type(
            self.receipt
        ) is not RepositoryAuditGenericReceiptEvidence:
            _fail("repository_audit_membership_read_invalid", "$.membership.receipt")
        if self.certificate is not None and type(
            self.certificate
        ) is not RepositoryAuditCertificateEvidence:
            _fail(
                "repository_audit_membership_read_invalid",
                "$.membership.certificate",
            )
        if self.current_head is not None and type(
            self.current_head
        ) is not RepositoryAuditCertificateLedgerHead:
            _fail("repository_audit_membership_read_invalid", "$.membership.head")


class RepositoryAuditLedgerReaderCapability:
    """Separate authority for a complete certificate-chain membership read."""

    __slots__ = ("_issuer",)

    def __init__(self, *, _issuer: Any) -> None:
        if _issuer is not _LEDGER_READER_CAPABILITY_ISSUER:
            _fail("repository_audit_ledger_reader_invalid", "$.ledger_reader")
        object.__setattr__(self, "_issuer", _issuer)

    def __setattr__(self, name: str, value: Any) -> None:
        del name, value
        raise AttributeError("RepositoryAuditLedgerReaderCapability is immutable")


_REPOSITORY_AUDIT_LEDGER_READER_CAPABILITY = (
    RepositoryAuditLedgerReaderCapability(
        _issuer=_LEDGER_READER_CAPABILITY_ISSUER
    )
)


class RepositoryAuditFreshMembershipObservation:
    """Ledger-reader stamped exact certificate-to-head chain observation."""

    __slots__ = ("membership", "_reader", "_issuer")

    def __init__(
        self,
        membership: RepositoryAuditMembershipObservation,
        *,
        _reader: RepositoryAuditLedgerReaderCapability,
        _issuer: Any,
    ) -> None:
        if (
            _issuer is not _FRESH_MEMBERSHIP_OBSERVATION_ISSUER
            or _reader is not _REPOSITORY_AUDIT_LEDGER_READER_CAPABILITY
            or type(membership) is not RepositoryAuditMembershipObservation
        ):
            _fail("repository_audit_fresh_membership_invalid", "$.membership")
        object.__setattr__(self, "membership", membership)
        object.__setattr__(self, "_reader", _reader)
        object.__setattr__(self, "_issuer", _issuer)

    def __setattr__(self, name: str, value: Any) -> None:
        del name, value
        raise AttributeError("RepositoryAuditFreshMembershipObservation is immutable")


def _stamp_repository_audit_fresh_membership_observation(
    reader: RepositoryAuditLedgerReaderCapability,
    membership: RepositoryAuditMembershipObservation,
) -> RepositoryAuditFreshMembershipObservation:
    """Private ledger-adapter hook after reading the complete successor path."""

    if reader is not _REPOSITORY_AUDIT_LEDGER_READER_CAPABILITY:
        _fail("repository_audit_ledger_reader_invalid", "$.ledger_reader")
    return RepositoryAuditFreshMembershipObservation(
        membership,
        _reader=reader,
        _issuer=_FRESH_MEMBERSHIP_OBSERVATION_ISSUER,
    )


def _unwrap_repository_audit_fresh_membership_observation(
    value: Any,
) -> RepositoryAuditMembershipObservation:
    if (
        type(value) is not RepositoryAuditFreshMembershipObservation
        or getattr(value, "_issuer", None)
        is not _FRESH_MEMBERSHIP_OBSERVATION_ISSUER
        or getattr(value, "_reader", None)
        is not _REPOSITORY_AUDIT_LEDGER_READER_CAPABILITY
        or type(getattr(value, "membership", None))
        is not RepositoryAuditMembershipObservation
    ):
        _fail("repository_audit_fresh_membership_invalid", "$.membership")
    return value.membership


@dataclass(frozen=True)
class RepositoryAuditClassification:
    status: RepositoryAuditReconcileStatus
    certificate_id: str
    evidence: Optional[RepositoryAuditCertificateEvidence] = None
    error_code: str = ""

    def __post_init__(self) -> None:
        if type(self.status) is not RepositoryAuditReconcileStatus:
            _fail("repository_audit_classification_invalid", "$.classification.status")
        _lower_hex(self.certificate_id, "$.classification.certificate_id", 32)
        exact = self.status is RepositoryAuditReconcileStatus.EXACT
        failed = self.status in (
            RepositoryAuditReconcileStatus.CONFLICT,
            RepositoryAuditReconcileStatus.CORRUPT,
            RepositoryAuditReconcileStatus.UNKNOWN,
        )
        if (
            (exact and type(self.evidence) is not RepositoryAuditCertificateEvidence)
            or (not exact and self.evidence is not None)
            or (
                exact
                and self.evidence is not None
                and self.evidence.certificate_id != self.certificate_id
            )
            or (failed and (type(self.error_code) is not str or not self.error_code))
            or (not failed and self.error_code != "")
        ):
            _fail("repository_audit_classification_invalid", "$.classification")
        if failed:
            _text(self.error_code, "$.classification.error_code", maximum=1024)

    @property
    def retry_action(self) -> RepositoryAuditRetryAction:
        if self.status is RepositoryAuditReconcileStatus.ABSENT:
            return RepositoryAuditRetryAction.APPLY_EXACT
        if self.status is RepositoryAuditReconcileStatus.EXACT:
            return RepositoryAuditRetryAction.RETURN_EXACT
        if self.status is RepositoryAuditReconcileStatus.UNKNOWN:
            return RepositoryAuditRetryAction.RECONCILE_REQUIRED
        return RepositoryAuditRetryAction.STOP


__all__ = ()
