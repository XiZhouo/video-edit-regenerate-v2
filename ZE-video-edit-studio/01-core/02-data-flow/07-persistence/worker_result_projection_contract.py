"""Pure carriers for durable worker effect/result payload projections.

The worker state machine deliberately seals payload hashes, not the payload
bytes a future durable auditor needs for plan-free reconstruction.  These
carriers close that one gap without defining a second state machine.  They
contain only projections issued from an already sealed ``WorkerExecutionPlan``;
they perform no database, clock, provider, or scheduler I/O.

An effect authorization has one independent request-payload projection.  Every
COMPLETE and RECONCILE has one result projection.  RECONCILE always has a proof
projection; COMPLETE has one only when its sealed request actually contains an
effect link, durable result, or terminal proof.  EXPIRE has no projection here:
its worker receipt plus the earlier effect-request projection is the complete
durable source of truth.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from business_codec import (
    BusinessCodecError,
    decode_payload_bounded,
    encode_payload_bounded,
)
from business_transaction_contract import (
    BusinessTransactionContractError,
    FrozenJsonPayload,
    MAX_ATOMIC_JSON_PAYLOAD_BYTES,
    MAX_RETRY_ATTEMPTS,
    verify_frozen_json_payload,
)
from worker_execution_contract import (
    WorkerEffectBinding,
    WorkerExecutionGenericReceiptEvidence,
    WorkerExecutionOperation,
    WorkerExecutionReceiptEvidence,
    WorkerExecutionStatus,
    WorkerTargetKind,
)


WORKER_RESULT_PROJECTION_PROTOCOL = "ze.worker-result-projection.v1"
WORKER_RESULT_PROJECTION_MANIFEST_SCHEMA = (
    "ze.worker-result-projection.manifest.v1"
)
WORKER_EFFECT_REQUEST_PROJECTION_SCHEMA = (
    "ze.worker-effect-request-projection.v1"
)
WORKER_RESULT_PAYLOAD_PROJECTION_SCHEMA = (
    "ze.worker-result-payload-projection.v1"
)
WORKER_PROOF_PAYLOAD_PROJECTION_SCHEMA = (
    "ze.worker-proof-payload-projection.v1"
)
WORKER_RESULT_PROJECTION_STATUS_PREPARED = "prepared"
EMPTY_WORKER_PROOF_PROJECTION_SHA256 = bytes.fromhex(
    "f14fd90bc57f6d54c068aff36091f91a"
    "2853b2b4ffa48c148a3a46d8854a3a47"
)

# The raw nested JSON payloads retain the existing atomic payload budget, but
# each remains single-owned by its FrozenJsonPayload carrier.  Aggregate bytes
# are metadata only; their digest streams over the exact payload five-tuples.
MAX_WORKER_PROJECTION_NESTED_PAYLOAD_BYTES = 4 * MAX_ATOMIC_JSON_PAYLOAD_BYTES
MAX_WORKER_PROJECTION_COMPONENT_BYTES = 4 * 1024 * 1024
MAX_WORKER_PROJECTION_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_WORKER_PROJECTION_MANIFEST_NODES = 100_000
MAX_WORKER_PROJECTION_TEXT_BYTES = 1024 * 1024

_LOWER_HEX = frozenset("0123456789abcdef")


class WorkerResultProjectionContractError(ValueError):
    """Stable, payload-free projection contract failure."""

    __slots__ = ("code", "path")

    def __init__(self, code: str, path: str) -> None:
        if type(code) is not str or not code:
            raise TypeError("code must be a non-empty exact string")
        if type(path) is not str or not path:
            raise TypeError("path must be a non-empty exact string")
        self.code = code
        self.path = path
        super().__init__(code)


def _fail(code: str, path: str) -> None:
    raise WorkerResultProjectionContractError(code, path)


def _text(value: Any, path: str, *, maximum: int = MAX_WORKER_PROJECTION_TEXT_BYTES) -> str:
    if type(value) is not str or not value:
        _fail("worker_result_projection_text_invalid", path)
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeEncodeError:
        _fail("worker_result_projection_text_invalid", path)
    if len(encoded) > maximum:
        _fail("worker_result_projection_text_budget_exceeded", path)
    return value


def _lower_hex(value: Any, path: str, length: int = 32) -> str:
    if (
        type(value) is not str
        or len(value) != length
        or any(character not in _LOWER_HEX for character in value)
    ):
        _fail("worker_result_projection_hex_invalid", path)
    return value


def _integer(value: Any, path: str, *, minimum: int = 0, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _fail("worker_result_projection_integer_invalid", path)
    return value


def _digest(value: Any, path: str) -> bytes:
    if type(value) is not bytes or len(value) != 32:
        _fail("worker_result_projection_digest_invalid", path)
    return value


def _timestamp(value: Any, path: str) -> str:
    if type(value) is not str or len(value) != 24:
        _fail("worker_result_projection_timestamp_invalid", path)
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except (TypeError, ValueError):
        _fail("worker_result_projection_timestamp_invalid", path)
    if parsed.strftime("%Y-%m-%dT%H:%M:%S.%fZ")[:-4] + "Z" != value:
        _fail("worker_result_projection_timestamp_invalid", path)
    return value


def _bytes(value: Any, path: str, *, maximum: int) -> bytes:
    if type(value) is not bytes or not value:
        _fail("worker_result_projection_bytes_invalid", path)
    if len(value) > maximum:
        _fail("worker_result_projection_bytes_budget_exceeded", path)
    return value


def _canonical_metadata_bytes(value: Any, path: str, *, maximum: int) -> bytes:
    value = _bytes(value, path, maximum=maximum)
    try:
        decoded = decode_payload_bounded(
            value,
            maximum,
            MAX_WORKER_PROJECTION_MANIFEST_NODES,
        )
        rebuilt = encode_payload_bounded(
            decoded,
            maximum,
            MAX_WORKER_PROJECTION_MANIFEST_NODES,
        )
    except BusinessCodecError:
        _fail("worker_result_projection_metadata_invalid", path)
    if rebuilt != value:
        _fail("worker_result_projection_metadata_not_canonical", path)
    return value


def _payload(value: Any, path: str) -> Optional[FrozenJsonPayload]:
    if value is None:
        return None
    try:
        return verify_frozen_json_payload(value)
    except BusinessTransactionContractError:
        _fail("worker_result_projection_payload_invalid", path)


class WorkerProofProjectionClass(str, Enum):
    COMPLETE = "complete"
    RECONCILE = "reconcile"


@dataclass(frozen=True)
class WorkerProjectionReceiptBinding:
    """Exact generic/specialised receipt, target, attempt and effect join."""

    receipt_parent: WorkerExecutionGenericReceiptEvidence
    execution_receipt: WorkerExecutionReceiptEvidence
    target_kind: WorkerTargetKind
    target_id: str
    attempt_id: str
    attempt_number: int
    effect: Optional[WorkerEffectBinding] = None

    def __post_init__(self) -> None:
        if (
            type(self.receipt_parent) is not WorkerExecutionGenericReceiptEvidence
            or type(self.execution_receipt) is not WorkerExecutionReceiptEvidence
            or self.execution_receipt.receipt_parent != self.receipt_parent
        ):
            _fail("worker_result_projection_receipt_binding_invalid", "$.binding.receipt")
        if (
            type(self.target_kind) is not WorkerTargetKind
            or self.target_kind is not self.execution_receipt.target_kind
        ):
            _fail("worker_result_projection_target_binding_invalid", "$.binding.target_kind")
        _lower_hex(self.target_id, "$.binding.target_id")
        if self.target_id != self.execution_receipt.target_id:
            _fail("worker_result_projection_target_binding_invalid", "$.binding.target_id")
        _lower_hex(self.attempt_id, "$.binding.attempt_id")
        _integer(
            self.attempt_number,
            "$.binding.attempt_number",
            minimum=1,
            maximum=MAX_RETRY_ATTEMPTS,
        )
        if self.effect is not None:
            if (
                type(self.effect) is not WorkerEffectBinding
                or self.effect.attempt_id != self.attempt_id
                or self.effect.attempt_number != self.attempt_number
            ):
                _fail("worker_result_projection_effect_binding_invalid", "$.binding.effect")

    @property
    def operation(self) -> WorkerExecutionOperation:
        return self.execution_receipt.operation

    @property
    def request_transaction_id(self) -> str:
        return self.execution_receipt.request_transaction_id


@dataclass(frozen=True)
class WorkerEffectRequestProjection:
    binding: WorkerProjectionReceiptBinding
    idempotency_scope: str
    idempotency_key: str
    request_payload: FrozenJsonPayload = field(repr=False)
    projection_manifest_bytes: bytes = field(repr=False)
    projection_sha256: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if (
            type(self.binding) is not WorkerProjectionReceiptBinding
            or self.binding.operation is not WorkerExecutionOperation.AUTHORIZE_EFFECT
            or self.binding.effect is None
        ):
            _fail("worker_effect_request_projection_binding_invalid", "$.effect.binding")
        _text(self.idempotency_scope, "$.effect.idempotency_scope")
        _text(self.idempotency_key, "$.effect.idempotency_key")
        if (
            self.idempotency_scope != self.binding.effect.idempotency_scope
            or self.idempotency_key != self.binding.effect.idempotency_key
        ):
            _fail("worker_effect_request_projection_binding_invalid", "$.effect.idempotency")
        _payload(self.request_payload, "$.effect.request_payload")
        _canonical_metadata_bytes(
            self.projection_manifest_bytes,
            "$.effect.projection_manifest_bytes",
            maximum=MAX_WORKER_PROJECTION_COMPONENT_BYTES,
        )
        _digest(self.projection_sha256, "$.effect.projection_sha256")


@dataclass(frozen=True)
class WorkerResultProjection:
    result_id: str
    binding: WorkerProjectionReceiptBinding
    after_status: WorkerExecutionStatus
    committed_at: str
    result_payload: Optional[FrozenJsonPayload] = field(default=None, repr=False)
    error_payload: Optional[FrozenJsonPayload] = field(default=None, repr=False)
    projection_manifest_bytes: bytes = field(default=b"", repr=False)
    result_sha256: bytes = field(default=b"", repr=False)
    proof_sha256: Optional[bytes] = field(default=None, repr=False)

    def __post_init__(self) -> None:
        _lower_hex(self.result_id, "$.result.result_id")
        if (
            type(self.binding) is not WorkerProjectionReceiptBinding
            or self.binding.operation not in (
                WorkerExecutionOperation.COMPLETE,
                WorkerExecutionOperation.RECONCILE,
            )
        ):
            _fail("worker_result_projection_binding_invalid", "$.result.binding")
        if (
            type(self.after_status) is not WorkerExecutionStatus
            or self.after_status is not self.binding.execution_receipt.after_status
        ):
            _fail("worker_result_projection_status_invalid", "$.result.after_status")
        _timestamp(self.committed_at, "$.result.committed_at")
        if self.committed_at != self.binding.receipt_parent.committed_at:
            _fail("worker_result_projection_receipt_binding_invalid", "$.result.committed_at")
        _payload(self.result_payload, "$.result.result_payload")
        _payload(self.error_payload, "$.result.error_payload")
        if self.result_payload is not None and self.error_payload is not None:
            _fail("worker_result_projection_payload_matrix_invalid", "$.result")
        _canonical_metadata_bytes(
            self.projection_manifest_bytes,
            "$.result.projection_manifest_bytes",
            maximum=MAX_WORKER_PROJECTION_COMPONENT_BYTES,
        )
        _digest(self.result_sha256, "$.result.result_sha256")
        if self.proof_sha256 is not None:
            _digest(self.proof_sha256, "$.result.proof_sha256")
            if hmac.compare_digest(
                self.proof_sha256,
                EMPTY_WORKER_PROOF_PROJECTION_SHA256,
            ):
                _fail(
                    "worker_result_projection_empty_proof_sentinel_reserved",
                    "$.result.proof_sha256",
                )


@dataclass(frozen=True)
class WorkerProofProjection:
    proof_id: str
    result_id: str
    binding: WorkerProjectionReceiptBinding
    proof_class: WorkerProofProjectionClass
    proof_kind: str
    observed_at: str
    evidence_payload: Optional[FrozenJsonPayload] = field(default=None, repr=False)
    provider_receipt_payload: Optional[FrozenJsonPayload] = field(default=None, repr=False)
    durable_result_id: Optional[str] = None
    durable_result_payload: Optional[FrozenJsonPayload] = field(default=None, repr=False)
    durable_result_recorded_at: Optional[str] = None
    result_commit_id: Optional[str] = None
    projection_manifest_bytes: bytes = field(default=b"", repr=False)
    proof_sha256: bytes = field(default=b"", repr=False)

    def __post_init__(self) -> None:
        _lower_hex(self.proof_id, "$.proof.proof_id")
        _lower_hex(self.result_id, "$.proof.result_id")
        if type(self.binding) is not WorkerProjectionReceiptBinding:
            _fail("worker_proof_projection_binding_invalid", "$.proof.binding")
        if type(self.proof_class) is not WorkerProofProjectionClass:
            _fail("worker_proof_projection_class_invalid", "$.proof.proof_class")
        expected_operation = (
            WorkerExecutionOperation.COMPLETE
            if self.proof_class is WorkerProofProjectionClass.COMPLETE
            else WorkerExecutionOperation.RECONCILE
        )
        if self.binding.operation is not expected_operation:
            _fail("worker_proof_projection_binding_invalid", "$.proof.binding.operation")
        _text(self.proof_kind, "$.proof.proof_kind", maximum=64)
        _timestamp(self.observed_at, "$.proof.observed_at")
        if self.observed_at != self.binding.receipt_parent.committed_at:
            _fail("worker_proof_projection_binding_invalid", "$.proof.observed_at")
        _payload(self.evidence_payload, "$.proof.evidence_payload")
        _payload(self.provider_receipt_payload, "$.proof.provider_receipt_payload")
        _payload(self.durable_result_payload, "$.proof.durable_result_payload")
        durable_fields = (
            self.durable_result_id,
            self.durable_result_payload,
            self.durable_result_recorded_at,
        )
        if any(value is None for value in durable_fields) and any(
            value is not None for value in durable_fields
        ):
            _fail("worker_proof_projection_payload_matrix_invalid", "$.proof.durable_result")
        if self.durable_result_id is not None:
            _lower_hex(self.durable_result_id, "$.proof.durable_result_id")
            _timestamp(
                self.durable_result_recorded_at,
                "$.proof.durable_result_recorded_at",
            )
        if self.result_commit_id is not None:
            _lower_hex(self.result_commit_id, "$.proof.result_commit_id")
        _canonical_metadata_bytes(
            self.projection_manifest_bytes,
            "$.proof.projection_manifest_bytes",
            maximum=MAX_WORKER_PROJECTION_COMPONENT_BYTES,
        )
        _digest(self.proof_sha256, "$.proof.proof_sha256")
        if hmac.compare_digest(
            self.proof_sha256,
            EMPTY_WORKER_PROOF_PROJECTION_SHA256,
        ):
            _fail(
                "worker_result_projection_empty_proof_sentinel_reserved",
                "$.proof.proof_sha256",
            )


@dataclass(frozen=True)
class WorkerResultProjectionClosure:
    effect_request: Optional[WorkerEffectRequestProjection]
    result: Optional[WorkerResultProjection]
    proof: Optional[WorkerProofProjection]
    manifest_bytes: bytes = field(repr=False)
    manifest_sha256: bytes = field(repr=False)

    def __post_init__(self) -> None:
        effect_only = (
            type(self.effect_request) is WorkerEffectRequestProjection
            and self.result is None
            and self.proof is None
        )
        result_shape = (
            self.effect_request is None
            and type(self.result) is WorkerResultProjection
            and (
                self.proof is None
                or type(self.proof) is WorkerProofProjection
            )
        )
        if not (effect_only or result_shape):
            _fail("worker_result_projection_closure_invalid", "$.closure")
        if result_shape and self.result is not None:
            if self.proof is None:
                if self.result.proof_sha256 is not None:
                    _fail("worker_result_projection_closure_invalid", "$.closure.proof")
            elif (
                self.result.binding != self.proof.binding
                or self.result.result_id != self.proof.result_id
                or self.result.proof_sha256 is None
                or not hmac.compare_digest(
                    self.result.proof_sha256,
                    self.proof.proof_sha256,
                )
            ):
                _fail("worker_result_projection_closure_invalid", "$.closure.proof")
        _canonical_metadata_bytes(
            self.manifest_bytes,
            "$.closure.manifest_bytes",
            maximum=MAX_WORKER_PROJECTION_MANIFEST_BYTES,
        )
        _digest(self.manifest_sha256, "$.closure.manifest_sha256")


__all__ = ()
