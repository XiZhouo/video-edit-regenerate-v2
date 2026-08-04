"""Pure canonical hash chain for the five durable cutover-head phases.

The schema has always stored ``previous_head_sha256`` and ``head_sha256`` on
``business_cutover_head``.  This module gives those columns one frozen
meaning.  It performs no I/O and grants no runtime authority: future fixed-SQL
writers and fresh readers must both derive the same five states from the
immutable install, coverage, audit, activation, and mount evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import hashlib
import hmac
from typing import Any, Dict, Tuple

from business_codec import BusinessCodecError, encode_payload_bounded
from business_cutover_activation_contract import (
    BUSINESS_CUTOVER_ACTIVATION_TRANSACTION_KIND,
    BUSINESS_CUTOVER_ACTIVATION_TRANSACTION_PREFIX,
)
from business_cutover_asset_contract import (
    BUSINESS_CUTOVER_ASSET_COVERAGE_TRANSACTION_KIND,
    BUSINESS_CUTOVER_ASSET_COVERAGE_TRANSACTION_PREFIX,
)
from business_cutover_contract import (
    BUSINESS_CUTOVER_INSTALL_TRANSACTION_KIND,
    BUSINESS_CUTOVER_INSTALL_TRANSACTION_PREFIX,
)
from business_cutover_mount_contract import (
    BUSINESS_CUTOVER_MOUNT_TRANSACTION_KIND,
    BUSINESS_CUTOVER_MOUNT_TRANSACTION_PREFIX,
    BusinessCutoverMountEvidence,
)
from repository_audit_contract import (
    REPOSITORY_AUDIT_TRANSACTION_KIND,
    REPOSITORY_AUDIT_TRANSACTION_PREFIX,
)


BUSINESS_CUTOVER_HEAD_PROTOCOL = "ze.business-cutover.phase-head.v1"
BUSINESS_CUTOVER_HEAD_MANIFEST_SCHEMA = (
    "ze.business-cutover.phase-head-manifest.v1"
)
BUSINESS_CUTOVER_HEAD_HASH_DOMAIN = b"ze.business-cutover.phase-head.v1"
MAX_BUSINESS_CUTOVER_HEAD_MANIFEST_BYTES = 1024 * 1024
MAX_BUSINESS_CUTOVER_HEAD_MANIFEST_NODES = 10_000
MAX_BUSINESS_CUTOVER_HEAD_RUNTIME_GENERATION_BYTES = 256
EMPTY_BUSINESS_CUTOVER_HEAD_SHA256 = b"\0" * 32

_LOWER_HEX = frozenset("0123456789abcdef")
_PHASES = (
    (1, "installed", "read_only", 1),
    (2, "assets_covered", "read_only", 2),
    (3, "audited", "read_only", 3),
    (4, "activated", "active", 4),
    (5, "mounted", "active", 5),
)


class BusinessCutoverHeadIntegrityError(ValueError):
    """Stable, payload-free failure at the phase-head integrity boundary."""

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
    raise BusinessCutoverHeadIntegrityError(code, path)


def _digest(value: Any, path: str, *, allow_zero: bool = True) -> bytes:
    if type(value) is not bytes or len(value) != 32:
        _fail("business_cutover_head_digest_invalid", path)
    if not allow_zero and hmac.compare_digest(value, EMPTY_BUSINESS_CUTOVER_HEAD_SHA256):
        _fail("business_cutover_head_digest_invalid", path)
    return value


def _lower_hex(value: Any, path: str, length: int) -> str:
    if (
        type(value) is not str
        or len(value) != length
        or any(character not in _LOWER_HEX for character in value)
    ):
        _fail("business_cutover_head_hex_invalid", path)
    return value


def _text(value: Any, path: str, maximum: int) -> str:
    if type(value) is not str or not value:
        _fail("business_cutover_head_text_invalid", path)
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeEncodeError:
        _fail("business_cutover_head_text_invalid", path)
        raise AssertionError("unreachable")
    if len(encoded) > maximum:
        _fail("business_cutover_head_text_budget_exceeded", path)
    return value


def _timestamp(value: Any, path: str) -> str:
    if type(value) is not str or len(value) != 24:
        _fail("business_cutover_head_timestamp_invalid", path)
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except (TypeError, ValueError):
        _fail("business_cutover_head_timestamp_invalid", path)
        raise AssertionError("unreachable")
    canonical = parsed.strftime("%Y-%m-%dT%H:%M:%S.") + (
        "%03dZ" % (parsed.microsecond // 1000)
    )
    if canonical != value:
        _fail("business_cutover_head_timestamp_invalid", path)
    return value


def _framed_digest(domain: bytes, payload: bytes) -> bytes:
    hasher = hashlib.sha256()
    for value in (domain, payload):
        hasher.update(len(value).to_bytes(8, "big"))
        hasher.update(value)
    return hasher.digest()


class BusinessCutoverHeadPointerRole(str, Enum):
    INSTALL = "install"
    COVERAGE = "coverage"
    AUDIT = "audit"
    ACTIVATION = "activation"
    MOUNT = "mount"


_POINTER_IDENTITIES = {
    BusinessCutoverHeadPointerRole.INSTALL: (
        BUSINESS_CUTOVER_INSTALL_TRANSACTION_PREFIX,
        BUSINESS_CUTOVER_INSTALL_TRANSACTION_KIND,
        64,
    ),
    BusinessCutoverHeadPointerRole.COVERAGE: (
        BUSINESS_CUTOVER_ASSET_COVERAGE_TRANSACTION_PREFIX,
        BUSINESS_CUTOVER_ASSET_COVERAGE_TRANSACTION_KIND,
        64,
    ),
    BusinessCutoverHeadPointerRole.AUDIT: (
        REPOSITORY_AUDIT_TRANSACTION_PREFIX,
        REPOSITORY_AUDIT_TRANSACTION_KIND,
        32,
    ),
    BusinessCutoverHeadPointerRole.ACTIVATION: (
        BUSINESS_CUTOVER_ACTIVATION_TRANSACTION_PREFIX,
        BUSINESS_CUTOVER_ACTIVATION_TRANSACTION_KIND,
        64,
    ),
    BusinessCutoverHeadPointerRole.MOUNT: (
        BUSINESS_CUTOVER_MOUNT_TRANSACTION_PREFIX,
        BUSINESS_CUTOVER_MOUNT_TRANSACTION_KIND,
        64,
    ),
}


@dataclass(frozen=True)
class BusinessCutoverHeadPointer:
    role: BusinessCutoverHeadPointerRole
    transaction_id: str
    receipt_kind: str
    occurred_at: str
    manifest_sha256: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.role) is not BusinessCutoverHeadPointerRole:
            _fail("business_cutover_head_pointer_invalid", "$.pointer.role")
        prefix, kind, suffix_length = _POINTER_IDENTITIES[self.role]
        _text(self.transaction_id, "$.pointer.transaction_id", 1024 * 1024)
        _text(self.receipt_kind, "$.pointer.receipt_kind", 1024 * 1024)
        if (
            not self.transaction_id.startswith(prefix)
            or self.receipt_kind != kind
        ):
            _fail("business_cutover_head_pointer_invalid", "$.pointer")
        _lower_hex(
            self.transaction_id[len(prefix) :],
            "$.pointer.transaction_id",
            suffix_length,
        )
        _timestamp(self.occurred_at, "$.pointer.occurred_at")
        _digest(self.manifest_sha256, "$.pointer.manifest_sha256", allow_zero=False)

    def canonical_value(self) -> Dict[str, Any]:
        return {
            "role": self.role.value,
            "transaction_id": self.transaction_id,
            "receipt_kind": self.receipt_kind,
            "occurred_at": self.occurred_at,
            "manifest_sha256": self.manifest_sha256.hex(),
        }


def _require_business_cutover_head_pointer(
    value: Any,
    path: str,
    *,
    expected_role: BusinessCutoverHeadPointerRole | None = None,
    cutover_id: str | None = None,
) -> BusinessCutoverHeadPointer:
    """Revalidate one typed pointer, including post-construction mutation."""

    if type(value) is not BusinessCutoverHeadPointer:
        _fail("business_cutover_head_pointer_invalid", path)
    if expected_role is not None and value.role is not expected_role:
        _fail("business_cutover_head_transition_invalid", path + ".role")
    try:
        rebuilt = BusinessCutoverHeadPointer(
            role=value.role,
            transaction_id=value.transaction_id,
            receipt_kind=value.receipt_kind,
            occurred_at=value.occurred_at,
            manifest_sha256=value.manifest_sha256,
        )
    except (
        AttributeError,
        IndexError,
        KeyError,
        TypeError,
        BusinessCutoverHeadIntegrityError,
    ) as exc:
        _fail("business_cutover_head_pointer_invalid", path)
        raise AssertionError("unreachable") from exc
    if rebuilt != value:
        _fail("business_cutover_head_pointer_invalid", path)
    if (
        cutover_id is not None
        and value.role is not BusinessCutoverHeadPointerRole.AUDIT
        and value.transaction_id.rsplit(":", 1)[-1] != cutover_id
    ):
        _fail("business_cutover_head_cutover_binding_invalid", path)
    return value


@dataclass(frozen=True)
class BusinessCutoverHeadInput:
    cutover_id: str
    repository_epoch: str
    runtime_generation: str
    code_digest: bytes = field(repr=False)
    install: BusinessCutoverHeadPointer
    coverage: BusinessCutoverHeadPointer
    audit: BusinessCutoverHeadPointer
    activation: BusinessCutoverHeadPointer
    mount: BusinessCutoverHeadPointer

    def __post_init__(self) -> None:
        _lower_hex(self.cutover_id, "$.input.cutover_id", 64)
        _lower_hex(self.repository_epoch, "$.input.repository_epoch", 64)
        _text(
            self.runtime_generation,
            "$.input.runtime_generation",
            MAX_BUSINESS_CUTOVER_HEAD_RUNTIME_GENERATION_BYTES,
        )
        _digest(self.code_digest, "$.input.code_digest")
        pointers = (self.install, self.coverage, self.audit, self.activation, self.mount)
        expected_roles = tuple(BusinessCutoverHeadPointerRole)
        if (
            any(type(value) is not BusinessCutoverHeadPointer for value in pointers)
            or tuple(value.role for value in pointers) != expected_roles
        ):
            _fail("business_cutover_head_pointer_order_invalid", "$.input")
        for role, value in zip(expected_roles, pointers):
            _require_business_cutover_head_pointer(
                value,
                "$.input." + role.value,
                expected_role=role,
                cutover_id=self.cutover_id,
            )
        if not (
            self.coverage.occurred_at <= self.install.occurred_at
            <= self.audit.occurred_at <= self.activation.occurred_at
            <= self.mount.occurred_at
        ):
            _fail("business_cutover_head_chronology_invalid", "$.input")

    @property
    def pointers(self) -> Tuple[BusinessCutoverHeadPointer, ...]:
        return (self.install, self.coverage, self.audit, self.activation, self.mount)


def _state_manifest_value(
    *,
    cutover_id: str,
    repository_epoch: str,
    runtime_generation: str,
    code_digest: bytes,
    state_revision: int,
    phase_revision: int,
    phase: str,
    authority_state: str,
    pointers: Tuple[BusinessCutoverHeadPointer, ...],
    previous_head_sha256: bytes,
    updated_at: str,
) -> Dict[str, Any]:
    return {
        "schema": BUSINESS_CUTOVER_HEAD_MANIFEST_SCHEMA,
        "protocol": BUSINESS_CUTOVER_HEAD_PROTOCOL,
        "identity": {
            "cutover_id": cutover_id,
            "repository_epoch": repository_epoch,
            "runtime_generation": runtime_generation,
            "code_digest": code_digest.hex(),
        },
        "state_revision": state_revision,
        "phase_revision": phase_revision,
        "phase": phase,
        "authority_state": authority_state,
        "pointers": [value.canonical_value() for value in pointers],
        "previous_head_sha256": previous_head_sha256.hex(),
        "updated_at": updated_at,
    }


def _manifest_bytes(value: Dict[str, Any]) -> bytes:
    try:
        return encode_payload_bounded(
            value,
            MAX_BUSINESS_CUTOVER_HEAD_MANIFEST_BYTES,
            MAX_BUSINESS_CUTOVER_HEAD_MANIFEST_NODES,
        )
    except BusinessCodecError as exc:
        _fail("business_cutover_head_manifest_invalid", "$.state")
        raise AssertionError("unreachable") from exc


def _state_digest(**values: Any) -> bytes:
    return _framed_digest(
        BUSINESS_CUTOVER_HEAD_HASH_DOMAIN,
        _manifest_bytes(_state_manifest_value(**values)),
    )


@dataclass(frozen=True)
class BusinessCutoverHeadState:
    cutover_id: str
    state_revision: int
    phase_revision: int
    phase: str
    authority_state: str
    repository_epoch: str
    runtime_generation: str
    code_digest: bytes = field(repr=False)
    pointers: Tuple[BusinessCutoverHeadPointer, ...]
    previous_head_sha256: bytes = field(repr=False)
    head_sha256: bytes = field(repr=False)
    updated_at: str

    def __post_init__(self) -> None:
        _lower_hex(self.cutover_id, "$.state.cutover_id", 64)
        _lower_hex(self.repository_epoch, "$.state.repository_epoch", 64)
        _text(
            self.runtime_generation,
            "$.state.runtime_generation",
            MAX_BUSINESS_CUTOVER_HEAD_RUNTIME_GENERATION_BYTES,
        )
        _digest(self.code_digest, "$.state.code_digest")
        if (
            type(self.phase_revision) is not int
            or not 1 <= self.phase_revision <= len(_PHASES)
            or type(self.state_revision) is not int
            or self.state_revision != self.phase_revision
        ):
            _fail("business_cutover_head_revision_invalid", "$.state")
        expected_revision, expected_phase, expected_authority, pointer_count = (
            _PHASES[self.phase_revision - 1]
        )
        if (
            expected_revision != self.phase_revision
            or self.phase != expected_phase
            or self.authority_state != expected_authority
            or type(self.pointers) is not tuple
            or len(self.pointers) != pointer_count
        ):
            _fail("business_cutover_head_phase_invalid", "$.state")
        expected_roles = tuple(BusinessCutoverHeadPointerRole)[:pointer_count]
        if (
            any(
                type(value) is not BusinessCutoverHeadPointer
                for value in self.pointers
            )
            or tuple(value.role for value in self.pointers) != expected_roles
        ):
            _fail("business_cutover_head_phase_invalid", "$.state")
        for role, value in zip(expected_roles, self.pointers):
            _require_business_cutover_head_pointer(
                value,
                "$.state.pointers." + role.value,
                expected_role=role,
                cutover_id=self.cutover_id,
            )
        install = self.pointers[0]
        if pointer_count >= 2 and self.pointers[1].occurred_at > install.occurred_at:
            _fail("business_cutover_head_chronology_invalid", "$.state.pointers")
        prior_time = install.occurred_at
        for value in self.pointers[2:]:
            if value.occurred_at < prior_time:
                _fail("business_cutover_head_chronology_invalid", "$.state.pointers")
            prior_time = value.occurred_at
        expected_updated_at = (
            install.occurred_at
            if self.phase_revision <= 2
            else self.pointers[-1].occurred_at
        )
        if self.updated_at != expected_updated_at:
            _fail("business_cutover_head_chronology_invalid", "$.state.updated_at")
        _digest(self.previous_head_sha256, "$.state.previous_head_sha256")
        previous_is_empty = hmac.compare_digest(
            self.previous_head_sha256,
            EMPTY_BUSINESS_CUTOVER_HEAD_SHA256,
        )
        if previous_is_empty != (self.phase_revision == 1):
            _fail(
                "business_cutover_head_predecessor_invalid",
                "$.state.previous_head_sha256",
            )
        _digest(self.head_sha256, "$.state.head_sha256", allow_zero=False)
        _timestamp(self.updated_at, "$.state.updated_at")
        expected = _state_digest(
            cutover_id=self.cutover_id,
            repository_epoch=self.repository_epoch,
            runtime_generation=self.runtime_generation,
            code_digest=self.code_digest,
            state_revision=self.state_revision,
            phase_revision=self.phase_revision,
            phase=self.phase,
            authority_state=self.authority_state,
            pointers=self.pointers,
            previous_head_sha256=self.previous_head_sha256,
            updated_at=self.updated_at,
        )
        if not hmac.compare_digest(expected, self.head_sha256):
            _fail("business_cutover_head_digest_mismatch", "$.state.head_sha256")

    @property
    def manifest_bytes(self) -> bytes:
        return _manifest_bytes(_state_manifest_value(
            cutover_id=self.cutover_id,
            repository_epoch=self.repository_epoch,
            runtime_generation=self.runtime_generation,
            code_digest=self.code_digest,
            state_revision=self.state_revision,
            phase_revision=self.phase_revision,
            phase=self.phase,
            authority_state=self.authority_state,
            pointers=self.pointers,
            previous_head_sha256=self.previous_head_sha256,
            updated_at=self.updated_at,
        ))

    def row_values(self) -> Dict[str, Any]:
        values: Dict[str, Any] = {
            "singleton": 1,
            "cutover_id": self.cutover_id,
            "state_revision": self.state_revision,
            "phase_revision": self.phase_revision,
            "phase": self.phase,
            "authority_state": self.authority_state,
            "repository_epoch": self.repository_epoch,
            "runtime_generation": self.runtime_generation,
            "code_digest": self.code_digest,
            "previous_head_sha256": self.previous_head_sha256,
            "head_sha256": self.head_sha256,
            "updated_at": self.updated_at,
        }
        by_role = {value.role: value for value in self.pointers}
        for role in BusinessCutoverHeadPointerRole:
            pointer = by_role.get(role)
            values[role.value + "_transaction_id"] = (
                None if pointer is None else pointer.transaction_id
            )
            values[role.value + "_manifest_sha256"] = (
                None if pointer is None else pointer.manifest_sha256
            )
        return values


def _validated_business_cutover_head_identity(
    *,
    cutover_id: Any,
    repository_epoch: Any,
    runtime_generation: Any,
    code_digest: Any,
    path: str,
) -> Tuple[str, str, str, bytes]:
    return (
        _lower_hex(cutover_id, path + ".cutover_id", 64),
        _lower_hex(repository_epoch, path + ".repository_epoch", 64),
        _text(
            runtime_generation,
            path + ".runtime_generation",
            MAX_BUSINESS_CUTOVER_HEAD_RUNTIME_GENERATION_BYTES,
        ),
        _digest(code_digest, path + ".code_digest"),
    )


def _issue_business_cutover_head_state(
    identity: Tuple[str, str, str, bytes],
    pointers: Tuple[BusinessCutoverHeadPointer, ...],
    previous_head_sha256: bytes,
) -> BusinessCutoverHeadState:
    phase_revision = len(pointers)
    if not 1 <= phase_revision <= len(_PHASES):
        _fail("business_cutover_head_transition_invalid", "$.transition")
    _, phase, authority_state, pointer_count = _PHASES[phase_revision - 1]
    if pointer_count != phase_revision:
        raise AssertionError("phase pointer cardinality drift")
    updated_at = (
        pointers[0].occurred_at
        if phase_revision <= 2
        else pointers[-1].occurred_at
    )
    cutover_id, repository_epoch, runtime_generation, code_digest = identity
    head = _state_digest(
        cutover_id=cutover_id,
        repository_epoch=repository_epoch,
        runtime_generation=runtime_generation,
        code_digest=code_digest,
        state_revision=phase_revision,
        phase_revision=phase_revision,
        phase=phase,
        authority_state=authority_state,
        pointers=pointers,
        previous_head_sha256=previous_head_sha256,
        updated_at=updated_at,
    )
    return BusinessCutoverHeadState(
        cutover_id=cutover_id,
        state_revision=phase_revision,
        phase_revision=phase_revision,
        phase=phase,
        authority_state=authority_state,
        repository_epoch=repository_epoch,
        runtime_generation=runtime_generation,
        code_digest=code_digest,
        pointers=pointers,
        previous_head_sha256=previous_head_sha256,
        head_sha256=head,
        updated_at=updated_at,
    )


def _issue_initial_business_cutover_head(
    *,
    cutover_id: str,
    repository_epoch: str,
    runtime_generation: str,
    code_digest: bytes,
    install: BusinessCutoverHeadPointer,
) -> BusinessCutoverHeadState:
    """Issue the exact phase-1 head without reading or writing any carrier."""

    identity = _validated_business_cutover_head_identity(
        cutover_id=cutover_id,
        repository_epoch=repository_epoch,
        runtime_generation=runtime_generation,
        code_digest=code_digest,
        path="$.initial.identity",
    )
    pointer = _require_business_cutover_head_pointer(
        install,
        "$.initial.install",
        expected_role=BusinessCutoverHeadPointerRole.INSTALL,
        cutover_id=cutover_id,
    )
    return _issue_business_cutover_head_state(
        identity,
        (pointer,),
        EMPTY_BUSINESS_CUTOVER_HEAD_SHA256,
    )


def _rebuild_business_cutover_head_prefix(
    value: BusinessCutoverHeadState,
) -> BusinessCutoverHeadState:
    identity = _validated_business_cutover_head_identity(
        cutover_id=value.cutover_id,
        repository_epoch=value.repository_epoch,
        runtime_generation=value.runtime_generation,
        code_digest=value.code_digest,
        path="$.predecessor.identity",
    )
    pointers = tuple(
        _require_business_cutover_head_pointer(
            pointer,
            "$.predecessor.pointers." + role.value,
            expected_role=role,
            cutover_id=value.cutover_id,
        )
        for role, pointer in zip(
            tuple(BusinessCutoverHeadPointerRole)[: value.phase_revision],
            value.pointers,
        )
    )
    previous = EMPTY_BUSINESS_CUTOVER_HEAD_SHA256
    rebuilt = None
    for pointer_count in range(1, len(pointers) + 1):
        rebuilt = _issue_business_cutover_head_state(
            identity,
            pointers[:pointer_count],
            previous,
        )
        previous = rebuilt.head_sha256
    if rebuilt is None:
        _fail("business_cutover_head_predecessor_invalid", "$.predecessor")
    return rebuilt


def _require_exact_business_cutover_head_predecessor(
    value: Any,
) -> BusinessCutoverHeadState:
    if type(value) is not BusinessCutoverHeadState:
        _fail("business_cutover_head_predecessor_invalid", "$.predecessor")
    try:
        rebuilt = _rebuild_business_cutover_head_prefix(value)
    except (
        AttributeError,
        IndexError,
        KeyError,
        TypeError,
        BusinessCutoverHeadIntegrityError,
    ) as exc:
        _fail("business_cutover_head_predecessor_invalid", "$.predecessor")
        raise AssertionError("unreachable") from exc
    if rebuilt != value:
        _fail("business_cutover_head_predecessor_invalid", "$.predecessor")
    return value


def _advance_business_cutover_head(
    previous: BusinessCutoverHeadState,
    *,
    cutover_id: str,
    repository_epoch: str,
    runtime_generation: str,
    code_digest: bytes,
    pointer: BusinessCutoverHeadPointer,
) -> BusinessCutoverHeadState:
    """Advance one, and only one, phase from an exact canonical predecessor."""

    predecessor = _require_exact_business_cutover_head_predecessor(previous)
    if predecessor.phase_revision >= len(_PHASES):
        _fail("business_cutover_head_transition_invalid", "$.transition")
    identity = _validated_business_cutover_head_identity(
        cutover_id=cutover_id,
        repository_epoch=repository_epoch,
        runtime_generation=runtime_generation,
        code_digest=code_digest,
        path="$.transition.identity",
    )
    predecessor_identity = (
        predecessor.cutover_id,
        predecessor.repository_epoch,
        predecessor.runtime_generation,
        predecessor.code_digest,
    )
    if (
        identity[:3] != predecessor_identity[:3]
        or not hmac.compare_digest(identity[3], predecessor_identity[3])
    ):
        _fail(
            "business_cutover_head_identity_transition_invalid",
            "$.transition.identity",
        )
    expected_role = tuple(BusinessCutoverHeadPointerRole)[
        predecessor.phase_revision
    ]
    next_pointer = _require_business_cutover_head_pointer(
        pointer,
        "$.transition.pointer",
        expected_role=expected_role,
        cutover_id=cutover_id,
    )
    return _issue_business_cutover_head_state(
        identity,
        predecessor.pointers + (next_pointer,),
        predecessor.head_sha256,
    )


def issue_business_cutover_head_chain(
    value: BusinessCutoverHeadInput,
) -> Tuple[BusinessCutoverHeadState, ...]:
    if type(value) is not BusinessCutoverHeadInput:
        _fail("business_cutover_head_input_invalid", "$.input")
    identity = {
        "cutover_id": value.cutover_id,
        "repository_epoch": value.repository_epoch,
        "runtime_generation": value.runtime_generation,
        "code_digest": value.code_digest,
    }
    states = [_issue_initial_business_cutover_head(
        **identity,
        install=value.install,
    )]
    for pointer in value.pointers[1:]:
        states.append(_advance_business_cutover_head(
            states[-1],
            **identity,
            pointer=pointer,
        ))
    return tuple(states)


def issue_business_cutover_head_chain_from_mount(
    value: BusinessCutoverMountEvidence,
) -> Tuple[BusinessCutoverHeadState, ...]:
    """Project one already-validated final mount into its five phase heads.

    This helper still grants no authority.  The caller remains responsible for
    obtaining ``value`` from the mount integrity issuer or a strict durable
    reader; the returned chain is only the canonical pure projection.
    """

    if type(value) is not BusinessCutoverMountEvidence:
        _fail("business_cutover_head_mount_invalid", "$.mount")
    return issue_business_cutover_head_chain(BusinessCutoverHeadInput(
        cutover_id=value.cutover_id,
        repository_epoch=value.runtime.repository_epoch,
        runtime_generation=value.runtime.runtime_generation,
        code_digest=value.runtime.code_digest,
        install=BusinessCutoverHeadPointer(
            BusinessCutoverHeadPointerRole.INSTALL,
            value.install.transaction_id,
            value.install.receipt_kind,
            value.install.committed_at,
            value.install.manifest_sha256,
        ),
        coverage=BusinessCutoverHeadPointer(
            BusinessCutoverHeadPointerRole.COVERAGE,
            value.coverage.transaction_id,
            value.coverage.receipt_kind,
            value.coverage.committed_at,
            value.coverage.manifest_sha256,
        ),
        audit=BusinessCutoverHeadPointer(
            BusinessCutoverHeadPointerRole.AUDIT,
            value.activation.audit_transaction_id,
            value.activation.audit_receipt_kind,
            value.activation.audit_completed_at,
            value.activation.audit_manifest_sha256,
        ),
        activation=BusinessCutoverHeadPointer(
            BusinessCutoverHeadPointerRole.ACTIVATION,
            value.activation.transaction_id,
            value.activation.receipt_kind,
            value.activation.activated_at,
            value.activation.manifest_sha256,
        ),
        mount=BusinessCutoverHeadPointer(
            BusinessCutoverHeadPointerRole.MOUNT,
            value.transaction_id,
            value.receipt_kind,
            value.mounted_at,
            value.manifest_sha256,
        ),
    ))


def _require_business_cutover_head_chain(
    value: Any,
) -> Tuple[BusinessCutoverHeadState, ...]:
    if (
        type(value) is not tuple
        or len(value) != len(_PHASES)
        or any(type(item) is not BusinessCutoverHeadState for item in value)
    ):
        _fail("business_cutover_head_chain_invalid", "$.chain")
    final = value[-1]
    by_role = {pointer.role: pointer for pointer in final.pointers}
    try:
        rebuilt = issue_business_cutover_head_chain(BusinessCutoverHeadInput(
            cutover_id=final.cutover_id,
            repository_epoch=final.repository_epoch,
            runtime_generation=final.runtime_generation,
            code_digest=final.code_digest,
            install=by_role[BusinessCutoverHeadPointerRole.INSTALL],
            coverage=by_role[BusinessCutoverHeadPointerRole.COVERAGE],
            audit=by_role[BusinessCutoverHeadPointerRole.AUDIT],
            activation=by_role[BusinessCutoverHeadPointerRole.ACTIVATION],
            mount=by_role[BusinessCutoverHeadPointerRole.MOUNT],
        ))
    except (KeyError, BusinessCutoverHeadIntegrityError) as exc:
        _fail("business_cutover_head_chain_invalid", "$.chain")
        raise AssertionError("unreachable") from exc
    if rebuilt != value:
        _fail("business_cutover_head_chain_invalid", "$.chain")
    return value


__all__ = ()
