"""Canonical issuer and strict verifier for V13 cutover phase bindings.

Every field of the predecessor, successor, external checkpoints and direct
stage evidence is encoded into one deterministic manifest.  The manifest is
then hashed with the stage-specific domain using 64-bit length framing.  No
field is accepted merely because a caller supplied a matching opaque digest.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json
from enum import Enum
from typing import Any, Dict

from business_codec import BusinessCodecError, encode_payload_bounded
from business_cutover_phase_binding_contract import (
    BUSINESS_CUTOVER_PHASE_BINDING_DOMAINS,
    BUSINESS_CUTOVER_PHASE_BINDING_MANIFEST_SCHEMA,
    BUSINESS_CUTOVER_PHASE_BINDING_PROTOCOL,
    BUSINESS_CUTOVER_PHASE_BINDING_STATUS,
    BUSINESS_CUTOVER_PROVIDER_RECEIPT_CODEC,
    BUSINESS_CUTOVER_PROVIDER_RECEIPT_SCHEMA,
    BUSINESS_CUTOVER_PROVIDER_RECEIPT_VERSION,
    MAX_BUSINESS_CUTOVER_PHASE_BINDING_MANIFEST_BYTES,
    MAX_BUSINESS_CUTOVER_PHASE_BINDING_MANIFEST_NODES,
    MAX_BUSINESS_CUTOVER_PROVIDER_PAYLOAD_BYTES,
    BusinessCutoverActivationEvidence,
    BusinessCutoverAdmissionCheckpoint,
    BusinessCutoverAllocationHighWater,
    BusinessCutoverAssetCoverageCheckpoint,
    BusinessCutoverAssetProviderEvidence,
    BusinessCutoverAuditEvidence,
    BusinessCutoverDispatchHighWater,
    BusinessCutoverDurableCheckpoint,
    BusinessCutoverMaintenanceCheckpoint,
    BusinessCutoverMountEvidence,
    BusinessCutoverPhaseBinding,
    BusinessCutoverPhaseBindingContractError,
    BusinessCutoverPhaseBindingHead,
    BusinessCutoverPhaseBindingIdentity,
    BusinessCutoverPhaseBindingProjection,
    BusinessCutoverPhaseBindingStage,
    BusinessCutoverReceiptPointer,
    BusinessCutoverRuntimeImage,
    BusinessCutoverWorkerHighWater,
    _BINDING_ISSUER,
)


BUSINESS_CUTOVER_PROVIDER_CHECKPOINT_HASH_DOMAIN = (
    b"ze.business-cutover.asset-provider-checkpoint.v1"
)

_PROJECTION_TYPES = frozenset((
    BusinessCutoverPhaseBindingProjection,
    BusinessCutoverPhaseBindingIdentity,
    BusinessCutoverPhaseBindingHead,
    BusinessCutoverAdmissionCheckpoint,
    BusinessCutoverMaintenanceCheckpoint,
    BusinessCutoverDurableCheckpoint,
    BusinessCutoverRuntimeImage,
    BusinessCutoverWorkerHighWater,
    BusinessCutoverAllocationHighWater,
    BusinessCutoverDispatchHighWater,
    BusinessCutoverReceiptPointer,
    BusinessCutoverAssetCoverageCheckpoint,
    BusinessCutoverAssetProviderEvidence,
    BusinessCutoverAuditEvidence,
    BusinessCutoverActivationEvidence,
    BusinessCutoverMountEvidence,
))


class BusinessCutoverPhaseBindingIntegrityError(ValueError):
    """Stable payload-free failure at the canonical binding boundary."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        if type(code) is not str or not code:
            raise TypeError("code must be a non-empty exact string")
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise BusinessCutoverPhaseBindingIntegrityError(code)


def _frame(hasher: Any, value: bytes) -> None:
    hasher.update(len(value).to_bytes(8, "big"))
    hasher.update(value)


def _framed_digest(domain: bytes, payload: bytes) -> bytes:
    hasher = hashlib.sha256()
    _frame(hasher, domain)
    _frame(hasher, payload)
    return hasher.digest()


def _canonical_value(value: Any) -> Any:
    """Project every declared field without accepting arbitrary mappings.

    Field names come from the frozen exact dataclass definitions.  This keeps
    the encoder total when a checkpoint grows: adding a declared field cannot
    silently leave it outside the manifest.
    """

    if type(value) is bytes:
        return value.hex()
    if isinstance(value, Enum):
        return value.value
    if value is None or type(value) in (str, int, bool):
        return value
    if type(value) in _PROJECTION_TYPES:
        return {
            item.name: _canonical_value(getattr(value, item.name))
            for item in dataclasses.fields(value)
            if not item.name.startswith("_")
        }
    _fail("business_cutover_phase_binding_canonical_type_invalid")
    raise AssertionError("unreachable")


def _rebuild(value: Any) -> Any:
    """Recursively reconstruct exact types to detect post-init mutation."""

    if type(value) in (bytes, str, int, bool) or value is None:
        return value
    if isinstance(value, Enum):
        try:
            return type(value)(value.value)
        except (TypeError, ValueError) as exc:
            raise BusinessCutoverPhaseBindingIntegrityError(
                "business_cutover_phase_binding_projection_changed"
            ) from exc
    if type(value) not in _PROJECTION_TYPES:
        _fail("business_cutover_phase_binding_projection_type_invalid")
    try:
        return type(value)(**{
            item.name: _rebuild(getattr(value, item.name))
            for item in dataclasses.fields(value)
            if not item.name.startswith("_")
        })
    except (
        AttributeError,
        BusinessCutoverPhaseBindingContractError,
        TypeError,
        ValueError,
    ) as exc:
        raise BusinessCutoverPhaseBindingIntegrityError(
            "business_cutover_phase_binding_projection_changed"
        ) from exc


def _require_projection(
    value: Any,
) -> BusinessCutoverPhaseBindingProjection:
    if type(value) is not BusinessCutoverPhaseBindingProjection:
        _fail("business_cutover_phase_binding_projection_type_invalid")
    rebuilt = _rebuild(value)
    if rebuilt != value:
        _fail("business_cutover_phase_binding_projection_changed")
    return rebuilt


def _provider_receipt_value(
    *,
    coverage: BusinessCutoverAssetCoverageCheckpoint,
    provider_protocol: str,
    provider_id: str,
    provider_generation: str,
    provider_sequence: int,
    checked_at: str,
    object_count: int,
    total_bytes: int,
    blob_root_sha256: bytes,
) -> Dict[str, Any]:
    """The provider receipt is useful data, not a digest-only wrapper."""

    return {
        "schema": BUSINESS_CUTOVER_PROVIDER_RECEIPT_SCHEMA,
        "codec": BUSINESS_CUTOVER_PROVIDER_RECEIPT_CODEC,
        "version": BUSINESS_CUTOVER_PROVIDER_RECEIPT_VERSION,
        "provider": {
            "protocol": provider_protocol,
            "provider_id": provider_id,
            "generation": provider_generation,
            "sequence": provider_sequence,
            "checked_at": checked_at,
            "object_count": object_count,
            "total_bytes": total_bytes,
            "blob_root_sha256": blob_root_sha256.hex(),
        },
        "coverage": _canonical_value(coverage),
    }


def _deterministic_json(value: Dict[str, Any]) -> bytes:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8", "strict")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise BusinessCutoverPhaseBindingIntegrityError(
            "business_cutover_phase_binding_provider_receipt_encoding_failed"
        ) from exc
    if not 1 <= len(payload) <= MAX_BUSINESS_CUTOVER_PROVIDER_PAYLOAD_BYTES:
        _fail("business_cutover_phase_binding_provider_receipt_budget_exceeded")
    return payload


def issue_business_cutover_asset_provider_evidence(
    *,
    coverage: BusinessCutoverAssetCoverageCheckpoint,
    provider_protocol: str,
    provider_id: str,
    provider_generation: str,
    provider_sequence: int,
    checked_at: str,
    object_count: int,
    total_bytes: int,
    blob_root_sha256: bytes,
) -> BusinessCutoverAssetProviderEvidence:
    """Build the self-describing deterministic provider checkpoint receipt."""

    if type(coverage) is not BusinessCutoverAssetCoverageCheckpoint:
        _fail("business_cutover_phase_binding_provider_coverage_invalid")
    payload = _deterministic_json(_provider_receipt_value(
        coverage=coverage,
        provider_protocol=provider_protocol,
        provider_id=provider_id,
        provider_generation=provider_generation,
        provider_sequence=provider_sequence,
        checked_at=checked_at,
        object_count=object_count,
        total_bytes=total_bytes,
        blob_root_sha256=blob_root_sha256,
    ))
    payload_sha256 = hashlib.sha256(payload).digest()
    checkpoint_sha256 = _framed_digest(
        BUSINESS_CUTOVER_PROVIDER_CHECKPOINT_HASH_DOMAIN,
        payload,
    )
    try:
        return BusinessCutoverAssetProviderEvidence(
            coverage=coverage,
            provider_protocol=provider_protocol,
            provider_id=provider_id,
            provider_generation=provider_generation,
            provider_sequence=provider_sequence,
            checked_at=checked_at,
            object_count=object_count,
            total_bytes=total_bytes,
            blob_root_sha256=blob_root_sha256,
            receipt_payload=payload,
            receipt_payload_sha256=payload_sha256,
            checkpoint_sha256=checkpoint_sha256,
        )
    except (BusinessCutoverPhaseBindingContractError, TypeError) as exc:
        raise BusinessCutoverPhaseBindingIntegrityError(
            "business_cutover_phase_binding_provider_evidence_invalid"
        ) from exc


def _require_provider_evidence(
    value: BusinessCutoverAssetProviderEvidence,
) -> BusinessCutoverAssetProviderEvidence:
    expected = issue_business_cutover_asset_provider_evidence(
        coverage=value.coverage,
        provider_protocol=value.provider_protocol,
        provider_id=value.provider_id,
        provider_generation=value.provider_generation,
        provider_sequence=value.provider_sequence,
        checked_at=value.checked_at,
        object_count=value.object_count,
        total_bytes=value.total_bytes,
        blob_root_sha256=value.blob_root_sha256,
    )
    if (
        expected.receipt_schema != value.receipt_schema
        or expected.receipt_codec != value.receipt_codec
        or expected.receipt_version != value.receipt_version
        or expected.receipt_payload != value.receipt_payload
        or not hmac.compare_digest(
            expected.receipt_payload_sha256,
            value.receipt_payload_sha256,
        )
        or not hmac.compare_digest(
            expected.checkpoint_sha256,
            value.checkpoint_sha256,
        )
    ):
        _fail("business_cutover_phase_binding_provider_receipt_mismatch")
    return value


def _manifest_value(
    projection: BusinessCutoverPhaseBindingProjection,
) -> Dict[str, Any]:
    return {
        "schema": BUSINESS_CUTOVER_PHASE_BINDING_MANIFEST_SCHEMA,
        "protocol": BUSINESS_CUTOVER_PHASE_BINDING_PROTOCOL,
        "status": BUSINESS_CUTOVER_PHASE_BINDING_STATUS,
        "binding_domain": BUSINESS_CUTOVER_PHASE_BINDING_DOMAINS[
            projection.stage
        ],
        "stage": projection.stage.value,
        "identity": _canonical_value(projection.identity),
        "old_head": _canonical_value(projection.old_head),
        "new_head": _canonical_value(projection.new_head),
        "admission": _canonical_value(projection.admission),
        "maintenance": _canonical_value(projection.maintenance),
        "durable_checkpoint": _canonical_value(projection.checkpoint),
        "direct_evidence": _canonical_value(projection.direct_evidence),
    }


def _manifest_bytes(projection: BusinessCutoverPhaseBindingProjection) -> bytes:
    try:
        return encode_payload_bounded(
            _manifest_value(projection),
            MAX_BUSINESS_CUTOVER_PHASE_BINDING_MANIFEST_BYTES,
            MAX_BUSINESS_CUTOVER_PHASE_BINDING_MANIFEST_NODES,
        )
    except BusinessCodecError as exc:
        raise BusinessCutoverPhaseBindingIntegrityError(
            "business_cutover_phase_binding_manifest_encoding_failed"
        ) from exc


def _manifest_digest(
    stage: BusinessCutoverPhaseBindingStage,
    manifest_bytes: bytes,
) -> bytes:
    return _framed_digest(
        BUSINESS_CUTOVER_PHASE_BINDING_DOMAINS[stage].encode("ascii"),
        manifest_bytes,
    )


def issue_business_cutover_phase_binding(
    projection: BusinessCutoverPhaseBindingProjection,
) -> BusinessCutoverPhaseBinding:
    """Issue exactly one stage binding from a fully explicit projection."""

    canonical = _require_projection(projection)
    if canonical.stage is BusinessCutoverPhaseBindingStage.ASSET_PROVIDER:
        _require_provider_evidence(canonical.direct_evidence)
    manifest_bytes = _manifest_bytes(canonical)
    manifest_sha256 = _manifest_digest(canonical.stage, manifest_bytes)
    try:
        return BusinessCutoverPhaseBinding(
            projection=canonical,
            binding_domain=BUSINESS_CUTOVER_PHASE_BINDING_DOMAINS[
                canonical.stage
            ],
            manifest_bytes=manifest_bytes,
            manifest_sha256=manifest_sha256,
            _issuer=_BINDING_ISSUER,
        )
    except (BusinessCutoverPhaseBindingContractError, TypeError) as exc:
        raise BusinessCutoverPhaseBindingIntegrityError(
            "business_cutover_phase_binding_issue_failed"
        ) from exc


def verify_business_cutover_phase_binding(
    value: BusinessCutoverPhaseBinding,
) -> BusinessCutoverPhaseBinding:
    """Strictly rebuild all fields and recompute the canonical fixed vector."""

    if type(value) is not BusinessCutoverPhaseBinding:
        _fail("business_cutover_phase_binding_type_invalid")
    try:
        if (
            value._issuer is not _BINDING_ISSUER
            or value.protocol != BUSINESS_CUTOVER_PHASE_BINDING_PROTOCOL
            or value.status != BUSINESS_CUTOVER_PHASE_BINDING_STATUS
        ):
            _fail("business_cutover_phase_binding_unissued")
        canonical = _require_projection(value.projection)
        if canonical.stage is BusinessCutoverPhaseBindingStage.ASSET_PROVIDER:
            _require_provider_evidence(canonical.direct_evidence)
        domain = BUSINESS_CUTOVER_PHASE_BINDING_DOMAINS[canonical.stage]
        manifest_bytes = _manifest_bytes(canonical)
        manifest_sha256 = _manifest_digest(canonical.stage, manifest_bytes)
        if (
            value.binding_domain != domain
            or value.manifest_bytes != manifest_bytes
            or not hmac.compare_digest(value.manifest_sha256, manifest_sha256)
        ):
            _fail("business_cutover_phase_binding_manifest_mismatch")
    except BusinessCutoverPhaseBindingContractError as exc:
        raise BusinessCutoverPhaseBindingIntegrityError(
            "business_cutover_phase_binding_changed"
        ) from exc
    return value


def _verify_business_cutover_phase_binding_chain(
    values: tuple[BusinessCutoverPhaseBinding, ...],
) -> tuple[BusinessCutoverPhaseBinding, ...]:
    """Verify the cumulative prefix, including every adjacent checkpoint.

    A canonical manifest proves one row.  Cutover safety additionally needs
    the next row to begin from the exact external and durable snapshot that
    the preceding row finished with; sharing only the same phase-head digest
    is not enough.
    """

    if type(values) is not tuple or len(values) > 4:
        _fail("business_cutover_phase_binding_chain_invalid")
    expected_stages = tuple(BusinessCutoverPhaseBindingStage)[: len(values)]
    verified = tuple(verify_business_cutover_phase_binding(value) for value in values)
    if tuple(value.projection.stage for value in verified) != expected_stages:
        _fail("business_cutover_phase_binding_chain_invalid")

    for previous, current in zip(verified, verified[1:]):
        left = previous.projection
        right = current.projection
        left_admission = left.admission
        right_admission = right.admission
        successor_runtime = (
            left.direct_evidence.after_runtime
            if left.stage is BusinessCutoverPhaseBindingStage.ACTIVATION
            else left.checkpoint.runtime
        )
        same_source = bool(
            left_admission.source_revision == right_admission.source_revision
            and hmac.compare_digest(
                left_admission.source_admission_sha256,
                right_admission.source_admission_sha256,
            )
            and hmac.compare_digest(
                left_admission.source_freeze_sha256,
                right_admission.source_freeze_sha256,
            )
            and left_admission.file_authority_epoch
            == right_admission.file_authority_epoch
            and left_admission.file_cutover_id == right_admission.file_cutover_id
        )
        same_file_checkpoint = (
            left_admission == right_admission
            if right.stage in (
                BusinessCutoverPhaseBindingStage.AUDIT,
                BusinessCutoverPhaseBindingStage.MOUNT,
            )
            else True
        )
        if not (
            left.identity == right.identity
            and left.new_head == right.old_head
            and same_source
            and same_file_checkpoint
            and left.maintenance.scope == right.maintenance.scope
            and hmac.compare_digest(
                left.maintenance.proof_sha256,
                right.maintenance.proof_sha256,
            )
            and left.maintenance.observed_at <= right.maintenance.observed_at
            and successor_runtime == right.checkpoint.runtime
            and left.checkpoint.receipt_after_count
            == right.checkpoint.receipt_before_count
            and hmac.compare_digest(
                left.checkpoint.receipt_after_sha256,
                right.checkpoint.receipt_before_sha256,
            )
            and left.checkpoint.worker == right.checkpoint.worker
            and left.checkpoint.allocation == right.checkpoint.allocation
            and left.checkpoint.dispatch == right.checkpoint.dispatch
        ):
            _fail("business_cutover_phase_binding_chain_invalid")
    return verified


__all__ = ()
