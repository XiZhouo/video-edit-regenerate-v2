"""Sealed Pipeline v2 construction and durable commit attestation.

This module is deliberately private.  It can create authority-bearing v2
Operations only while consuming an active command-root snapshot, proves the
prospective *complete* OpLog before storage, and issues a process-local
attestation only after the same store exposes the exact durable fresh head.
Neither a reduction projection nor an attestation is an execution grant.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
import sys
import threading
import uuid
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_CORE_DIR = Path(__file__).resolve().parent.parent
_OPERATION_DIR = _CORE_DIR / "03-commands-operations" / "02-operation"
_COMMAND_DIR = _CORE_DIR / "03-commands-operations" / "01-command"
_PERSISTENCE_DIR = _CORE_DIR / "02-data-flow" / "07-persistence"
for _directory in (_OPERATION_DIR, _COMMAND_DIR, _PERSISTENCE_DIR):
    if str(_directory) not in sys.path:
        sys.path.insert(0, str(_directory))

from operation import (  # noqa: E402
    OpLog,
    Operation,
    _PIPELINE_AUTHORITY_APPEND_HOOK_CAPABILITY,
    _PIPELINE_AUTHORITY_OPERATION_CAPABILITY,
    _install_pipeline_authority_append_hooks,
    _new_reserved_pipeline_authority_operation,
)
from operation_record_codec import (  # noqa: E402
    OperationRecordCodecError,
    _pipeline_operation_schema,
    encode_operation_chain_entry,
    freeze_operation_record,
)
from file_store import FileCommitStore  # noqa: E402
from pipeline_authority_codec import (  # noqa: E402
    PIPELINE_CONTROL_SCHEMA,
    PIPELINE_START_SCHEMA,
    PipelineAuthorityCodecError,
    build_pipeline_control_descriptor,
    build_pipeline_start_descriptor,
    parse_pipeline_control,
    parse_pipeline_start,
)
from pipeline_authority_reducer import (  # noqa: E402
    PipelineAuthorityReductionError,
    ReducedPipelineAuthority,
    reduce_fresh_pipeline_history,
)


_ACTION_COMMAND_IDS = {
    "pause": "pipeline.pause",
    "interrupt": "pipeline.interrupt",
    "cancel": "pipeline.cancel",
    "discard": "pipeline.discard",
    "resume": "pipeline.resume",
    "retry_failed": "pipeline.retry_failed",
    "rerun_step": "pipeline.rerun_step",
    "run_from_step": "pipeline.run_from_step",
    "set_breakpoint": "pipeline.set_breakpoint",
}
_V2_SCHEMAS = frozenset((PIPELINE_START_SCHEMA, PIPELINE_CONTROL_SCHEMA))
_TOKEN_CAPABILITY = object()
_LOCK = threading.RLock()
_ATTESTATION_BINDING_SECRET = os.urandom(32)


def _copy_fact(value: Any) -> Any:
    if value is None or type(value) in (bool, int, float, str):
        return value
    if type(value) in (list, tuple):
        return [_copy_fact(item) for item in value]
    if type(value) is dict and all(type(key) is str for key in value):
        return {key: _copy_fact(item) for key, item in value.items()}
    raise TypeError("commit facts must contain only JSON-like primitive values")


class PipelineAuthorityCommitError(RuntimeError):
    """Stable payload-safe failure at the Pipeline commit authority boundary."""

    def __init__(
        self,
        code: str,
        reason: str,
        message: str,
        *,
        outcome_unknown: bool = False,
        durability_phase: str = "preflight",
    ) -> None:
        if type(code) is not str or not code:
            raise TypeError("code must be a non-empty exact string")
        if type(reason) is not str or not reason:
            raise TypeError("reason must be a non-empty exact string")
        if type(message) is not str or not message:
            raise TypeError("message must be a non-empty exact string")
        if type(outcome_unknown) is not bool:
            raise TypeError("outcome_unknown must be boolean")
        if type(durability_phase) is not str or not durability_phase:
            raise TypeError("durability_phase must be a non-empty exact string")
        super().__init__(message)
        self.code = code
        self.reason = reason
        self.message = message
        self.outcome_unknown = outcome_unknown
        self.durability_phase = durability_phase
        self.commit_facts: dict[str, Any] = {}
        self.context: dict[str, Any] = {
            "reason": reason,
            "outcome_unknown": outcome_unknown,
            "durability_phase": durability_phase,
        }

    def with_commit_facts(self, facts: dict[str, Any]) -> "PipelineAuthorityCommitError":
        if type(facts) is not dict or any(type(key) is not str for key in facts):
            raise TypeError("commit facts must be a string-keyed dict")
        copied = _copy_fact(facts)
        self.commit_facts = copied
        self.context["commit_facts"] = copy.deepcopy(copied)
        return self


def _error(
    code: str,
    reason: str,
    message: str,
    *,
    post_durable: bool = False,
) -> PipelineAuthorityCommitError:
    return PipelineAuthorityCommitError(
        code,
        reason,
        message,
        outcome_unknown=post_durable,
        durability_phase="post_write_verification" if post_durable else "preflight",
    )


class _OpaqueToken:
    __slots__ = ("__weakref__",)

    def __new__(cls, *, _capability: object | None = None):
        if _capability is not _TOKEN_CAPABILITY:
            raise TypeError(f"{cls.__name__} is framework-issued")
        return super().__new__(cls)

    def __copy__(self):
        raise TypeError(f"{type(self).__name__} cannot be copied")

    def __deepcopy__(self, _memo: Any):
        raise TypeError(f"{type(self).__name__} cannot be copied")

    def __reduce_ex__(self, _protocol: int):
        raise TypeError(f"{type(self).__name__} cannot be serialized")

    def __reduce__(self):
        raise TypeError(f"{type(self).__name__} cannot be serialized")


class PipelineAuthorityCommitPermit(_OpaqueToken):
    """One exact prospective full-history proof; never an execution grant."""

    @property
    def expected_head_count(self) -> int:
        return _permit_entry(self).facts.expected_head_count

    @property
    def expected_head_hash(self) -> str:
        return _permit_entry(self).facts.expected_head_hash


class PipelineAuthorityAttestation(_OpaqueToken):
    """Exact process-local proof of one committed, durable, fresh reduction."""

    @property
    def document_id(self) -> str:
        return _attestation_facts(self).reduction.document_id

    @property
    def run_id(self) -> str:
        return _attestation_facts(self).reduction.run_id

    @property
    def pipeline_id(self) -> str:
        return _attestation_facts(self).reduction.pipeline_id

    @property
    def generation(self) -> int:
        return _attestation_facts(self).reduction.generation

    @property
    def state(self) -> str:
        return _attestation_facts(self).reduction.state

    @property
    def terminal(self) -> bool:
        return _attestation_facts(self).reduction.terminal

    @property
    def reconcile_required(self) -> bool:
        return _attestation_facts(self).reduction.reconcile_required

    @property
    def history_head_count(self) -> int:
        return _attestation_facts(self).reduction.history_head_count

    @property
    def history_head_hash(self) -> str:
        return _attestation_facts(self).reduction.history_head_hash


@dataclass
class _OperationSeal:
    reference: Any
    document: Any
    issuer_pid: int
    document_binding_hmac_sha256: bytes
    schema: str
    operation_id: str
    body_sha256: bytes
    claim_state: str = "available"
    permit_identity: int | None = None


@dataclass(frozen=True)
class _ReductionProof:
    key: tuple[str, str, str]
    reduction: ReducedPipelineAuthority
    fingerprint: tuple[Any, ...]


@dataclass(frozen=True)
class _PermitFacts:
    store: Any
    op_log: Any
    document: Any
    operations: tuple[Operation, ...]
    expected_head_count: int
    expected_head_hash: str
    reductions: tuple[_ReductionProof, ...]


@dataclass
class _PermitEntry:
    identity: int
    reference: Any
    facts: _PermitFacts
    append_states: list[str]
    state: str = "prepared"
    append_cursor: int = 0
    storage_may_have_changed: bool = False


@dataclass(frozen=True)
class _AppendBinding:
    reference: Any
    permit_identity: int
    position: int
    authority_v2: bool


@dataclass(frozen=True)
class _AppendClaim:
    permit_identity: int
    position: int
    op_log: Any
    operation: Operation


@dataclass(frozen=True)
class _AttestationFacts:
    store: Any
    document: Any
    reduction: ReducedPipelineAuthority


@dataclass(frozen=True)
class _AttestationEntry:
    reference: Any
    active_key: tuple[int, str, str, str]
    facts: _AttestationFacts
    integrity_hmac_sha256: bytes


_OPERATION_SEALS: dict[int, _OperationSeal] = {}
_PERMITS: dict[int, _PermitEntry] = {}
_APPEND_BINDINGS: dict[int, _AppendBinding] = {}
_ACTIVE_LOG_PERMITS: dict[int, int] = {}
_ATTESTATIONS: dict[int, _AttestationEntry] = {}
_ACTIVE_ATTESTATIONS: dict[tuple[int, str, str, str], PipelineAuthorityAttestation] = {}


def _reset_after_fork() -> None:
    """A child process must re-prove every process-local seal and attestation."""

    global _LOCK, _ATTESTATION_BINDING_SECRET
    _LOCK = threading.RLock()
    _ATTESTATION_BINDING_SECRET = os.urandom(32)
    _OPERATION_SEALS.clear()
    _PERMITS.clear()
    _APPEND_BINDINGS.clear()
    _ACTIVE_LOG_PERMITS.clear()
    _ATTESTATIONS.clear()
    _ACTIVE_ATTESTATIONS.clear()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_after_fork)


def _cleanup(ledger: dict[int, Any], identity: int, reference: Any) -> None:
    with _LOCK:
        current = ledger.get(identity)
        current_reference = current[0] if type(current) is tuple else getattr(current, "reference", None)
        if current_reference is reference:
            ledger.pop(identity, None)


def _operation_schema(operation: Operation) -> str | None:
    pipeline = object.__getattribute__(operation, "pipeline")
    try:
        return _pipeline_operation_schema(pipeline)
    except OperationRecordCodecError as exc:
        raise _error(
            "pipeline_authority_seal_invalid",
            "pipeline_authority_descriptor_changed",
            "Pipeline authority descriptor cannot be classified safely",
        ) from exc


def _revoke_active_attestations_locked(
    store: Any,
    reductions: tuple[_ReductionProof, ...],
) -> None:
    for proof in reductions:
        key = (id(store), proof.key[0], proof.key[1], proof.key[2])
        previous = _ACTIVE_ATTESTATIONS.pop(key, None)
        if previous is not None:
            _ATTESTATIONS.pop(id(previous), None)


def _remove_active_attestation_identity_locked(
    attestation: PipelineAuthorityAttestation,
) -> None:
    """Remove every reverse index that still points at one exact proof."""

    for key, current in tuple(_ACTIVE_ATTESTATIONS.items()):
        if current is attestation:
            _ACTIVE_ATTESTATIONS.pop(key, None)


def _set_seal_claims_locked(entry: _PermitEntry, *, release: bool) -> None:
    for operation in entry.facts.operations:
        seal = _OPERATION_SEALS.get(id(operation))
        if seal is None or seal.reference() is not operation:
            continue
        if seal.permit_identity != entry.identity:
            continue
        if release and seal.claim_state == "claimed":
            seal.claim_state = "available"
            seal.permit_identity = None
        else:
            seal.claim_state = "spent"
            seal.permit_identity = None


def _revoke_append_authorizations_locked(
    entry: _PermitEntry,
    *,
    release_seals: bool,
) -> None:
    for operation in entry.facts.operations:
        binding = _APPEND_BINDINGS.get(id(operation))
        if (
            binding is not None
            and binding.reference() is operation
            and binding.permit_identity == entry.identity
        ):
            _APPEND_BINDINGS.pop(id(operation), None)
    if _ACTIVE_LOG_PERMITS.get(id(entry.facts.op_log)) == entry.identity:
        _ACTIVE_LOG_PERMITS.pop(id(entry.facts.op_log), None)
    _set_seal_claims_locked(entry, release=release_seals)


def _cleanup_permit(identity: int, reference: Any) -> None:
    with _LOCK:
        entry = _PERMITS.get(identity)
        if entry is None or entry.reference is not reference:
            return
        clean = (
            not entry.storage_may_have_changed
            and all(state == "prepared" for state in entry.append_states)
        )
        _revoke_append_authorizations_locked(entry, release_seals=clean)
        _PERMITS.pop(identity, None)


def _operation_document_binding_hmac(operation: Operation, document: Any) -> bytes:
    payload = f"{os.getpid()}:{id(operation)}:{id(document)}".encode(
        "ascii",
        "strict",
    )
    return hmac.new(
        _ATTESTATION_BINDING_SECRET,
        payload,
        hashlib.sha256,
    ).digest()


def _seal_operation(operation: Operation, *, document: Any) -> Operation:
    record = freeze_operation_record(operation.to_dict())
    pipeline = object.__getattribute__(operation, "pipeline")
    schema = _pipeline_operation_schema(pipeline)
    if schema not in _V2_SCHEMAS:
        raise _error(
            "pipeline_authority_seal_invalid",
            "pipeline_authority_schema_invalid",
            "Pipeline authority Operation does not contain a v2 descriptor",
        )
    identity = id(operation)
    reference = weakref.ref(
        operation,
        lambda ref: _cleanup(_OPERATION_SEALS, identity, ref),
    )
    with _LOCK:
        _OPERATION_SEALS[identity] = _OperationSeal(
            reference,
            document,
            os.getpid(),
            _operation_document_binding_hmac(operation, document),
            schema,
            operation.id,
            bytes(record.body_sha256),
        )
    return operation


def _require_sealed(operation: Operation, *, document: Any = None) -> None:
    with _LOCK:
        seal = _OPERATION_SEALS.get(id(operation))
    if seal is None or seal.reference() is not operation:
        raise _error(
            "pipeline_authority_seal_required",
            "pipeline_authority_operation_unsealed",
            "Pipeline v2 Operation was not issued by the sealed factory",
        )
    try:
        document_binding = _operation_document_binding_hmac(
            operation,
            seal.document,
        )
    except (AttributeError, TypeError, ValueError, OverflowError):
        document_binding = b""
    if (
        seal.issuer_pid != os.getpid()
        or not hmac.compare_digest(
            document_binding,
            seal.document_binding_hmac_sha256,
        )
    ):
        raise _error(
            "pipeline_authority_seal_invalid",
            "pipeline_authority_operation_changed",
            "Sealed Pipeline authority Operation binding changed after issuance",
        )
    try:
        record = freeze_operation_record(operation.to_dict())
    except (OperationRecordCodecError, TypeError, ValueError) as exc:
        raise _error(
            "pipeline_authority_seal_invalid",
            "pipeline_authority_operation_changed",
            "Sealed Pipeline authority Operation changed after issuance",
        ) from exc
    pipeline = object.__getattribute__(operation, "pipeline")
    try:
        schema = _pipeline_operation_schema(pipeline)
    except OperationRecordCodecError as exc:
        raise _error(
            "pipeline_authority_seal_invalid",
            "pipeline_authority_operation_changed",
            "Sealed Pipeline authority Operation changed after issuance",
        ) from exc
    if (
        operation.id != seal.operation_id
        or schema != seal.schema
        or not hmac.compare_digest(record.body_sha256, seal.body_sha256)
    ):
        raise _error(
            "pipeline_authority_seal_invalid",
            "pipeline_authority_operation_changed",
            "Sealed Pipeline authority Operation changed after issuance",
        )
    if document is not None and (
        seal.document is not document
        or seal.issuer_pid != os.getpid()
    ):
        raise _error(
            "pipeline_authority_commit_invalid",
            "pipeline_authority_document_binding_mismatch",
            "Pipeline authority Operation belongs to another exact Document",
        )


def _consume_root(
    root_authority: object,
    *,
    document: Any,
    command_id: str,
    run_id: str,
    pipeline_id: str,
    entity_types: tuple[str, ...],
) -> Any:
    try:
        from command import _consume_pipeline_authority_root

        document_id = getattr(document, "id", "")
        return _consume_pipeline_authority_root(
            root_authority,
            document=document,
            command_id=command_id,
            document_id=document_id,
            run_id=run_id,
            pipeline_id=pipeline_id,
            entity_types=entity_types,
        )
    except PipelineAuthorityCommitError:
        raise
    except Exception as exc:
        raise _error(
            "pipeline_authority_root_invalid",
            "pipeline_authority_root_verification_failed",
            "Pipeline authority issuance requires a live registered command root",
        ) from exc


def _descriptor_entity_types(
    work_list: dict[str, Any],
    creation_scopes: list[dict[str, Any]] | None,
) -> tuple[str, ...]:
    try:
        item_types = [item["entity_type"] for item in work_list["items"]]
        creation_types = [scope["entity_type"] for scope in (creation_scopes or [])]
    except (KeyError, TypeError) as exc:
        raise _error(
            "pipeline_start_identity_mismatch",
            "pipeline_start_entity_types_invalid",
            "Pipeline Start entity types cannot be derived",
        ) from exc
    if any(type(value) is not str or not value for value in (*item_types, *creation_types)):
        raise _error(
            "pipeline_start_identity_mismatch",
            "pipeline_start_entity_types_invalid",
            "Pipeline Start entity types cannot be derived",
        )
    return tuple(sorted(set((*item_types, *creation_types))))


def issue_sealed_pipeline_start_operation(
    *,
    _root_authority: object,
    document: Any,
    run_id: str,
    pipeline_id: str,
    start_command_id: str,
    initial_operations: tuple[Operation, ...],
    work_list: dict[str, Any],
    stages: list[dict[str, Any]],
    placeholder_bindings: list[dict[str, Any]],
    concurrency: int,
    max_retries: int,
    parameters: dict[str, Any],
    worker_actor: dict[str, Any],
    worker_source: str,
    targets: list[dict[str, Any]],
    creation_scopes: list[dict[str, Any]] | None = None,
    group_id: str = "",
    trace_id: str = "",
    session_id: str = "",
) -> Operation:
    if type(initial_operations) is not tuple or any(
        type(operation) is not Operation for operation in initial_operations
    ):
        raise TypeError("initial_operations must be an exact tuple of Operations")
    root = _consume_root(
        _root_authority,
        document=document,
        command_id=start_command_id,
        run_id=run_id,
        pipeline_id=pipeline_id,
        entity_types=_descriptor_entity_types(work_list, creation_scopes),
    )
    operation_id = uuid.uuid4().hex
    try:
        descriptor = build_pipeline_start_descriptor(
            document_id=root.document_id,
            run_id=run_id,
            pipeline_id=pipeline_id,
            start_command_id=root.command_id,
            start_operation_id=operation_id,
            work_list=work_list,
            stages=stages,
            placeholder_bindings=placeholder_bindings,
            initial_operation_ids=[operation_id, *(op.id for op in initial_operations)],
            concurrency=concurrency,
            max_retries=max_retries,
            parameters=parameters,
            root_command_digest=root.root_command_digest,
            document_policy=root.document_policy,
            worker_actor=worker_actor,
            worker_source=worker_source,
            targets=targets,
            creation_scopes=creation_scopes,
        )
        operation = _new_reserved_pipeline_authority_operation(
            operation_id=operation_id,
            command_id=root.command_id,
            source=root.source,
            actor=root.actor,
            document_id=root.document_id,
            pipeline=descriptor,
            group_id=group_id,
            trace_id=trace_id,
            session_id=session_id,
            _capability=_PIPELINE_AUTHORITY_OPERATION_CAPABILITY,
        )
        return _seal_operation(operation, document=document)
    except PipelineAuthorityCommitError:
        raise
    except (PipelineAuthorityCodecError, OperationRecordCodecError, TypeError, ValueError) as exc:
        raise _error(
            "pipeline_start_identity_mismatch",
            "pipeline_start_sealed_construction_failed",
            "Pipeline Start cannot be sealed from the verified command root",
        ) from exc


def issue_sealed_pipeline_control_operation(
    *,
    _root_authority: object,
    document: Any,
    run_id: str,
    pipeline_id: str,
    start_operation_id: str,
    start_operation_hash: str,
    action: str,
    parameters: dict[str, Any],
    expected_generation: int,
    expected_state: str,
    group_id: str = "",
    trace_id: str = "",
    session_id: str = "",
) -> Operation:
    command_id = _ACTION_COMMAND_IDS.get(action)
    if command_id is None:
        raise _error(
            "pipeline_control_invalid",
            "pipeline_control_action_unknown",
            "Pipeline Control action is unsupported",
        )
    root = _consume_root(
        _root_authority,
        document=document,
        command_id=command_id,
        run_id=run_id,
        pipeline_id=pipeline_id,
        entity_types=(),
    )
    operation_id = uuid.uuid4().hex
    try:
        descriptor = build_pipeline_control_descriptor(
            document_id=root.document_id,
            run_id=run_id,
            pipeline_id=pipeline_id,
            control_operation_id=operation_id,
            start_operation_id=start_operation_id,
            start_operation_hash=start_operation_hash,
            action=action,
            parameters=parameters,
            expected_generation=expected_generation,
            expected_state=expected_state,
        )
        operation = _new_reserved_pipeline_authority_operation(
            operation_id=operation_id,
            command_id=root.command_id,
            source=root.source,
            actor=root.actor,
            document_id=root.document_id,
            pipeline=descriptor,
            group_id=group_id,
            trace_id=trace_id,
            session_id=session_id,
            _capability=_PIPELINE_AUTHORITY_OPERATION_CAPABILITY,
        )
        return _seal_operation(operation, document=document)
    except PipelineAuthorityCommitError:
        raise
    except (PipelineAuthorityCodecError, OperationRecordCodecError, TypeError, ValueError) as exc:
        raise _error(
            "pipeline_control_invalid",
            "pipeline_control_sealed_construction_failed",
            "Pipeline Control cannot be sealed from the verified command root",
        ) from exc


def _fresh_snapshot(store: Any, *, post_durable: bool) -> tuple[Any, tuple[dict[str, Any], ...], int, str]:
    if type(store) is not FileCommitStore:
        raise _error(
            "pipeline_authority_unavailable",
            "pipeline_authority_store_invalid",
            "Pipeline authority requires the exact FileCommitStore runtime binding",
            post_durable=post_durable,
        )
    op_log = getattr(store, "op_log", None)
    if type(op_log) is not OpLog:
        raise _error(
            "pipeline_authority_unavailable",
            "pipeline_authority_oplog_invalid",
            "Pipeline authority requires the exact canonical OpLog runtime binding",
            post_durable=post_durable,
        )
    snapshot = getattr(op_log, "_canonical_history_snapshot", None)
    if not callable(snapshot):
        raise _error(
            "pipeline_authority_unavailable",
            "pipeline_authority_oplog_protocol_missing",
            "CommitStore has no fresh canonical OpLog snapshot",
            post_durable=post_durable,
        )
    try:
        rows, count, tail_hash = snapshot()
    except Exception as exc:
        raise _error(
            "pipeline_authority_unavailable",
            "pipeline_authority_fresh_scan_failed",
            "Fresh Pipeline authority history is unavailable",
            post_durable=post_durable,
        ) from exc
    if type(rows) is not tuple or type(count) is not int or type(tail_hash) is not str:
        raise _error(
            "pipeline_authority_unavailable",
            "pipeline_authority_fresh_snapshot_invalid",
            "Fresh Pipeline authority history returned an invalid head",
            post_durable=post_durable,
        )
    return op_log, rows, count, tail_hash


def _reduction_fingerprint(value: ReducedPipelineAuthority) -> tuple[Any, ...]:
    return (
        value.protocol, value.document_id, value.run_id, value.pipeline_id,
        value.start_command_id, value.start_operation_id,
        value.start_operation_hash, value.start_descriptor_digest,
        value.initial_operation_ids, value.root_actor, value.root_source,
        value.root_command_digest, value.document_policy_digest,
        value.worker_actor, value.worker_source, value.work_item_ids,
        tuple(
            (v.item_id, v.entity_type, v.entity_id)
            for v in value.work_item_bindings
        ),
        value.allowed_stage_ids,
        tuple((v.item_id, v.entity_type, v.entity_id, v.operation_id) for v in value.placeholder_bindings),
        tuple((v.entity_type, v.entity_id, v.fields, v.lifecycle) for v in value.delegated_targets),
        tuple((v.entity_type, v.fields, v.max_per_operation, v.max_per_run) for v in value.delegated_creation_scopes),
        value.active_breakpoint_stage_ids, value.generation, value.state,
        value.reconciliation_pending, value.terminal_action,
        value.last_control_operation_id, value.last_control_hash,
        value.control_operation_ids, value.history_head_count,
        value.history_head_hash,
    )


def _reduction_fingerprint_digest(value: ReducedPipelineAuthority) -> bytes:
    """Hash one strict scalar-only reduction snapshot for later rechecks.

    Frozen dataclasses protect normal assignment, but ``object.__setattr__``
    can still rewrite an object held by the same process.  An attestation must
    therefore bind the reduction values at installation time and verify them
    again on every private consumption, rather than trusting the container's
    ``frozen=True`` declaration as an authority boundary.
    """

    payload = json.dumps(
        _reduction_fingerprint(value),
        ensure_ascii=False,
        sort_keys=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8", "strict")
    return hashlib.sha256(payload).digest()


def _attestation_integrity_hmac(
    store: Any,
    document: Any,
    active_key: tuple[int, str, str, str],
    reduction_fingerprint_sha256: bytes,
) -> bytes:
    payload = json.dumps(
        (
            os.getpid(),
            id(store),
            id(document),
            active_key,
            reduction_fingerprint_sha256.hex(),
        ),
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8", "strict")
    return hmac.new(
        _ATTESTATION_BINDING_SECRET,
        payload,
        hashlib.sha256,
    ).digest()


def _authority_key(operation: Operation) -> tuple[str, str, str] | None:
    pipeline = object.__getattribute__(operation, "pipeline")
    if type(pipeline) is not dict:
        return None
    try:
        schema = _pipeline_operation_schema(pipeline)
    except OperationRecordCodecError as exc:
        raise _error(
            "pipeline_authority_seal_invalid",
            "pipeline_authority_descriptor_changed",
            "Pipeline authority descriptor cannot be classified safely",
        ) from exc
    if schema not in _V2_SCHEMAS:
        return None
    _require_sealed(operation)
    try:
        descriptor = (
            parse_pipeline_start(pipeline)
            if schema == PIPELINE_START_SCHEMA
            else parse_pipeline_control(pipeline)
        )
    except PipelineAuthorityCodecError as exc:
        raise _error(
            "pipeline_authority_seal_invalid",
            "pipeline_authority_descriptor_changed",
            "Sealed Pipeline authority descriptor cannot be revalidated",
        ) from exc
    return descriptor.document_id, descriptor.run_id, descriptor.pipeline_id


def _wrap_reduction_error(exc: PipelineAuthorityReductionError, *, post_durable: bool):
    return _error(exc.code, exc.reason, str(exc), post_durable=post_durable)


def _validate_start_commit_batch(operations: tuple[Operation, ...]) -> None:
    """Bind one newly-issued Start descriptor to this exact commit tuple.

    The complete-history reducer deliberately permits unrelated durable rows
    before and after a Start closure.  At the writer boundary, however, a new
    Start is a stronger atomic contract: it is the first Operation in this
    commit and its ``initial_operation_ids`` names every Operation in the
    commit, in order.  Without this check a caller could prepend or append an
    unsealed business Operation that the durable Start never attests.

    Batch 3 supports one Start per commit.  Control-only commits do not enter
    this rule; their legality remains owned by the complete-history reducer.
    """

    starts: list[tuple[int, Any]] = []
    for index, operation in enumerate(operations):
        pipeline = object.__getattribute__(operation, "pipeline")
        if type(pipeline) is not dict:
            continue
        try:
            schema = _pipeline_operation_schema(pipeline)
        except OperationRecordCodecError as exc:
            raise _error(
                "pipeline_authority_seal_invalid",
                "pipeline_authority_descriptor_changed",
                "Pipeline authority descriptor cannot be classified safely",
            ) from exc
        if schema != PIPELINE_START_SCHEMA:
            continue
        try:
            starts.append((index, parse_pipeline_start(pipeline)))
        except PipelineAuthorityCodecError as exc:
            raise _error(
                "pipeline_authority_seal_invalid",
                "pipeline_authority_descriptor_changed",
                "Sealed Pipeline Start descriptor cannot be revalidated",
            ) from exc

    if len(starts) > 1:
        raise _error(
            "pipeline_start_prefix_incomplete",
            "pipeline_start_multiple_in_commit",
            "A Pipeline authority commit may contain only one new Start",
        )
    if not starts:
        return

    start_index, start = starts[0]
    if start_index != 0:
        raise _error(
            "pipeline_start_prefix_incomplete",
            "pipeline_start_not_commit_head",
            "Pipeline Start must be the first Operation in its commit",
        )
    actual_operation_ids = tuple(operation.id for operation in operations)
    if start.initial_operation_ids != actual_operation_ids:
        raise _error(
            "pipeline_start_prefix_incomplete",
            "pipeline_start_commit_members_mismatch",
            "Pipeline Start initial Operations do not equal its exact commit tuple",
        )


def _register_append_authorizations(
    permit: PipelineAuthorityCommitPermit,
    facts: _PermitFacts,
) -> None:
    """Atomically bind one permit, exact OpLog, exact objects, and order."""

    identity = id(permit)
    reference = weakref.ref(permit, lambda ref: _cleanup_permit(identity, ref))
    bindings = tuple(
        _AppendBinding(
            weakref.ref(operation),
            identity,
            position,
            _operation_schema(operation) in _V2_SCHEMAS,
        )
        for position, operation in enumerate(facts.operations)
    )
    entry = _PermitEntry(
        identity,
        reference,
        facts,
        ["prepared"] * len(facts.operations),
    )

    with _LOCK:
        # A sealed authority object represents one root-issued durable fact.
        # Claim every v2 seal before exposing the permit; a second store or a
        # concurrent prepare cannot clone that fact into another history.
        for operation, binding in zip(facts.operations, bindings):
            if not binding.authority_v2:
                continue
            seal = _OPERATION_SEALS.get(id(operation))
            if seal is None or seal.reference() is not operation:
                raise _error(
                    "pipeline_authority_seal_required",
                    "pipeline_authority_operation_unsealed",
                    "Pipeline v2 Operation was not issued by the sealed factory",
                )
            if seal.claim_state == "claimed":
                raise _error(
                    "pipeline_authority_commit_invalid",
                    "pipeline_authority_seal_already_claimed",
                    "Pipeline authority Operation already belongs to another commit",
                )
            if seal.claim_state == "spent":
                raise _error(
                    "pipeline_authority_commit_invalid",
                    "pipeline_authority_seal_spent",
                    "Pipeline authority Operation was already consumed",
                )

        active_identity = _ACTIVE_LOG_PERMITS.get(id(facts.op_log))
        active_entry = _PERMITS.get(active_identity) if active_identity is not None else None
        if active_entry is None and active_identity is not None:
            _ACTIVE_LOG_PERMITS.pop(id(facts.op_log), None)
            active_identity = None
        if active_identity is not None:
            raise _error(
                "pipeline_authority_commit_invalid",
                "pipeline_authority_oplog_already_bound",
                "Pipeline authority OpLog already has an active commit permit",
            )

        for operation in facts.operations:
            existing = _APPEND_BINDINGS.get(id(operation))
            if existing is None:
                continue
            existing_entry = _PERMITS.get(existing.permit_identity)
            if existing.reference() is not operation or existing_entry is None:
                _APPEND_BINDINGS.pop(id(operation), None)
                continue
            raise _error(
                "pipeline_authority_commit_invalid",
                "pipeline_authority_operation_already_bound",
                "Operation already belongs to another Pipeline authority commit",
            )

        _PERMITS[identity] = entry
        _ACTIVE_LOG_PERMITS[id(facts.op_log)] = identity
        for operation, binding in zip(facts.operations, bindings):
            _APPEND_BINDINGS[id(operation)] = binding
            if binding.authority_v2:
                seal = _OPERATION_SEALS[id(operation)]
                seal.claim_state = "claimed"
                seal.permit_identity = identity


def _prepare_pipeline_authority_commit(
    store: Any,
    operations: tuple[Operation, ...],
    *,
    document: Any = None,
) -> PipelineAuthorityCommitPermit | None:
    """Prove all affected runs against one fresh prospective complete history."""

    if type(operations) is not tuple or not operations or any(
        type(operation) is not Operation for operation in operations
    ):
        raise TypeError("operations must be a non-empty exact tuple of Operations")
    if len({operation.id for operation in operations}) != len(operations):
        raise _error(
            "pipeline_authority_commit_invalid",
            "pipeline_authority_operation_id_duplicate",
            "Pipeline authority commit contains duplicate Operation ids",
        )
    keys: list[tuple[str, str, str]] = []
    for operation in operations:
        key = _authority_key(operation)
        if key is not None and key not in keys:
            keys.append(key)
    if not keys:
        return None  # v1 is permanently carrier-only and never creates a permit.

    document_id = getattr(document, "id", None)
    if (
        type(document_id) is not str
        or not document_id
        or any(key[0] != document_id for key in keys)
    ):
        raise _error(
            "pipeline_authority_commit_invalid",
            "pipeline_authority_document_binding_mismatch",
            "Pipeline authority commit does not bind its exact coordinator Document",
        )
    for operation in operations:
        if _operation_schema(operation) in _V2_SCHEMAS:
            _require_sealed(operation, document=document)

    _validate_start_commit_batch(operations)

    op_log, rows, count, tail_hash = _fresh_snapshot(store, post_durable=False)
    prospective = list(rows)
    previous_hash = tail_hash
    try:
        for operation in operations:
            entry, _line = encode_operation_chain_entry(previous_hash, operation.to_dict())
            prospective.append(entry)
            previous_hash = entry["self_hash"]
        head_count = count + len(operations)
        reductions = tuple(
            _ReductionProof(
                key,
                reduction := reduce_fresh_pipeline_history(
                    tuple(prospective),
                    head_count=head_count,
                    head_hash=previous_hash,
                    document_id=key[0],
                    run_id=key[1],
                    pipeline_id=key[2],
                ),
                _reduction_fingerprint(reduction),
            )
            for key in keys
        )
    except PipelineAuthorityReductionError as exc:
        raise _wrap_reduction_error(exc, post_durable=False) from exc
    except (OperationRecordCodecError, PipelineAuthorityCodecError, TypeError, ValueError) as exc:
        raise _error(
            "pipeline_authority_commit_invalid",
            "pipeline_authority_prospective_reduction_failed",
            "Prospective Pipeline authority history cannot be reduced",
        ) from exc

    permit = PipelineAuthorityCommitPermit(_capability=_TOKEN_CAPABILITY)
    facts = _PermitFacts(
        store,
        op_log,
        document,
        operations,
        head_count,
        previous_hash,
        reductions,
    )
    _register_append_authorizations(permit, facts)
    return permit


def prepare_pipeline_authority_commit(
    store: Any,
    operations: tuple[Operation, ...],
    *,
    document: Any = None,
) -> PipelineAuthorityCommitPermit | None:
    """Typed public-to-framework wrapper for the pre-store proof boundary."""

    try:
        return _prepare_pipeline_authority_commit(
            store,
            operations,
            document=document,
        )
    except PipelineAuthorityCommitError:
        raise
    except PipelineAuthorityReductionError as exc:
        raise _wrap_reduction_error(exc, post_durable=False) from exc
    except Exception as exc:
        raise _error(
            "pipeline_authority_commit_invalid",
            "pipeline_authority_prepare_unexpected",
            "Prospective Pipeline authority preparation failed unexpectedly",
        ) from exc


def _permit_entry(permit: PipelineAuthorityCommitPermit) -> _PermitEntry:
    if type(permit) is not PipelineAuthorityCommitPermit:
        raise _error(
            "pipeline_authority_commit_invalid",
            "pipeline_authority_permit_invalid",
            "Pipeline authority commit permit is invalid",
        )
    with _LOCK:
        entry = _PERMITS.get(id(permit))
    if entry is None or entry.reference() is not permit:
        raise _error(
            "pipeline_authority_commit_invalid",
            "pipeline_authority_permit_stale",
            "Pipeline authority commit permit is stale or foreign",
        )
    return entry


def _claim_pipeline_authority_append(
    op_log: Any,
    operation: Any,
) -> _AppendClaim | None:
    """Consume the next exact append authorization before writer I/O."""

    if type(operation) is not Operation:
        return None
    schema = _operation_schema(operation)
    with _LOCK:
        binding = _APPEND_BINDINGS.get(id(operation))
        if binding is not None and binding.reference() is not operation:
            _APPEND_BINDINGS.pop(id(operation), None)
            binding = None

        if binding is not None:
            entry = _PERMITS.get(binding.permit_identity)
            if entry is None or entry.reference() is None:
                _APPEND_BINDINGS.pop(id(operation), None)
                binding = None
            elif entry.facts.op_log is not op_log:
                raise _error(
                    "pipeline_authority_append_invalid",
                    "pipeline_authority_append_log_mismatch",
                    "Pipeline authority append belongs to another exact OpLog",
                )

        active_identity = _ACTIVE_LOG_PERMITS.get(id(op_log))
        active_entry = _PERMITS.get(active_identity) if active_identity is not None else None
        if active_entry is None and active_identity is not None:
            _ACTIVE_LOG_PERMITS.pop(id(op_log), None)
            active_identity = None

        authority_v2 = (
            binding.authority_v2 if binding is not None else schema in _V2_SCHEMAS
        )
        if authority_v2:
            _require_sealed(operation)

        if active_identity is None:
            if binding is not None:
                raise _error(
                    "pipeline_authority_append_invalid",
                    "pipeline_authority_append_permit_stale",
                    "Pipeline authority append permit is stale",
                )
            if authority_v2:
                raise _error(
                    "pipeline_authority_append_required",
                    "pipeline_authority_append_permit_missing",
                    "Pipeline v2 Operation requires an exact prepared append permit",
                )
            return None

        entry = active_entry
        assert entry is not None
        if entry.facts.op_log is not op_log:
            raise _error(
                "pipeline_authority_append_invalid",
                "pipeline_authority_append_log_mismatch",
                "Pipeline authority append belongs to another exact OpLog",
            )
        if entry.state != "prepared":
            raise _error(
                "pipeline_authority_append_invalid",
                "pipeline_authority_append_permit_consumed",
                "Pipeline authority append permit is no longer writable",
            )
        if binding is None or binding.permit_identity != entry.identity:
            raise _error(
                "pipeline_authority_append_invalid",
                "pipeline_authority_append_order_mismatch",
                "OpLog append does not match the active Pipeline commit order",
            )
        position = binding.position
        state = entry.append_states[position]
        if state != "prepared":
            raise _error(
                "pipeline_authority_append_invalid",
                "pipeline_authority_append_permit_consumed",
                "Pipeline authority append authorization was already consumed",
            )
        if (
            position != entry.append_cursor
            or position >= len(entry.facts.operations)
            or entry.facts.operations[position] is not operation
        ):
            raise _error(
                "pipeline_authority_append_invalid",
                "pipeline_authority_append_order_mismatch",
                "OpLog append does not match the active Pipeline commit order",
            )

        # One-shot consumption precedes all writer projection and I/O.  Abort
        # can poison but never restore this slot.
        entry.append_states[position] = "consumed"
        entry.append_cursor += 1
        if binding.authority_v2:
            seal = _OPERATION_SEALS.get(id(operation))
            if seal is not None and seal.reference() is operation:
                seal.claim_state = "spent"
                seal.permit_identity = None
        return _AppendClaim(entry.identity, position, op_log, operation)


def _complete_pipeline_authority_append(
    claim: Any,
    *,
    op_log: Any,
    operation: Any,
) -> None:
    """Post-write hook is deliberately infallible; durable head proves output."""

    del claim, op_log, operation


def _abort_pipeline_authority_append(
    claim: Any,
    *,
    op_log: Any,
    operation: Any,
) -> None:
    """Poison a claimed slot without masking the writer's original failure."""

    if (
        type(claim) is not _AppendClaim
        or claim.op_log is not op_log
        or claim.operation is not operation
    ):
        return
    with _LOCK:
        entry = _PERMITS.get(claim.permit_identity)
        if entry is None:
            return
        if (
            claim.position < len(entry.append_states)
            and entry.append_states[claim.position] == "consumed"
        ):
            entry.append_states[claim.position] = "failed"
        entry.state = "append_failed"
        entry.storage_may_have_changed = True
        _revoke_active_attestations_locked(
            entry.facts.store,
            entry.facts.reductions,
        )
        _revoke_append_authorizations_locked(entry, release_seals=False)


def _attestation_facts(attestation: PipelineAuthorityAttestation) -> _AttestationFacts:
    if type(attestation) is not PipelineAuthorityAttestation:
        raise _error(
            "pipeline_authority_attestation_invalid",
            "pipeline_authority_attestation_invalid",
            "Pipeline authority attestation is invalid",
        )
    invalid_reason = "pipeline_authority_attestation_stale"
    with _LOCK:
        entry = _ATTESTATIONS.get(id(attestation))
        facts = (
            entry.facts
            if entry is not None and entry.reference() is attestation
            else None
        )
        if facts is not None and entry is not None:
            reduction = facts.reduction
            try:
                active_key = entry.active_key
                if (
                    type(active_key) is not tuple
                    or len(active_key) != 4
                    or type(active_key[0]) is not int
                    or any(type(value) is not str for value in active_key[1:])
                ):
                    raise TypeError("attestation active key is invalid")
                current_key = (
                    id(facts.store),
                    reduction.document_id,
                    reduction.run_id,
                    reduction.pipeline_id,
                )
                current_digest = _reduction_fingerprint_digest(reduction)
                current_integrity = _attestation_integrity_hmac(
                    facts.store,
                    facts.document,
                    active_key,
                    current_digest,
                )
                expected_integrity = entry.integrity_hmac_sha256
                active = _ACTIVE_ATTESTATIONS.get(active_key)
            except (
                AttributeError,
                TypeError,
                ValueError,
                OverflowError,
                UnicodeError,
                RecursionError,
                RuntimeError,
            ):
                active_key = None
                current_key = None
                current_integrity = b""
                expected_integrity = b""
                active = None
            integrity_changed = (
                current_key != active_key
                or type(expected_integrity) is not bytes
                or len(expected_integrity) != hashlib.sha256().digest_size
                or not hmac.compare_digest(
                    current_integrity,
                    expected_integrity,
                )
            )
            if active is not attestation or integrity_changed:
                _remove_active_attestation_identity_locked(attestation)
                if integrity_changed:
                    invalid_reason = "pipeline_authority_attestation_facts_changed"
                _ATTESTATIONS.pop(id(attestation), None)
                facts = None
    if facts is None:
        with _LOCK:
            _remove_active_attestation_identity_locked(attestation)
        raise _error(
            "pipeline_authority_attestation_invalid",
            invalid_reason,
            "Pipeline authority attestation is stale or foreign",
        )
    return facts


def _register_attestations(
    store: Any,
    reductions: tuple[ReducedPipelineAuthority, ...],
    permit_entry: _PermitEntry,
) -> tuple[PipelineAuthorityAttestation, ...]:
    prepared = []
    for reduction in reductions:
        attestation = PipelineAuthorityAttestation(_capability=_TOKEN_CAPABILITY)
        identity = id(attestation)
        reference = weakref.ref(
            attestation,
            lambda ref, identity=identity: _cleanup(_ATTESTATIONS, identity, ref),
        )
        key = (id(store), reduction.document_id, reduction.run_id, reduction.pipeline_id)
        prepared.append((
            attestation,
            identity,
            reference,
            key,
            _AttestationEntry(
                reference,
                key,
                _AttestationFacts(
                    store,
                    permit_entry.facts.document,
                    reduction,
                ),
                _attestation_integrity_hmac(
                    store,
                    permit_entry.facts.document,
                    key,
                    _reduction_fingerprint_digest(reduction),
                ),
            ),
        ))
    with _LOCK:
        if permit_entry.state != "reconciling":
            raise _error(
                "pipeline_authority_unavailable",
                "pipeline_authority_attestation_issue_state_invalid",
                "Pipeline authority attestation has no active reconcile claim",
                post_durable=True,
            )
        try:
            for attestation, identity, reference, key, entry in prepared:
                previous = _ACTIVE_ATTESTATIONS.get(key)
                if previous is not None:
                    _ATTESTATIONS.pop(id(previous), None)
                _ATTESTATIONS[identity] = entry
                _ACTIVE_ATTESTATIONS[key] = attestation
            _revoke_append_authorizations_locked(
                permit_entry,
                release_seals=False,
            )
            permit_entry.state = "reconciled"
        except BaseException:
            # Installation is one logical registry transaction.  A process
            # control interruption may land after any individual dict write;
            # remove every member from this attempt and deliberately restore
            # no older proof (reconcile claim already revoked those as stale).
            for attestation, identity, reference, key, _entry in prepared:
                if _ACTIVE_ATTESTATIONS.get(key) is attestation:
                    _ACTIVE_ATTESTATIONS.pop(key, None)
                installed = _ATTESTATIONS.get(identity)
                if installed is not None and installed.reference is reference:
                    _ATTESTATIONS.pop(identity, None)
            permit_entry.state = "repair_required"
            raise
    return tuple(item[0] for item in prepared)


def _claim_permit_for_reconcile(
    store: Any,
    permit: PipelineAuthorityCommitPermit,
) -> _PermitEntry:
    if type(permit) is not PipelineAuthorityCommitPermit:
        raise _error(
            "pipeline_authority_commit_invalid",
            "pipeline_authority_permit_invalid",
            "Pipeline authority commit permit is invalid",
            post_durable=True,
        )
    with _LOCK:
        entry = _PERMITS.get(id(permit))
        if entry is None or entry.reference() is not permit:
            raise _error(
                "pipeline_authority_commit_invalid",
                "pipeline_authority_permit_stale",
                "Pipeline authority commit permit is stale or foreign",
                post_durable=True,
            )
        if entry.state != "prepared" or entry.facts.store is not store:
            raise _error(
                "pipeline_authority_commit_invalid",
                "pipeline_authority_permit_consumed",
                "Pipeline authority commit permit is consumed or bound elsewhere",
                post_durable=True,
            )
        # Claim before any I/O.  A failed verifier leaves this permit consumed
        # and repair-required; no second thread may turn the same prospective
        # proof into another attestation.  The same atomic transition revokes
        # every older attestation for the affected runs: once a newer durable
        # head exists, failure to fresh-reduce it must leave zero usable proof,
        # never silently fall back to a stale pre-control attestation.
        entry.state = "reconciling"
        entry.storage_may_have_changed = True
        _revoke_active_attestations_locked(store, entry.facts.reductions)
        all_append_authorizations_consumed = (
            entry.append_cursor == len(entry.facts.operations)
            and all(state == "consumed" for state in entry.append_states)
        )
        _revoke_append_authorizations_locked(entry, release_seals=False)
        if not all_append_authorizations_consumed:
            entry.state = "repair_required"
            raise _error(
                "pipeline_authority_unavailable",
                "pipeline_authority_append_incomplete",
                "Pipeline authority commit did not consume its exact append sequence",
                post_durable=True,
            )
        return entry


def reconcile_durable_pipeline_authority(
    store: Any,
    permit: PipelineAuthorityCommitPermit,
) -> tuple[PipelineAuthorityAttestation, ...]:
    """Fresh-reduce the exact durable head and issue internal attestations."""

    entry = _claim_permit_for_reconcile(store, permit)
    try:
        op_log, rows, count, tail_hash = _fresh_snapshot(store, post_durable=True)
        facts = entry.facts
        if op_log is not facts.op_log or count != facts.expected_head_count or not hmac.compare_digest(
            tail_hash, facts.expected_head_hash
        ):
            raise _error(
                "pipeline_authority_unavailable",
                "pipeline_authority_durable_head_mismatch",
                "Durable Pipeline authority head differs from the prospective proof",
                post_durable=True,
            )
        durable: list[ReducedPipelineAuthority] = []
        for proof in facts.reductions:
            reduced = reduce_fresh_pipeline_history(
                rows,
                head_count=count,
                head_hash=tail_hash,
                document_id=proof.key[0],
                run_id=proof.key[1],
                pipeline_id=proof.key[2],
            )
            if _reduction_fingerprint(reduced) != proof.fingerprint:
                raise _error(
                    "pipeline_authority_unavailable",
                    "pipeline_authority_durable_reduction_mismatch",
                    "Durable Pipeline authority reduction changed after commit",
                    post_durable=True,
                )
            durable.append(reduced)
    except PipelineAuthorityCommitError:
        with _LOCK:
            if entry.state == "reconciling":
                entry.state = "repair_required"
        raise
    except PipelineAuthorityReductionError as exc:
        with _LOCK:
            if entry.state == "reconciling":
                entry.state = "repair_required"
        raise _wrap_reduction_error(exc, post_durable=True) from exc
    except Exception as exc:
        with _LOCK:
            if entry.state == "reconciling":
                entry.state = "repair_required"
        raise _error(
            "pipeline_authority_unavailable",
            "pipeline_authority_reconcile_unexpected",
            "Durable Pipeline authority reconciliation failed",
            post_durable=True,
        ) from exc

    try:
        attestations = _register_attestations(store, tuple(durable), entry)
    except PipelineAuthorityCommitError:
        with _LOCK:
            if entry.state == "reconciling":
                entry.state = "repair_required"
        raise
    except Exception as exc:
        with _LOCK:
            if entry.state == "reconciling":
                entry.state = "repair_required"
        raise _error(
            "pipeline_authority_unavailable",
            "pipeline_authority_attestation_issue_failed",
            "Durable Pipeline authority attestation could not be issued",
            post_durable=True,
        ) from exc
    return attestations


def discard_pipeline_authority_commit(
    permit: PipelineAuthorityCommitPermit | None,
) -> None:
    """Revoke an unused prospective permit; valid permits make this no-throw."""

    if permit is None:
        return
    entry = _permit_entry(permit)
    with _LOCK:
        if entry.state in {"prepared", "append_failed"}:
            clean_release = (
                not entry.storage_may_have_changed
                and all(state == "prepared" for state in entry.append_states)
            )
            if not clean_release:
                entry.storage_may_have_changed = True
                _revoke_active_attestations_locked(
                    entry.facts.store,
                    entry.facts.reductions,
                )
            entry.state = "discarded"
            _revoke_append_authorizations_locked(
                entry,
                release_seals=clean_release,
            )


def invalidate_pipeline_authority_attestations(
    permit: PipelineAuthorityCommitPermit | None,
) -> None:
    """Revoke old run proofs once storage can no longer prove untouched.

    This operation is idempotent and deliberately does not consume a prepared
    permit: a fully committed receipt may still proceed to fresh reconcile.
    It only ensures DIRTY/INDETERMINATE and post-store-finalizer failures cannot
    fall back to an attestation for the previous durable head.
    """

    if permit is None:
        return
    entry = _permit_entry(permit)
    with _LOCK:
        entry.storage_may_have_changed = True
        _set_seal_claims_locked(entry, release=False)
        _revoke_active_attestations_locked(
            entry.facts.store,
            entry.facts.reductions,
        )


def is_pipeline_authority_attestation(value: object) -> bool:
    try:
        _attestation_facts(value)  # type: ignore[arg-type]
    except PipelineAuthorityCommitError:
        return False
    return True


_install_pipeline_authority_append_hooks(
    claim=_claim_pipeline_authority_append,
    complete=_complete_pipeline_authority_append,
    abort=_abort_pipeline_authority_append,
    _capability=_PIPELINE_AUTHORITY_APPEND_HOOK_CAPABILITY,
)


__all__ = (
    "PipelineAuthorityAttestation",
    "PipelineAuthorityCommitError",
    "PipelineAuthorityCommitPermit",
    "discard_pipeline_authority_commit",
    "invalidate_pipeline_authority_attestations",
    "is_pipeline_authority_attestation",
    "issue_sealed_pipeline_control_operation",
    "issue_sealed_pipeline_start_operation",
    "prepare_pipeline_authority_commit",
    "reconcile_durable_pipeline_authority",
)
