"""INSERT-only writer for one historical fenced legacy-catalog seal.

The seal proves exact equality between one immutable import plan and one
SQLite terminal shadow at a completed observation.  Its sole supported caller
must first prove that the plan is still active and was rebuilt from the whole
strict catalog while holding the repository-wide legacy mutation barrier.
This leaf deliberately performs no source I/O, keeps ``authority=legacy`` and
can never claim cutover readiness.  The module-local capability only narrows
supported in-process callers; Python process memory is not a security sandbox
and the capability is not an authorization boundary.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Optional, Sequence, Tuple

from legacy_catalog_contract import (
    LEGACY_CATALOG_TRANSACTION_KIND,
    SQLITE_CATALOG_STATE_HISTORICAL_FENCED,
    SQLITE_CATALOG_STATE_KEY,
)
from legacy_catalog_integrity import LegacyCatalogIntegrityError
from legacy_import_contract import (
    SQLITE_IMPORT_STATE_KEY,
    SQLITE_IMPORT_STATE_TERMINAL_SHADOW,
    SQLITE_IMPORT_TRANSACTION_KIND,
)
from sqlite_kernel import (
    KernelReceipt,
    SQLiteKernelError,
    SqliteDatabase,
)
from sqlite_legacy_catalog_exact import (
    CatalogExactConflict as _CatalogConflict,
    CatalogExactCorrupt as _CatalogCorrupt,
    CatalogExactProjection as _CatalogProjection,
    SELECT_MEMBERS_SQL as _SELECT_MEMBERS_SQL,
    SELECT_SEAL_SQL as _SELECT_SEAL_SQL,
    catalog_marker as _catalog_marker,
    member_values as _member_values,
    project_empty_catalog as _project_empty_exact,
    project_normalized_import_plan as _project_normalized_import_plan,
    reconcile_existing_catalog as _reconcile_existing_catalog,
    seal_values as _seal_values,
)
from sqlite_legacy_import import (
    LegacyImportReconcileStatus,
    SQLiteLegacyImportError,
    _ImportPlan,
    _ShadowConflict,
    _ShadowCorrupt,
    _project_plan_graph,
    _reconcile_connection as _reconcile_import_connection,
    _unknown_kernel_error,
)

# The future SDK bridge imports this exact singleton.  Its purpose is to keep
# accidental application callers off the low-level writer, not to defend
# against hostile code running inside this Python process.
_LEGACY_CATALOG_SEAL_CAPABILITY = object()


class SQLiteLegacyCatalogError(RuntimeError):
    """Stable, payload-free failure at the historical seal boundary."""

    __slots__ = ("code", "reason")

    def __init__(self, code: str, reason: str) -> None:
        if type(code) is not str or not code:
            raise TypeError("code must be a non-empty exact string")
        if type(reason) is not str or not reason:
            raise TypeError("reason must be a non-empty exact string")
        self.code = code
        self.reason = reason
        super().__init__(code)


def _fail(code: str, reason: str) -> None:
    raise SQLiteLegacyCatalogError(code, reason)


class LegacyCatalogReconcileStatus(Enum):
    SEALED = "sealed"
    NOT_SEALED = "not_sealed"
    CONFLICT = "conflict"
    CORRUPT = "corrupt"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class LegacyCatalogReconcileResult:
    status: LegacyCatalogReconcileStatus
    catalog_id: str
    import_id: Optional[str]
    transaction_id: str
    receipt: Optional[KernelReceipt] = None
    error_code: str = ""
    message: str = ""


@dataclass(frozen=True)
class LegacyCatalogSealResult:
    """Historical evidence result; it is never an authority-cutover token."""

    catalog_id: str
    import_id: Optional[str]
    transaction_id: str
    receipt: KernelReceipt
    already_sealed: bool = False
    recovered_after_ack_loss: bool = False
    authority: str = field(init=False, default="legacy")

    @property
    def cutover_ready(self) -> bool:
        return False


class _CatalogAuthorizer:
    """Poison a seal transaction after any non-INSERT mutation attempt."""

    _INSERT_TABLES = frozenset(
        (
            "kernel_metadata",
            "transaction_receipts",
            "legacy_catalog_seals",
            "legacy_catalog_members",
        )
    )
    _CONTROL_ACTIONS = frozenset(
        getattr(sqlite3, name)
        for name in (
            "SQLITE_ATTACH",
            "SQLITE_DETACH",
            "SQLITE_PRAGMA",
            "SQLITE_TRANSACTION",
            "SQLITE_SAVEPOINT",
            "SQLITE_CREATE_INDEX",
            "SQLITE_CREATE_TABLE",
            "SQLITE_CREATE_TEMP_INDEX",
            "SQLITE_CREATE_TEMP_TABLE",
            "SQLITE_CREATE_TEMP_TRIGGER",
            "SQLITE_CREATE_TEMP_VIEW",
            "SQLITE_CREATE_TRIGGER",
            "SQLITE_CREATE_VIEW",
            "SQLITE_CREATE_VTABLE",
            "SQLITE_DROP_INDEX",
            "SQLITE_DROP_TABLE",
            "SQLITE_DROP_TEMP_INDEX",
            "SQLITE_DROP_TEMP_TABLE",
            "SQLITE_DROP_TEMP_TRIGGER",
            "SQLITE_DROP_TEMP_VIEW",
            "SQLITE_DROP_TRIGGER",
            "SQLITE_DROP_VIEW",
            "SQLITE_DROP_VTABLE",
            "SQLITE_ALTER_TABLE",
            "SQLITE_REINDEX",
            "SQLITE_ANALYZE",
        )
        if hasattr(sqlite3, name)
    )

    def __init__(self) -> None:
        self.denied = False

    def __call__(
        self,
        action: int,
        arg1: Optional[str],
        arg2: Optional[str],
        database_name: Optional[str],
        trigger_name: Optional[str],
    ) -> int:
        del arg2, database_name, trigger_name
        if action in self._CONTROL_ACTIONS:
            self.denied = True
            return sqlite3.SQLITE_DENY
        if action == getattr(sqlite3, "SQLITE_INSERT", -1):
            if (arg1 or "").lower() not in self._INSERT_TABLES:
                self.denied = True
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        if action in (
            getattr(sqlite3, "SQLITE_UPDATE", -2),
            getattr(sqlite3, "SQLITE_DELETE", -3),
        ):
            self.denied = True
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK


def _allow_authorizer(*args: Any) -> int:
    del args
    return sqlite3.SQLITE_OK


_INSERT_MARKER_SQL = """
    INSERT INTO kernel_metadata(key, value) VALUES (?, ?)
"""
_INSERT_RECEIPT_SQL = """
    INSERT INTO transaction_receipts(transaction_id, kind, committed_at)
    VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
"""
_INSERT_SEAL_SQL = """
    INSERT INTO legacy_catalog_seals(
        repository_singleton, catalog_id, transaction_id, receipt_kind,
        receipt_protocol, import_id, seal_protocol, manifest_schema, status,
        selection, project_count, entity_count, source_head_bytes,
        row_snapshot_bytes, manifest_bytes, manifest_sha256,
        source_catalog_sha256, target_coverage_sha256, seal_sha256,
        target_schema_version, schema_fingerprint, cutover_ready
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""
_INSERT_MEMBER_SQL = """
    INSERT INTO legacy_catalog_members(
        catalog_id, ordinal, import_id, project_id, generation, document_id,
        document_version, storage_version, source_head_sha256,
        source_head_bytes, business_root_sha256, entity_count,
        row_snapshot_bytes
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_FIXED_INSERT_SQL = frozenset(
    (
        _INSERT_MARKER_SQL,
        _INSERT_RECEIPT_SQL,
        _INSERT_SEAL_SQL,
        _INSERT_MEMBER_SQL,
    )
)

def _project_nonempty(plan: Any) -> _CatalogProjection:
    try:
        projected = _project_plan_graph(plan)
        return _project_normalized_import_plan(projected)
    except (SQLiteLegacyImportError, LegacyCatalogIntegrityError) as exc:
        raise SQLiteLegacyCatalogError(
            "sqlite_legacy_catalog_plan_invalid", "verified_plan_invalid"
        ) from exc


def _project_empty() -> _CatalogProjection:
    try:
        return _project_empty_exact()
    except LegacyCatalogIntegrityError as exc:  # pragma: no cover - invariant
        raise SQLiteLegacyCatalogError(
            "sqlite_legacy_catalog_integrity_invalid", "empty_projection_invalid"
        ) from exc


def _close_cursor(cursor: sqlite3.Cursor) -> None:
    try:
        cursor.close()
    except sqlite3.Error:
        pass


def _execute_closed(connection: Any, sql: str, values: Sequence[Any]) -> None:
    if sql != "BEGIN IMMEDIATE" and sql not in _FIXED_INSERT_SQL:
        _fail("sqlite_legacy_catalog_statement_forbidden", "statement_not_fixed")
    cursor = connection.execute(sql, values)
    _close_cursor(cursor)


def _execute_many_closed(
    connection: Any,
    sql: str,
    values: Iterable[Sequence[Any]],
) -> None:
    if sql not in _FIXED_INSERT_SQL:
        _fail("sqlite_legacy_catalog_statement_forbidden", "statement_not_fixed")
    cursor = connection.executemany(sql, values)
    _close_cursor(cursor)


def _single_row(connection: Any, sql: str, values: Sequence[Any] = ()) -> Any:
    cursor = connection.execute(sql, values)
    try:
        first = cursor.fetchone()
        second = cursor.fetchone()
    finally:
        _close_cursor(cursor)
    if second is not None:
        raise _CatalogCorrupt("row_cardinality_invalid")
    return first


def _table_count(connection: Any, table: str) -> int:
    queries = {
        "projects": "SELECT count(*) FROM projects",
        "documents": "SELECT count(*) FROM documents",
        "entities": "SELECT count(*) FROM entities",
        "transaction_receipts": "SELECT count(*) FROM transaction_receipts",
        "legacy_import_batches": "SELECT count(*) FROM legacy_import_batches",
        "legacy_import_projects": "SELECT count(*) FROM legacy_import_projects",
        "legacy_catalog_seals": "SELECT count(*) FROM legacy_catalog_seals",
        "legacy_catalog_members": "SELECT count(*) FROM legacy_catalog_members",
    }
    sql = queries.get(table)
    if sql is None:  # pragma: no cover - internal fixed call set
        raise AssertionError("unknown table")
    row = _single_row(connection, sql)
    if row is None or type(row[0]) is not int:
        raise _CatalogCorrupt("count_invalid")
    return row[0]


def _exact_target(
    connection: Any,
    projection: _CatalogProjection,
    *,
    sealed: bool,
) -> None:
    """Prove target equality, including namespaces outside plan JOINs."""

    import_plan = projection.import_plan
    expected_catalog_rows = 1 if sealed else 0
    expected_catalog_members = len(projection.projects) if sealed else 0
    if (
        _table_count(connection, "legacy_catalog_seals")
        != expected_catalog_rows
        or _table_count(connection, "legacy_catalog_members")
        != expected_catalog_members
    ):
        raise _CatalogConflict("catalog_shape_mismatch")

    if import_plan is None:
        import_marker = _single_row(
            connection,
            "SELECT value FROM kernel_metadata WHERE key = ?",
            (SQLITE_IMPORT_STATE_KEY,),
        )
        if import_marker is not None:
            raise _CatalogConflict("nonempty_target_for_empty_catalog")
        expected = {
            "projects": 0,
            "documents": 0,
            "entities": 0,
            "legacy_import_batches": 0,
            "legacy_import_projects": 0,
            "transaction_receipts": 1 if sealed else 0,
        }
        if any(_table_count(connection, table) != count for table, count in expected.items()):
            raise _CatalogConflict("empty_target_not_exact")
        return

    import_marker = _single_row(
        connection,
        "SELECT value FROM kernel_metadata WHERE key = ?",
        (SQLITE_IMPORT_STATE_KEY,),
    )
    if import_marker is None or tuple(import_marker) != (
        SQLITE_IMPORT_STATE_TERMINAL_SHADOW,
    ):
        raise _CatalogConflict("terminal_import_missing")
    try:
        reconciled = _reconcile_import_connection(
            connection,
            import_plan,
            # The sealed path has already completed the one shared exact
            # catalog comparison immediately above this substrate check.
            _catalog_exact_verified=sealed,
        )
    except _ShadowConflict as exc:
        raise _CatalogConflict("import_target_conflict") from exc
    except _ShadowCorrupt as exc:
        raise _CatalogCorrupt("import_target_corrupt") from exc
    if reconciled.status is not LegacyImportReconcileStatus.IMPORTED:
        raise _CatalogConflict("import_target_not_exact")

    expected = {
        "projects": import_plan.project_count,
        "documents": import_plan.project_count,
        "entities": import_plan.entity_count,
        "legacy_import_batches": 1,
        "legacy_import_projects": import_plan.project_count,
        "transaction_receipts": 2 if sealed else 1,
    }
    if any(_table_count(connection, table) != count for table, count in expected.items()):
        raise _CatalogConflict("target_coverage_not_exact")
    import_receipt = _single_row(
        connection,
        "SELECT transaction_id, kind FROM transaction_receipts "
        "WHERE kind = ?",
        (SQLITE_IMPORT_TRANSACTION_KIND,),
    )
    if import_receipt is None or tuple(import_receipt) != (
        import_plan.transaction_id,
        SQLITE_IMPORT_TRANSACTION_KIND,
    ):
        raise _CatalogCorrupt("import_receipt_invalid")


def _reconcile_connection(
    connection: Any, projection: _CatalogProjection
) -> LegacyCatalogReconcileResult:
    integrity = projection.integrity
    marker = _catalog_marker(connection)
    if marker is None:
        _exact_target(connection, projection, sealed=False)
        return LegacyCatalogReconcileResult(
            status=LegacyCatalogReconcileStatus.NOT_SEALED,
            catalog_id=integrity.catalog_id,
            import_id=integrity.import_id,
            transaction_id=integrity.transaction_id,
        )
    receipt = _reconcile_existing_catalog(connection, projection)
    _exact_target(connection, projection, sealed=True)
    return LegacyCatalogReconcileResult(
        status=LegacyCatalogReconcileStatus.SEALED,
        catalog_id=integrity.catalog_id,
        import_id=integrity.import_id,
        transaction_id=integrity.transaction_id,
        receipt=receipt,
    )


def _reconcile_projected(
    database: SqliteDatabase, projection: _CatalogProjection
) -> LegacyCatalogReconcileResult:
    integrity = projection.integrity
    connection = None
    result: Optional[LegacyCatalogReconcileResult] = None
    try:
        connection, _filesystem = database._open_unvalidated()
        connection.execute("BEGIN")
        database._validate_initialized(connection)
        result = _reconcile_connection(connection, projection)
    except _CatalogConflict as exc:
        return LegacyCatalogReconcileResult(
            status=LegacyCatalogReconcileStatus.CONFLICT,
            catalog_id=integrity.catalog_id,
            import_id=integrity.import_id,
            transaction_id=integrity.transaction_id,
            error_code="sqlite_legacy_catalog_conflict",
            message=str(exc),
        )
    except (_CatalogCorrupt, _ShadowCorrupt) as exc:
        return LegacyCatalogReconcileResult(
            status=LegacyCatalogReconcileStatus.CORRUPT,
            catalog_id=integrity.catalog_id,
            import_id=integrity.import_id,
            transaction_id=integrity.transaction_id,
            error_code="sqlite_legacy_catalog_corrupt",
            message=str(exc),
        )
    except SQLiteKernelError as exc:
        status = (
            LegacyCatalogReconcileStatus.UNKNOWN
            if _unknown_kernel_error(exc.code)
            else LegacyCatalogReconcileStatus.CORRUPT
        )
        return LegacyCatalogReconcileResult(
            status=status,
            catalog_id=integrity.catalog_id,
            import_id=integrity.import_id,
            transaction_id=integrity.transaction_id,
            error_code=exc.code,
            message="kernel_validation_failed",
        )
    except sqlite3.DatabaseError:
        return LegacyCatalogReconcileResult(
            status=LegacyCatalogReconcileStatus.CORRUPT,
            catalog_id=integrity.catalog_id,
            import_id=integrity.import_id,
            transaction_id=integrity.transaction_id,
            error_code="sqlite_legacy_catalog_read_failed",
            message="sqlite_read_failed",
        )
    except Exception:
        return LegacyCatalogReconcileResult(
            status=LegacyCatalogReconcileStatus.UNKNOWN,
            catalog_id=integrity.catalog_id,
            import_id=integrity.import_id,
            transaction_id=integrity.transaction_id,
            error_code="sqlite_legacy_catalog_reconcile_failed",
            message="reconcile_failed",
        )
    finally:
        if connection is not None:
            try:
                if connection.in_transaction:
                    connection.rollback()
            except Exception:
                pass
            try:
                connection.close()
            except Exception:
                pass

    assert result is not None
    if result.status is not LegacyCatalogReconcileStatus.SEALED:
        return result
    try:
        database._finalize_committed_storage()
    except SQLiteKernelError as exc:
        return LegacyCatalogReconcileResult(
            status=LegacyCatalogReconcileStatus.UNKNOWN,
            catalog_id=integrity.catalog_id,
            import_id=integrity.import_id,
            transaction_id=integrity.transaction_id,
            error_code=exc.code,
            message="durability_barrier_failed",
        )
    except Exception:
        return LegacyCatalogReconcileResult(
            status=LegacyCatalogReconcileStatus.UNKNOWN,
            catalog_id=integrity.catalog_id,
            import_id=integrity.import_id,
            transaction_id=integrity.transaction_id,
            error_code="sqlite_legacy_catalog_durability_failed",
            message="durability_barrier_failed",
        )
    return result


def _raise_for_reconcile(result: LegacyCatalogReconcileResult) -> None:
    codes = {
        LegacyCatalogReconcileStatus.CONFLICT: "sqlite_legacy_catalog_conflict",
        LegacyCatalogReconcileStatus.CORRUPT: "sqlite_legacy_catalog_corrupt",
        LegacyCatalogReconcileStatus.UNKNOWN: "sqlite_legacy_catalog_unknown",
    }
    code = codes.get(result.status)
    if code is not None:
        _fail(code, result.error_code or result.status.value)


def _seal_result(
    projection: _CatalogProjection,
    reconciled: LegacyCatalogReconcileResult,
    *,
    already_sealed: bool = False,
    recovered_after_ack_loss: bool = False,
) -> LegacyCatalogSealResult:
    if reconciled.receipt is None:
        _fail("sqlite_legacy_catalog_corrupt", "receipt_missing")
    integrity = projection.integrity
    return LegacyCatalogSealResult(
        catalog_id=integrity.catalog_id,
        import_id=integrity.import_id,
        transaction_id=integrity.transaction_id,
        receipt=reconciled.receipt,
        already_sealed=already_sealed,
        recovered_after_ack_loss=recovered_after_ack_loss,
    )


class SqliteLegacyCatalogSealer:
    """Narrow historical-seal writer bound to one initialized kernel."""

    __slots__ = ("_database",)

    def __init__(self, database: SqliteDatabase, *, _capability: Any = None) -> None:
        if _capability is not _LEGACY_CATALOG_SEAL_CAPABILITY:
            _fail("sqlite_legacy_catalog_forbidden", "adapter_capability_required")
        if type(database) is not SqliteDatabase:
            raise TypeError("database must be an exact SqliteDatabase")
        self._database = database

    def reconcile(self, plan: Any) -> LegacyCatalogReconcileResult:
        return _reconcile_projected(self._database, _project_nonempty(plan))

    def reconcile_empty(self) -> LegacyCatalogReconcileResult:
        return _reconcile_projected(self._database, _project_empty())

    def seal(self, plan: Any) -> LegacyCatalogSealResult:
        return self._seal_projected(_project_nonempty(plan))

    def seal_empty(self) -> LegacyCatalogSealResult:
        return self._seal_projected(_project_empty())

    def _seal_projected(
        self,
        projection: _CatalogProjection,
    ) -> LegacyCatalogSealResult:
        before = _reconcile_projected(self._database, projection)
        if before.status is LegacyCatalogReconcileStatus.SEALED:
            return _seal_result(projection, before, already_sealed=True)
        _raise_for_reconcile(before)
        if before.status is not LegacyCatalogReconcileStatus.NOT_SEALED:
            _fail("sqlite_legacy_catalog_unknown", "unexpected_reconcile_state")

        connection = None
        commit_started = False
        try:
            connection, _filesystem = self._database._open_unvalidated()
            self._database._validate_initialized(connection)
            _execute_closed(connection, "BEGIN IMMEDIATE", ())
            # Repeat validation under the writer snapshot.  The earlier fresh
            # reconcile is an optimization, never a TOCTOU authority.
            self._database._validate_initialized(connection)
            current = _reconcile_connection(connection, projection)
            if current.status is LegacyCatalogReconcileStatus.SEALED:
                connection.rollback()
                connection.close()
                connection = None
                fresh = _reconcile_projected(self._database, projection)
                if fresh.status is not LegacyCatalogReconcileStatus.SEALED:
                    _raise_for_reconcile(fresh)
                    _fail("sqlite_legacy_catalog_unknown", "fresh_reconcile_failed")
                return _seal_result(projection, fresh, already_sealed=True)
            if current.status is not LegacyCatalogReconcileStatus.NOT_SEALED:
                raise _CatalogCorrupt("writer_state_invalid")

            authorizer = _CatalogAuthorizer()
            connection.set_authorizer(authorizer)
            try:
                try:
                    _execute_closed(
                        connection,
                        _INSERT_MARKER_SQL,
                        (
                            SQLITE_CATALOG_STATE_KEY,
                            SQLITE_CATALOG_STATE_HISTORICAL_FENCED,
                        ),
                    )
                    integrity = projection.integrity
                    _execute_closed(
                        connection,
                        _INSERT_RECEIPT_SQL,
                        (integrity.transaction_id, LEGACY_CATALOG_TRANSACTION_KIND),
                    )
                    _execute_closed(connection, _INSERT_SEAL_SQL, _seal_values(projection))
                    _execute_many_closed(
                        connection,
                        _INSERT_MEMBER_SQL,
                        (
                            _member_values(projection, ordinal, project)
                            for ordinal, project in enumerate(projection.projects)
                        ),
                    )
                except sqlite3.DatabaseError as exc:
                    if authorizer.denied:
                        raise SQLiteLegacyCatalogError(
                            "sqlite_legacy_catalog_statement_forbidden",
                            "authorizer_denied",
                        ) from exc
                    raise
            finally:
                # Python 3.9 does not reliably clear with None.  Replacing the
                # callback lets kernel validation execute its owned PRAGMAs.
                connection.set_authorizer(_allow_authorizer)
            if authorizer.denied:
                _fail("sqlite_legacy_catalog_statement_forbidden", "authorizer_denied")
            if not connection.in_transaction:
                _fail("sqlite_legacy_catalog_transaction_escaped", "transaction_missing")
            self._database._validate_initialized(connection)
            commit_started = True
            connection.commit()
            connection.close()
            connection = None
        except BaseException as original_error:
            if connection is not None:
                try:
                    connection.rollback()
                except Exception:
                    pass
                try:
                    connection.close()
                except Exception:
                    pass
            try:
                reconciled = _reconcile_projected(self._database, projection)
            except BaseException as reconcile_error:
                if not isinstance(reconcile_error, Exception):
                    raise reconcile_error
                if not isinstance(original_error, Exception):
                    raise original_error
                _fail("sqlite_legacy_catalog_unknown", "fresh_reconcile_failed")
            if not isinstance(original_error, Exception):
                # Classification completed, but cancellation/process control
                # is never converted into a successful acknowledgement.
                raise original_error
            if reconciled.status is LegacyCatalogReconcileStatus.SEALED:
                return _seal_result(
                    projection,
                    reconciled,
                    recovered_after_ack_loss=commit_started,
                )
            if isinstance(original_error, SQLiteLegacyCatalogError):
                raise original_error
            if isinstance(original_error, _CatalogConflict):
                _fail("sqlite_legacy_catalog_conflict", "writer_conflict")
            if isinstance(original_error, (_CatalogCorrupt, _ShadowCorrupt)):
                _fail("sqlite_legacy_catalog_corrupt", "writer_corrupt")
            if reconciled.status is LegacyCatalogReconcileStatus.NOT_SEALED:
                _fail("sqlite_legacy_catalog_not_committed", "transaction_failed")
            _raise_for_reconcile(reconciled)
            _fail("sqlite_legacy_catalog_unknown", "transaction_indeterminate")

        reconciled = _reconcile_projected(self._database, projection)
        if reconciled.status is not LegacyCatalogReconcileStatus.SEALED:
            _raise_for_reconcile(reconciled)
            _fail("sqlite_legacy_catalog_not_committed", "fresh_reconcile_failed")
        return _seal_result(projection, reconciled)


__all__ = [
    "LegacyCatalogReconcileResult",
    "LegacyCatalogReconcileStatus",
    "LegacyCatalogSealResult",
    "SQLiteLegacyCatalogError",
    "SqliteLegacyCatalogSealer",
]
