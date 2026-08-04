"""Fresh semantic verifier for the schema-v12 cutover install closure.

SQLite can enforce row shape, parentage, counts, and append-only history, but
it cannot natively recompute the domain-separated hashes carried by the V12
artifact chunks and baseline sidecars.  This private reader performs that
second layer inside the caller's existing read transaction before a V12 mount
may mint production authority.

It performs no writes and exports no public SDK surface.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any, Mapping, Tuple

from asset_reference_projection import (
    ASSET_REFERENCE_PROJECTION_PROTOCOL,
    AssetReferenceMultiplicity,
    AssetReferenceOccurrence,
    AssetReferenceProjection,
    AssetReferenceProjectionError,
    AssetReferenceValue,
    project_asset_references,
    verify_asset_reference_projection,
)
from business_atomic_write_contract import (
    ATOMIC_BUSINESS_WRITE_MANIFEST_SCHEMA,
    ATOMIC_BUSINESS_WRITE_PLAN_STATUS,
    ATOMIC_BUSINESS_WRITE_PROTOCOL,
    AtomicBusinessWriteGenericReceiptEvidence,
)
from business_atomic_write_integrity import (
    ATOMIC_BUSINESS_WRITE_CONTROLS_HASH_DOMAIN,
    _atomic_receipt_digest,
    _digest as _atomic_digest,
    _manifest_digest as _atomic_manifest_digest,
)
from business_baseline_contract import (
    BUSINESS_BASELINE_MANIFEST_PROTOCOL,
    BUSINESS_BASELINE_MANIFEST_SCHEMA,
    digest_business_baseline_manifest,
)
from business_codec import (
    BusinessCodecError,
    decode_payload_bounded,
    encode_payload_bounded,
)
from business_cutover_baseline_projection import (
    ASSET_BLOBS_COLUMNS,
    BUSINESS_COMMITS_COLUMNS,
    BUSINESS_CUTOVER_BASELINE_PROJECTION_STATUS,
    BUSINESS_CUTOVER_BASELINE_TRACE_PREFIX,
    DOCUMENT_ASSET_REFS_COLUMNS,
    DOCUMENT_COMMIT_HEADS_COLUMNS,
    DOCUMENTS_COLUMNS,
    ENTITIES_COLUMNS,
    OUTBOX_DISPATCH_HEAD_COLUMNS,
    PROJECTS_COLUMNS,
    REPOSITORY_PROJECTS_COLUMNS,
    TRANSACTION_RECEIPTS_COLUMNS,
    WORKER_LEDGER_HEAD_COLUMNS,
    _projection_digest,
)
from business_cutover_contract import (
    BUSINESS_CUTOVER_AUTHORITY,
    BUSINESS_CUTOVER_INSTALL_AUTHORITY_STATE,
    BUSINESS_CUTOVER_INSTALL_TRANSACTION_KIND,
    BUSINESS_CUTOVER_INSTALL_STATE_REVISION,
    BUSINESS_CUTOVER_MEMBER_MAPPING_SCHEMA,
    BUSINESS_CUTOVER_PROTOCOL,
    BUSINESS_CUTOVER_SAVE_POLICIES,
)
from business_cutover_integrity import (
    BUSINESS_CUTOVER_INSTALL_MANIFEST_HASH_DOMAIN,
    BUSINESS_CUTOVER_MEMBER_MAPPING_HASH_DOMAIN,
    _baseline_commit_id,
    _domain_digest,
)
from business_cutover_install_closure import (
    BASELINE_ATOMIC_RECEIPT_COLUMNS,
    BUSINESS_CUTOVER_ARTIFACT_CHUNK_HASH_DOMAIN,
    BUSINESS_CUTOVER_BASELINE_OUTER_ATOMIC_STATUS,
    BUSINESS_CUTOVER_BASELINE_OUTER_MANIFEST_HASH_DOMAIN,
    BUSINESS_CUTOVER_BASELINE_OUTER_PROTOCOL,
    BUSINESS_CUTOVER_BASELINE_OUTER_RECEIPT_HASH_DOMAIN,
    BUSINESS_CUTOVER_BASELINE_OUTER_SCHEMA,
    BUSINESS_CUTOVER_BASELINE_PROJECTION_ARTIFACT,
    BUSINESS_CUTOVER_BASELINE_PROJECTION_V12_HASH_DOMAIN,
    BUSINESS_CUTOVER_BASELINE_PROJECTION_V12_PROTOCOL,
    BUSINESS_CUTOVER_BASELINE_PROJECTION_V12_SCHEMA,
    BUSINESS_CUTOVER_INSTALL_CLOSURE_PROTOCOL,
    BUSINESS_CUTOVER_INSTALL_CLOSURE_SCHEMA,
    BUSINESS_CUTOVER_INSTALL_CLOSURE_STATUS,
    BUSINESS_CUTOVER_MEMBER_MAPPING_ARTIFACT,
    INSTALL_ARTIFACT_CHUNK_COLUMNS,
    INSTALL_ARTIFACT_COLUMNS,
    INSTALL_CLOSURE_COLUMNS,
    BusinessCutoverBaselineAtomicReceiptRow,
    _baseline_receipt_root,
    _closure_manifest_digest,
    _digest,
    _empty_targets_sha256,
    _integer,
    _reconstruct_baseline_projection_v1_from_v2,
    _text,
)
from business_cutover_stored_manifest import (
    BusinessCutoverStoredManifestError,
    _parse_stored_install_manifest,
)
from business_snapshot import (
    BusinessSnapshotError,
    BusinessSnapshotSet,
    DocumentSnapshotRow,
    EntitySnapshotRow,
    ProjectSnapshotRow,
    verify_business_snapshot,
)
from business_transaction_integrity import _component_digest
from sqlite_cutover_install_provenance_reader import (
    SQLiteCutoverInstallProvenanceError,
    _read_cutover_install_provenance,
    _require_cutover_install_provenance_binding,
)
from sqlite_schema import (
    BUSINESS_TRANSACTION_KIND,
    BUSINESS_TRANSACTION_PREFIX,
    MAX_BUSINESS_CUTOVER_ARTIFACT_BYTES,
    MAX_BUSINESS_CUTOVER_ARTIFACT_CHUNK_BYTES,
    MAX_BUSINESS_CUTOVER_ARTIFACT_CHUNKS,
    MAX_BUSINESS_SMALL_PAYLOAD_BYTES,
)
from worker_execution_contract import EMPTY_WORKER_LEDGER_HEAD


_READER_ISSUER = object()
_ZERO = b"\0" * 32
_EMPTY_SOURCE_SHA256 = hashlib.sha256(b"").hexdigest()
_LOWER_HEX = frozenset("0123456789abcdef")
_MAPPING_MEMBER_KEYS = frozenset(
    {
        "ordinal",
        "project_id",
        "generation",
        "status",
        "operation_id",
        "source_state_sha256",
        "source_state_bytes",
        "catalog_ordinal",
        "import_id",
        "document_id",
        "document_version",
        "storage_version",
        "business_root_sha256",
        "entity_count",
        "row_snapshot_bytes",
        "save_policy",
        "operation_tail_sha256",
        "asset_projection_sha256",
        "asset_reference_count",
        "unique_asset_reference_count",
        "document_snapshot_sha256",
        "baseline_commit_id",
        "baseline_repository_revision",
        "baseline_manifest_sha256",
    }
)
_BASELINE_KEYS = frozenset(
    {
        "schema",
        "protocol",
        "status",
        "cutover_id",
        "committed_at",
        "runtime_repository_revision",
        "project_count",
        "live_project_count",
        "deleted_project_count",
        "total_snapshot_bytes",
        "entity_row_count",
        "asset_reference_row_count",
        "unique_asset_blob_count",
        "outer_atomic_status",
        "asset_blobs",
        "asset_blob_write",
        "scheduler_genesis",
        "source_projection_v1",
        "members",
    }
)
_BASELINE_LIVE_KEYS = frozenset(
    {
        "commit_id",
        "repository_revision",
        "project_revision",
        "head_revision",
        "trace_id",
        "before_root_sha256",
        "operation_tail_sha256",
        "project",
        "document",
        "entities",
        "asset_projection_sha256",
        "asset_reference_count",
        "asset_references",
        "entity_count",
        "baseline_manifest_sha256",
        "atomic_manifest_sha256",
        "atomic_receipt_sha256",
    }
)
_BASELINE_PROJECT_KEYS = frozenset({"snapshot_bytes", "snapshot_sha256"})
_BASELINE_DOCUMENT_KEYS = frozenset(
    {
        "document_id",
        "document_version",
        "snapshot_bytes",
        "snapshot_sha256",
    }
)
_BASELINE_ENTITY_KEYS = frozenset(
    {
        "entity_type",
        "entity_id",
        "entity_version",
        "deleted",
        "snapshot_bytes",
        "snapshot_sha256",
    }
)
_BASELINE_REFERENCE_KEYS = frozenset(
    {
        "ordinal",
        "asset_sha256",
        "entity_type",
        "entity_id",
        "entity_deleted",
        "value_path",
        "ref_mime",
        "ref_size",
        "ref_ext",
    }
)
_BASELINE_ASSET_KEYS = frozenset(
    {
        "asset_sha256",
        "content_key",
        "media_type",
        "extension",
        "byte_length",
        "status",
        "state_revision",
        "created_at",
        "updated_at",
    }
)
_MAPPING_DELETED_NULL_FIELDS = frozenset(
    {
        "catalog_ordinal",
        "import_id",
        "document_id",
        "document_version",
        "storage_version",
        "business_root_sha256",
        "entity_count",
        "row_snapshot_bytes",
        "save_policy",
        "operation_tail_sha256",
        "asset_projection_sha256",
        "asset_reference_count",
        "unique_asset_reference_count",
        "document_snapshot_sha256",
        "baseline_commit_id",
        "baseline_repository_revision",
        "baseline_manifest_sha256",
    }
)


class SQLiteCutoverInstallClosureError(ValueError):
    """Stable, payload-free V12 stored-closure verification failure."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        if type(code) is not str or not code:
            raise TypeError("code must be a non-empty exact string")
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise SQLiteCutoverInstallClosureError(code)


def _same(left: Any, right: Any) -> bool:
    if type(left) is bytes and type(right) is bytes:
        return hmac.compare_digest(left, right)
    return type(left) is type(right) and left == right


def _digest_value(value: Any) -> bytes:
    if type(value) is not bytes or len(value) != 32:
        _fail("cutover_install_closure_digest_invalid")
    return value


def _hex(value: Any, length: int) -> str:
    if (
        type(value) is not str
        or len(value) != length
        or any(character not in _LOWER_HEX for character in value)
    ):
        _fail("cutover_install_closure_identity_invalid")
    return value


def _count(value: Any, maximum: int) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        _fail("cutover_install_closure_count_invalid")
    return value


def _canonical_payload(value: bytes, limit: int, nodes: int) -> Any:
    if type(value) is not bytes or not 1 <= len(value) <= limit:
        _fail("cutover_install_closure_payload_invalid")
    try:
        decoded = decode_payload_bounded(value, limit, nodes)
        rebuilt = encode_payload_bounded(decoded, limit, nodes)
    except BusinessCodecError as exc:
        raise SQLiteCutoverInstallClosureError(
            "cutover_install_closure_payload_invalid"
        ) from exc
    if rebuilt != value:
        _fail("cutover_install_closure_payload_noncanonical")
    return decoded


def _row_tuple(row: Any, columns: Tuple[str, ...]) -> tuple[Any, ...]:
    if row is None:
        _fail("cutover_install_closure_row_missing")
    try:
        values = tuple(row[column] for column in columns)
    except (IndexError, KeyError, TypeError) as exc:
        raise SQLiteCutoverInstallClosureError(
            "cutover_install_closure_row_invalid"
        ) from exc
    if len(values) != len(columns):
        _fail("cutover_install_closure_row_invalid")
    return values


class SQLiteCutoverInstallClosureBinding:
    """Issuer-stamped result of one exact stored V12 closure snapshot."""

    __slots__ = (
        "cutover_id",
        "install_transaction_id",
        "install_manifest_sha256",
        "member_mapping_sha256",
        "baseline_projection_sha256",
        "baseline_receipt_root_sha256",
        "closure_manifest_sha256",
        "source_proof_sha256",
        "source_admission_sha256",
        "coverage_manifest_sha256",
        "coverage_sha256",
        "project_count",
        "live_project_count",
        "deleted_project_count",
        "baseline_commit_count",
        "_issuer",
    )

    def __init__(
        self,
        *,
        cutover_id: str,
        install_transaction_id: str,
        install_manifest_sha256: bytes,
        member_mapping_sha256: bytes,
        baseline_projection_sha256: bytes,
        baseline_receipt_root_sha256: bytes,
        closure_manifest_sha256: bytes,
        source_proof_sha256: bytes,
        source_admission_sha256: bytes,
        coverage_manifest_sha256: bytes,
        coverage_sha256: bytes,
        project_count: int,
        live_project_count: int,
        deleted_project_count: int,
        baseline_commit_count: int,
        _issuer: Any,
    ) -> None:
        if _issuer is not _READER_ISSUER:
            _fail("cutover_install_closure_reader_issuer_invalid")
        for name, value in locals().copy().items():
            if name not in {"self", "_issuer"}:
                object.__setattr__(self, name, value)
        object.__setattr__(self, "_issuer", _issuer)

    def __setattr__(self, name: str, value: Any) -> None:
        del name, value
        raise AttributeError("SQLiteCutoverInstallClosureBinding is immutable")


def _require_cutover_install_closure_binding(
    value: Any,
) -> SQLiteCutoverInstallClosureBinding:
    if (
        type(value) is not SQLiteCutoverInstallClosureBinding
        or getattr(value, "_issuer", None) is not _READER_ISSUER
    ):
        _fail("cutover_install_closure_reader_binding_invalid")
    return value


def _artifact(
    connection: Any,
    *,
    cutover_id: str,
    artifact_kind: str,
) -> tuple[Mapping[str, Any], bytes, Any]:
    row = connection.execute(
        "SELECT "
        + ",".join(INSTALL_ARTIFACT_COLUMNS)
        + " FROM business_cutover_install_artifacts "
        "WHERE cutover_id=? AND artifact_kind=?",
        (cutover_id, artifact_kind),
    ).fetchone()
    values = _row_tuple(row, INSTALL_ARTIFACT_COLUMNS)
    header = dict(zip(INSTALL_ARTIFACT_COLUMNS, values))
    if (
        header["cutover_id"] != cutover_id
        or header["artifact_kind"] != artifact_kind
        or type(header["artifact_schema"]) is not str
        or type(header["byte_length"]) is not int
        or not 1 <= header["byte_length"] <= MAX_BUSINESS_CUTOVER_ARTIFACT_BYTES
        or type(header["chunk_count"]) is not int
        or not 1 <= header["chunk_count"] <= MAX_BUSINESS_CUTOVER_ARTIFACT_CHUNKS
    ):
        _fail("cutover_install_closure_artifact_header_invalid")
    artifact_sha256 = _digest_value(header["artifact_sha256"])
    rows = connection.execute(
        "SELECT "
        + ",".join(INSTALL_ARTIFACT_CHUNK_COLUMNS)
        + " FROM business_cutover_install_artifact_chunks "
        "WHERE cutover_id=? AND artifact_kind=? ORDER BY ordinal",
        (cutover_id, artifact_kind),
    ).fetchall()
    if len(rows) != header["chunk_count"]:
        _fail("cutover_install_closure_artifact_chunks_incomplete")
    chunks = []
    for expected_ordinal, row in enumerate(rows):
        chunk_values = _row_tuple(row, INSTALL_ARTIFACT_CHUNK_COLUMNS)
        chunk = dict(zip(INSTALL_ARTIFACT_CHUNK_COLUMNS, chunk_values))
        expected_offset = expected_ordinal * MAX_BUSINESS_CUTOVER_ARTIFACT_CHUNK_BYTES
        expected_length = min(
            MAX_BUSINESS_CUTOVER_ARTIFACT_CHUNK_BYTES,
            header["byte_length"] - expected_offset,
        )
        chunk_bytes = chunk["chunk_bytes"]
        if (
            chunk["cutover_id"] != cutover_id
            or chunk["artifact_kind"] != artifact_kind
            or chunk["ordinal"] != expected_ordinal
            or chunk["byte_offset"] != expected_offset
            or type(chunk_bytes) is not bytes
            or expected_length < 1
            or len(chunk_bytes) != expected_length
        ):
            _fail("cutover_install_closure_artifact_chunk_invalid")
        expected_chunk_sha256 = _digest(
            BUSINESS_CUTOVER_ARTIFACT_CHUNK_HASH_DOMAIN,
            _text(cutover_id),
            _text(artifact_kind),
            _integer(expected_ordinal),
            _integer(expected_offset),
            chunk_bytes,
        )
        if not hmac.compare_digest(
            _digest_value(chunk["chunk_sha256"]),
            expected_chunk_sha256,
        ):
            _fail("cutover_install_closure_artifact_chunk_hash_mismatch")
        chunks.append(chunk_bytes)
    artifact_bytes = b"".join(chunks)
    if len(artifact_bytes) != header["byte_length"]:
        _fail("cutover_install_closure_artifact_length_mismatch")
    domain = (
        BUSINESS_CUTOVER_MEMBER_MAPPING_HASH_DOMAIN
        if artifact_kind == BUSINESS_CUTOVER_MEMBER_MAPPING_ARTIFACT
        else BUSINESS_CUTOVER_BASELINE_PROJECTION_V12_HASH_DOMAIN
    )
    if not hmac.compare_digest(artifact_sha256, _digest(domain, artifact_bytes)):
        _fail("cutover_install_closure_artifact_hash_mismatch")
    return (
        header,
        artifact_bytes,
        _canonical_payload(
            artifact_bytes,
            MAX_BUSINESS_CUTOVER_ARTIFACT_BYTES,
            1_000_000,
        ),
    )


def _mapping(
    value: Any,
    cutover_id: str,
) -> tuple[dict[int, Mapping[str, Any]], dict[str, int]]:
    if type(value) is not dict or set(value) != {
        "schema",
        "protocol",
        "cutover_id",
        "project_count",
        "live_project_count",
        "deleted_project_count",
        "members",
    }:
        _fail("cutover_install_closure_member_mapping_invalid")
    counts = {
        "project_count": _count(value["project_count"], 10_000),
        "live_project_count": _count(value["live_project_count"], 10_000),
        "deleted_project_count": _count(value["deleted_project_count"], 10_000),
    }
    members = value["members"]
    if (
        value["schema"] != BUSINESS_CUTOVER_MEMBER_MAPPING_SCHEMA
        or value["protocol"] != BUSINESS_CUTOVER_PROTOCOL
        or value["cutover_id"] != cutover_id
        or counts["project_count"]
        != (counts["live_project_count"] + counts["deleted_project_count"])
        or type(members) is not list
        or len(members) != counts["project_count"]
    ):
        _fail("cutover_install_closure_member_mapping_invalid")
    result: dict[int, Mapping[str, Any]] = {}
    live_revision = 0
    identities = set()
    project_ids = []
    for ordinal, member in enumerate(members):
        if (
            type(member) is not dict
            or set(member) != _MAPPING_MEMBER_KEYS
            or member.get("ordinal") != ordinal
            or member.get("status") not in {"live", "deleted"}
            or type(member.get("project_id")) is not str
            or not member["project_id"]
            or type(member.get("generation")) is not int
            or member["generation"] < 1
            or type(member.get("operation_id")) is not str
            or not member["operation_id"]
            or _hex(member.get("source_state_sha256"), 64)
            != member["source_state_sha256"]
            or type(member.get("source_state_bytes")) is not int
            or member["source_state_bytes"] < 0
        ):
            _fail("cutover_install_closure_member_mapping_invalid")
        identity = (member["project_id"], member["generation"])
        if identity in identities:
            _fail("cutover_install_closure_member_mapping_invalid")
        identities.add(identity)
        project_ids.append(member["project_id"])
        if member["status"] == "live":
            live_revision += 1
            if (
                not (
                    member["operation_id"] == "legacy-adopt"
                    or (
                        len(member["operation_id"]) == 32
                        and all(
                            character in _LOWER_HEX
                            for character in member["operation_id"]
                        )
                    )
                )
                or (
                    member["operation_id"] == "legacy-adopt"
                    and member["generation"] != 1
                )
                or member["source_state_bytes"] < 1
                or member.get("baseline_repository_revision") != live_revision
                or _hex(member.get("baseline_commit_id"), 32)
                != member["baseline_commit_id"]
                or member["baseline_commit_id"]
                != _baseline_commit_id(
                    cutover_id,
                    ordinal,
                    member["project_id"],
                    member["generation"],
                )
                or _hex(member.get("baseline_manifest_sha256"), 64)
                != member["baseline_manifest_sha256"]
                or type(member.get("catalog_ordinal")) is not int
                or member["catalog_ordinal"] < 0
                or _hex(member.get("import_id"), 64) != member["import_id"]
                or type(member.get("document_id")) is not str
                or not member["document_id"]
                or type(member.get("document_version")) is not int
                or member["document_version"] < 1
                or type(member.get("storage_version")) is not str
                or not member["storage_version"]
                or _hex(member.get("business_root_sha256"), 64)
                != member["business_root_sha256"]
                or _hex(member.get("document_snapshot_sha256"), 64)
                != member["document_snapshot_sha256"]
                or type(member.get("entity_count")) is not int
                or member["entity_count"] < 0
                or type(member.get("row_snapshot_bytes")) is not int
                or member["row_snapshot_bytes"] < 1
                or member.get("save_policy") not in BUSINESS_CUTOVER_SAVE_POLICIES
                or member.get("operation_tail_sha256") != _ZERO.hex()
                or _hex(member.get("asset_projection_sha256"), 64)
                != member["asset_projection_sha256"]
                or type(member.get("asset_reference_count")) is not int
                or member["asset_reference_count"] < 0
                or type(member.get("unique_asset_reference_count")) is not int
                or not 0
                <= member["unique_asset_reference_count"]
                <= (member["asset_reference_count"])
            ):
                _fail("cutover_install_closure_member_mapping_invalid")
        elif (
            len(member["operation_id"]) != 32
            or any(character not in _LOWER_HEX for character in member["operation_id"])
            or member["source_state_bytes"] != 0
            or member["source_state_sha256"] != _EMPTY_SOURCE_SHA256
            or any(member[field] is not None for field in _MAPPING_DELETED_NULL_FIELDS)
        ):
            _fail("cutover_install_closure_member_mapping_invalid")
        result[ordinal] = member
    if live_revision != counts["live_project_count"] or project_ids != sorted(
        project_ids
    ):
        _fail("cutover_install_closure_member_mapping_invalid")
    return result, counts


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


def _runtime(connection: Any, cutover_id: str) -> dict[str, Any]:
    columns = (
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
    )
    runtime = _select_one(
        connection,
        "repository_runtime_control",
        columns,
        "singleton=1",
        (),
        "cutover_install_closure_runtime_invalid",
    )
    if (
        runtime["authority"] != BUSINESS_CUTOVER_AUTHORITY
        or runtime["authority_state"] not in {"read_only", "active"}
        or runtime["cutover_id"] != cutover_id
        or type(runtime["state_revision"]) is not int
        or runtime["state_revision"] < BUSINESS_CUTOVER_INSTALL_STATE_REVISION
        or (runtime["authority_state"] == BUSINESS_CUTOVER_INSTALL_AUTHORITY_STATE)
        != (runtime["state_revision"] == BUSINESS_CUTOVER_INSTALL_STATE_REVISION)
        or type(runtime["repository_epoch"]) is not str
        or _hex(runtime["repository_epoch"], 64) != runtime["repository_epoch"]
        or type(runtime["source_admission_revision"]) is not int
        or runtime["source_admission_revision"] < 1
        or type(runtime["runtime_generation"]) is not str
        or not runtime["runtime_generation"]
        or type(runtime["code_digest"]) is not bytes
        or len(runtime["code_digest"]) != 32
        or type(runtime["schema_version"]) is not int
    ):
        _fail("cutover_install_closure_runtime_invalid")
    return runtime


def _install_runtime(
    install: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    cutover_id: str,
    install_transaction_id: str,
    install_manifest_sha256: bytes,
    install_committed_at: str,
    member_mapping_sha256: bytes,
    target_schema_version: int,
    target_schema_fingerprint: str,
) -> tuple[dict[str, Any], Mapping[str, Any]]:
    try:
        projection = _parse_stored_install_manifest(install["manifest_bytes"])
    except BusinessCutoverStoredManifestError as exc:
        raise SQLiteCutoverInstallClosureError(
            "cutover_install_closure_install_receipt_invalid"
        ) from exc
    evidence = projection.evidence
    runtime_value = evidence.runtime
    runtime = {
        "authority": runtime_value.authority,
        "authority_state": runtime_value.authority_state,
        "repository_epoch": runtime_value.repository_epoch,
        "cutover_id": runtime_value.cutover_id,
        "source_admission_revision": runtime_value.source_admission_revision,
        "repository_revision": runtime_value.repository_revision,
        "state_revision": runtime_value.state_revision,
        "runtime_generation": runtime_value.runtime_generation,
        "code_digest": runtime_value.code_digest,
        "schema_version": runtime_value.schema_version,
    }
    mapping = projection.member_mapping
    if (
        evidence.cutover_id != cutover_id
        or evidence.transaction_id != install_transaction_id
        or evidence.receipt_kind != BUSINESS_CUTOVER_INSTALL_TRANSACTION_KIND
        or not hmac.compare_digest(evidence.manifest_sha256, install_manifest_sha256)
        or not hmac.compare_digest(
            evidence.member_mapping_sha256, member_mapping_sha256
        )
        or projection.target_schema_version != target_schema_version
        or projection.target_schema_fingerprint != target_schema_fingerprint
        or runtime["authority"] != BUSINESS_CUTOVER_AUTHORITY
        or runtime["authority_state"] != BUSINESS_CUTOVER_INSTALL_AUTHORITY_STATE
        or runtime["repository_epoch"] != install["repository_epoch"]
        or runtime["cutover_id"] != cutover_id
        or runtime["state_revision"] != BUSINESS_CUTOVER_INSTALL_STATE_REVISION
        or runtime["runtime_generation"] != install["runtime_generation"]
        or not hmac.compare_digest(runtime["code_digest"], install["code_digest"])
        or mapping["schema"] != BUSINESS_CUTOVER_MEMBER_MAPPING_SCHEMA
        or mapping["sha256"] != member_mapping_sha256.hex()
        or any(
            not _same(current[key], expected)
            for key, expected in {
                "repository_epoch": runtime["repository_epoch"],
                "cutover_id": cutover_id,
                "source_admission_revision": runtime["source_admission_revision"],
                "runtime_generation": runtime["runtime_generation"],
                "code_digest": runtime["code_digest"],
                "schema_version": runtime["schema_version"],
            }.items()
        )
        or current["repository_revision"] < runtime["repository_revision"]
        or current["state_revision"] < runtime["state_revision"]
    ):
        _fail("cutover_install_closure_install_receipt_invalid")
    del install_committed_at
    return runtime, mapping


def _snapshot(
    connection: Any,
    member: Mapping[str, Any],
) -> tuple[BusinessSnapshotSet, tuple[dict[str, Any], ...]]:
    project = _select_one(
        connection,
        "projects",
        PROJECTS_COLUMNS,
        "project_id=?",
        (member["project_id"],),
        "cutover_install_closure_baseline_snapshot_invalid",
    )
    document = _select_one(
        connection,
        "documents",
        DOCUMENTS_COLUMNS,
        "project_id=?",
        (member["project_id"],),
        "cutover_install_closure_baseline_snapshot_invalid",
    )
    raw_entities = connection.execute(
        "SELECT " + ",".join(ENTITIES_COLUMNS) + " FROM entities "
        "WHERE project_id=? ORDER BY entity_type COLLATE BINARY,"
        "entity_id COLLATE BINARY",
        (member["project_id"],),
    ).fetchall()
    entities = tuple(
        dict(zip(ENTITIES_COLUMNS, _row_tuple(row, ENTITIES_COLUMNS)))
        for row in raw_entities
    )
    candidate = BusinessSnapshotSet(
        project=ProjectSnapshotRow(
            **{
                key: project[key]
                for key in PROJECTS_COLUMNS
                if key != "repository_singleton"
            }
        ),
        document=DocumentSnapshotRow(**document),
        entities=tuple(EntitySnapshotRow(**row) for row in entities),
    )
    try:
        verified = verify_business_snapshot(candidate)
    except BusinessSnapshotError as exc:
        raise SQLiteCutoverInstallClosureError(
            "cutover_install_closure_baseline_snapshot_invalid"
        ) from exc
    return verified, entities


def _inner_atomic_seed(
    *,
    commit_id: str,
    committed_at: str,
    atomic_manifest_sha256: bytes,
) -> dict[str, Any]:
    pipeline_intents_sha256 = _component_digest(
        b"ze.atomic-business-commit.pipelines.v1",
        (),
    )
    controls_sha256 = _atomic_digest(
        ATOMIC_BUSINESS_WRITE_CONTROLS_HASH_DOMAIN,
        (),
    )
    manifest_bytes = encode_payload_bounded(
        {
            "schema": ATOMIC_BUSINESS_WRITE_MANIFEST_SCHEMA,
            "protocol": ATOMIC_BUSINESS_WRITE_PROTOCOL,
            "status": ATOMIC_BUSINESS_WRITE_PLAN_STATUS,
            "commit": {
                "commit_id": commit_id,
                "committed_at": committed_at,
                "atomic_manifest_sha256": atomic_manifest_sha256.hex(),
                "pipeline_intents_sha256": pipeline_intents_sha256.hex(),
            },
            "worker_ledger": {
                "before": {"sequence": 0, "tail_sha256": _ZERO.hex()},
                "after": {"sequence": 0, "tail_sha256": _ZERO.hex()},
            },
            "controls": {
                "count": 0,
                "sha256": controls_sha256.hex(),
                "members": [],
            },
        },
        MAX_BUSINESS_SMALL_PAYLOAD_BYTES,
        100_000,
    )
    manifest_sha256 = _atomic_manifest_digest(manifest_bytes)
    transaction_id = BUSINESS_TRANSACTION_PREFIX + commit_id
    parent = AtomicBusinessWriteGenericReceiptEvidence(
        transaction_id,
        BUSINESS_TRANSACTION_KIND,
        committed_at,
    )
    return {
        "commit_id": commit_id,
        "transaction_id": transaction_id,
        "receipt_kind": BUSINESS_TRANSACTION_KIND,
        "committed_at": committed_at,
        "atomic_manifest_sha256": atomic_manifest_sha256,
        "pipeline_intents_sha256": pipeline_intents_sha256,
        "controls_sha256": controls_sha256,
        "control_count": 0,
        "before_worker_sequence": 0,
        "before_worker_tail_sha256": _ZERO,
        "after_worker_sequence": 0,
        "after_worker_tail_sha256": _ZERO,
        "manifest_bytes": manifest_bytes,
        "manifest_sha256": manifest_sha256,
        "receipt_sha256": _atomic_receipt_digest(
            commit_id=commit_id,
            receipt_parent=parent,
            atomic_manifest_sha256=atomic_manifest_sha256,
            pipeline_intents_sha256=pipeline_intents_sha256,
            controls_sha256=controls_sha256,
            control_count=0,
            before_worker_ledger=EMPTY_WORKER_LEDGER_HEAD,
            after_worker_ledger=EMPTY_WORKER_LEDGER_HEAD,
            manifest_sha256=manifest_sha256,
        ),
    }


def _baseline_commit_binding(
    connection: Any,
    *,
    cutover_id: str,
    committed_at: str,
    member: Mapping[str, Any],
    runtime: Mapping[str, Any],
) -> tuple[dict[str, Any], bytes]:
    commit_id = member["baseline_commit_id"]
    transaction_id = BUSINESS_TRANSACTION_PREFIX + commit_id
    manifest_value = {
        "schema": BUSINESS_BASELINE_MANIFEST_SCHEMA,
        "protocol": BUSINESS_BASELINE_MANIFEST_PROTOCOL,
        "status": "committed",
        "authority": {
            "authority": BUSINESS_CUTOVER_AUTHORITY,
            "authority_state": BUSINESS_CUTOVER_INSTALL_AUTHORITY_STATE,
            "epoch": runtime["repository_epoch"],
            "cutover_id": cutover_id,
            "source_admission_revision": runtime["source_admission_revision"],
            "repository_revision": member["baseline_repository_revision"],
            "state_revision": BUSINESS_CUTOVER_INSTALL_STATE_REVISION,
            "runtime_generation": runtime["runtime_generation"],
            "code_digest": runtime["code_digest"].hex(),
        },
        "commit": {
            "commit_id": commit_id,
            "commit_kind": "baseline",
            "project_id": member["project_id"],
            "generation": member["generation"],
            "document_id": member["document_id"],
            "document_version": member["document_version"],
            "save_policy": member["save_policy"],
        },
        "after": {
            "business_root_sha256": member["business_root_sha256"],
            "document_sha256": member["document_snapshot_sha256"],
            "entity_count": member["entity_count"],
            "operation_tail_sha256": member["operation_tail_sha256"],
        },
        "asset_references": {
            "protocol": ASSET_REFERENCE_PROJECTION_PROTOCOL,
            "reference_count": member["asset_reference_count"],
            "unique_reference_count": member["unique_asset_reference_count"],
            "projection_sha256": member["asset_projection_sha256"],
        },
    }
    manifest_bytes = encode_payload_bounded(
        manifest_value,
        MAX_BUSINESS_SMALL_PAYLOAD_BYTES,
        100_000,
    )
    manifest_sha256 = digest_business_baseline_manifest(manifest_bytes)
    if member["baseline_manifest_sha256"] != manifest_sha256.hex():
        _fail("cutover_install_closure_baseline_manifest_invalid")
    expected_commit = {
        "commit_id": commit_id,
        "transaction_id": transaction_id,
        "receipt_kind": BUSINESS_TRANSACTION_KIND,
        "project_id": member["project_id"],
        "generation": member["generation"],
        "repository_revision": member["baseline_repository_revision"],
        "repository_epoch": runtime["repository_epoch"],
        "cutover_id": cutover_id,
        "source_admission_revision": runtime["source_admission_revision"],
        "document_id": member["document_id"],
        "document_version": member["document_version"],
        "previous_commit_id": None,
        "commit_kind": "baseline",
        "save_policy": member["save_policy"],
        "trace_id": (
            BUSINESS_CUTOVER_BASELINE_TRACE_PREFIX
            + cutover_id
            + ":"
            + str(member["ordinal"])
        ),
        "operation_count": 0,
        "operation_target_count": 0,
        "last_operation_id": None,
        "last_operation_ordinal": None,
        "outbox_event_count": 0,
        "last_outbox_event_id": None,
        "last_outbox_ordinal": None,
        "durable_action_count": 0,
        "pipeline_intent_count": 0,
        "before_root_sha256": _ZERO,
        "after_root_sha256": bytes.fromhex(member["business_root_sha256"]),
        "previous_operation_tail_sha256": _ZERO,
        "operation_tail_sha256": bytes.fromhex(member["operation_tail_sha256"]),
        "asset_projection_sha256": bytes.fromhex(member["asset_projection_sha256"]),
        "asset_reference_count": member["asset_reference_count"],
        "entity_count": member["entity_count"],
        "manifest_bytes": manifest_bytes,
        "manifest_sha256": manifest_sha256,
        "committed_at": committed_at,
        "project_revision": 1,
        "head_revision": 1,
    }
    commit = _select_one(
        connection,
        "business_commits",
        BUSINESS_COMMITS_COLUMNS,
        "commit_id=?",
        (commit_id,),
        "cutover_install_closure_baseline_commit_invalid",
    )
    if not _dict_matches(commit, expected_commit):
        _fail("cutover_install_closure_baseline_commit_invalid")
    receipt = _select_one(
        connection,
        "transaction_receipts",
        TRANSACTION_RECEIPTS_COLUMNS,
        "transaction_id=?",
        (transaction_id,),
        "cutover_install_closure_baseline_commit_invalid",
    )
    if not _dict_matches(
        receipt,
        {
            "transaction_id": transaction_id,
            "kind": BUSINESS_TRANSACTION_KIND,
            "committed_at": committed_at,
        },
    ):
        _fail("cutover_install_closure_baseline_commit_invalid")
    return expected_commit, manifest_sha256


def _live_baseline_member(
    connection: Any,
    *,
    cutover_id: str,
    committed_at: str,
    member: Mapping[str, Any],
    runtime: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], int, int]:
    snapshot, entity_rows = _snapshot(connection, member)
    try:
        assets = verify_asset_reference_projection(project_asset_references(snapshot))
    except AssetReferenceProjectionError as exc:
        raise SQLiteCutoverInstallClosureError(
            "cutover_install_closure_baseline_asset_projection_invalid"
        ) from exc
    expected_commit, manifest_sha256 = _baseline_commit_binding(
        connection,
        cutover_id=cutover_id,
        committed_at=committed_at,
        member=member,
        runtime=runtime,
    )
    commit_id = member["baseline_commit_id"]
    if (
        snapshot.project.project_id != member["project_id"]
        or snapshot.project.generation != member["generation"]
        or snapshot.project.snapshot_sha256.hex() != member["business_root_sha256"]
        or snapshot.document.document_id != member["document_id"]
        or snapshot.document.document_version != member["document_version"]
        or snapshot.document.snapshot_sha256.hex() != member["document_snapshot_sha256"]
        or len(snapshot.entities) != member["entity_count"]
        or assets.reference_count != member["asset_reference_count"]
        or assets.unique_reference_count != member["unique_asset_reference_count"]
        or assets.projection_sha256.hex() != member["asset_projection_sha256"]
    ):
        _fail("cutover_install_closure_baseline_snapshot_binding_invalid")
    repository_project = _select_one(
        connection,
        "repository_projects",
        REPOSITORY_PROJECTS_COLUMNS,
        "project_id=?",
        (member["project_id"],),
        "cutover_install_closure_baseline_project_invalid",
    )
    expected_repository_project = {
        "repository_singleton": 1,
        "project_id": member["project_id"],
        "generation": member["generation"],
        "project_revision": 1,
        "status": "live",
        "current_snapshot_sha256": snapshot.project.snapshot_sha256,
        "created_at": committed_at,
        "updated_at": committed_at,
        "deleted_at": None,
    }
    if not _dict_matches(repository_project, expected_repository_project):
        _fail("cutover_install_closure_baseline_project_invalid")
    head = _select_one(
        connection,
        "document_commit_heads",
        DOCUMENT_COMMIT_HEADS_COLUMNS,
        "project_id=?",
        (member["project_id"],),
        "cutover_install_closure_baseline_head_invalid",
    )
    if not _dict_matches(
        head,
        {
            "project_id": member["project_id"],
            "generation": member["generation"],
            "document_id": member["document_id"],
            "document_version": member["document_version"],
            "commit_id": commit_id,
            "head_revision": 1,
            "status": "live",
            "current_snapshot_sha256": snapshot.document.snapshot_sha256,
            "current_project_snapshot_sha256": snapshot.project.snapshot_sha256,
            "business_root_sha256": snapshot.project.snapshot_sha256,
            "operation_tail_sha256": _ZERO,
            "asset_projection_sha256": assets.projection_sha256,
            "asset_reference_count": assets.reference_count,
            "entity_count": len(snapshot.entities),
            "updated_at": committed_at,
        },
    ):
        _fail("cutover_install_closure_baseline_head_invalid")
    raw_refs = connection.execute(
        "SELECT "
        + ",".join(DOCUMENT_ASSET_REFS_COLUMNS)
        + " FROM document_asset_refs WHERE commit_id=? ORDER BY ordinal",
        (commit_id,),
    ).fetchall()
    reference_rows = tuple(
        dict(
            zip(
                DOCUMENT_ASSET_REFS_COLUMNS,
                _row_tuple(row, DOCUMENT_ASSET_REFS_COLUMNS),
            )
        )
        for row in raw_refs
    )
    expected_reference_rows = tuple(
        {
            "project_id": member["project_id"],
            "generation": member["generation"],
            "document_id": member["document_id"],
            "document_version": member["document_version"],
            "commit_id": commit_id,
            "commit_asset_reference_count": assets.reference_count,
            "ordinal": ordinal,
            "asset_sha256": bytes.fromhex(occurrence.reference.sha256),
            "entity_type": occurrence.entity_type,
            "entity_id": occurrence.entity_id,
            "entity_deleted": int(occurrence.entity_deleted),
            "value_path": occurrence.value_path,
            "ref_mime": occurrence.reference.mime,
            "ref_size": occurrence.reference.size,
            "ref_ext": occurrence.reference.ext,
        }
        for ordinal, occurrence in enumerate(assets.occurrences)
    )
    if len(reference_rows) != len(expected_reference_rows) or any(
        not _dict_matches(observed, expected)
        for observed, expected in zip(reference_rows, expected_reference_rows)
    ):
        _fail("cutover_install_closure_baseline_asset_reference_invalid")
    atomic = _inner_atomic_seed(
        commit_id=commit_id,
        committed_at=committed_at,
        atomic_manifest_sha256=manifest_sha256,
    )
    nested = {
        "ordinal": member["ordinal"],
        "project_id": member["project_id"],
        "generation": member["generation"],
        "status": "live",
        "live": {
            "commit_id": commit_id,
            "repository_revision": member["baseline_repository_revision"],
            "project_revision": 1,
            "head_revision": 1,
            "trace_id": expected_commit["trace_id"],
            "before_root_sha256": _ZERO.hex(),
            "operation_tail_sha256": _ZERO.hex(),
            "project": {
                "snapshot_bytes": len(snapshot.project.snapshot_bytes),
                "snapshot_sha256": snapshot.project.snapshot_sha256.hex(),
            },
            "document": {
                "document_id": snapshot.document.document_id,
                "document_version": snapshot.document.document_version,
                "snapshot_bytes": len(snapshot.document.snapshot_bytes),
                "snapshot_sha256": snapshot.document.snapshot_sha256.hex(),
            },
            "entities": [
                {
                    "entity_type": row["entity_type"],
                    "entity_id": row["entity_id"],
                    "entity_version": row["entity_version"],
                    "deleted": row["deleted"],
                    "snapshot_bytes": len(row["snapshot_bytes"]),
                    "snapshot_sha256": row["snapshot_sha256"].hex(),
                }
                for row in entity_rows
            ],
            "asset_projection_sha256": assets.projection_sha256.hex(),
            "asset_reference_count": assets.reference_count,
            "asset_references": [
                {
                    "ordinal": row["ordinal"],
                    "asset_sha256": row["asset_sha256"].hex(),
                    "entity_type": row["entity_type"],
                    "entity_id": row["entity_id"],
                    "entity_deleted": row["entity_deleted"],
                    "value_path": row["value_path"],
                    "ref_mime": row["ref_mime"],
                    "ref_size": row["ref_size"],
                    "ref_ext": row["ref_ext"],
                }
                for row in reference_rows
            ],
            "entity_count": len(snapshot.entities),
            "baseline_manifest_sha256": manifest_sha256.hex(),
            "atomic_manifest_sha256": atomic["manifest_sha256"].hex(),
            "atomic_receipt_sha256": atomic["receipt_sha256"].hex(),
        },
    }
    snapshot_bytes = (
        len(snapshot.project.snapshot_bytes)
        + len(snapshot.document.snapshot_bytes)
        + sum(len(row.snapshot_bytes) for row in snapshot.entities)
    )
    return nested, atomic, snapshot_bytes, assets.reference_count


def _historical_live_baseline_member(
    connection: Any,
    *,
    cutover_id: str,
    committed_at: str,
    member: Mapping[str, Any],
    baseline_member: Any,
    runtime: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], int, int, set[str]]:
    if (
        type(baseline_member) is not dict
        or set(baseline_member)
        != {"ordinal", "project_id", "generation", "status", "live"}
        or baseline_member.get("ordinal") != member["ordinal"]
        or baseline_member.get("project_id") != member["project_id"]
        or baseline_member.get("generation") != member["generation"]
        or baseline_member.get("status") != "live"
        or type(baseline_member.get("live")) is not dict
    ):
        _fail("cutover_install_closure_baseline_member_invalid")
    live = baseline_member["live"]
    project = live.get("project")
    document = live.get("document")
    entities = live.get("entities")
    references = live.get("asset_references")
    if (
        set(live) != _BASELINE_LIVE_KEYS
        or type(project) is not dict
        or set(project) != _BASELINE_PROJECT_KEYS
        or type(document) is not dict
        or set(document) != _BASELINE_DOCUMENT_KEYS
        or type(entities) is not list
        or type(references) is not list
        or live.get("commit_id") != member["baseline_commit_id"]
        or live.get("repository_revision") != member["baseline_repository_revision"]
        or live.get("project_revision") != 1
        or live.get("head_revision") != 1
        or live.get("trace_id")
        != (
            BUSINESS_CUTOVER_BASELINE_TRACE_PREFIX
            + cutover_id
            + ":"
            + str(member["ordinal"])
        )
        or live.get("before_root_sha256") != _ZERO.hex()
        or live.get("operation_tail_sha256") != member["operation_tail_sha256"]
        or type(project.get("snapshot_bytes")) is not int
        or project["snapshot_bytes"] < 1
        or project.get("snapshot_sha256") != member["business_root_sha256"]
        or document.get("document_id") != member["document_id"]
        or document.get("document_version") != member["document_version"]
        or type(document.get("snapshot_bytes")) is not int
        or document["snapshot_bytes"] < 1
        or document.get("snapshot_sha256") != member["document_snapshot_sha256"]
        or len(entities) != member["entity_count"]
        or live.get("entity_count") != member["entity_count"]
        or live.get("asset_reference_count") != member["asset_reference_count"]
        or live.get("asset_projection_sha256") != member["asset_projection_sha256"]
        or live.get("baseline_manifest_sha256") != member["baseline_manifest_sha256"]
    ):
        _fail("cutover_install_closure_baseline_member_invalid")
    previous_entity = None
    entity_bytes = 0
    entity_identities = set()
    for entity in entities:
        if (
            type(entity) is not dict
            or set(entity) != _BASELINE_ENTITY_KEYS
            or type(entity.get("entity_type")) is not str
            or not entity["entity_type"]
            or type(entity.get("entity_id")) is not str
            or not entity["entity_id"]
            or type(entity.get("entity_version")) is not int
            or entity["entity_version"] < 1
            or type(entity.get("deleted")) is not int
            or entity["deleted"] not in {0, 1}
            or type(entity.get("snapshot_bytes")) is not int
            or entity["snapshot_bytes"] < 1
            or _hex(entity.get("snapshot_sha256"), 64) != entity["snapshot_sha256"]
        ):
            _fail("cutover_install_closure_baseline_member_invalid")
        identity = (entity["entity_type"], entity["entity_id"])
        if (
            identity in entity_identities
            or previous_entity is not None
            and identity <= previous_entity
        ):
            _fail("cutover_install_closure_baseline_member_invalid")
        entity_identities.add(identity)
        previous_entity = identity
        entity_bytes += entity["snapshot_bytes"]
    occurrences = []
    reference_values = []
    previous_reference = None
    for ordinal, reference in enumerate(references):
        if (
            type(reference) is not dict
            or set(reference) != _BASELINE_REFERENCE_KEYS
            or reference.get("ordinal") != ordinal
            or _hex(reference.get("asset_sha256"), 64) != reference["asset_sha256"]
            or type(reference.get("entity_type")) is not str
            or type(reference.get("entity_id")) is not str
            or type(reference.get("entity_deleted")) is not int
            or reference["entity_deleted"] not in {0, 1}
            or type(reference.get("value_path")) is not str
            or not reference["value_path"]
            or reference.get("ref_mime") is not None
            and type(reference["ref_mime"]) is not str
            or reference.get("ref_size") is not None
            and (type(reference["ref_size"]) is not int or reference["ref_size"] < 0)
            or reference.get("ref_ext") is not None
            and type(reference["ref_ext"]) is not str
        ):
            _fail("cutover_install_closure_baseline_asset_reference_invalid")
        order = (
            reference["entity_type"],
            reference["entity_id"],
            reference["value_path"],
        )
        if previous_reference is not None and order <= previous_reference:
            _fail("cutover_install_closure_baseline_asset_reference_invalid")
        previous_reference = order
        value = AssetReferenceValue(
            reference["asset_sha256"],
            reference["ref_mime"],
            reference["ref_size"],
            reference["ref_ext"],
        )
        reference_values.append(value)
        occurrences.append(
            AssetReferenceOccurrence(
                reference["entity_type"],
                reference["entity_id"],
                bool(reference["entity_deleted"]),
                reference["value_path"],
                value,
            )
        )
    counts_by_reference: dict[AssetReferenceValue, int] = {}
    for value in reference_values:
        counts_by_reference[value] = counts_by_reference.get(value, 0) + 1
    projection = AssetReferenceProjection(
        protocol=ASSET_REFERENCE_PROJECTION_PROTOCOL,
        project_id=member["project_id"],
        generation=member["generation"],
        document_id=member["document_id"],
        business_root_sha256=bytes.fromhex(member["business_root_sha256"]),
        occurrences=tuple(occurrences),
        multiplicities=tuple(
            AssetReferenceMultiplicity(reference=value, count=count)
            for value, count in sorted(counts_by_reference.items())
        ),
        projection_sha256=bytes.fromhex(member["asset_projection_sha256"]),
    )
    try:
        projection = verify_asset_reference_projection(projection)
    except AssetReferenceProjectionError as exc:
        raise SQLiteCutoverInstallClosureError(
            "cutover_install_closure_baseline_asset_projection_invalid"
        ) from exc
    if (
        projection.reference_count != member["asset_reference_count"]
        or projection.unique_reference_count != member["unique_asset_reference_count"]
    ):
        _fail("cutover_install_closure_baseline_asset_projection_invalid")
    expected_commit, manifest_sha256 = _baseline_commit_binding(
        connection,
        cutover_id=cutover_id,
        committed_at=committed_at,
        member=member,
        runtime=runtime,
    )
    atomic = _inner_atomic_seed(
        commit_id=member["baseline_commit_id"],
        committed_at=committed_at,
        atomic_manifest_sha256=manifest_sha256,
    )
    if (
        live.get("atomic_manifest_sha256") != atomic["manifest_sha256"].hex()
        or live.get("atomic_receipt_sha256") != atomic["receipt_sha256"].hex()
        or expected_commit["trace_id"] != live["trace_id"]
    ):
        _fail("cutover_install_closure_baseline_member_invalid")
    raw_refs = connection.execute(
        "SELECT "
        + ",".join(DOCUMENT_ASSET_REFS_COLUMNS)
        + " FROM document_asset_refs WHERE commit_id=? ORDER BY ordinal",
        (member["baseline_commit_id"],),
    ).fetchall()
    if len(raw_refs) != len(references):
        _fail("cutover_install_closure_baseline_asset_reference_invalid")
    for ordinal, (raw, reference) in enumerate(zip(raw_refs, references)):
        observed = dict(
            zip(
                DOCUMENT_ASSET_REFS_COLUMNS,
                _row_tuple(raw, DOCUMENT_ASSET_REFS_COLUMNS),
            )
        )
        if not _dict_matches(
            observed,
            {
                "project_id": member["project_id"],
                "generation": member["generation"],
                "document_id": member["document_id"],
                "document_version": member["document_version"],
                "commit_id": member["baseline_commit_id"],
                "commit_asset_reference_count": len(references),
                "ordinal": ordinal,
                "asset_sha256": bytes.fromhex(reference["asset_sha256"]),
                "entity_type": reference["entity_type"],
                "entity_id": reference["entity_id"],
                "entity_deleted": reference["entity_deleted"],
                "value_path": reference["value_path"],
                "ref_mime": reference["ref_mime"],
                "ref_size": reference["ref_size"],
                "ref_ext": reference["ref_ext"],
            },
        ):
            _fail("cutover_install_closure_baseline_asset_reference_invalid")
    return (
        baseline_member,
        atomic,
        project["snapshot_bytes"] + document["snapshot_bytes"] + entity_bytes,
        len(references),
        {value.sha256 for value in reference_values},
    )


def _deleted_baseline_member(
    connection: Any,
    *,
    committed_at: str,
    member: Mapping[str, Any],
) -> dict[str, Any]:
    repository_project = _select_one(
        connection,
        "repository_projects",
        REPOSITORY_PROJECTS_COLUMNS,
        "project_id=?",
        (member["project_id"],),
        "cutover_install_closure_baseline_project_invalid",
    )
    if not _dict_matches(
        repository_project,
        {
            "repository_singleton": 1,
            "project_id": member["project_id"],
            "generation": member["generation"],
            "project_revision": 1,
            "status": "deleted",
            "current_snapshot_sha256": None,
            "created_at": committed_at,
            "updated_at": committed_at,
            "deleted_at": committed_at,
        },
    ):
        _fail("cutover_install_closure_baseline_project_invalid")
    return {
        "ordinal": member["ordinal"],
        "project_id": member["project_id"],
        "generation": member["generation"],
        "status": "deleted",
        "deleted": {"project_revision": 1, "deleted_at": committed_at},
    }


def _baseline(
    connection: Any,
    value: Any,
    *,
    cutover_id: str,
    committed_at: str,
    mapping: Mapping[int, Mapping[str, Any]],
    counts: Mapping[str, int],
    runtime: Mapping[str, Any],
    require_current_install_snapshot: bool = False,
) -> tuple[
    dict[int, Mapping[str, Any]],
    str,
    dict[int, Mapping[str, Any]],
]:
    if type(value) is not dict or set(value) != _BASELINE_KEYS:
        _fail("cutover_install_closure_baseline_projection_invalid")
    members_value = value.get("members")
    assets_value = value.get("asset_blobs")
    scheduler = {
        "state_revision": 1,
        "worker_sequence": 0,
        "outbox_allocation_sequence": 0,
        "outbox_dispatch_revision": 1,
        "empty_sha256": _ZERO.hex(),
    }
    head_rows = connection.execute(
        "SELECT cutover_id,phase_revision,phase,authority_state "
        "FROM business_cutover_head WHERE singleton=1"
    ).fetchall()
    if len(head_rows) > 1:
        _fail("cutover_install_closure_cutover_head_invalid")
    mounted = False
    if head_rows:
        head = head_rows[0]
        phases = {
            1: ("installed", "read_only"),
            2: ("assets_covered", "read_only"),
            3: ("audited", "read_only"),
            4: ("activated", "active"),
            5: ("mounted", "active"),
        }
        if (
            head[0] != cutover_id
            or type(head[1]) is not int
            or phases.get(head[1]) != (head[2], head[3])
        ):
            _fail("cutover_install_closure_cutover_head_invalid")
        mounted = head[1] == 5 and head[2] == "mounted" and head[3] == "active"
    if type(require_current_install_snapshot) is not bool:
        raise TypeError("require_current_install_snapshot must be an exact bool")
    install_snapshot_required = require_current_install_snapshot or not mounted
    if (
        value.get("schema") != BUSINESS_CUTOVER_BASELINE_PROJECTION_V12_SCHEMA
        or value.get("protocol") != BUSINESS_CUTOVER_BASELINE_PROJECTION_V12_PROTOCOL
        or value.get("status") != BUSINESS_CUTOVER_BASELINE_PROJECTION_STATUS
        or value.get("cutover_id") != cutover_id
        or value.get("committed_at") != committed_at
        or value.get("runtime_repository_revision")
        != max(1, counts["live_project_count"])
        or value.get("project_count") != counts["project_count"]
        or value.get("live_project_count") != counts["live_project_count"]
        or value.get("deleted_project_count") != counts["deleted_project_count"]
        or value.get("outer_atomic_status")
        != BUSINESS_CUTOVER_BASELINE_OUTER_ATOMIC_STATUS
        or value.get("scheduler_genesis") != scheduler
        or type(members_value) is not list
        or len(members_value) != counts["project_count"]
        or type(assets_value) is not list
        or type(value.get("total_snapshot_bytes")) is not int
        or type(value.get("entity_row_count")) is not int
        or type(value.get("asset_reference_row_count")) is not int
        or type(value.get("unique_asset_blob_count")) is not int
    ):
        _fail("cutover_install_closure_baseline_projection_invalid")
    nested_members = []
    atomic_by_ordinal: dict[int, Mapping[str, Any]] = {}
    total_snapshot_bytes = 0
    entity_row_count = 0
    asset_reference_row_count = 0
    referenced_asset_shas: set[str] = set()
    metadata_by_sha: dict[str, list[Any]] = {}
    for ordinal in range(counts["project_count"]):
        member = mapping.get(ordinal)
        if member is None:
            _fail("cutover_install_closure_baseline_member_invalid")
        if member["status"] == "live":
            (
                nested,
                atomic,
                snapshot_bytes,
                reference_count,
                member_asset_shas,
            ) = _historical_live_baseline_member(
                connection,
                cutover_id=cutover_id,
                committed_at=committed_at,
                member=member,
                baseline_member=members_value[ordinal],
                runtime=runtime,
            )
            if install_snapshot_required:
                current_nested, _current_atomic, _bytes, _refs = _live_baseline_member(
                    connection,
                    cutover_id=cutover_id,
                    committed_at=committed_at,
                    member=member,
                    runtime=runtime,
                )
                if current_nested != nested:
                    _fail("cutover_install_closure_baseline_snapshot_binding_invalid")
            nested_members.append(nested)
            atomic_by_ordinal[ordinal] = atomic
            total_snapshot_bytes += snapshot_bytes
            entity_row_count += member["entity_count"]
            asset_reference_row_count += reference_count
            referenced_asset_shas.update(member_asset_shas)
            for reference in nested["live"]["asset_references"]:
                metadata = metadata_by_sha.setdefault(
                    reference["asset_sha256"],
                    [None, None, None],
                )
                for index, candidate in enumerate(
                    (
                        reference["ref_mime"],
                        reference["ref_size"],
                        reference["ref_ext"],
                    )
                ):
                    if (
                        metadata[index] is not None
                        and candidate is not None
                        and metadata[index] != candidate
                    ):
                        _fail("cutover_install_closure_baseline_asset_invalid")
                    if metadata[index] is None:
                        metadata[index] = candidate
        else:
            nested = members_value[ordinal]
            if (
                type(nested) is not dict
                or set(nested)
                != {"ordinal", "project_id", "generation", "status", "deleted"}
                or nested.get("ordinal") != ordinal
                or nested.get("project_id") != member["project_id"]
                or nested.get("generation") != member["generation"]
                or nested.get("status") != "deleted"
                or nested.get("deleted")
                != {
                    "project_revision": 1,
                    "deleted_at": committed_at,
                }
            ):
                _fail("cutover_install_closure_baseline_member_invalid")
            if (
                install_snapshot_required
                and _deleted_baseline_member(
                    connection,
                    committed_at=committed_at,
                    member=member,
                )
                != nested
            ):
                _fail("cutover_install_closure_baseline_project_invalid")
            nested_members.append(nested)
    asset_members = []
    previous_sha: str | None = None
    for asset in assets_value:
        if (
            type(asset) is not dict
            or set(asset) != _BASELINE_ASSET_KEYS
            or _hex(asset.get("asset_sha256"), 64) != asset["asset_sha256"]
            or previous_sha is not None
            and asset["asset_sha256"] <= previous_sha
            or asset.get("content_key")
            != ("sha256/" + asset["asset_sha256"][:2] + "/" + asset["asset_sha256"])
            or asset.get("media_type") is not None
            and type(asset["media_type"]) is not str
            or asset.get("extension") is not None
            and type(asset["extension"]) is not str
            or type(asset.get("byte_length")) is not int
            or asset["byte_length"] < 0
            or asset.get("status") != "available"
            or type(asset.get("state_revision")) is not int
            or asset["state_revision"] != 1
            or asset.get("created_at") != committed_at
            or asset.get("updated_at") != committed_at
        ):
            _fail("cutover_install_closure_baseline_asset_invalid")
        previous_sha = asset["asset_sha256"]
        expected_metadata = metadata_by_sha.get(asset["asset_sha256"])
        if (
            expected_metadata is None
            or expected_metadata[1] is None
            or expected_metadata[0] is not None
            and asset["media_type"] != expected_metadata[0]
            or asset["byte_length"] != expected_metadata[1]
            or expected_metadata[2] is not None
            and asset["extension"] != expected_metadata[2]
        ):
            _fail("cutover_install_closure_baseline_asset_invalid")
        asset_members.append(asset)
    asset_write = value.get("asset_blob_write")
    asset_coverage = (
        asset_write.get("asset_coverage") if type(asset_write) is dict else None
    )
    if (
        {asset["asset_sha256"] for asset in asset_members} != referenced_asset_shas
        or value.get("total_snapshot_bytes") != total_snapshot_bytes
        or value.get("entity_row_count") != entity_row_count
        or value.get("asset_reference_row_count") != asset_reference_row_count
        or value.get("unique_asset_blob_count") != len(asset_members)
        or type(asset_write) is not dict
        or set(asset_write) != {"mode", "created_at", "count", "asset_coverage"}
        or asset_write["mode"] != "insert_available_v1_from_fresh_coverage"
        or asset_write["created_at"] != committed_at
        or asset_write["count"] != len(asset_members)
        or type(asset_coverage) is not dict
        or set(asset_coverage)
        != {
            "transaction_id",
            "receipt_kind",
            "manifest_sha256",
            "coverage_sha256",
            "project_root_sha256",
            "occurrence_root_sha256",
            "blob_root_sha256",
        }
        or type(asset_coverage["transaction_id"]) is not str
        or not asset_coverage["transaction_id"]
        or type(asset_coverage["receipt_kind"]) is not str
        or not asset_coverage["receipt_kind"]
        or any(
            _hex(asset_coverage[field], 64) != asset_coverage[field]
            for field in (
                "manifest_sha256",
                "coverage_sha256",
                "project_root_sha256",
                "occurrence_root_sha256",
                "blob_root_sha256",
            )
        )
    ):
        _fail("cutover_install_closure_baseline_projection_invalid")
    try:
        source_bytes = _reconstruct_baseline_projection_v1_from_v2(value)
    except Exception as exc:
        raise SQLiteCutoverInstallClosureError(
            "cutover_install_closure_baseline_projection_invalid"
        ) from exc
    source_sha256 = _projection_digest(source_bytes).hex()
    if value["source_projection_v1"]["sha256"] != source_sha256:
        _fail("cutover_install_closure_baseline_projection_invalid")
    invariant_counts = {
        "business_cutover_baseline_atomic_receipts": counts["live_project_count"],
    }
    if install_snapshot_required:
        invariant_counts.update(
            {
                "repository_projects": counts["project_count"],
                "projects": counts["live_project_count"],
                "documents": counts["live_project_count"],
                "business_commits": counts["live_project_count"],
                "document_commit_heads": counts["live_project_count"],
                "entities": entity_row_count,
                "document_asset_refs": asset_reference_row_count,
                "asset_blobs": len(asset_members),
                "atomic_business_write_receipts": counts["live_project_count"],
            }
        )
    for table, expected_count in invariant_counts.items():
        observed = connection.execute("SELECT count(*) FROM " + table).fetchone()
        if observed is None or observed[0] != expected_count:
            _fail("cutover_install_closure_baseline_database_coverage_invalid")
    if install_snapshot_required:
        business_receipts = connection.execute(
            "SELECT count(*) FROM transaction_receipts "
            "WHERE substr(transaction_id,1,?)=?",
            (len(BUSINESS_TRANSACTION_PREFIX), BUSINESS_TRANSACTION_PREFIX),
        ).fetchone()
        if (
            business_receipts is None
            or business_receipts[0] != counts["live_project_count"]
        ):
            _fail("cutover_install_closure_baseline_database_coverage_invalid")
        raw_assets = connection.execute(
            "SELECT "
            + ",".join(ASSET_BLOBS_COLUMNS)
            + " FROM asset_blobs ORDER BY asset_sha256",
        ).fetchall()
        current_assets = [
            {
                "asset_sha256": row[0].hex(),
                "content_key": row[1],
                "media_type": row[2],
                "extension": row[3],
                "byte_length": row[4],
                "status": row[5],
                "state_revision": row[6],
                "created_at": row[7],
                "updated_at": row[8],
            }
            for row in raw_assets
        ]
        if current_assets != asset_members:
            _fail("cutover_install_closure_baseline_asset_invalid")
        expected_zero_head = (1, 1, 0, _ZERO, _ZERO, committed_at)
        for table in ("worker_ledger_head", "outbox_allocation_head"):
            row = connection.execute(
                "SELECT "
                + ",".join(WORKER_LEDGER_HEAD_COLUMNS)
                + " FROM "
                + table
                + " WHERE singleton=1"
            ).fetchone()
            if row is None or tuple(row) != expected_zero_head:
                _fail("cutover_install_closure_baseline_scheduler_invalid")
        dispatch = connection.execute(
            "SELECT "
            + ",".join(OUTBOX_DISPATCH_HEAD_COLUMNS)
            + " FROM outbox_dispatch_head WHERE singleton=1"
        ).fetchone()
        if dispatch is None or tuple(dispatch) != (
            1,
            1,
            0,
            None,
            None,
            None,
            None,
            None,
            _ZERO,
            _ZERO,
            committed_at,
        ):
            _fail("cutover_install_closure_baseline_scheduler_invalid")
    by_ordinal = {member["ordinal"]: member for member in nested_members}
    return by_ordinal, source_sha256, atomic_by_ordinal


def _sidecars(
    connection: Any,
    *,
    cutover_id: str,
    install_transaction_id: str,
    install_manifest_sha256: bytes,
    committed_at: str,
    member_mapping_sha256: bytes,
    baseline_projection_sha256: bytes,
    baseline_members: Mapping[int, Mapping[str, Any]],
    source_projection_sha256: str,
    atomic_by_ordinal: Mapping[int, Mapping[str, Any]],
) -> tuple[
    Tuple[BusinessCutoverBaselineAtomicReceiptRow, ...], tuple[Mapping[str, Any], ...]
]:
    rows = connection.execute(
        "SELECT "
        + ",".join(BASELINE_ATOMIC_RECEIPT_COLUMNS)
        + " FROM business_cutover_baseline_atomic_receipts "
        "WHERE cutover_id=? ORDER BY repository_revision",
        (cutover_id,),
    ).fetchall()
    canonical_targets = _empty_targets_sha256()
    sidecars = []
    manifest_members = []
    for expected_revision, raw in enumerate(rows, 1):
        values = _row_tuple(raw, BASELINE_ATOMIC_RECEIPT_COLUMNS)
        row = BusinessCutoverBaselineAtomicReceiptRow(*values)
        baseline_member = baseline_members.get(row.member_ordinal)
        live = None if baseline_member is None else baseline_member.get("live")
        expected_atomic = atomic_by_ordinal.get(row.member_ordinal)
        if (
            row.cutover_id != cutover_id
            or row.repository_revision != expected_revision
            or type(live) is not dict
            or expected_atomic is None
            or live.get("repository_revision") != expected_revision
            or live.get("commit_id") != row.commit_id
            or row.baseline_outer_schema != BUSINESS_CUTOVER_BASELINE_OUTER_SCHEMA
            or row.baseline_outer_protocol != BUSINESS_CUTOVER_BASELINE_OUTER_PROTOCOL
            or row.member_mapping_kind != BUSINESS_CUTOVER_MEMBER_MAPPING_ARTIFACT
            or row.baseline_projection_kind
            != BUSINESS_CUTOVER_BASELINE_PROJECTION_ARTIFACT
            or row.install_transaction_id != install_transaction_id
            or not hmac.compare_digest(
                row.install_manifest_sha256, install_manifest_sha256
            )
            or row.committed_at != committed_at
            or not hmac.compare_digest(row.member_mapping_sha256, member_mapping_sha256)
            or not hmac.compare_digest(
                row.baseline_projection_sha256, baseline_projection_sha256
            )
            or row.target_count != 0
            or not hmac.compare_digest(row.targets_sha256, canonical_targets)
            or any(
                not _same(getattr(row, key), expected_atomic[key])
                for key in (
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
                )
            )
        ):
            _fail("cutover_install_closure_baseline_sidecar_invalid")
        manifest = _canonical_payload(
            row.baseline_outer_manifest_bytes,
            MAX_BUSINESS_SMALL_PAYLOAD_BYTES,
            100_000,
        )
        expected_manifest = {
            "schema": BUSINESS_CUTOVER_BASELINE_OUTER_SCHEMA,
            "protocol": BUSINESS_CUTOVER_BASELINE_OUTER_PROTOCOL,
            "status": BUSINESS_CUTOVER_BASELINE_OUTER_ATOMIC_STATUS,
            "cutover_id": cutover_id,
            "member": {
                "ordinal": row.member_ordinal,
                "repository_revision": row.repository_revision,
                "project_id": baseline_member["project_id"],
                "generation": baseline_member["generation"],
            },
            "transaction": {
                "commit_id": row.commit_id,
                "transaction_id": row.transaction_id,
                "receipt_kind": row.receipt_kind,
                "committed_at": row.committed_at,
            },
            "install": {
                "transaction_id": install_transaction_id,
                "manifest_sha256": install_manifest_sha256.hex(),
            },
            "artifacts": {
                "member_mapping_sha256": member_mapping_sha256.hex(),
                "baseline_projection_sha256": baseline_projection_sha256.hex(),
                "source_projection_v1_sha256": source_projection_sha256,
            },
            "pure_atomic": {
                "atomic_manifest_sha256": expected_atomic[
                    "atomic_manifest_sha256"
                ].hex(),
                "manifest_sha256": expected_atomic["manifest_sha256"].hex(),
                "receipt_sha256": expected_atomic["receipt_sha256"].hex(),
            },
            "targets": {"count": 0, "sha256": canonical_targets.hex()},
        }
        expected_manifest_sha256 = _digest(
            BUSINESS_CUTOVER_BASELINE_OUTER_MANIFEST_HASH_DOMAIN,
            row.baseline_outer_manifest_bytes,
        )
        expected_receipt_sha256 = _digest(
            BUSINESS_CUTOVER_BASELINE_OUTER_RECEIPT_HASH_DOMAIN,
            _text(row.commit_id),
            _text(row.transaction_id),
            _text(row.receipt_kind),
            _text(row.committed_at),
            expected_atomic["receipt_sha256"],
            expected_manifest_sha256,
            (0).to_bytes(8, "big"),
            canonical_targets,
        )
        if (
            manifest != expected_manifest
            or not hmac.compare_digest(
                row.baseline_outer_manifest_sha256,
                expected_manifest_sha256,
            )
            or not hmac.compare_digest(
                row.baseline_outer_receipt_sha256,
                expected_receipt_sha256,
            )
        ):
            _fail("cutover_install_closure_baseline_sidecar_hash_mismatch")
        atomic = connection.execute(
            "SELECT commit_id,transaction_id,receipt_kind,committed_at,"
            "atomic_manifest_sha256,pipeline_intents_sha256,controls_sha256,"
            "control_count,before_worker_sequence,before_worker_tail_sha256,"
            "after_worker_sequence,after_worker_tail_sha256,manifest_bytes,"
            "manifest_sha256,receipt_sha256,production_manifest_bytes,"
            "production_manifest_sha256,target_count,targets_sha256,"
            "production_receipt_sha256 FROM atomic_business_write_receipts "
            "WHERE commit_id=?",
            (row.commit_id,),
        ).fetchone()
        expected_atomic_row = (
            expected_atomic["commit_id"],
            expected_atomic["transaction_id"],
            expected_atomic["receipt_kind"],
            expected_atomic["committed_at"],
            expected_atomic["atomic_manifest_sha256"],
            expected_atomic["pipeline_intents_sha256"],
            expected_atomic["controls_sha256"],
            expected_atomic["control_count"],
            expected_atomic["before_worker_sequence"],
            expected_atomic["before_worker_tail_sha256"],
            expected_atomic["after_worker_sequence"],
            expected_atomic["after_worker_tail_sha256"],
            expected_atomic["manifest_bytes"],
            expected_atomic["manifest_sha256"],
            expected_atomic["receipt_sha256"],
            row.baseline_outer_manifest_bytes,
            row.baseline_outer_manifest_sha256,
            row.target_count,
            row.targets_sha256,
            row.baseline_outer_receipt_sha256,
        )
        if atomic is None or any(
            not _same(atomic[index], expected)
            for index, expected in enumerate(expected_atomic_row)
        ):
            _fail("cutover_install_closure_atomic_mirror_invalid")
        sidecars.append(row)
        manifest_members.append(
            {
                "member_ordinal": row.member_ordinal,
                "repository_revision": row.repository_revision,
                "commit_id": row.commit_id,
                "receipt_sha256": row.baseline_outer_receipt_sha256.hex(),
            }
        )
    return tuple(sidecars), tuple(manifest_members)


def _read_business_cutover_install_closure(
    connection: Any,
    *,
    cutover_id: str,
    install_transaction_id: str,
    install_manifest_sha256: bytes,
    install_committed_at: str,
    target_schema_version: int,
    target_schema_fingerprint: str,
    require_current_install_snapshot: bool = False,
) -> SQLiteCutoverInstallClosureBinding:
    """Verify and stamp one stored V12 install closure in the current snapshot."""

    try:
        _hex(cutover_id, 64)
        if type(require_current_install_snapshot) is not bool:
            raise TypeError(
                "require_current_install_snapshot must be an exact bool"
            )
        _digest_value(install_manifest_sha256)
        _hex(target_schema_fingerprint, 64)
        if type(target_schema_version) is not int or target_schema_version < 12:
            _fail("cutover_install_closure_schema_target_invalid")
        mapping_header, _mapping_bytes, mapping_value = _artifact(
            connection,
            cutover_id=cutover_id,
            artifact_kind=BUSINESS_CUTOVER_MEMBER_MAPPING_ARTIFACT,
        )
        baseline_header, _baseline_bytes, baseline_value = _artifact(
            connection,
            cutover_id=cutover_id,
            artifact_kind=BUSINESS_CUTOVER_BASELINE_PROJECTION_ARTIFACT,
        )
        if (
            mapping_header["artifact_schema"] != BUSINESS_CUTOVER_MEMBER_MAPPING_SCHEMA
            or baseline_header["artifact_schema"]
            != BUSINESS_CUTOVER_BASELINE_PROJECTION_V12_SCHEMA
        ):
            _fail("cutover_install_closure_artifact_schema_invalid")
        current_runtime = _runtime(connection, cutover_id)
        install_columns = (
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
        install = _select_one(
            connection,
            "business_cutover_install_receipts",
            install_columns,
            "cutover_id=?",
            (cutover_id,),
            "cutover_install_closure_install_receipt_invalid",
        )
        if (
            install["cutover_id"] != cutover_id
            or install["transaction_id"] != install_transaction_id
            or install["receipt_kind"] != BUSINESS_CUTOVER_INSTALL_TRANSACTION_KIND
            or install["committed_at"] != install_committed_at
            or type(install["manifest_bytes"]) is not bytes
            or not hmac.compare_digest(
                _digest_value(install["member_mapping_sha256"]),
                mapping_header["artifact_sha256"],
            )
            or not hmac.compare_digest(
                _digest_value(install["manifest_sha256"]),
                install_manifest_sha256,
            )
            or not hmac.compare_digest(
                _domain_digest(
                    BUSINESS_CUTOVER_INSTALL_MANIFEST_HASH_DOMAIN,
                    install["manifest_bytes"],
                ),
                install_manifest_sha256,
            )
        ):
            _fail("cutover_install_closure_install_receipt_invalid")
        members, counts = _mapping(mapping_value, cutover_id)
        install_runtime, install_mapping = _install_runtime(
            install,
            current_runtime,
            cutover_id=cutover_id,
            install_transaction_id=install_transaction_id,
            install_manifest_sha256=install_manifest_sha256,
            install_committed_at=install_committed_at,
            member_mapping_sha256=mapping_header["artifact_sha256"],
            target_schema_version=target_schema_version,
            target_schema_fingerprint=target_schema_fingerprint,
        )
        if any(
            install_mapping[key] != expected
            for key, expected in {
                "project_count": counts["project_count"],
                "live_project_count": counts["live_project_count"],
                "deleted_project_count": counts["deleted_project_count"],
                "baseline_commit_count": counts["live_project_count"],
            }.items()
        ):
            _fail("cutover_install_closure_install_receipt_invalid")
        (
            baseline_members,
            source_projection_sha256,
            atomic_by_ordinal,
        ) = _baseline(
            connection,
            baseline_value,
            cutover_id=cutover_id,
            committed_at=install_committed_at,
            mapping=members,
            counts=counts,
            runtime=install_runtime,
            require_current_install_snapshot=require_current_install_snapshot,
        )
        sidecars, receipt_members = _sidecars(
            connection,
            cutover_id=cutover_id,
            install_transaction_id=install_transaction_id,
            install_manifest_sha256=install_manifest_sha256,
            committed_at=install_committed_at,
            member_mapping_sha256=mapping_header["artifact_sha256"],
            baseline_projection_sha256=baseline_header["artifact_sha256"],
            baseline_members=baseline_members,
            source_projection_sha256=source_projection_sha256,
            atomic_by_ordinal=atomic_by_ordinal,
        )
        live_ordinals = tuple(
            ordinal for ordinal, member in members.items() if member["status"] == "live"
        )
        if tuple(row.member_ordinal for row in sidecars) != live_ordinals:
            _fail("cutover_install_closure_baseline_coverage_invalid")
        receipt_root = _baseline_receipt_root(sidecars)
        raw_closure = connection.execute(
            "SELECT "
            + ",".join(INSTALL_CLOSURE_COLUMNS)
            + " FROM business_cutover_install_closures WHERE cutover_id=?",
            (cutover_id,),
        ).fetchone()
        values = _row_tuple(raw_closure, INSTALL_CLOSURE_COLUMNS)
        closure = dict(zip(INSTALL_CLOSURE_COLUMNS, values))
        try:
            provenance = _require_cutover_install_provenance_binding(
                _read_cutover_install_provenance(
                    connection,
                    cutover_id=cutover_id,
                    mapping=members,
                    baseline=baseline_value,
                    member_mapping_sha256=mapping_header["artifact_sha256"],
                    install_transaction_id=install_transaction_id,
                    install_manifest_sha256=install_manifest_sha256,
                    install_committed_at=install_committed_at,
                    target_schema_version=target_schema_version,
                    target_schema_fingerprint=target_schema_fingerprint,
                    closure=closure,
                )
            )
        except SQLiteCutoverInstallProvenanceError as exc:
            raise SQLiteCutoverInstallClosureError(
                "cutover_install_closure_provenance_invalid"
            ) from exc
        expected_manifest = {
            "schema": BUSINESS_CUTOVER_INSTALL_CLOSURE_SCHEMA,
            "protocol": BUSINESS_CUTOVER_INSTALL_CLOSURE_PROTOCOL,
            "status": BUSINESS_CUTOVER_INSTALL_CLOSURE_STATUS,
            "cutover_id": cutover_id,
            "install": {
                "transaction_id": install_transaction_id,
                "manifest_sha256": install_manifest_sha256.hex(),
                "committed_at": install_committed_at,
            },
            "schema_target": {
                "version": target_schema_version,
                "fingerprint": target_schema_fingerprint,
            },
            "source_proof": {
                "transaction_id": provenance.source_transaction_id,
                "receipt_kind": provenance.source_receipt_kind,
                "catalog_id": provenance.catalog_id,
                "catalog_seal_sha256": (provenance.catalog_seal_sha256.hex()),
                "repository_epoch": provenance.repository_epoch,
                "source_admission_revision": (provenance.source_admission_revision),
                "manifest_sha256": (provenance.source_proof_manifest_sha256.hex()),
                "proof_sha256": provenance.source_proof_sha256.hex(),
                "source_admission_sha256": (provenance.source_admission_sha256.hex()),
                "source_repository_sha256": (provenance.source_repository_sha256.hex()),
                "catalog_coverage_sha256": (provenance.catalog_coverage_sha256.hex()),
                "schema_fingerprint": provenance.schema_fingerprint,
                "member_root_sha256": provenance.member_root_sha256.hex(),
                "project_count": provenance.project_count,
                "live_project_count": provenance.live_project_count,
                "deleted_project_count": provenance.deleted_project_count,
            },
            "asset_coverage": {
                "transaction_id": provenance.coverage_transaction_id,
                "receipt_kind": provenance.coverage_receipt_kind,
                "repository_epoch": provenance.repository_epoch,
                "source_admission_revision": (provenance.source_admission_revision),
                "source_admission_sha256": (provenance.source_admission_sha256.hex()),
                "manifest_sha256": provenance.coverage_manifest_sha256.hex(),
                "coverage_sha256": provenance.coverage_sha256.hex(),
                "member_root_sha256": provenance.member_root_sha256.hex(),
                "batch_count": provenance.batch_count,
                "project_count": provenance.live_project_count,
                "reference_count": provenance.reference_count,
                "unique_blob_count": provenance.unique_blob_count,
                "unique_blob_bytes": provenance.unique_blob_bytes,
                "referenced_bytes": provenance.referenced_bytes,
                "project_root_sha256": provenance.project_root_sha256.hex(),
                "occurrence_root_sha256": (provenance.occurrence_root_sha256.hex()),
                "blob_root_sha256": provenance.blob_root_sha256.hex(),
            },
            "artifacts": [
                {
                    "kind": mapping_header["artifact_kind"],
                    "schema": mapping_header["artifact_schema"],
                    "byte_length": mapping_header["byte_length"],
                    "chunk_count": mapping_header["chunk_count"],
                    "sha256": mapping_header["artifact_sha256"].hex(),
                },
                {
                    "kind": baseline_header["artifact_kind"],
                    "schema": baseline_header["artifact_schema"],
                    "byte_length": baseline_header["byte_length"],
                    "chunk_count": baseline_header["chunk_count"],
                    "sha256": baseline_header["artifact_sha256"].hex(),
                },
            ],
            "projects": {
                "count": counts["project_count"],
                "live_count": counts["live_project_count"],
                "deleted_count": counts["deleted_project_count"],
            },
            "baseline_receipts": {
                "count": len(sidecars),
                "root_sha256": receipt_root.hex(),
                "members": list(receipt_members),
            },
        }
        closure_manifest = _canonical_payload(
            closure["manifest_bytes"],
            MAX_BUSINESS_SMALL_PAYLOAD_BYTES,
            100_000,
        )
        closure_sha256 = _closure_manifest_digest(closure["manifest_bytes"])
        if (
            closure_manifest != expected_manifest
            or closure["cutover_id"] != cutover_id
            or closure["install_transaction_id"] != install_transaction_id
            or not hmac.compare_digest(
                closure["install_manifest_sha256"], install_manifest_sha256
            )
            or any(
                not _same(closure[field], getattr(provenance, field))
                for field in (
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
                )
            )
            or closure["committed_at"] != install_committed_at
            or closure["member_mapping_kind"]
            != BUSINESS_CUTOVER_MEMBER_MAPPING_ARTIFACT
            or not hmac.compare_digest(
                closure["member_mapping_sha256"],
                mapping_header["artifact_sha256"],
            )
            or closure["baseline_projection_kind"]
            != BUSINESS_CUTOVER_BASELINE_PROJECTION_ARTIFACT
            or not hmac.compare_digest(
                closure["baseline_projection_sha256"],
                baseline_header["artifact_sha256"],
            )
            or closure["project_count"] != counts["project_count"]
            or closure["live_project_count"] != counts["live_project_count"]
            or closure["deleted_project_count"] != counts["deleted_project_count"]
            or closure["baseline_commit_count"] != len(sidecars)
            or not hmac.compare_digest(
                closure["baseline_outer_receipt_root_sha256"], receipt_root
            )
            or not hmac.compare_digest(closure["manifest_sha256"], closure_sha256)
        ):
            _fail("cutover_install_closure_manifest_mismatch")
        return SQLiteCutoverInstallClosureBinding(
            cutover_id=cutover_id,
            install_transaction_id=install_transaction_id,
            install_manifest_sha256=install_manifest_sha256,
            member_mapping_sha256=mapping_header["artifact_sha256"],
            baseline_projection_sha256=baseline_header["artifact_sha256"],
            baseline_receipt_root_sha256=receipt_root,
            closure_manifest_sha256=closure_sha256,
            source_proof_sha256=provenance.source_proof_sha256,
            source_admission_sha256=provenance.source_admission_sha256,
            coverage_manifest_sha256=provenance.coverage_manifest_sha256,
            coverage_sha256=provenance.coverage_sha256,
            project_count=counts["project_count"],
            live_project_count=counts["live_project_count"],
            deleted_project_count=counts["deleted_project_count"],
            baseline_commit_count=len(sidecars),
            _issuer=_READER_ISSUER,
        )
    except SQLiteCutoverInstallClosureError:
        raise
    except Exception as exc:
        raise SQLiteCutoverInstallClosureError(
            "cutover_install_closure_unavailable"
        ) from exc


__all__ = ()
