"""Pure issuer for effect-request and terminal worker payload projections.

This module never validates a worker transition itself.  Its sole authority is
an issuer-sealed ``WorkerExecutionPlan`` rebuilt by
``_require_worker_execution_plan``.  The projection issuer copies the exact
nested payload bytes from that plan, frames them with their schema/codec/hash
metadata, and binds the result to the plan's generic and specialised receipt.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any, NamedTuple, Optional, Sequence

from business_codec import BusinessCodecError, encode_payload_bounded
from business_transaction_contract import FrozenJsonPayload
from worker_execution_contract import (
    WorkerAuthorizeEffectRequest,
    WorkerCompleteRequest,
    WorkerDurableResultProof,
    WorkerEffectBinding,
    WorkerEffectLink,
    WorkerExecutionOperation,
    WorkerReconcileProofKind,
    WorkerReconcileRequest,
    WorkerTargetKind,
    WorkerTerminalProof,
)
from worker_execution_integrity import (
    WorkerExecutionIntegrityError,
    WorkerExecutionPlan,
    _require_worker_execution_plan,
)
from worker_result_projection_contract import (
    EMPTY_WORKER_PROOF_PROJECTION_SHA256,
    MAX_WORKER_PROJECTION_COMPONENT_BYTES,
    MAX_WORKER_PROJECTION_MANIFEST_BYTES,
    MAX_WORKER_PROJECTION_MANIFEST_NODES,
    MAX_WORKER_PROJECTION_NESTED_PAYLOAD_BYTES,
    WORKER_EFFECT_REQUEST_PROJECTION_SCHEMA,
    WORKER_PROOF_PAYLOAD_PROJECTION_SCHEMA,
    WORKER_RESULT_PAYLOAD_PROJECTION_SCHEMA,
    WORKER_RESULT_PROJECTION_MANIFEST_SCHEMA,
    WORKER_RESULT_PROJECTION_PROTOCOL,
    WORKER_RESULT_PROJECTION_STATUS_PREPARED,
    WorkerEffectRequestProjection,
    WorkerProofProjection,
    WorkerProofProjectionClass,
    WorkerProjectionReceiptBinding,
    WorkerResultProjection,
    WorkerResultProjectionClosure,
    WorkerResultProjectionContractError,
)


WORKER_EFFECT_REQUEST_PROJECTION_HASH_DOMAIN = (
    b"ze.worker-effect-request-projection.digest.v1"
)
WORKER_RESULT_PAYLOAD_PROJECTION_HASH_DOMAIN = (
    b"ze.worker-result-payload-projection.digest.v1"
)
WORKER_PROOF_PAYLOAD_PROJECTION_HASH_DOMAIN = (
    b"ze.worker-proof-payload-projection.digest.v1"
)
WORKER_RESULT_PROJECTION_MANIFEST_HASH_DOMAIN = (
    b"ze.worker-result-projection.manifest-digest.v1"
)
WORKER_RESULT_PROJECTION_ID_HASH_DOMAIN = (
    b"ze.worker-result-projection.result-id.v1"
)
WORKER_PROOF_PROJECTION_ID_HASH_DOMAIN = (
    b"ze.worker-result-projection.proof-id.v1"
)
WORKER_EMPTY_PROOF_PROJECTION_HASH_DOMAIN = (
    b"ze.worker-proof-projection.empty-v8-result-mirror.v1"
)

_PLAN_ISSUER = object()


class WorkerResultProjectionIntegrityError(ValueError):
    """Stable, payload-free projection integrity failure."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        if type(code) is not str or not code:
            raise TypeError("code must be a non-empty exact string")
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise WorkerResultProjectionIntegrityError(code)


class _PreparedProjection(NamedTuple):
    worker_plan: WorkerExecutionPlan
    binding: WorkerProjectionReceiptBinding
    closure: WorkerResultProjectionClosure


class WorkerResultProjectionPlan:
    """Issuer-stamped immutable projection of one sealed worker plan."""

    __slots__ = (
        "worker_plan",
        "binding",
        "closure",
        "protocol",
        "status",
        "_issuer",
    )

    def __init__(self, prepared: _PreparedProjection, *, _issuer: Any) -> None:
        if _issuer is not _PLAN_ISSUER:
            _fail("worker_result_projection_plan_issuer_invalid")
        object.__setattr__(self, "worker_plan", prepared.worker_plan)
        object.__setattr__(self, "binding", prepared.binding)
        object.__setattr__(self, "closure", prepared.closure)
        object.__setattr__(self, "protocol", WORKER_RESULT_PROJECTION_PROTOCOL)
        object.__setattr__(
            self,
            "status",
            WORKER_RESULT_PROJECTION_STATUS_PREPARED,
        )
        object.__setattr__(self, "_issuer", _issuer)

    def __setattr__(self, name: str, value: Any) -> None:
        del name, value
        raise AttributeError("WorkerResultProjectionPlan is immutable")


def _update_frame(hasher: Any, value: bytes) -> None:
    """Stream one length-prefixed value without copying its payload bytes."""

    hasher.update(len(value).to_bytes(8, "big"))
    hasher.update(value)


def _domain_digest(domain: bytes, *parts: bytes) -> bytes:
    hasher = hashlib.sha256()
    for value in (domain, *parts):
        _update_frame(hasher, value)
    return hasher.digest()


def _identity(domain: bytes, *values: str) -> str:
    return _domain_digest(
        domain,
        *(value.encode("utf-8", "strict") for value in values),
    )[:16].hex()


# V9 keeps an absent proof nullable.  This non-zero, domain-separated value is
# exclusively the compatibility value for V8 worker_results.proof_sha256,
# whose frozen schema cannot store NULL.  It is never a WorkerProofProjection.
_EXPECTED_EMPTY_WORKER_PROOF_PROJECTION_SHA256 = _domain_digest(
    WORKER_EMPTY_PROOF_PROJECTION_HASH_DOMAIN,
    WORKER_RESULT_PROJECTION_PROTOCOL.encode("ascii"),
    b"v8.worker_results.proof_sha256",
    b"no-proof",
)
if not hmac.compare_digest(
    EMPTY_WORKER_PROOF_PROJECTION_SHA256,
    _EXPECTED_EMPTY_WORKER_PROOF_PROJECTION_SHA256,
):
    raise RuntimeError("empty worker proof projection vector drifted")


def _encode_manifest(value: Any, code: str) -> bytes:
    try:
        return encode_payload_bounded(
            value,
            MAX_WORKER_PROJECTION_MANIFEST_BYTES,
            MAX_WORKER_PROJECTION_MANIFEST_NODES,
        )
    except BusinessCodecError:
        _fail(code)


def _payload_manifest(value: Optional[FrozenJsonPayload]) -> Any:
    if value is None:
        return None
    return {
        "schema": value.schema,
        "codec_id": value.codec_id,
        "codec_version": value.codec_version,
        "bytes": len(value.payload_bytes),
        "sha256": value.payload_sha256.hex(),
    }


def _effect_manifest(value: Optional[WorkerEffectBinding]) -> Any:
    if value is None:
        return None
    return {
        "attempt_id": value.attempt_id,
        "attempt_number": value.attempt_number,
        "effect_id": value.effect_id,
        "effect_kind": value.effect_kind.value,
        "request_sha256": value.request_sha256.hex(),
        "idempotency_scope": value.idempotency_scope,
        "idempotency_key": value.idempotency_key,
    }


def _receipt_manifest(value: WorkerProjectionReceiptBinding) -> dict[str, Any]:
    receipt = value.execution_receipt
    parent = value.receipt_parent
    return {
        "generic": {
            "transaction_id": parent.transaction_id,
            "kind": parent.kind,
            "committed_at": parent.committed_at,
        },
        "specialised": {
            "request_transaction_id": receipt.request_transaction_id,
            "request_class": receipt.request_class,
            "operation": receipt.operation.value,
            "target_kind": receipt.target_kind.value,
            "target_id": receipt.target_id,
            "before_status": receipt.before_status.value,
            "after_status": receipt.after_status.value,
            "before_state_revision": receipt.before_state_revision,
            "after_state_revision": receipt.after_state_revision,
            "sequence": receipt.sequence,
            "previous_ledger_tail_sha256": (
                receipt.previous_ledger_tail_sha256.hex()
            ),
            "ledger_tail_sha256": receipt.ledger_tail_sha256.hex(),
            "request_sha256": receipt.request_sha256.hex(),
            "after_state_sha256": receipt.after_state_sha256.hex(),
            "occurred_at": receipt.occurred_at,
            "receipt_sha256": receipt.receipt_sha256.hex(),
        },
        "target": {
            "kind": value.target_kind.value,
            "id": value.target_id,
        },
        "attempt": {
            "id": value.attempt_id,
            "number": value.attempt_number,
        },
        "effect": _effect_manifest(value.effect),
    }


def _projection_metadata_bytes(
    header: dict[str, Any],
    *,
    code: str,
) -> bytes:
    """Bounded canonical metadata; nested payload bytes remain single-owned."""

    return _encode_manifest(header, code)


def _projection_digest(
    domain: bytes,
    metadata_bytes: bytes,
    payloads: Sequence[tuple[str, Optional[FrozenJsonPayload]]],
) -> bytes:
    """Hash metadata and exact role-labelled payload five-tuples by framing."""

    total_payload_bytes = sum(
        len(payload.payload_bytes)
        for _role, payload in payloads
        if payload is not None
    )
    if total_payload_bytes > MAX_WORKER_PROJECTION_NESTED_PAYLOAD_BYTES:
        _fail("worker_result_projection_nested_payload_budget_exceeded")
    hasher = hashlib.sha256()
    for value in (domain, metadata_bytes):
        _update_frame(hasher, value)
    for role, payload in payloads:
        _update_frame(hasher, role.encode("ascii"))
        if payload is None:
            _update_frame(hasher, b"")
            continue
        for value in (
            payload.schema.encode("utf-8", "strict"),
            payload.codec_id.encode("ascii"),
            payload.codec_version.to_bytes(8, "big"),
            payload.payload_bytes,
            payload.payload_sha256,
        ):
            _update_frame(hasher, value)
    return hasher.digest()


def _effect_link_manifest(value: Optional[WorkerEffectLink]) -> Any:
    if value is None:
        return None
    return {
        "attempt_id": value.attempt_id,
        "attempt_number": value.attempt_number,
        "effect_id": value.effect_id,
        "effect_kind": value.effect_kind.value,
        "request_sha256": value.request_sha256.hex(),
        "result_commit_id": value.result_commit_id,
        "provider_receipt": _payload_manifest(value.provider_receipt),
    }


def _durable_result_manifest(value: Optional[WorkerDurableResultProof]) -> Any:
    if value is None:
        return None
    return {
        "result_id": value.result_id,
        "recorded_at": value.recorded_at,
        "effect": _effect_manifest(value.effect),
        "result": _payload_manifest(value.result),
    }


def _terminal_proof_manifest(value: Optional[WorkerTerminalProof]) -> Any:
    if value is None:
        return None
    return {
        "proof_kind": value.proof_kind.value,
        "effect": _effect_manifest(value.effect),
        "evidence": _payload_manifest(value.evidence),
        "effect_link": _effect_link_manifest(value.effect_link),
    }


def _binding_from_plan(plan: WorkerExecutionPlan) -> WorkerProjectionReceiptBinding:
    request = plan.request
    operation = plan.operation
    if operation in (
        WorkerExecutionOperation.AUTHORIZE_EFFECT,
        WorkerExecutionOperation.COMPLETE,
    ):
        attempt_id = request.lease.attempt_id
        attempt_number = request.lease.attempt_number
    elif operation is WorkerExecutionOperation.RECONCILE:
        attempt_id = request.attempt_id
        attempt_number = request.attempt_number
    else:
        _fail("worker_result_projection_not_applicable")

    if operation is WorkerExecutionOperation.AUTHORIZE_EFFECT:
        effect = plan.closure.after_state.current_effect
    elif operation is WorkerExecutionOperation.COMPLETE:
        effect = plan.before.current_effect
    else:
        effect = plan.before.unresolved_effect
    return WorkerProjectionReceiptBinding(
        receipt_parent=plan.expected_receipt.receipt_parent,
        execution_receipt=plan.expected_receipt,
        target_kind=plan.target.target_kind,
        target_id=plan.target.target_id,
        attempt_id=attempt_id,
        attempt_number=attempt_number,
        effect=effect,
    )


def _effect_projection(
    plan: WorkerExecutionPlan,
    binding: WorkerProjectionReceiptBinding,
) -> WorkerEffectRequestProjection:
    request = plan.request
    if type(request) is not WorkerAuthorizeEffectRequest or binding.effect is None:
        _fail("worker_effect_request_projection_source_invalid")
    header = {
        "schema": WORKER_EFFECT_REQUEST_PROJECTION_SCHEMA,
        "protocol": WORKER_RESULT_PROJECTION_PROTOCOL,
        "binding": _receipt_manifest(binding),
        "idempotency_scope": request.effect.idempotency_scope,
        "idempotency_key": request.effect.idempotency_key,
        "request_payload": _payload_manifest(request.effect.request),
    }
    projection_manifest_bytes = _projection_metadata_bytes(
        header,
        code="worker_effect_request_projection_encoding_invalid",
    )
    return WorkerEffectRequestProjection(
        binding=binding,
        idempotency_scope=request.effect.idempotency_scope,
        idempotency_key=request.effect.idempotency_key,
        request_payload=request.effect.request,
        projection_manifest_bytes=projection_manifest_bytes,
        projection_sha256=_projection_digest(
            WORKER_EFFECT_REQUEST_PROJECTION_HASH_DOMAIN,
            projection_manifest_bytes,
            (("request_payload", request.effect.request),),
        ),
    )


def _result_sources(plan: WorkerExecutionPlan) -> tuple[
    Optional[FrozenJsonPayload],
    Optional[FrozenJsonPayload],
    Optional[WorkerEffectLink],
    Optional[WorkerDurableResultProof],
]:
    request = plan.request
    if type(request) is WorkerCompleteRequest:
        link = (
            request.terminal_proof.effect_link
            if request.terminal_proof is not None
            else request.effect_link
        )
        return request.result, request.error, link, request.durable_result
    if type(request) is WorkerReconcileRequest:
        return (
            None,
            None,
            request.proof.effect_link,
            request.proof.durable_result,
        )
    _fail("worker_result_projection_source_invalid")


def _projection_result_id(
    plan: WorkerExecutionPlan,
    durable_result: Optional[WorkerDurableResultProof],
) -> str:
    if durable_result is not None:
        return durable_result.result_id
    return _identity(
        WORKER_RESULT_PROJECTION_ID_HASH_DOMAIN,
        plan.closure.request_transaction_id,
        plan.target.target_id,
        plan.operation.value,
        plan.closure.after_status.value,
    )


def _proof_sources(plan: WorkerExecutionPlan) -> tuple[
    WorkerProofProjectionClass,
    str,
    Optional[FrozenJsonPayload],
    Optional[WorkerEffectLink],
    Optional[WorkerDurableResultProof],
]:
    request = plan.request
    if type(request) is WorkerReconcileRequest:
        proof = request.proof
        return (
            WorkerProofProjectionClass.RECONCILE,
            proof.proof_kind.value,
            proof.evidence,
            proof.effect_link,
            proof.durable_result,
        )
    if type(request) is not WorkerCompleteRequest:
        _fail("worker_proof_projection_source_invalid")
    if request.terminal_proof is not None:
        proof = request.terminal_proof
        return (
            WorkerProofProjectionClass.COMPLETE,
            proof.proof_kind.value,
            proof.evidence,
            proof.effect_link,
            request.durable_result,
        )
    if request.durable_result is not None:
        return (
            WorkerProofProjectionClass.COMPLETE,
            WorkerReconcileProofKind.DURABLE_RESULT.value,
            None,
            None,
            request.durable_result,
        )
    if request.effect_link is not None:
        kind = (
            WorkerReconcileProofKind.RESULT_COMMIT
            if plan.target.target_kind is WorkerTargetKind.PIPELINE
            else WorkerReconcileProofKind.PROVIDER_RECEIPT
        )
        return (
            WorkerProofProjectionClass.COMPLETE,
            kind.value,
            None,
            request.effect_link,
            None,
        )
    _fail("worker_proof_projection_not_applicable")


def _proof_projection(
    plan: WorkerExecutionPlan,
    binding: WorkerProjectionReceiptBinding,
    result_id: str,
) -> Optional[WorkerProofProjection]:
    request = plan.request
    required = type(request) is WorkerReconcileRequest or bool(
        type(request) is WorkerCompleteRequest
        and (
            request.effect_link is not None
            or request.durable_result is not None
            or request.terminal_proof is not None
        )
    )
    if not required:
        return None
    proof_class, proof_kind, evidence, link, durable = _proof_sources(plan)
    provider = None if link is None else link.provider_receipt
    proof_id = _identity(
        WORKER_PROOF_PROJECTION_ID_HASH_DOMAIN,
        plan.closure.request_transaction_id,
        plan.target.target_id,
        plan.operation.value,
        plan.closure.after_status.value,
    )
    header = {
        "schema": WORKER_PROOF_PAYLOAD_PROJECTION_SCHEMA,
        "protocol": WORKER_RESULT_PROJECTION_PROTOCOL,
        "binding": _receipt_manifest(binding),
        "result_id": result_id,
        "proof_id": proof_id,
        "proof_class": proof_class.value,
        "proof_kind": proof_kind,
        "observed_at": binding.receipt_parent.committed_at,
        "evidence_payload": _payload_manifest(evidence),
        "effect_link": _effect_link_manifest(link),
        "durable_result": _durable_result_manifest(durable),
    }
    proof_payloads = (
        ("evidence_payload", evidence),
        ("provider_receipt_payload", provider),
        (
            "durable_result_payload",
            None if durable is None else durable.result,
        ),
    )
    proof_manifest_bytes = _projection_metadata_bytes(
        header,
        code="worker_proof_projection_encoding_invalid",
    )
    proof_sha256 = _projection_digest(
        WORKER_PROOF_PAYLOAD_PROJECTION_HASH_DOMAIN,
        proof_manifest_bytes,
        proof_payloads,
    )
    return WorkerProofProjection(
        proof_id=proof_id,
        result_id=result_id,
        binding=binding,
        proof_class=proof_class,
        proof_kind=proof_kind,
        observed_at=binding.receipt_parent.committed_at,
        evidence_payload=evidence,
        provider_receipt_payload=provider,
        durable_result_id=(None if durable is None else durable.result_id),
        durable_result_payload=(None if durable is None else durable.result),
        durable_result_recorded_at=(
            None if durable is None else durable.recorded_at
        ),
        result_commit_id=(None if link is None else link.result_commit_id),
        projection_manifest_bytes=proof_manifest_bytes,
        proof_sha256=proof_sha256,
    )


def _result_projection(
    plan: WorkerExecutionPlan,
    binding: WorkerProjectionReceiptBinding,
) -> tuple[WorkerResultProjection, Optional[WorkerProofProjection]]:
    result_payload, error_payload, link, durable = _result_sources(plan)
    result_id = _projection_result_id(plan, durable)
    proof = _proof_projection(plan, binding, result_id)
    provider = None if link is None else link.provider_receipt
    header = {
        "schema": WORKER_RESULT_PAYLOAD_PROJECTION_SCHEMA,
        "protocol": WORKER_RESULT_PROJECTION_PROTOCOL,
        "binding": _receipt_manifest(binding),
        "result_id": result_id,
        "after_status": plan.closure.after_status.value,
        "committed_at": binding.receipt_parent.committed_at,
        "result_payload": _payload_manifest(result_payload),
        "error_payload": _payload_manifest(error_payload),
        "proof_sha256": (
            None if proof is None else proof.proof_sha256.hex()
        ),
    }
    result_payloads = (
        ("result_payload", result_payload),
        ("error_payload", error_payload),
    )
    result_manifest_bytes = _projection_metadata_bytes(
        header,
        code="worker_result_projection_encoding_invalid",
    )
    return WorkerResultProjection(
        result_id=result_id,
        binding=binding,
        after_status=plan.closure.after_status,
        committed_at=binding.receipt_parent.committed_at,
        result_payload=result_payload,
        error_payload=error_payload,
        projection_manifest_bytes=result_manifest_bytes,
        result_sha256=_projection_digest(
            WORKER_RESULT_PAYLOAD_PROJECTION_HASH_DOMAIN,
            result_manifest_bytes,
            result_payloads,
        ),
        proof_sha256=(None if proof is None else proof.proof_sha256),
    ), proof


def _closure_manifest(
    plan: WorkerExecutionPlan,
    binding: WorkerProjectionReceiptBinding,
    effect_request: Optional[WorkerEffectRequestProjection],
    result: Optional[WorkerResultProjection],
    proof: Optional[WorkerProofProjection],
) -> bytes:
    return _encode_manifest(
        {
            "schema": WORKER_RESULT_PROJECTION_MANIFEST_SCHEMA,
            "protocol": WORKER_RESULT_PROJECTION_PROTOCOL,
            "status": WORKER_RESULT_PROJECTION_STATUS_PREPARED,
            "source": {
                "request_sha256": plan.closure.request_sha256.hex(),
                "after_state_sha256": plan.closure.after_state_sha256.hex(),
                "receipt_sha256": plan.expected_receipt.receipt_sha256.hex(),
            },
            "binding": _receipt_manifest(binding),
            "effect_request": (
                None
                if effect_request is None
                else {
                    "projection_sha256": effect_request.projection_sha256.hex(),
                    "request_payload": _payload_manifest(
                        effect_request.request_payload
                    ),
                }
            ),
            "result": (
                None
                if result is None
                else {
                    "result_id": result.result_id,
                    "after_status": result.after_status.value,
                    "result_sha256": result.result_sha256.hex(),
                    "proof_sha256": (
                        None
                        if result.proof_sha256 is None
                        else result.proof_sha256.hex()
                    ),
                }
            ),
            "proof": (
                None
                if proof is None
                else {
                    "proof_id": proof.proof_id,
                    "proof_class": proof.proof_class.value,
                    "proof_kind": proof.proof_kind,
                    "proof_sha256": proof.proof_sha256.hex(),
                }
            ),
        },
        "worker_result_projection_manifest_invalid",
    )


def _prepare(value: Any) -> _PreparedProjection:
    try:
        plan = _require_worker_execution_plan(value)
    except WorkerExecutionIntegrityError:
        _fail("worker_result_projection_worker_plan_invalid")
    if type(plan) is not WorkerExecutionPlan:
        _fail("worker_result_projection_worker_plan_invalid")
    try:
        binding = _binding_from_plan(plan)
        effect_request = None
        result = None
        proof = None
        if plan.operation is WorkerExecutionOperation.AUTHORIZE_EFFECT:
            effect_request = _effect_projection(plan, binding)
        elif plan.operation in (
            WorkerExecutionOperation.COMPLETE,
            WorkerExecutionOperation.RECONCILE,
        ):
            result, proof = _result_projection(plan, binding)
        else:
            _fail("worker_result_projection_not_applicable")
        manifest_bytes = _closure_manifest(
            plan,
            binding,
            effect_request,
            result,
            proof,
        )
        closure = WorkerResultProjectionClosure(
            effect_request=effect_request,
            result=result,
            proof=proof,
            manifest_bytes=manifest_bytes,
            manifest_sha256=_domain_digest(
                WORKER_RESULT_PROJECTION_MANIFEST_HASH_DOMAIN,
                manifest_bytes,
            ),
        )
    except WorkerResultProjectionContractError:
        _fail("worker_result_projection_changed")
    return _PreparedProjection(plan, binding, closure)


def _issue_worker_result_projection_plan(
    worker_plan: WorkerExecutionPlan,
) -> WorkerResultProjectionPlan:
    """Project payload bytes only after the worker issuer seals the transition."""

    return WorkerResultProjectionPlan(
        _prepare(worker_plan),
        _issuer=_PLAN_ISSUER,
    )


def _require_worker_result_projection_plan(
    value: Any,
) -> WorkerResultProjectionPlan:
    """Rebuild every source plan, byte frame, identity and digest."""

    if (
        type(value) is not WorkerResultProjectionPlan
        or getattr(value, "_issuer", None) is not _PLAN_ISSUER
        or getattr(value, "protocol", None) != WORKER_RESULT_PROJECTION_PROTOCOL
        or getattr(value, "status", None)
        != WORKER_RESULT_PROJECTION_STATUS_PREPARED
    ):
        _fail("worker_result_projection_plan_invalid")
    prepared = _prepare(value.worker_plan)
    if (
        prepared.binding != value.binding
        or prepared.closure != value.closure
        or not hmac.compare_digest(
            prepared.closure.manifest_sha256,
            value.closure.manifest_sha256,
        )
    ):
        _fail("worker_result_projection_plan_changed")
    return value


def _v8_worker_result_mirror_proof_sha256(
    value: Any,
) -> bytes:
    """Return the frozen V8 NOT NULL mirror value for one result projection.

    The sealed V9 projection remains unchanged: ``result.proof_sha256`` and
    ``closure.proof`` stay ``None`` when the worker plan carried no proof.
    """

    plan = _require_worker_result_projection_plan(value)
    result = plan.closure.result
    if result is None:
        _fail("worker_result_projection_v8_mirror_result_required")
    if plan.closure.proof is None:
        if result.proof_sha256 is not None:
            _fail("worker_result_projection_plan_changed")
        return EMPTY_WORKER_PROOF_PROJECTION_SHA256
    if (
        result.proof_sha256 is None
        or not hmac.compare_digest(
            result.proof_sha256,
            plan.closure.proof.proof_sha256,
        )
    ):
        _fail("worker_result_projection_plan_changed")
    return plan.closure.proof.proof_sha256


__all__ = ()
