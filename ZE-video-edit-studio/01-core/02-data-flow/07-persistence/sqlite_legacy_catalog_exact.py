"""Shared exact projection/reconcile for one historical catalog seal.

This private leaf deliberately does not import ``sqlite_legacy_import``.  It
accepts only a caller-normalized import plan, projects the canonical catalog
integrity, and compares every persisted seal/member byte.  Keeping this logic
below both writers prevents the importer and catalog sealer from drifting into
different definitions of a positive sealed outcome.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional, Sequence, Tuple

from legacy_catalog_contract import (
    LEGACY_CATALOG_CUTOVER_READY,
    LEGACY_CATALOG_MANIFEST_SCHEMA,
    LEGACY_CATALOG_RECEIPT_PROTOCOL,
    LEGACY_CATALOG_SEAL_PROTOCOL,
    LEGACY_CATALOG_SELECTION,
    LEGACY_CATALOG_STATUS,
    LEGACY_CATALOG_TARGET_SCHEMA_VERSION,
    LEGACY_CATALOG_TRANSACTION_KIND,
    SQLITE_CATALOG_STATE_HISTORICAL_FENCED,
    SQLITE_CATALOG_STATE_KEY,
)
from legacy_catalog_integrity import (
    LegacyCatalogIntegrity,
    LegacyCatalogIntegrityProject,
    build_legacy_catalog_integrity,
)
from sqlite_kernel import KernelReceipt, _expected_schema_fingerprint


_COMMITTED_AT_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$"
)


class CatalogExactConflict(Exception):
    """A healthy seal belongs to a different canonical plan."""


class CatalogExactCorrupt(Exception):
    """The claimed seal is missing, malformed, or byte-inexact."""


@dataclass(frozen=True)
class CatalogExactProjection:
    integrity: LegacyCatalogIntegrity = field(repr=False)
    schema_fingerprint: str
    projects: Tuple[LegacyCatalogIntegrityProject, ...] = field(repr=False)
    import_plan: Optional[Any] = field(repr=False)


SELECT_SEAL_SQL = """
    SELECT repository_singleton, catalog_id, transaction_id, receipt_kind,
           receipt_protocol, import_id, seal_protocol, manifest_schema, status,
           selection, project_count, entity_count, source_head_bytes,
           row_snapshot_bytes, manifest_bytes, manifest_sha256,
           source_catalog_sha256, target_coverage_sha256, seal_sha256,
           target_schema_version, schema_fingerprint, cutover_ready
    FROM legacy_catalog_seals ORDER BY repository_singleton
"""
SELECT_MEMBERS_SQL = """
    SELECT catalog_id, ordinal, import_id, project_id, generation, document_id,
           document_version, storage_version, source_head_sha256,
           source_head_bytes, business_root_sha256, entity_count,
           row_snapshot_bytes
    FROM legacy_catalog_members ORDER BY ordinal, project_id
"""


def _descriptor(project: Any) -> LegacyCatalogIntegrityProject:
    return LegacyCatalogIntegrityProject(
        project_id=project.project_id,
        generation=project.generation,
        document_id=project.document_id,
        document_version=project.document_version,
        storage_version=project.storage_version,
        source_head_sha256=project.source_head_sha256,
        source_head_bytes=project.source_head_bytes,
        business_root_sha256=project.business_root_sha256,
        entity_count=project.entity_count,
        row_snapshot_bytes=project.row_snapshot_bytes,
    )


def project_normalized_import_plan(plan: Any) -> CatalogExactProjection:
    projects = tuple(_descriptor(project) for project in plan.projects)
    schema_fingerprint = _expected_schema_fingerprint(
        LEGACY_CATALOG_TARGET_SCHEMA_VERSION
    )
    integrity = build_legacy_catalog_integrity(
        plan.import_id,
        projects,
        schema_fingerprint=schema_fingerprint,
    )
    return CatalogExactProjection(
        integrity=integrity,
        schema_fingerprint=schema_fingerprint,
        projects=projects,
        import_plan=plan,
    )


def project_empty_catalog() -> CatalogExactProjection:
    schema_fingerprint = _expected_schema_fingerprint(
        LEGACY_CATALOG_TARGET_SCHEMA_VERSION
    )
    integrity = build_legacy_catalog_integrity(
        None,
        (),
        schema_fingerprint=schema_fingerprint,
    )
    return CatalogExactProjection(
        integrity=integrity,
        schema_fingerprint=schema_fingerprint,
        projects=(),
        import_plan=None,
    )


def seal_values(projection: CatalogExactProjection) -> Tuple[Any, ...]:
    integrity = projection.integrity
    return (
        1,
        integrity.catalog_id,
        integrity.transaction_id,
        LEGACY_CATALOG_TRANSACTION_KIND,
        LEGACY_CATALOG_RECEIPT_PROTOCOL,
        integrity.import_id,
        LEGACY_CATALOG_SEAL_PROTOCOL,
        LEGACY_CATALOG_MANIFEST_SCHEMA,
        LEGACY_CATALOG_STATUS,
        LEGACY_CATALOG_SELECTION,
        integrity.project_count,
        integrity.entity_count,
        integrity.source_head_bytes,
        integrity.row_snapshot_bytes,
        integrity.manifest_bytes,
        integrity.manifest_sha256,
        integrity.source_catalog_sha256,
        integrity.target_coverage_sha256,
        integrity.seal_sha256,
        LEGACY_CATALOG_TARGET_SCHEMA_VERSION,
        projection.schema_fingerprint,
        LEGACY_CATALOG_CUTOVER_READY,
    )


def member_values(
    projection: CatalogExactProjection,
    ordinal: int,
    project: LegacyCatalogIntegrityProject,
) -> Tuple[Any, ...]:
    integrity = projection.integrity
    if integrity.import_id is None:
        raise AssertionError("empty catalog cannot have members")
    return (
        integrity.catalog_id,
        ordinal,
        integrity.import_id,
        project.project_id,
        project.generation,
        project.document_id,
        project.document_version,
        project.storage_version,
        project.source_head_sha256,
        project.source_head_bytes,
        project.business_root_sha256,
        project.entity_count,
        project.row_snapshot_bytes,
    )


def _close_cursor(cursor: sqlite3.Cursor) -> None:
    try:
        cursor.close()
    except sqlite3.Error:
        pass


def _single_row(connection: Any, sql: str, values: Sequence[Any] = ()) -> Any:
    cursor = connection.execute(sql, values)
    try:
        first = cursor.fetchone()
        second = cursor.fetchone()
    finally:
        _close_cursor(cursor)
    if second is not None:
        raise CatalogExactCorrupt("row_cardinality_invalid")
    return first


def _stream_exact(
    connection: Any,
    sql: str,
    expected: Iterator[Tuple[Any, ...]],
) -> None:
    cursor = connection.execute(sql)
    try:
        for wanted in expected:
            actual = cursor.fetchone()
            if actual is None or tuple(actual) != wanted:
                raise CatalogExactCorrupt("row_mismatch")
        if cursor.fetchone() is not None:
            raise CatalogExactCorrupt("unexpected_row")
    finally:
        _close_cursor(cursor)


def catalog_marker(connection: Any) -> Optional[Tuple[Any, ...]]:
    row = _single_row(
        connection,
        "SELECT value FROM kernel_metadata WHERE key = ?",
        (SQLITE_CATALOG_STATE_KEY,),
    )
    return None if row is None else tuple(row)


def reconcile_existing_catalog(
    connection: Any,
    projection: CatalogExactProjection,
) -> KernelReceipt:
    """Return a receipt only after every seal/member field is byte-exact."""

    marker = catalog_marker(connection)
    if marker is None:
        raise CatalogExactConflict("catalog_not_sealed")
    if marker != (SQLITE_CATALOG_STATE_HISTORICAL_FENCED,):
        raise CatalogExactCorrupt("catalog_marker_invalid")

    seal = _single_row(connection, SELECT_SEAL_SQL)
    if seal is None:
        raise CatalogExactCorrupt("seal_missing")
    actual_seal = tuple(seal)
    if actual_seal[1] != projection.integrity.catalog_id:
        raise CatalogExactConflict("different_catalog_already_sealed")
    if actual_seal != seal_values(projection):
        raise CatalogExactCorrupt("same_catalog_id_mismatch")

    _stream_exact(
        connection,
        SELECT_MEMBERS_SQL,
        (
            member_values(projection, ordinal, project)
            for ordinal, project in enumerate(projection.projects)
        ),
    )
    integrity = projection.integrity
    row = _single_row(
        connection,
        "SELECT transaction_id, kind, committed_at "
        "FROM transaction_receipts WHERE transaction_id = ?",
        (integrity.transaction_id,),
    )
    if row is None:
        raise CatalogExactCorrupt("receipt_missing")
    values = tuple(row)
    if (
        values[0] != integrity.transaction_id
        or values[1] != LEGACY_CATALOG_TRANSACTION_KIND
        or type(values[2]) is not str
        or _COMMITTED_AT_RE.fullmatch(values[2]) is None
    ):
        raise CatalogExactCorrupt("receipt_invalid")
    return KernelReceipt(values[0], values[1], values[2])


__all__: list[str] = []
