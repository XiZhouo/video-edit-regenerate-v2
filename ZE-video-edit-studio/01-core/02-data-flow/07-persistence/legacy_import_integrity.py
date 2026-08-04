"""Canonical byte integrity for one verified legacy-import batch.

This leaf is the single source of truth for the legacy payload framing,
manifest value/encoding and the three domain-separated digests.  Callers must
first establish business-snapshot validity, project ordering and source
authority; this module only projects already verified exact values into the
stable wire representation.  It performs no filesystem or SQLite I/O and
makes no authority decision.
"""

from __future__ import annotations

import hashlib
from typing import Any, Iterable, NamedTuple

from business_codec import encode_payload_bounded
from hash_framing import framed_size, update_hash_frame
from legacy_import_contract import (
    LEGACY_BATCH_PROTOCOL,
    LEGACY_MANIFEST_SCHEMA,
    LEGACY_PAYLOAD_PROTOCOL,
    MAX_LEGACY_ENTITIES,
    MAX_LEGACY_MANIFEST_BYTES,
    MAX_LEGACY_MANIFEST_NODES,
    MAX_LEGACY_PROJECTS,
    MAX_LEGACY_ROW_SNAPSHOT_BYTES,
)


LEGACY_PAYLOAD_HASH_DOMAIN = LEGACY_PAYLOAD_PROTOCOL.encode("ascii")
LEGACY_MANIFEST_HASH_DOMAIN = LEGACY_MANIFEST_SCHEMA.encode("ascii")
LEGACY_BATCH_HASH_DOMAIN = LEGACY_BATCH_PROTOCOL.encode("ascii")


class LegacyImportIntegrityError(ValueError):
    """Stable structural/resource failure without copying rejected data."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        if type(code) is not str or not code:
            raise TypeError("code must be a non-empty exact string")
        self.code = code
        super().__init__(code)


class LegacyImportIntegrityProject(NamedTuple):
    """Exact manifest metadata and canonical row bytes for one project."""

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
    project_snapshot_bytes: bytes
    document_snapshot_bytes: bytes
    entity_snapshot_bytes: Iterable[bytes]


class LegacyImportIntegrity(NamedTuple):
    """Complete deterministic integrity projection for one import batch."""

    entity_count: int
    row_snapshot_bytes: int
    framed_payload_bytes: int
    manifest_bytes: bytes
    manifest_sha256: bytes
    payload_sha256: bytes
    batch_sha256: bytes


def _fail(code: str) -> None:
    raise LegacyImportIntegrityError(code)


def _validate_project(project: Any) -> LegacyImportIntegrityProject:
    if type(project) is not LegacyImportIntegrityProject:
        _fail("project_invalid")
    if (
        type(project.project_id) is not str
        or type(project.generation) is not int
        or type(project.document_id) is not str
        or type(project.document_version) is not int
        or type(project.storage_version) is not str
        or type(project.source_head_sha256) is not bytes
        or len(project.source_head_sha256) != 32
        or type(project.source_head_bytes) is not int
        or project.source_head_bytes < 1
        or type(project.business_root_sha256) is not bytes
        or len(project.business_root_sha256) != 32
        or type(project.entity_count) is not int
        or project.entity_count < 0
        or type(project.row_snapshot_bytes) is not int
        or project.row_snapshot_bytes < 0
        or type(project.project_snapshot_bytes) is not bytes
        or type(project.document_snapshot_bytes) is not bytes
    ):
        _fail("project_invalid")
    if project.entity_count > MAX_LEGACY_ENTITIES:
        _fail("entity_count_exceeded")
    if project.row_snapshot_bytes > MAX_LEGACY_ROW_SNAPSHOT_BYTES:
        _fail("row_snapshot_bytes_exceeded")
    try:
        iter(project.entity_snapshot_bytes)
    except TypeError:
        _fail("project_invalid")
    return project


def _payload_integrity(
    projects: tuple[LegacyImportIntegrityProject, ...],
) -> tuple[bytes, int, int, int]:
    hasher = hashlib.sha256()
    framed_bytes = 0
    entity_count = 0
    row_snapshot_bytes = 0

    update_hash_frame(hasher, LEGACY_PAYLOAD_HASH_DOMAIN)
    framed_bytes += framed_size(LEGACY_PAYLOAD_HASH_DOMAIN)
    project_count_bytes = len(projects).to_bytes(8, "big")
    update_hash_frame(hasher, project_count_bytes)
    framed_bytes += framed_size(project_count_bytes)

    for project in projects:
        # Reject declared batch overflow before touching a project iterable.
        # A caller-controlled generator may be huge or infinite; limits are a
        # consumption boundary, not a post-hoc accounting assertion.
        if entity_count + project.entity_count > MAX_LEGACY_ENTITIES:
            _fail("entity_count_exceeded")
        if (
            row_snapshot_bytes + project.row_snapshot_bytes
            > MAX_LEGACY_ROW_SNAPSHOT_BYTES
        ):
            _fail("row_snapshot_bytes_exceeded")
        project_row_start = row_snapshot_bytes
        for payload in (
            project.project_snapshot_bytes,
            project.document_snapshot_bytes,
        ):
            update_hash_frame(hasher, payload)
            framed_bytes += framed_size(payload)
            row_snapshot_bytes += len(payload)

        count_bytes = project.entity_count.to_bytes(8, "big")
        update_hash_frame(hasher, count_bytes)
        framed_bytes += framed_size(count_bytes)
        actual_project_entity_count = 0
        for payload in project.entity_snapshot_bytes:
            if actual_project_entity_count >= project.entity_count:
                _fail("project_entity_count_mismatch")
            if type(payload) is not bytes:
                _fail("project_invalid")
            if (
                row_snapshot_bytes - project_row_start + len(payload)
                > project.row_snapshot_bytes
            ):
                _fail("project_row_snapshot_bytes_mismatch")
            update_hash_frame(hasher, payload)
            framed_bytes += framed_size(payload)
            row_snapshot_bytes += len(payload)
            actual_project_entity_count += 1
        if actual_project_entity_count != project.entity_count:
            _fail("project_entity_count_mismatch")
        entity_count += actual_project_entity_count
        if project.row_snapshot_bytes != row_snapshot_bytes - project_row_start:
            _fail("project_row_snapshot_bytes_mismatch")

    return hasher.digest(), framed_bytes, entity_count, row_snapshot_bytes


def _manifest_value(
    import_id: str,
    projects: tuple[LegacyImportIntegrityProject, ...],
    *,
    payload_sha256: bytes,
    framed_payload_bytes: int,
    entity_count: int,
    row_snapshot_bytes: int,
) -> dict[str, Any]:
    project_rows = [
        {
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
        for project in projects
    ]
    return {
        "schema": LEGACY_MANIFEST_SCHEMA,
        "protocol": LEGACY_BATCH_PROTOCOL,
        "payload_protocol": LEGACY_PAYLOAD_PROTOCOL,
        "import_id": import_id,
        "selection": "explicit_projects",
        "project_count": len(projects),
        "entity_count": entity_count,
        "row_snapshot_bytes": row_snapshot_bytes,
        "framed_payload_bytes": framed_payload_bytes,
        "payload_sha256": payload_sha256.hex(),
        "projects": project_rows,
    }


def _digest_framed(domain: bytes, *parts: bytes) -> bytes:
    hasher = hashlib.sha256()
    update_hash_frame(hasher, domain)
    for part in parts:
        update_hash_frame(hasher, part)
    return hasher.digest()


def build_legacy_import_integrity(
    import_id: str,
    projects: tuple[LegacyImportIntegrityProject, ...],
) -> LegacyImportIntegrity:
    """Return the unique manifest bytes and digests for verified project rows.

    ``BusinessCodecError`` remains the canonical encoding failure boundary;
    exact input-shape and resource failures use
    :class:`LegacyImportIntegrityError`.
    """
    if type(import_id) is not str:
        _fail("import_id_invalid")
    if type(projects) is not tuple or not projects:
        _fail("projects_invalid")
    if len(projects) > MAX_LEGACY_PROJECTS:
        _fail("project_count_exceeded")
    verified_projects = tuple(_validate_project(project) for project in projects)

    payload_sha256, framed_payload_bytes, entity_count, row_snapshot_bytes = (
        _payload_integrity(verified_projects)
    )
    # _payload_integrity enforces both budgets before consuming each project
    # and stops a lying iterable on its first count/byte beyond declaration.

    manifest_bytes = encode_payload_bounded(
        _manifest_value(
            import_id,
            verified_projects,
            payload_sha256=payload_sha256,
            framed_payload_bytes=framed_payload_bytes,
            entity_count=entity_count,
            row_snapshot_bytes=row_snapshot_bytes,
        ),
        MAX_LEGACY_MANIFEST_BYTES,
        MAX_LEGACY_MANIFEST_NODES,
    )
    manifest_sha256 = _digest_framed(
        LEGACY_MANIFEST_HASH_DOMAIN,
        manifest_bytes,
    )
    batch_sha256 = _digest_framed(
        LEGACY_BATCH_HASH_DOMAIN,
        manifest_bytes,
        payload_sha256,
    )
    return LegacyImportIntegrity(
        entity_count=entity_count,
        row_snapshot_bytes=row_snapshot_bytes,
        framed_payload_bytes=framed_payload_bytes,
        manifest_bytes=manifest_bytes,
        manifest_sha256=manifest_sha256,
        payload_sha256=payload_sha256,
        batch_sha256=batch_sha256,
    )


__all__ = [
    "LEGACY_BATCH_HASH_DOMAIN",
    "LEGACY_MANIFEST_HASH_DOMAIN",
    "LEGACY_PAYLOAD_HASH_DOMAIN",
    "LegacyImportIntegrity",
    "LegacyImportIntegrityError",
    "LegacyImportIntegrityProject",
    "build_legacy_import_integrity",
]
