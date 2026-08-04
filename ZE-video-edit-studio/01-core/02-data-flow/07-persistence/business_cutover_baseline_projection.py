"""Pure SQL-row projection for a V11 cutover baseline.

``BusinessCutoverPlan`` closes source identity and the baseline manifests, but
it deliberately does not say which exact columns a production cutover writer
must insert.  This module is that missing, I/O-free boundary.  It rebuilds the
issuer-stamped plan and projects every live snapshot and deleted high-water
member into immutable rows whose field order is the current V11 table order.

The cutover coordinator supplies one canonical install timestamp.  Baseline
receipts, project/head timestamps and the three scheduler genesis heads use
that value.  Install precedes mount.  The frozen production-atomic protocol,
however, permits only commits at or after mount; consequently a pre-mount
baseline can project only its canonical zero-control *inner* seed.  This module
marks the missing outer carrier ``requires_schema_upgrade`` and refuses to
pretend that a future mount can retroactively authorize it.  V12 (or a new
baseline-specific protocol) must close that schema-level chronology gap.

Projection and chronology preflight perform no filesystem, SQLite, clock or
network operation.  A writer must still use fixed SQL, preserve the documented
column order, bind expected blob metadata to asset coverage, and freshly read
the durable rows before advancing authority.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional, Tuple

from asset_reference_projection import (
    AssetReferenceProjectionError,
    project_asset_references,
    verify_asset_reference_projection,
)
from business_atomic_write_contract import (
    ATOMIC_BUSINESS_WRITE_MANIFEST_SCHEMA,
    ATOMIC_BUSINESS_WRITE_PLAN_STATUS,
    ATOMIC_BUSINESS_WRITE_PROTOCOL,
    MAX_ATOMIC_BUSINESS_WRITE_MANIFEST_BYTES,
    MAX_ATOMIC_BUSINESS_WRITE_MANIFEST_NODES,
    AtomicBusinessWriteGenericReceiptEvidence,
)
from business_atomic_write_integrity import (
    ATOMIC_BUSINESS_WRITE_CONTROLS_HASH_DOMAIN,
    _atomic_receipt_digest,
    _digest as _atomic_digest,
    _manifest_digest as _atomic_manifest_digest,
)
from business_codec import BusinessCodecError, encode_payload_bounded
from business_cutover_contract import (
    BUSINESS_CUTOVER_MEMBER_DELETED,
    BUSINESS_CUTOVER_MEMBER_LIVE,
    MAX_BUSINESS_CUTOVER_MANIFEST_BYTES,
    MAX_BUSINESS_CUTOVER_MANIFEST_NODES,
)
from business_cutover_integrity import (
    BusinessCutoverIntegrityError,
    BusinessCutoverPlan,
    _require_business_cutover_plan,
)
from business_snapshot import (
    MAX_ENTITY_COUNT,
    MAX_TOTAL_SNAPSHOT_BYTES,
    BusinessSnapshotError,
    BusinessSnapshotSet,
    verify_business_snapshot,
)
from business_transaction_integrity import _component_digest
from sqlite_schema import (
    BUSINESS_TRANSACTION_KIND,
    BUSINESS_TRANSACTION_PREFIX,
    MAX_BUSINESS_SMALL_PAYLOAD_BYTES,
)
from worker_execution_contract import EMPTY_WORKER_LEDGER_HEAD


BUSINESS_CUTOVER_BASELINE_PROJECTION_PROTOCOL = (
    "ze.business-cutover-baseline-projection.v1"
)
BUSINESS_CUTOVER_BASELINE_PROJECTION_SCHEMA = (
    "ze.business-cutover-baseline-projection.manifest.v1"
)
BUSINESS_CUTOVER_BASELINE_PROJECTION_STATUS = "projected"
BUSINESS_CUTOVER_BASELINE_TRACE_PREFIX = "business-cutover-baseline:"
BUSINESS_CUTOVER_BASELINE_OUTER_ATOMIC_STATUS = "requires_schema_upgrade"
BUSINESS_CUTOVER_BASELINE_PROJECTION_HASH_DOMAIN = (
    b"ze.business-cutover-baseline-projection.manifest-digest.v1"
)
BUSINESS_CUTOVER_BASELINE_EMPTY_DIGEST = b"\0" * 32

# These are repository-wide projection budgets, not per-project snapshot
# budgets.  They make the eventual SQL transaction bounded before a writer
# acquires database authority.
MAX_BUSINESS_CUTOVER_BASELINE_TOTAL_SNAPSHOT_BYTES = MAX_TOTAL_SNAPSHOT_BYTES
MAX_BUSINESS_CUTOVER_BASELINE_ENTITY_ROWS = MAX_ENTITY_COUNT
MAX_BUSINESS_CUTOVER_BASELINE_SQL_PAYLOAD_BYTES = (
    MAX_BUSINESS_SMALL_PAYLOAD_BYTES
)

PROJECTS_COLUMNS = (
    "repository_singleton",
    "project_id",
    "generation",
    "codec_id",
    "codec_version",
    "snapshot_format_version",
    "snapshot_bytes",
    "snapshot_sha256",
)
DOCUMENTS_COLUMNS = (
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
ENTITIES_COLUMNS = (
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
TRANSACTION_RECEIPTS_COLUMNS = (
    "transaction_id",
    "kind",
    "committed_at",
)
BUSINESS_COMMITS_COLUMNS = (
    "commit_id",
    "transaction_id",
    "receipt_kind",
    "project_id",
    "generation",
    "repository_revision",
    "repository_epoch",
    "cutover_id",
    "source_admission_revision",
    "document_id",
    "document_version",
    "previous_commit_id",
    "commit_kind",
    "save_policy",
    "trace_id",
    "operation_count",
    "operation_target_count",
    "last_operation_id",
    "last_operation_ordinal",
    "outbox_event_count",
    "last_outbox_event_id",
    "last_outbox_ordinal",
    "durable_action_count",
    "pipeline_intent_count",
    "before_root_sha256",
    "after_root_sha256",
    "previous_operation_tail_sha256",
    "operation_tail_sha256",
    "asset_projection_sha256",
    "asset_reference_count",
    "entity_count",
    "manifest_bytes",
    "manifest_sha256",
    "committed_at",
    "project_revision",
    "head_revision",
)
REPOSITORY_PROJECTS_COLUMNS = (
    "repository_singleton",
    "project_id",
    "generation",
    "project_revision",
    "status",
    "current_snapshot_sha256",
    "created_at",
    "updated_at",
    "deleted_at",
)
DOCUMENT_COMMIT_HEADS_COLUMNS = (
    "project_id",
    "generation",
    "document_id",
    "document_version",
    "commit_id",
    "head_revision",
    "status",
    "current_snapshot_sha256",
    "current_project_snapshot_sha256",
    "business_root_sha256",
    "operation_tail_sha256",
    "asset_projection_sha256",
    "asset_reference_count",
    "entity_count",
    "updated_at",
)
ASSET_BLOBS_COLUMNS = (
    "asset_sha256",
    "content_key",
    "media_type",
    "extension",
    "byte_length",
    "status",
    "state_revision",
    "created_at",
    "updated_at",
)
DOCUMENT_ASSET_REFS_COLUMNS = (
    "project_id",
    "generation",
    "document_id",
    "document_version",
    "commit_id",
    "commit_asset_reference_count",
    "ordinal",
    "asset_sha256",
    "entity_type",
    "entity_id",
    "entity_deleted",
    "value_path",
    "ref_mime",
    "ref_size",
    "ref_ext",
)
WORKER_LEDGER_HEAD_COLUMNS = (
    "singleton",
    "state_revision",
    "sequence",
    "previous_tail_sha256",
    "tail_sha256",
    "updated_at",
)
OUTBOX_ALLOCATION_HEAD_COLUMNS = WORKER_LEDGER_HEAD_COLUMNS
OUTBOX_DISPATCH_HEAD_COLUMNS = (
    "singleton",
    "state_revision",
    "published_sequence",
    "published_repository_revision",
    "published_ordinal",
    "inflight_sequence",
    "inflight_repository_revision",
    "inflight_ordinal",
    "previous_head_sha256",
    "head_sha256",
    "updated_at",
)
_PROJECTION_ISSUER = object()


class BusinessCutoverBaselineProjectionError(ValueError):
    """Stable, payload-free failure at the pure projection boundary."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        if type(code) is not str or not code:
            raise TypeError("code must be a non-empty exact string")
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise BusinessCutoverBaselineProjectionError(code)


def _timestamp(value: Any) -> str:
    if type(value) is not str or len(value) != 24:
        _fail("business_cutover_baseline_timestamp_invalid")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except (TypeError, ValueError):
        _fail("business_cutover_baseline_timestamp_invalid")
        raise AssertionError("unreachable")
    canonical = parsed.strftime("%Y-%m-%dT%H:%M:%S.") + (
        "%03dZ" % (parsed.microsecond // 1000)
    )
    if value != canonical:
        _fail("business_cutover_baseline_timestamp_invalid")
    return value


def _encode(value: Any, code: str) -> bytes:
    try:
        return encode_payload_bounded(
            value,
            MAX_BUSINESS_CUTOVER_MANIFEST_BYTES,
            MAX_BUSINESS_CUTOVER_MANIFEST_NODES,
        )
    except BusinessCodecError as exc:
        raise BusinessCutoverBaselineProjectionError(code) from exc


def _projection_digest(value: bytes) -> bytes:
    hasher = hashlib.sha256()
    for member in (BUSINESS_CUTOVER_BASELINE_PROJECTION_HASH_DOMAIN, value):
        hasher.update(len(member).to_bytes(8, "big"))
        hasher.update(member)
    return hasher.digest()


def _sql_values(value: Any, columns: Tuple[str, ...]) -> tuple:
    return tuple(getattr(value, column) for column in columns)


def _merge_asset_metadata(
    existing: Optional[tuple[Optional[str], Optional[int], Optional[str]]],
    candidate: tuple[Optional[str], Optional[int], Optional[str]],
) -> tuple[Optional[str], Optional[int], Optional[str]]:
    if existing is None:
        return candidate

    def known(left: Any, right: Any) -> Any:
        if left is not None and right is not None and left != right:
            _fail("business_cutover_baseline_asset_metadata_conflict")
        return left if left is not None else right

    return (
        known(existing[0], candidate[0]),
        known(existing[1], candidate[1]),
        known(existing[2], candidate[2]),
    )


@dataclass(frozen=True)
class BusinessCutoverProjectRow:
    repository_singleton: int
    project_id: str
    generation: int
    codec_id: str
    codec_version: int
    snapshot_format_version: int
    snapshot_bytes: bytes = field(repr=False)
    snapshot_sha256: bytes = field(repr=False)

    def sql_values(self) -> tuple:
        return _sql_values(self, PROJECTS_COLUMNS)


@dataclass(frozen=True)
class BusinessCutoverDocumentRow:
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

    def sql_values(self) -> tuple:
        return _sql_values(self, DOCUMENTS_COLUMNS)


@dataclass(frozen=True)
class BusinessCutoverEntityRow:
    project_id: str
    document_id: str
    entity_type: str
    entity_id: str
    entity_version: int
    deleted: int
    codec_id: str
    codec_version: int
    snapshot_format_version: int
    snapshot_bytes: bytes = field(repr=False)
    snapshot_sha256: bytes = field(repr=False)

    def sql_values(self) -> tuple:
        return _sql_values(self, ENTITIES_COLUMNS)


@dataclass(frozen=True)
class BusinessCutoverTransactionReceiptRow:
    transaction_id: str
    kind: str
    committed_at: str

    def sql_values(self) -> tuple:
        return _sql_values(self, TRANSACTION_RECEIPTS_COLUMNS)


@dataclass(frozen=True)
class BusinessCutoverCommitRow:
    commit_id: str
    transaction_id: str
    receipt_kind: str
    project_id: str
    generation: int
    repository_revision: int
    repository_epoch: str
    cutover_id: str
    source_admission_revision: int
    document_id: str
    document_version: int
    previous_commit_id: Optional[str]
    commit_kind: str
    save_policy: str
    trace_id: str
    operation_count: int
    operation_target_count: int
    last_operation_id: Optional[str]
    last_operation_ordinal: Optional[int]
    outbox_event_count: int
    last_outbox_event_id: Optional[str]
    last_outbox_ordinal: Optional[int]
    durable_action_count: int
    pipeline_intent_count: int
    before_root_sha256: bytes = field(repr=False)
    after_root_sha256: bytes = field(repr=False)
    previous_operation_tail_sha256: bytes = field(repr=False)
    operation_tail_sha256: bytes = field(repr=False)
    asset_projection_sha256: bytes = field(repr=False)
    asset_reference_count: int
    entity_count: int
    manifest_bytes: bytes = field(repr=False)
    manifest_sha256: bytes = field(repr=False)
    committed_at: str
    project_revision: int
    head_revision: int

    def sql_values(self) -> tuple:
        return _sql_values(self, BUSINESS_COMMITS_COLUMNS)


@dataclass(frozen=True)
class BusinessCutoverRepositoryProjectRow:
    repository_singleton: int
    project_id: str
    generation: int
    project_revision: int
    status: str
    current_snapshot_sha256: Optional[bytes] = field(repr=False)
    created_at: str
    updated_at: str
    deleted_at: Optional[str]

    def sql_values(self) -> tuple:
        return _sql_values(self, REPOSITORY_PROJECTS_COLUMNS)


@dataclass(frozen=True)
class BusinessCutoverDocumentHeadRow:
    project_id: str
    generation: int
    document_id: str
    document_version: int
    commit_id: str
    head_revision: int
    status: str
    current_snapshot_sha256: bytes = field(repr=False)
    current_project_snapshot_sha256: bytes = field(repr=False)
    business_root_sha256: bytes = field(repr=False)
    operation_tail_sha256: bytes = field(repr=False)
    asset_projection_sha256: bytes = field(repr=False)
    asset_reference_count: int
    entity_count: int
    updated_at: str

    def sql_values(self) -> tuple:
        return _sql_values(self, DOCUMENT_COMMIT_HEADS_COLUMNS)


@dataclass(frozen=True)
class BusinessCutoverAssetBlobRow:
    asset_sha256: bytes = field(repr=False)
    content_key: str
    media_type: Optional[str]
    extension: Optional[str]
    byte_length: int
    status: str
    state_revision: int
    created_at: str
    updated_at: str

    def sql_values(self) -> tuple:
        return _sql_values(self, ASSET_BLOBS_COLUMNS)


@dataclass(frozen=True)
class BusinessCutoverDocumentAssetRefRow:
    project_id: str
    generation: int
    document_id: str
    document_version: int
    commit_id: str
    commit_asset_reference_count: int
    ordinal: int
    asset_sha256: bytes = field(repr=False)
    entity_type: str
    entity_id: str
    entity_deleted: int
    value_path: str
    ref_mime: Optional[str]
    ref_size: Optional[int]
    ref_ext: Optional[str]

    def sql_values(self) -> tuple:
        return _sql_values(self, DOCUMENT_ASSET_REFS_COLUMNS)


@dataclass(frozen=True)
class BusinessCutoverGenesisHeadRow:
    singleton: int
    state_revision: int
    sequence: int
    previous_tail_sha256: bytes = field(repr=False)
    tail_sha256: bytes = field(repr=False)
    updated_at: str

    def sql_values(self) -> tuple:
        return _sql_values(self, WORKER_LEDGER_HEAD_COLUMNS)


@dataclass(frozen=True)
class BusinessCutoverDispatchHeadRow:
    singleton: int
    state_revision: int
    published_sequence: int
    published_repository_revision: Optional[int]
    published_ordinal: Optional[int]
    inflight_sequence: Optional[int]
    inflight_repository_revision: Optional[int]
    inflight_ordinal: Optional[int]
    previous_head_sha256: bytes = field(repr=False)
    head_sha256: bytes = field(repr=False)
    updated_at: str

    def sql_values(self) -> tuple:
        return _sql_values(self, OUTBOX_DISPATCH_HEAD_COLUMNS)


@dataclass(frozen=True)
class BusinessCutoverSchedulerGenesisProjection:
    worker_ledger: BusinessCutoverGenesisHeadRow
    outbox_allocation: BusinessCutoverGenesisHeadRow
    outbox_dispatch: BusinessCutoverDispatchHeadRow


@dataclass(frozen=True)
class BusinessCutoverBaselineAtomicSeed:
    commit_id: str
    transaction_id: str
    receipt_kind: str
    committed_at: str
    atomic_manifest_sha256: bytes = field(repr=False)
    pipeline_intents_sha256: bytes = field(repr=False)
    controls_sha256: bytes = field(repr=False)
    control_count: int
    before_worker_sequence: int
    before_worker_tail_sha256: bytes = field(repr=False)
    after_worker_sequence: int
    after_worker_tail_sha256: bytes = field(repr=False)
    manifest_bytes: bytes = field(repr=False)
    manifest_sha256: bytes = field(repr=False)
    receipt_sha256: bytes = field(repr=False)


@dataclass(frozen=True)
class BusinessCutoverLiveBaselineProjection:
    ordinal: int
    project: BusinessCutoverProjectRow
    document: BusinessCutoverDocumentRow
    entities: Tuple[BusinessCutoverEntityRow, ...]
    transaction_receipt: BusinessCutoverTransactionReceiptRow
    commit: BusinessCutoverCommitRow
    repository_project: BusinessCutoverRepositoryProjectRow
    document_head: BusinessCutoverDocumentHeadRow
    asset_references: Tuple[BusinessCutoverDocumentAssetRefRow, ...]
    atomic_seed: BusinessCutoverBaselineAtomicSeed


@dataclass(frozen=True)
class BusinessCutoverDeletedBaselineProjection:
    ordinal: int
    repository_project: BusinessCutoverRepositoryProjectRow


@dataclass(frozen=True)
class BusinessCutoverBaselineMemberProjection:
    ordinal: int
    project_id: str
    generation: int
    status: str
    live: Optional[BusinessCutoverLiveBaselineProjection] = None
    deleted: Optional[BusinessCutoverDeletedBaselineProjection] = None

    def __post_init__(self) -> None:
        valid = (
            self.status == BUSINESS_CUTOVER_MEMBER_LIVE
            and type(self.live) is BusinessCutoverLiveBaselineProjection
            and self.deleted is None
        ) or (
            self.status == BUSINESS_CUTOVER_MEMBER_DELETED
            and self.live is None
            and type(self.deleted) is BusinessCutoverDeletedBaselineProjection
        )
        if not valid:
            _fail("business_cutover_baseline_member_projection_invalid")


@dataclass(frozen=True)
class BusinessCutoverBaselineProjection:
    cutover_id: str
    committed_at: str
    runtime_repository_revision: int
    members: Tuple[BusinessCutoverBaselineMemberProjection, ...]
    live: Tuple[BusinessCutoverLiveBaselineProjection, ...]
    deleted: Tuple[BusinessCutoverDeletedBaselineProjection, ...]
    asset_blobs: Tuple[BusinessCutoverAssetBlobRow, ...]
    scheduler_genesis: BusinessCutoverSchedulerGenesisProjection
    total_snapshot_bytes: int
    entity_row_count: int
    asset_reference_row_count: int
    unique_asset_blob_count: int
    manifest_bytes: bytes = field(repr=False)
    manifest_sha256: bytes = field(repr=False)
    protocol: str = BUSINESS_CUTOVER_BASELINE_PROJECTION_PROTOCOL
    status: str = BUSINESS_CUTOVER_BASELINE_PROJECTION_STATUS
    outer_atomic_status: str = BUSINESS_CUTOVER_BASELINE_OUTER_ATOMIC_STATUS
    _plan: BusinessCutoverPlan = field(default=None, repr=False, compare=False)
    _issuer: Any = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._issuer is not _PROJECTION_ISSUER:
            _fail("business_cutover_baseline_projection_issuer_invalid")


def _trace_id(cutover_id: str, ordinal: int) -> str:
    return BUSINESS_CUTOVER_BASELINE_TRACE_PREFIX + cutover_id + ":" + str(ordinal)


def _atomic_seed(
    commit_id: str,
    committed_at: str,
    atomic_manifest_sha256: bytes,
) -> BusinessCutoverBaselineAtomicSeed:
    transaction_id = BUSINESS_TRANSACTION_PREFIX + commit_id
    parent = AtomicBusinessWriteGenericReceiptEvidence(
        transaction_id,
        BUSINESS_TRANSACTION_KIND,
        committed_at,
    )
    pipeline_intents_sha256 = _component_digest(
        b"ze.atomic-business-commit.pipelines.v1",
        (),
    )
    controls_sha256 = _atomic_digest(
        ATOMIC_BUSINESS_WRITE_CONTROLS_HASH_DOMAIN,
        (),
    )
    worker = EMPTY_WORKER_LEDGER_HEAD
    manifest_bytes = encode_payload_bounded(
        {
            "schema": ATOMIC_BUSINESS_WRITE_MANIFEST_SCHEMA,
            "protocol": ATOMIC_BUSINESS_WRITE_PROTOCOL,
            "status": ATOMIC_BUSINESS_WRITE_PLAN_STATUS,
            "commit": {
                "commit_id": commit_id,
                "committed_at": committed_at,
                "atomic_manifest_sha256": atomic_manifest_sha256.hex(),
                "pipeline_intents_sha256": pipeline_intents_sha256.hex(),
            },
            "worker_ledger": {
                "before": {
                    "sequence": worker.sequence,
                    "tail_sha256": worker.tail_sha256.hex(),
                },
                "after": {
                    "sequence": worker.sequence,
                    "tail_sha256": worker.tail_sha256.hex(),
                },
            },
            "controls": {
                "count": 0,
                "sha256": controls_sha256.hex(),
                "members": [],
            },
        },
        MAX_ATOMIC_BUSINESS_WRITE_MANIFEST_BYTES,
        MAX_ATOMIC_BUSINESS_WRITE_MANIFEST_NODES,
    )
    manifest_sha256 = _atomic_manifest_digest(manifest_bytes)
    receipt_sha256 = _atomic_receipt_digest(
        commit_id=commit_id,
        receipt_parent=parent,
        atomic_manifest_sha256=atomic_manifest_sha256,
        pipeline_intents_sha256=pipeline_intents_sha256,
        controls_sha256=controls_sha256,
        control_count=0,
        before_worker_ledger=worker,
        after_worker_ledger=worker,
        manifest_sha256=manifest_sha256,
    )
    return BusinessCutoverBaselineAtomicSeed(
        commit_id=commit_id,
        transaction_id=transaction_id,
        receipt_kind=BUSINESS_TRANSACTION_KIND,
        committed_at=committed_at,
        atomic_manifest_sha256=atomic_manifest_sha256,
        pipeline_intents_sha256=pipeline_intents_sha256,
        controls_sha256=controls_sha256,
        control_count=0,
        before_worker_sequence=0,
        before_worker_tail_sha256=BUSINESS_CUTOVER_BASELINE_EMPTY_DIGEST,
        after_worker_sequence=0,
        after_worker_tail_sha256=BUSINESS_CUTOVER_BASELINE_EMPTY_DIGEST,
        manifest_bytes=manifest_bytes,
        manifest_sha256=manifest_sha256,
        receipt_sha256=receipt_sha256,
    )


def _snapshot_rows(
    snapshot: BusinessSnapshotSet,
) -> tuple[
    BusinessCutoverProjectRow,
    BusinessCutoverDocumentRow,
    Tuple[BusinessCutoverEntityRow, ...],
]:
    project = snapshot.project
    document = snapshot.document
    return (
        BusinessCutoverProjectRow(
            1,
            project.project_id,
            project.generation,
            project.codec_id,
            project.codec_version,
            project.snapshot_format_version,
            project.snapshot_bytes,
            project.snapshot_sha256,
        ),
        BusinessCutoverDocumentRow(
            document.project_id,
            document.document_id,
            document.document_version,
            document.schema_version,
            document.collaboration_mode,
            document.save_policy,
            document.codec_id,
            document.codec_version,
            document.snapshot_format_version,
            document.snapshot_bytes,
            document.snapshot_sha256,
        ),
        tuple(
            BusinessCutoverEntityRow(
                entity.project_id,
                entity.document_id,
                entity.entity_type,
                entity.entity_id,
                entity.entity_version,
                entity.deleted,
                entity.codec_id,
                entity.codec_version,
                entity.snapshot_format_version,
                entity.snapshot_bytes,
                entity.snapshot_sha256,
            )
            for entity in snapshot.entities
        ),
    )


def _member_manifest(value: BusinessCutoverBaselineMemberProjection) -> dict:
    common = {
        "ordinal": value.ordinal,
        "project_id": value.project_id,
        "generation": value.generation,
        "status": value.status,
    }
    if value.deleted is not None:
        common["deleted"] = {
            "project_revision": value.deleted.repository_project.project_revision,
            "deleted_at": value.deleted.repository_project.deleted_at,
        }
        return common
    assert value.live is not None
    live = value.live
    common["live"] = {
        "commit_id": live.commit.commit_id,
        "repository_revision": live.commit.repository_revision,
        "project_revision": live.commit.project_revision,
        "head_revision": live.commit.head_revision,
        "trace_id": live.commit.trace_id,
        "before_root_sha256": live.commit.before_root_sha256.hex(),
        "operation_tail_sha256": live.commit.operation_tail_sha256.hex(),
        "project": {
            "snapshot_bytes": len(live.project.snapshot_bytes),
            "snapshot_sha256": live.project.snapshot_sha256.hex(),
        },
        "document": {
            "document_id": live.document.document_id,
            "document_version": live.document.document_version,
            "snapshot_bytes": len(live.document.snapshot_bytes),
            "snapshot_sha256": live.document.snapshot_sha256.hex(),
        },
        "entities": [
            {
                "entity_type": entity.entity_type,
                "entity_id": entity.entity_id,
                "entity_version": entity.entity_version,
                "deleted": entity.deleted,
                "snapshot_bytes": len(entity.snapshot_bytes),
                "snapshot_sha256": entity.snapshot_sha256.hex(),
            }
            for entity in live.entities
        ],
        "asset_projection_sha256": live.commit.asset_projection_sha256.hex(),
        "asset_reference_count": live.commit.asset_reference_count,
        "asset_references": [
            {
                "ordinal": reference.ordinal,
                "asset_sha256": reference.asset_sha256.hex(),
                "entity_type": reference.entity_type,
                "entity_id": reference.entity_id,
                "entity_deleted": reference.entity_deleted,
                "value_path": reference.value_path,
                "ref_mime": reference.ref_mime,
                "ref_size": reference.ref_size,
                "ref_ext": reference.ref_ext,
            }
            for reference in live.asset_references
        ],
        "entity_count": live.commit.entity_count,
        "baseline_manifest_sha256": live.commit.manifest_sha256.hex(),
        "atomic_manifest_sha256": live.atomic_seed.manifest_sha256.hex(),
        "atomic_receipt_sha256": live.atomic_seed.receipt_sha256.hex(),
    }
    return common


def _prepare_projection(
    plan: BusinessCutoverPlan,
    committed_at: str,
) -> BusinessCutoverBaselineProjection:
    try:
        plan = _require_business_cutover_plan(plan)
    except BusinessCutoverIntegrityError as exc:
        raise BusinessCutoverBaselineProjectionError(
            "business_cutover_baseline_plan_invalid"
        ) from exc
    committed_at = _timestamp(committed_at)

    members = []
    live_rows = []
    deleted_rows = []
    asset_metadata_by_sha = {}
    live_index = 0
    total_snapshot_bytes = 0
    entity_row_count = 0
    asset_reference_row_count = 0
    for member in plan.members:
        if member.status == BUSINESS_CUTOVER_MEMBER_DELETED:
            repository_project = BusinessCutoverRepositoryProjectRow(
                1,
                member.project_id,
                member.generation,
                1,
                BUSINESS_CUTOVER_MEMBER_DELETED,
                None,
                committed_at,
                committed_at,
                committed_at,
            )
            deleted = BusinessCutoverDeletedBaselineProjection(
                member.ordinal,
                repository_project,
            )
            deleted_rows.append(deleted)
            members.append(BusinessCutoverBaselineMemberProjection(
                member.ordinal,
                member.project_id,
                member.generation,
                member.status,
                deleted=deleted,
            ))
            continue

        try:
            snapshot = verify_business_snapshot(plan._business_snapshots[live_index])
        except (BusinessSnapshotError, IndexError) as exc:
            raise BusinessCutoverBaselineProjectionError(
                "business_cutover_baseline_snapshot_invalid"
            ) from exc
        live_index += 1
        project, document, entities = _snapshot_rows(snapshot)
        try:
            asset_projection = verify_asset_reference_projection(
                project_asset_references(snapshot)
            )
        except AssetReferenceProjectionError as exc:
            raise BusinessCutoverBaselineProjectionError(
                "business_cutover_baseline_asset_projection_invalid"
            ) from exc
        snapshot_bytes = (
            len(project.snapshot_bytes)
            + len(document.snapshot_bytes)
            + sum(len(entity.snapshot_bytes) for entity in entities)
        )
        total_snapshot_bytes += snapshot_bytes
        entity_row_count += len(entities)
        if (
            total_snapshot_bytes
            > MAX_BUSINESS_CUTOVER_BASELINE_TOTAL_SNAPSHOT_BYTES
        ):
            _fail("business_cutover_baseline_snapshot_budget_exceeded")
        if entity_row_count > MAX_BUSINESS_CUTOVER_BASELINE_ENTITY_ROWS:
            _fail("business_cutover_baseline_entity_budget_exceeded")
        assert member.baseline_commit_id is not None
        assert member.baseline_repository_revision is not None
        assert member.document_id is not None
        assert member.document_version is not None
        assert member.business_root_sha256 is not None
        assert member.document_snapshot_sha256 is not None
        assert member.operation_tail_sha256 is not None
        assert member.asset_projection_sha256 is not None
        assert member.asset_reference_count is not None
        assert member.entity_count is not None
        assert member.save_policy is not None
        assert member.baseline_manifest_bytes is not None
        assert member.baseline_manifest_sha256 is not None
        if len(member.baseline_manifest_bytes) > (
            MAX_BUSINESS_CUTOVER_BASELINE_SQL_PAYLOAD_BYTES
        ):
            _fail("business_cutover_baseline_sql_payload_budget_exceeded")
        if not hmac.compare_digest(
            member.operation_tail_sha256,
            BUSINESS_CUTOVER_BASELINE_EMPTY_DIGEST,
        ):
            _fail("business_cutover_baseline_operation_tail_invalid")
        if not (
            project.project_id == member.project_id
            and project.generation == member.generation
            and document.project_id == member.project_id
            and document.document_id == member.document_id
            and document.document_version == member.document_version
            and hmac.compare_digest(
                project.snapshot_sha256,
                member.business_root_sha256,
            )
            and hmac.compare_digest(
                document.snapshot_sha256,
                member.document_snapshot_sha256,
            )
            and len(entities) == member.entity_count
            and asset_projection.reference_count
            == member.asset_reference_count
            and asset_projection.unique_reference_count
            == member.unique_asset_reference_count
            and hmac.compare_digest(
                asset_projection.projection_sha256,
                member.asset_projection_sha256,
            )
        ):
            _fail("business_cutover_baseline_snapshot_mapping_changed")

        asset_references = []
        for ordinal, occurrence in enumerate(asset_projection.occurrences):
            reference = occurrence.reference
            asset_sha256 = bytes.fromhex(reference.sha256)
            asset_metadata_by_sha[reference.sha256] = _merge_asset_metadata(
                asset_metadata_by_sha.get(reference.sha256),
                (reference.mime, reference.size, reference.ext),
            )
            asset_references.append(BusinessCutoverDocumentAssetRefRow(
                project_id=member.project_id,
                generation=member.generation,
                document_id=member.document_id,
                document_version=member.document_version,
                commit_id=member.baseline_commit_id,
                commit_asset_reference_count=asset_projection.reference_count,
                ordinal=ordinal,
                asset_sha256=asset_sha256,
                entity_type=occurrence.entity_type,
                entity_id=occurrence.entity_id,
                entity_deleted=int(occurrence.entity_deleted),
                value_path=occurrence.value_path,
                ref_mime=reference.mime,
                ref_size=reference.size,
                ref_ext=reference.ext,
            ))
        asset_reference_row_count += len(asset_references)

        transaction_id = BUSINESS_TRANSACTION_PREFIX + member.baseline_commit_id
        receipt = BusinessCutoverTransactionReceiptRow(
            transaction_id,
            BUSINESS_TRANSACTION_KIND,
            committed_at,
        )
        trace_id = _trace_id(plan.cutover_id, member.ordinal)
        commit = BusinessCutoverCommitRow(
            member.baseline_commit_id,
            transaction_id,
            BUSINESS_TRANSACTION_KIND,
            member.project_id,
            member.generation,
            member.baseline_repository_revision,
            plan.install.runtime.repository_epoch,
            plan.cutover_id,
            plan.install.runtime.source_admission_revision,
            member.document_id,
            member.document_version,
            None,
            "baseline",
            member.save_policy,
            trace_id,
            0,
            0,
            None,
            None,
            0,
            None,
            None,
            0,
            0,
            BUSINESS_CUTOVER_BASELINE_EMPTY_DIGEST,
            member.business_root_sha256,
            BUSINESS_CUTOVER_BASELINE_EMPTY_DIGEST,
            member.operation_tail_sha256,
            member.asset_projection_sha256,
            member.asset_reference_count,
            member.entity_count,
            member.baseline_manifest_bytes,
            member.baseline_manifest_sha256,
            committed_at,
            1,
            1,
        )
        repository_project = BusinessCutoverRepositoryProjectRow(
            1,
            member.project_id,
            member.generation,
            1,
            BUSINESS_CUTOVER_MEMBER_LIVE,
            member.business_root_sha256,
            committed_at,
            committed_at,
            None,
        )
        document_head = BusinessCutoverDocumentHeadRow(
            member.project_id,
            member.generation,
            member.document_id,
            member.document_version,
            member.baseline_commit_id,
            1,
            BUSINESS_CUTOVER_MEMBER_LIVE,
            member.document_snapshot_sha256,
            member.business_root_sha256,
            member.business_root_sha256,
            member.operation_tail_sha256,
            member.asset_projection_sha256,
            member.asset_reference_count,
            member.entity_count,
            committed_at,
        )
        atomic_seed = _atomic_seed(
            member.baseline_commit_id,
            committed_at,
            member.baseline_manifest_sha256,
        )
        live = BusinessCutoverLiveBaselineProjection(
            member.ordinal,
            project,
            document,
            entities,
            receipt,
            commit,
            repository_project,
            document_head,
            tuple(asset_references),
            atomic_seed,
        )
        live_rows.append(live)
        members.append(BusinessCutoverBaselineMemberProjection(
            member.ordinal,
            member.project_id,
            member.generation,
            member.status,
            live=live,
        ))

    if live_index != len(plan._business_snapshots):
        _fail("business_cutover_baseline_snapshot_count_changed")
    members_tuple = tuple(members)
    if (
        tuple(value.ordinal for value in members_tuple)
        != tuple(range(len(members_tuple)))
        or tuple(value.project_id for value in members_tuple)
        != tuple(sorted(value.project_id for value in members_tuple))
        or tuple(value.commit.repository_revision for value in live_rows)
        != tuple(range(1, len(live_rows) + 1))
        or asset_reference_row_count
        != sum(value.commit.asset_reference_count for value in live_rows)
    ):
        _fail("business_cutover_baseline_projection_order_invalid")

    zero_head = BusinessCutoverGenesisHeadRow(
        1,
        1,
        0,
        BUSINESS_CUTOVER_BASELINE_EMPTY_DIGEST,
        BUSINESS_CUTOVER_BASELINE_EMPTY_DIGEST,
        committed_at,
    )
    scheduler = BusinessCutoverSchedulerGenesisProjection(
        worker_ledger=zero_head,
        outbox_allocation=BusinessCutoverGenesisHeadRow(*zero_head.sql_values()),
        outbox_dispatch=BusinessCutoverDispatchHeadRow(
            1,
            1,
            0,
            None,
            None,
            None,
            None,
            None,
            BUSINESS_CUTOVER_BASELINE_EMPTY_DIGEST,
            BUSINESS_CUTOVER_BASELINE_EMPTY_DIGEST,
            committed_at,
        ),
    )
    runtime_revision = max(1, len(live_rows))
    asset_blobs_list = []
    for asset_hex in sorted(asset_metadata_by_sha):
        media_type, byte_length, extension = asset_metadata_by_sha[asset_hex]
        # V11 ``asset_blobs.byte_length`` is NOT NULL.  Optional reference
        # metadata may merge with a known occurrence, but if the repository
        # still has no length the future coordinator needs an exact coverage
        # carrier; this projection must not fabricate zero.
        if byte_length is None:
            _fail("business_cutover_baseline_asset_size_missing")
        asset_blobs_list.append(BusinessCutoverAssetBlobRow(
            asset_sha256=bytes.fromhex(asset_hex),
            content_key="sha256/" + asset_hex[:2] + "/" + asset_hex,
            media_type=media_type,
            extension=extension,
            byte_length=byte_length,
            status="available",
            state_revision=1,
            created_at=committed_at,
            updated_at=committed_at,
        ))
    asset_blobs = tuple(asset_blobs_list)
    manifest_bytes = _encode(
        {
            "schema": BUSINESS_CUTOVER_BASELINE_PROJECTION_SCHEMA,
            "protocol": BUSINESS_CUTOVER_BASELINE_PROJECTION_PROTOCOL,
            "status": BUSINESS_CUTOVER_BASELINE_PROJECTION_STATUS,
            "cutover_id": plan.cutover_id,
            "committed_at": committed_at,
            "runtime_repository_revision": runtime_revision,
            "project_count": len(members_tuple),
            "live_project_count": len(live_rows),
            "deleted_project_count": len(deleted_rows),
            "total_snapshot_bytes": total_snapshot_bytes,
            "entity_row_count": entity_row_count,
            "asset_reference_row_count": asset_reference_row_count,
            "unique_asset_blob_count": len(asset_blobs),
            "outer_atomic_status": (
                BUSINESS_CUTOVER_BASELINE_OUTER_ATOMIC_STATUS
            ),
            "asset_blobs": [
                {
                    "asset_sha256": blob.asset_sha256.hex(),
                    "content_key": blob.content_key,
                    "media_type": blob.media_type,
                    "extension": blob.extension,
                    "byte_length": blob.byte_length,
                    "status": blob.status,
                    "state_revision": blob.state_revision,
                    "created_at": blob.created_at,
                    "updated_at": blob.updated_at,
                }
                for blob in asset_blobs
            ],
            "scheduler_genesis": {
                "state_revision": 1,
                "worker_sequence": 0,
                "outbox_allocation_sequence": 0,
                "outbox_dispatch_revision": 1,
                "empty_sha256": BUSINESS_CUTOVER_BASELINE_EMPTY_DIGEST.hex(),
            },
            "members": [_member_manifest(value) for value in members_tuple],
        },
        "business_cutover_baseline_projection_manifest_invalid",
    )
    return BusinessCutoverBaselineProjection(
        cutover_id=plan.cutover_id,
        committed_at=committed_at,
        runtime_repository_revision=runtime_revision,
        members=members_tuple,
        live=tuple(live_rows),
        deleted=tuple(deleted_rows),
        asset_blobs=asset_blobs,
        scheduler_genesis=scheduler,
        total_snapshot_bytes=total_snapshot_bytes,
        entity_row_count=entity_row_count,
        asset_reference_row_count=asset_reference_row_count,
        unique_asset_blob_count=len(asset_blobs),
        manifest_bytes=manifest_bytes,
        manifest_sha256=_projection_digest(manifest_bytes),
        _plan=plan,
        _issuer=_PROJECTION_ISSUER,
    )


def _project_business_cutover_baseline(
    plan: BusinessCutoverPlan,
    *,
    committed_at: str,
) -> BusinessCutoverBaselineProjection:
    """Return the only canonical V11 baseline row projection for ``plan``."""

    return _prepare_projection(plan, committed_at)


def _require_business_cutover_baseline_projection(
    value: Any,
) -> BusinessCutoverBaselineProjection:
    if (
        type(value) is not BusinessCutoverBaselineProjection
        or getattr(value, "_issuer", None) is not _PROJECTION_ISSUER
        or value.protocol != BUSINESS_CUTOVER_BASELINE_PROJECTION_PROTOCOL
        or value.status != BUSINESS_CUTOVER_BASELINE_PROJECTION_STATUS
        or value.outer_atomic_status
        != BUSINESS_CUTOVER_BASELINE_OUTER_ATOMIC_STATUS
    ):
        _fail("business_cutover_baseline_projection_invalid")
    rebuilt = _prepare_projection(value._plan, value.committed_at)
    if rebuilt != value or not hmac.compare_digest(
        rebuilt.manifest_sha256,
        value.manifest_sha256,
    ):
        _fail("business_cutover_baseline_projection_changed")
    return value


def _require_business_cutover_baseline_outer_atomic_supported(
    projection: BusinessCutoverBaselineProjection,
    *,
    mounted_at: str,
) -> None:
    """Fail explicitly for the unrepresentable V11 pre-mount outer receipt.

    The chronology check is deliberately separate from the capability error:
    reverse chronology is corrupt input; valid ``install <= mount`` chronology
    reaches the stable schema-upgrade requirement.
    """

    projection = _require_business_cutover_baseline_projection(projection)
    mounted_at = _timestamp(mounted_at)
    if mounted_at < projection.committed_at:
        _fail("business_cutover_baseline_mount_chronology_invalid")
    _fail("business_cutover_baseline_outer_atomic_requires_schema_upgrade")


__all__ = ()
