"""Real SQLite tests for the narrow OUTBOX V9 projection writer."""

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

from business_transaction_contract import freeze_json_payload
from sqlite_kernel import SQLiteKernelError
from sqlite_worker_claim_writer import (
    SQLiteWorkerClaimStatus,
    SQLiteWorkerClaimWriter,
)
import sqlite_worker_projection_writer as projection_writer_module
from sqlite_worker_projection_writer import (
    SQLiteWorkerProjectionError,
    SQLiteWorkerProjectionStatus,
    SQLiteWorkerProjectionTestPoint,
    SQLiteWorkerProjectionWriter,
    _WorkerProjectionAuthorizer,
)
from test_sqlite_worker_claim_writer import (
    _claim_fixture,
    _later,
    _one,
    _tamper_ignoring_checks,
)
from test_sqlite_production_writer import _persist_worker_execution
from worker_execution_contract import (
    WorkerAttemptStart,
    WorkerAuthorizeEffectRequest,
    WorkerBeforeState,
    WorkerClaimRequest,
    WorkerCompleteRequest,
    WorkerEffectAuthorization,
    WorkerEffectKind,
    WorkerEffectLink,
    WorkerExecutionStatus,
    WorkerExpireRequest,
    WorkerLeaseToken,
    WorkerOutboxDispatchFence,
    WorkerOutboxDispatchState,
    WorkerReconcileProof,
    WorkerReconcileProofKind,
    WorkerReconcileRequest,
    WorkerRequestHeader,
    WorkerTerminalProof,
    WorkerTerminalProofKind,
)
from worker_execution_integrity import (
    WorkerExecutionIntegrityError,
    _issue_worker_execution_plan,
)
from worker_result_projection_integrity import (
    _issue_worker_result_projection_plan,
)


def _database_now(database) -> str:
    row = _one(database, "SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')")
    assert row is not None and isinstance(row[0], str)
    return row[0]


def _before(after) -> WorkerBeforeState:
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


def _lease(before: WorkerBeforeState) -> WorkerLeaseToken:
    return WorkerLeaseToken(
        before.current_attempt_id,
        before.current_attempt_number,
        before.lease_owner,
        before.lease_fence,
        before.lease_expires_at,
    )


def _claimed(tmp_path, name: str):
    baseline, _production, claim = _claim_fixture(tmp_path, name)
    assert (
        SQLiteWorkerClaimWriter(baseline.database).commit(claim).status
        is SQLiteWorkerClaimStatus.COMMITTED
    )
    return baseline, claim


def _dispatch_state(baseline) -> WorkerOutboxDispatchState:
    connection, _filesystem = baseline.database._open_unvalidated()
    try:
        row = connection.execute(
            "SELECT state_revision,published_sequence,"
            "published_repository_revision,published_ordinal,"
            "inflight_sequence,inflight_repository_revision,inflight_ordinal "
            "FROM outbox_dispatch_head WHERE singleton=1"
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    from worker_execution_contract import (
        WorkerOutboxDispatchCursor,
        WorkerOutboxOrder,
    )
    published = (
        WorkerOutboxDispatchCursor(0, 0, -1)
        if row[1] == 0
        else WorkerOutboxDispatchCursor(row[1], row[2], row[3])
    )
    inflight = (
        None
        if row[4] is None
        else WorkerOutboxOrder(row[4], row[5], row[6])
    )
    return WorkerOutboxDispatchState(row[0], published, inflight)


def _terminal_dispatch_after(
    before: WorkerOutboxDispatchState,
) -> WorkerOutboxDispatchState:
    from worker_execution_contract import WorkerOutboxDispatchCursor
    assert before.inflight is not None
    return WorkerOutboxDispatchState(
        before.control_revision + 1,
        WorkerOutboxDispatchCursor(
            before.inflight.sequence,
            before.inflight.repository_revision,
            before.inflight.ordinal,
        ),
        None,
    )


def _authorize_plan(baseline, claim, *, request_id="c" * 32):
    before = _before(claim.closure.after_state)
    occurred_at = _database_now(baseline.database)
    request = WorkerAuthorizeEffectRequest(
        WorkerRequestHeader(
            request_id,
            claim.authority,
            claim.target,
            before,
            occurred_at,
        ),
        _lease(before),
        WorkerEffectAuthorization(
            "d" * 32,
            WorkerEffectKind.OUTBOX_PUBLISH,
            claim.target.idempotency_scope,
            claim.target.idempotency_key,
            freeze_json_payload(
                {"publish": claim.target.target_id},
                schema="test.outbox-publish-request.v1",
            ),
            occurred_at,
        ),
    )
    plan = _issue_worker_execution_plan(
        request,
        before_ledger=claim.after_ledger,
    )
    return plan, _issue_worker_result_projection_plan(plan)


def _effect_link(authorize, *, receipt="provider-ok") -> WorkerEffectLink:
    effect = authorize.closure.after_state.current_effect
    assert effect is not None
    return WorkerEffectLink(
        effect.attempt_id,
        effect.attempt_number,
        effect.effect_id,
        effect.effect_kind,
        effect.request_sha256,
        provider_receipt=freeze_json_payload(
            {"receipt": receipt},
            schema="test.provider-receipt.v1",
        ),
    )


def _complete_plan(
    baseline,
    authorize,
    *,
    status: WorkerExecutionStatus,
    request_id: str,
    with_proof: bool,
    error=None,
):
    before = _before(authorize.closure.after_state)
    occurred_at = _database_now(baseline.database)
    assert authorize.request.header.target.outbox_order is not None
    # The claim left this order inflight.  Non-terminal completion keeps the
    # fence unchanged; PUBLISHED advances the exact allocation cursor.
    connection, _filesystem = baseline.database._open_unvalidated()
    try:
        row = connection.execute(
            "SELECT state_revision,published_sequence,"
            "published_repository_revision,published_ordinal,"
            "inflight_sequence,inflight_repository_revision,inflight_ordinal "
            "FROM outbox_dispatch_head WHERE singleton=1"
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    from worker_execution_contract import (
        WorkerOutboxDispatchCursor,
        WorkerOutboxOrder,
    )
    published = (
        WorkerOutboxDispatchCursor(0, 0, -1)
        if row[1] == 0
        else WorkerOutboxDispatchCursor(row[1], row[2], row[3])
    )
    inflight = WorkerOutboxOrder(row[4], row[5], row[6])
    dispatch = WorkerOutboxDispatchState(row[0], published, inflight)
    dispatch_after = (
        WorkerOutboxDispatchState(
            dispatch.control_revision + 1,
            WorkerOutboxDispatchCursor(
                inflight.sequence,
                inflight.repository_revision,
                inflight.ordinal,
            ),
            None,
        )
        if status is WorkerExecutionStatus.PUBLISHED
        else dispatch
    )
    request = WorkerCompleteRequest(
        WorkerRequestHeader(
            request_id,
            authorize.authority,
            authorize.target,
            before,
            occurred_at,
        ),
        _lease(before),
        status,
        occurred_at,
        error=error,
        effect_link=_effect_link(authorize) if with_proof else None,
        dispatch_fence=WorkerOutboxDispatchFence(dispatch, dispatch_after),
    )
    plan = _issue_worker_execution_plan(
        request,
        before_ledger=authorize.after_ledger,
    )
    return plan, _issue_worker_result_projection_plan(plan)


def _retry_complete_after_claim(baseline, claim, *, request_id="2" * 32):
    before = _before(claim.closure.after_state)
    occurred_at = _database_now(baseline.database)
    dispatch = _dispatch_state(baseline)
    error = freeze_json_payload(
        {"error": "retryable"},
        schema="test.outbox-error.v1",
    )
    request = WorkerCompleteRequest(
        WorkerRequestHeader(
            request_id,
            claim.authority,
            claim.target,
            before,
            occurred_at,
        ),
        _lease(before),
        WorkerExecutionStatus.RETRY,
        occurred_at,
        error=error,
        next_attempt_at=occurred_at,
        dispatch_fence=WorkerOutboxDispatchFence(dispatch, dispatch),
    )
    plan = _issue_worker_execution_plan(
        request,
        before_ledger=claim.after_ledger,
    )
    return plan, _issue_worker_result_projection_plan(plan)


def _dead_complete_after_authorize(
    baseline,
    authorize,
    *,
    request_id="3" * 32,
):
    before = _before(authorize.closure.after_state)
    effect = before.current_effect
    assert effect is not None
    occurred_at = _database_now(baseline.database)
    dispatch = _dispatch_state(baseline)
    terminal = WorkerTerminalProof(
        WorkerTerminalProofKind.TERMINAL_FAILURE,
        effect,
        freeze_json_payload(
            {"terminal": "provider-failure"},
            schema="test.terminal-proof.v1",
        ),
    )
    error = freeze_json_payload(
        {"error": "terminal"},
        schema="test.outbox-error.v1",
    )
    request = WorkerCompleteRequest(
        WorkerRequestHeader(
            request_id,
            authorize.authority,
            authorize.target,
            before,
            occurred_at,
        ),
        _lease(before),
        WorkerExecutionStatus.DEAD,
        occurred_at,
        error=error,
        terminal_proof=terminal,
        dispatch_fence=WorkerOutboxDispatchFence(
            dispatch,
            _terminal_dispatch_after(dispatch),
        ),
    )
    plan = _issue_worker_execution_plan(
        request,
        before_ledger=authorize.after_ledger,
    )
    return plan, _issue_worker_result_projection_plan(plan)


def _reconcile_plan(baseline, unknown, *, request_id="f" * 32):
    before = _before(unknown.closure.after_state)
    effect = before.unresolved_effect
    assert effect is not None
    occurred_at = _database_now(baseline.database)
    connection, _filesystem = baseline.database._open_unvalidated()
    try:
        row = connection.execute(
            "SELECT state_revision,published_sequence,"
            "published_repository_revision,published_ordinal,"
            "inflight_sequence,inflight_repository_revision,inflight_ordinal "
            "FROM outbox_dispatch_head WHERE singleton=1"
        ).fetchone()
    finally:
        connection.close()
    from worker_execution_contract import (
        WorkerOutboxDispatchCursor,
        WorkerOutboxOrder,
    )
    published = (
        WorkerOutboxDispatchCursor(0, 0, -1)
        if row[1] == 0
        else WorkerOutboxDispatchCursor(row[1], row[2], row[3])
    )
    inflight = WorkerOutboxOrder(row[4], row[5], row[6])
    dispatch = WorkerOutboxDispatchState(row[0], published, inflight)
    after_dispatch = WorkerOutboxDispatchState(
        dispatch.control_revision + 1,
        WorkerOutboxDispatchCursor(
            inflight.sequence,
            inflight.repository_revision,
            inflight.ordinal,
        ),
        None,
    )
    link = WorkerEffectLink(
        effect.attempt_id,
        effect.attempt_number,
        effect.effect_id,
        effect.effect_kind,
        effect.request_sha256,
        provider_receipt=freeze_json_payload(
            {"receipt": "reconciled"},
            schema="test.provider-receipt.v1",
        ),
    )
    proof = WorkerReconcileProof(
        WorkerReconcileProofKind.PROVIDER_RECEIPT,
        effect,
        freeze_json_payload(
            {"observed": "published"},
            schema="test.reconcile-evidence.v1",
        ),
        effect_link=link,
    )
    request = WorkerReconcileRequest(
        WorkerRequestHeader(
            request_id,
            unknown.authority,
            unknown.target,
            before,
            occurred_at,
        ),
        effect.attempt_id,
        effect.attempt_number,
        before.fence_high_water,
        WorkerExecutionStatus.PUBLISHED,
        proof,
        occurred_at,
        dispatch_fence=WorkerOutboxDispatchFence(dispatch, after_dispatch),
    )
    plan = _issue_worker_execution_plan(
        request,
        before_ledger=unknown.after_ledger,
    )
    return plan, _issue_worker_result_projection_plan(plan)


def _reconcile_without_effect_link(
    baseline,
    unknown,
    *,
    status: WorkerExecutionStatus,
    proof_kind: WorkerReconcileProofKind,
    request_id: str,
):
    before = _before(unknown.closure.after_state)
    effect = before.unresolved_effect
    assert effect is not None
    occurred_at = _database_now(baseline.database)
    dispatch = _dispatch_state(baseline)
    dispatch_after = (
        _terminal_dispatch_after(dispatch)
        if status is WorkerExecutionStatus.DEAD
        else dispatch
    )
    proof = WorkerReconcileProof(
        proof_kind,
        effect,
        freeze_json_payload(
            {"proof": proof_kind.value},
            schema="test.reconcile-evidence.v1",
        ),
    )
    request = WorkerReconcileRequest(
        WorkerRequestHeader(
            request_id,
            unknown.authority,
            unknown.target,
            before,
            occurred_at,
        ),
        effect.attempt_id,
        effect.attempt_number,
        before.fence_high_water,
        status,
        proof,
        occurred_at,
        next_attempt_at=(
            occurred_at if status is WorkerExecutionStatus.RETRY else None
        ),
        dispatch_fence=WorkerOutboxDispatchFence(dispatch, dispatch_after),
    )
    plan = _issue_worker_execution_plan(
        request,
        before_ledger=unknown.after_ledger,
    )
    return plan, _issue_worker_result_projection_plan(plan)


def _commit_authorize(tmp_path, name="projection-authorize.sqlite3"):
    baseline, claim = _claimed(tmp_path, name)
    plan, projection = _authorize_plan(baseline, claim)
    writer = SQLiteWorkerProjectionWriter(baseline.database)
    assert writer.commit(plan, projection).status is SQLiteWorkerProjectionStatus.COMMITTED
    return baseline, claim, plan, projection, writer


def test_outbox_authorize_writes_v8_mirror_v9_sidecar_and_head_last(tmp_path) -> None:
    baseline, claim, plan, projection, writer = _commit_authorize(tmp_path)
    effect = projection.closure.effect_request
    assert effect is not None
    assert writer.reconcile(plan, projection).status is SQLiteWorkerProjectionStatus.COMMITTED
    assert _one(
        baseline.database,
        "SELECT effect_id,current_effect_id,state_revision FROM worker_effects "
        "JOIN worker_target_current USING(target_id) WHERE effect_id=?",
        (effect.binding.effect.effect_id,),
    ) == (effect.binding.effect.effect_id, effect.binding.effect.effect_id, 3)
    assert _one(
        baseline.database,
        "SELECT request_payload_bytes,projection_sha256,closure_manifest_sha256 "
        "FROM worker_effect_request_projections WHERE effect_id=?",
        (effect.binding.effect.effect_id,),
    ) == (
        effect.request_payload.payload_bytes,
        effect.projection_sha256,
        projection.closure.manifest_sha256,
    )
    assert _one(
        baseline.database,
        "SELECT sequence,tail_sha256 FROM worker_ledger_head WHERE singleton=1",
    ) == (plan.after_ledger.sequence, plan.after_ledger.tail_sha256)
    assert claim.after_ledger == plan.before_ledger


def test_old_claim_reconciles_through_supported_durable_ledger_suffix(
    tmp_path,
) -> None:
    baseline, claim, _plan, _projection, _writer = _commit_authorize(
        tmp_path,
        "claim-supported-suffix.sqlite3",
    )

    result = SQLiteWorkerClaimWriter(baseline.database).reconcile(claim)

    assert result.status is SQLiteWorkerClaimStatus.COMMITTED
    assert result.code == "worker_claim_exact_via_durable_audit"


def test_outbox_complete_with_and_without_proof_use_exact_presence_matrix(tmp_path) -> None:
    baseline, _claim, authorize, _projection, writer = _commit_authorize(
        tmp_path,
        "projection-complete-proof.sqlite3",
    )
    complete, projected = _complete_plan(
        baseline,
        authorize,
        status=WorkerExecutionStatus.PUBLISHED,
        request_id="e" * 32,
        with_proof=True,
    )
    assert writer.commit(complete, projected).status is SQLiteWorkerProjectionStatus.COMMITTED
    assert projected.closure.proof is not None
    assert _one(
        baseline.database,
        "SELECT count(*),proof_sha256 FROM worker_proof_payload_projections "
        "WHERE receipt_transaction_id=?",
        (complete.expected_receipt.receipt_parent.transaction_id,),
    ) == (1, projected.closure.proof.proof_sha256)
    assert _one(
        baseline.database,
        "SELECT status,published_at FROM event_outbox WHERE event_id=?",
        (complete.target.target_id,),
    ) == ("published", complete.expected_receipt.occurred_at)

    baseline, _claim, authorize, _projection, writer = _commit_authorize(
        tmp_path,
        "projection-complete-no-proof.sqlite3",
    )
    unknown, projected = _complete_plan(
        baseline,
        authorize,
        status=WorkerExecutionStatus.OUTCOME_UNKNOWN,
        request_id="e" * 32,
        with_proof=False,
    )
    assert writer.commit(unknown, projected).status is SQLiteWorkerProjectionStatus.COMMITTED
    assert projected.closure.proof is None
    assert _one(
        baseline.database,
        "SELECT proof_sha256 FROM worker_result_payload_projections "
        "WHERE receipt_transaction_id=?",
        (unknown.expected_receipt.receipt_parent.transaction_id,),
    ) == (None,)
    assert _one(
        baseline.database,
        "SELECT count(*) FROM worker_proof_payload_projections "
        "WHERE receipt_transaction_id=?",
        (unknown.expected_receipt.receipt_parent.transaction_id,),
    ) == (0,)


def test_outbox_effectless_complete_retry_persists_error_and_next_attempt(tmp_path) -> None:
    baseline, claim = _claimed(tmp_path, "projection-complete-retry.sqlite3")
    complete, projected = _retry_complete_after_claim(baseline, claim)
    writer = SQLiteWorkerProjectionWriter(baseline.database)

    assert writer.commit(complete, projected).status is SQLiteWorkerProjectionStatus.COMMITTED
    assert projected.closure.proof is None
    error = complete.request.error
    assert error is not None
    assert _one(
        baseline.database,
        "SELECT status,state_revision,available_at,last_error_bytes,"
        "last_error_sha256,lease_owner FROM event_outbox WHERE event_id=?",
        (complete.target.target_id,),
    ) == (
        "retry",
        complete.closure.after_state_revision,
        complete.request.next_attempt_at,
        error.payload_bytes,
        error.payload_sha256,
        None,
    )
    assert _one(
        baseline.database,
        "SELECT status,next_attempt_at,current_effect_id,unresolved_effect_id "
        "FROM worker_target_current WHERE target_id=?",
        (complete.target.target_id,),
    ) == ("retry", complete.request.next_attempt_at, None, None)
    dispatch = _dispatch_state(baseline)
    assert dispatch == complete.request.dispatch_fence.after
    assert _one(
        baseline.database,
        "SELECT count(*) FROM worker_proof_payload_projections "
        "WHERE receipt_transaction_id=?",
        (complete.expected_receipt.receipt_parent.transaction_id,),
    ) == (0,)


def test_outbox_effectless_complete_dead_is_rejected_by_pure_contract(tmp_path) -> None:
    baseline, claim = _claimed(tmp_path, "projection-effectless-dead.sqlite3")
    before = _before(claim.closure.after_state)
    occurred_at = _database_now(baseline.database)
    dispatch = _dispatch_state(baseline)
    request = WorkerCompleteRequest(
        WorkerRequestHeader(
            "4" * 32,
            claim.authority,
            claim.target,
            before,
            occurred_at,
        ),
        _lease(before),
        WorkerExecutionStatus.DEAD,
        occurred_at,
        dispatch_fence=WorkerOutboxDispatchFence(
            dispatch,
            _terminal_dispatch_after(dispatch),
        ),
    )
    with pytest.raises(WorkerExecutionIntegrityError) as rejected:
        _issue_worker_execution_plan(request, before_ledger=claim.after_ledger)
    assert rejected.value.code == "worker_execution_terminal_proof_required"
    assert _one(
        baseline.database,
        "SELECT count(*) FROM worker_results",
    ) == (0,)


def test_outbox_effectful_complete_dead_requires_terminal_proof_projection(tmp_path) -> None:
    baseline, _claim, authorize, _projection, writer = _commit_authorize(
        tmp_path,
        "projection-complete-dead.sqlite3",
    )
    complete, projected = _dead_complete_after_authorize(baseline, authorize)

    assert writer.commit(complete, projected).status is SQLiteWorkerProjectionStatus.COMMITTED
    proof = projected.closure.proof
    assert proof is not None
    assert proof.proof_kind == WorkerTerminalProofKind.TERMINAL_FAILURE.value
    assert _one(
        baseline.database,
        "SELECT status,dead_at,last_error_bytes FROM event_outbox WHERE event_id=?",
        (complete.target.target_id,),
    ) == (
        "dead",
        complete.expected_receipt.occurred_at,
        complete.request.error.payload_bytes,
    )
    assert _one(
        baseline.database,
        "SELECT status,current_effect_id,unresolved_effect_id "
        "FROM worker_target_current WHERE target_id=?",
        (complete.target.target_id,),
    ) == ("dead", None, None)
    assert _dispatch_state(baseline) == complete.request.dispatch_fence.after
    assert _one(
        baseline.database,
        "SELECT proof_kind,evidence_payload_bytes FROM "
        "worker_proof_payload_projections WHERE proof_id=?",
        (proof.proof_id,),
    ) == (proof.proof_kind, proof.evidence_payload.payload_bytes)


def test_outbox_reconcile_writes_original_evidence_and_v8_manifest_mirror(tmp_path) -> None:
    baseline, _claim, authorize, _projection, writer = _commit_authorize(
        tmp_path,
        "projection-reconcile.sqlite3",
    )
    diagnostic = freeze_json_payload(
        {"error": "provider-outcome-unknown"},
        schema="test.outbox-error.v1",
    )
    unknown, unknown_projection = _complete_plan(
        baseline,
        authorize,
        status=WorkerExecutionStatus.OUTCOME_UNKNOWN,
        request_id="e" * 32,
        with_proof=False,
        error=diagnostic,
    )
    assert writer.commit(unknown, unknown_projection).status is SQLiteWorkerProjectionStatus.COMMITTED
    reconcile, projected = _reconcile_plan(baseline, unknown)
    assert writer.commit(reconcile, projected).status is SQLiteWorkerProjectionStatus.COMMITTED
    proof = projected.closure.proof
    assert proof is not None
    assert _one(
        baseline.database,
        "SELECT proof_bytes,proof_sha256 FROM worker_reconcile_proofs "
        "WHERE receipt_transaction_id=?",
        (reconcile.expected_receipt.receipt_parent.transaction_id,),
    ) == (proof.projection_manifest_bytes, proof.proof_sha256)
    assert _one(
        baseline.database,
        "SELECT evidence_payload_bytes,provider_receipt_payload_bytes "
        "FROM worker_proof_payload_projections WHERE proof_id=?",
        (proof.proof_id,),
    ) == (
        proof.evidence_payload.payload_bytes,
        proof.provider_receipt_payload.payload_bytes,
    )
    assert _one(
        baseline.database,
        "SELECT last_error_bytes,last_error_sha256 FROM event_outbox "
        "WHERE event_id=?",
        (reconcile.target.target_id,),
    ) == (diagnostic.payload_bytes, diagnostic.payload_sha256)


@pytest.mark.parametrize(
    "proof_kind",
    (
        WorkerReconcileProofKind.PROVIDER_ABSENCE,
        WorkerReconcileProofKind.PROVEN_NOT_EXECUTED,
    ),
)
def test_outbox_reconcile_retry_keeps_dispatch_and_projects_absence_proof(
    tmp_path,
    proof_kind,
) -> None:
    baseline, _claim, authorize, _projection, writer = _commit_authorize(
        tmp_path,
        "projection-reconcile-retry-%s.sqlite3" % proof_kind.value,
    )
    unknown, unknown_projection = _complete_plan(
        baseline,
        authorize,
        status=WorkerExecutionStatus.OUTCOME_UNKNOWN,
        request_id="e" * 32,
        with_proof=False,
    )
    assert writer.commit(unknown, unknown_projection).status is SQLiteWorkerProjectionStatus.COMMITTED
    before_dispatch = _dispatch_state(baseline)
    reconcile, projected = _reconcile_without_effect_link(
        baseline,
        unknown,
        status=WorkerExecutionStatus.RETRY,
        proof_kind=proof_kind,
        request_id="5" * 32,
    )
    assert writer.commit(reconcile, projected).status is SQLiteWorkerProjectionStatus.COMMITTED
    proof = projected.closure.proof
    assert proof is not None
    assert _one(
        baseline.database,
        "SELECT status,available_at,lease_owner FROM event_outbox WHERE event_id=?",
        (reconcile.target.target_id,),
    ) == ("retry", reconcile.request.next_attempt_at, None)
    assert _one(
        baseline.database,
        "SELECT status,next_attempt_at,unresolved_effect_id "
        "FROM worker_target_current WHERE target_id=?",
        (reconcile.target.target_id,),
    ) == ("retry", reconcile.request.next_attempt_at, None)
    assert _dispatch_state(baseline) == before_dispatch
    assert _one(
        baseline.database,
        "SELECT proof_kind,result_id FROM worker_reconcile_proofs "
        "WHERE proof_id=?",
        (proof.proof_id,),
    ) == (proof_kind.value, None)


def test_outbox_reconcile_dead_advances_dispatch_with_provider_absence(tmp_path) -> None:
    baseline, _claim, authorize, _projection, writer = _commit_authorize(
        tmp_path,
        "projection-reconcile-dead.sqlite3",
    )
    unknown, unknown_projection = _complete_plan(
        baseline,
        authorize,
        status=WorkerExecutionStatus.OUTCOME_UNKNOWN,
        request_id="e" * 32,
        with_proof=False,
    )
    assert writer.commit(unknown, unknown_projection).status is SQLiteWorkerProjectionStatus.COMMITTED
    reconcile, projected = _reconcile_without_effect_link(
        baseline,
        unknown,
        status=WorkerExecutionStatus.DEAD,
        proof_kind=WorkerReconcileProofKind.PROVIDER_ABSENCE,
        request_id="6" * 32,
    )
    assert writer.commit(reconcile, projected).status is SQLiteWorkerProjectionStatus.COMMITTED
    proof = projected.closure.proof
    assert proof is not None
    assert _one(
        baseline.database,
        "SELECT status,dead_at FROM event_outbox WHERE event_id=?",
        (reconcile.target.target_id,),
    ) == ("dead", reconcile.expected_receipt.occurred_at)
    assert _one(
        baseline.database,
        "SELECT status,unresolved_effect_id FROM worker_target_current "
        "WHERE target_id=?",
        (reconcile.target.target_id,),
    ) == ("dead", None)
    assert _dispatch_state(baseline) == reconcile.request.dispatch_fence.after
    assert _one(
        baseline.database,
        "SELECT proof_kind,evidence_payload_bytes,provider_receipt_payload_bytes "
        "FROM worker_proof_payload_projections WHERE proof_id=?",
        (proof.proof_id,),
    ) == (
        WorkerReconcileProofKind.PROVIDER_ABSENCE.value,
        proof.evidence_payload.payload_bytes,
        None,
    )


def test_projection_writer_rejects_wrong_pair_and_authorizer_is_column_exact(tmp_path) -> None:
    baseline, claim = _claimed(tmp_path, "projection-wrong-plan.sqlite3")
    plan, projected = _authorize_plan(baseline, claim)
    other, _other_projection = _authorize_plan(
        baseline,
        claim,
        request_id="9" * 32,
    )
    writer = SQLiteWorkerProjectionWriter(baseline.database)
    with pytest.raises(SQLiteWorkerProjectionError) as rejected:
        writer.commit(other, projected)
    assert rejected.value.code == "worker_projection_vertical_unsupported"

    authorizer = _WorkerProjectionAuthorizer()
    assert authorizer(
        sqlite3.SQLITE_INSERT,
        "worker_effect_request_projections",
        None,
        "main",
        None,
    ) == sqlite3.SQLITE_OK
    assert authorizer(
        sqlite3.SQLITE_UPDATE,
        "worker_target_current",
        "current_effect_id",
        "main",
        None,
    ) == sqlite3.SQLITE_OK
    assert authorizer(
        sqlite3.SQLITE_UPDATE,
        "worker_target_current",
        "target_id",
        "main",
        None,
    ) == sqlite3.SQLITE_DENY
    assert authorizer(
        sqlite3.SQLITE_INSERT,
        "business_commits",
        None,
        "main",
        None,
    ) == sqlite3.SQLITE_DENY
    assert plan.operation.value == "authorize_effect"


def test_projection_writer_classifies_durable_prefix_tamper_as_corrupt(
    tmp_path,
) -> None:
    baseline, claim = _claimed(tmp_path, "projection-prefix-tamper.sqlite3")
    plan, projected = _authorize_plan(baseline, claim)
    _tamper_ignoring_checks(
        baseline.database,
        (
            "worker_target_current_update_binding_guard",
            "worker_target_current_cas_update_guard",
        ),
        "UPDATE worker_target_current SET state_revision=state_revision+1 "
        "WHERE target_id=?",
        (claim.target.target_id,),
    )

    result = SQLiteWorkerProjectionWriter(baseline.database).commit(
        plan,
        projected,
    )

    assert result.status is SQLiteWorkerProjectionStatus.CORRUPT
    assert result.code == "worker_projection_prefix_audit_corrupt"


def test_projection_writer_does_not_mask_authorizer_failure_as_retryable(
    tmp_path,
    monkeypatch,
) -> None:
    baseline, claim = _claimed(tmp_path, "projection-authorizer-failure.sqlite3")
    plan, projected = _authorize_plan(baseline, claim)

    def forbidden_write(connection, *_args):
        connection.execute("DELETE FROM event_outbox")

    monkeypatch.setattr(
        SQLiteWorkerProjectionWriter,
        "_write",
        staticmethod(forbidden_write),
    )
    result = SQLiteWorkerProjectionWriter(baseline.database).commit(
        plan,
        projected,
    )

    assert result.status is SQLiteWorkerProjectionStatus.CORRUPT
    assert result.code == "worker_projection_authorizer_denied"


def test_projection_writer_rejects_secondary_effect_identity_collision(
    tmp_path,
) -> None:
    baseline, first_claim, first_authorize, _projection, writer = (
        _commit_authorize(tmp_path, "projection-effect-collision.sqlite3")
    )
    unknown, unknown_projection = _complete_plan(
        baseline,
        first_authorize,
        status=WorkerExecutionStatus.OUTCOME_UNKNOWN,
        request_id="e" * 32,
        with_proof=False,
    )
    assert (
        writer.commit(unknown, unknown_projection).status
        is SQLiteWorkerProjectionStatus.COMMITTED
    )
    retry, retry_projection = _reconcile_without_effect_link(
        baseline,
        unknown,
        status=WorkerExecutionStatus.RETRY,
        proof_kind=WorkerReconcileProofKind.PROVIDER_ABSENCE,
        request_id="5" * 32,
    )
    assert (
        writer.commit(retry, retry_projection).status
        is SQLiteWorkerProjectionStatus.COMMITTED
    )
    before = _before(retry.closure.after_state)
    occurred_at = _database_now(baseline.database)
    dispatch = _dispatch_state(baseline)
    second_claim = _issue_worker_execution_plan(
        WorkerClaimRequest(
            WorkerRequestHeader(
                "7" * 32,
                retry.authority,
                retry.target,
                before,
                occurred_at,
            ),
            WorkerAttemptStart(
                "4" * 32,
                2,
                first_claim.request.attempt.attempt_id,
                first_claim.request.attempt.attempt_number,
                "worker-b",
                before.fence_high_water + 1,
                occurred_at,
                _later(occurred_at, 60),
            ),
            WorkerOutboxDispatchFence(dispatch, dispatch),
        ),
        before_ledger=retry.after_ledger,
    )
    _persist_worker_execution(baseline.database, second_claim)
    second_authorize, projected = _authorize_plan(
        baseline,
        second_claim,
        request_id="8" * 32,
    )

    result = writer.commit(second_authorize, projected)

    assert result.status is SQLiteWorkerProjectionStatus.CONFLICT
    assert result.code == "worker_projection_secondary_identity_conflict"


@pytest.mark.parametrize(
    ("code", "status", "result_code"),
    (
        (
            "sqlite_business_runtime_unreadable",
            SQLiteWorkerProjectionStatus.UNKNOWN,
            "worker_projection_runtime_unavailable",
        ),
        (
            "sqlite_schema_fingerprint_mismatch",
            SQLiteWorkerProjectionStatus.CORRUPT,
            "worker_projection_runtime_corrupt",
        ),
    ),
)
@pytest.mark.parametrize("entrypoint", ("commit", "reconcile"))
def test_projection_commit_and_reconcile_classify_kernel_failure(
    tmp_path,
    monkeypatch,
    code,
    status,
    result_code,
    entrypoint,
) -> None:
    baseline, claim = _claimed(tmp_path, "projection-kernel-%s.sqlite3" % code)
    plan, projected = _authorize_plan(baseline, claim)

    def fail_runtime(*_args, **_kwargs):
        raise SQLiteKernelError(code, "injected kernel failure")

    monkeypatch.setattr(
        projection_writer_module,
        "_validate_business_runtime_connection",
        fail_runtime,
    )
    writer = SQLiteWorkerProjectionWriter(baseline.database)
    result = getattr(writer, entrypoint)(plan, projected)

    assert result.status is status
    assert result.code == result_code


def test_projection_writer_rolls_back_exact_retries_and_recovers_ack_loss(tmp_path) -> None:
    baseline, claim = _claimed(tmp_path, "projection-outcomes.sqlite3")
    plan, projected = _authorize_plan(baseline, claim)
    writer = SQLiteWorkerProjectionWriter(baseline.database)

    def fail_before(point):
        if point is SQLiteWorkerProjectionTestPoint.BEFORE_COMMIT:
            raise RuntimeError("rollback")

    result = writer.commit(plan, projected, _test_hook=fail_before)
    assert result.status is SQLiteWorkerProjectionStatus.NOT_COMMITTED
    assert _one(
        baseline.database,
        "SELECT count(*) FROM worker_effect_request_projections",
    ) == (0,)
    assert writer.commit(plan, projected).status is SQLiteWorkerProjectionStatus.COMMITTED
    retry = writer.commit(plan, projected)
    assert retry.status is SQLiteWorkerProjectionStatus.COMMITTED
    assert retry.recovered_after_ack_loss

    baseline, claim = _claimed(tmp_path, "projection-ack.sqlite3")
    plan, projected = _authorize_plan(baseline, claim)
    writer = SQLiteWorkerProjectionWriter(baseline.database)

    def lose_ack(point):
        if point is SQLiteWorkerProjectionTestPoint.AFTER_COMMIT_BEFORE_ACK:
            raise RuntimeError("ack lost")

    recovered = writer.commit(plan, projected, _test_hook=lose_ack)
    assert recovered.status is SQLiteWorkerProjectionStatus.COMMITTED
    assert recovered.recovered_after_ack_loss


@pytest.mark.parametrize("mode", ("missing", "tampered"))
def test_fresh_reconcile_rejects_missing_or_tampered_sidecar(tmp_path, mode) -> None:
    baseline, _claim, plan, projected, writer = _commit_authorize(
        tmp_path,
        "projection-sidecar-%s.sqlite3" % mode,
    )
    effect = projected.closure.effect_request
    assert effect is not None
    if mode == "missing":
        _tamper_ignoring_checks(
            baseline.database,
            ("worker_effect_request_projections_no_delete",),
            "DELETE FROM worker_effect_request_projections WHERE effect_id=?",
            (effect.binding.effect.effect_id,),
        )
    else:
        _tamper_ignoring_checks(
            baseline.database,
            ("worker_effect_request_projections_no_update",),
            "UPDATE worker_effect_request_projections "
            "SET projection_manifest_bytes=? WHERE effect_id=?",
            (b"{}", effect.binding.effect.effect_id),
        )
    result = writer.reconcile(plan, projected)
    assert result.status is SQLiteWorkerProjectionStatus.CORRUPT


def test_fresh_reconcile_rejects_an_extra_proof_for_no_proof_complete(tmp_path) -> None:
    baseline, _claim, authorize, _projection, writer = _commit_authorize(
        tmp_path,
        "projection-extra-proof.sqlite3",
    )
    complete, projected = _complete_plan(
        baseline,
        authorize,
        status=WorkerExecutionStatus.OUTCOME_UNKNOWN,
        request_id="e" * 32,
        with_proof=False,
    )
    assert writer.commit(complete, projected).status is SQLiteWorkerProjectionStatus.COMMITTED
    result = projected.closure.result
    assert result is not None
    parent = complete.expected_receipt.receipt_parent.transaction_id
    _tamper_ignoring_checks(
        baseline.database,
        (
            "worker_proof_payload_projection_preadvance_guard",
            "worker_proof_payload_projections_binding_guard",
        ),
        """
        INSERT INTO worker_proof_payload_projections(
            proof_id,result_id,target_id,target_kind,attempt_id,attempt_number,
            effect_id,receipt_transaction_id,operation,protocol,
            projection_schema,proof_class,proof_kind,observed_at,
            provider_receipt_schema,provider_receipt_codec_id,
            provider_receipt_codec_version,provider_receipt_payload_bytes,
            provider_receipt_payload_sha256,projection_manifest_bytes,
            proof_sha256,closure_manifest_bytes,closure_manifest_sha256
        ) VALUES (?,?,?,'outbox',?,1,?,?,'complete',
            'ze.worker-result-projection.v1',
            'ze.worker-proof-payload-projection.v1','complete',
            'provider_receipt',?,'bogus.v1','ze.deterministic-json',1,
            X'7B7D',?, X'7B7D',?, X'7B7D',?)
        """,
        (
            "7" * 32,
            result.result_id,
            complete.target.target_id,
            projected.binding.attempt_id,
            projected.binding.effect.effect_id,
            parent,
            complete.expected_receipt.occurred_at,
            b"8" * 32,
            b"9" * 32,
            b"a" * 32,
        ),
    )
    assert writer.reconcile(complete, projected).status is SQLiteWorkerProjectionStatus.CORRUPT


def test_expire_cannot_enter_projection_writer(tmp_path) -> None:
    baseline, claim = _claimed(tmp_path, "projection-expire-rejected.sqlite3")
    plan, projected = _authorize_plan(baseline, claim)
    assert SQLiteWorkerProjectionWriter(baseline.database).commit(
        plan,
        projected,
    ).status is SQLiteWorkerProjectionStatus.COMMITTED
    before = _before(plan.closure.after_state)
    dispatch = plan.request.header.target.outbox_order
    assert dispatch is not None
    connection, _filesystem = baseline.database._open_unvalidated()
    try:
        row = connection.execute(
            "SELECT state_revision,published_sequence,"
            "published_repository_revision,published_ordinal,"
            "inflight_sequence,inflight_repository_revision,inflight_ordinal "
            "FROM outbox_dispatch_head WHERE singleton=1"
        ).fetchone()
    finally:
        connection.close()
    from worker_execution_contract import (
        WorkerOutboxDispatchCursor,
        WorkerOutboxOrder,
    )
    published = (
        WorkerOutboxDispatchCursor(0, 0, -1)
        if row[1] == 0
        else WorkerOutboxDispatchCursor(row[1], row[2], row[3])
    )
    durable_dispatch = WorkerOutboxDispatchState(
        row[0],
        published,
        WorkerOutboxOrder(row[4], row[5], row[6]),
    )
    expire_request = WorkerExpireRequest(
        WorkerRequestHeader(
            "1" * 32,
            plan.authority,
            plan.target,
            before,
            before.lease_expires_at,
        ),
        _lease(before),
        before.lease_expires_at,
        WorkerExecutionStatus.OUTCOME_UNKNOWN,
        dispatch_fence=WorkerOutboxDispatchFence(
            durable_dispatch,
            durable_dispatch,
        ),
    )
    expire = _issue_worker_execution_plan(
        expire_request,
        before_ledger=plan.after_ledger,
    )
    with pytest.raises(SQLiteWorkerProjectionError) as rejected:
        SQLiteWorkerProjectionWriter(baseline.database).commit(expire, projected)
    assert rejected.value.code == "worker_projection_vertical_unsupported"


def test_production_module_never_imports_test_code() -> None:
    source = Path(projection_writer_module.__file__).read_text(encoding="utf-8")
    assert "import test_" not in source
    assert "from test_" not in source
