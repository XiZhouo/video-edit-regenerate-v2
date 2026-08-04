"""Private, rooted reader for one schema-v6 SQLite business head.

The reader is deliberately not an SDK repository implementation.  It opens a
fresh kernel-owned connection, pins one SQLite read snapshot, proves the
runtime authority and the complete current-head closure, and returns detached
immutable carriers only.  No connection, cursor, path, SQL row, callback, or
mutable mapping crosses the boundary.

The generic :mod:`sqlite_kernel` validator intentionally rejects mounted v6
business state.  This leaf therefore uses the private mounted-runtime
validator and does not weaken the unmounted kernel profile.
"""

from __future__ import annotations

import hashlib
import hmac
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from typing import Any, Dict, List, Optional, Sequence, Tuple

from asset_reference_projection import (
    ASSET_REFERENCE_PROJECTION_PROTOCOL,
    AssetReferenceProjectionError,
    project_asset_references,
)
from business_codec import (
    BusinessCodecError,
    CODEC_ID,
    CODEC_VERSION,
    decode_payload_bounded,
    encode_payload_bounded,
)
from business_baseline_contract import (
    BUSINESS_BASELINE_MANIFEST_PROTOCOL,
    BUSINESS_BASELINE_MANIFEST_SCHEMA,
    digest_business_baseline_manifest,
)
from business_snapshot import (
    BusinessSnapshotError,
    BusinessSnapshotSet,
    DocumentSnapshotRow,
    EntitySnapshotRow,
    ProjectSnapshotRow,
    verify_business_snapshot,
)
from business_transaction_contract import (
    ACTION_STATUS_QUEUED,
    ATOMIC_COMMIT_MANIFEST_SCHEMA,
    ATOMIC_COMMIT_PROTOCOL,
    ATOMIC_COMMIT_STATUS_PREPARED,
    CONFIRMATION_EXPECTED_STATUS_RESERVED,
    CONFIRMATION_TARGET_STATUS_CONSUMED,
    INBOX_EXPECTED_STATUS_CLAIMED,
    INBOX_TARGET_STATUS_COMMITTED,
    MAX_ATOMIC_JSON_PAYLOAD_BYTES,
    MAX_ATOMIC_JSON_PAYLOAD_NODES,
    FrozenJsonPayload,
)
from business_transaction_integrity import (
    ATOMIC_COMMIT_MANIFEST_HASH_DOMAIN,
    MAX_ATOMIC_MANIFEST_BYTES,
    MAX_ATOMIC_MANIFEST_NODES,
)
from operation_record_codec import (
    FrozenOperationBody,
    FrozenOperationRecord,
    OPERATION_RECORD_PROTOCOL,
    OperationRecordCodecError,
    ZERO_OPERATION_HASH,
    build_frozen_operation_chain,
    freeze_operation_record,
    materialize_operation_body,
    project_operation_targets,
)
from sqlite_business_runtime import (
    BusinessAuthorityState,
    BusinessRuntimeAccess,
    BusinessRuntimeProfile,
    _validate_business_runtime_connection,
)
from sqlite_kernel import SqliteDatabase
from sqlite_schema import BUSINESS_TRANSACTION_KIND, BUSINESS_TRANSACTION_PREFIX


_LOWER_HEX = frozenset("0123456789abcdef")
_MAX_SQLITE_INTEGER = 2**63 - 1
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

# Exact minimal bootstrap descriptor.  This is not the future V5c cutover
# issuer/receipt contract; it only makes a pre-existing baseline manifest
# unambiguous and fail-closed while the relational/snapshot closure remains
# the authority.


class SQLiteBusinessReadError(RuntimeError):
    """Stable, payload-free failure at the rooted read boundary."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        if type(code) is not str or not code:
            raise TypeError("code must be a non-empty exact string")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class SQLiteBusinessHead:
    """Detached proof result for one current head.

    ``snapshot`` and ``runtime`` are frozen value carriers.  Deliberately
    absent are database identity, filesystem paths, SQL rows and live handles.
    """

    project_id: str
    generation: int
    document_id: str
    document_version: int
    commit_id: str
    project_revision: int
    head_revision: int
    repository_revision: int
    operation_count: int
    outbox_event_count: int
    durable_action_count: int
    pipeline_intent_count: int
    asset_reference_count: int
    runtime: BusinessRuntimeProfile
    snapshot: BusinessSnapshotSet = field(repr=False)


@dataclass(frozen=True)
class _CommitEvidence:
    operations: Tuple[Dict[str, Any], ...]
    targets: Tuple[Dict[str, Any], ...]
    outbox: Tuple[Dict[str, Any], ...]
    actions: Tuple[Dict[str, Any], ...]
    pipelines: Tuple[Dict[str, Any], ...]
    inbox: Optional[Dict[str, Any]]
    confirmation: Optional[Dict[str, Any]]
    asset_projection_sha256: bytes = field(repr=False)
    asset_reference_count: int
    unique_asset_reference_count: int
    auxiliary_payload_bytes: int


def _fail(code: str) -> None:
    raise SQLiteBusinessReadError(code)


def _exact_text(value: Any, *, nonempty: bool = True) -> str:
    if type(value) is not str or (nonempty and not value):
        _fail("sqlite_business_read_corrupt")
    invalid = False
    try:
        value.encode("utf-8", "strict")
    except UnicodeEncodeError:
        invalid = True
    if invalid:
        _fail("sqlite_business_read_corrupt")
    return value


def _input_text(value: Any) -> str:
    if type(value) is not str or not value or len(value) > 160:
        _fail("sqlite_business_read_input_invalid")
    invalid = False
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeEncodeError:
        invalid = True
        encoded = b""
    if invalid or len(encoded) > 640:
        _fail("sqlite_business_read_input_invalid")
    return value


def _positive_integer(value: Any) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_SQLITE_INTEGER:
        _fail("sqlite_business_read_input_invalid")
    return value


def _optional_positive_integer(value: Any) -> Optional[int]:
    if value is None:
        return None
    return _positive_integer(value)


def _hex_identity(value: Any, length: int) -> str:
    if (
        type(value) is not str
        or len(value) != length
        or any(character not in _LOWER_HEX for character in value)
    ):
        _fail("sqlite_business_read_input_invalid")
    return value


def _digest(value: Any) -> bytes:
    if type(value) is not bytes or len(value) != 32:
        _fail("sqlite_business_read_corrupt")
    return value


def _one(connection: Any, sql: str, parameters: Sequence[Any]) -> Any:
    rows = connection.execute(sql, parameters).fetchall()
    if len(rows) != 1:
        _fail("sqlite_business_read_corrupt")
    return rows[0]


def _payload_manifest(
    raw: Any,
    digest: Any,
    schema: Any,
    codec_id: Any,
    codec_version: Any,
) -> Tuple[Dict[str, Any], int]:
    rejected = False
    payload = None
    try:
        payload = FrozenJsonPayload(
            schema=schema,
            codec_id=codec_id,
            codec_version=codec_version,
            payload_bytes=raw,
            payload_sha256=digest,
        )
    except Exception as exc:
        if not isinstance(exc, (BusinessCodecError, TypeError, ValueError)):
            raise
        rejected = True
    if rejected or payload is None:
        _fail("sqlite_business_read_corrupt")
    return (
        {
            "schema": payload.schema,
            "codec_id": payload.codec_id,
            "codec_version": payload.codec_version,
            "bytes": len(payload.payload_bytes),
            "sha256": payload.payload_sha256.hex(),
        },
        len(payload.payload_bytes),
    )


def _frame(hasher: Any, value: bytes) -> None:
    hasher.update(len(value).to_bytes(8, "big"))
    hasher.update(value)


def _component_digest(domain: bytes, values: Tuple[Dict[str, Any], ...]) -> bytes:
    hasher = hashlib.sha256()
    _frame(hasher, domain)
    for value in values:
        rejected = False
        encoded = b""
        try:
            encoded = encode_payload_bounded(
                value,
                MAX_ATOMIC_JSON_PAYLOAD_BYTES,
                MAX_ATOMIC_JSON_PAYLOAD_NODES,
            )
        except BusinessCodecError:
            rejected = True
        if rejected:
            _fail("sqlite_business_read_corrupt")
        _frame(hasher, encoded)
    return hasher.digest()


def _manifest_digest(payload: bytes) -> bytes:
    hasher = hashlib.sha256()
    _frame(hasher, ATOMIC_COMMIT_MANIFEST_HASH_DOMAIN)
    _frame(hasher, payload)
    return hasher.digest()


def _canonical_operation_time(value: Any) -> str:
    """Mirror the writer's platform-independent floor-to-millisecond rule."""

    if type(value) not in (int, float) or type(value) is bool:
        _fail("sqlite_business_read_corrupt")
    rejected = False
    milliseconds = 0
    instant = _EPOCH
    try:
        milliseconds = int(
            (Decimal(str(value)) * Decimal(1000)).to_integral_value(
                rounding=ROUND_FLOOR
            )
        )
        instant = _EPOCH + timedelta(milliseconds=milliseconds)
    except (InvalidOperation, OverflowError, ValueError):
        rejected = True
    if rejected:
        _fail("sqlite_business_read_corrupt")
    return instant.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (
        milliseconds % 1000
    )


class _ReadOnlyAuthorizer:
    __slots__ = ()

    def __call__(
        self,
        action: int,
        arg1: Optional[str],
        arg2: Optional[str],
        database_name: Optional[str],
        trigger_name: Optional[str],
    ) -> int:
        del arg1, arg2, database_name, trigger_name
        denied = {
            getattr(sqlite3, name)
            for name in (
                "SQLITE_INSERT",
                "SQLITE_UPDATE",
                "SQLITE_DELETE",
                "SQLITE_CREATE_INDEX",
                "SQLITE_CREATE_TABLE",
                "SQLITE_CREATE_TRIGGER",
                "SQLITE_CREATE_VIEW",
                "SQLITE_DROP_INDEX",
                "SQLITE_DROP_TABLE",
                "SQLITE_DROP_TRIGGER",
                "SQLITE_DROP_VIEW",
                "SQLITE_ALTER_TABLE",
                "SQLITE_ATTACH",
                "SQLITE_DETACH",
            )
            if hasattr(sqlite3, name)
        }
        return sqlite3.SQLITE_DENY if action in denied else sqlite3.SQLITE_OK


def _allow_authorizer(*args: Any) -> int:
    del args
    return sqlite3.SQLITE_OK


def _finish_read_connection(connection: Any, *, reset_authorizer: bool) -> bool:
    """Best-effort rollback/close without masking process-control exceptions."""

    if connection is None:
        return True
    complete = True
    if reset_authorizer:
        try:
            # Python 3.9's sqlite3 binding does not accept ``None`` here.
            connection.set_authorizer(_allow_authorizer)
        except Exception:
            complete = False
    try:
        if connection.in_transaction:
            connection.rollback()
    except Exception:
        complete = False
    try:
        connection.close()
    except Exception:
        complete = False
    return complete


def _publish_fence_is_compatible(
    pinned: BusinessRuntimeProfile,
    confirmed: BusinessRuntimeProfile,
) -> bool:
    """Allow unrelated global progress, reject control/authority transitions."""

    return (
        confirmed.authority == pinned.authority
        and confirmed.authority_state is pinned.authority_state
        and confirmed.repository_epoch == pinned.repository_epoch
        and confirmed.cutover_id == pinned.cutover_id
        and confirmed.source_admission_revision == pinned.source_admission_revision
        and confirmed.state_revision == pinned.state_revision
        and confirmed.runtime_generation == pinned.runtime_generation
        and hmac.compare_digest(confirmed.code_digest, pinned.code_digest)
        and confirmed.schema_version == pinned.schema_version
        and confirmed.repository_revision >= pinned.repository_revision
    )


def _read_snapshot(connection: Any, project: Any, head: Any, commit: Any) -> BusinessSnapshotSet:
    project_row = _one(
        connection,
        "SELECT project_id,generation,codec_id,codec_version,"
        "snapshot_format_version,snapshot_bytes,snapshot_sha256 "
        "FROM projects WHERE project_id=?",
        (project["project_id"],),
    )
    document_rows = connection.execute(
        "SELECT project_id,document_id,document_version,schema_version,"
        "collaboration_mode,save_policy,codec_id,codec_version,"
        "snapshot_format_version,snapshot_bytes,snapshot_sha256 "
        "FROM documents WHERE project_id=? ORDER BY document_id COLLATE BINARY",
        (project["project_id"],),
    ).fetchall()
    if len(document_rows) != 1:
        _fail("sqlite_business_read_corrupt")
    document_row = document_rows[0]

    if (
        project_row["generation"] != project["generation"]
        or project_row["snapshot_sha256"] != project["current_snapshot_sha256"]
        or project_row["snapshot_sha256"] != head["business_root_sha256"]
        or project_row["snapshot_sha256"] != commit["after_root_sha256"]
        or document_row["document_id"] != head["document_id"]
        or document_row["document_id"] != commit["document_id"]
        or document_row["document_version"] != head["document_version"]
        or document_row["document_version"] != commit["document_version"]
        or document_row["snapshot_sha256"] != head["current_snapshot_sha256"]
    ):
        _fail("sqlite_business_read_corrupt")

    count_row = _one(
        connection,
        "SELECT count(*) AS count FROM entities WHERE project_id=?",
        (project["project_id"],),
    )
    if type(count_row["count"]) is not int or count_row["count"] != head["entity_count"]:
        _fail("sqlite_business_read_corrupt")
    entity_rows = connection.execute(
        "SELECT project_id,document_id,entity_type,entity_id,entity_version,"
        "deleted,codec_id,codec_version,snapshot_format_version,snapshot_bytes,"
        "snapshot_sha256 FROM entities WHERE project_id=? "
        "ORDER BY entity_type COLLATE BINARY,entity_id COLLATE BINARY",
        (project["project_id"],),
    ).fetchall()
    snapshot = BusinessSnapshotSet(
        project=ProjectSnapshotRow(
            project_id=project_row["project_id"],
            generation=project_row["generation"],
            codec_id=project_row["codec_id"],
            codec_version=project_row["codec_version"],
            snapshot_format_version=project_row["snapshot_format_version"],
            snapshot_bytes=project_row["snapshot_bytes"],
            snapshot_sha256=project_row["snapshot_sha256"],
        ),
        document=DocumentSnapshotRow(
            project_id=document_row["project_id"],
            document_id=document_row["document_id"],
            document_version=document_row["document_version"],
            schema_version=document_row["schema_version"],
            collaboration_mode=document_row["collaboration_mode"],
            save_policy=document_row["save_policy"],
            codec_id=document_row["codec_id"],
            codec_version=document_row["codec_version"],
            snapshot_format_version=document_row["snapshot_format_version"],
            snapshot_bytes=document_row["snapshot_bytes"],
            snapshot_sha256=document_row["snapshot_sha256"],
        ),
        entities=tuple(
            EntitySnapshotRow(
                project_id=row["project_id"],
                document_id=row["document_id"],
                entity_type=row["entity_type"],
                entity_id=row["entity_id"],
                entity_version=row["entity_version"],
                deleted=row["deleted"],
                codec_id=row["codec_id"],
                codec_version=row["codec_version"],
                snapshot_format_version=row["snapshot_format_version"],
                snapshot_bytes=row["snapshot_bytes"],
                snapshot_sha256=row["snapshot_sha256"],
            )
            for row in entity_rows
        ),
    )
    rejected = False
    try:
        verify_business_snapshot(snapshot)
    except BusinessSnapshotError:
        rejected = True
    if rejected:
        _fail("sqlite_business_read_corrupt")
    return snapshot


def _read_operations(connection: Any, commit: Any) -> Tuple[Tuple[Dict[str, Any], ...], Tuple[Dict[str, Any], ...], int]:
    rows = connection.execute(
        "SELECT operation_id,previous_operation_id,previous_ordinal,previous_hash,"
        "self_hash,commit_id,commit_operation_count,commit_kind,ordinal,project_id,"
        "generation,document_id,command_id,source,kind,trace_id,group_id,actor_bytes,"
        "actor_sha256,operation_bytes,operation_sha256,operation_schema,codec_id,"
        "codec_version,occurred_at FROM business_operations WHERE commit_id=? "
        "ORDER BY ordinal",
        (commit["commit_id"],),
    ).fetchall()
    if len(rows) != commit["operation_count"]:
        _fail("sqlite_business_read_corrupt")
    if not rows:
        if (
            commit["commit_kind"] != "baseline"
            or commit["operation_target_count"] != 0
            or commit["last_operation_id"] is not None
            or commit["last_operation_ordinal"] is not None
            or commit["operation_tail_sha256"] != commit["previous_operation_tail_sha256"]
        ):
            _fail("sqlite_business_read_corrupt")
        return (), (), 0

    records: List[FrozenOperationRecord] = []
    bodies: List[Dict[str, Any]] = []
    for ordinal, row in enumerate(rows):
        rejected = False
        record = None
        body = None
        rebuilt = None
        actor_bytes = b""
        try:
            record = FrozenOperationRecord(
                protocol=row["operation_schema"],
                operation_id=row["operation_id"],
                command_id=row["command_id"],
                source=row["source"],
                trace_id=row["trace_id"],
                document_id=row["document_id"],
                body_bytes=row["operation_bytes"],
                body_sha256=row["operation_sha256"],
            )
            body = materialize_operation_body(
                FrozenOperationBody(
                    protocol=record.protocol,
                    body_bytes=record.body_bytes,
                    body_sha256=record.body_sha256,
                )
            )
            rebuilt = freeze_operation_record(body)
            actor_bytes = encode_payload_bounded(
                body["actor"],
                MAX_ATOMIC_JSON_PAYLOAD_BYTES,
                MAX_ATOMIC_JSON_PAYLOAD_NODES,
            )
        except (OperationRecordCodecError, BusinessCodecError, KeyError, TypeError):
            rejected = True
        if rejected or record is None or body is None or rebuilt is None:
            _fail("sqlite_business_read_corrupt")
        if (
            rebuilt != record
            or row["ordinal"] != ordinal
            or row["commit_operation_count"] != len(rows)
            or row["commit_kind"] != "change"
            or row["project_id"] != commit["project_id"]
            or row["generation"] != commit["generation"]
            or row["document_id"] != commit["document_id"]
            or row["trace_id"] != commit["trace_id"]
            or row["source"] != body["source"]
            or row["kind"] != body["kind"]
            or row["group_id"] != (body["group_id"] or None)
            or row["command_id"] != body["command_id"]
            or row["actor_bytes"] != actor_bytes
            or row["actor_sha256"] != hashlib.sha256(actor_bytes).digest()
            or row["operation_schema"] != OPERATION_RECORD_PROTOCOL
            or row["codec_id"] != CODEC_ID
            or row["codec_version"] != CODEC_VERSION
            or row["occurred_at"] != _canonical_operation_time(body["ts"])
        ):
            _fail("sqlite_business_read_corrupt")
        records.append(record)
        bodies.append(body)

    rejected = False
    chain = ()
    try:
        chain = build_frozen_operation_chain(
            tuple(records),
            first_prev_hash=commit["previous_operation_tail_sha256"].hex(),
        )
    except OperationRecordCodecError:
        rejected = True
    if rejected:
        _fail("sqlite_business_read_corrupt")
    operation_manifest: List[Dict[str, Any]] = []
    projected_targets = []
    for ordinal, (row, record, entry) in enumerate(zip(rows, records, chain)):
        if (
            row["previous_operation_id"] != (
                None if ordinal == 0 else rows[ordinal - 1]["operation_id"]
            )
            or row["previous_ordinal"] != (None if ordinal == 0 else ordinal - 1)
            or row["previous_hash"] != bytes.fromhex(entry.prev_hash)
            or row["self_hash"] != bytes.fromhex(entry.self_hash)
        ):
            _fail("sqlite_business_read_corrupt")
        operation_manifest.append(
            {
                "ordinal": ordinal,
                "operation_id": record.operation_id,
                "record_bytes": len(record.body_bytes),
                "record_sha256": record.body_sha256.hex(),
                "prev_hash": entry.prev_hash,
                "self_hash": entry.self_hash,
            }
        )
        rejected = False
        targets = ()
        try:
            targets = project_operation_targets(record)
        except OperationRecordCodecError:
            rejected = True
        if rejected:
            _fail("sqlite_business_read_corrupt")
        projected_targets.extend(targets)
    if (
        commit["commit_kind"] != "change"
        or commit["last_operation_id"] != records[-1].operation_id
        or commit["last_operation_ordinal"] != len(records) - 1
        or commit["operation_tail_sha256"] != bytes.fromhex(chain[-1].self_hash)
    ):
        _fail("sqlite_business_read_corrupt")

    target_rows = connection.execute(
        "SELECT target.operation_id,target.ordinal,target.entity_type,target.entity_id,"
        "target.field,target.patch_kind FROM business_operation_targets AS target "
        "JOIN business_operations AS operation ON operation.operation_id=target.operation_id "
        "WHERE operation.commit_id=? ORDER BY operation.ordinal,target.ordinal",
        (commit["commit_id"],),
    ).fetchall()
    if len(target_rows) != len(projected_targets) or len(target_rows) != commit["operation_target_count"]:
        _fail("sqlite_business_read_corrupt")
    target_manifest = []
    for row, expected in zip(target_rows, projected_targets):
        value = {
            "operation_id": row["operation_id"],
            "ordinal": row["ordinal"],
            "entity_type": row["entity_type"],
            "entity_id": row["entity_id"],
            "field": row["field"],
            "patch_kind": row["patch_kind"],
        }
        if value != {
            "operation_id": expected.operation_id,
            "ordinal": expected.ordinal,
            "entity_type": expected.entity_type,
            "entity_id": expected.entity_id,
            "field": expected.field,
            "patch_kind": expected.patch_kind,
        }:
            _fail("sqlite_business_read_corrupt")
        target_manifest.append(value)
    return tuple(operation_manifest), tuple(target_manifest), sum(
        len(record.body_bytes) for record in records
    )


def _read_outbox(connection: Any, commit: Any) -> Tuple[Tuple[Dict[str, Any], ...], int]:
    rows = connection.execute(
        "SELECT event_id,previous_event_id,previous_ordinal,commit_id,"
        "repository_revision,commit_event_count,operation_id,ordinal,topic,"
        "partition_key,event_bytes,event_sha256,payload_schema,codec_id,"
        "codec_version,status,state_revision,attempt_count FROM event_outbox "
        "WHERE commit_id=? ORDER BY ordinal",
        (commit["commit_id"],),
    ).fetchall()
    if len(rows) != commit["outbox_event_count"]:
        _fail("sqlite_business_read_corrupt")
    output = []
    payload_bytes = 0
    for ordinal, row in enumerate(rows):
        payload, size = _payload_manifest(
            row["event_bytes"], row["event_sha256"], row["payload_schema"],
            row["codec_id"], row["codec_version"],
        )
        if (
            row["ordinal"] != ordinal
            or row["repository_revision"] != commit["repository_revision"]
            or row["commit_event_count"] != len(rows)
            or row["previous_event_id"] != (None if ordinal == 0 else rows[ordinal - 1]["event_id"])
            or row["previous_ordinal"] != (None if ordinal == 0 else ordinal - 1)
            or type(row["state_revision"]) is not int
            or row["state_revision"] < 1
            or type(row["attempt_count"]) is not int
            or row["attempt_count"] < 0
        ):
            _fail("sqlite_business_read_corrupt")
        output.append(
            {
                "ordinal": ordinal,
                "event_id": row["event_id"],
                "operation_id": row["operation_id"],
                "topic": row["topic"],
                "partition_key": row["partition_key"],
                "status": "pending",
                "payload": payload,
            }
        )
        payload_bytes += size
    if rows and (
        commit["last_outbox_event_id"] != rows[-1]["event_id"]
        or commit["last_outbox_ordinal"] != len(rows) - 1
    ):
        _fail("sqlite_business_read_corrupt")
    if not rows and (
        commit["last_outbox_event_id"] is not None
        or commit["last_outbox_ordinal"] is not None
    ):
        _fail("sqlite_business_read_corrupt")
    return tuple(output), payload_bytes


def _read_actions(connection: Any, commit: Any) -> Tuple[Tuple[Dict[str, Any], ...], int]:
    rows = connection.execute(
        "SELECT action_id,idempotency_scope,idempotency_key,origin_commit_id,"
        "commit_action_count,ordinal,action_kind,action_bytes,action_sha256,"
        "payload_schema,codec_id,codec_version,replay_policy,idempotency_policy,"
        "status,state_revision,attempt_count,max_attempts FROM deferred_actions "
        "WHERE origin_commit_id=? ORDER BY ordinal",
        (commit["commit_id"],),
    ).fetchall()
    if len(rows) != commit["durable_action_count"]:
        _fail("sqlite_business_read_corrupt")
    output = []
    payload_bytes = 0
    for ordinal, row in enumerate(rows):
        payload, size = _payload_manifest(
            row["action_bytes"], row["action_sha256"], row["payload_schema"],
            row["codec_id"], row["codec_version"],
        )
        if (
            row["ordinal"] != ordinal
            or row["commit_action_count"] != len(rows)
            or type(row["state_revision"]) is not int
            or row["state_revision"] < 1
            or type(row["attempt_count"]) is not int
            or row["attempt_count"] < 0
        ):
            _fail("sqlite_business_read_corrupt")
        output.append(
            {
                "ordinal": ordinal,
                "action_id": row["action_id"],
                "action_type": row["action_kind"],
                "idempotency_key": row["idempotency_key"],
                "idempotency_scope": row["idempotency_scope"],
                "idempotency_policy": row["idempotency_policy"],
                "replay_policy": row["replay_policy"],
                "max_attempts": row["max_attempts"],
                "status": ACTION_STATUS_QUEUED,
                "payload": payload,
            }
        )
        payload_bytes += size
    return tuple(output), payload_bytes


def _read_pipelines(connection: Any, commit: Any) -> Tuple[Tuple[Dict[str, Any], ...], int]:
    rows = connection.execute(
        "SELECT transition.intent_id,transition.origin_commit_id,"
        "transition.commit_pipeline_intent_count,transition.ordinal,"
        "transition.job_id,transition.intent_type,transition.idempotency_key,"
        "transition.intent_kind,transition.before_state_revision,"
        "transition.after_state_revision,transition.before_status,"
        "transition.after_status,transition.intent_bytes,transition.intent_sha256,"
        "transition.payload_schema,transition.codec_id,transition.codec_version,"
        "transition.replay_policy,transition.max_attempts,job.pipeline_id "
        "FROM pipeline_job_transitions AS transition "
        "JOIN pipeline_jobs AS job ON job.job_id=transition.job_id "
        "WHERE transition.origin_commit_id=? ORDER BY transition.ordinal",
        (commit["commit_id"],),
    ).fetchall()
    if len(rows) != commit["pipeline_intent_count"]:
        _fail("sqlite_business_read_corrupt")
    output = []
    payload_bytes = 0
    for ordinal, row in enumerate(rows):
        payload, size = _payload_manifest(
            row["intent_bytes"], row["intent_sha256"], row["payload_schema"],
            row["codec_id"], row["codec_version"],
        )
        if (
            row["ordinal"] != ordinal
            or row["commit_pipeline_intent_count"] != len(rows)
            or row["after_state_revision"] != (
                1 if row["before_state_revision"] is None else row["before_state_revision"] + 1
            )
        ):
            _fail("sqlite_business_read_corrupt")
        output.append(
            {
                "ordinal": ordinal,
                "intent_id": row["intent_id"],
                "job_id": row["job_id"],
                "pipeline_id": row["pipeline_id"],
                "intent_type": row["intent_type"],
                "intent_kind": row["intent_kind"],
                "expected_status": row["before_status"],
                "target_status": row["after_status"],
                "expected_state_revision": row["before_state_revision"],
                "idempotency_key": row["idempotency_key"],
                "replay_policy": row["replay_policy"],
                "max_attempts": row["max_attempts"],
                "status": "queued",
                "payload": payload,
            }
        )
        payload_bytes += size
    return tuple(output), payload_bytes


def _read_assets(connection: Any, commit: Any, snapshot: BusinessSnapshotSet) -> Tuple[bytes, int, int]:
    rejected = False
    projection = None
    try:
        projection = project_asset_references(snapshot)
    except AssetReferenceProjectionError:
        rejected = True
    if rejected or projection is None:
        _fail("sqlite_business_read_corrupt")
    if (
        projection.protocol != ASSET_REFERENCE_PROJECTION_PROTOCOL
        or projection.reference_count != commit["asset_reference_count"]
        or projection.projection_sha256 != commit["asset_projection_sha256"]
    ):
        _fail("sqlite_business_read_corrupt")
    rows = connection.execute(
        "SELECT ref.project_id,ref.generation,ref.document_id,ref.document_version,"
        "ref.commit_asset_reference_count,ref.ordinal,ref.asset_sha256,"
        "ref.entity_type,ref.entity_id,"
        "ref.entity_deleted,ref.value_path,ref.ref_mime,ref.ref_size,ref.ref_ext,"
        "blob.content_key,blob.media_type,blob.extension,blob.byte_length,blob.status "
        "FROM document_asset_refs AS ref JOIN asset_blobs AS blob "
        "ON blob.asset_sha256=ref.asset_sha256 WHERE ref.commit_id=? "
        "ORDER BY ref.ordinal",
        (commit["commit_id"],),
    ).fetchall()
    if len(rows) != projection.reference_count:
        _fail("sqlite_business_read_corrupt")
    for ordinal, (row, occurrence) in enumerate(zip(rows, projection.occurrences)):
        reference = occurrence.reference
        sha = bytes.fromhex(reference.sha256)
        if (
            row["project_id"] != commit["project_id"]
            or row["generation"] != commit["generation"]
            or row["document_id"] != commit["document_id"]
            or row["document_version"] != commit["document_version"]
            or row["commit_asset_reference_count"] != projection.reference_count
            or row["ordinal"] != ordinal
            or row["asset_sha256"] != sha
            or row["entity_type"] != occurrence.entity_type
            or row["entity_id"] != occurrence.entity_id
            or row["entity_deleted"] != int(occurrence.entity_deleted)
            or row["value_path"] != occurrence.value_path
            or row["ref_mime"] != reference.mime
            or row["ref_size"] != reference.size
            or row["ref_ext"] != reference.ext
            or row["content_key"] != "sha256/%s/%s" % (reference.sha256[:2], reference.sha256)
            or (reference.mime is not None and row["media_type"] != reference.mime)
            or (reference.size is not None and row["byte_length"] != reference.size)
            or (reference.ext is not None and row["extension"] != reference.ext)
            or row["status"] != "available"
        ):
            _fail("sqlite_business_read_corrupt")
    return projection.projection_sha256, projection.reference_count, projection.unique_reference_count


def _read_inbox(connection: Any, commit: Any) -> Tuple[Optional[Dict[str, Any]], int]:
    rows = connection.execute(
        "SELECT scope,idempotency_key,request_sha256,request_bytes,request_schema,"
        "request_codec_id,request_codec_version,actor_id,claim_owner_id,claim_fence,"
        "status,state_revision,result_kind,response_bytes,response_sha256,"
        "response_schema,response_codec_id,response_codec_version FROM idempotency_inbox "
        "WHERE commit_id=? ORDER BY scope,idempotency_key",
        (commit["commit_id"],),
    ).fetchall()
    if len(rows) > 1:
        _fail("sqlite_business_read_corrupt")
    if not rows:
        return None, 0
    row = rows[0]
    request, request_size = _payload_manifest(
        row["request_bytes"], row["request_sha256"], row["request_schema"],
        row["request_codec_id"], row["request_codec_version"],
    )
    response, response_size = _payload_manifest(
        row["response_bytes"], row["response_sha256"], row["response_schema"],
        row["response_codec_id"], row["response_codec_version"],
    )
    if row["status"] != "committed" or row["result_kind"] != "commit" or row["state_revision"] < 2:
        _fail("sqlite_business_read_corrupt")
    return {
        "scope": row["scope"],
        "inbox_key": row["idempotency_key"],
        "actor_id": row["actor_id"],
        "claim_owner_id": row["claim_owner_id"],
        "claim_fence": row["claim_fence"],
        "state_revision": row["state_revision"] - 1,
        "outcome_kind": "commit",
        "request_sha256": row["request_sha256"].hex(),
        "expected_status": INBOX_EXPECTED_STATUS_CLAIMED,
        "target_status": INBOX_TARGET_STATUS_COMMITTED,
        "request": request,
        "response": response,
    }, request_size + response_size


def _read_confirmation(connection: Any, commit: Any) -> Tuple[Optional[Dict[str, Any]], int]:
    rows = connection.execute(
        "SELECT confirmation_id,actor_id,approval_nonce_hmac,nonce_key_id,"
        "request_sha256,status,state_revision,decision,resolution_bytes,"
        "resolution_sha256,resolution_schema,resolution_codec_id,"
        "resolution_codec_version,result_commit_id FROM confirmation_requests "
        "WHERE result_commit_id=? ORDER BY confirmation_id",
        (commit["commit_id"],),
    ).fetchall()
    if len(rows) > 1:
        _fail("sqlite_business_read_corrupt")
    if not rows:
        return None, 0
    row = rows[0]
    resolution, size = _payload_manifest(
        row["resolution_bytes"], row["resolution_sha256"], row["resolution_schema"],
        row["resolution_codec_id"], row["resolution_codec_version"],
    )
    if row["status"] != "consumed" or row["decision"] != "confirmed" or row["state_revision"] < 2:
        _fail("sqlite_business_read_corrupt")
    # Reservation owner/fence are intentionally cleared by the consumed-state
    # schema.  The immutable manifest remains their evidence; all durable
    # fields that survive consumption are checked below in manifest validation.
    return {
        "confirmation_id": row["confirmation_id"],
        "nonce_hmac": row["approval_nonce_hmac"].hex(),
        "nonce_key_id": row["nonce_key_id"],
        "expected_revision": row["state_revision"] - 1,
        "expected_request_sha256": row["request_sha256"].hex(),
        "actor_id": row["actor_id"],
        "decision": "confirmed",
        "result_commit_id": commit["commit_id"],
        "expected_status": CONFIRMATION_EXPECTED_STATUS_RESERVED,
        "target_status": CONFIRMATION_TARGET_STATUS_CONSUMED,
        "resolution": resolution,
    }, size


def _manifest_runtime_identity_matches(
    authority: Dict[str, Any],
    profile: BusinessRuntimeProfile,
) -> bool:
    """Bind immutable manifest authority to the exact mounted runtime."""

    runtime_generation = authority.get("runtime_generation")
    code_digest = authority.get("code_digest")
    return (
        type(runtime_generation) is str
        and runtime_generation == profile.runtime_generation
        and type(code_digest) is str
        and len(code_digest) == 64
        and all(character in _LOWER_HEX for character in code_digest)
        and hmac.compare_digest(code_digest, profile.code_digest.hex())
    )


def _verify_baseline_manifest(
    commit: Any,
    profile: BusinessRuntimeProfile,
    snapshot: BusinessSnapshotSet,
    evidence: _CommitEvidence,
    manifest: Dict[str, Any],
) -> None:
    """Verify the exact minimal descriptor for a bootstrap baseline.

    V5c will add cutover issuer/receipt semantics around this descriptor.  It
    does not weaken the present rule: head, commit and v2 snapshot rows are the
    baseline authority and every redundant manifest declaration must match.
    """

    if frozenset(manifest) != frozenset(
        ("schema", "protocol", "status", "authority", "commit", "after", "asset_references")
    ):
        _fail("sqlite_business_read_corrupt")
    if (
        manifest["schema"] != BUSINESS_BASELINE_MANIFEST_SCHEMA
        or manifest["protocol"] != BUSINESS_BASELINE_MANIFEST_PROTOCOL
        or manifest["status"] != "committed"
    ):
        _fail("sqlite_business_read_corrupt")
    authority = manifest["authority"]
    if type(authority) is not dict or frozenset(authority) != frozenset(
        (
            "authority", "authority_state", "epoch", "cutover_id",
            "source_admission_revision", "repository_revision",
            "state_revision", "runtime_generation", "code_digest",
        )
    ):
        _fail("sqlite_business_read_corrupt")
    if (
        authority.get("authority") != "sqlite"
        or authority.get("authority_state") not in ("active", "read_only")
        or authority.get("epoch") != commit["repository_epoch"]
        or authority.get("cutover_id") != commit["cutover_id"]
        or authority.get("source_admission_revision")
        != commit["source_admission_revision"]
        or authority.get("repository_revision") != commit["repository_revision"]
        or type(authority.get("state_revision")) is not int
        or not 1 <= authority.get("state_revision") <= profile.state_revision
        or not _manifest_runtime_identity_matches(authority, profile)
    ):
        _fail("sqlite_business_read_corrupt")
    if manifest["commit"] != {
        "commit_id": commit["commit_id"],
        "commit_kind": "baseline",
        "project_id": commit["project_id"],
        "generation": commit["generation"],
        "document_id": commit["document_id"],
        "document_version": commit["document_version"],
        "save_policy": commit["save_policy"],
    }:
        _fail("sqlite_business_read_corrupt")
    if manifest["after"] != {
        "business_root_sha256": commit["after_root_sha256"].hex(),
        "document_sha256": snapshot.document.snapshot_sha256.hex(),
        "entity_count": commit["entity_count"],
        "operation_tail_sha256": commit["operation_tail_sha256"].hex(),
    }:
        _fail("sqlite_business_read_corrupt")
    if manifest["asset_references"] != {
        "protocol": ASSET_REFERENCE_PROJECTION_PROTOCOL,
        "reference_count": evidence.asset_reference_count,
        "unique_reference_count": evidence.unique_asset_reference_count,
        "projection_sha256": evidence.asset_projection_sha256.hex(),
    }:
        _fail("sqlite_business_read_corrupt")
    if (
        commit["operation_count"] != 0
        or commit["operation_target_count"] != 0
        or commit["outbox_event_count"] != 0
        or commit["durable_action_count"] != 0
        or commit["pipeline_intent_count"] != 0
        or evidence.operations
        or evidence.targets
        or evidence.outbox
        or evidence.actions
        or evidence.pipelines
        or evidence.inbox is not None
        or evidence.confirmation is not None
        or evidence.auxiliary_payload_bytes != 0
    ):
        _fail("sqlite_business_read_corrupt")


def _verify_manifest(
    commit: Any,
    profile: BusinessRuntimeProfile,
    snapshot: BusinessSnapshotSet,
    evidence: _CommitEvidence,
) -> None:
    raw = commit["manifest_bytes"]
    digest = commit["manifest_sha256"]
    if (
        type(raw) is not bytes
        or not raw
        or len(raw) > MAX_ATOMIC_MANIFEST_BYTES
        or type(digest) is not bytes
        or len(digest) != 32
        or not hmac.compare_digest(
            digest_business_baseline_manifest(raw)
            if commit["commit_kind"] == "baseline"
            else _manifest_digest(raw),
            digest,
        )
    ):
        _fail("sqlite_business_read_corrupt")
    rejected = False
    manifest = None
    try:
        manifest = decode_payload_bounded(raw, MAX_ATOMIC_MANIFEST_BYTES, MAX_ATOMIC_MANIFEST_NODES)
    except BusinessCodecError:
        rejected = True
    if rejected or type(manifest) is not dict:
        _fail("sqlite_business_read_corrupt")
    if commit["commit_kind"] == "baseline":
        _verify_baseline_manifest(commit, profile, snapshot, evidence, manifest)
        return
    expected_keys = frozenset(
        (
            "schema", "protocol", "status", "authority", "commit", "before",
            "after", "operations", "operation_targets", "outbox",
            "durable_actions", "pipeline_intents", "asset_references", "inbox",
            "confirmation", "closure",
        )
    )
    if frozenset(manifest) != expected_keys:
        _fail("sqlite_business_read_corrupt")
    authority = manifest["authority"]
    before = manifest["before"]
    after = manifest["after"]
    expected_authority_keys = frozenset(
        (
            "authority", "authority_state", "epoch",
            "source_admission_revision", "revision", "control_revision",
            "cutover_id", "runtime_generation", "code_digest",
        )
    )
    if (
        manifest["schema"] != ATOMIC_COMMIT_MANIFEST_SCHEMA
        or manifest["protocol"] != ATOMIC_COMMIT_PROTOCOL
        or manifest["status"] != ATOMIC_COMMIT_STATUS_PREPARED
        or type(authority) is not dict
        or frozenset(authority) != expected_authority_keys
        or authority.get("authority") != "sqlite"
        or authority.get("authority_state") != "active"
        or authority.get("epoch") != commit["repository_epoch"]
        or authority.get("cutover_id") != commit["cutover_id"]
        or authority.get("source_admission_revision") != commit["source_admission_revision"]
        or authority.get("revision") != commit["repository_revision"] - 1
        or type(authority.get("control_revision")) is not int
        or not 1 <= authority.get("control_revision") <= profile.state_revision
        or not _manifest_runtime_identity_matches(authority, profile)
        or type(before) is not dict
        or type(after) is not dict
    ):
        _fail("sqlite_business_read_corrupt")
    if before != {
        "repository_revision": commit["repository_revision"] - 1,
        "project_revision": commit["project_revision"] - 1,
        "head_revision": commit["head_revision"] - 1,
        "document_version": commit["document_version"] - 1,
        "expected_last_commit_id": commit["previous_commit_id"],
        "business_root_sha256": commit["before_root_sha256"].hex(),
        "operation_tail_hash": commit["previous_operation_tail_sha256"].hex(),
    }:
        _fail("sqlite_business_read_corrupt")
    if after != {
        "document_version": commit["document_version"],
        "repository_revision": commit["repository_revision"],
        "project_revision": commit["project_revision"],
        "head_revision": commit["head_revision"],
        "commit_id": commit["commit_id"],
        "business_root_sha256": commit["after_root_sha256"].hex(),
        "document_sha256": snapshot.document.snapshot_sha256.hex(),
        "entity_count": commit["entity_count"],
    }:
        _fail("sqlite_business_read_corrupt")
    expected_commit = {
        "commit_id": commit["commit_id"],
        "commit_kind": "change",
        "trace_id": commit["trace_id"],
        "project_id": commit["project_id"],
        "generation": commit["generation"],
        "document_id": commit["document_id"],
        "save_policy": commit["save_policy"],
    }
    target_digest = _component_digest(
        b"ze.atomic-business-commit.operation-targets.v1", evidence.targets
    )
    if (
        manifest["commit"] != expected_commit
        or manifest["operations"] != list(evidence.operations)
        or manifest["operation_targets"] != {
            "count": len(evidence.targets), "sha256": target_digest.hex()
        }
        or manifest["outbox"] != list(evidence.outbox)
        or manifest["durable_actions"] != list(evidence.actions)
        or manifest["pipeline_intents"] != list(evidence.pipelines)
        or manifest["asset_references"] != {
            "protocol": ASSET_REFERENCE_PROJECTION_PROTOCOL,
            "reference_count": evidence.asset_reference_count,
            "unique_reference_count": evidence.unique_asset_reference_count,
            "projection_sha256": evidence.asset_projection_sha256.hex(),
        }
        or manifest["inbox"] != evidence.inbox
    ):
        _fail("sqlite_business_read_corrupt")
    confirmation = manifest["confirmation"]
    if evidence.confirmation is None:
        if confirmation is not None:
            _fail("sqlite_business_read_corrupt")
    else:
        if type(confirmation) is not dict:
            _fail("sqlite_business_read_corrupt")
        if frozenset(confirmation) != frozenset(evidence.confirmation) | frozenset(
            ("reservation_owner_id", "reservation_fence")
        ):
            _fail("sqlite_business_read_corrupt")
        for key, value in evidence.confirmation.items():
            if confirmation.get(key) != value:
                _fail("sqlite_business_read_corrupt")
        owner = confirmation.get("reservation_owner_id")
        fence = confirmation.get("reservation_fence")
        if (
            type(owner) is not str
            or len(owner) != 32
            or any(character not in _LOWER_HEX for character in owner)
            or type(fence) is not int
            or fence < 1
        ):
            _fail("sqlite_business_read_corrupt")
    closure = manifest["closure"]
    if closure != {
        "operation_count": len(evidence.operations),
        "operation_target_count": len(evidence.targets),
        "outbox_count": len(evidence.outbox),
        "durable_action_count": len(evidence.actions),
        "pipeline_intent_count": len(evidence.pipelines),
        "asset_reference_count": evidence.asset_reference_count,
        "auxiliary_payload_bytes": evidence.auxiliary_payload_bytes,
        "operation_tail_hash": commit["operation_tail_sha256"].hex(),
    }:
        _fail("sqlite_business_read_corrupt")


def _read_pinned(
    connection: Any,
    profile: BusinessRuntimeProfile,
    project_id: str,
    generation: int,
    expected_head_revision: Optional[int],
    expected_project_revision: Optional[int],
    expected_commit_id: Optional[str],
) -> SQLiteBusinessHead:
    project = _one(
        connection,
        "SELECT project_id,generation,project_revision,status,current_snapshot_sha256 "
        "FROM repository_projects WHERE project_id=?",
        (project_id,),
    )
    if project["generation"] != generation:
        _fail("sqlite_business_read_generation_stale")
    if project["status"] != "live":
        _fail("sqlite_business_read_head_unavailable")
    heads = connection.execute(
        "SELECT project_id,generation,document_id,document_version,commit_id,"
        "head_revision,status,current_snapshot_sha256,current_project_snapshot_sha256,"
        "business_root_sha256,operation_tail_sha256,asset_projection_sha256,"
        "asset_reference_count,entity_count FROM document_commit_heads "
        "WHERE project_id=? AND generation=?",
        (project_id, generation),
    ).fetchall()
    if len(heads) != 1:
        _fail("sqlite_business_read_corrupt")
    head = heads[0]
    if head["status"] != "live":
        _fail("sqlite_business_read_head_unavailable")
    if expected_head_revision is not None and head["head_revision"] != expected_head_revision:
        _fail("sqlite_business_read_revision_stale")
    if (
        expected_project_revision is not None
        and project["project_revision"] != expected_project_revision
    ):
        _fail("sqlite_business_read_revision_stale")
    if expected_commit_id is not None and head["commit_id"] != expected_commit_id:
        _fail("sqlite_business_read_revision_stale")
    commit = _one(
        connection,
        "SELECT * FROM business_commits WHERE commit_id=?",
        (head["commit_id"],),
    )
    if (
        head["project_id"] != project_id
        or head["generation"] != generation
        or head["head_revision"] != commit["head_revision"]
        or project["project_revision"] != commit["project_revision"]
        or profile.repository_revision < commit["repository_revision"]
        or head["document_id"] != commit["document_id"]
        or head["document_version"] != commit["document_version"]
        or head["business_root_sha256"] != commit["after_root_sha256"]
        or head["operation_tail_sha256"] != commit["operation_tail_sha256"]
        or head["asset_projection_sha256"] != commit["asset_projection_sha256"]
        or head["asset_reference_count"] != commit["asset_reference_count"]
        or head["entity_count"] != commit["entity_count"]
        or head["current_project_snapshot_sha256"] != head["business_root_sha256"]
        or commit["transaction_id"] != BUSINESS_TRANSACTION_PREFIX + commit["commit_id"]
        or commit["receipt_kind"] != BUSINESS_TRANSACTION_KIND
    ):
        _fail("sqlite_business_read_corrupt")
    receipt = _one(
        connection,
        "SELECT transaction_id,kind FROM transaction_receipts WHERE transaction_id=?",
        (commit["transaction_id"],),
    )
    if receipt["kind"] != BUSINESS_TRANSACTION_KIND:
        _fail("sqlite_business_read_corrupt")

    snapshot = _read_snapshot(connection, project, head, commit)
    operations, targets, auxiliary = _read_operations(connection, commit)
    outbox, size = _read_outbox(connection, commit)
    auxiliary += size
    if len(outbox) != len(operations) or any(
        event["ordinal"] != operation["ordinal"]
        or event["operation_id"] != operation["operation_id"]
        for operation, event in zip(operations, outbox)
    ):
        if commit["commit_kind"] == "change":
            _fail("sqlite_business_read_corrupt")
    actions, size = _read_actions(connection, commit)
    auxiliary += size
    pipelines, size = _read_pipelines(connection, commit)
    auxiliary += size
    projection_sha256, reference_count, unique_reference_count = _read_assets(
        connection, commit, snapshot
    )
    inbox, size = _read_inbox(connection, commit)
    auxiliary += size
    confirmation, size = _read_confirmation(connection, commit)
    auxiliary += size
    evidence = _CommitEvidence(
        operations=operations,
        targets=targets,
        outbox=outbox,
        actions=actions,
        pipelines=pipelines,
        inbox=inbox,
        confirmation=confirmation,
        asset_projection_sha256=projection_sha256,
        asset_reference_count=reference_count,
        unique_asset_reference_count=unique_reference_count,
        auxiliary_payload_bytes=auxiliary,
    )
    _verify_manifest(commit, profile, snapshot, evidence)
    return SQLiteBusinessHead(
        project_id=project_id,
        generation=generation,
        document_id=head["document_id"],
        document_version=head["document_version"],
        commit_id=head["commit_id"],
        project_revision=project["project_revision"],
        head_revision=head["head_revision"],
        repository_revision=profile.repository_revision,
        operation_count=commit["operation_count"],
        outbox_event_count=commit["outbox_event_count"],
        durable_action_count=commit["durable_action_count"],
        pipeline_intent_count=commit["pipeline_intent_count"],
        asset_reference_count=commit["asset_reference_count"],
        runtime=profile,
        snapshot=snapshot,
    )


class SQLiteBusinessReader:
    """Private mounted-v6 reader; no production export or implicit fallback."""

    __slots__ = ("_database",)

    def __init__(self, database: SqliteDatabase) -> None:
        if type(database) is not SqliteDatabase:
            raise TypeError("database must be an exact SqliteDatabase")
        self._database = database

    def read_current_head(
        self,
        project_id: str,
        generation: int,
        *,
        expected_runtime: Optional[BusinessRuntimeProfile] = None,
        expected_repository_revision: Optional[int] = None,
        expected_project_revision: Optional[int] = None,
        expected_head_revision: Optional[int] = None,
        expected_commit_id: Optional[str] = None,
    ) -> SQLiteBusinessHead:
        project_id = _input_text(project_id)
        generation = _positive_integer(generation)
        expected_repository_revision = _optional_positive_integer(
            expected_repository_revision
        )
        expected_head_revision = _optional_positive_integer(expected_head_revision)
        expected_project_revision = _optional_positive_integer(
            expected_project_revision
        )
        if expected_commit_id is not None:
            expected_commit_id = _hex_identity(expected_commit_id, 32)
        if expected_runtime is not None and type(expected_runtime) is not BusinessRuntimeProfile:
            _fail("sqlite_business_read_input_invalid")

        connection = None
        unexpected_failure = False
        cleanup_complete = True
        try:
            connection, _filesystem = self._database._open_unvalidated()
            connection.execute("BEGIN")
            profile = _validate_business_runtime_connection(
                self._database,
                connection,
                access=BusinessRuntimeAccess.READER,
            )
            if profile.authority_state not in (
                BusinessAuthorityState.ACTIVE,
                BusinessAuthorityState.READ_ONLY,
            ):
                _fail("sqlite_business_read_repair_required")
            if expected_runtime is not None and profile != expected_runtime:
                _fail("sqlite_business_read_authority_stale")
            if (
                expected_repository_revision is not None
                and profile.repository_revision != expected_repository_revision
            ):
                _fail("sqlite_business_read_revision_stale")
            connection.set_authorizer(_ReadOnlyAuthorizer())
            result = _read_pinned(
                connection,
                profile,
                project_id,
                generation,
                expected_head_revision,
                expected_project_revision,
                expected_commit_id,
            )
        except SQLiteBusinessReadError:
            raise
        except BaseException as exc:
            if not isinstance(exc, Exception):
                raise
            unexpected_failure = True
        finally:
            cleanup_complete = _finish_read_connection(
                connection,
                reset_authorizer=True,
            )
        if unexpected_failure or not cleanup_complete:
            _fail("sqlite_business_read_corrupt")

        # The first BEGIN snapshot is the read's linearization point.  A normal
        # commit that overlaps the read (even for this project) may therefore
        # sort after it and advance the global revision.  This fresh fence does
        # not promise "latest at return"; it only rejects authority/control
        # transitions and impossible global-revision rollback before handoff.
        confirmation_connection = None
        unexpected_failure = False
        cleanup_complete = True
        try:
            confirmation_connection, _filesystem = self._database._open_unvalidated()
            confirmation_connection.execute("BEGIN")
            confirmed = _validate_business_runtime_connection(
                self._database,
                confirmation_connection,
                access=BusinessRuntimeAccess.READER,
            )
            if not _publish_fence_is_compatible(profile, confirmed):
                _fail("sqlite_business_read_authority_changed")
            if confirmed.authority_state not in (
                BusinessAuthorityState.ACTIVE,
                BusinessAuthorityState.READ_ONLY,
            ):
                _fail("sqlite_business_read_repair_required")
        except SQLiteBusinessReadError:
            raise
        except BaseException as exc:
            if not isinstance(exc, Exception):
                raise
            unexpected_failure = True
        finally:
            cleanup_complete = _finish_read_connection(
                confirmation_connection,
                reset_authorizer=False,
            )
        if unexpected_failure or not cleanup_complete:
            _fail("sqlite_business_read_corrupt")
        return result


__all__ = [
    "SQLiteBusinessHead",
    "SQLiteBusinessReadError",
    "SQLiteBusinessReader",
]
