"""Pure V12 install-closure and baseline-atomic row projection.

V11 can describe a complete cutover in memory, but it cannot durably carry
the member mapping and it cannot lawfully reuse the post-mount production
outer receipt for pre-mount baseline commits.  This module closes both facts
without performing I/O:

* the exact member mapping and baseline projection are split into deterministic
  16 MiB chunks whose complete, domain-separated hashes remain unchanged;
* every cutover baseline commit receives a dedicated outer sidecar receipt;
* one bounded install-closure manifest binds the V5 source proof, the fresh
  repository asset-coverage seal, both artifacts, every baseline receipt, the
  install pointer, counts, and the canonical install time.

The result is a private, issuer-stamped SQL row projection.  A fixed writer
must persist all rows and the V11 baseline rows in one ``BEGIN IMMEDIATE``
transaction, read them back before commit, and reconcile through a fresh
connection after commit.  These values grant no database or file authority.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from typing import Any, Tuple

from business_codec import (
    BusinessCodecError,
    decode_payload_bounded,
    encode_payload_bounded,
)
from business_cutover_asset_contract import (
    BUSINESS_CUTOVER_ASSET_COVERAGE_TRANSACTION_KIND,
)
from business_cutover_asset_integrity import (
    BusinessCutoverAssetCoveragePlan,
    BusinessCutoverAssetIntegrityError,
    _repository_member_root,
    _require_business_cutover_asset_coverage_plan,
)
from business_cutover_baseline_projection import (
    BUSINESS_CUTOVER_BASELINE_OUTER_ATOMIC_STATUS as _V1_OUTER_ATOMIC_STATUS,
    BUSINESS_CUTOVER_BASELINE_PROJECTION_HASH_DOMAIN,
    BUSINESS_CUTOVER_BASELINE_PROJECTION_PROTOCOL,
    BUSINESS_CUTOVER_BASELINE_PROJECTION_SCHEMA,
    BusinessCutoverBaselineProjection,
    BusinessCutoverBaselineProjectionError,
    _require_business_cutover_baseline_projection,
)
from business_cutover_contract import (
    BUSINESS_CUTOVER_MEMBER_MAPPING_SCHEMA,
)
from business_cutover_integrity import (
    BusinessCutoverIntegrityError,
    BusinessCutoverPlan,
    _require_business_cutover_plan,
)
from legacy_repository_contract import LEGACY_REPOSITORY_TRANSACTION_KIND
from production_atomic_write_contract import (
    EMPTY_PRODUCTION_TARGETS_SHA256,
)
from sqlite_schema import (
    INSTALL_CLOSURE_MIGRATION,
    MAX_BUSINESS_CUTOVER_ARTIFACT_BYTES,
    MAX_BUSINESS_CUTOVER_ARTIFACT_CHUNK_BYTES,
    MAX_BUSINESS_CUTOVER_ARTIFACT_CHUNKS,
    MAX_BUSINESS_SMALL_PAYLOAD_BYTES,
)


BUSINESS_CUTOVER_INSTALL_CLOSURE_PROTOCOL = "ze.business-cutover-install-closure.v1"
BUSINESS_CUTOVER_INSTALL_CLOSURE_SCHEMA = (
    "ze.business-cutover-install-closure.manifest.v1"
)
BUSINESS_CUTOVER_INSTALL_CLOSURE_STATUS = "install_closed"
BUSINESS_CUTOVER_MEMBER_MAPPING_ARTIFACT = "member_mapping"
BUSINESS_CUTOVER_BASELINE_PROJECTION_ARTIFACT = "baseline_projection"
BUSINESS_CUTOVER_BASELINE_PROJECTION_V12_PROTOCOL = (
    "ze.business-cutover-baseline-projection.v2"
)
BUSINESS_CUTOVER_BASELINE_PROJECTION_V12_SCHEMA = (
    "ze.business-cutover-baseline-projection.manifest.v2"
)
BUSINESS_CUTOVER_BASELINE_OUTER_ATOMIC_STATUS = "baseline_atomic_sidecar_v1"
BUSINESS_CUTOVER_BASELINE_OUTER_PROTOCOL = (
    "ze.business-cutover-baseline-atomic-outer.v1"
)
BUSINESS_CUTOVER_BASELINE_OUTER_SCHEMA = (
    "ze.business-cutover-baseline-atomic-outer.manifest.v1"
)

BUSINESS_CUTOVER_ARTIFACT_CHUNK_HASH_DOMAIN = (
    b"ze.business-cutover-install-artifact-chunk.v1"
)
BUSINESS_CUTOVER_BASELINE_ATOMIC_ROOT_HASH_DOMAIN = (
    b"ze.business-cutover-baseline-atomic-root.v1"
)
BUSINESS_CUTOVER_INSTALL_CLOSURE_HASH_DOMAIN = (
    b"ze.business-cutover-install-closure.manifest-digest.v1"
)
BUSINESS_CUTOVER_BASELINE_PROJECTION_V12_HASH_DOMAIN = (
    b"ze.business-cutover-baseline-projection.manifest-digest.v2"
)
BUSINESS_CUTOVER_BASELINE_OUTER_MANIFEST_HASH_DOMAIN = (
    b"ze.business-cutover-baseline-atomic-outer.manifest-digest.v1"
)
BUSINESS_CUTOVER_BASELINE_OUTER_RECEIPT_HASH_DOMAIN = (
    b"ze.business-cutover-baseline-atomic-outer.receipt.v1"
)

INSTALL_ARTIFACT_COLUMNS = (
    "cutover_id",
    "artifact_kind",
    "artifact_schema",
    "byte_length",
    "chunk_count",
    "artifact_sha256",
)
INSTALL_ARTIFACT_CHUNK_COLUMNS = (
    "cutover_id",
    "artifact_kind",
    "ordinal",
    "byte_offset",
    "chunk_bytes",
    "chunk_sha256",
)
BASELINE_ATOMIC_RECEIPT_COLUMNS = (
    "cutover_id",
    "member_ordinal",
    "repository_revision",
    "baseline_outer_schema",
    "baseline_outer_protocol",
    "member_mapping_kind",
    "member_mapping_sha256",
    "baseline_projection_kind",
    "baseline_projection_sha256",
    "commit_id",
    "transaction_id",
    "receipt_kind",
    "committed_at",
    "install_transaction_id",
    "install_manifest_sha256",
    "atomic_manifest_sha256",
    "pipeline_intents_sha256",
    "controls_sha256",
    "control_count",
    "before_worker_sequence",
    "before_worker_tail_sha256",
    "after_worker_sequence",
    "after_worker_tail_sha256",
    "manifest_bytes",
    "manifest_sha256",
    "receipt_sha256",
    "baseline_outer_manifest_bytes",
    "baseline_outer_manifest_sha256",
    "target_count",
    "targets_sha256",
    "baseline_outer_receipt_sha256",
)
ATOMIC_BUSINESS_WRITE_RECEIPT_COLUMNS = (
    "commit_id",
    "transaction_id",
    "receipt_kind",
    "committed_at",
    "atomic_manifest_sha256",
    "pipeline_intents_sha256",
    "controls_sha256",
    "control_count",
    "before_worker_sequence",
    "before_worker_tail_sha256",
    "after_worker_sequence",
    "after_worker_tail_sha256",
    "manifest_bytes",
    "manifest_sha256",
    "receipt_sha256",
    "production_manifest_bytes",
    "production_manifest_sha256",
    "target_count",
    "targets_sha256",
    "production_receipt_sha256",
)
INSTALL_CLOSURE_COLUMNS = (
    "cutover_id",
    "install_transaction_id",
    "install_manifest_sha256",
    "source_transaction_id",
    "source_receipt_kind",
    "source_admission_revision",
    "source_proof_manifest_sha256",
    "source_proof_sha256",
    "source_admission_sha256",
    "coverage_transaction_id",
    "coverage_receipt_kind",
    "coverage_manifest_sha256",
    "coverage_sha256",
    "committed_at",
    "member_mapping_kind",
    "member_mapping_sha256",
    "baseline_projection_kind",
    "baseline_projection_sha256",
    "project_count",
    "live_project_count",
    "deleted_project_count",
    "baseline_commit_count",
    "baseline_outer_receipt_root_sha256",
    "manifest_bytes",
    "manifest_sha256",
)

_CLOSURE_ISSUER = object()


class BusinessCutoverInstallClosureError(ValueError):
    """Stable payload-free failure at the V12 pure boundary."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        if type(code) is not str or not code:
            raise TypeError("code must be a non-empty exact string")
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise BusinessCutoverInstallClosureError(code)


def _frame(hasher: Any, value: bytes) -> None:
    hasher.update(len(value).to_bytes(8, "big"))
    hasher.update(value)


def _digest(domain: bytes, *values: bytes) -> bytes:
    hasher = hashlib.sha256()
    _frame(hasher, domain)
    for value in values:
        _frame(hasher, value)
    return hasher.digest()


def _integer(value: int) -> bytes:
    if type(value) is not int or value < 0:
        _fail("business_cutover_install_closure_integer_invalid")
    return str(value).encode("ascii")


def _text(value: str) -> bytes:
    if type(value) is not str or not value:
        _fail("business_cutover_install_closure_text_invalid")
    return value.encode("utf-8")


def _sql_values(value: Any, columns: Tuple[str, ...]) -> tuple:
    return tuple(getattr(value, column) for column in columns)


@dataclass(frozen=True)
class BusinessCutoverInstallArtifactRow:
    cutover_id: str
    artifact_kind: str
    artifact_schema: str
    byte_length: int
    chunk_count: int
    artifact_sha256: bytes = field(repr=False)

    def sql_values(self) -> tuple:
        return _sql_values(self, INSTALL_ARTIFACT_COLUMNS)


@dataclass(frozen=True)
class BusinessCutoverInstallArtifactChunkRow:
    cutover_id: str
    artifact_kind: str
    ordinal: int
    byte_offset: int
    chunk_bytes: bytes = field(repr=False)
    chunk_sha256: bytes = field(repr=False)

    def sql_values(self) -> tuple:
        return _sql_values(self, INSTALL_ARTIFACT_CHUNK_COLUMNS)


@dataclass(frozen=True)
class BusinessCutoverInstallArtifactProjection:
    header: BusinessCutoverInstallArtifactRow
    chunks: Tuple[BusinessCutoverInstallArtifactChunkRow, ...]


@dataclass(frozen=True)
class BusinessCutoverAtomicWriteReceiptRow:
    commit_id: str
    transaction_id: str
    receipt_kind: str
    committed_at: str
    atomic_manifest_sha256: bytes = field(repr=False)
    pipeline_intents_sha256: bytes = field(repr=False)
    controls_sha256: bytes = field(repr=False)
    control_count: int
    before_worker_sequence: int
    before_worker_tail_sha256: bytes = field(repr=False)
    after_worker_sequence: int
    after_worker_tail_sha256: bytes = field(repr=False)
    manifest_bytes: bytes = field(repr=False)
    manifest_sha256: bytes = field(repr=False)
    receipt_sha256: bytes = field(repr=False)
    production_manifest_bytes: bytes = field(repr=False)
    production_manifest_sha256: bytes = field(repr=False)
    target_count: int
    targets_sha256: bytes = field(repr=False)
    production_receipt_sha256: bytes = field(repr=False)

    def sql_values(self) -> tuple:
        return _sql_values(self, ATOMIC_BUSINESS_WRITE_RECEIPT_COLUMNS)


@dataclass(frozen=True)
class BusinessCutoverBaselineAtomicReceiptRow:
    cutover_id: str
    member_ordinal: int
    repository_revision: int
    baseline_outer_schema: str
    baseline_outer_protocol: str
    member_mapping_kind: str
    member_mapping_sha256: bytes = field(repr=False)
    baseline_projection_kind: str
    baseline_projection_sha256: bytes = field(repr=False)
    commit_id: str
    transaction_id: str
    receipt_kind: str
    committed_at: str
    install_transaction_id: str
    install_manifest_sha256: bytes = field(repr=False)
    atomic_manifest_sha256: bytes = field(repr=False)
    pipeline_intents_sha256: bytes = field(repr=False)
    controls_sha256: bytes = field(repr=False)
    control_count: int
    before_worker_sequence: int
    before_worker_tail_sha256: bytes = field(repr=False)
    after_worker_sequence: int
    after_worker_tail_sha256: bytes = field(repr=False)
    manifest_bytes: bytes = field(repr=False)
    manifest_sha256: bytes = field(repr=False)
    receipt_sha256: bytes = field(repr=False)
    baseline_outer_manifest_bytes: bytes = field(repr=False)
    baseline_outer_manifest_sha256: bytes = field(repr=False)
    target_count: int
    targets_sha256: bytes = field(repr=False)
    baseline_outer_receipt_sha256: bytes = field(repr=False)

    def sql_values(self) -> tuple:
        return _sql_values(self, BASELINE_ATOMIC_RECEIPT_COLUMNS)


@dataclass(frozen=True)
class BusinessCutoverInstallClosureRow:
    cutover_id: str
    install_transaction_id: str
    install_manifest_sha256: bytes = field(repr=False)
    source_transaction_id: str
    source_receipt_kind: str
    source_admission_revision: int
    source_proof_manifest_sha256: bytes = field(repr=False)
    source_proof_sha256: bytes = field(repr=False)
    source_admission_sha256: bytes = field(repr=False)
    coverage_transaction_id: str
    coverage_receipt_kind: str
    coverage_manifest_sha256: bytes = field(repr=False)
    coverage_sha256: bytes = field(repr=False)
    committed_at: str
    member_mapping_kind: str
    member_mapping_sha256: bytes = field(repr=False)
    baseline_projection_kind: str
    baseline_projection_sha256: bytes = field(repr=False)
    project_count: int
    live_project_count: int
    deleted_project_count: int
    baseline_commit_count: int
    baseline_outer_receipt_root_sha256: bytes = field(repr=False)
    manifest_bytes: bytes = field(repr=False)
    manifest_sha256: bytes = field(repr=False)

    def sql_values(self) -> tuple:
        return _sql_values(self, INSTALL_CLOSURE_COLUMNS)


@dataclass(frozen=True)
class BusinessCutoverInstallClosureProjection:
    cutover_id: str
    member_mapping: BusinessCutoverInstallArtifactProjection
    baseline_projection: BusinessCutoverInstallArtifactProjection
    atomic_receipts: Tuple[BusinessCutoverAtomicWriteReceiptRow, ...]
    baseline_receipts: Tuple[BusinessCutoverBaselineAtomicReceiptRow, ...]
    closure: BusinessCutoverInstallClosureRow
    protocol: str = BUSINESS_CUTOVER_INSTALL_CLOSURE_PROTOCOL
    status: str = BUSINESS_CUTOVER_INSTALL_CLOSURE_STATUS
    _plan: BusinessCutoverPlan = field(
        default=None,
        repr=False,
        compare=False,
    )
    _baseline: BusinessCutoverBaselineProjection = field(
        default=None,
        repr=False,
        compare=False,
    )
    _coverage_plan: BusinessCutoverAssetCoveragePlan = field(
        default=None,
        repr=False,
        compare=False,
    )
    _issuer: Any = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._issuer is not _CLOSURE_ISSUER:
            _fail("business_cutover_install_closure_issuer_invalid")


def _artifact(
    *,
    cutover_id: str,
    artifact_kind: str,
    artifact_schema: str,
    artifact_bytes: bytes,
    artifact_sha256: bytes,
) -> BusinessCutoverInstallArtifactProjection:
    if (
        type(artifact_bytes) is not bytes
        or not 1 <= len(artifact_bytes) <= MAX_BUSINESS_CUTOVER_ARTIFACT_BYTES
        or type(artifact_sha256) is not bytes
        or len(artifact_sha256) != 32
    ):
        _fail("business_cutover_install_artifact_invalid")
    chunks = []
    for ordinal, offset in enumerate(
        range(0, len(artifact_bytes), MAX_BUSINESS_CUTOVER_ARTIFACT_CHUNK_BYTES)
    ):
        chunk = artifact_bytes[
            offset : offset + MAX_BUSINESS_CUTOVER_ARTIFACT_CHUNK_BYTES
        ]
        chunks.append(
            BusinessCutoverInstallArtifactChunkRow(
                cutover_id=cutover_id,
                artifact_kind=artifact_kind,
                ordinal=ordinal,
                byte_offset=offset,
                chunk_bytes=chunk,
                chunk_sha256=_digest(
                    BUSINESS_CUTOVER_ARTIFACT_CHUNK_HASH_DOMAIN,
                    _text(cutover_id),
                    _text(artifact_kind),
                    _integer(ordinal),
                    _integer(offset),
                    chunk,
                ),
            )
        )
    if not chunks or len(chunks) > MAX_BUSINESS_CUTOVER_ARTIFACT_CHUNKS:
        _fail("business_cutover_install_artifact_chunk_count_invalid")
    header = BusinessCutoverInstallArtifactRow(
        cutover_id=cutover_id,
        artifact_kind=artifact_kind,
        artifact_schema=artifact_schema,
        byte_length=len(artifact_bytes),
        chunk_count=len(chunks),
        artifact_sha256=artifact_sha256,
    )
    return BusinessCutoverInstallArtifactProjection(header, tuple(chunks))


def _validate_asset_coverage_binding(
    *,
    plan: BusinessCutoverPlan,
    baseline: BusinessCutoverBaselineProjection,
    coverage_plan: BusinessCutoverAssetCoveragePlan,
) -> None:
    """Prove coverage describes every baseline occurrence and blob exactly.

    The coverage issuer already closes its own snapshot inputs.  V12 must also
    close the *relationship* between that seal and the SQL rows being installed;
    object identity alone would not make a durable reader's three carriers one
    proof.  This comparison therefore deliberately walks all projects,
    occurrences, and unique blob descriptors.
    """

    if (
        getattr(coverage_plan, "_cutover_plan", None) is not plan
        or coverage_plan.cutover_id != plan.cutover_id
        or coverage_plan.source_admission_revision
        != plan.legacy_proof.source_admission_revision
        or coverage_plan.source_admission_sha256 != plan.closure.source_admission_sha256
        or coverage_plan.repository_epoch != plan.legacy_proof.repository_epoch
        or coverage_plan.transaction_id
        != coverage_plan.expected_evidence.transaction_id
        or coverage_plan.manifest_sha256
        != coverage_plan.expected_evidence.manifest_sha256
    ):
        _fail("business_cutover_install_closure_coverage_identity_mismatch")

    evidence = coverage_plan.expected_evidence
    coverage = coverage_plan.coverage
    if (
        evidence.cutover_id != plan.cutover_id
        or evidence.receipt_kind != BUSINESS_CUTOVER_ASSET_COVERAGE_TRANSACTION_KIND
        or evidence.project_count != coverage.project_count
        or evidence.reference_count != coverage.reference_count
        or evidence.unique_blob_count != coverage.unique_blob_count
        or evidence.unique_blob_bytes != coverage.unique_blob_bytes
        or evidence.referenced_bytes != coverage.referenced_bytes
        or evidence.project_root_sha256 != coverage.project_root_sha256
        or evidence.occurrence_root_sha256 != coverage.occurrence_root_sha256
        or evidence.blob_root_sha256 != coverage.blob_root_sha256
        or evidence.coverage_sha256 != coverage.coverage_sha256
    ):
        _fail("business_cutover_install_closure_coverage_identity_mismatch")

    blobs = coverage_plan.blobs
    if (
        tuple(value.blob_ordinal for value in blobs) != tuple(range(len(blobs)))
        or tuple(value.asset_sha256 for value in blobs)
        != tuple(sorted(value.asset_sha256 for value in blobs))
        or len({value.asset_sha256 for value in blobs}) != len(blobs)
    ):
        _fail("business_cutover_install_closure_coverage_blob_mismatch")
    blob_by_sha = {value.asset_sha256: value for value in blobs}

    if (
        len(coverage_plan.projects) != len(baseline.live)
        or coverage.project_count != len(baseline.live)
        or coverage.reference_count != baseline.asset_reference_row_count
        or coverage.unique_blob_count != len(blobs)
        or coverage.unique_blob_count != len(baseline.asset_blobs)
    ):
        _fail("business_cutover_install_closure_coverage_count_mismatch")

    expected_occurrences = []
    occurrence_start = 0
    for project, live in zip(coverage_plan.projects, baseline.live):
        references = live.asset_references
        project_shas = {value.asset_sha256.hex() for value in references}
        if (
            project.project_ordinal != live.ordinal
            or project.project_id != live.commit.project_id
            or project.generation != live.commit.generation
            or project.document_id != live.commit.document_id
            or project.business_root_sha256 != live.commit.after_root_sha256
            or project.projection_sha256 != live.commit.asset_projection_sha256
            or project.occurrence_start != occurrence_start
            or project.occurrence_count != len(references)
            or project.unique_blob_count != len(project_shas)
            or project.unique_blob_bytes
            != sum(blob_by_sha[value].byte_length for value in project_shas)
        ):
            _fail("business_cutover_install_closure_coverage_project_mismatch")
        if tuple(value.ordinal for value in references) != tuple(
            range(len(references))
        ):
            _fail("business_cutover_install_closure_coverage_occurrence_mismatch")
        expected_occurrences.extend((live, value) for value in references)
        occurrence_start += len(references)

    occurrences = coverage_plan.occurrences
    if len(occurrences) != len(expected_occurrences):
        _fail("business_cutover_install_closure_coverage_occurrence_mismatch")
    occurrence_counts = {value.asset_sha256: 0 for value in blobs}
    occurrence_projects = {value.asset_sha256: set() for value in blobs}
    referenced_bytes = 0
    for ordinal, (occurrence, expected) in enumerate(
        zip(occurrences, expected_occurrences)
    ):
        live, reference = expected
        asset_sha256 = reference.asset_sha256.hex()
        descriptor = blob_by_sha.get(asset_sha256)
        if descriptor is None:
            _fail("business_cutover_install_closure_coverage_blob_mismatch")
        if (
            occurrence.ordinal != ordinal
            or occurrence.project_ordinal != live.ordinal
            or occurrence.project_id != reference.project_id
            or occurrence.generation != reference.generation
            or occurrence.business_root_sha256 != live.commit.after_root_sha256
            or occurrence.projection_sha256 != live.commit.asset_projection_sha256
            or occurrence.entity_type != reference.entity_type
            or occurrence.entity_id != reference.entity_id
            or occurrence.entity_deleted is not bool(reference.entity_deleted)
            or occurrence.value_path != reference.value_path
            or occurrence.blob_ordinal != descriptor.blob_ordinal
            or occurrence.asset_sha256 != asset_sha256
            or occurrence.media_type != descriptor.media_type
            or occurrence.byte_length != descriptor.byte_length
            or occurrence.extension != descriptor.extension
            or (
                reference.ref_mime is not None
                and reference.ref_mime != descriptor.media_type
            )
            or (
                reference.ref_size is not None
                and reference.ref_size != descriptor.byte_length
            )
            or (
                reference.ref_ext is not None
                and reference.ref_ext != descriptor.extension
            )
        ):
            _fail("business_cutover_install_closure_coverage_occurrence_mismatch")
        occurrence_counts[asset_sha256] += 1
        occurrence_projects[asset_sha256].add(occurrence.project_id)
        referenced_bytes += descriptor.byte_length

    baseline_blob_by_sha = {
        value.asset_sha256.hex(): value for value in baseline.asset_blobs
    }
    if set(baseline_blob_by_sha) != set(blob_by_sha):
        _fail("business_cutover_install_closure_coverage_blob_mismatch")
    for asset_sha256, descriptor in blob_by_sha.items():
        source = baseline_blob_by_sha[asset_sha256]
        if (
            descriptor.content_key != source.content_key
            or descriptor.byte_length != source.byte_length
            or (
                source.media_type is not None
                and source.media_type != descriptor.media_type
            )
            or (
                source.extension is not None
                and source.extension != descriptor.extension
            )
            or descriptor.occurrence_count != occurrence_counts[asset_sha256]
            or descriptor.project_count != len(occurrence_projects[asset_sha256])
        ):
            _fail("business_cutover_install_closure_coverage_blob_mismatch")
    if (
        coverage.unique_blob_bytes != sum(value.byte_length for value in blobs)
        or coverage.referenced_bytes != referenced_bytes
    ):
        _fail("business_cutover_install_closure_coverage_count_mismatch")


def _source_proof_manifest(
    plan: BusinessCutoverPlan,
    *,
    member_root_sha256: bytes,
) -> dict:
    proof = plan.legacy_proof
    return {
        "transaction_id": proof.transaction_id,
        "receipt_kind": LEGACY_REPOSITORY_TRANSACTION_KIND,
        "catalog_id": proof.catalog_id,
        "catalog_seal_sha256": proof.catalog_seal_sha256.hex(),
        "repository_epoch": proof.repository_epoch,
        "source_admission_revision": proof.source_admission_revision,
        "manifest_sha256": proof.manifest_sha256.hex(),
        "proof_sha256": proof.proof_sha256.hex(),
        "source_admission_sha256": (plan.closure.source_admission_sha256.hex()),
        "source_repository_sha256": proof.source_repository_sha256.hex(),
        "catalog_coverage_sha256": proof.catalog_coverage_sha256.hex(),
        "schema_fingerprint": proof.schema_fingerprint,
        "member_root_sha256": member_root_sha256.hex(),
        "project_count": proof.project_count,
        "live_project_count": proof.live_project_count,
        "deleted_project_count": proof.deleted_project_count,
    }


def _asset_coverage_manifest(
    coverage_plan: BusinessCutoverAssetCoveragePlan,
    *,
    member_root_sha256: bytes,
) -> dict:
    evidence = coverage_plan.expected_evidence
    coverage = coverage_plan.coverage
    return {
        "transaction_id": coverage_plan.transaction_id,
        "receipt_kind": BUSINESS_CUTOVER_ASSET_COVERAGE_TRANSACTION_KIND,
        "repository_epoch": coverage_plan.repository_epoch,
        "source_admission_revision": coverage_plan.source_admission_revision,
        "source_admission_sha256": (coverage_plan.source_admission_sha256.hex()),
        "manifest_sha256": coverage_plan.manifest_sha256.hex(),
        "coverage_sha256": coverage.coverage_sha256.hex(),
        "member_root_sha256": member_root_sha256.hex(),
        "batch_count": evidence.batch_count,
        "project_count": coverage.project_count,
        "reference_count": coverage.reference_count,
        "unique_blob_count": coverage.unique_blob_count,
        "unique_blob_bytes": coverage.unique_blob_bytes,
        "referenced_bytes": coverage.referenced_bytes,
        "project_root_sha256": coverage.project_root_sha256.hex(),
        "occurrence_root_sha256": coverage.occurrence_root_sha256.hex(),
        "blob_root_sha256": coverage.blob_root_sha256.hex(),
    }


def _reconstruct_baseline_projection_v1_from_v2(value: Any) -> bytes:
    """Rebuild the exact source V1 bytes, including pre-enhancement metadata."""

    if type(value) is not dict:
        _fail("business_cutover_baseline_projection_v2_source_proof_invalid")
    source = value.get("source_projection_v1")
    asset_write = value.get("asset_blob_write")
    blobs = value.get("asset_blobs")
    if (
        set(source) != {"schema", "sha256", "asset_blob_metadata"}
        if type(source) is dict
        else True
    ):
        _fail("business_cutover_baseline_projection_v2_source_proof_invalid")
    if (
        source["schema"] != BUSINESS_CUTOVER_BASELINE_PROJECTION_SCHEMA
        or type(source["sha256"]) is not str
        or len(source["sha256"]) != 64
        or any(value not in "0123456789abcdef" for value in source["sha256"])
        or type(source["asset_blob_metadata"]) is not list
        or type(asset_write) is not dict
        or type(blobs) is not list
        or len(source["asset_blob_metadata"]) != len(blobs)
    ):
        _fail("business_cutover_baseline_projection_v2_source_proof_invalid")
    restored_blobs = []
    for blob, metadata in zip(blobs, source["asset_blob_metadata"]):
        if (
            type(blob) is not dict
            or type(metadata) is not dict
            or set(metadata)
            != {"asset_sha256", "media_type", "extension", "byte_length"}
            or metadata["asset_sha256"] != blob.get("asset_sha256")
        ):
            _fail("business_cutover_baseline_projection_v2_source_proof_invalid")
        restored = dict(blob)
        restored["media_type"] = metadata["media_type"]
        restored["extension"] = metadata["extension"]
        restored["byte_length"] = metadata["byte_length"]
        restored_blobs.append(restored)
    restored = dict(value)
    restored["schema"] = BUSINESS_CUTOVER_BASELINE_PROJECTION_SCHEMA
    restored["protocol"] = BUSINESS_CUTOVER_BASELINE_PROJECTION_PROTOCOL
    restored["outer_atomic_status"] = _V1_OUTER_ATOMIC_STATUS
    restored["asset_blobs"] = restored_blobs
    del restored["source_projection_v1"]
    del restored["asset_blob_write"]
    try:
        encoded = encode_payload_bounded(
            restored,
            MAX_BUSINESS_CUTOVER_ARTIFACT_BYTES,
            1_000_000,
        )
    except BusinessCodecError as exc:
        raise BusinessCutoverInstallClosureError(
            "business_cutover_baseline_projection_v2_source_proof_invalid"
        ) from exc
    source_sha256 = _digest(
        BUSINESS_CUTOVER_BASELINE_PROJECTION_HASH_DOMAIN,
        encoded,
    )
    try:
        expected_sha256 = bytes.fromhex(source["sha256"])
    except ValueError as exc:
        raise BusinessCutoverInstallClosureError(
            "business_cutover_baseline_projection_v2_source_proof_invalid"
        ) from exc
    if len(expected_sha256) != 32 or not hmac.compare_digest(
        source_sha256,
        expected_sha256,
    ):
        _fail("business_cutover_baseline_projection_v2_source_proof_invalid")
    return encoded


def _baseline_projection_v12(
    baseline: BusinessCutoverBaselineProjection,
    coverage_plan: BusinessCutoverAssetCoveragePlan,
) -> tuple[bytes, bytes]:
    try:
        value = decode_payload_bounded(
            baseline.manifest_bytes,
            MAX_BUSINESS_CUTOVER_ARTIFACT_BYTES,
            1_000_000,
        )
    except BusinessCodecError as exc:
        raise BusinessCutoverInstallClosureError(
            "business_cutover_baseline_projection_v2_source_invalid"
        ) from exc
    if (
        type(value) is not dict
        or value.get("schema") != BUSINESS_CUTOVER_BASELINE_PROJECTION_SCHEMA
        or value.get("cutover_id") != baseline.cutover_id
        or value.get("outer_atomic_status") != "requires_schema_upgrade"
    ):
        _fail("business_cutover_baseline_projection_v2_source_invalid")
    value = dict(value)
    source_blobs = value.get("asset_blobs")
    if type(source_blobs) is not list or any(
        type(blob) is not dict for blob in source_blobs
    ):
        _fail("business_cutover_baseline_projection_v2_source_invalid")
    descriptor_by_sha = {blob.asset_sha256: blob for blob in coverage_plan.blobs}
    if set(descriptor_by_sha) != {blob.get("asset_sha256") for blob in source_blobs}:
        _fail("business_cutover_baseline_projection_v2_coverage_invalid")
    source_metadata = []
    enhanced_blobs = []
    for source_blob in source_blobs:
        asset_sha256 = source_blob.get("asset_sha256")
        descriptor = descriptor_by_sha.get(asset_sha256)
        if descriptor is None:
            _fail("business_cutover_baseline_projection_v2_coverage_invalid")
        source_metadata.append(
            {
                "asset_sha256": asset_sha256,
                "media_type": source_blob.get("media_type"),
                "extension": source_blob.get("extension"),
                "byte_length": source_blob.get("byte_length"),
            }
        )
        enhanced = dict(source_blob)
        enhanced["media_type"] = descriptor.media_type
        enhanced["extension"] = descriptor.extension
        enhanced["byte_length"] = descriptor.byte_length
        enhanced_blobs.append(enhanced)

    value["schema"] = BUSINESS_CUTOVER_BASELINE_PROJECTION_V12_SCHEMA
    value["protocol"] = BUSINESS_CUTOVER_BASELINE_PROJECTION_V12_PROTOCOL
    value["outer_atomic_status"] = BUSINESS_CUTOVER_BASELINE_OUTER_ATOMIC_STATUS
    value["asset_blobs"] = enhanced_blobs
    value["source_projection_v1"] = {
        "schema": BUSINESS_CUTOVER_BASELINE_PROJECTION_SCHEMA,
        "sha256": baseline.manifest_sha256.hex(),
        # V2 may fill metadata omitted by an old reference.  A reader must use
        # this exact source metadata when reconstructing and hashing V1; simply
        # deleting V2-only keys would hash the enhanced semantics instead.
        "asset_blob_metadata": source_metadata,
    }
    if any(
        blob.get("status") != "available"
        or blob.get("state_revision") != 1
        or blob.get("created_at") != baseline.committed_at
        or blob.get("updated_at") != baseline.committed_at
        for blob in source_blobs
    ):
        _fail("business_cutover_baseline_projection_v2_source_invalid")
    # Asset staging is an external-provider receipt, not an ``asset_blobs``
    # database state.  The install transaction therefore inserts the exact
    # available@revision-1 rows already projected by v1 after revalidating
    # fresh coverage; inventing a staged row followed by revision 2 would
    # contradict the real V8 table history.
    value["asset_blob_write"] = {
        "mode": "insert_available_v1_from_fresh_coverage",
        "created_at": baseline.committed_at,
        "count": len(source_blobs),
        "asset_coverage": {
            "transaction_id": coverage_plan.transaction_id,
            "receipt_kind": BUSINESS_CUTOVER_ASSET_COVERAGE_TRANSACTION_KIND,
            "manifest_sha256": coverage_plan.manifest_sha256.hex(),
            "coverage_sha256": coverage_plan.coverage.coverage_sha256.hex(),
            "project_root_sha256": (coverage_plan.coverage.project_root_sha256.hex()),
            "occurrence_root_sha256": (
                coverage_plan.coverage.occurrence_root_sha256.hex()
            ),
            "blob_root_sha256": coverage_plan.coverage.blob_root_sha256.hex(),
        },
    }
    if _reconstruct_baseline_projection_v1_from_v2(value) != baseline.manifest_bytes:
        _fail("business_cutover_baseline_projection_v2_source_proof_invalid")
    try:
        encoded = encode_payload_bounded(
            value,
            MAX_BUSINESS_CUTOVER_ARTIFACT_BYTES,
            1_000_000,
        )
    except BusinessCodecError as exc:
        raise BusinessCutoverInstallClosureError(
            "business_cutover_baseline_projection_v2_invalid"
        ) from exc
    return encoded, _digest(
        BUSINESS_CUTOVER_BASELINE_PROJECTION_V12_HASH_DOMAIN,
        encoded,
    )


def _empty_targets_sha256() -> bytes:
    # Empty production writes still carry the three canonical target groups.
    # Hashing only the aggregate count would create a different protocol root
    # that the existing production reader and durable auditor correctly reject.
    return EMPTY_PRODUCTION_TARGETS_SHA256


def _baseline_outer_manifest(
    *,
    plan: BusinessCutoverPlan,
    baseline: BusinessCutoverBaselineProjection,
    live: Any,
    member_mapping_sha256: bytes,
    baseline_projection_sha256: bytes,
    targets_sha256: bytes,
) -> bytes:
    seed = live.atomic_seed
    try:
        return encode_payload_bounded(
            {
                "schema": BUSINESS_CUTOVER_BASELINE_OUTER_SCHEMA,
                "protocol": BUSINESS_CUTOVER_BASELINE_OUTER_PROTOCOL,
                "status": BUSINESS_CUTOVER_BASELINE_OUTER_ATOMIC_STATUS,
                "cutover_id": plan.cutover_id,
                "member": {
                    "ordinal": live.ordinal,
                    "repository_revision": live.commit.repository_revision,
                    "project_id": live.commit.project_id,
                    "generation": live.commit.generation,
                },
                "transaction": {
                    "commit_id": seed.commit_id,
                    "transaction_id": seed.transaction_id,
                    "receipt_kind": seed.receipt_kind,
                    "committed_at": seed.committed_at,
                },
                "install": {
                    "transaction_id": plan.install.transaction_id,
                    "manifest_sha256": plan.install.manifest_sha256.hex(),
                },
                "artifacts": {
                    "member_mapping_sha256": member_mapping_sha256.hex(),
                    "baseline_projection_sha256": (baseline_projection_sha256.hex()),
                    "source_projection_v1_sha256": (baseline.manifest_sha256.hex()),
                },
                "pure_atomic": {
                    "atomic_manifest_sha256": (seed.atomic_manifest_sha256.hex()),
                    "manifest_sha256": seed.manifest_sha256.hex(),
                    "receipt_sha256": seed.receipt_sha256.hex(),
                },
                "targets": {
                    "count": 0,
                    "sha256": targets_sha256.hex(),
                },
            },
            MAX_BUSINESS_SMALL_PAYLOAD_BYTES,
            100_000,
        )
    except BusinessCodecError as exc:
        raise BusinessCutoverInstallClosureError(
            "business_cutover_baseline_outer_manifest_invalid"
        ) from exc


def _baseline_outer_receipt_sha256(
    *,
    seed: Any,
    outer_manifest_sha256: bytes,
    targets_sha256: bytes,
) -> bytes:
    return _digest(
        BUSINESS_CUTOVER_BASELINE_OUTER_RECEIPT_HASH_DOMAIN,
        _text(seed.commit_id),
        _text(seed.transaction_id),
        _text(seed.receipt_kind),
        _text(seed.committed_at),
        seed.receipt_sha256,
        outer_manifest_sha256,
        (0).to_bytes(8, "big"),
        targets_sha256,
    )


def _baseline_receipt_root(
    rows: Tuple[BusinessCutoverBaselineAtomicReceiptRow, ...],
) -> bytes:
    values = []
    for row in rows:
        values.extend(
            (
                _integer(row.member_ordinal),
                _integer(row.repository_revision),
                row.baseline_outer_receipt_sha256,
            )
        )
    return _digest(
        BUSINESS_CUTOVER_BASELINE_ATOMIC_ROOT_HASH_DOMAIN,
        *values,
    )


def _closure_manifest_digest(value: bytes) -> bytes:
    return _digest(BUSINESS_CUTOVER_INSTALL_CLOSURE_HASH_DOMAIN, value)


def _prepare(
    plan: Any,
    baseline: Any,
    coverage_plan: Any,
) -> BusinessCutoverInstallClosureProjection:
    try:
        plan = _require_business_cutover_plan(plan)
        baseline = _require_business_cutover_baseline_projection(baseline)
        coverage_plan = _require_business_cutover_asset_coverage_plan(coverage_plan)
    except (
        BusinessCutoverIntegrityError,
        BusinessCutoverBaselineProjectionError,
        BusinessCutoverAssetIntegrityError,
        AttributeError,
        TypeError,
    ) as exc:
        raise BusinessCutoverInstallClosureError(
            "business_cutover_install_closure_input_invalid"
        ) from exc
    if (
        getattr(baseline, "_plan", None) is not plan
        or baseline.cutover_id != plan.cutover_id
        or getattr(coverage_plan, "_cutover_plan", None) is not plan
        or coverage_plan.cutover_id != plan.cutover_id
    ):
        _fail("business_cutover_install_closure_plan_mismatch")
    if plan.expected_target_schema_version < INSTALL_CLOSURE_MIGRATION.version:
        _fail("business_cutover_install_closure_schema_target_invalid")

    _validate_asset_coverage_binding(
        plan=plan,
        baseline=baseline,
        coverage_plan=coverage_plan,
    )
    member_root_sha256 = _repository_member_root(plan)

    member_mapping = _artifact(
        cutover_id=plan.cutover_id,
        artifact_kind=BUSINESS_CUTOVER_MEMBER_MAPPING_ARTIFACT,
        artifact_schema=BUSINESS_CUTOVER_MEMBER_MAPPING_SCHEMA,
        artifact_bytes=plan.closure.member_mapping_bytes,
        artifact_sha256=plan.closure.member_mapping_sha256,
    )
    baseline_v12_bytes, baseline_v12_sha256 = _baseline_projection_v12(
        baseline,
        coverage_plan,
    )
    baseline_artifact = _artifact(
        cutover_id=plan.cutover_id,
        artifact_kind=BUSINESS_CUTOVER_BASELINE_PROJECTION_ARTIFACT,
        artifact_schema=BUSINESS_CUTOVER_BASELINE_PROJECTION_V12_SCHEMA,
        artifact_bytes=baseline_v12_bytes,
        artifact_sha256=baseline_v12_sha256,
    )

    atomic_receipts = []
    receipts = []
    targets_sha256 = _empty_targets_sha256()
    for live in baseline.live:
        seed = live.atomic_seed
        outer_manifest_bytes = _baseline_outer_manifest(
            plan=plan,
            baseline=baseline,
            live=live,
            member_mapping_sha256=plan.closure.member_mapping_sha256,
            baseline_projection_sha256=baseline_v12_sha256,
            targets_sha256=targets_sha256,
        )
        outer_manifest_sha256 = _digest(
            BUSINESS_CUTOVER_BASELINE_OUTER_MANIFEST_HASH_DOMAIN,
            outer_manifest_bytes,
        )
        outer_receipt_sha256 = _baseline_outer_receipt_sha256(
            seed=seed,
            outer_manifest_sha256=outer_manifest_sha256,
            targets_sha256=targets_sha256,
        )
        atomic_receipts.append(
            BusinessCutoverAtomicWriteReceiptRow(
                commit_id=seed.commit_id,
                transaction_id=seed.transaction_id,
                receipt_kind=seed.receipt_kind,
                committed_at=seed.committed_at,
                atomic_manifest_sha256=seed.atomic_manifest_sha256,
                pipeline_intents_sha256=seed.pipeline_intents_sha256,
                controls_sha256=seed.controls_sha256,
                control_count=seed.control_count,
                before_worker_sequence=seed.before_worker_sequence,
                before_worker_tail_sha256=seed.before_worker_tail_sha256,
                after_worker_sequence=seed.after_worker_sequence,
                after_worker_tail_sha256=seed.after_worker_tail_sha256,
                manifest_bytes=seed.manifest_bytes,
                manifest_sha256=seed.manifest_sha256,
                receipt_sha256=seed.receipt_sha256,
                production_manifest_bytes=outer_manifest_bytes,
                production_manifest_sha256=outer_manifest_sha256,
                target_count=0,
                targets_sha256=targets_sha256,
                production_receipt_sha256=outer_receipt_sha256,
            )
        )
        receipts.append(
            BusinessCutoverBaselineAtomicReceiptRow(
                cutover_id=plan.cutover_id,
                member_ordinal=live.ordinal,
                repository_revision=live.commit.repository_revision,
                baseline_outer_schema=BUSINESS_CUTOVER_BASELINE_OUTER_SCHEMA,
                baseline_outer_protocol=BUSINESS_CUTOVER_BASELINE_OUTER_PROTOCOL,
                member_mapping_kind=BUSINESS_CUTOVER_MEMBER_MAPPING_ARTIFACT,
                member_mapping_sha256=plan.closure.member_mapping_sha256,
                baseline_projection_kind=(
                    BUSINESS_CUTOVER_BASELINE_PROJECTION_ARTIFACT
                ),
                baseline_projection_sha256=baseline_v12_sha256,
                commit_id=seed.commit_id,
                transaction_id=seed.transaction_id,
                receipt_kind=seed.receipt_kind,
                committed_at=seed.committed_at,
                install_transaction_id=plan.install.transaction_id,
                install_manifest_sha256=plan.install.manifest_sha256,
                atomic_manifest_sha256=seed.atomic_manifest_sha256,
                pipeline_intents_sha256=seed.pipeline_intents_sha256,
                controls_sha256=seed.controls_sha256,
                control_count=seed.control_count,
                before_worker_sequence=seed.before_worker_sequence,
                before_worker_tail_sha256=seed.before_worker_tail_sha256,
                after_worker_sequence=seed.after_worker_sequence,
                after_worker_tail_sha256=seed.after_worker_tail_sha256,
                manifest_bytes=seed.manifest_bytes,
                manifest_sha256=seed.manifest_sha256,
                receipt_sha256=seed.receipt_sha256,
                baseline_outer_manifest_bytes=outer_manifest_bytes,
                baseline_outer_manifest_sha256=outer_manifest_sha256,
                target_count=0,
                targets_sha256=targets_sha256,
                baseline_outer_receipt_sha256=outer_receipt_sha256,
            )
        )
    atomic_rows = tuple(atomic_receipts)
    receipt_rows = tuple(receipts)
    if tuple(value.member_ordinal for value in receipt_rows) != tuple(
        value.ordinal for value in baseline.live
    ) or tuple(value.repository_revision for value in receipt_rows) != tuple(
        range(1, len(receipt_rows) + 1)
    ):
        _fail("business_cutover_install_closure_receipt_order_invalid")
    if tuple(value.production_receipt_sha256 for value in atomic_rows) != tuple(
        value.baseline_outer_receipt_sha256 for value in receipt_rows
    ):
        _fail("business_cutover_install_closure_atomic_mirror_invalid")
    receipt_root = _baseline_receipt_root(receipt_rows)

    try:
        manifest_bytes = encode_payload_bounded(
            {
                "schema": BUSINESS_CUTOVER_INSTALL_CLOSURE_SCHEMA,
                "protocol": BUSINESS_CUTOVER_INSTALL_CLOSURE_PROTOCOL,
                "status": BUSINESS_CUTOVER_INSTALL_CLOSURE_STATUS,
                "cutover_id": plan.cutover_id,
                "install": {
                    "transaction_id": plan.install.transaction_id,
                    "manifest_sha256": plan.install.manifest_sha256.hex(),
                    "committed_at": baseline.committed_at,
                },
                "schema_target": {
                    "version": plan.expected_target_schema_version,
                    "fingerprint": plan.expected_target_schema_fingerprint,
                },
                "source_proof": _source_proof_manifest(
                    plan,
                    member_root_sha256=member_root_sha256,
                ),
                "asset_coverage": _asset_coverage_manifest(
                    coverage_plan,
                    member_root_sha256=member_root_sha256,
                ),
                "artifacts": [
                    {
                        "kind": value.header.artifact_kind,
                        "schema": value.header.artifact_schema,
                        "byte_length": value.header.byte_length,
                        "chunk_count": value.header.chunk_count,
                        "sha256": value.header.artifact_sha256.hex(),
                    }
                    for value in (member_mapping, baseline_artifact)
                ],
                "projects": {
                    "count": len(baseline.members),
                    "live_count": len(baseline.live),
                    "deleted_count": len(baseline.deleted),
                },
                "baseline_receipts": {
                    "count": len(receipt_rows),
                    "root_sha256": receipt_root.hex(),
                    "members": [
                        {
                            "member_ordinal": value.member_ordinal,
                            "repository_revision": value.repository_revision,
                            "commit_id": value.commit_id,
                            "receipt_sha256": (
                                value.baseline_outer_receipt_sha256.hex()
                            ),
                        }
                        for value in receipt_rows
                    ],
                },
            },
            MAX_BUSINESS_SMALL_PAYLOAD_BYTES,
            100_000,
        )
    except BusinessCodecError as exc:
        raise BusinessCutoverInstallClosureError(
            "business_cutover_install_closure_manifest_invalid"
        ) from exc
    closure_row = BusinessCutoverInstallClosureRow(
        cutover_id=plan.cutover_id,
        install_transaction_id=plan.install.transaction_id,
        install_manifest_sha256=plan.install.manifest_sha256,
        source_transaction_id=plan.legacy_proof.transaction_id,
        source_receipt_kind=LEGACY_REPOSITORY_TRANSACTION_KIND,
        source_admission_revision=(plan.legacy_proof.source_admission_revision),
        source_proof_manifest_sha256=plan.legacy_proof.manifest_sha256,
        source_proof_sha256=plan.legacy_proof.proof_sha256,
        source_admission_sha256=plan.closure.source_admission_sha256,
        coverage_transaction_id=coverage_plan.transaction_id,
        coverage_receipt_kind=(BUSINESS_CUTOVER_ASSET_COVERAGE_TRANSACTION_KIND),
        coverage_manifest_sha256=coverage_plan.manifest_sha256,
        coverage_sha256=coverage_plan.coverage.coverage_sha256,
        committed_at=baseline.committed_at,
        member_mapping_kind=BUSINESS_CUTOVER_MEMBER_MAPPING_ARTIFACT,
        member_mapping_sha256=plan.closure.member_mapping_sha256,
        baseline_projection_kind=(BUSINESS_CUTOVER_BASELINE_PROJECTION_ARTIFACT),
        baseline_projection_sha256=baseline_v12_sha256,
        project_count=len(baseline.members),
        live_project_count=len(baseline.live),
        deleted_project_count=len(baseline.deleted),
        baseline_commit_count=len(receipt_rows),
        baseline_outer_receipt_root_sha256=receipt_root,
        manifest_bytes=manifest_bytes,
        manifest_sha256=_closure_manifest_digest(manifest_bytes),
    )
    return BusinessCutoverInstallClosureProjection(
        cutover_id=plan.cutover_id,
        member_mapping=member_mapping,
        baseline_projection=baseline_artifact,
        atomic_receipts=atomic_rows,
        baseline_receipts=receipt_rows,
        closure=closure_row,
        _plan=plan,
        _baseline=baseline,
        _coverage_plan=coverage_plan,
        _issuer=_CLOSURE_ISSUER,
    )


def _project_business_cutover_install_closure(
    plan: BusinessCutoverPlan,
    baseline: BusinessCutoverBaselineProjection,
    *,
    coverage_plan: BusinessCutoverAssetCoveragePlan,
) -> BusinessCutoverInstallClosureProjection:
    """Return the only canonical V12 install-closure SQL projection."""

    return _prepare(plan, baseline, coverage_plan)


def _require_business_cutover_install_closure(
    value: Any,
) -> BusinessCutoverInstallClosureProjection:
    if (
        type(value) is not BusinessCutoverInstallClosureProjection
        or getattr(value, "_issuer", None) is not _CLOSURE_ISSUER
        or value.protocol != BUSINESS_CUTOVER_INSTALL_CLOSURE_PROTOCOL
        or value.status != BUSINESS_CUTOVER_INSTALL_CLOSURE_STATUS
    ):
        _fail("business_cutover_install_closure_invalid")
    rebuilt = _prepare(
        value._plan,
        value._baseline,
        value._coverage_plan,
    )
    if rebuilt != value or not hmac.compare_digest(
        rebuilt.closure.manifest_sha256,
        value.closure.manifest_sha256,
    ):
        _fail("business_cutover_install_closure_changed")
    return value


__all__ = ()
