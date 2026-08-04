"""Pure issuer, closure, and durable reconciliation for cutover asset staging.

The issuer rebuilds the exact V5c cutover plan, verifies every supplied
``BusinessSnapshotSet``, derives every schema-declared asset occurrence, and
hashes the actual frozen legacy blob bytes.  It has no I/O capability.  The
future legacy adapter must capture the byte carriers under the repository
barrier *before* ``cutover_pending``; the future SQLite coordinator must stage
the issued content and freshly classify durable evidence as exact before it
may perform the file admission CAS.

One SHA may occur in several bounded batch manifests.  Repository coverage
coalesces exact content-key/length/metadata matches into one global blob and
one unique-byte charge; a future batch writer must likewise reuse an already
exact staged row instead of writing the blob again.  Any mismatch fails closed.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any, Dict, List, NamedTuple, Optional, Set, Tuple

from asset_reference_projection import (
    AssetReferenceProjection,
    AssetReferenceProjectionError,
    project_asset_references,
    verify_asset_reference_projection,
)
from business_codec import BusinessCodecError, encode_payload_bounded
from business_cutover_asset_contract import (
    BUSINESS_CUTOVER_ASSET_STAGE_BLOB_MEMBER_KIND,
    BUSINESS_CUTOVER_ASSET_STAGE_MANIFEST_SCHEMA,
    BUSINESS_CUTOVER_ASSET_STAGE_MEMBER_IDENTITY_SCHEMA,
    BUSINESS_CUTOVER_ASSET_STAGE_MEMBER_MANIFEST_SCHEMA,
    BUSINESS_CUTOVER_ASSET_STAGE_MEMBER_METADATA_SCHEMA,
    BUSINESS_CUTOVER_ASSET_STAGE_OCCURRENCE_MEMBER_KIND,
    BUSINESS_CUTOVER_ASSET_STAGE_PROJECT_MEMBER_KIND,
    BUSINESS_CUTOVER_ASSET_STAGE_PROTOCOL,
    BUSINESS_CUTOVER_ASSET_STAGE_STATUS,
    BUSINESS_CUTOVER_ASSET_STAGE_TRANSACTION_KIND,
    BUSINESS_CUTOVER_ASSET_STAGE_TRANSACTION_PREFIX,
    BUSINESS_CUTOVER_ASSET_STAGED_STATUS,
    BUSINESS_CUTOVER_ASSET_COVERAGE_MANIFEST_SCHEMA,
    BUSINESS_CUTOVER_ASSET_COVERAGE_PROTOCOL,
    BUSINESS_CUTOVER_ASSET_COVERAGE_STATUS,
    BUSINESS_CUTOVER_ASSET_COVERAGE_TRANSACTION_KIND,
    BUSINESS_CUTOVER_ASSET_COVERAGE_TRANSACTION_PREFIX,
    BUSINESS_CUTOVER_ASSET_COVERED_STATUS,
    MAX_BUSINESS_CUTOVER_ASSET_EXTENSION_BYTES,
    MAX_BUSINESS_CUTOVER_ASSET_BLOBS,
    MAX_BUSINESS_CUTOVER_ASSET_BLOB_BYTES,
    MAX_BUSINESS_CUTOVER_ASSET_ID_BYTES,
    MAX_BUSINESS_CUTOVER_ASSET_MANIFEST_BYTES,
    MAX_BUSINESS_CUTOVER_ASSET_MANIFEST_NODES,
    MAX_BUSINESS_CUTOVER_ASSET_MIME_BYTES,
    MAX_BUSINESS_CUTOVER_ASSET_PROJECTS,
    MAX_BUSINESS_CUTOVER_ASSET_REFERENCES,
    MAX_BUSINESS_CUTOVER_ASSET_STAGE_BATCH_BYTES,
    MAX_BUSINESS_CUTOVER_ASSET_STAGE_MEMBER_ORDINAL,
    MAX_BUSINESS_CUTOVER_ASSET_TEXT_BYTES,
    MAX_SQLITE_INTEGER,
    BusinessCutoverAssetBlobCarrier,
    BusinessCutoverAssetBlobCoverage,
    BusinessCutoverAssetCoverage,
    BusinessCutoverAssetOccurrenceCoverage,
    BusinessCutoverAssetProjectCoverage,
    BusinessCutoverAssetStageEvidence,
    BusinessCutoverAssetStageLimits,
    BusinessCutoverAssetStageReconcileStatus,
    BusinessCutoverAssetStageResult,
    BusinessCutoverAssetCoverageEvidence,
    BusinessCutoverAssetCoverageReconcileStatus,
    BusinessCutoverAssetCoverageResult,
)
from business_cutover_contract import BusinessCutoverAdmission
from business_cutover_integrity import (
    BusinessCutoverPlan,
    BusinessCutoverIntegrityError,
    _require_business_cutover_plan,
)
from business_snapshot import (
    BusinessSnapshotError,
    BusinessSnapshotSet,
    verify_business_snapshot,
)


BUSINESS_CUTOVER_ASSET_PROJECT_HASH_DOMAIN = (
    b"ze.business-cutover-asset-stage.projects.v1"
)
BUSINESS_CUTOVER_ASSET_OCCURRENCE_HASH_DOMAIN = (
    b"ze.business-cutover-asset-stage.occurrences.v1"
)
BUSINESS_CUTOVER_ASSET_BLOB_HASH_DOMAIN = (
    b"ze.business-cutover-asset-stage.blobs.v1"
)
BUSINESS_CUTOVER_ASSET_COVERAGE_HASH_DOMAIN = (
    b"ze.business-cutover-asset-stage.coverage.v1"
)
BUSINESS_CUTOVER_ASSET_MANIFEST_HASH_DOMAIN = (
    b"ze.business-cutover-asset-stage.manifest.v1"
)
BUSINESS_CUTOVER_ASSET_BATCH_ID_HASH_DOMAIN = (
    b"ze.business-cutover-asset-stage.batch-id.v1"
)
BUSINESS_CUTOVER_ASSET_REPOSITORY_COVERAGE_HASH_DOMAIN = (
    b"ze.business-cutover-asset-coverage.repository.v1"
)
BUSINESS_CUTOVER_ASSET_COVERAGE_MANIFEST_HASH_DOMAIN = (
    b"ze.business-cutover-asset-coverage.manifest.v1"
)
BUSINESS_CUTOVER_ASSET_REPOSITORY_MEMBER_HASH_DOMAIN = (
    b"ze.business-cutover-asset-coverage.members.v1"
)
BUSINESS_CUTOVER_ASSET_PROJECT_MEMBER_ID_HASH_DOMAIN = (
    b"ze.business-cutover-asset-stage.member.project.id.v1"
)
BUSINESS_CUTOVER_ASSET_PROJECT_MEMBER_HASH_DOMAIN = (
    b"ze.business-cutover-asset-stage.member.project.value.v1"
)
BUSINESS_CUTOVER_ASSET_PROJECT_MEMBER_METADATA_HASH_DOMAIN = (
    b"ze.business-cutover-asset-stage.member.project.metadata.v1"
)
BUSINESS_CUTOVER_ASSET_BLOB_MEMBER_ID_HASH_DOMAIN = (
    b"ze.business-cutover-asset-stage.member.blob.id.v1"
)
BUSINESS_CUTOVER_ASSET_BLOB_MEMBER_HASH_DOMAIN = (
    b"ze.business-cutover-asset-stage.member.blob.value.v1"
)
BUSINESS_CUTOVER_ASSET_BLOB_MEMBER_METADATA_HASH_DOMAIN = (
    b"ze.business-cutover-asset-stage.member.blob.metadata.v1"
)
BUSINESS_CUTOVER_ASSET_OCCURRENCE_MEMBER_ID_HASH_DOMAIN = (
    b"ze.business-cutover-asset-stage.member.occurrence.id.v1"
)
BUSINESS_CUTOVER_ASSET_OCCURRENCE_MEMBER_HASH_DOMAIN = (
    b"ze.business-cutover-asset-stage.member.occurrence.value.v1"
)
BUSINESS_CUTOVER_ASSET_OCCURRENCE_MEMBER_METADATA_HASH_DOMAIN = (
    b"ze.business-cutover-asset-stage.member.occurrence.metadata.v1"
)

_PLAN_ISSUER = object()
_STAGE_MEMBER_ISSUER = object()
# Capability owned by the future durable reader module.  There is deliberately
# no factory that returns it.  Tests import this private singleton to simulate
# that reader; the production-zero gate constrains all real stamp call sites.
_DURABLE_READER_TOKEN = object()
_FRESH_OBSERVATION_ISSUER = object()
_MIME_TOKEN = frozenset(
    "abcdefghijklmnopqrstuvwxyz0123456789!#$&^_.+-"
)
_EXTENSION_TOKEN = frozenset(
    "abcdefghijklmnopqrstuvwxyz0123456789._-"
)
_LOWER_HEX = frozenset("0123456789abcdef")


class BusinessCutoverAssetIntegrityError(ValueError):
    """Stable payload-free issuer failure."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        if type(code) is not str or not code:
            raise TypeError("code must be a non-empty exact string")
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise BusinessCutoverAssetIntegrityError(code)


def _frame(hasher: Any, value: bytes) -> None:
    hasher.update(len(value).to_bytes(8, "big"))
    hasher.update(value)


def _digest(domain: bytes, *values: bytes) -> bytes:
    hasher = hashlib.sha256()
    _frame(hasher, domain)
    for value in values:
        _frame(hasher, value)
    return hasher.digest()


def _digest_with_sequence(
    domain: bytes,
    prefix: Tuple[bytes, ...],
    sequence: Any,
    suffix: Tuple[bytes, ...] = (),
) -> bytes:
    """Frame a large deterministic sequence without a giant ``*args`` tuple."""

    hasher = hashlib.sha256()
    _frame(hasher, domain)
    for value in prefix:
        _frame(hasher, value)
    for value in sequence:
        _frame(hasher, value)
    for value in suffix:
        _frame(hasher, value)
    return hasher.digest()


def _utf8(value: str, code: str) -> bytes:
    if type(value) is not str:
        _fail(code)
    try:
        return value.encode("utf-8", "strict")
    except UnicodeEncodeError as exc:
        raise BusinessCutoverAssetIntegrityError(code) from exc


def _integer_bytes(value: int) -> bytes:
    return value.to_bytes(8, "big")


def _manifest(value: Any) -> bytes:
    try:
        return encode_payload_bounded(
            value,
            MAX_BUSINESS_CUTOVER_ASSET_MANIFEST_BYTES,
            MAX_BUSINESS_CUTOVER_ASSET_MANIFEST_NODES,
        )
    except BusinessCodecError as exc:
        raise BusinessCutoverAssetIntegrityError(
            "business_cutover_asset_manifest_encoding_failed"
        ) from exc


def _stage_member_integer(
    value: Any,
    code: str,
    *,
    minimum: int = 0,
    maximum: int = MAX_SQLITE_INTEGER,
) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _fail(code)
    return value


def _stage_member_text(
    value: Any,
    code: str,
    *,
    maximum: int = MAX_BUSINESS_CUTOVER_ASSET_ID_BYTES,
    allow_empty: bool = False,
) -> str:
    encoded = _utf8(value, code)
    if (not allow_empty and not encoded) or len(encoded) > maximum:
        _fail(code)
    return value


def _stage_member_digest(value: Any, code: str) -> bytes:
    if type(value) is not bytes or len(value) != 32:
        _fail(code)
    return value


def _stage_member_hex(value: Any, code: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in _LOWER_HEX for character in value)
    ):
        _fail(code)
    return value


def _canonical_media_type(value: Any) -> str:
    if type(value) is not str or not value or value != value.lower():
        _fail("business_cutover_asset_media_type_not_canonical")
    encoded = _utf8(value, "business_cutover_asset_media_type_not_canonical")
    if len(encoded) > MAX_BUSINESS_CUTOVER_ASSET_MIME_BYTES:
        _fail("business_cutover_asset_media_type_not_canonical")
    if not value.isascii() or value.count("/") != 1:
        _fail("business_cutover_asset_media_type_not_canonical")
    major, minor = value.split("/", 1)
    if (
        not major
        or not minor
        or any(character not in _MIME_TOKEN for character in major)
        or any(character not in _MIME_TOKEN for character in minor)
    ):
        _fail("business_cutover_asset_media_type_not_canonical")
    return value


def _canonical_extension(value: Any) -> str:
    if type(value) is not str or value != value.lower():
        _fail("business_cutover_asset_extension_not_canonical")
    encoded = _utf8(value, "business_cutover_asset_extension_not_canonical")
    if len(encoded) > MAX_BUSINESS_CUTOVER_ASSET_EXTENSION_BYTES:
        _fail("business_cutover_asset_extension_not_canonical")
    if not value:
        return value
    if (
        not value.isascii()
        or not value.startswith(".")
        or len(value) == 1
        or ".." in value
        or any(character not in _EXTENSION_TOKEN for character in value[1:])
    ):
        _fail("business_cutover_asset_extension_not_canonical")
    return value


def _merge_known_metadata(
    known: Optional[Any],
    candidate: Optional[Any],
) -> Optional[Any]:
    if candidate is None:
        return known
    if known is not None and known != candidate:
        _fail("business_cutover_asset_reference_metadata_conflict")
    return candidate


class _ReferenceMetadata(NamedTuple):
    media_type: Optional[str]
    byte_length: Optional[int]
    extension: Optional[str]


class _ProjectProjection(NamedTuple):
    member_ordinal: int
    snapshot: BusinessSnapshotSet
    projection: AssetReferenceProjection


class BusinessCutoverAssetStageMemberProjection:
    """One issuer-stamped canonical row for ``business_asset_stage_members``.

    The six public values are the complete writer/reader boundary.  They are
    derived from bounded canonical identity, value, and metadata manifests;
    adapters must compare or persist these values and must not independently
    invent another member-id or digest recipe.
    """

    __slots__ = (
        "member_kind",
        "ordinal",
        "member_id",
        "member_bytes",
        "member_sha256",
        "metadata_sha256",
        "_source",
        "_issuer",
    )

    def __init__(
        self,
        *,
        member_kind: str,
        ordinal: int,
        member_id: str,
        member_bytes: int,
        member_sha256: bytes,
        metadata_sha256: bytes,
        source: Any,
        _issuer: Any,
    ) -> None:
        if _issuer is not _STAGE_MEMBER_ISSUER:
            _fail("business_cutover_asset_stage_member_issuer_invalid")
        object.__setattr__(self, "member_kind", member_kind)
        object.__setattr__(self, "ordinal", ordinal)
        object.__setattr__(self, "member_id", member_id)
        object.__setattr__(self, "member_bytes", member_bytes)
        object.__setattr__(self, "member_sha256", member_sha256)
        object.__setattr__(self, "metadata_sha256", metadata_sha256)
        object.__setattr__(self, "_source", source)
        object.__setattr__(self, "_issuer", _issuer)

    def __setattr__(self, name: str, value: Any) -> None:
        del name, value
        raise AttributeError(
            "BusinessCutoverAssetStageMemberProjection is immutable"
        )

    def __eq__(self, other: Any) -> bool:
        if type(other) is not BusinessCutoverAssetStageMemberProjection:
            return False
        names = (
            "member_kind",
            "ordinal",
            "member_id",
            "member_bytes",
            "member_sha256",
            "metadata_sha256",
        )
        return tuple(getattr(self, name, None) for name in names) == tuple(
            getattr(other, name, None) for name in names
        )

    def __hash__(self) -> int:
        return hash((
            self.member_kind,
            self.ordinal,
            self.member_id,
            self.member_bytes,
            self.member_sha256,
            self.metadata_sha256,
        ))


class _PreparedAssetStage(NamedTuple):
    projects: Tuple[BusinessCutoverAssetProjectCoverage, ...]
    occurrences: Tuple[BusinessCutoverAssetOccurrenceCoverage, ...]
    blobs: Tuple[BusinessCutoverAssetBlobCoverage, ...]
    members: Tuple[BusinessCutoverAssetStageMemberProjection, ...]
    coverage: BusinessCutoverAssetCoverage
    manifest_bytes: bytes
    manifest_sha256: bytes
    expected_evidence: BusinessCutoverAssetStageEvidence


class BusinessCutoverAssetStagePlan:
    """Issuer-stamped immutable executable plan, never durable evidence."""

    __slots__ = (
        "cutover_id",
        "batch_id",
        "transaction_id",
        "source_admission",
        "pending_admission",
        "final_admission",
        "repository_epoch",
        "source_admission_revision",
        "source_admission_sha256",
        "runtime_generation",
        "code_digest",
        "limits",
        "projects",
        "occurrences",
        "blobs",
        "members",
        "coverage",
        "manifest_bytes",
        "manifest_sha256",
        "expected_evidence",
        "protocol",
        "status",
        "_cutover_plan",
        "_business_snapshots",
        "_blob_carriers",
        "_issuer",
    )

    def __init__(
        self,
        cutover_plan: BusinessCutoverPlan,
        prepared: _PreparedAssetStage,
        *,
        business_snapshots: Tuple[BusinessSnapshotSet, ...],
        blob_carriers: Tuple[BusinessCutoverAssetBlobCarrier, ...],
        limits: BusinessCutoverAssetStageLimits,
        _issuer: Any,
    ) -> None:
        if _issuer is not _PLAN_ISSUER:
            _fail("business_cutover_asset_plan_issuer_invalid")
        runtime = cutover_plan.install.runtime
        object.__setattr__(self, "cutover_id", cutover_plan.cutover_id)
        object.__setattr__(
            self, "batch_id", prepared.expected_evidence.batch_id
        )
        object.__setattr__(
            self,
            "transaction_id",
            prepared.expected_evidence.transaction_id,
        )
        object.__setattr__(self, "source_admission", cutover_plan.source_admission)
        object.__setattr__(self, "pending_admission", cutover_plan.pending_admission)
        object.__setattr__(self, "final_admission", cutover_plan.final_admission)
        object.__setattr__(self, "repository_epoch", runtime.repository_epoch)
        object.__setattr__(
            self,
            "source_admission_revision",
            runtime.source_admission_revision,
        )
        object.__setattr__(
            self,
            "source_admission_sha256",
            cutover_plan.closure.source_admission_sha256,
        )
        object.__setattr__(self, "runtime_generation", runtime.runtime_generation)
        object.__setattr__(self, "code_digest", runtime.code_digest)
        object.__setattr__(self, "limits", limits)
        object.__setattr__(self, "projects", prepared.projects)
        object.__setattr__(self, "occurrences", prepared.occurrences)
        object.__setattr__(self, "blobs", prepared.blobs)
        object.__setattr__(self, "members", prepared.members)
        object.__setattr__(self, "coverage", prepared.coverage)
        object.__setattr__(self, "manifest_bytes", prepared.manifest_bytes)
        object.__setattr__(self, "manifest_sha256", prepared.manifest_sha256)
        object.__setattr__(self, "expected_evidence", prepared.expected_evidence)
        object.__setattr__(self, "protocol", BUSINESS_CUTOVER_ASSET_STAGE_PROTOCOL)
        object.__setattr__(self, "status", BUSINESS_CUTOVER_ASSET_STAGE_STATUS)
        object.__setattr__(self, "_cutover_plan", cutover_plan)
        object.__setattr__(self, "_business_snapshots", business_snapshots)
        object.__setattr__(self, "_blob_carriers", blob_carriers)
        object.__setattr__(self, "_issuer", _issuer)

    def __setattr__(self, name: str, value: Any) -> None:
        del name, value
        raise AttributeError("BusinessCutoverAssetStagePlan is immutable")


class _FreshBusinessCutoverAssetStageObservation:
    """Issuer-stamped output of one durable-reader snapshot.

    It is valid only inside the repository barrier in which it was stamped;
    coordinators must not cache or replay the wrapper after releasing it.
    """

    __slots__ = (
        "plan",
        "current_admission",
        "evidence",
        "indeterminate",
        "_reader_token",
        "_issuer",
    )

    def __init__(
        self,
        *,
        plan: BusinessCutoverAssetStagePlan,
        current_admission: BusinessCutoverAdmission,
        evidence: Optional[BusinessCutoverAssetStageEvidence],
        indeterminate: bool,
        _reader_token: Any,
        _issuer: Any,
    ) -> None:
        if (
            _reader_token is not _DURABLE_READER_TOKEN
            or _issuer is not _FRESH_OBSERVATION_ISSUER
        ):
            _fail("business_cutover_asset_fresh_observation_issuer_invalid")
        if (
            type(plan) is not BusinessCutoverAssetStagePlan
            or type(current_admission) is not BusinessCutoverAdmission
            or (
                evidence is not None
                and type(evidence) is not BusinessCutoverAssetStageEvidence
            )
            or type(indeterminate) is not bool
            or (indeterminate and evidence is not None)
        ):
            _fail("business_cutover_asset_fresh_observation_invalid")
        object.__setattr__(self, "plan", plan)
        object.__setattr__(self, "current_admission", current_admission)
        object.__setattr__(self, "evidence", evidence)
        object.__setattr__(self, "indeterminate", indeterminate)
        object.__setattr__(self, "_reader_token", _reader_token)
        object.__setattr__(self, "_issuer", _issuer)

    def __setattr__(self, name: str, value: Any) -> None:
        del name, value
        raise AttributeError("fresh stage observation is immutable")


def _stamp_business_cutover_asset_stage_observation(
    *,
    plan: BusinessCutoverAssetStagePlan,
    current_admission: BusinessCutoverAdmission,
    evidence: Optional[BusinessCutoverAssetStageEvidence],
    indeterminate: bool = False,
    _reader_token: Any,
) -> _FreshBusinessCutoverAssetStageObservation:
    """Private durable-reader seam; no public capability factory exists."""

    if _reader_token is not _DURABLE_READER_TOKEN:
        _fail("business_cutover_asset_durable_reader_token_invalid")
    _require_business_cutover_asset_stage_plan(plan)
    return _FreshBusinessCutoverAssetStageObservation(
        plan=plan,
        current_admission=current_admission,
        evidence=evidence,
        indeterminate=indeterminate,
        _reader_token=_reader_token,
        _issuer=_FRESH_OBSERVATION_ISSUER,
    )


def _require_fresh_stage_observation(
    value: Any,
) -> _FreshBusinessCutoverAssetStageObservation:
    if (
        type(value) is not _FreshBusinessCutoverAssetStageObservation
        or getattr(value, "_reader_token", None) is not _DURABLE_READER_TOKEN
        or getattr(value, "_issuer", None) is not _FRESH_OBSERVATION_ISSUER
        or type(getattr(value, "plan", None))
        is not BusinessCutoverAssetStagePlan
        or type(getattr(value, "current_admission", None))
        is not BusinessCutoverAdmission
        or type(getattr(value, "indeterminate", None)) is not bool
    ):
        _fail("business_cutover_asset_fresh_stage_observation_invalid")
    evidence = getattr(value, "evidence", None)
    if (
        evidence is not None
        and type(evidence) is not BusinessCutoverAssetStageEvidence
    ) or (value.indeterminate and evidence is not None):
        _fail("business_cutover_asset_fresh_stage_observation_invalid")
    return value


def _require_cutover_identity(value: Any) -> BusinessCutoverPlan:
    try:
        plan = _require_business_cutover_plan(value)
    except BusinessCutoverIntegrityError as exc:
        raise BusinessCutoverAssetIntegrityError(
            "business_cutover_asset_cutover_plan_invalid"
        ) from exc
    runtime = plan.install.runtime
    if (
        plan.source_admission.authority_state != "legacy"
        or plan.source_admission.cutover_id != ""
        or plan.pending_admission.authority_state != "cutover_pending"
        or plan.final_admission.authority_state != "sqlite"
        or plan.pending_admission.cutover_id != plan.cutover_id
        or plan.final_admission.cutover_id != plan.cutover_id
        or runtime.repository_epoch != plan.legacy_proof.repository_epoch
        or runtime.source_admission_revision
        != plan.source_admission.revision
        or plan.pending_admission.revision != plan.source_admission.revision + 1
        or plan.final_admission.revision != plan.source_admission.revision + 2
    ):
        _fail("business_cutover_asset_cutover_identity_mismatch")
    return plan


def _verified_project_projections(
    cutover_plan: BusinessCutoverPlan,
    business_snapshots: Any,
    limits: BusinessCutoverAssetStageLimits,
) -> Tuple[_ProjectProjection, ...]:
    if type(business_snapshots) is not tuple or any(
        type(snapshot) is not BusinessSnapshotSet
        for snapshot in business_snapshots
    ):
        _fail("business_cutover_asset_snapshots_invalid")
    live_members = tuple(
        member for member in cutover_plan.members if member.status == "live"
    )
    if len(business_snapshots) != len(live_members):
        _fail("business_cutover_asset_snapshot_count_mismatch")
    output: List[_ProjectProjection] = []
    reference_count = 0
    for member, candidate in zip(live_members, business_snapshots):
        try:
            snapshot = verify_business_snapshot(candidate)
            projection = verify_asset_reference_projection(
                project_asset_references(snapshot)
            )
        except (BusinessSnapshotError, AssetReferenceProjectionError) as exc:
            raise BusinessCutoverAssetIntegrityError(
                "business_cutover_asset_snapshot_invalid"
            ) from exc
        if (
            snapshot.project.project_id != member.project_id
            or snapshot.project.generation != member.generation
            or snapshot.document.document_id != member.document_id
            or snapshot.document.document_version != member.document_version
            or not hmac.compare_digest(
                snapshot.project.snapshot_sha256,
                member.business_root_sha256,
            )
            or not hmac.compare_digest(
                projection.projection_sha256,
                member.asset_projection_sha256,
            )
            or projection.reference_count != member.asset_reference_count
            or projection.unique_reference_count
            != member.unique_asset_reference_count
        ):
            _fail("business_cutover_asset_snapshot_cutover_mismatch")
        reference_count += projection.reference_count
        if reference_count > limits.max_reference_count:
            _fail("business_cutover_asset_reference_budget_exceeded")
        output.append(_ProjectProjection(member.ordinal, snapshot, projection))
    return tuple(output)


def _aggregate_reference_metadata(
    projects: Tuple[_ProjectProjection, ...],
) -> Tuple[Dict[str, _ReferenceMetadata], Dict[str, int], Dict[str, Set[str]]]:
    metadata: Dict[str, _ReferenceMetadata] = {}
    occurrence_counts: Dict[str, int] = {}
    project_ids: Dict[str, Set[str]] = {}
    for project in projects:
        for occurrence in project.projection.occurrences:
            reference = occurrence.reference
            previous = metadata.get(reference.sha256)
            if previous is None:
                previous = _ReferenceMetadata(None, None, None)
            metadata[reference.sha256] = _ReferenceMetadata(
                _merge_known_metadata(previous.media_type, reference.mime),
                _merge_known_metadata(previous.byte_length, reference.size),
                _merge_known_metadata(previous.extension, reference.ext),
            )
            occurrence_counts[reference.sha256] = (
                occurrence_counts.get(reference.sha256, 0) + 1
            )
            project_ids.setdefault(reference.sha256, set()).add(
                project.snapshot.project.project_id
            )
    return metadata, occurrence_counts, project_ids


def _verified_carriers(
    carriers: Any,
    metadata: Dict[str, _ReferenceMetadata],
    limits: BusinessCutoverAssetStageLimits,
) -> Tuple[BusinessCutoverAssetBlobCarrier, ...]:
    if type(carriers) is not tuple or any(
        type(carrier) is not BusinessCutoverAssetBlobCarrier
        for carrier in carriers
    ):
        _fail("business_cutover_asset_blob_carriers_invalid")
    if not carriers:
        _fail("business_cutover_asset_empty_batch_invalid")
    if len(carriers) > limits.max_unique_blob_count:
        _fail("business_cutover_asset_blob_count_budget_exceeded")
    if tuple(carrier.sha256 for carrier in carriers) != tuple(
        sorted(carrier.sha256 for carrier in carriers)
    ) or len({carrier.sha256 for carrier in carriers}) != len(carriers):
        _fail("business_cutover_asset_blob_carrier_order_invalid")
    required = set(metadata)
    observed = {carrier.sha256 for carrier in carriers}
    if observed - required:
        _fail("business_cutover_asset_blob_extra")

    total = 0
    for carrier in carriers:
        actual_length = len(carrier.blob_bytes)
        if actual_length != carrier.byte_length:
            _fail("business_cutover_asset_blob_size_mismatch")
        if actual_length > limits.max_blob_bytes:
            _fail("business_cutover_asset_blob_budget_exceeded")
        total += actual_length
        if total > limits.max_batch_unique_bytes:
            _fail("business_cutover_asset_batch_budget_exceeded")
        # Consumption fence: bounded StagePlan input makes one current-byte
        # hash per issuance/rebuild affordable.  Compare it to both the public
        # identity and the construction-time capture seal so a low-level
        # ``object.__setattr__`` replacement cannot reuse a stale stamp.
        current_sha256 = hashlib.sha256(carrier.blob_bytes).hexdigest()
        if (
            not hmac.compare_digest(current_sha256, carrier.sha256)
            or not hmac.compare_digest(
                current_sha256,
                carrier._content_sha256,
            )
        ):
            _fail("business_cutover_asset_blob_hash_mismatch")
        expected = metadata[carrier.sha256]
        expected_media_type = (
            None
            if expected.media_type is None
            else _canonical_media_type(expected.media_type)
        )
        expected_extension = (
            None
            if expected.extension is None
            else _canonical_extension(expected.extension)
        )
        media_type = _canonical_media_type(carrier.media_type)
        extension = _canonical_extension(carrier.extension)
        if (
            (
                expected_media_type is not None
                and media_type != expected_media_type
            )
            or (
                expected_extension is not None
                and extension != expected_extension
            )
            or (
                expected.byte_length is not None
                and expected.byte_length != actual_length
            )
        ):
            _fail("business_cutover_asset_blob_metadata_mismatch")
    return carriers


def _occurrence_hash(
    occurrence: BusinessCutoverAssetOccurrenceCoverage,
) -> bytes:
    return _digest(
        BUSINESS_CUTOVER_ASSET_OCCURRENCE_HASH_DOMAIN,
        _integer_bytes(occurrence.ordinal),
        _integer_bytes(occurrence.project_ordinal),
        _utf8(occurrence.project_id, "business_cutover_asset_identity_invalid"),
        _integer_bytes(occurrence.generation),
        occurrence.business_root_sha256,
        occurrence.projection_sha256,
        _utf8(occurrence.entity_type, "business_cutover_asset_identity_invalid"),
        _utf8(occurrence.entity_id, "business_cutover_asset_identity_invalid"),
        b"\x01" if occurrence.entity_deleted else b"\x00",
        _utf8(occurrence.value_path, "business_cutover_asset_identity_invalid"),
        _integer_bytes(occurrence.blob_ordinal),
        bytes.fromhex(occurrence.asset_sha256),
        _utf8(occurrence.media_type, "business_cutover_asset_metadata_invalid"),
        _integer_bytes(occurrence.byte_length),
        _utf8(occurrence.extension, "business_cutover_asset_metadata_invalid"),
    )


def _project_root(
    projects: Tuple[BusinessCutoverAssetProjectCoverage, ...],
) -> bytes:
    hasher = hashlib.sha256()
    _frame(hasher, BUSINESS_CUTOVER_ASSET_PROJECT_HASH_DOMAIN)
    _frame(hasher, _integer_bytes(len(projects)))
    for project in projects:
        for value in (
            _integer_bytes(project.project_ordinal),
            _utf8(project.project_id, "business_cutover_asset_identity_invalid"),
            _integer_bytes(project.generation),
            _utf8(project.document_id, "business_cutover_asset_identity_invalid"),
            project.business_root_sha256,
            project.projection_sha256,
            _integer_bytes(project.occurrence_start),
            _integer_bytes(project.occurrence_count),
            _integer_bytes(project.unique_blob_count),
            _integer_bytes(project.unique_blob_bytes),
            project.occurrence_sha256,
        ):
            _frame(hasher, value)
    return hasher.digest()


def _occurrence_root(
    occurrences: Tuple[BusinessCutoverAssetOccurrenceCoverage, ...],
) -> bytes:
    hasher = hashlib.sha256()
    _frame(hasher, BUSINESS_CUTOVER_ASSET_OCCURRENCE_HASH_DOMAIN)
    _frame(hasher, _integer_bytes(len(occurrences)))
    for occurrence in occurrences:
        _frame(hasher, _occurrence_hash(occurrence))
    return hasher.digest()


def _blob_root(
    blobs: Tuple[BusinessCutoverAssetBlobCoverage, ...],
) -> bytes:
    hasher = hashlib.sha256()
    _frame(hasher, BUSINESS_CUTOVER_ASSET_BLOB_HASH_DOMAIN)
    _frame(hasher, _integer_bytes(len(blobs)))
    for blob in blobs:
        for value in (
            _integer_bytes(blob.blob_ordinal),
            bytes.fromhex(blob.asset_sha256),
            _utf8(blob.content_key, "business_cutover_asset_content_key_invalid"),
            _utf8(blob.media_type, "business_cutover_asset_metadata_invalid"),
            _integer_bytes(blob.byte_length),
            _utf8(blob.extension, "business_cutover_asset_metadata_invalid"),
            _integer_bytes(blob.occurrence_count),
            _integer_bytes(blob.project_count),
        ):
            _frame(hasher, value)
    return hasher.digest()


def _project_manifest(value: BusinessCutoverAssetProjectCoverage) -> dict:
    return {
        "project_ordinal": value.project_ordinal,
        "project_id": value.project_id,
        "generation": value.generation,
        "document_id": value.document_id,
        "business_root_sha256": value.business_root_sha256.hex(),
        "projection_sha256": value.projection_sha256.hex(),
        "occurrence_start": value.occurrence_start,
        "occurrence_count": value.occurrence_count,
        "unique_blob_count": value.unique_blob_count,
        "unique_blob_bytes": value.unique_blob_bytes,
        "occurrence_sha256": value.occurrence_sha256.hex(),
    }


def _occurrence_manifest(
    value: BusinessCutoverAssetOccurrenceCoverage,
) -> dict:
    return {
        "ordinal": value.ordinal,
        "project_ordinal": value.project_ordinal,
        "project_id": value.project_id,
        "generation": value.generation,
        "business_root_sha256": value.business_root_sha256.hex(),
        "projection_sha256": value.projection_sha256.hex(),
        "entity_type": value.entity_type,
        "entity_id": value.entity_id,
        "entity_deleted": value.entity_deleted,
        "value_path": value.value_path,
        "blob_ordinal": value.blob_ordinal,
        "asset_sha256": value.asset_sha256,
        "media_type": value.media_type,
        "byte_length": value.byte_length,
        "extension": value.extension,
    }


def _blob_manifest(value: BusinessCutoverAssetBlobCoverage) -> dict:
    return {
        "blob_ordinal": value.blob_ordinal,
        "asset_sha256": value.asset_sha256,
        "content_key": value.content_key,
        "media_type": value.media_type,
        "byte_length": value.byte_length,
        "extension": value.extension,
        "occurrence_count": value.occurrence_count,
        "project_count": value.project_count,
    }


class _StageMemberProjectionParts(NamedTuple):
    member_kind: str
    member_bytes: int
    identity: Dict[str, Any]
    metadata: Dict[str, Any]
    id_domain: bytes
    member_domain: bytes
    metadata_domain: bytes


def _stage_member_projection_parts(
    source: Any,
) -> _StageMemberProjectionParts:
    if type(source) is BusinessCutoverAssetProjectCoverage:
        _stage_member_integer(
            source.project_ordinal,
            "business_cutover_asset_stage_member_project_invalid",
            maximum=MAX_BUSINESS_CUTOVER_ASSET_PROJECTS - 1,
        )
        _stage_member_text(
            source.project_id,
            "business_cutover_asset_stage_member_project_invalid",
        )
        _stage_member_integer(
            source.generation,
            "business_cutover_asset_stage_member_project_invalid",
            minimum=1,
        )
        _stage_member_text(
            source.document_id,
            "business_cutover_asset_stage_member_project_invalid",
        )
        _stage_member_digest(
            source.business_root_sha256,
            "business_cutover_asset_stage_member_project_invalid",
        )
        _stage_member_digest(
            source.projection_sha256,
            "business_cutover_asset_stage_member_project_invalid",
        )
        _stage_member_integer(
            source.occurrence_start,
            "business_cutover_asset_stage_member_project_invalid",
            maximum=MAX_BUSINESS_CUTOVER_ASSET_REFERENCES,
        )
        _stage_member_integer(
            source.occurrence_count,
            "business_cutover_asset_stage_member_project_invalid",
            minimum=1,
            maximum=MAX_BUSINESS_CUTOVER_ASSET_REFERENCES,
        )
        _stage_member_integer(
            source.unique_blob_count,
            "business_cutover_asset_stage_member_project_invalid",
            minimum=1,
            maximum=MAX_BUSINESS_CUTOVER_ASSET_BLOBS,
        )
        _stage_member_integer(
            source.unique_blob_bytes,
            "business_cutover_asset_stage_member_project_invalid",
            maximum=MAX_BUSINESS_CUTOVER_ASSET_STAGE_BATCH_BYTES,
        )
        _stage_member_digest(
            source.occurrence_sha256,
            "business_cutover_asset_stage_member_project_invalid",
        )
        return _StageMemberProjectionParts(
            BUSINESS_CUTOVER_ASSET_STAGE_PROJECT_MEMBER_KIND,
            0,
            {
                "project_id": source.project_id,
                "generation": source.generation,
            },
            _project_manifest(source),
            BUSINESS_CUTOVER_ASSET_PROJECT_MEMBER_ID_HASH_DOMAIN,
            BUSINESS_CUTOVER_ASSET_PROJECT_MEMBER_HASH_DOMAIN,
            BUSINESS_CUTOVER_ASSET_PROJECT_MEMBER_METADATA_HASH_DOMAIN,
        )

    if type(source) is BusinessCutoverAssetBlobCoverage:
        _stage_member_integer(
            source.blob_ordinal,
            "business_cutover_asset_stage_member_blob_invalid",
            maximum=MAX_BUSINESS_CUTOVER_ASSET_BLOBS - 1,
        )
        asset_sha256 = _stage_member_hex(
            source.asset_sha256,
            "business_cutover_asset_stage_member_blob_invalid",
        )
        _stage_member_text(
            source.content_key,
            "business_cutover_asset_stage_member_blob_invalid",
            maximum=MAX_BUSINESS_CUTOVER_ASSET_TEXT_BYTES,
        )
        if source.content_key != "sha256/%s/%s" % (
            asset_sha256[:2],
            asset_sha256,
        ):
            _fail("business_cutover_asset_stage_member_blob_invalid")
        _canonical_media_type(source.media_type)
        _stage_member_integer(
            source.byte_length,
            "business_cutover_asset_stage_member_blob_invalid",
            maximum=MAX_BUSINESS_CUTOVER_ASSET_BLOB_BYTES,
        )
        _canonical_extension(source.extension)
        _stage_member_integer(
            source.occurrence_count,
            "business_cutover_asset_stage_member_blob_invalid",
            minimum=1,
            maximum=MAX_BUSINESS_CUTOVER_ASSET_REFERENCES,
        )
        _stage_member_integer(
            source.project_count,
            "business_cutover_asset_stage_member_blob_invalid",
            minimum=1,
            maximum=MAX_BUSINESS_CUTOVER_ASSET_PROJECTS,
        )
        return _StageMemberProjectionParts(
            BUSINESS_CUTOVER_ASSET_STAGE_BLOB_MEMBER_KIND,
            source.byte_length,
            {"asset_sha256": asset_sha256},
            _blob_manifest(source),
            BUSINESS_CUTOVER_ASSET_BLOB_MEMBER_ID_HASH_DOMAIN,
            BUSINESS_CUTOVER_ASSET_BLOB_MEMBER_HASH_DOMAIN,
            BUSINESS_CUTOVER_ASSET_BLOB_MEMBER_METADATA_HASH_DOMAIN,
        )

    if type(source) is BusinessCutoverAssetOccurrenceCoverage:
        _stage_member_integer(
            source.ordinal,
            "business_cutover_asset_stage_member_occurrence_invalid",
            maximum=MAX_BUSINESS_CUTOVER_ASSET_REFERENCES - 1,
        )
        _stage_member_integer(
            source.project_ordinal,
            "business_cutover_asset_stage_member_occurrence_invalid",
            maximum=MAX_BUSINESS_CUTOVER_ASSET_PROJECTS - 1,
        )
        _stage_member_text(
            source.project_id,
            "business_cutover_asset_stage_member_occurrence_invalid",
        )
        _stage_member_integer(
            source.generation,
            "business_cutover_asset_stage_member_occurrence_invalid",
            minimum=1,
        )
        _stage_member_digest(
            source.business_root_sha256,
            "business_cutover_asset_stage_member_occurrence_invalid",
        )
        _stage_member_digest(
            source.projection_sha256,
            "business_cutover_asset_stage_member_occurrence_invalid",
        )
        _stage_member_text(
            source.entity_type,
            "business_cutover_asset_stage_member_occurrence_invalid",
        )
        _stage_member_text(
            source.entity_id,
            "business_cutover_asset_stage_member_occurrence_invalid",
        )
        if type(source.entity_deleted) is not bool:
            _fail("business_cutover_asset_stage_member_occurrence_invalid")
        _stage_member_text(
            source.value_path,
            "business_cutover_asset_stage_member_occurrence_invalid",
        )
        _stage_member_integer(
            source.blob_ordinal,
            "business_cutover_asset_stage_member_occurrence_invalid",
            maximum=MAX_BUSINESS_CUTOVER_ASSET_BLOBS - 1,
        )
        asset_sha256 = _stage_member_hex(
            source.asset_sha256,
            "business_cutover_asset_stage_member_occurrence_invalid",
        )
        _canonical_media_type(source.media_type)
        _stage_member_integer(
            source.byte_length,
            "business_cutover_asset_stage_member_occurrence_invalid",
            maximum=MAX_BUSINESS_CUTOVER_ASSET_BLOB_BYTES,
        )
        _canonical_extension(source.extension)
        return _StageMemberProjectionParts(
            BUSINESS_CUTOVER_ASSET_STAGE_OCCURRENCE_MEMBER_KIND,
            source.byte_length,
            {
                "project_id": source.project_id,
                "generation": source.generation,
                "entity_type": source.entity_type,
                "entity_id": source.entity_id,
                "entity_deleted": source.entity_deleted,
                "value_path": source.value_path,
                "asset_sha256": asset_sha256,
            },
            _occurrence_manifest(source),
            BUSINESS_CUTOVER_ASSET_OCCURRENCE_MEMBER_ID_HASH_DOMAIN,
            BUSINESS_CUTOVER_ASSET_OCCURRENCE_MEMBER_HASH_DOMAIN,
            BUSINESS_CUTOVER_ASSET_OCCURRENCE_MEMBER_METADATA_HASH_DOMAIN,
        )

    _fail("business_cutover_asset_stage_member_source_invalid")
    raise AssertionError("unreachable")


def _issue_business_cutover_asset_stage_member_projection(
    *,
    source: Any,
    ordinal: int,
) -> BusinessCutoverAssetStageMemberProjection:
    """Project one coverage value into the only legal durable member row."""

    sql_ordinal = _stage_member_integer(
        ordinal,
        "business_cutover_asset_stage_member_ordinal_invalid",
        maximum=MAX_BUSINESS_CUTOVER_ASSET_STAGE_MEMBER_ORDINAL,
    )
    parts = _stage_member_projection_parts(source)
    identity_manifest = _manifest({
        "schema": BUSINESS_CUTOVER_ASSET_STAGE_MEMBER_IDENTITY_SCHEMA,
        "member_kind": parts.member_kind,
        "identity": parts.identity,
    })
    member_id = _digest(parts.id_domain, identity_manifest).hex()
    metadata_manifest = _manifest({
        "schema": BUSINESS_CUTOVER_ASSET_STAGE_MEMBER_METADATA_SCHEMA,
        "member_kind": parts.member_kind,
        "member_id": member_id,
        "metadata": parts.metadata,
    })
    metadata_sha256 = _digest(parts.metadata_domain, metadata_manifest)
    member_manifest = _manifest({
        "schema": BUSINESS_CUTOVER_ASSET_STAGE_MEMBER_MANIFEST_SCHEMA,
        "member_kind": parts.member_kind,
        "ordinal": sql_ordinal,
        "member_id": member_id,
        "member_bytes": parts.member_bytes,
        "metadata_sha256": metadata_sha256.hex(),
        "value": parts.metadata,
    })
    return BusinessCutoverAssetStageMemberProjection(
        member_kind=parts.member_kind,
        ordinal=sql_ordinal,
        member_id=member_id,
        member_bytes=parts.member_bytes,
        member_sha256=_digest(parts.member_domain, member_manifest),
        metadata_sha256=metadata_sha256,
        source=source,
        _issuer=_STAGE_MEMBER_ISSUER,
    )


def _require_business_cutover_asset_stage_member_projection(
    value: Any,
) -> BusinessCutoverAssetStageMemberProjection:
    if (
        type(value) is not BusinessCutoverAssetStageMemberProjection
        or getattr(value, "_issuer", None) is not _STAGE_MEMBER_ISSUER
    ):
        _fail("business_cutover_asset_stage_member_invalid")
    rebuilt = _issue_business_cutover_asset_stage_member_projection(
        source=getattr(value, "_source", None),
        ordinal=getattr(value, "ordinal", None),
    )
    if value != rebuilt:
        _fail("business_cutover_asset_stage_member_changed")
    return value


def _issue_business_cutover_asset_stage_members(
    *,
    projects: Tuple[BusinessCutoverAssetProjectCoverage, ...],
    blobs: Tuple[BusinessCutoverAssetBlobCoverage, ...],
    occurrences: Tuple[BusinessCutoverAssetOccurrenceCoverage, ...],
) -> Tuple[BusinessCutoverAssetStageMemberProjection, ...]:
    """Freeze project, blob, then occurrence rows with per-kind ordinals."""

    if (
        type(projects) is not tuple
        or type(blobs) is not tuple
        or type(occurrences) is not tuple
        or any(
            type(value) is not BusinessCutoverAssetProjectCoverage
            for value in projects
        )
        or any(
            type(value) is not BusinessCutoverAssetBlobCoverage
            for value in blobs
        )
        or any(
            type(value) is not BusinessCutoverAssetOccurrenceCoverage
            for value in occurrences
        )
    ):
        _fail("business_cutover_asset_stage_members_invalid")
    if (
        len(projects) > MAX_BUSINESS_CUTOVER_ASSET_PROJECTS
        or len(blobs) > MAX_BUSINESS_CUTOVER_ASSET_BLOBS
        or len(occurrences) > MAX_BUSINESS_CUTOVER_ASSET_REFERENCES
    ):
        _fail("business_cutover_asset_stage_members_budget_exceeded")
    if (
        tuple(value.project_ordinal for value in projects)
        != tuple(sorted(value.project_ordinal for value in projects))
        or len({value.project_ordinal for value in projects}) != len(projects)
        or tuple(value.blob_ordinal for value in blobs)
        != tuple(range(len(blobs)))
        or tuple(value.ordinal for value in occurrences)
        != tuple(range(len(occurrences)))
    ):
        _fail("business_cutover_asset_stage_members_order_invalid")
    members = tuple(
        _issue_business_cutover_asset_stage_member_projection(
            source=source,
            ordinal=ordinal,
        )
        for sources in (projects, blobs, occurrences)
        for ordinal, source in enumerate(sources)
    )
    identities = {
        (member.member_kind, member.member_id) for member in members
    }
    if len(identities) != len(members):
        _fail("business_cutover_asset_stage_member_identity_conflict")
    return members


def _require_business_cutover_asset_stage_members(
    value: Any,
) -> Tuple[BusinessCutoverAssetStageMemberProjection, ...]:
    if type(value) is not tuple or any(
        type(member) is not BusinessCutoverAssetStageMemberProjection
        for member in value
    ):
        _fail("business_cutover_asset_stage_members_invalid")
    for member in value:
        _require_business_cutover_asset_stage_member_projection(member)
    projects = tuple(
        member._source
        for member in value
        if type(member._source) is BusinessCutoverAssetProjectCoverage
    )
    blobs = tuple(
        member._source
        for member in value
        if type(member._source) is BusinessCutoverAssetBlobCoverage
    )
    occurrences = tuple(
        member._source
        for member in value
        if type(member._source) is BusinessCutoverAssetOccurrenceCoverage
    )
    if len(projects) + len(blobs) + len(occurrences) != len(value):
        _fail("business_cutover_asset_stage_member_source_invalid")
    rebuilt = _issue_business_cutover_asset_stage_members(
        projects=projects,
        blobs=blobs,
        occurrences=occurrences,
    )
    if value != rebuilt:
        _fail("business_cutover_asset_stage_members_changed")
    return value


def _prepare(
    *,
    cutover_plan: Any,
    business_snapshots: Any,
    blob_carriers: Any,
    limits: Any,
) -> _PreparedAssetStage:
    cutover = _require_cutover_identity(cutover_plan)
    if type(limits) is not BusinessCutoverAssetStageLimits:
        _fail("business_cutover_asset_limits_invalid")
    projects = _verified_project_projections(cutover, business_snapshots, limits)
    metadata, occurrence_counts, referencing_projects = (
        _aggregate_reference_metadata(projects)
    )
    if len(metadata) > limits.max_unique_blob_count:
        _fail("business_cutover_asset_blob_count_budget_exceeded")
    carriers = _verified_carriers(blob_carriers, metadata, limits)
    carrier_by_sha = {carrier.sha256: carrier for carrier in carriers}
    blob_ordinal_by_sha = {
        carrier.sha256: ordinal for ordinal, carrier in enumerate(carriers)
    }

    occurrences_list: List[BusinessCutoverAssetOccurrenceCoverage] = []
    project_coverages: List[BusinessCutoverAssetProjectCoverage] = []
    referenced_bytes = 0
    for projected in projects:
        start = len(occurrences_list)
        project_shas: Set[str] = set()
        project_occurrence_hashes: List[bytes] = []
        for occurrence in projected.projection.occurrences:
            if occurrence.reference.sha256 not in carrier_by_sha:
                continue
            carrier = carrier_by_sha[occurrence.reference.sha256]
            coverage = BusinessCutoverAssetOccurrenceCoverage(
                ordinal=len(occurrences_list),
                project_ordinal=projected.member_ordinal,
                project_id=projected.snapshot.project.project_id,
                generation=projected.snapshot.project.generation,
                business_root_sha256=projected.snapshot.project.snapshot_sha256,
                projection_sha256=projected.projection.projection_sha256,
                entity_type=occurrence.entity_type,
                entity_id=occurrence.entity_id,
                entity_deleted=occurrence.entity_deleted,
                value_path=occurrence.value_path,
                blob_ordinal=blob_ordinal_by_sha[carrier.sha256],
                asset_sha256=carrier.sha256,
                media_type=carrier.media_type,
                byte_length=carrier.byte_length,
                extension=carrier.extension,
            )
            occurrences_list.append(coverage)
            project_occurrence_hashes.append(_occurrence_hash(coverage))
            project_shas.add(carrier.sha256)
            referenced_bytes += carrier.byte_length
        project_unique_bytes = sum(
            carrier_by_sha[sha256].byte_length for sha256 in project_shas
        )
        if project_unique_bytes > limits.max_project_unique_bytes:
            _fail("business_cutover_asset_project_budget_exceeded")
        occurrence_digest = _digest_with_sequence(
            BUSINESS_CUTOVER_ASSET_OCCURRENCE_HASH_DOMAIN,
            (
                _utf8(
                    projected.snapshot.project.project_id,
                    "business_cutover_asset_identity_invalid",
                ),
                projected.projection.projection_sha256,
                _integer_bytes(len(project_occurrence_hashes)),
            ),
            project_occurrence_hashes,
        )
        if project_occurrence_hashes:
            project_coverages.append(BusinessCutoverAssetProjectCoverage(
                project_ordinal=projected.member_ordinal,
                project_id=projected.snapshot.project.project_id,
                generation=projected.snapshot.project.generation,
                document_id=projected.snapshot.document.document_id,
                business_root_sha256=projected.snapshot.project.snapshot_sha256,
                projection_sha256=projected.projection.projection_sha256,
                occurrence_start=start,
                occurrence_count=len(project_occurrence_hashes),
                unique_blob_count=len(project_shas),
                unique_blob_bytes=project_unique_bytes,
                occurrence_sha256=occurrence_digest,
            ))

    blob_coverages = tuple(
        BusinessCutoverAssetBlobCoverage(
            blob_ordinal=ordinal,
            asset_sha256=carrier.sha256,
            content_key="sha256/%s/%s" % (carrier.sha256[:2], carrier.sha256),
            media_type=carrier.media_type,
            byte_length=carrier.byte_length,
            extension=carrier.extension,
            occurrence_count=occurrence_counts[carrier.sha256],
            project_count=len(referencing_projects[carrier.sha256]),
        )
        for ordinal, carrier in enumerate(carriers)
    )
    frozen_projects = tuple(project_coverages)
    frozen_occurrences = tuple(occurrences_list)
    frozen_members = _issue_business_cutover_asset_stage_members(
        projects=frozen_projects,
        blobs=blob_coverages,
        occurrences=frozen_occurrences,
    )
    project_root = _project_root(frozen_projects)
    occurrence_root = _occurrence_root(frozen_occurrences)
    blob_root = _blob_root(blob_coverages)
    unique_blob_bytes = sum(carrier.byte_length for carrier in carriers)
    runtime = cutover.install.runtime
    batch_id = _digest(
        BUSINESS_CUTOVER_ASSET_BATCH_ID_HASH_DOMAIN,
        cutover.cutover_id.encode("ascii"),
        runtime.repository_epoch.encode("ascii"),
        _integer_bytes(runtime.source_admission_revision),
        cutover.closure.source_admission_sha256,
        _integer_bytes(len(blob_coverages)),
        _integer_bytes(unique_blob_bytes),
        blob_root,
        occurrence_root,
    ).hex()
    coverage_sha256 = _digest(
        BUSINESS_CUTOVER_ASSET_COVERAGE_HASH_DOMAIN,
        cutover.cutover_id.encode("ascii"),
        batch_id.encode("ascii"),
        runtime.repository_epoch.encode("ascii"),
        _integer_bytes(runtime.source_admission_revision),
        cutover.closure.source_admission_sha256,
        runtime.runtime_generation.encode("utf-8"),
        runtime.code_digest,
        _integer_bytes(limits.max_reference_count),
        _integer_bytes(limits.max_unique_blob_count),
        _integer_bytes(limits.max_blob_bytes),
        _integer_bytes(limits.max_project_unique_bytes),
        _integer_bytes(limits.max_batch_unique_bytes),
        _integer_bytes(limits.max_coverage_batch_count),
        _integer_bytes(limits.max_coverage_observed_occurrences),
        _integer_bytes(limits.max_coverage_observed_rows),
        _integer_bytes(limits.max_coverage_observed_manifest_bytes),
        _integer_bytes(len(frozen_projects)),
        _integer_bytes(len(frozen_occurrences)),
        _integer_bytes(len(blob_coverages)),
        _integer_bytes(unique_blob_bytes),
        _integer_bytes(referenced_bytes),
        project_root,
        occurrence_root,
        blob_root,
    )
    coverage = BusinessCutoverAssetCoverage(
        project_count=len(frozen_projects),
        reference_count=len(frozen_occurrences),
        unique_blob_count=len(blob_coverages),
        unique_blob_bytes=unique_blob_bytes,
        referenced_bytes=referenced_bytes,
        project_root_sha256=project_root,
        occurrence_root_sha256=occurrence_root,
        blob_root_sha256=blob_root,
        coverage_sha256=coverage_sha256,
    )
    transaction_id = (
        BUSINESS_CUTOVER_ASSET_STAGE_TRANSACTION_PREFIX
        + cutover.cutover_id
        + ":"
        + batch_id
    )
    manifest_bytes = _manifest({
        "schema": BUSINESS_CUTOVER_ASSET_STAGE_MANIFEST_SCHEMA,
        "protocol": BUSINESS_CUTOVER_ASSET_STAGE_PROTOCOL,
        "status": BUSINESS_CUTOVER_ASSET_STAGE_STATUS,
        "transaction": {
            "batch_id": batch_id,
            "transaction_id": transaction_id,
            "receipt_kind": BUSINESS_CUTOVER_ASSET_STAGE_TRANSACTION_KIND,
        },
        "authority": {
            "repository_epoch": runtime.repository_epoch,
            "cutover_id": cutover.cutover_id,
            "source_admission_revision": runtime.source_admission_revision,
            "source_admission_sha256": (
                cutover.closure.source_admission_sha256.hex()
            ),
            "pending_admission_sha256": (
                cutover.closure.pending_admission_sha256.hex()
            ),
            "final_admission_sha256": (
                cutover.closure.final_admission_sha256.hex()
            ),
            "runtime_generation": runtime.runtime_generation,
            "code_digest": runtime.code_digest.hex(),
        },
        "limits": {
            "max_reference_count": limits.max_reference_count,
            "max_unique_blob_count": limits.max_unique_blob_count,
            "max_blob_bytes": limits.max_blob_bytes,
            "max_project_unique_bytes": limits.max_project_unique_bytes,
            "max_batch_unique_bytes": limits.max_batch_unique_bytes,
            "max_coverage_batch_count": limits.max_coverage_batch_count,
            "max_coverage_observed_occurrences": (
                limits.max_coverage_observed_occurrences
            ),
            "max_coverage_observed_rows": limits.max_coverage_observed_rows,
            "max_coverage_observed_manifest_bytes": (
                limits.max_coverage_observed_manifest_bytes
            ),
        },
        "coverage": {
            "project_count": coverage.project_count,
            "reference_count": coverage.reference_count,
            "unique_blob_count": coverage.unique_blob_count,
            "unique_blob_bytes": coverage.unique_blob_bytes,
            "referenced_bytes": coverage.referenced_bytes,
            "project_root_sha256": coverage.project_root_sha256.hex(),
            "occurrence_root_sha256": coverage.occurrence_root_sha256.hex(),
            "blob_root_sha256": coverage.blob_root_sha256.hex(),
            "coverage_sha256": coverage.coverage_sha256.hex(),
        },
        "projects": [_project_manifest(project) for project in frozen_projects],
        "blobs": [_blob_manifest(blob) for blob in blob_coverages],
        "occurrences": [
            _occurrence_manifest(occurrence) for occurrence in frozen_occurrences
        ],
    })
    manifest_sha256 = _digest(
        BUSINESS_CUTOVER_ASSET_MANIFEST_HASH_DOMAIN,
        manifest_bytes,
    )
    expected_evidence = BusinessCutoverAssetStageEvidence(
        cutover_id=cutover.cutover_id,
        batch_id=batch_id,
        transaction_id=transaction_id,
        receipt_kind=BUSINESS_CUTOVER_ASSET_STAGE_TRANSACTION_KIND,
        status=BUSINESS_CUTOVER_ASSET_STAGED_STATUS,
        repository_epoch=runtime.repository_epoch,
        source_admission_revision=runtime.source_admission_revision,
        source_admission_sha256=cutover.closure.source_admission_sha256,
        manifest_bytes=manifest_bytes,
        manifest_sha256=manifest_sha256,
        project_count=coverage.project_count,
        reference_count=coverage.reference_count,
        unique_blob_count=coverage.unique_blob_count,
        unique_blob_bytes=coverage.unique_blob_bytes,
        referenced_bytes=coverage.referenced_bytes,
        project_root_sha256=coverage.project_root_sha256,
        blob_root_sha256=coverage.blob_root_sha256,
        occurrence_root_sha256=coverage.occurrence_root_sha256,
        coverage_sha256=coverage.coverage_sha256,
        projects=frozen_projects,
        blobs=blob_coverages,
        occurrences=frozen_occurrences,
        limits=limits,
    )
    return _PreparedAssetStage(
        frozen_projects,
        frozen_occurrences,
        blob_coverages,
        frozen_members,
        coverage,
        manifest_bytes,
        manifest_sha256,
        expected_evidence,
    )


def _issue_business_cutover_asset_stage_plan(
    *,
    cutover_plan: BusinessCutoverPlan,
    business_snapshots: Tuple[BusinessSnapshotSet, ...],
    blob_carriers: Tuple[BusinessCutoverAssetBlobCarrier, ...],
    limits: Optional[BusinessCutoverAssetStageLimits] = None,
) -> BusinessCutoverAssetStagePlan:
    """Issue from exact snapshot projections and actual frozen legacy bytes."""

    selected_limits = (
        BusinessCutoverAssetStageLimits() if limits is None else limits
    )
    prepared = _prepare(
        cutover_plan=cutover_plan,
        business_snapshots=business_snapshots,
        blob_carriers=blob_carriers,
        limits=selected_limits,
    )
    return BusinessCutoverAssetStagePlan(
        cutover_plan,
        prepared,
        business_snapshots=business_snapshots,
        blob_carriers=blob_carriers,
        limits=selected_limits,
        _issuer=_PLAN_ISSUER,
    )


def _require_business_cutover_asset_stage_plan(
    value: Any,
) -> BusinessCutoverAssetStagePlan:
    """Rebuild actual byte hashes, projections, roots, and manifest."""

    if (
        type(value) is not BusinessCutoverAssetStagePlan
        or getattr(value, "_issuer", None) is not _PLAN_ISSUER
        or getattr(value, "protocol", None)
        != BUSINESS_CUTOVER_ASSET_STAGE_PROTOCOL
        or getattr(value, "status", None) != BUSINESS_CUTOVER_ASSET_STAGE_STATUS
    ):
        _fail("business_cutover_asset_plan_invalid")
    _require_business_cutover_asset_stage_members(
        getattr(value, "members", None)
    )
    prepared = _prepare(
        cutover_plan=value._cutover_plan,
        business_snapshots=value._business_snapshots,
        blob_carriers=value._blob_carriers,
        limits=value.limits,
    )
    cutover = _require_cutover_identity(value._cutover_plan)
    runtime = cutover.install.runtime
    if (
        value.cutover_id != cutover.cutover_id
        or value.batch_id != prepared.expected_evidence.batch_id
        or value.transaction_id
        != prepared.expected_evidence.transaction_id
        or value.source_admission != cutover.source_admission
        or value.pending_admission != cutover.pending_admission
        or value.final_admission != cutover.final_admission
        or value.repository_epoch != runtime.repository_epoch
        or value.source_admission_revision != runtime.source_admission_revision
        or not hmac.compare_digest(
            value.source_admission_sha256,
            cutover.closure.source_admission_sha256,
        )
        or value.runtime_generation != runtime.runtime_generation
        or not hmac.compare_digest(value.code_digest, runtime.code_digest)
        or value.projects != prepared.projects
        or value.occurrences != prepared.occurrences
        or value.blobs != prepared.blobs
        or value.members != prepared.members
        or value.coverage != prepared.coverage
        or value.manifest_bytes != prepared.manifest_bytes
        or not hmac.compare_digest(
            value.manifest_sha256,
            prepared.manifest_sha256,
        )
        or value.expected_evidence != prepared.expected_evidence
    ):
        _fail("business_cutover_asset_plan_changed")
    return value


def _classify_business_cutover_asset_stage(
    plan: BusinessCutoverAssetStagePlan,
    observation: _FreshBusinessCutoverAssetStageObservation,
) -> BusinessCutoverAssetStageResult:
    """Classify one *fresh* durable read against exact file admission.

    ``STAGED_EXACT`` closes only this bounded byte batch.  It never authorizes
    ``legacy -> cutover_pending``; the independent repository coverage seal
    must first aggregate every exact batch and itself be freshly classified.
    """

    plan = _require_business_cutover_asset_stage_plan(plan)
    fresh = _require_fresh_stage_observation(observation)
    if fresh.plan is not plan:
        _fail("business_cutover_asset_fresh_observation_plan_mismatch")
    current_admission = fresh.current_admission
    observed_evidence = fresh.evidence
    indeterminate = fresh.indeterminate
    if current_admission == plan.source_admission:
        admission_position = "source"
    elif current_admission == plan.pending_admission:
        admission_position = "pending"
    elif current_admission == plan.final_admission:
        admission_position = "final"
    else:
        admission_position = "other"
    if admission_position == "other":
        return BusinessCutoverAssetStageResult(
            BusinessCutoverAssetStageReconcileStatus.CONFLICT,
            plan.cutover_id,
            error_code="business_cutover_asset_file_admission_conflict",
        )
    if indeterminate:
        return BusinessCutoverAssetStageResult(
            BusinessCutoverAssetStageReconcileStatus.UNKNOWN,
            plan.cutover_id,
            error_code="business_cutover_asset_observation_indeterminate",
        )
    if observed_evidence is None:
        if admission_position == "source":
            return BusinessCutoverAssetStageResult(
                BusinessCutoverAssetStageReconcileStatus.NOT_STAGED,
                plan.cutover_id,
            )
        return BusinessCutoverAssetStageResult(
            BusinessCutoverAssetStageReconcileStatus.CORRUPT,
            plan.cutover_id,
            error_code="business_cutover_asset_pending_without_stage_evidence",
        )
    if (
        observed_evidence.cutover_id != plan.cutover_id
        or observed_evidence.batch_id != plan.batch_id
        or observed_evidence.transaction_id != plan.transaction_id
        or observed_evidence.repository_epoch != plan.repository_epoch
        or observed_evidence.source_admission_revision
        != plan.source_admission_revision
    ):
        return BusinessCutoverAssetStageResult(
            BusinessCutoverAssetStageReconcileStatus.CONFLICT,
            plan.cutover_id,
            error_code="business_cutover_asset_stage_identity_conflict",
        )
    observed_digest = _digest(
        BUSINESS_CUTOVER_ASSET_MANIFEST_HASH_DOMAIN,
        observed_evidence.manifest_bytes,
    )
    if (
        not hmac.compare_digest(
            observed_digest,
            observed_evidence.manifest_sha256,
        )
        or observed_evidence != plan.expected_evidence
    ):
        return BusinessCutoverAssetStageResult(
            BusinessCutoverAssetStageReconcileStatus.CORRUPT,
            plan.cutover_id,
            error_code="business_cutover_asset_stage_evidence_mismatch",
        )
    return BusinessCutoverAssetStageResult(
        BusinessCutoverAssetStageReconcileStatus.STAGED_EXACT,
        plan.cutover_id,
        evidence=observed_evidence,
    )


def _exact_integer(value: Any, maximum: int, code: str) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        _fail(code)
    return value


def _exact_digest(value: Any, code: str) -> bytes:
    if type(value) is not bytes or len(value) != 32:
        _fail(code)
    return value


def _exact_sha256(value: Any, code: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        _fail(code)
    return value


def _validate_coverage_rows(
    evidence: BusinessCutoverAssetStageEvidence,
) -> BusinessCutoverAssetStageEvidence:
    """Rebuild all typed row roots in one byte-free durable observation."""

    limits = evidence.limits
    blobs = evidence.blobs
    occurrences = evidence.occurrences
    projects = evidence.projects
    if not blobs or not occurrences or not projects:
        _fail("business_cutover_asset_empty_stage_evidence_invalid")
    if (
        len(blobs) > limits.max_unique_blob_count
        or len(occurrences) > limits.max_reference_count
    ):
        _fail("business_cutover_asset_evidence_budget_exceeded")
    previous_sha: Optional[str] = None
    blob_by_ordinal: Dict[int, BusinessCutoverAssetBlobCoverage] = {}
    unique_bytes = 0
    for ordinal, blob in enumerate(blobs):
        if type(blob) is not BusinessCutoverAssetBlobCoverage:
            _fail("business_cutover_asset_evidence_blob_invalid")
        sha256 = _exact_sha256(
            blob.asset_sha256,
            "business_cutover_asset_evidence_blob_invalid",
        )
        if blob.blob_ordinal != ordinal or (
            previous_sha is not None and sha256 <= previous_sha
        ):
            _fail("business_cutover_asset_evidence_blob_order_invalid")
        previous_sha = sha256
        if blob.content_key != "sha256/%s/%s" % (sha256[:2], sha256):
            _fail("business_cutover_asset_evidence_content_key_invalid")
        _canonical_media_type(blob.media_type)
        _canonical_extension(blob.extension)
        _exact_integer(
            blob.byte_length,
            limits.max_blob_bytes,
            "business_cutover_asset_evidence_blob_size_invalid",
        )
        _exact_integer(
            blob.occurrence_count,
            limits.max_reference_count,
            "business_cutover_asset_evidence_blob_count_invalid",
        )
        _exact_integer(
            blob.project_count,
            MAX_BUSINESS_CUTOVER_ASSET_PROJECTS,
            "business_cutover_asset_evidence_blob_count_invalid",
        )
        if blob.occurrence_count < 1 or blob.project_count < 1:
            _fail("business_cutover_asset_evidence_blob_count_invalid")
        unique_bytes += blob.byte_length
        if unique_bytes > limits.max_batch_unique_bytes:
            _fail("business_cutover_asset_evidence_batch_budget_exceeded")
        blob_by_ordinal[ordinal] = blob

    occurrence_counts: Dict[int, int] = {}
    occurrence_projects: Dict[int, Set[str]] = {}
    for ordinal, occurrence in enumerate(occurrences):
        if type(occurrence) is not BusinessCutoverAssetOccurrenceCoverage:
            _fail("business_cutover_asset_evidence_occurrence_invalid")
        if occurrence.ordinal != ordinal:
            _fail("business_cutover_asset_evidence_occurrence_order_invalid")
        _exact_integer(
            occurrence.project_ordinal,
            9_999,
            "business_cutover_asset_evidence_occurrence_invalid",
        )
        _exact_integer(
            occurrence.blob_ordinal,
            max(0, len(blobs) - 1),
            "business_cutover_asset_evidence_occurrence_invalid",
        )
        if (
            not _utf8(
                occurrence.project_id,
                "business_cutover_asset_evidence_occurrence_invalid",
            )
            or type(occurrence.generation) is not int
            or occurrence.generation < 1
            or not _utf8(
                occurrence.entity_type,
                "business_cutover_asset_evidence_occurrence_invalid",
            )
            or not _utf8(
                occurrence.entity_id,
                "business_cutover_asset_evidence_occurrence_invalid",
            )
            or not _utf8(
                occurrence.value_path,
                "business_cutover_asset_evidence_occurrence_invalid",
            )
            or type(occurrence.entity_deleted) is not bool
        ):
            _fail("business_cutover_asset_evidence_occurrence_invalid")
        _exact_digest(
            occurrence.business_root_sha256,
            "business_cutover_asset_evidence_occurrence_invalid",
        )
        _exact_digest(
            occurrence.projection_sha256,
            "business_cutover_asset_evidence_occurrence_invalid",
        )
        blob = blob_by_ordinal.get(occurrence.blob_ordinal)
        if blob is None or (
            occurrence.asset_sha256 != blob.asset_sha256
            or occurrence.media_type != blob.media_type
            or occurrence.byte_length != blob.byte_length
            or occurrence.extension != blob.extension
        ):
            _fail("business_cutover_asset_evidence_occurrence_blob_mismatch")
        occurrence_counts[blob.blob_ordinal] = (
            occurrence_counts.get(blob.blob_ordinal, 0) + 1
        )
        occurrence_projects.setdefault(blob.blob_ordinal, set()).add(
            occurrence.project_id
        )

    previous_project_ordinal: Optional[int] = None
    expected_start = 0
    for project in projects:
        if type(project) is not BusinessCutoverAssetProjectCoverage:
            _fail("business_cutover_asset_evidence_project_invalid")
        if (
            type(project.project_ordinal) is not int
            or not 0
            <= project.project_ordinal
            < MAX_BUSINESS_CUTOVER_ASSET_PROJECTS
            or (
                previous_project_ordinal is not None
                and project.project_ordinal <= previous_project_ordinal
            )
            or project.occurrence_start != expected_start
            or type(project.occurrence_count) is not int
            or project.occurrence_count < 1
        ):
            _fail("business_cutover_asset_evidence_project_order_invalid")
        if (
            not _utf8(
                project.project_id,
                "business_cutover_asset_evidence_project_invalid",
            )
            or not _utf8(
                project.document_id,
                "business_cutover_asset_evidence_project_invalid",
            )
            or type(project.generation) is not int
            or project.generation < 1
        ):
            _fail("business_cutover_asset_evidence_project_invalid")
        _exact_digest(
            project.business_root_sha256,
            "business_cutover_asset_evidence_project_invalid",
        )
        _exact_digest(
            project.projection_sha256,
            "business_cutover_asset_evidence_project_invalid",
        )
        _exact_digest(
            project.occurrence_sha256,
            "business_cutover_asset_evidence_project_invalid",
        )
        previous_project_ordinal = project.project_ordinal
        end = expected_start + project.occurrence_count
        if end > len(occurrences):
            _fail("business_cutover_asset_evidence_project_range_invalid")
        selected = occurrences[expected_start:end]
        if any(
            occurrence.project_ordinal != project.project_ordinal
            or occurrence.project_id != project.project_id
            or occurrence.generation != project.generation
            or occurrence.business_root_sha256
            != project.business_root_sha256
            or occurrence.projection_sha256 != project.projection_sha256
            for occurrence in selected
        ):
            _fail("business_cutover_asset_evidence_project_range_invalid")
        selected_blob_ordinals = {
            occurrence.blob_ordinal for occurrence in selected
        }
        selected_shas = {
            blobs[blob_ordinal].asset_sha256
            for blob_ordinal in selected_blob_ordinals
        }
        # One pass builds the unique ordinals; one direct lookup per unique
        # blob keeps project validation O(project occurrences), not O(P²).
        selected_bytes = sum(
            blobs[blob_ordinal].byte_length
            for blob_ordinal in selected_blob_ordinals
        )
        project_occurrence_sha256 = _digest_with_sequence(
            BUSINESS_CUTOVER_ASSET_OCCURRENCE_HASH_DOMAIN,
            (
                _utf8(
                    project.project_id,
                    "business_cutover_asset_evidence_project_invalid",
                ),
                project.projection_sha256,
                _integer_bytes(len(selected)),
            ),
            (_occurrence_hash(occurrence) for occurrence in selected),
        )
        if (
            project.unique_blob_count != len(selected_shas)
            or project.unique_blob_bytes != selected_bytes
            or project.unique_blob_bytes > limits.max_project_unique_bytes
            or not hmac.compare_digest(
                project.occurrence_sha256,
                project_occurrence_sha256,
            )
        ):
            _fail("business_cutover_asset_evidence_project_coverage_mismatch")
        expected_start = end
    if expected_start != len(occurrences):
        _fail("business_cutover_asset_evidence_project_range_invalid")
    for blob in blobs:
        if (
            occurrence_counts.get(blob.blob_ordinal, 0)
            != blob.occurrence_count
            or len(occurrence_projects.get(blob.blob_ordinal, set()))
            != blob.project_count
        ):
            _fail("business_cutover_asset_evidence_blob_coverage_mismatch")

    project_root = _project_root(projects)
    occurrence_root = _occurrence_root(occurrences)
    blob_root = _blob_root(blobs)
    referenced_bytes = sum(
        occurrence.byte_length for occurrence in occurrences
    )
    if (
        evidence.project_count != len(projects)
        or evidence.reference_count != len(occurrences)
        or evidence.unique_blob_count != len(blobs)
        or evidence.unique_blob_bytes != unique_bytes
        or evidence.referenced_bytes != referenced_bytes
        or not hmac.compare_digest(evidence.project_root_sha256, project_root)
        or not hmac.compare_digest(
            evidence.occurrence_root_sha256,
            occurrence_root,
        )
        or not hmac.compare_digest(evidence.blob_root_sha256, blob_root)
    ):
        _fail("business_cutover_asset_evidence_coverage_mismatch")
    return evidence


def _stage_manifest_from_evidence(
    cutover: BusinessCutoverPlan,
    evidence: BusinessCutoverAssetStageEvidence,
) -> bytes:
    runtime = cutover.install.runtime
    limits = evidence.limits
    return _manifest({
        "schema": BUSINESS_CUTOVER_ASSET_STAGE_MANIFEST_SCHEMA,
        "protocol": BUSINESS_CUTOVER_ASSET_STAGE_PROTOCOL,
        "status": BUSINESS_CUTOVER_ASSET_STAGE_STATUS,
        "transaction": {
            "batch_id": evidence.batch_id,
            "transaction_id": evidence.transaction_id,
            "receipt_kind": BUSINESS_CUTOVER_ASSET_STAGE_TRANSACTION_KIND,
        },
        "authority": {
            "repository_epoch": runtime.repository_epoch,
            "cutover_id": cutover.cutover_id,
            "source_admission_revision": runtime.source_admission_revision,
            "source_admission_sha256": (
                cutover.closure.source_admission_sha256.hex()
            ),
            "pending_admission_sha256": (
                cutover.closure.pending_admission_sha256.hex()
            ),
            "final_admission_sha256": (
                cutover.closure.final_admission_sha256.hex()
            ),
            "runtime_generation": runtime.runtime_generation,
            "code_digest": runtime.code_digest.hex(),
        },
        "limits": {
            "max_reference_count": limits.max_reference_count,
            "max_unique_blob_count": limits.max_unique_blob_count,
            "max_blob_bytes": limits.max_blob_bytes,
            "max_project_unique_bytes": limits.max_project_unique_bytes,
            "max_batch_unique_bytes": limits.max_batch_unique_bytes,
            "max_coverage_batch_count": limits.max_coverage_batch_count,
            "max_coverage_observed_occurrences": (
                limits.max_coverage_observed_occurrences
            ),
            "max_coverage_observed_rows": limits.max_coverage_observed_rows,
            "max_coverage_observed_manifest_bytes": (
                limits.max_coverage_observed_manifest_bytes
            ),
        },
        "coverage": {
            "project_count": evidence.project_count,
            "reference_count": evidence.reference_count,
            "unique_blob_count": evidence.unique_blob_count,
            "unique_blob_bytes": evidence.unique_blob_bytes,
            "referenced_bytes": evidence.referenced_bytes,
            "project_root_sha256": evidence.project_root_sha256.hex(),
            "occurrence_root_sha256": evidence.occurrence_root_sha256.hex(),
            "blob_root_sha256": evidence.blob_root_sha256.hex(),
            "coverage_sha256": evidence.coverage_sha256.hex(),
        },
        "projects": [_project_manifest(project) for project in evidence.projects],
        "blobs": [_blob_manifest(blob) for blob in evidence.blobs],
        "occurrences": [
            _occurrence_manifest(occurrence)
            for occurrence in evidence.occurrences
        ],
    })


def _validate_stage_evidence_against_cutover(
    cutover: BusinessCutoverPlan,
    value: Any,
) -> BusinessCutoverAssetStageEvidence:
    if type(value) is not BusinessCutoverAssetStageEvidence:
        _fail("business_cutover_asset_evidence_type_invalid")
    evidence = _validate_coverage_rows(value)
    runtime = cutover.install.runtime
    if (
        evidence.cutover_id != cutover.cutover_id
        or evidence.transaction_id
        != BUSINESS_CUTOVER_ASSET_STAGE_TRANSACTION_PREFIX
        + cutover.cutover_id
        + ":"
        + evidence.batch_id
        or evidence.receipt_kind
        != BUSINESS_CUTOVER_ASSET_STAGE_TRANSACTION_KIND
        or evidence.status != BUSINESS_CUTOVER_ASSET_STAGED_STATUS
        or evidence.repository_epoch != runtime.repository_epoch
        or evidence.source_admission_revision
        != runtime.source_admission_revision
        or not hmac.compare_digest(
            evidence.source_admission_sha256,
            cutover.closure.source_admission_sha256,
        )
    ):
        _fail("business_cutover_asset_evidence_identity_mismatch")
    batch_id = _digest(
        BUSINESS_CUTOVER_ASSET_BATCH_ID_HASH_DOMAIN,
        cutover.cutover_id.encode("ascii"),
        runtime.repository_epoch.encode("ascii"),
        _integer_bytes(runtime.source_admission_revision),
        cutover.closure.source_admission_sha256,
        _integer_bytes(evidence.unique_blob_count),
        _integer_bytes(evidence.unique_blob_bytes),
        evidence.blob_root_sha256,
        evidence.occurrence_root_sha256,
    ).hex()
    coverage_sha256 = _digest(
        BUSINESS_CUTOVER_ASSET_COVERAGE_HASH_DOMAIN,
        cutover.cutover_id.encode("ascii"),
        batch_id.encode("ascii"),
        runtime.repository_epoch.encode("ascii"),
        _integer_bytes(runtime.source_admission_revision),
        cutover.closure.source_admission_sha256,
        runtime.runtime_generation.encode("utf-8"),
        runtime.code_digest,
        _integer_bytes(evidence.limits.max_reference_count),
        _integer_bytes(evidence.limits.max_unique_blob_count),
        _integer_bytes(evidence.limits.max_blob_bytes),
        _integer_bytes(evidence.limits.max_project_unique_bytes),
        _integer_bytes(evidence.limits.max_batch_unique_bytes),
        _integer_bytes(evidence.limits.max_coverage_batch_count),
        _integer_bytes(evidence.limits.max_coverage_observed_occurrences),
        _integer_bytes(evidence.limits.max_coverage_observed_rows),
        _integer_bytes(evidence.limits.max_coverage_observed_manifest_bytes),
        _integer_bytes(evidence.project_count),
        _integer_bytes(evidence.reference_count),
        _integer_bytes(evidence.unique_blob_count),
        _integer_bytes(evidence.unique_blob_bytes),
        _integer_bytes(evidence.referenced_bytes),
        evidence.project_root_sha256,
        evidence.occurrence_root_sha256,
        evidence.blob_root_sha256,
    )
    manifest_bytes = _stage_manifest_from_evidence(cutover, evidence)
    manifest_sha256 = _digest(
        BUSINESS_CUTOVER_ASSET_MANIFEST_HASH_DOMAIN,
        manifest_bytes,
    )
    if (
        evidence.batch_id != batch_id
        or not hmac.compare_digest(evidence.coverage_sha256, coverage_sha256)
        or evidence.manifest_bytes != manifest_bytes
        or not hmac.compare_digest(evidence.manifest_sha256, manifest_sha256)
    ):
        _fail("business_cutover_asset_evidence_digest_mismatch")
    return evidence


def _occurrence_identity(
    value: BusinessCutoverAssetOccurrenceCoverage,
) -> Tuple[int, str, int, str, str, bool, str, str]:
    return (
        value.project_ordinal,
        value.project_id,
        value.generation,
        value.entity_type,
        value.entity_id,
        value.entity_deleted,
        value.value_path,
        value.asset_sha256,
    )


def _repository_member_root(cutover: BusinessCutoverPlan) -> bytes:
    """Close live and deleted V5 identities, not only asset-bearing projects."""

    hasher = hashlib.sha256()
    _frame(hasher, BUSINESS_CUTOVER_ASSET_REPOSITORY_MEMBER_HASH_DOMAIN)
    _frame(hasher, cutover.cutover_id.encode("ascii"))
    _frame(hasher, _integer_bytes(len(cutover.members)))
    for member in cutover.members:
        for value in (
            _integer_bytes(member.ordinal),
            member.project_id.encode("utf-8"),
            _integer_bytes(member.generation),
            member.status.encode("ascii"),
            member.operation_id.encode("ascii"),
            member.source_state_sha256,
            _integer_bytes(member.source_state_bytes),
        ):
            _frame(hasher, value)
    return hasher.digest()


def _repository_member_manifest(cutover: BusinessCutoverPlan) -> List[dict]:
    return [
        {
            "ordinal": member.ordinal,
            "project_id": member.project_id,
            "generation": member.generation,
            "status": member.status,
            "operation_id": member.operation_id,
            "source_state_sha256": member.source_state_sha256.hex(),
            "source_state_bytes": member.source_state_bytes,
        }
        for member in cutover.members
    ]


class _PreparedRepositoryCoverage(NamedTuple):
    batch_evidences: Tuple[BusinessCutoverAssetStageEvidence, ...]
    projects: Tuple[BusinessCutoverAssetProjectCoverage, ...]
    occurrences: Tuple[BusinessCutoverAssetOccurrenceCoverage, ...]
    blobs: Tuple[BusinessCutoverAssetBlobCoverage, ...]
    coverage: BusinessCutoverAssetCoverage
    manifest_bytes: bytes
    manifest_sha256: bytes
    expected_evidence: BusinessCutoverAssetCoverageEvidence


class BusinessCutoverAssetCoveragePlan:
    """Byte-free full-repository seal plan over freshly read batch evidence."""

    __slots__ = (
        "cutover_id",
        "transaction_id",
        "source_admission",
        "pending_admission",
        "final_admission",
        "repository_epoch",
        "source_admission_revision",
        "source_admission_sha256",
        "runtime_generation",
        "code_digest",
        "limits",
        "batch_evidences",
        "projects",
        "occurrences",
        "blobs",
        "coverage",
        "manifest_bytes",
        "manifest_sha256",
        "expected_evidence",
        "protocol",
        "status",
        "_cutover_plan",
        "_business_snapshots",
        "_issuer",
    )

    def __init__(
        self,
        cutover: BusinessCutoverPlan,
        prepared: _PreparedRepositoryCoverage,
        *,
        business_snapshots: Tuple[BusinessSnapshotSet, ...],
        limits: BusinessCutoverAssetStageLimits,
        _issuer: Any,
    ) -> None:
        if _issuer is not _PLAN_ISSUER:
            _fail("business_cutover_asset_coverage_plan_issuer_invalid")
        runtime = cutover.install.runtime
        object.__setattr__(self, "cutover_id", cutover.cutover_id)
        object.__setattr__(
            self,
            "transaction_id",
            BUSINESS_CUTOVER_ASSET_COVERAGE_TRANSACTION_PREFIX
            + cutover.cutover_id,
        )
        object.__setattr__(self, "source_admission", cutover.source_admission)
        object.__setattr__(self, "pending_admission", cutover.pending_admission)
        object.__setattr__(self, "final_admission", cutover.final_admission)
        object.__setattr__(self, "repository_epoch", runtime.repository_epoch)
        object.__setattr__(
            self, "source_admission_revision", runtime.source_admission_revision
        )
        object.__setattr__(
            self,
            "source_admission_sha256",
            cutover.closure.source_admission_sha256,
        )
        object.__setattr__(self, "runtime_generation", runtime.runtime_generation)
        object.__setattr__(self, "code_digest", runtime.code_digest)
        object.__setattr__(self, "limits", limits)
        object.__setattr__(self, "batch_evidences", prepared.batch_evidences)
        object.__setattr__(self, "projects", prepared.projects)
        object.__setattr__(self, "occurrences", prepared.occurrences)
        object.__setattr__(self, "blobs", prepared.blobs)
        object.__setattr__(self, "coverage", prepared.coverage)
        object.__setattr__(self, "manifest_bytes", prepared.manifest_bytes)
        object.__setattr__(self, "manifest_sha256", prepared.manifest_sha256)
        object.__setattr__(self, "expected_evidence", prepared.expected_evidence)
        object.__setattr__(
            self, "protocol", BUSINESS_CUTOVER_ASSET_COVERAGE_PROTOCOL
        )
        object.__setattr__(self, "status", BUSINESS_CUTOVER_ASSET_COVERAGE_STATUS)
        object.__setattr__(self, "_cutover_plan", cutover)
        object.__setattr__(self, "_business_snapshots", business_snapshots)
        object.__setattr__(self, "_issuer", _issuer)

    def __setattr__(self, name: str, value: Any) -> None:
        del name, value
        raise AttributeError("BusinessCutoverAssetCoveragePlan is immutable")


class _FreshBusinessCutoverAssetCoverageObservation:
    """Issuer-stamped durable read of the repository coverage receipt.

    It is a same-barrier handoff to the admission CAS, not a cacheable lease.
    """

    __slots__ = (
        "plan",
        "current_admission",
        "evidence",
        "indeterminate",
        "_reader_token",
        "_issuer",
    )

    def __init__(
        self,
        *,
        plan: BusinessCutoverAssetCoveragePlan,
        current_admission: BusinessCutoverAdmission,
        evidence: Optional[BusinessCutoverAssetCoverageEvidence],
        indeterminate: bool,
        _reader_token: Any,
        _issuer: Any,
    ) -> None:
        if (
            _reader_token is not _DURABLE_READER_TOKEN
            or _issuer is not _FRESH_OBSERVATION_ISSUER
        ):
            _fail("business_cutover_asset_fresh_observation_issuer_invalid")
        if (
            type(plan) is not BusinessCutoverAssetCoveragePlan
            or type(current_admission) is not BusinessCutoverAdmission
            or (
                evidence is not None
                and type(evidence) is not BusinessCutoverAssetCoverageEvidence
            )
            or type(indeterminate) is not bool
            or (indeterminate and evidence is not None)
        ):
            _fail("business_cutover_asset_fresh_observation_invalid")
        object.__setattr__(self, "plan", plan)
        object.__setattr__(self, "current_admission", current_admission)
        object.__setattr__(self, "evidence", evidence)
        object.__setattr__(self, "indeterminate", indeterminate)
        object.__setattr__(self, "_reader_token", _reader_token)
        object.__setattr__(self, "_issuer", _issuer)

    def __setattr__(self, name: str, value: Any) -> None:
        del name, value
        raise AttributeError("fresh coverage observation is immutable")


def _stamp_business_cutover_asset_coverage_observation(
    *,
    plan: BusinessCutoverAssetCoveragePlan,
    current_admission: BusinessCutoverAdmission,
    evidence: Optional[BusinessCutoverAssetCoverageEvidence],
    indeterminate: bool = False,
    _reader_token: Any,
) -> _FreshBusinessCutoverAssetCoverageObservation:
    if _reader_token is not _DURABLE_READER_TOKEN:
        _fail("business_cutover_asset_durable_reader_token_invalid")
    _require_business_cutover_asset_coverage_plan(plan)
    return _FreshBusinessCutoverAssetCoverageObservation(
        plan=plan,
        current_admission=current_admission,
        evidence=evidence,
        indeterminate=indeterminate,
        _reader_token=_reader_token,
        _issuer=_FRESH_OBSERVATION_ISSUER,
    )


def _require_fresh_coverage_observation(
    value: Any,
) -> _FreshBusinessCutoverAssetCoverageObservation:
    if (
        type(value) is not _FreshBusinessCutoverAssetCoverageObservation
        or getattr(value, "_reader_token", None) is not _DURABLE_READER_TOKEN
        or getattr(value, "_issuer", None) is not _FRESH_OBSERVATION_ISSUER
        or type(getattr(value, "plan", None))
        is not BusinessCutoverAssetCoveragePlan
        or type(getattr(value, "current_admission", None))
        is not BusinessCutoverAdmission
        or type(getattr(value, "indeterminate", None)) is not bool
    ):
        _fail("business_cutover_asset_fresh_coverage_observation_invalid")
    evidence = getattr(value, "evidence", None)
    if (
        evidence is not None
        and type(evidence) is not BusinessCutoverAssetCoverageEvidence
    ) or (value.indeterminate and evidence is not None):
        _fail("business_cutover_asset_fresh_coverage_observation_invalid")
    return value


def _prepare_repository_coverage(
    *,
    cutover_plan: Any,
    business_snapshots: Any,
    stage_observations: Any = None,
    _rebuild_batch_evidences: Any = None,
    _rebuild_issuer: Any = None,
    limits: Any,
) -> _PreparedRepositoryCoverage:
    cutover = _require_cutover_identity(cutover_plan)
    if type(limits) is not BusinessCutoverAssetStageLimits:
        _fail("business_cutover_asset_limits_invalid")
    issuing = stage_observations is not None
    if issuing:
        if type(stage_observations) is not tuple:
            _fail("business_cutover_asset_stage_observations_invalid")
        source_values = stage_observations
    elif (
        _rebuild_issuer is _PLAN_ISSUER
        and type(_rebuild_batch_evidences) is tuple
    ):
        # Content-only plan verification path.  It cannot issue a new coverage
        # plan and retains no fresh wrapper or raw StagePlan/blob carrier.
        source_values = _rebuild_batch_evidences
    else:
        _fail("business_cutover_asset_stage_observations_invalid")
    if len(source_values) > limits.max_coverage_batch_count:
        _fail("business_cutover_asset_coverage_batch_budget_exceeded")
    if not issuing and any(
        type(value) is not BusinessCutoverAssetStageEvidence
        for value in source_values
    ):
        _fail("business_cutover_asset_batch_evidences_invalid")

    # Cheap stamp/shape and aggregate-size preflight comes before snapshot
    # projection, manifest reconstruction, or per-batch plan verification.
    observed_occurrence_count = 0
    observed_project_row_count = 0
    observed_blob_row_count = 0
    observed_row_count = 0
    observed_manifest_bytes = 0
    raw_evidences: List[BusinessCutoverAssetStageEvidence] = []
    for source in source_values:
        if issuing:
            observation = source
            if (
                type(observation)
                is not _FreshBusinessCutoverAssetStageObservation
                or getattr(observation, "_reader_token", None)
                is not _DURABLE_READER_TOKEN
                or getattr(observation, "_issuer", None)
                is not _FRESH_OBSERVATION_ISSUER
                or type(getattr(observation, "plan", None))
                is not BusinessCutoverAssetStagePlan
                or getattr(observation, "current_admission", None)
                != cutover.source_admission
                or getattr(observation, "indeterminate", None) is not False
                or type(getattr(observation, "evidence", None))
                is not BusinessCutoverAssetStageEvidence
            ):
                _fail("business_cutover_asset_stage_observation_not_exact")
            evidence = observation.evidence
            assert evidence is not None
        else:
            evidence = source
        observed_occurrence_count += len(evidence.occurrences)
        observed_project_row_count += len(evidence.projects)
        observed_blob_row_count += len(evidence.blobs)
        observed_row_count += (
            len(evidence.occurrences)
            + len(evidence.projects)
            + len(evidence.blobs)
        )
        observed_manifest_bytes += len(evidence.manifest_bytes)
        if (
            observed_occurrence_count
            > limits.max_coverage_observed_occurrences
            or observed_row_count > limits.max_coverage_observed_rows
            or observed_manifest_bytes
            > limits.max_coverage_observed_manifest_bytes
        ):
            _fail("business_cutover_asset_coverage_aggregate_budget_exceeded")
        raw_evidences.append(evidence)
    if tuple(value.batch_id for value in raw_evidences) != tuple(
        sorted(value.batch_id for value in raw_evidences)
    ) or len({value.batch_id for value in raw_evidences}) != len(
        raw_evidences
    ):
        _fail("business_cutover_asset_batch_evidence_order_invalid")

    verified_list: List[BusinessCutoverAssetStageEvidence] = []
    if issuing:
        for observation in stage_observations:
            classification = _classify_business_cutover_asset_stage(
                observation.plan,
                observation,
            )
            if (
                classification.status
                is not BusinessCutoverAssetStageReconcileStatus.STAGED_EXACT
                or classification.evidence is None
            ):
                _fail("business_cutover_asset_stage_observation_not_exact")
            verified_list.append(_validate_stage_evidence_against_cutover(
                cutover,
                classification.evidence,
            ))
    else:
        verified_list.extend(
            _validate_stage_evidence_against_cutover(cutover, evidence)
            for evidence in raw_evidences
        )
    verified_batches = tuple(verified_list)
    if any(value.limits != limits for value in verified_batches):
        _fail("business_cutover_asset_batch_limits_mismatch")

    projections = _verified_project_projections(
        cutover, business_snapshots, limits
    )
    reference_metadata, _counts, _project_ids = (
        _aggregate_reference_metadata(projections)
    )

    descriptor_by_sha: Dict[str, BusinessCutoverAssetBlobCoverage] = {}
    evidence_occurrences: Dict[
        Tuple[int, str, int, str, str, bool, str, str],
        BusinessCutoverAssetOccurrenceCoverage,
    ] = {}
    for evidence in verified_batches:
        for blob in evidence.blobs:
            existing_blob = descriptor_by_sha.get(blob.asset_sha256)
            if existing_blob is not None:
                # A shared content-addressed blob may be named by more than one
                # bounded execution batch.  This is exact reuse, not another
                # physical blob: local ordinals/counts may differ, but the
                # immutable content key, length, and canonical metadata may not.
                if (
                    existing_blob.content_key != blob.content_key
                    or existing_blob.media_type != blob.media_type
                    or existing_blob.byte_length != blob.byte_length
                    or existing_blob.extension != blob.extension
                ):
                    _fail("business_cutover_asset_blob_reuse_metadata_conflict")
            else:
                descriptor_by_sha[blob.asset_sha256] = blob
        for occurrence in evidence.occurrences:
            key = _occurrence_identity(occurrence)
            existing_occurrence = evidence_occurrences.get(key)
            if existing_occurrence is not None:
                # The same shared-SHA occurrence is repeated in each batch
                # manifest so that every batch remains independently closed.
                # Coalesce it only when every source/projection/metadata fact is
                # byte-exact; local batch/blob ordinals are intentionally local.
                if (
                    existing_occurrence.business_root_sha256
                    != occurrence.business_root_sha256
                    or existing_occurrence.projection_sha256
                    != occurrence.projection_sha256
                    or existing_occurrence.media_type != occurrence.media_type
                    or existing_occurrence.byte_length != occurrence.byte_length
                    or existing_occurrence.extension != occurrence.extension
                ):
                    _fail("business_cutover_asset_occurrence_reuse_conflict")
            else:
                evidence_occurrences[key] = occurrence

    required_shas = set(reference_metadata)
    observed_shas = set(descriptor_by_sha)
    if required_shas - observed_shas:
        _fail("business_cutover_asset_repository_blob_missing")
    if observed_shas - required_shas:
        _fail("business_cutover_asset_repository_blob_extra")
    if len(observed_shas) > limits.max_unique_blob_count:
        _fail("business_cutover_asset_blob_count_budget_exceeded")
    for sha256, known in reference_metadata.items():
        descriptor = descriptor_by_sha[sha256]
        expected_media_type = (
            None
            if known.media_type is None
            else _canonical_media_type(known.media_type)
        )
        expected_extension = (
            None
            if known.extension is None
            else _canonical_extension(known.extension)
        )
        if (
            (
                expected_media_type is not None
                and descriptor.media_type != expected_media_type
            )
            or (
                expected_extension is not None
                and descriptor.extension != expected_extension
            )
            or (
                known.byte_length is not None
                and descriptor.byte_length != known.byte_length
            )
        ):
            _fail("business_cutover_asset_repository_metadata_mismatch")

    ordered_shas = tuple(sorted(required_shas))
    global_blob_ordinal = {
        sha256: ordinal for ordinal, sha256 in enumerate(ordered_shas)
    }
    normalized_occurrences: List[BusinessCutoverAssetOccurrenceCoverage] = []
    normalized_projects: List[BusinessCutoverAssetProjectCoverage] = []
    expected_keys = set()
    referenced_bytes = 0
    for projected in projections:
        start = len(normalized_occurrences)
        project_shas: Set[str] = set()
        project_hashes: List[bytes] = []
        for expected in projected.projection.occurrences:
            key = (
                projected.member_ordinal,
                projected.snapshot.project.project_id,
                projected.snapshot.project.generation,
                expected.entity_type,
                expected.entity_id,
                expected.entity_deleted,
                expected.value_path,
                expected.reference.sha256,
            )
            expected_keys.add(key)
            observed = evidence_occurrences.get(key)
            if observed is None:
                _fail("business_cutover_asset_repository_occurrence_missing")
            descriptor = descriptor_by_sha[expected.reference.sha256]
            if (
                observed.business_root_sha256
                != projected.snapshot.project.snapshot_sha256
                or observed.projection_sha256
                != projected.projection.projection_sha256
                or observed.media_type != descriptor.media_type
                or observed.byte_length != descriptor.byte_length
                or observed.extension != descriptor.extension
            ):
                _fail("business_cutover_asset_repository_occurrence_mismatch")
            normalized = BusinessCutoverAssetOccurrenceCoverage(
                ordinal=len(normalized_occurrences),
                project_ordinal=projected.member_ordinal,
                project_id=projected.snapshot.project.project_id,
                generation=projected.snapshot.project.generation,
                business_root_sha256=projected.snapshot.project.snapshot_sha256,
                projection_sha256=projected.projection.projection_sha256,
                entity_type=expected.entity_type,
                entity_id=expected.entity_id,
                entity_deleted=expected.entity_deleted,
                value_path=expected.value_path,
                blob_ordinal=global_blob_ordinal[expected.reference.sha256],
                asset_sha256=expected.reference.sha256,
                media_type=descriptor.media_type,
                byte_length=descriptor.byte_length,
                extension=descriptor.extension,
            )
            normalized_occurrences.append(normalized)
            project_hashes.append(_occurrence_hash(normalized))
            project_shas.add(expected.reference.sha256)
            referenced_bytes += descriptor.byte_length
        project_unique_bytes = sum(
            descriptor_by_sha[sha256].byte_length for sha256 in project_shas
        )
        if project_unique_bytes > limits.max_project_unique_bytes:
            _fail("business_cutover_asset_project_budget_exceeded")
        project_digest = _digest_with_sequence(
            BUSINESS_CUTOVER_ASSET_OCCURRENCE_HASH_DOMAIN,
            (
                projected.snapshot.project.project_id.encode("utf-8"),
                projected.projection.projection_sha256,
                _integer_bytes(len(project_hashes)),
            ),
            project_hashes,
        )
        # Every live V5 member is represented, including explicit zero assets.
        normalized_projects.append(BusinessCutoverAssetProjectCoverage(
            project_ordinal=projected.member_ordinal,
            project_id=projected.snapshot.project.project_id,
            generation=projected.snapshot.project.generation,
            document_id=projected.snapshot.document.document_id,
            business_root_sha256=projected.snapshot.project.snapshot_sha256,
            projection_sha256=projected.projection.projection_sha256,
            occurrence_start=start,
            occurrence_count=len(project_hashes),
            unique_blob_count=len(project_shas),
            unique_blob_bytes=project_unique_bytes,
            occurrence_sha256=project_digest,
        ))
    if set(evidence_occurrences) - expected_keys:
        _fail("business_cutover_asset_repository_occurrence_extra")

    occurrence_count_by_sha: Dict[str, int] = {}
    project_ids_by_sha: Dict[str, Set[str]] = {}
    for occurrence in normalized_occurrences:
        occurrence_count_by_sha[occurrence.asset_sha256] = (
            occurrence_count_by_sha.get(occurrence.asset_sha256, 0) + 1
        )
        project_ids_by_sha.setdefault(occurrence.asset_sha256, set()).add(
            occurrence.project_id
        )
    normalized_blobs = tuple(
        BusinessCutoverAssetBlobCoverage(
            blob_ordinal=global_blob_ordinal[sha256],
            asset_sha256=sha256,
            content_key="sha256/%s/%s" % (sha256[:2], sha256),
            media_type=descriptor_by_sha[sha256].media_type,
            byte_length=descriptor_by_sha[sha256].byte_length,
            extension=descriptor_by_sha[sha256].extension,
            occurrence_count=occurrence_count_by_sha[sha256],
            project_count=len(project_ids_by_sha[sha256]),
        )
        for sha256 in ordered_shas
    )
    frozen_projects = tuple(normalized_projects)
    frozen_occurrences = tuple(normalized_occurrences)
    project_root = _project_root(frozen_projects)
    occurrence_root = _occurrence_root(frozen_occurrences)
    blob_root = _blob_root(normalized_blobs)
    unique_blob_bytes = sum(blob.byte_length for blob in normalized_blobs)
    if unique_blob_bytes > 2**63 - 1 or referenced_bytes > 2**63 - 1:
        _fail("business_cutover_asset_repository_bytes_exceeded")
    runtime = cutover.install.runtime
    member_root = _repository_member_root(cutover)
    coverage_sha256 = _digest_with_sequence(
        BUSINESS_CUTOVER_ASSET_REPOSITORY_COVERAGE_HASH_DOMAIN,
        (
            cutover.cutover_id.encode("ascii"),
            runtime.repository_epoch.encode("ascii"),
            _integer_bytes(runtime.source_admission_revision),
            cutover.closure.source_admission_sha256,
            runtime.runtime_generation.encode("utf-8"),
            runtime.code_digest,
            _integer_bytes(cutover.legacy_proof.project_count),
            _integer_bytes(cutover.legacy_proof.live_project_count),
            _integer_bytes(cutover.legacy_proof.deleted_project_count),
            member_root,
            _integer_bytes(len(verified_batches)),
            _integer_bytes(observed_occurrence_count),
            _integer_bytes(observed_project_row_count),
            _integer_bytes(observed_blob_row_count),
            _integer_bytes(observed_row_count),
            _integer_bytes(observed_manifest_bytes),
        ),
        (value.manifest_sha256 for value in verified_batches),
        (
            _integer_bytes(len(frozen_projects)),
            _integer_bytes(len(frozen_occurrences)),
            _integer_bytes(len(normalized_blobs)),
            _integer_bytes(unique_blob_bytes),
            _integer_bytes(referenced_bytes),
            project_root,
            occurrence_root,
            blob_root,
        ),
    )
    coverage = BusinessCutoverAssetCoverage(
        project_count=len(frozen_projects),
        reference_count=len(frozen_occurrences),
        unique_blob_count=len(normalized_blobs),
        unique_blob_bytes=unique_blob_bytes,
        referenced_bytes=referenced_bytes,
        project_root_sha256=project_root,
        occurrence_root_sha256=occurrence_root,
        blob_root_sha256=blob_root,
        coverage_sha256=coverage_sha256,
    )
    transaction_id = (
        BUSINESS_CUTOVER_ASSET_COVERAGE_TRANSACTION_PREFIX + cutover.cutover_id
    )
    manifest_bytes = _manifest({
        "schema": BUSINESS_CUTOVER_ASSET_COVERAGE_MANIFEST_SCHEMA,
        "protocol": BUSINESS_CUTOVER_ASSET_COVERAGE_PROTOCOL,
        "status": BUSINESS_CUTOVER_ASSET_COVERAGE_STATUS,
        "transaction": {
            "transaction_id": transaction_id,
            "receipt_kind": BUSINESS_CUTOVER_ASSET_COVERAGE_TRANSACTION_KIND,
        },
        "authority": {
            "repository_epoch": runtime.repository_epoch,
            "cutover_id": cutover.cutover_id,
            "source_admission_revision": runtime.source_admission_revision,
            "source_admission_sha256": (
                cutover.closure.source_admission_sha256.hex()
            ),
            "pending_admission_sha256": (
                cutover.closure.pending_admission_sha256.hex()
            ),
            "runtime_generation": runtime.runtime_generation,
            "code_digest": runtime.code_digest.hex(),
        },
        "limits": {
            "max_reference_count": limits.max_reference_count,
            "max_unique_blob_count": limits.max_unique_blob_count,
            "max_blob_bytes": limits.max_blob_bytes,
            "max_project_unique_bytes": limits.max_project_unique_bytes,
            "max_batch_unique_bytes": limits.max_batch_unique_bytes,
            "max_coverage_batch_count": limits.max_coverage_batch_count,
            "max_coverage_observed_occurrences": (
                limits.max_coverage_observed_occurrences
            ),
            "max_coverage_observed_rows": limits.max_coverage_observed_rows,
            "max_coverage_observed_manifest_bytes": (
                limits.max_coverage_observed_manifest_bytes
            ),
        },
        "repository": {
            "project_count": cutover.legacy_proof.project_count,
            "live_project_count": cutover.legacy_proof.live_project_count,
            "deleted_project_count": cutover.legacy_proof.deleted_project_count,
            "member_root_sha256": member_root.hex(),
            "members": _repository_member_manifest(cutover),
        },
        "batches": [
            {
                "batch_id": value.batch_id,
                "transaction_id": value.transaction_id,
                "manifest_sha256": value.manifest_sha256.hex(),
                "coverage_sha256": value.coverage_sha256.hex(),
                "reference_count": value.reference_count,
                "unique_blob_count": value.unique_blob_count,
                "unique_blob_bytes": value.unique_blob_bytes,
            }
            for value in verified_batches
        ],
        "observed": {
            "batch_count": len(verified_batches),
            "occurrence_count": observed_occurrence_count,
            "project_row_count": observed_project_row_count,
            "blob_row_count": observed_blob_row_count,
            "row_count": observed_row_count,
            "manifest_bytes": observed_manifest_bytes,
        },
        "coverage": {
            "batch_count": len(verified_batches),
            "project_count": coverage.project_count,
            "reference_count": coverage.reference_count,
            "unique_blob_count": coverage.unique_blob_count,
            "unique_blob_bytes": coverage.unique_blob_bytes,
            "referenced_bytes": coverage.referenced_bytes,
            "project_root_sha256": coverage.project_root_sha256.hex(),
            "occurrence_root_sha256": coverage.occurrence_root_sha256.hex(),
            "blob_root_sha256": coverage.blob_root_sha256.hex(),
            "coverage_sha256": coverage.coverage_sha256.hex(),
        },
        "projects": [_project_manifest(project) for project in frozen_projects],
        "blobs": [_blob_manifest(blob) for blob in normalized_blobs],
        "occurrences": [
            _occurrence_manifest(occurrence)
            for occurrence in frozen_occurrences
        ],
    })
    manifest_sha256 = _digest(
        BUSINESS_CUTOVER_ASSET_COVERAGE_MANIFEST_HASH_DOMAIN,
        manifest_bytes,
    )
    expected_evidence = BusinessCutoverAssetCoverageEvidence(
        cutover_id=cutover.cutover_id,
        transaction_id=transaction_id,
        receipt_kind=BUSINESS_CUTOVER_ASSET_COVERAGE_TRANSACTION_KIND,
        status=BUSINESS_CUTOVER_ASSET_COVERED_STATUS,
        repository_epoch=runtime.repository_epoch,
        source_admission_revision=runtime.source_admission_revision,
        source_admission_sha256=cutover.closure.source_admission_sha256,
        manifest_bytes=manifest_bytes,
        manifest_sha256=manifest_sha256,
        batch_count=len(verified_batches),
        observed_occurrence_count=observed_occurrence_count,
        observed_row_count=observed_row_count,
        observed_manifest_bytes=observed_manifest_bytes,
        project_count=coverage.project_count,
        reference_count=coverage.reference_count,
        unique_blob_count=coverage.unique_blob_count,
        unique_blob_bytes=coverage.unique_blob_bytes,
        referenced_bytes=coverage.referenced_bytes,
        project_root_sha256=coverage.project_root_sha256,
        blob_root_sha256=coverage.blob_root_sha256,
        occurrence_root_sha256=coverage.occurrence_root_sha256,
        coverage_sha256=coverage.coverage_sha256,
    )
    return _PreparedRepositoryCoverage(
        verified_batches,
        frozen_projects,
        frozen_occurrences,
        normalized_blobs,
        coverage,
        manifest_bytes,
        manifest_sha256,
        expected_evidence,
    )


def _issue_business_cutover_asset_coverage_plan(
    *,
    cutover_plan: BusinessCutoverPlan,
    business_snapshots: Tuple[BusinessSnapshotSet, ...],
    stage_observations: Tuple[
        _FreshBusinessCutoverAssetStageObservation, ...
    ],
    limits: Optional[BusinessCutoverAssetStageLimits] = None,
) -> BusinessCutoverAssetCoveragePlan:
    """Issue a byte-free seal only from reader-stamped exact observations."""

    selected_limits = (
        BusinessCutoverAssetStageLimits() if limits is None else limits
    )
    prepared = _prepare_repository_coverage(
        cutover_plan=cutover_plan,
        business_snapshots=business_snapshots,
        stage_observations=stage_observations,
        limits=selected_limits,
    )
    return BusinessCutoverAssetCoveragePlan(
        cutover_plan,
        prepared,
        business_snapshots=business_snapshots,
        limits=selected_limits,
        _issuer=_PLAN_ISSUER,
    )


def _require_business_cutover_asset_coverage_plan(
    value: Any,
) -> BusinessCutoverAssetCoveragePlan:
    if (
        type(value) is not BusinessCutoverAssetCoveragePlan
        or getattr(value, "_issuer", None) is not _PLAN_ISSUER
        or getattr(value, "protocol", None)
        != BUSINESS_CUTOVER_ASSET_COVERAGE_PROTOCOL
        or getattr(value, "status", None)
        != BUSINESS_CUTOVER_ASSET_COVERAGE_STATUS
    ):
        _fail("business_cutover_asset_coverage_plan_invalid")
    prepared = _prepare_repository_coverage(
        cutover_plan=value._cutover_plan,
        business_snapshots=value._business_snapshots,
        _rebuild_batch_evidences=value.batch_evidences,
        _rebuild_issuer=_PLAN_ISSUER,
        limits=value.limits,
    )
    cutover = _require_cutover_identity(value._cutover_plan)
    runtime = cutover.install.runtime
    if (
        value.cutover_id != cutover.cutover_id
        or value.transaction_id
        != BUSINESS_CUTOVER_ASSET_COVERAGE_TRANSACTION_PREFIX
        + cutover.cutover_id
        or value.source_admission != cutover.source_admission
        or value.pending_admission != cutover.pending_admission
        or value.final_admission != cutover.final_admission
        or value.repository_epoch != runtime.repository_epoch
        or value.source_admission_revision != runtime.source_admission_revision
        or value.source_admission_sha256
        != cutover.closure.source_admission_sha256
        or value.runtime_generation != runtime.runtime_generation
        or value.code_digest != runtime.code_digest
        or value.batch_evidences != prepared.batch_evidences
        or value.projects != prepared.projects
        or value.occurrences != prepared.occurrences
        or value.blobs != prepared.blobs
        or value.coverage != prepared.coverage
        or value.manifest_bytes != prepared.manifest_bytes
        or value.manifest_sha256 != prepared.manifest_sha256
        or value.expected_evidence != prepared.expected_evidence
    ):
        _fail("business_cutover_asset_coverage_plan_changed")
    return value


def _classify_business_cutover_asset_coverage(
    plan: BusinessCutoverAssetCoveragePlan,
    observation: _FreshBusinessCutoverAssetCoverageObservation,
) -> BusinessCutoverAssetCoverageResult:
    """Fresh exact coverage is the only asset fact that permits pending CAS."""

    plan = _require_business_cutover_asset_coverage_plan(plan)
    fresh = _require_fresh_coverage_observation(observation)
    if fresh.plan is not plan:
        _fail("business_cutover_asset_fresh_observation_plan_mismatch")
    current_admission = fresh.current_admission
    observed_evidence = fresh.evidence
    indeterminate = fresh.indeterminate
    if current_admission == plan.source_admission:
        position = "source"
    elif current_admission == plan.pending_admission:
        position = "pending"
    elif current_admission == plan.final_admission:
        position = "final"
    else:
        position = "other"
    if position == "other":
        return BusinessCutoverAssetCoverageResult(
            BusinessCutoverAssetCoverageReconcileStatus.CONFLICT,
            plan.cutover_id,
            error_code="business_cutover_asset_file_admission_conflict",
        )
    if indeterminate:
        return BusinessCutoverAssetCoverageResult(
            BusinessCutoverAssetCoverageReconcileStatus.UNKNOWN,
            plan.cutover_id,
            error_code="business_cutover_asset_coverage_observation_indeterminate",
        )
    if observed_evidence is None:
        if position == "source":
            return BusinessCutoverAssetCoverageResult(
                BusinessCutoverAssetCoverageReconcileStatus.NOT_COVERED,
                plan.cutover_id,
            )
        return BusinessCutoverAssetCoverageResult(
            BusinessCutoverAssetCoverageReconcileStatus.CORRUPT,
            plan.cutover_id,
            error_code="business_cutover_asset_pending_without_coverage",
        )
    if (
        observed_evidence.cutover_id != plan.cutover_id
        or observed_evidence.transaction_id != plan.transaction_id
        or observed_evidence.repository_epoch != plan.repository_epoch
        or observed_evidence.source_admission_revision
        != plan.source_admission_revision
    ):
        return BusinessCutoverAssetCoverageResult(
            BusinessCutoverAssetCoverageReconcileStatus.CONFLICT,
            plan.cutover_id,
            error_code="business_cutover_asset_coverage_identity_conflict",
        )
    observed_digest = _digest(
        BUSINESS_CUTOVER_ASSET_COVERAGE_MANIFEST_HASH_DOMAIN,
        observed_evidence.manifest_bytes,
    )
    if (
        not hmac.compare_digest(
            observed_digest,
            observed_evidence.manifest_sha256,
        )
        or observed_evidence != plan.expected_evidence
    ):
        return BusinessCutoverAssetCoverageResult(
            BusinessCutoverAssetCoverageReconcileStatus.CORRUPT,
            plan.cutover_id,
            error_code="business_cutover_asset_coverage_evidence_mismatch",
        )
    return BusinessCutoverAssetCoverageResult(
        BusinessCutoverAssetCoverageReconcileStatus.COVERED_EXACT,
        plan.cutover_id,
        evidence=observed_evidence,
    )


# Private migration leaf: exact imports only, no stable or wildcard surface.
__all__ = ()
