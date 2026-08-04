"""Fail-closed SQLite/WAL transaction kernel for one local ZE repository.

W06.1 deliberately stops below the business persistence layer.  A receipt from
this module proves that *one SQLite transaction* committed; it says nothing
about the legacy Document/OpLog files until a later adapter writes the business
rows and the receipt in this same transaction.

The implementation keeps the safety profile fixed.  The only deployment knob
is the bounded writer wait in ``ZE_SQLITE_BUSY_TIMEOUT_MS``.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import sqlite3
import stat
import subprocess
import threading
import time
import weakref
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional, Sequence, Tuple

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
    SQLITE_CATALOG_STATE_HISTORICAL_FENCED,
    SQLITE_CATALOG_STATE_KEY,
)
from legacy_import_contract import (
    LEGACY_BATCH_PROTOCOL,
    LEGACY_MANIFEST_SCHEMA,
    LEGACY_PAYLOAD_PROTOCOL,
    MAX_LEGACY_ENTITIES,
    MAX_LEGACY_MANIFEST_BYTES,
    MAX_LEGACY_PROJECTS,
    MAX_LEGACY_ROW_SNAPSHOT_BYTES,
    SQLITE_IMPORT_RECEIPT_PROTOCOL,
    SQLITE_IMPORT_STATE_KEY,
    SQLITE_IMPORT_STATE_TERMINAL_SHADOW,
    SQLITE_IMPORT_TERMINAL_STATUS,
    SQLITE_IMPORT_TRANSACTION_KIND,
    SQLITE_IMPORT_TRANSACTION_PREFIX,
)
from legacy_repository_contract import (
    LEGACY_REPOSITORY_AUTHORITY,
    LEGACY_REPOSITORY_AUTHORITY_STATE,
    LEGACY_REPOSITORY_CUTOVER_READY,
    LEGACY_REPOSITORY_LEGACY_ADOPT_OPERATION_ID,
    LEGACY_REPOSITORY_MANIFEST_SCHEMA,
    LEGACY_REPOSITORY_MEMBER_DELETED,
    LEGACY_REPOSITORY_MEMBER_LIVE,
    LEGACY_REPOSITORY_PROOF_PROTOCOL,
    LEGACY_REPOSITORY_RECEIPT_PROTOCOL,
    LEGACY_REPOSITORY_SELECTION,
    LEGACY_REPOSITORY_STATUS,
    LEGACY_REPOSITORY_TARGET_SCHEMA_VERSION,
    LEGACY_REPOSITORY_TRANSACTION_KIND,
    LEGACY_REPOSITORY_TRANSACTION_PREFIX,
    MAX_LEGACY_REPOSITORY_MANIFEST_BYTES,
    MAX_LEGACY_REPOSITORY_PROJECTS,
    MAX_LEGACY_REPOSITORY_PROJECT_SOURCE_STATE_BYTES,
    MAX_LEGACY_REPOSITORY_RUNTIME_GENERATION_BYTES,
    MAX_LEGACY_REPOSITORY_SOURCE_STATE_BYTES,
)
from sqlite_schema import (
    BUSINESS_SNAPSHOT_MIGRATION,
    BUSINESS_TABLES,
    BUSINESS_TRANSACTION_KIND,
    BUSINESS_TRANSACTION_MIGRATION,
    BUSINESS_TRANSACTION_PREFIX,
    BUSINESS_TRANSACTION_TABLES,
    CATALOG_LEDGER_TABLES,
    CUTOVER_INSTALL_CLOSURE_TABLES,
    DIRECT_PREDECESSOR_BINDING_TABLES,
    IMPORT_LEDGER_TABLES,
    INSTALL_CLOSURE_MIGRATION,
    KERNEL_MIGRATION,
    LEGACY_CATALOG_SEAL_MIGRATION,
    LEGACY_IMPORT_LEDGER_MIGRATION,
    LEGACY_REPOSITORY_PROOF_MIGRATION,
    LOCAL_REVISION_MIGRATION,
    LOCAL_REVISION_PREFLIGHT_PREDICATE,
    MIGRATIONS,
    PRIVATE_TABLES,
    PRODUCTION_AUTHORITY_WORKER_MIGRATION,
    PRODUCTION_RUNTIME_TABLES,
    PRODUCTION_RUNTIME_V8_TABLES,
    REPOSITORY_PROOF_TABLES,
    RUNTIME_SCHEMA_HARDENING_MIGRATION,
    WORKER_PAYLOAD_PROJECTION_MIGRATION,
    WORKER_PROJECTION_SIDECAR_TABLES,
    Migration,
    legacy_normalize_schema_sql,
    normalize_schema_sql,
)


APPLICATION_ID = 0x5A455633  # ASCII-ish "ZEV3", within SQLite's signed range.
BUSY_TIMEOUT_ENV = "ZE_SQLITE_BUSY_TIMEOUT_MS"
DEFAULT_BUSY_TIMEOUT_MS = 5_000
MIN_BUSY_TIMEOUT_MS = 1
MAX_BUSY_TIMEOUT_MS = 60_000
MIN_SQLITE_VERSION = (3, 37, 0)  # STRICT tables used by schema migration v2.

_IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_COMMITTED_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")

# W06.1 exposed this module-level name before the registry was split out.
INITIAL_MIGRATION = KERNEL_MIGRATION


class SQLiteKernelError(RuntimeError):
    """Stable, machine-readable failure from the kernel boundary."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SQLiteSettings:
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS

    def __post_init__(self) -> None:
        value = self.busy_timeout_ms
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < MIN_BUSY_TIMEOUT_MS
            or value > MAX_BUSY_TIMEOUT_MS
        ):
            raise SQLiteKernelError(
                "sqlite_busy_timeout_invalid",
                "busy_timeout_ms must be an integer between %d and %d"
                % (MIN_BUSY_TIMEOUT_MS, MAX_BUSY_TIMEOUT_MS),
            )

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "SQLiteSettings":
        source = os.environ if env is None else env
        raw = source.get(BUSY_TIMEOUT_ENV)
        if raw is None:
            return cls()
        if (
            not isinstance(raw, str)
            or not raw
            or raw.strip() != raw
            or not raw.isdigit()
            or str(int(raw, 10)) != raw
        ):
            raise SQLiteKernelError(
                "sqlite_busy_timeout_invalid",
                "%s must be a canonical positive integer" % BUSY_TIMEOUT_ENV,
            )
        return cls(busy_timeout_ms=int(raw, 10))


@dataclass(frozen=True)
class FilesystemInfo:
    filesystem: str
    mount_point: str
    is_local: bool
    reason: str = ""


FilesystemProbe = Callable[[Path], FilesystemInfo]


@dataclass(frozen=True)
class KernelReceipt:
    transaction_id: str
    kind: str
    committed_at: str


@dataclass(frozen=True)
class KernelCommitResult:
    receipt: KernelReceipt
    value: Any = None
    recovered_after_ack_loss: bool = False


class ReconcileStatus(Enum):
    COMMITTED = "committed"
    NOT_COMMITTED = "not_committed"
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True)
class ReconcileResult:
    status: ReconcileStatus
    receipt: Optional[KernelReceipt] = None
    error_code: str = ""
    message: str = ""


class CrashPoint(Enum):
    """Test-only hook points; never parse these values from external input."""

    BEFORE_COMMIT = "before_commit"
    AFTER_COMMIT_BEFORE_ACK = "after_commit_before_ack"


@dataclass(frozen=True)
class ConnectionProfile:
    encoding: str
    journal_mode: str
    synchronous: int
    foreign_keys: int
    trusted_schema: int
    busy_timeout_ms: int
    application_id: int
    user_version: int
    sqlite_version: str
    filesystem: FilesystemInfo


class _OwnedConnection:
    """A connection that rejects use after fork or from another thread."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._pid = os.getpid()
        self._thread_id = threading.get_ident()
        self._closed = False

    def _assert_owner(self) -> None:
        if self._closed:
            raise SQLiteKernelError("sqlite_connection_closed", "SQLite connection is closed")
        if os.getpid() != self._pid or threading.get_ident() != self._thread_id:
            raise SQLiteKernelError(
                "sqlite_connection_owner_mismatch",
                "SQLite connection cannot cross a process or thread boundary",
            )

    def execute(self, sql: str, parameters: Sequence[Any] = ()) -> sqlite3.Cursor:
        self._assert_owner()
        return self._connection.execute(sql, parameters)

    def executemany(
        self, sql: str, parameters: Sequence[Sequence[Any]]
    ) -> sqlite3.Cursor:
        self._assert_owner()
        return self._connection.executemany(sql, parameters)

    def set_authorizer(self, callback: Optional[Callable[..., int]]) -> None:
        self._assert_owner()
        self._connection.set_authorizer(callback)

    def set_progress_handler(
        self,
        callback: Optional[Callable[[], int]],
        instruction_interval: int,
    ) -> None:
        self._assert_owner()
        self._connection.set_progress_handler(callback, instruction_interval)

    @property
    def in_transaction(self) -> bool:
        self._assert_owner()
        return bool(self._connection.in_transaction)

    def commit(self) -> None:
        self._assert_owner()
        self._connection.commit()

    def rollback(self) -> None:
        self._assert_owner()
        self._connection.rollback()

    def close(self) -> None:
        self._assert_owner()
        self._connection.close()
        self._closed = True


@dataclass(frozen=True)
class StatementResult:
    """Materialized SQL result with no cursor/connection capability."""

    rows: Tuple[Any, ...]
    rowcount: int
    lastrowid: Optional[int]

    def fetchone(self) -> Optional[Any]:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> Tuple[Any, ...]:
        return self.rows


class UnitOfWork:
    """Opaque callback handle: SQL only, no stored connection capability."""

    __slots__ = ("__weakref__",)

    def __init__(self) -> None:
        raise TypeError("UnitOfWork handles are issued only by SqliteDatabase.commit")

    def execute(self, sql: str, parameters: Sequence[Any] = ()) -> StatementResult:
        return _run_uow_statement(self, sql, parameters, False)

    def executemany(
        self, sql: str, parameters: Sequence[Sequence[Any]]
    ) -> StatementResult:
        return _run_uow_statement(self, sql, parameters, True)


def _build_uow_ledger():
    """Keep connection state in closure cells and never return a cursor/connection."""

    lock = threading.RLock()
    connections: weakref.WeakKeyDictionary[UnitOfWork, _OwnedConnection] = (
        weakref.WeakKeyDictionary()
    )

    def issue(connection: _OwnedConnection) -> UnitOfWork:
        handle = object.__new__(UnitOfWork)
        with lock:
            connections[handle] = connection
        return handle

    def run_statement(
        handle: UnitOfWork,
        sql: str,
        parameters: Any,
        many: bool,
    ) -> StatementResult:
        if type(handle) is not UnitOfWork:
            raise SQLiteKernelError(
                "sqlite_uow_capability_lost", "unit-of-work handle is not current"
            )
        with lock:
            connection = connections.get(handle)
        if connection is None:
            raise SQLiteKernelError(
                "sqlite_uow_capability_lost", "unit-of-work handle is no longer active"
            )
        cursor = (
            connection.executemany(sql, parameters)
            if many
            else connection.execute(sql, parameters)
        )
        try:
            rows = tuple(cursor.fetchall()) if cursor.description is not None else ()
            return StatementResult(
                rows=rows,
                rowcount=cursor.rowcount,
                lastrowid=cursor.lastrowid,
            )
        finally:
            cursor.close()

    def revoke(handle: UnitOfWork) -> None:
        with lock:
            connections.pop(handle, None)

    def after_fork_child() -> None:
        nonlocal lock, connections
        # Never touch an inherited lock: another parent thread may have held
        # it at fork.  Child handles/connections are invalid in every case.
        lock = threading.RLock()
        connections = weakref.WeakKeyDictionary()

    @contextmanager
    def hold_lock_for_test() -> Iterator[None]:
        with lock:
            yield

    return issue, run_statement, revoke, after_fork_child, hold_lock_for_test


(
    _issue_uow,
    _run_uow_statement,
    _revoke_uow,
    _reset_uow_after_fork,
    _hold_uow_ledger_lock_for_test,
) = _build_uow_ledger()
if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_uow_after_fork)


class _UnitOfWorkAuthorizer:
    def __init__(self) -> None:
        self.denied = ""

    def __call__(
        self,
        action: int,
        arg1: Optional[str],
        arg2: Optional[str],
        database_name: Optional[str],
        trigger_name: Optional[str],
    ) -> int:
        del database_name, trigger_name
        forbidden_actions = {
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
        }
        if action in forbidden_actions:
            self.denied = "transaction, schema, PRAGMA and ATTACH controls are kernel-owned"
            return sqlite3.SQLITE_DENY

        write_actions = {
            getattr(sqlite3, name)
            for name in ("SQLITE_INSERT", "SQLITE_UPDATE", "SQLITE_DELETE")
            if hasattr(sqlite3, name)
        }
        table_name = (arg1 or arg2 or "").lower()
        if (
            action == getattr(sqlite3, "SQLITE_READ", -1)
            and table_name in PRIVATE_TABLES
        ):
            self.denied = "kernel table %s is not readable by a unit of work" % table_name
            return sqlite3.SQLITE_DENY
        if action in write_actions and table_name in PRIVATE_TABLES:
            self.denied = "kernel table %s is not writable by a unit of work" % table_name
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK


def _allow_authorizer(*args: Any) -> int:
    del args
    return sqlite3.SQLITE_OK


class SqliteDatabase:
    """Owns initialization, fixed-profile connections and receipt reconcile."""

    def __init__(
        self,
        path: Any,
        *,
        settings: Optional[SQLiteSettings] = None,
        _test_filesystem_probe: Optional[FilesystemProbe] = None,
    ) -> None:
        expanded = os.path.expanduser(os.fspath(path))
        if not expanded or expanded == ":memory:" or expanded.startswith("file:"):
            raise SQLiteKernelError(
                "sqlite_path_unsafe", "SQLite kernel requires a normal filesystem path"
            )
        self.path = Path(os.path.abspath(expanded))
        self.settings = settings or SQLiteSettings.from_env()
        # The override is deliberately an underscored test seam: filesystem
        # locality is a safety decision, not a downstream configuration hook.
        self._filesystem_probe = _test_filesystem_probe or probe_local_filesystem

    def initialize(self) -> ConnectionProfile:
        """Create or migrate the kernel under one exclusive transaction."""
        # PRAGMA journal_mode cannot be changed inside the migration transaction
        # and can fail immediately when several pristine connections race.  A
        # separate crash-safe bootstrap lease serializes only initialization;
        # all normal writers remain coordinated by SQLite itself.
        self._prepare_storage(allow_create=True)
        with self._initialization_lock():
            connection, filesystem = self._open_unvalidated(allow_create=True)
            try:
                connection.execute("BEGIN EXCLUSIVE")
                application_id = _pragma_int(connection, "application_id")
                user_version = _pragma_int(connection, "user_version")
                objects = _schema_rows(connection)

                if application_id == 0:
                    if user_version != 0 or objects:
                        raise SQLiteKernelError(
                            "sqlite_application_id_missing",
                            "non-empty database has no ZE application_id",
                        )
                elif application_id != APPLICATION_ID:
                    raise SQLiteKernelError(
                        "sqlite_application_id_mismatch",
                        "database belongs to another application",
                    )

                if user_version < 0 or user_version > MIGRATIONS[-1].version:
                    raise SQLiteKernelError(
                        "sqlite_schema_version_unsupported",
                        "database user_version %d is not supported" % user_version,
                    )

                if user_version == 0:
                    if objects:
                        raise SQLiteKernelError(
                            "sqlite_schema_drift", "version zero database must be empty"
                        )
                    self._apply_migrations(connection, from_version=0)
                else:
                    # Never let an upgrade overwrite evidence of an already
                    # drifted lower schema.  Validate that exact version first;
                    # only then may the next migration change DDL/metadata.
                    self._verify_schema(connection, user_version)
                    self._verify_repository_marker(connection)
                    self._apply_migrations(connection, from_version=user_version)

                connection.execute("PRAGMA application_id = %d" % APPLICATION_ID)
                connection.execute("PRAGMA user_version = %d" % MIGRATIONS[-1].version)
                self._verify_schema(connection)
                self._verify_repository_marker(connection)
                self._verify_inactive_persistence_state(connection)
                connection.commit()
            except BaseException:
                _rollback_quietly(connection)
                _close_quietly(connection)
                raise
            else:
                connection.close()

        self._finalize_committed_storage()
        return self.inspect_profile(filesystem_hint=filesystem)

    def inspect_profile(
        self, *, filesystem_hint: Optional[FilesystemInfo] = None
    ) -> ConnectionProfile:
        connection, filesystem = self._open_unvalidated()
        try:
            connection.execute("BEGIN")
            self._validate_initialized(connection)
            profile = _read_profile(
                connection,
                self.settings.busy_timeout_ms,
                filesystem_hint or filesystem,
            )
        finally:
            if connection.in_transaction:
                connection.rollback()
            connection.close()
        self._validate_storage_files()
        return profile

    def commit(
        self,
        transaction_id: str,
        kind: str,
        callback: Callable[[UnitOfWork], Any],
        *,
        _test_crash_hook: Optional[Callable[[CrashPoint], None]] = None,
    ) -> KernelCommitResult:
        """Commit callback work and one receipt atomically.

        A repeated transaction id is a conflict, even when ``kind`` matches.
        W06.1 has no payload digest and therefore cannot infer idempotency.
        """
        _validate_identity("transaction_id", transaction_id)
        _validate_identity("kind", kind)
        if (
            transaction_id.startswith(SQLITE_IMPORT_TRANSACTION_PREFIX)
            or transaction_id.startswith(LEGACY_CATALOG_TRANSACTION_PREFIX)
            or transaction_id.startswith(LEGACY_REPOSITORY_TRANSACTION_PREFIX)
            or transaction_id.startswith(BUSINESS_TRANSACTION_PREFIX)
            or kind == SQLITE_IMPORT_TRANSACTION_KIND
            or kind == LEGACY_CATALOG_TRANSACTION_KIND
            or kind == LEGACY_REPOSITORY_TRANSACTION_KIND
            or kind == BUSINESS_TRANSACTION_KIND
        ):
            raise SQLiteKernelError(
                "sqlite_transaction_namespace_reserved",
                "legacy evidence transaction identities are adapter-owned",
            )
        if not callable(callback):
            raise TypeError("callback must be callable")

        connection, _filesystem = self._open_unvalidated()
        value: Any = None
        receipt: Optional[KernelReceipt] = None
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._validate_initialized(connection)
            marker = connection.execute(
                "SELECT EXISTS(SELECT 1 FROM kernel_metadata WHERE key IN (?, ?))",
                (SQLITE_IMPORT_STATE_KEY, SQLITE_CATALOG_STATE_KEY),
            ).fetchone()
            if marker is None or int(marker[0]) != 0:
                raise SQLiteKernelError(
                    "sqlite_terminal_shadow_read_only",
                    "generic unit-of-work commits are closed after terminal evidence",
                )
            existing = _fetch_receipt(connection, transaction_id)
            if existing is not None:
                raise SQLiteKernelError(
                    "sqlite_transaction_id_conflict",
                    "transaction_id already has a committed receipt",
                )

            schema_version = _pragma_int(connection, "schema_version")
            authorizer = _UnitOfWorkAuthorizer()
            connection.set_authorizer(authorizer)
            unit_of_work = _issue_uow(connection)
            try:
                value = callback(unit_of_work)
            except sqlite3.DatabaseError as exc:
                if authorizer.denied:
                    raise SQLiteKernelError(
                        "sqlite_uow_statement_forbidden", authorizer.denied
                    ) from exc
                raise
            finally:
                _revoke_uow(unit_of_work)
                connection.set_authorizer(_allow_authorizer)

            # A callback may catch sqlite3's "not authorized" exception.  The
            # denied attempt still poisons the whole UoW; otherwise it could
            # return normally and mint a receipt for forbidden work.
            if authorizer.denied:
                raise SQLiteKernelError(
                    "sqlite_uow_statement_forbidden", authorizer.denied
                )
            if not connection.in_transaction:
                raise SQLiteKernelError(
                    "sqlite_uow_transaction_escaped",
                    "unit of work ended the kernel-owned transaction",
                )
            if _pragma_int(connection, "schema_version") != schema_version:
                raise SQLiteKernelError(
                    "sqlite_uow_schema_changed",
                    "unit of work changed the kernel schema",
                )
            self._validate_initialized(connection)

            connection.execute(
                """
                INSERT INTO transaction_receipts(transaction_id, kind, committed_at)
                VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                """,
                (transaction_id, kind),
            )
            receipt = _fetch_receipt(connection, transaction_id)
            if receipt is None:
                raise SQLiteKernelError(
                    "sqlite_receipt_missing", "receipt insert did not become visible"
                )
            if _test_crash_hook is not None:
                _test_crash_hook(CrashPoint.BEFORE_COMMIT)
        except BaseException as transaction_error:
            _rollback_quietly(connection)
            _close_quietly(connection)
            if isinstance(transaction_error, sqlite3.Error):
                message = str(transaction_error)
                lowered = message.lower()
                code = (
                    "sqlite_busy"
                    if "locked" in lowered or "busy" in lowered
                    else "sqlite_transaction_failed"
                )
                raise SQLiteKernelError(code, message) from transaction_error
            raise

        try:
            connection.commit()
        except BaseException as commit_error:
            _close_quietly(connection)
            return self._recover_post_commit_error(
                transaction_id, value, commit_error
            )

        try:
            if _test_crash_hook is not None:
                _test_crash_hook(CrashPoint.AFTER_COMMIT_BEFORE_ACK)
            connection.close()
            self._finalize_committed_storage()
        except BaseException as post_commit_error:
            _close_quietly(connection)
            return self._recover_post_commit_error(
                transaction_id, value, post_commit_error
            )
        assert receipt is not None
        return KernelCommitResult(receipt=receipt, value=value)

    def _recover_post_commit_error(
        self,
        transaction_id: str,
        value: Any,
        original_error: BaseException,
    ) -> KernelCommitResult:
        """Freshly classify every error edge after COMMIT may have started."""

        try:
            reconciled = self.reconcile(transaction_id)
        except BaseException as reconcile_error:
            # A second cancellation can prevent classification itself.  Never
            # replace or swallow either process-control signal.
            if not isinstance(reconcile_error, Exception):
                raise reconcile_error
            if not isinstance(original_error, Exception):
                raise original_error
            raise SQLiteKernelError(
                "sqlite_commit_indeterminate",
                "post-COMMIT failure and fresh reconcile could not complete",
            ) from original_error
        if not isinstance(original_error, Exception):
            # Classification (and the positive durability barrier) completed;
            # cancellation/process control must still propagate in every state.
            raise original_error
        if (
            reconciled.status is ReconcileStatus.COMMITTED
            and reconciled.receipt is not None
        ):
            return KernelCommitResult(
                receipt=reconciled.receipt,
                value=value,
                recovered_after_ack_loss=True,
            )
        if reconciled.status is ReconcileStatus.NOT_COMMITTED:
            raise SQLiteKernelError(
                "sqlite_transaction_not_committed",
                "post-COMMIT failure and a fresh connection found no receipt",
            ) from original_error
        raise SQLiteKernelError(
            "sqlite_commit_indeterminate",
            "post-COMMIT failure and fresh reconcile could not prove the outcome",
        ) from original_error

    def reconcile(self, transaction_id: str) -> ReconcileResult:
        """Read a receipt through a new connection; never reuses callback state."""
        _validate_identity("transaction_id", transaction_id)
        if (
            transaction_id.startswith(SQLITE_IMPORT_TRANSACTION_PREFIX)
            or transaction_id.startswith(LEGACY_CATALOG_TRANSACTION_PREFIX)
            or transaction_id.startswith(LEGACY_REPOSITORY_TRANSACTION_PREFIX)
            or transaction_id.startswith(BUSINESS_TRANSACTION_PREFIX)
        ):
            # A generic receipt proves only transaction existence.  Reserved
            # adapters must reconcile exact payload/descriptor/head equality.
            return ReconcileResult(
                status=ReconcileStatus.INDETERMINATE,
                error_code="sqlite_transaction_namespace_reserved",
                message="private receipts require adapter-specific reconcile",
            )
        try:
            connection, _filesystem = self._open_unvalidated()
            try:
                connection.execute("BEGIN")
                self._validate_initialized(connection)
                receipt = _fetch_receipt(connection, transaction_id)
            finally:
                if connection.in_transaction:
                    connection.rollback()
                connection.close()
            if receipt is None:
                self._validate_storage_files()
            else:
                # A visible receipt is not enough to upgrade an earlier
                # post-COMMIT directory-fsync failure into COMMITTED.  Every
                # positive reconcile repeats the same barrier as writers.
                self._finalize_committed_storage()
        except Exception as exc:
            return ReconcileResult(
                status=ReconcileStatus.INDETERMINATE,
                error_code=getattr(exc, "code", "sqlite_reconcile_failed"),
                message=str(exc),
            )
        if receipt is None:
            return ReconcileResult(status=ReconcileStatus.NOT_COMMITTED)
        return ReconcileResult(status=ReconcileStatus.COMMITTED, receipt=receipt)

    @contextmanager
    def _owned_connection_for_test(self) -> Iterator[_OwnedConnection]:
        """Narrow test seam for PID/thread ownership evidence."""
        connection, _filesystem = self._open_unvalidated()
        try:
            self._validate_initialized(connection)
            yield connection
        finally:
            if os.getpid() == connection._pid and not connection._closed:
                connection.close()

    def _open_unvalidated(
        self, *, allow_create: bool = False
    ) -> Tuple[_OwnedConnection, FilesystemInfo]:
        filesystem = self._prepare_storage(allow_create=allow_create)
        try:
            target = str(self.path) if allow_create else self.path.as_uri() + "?mode=rw"
            raw = sqlite3.connect(
                target,
                timeout=self.settings.busy_timeout_ms / 1000.0,
                isolation_level=None,
                check_same_thread=True,
                uri=not allow_create,
            )
        except sqlite3.Error as exc:
            raise SQLiteKernelError("sqlite_open_failed", str(exc)) from exc
        raw.row_factory = sqlite3.Row
        try:
            _preflight_database_identity(raw)
        except BaseException:
            raw.close()
            raise
        connection = _OwnedConnection(raw)
        try:
            _configure_connection(connection, self.settings.busy_timeout_ms)
        except BaseException:
            _close_quietly(connection)
            raise
        return connection, filesystem

    def _open_readonly_unvalidated(self) -> Tuple[_OwnedConnection, FilesystemInfo]:
        """Open an existing database without normalizing persistent storage."""

        filesystem = self._validate_readonly_storage()
        try:
            raw = sqlite3.connect(
                self.path.as_uri() + "?mode=ro",
                timeout=self.settings.busy_timeout_ms / 1000.0,
                isolation_level=None,
                check_same_thread=True,
                uri=True,
            )
        except sqlite3.Error as exc:
            raise SQLiteKernelError("sqlite_open_failed", str(exc)) from exc
        raw.row_factory = sqlite3.Row
        try:
            _preflight_database_identity(raw)
        except BaseException:
            raw.close()
            raise
        connection = _OwnedConnection(raw)
        try:
            _configure_readonly_connection(
                connection,
                self.settings.busy_timeout_ms,
            )
        except BaseException:
            _close_quietly(connection)
            raise
        return connection, filesystem

    def _validate_readonly_storage(self) -> FilesystemInfo:
        return _inspect_readonly_storage(self)

    def _apply_migrations(self, connection: _OwnedConnection, from_version: int) -> None:
        applied = False
        for migration in MIGRATIONS:
            if migration.version <= from_version:
                continue
            applied = True
            if migration.version != _pragma_int(connection, "user_version") + 1:
                raise SQLiteKernelError(
                    "sqlite_migration_sequence_invalid", "migration versions must be contiguous"
                )
            if migration.version == LEGACY_IMPORT_LEDGER_MIGRATION.version:
                self._verify_legacy_import_migration_preconditions(connection)
            if migration.version == BUSINESS_TRANSACTION_MIGRATION.version:
                self._verify_business_transaction_migration_preconditions(connection)
            if migration.version == LOCAL_REVISION_MIGRATION.version:
                self._verify_local_revision_migration_preconditions(connection)
            for statement in migration.statements:
                # executescript() performs implicit transaction control; never use it here.
                connection.execute(statement)
            connection.execute(
                """
                INSERT INTO schema_migrations(version, name, checksum, applied_at)
                VALUES (?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                """,
                (migration.version, migration.name, migration.checksum),
            )
            if migration.version == 1:
                connection.execute(
                    """
                    INSERT INTO kernel_repository(singleton, authority, created_at)
                    VALUES (1, 'legacy', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                    """
                )
            connection.execute("PRAGMA user_version = %d" % migration.version)

        # A healthy reopen is verification-only.  Silently rewriting these
        # values would hide operator tampering instead of reporting drift.
        if not applied:
            return
        fingerprint = _expected_schema_fingerprint()
        connection.execute(
            """
            INSERT INTO kernel_metadata(key, value) VALUES ('schema_fingerprint', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (fingerprint,),
        )
        connection.execute(
            """
            INSERT INTO kernel_metadata(key, value) VALUES ('kernel_schema_version', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (str(MIGRATIONS[-1].version),),
        )

    def _verify_migration_ledger(
        self, connection: _OwnedConnection, user_version: int
    ) -> None:
        try:
            rows = connection.execute(
                "SELECT version, name, checksum, applied_at "
                "FROM schema_migrations ORDER BY version"
            ).fetchall()
        except sqlite3.Error as exc:
            raise SQLiteKernelError(
                "sqlite_migration_ledger_missing", "migration ledger is missing or unreadable"
            ) from exc
        expected = [migration for migration in MIGRATIONS if migration.version <= user_version]
        actual = [(row["version"], row["name"], row["checksum"]) for row in rows]
        wanted = [(item.version, item.name, item.checksum) for item in expected]
        if actual != wanted:
            raise SQLiteKernelError(
                "sqlite_migration_checksum_mismatch", "migration ledger differs from code"
            )
        if any(
            not isinstance(row["applied_at"], str)
            or _COMMITTED_AT_RE.fullmatch(row["applied_at"]) is None
            for row in rows
        ):
            raise SQLiteKernelError(
                "sqlite_migration_ledger_corrupt",
                "migration applied_at must be canonical UTC",
            )

    def _verify_schema(
        self,
        connection: _OwnedConnection,
        schema_version: Optional[int] = None,
    ) -> None:
        version = MIGRATIONS[-1].version if schema_version is None else schema_version
        self._verify_migration_ledger(connection, version)
        actual = _schema_fingerprint(connection, version)
        expected = _expected_schema_fingerprint(version)
        if actual != expected:
            raise SQLiteKernelError(
                "sqlite_schema_drift", "sqlite_schema differs from the immutable migration"
            )
        rows = connection.execute(
            "SELECT key, value FROM kernel_metadata WHERE key IN (?, ?)",
            ("schema_fingerprint", "kernel_schema_version"),
        ).fetchall()
        metadata = {row["key"]: row["value"] for row in rows}
        if metadata != {
            "schema_fingerprint": expected,
            "kernel_schema_version": str(version),
        }:
            raise SQLiteKernelError(
                "sqlite_kernel_metadata_drift", "kernel metadata does not match the schema"
            )

    def _verify_repository_marker(self, connection: _OwnedConnection) -> None:
        rows = connection.execute(
            "SELECT singleton, authority, created_at "
            "FROM kernel_repository ORDER BY singleton"
        ).fetchall()
        if [(row["singleton"], row["authority"]) for row in rows] != [(1, "legacy")]:
            raise SQLiteKernelError(
                "sqlite_authority_marker_invalid",
                "W06.1 requires exactly one authority=legacy repository marker",
            )
        created_at = rows[0]["created_at"]
        if not isinstance(created_at, str) or _COMMITTED_AT_RE.fullmatch(created_at) is None:
            raise SQLiteKernelError(
                "sqlite_authority_marker_invalid",
                "repository marker created_at must be canonical UTC",
            )

    def _validate_initialized(self, connection: _OwnedConnection) -> None:
        # Validation spans schema, marker, batches, receipts, descriptors and
        # business coverage.  In autocommit mode each SELECT would otherwise
        # observe a different WAL snapshot while a disjoint import commits,
        # producing a false corruption verdict from a mixed old/new view.
        owns_snapshot = not connection.in_transaction
        if owns_snapshot:
            connection.execute("BEGIN")
        try:
            if _pragma_int(connection, "application_id") != APPLICATION_ID:
                raise SQLiteKernelError(
                    "sqlite_application_id_mismatch",
                    "database is not an initialized ZE kernel",
                )
            if _pragma_int(connection, "user_version") != MIGRATIONS[-1].version:
                raise SQLiteKernelError(
                    "sqlite_schema_version_unsupported",
                    "database schema version is not current",
                )
            self._verify_schema(connection)
            self._verify_repository_marker(connection)
            self._verify_inactive_persistence_state(connection)
        finally:
            if owns_snapshot and connection.in_transaction:
                connection.rollback()

    def _verify_legacy_import_migration_preconditions(
        self, connection: _OwnedConnection
    ) -> None:
        """Never bless dirty v2 state as a valid import-ledger database."""
        if _pragma_int(connection, "user_version") != (
            LEGACY_IMPORT_LEDGER_MIGRATION.version - 1
        ):
            raise SQLiteKernelError(
                "sqlite_legacy_import_migration_precondition_failed",
                "legacy import ledger requires the exact preceding schema",
            )
        for table in BUSINESS_TABLES:
            row = connection.execute(
                "SELECT EXISTS(SELECT 1 FROM %s LIMIT 1)" % table
            ).fetchone()
            if row is None or int(row[0]) != 0:
                raise SQLiteKernelError(
                    "sqlite_legacy_import_migration_dirty",
                    "legacy import ledger requires empty business tables",
                )
        if _reserved_import_receipt_exists(connection):
            raise SQLiteKernelError(
                "sqlite_legacy_import_migration_dirty",
                "legacy import receipt namespace must be empty before migration",
            )

    def _verify_inactive_persistence_state(
        self, connection: _OwnedConnection
    ) -> None:
        self._verify_legacy_evidence_state(connection)
        if MIGRATIONS[-1].version >= LEGACY_REPOSITORY_PROOF_MIGRATION.version:
            self._verify_repository_proof_state(connection)
        if MIGRATIONS[-1].version >= BUSINESS_TRANSACTION_MIGRATION.version:
            self._verify_inactive_business_transaction_state(connection)
        if MIGRATIONS[-1].version >= PRODUCTION_AUTHORITY_WORKER_MIGRATION.version:
            self._verify_inactive_production_runtime_state(connection)

    def _verify_business_transaction_migration_preconditions(
        self, connection: _OwnedConnection
    ) -> None:
        """Never bless a v5 generic receipt as a future exact business commit."""
        if _pragma_int(connection, "user_version") != (
            BUSINESS_TRANSACTION_MIGRATION.version - 1
        ):
            raise SQLiteKernelError(
                "sqlite_business_migration_precondition_failed",
                "business transaction substrate requires the exact preceding schema",
            )
        if _reserved_business_receipt_exists(connection):
            raise SQLiteKernelError(
                "sqlite_business_migration_dirty",
                "business transaction receipt namespace must be empty before migration",
            )

    def _verify_local_revision_migration_preconditions(
        self, connection: _OwnedConnection
    ) -> None:
        """Only upgrade empty v6 or its one closed global=local stream."""

        if _pragma_int(connection, "user_version") != (
            LOCAL_REVISION_MIGRATION.version - 1
        ):
            raise SQLiteKernelError(
                "sqlite_local_revision_migration_precondition_failed",
                "local revision separation requires the exact v6 schema",
            )
        row = connection.execute(
            "SELECT CASE WHEN " + LOCAL_REVISION_PREFLIGHT_PREDICATE +
            " THEN 1 ELSE 0 END"
        ).fetchone()
        if row is None or int(row[0]) != 1:
            raise SQLiteKernelError(
                "sqlite_local_revision_migration_precondition_failed",
                "v6 business rows are not one closed global=local stream",
            )

    def _verify_inactive_business_transaction_state(
        self, connection: _OwnedConnection
    ) -> None:
        """Schema-v6 is an unmounted substrate until the cutover coordinator exists."""
        for table in BUSINESS_TRANSACTION_TABLES:
            row = connection.execute(
                "SELECT EXISTS(SELECT 1 FROM %s LIMIT 1)" % table
            ).fetchone()
            if row is None or int(row[0]) != 0:
                raise SQLiteKernelError(
                    "sqlite_business_state_unexpected",
                    "unmounted business transaction tables must remain empty",
                )
        if _reserved_business_receipt_exists(connection):
            raise SQLiteKernelError(
                "sqlite_business_state_unexpected",
                "unmounted business transaction receipt namespace must remain empty",
            )

    def _verify_inactive_production_runtime_state(
        self, connection: _OwnedConnection
    ) -> None:
        """Keep every installed writer-owned table inert below its runtime.

        Historical registry probes deliberately replace ``MIGRATIONS`` with
        the immutable v8 prefix.  They must inspect only the 25 tables that
        existed in that prefix; querying the v9 sidecars in a real v8
        database would turn the old-binary fence into a false corruption
        report.
        """

        if MIGRATIONS[-1].version >= INSTALL_CLOSURE_MIGRATION.version:
            tables = PRODUCTION_RUNTIME_TABLES
        elif MIGRATIONS[-1].version >= WORKER_PAYLOAD_PROJECTION_MIGRATION.version:
            tables = (
                *PRODUCTION_RUNTIME_V8_TABLES,
                *WORKER_PROJECTION_SIDECAR_TABLES,
            )
        else:
            tables = PRODUCTION_RUNTIME_V8_TABLES
        for table in tables:
            row = connection.execute(
                "SELECT EXISTS(SELECT 1 FROM %s LIMIT 1)" % table
            ).fetchone()
            if row is None or int(row[0]) != 0:
                raise SQLiteKernelError(
                    "sqlite_production_runtime_state_unexpected",
                    "inactive production runtime tables must remain empty",
                )

    def _verify_legacy_evidence_state(
        self, connection: _OwnedConnection
    ) -> None:
        """Accept exactly one of the four closed persistence evidence states.

        Import and catalog markers are independent state discriminators.  Their
        four combinations mean EMPTY, TERMINAL_IMPORT, HISTORICAL_EMPTY and
        HISTORICAL_NONEMPTY respectively.  A catalog seal is historical
        evidence only; every state keeps ``authority=legacy``.

        This verifier intentionally never reads snapshot/manifest payloads and
        never recomputes the potentially 1 GiB virtual payload digest.  The
        importer-specific fresh reconcile owns cryptographic equality with its
        immutable plan.  Global kernel opens prove the relational closure only.
        """
        if MIGRATIONS[-1].version < LEGACY_IMPORT_LEDGER_MIGRATION.version:
            self._verify_inert_business_tables(connection)
            return

        import_marker = connection.execute(
            "SELECT value FROM kernel_metadata WHERE key = ?",
            (SQLITE_IMPORT_STATE_KEY,),
        ).fetchone()
        if import_marker is not None and (
            type(import_marker["value"]) is not str
            or import_marker["value"] != SQLITE_IMPORT_STATE_TERMINAL_SHADOW
        ):
            raise SQLiteKernelError(
                "sqlite_import_state_invalid",
                "legacy import state marker is not a supported closed state",
            )

        # Historical v3 tests deliberately run a shortened migration registry.
        # Preserve the exact v3 state machine until the catalog tables exist.
        if MIGRATIONS[-1].version < LEGACY_CATALOG_SEAL_MIGRATION.version:
            if import_marker is None:
                self._verify_empty_persistence_state(connection)
            else:
                self._verify_terminal_import_shadow(connection)
            return

        catalog_marker = connection.execute(
            "SELECT value FROM kernel_metadata WHERE key = ?",
            (SQLITE_CATALOG_STATE_KEY,),
        ).fetchone()
        if catalog_marker is not None and (
            type(catalog_marker["value"]) is not str
            or catalog_marker["value"] != SQLITE_CATALOG_STATE_HISTORICAL_FENCED
        ):
            raise SQLiteKernelError(
                "sqlite_catalog_state_invalid",
                "legacy catalog state marker is not a supported historical state",
            )

        if import_marker is None and catalog_marker is None:
            self._verify_empty_persistence_state(connection)
            return
        if import_marker is not None and catalog_marker is None:
            self._verify_terminal_import_shadow(connection)
            self._verify_catalog_ledger_empty(connection)
            return
        if import_marker is None:
            self._verify_empty_import_state(connection)
            self._verify_historical_catalog_seal(connection, expect_empty=True)
            return

        self._verify_historical_catalog_seal(connection, expect_empty=False)
        self._verify_terminal_import_shadow(connection)

    def _verify_empty_persistence_state(self, connection: _OwnedConnection) -> None:
        # Preserve the established fail-closed codes and precedence for dirty
        # pre-import databases: business rows are reported before ledger rows.
        self._verify_empty_import_state(connection)
        if MIGRATIONS[-1].version >= LEGACY_CATALOG_SEAL_MIGRATION.version:
            self._verify_catalog_ledger_empty(connection)

    def _verify_empty_import_state(self, connection: _OwnedConnection) -> None:
        """Require no business/import rows or reserved import receipt."""
        self._verify_inert_business_tables(connection)
        for table in IMPORT_LEDGER_TABLES:
            row = connection.execute(
                "SELECT EXISTS(SELECT 1 FROM %s LIMIT 1)" % table
            ).fetchone()
            if row is None or int(row[0]) != 0:
                raise SQLiteKernelError(
                    "sqlite_import_state_unexpected",
                    "inactive legacy import ledger tables must remain empty",
                )
        if _reserved_import_receipt_exists(connection):
            raise SQLiteKernelError(
                "sqlite_import_state_unexpected",
                "inactive legacy import receipt namespace must remain empty",
            )

    def _verify_catalog_ledger_empty(self, connection: _OwnedConnection) -> None:
        """Require no catalog rows or catalog-reserved receipt."""
        for table in CATALOG_LEDGER_TABLES:
            row = connection.execute(
                "SELECT EXISTS(SELECT 1 FROM %s LIMIT 1)" % table
            ).fetchone()
            if row is None or int(row[0]) != 0:
                raise SQLiteKernelError(
                    "sqlite_catalog_state_unexpected",
                    "inactive legacy catalog ledger tables must remain empty",
                )
        if _reserved_catalog_receipt_exists(connection):
            raise SQLiteKernelError(
                "sqlite_catalog_state_unexpected",
                "inactive legacy catalog receipt namespace must remain empty",
            )

    def _verify_repository_proof_state(self, connection: _OwnedConnection) -> None:
        """Accept an empty v5 substrate or one closed, legacy-authority proof.

        This global verifier is intentionally structural.  It reads small
        identities/digests and BLOB lengths, never manifest/snapshot payloads.
        The future proof-specific reconciler must own cryptographic equality
        with the strict source snapshot before any authority CAS is designed.
        """

        control_rows = connection.execute(
            """
            SELECT
                singleton, authority, authority_state, repository_epoch,
                repository_revision, catalog_revision, cutover_id,
                runtime_generation,
                length(CAST(runtime_generation AS BLOB)) AS runtime_bytes,
                code_digest, typeof(code_digest) AS code_digest_type,
                length(code_digest) AS code_digest_length, cutover_ready
            FROM legacy_repository_proof_control
            ORDER BY singleton
            """
        ).fetchall()
        if not control_rows:
            self._verify_repository_proof_ledger_empty(connection)
            return
        if len(control_rows) != 1:
            self._raise_repository_state_invalid(
                "repository proof must have exactly one control row"
            )
        control = control_rows[0]
        if (
            control["singleton"] != 1
            or type(control["authority"]) is not str
            or control["authority"] != LEGACY_REPOSITORY_AUTHORITY
            or type(control["authority_state"]) is not str
            or control["authority_state"] != LEGACY_REPOSITORY_AUTHORITY_STATE
            or not _is_lower_hex64(control["repository_epoch"])
            or type(control["repository_revision"]) is not int
            or control["repository_revision"] < 1
            or type(control["catalog_revision"]) is not int
            or control["catalog_revision"] != control["repository_revision"]
            or not _is_lower_hex64(control["cutover_id"])
            or type(control["runtime_generation"]) is not str
            or type(control["runtime_bytes"]) is not int
            or not 1
            <= control["runtime_bytes"]
            <= MAX_LEGACY_REPOSITORY_RUNTIME_GENERATION_BYTES
            or control["code_digest_type"] != "blob"
            or control["code_digest_length"] != 32
            or type(control["cutover_ready"]) is not int
            or control["cutover_ready"] != LEGACY_REPOSITORY_CUTOVER_READY
        ):
            self._raise_repository_state_invalid(
                "repository control row has an invalid non-authoritative shape"
            )

        seal_rows = connection.execute(
            """
            SELECT
                repository_singleton, cutover_id, transaction_id,
                receipt_kind, receipt_protocol, catalog_id,
                catalog_seal_sha256,
                typeof(catalog_seal_sha256) AS catalog_digest_type,
                length(catalog_seal_sha256) AS catalog_digest_length,
                proof_protocol, manifest_schema, status, selection,
                repository_epoch, repository_revision, catalog_revision,
                runtime_generation,
                length(CAST(runtime_generation AS BLOB)) AS runtime_bytes,
                code_digest, typeof(code_digest) AS code_digest_type,
                length(code_digest) AS code_digest_length,
                project_count, live_project_count, deleted_project_count,
                source_state_bytes,
                typeof(manifest_bytes) AS manifest_type,
                length(manifest_bytes) AS manifest_length,
                typeof(manifest_sha256) AS manifest_digest_type,
                length(manifest_sha256) AS manifest_digest_length,
                typeof(source_repository_sha256) AS source_digest_type,
                length(source_repository_sha256) AS source_digest_length,
                typeof(catalog_coverage_sha256) AS coverage_digest_type,
                length(catalog_coverage_sha256) AS coverage_digest_length,
                typeof(proof_sha256) AS proof_digest_type,
                length(proof_sha256) AS proof_digest_length,
                target_schema_version, schema_fingerprint, cutover_ready
            FROM legacy_repository_seals
            ORDER BY repository_singleton
            """
        ).fetchall()
        if len(seal_rows) != 1:
            self._raise_repository_state_invalid(
                "repository proof must have exactly one seal"
            )
        seal = seal_rows[0]
        cutover_id = control["cutover_id"]
        transaction_id = seal["transaction_id"]
        if (
            seal["repository_singleton"] != 1
            or seal["cutover_id"] != cutover_id
            or type(transaction_id) is not str
            or transaction_id
            != LEGACY_REPOSITORY_TRANSACTION_PREFIX + cutover_id
            or type(seal["receipt_kind"]) is not str
            or seal["receipt_kind"] != LEGACY_REPOSITORY_TRANSACTION_KIND
            or type(seal["receipt_protocol"]) is not str
            or seal["receipt_protocol"] != LEGACY_REPOSITORY_RECEIPT_PROTOCOL
            or not _is_lower_hex64(seal["catalog_id"])
            or seal["catalog_digest_type"] != "blob"
            or seal["catalog_digest_length"] != 32
            or type(seal["proof_protocol"]) is not str
            or seal["proof_protocol"] != LEGACY_REPOSITORY_PROOF_PROTOCOL
            or type(seal["manifest_schema"]) is not str
            or seal["manifest_schema"] != LEGACY_REPOSITORY_MANIFEST_SCHEMA
            or type(seal["status"]) is not str
            or seal["status"] != LEGACY_REPOSITORY_STATUS
            or type(seal["selection"]) is not str
            or seal["selection"] != LEGACY_REPOSITORY_SELECTION
            or seal["repository_epoch"] != control["repository_epoch"]
            or seal["repository_revision"] != control["repository_revision"]
            or seal["catalog_revision"] != control["catalog_revision"]
            or seal["runtime_generation"] != control["runtime_generation"]
            or seal["runtime_bytes"] != control["runtime_bytes"]
            or seal["code_digest_type"] != "blob"
            or seal["code_digest_length"] != 32
            or seal["code_digest"] != control["code_digest"]
            or type(seal["project_count"]) is not int
            or not 0
            <= seal["project_count"]
            <= MAX_LEGACY_REPOSITORY_PROJECTS
            or type(seal["live_project_count"]) is not int
            or not 0
            <= seal["live_project_count"]
            <= MAX_LEGACY_REPOSITORY_PROJECTS
            or type(seal["deleted_project_count"]) is not int
            or not 0
            <= seal["deleted_project_count"]
            <= MAX_LEGACY_REPOSITORY_PROJECTS
            or seal["project_count"]
            != seal["live_project_count"] + seal["deleted_project_count"]
            or type(seal["source_state_bytes"]) is not int
            or not 0
            <= seal["source_state_bytes"]
            <= MAX_LEGACY_REPOSITORY_SOURCE_STATE_BYTES
            or seal["manifest_type"] != "blob"
            or type(seal["manifest_length"]) is not int
            or not 1
            <= seal["manifest_length"]
            <= MAX_LEGACY_REPOSITORY_MANIFEST_BYTES
            or seal["manifest_digest_type"] != "blob"
            or seal["manifest_digest_length"] != 32
            or seal["source_digest_type"] != "blob"
            or seal["source_digest_length"] != 32
            or seal["coverage_digest_type"] != "blob"
            or seal["coverage_digest_length"] != 32
            or seal["proof_digest_type"] != "blob"
            or seal["proof_digest_length"] != 32
            or seal["target_schema_version"]
            != LEGACY_REPOSITORY_TARGET_SCHEMA_VERSION
            or seal["schema_fingerprint"]
            != _expected_schema_fingerprint(
                LEGACY_REPOSITORY_TARGET_SCHEMA_VERSION
            )
            or seal["cutover_ready"] != LEGACY_REPOSITORY_CUTOVER_READY
        ):
            self._raise_repository_state_invalid(
                "repository proof seal has an invalid shape"
            )

        catalog = connection.execute(
            """
            SELECT catalog_id, seal_sha256, project_count
            FROM legacy_catalog_seals
            """
        ).fetchone()
        if (
            catalog is None
            or catalog["catalog_id"] != seal["catalog_id"]
            or catalog["seal_sha256"] != seal["catalog_seal_sha256"]
            or catalog["project_count"] != seal["live_project_count"]
        ):
            self._raise_repository_state_invalid(
                "repository proof does not exactly reference the v4 catalog"
            )

        receipt_rows = connection.execute(
            """
            SELECT transaction_id, kind, committed_at
            FROM transaction_receipts
            WHERE substr(CAST(transaction_id AS TEXT), 1, ?) = ?
               OR CAST(kind AS TEXT) = ?
            ORDER BY transaction_id
            """,
            (
                len(LEGACY_REPOSITORY_TRANSACTION_PREFIX),
                LEGACY_REPOSITORY_TRANSACTION_PREFIX,
                LEGACY_REPOSITORY_TRANSACTION_KIND,
            ),
        ).fetchall()
        if len(receipt_rows) != 1:
            self._raise_repository_state_invalid(
                "repository proof receipt namespace is not one-to-one"
            )
        receipt = receipt_rows[0]
        if (
            receipt["transaction_id"] != transaction_id
            or receipt["kind"] != LEGACY_REPOSITORY_TRANSACTION_KIND
            or type(receipt["committed_at"]) is not str
            or _COMMITTED_AT_RE.fullmatch(receipt["committed_at"]) is None
        ):
            self._raise_repository_state_invalid(
                "repository proof receipt is not canonical"
            )

        member_rows = connection.execute(
            """
            SELECT
                cutover_id, ordinal, project_id, generation, status,
                operation_id, source_state_sha256,
                typeof(source_state_sha256) AS source_digest_type,
                length(source_state_sha256) AS source_digest_length,
                source_state_bytes, catalog_id, catalog_ordinal, import_id,
                document_id, document_version, storage_version,
                typeof(business_root_sha256) AS root_digest_type,
                length(business_root_sha256) AS root_digest_length,
                entity_count, row_snapshot_bytes
            FROM legacy_repository_members
            ORDER BY ordinal, project_id
            """
        ).fetchall()
        project_ids: list[str] = []
        live_by_catalog_ordinal: dict[int, str] = {}
        live_count = 0
        deleted_count = 0
        source_state_bytes = 0
        empty_digest = hashlib.sha256(b"").digest()
        for ordinal, member in enumerate(member_rows):
            project_id = member["project_id"]
            status = member["status"]
            operation_id = member["operation_id"]
            if (
                member["cutover_id"] != cutover_id
                or member["ordinal"] != ordinal
                or type(project_id) is not str
                or not 1 <= len(project_id) <= 160
                or type(member["generation"]) is not int
                or member["generation"] < 1
                or status
                not in (
                    LEGACY_REPOSITORY_MEMBER_LIVE,
                    LEGACY_REPOSITORY_MEMBER_DELETED,
                )
                or not _repository_operation_id_valid(
                    operation_id,
                    allow_legacy_adopt=(status == LEGACY_REPOSITORY_MEMBER_LIVE),
                    generation=member["generation"],
                )
                or member["source_digest_type"] != "blob"
                or member["source_digest_length"] != 32
                or type(member["source_state_bytes"]) is not int
                or not 0
                <= member["source_state_bytes"]
                <= MAX_LEGACY_REPOSITORY_PROJECT_SOURCE_STATE_BYTES
            ):
                self._raise_repository_state_invalid(
                    "repository proof member has an invalid shape"
                )
            live_fields = (
                member["catalog_id"],
                member["catalog_ordinal"],
                member["import_id"],
                member["document_id"],
                member["document_version"],
                member["storage_version"],
                member["entity_count"],
                member["row_snapshot_bytes"],
            )
            if status == LEGACY_REPOSITORY_MEMBER_DELETED:
                if (
                    member["source_state_bytes"] != 0
                    or member["source_state_sha256"] != empty_digest
                    or member["root_digest_type"] != "null"
                    or member["root_digest_length"] is not None
                    or any(value is not None for value in live_fields)
                ):
                    self._raise_repository_state_invalid(
                        "deleted repository member retains a live reference"
                    )
                deleted_count += 1
            else:
                catalog_ordinal = member["catalog_ordinal"]
                if (
                    member["source_state_bytes"] < 1
                    or member["catalog_id"] != seal["catalog_id"]
                    or type(catalog_ordinal) is not int
                    or not 0
                    <= catalog_ordinal
                    < MAX_LEGACY_REPOSITORY_PROJECTS
                    or not _is_lower_hex64(member["import_id"])
                    or type(member["document_id"]) is not str
                    or not member["document_id"]
                    or type(member["document_version"]) is not int
                    or member["document_version"] < 1
                    or member["storage_version"] not in ("legacy", "v2")
                    or member["root_digest_type"] != "blob"
                    or member["root_digest_length"] != 32
                    or type(member["entity_count"]) is not int
                    or not 0 <= member["entity_count"] <= MAX_LEGACY_ENTITIES
                    or type(member["row_snapshot_bytes"]) is not int
                    or member["row_snapshot_bytes"] < 1
                    or catalog_ordinal in live_by_catalog_ordinal
                ):
                    self._raise_repository_state_invalid(
                        "live repository member is not a closed catalog reference"
                    )
                live_by_catalog_ordinal[catalog_ordinal] = project_id
                live_count += 1
            project_ids.append(project_id)
            source_state_bytes += member["source_state_bytes"]

        catalog_members = connection.execute(
            """
            SELECT ordinal, project_id
            FROM legacy_catalog_members
            ORDER BY ordinal
            """
        ).fetchall()
        catalog_identity = {
            row["ordinal"]: row["project_id"] for row in catalog_members
        }
        if (
            len(member_rows) != seal["project_count"]
            or project_ids != sorted(project_ids)
            or len(set(project_ids)) != len(project_ids)
            or live_count != seal["live_project_count"]
            or deleted_count != seal["deleted_project_count"]
            or source_state_bytes != seal["source_state_bytes"]
            or live_by_catalog_ordinal != catalog_identity
        ):
            self._raise_repository_state_invalid(
                "repository proof members do not close the full repository state"
            )

    def _verify_repository_proof_ledger_empty(
        self, connection: _OwnedConnection
    ) -> None:
        for table in REPOSITORY_PROOF_TABLES:
            row = connection.execute(
                "SELECT EXISTS(SELECT 1 FROM %s LIMIT 1)" % table
            ).fetchone()
            if row is None or int(row[0]) != 0:
                raise SQLiteKernelError(
                    "sqlite_repository_state_unexpected",
                    "inactive repository proof tables must remain empty",
                )
        if _reserved_repository_receipt_exists(connection):
            raise SQLiteKernelError(
                "sqlite_repository_state_unexpected",
                "inactive repository proof receipt namespace must remain empty",
            )

    def _verify_terminal_import_shadow(self, connection: _OwnedConnection) -> None:
        """Verify relational closure without reading snapshot/manifest BLOBs.

        This is intentionally deep rather than constant-time: current schema
        v3 has no authenticated aggregate summary row.  Callers run it inside
        one WAL read/write snapshot so growth cannot create a mixed-view false
        corruption result.  A later hot production authority needs a schema
        migration that separates fast admission from scheduled deep scrub.
        """
        foreign_key_violation = connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchone()
        if foreign_key_violation is not None:
            raise SQLiteKernelError(
                "sqlite_import_state_invalid",
                "terminal legacy import shadow has a foreign-key violation",
            )

        batch_rows = connection.execute(
            """
            SELECT
                import_id, transaction_id, receipt_kind, receipt_protocol,
                status, manifest_schema, batch_protocol, payload_protocol,
                selection, project_count, entity_count, row_snapshot_bytes,
                framed_payload_bytes,
                typeof(manifest_bytes) AS manifest_type,
                length(manifest_bytes) AS manifest_length,
                typeof(manifest_sha256) AS manifest_digest_type,
                length(manifest_sha256) AS manifest_digest_length,
                typeof(payload_sha256) AS payload_digest_type,
                length(payload_sha256) AS payload_digest_length,
                typeof(batch_sha256) AS batch_digest_type,
                length(batch_sha256) AS batch_digest_length
            FROM legacy_import_batches
            ORDER BY import_id
            """
        ).fetchall()
        if not batch_rows:
            self._raise_import_state_invalid(
                "terminal legacy import shadow has no terminal batch"
            )

        batches: dict[str, sqlite3.Row] = {}
        batch_receipts: set[tuple[str, str]] = set()
        for row in batch_rows:
            import_id = row["import_id"]
            transaction_id = row["transaction_id"]
            receipt_kind = row["receipt_kind"]
            if (
                type(import_id) is not str
                or len(import_id) != 64
                or any(character not in "0123456789abcdef" for character in import_id)
                or type(transaction_id) is not str
                or transaction_id != SQLITE_IMPORT_TRANSACTION_PREFIX + import_id
                or type(receipt_kind) is not str
                or receipt_kind != SQLITE_IMPORT_TRANSACTION_KIND
                or type(row["receipt_protocol"]) is not str
                or row["receipt_protocol"] != SQLITE_IMPORT_RECEIPT_PROTOCOL
                or type(row["status"]) is not str
                or row["status"] != SQLITE_IMPORT_TERMINAL_STATUS
                or type(row["manifest_schema"]) is not str
                or row["manifest_schema"] != LEGACY_MANIFEST_SCHEMA
                or type(row["batch_protocol"]) is not str
                or row["batch_protocol"] != LEGACY_BATCH_PROTOCOL
                or type(row["payload_protocol"]) is not str
                or row["payload_protocol"] != LEGACY_PAYLOAD_PROTOCOL
                or type(row["selection"]) is not str
                or row["selection"] != "explicit_projects"
                or type(row["project_count"]) is not int
                or not 1 <= row["project_count"] <= MAX_LEGACY_PROJECTS
                or type(row["entity_count"]) is not int
                or not 0 <= row["entity_count"] <= MAX_LEGACY_ENTITIES
                or type(row["row_snapshot_bytes"]) is not int
                or not 1
                <= row["row_snapshot_bytes"]
                <= MAX_LEGACY_ROW_SNAPSHOT_BYTES
                or type(row["framed_payload_bytes"]) is not int
                or row["framed_payload_bytes"] <= row["row_snapshot_bytes"]
                or row["manifest_type"] != "blob"
                or type(row["manifest_length"]) is not int
                or not 1
                <= row["manifest_length"]
                <= MAX_LEGACY_MANIFEST_BYTES
                or row["manifest_digest_type"] != "blob"
                or row["manifest_digest_length"] != 32
                or row["payload_digest_type"] != "blob"
                or row["payload_digest_length"] != 32
                or row["batch_digest_type"] != "blob"
                or row["batch_digest_length"] != 32
            ):
                self._raise_import_state_invalid(
                    "terminal legacy import batch has an invalid shape"
                )
            batches[import_id] = row
            batch_receipts.add((transaction_id, receipt_kind))

        receipt_rows = connection.execute(
            """
            SELECT transaction_id, kind, committed_at
            FROM transaction_receipts
            WHERE substr(CAST(transaction_id AS TEXT), 1, ?) = ?
               OR CAST(kind AS TEXT) = ?
            ORDER BY transaction_id
            """,
            (
                len(SQLITE_IMPORT_TRANSACTION_PREFIX),
                SQLITE_IMPORT_TRANSACTION_PREFIX,
                SQLITE_IMPORT_TRANSACTION_KIND,
            ),
        ).fetchall()
        reserved_receipts: set[tuple[str, str]] = set()
        for row in receipt_rows:
            transaction_id = row["transaction_id"]
            kind = row["kind"]
            committed_at = row["committed_at"]
            if (
                type(transaction_id) is not str
                or type(kind) is not str
                or type(committed_at) is not str
                or not transaction_id.startswith(SQLITE_IMPORT_TRANSACTION_PREFIX)
                or kind != SQLITE_IMPORT_TRANSACTION_KIND
                or _COMMITTED_AT_RE.fullmatch(committed_at) is None
            ):
                self._raise_import_state_invalid(
                    "terminal legacy import receipt namespace is not closed"
                )
            reserved_receipts.add((transaction_id, kind))
        if reserved_receipts != batch_receipts:
            self._raise_import_state_invalid(
                "terminal legacy import batches and receipts are not one-to-one"
            )

        descriptor_rows = connection.execute(
            """
            SELECT
                import_id, ordinal, project_id, generation, document_id,
                document_version, storage_version,
                typeof(source_head_sha256) AS source_digest_type,
                length(source_head_sha256) AS source_digest_length,
                source_head_bytes, business_root_sha256,
                typeof(business_root_sha256) AS root_digest_type,
                length(business_root_sha256) AS root_digest_length,
                entity_count, row_snapshot_bytes
            FROM legacy_import_projects
            ORDER BY import_id, ordinal
            """
        ).fetchall()
        descriptors_by_batch: dict[str, list[sqlite3.Row]] = {
            import_id: [] for import_id in batches
        }
        descriptor_by_project: dict[str, sqlite3.Row] = {}
        descriptor_by_document: dict[str, sqlite3.Row] = {}
        for row in descriptor_rows:
            import_id = row["import_id"]
            project_id = row["project_id"]
            document_id = row["document_id"]
            if (
                type(import_id) is not str
                or import_id not in batches
                or type(row["ordinal"]) is not int
                or not 0 <= row["ordinal"] < MAX_LEGACY_PROJECTS
                or type(project_id) is not str
                or not 1 <= len(project_id) <= 160
                or type(row["generation"]) is not int
                or row["generation"] < 1
                or type(document_id) is not str
                or not document_id
                or type(row["document_version"]) is not int
                or row["document_version"] < 1
                or type(row["storage_version"]) is not str
                or row["storage_version"] not in ("legacy", "v2")
                or row["source_digest_type"] != "blob"
                or row["source_digest_length"] != 32
                or type(row["source_head_bytes"]) is not int
                or row["source_head_bytes"] < 1
                or row["root_digest_type"] != "blob"
                or row["root_digest_length"] != 32
                or type(row["entity_count"]) is not int
                or not 0 <= row["entity_count"] <= MAX_LEGACY_ENTITIES
                or type(row["row_snapshot_bytes"]) is not int
                or not 1
                <= row["row_snapshot_bytes"]
                <= MAX_LEGACY_ROW_SNAPSHOT_BYTES
                or project_id in descriptor_by_project
                or document_id in descriptor_by_document
            ):
                self._raise_import_state_invalid(
                    "terminal legacy import project descriptor is not closed"
                )
            descriptors_by_batch[import_id].append(row)
            descriptor_by_project[project_id] = row
            descriptor_by_document[document_id] = row

        for import_id, batch in batches.items():
            descriptors = descriptors_by_batch[import_id]
            if (
                len(descriptors) != batch["project_count"]
                or [row["ordinal"] for row in descriptors]
                != list(range(len(descriptors)))
                or [row["project_id"] for row in descriptors]
                != sorted(row["project_id"] for row in descriptors)
                or sum(row["entity_count"] for row in descriptors)
                != batch["entity_count"]
                or sum(row["row_snapshot_bytes"] for row in descriptors)
                != batch["row_snapshot_bytes"]
            ):
                self._raise_import_state_invalid(
                    "terminal legacy import descriptor aggregate is inconsistent"
                )

        project_rows = connection.execute(
            """
            SELECT
                project_id, generation, snapshot_sha256,
                typeof(snapshot_sha256) AS digest_type,
                length(snapshot_sha256) AS digest_length,
                length(snapshot_bytes) AS snapshot_length
            FROM projects
            ORDER BY project_id
            """
        ).fetchall()
        document_rows = connection.execute(
            """
            SELECT project_id, document_id, document_version,
                   length(snapshot_bytes) AS snapshot_length
            FROM documents
            ORDER BY project_id
            """
        ).fetchall()
        entity_groups = connection.execute(
            """
            SELECT project_id, document_id, count(*) AS entity_count,
                   sum(length(snapshot_bytes)) AS snapshot_bytes
            FROM entities
            GROUP BY project_id, document_id
            ORDER BY project_id, document_id
            """
        ).fetchall()

        projects = {row["project_id"]: row for row in project_rows}
        documents = {row["document_id"]: row for row in document_rows}
        entities = {
            (row["project_id"], row["document_id"]): row
            for row in entity_groups
        }
        if (
            len(projects) != len(project_rows)
            or len(documents) != len(document_rows)
            or set(projects) != set(descriptor_by_project)
            or set(documents) != set(descriptor_by_document)
        ):
            self._raise_import_state_invalid(
                "terminal legacy import business rows are not fully covered"
            )

        descriptor_pairs = {
            (row["project_id"], row["document_id"])
            for row in descriptor_by_project.values()
        }
        if not set(entities).issubset(descriptor_pairs):
            self._raise_import_state_invalid(
                "terminal legacy import entity rows are not fully covered"
            )

        for project_id, descriptor in descriptor_by_project.items():
            project = projects[project_id]
            document = documents.get(descriptor["document_id"])
            entity_group = entities.get(
                (project_id, descriptor["document_id"])
            )
            entity_count = 0 if entity_group is None else entity_group["entity_count"]
            entity_bytes = 0 if entity_group is None else entity_group["snapshot_bytes"]
            if (
                document is None
                or type(project["generation"]) is not int
                or project["generation"] != descriptor["generation"]
                or project["digest_type"] != "blob"
                or project["digest_length"] != 32
                or project["snapshot_sha256"] != descriptor["business_root_sha256"]
                or document["project_id"] != project_id
                or document["document_id"] != descriptor["document_id"]
                or type(document["document_version"]) is not int
                or document["document_version"] != descriptor["document_version"]
                or type(project["snapshot_length"]) is not int
                or project["snapshot_length"] < 1
                or type(document["snapshot_length"]) is not int
                or document["snapshot_length"] < 1
                or type(entity_count) is not int
                or entity_count != descriptor["entity_count"]
                or type(entity_bytes) is not int
                or project["snapshot_length"]
                + document["snapshot_length"]
                + entity_bytes
                != descriptor["row_snapshot_bytes"]
            ):
                self._raise_import_state_invalid(
                    "terminal legacy import business graph is inconsistent"
                )

    def _verify_historical_catalog_seal(
        self, connection: _OwnedConnection, *, expect_empty: bool
    ) -> None:
        """Verify one historical catalog seal's relational closure.

        Large manifest and snapshot BLOBs are never selected.  The global
        verifier checks only their SQLite types/lengths and the relational
        coverage graph; the catalog-specific reconciler owns cryptographic
        equality with its immutable source plan.
        """
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            self._raise_catalog_state_invalid(
                "historical legacy catalog has a foreign-key violation"
            )

        seal_rows = connection.execute(
            """
            SELECT
                repository_singleton, catalog_id, transaction_id,
                receipt_kind, receipt_protocol, import_id, seal_protocol,
                manifest_schema, status, selection, project_count,
                entity_count, source_head_bytes, row_snapshot_bytes,
                typeof(manifest_bytes) AS manifest_type,
                length(manifest_bytes) AS manifest_length,
                typeof(manifest_sha256) AS manifest_digest_type,
                length(manifest_sha256) AS manifest_digest_length,
                typeof(source_catalog_sha256) AS source_digest_type,
                length(source_catalog_sha256) AS source_digest_length,
                typeof(target_coverage_sha256) AS target_digest_type,
                length(target_coverage_sha256) AS target_digest_length,
                typeof(seal_sha256) AS seal_digest_type,
                length(seal_sha256) AS seal_digest_length,
                target_schema_version, schema_fingerprint, cutover_ready
            FROM legacy_catalog_seals
            ORDER BY repository_singleton
            """
        ).fetchall()
        if len(seal_rows) != 1:
            self._raise_catalog_state_invalid(
                "historical legacy catalog must have exactly one seal"
            )
        seal = seal_rows[0]
        catalog_id = seal["catalog_id"]
        transaction_id = seal["transaction_id"]
        import_id = seal["import_id"]
        if (
            seal["repository_singleton"] != 1
            or not _is_lower_hex64(catalog_id)
            or type(transaction_id) is not str
            or transaction_id != LEGACY_CATALOG_TRANSACTION_PREFIX + catalog_id
            or type(seal["receipt_kind"]) is not str
            or seal["receipt_kind"] != LEGACY_CATALOG_TRANSACTION_KIND
            or type(seal["receipt_protocol"]) is not str
            or seal["receipt_protocol"] != LEGACY_CATALOG_RECEIPT_PROTOCOL
            or type(seal["seal_protocol"]) is not str
            or seal["seal_protocol"] != LEGACY_CATALOG_SEAL_PROTOCOL
            or type(seal["manifest_schema"]) is not str
            or seal["manifest_schema"] != LEGACY_CATALOG_MANIFEST_SCHEMA
            or type(seal["status"]) is not str
            or seal["status"] != LEGACY_CATALOG_STATUS
            or type(seal["selection"]) is not str
            or seal["selection"] != LEGACY_CATALOG_SELECTION
            or type(seal["project_count"]) is not int
            or not 0 <= seal["project_count"] <= MAX_LEGACY_CATALOG_PROJECTS
            or type(seal["entity_count"]) is not int
            or not 0 <= seal["entity_count"] <= MAX_LEGACY_ENTITIES
            or type(seal["source_head_bytes"]) is not int
            or not 0
            <= seal["source_head_bytes"]
            <= MAX_LEGACY_CATALOG_SOURCE_HEAD_BYTES
            or type(seal["row_snapshot_bytes"]) is not int
            or not 0
            <= seal["row_snapshot_bytes"]
            <= MAX_LEGACY_CATALOG_TARGET_SNAPSHOT_BYTES
            or seal["manifest_type"] != "blob"
            or type(seal["manifest_length"]) is not int
            or not 1
            <= seal["manifest_length"]
            <= MAX_LEGACY_CATALOG_MANIFEST_BYTES
            or seal["manifest_digest_type"] != "blob"
            or seal["manifest_digest_length"] != 32
            or seal["source_digest_type"] != "blob"
            or seal["source_digest_length"] != 32
            or seal["target_digest_type"] != "blob"
            or seal["target_digest_length"] != 32
            or seal["seal_digest_type"] != "blob"
            or seal["seal_digest_length"] != 32
            or type(seal["target_schema_version"]) is not int
            or seal["target_schema_version"] != LEGACY_CATALOG_TARGET_SCHEMA_VERSION
            or type(seal["schema_fingerprint"]) is not str
            or seal["schema_fingerprint"]
            != _expected_schema_fingerprint(LEGACY_CATALOG_TARGET_SCHEMA_VERSION)
            or type(seal["cutover_ready"]) is not int
            or seal["cutover_ready"] != LEGACY_CATALOG_CUTOVER_READY
        ):
            self._raise_catalog_state_invalid(
                "historical legacy catalog seal has an invalid shape"
            )

        receipt_rows = connection.execute(
            """
            SELECT transaction_id, kind, committed_at
            FROM transaction_receipts
            WHERE substr(CAST(transaction_id AS TEXT), 1, ?) = ?
               OR CAST(kind AS TEXT) = ?
            ORDER BY transaction_id
            """,
            (
                len(LEGACY_CATALOG_TRANSACTION_PREFIX),
                LEGACY_CATALOG_TRANSACTION_PREFIX,
                LEGACY_CATALOG_TRANSACTION_KIND,
            ),
        ).fetchall()
        if len(receipt_rows) != 1:
            self._raise_catalog_state_invalid(
                "historical legacy catalog receipt namespace is not one-to-one"
            )
        receipt = receipt_rows[0]
        if (
            type(receipt["transaction_id"]) is not str
            or receipt["transaction_id"] != transaction_id
            or type(receipt["kind"]) is not str
            or receipt["kind"] != LEGACY_CATALOG_TRANSACTION_KIND
            or type(receipt["committed_at"]) is not str
            or _COMMITTED_AT_RE.fullmatch(receipt["committed_at"]) is None
        ):
            self._raise_catalog_state_invalid(
                "historical legacy catalog receipt is not canonical"
            )

        member_rows = connection.execute(
            """
            SELECT
                catalog_id, ordinal, import_id, project_id, generation,
                document_id, document_version, storage_version,
                source_head_sha256,
                typeof(source_head_sha256) AS source_digest_type,
                length(source_head_sha256) AS source_digest_length,
                source_head_bytes, business_root_sha256,
                typeof(business_root_sha256) AS root_digest_type,
                length(business_root_sha256) AS root_digest_length,
                entity_count, row_snapshot_bytes
            FROM legacy_catalog_members
            ORDER BY ordinal, project_id
            """
        ).fetchall()

        if expect_empty:
            if (
                import_id is not None
                or seal["project_count"] != 0
                or seal["entity_count"] != 0
                or seal["source_head_bytes"] != 0
                or seal["row_snapshot_bytes"] != 0
                or member_rows
            ):
                self._raise_catalog_state_invalid(
                    "historical empty catalog is not structurally empty"
                )
            return

        if not _is_lower_hex64(import_id):
            self._raise_catalog_state_invalid(
                "historical non-empty catalog has no canonical import identity"
            )
        batch_rows = connection.execute(
            """
            SELECT import_id, project_count, entity_count, row_snapshot_bytes
            FROM legacy_import_batches
            ORDER BY import_id
            """
        ).fetchall()
        if len(batch_rows) != 1:
            self._raise_catalog_state_invalid(
                "historical non-empty catalog must cover exactly one import batch"
            )
        batch = batch_rows[0]
        if (
            batch["import_id"] != import_id
            or batch["project_count"] != seal["project_count"]
            or batch["entity_count"] != seal["entity_count"]
            or batch["row_snapshot_bytes"] != seal["row_snapshot_bytes"]
        ):
            self._raise_catalog_state_invalid(
                "historical catalog and import batch aggregates differ"
            )

        descriptor_rows = connection.execute(
            """
            SELECT
                import_id, ordinal, project_id, generation, document_id,
                document_version, storage_version, source_head_sha256,
                source_head_bytes, business_root_sha256, entity_count,
                row_snapshot_bytes
            FROM legacy_import_projects
            ORDER BY ordinal, project_id
            """
        ).fetchall()
        if len(member_rows) != len(descriptor_rows):
            self._raise_catalog_state_invalid(
                "historical catalog members do not cover every descriptor"
            )

        source_head_bytes = 0
        entity_count = 0
        row_snapshot_bytes = 0
        project_ids: list[str] = []
        for ordinal, (member, descriptor) in enumerate(
            zip(member_rows, descriptor_rows)
        ):
            project_id = member["project_id"]
            if (
                member["catalog_id"] != catalog_id
                or member["import_id"] != import_id
                or type(member["ordinal"]) is not int
                or member["ordinal"] != ordinal
                or type(project_id) is not str
                or not 1 <= len(project_id) <= 160
                or type(member["generation"]) is not int
                or member["generation"] < 1
                or type(member["document_id"]) is not str
                or not member["document_id"]
                or type(member["document_version"]) is not int
                or member["document_version"] < 1
                or type(member["storage_version"]) is not str
                or member["storage_version"] not in ("legacy", "v2")
                or member["source_digest_type"] != "blob"
                or member["source_digest_length"] != 32
                or type(member["source_head_bytes"]) is not int
                or not 1
                <= member["source_head_bytes"]
                <= MAX_LEGACY_CATALOG_PROJECT_SOURCE_HEAD_BYTES
                or member["root_digest_type"] != "blob"
                or member["root_digest_length"] != 32
                or type(member["entity_count"]) is not int
                or not 0 <= member["entity_count"] <= MAX_LEGACY_ENTITIES
                or type(member["row_snapshot_bytes"]) is not int
                or not 1
                <= member["row_snapshot_bytes"]
                <= MAX_LEGACY_CATALOG_TARGET_SNAPSHOT_BYTES
            ):
                self._raise_catalog_state_invalid(
                    "historical legacy catalog member has an invalid shape"
                )
            member_identity = (
                member["import_id"],
                member["ordinal"],
                project_id,
                member["generation"],
                member["document_id"],
                member["document_version"],
                member["storage_version"],
                member["source_head_sha256"],
                member["source_head_bytes"],
                member["business_root_sha256"],
                member["entity_count"],
                member["row_snapshot_bytes"],
            )
            if member_identity != tuple(descriptor):
                self._raise_catalog_state_invalid(
                    "historical catalog member differs from its import descriptor"
                )
            project_ids.append(project_id)
            source_head_bytes += member["source_head_bytes"]
            entity_count += member["entity_count"]
            row_snapshot_bytes += member["row_snapshot_bytes"]

        if (
            len(member_rows) != seal["project_count"]
            or [member["ordinal"] for member in member_rows]
            != list(range(len(member_rows)))
            or project_ids != sorted(project_ids)
            or source_head_bytes != seal["source_head_bytes"]
            or entity_count != seal["entity_count"]
            or row_snapshot_bytes != seal["row_snapshot_bytes"]
        ):
            self._raise_catalog_state_invalid(
                "historical catalog member aggregates are inconsistent"
            )

    @staticmethod
    def _raise_import_state_invalid(message: str) -> None:
        raise SQLiteKernelError("sqlite_import_state_invalid", message)

    @staticmethod
    def _raise_catalog_state_invalid(message: str) -> None:
        raise SQLiteKernelError("sqlite_catalog_state_invalid", message)

    @staticmethod
    def _raise_repository_state_invalid(message: str) -> None:
        raise SQLiteKernelError("sqlite_repository_state_invalid", message)

    def _verify_inert_business_tables(self, connection: _OwnedConnection) -> None:
        """Before a controlled importer exists, any business row is corruption."""
        if MIGRATIONS[-1].version < BUSINESS_SNAPSHOT_MIGRATION.version:
            return
        for table in BUSINESS_TABLES:
            row = connection.execute(
                "SELECT EXISTS(SELECT 1 FROM %s LIMIT 1)" % table
            ).fetchone()
            if row is None or int(row[0]) != 0:
                raise SQLiteKernelError(
                    "sqlite_business_state_unexpected",
                    "inactive business snapshot tables must remain empty",
                )

    def _prepare_storage(self, *, allow_create: bool = False) -> FilesystemInfo:
        _reject_unsafe_components(self.path)
        if allow_create:
            missing_directories = _missing_directories(self.path.parent)
            try:
                self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            except OSError as exc:
                raise SQLiteKernelError("sqlite_directory_create_failed", str(exc)) from exc
            _reject_unsafe_components(self.path)
            for directory in reversed(missing_directories):
                _fsync_directory(directory.parent)
        elif not self.path.parent.is_dir() or not self.path.exists():
            raise SQLiteKernelError(
                "sqlite_database_missing",
                "database does not exist; only initialize() may create it",
            )
        filesystem = self._filesystem_probe(self.path.parent)
        if not isinstance(filesystem, FilesystemInfo):
            raise SQLiteKernelError(
                "sqlite_filesystem_probe_invalid", "filesystem probe returned an invalid result"
            )
        if not filesystem.is_local:
            raise SQLiteKernelError(
                "sqlite_filesystem_unsupported",
                filesystem.reason or "filesystem is not proven local",
            )
        _make_directory_private(self.path.parent)

        if allow_create and not self.path.exists():
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            flags |= getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(str(self.path), flags, 0o600)
            except FileExistsError:
                pass
            except OSError as exc:
                raise SQLiteKernelError("sqlite_file_create_failed", str(exc)) from exc
            else:
                os.fsync(descriptor)
                os.close(descriptor)
                _fsync_directory(self.path.parent)

        self._validate_storage_files()
        return filesystem

    @contextmanager
    def _initialization_lock(self) -> Iterator[None]:
        if os.name != "posix":
            raise SQLiteKernelError(
                "sqlite_process_lock_unsupported",
                "cross-process SQLite bootstrap lock requires POSIX flock",
            )
        try:
            import fcntl
        except ImportError as exc:
            raise SQLiteKernelError(
                "sqlite_process_lock_unsupported", "fcntl.flock is unavailable"
            ) from exc

        lock_path = _initialization_lock_path(self.path)
        flags = os.O_CREAT | os.O_RDWR
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_NONBLOCK", 0)
        try:
            descriptor = os.open(str(lock_path), flags, 0o600)
        except OSError as exc:
            raise SQLiteKernelError("sqlite_process_lock_open_failed", str(exc)) from exc
        acquired = False
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise SQLiteKernelError(
                    "sqlite_process_lock_unsafe", "bootstrap lock is not a single regular file"
                )
            if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
                raise SQLiteKernelError(
                    "sqlite_process_lock_unsafe", "bootstrap lock has another owner"
                )
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
            _fsync_directory(self.path.parent)

            deadline = time.monotonic() + self.settings.busy_timeout_ms / 1000.0
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except BlockingIOError as exc:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise SQLiteKernelError(
                            "sqlite_busy", "SQLite bootstrap lock wait exceeded busy_timeout"
                        ) from exc
                    time.sleep(min(0.01, remaining))
            yield
        finally:
            if acquired:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                except OSError:
                    pass
            os.close(descriptor)

    def _validate_storage_files(self) -> None:
        _reject_unsafe_components(self.path)
        for candidate in _storage_paths(self.path):
            try:
                info = os.lstat(str(candidate))
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise SQLiteKernelError("sqlite_file_stat_failed", str(exc)) from exc
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise SQLiteKernelError(
                    "sqlite_file_type_unsafe", "%s is not a regular file" % candidate
                )
            if info.st_nlink != 1:
                raise SQLiteKernelError(
                    "sqlite_file_hardlink_unsafe", "%s has multiple hard links" % candidate
                )
            _make_file_private(candidate, info)

    def _finalize_committed_storage(self) -> None:
        """Apply the one post-COMMIT file and directory durability barrier.

        Every writer that reports a committed receipt, including the private
        terminal-shadow importer and acknowledgement-loss recovery, must pass
        this exact barrier.  A relational receipt alone cannot prove that a
        newly created WAL sidecar directory entry reached stable storage.
        """

        self._validate_storage_files()
        _fsync_directory(self.path.parent)

    def _finalize_readonly_storage(self) -> None:
        """Durability fence an audited store without normalizing its profile."""

        self._validate_readonly_storage()
        _fsync_directory(self.path.parent)


def _configure_connection(connection: _OwnedConnection, busy_timeout_ms: int) -> None:
    try:
        sqlite_version = str(connection.execute("SELECT sqlite_version()").fetchone()[0])
        if _sqlite_version_tuple(sqlite_version) < MIN_SQLITE_VERSION:
            raise SQLiteKernelError(
                "sqlite_feature_unsupported",
                "SQLite %s is older than required %s"
                % (sqlite_version, ".".join(str(value) for value in MIN_SQLITE_VERSION)),
            )
        connection.execute("PRAGMA busy_timeout = %d" % busy_timeout_ms)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA trusted_schema = OFF")
        trusted = connection.execute("PRAGMA trusted_schema").fetchone()
        if trusted is None:
            raise SQLiteKernelError(
                "sqlite_feature_unsupported", "SQLite does not support trusted_schema"
            )
        journal_row = connection.execute("PRAGMA journal_mode = WAL").fetchone()
        journal = str(journal_row[0]).lower() if journal_row is not None else ""
        if journal != "wal":
            raise SQLiteKernelError(
                "sqlite_profile_mismatch", "journal_mode did not become WAL"
            )
        connection.execute("PRAGMA synchronous = FULL")
        if platform.system() == "Darwin":
            connection.execute("PRAGMA fullfsync = ON")
            connection.execute("PRAGMA checkpoint_fullfsync = ON")
        profile = _read_profile(
            connection,
            busy_timeout_ms,
            FilesystemInfo("pending", "", True),
        )
        if (
            profile.encoding != "UTF-8"
            or profile.journal_mode != "wal"
            or profile.synchronous != 2
            or profile.foreign_keys != 1
            or profile.trusted_schema != 0
            or profile.busy_timeout_ms != busy_timeout_ms
        ):
            raise SQLiteKernelError(
                "sqlite_profile_mismatch", "connection PRAGMA safety profile did not stick"
            )
        if platform.system() == "Darwin":
            if _pragma_int(connection, "fullfsync") != 1:
                raise SQLiteKernelError(
                    "sqlite_profile_mismatch", "fullfsync did not become ON"
                )
            if _pragma_int(connection, "checkpoint_fullfsync") != 1:
                raise SQLiteKernelError(
                    "sqlite_profile_mismatch", "checkpoint_fullfsync did not become ON"
                )
    except SQLiteKernelError:
        raise
    except sqlite3.Error as exc:
        message = str(exc)
        code = "sqlite_busy" if "locked" in message.lower() or "busy" in message.lower() else "sqlite_profile_failed"
        raise SQLiteKernelError(code, message) from exc


def _configure_readonly_connection(
    connection: _OwnedConnection,
    busy_timeout_ms: int,
) -> None:
    """Install connection-local safety flags without changing journal mode."""

    try:
        sqlite_version = str(
            connection.execute("SELECT sqlite_version()").fetchone()[0]
        )
        if _sqlite_version_tuple(sqlite_version) < MIN_SQLITE_VERSION:
            raise SQLiteKernelError(
                "sqlite_feature_unsupported",
                "SQLite %s is older than required %s"
                % (
                    sqlite_version,
                    ".".join(str(value) for value in MIN_SQLITE_VERSION),
                ),
            )
        connection.execute("PRAGMA busy_timeout = %d" % busy_timeout_ms)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA trusted_schema = OFF")
        connection.execute("PRAGMA synchronous = FULL")
        if platform.system() == "Darwin":
            connection.execute("PRAGMA fullfsync = ON")
            connection.execute("PRAGMA checkpoint_fullfsync = ON")
        connection.execute("PRAGMA query_only = ON")
        query_only = connection.execute("PRAGMA query_only").fetchone()
        profile = _read_profile(
            connection,
            busy_timeout_ms,
            FilesystemInfo("pending", "", True),
        )
        if (
            query_only is None
            or int(query_only[0]) != 1
            or profile.encoding != "UTF-8"
            or profile.journal_mode != "wal"
            or profile.synchronous != 2
            or profile.foreign_keys != 1
            or profile.trusted_schema != 0
            or profile.busy_timeout_ms != busy_timeout_ms
        ):
            raise SQLiteKernelError(
                "sqlite_profile_mismatch",
                "read-only connection profile is not the required WAL profile",
            )
        if platform.system() == "Darwin" and (
            _pragma_int(connection, "fullfsync") != 1
            or _pragma_int(connection, "checkpoint_fullfsync") != 1
        ):
            raise SQLiteKernelError(
                "sqlite_profile_mismatch",
                "read-only connection full-fsync profile did not stick",
            )
    except SQLiteKernelError:
        raise
    except sqlite3.Error as exc:
        message = str(exc)
        code = (
            "sqlite_busy"
            if "locked" in message.lower() or "busy" in message.lower()
            else "sqlite_profile_failed"
        )
        raise SQLiteKernelError(code, message) from exc


def _sqlite_version_tuple(value: str) -> Tuple[int, int, int]:
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)", value)
    if match is None:
        raise SQLiteKernelError(
            "sqlite_feature_unsupported", "SQLite version is not parseable"
        )
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _preflight_database_identity(connection: sqlite3.Connection) -> None:
    """Reject an obvious wrong database before changing its persistent WAL mode."""
    try:
        application_row = connection.execute("PRAGMA application_id").fetchone()
        if application_row is None:
            raise SQLiteKernelError(
                "sqlite_feature_unsupported", "SQLite application_id is unavailable"
            )
        application_id = int(application_row[0])
        if application_id not in (0, APPLICATION_ID):
            raise SQLiteKernelError(
                "sqlite_application_id_mismatch", "database belongs to another application"
            )
        if application_id == 0:
            row = connection.execute(
                "SELECT COUNT(*) FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'"
            ).fetchone()
            if row is None or int(row[0]) != 0:
                raise SQLiteKernelError(
                    "sqlite_application_id_missing",
                    "non-empty database has no ZE application_id",
                )
    except SQLiteKernelError:
        raise
    except sqlite3.Error as exc:
        raise SQLiteKernelError("sqlite_identity_probe_failed", str(exc)) from exc


def _read_profile(
    connection: _OwnedConnection,
    expected_busy_timeout_ms: int,
    filesystem: FilesystemInfo,
) -> ConnectionProfile:
    journal_row = connection.execute("PRAGMA journal_mode").fetchone()
    trusted_row = connection.execute("PRAGMA trusted_schema").fetchone()
    encoding_row = connection.execute("PRAGMA encoding").fetchone()
    if journal_row is None or trusted_row is None or encoding_row is None:
        raise SQLiteKernelError(
            "sqlite_feature_unsupported", "required SQLite PRAGMA is unavailable"
        )
    profile = ConnectionProfile(
        encoding=str(encoding_row[0]).upper(),
        journal_mode=str(journal_row[0]).lower(),
        synchronous=_pragma_int(connection, "synchronous"),
        foreign_keys=_pragma_int(connection, "foreign_keys"),
        trusted_schema=int(trusted_row[0]),
        busy_timeout_ms=_pragma_int(connection, "busy_timeout"),
        application_id=_pragma_int(connection, "application_id"),
        user_version=_pragma_int(connection, "user_version"),
        sqlite_version=str(connection.execute("SELECT sqlite_version()").fetchone()[0]),
        filesystem=filesystem,
    )
    if profile.busy_timeout_ms != expected_busy_timeout_ms:
        raise SQLiteKernelError(
            "sqlite_profile_mismatch", "busy_timeout does not match configured bound"
        )
    return profile


def _fetch_receipt(
    connection: _OwnedConnection, transaction_id: str
) -> Optional[KernelReceipt]:
    row = connection.execute(
        """
        SELECT transaction_id, kind, committed_at
        FROM transaction_receipts
        WHERE transaction_id = ?
        """,
        (transaction_id,),
    ).fetchone()
    if row is None:
        return None
    stored_id = row["transaction_id"]
    stored_kind = row["kind"]
    committed_at = row["committed_at"]
    if (
        not isinstance(stored_id, str)
        or not isinstance(stored_kind, str)
        or not isinstance(committed_at, str)
    ):
        raise SQLiteKernelError(
            "sqlite_receipt_corrupt", "receipt columns must all be canonical text"
        )
    if stored_id != transaction_id:
        raise SQLiteKernelError(
            "sqlite_receipt_corrupt", "receipt identity differs from its lookup key"
        )
    _validate_identity("transaction_id", stored_id)
    _validate_identity("kind", stored_kind)
    if _COMMITTED_AT_RE.fullmatch(committed_at) is None:
        raise SQLiteKernelError(
            "sqlite_receipt_corrupt", "receipt committed_at is not canonical UTC"
        )
    return KernelReceipt(
        transaction_id=stored_id,
        kind=stored_kind,
        committed_at=committed_at,
    )


def _reserved_import_receipt_exists(connection: _OwnedConnection) -> bool:
    row = connection.execute(
        """
        SELECT EXISTS(
            SELECT 1
            FROM transaction_receipts
            WHERE substr(CAST(transaction_id AS TEXT), 1, ?) = ?
               OR CAST(kind AS TEXT) = ?
            LIMIT 1
        )
        """,
        (
            len(SQLITE_IMPORT_TRANSACTION_PREFIX),
            SQLITE_IMPORT_TRANSACTION_PREFIX,
            SQLITE_IMPORT_TRANSACTION_KIND,
        ),
    ).fetchone()
    if row is None:
        raise SQLiteKernelError(
            "sqlite_receipt_namespace_unreadable",
            "legacy import receipt namespace could not be inspected",
        )
    return int(row[0]) != 0


def _reserved_catalog_receipt_exists(connection: _OwnedConnection) -> bool:
    row = connection.execute(
        """
        SELECT EXISTS(
            SELECT 1
            FROM transaction_receipts
            WHERE substr(CAST(transaction_id AS TEXT), 1, ?) = ?
               OR CAST(kind AS TEXT) = ?
            LIMIT 1
        )
        """,
        (
            len(LEGACY_CATALOG_TRANSACTION_PREFIX),
            LEGACY_CATALOG_TRANSACTION_PREFIX,
            LEGACY_CATALOG_TRANSACTION_KIND,
        ),
    ).fetchone()
    if row is None:
        raise SQLiteKernelError(
            "sqlite_receipt_namespace_unreadable",
            "legacy catalog receipt namespace could not be inspected",
        )
    return int(row[0]) != 0


def _reserved_repository_receipt_exists(connection: _OwnedConnection) -> bool:
    row = connection.execute(
        """
        SELECT EXISTS(
            SELECT 1
            FROM transaction_receipts
            WHERE substr(CAST(transaction_id AS TEXT), 1, ?) = ?
               OR CAST(kind AS TEXT) = ?
            LIMIT 1
        )
        """,
        (
            len(LEGACY_REPOSITORY_TRANSACTION_PREFIX),
            LEGACY_REPOSITORY_TRANSACTION_PREFIX,
            LEGACY_REPOSITORY_TRANSACTION_KIND,
        ),
    ).fetchone()
    if row is None:
        raise SQLiteKernelError(
            "sqlite_receipt_namespace_unreadable",
            "legacy repository receipt namespace could not be inspected",
        )
    return int(row[0]) != 0


def _reserved_business_receipt_exists(connection: _OwnedConnection) -> bool:
    row = connection.execute(
        """
        SELECT EXISTS(
            SELECT 1
            FROM transaction_receipts
            WHERE substr(CAST(transaction_id AS TEXT), 1, ?) = ?
               OR CAST(kind AS TEXT) = ?
            LIMIT 1
        )
        """,
        (
            len(BUSINESS_TRANSACTION_PREFIX),
            BUSINESS_TRANSACTION_PREFIX,
            BUSINESS_TRANSACTION_KIND,
        ),
    ).fetchone()
    if row is None:
        raise SQLiteKernelError(
            "sqlite_receipt_namespace_unreadable",
            "business transaction receipt namespace could not be inspected",
        )
    return int(row[0]) != 0


def _is_lower_hex64(value: Any) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _repository_operation_id_valid(
    value: Any, *, allow_legacy_adopt: bool, generation: Any
) -> bool:
    return (
        type(value) is str
        and (
            (
                len(value) == 32
                and all(
                    character in "0123456789abcdef" for character in value
                )
            )
            or (
                allow_legacy_adopt
                and value == LEGACY_REPOSITORY_LEGACY_ADOPT_OPERATION_ID
                and generation == 1
            )
        )
    )


def _validate_identity(field: str, value: Any) -> None:
    if type(value) is not str or _IDENTITY_RE.fullmatch(value) is None:
        raise SQLiteKernelError(
            "sqlite_%s_invalid" % field,
            "%s must be 1-128 characters from the stable identity alphabet" % field,
        )


def _pragma_int(connection: _OwnedConnection, name: str) -> int:
    if name not in {
        "application_id",
        "busy_timeout",
        "checkpoint_fullfsync",
        "foreign_keys",
        "fullfsync",
        "schema_version",
        "synchronous",
        "trusted_schema",
        "user_version",
    }:
        raise ValueError("unsupported PRAGMA name")
    row = connection.execute("PRAGMA %s" % name).fetchone()
    if row is None:
        raise SQLiteKernelError(
            "sqlite_feature_unsupported", "required PRAGMA %s is unavailable" % name
        )
    return int(row[0])


def _schema_normalizer(schema_version: int) -> Callable[[str], str]:
    # W06.1 persisted a whitespace-collapsed v1 fingerprint.  Preserve that
    # one historical identity; v2+ fingerprints retain SQL literal bytes.
    if schema_version == KERNEL_MIGRATION.version:
        return legacy_normalize_schema_sql
    return normalize_schema_sql


def _schema_rows(
    connection: _OwnedConnection,
    schema_version: Optional[int] = None,
) -> Tuple[Tuple[str, str, str, str], ...]:
    version = MIGRATIONS[-1].version if schema_version is None else schema_version
    normalize = _schema_normalizer(version)
    rows = connection.execute(
        """
        SELECT type, name, tbl_name, sql
        FROM sqlite_schema
        WHERE name NOT LIKE 'sqlite_%'
        ORDER BY type, name, tbl_name
        """
    ).fetchall()
    return tuple(
        (
            row["type"],
            row["name"],
            row["tbl_name"],
            normalize(row["sql"] or ""),
        )
        for row in rows
    )


def _schema_fingerprint(connection: _OwnedConnection, schema_version: int) -> str:
    payload = json.dumps(
        _schema_rows(connection, schema_version),
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _expected_schema_fingerprint(schema_version: Optional[int] = None) -> str:
    # Replaying immutable statements in an isolated in-memory database makes
    # the expectation grow with ALTER/INDEX/TRIGGER migrations too.  Parsing
    # only today's CREATE TABLE statements would turn the W06.1 checker into a
    # blocker as soon as the business schema arrives.
    version = MIGRATIONS[-1].version if schema_version is None else schema_version
    selected = tuple(migration for migration in MIGRATIONS if migration.version <= version)
    if not selected or selected[-1].version != version:
        raise SQLiteKernelError(
            "sqlite_schema_version_unsupported",
            "cannot fingerprint unknown schema version %d" % version,
        )
    frozen_selected = tuple(
        (
            migration.version,
            migration.name,
            tuple(migration.statements),
            migration.checksum_scheme,
        )
        for migration in selected
    )
    return _expected_schema_fingerprint_for_migrations(frozen_selected)


@lru_cache(maxsize=16)
def _expected_schema_fingerprint_for_migrations(
    selected: Tuple[Tuple[int, str, Tuple[str, ...], str], ...],
) -> str:
    """Replay each immutable migration registry at most once per process."""
    version = selected[-1][0]
    raw = sqlite3.connect(":memory:", isolation_level=None)
    raw.row_factory = sqlite3.Row
    owned = _OwnedConnection(raw)
    try:
        owned.execute("PRAGMA trusted_schema = OFF")
        owned.execute("BEGIN")
        for _migration_version, _name, statements, _scheme in selected:
            for statement in statements:
                owned.execute(statement)
        rows = _schema_rows(owned, version)
        owned.rollback()
    finally:
        if not owned._closed:
            owned.close()
    payload = json.dumps(rows, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _rollback_quietly(connection: _OwnedConnection) -> None:
    try:
        connection.rollback()
    except BaseException:
        pass


def _close_quietly(connection: _OwnedConnection) -> None:
    try:
        connection.close()
    except BaseException:
        pass


def _storage_paths(path: Path) -> Tuple[Path, ...]:
    base = str(path)
    return tuple(
        Path(base + suffix)
        for suffix in ("", "-wal", "-shm", "-journal", "-bootstrap.lock")
    )


def _initialization_lock_path(path: Path) -> Path:
    return Path(str(path) + "-bootstrap.lock")


def _missing_directories(path: Path) -> Tuple[Path, ...]:
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        parent = current.parent
        if parent == current:
            break
        current = parent
    return tuple(missing)


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        raise SQLiteKernelError(
            "sqlite_directory_sync_unsupported",
            "durable SQLite directory entries require POSIX directory fsync",
        )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(str(path), flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise SQLiteKernelError("sqlite_directory_sync_failed", str(exc)) from exc


def _reject_unsafe_components(path: Path) -> None:
    current = Path(path.anchor)
    parts = path.parts[1:] if path.anchor else path.parts
    for part in parts:
        current = current / part
        try:
            info = os.lstat(str(current))
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise SQLiteKernelError("sqlite_path_stat_failed", str(exc)) from exc
        if stat.S_ISLNK(info.st_mode):
            raise SQLiteKernelError(
                "sqlite_path_symlink_unsafe", "path contains symlink: %s" % current
            )
        if current != path and not stat.S_ISDIR(info.st_mode):
            raise SQLiteKernelError(
                "sqlite_path_component_unsafe", "path component is not a directory: %s" % current
            )


def _make_directory_private(path: Path) -> None:
    if os.name != "posix":
        raise SQLiteKernelError(
            "sqlite_permission_model_unsupported",
            "private SQLite directory verification is not implemented on this OS",
        )
    try:
        info = os.stat(str(path), follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode):
            raise SQLiteKernelError(
                "sqlite_directory_type_unsafe", "%s is not a directory" % path
            )
        if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
            raise SQLiteKernelError(
                "sqlite_directory_owner_unsafe", "%s is not owned by this user" % path
            )
        if stat.S_IMODE(info.st_mode) & 0o077:
            os.chmod(str(path), 0o700, follow_symlinks=False)
            info = os.stat(str(path), follow_symlinks=False)
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise SQLiteKernelError(
                "sqlite_directory_permissions_unsafe", "%s is not private" % path
            )
        _fsync_directory(path)
    except SQLiteKernelError:
        raise
    except OSError as exc:
        raise SQLiteKernelError("sqlite_directory_permissions_failed", str(exc)) from exc


def _inspect_readonly_storage(database: SqliteDatabase) -> FilesystemInfo:
    """Validate the existing storage profile without chmod/fsync/create."""

    path = database.path
    _reject_unsafe_components(path)
    if not path.parent.is_dir() or not path.exists():
        raise SQLiteKernelError(
            "sqlite_database_missing",
            "database does not exist; read-only audit never creates it",
        )
    filesystem = database._filesystem_probe(path.parent)
    if not isinstance(filesystem, FilesystemInfo):
        raise SQLiteKernelError(
            "sqlite_filesystem_probe_invalid",
            "filesystem probe returned an invalid result",
        )
    if not filesystem.is_local:
        raise SQLiteKernelError(
            "sqlite_filesystem_unsupported",
            filesystem.reason or "filesystem is not proven local",
        )
    if os.name != "posix":
        raise SQLiteKernelError(
            "sqlite_permission_model_unsupported",
            "read-only SQLite permission verification is not implemented on this OS",
        )
    try:
        directory = os.stat(str(path.parent), follow_symlinks=False)
        if not stat.S_ISDIR(directory.st_mode):
            raise SQLiteKernelError(
                "sqlite_directory_type_unsafe",
                "%s is not a directory" % path.parent,
            )
        if hasattr(os, "geteuid") and directory.st_uid != os.geteuid():
            raise SQLiteKernelError(
                "sqlite_directory_owner_unsafe",
                "%s is not owned by this user" % path.parent,
            )
        if stat.S_IMODE(directory.st_mode) & 0o077:
            raise SQLiteKernelError(
                "sqlite_directory_permissions_unsafe",
                "%s is not private" % path.parent,
            )
        for candidate in _storage_paths(path):
            try:
                info = os.lstat(str(candidate))
            except FileNotFoundError:
                continue
            if (
                stat.S_ISLNK(info.st_mode)
                or not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
            ):
                raise SQLiteKernelError(
                    "sqlite_file_type_unsafe",
                    "%s is not a single regular file" % candidate,
                )
            if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
                raise SQLiteKernelError(
                    "sqlite_file_owner_unsafe",
                    "%s is not owned by this user" % candidate,
                )
            if stat.S_IMODE(info.st_mode) & 0o077:
                raise SQLiteKernelError(
                    "sqlite_file_permissions_unsafe",
                    "%s is not private" % candidate,
                )
    except SQLiteKernelError:
        raise
    except OSError as exc:
        raise SQLiteKernelError("sqlite_file_stat_failed", str(exc)) from exc
    return filesystem


def _make_file_private(path: Path, info: os.stat_result) -> None:
    if os.name != "posix":
        raise SQLiteKernelError(
            "sqlite_permission_model_unsupported",
            "private SQLite file verification is not implemented on this OS",
        )
    try:
        if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
            raise SQLiteKernelError(
                "sqlite_file_owner_unsafe", "%s is not owned by this user" % path
            )
        if stat.S_IMODE(info.st_mode) & 0o077:
            os.chmod(str(path), 0o600, follow_symlinks=False)
            info = os.lstat(str(path))
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise SQLiteKernelError(
                "sqlite_file_permissions_unsafe", "%s is not private" % path
            )
    except SQLiteKernelError:
        raise
    except OSError as exc:
        raise SQLiteKernelError("sqlite_file_permissions_failed", str(exc)) from exc


def probe_local_filesystem(path: Path) -> FilesystemInfo:
    """Conservative platform probe: unknown and network filesystems fail closed."""
    system = platform.system()
    if system == "Linux":
        return _probe_linux_mountinfo(path)
    if system == "Darwin":
        return _probe_darwin_mount(path)
    if system == "Windows":
        return _probe_windows_drive(path)
    return FilesystemInfo("unknown", "", False, "unsupported filesystem probe platform")


def _decode_mount_field(value: str) -> str:
    return (
        value.replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\134", "\\")
    )


def _path_is_under(path: Path, mount_point: Path) -> bool:
    try:
        path.relative_to(mount_point)
        return True
    except ValueError:
        return False


def _probe_linux_mountinfo(path: Path) -> FilesystemInfo:
    allowed = {
        "bcachefs",
        "btrfs",
        "ext2",
        "ext3",
        "ext4",
        "f2fs",
        "jfs",
        "overlay",
        "ramfs",
        "reiserfs",
        "tmpfs",
        "ubifs",
        "xfs",
        "zfs",
    }
    try:
        lines = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return FilesystemInfo("unknown", "", False, "cannot read mountinfo: %s" % exc)
    candidates = []
    for line in lines:
        fields = line.split()
        try:
            separator = fields.index("-")
            mount_point = Path(_decode_mount_field(fields[4]))
            filesystem = fields[separator + 1].lower()
        except (ValueError, IndexError):
            continue
        if _path_is_under(path, mount_point):
            candidates.append((len(str(mount_point)), mount_point, filesystem))
    if not candidates:
        return FilesystemInfo("unknown", "", False, "no matching Linux mount")
    _length, mount_point, filesystem = max(candidates, key=lambda item: item[0])
    is_local = filesystem in allowed
    return FilesystemInfo(
        filesystem,
        str(mount_point),
        is_local,
        "" if is_local else "filesystem %s is not in the local WAL allowlist" % filesystem,
    )


def _probe_darwin_mount(path: Path) -> FilesystemInfo:
    allowed = {"apfs", "hfs", "hfs+", "tmpfs"}
    executable = "/sbin/mount"
    try:
        completed = subprocess.run(
            [executable],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        return FilesystemInfo("unknown", "", False, "cannot inspect mounts: %s" % exc)
    candidates = []
    for line in completed.stdout.splitlines():
        match = re.match(r"^.+ on (.+) \(([^)]*)\)$", line)
        if match is None:
            continue
        mount_point = Path(match.group(1))
        options = [part.strip().lower() for part in match.group(2).split(",")]
        filesystem = options[0] if options else "unknown"
        if _path_is_under(path, mount_point):
            candidates.append((len(str(mount_point)), mount_point, filesystem, options))
    if not candidates:
        return FilesystemInfo("unknown", "", False, "no matching Darwin mount")
    _length, mount_point, filesystem, options = max(candidates, key=lambda item: item[0])
    is_local = filesystem in allowed and "local" in options
    return FilesystemInfo(
        filesystem,
        str(mount_point),
        is_local,
        "" if is_local else "mount is not a proven local filesystem with SQLite WAL semantics",
    )


def _probe_windows_drive(path: Path) -> FilesystemInfo:
    if str(path).startswith(("\\\\", "//")):
        return FilesystemInfo("unc", "", False, "UNC paths are not local SQLite WAL storage")
    try:
        import ctypes

        root = path.drive + "\\"
        drive_type = ctypes.windll.kernel32.GetDriveTypeW(root)
    except Exception as exc:
        return FilesystemInfo("unknown", "", False, "cannot inspect Windows drive: %s" % exc)
    # DRIVE_FIXED == 3.  Removable, remote, optical and unknown drives fail closed.
    is_local = drive_type == 3
    return FilesystemInfo(
        "fixed" if is_local else "unsupported",
        root,
        is_local,
        "" if is_local else "Windows database must live on a fixed local drive",
    )


__all__ = [
    "APPLICATION_ID",
    "BUSINESS_SNAPSHOT_MIGRATION",
    "BUSINESS_TABLES",
    "BUSINESS_TRANSACTION_KIND",
    "BUSINESS_TRANSACTION_MIGRATION",
    "BUSINESS_TRANSACTION_PREFIX",
    "BUSINESS_TRANSACTION_TABLES",
    "BUSY_TIMEOUT_ENV",
    "CATALOG_LEDGER_TABLES",
    "ConnectionProfile",
    "CrashPoint",
    "FilesystemInfo",
    "KernelCommitResult",
    "KernelReceipt",
    "INITIAL_MIGRATION",
    "IMPORT_LEDGER_TABLES",
    "KERNEL_MIGRATION",
    "LEGACY_CATALOG_SEAL_MIGRATION",
    "LEGACY_IMPORT_LEDGER_MIGRATION",
    "LEGACY_REPOSITORY_PROOF_MIGRATION",
    "LOCAL_REVISION_MIGRATION",
    "LOCAL_REVISION_PREFLIGHT_PREDICATE",
    "MIN_SQLITE_VERSION",
    "MIGRATIONS",
    "Migration",
    "PRIVATE_TABLES",
    "PRODUCTION_AUTHORITY_WORKER_MIGRATION",
    "PRODUCTION_RUNTIME_TABLES",
    "PRODUCTION_RUNTIME_V8_TABLES",
    "REPOSITORY_PROOF_TABLES",
    "RUNTIME_SCHEMA_HARDENING_MIGRATION",
    "WORKER_PAYLOAD_PROJECTION_MIGRATION",
    "WORKER_PROJECTION_SIDECAR_TABLES",
    "ReconcileResult",
    "ReconcileStatus",
    "SQLiteKernelError",
    "SQLiteSettings",
    "SQLITE_IMPORT_TRANSACTION_KIND",
    "SQLITE_IMPORT_TRANSACTION_PREFIX",
    "SQLITE_CATALOG_STATE_HISTORICAL_FENCED",
    "SQLITE_CATALOG_STATE_KEY",
    "SqliteDatabase",
    "StatementResult",
    "UnitOfWork",
    "probe_local_filesystem",
]
