"""INSERT-only writer for one non-authoritative v5 repository proof.

The proof closes a strict legacy repository snapshot over the existing v4
catalog seal.  It is durable migration evidence only: authority remains
``legacy`` and no result from this module can claim cutover readiness.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Optional, Sequence

from legacy_repository_contract import (
    LEGACY_REPOSITORY_AUTHORITY,
    LEGACY_REPOSITORY_TRANSACTION_KIND,
)
from sqlite_kernel import KernelReceipt, SQLiteKernelError, SqliteDatabase
from sqlite_legacy_import import _unknown_kernel_error
from sqlite_legacy_repository_exact import (
    RepositoryExactConflict as _RepositoryConflict,
    RepositoryExactCorrupt as _RepositoryCorrupt,
    RepositoryExactProjectionError as _RepositoryProjectionError,
    _RepositoryExactProjection as _RepositoryProjection,
    _require_projection,
    control_values as _control_values,
    member_values as _member_values,
    reconcile_existing_repository as _reconcile_existing_repository,
    reconcile_unsealed_repository as _reconcile_unsealed_repository,
    repository_control as _repository_control,
    seal_values as _seal_values,
)


_LEGACY_REPOSITORY_SEAL_CAPABILITY = object()


class SQLiteLegacyRepositoryError(RuntimeError):
    """Stable, payload-free failure at the repository-proof boundary."""

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
    raise SQLiteLegacyRepositoryError(code, reason)


class LegacyRepositoryReconcileStatus(Enum):
    NOT_SEALED = "not_sealed"
    SEALED = "sealed"
    CONFLICT = "conflict"
    CORRUPT = "corrupt"
    UNKNOWN = "unknown"


class LegacyRepositoryNamespaceStatus(Enum):
    ABSENT = "absent"
    PRESENT = "present"
    CORRUPT = "corrupt"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class LegacyRepositoryNamespaceResult:
    status: LegacyRepositoryNamespaceStatus
    error_code: str = ""
    message: str = ""


@dataclass(frozen=True)
class LegacyRepositoryReconcileResult:
    status: LegacyRepositoryReconcileStatus
    cutover_id: str
    catalog_id: str
    transaction_id: str
    receipt: Optional[KernelReceipt] = None
    error_code: str = ""
    message: str = ""


@dataclass(frozen=True)
class LegacyRepositorySealResult:
    """Durable evidence result; never an authority-cutover token."""

    cutover_id: str
    catalog_id: str
    transaction_id: str
    receipt: KernelReceipt
    already_sealed: bool = False
    recovered_after_ack_loss: bool = False
    authority: str = field(init=False, default=LEGACY_REPOSITORY_AUTHORITY)

    @property
    def cutover_ready(self) -> bool:
        return False


class _RepositoryAuthorizer:
    """Poison a proof transaction after any non-INSERT mutation attempt."""

    _INSERT_TABLES = frozenset(
        (
            "legacy_repository_proof_control",
            "transaction_receipts",
            "legacy_repository_seals",
            "legacy_repository_members",
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


_INSERT_CONTROL_SQL = """
    INSERT INTO legacy_repository_proof_control(
        singleton, authority, authority_state, repository_epoch,
        repository_revision, catalog_revision, cutover_id,
        runtime_generation, code_digest, cutover_ready
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""
_INSERT_RECEIPT_SQL = """
    INSERT INTO transaction_receipts(transaction_id, kind, committed_at)
    VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
"""
_INSERT_SEAL_SQL = """
    INSERT INTO legacy_repository_seals(
        repository_singleton, cutover_id, transaction_id, receipt_kind,
        receipt_protocol, catalog_id, catalog_seal_sha256, proof_protocol,
        manifest_schema, status, selection, repository_epoch,
        repository_revision, catalog_revision, runtime_generation,
        code_digest, project_count, live_project_count,
        deleted_project_count, source_state_bytes, manifest_bytes,
        manifest_sha256, source_repository_sha256,
        catalog_coverage_sha256, proof_sha256, target_schema_version,
        schema_fingerprint, cutover_ready
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
              ?, ?, ?, ?, ?, ?, ?, ?)
"""
_INSERT_MEMBER_SQL = """
    INSERT INTO legacy_repository_members(
        cutover_id, ordinal, project_id, generation, status, operation_id,
        source_state_sha256, source_state_bytes, catalog_id,
        catalog_ordinal, import_id, document_id, document_version,
        storage_version, business_root_sha256, entity_count,
        row_snapshot_bytes
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_FIXED_INSERT_SQL = frozenset(
    (_INSERT_CONTROL_SQL, _INSERT_RECEIPT_SQL, _INSERT_SEAL_SQL, _INSERT_MEMBER_SQL)
)


def _validated_projection(value: Any) -> _RepositoryProjection:
    try:
        return _require_projection(value)
    except _RepositoryProjectionError as exc:
        raise SQLiteLegacyRepositoryError(
            "sqlite_legacy_repository_projection_invalid", exc.code
        ) from exc


def _close_cursor(cursor: sqlite3.Cursor) -> None:
    try:
        cursor.close()
    except sqlite3.Error:
        pass


def _execute_closed(connection: Any, sql: str, values: Sequence[Any]) -> None:
    if sql != "BEGIN IMMEDIATE" and sql not in _FIXED_INSERT_SQL:
        _fail("sqlite_legacy_repository_statement_forbidden", "statement_not_fixed")
    cursor = connection.execute(sql, values)
    _close_cursor(cursor)


def _execute_many_closed(
    connection: Any,
    sql: str,
    values: Iterable[Sequence[Any]],
) -> None:
    if sql not in _FIXED_INSERT_SQL:
        _fail("sqlite_legacy_repository_statement_forbidden", "statement_not_fixed")
    cursor = connection.executemany(sql, values)
    _close_cursor(cursor)


def _result(
    projection: _RepositoryProjection,
    status: LegacyRepositoryReconcileStatus,
    *,
    receipt: Optional[KernelReceipt] = None,
    error_code: str = "",
    message: str = "",
) -> LegacyRepositoryReconcileResult:
    integrity = projection.integrity
    return LegacyRepositoryReconcileResult(
        status=status,
        cutover_id=integrity.cutover_id,
        catalog_id=integrity.catalog_id,
        transaction_id=integrity.transaction_id,
        receipt=receipt,
        error_code=error_code,
        message=message,
    )


def _namespace_result(
    status: LegacyRepositoryNamespaceStatus,
    *,
    error_code: str = "",
    message: str = "",
) -> LegacyRepositoryNamespaceResult:
    return LegacyRepositoryNamespaceResult(
        status=status,
        error_code=error_code,
        message=message,
    )


def _namespace_counts(connection: Any) -> tuple[int, int, int]:
    cursor = connection.execute(
        "SELECT "
        "(SELECT count(*) FROM legacy_repository_proof_control), "
        "(SELECT count(*) FROM legacy_repository_seals), "
        "(SELECT count(*) FROM legacy_repository_members)"
    )
    try:
        first = cursor.fetchone()
        second = cursor.fetchone()
    finally:
        _close_cursor(cursor)
    if first is None or second is not None:
        raise _RepositoryCorrupt("repository_namespace_count_invalid")
    values = tuple(first)
    if len(values) != 3 or any(type(value) is not int for value in values):
        raise _RepositoryCorrupt("repository_namespace_count_invalid")
    return values  # type: ignore[return-value]


def _sqlite_io_indeterminate(exc: sqlite3.DatabaseError) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)
    if type(code) is not int:
        return False
    primary = code & 0xFF
    # SQLite's primary result codes are part of the stable C API.  Some
    # supported Python/sqlite builds do not re-export the symbolic constants,
    # so relying on ``sqlite3.SQLITE_BUSY`` would silently turn every
    # indeterminate storage failure into a false corruption verdict.
    return primary in frozenset((5, 6, 7, 9, 10, 13, 14))


def _probe_namespace(database: SqliteDatabase) -> LegacyRepositoryNamespaceResult:
    """Classify only the v5 namespace after full kernel validation."""

    connection = None
    try:
        connection, _filesystem = database._open_unvalidated()
        connection.execute("BEGIN")
        database._validate_initialized(connection)
        control_count, seal_count, member_count = _namespace_counts(connection)
        if (control_count, seal_count, member_count) == (0, 0, 0):
            return _namespace_result(LegacyRepositoryNamespaceStatus.ABSENT)
        if control_count == 1 and seal_count == 1 and member_count >= 0:
            # Kernel validation has already proved the one-to-one control,
            # seal, reserved receipt and structural member closure.  This is
            # only a routing hint; projection-specific byte equality remains
            # the later reconciler's job.
            return _namespace_result(LegacyRepositoryNamespaceStatus.PRESENT)
        return _namespace_result(
            LegacyRepositoryNamespaceStatus.CORRUPT,
            error_code="sqlite_legacy_repository_namespace_partial",
            message="repository_namespace_partial",
        )
    except _RepositoryCorrupt as exc:
        return _namespace_result(
            LegacyRepositoryNamespaceStatus.CORRUPT,
            error_code="sqlite_legacy_repository_corrupt",
            message=str(exc),
        )
    except SQLiteKernelError as exc:
        status = (
            LegacyRepositoryNamespaceStatus.UNKNOWN
            if _unknown_kernel_error(exc.code)
            else LegacyRepositoryNamespaceStatus.CORRUPT
        )
        return _namespace_result(
            status,
            error_code=exc.code,
            message="kernel_validation_failed",
        )
    except sqlite3.DatabaseError as exc:
        return _namespace_result(
            (
                LegacyRepositoryNamespaceStatus.UNKNOWN
                if _sqlite_io_indeterminate(exc)
                else LegacyRepositoryNamespaceStatus.CORRUPT
            ),
            error_code="sqlite_legacy_repository_namespace_read_failed",
            message="sqlite_read_failed",
        )
    except Exception:
        return _namespace_result(
            LegacyRepositoryNamespaceStatus.UNKNOWN,
            error_code="sqlite_legacy_repository_namespace_probe_failed",
            message="namespace_probe_failed",
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


def _reconcile_connection(
    connection: Any,
    projection: _RepositoryProjection,
) -> LegacyRepositoryReconcileResult:
    control = _repository_control(connection)
    if control is None:
        _reconcile_unsealed_repository(connection, projection)
        return _result(projection, LegacyRepositoryReconcileStatus.NOT_SEALED)
    receipt = _reconcile_existing_repository(connection, projection)
    return _result(
        projection,
        LegacyRepositoryReconcileStatus.SEALED,
        receipt=receipt,
    )


def _reconcile_projected(
    database: SqliteDatabase,
    projection: _RepositoryProjection,
) -> LegacyRepositoryReconcileResult:
    connection = None
    result: Optional[LegacyRepositoryReconcileResult] = None
    try:
        connection, _filesystem = database._open_unvalidated()
        connection.execute("BEGIN")
        database._validate_initialized(connection)
        result = _reconcile_connection(connection, projection)
    except _RepositoryConflict as exc:
        return _result(
            projection,
            LegacyRepositoryReconcileStatus.CONFLICT,
            error_code="sqlite_legacy_repository_conflict",
            message=str(exc),
        )
    except _RepositoryCorrupt as exc:
        return _result(
            projection,
            LegacyRepositoryReconcileStatus.CORRUPT,
            error_code="sqlite_legacy_repository_corrupt",
            message=str(exc),
        )
    except SQLiteKernelError as exc:
        status = (
            LegacyRepositoryReconcileStatus.UNKNOWN
            if _unknown_kernel_error(exc.code)
            else LegacyRepositoryReconcileStatus.CORRUPT
        )
        return _result(
            projection,
            status,
            error_code=exc.code,
            message="kernel_validation_failed",
        )
    except sqlite3.DatabaseError as exc:
        return _result(
            projection,
            (
                LegacyRepositoryReconcileStatus.UNKNOWN
                if _sqlite_io_indeterminate(exc)
                else LegacyRepositoryReconcileStatus.CORRUPT
            ),
            error_code=(
                "sqlite_legacy_repository_read_indeterminate"
                if _sqlite_io_indeterminate(exc)
                else "sqlite_legacy_repository_read_failed"
            ),
            message="sqlite_read_failed",
        )
    except Exception:
        return _result(
            projection,
            LegacyRepositoryReconcileStatus.UNKNOWN,
            error_code="sqlite_legacy_repository_reconcile_failed",
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
    if result.status is not LegacyRepositoryReconcileStatus.SEALED:
        return result
    try:
        database._finalize_committed_storage()
    except SQLiteKernelError as exc:
        return _result(
            projection,
            LegacyRepositoryReconcileStatus.UNKNOWN,
            error_code=exc.code,
            message="durability_barrier_failed",
        )
    except Exception:
        return _result(
            projection,
            LegacyRepositoryReconcileStatus.UNKNOWN,
            error_code="sqlite_legacy_repository_durability_failed",
            message="durability_barrier_failed",
        )
    return result


def _raise_for_reconcile(result: LegacyRepositoryReconcileResult) -> None:
    codes = {
        LegacyRepositoryReconcileStatus.CONFLICT: "sqlite_legacy_repository_conflict",
        LegacyRepositoryReconcileStatus.CORRUPT: "sqlite_legacy_repository_corrupt",
        LegacyRepositoryReconcileStatus.UNKNOWN: "sqlite_legacy_repository_unknown",
    }
    code = codes.get(result.status)
    if code is not None:
        _fail(code, result.error_code or result.status.value)


def _seal_result(
    projection: _RepositoryProjection,
    reconciled: LegacyRepositoryReconcileResult,
    *,
    already_sealed: bool = False,
    recovered_after_ack_loss: bool = False,
) -> LegacyRepositorySealResult:
    if reconciled.receipt is None:
        _fail("sqlite_legacy_repository_corrupt", "receipt_missing")
    integrity = projection.integrity
    return LegacyRepositorySealResult(
        cutover_id=integrity.cutover_id,
        catalog_id=integrity.catalog_id,
        transaction_id=integrity.transaction_id,
        receipt=reconciled.receipt,
        already_sealed=already_sealed,
        recovered_after_ack_loss=recovered_after_ack_loss,
    )


class SqliteLegacyRepositorySealer:
    """Narrow repository-proof writer bound to one initialized kernel."""

    __slots__ = ("_database",)

    def __init__(self, database: SqliteDatabase, *, _capability: Any = None) -> None:
        if _capability is not _LEGACY_REPOSITORY_SEAL_CAPABILITY:
            _fail("sqlite_legacy_repository_forbidden", "adapter_capability_required")
        if type(database) is not SqliteDatabase:
            raise TypeError("database must be an exact SqliteDatabase")
        self._database = database

    def reconcile(self, projection: Any) -> LegacyRepositoryReconcileResult:
        return _reconcile_projected(
            self._database,
            _validated_projection(projection),
        )

    def probe_namespace(self) -> LegacyRepositoryNamespaceResult:
        return _probe_namespace(self._database)

    def seal(self, projection: Any) -> LegacyRepositorySealResult:
        projection = _validated_projection(projection)
        before = _reconcile_projected(self._database, projection)
        if before.status is LegacyRepositoryReconcileStatus.SEALED:
            return _seal_result(projection, before, already_sealed=True)
        _raise_for_reconcile(before)
        if before.status is not LegacyRepositoryReconcileStatus.NOT_SEALED:
            _fail("sqlite_legacy_repository_unknown", "unexpected_reconcile_state")

        connection = None
        commit_started = False
        try:
            connection, _filesystem = self._database._open_unvalidated()
            self._database._validate_initialized(connection)
            _execute_closed(connection, "BEGIN IMMEDIATE", ())
            self._database._validate_initialized(connection)
            current = _reconcile_connection(connection, projection)
            if current.status is LegacyRepositoryReconcileStatus.SEALED:
                connection.rollback()
                connection.close()
                connection = None
                fresh = _reconcile_projected(self._database, projection)
                if fresh.status is not LegacyRepositoryReconcileStatus.SEALED:
                    _raise_for_reconcile(fresh)
                    _fail("sqlite_legacy_repository_unknown", "fresh_reconcile_failed")
                return _seal_result(projection, fresh, already_sealed=True)
            if current.status is not LegacyRepositoryReconcileStatus.NOT_SEALED:
                raise _RepositoryCorrupt("writer_state_invalid")

            authorizer = _RepositoryAuthorizer()
            connection.set_authorizer(authorizer)
            try:
                try:
                    integrity = projection.integrity
                    _execute_closed(
                        connection,
                        _INSERT_CONTROL_SQL,
                        _control_values(projection),
                    )
                    _execute_closed(
                        connection,
                        _INSERT_RECEIPT_SQL,
                        (integrity.transaction_id, LEGACY_REPOSITORY_TRANSACTION_KIND),
                    )
                    _execute_closed(connection, _INSERT_SEAL_SQL, _seal_values(projection))
                    _execute_many_closed(
                        connection,
                        _INSERT_MEMBER_SQL,
                        (
                            _member_values(projection, ordinal, member)
                            for ordinal, member in enumerate(projection.members)
                        ),
                    )
                except sqlite3.DatabaseError as exc:
                    if authorizer.denied:
                        raise SQLiteLegacyRepositoryError(
                            "sqlite_legacy_repository_statement_forbidden",
                            "authorizer_denied",
                        ) from exc
                    raise
            finally:
                connection.set_authorizer(_allow_authorizer)
            if authorizer.denied:
                _fail("sqlite_legacy_repository_statement_forbidden", "authorizer_denied")
            if not connection.in_transaction:
                _fail("sqlite_legacy_repository_transaction_escaped", "transaction_missing")
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
                _fail("sqlite_legacy_repository_unknown", "fresh_reconcile_failed")
            if not isinstance(original_error, Exception):
                raise original_error
            if reconciled.status is LegacyRepositoryReconcileStatus.SEALED:
                return _seal_result(
                    projection,
                    reconciled,
                    recovered_after_ack_loss=commit_started,
                )
            if isinstance(original_error, SQLiteLegacyRepositoryError):
                raise original_error
            if isinstance(original_error, _RepositoryConflict):
                _fail("sqlite_legacy_repository_conflict", "writer_conflict")
            if isinstance(original_error, _RepositoryCorrupt):
                _fail("sqlite_legacy_repository_corrupt", "writer_corrupt")
            if reconciled.status is LegacyRepositoryReconcileStatus.NOT_SEALED:
                _fail("sqlite_legacy_repository_not_committed", "transaction_failed")
            _raise_for_reconcile(reconciled)
            _fail("sqlite_legacy_repository_unknown", "transaction_indeterminate")

        reconciled = _reconcile_projected(self._database, projection)
        if reconciled.status is not LegacyRepositoryReconcileStatus.SEALED:
            _raise_for_reconcile(reconciled)
            _fail("sqlite_legacy_repository_not_committed", "fresh_reconcile_failed")
        return _seal_result(projection, reconciled)


__all__ = [
    "LegacyRepositoryNamespaceResult",
    "LegacyRepositoryNamespaceStatus",
    "LegacyRepositoryReconcileResult",
    "LegacyRepositoryReconcileStatus",
    "LegacyRepositorySealResult",
    "SQLiteLegacyRepositoryError",
    "SqliteLegacyRepositorySealer",
]
