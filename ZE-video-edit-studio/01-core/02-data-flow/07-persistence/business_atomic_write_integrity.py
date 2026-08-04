"""One-to-one integrity issuer for AtomicCommitPlan and pipeline controls."""

from __future__ import annotations

import hashlib
import hmac
from typing import Any, NamedTuple, Optional, Tuple

from business_codec import BusinessCodecError, encode_payload_bounded
from business_transaction_contract import (
    PipelineIntent,
    PipelineIntentKind,
    PipelineReplayPolicy,
)
from business_transaction_integrity import (
    AtomicCommitPlan,
    BusinessTransactionIntegrityError,
    _require_atomic_commit_plan,
)
from worker_execution_contract import (
    EMPTY_WORKER_LEDGER_HEAD,
    PipelineBusinessControlReceiptEvidence,
    PipelineBusinessControlOperation,
    WorkerExecutionContractError,
    WorkerExecutionStatus,
    WorkerIdempotencyPolicy,
    WorkerLedgerHead,
    WorkerLedgerMembershipEvidence,
    WorkerReplayPolicy,
    WorkerTargetKind,
)
from worker_execution_integrity import (
    PipelineBusinessControlPlan,
    WorkerExecutionIntegrityError,
    _rebuild_control_receipt,
    _require_pipeline_business_control_plan,
)

from business_atomic_write_contract import (
    ATOMIC_BUSINESS_WRITE_MANIFEST_SCHEMA,
    ATOMIC_BUSINESS_WRITE_PLAN_STATUS,
    ATOMIC_BUSINESS_WRITE_PROTOCOL,
    ATOMIC_BUSINESS_WRITE_TRANSACTION_KIND,
    ATOMIC_BUSINESS_WRITE_TRANSACTION_PREFIX,
    MAX_ATOMIC_BUSINESS_CONTROL_BINDINGS,
    MAX_ATOMIC_BUSINESS_WRITE_MANIFEST_BYTES,
    MAX_ATOMIC_BUSINESS_WRITE_MANIFEST_NODES,
    AtomicBusinessControlBinding,
    AtomicBusinessWriteContractError,
    AtomicBusinessWriteClassification,
    AtomicBusinessWriteFreshObservation,
    AtomicBusinessWriteGenericReceiptEvidence,
    AtomicBusinessWriteEvidence,
    AtomicBusinessWritePersistedObservation,
    AtomicBusinessWritePersistedReadStatus,
    AtomicBusinessWriteReceiptEvidence,
    AtomicBusinessWriteReconcileStatus,
    _unwrap_atomic_business_write_fresh_observation,
)


ATOMIC_BUSINESS_WRITE_MANIFEST_HASH_DOMAIN = (
    b"ze.atomic-business-write.manifest-digest.v1"
)
ATOMIC_BUSINESS_WRITE_CONTROLS_HASH_DOMAIN = (
    b"ze.atomic-business-write.controls.v1"
)
ATOMIC_BUSINESS_WRITE_RECEIPT_HASH_DOMAIN = (
    b"ze.atomic-business-write.receipt.v1"
)

_PLAN_ISSUER = object()


class AtomicBusinessWriteIntegrityError(ValueError):
    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        if type(code) is not str or not code:
            raise TypeError("code must be a non-empty exact string")
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise AtomicBusinessWriteIntegrityError(code)


def _frame(hasher: Any, value: bytes) -> None:
    hasher.update(len(value).to_bytes(8, "big"))
    hasher.update(value)


def _digest(domain: bytes, values: Tuple[bytes, ...]) -> bytes:
    hasher = hashlib.sha256()
    _frame(hasher, domain)
    for value in values:
        _frame(hasher, value)
    return hasher.digest()


def _manifest_digest(value: bytes) -> bytes:
    return _digest(ATOMIC_BUSINESS_WRITE_MANIFEST_HASH_DOMAIN, (value,))


def _ledger_manifest(value: WorkerLedgerHead) -> dict:
    return {"sequence": value.sequence, "tail_sha256": value.tail_sha256.hex()}


def _binding_manifest(value: AtomicBusinessControlBinding) -> dict:
    return {
        "ordinal": value.ordinal,
        "intent_id": value.intent_id,
        "job_id": value.job_id,
        "control_transaction_id": value.control_transaction_id,
        "request_sha256": value.request_sha256.hex(),
        "after_state_sha256": value.after_state_sha256.hex(),
        "receipt_sha256": value.receipt_sha256.hex(),
        "receipt_sequence": value.receipt_sequence,
    }


def _build_manifest(
    atomic: AtomicCommitPlan,
    committed_at: str,
    bindings: Tuple[AtomicBusinessControlBinding, ...],
    controls_sha256: bytes,
    before_ledger: WorkerLedgerHead,
    after_ledger: WorkerLedgerHead,
) -> bytes:
    value = {
        "schema": ATOMIC_BUSINESS_WRITE_MANIFEST_SCHEMA,
        "protocol": ATOMIC_BUSINESS_WRITE_PROTOCOL,
        "status": ATOMIC_BUSINESS_WRITE_PLAN_STATUS,
        "commit": {
            "commit_id": atomic.commit_id,
            "committed_at": committed_at,
            "atomic_manifest_sha256": atomic.closure.manifest_sha256.hex(),
            "pipeline_intents_sha256": atomic.closure.pipeline_intents_sha256.hex(),
        },
        "worker_ledger": {
            "before": _ledger_manifest(before_ledger),
            "after": _ledger_manifest(after_ledger),
        },
        "controls": {
            "count": len(bindings),
            "sha256": controls_sha256.hex(),
            "members": [_binding_manifest(value) for value in bindings],
        },
    }
    try:
        return encode_payload_bounded(
            value,
            MAX_ATOMIC_BUSINESS_WRITE_MANIFEST_BYTES,
            MAX_ATOMIC_BUSINESS_WRITE_MANIFEST_NODES,
        )
    except BusinessCodecError as exc:
        raise AtomicBusinessWriteIntegrityError(
            "atomic_business_write_manifest_invalid"
        ) from exc


def _expected_replay(value: PipelineReplayPolicy) -> WorkerReplayPolicy:
    try:
        return WorkerReplayPolicy(value.value)
    except (TypeError, ValueError):
        _fail("atomic_business_write_replay_policy_invalid")
    raise AssertionError("unreachable")


def _expected_idempotency(replay: WorkerReplayPolicy) -> WorkerIdempotencyPolicy:
    if replay is WorkerReplayPolicy.NEVER_REPLAY:
        return WorkerIdempotencyPolicy.SINGLE_ATTEMPT
    return WorkerIdempotencyPolicy.DURABLE_RECEIPT_RECONCILE


def _expected_status(value: Any) -> Any:
    if value is None:
        return None
    try:
        return WorkerExecutionStatus(value.value)
    except (TypeError, ValueError):
        _fail("atomic_business_write_pipeline_status_invalid")
    raise AssertionError("unreachable")


def _require_control_mapping(
    atomic: AtomicCommitPlan,
    intent: PipelineIntent,
    control: PipelineBusinessControlPlan,
    committed_at: str,
) -> AtomicBusinessControlBinding:
    control = _require_pipeline_business_control_plan(control)
    request = control.request
    target = control.target
    control_intent = control.control_intent
    replay = _expected_replay(intent.replay_policy)
    expected_operation = (
        PipelineBusinessControlOperation.CREATE
        if intent.intent_kind is PipelineIntentKind.CREATE
        else PipelineBusinessControlOperation.APPLY_CONTROL_INTENT
    )
    if (
        control.authority != atomic.authority
        or request.request_transaction_id != intent.intent_id
        or request.operation is not expected_operation
        or request.occurred_at != committed_at
        or target.target_kind is not WorkerTargetKind.PIPELINE
        or target.target_id != intent.job_id
        or target.project_id != atomic.before.project_id
        or target.generation != atomic.before.generation
        or target.document_id != atomic.before.document_id
        or target.pipeline_id != intent.pipeline_id
        or control_intent.origin_commit_id != atomic.commit_id
        or control_intent.intent_type != intent.intent_type
        or control_intent.idempotency_key != intent.idempotency_key
        or control_intent.payload != intent.payload
        or control_intent.replay_policy is not replay
        or control_intent.idempotency_policy
        is not _expected_idempotency(replay)
        or control_intent.max_attempts != intent.max_attempts
        or request.target_status is not _expected_status(intent.target_status)
    ):
        _fail("atomic_business_write_control_mapping_invalid")

    if intent.intent_kind is PipelineIntentKind.CREATE:
        if (
            request.before is not None
            or target.origin_commit_id != atomic.commit_id
            or target.payload != intent.payload
            or target.idempotency_key != intent.idempotency_key
            or target.intent_type != intent.intent_type
            or target.replay_policy is not replay
            or target.idempotency_policy is not _expected_idempotency(replay)
            or target.max_attempts != intent.max_attempts
        ):
            _fail("atomic_business_write_control_mapping_invalid")
    else:
        if (
            request.before is None
            or request.before.status is not _expected_status(intent.expected_status)
            or request.before.state_revision != intent.expected_state_revision
        ):
            _fail("atomic_business_write_control_mapping_invalid")

    receipt = control.expected_receipt
    if (
        receipt.request_transaction_id != intent.intent_id
        or receipt.receipt_parent.committed_at != committed_at
        or receipt.request_sha256 != control.closure.request_sha256
        or receipt.after_state_sha256 != control.closure.after_state_sha256
    ):
        _fail("atomic_business_write_control_receipt_invalid")
    return AtomicBusinessControlBinding(
        ordinal=intent.ordinal,
        intent_id=intent.intent_id,
        job_id=intent.job_id,
        control_transaction_id=request.request_transaction_id,
        request_sha256=control.closure.request_sha256,
        after_state_sha256=control.closure.after_state_sha256,
        receipt_sha256=receipt.receipt_sha256,
        receipt_sequence=receipt.sequence,
    )


def _atomic_receipt_digest(
    *,
    commit_id: str,
    receipt_parent: AtomicBusinessWriteGenericReceiptEvidence,
    atomic_manifest_sha256: bytes,
    pipeline_intents_sha256: bytes,
    controls_sha256: bytes,
    control_count: int,
    before_worker_ledger: WorkerLedgerHead,
    after_worker_ledger: WorkerLedgerHead,
    manifest_sha256: bytes,
) -> bytes:
    return _digest(
        ATOMIC_BUSINESS_WRITE_RECEIPT_HASH_DOMAIN,
        (
            commit_id.encode("ascii"),
            receipt_parent.transaction_id.encode("utf-8"),
            receipt_parent.kind.encode("utf-8"),
            receipt_parent.committed_at.encode("ascii"),
            atomic_manifest_sha256,
            pipeline_intents_sha256,
            controls_sha256,
            control_count.to_bytes(8, "big"),
            before_worker_ledger.sequence.to_bytes(8, "big"),
            before_worker_ledger.tail_sha256,
            after_worker_ledger.sequence.to_bytes(8, "big"),
            after_worker_ledger.tail_sha256,
            manifest_sha256,
        ),
    )


def _build_atomic_receipt(
    evidence: AtomicBusinessWriteEvidence,
) -> AtomicBusinessWriteReceiptEvidence:
    parent = AtomicBusinessWriteGenericReceiptEvidence(
        ATOMIC_BUSINESS_WRITE_TRANSACTION_PREFIX + evidence.commit_id,
        ATOMIC_BUSINESS_WRITE_TRANSACTION_KIND,
        evidence.committed_at,
    )
    receipt_sha256 = _atomic_receipt_digest(
        commit_id=evidence.commit_id,
        receipt_parent=parent,
        atomic_manifest_sha256=evidence.atomic_manifest_sha256,
        pipeline_intents_sha256=evidence.pipeline_intents_sha256,
        controls_sha256=evidence.controls_sha256,
        control_count=len(evidence.controls),
        before_worker_ledger=evidence.before_worker_ledger,
        after_worker_ledger=evidence.after_worker_ledger,
        manifest_sha256=evidence.manifest_sha256,
    )
    return AtomicBusinessWriteReceiptEvidence(
        commit_id=evidence.commit_id,
        receipt_parent=parent,
        atomic_manifest_sha256=evidence.atomic_manifest_sha256,
        pipeline_intents_sha256=evidence.pipeline_intents_sha256,
        controls_sha256=evidence.controls_sha256,
        control_count=len(evidence.controls),
        before_worker_ledger=evidence.before_worker_ledger,
        after_worker_ledger=evidence.after_worker_ledger,
        manifest_sha256=evidence.manifest_sha256,
        committed_at=evidence.committed_at,
        receipt_sha256=receipt_sha256,
    )


class _PreparedWrite(NamedTuple):
    atomic_plan: AtomicCommitPlan
    control_plans: Tuple[PipelineBusinessControlPlan, ...]
    evidence: AtomicBusinessWriteEvidence
    expected_receipt: AtomicBusinessWriteReceiptEvidence


def _prepare(
    atomic_plan: Any,
    control_plans: Any,
    committed_at: Any,
    before_worker_ledger: Any,
) -> _PreparedWrite:
    try:
        atomic = _require_atomic_commit_plan(atomic_plan)
    except (BusinessTransactionIntegrityError, TypeError):
        _fail("atomic_business_write_atomic_plan_invalid")
    if (
        type(control_plans) is not tuple
        or len(control_plans) != len(atomic.pipeline_intents)
        or len(control_plans) > MAX_ATOMIC_BUSINESS_CONTROL_BINDINGS
    ):
        _fail("atomic_business_write_control_count_invalid")
    if type(before_worker_ledger) is not WorkerLedgerHead:
        _fail("atomic_business_write_worker_ledger_invalid")
    try:
        exact_before_worker_ledger = WorkerLedgerHead(
            before_worker_ledger.sequence,
            before_worker_ledger.tail_sha256,
        )
    except (AttributeError, TypeError, WorkerExecutionContractError):
        _fail("atomic_business_write_worker_ledger_invalid")
    if exact_before_worker_ledger != before_worker_ledger:
        _fail("atomic_business_write_worker_ledger_invalid")

    bindings = []
    previous = exact_before_worker_ledger
    exact_controls = []
    for intent, raw_control in zip(atomic.pipeline_intents, control_plans):
        try:
            control = _require_pipeline_business_control_plan(raw_control)
        except (WorkerExecutionIntegrityError, TypeError):
            _fail("atomic_business_write_control_plan_invalid")
        if control.before_ledger != previous:
            _fail("atomic_business_write_worker_ledger_invalid")
        binding = _require_control_mapping(
            atomic,
            intent,
            control,
            committed_at,
        )
        bindings.append(binding)
        exact_controls.append(control)
        previous = control.after_ledger
    bindings_tuple = tuple(bindings)
    controls_sha256 = _digest(
        ATOMIC_BUSINESS_WRITE_CONTROLS_HASH_DOMAIN,
        tuple(
            value.request_sha256
            + value.after_state_sha256
            + value.receipt_sha256
            + value.receipt_sequence.to_bytes(8, "big")
            for value in bindings_tuple
        ),
    )
    manifest_bytes = _build_manifest(
        atomic,
        committed_at,
        bindings_tuple,
        controls_sha256,
        exact_before_worker_ledger,
        previous,
    )
    try:
        evidence = AtomicBusinessWriteEvidence(
            commit_id=atomic.commit_id,
            committed_at=committed_at,
            atomic_manifest_sha256=atomic.closure.manifest_sha256,
            pipeline_intents_sha256=atomic.closure.pipeline_intents_sha256,
            controls=bindings_tuple,
            controls_sha256=controls_sha256,
            before_worker_ledger=exact_before_worker_ledger,
            after_worker_ledger=previous,
            manifest_bytes=manifest_bytes,
            manifest_sha256=_manifest_digest(manifest_bytes),
        )
    except (AtomicBusinessWriteContractError, TypeError) as exc:
        raise AtomicBusinessWriteIntegrityError(
            "atomic_business_write_evidence_invalid"
        ) from exc
    expected_receipt = _build_atomic_receipt(evidence)
    return _PreparedWrite(
        atomic,
        tuple(exact_controls),
        evidence,
        expected_receipt,
    )


class AtomicBusinessWritePlan:
    __slots__ = (
        "atomic_plan",
        "control_plans",
        "evidence",
        "expected_receipt",
        "protocol",
        "status",
        "_committed_at",
        "_before_worker_ledger",
        "_issuer",
    )

    def __init__(
        self,
        prepared: _PreparedWrite,
        *,
        committed_at: str,
        before_worker_ledger: WorkerLedgerHead,
        _issuer: Any,
    ) -> None:
        if _issuer is not _PLAN_ISSUER:
            _fail("atomic_business_write_plan_issuer_invalid")
        object.__setattr__(self, "atomic_plan", prepared.atomic_plan)
        object.__setattr__(self, "control_plans", prepared.control_plans)
        object.__setattr__(self, "evidence", prepared.evidence)
        object.__setattr__(self, "expected_receipt", prepared.expected_receipt)
        object.__setattr__(self, "protocol", ATOMIC_BUSINESS_WRITE_PROTOCOL)
        object.__setattr__(self, "status", ATOMIC_BUSINESS_WRITE_PLAN_STATUS)
        object.__setattr__(self, "_committed_at", committed_at)
        object.__setattr__(self, "_before_worker_ledger", before_worker_ledger)
        object.__setattr__(self, "_issuer", _issuer)

    def __setattr__(self, name: str, value: Any) -> None:
        del name, value
        raise AttributeError("AtomicBusinessWritePlan is immutable")


def _issue_atomic_business_write_plan(
    *,
    atomic_plan: AtomicCommitPlan,
    control_plans: Tuple[PipelineBusinessControlPlan, ...],
    committed_at: str,
    before_worker_ledger: WorkerLedgerHead = EMPTY_WORKER_LEDGER_HEAD,
) -> AtomicBusinessWritePlan:
    prepared = _prepare(
        atomic_plan,
        control_plans,
        committed_at,
        before_worker_ledger,
    )
    return AtomicBusinessWritePlan(
        prepared,
        committed_at=committed_at,
        before_worker_ledger=prepared.evidence.before_worker_ledger,
        _issuer=_PLAN_ISSUER,
    )


def _require_atomic_business_write_plan(value: Any) -> AtomicBusinessWritePlan:
    if (
        type(value) is not AtomicBusinessWritePlan
        or getattr(value, "_issuer", None) is not _PLAN_ISSUER
        or getattr(value, "protocol", None) != ATOMIC_BUSINESS_WRITE_PROTOCOL
        or getattr(value, "status", None) != ATOMIC_BUSINESS_WRITE_PLAN_STATUS
    ):
        _fail("atomic_business_write_plan_invalid")
    prepared = _prepare(
        value.atomic_plan,
        value.control_plans,
        value._committed_at,
        value._before_worker_ledger,
    )
    if (
        prepared.atomic_plan != value.atomic_plan
        or prepared.control_plans != value.control_plans
        or prepared.evidence != value.evidence
        or prepared.expected_receipt != value.expected_receipt
        or not hmac.compare_digest(
            prepared.evidence.manifest_sha256,
            value.evidence.manifest_sha256,
        )
    ):
        _fail("atomic_business_write_plan_changed")
    return value


def _canonical_atomic_generic_receipt(
    value: Any,
) -> AtomicBusinessWriteGenericReceiptEvidence:
    return AtomicBusinessWriteGenericReceiptEvidence(
        transaction_id=value.transaction_id,
        kind=value.kind,
        committed_at=value.committed_at,
    )


def _canonical_worker_ledger(value: Any) -> WorkerLedgerHead:
    return WorkerLedgerHead(
        sequence=value.sequence,
        tail_sha256=value.tail_sha256,
    )


def _canonical_worker_membership(value: Any) -> Any:
    if value is None:
        return None
    return WorkerLedgerMembershipEvidence(
        receipt_sequence=value.receipt_sequence,
        receipt_tail_sha256=value.receipt_tail_sha256,
        observed_head=_canonical_worker_ledger(value.observed_head),
    )


def _rebuild_atomic_receipt(
    value: Any,
) -> AtomicBusinessWriteReceiptEvidence:
    if type(value) is not AtomicBusinessWriteReceiptEvidence:
        _fail("atomic_business_write_receipt_type_invalid")
    try:
        parent = _canonical_atomic_generic_receipt(value.receipt_parent)
        before = _canonical_worker_ledger(value.before_worker_ledger)
        after = _canonical_worker_ledger(value.after_worker_ledger)
        receipt_sha256 = _atomic_receipt_digest(
            commit_id=value.commit_id,
            receipt_parent=parent,
            atomic_manifest_sha256=value.atomic_manifest_sha256,
            pipeline_intents_sha256=value.pipeline_intents_sha256,
            controls_sha256=value.controls_sha256,
            control_count=value.control_count,
            before_worker_ledger=before,
            after_worker_ledger=after,
            manifest_sha256=value.manifest_sha256,
        )
        rebuilt = AtomicBusinessWriteReceiptEvidence(
            commit_id=value.commit_id,
            receipt_parent=parent,
            atomic_manifest_sha256=value.atomic_manifest_sha256,
            pipeline_intents_sha256=value.pipeline_intents_sha256,
            controls_sha256=value.controls_sha256,
            control_count=value.control_count,
            before_worker_ledger=before,
            after_worker_ledger=after,
            manifest_sha256=value.manifest_sha256,
            committed_at=value.committed_at,
            receipt_sha256=receipt_sha256,
        )
    except (
        AttributeError,
        AtomicBusinessWriteContractError,
        AtomicBusinessWriteIntegrityError,
        TypeError,
        WorkerExecutionContractError,
    ):
        _fail("atomic_business_write_receipt_changed")
    if (
        rebuilt != value
        or not hmac.compare_digest(
            rebuilt.receipt_sha256,
            value.receipt_sha256,
        )
    ):
        _fail("atomic_business_write_receipt_changed")
    return rebuilt


def _classification(
    status: AtomicBusinessWriteReconcileStatus,
    plan: AtomicBusinessWritePlan,
    *,
    evidence: Optional[AtomicBusinessWriteReceiptEvidence] = None,
    error_code: str = "",
) -> AtomicBusinessWriteClassification:
    return AtomicBusinessWriteClassification(
        status=status,
        commit_id=plan.evidence.commit_id,
        evidence=evidence,
        error_code=error_code,
    )


def _classify_atomic_business_write_receipt(
    plan: AtomicBusinessWritePlan,
    fresh: AtomicBusinessWriteFreshObservation,
) -> AtomicBusinessWriteClassification:
    """Classify one capability-stamped atomic/control/ledger snapshot."""

    plan = _require_atomic_business_write_plan(plan)
    try:
        raw = _unwrap_atomic_business_write_fresh_observation(fresh)
        canonical = AtomicBusinessWritePersistedObservation(
            read_status=raw.read_status,
            receipt=(
                None
                if raw.receipt is None
                else _canonical_atomic_generic_receipt(raw.receipt)
            ),
            atomic_receipt=raw.atomic_receipt,
            control_receipts=raw.control_receipts,
            current_worker_ledger=(
                None
                if raw.current_worker_ledger is None
                else _canonical_worker_ledger(raw.current_worker_ledger)
            ),
            ledger_membership=_canonical_worker_membership(
                raw.ledger_membership
            ),
            error_code=raw.error_code,
        )
    except (
        AttributeError,
        AtomicBusinessWriteContractError,
        TypeError,
        WorkerExecutionContractError,
    ):
        _fail("atomic_business_write_fresh_observation_invalid")
    if canonical != raw:
        _fail("atomic_business_write_persisted_observation_changed")
    if canonical.read_status is AtomicBusinessWritePersistedReadStatus.UNKNOWN:
        return _classification(
            AtomicBusinessWriteReconcileStatus.UNKNOWN,
            plan,
            error_code=canonical.error_code,
        )

    expected = plan.expected_receipt
    receipt = canonical.receipt
    atomic_receipt = canonical.atomic_receipt
    controls = canonical.control_receipts
    current = canonical.current_worker_ledger
    membership = canonical.ledger_membership
    assert current is not None

    if receipt is None and atomic_receipt is None:
        if controls or membership is not None:
            return _classification(
                AtomicBusinessWriteReconcileStatus.CORRUPT,
                plan,
                error_code="atomic_business_write_orphan_control_receipt",
            )
        if current == plan.evidence.before_worker_ledger:
            return _classification(
                AtomicBusinessWriteReconcileStatus.ABSENT,
                plan,
            )
        return _classification(
            AtomicBusinessWriteReconcileStatus.CONFLICT,
            plan,
            error_code="atomic_business_write_absent_ledger_conflict",
        )

    if receipt is None or atomic_receipt is None:
        identity_conflict = bool(
            receipt is not None
            and receipt.transaction_id == expected.receipt_parent.transaction_id
            and receipt.kind != expected.receipt_parent.kind
        )
        return _classification(
            (
                AtomicBusinessWriteReconcileStatus.CONFLICT
                if identity_conflict
                else AtomicBusinessWriteReconcileStatus.CORRUPT
            ),
            plan,
            error_code=(
                "atomic_business_write_receipt_identity_conflict"
                if identity_conflict
                else "atomic_business_write_receipt_pair_incomplete"
            ),
        )

    try:
        observed = _rebuild_atomic_receipt(atomic_receipt)
    except AtomicBusinessWriteIntegrityError:
        return _classification(
            AtomicBusinessWriteReconcileStatus.CORRUPT,
            plan,
            error_code="atomic_business_write_receipt_corrupt",
        )
    if receipt != observed.receipt_parent:
        identity_conflict = bool(
            receipt.transaction_id == expected.receipt_parent.transaction_id
            and receipt.kind != expected.receipt_parent.kind
        )
        return _classification(
            (
                AtomicBusinessWriteReconcileStatus.CONFLICT
                if identity_conflict
                else AtomicBusinessWriteReconcileStatus.CORRUPT
            ),
            plan,
            error_code=(
                "atomic_business_write_receipt_identity_conflict"
                if identity_conflict
                else "atomic_business_write_receipt_parent_mismatch"
            ),
        )
    if (
        observed.commit_id != expected.commit_id
        or receipt.transaction_id != expected.receipt_parent.transaction_id
    ):
        return _classification(
            AtomicBusinessWriteReconcileStatus.CONFLICT,
            plan,
            error_code="atomic_business_write_identity_conflict",
        )
    if observed != expected:
        return _classification(
            AtomicBusinessWriteReconcileStatus.CONFLICT,
            plan,
            error_code="atomic_business_write_content_conflict",
        )

    expected_controls = tuple(
        value.expected_receipt for value in plan.control_plans
    )
    if len(controls) != len(expected_controls):
        return _classification(
            AtomicBusinessWriteReconcileStatus.CORRUPT,
            plan,
            error_code="atomic_business_write_control_count_corrupt",
        )
    rebuilt_controls = []
    for value in controls:
        try:
            rebuilt_controls.append(_rebuild_control_receipt(value))
        except WorkerExecutionIntegrityError:
            return _classification(
                AtomicBusinessWriteReconcileStatus.CORRUPT,
                plan,
                error_code="atomic_business_write_control_receipt_corrupt",
            )
    rebuilt_controls_tuple = tuple(rebuilt_controls)
    observed_sequences = tuple(value.sequence for value in rebuilt_controls_tuple)
    exact_sequences = tuple(range(
        expected.before_worker_ledger.sequence + 1,
        expected.after_worker_ledger.sequence + 1,
    ))
    if observed_sequences != exact_sequences:
        return _classification(
            AtomicBusinessWriteReconcileStatus.CORRUPT,
            plan,
            error_code="atomic_business_write_control_sequence_corrupt",
        )
    previous_tail = expected.before_worker_ledger.tail_sha256
    for value in rebuilt_controls_tuple:
        if not hmac.compare_digest(
            value.previous_ledger_tail_sha256,
            previous_tail,
        ):
            return _classification(
                AtomicBusinessWriteReconcileStatus.CORRUPT,
                plan,
                error_code="atomic_business_write_control_chain_corrupt",
            )
        previous_tail = value.ledger_tail_sha256
    if rebuilt_controls_tuple != expected_controls:
        return _classification(
            AtomicBusinessWriteReconcileStatus.CONFLICT,
            plan,
            error_code="atomic_business_write_control_content_conflict",
        )

    after = expected.after_worker_ledger
    if current == after:
        if membership is not None:
            return _classification(
                AtomicBusinessWriteReconcileStatus.CORRUPT,
                plan,
                error_code="atomic_business_write_membership_unexpected",
            )
    elif (
        current.sequence > after.sequence
        and after == EMPTY_WORKER_LEDGER_HEAD
        and membership is None
    ):
        # The schema fixes sequence zero to the unique empty-ledger digest, so
        # every valid non-empty head descends from this genesis implicitly.
        pass
    elif current.sequence > after.sequence:
        try:
            expected_membership = WorkerLedgerMembershipEvidence(
                receipt_sequence=after.sequence,
                receipt_tail_sha256=after.tail_sha256,
                observed_head=current,
            )
        except WorkerExecutionContractError:
            return _classification(
                AtomicBusinessWriteReconcileStatus.CORRUPT,
                plan,
                error_code="atomic_business_write_ledger_membership_invalid",
            )
        if membership != expected_membership:
            return _classification(
                AtomicBusinessWriteReconcileStatus.CORRUPT,
                plan,
                error_code="atomic_business_write_ledger_membership_invalid",
            )
    else:
        return _classification(
            AtomicBusinessWriteReconcileStatus.CORRUPT,
            plan,
            error_code="atomic_business_write_ledger_regressed",
        )

    return _classification(
        AtomicBusinessWriteReconcileStatus.EXACT,
        plan,
        evidence=observed,
    )


__all__ = ()
