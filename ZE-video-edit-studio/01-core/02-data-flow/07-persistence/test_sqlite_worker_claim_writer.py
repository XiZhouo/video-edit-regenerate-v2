"""Real SQLite tests for the first fixed-SQL worker OUTBOX claim."""

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
from pathlib import Path
import sqlite3
import sys
import time

import pytest


_HERE = Path(__file__).resolve().parent
_DATA_STRUCTURE = _HERE.parents[1] / "01-data-structure"
for _path in (
    _HERE,
    _DATA_STRUCTURE / "01-document",
    _DATA_STRUCTURE / "02-entity",
    _DATA_STRUCTURE / "04-field-types",
):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from business_atomic_write_integrity import _issue_atomic_business_write_plan
from business_transaction_contract import (
    MAX_ATOMIC_JSON_PAYLOAD_BYTES,
    OutboxEventIntent,
    freeze_json_payload,
)
from business_transaction_integrity import _issue_atomic_commit_plan
from production_atomic_write_contract import (
    PRODUCTION_OUTBOX_IDEMPOTENCY_POLICY,
    PRODUCTION_OUTBOX_MAX_ATTEMPTS,
    PRODUCTION_OUTBOX_REPLAY_POLICY,
    ProductionWorkerTarget,
    ProductionWriteStatus,
)
from production_atomic_write_integrity import (
    _issue_production_atomic_write_plan,
    _production_worker_genesis_state_sha256,
)
from sqlite_business_runtime import (
    BusinessRuntimeAccess,
    inspect_business_runtime,
)
from sqlite_production_authority import (
    _read_production_atomic_planning_snapshot,
)
from sqlite_production_test_support import (
    create_canonical_mounted_business_database,
)
from sqlite_production_writer import SQLiteProductionAtomicWriter
from sqlite_kernel import _OwnedConnection
import sqlite_worker_claim_writer as worker_claim_module
from sqlite_worker_claim_writer import (
    SQLiteWorkerClaimError,
    SQLiteWorkerClaimStatus,
    SQLiteWorkerClaimTestPoint,
    SQLiteWorkerClaimWriter,
    _WorkerClaimAuthorizer,
)
from test_sqlite_production_writer import (
    _persist_worker_execution,
    _production_plan,
    _tamper_then_restore_schema,
)
from test_sqlite_business_transaction import (
    _next_before as _business_next_before,
    _plan as _business_plan,
)
from worker_execution_contract import (
    WorkerAttemptStart,
    WorkerBeforeState,
    WorkerClaimRequest,
    WorkerExecutionStatus,
    WorkerExpireRequest,
    WorkerLeaseToken,
    WorkerOutboxDispatchFence,
    WorkerOutboxDispatchState,
    WorkerRequestHeader,
    WorkerRenewRequest,
    WorkerOutboxOrder,
    WorkerTarget,
    WorkerTargetKind,
)
from worker_execution_integrity import _issue_worker_execution_plan


def _later(value: str, seconds: int) -> str:
    parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    return (
        (parsed + timedelta(seconds=seconds)).strftime(
            "%Y-%m-%dT%H:%M:%S.%f"
        )[:-3]
        + "Z"
    )


def _later_ms(value: str, milliseconds: int) -> str:
    parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    return (
        (parsed + timedelta(milliseconds=milliseconds)).strftime(
            "%Y-%m-%dT%H:%M:%S.%f"
        )[:-3]
        + "Z"
    )


def _one(database, sql, values=()):
    connection, _filesystem = database._open_unvalidated()
    try:
        row = connection.execute(sql, values).fetchone()
        return None if row is None else tuple(row)
    finally:
        connection.close()


def _fresh_claim_window(database, *, not_before: str) -> tuple[str, str]:
    """Anchor a fixture lease after expensive setup, using the writer's clock."""
    row = _one(database, "SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')")
    assert row is not None and isinstance(row[0], str)
    occurred_at = row[0]
    assert occurred_at >= not_before
    return occurred_at, _later(occurred_at, 60)


def _tamper_ignoring_checks(
    database,
    trigger_names,
    sql,
    values=(),
):
    """Restore canonical DDL after injecting a constraint-breaking row image."""

    raw = sqlite3.connect(database.path, isolation_level=None)
    try:
        trigger_sql = []
        for name in trigger_names:
            row = raw.execute(
                "SELECT sql FROM sqlite_schema WHERE type='trigger' AND name=?",
                (name,),
            ).fetchone()
            assert row is not None and isinstance(row[0], str)
            trigger_sql.append(row[0])
        raw.execute("PRAGMA ignore_check_constraints=ON")
        raw.execute("BEGIN IMMEDIATE")
        for name in trigger_names:
            raw.execute("DROP TRIGGER " + name)
        raw.execute(sql, values)
        for statement in trigger_sql:
            raw.execute(statement)
        raw.commit()
    finally:
        if raw.in_transaction:
            raw.rollback()
        raw.close()


def _claim_fixture(
    tmp_path,
    name="worker-claim.sqlite3",
    *,
    large_payload=False,
):
    baseline = create_canonical_mounted_business_database(tmp_path / name)
    production = (
        _production_plan_with_large_payload(baseline)
        if large_payload
        else _production_plan(baseline)
    )
    assert (
        SQLiteProductionAtomicWriter(baseline.database).commit(production).status
        is ProductionWriteStatus.COMMITTED
    )
    profile = inspect_business_runtime(
        baseline.database,
        access=BusinessRuntimeAccess.WRITER,
    )
    snapshot = _read_production_atomic_planning_snapshot(baseline.database)
    target = production.targets[0].target
    before = WorkerBeforeState(
        status=WorkerExecutionStatus.PENDING,
        state_revision=1,
        attempt_count=0,
        last_attempt_id=None,
        last_attempt_number=None,
        current_attempt_id=None,
        current_attempt_number=None,
        lease_owner=None,
        lease_fence=None,
        lease_expires_at=None,
        fence_high_water=0,
    )
    occurred_at, lease_expires_at = _fresh_claim_window(
        baseline.database,
        not_before=snapshot.committed_at,
    )
    dispatch_before = snapshot.outbox_dispatch_head.evidence.dispatch_state
    dispatch_after = WorkerOutboxDispatchState(
        dispatch_before.control_revision + 1,
        dispatch_before.published,
        target.outbox_order,
    )
    request = WorkerClaimRequest(
        WorkerRequestHeader(
            "a" * 32,
            profile.to_authority_binding(),
            target,
            before,
            occurred_at,
        ),
        WorkerAttemptStart(
            "b" * 32,
            1,
            None,
            None,
            "worker-a",
            1,
            occurred_at,
            lease_expires_at,
        ),
        WorkerOutboxDispatchFence(dispatch_before, dispatch_after),
    )
    plan = _issue_worker_execution_plan(
        request,
        before_ledger=snapshot.worker_ledger_head.evidence.ledger_head,
    )
    return baseline, production, plan


def _claim_with_times(plan, *, occurred_at, lease_expires_at):
    request = WorkerClaimRequest(
        WorkerRequestHeader(
            plan.request.header.request_transaction_id,
            plan.authority,
            plan.target,
            plan.before,
            occurred_at,
        ),
        WorkerAttemptStart(
            plan.request.attempt.attempt_id,
            1,
            None,
            None,
            plan.request.attempt.lease_owner,
            1,
            occurred_at,
            lease_expires_at,
        ),
        plan.request.dispatch_fence,
    )
    return _issue_worker_execution_plan(
        request,
        before_ledger=plan.before_ledger,
    )


def _expiry_after_claim(plan):
    after = plan.closure.after_state
    before = WorkerBeforeState(
        status=after.status,
        state_revision=after.state_revision,
        attempt_count=after.attempt_count,
        last_attempt_id=after.last_attempt_id,
        last_attempt_number=after.last_attempt_number,
        current_attempt_id=after.current_attempt_id,
        current_attempt_number=after.current_attempt_number,
        lease_owner=after.lease_owner,
        lease_fence=after.lease_fence,
        lease_expires_at=after.lease_expires_at,
        fence_high_water=after.fence_high_water,
    )
    expired_at = after.lease_expires_at
    dispatch = plan.request.dispatch_fence.after
    return _issue_worker_execution_plan(
        WorkerExpireRequest(
            WorkerRequestHeader(
                "c" * 32,
                plan.authority,
                plan.target,
                before,
                expired_at,
            ),
            WorkerLeaseToken(
                after.current_attempt_id,
                after.current_attempt_number,
                after.lease_owner,
                after.lease_fence,
                after.lease_expires_at,
            ),
            expired_at,
            WorkerExecutionStatus.RETRY,
            expired_at,
            WorkerOutboxDispatchFence(dispatch, dispatch),
        ),
        before_ledger=plan.after_ledger,
    )


def _production_from_atomic(atomic, planning):
    inner = _issue_atomic_business_write_plan(
        atomic_plan=atomic,
        control_plans=(),
        committed_at=planning.committed_at,
        before_worker_ledger=planning.worker_ledger_head.evidence.ledger_head,
    )
    event = atomic.outbox[0]
    target = WorkerTarget(
        target_kind=WorkerTargetKind.OUTBOX,
        target_id=event.event_id,
        project_id=atomic.before.project_id,
        generation=atomic.before.generation,
        document_id=atomic.before.document_id,
        origin_commit_id=atomic.commit_id,
        payload=event.payload,
        replay_policy=PRODUCTION_OUTBOX_REPLAY_POLICY,
        idempotency_policy=PRODUCTION_OUTBOX_IDEMPOTENCY_POLICY,
        idempotency_scope="outbox/%s/%d" % (
            atomic.before.project_id,
            atomic.before.generation,
        ),
        idempotency_key=event.event_id,
        max_attempts=PRODUCTION_OUTBOX_MAX_ATTEMPTS,
        outbox_order=WorkerOutboxOrder(
            planning.outbox_allocation_head.sequence + 1,
            atomic.candidate.authority_revision,
            event.ordinal,
        ),
    )
    wrapped = ProductionWorkerTarget(
        source_ordinal=event.ordinal,
        target=target,
        genesis_status=WorkerExecutionStatus.PENDING,
        genesis_state_sha256=_production_worker_genesis_state_sha256(
            target,
            status=WorkerExecutionStatus.PENDING,
        ),
        outbox_topic=event.topic,
        outbox_partition_key=event.partition_key,
    )
    return _issue_production_atomic_write_plan(
        atomic_write=inner,
        mounted_authority=planning.mounted_authority,
        targets=(wrapped,),
        before_outbox_allocation=planning.outbox_allocation_head,
        worker_ledger_head=planning.worker_ledger_head,
        outbox_dispatch_head=planning.outbox_dispatch_head,
    )


def _production_plan_with_current_worker_head(
    baseline,
    *,
    planning,
    suffix,
    before,
    authority,
    version,
):
    atomic = _business_plan(
        baseline,
        suffix=suffix,
        before=before,
        authority=authority,
        version=version,
    )
    return _production_from_atomic(atomic, planning)


def _production_plan_with_large_payload(baseline):
    base = _business_plan(baseline)
    original = base.outbox[0]
    payload = freeze_json_payload(
        {"blob": "x" * min(1024 * 1024, MAX_ATOMIC_JSON_PAYLOAD_BYTES - 64)},
        schema=original.payload.schema,
    )
    event = OutboxEventIntent(
        original.ordinal,
        original.event_id,
        original.operation_id,
        original.topic,
        original.partition_key,
        payload,
    )
    atomic = _issue_atomic_commit_plan(
        authority=base.authority,
        commit_id=base.commit_id,
        trace_id=base.trace_id,
        before=base.before,
        after_snapshot=base.after_snapshot,
        operations=base.operations,
        outbox=(event,),
        durable_actions=base.durable_actions,
        pipeline_intents=base.pipeline_intents,
        inbox=base.inbox,
        confirmation=base.confirmation,
    )
    planning = _read_production_atomic_planning_snapshot(baseline.database)
    return _production_from_atomic(atomic, planning)


def test_first_outbox_claim_commits_and_freshly_reconciles(tmp_path) -> None:
    baseline, _production, plan = _claim_fixture(tmp_path)
    writer = SQLiteWorkerClaimWriter(baseline.database)

    result = writer.commit(plan)

    assert result.status is SQLiteWorkerClaimStatus.COMMITTED
    assert not result.recovered_after_ack_loss
    assert writer.reconcile(plan).status is SQLiteWorkerClaimStatus.COMMITTED
    assert _one(
        baseline.database,
        "SELECT status,state_revision,attempt_count,current_attempt_id "
        "FROM worker_target_current WHERE target_id=?",
        (plan.target.target_id,),
    ) == ("leased", 2, 1, plan.request.attempt.attempt_id)
    assert _one(
        baseline.database,
        "SELECT sequence,tail_sha256 FROM worker_ledger_head WHERE singleton=1",
    ) == (plan.after_ledger.sequence, plan.after_ledger.tail_sha256)


def test_large_payload_origin_reads_are_streamed_and_target_is_not_refetched(
    tmp_path,
    monkeypatch,
) -> None:
    baseline, _production, plan = _claim_fixture(
        tmp_path,
        "large-stream.sqlite3",
        large_payload=True,
    )
    writer = SQLiteWorkerClaimWriter(baseline.database)
    assert writer.commit(plan).status is SQLiteWorkerClaimStatus.COMMITTED
    assert len(plan.target.payload.payload_bytes) > 1024 * 1024

    original_execute = _OwnedConnection.execute
    original_payload_constructor = worker_claim_module.FrozenJsonPayload
    raw_queries = []
    current_target_reads = 0
    current_event_reads = 0
    payload_constructor_calls = 0

    class SpyCursor:
        def __init__(self, cursor, sql):
            self._cursor = cursor
            self._sql = sql

        def fetchone(self):
            return self._cursor.fetchone()

        def fetchall(self):
            raise AssertionError(
                "raw payload cursor must never call fetchall: " + self._sql
            )

        def __iter__(self):
            return self

        def __next__(self):
            row = self._cursor.fetchone()
            if row is None:
                raise StopIteration
            return row

    def execute_spy(connection, sql, parameters=()):
        nonlocal current_target_reads, current_event_reads
        cursor = original_execute(connection, sql, parameters)
        normalized = " ".join(sql.lower().split())
        target_blob = (
            "payload_bytes" in normalized
            and "payload_sha256" in normalized
            and "from worker_targets" in normalized
        )
        event_blob = (
            "event_bytes" in normalized
            and "event_sha256" in normalized
            and "from event_outbox" in normalized
        )
        if not (target_blob or event_blob):
            return cursor
        raw_queries.append(normalized)
        if "from worker_targets where target_id=?" in normalized:
            current_target_reads += 1
        if "from event_outbox where event_id=?" in normalized:
            current_event_reads += 1
        return SpyCursor(cursor, normalized)

    monkeypatch.setattr(_OwnedConnection, "execute", execute_spy)

    def counted_payload_constructor(*args, **kwargs):
        nonlocal payload_constructor_calls
        payload_constructor_calls += 1
        return original_payload_constructor(*args, **kwargs)

    monkeypatch.setattr(
        worker_claim_module,
        "FrozenJsonPayload",
        counted_payload_constructor,
    )

    result = writer.reconcile(plan)

    assert result.status is SQLiteWorkerClaimStatus.COMMITTED
    assert raw_queries
    assert current_target_reads == 1
    assert current_event_reads == 1
    assert payload_constructor_calls == 0


def test_origin_digest_resource_failure_is_unknown(tmp_path, monkeypatch) -> None:
    baseline, _production, plan = _claim_fixture(
        tmp_path,
        "origin-memory-error.sqlite3",
    )
    writer = SQLiteWorkerClaimWriter(baseline.database)
    assert writer.commit(plan).status is SQLiteWorkerClaimStatus.COMMITTED

    def exhausted(_wrapped):
        raise MemoryError("digest allocation failed")

    monkeypatch.setattr(
        worker_claim_module,
        "_production_target_member_sha256",
        exhausted,
    )
    result = writer.reconcile(plan)

    assert result.status is SQLiteWorkerClaimStatus.UNKNOWN
    assert result.code == "worker_claim_reconcile_unavailable"


def test_first_outbox_claim_precommit_failure_rolls_back_every_row(tmp_path) -> None:
    baseline, _production, plan = _claim_fixture(tmp_path, "rollback.sqlite3")
    writer = SQLiteWorkerClaimWriter(baseline.database)

    def fail(point):
        if point is SQLiteWorkerClaimTestPoint.BEFORE_COMMIT:
            raise RuntimeError("rollback")

    result = writer.commit(plan, _test_hook=fail)

    assert result.status is SQLiteWorkerClaimStatus.NOT_COMMITTED
    assert writer.reconcile(plan).status is SQLiteWorkerClaimStatus.NOT_COMMITTED
    assert _one(
        baseline.database,
        "SELECT count(*) FROM worker_execution_receipts WHERE request_id=?",
        (plan.closure.request_transaction_id,),
    ) == (0,)
    assert _one(
        baseline.database,
        "SELECT status,state_revision FROM worker_target_current WHERE target_id=?",
        (plan.target.target_id,),
    ) == ("pending", 1)


def test_first_outbox_claim_expiring_inside_before_commit_hook_rolls_back(
    tmp_path,
) -> None:
    baseline, _production, original = _claim_fixture(
        tmp_path,
        "hook-expiry.sqlite3",
    )
    plan = _claim_with_times(
        original,
        occurred_at=original.request.header.occurred_at,
        lease_expires_at=_later_ms(
            original.request.header.occurred_at,
            1_400,
        ),
    )
    writer = SQLiteWorkerClaimWriter(baseline.database)

    def cross_expiry(point):
        if point is SQLiteWorkerClaimTestPoint.BEFORE_COMMIT:
            time.sleep(1.6)

    result = writer.commit(plan, _test_hook=cross_expiry)

    assert result.status is SQLiteWorkerClaimStatus.CONFLICT
    assert result.code == "worker_claim_lease_expired"
    assert writer.reconcile(plan).status is SQLiteWorkerClaimStatus.NOT_COMMITTED
    assert _one(
        baseline.database,
        "SELECT count(*) FROM worker_execution_receipts WHERE request_id=?",
        (plan.closure.request_transaction_id,),
    ) == (0,)
    assert _one(
        baseline.database,
        "SELECT status,state_revision,lease_owner,lease_expires_at "
        "FROM event_outbox WHERE event_id=?",
        (plan.target.target_id,),
    ) == ("pending", 1, None, None)
    assert _one(
        baseline.database,
        "SELECT sequence,state_revision FROM worker_ledger_head WHERE singleton=1",
    ) == (0, 1)


@pytest.mark.parametrize("failure_kind", ("corrupt", "conflict"))
def test_uncertain_rollback_after_written_rows_forces_fresh_reconcile(
    tmp_path,
    monkeypatch,
    failure_kind,
) -> None:
    baseline, _production, plan = _claim_fixture(
        tmp_path,
        "uncertain-rollback-%s.sqlite3" % failure_kind,
    )
    writer = SQLiteWorkerClaimWriter(baseline.database)
    original_rollback_close = worker_claim_module._rollback_close
    rollback_calls = []

    def rollback_but_report_uncertain(connection):
        rollback_calls.append(connection)
        assert original_rollback_close(connection)
        return False

    monkeypatch.setattr(
        worker_claim_module,
        "_rollback_close",
        rollback_but_report_uncertain,
    )
    if failure_kind == "corrupt":
        original_snapshot = SQLiteWorkerClaimWriter._reconcile_snapshot
        injected = False

        def corrupt_first_precommit(
            self,
            connection,
            current_plan,
            *,
            validate_runtime=True,
        ):
            nonlocal injected
            if not validate_runtime and not injected:
                injected = True
                return self._result(
                    current_plan,
                    SQLiteWorkerClaimStatus.CORRUPT,
                    "injected_precommit_corrupt",
                )
            return original_snapshot(
                self,
                connection,
                current_plan,
                validate_runtime=validate_runtime,
            )

        monkeypatch.setattr(
            SQLiteWorkerClaimWriter,
            "_reconcile_snapshot",
            corrupt_first_precommit,
        )
    else:
        original_time_check = (
            SQLiteWorkerClaimWriter._require_live_database_time
        )
        time_checks = 0

        def conflict_at_final_time_check(connection, current_plan):
            nonlocal time_checks
            time_checks += 1
            if time_checks == 2:
                raise worker_claim_module._ClaimConflict(
                    "injected_precommit_conflict"
                )
            return original_time_check(connection, current_plan)

        monkeypatch.setattr(
            SQLiteWorkerClaimWriter,
            "_require_live_database_time",
            staticmethod(conflict_at_final_time_check),
        )

    result = writer.commit(plan)

    assert rollback_calls
    assert result.status is SQLiteWorkerClaimStatus.NOT_COMMITTED
    assert result.code == "worker_claim_absent"
    assert _one(
        baseline.database,
        "SELECT count(*) FROM worker_execution_receipts WHERE request_id=?",
        (plan.closure.request_transaction_id,),
    ) == (0,)
    assert _one(
        baseline.database,
        "SELECT status,state_revision FROM worker_target_current WHERE target_id=?",
        (plan.target.target_id,),
    ) == ("pending", 1)


def test_first_outbox_claim_exact_retry_returns_same_durable_fact(tmp_path) -> None:
    baseline, _production, plan = _claim_fixture(tmp_path, "retry.sqlite3")
    writer = SQLiteWorkerClaimWriter(baseline.database)
    assert writer.commit(plan).status is SQLiteWorkerClaimStatus.COMMITTED

    retried = writer.commit(plan)

    assert retried.status is SQLiteWorkerClaimStatus.COMMITTED
    assert retried.recovered_after_ack_loss
    assert _one(
        baseline.database,
        "SELECT count(*) FROM worker_execution_receipts WHERE request_id=?",
        (plan.closure.request_transaction_id,),
    ) == (1,)


def test_first_outbox_claim_ack_loss_uses_fresh_reconcile(tmp_path) -> None:
    baseline, _production, plan = _claim_fixture(tmp_path, "ack-loss.sqlite3")
    writer = SQLiteWorkerClaimWriter(baseline.database)

    def lose_ack(point):
        if point is SQLiteWorkerClaimTestPoint.AFTER_COMMIT_BEFORE_ACK:
            raise RuntimeError("ack lost")

    result = writer.commit(plan, _test_hook=lose_ack)

    assert result.status is SQLiteWorkerClaimStatus.COMMITTED
    assert result.recovered_after_ack_loss
    assert writer.reconcile(plan).status is SQLiteWorkerClaimStatus.COMMITTED


def test_first_outbox_claim_rejects_expired_lease_using_sqlite_now(
    tmp_path,
) -> None:
    baseline, _production, original = _claim_fixture(
        tmp_path,
        "expired.sqlite3",
    )
    expiry = _later_ms(original.request.header.occurred_at, 400)
    plan = _claim_with_times(
        original,
        occurred_at=original.request.header.occurred_at,
        lease_expires_at=expiry,
    )
    time.sleep(0.55)

    result = SQLiteWorkerClaimWriter(baseline.database).commit(plan)

    assert result.status is SQLiteWorkerClaimStatus.CONFLICT
    assert result.code == "worker_claim_lease_expired"
    assert _one(
        baseline.database,
        "SELECT status,state_revision FROM worker_target_current WHERE target_id=?",
        (plan.target.target_id,),
    ) == ("pending", 1)


def test_first_outbox_claim_rejects_future_occurred_at_using_sqlite_now(
    tmp_path,
) -> None:
    baseline, _production, original = _claim_fixture(
        tmp_path,
        "future.sqlite3",
    )
    occurred_at = _later(original.request.header.occurred_at, 120)
    plan = _claim_with_times(
        original,
        occurred_at=occurred_at,
        lease_expires_at=_later(occurred_at, 60),
    )

    result = SQLiteWorkerClaimWriter(baseline.database).commit(plan)

    assert result.status is SQLiteWorkerClaimStatus.CONFLICT
    assert result.code == "worker_claim_time_future"


def test_old_claim_remains_exact_after_later_pure_business_commit(
    tmp_path,
) -> None:
    baseline, first, claim = _claim_fixture(
        tmp_path,
        "later-business.sqlite3",
    )
    claim_writer = SQLiteWorkerClaimWriter(baseline.database)
    assert claim_writer.commit(claim).status is SQLiteWorkerClaimStatus.COMMITTED
    profile = inspect_business_runtime(
        baseline.database,
        access=BusinessRuntimeAccess.WRITER,
    )
    planning = _read_production_atomic_planning_snapshot(baseline.database)
    second = _production_plan_with_current_worker_head(
        baseline,
        planning=planning,
        suffix="3",
        before=_business_next_before(first.atomic_write.atomic_plan),
        authority=profile.to_authority_binding(),
        version=3,
    )
    assert (
        SQLiteProductionAtomicWriter(baseline.database).commit(second).status
        is ProductionWriteStatus.COMMITTED
    )

    result = claim_writer.reconcile(claim)

    assert result.status is SQLiteWorkerClaimStatus.COMMITTED
    assert _one(
        baseline.database,
        "SELECT repository_revision FROM repository_runtime_control "
        "WHERE singleton=1",
    )[0] > claim.authority.authority_revision


def test_worker_ledger_suffix_stays_unknown_when_auditor_lacks_operation(
    tmp_path,
) -> None:
    baseline, _production, claim = _claim_fixture(
        tmp_path,
        "suffix-unknown.sqlite3",
    )
    writer = SQLiteWorkerClaimWriter(baseline.database)
    assert writer.commit(claim).status is SQLiteWorkerClaimStatus.COMMITTED
    _persist_worker_execution(baseline.database, _expiry_after_claim(claim))

    result = writer.reconcile(claim)

    assert result.status is SQLiteWorkerClaimStatus.UNKNOWN
    assert result.code == "worker_durable_audit_operation_unsupported"


@pytest.mark.parametrize("phase", ("before", "after"))
def test_origin_outer_target_root_rejects_dual_topic_partition_tamper(
    tmp_path,
    phase,
) -> None:
    baseline, _production, plan = _claim_fixture(
        tmp_path,
        "origin-dual-%s.sqlite3" % phase,
    )
    writer = SQLiteWorkerClaimWriter(baseline.database)
    if phase == "after":
        assert writer.commit(plan).status is SQLiteWorkerClaimStatus.COMMITTED
    _tamper_then_restore_schema(
        baseline.database,
        ("worker_targets_no_update",),
        "UPDATE worker_targets SET outbox_topic=?,outbox_partition_key=? "
        "WHERE target_id=?",
        ("forged-topic", "forged-partition", plan.target.target_id),
    )
    _tamper_then_restore_schema(
        baseline.database,
        ("event_outbox_immutable_columns", "event_outbox_revision_guard"),
        "UPDATE event_outbox SET topic=?,partition_key=? WHERE event_id=?",
        ("forged-topic", "forged-partition", plan.target.target_id),
    )

    result = writer.commit(plan) if phase == "before" else writer.reconcile(plan)

    assert result.status is SQLiteWorkerClaimStatus.CORRUPT
    assert result.code == "worker_claim_origin_event_members_corrupt"


@pytest.mark.parametrize(
    ("field", "sql", "values"),
    (
        (
            "previous_event_id",
            "UPDATE event_outbox SET previous_event_id=? WHERE event_id=?",
            ("d" * 32,),
        ),
        (
            "previous_ordinal",
            "UPDATE event_outbox SET previous_ordinal=0 WHERE event_id=?",
            (),
        ),
        (
            "commit_event_count",
            "UPDATE event_outbox SET commit_event_count=2 WHERE event_id=?",
            (),
        ),
        (
            "operation_id",
            "UPDATE event_outbox SET operation_id=? WHERE event_id=?",
            ("e" * 32,),
        ),
    ),
)
def test_origin_pure_atomic_event_projection_rejects_each_immutable_tamper(
    tmp_path,
    field,
    sql,
    values,
) -> None:
    baseline, _production, plan = _claim_fixture(
        tmp_path,
        "origin-event-%s.sqlite3" % field,
    )
    writer = SQLiteWorkerClaimWriter(baseline.database)
    assert writer.commit(plan).status is SQLiteWorkerClaimStatus.COMMITTED
    _tamper_ignoring_checks(
        baseline.database,
        ("event_outbox_immutable_columns", "event_outbox_revision_guard"),
        sql,
        (*values, plan.target.target_id),
    )

    result = writer.reconcile(plan)

    assert result.status is SQLiteWorkerClaimStatus.CORRUPT
    assert result.code == "worker_claim_origin_event_members_corrupt"


@pytest.mark.parametrize(
    ("field", "sql", "values"),
    (
        (
            "atomic_manifest_sha256",
            "UPDATE atomic_business_write_receipts "
            "SET atomic_manifest_sha256=? WHERE commit_id=?",
            (b"a" * 32,),
        ),
        (
            "pipeline_intents_sha256",
            "UPDATE atomic_business_write_receipts "
            "SET pipeline_intents_sha256=? WHERE commit_id=?",
            (b"b" * 32,),
        ),
        (
            "controls_sha256",
            "UPDATE atomic_business_write_receipts "
            "SET controls_sha256=? WHERE commit_id=?",
            (b"c" * 32,),
        ),
        (
            "control_count",
            "UPDATE atomic_business_write_receipts "
            "SET control_count=1 WHERE commit_id=?",
            (),
        ),
        (
            "worker_ledger",
            "UPDATE atomic_business_write_receipts "
            "SET before_worker_sequence=1,before_worker_tail_sha256=?,"
            "after_worker_sequence=1,after_worker_tail_sha256=? "
            "WHERE commit_id=?",
            (b"d" * 32, b"d" * 32),
        ),
        (
            "pure_manifest_bytes",
            "UPDATE atomic_business_write_receipts "
            "SET manifest_bytes=? WHERE commit_id=?",
            (b"{}",),
        ),
        (
            "receipt_sha256",
            "UPDATE atomic_business_write_receipts "
            "SET receipt_sha256=? WHERE commit_id=?",
            (b"e" * 32,),
        ),
    ),
)
def test_origin_pure_atomic_receipt_rejects_each_root_field_tamper(
    tmp_path,
    field,
    sql,
    values,
) -> None:
    baseline, _production, plan = _claim_fixture(
        tmp_path,
        "origin-atomic-%s.sqlite3" % field,
    )
    writer = SQLiteWorkerClaimWriter(baseline.database)
    assert writer.commit(plan).status is SQLiteWorkerClaimStatus.COMMITTED
    _tamper_ignoring_checks(
        baseline.database,
        ("atomic_business_write_receipts_no_update",),
        sql,
        (*values, plan.target.origin_commit_id),
    )

    result = writer.reconcile(plan)

    assert result.status is SQLiteWorkerClaimStatus.CORRUPT
    assert result.code.startswith("worker_claim_origin_atomic_")


@pytest.mark.parametrize("field", ("available_at", "last_error"))
def test_reconcile_checks_complete_after_event_projection(
    tmp_path,
    field,
) -> None:
    baseline, _production, plan = _claim_fixture(
        tmp_path,
        "event-%s.sqlite3" % field,
    )
    writer = SQLiteWorkerClaimWriter(baseline.database)
    assert writer.commit(plan).status is SQLiteWorkerClaimStatus.COMMITTED
    if field == "available_at":
        sql = "UPDATE event_outbox SET available_at=? WHERE event_id=?"
        values = (_later(plan.expected_receipt.occurred_at, 1), plan.target.target_id)
    else:
        error = b"forged-error"
        sql = (
            "UPDATE event_outbox SET last_error_bytes=?,last_error_sha256=? "
            "WHERE event_id=?"
        )
        values = (error, hashlib.sha256(error).digest(), plan.target.target_id)
    _tamper_then_restore_schema(
        baseline.database,
        ("event_outbox_revision_guard",),
        sql,
        values,
    )

    result = writer.reconcile(plan)

    assert result.status is SQLiteWorkerClaimStatus.CORRUPT
    assert result.code == "worker_claim_durable_closure_corrupt"


def test_absent_receipt_with_partial_current_residue_is_not_not_committed(
    tmp_path,
) -> None:
    baseline, _production, plan = _claim_fixture(
        tmp_path,
        "partial-absent.sqlite3",
    )
    _tamper_then_restore_schema(
        baseline.database,
        ("worker_target_current_update_binding_guard",),
        """
        UPDATE worker_target_current
        SET state_revision=state_revision+1,
            previous_state_sha256=state_sha256,
            state_sha256=?
        WHERE target_id=?
        """,
        (b"p" * 32, plan.target.target_id),
    )

    result = SQLiteWorkerClaimWriter(baseline.database).reconcile(plan)

    assert result.status is SQLiteWorkerClaimStatus.CORRUPT
    assert result.status is not SQLiteWorkerClaimStatus.NOT_COMMITTED
    assert result.code == "worker_claim_current_corrupt"


@pytest.mark.parametrize(
    "collision",
    ("attempt_id", "receipt_transaction_id"),
)
def test_absent_rejects_cross_target_orphan_claim_identity(
    tmp_path,
    collision,
) -> None:
    baseline, _production, plan = _claim_fixture(
        tmp_path,
        "orphan-%s.sqlite3" % collision,
    )
    attempt_id = (
        plan.request.attempt.attempt_id
        if collision == "attempt_id"
        else "d" * 32
    )
    receipt_transaction_id = (
        plan.expected_receipt.receipt_parent.transaction_id
        if collision == "receipt_transaction_id"
        else "_worker-execution:" + "e" * 32
    )
    _tamper_then_restore_schema(
        baseline.database,
        ("worker_attempts_receipt_binding_guard",),
        """
        INSERT INTO worker_attempts(
            attempt_id,target_id,attempt_number,previous_attempt_id,
            execution_fence,owner_id,started_at,receipt_transaction_id
        ) VALUES (?,?,1,NULL,1,?,?,?)
        """,
        (
            attempt_id,
            "f" * 32,
            "orphan-worker",
            plan.request.header.occurred_at,
            receipt_transaction_id,
        ),
    )

    result = SQLiteWorkerClaimWriter(baseline.database).reconcile(plan)

    assert result.status is SQLiteWorkerClaimStatus.CORRUPT
    assert result.code == "worker_claim_absent_identity_occupied"


def test_mid_mount_read_io_failure_is_unknown(tmp_path, monkeypatch) -> None:
    baseline, _production, plan = _claim_fixture(
        tmp_path,
        "mid-read.sqlite3",
    )
    writer = SQLiteWorkerClaimWriter(baseline.database)
    assert writer.commit(plan).status is SQLiteWorkerClaimStatus.COMMITTED

    def unavailable(_connection):
        raise OSError("mid-read storage failure")

    monkeypatch.setattr(worker_claim_module, "_mounted_row", unavailable)
    result = writer.reconcile(plan)

    assert result.status is SQLiteWorkerClaimStatus.UNKNOWN
    assert result.code == "worker_claim_runtime_or_mount_unavailable"


@pytest.mark.parametrize("tamper", ("state_revision", "previous_tail"))
def test_worker_ledger_head_requires_unique_revision_and_direct_predecessor(
    tmp_path,
    tamper,
) -> None:
    baseline, _production, claim = _claim_fixture(
        tmp_path,
        "ledger-head-%s.sqlite3" % tamper,
    )
    writer = SQLiteWorkerClaimWriter(baseline.database)
    assert writer.commit(claim).status is SQLiteWorkerClaimStatus.COMMITTED
    _persist_worker_execution(baseline.database, _expiry_after_claim(claim))
    if tamper == "state_revision":
        sql = "UPDATE worker_ledger_head SET state_revision=2 WHERE singleton=1"
        values = ()
    else:
        sql = (
            "UPDATE worker_ledger_head SET previous_tail_sha256=zeroblob(32) "
            "WHERE singleton=1"
        )
        values = ()
    _tamper_then_restore_schema(
        baseline.database,
        (
            "worker_ledger_head_cas_update_guard",
            "worker_ledger_head_sequence_guard",
        ),
        sql,
        values,
    )

    result = writer.reconcile(claim)

    assert result.status is SQLiteWorkerClaimStatus.CORRUPT
    assert result.code == "worker_claim_ledger_corrupt"


def test_first_outbox_claim_fresh_reconcile_rejects_current_tamper(
    tmp_path,
) -> None:
    baseline, _production, plan = _claim_fixture(tmp_path, "tamper.sqlite3")
    writer = SQLiteWorkerClaimWriter(baseline.database)
    assert writer.commit(plan).status is SQLiteWorkerClaimStatus.COMMITTED

    _tamper_then_restore_schema(
        baseline.database,
        ("worker_target_current_update_binding_guard",),
        """
        UPDATE worker_target_current
        SET state_revision=state_revision+1,
            previous_state_sha256=state_sha256,
            state_sha256=?
        WHERE target_id=?
        """,
        (b"x" * 32, plan.target.target_id),
    )

    result = writer.reconcile(plan)
    assert result.status is SQLiteWorkerClaimStatus.CORRUPT
    assert result.code == "worker_claim_durable_closure_corrupt"


def test_first_outbox_claim_reserved_identity_conflict_never_writes(
    tmp_path,
) -> None:
    baseline, _production, plan = _claim_fixture(tmp_path, "conflict.sqlite3")
    connection, _filesystem = baseline.database._open_unvalidated()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO transaction_receipts(transaction_id,kind,committed_at) "
            "VALUES (?,?,?)",
            (
                plan.expected_receipt.receipt_parent.transaction_id,
                "occupied_by_other_kind",
                plan.expected_receipt.receipt_parent.committed_at,
            ),
        )
        connection.commit()
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()
    baseline.database._finalize_committed_storage()

    result = SQLiteWorkerClaimWriter(baseline.database).commit(plan)
    assert result.status is SQLiteWorkerClaimStatus.CONFLICT
    assert result.code == "worker_claim_identity_conflict"
    assert _one(
        baseline.database,
        "SELECT count(*) FROM worker_execution_receipts WHERE request_id=?",
        (plan.closure.request_transaction_id,),
    ) == (0,)
    assert _one(
        baseline.database,
        "SELECT status,state_revision FROM worker_target_current WHERE target_id=?",
        (plan.target.target_id,),
    ) == ("pending", 1)


def test_first_outbox_claim_unavailable_fresh_read_is_unknown(
    tmp_path,
    monkeypatch,
) -> None:
    baseline, _production, plan = _claim_fixture(tmp_path, "unknown.sqlite3")

    def unavailable(_database):
        raise OSError("storage unavailable")

    monkeypatch.setattr(
        type(baseline.database),
        "_open_unvalidated",
        unavailable,
    )
    result = SQLiteWorkerClaimWriter(baseline.database).reconcile(plan)
    assert result.status is SQLiteWorkerClaimStatus.UNKNOWN
    assert result.code == "worker_claim_reconcile_unavailable"


def test_worker_claim_authorizer_is_action_and_table_exact() -> None:
    authorizer = _WorkerClaimAuthorizer()
    assert authorizer(
        sqlite3.SQLITE_INSERT,
        "worker_attempts",
        None,
        "main",
        None,
    ) == sqlite3.SQLITE_OK
    assert authorizer(
        sqlite3.SQLITE_UPDATE,
        "event_outbox",
        "status",
        "main",
        None,
    ) == sqlite3.SQLITE_OK
    assert authorizer(
        sqlite3.SQLITE_UPDATE,
        "event_outbox",
        "available_at",
        "main",
        None,
    ) == sqlite3.SQLITE_DENY
    assert authorizer(
        sqlite3.SQLITE_UPDATE,
        "business_commits",
        "repository_revision",
        "main",
        None,
    ) == sqlite3.SQLITE_DENY
    assert authorizer(
        sqlite3.SQLITE_DELETE,
        "event_outbox",
        None,
        "main",
        None,
    ) == sqlite3.SQLITE_DENY
    assert authorizer.denied


def test_worker_claim_writer_fixed_rejects_every_non_claim_operation(
    tmp_path,
) -> None:
    baseline, _production, claim = _claim_fixture(
        tmp_path,
        "unsupported.sqlite3",
    )
    after = claim.closure.after_state
    before = WorkerBeforeState(
        status=after.status,
        state_revision=after.state_revision,
        attempt_count=after.attempt_count,
        last_attempt_id=after.last_attempt_id,
        last_attempt_number=after.last_attempt_number,
        current_attempt_id=after.current_attempt_id,
        current_attempt_number=after.current_attempt_number,
        lease_owner=after.lease_owner,
        lease_fence=after.lease_fence,
        lease_expires_at=after.lease_expires_at,
        fence_high_water=after.fence_high_water,
    )
    occurred_at = _later(claim.request.header.occurred_at, 30)
    renew = _issue_worker_execution_plan(
        WorkerRenewRequest(
            WorkerRequestHeader(
                "c" * 32,
                claim.authority,
                claim.target,
                before,
                occurred_at,
            ),
            WorkerLeaseToken(
                after.current_attempt_id,
                after.current_attempt_number,
                after.lease_owner,
                after.lease_fence,
                after.lease_expires_at,
            ),
            _later(after.lease_expires_at, 60),
        ),
        before_ledger=claim.after_ledger,
    )

    with pytest.raises(SQLiteWorkerClaimError) as rejected:
        SQLiteWorkerClaimWriter(baseline.database).commit(renew)
    assert rejected.value.code == "worker_claim_vertical_unsupported"


__all__ = ()
