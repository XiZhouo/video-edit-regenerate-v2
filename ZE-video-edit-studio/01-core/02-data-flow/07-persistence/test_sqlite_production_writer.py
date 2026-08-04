"""Real schema-v8 transaction tests for the first production atomic vertical."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta
from pathlib import Path
import sqlite3
import sys

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
    PipelineIntent,
    PipelineIntentKind,
    PipelineJobStatus,
    freeze_json_payload,
)
from production_atomic_write_contract import (
    PRODUCTION_OUTBOX_IDEMPOTENCY_POLICY,
    PRODUCTION_OUTBOX_MAX_ATTEMPTS,
    PRODUCTION_OUTBOX_REPLAY_POLICY,
    ProductionAtomicWriteTestPoint,
    ProductionWorkerTarget,
    ProductionWriteStatus,
)
from production_atomic_write_integrity import (
    _issue_production_atomic_write_plan,
    _production_worker_genesis_state_sha256,
)
from sqlite_production_authority import (
    SQLiteProductionAuthorityError,
    _read_production_atomic_planning_snapshot,
)
from sqlite_production_test_support import (
    create_canonical_mounted_business_database,
    issue_mounted_production_atomic_write_plan,
)
from sqlite_business_runtime import (
    BusinessRuntimeAccess,
    inspect_business_runtime,
)
from sqlite_production_writer import (
    SQLiteProductionAtomicWriter,
    _ProductionWriterAuthorizer,
)
from test_sqlite_business_transaction import (
    _next_before as _business_next_before,
    _plan as _business_plan,
)
from test_sqlite_production_test_support import _multi_group_atomic_plan
from worker_execution_contract import (
    WorkerAttemptStart,
    WorkerBeforeState,
    WorkerClaimRequest,
    WorkerExecutionStatus,
    WorkerExpireRequest,
    WorkerIdempotencyPolicy,
    WorkerLeaseToken,
    WorkerLedgerHead,
    WorkerOutboxDispatchCursor,
    WorkerOutboxDispatchFence,
    WorkerOutboxDispatchState,
    WorkerOutboxOrder,
    WorkerReplayPolicy,
    WorkerRequestHeader,
    WorkerTarget,
    WorkerTargetKind,
    PipelineBusinessControlIntentBinding,
    PipelineBusinessControlOperation,
    PipelineBusinessControlRequest,
)
from worker_execution_integrity import (
    _issue_pipeline_business_control_plan,
    _issue_worker_execution_plan,
)


def _production_plan(
    baseline,
    *,
    topic="document_change",
    planning=None,
    suffix="2",
    before=None,
    authority=None,
    version=2,
):
    atomic = _business_plan(
        baseline,
        suffix=suffix,
        topic=topic,
        before=before,
        authority=authority,
        version=version,
    )
    snapshot = (
        _read_production_atomic_planning_snapshot(baseline.database)
        if planning is None
        else planning
    )
    inner = _issue_atomic_business_write_plan(
        atomic_plan=atomic,
        control_plans=(),
        committed_at=snapshot.committed_at,
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
            snapshot.outbox_allocation_head.sequence + 1,
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
        mounted_authority=snapshot.mounted_authority,
        targets=(wrapped,),
        before_outbox_allocation=snapshot.outbox_allocation_head,
        worker_ledger_head=snapshot.worker_ledger_head,
        outbox_dispatch_head=snapshot.outbox_dispatch_head,
    )


def _generic_production_plan(baseline):
    return issue_mounted_production_atomic_write_plan(
        baseline.database,
        _multi_group_atomic_plan(baseline),
    )


def _pipeline_transition_plan(baseline, first_plan):
    profile = inspect_business_runtime(
        baseline.database,
        access=BusinessRuntimeAccess.WRITER,
    )
    planning = _read_production_atomic_planning_snapshot(baseline.database)
    original_target = next(
        wrapped.target for wrapped in first_plan.targets
        if wrapped.target.target_kind is WorkerTargetKind.PIPELINE
    )
    intent = PipelineIntent(
        ordinal=0,
        intent_id="6" * 32,
        job_id=original_target.target_id,
        pipeline_id=original_target.pipeline_id,
        intent_type="pause",
        idempotency_key="pause-request-1",
        payload=freeze_json_payload(
            {"reason": "operator"},
            schema="test.pipeline-control.v1",
        ),
        intent_kind=PipelineIntentKind.TRANSITION,
        expected_status=PipelineJobStatus.QUEUED,
        target_status=PipelineJobStatus.PAUSED,
        expected_state_revision=1,
    )
    atomic = _business_plan(
        baseline,
        suffix="7",
        before=_business_next_before(first_plan.atomic_write.atomic_plan),
        authority=profile.to_authority_binding(),
        version=3,
        pipelines=(intent,),
    )
    replay = WorkerReplayPolicy(intent.replay_policy.value)
    control = _issue_pipeline_business_control_plan(
        PipelineBusinessControlRequest(
            request_transaction_id=intent.intent_id,
            authority=atomic.authority,
            target=original_target,
            control_intent=PipelineBusinessControlIntentBinding(
                origin_commit_id=atomic.commit_id,
                intent_type=intent.intent_type,
                idempotency_key=intent.idempotency_key,
                payload=intent.payload,
                replay_policy=replay,
                idempotency_policy=(
                    WorkerIdempotencyPolicy.SINGLE_ATTEMPT
                    if replay is WorkerReplayPolicy.NEVER_REPLAY
                    else WorkerIdempotencyPolicy.DURABLE_RECEIPT_RECONCILE
                ),
                max_attempts=intent.max_attempts,
            ),
            operation=PipelineBusinessControlOperation.APPLY_CONTROL_INTENT,
            before=WorkerBeforeState(
                status=WorkerExecutionStatus.QUEUED,
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
            ),
            target_status=WorkerExecutionStatus.PAUSED,
            occurred_at=planning.committed_at,
        ),
        before_ledger=planning.worker_ledger_head.evidence.ledger_head,
    )
    return issue_mounted_production_atomic_write_plan(
        baseline.database,
        atomic,
        planning=planning,
        control_plans=(control,),
    )


def _one(database, sql, values=()):
    connection, _filesystem = database._open_unvalidated()
    try:
        row = connection.execute(sql, values).fetchone()
        return None if row is None else tuple(row)
    finally:
        connection.close()


def _tamper_then_restore_schema(
    database,
    trigger_names,
    sql,
    values=(),
):
    """Mutate behind real guards, then restore byte-identical schema DDL."""

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


def _before_from_after(after) -> WorkerBeforeState:
    return WorkerBeforeState(
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
        current_effect=after.current_effect,
        unresolved_effect=after.unresolved_effect,
        next_attempt_at=after.next_attempt_at,
        cancellation_pending=after.cancellation_pending,
    )


def _persist_worker_execution(database, execution) -> None:
    receipt = execution.expected_receipt
    parent = receipt.receipt_parent
    after = execution.closure.after_state
    connection, _filesystem = database._open_unvalidated()
    committed = False
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO transaction_receipts(transaction_id,kind,committed_at) "
            "VALUES (?,?,?)",
            (parent.transaction_id, parent.kind, parent.committed_at),
        )
        connection.execute(
            """
            INSERT INTO worker_ledger_entries(
                sequence,entry_kind,transaction_id,receipt_kind,committed_at,
                previous_tail_sha256,ledger_tail_sha256,request_sha256,
                after_state_sha256,receipt_sha256,occurred_at
            ) VALUES (?,'worker_execution',?,?,?,?,?,?,?,?,?)
            """,
            (
                receipt.sequence,
                parent.transaction_id,
                parent.kind,
                parent.committed_at,
                receipt.previous_ledger_tail_sha256,
                receipt.ledger_tail_sha256,
                receipt.request_sha256,
                receipt.after_state_sha256,
                receipt.receipt_sha256,
                receipt.occurred_at,
            ),
        )
        connection.execute(
            """
            INSERT INTO worker_execution_receipts(
                request_id,transaction_id,entry_kind,receipt_kind,
                committed_at,operation,target_kind,target_id,before_status,
                after_status,before_state_revision,after_state_revision,
                sequence,previous_ledger_tail_sha256,ledger_tail_sha256,
                manifest_bytes,request_sha256,after_state_sha256,receipt_sha256
            ) VALUES (?,?,'worker_execution',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                receipt.request_transaction_id,
                parent.transaction_id,
                parent.kind,
                parent.committed_at,
                receipt.operation.value,
                receipt.target_kind.value,
                receipt.target_id,
                receipt.before_status.value,
                receipt.after_status.value,
                receipt.before_state_revision,
                receipt.after_state_revision,
                receipt.sequence,
                receipt.previous_ledger_tail_sha256,
                receipt.ledger_tail_sha256,
                execution.closure.manifest_bytes,
                receipt.request_sha256,
                receipt.after_state_sha256,
                receipt.receipt_sha256,
            ),
        )
        if execution.operation.value == "claim":
            attempt = execution.request.attempt
            connection.execute(
                """
                INSERT INTO worker_attempts(
                    attempt_id,target_id,attempt_number,previous_attempt_id,
                    execution_fence,owner_id,started_at,receipt_transaction_id
                ) VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    attempt.attempt_id,
                    receipt.target_id,
                    attempt.attempt_number,
                    attempt.previous_attempt_id,
                    attempt.execution_fence,
                    attempt.lease_owner,
                    attempt.started_at,
                    parent.transaction_id,
                ),
            )
        connection.execute(
            """
            UPDATE event_outbox
            SET status=?,state_revision=?,attempt_count=?,
                available_at=COALESCE(?,available_at),lease_owner=?,
                lease_fence=?,lease_expires_at=?,published_at=?,
                outcome_unknown_at=?,dead_at=?,last_error_bytes=NULL,
                last_error_sha256=NULL,updated_at=?
            WHERE event_id=?
            """,
            (
                after.status.value,
                after.state_revision,
                after.attempt_count,
                after.next_attempt_at,
                after.lease_owner,
                after.lease_fence,
                after.lease_expires_at,
                receipt.occurred_at
                if after.status is WorkerExecutionStatus.PUBLISHED
                else None,
                receipt.occurred_at
                if after.status is WorkerExecutionStatus.OUTCOME_UNKNOWN
                else None,
                receipt.occurred_at
                if after.status is WorkerExecutionStatus.DEAD
                else None,
                receipt.occurred_at,
                receipt.target_id,
            ),
        )
        connection.execute(
            """
            UPDATE worker_target_current
            SET status=?,state_revision=?,attempt_count=?,last_attempt_id=?,
                last_attempt_number=?,current_attempt_id=?,
                current_attempt_number=?,fence_high_water=?,
                cancellation_pending=?,next_attempt_at=?,lease_owner=?,
                lease_fence=?,lease_expires_at=?,current_effect_id=?,
                unresolved_effect_id=?,last_receipt_transaction_id=?,
                previous_state_sha256=state_sha256,state_sha256=?,updated_at=?
            WHERE target_id=?
            """,
            (
                after.status.value,
                after.state_revision,
                after.attempt_count,
                after.last_attempt_id,
                after.last_attempt_number,
                after.current_attempt_id,
                after.current_attempt_number,
                after.fence_high_water,
                int(after.cancellation_pending),
                after.next_attempt_at,
                after.lease_owner,
                after.lease_fence,
                after.lease_expires_at,
                None if after.current_effect is None else after.current_effect.effect_id,
                None
                if after.unresolved_effect is None
                else after.unresolved_effect.effect_id,
                parent.transaction_id,
                receipt.after_state_sha256,
                receipt.occurred_at,
                receipt.target_id,
            ),
        )
        fence = execution.request.dispatch_fence
        if fence is not None and fence.before != fence.after:
            old_head = connection.execute(
                "SELECT head_sha256 FROM outbox_dispatch_head WHERE singleton=1"
            ).fetchone()[0]
            published = fence.after.published
            inflight = fence.after.inflight
            connection.execute(
                """
                UPDATE outbox_dispatch_head
                SET state_revision=?,published_sequence=?,
                    published_repository_revision=?,published_ordinal=?,
                    inflight_sequence=?,inflight_repository_revision=?,
                    inflight_ordinal=?,previous_head_sha256=?,head_sha256=?,
                    updated_at=?
                WHERE singleton=1
                """,
                (
                    fence.after.control_revision,
                    published.sequence,
                    None if published.sequence == 0 else published.repository_revision,
                    None if published.sequence == 0 else published.ordinal,
                    None if inflight is None else inflight.sequence,
                    None if inflight is None else inflight.repository_revision,
                    None if inflight is None else inflight.ordinal,
                    old_head,
                    receipt.after_state_sha256,
                    receipt.occurred_at,
                ),
            )
        worker_head = connection.execute(
            "SELECT state_revision,tail_sha256 FROM worker_ledger_head WHERE singleton=1"
        ).fetchone()
        connection.execute(
            """
            UPDATE worker_ledger_head
            SET state_revision=?,sequence=?,previous_tail_sha256=?,
                tail_sha256=?,updated_at=?
            WHERE singleton=1
            """,
            (
                worker_head[0] + 1,
                receipt.sequence,
                worker_head[1],
                receipt.ledger_tail_sha256,
                receipt.occurred_at,
            ),
        )
        connection.commit()
        committed = True
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()
    if committed:
        database._finalize_committed_storage()


def _advance_outbox_to_effectless_dead(baseline, plan) -> None:
    target = plan.targets[0].target
    atomic = plan.atomic_write.atomic_plan
    authority = dataclasses.replace(
        atomic.authority,
        authority_revision=atomic.candidate.authority_revision,
    )
    before = WorkerBeforeState(
        WorkerExecutionStatus.PENDING,
        1,
        0,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        0,
    )
    ledger = WorkerLedgerHead(0, b"\0" * 32)
    order = target.outbox_order
    assert order is not None
    empty = WorkerOutboxDispatchCursor(0, 0, -1)
    available = WorkerOutboxDispatchState(1, empty, None)
    inflight = WorkerOutboxDispatchState(2, empty, order)
    dispatch = available
    previous_attempt_id = None
    committed_at = datetime.strptime(
        plan.evidence.committed_at,
        "%Y-%m-%dT%H:%M:%S.%fZ",
    )

    def later(seconds: int) -> str:
        return (
            (committed_at + timedelta(seconds=seconds)).strftime(
                "%Y-%m-%dT%H:%M:%S.%f"
            )[:-3]
            + "Z"
        )

    for attempt_number in range(1, target.max_attempts + 1):
        started_at = later(attempt_number * 2 - 1)
        expires_at = later(attempt_number * 2)
        attempt_id = "%032x" % (0x200 + attempt_number)
        claim_fence = WorkerOutboxDispatchFence(
            dispatch,
            inflight if attempt_number == 1 else dispatch,
        )
        claim = _issue_worker_execution_plan(
            WorkerClaimRequest(
                WorkerRequestHeader(
                    "%032x" % (0x1000 + attempt_number * 2 - 1),
                    authority,
                    target,
                    before,
                    started_at,
                ),
                WorkerAttemptStart(
                    attempt_id,
                    attempt_number,
                    previous_attempt_id,
                    None if attempt_number == 1 else attempt_number - 1,
                    "worker-a",
                    attempt_number,
                    started_at,
                    expires_at,
                ),
                claim_fence,
            ),
            before_ledger=ledger,
        )
        _persist_worker_execution(baseline.database, claim)
        before = _before_from_after(claim.closure.after_state)
        ledger = claim.after_ledger
        dispatch = claim_fence.after

        final = attempt_number == target.max_attempts
        after_dispatch = (
            WorkerOutboxDispatchState(
                3,
                WorkerOutboxDispatchCursor(
                    order.sequence,
                    order.repository_revision,
                    order.ordinal,
                ),
                None,
            )
            if final
            else dispatch
        )
        expiry_fence = WorkerOutboxDispatchFence(dispatch, after_dispatch)
        expiry = _issue_worker_execution_plan(
            WorkerExpireRequest(
                WorkerRequestHeader(
                    "%032x" % (0x1000 + attempt_number * 2),
                    authority,
                    target,
                    before,
                    expires_at,
                ),
                WorkerLeaseToken(
                    attempt_id,
                    attempt_number,
                    "worker-a",
                    attempt_number,
                    expires_at,
                ),
                expires_at,
                (
                    WorkerExecutionStatus.DEAD
                    if final
                    else WorkerExecutionStatus.RETRY
                ),
                None if final else expires_at,
                expiry_fence,
            ),
            before_ledger=ledger,
        )
        _persist_worker_execution(baseline.database, expiry)
        before = _before_from_after(expiry.closure.after_state)
        ledger = expiry.after_ledger
        dispatch = expiry_fence.after
        previous_attempt_id = attempt_id
    assert before.status is WorkerExecutionStatus.DEAD


def test_real_outbox_vertical_commits_reconciles_and_retries_exact(tmp_path) -> None:
    baseline = create_canonical_mounted_business_database(
        tmp_path / "production.sqlite3"
    )
    plan = _production_plan(baseline)
    writer = SQLiteProductionAtomicWriter(baseline.database)

    committed = writer.commit(plan)
    assert committed.status is ProductionWriteStatus.COMMITTED
    assert committed.repository_revision == 3
    assert not committed.recovered_after_ack_loss
    assert writer.reconcile(plan).status is ProductionWriteStatus.COMMITTED

    duplicate = writer.commit(plan)
    assert duplicate.status is ProductionWriteStatus.COMMITTED
    assert duplicate.recovered_after_ack_loss
    assert _one(
        baseline.database,
        "SELECT count(*) FROM atomic_business_write_receipts WHERE commit_id=?",
        (plan.evidence.commit_id,),
    )[0] == 1
    assert _one(
        baseline.database,
        "SELECT target_count,targets_sha256,production_receipt_sha256 "
        "FROM atomic_business_write_receipts WHERE commit_id=?",
        (plan.evidence.commit_id,),
    ) == (
        1,
        plan.evidence.targets_sha256,
        plan.expected_receipt.production_receipt_sha256,
    )
    assert _one(
        baseline.database,
        "SELECT sequence,tail_sha256 FROM outbox_allocation_head WHERE singleton=1",
    ) == (
        1,
        plan.evidence.allocations[0].allocation_tail_sha256,
    )
    assert _one(
        baseline.database,
        "SELECT status,state_revision,last_receipt_transaction_id,state_sha256 "
        "FROM worker_target_current WHERE target_id=?",
        (plan.targets[0].target.target_id,),
    ) == (
        "pending",
        1,
        plan.expected_receipt.receipt_parent.transaction_id,
        plan.targets[0].genesis_state_sha256,
    )


def test_generic_shape_commits_controls_actions_and_multi_outbox_atomically(
    tmp_path,
) -> None:
    baseline = create_canonical_mounted_business_database(
        tmp_path / "generic-shape.sqlite3"
    )
    plan = _generic_production_plan(baseline)
    writer = SQLiteProductionAtomicWriter(baseline.database)

    result = writer.commit(plan)
    assert result.status is ProductionWriteStatus.COMMITTED
    assert writer.reconcile(plan).status is ProductionWriteStatus.COMMITTED
    duplicate = writer.commit(plan)
    assert duplicate.status is ProductionWriteStatus.COMMITTED
    assert duplicate.recovered_after_ack_loss

    assert _one(
        baseline.database,
        "SELECT count(*) FROM worker_targets WHERE origin_commit_id=?",
        (plan.evidence.commit_id,),
    ) == (5,)
    assert _one(
        baseline.database,
        "SELECT count(*) FROM worker_target_current AS current "
        "JOIN worker_targets AS target ON target.target_id=current.target_id "
        "WHERE target.origin_commit_id=?",
        (plan.evidence.commit_id,),
    ) == (5,)
    assert _one(
        baseline.database,
        "SELECT count(*) FROM pipeline_control_intents WHERE origin_commit_id=?",
        (plan.evidence.commit_id,),
    ) == (2,)
    assert _one(
        baseline.database,
        "SELECT count(*) FROM atomic_business_control_members WHERE commit_id=?",
        (plan.evidence.commit_id,),
    ) == (2,)
    assert _one(
        baseline.database,
        "SELECT state_revision,sequence,previous_tail_sha256,tail_sha256 "
        "FROM worker_ledger_head WHERE singleton=1",
    ) == (
        2,
        plan.atomic_write.evidence.after_worker_ledger.sequence,
        plan.atomic_write.evidence.before_worker_ledger.tail_sha256,
        plan.atomic_write.evidence.after_worker_ledger.tail_sha256,
    )
    assert _one(
        baseline.database,
        "SELECT state_revision,sequence,previous_tail_sha256,tail_sha256 "
        "FROM outbox_allocation_head WHERE singleton=1",
    ) == (
        2,
        2,
        plan.evidence.before_outbox_allocation.tail_sha256,
        plan.evidence.allocations[-1].allocation_tail_sha256,
    )


def test_generic_shape_before_commit_failure_rolls_back_every_group(
    tmp_path,
) -> None:
    baseline = create_canonical_mounted_business_database(
        tmp_path / "generic-rollback.sqlite3"
    )
    plan = _generic_production_plan(baseline)
    writer = SQLiteProductionAtomicWriter(baseline.database)

    def fail(point):
        if point is ProductionAtomicWriteTestPoint.BEFORE_COMMIT:
            raise RuntimeError("rollback")

    result = writer.commit(plan, _test_hook=fail)
    assert result.status is ProductionWriteStatus.NOT_COMMITTED
    for table, column in (
        ("worker_targets", "origin_commit_id"),
        ("outbox_allocations", "commit_id"),
        ("pipeline_control_intents", "origin_commit_id"),
        ("atomic_business_control_members", "commit_id"),
    ):
        assert _one(
            baseline.database,
            "SELECT count(*) FROM " + table + " WHERE " + column + "=?",
            (plan.evidence.commit_id,),
        ) == (0,)
    assert _one(
        baseline.database,
        "SELECT state_revision,sequence FROM worker_ledger_head WHERE singleton=1",
    ) == (1, 0)
    assert _one(
        baseline.database,
        "SELECT state_revision,sequence FROM outbox_allocation_head WHERE singleton=1",
    ) == (1, 0)


@pytest.mark.parametrize(
    "occupied",
    ("generic_parent", "specialised_receipt", "ledger_entry", "member"),
)
def test_generic_first_commit_classifies_orphan_control_identity_as_conflict(
    tmp_path,
    occupied,
) -> None:
    baseline = create_canonical_mounted_business_database(
        tmp_path / ("generic-control-identity-%s.sqlite3" % occupied)
    )
    plan = _generic_production_plan(baseline)
    intent = plan.atomic_write.atomic_plan.pipeline_intents[0]
    control = plan.atomic_write.control_plans[0]
    receipt = control.expected_receipt
    parent = receipt.receipt_parent
    closure = control.closure

    if occupied == "generic_parent":
        triggers = ()
        sql = (
            "INSERT INTO transaction_receipts(transaction_id,kind,committed_at) "
            "VALUES (?,?,?)"
        )
        values = (parent.transaction_id, parent.kind, parent.committed_at)
    elif occupied == "specialised_receipt":
        triggers = ("pipeline_control_receipts_binding_guard",)
        sql = """
            INSERT INTO pipeline_control_receipts(
                intent_id,transaction_id,entry_kind,receipt_kind,committed_at,
                operation,intent_type,target_id,before_status,after_status,
                before_state_revision,after_state_revision,sequence,
                previous_ledger_tail_sha256,ledger_tail_sha256,manifest_bytes,
                request_sha256,after_state_sha256,receipt_sha256
            ) VALUES (?,?,'pipeline_control',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """
        values = (
            intent.intent_id,
            parent.transaction_id,
            parent.kind,
            parent.committed_at,
            control.operation.value,
            control.control_intent.intent_type,
            control.target.target_id,
            None,
            closure.after_status.value,
            closure.before_state_revision,
            closure.after_state_revision,
            receipt.sequence,
            receipt.previous_ledger_tail_sha256,
            receipt.ledger_tail_sha256,
            closure.manifest_bytes,
            receipt.request_sha256,
            receipt.after_state_sha256,
            receipt.receipt_sha256,
        )
    elif occupied == "ledger_entry":
        triggers = ()
        sql = """
            INSERT INTO worker_ledger_entries(
                sequence,entry_kind,transaction_id,receipt_kind,committed_at,
                previous_tail_sha256,ledger_tail_sha256,request_sha256,
                after_state_sha256,receipt_sha256,occurred_at
            ) VALUES (?,'pipeline_control',?,?,?,?,?,?,?,?,?)
        """
        values = (
            receipt.sequence,
            parent.transaction_id,
            parent.kind,
            parent.committed_at,
            receipt.previous_ledger_tail_sha256,
            receipt.ledger_tail_sha256,
            receipt.request_sha256,
            receipt.after_state_sha256,
            receipt.receipt_sha256,
            receipt.occurred_at,
        )
    else:
        triggers = ("atomic_business_control_members_binding_guard",)
        sql = """
            INSERT INTO atomic_business_control_members(
                commit_id,ordinal,intent_id,control_transaction_id,sequence,
                ledger_tail_sha256,binding_sha256
            ) VALUES (?,?,?,?,?,?,?)
        """
        values = (
            plan.evidence.commit_id,
            intent.ordinal,
            intent.intent_id,
            parent.transaction_id,
            receipt.sequence,
            receipt.ledger_tail_sha256,
            b"x" * 32,
        )
    _tamper_then_restore_schema(
        baseline.database,
        triggers,
        sql,
        values,
    )

    result = SQLiteProductionAtomicWriter(baseline.database).commit(plan)
    assert result.status is ProductionWriteStatus.CONFLICT
    assert result.code == "production_pipeline_control_identity_conflict"
    assert _one(
        baseline.database,
        "SELECT count(*) FROM business_commits WHERE commit_id=?",
        (plan.evidence.commit_id,),
    ) == (0,)
    assert _one(
        baseline.database,
        "SELECT count(*) FROM worker_targets WHERE origin_commit_id=?",
        (plan.evidence.commit_id,),
    ) == (0,)
    assert _one(
        baseline.database,
        "SELECT repository_revision FROM repository_runtime_control "
        "WHERE singleton=1",
    ) == (2,)


def test_generic_ack_loss_recovers_every_group_as_exact_committed(tmp_path) -> None:
    baseline = create_canonical_mounted_business_database(
        tmp_path / "generic-ack-loss.sqlite3"
    )
    plan = _generic_production_plan(baseline)
    writer = SQLiteProductionAtomicWriter(baseline.database)

    def lose_ack(point):
        if point is ProductionAtomicWriteTestPoint.AFTER_COMMIT_BEFORE_ACK:
            raise RuntimeError("ack lost")

    result = writer.commit(plan, _test_hook=lose_ack)
    assert result.status is ProductionWriteStatus.COMMITTED
    assert result.recovered_after_ack_loss
    assert writer.reconcile(plan).status is ProductionWriteStatus.COMMITTED
    assert _one(
        baseline.database,
        "SELECT count(*) FROM worker_targets WHERE origin_commit_id=?",
        (plan.evidence.commit_id,),
    ) == (len(plan.targets),)
    assert _one(
        baseline.database,
        "SELECT count(*) FROM outbox_allocations WHERE commit_id=?",
        (plan.evidence.commit_id,),
    ) == (len(plan.evidence.allocations),)
    assert _one(
        baseline.database,
        "SELECT count(*) FROM atomic_business_control_members WHERE commit_id=?",
        (plan.evidence.commit_id,),
    ) == (len(plan.atomic_write.control_plans),)


def test_generic_reconcile_rejects_control_member_tamper_after_schema_restore(
    tmp_path,
) -> None:
    baseline = create_canonical_mounted_business_database(
        tmp_path / "generic-control-tamper.sqlite3"
    )
    plan = _generic_production_plan(baseline)
    writer = SQLiteProductionAtomicWriter(baseline.database)
    assert writer.commit(plan).status is ProductionWriteStatus.COMMITTED

    _tamper_then_restore_schema(
        baseline.database,
        ("atomic_business_control_members_no_update",),
        "UPDATE atomic_business_control_members SET binding_sha256=zeroblob(32) "
        "WHERE commit_id=? AND ordinal=1",
        (plan.evidence.commit_id,),
    )

    result = writer.reconcile(plan)
    assert result.status is ProductionWriteStatus.CORRUPT
    assert result.code == "production_control_corrupt"


def test_generic_reconcile_rejects_same_length_control_receipt_tamper(
    tmp_path,
) -> None:
    baseline = create_canonical_mounted_business_database(
        tmp_path / "generic-control-receipt-tamper.sqlite3"
    )
    plan = _generic_production_plan(baseline)
    writer = SQLiteProductionAtomicWriter(baseline.database)
    assert writer.commit(plan).status is ProductionWriteStatus.COMMITTED
    intent_id = plan.atomic_write.atomic_plan.pipeline_intents[1].intent_id
    original = _one(
        baseline.database,
        "SELECT manifest_bytes FROM pipeline_control_receipts WHERE intent_id=?",
        (intent_id,),
    )[0]
    changed = original.replace(b"{", b"[", 1)
    assert len(changed) == len(original)
    assert changed != original
    _tamper_then_restore_schema(
        baseline.database,
        ("pipeline_control_receipts_no_update",),
        "UPDATE pipeline_control_receipts SET manifest_bytes=? WHERE intent_id=?",
        (changed, intent_id),
    )

    result = writer.reconcile(plan)
    assert result.status is ProductionWriteStatus.CORRUPT
    assert result.code == "production_control_corrupt"


def test_generic_reconcile_rejects_second_allocation_tamper_after_schema_restore(
    tmp_path,
) -> None:
    baseline = create_canonical_mounted_business_database(
        tmp_path / "generic-allocation-tamper.sqlite3"
    )
    plan = _generic_production_plan(baseline)
    writer = SQLiteProductionAtomicWriter(baseline.database)
    assert writer.commit(plan).status is ProductionWriteStatus.COMMITTED

    _tamper_then_restore_schema(
        baseline.database,
        ("outbox_allocations_no_update",),
        "UPDATE outbox_allocations SET allocation_tail_sha256=? WHERE sequence=2",
        (b"z" * 32,),
    )

    result = writer.reconcile(plan)
    assert result.status is ProductionWriteStatus.CORRUPT
    assert result.code == "production_allocation_corrupt"


def test_transition_control_updates_existing_pipeline_without_new_target(
    tmp_path,
) -> None:
    baseline = create_canonical_mounted_business_database(
        tmp_path / "pipeline-transition.sqlite3"
    )
    first = _generic_production_plan(baseline)
    writer = SQLiteProductionAtomicWriter(baseline.database)
    assert writer.commit(first).status is ProductionWriteStatus.COMMITTED
    target_id = next(
        wrapped.target.target_id for wrapped in first.targets
        if wrapped.target.target_kind is WorkerTargetKind.PIPELINE
    )

    transition = _pipeline_transition_plan(baseline, first)
    assert all(
        wrapped.target.target_kind is not WorkerTargetKind.PIPELINE
        for wrapped in transition.targets
    )
    assert writer.commit(transition).status is ProductionWriteStatus.COMMITTED
    assert writer.reconcile(transition).status is ProductionWriteStatus.COMMITTED
    assert _one(
        baseline.database,
        "SELECT count(*) FROM worker_targets WHERE target_id=?",
        (target_id,),
    ) == (1,)
    assert _one(
        baseline.database,
        "SELECT status,state_revision,last_receipt_transaction_id "
        "FROM worker_target_current WHERE target_id=?",
        (target_id,),
    ) == (
        "paused",
        2,
        transition.atomic_write.control_plans[0]
        .expected_receipt.receipt_parent.transaction_id,
    )

    # The earlier CREATE remains valid immutable history, but its mutable
    # pipeline current has advanced beyond the proof language this writer
    # owns.  Fail closed as UNKNOWN, never mislabel legal progress CORRUPT.
    earlier = writer.reconcile(first)
    assert earlier.status is ProductionWriteStatus.UNKNOWN
    assert earlier.code == "production_worker_progress_unsupported"


def test_before_commit_failure_rolls_back_every_old_and_new_row(tmp_path) -> None:
    baseline = create_canonical_mounted_business_database(
        tmp_path / "rollback.sqlite3"
    )
    plan = _production_plan(baseline)
    writer = SQLiteProductionAtomicWriter(baseline.database)

    def fail(point):
        if point is ProductionAtomicWriteTestPoint.BEFORE_COMMIT:
            raise RuntimeError("rollback")

    result = writer.commit(plan, _test_hook=fail)
    assert result.status is ProductionWriteStatus.NOT_COMMITTED
    assert writer.reconcile(plan).status is ProductionWriteStatus.NOT_COMMITTED
    assert _one(
        baseline.database,
        "SELECT repository_revision FROM repository_runtime_control WHERE singleton=1",
    )[0] == 2
    assert _one(
        baseline.database,
        "SELECT count(*) FROM business_commits WHERE commit_id=?",
        (plan.evidence.commit_id,),
    )[0] == 0
    assert _one(
        baseline.database,
        "SELECT count(*) FROM worker_targets WHERE origin_commit_id=?",
        (plan.evidence.commit_id,),
    )[0] == 0


def test_ack_loss_returns_fresh_exact_committed_result(tmp_path) -> None:
    baseline = create_canonical_mounted_business_database(
        tmp_path / "ack-loss.sqlite3"
    )
    plan = _production_plan(baseline)
    writer = SQLiteProductionAtomicWriter(baseline.database)

    def lose_ack(point):
        if point is ProductionAtomicWriteTestPoint.AFTER_COMMIT_BEFORE_ACK:
            raise RuntimeError("ack lost")

    result = writer.commit(plan, _test_hook=lose_ack)
    assert result.status is ProductionWriteStatus.COMMITTED
    assert result.recovered_after_ack_loss
    assert writer.reconcile(plan).status is ProductionWriteStatus.COMMITTED


def test_reconcile_accepts_plan_free_authorize_and_complete_suffix(
    tmp_path,
) -> None:
    # Local imports avoid a test-module import cycle: the worker fixtures use
    # this module's fixed production-plan helper during their own collection.
    from sqlite_worker_claim_writer import (
        SQLiteWorkerClaimStatus,
        SQLiteWorkerClaimWriter,
    )
    from sqlite_worker_projection_writer import (
        SQLiteWorkerProjectionStatus,
        SQLiteWorkerProjectionWriter,
    )
    from test_sqlite_worker_claim_writer import _claim_fixture
    from test_sqlite_worker_projection_writer import (
        _authorize_plan,
        _complete_plan,
    )

    baseline, production, claim = _claim_fixture(
        tmp_path,
        "production-plan-free-complete.sqlite3",
    )
    assert (
        SQLiteWorkerClaimWriter(baseline.database).commit(claim).status
        is SQLiteWorkerClaimStatus.COMMITTED
    )
    authorize, projection = _authorize_plan(baseline, claim)
    worker = SQLiteWorkerProjectionWriter(baseline.database)
    assert (
        worker.commit(authorize, projection).status
        is SQLiteWorkerProjectionStatus.COMMITTED
    )
    writer = SQLiteProductionAtomicWriter(baseline.database)
    assert writer.reconcile(production).status is ProductionWriteStatus.COMMITTED

    complete, projection = _complete_plan(
        baseline,
        authorize,
        status=WorkerExecutionStatus.PUBLISHED,
        request_id="e" * 32,
        with_proof=True,
    )
    assert (
        worker.commit(complete, projection).status
        is SQLiteWorkerProjectionStatus.COMMITTED
    )
    result = writer.reconcile(production)
    assert result.status is ProductionWriteStatus.COMMITTED
    assert result.code == "production_atomic_exact"

    result_payload = _one(
        baseline.database,
        "SELECT projection_manifest_bytes "
        "FROM worker_result_payload_projections "
        "WHERE receipt_transaction_id=?",
        (complete.expected_receipt.receipt_parent.transaction_id,),
    )[0]
    changed_payload = result_payload.replace(b"{", b"[", 1)
    assert len(changed_payload) == len(result_payload)
    assert changed_payload != result_payload
    _tamper_then_restore_schema(
        baseline.database,
        ("worker_result_payload_projections_no_update",),
        "UPDATE worker_result_payload_projections SET projection_manifest_bytes=? "
        "WHERE receipt_transaction_id=?",
        (
            changed_payload,
            complete.expected_receipt.receipt_parent.transaction_id,
        ),
    )
    tampered = writer.reconcile(production)
    assert tampered.status is ProductionWriteStatus.CORRUPT
    assert tampered.code == "production_current_corrupt"


def test_reconcile_accepts_plan_free_outcome_unknown_and_reconcile_suffix(
    tmp_path,
) -> None:
    from sqlite_worker_claim_writer import (
        SQLiteWorkerClaimStatus,
        SQLiteWorkerClaimWriter,
    )
    from sqlite_worker_projection_writer import (
        SQLiteWorkerProjectionStatus,
        SQLiteWorkerProjectionWriter,
    )
    from test_sqlite_worker_claim_writer import _claim_fixture
    from test_sqlite_worker_projection_writer import (
        _authorize_plan,
        _complete_plan,
        _reconcile_plan,
    )

    baseline, production, claim = _claim_fixture(
        tmp_path,
        "production-plan-free-reconcile.sqlite3",
    )
    assert (
        SQLiteWorkerClaimWriter(baseline.database).commit(claim).status
        is SQLiteWorkerClaimStatus.COMMITTED
    )
    authorize, projection = _authorize_plan(baseline, claim)
    worker = SQLiteWorkerProjectionWriter(baseline.database)
    assert (
        worker.commit(authorize, projection).status
        is SQLiteWorkerProjectionStatus.COMMITTED
    )
    unknown, projection = _complete_plan(
        baseline,
        authorize,
        status=WorkerExecutionStatus.OUTCOME_UNKNOWN,
        request_id="e" * 32,
        with_proof=False,
    )
    assert (
        worker.commit(unknown, projection).status
        is SQLiteWorkerProjectionStatus.COMMITTED
    )
    writer = SQLiteProductionAtomicWriter(baseline.database)
    assert writer.reconcile(production).status is ProductionWriteStatus.COMMITTED

    reconciled, projection = _reconcile_plan(baseline, unknown)
    assert (
        worker.commit(reconciled, projection).status
        is SQLiteWorkerProjectionStatus.COMMITTED
    )
    result = writer.reconcile(production)
    assert result.status is ProductionWriteStatus.COMMITTED
    assert result.code == "production_atomic_exact"


def test_plan_free_fresh_head_race_is_unknown_not_corrupt(
    tmp_path,
    monkeypatch,
) -> None:
    from sqlite_worker_claim_writer import (
        SQLiteWorkerClaimStatus,
        SQLiteWorkerClaimWriter,
    )
    from sqlite_worker_durable_audit import (
        SQLiteWorkerDurableAuditStatus,
        SQLiteWorkerDurableAuditor,
    )
    from sqlite_worker_projection_writer import (
        SQLiteWorkerProjectionStatus,
        SQLiteWorkerProjectionWriter,
    )
    from test_sqlite_worker_claim_writer import _claim_fixture
    from test_sqlite_worker_projection_writer import _authorize_plan

    baseline, production, claim = _claim_fixture(
        tmp_path,
        "production-plan-free-head-race.sqlite3",
    )
    assert (
        SQLiteWorkerClaimWriter(baseline.database).commit(claim).status
        is SQLiteWorkerClaimStatus.COMMITTED
    )
    authorize, projection = _authorize_plan(baseline, claim)
    assert (
        SQLiteWorkerProjectionWriter(baseline.database)
        .commit(authorize, projection).status
        is SQLiteWorkerProjectionStatus.COMMITTED
    )
    original = SQLiteWorkerDurableAuditor.inspect

    def shifted_head(self, request_id, *, expected=None):
        result = original(self, request_id, expected=expected)
        assert result.status is SQLiteWorkerDurableAuditStatus.EXACT
        assert result.ledger_sequence is not None
        return dataclasses.replace(
            result,
            ledger_sequence=result.ledger_sequence + 1,
        )

    monkeypatch.setattr(SQLiteWorkerDurableAuditor, "inspect", shifted_head)
    result = SQLiteProductionAtomicWriter(baseline.database).reconcile(production)
    assert result.status is ProductionWriteStatus.UNKNOWN
    assert result.code == "production_worker_progress_unsupported"


def test_same_commit_identity_with_different_manifest_is_conflict(tmp_path) -> None:
    baseline = create_canonical_mounted_business_database(
        tmp_path / "conflict.sqlite3"
    )
    planning = _read_production_atomic_planning_snapshot(baseline.database)
    exact = _production_plan(baseline, planning=planning)
    conflicting = _production_plan(
        baseline,
        topic="different_topic",
        planning=planning,
    )
    writer = SQLiteProductionAtomicWriter(baseline.database)
    assert writer.commit(exact).status is ProductionWriteStatus.COMMITTED
    result = writer.commit(conflicting)
    assert result.status is ProductionWriteStatus.CONFLICT
    assert not result.recovered_after_ack_loss


def test_stale_authority_and_allocation_snapshot_is_conflict(tmp_path) -> None:
    baseline = create_canonical_mounted_business_database(
        tmp_path / "stale.sqlite3"
    )
    planning = _read_production_atomic_planning_snapshot(baseline.database)
    first = _production_plan(baseline, planning=planning, suffix="2")
    stale = _production_plan(baseline, planning=planning, suffix="3")
    writer = SQLiteProductionAtomicWriter(baseline.database)
    assert writer.commit(first).status is ProductionWriteStatus.COMMITTED
    result = writer.commit(stale)
    assert result.status is ProductionWriteStatus.CONFLICT
    assert _one(
        baseline.database,
        "SELECT count(*) FROM business_commits WHERE commit_id=?",
        (stale.evidence.commit_id,),
    )[0] == 0


def test_future_worker_progress_after_planning_cannot_retreat_commit_time(
    tmp_path,
) -> None:
    baseline = create_canonical_mounted_business_database(
        tmp_path / "future-worker-progress.sqlite3"
    )
    first = _production_plan(baseline, suffix="2")
    writer = SQLiteProductionAtomicWriter(baseline.database)
    assert writer.commit(first).status is ProductionWriteStatus.COMMITTED

    profile = inspect_business_runtime(
        baseline.database,
        access=BusinessRuntimeAccess.WRITER,
    )
    planning = _read_production_atomic_planning_snapshot(baseline.database)
    stale = _production_plan(
        baseline,
        planning=planning,
        suffix="3",
        before=_business_next_before(first.atomic_write.atomic_plan),
        authority=profile.to_authority_binding(),
        version=3,
    )
    _advance_outbox_to_effectless_dead(baseline, first)

    worker_time = _one(
        baseline.database,
        "SELECT updated_at FROM worker_ledger_head WHERE singleton=1",
    )[0]
    dispatch_time = _one(
        baseline.database,
        "SELECT updated_at FROM outbox_dispatch_head WHERE singleton=1",
    )[0]
    assert worker_time > stale.evidence.committed_at
    assert dispatch_time > stale.evidence.committed_at
    fresh = _read_production_atomic_planning_snapshot(baseline.database)
    assert fresh.worker_ledger_head.evidence.updated_at == worker_time
    assert fresh.outbox_dispatch_head.evidence.updated_at == dispatch_time
    assert fresh.committed_at >= max(worker_time, dispatch_time)

    connection, _filesystem = baseline.database._open_unvalidated()
    try:
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(
            RuntimeError,
            match="^production_committed_time_stale$",
        ):
            writer._require_committed_time(connection, stale)
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()

    result = writer.commit(stale)
    assert result.status is ProductionWriteStatus.CONFLICT
    assert result.code == "production_worker_ledger_head_conflict"
    assert _one(
        baseline.database,
        "SELECT count(*) FROM business_commits WHERE commit_id=?",
        (stale.evidence.commit_id,),
    ) == (0,)


def test_reconcile_rejects_unreceipted_worker_current_progress_after_schema_restore(
    tmp_path,
) -> None:
    baseline = create_canonical_mounted_business_database(
        tmp_path / "worker-current-tamper.sqlite3"
    )
    plan = _production_plan(baseline)
    writer = SQLiteProductionAtomicWriter(baseline.database)
    assert writer.commit(plan).status is ProductionWriteStatus.COMMITTED

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
        (b"x" * 32, plan.targets[0].target.target_id),
    )

    # Schema fingerprint is healthy again.  COMMITTED must still be withheld:
    # rev2 has no specialised receipt, ledger member, attempt, or event mirror.
    result = writer.reconcile(plan)
    assert result.status is ProductionWriteStatus.CORRUPT
    assert result.code == "production_current_corrupt"


def test_reconcile_rejects_forged_allocation_head_after_schema_restore(
    tmp_path,
) -> None:
    baseline = create_canonical_mounted_business_database(
        tmp_path / "allocation-head-tamper.sqlite3"
    )
    plan = _production_plan(baseline)
    writer = SQLiteProductionAtomicWriter(baseline.database)
    assert writer.commit(plan).status is ProductionWriteStatus.COMMITTED

    _tamper_then_restore_schema(
        baseline.database,
        (
            "outbox_allocation_head_cas_update_guard",
            "outbox_allocation_head_sequence_guard",
        ),
        """
        UPDATE outbox_allocation_head
        SET state_revision=state_revision+1,
            previous_tail_sha256=?
        WHERE singleton=1
        """,
        (b"y" * 32,),
    )

    # The row still points at the real final allocation and fooled the old
    # contains-check.  The one-outbox-per-commit head chain proves revision,
    # predecessor tail, final tail and time together.
    result = writer.reconcile(plan)
    assert result.status is ProductionWriteStatus.CORRUPT
    assert result.code == "production_allocation_head_corrupt"


def test_reconcile_accepts_exact_effectless_exhausted_expiry_history(
    tmp_path,
) -> None:
    baseline = create_canonical_mounted_business_database(
        tmp_path / "effectless-dead.sqlite3"
    )
    plan = _production_plan(baseline)
    writer = SQLiteProductionAtomicWriter(baseline.database)
    assert writer.commit(plan).status is ProductionWriteStatus.COMMITTED

    _advance_outbox_to_effectless_dead(baseline, plan)

    assert _one(
        baseline.database,
        "SELECT status,state_revision,attempt_count FROM worker_target_current "
        "WHERE target_id=?",
        (plan.targets[0].target.target_id,),
    ) == ("dead", 21, plan.targets[0].target.max_attempts)
    assert _one(
        baseline.database,
        "SELECT count(*) FROM worker_results WHERE target_id=?",
        (plan.targets[0].target.target_id,),
    ) == (0,)
    assert writer.reconcile(plan).status is ProductionWriteStatus.COMMITTED


def test_reconcile_rejects_dispatch_head_rollback_after_schema_restore(
    tmp_path,
) -> None:
    baseline = create_canonical_mounted_business_database(
        tmp_path / "dispatch-head-rollback.sqlite3"
    )
    plan = _production_plan(baseline)
    writer = SQLiteProductionAtomicWriter(baseline.database)
    assert writer.commit(plan).status is ProductionWriteStatus.COMMITTED
    _advance_outbox_to_effectless_dead(baseline, plan)
    assert writer.reconcile(plan).status is ProductionWriteStatus.COMMITTED

    _tamper_then_restore_schema(
        baseline.database,
        (
            "outbox_dispatch_head_cas_update_guard",
            "outbox_dispatch_head_progress_guard",
        ),
        """
        UPDATE outbox_dispatch_head
        SET state_revision=1,published_sequence=0,
            published_repository_revision=NULL,published_ordinal=NULL,
            inflight_sequence=NULL,inflight_repository_revision=NULL,
            inflight_ordinal=NULL,previous_head_sha256=zeroblob(32),
            head_sha256=zeroblob(32)
        WHERE singleton=1
        """,
    )

    result = writer.reconcile(plan)
    assert result.status is ProductionWriteStatus.CORRUPT
    assert result.code == "production_dispatch_head_corrupt"


def test_noncanonical_mount_binding_cannot_authorize_production(tmp_path) -> None:
    baseline = create_canonical_mounted_business_database(
        tmp_path / "noncanonical-binding.sqlite3"
    )
    _tamper_then_restore_schema(
        baseline.database,
        ("business_cutover_mount_bindings_no_update",),
        "UPDATE business_cutover_mount_bindings SET "
        "binding_manifest_bytes=CAST(binding_manifest_bytes || X'00' AS BLOB)",
    )
    with pytest.raises(SQLiteProductionAuthorityError):
        _read_production_atomic_planning_snapshot(baseline.database)


def test_reconcile_revalidates_full_strict_mount_after_schema_restore(
    tmp_path,
) -> None:
    baseline = create_canonical_mounted_business_database(
        tmp_path / "reconcile-mount-tamper.sqlite3"
    )
    plan = _production_plan(baseline)
    writer = SQLiteProductionAtomicWriter(baseline.database)
    assert writer.commit(plan).status is ProductionWriteStatus.COMMITTED

    _tamper_then_restore_schema(
        baseline.database,
        ("business_cutover_install_receipts_no_update",),
        "UPDATE business_cutover_install_receipts "
        "SET member_mapping_sha256=zeroblob(32)",
    )

    with pytest.raises(SQLiteProductionAuthorityError):
        _read_production_atomic_planning_snapshot(baseline.database)
    result = writer.reconcile(plan)
    assert result.status is ProductionWriteStatus.CORRUPT
    assert result.code == "production_mount_authorization_corrupt"


def test_fresh_reconcile_distinguishes_corrupt_and_unavailable(tmp_path) -> None:
    corrupt = create_canonical_mounted_business_database(
        tmp_path / "corrupt.sqlite3"
    )
    corrupt_plan = _production_plan(corrupt)
    corrupt_writer = SQLiteProductionAtomicWriter(corrupt.database)
    assert corrupt_writer.commit(corrupt_plan).status is ProductionWriteStatus.COMMITTED
    raw = sqlite3.connect(corrupt.database.path)
    try:
        raw.execute("DROP TRIGGER worker_target_current_no_delete")
        raw.commit()
    finally:
        raw.close()
    assert corrupt_writer.reconcile(corrupt_plan).status is (
        ProductionWriteStatus.CORRUPT
    )

    unavailable = create_canonical_mounted_business_database(
        tmp_path / "unavailable.sqlite3"
    )
    unavailable_plan = _production_plan(unavailable)
    unavailable_writer = SQLiteProductionAtomicWriter(unavailable.database)
    unavailable.database.path.unlink()
    assert unavailable_writer.reconcile(unavailable_plan).status is (
        ProductionWriteStatus.UNKNOWN
    )


def test_authorizer_denies_unowned_writes_and_ddl() -> None:
    authorizer = _ProductionWriterAuthorizer()
    for table in (
        "worker_targets",
        "atomic_business_control_members",
        "worker_ledger_entries",
        "worker_ledger_head",
        "pipeline_control_intents",
        "pipeline_control_receipts",
    ):
        assert authorizer(
            sqlite3.SQLITE_INSERT,
            table,
            None,
            "main",
            None,
        ) == sqlite3.SQLITE_OK
    assert authorizer(
        sqlite3.SQLITE_UPDATE,
        "schema_migrations",
        None,
        "main",
        None,
    ) == sqlite3.SQLITE_DENY
    assert authorizer(
        sqlite3.SQLITE_CREATE_TABLE,
        "escape",
        None,
        "main",
        None,
    ) == sqlite3.SQLITE_DENY
    assert authorizer.denied


__all__ = ()
