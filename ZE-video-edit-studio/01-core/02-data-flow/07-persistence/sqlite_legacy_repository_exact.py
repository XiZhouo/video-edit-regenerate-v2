"""Byte-exact projection and reconciliation for one v5 repository proof.

This is the shared, private leaf below the repository proof writer and its
SDK bridge.  It accepts an already-normalized v4 catalog projection plus the
pure v5 repository member union; arbitrary dictionaries and source objects do
not cross this boundary.  The issuer re-derives both catalog and repository
integrity before a projection can be consumed.

The v4 dependency is reconciled inside the caller's SQLite snapshot.  Once a
v5 receipt exists we deliberately do *not* call the v4 catalog writer's
top-level reconcile function: its historical receipt-total contract predates
the private v5 receipt.  Instead this module composes the shared catalog exact
layer, the normalized import-row exact layer, and a v5-aware namespace check.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any, Iterator, Optional, Sequence, Tuple

from legacy_catalog_contract import LEGACY_CATALOG_TRANSACTION_KIND
from legacy_repository_contract import (
    LEGACY_REPOSITORY_AUTHORITY,
    LEGACY_REPOSITORY_AUTHORITY_STATE,
    LEGACY_REPOSITORY_CUTOVER_READY,
    LEGACY_REPOSITORY_MANIFEST_SCHEMA,
    LEGACY_REPOSITORY_MEMBER_LIVE,
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
from sqlite_kernel import KernelReceipt, _expected_schema_fingerprint
from sqlite_legacy_catalog_exact import (
    CatalogExactConflict,
    CatalogExactCorrupt,
    CatalogExactProjection,
    project_empty_catalog,
    project_normalized_import_plan,
    reconcile_existing_catalog,
)
from sqlite_legacy_import import (
    LegacyImportReconcileStatus,
    SQLITE_IMPORT_TRANSACTION_KIND,
    _ImportPlan,
    _ShadowConflict,
    _ShadowCorrupt,
    _reconcile_connection as _reconcile_import_connection,
)


_COMMITTED_AT_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$"
)
_PROJECTION_ISSUER = object()


class RepositoryExactProjectionError(ValueError):
    """The private SDK-to-core projection input is not canonical."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        if type(code) is not str or not code:
            raise TypeError("code must be a non-empty exact string")
        self.code = code
        super().__init__(code)


class RepositoryExactConflict(Exception):
    """A healthy durable state belongs to another repository projection."""


class RepositoryExactCorrupt(Exception):
    """The claimed durable state is malformed or byte-inexact."""


class _RepositoryExactProjection:
    """An issuer-stamped immutable projection; never part of the public API."""

    __slots__ = ("catalog", "integrity", "members", "schema_fingerprint", "_issuer")

    def __init__(
        self,
        catalog: CatalogExactProjection,
        integrity: LegacyRepositoryIntegrity,
        members: Tuple[LegacyRepositoryIntegrityMember, ...],
        schema_fingerprint: str,
        *,
        _issuer: Any,
    ) -> None:
        if _issuer is not _PROJECTION_ISSUER:
            raise RepositoryExactProjectionError("projection_issuer_invalid")
        self.catalog = catalog
        self.integrity = integrity
        self.members = members
        self.schema_fingerprint = schema_fingerprint
        self._issuer = _issuer


SELECT_CONTROL_SQL = """
    SELECT singleton, authority, authority_state, repository_epoch,
           repository_revision, catalog_revision, cutover_id,
           runtime_generation, code_digest, cutover_ready
    FROM legacy_repository_proof_control ORDER BY singleton
"""
SELECT_SEAL_SQL = """
    SELECT repository_singleton, cutover_id, transaction_id, receipt_kind,
           receipt_protocol, catalog_id, catalog_seal_sha256, proof_protocol,
           manifest_schema, status, selection, repository_epoch,
           repository_revision, catalog_revision, runtime_generation,
           code_digest, project_count, live_project_count,
           deleted_project_count, source_state_bytes, manifest_bytes,
           manifest_sha256, source_repository_sha256,
           catalog_coverage_sha256, proof_sha256, target_schema_version,
           schema_fingerprint, cutover_ready
    FROM legacy_repository_seals ORDER BY repository_singleton
"""
SELECT_MEMBERS_SQL = """
    SELECT cutover_id, ordinal, project_id, generation, status, operation_id,
           source_state_sha256, source_state_bytes, catalog_id,
           catalog_ordinal, import_id, document_id, document_version,
           storage_version, business_root_sha256, entity_count,
           row_snapshot_bytes
    FROM legacy_repository_members ORDER BY ordinal, project_id
"""


def _projection_fail(code: str) -> None:
    raise RepositoryExactProjectionError(code)


def _canonical_catalog(value: Any) -> CatalogExactProjection:
    if type(value) is not CatalogExactProjection:
        _projection_fail("catalog_projection_invalid")
    try:
        if value.import_plan is None:
            rebuilt = project_empty_catalog()
        else:
            if type(value.import_plan) is not _ImportPlan:
                _projection_fail("catalog_import_plan_invalid")
            rebuilt = project_normalized_import_plan(value.import_plan)
    except RepositoryExactProjectionError:
        raise
    except Exception as exc:
        raise RepositoryExactProjectionError("catalog_projection_invalid") from exc
    if (
        value.schema_fingerprint != rebuilt.schema_fingerprint
        or value.integrity != rebuilt.integrity
        or value.projects != rebuilt.projects
    ):
        _projection_fail("catalog_projection_mismatch")
    return rebuilt


def _member_catalog_values(member: LegacyRepositoryIntegrityMember) -> Tuple[Any, ...]:
    return (
        member.project_id,
        member.generation,
        member.document_id,
        member.document_version,
        member.storage_version,
        member.source_state_sha256,
        member.source_state_bytes,
        member.business_root_sha256,
        member.entity_count,
        member.row_snapshot_bytes,
    )


def _catalog_project_values(project: Any) -> Tuple[Any, ...]:
    return (
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


def _verify_live_catalog_binding(
    catalog: CatalogExactProjection,
    members: Tuple[LegacyRepositoryIntegrityMember, ...],
) -> None:
    live = tuple(
        member for member in members if member.status == LEGACY_REPOSITORY_MEMBER_LIVE
    )
    if len(live) != len(catalog.projects):
        _projection_fail("catalog_live_count_mismatch")
    by_ordinal = sorted(live, key=lambda member: member.catalog_ordinal)
    for ordinal, (member, project) in enumerate(zip(by_ordinal, catalog.projects)):
        if (
            member.catalog_ordinal != ordinal
            or member.import_id != catalog.integrity.import_id
            or _member_catalog_values(member) != _catalog_project_values(project)
        ):
            _projection_fail("catalog_member_mismatch")


def _issue_repository_projection(
    catalog_projection: CatalogExactProjection,
    repository_members: Tuple[LegacyRepositoryIntegrityMember, ...],
    *,
    repository_epoch: str,
    repository_revision: int,
    catalog_revision: int,
    runtime_generation: str,
    code_digest: bytes,
) -> _RepositoryExactProjection:
    """Issue one exact projection from normalized, typed source evidence."""

    if type(repository_members) is not tuple:
        _projection_fail("repository_members_invalid")
    catalog = _canonical_catalog(catalog_projection)
    schema_fingerprint = _expected_schema_fingerprint(
        LEGACY_REPOSITORY_TARGET_SCHEMA_VERSION
    )
    try:
        integrity = build_legacy_repository_integrity(
            catalog_id=catalog.integrity.catalog_id,
            catalog_seal_sha256=catalog.integrity.seal_sha256,
            repository_epoch=repository_epoch,
            repository_revision=repository_revision,
            catalog_revision=catalog_revision,
            runtime_generation=runtime_generation,
            code_digest=code_digest,
            members=repository_members,
            schema_fingerprint=schema_fingerprint,
        )
    except LegacyRepositoryIntegrityError as exc:
        raise RepositoryExactProjectionError(exc.code) from exc
    except Exception as exc:
        raise RepositoryExactProjectionError("repository_integrity_rebuild_failed") from exc
    _verify_live_catalog_binding(catalog, repository_members)
    return _RepositoryExactProjection(
        catalog,
        integrity,
        repository_members,
        schema_fingerprint,
        _issuer=_PROJECTION_ISSUER,
    )


def _require_projection(value: Any) -> _RepositoryExactProjection:
    if (
        type(value) is not _RepositoryExactProjection
        or getattr(value, "_issuer", None) is not _PROJECTION_ISSUER
    ):
        _projection_fail("projection_invalid")
    return value


def control_values(projection: _RepositoryExactProjection) -> Tuple[Any, ...]:
    projection = _require_projection(projection)
    integrity = projection.integrity
    return (
        1,
        LEGACY_REPOSITORY_AUTHORITY,
        LEGACY_REPOSITORY_AUTHORITY_STATE,
        integrity.repository_epoch,
        integrity.repository_revision,
        integrity.catalog_revision,
        integrity.cutover_id,
        integrity.runtime_generation,
        integrity.code_digest,
        LEGACY_REPOSITORY_CUTOVER_READY,
    )


def seal_values(projection: _RepositoryExactProjection) -> Tuple[Any, ...]:
    projection = _require_projection(projection)
    integrity = projection.integrity
    return (
        1,
        integrity.cutover_id,
        integrity.transaction_id,
        LEGACY_REPOSITORY_TRANSACTION_KIND,
        LEGACY_REPOSITORY_RECEIPT_PROTOCOL,
        integrity.catalog_id,
        projection.catalog.integrity.seal_sha256,
        LEGACY_REPOSITORY_PROOF_PROTOCOL,
        LEGACY_REPOSITORY_MANIFEST_SCHEMA,
        LEGACY_REPOSITORY_STATUS,
        LEGACY_REPOSITORY_SELECTION,
        integrity.repository_epoch,
        integrity.repository_revision,
        integrity.catalog_revision,
        integrity.runtime_generation,
        integrity.code_digest,
        integrity.project_count,
        integrity.live_project_count,
        integrity.deleted_project_count,
        integrity.source_state_bytes,
        integrity.manifest_bytes,
        integrity.manifest_sha256,
        integrity.source_repository_sha256,
        integrity.catalog_coverage_sha256,
        integrity.proof_sha256,
        LEGACY_REPOSITORY_TARGET_SCHEMA_VERSION,
        projection.schema_fingerprint,
        LEGACY_REPOSITORY_CUTOVER_READY,
    )


def member_values(
    projection: _RepositoryExactProjection,
    ordinal: int,
    member: LegacyRepositoryIntegrityMember,
) -> Tuple[Any, ...]:
    projection = _require_projection(projection)
    live = member.status == LEGACY_REPOSITORY_MEMBER_LIVE
    return (
        projection.integrity.cutover_id,
        ordinal,
        member.project_id,
        member.generation,
        member.status,
        member.operation_id,
        member.source_state_sha256,
        member.source_state_bytes,
        projection.integrity.catalog_id if live else None,
        member.catalog_ordinal,
        member.import_id,
        member.document_id,
        member.document_version,
        member.storage_version,
        member.business_root_sha256,
        member.entity_count,
        member.row_snapshot_bytes,
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
        raise RepositoryExactCorrupt("row_cardinality_invalid")
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
                raise RepositoryExactCorrupt("row_mismatch")
        if cursor.fetchone() is not None:
            raise RepositoryExactCorrupt("unexpected_row")
    finally:
        _close_cursor(cursor)


def _table_count(connection: Any, table: str) -> int:
    queries = {
        "projects": "SELECT count(*) FROM projects",
        "documents": "SELECT count(*) FROM documents",
        "entities": "SELECT count(*) FROM entities",
        "legacy_import_batches": "SELECT count(*) FROM legacy_import_batches",
        "legacy_import_projects": "SELECT count(*) FROM legacy_import_projects",
        "legacy_repository_proof_control": (
            "SELECT count(*) FROM legacy_repository_proof_control"
        ),
        "legacy_repository_seals": "SELECT count(*) FROM legacy_repository_seals",
        "legacy_repository_members": "SELECT count(*) FROM legacy_repository_members",
    }
    sql = queries.get(table)
    if sql is None:  # pragma: no cover - fixed internal call set
        raise AssertionError("unknown table")
    row = _single_row(connection, sql)
    if row is None or type(row[0]) is not int:
        raise RepositoryExactCorrupt("count_invalid")
    return row[0]


def _reconcile_catalog_dependency(
    connection: Any,
    projection: _RepositoryExactProjection,
) -> None:
    """Prove the v4 seal and its normalized business rows in this snapshot."""

    try:
        reconcile_existing_catalog(connection, projection.catalog)
    except CatalogExactConflict as exc:
        raise RepositoryExactConflict("catalog_dependency_conflict") from exc
    except CatalogExactCorrupt as exc:
        raise RepositoryExactCorrupt("catalog_dependency_corrupt") from exc

    plan = projection.catalog.import_plan
    if plan is not None:
        try:
            imported = _reconcile_import_connection(
                connection,
                plan,
                _catalog_exact_verified=True,
            )
        except _ShadowConflict as exc:
            raise RepositoryExactConflict("import_dependency_conflict") from exc
        except _ShadowCorrupt as exc:
            raise RepositoryExactCorrupt("import_dependency_corrupt") from exc
        if imported.status is not LegacyImportReconcileStatus.IMPORTED:
            raise RepositoryExactConflict("import_dependency_not_exact")

    expected = {
        "projects": 0 if plan is None else plan.project_count,
        "documents": 0 if plan is None else plan.project_count,
        "entities": 0 if plan is None else plan.entity_count,
        "legacy_import_batches": 0 if plan is None else 1,
        "legacy_import_projects": 0 if plan is None else plan.project_count,
    }
    if any(_table_count(connection, table) != count for table, count in expected.items()):
        raise RepositoryExactConflict("catalog_target_coverage_mismatch")


def _expected_receipt_identities(
    projection: _RepositoryExactProjection,
    *,
    sealed: bool,
) -> Tuple[Tuple[str, str], ...]:
    expected = []
    plan = projection.catalog.import_plan
    if plan is not None:
        expected.append((plan.transaction_id, SQLITE_IMPORT_TRANSACTION_KIND))
    expected.append(
        (
            projection.catalog.integrity.transaction_id,
            LEGACY_CATALOG_TRANSACTION_KIND,
        )
    )
    if sealed:
        expected.append(
            (projection.integrity.transaction_id, LEGACY_REPOSITORY_TRANSACTION_KIND)
        )
    return tuple(sorted(expected))


def _verify_receipt_namespace(
    connection: Any,
    projection: _RepositoryExactProjection,
    *,
    sealed: bool,
) -> None:
    _stream_exact(
        connection,
        "SELECT transaction_id, kind FROM transaction_receipts "
        "ORDER BY transaction_id, kind",
        iter(_expected_receipt_identities(projection, sealed=sealed)),
    )


def repository_control(connection: Any) -> Optional[Tuple[Any, ...]]:
    row = _single_row(connection, SELECT_CONTROL_SQL)
    return None if row is None else tuple(row)


def reconcile_unsealed_repository(
    connection: Any,
    projection: _RepositoryExactProjection,
) -> None:
    """Prove a fresh v4 dependency and a wholly unused v5 namespace."""

    projection = _require_projection(projection)
    _reconcile_catalog_dependency(connection, projection)
    expected = {
        "legacy_repository_proof_control": 0,
        "legacy_repository_seals": 0,
        "legacy_repository_members": 0,
    }
    if any(_table_count(connection, table) != count for table, count in expected.items()):
        raise RepositoryExactCorrupt("repository_namespace_partial")
    _verify_receipt_namespace(connection, projection, sealed=False)


def reconcile_existing_repository(
    connection: Any,
    projection: _RepositoryExactProjection,
) -> KernelReceipt:
    """Return a receipt only after v4 and every v5 field are byte-exact."""

    projection = _require_projection(projection)
    _reconcile_catalog_dependency(connection, projection)
    control = repository_control(connection)
    if control is None:
        raise RepositoryExactConflict("repository_not_sealed")
    if control[6] != projection.integrity.cutover_id:
        raise RepositoryExactConflict("different_repository_already_sealed")
    if control != control_values(projection):
        raise RepositoryExactCorrupt("repository_control_mismatch")

    seal = _single_row(connection, SELECT_SEAL_SQL)
    if seal is None:
        raise RepositoryExactCorrupt("repository_seal_missing")
    if tuple(seal) != seal_values(projection):
        raise RepositoryExactCorrupt("repository_seal_mismatch")
    _stream_exact(
        connection,
        SELECT_MEMBERS_SQL,
        (
            member_values(projection, ordinal, member)
            for ordinal, member in enumerate(projection.members)
        ),
    )

    row = _single_row(
        connection,
        "SELECT transaction_id, kind, committed_at "
        "FROM transaction_receipts WHERE transaction_id = ?",
        (projection.integrity.transaction_id,),
    )
    if row is None:
        raise RepositoryExactCorrupt("repository_receipt_missing")
    values = tuple(row)
    if (
        values[0] != projection.integrity.transaction_id
        or values[1] != LEGACY_REPOSITORY_TRANSACTION_KIND
        or type(values[2]) is not str
        or _COMMITTED_AT_RE.fullmatch(values[2]) is None
    ):
        raise RepositoryExactCorrupt("repository_receipt_invalid")
    _verify_receipt_namespace(connection, projection, sealed=True)
    return KernelReceipt(values[0], values[1], values[2])


__all__: list[str] = []
