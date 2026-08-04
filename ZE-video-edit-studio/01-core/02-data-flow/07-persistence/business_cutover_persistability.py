"""Pure V11 preflight for every cutover manifest carrier persisted by SQLite.

The integrity issuers deliberately allow manifests larger than an individual
SQLite carrier so they can describe the complete source closure.  The V11
schema is stricter: every manifest BLOB written by the production cutover
coordinator must fit one ``MAX_BUSINESS_SMALL_PAYLOAD_BYTES`` column.  This
module closes that impedance mismatch before a writer is allowed to perform
I/O.

V11 stores only ``member_mapping_sha256`` on the install receipt.  It has no
``member_mapping_bytes`` carrier, and the current baseline rows do not yet have
a frozen complete reconstruction contract for that source mapping.  Install
therefore measures its real manifest carrier and then fails closed; it must not
return a ``persistable`` profile until a durable row projection plus fresh
reconstruction reader exists.

Every phase entry point first revalidates the private issuer stamp and rebuilds
the corresponding plan.  Only then does it measure the exact immutable bytes.
The returned profile is diagnostic metadata, not persistence evidence or
runtime authority.  Failures expose only a stable code and bounded numeric
measurements; manifest contents and repository identities never enter the
exception message.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, NoReturn, Tuple

from business_cutover_activation_integrity import (
    BusinessCutoverActivationIntegrityError,
    _require_business_cutover_activation_plan,
)
from business_cutover_asset_integrity import (
    BusinessCutoverAssetIntegrityError,
    _require_business_cutover_asset_coverage_plan,
    _require_business_cutover_asset_stage_plan,
)
from business_cutover_head_integrity import (
    BusinessCutoverHeadIntegrityError,
    _require_business_cutover_head_chain,
)
from business_cutover_integrity import (
    BusinessCutoverIntegrityError,
    _require_business_cutover_plan,
)
from business_cutover_install_closure import (
    BusinessCutoverInstallClosureError,
    _require_business_cutover_install_closure,
)
from business_cutover_mount_integrity import (
    BusinessCutoverMountIntegrityError,
    _require_business_cutover_mount_plan,
)
from repository_audit_integrity import (
    RepositoryAuditIntegrityError,
    _require_repository_audit_certificate_plan,
)
from sqlite_schema import MAX_BUSINESS_SMALL_PAYLOAD_BYTES


BUSINESS_CUTOVER_PERSISTABILITY_PROTOCOL = "ze.business-cutover.persistability.v1"
BUSINESS_CUTOVER_PERSISTABILITY_STATUS = "persistable"

_LOWER_HEX = frozenset("0123456789abcdef")


class BusinessCutoverPersistabilityPhase(str, Enum):
    INSTALL = "install"
    ASSET_STAGE = "asset_stage"
    ASSET_COVERAGE = "asset_coverage"
    AUDIT = "audit"
    ACTIVATION = "activation"
    MOUNT = "mount"
    HEAD = "head"


class BusinessCutoverPersistabilityCarrierKind(str, Enum):
    MANIFEST = "manifest"
    PHASE_HEAD_MANIFEST = "phase_head_manifest"
    MEMBER_MAPPING_CHUNK = "member_mapping_chunk"
    BASELINE_PROJECTION_CHUNK = "baseline_projection_chunk"
    BASELINE_INNER_MANIFEST = "baseline_inner_manifest"
    BASELINE_OUTER_MANIFEST = "baseline_outer_manifest"
    INSTALL_CLOSURE_MANIFEST = "install_closure_manifest"


class BusinessCutoverPersistabilityError(ValueError):
    """Stable payload-free failure at the V11 pre-I/O boundary."""

    __slots__ = (
        "code",
        "phase",
        "carrier",
        "actual_bytes",
        "limit_bytes",
    )

    def __init__(
        self,
        code: str,
        *,
        phase: str,
        carrier: str = "",
        actual_bytes: int = 0,
        limit_bytes: int = MAX_BUSINESS_SMALL_PAYLOAD_BYTES,
    ) -> None:
        if type(code) is not str or not code:
            raise TypeError("code must be a non-empty exact string")
        if type(phase) is not str or not phase:
            raise TypeError("phase must be a non-empty exact string")
        if type(carrier) is not str:
            raise TypeError("carrier must be an exact string")
        if type(actual_bytes) is not int or actual_bytes < 0:
            raise TypeError("actual_bytes must be a non-negative exact integer")
        if type(limit_bytes) is not int or limit_bytes < 0:
            raise TypeError("limit_bytes must be a non-negative exact integer")
        self.code = code
        self.phase = phase
        self.carrier = carrier
        self.actual_bytes = actual_bytes
        self.limit_bytes = limit_bytes
        super().__init__(code)


def _fail(
    code: str,
    *,
    phase: BusinessCutoverPersistabilityPhase,
    carrier: str = "",
    actual_bytes: int = 0,
    limit_bytes: int = MAX_BUSINESS_SMALL_PAYLOAD_BYTES,
) -> None:
    raise BusinessCutoverPersistabilityError(
        code,
        phase=phase.value,
        carrier=carrier,
        actual_bytes=actual_bytes,
        limit_bytes=limit_bytes,
    )


@dataclass(frozen=True)
class BusinessCutoverPersistabilityCarrier:
    """One exact SQLite BLOB carrier measurement."""

    phase: BusinessCutoverPersistabilityPhase
    carrier: BusinessCutoverPersistabilityCarrierKind
    ordinal: int
    actual_bytes: int
    limit_bytes: int

    def __post_init__(self) -> None:
        if type(self.phase) is not BusinessCutoverPersistabilityPhase:
            raise TypeError("phase must be an exact persistability phase")
        if type(self.carrier) is not BusinessCutoverPersistabilityCarrierKind:
            raise TypeError("carrier must be an exact persistability carrier")
        if type(self.ordinal) is not int or self.ordinal < 0:
            raise TypeError("ordinal must be a non-negative exact integer")
        if type(self.actual_bytes) is not int or self.actual_bytes < 1:
            raise TypeError("actual_bytes must be a positive exact integer")
        if (
            type(self.limit_bytes) is not int
            or not 1 <= self.limit_bytes <= MAX_BUSINESS_SMALL_PAYLOAD_BYTES
        ):
            raise TypeError("limit_bytes must be within the V11 carrier budget")
        if self.actual_bytes > self.limit_bytes:
            raise ValueError("persistability carrier exceeds its limit")


@dataclass(frozen=True)
class BusinessCutoverPersistabilityProfile:
    """Immutable, non-authoritative result of one phase preflight."""

    phase: BusinessCutoverPersistabilityPhase
    cutover_id: str
    carriers: Tuple[BusinessCutoverPersistabilityCarrier, ...]
    protocol: str = BUSINESS_CUTOVER_PERSISTABILITY_PROTOCOL
    status: str = BUSINESS_CUTOVER_PERSISTABILITY_STATUS

    def __post_init__(self) -> None:
        if type(self.phase) is not BusinessCutoverPersistabilityPhase:
            raise TypeError("phase must be an exact persistability phase")
        if (
            type(self.cutover_id) is not str
            or len(self.cutover_id) != 64
            or any(value not in _LOWER_HEX for value in self.cutover_id)
        ):
            raise TypeError("cutover_id must be one exact lowercase digest")
        if (
            type(self.carriers) is not tuple
            or not self.carriers
            or any(
                type(value) is not BusinessCutoverPersistabilityCarrier
                or value.phase is not self.phase
                for value in self.carriers
            )
        ):
            raise TypeError("carriers must be one non-empty exact phase tuple")
        identities = tuple((value.carrier, value.ordinal) for value in self.carriers)
        if len(set(identities)) != len(identities):
            raise ValueError("persistability carrier identities must be unique")
        if self.protocol != BUSINESS_CUTOVER_PERSISTABILITY_PROTOCOL:
            raise ValueError("persistability protocol is invalid")
        if self.status != BUSINESS_CUTOVER_PERSISTABILITY_STATUS:
            raise ValueError("persistability status is invalid")

    @property
    def total_carrier_bytes(self) -> int:
        """Diagnostic sum only; the schema limit applies to each carrier."""

        return sum(value.actual_bytes for value in self.carriers)


_PLAN_ERRORS = (
    BusinessCutoverIntegrityError,
    BusinessCutoverAssetIntegrityError,
    RepositoryAuditIntegrityError,
    BusinessCutoverActivationIntegrityError,
    BusinessCutoverMountIntegrityError,
    BusinessCutoverHeadIntegrityError,
    BusinessCutoverInstallClosureError,
    AttributeError,
    TypeError,
)


def _revalidate_plan(
    value: Any,
    verifier: Callable[[Any], Any],
    phase: BusinessCutoverPersistabilityPhase,
) -> Any:
    """Normalize all issuer failures before any carrier is inspected."""

    try:
        return verifier(value)
    except _PLAN_ERRORS as exc:
        _fail(
            "business_cutover_persistability_plan_invalid",
            phase=phase,
        )
        raise AssertionError("unreachable") from exc


def _measure_business_cutover_persistable_carrier(
    *,
    phase: BusinessCutoverPersistabilityPhase,
    carrier: BusinessCutoverPersistabilityCarrierKind,
    ordinal: int,
    payload: Any,
    _limit: int = MAX_BUSINESS_SMALL_PAYLOAD_BYTES,
) -> BusinessCutoverPersistabilityCarrier:
    """Measure one carrier; ``_limit`` exists only for bounded boundary tests."""

    if type(phase) is not BusinessCutoverPersistabilityPhase:
        raise TypeError("phase must be an exact persistability phase")
    if type(carrier) is not BusinessCutoverPersistabilityCarrierKind:
        raise TypeError("carrier must be an exact persistability carrier")
    if type(ordinal) is not int or ordinal < 0:
        raise TypeError("ordinal must be a non-negative exact integer")
    if type(_limit) is not int or not 1 <= _limit <= MAX_BUSINESS_SMALL_PAYLOAD_BYTES:
        raise TypeError("_limit must be within the V11 carrier budget")
    if type(payload) is not bytes:
        _fail(
            "business_cutover_persistability_payload_invalid",
            phase=phase,
            carrier=carrier.value,
            limit_bytes=_limit,
        )
    actual = len(payload)
    if actual < 1:
        _fail(
            "business_cutover_persistability_payload_invalid",
            phase=phase,
            carrier=carrier.value,
            actual_bytes=actual,
            limit_bytes=_limit,
        )
    if actual > _limit:
        _fail(
            "business_cutover_persistability_payload_bytes_exceeded",
            phase=phase,
            carrier=carrier.value,
            actual_bytes=actual,
            limit_bytes=_limit,
        )
    return BusinessCutoverPersistabilityCarrier(
        phase=phase,
        carrier=carrier,
        ordinal=ordinal,
        actual_bytes=actual,
        limit_bytes=_limit,
    )


def _profile(
    *,
    phase: BusinessCutoverPersistabilityPhase,
    cutover_id: str,
    payloads: Tuple[Tuple[BusinessCutoverPersistabilityCarrierKind, int, bytes], ...],
) -> BusinessCutoverPersistabilityProfile:
    carriers = tuple(
        _measure_business_cutover_persistable_carrier(
            phase=phase,
            carrier=carrier,
            ordinal=ordinal,
            payload=payload,
        )
        for carrier, ordinal, payload in payloads
    )
    return BusinessCutoverPersistabilityProfile(
        phase=phase,
        cutover_id=cutover_id,
        carriers=carriers,
    )


def _require_business_cutover_install_persistability(
    value: Any,
) -> NoReturn:
    phase = BusinessCutoverPersistabilityPhase.INSTALL
    plan = _revalidate_plan(value, _require_business_cutover_plan, phase)
    _measure_business_cutover_persistable_carrier(
        phase=phase,
        carrier=BusinessCutoverPersistabilityCarrierKind.MANIFEST,
        ordinal=0,
        payload=plan.closure.install_manifest_bytes,
    )
    _fail(
        "business_cutover_persistability_member_mapping_projection_unproven",
        phase=phase,
        carrier="member_mapping_projection",
        limit_bytes=0,
    )
    raise AssertionError("unreachable")


def _require_business_cutover_install_closure_persistability(
    value: Any,
) -> BusinessCutoverPersistabilityProfile:
    """Measure every real V12 install carrier after full closure projection."""

    phase = BusinessCutoverPersistabilityPhase.INSTALL
    closure = _revalidate_plan(
        value,
        _require_business_cutover_install_closure,
        phase,
    )
    payloads = [
        (
            BusinessCutoverPersistabilityCarrierKind.MANIFEST,
            0,
            closure._plan.closure.install_manifest_bytes,
        ),
    ]
    payloads.extend(
        (
            BusinessCutoverPersistabilityCarrierKind.MEMBER_MAPPING_CHUNK,
            value.ordinal,
            value.chunk_bytes,
        )
        for value in closure.member_mapping.chunks
    )
    payloads.extend(
        (
            BusinessCutoverPersistabilityCarrierKind.BASELINE_PROJECTION_CHUNK,
            value.ordinal,
            value.chunk_bytes,
        )
        for value in closure.baseline_projection.chunks
    )
    payloads.extend(
        (
            BusinessCutoverPersistabilityCarrierKind.BASELINE_INNER_MANIFEST,
            value.member_ordinal,
            value.manifest_bytes,
        )
        for value in closure.baseline_receipts
    )
    payloads.extend(
        (
            BusinessCutoverPersistabilityCarrierKind.BASELINE_OUTER_MANIFEST,
            value.member_ordinal,
            value.baseline_outer_manifest_bytes,
        )
        for value in closure.baseline_receipts
    )
    payloads.append((
        BusinessCutoverPersistabilityCarrierKind.INSTALL_CLOSURE_MANIFEST,
        0,
        closure.closure.manifest_bytes,
    ))
    return _profile(
        phase=phase,
        cutover_id=closure.cutover_id,
        payloads=tuple(payloads),
    )


def _require_business_cutover_asset_stage_persistability(
    value: Any,
) -> BusinessCutoverPersistabilityProfile:
    phase = BusinessCutoverPersistabilityPhase.ASSET_STAGE
    plan = _revalidate_plan(
        value,
        _require_business_cutover_asset_stage_plan,
        phase,
    )
    return _profile(
        phase=phase,
        cutover_id=plan.cutover_id,
        payloads=(
            (BusinessCutoverPersistabilityCarrierKind.MANIFEST, 0, plan.manifest_bytes),
        ),
    )


def _require_business_cutover_asset_coverage_persistability(
    value: Any,
) -> BusinessCutoverPersistabilityProfile:
    phase = BusinessCutoverPersistabilityPhase.ASSET_COVERAGE
    plan = _revalidate_plan(
        value,
        _require_business_cutover_asset_coverage_plan,
        phase,
    )
    return _profile(
        phase=phase,
        cutover_id=plan.cutover_id,
        payloads=(
            (BusinessCutoverPersistabilityCarrierKind.MANIFEST, 0, plan.manifest_bytes),
        ),
    )


def _require_repository_audit_persistability(
    value: Any,
) -> BusinessCutoverPersistabilityProfile:
    phase = BusinessCutoverPersistabilityPhase.AUDIT
    plan = _revalidate_plan(
        value,
        _require_repository_audit_certificate_plan,
        phase,
    )
    return _profile(
        phase=phase,
        cutover_id=plan.evidence.runtime.cutover_id,
        payloads=(
            (
                BusinessCutoverPersistabilityCarrierKind.MANIFEST,
                0,
                plan.evidence.manifest_bytes,
            ),
        ),
    )


def _require_business_cutover_activation_persistability(
    value: Any,
) -> BusinessCutoverPersistabilityProfile:
    phase = BusinessCutoverPersistabilityPhase.ACTIVATION
    plan = _revalidate_plan(
        value,
        _require_business_cutover_activation_plan,
        phase,
    )
    return _profile(
        phase=phase,
        cutover_id=plan.cutover_id,
        payloads=(
            (
                BusinessCutoverPersistabilityCarrierKind.MANIFEST,
                0,
                plan.evidence.manifest_bytes,
            ),
        ),
    )


def _require_business_cutover_mount_persistability(
    value: Any,
) -> BusinessCutoverPersistabilityProfile:
    phase = BusinessCutoverPersistabilityPhase.MOUNT
    plan = _revalidate_plan(
        value,
        _require_business_cutover_mount_plan,
        phase,
    )
    return _profile(
        phase=phase,
        cutover_id=plan.cutover_id,
        payloads=(
            (
                BusinessCutoverPersistabilityCarrierKind.MANIFEST,
                0,
                plan.evidence.manifest_bytes,
            ),
        ),
    )


def _require_business_cutover_head_persistability(
    value: Any,
) -> BusinessCutoverPersistabilityProfile:
    phase = BusinessCutoverPersistabilityPhase.HEAD
    chain = _revalidate_plan(
        value,
        _require_business_cutover_head_chain,
        phase,
    )
    return _profile(
        phase=phase,
        cutover_id=chain[0].cutover_id,
        payloads=tuple(
            (
                BusinessCutoverPersistabilityCarrierKind.PHASE_HEAD_MANIFEST,
                state.phase_revision,
                state.manifest_bytes,
            )
            for state in chain
        ),
    )


__all__ = ()
