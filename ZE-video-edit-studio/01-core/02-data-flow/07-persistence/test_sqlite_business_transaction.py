"""Frozen schema-v7 commit/reconcile/crash evidence for the retired writer."""

from __future__ import annotations

import asyncio
import hashlib
import multiprocessing
import os
import sqlite3
import sys
from pathlib import Path

import pytest


_HERE = Path(__file__).resolve().parent
_DATA_STRUCTURE = _HERE.parents[1] / "01-data-structure"
for _directory in (
    _HERE,
    _DATA_STRUCTURE / "01-document",
    _DATA_STRUCTURE / "02-entity",
    _DATA_STRUCTURE / "04-field-types",
):
    if str(_directory) not in sys.path:
        sys.path.insert(0, str(_directory))

from business_snapshot import capture_business_snapshot  # noqa: E402
from business_transaction_contract import (  # noqa: E402
    ATOMIC_COMMIT_STATUS_ABORTED,
    ATOMIC_COMMIT_STATUS_COMMITTED,
    ATOMIC_COMMIT_STATUS_INDETERMINATE,
    ActionReplayPolicy,
    AtomicFailureKind,
    BeforeBusinessHead,
    DurableActionIntent,
    IdempotencyPolicy,
    InboxClaim,
    InboxOutcomeKind,
    OutboxEventIntent,
    ConfirmationConsumption,
    PipelineIntent,
    PipelineIntentKind,
    PipelineJobStatus,
    PipelineReplayPolicy,
    freeze_json_payload,
)
from business_transaction_integrity import _issue_atomic_commit_plan  # noqa: E402
from document import Document  # noqa: E402
from entity import Entity  # noqa: E402
from operation_record_codec import freeze_operation_record  # noqa: E402
import sqlite_business_audit as business_audit_module  # noqa: E402
import sqlite_business_runtime as business_runtime_module  # noqa: E402
import sqlite_business_test_support as business_support_module  # noqa: E402
import sqlite_kernel as sqlite_kernel_module  # noqa: E402
import sqlite_schema as sqlite_schema_module  # noqa: E402
from sqlite_business_runtime import (  # noqa: E402
    BusinessRuntimeAccess,
    inspect_business_runtime,
)
from sqlite_business_audit import startup_business_runtime_full_audit  # noqa: E402
from sqlite_business_test_support import (  # noqa: E402
    add_historical_v7_active_business_test_project,
    create_historical_v7_active_business_database,
)
from sqlite_business_transaction import (  # noqa: E402
    BusinessCommitTestPoint,
    BusinessReconcileStatus,
    SQLiteBusinessTransactionWriter,
    _BusinessWriterAuthorizer,
    _canonical_operation_time,
)
from sqlite_kernel import SQLiteKernelError, SqliteDatabase  # noqa: E402


@pytest.fixture(autouse=True)
def _freeze_retired_writer_to_historical_v7(monkeypatch):
    """Model one coherent v7 binary for the full lifetime of every test."""

    historical = sqlite_schema_module.MIGRATIONS[:7]
    assert tuple(migration.version for migration in historical) == tuple(range(1, 8))
    for module in (
        sqlite_schema_module,
        sqlite_kernel_module,
        business_runtime_module,
        business_audit_module,
        business_support_module,
    ):
        monkeypatch.setattr(module, "MIGRATIONS", historical)


def _snapshot(baseline, version: int, suffix: str):
    document = Document(
        id=baseline.document_id,
        name="changed-" + suffix,
        schema_version="1.0.0",
        collaboration_mode="with_save",
        save_policy="auto_commit",
        schema={},
    )
    document._v = version
    document._created_at = 1
    document._updated_at = version
    return capture_business_snapshot(
        baseline.project_id,
        baseline.generation,
        document,
    ).snapshot


def _operation(baseline, suffix: str, timestamp=1.2349):
    operation_id = suffix * 32
    trace_id = ("f" if suffix != "f" else "e") * 32
    return freeze_operation_record(
        {
            "id": operation_id,
            "command_id": "document.rename",
            "source": "user",
            "actor": {"user_id": "writer-test", "source": "user"},
            "ts": timestamp,
            "patches": [
                {
                    "entity_type": "document",
                    "entity_id": baseline.document_id,
                    "field": "name",
                    "op": "name",
                    "before": "before",
                    "after": "after-" + suffix,
                }
            ],
            "reverse_patches": [
                {
                    "entity_type": "document",
                    "entity_id": baseline.document_id,
                    "field": "name",
                    "op": "name",
                    "before": "after-" + suffix,
                    "after": "before",
                }
            ],
            "group_id": "",
            "trace_id": trace_id,
            "kind": "native",
            "error": None,
            "document_id": baseline.document_id,
        }
    )


def _plan(
    baseline,
    suffix: str = "2",
    *,
    before=None,
    authority=None,
    version: int = 2,
    actions=(),
    pipelines=(),
    inbox=None,
    confirmation=None,
    after_snapshot=None,
    topic: str = "document_change",
):
    operation = _operation(baseline, suffix)
    trace_id = operation.trace_id
    event_suffix = hex((int(suffix, 16) + 7) % 16)[2:]
    payload = freeze_json_payload(
        {"operation_id": operation.operation_id},
        schema="ze.document-change.v1",
    )
    if before is None:
        before = BeforeBusinessHead(
            project_id=baseline.project_id,
            generation=baseline.generation,
            document_id=baseline.document_id,
            document_version=baseline.document_version,
            project_revision=1,
            head_revision=1,
            expected_last_commit_id=baseline.commit_id,
            business_root_sha256=baseline.business_root_sha256,
            operation_tail_hash=baseline.operation_tail_sha256.hex(),
        )
    return _issue_atomic_commit_plan(
        authority=(
            baseline.profile.to_authority_binding() if authority is None else authority
        ),
        commit_id=hex((int(suffix, 16) + 1) % 16)[2:] * 32,
        trace_id=trace_id,
        before=before,
        after_snapshot=(
            _snapshot(baseline, version, suffix)
            if after_snapshot is None
            else after_snapshot
        ),
        operations=(operation,),
        outbox=(
            OutboxEventIntent(
                0,
                event_suffix * 32,
                operation.operation_id,
                topic,
                baseline.project_id,
                payload,
            ),
        ),
        durable_actions=actions,
        pipeline_intents=pipelines,
        inbox=inbox,
        confirmation=confirmation,
    )


def _next_before(plan):
    return BeforeBusinessHead(
        project_id=plan.before.project_id,
        generation=plan.before.generation,
        document_id=plan.before.document_id,
        document_version=plan.candidate.document_version,
        project_revision=plan.candidate.project_revision,
        head_revision=plan.candidate.head_revision,
        expected_last_commit_id=plan.commit_id,
        business_root_sha256=plan.candidate.business_root_sha256,
        operation_tail_hash=plan.candidate.operation_tail_hash,
    )


def _read_one(database, sql, values=()):
    connection, _filesystem = database._open_unvalidated()
    try:
        return connection.execute(sql, values).fetchone()
    finally:
        connection.close()


def test_commit_fresh_reconcile_and_exact_retry(tmp_path) -> None:
    baseline = create_historical_v7_active_business_database(tmp_path / "business.sqlite3")
    plan = _plan(baseline)
    writer = SQLiteBusinessTransactionWriter(baseline.database)

    committed = writer.commit(plan)
    assert committed.status == ATOMIC_COMMIT_STATUS_COMMITTED
    assert committed.repository_revision == 2
    assert committed.recovered_after_ack_loss is False
    assert writer.reconcile(plan).status is BusinessReconcileStatus.COMMITTED

    exact_retry = writer.commit(plan)
    assert exact_retry.status == ATOMIC_COMMIT_STATUS_COMMITTED
    assert exact_retry.repository_revision == 2
    assert exact_retry.recovered_after_ack_loss is True


def test_same_commit_identity_with_different_manifest_is_corrupt(tmp_path) -> None:
    baseline = create_historical_v7_active_business_database(tmp_path / "business.sqlite3")
    writer = SQLiteBusinessTransactionWriter(baseline.database)
    original = _plan(baseline, topic="document_change")
    assert writer.commit(original).status == ATOMIC_COMMIT_STATUS_COMMITTED
    mismatched = _plan(baseline, topic="different_topic")
    result = writer.commit(mismatched)
    assert result.status == ATOMIC_COMMIT_STATUS_INDETERMINATE
    assert result.failure.kind is AtomicFailureKind.DURABLE_CORRUPT
    assert writer.reconcile(mismatched).status is BusinessReconcileStatus.CORRUPT


def test_fixed_sql_treats_identifiers_and_topics_as_data(tmp_path) -> None:
    baseline = create_historical_v7_active_business_database(tmp_path / "business.sqlite3")
    plan = _plan(
        baseline,
        topic="x'); DROP TABLE business_commits; --",
    )
    result = SQLiteBusinessTransactionWriter(baseline.database).commit(plan)
    assert result.status == ATOMIC_COMMIT_STATUS_COMMITTED
    assert _read_one(
        baseline.database,
        "SELECT count(*) FROM business_commits",
    )[0] == 2


def test_authorizer_denies_schema_transaction_and_unowned_writes() -> None:
    authorizer = _BusinessWriterAuthorizer()
    assert authorizer(
        sqlite3.SQLITE_DROP_TABLE, "business_commits", None, "main", None
    ) == sqlite3.SQLITE_DENY
    assert authorizer(
        sqlite3.SQLITE_PRAGMA, "writable_schema", "ON", "main", None
    ) == sqlite3.SQLITE_DENY
    assert authorizer(
        sqlite3.SQLITE_PRAGMA, "foreign_key_check", None, "main", None
    ) == sqlite3.SQLITE_DENY
    assert authorizer(
        sqlite3.SQLITE_UPDATE, "schema_migrations", "checksum", "main", None
    ) == sqlite3.SQLITE_DENY
    assert authorizer.denied is True


def test_precommit_exception_rolls_back_every_member(tmp_path) -> None:
    baseline = create_historical_v7_active_business_database(tmp_path / "business.sqlite3")
    plan = _plan(baseline)

    def crash(point):
        if point is BusinessCommitTestPoint.BEFORE_COMMIT:
            raise RuntimeError("simulated")

    result = SQLiteBusinessTransactionWriter(baseline.database).commit(
        plan, _test_hook=crash
    )
    assert result.status == ATOMIC_COMMIT_STATUS_ABORTED
    assert result.failure.kind is AtomicFailureKind.EXACT_NOT_COMMITTED
    assert _read_one(
        baseline.database,
        "SELECT count(*) FROM business_commits",
    )[0] == 1


def test_same_cardinality_wrong_outbox_rolls_back_before_commit(
    tmp_path,
    monkeypatch,
) -> None:
    baseline = create_historical_v7_active_business_database(tmp_path / "business.sqlite3")
    plan = _plan(baseline)
    original = SQLiteBusinessTransactionWriter._write_outbox
    injected = []

    def write_wrong_outbox(connection, active_plan, committed_at):
        original_events = active_plan.outbox
        event = original_events[0]
        wrong_event = OutboxEventIntent(
            ordinal=event.ordinal,
            event_id=event.event_id,
            operation_id=event.operation_id,
            topic=event.topic,
            partition_key=event.partition_key,
            payload=freeze_json_payload(
                {"wrong_same_cardinality": True},
                schema=event.payload.schema,
            ),
        )
        object.__setattr__(active_plan, "outbox", (wrong_event,))
        try:
            original(connection, active_plan, committed_at)
            injected.append(wrong_event.payload.payload_sha256)
        finally:
            object.__setattr__(active_plan, "outbox", original_events)

    monkeypatch.setattr(
        SQLiteBusinessTransactionWriter,
        "_write_outbox",
        staticmethod(write_wrong_outbox),
    )
    writer = SQLiteBusinessTransactionWriter(baseline.database)
    result = writer.commit(plan)

    assert len(injected) == 1
    assert injected[0] != plan.outbox[0].payload.payload_sha256
    assert result.status == ATOMIC_COMMIT_STATUS_ABORTED
    assert result.failure.kind is AtomicFailureKind.EXACT_NOT_COMMITTED
    assert writer.reconcile(plan).status is BusinessReconcileStatus.NOT_COMMITTED
    assert _read_one(
        baseline.database,
        "SELECT count(*) FROM business_commits WHERE commit_id = ?",
        (plan.commit_id,),
    )[0] == 0
    assert _read_one(
        baseline.database,
        "SELECT count(*) FROM event_outbox WHERE commit_id = ?",
        (plan.commit_id,),
    )[0] == 0
    assert _read_one(
        baseline.database,
        "SELECT repository_revision FROM repository_runtime_control",
    )[0] == 1


def test_wrong_current_document_bytes_roll_back_before_commit(
    tmp_path,
    monkeypatch,
) -> None:
    baseline = create_historical_v7_active_business_database(tmp_path / "business.sqlite3")
    plan = _plan(baseline)
    original = SQLiteBusinessTransactionWriter._write_current_snapshot
    injected = []

    def write_wrong_document(connection, active_plan):
        original(connection, active_plan)
        raw = active_plan.after_snapshot.document.snapshot_bytes
        wrong = bytes((raw[0] ^ 1,)) + raw[1:]
        assert wrong != raw and len(wrong) == len(raw)
        changed = connection.execute(
            "UPDATE documents SET snapshot_bytes = ? WHERE project_id = ?",
            (wrong, active_plan.before.project_id),
        ).rowcount
        assert changed == 1
        injected.append(wrong)

    monkeypatch.setattr(
        SQLiteBusinessTransactionWriter,
        "_write_current_snapshot",
        staticmethod(write_wrong_document),
    )
    writer = SQLiteBusinessTransactionWriter(baseline.database)
    result = writer.commit(plan)

    assert len(injected) == 1
    assert injected[0] != plan.after_snapshot.document.snapshot_bytes
    assert result.status == ATOMIC_COMMIT_STATUS_ABORTED
    assert result.failure.kind is AtomicFailureKind.EXACT_NOT_COMMITTED
    assert writer.reconcile(plan).status is BusinessReconcileStatus.NOT_COMMITTED
    assert _read_one(
        baseline.database,
        "SELECT snapshot_bytes FROM documents WHERE project_id = ?",
        (baseline.project_id,),
    )[0] == baseline.snapshot.document.snapshot_bytes
    assert _read_one(
        baseline.database,
        "SELECT count(*) FROM business_commits WHERE commit_id = ?",
        (plan.commit_id,),
    )[0] == 0


def test_deferred_foreign_key_rejects_delta_injected_after_exact_closure(
    tmp_path,
    monkeypatch,
) -> None:
    baseline = create_historical_v7_active_business_database(tmp_path / "business.sqlite3")
    plan = _plan(baseline)
    original = SQLiteBusinessTransactionWriter._verify_exact_precommit_closure
    injected = []

    def verify_then_inject_orphan(self, connection, active_plan):
        original(self, connection, active_plan)
        document = active_plan.after_snapshot.document
        connection.execute(
            """
            INSERT INTO documents(
                project_id, document_id, document_version, schema_version,
                collaboration_mode, save_policy, codec_id, codec_version,
                snapshot_format_version, snapshot_bytes, snapshot_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "orphan-project",
                "orphan-document",
                document.document_version,
                document.schema_version,
                document.collaboration_mode,
                document.save_policy,
                document.codec_id,
                document.codec_version,
                document.snapshot_format_version,
                document.snapshot_bytes,
                document.snapshot_sha256,
            ),
        )
        injected.append(True)

    monkeypatch.setattr(
        SQLiteBusinessTransactionWriter,
        "_verify_exact_precommit_closure",
        verify_then_inject_orphan,
    )
    writer = SQLiteBusinessTransactionWriter(baseline.database)
    result = writer.commit(plan)
    assert injected == [True]
    assert result.status == ATOMIC_COMMIT_STATUS_ABORTED
    assert result.failure.kind is AtomicFailureKind.EXACT_NOT_COMMITTED
    assert writer.reconcile(plan).status is BusinessReconcileStatus.NOT_COMMITTED
    assert _read_one(
        baseline.database,
        "SELECT count(*) FROM documents WHERE project_id='orphan-project'",
    )[0] == 0


@pytest.mark.parametrize("signal", [KeyboardInterrupt, asyncio.CancelledError])
def test_process_control_before_commit_rolls_back_and_propagates(
    tmp_path, signal
) -> None:
    baseline = create_historical_v7_active_business_database(tmp_path / "business.sqlite3")
    plan = _plan(baseline)

    def cancel(point):
        if point is BusinessCommitTestPoint.BEFORE_COMMIT:
            raise signal()

    with pytest.raises(signal):
        SQLiteBusinessTransactionWriter(baseline.database).commit(
            plan, _test_hook=cancel
        )
    assert SQLiteBusinessTransactionWriter(baseline.database).reconcile(
        plan
    ).status is BusinessReconcileStatus.NOT_COMMITTED


def test_ack_loss_is_reconciled_as_committed(tmp_path) -> None:
    baseline = create_historical_v7_active_business_database(tmp_path / "business.sqlite3")
    plan = _plan(baseline)

    def lose_ack(point):
        if point is BusinessCommitTestPoint.AFTER_COMMIT_BEFORE_ACK:
            raise RuntimeError("ack lost")

    result = SQLiteBusinessTransactionWriter(baseline.database).commit(
        plan, _test_hook=lose_ack
    )
    assert result.status == ATOMIC_COMMIT_STATUS_COMMITTED
    assert result.recovered_after_ack_loss is True


def test_postcommit_keyboard_interrupt_is_classified_then_rethrown(tmp_path) -> None:
    baseline = create_historical_v7_active_business_database(tmp_path / "business.sqlite3")
    plan = _plan(baseline)

    def interrupt(point):
        if point is BusinessCommitTestPoint.AFTER_COMMIT_BEFORE_ACK:
            raise KeyboardInterrupt()

    writer = SQLiteBusinessTransactionWriter(baseline.database)
    with pytest.raises(KeyboardInterrupt):
        writer.commit(plan, _test_hook=interrupt)
    assert writer.reconcile(plan).status is BusinessReconcileStatus.COMMITTED


def test_durability_barrier_failure_is_unknown_not_success(
    tmp_path, monkeypatch
) -> None:
    baseline = create_historical_v7_active_business_database(tmp_path / "business.sqlite3")
    plan = _plan(baseline)

    def fail_barrier():
        raise OSError("simulated directory fsync failure")

    original = baseline.database._finalize_committed_storage
    monkeypatch.setattr(
        baseline.database, "_finalize_committed_storage", fail_barrier
    )
    result = SQLiteBusinessTransactionWriter(baseline.database).commit(plan)
    assert result.status == ATOMIC_COMMIT_STATUS_INDETERMINATE
    assert result.failure.kind is AtomicFailureKind.OUTCOME_UNKNOWN
    monkeypatch.setattr(
        baseline.database, "_finalize_committed_storage", original
    )
    assert SQLiteBusinessTransactionWriter(baseline.database).reconcile(
        plan
    ).status is BusinessReconcileStatus.COMMITTED


def test_later_commit_does_not_erase_earlier_commit_proof(tmp_path) -> None:
    baseline = create_historical_v7_active_business_database(tmp_path / "business.sqlite3")
    writer = SQLiteBusinessTransactionWriter(baseline.database)
    first = _plan(baseline, "2")
    assert writer.commit(first).status == ATOMIC_COMMIT_STATUS_COMMITTED
    profile = inspect_business_runtime(
        baseline.database,
        access=BusinessRuntimeAccess.WRITER,
    )
    second = _plan(
        baseline,
        "4",
        before=_next_before(first),
        authority=profile.to_authority_binding(),
        version=3,
    )
    assert writer.commit(second).status == ATOMIC_COMMIT_STATUS_COMMITTED
    assert writer.reconcile(first).status is BusinessReconcileStatus.COMMITTED
    assert writer.reconcile(second).status is BusinessReconcileStatus.COMMITTED


def test_global_revision_can_advance_without_relabeling_project_or_head(
    tmp_path,
) -> None:
    baseline = create_historical_v7_active_business_database(tmp_path / "business.sqlite3")
    second = add_historical_v7_active_business_test_project(
        baseline.database,
        project_id="project-v7-unrelated",
        document_id="document-v7-unrelated",
        commit_id="9" * 32,
    )
    profile = second.profile
    plan = _plan(
        baseline,
        authority=profile.to_authority_binding(),
    )
    assert plan.before.project_revision == 1
    assert plan.before.head_revision == 1
    assert plan.authority.authority_revision == 2
    result = SQLiteBusinessTransactionWriter(baseline.database).commit(plan)
    assert result.status == ATOMIC_COMMIT_STATUS_COMMITTED
    assert result.repository_revision == 3
    commit_revisions = _read_one(
        baseline.database,
        """
        SELECT repository_revision, project_revision, head_revision
        FROM business_commits WHERE commit_id = ?
        """,
        (plan.commit_id,),
    )
    assert tuple(commit_revisions) == (3, 2, 2)


def test_full_audit_rejects_non_newest_live_local_projection(tmp_path) -> None:
    baseline = create_historical_v7_active_business_database(tmp_path / "business.sqlite3")
    connection = sqlite3.connect(str(baseline.database.path), isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        baseline_rows = {
            "projects": dict(connection.execute(
                "SELECT * FROM projects WHERE project_id=?",
                (baseline.project_id,),
            ).fetchone()),
            "documents": dict(connection.execute(
                "SELECT * FROM documents WHERE project_id=? AND document_id=?",
                (baseline.project_id, baseline.document_id),
            ).fetchone()),
            "repository_projects": dict(connection.execute(
                "SELECT * FROM repository_projects "
                "WHERE project_id=? AND generation=?",
                (baseline.project_id, baseline.generation),
            ).fetchone()),
            "document_commit_heads": dict(connection.execute(
                "SELECT * FROM document_commit_heads "
                "WHERE project_id=? AND generation=? AND document_id=?",
                (baseline.project_id, baseline.generation, baseline.document_id),
            ).fetchone()),
        }
    finally:
        connection.close()

    writer = SQLiteBusinessTransactionWriter(baseline.database)
    assert writer.commit(_plan(baseline, "2")).status == (
        ATOMIC_COMMIT_STATUS_COMMITTED
    )
    assert add_historical_v7_active_business_test_project(
        baseline.database,
        project_id="project-local-audit-b",
        document_id="document-local-audit-b",
        commit_id="4" * 32,
    ).profile.repository_revision == 3

    connection = sqlite3.connect(str(baseline.database.path), isolation_level=None)
    connection.row_factory = sqlite3.Row
    controlled_tables = tuple(baseline_rows)
    placeholders = ",".join("?" for _ in controlled_tables)
    triggers = connection.execute(
        "SELECT name, sql FROM sqlite_schema "
        "WHERE type='trigger' AND tbl_name IN (" + placeholders + ") "
        "ORDER BY name",
        controlled_tables,
    ).fetchall()
    primary_keys = {
        "projects": ("project_id",),
        "documents": ("project_id", "document_id"),
        "repository_projects": ("project_id", "generation"),
        "document_commit_heads": (
            "project_id", "generation", "document_id",
        ),
    }
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("BEGIN IMMEDIATE")
        for trigger in triggers:
            connection.execute('DROP TRIGGER "' + trigger["name"] + '"')
        for table, values in baseline_rows.items():
            keys = primary_keys[table]
            columns = tuple(values)
            assignments = ",".join('"' + name + '"=?' for name in columns)
            predicate = " AND ".join('"' + name + '"=?' for name in keys)
            connection.execute(
                'UPDATE "' + table + '" SET ' + assignments + " WHERE " + predicate,
                tuple(values[name] for name in columns)
                + tuple(values[name] for name in keys),
            )
        for trigger in triggers:
            connection.execute(trigger["sql"])
        connection.commit()
        connection.execute("PRAGMA foreign_keys=ON")
        assert connection.execute("PRAGMA foreign_key_check").fetchone() is None
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()

    with pytest.raises(SQLiteKernelError) as stale:
        startup_business_runtime_full_audit(baseline.database)
    assert stale.value.code == "sqlite_business_current_projection_stale"


def test_full_audit_rejects_a_gap_in_one_local_revision_stream(tmp_path) -> None:
    baseline = create_historical_v7_active_business_database(tmp_path / "business.sqlite3")
    writer = SQLiteBusinessTransactionWriter(baseline.database)
    second = _plan(baseline, "2")
    assert writer.commit(second).status == ATOMIC_COMMIT_STATUS_COMMITTED
    assert add_historical_v7_active_business_test_project(
        baseline.database,
        project_id="project-local-gap-b",
        document_id="document-local-gap-b",
        commit_id="4" * 32,
    ).profile.repository_revision == 3

    connection = sqlite3.connect(str(baseline.database.path), isolation_level=None)
    connection.row_factory = sqlite3.Row
    controlled_tables = (
        "business_commits",
        "repository_projects",
        "document_commit_heads",
    )
    placeholders = ",".join("?" for _ in controlled_tables)
    triggers = connection.execute(
        "SELECT name, sql FROM sqlite_schema "
        "WHERE type='trigger' AND tbl_name IN (" + placeholders + ") "
        "ORDER BY name",
        controlled_tables,
    ).fetchall()
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("BEGIN IMMEDIATE")
        for trigger in triggers:
            connection.execute('DROP TRIGGER "' + trigger["name"] + '"')
        assert connection.execute(
            "UPDATE business_commits SET project_revision=3, head_revision=3 "
            "WHERE commit_id=?",
            (second.commit_id,),
        ).rowcount == 1
        assert connection.execute(
            "UPDATE repository_projects SET project_revision=3 "
            "WHERE project_id=? AND generation=?",
            (baseline.project_id, baseline.generation),
        ).rowcount == 1
        assert connection.execute(
            "UPDATE document_commit_heads SET head_revision=3 "
            "WHERE project_id=? AND generation=? AND document_id=?",
            (baseline.project_id, baseline.generation, baseline.document_id),
        ).rowcount == 1
        for trigger in triggers:
            connection.execute(trigger["sql"])
        connection.commit()
        connection.execute("PRAGMA foreign_keys=ON")
        assert connection.execute("PRAGMA foreign_key_check").fetchone() is None
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()

    with pytest.raises(SQLiteKernelError) as gap:
        startup_business_runtime_full_audit(baseline.database)
    assert gap.value.code == "sqlite_business_local_revision_history_invalid"


def test_later_runtime_control_transition_preserves_historical_commit(tmp_path) -> None:
    baseline = create_historical_v7_active_business_database(tmp_path / "business.sqlite3")
    plan = _plan(baseline)
    writer = SQLiteBusinessTransactionWriter(baseline.database)
    assert writer.commit(plan).status == ATOMIC_COMMIT_STATUS_COMMITTED

    connection, _filesystem = baseline.database._open_unvalidated()
    try:
        connection.execute("BEGIN IMMEDIATE")
        current_updated_at = connection.execute(
            "SELECT updated_at FROM repository_runtime_control WHERE singleton=1"
        ).fetchone()[0]
        connection.execute(
            """
            UPDATE repository_runtime_control
            SET authority_state = 'read_only', state_revision = state_revision + 1,
                updated_at = ?
            WHERE singleton = 1 AND authority_state = 'active'
            """,
            (current_updated_at,),
        )
        connection.commit()
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()
    baseline.database._finalize_committed_storage()
    assert writer.reconcile(plan).status is BusinessReconcileStatus.COMMITTED


def test_repair_required_runtime_never_returns_positive_commit(tmp_path) -> None:
    baseline = create_historical_v7_active_business_database(tmp_path / "business.sqlite3")
    plan = _plan(baseline)
    writer = SQLiteBusinessTransactionWriter(baseline.database)
    assert writer.commit(plan).status == ATOMIC_COMMIT_STATUS_COMMITTED
    connection, _filesystem = baseline.database._open_unvalidated()
    try:
        connection.execute("BEGIN IMMEDIATE")
        current_updated_at = connection.execute(
            "SELECT updated_at FROM repository_runtime_control WHERE singleton=1"
        ).fetchone()[0]
        connection.execute(
            """
            UPDATE repository_runtime_control
            SET authority_state = 'repair_required',
                state_revision = state_revision + 1, updated_at = ?
            WHERE singleton = 1 AND authority_state = 'active'
            """,
            (current_updated_at,),
        )
        connection.commit()
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()
    baseline.database._finalize_committed_storage()
    reconciled = writer.reconcile(plan)
    assert reconciled.status is BusinessReconcileStatus.CORRUPT


def test_action_and_pipeline_create_share_commit_boundary(tmp_path) -> None:
    baseline = create_historical_v7_active_business_database(tmp_path / "business.sqlite3")
    action_payload = freeze_json_payload(
        {"channel": "review"}, schema="ze.action.notify.v1"
    )
    pipeline_payload = freeze_json_payload(
        {"name": "render"}, schema="ze.pipeline.create.v1"
    )
    actions = (
        DurableActionIntent(
            0,
            "a" * 32,
            "notify.push",
            "notify-request-1",
            action_payload,
            replay_policy=ActionReplayPolicy.RECONCILE_BEFORE_RETRY,
            idempotency_policy=IdempotencyPolicy.DURABLE_RECEIPT_RECONCILE,
            idempotency_scope="provider/account-1",
        ),
    )
    pipelines = (
        PipelineIntent(
            0,
            "b" * 32,
            "b" * 32,
            "render-pipeline",
            "create",
            "pipeline-create-1",
            pipeline_payload,
            intent_kind=PipelineIntentKind.CREATE,
            replay_policy=PipelineReplayPolicy.EXACT_CAS_RETRY,
        ),
    )
    plan = _plan(baseline, actions=actions, pipelines=pipelines)
    writer = SQLiteBusinessTransactionWriter(baseline.database)
    assert writer.commit(plan).status == ATOMIC_COMMIT_STATUS_COMMITTED
    assert writer.reconcile(plan).status is BusinessReconcileStatus.COMMITTED
    assert _read_one(
        baseline.database,
        "SELECT status FROM deferred_actions WHERE action_id = ?",
        ("a" * 32,),
    )[0] == "queued"
    assert _read_one(
        baseline.database,
        "SELECT status FROM pipeline_jobs WHERE job_id = ?",
        ("b" * 32,),
    )[0] == "queued"


def test_pipeline_control_transition_is_cas_and_append_only(tmp_path) -> None:
    baseline = create_historical_v7_active_business_database(tmp_path / "business.sqlite3")
    payload = freeze_json_payload(
        {"name": "render"}, schema="ze.pipeline.create.v1"
    )
    create = PipelineIntent(
        0,
        "b" * 32,
        "b" * 32,
        "render-pipeline",
        "create",
        "pipeline-create-1",
        payload,
        intent_kind=PipelineIntentKind.CREATE,
    )
    writer = SQLiteBusinessTransactionWriter(baseline.database)
    first = _plan(baseline, "2", pipelines=(create,))
    assert writer.commit(first).status == ATOMIC_COMMIT_STATUS_COMMITTED

    control_payload = freeze_json_payload(
        {"reason": "operator pause"}, schema="ze.pipeline.pause.v1"
    )
    pause = PipelineIntent(
        0,
        "c" * 32,
        "b" * 32,
        "render-pipeline",
        "pause",
        "pipeline-pause-1",
        control_payload,
        intent_kind=PipelineIntentKind.TRANSITION,
        expected_status=PipelineJobStatus.QUEUED,
        target_status=PipelineJobStatus.PAUSED,
        expected_state_revision=1,
    )
    profile = inspect_business_runtime(
        baseline.database, access=BusinessRuntimeAccess.WRITER
    )
    second = _plan(
        baseline,
        "4",
        before=_next_before(first),
        authority=profile.to_authority_binding(),
        version=3,
        pipelines=(pause,),
    )
    assert writer.commit(second).status == ATOMIC_COMMIT_STATUS_COMMITTED
    assert tuple(_read_one(
        baseline.database,
        "SELECT status, state_revision FROM pipeline_jobs WHERE job_id = ?",
        ("b" * 32,),
    )) == ("paused", 2)
    assert writer.reconcile(first).status is BusinessReconcileStatus.COMMITTED
    assert writer.reconcile(second).status is BusinessReconcileStatus.COMMITTED


def test_absent_commit_detects_pipeline_composite_identity_collision(
    tmp_path,
) -> None:
    baseline = create_historical_v7_active_business_database(tmp_path / "business.sqlite3")
    payload = freeze_json_payload(
        {"name": "render"}, schema="ze.pipeline.create.v1"
    )
    existing = PipelineIntent(
        0,
        "b" * 32,
        "b" * 32,
        "render-pipeline",
        "create",
        "shared-pipeline-key",
        payload,
        intent_kind=PipelineIntentKind.CREATE,
    )
    writer = SQLiteBusinessTransactionWriter(baseline.database)
    first = _plan(baseline, "2", pipelines=(existing,))
    assert writer.commit(first).status == ATOMIC_COMMIT_STATUS_COMMITTED

    colliding = PipelineIntent(
        0,
        "d" * 32,
        "d" * 32,
        "render-pipeline",
        "create",
        "shared-pipeline-key",
        payload,
        intent_kind=PipelineIntentKind.CREATE,
    )
    profile = inspect_business_runtime(
        baseline.database, access=BusinessRuntimeAccess.WRITER
    )
    second = _plan(
        baseline,
        "4",
        before=_next_before(first),
        authority=profile.to_authority_binding(),
        version=3,
        pipelines=(colliding,),
    )
    reconciled = writer.reconcile(second)
    assert reconciled.status is BusinessReconcileStatus.CONFLICT
    result = writer.commit(second)
    assert result.status == ATOMIC_COMMIT_STATUS_ABORTED
    assert result.failure.kind is AtomicFailureKind.IDENTITY_CONFLICT


def _seed_inbox_and_confirmation(database, baseline, request, nonce_digest) -> None:
    connection, _filesystem = database._open_unvalidated()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT INTO idempotency_inbox(
                scope, idempotency_key, request_sha256, request_bytes,
                request_schema, request_codec_id, request_codec_version,
                actor_id, claim_owner_id, claim_fence, status, state_revision,
                lease_expires_at, claimed_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'claimed', ?, ?, ?, ?)
            """,
            (
                "project/" + baseline.project_id,
                "request-1",
                request.payload_sha256,
                request.payload_bytes,
                request.schema,
                request.codec_id,
                request.codec_version,
                "user-1",
                "8" * 32,
                10,
                9,
                "2026-07-13T00:00:00.000Z",
                "2026-07-12T00:00:00.000Z",
                "2026-07-12T00:00:00.000Z",
            ),
        )
        connection.execute(
            """
            INSERT INTO confirmation_requests(
                confirmation_id, project_id, generation, document_id,
                actor_id, approval_nonce_hmac, nonce_key_id, request_bytes,
                request_sha256, request_schema, request_codec_id,
                request_codec_version, status, state_revision,
                reservation_owner_id, reservation_fence,
                reservation_expires_at, expires_at, created_at, updated_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved', ?, ?, ?,
                ?, ?, ?, ?
            )
            """,
            (
                "9" * 32,
                baseline.project_id,
                baseline.generation,
                baseline.document_id,
                "user-1",
                nonce_digest,
                "confirmation-key-1",
                request.payload_bytes,
                request.payload_sha256,
                request.schema,
                request.codec_id,
                request.codec_version,
                3,
                "b" * 32,
                11,
                "2026-07-13T00:00:00.000Z",
                "2026-07-14T00:00:00.000Z",
                "2026-07-12T00:00:00.000Z",
                "2026-07-12T00:00:00.000Z",
            ),
        )
        connection.commit()
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()
    database._finalize_committed_storage()


def test_inbox_and_confirmation_are_consumed_with_business_head(tmp_path) -> None:
    baseline = create_historical_v7_active_business_database(tmp_path / "business.sqlite3")
    request = freeze_json_payload(
        {"command": "document.rename"}, schema="ze.inbox.request.v1"
    )
    nonce_digest = hashlib.sha256(b"confirmation-nonce").digest()
    _seed_inbox_and_confirmation(
        baseline.database, baseline, request, nonce_digest
    )
    response = freeze_json_payload(
        {"commit_id": "3" * 32}, schema="ze.inbox.response.v1"
    )
    inbox = InboxClaim(
        scope="project/" + baseline.project_id,
        inbox_key="request-1",
        actor_id="user-1",
        claim_owner_id="8" * 32,
        claim_fence=10,
        state_revision=9,
        outcome_kind=InboxOutcomeKind.COMMIT,
        request_sha256=request.payload_sha256,
        request=request,
        response=response,
    )
    confirmation = ConfirmationConsumption(
        confirmation_id="9" * 32,
        nonce_hmac=nonce_digest,
        nonce_key_id="confirmation-key-1",
        reservation_fence=11,
        reservation_owner_id="b" * 32,
        expected_revision=3,
        expected_request_sha256=request.payload_sha256,
        actor_id="user-1",
        resolution=freeze_json_payload(
            {"decision": "confirmed", "commit_id": "3" * 32},
            schema="ze.confirmation.resolution.v1",
        ),
    )
    plan = _plan(
        baseline,
        inbox=inbox,
        confirmation=confirmation,
    )
    writer = SQLiteBusinessTransactionWriter(baseline.database)
    assert writer.commit(plan).status == ATOMIC_COMMIT_STATUS_COMMITTED
    assert writer.reconcile(plan).status is BusinessReconcileStatus.COMMITTED
    assert tuple(_read_one(
        baseline.database,
        "SELECT status, result_kind, commit_id FROM idempotency_inbox",
    )) == ("committed", "commit", plan.commit_id)
    assert tuple(_read_one(
        baseline.database,
        "SELECT status, decision, result_commit_id FROM confirmation_requests",
    )) == ("consumed", "confirmed", plan.commit_id)


def test_absent_commit_detects_mismatched_inbox_and_confirmation_claims(
    tmp_path,
) -> None:
    baseline = create_historical_v7_active_business_database(tmp_path / "business.sqlite3")
    request = freeze_json_payload(
        {"command": "document.rename"}, schema="ze.inbox.request.v1"
    )
    nonce_digest = hashlib.sha256(b"confirmation-nonce").digest()
    _seed_inbox_and_confirmation(
        baseline.database, baseline, request, nonce_digest
    )
    mismatched_inbox = InboxClaim(
        scope="project/" + baseline.project_id,
        inbox_key="request-1",
        actor_id="user-1",
        claim_owner_id="7" * 32,
        claim_fence=10,
        state_revision=9,
        outcome_kind=InboxOutcomeKind.COMMIT,
        request_sha256=request.payload_sha256,
        request=request,
        response=freeze_json_payload(
            {"commit_id": "3" * 32}, schema="ze.inbox.response.v1"
        ),
    )
    writer = SQLiteBusinessTransactionWriter(baseline.database)
    inbox_plan = _plan(baseline, "2", inbox=mismatched_inbox)
    assert writer.reconcile(inbox_plan).status is BusinessReconcileStatus.CONFLICT

    mismatched_confirmation = ConfirmationConsumption(
        confirmation_id="9" * 32,
        nonce_hmac=nonce_digest,
        nonce_key_id="confirmation-key-1",
        reservation_fence=11,
        reservation_owner_id="a" * 32,
        expected_revision=3,
        expected_request_sha256=request.payload_sha256,
        actor_id="user-1",
        resolution=freeze_json_payload(
            {"decision": "confirmed", "commit_id": "5" * 32},
            schema="ze.confirmation.resolution.v1",
        ),
    )
    confirmation_plan = _plan(
        baseline,
        "4",
        confirmation=mismatched_confirmation,
    )
    assert (
        writer.reconcile(confirmation_plan).status
        is BusinessReconcileStatus.CONFLICT
    )


def test_unmanifested_confirmation_link_makes_reconcile_corrupt(tmp_path) -> None:
    baseline = create_historical_v7_active_business_database(tmp_path / "business.sqlite3")
    plan = _plan(baseline)
    writer = SQLiteBusinessTransactionWriter(baseline.database)
    assert writer.commit(plan).status == ATOMIC_COMMIT_STATUS_COMMITTED
    payload = freeze_json_payload(
        {"commit_id": plan.commit_id}, schema="ze.confirmation.extra.v1"
    )
    connection, _filesystem = baseline.database._open_unvalidated()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT INTO confirmation_requests(
                confirmation_id, project_id, generation, document_id,
                actor_id, approval_nonce_hmac, nonce_key_id, request_bytes,
                request_sha256, request_schema, request_codec_id,
                request_codec_version, status, state_revision, decision,
                resolution_bytes, resolution_sha256, resolution_schema,
                resolution_codec_id, resolution_codec_version,
                result_commit_id, expires_at, resolved_at, created_at,
                updated_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'consumed', 1,
                'confirmed', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                "e" * 32,
                baseline.project_id,
                baseline.generation,
                baseline.document_id,
                "unexpected-actor",
                hashlib.sha256(b"unexpected-confirmation").digest(),
                "unexpected-key",
                payload.payload_bytes,
                payload.payload_sha256,
                payload.schema,
                payload.codec_id,
                payload.codec_version,
                payload.payload_bytes,
                payload.payload_sha256,
                payload.schema,
                payload.codec_id,
                payload.codec_version,
                plan.commit_id,
                "2026-07-14T00:00:00.000Z",
                "2026-07-12T00:00:00.004Z",
                "2026-07-12T00:00:00.004Z",
                "2026-07-12T00:00:00.004Z",
            ),
        )
        connection.commit()
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()
    baseline.database._finalize_committed_storage()
    assert writer.reconcile(plan).status is BusinessReconcileStatus.CORRUPT


def test_unmanifested_current_entity_makes_reconcile_corrupt(tmp_path) -> None:
    baseline = create_historical_v7_active_business_database(tmp_path / "business.sqlite3")
    plan = _plan(baseline)
    writer = SQLiteBusinessTransactionWriter(baseline.database)
    assert writer.commit(plan).status == ATOMIC_COMMIT_STATUS_COMMITTED
    raw = b"{}"
    connection, _filesystem = baseline.database._open_unvalidated()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT INTO entities(
                project_id, document_id, entity_type, entity_id,
                entity_version, deleted, codec_id, codec_version,
                snapshot_format_version, snapshot_bytes, snapshot_sha256
            ) VALUES (?, ?, 'unexpected', 'unexpected-1', 1, 0,
                      'ze.deterministic-json', 1, 1, ?, ?)
            """,
            (
                baseline.project_id,
                baseline.document_id,
                raw,
                hashlib.sha256(raw).digest(),
            ),
        )
        connection.commit()
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()
    baseline.database._finalize_committed_storage()
    assert writer.reconcile(plan).status is BusinessReconcileStatus.CORRUPT


def test_current_document_payload_tamper_is_byte_exact_corruption(tmp_path) -> None:
    baseline = create_historical_v7_active_business_database(tmp_path / "business.sqlite3")
    plan = _plan(baseline)
    writer = SQLiteBusinessTransactionWriter(baseline.database)
    assert writer.commit(plan).status == ATOMIC_COMMIT_STATUS_COMMITTED
    original = plan.after_snapshot.document.snapshot_bytes
    tampered = original.replace(b"changed-2", b"changed-X")
    assert tampered != original and len(tampered) == len(original)
    connection, _filesystem = baseline.database._open_unvalidated()
    try:
        connection.execute("BEGIN IMMEDIATE")
        changed = connection.execute(
            """
            UPDATE documents SET snapshot_bytes = ?
            WHERE project_id = ? AND document_id = ?
            """,
            (tampered, baseline.project_id, baseline.document_id),
        ).rowcount
        assert changed == 1
        connection.commit()
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()
    baseline.database._finalize_committed_storage()
    assert writer.reconcile(plan).status is BusinessReconcileStatus.CORRUPT


def test_schema_aware_asset_occurrence_requires_available_blob(tmp_path) -> None:
    baseline = create_historical_v7_active_business_database(tmp_path / "business.sqlite3")
    asset_hex = "d" * 64
    document = Document(
        id=baseline.document_id,
        name="asset change",
        schema_version="1.0.0",
        collaboration_mode="with_save",
        save_policy="auto_commit",
        schema={
            "card": {
                "type": "object",
                "properties": {"image": {"type": "asset_ref"}},
            }
        },
    )
    entity = Entity(
        type="card",
        id="card-1",
        data={
            "image": {
                "sha256": asset_hex,
                "mime": "image/png",
                "size": 12,
                "ext": "png",
            }
        },
    )
    document.add(entity)
    entity._created_at = 1
    entity._updated_at = 2
    document._v = 2
    document._created_at = 1
    document._updated_at = 2
    after = capture_business_snapshot(
        baseline.project_id, baseline.generation, document
    ).snapshot

    unavailable_plan = _plan(baseline, after_snapshot=after)
    unavailable = SQLiteBusinessTransactionWriter(baseline.database).commit(
        unavailable_plan
    )
    assert unavailable.status == ATOMIC_COMMIT_STATUS_ABORTED
    assert unavailable.failure.kind is AtomicFailureKind.IDENTITY_CONFLICT

    connection, _filesystem = baseline.database._open_unvalidated()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT INTO asset_blobs(
                asset_sha256, content_key, media_type, extension, byte_length,
                status, state_revision, created_at, updated_at
            ) VALUES (?, ?, 'image/png', 'png', 12, 'available', 1, ?, ?)
            """,
            (
                bytes.fromhex(asset_hex),
                "sha256/dd/" + asset_hex,
                "2026-07-12T00:00:00.000Z",
                "2026-07-12T00:00:00.000Z",
            ),
        )
        connection.commit()
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()
    baseline.database._finalize_committed_storage()

    committed = SQLiteBusinessTransactionWriter(baseline.database).commit(
        unavailable_plan
    )
    assert committed.status == ATOMIC_COMMIT_STATUS_COMMITTED
    assert _read_one(
        baseline.database,
        "SELECT count(*) FROM document_asset_refs WHERE commit_id = ?",
        (unavailable_plan.commit_id,),
    )[0] == 1


def test_operation_time_is_platform_independent_floor_to_milliseconds() -> None:
    assert _canonical_operation_time(1.2349) == "1970-01-01T00:00:01.234Z"
    assert _canonical_operation_time(0) == "1970-01-01T00:00:00.000Z"


def _process_commit(path, plan, gate, queue):
    database = SqliteDatabase(path)
    gate.wait()
    result = SQLiteBusinessTransactionWriter(database).commit(plan)
    queue.put((result.status, None if result.failure is None else result.failure.kind.value))


def _process_hard_crash(path, plan, crash_point):
    database = SqliteDatabase(path)

    def crash(point):
        if point is crash_point:
            os._exit(71)

    SQLiteBusinessTransactionWriter(database).commit(plan, _test_hook=crash)
    os._exit(72)


@pytest.mark.parametrize(
    ("crash_point", "expected"),
    (
        (BusinessCommitTestPoint.BEFORE_COMMIT, BusinessReconcileStatus.NOT_COMMITTED),
        (
            BusinessCommitTestPoint.AFTER_COMMIT_BEFORE_ACK,
            BusinessReconcileStatus.COMMITTED,
        ),
    ),
)
def test_real_child_process_crash_has_exact_fresh_outcome(
    tmp_path,
    crash_point,
    expected,
) -> None:
    if "fork" not in multiprocessing.get_all_start_methods():
        pytest.skip("requires POSIX fork")
    baseline = create_historical_v7_active_business_database(tmp_path / "business.sqlite3")
    plan = _plan(baseline)
    context = multiprocessing.get_context("fork")
    process = context.Process(
        target=_process_hard_crash,
        args=(str(baseline.database.path), plan, crash_point),
    )
    process.start()
    process.join(timeout=20)
    assert process.exitcode == 71

    writer = SQLiteBusinessTransactionWriter(baseline.database)
    assert writer.reconcile(plan).status is expected
    retry = writer.commit(plan)
    assert retry.status == ATOMIC_COMMIT_STATUS_COMMITTED
    if crash_point is BusinessCommitTestPoint.AFTER_COMMIT_BEFORE_ACK:
        assert retry.recovered_after_ack_loss is True
    assert _read_one(
        baseline.database,
        "SELECT count(*) FROM business_commits WHERE commit_id = ?",
        (plan.commit_id,),
    )[0] == 1
    assert _read_one(
        baseline.database,
        "SELECT count(*) FROM business_operations WHERE commit_id = ?",
        (plan.commit_id,),
    )[0] == len(plan.operations)


def test_two_processes_from_same_head_have_one_winner(tmp_path) -> None:
    if "fork" not in multiprocessing.get_all_start_methods():
        pytest.skip("requires POSIX fork")
    baseline = create_historical_v7_active_business_database(tmp_path / "business.sqlite3")
    left = _plan(baseline, "2")
    right = _plan(baseline, "4")
    context = multiprocessing.get_context("fork")
    gate = context.Barrier(2)
    queue = context.Queue()
    processes = [
        context.Process(
            target=_process_commit,
            args=(str(baseline.database.path), plan, gate, queue),
        )
        for plan in (left, right)
    ]
    for process in processes:
        process.start()
    results = [queue.get(timeout=20) for _ in processes]
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0
    assert sum(status == ATOMIC_COMMIT_STATUS_COMMITTED for status, _ in results) == 1
    assert sorted(kind for _, kind in results if kind is not None) == [
        AtomicFailureKind.HEAD_CONFLICT.value
    ]


def test_missing_durable_member_is_corrupt_not_retryable(tmp_path) -> None:
    baseline = create_historical_v7_active_business_database(tmp_path / "business.sqlite3")
    plan = _plan(baseline)
    writer = SQLiteBusinessTransactionWriter(baseline.database)
    assert writer.commit(plan).status == ATOMIC_COMMIT_STATUS_COMMITTED

    raw = sqlite3.connect(str(baseline.database.path))
    try:
        raw.execute("PRAGMA foreign_keys = OFF")
        raw.execute("DROP TRIGGER business_operations_no_delete")
        raw.execute(
            "DELETE FROM business_operations WHERE operation_id = ?",
            (plan.operations[0].operation_id,),
        )
        raw.commit()
    finally:
        raw.close()
    reconciled = writer.reconcile(plan)
    assert reconciled.status is BusinessReconcileStatus.CORRUPT
