"""Pure private contracts for one irreversible repository authority cutover.

The contracts in this module are values only.  They cannot inspect a file,
open SQLite, acquire a lock, choose a clock, invoke a callback, or move
authority.  The private integrity issuer builds the five cutover facts from
one exact legacy repository proof; later adapters may persist only those
issuer-stamped facts and must freshly reconcile them before acknowledging an
install or join.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


BUSINESS_CUTOVER_PROTOCOL = "ze.business-cutover.v1"
BUSINESS_CUTOVER_PLAN_STATUS = "prepared"
BUSINESS_CUTOVER_INSTALL_MANIFEST_SCHEMA = (
    "ze.business-cutover.install-manifest.v1"
)
BUSINESS_CUTOVER_JOIN_MANIFEST_SCHEMA = "ze.business-cutover.join-manifest.v1"
BUSINESS_CUTOVER_MEMBER_MAPPING_SCHEMA = "ze.business-cutover.member-mapping.v1"

BUSINESS_CUTOVER_INSTALL_STATUS = "installed_read_only"
BUSINESS_CUTOVER_JOIN_STATUS = "joined_active"
BUSINESS_CUTOVER_INSTALL_TRANSACTION_PREFIX = "_business-cutover-install:"
BUSINESS_CUTOVER_INSTALL_TRANSACTION_KIND = "_business_cutover_install_v1"
BUSINESS_CUTOVER_JOIN_TRANSACTION_PREFIX = "_business-cutover-join:"
BUSINESS_CUTOVER_JOIN_TRANSACTION_KIND = "_business_cutover_join_v1"

REPOSITORY_ADMISSION_SCHEMA = "ze.repository_admission.v1"
REPOSITORY_ADMISSION_HASH_DOMAIN = (
    b"ze.repository-admission.install-binding.v1\0"
)

BUSINESS_CUTOVER_AUTHORITY = "sqlite"
BUSINESS_CUTOVER_INSTALL_AUTHORITY_STATE = "read_only"
BUSINESS_CUTOVER_JOIN_AUTHORITY_STATE = "active"
BUSINESS_CUTOVER_RUNTIME_CONTROL_SCHEMA_VERSION = 6
BUSINESS_CUTOVER_INSTALL_STATE_REVISION = 1
BUSINESS_CUTOVER_JOIN_STATE_REVISION = 2

BUSINESS_CUTOVER_MEMBER_LIVE = "live"
BUSINESS_CUTOVER_MEMBER_DELETED = "deleted"
BUSINESS_CUTOVER_SAVE_POLICIES = frozenset(
    ("auto_commit", "checkpoint_commit", "legacy")
)

MAX_BUSINESS_CUTOVER_PROJECTS = 10_000
MAX_BUSINESS_CUTOVER_TEXT_BYTES = 1024 * 1024
MAX_BUSINESS_CUTOVER_PROJECT_ID_BYTES = 640
MAX_BUSINESS_CUTOVER_PROJECT_ID_CHARS = 160
MAX_BUSINESS_CUTOVER_RUNTIME_GENERATION_BYTES = 256
MAX_BUSINESS_CUTOVER_MANIFEST_BYTES = 64 * 1024 * 1024
MAX_BUSINESS_CUTOVER_MANIFEST_NODES = 1_000_000
MAX_SQLITE_INTEGER = 2**63 - 1

_LOWER_HEX = frozenset("0123456789abcdef")


class BusinessCutoverContractError(ValueError):
    """Stable payload-free failure at the pure contract boundary."""

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
    raise BusinessCutoverContractError(code, path)


def _text(
    value: Any,
    path: str,
    *,
    maximum: int = MAX_BUSINESS_CUTOVER_TEXT_BYTES,
) -> str:
    if type(value) is not str or not value:
        _fail("business_cutover_text_invalid", path)
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeEncodeError:
        _fail("business_cutover_text_invalid", path)
        raise AssertionError("unreachable")
    if len(encoded) > maximum:
        _fail("business_cutover_text_budget_exceeded", path)
    return value


def _project_id(value: Any, path: str) -> str:
    value = _text(value, path, maximum=MAX_BUSINESS_CUTOVER_PROJECT_ID_BYTES)
    if len(value) > MAX_BUSINESS_CUTOVER_PROJECT_ID_CHARS:
        _fail("business_cutover_project_id_invalid", path)
    return value


def _integer(
    value: Any,
    path: str,
    *,
    minimum: int = 0,
    maximum: int = MAX_SQLITE_INTEGER,
) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _fail("business_cutover_integer_invalid", path)
    return value


def _digest(value: Any, path: str) -> bytes:
    if type(value) is not bytes or len(value) != 32:
        _fail("business_cutover_digest_invalid", path)
    return value


def _lower_hex(value: Any, path: str, length: int) -> str:
    if (
        type(value) is not str
        or len(value) != length
        or any(character not in _LOWER_HEX for character in value)
    ):
        _fail("business_cutover_hex_invalid", path)
    return value


class BusinessCutoverPhase(str, Enum):
    """Derived cross-store states; transitions are forward-only."""

    LEGACY = "legacy"
    CUTOVER_PENDING = "cutover_pending"
    SQLITE_INSTALLED_READ_ONLY = "sqlite_installed_read_only"
    FILE_AUTHORITY_SQLITE = "file_authority_sqlite"
    SQLITE_ACTIVE = "sqlite_active"


class BusinessCutoverRetryAction(str, Enum):
    """What a caller may do after one fresh exact classification."""

    APPLY_EXACT = "apply_exact"
    RETURN_EXACT = "return_exact"
    STOP = "stop"
    RECONCILE_REQUIRED = "reconcile_required"


class BusinessCutoverInstallReconcileStatus(str, Enum):
    NOT_INSTALLED = "not_installed"
    INSTALLED_READ_ONLY = "installed_read_only"
    CONFLICT = "conflict"
    CORRUPT = "corrupt"
    UNKNOWN = "unknown"


class BusinessCutoverJoinReconcileStatus(str, Enum):
    NOT_JOINED = "not_joined"
    JOINED_ACTIVE = "joined_active"
    CONFLICT = "conflict"
    CORRUPT = "corrupt"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class BusinessCutoverAdmission:
    """One exact repository-admission value, without filesystem authority."""

    authority_state: str
    authority_epoch: str
    revision: int
    cutover_id: str

    def __post_init__(self) -> None:
        if type(self.authority_state) is not str or self.authority_state not in (
            "legacy",
            "cutover_pending",
            "sqlite",
        ):
            _fail("business_cutover_admission_state_invalid", "$.admission.state")
        _lower_hex(self.authority_epoch, "$.admission.epoch", 64)
        _integer(self.revision, "$.admission.revision", minimum=1)
        if self.authority_state == "legacy":
            if type(self.cutover_id) is not str or self.cutover_id != "":
                _fail(
                    "business_cutover_admission_cutover_invalid",
                    "$.admission.cutover_id",
                )
        else:
            _lower_hex(self.cutover_id, "$.admission.cutover_id", 64)

    def canonical_value(self) -> dict:
        return {
            "schema": REPOSITORY_ADMISSION_SCHEMA,
            "authority_state": self.authority_state,
            "authority_epoch": self.authority_epoch,
            "revision": self.revision,
            "cutover_id": self.cutover_id,
        }


def digest_business_cutover_admission(admission: BusinessCutoverAdmission) -> bytes:
    """Match topology's v1 admission binding digest without importing the SDK."""

    if type(admission) is not BusinessCutoverAdmission:
        _fail("business_cutover_admission_invalid", "$.admission")
    canonical = json.dumps(
        admission.canonical_value(),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(REPOSITORY_ADMISSION_HASH_DOMAIN + canonical).digest()


@dataclass(frozen=True)
class BusinessCutoverLegacyProof:
    """Exact V5 proof identity consumed by the cutover manifest."""

    cutover_id: str
    transaction_id: str
    catalog_id: str
    catalog_seal_sha256: bytes = field(repr=False)
    repository_epoch: str = ""
    source_admission_revision: int = 0
    manifest_sha256: bytes = field(default=b"", repr=False)
    source_repository_sha256: bytes = field(default=b"", repr=False)
    catalog_coverage_sha256: bytes = field(default=b"", repr=False)
    proof_sha256: bytes = field(default=b"", repr=False)
    schema_fingerprint: str = ""
    project_count: int = 0
    live_project_count: int = 0
    deleted_project_count: int = 0

    def __post_init__(self) -> None:
        _lower_hex(self.cutover_id, "$.legacy_proof.cutover_id", 64)
        _text(self.transaction_id, "$.legacy_proof.transaction_id")
        _lower_hex(self.catalog_id, "$.legacy_proof.catalog_id", 64)
        _digest(self.catalog_seal_sha256, "$.legacy_proof.catalog_seal_sha256")
        _lower_hex(self.repository_epoch, "$.legacy_proof.repository_epoch", 64)
        _integer(
            self.source_admission_revision,
            "$.legacy_proof.source_admission_revision",
            minimum=1,
            maximum=MAX_SQLITE_INTEGER - 2,
        )
        _digest(self.manifest_sha256, "$.legacy_proof.manifest_sha256")
        _digest(
            self.source_repository_sha256,
            "$.legacy_proof.source_repository_sha256",
        )
        _digest(
            self.catalog_coverage_sha256,
            "$.legacy_proof.catalog_coverage_sha256",
        )
        _digest(self.proof_sha256, "$.legacy_proof.proof_sha256")
        _lower_hex(self.schema_fingerprint, "$.legacy_proof.schema_fingerprint", 64)
        project_count = _integer(
            self.project_count,
            "$.legacy_proof.project_count",
            maximum=MAX_BUSINESS_CUTOVER_PROJECTS,
        )
        live_count = _integer(
            self.live_project_count,
            "$.legacy_proof.live_project_count",
            maximum=MAX_BUSINESS_CUTOVER_PROJECTS,
        )
        deleted_count = _integer(
            self.deleted_project_count,
            "$.legacy_proof.deleted_project_count",
            maximum=MAX_BUSINESS_CUTOVER_PROJECTS,
        )
        if live_count + deleted_count != project_count:
            _fail("business_cutover_legacy_count_invalid", "$.legacy_proof")


@dataclass(frozen=True)
class BusinessCutoverLiveBaseline:
    """Typed current target facts for one live V5 member."""

    project_id: str
    generation: int
    document_id: str
    document_version: int
    business_root_sha256: bytes = field(repr=False)
    document_snapshot_sha256: bytes = field(repr=False)
    entity_count: int = 0
    save_policy: str = "legacy"
    operation_tail_sha256: bytes = field(default=b"", repr=False)
    asset_projection_sha256: bytes = field(default=b"", repr=False)
    asset_reference_count: int = 0
    unique_asset_reference_count: int = 0

    def __post_init__(self) -> None:
        _project_id(self.project_id, "$.baseline.project_id")
        _integer(self.generation, "$.baseline.generation", minimum=1)
        _text(self.document_id, "$.baseline.document_id")
        _integer(self.document_version, "$.baseline.document_version", minimum=1)
        _digest(self.business_root_sha256, "$.baseline.business_root_sha256")
        _digest(
            self.document_snapshot_sha256,
            "$.baseline.document_snapshot_sha256",
        )
        _integer(self.entity_count, "$.baseline.entity_count", maximum=1_000_000)
        if type(self.save_policy) is not str or self.save_policy not in (
            BUSINESS_CUTOVER_SAVE_POLICIES
        ):
            _fail("business_cutover_save_policy_invalid", "$.baseline.save_policy")
        operation_tail = _digest(
            self.operation_tail_sha256,
            "$.baseline.operation_tail_sha256",
        )
        if operation_tail != b"\0" * 32:
            _fail(
                "business_cutover_baseline_operation_tail_invalid",
                "$.baseline.operation_tail_sha256",
            )
        _digest(
            self.asset_projection_sha256,
            "$.baseline.asset_projection_sha256",
        )
        references = _integer(
            self.asset_reference_count,
            "$.baseline.asset_reference_count",
            maximum=1_000_000,
        )
        unique = _integer(
            self.unique_asset_reference_count,
            "$.baseline.unique_asset_reference_count",
            maximum=1_000_000,
        )
        if unique > references:
            _fail("business_cutover_asset_count_invalid", "$.baseline")


@dataclass(frozen=True)
class BusinessCutoverBaselineMember:
    """One complete live baseline mapping or deleted high-water mapping."""

    ordinal: int
    project_id: str
    generation: int
    status: str
    operation_id: str
    source_state_sha256: bytes = field(repr=False)
    source_state_bytes: int = 0
    catalog_ordinal: Optional[int] = None
    import_id: Optional[str] = None
    storage_version: Optional[str] = None
    row_snapshot_bytes: Optional[int] = None
    baseline_commit_id: Optional[str] = None
    baseline_repository_revision: Optional[int] = None
    document_id: Optional[str] = None
    document_version: Optional[int] = None
    business_root_sha256: Optional[bytes] = field(default=None, repr=False)
    document_snapshot_sha256: Optional[bytes] = field(default=None, repr=False)
    entity_count: Optional[int] = None
    save_policy: Optional[str] = None
    operation_tail_sha256: Optional[bytes] = field(default=None, repr=False)
    asset_projection_sha256: Optional[bytes] = field(default=None, repr=False)
    asset_reference_count: Optional[int] = None
    unique_asset_reference_count: Optional[int] = None
    baseline_manifest_bytes: Optional[bytes] = field(default=None, repr=False)
    baseline_manifest_sha256: Optional[bytes] = field(default=None, repr=False)

    def __post_init__(self) -> None:
        _integer(self.ordinal, "$.member.ordinal", maximum=MAX_BUSINESS_CUTOVER_PROJECTS - 1)
        _project_id(self.project_id, "$.member.project_id")
        _integer(self.generation, "$.member.generation", minimum=1)
        if type(self.status) is not str or self.status not in (
            BUSINESS_CUTOVER_MEMBER_LIVE,
            BUSINESS_CUTOVER_MEMBER_DELETED,
        ):
            _fail("business_cutover_member_status_invalid", "$.member.status")
        _text(self.operation_id, "$.member.operation_id")
        _digest(self.source_state_sha256, "$.member.source_state_sha256")
        _integer(
            self.source_state_bytes,
            "$.member.source_state_bytes",
            maximum=1024 * 1024 * 1024,
        )

        live_values = (
            self.catalog_ordinal,
            self.import_id,
            self.storage_version,
            self.row_snapshot_bytes,
            self.baseline_commit_id,
            self.baseline_repository_revision,
            self.document_id,
            self.document_version,
            self.business_root_sha256,
            self.document_snapshot_sha256,
            self.entity_count,
            self.save_policy,
            self.operation_tail_sha256,
            self.asset_projection_sha256,
            self.asset_reference_count,
            self.unique_asset_reference_count,
            self.baseline_manifest_bytes,
            self.baseline_manifest_sha256,
        )
        if self.status == BUSINESS_CUTOVER_MEMBER_DELETED:
            if self.source_state_bytes != 0 or any(value is not None for value in live_values):
                _fail("business_cutover_deleted_member_invalid", "$.member")
            return

        if any(value is None for value in live_values):
            _fail("business_cutover_live_member_invalid", "$.member")
        _integer(self.catalog_ordinal, "$.member.catalog_ordinal")
        _lower_hex(self.import_id, "$.member.import_id", 64)
        _text(self.storage_version, "$.member.storage_version")
        _integer(self.row_snapshot_bytes, "$.member.row_snapshot_bytes", minimum=1)
        _lower_hex(self.baseline_commit_id, "$.member.baseline_commit_id", 32)
        _integer(
            self.baseline_repository_revision,
            "$.member.baseline_repository_revision",
            minimum=1,
        )
        _text(self.document_id, "$.member.document_id")
        _integer(self.document_version, "$.member.document_version", minimum=1)
        _digest(self.business_root_sha256, "$.member.business_root_sha256")
        _digest(
            self.document_snapshot_sha256,
            "$.member.document_snapshot_sha256",
        )
        _integer(self.entity_count, "$.member.entity_count", maximum=1_000_000)
        if type(self.save_policy) is not str or self.save_policy not in (
            BUSINESS_CUTOVER_SAVE_POLICIES
        ):
            _fail("business_cutover_save_policy_invalid", "$.member.save_policy")
        _digest(self.operation_tail_sha256, "$.member.operation_tail_sha256")
        _digest(self.asset_projection_sha256, "$.member.asset_projection_sha256")
        references = _integer(
            self.asset_reference_count,
            "$.member.asset_reference_count",
            maximum=1_000_000,
        )
        unique = _integer(
            self.unique_asset_reference_count,
            "$.member.unique_asset_reference_count",
            maximum=1_000_000,
        )
        if unique > references:
            _fail("business_cutover_asset_count_invalid", "$.member")
        if type(self.baseline_manifest_bytes) is not bytes or not self.baseline_manifest_bytes:
            _fail("business_cutover_manifest_invalid", "$.member.baseline_manifest_bytes")
        _digest(
            self.baseline_manifest_sha256,
            "$.member.baseline_manifest_sha256",
        )


@dataclass(frozen=True)
class BusinessCutoverRuntimeBinding:
    """The exact read-only install or active join runtime row."""

    authority: str
    authority_state: str
    repository_epoch: str
    cutover_id: str
    source_admission_revision: int
    repository_revision: int
    state_revision: int
    runtime_generation: str
    code_digest: bytes = field(repr=False)
    schema_version: int = BUSINESS_CUTOVER_RUNTIME_CONTROL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.authority != BUSINESS_CUTOVER_AUTHORITY or type(self.authority) is not str:
            _fail("business_cutover_runtime_authority_invalid", "$.runtime.authority")
        if type(self.authority_state) is not str or self.authority_state not in (
            BUSINESS_CUTOVER_INSTALL_AUTHORITY_STATE,
            BUSINESS_CUTOVER_JOIN_AUTHORITY_STATE,
        ):
            _fail("business_cutover_runtime_state_invalid", "$.runtime.authority_state")
        _lower_hex(self.repository_epoch, "$.runtime.repository_epoch", 64)
        _lower_hex(self.cutover_id, "$.runtime.cutover_id", 64)
        _integer(
            self.source_admission_revision,
            "$.runtime.source_admission_revision",
            minimum=1,
            maximum=MAX_SQLITE_INTEGER - 2,
        )
        _integer(self.repository_revision, "$.runtime.repository_revision", minimum=1)
        _integer(self.state_revision, "$.runtime.state_revision", minimum=1)
        _text(
            self.runtime_generation,
            "$.runtime.runtime_generation",
            maximum=MAX_BUSINESS_CUTOVER_RUNTIME_GENERATION_BYTES,
        )
        _digest(self.code_digest, "$.runtime.code_digest")
        if (
            type(self.schema_version) is not int
            or self.schema_version != BUSINESS_CUTOVER_RUNTIME_CONTROL_SCHEMA_VERSION
        ):
            _fail("business_cutover_runtime_schema_invalid", "$.runtime.schema_version")
        expected_state_revision = (
            BUSINESS_CUTOVER_INSTALL_STATE_REVISION
            if self.authority_state == BUSINESS_CUTOVER_INSTALL_AUTHORITY_STATE
            else BUSINESS_CUTOVER_JOIN_STATE_REVISION
        )
        if self.state_revision != expected_state_revision:
            _fail("business_cutover_runtime_revision_invalid", "$.runtime.state_revision")


@dataclass(frozen=True)
class BusinessCutoverInstallEvidence:
    cutover_id: str
    transaction_id: str
    receipt_kind: str
    authority_state: str
    runtime: BusinessCutoverRuntimeBinding
    member_mapping_sha256: bytes = field(repr=False)
    manifest_bytes: bytes = field(repr=False)
    manifest_sha256: bytes = field(repr=False)

    def __post_init__(self) -> None:
        _lower_hex(self.cutover_id, "$.install.cutover_id", 64)
        if (
            type(self.transaction_id) is not str
            or self.transaction_id
            != BUSINESS_CUTOVER_INSTALL_TRANSACTION_PREFIX + self.cutover_id
            or self.receipt_kind != BUSINESS_CUTOVER_INSTALL_TRANSACTION_KIND
            or type(self.receipt_kind) is not str
            or self.authority_state != BUSINESS_CUTOVER_INSTALL_AUTHORITY_STATE
            or type(self.authority_state) is not str
            or type(self.runtime) is not BusinessCutoverRuntimeBinding
            or self.runtime.cutover_id != self.cutover_id
            or self.runtime.authority_state != self.authority_state
        ):
            _fail("business_cutover_install_evidence_invalid", "$.install")
        _digest(self.member_mapping_sha256, "$.install.member_mapping_sha256")
        if type(self.manifest_bytes) is not bytes or not self.manifest_bytes:
            _fail("business_cutover_manifest_invalid", "$.install.manifest_bytes")
        _digest(self.manifest_sha256, "$.install.manifest_sha256")


@dataclass(frozen=True)
class BusinessCutoverJoinEvidence:
    cutover_id: str
    transaction_id: str
    receipt_kind: str
    authority_state: str
    runtime: BusinessCutoverRuntimeBinding
    install_manifest_sha256: bytes = field(repr=False)
    final_admission_sha256: bytes = field(repr=False)
    manifest_bytes: bytes = field(repr=False)
    manifest_sha256: bytes = field(repr=False)

    def __post_init__(self) -> None:
        _lower_hex(self.cutover_id, "$.join.cutover_id", 64)
        if (
            type(self.transaction_id) is not str
            or self.transaction_id
            != BUSINESS_CUTOVER_JOIN_TRANSACTION_PREFIX + self.cutover_id
            or self.receipt_kind != BUSINESS_CUTOVER_JOIN_TRANSACTION_KIND
            or type(self.receipt_kind) is not str
            or self.authority_state != BUSINESS_CUTOVER_JOIN_AUTHORITY_STATE
            or type(self.authority_state) is not str
            or type(self.runtime) is not BusinessCutoverRuntimeBinding
            or self.runtime.cutover_id != self.cutover_id
            or self.runtime.authority_state != self.authority_state
        ):
            _fail("business_cutover_join_evidence_invalid", "$.join")
        _digest(self.install_manifest_sha256, "$.join.install_manifest_sha256")
        _digest(self.final_admission_sha256, "$.join.final_admission_sha256")
        if type(self.manifest_bytes) is not bytes or not self.manifest_bytes:
            _fail("business_cutover_manifest_invalid", "$.join.manifest_bytes")
        _digest(self.manifest_sha256, "$.join.manifest_sha256")


@dataclass(frozen=True)
class BusinessCutoverInstallResult:
    status: BusinessCutoverInstallReconcileStatus
    cutover_id: str
    evidence: Optional[BusinessCutoverInstallEvidence] = None
    error_code: str = ""

    def __post_init__(self) -> None:
        if type(self.status) is not BusinessCutoverInstallReconcileStatus:
            _fail("business_cutover_install_result_invalid", "$.install_result.status")
        _lower_hex(self.cutover_id, "$.install_result.cutover_id", 64)
        exact = self.status is BusinessCutoverInstallReconcileStatus.INSTALLED_READ_ONLY
        failed = self.status in (
            BusinessCutoverInstallReconcileStatus.CONFLICT,
            BusinessCutoverInstallReconcileStatus.CORRUPT,
            BusinessCutoverInstallReconcileStatus.UNKNOWN,
        )
        if (
            (exact and type(self.evidence) is not BusinessCutoverInstallEvidence)
            or (not exact and self.evidence is not None)
            or (exact and self.evidence is not None and self.evidence.cutover_id != self.cutover_id)
            or (failed and (type(self.error_code) is not str or not self.error_code))
            or (not failed and self.error_code != "")
        ):
            _fail("business_cutover_install_result_invalid", "$.install_result")

    @property
    def retry_action(self) -> BusinessCutoverRetryAction:
        if self.status is BusinessCutoverInstallReconcileStatus.NOT_INSTALLED:
            return BusinessCutoverRetryAction.APPLY_EXACT
        if self.status is BusinessCutoverInstallReconcileStatus.INSTALLED_READ_ONLY:
            return BusinessCutoverRetryAction.RETURN_EXACT
        if self.status is BusinessCutoverInstallReconcileStatus.UNKNOWN:
            return BusinessCutoverRetryAction.RECONCILE_REQUIRED
        return BusinessCutoverRetryAction.STOP


@dataclass(frozen=True)
class BusinessCutoverJoinResult:
    status: BusinessCutoverJoinReconcileStatus
    cutover_id: str
    evidence: Optional[BusinessCutoverJoinEvidence] = None
    error_code: str = ""

    def __post_init__(self) -> None:
        if type(self.status) is not BusinessCutoverJoinReconcileStatus:
            _fail("business_cutover_join_result_invalid", "$.join_result.status")
        _lower_hex(self.cutover_id, "$.join_result.cutover_id", 64)
        exact = self.status is BusinessCutoverJoinReconcileStatus.JOINED_ACTIVE
        failed = self.status in (
            BusinessCutoverJoinReconcileStatus.CONFLICT,
            BusinessCutoverJoinReconcileStatus.CORRUPT,
            BusinessCutoverJoinReconcileStatus.UNKNOWN,
        )
        if (
            (exact and type(self.evidence) is not BusinessCutoverJoinEvidence)
            or (not exact and self.evidence is not None)
            or (exact and self.evidence is not None and self.evidence.cutover_id != self.cutover_id)
            or (failed and (type(self.error_code) is not str or not self.error_code))
            or (not failed and self.error_code != "")
        ):
            _fail("business_cutover_join_result_invalid", "$.join_result")

    @property
    def retry_action(self) -> BusinessCutoverRetryAction:
        if self.status is BusinessCutoverJoinReconcileStatus.NOT_JOINED:
            return BusinessCutoverRetryAction.APPLY_EXACT
        if self.status is BusinessCutoverJoinReconcileStatus.JOINED_ACTIVE:
            return BusinessCutoverRetryAction.RETURN_EXACT
        if self.status is BusinessCutoverJoinReconcileStatus.UNKNOWN:
            return BusinessCutoverRetryAction.RECONCILE_REQUIRED
        return BusinessCutoverRetryAction.STOP


# This is a private core leaf.  Explicit imports in the future private cutover
# coordinator are deliberate; wildcard and stable SDK export remain disabled.
__all__ = ()
