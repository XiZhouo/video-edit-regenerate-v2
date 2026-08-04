"""Dormant delegated-mutation request checks for Pipeline authority v2.

This module bridges a committed, durable, fresh Batch-3 attestation to one
exact *non-executable* worker mutation request.  It deliberately does not
dispatch a command, mutate a Document, create a grant, register a run, write a
manifest, issue a Control Operation, or start a worker.  Batch 5 must combine
the same checks with a live grant registry and the existing 3.36 actual-delta
and reversible-Operation validation inside one CommitCoordinator session.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import threading
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_CORE_DIR = Path(__file__).resolve().parent.parent
_DOCUMENT_DIR = _CORE_DIR / "01-data-structure" / "01-document"
_ENTITY_DIR = _CORE_DIR / "01-data-structure" / "02-entity"
_COMMAND_DIR = _CORE_DIR / "03-commands-operations" / "01-command"
_PERSISTENCE_DIR = _CORE_DIR / "02-data-flow" / "07-persistence"
for _directory in (_DOCUMENT_DIR, _ENTITY_DIR, _COMMAND_DIR, _PERSISTENCE_DIR):
    if str(_directory) not in sys.path:
        sys.path.insert(0, str(_directory))

from command import (  # noqa: E402
    CommandRegistry,
    _revalidate_pipeline_delegated_mutation_root,
)
from document import Document  # noqa: E402
from entity import Actor, Entity, ZeError  # noqa: E402
from file_store import FileCommitStore  # noqa: E402
from history_authority import (  # noqa: E402
    HistoryAuthorityError,
    _HISTORY_RUNTIME_VERIFIER_CAPABILITY,
    _verify_canonical_history_runtime,
)
from pipeline_authority_codec import (  # noqa: E402
    PipelineAuthorityCodecError,
    _pipeline_document_policy_digest,
)
from pipeline_authority_commit import (  # noqa: E402
    PipelineAuthorityAttestation,
    PipelineAuthorityCommitError,
    _attestation_facts,
)
from pipeline_authority_reducer import (  # noqa: E402
    PipelineAuthorityReductionError,
    reduce_fresh_pipeline_history,
)


PIPELINE_DELEGATED_MUTATION_PROTOCOL = "ze.pipeline-delegated-mutation.v1"
_LIFECYCLE_ACTIONS = frozenset(
    ("create", "rename", "soft_delete", "restore", "hard_remove")
)
_RESERVED_FIELDS = frozenset(("__entity__", "__deleted__", "__restored__"))
_LOWER_HEX = frozenset("0123456789abcdef")
_TOKEN_CAPABILITY = object()
_LOCK = threading.RLock()
_REQUEST_INTEGRITY_SECRET = os.urandom(32)


class PipelineDelegatedMutationError(RuntimeError):
    """Stable failure for a dormant delegated-mutation request check."""

    def __init__(self, code: str, reason: str, message: str) -> None:
        if type(code) is not str or not code:
            raise TypeError("code must be a non-empty exact string")
        if type(reason) is not str or not reason:
            raise TypeError("reason must be a non-empty exact string")
        if type(message) is not str or not message:
            raise TypeError("message must be a non-empty exact string")
        super().__init__(message)
        self.code = code
        self.reason = reason
        self.message = message
        self.context = {"reason": reason, "execution_authority": False}


def _error(code: str, reason: str, message: str) -> PipelineDelegatedMutationError:
    return PipelineDelegatedMutationError(code, reason, message)


class PipelineDelegatedMutationRequest:
    """One exact dormant check request; never a worker permit or grant."""

    __slots__ = ("__weakref__",)

    def __new__(cls, *, _capability: object | None = None):
        if _capability is not _TOKEN_CAPABILITY:
            raise TypeError("delegated mutation requests are framework-issued")
        return super().__new__(cls)

    @property
    def protocol(self) -> str:
        _request_facts(self)
        return PIPELINE_DELEGATED_MUTATION_PROTOCOL

    @property
    def run_id(self) -> str:
        return _request_facts(self).run_id

    @property
    def pipeline_id(self) -> str:
        return _request_facts(self).pipeline_id

    @property
    def item_id(self) -> str:
        return _request_facts(self).item_id

    @property
    def stage_id(self) -> str:
        return _request_facts(self).stage_id

    @property
    def entity_type(self) -> str:
        return _request_facts(self).entity_type

    @property
    def entity_id(self) -> str:
        return _request_facts(self).entity_id

    @property
    def fields(self) -> tuple[str, ...]:
        return _request_facts(self).fields

    @property
    def lifecycle(self) -> tuple[str, ...]:
        return _request_facts(self).lifecycle

    @property
    def execution_authority(self) -> bool:
        _request_facts(self)
        return False

    def __copy__(self):
        raise TypeError("delegated mutation requests cannot be copied")

    def __deepcopy__(self, _memo: Any):
        raise TypeError("delegated mutation requests cannot be copied")

    def __reduce_ex__(self, _protocol: int):
        raise TypeError("delegated mutation requests cannot be serialized")

    def __reduce__(self):
        raise TypeError("delegated mutation requests cannot be serialized")


@dataclass(frozen=True)
class _RequestFacts:
    attestation: PipelineAuthorityAttestation
    store: FileCommitStore
    document: Document
    registry: CommandRegistry
    issuer_pid: int
    document_id: str
    run_id: str
    pipeline_id: str
    start_operation_id: str
    start_operation_hash: str
    start_descriptor_digest: str
    generation: int
    history_head_count: int
    history_head_hash: str
    root_command_id: str
    root_command_digest: str
    document_policy_digest: str
    worker_actor: tuple[str, str, str]
    worker_source: str
    item_id: str
    stage_id: str
    operation_command_id: str
    entity_type: str
    entity_id: str
    fields: tuple[str, ...]
    field_roots: tuple[str, ...]
    lifecycle: tuple[str, ...]
    expected_entity_version: int
    entity: Entity


@dataclass
class _RequestEntry:
    reference: Any
    facts: _RequestFacts
    facts_integrity_hmac_sha256: bytes
    state: str = "prepared"


_REQUESTS: dict[int, _RequestEntry] = {}
_CONSUMED_REQUESTS: dict[int, Any] = {}


def _reset_after_fork() -> None:
    global _LOCK, _REQUEST_INTEGRITY_SECRET
    _LOCK = threading.RLock()
    _REQUEST_INTEGRITY_SECRET = os.urandom(32)
    _REQUESTS.clear()
    _CONSUMED_REQUESTS.clear()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_after_fork)


def _cleanup_request(identity: int, reference: Any) -> None:
    with _LOCK:
        entry = _REQUESTS.get(identity)
        if entry is not None and entry.reference is reference:
            _REQUESTS.pop(identity, None)
        if _CONSUMED_REQUESTS.get(identity) is reference:
            _CONSUMED_REQUESTS.pop(identity, None)


def _request_entry(request: PipelineDelegatedMutationRequest) -> _RequestEntry:
    if type(request) is not PipelineDelegatedMutationRequest:
        raise _error(
            "pipeline_delegated_mutation_invalid",
            "pipeline_delegated_request_type_invalid",
            "delegated mutation request is not framework-issued",
        )
    with _LOCK:
        entry = _REQUESTS.get(id(request))
        if entry is None or entry.reference() is not request:
            consumed = _CONSUMED_REQUESTS.get(id(request))
            if consumed is not None and consumed() is request:
                raise _error(
                    "pipeline_delegated_mutation_invalid",
                    "pipeline_delegated_request_consumed",
                    "delegated mutation request is already claimed or consumed",
                )
            raise _error(
                "pipeline_delegated_mutation_invalid",
                "pipeline_delegated_request_stale",
                "delegated mutation request is stale or foreign",
            )
        try:
            current_fingerprint = _request_fingerprint(entry.facts)
        except (
            AttributeError,
            TypeError,
            ValueError,
            OverflowError,
            UnicodeError,
            RecursionError,
        ):
            current_fingerprint = b""
        expected_fingerprint = entry.facts_integrity_hmac_sha256
        if (
            type(expected_fingerprint) is not bytes
            or len(expected_fingerprint) != hashlib.sha256().digest_size
            or not hmac.compare_digest(
                current_fingerprint,
                expected_fingerprint,
            )
        ):
            _REQUESTS.pop(id(request), None)
            raise _error(
                "pipeline_delegated_mutation_invalid",
                "pipeline_delegated_request_facts_changed",
                "delegated mutation request facts changed after issuance",
            )
        return entry


def _request_facts(request: PipelineDelegatedMutationRequest) -> _RequestFacts:
    return _request_entry(request).facts


def _request_fingerprint(facts: _RequestFacts) -> bytes:
    payload = (
        id(facts.attestation),
        id(facts.store),
        id(facts.document),
        id(facts.registry),
        id(facts.entity),
        facts.issuer_pid,
        facts.document_id,
        facts.run_id,
        facts.pipeline_id,
        facts.start_operation_id,
        facts.start_operation_hash,
        facts.start_descriptor_digest,
        facts.generation,
        facts.history_head_count,
        facts.history_head_hash,
        facts.root_command_id,
        facts.root_command_digest,
        facts.document_policy_digest,
        facts.worker_actor,
        facts.worker_source,
        facts.item_id,
        facts.stage_id,
        facts.operation_command_id,
        facts.entity_type,
        facts.entity_id,
        facts.fields,
        facts.field_roots,
        facts.lifecycle,
        facts.expected_entity_version,
    )
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8", "strict")
    return hmac.new(
        _REQUEST_INTEGRITY_SECRET,
        encoded,
        hashlib.sha256,
    ).digest()


def _identifier(value: Any, *, field: str) -> str:
    if type(value) is not str or not value:
        raise _error(
            "pipeline_delegated_mutation_invalid",
            f"pipeline_delegated_{field}_invalid",
            f"delegated mutation {field} must be a non-empty exact string",
        )
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeError:
        encoded = b""
    if (
        not encoded
        or len(encoded) > 256
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
        or any(character in value for character in ("*", "?", "[", "]"))
    ):
        raise _error(
            "pipeline_delegated_mutation_invalid",
            f"pipeline_delegated_{field}_invalid",
            f"delegated mutation {field} is invalid",
        )
    return value


def _hash(value: Any, *, field: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in _LOWER_HEX for character in value)
    ):
        raise _error(
            "pipeline_delegated_mutation_invalid",
            f"pipeline_delegated_{field}_invalid",
            f"delegated mutation {field} is invalid",
        )
    return value


def _worker_identity(
    worker_actor: Any,
    worker_source: Any,
) -> tuple[tuple[str, str, str], str]:
    if type(worker_actor) is not Actor:
        raise _error(
            "pipeline_delegated_mutation_invalid",
            "pipeline_delegated_worker_actor_invalid",
            "delegated mutation requires one exact worker Actor",
        )
    try:
        payload = worker_actor.to_dict()
        actor_facts = (
            payload["user_id"],
            payload["device_id"],
            payload["source"],
        )
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise _error(
            "pipeline_delegated_mutation_invalid",
            "pipeline_delegated_worker_actor_invalid",
            "delegated mutation worker Actor is invalid",
        ) from exc
    if any(type(value) is not str or not value for value in actor_facts):
        raise _error(
            "pipeline_delegated_mutation_invalid",
            "pipeline_delegated_worker_actor_invalid",
            "delegated mutation worker Actor is invalid",
        )
    return actor_facts, _identifier(worker_source, field="worker_source")


def _field_paths(value: Any) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if type(value) is not tuple:
        raise _error(
            "pipeline_delegated_mutation_invalid",
            "pipeline_delegated_fields_invalid",
            "delegated mutation fields must be one exact tuple",
        )
    paths: list[str] = []
    roots: list[str] = []
    for raw in value:
        field = _identifier(raw, field="field")
        segments = field.split(".")
        if (
            any(not segment for segment in segments)
            or segments[0] in _RESERVED_FIELDS
        ):
            raise _error(
                "pipeline_delegated_mutation_invalid",
                "pipeline_delegated_field_path_invalid",
                "delegated mutation field path is invalid",
            )
        paths.append(field)
        roots.append(segments[0])
    if tuple(sorted(set(paths))) != value:
        raise _error(
            "pipeline_delegated_mutation_invalid",
            "pipeline_delegated_fields_unsorted_or_duplicate",
            "delegated mutation fields must be sorted and unique",
        )
    return value, tuple(sorted(set(roots)))


def _lifecycle(value: Any) -> tuple[str, ...]:
    if type(value) is not tuple or any(
        type(action) is not str or action not in _LIFECYCLE_ACTIONS
        for action in value
    ):
        raise _error(
            "pipeline_delegated_mutation_invalid",
            "pipeline_delegated_lifecycle_invalid",
            "delegated mutation lifecycle actions must be sorted and unique",
        )
    if tuple(sorted(set(value))) != value:
        raise _error(
            "pipeline_delegated_mutation_invalid",
            "pipeline_delegated_lifecycle_invalid",
            "delegated mutation lifecycle actions must be sorted and unique",
        )
    if "create" in value:
        raise _error(
            "pipeline_delegated_creation_registry_required",
            "pipeline_delegated_dynamic_creation_dormant",
            "dynamic delegated creation requires the Batch 5 run registry",
        )
    return value


def _active_attestation_facts(attestation: Any) -> Any:
    if type(attestation) is not PipelineAuthorityAttestation:
        raise _error(
            "pipeline_delegated_mutation_invalid",
            "pipeline_delegated_attestation_type_invalid",
            "delegated mutation requires an internal Pipeline attestation",
        )
    try:
        return _attestation_facts(attestation)
    except PipelineAuthorityCommitError as exc:
        raise _error(
            "pipeline_authority_stale",
            exc.reason,
            "delegated mutation attestation is stale or invalid",
        ) from exc


def _current_document_policy(reduction: Any, *,
                             registry: CommandRegistry, document: Document) -> dict[str, Any]:
    entity_types = tuple(sorted({
        target.entity_type for target in reduction.delegated_targets
    } | {
        scope.entity_type for scope in reduction.delegated_creation_scopes
    }))
    try:
        policy = _revalidate_pipeline_delegated_mutation_root(
            registry=registry,
            document=document,
            command_id=reduction.start_command_id,
            root_command_digest=reduction.root_command_digest,
            entity_types=entity_types,
        )
    except ZeError as exc:
        reason = exc.context.get("reason", "pipeline_delegated_root_recheck_failed")
        raise _error(
            "pipeline_authority_stale",
            reason,
            "delegated mutation business root is no longer current",
        ) from exc
    try:
        digest = _pipeline_document_policy_digest(policy)
    except PipelineAuthorityCodecError as exc:
        raise _error(
            "pipeline_authority_stale",
            "pipeline_delegated_document_policy_invalid",
            "delegated mutation Document policy is no longer canonical",
        ) from exc
    if not hmac.compare_digest(digest, reduction.document_policy_digest):
        raise _error(
            "pipeline_authority_stale",
            "pipeline_delegated_document_policy_digest_mismatch",
            "delegated mutation Document policy changed since Pipeline Start",
        )
    return policy


def _policy_for_type(policy: dict[str, Any], entity_type: str) -> dict[str, Any]:
    matches = [
        entry["policy"] for entry in policy["entity_policies"]
        if entry["entity_type"] == entity_type
    ]
    if len(matches) != 1:
        raise _error(
            "pipeline_authority_stale",
            "pipeline_delegated_entity_policy_missing",
            "delegated mutation entity policy is no longer exact",
        )
    return matches[0]


def _static_reduction_digest(reduction: Any) -> bytes:
    payload = (
        reduction.protocol,
        reduction.document_id,
        reduction.run_id,
        reduction.pipeline_id,
        reduction.start_command_id,
        reduction.start_operation_id,
        reduction.start_operation_hash,
        reduction.start_descriptor_digest,
        reduction.initial_operation_ids,
        reduction.root_actor,
        reduction.root_source,
        reduction.root_command_digest,
        reduction.document_policy_digest,
        reduction.worker_actor,
        reduction.worker_source,
        tuple(
            (value.item_id, value.entity_type, value.entity_id)
            for value in reduction.work_item_bindings
        ),
        reduction.allowed_stage_ids,
        tuple(
            (
                value.item_id,
                value.entity_type,
                value.entity_id,
                value.operation_id,
            )
            for value in reduction.placeholder_bindings
        ),
        tuple(
            (value.entity_type, value.entity_id, value.fields, value.lifecycle)
            for value in reduction.delegated_targets
        ),
        tuple(
            (
                value.entity_type,
                value.fields,
                value.max_per_operation,
                value.max_per_run,
            )
            for value in reduction.delegated_creation_scopes
        ),
    )
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8", "strict")
    return hashlib.sha256(encoded).digest()


def _fresh_runtime_reduction(
    attested: Any,
    *,
    store: FileCommitStore,
    document: Document,
    registry: CommandRegistry,
) -> Any:
    if (
        attested.store is not store
        or attested.document is not document
        or type(store) is not FileCommitStore
        or type(document) is not Document
        or type(registry) is not CommandRegistry
        or object.__getattribute__(
            document,
            "_mutation_policy_registry_authority",
        ) is not registry
    ):
        raise _error(
            "pipeline_authority_stale",
            "pipeline_delegated_runtime_binding_mismatch",
            "delegated mutation attestation belongs to another runtime",
        )
    controller = object.__getattribute__(
        document,
        "_history_controller_authority",
    )
    try:
        rows, fresh_history_head = _verify_canonical_history_runtime(
            document=document,
            store=store,
            op_log=store.op_log,
            controller=controller,
            _capability=_HISTORY_RUNTIME_VERIFIER_CAPABILITY,
        )
    except HistoryAuthorityError as exc:
        raise _error(
            "pipeline_authority_stale",
            exc.reason,
            "delegated mutation canonical history authority is unavailable",
        ) from exc
    durable = attested.reduction
    try:
        current = reduce_fresh_pipeline_history(
            rows,
            head_count=fresh_history_head.count,
            head_hash=fresh_history_head.tail_hash,
            document_id=durable.document_id,
            run_id=durable.run_id,
            pipeline_id=durable.pipeline_id,
        )
        static_matches = hmac.compare_digest(
            _static_reduction_digest(current),
            _static_reduction_digest(durable),
        )
    except PipelineAuthorityReductionError as exc:
        raise _error(
            "pipeline_authority_stale",
            exc.reason,
            "delegated mutation current Pipeline history is invalid",
        ) from exc
    except (
        AttributeError,
        TypeError,
        ValueError,
        OverflowError,
        UnicodeError,
        RecursionError,
        RuntimeError,
    ) as exc:
        raise _error(
            "pipeline_authority_stale",
            "pipeline_delegated_fresh_reduction_invalid",
            "delegated mutation current Pipeline authority is invalid",
        ) from exc
    if not static_matches:
        raise _error(
            "pipeline_authority_stale",
            "pipeline_delegated_start_authority_changed",
            "delegated mutation durable Start authority changed",
        )
    return current


def _validate_facts(facts: _RequestFacts) -> None:
    if facts.issuer_pid != os.getpid():
        raise _error(
            "pipeline_authority_stale",
            "pipeline_delegated_request_process_mismatch",
            "delegated mutation request belongs to another process",
        )
    attested = _active_attestation_facts(facts.attestation)
    reduction = _fresh_runtime_reduction(
        attested,
        store=facts.store,
        document=facts.document,
        registry=facts.registry,
    )
    exact = (
        (reduction.document_id, facts.document_id),
        (reduction.run_id, facts.run_id),
        (reduction.pipeline_id, facts.pipeline_id),
        (reduction.start_operation_id, facts.start_operation_id),
        (reduction.start_operation_hash, facts.start_operation_hash),
        (reduction.start_descriptor_digest, facts.start_descriptor_digest),
        (reduction.generation, facts.generation),
        (reduction.history_head_count, facts.history_head_count),
        (reduction.history_head_hash, facts.history_head_hash),
        (reduction.start_command_id, facts.root_command_id),
        (reduction.root_command_digest, facts.root_command_digest),
        (reduction.document_policy_digest, facts.document_policy_digest),
    )
    if any(type(left) is not type(right) or left != right for left, right in exact):
        raise _error(
            "pipeline_authority_stale",
            "pipeline_delegated_attestation_facts_changed",
            "delegated mutation request no longer matches its fresh attestation",
        )
    if facts.document.id != reduction.document_id:
        raise _error(
            "pipeline_authority_stale",
            "pipeline_delegated_document_mismatch",
            "delegated mutation belongs to another Document",
        )
    if reduction.terminal:
        raise _error(
            "pipeline_authority_terminal",
            "pipeline_delegated_authority_terminal",
            "terminal Pipeline authority cannot validate delegated mutation",
        )
    if reduction.reconcile_required:
        raise _error(
            "pipeline_reconcile_required",
            "pipeline_delegated_reconcile_required",
            "Pipeline authority must be reconciled before delegated mutation",
        )
    if (
        facts.worker_actor != reduction.worker_actor
        or facts.worker_source != reduction.worker_source
    ):
        raise _error(
            "pipeline_delegated_mutation_denied",
            "pipeline_delegated_worker_identity_mismatch",
            "actual worker identity differs from the durable Pipeline Start",
        )
    if reduction.worker_actor[2] != reduction.worker_source:
        raise _error(
            "pipeline_delegated_mutation_denied",
            "pipeline_delegated_worker_source_mismatch",
            "worker Actor source and mutation source cannot satisfy the real policy authorizer",
        )
    binding = next(
        (
            item for item in reduction.work_item_bindings
            if item.item_id == facts.item_id
        ),
        None,
    )
    if binding is None or (
        binding.entity_type,
        binding.entity_id,
    ) != (facts.entity_type, facts.entity_id):
        raise _error(
            "pipeline_delegated_mutation_denied",
            "pipeline_delegated_item_target_mismatch",
            "delegated mutation item does not own the requested entity",
        )
    if facts.stage_id not in reduction.allowed_stage_ids:
        raise _error(
            "pipeline_delegated_mutation_denied",
            "pipeline_delegated_stage_denied",
            "delegated mutation stage is outside the durable Start",
        )
    target = next(
        (
            value for value in reduction.delegated_targets
            if (value.entity_type, value.entity_id)
            == (facts.entity_type, facts.entity_id)
        ),
        None,
    )
    if target is None:
        raise _error(
            "pipeline_delegated_mutation_denied",
            "pipeline_delegated_target_denied",
            "delegated mutation target is outside the durable Start",
        )
    if not set(facts.field_roots).issubset(target.fields) or not set(
        facts.lifecycle
    ).issubset(target.lifecycle):
        raise _error(
            "pipeline_delegated_mutation_denied",
            "pipeline_delegated_scope_exceeded",
            "delegated mutation exceeds its exact field or lifecycle scope",
        )
    if (
        reduction.start_command_id.startswith("entity.")
        or facts.operation_command_id.startswith("entity.")
    ):
        raise _error(
            "pipeline_delegated_mutation_denied",
            "pipeline_delegated_generic_command_forbidden",
            "generic entity commands cannot become delegated authority roots",
        )
    if facts.operation_command_id != reduction.start_command_id:
        raise _error(
            "pipeline_delegated_mutation_denied",
            "pipeline_delegated_root_command_mismatch",
            "delegated mutation primary Operation must retain the business root identity",
        )
    current_policy = _current_document_policy(
        reduction,
        registry=facts.registry,
        document=facts.document,
    )
    entity_policy = _policy_for_type(current_policy, facts.entity_type)
    if any(
        root not in entity_policy["fields"]
        or reduction.start_command_id not in entity_policy["fields"][root]
        for root in facts.field_roots
    ) or any(
        reduction.start_command_id not in entity_policy["lifecycle"][action]
        for action in facts.lifecycle
    ):
        raise _error(
            "pipeline_delegated_mutation_denied",
            "pipeline_delegated_current_policy_denied",
            "current business policy does not authorize the delegated scope",
        )
    entity = facts.document.find(facts.entity_type, facts.entity_id)
    if entity is None:
        raise _error(
            "pipeline_delegated_creation_registry_required",
            "pipeline_delegated_target_missing",
            "delegated target is missing; dynamic creation remains dormant until Batch 5",
        )
    if (
        type(entity) is not Entity
        or entity is not facts.entity
        or entity.type != facts.entity_type
        or entity.id != facts.entity_id
        or entity.read_only
        or entity.version != facts.expected_entity_version
    ):
        raise _error(
            "pipeline_authority_stale",
            "pipeline_delegated_entity_head_changed",
            "delegated mutation target identity or version changed",
        )
    try:
        facts.document._validate_mutation_policy_cardinality()
    except ZeError as exc:
        raise _error(
            "pipeline_delegated_mutation_denied",
            exc.context.get("reason", "pipeline_delegated_cardinality_invalid"),
            "delegated mutation Document cardinality is invalid",
        ) from exc


def prepare_dormant_pipeline_delegated_mutation_request(
    attestation: PipelineAuthorityAttestation,
    *,
    store: FileCommitStore,
    document: Document,
    registry: CommandRegistry,
    run_id: str,
    pipeline_id: str,
    start_operation_id: str,
    start_operation_hash: str,
    generation: int,
    item_id: str,
    stage_id: str,
    operation_command_id: str,
    entity_type: str,
    entity_id: str,
    worker_actor: Actor,
    worker_source: str,
    fields: tuple[str, ...] = (),
    lifecycle: tuple[str, ...] = (),
    expected_entity_version: int,
) -> PipelineDelegatedMutationRequest:
    """Prepare one side-effect-free, one-shot dormant scope check."""

    if type(store) is not FileCommitStore:
        raise _error(
            "pipeline_delegated_mutation_invalid",
            "pipeline_delegated_store_type_invalid",
            "delegated mutation requires an exact FileCommitStore",
        )
    if type(document) is not Document or type(registry) is not CommandRegistry:
        raise _error(
            "pipeline_delegated_mutation_invalid",
            "pipeline_delegated_runtime_type_invalid",
            "delegated mutation requires exact runtime objects",
        )
    run_id = _identifier(run_id, field="run_id")
    pipeline_id = _identifier(pipeline_id, field="pipeline_id")
    start_operation_id = _identifier(start_operation_id, field="start_operation_id")
    start_operation_hash = _hash(start_operation_hash, field="start_operation_hash")
    item_id = _identifier(item_id, field="item_id")
    stage_id = _identifier(stage_id, field="stage_id")
    operation_command_id = _identifier(
        operation_command_id,
        field="operation_command_id",
    )
    entity_type = _identifier(entity_type, field="entity_type")
    entity_id = _identifier(entity_id, field="entity_id")
    worker_actor_facts, worker_source = _worker_identity(
        worker_actor,
        worker_source,
    )
    fields, field_roots = _field_paths(fields)
    lifecycle = _lifecycle(lifecycle)
    if not fields and not lifecycle:
        raise _error(
            "pipeline_delegated_mutation_invalid",
            "pipeline_delegated_scope_empty",
            "delegated mutation request must name a field or lifecycle action",
        )
    if type(generation) is not int or generation < 0:
        raise _error(
            "pipeline_delegated_mutation_invalid",
            "pipeline_delegated_generation_invalid",
            "delegated mutation generation is invalid",
        )
    if type(expected_entity_version) is not int or expected_entity_version < 0:
        raise _error(
            "pipeline_delegated_mutation_invalid",
            "pipeline_delegated_entity_version_invalid",
            "delegated mutation expected entity version is invalid",
        )
    attested = _active_attestation_facts(attestation)
    reduction = _fresh_runtime_reduction(
        attested,
        store=store,
        document=document,
        registry=registry,
    )
    entity = document.find(entity_type, entity_id)
    if entity is None:
        raise _error(
            "pipeline_delegated_creation_registry_required",
            "pipeline_delegated_target_missing",
            "dynamic delegated creation remains dormant until Batch 5",
        )
    request_facts = _RequestFacts(
        attestation=attestation,
        store=store,
        document=document,
        registry=registry,
        issuer_pid=os.getpid(),
        document_id=document.id,
        run_id=run_id,
        pipeline_id=pipeline_id,
        start_operation_id=start_operation_id,
        start_operation_hash=start_operation_hash,
        start_descriptor_digest=reduction.start_descriptor_digest,
        generation=generation,
        history_head_count=reduction.history_head_count,
        history_head_hash=reduction.history_head_hash,
        root_command_id=reduction.start_command_id,
        root_command_digest=reduction.root_command_digest,
        document_policy_digest=reduction.document_policy_digest,
        worker_actor=worker_actor_facts,
        worker_source=worker_source,
        item_id=item_id,
        stage_id=stage_id,
        operation_command_id=operation_command_id,
        entity_type=entity_type,
        entity_id=entity_id,
        fields=fields,
        field_roots=field_roots,
        lifecycle=lifecycle,
        expected_entity_version=expected_entity_version,
        entity=entity,
    )
    _validate_facts(request_facts)

    request = PipelineDelegatedMutationRequest(_capability=_TOKEN_CAPABILITY)
    identity = id(request)
    reference = weakref.ref(
        request,
        lambda ref, identity=identity: _cleanup_request(identity, ref),
    )
    with _LOCK:
        _REQUESTS[identity] = _RequestEntry(
            reference,
            request_facts,
            _request_fingerprint(request_facts),
        )
    return request


def consume_dormant_pipeline_delegated_mutation_request(
    request: PipelineDelegatedMutationRequest,
    *,
    attestation: PipelineAuthorityAttestation,
    store: FileCommitStore,
    document: Document,
    registry: CommandRegistry,
    worker_actor: Actor,
    worker_source: str,
) -> None:
    """Consume and recheck one request without granting or executing anything."""

    entry = _request_entry(request)
    with _LOCK:
        current = _REQUESTS.get(id(request))
        if current is not entry or entry.state != "prepared":
            raise _error(
                "pipeline_delegated_mutation_invalid",
                "pipeline_delegated_request_consumed",
                "delegated mutation request is already claimed or consumed",
            )
        # Claim by removing the live request from the issuable ledger before
        # any revalidation.  The weak tombstone preserves a stable one-shot
        # error without trusting the mutable entry.state as authority; changing
        # a detached entry back to ``prepared`` cannot make it consumable again.
        _REQUESTS.pop(id(request), None)
        _CONSUMED_REQUESTS[id(request)] = entry.reference
        entry.state = "consuming"
    try:
        facts = entry.facts
        if (
            facts.attestation is not attestation
            or facts.store is not store
            or facts.document is not document
            or facts.registry is not registry
        ):
            raise _error(
                "pipeline_authority_stale",
                "pipeline_delegated_request_binding_mismatch",
                "delegated mutation request was consumed by another runtime graph",
            )
        actual_worker_actor, actual_worker_source = _worker_identity(
            worker_actor,
            worker_source,
        )
        if (
            actual_worker_actor != facts.worker_actor
            or actual_worker_source != facts.worker_source
        ):
            raise _error(
                "pipeline_delegated_mutation_denied",
                "pipeline_delegated_worker_identity_mismatch",
                "current worker identity differs from the durable Pipeline Start",
            )
        _validate_facts(facts)
    finally:
        with _LOCK:
            entry.state = "consumed"


__all__ = (
    "PIPELINE_DELEGATED_MUTATION_PROTOCOL",
    "PipelineDelegatedMutationError",
    "PipelineDelegatedMutationRequest",
    "consume_dormant_pipeline_delegated_mutation_request",
    "prepare_dormant_pipeline_delegated_mutation_request",
)
