"""W04 integration gates across the real mutation surfaces.

Unit tests in the individual leaves prove their local contracts.  These tests
deliberately cross the Document, CommandRegistry, CommitCoordinator,
FileCommitStore, history, and pipeline boundaries so a locally correct session
cannot hide a broken whole-commit rollback.
"""

from __future__ import annotations

import sys
import threading
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest


_HERE = Path(__file__).resolve().parent
_CORE = _HERE.parent.parent
_PATHS = (
    _HERE,
    _CORE / "01-data-structure" / "01-document",
    _CORE / "01-data-structure" / "02-entity",
    _CORE / "02-data-flow" / "04-history-rollback",
    _CORE / "03-commands-operations" / "01-command",
    _CORE / "03-commands-operations" / "02-operation",
    _CORE / "03-commands-operations" / "03-pipeline",
)
for _path in _PATHS:
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from builtin_commands import register_builtin_commands  # noqa: E402
from command import CommandRegistry, CommandSpec  # noqa: E402
from commit_contracts import (  # noqa: E402
    CommitStatus,
    DurabilityState,
)
from commit_coordinator import CommitCoordinator  # noqa: E402
from document import Document, _HISTORY_REPLAY_CAPABILITY  # noqa: E402
from entity import Actor, Entity  # noqa: E402
import entity_file_store as entity_store_module  # noqa: E402
from entity_file_store import EntityFileStore, EntityFileStoreError  # noqa: E402
from file_store import FileCommitStore  # noqa: E402
from operation import OpLog, Patch, new_operation  # noqa: E402
from ports import WriteOutcomeError  # noqa: E402
from supervisor import PipelineSupervisor  # noqa: E402
from undo import UndoController  # noqa: E402
from work_list import WorkItem  # noqa: E402


class NeverCommitStore:
    """A tripwire: producer failures must never enter persistence."""

    def __init__(self) -> None:
        self.calls = 0

    def commit(self, intent):
        self.calls += 1
        raise AssertionError("persistence must not run after a producer failure")


def _document() -> Document:
    document = Document(
        id="doc-w04-real",
        schema_version="1.0.0",
        collaboration_mode="with_save",
        save_policy="auto_commit",
    )
    document.add(Entity(type="item", id="item-1", data={"x": 1}))
    return document


def _set_operation(
    document: Document,
    actor: Actor,
    *,
    command_id: str,
    before: int,
    after: int,
):
    return new_operation(
        command_id=command_id,
        source="user",
        actor=actor.to_dict(),
        patches=[Patch("item", "item-1", "x", "set", before, after)],
        reverse_patches=[Patch("item", "item-1", "x", "set", after, before)],
        document_id=document.id,
    )


def _register_set_command(registry: CommandRegistry, command_id: str = "test.set") -> None:
    def execute(args, actor, source, doc):
        entity = doc.get("item", "item-1")
        before = entity.data["x"]
        after = int(args["value"])
        entity.update(
            {"x": after},
            expected_version=entity.version,
            actor=actor,
        )
        return _set_operation(
            doc,
            actor,
            command_id=command_id,
            before=before,
            after=after,
        )

    registry.register(CommandSpec(id=command_id, label=command_id, execute=execute))


def test_two_real_dispatches_roll_back_as_one_producer_when_second_raises():
    """An inner committed handler is still provisional until the outer commit."""
    document = _document()
    entity = document.get("item", "item-1")
    entity_identity = entity
    before = deepcopy(entity.to_dict())
    registry = CommandRegistry()
    actor = Actor(user_id="u-w04")
    store = NeverCommitStore()

    _register_set_command(registry, "test.first")

    def fail_after_mutation(args, actor, source, doc):
        current = doc.get("item", "item-1")
        current.update(
            {"x": int(args["value"])},
            expected_version=current.version,
            actor=actor,
        )
        raise RuntimeError("second command failed after mutation")

    registry.register(CommandSpec(
        id="test.second",
        label="test.second",
        execute=fail_after_mutation,
    ))

    def producer():
        first = registry.dispatch(
            "test.first",
            {"value": 2},
            actor=actor,
            source="user",
            doc=document,
        )
        second = registry.dispatch(
            "test.second",
            {"value": 3},
            actor=actor,
            source="user",
            doc=document,
        )
        return (first, second)  # pragma: no cover - the second dispatch raises

    with pytest.raises(RuntimeError, match="second command failed after mutation"):
        CommitCoordinator(document, store).execute(producer)

    assert document.get("item", "item-1") is entity_identity
    assert document.get("item", "item-1").to_dict() == before
    assert store.calls == 0


def test_file_store_explicit_not_durable_restores_and_returns_aborted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Only an explicit NOT_DURABLE acknowledgement permits memory rollback."""
    document = _document()
    before = deepcopy(document.get("item", "item-1").to_dict())
    registry = CommandRegistry()
    _register_set_command(registry)
    actor = Actor(user_id="u-w04")
    op_log = OpLog()
    save_calls: list[Path] = []

    def reject_before_durability(self, path, *args, **kwargs):
        save_calls.append(Path(path))
        raise WriteOutcomeError(
            "atomic replace was rejected before persistence",
            outcome=DurabilityState.NOT_DURABLE,
            code="document_not_written",
        )

    monkeypatch.setattr(Document, "save", reject_before_durability)
    coordinator = CommitCoordinator(
        document,
        FileCommitStore(tmp_path / "document.json", op_log),
    )

    execution = coordinator.execute(lambda: registry.dispatch(
        "test.set",
        {"value": 2},
        actor=actor,
        source="user",
        doc=document,
    ))

    assert execution.receipt is not None
    assert execution.receipt.status is CommitStatus.ABORTED
    assert execution.receipt.retry_safe is True
    assert execution.receipt.failure is not None
    assert execution.receipt.failure.facts.rollback_succeeded is True
    assert execution.receipt.failure.facts.document is DurabilityState.NOT_DURABLE
    assert execution.receipt.failure.facts.oplog is DurabilityState.NOT_DURABLE
    assert document.get("item", "item-1").to_dict() == before
    assert save_calls == [tmp_path / "document.json"]
    assert len(op_log) == 0


def test_file_store_generic_save_failure_stays_mutated_and_indeterminate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """An unclassified save exception may have reached disk, so memory stays new."""
    document = _document()
    registry = CommandRegistry()
    _register_set_command(registry)
    actor = Actor(user_id="u-w04")
    op_log = OpLog()
    save_calls = 0

    def lose_save_acknowledgement(self, path, *args, **kwargs):
        nonlocal save_calls
        save_calls += 1
        raise OSError("atomic replace acknowledgement was lost")

    monkeypatch.setattr(Document, "save", lose_save_acknowledgement)
    coordinator = CommitCoordinator(
        document,
        FileCommitStore(tmp_path / "document.json", op_log),
    )

    execution = coordinator.execute(lambda: registry.dispatch(
        "test.set",
        {"value": 2},
        actor=actor,
        source="user",
        doc=document,
    ))

    assert execution.receipt is not None
    assert execution.receipt.status is CommitStatus.INDETERMINATE
    assert execution.receipt.retry_safe is False
    assert execution.receipt.failure is not None
    assert execution.receipt.failure.facts.rollback_succeeded is False
    assert execution.receipt.failure.facts.document is DurabilityState.UNKNOWN
    assert document.get("item", "item-1").data["x"] == 2
    assert save_calls == 1
    assert len(op_log) == 0


def test_document_rename_then_fsync_unknown_never_rolls_back_logs_or_publishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A visible v2 head is UNKNOWN, aligned in memory, and never replayable."""
    document = _document()
    registry = CommandRegistry()
    _register_set_command(registry)
    actor = Actor(user_id="u-w04")
    op_log = OpLog()
    publish_calls: list[dict] = []
    publisher = SimpleNamespace(
        publish=lambda payload: publish_calls.append(dict(payload))
    )
    real_fsync_directory = entity_store_module._fsync_directory

    def fail_only_after_document_rename(
        descriptor: int,
        *,
        before_commit: bool,
    ) -> None:
        if not before_commit:
            raise EntityFileStoreError(
                "document_durability_unknown",
                "simulated_root_fsync_failure",
                committed=True,
            )
        real_fsync_directory(descriptor, before_commit=True)

    monkeypatch.setattr(
        entity_store_module,
        "_fsync_directory",
        fail_only_after_document_rename,
    )
    path = tmp_path / "document.json"
    execution = CommitCoordinator(
        document,
        FileCommitStore(path, op_log),
        publisher=publisher,
    ).execute(lambda: registry.dispatch(
        "test.set",
        {"value": 2},
        actor=actor,
        source="user",
        doc=document,
    ))

    assert execution.receipt is not None
    assert execution.receipt.status is CommitStatus.INDETERMINATE
    assert execution.receipt.failure is not None
    assert execution.receipt.failure.facts.document is DurabilityState.UNKNOWN
    assert execution.receipt.failure.facts.rollback_succeeded is False
    assert document.version == 2
    assert document.get("item", "item-1").data["x"] == 2
    assert json.loads(path.read_text(encoding="utf-8"))["_v"] == 2
    assert len(op_log) == 0
    assert publish_calls == []
    assert execution.events == ()


def test_stale_durable_head_is_rejected_and_real_mutation_rolls_back(
    tmp_path: Path,
) -> None:
    path = tmp_path / "document.json"
    original = _document()
    original.save(path)
    stale = Document.open(path)
    winner = Document.open(path)
    winner.get("item", "item-1").update(
        {"x": 9},
        expected_version=1,
        actor=Actor(user_id="winner"),
    )
    winner.save(path)

    registry = CommandRegistry()
    _register_set_command(registry)
    actor = Actor(user_id="stale-writer")
    op_log = OpLog()
    published: list[dict] = []
    execution = CommitCoordinator(
        stale,
        FileCommitStore(path, op_log),
        publisher=SimpleNamespace(
            publish=lambda payload: published.append(dict(payload))
        ),
    ).execute(lambda: registry.dispatch(
        "test.set",
        {"value": 2},
        actor=actor,
        source="user",
        doc=stale,
    ))

    assert execution.receipt is not None
    assert execution.receipt.status is CommitStatus.ABORTED
    assert execution.receipt.retry_safe is True
    assert execution.receipt.failure is not None
    assert execution.receipt.failure.code == "version_conflict"
    assert execution.receipt.failure.facts.document is DurabilityState.NOT_DURABLE
    assert execution.receipt.failure.facts.rollback_succeeded is True
    assert stale.version == 2
    assert stale.get("item", "item-1").data == {"x": 1}
    assert Document.open(path).get("item", "item-1").data == {"x": 9}
    assert len(op_log) == 0
    assert published == []
    assert execution.events == ()


def test_lock_timeout_is_proven_precommit_and_real_mutation_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "document.json"
    document = _document()
    document.save(path)
    registry = CommandRegistry()
    _register_set_command(registry)
    actor = Actor(user_id="blocked-writer")
    op_log = OpLog()
    published: list[dict] = []
    monkeypatch.setattr(entity_store_module, "_LOCK_TIMEOUT_SECONDS", 0.03)

    with EntityFileStore(path, create_parent=False) as holder:
        holder.acquire_exclusive_lock()
        execution = CommitCoordinator(
            document,
            FileCommitStore(path, op_log),
            publisher=SimpleNamespace(
                publish=lambda payload: published.append(dict(payload))
            ),
        ).execute(lambda: registry.dispatch(
            "test.set",
            {"value": 2},
            actor=actor,
            source="user",
            doc=document,
        ))

    assert execution.receipt is not None
    assert execution.receipt.status is CommitStatus.ABORTED
    assert execution.receipt.failure is not None
    assert execution.receipt.failure.code == "document_lock_unavailable"
    assert execution.receipt.failure.facts.document is DurabilityState.NOT_DURABLE
    assert execution.receipt.failure.facts.rollback_succeeded is True
    assert document.version == 2
    assert document.get("item", "item-1").data == {"x": 1}
    assert Document.open(path).get("item", "item-1").data == {"x": 1}
    assert len(op_log) == 0
    assert published == []


def test_generation_write_failure_is_precommit_and_real_mutation_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "document.json"
    document = _document()
    document.save(path)
    before_head = path.read_bytes()
    registry = CommandRegistry()
    _register_set_command(registry)
    op_log = OpLog()

    def fail_generation(_store, _version, _payloads):
        raise EntityFileStoreError(
            "document_write_failed",
            "simulated_generation_write_failure",
        )

    monkeypatch.setattr(EntityFileStore, "write_generation", fail_generation)
    execution = CommitCoordinator(
        document,
        FileCommitStore(path, op_log),
    ).execute(lambda: registry.dispatch(
        "test.set",
        {"value": 2},
        actor=Actor(user_id="failed-writer"),
        source="user",
        doc=document,
    ))

    assert execution.receipt is not None
    assert execution.receipt.status is CommitStatus.ABORTED
    assert execution.receipt.failure is not None
    assert execution.receipt.failure.code == "document_write_failed"
    assert execution.receipt.failure.facts.document is DurabilityState.NOT_DURABLE
    assert execution.receipt.failure.facts.rollback_succeeded is True
    assert document.version == 2
    assert document.get("item", "item-1").data == {"x": 1}
    assert path.read_bytes() == before_head
    assert len(op_log) == 0


def test_history_prepare_is_restored_and_discarded_after_safe_store_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """The server's prepare/execute/discard pattern leaves no ghost undo state."""
    document = _document()
    actor = Actor(user_id="u-w04")
    op_log = OpLog()
    document.apply_patch_directly(
        Patch("item", "item-1", "x", "set", 1, 2),
        actor=actor,
        _replay_capability=_HISTORY_REPLAY_CAPABILITY,
    )
    edit = _set_operation(
        document,
        actor,
        command_id="entity.update",
        before=1,
        after=2,
    )
    op_log.append(edit)
    history = UndoController(op_log, document)
    before_entity = deepcopy(document.get("item", "item-1").to_dict())
    before_entries = op_log.entries()
    before_redo = list(history._redo_stack)
    prepared = {}

    def reject_before_durability(self, path, *args, **kwargs):
        raise WriteOutcomeError(
            "document was not written",
            outcome=DurabilityState.NOT_DURABLE,
        )

    monkeypatch.setattr(Document, "save", reject_before_durability)

    def producer():
        record = history.prepare_undo(actor=actor)
        prepared["record"] = record
        assert record is not None
        return record.operations[0]

    execution = CommitCoordinator(
        document,
        FileCommitStore(tmp_path / "document.json", op_log),
    ).execute(producer)
    record = prepared["record"]
    if execution.is_committed:
        history.confirm_deferred(record)
    else:
        history.discard_deferred(record)

    assert execution.receipt is not None
    assert execution.receipt.status is CommitStatus.ABORTED
    assert document.get("item", "item-1").to_dict() == before_entity
    assert op_log.entries() == before_entries
    assert history._redo_stack == before_redo
    assert history._pending_deferred == {}
    assert history.can_undo() is True
    assert history.can_redo() is False


def test_pipeline_multistep_producer_restores_update_when_created_batch_fails(
    tmp_path: Path,
):
    """The real stage producer cannot retain its first dispatch after the second fails."""
    document = _document()
    registry = CommandRegistry()
    register_builtin_commands(registry)
    actor = Actor(user_id="pipeline-user")
    store = NeverCommitStore()
    coordinator = CommitCoordinator(document, store)
    supervisor = PipelineSupervisor(
        doc=document,
        cmd_reg=registry,
        op_log=OpLog(),
        broker=SimpleNamespace(publish_change=lambda payload: None),
        data_dir=tmp_path,
        actor=actor,
        lock=threading.RLock(),
        commit_coordinator=coordinator,
    )
    entity = document.get("item", "item-1")
    entity_identity = entity
    before = deepcopy(entity.to_dict())
    claimed_before = entity.version
    state = SimpleNamespace(
        claimed={"work-1": claimed_before},
        trace_id="trace-pipeline-w04",
    )
    item = WorkItem(
        item_id="work-1",
        entity_type="item",
        entity_id="item-1",
        artifacts=(),
        payload={},
    )
    import artifact_scan as scan
    attempt = scan.begin_attempt(
        item,
        tmp_path,
        "run-w04",
        "stage-w04",
        single_stage=False,
    )

    result = supervisor._commit_stage(state, item, {
        "patches": {"x": 2},
        "created_entities": [
            {"type": "item", "id": "created-before-error", "data": {"x": 10}},
            # The first new entity is already added when this duplicate fails.
            {"type": "item", "id": "item-1", "data": {"x": 99}},
        ],
    }, attempt)

    assert result["ok"] is False
    assert result["error"]["code"] == "validation_failed"
    assert document.get("item", "item-1") is entity_identity
    assert document.get("item", "item-1").to_dict() == before
    assert document.find("item", "created-before-error") is None
    assert state.claimed["work-1"] == claimed_before
    assert store.calls == 0
