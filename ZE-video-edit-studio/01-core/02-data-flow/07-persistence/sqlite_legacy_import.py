"""Strict INSERT-only adapter for one verified legacy terminal shadow.

The adapter consumes a complete immutable, duck-typed row plan.  It never
imports the SDK, reads a package/reservation/record, accepts arbitrary SQL or
hands a connection, cursor, or callback to its caller.  A successful result is
proved by an adapter-specific comparison through a fresh SQLite connection;
the generic kernel receipt is intentionally insufficient evidence.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Iterator, Optional, Sequence, Tuple

from business_codec import BusinessCodecError
from business_snapshot import (
    BusinessSnapshotError,
    BusinessSnapshotSet,
    DocumentSnapshotRow,
    EntitySnapshotRow,
    ProjectSnapshotRow,
    verify_business_snapshot,
)
from legacy_import_integrity import (
    LegacyImportIntegrityError,
    LegacyImportIntegrityProject,
    build_legacy_import_integrity,
)
from legacy_catalog_integrity import LegacyCatalogIntegrityError
from legacy_catalog_contract import (
    SQLITE_CATALOG_STATE_HISTORICAL_FENCED,
    SQLITE_CATALOG_STATE_KEY,
)
from legacy_import_contract import (
    LEGACY_BATCH_PROTOCOL,
    LEGACY_IMPORT_PLAN_PROTOCOL,
    LEGACY_MANIFEST_SCHEMA,
    LEGACY_PAYLOAD_PROTOCOL,
    MAX_LEGACY_ENTITIES,
    MAX_LEGACY_PROJECTS,
    MAX_LEGACY_ROW_SNAPSHOT_BYTES,
    SQLITE_IMPORT_RECEIPT_PROTOCOL,
    SQLITE_IMPORT_RECONCILE_PROTOCOL,
    SQLITE_IMPORT_STATE_KEY,
    SQLITE_IMPORT_STATE_TERMINAL_SHADOW,
    SQLITE_IMPORT_TERMINAL_STATUS,
    SQLITE_IMPORT_TRANSACTION_KIND,
    SQLITE_IMPORT_TRANSACTION_PREFIX,
)
from sqlite_kernel import KernelReceipt, SQLiteKernelError, SqliteDatabase
from sqlite_legacy_catalog_exact import (
    CatalogExactConflict,
    CatalogExactCorrupt,
    project_normalized_import_plan,
    reconcile_existing_catalog,
)


_LOWER_HEX = frozenset("0123456789abcdef")
_COMMITTED_AT_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$"
)
_MAX_SQLITE_INTEGER = 2**63 - 1
_STORAGE_VERSIONS = frozenset(("legacy", "v2"))

_PROJECT_ROW_FIELDS = (
    "project_id",
    "generation",
    "codec_id",
    "codec_version",
    "snapshot_format_version",
    "snapshot_bytes",
    "snapshot_sha256",
)
_DOCUMENT_ROW_FIELDS = (
    "project_id",
    "document_id",
    "document_version",
    "schema_version",
    "collaboration_mode",
    "save_policy",
    "codec_id",
    "codec_version",
    "snapshot_format_version",
    "snapshot_bytes",
    "snapshot_sha256",
)
_ENTITY_ROW_FIELDS = (
    "project_id",
    "document_id",
    "entity_type",
    "entity_id",
    "entity_version",
    "deleted",
    "codec_id",
    "codec_version",
    "snapshot_format_version",
    "snapshot_bytes",
    "snapshot_sha256",
)


class SQLiteLegacyImportError(RuntimeError):
    """Stable, payload-free error at the durable import boundary."""

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
    raise SQLiteLegacyImportError(code, reason)


class LegacyImportReconcileStatus(Enum):
    IMPORTED = "imported"
    NOT_IMPORTED = "not_imported"
    CONFLICT = "conflict"
    CORRUPT = "corrupt"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class LegacyImportReconcileResult:
    protocol: str
    status: LegacyImportReconcileStatus
    import_id: str
    transaction_id: str
    receipt: Optional[KernelReceipt] = None
    error_code: str = ""
    message: str = ""


@dataclass(frozen=True)
class LegacyImportCommitResult:
    import_id: str
    transaction_id: str
    receipt: KernelReceipt
    already_imported: bool = False
    recovered_after_ack_loss: bool = False


@dataclass(frozen=True)
class _ProjectRow:
    project_id: str
    generation: int
    codec_id: str
    codec_version: int
    snapshot_format_version: int
    snapshot_bytes: bytes = field(repr=False)
    snapshot_sha256: bytes = field(repr=False)


@dataclass(frozen=True)
class _DocumentRow:
    project_id: str
    document_id: str
    document_version: int
    schema_version: str
    collaboration_mode: str
    save_policy: Optional[str]
    codec_id: str
    codec_version: int
    snapshot_format_version: int
    snapshot_bytes: bytes = field(repr=False)
    snapshot_sha256: bytes = field(repr=False)


@dataclass(frozen=True)
class _ProjectPlan:
    ordinal: int
    project_id: str
    generation: int
    document_id: str
    document_version: int
    storage_version: str
    source_head_sha256: bytes = field(repr=False)
    source_head_bytes: int
    business_root_sha256: bytes = field(repr=False)
    entity_count: int
    row_snapshot_bytes: int
    project_row: _ProjectRow = field(repr=False)
    document_row: _DocumentRow = field(repr=False)
    # Retain one exact frozen container of the plan-owned tuple rows.  Every
    # later read bypasses subclass hooks through base tuple storage.  The
    # temporary normalized business graph remains project-scoped and is
    # released before the next project, avoiding a second all-batch graph.
    entity_rows: Tuple[Any, ...] = field(repr=False)


@dataclass(frozen=True)
class _ImportPlan:
    import_id: str
    transaction_id: str
    project_count: int
    entity_count: int
    row_snapshot_bytes: int
    framed_payload_bytes: int
    manifest_bytes: bytes = field(repr=False)
    manifest_sha256: bytes = field(repr=False)
    payload_sha256: bytes = field(repr=False)
    batch_sha256: bytes = field(repr=False)
    projects: Tuple[_ProjectPlan, ...] = field(repr=False)


class _ImportAuthorizer:
    """Poison the transaction after any non-INSERT mutation attempt."""

    _INSERT_TABLES = frozenset(
        (
            "kernel_metadata",
            "projects",
            "documents",
            "entities",
            "transaction_receipts",
            "legacy_import_batches",
            "legacy_import_projects",
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
_INSERT_PROJECT_SQL = """
    INSERT INTO projects(
        repository_singleton, project_id, generation, codec_id, codec_version,
        snapshot_format_version, snapshot_bytes, snapshot_sha256
    ) VALUES (1, ?, ?, ?, ?, ?, ?, ?)
"""
_INSERT_DOCUMENT_SQL = """
    INSERT INTO documents(
        project_id, document_id, document_version, schema_version,
        collaboration_mode, save_policy, codec_id, codec_version,
        snapshot_format_version, snapshot_bytes, snapshot_sha256
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""
_INSERT_ENTITY_SQL = """
    INSERT INTO entities(
        project_id, document_id, entity_type, entity_id, entity_version,
        deleted, codec_id, codec_version, snapshot_format_version,
        snapshot_bytes, snapshot_sha256
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""
_INSERT_RECEIPT_SQL = """
    INSERT INTO transaction_receipts(transaction_id, kind, committed_at)
    VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
"""
_INSERT_BATCH_SQL = """
    INSERT INTO legacy_import_batches(
        import_id, transaction_id, receipt_kind, receipt_protocol, status,
        manifest_schema, batch_protocol, payload_protocol, selection,
        project_count, entity_count, row_snapshot_bytes,
        framed_payload_bytes, manifest_bytes, manifest_sha256,
        payload_sha256, batch_sha256
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""
_INSERT_DESCRIPTOR_SQL = """
    INSERT INTO legacy_import_projects(
        import_id, ordinal, project_id, generation, document_id,
        document_version, storage_version, source_head_sha256,
        source_head_bytes, business_root_sha256, entity_count,
        row_snapshot_bytes
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

# SQL grammar responsibility lives here, before SQLite's action authorizer:
# SQLite reports INSERT OR REPLACE as SQLITE_INSERT and does not expose its
# implicit conflict-delete as SQLITE_DELETE.  Exact fixed-statement identity
# therefore rejects REPLACE/upsert/mutated SQL; the authorizer independently
# limits tables, direct UPDATE/DELETE and control statements.
_FIXED_INSERT_SQL = frozenset(
    (
        _INSERT_MARKER_SQL,
        _INSERT_PROJECT_SQL,
        _INSERT_DOCUMENT_SQL,
        _INSERT_ENTITY_SQL,
        _INSERT_RECEIPT_SQL,
        _INSERT_BATCH_SQL,
        _INSERT_DESCRIPTOR_SQL,
    )
)

_SELECT_BATCH_SQL = """
    SELECT import_id, transaction_id, receipt_kind, receipt_protocol, status,
           manifest_schema, batch_protocol, payload_protocol, selection,
           project_count, entity_count, row_snapshot_bytes,
           framed_payload_bytes, manifest_bytes, manifest_sha256,
           payload_sha256, batch_sha256
    FROM legacy_import_batches WHERE import_id = ?
"""
_SELECT_DESCRIPTORS_SQL = """
    SELECT import_id, ordinal, project_id, generation, document_id,
           document_version, storage_version, source_head_sha256,
           source_head_bytes, business_root_sha256, entity_count,
           row_snapshot_bytes
    FROM legacy_import_projects WHERE import_id = ? ORDER BY ordinal
"""
_SELECT_PROJECTS_SQL = """
    SELECT p.repository_singleton, p.project_id, p.generation, p.codec_id,
           p.codec_version, p.snapshot_format_version, p.snapshot_bytes,
           p.snapshot_sha256
    FROM projects AS p
    JOIN legacy_import_projects AS i ON i.project_id = p.project_id
    WHERE i.import_id = ? ORDER BY i.ordinal
"""
_SELECT_DOCUMENTS_SQL = """
    SELECT d.project_id, d.document_id, d.document_version, d.schema_version,
           d.collaboration_mode, d.save_policy, d.codec_id, d.codec_version,
           d.snapshot_format_version, d.snapshot_bytes, d.snapshot_sha256
    FROM documents AS d
    JOIN legacy_import_projects AS i
      ON i.project_id = d.project_id AND i.document_id = d.document_id
    WHERE i.import_id = ? ORDER BY i.ordinal
"""
_SELECT_ENTITIES_SQL = """
    SELECT e.project_id, e.document_id, e.entity_type, e.entity_id,
           e.entity_version, e.deleted, e.codec_id, e.codec_version,
           e.snapshot_format_version, e.snapshot_bytes, e.snapshot_sha256
    FROM entities AS e
    JOIN legacy_import_projects AS i
      ON i.project_id = e.project_id AND i.document_id = e.document_id
    WHERE i.import_id = ?
    ORDER BY i.ordinal, e.entity_type, e.entity_id
"""


def _read(value: Any, name: str) -> Any:
    try:
        return getattr(value, name)
    except Exception:
        _fail("sqlite_legacy_import_plan_invalid", "field_unreadable")


def _exact_text(value: Any, *, nonempty: bool = True, maximum: int = 0) -> str:
    if type(value) is not str or (nonempty and not value):
        _fail("sqlite_legacy_import_plan_invalid", "text_invalid")
    try:
        value.encode("utf-8", "strict")
    except UnicodeEncodeError:
        _fail("sqlite_legacy_import_plan_invalid", "text_invalid")
    if maximum and len(value) > maximum:
        _fail("sqlite_legacy_import_plan_invalid", "text_invalid")
    return value


def _positive_int(value: Any) -> int:
    if type(value) is not int or value < 1 or value > _MAX_SQLITE_INTEGER:
        _fail("sqlite_legacy_import_plan_invalid", "integer_invalid")
    return value


def _nonnegative_int(value: Any) -> int:
    if type(value) is not int or value < 0 or value > _MAX_SQLITE_INTEGER:
        _fail("sqlite_legacy_import_plan_invalid", "integer_invalid")
    return value


def _digest(value: Any) -> bytes:
    if type(value) is not bytes or len(value) != 32:
        _fail("sqlite_legacy_import_plan_invalid", "digest_invalid")
    return value


def _snapshot_bytes(value: Any) -> bytes:
    if type(value) is not bytes or not value:
        _fail("sqlite_legacy_import_plan_invalid", "snapshot_invalid")
    return value


def _named_row(value: Any, fields: Tuple[str, ...]) -> Tuple[Any, ...]:
    if not isinstance(value, tuple):
        _fail("sqlite_legacy_import_plan_invalid", "row_shape_invalid")
    try:
        # Read the NamedTuple declaration from class dictionaries, and values
        # from tuple storage.  A tuple subclass may override ``__iter__``,
        # ``__len__`` or a field property; none of those hooks may make
        # validation see A while DML/reconcile later sees B.
        declared_fields = None
        row_type = type(value)
        for base in type.__getattribute__(row_type, "__mro__"):
            namespace = type.__getattribute__(base, "__dict__")
            if "_fields" in namespace:
                declared_fields = namespace["_fields"]
                break
        if (
            type(declared_fields) is not tuple
            or declared_fields != fields
            or tuple.__len__(value) != len(fields)
        ):
            _fail("sqlite_legacy_import_plan_invalid", "row_shape_invalid")
        return tuple(
            tuple.__getitem__(value, index) for index in range(len(fields))
        )
    except SQLiteLegacyImportError:
        raise
    except Exception:
        _fail("sqlite_legacy_import_plan_invalid", "row_unreadable")


def _project_row(value: Any) -> _ProjectRow:
    values = _named_row(value, _PROJECT_ROW_FIELDS)
    return _ProjectRow(
        project_id=_exact_text(values[0], maximum=160),
        generation=_positive_int(values[1]),
        codec_id=_exact_text(values[2], maximum=64),
        codec_version=_positive_int(values[3]),
        snapshot_format_version=_positive_int(values[4]),
        snapshot_bytes=_snapshot_bytes(values[5]),
        snapshot_sha256=_digest(values[6]),
    )


def _document_row(value: Any) -> _DocumentRow:
    values = _named_row(value, _DOCUMENT_ROW_FIELDS)
    save_policy = values[5]
    if save_policy is not None:
        save_policy = _exact_text(save_policy)
    return _DocumentRow(
        project_id=_exact_text(values[0], maximum=160),
        document_id=_exact_text(values[1]),
        document_version=_positive_int(values[2]),
        schema_version=_exact_text(values[3]),
        collaboration_mode=_exact_text(values[4]),
        save_policy=save_policy,
        codec_id=_exact_text(values[6], maximum=64),
        codec_version=_positive_int(values[7]),
        snapshot_format_version=_positive_int(values[8]),
        snapshot_bytes=_snapshot_bytes(values[9]),
        snapshot_sha256=_digest(values[10]),
    )


def _entity_row(value: Any) -> EntitySnapshotRow:
    values = _named_row(value, _ENTITY_ROW_FIELDS)
    deleted = values[5]
    if type(deleted) is not int or deleted not in (0, 1):
        _fail("sqlite_legacy_import_plan_invalid", "deleted_invalid")
    return EntitySnapshotRow(
        project_id=_exact_text(values[0], maximum=160),
        document_id=_exact_text(values[1]),
        entity_type=_exact_text(values[2]),
        entity_id=_exact_text(values[3]),
        entity_version=_positive_int(values[4]),
        deleted=deleted,
        codec_id=_exact_text(values[6], maximum=64),
        codec_version=_positive_int(values[7]),
        snapshot_format_version=_positive_int(values[8]),
        snapshot_bytes=_snapshot_bytes(values[9]),
        snapshot_sha256=_digest(values[10]),
    )


def _project_plan(
    value: Any,
    ordinal: int,
    remaining_entities: int,
    remaining_row_bytes: int,
) -> _ProjectPlan:
    actual_ordinal = _nonnegative_int(_read(value, "ordinal"))
    if actual_ordinal != ordinal:
        _fail("sqlite_legacy_import_plan_invalid", "ordinal_invalid")
    raw_entity_source = _read(value, "entity_rows")
    if not isinstance(raw_entity_source, tuple):
        _fail("sqlite_legacy_import_plan_invalid", "entity_rows_invalid")
    # Freeze the container through tuple's own iterator.  SDK's supported
    # _EntityRowIntents is a tuple subclass; arbitrary subclass iteration and
    # length hooks must not change the rows between preflight and DML.
    raw_entities = tuple(tuple.__iter__(raw_entity_source))
    declared_entity_count = _nonnegative_int(_read(value, "entity_count"))
    declared_row_bytes = _positive_int(_read(value, "row_snapshot_bytes"))
    if (
        len(raw_entities) != declared_entity_count
        or declared_entity_count > remaining_entities
        or declared_row_bytes > remaining_row_bytes
    ):
        _fail("sqlite_legacy_import_plan_invalid", "batch_budget_exceeded")
    # Exactly one normalized EntitySnapshotRow graph is project-scoped and
    # released before the next project; do not create an intermediate adapter
    # dataclass graph and then clone it again for BusinessSnapshotSet.
    entities = tuple(_entity_row(row) for row in raw_entities)
    project = _project_row(_read(value, "project_row"))
    document = _document_row(_read(value, "document_row"))
    plan = _ProjectPlan(
        ordinal=ordinal,
        project_id=_exact_text(_read(value, "project_id"), maximum=160),
        generation=_positive_int(_read(value, "generation")),
        document_id=_exact_text(_read(value, "document_id")),
        document_version=_positive_int(_read(value, "document_version")),
        storage_version=_exact_text(_read(value, "storage_version")),
        source_head_sha256=_digest(_read(value, "source_head_sha256")),
        source_head_bytes=_positive_int(_read(value, "source_head_bytes")),
        business_root_sha256=_digest(_read(value, "business_root_sha256")),
        entity_count=declared_entity_count,
        row_snapshot_bytes=declared_row_bytes,
        project_row=project,
        document_row=document,
        entity_rows=raw_entities,
    )
    if plan.storage_version not in _STORAGE_VERSIONS:
        _fail("sqlite_legacy_import_plan_invalid", "storage_version_invalid")
    if (
        plan.project_id != project.project_id
        or plan.generation != project.generation
        or document.project_id != plan.project_id
        or plan.document_id != document.document_id
        or plan.document_version != document.document_version
        or plan.business_root_sha256 != project.snapshot_sha256
        or plan.entity_count != len(entities)
    ):
        _fail("sqlite_legacy_import_plan_invalid", "project_binding_invalid")
    previous_key: Optional[Tuple[str, str]] = None
    for row in entities:
        key = (row.entity_type, row.entity_id)
        if (
            row.project_id != plan.project_id
            or row.document_id != plan.document_id
            or (previous_key is not None and key <= previous_key)
        ):
            _fail("sqlite_legacy_import_plan_invalid", "entity_binding_invalid")
        previous_key = key
    actual_row_bytes = (
        len(project.snapshot_bytes)
        + len(document.snapshot_bytes)
        + sum(len(row.snapshot_bytes) for row in entities)
    )
    if plan.row_snapshot_bytes != actual_row_bytes:
        _fail("sqlite_legacy_import_plan_invalid", "row_bytes_invalid")
    snapshot = BusinessSnapshotSet(
        project=ProjectSnapshotRow(
            project_id=project.project_id,
            generation=project.generation,
            codec_id=project.codec_id,
            codec_version=project.codec_version,
            snapshot_format_version=project.snapshot_format_version,
            snapshot_bytes=project.snapshot_bytes,
            snapshot_sha256=project.snapshot_sha256,
        ),
        document=DocumentSnapshotRow(
            project_id=document.project_id,
            document_id=document.document_id,
            document_version=document.document_version,
            schema_version=document.schema_version,
            collaboration_mode=document.collaboration_mode,
            save_policy=document.save_policy,
            codec_id=document.codec_id,
            codec_version=document.codec_version,
            snapshot_format_version=document.snapshot_format_version,
            snapshot_bytes=document.snapshot_bytes,
            snapshot_sha256=document.snapshot_sha256,
        ),
        entities=entities,
    )
    try:
        verify_business_snapshot(snapshot)
    except BusinessSnapshotError:
        _fail("sqlite_legacy_import_plan_invalid", "business_snapshot_invalid")
    return plan


def _entity_snapshot_payloads(rows: Tuple[Any, ...]) -> Iterator[bytes]:
    # A generator argument is bound per call, unlike a nested comprehension's
    # late-bound project variable.  This stays streaming across many projects.
    for row in rows:
        yield _entity_values(row)[9]


def _project_plan_graph(value: Any) -> _ImportPlan:
    fixed = {
        "plan_protocol": LEGACY_IMPORT_PLAN_PROTOCOL,
        "receipt_kind": SQLITE_IMPORT_TRANSACTION_KIND,
        "receipt_protocol": SQLITE_IMPORT_RECEIPT_PROTOCOL,
        "status": SQLITE_IMPORT_TERMINAL_STATUS,
        "manifest_schema": LEGACY_MANIFEST_SCHEMA,
        "batch_protocol": LEGACY_BATCH_PROTOCOL,
        "payload_protocol": LEGACY_PAYLOAD_PROTOCOL,
        "selection": "explicit_projects",
    }
    for name, expected in fixed.items():
        actual = _read(value, name)
        if type(actual) is not str or actual != expected:
            _fail("sqlite_legacy_import_plan_invalid", "protocol_invalid")
    import_id = _read(value, "import_id")
    if (
        type(import_id) is not str
        or len(import_id) != 64
        or any(character not in _LOWER_HEX for character in import_id)
    ):
        _fail("sqlite_legacy_import_plan_invalid", "import_id_invalid")
    transaction_id = _read(value, "transaction_id")
    if (
        type(transaction_id) is not str
        or transaction_id != SQLITE_IMPORT_TRANSACTION_PREFIX + import_id
    ):
        _fail("sqlite_legacy_import_plan_invalid", "transaction_id_invalid")
    raw_projects = _read(value, "projects")
    if type(raw_projects) is not tuple or not raw_projects:
        _fail("sqlite_legacy_import_plan_invalid", "projects_invalid")
    if len(raw_projects) > MAX_LEGACY_PROJECTS:
        _fail("sqlite_legacy_import_plan_invalid", "project_budget_exceeded")
    projected_projects = []
    entity_count = 0
    row_bytes = 0
    for ordinal, project in enumerate(raw_projects):
        projected = _project_plan(
            project,
            ordinal,
            MAX_LEGACY_ENTITIES - entity_count,
            MAX_LEGACY_ROW_SNAPSHOT_BYTES - row_bytes,
        )
        projected_projects.append(projected)
        entity_count += projected.entity_count
        row_bytes += projected.row_snapshot_bytes
    projects = tuple(projected_projects)
    previous_project_id: Optional[str] = None
    document_ids = set()
    for project in projects:
        if (
            previous_project_id is not None
            and project.project_id <= previous_project_id
        ) or project.document_id in document_ids:
            _fail("sqlite_legacy_import_plan_invalid", "project_order_invalid")
        previous_project_id = project.project_id
        document_ids.add(project.document_id)

    try:
        integrity = build_legacy_import_integrity(
            import_id,
            tuple(
                LegacyImportIntegrityProject(
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
                    project_snapshot_bytes=project.project_row.snapshot_bytes,
                    document_snapshot_bytes=project.document_row.snapshot_bytes,
                    entity_snapshot_bytes=_entity_snapshot_payloads(
                        project.entity_rows
                    ),
                )
                for project in projects
            ),
        )
    except (LegacyImportIntegrityError, BusinessCodecError):
        _fail("sqlite_legacy_import_plan_invalid", "integrity_invalid")

    expected_scalars = (
        ("project_count", len(projects)),
        ("entity_count", integrity.entity_count),
        ("row_snapshot_bytes", integrity.row_snapshot_bytes),
        ("framed_payload_bytes", integrity.framed_payload_bytes),
    )
    for name, expected in expected_scalars:
        actual = _read(value, name)
        if type(actual) is not int or actual != expected:
            _fail("sqlite_legacy_import_plan_invalid", "aggregate_invalid")
    expected_bytes = (
        ("manifest_bytes", integrity.manifest_bytes),
        ("manifest_sha256", integrity.manifest_sha256),
        ("payload_sha256", integrity.payload_sha256),
        ("batch_sha256", integrity.batch_sha256),
    )
    for name, expected in expected_bytes:
        actual = _read(value, name)
        if type(actual) is not bytes or actual != expected:
            _fail("sqlite_legacy_import_plan_invalid", "digest_binding_invalid")

    return _ImportPlan(
        import_id=import_id,
        transaction_id=transaction_id,
        project_count=len(projects),
        entity_count=integrity.entity_count,
        row_snapshot_bytes=integrity.row_snapshot_bytes,
        framed_payload_bytes=integrity.framed_payload_bytes,
        manifest_bytes=integrity.manifest_bytes,
        manifest_sha256=integrity.manifest_sha256,
        payload_sha256=integrity.payload_sha256,
        batch_sha256=integrity.batch_sha256,
        projects=projects,
    )


def _project_insert_values(row: _ProjectRow) -> Tuple[Any, ...]:
    return (
        row.project_id,
        row.generation,
        row.codec_id,
        row.codec_version,
        row.snapshot_format_version,
        row.snapshot_bytes,
        row.snapshot_sha256,
    )


def _document_values(row: _DocumentRow) -> Tuple[Any, ...]:
    return (
        row.project_id,
        row.document_id,
        row.document_version,
        row.schema_version,
        row.collaboration_mode,
        row.save_policy,
        row.codec_id,
        row.codec_version,
        row.snapshot_format_version,
        row.snapshot_bytes,
        row.snapshot_sha256,
    )


def _entity_values(row: Any) -> Tuple[Any, ...]:
    # The container was frozen with tuple's own iterator and every field read
    # comes from tuple storage, never a subclass property/getattr hook.
    return _named_row(row, _ENTITY_ROW_FIELDS)


def _batch_values(plan: _ImportPlan) -> Tuple[Any, ...]:
    return (
        plan.import_id,
        plan.transaction_id,
        SQLITE_IMPORT_TRANSACTION_KIND,
        SQLITE_IMPORT_RECEIPT_PROTOCOL,
        SQLITE_IMPORT_TERMINAL_STATUS,
        LEGACY_MANIFEST_SCHEMA,
        LEGACY_BATCH_PROTOCOL,
        LEGACY_PAYLOAD_PROTOCOL,
        "explicit_projects",
        plan.project_count,
        plan.entity_count,
        plan.row_snapshot_bytes,
        plan.framed_payload_bytes,
        plan.manifest_bytes,
        plan.manifest_sha256,
        plan.payload_sha256,
        plan.batch_sha256,
    )


def _descriptor_values(plan: _ImportPlan, project: _ProjectPlan) -> Tuple[Any, ...]:
    return (
        plan.import_id,
        project.ordinal,
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


def _execute_closed(connection: Any, sql: str, values: Sequence[Any]) -> None:
    if sql != "BEGIN IMMEDIATE" and sql not in _FIXED_INSERT_SQL:
        _fail("sqlite_legacy_import_statement_forbidden", "statement_not_fixed")
    cursor = connection.execute(sql, values)
    _close_cursor(cursor)


def _execute_many_closed(
    connection: Any,
    sql: str,
    values: Iterable[Sequence[Any]],
) -> None:
    if sql not in _FIXED_INSERT_SQL:
        _fail("sqlite_legacy_import_statement_forbidden", "statement_not_fixed")
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
        raise _ShadowCorrupt("row_cardinality_invalid")
    return first


class _ShadowConflict(Exception):
    pass


class _ShadowCorrupt(Exception):
    pass


def _stream_exact(
    connection: Any,
    sql: str,
    expected: Iterator[Tuple[Any, ...]],
    values: Sequence[Any] = (),
) -> None:
    cursor = connection.execute(sql, values)
    try:
        for wanted in expected:
            actual = cursor.fetchone()
            if actual is None or tuple(actual) != wanted:
                raise _ShadowCorrupt("row_mismatch")
        if cursor.fetchone() is not None:
            raise _ShadowCorrupt("unexpected_row")
    finally:
        _close_cursor(cursor)


def _receipt_from_connection(connection: Any, plan: _ImportPlan) -> KernelReceipt:
    row = _single_row(
        connection,
        "SELECT transaction_id, kind, committed_at "
        "FROM transaction_receipts WHERE transaction_id = ?",
        (plan.transaction_id,),
    )
    if row is None:
        raise _ShadowCorrupt("receipt_missing")
    values = tuple(row)
    if (
        values[0] != plan.transaction_id
        or values[1] != SQLITE_IMPORT_TRANSACTION_KIND
        or type(values[2]) is not str
        or _COMMITTED_AT_RE.fullmatch(values[2]) is None
    ):
        raise _ShadowCorrupt("receipt_invalid")
    return KernelReceipt(values[0], values[1], values[2])


def _target_collides(connection: Any, plan: _ImportPlan) -> bool:
    """Check only globally unique keys owned by this proposed batch."""
    for project in plan.projects:
        row = _single_row(
            connection,
            "SELECT "
            "EXISTS(SELECT 1 FROM projects WHERE project_id = ?), "
            "EXISTS(SELECT 1 FROM documents WHERE document_id = ?)",
            (
                project.project_id,
                project.document_id,
            ),
        )
        # Kernel validation has already proved descriptor↔business coverage in
        # this same read/write snapshot, so a third full-ledger descriptor scan
        # is redundant.  The project PK and global document UNIQUE indexes are
        # the collision authorities and keep this O(projects in proposed plan).
        if row is None or tuple(row) != (0, 0):
            return True
    return False


def _reconcile_connection(
    connection: Any,
    plan: _ImportPlan,
    *,
    _catalog_exact_verified: bool = False,
) -> LegacyImportReconcileResult:
    if type(_catalog_exact_verified) is not bool:
        raise TypeError("_catalog_exact_verified must be an exact bool")
    catalog_marker = _single_row(
        connection,
        "SELECT value FROM kernel_metadata WHERE key = ?",
        (SQLITE_CATALOG_STATE_KEY,),
    )
    if catalog_marker is not None and tuple(catalog_marker) != (
        SQLITE_CATALOG_STATE_HISTORICAL_FENCED,
    ):
        raise _ShadowCorrupt("catalog_marker_invalid")
    marker = _single_row(
        connection,
        "SELECT value FROM kernel_metadata WHERE key = ?",
        (SQLITE_IMPORT_STATE_KEY,),
    )
    if marker is None:
        if catalog_marker is not None:
            raise _ShadowConflict("catalog_sealed")
        return LegacyImportReconcileResult(
            protocol=SQLITE_IMPORT_RECONCILE_PROTOCOL,
            status=LegacyImportReconcileStatus.NOT_IMPORTED,
            import_id=plan.import_id,
            transaction_id=plan.transaction_id,
        )
    if tuple(marker) != (SQLITE_IMPORT_STATE_TERMINAL_SHADOW,):
        raise _ShadowCorrupt("marker_invalid")

    batch = _single_row(connection, _SELECT_BATCH_SQL, (plan.import_id,))
    if batch is None:
        if catalog_marker is not None:
            raise _ShadowConflict("catalog_sealed")
        if _target_collides(connection, plan):
            raise _ShadowConflict("target_identity_collision")
        return LegacyImportReconcileResult(
            protocol=SQLITE_IMPORT_RECONCILE_PROTOCOL,
            status=LegacyImportReconcileStatus.NOT_IMPORTED,
            import_id=plan.import_id,
            transaction_id=plan.transaction_id,
        )
    actual_batch = tuple(batch)
    if actual_batch != _batch_values(plan):
        raise _ShadowConflict("same_import_id_mismatch")

    receipt = _receipt_from_connection(connection, plan)
    _stream_exact(
        connection,
        _SELECT_DESCRIPTORS_SQL,
        (_descriptor_values(plan, project) for project in plan.projects),
        (plan.import_id,),
    )
    _stream_exact(
        connection,
        _SELECT_PROJECTS_SQL,
        (
            (1,) + _project_insert_values(project.project_row)
            for project in plan.projects
        ),
        (plan.import_id,),
    )
    _stream_exact(
        connection,
        _SELECT_DOCUMENTS_SQL,
        (_document_values(project.document_row) for project in plan.projects),
        (plan.import_id,),
    )
    _stream_exact(
        connection,
        _SELECT_ENTITIES_SQL,
        (
            _entity_values(row)
            for project in plan.projects
            for row in project.entity_rows
        ),
        (plan.import_id,),
    )
    if catalog_marker is not None and not _catalog_exact_verified:
        # Once the repository claims a historical catalog seal, a positive
        # import outcome is stronger than business-row equality.  Both the
        # importer and sealer consume this shared byte-exact definition so a
        # same-length seal/manifest/member tamper cannot leave IMPORTED green
        # while the catalog-specific reconciler reports CORRUPT.
        try:
            projection = project_normalized_import_plan(plan)
            reconcile_existing_catalog(connection, projection)
        except CatalogExactConflict as exc:
            raise _ShadowConflict("catalog_seal_mismatch") from exc
        except (CatalogExactCorrupt, LegacyCatalogIntegrityError) as exc:
            raise _ShadowCorrupt("catalog_seal_corrupt") from exc
    return LegacyImportReconcileResult(
        protocol=SQLITE_IMPORT_RECONCILE_PROTOCOL,
        status=LegacyImportReconcileStatus.IMPORTED,
        import_id=plan.import_id,
        transaction_id=plan.transaction_id,
        receipt=receipt,
    )


def _unknown_kernel_error(code: str) -> bool:
    return code in (
        "sqlite_busy",
        "sqlite_database_missing",
        "sqlite_open_failed",
        "sqlite_path_unsafe",
    ) or code.startswith(
        (
            "sqlite_connection_",
            "sqlite_directory_",
            "sqlite_file_",
            "sqlite_filesystem_",
            "sqlite_process_lock_",
            "sqlite_storage_",
        )
    )


def _reconcile_projected(
    database: SqliteDatabase,
    plan: _ImportPlan,
) -> LegacyImportReconcileResult:
    connection = None
    result: Optional[LegacyImportReconcileResult] = None
    try:
        connection, _filesystem = database._open_unvalidated()
        connection.execute("BEGIN")
        database._validate_initialized(connection)
        result = _reconcile_connection(connection, plan)
    except _ShadowConflict as exc:
        return LegacyImportReconcileResult(
            protocol=SQLITE_IMPORT_RECONCILE_PROTOCOL,
            status=LegacyImportReconcileStatus.CONFLICT,
            import_id=plan.import_id,
            transaction_id=plan.transaction_id,
            error_code="sqlite_legacy_import_conflict",
            message=str(exc),
        )
    except _ShadowCorrupt as exc:
        return LegacyImportReconcileResult(
            protocol=SQLITE_IMPORT_RECONCILE_PROTOCOL,
            status=LegacyImportReconcileStatus.CORRUPT,
            import_id=plan.import_id,
            transaction_id=plan.transaction_id,
            error_code="sqlite_legacy_import_corrupt",
            message=str(exc),
        )
    except SQLiteKernelError as exc:
        status = (
            LegacyImportReconcileStatus.UNKNOWN
            if _unknown_kernel_error(exc.code)
            else LegacyImportReconcileStatus.CORRUPT
        )
        return LegacyImportReconcileResult(
            protocol=SQLITE_IMPORT_RECONCILE_PROTOCOL,
            status=status,
            import_id=plan.import_id,
            transaction_id=plan.transaction_id,
            error_code=exc.code,
            message=str(exc),
        )
    except sqlite3.DatabaseError as exc:
        return LegacyImportReconcileResult(
            protocol=SQLITE_IMPORT_RECONCILE_PROTOCOL,
            status=LegacyImportReconcileStatus.CORRUPT,
            import_id=plan.import_id,
            transaction_id=plan.transaction_id,
            error_code="sqlite_legacy_import_read_failed",
            message=str(exc),
        )
    except Exception as exc:
        return LegacyImportReconcileResult(
            protocol=SQLITE_IMPORT_RECONCILE_PROTOCOL,
            status=LegacyImportReconcileStatus.UNKNOWN,
            import_id=plan.import_id,
            transaction_id=plan.transaction_id,
            error_code="sqlite_legacy_import_reconcile_failed",
            message=str(exc),
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
    if result.status is not LegacyImportReconcileStatus.IMPORTED:
        return result
    try:
        database._finalize_committed_storage()
    except SQLiteKernelError as exc:
        return LegacyImportReconcileResult(
            protocol=SQLITE_IMPORT_RECONCILE_PROTOCOL,
            status=LegacyImportReconcileStatus.UNKNOWN,
            import_id=plan.import_id,
            transaction_id=plan.transaction_id,
            error_code=exc.code,
            message=str(exc),
        )
    except Exception as exc:
        return LegacyImportReconcileResult(
            protocol=SQLITE_IMPORT_RECONCILE_PROTOCOL,
            status=LegacyImportReconcileStatus.UNKNOWN,
            import_id=plan.import_id,
            transaction_id=plan.transaction_id,
            error_code="sqlite_legacy_import_durability_failed",
            message=str(exc),
        )
    return result


def _raise_for_reconcile(result: LegacyImportReconcileResult) -> None:
    codes = {
        LegacyImportReconcileStatus.CONFLICT: "sqlite_legacy_import_conflict",
        LegacyImportReconcileStatus.CORRUPT: "sqlite_legacy_import_corrupt",
        LegacyImportReconcileStatus.UNKNOWN: "sqlite_legacy_import_unknown",
    }
    code = codes.get(result.status)
    if code is not None:
        _fail(code, result.error_code or result.status.value)


class SqliteLegacyImporter:
    """One narrow terminal-shadow writer bound to an initialized kernel."""

    __slots__ = ("_database",)

    def __init__(self, database: SqliteDatabase) -> None:
        if type(database) is not SqliteDatabase:
            raise TypeError("database must be an exact SqliteDatabase")
        self._database = database

    def reconcile(self, plan: Any) -> LegacyImportReconcileResult:
        return _reconcile_projected(self._database, _project_plan_graph(plan))

    def import_plan(self, plan: Any) -> LegacyImportCommitResult:
        projected = _project_plan_graph(plan)
        before = _reconcile_projected(self._database, projected)
        if before.status is LegacyImportReconcileStatus.IMPORTED:
            assert before.receipt is not None
            return LegacyImportCommitResult(
                import_id=projected.import_id,
                transaction_id=projected.transaction_id,
                receipt=before.receipt,
                already_imported=True,
            )
        _raise_for_reconcile(before)
        if before.status is not LegacyImportReconcileStatus.NOT_IMPORTED:
            _fail("sqlite_legacy_import_unknown", "unexpected_reconcile_state")

        connection = None
        commit_started = False
        try:
            connection, _filesystem = self._database._open_unvalidated()
            self._database._validate_initialized(connection)
            _execute_closed(connection, "BEGIN IMMEDIATE", ())
            # Validation is repeated under the writer lock.  The earlier fresh
            # reconcile is only an optimization; it is never a TOCTOU proof.
            self._database._validate_initialized(connection)
            current = _reconcile_connection(connection, projected)
            if current.status is LegacyImportReconcileStatus.IMPORTED:
                connection.rollback()
                connection.close()
                connection = None
                current = _reconcile_projected(self._database, projected)
                if current.status is not LegacyImportReconcileStatus.IMPORTED:
                    _raise_for_reconcile(current)
                    _fail("sqlite_legacy_import_unknown", "fresh_reconcile_failed")
                assert current.receipt is not None
                return LegacyImportCommitResult(
                    import_id=projected.import_id,
                    transaction_id=projected.transaction_id,
                    receipt=current.receipt,
                    already_imported=True,
                )
            if current.status is not LegacyImportReconcileStatus.NOT_IMPORTED:
                raise _ShadowCorrupt("writer_state_invalid")
            marker = _single_row(
                connection,
                "SELECT value FROM kernel_metadata WHERE key = ?",
                (SQLITE_IMPORT_STATE_KEY,),
            )

            authorizer = _ImportAuthorizer()
            connection.set_authorizer(authorizer)
            try:
                try:
                    if marker is None:
                        _execute_closed(
                            connection,
                            _INSERT_MARKER_SQL,
                            (
                                SQLITE_IMPORT_STATE_KEY,
                                SQLITE_IMPORT_STATE_TERMINAL_SHADOW,
                            ),
                        )
                    for project in projected.projects:
                        _execute_closed(
                            connection,
                            _INSERT_PROJECT_SQL,
                            _project_insert_values(project.project_row),
                        )
                        _execute_closed(
                            connection,
                            _INSERT_DOCUMENT_SQL,
                            _document_values(project.document_row),
                        )
                        _execute_many_closed(
                            connection,
                            _INSERT_ENTITY_SQL,
                            (_entity_values(row) for row in project.entity_rows),
                        )
                    _execute_closed(
                        connection,
                        _INSERT_RECEIPT_SQL,
                        (projected.transaction_id, SQLITE_IMPORT_TRANSACTION_KIND),
                    )
                    _execute_closed(connection, _INSERT_BATCH_SQL, _batch_values(projected))
                    _execute_many_closed(
                        connection,
                        _INSERT_DESCRIPTOR_SQL,
                        (
                            _descriptor_values(projected, project)
                            for project in projected.projects
                        ),
                    )
                except sqlite3.DatabaseError as exc:
                    if authorizer.denied:
                        raise SQLiteLegacyImportError(
                            "sqlite_legacy_import_statement_forbidden",
                            "authorizer_denied",
                        ) from exc
                    raise
            finally:
                # CPython 3.9 does not reliably clear an installed authorizer
                # with None.  Replace it with an explicit allow-only callback
                # before kernel validation needs its owned PRAGMAs.
                connection.set_authorizer(_allow_authorizer)
            if authorizer.denied:
                _fail("sqlite_legacy_import_statement_forbidden", "authorizer_denied")
            if not connection.in_transaction:
                _fail("sqlite_legacy_import_transaction_escaped", "transaction_missing")
            self._database._validate_initialized(connection)
            commit_started = True
            connection.commit()
            connection.close()
            connection = None
        except BaseException as exc:
            if connection is not None:
                try:
                    connection.rollback()
                except Exception:
                    pass
                try:
                    connection.close()
                except Exception:
                    pass
            reconciled = _reconcile_projected(self._database, projected)
            if reconciled.status is LegacyImportReconcileStatus.IMPORTED:
                assert reconciled.receipt is not None
                if not isinstance(exc, Exception):
                    # Durable outcome is known, but process-control exceptions
                    # are not acknowledgements to suppress.  The SDK bridge
                    # will fresh-reconcile, terminalize its lane, then preserve
                    # this cancellation for the caller.
                    raise
                return LegacyImportCommitResult(
                    import_id=projected.import_id,
                    transaction_id=projected.transaction_id,
                    receipt=reconciled.receipt,
                    recovered_after_ack_loss=commit_started,
                )
            if not isinstance(exc, Exception):
                # Preserve async process-control semantics after the target is
                # freshly proven NOT_IMPORTED/other non-success.  Converting a
                # KeyboardInterrupt/SystemExit into a retryable adapter error
                # would hide the caller's cancellation request.
                raise
            if isinstance(exc, SQLiteLegacyImportError):
                raise
            if isinstance(exc, _ShadowConflict):
                _fail("sqlite_legacy_import_conflict", "writer_conflict")
            if isinstance(exc, _ShadowCorrupt):
                _fail("sqlite_legacy_import_corrupt", "writer_corrupt")
            if reconciled.status is LegacyImportReconcileStatus.NOT_IMPORTED:
                _fail("sqlite_legacy_import_not_committed", "transaction_failed")
            _raise_for_reconcile(reconciled)
            _fail("sqlite_legacy_import_unknown", "transaction_indeterminate")

        reconciled = _reconcile_projected(self._database, projected)
        if reconciled.status is not LegacyImportReconcileStatus.IMPORTED:
            _raise_for_reconcile(reconciled)
            _fail("sqlite_legacy_import_not_committed", "fresh_reconcile_failed")
        assert reconciled.receipt is not None
        return LegacyImportCommitResult(
            import_id=projected.import_id,
            transaction_id=projected.transaction_id,
            receipt=reconciled.receipt,
        )


__all__ = [
    "LegacyImportCommitResult",
    "LegacyImportReconcileResult",
    "LegacyImportReconcileStatus",
    "SQLiteLegacyImportError",
    "SqliteLegacyImporter",
]
