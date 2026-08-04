"""Compatibility file-backed CommitStore.

This adapter cannot make two files physically atomic.  Its job is narrower and
important: enforce the only safe legacy order (Document first, OpLog second)
and return an honest receipt when the boundary splits.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


_HERE = Path(__file__).resolve().parent
_CORE_DIR = _HERE.parent.parent
_OPERATION_DIR = _CORE_DIR / "03-commands-operations" / "02-operation"
_DOCUMENT_DIR = _CORE_DIR / "01-data-structure" / "01-document"
for _directory in (_OPERATION_DIR, _DOCUMENT_DIR):
    if str(_directory) not in sys.path:
        sys.path.insert(0, str(_directory))

from commit_contracts import (  # noqa: E402
    CommitFailure,
    CommitReceipt,
    CommitStage,
    DurabilityFacts,
    DurabilityState,
    EventStatus,
)
from ports import CommitIntent, WriteOutcomeError  # noqa: E402
from document import proven_document_write_outcome  # noqa: E402


class FileCommitStore:
    """Persist one logical mutation using the current Document/OpLog files.

    ``Document.save`` must complete before the first ``OpLog.append``.  This is
    not a transaction: if the document succeeds and any log append cannot be
    proven durable, the receipt is ``indeterminate`` and the command must not
    be replayed automatically.  Event publication is intentionally absent from
    this class; only CommitCoordinator owns that phase.
    """

    def __init__(self, document_path: str | Path, op_log: Any) -> None:
        self.document_path = Path(document_path)
        self.op_log = op_log

    def commit(self, intent: CommitIntent) -> CommitReceipt:
        if not isinstance(intent, CommitIntent):
            raise TypeError("intent must be CommitIntent")

        document = intent.document
        before_version = _positive_version(document)

        # Phase 1: canonical document.  A generic exception cannot prove
        # whether the final atomic rename reached durable storage, so UNKNOWN
        # is the only safe default.
        try:
            document.save(self.document_path)
        except Exception as exc:
            outcome = _reported_outcome(exc)
            version = _reported_document_version(exc) if outcome is DurabilityState.DURABLE else None
            if outcome is DurabilityState.DURABLE and version is None:
                outcome = DurabilityState.UNKNOWN
            return _failure_receipt(
                intent,
                stage=CommitStage.DOCUMENT,
                code=_error_code(exc, "document_write_failed"),
                message=_error_message(exc, "document write failed"),
                facts=DurabilityFacts(
                    mutation_applied=True,
                    document=outcome,
                ),
                document_version=version,
            )

        document_version = _positive_version(document)
        if document_version is None or (
            before_version is not None and document_version <= before_version
        ):
            # A successful return without a new, observable version cannot be
            # used as a durability acknowledgement.
            return _failure_receipt(
                intent,
                stage=CommitStage.DOCUMENT,
                code="document_version_unconfirmed",
                message="document save returned without a newer durable version",
                facts=DurabilityFacts(
                    mutation_applied=True,
                    document=DurabilityState.UNKNOWN,
                ),
            )

        # Phase 2: append every operation.  File OpLog is write-ahead, but a
        # raised OS error can still leave a partial tail; unless the adapter
        # explicitly reports otherwise, the attempted append is UNKNOWN.
        appended = 0
        for index, operation in enumerate(intent.operations):
            try:
                self.op_log.append(operation)
                appended += 1
            except Exception as exc:
                attempted = _reported_outcome(exc)
                is_last = index == len(intent.operations) - 1
                if attempted is DurabilityState.DURABLE and is_last:
                    oplog_outcome = DurabilityState.DURABLE
                elif attempted is DurabilityState.NOT_DURABLE and appended == 0:
                    oplog_outcome = DurabilityState.NOT_DURABLE
                else:
                    # A durable prefix, an unknown append, or an acknowledged
                    # append followed by unattempted suffix is not a complete
                    # logical batch.  UNKNOWN prevents unsafe whole-command retry.
                    oplog_outcome = DurabilityState.UNKNOWN
                return _failure_receipt(
                    intent,
                    stage=CommitStage.OPLOG,
                    code=_error_code(exc, "oplog_write_failed"),
                    message=_error_message(exc, "operation log write failed"),
                    facts=DurabilityFacts(
                        mutation_applied=True,
                        document=DurabilityState.DURABLE,
                        oplog=oplog_outcome,
                    ),
                    document_version=document_version,
                )

        return CommitReceipt.committed(
            commit_id=intent.commit_id,
            document_id=intent.document_id,
            operation_ids=intent.operation_ids,
            trace_id=intent.trace_id,
            save_policy=intent.save_policy,
            document_version=document_version,
            event_status=EventStatus.NOT_REQUIRED,
        )


def _failure_receipt(
    intent: CommitIntent,
    *,
    stage: CommitStage,
    code: str,
    message: str,
    facts: DurabilityFacts,
    document_version: int | None = None,
) -> CommitReceipt:
    failure = CommitFailure(
        stage=stage,
        code=code,
        message=message,
        facts=facts,
    )
    return CommitReceipt.from_failure(
        commit_id=intent.commit_id,
        document_id=intent.document_id,
        operation_ids=intent.operation_ids,
        trace_id=intent.trace_id,
        save_policy=intent.save_policy,
        failure=failure,
        document_version=document_version,
    )


def _reported_outcome(exc: Exception) -> DurabilityState:
    if isinstance(exc, WriteOutcomeError):
        return exc.outcome
    # Domain and persistence remain one-way: Document issues an exact private-
    # sealed outcome without importing this layer's enum.  Never infer safety
    # from a public code/context alone; forged/generic ZeError stays UNKNOWN.
    proven = proven_document_write_outcome(exc)
    if proven is not None:
        outcome, _version = proven
        if outcome == "not_durable":
            return DurabilityState.NOT_DURABLE
        if outcome == "unknown":
            return DurabilityState.UNKNOWN
    return DurabilityState.UNKNOWN


def _reported_document_version(exc: Exception) -> int | None:
    if not isinstance(exc, WriteOutcomeError):
        return None
    value = exc.document_version
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    return None


def _error_code(exc: Exception, fallback: str) -> str:
    value = getattr(exc, "code", "")
    return value if isinstance(value, str) and value else fallback


def _error_message(exc: Exception, fallback: str) -> str:
    value = str(exc).strip()
    return value or fallback


def _positive_version(document: Any) -> int | None:
    value = getattr(document, "version", None)
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    return None


__all__ = ["FileCommitStore"]
