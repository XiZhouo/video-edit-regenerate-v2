"""Audit-only evidence for pure worker projection and the first SQLite claim.

This module deliberately contains no pytest dependency and is not a production
adapter.  ``GATE-COMMIT-10`` uses it to execute the sealed projection matrix
and the real schema-v8 fixed-SQL OUTBOX claim without making either private
capability reachable from server/bootstrap code.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import time
from typing import Tuple

from business_atomic_write_integrity import _issue_atomic_business_write_plan
from business_transaction_contract import AuthorityBinding, freeze_json_payload
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
from sqlite_business_audit_probe import _plan as _business_plan
from sqlite_business_runtime import BusinessRuntimeAccess, inspect_business_runtime
from sqlite_production_authority import _read_production_atomic_planning_snapshot
from sqlite_production_test_support import create_canonical_mounted_business_database
from sqlite_production_writer import SQLiteProductionAtomicWriter
from sqlite_worker_claim_writer import (
    SQLiteWorkerClaimStatus,
    SQLiteWorkerClaimTestPoint,
    SQLiteWorkerClaimWriter,
)
from sqlite_worker_durable_audit import (
    SQLiteWorkerDurableAuditor,
    WorkerDurableIdentity,
)
from sqlite_worker_projection_writer import (
    SQLiteWorkerProjectionTestPoint,
    SQLiteWorkerProjectionWriter,
)
from worker_execution_contract import (
    WorkerAttemptStart,
    WorkerAuthorizeEffectRequest,
    WorkerBeforeState,
    WorkerClaimRequest,
    WorkerCompleteRequest,
    WorkerEffectAuthorization,
    WorkerEffectBinding,
    WorkerEffectKind,
    WorkerEffectLink,
    WorkerExecutionStatus,
    WorkerIdempotencyPolicy,
    WorkerLeaseToken,
    WorkerOutboxDispatchCursor,
    WorkerOutboxDispatchFence,
    WorkerOutboxDispatchState,
    WorkerOutboxOrder,
    WorkerReconcileProof,
    WorkerReconcileProofKind,
    WorkerReconcileRequest,
    WorkerReplayPolicy,
    WorkerRequestHeader,
    WorkerTarget,
    WorkerTargetKind,
)
from worker_execution_integrity import _issue_worker_execution_plan
from worker_result_projection_contract import (
    EMPTY_WORKER_PROOF_PROJECTION_SHA256,
    WorkerProofProjectionClass,
)
from worker_result_projection_integrity import (
    WorkerResultProjectionIntegrityError,
    _issue_worker_result_projection_plan,
    _require_worker_result_projection_plan,
    _v8_worker_result_mirror_proof_sha256,
)


_T1 = "2026-07-12T00:00:10.000Z"
_T2 = "2026-07-12T00:00:20.000Z"
_T3 = "2026-07-12T00:00:30.000Z"
_ATTEMPT_ID = "a" * 32
_EFFECT_ID = "f" * 32


@dataclass(frozen=True)
class WorkerProjectionEvidence:
    authorize_shape: Tuple[int, int, int]
    complete_shape: Tuple[int, int, int]
    reconcile_shape: Tuple[int, int, int]
    empty_proof_sentinel: str
    complete_proof_kind: str
    reconcile_proof_kind: str
    frozen_vectors: Tuple[str, ...]
    payload_ownership_exact: bool
    tamper_rejected: bool


@dataclass(frozen=True)
class WorkerClaimEvidence:
    committed_status: str
    reconciled_status: str
    retry_status: str
    retry_recovered: bool
    committed_rows: Tuple[int, ...]
    rollback_status: str
    rollback_reconcile: str
    rollback_rows: Tuple[int, ...]
    ack_status: str
    ack_recovered: bool
    ack_reconcile: str
    barrier_status: str
    barrier_code: str
    barrier_fresh_reconcile: str
    crossing_hook_reached: bool
    crossing_expiry_status: str
    crossing_expiry_code: str
    crossing_expiry_rows: Tuple[int, ...]
    identity_conflict_status: str
    identity_conflict_code: str
    identity_conflict_rows: Tuple[int, ...]


@dataclass(frozen=True)
class WorkerProjectionRuntimeEvidence:
    claim_statuses: Tuple[str, str]
    authorize_statuses: Tuple[str, str]
    complete_statuses: Tuple[str, str]
    reconcile_statuses: Tuple[str, str]
    claim_supported_suffix_status: str
    claim_supported_suffix_code: str
    auditor_identity_statuses: Tuple[str, str, str, str]
    auditor_absent_status: str
    v8_projection_rows: Tuple[int, int, int]
    v9_sidecar_rows: Tuple[int, int, int]
    durable_closure_rows: Tuple[int, int, int, int, int]
    rollback_status: str
    rollback_reconcile_status: str
    rollback_rows: Tuple[int, int, int, int, int, int, int, int]
    ack_status: str
    ack_reconcile_status: str
    ack_recovered: bool
    barrier_status: str
    barrier_code: str
    barrier_reconcile_status: str
    barrier_fresh_audit_status: str


def _authority() -> AuthorityBinding:
    return AuthorityBinding(
        "sqlite",
        "active",
        "e" * 64,
        7,
        11,
        13,
        "c" * 64,
        "runtime-1",
        b"d" * 32,
    )


def _action_target() -> WorkerTarget:
    return WorkerTarget(
        target_kind=WorkerTargetKind.ACTION,
        target_id="2" * 32,
        project_id="project-1",
        generation=2,
        document_id="document-1",
        origin_commit_id="b" * 32,
        payload=freeze_json_payload(
            {"kind": "action", "input": "exact"},
            schema="audit.worker-target.v1",
        ),
        replay_policy=WorkerReplayPolicy.PROVIDER_IDEMPOTENT,
        idempotency_policy=WorkerIdempotencyPolicy.REQUIRED_EXTERNAL_KEY,
        idempotency_scope="provider/account-1",
        idempotency_key="external-key-1",
        max_attempts=3,
    )


def _effect_payload():
    return freeze_json_payload(
        {"external": "request"},
        schema="audit.effect-request.v1",
    )


def _effect_binding() -> WorkerEffectBinding:
    return WorkerEffectBinding(
        _ATTEMPT_ID,
        1,
        _EFFECT_ID,
        WorkerEffectKind.ACTION_PROVIDER,
        _effect_payload().payload_sha256,
        "provider/account-1",
        "external-key-1",
    )


def _active(*, effect: bool = False) -> WorkerBeforeState:
    return WorkerBeforeState(
        status=WorkerExecutionStatus.RUNNING,
        state_revision=2,
        attempt_count=1,
        last_attempt_id=None,
        last_attempt_number=None,
        current_attempt_id=_ATTEMPT_ID,
        current_attempt_number=1,
        lease_owner="worker-1",
        lease_fence=1,
        lease_expires_at=_T2,
        fence_high_water=1,
        current_effect=_effect_binding() if effect else None,
    )


def _unknown() -> WorkerBeforeState:
    return WorkerBeforeState(
        status=WorkerExecutionStatus.OUTCOME_UNKNOWN,
        state_revision=3,
        attempt_count=1,
        last_attempt_id=_ATTEMPT_ID,
        last_attempt_number=1,
        current_attempt_id=None,
        current_attempt_number=None,
        lease_owner=None,
        lease_fence=None,
        lease_expires_at=None,
        fence_high_water=1,
        unresolved_effect=_effect_binding(),
    )


def _header(before: WorkerBeforeState, transaction_id: str, at: str) -> WorkerRequestHeader:
    return WorkerRequestHeader(
        transaction_id,
        _authority(),
        _action_target(),
        before,
        at,
    )


def _lease() -> WorkerLeaseToken:
    return WorkerLeaseToken(_ATTEMPT_ID, 1, "worker-1", 1, _T2)


def _authorize_plan():
    return _issue_worker_execution_plan(
        WorkerAuthorizeEffectRequest(
            _header(_active(), "8" * 32, _T1),
            _lease(),
            WorkerEffectAuthorization(
                _EFFECT_ID,
                WorkerEffectKind.ACTION_PROVIDER,
                "provider/account-1",
                "external-key-1",
                _effect_payload(),
                _T1,
            ),
        )
    )


def _effectless_complete_plan():
    return _issue_worker_execution_plan(
        WorkerCompleteRequest(
            _header(_active(), "9" * 32, _T1),
            _lease(),
            WorkerExecutionStatus.FAILED_TERMINAL,
            _T1,
            error=freeze_json_payload(
                {"error": "failed"},
                schema="audit.error.v1",
            ),
        )
    )


def _complete_with_proof_plan():
    effect = _effect_binding()
    return _issue_worker_execution_plan(
        WorkerCompleteRequest(
            _header(_active(effect=True), "7" * 32, _T1),
            _lease(),
            WorkerExecutionStatus.SUCCEEDED,
            _T1,
            effect_link=WorkerEffectLink(
                effect.attempt_id,
                effect.attempt_number,
                effect.effect_id,
                effect.effect_kind,
                effect.request_sha256,
                provider_receipt=freeze_json_payload(
                    {"receipt": "ok"},
                    schema="audit.provider-receipt.v1",
                ),
            ),
        )
    )


def _reconcile_plan():
    return _issue_worker_execution_plan(
        WorkerReconcileRequest(
            _header(_unknown(), "4" * 32, _T3),
            _ATTEMPT_ID,
            1,
            1,
            WorkerExecutionStatus.RETRY,
            WorkerReconcileProof(
                WorkerReconcileProofKind.PROVIDER_ABSENCE,
                _effect_binding(),
                freeze_json_payload(
                    {"proof": "exact"},
                    schema="audit.reconcile.v1",
                ),
            ),
            _T3,
            _T3,
        )
    )


def run_worker_projection_probe() -> WorkerProjectionEvidence:
    """Execute the three material projection classes and one tamper path."""

    authorize = _issue_worker_result_projection_plan(_authorize_plan())
    complete = _issue_worker_result_projection_plan(_effectless_complete_plan())
    proved = _issue_worker_result_projection_plan(_complete_with_proof_plan())
    reconciled = _issue_worker_result_projection_plan(_reconcile_plan())
    for value in (authorize, complete, proved, reconciled):
        if _require_worker_result_projection_plan(value) is not value:
            raise AssertionError("projection verifier did not preserve identity")

    frozen_vectors = (
        authorize.closure.effect_request.projection_sha256.hex(),
        authorize.closure.manifest_sha256.hex(),
        complete.closure.result.result_id,
        complete.closure.result.result_sha256.hex(),
        complete.closure.manifest_sha256.hex(),
        proved.closure.result.result_id,
        proved.closure.result.result_sha256.hex(),
        proved.closure.proof.proof_id,
        proved.closure.proof.proof_sha256.hex(),
        proved.closure.manifest_sha256.hex(),
        reconciled.closure.result.result_id,
        reconciled.closure.result.result_sha256.hex(),
        reconciled.closure.proof.proof_id,
        reconciled.closure.proof.proof_sha256.hex(),
        reconciled.closure.manifest_sha256.hex(),
    )
    payload_ownership_exact = bool(
        authorize.closure.effect_request.request_payload.payload_bytes
        not in authorize.closure.effect_request.projection_manifest_bytes
        and complete.closure.result.error_payload.payload_bytes
        not in complete.closure.result.projection_manifest_bytes
        and proved.closure.proof.provider_receipt_payload.payload_bytes
        not in proved.closure.proof.projection_manifest_bytes
        and reconciled.closure.proof.evidence_payload.payload_bytes
        not in reconciled.closure.proof.projection_manifest_bytes
        and proved.closure.result.result_payload is None
        and proved.closure.result.error_payload is None
        and not hasattr(proved.closure.result, "provider_receipt_payload")
        and reconciled.closure.result.result_payload is None
        and reconciled.closure.result.error_payload is None
    )

    tamper_rejected = False
    object.__setattr__(complete.closure, "manifest_sha256", b"x" * 32)
    try:
        _require_worker_result_projection_plan(complete)
    except WorkerResultProjectionIntegrityError:
        tamper_rejected = True

    return WorkerProjectionEvidence(
        authorize_shape=(
            int(authorize.closure.effect_request is not None),
            int(authorize.closure.result is not None),
            int(authorize.closure.proof is not None),
        ),
        complete_shape=(
            int(complete.closure.effect_request is not None),
            int(complete.closure.result is not None),
            int(complete.closure.proof is not None),
        ),
        reconcile_shape=(
            int(reconciled.closure.effect_request is not None),
            int(reconciled.closure.result is not None),
            int(reconciled.closure.proof is not None),
        ),
        empty_proof_sentinel=_v8_worker_result_mirror_proof_sha256(
            _issue_worker_result_projection_plan(_effectless_complete_plan())
        ).hex(),
        complete_proof_kind=proved.closure.proof.proof_kind,
        reconcile_proof_kind=reconciled.closure.proof.proof_kind,
        frozen_vectors=frozen_vectors,
        payload_ownership_exact=payload_ownership_exact,
        tamper_rejected=tamper_rejected,
    )


def _production_plan(baseline):
    profile = inspect_business_runtime(
        baseline.database,
        access=BusinessRuntimeAccess.WRITER,
    )
    planning = _read_production_atomic_planning_snapshot(baseline.database)
    atomic = _business_plan(
        baseline,
        commit_id="2" * 32,
        authority=profile.to_authority_binding(),
    )
    inner = _issue_atomic_business_write_plan(
        atomic_plan=atomic,
        control_plans=(),
        committed_at=planning.committed_at,
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


def _later_ms(value: str, milliseconds: int) -> str:
    parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    return (
        (parsed + timedelta(milliseconds=milliseconds)).strftime(
            "%Y-%m-%dT%H:%M:%S.%f"
        )[:-3]
        + "Z"
    )


def _claim_fixture(root: Path, name: str):
    baseline = create_canonical_mounted_business_database(root / name)
    production = _production_plan(baseline)
    if (
        SQLiteProductionAtomicWriter(baseline.database).commit(production).status
        is not ProductionWriteStatus.COMMITTED
    ):
        raise AssertionError("production origin was not committed")
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
    occurred_at = snapshot.committed_at
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
            _later_ms(occurred_at, 60_000),
        ),
        WorkerOutboxDispatchFence(dispatch_before, dispatch_after),
    )
    return baseline, _issue_worker_execution_plan(
        request,
        before_ledger=snapshot.worker_ledger_head.evidence.ledger_head,
    )


def _runtime_database_now(database) -> str:
    connection, _filesystem = database._open_unvalidated()
    try:
        row = connection.execute(
            "SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')"
        ).fetchone()
    finally:
        connection.close()
    if row is None or type(row[0]) is not str:
        raise AssertionError("SQLite runtime clock was unavailable")
    return row[0]


def _runtime_before(after) -> WorkerBeforeState:
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


def _runtime_lease(before: WorkerBeforeState) -> WorkerLeaseToken:
    return WorkerLeaseToken(
        before.current_attempt_id,
        before.current_attempt_number,
        before.lease_owner,
        before.lease_fence,
        before.lease_expires_at,
    )


def _runtime_dispatch_state(database) -> WorkerOutboxDispatchState:
    connection, _filesystem = database._open_unvalidated()
    try:
        row = connection.execute(
            "SELECT state_revision,published_sequence,"
            "published_repository_revision,published_ordinal,"
            "inflight_sequence,inflight_repository_revision,inflight_ordinal "
            "FROM outbox_dispatch_head WHERE singleton=1"
        ).fetchone()
    finally:
        connection.close()
    if row is None or len(row) != 7:
        raise AssertionError("OUTBOX dispatch state was unavailable")
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


def _runtime_authorize_plan(baseline, claim):
    before = _runtime_before(claim.closure.after_state)
    occurred_at = _runtime_database_now(baseline.database)
    plan = _issue_worker_execution_plan(
        WorkerAuthorizeEffectRequest(
            WorkerRequestHeader(
                "c" * 32,
                claim.authority,
                claim.target,
                before,
                occurred_at,
            ),
            _runtime_lease(before),
            WorkerEffectAuthorization(
                "d" * 32,
                WorkerEffectKind.OUTBOX_PUBLISH,
                claim.target.idempotency_scope,
                claim.target.idempotency_key,
                freeze_json_payload(
                    {"publish": claim.target.target_id},
                    schema="audit.runtime.outbox-publish-request.v1",
                ),
                occurred_at,
            ),
        ),
        before_ledger=claim.after_ledger,
    )
    return plan, _issue_worker_result_projection_plan(plan)


def _runtime_unknown_plan(baseline, authorize):
    before = _runtime_before(authorize.closure.after_state)
    occurred_at = _runtime_database_now(baseline.database)
    dispatch = _runtime_dispatch_state(baseline.database)
    plan = _issue_worker_execution_plan(
        WorkerCompleteRequest(
            WorkerRequestHeader(
                "e" * 32,
                authorize.authority,
                authorize.target,
                before,
                occurred_at,
            ),
            _runtime_lease(before),
            WorkerExecutionStatus.OUTCOME_UNKNOWN,
            occurred_at,
            error=freeze_json_payload(
                {"error": "provider-outcome-unknown"},
                schema="audit.runtime.outbox-error.v1",
            ),
            dispatch_fence=WorkerOutboxDispatchFence(dispatch, dispatch),
        ),
        before_ledger=authorize.after_ledger,
    )
    return plan, _issue_worker_result_projection_plan(plan)


def _runtime_reconcile_plan(baseline, unknown):
    before = _runtime_before(unknown.closure.after_state)
    effect = before.unresolved_effect
    if effect is None:
        raise AssertionError("OUTBOX unknown state lost its unresolved effect")
    occurred_at = _runtime_database_now(baseline.database)
    dispatch = _runtime_dispatch_state(baseline.database)
    if dispatch.inflight is None:
        raise AssertionError("OUTBOX reconciliation lost its inflight order")
    after_dispatch = WorkerOutboxDispatchState(
        dispatch.control_revision + 1,
        WorkerOutboxDispatchCursor(
            dispatch.inflight.sequence,
            dispatch.inflight.repository_revision,
            dispatch.inflight.ordinal,
        ),
        None,
    )
    effect_link = WorkerEffectLink(
        effect.attempt_id,
        effect.attempt_number,
        effect.effect_id,
        effect.effect_kind,
        effect.request_sha256,
        provider_receipt=freeze_json_payload(
            {"receipt": "published"},
            schema="audit.runtime.provider-receipt.v1",
        ),
    )
    plan = _issue_worker_execution_plan(
        WorkerReconcileRequest(
            WorkerRequestHeader(
                "f" * 32,
                unknown.authority,
                unknown.target,
                before,
                occurred_at,
            ),
            effect.attempt_id,
            effect.attempt_number,
            before.fence_high_water,
            WorkerExecutionStatus.PUBLISHED,
            WorkerReconcileProof(
                WorkerReconcileProofKind.PROVIDER_RECEIPT,
                effect,
                freeze_json_payload(
                    {"observed": "published"},
                    schema="audit.runtime.reconcile-evidence.v1",
                ),
                effect_link=effect_link,
            ),
            occurred_at,
            dispatch_fence=WorkerOutboxDispatchFence(
                dispatch,
                after_dispatch,
            ),
        ),
        before_ledger=unknown.after_ledger,
    )
    return plan, _issue_worker_result_projection_plan(plan)


def _runtime_identity(plan, projection=None) -> WorkerDurableIdentity:
    return WorkerDurableIdentity(
        plan.closure.request_transaction_id,
        plan.operation.value,
        plan.target.target_kind.value,
        plan.target.target_id,
        plan.closure.request_sha256,
        plan.closure.after_state_sha256,
        plan.expected_receipt.receipt_sha256,
        (
            None
            if projection is None
            else projection.closure.manifest_sha256
        ),
    )


def _runtime_counts(database, target_id: str):
    connection, _filesystem = database._open_unvalidated()
    try:
        def count(table: str) -> int:
            return int(connection.execute(
                "SELECT count(*) FROM %s WHERE target_id=?" % table,
                (target_id,),
            ).fetchone()[0])

        v8 = (
            count("worker_effects"),
            count("worker_results"),
            count("worker_reconcile_proofs"),
        )
        v9 = (
            count("worker_effect_request_projections"),
            count("worker_result_payload_projections"),
            count("worker_proof_payload_projections"),
        )
        closure = (
            count("worker_execution_receipts"),
            int(connection.execute(
                "SELECT count(*) FROM worker_ledger_entries AS ledger "
                "JOIN worker_execution_receipts AS receipt "
                "ON receipt.transaction_id=ledger.transaction_id "
                "WHERE receipt.target_id=?",
                (target_id,),
            ).fetchone()[0]),
            count("worker_attempts"),
            count("worker_target_current"),
            int(connection.execute(
                "SELECT count(*) FROM event_outbox WHERE event_id=?",
                (target_id,),
            ).fetchone()[0]),
        )
        return v8, v9, closure
    finally:
        connection.close()


def _runtime_rollback_rows(database, authorize, projection) -> Tuple[int, ...]:
    request_id = authorize.closure.request_transaction_id
    transaction_id = authorize.expected_receipt.receipt_parent.transaction_id
    effect = projection.closure.effect_request
    if effect is None:
        raise AssertionError("authorize projection lost its effect request")
    connection, _filesystem = database._open_unvalidated()
    try:
        keyed = (
            ("transaction_receipts", "transaction_id", transaction_id),
            ("worker_ledger_entries", "transaction_id", transaction_id),
            ("worker_execution_receipts", "request_id", request_id),
            ("worker_effects", "effect_id", effect.binding.effect.effect_id),
            (
                "worker_effect_request_projections",
                "effect_id",
                effect.binding.effect.effect_id,
            ),
        )
        values = [
            int(connection.execute(
                "SELECT count(*) FROM %s WHERE %s=?" % (table, column),
                (key,),
            ).fetchone()[0])
            for table, column, key in keyed
        ]
        current = connection.execute(
            "SELECT state_revision FROM worker_target_current WHERE target_id=?",
            (authorize.target.target_id,),
        ).fetchone()
        event = connection.execute(
            "SELECT state_revision FROM event_outbox WHERE event_id=?",
            (authorize.target.target_id,),
        ).fetchone()
        ledger = connection.execute(
            "SELECT sequence FROM worker_ledger_head WHERE singleton=1"
        ).fetchone()
        if current is None or event is None or ledger is None:
            raise AssertionError("rollback closure rows were unavailable")
        values.extend((int(current[0]), int(event[0]), int(ledger[0])))
        return tuple(values)
    finally:
        connection.close()


def _claim_with_expiry(plan, expiry: str):
    request = WorkerClaimRequest(
        WorkerRequestHeader(
            plan.request.header.request_transaction_id,
            plan.authority,
            plan.target,
            plan.before,
            plan.request.header.occurred_at,
        ),
        WorkerAttemptStart(
            plan.request.attempt.attempt_id,
            1,
            None,
            None,
            plan.request.attempt.lease_owner,
            1,
            plan.request.header.occurred_at,
            expiry,
        ),
        plan.request.dispatch_fence,
    )
    return _issue_worker_execution_plan(request, before_ledger=plan.before_ledger)


def _claim_rows(database, plan) -> Tuple[int, ...]:
    connection, _filesystem = database._open_unvalidated()
    try:
        statements = (
            (
                "SELECT count(*) FROM transaction_receipts WHERE transaction_id=?",
                plan.expected_receipt.receipt_parent.transaction_id,
            ),
            (
                "SELECT count(*) FROM worker_ledger_entries WHERE transaction_id=?",
                plan.expected_receipt.receipt_parent.transaction_id,
            ),
            (
                "SELECT count(*) FROM worker_execution_receipts WHERE request_id=?",
                plan.closure.request_transaction_id,
            ),
            (
                "SELECT count(*) FROM worker_attempts WHERE attempt_id=?",
                plan.request.attempt.attempt_id,
            ),
        )
        values = [
            int(connection.execute(sql, (key,)).fetchone()[0])
            for sql, key in statements
        ]
        current = connection.execute(
            "SELECT state_revision FROM worker_target_current WHERE target_id=?",
            (plan.target.target_id,),
        ).fetchone()
        event = connection.execute(
            "SELECT state_revision FROM event_outbox WHERE event_id=?",
            (plan.target.target_id,),
        ).fetchone()
        dispatch = connection.execute(
            "SELECT state_revision FROM outbox_dispatch_head WHERE singleton=1"
        ).fetchone()
        ledger = connection.execute(
            "SELECT sequence FROM worker_ledger_head WHERE singleton=1"
        ).fetchone()
        values.extend((int(current[0]), int(event[0]), int(dispatch[0]), int(ledger[0])))
        return tuple(values)
    finally:
        connection.close()


def run_worker_projection_runtime_probe() -> WorkerProjectionRuntimeEvidence:
    """Exercise the real V9 writer and its independent plan-free reader."""

    with tempfile.TemporaryDirectory(prefix="ze-worker-projection-audit-") as tmp:
        root = Path(tmp).resolve()

        baseline, claim = _claim_fixture(root, "lifecycle.sqlite3")
        claim_writer = SQLiteWorkerClaimWriter(baseline.database)
        claim_commit = claim_writer.commit(claim)
        claim_reconcile = claim_writer.reconcile(claim)
        if claim_commit.status is not SQLiteWorkerClaimStatus.COMMITTED:
            raise AssertionError("runtime claim did not commit")

        projection_writer = SQLiteWorkerProjectionWriter(baseline.database)
        authorize, authorize_projection = _runtime_authorize_plan(
            baseline,
            claim,
        )
        authorize_commit = projection_writer.commit(
            authorize,
            authorize_projection,
        )
        authorize_reconcile = projection_writer.reconcile(
            authorize,
            authorize_projection,
        )
        if authorize_commit.status.value != "committed":
            raise AssertionError("runtime authorization did not commit")

        unknown, unknown_projection = _runtime_unknown_plan(
            baseline,
            authorize,
        )
        unknown_commit = projection_writer.commit(
            unknown,
            unknown_projection,
        )
        unknown_reconcile = projection_writer.reconcile(
            unknown,
            unknown_projection,
        )
        if unknown_commit.status.value != "committed":
            raise AssertionError("runtime unknown completion did not commit")

        reconciled, reconcile_projection = _runtime_reconcile_plan(
            baseline,
            unknown,
        )
        reconcile_commit = projection_writer.commit(
            reconciled,
            reconcile_projection,
        )
        reconcile_reconcile = projection_writer.reconcile(
            reconciled,
            reconcile_projection,
        )
        if reconcile_commit.status.value != "committed":
            raise AssertionError("runtime reconciliation did not commit")

        claim_supported_suffix = claim_writer.reconcile(claim)
        auditor = SQLiteWorkerDurableAuditor(baseline.database)
        lifecycle = (
            (claim, None),
            (authorize, authorize_projection),
            (unknown, unknown_projection),
            (reconciled, reconcile_projection),
        )
        auditor_statuses = tuple(
            auditor.inspect(
                plan.closure.request_transaction_id,
                expected=_runtime_identity(plan, projection),
            ).status.value
            for plan, projection in lifecycle
        )
        absent_status = auditor.inspect("0" * 32).status.value
        v8_rows, v9_rows, closure_rows = _runtime_counts(
            baseline.database,
            claim.target.target_id,
        )

        rollback, rollback_claim = _claim_fixture(
            root,
            "rollback.sqlite3",
        )
        if (
            SQLiteWorkerClaimWriter(rollback.database)
            .commit(rollback_claim)
            .status
            is not SQLiteWorkerClaimStatus.COMMITTED
        ):
            raise AssertionError("rollback fixture claim did not commit")
        rollback_plan, rollback_projection = _runtime_authorize_plan(
            rollback,
            rollback_claim,
        )

        def fail_precommit(point):
            if point is SQLiteWorkerProjectionTestPoint.BEFORE_COMMIT:
                raise RuntimeError("audit projection rollback")

        rollback_writer = SQLiteWorkerProjectionWriter(rollback.database)
        rollback_result = rollback_writer.commit(
            rollback_plan,
            rollback_projection,
            _test_hook=fail_precommit,
        )
        rollback_reconcile = rollback_writer.reconcile(
            rollback_plan,
            rollback_projection,
        )
        rollback_rows = _runtime_rollback_rows(
            rollback.database,
            rollback_plan,
            rollback_projection,
        )

        ack, ack_claim = _claim_fixture(root, "ack.sqlite3")
        if (
            SQLiteWorkerClaimWriter(ack.database).commit(ack_claim).status
            is not SQLiteWorkerClaimStatus.COMMITTED
        ):
            raise AssertionError("ack-loss fixture claim did not commit")
        ack_plan, ack_projection = _runtime_authorize_plan(ack, ack_claim)

        def lose_ack(point):
            if point is SQLiteWorkerProjectionTestPoint.AFTER_COMMIT_BEFORE_ACK:
                raise RuntimeError("audit projection ack loss")

        ack_writer = SQLiteWorkerProjectionWriter(ack.database)
        ack_result = ack_writer.commit(
            ack_plan,
            ack_projection,
            _test_hook=lose_ack,
        )
        ack_reconcile = ack_writer.reconcile(ack_plan, ack_projection)

        barrier, barrier_claim = _claim_fixture(root, "barrier.sqlite3")
        if (
            SQLiteWorkerClaimWriter(barrier.database)
            .commit(barrier_claim)
            .status
            is not SQLiteWorkerClaimStatus.COMMITTED
        ):
            raise AssertionError("barrier fixture claim did not commit")
        barrier_plan, barrier_projection = _runtime_authorize_plan(
            barrier,
            barrier_claim,
        )
        barrier_writer = SQLiteWorkerProjectionWriter(barrier.database)
        database_type = type(barrier.database)
        original_barrier = database_type._finalize_committed_storage

        def fail_barrier(_database):
            raise OSError("audit projection durability barrier failure")

        database_type._finalize_committed_storage = fail_barrier
        try:
            barrier_result = barrier_writer.commit(
                barrier_plan,
                barrier_projection,
            )
        finally:
            database_type._finalize_committed_storage = original_barrier
        barrier_reconcile = barrier_writer.reconcile(
            barrier_plan,
            barrier_projection,
        )
        barrier_audit = SQLiteWorkerDurableAuditor(
            barrier.database
        ).inspect(
            barrier_plan.closure.request_transaction_id,
            expected=_runtime_identity(barrier_plan, barrier_projection),
        )

        return WorkerProjectionRuntimeEvidence(
            claim_statuses=(
                claim_commit.status.value,
                claim_reconcile.status.value,
            ),
            authorize_statuses=(
                authorize_commit.status.value,
                authorize_reconcile.status.value,
            ),
            complete_statuses=(
                unknown_commit.status.value,
                unknown_reconcile.status.value,
            ),
            reconcile_statuses=(
                reconcile_commit.status.value,
                reconcile_reconcile.status.value,
            ),
            claim_supported_suffix_status=claim_supported_suffix.status.value,
            claim_supported_suffix_code=claim_supported_suffix.code,
            auditor_identity_statuses=auditor_statuses,
            auditor_absent_status=absent_status,
            v8_projection_rows=v8_rows,
            v9_sidecar_rows=v9_rows,
            durable_closure_rows=closure_rows,
            rollback_status=rollback_result.status.value,
            rollback_reconcile_status=rollback_reconcile.status.value,
            rollback_rows=rollback_rows,
            ack_status=ack_result.status.value,
            ack_reconcile_status=ack_reconcile.status.value,
            ack_recovered=ack_result.recovered_after_ack_loss,
            barrier_status=barrier_result.status.value,
            barrier_code=barrier_result.code,
            barrier_reconcile_status=barrier_reconcile.status.value,
            barrier_fresh_audit_status=barrier_audit.status.value,
        )


def run_worker_claim_probe() -> WorkerClaimEvidence:
    """Exercise exact commit/retry/rollback/ack-loss/time/conflict outcomes."""

    with tempfile.TemporaryDirectory(prefix="ze-worker-claim-audit-") as tmp:
        root = Path(tmp).resolve()

        committed, committed_plan = _claim_fixture(root, "committed.sqlite3")
        committed_writer = SQLiteWorkerClaimWriter(committed.database)
        committed_result = committed_writer.commit(committed_plan)
        reconciled = committed_writer.reconcile(committed_plan)
        retried = committed_writer.commit(committed_plan)
        committed_rows = _claim_rows(committed.database, committed_plan)

        rolled, rolled_plan = _claim_fixture(root, "rollback.sqlite3")

        def fail_before(point):
            if point is SQLiteWorkerClaimTestPoint.BEFORE_COMMIT:
                raise RuntimeError("audit rollback")

        rolled_writer = SQLiteWorkerClaimWriter(rolled.database)
        rolled_result = rolled_writer.commit(rolled_plan, _test_hook=fail_before)
        rolled_reconcile = rolled_writer.reconcile(rolled_plan)
        rolled_rows = _claim_rows(rolled.database, rolled_plan)

        ack, ack_plan = _claim_fixture(root, "ack.sqlite3")

        def lose_ack(point):
            if point is SQLiteWorkerClaimTestPoint.AFTER_COMMIT_BEFORE_ACK:
                raise RuntimeError("audit ack loss")

        ack_writer = SQLiteWorkerClaimWriter(ack.database)
        ack_result = ack_writer.commit(ack_plan, _test_hook=lose_ack)
        ack_reconcile = ack_writer.reconcile(ack_plan)

        barrier, barrier_plan = _claim_fixture(root, "barrier.sqlite3")
        barrier_writer = SQLiteWorkerClaimWriter(barrier.database)
        database_type = type(barrier.database)
        original_barrier = database_type._finalize_committed_storage

        def fail_barrier(_database):
            raise OSError("audit durability barrier failure")

        database_type._finalize_committed_storage = fail_barrier
        try:
            barrier_result = barrier_writer.commit(barrier_plan)
        finally:
            database_type._finalize_committed_storage = original_barrier
        barrier_reconcile = barrier_writer.reconcile(barrier_plan)

        crossing, crossing_original = _claim_fixture(root, "crossing.sqlite3")
        expiry = _later_ms(crossing_original.request.header.occurred_at, 1_500)
        crossing_plan = _claim_with_expiry(crossing_original, expiry)
        crossing_hooks = []

        def cross_expiry(point):
            if point is SQLiteWorkerClaimTestPoint.BEFORE_COMMIT:
                crossing_hooks.append(point.value)
                deadline = time.monotonic() + 5.0
                while True:
                    clock, _filesystem = crossing.database._open_unvalidated()
                    try:
                        observed = clock.execute(
                            "SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')"
                        ).fetchone()[0]
                    finally:
                        clock.close()
                    if observed >= expiry:
                        break
                    if time.monotonic() >= deadline:
                        raise RuntimeError("SQLite clock did not cross claim expiry")
                    time.sleep(0.025)

        crossing_result = SQLiteWorkerClaimWriter(crossing.database).commit(
            crossing_plan,
            _test_hook=cross_expiry,
        )
        crossing_rows = _claim_rows(crossing.database, crossing_plan)

        conflict, conflict_plan = _claim_fixture(root, "conflict.sqlite3")
        connection, _filesystem = conflict.database._open_unvalidated()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO transaction_receipts(transaction_id,kind,committed_at) "
                "VALUES (?,?,?)",
                (
                    conflict_plan.expected_receipt.receipt_parent.transaction_id,
                    "occupied_by_other_kind",
                    conflict_plan.expected_receipt.receipt_parent.committed_at,
                ),
            )
            connection.commit()
        finally:
            if connection.in_transaction:
                connection.rollback()
            connection.close()
        conflict.database._finalize_committed_storage()
        conflict_result = SQLiteWorkerClaimWriter(conflict.database).commit(
            conflict_plan
        )
        conflict_rows = _claim_rows(conflict.database, conflict_plan)

        return WorkerClaimEvidence(
            committed_result.status.value,
            reconciled.status.value,
            retried.status.value,
            retried.recovered_after_ack_loss,
            committed_rows,
            rolled_result.status.value,
            rolled_reconcile.status.value,
            rolled_rows,
            ack_result.status.value,
            ack_result.recovered_after_ack_loss,
            ack_reconcile.status.value,
            barrier_result.status.value,
            barrier_result.code,
            barrier_reconcile.status.value,
            crossing_hooks == [SQLiteWorkerClaimTestPoint.BEFORE_COMMIT.value],
            crossing_result.status.value,
            crossing_result.code,
            crossing_rows,
            conflict_result.status.value,
            conflict_result.code,
            conflict_rows,
        )


__all__ = ()
