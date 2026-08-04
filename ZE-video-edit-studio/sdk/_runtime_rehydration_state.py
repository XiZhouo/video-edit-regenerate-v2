"""Identity-bound carriers and the single runtime-load authority ledger.

This module is the bottom dependency of runtime rehydration.  It owns carrier
class identity, authority request/receipt records, in-flight state records, and
the one process-local issuance ledger.  Projection and coordinator modules may
consume this state; this module must never import either of them.
"""

from __future__ import annotations

import json
import threading
import weakref
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from . import _paths  # noqa: F401  (install flat core imports first)

from document import Document, DocumentMutationSession, ReadOnlySourceSpec  # noqa: E402
from entity import Entity, ZeError, _clone_json_value  # noqa: E402


AUTHORITY_WRITE_PROTOCOL = "exact_business_snapshot_cas_v1"
AUTHORITY_FENCE_PROTOCOL = "exact_business_head_fence_v1"
_MAX_SQLITE_INTEGER = 2**63 - 1
_AUTHORITY_OUTCOMES = frozenset(("committed", "not_durable", "unknown"))
_FENCE_OUTCOMES = frozenset(("current", "advanced", "unknown"))


def _fail(code: str, reason: str, message: str) -> None:
    raise ZeError(code, message, context={"reason": reason})


def _encode_json(value: Any) -> bytes:
    owned = _clone_json_value(value)
    return json.dumps(
        owned,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8", "strict")


def _decode_json(value: bytes) -> Any:
    return json.loads(value.decode("utf-8", "strict"))


@dataclass(frozen=True)
class ReadOnlyDeclarationSnapshot:
    name: str
    entity_type: str
    loader_path: str


class RegistryDeclarationSnapshot:
    """Issuance-ledger handle for one complete runtime declaration manifest."""

    __slots__ = ("__weakref__",)

    @property
    def schema_version(self) -> str:
        return _LEDGER.declaration(self).schema_version

    @property
    def runtime_generation(self) -> str:
        return _LEDGER.declaration(self).runtime_generation

    @property
    def schema(self) -> dict[str, Any]:
        value = _decode_json(_LEDGER.declaration(self).schema_bytes)
        if type(value) is not dict:
            _fail(
                "runtime_declaration_invalid",
                "runtime_schema_authority_invalid",
                "runtime schema authority is not an exact object",
            )
        return value

    @property
    def mutation_policies(self) -> dict[str, Any]:
        """Detached runtime-only entity mutation authority.

        Policies intentionally travel with the deployment declaration rather
        than the business snapshot.  Returning a decoded copy keeps callers
        from mutating the issuance ledger's authority bytes in place.
        """
        value = _decode_json(_LEDGER.declaration(self).mutation_policy_bytes)
        if type(value) is not dict:
            _fail(
                "runtime_declaration_invalid",
                "runtime_mutation_policy_authority_invalid",
                "runtime mutation policy authority is not an exact object",
            )
        return value

    @property
    def collaboration_mode(self) -> str:
        return _LEDGER.declaration(self).collaboration_mode

    @property
    def save_policy(self) -> str | None:
        return _LEDGER.declaration(self).save_policy

    @property
    def collaboration_units(self) -> tuple[tuple[str, bool], ...]:
        return _LEDGER.declaration(self).collaboration_units

    @property
    def harvest_rules(self) -> list[dict[str, Any]]:
        value = _decode_json(_LEDGER.declaration(self).harvest_bytes)
        if type(value) is not list:
            _fail(
                "runtime_declaration_invalid",
                "runtime_harvest_authority_invalid",
                "runtime harvest authority is not an exact array",
            )
        return value

    @property
    def read_only_sources(self) -> tuple[ReadOnlyDeclarationSnapshot, ...]:
        return tuple(
            ReadOnlyDeclarationSnapshot(*source)
            for source in _LEDGER.declaration(self).read_only_sources
        )

    @property
    def migrations(self) -> tuple[Callable[[Document], bool], ...]:
        return _LEDGER.declaration(self).migrations

    @property
    def digest(self) -> bytes:
        return _LEDGER.declaration(self).digest


class DetachedBusinessHead:
    """One-shot handle to a verified, unpublished canonical Document."""

    __slots__ = ("__weakref__",)

    @property
    def project_id(self) -> str:
        return _LEDGER.detached(self).project_id

    @property
    def generation(self) -> int:
        return _LEDGER.detached(self).generation

    @property
    def origin(self) -> str:
        return _LEDGER.detached(self).origin


class RuntimeReadyHead:
    """One-shot admission handle; it intentionally does not expose Document."""

    __slots__ = ("__weakref__",)

    @property
    def project_id(self) -> str:
        return _LEDGER.ready(self, require_issued=False).project_id

    @property
    def generation(self) -> int:
        return _LEDGER.ready(self, require_issued=False).generation

    @property
    def origin(self) -> str:
        return _LEDGER.ready(self, require_issued=False).origin

    @property
    def authority_proof(self) -> str:
        return _LEDGER.ready(self, require_issued=False).authority_proof

    @property
    def document_version(self) -> int:
        return _LEDGER.ready(self, require_issued=False).document_version

    @property
    def business_root(self) -> bytes:
        return _LEDGER.ready(self, require_issued=False).business_root

    @property
    def declaration_digest(self) -> bytes:
        return _LEDGER.ready(self, require_issued=False).declaration_digest

    @property
    def authority_revision(self) -> str:
        return _LEDGER.ready(self, require_issued=False).authority_revision


class RuntimeAdmission:
    """Issued Context input binding one Document to one runtime manifest/revision."""

    __slots__ = ("__weakref__",)

    @property
    def document(self) -> Document:
        return _LEDGER.admission(self).document

    @property
    def project_id(self) -> str:
        return _LEDGER.admission(self).project_id

    @property
    def generation(self) -> int:
        return _LEDGER.admission(self).generation

    @property
    def origin(self) -> str:
        return _LEDGER.admission(self).origin

    @property
    def document_version(self) -> int:
        return _LEDGER.admission(self).document_version

    @property
    def business_root(self) -> bytes:
        return _LEDGER.admission(self).business_root

    @property
    def authority_revision(self) -> str:
        return _LEDGER.admission(self).authority_revision

    @property
    def authority_proof(self) -> str:
        return _LEDGER.admission(self).authority_proof

    @property
    def declaration_digest(self) -> bytes:
        return _LEDGER.admission(self).declaration_digest

    @property
    def declarations(self) -> RegistryDeclarationSnapshot:
        return _LEDGER.admission(self).declarations

    @property
    def collaboration_units(self) -> tuple[tuple[str, bool], ...]:
        return self.declarations.collaboration_units

    @property
    def runtime_generation(self) -> str:
        return self.declarations.runtime_generation


@dataclass(frozen=True)
class RuntimeAuthorityWrite:
    """Exact CAS request handed to the trusted persistence authority port."""

    request_identity: object = field(repr=False, compare=False)
    protocol: str
    project_id: str
    generation: int
    before_business_root: bytes = field(repr=False)
    before_document_version: int
    after_business_root: bytes = field(repr=False)
    after_document_version: int
    business_snapshot: Any = field(repr=False, compare=False)


@dataclass(frozen=True)
class RuntimeAuthorityReceipt:
    """Typed durability result returned synchronously by the authority port.

    ``outcome`` is one of ``committed``, ``not_durable``, or ``unknown``.
    A receipt is an architectural trust boundary, not protection against code
    that can execute arbitrary Python inside this process.
    """

    request_identity: object = field(repr=False, compare=False)
    outcome: str
    protocol: str
    project_id: str
    generation: int
    before_business_root: bytes = field(repr=False)
    before_document_version: int
    after_business_root: bytes = field(repr=False)
    after_document_version: int
    transaction_id: str = ""


@dataclass(frozen=True)
class RuntimeAuthorityFenceRequest:
    """Fresh-read fence required immediately before runtime publication."""

    request_identity: object = field(repr=False, compare=False)
    protocol: str
    project_id: str
    generation: int
    business_root: bytes = field(repr=False)
    document_version: int
    declaration_digest: bytes = field(repr=False)
    runtime_generation: str


@dataclass(frozen=True)
class RuntimeAuthorityFenceReceipt:
    request_identity: object = field(repr=False, compare=False)
    outcome: str
    protocol: str
    project_id: str
    generation: int
    business_root: bytes = field(repr=False)
    document_version: int
    declaration_digest: bytes = field(repr=False)
    runtime_generation: str
    authority_revision: str


@dataclass(frozen=True)
class _DeclarationRecord:
    schema_version: str
    runtime_generation: str
    schema_bytes: bytes
    mutation_policy_bytes: bytes
    collaboration_mode: str
    save_policy: str | None
    collaboration_units: tuple[tuple[str, bool], ...]
    harvest_bytes: bytes
    read_only_sources: tuple[tuple[str, str, str], ...]
    migrations: tuple[Callable[[Document], bool], ...]
    digest: bytes


@dataclass
class _DetachedRecord:
    project_id: str
    generation: int
    document: Document
    origin: str
    baseline_snapshot: Any
    state: str = "available"


@dataclass
class _DocumentLease:
    token: object
    state: str


@dataclass(frozen=True)
class _EntityInventory:
    key: str
    entity: Entity = field(compare=False)
    entity_type: str
    entity_id: str
    read_only: bool
    schema_bytes: bytes = field(repr=False)
    validator: Any = field(repr=False, compare=False)
    owner: Any = field(repr=False, compare=False)
    admission_schema_bytes: bytes | None = field(repr=False)
    admission_validator: Any = field(repr=False, compare=False)
    read_only_payload: bytes | None = field(repr=False)


@dataclass(frozen=True)
class _BucketInventory:
    entity_type: str
    bucket: dict[str, Entity] = field(repr=False, compare=False)
    entities: tuple[_EntityInventory, ...]


@dataclass(frozen=True)
class _SourceInventory:
    key: str
    spec: ReadOnlySourceSpec = field(repr=False, compare=False)
    name: str
    entity_type: str
    schema_bytes: bytes = field(repr=False)
    loader: Any = field(repr=False, compare=False)


@dataclass(frozen=True)
class _RuntimeInventory:
    collections: Any = field(repr=False, compare=False)
    buckets: tuple[_BucketInventory, ...]
    sources: Any = field(repr=False, compare=False)
    source_rows: tuple[_SourceInventory, ...]
    schema: Any = field(repr=False, compare=False)
    schema_bytes: bytes = field(repr=False)
    mutation_policies: Any = field(repr=False, compare=False)
    mutation_policy_bytes: bytes = field(repr=False)
    mutation_policy_sealed: bool
    mutation_policy_registry_authority: Any = field(repr=False, compare=False)
    mutation_policy_registry_epoch: Any = field(repr=False, compare=False)
    mutation_policy_registry_pid: int | None
    mutation_policy_command_authorities: Any = field(
        repr=False, compare=False,
    )
    mutation_policy_command_items: tuple[tuple[str, Any], ...] = field(
        repr=False, compare=False,
    )
    history_op_log_authority: Any = field(repr=False, compare=False)
    history_controller_authority: Any = field(repr=False, compare=False)
    history_authority_epoch: Any = field(repr=False, compare=False)
    history_authority_pid: int | None
    collaboration_mode: str
    save_policy: str | None
    harvest_rules: Any = field(repr=False, compare=False)
    harvest_bytes: bytes = field(repr=False)


@dataclass
class _CandidateRecord:
    project_id: str
    generation: int
    document: Document
    origin: str
    declarations: RegistryDeclarationSnapshot
    declaration: _DeclarationRecord
    session: DocumentMutationSession
    lease_token: object
    owner_thread_id: int
    before_snapshot: Any
    after_snapshot: Any
    before_business_root: bytes
    after_business_root: bytes
    before_document_version: int
    after_document_version: int
    canonical_changed: bool
    inventory: _RuntimeInventory
    read_only_loader_resolver: (
        Callable[[ReadOnlyDeclarationSnapshot], Callable[[], Iterable[dict[str, Any]]]]
        | None
    )
    state: str = "pending"


@dataclass
class _ReadyRecord:
    project_id: str
    generation: int
    document: Document
    origin: str
    authority_proof: str
    document_version: int
    business_root: bytes
    declaration_digest: bytes
    declarations: RegistryDeclarationSnapshot
    authority_revision: str
    snapshot: Any
    inventory: _RuntimeInventory
    lease_token: object
    state: str = "issued"


class _RuntimeIssuanceLedger:
    """Identity ledger; carriers contain no caller-overwritable authority facts."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._declarations: "weakref.WeakKeyDictionary[RegistryDeclarationSnapshot, _DeclarationRecord]" = weakref.WeakKeyDictionary()
        self._detached: "weakref.WeakKeyDictionary[DetachedBusinessHead, _DetachedRecord]" = weakref.WeakKeyDictionary()
        self._candidates: "weakref.WeakKeyDictionary[_RuntimeRehydrationCandidate, _CandidateRecord]" = weakref.WeakKeyDictionary()
        self._ready: "weakref.WeakKeyDictionary[RuntimeReadyHead, _ReadyRecord]" = weakref.WeakKeyDictionary()
        self._admissions: "weakref.WeakKeyDictionary[RuntimeAdmission, _ReadyRecord]" = weakref.WeakKeyDictionary()
        self._leases: "weakref.WeakKeyDictionary[Document, _DocumentLease]" = weakref.WeakKeyDictionary()

    def issue_declaration(self, record: _DeclarationRecord) -> RegistryDeclarationSnapshot:
        value = object.__new__(RegistryDeclarationSnapshot)
        with self._lock:
            self._declarations[value] = record
        return value

    def declaration(self, value: Any) -> _DeclarationRecord:
        if type(value) is not RegistryDeclarationSnapshot:
            _fail(
                "runtime_declaration_invalid",
                "runtime_declaration_carrier_invalid",
                "runtime declaration manifest was not issued by the framework",
            )
        with self._lock:
            record = self._declarations.get(value)
        if record is None:
            _fail(
                "runtime_declaration_invalid",
                "runtime_declaration_carrier_invalid",
                "runtime declaration manifest was not issued by the framework",
            )
        return record

    def issue_detached(self, record: _DetachedRecord) -> DetachedBusinessHead:
        value = object.__new__(DetachedBusinessHead)
        with self._lock:
            self._detached[value] = record
        return value

    def detached(self, value: Any) -> _DetachedRecord:
        if type(value) is not DetachedBusinessHead:
            _fail(
                "runtime_head_invalid",
                "runtime_detached_head_invalid",
                "runtime load requires a framework-issued detached business head",
            )
        with self._lock:
            record = self._detached.get(value)
        if record is None:
            _fail(
                "runtime_head_invalid",
                "runtime_detached_head_invalid",
                "runtime load requires a framework-issued detached business head",
            )
        return record

    def consume_and_reserve(
        self,
        value: DetachedBusinessHead,
    ) -> tuple[_DetachedRecord, object]:
        with self._lock:
            record = self._detached.get(value) if type(value) is DetachedBusinessHead else None
            if record is None or record.state != "available":
                _fail(
                    "runtime_head_invalid",
                    "runtime_detached_head_consumed",
                    "detached business heads are one-shot",
                )
            existing = self._leases.get(record.document)
            if existing is not None:
                reasons = {
                    "pending": "runtime_rehydration_already_pending",
                    "committing": "runtime_rehydration_already_pending",
                    "durable_committed": "runtime_rehydration_already_pending",
                    "ready": "runtime_document_already_ready",
                    "admitted": "runtime_document_already_admitted",
                    "quarantined": "runtime_document_quarantined",
                }
                _fail(
                    "runtime_rehydration_forbidden",
                    reasons.get(existing.state, "runtime_document_lease_conflict"),
                    "Document already has a runtime-load lifecycle",
                )
            token = object()
            record.state = "consumed"
            self._leases[record.document] = _DocumentLease(token, "pending")
            return record, token

    def release_unpublished_lease(self, document: Document, token: object) -> None:
        with self._lock:
            lease = self._leases.get(document)
            if lease is not None and lease.token is token and lease.state == "pending":
                self._leases.pop(document, None)

    def quarantine_unissued_lease(self, document: Document, token: object) -> None:
        with self._lock:
            lease = self._leases.get(document)
            if lease is not None and lease.token is token:
                lease.state = "quarantined"

    def issue_candidate(
        self,
        record: _CandidateRecord,
    ) -> "_RuntimeRehydrationCandidate":
        value = object.__new__(_RuntimeRehydrationCandidate)
        with self._lock:
            lease = self._leases.get(record.document)
            if lease is None or lease.token is not record.lease_token or lease.state != "pending":
                _fail(
                    "runtime_rehydration_failed",
                    "runtime_document_lease_lost",
                    "runtime Document lease changed during preparation",
                )
            self._candidates[value] = record
        return value

    def candidate(
        self,
        value: Any,
        *,
        states: tuple[str, ...] = ("pending",),
    ) -> _CandidateRecord:
        if type(value) is not _RuntimeRehydrationCandidate:
            _fail(
                "runtime_rehydration_forbidden",
                "runtime_rehydration_candidate_invalid",
                "runtime transition requires a framework-issued candidate",
            )
        with self._lock:
            record = self._candidates.get(value)
            if record is None or record.state not in states:
                _fail(
                    "runtime_rehydration_candidate_closed",
                    "runtime_rehydration_candidate_not_pending",
                    "runtime rehydration candidate is no longer pending",
                )
            lease = self._leases.get(record.document)
            expected_lease_state = "pending" if record.state == "pending" else record.state
            if (
                lease is None
                or lease.token is not record.lease_token
                or lease.state != expected_lease_state
            ):
                _fail(
                    "runtime_rehydration_failed",
                    "runtime_document_lease_lost",
                    "runtime Document lease changed during transition",
                )
            return record

    def mark_committing(self, value: "_RuntimeRehydrationCandidate") -> _CandidateRecord:
        with self._lock:
            record = self._candidates.get(value)
            if record is None or record.state != "pending":
                _fail(
                    "runtime_rehydration_candidate_closed",
                    "runtime_rehydration_candidate_not_pending",
                    "runtime rehydration candidate is no longer pending",
                )
            lease = self._leases.get(record.document)
            if lease is None or lease.token is not record.lease_token or lease.state != "pending":
                _fail(
                    "runtime_rehydration_failed",
                    "runtime_document_lease_lost",
                    "runtime Document lease changed during transition",
                )
            record.state = "committing"
            lease.state = "committing"
            return record

    def restore_pending(self, value: "_RuntimeRehydrationCandidate") -> None:
        with self._lock:
            record = self._candidates.get(value)
            if record is None or record.state != "committing":
                return
            lease = self._leases.get(record.document)
            if lease is None or lease.token is not record.lease_token:
                return
            record.state = "pending"
            lease.state = "pending"

    def mark_durable(self, value: "_RuntimeRehydrationCandidate") -> _CandidateRecord:
        with self._lock:
            record = self._candidates.get(value)
            if record is None or record.state != "committing":
                _fail(
                    "runtime_rehydration_failed",
                    "runtime_commit_state_invalid",
                    "runtime authority state changed during commit",
                )
            lease = self._leases.get(record.document)
            if lease is None or lease.token is not record.lease_token or lease.state != "committing":
                _fail(
                    "runtime_rehydration_failed",
                    "runtime_document_lease_lost",
                    "runtime Document lease changed during commit",
                )
            record.state = "durable_committed"
            lease.state = "durable_committed"
            return record

    def abort(self, value: "_RuntimeRehydrationCandidate") -> None:
        with self._lock:
            record = self._candidates.get(value)
            if record is None or record.state != "pending":
                _fail(
                    "runtime_rehydration_candidate_closed",
                    "runtime_rehydration_candidate_not_abortable",
                    "only a pre-durability pending candidate may be rolled back",
                )
            lease = self._leases.get(record.document)
            if lease is None or lease.token is not record.lease_token or lease.state != "pending":
                _fail(
                    "runtime_rehydration_failed",
                    "runtime_document_lease_lost",
                    "runtime Document lease changed before rollback",
                )
        try:
            record.session.rollback()
        except BaseException:
            self.quarantine(value, allowed=("pending",))
            raise
        with self._lock:
            record.state = "aborted"
            lease = self._leases.get(record.document)
            if lease is not None and lease.token is record.lease_token:
                self._leases.pop(record.document, None)

    def quarantine(
        self,
        value: "_RuntimeRehydrationCandidate",
        *,
        allowed: tuple[str, ...] = ("committing", "durable_committed"),
    ) -> None:
        with self._lock:
            record = self._candidates.get(value)
            if record is None or record.state not in allowed:
                return
            record.state = "quarantined"
            lease = self._leases.get(record.document)
            if lease is not None and lease.token is record.lease_token:
                lease.state = "quarantined"

    def issue_ready(
        self,
        value: "_RuntimeRehydrationCandidate",
        *,
        authority_proof: str,
        authority_revision: str,
        inventory: _RuntimeInventory,
    ) -> RuntimeReadyHead:
        with self._lock:
            record = self._candidates.get(value)
            if record is None or record.state not in ("pending", "durable_committed"):
                _fail(
                    "runtime_rehydration_failed",
                    "runtime_ready_transition_invalid",
                    "runtime candidate cannot enter ready state",
                )
            lease = self._leases.get(record.document)
            expected = record.state
            if lease is None or lease.token is not record.lease_token or lease.state != expected:
                _fail(
                    "runtime_rehydration_failed",
                    "runtime_document_lease_lost",
                    "runtime Document lease changed before ready issuance",
                )
            ready = object.__new__(RuntimeReadyHead)
            self._ready[ready] = _ReadyRecord(
                project_id=record.project_id,
                generation=record.generation,
                document=record.document,
                origin=record.origin,
                authority_proof=authority_proof,
                document_version=record.after_document_version,
                business_root=record.after_business_root,
                declaration_digest=record.declaration.digest,
                declarations=record.declarations,
                authority_revision=authority_revision,
                snapshot=record.after_snapshot,
                inventory=inventory,
                lease_token=record.lease_token,
            )
            record.state = "ready"
            lease.state = "ready"
            return ready

    def ready(self, value: Any, *, require_issued: bool) -> _ReadyRecord:
        if type(value) is not RuntimeReadyHead:
            _fail(
                "runtime_rehydration_forbidden",
                "runtime_ready_head_invalid",
                "Context admission requires a framework-issued RuntimeReadyHead",
            )
        with self._lock:
            record = self._ready.get(value)
            if record is None or (require_issued and record.state != "issued"):
                _fail(
                    "runtime_rehydration_forbidden",
                    (
                        "runtime_ready_head_already_admitted"
                        if record is not None and record.state == "admitted"
                        else "runtime_ready_head_invalid"
                    ),
                    "RuntimeReadyHead is invalid or has already been admitted",
                )
            lease = self._leases.get(record.document)
            expected = "ready" if record.state == "issued" else "admitted"
            if lease is None or lease.token is not record.lease_token or lease.state != expected:
                _fail(
                    "runtime_rehydration_forbidden",
                    "runtime_ready_head_lease_invalid",
                    "RuntimeReadyHead no longer owns its Document lifecycle",
                )
            return record

    def admit(self, value: RuntimeReadyHead) -> RuntimeAdmission:
        with self._lock:
            record = self._ready.get(value) if type(value) is RuntimeReadyHead else None
            if record is None or record.state != "issued":
                _fail(
                    "runtime_rehydration_forbidden",
                    (
                        "runtime_ready_head_already_admitted"
                        if record is not None and record.state == "admitted"
                        else "runtime_ready_head_invalid"
                    ),
                    "RuntimeReadyHead is invalid or has already been admitted",
                )
            lease = self._leases.get(record.document)
            if lease is None or lease.token is not record.lease_token or lease.state != "ready":
                _fail(
                    "runtime_rehydration_forbidden",
                    "runtime_ready_head_lease_invalid",
                    "RuntimeReadyHead no longer owns its Document lifecycle",
                )
            admission = object.__new__(RuntimeAdmission)
            self._admissions[admission] = record
            record.state = "admitted"
            lease.state = "admitted"
            return admission

    def admission(self, value: Any) -> _ReadyRecord:
        if type(value) is not RuntimeAdmission:
            _fail(
                "runtime_rehydration_forbidden",
                "runtime_admission_invalid",
                "Context construction requires a framework-issued RuntimeAdmission",
            )
        with self._lock:
            record = self._admissions.get(value)
            if record is None or record.state != "admitted":
                _fail(
                    "runtime_rehydration_forbidden",
                    "runtime_admission_invalid",
                    "Context construction requires a framework-issued RuntimeAdmission",
                )
            lease = self._leases.get(record.document)
            if lease is None or lease.token is not record.lease_token or lease.state != "admitted":
                _fail(
                    "runtime_rehydration_forbidden",
                    "runtime_admission_lease_invalid",
                    "RuntimeAdmission no longer owns its Document lifecycle",
                )
            return record

    def quarantine_ready(self, value: RuntimeReadyHead) -> None:
        with self._lock:
            record = self._ready.get(value) if type(value) is RuntimeReadyHead else None
            if record is None or record.state != "issued":
                return
            record.state = "quarantined"
            lease = self._leases.get(record.document)
            if lease is not None and lease.token is record.lease_token:
                lease.state = "quarantined"


_LEDGER = _RuntimeIssuanceLedger()


class _RuntimeRehydrationCandidate:
    __slots__ = ("__weakref__",)
