"""Pure closure projection for one future atomic business commit.

The projection binds authority, optimistic before-head, verified after-head,
canonical Operations, event outbox, serializable actions/pipeline intents,
schema-aware asset references and optional inbox/confirmation facts.  It is a
private prepared plan, not a commit receipt and not a database authority token.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any, NamedTuple, Optional, Tuple

from asset_reference_projection import (
    AssetReferenceProjection,
    AssetReferenceProjectionError,
    project_asset_references,
    verify_asset_reference_projection,
)
from business_codec import BusinessCodecError, encode_payload_bounded
from business_snapshot import (
    BusinessSnapshotError,
    BusinessSnapshotSet,
    verify_business_snapshot,
)
from business_transaction_contract import (
    ATOMIC_COMMIT_MANIFEST_SCHEMA,
    ATOMIC_COMMIT_PROTOCOL,
    ATOMIC_COMMIT_STATUS_PREPARED,
    AuthorityBinding,
    BeforeBusinessHead,
    BusinessTransactionContractError,
    CandidateBusinessHead,
    ConfirmationConsumption,
    DurableActionIntent,
    FrozenJsonPayload,
    InboxClaim,
    MAX_ATOMIC_DURABLE_ACTIONS,
    MAX_ATOMIC_JSON_PAYLOAD_BYTES,
    MAX_ATOMIC_JSON_PAYLOAD_NODES,
    MAX_ATOMIC_MANIFEST_BYTES,
    MAX_ATOMIC_MANIFEST_NODES,
    MAX_ATOMIC_OPERATION_TARGETS,
    MAX_ATOMIC_OPERATIONS,
    MAX_ATOMIC_OUTBOX_EVENTS,
    MAX_ATOMIC_PIPELINE_INTENTS,
    MAX_ATOMIC_TOTAL_AUXILIARY_BYTES,
    MAX_BUSINESS_ID_BYTES,
    OutboxEventIntent,
    PipelineIntent,
    verify_frozen_json_payload,
)
from operation_record_codec import (
    FrozenOperationBody,
    FrozenOperationChainEntry,
    FrozenOperationRecord,
    FrozenOperationTarget,
    OperationRecordCodecError,
    build_frozen_operation_chain,
    freeze_operation_record,
    materialize_operation_body,
    project_operation_targets,
)


ATOMIC_COMMIT_MANIFEST_HASH_DOMAIN = b"ze.atomic-business-commit.manifest-digest.v2"
_PLAN_ISSUER = object()
_LOWER_HEX = frozenset("0123456789abcdef")


class BusinessTransactionIntegrityError(ValueError):
    """Stable, payload-free closure validation failure."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        if type(code) is not str or not code:
            raise TypeError("code must be a non-empty exact string")
        self.code = code
        super().__init__(code)


class AtomicCommitClosure(NamedTuple):
    before_repository_revision: int
    after_repository_revision: int
    before_project_revision: int
    after_project_revision: int
    before_head_revision: int
    after_head_revision: int
    operation_count: int
    operation_target_count: int
    outbox_count: int
    durable_action_count: int
    pipeline_intent_count: int
    asset_reference_count: int
    auxiliary_payload_bytes: int
    operation_tail_hash: str
    after_snapshot_sha256: bytes
    operations_sha256: bytes
    operation_targets_sha256: bytes
    outbox_sha256: bytes
    durable_actions_sha256: bytes
    pipeline_intents_sha256: bytes
    asset_references_sha256: bytes
    inbox_sha256: bytes
    confirmation_sha256: bytes
    manifest_bytes: bytes
    manifest_sha256: bytes


class _PreparedPlan(NamedTuple):
    snapshot: BusinessSnapshotSet
    records: Tuple[FrozenOperationRecord, ...]
    chain: Tuple[FrozenOperationChainEntry, ...]
    targets: Tuple[FrozenOperationTarget, ...]
    outbox: Tuple[OutboxEventIntent, ...]
    actions: Tuple[DurableActionIntent, ...]
    pipelines: Tuple[PipelineIntent, ...]
    assets: AssetReferenceProjection
    candidate: CandidateBusinessHead
    closure: AtomicCommitClosure


class AtomicCommitPlan:
    """Issuer-stamped immutable carrier accepted by a future SQL writer."""

    __slots__ = (
        "authority",
        "commit_id",
        "trace_id",
        "before",
        "candidate",
        "after_snapshot",
        "operations",
        "operation_chain",
        "operation_targets",
        "outbox",
        "durable_actions",
        "pipeline_intents",
        "asset_references",
        "inbox",
        "confirmation",
        "closure",
        "protocol",
        "status",
        "_issuer",
    )

    def __init__(
        self,
        authority: AuthorityBinding,
        commit_id: str,
        trace_id: str,
        before: BeforeBusinessHead,
        prepared: _PreparedPlan,
        inbox: Optional[InboxClaim],
        confirmation: Optional[ConfirmationConsumption],
        *,
        _issuer: Any,
    ) -> None:
        if _issuer is not _PLAN_ISSUER:
            raise BusinessTransactionIntegrityError("atomic_commit_plan_issuer_invalid")
        object.__setattr__(self, "authority", authority)
        object.__setattr__(self, "commit_id", commit_id)
        object.__setattr__(self, "trace_id", trace_id)
        object.__setattr__(self, "before", before)
        object.__setattr__(self, "candidate", prepared.candidate)
        object.__setattr__(self, "after_snapshot", prepared.snapshot)
        object.__setattr__(self, "operations", prepared.records)
        object.__setattr__(self, "operation_chain", prepared.chain)
        object.__setattr__(self, "operation_targets", prepared.targets)
        object.__setattr__(self, "outbox", prepared.outbox)
        object.__setattr__(self, "durable_actions", prepared.actions)
        object.__setattr__(self, "pipeline_intents", prepared.pipelines)
        object.__setattr__(self, "asset_references", prepared.assets)
        object.__setattr__(self, "inbox", inbox)
        object.__setattr__(self, "confirmation", confirmation)
        object.__setattr__(self, "closure", prepared.closure)
        object.__setattr__(self, "protocol", ATOMIC_COMMIT_PROTOCOL)
        object.__setattr__(self, "status", ATOMIC_COMMIT_STATUS_PREPARED)
        object.__setattr__(self, "_issuer", _issuer)

    def __setattr__(self, name: str, value: Any) -> None:
        del name, value
        raise AttributeError("AtomicCommitPlan is immutable")


def _fail(code: str) -> None:
    raise BusinessTransactionIntegrityError(code)


def _frame(hasher: Any, value: bytes) -> None:
    hasher.update(len(value).to_bytes(8, "big"))
    hasher.update(value)


def _manifest_digest(payload: bytes) -> bytes:
    hasher = hashlib.sha256()
    _frame(hasher, ATOMIC_COMMIT_MANIFEST_HASH_DOMAIN)
    _frame(hasher, payload)
    return hasher.digest()


def _component_digest(domain: bytes, parts: Tuple[bytes, ...]) -> bytes:
    hasher = hashlib.sha256()
    _frame(hasher, domain)
    for part in parts:
        _frame(hasher, part)
    return hasher.digest()


def _component_part(value: Any) -> bytes:
    invalid = False
    try:
        return encode_payload_bounded(
            value,
            MAX_ATOMIC_JSON_PAYLOAD_BYTES,
            MAX_ATOMIC_JSON_PAYLOAD_NODES,
        )
    except BusinessCodecError:
        invalid = True
    if invalid:
        _fail("atomic_commit_component_budget_exceeded")
    raise AssertionError("unreachable")


def _text(value: Any, *, nonempty: bool = True) -> str:
    if type(value) is not str or (nonempty and not value):
        _fail("atomic_commit_text_invalid")
    invalid_utf8 = False
    try:
        value.encode("utf-8", "strict")
    except UnicodeEncodeError:
        invalid_utf8 = True
    if invalid_utf8:
        _fail("atomic_commit_text_invalid")
    return value


def _business_id(value: Any) -> str:
    value = _text(value)
    if len(value.encode("utf-8", "strict")) > MAX_BUSINESS_ID_BYTES:
        _fail("atomic_commit_business_id_too_large")
    return value


def _hex(value: Any, length: int) -> str:
    if (
        type(value) is not str
        or len(value) != length
        or any(character not in _LOWER_HEX for character in value)
    ):
        _fail("atomic_commit_identity_invalid")
    return value


def _exact_tuple(value: Any, item_type: type, maximum: int, code: str) -> tuple:
    if type(value) is not tuple or len(value) > maximum:
        _fail(code)
    if any(type(item) is not item_type for item in value):
        _fail(code)
    return value


def _preflight_auxiliary_bytes(
    *,
    operations: Any,
    outbox: Any,
    durable_actions: Any,
    pipeline_intents: Any,
    inbox: Optional[InboxClaim],
    confirmation: Optional[ConfirmationConsumption],
) -> int:
    """Reject an oversized frozen carrier before decoding or projecting it."""

    records = _exact_tuple(
        operations,
        FrozenOperationRecord,
        MAX_ATOMIC_OPERATIONS,
        "atomic_commit_operations_invalid",
    )
    events = _exact_tuple(
        outbox,
        OutboxEventIntent,
        MAX_ATOMIC_OUTBOX_EVENTS,
        "atomic_commit_outbox_invalid",
    )
    actions = _exact_tuple(
        durable_actions,
        DurableActionIntent,
        MAX_ATOMIC_DURABLE_ACTIONS,
        "atomic_commit_actions_invalid",
    )
    pipelines = _exact_tuple(
        pipeline_intents,
        PipelineIntent,
        MAX_ATOMIC_PIPELINE_INTENTS,
        "atomic_commit_pipelines_invalid",
    )
    if inbox is not None and type(inbox) is not InboxClaim:
        _fail("atomic_commit_inbox_type_invalid")
    if confirmation is not None and type(confirmation) is not ConfirmationConsumption:
        _fail("atomic_commit_confirmation_type_invalid")

    total = 0

    def add(raw: Any) -> None:
        nonlocal total
        if type(raw) is not bytes:
            _fail("atomic_commit_auxiliary_carrier_invalid")
        size = len(raw)
        if size > MAX_ATOMIC_TOTAL_AUXILIARY_BYTES - total:
            _fail("atomic_commit_auxiliary_budget_exceeded")
        total += size

    def add_payload(payload: Any) -> None:
        if type(payload) is not FrozenJsonPayload:
            _fail("atomic_commit_auxiliary_carrier_invalid")
        try:
            raw = payload.payload_bytes
        except AttributeError:
            _fail("atomic_commit_auxiliary_carrier_invalid")
        add(raw)

    for record in records:
        try:
            raw = record.body_bytes
        except AttributeError:
            _fail("atomic_commit_auxiliary_carrier_invalid")
        add(raw)
    for event in events:
        add_payload(event.payload)
    for action in actions:
        add_payload(action.payload)
    for intent in pipelines:
        add_payload(intent.payload)
    if inbox is not None:
        add_payload(inbox.request)
        add_payload(inbox.response)
    if confirmation is not None:
        add_payload(confirmation.resolution)
    return total


def _canonical_records(
    operations: Any,
    *,
    trace_id: str,
    document_id: str,
) -> Tuple[FrozenOperationRecord, ...]:
    records = _exact_tuple(
        operations,
        FrozenOperationRecord,
        MAX_ATOMIC_OPERATIONS,
        "atomic_commit_operations_invalid",
    )
    if not records:
        _fail("atomic_commit_operations_empty")
    verified = []
    operation_ids = set()
    for record in records:
        invalid_record = False
        try:
            owned_body = materialize_operation_body(
                FrozenOperationBody(
                    protocol=record.protocol,
                    body_bytes=record.body_bytes,
                    body_sha256=record.body_sha256,
                )
            )
            rebuilt = freeze_operation_record(owned_body)
        except OperationRecordCodecError:
            invalid_record = True
            rebuilt = None
        if invalid_record:
            _fail("atomic_commit_operation_changed")
        if rebuilt != record:
            _fail("atomic_commit_operation_changed")
        _business_id(record.command_id)
        _business_id(record.document_id)
        _business_id(record.trace_id)
        group_id = owned_body["group_id"]
        if group_id:
            _business_id(group_id)
        for patch in owned_body["patches"]:
            _business_id(patch["entity_type"])
            _business_id(patch["entity_id"])
            _business_id(patch["field"])
        if (
            record.operation_id in operation_ids
            or _hex(record.operation_id, 32) != record.operation_id
            or record.trace_id != trace_id
            or record.document_id != document_id
        ):
            _fail("atomic_commit_operation_binding_invalid")
        operation_ids.add(record.operation_id)
        verified.append(record)
    return tuple(verified)


def _canonical_operation_targets(
    records: Tuple[FrozenOperationRecord, ...],
) -> Tuple[FrozenOperationTarget, ...]:
    """Project every forward patch without exceeding the v6 table domain."""

    projected = []
    for record in records:
        invalid_targets = False
        try:
            targets = project_operation_targets(record)
        except OperationRecordCodecError:
            invalid_targets = True
            targets = ()
        if invalid_targets:
            _fail("atomic_commit_operation_targets_invalid")
        if len(projected) + len(targets) > MAX_ATOMIC_OPERATION_TARGETS:
            _fail("atomic_commit_operation_target_count_exceeded")
        projected.extend(targets)
    return tuple(projected)


def _canonical_outbox(
    value: Any,
    records: Tuple[FrozenOperationRecord, ...],
) -> Tuple[OutboxEventIntent, ...]:
    events = _exact_tuple(
        value,
        OutboxEventIntent,
        MAX_ATOMIC_OUTBOX_EVENTS,
        "atomic_commit_outbox_invalid",
    )
    if len(events) != len(records):
        _fail("atomic_commit_outbox_not_closed")
    event_ids = set()
    for ordinal, (event, record) in enumerate(zip(events, records)):
        invalid_event = False
        try:
            verify_frozen_json_payload(event.payload)
            rebuilt = OutboxEventIntent(
                ordinal=event.ordinal,
                event_id=event.event_id,
                operation_id=event.operation_id,
                topic=event.topic,
                partition_key=event.partition_key,
                payload=event.payload,
            )
        except BusinessTransactionContractError:
            invalid_event = True
            rebuilt = None
        if invalid_event:
            _fail("atomic_commit_outbox_changed")
        if (
            rebuilt != event
            or event.ordinal != ordinal
            or event.operation_id != record.operation_id
            or event.event_id in event_ids
        ):
            _fail("atomic_commit_outbox_order_invalid")
        event_ids.add(event.event_id)
    return events


def _canonical_actions(value: Any) -> Tuple[DurableActionIntent, ...]:
    actions = _exact_tuple(
        value,
        DurableActionIntent,
        MAX_ATOMIC_DURABLE_ACTIONS,
        "atomic_commit_actions_invalid",
    )
    ids = set()
    idempotency = set()
    for ordinal, action in enumerate(actions):
        invalid_action = False
        try:
            verify_frozen_json_payload(action.payload)
            rebuilt = DurableActionIntent(
                ordinal=action.ordinal,
                action_id=action.action_id,
                action_type=action.action_type,
                idempotency_key=action.idempotency_key,
                payload=action.payload,
                replay_policy=action.replay_policy,
                idempotency_policy=action.idempotency_policy,
                idempotency_scope=action.idempotency_scope,
                max_attempts=action.max_attempts,
            )
        except BusinessTransactionContractError:
            invalid_action = True
            rebuilt = None
        if invalid_action:
            _fail("atomic_commit_action_changed")
        if (
            rebuilt != action
            or action.ordinal != ordinal
            or action.action_id in ids
            or action.idempotency_key in idempotency
        ):
            _fail("atomic_commit_action_order_invalid")
        ids.add(action.action_id)
        idempotency.add(action.idempotency_key)
    return actions


def _canonical_pipelines(value: Any) -> Tuple[PipelineIntent, ...]:
    pipelines = _exact_tuple(
        value,
        PipelineIntent,
        MAX_ATOMIC_PIPELINE_INTENTS,
        "atomic_commit_pipelines_invalid",
    )
    ids = set()
    job_ids = set()
    idempotency = set()
    for ordinal, intent in enumerate(pipelines):
        invalid_intent = False
        try:
            verify_frozen_json_payload(intent.payload)
            rebuilt = PipelineIntent(
                ordinal=intent.ordinal,
                intent_id=intent.intent_id,
                job_id=intent.job_id,
                pipeline_id=intent.pipeline_id,
                intent_type=intent.intent_type,
                idempotency_key=intent.idempotency_key,
                payload=intent.payload,
                intent_kind=intent.intent_kind,
                expected_status=intent.expected_status,
                target_status=intent.target_status,
                replay_policy=intent.replay_policy,
                expected_state_revision=intent.expected_state_revision,
                max_attempts=intent.max_attempts,
            )
        except BusinessTransactionContractError:
            invalid_intent = True
            rebuilt = None
        if invalid_intent:
            _fail("atomic_commit_pipeline_changed")
        if (
            rebuilt != intent
            or intent.ordinal != ordinal
            or intent.intent_id in ids
            or intent.job_id in job_ids
            or intent.idempotency_key in idempotency
        ):
            _fail("atomic_commit_pipeline_order_invalid")
        ids.add(intent.intent_id)
        job_ids.add(intent.job_id)
        idempotency.add(intent.idempotency_key)
    return pipelines


def _payload_manifest(value: Any) -> dict:
    return {
        "schema": value.schema,
        "codec_id": value.codec_id,
        "codec_version": value.codec_version,
        "bytes": len(value.payload_bytes),
        "sha256": value.payload_sha256.hex(),
    }


def _build_manifest(
    *,
    authority: AuthorityBinding,
    commit_id: str,
    trace_id: str,
    before: BeforeBusinessHead,
    snapshot: BusinessSnapshotSet,
    records: Tuple[FrozenOperationRecord, ...],
    chain: Tuple[FrozenOperationChainEntry, ...],
    targets: Tuple[FrozenOperationTarget, ...],
    target_digest: bytes,
    outbox: Tuple[OutboxEventIntent, ...],
    actions: Tuple[DurableActionIntent, ...],
    pipelines: Tuple[PipelineIntent, ...],
    assets: AssetReferenceProjection,
    inbox: Optional[InboxClaim],
    confirmation: Optional[ConfirmationConsumption],
    auxiliary_bytes: int,
) -> bytes:
    manifest = {
        "schema": ATOMIC_COMMIT_MANIFEST_SCHEMA,
        "protocol": ATOMIC_COMMIT_PROTOCOL,
        "status": ATOMIC_COMMIT_STATUS_PREPARED,
        "authority": {
            "authority": authority.authority,
            "authority_state": authority.authority_state,
            "epoch": authority.authority_epoch,
            "source_admission_revision": authority.source_admission_revision,
            "revision": authority.authority_revision,
            "control_revision": authority.control_revision,
            "cutover_id": authority.cutover_id,
            "runtime_generation": authority.runtime_generation,
            "code_digest": authority.code_digest.hex(),
        },
        "commit": {
            "commit_id": commit_id,
            "commit_kind": "change",
            "trace_id": trace_id,
            "project_id": snapshot.project.project_id,
            "generation": snapshot.project.generation,
            "document_id": snapshot.document.document_id,
            "save_policy": snapshot.document.save_policy or "legacy",
        },
        "before": {
            "repository_revision": authority.authority_revision,
            "project_revision": before.project_revision,
            "head_revision": before.head_revision,
            "document_version": before.document_version,
            "expected_last_commit_id": before.expected_last_commit_id,
            "business_root_sha256": before.business_root_sha256.hex(),
            "operation_tail_hash": before.operation_tail_hash,
        },
        "after": {
            "document_version": snapshot.document.document_version,
            "repository_revision": authority.authority_revision + 1,
            "project_revision": before.project_revision + 1,
            "head_revision": before.head_revision + 1,
            "commit_id": commit_id,
            "business_root_sha256": snapshot.project.snapshot_sha256.hex(),
            "document_sha256": snapshot.document.snapshot_sha256.hex(),
            "entity_count": len(snapshot.entities),
        },
        "operations": [
            {
                "ordinal": ordinal,
                "operation_id": record.operation_id,
                "record_bytes": len(record.body_bytes),
                "record_sha256": record.body_sha256.hex(),
                "prev_hash": entry.prev_hash,
                "self_hash": entry.self_hash,
            }
            for ordinal, (record, entry) in enumerate(zip(records, chain))
        ],
        "operation_targets": {
            "count": len(targets),
            "sha256": target_digest.hex(),
        },
        "outbox": [
            {
                "ordinal": event.ordinal,
                "event_id": event.event_id,
                "operation_id": event.operation_id,
                "topic": event.topic,
                "partition_key": event.partition_key,
                "status": event.status,
                "payload": _payload_manifest(event.payload),
            }
            for event in outbox
        ],
        "durable_actions": [
            {
                "ordinal": action.ordinal,
                "action_id": action.action_id,
                "action_type": action.action_type,
                "idempotency_key": action.idempotency_key,
                "idempotency_scope": action.idempotency_scope,
                "idempotency_policy": action.idempotency_policy.value,
                "replay_policy": action.replay_policy.value,
                "max_attempts": action.max_attempts,
                "status": action.status,
                "payload": _payload_manifest(action.payload),
            }
            for action in actions
        ],
        "pipeline_intents": [
            {
                "ordinal": intent.ordinal,
                "intent_id": intent.intent_id,
                "job_id": intent.job_id,
                "pipeline_id": intent.pipeline_id,
                "intent_type": intent.intent_type,
                "intent_kind": intent.intent_kind.value,
                "expected_status": (
                    None if intent.expected_status is None else intent.expected_status.value
                ),
                "target_status": intent.target_status.value,
                "expected_state_revision": intent.expected_state_revision,
                "idempotency_key": intent.idempotency_key,
                "replay_policy": intent.replay_policy.value,
                "max_attempts": intent.max_attempts,
                "status": intent.status,
                "payload": _payload_manifest(intent.payload),
            }
            for intent in pipelines
        ],
        "asset_references": {
            "protocol": assets.protocol,
            "reference_count": assets.reference_count,
            "unique_reference_count": assets.unique_reference_count,
            "projection_sha256": assets.projection_sha256.hex(),
        },
        "inbox": None if inbox is None else {
            "scope": inbox.scope,
            "inbox_key": inbox.inbox_key,
            "actor_id": inbox.actor_id,
            "claim_owner_id": inbox.claim_owner_id,
            "claim_fence": inbox.claim_fence,
            "state_revision": inbox.state_revision,
            "outcome_kind": inbox.outcome_kind.value,
            "request_sha256": inbox.request_sha256.hex(),
            "expected_status": inbox.expected_status,
            "target_status": inbox.target_status,
            "request": _payload_manifest(inbox.request),
            "response": _payload_manifest(inbox.response),
        },
        "confirmation": None if confirmation is None else {
            "confirmation_id": confirmation.confirmation_id,
            "nonce_hmac": confirmation.nonce_hmac.hex(),
            "nonce_key_id": confirmation.nonce_key_id,
            "reservation_fence": confirmation.reservation_fence,
            "reservation_owner_id": confirmation.reservation_owner_id,
            "expected_revision": confirmation.expected_revision,
            "expected_request_sha256": (
                confirmation.expected_request_sha256.hex()
            ),
            "actor_id": confirmation.actor_id,
            "decision": confirmation.decision,
            "result_commit_id": commit_id,
            "expected_status": confirmation.expected_status,
            "target_status": confirmation.target_status,
            "resolution": _payload_manifest(confirmation.resolution),
        },
        "closure": {
            "operation_count": len(records),
            "operation_target_count": len(targets),
            "outbox_count": len(outbox),
            "durable_action_count": len(actions),
            "pipeline_intent_count": len(pipelines),
            "asset_reference_count": assets.reference_count,
            "auxiliary_payload_bytes": auxiliary_bytes,
            "operation_tail_hash": chain[-1].self_hash,
        },
    }
    invalid_manifest = False
    try:
        return encode_payload_bounded(
            manifest,
            MAX_ATOMIC_MANIFEST_BYTES,
            MAX_ATOMIC_MANIFEST_NODES,
        )
    except BusinessCodecError:
        invalid_manifest = True
    if invalid_manifest:
        _fail("atomic_commit_manifest_budget_exceeded")
    raise AssertionError("unreachable")


def _prepare(
    *,
    authority: AuthorityBinding,
    commit_id: str,
    trace_id: str,
    before: BeforeBusinessHead,
    after_snapshot: BusinessSnapshotSet,
    operations: Any,
    outbox: Any,
    durable_actions: Any,
    pipeline_intents: Any,
    inbox: Optional[InboxClaim],
    confirmation: Optional[ConfirmationConsumption],
) -> _PreparedPlan:
    preflight_auxiliary_bytes = _preflight_auxiliary_bytes(
        operations=operations,
        outbox=outbox,
        durable_actions=durable_actions,
        pipeline_intents=pipeline_intents,
        inbox=inbox,
        confirmation=confirmation,
    )
    if type(authority) is not AuthorityBinding or type(before) is not BeforeBusinessHead:
        _fail("atomic_commit_binding_type_invalid")
    invalid_binding = False
    try:
        rebuilt_authority = AuthorityBinding(
            authority=authority.authority,
            authority_state=authority.authority_state,
            authority_epoch=authority.authority_epoch,
            source_admission_revision=authority.source_admission_revision,
            authority_revision=authority.authority_revision,
            control_revision=authority.control_revision,
            cutover_id=authority.cutover_id,
            runtime_generation=authority.runtime_generation,
            code_digest=authority.code_digest,
        )
        rebuilt_before = BeforeBusinessHead(
            project_id=before.project_id,
            generation=before.generation,
            document_id=before.document_id,
            document_version=before.document_version,
            project_revision=before.project_revision,
            head_revision=before.head_revision,
            expected_last_commit_id=before.expected_last_commit_id,
            business_root_sha256=before.business_root_sha256,
            operation_tail_hash=before.operation_tail_hash,
        )
    except BusinessTransactionContractError:
        invalid_binding = True
        rebuilt_authority = None
        rebuilt_before = None
    if invalid_binding:
        _fail("atomic_commit_binding_changed")
    if rebuilt_authority != authority or rebuilt_before != before:
        _fail("atomic_commit_binding_changed")
    _hex(commit_id, 32)
    _business_id(trace_id)
    if inbox is not None and type(inbox) is not InboxClaim:
        _fail("atomic_commit_inbox_type_invalid")
    if confirmation is not None and type(confirmation) is not ConfirmationConsumption:
        _fail("atomic_commit_confirmation_type_invalid")
    invalid_auxiliary = False
    try:
        if inbox is not None:
            verify_frozen_json_payload(inbox.request)
            verify_frozen_json_payload(inbox.response)
            rebuilt_inbox = InboxClaim(
                scope=inbox.scope,
                inbox_key=inbox.inbox_key,
                actor_id=inbox.actor_id,
                claim_owner_id=inbox.claim_owner_id,
                claim_fence=inbox.claim_fence,
                state_revision=inbox.state_revision,
                outcome_kind=inbox.outcome_kind,
                request_sha256=inbox.request_sha256,
                request=inbox.request,
                response=inbox.response,
            )
            if rebuilt_inbox != inbox:
                _fail("atomic_commit_inbox_changed")
        if confirmation is not None:
            verify_frozen_json_payload(confirmation.resolution)
            rebuilt_confirmation = ConfirmationConsumption(
                confirmation_id=confirmation.confirmation_id,
                nonce_hmac=confirmation.nonce_hmac,
                nonce_key_id=confirmation.nonce_key_id,
                reservation_fence=confirmation.reservation_fence,
                reservation_owner_id=confirmation.reservation_owner_id,
                expected_revision=confirmation.expected_revision,
                expected_request_sha256=confirmation.expected_request_sha256,
                actor_id=confirmation.actor_id,
                resolution=confirmation.resolution,
                decision=confirmation.decision,
            )
            if rebuilt_confirmation != confirmation:
                _fail("atomic_commit_confirmation_changed")
    except BusinessTransactionContractError:
        invalid_auxiliary = True
    if invalid_auxiliary:
        _fail("atomic_commit_auxiliary_binding_changed")
    if inbox is not None and inbox.outcome_kind.value != "commit":
        _fail("atomic_commit_inbox_outcome_invalid")
    if (
        inbox is not None
        and confirmation is not None
        and inbox.actor_id != confirmation.actor_id
    ):
        _fail("atomic_commit_confirmation_actor_mismatch")
    invalid_snapshot = False
    try:
        snapshot = verify_business_snapshot(after_snapshot)
    except BusinessSnapshotError:
        invalid_snapshot = True
        snapshot = None
    if invalid_snapshot:
        _fail("atomic_commit_snapshot_invalid")
    assert snapshot is not None
    if (
        before.project_id != snapshot.project.project_id
        or before.generation != snapshot.project.generation
        or before.document_id != snapshot.document.document_id
        or before.document_version >= snapshot.document.document_version
        or snapshot.document.document_version != before.document_version + 1
    ):
        _fail("atomic_commit_before_after_mismatch")
    records = _canonical_records(
        operations,
        trace_id=trace_id,
        document_id=snapshot.document.document_id,
    )
    invalid_chain = False
    try:
        chain = build_frozen_operation_chain(
            records,
            first_prev_hash=before.operation_tail_hash,
        )
    except OperationRecordCodecError:
        invalid_chain = True
        chain = None
    if invalid_chain:
        _fail("atomic_commit_operation_chain_invalid")
    assert chain is not None
    targets = _canonical_operation_targets(records)
    target_digest = _component_digest(
        b"ze.atomic-business-commit.operation-targets.v1",
        tuple(
            _component_part(
                {
                    "operation_id": target.operation_id,
                    "ordinal": target.ordinal,
                    "entity_type": target.entity_type,
                    "entity_id": target.entity_id,
                    "field": target.field,
                    "patch_kind": target.patch_kind,
                }
            )
            for target in targets
        ),
    )
    events = _canonical_outbox(outbox, records)
    actions = _canonical_actions(durable_actions)
    pipelines = _canonical_pipelines(pipeline_intents)
    invalid_assets = False
    try:
        assets = project_asset_references(snapshot)
        verify_asset_reference_projection(assets)
    except AssetReferenceProjectionError:
        invalid_assets = True
        assets = None
    if invalid_assets:
        _fail("atomic_commit_asset_projection_invalid")
    assert assets is not None
    candidate = CandidateBusinessHead(
        project_id=snapshot.project.project_id,
        generation=snapshot.project.generation,
        document_id=snapshot.document.document_id,
        document_version=snapshot.document.document_version,
        project_revision=before.project_revision + 1,
        head_revision=before.head_revision + 1,
        authority_revision=authority.authority_revision + 1,
        commit_id=commit_id,
        business_root_sha256=snapshot.project.snapshot_sha256,
        operation_tail_hash=chain[-1].self_hash,
    )
    auxiliary_bytes = sum(len(record.body_bytes) for record in records)
    auxiliary_bytes += sum(len(event.payload.payload_bytes) for event in events)
    auxiliary_bytes += sum(len(action.payload.payload_bytes) for action in actions)
    auxiliary_bytes += sum(len(intent.payload.payload_bytes) for intent in pipelines)
    if inbox is not None:
        auxiliary_bytes += len(inbox.request.payload_bytes)
        auxiliary_bytes += len(inbox.response.payload_bytes)
    if confirmation is not None:
        auxiliary_bytes += len(confirmation.resolution.payload_bytes)
    if auxiliary_bytes != preflight_auxiliary_bytes:
        _fail("atomic_commit_auxiliary_carrier_changed")
    if auxiliary_bytes > MAX_ATOMIC_TOTAL_AUXILIARY_BYTES:
        _fail("atomic_commit_auxiliary_budget_exceeded")
    manifest_bytes = _build_manifest(
        authority=authority,
        commit_id=commit_id,
        trace_id=trace_id,
        before=before,
        snapshot=snapshot,
        records=records,
        chain=chain,
        targets=targets,
        target_digest=target_digest,
        outbox=events,
        actions=actions,
        pipelines=pipelines,
        assets=assets,
        inbox=inbox,
        confirmation=confirmation,
        auxiliary_bytes=auxiliary_bytes,
    )
    operation_digest = _component_digest(
        b"ze.atomic-business-commit.operations.v1",
        tuple(
            record.body_sha256
            + bytes.fromhex(entry.prev_hash)
            + bytes.fromhex(entry.self_hash)
            for record, entry in zip(records, chain)
        ),
    )
    outbox_digest = _component_digest(
        b"ze.atomic-business-commit.outbox.v1",
        tuple(
            _component_part(
                {
                    "ordinal": event.ordinal,
                    "event_id": event.event_id,
                    "operation_id": event.operation_id,
                    "topic": event.topic,
                    "partition_key": event.partition_key,
                    "status": event.status,
                    "payload_sha256": event.payload.payload_sha256.hex(),
                }
            )
            for event in events
        ),
    )
    action_digest = _component_digest(
        b"ze.atomic-business-commit.actions.v1",
        tuple(
            _component_part(
                {
                    "ordinal": action.ordinal,
                    "action_id": action.action_id,
                    "action_type": action.action_type,
                    "idempotency_key": action.idempotency_key,
                    "idempotency_scope": action.idempotency_scope,
                    "idempotency_policy": action.idempotency_policy.value,
                    "replay_policy": action.replay_policy.value,
                    "max_attempts": action.max_attempts,
                    "status": action.status,
                    "payload_sha256": action.payload.payload_sha256.hex(),
                }
            )
            for action in actions
        ),
    )
    pipeline_digest = _component_digest(
        b"ze.atomic-business-commit.pipelines.v1",
        tuple(
            _component_part(
                {
                    "ordinal": intent.ordinal,
                    "intent_id": intent.intent_id,
                    "job_id": intent.job_id,
                    "pipeline_id": intent.pipeline_id,
                    "intent_type": intent.intent_type,
                    "intent_kind": intent.intent_kind.value,
                    "expected_status": (
                        None
                        if intent.expected_status is None
                        else intent.expected_status.value
                    ),
                    "target_status": intent.target_status.value,
                    "expected_state_revision": intent.expected_state_revision,
                    "idempotency_key": intent.idempotency_key,
                    "replay_policy": intent.replay_policy.value,
                    "max_attempts": intent.max_attempts,
                    "status": intent.status,
                    "payload_sha256": intent.payload.payload_sha256.hex(),
                }
            )
            for intent in pipelines
        ),
    )
    inbox_digest = _component_digest(
        b"ze.atomic-business-commit.inbox.v1",
        () if inbox is None else (_component_part({
            "scope": inbox.scope,
            "inbox_key": inbox.inbox_key,
            "actor_id": inbox.actor_id,
            "claim_owner_id": inbox.claim_owner_id,
            "claim_fence": inbox.claim_fence,
            "state_revision": inbox.state_revision,
            "outcome_kind": inbox.outcome_kind.value,
            "request_sha256": inbox.request_sha256.hex(),
            "response_sha256": inbox.response.payload_sha256.hex(),
            "expected_status": inbox.expected_status,
            "target_status": inbox.target_status,
        }),),
    )
    confirmation_digest = _component_digest(
        b"ze.atomic-business-commit.confirmation.v1",
        () if confirmation is None else (_component_part({
            "confirmation_id": confirmation.confirmation_id,
            "nonce_hmac": confirmation.nonce_hmac.hex(),
            "nonce_key_id": confirmation.nonce_key_id,
            "reservation_fence": confirmation.reservation_fence,
            "reservation_owner_id": confirmation.reservation_owner_id,
            "expected_revision": confirmation.expected_revision,
            "expected_request_sha256": (
                confirmation.expected_request_sha256.hex()
            ),
            "actor_id": confirmation.actor_id,
            "decision": confirmation.decision,
            "expected_status": confirmation.expected_status,
            "target_status": confirmation.target_status,
            "result_commit_id": commit_id,
            "resolution": _payload_manifest(confirmation.resolution),
        }),),
    )
    closure = AtomicCommitClosure(
        before_repository_revision=authority.authority_revision,
        after_repository_revision=authority.authority_revision + 1,
        before_project_revision=before.project_revision,
        after_project_revision=before.project_revision + 1,
        before_head_revision=before.head_revision,
        after_head_revision=before.head_revision + 1,
        operation_count=len(records),
        operation_target_count=len(targets),
        outbox_count=len(events),
        durable_action_count=len(actions),
        pipeline_intent_count=len(pipelines),
        asset_reference_count=assets.reference_count,
        auxiliary_payload_bytes=auxiliary_bytes,
        operation_tail_hash=chain[-1].self_hash,
        after_snapshot_sha256=snapshot.project.snapshot_sha256,
        operations_sha256=operation_digest,
        operation_targets_sha256=target_digest,
        outbox_sha256=outbox_digest,
        durable_actions_sha256=action_digest,
        pipeline_intents_sha256=pipeline_digest,
        asset_references_sha256=assets.projection_sha256,
        inbox_sha256=inbox_digest,
        confirmation_sha256=confirmation_digest,
        manifest_bytes=manifest_bytes,
        manifest_sha256=_manifest_digest(manifest_bytes),
    )
    return _PreparedPlan(
        snapshot=snapshot,
        records=records,
        chain=chain,
        targets=targets,
        outbox=events,
        actions=actions,
        pipelines=pipelines,
        assets=assets,
        candidate=candidate,
        closure=closure,
    )


def _issue_atomic_commit_plan(
    *,
    authority: AuthorityBinding,
    commit_id: str,
    trace_id: str,
    before: BeforeBusinessHead,
    after_snapshot: BusinessSnapshotSet,
    operations: Tuple[FrozenOperationRecord, ...],
    outbox: Tuple[OutboxEventIntent, ...],
    durable_actions: Tuple[DurableActionIntent, ...] = (),
    pipeline_intents: Tuple[PipelineIntent, ...] = (),
    inbox: Optional[InboxClaim] = None,
    confirmation: Optional[ConfirmationConsumption] = None,
) -> AtomicCommitPlan:
    """Private typed issuer; no raw dict or callable can enter the plan."""
    prepared = _prepare(
        authority=authority,
        commit_id=commit_id,
        trace_id=trace_id,
        before=before,
        after_snapshot=after_snapshot,
        operations=operations,
        outbox=outbox,
        durable_actions=durable_actions,
        pipeline_intents=pipeline_intents,
        inbox=inbox,
        confirmation=confirmation,
    )
    return AtomicCommitPlan(
        authority,
        commit_id,
        trace_id,
        before,
        prepared,
        inbox,
        confirmation,
        _issuer=_PLAN_ISSUER,
    )


def _require_atomic_commit_plan(value: Any) -> AtomicCommitPlan:
    """Rebuild every closure before a writer may trust an issued plan."""
    if (
        type(value) is not AtomicCommitPlan
        or getattr(value, "_issuer", None) is not _PLAN_ISSUER
        or getattr(value, "protocol", None) != ATOMIC_COMMIT_PROTOCOL
        or getattr(value, "status", None) != ATOMIC_COMMIT_STATUS_PREPARED
    ):
        _fail("atomic_commit_plan_invalid")
    prepared = _prepare(
        authority=value.authority,
        commit_id=value.commit_id,
        trace_id=value.trace_id,
        before=value.before,
        after_snapshot=value.after_snapshot,
        operations=value.operations,
        outbox=value.outbox,
        durable_actions=value.durable_actions,
        pipeline_intents=value.pipeline_intents,
        inbox=value.inbox,
        confirmation=value.confirmation,
    )
    if (
        prepared.snapshot != value.after_snapshot
        or prepared.records != value.operations
        or prepared.chain != value.operation_chain
        or prepared.targets != value.operation_targets
        or prepared.outbox != value.outbox
        or prepared.actions != value.durable_actions
        or prepared.pipelines != value.pipeline_intents
        or prepared.assets != value.asset_references
        or prepared.candidate != value.candidate
        or prepared.closure != value.closure
        or not hmac.compare_digest(
            prepared.closure.manifest_sha256,
            value.closure.manifest_sha256,
        )
    ):
        _fail("atomic_commit_plan_changed")
    return value


__all__ = [
    "ATOMIC_COMMIT_MANIFEST_HASH_DOMAIN",
    "AtomicCommitClosure",
    "AtomicCommitPlan",
    "BusinessTransactionIntegrityError",
]
