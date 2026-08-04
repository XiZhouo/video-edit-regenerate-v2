"""Sealed legacy-scan carriers and the single import issuance ledger.

This is the bottom layer of the legacy import seam.  It owns only immutable
snapshot records, canonical manifest/digest construction, exact carrier
identity and one process-local identity ledger.  Filesystem discovery,
Document loading, runtime restoration and persistence authority all belong to
the coordinator above this module.
"""

from __future__ import annotations

import os
import threading
import weakref
from dataclasses import dataclass, field
from typing import Any, NamedTuple

from . import _paths  # noqa: F401  (install flat persistence imports first)

from business_codec import BusinessCodecError  # noqa: E402
from business_snapshot import (  # noqa: E402
    BusinessSnapshotError,
    BusinessSnapshotSet,
    DocumentSnapshotRow,
    EntitySnapshotRow,
    MAX_ENTITY_COUNT,
    MAX_TOTAL_SNAPSHOT_BYTES,
    ProjectSnapshotRow,
    verify_business_snapshot,
)
from legacy_import_contract import (  # noqa: E402
    LEGACY_BATCH_PROTOCOL,
    LEGACY_IMPORT_PLAN_PROTOCOL,
    LEGACY_MANIFEST_SCHEMA,
    LEGACY_PAYLOAD_PROTOCOL,
    MAX_LEGACY_ENTITIES,
    MAX_LEGACY_MANIFEST_BYTES,
    MAX_LEGACY_MANIFEST_NODES,
    MAX_LEGACY_PROJECTS,
    MAX_LEGACY_ROW_SNAPSHOT_BYTES,
    PREPARED_PAYLOAD_STATUS,
    SQLITE_IMPORT_RECEIPT_PROTOCOL,
    SQLITE_IMPORT_TERMINAL_STATUS,
    SQLITE_IMPORT_TRANSACTION_KIND,
    SQLITE_IMPORT_TRANSACTION_PREFIX,
)
from legacy_import_integrity import (  # noqa: E402
    LEGACY_BATCH_HASH_DOMAIN as _BATCH_HASH_DOMAIN,
    LEGACY_MANIFEST_HASH_DOMAIN as _MANIFEST_HASH_DOMAIN,
    LEGACY_PAYLOAD_HASH_DOMAIN as _PAYLOAD_DOMAIN,
    LegacyImportIntegrityError,
    LegacyImportIntegrityProject,
    build_legacy_import_integrity,
)


if (
    MAX_LEGACY_ENTITIES > MAX_ENTITY_COUNT
    or MAX_LEGACY_ROW_SNAPSHOT_BYTES > MAX_TOTAL_SNAPSHOT_BYTES
):
    raise RuntimeError("legacy import budget exceeds the business snapshot format")

_LOWER_HEX = frozenset("0123456789abcdef")
_VALID_STORAGE_VERSIONS = frozenset(("legacy", "v2"))


class LegacyImportError(ValueError):
    """Stable path- and payload-free failure at the legacy import boundary."""

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
    raise LegacyImportError(code, reason)


class _IssuedCarrier:
    __slots__ = ()

    def __new__(cls):
        raise TypeError("legacy import carriers are framework-issued")

    def __reduce_ex__(self, _protocol: int):
        raise TypeError("legacy import carriers cannot be serialized")


class LegacyScanHandle(_IssuedCarrier):
    """Opaque two-lane handle for one verified live-project scan."""

    __slots__ = ("__weakref__",)

    @property
    def project_id(self) -> str:
        return _LEDGER.scan(self).snapshot.project.project_id

    @property
    def generation(self) -> int:
        return _LEDGER.scan(self).snapshot.project.generation

    @property
    def document_id(self) -> str:
        return _LEDGER.scan(self).snapshot.document.document_id

    @property
    def document_version(self) -> int:
        return _LEDGER.scan(self).snapshot.document.document_version

    @property
    def storage_version(self) -> str:
        return _LEDGER.scan(self).storage_version

    @property
    def source_head_sha256(self) -> bytes:
        return _LEDGER.scan(self).source_head_sha256

    @property
    def source_head_bytes(self) -> int:
        return _LEDGER.scan(self).source_head_bytes

    @property
    def business_root(self) -> bytes:
        return _LEDGER.scan(self).snapshot.project.snapshot_sha256

    @property
    def entity_count(self) -> int:
        return len(_LEDGER.scan(self).snapshot.entities)

    @property
    def row_snapshot_bytes(self) -> int:
        return _LEDGER.scan(self).row_snapshot_bytes


class FrozenLegacyImport(_IssuedCarrier):
    """Opaque verified virtual payload; it is not a durability receipt."""

    __slots__ = ("__weakref__",)

    @property
    def import_id(self) -> str:
        return _LEDGER.prepared_package(self).import_id

    @property
    def project_count(self) -> int:
        return len(_LEDGER.prepared_package(self).projects)

    @property
    def entity_count(self) -> int:
        return _LEDGER.prepared_package(self).entity_count

    @property
    def row_snapshot_bytes(self) -> int:
        return _LEDGER.prepared_package(self).row_snapshot_bytes

    @property
    def framed_payload_bytes(self) -> int:
        return _LEDGER.prepared_package(self).framed_payload_bytes

    @property
    def manifest_bytes(self) -> bytes:
        return _LEDGER.prepared_package(self).manifest_bytes

    @property
    def manifest_sha256(self) -> bytes:
        return _LEDGER.prepared_package(self).manifest_sha256

    @property
    def payload_sha256(self) -> bytes:
        return _LEDGER.prepared_package(self).payload_sha256

    @property
    def batch_sha256(self) -> bytes:
        return _LEDGER.prepared_package(self).batch_sha256


class PreparedLegacyPayloadReceipt(_IssuedCarrier):
    """Identity-bound proof of preparation, never of commit or authority."""

    __slots__ = ("__weakref__",)

    @property
    def status(self) -> str:
        _LEDGER.prepared_receipt(self)
        return PREPARED_PAYLOAD_STATUS

    @property
    def import_id(self) -> str:
        return _LEDGER.prepared_receipt(self).import_id

    @property
    def project_count(self) -> int:
        return len(_LEDGER.prepared_receipt(self).projects)

    @property
    def entity_count(self) -> int:
        return _LEDGER.prepared_receipt(self).entity_count

    @property
    def row_snapshot_bytes(self) -> int:
        return _LEDGER.prepared_receipt(self).row_snapshot_bytes

    @property
    def manifest_sha256(self) -> bytes:
        return _LEDGER.prepared_receipt(self).manifest_sha256

    @property
    def payload_sha256(self) -> bytes:
        return _LEDGER.prepared_receipt(self).payload_sha256

    @property
    def batch_sha256(self) -> bytes:
        return _LEDGER.prepared_receipt(self).batch_sha256


@dataclass
class _ScanRecord:
    snapshot: BusinessSnapshotSet = field(repr=False)
    source_head_sha256: bytes = field(repr=False)
    source_head_bytes: int
    storage_version: str
    row_snapshot_bytes: int
    payload_state: str = "available"
    payload_token: object | None = field(default=None, repr=False)
    runtime_state: str = "available"
    runtime_token: object | None = field(default=None, repr=False)
    issuer_pid: int = field(default=0, repr=False)
    issuer_epoch: int = field(default=0, repr=False)


@dataclass(frozen=True)
class _FrozenProject:
    snapshot: BusinessSnapshotSet = field(repr=False, compare=False)
    source_head_sha256: bytes = field(repr=False)
    source_head_bytes: int
    storage_version: str
    row_snapshot_bytes: int


@dataclass
class _PreparedRecord:
    import_id: str
    projects: tuple[_FrozenProject, ...] = field(repr=False)
    entity_count: int
    row_snapshot_bytes: int
    framed_payload_bytes: int
    manifest_bytes: bytes = field(repr=False)
    manifest_sha256: bytes = field(repr=False)
    payload_sha256: bytes = field(repr=False)
    batch_sha256: bytes = field(repr=False)
    adapter_state: str = "available"
    adapter_token: object | None = field(default=None, repr=False)
    issuer_pid: int = field(default=0, repr=False)
    issuer_epoch: int = field(default=0, repr=False)


class _ProjectRowIntent(NamedTuple):
    project_id: str
    generation: int
    codec_id: str
    codec_version: int
    snapshot_format_version: int
    snapshot_bytes: bytes
    snapshot_sha256: bytes

    def __repr__(self) -> str:
        return (
            "_ProjectRowIntent("
            f"generation={self.generation}, codec_version={self.codec_version}, "
            f"snapshot_format_version={self.snapshot_format_version})"
        )

    def __reduce_ex__(self, _protocol: int):
        raise TypeError("verified legacy import rows cannot be serialized")


class _DocumentRowIntent(NamedTuple):
    project_id: str
    document_id: str
    document_version: int
    schema_version: str
    collaboration_mode: str
    save_policy: str | None
    codec_id: str
    codec_version: int
    snapshot_format_version: int
    snapshot_bytes: bytes
    snapshot_sha256: bytes

    def __repr__(self) -> str:
        return (
            "_DocumentRowIntent("
            f"document_version={self.document_version}, "
            f"codec_version={self.codec_version}, "
            f"snapshot_format_version={self.snapshot_format_version})"
        )

    def __reduce_ex__(self, _protocol: int):
        raise TypeError("verified legacy import rows cannot be serialized")


class _EntityRowIntent(NamedTuple):
    project_id: str
    document_id: str
    entity_type: str
    entity_id: str
    entity_version: int
    deleted: int
    codec_id: str
    codec_version: int
    snapshot_format_version: int
    snapshot_bytes: bytes
    snapshot_sha256: bytes

    def __repr__(self) -> str:
        return (
            "_EntityRowIntent("
            f"entity_version={self.entity_version}, deleted={self.deleted}, "
            f"codec_version={self.codec_version}, "
            f"snapshot_format_version={self.snapshot_format_version})"
        )

    def __reduce_ex__(self, _protocol: int):
        raise TypeError("verified legacy import rows cannot be serialized")


class _EntityRowIntents(tuple):
    """Immutable entity-row collection with a payload-free bounded repr."""

    __slots__ = ()

    def __new__(cls, rows: Any):
        return tuple.__new__(cls, rows)

    def __repr__(self) -> str:
        return f"_EntityRowIntents(count={len(self)})"

    def __getitem__(self, index):
        value = tuple.__getitem__(self, index)
        if isinstance(index, slice):
            return type(self)(value)
        return value

    def __reduce_ex__(self, _protocol: int):
        raise TypeError("verified legacy import rows cannot be serialized")


class VerifiedLegacyProjectPlan(_IssuedCarrier):
    """Opaque view of one framework-issued, isolated project row intent."""

    __slots__ = ("__weakref__",)

    @property
    def ordinal(self) -> int:
        return _LEDGER.verified_project_plan(self).ordinal

    @property
    def project_id(self) -> str:
        return _LEDGER.verified_project_plan(self).project_id

    @property
    def generation(self) -> int:
        return _LEDGER.verified_project_plan(self).generation

    @property
    def document_id(self) -> str:
        return _LEDGER.verified_project_plan(self).document_id

    @property
    def document_version(self) -> int:
        return _LEDGER.verified_project_plan(self).document_version

    @property
    def storage_version(self) -> str:
        return _LEDGER.verified_project_plan(self).storage_version

    @property
    def source_head_sha256(self) -> bytes:
        return _LEDGER.verified_project_plan(self).source_head_sha256

    @property
    def source_head_bytes(self) -> int:
        return _LEDGER.verified_project_plan(self).source_head_bytes

    @property
    def business_root_sha256(self) -> bytes:
        return _LEDGER.verified_project_plan(self).business_root_sha256

    @property
    def entity_count(self) -> int:
        return _LEDGER.verified_project_plan(self).entity_count

    @property
    def row_snapshot_bytes(self) -> int:
        return _LEDGER.verified_project_plan(self).row_snapshot_bytes

    @property
    def project_row(self) -> _ProjectRowIntent:
        return _LEDGER.verified_project_plan(self).project_row

    @property
    def document_row(self) -> _DocumentRowIntent:
        return _LEDGER.verified_project_plan(self).document_row

    @property
    def entity_rows(self) -> _EntityRowIntents:
        return _LEDGER.verified_project_plan(self).entity_rows

    @property
    def snapshot(self) -> BusinessSnapshotSet:
        # Compatibility projection only.  The private SQLite bridge consumes the
        # immutable *_row views above; this detached dataclass graph may be
        # retained or corrupted without changing those plan-owned rows.
        return _snapshot_from_plan_rows(_LEDGER.verified_project_plan(self))


class VerifiedLegacyImportPlan(_IssuedCarrier):
    """Opaque complete row intent bound to one exact active reservation."""

    __slots__ = ("__weakref__",)

    @property
    def plan_protocol(self) -> str:
        return _LEDGER.verified_import_plan(self).data.plan_protocol

    @property
    def import_id(self) -> str:
        return _LEDGER.verified_import_plan(self).data.import_id

    @property
    def transaction_id(self) -> str:
        return _LEDGER.verified_import_plan(self).data.transaction_id

    @property
    def receipt_kind(self) -> str:
        return _LEDGER.verified_import_plan(self).data.receipt_kind

    @property
    def receipt_protocol(self) -> str:
        return _LEDGER.verified_import_plan(self).data.receipt_protocol

    @property
    def status(self) -> str:
        return _LEDGER.verified_import_plan(self).data.status

    @property
    def manifest_schema(self) -> str:
        return _LEDGER.verified_import_plan(self).data.manifest_schema

    @property
    def batch_protocol(self) -> str:
        return _LEDGER.verified_import_plan(self).data.batch_protocol

    @property
    def payload_protocol(self) -> str:
        return _LEDGER.verified_import_plan(self).data.payload_protocol

    @property
    def selection(self) -> str:
        return _LEDGER.verified_import_plan(self).data.selection

    @property
    def project_count(self) -> int:
        return _LEDGER.verified_import_plan(self).data.project_count

    @property
    def entity_count(self) -> int:
        return _LEDGER.verified_import_plan(self).data.entity_count

    @property
    def row_snapshot_bytes(self) -> int:
        return _LEDGER.verified_import_plan(self).data.row_snapshot_bytes

    @property
    def framed_payload_bytes(self) -> int:
        return _LEDGER.verified_import_plan(self).data.framed_payload_bytes

    @property
    def manifest_bytes(self) -> bytes:
        return _LEDGER.verified_import_plan(self).data.manifest_bytes

    @property
    def manifest_sha256(self) -> bytes:
        return _LEDGER.verified_import_plan(self).data.manifest_sha256

    @property
    def payload_sha256(self) -> bytes:
        return _LEDGER.verified_import_plan(self).data.payload_sha256

    @property
    def batch_sha256(self) -> bytes:
        return _LEDGER.verified_import_plan(self).data.batch_sha256

    @property
    def projects(self) -> tuple[VerifiedLegacyProjectPlan, ...]:
        return _LEDGER.verified_import_plan(self).projects

    @property
    def snapshots(self) -> tuple[BusinessSnapshotSet, ...]:
        return tuple(project.snapshot for project in self.projects)


@dataclass(frozen=True)
class _VerifiedLegacyProjectPlanData:
    """Ledger-owned project intent; never returned by a public property."""

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
    project_row: _ProjectRowIntent = field(repr=False)
    document_row: _DocumentRowIntent = field(repr=False)
    entity_rows: _EntityRowIntents = field(repr=False)


@dataclass(frozen=True)
class _VerifiedLegacyImportPlanData:
    """Ledger-owned top-level intent before opaque carriers are issued."""

    plan_protocol: str
    import_id: str
    transaction_id: str
    receipt_kind: str
    receipt_protocol: str
    status: str
    manifest_schema: str
    batch_protocol: str
    payload_protocol: str
    selection: str
    project_count: int
    entity_count: int
    row_snapshot_bytes: int
    framed_payload_bytes: int
    manifest_bytes: bytes = field(repr=False)
    manifest_sha256: bytes = field(repr=False)
    payload_sha256: bytes = field(repr=False)
    batch_sha256: bytes = field(repr=False)
    projects: tuple[_VerifiedLegacyProjectPlanData, ...]


@dataclass(frozen=True)
class _IssuedVerifiedLegacyImportPlan:
    data: _VerifiedLegacyImportPlanData = field(repr=False)
    projects: tuple[VerifiedLegacyProjectPlan, ...] = field(repr=False)
    issuer_pid: int = field(repr=False)
    issuer_epoch: int = field(repr=False)


@dataclass(frozen=True)
class _PreparedProjectionSource:
    """One lock-atomic copy of every sealed prepared payload field."""

    import_id: str
    projects: tuple[_FrozenProject, ...] = field(repr=False)
    entity_count: int
    row_snapshot_bytes: int
    framed_payload_bytes: int
    manifest_bytes: bytes = field(repr=False)
    manifest_sha256: bytes = field(repr=False)
    payload_sha256: bytes = field(repr=False)
    batch_sha256: bytes = field(repr=False)


@dataclass(frozen=True)
class _PayloadReservation:
    handles: tuple[LegacyScanHandle, ...] = field(repr=False)
    records: tuple[_ScanRecord, ...] = field(repr=False)
    token: object = field(repr=False)
    issuer_pid: int
    issuer_epoch: int


@dataclass(frozen=True)
class _RuntimeReservation:
    handle: LegacyScanHandle = field(repr=False)
    record: _ScanRecord = field(repr=False)
    token: object = field(repr=False)
    issuer_pid: int
    issuer_epoch: int


@dataclass(frozen=True, eq=False)
class _PreparedReservation:
    package: FrozenLegacyImport = field(repr=False)
    receipt: PreparedLegacyPayloadReceipt = field(repr=False)
    record: _PreparedRecord = field(repr=False)
    token: object = field(repr=False)
    attempt_token: object = field(repr=False)
    issuer_pid: int
    issuer_epoch: int


def _snapshot_from_plan_rows(
    data: _VerifiedLegacyProjectPlanData,
) -> BusinessSnapshotSet:
    project = data.project_row
    document = data.document_row
    return BusinessSnapshotSet(
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
        entities=tuple(
            EntitySnapshotRow(
                project_id=row.project_id,
                document_id=row.document_id,
                entity_type=row.entity_type,
                entity_id=row.entity_id,
                entity_version=row.entity_version,
                deleted=row.deleted,
                codec_id=row.codec_id,
                codec_version=row.codec_version,
                snapshot_format_version=row.snapshot_format_version,
                snapshot_bytes=row.snapshot_bytes,
                snapshot_sha256=row.snapshot_sha256,
            )
            for row in data.entity_rows
        ),
    )


def _clone_business_snapshot(snapshot: BusinessSnapshotSet) -> BusinessSnapshotSet:
    """Clone the complete row graph without sharing any mutable row object."""

    if (
        type(snapshot) is not BusinessSnapshotSet
        or type(snapshot.project) is not ProjectSnapshotRow
        or type(snapshot.document) is not DocumentSnapshotRow
        or type(snapshot.entities) is not tuple
        or any(type(row) is not EntitySnapshotRow for row in snapshot.entities)
    ):
        _fail("legacy_import_payload_invalid", "prepared_record_tampered")
    project = snapshot.project
    document = snapshot.document
    return BusinessSnapshotSet(
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
        entities=tuple(
            EntitySnapshotRow(
                project_id=row.project_id,
                document_id=row.document_id,
                entity_type=row.entity_type,
                entity_id=row.entity_id,
                entity_version=row.entity_version,
                deleted=row.deleted,
                codec_id=row.codec_id,
                codec_version=row.codec_version,
                snapshot_format_version=row.snapshot_format_version,
                snapshot_bytes=row.snapshot_bytes,
                snapshot_sha256=row.snapshot_sha256,
            )
            for row in snapshot.entities
        ),
    )


def _clone_frozen_project(project: _FrozenProject) -> _FrozenProject:
    if type(project) is not _FrozenProject:
        _fail("legacy_import_payload_invalid", "prepared_record_tampered")
    return _FrozenProject(
        snapshot=_clone_business_snapshot(project.snapshot),
        source_head_sha256=project.source_head_sha256,
        source_head_bytes=project.source_head_bytes,
        storage_version=project.storage_version,
        row_snapshot_bytes=project.row_snapshot_bytes,
    )


def _row_snapshot_bytes(snapshot: BusinessSnapshotSet) -> int:
    return (
        len(snapshot.project.snapshot_bytes)
        + len(snapshot.document.snapshot_bytes)
        + sum(len(row.snapshot_bytes) for row in snapshot.entities)
    )


def _validate_import_id(import_id: Any) -> str:
    if (
        type(import_id) is not str
        or len(import_id) != 64
        or any(character not in _LOWER_HEX for character in import_id)
    ):
        _fail("legacy_import_prepare_invalid", "import_id_invalid")
    return import_id


def _frozen_project(record: _ScanRecord) -> _FrozenProject:
    return _FrozenProject(
        snapshot=record.snapshot,
        source_head_sha256=record.source_head_sha256,
        source_head_bytes=record.source_head_bytes,
        storage_version=record.storage_version,
        row_snapshot_bytes=record.row_snapshot_bytes,
    )


def _verify_frozen_project(project: _FrozenProject) -> BusinessSnapshotSet:
    if type(project) is not _FrozenProject:
        _fail("legacy_import_payload_invalid", "project_record_invalid")
    try:
        snapshot = verify_business_snapshot(project.snapshot)
    except BusinessSnapshotError as exc:
        raise LegacyImportError(
            "legacy_import_payload_invalid", exc.code,
        ) from None
    expected_bytes = _row_snapshot_bytes(snapshot)
    if (
        type(project.source_head_sha256) is not bytes
        or len(project.source_head_sha256) != 32
        or type(project.source_head_bytes) is not int
        or project.source_head_bytes < 1
        or type(project.storage_version) is not str
        or project.storage_version not in _VALID_STORAGE_VERSIONS
        or type(project.row_snapshot_bytes) is not int
        or project.row_snapshot_bytes != expected_bytes
    ):
        _fail("legacy_import_payload_invalid", "project_record_invalid")
    return snapshot


def _integrity_project(project: _FrozenProject) -> LegacyImportIntegrityProject:
    """Project one verified carrier record into the core byte-only contract."""
    snapshot = project.snapshot
    return LegacyImportIntegrityProject(
        project_id=snapshot.project.project_id,
        generation=snapshot.project.generation,
        document_id=snapshot.document.document_id,
        document_version=snapshot.document.document_version,
        storage_version=project.storage_version,
        source_head_sha256=project.source_head_sha256,
        source_head_bytes=project.source_head_bytes,
        business_root_sha256=snapshot.project.snapshot_sha256,
        entity_count=len(snapshot.entities),
        row_snapshot_bytes=project.row_snapshot_bytes,
        project_snapshot_bytes=snapshot.project.snapshot_bytes,
        document_snapshot_bytes=snapshot.document.snapshot_bytes,
        entity_snapshot_bytes=(
            row.snapshot_bytes for row in snapshot.entities
        ),
    )


def _build_prepared_record(
    import_id: str,
    projects: tuple[_FrozenProject, ...],
) -> _PreparedRecord:
    _validate_import_id(import_id)
    if type(projects) is not tuple or not projects:
        _fail("legacy_import_prepare_invalid", "projects_invalid")
    if len(projects) > MAX_LEGACY_PROJECTS:
        _fail("legacy_import_budget_exceeded", "project_count_exceeded")

    previous_project_id: str | None = None
    seen_document_ids: set[str] = set()
    for project in projects:
        snapshot = _verify_frozen_project(project)
        project_id = snapshot.project.project_id
        if previous_project_id is not None and project_id <= previous_project_id:
            _fail("legacy_import_prepare_invalid", "project_order_invalid")
        previous_project_id = project_id
        document_id = snapshot.document.document_id
        if document_id in seen_document_ids:
            _fail("legacy_import_prepare_invalid", "duplicate_document")
        seen_document_ids.add(document_id)

    try:
        integrity = build_legacy_import_integrity(
            import_id,
            tuple(_integrity_project(project) for project in projects),
        )
    except LegacyImportIntegrityError as exc:
        if exc.code in {
            "project_count_exceeded",
            "entity_count_exceeded",
            "row_snapshot_bytes_exceeded",
        }:
            raise LegacyImportError(
                "legacy_import_budget_exceeded", exc.code,
            ) from None
        raise LegacyImportError(
            "legacy_import_payload_invalid", exc.code,
        ) from None
    except BusinessCodecError as exc:
        raise LegacyImportError(
            "legacy_import_manifest_invalid", exc.code,
        ) from None
    return _PreparedRecord(
        import_id=import_id,
        projects=projects,
        entity_count=integrity.entity_count,
        row_snapshot_bytes=integrity.row_snapshot_bytes,
        framed_payload_bytes=integrity.framed_payload_bytes,
        manifest_bytes=integrity.manifest_bytes,
        manifest_sha256=integrity.manifest_sha256,
        payload_sha256=integrity.payload_sha256,
        batch_sha256=integrity.batch_sha256,
    )


def _capture_prepared_projection(
    record: _PreparedRecord,
) -> _PreparedProjectionSource:
    if type(record) is not _PreparedRecord:
        _fail("legacy_import_payload_invalid", "prepared_record_invalid")
    if type(record.projects) is not tuple:
        _fail("legacy_import_payload_invalid", "prepared_record_invalid")
    return _PreparedProjectionSource(
        import_id=record.import_id,
        projects=tuple(_clone_frozen_project(project) for project in record.projects),
        entity_count=record.entity_count,
        row_snapshot_bytes=record.row_snapshot_bytes,
        framed_payload_bytes=record.framed_payload_bytes,
        manifest_bytes=record.manifest_bytes,
        manifest_sha256=record.manifest_sha256,
        payload_sha256=record.payload_sha256,
        batch_sha256=record.batch_sha256,
    )


def _verify_prepared_projection(
    source: _PreparedProjectionSource,
) -> _VerifiedLegacyImportPlanData:
    if type(source) is not _PreparedProjectionSource:
        _fail("legacy_import_payload_invalid", "prepared_projection_invalid")
    if (
        type(source.import_id) is not str
        or type(source.projects) is not tuple
        or type(source.entity_count) is not int
        or type(source.row_snapshot_bytes) is not int
        or type(source.framed_payload_bytes) is not int
        or type(source.manifest_bytes) is not bytes
        or type(source.manifest_sha256) is not bytes
        or type(source.payload_sha256) is not bytes
        or type(source.batch_sha256) is not bytes
    ):
        _fail("legacy_import_payload_invalid", "prepared_record_tampered")
    expected = _build_prepared_record(source.import_id, source.projects)
    if (
        source.entity_count != expected.entity_count
        or source.row_snapshot_bytes != expected.row_snapshot_bytes
        or source.framed_payload_bytes != expected.framed_payload_bytes
        or source.manifest_bytes != expected.manifest_bytes
        or source.manifest_sha256 != expected.manifest_sha256
        or source.payload_sha256 != expected.payload_sha256
        or source.batch_sha256 != expected.batch_sha256
    ):
        _fail("legacy_import_payload_invalid", "prepared_record_tampered")

    projects = tuple(
        _VerifiedLegacyProjectPlanData(
            ordinal=ordinal,
            project_id=project.snapshot.project.project_id,
            generation=project.snapshot.project.generation,
            document_id=project.snapshot.document.document_id,
            document_version=project.snapshot.document.document_version,
            storage_version=project.storage_version,
            source_head_sha256=project.source_head_sha256,
            source_head_bytes=project.source_head_bytes,
            business_root_sha256=project.snapshot.project.snapshot_sha256,
            entity_count=len(project.snapshot.entities),
            row_snapshot_bytes=project.row_snapshot_bytes,
            project_row=_ProjectRowIntent(
                project_id=project.snapshot.project.project_id,
                generation=project.snapshot.project.generation,
                codec_id=project.snapshot.project.codec_id,
                codec_version=project.snapshot.project.codec_version,
                snapshot_format_version=(
                    project.snapshot.project.snapshot_format_version
                ),
                snapshot_bytes=project.snapshot.project.snapshot_bytes,
                snapshot_sha256=project.snapshot.project.snapshot_sha256,
            ),
            document_row=_DocumentRowIntent(
                project_id=project.snapshot.document.project_id,
                document_id=project.snapshot.document.document_id,
                document_version=project.snapshot.document.document_version,
                schema_version=project.snapshot.document.schema_version,
                collaboration_mode=project.snapshot.document.collaboration_mode,
                save_policy=project.snapshot.document.save_policy,
                codec_id=project.snapshot.document.codec_id,
                codec_version=project.snapshot.document.codec_version,
                snapshot_format_version=(
                    project.snapshot.document.snapshot_format_version
                ),
                snapshot_bytes=project.snapshot.document.snapshot_bytes,
                snapshot_sha256=project.snapshot.document.snapshot_sha256,
            ),
            entity_rows=_EntityRowIntents(
                _EntityRowIntent(
                    project_id=row.project_id,
                    document_id=row.document_id,
                    entity_type=row.entity_type,
                    entity_id=row.entity_id,
                    entity_version=row.entity_version,
                    deleted=row.deleted,
                    codec_id=row.codec_id,
                    codec_version=row.codec_version,
                    snapshot_format_version=row.snapshot_format_version,
                    snapshot_bytes=row.snapshot_bytes,
                    snapshot_sha256=row.snapshot_sha256,
                )
                for row in project.snapshot.entities
            ),
        )
        for ordinal, project in enumerate(expected.projects)
    )
    return _VerifiedLegacyImportPlanData(
        plan_protocol=LEGACY_IMPORT_PLAN_PROTOCOL,
        import_id=expected.import_id,
        transaction_id=SQLITE_IMPORT_TRANSACTION_PREFIX + expected.import_id,
        receipt_kind=SQLITE_IMPORT_TRANSACTION_KIND,
        receipt_protocol=SQLITE_IMPORT_RECEIPT_PROTOCOL,
        status=SQLITE_IMPORT_TERMINAL_STATUS,
        manifest_schema=LEGACY_MANIFEST_SCHEMA,
        batch_protocol=LEGACY_BATCH_PROTOCOL,
        payload_protocol=LEGACY_PAYLOAD_PROTOCOL,
        selection="explicit_projects",
        project_count=len(projects),
        entity_count=expected.entity_count,
        row_snapshot_bytes=expected.row_snapshot_bytes,
        framed_payload_bytes=expected.framed_payload_bytes,
        manifest_bytes=expected.manifest_bytes,
        manifest_sha256=expected.manifest_sha256,
        payload_sha256=expected.payload_sha256,
        batch_sha256=expected.batch_sha256,
        projects=projects,
    )


def _same_row_primitives(
    current: Any,
    projected: Any,
    row_type: type,
    fields: tuple[str, ...],
) -> bool:
    return (
        type(current) is row_type
        and type(projected) is row_type
        and all(
            getattr(current, name) is getattr(projected, name)
            for name in fields
        )
    )


def _same_snapshot_primitive_graph(
    current: BusinessSnapshotSet,
    projected: BusinessSnapshotSet,
) -> bool:
    """Compare the cloned graph's immutable leaves without decoding bytes."""

    if (
        type(current) is not BusinessSnapshotSet
        or type(projected) is not BusinessSnapshotSet
        or type(current.entities) is not tuple
        or type(projected.entities) is not tuple
        or len(current.entities) != len(projected.entities)
        or not _same_row_primitives(
            current.project,
            projected.project,
            ProjectSnapshotRow,
            (
                "project_id", "generation", "codec_id", "codec_version",
                "snapshot_format_version", "snapshot_bytes", "snapshot_sha256",
            ),
        )
        or not _same_row_primitives(
            current.document,
            projected.document,
            DocumentSnapshotRow,
            (
                "project_id", "document_id", "document_version",
                "schema_version", "collaboration_mode", "save_policy",
                "codec_id", "codec_version", "snapshot_format_version",
                "snapshot_bytes", "snapshot_sha256",
            ),
        )
    ):
        return False
    entity_fields = (
        "project_id", "document_id", "entity_type", "entity_id",
        "entity_version", "deleted", "codec_id", "codec_version",
        "snapshot_format_version", "snapshot_bytes", "snapshot_sha256",
    )
    return all(
        _same_row_primitives(
            current_row,
            projected_row,
            EntitySnapshotRow,
            entity_fields,
        )
        for current_row, projected_row in zip(
            current.entities,
            projected.entities,
        )
    )


def _prepared_record_matches_projection(
    record: _PreparedRecord,
    source: _PreparedProjectionSource,
) -> bool:
    """Compare current sealed state to an isolated verified projection."""

    if (
        type(record) is not _PreparedRecord
        or type(source) is not _PreparedProjectionSource
        or type(record.import_id) is not str
        or type(record.projects) is not tuple
        or type(record.entity_count) is not int
        or type(record.row_snapshot_bytes) is not int
        or type(record.framed_payload_bytes) is not int
        or type(record.manifest_bytes) is not bytes
        or type(record.manifest_sha256) is not bytes
        or type(record.payload_sha256) is not bytes
        or type(record.batch_sha256) is not bytes
        or record.import_id != source.import_id
        or record.entity_count != source.entity_count
        or record.row_snapshot_bytes != source.row_snapshot_bytes
        or record.framed_payload_bytes != source.framed_payload_bytes
        or record.manifest_bytes != source.manifest_bytes
        or record.manifest_sha256 != source.manifest_sha256
        or record.payload_sha256 != source.payload_sha256
        or record.batch_sha256 != source.batch_sha256
        or len(record.projects) != len(source.projects)
    ):
        return False
    for current, projected in zip(record.projects, source.projects):
        if type(current) is not _FrozenProject:
            return False
        if (
            not _same_snapshot_primitive_graph(
                current.snapshot,
                projected.snapshot,
            )
            or current.source_head_sha256 is not projected.source_head_sha256
            or current.source_head_bytes is not projected.source_head_bytes
            or current.storage_version is not projected.storage_version
            or current.row_snapshot_bytes is not projected.row_snapshot_bytes
        ):
            return False
    return True


def _same_plan_row_primitives(
    current: Any,
    intended: Any,
    current_type: type,
    intended_type: type,
    fields: tuple[str, ...],
) -> bool:
    return (
        type(current) is current_type
        and type(intended) is intended_type
        and all(
            getattr(current, name) is getattr(intended, name)
            for name in fields
        )
    )


def _same_snapshot_and_plan_rows(
    current: BusinessSnapshotSet,
    data: _VerifiedLegacyProjectPlanData,
) -> bool:
    if (
        type(current) is not BusinessSnapshotSet
        or type(data) is not _VerifiedLegacyProjectPlanData
        or type(current.entities) is not tuple
        or type(data.entity_rows) is not _EntityRowIntents
        or len(current.entities) != len(data.entity_rows)
        or not _same_plan_row_primitives(
            current.project,
            data.project_row,
            ProjectSnapshotRow,
            _ProjectRowIntent,
            (
                "project_id", "generation", "codec_id", "codec_version",
                "snapshot_format_version", "snapshot_bytes", "snapshot_sha256",
            ),
        )
        or not _same_plan_row_primitives(
            current.document,
            data.document_row,
            DocumentSnapshotRow,
            _DocumentRowIntent,
            (
                "project_id", "document_id", "document_version",
                "schema_version", "collaboration_mode", "save_policy",
                "codec_id", "codec_version", "snapshot_format_version",
                "snapshot_bytes", "snapshot_sha256",
            ),
        )
    ):
        return False
    entity_fields = (
        "project_id", "document_id", "entity_type", "entity_id",
        "entity_version", "deleted", "codec_id", "codec_version",
        "snapshot_format_version", "snapshot_bytes", "snapshot_sha256",
    )
    return all(
        _same_plan_row_primitives(
            current_row,
            intended_row,
            EntitySnapshotRow,
            _EntityRowIntent,
            entity_fields,
        )
        for current_row, intended_row in zip(
            current.entities,
            data.entity_rows,
        )
    )


def _prepared_record_matches_plan_data(
    record: _PreparedRecord,
    data: _VerifiedLegacyImportPlanData,
) -> bool:
    if (
        type(record) is not _PreparedRecord
        or type(data) is not _VerifiedLegacyImportPlanData
        or type(record.import_id) is not str
        or type(record.projects) is not tuple
        or type(record.entity_count) is not int
        or type(record.row_snapshot_bytes) is not int
        or type(record.framed_payload_bytes) is not int
        or type(record.manifest_bytes) is not bytes
        or type(record.manifest_sha256) is not bytes
        or type(record.payload_sha256) is not bytes
        or type(record.batch_sha256) is not bytes
        or record.import_id != data.import_id
        or record.entity_count != data.entity_count
        or record.row_snapshot_bytes != data.row_snapshot_bytes
        or record.framed_payload_bytes != data.framed_payload_bytes
        or record.manifest_bytes != data.manifest_bytes
        or record.manifest_sha256 != data.manifest_sha256
        or record.payload_sha256 != data.payload_sha256
        or record.batch_sha256 != data.batch_sha256
        or len(record.projects) != len(data.projects)
    ):
        return False
    return all(
        type(current) is _FrozenProject
        and current.source_head_sha256 is intended.source_head_sha256
        and current.source_head_bytes is intended.source_head_bytes
        and current.storage_version is intended.storage_version
        and current.row_snapshot_bytes is intended.row_snapshot_bytes
        and _same_snapshot_and_plan_rows(current.snapshot, intended)
        for current, intended in zip(record.projects, data.projects)
    )


class _LegacyImportLedger:
    """Single identity ledger for scan, prepared-package and receipt carriers."""

    def __init__(self) -> None:
        self._issuer_pid = os.getpid()
        self._issuer_epoch = 1
        self._install_empty_ledgers()

    def _install_empty_ledgers(self) -> None:
        # This helper deliberately does not acquire the previous lock.  A fork
        # can inherit an RLock while another parent thread owns it; touching
        # that lock in the child would deadlock before the fail-closed reset.
        self._lock = threading.RLock()
        self._scans: "weakref.WeakKeyDictionary[LegacyScanHandle, _ScanRecord]" = (
            weakref.WeakKeyDictionary()
        )
        self._packages: (
            "weakref.WeakKeyDictionary[FrozenLegacyImport, _PreparedRecord]"
        ) = weakref.WeakKeyDictionary()
        self._receipts: (
            "weakref.WeakKeyDictionary[PreparedLegacyPayloadReceipt, _PreparedRecord]"
        ) = weakref.WeakKeyDictionary()
        self._verified_import_plans: (
            "weakref.WeakKeyDictionary[VerifiedLegacyImportPlan, "
            "_IssuedVerifiedLegacyImportPlan]"
        ) = weakref.WeakKeyDictionary()
        self._verified_project_plans: (
            "weakref.WeakKeyDictionary[VerifiedLegacyProjectPlan, "
            "_VerifiedLegacyProjectPlanData]"
        ) = weakref.WeakKeyDictionary()
        # Deliberately strong: losing the caller's last reservation reference
        # must not silently erase membership while its record stays reserved.
        # An unresolved future write therefore remains visibly fail-closed
        # until an explicit consumed/release/quarantine decision.
        self._prepared_reservations: dict[
            int,
            tuple[
                _PreparedReservation,
                _PreparedRecord,
                str | None,
                VerifiedLegacyImportPlan | None,
            ],
        ] = {}
        # A weak terminal receipt makes finish acknowledgement-loss idempotent
        # while the caller still owns the reservation, without retaining old
        # packages after all external references disappear.
        self._prepared_resolutions: (
            "weakref.WeakKeyDictionary[_PreparedReservation, str]"
        ) = weakref.WeakKeyDictionary()

    def _after_fork_child(self) -> None:
        previous_epoch = self._issuer_epoch
        child_pid = os.getpid()
        self._install_empty_ledgers()
        # Publish the new identity only after the inherited lock and every
        # carrier registry have been replaced.  If allocation is interrupted,
        # the old PID remains visible and the lock-free PID fallback retries
        # this reset instead of treating a partially-reset child as current.
        self._issuer_pid = child_pid
        self._issuer_epoch = previous_epoch + 1

    def _ensure_current_process(self) -> None:
        # register_at_fork is the primary fence.  This second check keeps the
        # ledger closed on runtimes that change PID without invoking the hook.
        if os.getpid() != self._issuer_pid:
            self._after_fork_child()

    def _identity_is_current(self, issuer_pid: int, issuer_epoch: int) -> bool:
        return (
            issuer_pid == self._issuer_pid
            and issuer_epoch == self._issuer_epoch
        )

    def _record_is_current(self, record: _ScanRecord | _PreparedRecord) -> bool:
        return self._identity_is_current(record.issuer_pid, record.issuer_epoch)

    def _bind_record(self, record: _ScanRecord | _PreparedRecord) -> None:
        if record.issuer_pid != 0 or record.issuer_epoch != 0:
            _fail("legacy_import_payload_invalid", "record_already_issued")
        record.issuer_pid = self._issuer_pid
        record.issuer_epoch = self._issuer_epoch

    @staticmethod
    def _unbind_record(record: _ScanRecord | _PreparedRecord) -> None:
        record.issuer_pid = 0
        record.issuer_epoch = 0

    def issue_scan(self, record: _ScanRecord) -> LegacyScanHandle:
        self._ensure_current_process()
        if type(record) is not _ScanRecord:
            _fail("legacy_import_scan_invalid", "scan_record_invalid")
        handle = object.__new__(LegacyScanHandle)
        with self._lock:
            self._bind_record(record)
            try:
                self._scans[handle] = record
            except BaseException:
                self._unbind_record(record)
                raise
        return handle

    def scan(self, value: Any) -> _ScanRecord:
        self._ensure_current_process()
        if type(value) is not LegacyScanHandle:
            _fail("legacy_import_handle_invalid", "scan_handle_invalid")
        with self._lock:
            record = self._scans.get(value)
        if record is None or not self._record_is_current(record):
            _fail("legacy_import_handle_invalid", "scan_handle_invalid")
        return record

    def reserve_payload_batch(
        self,
        handles: Any,
    ) -> _PayloadReservation:
        self._ensure_current_process()
        if type(handles) is not tuple or not handles:
            _fail("legacy_import_prepare_invalid", "handles_must_be_nonempty_tuple")
        if len(handles) > MAX_LEGACY_PROJECTS:
            _fail("legacy_import_budget_exceeded", "project_count_exceeded")

        with self._lock:
            pairs: list[tuple[LegacyScanHandle, _ScanRecord]] = []
            seen_handles: set[int] = set()
            seen_projects: set[str] = set()
            total_entities = 0
            total_bytes = 0
            for handle in handles:
                if type(handle) is not LegacyScanHandle:
                    _fail("legacy_import_handle_invalid", "scan_handle_invalid")
                identity = id(handle)
                if identity in seen_handles:
                    _fail("legacy_import_prepare_invalid", "duplicate_handle")
                seen_handles.add(identity)
                record = self._scans.get(handle)
                if record is None or not self._record_is_current(record):
                    _fail("legacy_import_handle_invalid", "scan_handle_invalid")
                project_id = record.snapshot.project.project_id
                if project_id in seen_projects:
                    _fail("legacy_import_prepare_invalid", "duplicate_project")
                seen_projects.add(project_id)
                if record.payload_state != "available":
                    _fail("legacy_import_payload_consumed", "payload_lane_unavailable")
                total_entities += len(record.snapshot.entities)
                total_bytes += record.row_snapshot_bytes
                if total_entities > MAX_LEGACY_ENTITIES:
                    _fail("legacy_import_budget_exceeded", "entity_count_exceeded")
                if total_bytes > MAX_LEGACY_ROW_SNAPSHOT_BYTES:
                    _fail("legacy_import_budget_exceeded", "row_snapshot_bytes_exceeded")
                pairs.append((handle, record))

            pairs.sort(key=lambda pair: pair[1].snapshot.project.project_id)
            token = object()
            reservation = _PayloadReservation(
                handles=tuple(pair[0] for pair in pairs),
                records=tuple(pair[1] for pair in pairs),
                token=token,
                issuer_pid=self._issuer_pid,
                issuer_epoch=self._issuer_epoch,
            )
            try:
                for _handle, record in pairs:
                    record.payload_state = "reserved"
                    record.payload_token = token
            except BaseException:
                for _handle, record in pairs:
                    record.payload_state = "available"
                    record.payload_token = None
                raise
            return reservation

    def _payload_reservation_is_active(
        self,
        reservation: _PayloadReservation,
    ) -> bool:
        return (
            type(reservation) is _PayloadReservation
            and self._identity_is_current(
                reservation.issuer_pid,
                reservation.issuer_epoch,
            )
            and len(reservation.handles) == len(reservation.records)
            and bool(reservation.records)
            and all(
                self._scans.get(handle) is record
                and self._record_is_current(record)
                and record.payload_state == "reserved"
                and record.payload_token is reservation.token
                for handle, record in zip(
                    reservation.handles,
                    reservation.records,
                )
            )
        )

    def payload_reservation_records(
        self,
        reservation: _PayloadReservation,
    ) -> tuple[_ScanRecord, ...]:
        self._ensure_current_process()
        with self._lock:
            if not self._payload_reservation_is_active(reservation):
                _fail("legacy_import_prepare_failed", "payload_reservation_lost")
            return reservation.records

    def abort_payload_batch(self, reservation: _PayloadReservation) -> None:
        self._ensure_current_process()
        if type(reservation) is not _PayloadReservation:
            return
        with self._lock:
            if not self._payload_reservation_is_active(reservation):
                return
            for record in reservation.records:
                record.payload_state = "available"
                record.payload_token = None

    def finish_payload_batch(
        self,
        reservation: _PayloadReservation,
        prepared: _PreparedRecord,
    ) -> tuple[FrozenLegacyImport, PreparedLegacyPayloadReceipt]:
        self._ensure_current_process()
        if type(prepared) is not _PreparedRecord:
            _fail("legacy_import_prepare_failed", "prepared_record_invalid")
        package = object.__new__(FrozenLegacyImport)
        receipt = object.__new__(PreparedLegacyPayloadReceipt)
        result = (package, receipt)
        with self._lock:
            if not self._payload_reservation_is_active(reservation):
                _fail("legacy_import_prepare_failed", "payload_reservation_lost")
            self._bind_record(prepared)
            try:
                # Neither carrier may escape until both identity indexes exist.
                # In particular, a failure inserting the receipt must not leave
                # the entire batch consumed or one half-issued package behind.
                self._packages[package] = prepared
                self._receipts[receipt] = prepared
                for record in reservation.records:
                    record.payload_state = "consumed"
                    record.payload_token = None
            except BaseException:
                for record in reservation.records:
                    record.payload_state = "reserved"
                    record.payload_token = reservation.token
                try:
                    self._receipts.pop(receipt, None)
                except BaseException:
                    pass
                try:
                    self._packages.pop(package, None)
                except BaseException:
                    pass
                self._unbind_record(prepared)
                raise
        return result

    def prepared_package(self, value: Any) -> _PreparedRecord:
        self._ensure_current_process()
        if type(value) is not FrozenLegacyImport:
            _fail("legacy_import_payload_invalid", "prepared_package_invalid")
        with self._lock:
            record = self._packages.get(value)
        if record is None or not self._record_is_current(record):
            _fail("legacy_import_payload_invalid", "prepared_package_invalid")
        return record

    def prepared_receipt(self, value: Any) -> _PreparedRecord:
        self._ensure_current_process()
        if type(value) is not PreparedLegacyPayloadReceipt:
            _fail("legacy_import_receipt_invalid", "prepared_receipt_invalid")
        with self._lock:
            record = self._receipts.get(value)
        if record is None or not self._record_is_current(record):
            _fail("legacy_import_receipt_invalid", "prepared_receipt_invalid")
        return record

    def reserve_runtime(self, handle: Any) -> _RuntimeReservation:
        self._ensure_current_process()
        if type(handle) is not LegacyScanHandle:
            _fail("legacy_import_handle_invalid", "scan_handle_invalid")
        with self._lock:
            record = self._scans.get(handle)
            if record is None or not self._record_is_current(record):
                _fail("legacy_import_handle_invalid", "scan_handle_invalid")
            if record.runtime_state != "available":
                _fail("legacy_import_runtime_consumed", "runtime_lane_unavailable")
            token = object()
            reservation = _RuntimeReservation(
                handle=handle,
                record=record,
                token=token,
                issuer_pid=self._issuer_pid,
                issuer_epoch=self._issuer_epoch,
            )
            try:
                record.runtime_state = "reserved"
                record.runtime_token = token
            except BaseException:
                record.runtime_state = "available"
                record.runtime_token = None
                raise
            return reservation

    def _runtime_reservation_is_active(
        self,
        reservation: _RuntimeReservation,
    ) -> bool:
        return (
            type(reservation) is _RuntimeReservation
            and self._identity_is_current(
                reservation.issuer_pid,
                reservation.issuer_epoch,
            )
            and self._scans.get(reservation.handle) is reservation.record
            and self._record_is_current(reservation.record)
            and reservation.record.runtime_state == "reserved"
            and reservation.record.runtime_token is reservation.token
        )

    def runtime_reservation_record(
        self,
        reservation: _RuntimeReservation,
    ) -> _ScanRecord:
        self._ensure_current_process()
        with self._lock:
            if not self._runtime_reservation_is_active(reservation):
                _fail("legacy_import_runtime_failed", "runtime_reservation_lost")
            return reservation.record

    def abort_runtime(self, reservation: _RuntimeReservation) -> None:
        self._ensure_current_process()
        if type(reservation) is not _RuntimeReservation:
            return
        with self._lock:
            if not self._runtime_reservation_is_active(reservation):
                return
            reservation.record.runtime_state = "available"
            reservation.record.runtime_token = None

    def finish_runtime(self, reservation: _RuntimeReservation) -> None:
        self._ensure_current_process()
        with self._lock:
            if not self._runtime_reservation_is_active(reservation):
                _fail("legacy_import_runtime_failed", "runtime_reservation_lost")
            reservation.record.runtime_state = "consumed"
            reservation.record.runtime_token = None

    def reserve_prepared(
        self,
        package: Any,
        receipt: Any,
        attempt_token: Any,
    ) -> _PreparedReservation:
        self._ensure_current_process()
        if type(package) is not FrozenLegacyImport:
            _fail("legacy_import_payload_invalid", "prepared_package_invalid")
        if type(receipt) is not PreparedLegacyPayloadReceipt:
            _fail("legacy_import_receipt_invalid", "prepared_receipt_invalid")
        if type(attempt_token) is not object:
            _fail("legacy_import_payload_invalid", "adapter_attempt_invalid")
        with self._lock:
            package_record = self._packages.get(package)
            receipt_record = self._receipts.get(receipt)
            if (
                package_record is None
                or receipt_record is not package_record
                or not self._record_is_current(package_record)
            ):
                _fail("legacy_import_receipt_invalid", "prepared_pair_invalid")
            if package_record.adapter_state != "available":
                _fail("legacy_import_payload_consumed", "adapter_lane_unavailable")
            record_identity = id(package_record)
            if record_identity in self._prepared_reservations:
                _fail("legacy_import_payload_consumed", "adapter_lane_unavailable")
            token = object()
            reservation = _PreparedReservation(
                package=package,
                receipt=receipt,
                record=package_record,
                token=token,
                attempt_token=attempt_token,
                issuer_pid=self._issuer_pid,
                issuer_epoch=self._issuer_epoch,
            )
            try:
                package_record.adapter_state = "reserved"
                package_record.adapter_token = token
                self._prepared_reservations[record_identity] = (
                    reservation,
                    package_record,
                    None,
                    None,
                )
            except BaseException:
                self._prepared_reservations.pop(record_identity, None)
                package_record.adapter_state = "available"
                package_record.adapter_token = None
                raise
            return reservation

    def _prepared_reservation_is_active(
        self,
        reservation: _PreparedReservation,
    ) -> bool:
        if type(reservation) is not _PreparedReservation:
            return False
        try:
            record = object.__getattribute__(reservation, "record")
        except AttributeError:
            return False
        if type(record) is not _PreparedRecord:
            return False
        registered = self._prepared_reservations.get(id(record))
        return (
            registered is not None
            and registered[0] is reservation
            and registered[1] is record
            and registered[2] is None
            and self._identity_is_current(
                reservation.issuer_pid,
                reservation.issuer_epoch,
            )
            and self._packages.get(reservation.package) is reservation.record
            and self._receipts.get(reservation.receipt) is reservation.record
            and self._record_is_current(reservation.record)
            and reservation.record.adapter_state == "reserved"
            and reservation.record.adapter_token is reservation.token
        )

    def prepared_projection_source(
        self,
        reservation: _PreparedReservation,
    ) -> _PreparedProjectionSource:
        self._ensure_current_process()
        with self._lock:
            if not self._prepared_reservation_is_active(reservation):
                _fail("legacy_import_payload_invalid", "adapter_reservation_lost")
            return _capture_prepared_projection(reservation.record)

    def verified_import_plan(
        self,
        value: Any,
    ) -> _IssuedVerifiedLegacyImportPlan:
        self._ensure_current_process()
        if type(value) is not VerifiedLegacyImportPlan:
            _fail("legacy_import_payload_invalid", "verified_plan_invalid")
        with self._lock:
            issued = self._verified_import_plans.get(value)
        if issued is None or not self._identity_is_current(
            issued.issuer_pid,
            issued.issuer_epoch,
        ):
            _fail("legacy_import_payload_invalid", "verified_plan_invalid")
        return issued

    def verified_project_plan(
        self,
        value: Any,
    ) -> _VerifiedLegacyProjectPlanData:
        self._ensure_current_process()
        if type(value) is not VerifiedLegacyProjectPlan:
            _fail("legacy_import_payload_invalid", "verified_project_plan_invalid")
        with self._lock:
            data = self._verified_project_plans.get(value)
        if data is None:
            _fail("legacy_import_payload_invalid", "verified_project_plan_invalid")
        return data

    def bound_prepared_plan(
        self,
        reservation: _PreparedReservation,
    ) -> VerifiedLegacyImportPlan | None:
        self._ensure_current_process()
        with self._lock:
            if not self._prepared_reservation_is_active(reservation):
                _fail("legacy_import_payload_invalid", "adapter_reservation_lost")
            return self._prepared_reservations[id(reservation.record)][3]

    def bind_prepared_plan(
        self,
        reservation: _PreparedReservation,
        source: _PreparedProjectionSource,
        data: _VerifiedLegacyImportPlanData,
    ) -> VerifiedLegacyImportPlan:
        self._ensure_current_process()
        if type(source) is not _PreparedProjectionSource:
            _fail("legacy_import_payload_invalid", "prepared_projection_invalid")
        if type(data) is not _VerifiedLegacyImportPlanData:
            _fail("legacy_import_payload_invalid", "verified_plan_invalid")
        with self._lock:
            if not self._prepared_reservation_is_active(reservation):
                _fail("legacy_import_payload_invalid", "adapter_reservation_lost")
            record_identity = id(reservation.record)
            registered = self._prepared_reservations[record_identity]
            if registered[3] is not None:
                return registered[3]

            # Confirmation and issuance share the ledger critical section.  A
            # managed state transition therefore cannot slip between the final
            # sealed-record comparison and binding the exact opaque plan.
            if not _prepared_record_matches_projection(
                reservation.record,
                source,
            ):
                _fail("legacy_import_payload_invalid", "prepared_record_changed")

            project_carriers = tuple(
                object.__new__(VerifiedLegacyProjectPlan)
                for _project in data.projects
            )
            plan = object.__new__(VerifiedLegacyImportPlan)
            issued = _IssuedVerifiedLegacyImportPlan(
                data=data,
                projects=project_carriers,
                issuer_pid=self._issuer_pid,
                issuer_epoch=self._issuer_epoch,
            )
            inserted_projects: list[VerifiedLegacyProjectPlan] = []
            try:
                for carrier, project_data in zip(project_carriers, data.projects):
                    self._verified_project_plans[carrier] = project_data
                    inserted_projects.append(carrier)
                self._verified_import_plans[plan] = issued
                self._prepared_reservations[record_identity] = (
                    reservation,
                    reservation.record,
                    None,
                    plan,
                )
            except BaseException:
                self._verified_import_plans.pop(plan, None)
                for carrier in inserted_projects:
                    self._verified_project_plans.pop(carrier, None)
                raise
            return plan

    def recover_prepared_reservation(
        self,
        package: Any,
        receipt: Any,
        attempt_token: Any,
    ) -> _PreparedReservation:
        """Recover the exact active attempt, including a pre-plan reserve."""

        self._ensure_current_process()
        if type(package) is not FrozenLegacyImport:
            _fail("legacy_import_payload_invalid", "prepared_package_invalid")
        if type(receipt) is not PreparedLegacyPayloadReceipt:
            _fail("legacy_import_receipt_invalid", "prepared_receipt_invalid")
        if type(attempt_token) is not object:
            _fail("legacy_import_payload_invalid", "adapter_recovery_unavailable")
        with self._lock:
            package_record = self._packages.get(package)
            receipt_record = self._receipts.get(receipt)
            if (
                package_record is None
                or receipt_record is not package_record
                or not self._record_is_current(package_record)
            ):
                _fail("legacy_import_receipt_invalid", "prepared_pair_invalid")
            registered = self._prepared_reservations.get(id(package_record))
            if (
                registered is None
                or registered[1] is not package_record
                or registered[0].package is not package
                or registered[0].receipt is not receipt
                or registered[0].attempt_token is not attempt_token
            ):
                _fail("legacy_import_payload_invalid", "adapter_recovery_unavailable")
            if registered[2] is not None:
                pending_outcome = registered[2]
                self.finish_prepared(registered[0], pending_outcome)
                if pending_outcome == "quarantine":
                    _fail("legacy_import_payload_invalid", "prepared_record_changed")
                _fail("legacy_import_payload_invalid", "adapter_recovery_unavailable")
            if not self._prepared_reservation_is_active(registered[0]):
                _fail("legacy_import_payload_invalid", "adapter_recovery_unavailable")
            return registered[0]

    def verify_prepared_plan_binding(
        self,
        reservation: _PreparedReservation,
        plan: Any,
    ) -> None:
        """Prove that the private SQLite bridge holds the exact active pair."""

        self._ensure_current_process()
        with self._lock:
            if not self._prepared_reservation_is_active(reservation):
                _fail("legacy_import_payload_invalid", "adapter_reservation_lost")
            registered = self._prepared_reservations[id(reservation.record)]
            issued = (
                self._verified_import_plans.get(plan)
                if type(plan) is VerifiedLegacyImportPlan
                else None
            )
            if (
                type(plan) is not VerifiedLegacyImportPlan
                or registered[3] is not plan
                or issued is None
            ):
                _fail("legacy_import_payload_invalid", "verified_plan_binding_invalid")
            if not _prepared_record_matches_plan_data(
                reservation.record,
                issued.data,
            ):
                _fail("legacy_import_payload_invalid", "prepared_record_changed")

    def finish_prepared(
        self,
        reservation: _PreparedReservation,
        outcome: str,
    ) -> None:
        # "consumed" is only a terminal lane decision.  It is deliberately
        # not named "commit": the private bridge may select it only after its
        # durable commit/fresh reconciliation has established the outcome.
        self._ensure_current_process()
        if type(outcome) is not str or outcome not in {
            "consumed", "release", "quarantine",
        }:
            _fail("legacy_import_payload_invalid", "adapter_outcome_invalid")
        with self._lock:
            if type(reservation) is not _PreparedReservation:
                _fail("legacy_import_payload_invalid", "adapter_reservation_lost")
            resolved_outcome = self._prepared_resolutions.get(reservation)
            if resolved_outcome is not None:
                try:
                    resolved_record = object.__getattribute__(reservation, "record")
                except AttributeError:
                    _fail("legacy_import_payload_invalid", "adapter_reservation_lost")
                if (
                    type(resolved_record) is not _PreparedRecord
                    or not self._identity_is_current(
                        reservation.issuer_pid,
                        reservation.issuer_epoch,
                    )
                    or not self._record_is_current(resolved_record)
                    or self._packages.get(reservation.package) is not resolved_record
                    or self._receipts.get(reservation.receipt) is not resolved_record
                ):
                    _fail("legacy_import_payload_invalid", "adapter_reservation_lost")
                if resolved_outcome != outcome:
                    _fail("legacy_import_payload_invalid", "adapter_outcome_conflict")
                registered = self._prepared_reservations.get(
                    id(resolved_record)
                )
                if registered is not None and registered[0] is reservation:
                    resolved_record.adapter_token = None
                    self._prepared_reservations.pop(id(resolved_record), None)
                return
            try:
                record = object.__getattribute__(reservation, "record")
            except AttributeError:
                _fail("legacy_import_payload_invalid", "adapter_reservation_lost")
            if type(record) is not _PreparedRecord:
                _fail("legacy_import_payload_invalid", "adapter_reservation_lost")
            record_identity = id(record)
            registered = self._prepared_reservations.get(record_identity)
            if (
                registered is None
                or registered[0] is not reservation
                or registered[1] is not record
                or not self._identity_is_current(
                    reservation.issuer_pid,
                    reservation.issuer_epoch,
                )
                or self._packages.get(reservation.package) is not record
                or self._receipts.get(reservation.receipt) is not record
                or not self._record_is_current(record)
            ):
                _fail("legacy_import_payload_invalid", "adapter_reservation_lost")
            pending_outcome = registered[2]
            if pending_outcome is not None and pending_outcome != outcome:
                _fail("legacy_import_payload_invalid", "adapter_outcome_conflict")
            next_state = {
                "consumed": "consumed",
                "release": "available",
                "quarantine": "quarantined",
            }[outcome]
            if pending_outcome is None:
                if (
                    record.adapter_state != "reserved"
                    or record.adapter_token is not reservation.token
                ):
                    _fail("legacy_import_payload_invalid", "adapter_reservation_lost")
                # Persist the intended decision in the strong registry first.
                # If an async BaseException lands after this point, retrying
                # the same outcome can finish; a different outcome is rejected.
                self._prepared_reservations[record_identity] = (
                    reservation,
                    record,
                    outcome,
                    registered[3],
                )
            if record.adapter_state == "reserved":
                if record.adapter_token is not reservation.token:
                    _fail("legacy_import_payload_invalid", "adapter_reservation_lost")
                record.adapter_state = next_state
            elif record.adapter_state != next_state:
                _fail("legacy_import_payload_invalid", "adapter_reservation_lost")
            record.adapter_token = None
            # Publish the process-local terminal acknowledgement before
            # deleting active membership.  An async exception before the pop
            # remains retryable through the pending entry; after the pop it is
            # retryable through this weak terminal receipt.
            self._prepared_resolutions[reservation] = outcome
            self._prepared_reservations.pop(record_identity, None)


_LEDGER = _LegacyImportLedger()
if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_LEDGER._after_fork_child)


def _issue_scan_handle(
    snapshot: BusinessSnapshotSet,
    *,
    source_head_sha256: bytes,
    source_head_bytes: int,
    storage_version: str,
) -> LegacyScanHandle:
    try:
        verified = verify_business_snapshot(snapshot)
    except BusinessSnapshotError as exc:
        raise LegacyImportError("legacy_import_scan_invalid", exc.code) from None
    row_bytes = _row_snapshot_bytes(verified)
    frozen = _FrozenProject(
        snapshot=verified,
        source_head_sha256=source_head_sha256,
        source_head_bytes=source_head_bytes,
        storage_version=storage_version,
        row_snapshot_bytes=row_bytes,
    )
    _verify_frozen_project(frozen)
    return _LEDGER.issue_scan(_ScanRecord(
        snapshot=verified,
        source_head_sha256=source_head_sha256,
        source_head_bytes=source_head_bytes,
        storage_version=storage_version,
        row_snapshot_bytes=row_bytes,
    ))


def _prepare_frozen_import(
    import_id: Any,
    handles: Any,
) -> tuple[FrozenLegacyImport, PreparedLegacyPayloadReceipt]:
    _validate_import_id(import_id)
    reservation = _LEDGER.reserve_payload_batch(handles)
    try:
        records = _LEDGER.payload_reservation_records(reservation)
        projects = tuple(_frozen_project(record) for record in records)
        prepared = _build_prepared_record(import_id, projects)
        return _LEDGER.finish_payload_batch(reservation, prepared)
    except LegacyImportError:
        _LEDGER.abort_payload_batch(reservation)
        raise
    except Exception:
        _LEDGER.abort_payload_batch(reservation)
        raise LegacyImportError(
            "legacy_import_prepare_failed", "internal_failure",
        ) from None
    except BaseException:
        _LEDGER.abort_payload_batch(reservation)
        raise


def _reserve_runtime_handle(handle: Any) -> _RuntimeReservation:
    return _LEDGER.reserve_runtime(handle)


def _abort_runtime_handle(reservation: _RuntimeReservation) -> None:
    _LEDGER.abort_runtime(reservation)


def _finish_runtime_handle(reservation: _RuntimeReservation) -> None:
    _LEDGER.finish_runtime(reservation)


def _verified_runtime_snapshot(
    reservation: _RuntimeReservation,
) -> BusinessSnapshotSet:
    record = _LEDGER.runtime_reservation_record(reservation)
    try:
        verified = verify_business_snapshot(record.snapshot)
    except BusinessSnapshotError as exc:
        raise LegacyImportError(
            "legacy_import_runtime_failed", exc.code,
        ) from None
    if _LEDGER.runtime_reservation_record(reservation) is not record:
        _fail("legacy_import_runtime_failed", "runtime_reservation_lost")
    return verified


def _reserve_prepared_import(
    package: Any,
    receipt: Any,
    attempt_token: Any,
) -> _PreparedReservation:
    return _LEDGER.reserve_prepared(package, receipt, attempt_token)


def _verified_prepared_plan(
    reservation: _PreparedReservation,
) -> VerifiedLegacyImportPlan:
    bound = _LEDGER.bound_prepared_plan(reservation)
    if bound is not None:
        return bound
    source = _LEDGER.prepared_projection_source(reservation)
    data = _verify_prepared_projection(source)
    return _LEDGER.bind_prepared_plan(reservation, source, data)


def _recover_prepared_import(
    package: Any,
    receipt: Any,
    attempt_token: Any,
) -> tuple[_PreparedReservation, VerifiedLegacyImportPlan]:
    reservation = _LEDGER.recover_prepared_reservation(
        package,
        receipt,
        attempt_token,
    )
    try:
        # A return-edge interruption may happen immediately after the ledger
        # installs the strong reservation but before the facade receives it.
        # Exact-token recovery therefore completes an unbound plan as well as
        # returning an already-bound one, then rechecks the live pair either
        # way.  A later retry remains possible if this function's own return
        # acknowledgement is lost.
        plan = _verified_prepared_plan(reservation)
        _verify_prepared_plan_binding(reservation, plan)
        return reservation, plan
    except LegacyImportError:
        _finish_prepared_import(reservation, outcome="quarantine")
        raise
    except Exception:
        _finish_prepared_import(reservation, outcome="quarantine")
        raise LegacyImportError(
            "legacy_import_payload_invalid", "internal_failure",
        ) from None
    except BaseException:
        _finish_prepared_import(reservation, outcome="release")
        raise


def _verify_prepared_plan_binding(
    reservation: _PreparedReservation,
    plan: Any,
) -> None:
    _LEDGER.verify_prepared_plan_binding(reservation, plan)


def _finish_prepared_import(
    reservation: _PreparedReservation,
    *,
    outcome: str,
) -> None:
    _LEDGER.finish_prepared(reservation, outcome)


__all__ = [
    "FrozenLegacyImport",
    "LegacyImportError",
    "LegacyScanHandle",
    "PreparedLegacyPayloadReceipt",
]
