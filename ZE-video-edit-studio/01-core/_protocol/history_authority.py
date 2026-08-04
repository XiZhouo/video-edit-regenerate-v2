"""Process-local authority for canonical project history.

The durable truth remains the existing OpLog.  This module keeps only opaque
runtime bindings, trusted head facts, and one-shot transaction/source tickets
that connect bootstrap, CommitCoordinator, UndoController, and OpLog.append.
It intentionally imports no concrete framework leaf.
"""

from __future__ import annotations

import copy
import json
import os
import threading
import weakref
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any


_OUTCOME_UNKNOWN_REASONS = frozenset({
    "history_suffix_incomplete",
    "history_suffix_mismatch",
    "history_suffix_verification_failed",
    "history_discard_head_changed",
    "history_commit_outcome_indeterminate",
    "history_append_publish_mismatch",
    "history_append_outcome_indeterminate",
})


def _history_durability_phase(reason: str) -> str:
    if reason == "history_commit_outcome_indeterminate":
        return "commit_outcome"
    if reason in _OUTCOME_UNKNOWN_REASONS:
        return "post_write_verification"
    return "preflight"


def _copy_commit_fact(value: Any) -> Any:
    """Copy one JSON-like primitive without importing a receipt type."""
    if value is None or type(value) in {bool, int, float, str}:
        return value
    if type(value) in {list, tuple}:
        return [_copy_commit_fact(item) for item in value]
    if type(value) is dict and all(type(key) is str for key in value):
        return {key: _copy_commit_fact(item) for key, item in value.items()}
    raise TypeError("commit facts must contain only JSON-like primitive values")


class HistoryAuthorityError(RuntimeError):
    """Fail-closed runtime history protocol violation."""

    def __init__(
        self,
        reason: str,
        message: str,
        *,
        outcome_unknown: bool | None = None,
        durability_phase: str | None = None,
    ) -> None:
        super().__init__(message)
        if outcome_unknown is None:
            outcome_unknown = reason in _OUTCOME_UNKNOWN_REASONS
        if type(outcome_unknown) is not bool:
            raise TypeError("outcome_unknown must be boolean")
        if durability_phase is None:
            durability_phase = _history_durability_phase(reason)
        if type(durability_phase) is not str or not durability_phase:
            raise TypeError("durability_phase must be a non-empty string")
        self.code = "history_authority_mismatch"
        self.reason = reason
        self.outcome_unknown = outcome_unknown
        self.durability_phase = durability_phase
        self.commit_facts: dict[str, Any] = {}
        self.context = {
            "reason": reason,
            "outcome_unknown": outcome_unknown,
            "durability_phase": durability_phase,
        }

    def with_commit_facts(self, facts: dict[str, Any]) -> "HistoryAuthorityError":
        """Attach primitive receipt facts at the coordinator boundary.

        The neutral authority protocol intentionally knows nothing about
        ``CommitReceipt``.  Its one enrichment seam accepts only copied,
        JSON-like values so transports can preserve the otherwise-lost
        indeterminate receipt without importing a persistence leaf here.
        """
        if type(facts) is not dict or any(type(key) is not str for key in facts):
            raise TypeError("commit facts must be a string-keyed dict")
        copied = _copy_commit_fact(facts)
        self.commit_facts = copied
        self.context["commit_facts"] = copy.deepcopy(copied)
        return self


def _fail(reason: str, message: str) -> None:
    raise HistoryAuthorityError(reason, message)


def _canonical_body(value: Any) -> bytes:
    payload = value.to_dict() if hasattr(value, "to_dict") else value
    try:
        return json.dumps(
            payload, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ).encode("utf-8", "strict")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        _fail("history_body_invalid", "history value is not canonical JSON")


@dataclass(frozen=True)
class HistoryHead:
    count: int
    tail_hash: str
    path_identity: str
    stat_signature: tuple[int, ...] | None


class _HistoryBindCapability:
    __slots__ = ("__weakref__",)

    def __new__(cls):
        raise TypeError("history bind capabilities are framework-issued")


@dataclass
class _CapabilityRecord:
    owner: Any
    pid: int
    used: bool = False


_REGISTRY_LOCK = threading.RLock()
_CAPABILITIES: weakref.WeakKeyDictionary[
    _HistoryBindCapability, _CapabilityRecord
] = weakref.WeakKeyDictionary()


def _issue_history_bind_capability(*, bootstrap_owner: Any) -> _HistoryBindCapability:
    if bootstrap_owner is None:
        raise TypeError("bootstrap_owner is required")
    capability = object.__new__(_HistoryBindCapability)
    with _REGISTRY_LOCK:
        _CAPABILITIES[capability] = _CapabilityRecord(bootstrap_owner, os.getpid())
    return capability


class HistorySourceTicket:
    __slots__ = ("__weakref__",)

    def __new__(cls):
        raise TypeError("history source tickets are framework-issued")

    @property
    def state(self) -> str:
        return _source_record(self).state

    @property
    def source_entries(self) -> tuple[dict[str, Any], ...]:
        return tuple(copy.deepcopy(_source_record(self).source_entries))

    @property
    def source_entry(self) -> dict[str, Any]:
        record = _source_record(self)
        if len(record.source_entries) != 1:
            _fail("history_source_arity_invalid", "ticket has multiple source entries")
        return copy.deepcopy(record.source_entries[0])


@dataclass
class _SourceRecord:
    transaction: Any
    controller: Any
    purpose: str
    indexes: tuple[int, ...]
    source_entries: tuple[dict[str, Any], ...]
    source_bodies: tuple[bytes, ...]
    operation: Any = None
    operation_body: bytes = b""
    state: str = "ISSUED"


_SOURCE_RECORDS: weakref.WeakKeyDictionary[
    HistorySourceTicket, _SourceRecord
] = weakref.WeakKeyDictionary()


def _source_record(ticket: HistorySourceTicket) -> _SourceRecord:
    if type(ticket) is not HistorySourceTicket:
        _fail("history_ticket_invalid", "history source ticket is not framework-issued")
    with _REGISTRY_LOCK:
        record = _SOURCE_RECORDS.get(ticket)
    if record is None:
        _fail("history_ticket_invalid", "history source ticket is stale or foreign")
    return record


_ACTIVE_TRANSACTION: ContextVar[Any | None] = ContextVar(
    "ze_history_transaction", default=None,
)
_ACTIVE_WRITE: ContextVar[Any | None] = ContextVar(
    "ze_history_write", default=None,
)


def _oplog_snapshot(op_log: Any) -> tuple[tuple[dict[str, Any], ...], HistoryHead]:
    rows_snapshot = getattr(op_log, "_canonical_history_rows_snapshot", None)
    if callable(rows_snapshot):
        rows, count, tail_hash, path_identity, stat_signature = rows_snapshot()
        if (
            type(rows) is not tuple or type(count) is not int
            or type(tail_hash) is not str or type(path_identity) is not str
            or (stat_signature is not None and type(stat_signature) is not tuple)
        ):
            _fail("history_head_invalid", "OpLog returned an invalid canonical rows snapshot")
        return rows, HistoryHead(
            count, tail_hash, path_identity, stat_signature,
        )
    snapshot = getattr(op_log, "_canonical_history_snapshot", None)
    if not callable(snapshot):
        _fail("history_oplog_protocol_missing", "OpLog has no canonical snapshot port")
    rows, count, tail_hash = snapshot()
    if type(rows) is not tuple or type(count) is not int or type(tail_hash) is not str:
        _fail("history_head_invalid", "OpLog returned an invalid canonical head")
    path = getattr(op_log, "_path", None)
    path_identity = str(path.resolve()) if path is not None else ""
    stat_value = getattr(op_log, "_last_persisted_stat", None)
    stat_signature = tuple(stat_value) if type(stat_value) is tuple else None
    return rows, HistoryHead(count, tail_hash, path_identity, stat_signature)


def _oplog_head(op_log: Any) -> HistoryHead:
    probe = getattr(op_log, "_canonical_history_head_probe", None)
    if not callable(probe):
        return _oplog_snapshot(op_log)[1]
    count, tail_hash, path_identity, cached_stat, observed_stat = probe()
    if (
        type(count) is not int or type(tail_hash) is not str
        or type(path_identity) is not str
        or (cached_stat is not None and type(cached_stat) is not tuple)
        or (observed_stat is not None and type(observed_stat) is not tuple)
    ):
        _fail("history_head_invalid", "OpLog returned an invalid canonical head probe")
    # count/tail belong to cached_stat.  A different observed stat deliberately
    # makes the resulting head unequal to the trusted head; callers freeze
    # without spending an O(history) scan on an unauthorized writer.
    return HistoryHead(count, tail_hash, path_identity, observed_stat)


def _oplog_suffix(
    op_log: Any, start_count: int,
) -> tuple[tuple[dict[str, Any], ...], HistoryHead]:
    suffix_snapshot = getattr(op_log, "_canonical_history_suffix_snapshot", None)
    if not callable(suffix_snapshot):
        rows, head = _oplog_snapshot(op_log)
        return rows[start_count:], head
    rows, count, tail_hash, path_identity, stat_signature = suffix_snapshot(
        start_count,
    )
    if (
        type(rows) is not tuple or type(count) is not int
        or type(tail_hash) is not str or type(path_identity) is not str
        or (stat_signature is not None and type(stat_signature) is not tuple)
    ):
        _fail("history_head_invalid", "OpLog returned an invalid canonical suffix")
    return rows, HistoryHead(count, tail_hash, path_identity, stat_signature)


class HistoryAuthority:
    """Opaque diagnostic handle; never a transaction/write capability."""

    __slots__ = ("__weakref__",)

    def __new__(cls, *args, **kwargs):
        del args, kwargs
        raise TypeError("history authority handles are framework-issued")

    @property
    def state(self) -> str:
        record = _authority_record(self)
        record._refresh_lifecycle()
        return record.state

    @property
    def trusted_head(self) -> HistoryHead:
        return _authority_record(self).trusted_head

    @property
    def project_id(self) -> str:
        return _authority_record(self).project_id

    @property
    def generation(self) -> int:
        return _authority_record(self).generation

    @property
    def runtime_revision(self) -> str:
        return _authority_record(self).runtime_revision

    @property
    def freeze_reason(self) -> str:
        return _authority_record(self).freeze_reason

    def fresh_head(self) -> HistoryHead:
        """Read-only diagnostic probe; does not grant a transaction."""
        return _authority_record(self).fresh_head()


class _AuthorityRecord:
    """Module-private authority state containing weak exact runtime identities."""

    __slots__ = (
        "document_ref", "op_log_ref", "undo_controller_ref",
        "coordinator_ref", "store_ref", "project_id", "generation",
        "runtime_revision", "pid", "epoch", "trusted_head", "state",
        "freeze_reason", "coordinator_port", "replay_verifier",
        "active_transaction",
    )

    def __init__(
        self, *, document_ref: Any, op_log_ref: Any, undo_controller_ref: Any,
        coordinator_ref: Any, store_ref: Any, project_id: str,
        generation: int, runtime_revision: str, trusted_head: HistoryHead,
        replay_verifier: Any,
    ) -> None:
        self.document_ref = document_ref
        self.op_log_ref = op_log_ref
        self.undo_controller_ref = undo_controller_ref
        self.coordinator_ref = coordinator_ref
        self.store_ref = store_ref
        self.project_id = project_id
        self.generation = generation
        self.runtime_revision = runtime_revision
        self.pid = os.getpid()
        self.epoch = object()
        self.trusted_head = trusted_head
        self.state = "ACTIVE"
        self.freeze_reason = ""
        self.coordinator_port = None
        self.replay_verifier = replay_verifier
        self.active_transaction = None

    def _refresh_lifecycle(self) -> None:
        if self.state == "ACTIVE" and any(
            reference() is None
            for reference in (
                self.document_ref, self.op_log_ref, self.undo_controller_ref,
                self.coordinator_ref, self.store_ref,
            )
        ):
            # Losing one member of the exact graph is not permission to unlock
            # the canonical path.  Only capability-gated retirement releases
            # indexes; collection therefore freezes fail-closed.
            self.state = "FROZEN"
            self.freeze_reason = "history_runtime_collected"

    def _require_live(self) -> None:
        if self.pid != os.getpid():
            _fail("history_authority_pid_mismatch", "history authority belongs to another process")
        self._refresh_lifecycle()
        if self.state != "ACTIVE":
            _fail("history_authority_frozen", "history authority requires runtime reload")

    @staticmethod
    def _exact(reference: Any, reason: str) -> Any:
        value = reference()
        if value is None:
            _fail(reason, "history runtime identity is no longer live")
        return value

    @property
    def document(self) -> Any:
        return self._exact(self.document_ref, "history_document_retired")

    @property
    def op_log(self) -> Any:
        return self._exact(self.op_log_ref, "history_oplog_retired")

    @property
    def undo_controller(self) -> Any:
        return self._exact(self.undo_controller_ref, "history_controller_retired")

    @property
    def coordinator(self) -> Any:
        return self._exact(self.coordinator_ref, "history_coordinator_retired")

    @property
    def store(self) -> Any:
        return self._exact(self.store_ref, "history_store_retired")

    def fresh_head(self) -> HistoryHead:
        self._require_live()
        return _oplog_snapshot(self.op_log)[1]

    def begin_transaction(
        self, *, coordinator: Any, store: Any, execute_nonce: Any,
    ) -> "_HistoryTransaction":
        self._require_live()
        if coordinator is not self.coordinator or store is not self.store:
            _fail("history_coordinator_mismatch", "history transaction used another coordinator/store")
        if getattr(coordinator, "document", None) is not self.document:
            _fail("history_document_mismatch", "coordinator owns another Document")
        if getattr(coordinator, "store", None) is not store:
            _fail("history_store_mismatch", "coordinator store identity changed")
        if getattr(store, "op_log", self.op_log) is not self.op_log:
            _fail("history_oplog_mismatch", "commit store owns another OpLog")
        head = _oplog_head(self.op_log)
        if head != self.trusted_head:
            self.freeze("history_head_changed", "canonical OpLog changed outside its coordinator")
        transaction = _HistoryTransaction(
            self,
            coordinator,
            store,
            head,
            execute_nonce,
            _capability=_HISTORY_TRANSACTION_CONSTRUCTION_CAPABILITY,
        )
        # Publish the process/thread-bound transaction before returning it.
        # Retirement therefore also rejects a minted transaction that has not
        # entered its context manager yet.
        with _REGISTRY_LOCK:
            if self.active_transaction is not None:
                _fail("history_transaction_nested", "history transactions cannot nest")
            self.active_transaction = transaction
        return transaction

    def freeze(self, code: str, message: str) -> None:
        self.state = "FROZEN"
        self.freeze_reason = code or "history_authority_frozen"
        try:
            raise HistoryAuthorityError(
                self.freeze_reason,
                message or "canonical history outcome requires runtime reload",
            )
        finally:
            # A post-write verifier can itself fail.  The permit registry owns
            # its value strongly, while the transaction owns the weak key; if
            # a frozen authority leaves that pair attached, the whole runtime
            # graph becomes self-rooting and cannot be retired or collected.
            _release_authority_append_permits(self)


_AUTHORITY_RECORDS: weakref.WeakKeyDictionary[
    HistoryAuthority, _AuthorityRecord
] = weakref.WeakKeyDictionary()


def _authority_record(authority: HistoryAuthority) -> _AuthorityRecord:
    if type(authority) is not HistoryAuthority:
        _fail("history_authority_invalid", "history authority handle is invalid")
    with _REGISTRY_LOCK:
        record = _AUTHORITY_RECORDS.get(authority)
    if record is None:
        _fail("history_authority_retired", "history authority handle is stale")
    return record


class _HistoryExecuteNonce:
    __slots__ = ("__weakref__",)

    def __new__(cls):
        raise TypeError("history execute nonces are framework-issued")


@dataclass(frozen=True)
class _ExecuteRecord:
    authority: _AuthorityRecord
    coordinator_ref: Any
    store_ref: Any
    pid: int
    thread_id: int


_HISTORY_TRANSACTION_CONSTRUCTION_CAPABILITY = object()
_EXECUTE_NONCES: weakref.WeakKeyDictionary[
    _HistoryExecuteNonce, _ExecuteRecord
] = weakref.WeakKeyDictionary()


class _HistoryReplayPort:
    """Opaque read/select/bind port consumed only by UndoController."""

    __slots__ = ()

    def __new__(cls):
        raise TypeError("history replay ports are framework-issued")

    @property
    def fresh_entries(self) -> tuple[dict[str, Any], ...]:
        return _replay_transaction(self).fresh_entries

    def select_history_source(
        self, *, controller: Any, purpose: str,
        entry_index: int | None = None,
        entry_indexes: tuple[int, ...] | None = None,
    ) -> HistorySourceTicket:
        return _replay_transaction(self).select_history_source(
            controller=controller,
            purpose=purpose,
            entry_index=entry_index,
            entry_indexes=entry_indexes,
        )

    def bind_replay(self, ticket: HistorySourceTicket, operation: Any) -> None:
        _replay_transaction(self).bind_replay(ticket, operation)


_HISTORY_REPLAY_CONSUMER_CAPABILITY = object()


def _replay_transaction(port: _HistoryReplayPort) -> "_HistoryTransaction":
    transaction = _ACTIVE_TRANSACTION.get()
    if (
        type(port) is not _HistoryReplayPort
        or transaction is None
        or transaction._replay_port is not port
    ):
        _fail("history_replay_port_inactive", "history replay port is not active")
    transaction._require_open()
    return transaction


class _HistoryTransaction:
    __slots__ = (
        "authority", "coordinator", "store", "_fresh_entries", "source_head",
        "tickets", "state", "expected_operations", "expected_bodies",
        "append_cursor", "_context_token", "_execute_nonce", "_owner_pid",
        "_owner_thread_id", "_claimed_source_indexes",
        "_claimed_replay_operations", "_claimed_replay_bodies",
        "_replay_port", "write_head", "_append_permit",
    )

    def __init__(
        self, authority, coordinator, store, head, execute_nonce,
        *, _capability: Any = None,
    ) -> None:
        if _capability is not _HISTORY_TRANSACTION_CONSTRUCTION_CAPABILITY:
            raise TypeError("history transactions are coordinator-issued")
        self.authority = authority
        self.coordinator = coordinator
        self.store = store
        self._fresh_entries = None
        self.source_head = head
        self.tickets: list[HistorySourceTicket] = []
        self.state = "OPEN"
        self.expected_operations: tuple[Any, ...] = ()
        self.expected_bodies: tuple[bytes, ...] = ()
        self.append_cursor = 0
        self._context_token = None
        self._execute_nonce = execute_nonce
        self._owner_pid = os.getpid()
        self._owner_thread_id = threading.get_ident()
        self._claimed_source_indexes: set[int] = set()
        self._claimed_replay_operations: list[Any] = []
        self._claimed_replay_bodies: set[bytes] = set()
        self._replay_port = object.__new__(_HistoryReplayPort)
        self.write_head = head
        self._append_permit = None

    @property
    def fresh_entries(self) -> tuple[dict[str, Any], ...]:
        self._require_open()
        if self._fresh_entries is None:
            rows, head = _oplog_snapshot(self.authority.op_log)
            if head != self.source_head:
                self.freeze(
                    "history_head_changed_before_source",
                    "canonical history changed before source selection",
                )
            self._fresh_entries = tuple(copy.deepcopy(rows))
        return tuple(copy.deepcopy(self._fresh_entries))

    def _require_owner(self) -> None:
        if (
            self._owner_pid != os.getpid()
            or self._owner_thread_id != threading.get_ident()
        ):
            _fail("history_transaction_owner_mismatch", "history transaction crossed process/thread")
        with _REGISTRY_LOCK:
            execute = _EXECUTE_NONCES.get(self._execute_nonce)
        if (
            execute is None or execute.authority is not self.authority
            or execute.coordinator_ref() is not self.coordinator
            or execute.store_ref() is not self.store
            or execute.pid != self._owner_pid
            or execute.thread_id != self._owner_thread_id
        ):
            _fail("history_execute_nonce_invalid", "history execute nonce is stale or foreign")

        # ContextVars are thread-local, so transaction validity must also live
        # on the shared authority record.  This catches cross-thread retire and
        # lifecycle changes on every transaction step.
        if self.authority.pid != os.getpid():
            _fail("history_authority_pid_mismatch", "history authority belongs to another process")
        self.authority._refresh_lifecycle()
        if self.authority.state == "RETIRED":
            _fail("history_authority_retired", "history authority is retired")
        if self.authority.state != "ACTIVE" and not (
            self.authority.state == "FROZEN" and self.state == "FROZEN"
        ):
            _fail("history_authority_frozen", "history authority requires runtime reload")
        if self.authority.active_transaction is not self:
            _fail("history_transaction_inactive", "history transaction is no longer authoritative")

    def __enter__(self) -> "_HistoryTransaction":
        self._require_owner()
        if _ACTIVE_TRANSACTION.get() is not None:
            _fail("history_transaction_nested", "history transactions cannot nest")
        self._context_token = _ACTIVE_TRANSACTION.set(self)
        return self

    def __exit__(self, exc_type, exc, _tb) -> bool:
        self._require_owner()
        if self._context_token is not None:
            _ACTIVE_TRANSACTION.reset(self._context_token)
            self._context_token = None
        try:
            if self.state in {"DURABLE", "DISCARDED", "FROZEN"}:
                return False
            try:
                self.discard_if_unchanged()
            except BaseException:
                if exc is not None:
                    return False
                raise
            return False
        finally:
            with _REGISTRY_LOCK:
                _EXECUTE_NONCES.pop(self._execute_nonce, None)
                if self.authority.active_transaction is self:
                    self.authority.active_transaction = None

    def select_history_source(
        self, *, controller: Any, purpose: str,
        entry_index: int | None = None,
        entry_indexes: tuple[int, ...] | None = None,
    ) -> HistorySourceTicket:
        self._require_open()
        if controller is not self.authority.undo_controller:
            _fail("history_controller_mismatch", "history source used another controller")
        if purpose not in {"undo", "redo"}:
            raise ValueError("purpose must be undo or redo")
        if entry_index is not None and entry_indexes is not None:
            _fail(
                "history_source_index_invalid",
                "history source must use entry_index or entry_indexes, not both",
            )
        indexes = entry_indexes if entry_indexes is not None else (entry_index,)
        rows = self.fresh_entries
        if (
            type(indexes) is not tuple or not indexes
            or any(type(index) is not int for index in indexes)
            or len(set(indexes)) != len(indexes)
            or tuple(sorted(indexes)) != indexes
            or any(index < 0 or index >= len(rows) for index in indexes)
            or any(index in self._claimed_source_indexes for index in indexes)
        ):
            _fail("history_source_index_invalid", "history source indexes are invalid")
        if purpose == "undo":
            groups: list[tuple[int, ...]] = []
            group_ids: list[str] = []
            for index, row in enumerate(rows):
                operation = row.get("op")
                if type(operation) is not dict:
                    _fail("history_source_invalid", "history source row is invalid")
                if operation.get("kind") in {"rollback", "stage"}:
                    continue
                group_id = operation.get("group_id", "")
                if (
                    groups and group_id
                    and group_ids[-1] == group_id
                ):
                    groups[-1] = (*groups[-1], index)
                else:
                    groups.append((index,))
                    group_ids.append(group_id)
            if indexes not in groups:
                _fail(
                    "history_source_group_incomplete",
                    "undo source must be one complete ordered native group",
                )
        else:
            source_operation = rows[indexes[0]].get("op") if len(indexes) == 1 else None
            if (
                type(source_operation) is not dict
                or source_operation.get("kind") != "rollback"
                or source_operation.get("command_id") != "undo"
            ):
                _fail(
                    "history_source_redo_invalid",
                    "redo source must be one committed undo Operation",
                )
        selected = tuple(copy.deepcopy(rows[index]) for index in indexes)
        ticket = object.__new__(HistorySourceTicket)
        record = _SourceRecord(
            transaction=self,
            controller=controller,
            purpose=purpose,
            indexes=indexes,
            source_entries=selected,
            source_bodies=tuple(_canonical_body(row) for row in selected),
        )
        with _REGISTRY_LOCK:
            _SOURCE_RECORDS[ticket] = record
        self._claimed_source_indexes.update(indexes)
        self.tickets.append(ticket)
        return ticket

    def bind_replay(self, ticket: HistorySourceTicket, operation: Any) -> None:
        self._require_open()
        record = self._ticket(ticket, "ISSUED")
        operation_body = _canonical_body(operation)
        if (
            any(claimed is operation for claimed in self._claimed_replay_operations)
            or operation_body in self._claimed_replay_bodies
        ):
            _fail(
                "history_replay_operation_claimed",
                "one replay Operation cannot satisfy multiple history source tickets",
            )
        verifier = self.authority.replay_verifier
        try:
            verified = verifier(
                record.purpose,
                tuple(copy.deepcopy(record.source_entries)),
                operation,
            )
        except BaseException:
            verified = False
        if verified is not True:
            _fail(
                "history_replay_derivation_invalid",
                "history replay Operation is not derived from its exact source",
            )
        record.operation = operation
        record.operation_body = operation_body
        record.state = "BOUND"
        self._claimed_replay_operations.append(operation)
        self._claimed_replay_bodies.add(operation_body)

    def mark_replayed(self, ticket: HistorySourceTicket) -> None:
        self._require_open()
        self._ticket(ticket, "BOUND").state = "REPLAYED"

    def prepare_store(self, operations: tuple[Any, ...]) -> None:
        self._require_open()
        if type(operations) is not tuple or not operations:
            raise TypeError("operations must be a non-empty tuple")
        if _oplog_head(self.authority.op_log) != self.source_head:
            self.freeze("history_head_changed_before_store", "canonical history changed during producer")
        bodies = tuple(_canonical_body(operation) for operation in operations)
        for ticket in self.tickets:
            record = self._ticket(ticket, "REPLAYED")
            positions = [
                index for index, operation in enumerate(operations)
                if operation is record.operation and bodies[index] == record.operation_body
            ]
            if len(positions) != 1:
                _fail("history_ticket_operation_mismatch", "history replay is absent or duplicated in commit intent")
        self.expected_operations = operations
        self.expected_bodies = bodies
        self.state = "PREPARED"

    def write_scope(self):
        return _HistoryWriteScope(self)

    def confirm_durable(self) -> None:
        self._require_owner()
        if self.state != "PREPARED" or self.append_cursor != len(self.expected_operations):
            self.freeze("history_suffix_incomplete", "commit did not append the exact operation suffix")
        try:
            suffix, head = _oplog_suffix(
                self.authority.op_log, self.source_head.count,
            )
            suffix_bodies = tuple(
                _canonical_body(row.get("op")) for row in suffix
            )
        except BaseException as verification_error:
            try:
                self.freeze(
                    "history_suffix_verification_failed",
                    "canonical history suffix could not be verified",
                )
            except HistoryAuthorityError as authority_error:
                raise authority_error from verification_error
        if head != self.write_head:
            self.freeze(
                "history_suffix_verification_failed",
                "canonical history head changed during durable verification",
            )
        if (
            len(suffix) != len(self.expected_bodies)
            or suffix_bodies != self.expected_bodies
        ):
            self.freeze("history_suffix_mismatch", "durable OpLog suffix differs from commit intent")
        self.authority.trusted_head = head
        self.state = "DURABLE"
        for ticket in self.tickets:
            _source_record(ticket).state = "DURABLE"
        self.tickets.clear()

    def discard_if_unchanged(self) -> None:
        self._require_owner()
        if _oplog_head(self.authority.op_log) != self.source_head:
            self.freeze("history_discard_head_changed", "cannot discard after canonical history changed")
        self.state = "DISCARDED"
        for ticket in self.tickets:
            _source_record(ticket).state = "DISCARDED"
        # Tickets may remain alive in UndoController until its explicit
        # discard acknowledgement, but the transaction must not own them.
        self.tickets.clear()

    def freeze(self, code: str, message: str) -> None:
        try:
            self._require_owner()
            self.state = "FROZEN"
            with _REGISTRY_LOCK:
                for ticket in self.tickets:
                    record = _SOURCE_RECORDS.get(ticket)
                    if record is not None:
                        record.state = "FROZEN"
                        _SOURCE_RECORDS.pop(ticket, None)
                self.tickets.clear()
            self.authority.freeze(code, message)
        finally:
            _release_transaction_append_permit(self)

    def _require_open(self) -> None:
        self._require_owner()
        if self.state != "OPEN" or _ACTIVE_TRANSACTION.get() is not self:
            _fail("history_transaction_inactive", "history transaction is not the active producer")

    def _ticket(self, ticket: HistorySourceTicket, state: str) -> _SourceRecord:
        if type(ticket) is not HistorySourceTicket or ticket not in self.tickets:
            _fail("history_ticket_invalid", "history source ticket was not issued by this transaction")
        record = _source_record(ticket)
        if record.transaction is not self or record.state != state:
            _fail("history_ticket_state_invalid", "history source ticket is in the wrong state")
        if tuple(
            _canonical_body(row) for row in record.source_entries
        ) != record.source_bodies:
            _fail("history_source_body_changed", "history source ticket body changed")
        return record


class _HistoryWriteScope:
    def __init__(self, transaction: _HistoryTransaction) -> None:
        self.transaction = transaction
        self.token = None

    def __enter__(self):
        tx = self.transaction
        tx._require_owner()
        if tx.state != "PREPARED" or _ACTIVE_WRITE.get() is not None:
            _fail("history_write_scope_invalid", "canonical history write scope is unavailable")
        if _ACTIVE_TRANSACTION.get() is not tx:
            _fail("history_write_scope_invalid", "history write requires its active execute transaction")
        self.token = _ACTIVE_WRITE.set(tx)
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        self.transaction._require_owner()
        if self.token is not None:
            _ACTIVE_WRITE.reset(self.token)
        return False


class _CoordinatorHistoryPort:
    """Private capability port held only by the exact CommitCoordinator path."""

    __slots__ = ("__weakref__",)

    def __new__(cls):
        raise TypeError("history coordinator ports are framework-issued")

    def open(
        self, *, coordinator: Any, store: Any, _capability: Any = None,
    ) -> _HistoryTransaction:
        if _capability is not _HISTORY_COORDINATOR_CAPABILITY:
            _fail("history_coordinator_port_forbidden", "history port requires coordinator authority")
        with _REGISTRY_LOCK:
            handle = _live_handle(_PORT_AUTHORITIES.get(self))
            if handle is None:
                _fail("history_coordinator_port_stale", "history coordinator port is stale")
            authority = _authority_record(handle)
            if authority.coordinator_port is not self:
                _fail("history_coordinator_port_stale", "history coordinator port identity changed")
            if (
                coordinator is not authority.coordinator
                or store is not authority.store
            ):
                _fail("history_coordinator_mismatch", "history port used another coordinator/store")
            if _ACTIVE_TRANSACTION.get() is not None:
                _fail("history_transaction_nested", "history transactions cannot nest")
            nonce = object.__new__(_HistoryExecuteNonce)
            execute_record = _ExecuteRecord(
                authority=authority,
                coordinator_ref=weakref.ref(coordinator),
                store_ref=weakref.ref(store),
                pid=os.getpid(),
                thread_id=threading.get_ident(),
            )
            _EXECUTE_NONCES[nonce] = execute_record
        try:
            return authority.begin_transaction(
                coordinator=coordinator,
                store=store,
                execute_nonce=nonce,
            )
        except BaseException:
            with _REGISTRY_LOCK:
                _EXECUTE_NONCES.pop(nonce, None)
            raise


_HISTORY_COORDINATOR_CAPABILITY = object()
_PORT_AUTHORITIES: weakref.WeakKeyDictionary[
    _CoordinatorHistoryPort, HistoryAuthority
] = weakref.WeakKeyDictionary()


_BY_COORDINATOR: weakref.WeakKeyDictionary[
    Any, HistoryAuthority
] = weakref.WeakKeyDictionary()
_BY_CONTROLLER: weakref.WeakKeyDictionary[
    Any, HistoryAuthority
] = weakref.WeakKeyDictionary()
_BY_OPLOG: weakref.WeakKeyDictionary[
    Any, HistoryAuthority
] = weakref.WeakKeyDictionary()
_BY_PATH: dict[str, HistoryAuthority] = {}
# A binding is a runtime lease, not a diagnostic-handle lease.  Keeping an
# explicit strong root guarantees that dropping the returned handle can never
# turn a protected OpLog back into a legacy/unbound writer.  Only retire may
# release this root.
_LIVE_AUTHORITIES: set[HistoryAuthority] = set()
_POISONED_OPLOGS: weakref.WeakKeyDictionary[Any, bool] = weakref.WeakKeyDictionary()
_POISONED_PATHS: set[str] = set()
_HISTORY_RETIRE_CAPABILITY = object()
_HISTORY_RUNTIME_VERIFIER_CAPABILITY = object()


def _live_handle(handle: Any) -> HistoryAuthority | None:
    if type(handle) is not HistoryAuthority:
        return None
    try:
        record = _authority_record(handle)
    except HistoryAuthorityError:
        return None
    record._refresh_lifecycle()
    return handle if record.state != "RETIRED" else None


def bind_history_authority(
    capability: _HistoryBindCapability, *, bootstrap_owner: Any,
    document: Any, op_log: Any, undo_controller: Any, coordinator: Any,
    store: Any, project_id: str, generation: int, runtime_revision: str,
    replay_verifier: Any,
) -> HistoryAuthority:
    # All validation, path uniqueness, capability consumption, and map
    # publication are one registry transaction.  A losing bind leaves no
    # coordinator/controller/OpLog residue and may retry its one-shot token.
    with _REGISTRY_LOCK:
        capability_record = _CAPABILITIES.get(capability)
        if (
            capability_record is None or capability_record.used
            or capability_record.owner is not bootstrap_owner
            or capability_record.pid != os.getpid()
        ):
            _fail(
                "history_bind_capability_invalid",
                "history binding requires a fresh bootstrap capability",
            )
        if (
            not project_id or type(generation) is not int or generation < 1
            or not runtime_revision or not callable(replay_verifier)
            or getattr(undo_controller, "_op_log", None) is not op_log
            or getattr(undo_controller, "_doc", None) is not document
            or getattr(coordinator, "document", None) is not document
            or getattr(coordinator, "store", None) is not store
            or getattr(store, "op_log", op_log) is not op_log
        ):
            _fail("history_binding_invalid", "runtime history binding graph is invalid")

        try:
            runtime_refs = tuple(
                weakref.ref(value)
                for value in (document, op_log, undo_controller, coordinator, store)
            )
        except TypeError:
            _fail(
                "history_binding_invalid",
                "runtime history identities must support weak exact binding",
            )
        if op_log in _POISONED_OPLOGS:
            _fail(
                "history_inherited_runtime",
                "a fork child must bootstrap a fresh OpLog runtime",
            )
        rows, head = _oplog_snapshot(op_log)
        del rows
        if (
            _live_handle(_BY_COORDINATOR.get(coordinator)) is not None
            or _live_handle(_BY_CONTROLLER.get(undo_controller)) is not None
            or _live_handle(_BY_OPLOG.get(op_log)) is not None
        ):
            _fail("history_runtime_already_bound", "runtime history graph is already bound")
        if head.path_identity:
            existing = _live_handle(_BY_PATH.get(head.path_identity))
            if existing is not None:
                _fail("history_path_already_bound", "canonical OpLog path is already bound")
            _BY_PATH.pop(head.path_identity, None)

        document_installer = getattr(
            document, "_install_canonical_history_authority", None,
        )
        document_rollback = getattr(
            document,
            "_rollback_canonical_history_authority_install",
            None,
        )
        controller_installer = getattr(
            undo_controller, "_install_canonical_history_authority", None,
        )
        controller_rollback = getattr(
            undo_controller,
            "_rollback_canonical_history_authority_install",
            None,
        )
        installs_document_graph = callable(document_installer)
        if installs_document_graph and (
            not callable(document_rollback)
            or
            not callable(controller_installer)
            or not callable(controller_rollback)
        ):
            _fail(
                "history_binding_invalid",
                "protected Document requires an installable canonical controller",
            )
        if not installs_document_graph and (
            callable(document_rollback)
            or callable(controller_installer)
            or callable(controller_rollback)
        ):
            _fail(
                "history_binding_invalid",
                "canonical controller cannot bind a Document without the same protocol",
            )

        handle = object.__new__(HistoryAuthority)
        authority_record = _AuthorityRecord(
            document_ref=runtime_refs[0],
            op_log_ref=runtime_refs[1],
            undo_controller_ref=runtime_refs[2],
            coordinator_ref=runtime_refs[3],
            store_ref=runtime_refs[4],
            project_id=project_id,
            generation=generation,
            runtime_revision=runtime_revision,
            trusted_head=head,
            replay_verifier=replay_verifier,
        )
        coordinator_port = object.__new__(_CoordinatorHistoryPort)
        authority_record.coordinator_port = coordinator_port
        published_path = False
        controller_install_attempted = False
        document_install_attempted = False
        try:
            _AUTHORITY_RECORDS[handle] = authority_record
            _LIVE_AUTHORITIES.add(handle)
            _PORT_AUTHORITIES[coordinator_port] = handle
            _BY_COORDINATOR[coordinator] = handle
            _BY_CONTROLLER[undo_controller] = handle
            _BY_OPLOG[op_log] = handle
            if head.path_identity:
                _BY_PATH[head.path_identity] = handle
                published_path = True

            # Publish the registry edges under the same lock before installing
            # the leaf identities.  No other thread can observe the tentative
            # graph, and any install failure removes every edge while leaving
            # the one-shot capability reusable.
            if installs_document_graph:
                controller_install_attempted = True
                controller_installer(
                    authority_epoch=authority_record.epoch,
                    issuer_pid=authority_record.pid,
                )
                document_install_attempted = True
                document_installer(
                    op_log=op_log,
                    controller=undo_controller,
                    authority_epoch=authority_record.epoch,
                    issuer_pid=authority_record.pid,
                )
        except BaseException as install_error:
            if document_install_attempted:
                try:
                    document_rollback(
                        op_log=op_log,
                        controller=undo_controller,
                        authority_epoch=authority_record.epoch,
                        issuer_pid=authority_record.pid,
                    )
                except BaseException:
                    # Registry authority is still revoked below.  A hostile
                    # rollback hook cannot turn a partial runtime into a live
                    # neutral authority.
                    pass
            if controller_install_attempted:
                try:
                    controller_rollback(
                        authority_epoch=authority_record.epoch,
                        issuer_pid=authority_record.pid,
                    )
                except BaseException:
                    # A rollback failure cannot make the tentative authority
                    # usable: all neutral registry edges are still revoked.
                    pass
            _BY_COORDINATOR.pop(coordinator, None)
            _BY_CONTROLLER.pop(undo_controller, None)
            _BY_OPLOG.pop(op_log, None)
            if published_path and _BY_PATH.get(head.path_identity) is handle:
                _BY_PATH.pop(head.path_identity, None)
            _PORT_AUTHORITIES.pop(coordinator_port, None)
            _LIVE_AUTHORITIES.discard(handle)
            _AUTHORITY_RECORDS.pop(handle, None)
            raise HistoryAuthorityError(
                "history_binding_runtime_install_failed",
                "canonical history runtime graph could not be installed",
            ) from install_error

        if head.path_identity:
            # A fresh child capability plus a fresh exact OpLog is the
            # explicit replacement event that may reclaim an inherited path.
            _POISONED_PATHS.discard(head.path_identity)
        capability_record.used = True
        return handle


def history_authority_for_coordinator(coordinator: Any) -> HistoryAuthority | None:
    with _REGISTRY_LOCK:
        return _live_handle(_BY_COORDINATOR.get(coordinator))


def _canonical_history_runtime_record_locked(
    *,
    document: Any,
    store: Any,
    op_log: Any,
    controller: Any,
) -> tuple[HistoryAuthority, _AuthorityRecord]:
    """Return one exact inactive runtime while ``_REGISTRY_LOCK`` is held."""

    handle = _live_handle(_BY_OPLOG.get(op_log))
    if handle is None:
        _fail(
            "history_authority_not_bound",
            "canonical history runtime is not bound",
        )
    record = _authority_record(handle)
    record._require_live()
    if record.active_transaction is not None:
        _fail(
            "history_transaction_active",
            "canonical history runtime is committing another transaction",
        )
    if (
        record.document is not document
        or record.store is not store
        or record.op_log is not op_log
        or record.undo_controller is not controller
        or record.pid != os.getpid()
        or getattr(store, "op_log", None) is not op_log
        or getattr(controller, "_doc", None) is not document
        or getattr(controller, "_op_log", None) is not op_log
        or getattr(document, "_history_op_log_authority", None) is not op_log
        or getattr(document, "_history_controller_authority", None)
        is not controller
        or getattr(document, "_history_authority_epoch", None)
        is not record.epoch
        or getattr(controller, "_history_authority_epoch", None)
        is not record.epoch
        or getattr(document, "_history_authority_pid", None) != record.pid
        or getattr(controller, "_history_authority_pid", None) != record.pid
    ):
        _fail(
            "history_runtime_graph_mismatch",
            "canonical history runtime graph changed",
        )
    return handle, record


def _verify_canonical_history_runtime(
    *,
    document: Any,
    store: Any,
    op_log: Any,
    controller: Any,
    _capability: Any = None,
) -> tuple[tuple[dict[str, Any], ...], HistoryHead]:
    """Prove one exact ACTIVE runtime graph and return one fresh snapshot.

    This is a deny-only private seam for framework authority consumers.  It
    never returns the HistoryAuthority handle, transaction port, write permit,
    or replay capability.
    """

    if _capability is not _HISTORY_RUNTIME_VERIFIER_CAPABILITY:
        _fail(
            "history_runtime_verifier_forbidden",
            "canonical history runtime verification is private",
        )
    # OpLog.append owns its object lock before it asks this registry to approve
    # the write.  Never hold the registry lock while taking a fresh OpLog
    # snapshot: an invalid direct append must fail, not deadlock with a verifier.
    # The pre/post registry checks make the unlocked read optimistic.  A real
    # coordinator commit that advances ``trusted_head`` between them is retried;
    # a stable graph whose durable read fails is frozen and requires reload.
    for _attempt in range(3):
        with _REGISTRY_LOCK:
            handle, record = _canonical_history_runtime_record_locked(
                document=document,
                store=store,
                op_log=op_log,
                controller=controller,
            )
            expected_trusted_head = record.trusted_head

        try:
            rows, fresh = _oplog_snapshot(op_log)
        except BaseException as snapshot_error:
            # Process-control failures are not a safe reason to leave this
            # runtime usable.  Preserve the optimistic retry only for an
            # ordinary Exception that raced a legitimate coordinator head
            # advance; KeyboardInterrupt/SystemExit/direct BaseException must
            # freeze and surface the same typed unavailable result.
            retry_after_head_advance = isinstance(snapshot_error, Exception)
            try:
                with _REGISTRY_LOCK:
                    current_handle, current_record = (
                        _canonical_history_runtime_record_locked(
                            document=document,
                            store=store,
                            op_log=op_log,
                            controller=controller,
                        )
                    )
                    if current_handle is not handle or current_record is not record:
                        _fail(
                            "history_runtime_graph_mismatch",
                            "canonical history runtime graph changed during verification",
                        )
                    if (
                        retry_after_head_advance
                        and current_record.trusted_head != expected_trusted_head
                    ):
                        continue
                    current_record.freeze(
                        "history_runtime_snapshot_unavailable",
                        "canonical history could not be read for runtime verification",
                    )
            except HistoryAuthorityError as authority_error:
                raise authority_error from snapshot_error
            _fail(
                "history_runtime_snapshot_unavailable",
                "canonical history could not fail closed after a snapshot error",
            )

        with _REGISTRY_LOCK:
            current_handle, current_record = _canonical_history_runtime_record_locked(
                document=document,
                store=store,
                op_log=op_log,
                controller=controller,
            )
            if current_handle is not handle or current_record is not record:
                _fail(
                    "history_runtime_graph_mismatch",
                    "canonical history runtime graph changed during verification",
                )
            if current_record.trusted_head != expected_trusted_head:
                continue
            if fresh != current_record.trusted_head:
                current_record.freeze(
                    "history_head_changed",
                    "canonical history changed outside its coordinator",
                )
            return rows, fresh

    _fail(
        "history_runtime_snapshot_unstable",
        "canonical history changed repeatedly during runtime verification",
    )


def _history_coordinator_port(
    coordinator: Any, *, _capability: Any = None,
) -> _CoordinatorHistoryPort | None:
    if _capability is not _HISTORY_COORDINATOR_CAPABILITY:
        _fail("history_coordinator_port_forbidden", "history port lookup is private")
    with _REGISTRY_LOCK:
        handle = _live_handle(_BY_COORDINATOR.get(coordinator))
        if handle is None:
            return None
        record = _authority_record(handle)
        port = record.coordinator_port
        if type(port) is not _CoordinatorHistoryPort:
            _fail("history_coordinator_port_stale", "history coordinator port is unavailable")
        return port


def history_authority_for_controller(controller: Any) -> HistoryAuthority | None:
    with _REGISTRY_LOCK:
        return _live_handle(_BY_CONTROLLER.get(controller))


def _history_oplog_is_bound(op_log: Any) -> bool:
    """Deny-only exact-identity probe for legacy history compatibility paths.

    This private seam exposes no handle, transaction, or write capability.  It
    lets an old standalone helper fail before reading a canonical OpLog once
    bootstrap has leased that exact runtime to a CommitCoordinator.
    """
    with _REGISTRY_LOCK:
        return _live_handle(_BY_OPLOG.get(op_log)) is not None


def _history_replay_port(
    controller: Any, *, _capability: Any = None,
) -> _HistoryReplayPort | None:
    if _capability is not _HISTORY_REPLAY_CONSUMER_CAPABILITY:
        _fail("history_replay_port_forbidden", "history replay port lookup is private")
    transaction = _ACTIVE_TRANSACTION.get()
    with _REGISTRY_LOCK:
        handle = _live_handle(_BY_CONTROLLER.get(controller))
        if handle is None:
            return None
        record = _authority_record(handle)
        if transaction is None:
            return None
        if (
            transaction.authority is not record
            or record.active_transaction is not transaction
        ):
            _fail("history_transaction_inactive", "history replay transaction is not authoritative")
        transaction._require_open()
        return transaction._replay_port


def retire_history_authority(
    authority: HistoryAuthority,
    *,
    _capability: Any = None,
) -> None:
    """Atomically retire one exact runtime binding.

    Bootstrap owns the private capability.  Ordinary diagnostic-handle holders
    cannot unbind a live project and create a same-path second authority.
    """
    if _capability is not _HISTORY_RETIRE_CAPABILITY:
        _fail("history_retire_forbidden", "history retirement requires bootstrap authority")
    with _REGISTRY_LOCK:
        record = _authority_record(authority)
        if record.state == "RETIRED":
            return
        if record.active_transaction is not None:
            _fail("history_retire_active", "cannot retire active history transaction")
        try:
            coordinator = record.coordinator_ref()
            controller = record.undo_controller_ref()
            op_log = record.op_log_ref()
            for mapping, runtime in (
                (_BY_COORDINATOR, coordinator),
                (_BY_CONTROLLER, controller),
                (_BY_OPLOG, op_log),
            ):
                if runtime is not None:
                    mapped = _live_handle(mapping.get(runtime))
                    if mapped is authority:
                        mapping.pop(runtime, None)
            path_identity = record.trusted_head.path_identity
            if path_identity:
                mapped = _live_handle(_BY_PATH.get(path_identity))
                if mapped is authority:
                    _BY_PATH.pop(path_identity, None)
            port = record.coordinator_port
            if type(port) is _CoordinatorHistoryPort:
                _PORT_AUTHORITIES.pop(port, None)
            record.coordinator_port = None
            record.state = "RETIRED"
            record.freeze_reason = "history_authority_retired"
            _LIVE_AUTHORITIES.discard(authority)
        finally:
            # A previously frozen transaction is no longer active, but an
            # interrupted post-write probe may still have left its permit
            # record behind.  Retirement is the final ownership boundary and
            # must sever every such edge even when later cleanup raises.
            _release_authority_append_permits(record)


def validate_history_replay_ticket(
    ticket: HistorySourceTicket, *, document: Any, operation: Any,
    controller: Any = None,
) -> None:
    if type(ticket) is not HistorySourceTicket:
        _fail("history_ticket_invalid", "protected replay requires a source ticket")
    record = _source_record(ticket)
    tx = _ACTIVE_TRANSACTION.get()
    if (
        tx is None or record.transaction is not tx or record.state != "BOUND"
        or (controller is not None and record.controller is not controller)
        or tx.authority.document is not document
        or record.operation is not operation
        or record.operation_body != _canonical_body(operation)
    ):
        _fail("history_ticket_binding_mismatch", "history replay ticket does not bind this operation")


def require_history_ticket_replayed(
    ticket: HistorySourceTicket, *, document: Any, operation: Any,
) -> None:
    if type(ticket) is not HistorySourceTicket:
        _fail("history_ticket_invalid", "history coverage requires a source ticket")
    record = _source_record(ticket)
    record.transaction._require_owner()
    if (
        record.state not in {"REPLAYED", "DURABLE"}
        or record.transaction.authority.document is not document
        or record.operation is not operation
        or record.operation_body != _canonical_body(operation)
    ):
        _fail("history_ticket_state_invalid", "history source ticket was not replayed exactly")


def mark_history_replay_ticket(
    ticket: HistorySourceTicket, *, document: Any, operation: Any,
    controller: Any,
) -> None:
    validate_history_replay_ticket(
        ticket, document=document, operation=operation, controller=controller,
    )
    _source_record(ticket).transaction.mark_replayed(ticket)


def confirm_history_source_ticket(
    ticket: HistorySourceTicket, *, controller: Any, operation: Any,
) -> None:
    if type(ticket) is not HistorySourceTicket:
        _fail("history_ticket_invalid", "history confirmation requires a source ticket")
    with _REGISTRY_LOCK:
        record = _SOURCE_RECORDS.get(ticket)
        if record is None:
            _fail("history_ticket_invalid", "history source ticket is stale or foreign")
        transaction = record.transaction
        authority = transaction.authority
        authority._require_live()
        if (
            transaction._owner_pid != os.getpid()
            or transaction._owner_thread_id != threading.get_ident()
        ):
            _fail("history_ticket_owner_mismatch", "history ticket crossed process/thread")
        if (
            transaction.state != "DURABLE" or record.state != "DURABLE"
            or record.controller is not controller
            or record.operation is not operation
            or record.operation_body != _canonical_body(operation)
        ):
            _fail("history_ticket_not_durable", "history source ticket is not durably committed")
        record.state = "CONFIRMED"
        _SOURCE_RECORDS.pop(ticket, None)


def discard_history_source_ticket(
    ticket: HistorySourceTicket, *, controller: Any,
) -> None:
    if type(ticket) is not HistorySourceTicket:
        _fail("history_ticket_invalid", "history discard requires a source ticket")
    with _REGISTRY_LOCK:
        record = _SOURCE_RECORDS.get(ticket)
        if record is None:
            _fail("history_ticket_invalid", "history source ticket is stale or foreign")
        transaction = record.transaction
        transaction.authority._require_live()
        if (
            transaction._owner_pid != os.getpid()
            or transaction._owner_thread_id != threading.get_ident()
        ):
            _fail("history_ticket_owner_mismatch", "history ticket crossed process/thread")
        if (
            transaction.state != "DISCARDED"
            or record.controller is not controller or record.state != "DISCARDED"
        ):
            _fail("history_ticket_not_discardable", "history source ticket cannot be cleanly discarded")
        _SOURCE_RECORDS.pop(ticket, None)


class _HistoryAppendPermit:
    __slots__ = ("__weakref__",)

    def __new__(cls):
        raise TypeError("history append permits are framework-issued")


@dataclass(frozen=True)
class _AppendPermitRecord:
    transaction: _HistoryTransaction
    op_log: Any
    operation: Any
    operation_body: bytes
    index: int


_APPEND_PERMITS: weakref.WeakKeyDictionary[
    _HistoryAppendPermit, _AppendPermitRecord
] = weakref.WeakKeyDictionary()


def _release_append_permit(
    permit: _HistoryAppendPermit, record: _AppendPermitRecord,
) -> None:
    with _REGISTRY_LOCK:
        if _APPEND_PERMITS.get(permit) is record:
            _APPEND_PERMITS.pop(permit, None)
        if record.transaction._append_permit is permit:
            record.transaction._append_permit = None


def _release_transaction_append_permit(transaction: _HistoryTransaction) -> None:
    """Sever both sides of one transaction's append-permit ownership."""
    with _REGISTRY_LOCK:
        permit = transaction._append_permit
        if permit is None:
            return
        record = _APPEND_PERMITS.get(permit)
        if record is None or record.transaction is transaction:
            _APPEND_PERMITS.pop(permit, None)
        transaction._append_permit = None


def _release_authority_append_permits(authority: _AuthorityRecord) -> None:
    """Remove every stale permit edge owned by one exact authority record."""
    with _REGISTRY_LOCK:
        for permit, record in tuple(_APPEND_PERMITS.items()):
            if record.transaction.authority is not authority:
                continue
            _APPEND_PERMITS.pop(permit, None)
            if record.transaction._append_permit is permit:
                record.transaction._append_permit = None


def _freeze_after_append_verification_failure(
    transaction: _HistoryTransaction,
    cause: BaseException,
) -> None:
    """Fail closed when any post-publication verifier itself becomes unusable."""
    transaction.state = "FROZEN"
    with _REGISTRY_LOCK:
        for ticket in transaction.tickets:
            source = _SOURCE_RECORDS.get(ticket)
            if source is not None:
                source.state = "FROZEN"
                _SOURCE_RECORDS.pop(ticket, None)
        transaction.tickets.clear()
    try:
        transaction.authority.freeze(
            "history_append_outcome_indeterminate",
            "canonical history publication could not be verified",
        )
    except HistoryAuthorityError as authority_error:
        raise authority_error from cause


def authorize_history_append(
    op_log: Any, operation: Any,
) -> _HistoryAppendPermit | None:
    with _REGISTRY_LOCK:
        if op_log in _POISONED_OPLOGS:
            _fail(
                "history_inherited_runtime",
                "fork-inherited canonical OpLog cannot become an unbound writer",
            )
        handle = _live_handle(_BY_OPLOG.get(op_log))
        path = getattr(op_log, "_path", None)
        path_identity = str(path.resolve()) if path is not None else ""
        path_handle = (
            _live_handle(_BY_PATH.get(path_identity)) if path_identity else None
        )
        if handle is None:
            # Path lookup is a deny-only alias guard.  It must never promote a
            # second OpLog object into the exact bound writer, even inside the
            # legitimate transaction's write scope.
            if path_identity in _POISONED_PATHS:
                _fail(
                    "history_inherited_path",
                    "fork-inherited canonical path requires explicit fresh bootstrap",
                )
            if path_handle is not None:
                _fail(
                    "history_oplog_alias",
                    "canonical history writes require the exact bound OpLog object",
                )
            return None
    if handle is None:
        return None
    authority = _authority_record(handle)
    authority._require_live()
    if op_log is not authority.op_log:
        _fail("history_oplog_alias", "canonical history writer identity changed")
    tx = _ACTIVE_WRITE.get()
    if tx is not None:
        tx._require_owner()
    if (
        tx is None or _ACTIVE_TRANSACTION.get() is not tx
        or tx.authority is not authority or tx.state != "PREPARED"
    ):
        _fail("history_append_outside_store", "canonical OpLog append requires its coordinator write scope")
    if tx._append_permit is not None:
        _fail("history_append_nested", "canonical OpLog append permit is already active")
    index = tx.append_cursor
    if (
        index >= len(tx.expected_operations)
        or operation is not tx.expected_operations[index]
        or _canonical_body(operation) != tx.expected_bodies[index]
    ):
        _fail("history_append_intent_mismatch", "OpLog append differs from the exact commit intent")
    permit = object.__new__(_HistoryAppendPermit)
    permit_record = _AppendPermitRecord(
        transaction=tx,
        op_log=op_log,
        operation=operation,
        operation_body=tx.expected_bodies[index],
        index=index,
    )
    with _REGISTRY_LOCK:
        _APPEND_PERMITS[permit] = permit_record
        tx._append_permit = permit
    return permit


def consume_history_append(
    permit: _HistoryAppendPermit | None, *, op_log: Any, operation: Any,
) -> None:
    """Advance append intent only after OpLog has published the durable row."""
    if permit is None:
        return
    with _REGISTRY_LOCK:
        record = _APPEND_PERMITS.get(permit)
    if (
        record is None or record.op_log is not op_log
        or record.operation is not operation
    ):
        _fail("history_append_permit_invalid", "history append permit is stale or foreign")
    tx = record.transaction
    try:
        tx._require_owner()
        if (
            _ACTIVE_TRANSACTION.get() is not tx or _ACTIVE_WRITE.get() is not tx
            or tx.state != "PREPARED" or tx._append_permit is not permit
            or tx.append_cursor != record.index
            or _canonical_body(operation) != record.operation_body
        ):
            tx.freeze(
                "history_append_publish_mismatch",
                "published history no longer matches its exact append intent",
            )
        head = _oplog_head(op_log)
        previous = tx.write_head
        if (
            head.path_identity != previous.path_identity
            or head.count != previous.count + 1
            or head.tail_hash == previous.tail_hash
        ):
            tx.freeze(
                "history_append_publish_mismatch",
                "OpLog did not publish exactly one canonical history row",
            )
        tx.write_head = head
        tx.append_cursor += 1
    except HistoryAuthorityError as verification_error:
        if verification_error.outcome_unknown:
            raise
        _freeze_after_append_verification_failure(tx, verification_error)
    except BaseException as verification_error:
        _freeze_after_append_verification_failure(tx, verification_error)
    finally:
        _release_append_permit(permit, record)


def abort_history_append(
    permit: _HistoryAppendPermit | None, *, op_log: Any, operation: Any,
) -> None:
    """Preserve clean pre-write errors; freeze when persistence may have won."""
    if permit is None:
        return
    with _REGISTRY_LOCK:
        record = _APPEND_PERMITS.get(permit)
    if record is None:
        return
    if record.op_log is not op_log or record.operation is not operation:
        _fail("history_append_permit_invalid", "history append permit is foreign")
    tx = record.transaction
    try:
        tx._require_owner()
        try:
            head = _oplog_head(op_log)
        except BaseException as probe_error:
            try:
                tx.freeze(
                    "history_append_outcome_indeterminate",
                    "append failure could not be proven clean",
                )
            except HistoryAuthorityError as authority_error:
                raise authority_error from probe_error
        if head != tx.write_head:
            tx.freeze(
                "history_append_outcome_indeterminate",
                "OpLog changed before an append failure could be proven clean",
            )
    finally:
        _release_append_permit(permit, record)


def _reset_history_authority_after_fork() -> None:
    """A child inherits objects, never process-local history capabilities."""
    global _REGISTRY_LOCK, _CAPABILITIES, _SOURCE_RECORDS, _AUTHORITY_RECORDS
    global _BY_COORDINATOR, _BY_CONTROLLER, _BY_OPLOG, _BY_PATH
    global _PORT_AUTHORITIES, _EXECUTE_NONCES, _LIVE_AUTHORITIES
    global _POISONED_OPLOGS, _POISONED_PATHS, _APPEND_PERMITS
    global _ACTIVE_TRANSACTION, _ACTIVE_WRITE

    inherited_op_logs: list[Any] = []
    inherited_paths = set(_POISONED_PATHS)
    for handle in tuple(_LIVE_AUTHORITIES):
        record = _AUTHORITY_RECORDS.get(handle)
        if record is None:
            continue
        op_log = record.op_log_ref()
        if op_log is not None:
            inherited_op_logs.append(op_log)
        if record.trusted_head.path_identity:
            inherited_paths.add(record.trusted_head.path_identity)
    inherited_op_logs.extend(tuple(_POISONED_OPLOGS.keys()))

    _REGISTRY_LOCK = threading.RLock()
    _CAPABILITIES = weakref.WeakKeyDictionary()
    _SOURCE_RECORDS = weakref.WeakKeyDictionary()
    _AUTHORITY_RECORDS = weakref.WeakKeyDictionary()
    _PORT_AUTHORITIES = weakref.WeakKeyDictionary()
    _EXECUTE_NONCES = weakref.WeakKeyDictionary()
    _APPEND_PERMITS = weakref.WeakKeyDictionary()
    _BY_COORDINATOR = weakref.WeakKeyDictionary()
    _BY_CONTROLLER = weakref.WeakKeyDictionary()
    _BY_OPLOG = weakref.WeakKeyDictionary()
    _BY_PATH = {}
    _LIVE_AUTHORITIES = set()
    _POISONED_OPLOGS = weakref.WeakKeyDictionary(
        (op_log, True) for op_log in inherited_op_logs
    )
    _POISONED_PATHS = inherited_paths
    _ACTIVE_TRANSACTION = ContextVar("ze_history_transaction", default=None)
    _ACTIVE_WRITE = ContextVar("ze_history_write", default=None)


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_history_authority_after_fork)


__all__ = [
    "HistoryAuthority", "HistoryAuthorityError", "HistoryHead",
    "HistorySourceTicket", "authorize_history_append",
    "bind_history_authority", "confirm_history_source_ticket",
    "discard_history_source_ticket",
    "history_authority_for_controller", "history_authority_for_coordinator",
    "require_history_ticket_replayed",
    "mark_history_replay_ticket", "validate_history_replay_ticket",
]
