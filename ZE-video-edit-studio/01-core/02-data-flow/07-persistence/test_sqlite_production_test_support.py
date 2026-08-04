"""Closed multi-group plan evidence for the production test fixture."""

from __future__ import annotations

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

from business_transaction_contract import (  # noqa: E402
    ActionReplayPolicy,
    DurableActionIntent,
    IdempotencyPolicy,
    OutboxEventIntent,
    PipelineIntent,
    PipelineIntentKind,
    PipelineReplayPolicy,
    freeze_json_payload,
)
from business_transaction_integrity import _issue_atomic_commit_plan  # noqa: E402
from business_cutover_head_integrity import (  # noqa: E402
    issue_business_cutover_head_chain_from_mount,
)
from production_atomic_write_integrity import (  # noqa: E402
    _require_production_atomic_write_plan,
)
import sqlite_business_test_support as business_support  # noqa: E402
import sqlite_production_authority as production_authority  # noqa: E402
import sqlite_production_test_support as production_support  # noqa: E402
from sqlite_production_authority import (  # noqa: E402
    _read_production_atomic_planning_snapshot,
)
from sqlite_production_test_support import (  # noqa: E402
    create_canonical_mounted_business_database,
    issue_mounted_production_atomic_write_plan,
)
from test_sqlite_business_transaction import (  # noqa: E402
    _operation as _business_operation,
    _plan as _business_plan,
)
from worker_execution_contract import (  # noqa: E402
    WorkerExecutionStatus,
    WorkerTargetKind,
)


_UPGRADE_MUTATED_TABLES = (
    "business_cutover_install_receipts",
    "business_asset_coverage_receipts",
    "repository_audit_certificates",
    "repository_audit_head",
    "business_cutover_activation_receipts",
    "business_cutover_mount_receipts",
    "business_cutover_head",
    "atomic_business_write_receipts",
)


def _raw_active_fixture(path):
    return business_support.create_active_business_database(path)


def _upgrade_fixture(baseline) -> None:
    profile = baseline.profile
    production_support._upgrade_to_canonical_mount(
        baseline,
        repository_epoch=profile.repository_epoch,
        cutover_id=profile.cutover_id,
        source_revision=profile.source_admission_revision,
        runtime_generation=profile.runtime_generation,
        code_digest=profile.code_digest,
    )


def _upgrade_state(database):
    connection = sqlite3.connect(database.path)
    try:
        tables = tuple(
            (
                table,
                tuple(
                    connection.execute(
                        'SELECT * FROM "' + table + '" ORDER BY rowid'
                    ).fetchall()
                ),
            )
            for table in _UPGRADE_MUTATED_TABLES
        )
        triggers = tuple(connection.execute(
            "SELECT name,sql FROM sqlite_master "
            "WHERE type='trigger' ORDER BY name"
        ).fetchall())
        return tables, triggers
    finally:
        connection.close()


def _trigger_state(database):
    return _upgrade_state(database)[1]


def _multi_group_atomic_plan(baseline):
    action = DurableActionIntent(
        ordinal=0,
        action_id="a" * 32,
        action_type="notify.push",
        idempotency_key="notify-request-1",
        payload=freeze_json_payload(
            {"channel": "review"},
            schema="ze.action.notify.v1",
        ),
        replay_policy=ActionReplayPolicy.RECONCILE_BEFORE_RETRY,
        idempotency_policy=IdempotencyPolicy.DURABLE_RECEIPT_RECONCILE,
        idempotency_scope="provider/account-1",
    )
    pipelines = (
        PipelineIntent(
            ordinal=0,
            intent_id="b" * 32,
            job_id="b" * 32,
            pipeline_id="render-main",
            intent_type="create",
            idempotency_key="render-create-1",
            payload=freeze_json_payload(
                {"name": "render-main"},
                schema="ze.pipeline.create.v1",
            ),
            intent_kind=PipelineIntentKind.CREATE,
        ),
        PipelineIntent(
            ordinal=1,
            intent_id="d" * 32,
            job_id="d" * 32,
            pipeline_id="render-preview",
            intent_type="create",
            idempotency_key="render-create-2",
            payload=freeze_json_payload(
                {"name": "render-preview"},
                schema="ze.pipeline.create.v1",
            ),
            intent_kind=PipelineIntentKind.CREATE,
            replay_policy=PipelineReplayPolicy.NEVER_REPLAY,
            max_attempts=1,
        ),
    )
    seed = _business_plan(
        baseline,
        actions=(action,),
        pipelines=pipelines,
    )
    second_operation = _business_operation(baseline, "4")
    second_event = OutboxEventIntent(
        ordinal=1,
        event_id="c" * 32,
        operation_id=second_operation.operation_id,
        topic="document_change_secondary",
        partition_key=baseline.project_id,
        payload=freeze_json_payload(
            {"operation_id": second_operation.operation_id, "copy": 2},
            schema="ze.document-change.v1",
        ),
    )
    return _issue_atomic_commit_plan(
        authority=seed.authority,
        commit_id=seed.commit_id,
        trace_id=seed.trace_id,
        before=seed.before,
        after_snapshot=seed.after_snapshot,
        operations=seed.operations + (second_operation,),
        outbox=seed.outbox + (second_event,),
        durable_actions=seed.durable_actions,
        pipeline_intents=seed.pipeline_intents,
        inbox=seed.inbox,
        confirmation=seed.confirmation,
    )


def test_fixture_persists_exact_canonical_phase_five_head(tmp_path) -> None:
    baseline = create_canonical_mounted_business_database(
        tmp_path / "canonical-head.sqlite3"
    )
    connection = sqlite3.connect(baseline.database.path)
    connection.row_factory = sqlite3.Row
    try:
        mount_row = production_authority._mounted_row(connection)
        assert mount_row is not None
        mount_evidence = production_authority._mount_evidence_from_row(mount_row)
        expected = issue_business_cutover_head_chain_from_mount(
            mount_evidence
        )[-1].row_values()
        actual = connection.execute(
            "SELECT * FROM business_cutover_head WHERE singleton=1"
        ).fetchone()
        columns = tuple(
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(business_cutover_head)"
            ).fetchall()
        )
    finally:
        connection.close()

    assert actual is not None
    assert len(columns) == 22
    assert set(expected) == set(columns)
    assert {name: actual[name] for name in columns} == expected


def test_upgrade_restores_every_trigger_with_identical_sql(tmp_path) -> None:
    baseline = _raw_active_fixture(tmp_path / "trigger-restore.sqlite3")
    before = _trigger_state(baseline.database)

    _upgrade_fixture(baseline)

    assert _trigger_state(baseline.database) == before


def test_current_v13_upgrade_does_not_reissue_or_rewrite_the_head_chain(
    tmp_path,
    monkeypatch,
) -> None:
    baseline = _raw_active_fixture(tmp_path / "v13-no-head-rewrite.sqlite3")
    before = _upgrade_state(baseline.database)

    def forbidden_reissue(_mount_evidence):
        raise AssertionError("current V13 canonical head must not be reissued")

    monkeypatch.setattr(
        production_support,
        "issue_business_cutover_head_chain_from_mount",
        forbidden_reissue,
    )

    _upgrade_fixture(baseline)

    assert _upgrade_state(baseline.database) == before


def test_current_v13_upgrade_never_discovers_or_drops_triggers(
    tmp_path,
    monkeypatch,
) -> None:
    baseline = _raw_active_fixture(tmp_path / "v13-no-trigger-drop.sqlite3")
    before = _upgrade_state(baseline.database)

    def forbidden_trigger_discovery(_connection, _names):
        raise AssertionError("current V13 canonical fixture must not inspect triggers")

    monkeypatch.setattr(
        production_support,
        "_trigger_sql",
        forbidden_trigger_discovery,
    )

    _upgrade_fixture(baseline)

    assert _upgrade_state(baseline.database) == before


def test_fixture_issues_closed_pipeline_action_multi_outbox_plan(tmp_path) -> None:
    baseline = create_canonical_mounted_business_database(
        tmp_path / "business.sqlite3"
    )
    atomic = _multi_group_atomic_plan(baseline)
    planning = _read_production_atomic_planning_snapshot(baseline.database)

    plan = issue_mounted_production_atomic_write_plan(
        baseline.database,
        atomic,
        planning=planning,
    )

    assert _require_production_atomic_write_plan(plan) is plan
    controls = plan.atomic_write.control_plans
    initial_ledger = planning.worker_ledger_head.evidence.ledger_head
    assert len(controls) == 2
    assert controls[0].before_ledger == initial_ledger
    assert controls[1].before_ledger == controls[0].after_ledger
    assert plan.atomic_write.evidence.before_worker_ledger == initial_ledger
    assert plan.atomic_write.evidence.after_worker_ledger == controls[-1].after_ledger
    assert plan.atomic_write.evidence.after_worker_ledger.sequence == (
        initial_ledger.sequence + 2
    )

    assert tuple(target.target.target_kind for target in plan.targets) == (
        WorkerTargetKind.PIPELINE,
        WorkerTargetKind.PIPELINE,
        WorkerTargetKind.ACTION,
        WorkerTargetKind.OUTBOX,
        WorkerTargetKind.OUTBOX,
    )
    assert tuple(target.source_ordinal for target in plan.targets) == (0, 1, 0, 0, 1)
    assert tuple(target.genesis_status for target in plan.targets) == (
        WorkerExecutionStatus.QUEUED,
        WorkerExecutionStatus.QUEUED,
        WorkerExecutionStatus.QUEUED,
        WorkerExecutionStatus.PENDING,
        WorkerExecutionStatus.PENDING,
    )
    assert plan.targets[0].genesis_state_sha256 == controls[0].closure.after_state_sha256
    assert plan.targets[1].genesis_state_sha256 == controls[1].closure.after_state_sha256

    before_allocation = planning.outbox_allocation_head
    assert tuple(value.sequence for value in plan.evidence.allocations) == (
        before_allocation.sequence + 1,
        before_allocation.sequence + 2,
    )
    assert tuple(value.ordinal for value in plan.evidence.allocations) == (0, 1)
    assert plan.evidence.allocations[0].previous_tail_sha256 == (
        before_allocation.tail_sha256
    )
    assert plan.evidence.allocations[1].previous_tail_sha256 == (
        plan.evidence.allocations[0].allocation_tail_sha256
    )
    assert plan.evidence.after_outbox_allocation.sequence == (
        before_allocation.sequence + 2
    )
    assert plan.evidence.after_outbox_allocation.tail_sha256 == (
        plan.evidence.allocations[-1].allocation_tail_sha256
    )
    assert tuple(
        target.target.outbox_order.sequence for target in plan.targets[-2:]
    ) == (
        before_allocation.sequence + 1,
        before_allocation.sequence + 2,
    )
