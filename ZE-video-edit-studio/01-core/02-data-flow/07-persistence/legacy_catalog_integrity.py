"""Canonical integrity projection for one historical legacy-catalog seal.

The caller must already hold the repository-wide legacy mutation barrier and
must pass descriptors projected from one verified SQLite import plan.  This
leaf only validates exact scalar shapes, computes domain-separated digests and
encodes the canonical manifest.  It performs no filesystem, SDK or SQLite I/O,
does not issue a receipt, and cannot claim currentness or authority cutover.
"""

from __future__ import annotations

import hashlib
from typing import Any, NamedTuple, Optional, Tuple

from business_codec import encode_payload_bounded
from hash_framing import update_hash_frame
from legacy_catalog_contract import (
    LEGACY_CATALOG_CUTOVER_READY,
    LEGACY_CATALOG_MANIFEST_SCHEMA,
    LEGACY_CATALOG_RECEIPT_PROTOCOL,
    LEGACY_CATALOG_SEAL_PROTOCOL,
    LEGACY_CATALOG_SELECTION,
    LEGACY_CATALOG_STATUS,
    LEGACY_CATALOG_TARGET_SCHEMA_VERSION,
    LEGACY_CATALOG_TRANSACTION_KIND,
    LEGACY_CATALOG_TRANSACTION_PREFIX,
    MAX_LEGACY_CATALOG_MANIFEST_BYTES,
    MAX_LEGACY_CATALOG_PROJECT_SOURCE_HEAD_BYTES,
    MAX_LEGACY_CATALOG_PROJECTS,
    MAX_LEGACY_CATALOG_SOURCE_HEAD_BYTES,
    MAX_LEGACY_CATALOG_TARGET_SNAPSHOT_BYTES,
)
from legacy_import_contract import MAX_LEGACY_ENTITIES, MAX_LEGACY_MANIFEST_NODES


LEGACY_CATALOG_SOURCE_HASH_DOMAIN = b"ze.legacy-catalog.source.v1"
LEGACY_CATALOG_TARGET_HASH_DOMAIN = b"ze.legacy-catalog.target.v1"
LEGACY_CATALOG_IMPORT_ID_HASH_DOMAIN = b"ze.legacy-catalog.import-id.v1"
LEGACY_CATALOG_ID_HASH_DOMAIN = b"ze.legacy-catalog.id.v1"
LEGACY_CATALOG_MANIFEST_HASH_DOMAIN = LEGACY_CATALOG_MANIFEST_SCHEMA.encode(
    "ascii"
)
LEGACY_CATALOG_SEAL_HASH_DOMAIN = LEGACY_CATALOG_SEAL_PROTOCOL.encode("ascii")

_LOWER_HEX = frozenset("0123456789abcdef")
_MAX_SQLITE_INTEGER = 2**63 - 1
_STORAGE_VERSIONS = frozenset(("legacy", "v2"))


class LegacyCatalogIntegrityError(ValueError):
    """Stable, payload-free validation failure at the pure seal boundary."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        if type(code) is not str or not code:
            raise TypeError("code must be a non-empty exact string")
        self.code = code
        super().__init__(code)


class LegacyCatalogIntegrityProject(NamedTuple):
    """One exact live descriptor; ordinal is its canonical tuple position."""

    project_id: str
    generation: int
    document_id: str
    document_version: int
    storage_version: str
    source_head_sha256: bytes
    source_head_bytes: int
    business_root_sha256: bytes
    entity_count: int
    row_snapshot_bytes: int


class LegacyCatalogIntegrity(NamedTuple):
    """Complete content-addressed projection written by the catalog sealer."""

    catalog_id: str
    transaction_id: str
    import_id: Optional[str]
    project_count: int
    entity_count: int
    source_head_bytes: int
    row_snapshot_bytes: int
    manifest_bytes: bytes
    manifest_sha256: bytes
    source_catalog_sha256: bytes
    target_coverage_sha256: bytes
    seal_sha256: bytes


def _fail(code: str) -> None:
    raise LegacyCatalogIntegrityError(code)


def _exact_lower_hex(value: Any, length: int) -> bool:
    return (
        type(value) is str
        and len(value) == length
        and all(character in _LOWER_HEX for character in value)
    )


def _utf8(value: str) -> bytes:
    try:
        return value.encode("utf-8", "strict")
    except UnicodeEncodeError:
        _fail("project_invalid")
    raise AssertionError("unreachable")


def _frame(hasher: Any, value: bytes) -> None:
    update_hash_frame(hasher, value)


def _frame_integer(hasher: Any, value: int) -> None:
    _frame(hasher, value.to_bytes(8, "big"))


def _digest(domain: bytes, *parts: bytes) -> bytes:
    hasher = hashlib.sha256()
    _frame(hasher, domain)
    for part in parts:
        _frame(hasher, part)
    return hasher.digest()


def _validate_project(value: Any) -> LegacyCatalogIntegrityProject:
    if type(value) is not LegacyCatalogIntegrityProject:
        _fail("project_invalid")
    if (
        type(value.project_id) is not str
        or not 1 <= len(value.project_id) <= 160
        or type(value.generation) is not int
        or not 1 <= value.generation <= _MAX_SQLITE_INTEGER
        or type(value.document_id) is not str
        or not value.document_id
        or type(value.document_version) is not int
        or not 1 <= value.document_version <= _MAX_SQLITE_INTEGER
        or type(value.storage_version) is not str
        or value.storage_version not in _STORAGE_VERSIONS
        or type(value.source_head_sha256) is not bytes
        or len(value.source_head_sha256) != 32
        or type(value.source_head_bytes) is not int
        or not 1
        <= value.source_head_bytes
        <= MAX_LEGACY_CATALOG_PROJECT_SOURCE_HEAD_BYTES
        or type(value.business_root_sha256) is not bytes
        or len(value.business_root_sha256) != 32
        or type(value.entity_count) is not int
        or not 0 <= value.entity_count <= MAX_LEGACY_ENTITIES
        or type(value.row_snapshot_bytes) is not int
        or not 1
        <= value.row_snapshot_bytes
        <= MAX_LEGACY_CATALOG_TARGET_SNAPSHOT_BYTES
    ):
        _fail("project_invalid")
    _utf8(value.project_id)
    _utf8(value.document_id)
    return value


def _source_digest(
    projects: Tuple[LegacyCatalogIntegrityProject, ...],
) -> bytes:
    hasher = hashlib.sha256()
    _frame(hasher, LEGACY_CATALOG_SOURCE_HASH_DOMAIN)
    _frame_integer(hasher, len(projects))
    for ordinal, project in enumerate(projects):
        _frame_integer(hasher, ordinal)
        _frame(hasher, _utf8(project.project_id))
        _frame_integer(hasher, project.generation)
        _frame(hasher, _utf8(project.document_id))
        _frame_integer(hasher, project.document_version)
        _frame(hasher, project.storage_version.encode("ascii"))
        _frame(hasher, project.source_head_sha256)
        _frame_integer(hasher, project.source_head_bytes)
        _frame(hasher, project.business_root_sha256)
        _frame_integer(hasher, project.entity_count)
        _frame_integer(hasher, project.row_snapshot_bytes)
    return hasher.digest()


def _target_digest(
    import_id: Optional[str],
    projects: Tuple[LegacyCatalogIntegrityProject, ...],
    *,
    schema_fingerprint: str,
) -> bytes:
    hasher = hashlib.sha256()
    _frame(hasher, LEGACY_CATALOG_TARGET_HASH_DOMAIN)
    _frame(
        hasher,
        b"empty" if import_id is None else import_id.encode("ascii"),
    )
    _frame_integer(hasher, LEGACY_CATALOG_TARGET_SCHEMA_VERSION)
    _frame(hasher, schema_fingerprint.encode("ascii"))
    _frame_integer(hasher, len(projects))
    for ordinal, project in enumerate(projects):
        _frame_integer(hasher, ordinal)
        _frame(hasher, _utf8(project.project_id))
        _frame_integer(hasher, project.generation)
        _frame(hasher, _utf8(project.document_id))
        _frame_integer(hasher, project.document_version)
        _frame(hasher, project.storage_version.encode("ascii"))
        _frame(hasher, project.source_head_sha256)
        _frame_integer(hasher, project.source_head_bytes)
        _frame(hasher, project.business_root_sha256)
        _frame_integer(hasher, project.entity_count)
        _frame_integer(hasher, project.row_snapshot_bytes)
    return hasher.digest()


def _manifest_value(
    *,
    catalog_id: str,
    import_id: Optional[str],
    projects: Tuple[LegacyCatalogIntegrityProject, ...],
    entity_count: int,
    source_head_bytes: int,
    row_snapshot_bytes: int,
    source_catalog_sha256: bytes,
    target_coverage_sha256: bytes,
    schema_fingerprint: str,
) -> dict[str, Any]:
    return {
        "schema": LEGACY_CATALOG_MANIFEST_SCHEMA,
        "protocol": LEGACY_CATALOG_SEAL_PROTOCOL,
        "receipt_protocol": LEGACY_CATALOG_RECEIPT_PROTOCOL,
        "receipt_kind": LEGACY_CATALOG_TRANSACTION_KIND,
        "catalog_id": catalog_id,
        "import_id": import_id,
        "status": LEGACY_CATALOG_STATUS,
        "selection": LEGACY_CATALOG_SELECTION,
        "cutover_ready": LEGACY_CATALOG_CUTOVER_READY,
        "target_schema_version": LEGACY_CATALOG_TARGET_SCHEMA_VERSION,
        "schema_fingerprint": schema_fingerprint,
        "project_count": len(projects),
        "entity_count": entity_count,
        "source_head_bytes": source_head_bytes,
        "row_snapshot_bytes": row_snapshot_bytes,
        "source_catalog_sha256": source_catalog_sha256.hex(),
        "target_coverage_sha256": target_coverage_sha256.hex(),
        "projects": [
            {
                "ordinal": ordinal,
                "project_id": project.project_id,
                "generation": project.generation,
                "document_id": project.document_id,
                "document_version": project.document_version,
                "storage_version": project.storage_version,
                "source_head_sha256": project.source_head_sha256.hex(),
                "source_head_bytes": project.source_head_bytes,
                "business_root_sha256": project.business_root_sha256.hex(),
                "entity_count": project.entity_count,
                "row_snapshot_bytes": project.row_snapshot_bytes,
            }
            for ordinal, project in enumerate(projects)
        ],
    }


def _verified_projects_and_aggregates(
    projects: Any,
    *,
    allow_empty: bool,
) -> tuple[
    Tuple[LegacyCatalogIntegrityProject, ...],
    int,
    int,
    int,
]:
    if type(projects) is not tuple or (not allow_empty and not projects):
        _fail("projects_invalid")
    if len(projects) > MAX_LEGACY_CATALOG_PROJECTS:
        _fail("project_count_exceeded")
    verified = tuple(_validate_project(project) for project in projects)
    previous_project_id: Optional[str] = None
    document_ids = set()
    entity_count = 0
    source_head_bytes = 0
    row_snapshot_bytes = 0
    for project in verified:
        if (
            previous_project_id is not None
            and project.project_id <= previous_project_id
        ) or project.document_id in document_ids:
            _fail("project_order_invalid")
        previous_project_id = project.project_id
        document_ids.add(project.document_id)
        if entity_count + project.entity_count > MAX_LEGACY_ENTITIES:
            _fail("entity_count_exceeded")
        if (
            source_head_bytes + project.source_head_bytes
            > MAX_LEGACY_CATALOG_SOURCE_HEAD_BYTES
        ):
            _fail("source_head_bytes_exceeded")
        if (
            row_snapshot_bytes + project.row_snapshot_bytes
            > MAX_LEGACY_CATALOG_TARGET_SNAPSHOT_BYTES
        ):
            _fail("row_snapshot_bytes_exceeded")
        entity_count += project.entity_count
        source_head_bytes += project.source_head_bytes
        row_snapshot_bytes += project.row_snapshot_bytes
    return verified, entity_count, source_head_bytes, row_snapshot_bytes


def derive_legacy_catalog_import_id(
    projects: Tuple[LegacyCatalogIntegrityProject, ...],
) -> str:
    """Derive the stable import id before consuming first-scan handles.

    The id binds the complete source descriptors, including both the locked
    source-head digest and deterministic business root.  It is intentionally
    independent from target schema: the same source observation retried after
    a lost acknowledgement addresses the same import batch, while an advanced
    source head produces a different id on a fresh empty target.
    """

    verified, entity_count, source_head_bytes, row_snapshot_bytes = (
        _verified_projects_and_aggregates(projects, allow_empty=False)
    )
    source_catalog_sha256 = _source_digest(verified)
    return _digest(
        LEGACY_CATALOG_IMPORT_ID_HASH_DOMAIN,
        source_catalog_sha256,
        len(verified).to_bytes(8, "big"),
        entity_count.to_bytes(8, "big"),
        source_head_bytes.to_bytes(8, "big"),
        row_snapshot_bytes.to_bytes(8, "big"),
    ).hex()


def build_legacy_catalog_integrity(
    import_id: Optional[str],
    projects: Tuple[LegacyCatalogIntegrityProject, ...],
    *,
    schema_fingerprint: str,
) -> LegacyCatalogIntegrity:
    """Build the unique historical seal projection for one exact live set.

    Empty catalogs use ``import_id=None`` and an empty exact tuple.  Non-empty
    catalogs require the import id of the one complete target batch.  The
    returned ``catalog_id`` is content-addressed over both source descriptors
    and the exact target schema/coverage digest; it is not caller-selected.
    """

    if not _exact_lower_hex(schema_fingerprint, 64):
        _fail("schema_fingerprint_invalid")
    verified, entity_count, source_head_bytes, row_snapshot_bytes = (
        _verified_projects_and_aggregates(projects, allow_empty=True)
    )
    if projects:
        if not _exact_lower_hex(import_id, 64):
            _fail("import_id_invalid")
    elif import_id is not None:
        _fail("import_id_invalid")

    source_catalog_sha256 = _source_digest(verified)
    target_coverage_sha256 = _target_digest(
        import_id,
        verified,
        schema_fingerprint=schema_fingerprint,
    )
    identity_sha256 = _digest(
        LEGACY_CATALOG_ID_HASH_DOMAIN,
        b"empty" if import_id is None else import_id.encode("ascii"),
        source_catalog_sha256,
        target_coverage_sha256,
        len(verified).to_bytes(8, "big"),
        entity_count.to_bytes(8, "big"),
        source_head_bytes.to_bytes(8, "big"),
        row_snapshot_bytes.to_bytes(8, "big"),
    )
    catalog_id = identity_sha256.hex()
    transaction_id = LEGACY_CATALOG_TRANSACTION_PREFIX + catalog_id
    manifest_bytes = encode_payload_bounded(
        _manifest_value(
            catalog_id=catalog_id,
            import_id=import_id,
            projects=verified,
            entity_count=entity_count,
            source_head_bytes=source_head_bytes,
            row_snapshot_bytes=row_snapshot_bytes,
            source_catalog_sha256=source_catalog_sha256,
            target_coverage_sha256=target_coverage_sha256,
            schema_fingerprint=schema_fingerprint,
        ),
        MAX_LEGACY_CATALOG_MANIFEST_BYTES,
        MAX_LEGACY_MANIFEST_NODES,
    )
    manifest_sha256 = _digest(
        LEGACY_CATALOG_MANIFEST_HASH_DOMAIN,
        manifest_bytes,
    )
    seal_sha256 = _digest(
        LEGACY_CATALOG_SEAL_HASH_DOMAIN,
        catalog_id.encode("ascii"),
        transaction_id.encode("ascii"),
        manifest_bytes,
        manifest_sha256,
        source_catalog_sha256,
        target_coverage_sha256,
        LEGACY_CATALOG_TARGET_SCHEMA_VERSION.to_bytes(8, "big"),
        schema_fingerprint.encode("ascii"),
    )
    return LegacyCatalogIntegrity(
        catalog_id=catalog_id,
        transaction_id=transaction_id,
        import_id=import_id,
        project_count=len(verified),
        entity_count=entity_count,
        source_head_bytes=source_head_bytes,
        row_snapshot_bytes=row_snapshot_bytes,
        manifest_bytes=manifest_bytes,
        manifest_sha256=manifest_sha256,
        source_catalog_sha256=source_catalog_sha256,
        target_coverage_sha256=target_coverage_sha256,
        seal_sha256=seal_sha256,
    )


__all__ = [
    "LEGACY_CATALOG_ID_HASH_DOMAIN",
    "LEGACY_CATALOG_IMPORT_ID_HASH_DOMAIN",
    "LEGACY_CATALOG_MANIFEST_HASH_DOMAIN",
    "LEGACY_CATALOG_SEAL_HASH_DOMAIN",
    "LEGACY_CATALOG_SOURCE_HASH_DOMAIN",
    "LEGACY_CATALOG_TARGET_HASH_DOMAIN",
    "LegacyCatalogIntegrity",
    "LegacyCatalogIntegrityError",
    "LegacyCatalogIntegrityProject",
    "build_legacy_catalog_integrity",
    "derive_legacy_catalog_import_id",
]
