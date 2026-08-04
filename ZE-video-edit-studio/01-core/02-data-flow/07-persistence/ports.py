"""Persistence commit ports shared by every mutation surface.

The types in this leaf deliberately describe one logical commit.  Storage
implementations may change (files today, a transactional database later), but
callers only see ``CommitStore.commit(intent) -> CommitReceipt``.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable


_HERE = Path(__file__).resolve().parent
_CORE_DIR = _HERE.parent.parent
_OPERATION_DIR = _CORE_DIR / "03-commands-operations" / "02-operation"
_ENTITY_DIR = _CORE_DIR / "01-data-structure" / "02-entity"
for _directory in (_OPERATION_DIR, _ENTITY_DIR):
    if str(_directory) not in sys.path:
        sys.path.insert(0, str(_directory))

from commit_contracts import (  # noqa: E402
    CommitReceipt,
    CommitStatus,
    DurabilityState,
    EventStatus,
    SavePolicy,
)
from operation import Operation, PendingConfirmation  # noqa: E402
from entity import ZeError  # noqa: E402


class ActionStatus(str, Enum):
    """提交后动作的独立结果；它不改写业务 CommitStatus。"""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class ActionReceipt:
    """一项 after-commit action 的审计结果。"""

    action_id: str
    name: str
    status: ActionStatus
    code: str = ""
    message: str = ""

    def __post_init__(self) -> None:
        if not self.action_id or not self.name:
            raise ValueError("action_id and name must not be empty")
        if not isinstance(self.status, ActionStatus):
            raise TypeError("status must be ActionStatus")
        if self.status is ActionStatus.FAILED and (not self.code or not self.message):
            raise ValueError("failed action receipt requires code and message")
        if self.status is not ActionStatus.FAILED and (self.code or self.message):
            raise ValueError("only failed action receipts may carry an error")

    def to_dict(self) -> dict[str, str]:
        out = {
            "action_id": self.action_id,
            "name": self.name,
            "status": self.status.value,
        }
        if self.code:
            out["code"] = self.code
            out["message"] = self.message
        return out


@dataclass(frozen=True)
class MutationBatch:
    """Several Operations that form one indivisible logical commit.

    A batch is not a shortcut for several independent commits.  Its operation
    ids appear together in one receipt, so undo/redo and future database stores
    can preserve the caller's atomic intent.
    """

    operations: tuple[Operation, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.operations, tuple) or not self.operations:
            raise ValueError("MutationBatch.operations must be a non-empty tuple")
        if any(not isinstance(operation, Operation) for operation in self.operations):
            raise TypeError("MutationBatch.operations must contain only Operation values")
        operation_ids = tuple(operation.id for operation in self.operations)
        if len(set(operation_ids)) != len(operation_ids):
            raise ValueError("MutationBatch operation ids must be unique")


@dataclass(frozen=True)
class CommitIntent:
    """All data a store needs to durably commit an already-applied mutation."""

    commit_id: str
    document: Any
    operations: tuple[Operation, ...]
    trace_id: str
    save_policy: SavePolicy

    def __post_init__(self) -> None:
        if not self.commit_id:
            raise ValueError("commit_id must not be empty")
        if not self.trace_id:
            raise ValueError("trace_id must not be empty")
        if not isinstance(self.save_policy, SavePolicy):
            raise TypeError("save_policy must be SavePolicy")
        if not isinstance(self.operations, tuple) or not self.operations:
            raise ValueError("operations must be a non-empty tuple")
        if any(not isinstance(operation, Operation) for operation in self.operations):
            raise TypeError("operations must contain only Operation values")
        operation_ids = self.operation_ids
        if len(set(operation_ids)) != len(operation_ids):
            raise ValueError("operation ids must be unique inside one commit")
        if not self.document_id:
            raise ValueError("document.id must not be empty")

    @property
    def document_id(self) -> str:
        value = getattr(self.document, "id", "")
        return value if isinstance(value, str) else ""

    @property
    def operation_ids(self) -> tuple[str, ...]:
        return tuple(operation.id for operation in self.operations)


@dataclass(frozen=True)
class CommitExecution:
    """Coordinator result for no-op, confirmation, and mutation paths.

    ``PendingConfirmation`` and ``None`` intentionally have no receipt: neither
    path contains a mutation and therefore neither path may touch persistence.
    A real Operation or MutationBatch always carries the store's auditable
    receipt, including failed/indeterminate outcomes.
    """

    result: Operation | MutationBatch | PendingConfirmation | None
    receipt: CommitReceipt | None = None
    events: tuple[Mapping[str, Any], ...] = ()
    actions: tuple[ActionReceipt, ...] = ()

    def __post_init__(self) -> None:
        is_mutation = isinstance(self.result, (Operation, MutationBatch))
        if is_mutation and self.receipt is None:
            raise ValueError("a mutation execution requires a CommitReceipt")
        if not is_mutation and self.receipt is not None:
            raise ValueError("a no-op or pending confirmation cannot have a CommitReceipt")
        if not isinstance(self.events, tuple) or any(
            not isinstance(event, Mapping) for event in self.events
        ):
            raise TypeError("events must be a tuple of mappings")
        if not isinstance(self.actions, tuple) or any(
            not isinstance(action, ActionReceipt) for action in self.actions
        ):
            raise TypeError("actions must be a tuple of ActionReceipt values")
        if not is_mutation and self.events:
            raise ValueError("a no-op or pending confirmation cannot have events")
        if not is_mutation and self.actions:
            raise ValueError("a no-op or pending confirmation cannot have actions")
        if self.events and (
            self.receipt is None
            or self.receipt.status is not CommitStatus.COMMITTED
            or not self.receipt.document_durable
            or not self.receipt.oplog_durable
        ):
            raise ValueError("events require a committed, fully durable receipt")
        if self.events and len(self.events) != len(self.operations):
            raise ValueError("events must contain exactly one payload per Operation")
        if any(action.status is ActionStatus.SUCCEEDED for action in self.actions) and (
            self.receipt is None or self.receipt.status is not CommitStatus.COMMITTED
        ):
            raise ValueError("a succeeded action requires a committed business receipt")

    @property
    def operations(self) -> tuple[Operation, ...]:
        if isinstance(self.result, Operation):
            return (self.result,)
        if isinstance(self.result, MutationBatch):
            return self.result.operations
        return ()

    @property
    def pending_confirmation(self) -> PendingConfirmation | None:
        return self.result if isinstance(self.result, PendingConfirmation) else None

    @property
    def is_noop(self) -> bool:
        return self.result is None

    @property
    def is_committed(self) -> bool:
        return self.receipt is not None and self.receipt.status is CommitStatus.COMMITTED

    @property
    def action_failed(self) -> bool:
        return any(action.status is ActionStatus.FAILED for action in self.actions)


@runtime_checkable
class CommitStore(Protocol):
    """The only persistence seam visible to CommitCoordinator.

    Once a write may have started, implementations return an honest receipt
    instead of leaking an unclassified exception.  The coordinator still
    treats a broken third-party implementation conservatively as unknown.
    """

    def commit(self, intent: CommitIntent) -> CommitReceipt:
        ...


@runtime_checkable
class EventPublisher(Protocol):
    """Publish one already-durable change event.

    ``None`` and ``EventStatus.PUBLISHED`` mean synchronous delivery succeeded.
    ``EventStatus.PENDING`` means the publisher durably accepted the event for
    later delivery.  FAILED is returned only when the publisher can classify a
    failure without throwing.
    """

    def publish(self, payload: Mapping[str, Any]) -> EventStatus | None:
        ...


@runtime_checkable
class EventFactory(Protocol):
    """Translate one Operation into the existing realtime wire payload."""

    def build(
        self,
        operation: Operation,
        document: Any,
        receipt: CommitReceipt,
    ) -> Mapping[str, Any]:
        ...


class WriteOutcomeError(RuntimeError):
    """A storage adapter's explicit durability statement for a failed call.

    Generic exceptions are always treated as ``UNKNOWN``.  Adapters may raise
    this type only when they can prove whether the attempted resource write was
    durable.  For a durable Document write, ``document_version`` is mandatory.
    """

    def __init__(
        self,
        message: str,
        *,
        outcome: DurabilityState,
        code: str = "write_failed",
        document_version: int | None = None,
    ) -> None:
        if not isinstance(outcome, DurabilityState):
            raise TypeError("outcome must be DurabilityState")
        if not code:
            raise ValueError("code must not be empty")
        if outcome is DurabilityState.DURABLE and document_version is not None and (
            not isinstance(document_version, int)
            or isinstance(document_version, bool)
            or document_version < 1
        ):
            raise ValueError("document_version must be a positive integer")
        super().__init__(message)
        self.outcome = outcome
        self.code = code
        self.document_version = document_version


class EventDeliveryError(RuntimeError):
    """A publisher-classified delivery failure after business commit."""

    def __init__(
        self,
        message: str,
        *,
        status: EventStatus = EventStatus.FAILED,
        code: str = "event_publish_failed",
    ) -> None:
        if status not in (EventStatus.PENDING, EventStatus.FAILED):
            raise ValueError("event delivery errors must be pending or failed")
        if not code:
            raise ValueError("code must not be empty")
        super().__init__(message)
        self.status = status
        self.code = code


class NestedCommitError(RuntimeError):
    """A producer attempted to open a second commit inside the active commit."""

    code = "nested_commit_forbidden"

    def __init__(self) -> None:
        super().__init__(
            "nested CommitCoordinator.execute() is forbidden; return one CommandMutation instead"
        )


@runtime_checkable
class MutationSession(Protocol):
    """In-memory mutation snapshot owned by one coordinator execution.

    ``commit`` accepts the current object graph and releases the snapshot;
    ``rollback`` restores that exact graph when storage is proven untouched.
    Implementations must reject double finalization themselves.
    """

    def commit(self) -> None:
        ...

    def rollback(self) -> None:
        ...


class MutationRollbackError(ZeError):
    """The framework could not restore an uncommitted in-memory mutation."""

    def __init__(
        self,
        rollback_error: BaseException,
        *,
        original_error: BaseException | None = None,
        receipt: CommitReceipt | None = None,
    ) -> None:
        message = str(rollback_error).strip() or type(rollback_error).__name__
        super().__init__(
            "mutation_rollback_failed",
            f"mutation rollback failed: {message}",
            context={
                "rollback_error_type": type(rollback_error).__name__,
                "original_error_type": (
                    type(original_error).__name__ if original_error is not None else ""
                ),
                "commit_id": receipt.commit_id if receipt is not None else "",
            },
        )
        self.rollback_error = rollback_error
        self.original_error = original_error
        self.receipt = receipt


__all__ = [
    "ActionReceipt",
    "ActionStatus",
    "CommitExecution",
    "CommitIntent",
    "CommitStore",
    "EventDeliveryError",
    "EventFactory",
    "EventPublisher",
    "MutationBatch",
    "MutationRollbackError",
    "MutationSession",
    "NestedCommitError",
    "WriteOutcomeError",
]
