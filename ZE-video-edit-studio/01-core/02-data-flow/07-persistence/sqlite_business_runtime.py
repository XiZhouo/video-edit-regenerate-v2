"""Private admission checks for the schema-v6 SQLite business runtime.

The generic :class:`sqlite_kernel.SqliteDatabase` deliberately continues to
accept only the legacy-authority, unmounted schema-v6 state.  This leaf is the
separate boundary used by future private business readers and writers *after*
a controlled cutover has already installed an active runtime row.  It never
creates that row, changes authority, or mounts itself into the SDK.

Snapshot, operation and manifest payloads are not decoded here; their
dedicated codecs own semantic verification.  The transaction-scoped helper is
an indexed fast fence: it proves the exact schema/security identity and the
current repository control row without scanning historical business rows.
Full page, foreign-key and receipt-history audits live in
``sqlite_business_audit`` and run only through an explicit startup/periodic
entrypoint.
"""

from __future__ import annotations

import platform
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from business_transaction_contract import AuthorityBinding
from sqlite_kernel import (
    APPLICATION_ID,
    SQLiteKernelError,
    SqliteDatabase,
    _OwnedConnection,
    _pragma_int,
)
from sqlite_schema import (
    BUSINESS_TRANSACTION_MIGRATION,
    MAX_BUSINESS_RUNTIME_GENERATION_BYTES,
    MIGRATIONS,
)


_CANONICAL_UTC_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$"
)
_LOWER_HEX = frozenset("0123456789abcdef")
_MAX_SQLITE_INTEGER = 2**63 - 1


class BusinessRuntimeAccess(str, Enum):
    """The two admission policies supported by the private runtime leaf."""

    WRITER = "writer"
    READER = "reader"


class BusinessAuthorityState(str, Enum):
    """Exact persisted values; ``repair_required`` is intentionally fail-closed."""

    ACTIVE = "active"
    READ_ONLY = "read_only"
    REPAIR_REQUIRED = "repair_required"


@dataclass(frozen=True)
class BusinessRuntimeProfile:
    """Immutable repository fence observed from one validated SQLite snapshot."""

    authority: str
    authority_state: BusinessAuthorityState
    repository_epoch: str
    cutover_id: str
    source_admission_revision: int
    repository_revision: int
    state_revision: int
    runtime_generation: str
    code_digest: bytes = field(repr=False)
    schema_version: int
    updated_at: str

    def to_authority_binding(self) -> AuthorityBinding:
        """Project the validated writer fence into the pure transaction contract."""

        if self.authority_state is not BusinessAuthorityState.ACTIVE:
            _fail(
                "sqlite_business_authority_state_forbidden",
                "only an active business runtime can issue a writer binding",
            )
        return AuthorityBinding(
            authority=self.authority,
            authority_state=self.authority_state.value,
            authority_epoch=self.repository_epoch,
            source_admission_revision=self.source_admission_revision,
            authority_revision=self.repository_revision,
            control_revision=self.state_revision,
            cutover_id=self.cutover_id,
            runtime_generation=self.runtime_generation,
            code_digest=self.code_digest,
        )


def _fail(code: str, message: str) -> None:
    raise SQLiteKernelError(code, message)


def _canonical_utc(value: Any) -> bool:
    if type(value) is not str or _CANONICAL_UTC_RE.fullmatch(value) is None:
        return False
    try:
        instant = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError:
        return False
    return (
        instant.strftime("%Y-%m-%dT%H:%M:%S.")
        + "%03dZ" % (instant.microsecond // 1000)
        == value
    )


def _lower_hex(value: Any, length: int) -> bool:
    return (
        type(value) is str
        and len(value) == length
        and all(character in _LOWER_HEX for character in value)
    )


def _validate_access(value: Any) -> BusinessRuntimeAccess:
    if type(value) is not BusinessRuntimeAccess:
        _fail(
            "sqlite_business_access_invalid",
            "business runtime access must be an exact BusinessRuntimeAccess",
        )
    return value


def _verify_security_profile(
    database: SqliteDatabase,
    connection: _OwnedConnection,
) -> None:
    journal_row = connection.execute("PRAGMA journal_mode").fetchone()
    trusted_row = connection.execute("PRAGMA trusted_schema").fetchone()
    encoding_row = connection.execute("PRAGMA encoding").fetchone()
    if journal_row is None or trusted_row is None or encoding_row is None:
        _fail(
            "sqlite_feature_unsupported",
            "required SQLite security PRAGMA is unavailable",
        )
    if (
        str(encoding_row[0]).upper() != "UTF-8"
        or str(journal_row[0]).lower() != "wal"
        or _pragma_int(connection, "synchronous") != 2
        or _pragma_int(connection, "foreign_keys") != 1
        or int(trusted_row[0]) != 0
        or _pragma_int(connection, "busy_timeout")
        != database.settings.busy_timeout_ms
    ):
        _fail(
            "sqlite_profile_mismatch",
            "business runtime connection does not have the fixed safety profile",
        )
    if platform.system() == "Darwin" and (
        _pragma_int(connection, "fullfsync") != 1
        or _pragma_int(connection, "checkpoint_fullfsync") != 1
    ):
        _fail(
            "sqlite_profile_mismatch",
            "business runtime connection is missing Darwin durability controls",
        )


def _verify_repository_marker(connection: _OwnedConnection) -> None:
    rows = connection.execute(
        "SELECT singleton, authority, created_at "
        "FROM kernel_repository ORDER BY singleton"
    ).fetchall()
    if len(rows) != 1:
        _fail(
            "sqlite_business_authority_invalid",
            "business runtime requires exactly one repository marker",
        )
    row = rows[0]
    if (
        type(row["singleton"]) is not int
        or row["singleton"] != 1
        or type(row["authority"]) is not str
        or row["authority"] != "sqlite"
        or not _canonical_utc(row["created_at"])
    ):
        _fail(
            "sqlite_business_authority_invalid",
            "business runtime repository marker is not canonical sqlite authority",
        )


def _read_runtime_profile(
    connection: _OwnedConnection,
    access: BusinessRuntimeAccess,
) -> BusinessRuntimeProfile:
    rows = connection.execute(
        """
        SELECT singleton, authority, authority_state, repository_epoch,
               cutover_id, source_admission_revision, repository_revision,
               state_revision, runtime_generation, code_digest,
               schema_version, updated_at
        FROM repository_runtime_control
        ORDER BY singleton
        """
    ).fetchall()
    if len(rows) != 1:
        _fail(
            "sqlite_business_runtime_control_invalid",
            "business runtime requires exactly one control row",
        )
    row = rows[0]
    state_value = row["authority_state"]
    try:
        state = BusinessAuthorityState(state_value)
    except (TypeError, ValueError):
        _fail(
            "sqlite_business_runtime_control_invalid",
            "business runtime authority state is invalid",
        )
        raise AssertionError("unreachable")

    runtime_generation = row["runtime_generation"]
    runtime_bytes = b""
    if type(runtime_generation) is str:
        try:
            runtime_bytes = runtime_generation.encode("utf-8", "strict")
        except UnicodeEncodeError:
            runtime_bytes = b""
    if (
        type(row["singleton"]) is not int
        or row["singleton"] != 1
        or type(row["authority"]) is not str
        or row["authority"] != "sqlite"
        or type(state_value) is not str
        or not _lower_hex(row["repository_epoch"], 64)
        or not _lower_hex(row["cutover_id"], 64)
        or type(row["source_admission_revision"]) is not int
        or not 1 <= row["source_admission_revision"] <= _MAX_SQLITE_INTEGER
        or type(row["repository_revision"]) is not int
        or not 1 <= row["repository_revision"] <= _MAX_SQLITE_INTEGER
        or type(row["state_revision"]) is not int
        or not 1 <= row["state_revision"] <= _MAX_SQLITE_INTEGER
        or type(runtime_generation) is not str
        or not 1 <= len(runtime_bytes) <= MAX_BUSINESS_RUNTIME_GENERATION_BYTES
        or type(row["code_digest"]) is not bytes
        or len(row["code_digest"]) != 32
        or type(row["schema_version"]) is not int
        # This column versions the runtime-control row format introduced by
        # schema v6.  Later compatibility migrations are proven separately by
        # PRAGMA user_version plus the exact schema fingerprint above.
        or row["schema_version"] != BUSINESS_TRANSACTION_MIGRATION.version
        or not _canonical_utc(row["updated_at"])
    ):
        _fail(
            "sqlite_business_runtime_control_invalid",
            "business runtime control row is not canonical",
        )
    # ``repository_revision`` is UNIQUE, so the newest durable commit is an
    # indexed O(log N) lookup rather than a history-wide count/aggregate.
    commit_revision = connection.execute(
        "SELECT repository_revision FROM business_commits "
        "ORDER BY repository_revision DESC LIMIT 1"
    ).fetchone()
    # An empty/deleted-only cutover has no business commit row, but its
    # runtime still has the canonical bootstrap high-water of 1.  Treating
    # ``None`` as "nothing to compare" would let any forged revision pass the
    # fast writer fence until the first real commit.
    expected_repository_revision = (
        1
        if commit_revision is None
        else commit_revision["repository_revision"]
    )
    if (
        type(expected_repository_revision) is not int
        or expected_repository_revision != row["repository_revision"]
    ):
        _fail(
            "sqlite_business_runtime_revision_invalid",
            "runtime revision does not close the durable commit history",
        )
    if access is BusinessRuntimeAccess.WRITER and state is not BusinessAuthorityState.ACTIVE:
        _fail(
            "sqlite_business_authority_state_forbidden",
            "business writer requires authority_state=active",
        )

    return BusinessRuntimeProfile(
        authority=row["authority"],
        authority_state=state,
        repository_epoch=row["repository_epoch"],
        cutover_id=row["cutover_id"],
        source_admission_revision=row["source_admission_revision"],
        repository_revision=row["repository_revision"],
        state_revision=row["state_revision"],
        runtime_generation=runtime_generation,
        code_digest=row["code_digest"],
        schema_version=row["schema_version"],
        updated_at=row["updated_at"],
    )


def _validate_business_runtime_connection(
    database: SqliteDatabase,
    connection: _OwnedConnection,
    *,
    access: BusinessRuntimeAccess,
) -> BusinessRuntimeProfile:
    """Return the indexed runtime fence inside the caller's SQLite snapshot.

    A writer should call this immediately after ``BEGIN IMMEDIATE``.  A reader
    may call it inside an ordinary ``BEGIN`` snapshot.  When invoked in
    autocommit mode the helper owns and rolls back a temporary read snapshot.
    This hot-path helper deliberately performs no page, global foreign-key or
    receipt-history scan; callers that mount or periodically audit a database
    must use :mod:`sqlite_business_audit` explicitly.
    """

    if not isinstance(database, SqliteDatabase) or not isinstance(
        connection, _OwnedConnection
    ):
        raise TypeError("database and connection must be private SQLite kernel types")
    access = _validate_access(access)
    owns_snapshot = not connection.in_transaction
    if owns_snapshot:
        connection.execute("BEGIN")
    try:
        if _pragma_int(connection, "application_id") != APPLICATION_ID:
            _fail(
                "sqlite_application_id_mismatch",
                "database is not an initialized ZE kernel",
            )
        if _pragma_int(connection, "user_version") != MIGRATIONS[-1].version:
            _fail(
                "sqlite_schema_version_unsupported",
                "business runtime requires the current schema version",
            )
        _verify_security_profile(database, connection)
        database._verify_schema(connection)
        _verify_repository_marker(connection)
        profile = _read_runtime_profile(connection, access)
        return profile
    except sqlite3.Error as exc:
        raise SQLiteKernelError(
            "sqlite_business_runtime_unreadable",
            "business runtime structural state could not be inspected",
        ) from exc
    finally:
        if owns_snapshot and connection.in_transaction:
            connection.rollback()


def inspect_business_runtime(
    database: SqliteDatabase,
    *,
    access: BusinessRuntimeAccess = BusinessRuntimeAccess.READER,
) -> BusinessRuntimeProfile:
    """Preserve the historical full-inspection semantics for explicit callers.

    Transaction hot paths call ``_validate_business_runtime_connection``
    directly.  This public-by-name inspection helper remains a full startup
    audit so existing diagnostics do not silently become weaker.
    """

    # Lazy import avoids a module cycle: the full-audit leaf consumes the fast
    # validator above, while this compatibility entrypoint delegates to it.
    from sqlite_business_audit import startup_business_runtime_full_audit

    return startup_business_runtime_full_audit(database, access=access)


# Private core leaf: consumers import exact names deliberately; nothing is
# re-exported through a wildcard or the public SDK surface.
__all__ = ()
