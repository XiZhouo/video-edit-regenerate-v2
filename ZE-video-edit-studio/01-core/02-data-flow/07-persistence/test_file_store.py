from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from ports import CommitIntent, WriteOutcomeError
from commit_contracts import (
    CommitStage,
    CommitStatus,
    DurabilityState,
    EventStatus,
    SavePolicy,
)
from file_store import FileCommitStore
from document import ZeError
from operation import new_operation


class FakeDocument:
    def __init__(self, calls: list[str], *, error: Exception | None = None) -> None:
        self.id = "doc-1"
        self.version = 1
        self.calls = calls
        self.error = error

    def save(self, path: Path) -> None:
        self.calls.append(f"document:{path.name}")
        if self.error is not None:
            raise self.error
        self.version += 1


class FakeOpLog:
    def __init__(
        self,
        calls: list[str],
        *,
        fail_at: int | None = None,
        error: Exception | None = None,
    ) -> None:
        self.calls = calls
        self.fail_at = fail_at
        self.error = error or OSError("oplog disk error")
        self.appended = []

    def append(self, operation):
        index = len(self.appended)
        self.calls.append(f"oplog:{operation.id}")
        if self.fail_at == index:
            raise self.error
        self.appended.append(operation)
        return {"op": operation.to_dict()}


def _operation(label: str = "one"):
    return new_operation(
        command_id=f"test.{label}",
        source="user",
        actor={"user_id": "u1"},
        patches=[],
        reverse_patches=[],
        document_id="doc-1",
        trace_id=f"trace-{label}",
    )


def _intent(document, *operations):
    return CommitIntent(
        commit_id=uuid.uuid4().hex,
        document=document,
        operations=tuple(operations),
        trace_id="trace-commit",
        save_policy=SavePolicy.AUTO_COMMIT,
    )


def test_file_store_saves_document_before_appending_every_operation(tmp_path):
    calls: list[str] = []
    document = FakeDocument(calls)
    op_log = FakeOpLog(calls)
    first, second = _operation("first"), _operation("second")

    receipt = FileCommitStore(tmp_path / "document.json", op_log).commit(
        _intent(document, first, second)
    )

    assert calls == ["document:document.json", f"oplog:{first.id}", f"oplog:{second.id}"]
    assert receipt.status is CommitStatus.COMMITTED
    assert receipt.event_status is EventStatus.NOT_REQUIRED
    assert receipt.stage is CommitStage.OPLOG
    assert receipt.document_durable is True
    assert receipt.oplog_durable is True
    assert receipt.document_version == 2
    assert receipt.operation_ids == (first.id, second.id)


def test_generic_document_exception_is_unknown_and_never_appends(tmp_path):
    calls: list[str] = []
    document = FakeDocument(calls, error=OSError("rename acknowledgement lost"))
    op_log = FakeOpLog(calls)

    receipt = FileCommitStore(tmp_path / "document.json", op_log).commit(
        _intent(document, _operation())
    )

    assert calls == ["document:document.json"]
    assert receipt.status is CommitStatus.INDETERMINATE
    assert receipt.stage is CommitStage.DOCUMENT
    assert receipt.retry_safe is False
    assert receipt.failure is not None
    assert receipt.failure.facts.document is DurabilityState.UNKNOWN
    assert receipt.document_durable is False
    assert receipt.oplog_durable is False


def test_forged_version_conflict_code_is_not_a_durability_proof(tmp_path):
    calls: list[str] = []
    forged = ZeError(
        "version_conflict",
        "looks like a durable head conflict",
        context={
            "reason": "durable_head_advanced",
            "commit_point_reached": False,
        },
    )
    document = FakeDocument(calls, error=forged)

    receipt = FileCommitStore(
        tmp_path / "document.json",
        FakeOpLog(calls),
    ).commit(_intent(document, _operation()))

    assert receipt.status is CommitStatus.INDETERMINATE
    assert receipt.failure is not None
    assert receipt.failure.facts.document is DurabilityState.UNKNOWN
    assert receipt.retry_safe is False


def test_explicit_not_written_document_failure_is_dirty_not_retryable(tmp_path):
    calls: list[str] = []
    document = FakeDocument(
        calls,
        error=WriteOutcomeError(
            "permission denied before write",
            outcome=DurabilityState.NOT_DURABLE,
            code="document_permission_denied",
        ),
    )

    receipt = FileCommitStore(tmp_path / "document.json", FakeOpLog(calls)).commit(
        _intent(document, _operation())
    )

    assert receipt.status is CommitStatus.DIRTY
    assert receipt.retry_safe is False
    assert receipt.failure is not None
    assert receipt.failure.code == "document_permission_denied"
    assert receipt.failure.facts.mutation_applied is True


def test_document_success_then_known_oplog_failure_is_indeterminate(tmp_path):
    calls: list[str] = []
    document = FakeDocument(calls)
    operation = _operation()
    op_log = FakeOpLog(
        calls,
        fail_at=0,
        error=WriteOutcomeError(
            "log append rejected before write",
            outcome=DurabilityState.NOT_DURABLE,
            code="oplog_read_only",
        ),
    )

    receipt = FileCommitStore(tmp_path / "document.json", op_log).commit(
        _intent(document, operation)
    )

    assert calls == ["document:document.json", f"oplog:{operation.id}"]
    assert receipt.status is CommitStatus.INDETERMINATE
    assert receipt.stage is CommitStage.OPLOG
    assert receipt.retry_safe is False
    assert receipt.document_durable is True
    assert receipt.oplog_durable is False
    assert receipt.document_version == 2
    assert receipt.failure is not None
    assert receipt.failure.facts.oplog is DurabilityState.NOT_DURABLE


def test_generic_oplog_exception_is_unknown_not_a_safe_retry(tmp_path):
    calls: list[str] = []
    document = FakeDocument(calls)
    op_log = FakeOpLog(calls, fail_at=0, error=OSError("fsync response lost"))

    receipt = FileCommitStore(tmp_path / "document.json", op_log).commit(
        _intent(document, _operation())
    )

    assert receipt.status is CommitStatus.INDETERMINATE
    assert receipt.retry_safe is False
    assert receipt.failure is not None
    assert receipt.failure.facts.oplog is DurabilityState.UNKNOWN


def test_batch_partial_append_is_unknown_even_if_next_write_is_known_not_durable(tmp_path):
    calls: list[str] = []
    document = FakeDocument(calls)
    first, second = _operation("first"), _operation("second")
    op_log = FakeOpLog(
        calls,
        fail_at=1,
        error=WriteOutcomeError(
            "second operation rejected",
            outcome=DurabilityState.NOT_DURABLE,
        ),
    )

    receipt = FileCommitStore(tmp_path / "document.json", op_log).commit(
        _intent(document, first, second)
    )

    assert [operation.id for operation in op_log.appended] == [first.id]
    assert receipt.status is CommitStatus.INDETERMINATE
    assert receipt.failure is not None
    assert receipt.failure.facts.oplog is DurabilityState.UNKNOWN


def test_final_oplog_append_acknowledged_durable_can_still_commit(tmp_path):
    calls: list[str] = []
    document = FakeDocument(calls)
    op_log = FakeOpLog(
        calls,
        fail_at=0,
        error=WriteOutcomeError(
            "append was durable but response was lost",
            outcome=DurabilityState.DURABLE,
            code="oplog_ack_lost",
        ),
    )

    receipt = FileCommitStore(tmp_path / "document.json", op_log).commit(
        _intent(document, _operation())
    )

    assert receipt.status is CommitStatus.COMMITTED
    assert receipt.document_durable is True
    assert receipt.oplog_durable is True
    assert receipt.retry_safe is False
    assert receipt.failure is not None
    assert receipt.failure.code == "oplog_ack_lost"


def test_successful_save_without_version_advance_is_not_claimed_durable(tmp_path):
    calls: list[str] = []
    document = FakeDocument(calls)

    def no_version_save(path):
        calls.append(f"document:{Path(path).name}")

    document.save = no_version_save
    receipt = FileCommitStore(tmp_path / "document.json", FakeOpLog(calls)).commit(
        _intent(document, _operation())
    )

    assert receipt.status is CommitStatus.INDETERMINATE
    assert receipt.failure is not None
    assert receipt.failure.code == "document_version_unconfirmed"
    assert receipt.failure.facts.document is DurabilityState.UNKNOWN


def test_intent_rejects_empty_or_duplicate_operation_batches():
    document = FakeDocument([])
    with pytest.raises(ValueError, match="non-empty"):
        CommitIntent(
            commit_id="c",
            document=document,
            operations=(),
            trace_id="t",
            save_policy=SavePolicy.AUTO_COMMIT,
        )

    operation = _operation()
    with pytest.raises(ValueError, match="unique"):
        _intent(document, operation, operation)
