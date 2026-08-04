"""Fresh V12 reader for durable cutover source and asset provenance.

The install closure is only useful when its provenance pointers name facts
that are independently reproducible.  This private, read-only module rebuilds
the V5 legacy repository proof, decodes the exact install admissions, rebuilds
every V8 asset stage receipt and member row, and recomputes the full repository
coverage root from the already-verified V12 baseline projection.

It deliberately issues a private capability rather than returning loose
dictionaries that a caller could assemble from structural joins alone.
"""

from __future__ import annotations

import hmac
from types import SimpleNamespace
from typing import Any, Mapping, NamedTuple, Tuple

from business_codec import BusinessCodecError, decode_payload_bounded
from business_cutover_asset_contract import (
    BUSINESS_CUTOVER_ASSET_COVERAGE_MANIFEST_SCHEMA,
    BUSINESS_CUTOVER_ASSET_COVERAGE_PROTOCOL,
    BUSINESS_CUTOVER_ASSET_COVERAGE_STATUS,
    BUSINESS_CUTOVER_ASSET_COVERAGE_TRANSACTION_KIND,
    BUSINESS_CUTOVER_ASSET_STAGE_MANIFEST_SCHEMA,
    BUSINESS_CUTOVER_ASSET_STAGE_PROTOCOL,
    BUSINESS_CUTOVER_ASSET_STAGE_STATUS,
    BUSINESS_CUTOVER_ASSET_STAGE_TRANSACTION_KIND,
    BUSINESS_CUTOVER_ASSET_STAGED_STATUS,
    MAX_BUSINESS_CUTOVER_ASSET_MANIFEST_BYTES,
    MAX_BUSINESS_CUTOVER_ASSET_MANIFEST_NODES,
    BusinessCutoverAssetBlobCoverage,
    BusinessCutoverAssetContractError,
    BusinessCutoverAssetOccurrenceCoverage,
    BusinessCutoverAssetProjectCoverage,
    BusinessCutoverAssetStageEvidence,
    BusinessCutoverAssetStageLimits,
)
from business_cutover_asset_integrity import (
    BUSINESS_CUTOVER_ASSET_COVERAGE_MANIFEST_HASH_DOMAIN,
    BUSINESS_CUTOVER_ASSET_OCCURRENCE_HASH_DOMAIN,
    BUSINESS_CUTOVER_ASSET_REPOSITORY_COVERAGE_HASH_DOMAIN,
    BusinessCutoverAssetIntegrityError,
    _blob_manifest,
    _blob_root,
    _digest,
    _digest_with_sequence,
    _integer_bytes,
    _issue_business_cutover_asset_stage_members,
    _manifest,
    _occurrence_hash,
    _occurrence_manifest,
    _occurrence_root,
    _project_manifest,
    _project_root,
    _repository_member_manifest,
    _repository_member_root,
    _validate_stage_evidence_against_cutover,
)
from business_cutover_contract import (
    BUSINESS_CUTOVER_AUTHORITY,
    BUSINESS_CUTOVER_INSTALL_AUTHORITY_STATE,
    BUSINESS_CUTOVER_INSTALL_STATE_REVISION,
    BUSINESS_CUTOVER_INSTALL_TRANSACTION_KIND,
    BUSINESS_CUTOVER_MEMBER_MAPPING_SCHEMA,
)
from business_cutover_stored_manifest import (
    BusinessCutoverStoredManifestError,
    _parse_stored_coverage_manifest,
    _parse_stored_install_manifest,
)
from legacy_repository_contract import (
    LEGACY_REPOSITORY_AUTHORITY,
    LEGACY_REPOSITORY_AUTHORITY_STATE,
    LEGACY_REPOSITORY_CUTOVER_READY,
    LEGACY_REPOSITORY_MANIFEST_SCHEMA,
    LEGACY_REPOSITORY_PROOF_PROTOCOL,
    LEGACY_REPOSITORY_RECEIPT_PROTOCOL,
    LEGACY_REPOSITORY_SELECTION,
    LEGACY_REPOSITORY_STATUS,
    LEGACY_REPOSITORY_TARGET_SCHEMA_VERSION,
    LEGACY_REPOSITORY_TRANSACTION_KIND,
)
from legacy_repository_integrity import (
    LegacyRepositoryIntegrity,
    LegacyRepositoryIntegrityError,
    LegacyRepositoryIntegrityMember,
    build_legacy_repository_integrity,
)
from sqlite_business_runtime import _canonical_utc

_PROVENANCE_ISSUER = object()
_LOWER_HEX = frozenset("0123456789abcdef")

_SOURCE_CONTROL_COLUMNS = (
    "singleton",
    "authority",
    "authority_state",
    "repository_epoch",
    "repository_revision",
    "catalog_revision",
    "cutover_id",
    "runtime_generation",
    "code_digest",
    "cutover_ready",
)
_SOURCE_SEAL_COLUMNS = (
    "repository_singleton",
    "cutover_id",
    "transaction_id",
    "receipt_kind",
    "receipt_protocol",
    "catalog_id",
    "catalog_seal_sha256",
    "proof_protocol",
    "manifest_schema",
    "status",
    "selection",
    "repository_epoch",
    "repository_revision",
    "catalog_revision",
    "runtime_generation",
    "code_digest",
    "project_count",
    "live_project_count",
    "deleted_project_count",
    "source_state_bytes",
    "manifest_bytes",
    "manifest_sha256",
    "source_repository_sha256",
    "catalog_coverage_sha256",
    "proof_sha256",
    "target_schema_version",
    "schema_fingerprint",
    "cutover_ready",
)
_SOURCE_MEMBER_COLUMNS = (
    "cutover_id",
    "ordinal",
    "project_id",
    "generation",
    "status",
    "operation_id",
    "source_state_sha256",
    "source_state_bytes",
    "catalog_id",
    "catalog_ordinal",
    "import_id",
    "document_id",
    "document_version",
    "storage_version",
    "business_root_sha256",
    "entity_count",
    "row_snapshot_bytes",
)
_INSTALL_COLUMNS = (
    "cutover_id",
    "transaction_id",
    "receipt_kind",
    "committed_at",
    "repository_epoch",
    "runtime_generation",
    "code_digest",
    "member_mapping_sha256",
    "manifest_bytes",
    "manifest_sha256",
)
_STAGE_COLUMNS = (
    "batch_id",
    "cutover_id",
    "transaction_id",
    "receipt_kind",
    "committed_at",
    "repository_epoch",
    "source_admission_revision",
    "source_admission_sha256",
    "member_count",
    "project_count",
    "blob_count",
    "occurrence_count",
    "unique_blob_bytes",
    "referenced_bytes",
    "project_root_sha256",
    "blob_root_sha256",
    "occurrence_root_sha256",
    "coverage_sha256",
    "manifest_bytes",
    "manifest_sha256",
)
_STAGE_MEMBER_COLUMNS = (
    "batch_id",
    "cutover_id",
    "member_kind",
    "ordinal",
    "member_id",
    "member_bytes",
    "member_sha256",
    "metadata_sha256",
)
_COVERAGE_COLUMNS = (
    "cutover_id",
    "transaction_id",
    "receipt_kind",
    "committed_at",
    "repository_epoch",
    "source_admission_revision",
    "source_admission_sha256",
    "batch_count",
    "member_root_sha256",
    "repository_member_count",
    "live_project_count",
    "deleted_project_count",
    "observed_occurrence_count",
    "observed_row_count",
    "observed_manifest_bytes",
    "project_count",
    "reference_count",
    "unique_blob_count",
    "unique_blob_bytes",
    "referenced_bytes",
    "project_root_sha256",
    "blob_root_sha256",
    "occurrence_root_sha256",
    "coverage_sha256",
    "manifest_bytes",
    "manifest_sha256",
)
_TRANSACTION_COLUMNS = ("transaction_id", "kind", "committed_at")


class SQLiteCutoverInstallProvenanceError(ValueError):
    """Stable payload-free rejection of a broken durable provenance chain."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        if type(code) is not str or not code:
            raise TypeError("code must be a non-empty exact string")
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise SQLiteCutoverInstallProvenanceError(code)


def _same(left: Any, right: Any) -> bool:
    if type(left) is bytes and type(right) is bytes:
        return hmac.compare_digest(left, right)
    return type(left) is type(right) and left == right


def _digest_value(value: Any) -> bytes:
    if type(value) is not bytes or len(value) != 32:
        _fail("cutover_install_provenance_digest_invalid")
    return value


def _hex_digest(value: Any) -> bytes:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in _LOWER_HEX for character in value)
    ):
        _fail("cutover_install_provenance_manifest_invalid")
    return bytes.fromhex(value)


def _row_tuple(row: Any, columns: Tuple[str, ...]) -> tuple[Any, ...]:
    if row is None:
        _fail("cutover_install_provenance_row_missing")
    try:
        values = tuple(row[column] for column in columns)
    except (IndexError, KeyError, TypeError) as exc:
        raise SQLiteCutoverInstallProvenanceError(
            "cutover_install_provenance_row_invalid"
        ) from exc
    if len(values) != len(columns):
        _fail("cutover_install_provenance_row_invalid")
    return values


def _select_one(
    connection: Any,
    table: str,
    columns: Tuple[str, ...],
    where: str,
    parameters: tuple[Any, ...],
    code: str,
) -> dict[str, Any]:
    rows = connection.execute(
        "SELECT " + ",".join(columns) + " FROM " + table + " WHERE " + where,
        parameters,
    ).fetchall()
    if len(rows) != 1:
        _fail(code)
    return dict(zip(columns, _row_tuple(rows[0], columns)))


def _dict_matches(observed: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
    return set(observed) == set(expected) and all(
        _same(observed[key], value) for key, value in expected.items()
    )


def _exact_object(value: Any, keys: Tuple[str, ...]) -> dict:
    if type(value) is not dict or set(value) != set(keys):
        _fail("cutover_install_provenance_manifest_invalid")
    return value


class _SourceProof(NamedTuple):
    integrity: LegacyRepositoryIntegrity
    members: Tuple[LegacyRepositoryIntegrityMember, ...]
    catalog_seal_sha256: bytes
    schema_fingerprint: str
    committed_at: str


def _read_source_proof(
    connection: Any,
    *,
    cutover_id: str,
    mapping: Mapping[int, Mapping[str, Any]],
) -> _SourceProof:
    control = _select_one(
        connection,
        "legacy_repository_proof_control",
        _SOURCE_CONTROL_COLUMNS,
        "singleton=1",
        (),
        "cutover_install_provenance_source_invalid",
    )
    seal = _select_one(
        connection,
        "legacy_repository_seals",
        _SOURCE_SEAL_COLUMNS,
        "cutover_id=?",
        (cutover_id,),
        "cutover_install_provenance_source_invalid",
    )
    raw_members = connection.execute(
        "SELECT "
        + ",".join(_SOURCE_MEMBER_COLUMNS)
        + " FROM legacy_repository_members WHERE cutover_id=? ORDER BY ordinal",
        (cutover_id,),
    ).fetchall()
    if len(raw_members) != len(mapping):
        _fail("cutover_install_provenance_source_invalid")
    members = []
    for ordinal, raw in enumerate(raw_members):
        row = dict(
            zip(
                _SOURCE_MEMBER_COLUMNS,
                _row_tuple(raw, _SOURCE_MEMBER_COLUMNS),
            )
        )
        mapped = mapping.get(ordinal)
        if (
            mapped is None
            or row["cutover_id"] != cutover_id
            or row["ordinal"] != ordinal
            or row["project_id"] != mapped["project_id"]
            or row["generation"] != mapped["generation"]
            or row["status"] != mapped["status"]
            or row["operation_id"] != mapped["operation_id"]
            or row["source_state_sha256"].hex() != mapped["source_state_sha256"]
            or row["source_state_bytes"] != mapped["source_state_bytes"]
            or row["catalog_ordinal"] != mapped["catalog_ordinal"]
            or row["import_id"] != mapped["import_id"]
            or row["document_id"] != mapped["document_id"]
            or row["document_version"] != mapped["document_version"]
            or row["storage_version"] != mapped["storage_version"]
            or (
                None
                if row["business_root_sha256"] is None
                else row["business_root_sha256"].hex()
            )
            != mapped["business_root_sha256"]
            or row["entity_count"] != mapped["entity_count"]
            or row["row_snapshot_bytes"] != mapped["row_snapshot_bytes"]
            or (mapped["status"] == "live" and row["catalog_id"] != seal["catalog_id"])
            or (mapped["status"] == "deleted" and row["catalog_id"] is not None)
        ):
            _fail("cutover_install_provenance_source_mapping_invalid")
        try:
            members.append(
                LegacyRepositoryIntegrityMember(
                    project_id=row["project_id"],
                    generation=row["generation"],
                    status=row["status"],
                    operation_id=row["operation_id"],
                    source_state_sha256=row["source_state_sha256"],
                    source_state_bytes=row["source_state_bytes"],
                    catalog_ordinal=row["catalog_ordinal"],
                    import_id=row["import_id"],
                    document_id=row["document_id"],
                    document_version=row["document_version"],
                    storage_version=row["storage_version"],
                    business_root_sha256=row["business_root_sha256"],
                    entity_count=row["entity_count"],
                    row_snapshot_bytes=row["row_snapshot_bytes"],
                )
            )
        except (LegacyRepositoryIntegrityError, TypeError, ValueError) as exc:
            raise SQLiteCutoverInstallProvenanceError(
                "cutover_install_provenance_source_invalid"
            ) from exc
    try:
        rebuilt = build_legacy_repository_integrity(
            catalog_id=seal["catalog_id"],
            catalog_seal_sha256=seal["catalog_seal_sha256"],
            repository_epoch=seal["repository_epoch"],
            repository_revision=seal["repository_revision"],
            catalog_revision=seal["catalog_revision"],
            runtime_generation=seal["runtime_generation"],
            code_digest=seal["code_digest"],
            members=tuple(members),
            schema_fingerprint=seal["schema_fingerprint"],
        )
    except LegacyRepositoryIntegrityError as exc:
        raise SQLiteCutoverInstallProvenanceError(
            "cutover_install_provenance_source_invalid"
        ) from exc
    expected_seal = {
        "repository_singleton": 1,
        "cutover_id": rebuilt.cutover_id,
        "transaction_id": rebuilt.transaction_id,
        "receipt_kind": LEGACY_REPOSITORY_TRANSACTION_KIND,
        "receipt_protocol": LEGACY_REPOSITORY_RECEIPT_PROTOCOL,
        "catalog_id": rebuilt.catalog_id,
        "catalog_seal_sha256": seal["catalog_seal_sha256"],
        "proof_protocol": LEGACY_REPOSITORY_PROOF_PROTOCOL,
        "manifest_schema": LEGACY_REPOSITORY_MANIFEST_SCHEMA,
        "status": LEGACY_REPOSITORY_STATUS,
        "selection": LEGACY_REPOSITORY_SELECTION,
        "repository_epoch": rebuilt.repository_epoch,
        "repository_revision": rebuilt.repository_revision,
        "catalog_revision": rebuilt.catalog_revision,
        "runtime_generation": rebuilt.runtime_generation,
        "code_digest": rebuilt.code_digest,
        "project_count": rebuilt.project_count,
        "live_project_count": rebuilt.live_project_count,
        "deleted_project_count": rebuilt.deleted_project_count,
        "source_state_bytes": rebuilt.source_state_bytes,
        "manifest_bytes": rebuilt.manifest_bytes,
        "manifest_sha256": rebuilt.manifest_sha256,
        "source_repository_sha256": rebuilt.source_repository_sha256,
        "catalog_coverage_sha256": rebuilt.catalog_coverage_sha256,
        "proof_sha256": rebuilt.proof_sha256,
        "target_schema_version": LEGACY_REPOSITORY_TARGET_SCHEMA_VERSION,
        "schema_fingerprint": seal["schema_fingerprint"],
        "cutover_ready": LEGACY_REPOSITORY_CUTOVER_READY,
    }
    if (
        rebuilt.cutover_id != cutover_id
        or not _dict_matches(seal, expected_seal)
        or not _dict_matches(
            control,
            {
                "singleton": 1,
                "authority": LEGACY_REPOSITORY_AUTHORITY,
                "authority_state": LEGACY_REPOSITORY_AUTHORITY_STATE,
                "repository_epoch": rebuilt.repository_epoch,
                "repository_revision": rebuilt.repository_revision,
                "catalog_revision": rebuilt.catalog_revision,
                "cutover_id": rebuilt.cutover_id,
                "runtime_generation": rebuilt.runtime_generation,
                "code_digest": rebuilt.code_digest,
                "cutover_ready": LEGACY_REPOSITORY_CUTOVER_READY,
            },
        )
    ):
        _fail("cutover_install_provenance_source_invalid")
    receipt = _select_one(
        connection,
        "transaction_receipts",
        _TRANSACTION_COLUMNS,
        "transaction_id=?",
        (rebuilt.transaction_id,),
        "cutover_install_provenance_source_receipt_invalid",
    )
    if receipt["kind"] != LEGACY_REPOSITORY_TRANSACTION_KIND or not _canonical_utc(
        receipt["committed_at"]
    ):
        _fail("cutover_install_provenance_source_receipt_invalid")
    return _SourceProof(
        rebuilt,
        tuple(members),
        seal["catalog_seal_sha256"],
        seal["schema_fingerprint"],
        receipt["committed_at"],
    )


def _member_cutover_view(
    cutover_id: str,
    mapping: Mapping[int, Mapping[str, Any]],
) -> Any:
    return SimpleNamespace(
        cutover_id=cutover_id,
        members=tuple(
            SimpleNamespace(
                ordinal=ordinal,
                project_id=member["project_id"],
                generation=member["generation"],
                status=member["status"],
                operation_id=member["operation_id"],
                source_state_sha256=bytes.fromhex(member["source_state_sha256"]),
                source_state_bytes=member["source_state_bytes"],
            )
            for ordinal, member in mapping.items()
        ),
    )


def _install_projection_exact(
    connection: Any,
    *,
    cutover_id: str,
    mapping: Mapping[int, Mapping[str, Any]],
    member_mapping_sha256: bytes,
    source: _SourceProof,
    install_transaction_id: str,
    install_manifest_sha256: bytes,
    install_committed_at: str,
    target_schema_version: int,
    target_schema_fingerprint: str,
) -> Any:
    install = _select_one(
        connection,
        "business_cutover_install_receipts",
        _INSTALL_COLUMNS,
        "cutover_id=?",
        (cutover_id,),
        "cutover_install_provenance_install_invalid",
    )
    try:
        projection = _parse_stored_install_manifest(install["manifest_bytes"])
    except BusinessCutoverStoredManifestError as exc:
        raise SQLiteCutoverInstallProvenanceError(
            "cutover_install_provenance_install_invalid"
        ) from exc
    proof = source.integrity
    runtime = projection.evidence.runtime
    expected_legacy = {
        "cutover_id": proof.cutover_id,
        "transaction_id": proof.transaction_id,
        "catalog_id": proof.catalog_id,
        "catalog_seal_sha256": source.catalog_seal_sha256.hex(),
        "repository_epoch": proof.repository_epoch,
        "source_admission_revision": proof.repository_revision,
        "manifest_sha256": proof.manifest_sha256.hex(),
        "source_repository_sha256": proof.source_repository_sha256.hex(),
        "catalog_coverage_sha256": proof.catalog_coverage_sha256.hex(),
        "proof_sha256": proof.proof_sha256.hex(),
        "schema_fingerprint": source.schema_fingerprint,
        "project_count": proof.project_count,
        "live_project_count": proof.live_project_count,
        "deleted_project_count": proof.deleted_project_count,
    }
    mapping_counts = {
        "schema": BUSINESS_CUTOVER_MEMBER_MAPPING_SCHEMA,
        "sha256": member_mapping_sha256.hex(),
        "project_count": proof.project_count,
        "live_project_count": proof.live_project_count,
        "deleted_project_count": proof.deleted_project_count,
        "baseline_commit_count": proof.live_project_count,
    }
    receipt = _select_one(
        connection,
        "transaction_receipts",
        _TRANSACTION_COLUMNS,
        "transaction_id=?",
        (install_transaction_id,),
        "cutover_install_provenance_install_receipt_invalid",
    )
    if (
        not _canonical_utc(install_committed_at)
        or not _canonical_utc(install["committed_at"])
        or not _canonical_utc(receipt["committed_at"])
        or not _canonical_utc(source.committed_at)
        or install["cutover_id"] != cutover_id
        or install["transaction_id"] != install_transaction_id
        or install["receipt_kind"] != BUSINESS_CUTOVER_INSTALL_TRANSACTION_KIND
        or install["committed_at"] != install_committed_at
        or install["repository_epoch"] != proof.repository_epoch
        or install["runtime_generation"] != proof.runtime_generation
        or not _same(install["code_digest"], proof.code_digest)
        or not _same(install["member_mapping_sha256"], member_mapping_sha256)
        or not _same(install["manifest_sha256"], install_manifest_sha256)
        or projection.evidence.cutover_id != cutover_id
        or projection.evidence.transaction_id != install_transaction_id
        or projection.evidence.receipt_kind != BUSINESS_CUTOVER_INSTALL_TRANSACTION_KIND
        or not _same(projection.evidence.manifest_sha256, install_manifest_sha256)
        or not _same(projection.evidence.member_mapping_sha256, member_mapping_sha256)
        or projection.target_schema_version != target_schema_version
        or projection.target_schema_fingerprint != target_schema_fingerprint
        or projection.legacy_proof != expected_legacy
        or projection.member_mapping != mapping_counts
        or runtime.authority != BUSINESS_CUTOVER_AUTHORITY
        or runtime.authority_state != BUSINESS_CUTOVER_INSTALL_AUTHORITY_STATE
        or runtime.repository_epoch != proof.repository_epoch
        or runtime.cutover_id != cutover_id
        or runtime.source_admission_revision != proof.repository_revision
        or runtime.repository_revision != max(1, proof.live_project_count)
        or runtime.state_revision != BUSINESS_CUTOVER_INSTALL_STATE_REVISION
        or runtime.runtime_generation != proof.runtime_generation
        or not _same(runtime.code_digest, proof.code_digest)
        or receipt
        != {
            "transaction_id": install_transaction_id,
            "kind": BUSINESS_CUTOVER_INSTALL_TRANSACTION_KIND,
            "committed_at": install_committed_at,
        }
        or source.committed_at > install_committed_at
        or set(mapping) != set(range(proof.project_count))
    ):
        _fail("cutover_install_provenance_install_invalid")
    return projection


def _project_value(value: Any) -> BusinessCutoverAssetProjectCoverage:
    item = _exact_object(
        value,
        (
            "project_ordinal",
            "project_id",
            "generation",
            "document_id",
            "business_root_sha256",
            "projection_sha256",
            "occurrence_start",
            "occurrence_count",
            "unique_blob_count",
            "unique_blob_bytes",
            "occurrence_sha256",
        ),
    )
    return BusinessCutoverAssetProjectCoverage(
        project_ordinal=item["project_ordinal"],
        project_id=item["project_id"],
        generation=item["generation"],
        document_id=item["document_id"],
        business_root_sha256=_hex_digest(item["business_root_sha256"]),
        projection_sha256=_hex_digest(item["projection_sha256"]),
        occurrence_start=item["occurrence_start"],
        occurrence_count=item["occurrence_count"],
        unique_blob_count=item["unique_blob_count"],
        unique_blob_bytes=item["unique_blob_bytes"],
        occurrence_sha256=_hex_digest(item["occurrence_sha256"]),
    )


def _blob_value(value: Any) -> BusinessCutoverAssetBlobCoverage:
    item = _exact_object(
        value,
        (
            "blob_ordinal",
            "asset_sha256",
            "content_key",
            "media_type",
            "byte_length",
            "extension",
            "occurrence_count",
            "project_count",
        ),
    )
    return BusinessCutoverAssetBlobCoverage(**item)


def _occurrence_value(value: Any) -> BusinessCutoverAssetOccurrenceCoverage:
    item = _exact_object(
        value,
        (
            "ordinal",
            "project_ordinal",
            "project_id",
            "generation",
            "business_root_sha256",
            "projection_sha256",
            "entity_type",
            "entity_id",
            "entity_deleted",
            "value_path",
            "blob_ordinal",
            "asset_sha256",
            "media_type",
            "byte_length",
            "extension",
        ),
    )
    return BusinessCutoverAssetOccurrenceCoverage(
        ordinal=item["ordinal"],
        project_ordinal=item["project_ordinal"],
        project_id=item["project_id"],
        generation=item["generation"],
        business_root_sha256=_hex_digest(item["business_root_sha256"]),
        projection_sha256=_hex_digest(item["projection_sha256"]),
        entity_type=item["entity_type"],
        entity_id=item["entity_id"],
        entity_deleted=item["entity_deleted"],
        value_path=item["value_path"],
        blob_ordinal=item["blob_ordinal"],
        asset_sha256=item["asset_sha256"],
        media_type=item["media_type"],
        byte_length=item["byte_length"],
        extension=item["extension"],
    )


def _stage_evidence(
    connection: Any,
    row: Mapping[str, Any],
    *,
    cutover: Any,
) -> BusinessCutoverAssetStageEvidence:
    try:
        root = decode_payload_bounded(
            row["manifest_bytes"],
            MAX_BUSINESS_CUTOVER_ASSET_MANIFEST_BYTES,
            MAX_BUSINESS_CUTOVER_ASSET_MANIFEST_NODES,
        )
    except BusinessCodecError as exc:
        raise SQLiteCutoverInstallProvenanceError(
            "cutover_install_provenance_stage_invalid"
        ) from exc
    root = _exact_object(
        root,
        (
            "schema",
            "protocol",
            "status",
            "transaction",
            "authority",
            "limits",
            "coverage",
            "projects",
            "blobs",
            "occurrences",
        ),
    )
    transaction = _exact_object(
        root["transaction"],
        ("batch_id", "transaction_id", "receipt_kind"),
    )
    authority = _exact_object(
        root["authority"],
        (
            "repository_epoch",
            "cutover_id",
            "source_admission_revision",
            "source_admission_sha256",
            "pending_admission_sha256",
            "final_admission_sha256",
            "runtime_generation",
            "code_digest",
        ),
    )
    limits_value = _exact_object(
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
    coverage = _exact_object(
        root["coverage"],
        (
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
    if (
        root["schema"] != BUSINESS_CUTOVER_ASSET_STAGE_MANIFEST_SCHEMA
        or root["protocol"] != BUSINESS_CUTOVER_ASSET_STAGE_PROTOCOL
        or root["status"] != BUSINESS_CUTOVER_ASSET_STAGE_STATUS
        or any(
            type(root[name]) is not list
            for name in ("projects", "blobs", "occurrences")
        )
    ):
        _fail("cutover_install_provenance_stage_invalid")
    try:
        limits = BusinessCutoverAssetStageLimits(**limits_value)
        projects = tuple(_project_value(value) for value in root["projects"])
        blobs = tuple(_blob_value(value) for value in root["blobs"])
        occurrences = tuple(_occurrence_value(value) for value in root["occurrences"])
        evidence = BusinessCutoverAssetStageEvidence(
            cutover_id=authority["cutover_id"],
            batch_id=transaction["batch_id"],
            transaction_id=transaction["transaction_id"],
            receipt_kind=transaction["receipt_kind"],
            status=BUSINESS_CUTOVER_ASSET_STAGED_STATUS,
            repository_epoch=authority["repository_epoch"],
            source_admission_revision=authority["source_admission_revision"],
            source_admission_sha256=_hex_digest(authority["source_admission_sha256"]),
            manifest_bytes=row["manifest_bytes"],
            manifest_sha256=row["manifest_sha256"],
            project_count=coverage["project_count"],
            reference_count=coverage["reference_count"],
            unique_blob_count=coverage["unique_blob_count"],
            unique_blob_bytes=coverage["unique_blob_bytes"],
            referenced_bytes=coverage["referenced_bytes"],
            project_root_sha256=_hex_digest(coverage["project_root_sha256"]),
            blob_root_sha256=_hex_digest(coverage["blob_root_sha256"]),
            occurrence_root_sha256=_hex_digest(coverage["occurrence_root_sha256"]),
            coverage_sha256=_hex_digest(coverage["coverage_sha256"]),
            projects=projects,
            blobs=blobs,
            occurrences=occurrences,
            limits=limits,
        )
        evidence = _validate_stage_evidence_against_cutover(cutover, evidence)
    except (
        BusinessCutoverAssetContractError,
        BusinessCutoverAssetIntegrityError,
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        raise SQLiteCutoverInstallProvenanceError(
            "cutover_install_provenance_stage_invalid"
        ) from exc
    expected_row = {
        "batch_id": evidence.batch_id,
        "cutover_id": evidence.cutover_id,
        "transaction_id": evidence.transaction_id,
        "receipt_kind": BUSINESS_CUTOVER_ASSET_STAGE_TRANSACTION_KIND,
        "committed_at": row["committed_at"],
        "repository_epoch": evidence.repository_epoch,
        "source_admission_revision": evidence.source_admission_revision,
        "source_admission_sha256": evidence.source_admission_sha256,
        "member_count": len(projects) + len(blobs) + len(occurrences),
        "project_count": evidence.project_count,
        "blob_count": evidence.unique_blob_count,
        "occurrence_count": evidence.reference_count,
        "unique_blob_bytes": evidence.unique_blob_bytes,
        "referenced_bytes": evidence.referenced_bytes,
        "project_root_sha256": evidence.project_root_sha256,
        "blob_root_sha256": evidence.blob_root_sha256,
        "occurrence_root_sha256": evidence.occurrence_root_sha256,
        "coverage_sha256": evidence.coverage_sha256,
        "manifest_bytes": evidence.manifest_bytes,
        "manifest_sha256": evidence.manifest_sha256,
    }
    if (
        not _canonical_utc(row["committed_at"])
        or not _dict_matches(row, expected_row)
        or authority["pending_admission_sha256"]
        != cutover.closure.pending_admission_sha256.hex()
        or authority["final_admission_sha256"]
        != cutover.closure.final_admission_sha256.hex()
        or authority["runtime_generation"] != cutover.install.runtime.runtime_generation
        or authority["code_digest"] != cutover.install.runtime.code_digest.hex()
    ):
        _fail("cutover_install_provenance_stage_invalid")
    expected_members = _issue_business_cutover_asset_stage_members(
        projects=projects,
        blobs=blobs,
        occurrences=occurrences,
    )
    raw_members = connection.execute(
        "SELECT "
        + ",".join(_STAGE_MEMBER_COLUMNS)
        + " FROM business_asset_stage_members WHERE batch_id=?",
        (evidence.batch_id,),
    ).fetchall()
    observed_members = {
        (value["member_kind"], value["ordinal"]): value
        for value in (
            dict(
                zip(
                    _STAGE_MEMBER_COLUMNS,
                    _row_tuple(raw, _STAGE_MEMBER_COLUMNS),
                )
            )
            for raw in raw_members
        )
    }
    if len(observed_members) != len(expected_members):
        _fail("cutover_install_provenance_stage_member_invalid")
    for member in expected_members:
        observed = observed_members.get((member.member_kind, member.ordinal))
        expected = {
            "batch_id": evidence.batch_id,
            "cutover_id": evidence.cutover_id,
            "member_kind": member.member_kind,
            "ordinal": member.ordinal,
            "member_id": member.member_id,
            "member_bytes": member.member_bytes,
            "member_sha256": member.member_sha256,
            "metadata_sha256": member.metadata_sha256,
        }
        if observed is None or not _dict_matches(observed, expected):
            _fail("cutover_install_provenance_stage_member_invalid")
    receipt = _select_one(
        connection,
        "transaction_receipts",
        _TRANSACTION_COLUMNS,
        "transaction_id=?",
        (evidence.transaction_id,),
        "cutover_install_provenance_stage_receipt_invalid",
    )
    if receipt != {
        "transaction_id": evidence.transaction_id,
        "kind": BUSINESS_CUTOVER_ASSET_STAGE_TRANSACTION_KIND,
        "committed_at": row["committed_at"],
    }:
        _fail("cutover_install_provenance_stage_receipt_invalid")
    return evidence


def _baseline_global_coverage(
    baseline: Mapping[str, Any],
    mapping: Mapping[int, Mapping[str, Any]],
) -> tuple[
    Tuple[BusinessCutoverAssetProjectCoverage, ...],
    Tuple[BusinessCutoverAssetOccurrenceCoverage, ...],
    Tuple[BusinessCutoverAssetBlobCoverage, ...],
]:
    assets = baseline.get("asset_blobs")
    members = baseline.get("members")
    if type(assets) is not list or type(members) is not list:
        _fail("cutover_install_provenance_baseline_invalid")
    metadata = {}
    for ordinal, asset in enumerate(assets):
        if type(asset) is not dict:
            _fail("cutover_install_provenance_baseline_invalid")
        sha256 = asset.get("asset_sha256")
        if (
            _hex_digest(sha256).hex() != sha256
            or type(asset.get("media_type")) is not str
            or type(asset.get("extension")) is not str
            or type(asset.get("byte_length")) is not int
        ):
            _fail("cutover_install_provenance_baseline_invalid")
        metadata[sha256] = (ordinal, asset)
    projects = []
    occurrences = []
    occurrence_counts: dict[str, int] = {}
    occurrence_projects: dict[str, set[str]] = {}
    for ordinal, mapped in mapping.items():
        if mapped["status"] != "live":
            continue
        member = members[ordinal]
        live = member["live"]
        references = live["asset_references"]
        start = len(occurrences)
        hashes = []
        project_shas = set()
        for reference in references:
            sha256 = reference["asset_sha256"]
            descriptor = metadata.get(sha256)
            if descriptor is None:
                _fail("cutover_install_provenance_baseline_invalid")
            blob_ordinal, asset = descriptor
            occurrence = BusinessCutoverAssetOccurrenceCoverage(
                ordinal=len(occurrences),
                project_ordinal=ordinal,
                project_id=mapped["project_id"],
                generation=mapped["generation"],
                business_root_sha256=bytes.fromhex(mapped["business_root_sha256"]),
                projection_sha256=bytes.fromhex(live["asset_projection_sha256"]),
                entity_type=reference["entity_type"],
                entity_id=reference["entity_id"],
                entity_deleted=bool(reference["entity_deleted"]),
                value_path=reference["value_path"],
                blob_ordinal=blob_ordinal,
                asset_sha256=sha256,
                media_type=asset["media_type"],
                byte_length=asset["byte_length"],
                extension=asset["extension"],
            )
            occurrences.append(occurrence)
            hashes.append(_occurrence_hash(occurrence))
            project_shas.add(sha256)
            occurrence_counts[sha256] = occurrence_counts.get(sha256, 0) + 1
            occurrence_projects.setdefault(sha256, set()).add(mapped["project_id"])
        occurrence_sha256 = _digest_with_sequence(
            BUSINESS_CUTOVER_ASSET_OCCURRENCE_HASH_DOMAIN,
            (
                mapped["project_id"].encode("utf-8"),
                bytes.fromhex(live["asset_projection_sha256"]),
                _integer_bytes(len(hashes)),
            ),
            hashes,
        )
        projects.append(
            BusinessCutoverAssetProjectCoverage(
                project_ordinal=ordinal,
                project_id=mapped["project_id"],
                generation=mapped["generation"],
                document_id=mapped["document_id"],
                business_root_sha256=bytes.fromhex(mapped["business_root_sha256"]),
                projection_sha256=bytes.fromhex(live["asset_projection_sha256"]),
                occurrence_start=start,
                occurrence_count=len(hashes),
                unique_blob_count=len(project_shas),
                unique_blob_bytes=sum(
                    metadata[sha256][1]["byte_length"] for sha256 in project_shas
                ),
                occurrence_sha256=occurrence_sha256,
            )
        )
    blobs = tuple(
        BusinessCutoverAssetBlobCoverage(
            blob_ordinal=ordinal,
            asset_sha256=asset["asset_sha256"],
            content_key=asset["content_key"],
            media_type=asset["media_type"],
            byte_length=asset["byte_length"],
            extension=asset["extension"],
            occurrence_count=occurrence_counts.get(asset["asset_sha256"], 0),
            project_count=len(occurrence_projects.get(asset["asset_sha256"], set())),
        )
        for ordinal, asset in enumerate(assets)
    )
    if any(blob.occurrence_count < 1 or blob.project_count < 1 for blob in blobs):
        _fail("cutover_install_provenance_baseline_invalid")
    return tuple(projects), tuple(occurrences), blobs


class SQLiteCutoverInstallProvenanceBinding:
    """Issuer-stamped exact V5 -> V8 -> install provenance observation."""

    __slots__ = (
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
        "source_repository_sha256",
        "catalog_coverage_sha256",
        "catalog_id",
        "catalog_seal_sha256",
        "schema_fingerprint",
        "repository_epoch",
        "member_root_sha256",
        "project_root_sha256",
        "occurrence_root_sha256",
        "blob_root_sha256",
        "project_count",
        "live_project_count",
        "deleted_project_count",
        "batch_count",
        "reference_count",
        "unique_blob_count",
        "unique_blob_bytes",
        "referenced_bytes",
        "_issuer",
    )

    def __init__(self, *, _issuer: Any, **values: Any) -> None:
        if _issuer is not _PROVENANCE_ISSUER or set(values) != set(self.__slots__) - {
            "_issuer"
        }:
            _fail("cutover_install_provenance_issuer_invalid")
        for name, value in values.items():
            object.__setattr__(self, name, value)
        object.__setattr__(self, "_issuer", _issuer)

    def __setattr__(self, name: str, value: Any) -> None:
        del name, value
        raise AttributeError("SQLiteCutoverInstallProvenanceBinding is immutable")


def _require_cutover_install_provenance_binding(
    value: Any,
) -> SQLiteCutoverInstallProvenanceBinding:
    if (
        type(value) is not SQLiteCutoverInstallProvenanceBinding
        or getattr(value, "_issuer", None) is not _PROVENANCE_ISSUER
    ):
        _fail("cutover_install_provenance_binding_invalid")
    return value


def _read_cutover_install_provenance(
    connection: Any,
    *,
    cutover_id: str,
    mapping: Mapping[int, Mapping[str, Any]],
    baseline: Mapping[str, Any],
    member_mapping_sha256: bytes,
    install_transaction_id: str,
    install_manifest_sha256: bytes,
    install_committed_at: str,
    target_schema_version: int,
    target_schema_fingerprint: str,
    closure: Mapping[str, Any],
) -> SQLiteCutoverInstallProvenanceBinding:
    """Recompute all durable provenance named by one V12 closure row."""

    try:
        source = _read_source_proof(
            connection,
            cutover_id=cutover_id,
            mapping=mapping,
        )
        install = _install_projection_exact(
            connection,
            cutover_id=cutover_id,
            mapping=mapping,
            member_mapping_sha256=member_mapping_sha256,
            source=source,
            install_transaction_id=install_transaction_id,
            install_manifest_sha256=install_manifest_sha256,
            install_committed_at=install_committed_at,
            target_schema_version=target_schema_version,
            target_schema_fingerprint=target_schema_fingerprint,
        )
        runtime = install.evidence.runtime
        cutover = _member_cutover_view(cutover_id, mapping)
        cutover.install = SimpleNamespace(runtime=runtime)
        cutover.closure = SimpleNamespace(
            source_admission_sha256=install.source_admission_sha256,
            pending_admission_sha256=install.pending_admission_sha256,
            final_admission_sha256=install.final_admission_sha256,
        )
        cutover.legacy_proof = source.integrity
        coverage_row = _select_one(
            connection,
            "business_asset_coverage_receipts",
            _COVERAGE_COLUMNS,
            "cutover_id=?",
            (cutover_id,),
            "cutover_install_provenance_coverage_invalid",
        )
        if not _canonical_utc(coverage_row["committed_at"]):
            _fail("cutover_install_provenance_coverage_invalid")
        try:
            coverage_projection = _parse_stored_coverage_manifest(
                coverage_row["manifest_bytes"]
            )
            limits = BusinessCutoverAssetStageLimits(
                **coverage_projection.root["limits"]
            )
        except (
            BusinessCutoverStoredManifestError,
            BusinessCutoverAssetContractError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            raise SQLiteCutoverInstallProvenanceError(
                "cutover_install_provenance_coverage_invalid"
            ) from exc
        raw_stage_rows = connection.execute(
            "SELECT "
            + ",".join(_STAGE_COLUMNS)
            + " FROM business_asset_stage_receipts "
            "WHERE cutover_id=? ORDER BY batch_id",
            (cutover_id,),
        ).fetchall()
        stages = tuple(
            _stage_evidence(
                connection,
                dict(zip(_STAGE_COLUMNS, _row_tuple(raw, _STAGE_COLUMNS))),
                cutover=cutover,
            )
            for raw in raw_stage_rows
        )
        if (
            tuple(stage.batch_id for stage in stages)
            != tuple(sorted(stage.batch_id for stage in stages))
            or len({stage.batch_id for stage in stages}) != len(stages)
            or any(stage.limits != limits for stage in stages)
        ):
            _fail("cutover_install_provenance_stage_invalid")
        projects, occurrences, blobs = _baseline_global_coverage(
            baseline,
            mapping,
        )
        required_blobs = {value.asset_sha256: value for value in blobs}
        observed_blobs = {}
        observed_occurrences = {}
        for stage in stages:
            for blob in stage.blobs:
                previous = observed_blobs.get(blob.asset_sha256)
                identity = (
                    blob.content_key,
                    blob.media_type,
                    blob.byte_length,
                    blob.extension,
                )
                if previous is not None and previous != identity:
                    _fail("cutover_install_provenance_coverage_invalid")
                observed_blobs[blob.asset_sha256] = identity
            for occurrence in stage.occurrences:
                key = (
                    occurrence.project_ordinal,
                    occurrence.project_id,
                    occurrence.generation,
                    occurrence.entity_type,
                    occurrence.entity_id,
                    occurrence.entity_deleted,
                    occurrence.value_path,
                    occurrence.asset_sha256,
                )
                identity = (
                    occurrence.business_root_sha256,
                    occurrence.projection_sha256,
                    occurrence.media_type,
                    occurrence.byte_length,
                    occurrence.extension,
                )
                previous = observed_occurrences.get(key)
                if previous is not None and previous != identity:
                    _fail("cutover_install_provenance_coverage_invalid")
                observed_occurrences[key] = identity
        expected_blob_metadata = {
            sha256: (
                value.content_key,
                value.media_type,
                value.byte_length,
                value.extension,
            )
            for sha256, value in required_blobs.items()
        }
        expected_occurrences = {
            (
                value.project_ordinal,
                value.project_id,
                value.generation,
                value.entity_type,
                value.entity_id,
                value.entity_deleted,
                value.value_path,
                value.asset_sha256,
            ): (
                value.business_root_sha256,
                value.projection_sha256,
                value.media_type,
                value.byte_length,
                value.extension,
            )
            for value in occurrences
        }
        if (
            observed_blobs != expected_blob_metadata
            or observed_occurrences != expected_occurrences
            or bool(stages) != bool(occurrences)
        ):
            _fail("cutover_install_provenance_coverage_invalid")
        member_root = _repository_member_root(cutover)
        project_root = _project_root(projects)
        occurrence_root = _occurrence_root(occurrences)
        blob_root = _blob_root(blobs)
        observed_occurrence_count = sum(len(stage.occurrences) for stage in stages)
        observed_project_row_count = sum(len(stage.projects) for stage in stages)
        observed_blob_row_count = sum(len(stage.blobs) for stage in stages)
        observed_row_count = (
            observed_occurrence_count
            + observed_project_row_count
            + observed_blob_row_count
        )
        observed_manifest_bytes = sum(len(stage.manifest_bytes) for stage in stages)
        unique_blob_bytes = sum(blob.byte_length for blob in blobs)
        referenced_bytes = sum(value.byte_length for value in occurrences)
        coverage_sha256 = _digest_with_sequence(
            BUSINESS_CUTOVER_ASSET_REPOSITORY_COVERAGE_HASH_DOMAIN,
            (
                cutover_id.encode("ascii"),
                runtime.repository_epoch.encode("ascii"),
                _integer_bytes(runtime.source_admission_revision),
                install.source_admission_sha256,
                runtime.runtime_generation.encode("utf-8"),
                runtime.code_digest,
                _integer_bytes(source.integrity.project_count),
                _integer_bytes(source.integrity.live_project_count),
                _integer_bytes(source.integrity.deleted_project_count),
                member_root,
                _integer_bytes(len(stages)),
                _integer_bytes(observed_occurrence_count),
                _integer_bytes(observed_project_row_count),
                _integer_bytes(observed_blob_row_count),
                _integer_bytes(observed_row_count),
                _integer_bytes(observed_manifest_bytes),
            ),
            (value.manifest_sha256 for value in stages),
            (
                _integer_bytes(len(projects)),
                _integer_bytes(len(occurrences)),
                _integer_bytes(len(blobs)),
                _integer_bytes(unique_blob_bytes),
                _integer_bytes(referenced_bytes),
                project_root,
                occurrence_root,
                blob_root,
            ),
        )
        expected_root = {
            "schema": BUSINESS_CUTOVER_ASSET_COVERAGE_MANIFEST_SCHEMA,
            "protocol": BUSINESS_CUTOVER_ASSET_COVERAGE_PROTOCOL,
            "status": BUSINESS_CUTOVER_ASSET_COVERAGE_STATUS,
            "transaction": {
                "transaction_id": coverage_row["transaction_id"],
                "receipt_kind": BUSINESS_CUTOVER_ASSET_COVERAGE_TRANSACTION_KIND,
            },
            "authority": {
                "repository_epoch": runtime.repository_epoch,
                "cutover_id": cutover_id,
                "source_admission_revision": runtime.source_admission_revision,
                "source_admission_sha256": install.source_admission_sha256.hex(),
                "pending_admission_sha256": install.pending_admission_sha256.hex(),
                "runtime_generation": runtime.runtime_generation,
                "code_digest": runtime.code_digest.hex(),
            },
            "limits": coverage_projection.root["limits"],
            "repository": {
                "project_count": source.integrity.project_count,
                "live_project_count": source.integrity.live_project_count,
                "deleted_project_count": source.integrity.deleted_project_count,
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
                for value in stages
            ],
            "observed": {
                "batch_count": len(stages),
                "occurrence_count": observed_occurrence_count,
                "project_row_count": observed_project_row_count,
                "blob_row_count": observed_blob_row_count,
                "row_count": observed_row_count,
                "manifest_bytes": observed_manifest_bytes,
            },
            "coverage": {
                "batch_count": len(stages),
                "project_count": len(projects),
                "reference_count": len(occurrences),
                "unique_blob_count": len(blobs),
                "unique_blob_bytes": unique_blob_bytes,
                "referenced_bytes": referenced_bytes,
                "project_root_sha256": project_root.hex(),
                "occurrence_root_sha256": occurrence_root.hex(),
                "blob_root_sha256": blob_root.hex(),
                "coverage_sha256": coverage_sha256.hex(),
            },
            "projects": [_project_manifest(value) for value in projects],
            "blobs": [_blob_manifest(value) for value in blobs],
            "occurrences": [_occurrence_manifest(value) for value in occurrences],
        }
        expected_manifest = _manifest(expected_root)
        expected_manifest_sha256 = _digest(
            BUSINESS_CUTOVER_ASSET_COVERAGE_MANIFEST_HASH_DOMAIN,
            expected_manifest,
        )
        expected_coverage_row = {
            "cutover_id": cutover_id,
            "transaction_id": coverage_row["transaction_id"],
            "receipt_kind": BUSINESS_CUTOVER_ASSET_COVERAGE_TRANSACTION_KIND,
            "committed_at": coverage_row["committed_at"],
            "repository_epoch": runtime.repository_epoch,
            "source_admission_revision": runtime.source_admission_revision,
            "source_admission_sha256": install.source_admission_sha256,
            "batch_count": len(stages),
            "member_root_sha256": member_root,
            "repository_member_count": source.integrity.project_count,
            "live_project_count": source.integrity.live_project_count,
            "deleted_project_count": source.integrity.deleted_project_count,
            "observed_occurrence_count": observed_occurrence_count,
            "observed_row_count": observed_row_count,
            "observed_manifest_bytes": observed_manifest_bytes,
            "project_count": len(projects),
            "reference_count": len(occurrences),
            "unique_blob_count": len(blobs),
            "unique_blob_bytes": unique_blob_bytes,
            "referenced_bytes": referenced_bytes,
            "project_root_sha256": project_root,
            "blob_root_sha256": blob_root,
            "occurrence_root_sha256": occurrence_root,
            "coverage_sha256": coverage_sha256,
            "manifest_bytes": expected_manifest,
            "manifest_sha256": expected_manifest_sha256,
        }
        coverage_receipt = _select_one(
            connection,
            "transaction_receipts",
            _TRANSACTION_COLUMNS,
            "transaction_id=?",
            (coverage_row["transaction_id"],),
            "cutover_install_provenance_coverage_receipt_invalid",
        )
        asset_binding = baseline.get("asset_blob_write", {}).get("asset_coverage")
        expected_asset_binding = {
            "transaction_id": coverage_row["transaction_id"],
            "receipt_kind": BUSINESS_CUTOVER_ASSET_COVERAGE_TRANSACTION_KIND,
            "manifest_sha256": expected_manifest_sha256.hex(),
            "coverage_sha256": coverage_sha256.hex(),
            "project_root_sha256": project_root.hex(),
            "occurrence_root_sha256": occurrence_root.hex(),
            "blob_root_sha256": blob_root.hex(),
        }
        expected_pointers = {
            "source_transaction_id": source.integrity.transaction_id,
            "source_receipt_kind": LEGACY_REPOSITORY_TRANSACTION_KIND,
            "source_admission_revision": source.integrity.repository_revision,
            "source_proof_manifest_sha256": source.integrity.manifest_sha256,
            "source_proof_sha256": source.integrity.proof_sha256,
            "source_admission_sha256": install.source_admission_sha256,
            "coverage_transaction_id": coverage_row["transaction_id"],
            "coverage_receipt_kind": BUSINESS_CUTOVER_ASSET_COVERAGE_TRANSACTION_KIND,
            "coverage_manifest_sha256": expected_manifest_sha256,
            "coverage_sha256": coverage_sha256,
        }
        if (
            coverage_projection.root != expected_root
            or not _dict_matches(coverage_row, expected_coverage_row)
            or coverage_receipt
            != {
                "transaction_id": coverage_row["transaction_id"],
                "kind": BUSINESS_CUTOVER_ASSET_COVERAGE_TRANSACTION_KIND,
                "committed_at": coverage_row["committed_at"],
            }
            or asset_binding != expected_asset_binding
            or any(
                not _same(closure.get(key), value)
                for key, value in expected_pointers.items()
            )
            or source.committed_at > coverage_row["committed_at"]
            or coverage_row["committed_at"] > install_committed_at
            or any(
                not source.committed_at
                <= row["committed_at"]
                <= coverage_row["committed_at"]
                for row in (
                    dict(zip(_STAGE_COLUMNS, _row_tuple(raw, _STAGE_COLUMNS)))
                    for raw in raw_stage_rows
                )
            )
        ):
            _fail("cutover_install_provenance_coverage_invalid")
        return SQLiteCutoverInstallProvenanceBinding(
            **expected_pointers,
            source_repository_sha256=source.integrity.source_repository_sha256,
            catalog_coverage_sha256=source.integrity.catalog_coverage_sha256,
            catalog_id=source.integrity.catalog_id,
            catalog_seal_sha256=source.catalog_seal_sha256,
            schema_fingerprint=source.schema_fingerprint,
            repository_epoch=runtime.repository_epoch,
            member_root_sha256=member_root,
            project_root_sha256=project_root,
            occurrence_root_sha256=occurrence_root,
            blob_root_sha256=blob_root,
            project_count=source.integrity.project_count,
            live_project_count=source.integrity.live_project_count,
            deleted_project_count=source.integrity.deleted_project_count,
            batch_count=len(stages),
            reference_count=len(occurrences),
            unique_blob_count=len(blobs),
            unique_blob_bytes=unique_blob_bytes,
            referenced_bytes=referenced_bytes,
            _issuer=_PROVENANCE_ISSUER,
        )
    except SQLiteCutoverInstallProvenanceError:
        raise
    except Exception as exc:
        raise SQLiteCutoverInstallProvenanceError(
            "cutover_install_provenance_unavailable"
        ) from exc


__all__ = ()
