"""Neutral transaction-scoped verification for SQLite cutover consumers.

This production-private leaf owns the bounded, read-only facts shared by the
detached durable auditor and fixed-SQL cutover writers.  It knows neither
auditor states nor writer capabilities: callers translate its typed failures
into their own five-state result without reimplementing budgets, namespace
proofs, process-control preservation, or storage-unavailability policy.
"""

from __future__ import annotations

from dataclasses import dataclass
import hmac
import sqlite3
from typing import Any, Iterable, Optional, Sequence

from sqlite_business_runtime import _canonical_utc
from sqlite_kernel import SQLiteKernelError
from sqlite_schema import PRIVATE_TABLES


MAX_CUTOVER_DURABLE_AUDIT_ENTRIES = 1_000_000
MAX_CUTOVER_DURABLE_AUDIT_BYTES = 512 * 1024 * 1024
MAX_CUTOVER_DURABLE_AUDIT_VM_STEPS = 100_000_000

_LOWER_HEX = frozenset("0123456789abcdef")

_PRODUCTION_RECEIPT_PREFIXES = (
    "_business-cutover-install:",
    "_business-cutover-join:",
    "_business-cutover-asset-stage:",
    "_business-cutover-asset-coverage:",
    "_repository-audit:",
    "_business-cutover-activation:",
    "_business-cutover-mount:",
    "_worker-execution:",
    "_pipeline-business-control:",
    "ze-business:",
)
_PRODUCTION_RECEIPT_KINDS = (
    "_business_cutover_install_v1",
    "_business_cutover_join_v1",
    "_business_cutover_asset_stage_v1",
    "_business_cutover_asset_coverage_v1",
    "_repository_audit_certificate_v1",
    "_business_cutover_activation_v2",
    "_business_cutover_mount_v2",
    "_worker_execution_v3",
    "_pipeline_business_control_v2",
    "ze_business_commit_v1",
)
_PRODUCTION_RECEIPT_PREFIX_BY_KIND = dict(zip(
    _PRODUCTION_RECEIPT_KINDS,
    _PRODUCTION_RECEIPT_PREFIXES,
))
_LEGACY_RECEIPT_PREFIXES = (
    "legacy-import:",
    "_legacy-catalog-seal:",
    "_legacy-repository-seal:",
)
_LEGACY_RECEIPT_KINDS = (
    "legacy_import_v1",
    "_legacy_catalog_seal_v1",
    "_legacy_repository_seal_v1",
)


@dataclass(frozen=True)
class SQLiteCutoverVerificationBudget:
    entries: int
    payload_bytes: int

    def __post_init__(self) -> None:
        if (
            type(self.entries) is not int
            or self.entries < 0
            or type(self.payload_bytes) is not int
            or self.payload_bytes < 0
        ):
            raise TypeError("audit budget values must be non-negative integers")


class _CutoverVerificationCorrupt(RuntimeError):
    pass


class _CutoverVerificationUnsupported(RuntimeError):
    pass


class _CutoverVerificationBudgetExceeded(RuntimeError):
    def __init__(self, code: str, budget: SQLiteCutoverVerificationBudget) -> None:
        self.budget = budget
        super().__init__(code)


class _CutoverVerificationUnavailable(RuntimeError):
    pass


def _same(left: Any, right: Any) -> bool:
    if type(left) is bytes and type(right) is bytes:
        return hmac.compare_digest(left, right)
    return type(left) is type(right) and left == right


def _row_tuple(row: Any) -> tuple:
    if row is None:
        raise _CutoverVerificationCorrupt("cutover_durable_audit_row_missing")
    return tuple(row[index] for index in range(len(row)))


def _rows_exact(
    observed: Iterable[Sequence[Any]],
    expected: Iterable[Sequence[Any]],
) -> bool:
    left = tuple(tuple(row) for row in observed)
    right = tuple(tuple(row) for row in expected)
    return len(left) == len(right) and all(
        len(a) == len(b) and all(_same(a[index], b[index]) for index in range(len(b)))
        for a, b in zip(left, right)
    )


class _CutoverVerificationProgressBudget:
    __slots__ = ("failure", "remaining")

    def __init__(self, remaining: int) -> None:
        self.remaining = remaining
        self.failure: Optional[BaseException] = None

    def __call__(self) -> int:
        try:
            self.remaining -= 1
            return int(self.remaining < 0)
        except BaseException as exc:
            self.failure = exc
            return 1


def _install_cutover_verification_progress_budget(
    connection: Any,
) -> _CutoverVerificationProgressBudget:
    interval = 10_000
    state = _CutoverVerificationProgressBudget(
        MAX_CUTOVER_DURABLE_AUDIT_VM_STEPS // interval
    )
    connection.set_progress_handler(state, interval)
    return state


def _read_cutover_verification_budget(
    connection: Any,
) -> SQLiteCutoverVerificationBudget:
    """Count rows and declared BLOB/TEXT payload before semantic decoding."""

    tables = tuple(sorted(PRIVATE_TABLES))
    fragments = []
    for table in tables:
        columns = connection.execute('PRAGMA table_info("' + table + '")').fetchall()
        variable_columns = tuple(
            row[1]
            for row in columns
            if len(row) >= 3
            and type(row[1]) is str
            and str(row[2]).upper() in ("BLOB", "TEXT")
        )
        payload = "+".join(
            'COALESCE(length(CAST("' + column + '" AS BLOB)),0)'
            for column in variable_columns
        ) or "0"
        fragments.append(
            'SELECT count(*) AS entries,COALESCE(sum(' + payload + '),0) AS payload_bytes '
            'FROM "' + table + '"'
        )
    row = connection.execute(
        "SELECT COALESCE(sum(entries),0),COALESCE(sum(payload_bytes),0) FROM ("
        + " UNION ALL ".join(fragments)
        + ")"
    ).fetchone()
    if (
        row is None
        or len(row) != 2
        or type(row[0]) is not int
        or row[0] < 0
        or type(row[1]) is not int
        or row[1] < 0
    ):
        raise _CutoverVerificationCorrupt("cutover_durable_audit_budget_invalid")
    observed = SQLiteCutoverVerificationBudget(row[0], row[1])
    if observed.entries > MAX_CUTOVER_DURABLE_AUDIT_ENTRIES:
        raise _CutoverVerificationBudgetExceeded(
            "cutover_durable_audit_entry_budget_exceeded", observed
        )
    if observed.payload_bytes > MAX_CUTOVER_DURABLE_AUDIT_BYTES:
        raise _CutoverVerificationBudgetExceeded(
            "cutover_durable_audit_byte_budget_exceeded", observed
        )
    return observed


def _reserved_cursor(
    connection: Any,
    prefixes: tuple[str, ...],
    kinds: tuple[str, ...],
) -> Any:
    predicate = " OR ".join(
        "substr(CAST(transaction_id AS TEXT),1,?)=?" for _ in prefixes
    )
    sql = (
        "SELECT transaction_id,kind,committed_at FROM transaction_receipts WHERE "
        + predicate
        + " OR CAST(kind AS TEXT) IN ("
        + ",".join("?" for _ in kinds)
        + ") ORDER BY transaction_id COLLATE BINARY,kind COLLATE BINARY"
    )
    values = tuple(value for prefix in prefixes for value in (len(prefix), prefix)) + kinds
    return connection.execute(sql, values)


def _reserved_stream_exact(
    observed: Any,
    expected: Any,
    *,
    compare_time: bool,
    identity_validator: Any = None,
) -> bool:
    """Compare two canonical receipt streams without materializing history."""

    try:
        while True:
            left = observed.fetchone()
            right = expected.fetchone()
            if left is None:
                return right is None
            left_tuple = _row_tuple(left)
            if len(left_tuple) != 3 or any(
                type(value) is not str for value in left_tuple
            ):
                raise _CutoverVerificationCorrupt(
                    "cutover_durable_audit_reserved_receipt_blob_pollution"
                )
            if right is None:
                return False
            right_tuple = _row_tuple(right)
            if (
                not _canonical_utc(left_tuple[2])
                or identity_validator is not None
                and not identity_validator(left_tuple[0], left_tuple[1])
                or not _rows_exact(
                    (left_tuple if compare_time else left_tuple[:2],),
                    (right_tuple,),
                )
            ):
                return False
    finally:
        observed.close()
        expected.close()


def _verify_legacy_receipt_namespace(connection: Any) -> None:
    observed = _reserved_cursor(
        connection, _LEGACY_RECEIPT_PREFIXES, _LEGACY_RECEIPT_KINDS
    )
    expected = connection.execute(
        "SELECT transaction_id,receipt_kind FROM legacy_import_batches "
        "UNION ALL SELECT transaction_id,receipt_kind FROM legacy_catalog_seals "
        "UNION ALL SELECT transaction_id,receipt_kind FROM legacy_repository_seals "
        "ORDER BY transaction_id COLLATE BINARY,receipt_kind COLLATE BINARY"
    )
    if not _reserved_stream_exact(observed, expected, compare_time=False):
        raise _CutoverVerificationCorrupt(
            "cutover_durable_audit_legacy_receipt_namespace_invalid"
        )


def _verify_production_receipt_namespace(connection: Any) -> None:
    observed = _reserved_cursor(
        connection, _PRODUCTION_RECEIPT_PREFIXES, _PRODUCTION_RECEIPT_KINDS
    )
    expected = connection.execute(
        "SELECT transaction_id,receipt_kind,committed_at FROM business_cutover_install_receipts "
        "UNION ALL SELECT transaction_id,receipt_kind,committed_at FROM business_asset_stage_receipts "
        "UNION ALL SELECT transaction_id,receipt_kind,committed_at FROM business_asset_coverage_receipts "
        "UNION ALL SELECT transaction_id,receipt_kind,committed_at FROM repository_audit_certificates "
        "UNION ALL SELECT transaction_id,receipt_kind,committed_at FROM business_cutover_activation_receipts "
        "UNION ALL SELECT transaction_id,receipt_kind,committed_at FROM business_cutover_mount_receipts "
        "UNION ALL SELECT transaction_id,receipt_kind,committed_at FROM worker_execution_receipts "
        "UNION ALL SELECT transaction_id,receipt_kind,committed_at FROM pipeline_control_receipts "
        "UNION ALL SELECT transaction_id,receipt_kind,committed_at FROM business_commits "
        "ORDER BY transaction_id COLLATE BINARY,receipt_kind COLLATE BINARY"
    )
    if not _reserved_stream_exact(
        observed,
        expected,
        compare_time=True,
        identity_validator=_production_receipt_identity_exact,
    ):
        raise _CutoverVerificationCorrupt(
            "cutover_durable_audit_production_receipt_namespace_invalid"
        )


def _production_receipt_identity_exact(transaction_id: str, kind: str) -> bool:
    prefix = _PRODUCTION_RECEIPT_PREFIX_BY_KIND.get(kind)
    if prefix is None or not transaction_id.startswith(prefix):
        return False
    suffix = transaction_id[len(prefix):]
    if kind == "_business_cutover_asset_stage_v1":
        parts = suffix.split(":")
        return len(parts) == 2 and all(
            type(part) is str
            and len(part) == 64
            and all(character in _LOWER_HEX for character in part)
            for part in parts
        )
    suffix_length = (
        32
        if kind in (
            "_repository_audit_certificate_v1",
            "_worker_execution_v3",
            "_pipeline_business_control_v2",
            "ze_business_commit_v1",
        )
        else 64
    )
    return (
        len(suffix) == suffix_length
        and all(character in _LOWER_HEX for character in suffix)
    )


_UNAVAILABLE_SQLITE_CODES = frozenset(
    value
    for value in (
        getattr(sqlite3, "SQLITE_ABORT", None),
        getattr(sqlite3, "SQLITE_BUSY", None),
        getattr(sqlite3, "SQLITE_CANTOPEN", None),
        getattr(sqlite3, "SQLITE_FULL", None),
        getattr(sqlite3, "SQLITE_INTERRUPT", None),
        getattr(sqlite3, "SQLITE_IOERR", None),
        getattr(sqlite3, "SQLITE_LOCKED", None),
        getattr(sqlite3, "SQLITE_NOMEM", None),
        getattr(sqlite3, "SQLITE_NOLFS", None),
        getattr(sqlite3, "SQLITE_PERM", None),
        getattr(sqlite3, "SQLITE_PROTOCOL", None),
        getattr(sqlite3, "SQLITE_READONLY", None),
    )
    if type(value) is int
)

_UNAVAILABLE_KERNEL_CODES = frozenset((
    "sqlite_busy",
    "sqlite_locked",
    "sqlite_io_error",
    "sqlite_business_runtime_unreadable",
    "sqlite_business_storage_identity_unreadable",
    "sqlite_business_storage_identity_changed",
    "sqlite_database_missing",
    "sqlite_open_failed",
    "sqlite_identity_probe_failed",
    "sqlite_directory_create_failed",
    "sqlite_file_create_failed",
    "sqlite_process_lock_open_failed",
    "sqlite_directory_sync_failed",
    "sqlite_directory_sync_unsupported",
    "sqlite_directory_permissions_failed",
    "sqlite_file_permissions_failed",
    "sqlite_path_stat_failed",
    "sqlite_file_stat_failed",
    "sqlite_storage_missing",
    "sqlite_storage_unsafe",
    "sqlite_sync_failed",
    "sqlite_profile_failed",
))


def _failure_is_unavailable(failure: BaseException) -> bool:
    current: Optional[BaseException] = failure
    seen = set()
    kernel_codes = []
    saw_os_failure = False
    saw_stable_database_failure = False
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, sqlite3.Error):
            code = getattr(current, "sqlite_errorcode", None)
            if type(code) is int:
                return (code & 0xFF) in _UNAVAILABLE_SQLITE_CODES
            if isinstance(current, sqlite3.OperationalError):
                saw_os_failure = True
            elif isinstance(current, sqlite3.DatabaseError):
                saw_stable_database_failure = True
        if isinstance(current, (OSError, MemoryError)):
            saw_os_failure = True
        if isinstance(current, SQLiteKernelError):
            kernel_codes.append(current.code)
        current = current.__cause__ or current.__context__
    if saw_stable_database_failure:
        return False
    return saw_os_failure or any(
        code in _UNAVAILABLE_KERNEL_CODES for code in kernel_codes
    )


__all__ = ()
