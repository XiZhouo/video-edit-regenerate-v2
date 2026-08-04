"""Pure values for repository-wide legacy asset staging before cutover.

The legacy ``AssetStore`` is filesystem-backed, but a pure migration contract
must not pretend that a metadata row proves the blob was read.  A future
adapter therefore captures :class:`BusinessCutoverAssetBlobCarrier` values
while the same repository barrier that protects the V5 source proof is held.
Every carrier contains the exact immutable bytes; this leaf has no path,
SQLite, clock, callback, or storage capability of its own.

An issued plan is only an executable expectation.  It is never evidence that
bytes were durably staged.  Only a freshly read, exact
``BusinessCutoverAssetStageEvidence`` may let the future coordinator advance
the file admission from ``legacy`` to ``cutover_pending``.

Different bounded batches may name the same SHA.  The future writer must treat
an already-staged row with the exact same content key, length, media type, and
extension as idempotent reuse and must not materialize or charge the bytes a
second time.  Any disagreement is a conflict, never an overwrite.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Tuple


BUSINESS_CUTOVER_ASSET_STAGE_PROTOCOL = "ze.business-cutover-asset-stage.v1"
BUSINESS_CUTOVER_ASSET_COVERAGE_PROTOCOL = (
    "ze.business-cutover-asset-coverage.v1"
)
BUSINESS_CUTOVER_ASSET_STAGE_MANIFEST_SCHEMA = (
    "ze.business-cutover-asset-stage-manifest.v1"
)
BUSINESS_CUTOVER_ASSET_STAGE_MEMBER_IDENTITY_SCHEMA = (
    "ze.business-cutover-asset-stage-member-identity.v1"
)
BUSINESS_CUTOVER_ASSET_STAGE_MEMBER_MANIFEST_SCHEMA = (
    "ze.business-cutover-asset-stage-member-manifest.v1"
)
BUSINESS_CUTOVER_ASSET_STAGE_MEMBER_METADATA_SCHEMA = (
    "ze.business-cutover-asset-stage-member-metadata.v1"
)
BUSINESS_CUTOVER_ASSET_STAGE_PROJECT_MEMBER_KIND = "project"
BUSINESS_CUTOVER_ASSET_STAGE_BLOB_MEMBER_KIND = "blob"
BUSINESS_CUTOVER_ASSET_STAGE_OCCURRENCE_MEMBER_KIND = "occurrence"
BUSINESS_CUTOVER_ASSET_STAGE_STATUS = "prepared"
BUSINESS_CUTOVER_ASSET_STAGED_STATUS = "staged_exact"
BUSINESS_CUTOVER_ASSET_STAGE_TRANSACTION_PREFIX = (
    "_business-cutover-asset-stage:"
)
BUSINESS_CUTOVER_ASSET_STAGE_TRANSACTION_KIND = (
    "_business_cutover_asset_stage_v1"
)
BUSINESS_CUTOVER_ASSET_COVERAGE_MANIFEST_SCHEMA = (
    "ze.business-cutover-asset-coverage-manifest.v1"
)
BUSINESS_CUTOVER_ASSET_COVERAGE_STATUS = "prepared"
BUSINESS_CUTOVER_ASSET_COVERED_STATUS = "covered_exact"
BUSINESS_CUTOVER_ASSET_COVERAGE_TRANSACTION_PREFIX = (
    "_business-cutover-asset-coverage:"
)
BUSINESS_CUTOVER_ASSET_COVERAGE_TRANSACTION_KIND = (
    "_business_cutover_asset_coverage_v1"
)

DEFAULT_ASSET_MEDIA_TYPE = "application/octet-stream"
DEFAULT_ASSET_EXTENSION = ""

MAX_BUSINESS_CUTOVER_ASSET_REFERENCES = 1_000_000
MAX_BUSINESS_CUTOVER_ASSET_BLOBS = 1_000_000
MAX_BUSINESS_CUTOVER_ASSET_PROJECTS = 10_000
MAX_BUSINESS_CUTOVER_ASSET_STAGE_MEMBER_ORDINAL = 2_999_999
MAX_BUSINESS_CUTOVER_ASSET_BLOB_BYTES = 512 * 1024 * 1024
MAX_BUSINESS_CUTOVER_PROJECT_ASSET_BYTES = 512 * 1024 * 1024
# One byte-bearing plan is a bounded execution batch, never a repository-sized
# in-memory bundle.  Full-repository closure later aggregates byte-free exact
# batch evidence.
MAX_BUSINESS_CUTOVER_ASSET_STAGE_BATCH_BYTES = 512 * 1024 * 1024
MAX_BUSINESS_CUTOVER_ASSET_MANIFEST_BYTES = 512 * 1024 * 1024
MAX_BUSINESS_CUTOVER_ASSET_MANIFEST_NODES = 40_000_000
MAX_BUSINESS_CUTOVER_ASSET_COVERAGE_BATCHES = 100_000
MAX_BUSINESS_CUTOVER_ASSET_COVERAGE_OBSERVED_OCCURRENCES = 4_000_000
MAX_BUSINESS_CUTOVER_ASSET_COVERAGE_OBSERVED_ROWS = 8_000_000
MAX_BUSINESS_CUTOVER_ASSET_COVERAGE_OBSERVED_MANIFEST_BYTES = (
    512 * 1024 * 1024
)
MAX_BUSINESS_CUTOVER_ASSET_TEXT_BYTES = 1024 * 1024
MAX_BUSINESS_CUTOVER_ASSET_ID_BYTES = 1024
MAX_BUSINESS_CUTOVER_ASSET_MIME_BYTES = 255
MAX_BUSINESS_CUTOVER_ASSET_EXTENSION_BYTES = 32
MAX_SQLITE_INTEGER = 2**63 - 1

_LOWER_HEX = frozenset("0123456789abcdef")


class BusinessCutoverAssetContractError(ValueError):
    """Stable payload-free contract failure."""

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
    raise BusinessCutoverAssetContractError(code, path)


def _text(value: Any, path: str, maximum: int) -> str:
    if type(value) is not str or not value:
        _fail("business_cutover_asset_text_invalid", path)
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeEncodeError:
        _fail("business_cutover_asset_text_invalid", path)
        raise AssertionError("unreachable")
    if len(encoded) > maximum:
        _fail("business_cutover_asset_text_budget_exceeded", path)
    return value


def _optional_text(value: Any, path: str, maximum: int) -> str:
    if type(value) is not str:
        _fail("business_cutover_asset_text_invalid", path)
    if value:
        _text(value, path, maximum)
    return value


def _integer(
    value: Any,
    path: str,
    *,
    minimum: int = 0,
    maximum: int = MAX_SQLITE_INTEGER,
) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _fail("business_cutover_asset_integer_invalid", path)
    return value


def _digest(value: Any, path: str) -> bytes:
    if type(value) is not bytes or len(value) != 32:
        _fail("business_cutover_asset_digest_invalid", path)
    return value


def _lower_hex(value: Any, path: str, length: int) -> str:
    if (
        type(value) is not str
        or len(value) != length
        or any(character not in _LOWER_HEX for character in value)
    ):
        _fail("business_cutover_asset_hex_invalid", path)
    return value


class BusinessCutoverAssetStageReconcileStatus(str, Enum):
    NOT_STAGED = "not_staged"
    STAGED_EXACT = "staged_exact"
    CONFLICT = "conflict"
    CORRUPT = "corrupt"
    UNKNOWN = "unknown"


class BusinessCutoverAssetStageRetryAction(str, Enum):
    STAGE_EXACT = "stage_exact"
    RETURN_EXACT = "return_exact"
    STOP = "stop"
    RECONCILE_REQUIRED = "reconcile_required"


class BusinessCutoverAssetCoverageReconcileStatus(str, Enum):
    NOT_COVERED = "not_covered"
    COVERED_EXACT = "covered_exact"
    CONFLICT = "conflict"
    CORRUPT = "corrupt"
    UNKNOWN = "unknown"


class BusinessCutoverAssetCoverageRetryAction(str, Enum):
    PERSIST_EXACT = "persist_exact"
    RETURN_EXACT = "return_exact"
    STOP = "stop"
    RECONCILE_REQUIRED = "reconcile_required"


@dataclass(frozen=True)
class BusinessCutoverAssetStageLimits:
    """Explicit bounded policy frozen into the stage manifest."""

    max_reference_count: int = MAX_BUSINESS_CUTOVER_ASSET_REFERENCES
    max_unique_blob_count: int = MAX_BUSINESS_CUTOVER_ASSET_BLOBS
    max_blob_bytes: int = MAX_BUSINESS_CUTOVER_ASSET_BLOB_BYTES
    max_project_unique_bytes: int = MAX_BUSINESS_CUTOVER_PROJECT_ASSET_BYTES
    max_batch_unique_bytes: int = MAX_BUSINESS_CUTOVER_ASSET_STAGE_BATCH_BYTES
    max_coverage_batch_count: int = MAX_BUSINESS_CUTOVER_ASSET_COVERAGE_BATCHES
    max_coverage_observed_occurrences: int = (
        MAX_BUSINESS_CUTOVER_ASSET_COVERAGE_OBSERVED_OCCURRENCES
    )
    max_coverage_observed_rows: int = (
        MAX_BUSINESS_CUTOVER_ASSET_COVERAGE_OBSERVED_ROWS
    )
    max_coverage_observed_manifest_bytes: int = (
        MAX_BUSINESS_CUTOVER_ASSET_COVERAGE_OBSERVED_MANIFEST_BYTES
    )

    def __post_init__(self) -> None:
        _integer(
            self.max_reference_count,
            "$.limits.max_reference_count",
            minimum=1,
            maximum=MAX_BUSINESS_CUTOVER_ASSET_REFERENCES,
        )
        _integer(
            self.max_unique_blob_count,
            "$.limits.max_unique_blob_count",
            minimum=1,
            maximum=MAX_BUSINESS_CUTOVER_ASSET_BLOBS,
        )
        _integer(
            self.max_blob_bytes,
            "$.limits.max_blob_bytes",
            minimum=1,
            maximum=MAX_BUSINESS_CUTOVER_ASSET_BLOB_BYTES,
        )
        _integer(
            self.max_project_unique_bytes,
            "$.limits.max_project_unique_bytes",
            minimum=1,
            maximum=MAX_BUSINESS_CUTOVER_PROJECT_ASSET_BYTES,
        )
        _integer(
            self.max_batch_unique_bytes,
            "$.limits.max_batch_unique_bytes",
            minimum=1,
            maximum=MAX_BUSINESS_CUTOVER_ASSET_STAGE_BATCH_BYTES,
        )
        _integer(
            self.max_coverage_batch_count,
            "$.limits.max_coverage_batch_count",
            minimum=1,
            maximum=MAX_BUSINESS_CUTOVER_ASSET_COVERAGE_BATCHES,
        )
        _integer(
            self.max_coverage_observed_occurrences,
            "$.limits.max_coverage_observed_occurrences",
            minimum=1,
            maximum=MAX_BUSINESS_CUTOVER_ASSET_COVERAGE_OBSERVED_OCCURRENCES,
        )
        _integer(
            self.max_coverage_observed_rows,
            "$.limits.max_coverage_observed_rows",
            minimum=1,
            maximum=MAX_BUSINESS_CUTOVER_ASSET_COVERAGE_OBSERVED_ROWS,
        )
        _integer(
            self.max_coverage_observed_manifest_bytes,
            "$.limits.max_coverage_observed_manifest_bytes",
            minimum=1,
            maximum=MAX_BUSINESS_CUTOVER_ASSET_COVERAGE_OBSERVED_MANIFEST_BYTES,
        )
        if (
            self.max_blob_bytes > self.max_project_unique_bytes
            or self.max_blob_bytes > self.max_batch_unique_bytes
        ):
            _fail("business_cutover_asset_limits_invalid", "$.limits")


@dataclass(frozen=True)
class BusinessCutoverAssetBlobCarrier:
    """Exact frozen legacy blob bytes plus their canonical metadata.

    The future filesystem adapter must create these carriers by reading under
    the pre-pending repository barrier.  A boolean such as ``available=True``
    cannot inhabit this type and therefore cannot satisfy the issuer.

    A carrier can intentionally recur in another bounded batch.  Its exact
    metadata lets the durable writer distinguish content-addressed reuse from
    an illegal attempt to replace an existing SHA.
    """

    sha256: str
    blob_bytes: bytes = field(repr=False)
    byte_length: int = 0
    media_type: str = DEFAULT_ASSET_MEDIA_TYPE
    extension: str = DEFAULT_ASSET_EXTENSION
    _content_sha256: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        _lower_hex(self.sha256, "$.blob.sha256", 64)
        if type(self.blob_bytes) is not bytes:
            _fail("business_cutover_asset_blob_bytes_invalid", "$.blob.blob_bytes")
        _integer(
            self.byte_length,
            "$.blob.byte_length",
            maximum=MAX_BUSINESS_CUTOVER_ASSET_BLOB_BYTES,
        )
        if len(self.blob_bytes) != self.byte_length:
            _fail("business_cutover_asset_blob_size_mismatch", "$.blob.byte_length")
        # Capture seal: hash once when the frozen carrier is built.  The plan
        # consumer deliberately re-hashes the then-current bytes once more;
        # Python's ``object.__setattr__`` can bypass ordinary frozen guards.
        actual_sha256 = hashlib.sha256(self.blob_bytes).hexdigest()
        object.__setattr__(self, "_content_sha256", actual_sha256)
        if actual_sha256 != self.sha256:
            _fail("business_cutover_asset_blob_hash_mismatch", "$.blob.sha256")
        _text(
            self.media_type,
            "$.blob.media_type",
            MAX_BUSINESS_CUTOVER_ASSET_MIME_BYTES,
        )
        _optional_text(
            self.extension,
            "$.blob.extension",
            MAX_BUSINESS_CUTOVER_ASSET_EXTENSION_BYTES,
        )


@dataclass(frozen=True)
class BusinessCutoverAssetOccurrenceCoverage:
    ordinal: int
    project_ordinal: int
    project_id: str
    generation: int
    business_root_sha256: bytes = field(repr=False)
    projection_sha256: bytes = field(repr=False)
    entity_type: str = ""
    entity_id: str = ""
    entity_deleted: bool = False
    value_path: str = ""
    blob_ordinal: int = 0
    asset_sha256: str = ""
    media_type: str = DEFAULT_ASSET_MEDIA_TYPE
    byte_length: int = 0
    extension: str = DEFAULT_ASSET_EXTENSION


@dataclass(frozen=True)
class BusinessCutoverAssetProjectCoverage:
    project_ordinal: int
    project_id: str
    generation: int
    document_id: str
    business_root_sha256: bytes = field(repr=False)
    projection_sha256: bytes = field(repr=False)
    occurrence_start: int = 0
    occurrence_count: int = 0
    unique_blob_count: int = 0
    unique_blob_bytes: int = 0
    occurrence_sha256: bytes = field(default=b"", repr=False)


@dataclass(frozen=True)
class BusinessCutoverAssetBlobCoverage:
    blob_ordinal: int
    asset_sha256: str
    content_key: str
    media_type: str
    byte_length: int
    extension: str
    occurrence_count: int
    project_count: int


@dataclass(frozen=True)
class BusinessCutoverAssetCoverage:
    project_count: int
    reference_count: int
    unique_blob_count: int
    unique_blob_bytes: int
    referenced_bytes: int
    project_root_sha256: bytes = field(repr=False)
    occurrence_root_sha256: bytes = field(repr=False)
    blob_root_sha256: bytes = field(repr=False)
    coverage_sha256: bytes = field(repr=False)


@dataclass(frozen=True)
class BusinessCutoverAssetStageEvidence:
    """A typed durable observation; construction alone grants no authority.

    The future SQLite reader may build this only from one fresh transaction
    that joins the stage receipt to every named ``asset_blobs`` row and proves
    its exact cutover-stage state, digest identity, length, and metadata.  This
    pure carrier cannot perform that read and callers must not substitute the
    plan's expected value for the durable observation.
    """

    cutover_id: str
    batch_id: str
    transaction_id: str
    receipt_kind: str
    status: str
    repository_epoch: str
    source_admission_revision: int
    source_admission_sha256: bytes = field(repr=False)
    manifest_bytes: bytes = field(repr=False)
    manifest_sha256: bytes = field(repr=False)
    project_count: int = 0
    reference_count: int = 0
    unique_blob_count: int = 0
    unique_blob_bytes: int = 0
    referenced_bytes: int = 0
    project_root_sha256: bytes = field(default=b"", repr=False)
    blob_root_sha256: bytes = field(default=b"", repr=False)
    occurrence_root_sha256: bytes = field(default=b"", repr=False)
    coverage_sha256: bytes = field(default=b"", repr=False)
    projects: Tuple[BusinessCutoverAssetProjectCoverage, ...] = ()
    blobs: Tuple[BusinessCutoverAssetBlobCoverage, ...] = ()
    occurrences: Tuple[BusinessCutoverAssetOccurrenceCoverage, ...] = ()
    limits: BusinessCutoverAssetStageLimits = field(
        default_factory=BusinessCutoverAssetStageLimits
    )

    def __post_init__(self) -> None:
        _lower_hex(self.cutover_id, "$.evidence.cutover_id", 64)
        _lower_hex(self.batch_id, "$.evidence.batch_id", 64)
        _text(
            self.transaction_id,
            "$.evidence.transaction_id",
            MAX_BUSINESS_CUTOVER_ASSET_TEXT_BYTES,
        )
        _text(
            self.receipt_kind,
            "$.evidence.receipt_kind",
            MAX_BUSINESS_CUTOVER_ASSET_TEXT_BYTES,
        )
        _text(self.status, "$.evidence.status", 64)
        _lower_hex(self.repository_epoch, "$.evidence.repository_epoch", 64)
        _integer(
            self.source_admission_revision,
            "$.evidence.source_admission_revision",
            minimum=1,
        )
        _digest(
            self.source_admission_sha256,
            "$.evidence.source_admission_sha256",
        )
        if type(self.manifest_bytes) is not bytes or not self.manifest_bytes:
            _fail("business_cutover_asset_manifest_invalid", "$.evidence.manifest_bytes")
        _digest(self.manifest_sha256, "$.evidence.manifest_sha256")
        _integer(
            self.project_count,
            "$.evidence.project_count",
            maximum=MAX_BUSINESS_CUTOVER_ASSET_PROJECTS,
        )
        _integer(
            self.reference_count,
            "$.evidence.reference_count",
            maximum=MAX_BUSINESS_CUTOVER_ASSET_REFERENCES,
        )
        _integer(
            self.unique_blob_count,
            "$.evidence.unique_blob_count",
            maximum=MAX_BUSINESS_CUTOVER_ASSET_BLOBS,
        )
        _integer(
            self.unique_blob_bytes,
            "$.evidence.unique_blob_bytes",
            maximum=MAX_BUSINESS_CUTOVER_ASSET_STAGE_BATCH_BYTES,
        )
        _integer(
            self.referenced_bytes,
            "$.evidence.referenced_bytes",
            maximum=MAX_SQLITE_INTEGER,
        )
        _digest(self.project_root_sha256, "$.evidence.project_root_sha256")
        _digest(self.blob_root_sha256, "$.evidence.blob_root_sha256")
        _digest(
            self.occurrence_root_sha256,
            "$.evidence.occurrence_root_sha256",
        )
        _digest(self.coverage_sha256, "$.evidence.coverage_sha256")
        if (
            type(self.projects) is not tuple
            or type(self.blobs) is not tuple
            or type(self.occurrences) is not tuple
            or any(
                type(value) is not BusinessCutoverAssetProjectCoverage
                for value in self.projects
            )
            or any(
                type(value) is not BusinessCutoverAssetBlobCoverage
                for value in self.blobs
            )
            or any(
                type(value) is not BusinessCutoverAssetOccurrenceCoverage
                for value in self.occurrences
            )
            or type(self.limits) is not BusinessCutoverAssetStageLimits
        ):
            _fail("business_cutover_asset_evidence_rows_invalid", "$.evidence")


@dataclass(frozen=True)
class BusinessCutoverAssetStageResult:
    status: BusinessCutoverAssetStageReconcileStatus
    cutover_id: str
    evidence: Optional[BusinessCutoverAssetStageEvidence] = None
    error_code: str = ""

    def __post_init__(self) -> None:
        if type(self.status) is not BusinessCutoverAssetStageReconcileStatus:
            _fail("business_cutover_asset_result_invalid", "$.result.status")
        _lower_hex(self.cutover_id, "$.result.cutover_id", 64)
        exact = self.status is BusinessCutoverAssetStageReconcileStatus.STAGED_EXACT
        failed = self.status in (
            BusinessCutoverAssetStageReconcileStatus.CONFLICT,
            BusinessCutoverAssetStageReconcileStatus.CORRUPT,
            BusinessCutoverAssetStageReconcileStatus.UNKNOWN,
        )
        if (
            (exact and type(self.evidence) is not BusinessCutoverAssetStageEvidence)
            or (not exact and self.evidence is not None)
            or (
                exact
                and self.evidence is not None
                and self.evidence.cutover_id != self.cutover_id
            )
            or (failed and (type(self.error_code) is not str or not self.error_code))
            or (not failed and self.error_code != "")
        ):
            _fail("business_cutover_asset_result_invalid", "$.result")

    @property
    def retry_action(self) -> BusinessCutoverAssetStageRetryAction:
        if self.status is BusinessCutoverAssetStageReconcileStatus.NOT_STAGED:
            return BusinessCutoverAssetStageRetryAction.STAGE_EXACT
        if self.status is BusinessCutoverAssetStageReconcileStatus.STAGED_EXACT:
            return BusinessCutoverAssetStageRetryAction.RETURN_EXACT
        if self.status is BusinessCutoverAssetStageReconcileStatus.UNKNOWN:
            return BusinessCutoverAssetStageRetryAction.RECONCILE_REQUIRED
        return BusinessCutoverAssetStageRetryAction.STOP


@dataclass(frozen=True)
class BusinessCutoverAssetCoverageEvidence:
    """Durable repository coverage seal observed after all exact batches."""

    cutover_id: str
    transaction_id: str
    receipt_kind: str
    status: str
    repository_epoch: str
    source_admission_revision: int
    source_admission_sha256: bytes = field(repr=False)
    manifest_bytes: bytes = field(repr=False)
    manifest_sha256: bytes = field(repr=False)
    batch_count: int = 0
    observed_occurrence_count: int = 0
    observed_row_count: int = 0
    observed_manifest_bytes: int = 0
    project_count: int = 0
    reference_count: int = 0
    unique_blob_count: int = 0
    unique_blob_bytes: int = 0
    referenced_bytes: int = 0
    project_root_sha256: bytes = field(default=b"", repr=False)
    blob_root_sha256: bytes = field(default=b"", repr=False)
    occurrence_root_sha256: bytes = field(default=b"", repr=False)
    coverage_sha256: bytes = field(default=b"", repr=False)

    def __post_init__(self) -> None:
        _lower_hex(self.cutover_id, "$.coverage_evidence.cutover_id", 64)
        _text(
            self.transaction_id,
            "$.coverage_evidence.transaction_id",
            MAX_BUSINESS_CUTOVER_ASSET_TEXT_BYTES,
        )
        _text(
            self.receipt_kind,
            "$.coverage_evidence.receipt_kind",
            MAX_BUSINESS_CUTOVER_ASSET_TEXT_BYTES,
        )
        _text(self.status, "$.coverage_evidence.status", 64)
        _lower_hex(
            self.repository_epoch,
            "$.coverage_evidence.repository_epoch",
            64,
        )
        _integer(
            self.source_admission_revision,
            "$.coverage_evidence.source_admission_revision",
            minimum=1,
        )
        _digest(
            self.source_admission_sha256,
            "$.coverage_evidence.source_admission_sha256",
        )
        if type(self.manifest_bytes) is not bytes or not self.manifest_bytes:
            _fail(
                "business_cutover_asset_manifest_invalid",
                "$.coverage_evidence.manifest_bytes",
            )
        _digest(self.manifest_sha256, "$.coverage_evidence.manifest_sha256")
        _integer(
            self.batch_count,
            "$.coverage_evidence.batch_count",
            maximum=MAX_BUSINESS_CUTOVER_ASSET_BLOBS,
        )
        _integer(
            self.observed_occurrence_count,
            "$.coverage_evidence.observed_occurrence_count",
            maximum=MAX_BUSINESS_CUTOVER_ASSET_COVERAGE_OBSERVED_OCCURRENCES,
        )
        _integer(
            self.observed_row_count,
            "$.coverage_evidence.observed_row_count",
            maximum=MAX_BUSINESS_CUTOVER_ASSET_COVERAGE_OBSERVED_ROWS,
        )
        _integer(
            self.observed_manifest_bytes,
            "$.coverage_evidence.observed_manifest_bytes",
            maximum=MAX_BUSINESS_CUTOVER_ASSET_COVERAGE_OBSERVED_MANIFEST_BYTES,
        )
        _integer(
            self.project_count,
            "$.coverage_evidence.project_count",
            maximum=MAX_BUSINESS_CUTOVER_ASSET_PROJECTS,
        )
        _integer(
            self.reference_count,
            "$.coverage_evidence.reference_count",
            maximum=MAX_BUSINESS_CUTOVER_ASSET_REFERENCES,
        )
        _integer(
            self.unique_blob_count,
            "$.coverage_evidence.unique_blob_count",
            maximum=MAX_BUSINESS_CUTOVER_ASSET_BLOBS,
        )
        _integer(
            self.unique_blob_bytes,
            "$.coverage_evidence.unique_blob_bytes",
            maximum=MAX_SQLITE_INTEGER,
        )
        _integer(
            self.referenced_bytes,
            "$.coverage_evidence.referenced_bytes",
            maximum=MAX_SQLITE_INTEGER,
        )
        _digest(
            self.project_root_sha256,
            "$.coverage_evidence.project_root_sha256",
        )
        _digest(self.blob_root_sha256, "$.coverage_evidence.blob_root_sha256")
        _digest(
            self.occurrence_root_sha256,
            "$.coverage_evidence.occurrence_root_sha256",
        )
        _digest(self.coverage_sha256, "$.coverage_evidence.coverage_sha256")


@dataclass(frozen=True)
class BusinessCutoverAssetCoverageResult:
    status: BusinessCutoverAssetCoverageReconcileStatus
    cutover_id: str
    evidence: Optional[BusinessCutoverAssetCoverageEvidence] = None
    error_code: str = ""

    def __post_init__(self) -> None:
        if type(self.status) is not BusinessCutoverAssetCoverageReconcileStatus:
            _fail("business_cutover_asset_coverage_result_invalid", "$.result.status")
        _lower_hex(self.cutover_id, "$.result.cutover_id", 64)
        exact = (
            self.status is BusinessCutoverAssetCoverageReconcileStatus.COVERED_EXACT
        )
        failed = self.status in (
            BusinessCutoverAssetCoverageReconcileStatus.CONFLICT,
            BusinessCutoverAssetCoverageReconcileStatus.CORRUPT,
            BusinessCutoverAssetCoverageReconcileStatus.UNKNOWN,
        )
        if (
            (exact and type(self.evidence) is not BusinessCutoverAssetCoverageEvidence)
            or (not exact and self.evidence is not None)
            or (
                exact
                and self.evidence is not None
                and self.evidence.cutover_id != self.cutover_id
            )
            or (failed and (type(self.error_code) is not str or not self.error_code))
            or (not failed and self.error_code != "")
        ):
            _fail("business_cutover_asset_coverage_result_invalid", "$.result")

    @property
    def retry_action(self) -> BusinessCutoverAssetCoverageRetryAction:
        if self.status is BusinessCutoverAssetCoverageReconcileStatus.NOT_COVERED:
            return BusinessCutoverAssetCoverageRetryAction.PERSIST_EXACT
        if self.status is BusinessCutoverAssetCoverageReconcileStatus.COVERED_EXACT:
            return BusinessCutoverAssetCoverageRetryAction.RETURN_EXACT
        if self.status is BusinessCutoverAssetCoverageReconcileStatus.UNKNOWN:
            return BusinessCutoverAssetCoverageRetryAction.RECONCILE_REQUIRED
        return BusinessCutoverAssetCoverageRetryAction.STOP


# Private migration leaf: exact imports only, no stable or wildcard surface.
__all__ = ()
