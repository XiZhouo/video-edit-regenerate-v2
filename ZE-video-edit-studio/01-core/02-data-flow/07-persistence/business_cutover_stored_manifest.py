"""Pure exact decoders for stored cutover install and coverage manifests.

The production authority reader and the V12 durable-provenance reader must
interpret these bytes identically.  This module owns that shared, I/O-free
boundary so neither SQLite reader can silently accept a weaker manifest shape.
It issues no authority capability and exports no public SDK surface.
"""

from __future__ import annotations

import hmac
from typing import Any, NamedTuple, Tuple

from business_codec import BusinessCodecError, decode_payload_bounded
from business_cutover_asset_contract import (
    BUSINESS_CUTOVER_ASSET_COVERAGE_MANIFEST_SCHEMA,
    BUSINESS_CUTOVER_ASSET_COVERAGE_PROTOCOL,
    BUSINESS_CUTOVER_ASSET_COVERAGE_STATUS,
    BUSINESS_CUTOVER_ASSET_COVERED_STATUS,
    MAX_BUSINESS_CUTOVER_ASSET_MANIFEST_BYTES,
    MAX_BUSINESS_CUTOVER_ASSET_MANIFEST_NODES,
    BusinessCutoverAssetContractError,
    BusinessCutoverAssetCoverageEvidence,
    BusinessCutoverAssetStageLimits,
)
from business_cutover_asset_integrity import (
    BUSINESS_CUTOVER_ASSET_COVERAGE_MANIFEST_HASH_DOMAIN,
    _digest as _coverage_manifest_digest,
)
from business_cutover_contract import (
    BUSINESS_CUTOVER_INSTALL_AUTHORITY_STATE,
    BUSINESS_CUTOVER_INSTALL_MANIFEST_SCHEMA,
    BUSINESS_CUTOVER_INSTALL_STATUS,
    BUSINESS_CUTOVER_MEMBER_MAPPING_SCHEMA,
    BUSINESS_CUTOVER_PROTOCOL,
    MAX_BUSINESS_CUTOVER_MANIFEST_BYTES,
    MAX_BUSINESS_CUTOVER_MANIFEST_NODES,
    BusinessCutoverAdmission,
    BusinessCutoverContractError,
    BusinessCutoverInstallEvidence,
    BusinessCutoverRuntimeBinding,
    digest_business_cutover_admission,
)
from business_cutover_integrity import (
    BUSINESS_CUTOVER_INSTALL_MANIFEST_HASH_DOMAIN,
    _domain_digest as _install_manifest_digest,
)


_LOWER_HEX = frozenset("0123456789abcdef")


class BusinessCutoverStoredManifestError(ValueError):
    """Stable payload-free rejection of noncanonical stored manifest bytes."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        if type(code) is not str or not code:
            raise TypeError("code must be a non-empty exact string")
        self.code = code
        super().__init__(code)


def _fail() -> None:
    raise BusinessCutoverStoredManifestError("cutover_stored_manifest_invalid")


def _exact_object(value: Any, keys: Tuple[str, ...]) -> dict:
    if type(value) is not dict or set(value) != set(keys):
        _fail()
    return value


def _manifest_digest(value: Any) -> bytes:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in _LOWER_HEX for character in value)
    ):
        _fail()
    return bytes.fromhex(value)


def _exact_integer(value: Any, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        _fail()
    return value


def _exact_text(value: Any) -> str:
    if type(value) is not str or not value:
        _fail()
    return value


def _decode_exact_manifest(
    manifest_bytes: Any,
    maximum_bytes: int,
    maximum_nodes: int,
    root_keys: Tuple[str, ...],
) -> dict:
    try:
        root = decode_payload_bounded(
            manifest_bytes,
            maximum_bytes,
            maximum_nodes,
        )
    except BusinessCodecError as exc:
        raise BusinessCutoverStoredManifestError(
            "cutover_stored_manifest_invalid"
        ) from exc
    return _exact_object(root, root_keys)


def _business_runtime_from_manifest(value: Any) -> BusinessCutoverRuntimeBinding:
    runtime = _exact_object(
        value,
        (
            "authority",
            "authority_state",
            "repository_epoch",
            "cutover_id",
            "source_admission_revision",
            "repository_revision",
            "state_revision",
            "runtime_generation",
            "code_digest",
            "schema_version",
        ),
    )
    try:
        return BusinessCutoverRuntimeBinding(
            authority=runtime["authority"],
            authority_state=runtime["authority_state"],
            repository_epoch=runtime["repository_epoch"],
            cutover_id=runtime["cutover_id"],
            source_admission_revision=runtime["source_admission_revision"],
            repository_revision=runtime["repository_revision"],
            state_revision=runtime["state_revision"],
            runtime_generation=runtime["runtime_generation"],
            code_digest=_manifest_digest(runtime["code_digest"]),
            schema_version=runtime["schema_version"],
        )
    except (BusinessCutoverContractError, TypeError, ValueError) as exc:
        raise BusinessCutoverStoredManifestError(
            "cutover_stored_manifest_invalid"
        ) from exc


class _StoredInstallProjection(NamedTuple):
    evidence: BusinessCutoverInstallEvidence
    source_admission_sha256: bytes
    pending_admission_sha256: bytes
    final_admission_sha256: bytes
    target_schema_version: int
    target_schema_fingerprint: str
    legacy_proof: dict
    member_mapping: dict


def _parse_stored_install_manifest_unchecked(
    manifest_bytes: bytes,
) -> _StoredInstallProjection:
    """Decode and re-hash one exact stored install manifest."""

    root = _decode_exact_manifest(
        manifest_bytes,
        MAX_BUSINESS_CUTOVER_MANIFEST_BYTES,
        MAX_BUSINESS_CUTOVER_MANIFEST_NODES,
        (
            "schema",
            "protocol",
            "status",
            "transaction",
            "cutover_id",
            "admissions",
            "legacy_proof",
            "runtime",
            "expected_target",
            "member_mapping",
        ),
    )
    transaction = _exact_object(root["transaction"], ("transaction_id", "receipt_kind"))
    admissions = _exact_object(root["admissions"], ("source", "pending", "final"))
    admission_values = {}
    admission_digests = {}
    for name in ("source", "pending", "final"):
        item = _exact_object(
            admissions[name],
            (
                "schema",
                "authority_state",
                "authority_epoch",
                "revision",
                "cutover_id",
                "sha256",
            ),
        )
        try:
            admission = BusinessCutoverAdmission(
                authority_state=item["authority_state"],
                authority_epoch=item["authority_epoch"],
                revision=item["revision"],
                cutover_id=item["cutover_id"],
            )
            digest = _manifest_digest(item["sha256"])
        except (BusinessCutoverContractError, TypeError, ValueError) as exc:
            raise BusinessCutoverStoredManifestError(
                "cutover_stored_manifest_invalid"
            ) from exc
        if item["schema"] != "ze.repository_admission.v1" or not hmac.compare_digest(
            digest_business_cutover_admission(admission), digest
        ):
            _fail()
        admission_values[name] = admission
        admission_digests[name] = digest
    legacy = _exact_object(
        root["legacy_proof"],
        (
            "cutover_id",
            "transaction_id",
            "catalog_id",
            "catalog_seal_sha256",
            "repository_epoch",
            "source_admission_revision",
            "manifest_sha256",
            "source_repository_sha256",
            "catalog_coverage_sha256",
            "proof_sha256",
            "schema_fingerprint",
            "project_count",
            "live_project_count",
            "deleted_project_count",
        ),
    )
    expected_target = _exact_object(
        root["expected_target"], ("schema_version", "schema_fingerprint")
    )
    member_mapping = _exact_object(
        root["member_mapping"],
        (
            "schema",
            "sha256",
            "project_count",
            "live_project_count",
            "deleted_project_count",
            "baseline_commit_count",
        ),
    )
    for field in (
        "catalog_seal_sha256",
        "manifest_sha256",
        "source_repository_sha256",
        "catalog_coverage_sha256",
        "proof_sha256",
    ):
        _manifest_digest(legacy[field])
    for field in ("cutover_id", "catalog_id", "repository_epoch"):
        _manifest_digest(legacy[field])
    _manifest_digest(expected_target["schema_fingerprint"])
    _manifest_digest(member_mapping["sha256"])
    _exact_integer(legacy["source_admission_revision"], minimum=1)
    _exact_integer(expected_target["schema_version"], minimum=1)
    _exact_text(legacy["transaction_id"])
    _exact_text(legacy["schema_fingerprint"])
    for field in (
        "project_count",
        "live_project_count",
        "deleted_project_count",
    ):
        _exact_integer(legacy[field])
        _exact_integer(member_mapping[field])
    _exact_integer(member_mapping["baseline_commit_count"])
    if (
        root["schema"] != BUSINESS_CUTOVER_INSTALL_MANIFEST_SCHEMA
        or root["protocol"] != BUSINESS_CUTOVER_PROTOCOL
        or root["status"] != BUSINESS_CUTOVER_INSTALL_STATUS
        or member_mapping["schema"] != BUSINESS_CUTOVER_MEMBER_MAPPING_SCHEMA
        or legacy["project_count"]
        != legacy["live_project_count"] + legacy["deleted_project_count"]
    ):
        _fail()
    try:
        runtime = _business_runtime_from_manifest(root["runtime"])
        source = admission_values["source"]
        pending = admission_values["pending"]
        final = admission_values["final"]
        if (
            source.authority_state != "legacy"
            or pending.authority_state != "cutover_pending"
            or final.authority_state != "sqlite"
            or source.authority_epoch != runtime.repository_epoch
            or pending.authority_epoch != runtime.repository_epoch
            or final.authority_epoch != runtime.repository_epoch
            or source.revision != runtime.source_admission_revision
            or pending.revision != source.revision + 1
            or final.revision != source.revision + 2
            or source.cutover_id != ""
            or pending.cutover_id != root["cutover_id"]
            or final.cutover_id != root["cutover_id"]
            or legacy["cutover_id"] != root["cutover_id"]
            or legacy["repository_epoch"] != runtime.repository_epoch
            or legacy["source_admission_revision"] != runtime.source_admission_revision
            or member_mapping["project_count"] != legacy["project_count"]
            or member_mapping["live_project_count"] != legacy["live_project_count"]
            or member_mapping["deleted_project_count"]
            != legacy["deleted_project_count"]
            or member_mapping["baseline_commit_count"] != legacy["live_project_count"]
        ):
            _fail()
        evidence = BusinessCutoverInstallEvidence(
            cutover_id=root["cutover_id"],
            transaction_id=transaction["transaction_id"],
            receipt_kind=transaction["receipt_kind"],
            authority_state=BUSINESS_CUTOVER_INSTALL_AUTHORITY_STATE,
            runtime=runtime,
            member_mapping_sha256=_manifest_digest(member_mapping["sha256"]),
            manifest_bytes=manifest_bytes,
            manifest_sha256=_install_manifest_digest(
                BUSINESS_CUTOVER_INSTALL_MANIFEST_HASH_DOMAIN,
                manifest_bytes,
            ),
        )
    except (BusinessCutoverContractError, TypeError, ValueError) as exc:
        raise BusinessCutoverStoredManifestError(
            "cutover_stored_manifest_invalid"
        ) from exc
    return _StoredInstallProjection(
        evidence=evidence,
        source_admission_sha256=admission_digests["source"],
        pending_admission_sha256=admission_digests["pending"],
        final_admission_sha256=admission_digests["final"],
        target_schema_version=expected_target["schema_version"],
        target_schema_fingerprint=expected_target["schema_fingerprint"],
        legacy_proof=legacy,
        member_mapping=member_mapping,
    )


def _parse_stored_install_manifest(
    manifest_bytes: bytes,
) -> _StoredInstallProjection:
    """Decode install bytes with one stable error boundary around all data use."""

    try:
        return _parse_stored_install_manifest_unchecked(manifest_bytes)
    except BusinessCutoverStoredManifestError:
        raise
    except (
        BusinessCodecError,
        BusinessCutoverContractError,
        KeyError,
        OverflowError,
        TypeError,
        ValueError,
    ) as exc:
        raise BusinessCutoverStoredManifestError(
            "cutover_stored_manifest_invalid"
        ) from exc


class _StoredCoverageProjection(NamedTuple):
    evidence: BusinessCutoverAssetCoverageEvidence
    member_root_sha256: bytes
    repository_member_count: int
    live_project_count: int
    deleted_project_count: int
    pending_admission_sha256: bytes
    runtime_generation: str
    code_digest: bytes
    root: dict


def _parse_stored_coverage_manifest_unchecked(
    manifest_bytes: bytes,
) -> _StoredCoverageProjection:
    """Decode and domain-hash one exact stored repository coverage manifest."""

    root = _decode_exact_manifest(
        manifest_bytes,
        MAX_BUSINESS_CUTOVER_ASSET_MANIFEST_BYTES,
        MAX_BUSINESS_CUTOVER_ASSET_MANIFEST_NODES,
        (
            "schema",
            "protocol",
            "status",
            "transaction",
            "authority",
            "limits",
            "repository",
            "batches",
            "observed",
            "coverage",
            "projects",
            "blobs",
            "occurrences",
        ),
    )
    transaction = _exact_object(root["transaction"], ("transaction_id", "receipt_kind"))
    authority = _exact_object(
        root["authority"],
        (
            "repository_epoch",
            "cutover_id",
            "source_admission_revision",
            "source_admission_sha256",
            "pending_admission_sha256",
            "runtime_generation",
            "code_digest",
        ),
    )
    limits = _exact_object(
        root["limits"],
        (
            "max_reference_count",
            "max_unique_blob_count",
            "max_blob_bytes",
            "max_project_unique_bytes",
            "max_batch_unique_bytes",
            "max_coverage_batch_count",
            "max_coverage_observed_occurrences",
            "max_coverage_observed_rows",
            "max_coverage_observed_manifest_bytes",
        ),
    )
    repository = _exact_object(
        root["repository"],
        (
            "project_count",
            "live_project_count",
            "deleted_project_count",
            "member_root_sha256",
            "members",
        ),
    )
    observed = _exact_object(
        root["observed"],
        (
            "batch_count",
            "occurrence_count",
            "project_row_count",
            "blob_row_count",
            "row_count",
            "manifest_bytes",
        ),
    )
    coverage = _exact_object(
        root["coverage"],
        (
            "batch_count",
            "project_count",
            "reference_count",
            "unique_blob_count",
            "unique_blob_bytes",
            "referenced_bytes",
            "project_root_sha256",
            "occurrence_root_sha256",
            "blob_root_sha256",
            "coverage_sha256",
        ),
    )
    try:
        BusinessCutoverAssetStageLimits(**limits)
    except (BusinessCutoverAssetContractError, TypeError, ValueError) as exc:
        raise BusinessCutoverStoredManifestError(
            "cutover_stored_manifest_invalid"
        ) from exc
    for field in (
        "project_count",
        "live_project_count",
        "deleted_project_count",
    ):
        _exact_integer(repository[field])
    for field in (
        "batch_count",
        "occurrence_count",
        "project_row_count",
        "blob_row_count",
        "row_count",
        "manifest_bytes",
    ):
        _exact_integer(observed[field])
    for field in (
        "batch_count",
        "project_count",
        "reference_count",
        "unique_blob_count",
        "unique_blob_bytes",
        "referenced_bytes",
    ):
        _exact_integer(coverage[field])
    _manifest_digest(repository["member_root_sha256"])
    _manifest_digest(authority["source_admission_sha256"])
    _manifest_digest(authority["pending_admission_sha256"])
    _manifest_digest(authority["code_digest"])
    _exact_text(authority["runtime_generation"])
    if (
        root["schema"] != BUSINESS_CUTOVER_ASSET_COVERAGE_MANIFEST_SCHEMA
        or root["protocol"] != BUSINESS_CUTOVER_ASSET_COVERAGE_PROTOCOL
        or root["status"] != BUSINESS_CUTOVER_ASSET_COVERAGE_STATUS
        or any(
            type(root[name]) is not list
            for name in ("batches", "projects", "blobs", "occurrences")
        )
        or type(repository["members"]) is not list
        or len(repository["members"]) != repository["project_count"]
        or observed["batch_count"] != coverage["batch_count"]
        or repository["project_count"]
        != repository["live_project_count"] + repository["deleted_project_count"]
        or len(root["batches"]) != coverage["batch_count"]
        or len(root["projects"]) != coverage["project_count"]
        or len(root["blobs"]) != coverage["unique_blob_count"]
        or len(root["occurrences"]) != coverage["reference_count"]
        or observed["row_count"]
        != observed["project_row_count"]
        + observed["blob_row_count"]
        + observed["occurrence_count"]
    ):
        _fail()
    try:
        evidence = BusinessCutoverAssetCoverageEvidence(
            cutover_id=authority["cutover_id"],
            transaction_id=transaction["transaction_id"],
            receipt_kind=transaction["receipt_kind"],
            status=BUSINESS_CUTOVER_ASSET_COVERED_STATUS,
            repository_epoch=authority["repository_epoch"],
            source_admission_revision=authority["source_admission_revision"],
            source_admission_sha256=_manifest_digest(
                authority["source_admission_sha256"]
            ),
            manifest_bytes=manifest_bytes,
            manifest_sha256=_coverage_manifest_digest(
                BUSINESS_CUTOVER_ASSET_COVERAGE_MANIFEST_HASH_DOMAIN,
                manifest_bytes,
            ),
            batch_count=coverage["batch_count"],
            observed_occurrence_count=observed["occurrence_count"],
            observed_row_count=observed["row_count"],
            observed_manifest_bytes=observed["manifest_bytes"],
            project_count=coverage["project_count"],
            reference_count=coverage["reference_count"],
            unique_blob_count=coverage["unique_blob_count"],
            unique_blob_bytes=coverage["unique_blob_bytes"],
            referenced_bytes=coverage["referenced_bytes"],
            project_root_sha256=_manifest_digest(coverage["project_root_sha256"]),
            blob_root_sha256=_manifest_digest(coverage["blob_root_sha256"]),
            occurrence_root_sha256=_manifest_digest(coverage["occurrence_root_sha256"]),
            coverage_sha256=_manifest_digest(coverage["coverage_sha256"]),
        )
    except (BusinessCutoverAssetContractError, TypeError, ValueError) as exc:
        raise BusinessCutoverStoredManifestError(
            "cutover_stored_manifest_invalid"
        ) from exc
    return _StoredCoverageProjection(
        evidence=evidence,
        member_root_sha256=_manifest_digest(repository["member_root_sha256"]),
        repository_member_count=repository["project_count"],
        live_project_count=repository["live_project_count"],
        deleted_project_count=repository["deleted_project_count"],
        pending_admission_sha256=_manifest_digest(
            authority["pending_admission_sha256"]
        ),
        runtime_generation=authority["runtime_generation"],
        code_digest=_manifest_digest(authority["code_digest"]),
        root=root,
    )


def _parse_stored_coverage_manifest(
    manifest_bytes: bytes,
) -> _StoredCoverageProjection:
    """Decode coverage bytes with one stable error boundary around all data use."""

    try:
        return _parse_stored_coverage_manifest_unchecked(manifest_bytes)
    except BusinessCutoverStoredManifestError:
        raise
    except (
        BusinessCodecError,
        BusinessCutoverAssetContractError,
        KeyError,
        OverflowError,
        TypeError,
        ValueError,
    ) as exc:
        raise BusinessCutoverStoredManifestError(
            "cutover_stored_manifest_invalid"
        ) from exc


__all__ = ()
