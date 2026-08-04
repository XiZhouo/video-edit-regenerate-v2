"""Fixed-vector and adversarial tests for the production atomic outer seal."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
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

import sqlite_production_authority as authority_module
from business_atomic_write_integrity import _issue_atomic_business_write_plan
from business_transaction_contract import (
    PipelineIntent,
    PipelineIntentKind,
    PipelineJobStatus,
    PipelineReplayPolicy,
)
from business_transaction_integrity import _issue_atomic_commit_plan
from production_atomic_write_contract import (
    EMPTY_PRODUCTION_OUTBOX_ALLOCATION_HEAD,
    PRODUCTION_ATOMIC_TARGET_GROUP_ORDER,
    ProductionAtomicTargetGroupEvidence,
    ProductionAtomicWriteContractError,
    ProductionMountedAuthorityEvidence,
    ProductionOutboxDispatchHeadEvidence,
    ProductionOutboxAllocationHead,
    ProductionWorkerLedgerHeadEvidence,
    ProductionWorkerTarget,
)
from production_atomic_write_integrity import (
    PRODUCTION_ATOMIC_CONTROL_MEMBER_HASH_DOMAIN,
    PRODUCTION_ATOMIC_MANIFEST_HASH_DOMAIN,
    PRODUCTION_ATOMIC_RECEIPT_HASH_DOMAIN,
    PRODUCTION_ATOMIC_TARGET_GROUP_HASH_DOMAIN,
    PRODUCTION_ATOMIC_TARGET_MEMBER_HASH_DOMAIN,
    PRODUCTION_ATOMIC_TARGETS_HASH_DOMAIN,
    PRODUCTION_OUTBOX_ALLOCATION_TAIL_HASH_DOMAIN,
    PRODUCTION_WORKER_GENESIS_STATE_HASH_DOMAIN,
    ProductionAtomicWriteIntegrityError,
    _issue_production_atomic_write_plan,
    _production_atomic_control_member_sha256,
    _production_outbox_allocation_tail,
    _production_worker_genesis_state_sha256,
    _require_production_atomic_write_plan,
)
from sqlite_production_authority import (
    ProductionMountedAuthorityBinding,
    ProductionOutboxDispatchHeadBinding,
    ProductionWorkerLedgerHeadBinding,
)
from test_business_atomic_write_integrity import COMMITTED_AT, _control
from test_business_transaction_integrity import _parts
from worker_execution_contract import (
    WorkerExecutionStatus,
    WorkerIdempotencyPolicy,
    WorkerOutboxDispatchCursor,
    WorkerOutboxDispatchState,
    WorkerOutboxOrder,
    WorkerReplayPolicy,
    WorkerTarget,
    WorkerTargetKind,
)


def _atomic_write():
    atomic = _issue_atomic_commit_plan(**_parts())
    control = _control(atomic, atomic.pipeline_intents[0])
    return _issue_atomic_business_write_plan(
        atomic_plan=atomic,
        control_plans=(control,),
        committed_at=COMMITTED_AT,
    )


def _mount(atomic_write):
    authority = atomic_write.atomic_plan.authority
    evidence = ProductionMountedAuthorityEvidence(
        authority=authority.authority,
        authority_state=authority.authority_state,
        repository_epoch=authority.authority_epoch,
        cutover_id=authority.cutover_id,
        source_admission_revision=authority.source_admission_revision,
        repository_revision=authority.authority_revision,
        control_revision=authority.control_revision,
        runtime_generation=authority.runtime_generation,
        code_digest=authority.code_digest,
        target_schema_version=8,
        target_schema_fingerprint="f" * 64,
        mount_transaction_id="_business-cutover-mount:" + authority.cutover_id,
        mount_manifest_sha256=b"m" * 32,
        mounted_at="2026-07-12T00:00:00.000Z",
    )
    return ProductionMountedAuthorityBinding(
        evidence,
        _issuer=authority_module._FRESH_MOUNT_ISSUER,
    )


def _targets(atomic_write):
    atomic = atomic_write.atomic_plan
    control = atomic_write.control_plans[0]
    pipeline = ProductionWorkerTarget(
        source_ordinal=0,
        target=control.target,
        genesis_status=WorkerExecutionStatus.QUEUED,
        genesis_state_sha256=control.closure.after_state_sha256,
    )
    action_intent = atomic.durable_actions[0]
    action_target = WorkerTarget(
        target_kind=WorkerTargetKind.ACTION,
        target_id=action_intent.action_id,
        project_id=atomic.before.project_id,
        generation=atomic.before.generation,
        document_id=atomic.before.document_id,
        origin_commit_id=atomic.commit_id,
        payload=action_intent.payload,
        replay_policy=WorkerReplayPolicy(action_intent.replay_policy.value),
        idempotency_policy=WorkerIdempotencyPolicy(
            action_intent.idempotency_policy.value
        ),
        idempotency_scope=action_intent.idempotency_scope,
        idempotency_key=action_intent.idempotency_key,
        max_attempts=action_intent.max_attempts,
    )
    action = ProductionWorkerTarget(
        source_ordinal=action_intent.ordinal,
        target=action_target,
        genesis_status=WorkerExecutionStatus.QUEUED,
        genesis_state_sha256=_production_worker_genesis_state_sha256(
            action_target,
            status=WorkerExecutionStatus.QUEUED,
        ),
        action_type=action_intent.action_type,
    )
    event = atomic.outbox[0]
    outbox_target = WorkerTarget(
        target_kind=WorkerTargetKind.OUTBOX,
        target_id=event.event_id,
        project_id=atomic.before.project_id,
        generation=atomic.before.generation,
        document_id=atomic.before.document_id,
        origin_commit_id=atomic.commit_id,
        payload=event.payload,
        replay_policy=WorkerReplayPolicy.RECONCILE_BEFORE_RETRY,
        idempotency_policy=WorkerIdempotencyPolicy.DURABLE_RECEIPT_RECONCILE,
        idempotency_scope="outbox/project-1/5",
        idempotency_key=event.event_id,
        max_attempts=10,
        outbox_order=WorkerOutboxOrder(
            sequence=1,
            repository_revision=atomic.candidate.authority_revision,
            ordinal=event.ordinal,
        ),
    )
    outbox = ProductionWorkerTarget(
        source_ordinal=event.ordinal,
        target=outbox_target,
        genesis_status=WorkerExecutionStatus.PENDING,
        genesis_state_sha256=_production_worker_genesis_state_sha256(
            outbox_target,
            status=WorkerExecutionStatus.PENDING,
        ),
        outbox_topic=event.topic,
        outbox_partition_key=event.partition_key,
    )
    return pipeline, action, outbox


def _scheduler_heads(atomic_write, *, updated_at="2026-07-12T00:00:00.000Z"):
    worker = ProductionWorkerLedgerHeadEvidence(
        state_revision=1,
        ledger_head=atomic_write.evidence.before_worker_ledger,
        previous_tail_sha256=b"\0" * 32,
        updated_at=updated_at,
    )
    dispatch = ProductionOutboxDispatchHeadEvidence(
        dispatch_state=WorkerOutboxDispatchState(
            1,
            WorkerOutboxDispatchCursor(0, 0, -1),
            None,
        ),
        previous_head_sha256=b"\0" * 32,
        head_sha256=b"\0" * 32,
        updated_at=updated_at,
    )
    return (
        ProductionWorkerLedgerHeadBinding(
            worker,
            _issuer=authority_module._FRESH_WORKER_LEDGER_HEAD_ISSUER,
        ),
        ProductionOutboxDispatchHeadBinding(
            dispatch,
            _issuer=authority_module._FRESH_OUTBOX_DISPATCH_HEAD_ISSUER,
        ),
    )


def _issue_plan(*, atomic_write, mounted_authority, targets,
                before_outbox_allocation, scheduler_heads=None):
    worker_head, dispatch_head = (
        _scheduler_heads(atomic_write)
        if scheduler_heads is None
        else scheduler_heads
    )
    return _issue_production_atomic_write_plan(
        atomic_write=atomic_write,
        mounted_authority=mounted_authority,
        worker_ledger_head=worker_head,
        outbox_dispatch_head=dispatch_head,
        targets=targets,
        before_outbox_allocation=before_outbox_allocation,
    )


def _plan():
    atomic = _atomic_write()
    return _issue_plan(
        atomic_write=atomic,
        mounted_authority=_mount(atomic),
        targets=_targets(atomic),
        before_outbox_allocation=EMPTY_PRODUCTION_OUTBOX_ALLOCATION_HEAD,
    )


def test_outer_plan_closes_three_groups_and_has_fixed_vectors() -> None:
    plan = _plan()
    assert _require_production_atomic_write_plan(plan) is plan
    assert tuple(group.target_kind for group in plan.evidence.target_groups) == (
        PRODUCTION_ATOMIC_TARGET_GROUP_ORDER
    )
    assert tuple(len(group.members) for group in plan.evidence.target_groups) == (
        1,
        1,
        1,
    )
    assert plan.evidence.target_count == 3
    assert plan.evidence.targets_sha256.hex() == (
        "559cdd84574668beae10c8f01d6dcfab5adc3193be32855f25af0369d9019545"
    )
    assert plan.evidence.production_manifest_sha256.hex() == (
        "5b84d705e66128555bfa0216eada1f1b3e8cbd8425524238862c196dd800238c"
    )
    assert plan.expected_receipt.production_receipt_sha256.hex() == (
        "63550d6c42ae72bc57678cbab8d998866a25b2ae0f45eec876ef6717fae65877"
    )
    assert plan.evidence.allocations[0].allocation_tail_sha256.hex() == (
        "51b0afa7e772c74d73c6336e02ceab844e3a5c51e1c463194a7262f6e5efd1d4"
    )


def test_shared_hash_domains_and_control_member_have_fixed_vectors() -> None:
    assert (
        PRODUCTION_OUTBOX_ALLOCATION_TAIL_HASH_DOMAIN,
        PRODUCTION_ATOMIC_CONTROL_MEMBER_HASH_DOMAIN,
        PRODUCTION_WORKER_GENESIS_STATE_HASH_DOMAIN,
        PRODUCTION_ATOMIC_TARGET_MEMBER_HASH_DOMAIN,
        PRODUCTION_ATOMIC_TARGET_GROUP_HASH_DOMAIN,
        PRODUCTION_ATOMIC_TARGETS_HASH_DOMAIN,
        PRODUCTION_ATOMIC_MANIFEST_HASH_DOMAIN,
        PRODUCTION_ATOMIC_RECEIPT_HASH_DOMAIN,
    ) == (
        b"ze.production-outbox-allocation.tail.v1",
        b"ze.production-atomic-control-member.v1",
        b"ze.production-worker-genesis-state.v1",
        b"ze.production-atomic-target-member.v1",
        b"ze.production-atomic-target-group.v1",
        b"ze.production-atomic-targets.v1",
        b"ze.production-atomic-write.manifest-digest.v1",
        b"ze.production-atomic-write.receipt.v1",
    )
    member = _production_atomic_control_member_sha256(
        commit_id="a" * 32,
        ordinal=7,
        intent_id="5" * 32,
        control_transaction_id="_pipeline-business-control:" + "5" * 32,
        sequence=9,
        ledger_tail_sha256=b"l" * 32,
    )
    allocation = _production_outbox_allocation_tail(
        sequence=11,
        event_id="3" * 32,
        commit_id="a" * 32,
        repository_revision=17,
        ordinal=2,
        previous_tail_sha256=b"p" * 32,
    )
    assert member.hex() == (
        "8423fa705c3808fbdea0549855f8bd843583cec39d1b2a677ac2f5c78485b3d2"
    )
    assert allocation.hex() == (
        "5e556abf7dd465d0e0857be63d56dfa3ce21207e70bd6552b09c2dc59caa85f3"
    )


def test_manifest_is_a_bounded_summary_not_a_second_target_payload_store() -> None:
    plan = _plan()
    manifest = json.loads(plan.evidence.production_manifest_bytes)
    assert manifest["targets"] == {
        "count": 3,
        "group_order": ["pipeline", "action", "outbox"],
        "sha256": plan.evidence.targets_sha256.hex(),
    }
    assert manifest["pure_atomic"]["receipt_sha256"] == (
        plan.atomic_write.expected_receipt.receipt_sha256.hex()
    )
    assert manifest["mounted_authority"]["mount_manifest_sha256"] == (
        plan.mounted_authority.evidence.mount_manifest_sha256.hex()
    )
    encoded = plan.evidence.production_manifest_bytes
    for target in plan.targets:
        assert target.target.target_id.encode("ascii") not in encoded
        assert target.target.payload.payload_bytes not in encoded


def test_direct_mount_evidence_cannot_mint_plan_authority() -> None:
    atomic = _atomic_write()
    direct = _mount(atomic).evidence
    with pytest.raises(ProductionAtomicWriteIntegrityError) as rejected:
        _issue_plan(
            atomic_write=atomic,
            mounted_authority=direct,
            targets=_targets(atomic),
            before_outbox_allocation=EMPTY_PRODUCTION_OUTBOX_ALLOCATION_HEAD,
        )
    assert rejected.value.code == "production_atomic_mount_binding_invalid"
    with pytest.raises(authority_module.SQLiteProductionAuthorityError):
        ProductionMountedAuthorityBinding(direct, _issuer=object())


@pytest.mark.parametrize(
    "index,changes,code",
    (
        (0, {"genesis_state_sha256": b"z" * 32}, "production_pipeline_target_mapping_invalid"),
        (1, {"action_type": "changed"}, "production_action_target_mapping_invalid"),
        (2, {"outbox_topic": "changed"}, "production_outbox_target_mapping_invalid"),
    ),
)
def test_routing_and_genesis_are_not_advisory(index, changes, code) -> None:
    atomic = _atomic_write()
    targets = list(_targets(atomic))
    targets[index] = dataclasses.replace(targets[index], **changes)
    with pytest.raises(ProductionAtomicWriteIntegrityError) as rejected:
        _issue_plan(
            atomic_write=atomic,
            mounted_authority=_mount(atomic),
            targets=tuple(targets),
            before_outbox_allocation=EMPTY_PRODUCTION_OUTBOX_ALLOCATION_HEAD,
        )
    assert rejected.value.code == code


@pytest.mark.parametrize(
    "target_changes",
    (
        {
            "replay_policy": WorkerReplayPolicy.PROVIDER_IDEMPOTENT,
            "idempotency_policy": WorkerIdempotencyPolicy.REQUIRED_EXTERNAL_KEY,
        },
        {"idempotency_scope": "outbox/wrong-scope"},
        {"idempotency_key": "9" * 32},
        {"max_attempts": 9},
    ),
)
def test_outbox_execution_policy_is_one_canonical_set(target_changes) -> None:
    atomic = _atomic_write()
    targets = list(_targets(atomic))
    targets[-1] = dataclasses.replace(
        targets[-1],
        target=dataclasses.replace(targets[-1].target, **target_changes),
    )
    with pytest.raises(ProductionAtomicWriteIntegrityError) as rejected:
        _issue_plan(
            atomic_write=atomic,
            mounted_authority=_mount(atomic),
            targets=tuple(targets),
            before_outbox_allocation=EMPTY_PRODUCTION_OUTBOX_ALLOCATION_HEAD,
        )
    assert rejected.value.code == "production_outbox_target_mapping_invalid"


def test_group_order_and_ordinals_are_contract_facts() -> None:
    plan = _plan()
    with pytest.raises(ProductionAtomicWriteIntegrityError) as rejected:
        _issue_plan(
            atomic_write=plan.atomic_write,
            mounted_authority=plan.mounted_authority,
            targets=(plan.targets[1], plan.targets[0], plan.targets[2]),
            before_outbox_allocation=EMPTY_PRODUCTION_OUTBOX_ALLOCATION_HEAD,
        )
    assert rejected.value.code == "production_atomic_target_order_invalid"

    member = dataclasses.replace(plan.targets[1], source_ordinal=1)
    duplicate = dataclasses.replace(plan.targets[1], source_ordinal=1)
    with pytest.raises(ProductionAtomicWriteContractError):
        ProductionAtomicTargetGroupEvidence(
            WorkerTargetKind.ACTION,
            (member, duplicate),
            b"g" * 32,
        )


def test_pipeline_create_keeps_original_ordinal_across_interleaved_transition() -> None:
    original = _parts()["pipeline_intents"][0]
    transition = PipelineIntent(
        ordinal=0,
        intent_id="6" * 32,
        job_id=original.job_id,
        pipeline_id=original.pipeline_id,
        intent_type="pause",
        idempotency_key="pause-before-create",
        payload=original.payload,
        intent_kind=PipelineIntentKind.TRANSITION,
        expected_status=PipelineJobStatus.QUEUED,
        target_status=PipelineJobStatus.PAUSED,
        replay_policy=PipelineReplayPolicy.EXACT_CAS_RETRY,
        expected_state_revision=3,
    )
    create = dataclasses.replace(
        original,
        ordinal=1,
        intent_id="7" * 32,
        job_id="7" * 32,
        pipeline_id="render-pipeline-later",
        idempotency_key="pipeline-create-later",
    )
    parts = _parts()
    parts["pipeline_intents"] = (transition, create)
    atomic = _issue_atomic_commit_plan(**parts)
    transition_control = _control(atomic, transition)
    create_control = _control(
        atomic,
        create,
        before_ledger=transition_control.after_ledger,
    )
    atomic_write = _issue_atomic_business_write_plan(
        atomic_plan=atomic,
        control_plans=(transition_control, create_control),
        committed_at=COMMITTED_AT,
    )
    _discarded_pipeline, action, outbox = _targets(atomic_write)
    pipeline = ProductionWorkerTarget(
        source_ordinal=create.ordinal,
        target=create_control.target,
        genesis_status=WorkerExecutionStatus.QUEUED,
        genesis_state_sha256=create_control.closure.after_state_sha256,
    )
    plan = _issue_plan(
        atomic_write=atomic_write,
        mounted_authority=_mount(atomic_write),
        targets=(pipeline, action, outbox),
        before_outbox_allocation=EMPTY_PRODUCTION_OUTBOX_ALLOCATION_HEAD,
    )
    assert plan.evidence.target_groups[0].members[0].source_ordinal == 1


def test_pipeline_group_preserves_create_transition_create_gap() -> None:
    create_zero = _parts()["pipeline_intents"][0]
    transition = PipelineIntent(
        ordinal=1,
        intent_id="6" * 32,
        job_id="8" * 32,
        pipeline_id="existing-render-pipeline",
        intent_type="pause",
        idempotency_key="pause-between-creates",
        payload=create_zero.payload,
        intent_kind=PipelineIntentKind.TRANSITION,
        expected_status=PipelineJobStatus.QUEUED,
        target_status=PipelineJobStatus.PAUSED,
        replay_policy=PipelineReplayPolicy.EXACT_CAS_RETRY,
        expected_state_revision=1,
    )
    create_two = dataclasses.replace(
        create_zero,
        ordinal=2,
        intent_id="7" * 32,
        job_id="7" * 32,
        pipeline_id="render-pipeline-two",
        idempotency_key="pipeline-create-two",
    )
    parts = _parts()
    parts["pipeline_intents"] = (create_zero, transition, create_two)
    atomic = _issue_atomic_commit_plan(**parts)
    first = _control(atomic, create_zero)
    middle = _control(
        atomic,
        transition,
        before_ledger=first.after_ledger,
    )
    last = _control(
        atomic,
        create_two,
        before_ledger=middle.after_ledger,
    )
    atomic_write = _issue_atomic_business_write_plan(
        atomic_plan=atomic,
        control_plans=(first, middle, last),
        committed_at=COMMITTED_AT,
    )
    pipeline_zero, action, outbox = _targets(atomic_write)
    pipeline_two = ProductionWorkerTarget(
        source_ordinal=2,
        target=last.target,
        genesis_status=WorkerExecutionStatus.QUEUED,
        genesis_state_sha256=last.closure.after_state_sha256,
    )
    plan = _issue_plan(
        atomic_write=atomic_write,
        mounted_authority=_mount(atomic_write),
        targets=(pipeline_zero, pipeline_two, action, outbox),
        before_outbox_allocation=EMPTY_PRODUCTION_OUTBOX_ALLOCATION_HEAD,
    )
    assert tuple(
        value.source_ordinal
        for value in plan.evidence.target_groups[0].members
    ) == (0, 2)


def test_allocation_head_is_full_cas_and_is_manifest_bound() -> None:
    plan = _plan()
    before = plan.evidence.before_outbox_allocation
    after = plan.evidence.after_outbox_allocation
    assert before == EMPTY_PRODUCTION_OUTBOX_ALLOCATION_HEAD
    assert after.state_revision == before.state_revision + 1
    assert after.sequence == before.sequence + 1
    assert after.previous_tail_sha256 == before.tail_sha256
    assert after.tail_sha256 == plan.evidence.allocations[-1].allocation_tail_sha256

    changed = ProductionOutboxAllocationHead(
        state_revision=3,
        sequence=4,
        previous_tail_sha256=b"q" * 32,
        tail_sha256=b"r" * 32,
        updated_at="2026-07-11T23:59:59.000Z",
    )
    targets = list(_targets(plan.atomic_write))
    event_target = targets[-1].target
    targets[-1] = dataclasses.replace(
        targets[-1],
        target=dataclasses.replace(
            event_target,
            outbox_order=dataclasses.replace(event_target.outbox_order, sequence=5),
        ),
    )
    targets[-1] = dataclasses.replace(
        targets[-1],
        genesis_state_sha256=_production_worker_genesis_state_sha256(
            targets[-1].target,
            status=WorkerExecutionStatus.PENDING,
        ),
    )
    replanned = _issue_plan(
        atomic_write=plan.atomic_write,
        mounted_authority=plan.mounted_authority,
        targets=tuple(targets),
        before_outbox_allocation=changed,
    )
    assert replanned.evidence.before_outbox_allocation == changed
    assert replanned.evidence.after_outbox_allocation.sequence == 5
    assert replanned.evidence.production_manifest_sha256 != (
        plan.evidence.production_manifest_sha256
    )


def test_allocation_head_time_is_exact_and_no_default_head_can_be_smuggled() -> None:
    atomic = _atomic_write()
    targets = _targets(atomic)
    binding = _mount(atomic)
    worker_head, dispatch_head = _scheduler_heads(atomic)
    with pytest.raises(TypeError):
        _issue_production_atomic_write_plan(
            atomic_write=atomic,
            mounted_authority=binding,
            worker_ledger_head=worker_head,
            outbox_dispatch_head=dispatch_head,
            targets=targets,
        )

    changed_time = dataclasses.replace(
        EMPTY_PRODUCTION_OUTBOX_ALLOCATION_HEAD,
        updated_at="2026-07-11T23:59:59.999Z",
    )
    baseline = _issue_plan(
        atomic_write=atomic,
        mounted_authority=binding,
        targets=targets,
        before_outbox_allocation=EMPTY_PRODUCTION_OUTBOX_ALLOCATION_HEAD,
    )
    changed = _issue_plan(
        atomic_write=atomic,
        mounted_authority=binding,
        targets=targets,
        before_outbox_allocation=changed_time,
    )
    assert changed.evidence.before_outbox_allocation.updated_at == (
        changed_time.updated_at
    )
    assert changed.evidence.after_outbox_allocation.updated_at == COMMITTED_AT
    assert changed.evidence.production_manifest_sha256 != (
        baseline.evidence.production_manifest_sha256
    )


def test_scheduler_heads_are_fresh_manifest_bound_time_authorities() -> None:
    atomic = _atomic_write()
    targets = _targets(atomic)
    binding = _mount(atomic)
    baseline_heads = _scheduler_heads(atomic)
    baseline = _issue_plan(
        atomic_write=atomic,
        mounted_authority=binding,
        targets=targets,
        before_outbox_allocation=EMPTY_PRODUCTION_OUTBOX_ALLOCATION_HEAD,
        scheduler_heads=baseline_heads,
    )
    changed_heads = _scheduler_heads(
        atomic,
        updated_at="2026-07-12T01:02:03.003Z",
    )
    changed = _issue_plan(
        atomic_write=atomic,
        mounted_authority=binding,
        targets=targets,
        before_outbox_allocation=EMPTY_PRODUCTION_OUTBOX_ALLOCATION_HEAD,
        scheduler_heads=changed_heads,
    )
    assert changed.evidence.before_worker_ledger_head.updated_at.endswith(
        "03.003Z"
    )
    assert changed.evidence.before_outbox_dispatch_head.updated_at.endswith(
        "03.003Z"
    )
    assert changed.evidence.production_manifest_sha256 != (
        baseline.evidence.production_manifest_sha256
    )

    future_heads = _scheduler_heads(
        atomic,
        updated_at="2026-07-12T01:02:03.005Z",
    )
    with pytest.raises(ProductionAtomicWriteIntegrityError) as rejected:
        _issue_plan(
            atomic_write=atomic,
            mounted_authority=binding,
            targets=targets,
            before_outbox_allocation=EMPTY_PRODUCTION_OUTBOX_ALLOCATION_HEAD,
            scheduler_heads=future_heads,
        )
    assert rejected.value.code == "production_committed_time_stale"


def test_scheduler_head_bindings_cannot_be_plain_or_cross_plan() -> None:
    atomic = _atomic_write()
    targets = _targets(atomic)
    binding = _mount(atomic)
    worker_head, dispatch_head = _scheduler_heads(atomic)
    with pytest.raises(ProductionAtomicWriteIntegrityError) as rejected:
        _issue_production_atomic_write_plan(
            atomic_write=atomic,
            mounted_authority=binding,
            worker_ledger_head=worker_head.evidence,
            outbox_dispatch_head=dispatch_head,
            targets=targets,
            before_outbox_allocation=EMPTY_PRODUCTION_OUTBOX_ALLOCATION_HEAD,
        )
    assert rejected.value.code == "production_scheduler_head_binding_invalid"

    changed_worker = dataclasses.replace(
        worker_head.evidence,
        ledger_head=dataclasses.replace(
            worker_head.evidence.ledger_head,
            sequence=1,
            tail_sha256=b"w" * 32,
        ),
    )
    changed_binding = ProductionWorkerLedgerHeadBinding(
        changed_worker,
        _issuer=authority_module._FRESH_WORKER_LEDGER_HEAD_ISSUER,
    )
    with pytest.raises(ProductionAtomicWriteIntegrityError) as rejected:
        _issue_plan(
            atomic_write=atomic,
            mounted_authority=binding,
            targets=targets,
            before_outbox_allocation=EMPTY_PRODUCTION_OUTBOX_ALLOCATION_HEAD,
            scheduler_heads=(changed_binding, dispatch_head),
        )
    assert rejected.value.code == "production_scheduler_head_changed"


def test_require_rebuilds_every_outer_digest() -> None:
    plan = _plan()
    object.__setattr__(plan.evidence, "targets_sha256", b"x" * 32)
    with pytest.raises(ProductionAtomicWriteIntegrityError) as rejected:
        _require_production_atomic_write_plan(plan)
    assert rejected.value.code == "production_atomic_plan_changed"


def test_wrapper_rejects_cross_kind_routing() -> None:
    plan = _plan()
    with pytest.raises(ProductionAtomicWriteContractError) as rejected:
        dataclasses.replace(plan.targets[0], action_type="not-pipeline")
    assert rejected.value.code == "production_worker_target_routing_invalid"
    with pytest.raises(ProductionAtomicWriteContractError) as rejected:
        dataclasses.replace(plan.targets[1], action_type=None)
    assert rejected.value.code == "production_worker_target_routing_invalid"


def test_empty_head_rejects_partial_zero_identity() -> None:
    with pytest.raises(ProductionAtomicWriteContractError) as rejected:
        ProductionOutboxAllocationHead(
            1,
            0,
            b"x" * 32,
            b"\0" * 32,
            "1970-01-01T00:00:00.000Z",
        )
    assert rejected.value.code == "production_outbox_allocation_head_invalid"


__all__ = ()
