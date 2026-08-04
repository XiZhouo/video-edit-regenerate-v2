"""Document mutation-session protocol and commit-candidate authority.

This module owns thread/process reservation, history replay scopes, protected
coverage ledgers, rollback lifecycle, and unpublished atomic-head candidates.
It imports the Document class only inside exact runtime type checks, avoiding a
module cycle while document.py re-exports the established public surface.
"""

from __future__ import annotations

import math
import os
import threading
import time
import weakref
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

from entity import Actor, Entity, ZeError
from mutation_policy_coverage import (
    MUTATION_POLICY_COVERAGE_CAPABILITY as _MUTATION_POLICY_COVERAGE_CAPABILITY,
    MUTATION_POLICY_COVERAGE_VALIDATED as _MUTATION_POLICY_COVERAGE_VALIDATED,
)
from history_authority import (
    mark_history_replay_ticket,
    require_history_ticket_replayed,
    validate_history_replay_ticket,
)
from mutation_policy_graph import (
    DocumentMutationSnapshot,
    _AdmittedEntityMutationState,
    _ProtectedMutationFingerprint,
    _document_matches_mutation_snapshot,
    _json_values_exact,
    _mutation_fingerprint_bytes,
    _protected_fingerprints_match,
    _protected_mutation_fingerprint,
)

_ATOMIC_DOCUMENT_COMMIT_CAPABILITY = object()
_HISTORY_REPLAY_CAPABILITY = object()


def _document_type() -> type:
    from document import Document
    return Document



_MUTATION_SESSION_LOCAL = threading.local()
_HISTORY_REPLAY_LOCAL = threading.local()
_HISTORY_OPERATION_LOCAL = threading.local()
_MUTATION_SESSION_REGISTRY_LOCK = threading.RLock()
_MUTATION_SESSION_REGISTRY: dict[int, list[Any]] = {}


class _DocumentCommitCandidate:
    """Opaque one-session version advance; never a durability receipt."""

    __slots__ = ("__weakref__",)

    def __new__(cls):
        raise TypeError("Document commit candidates are framework-issued")

    def __reduce_ex__(self, _protocol: int):
        raise TypeError("Document commit candidates cannot be serialized")


@dataclass(frozen=True)
class _DocumentCommitCandidateRecord:
    document: Any = field(repr=False, compare=False)
    session: Any = field(repr=False, compare=False)
    issuer_pid: int
    owner_thread_id: int
    document_id: str
    before_version: int
    document_version: int
    updated_at: float


@dataclass(frozen=True)
class _VerifiedDocumentCommitCandidate:
    """Path-free scalar view returned only after active-session verification."""

    document_id: str
    before_version: int
    document_version: int
    updated_at: float


_DOCUMENT_COMMIT_CANDIDATES: weakref.WeakKeyDictionary[
    _DocumentCommitCandidate, _DocumentCommitCandidateRecord
] = weakref.WeakKeyDictionary()


def _reset_mutation_state_after_fork() -> None:
    """A child must never inherit a usable rollback/candidate authority."""

    global _MUTATION_SESSION_LOCAL
    global _HISTORY_REPLAY_LOCAL
    global _HISTORY_OPERATION_LOCAL
    global _MUTATION_SESSION_REGISTRY_LOCK
    global _MUTATION_SESSION_REGISTRY
    global _DOCUMENT_COMMIT_CANDIDATES
    _MUTATION_SESSION_LOCAL = threading.local()
    _HISTORY_REPLAY_LOCAL = threading.local()
    _HISTORY_OPERATION_LOCAL = threading.local()
    _MUTATION_SESSION_REGISTRY_LOCK = threading.RLock()
    _MUTATION_SESSION_REGISTRY = {}
    _DOCUMENT_COMMIT_CANDIDATES = weakref.WeakKeyDictionary()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_mutation_state_after_fork)


def _mutation_session_stacks() -> dict[int, list[Any]]:
    """Per-thread LIFO stacks; kept outside Document snapshot state."""
    stacks = getattr(_MUTATION_SESSION_LOCAL, "stacks", None)
    if stacks is None:
        stacks = {}
        _MUTATION_SESSION_LOCAL.stacks = stacks
    return stacks


@contextmanager
def _history_replay_scope(document: "Document") -> Iterator[None]:
    """Mark only the lexical body of framework history application."""
    stack = getattr(_HISTORY_REPLAY_LOCAL, "stack", None)
    if stack is None:
        stack = []
        _HISTORY_REPLAY_LOCAL.stack = stack
    stack.append(document)
    try:
        yield
    finally:
        if not stack or stack[-1] is not document:
            raise ZeError(
                "history_replay_scope_violation",
                "history replay scope stack was corrupted",
                context={"document_id": document.id},
            )
        stack.pop()


def _history_replay_scope_active(document: "Document") -> bool:
    stack = getattr(_HISTORY_REPLAY_LOCAL, "stack", ())
    return bool(stack) and stack[-1] is document


@contextmanager
def _history_operation_scope(
    document: "Document",
    operation: Any,
    *,
    actor: Actor,
    controller: Any = None,
    source_ticket: Any = None,
    _capability: Any = None,
) -> Iterator[None]:
    """Lexically bind one issued history Operation to its exact replay call."""
    if _capability is not _HISTORY_REPLAY_CAPABILITY:
        raise ZeError(
            "history_replay_forbidden",
            "history Operation binding requires framework replay authority",
        )
    protected = bool(object.__getattribute__(document, "_mutation_policies"))
    if protected:
        validate_history_replay_ticket(
            source_ticket,
            document=document,
            operation=operation,
            controller=controller,
        )
    actor_payload = actor.to_dict()
    stack = getattr(_HISTORY_OPERATION_LOCAL, "stack", None)
    if stack is None:
        stack = []
        _HISTORY_OPERATION_LOCAL.stack = stack
    binding = (
        document,
        operation,
        actor_payload,
        controller,
        source_ticket,
    )
    stack.append(binding)
    try:
        yield
        # 票据生命周期与签发侧对称:只要本次 replay 持有已签发的 source ticket,退出时就必须
        # 推进到 REPLAYED —— 提交预检(history_authority.prepare_store)对所有文档一律要求
        # REPLAYED,而 ticket 的签发/绑定(select_history_source/bind_replay)并不看 protected。
        # 旧条件 `if protected` 使未声明 mutation policy 的普通文档票据停在 BOUND,
        # 首次 undo/redo 100% 在 preflight 撞 history_ticket_state_invalid(3.60.1 修复)。
        # protected 专属的仍是上方 validate_history_replay_ticket(策略台账校验)。
        if source_ticket is not None:
            mark_history_replay_ticket(
                source_ticket,
                document=document,
                operation=operation,
                controller=controller,
            )
    finally:
        if not stack or stack[-1] is not binding:
            raise ZeError(
                "history_replay_scope_violation",
                "history Operation binding stack was corrupted",
                context={"document_id": document.id},
            )
        stack.pop()


def _active_history_operation_binding(
    document: "Document",
) -> tuple[Any, dict[str, Any], Any, Any] | None:
    stack = getattr(_HISTORY_OPERATION_LOCAL, "stack", ())
    if not stack or stack[-1][0] is not document:
        return None
    return (
        stack[-1][1],
        stack[-1][2],
        stack[-1][3],
        stack[-1][4],
    )


def _require_active_history_policy_coverage(
    document: "Document",
) -> Any | None:
    """Fail closed before privileged replay on a policy-protected Document."""
    if not document._mutation_policies:
        return None
    document._require_mutation_policy_live_seal()
    stack = _mutation_session_stacks().get(id(document), [])
    coverage = next(
        (
            candidate for candidate in reversed(stack)
            if candidate._policy_coverage_active
            and candidate._policy_coverage_enforced
        ),
        None,
    )
    if coverage is None:
        raise ZeError(
            "history_replay_coordinator_required",
            "protected history replay requires active commit-coordinator coverage",
            context={"document_id": document.id},
        )
    if not stack or stack[-1] is not coverage:
        raise ZeError(
            "history_replay_scope_violation",
            "history replay cannot be nested inside another mutation segment",
            context={"document_id": document.id},
        )
    return coverage


def _record_history_policy_coverage(
    document: "Document",
    before: _ProtectedMutationFingerprint,
    patches: tuple[Any, ...],
    *,
    actor: Actor,
) -> None:
    coverage = _require_active_history_policy_coverage(document)
    if coverage is None:
        return
    binding = _active_history_operation_binding(document)
    if binding is None:
        raise ZeError(
            "mutation_operation_ledger_mismatch",
            "protected history replay requires its issued Operation",
            context={"reason": "history_operation_missing"},
        )
    (
        operation,
        issued_actor_payload,
        controller,
        source_ticket,
    ) = binding
    validate_history_replay_ticket(
        source_ticket,
        document=document,
        operation=operation,
        controller=controller,
    )
    actor_payload = actor.to_dict()
    if not _json_values_exact(issued_actor_payload, actor_payload):
        raise ZeError(
            "mutation_operation_authority_mismatch",
            "history replay actor changed after Operation issuance",
            context={"reason": "history_replay_actor_mismatch"},
        )
    coverage._record_history_policy_segment(
        before,
        patches=patches,
        actor_payload=actor_payload,
        operation=operation,
        history_source_ticket=source_ticket,
        _capability=_HISTORY_REPLAY_CAPABILITY,
    )


def _record_session_entity_admission(document: "Document", entity: Entity) -> None:
    """Journal a newly bound Entity in every active outer rollback snapshot."""
    state = object.__getattribute__(entity, "__dict__")
    admission = state.get("_document_admission_state")
    if (
        state.get("_document_owner") is not document
        or type(admission) is not tuple
        or len(admission) != 2
        or type(admission[0]) is not dict
        or not callable(admission[1])
    ):
        raise ZeError(
            "mutation_entity_admission_invalid",
            "newly admitted Entity did not retain its standalone authority",
            context={
                "document_id": document.id,
                "entity_type": entity.type,
                "entity_id": entity.id,
            },
        )
    for session in _mutation_session_stacks().get(id(document), []):
        snapshot = session._snapshot
        identity = id(entity)
        if _session_snapshot_contains_identity(session, identity):
            continue
        if identity not in session._admitted_entity_identities:
            snapshot._admitted_entities.append(_AdmittedEntityMutationState(
                entity=entity,
                schema_authority=admission[0],
                schema_bytes=_mutation_fingerprint_bytes(
                    admission[0], reason="mutation_entity_admission_schema_invalid",
                ),
                validator_authority=admission[1],
            ))
            session._admitted_entity_identities.add(identity)


def _guard_entity_not_reserved_by_foreign_session(
    target_document: "Document",
    entity: Entity,
) -> None:
    """Prevent cross-Document transfer while another rollback owns the Entity."""
    with _MUTATION_SESSION_REGISTRY_LOCK:
        for active in _MUTATION_SESSION_REGISTRY.values():
            for session in active:
                if session._document is target_document or not session.active:
                    continue
                identity = id(entity)
                if (
                    _session_snapshot_contains_identity(session, identity)
                    or identity in session._admitted_entity_identities
                ):
                    raise ZeError(
                        "mutation_entity_session_reserved",
                        "Entity is reserved by another Document mutation session",
                        context={
                            "target_document_id": target_document.id,
                            "owner_document_id": session.document_id,
                            "entity_id": entity.id,
                            "entity_type": entity.type,
                        },
                    )


def _session_snapshot_contains_identity(
    session: "DocumentMutationSession",
    identity: int,
) -> bool:
    """Build the O(all entities) identity index only for admission/transfer.

    Ordinary update commands never need this index; eagerly rebuilding it for
    both the coordinator and nested command snapshot was measurable overhead on
    large policy-opt-out Documents.
    """
    identities = session._snapshot_entity_identities
    if identities is None:
        identities = {
            id(record.entity) for record in session._snapshot._entities
        }
        session._snapshot_entity_identities = identities
    return identity in identities


def _reserve_mutation_session(
    document: "Document",
    session: "DocumentMutationSession",
    *,
    exclusive: bool,
) -> None:
    """Reserve one process-wide session slot before capturing its snapshot.

    Ordinary command sessions may nest on the same thread.  Runtime bootstrap
    uses an exclusive session: it must start from an unpublished head with no
    outer rollback token, and no later session may be opened until promotion or
    rollback closes it.  Cross-thread overlap is rejected rather than pretending
    independently captured snapshots can be finalized safely.
    """
    key = id(document)
    owner = threading.get_ident()
    with _MUTATION_SESSION_REGISTRY_LOCK:
        active = _MUTATION_SESSION_REGISTRY.get(key, [])
        if active and (
            exclusive
            or any(item._exclusive for item in active)
            or any(item._owner_thread_id != owner for item in active)
        ):
            raise ZeError(
                "mutation_session_busy",
                "Document already has an incompatible mutation session",
                context={
                    "document_id": document.id,
                    "reason": (
                        "exclusive_mutation_session_required"
                        if exclusive
                        else "mutation_session_owned_elsewhere"
                    ),
                },
            )
        _MUTATION_SESSION_REGISTRY.setdefault(key, []).append(session)


def _release_mutation_session(
    document: "Document",
    session: "DocumentMutationSession",
) -> None:
    key = id(document)
    with _MUTATION_SESSION_REGISTRY_LOCK:
        active = _MUTATION_SESSION_REGISTRY.get(key, [])
        if not active or active[-1] is not session:
            raise ZeError(
                "mutation_session_order_violation",
                "mutation sessions must finalize in process-wide LIFO order",
                context={
                    "document_id": document.id,
                    "state": session.state,
                    "action": "close",
                },
            )
        active.pop()
        if not active:
            _MUTATION_SESSION_REGISTRY.pop(key, None)


def _document_has_exclusive_mutation_session(document: "Document") -> bool:
    with _MUTATION_SESSION_REGISTRY_LOCK:
        return any(
            session._exclusive
            for session in _MUTATION_SESSION_REGISTRY.get(id(document), [])
        )


def _verify_atomic_document_commit_candidate(
    candidate: Any,
    document: "Document",
    session: "DocumentMutationSession",
    *,
    _capability: Any = None,
) -> _VerifiedDocumentCommitCandidate:
    """Verify one candidate while its exact exclusive session is still live."""

    if _capability is not _ATOMIC_DOCUMENT_COMMIT_CAPABILITY:
        raise ZeError(
            "document_atomic_commit_forbidden",
            "atomic Document commit capability is required",
            context={"reason": "atomic_commit_capability_required"},
        )
    if type(document) is not _document_type():
        raise TypeError("document must be an exact Document")
    if type(session) is not DocumentMutationSession:
        raise TypeError("session must be an exact DocumentMutationSession")
    if type(candidate) is not _DocumentCommitCandidate:
        raise ZeError(
            "document_commit_candidate_invalid",
            "Document commit candidate is not framework-issued",
            context={"reason": "candidate_type_invalid"},
        )
    if session._owner_pid != os.getpid():
        raise ZeError(
            "mutation_session_process_mismatch",
            "mutation session belongs to another process",
            context={"document_id": document.id},
        )
    if session._owner_thread_id != threading.get_ident():
        raise ZeError(
            "mutation_session_thread_mismatch",
            "mutation session belongs to another thread",
            context={"document_id": document.id},
        )
    with _MUTATION_SESSION_REGISTRY_LOCK:
        record = _DOCUMENT_COMMIT_CANDIDATES.get(candidate)
        active = _MUTATION_SESSION_REGISTRY.get(id(document), [])
        if (
            record is None
            or record.document is not document
            or record.session is not session
            or record.issuer_pid != os.getpid()
            or record.owner_thread_id != threading.get_ident()
            or session._state != "active"
            or not session._exclusive
            or session._atomic_candidate is not candidate
            or not active
            or active[-1] is not session
            or document._id != record.document_id
            or document._v != record.document_version
            or document._updated_at != record.updated_at
        ):
            raise ZeError(
                "document_commit_candidate_invalid",
                "Document commit candidate is no longer bound to the active head",
                context={"reason": "candidate_binding_invalid"},
            )
        return _VerifiedDocumentCommitCandidate(
            document_id=record.document_id,
            before_version=record.before_version,
            document_version=record.document_version,
            updated_at=record.updated_at,
        )


@dataclass(frozen=True)
class _PolicyOperationLedgerEntry:
    """Exact framework-issued values that may explain one protected segment."""

    kind: str
    operations: tuple[Any, ...]
    operation_bodies: tuple[bytes, ...]
    replayed_patches: tuple[Any, ...] = ()
    replayed_patch_bodies: tuple[bytes, ...] = ()
    history_source_ticket: Any = field(default=None, repr=False, compare=False)


class DocumentMutationSession:
    """One full-snapshot mutation attempt bound to one Document object.

    The commit coordinator creates a session before invoking a command handler.
    A proven durable commit calls ``commit()``; a safe pre-durability failure
    calls ``rollback()``.  Both are terminal, one-shot transitions so stale
    recovery tokens cannot undo a later mutation.
    """

    def __init__(self, document: Document, *, exclusive: bool = False) -> None:
        if not isinstance(document, _document_type()):
            raise TypeError("document must be Document")
        if type(exclusive) is not bool:
            raise TypeError("exclusive must be an exact bool")
        self._document = document
        self._exclusive = exclusive
        self._owner_pid = os.getpid()
        self._owner_thread_id = threading.get_ident()
        self._atomic_candidate: _DocumentCommitCandidate | None = None
        self._state = "initializing"
        existing_stack = _mutation_session_stacks().get(id(document), [])
        self._policy_validation_deferred = any(
            getattr(candidate, "_policy_command_authority_active", False) is True
            for candidate in reversed(existing_stack)
        )
        _reserve_mutation_session(document, self, exclusive=exclusive)
        try:
            self._snapshot = document._capture_mutation_snapshot(
                include_policy_fingerprints=not self._policy_validation_deferred,
            )
            self._protected_fingerprint = (
                _protected_mutation_fingerprint(document)
                if (
                    object.__getattribute__(document, "_mutation_policies")
                    and not self._policy_validation_deferred
                )
                else None
            )
        except BaseException:
            _release_mutation_session(document, self)
            self._state = "initialization_failed"
            raise
        self._snapshot_entity_identities: set[int] | None = None
        self._admitted_entity_identities: set[int] = set()
        self._policy_coverage_active = False
        self._policy_coverage_enforced = False
        self._policy_coverage_tail = self._protected_fingerprint
        self._policy_coverage_parent: DocumentMutationSession | None = None
        self._policy_command_segment_open = False
        self._policy_command_authority_active = False
        self._policy_segment_enforced = False
        self._policy_registry_authority: Any = None
        self._policy_registry_epoch: Any = None
        self._policy_registry_pid: int | None = None
        self._policy_root_command_id = ""
        self._policy_command_attestation: Any = None
        self._policy_operation_ledger: list[_PolicyOperationLedgerEntry] = []
        self._issued_nested_operations: list[tuple[Any, bytes]] = []
        self._state = "active"
        _mutation_session_stacks().setdefault(id(document), []).append(self)

    @property
    def document_id(self) -> str:
        return self._snapshot.document_id

    @property
    def state(self) -> str:
        return self._state

    @property
    def active(self) -> bool:
        return self._state == "active"

    @property
    def snapshot(self) -> DocumentMutationSnapshot:
        """Opaque checkpoint for diagnostics; ownership checks still apply."""
        return self._snapshot

    def validate_command_mutation(
        self,
        command_id: str,
        *,
        actor_payload: dict[str, Any],
        source: str,
        operations: tuple[Any, ...] | None = None,
    ) -> frozenset[tuple[str, str, str, str]] | None:
        """Validate the real post-handler graph before this session commits."""
        self._ensure_can_finalize("validate_command_mutation")
        if not self._policy_validation_deferred:
            self._require_bound_dispatch_authority()
        policies = object.__getattribute__(self._document, "_mutation_policies")
        if (
            policies is self._snapshot._mutation_policy_authority
            and type(policies) is dict
            and not policies
        ):
            # Mutation policy is explicitly opt-in.  The outer coordinator
            # still rejects a policy authority installed during the producer,
            # but an ordinary legacy command pays no Actor/delta/audit cost.
            self._policy_segment_enforced = False
            return frozenset()
        if self._policy_validation_deferred:
            if type(operations) is not tuple or not operations:
                raise ZeError(
                    "mutation_operation_ledger_mismatch",
                    "nested command must return one exact Operation sequence",
                    context={"reason": "nested_operation_sequence_invalid"},
                )
            self._validate_operation_authority(
                operations,
                actor_payload=actor_payload,
                source=source,
                command_id=command_id,
            )
            self._validate_issued_nested_operations(operations)
            # The exact outer business root owns the only graph-wide delta and
            # reversible-program validation.  This nested session still owns a
            # complete W04 rollback snapshot; only duplicate policy scans are
            # deferred.
            self._policy_segment_enforced = False
            return None
        actual_touches = self._document._authorize_command_mutation(
            self._snapshot,
            command_id=command_id,
            actor_payload=actor_payload,
            source=source,
        )
        claimed_protected = False
        if operations is not None:
            protected_types = set(self._document._mutation_policies)
            claimed_protected = any(
                getattr(patch, "entity_type", None) in protected_types
                for operation in operations
                for patch in (
                    tuple(getattr(operation, "patches", ()))
                    + tuple(getattr(operation, "reverse_patches", ()))
                )
            )
        self._policy_segment_enforced = bool(actual_touches) or claimed_protected
        if operations is not None and self._policy_segment_enforced:
            self._validate_operation_authority(
                operations,
                actor_payload=actor_payload,
                source=source,
                command_id=command_id,
            )
            self._validate_issued_nested_operations(operations)
        return actual_touches

    @staticmethod
    def _validate_operation_authority(
        operations: tuple[Any, ...],
        *,
        actor_payload: dict[str, Any],
        source: str,
        command_id: str,
    ) -> None:
        for operation in operations:
            if (
                getattr(operation, "source", None) != source
                or not _json_values_exact(
                    getattr(operation, "actor", None), actor_payload,
                )
            ):
                raise ZeError(
                    "mutation_operation_authority_mismatch",
                    "Operation actor/source must equal dispatch authority",
                    context={
                        "command_id": command_id,
                        "reason": "operation_actor_source_mismatch",
                    },
                )

    def _validate_issued_nested_operations(
        self,
        operations: tuple[Any, ...],
    ) -> None:
        cursor = -1
        for issued, issued_body in self._issued_nested_operations:
            positions = [
                index for index, operation in enumerate(operations)
                if operation is issued
            ]
            if len(positions) != 1 or positions[0] <= cursor:
                raise ZeError(
                    "mutation_operation_ledger_mismatch",
                    "outer command did not preserve every nested dispatch Operation",
                    context={"reason": "nested_operation_missing_or_reordered"},
                )
            try:
                current_body = _mutation_fingerprint_bytes(
                    issued.to_dict(),
                    reason="mutation_operation_body_invalid",
                )
            except (AttributeError, ZeError):
                raise ZeError(
                    "mutation_operation_ledger_mismatch",
                    "nested dispatch Operation changed after validation",
                    context={"reason": "nested_operation_body_changed"},
                ) from None
            if current_body != issued_body:
                raise ZeError(
                    "mutation_operation_ledger_mismatch",
                    "nested dispatch Operation changed after validation",
                    context={"reason": "nested_operation_body_changed"},
                )
            cursor = positions[0]

    def _record_nested_command_operations(
        self,
        operations: tuple[Any, ...],
        *,
        _capability: Any = None,
    ) -> None:
        """Bind child dispatch results to the enclosing command result."""
        self._ensure_can_finalize("record_nested_command_operations")
        if _capability is not _MUTATION_POLICY_COVERAGE_CAPABILITY:
            raise ZeError(
                "mutation_policy_coverage_forbidden",
                "nested operation coverage requires framework authority",
            )
        stack = _mutation_session_stacks().get(id(self._document), [])
        if not stack or stack[-1] is not self or len(stack) < 2:
            return
        parent = stack[-2]
        if parent._document is not self._document or not parent.active:
            raise ZeError(
                "mutation_policy_coverage_invalid",
                "nested command session has no active parent",
            )
        for operation in operations:
            try:
                body = _mutation_fingerprint_bytes(
                    operation.to_dict(), reason="mutation_operation_body_invalid",
                )
            except (AttributeError, ZeError):
                raise ZeError(
                    "mutation_operation_ledger_mismatch",
                    "nested command returned an invalid Operation",
                    context={"reason": "nested_operation_body_invalid"},
                ) from None
            parent._issued_nested_operations.append((operation, body))

    def assert_unchanged(self, *, stage: str) -> None:
        """Require a dispatch preflight callback to have remained read-only."""
        self._ensure_can_finalize("assert_unchanged")
        if type(stage) is not str or not stage:
            raise TypeError("stage must be a non-empty string")
        if not _document_matches_mutation_snapshot(self._document, self._snapshot):
            raise ZeError(
                "command_preflight_mutated_document",
                "command poll/governor/confirmation preflight must be read-only",
                context={"document_id": self.document_id, "stage": stage},
            )

    def _activate_policy_coverage(self, *, _capability: Any = None) -> None:
        """Turn this outer coordinator session into a protected-delta ledger."""
        self._ensure_can_finalize("activate_policy_coverage")
        if _capability is not _MUTATION_POLICY_COVERAGE_CAPABILITY:
            raise ZeError(
                "mutation_policy_coverage_forbidden",
                "mutation policy coverage requires framework authority",
            )
        if self._policy_coverage_active:
            raise ZeError(
                "mutation_policy_coverage_invalid",
                "mutation policy coverage is already active",
            )
        self._policy_coverage_active = True
        self._policy_coverage_enforced = bool(self._document._mutation_policies)
        self._policy_coverage_tail = self._protected_fingerprint
        self._policy_operation_ledger = []

    def _begin_command_policy_segment(
        self,
        *,
        registry_authority: Any = None,
        registry_epoch: Any = None,
        registry_pid: int | None = None,
        command_id: str = "",
        command_attestation: Any = None,
        _capability: Any = None,
    ) -> None:
        """Bind one exact root dispatch segment to the active coordinator tail."""
        self._ensure_can_finalize("begin_command_policy_segment")
        if _capability is not _MUTATION_POLICY_COVERAGE_CAPABILITY:
            raise ZeError(
                "mutation_policy_coverage_forbidden",
                "command coverage requires framework authority",
            )
        if self._policy_command_segment_open:
            raise ZeError(
                "mutation_policy_coverage_invalid",
                "this command session already opened a coverage segment",
            )
        if self._policy_command_authority_active:
            raise ZeError(
                "mutation_policy_coverage_invalid",
                "this command session already owns mutation authority",
            )
        stack = _mutation_session_stacks().get(id(self._document), [])
        if not stack or stack[-1] is not self:
            raise ZeError(
                "mutation_policy_coverage_invalid",
                "command coverage session is not the active mutation scope",
            )
        if self._document._mutation_policy_dispatch_required():
            self._document._require_mutation_policy_dispatch_authority(
                registry_authority=registry_authority,
                registry_epoch=registry_epoch,
                registry_pid=registry_pid,
                command_id=command_id,
                command_attestation=command_attestation,
            )
            self._policy_registry_authority = registry_authority
            self._policy_registry_epoch = registry_epoch
            self._policy_registry_pid = registry_pid
            self._policy_root_command_id = command_id
            self._policy_command_attestation = command_attestation
        elif any(value is not None for value in (
            registry_authority,
            registry_epoch,
            registry_pid,
            command_attestation,
        )) or command_id:
            raise ZeError(
                "mutation_policy_coverage_invalid",
                "unprotected command segment received registry authority",
                context={"reason": "unexpected_registry_authority"},
            )
        self._policy_command_authority_active = True
        parent = next(
            (
                candidate for candidate in reversed(stack[:-1])
                if candidate._policy_coverage_active
                and candidate._policy_coverage_enforced
            ),
            None,
        )
        if parent is None:
            return
        if not _protected_fingerprints_match(
            parent._policy_coverage_tail, self._protected_fingerprint,
        ):
            raise ZeError(
                "mutation_policy_uncovered_delta",
                "protected Document state changed outside an authorized segment",
                context={
                    "document_id": self.document_id,
                    "reason": "mutation_before_command_uncovered",
                },
            )
        self._policy_coverage_parent = parent
        self._policy_command_segment_open = True

    def _require_bound_dispatch_authority(self) -> None:
        if not self._document._mutation_policy_dispatch_required():
            return
        registry = self._policy_registry_authority
        current_authority = getattr(
            registry, "_mutation_policy_dispatch_attestation", None,
        )
        if not callable(current_authority):
            raise ZeError(
                "mutation_policy_registry_authority_mismatch",
                "bound registry no longer exposes its dispatch attestation",
                context={
                    "command_id": self._policy_root_command_id,
                    "reason": "bound_registry_attestation_missing",
                },
            )
        current = current_authority(self._policy_root_command_id)
        if (
            type(current) is not tuple
            or len(current) != 3
            or current[0] is not self._policy_registry_epoch
            or current[1] != self._policy_registry_pid
            or current[2] is not self._policy_command_attestation
        ):
            raise ZeError(
                "mutation_policy_registry_authority_mismatch",
                "bound registry authority changed during the command segment",
                context={
                    "command_id": self._policy_root_command_id,
                    "reason": "bound_registry_attestation_changed",
                },
            )
        self._document._require_mutation_policy_dispatch_authority(
            registry_authority=registry,
            registry_epoch=current[0],
            registry_pid=current[1],
            command_id=self._policy_root_command_id,
            command_attestation=current[2],
        )

    def _complete_command_policy_segment(
        self,
        *,
        operations: tuple[Any, ...],
        protected_segment: bool,
        _capability: Any = None,
    ) -> None:
        """Advance the coordinator tail after this root command was authorized."""
        self._ensure_can_finalize("complete_command_policy_segment")
        if _capability is not _MUTATION_POLICY_COVERAGE_CAPABILITY:
            raise ZeError(
                "mutation_policy_coverage_forbidden",
                "command coverage requires framework authority",
            )
        self._require_bound_dispatch_authority()
        parent = self._policy_coverage_parent
        if parent is None:
            self._policy_command_authority_active = False
            return
        if not self._policy_command_segment_open or not parent.active:
            raise ZeError(
                "mutation_policy_coverage_invalid",
                "command coverage segment is not active",
            )
        if type(protected_segment) is not bool:
            raise TypeError("protected_segment must be an exact bool")
        if protected_segment is not self._policy_segment_enforced:
            raise ZeError(
                "mutation_policy_coverage_invalid",
                "command coverage classification changed after validation",
            )
        if not protected_segment:
            self._policy_command_segment_open = False
            self._policy_coverage_parent = None
            self._policy_command_authority_active = False
            return
        if not _protected_fingerprints_match(
            parent._policy_coverage_tail, self._protected_fingerprint,
        ):
            raise ZeError(
                "mutation_policy_uncovered_delta",
                "protected Document state changed outside the root command segment",
                context={
                    "document_id": self.document_id,
                    "reason": "mutation_command_segment_discontinuous",
                },
            )
        if type(operations) is not tuple or not operations:
            raise ZeError(
                "mutation_operation_ledger_mismatch",
                "root command coverage requires a non-empty Operation tuple",
                context={"reason": "root_operation_sequence_invalid"},
            )
        bodies: list[bytes] = []
        for operation in operations:
            try:
                bodies.append(_mutation_fingerprint_bytes(
                    operation.to_dict(), reason="mutation_operation_body_invalid",
                ))
            except (AttributeError, ZeError):
                raise ZeError(
                    "mutation_operation_ledger_mismatch",
                    "root command returned an invalid Operation",
                    context={"reason": "root_operation_body_invalid"},
                ) from None
        parent._policy_coverage_tail = _protected_mutation_fingerprint(self._document)
        parent._policy_operation_ledger.append(_PolicyOperationLedgerEntry(
            kind="command",
            operations=operations,
            operation_bodies=tuple(bodies),
        ))
        self._policy_command_segment_open = False
        self._policy_coverage_parent = None
        self._policy_command_authority_active = False

    def _record_history_policy_segment(
        self,
        before: _ProtectedMutationFingerprint,
        *,
        patches: tuple[Any, ...],
        actor_payload: dict[str, Any],
        operation: Any,
        history_source_ticket: Any,
        _capability: Any = None,
    ) -> None:
        """Bind one replay to its exact framework-issued history Operation."""
        self._ensure_can_finalize("record_history_policy_segment")
        if _capability is not _HISTORY_REPLAY_CAPABILITY:
            raise ZeError(
                "mutation_policy_coverage_forbidden",
                "history coverage requires replay authority",
            )
        if not self._policy_coverage_active:
            return
        if not self._policy_coverage_enforced:
            return
        validate_history_replay_ticket(
            history_source_ticket,
            document=self._document,
            operation=operation,
        )
        if not _protected_fingerprints_match(self._policy_coverage_tail, before):
            raise ZeError(
                "mutation_policy_uncovered_delta",
                "protected Document state changed before history replay",
                context={
                    "document_id": self.document_id,
                    "reason": "mutation_before_history_uncovered",
                },
            )
        if type(patches) is not tuple or not patches:
            raise ZeError(
                "mutation_operation_ledger_mismatch",
                "history coverage requires a non-empty Patch tuple",
                context={"reason": "history_patch_sequence_invalid"},
            )
        bodies: list[bytes] = []
        for patch in patches:
            try:
                bodies.append(_mutation_fingerprint_bytes(
                    patch.to_dict(), reason="mutation_operation_patch_value_invalid",
                ))
            except (AttributeError, ZeError):
                raise ZeError(
                    "mutation_operation_ledger_mismatch",
                    "history replay contains an invalid Patch",
                    context={"reason": "history_patch_body_invalid"},
                ) from None
        try:
            operation_patches = operation.patches
            operation_body = _mutation_fingerprint_bytes(
                operation.to_dict(), reason="mutation_operation_body_invalid",
            )
        except (AttributeError, ZeError):
            raise ZeError(
                "mutation_operation_ledger_mismatch",
                "history replay requires one valid issued Operation",
                context={"reason": "history_operation_body_invalid"},
            ) from None
        if (
            type(operation_patches) is not tuple
            or len(operation_patches) != len(patches)
            or any(
                operation_patch is not replayed_patch
                for operation_patch, replayed_patch
                in zip(operation_patches, patches)
            )
        ):
            raise ZeError(
                "mutation_operation_ledger_mismatch",
                "history Operation must own the exact replayed Patch sequence",
                context={"reason": "history_patch_sequence_mismatch"},
            )
        if (
            getattr(operation, "kind", None) != "rollback"
            or type(getattr(operation, "command_id", None)) is not str
            or not operation.command_id
        ):
            raise ZeError(
                "mutation_operation_ledger_mismatch",
                "history replay requires a named rollback Operation",
                context={"reason": "history_operation_kind_invalid"},
            )
        if (
            type(actor_payload) is not dict
            or getattr(operation, "source", None) != actor_payload.get("source")
            or not _json_values_exact(
                getattr(operation, "actor", None), actor_payload,
            )
        ):
            raise ZeError(
                "mutation_operation_authority_mismatch",
                "history Operation actor/source must equal replay authority",
                context={"reason": "history_operation_actor_source_mismatch"},
            )
        self._policy_coverage_tail = _protected_mutation_fingerprint(self._document)
        self._policy_operation_ledger.append(_PolicyOperationLedgerEntry(
            kind="history",
            operations=(operation,),
            operation_bodies=(operation_body,),
            replayed_patches=patches,
            replayed_patch_bodies=tuple(bodies),
            history_source_ticket=history_source_ticket,
        ))

    def _validate_policy_coverage(
        self,
        *,
        operations: tuple[Any, ...],
        _capability: Any = None,
    ) -> Any:
        """Require the producer's final protected state to equal its coverage tail."""
        self._ensure_can_finalize("validate_policy_coverage")
        if _capability is not _MUTATION_POLICY_COVERAGE_CAPABILITY:
            raise ZeError(
                "mutation_policy_coverage_forbidden",
                "mutation policy coverage requires framework authority",
            )
        if not self._policy_coverage_active:
            raise ZeError(
                "mutation_policy_coverage_invalid",
                "mutation policy coverage was not activated",
            )
        if not self._policy_coverage_enforced:
            if self._document._mutation_policies:
                raise ZeError(
                    "mutation_policy_uncovered_delta",
                    "producer installed mutation policy authority at runtime",
                    context={
                        "document_id": self.document_id,
                        "reason": "mutation_policy_runtime_install_forbidden",
                    },
                )
            return False
        if type(operations) is not tuple or not operations:
            raise ZeError(
                "mutation_operation_ledger_mismatch",
                "producer must return a non-empty exact Operation sequence",
                context={"reason": "producer_operation_sequence_invalid"},
            )
        current = _protected_mutation_fingerprint(self._document)
        if not _protected_fingerprints_match(self._policy_coverage_tail, current):
            raise ZeError(
                "mutation_policy_uncovered_delta",
                "protected Document state changed outside an authorized segment",
                context={
                    "document_id": self.document_id,
                    "reason": "mutation_after_authorized_segment_uncovered",
                },
            )
        self._document._validate_mutation_policy_cardinality()

        cursor = -1
        matched_operation_positions: set[int] = set()
        has_history_segment = False
        for entry in self._policy_operation_ledger:
            if entry.kind == "command":
                for issued, issued_body in zip(
                    entry.operations, entry.operation_bodies,
                ):
                    positions = [
                        index for index, returned in enumerate(operations)
                        if index > cursor and returned is issued
                    ]
                    if len(positions) != 1:
                        raise ZeError(
                            "mutation_operation_ledger_mismatch",
                            "producer omitted, duplicated, or reordered a root Operation",
                            context={"reason": "root_operation_missing"},
                        )
                    position = positions[0]
                    returned = operations[position]
                    try:
                        returned_body = _mutation_fingerprint_bytes(
                            returned.to_dict(),
                            reason="mutation_operation_body_invalid",
                        )
                    except (AttributeError, ZeError):
                        raise ZeError(
                            "mutation_operation_ledger_mismatch",
                            "producer returned an invalid Operation",
                            context={"reason": "returned_operation_body_invalid"},
                        ) from None
                    if returned is not issued or returned_body != issued_body:
                        raise ZeError(
                            "mutation_operation_ledger_mismatch",
                            "producer changed, reordered, or replaced a root Operation",
                            context={"reason": "root_operation_identity_mismatch"},
                        )
                    cursor = position
                    matched_operation_positions.add(position)
                continue
            if entry.kind != "history":
                raise ZeError(
                    "mutation_operation_ledger_mismatch",
                    "producer history Operation sequence is incomplete",
                    context={"reason": "history_operation_missing"},
                )
            has_history_segment = True
            if (
                len(entry.operations) != 1
                or len(entry.operation_bodies) != 1
            ):
                raise ZeError(
                    "mutation_operation_ledger_mismatch",
                    "history coverage ledger is malformed",
                    context={"reason": "history_operation_ledger_invalid"},
                )
            issued_operation = entry.operations[0]
            history_positions = [
                index for index, returned_operation in enumerate(operations)
                if index > cursor and returned_operation is issued_operation
            ]
            if len(history_positions) != 1:
                raise ZeError(
                    "mutation_operation_ledger_mismatch",
                    "producer omitted, duplicated, reordered, or replaced a history Operation",
                    context={"reason": "history_operation_identity_mismatch"},
                )
            position = history_positions[0]
            returned_operation = operations[position]
            try:
                returned_operation_body = _mutation_fingerprint_bytes(
                    returned_operation.to_dict(),
                    reason="mutation_operation_body_invalid",
                )
                returned_patches = returned_operation.patches
            except (AttributeError, ZeError):
                raise ZeError(
                    "mutation_operation_ledger_mismatch",
                    "producer returned an invalid history Operation",
                    context={"reason": "history_operation_body_invalid"},
                ) from None
            if returned_operation_body != entry.operation_bodies[0]:
                raise ZeError(
                    "mutation_operation_ledger_mismatch",
                    "producer changed the issued history Operation body",
                    context={"reason": "history_operation_body_mismatch"},
                )
            if (
                type(returned_patches) is not tuple
                or len(returned_patches) != len(entry.replayed_patches)
            ):
                raise ZeError(
                    "mutation_operation_ledger_mismatch",
                    "history Operation did not preserve the replayed Patch sequence",
                    context={"reason": "history_patch_sequence_mismatch"},
                )
            for returned, issued, issued_body in zip(
                returned_patches,
                entry.replayed_patches,
                entry.replayed_patch_bodies,
            ):
                try:
                    returned_body = _mutation_fingerprint_bytes(
                        returned.to_dict(),
                        reason="mutation_operation_patch_value_invalid",
                    )
                except (AttributeError, ZeError):
                    raise ZeError(
                        "mutation_operation_ledger_mismatch",
                        "history Operation contains an invalid Patch",
                        context={"reason": "history_patch_body_invalid"},
                    ) from None
                if returned is not issued or returned_body != issued_body:
                    raise ZeError(
                        "mutation_operation_ledger_mismatch",
                        "history Operation changed or replaced a replayed Patch",
                        context={"reason": "history_patch_identity_mismatch"},
                    )
            # Validate the opaque source ticket only after the private ledger
            # has produced the stable public error for any in-place body or
            # Patch mutation.  A valid body must still carry one exact replayed
            # ticket before the coordinator may persist it.
            require_history_ticket_replayed(
                entry.history_source_ticket,
                document=self._document,
                operation=issued_operation,
            )
            cursor = position
            matched_operation_positions.add(position)
        protected_types = set(self._document._mutation_policies)
        for index, operation in enumerate(operations):
            if index in matched_operation_positions:
                continue
            if any(
                getattr(patch, "entity_type", None) in protected_types
                for patch in (
                    tuple(getattr(operation, "patches", ()))
                    + tuple(getattr(operation, "reverse_patches", ()))
                )
            ):
                raise ZeError(
                    "mutation_operation_ledger_mismatch",
                    "producer returned a protected Operation outside an authorized segment",
                    context={"reason": "unissued_protected_operation_returned"},
                )
        # Root command segments have already undergone exact graph-delta and
        # reversible-program validation, so their exact seal avoids a third
        # replay.  History segments are identity/body sealed above, but unlike root
        # command dispatch they have not yet undergone the complete reversible
        # program audit.  Ask the coordinator to prove the combined forward and
        # reverse program against the outer snapshot before persistence.
        if has_history_segment:
            return True
        return _MUTATION_POLICY_COVERAGE_VALIDATED

    def commit(self) -> None:
        """Close the guard without changing the now-durable in-memory state."""
        self._ensure_can_finalize("commit")
        self._close("committed")

    def rollback(self) -> None:
        """Restore the exact pre-session state and close the guard.

        A failed restore is terminal and becomes ``mutation_rollback_failed``;
        callers can chain that error to the mutation/store error that triggered
        recovery without mistaking the in-memory state for clean.
        """
        self._ensure_can_finalize("rollback")
        try:
            self._document.restore_mutation_snapshot(self._snapshot)
        except Exception as exc:
            self._close("rollback_failed")
            raise ZeError(
                "mutation_rollback_failed",
                "could not restore the Document after a failed mutation",
                context={
                    "document_id": self.document_id,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "restore_error_code": getattr(exc, "code", None),
                },
            ) from exc
        self._close("rolled_back")

    def _prepare_atomic_commit_candidate(
        self,
        expected_version: int,
        *,
        _capability: Any = None,
    ) -> _DocumentCommitCandidate:
        """Advance one unpublished head exactly once for a future DB commit.

        This changes only the in-memory version/timestamp protected by this
        session.  It does not capture business rows, write SQLite, save files,
        or prove durability.  A NOT_COMMITTED result must call ``rollback``;
        only a separately exact COMMITTED result may call ``commit``.
        """

        if _capability is not _ATOMIC_DOCUMENT_COMMIT_CAPABILITY:
            raise ZeError(
                "document_atomic_commit_forbidden",
                "atomic Document commit capability is required",
                context={"reason": "atomic_commit_capability_required"},
            )
        self._ensure_can_finalize("prepare_atomic_commit")
        if not self._exclusive:
            raise ZeError(
                "mutation_session_exclusive_required",
                "atomic commit preparation requires an exclusive mutation session",
                context={"document_id": self.document_id},
            )
        if self._atomic_candidate is not None:
            raise ZeError(
                "document_commit_candidate_already_issued",
                "this mutation session already issued a commit candidate",
                context={"document_id": self.document_id},
            )

        document = self._document
        document._guard_document_version(expected_version)
        before_version = document._v
        next_version = document._next_document_version()
        now = time.time()
        if type(now) is not float or not math.isfinite(now):
            raise ZeError(
                "document_commit_clock_invalid",
                "system clock did not provide a finite timestamp",
                context={"document_id": self.document_id},
            )
        updated_at = max(now, document._updated_at, document._created_at)
        previous_updated_at = document._updated_at
        document._v = next_version
        document._updated_at = updated_at
        candidate = object.__new__(_DocumentCommitCandidate)
        try:
            record = _DocumentCommitCandidateRecord(
                document=document,
                session=self,
                issuer_pid=self._owner_pid,
                owner_thread_id=self._owner_thread_id,
                document_id=document.id,
                before_version=before_version,
                document_version=next_version,
                updated_at=updated_at,
            )
            with _MUTATION_SESSION_REGISTRY_LOCK:
                _DOCUMENT_COMMIT_CANDIDATES[candidate] = record
                self._atomic_candidate = candidate
        except BaseException:
            document._v = before_version
            document._updated_at = previous_updated_at
            raise
        return candidate

    def _ensure_can_finalize(self, action: str) -> None:
        if self._owner_pid != os.getpid():
            raise ZeError(
                "mutation_session_process_mismatch",
                "mutation session belongs to another process",
                context={"document_id": self.document_id, "action": action},
            )
        if self._owner_thread_id != threading.get_ident():
            raise ZeError(
                "mutation_session_thread_mismatch",
                "mutation session belongs to another thread",
                context={"document_id": self.document_id, "action": action},
            )
        if self._state != "active":
            raise ZeError(
                "mutation_session_closed",
                f"cannot {action} a mutation session in state {self._state!r}",
                context={
                    "document_id": self.document_id,
                    "state": self._state,
                    "action": action,
                },
            )
        stack = _mutation_session_stacks().get(id(self._document), [])
        if not stack or stack[-1] is not self:
            raise ZeError(
                "mutation_session_order_violation",
                "nested mutation sessions must finalize in last-in-first-out order",
                context={
                    "document_id": self.document_id,
                    "state": self._state,
                    "action": action,
                    "open_inner_sessions": max(0, len(stack) - 1),
                },
            )
        with _MUTATION_SESSION_REGISTRY_LOCK:
            active = _MUTATION_SESSION_REGISTRY.get(id(self._document), [])
            if not active or active[-1] is not self:
                raise ZeError(
                    "mutation_session_order_violation",
                    "mutation sessions must finalize in process-wide LIFO order",
                    context={
                        "document_id": self.document_id,
                        "state": self._state,
                        "action": action,
                        "open_inner_sessions": max(0, len(active) - 1),
                    },
                )
            candidate = self._atomic_candidate
            if candidate is not None and action == "commit":
                record = _DOCUMENT_COMMIT_CANDIDATES.get(candidate)
                if (
                    record is None
                    or record.session is not self
                    or record.document is not self._document
                    or self._document._v != record.document_version
                    or self._document._updated_at != record.updated_at
                ):
                    raise ZeError(
                        "document_commit_candidate_invalid",
                        "prepared Document commit head changed before finalization",
                        context={"reason": "candidate_binding_invalid"},
                    )

    def _close(self, state: str) -> None:
        stack_key = id(self._document)
        stacks = _mutation_session_stacks()
        stack = stacks.get(stack_key)
        if not stack or stack[-1] is not self:
            raise ZeError(
                "mutation_session_order_invalid",
                "mutation sessions must close in strict LIFO order",
                context={"document_id": self.document_id},
            )
        stack.pop()
        if not stack:
            stacks.pop(stack_key, None)
        _release_mutation_session(self._document, self)
        candidate = self._atomic_candidate
        if candidate is not None:
            with _MUTATION_SESSION_REGISTRY_LOCK:
                _DOCUMENT_COMMIT_CANDIDATES.pop(candidate, None)
            self._atomic_candidate = None
        self._state = state
