"""Strict fresh reader for current mounted production authority.

Structural joins are not authorization.  This reader decodes the persisted
mount manifest, rebuilds and domain-hashes the pure mount evidence, compares
every linked durable receipt, and only then stamps a private fresh wrapper.
Ordinary construction of :class:`ProductionMountedAuthorityEvidence` cannot
mint the capability consumed by the production plan issuer.

Schema-v9 and schema-v10 are additive to the frozen schema-v8 mount substrate.
A mount sealed against the exact v8 or v9 prefix therefore remains a
historically targeted fact only while the running registry is still the exact
v10 registry.  Schema-v11 corrects mount chronology and schema-v12 adds the
install closure required before production mounting.  Both current targets are
accepted only under their exact frozen registry identity; neither reader ever
pretends that an already-mounted older database crossed a clean-only upgrade
precondition or rewrites the persisted target into the current version.
"""

from __future__ import annotations

import hmac
from typing import Any, NamedTuple, Sequence, Tuple

from business_codec import BusinessCodecError, decode_payload_bounded
from business_cutover_contract import (
    BUSINESS_CUTOVER_PROTOCOL,
    BusinessCutoverAdmission,
    BusinessCutoverContractError,
    BusinessCutoverRuntimeBinding,
)
from business_cutover_asset_contract import (
    BUSINESS_CUTOVER_ASSET_COVERAGE_PROTOCOL,
    BusinessCutoverAssetContractError,
)
from business_cutover_stored_manifest import (
    BusinessCutoverStoredManifestError,
    _parse_stored_coverage_manifest,
    _parse_stored_install_manifest,
    _StoredCoverageProjection,
    _StoredInstallProjection,
)
from business_cutover_mount_contract import (
    MAX_BUSINESS_CUTOVER_MOUNT_MANIFEST_BYTES,
    MAX_BUSINESS_CUTOVER_MOUNT_MANIFEST_NODES,
    BusinessCutoverMountActivationBinding,
    BusinessCutoverMountContractError,
    BusinessCutoverMountCoverageBinding,
    BusinessCutoverMountEvidence,
    BusinessCutoverMountGenericReceiptEvidence,
    BusinessCutoverMountInstallBinding,
    BusinessCutoverMountRuntimeIdentity,
)
from business_cutover_mount_integrity import (
    BusinessCutoverMountIntegrityError,
    _require_business_cutover_mount_evidence,
)
from business_cutover_head_integrity import (
    BusinessCutoverHeadIntegrityError,
    issue_business_cutover_head_chain_from_mount,
)
from repository_audit_integrity import (
    RepositoryAuditIntegrityError,
    _certificate_tail_digest as _audit_certificate_tail_digest,
    _findings_digest as _audit_findings_digest,
    _manifest_digest as _audit_manifest_digest,
    _require_repository_audit_certificate_evidence,
)
from business_cutover_activation_integrity import (
    BusinessCutoverActivationIntegrityError,
    _manifest_digest as _activation_manifest_digest,
    _require_business_cutover_activation_evidence,
)
from business_cutover_activation_contract import (
    MAX_BUSINESS_CUTOVER_ACTIVATION_MANIFEST_BYTES,
    MAX_BUSINESS_CUTOVER_ACTIVATION_MANIFEST_NODES,
    BusinessCutoverActivationContractError,
    BusinessCutoverActivationEvidence,
    BusinessCutoverActivationGenericReceiptEvidence,
)
from repository_audit_contract import (
    EMPTY_REPOSITORY_AUDIT_CERTIFICATE_TAIL_SHA256,
    MAX_REPOSITORY_AUDIT_MANIFEST_BYTES,
    MAX_REPOSITORY_AUDIT_MANIFEST_NODES,
    RepositoryAuditAuthorityState,
    RepositoryAuditCertificateEvidence,
    RepositoryAuditContractError,
    RepositoryAuditCoverage,
    RepositoryAuditFindings,
    RepositoryAuditGenericReceiptEvidence,
    RepositoryAuditPurpose,
    RepositoryAuditRuntimeBinding,
    RepositoryAuditSchemaObservation,
)
from production_atomic_write_contract import (
    ProductionMountedAuthorityEvidence,
    ProductionOutboxDispatchHeadEvidence,
    ProductionOutboxAllocationHead,
    ProductionWorkerLedgerHeadEvidence,
)
from sqlite_business_runtime import (
    BusinessRuntimeAccess,
    _validate_business_runtime_connection,
)
from sqlite_cutover_install_closure_reader import (
    SQLiteCutoverInstallClosureError,
    _read_business_cutover_install_closure,
    _require_cutover_install_closure_binding,
)
from sqlite_cutover_phase_binding_reader import (
    SQLiteCutoverPhaseBindingReaderError,
    _read_cutover_phase_binding_prefix,
)
from sqlite_kernel import SqliteDatabase, _expected_schema_fingerprint
from sqlite_schema import MIGRATIONS, _expected_migration_ledger_sha256
from worker_execution_contract import (
    WorkerLedgerHead,
    WorkerOutboxDispatchCursor,
    WorkerOutboxDispatchState,
    WorkerOutboxOrder,
)


_FRESH_MOUNT_ISSUER = object()
_FRESH_WORKER_LEDGER_HEAD_ISSUER = object()
_FRESH_OUTBOX_DISPATCH_HEAD_ISSUER = object()

_SCHEMA_V8_VERSION = 8
_SCHEMA_V8_MIGRATION_NAME = "production_authority_worker_runtime_v1"
_SCHEMA_V8_MIGRATION_CHECKSUM = (
    "f6c8a8e1f2aa633a52da52a04e6b58ad"
    "11230f5093e4b883e7b63674bb8ad80b"
)
_SCHEMA_V8_FINGERPRINT = (
    "0d96ffa4e88ac3c3d3fd1ba1b6f09f11"
    "a3a2d7a0e1861f5781bf0856d7fce771"
)
_SCHEMA_V8_MIGRATION_ROOT = bytes.fromhex(
    "9595ea544b5b055f71d2c93619e49b33"
    "53a5d48424951fc410b0ca2484c1834a"
)
_SCHEMA_V9_VERSION = 9
_SCHEMA_V9_MIGRATION_NAME = "worker_payload_projection_sidecars_v1"
_SCHEMA_V9_MIGRATION_CHECKSUM = (
    "23018e6b59b8d3ad235e3110abd122ba"
    "12584cee272cff0614730f4d4be0dba0"
)
_SCHEMA_V9_FINGERPRINT = (
    "ccb5b4d248c2c429c0a99a5811816521"
    "cf4563de3bee457db826b4e4d4090ddf"
)
_SCHEMA_V10_VERSION = 10
_SCHEMA_V10_MIGRATION_NAME = "runtime_timestamp_dispatch_hardening_v1"
_SCHEMA_V10_MIGRATION_CHECKSUM = (
    "1be88f2630f37ca45d1e84ea7547b78e"
    "bc9da48385774d7e279b74721af81baa"
)
_SCHEMA_V10_FINGERPRINT = (
    "4ef1cb22745c3982b00077762a741371a"
    "dbc17ca057bf908eb18a1642a45bc5e"
)
_SCHEMA_V11_VERSION = 11
_SCHEMA_V11_MIGRATION_NAME = "mount_chronology_state_closure_v1"
_SCHEMA_V11_MIGRATION_CHECKSUM = (
    "48de0b2513175f8575a7127e54f68492"
    "2db4eb687739e0ebbab93a0a524f54cb"
)
_SCHEMA_V11_FINGERPRINT = (
    "d92e9a6ac8c58ef6ed598a4c53bc972"
    "239f93b8e8c2a4d8a7e54369e3dc1c74f"
)
_SCHEMA_V11_MIGRATION_ROOT = bytes.fromhex(
    "39c8bc8f749de98b64d7a89b27a2d849"
    "6f46f39e1edb3e5ff0be83599314de70"
)
_SCHEMA_V12_VERSION = 12
_SCHEMA_V12_MIGRATION_NAME = "cutover_install_closure_v1"
_SCHEMA_V12_MIGRATION_CHECKSUM = (
    "aa61960d2bd4e10a41d1ef51767bf4f4"
    "809f5fab26cae5ebef3bfa30fccf35a2"
)
_SCHEMA_V12_FINGERPRINT = (
    "4c265275dcf7d90e4ee77fda3b3a1749"
    "646e5e55783ce17fdff40e737195b226"
)
_SCHEMA_V12_MIGRATION_ROOT = bytes.fromhex(
    "1537102bc6fe371ceef49a58b566dbd6"
    "f86940eaf838dcfeb263e3df37cbb77e"
)
_SCHEMA_V13_VERSION = 13
_SCHEMA_V13_MIGRATION_NAME = "direct_predecessor_phase_bindings_v1"
_SCHEMA_V13_MIGRATION_CHECKSUM = (
    "c65b56a6fd74f9609dfb5d5295458e0d"
    "e473120c245881ac4da29abd0908860e"
)
_SCHEMA_V13_FINGERPRINT = (
    "28ea7b266fdce716b14b4002c3e5431d"
    "8984f51195aba838f392fa0060b25527"
)
_SCHEMA_V13_MIGRATION_ROOT = bytes.fromhex(
    "00eacfaef1b42673d0d22478eb0b3675"
    "eed3d4b42bb75b842e3a28b14355a23a"
)


class SQLiteProductionAuthorityError(ValueError):
    """Stable, payload-free mounted-authority read failure."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        if type(code) is not str or not code:
            raise TypeError("code must be a non-empty exact string")
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise SQLiteProductionAuthorityError(code)


def _same(left: Any, right: Any) -> bool:
    if type(left) is bytes and type(right) is bytes:
        return hmac.compare_digest(left, right)
    return type(left) is type(right) and left == right


def _historical_mount_prefixes_are_frozen_under_current_v10() -> bool:
    """Prove that historical v8/v9 mounts are meaningful under exact v10.

    The v8 cumulative migration root freezes the complete first-eight registry,
    while its terminal checksum and schema fingerprint freeze the published v8
    migration and resulting DDL independently.  The exact v9 and v10 tails,
    including their resulting fingerprints, prevent accepting an old target
    merely because a tuple still happens to contain migrations numbered
    ``8`` through ``10``.
    """

    try:
        if (
            type(MIGRATIONS) is not tuple
            or len(MIGRATIONS) != _SCHEMA_V10_VERSION
            or tuple(item.version for item in MIGRATIONS)
            != tuple(range(1, _SCHEMA_V10_VERSION + 1))
        ):
            return False
        v8 = MIGRATIONS[_SCHEMA_V8_VERSION - 1]
        v9 = MIGRATIONS[_SCHEMA_V9_VERSION - 1]
        v10 = MIGRATIONS[_SCHEMA_V10_VERSION - 1]
        return bool(
            v8.version == _SCHEMA_V8_VERSION
            and v8.name == _SCHEMA_V8_MIGRATION_NAME
            and v8.checksum == _SCHEMA_V8_MIGRATION_CHECKSUM
            and _expected_schema_fingerprint(_SCHEMA_V8_VERSION)
            == _SCHEMA_V8_FINGERPRINT
            and hmac.compare_digest(
                _expected_migration_ledger_sha256(_SCHEMA_V8_VERSION),
                _SCHEMA_V8_MIGRATION_ROOT,
            )
            and v9.version == _SCHEMA_V9_VERSION
            and v9.name == _SCHEMA_V9_MIGRATION_NAME
            and v9.checksum == _SCHEMA_V9_MIGRATION_CHECKSUM
            and _expected_schema_fingerprint(_SCHEMA_V9_VERSION)
            == _SCHEMA_V9_FINGERPRINT
            and v10.version == _SCHEMA_V10_VERSION
            and v10.name == _SCHEMA_V10_MIGRATION_NAME
            and v10.checksum == _SCHEMA_V10_MIGRATION_CHECKSUM
            and _expected_schema_fingerprint(_SCHEMA_V10_VERSION)
            == _SCHEMA_V10_FINGERPRINT
        )
    except (AttributeError, IndexError, TypeError, ValueError):
        return False


def _current_v11_identity_is_frozen() -> bool:
    """Pin the only mount target accepted by the current v11 registry."""

    try:
        if (
            type(MIGRATIONS) is not tuple
            or len(MIGRATIONS) != _SCHEMA_V11_VERSION
            or tuple(item.version for item in MIGRATIONS)
            != tuple(range(1, _SCHEMA_V11_VERSION + 1))
        ):
            return False
        v11 = MIGRATIONS[_SCHEMA_V11_VERSION - 1]
        return bool(
            v11.name == _SCHEMA_V11_MIGRATION_NAME
            and v11.checksum == _SCHEMA_V11_MIGRATION_CHECKSUM
            and _expected_schema_fingerprint(_SCHEMA_V11_VERSION)
            == _SCHEMA_V11_FINGERPRINT
            and hmac.compare_digest(
                _expected_migration_ledger_sha256(_SCHEMA_V11_VERSION),
                _SCHEMA_V11_MIGRATION_ROOT,
            )
        )
    except (AttributeError, IndexError, TypeError, ValueError):
        return False


def _current_v12_identity_is_frozen() -> bool:
    """Pin the only mount target accepted by the current v12 registry."""

    try:
        if (
            type(MIGRATIONS) is not tuple
            or len(MIGRATIONS) != _SCHEMA_V12_VERSION
            or tuple(item.version for item in MIGRATIONS)
            != tuple(range(1, _SCHEMA_V12_VERSION + 1))
        ):
            return False
        v12 = MIGRATIONS[_SCHEMA_V12_VERSION - 1]
        return bool(
            v12.name == _SCHEMA_V12_MIGRATION_NAME
            and v12.checksum == _SCHEMA_V12_MIGRATION_CHECKSUM
            and _expected_schema_fingerprint(_SCHEMA_V12_VERSION)
            == _SCHEMA_V12_FINGERPRINT
            and hmac.compare_digest(
                _expected_migration_ledger_sha256(_SCHEMA_V12_VERSION),
                _SCHEMA_V12_MIGRATION_ROOT,
            )
        )
    except (AttributeError, IndexError, TypeError, ValueError):
        return False


def _current_v13_identity_is_frozen() -> bool:
    """Pin the additive direct-predecessor schema used by current fixtures."""

    try:
        if (
            type(MIGRATIONS) is not tuple
            or len(MIGRATIONS) != _SCHEMA_V13_VERSION
            or tuple(item.version for item in MIGRATIONS)
            != tuple(range(1, _SCHEMA_V13_VERSION + 1))
        ):
            return False
        v13 = MIGRATIONS[_SCHEMA_V13_VERSION - 1]
        return bool(
            v13.name == _SCHEMA_V13_MIGRATION_NAME
            and v13.checksum == _SCHEMA_V13_MIGRATION_CHECKSUM
            and _expected_schema_fingerprint(_SCHEMA_V13_VERSION)
            == _SCHEMA_V13_FINGERPRINT
            and hmac.compare_digest(
                _expected_migration_ledger_sha256(_SCHEMA_V13_VERSION),
                _SCHEMA_V13_MIGRATION_ROOT,
            )
        )
    except (AttributeError, IndexError, TypeError, ValueError):
        return False


def _mount_target_identity_is_compatible(
    target_schema_version: Any,
    target_schema_fingerprint: Any,
) -> bool:
    """Accept frozen current mounts, plus exact v8/v9 prefixes on v10."""

    if (
        type(target_schema_version) is not int
        or type(target_schema_fingerprint) is not str
        or type(MIGRATIONS) is not tuple
        or not MIGRATIONS
    ):
        return False
    current_version = MIGRATIONS[-1].version
    if target_schema_version == current_version:
        current_identity_is_frozen = (
            _historical_mount_prefixes_are_frozen_under_current_v10()
            if current_version == _SCHEMA_V10_VERSION
            else _current_v11_identity_is_frozen()
            if current_version == _SCHEMA_V11_VERSION
            else _current_v12_identity_is_frozen()
            if current_version == _SCHEMA_V12_VERSION
            else _current_v13_identity_is_frozen()
            if current_version == _SCHEMA_V13_VERSION
            else False
        )
        if not current_identity_is_frozen:
            return False
        return target_schema_fingerprint == _expected_schema_fingerprint()
    if (
        current_version != _SCHEMA_V10_VERSION
        or not _historical_mount_prefixes_are_frozen_under_current_v10()
    ):
        return False
    return bool(
        (
            target_schema_version == _SCHEMA_V8_VERSION
            and target_schema_fingerprint == _SCHEMA_V8_FINGERPRINT
        )
        or (
            target_schema_version == _SCHEMA_V9_VERSION
            and target_schema_fingerprint == _SCHEMA_V9_FINGERPRINT
        )
    )


def _row_is(row: Any, expected: Sequence[Any]) -> bool:
    return row is not None and len(row) == len(expected) and all(
        _same(row[index], value) for index, value in enumerate(expected)
    )


class ProductionMountedAuthorityBinding:
    """Unforgeable fresh wrapper issued only by this fixed SQLite reader."""

    __slots__ = ("evidence", "_issuer")

    def __init__(
        self,
        evidence: ProductionMountedAuthorityEvidence,
        *,
        _issuer: Any,
    ) -> None:
        if (
            _issuer is not _FRESH_MOUNT_ISSUER
            or type(evidence) is not ProductionMountedAuthorityEvidence
        ):
            _fail("production_mount_reader_issuer_invalid")
        object.__setattr__(self, "evidence", evidence)
        object.__setattr__(self, "_issuer", _issuer)

    def __setattr__(self, name: str, value: Any) -> None:
        del name, value
        raise AttributeError("ProductionMountedAuthorityBinding is immutable")


def _require_mounted_binding(value: Any) -> ProductionMountedAuthorityBinding:
    if (
        type(value) is not ProductionMountedAuthorityBinding
        or getattr(value, "_issuer", None) is not _FRESH_MOUNT_ISSUER
        or type(getattr(value, "evidence", None))
        is not ProductionMountedAuthorityEvidence
    ):
        _fail("production_mount_authorization_invalid")
    return value


class ProductionWorkerLedgerHeadBinding:
    """Unforgeable same-snapshot worker-ledger head observation."""

    __slots__ = ("evidence", "_issuer")

    def __init__(
        self,
        evidence: ProductionWorkerLedgerHeadEvidence,
        *,
        _issuer: Any,
    ) -> None:
        if (
            _issuer is not _FRESH_WORKER_LEDGER_HEAD_ISSUER
            or type(evidence) is not ProductionWorkerLedgerHeadEvidence
        ):
            _fail("production_worker_ledger_head_reader_issuer_invalid")
        object.__setattr__(self, "evidence", evidence)
        object.__setattr__(self, "_issuer", _issuer)

    def __setattr__(self, name: str, value: Any) -> None:
        del name, value
        raise AttributeError("ProductionWorkerLedgerHeadBinding is immutable")


def _require_worker_ledger_head_binding(
    value: Any,
) -> ProductionWorkerLedgerHeadBinding:
    if (
        type(value) is not ProductionWorkerLedgerHeadBinding
        or getattr(value, "_issuer", None)
        is not _FRESH_WORKER_LEDGER_HEAD_ISSUER
        or type(getattr(value, "evidence", None))
        is not ProductionWorkerLedgerHeadEvidence
    ):
        _fail("production_worker_ledger_head_authorization_invalid")
    return value


class ProductionOutboxDispatchHeadBinding:
    """Unforgeable same-snapshot outbox-dispatch head observation."""

    __slots__ = ("evidence", "_issuer")

    def __init__(
        self,
        evidence: ProductionOutboxDispatchHeadEvidence,
        *,
        _issuer: Any,
    ) -> None:
        if (
            _issuer is not _FRESH_OUTBOX_DISPATCH_HEAD_ISSUER
            or type(evidence) is not ProductionOutboxDispatchHeadEvidence
        ):
            _fail("production_outbox_dispatch_head_reader_issuer_invalid")
        object.__setattr__(self, "evidence", evidence)
        object.__setattr__(self, "_issuer", _issuer)

    def __setattr__(self, name: str, value: Any) -> None:
        del name, value
        raise AttributeError("ProductionOutboxDispatchHeadBinding is immutable")


def _require_outbox_dispatch_head_binding(
    value: Any,
) -> ProductionOutboxDispatchHeadBinding:
    if (
        type(value) is not ProductionOutboxDispatchHeadBinding
        or getattr(value, "_issuer", None)
        is not _FRESH_OUTBOX_DISPATCH_HEAD_ISSUER
        or type(getattr(value, "evidence", None))
        is not ProductionOutboxDispatchHeadEvidence
    ):
        _fail("production_outbox_dispatch_head_authorization_invalid")
    return value


def _exact_object(value: Any, keys: Tuple[str, ...]) -> dict:
    if type(value) is not dict or set(value) != set(keys):
        _fail("production_mount_manifest_invalid")
    return value


def _manifest_digest(value: Any) -> bytes:
    if type(value) is not str or len(value) != 64:
        _fail("production_mount_manifest_invalid")
    try:
        decoded = bytes.fromhex(value)
    except ValueError:
        _fail("production_mount_manifest_invalid")
        raise AssertionError("unreachable")
    if len(decoded) != 32:
        _fail("production_mount_manifest_invalid")
    return decoded


def _decode_exact_manifest(
    manifest_bytes: Any,
    maximum_bytes: int,
    maximum_nodes: int,
    root_keys: Tuple[str, ...],
) -> dict:
    root = decode_payload_bounded(
        manifest_bytes,
        maximum_bytes,
        maximum_nodes,
    )
    return _exact_object(root, root_keys)


def _business_runtime_from_manifest(value: Any) -> BusinessCutoverRuntimeBinding:
    runtime = _exact_object(value, (
        "authority", "authority_state", "repository_epoch", "cutover_id",
        "source_admission_revision", "repository_revision", "state_revision",
        "runtime_generation", "code_digest", "schema_version",
    ))
    return BusinessCutoverRuntimeBinding(
        authority=runtime["authority"],
        authority_state=runtime["authority_state"],
        repository_epoch=runtime["repository_epoch"],
        cutover_id=runtime["cutover_id"],
        source_admission_revision=runtime["source_admission_revision"],
        repository_revision=runtime["repository_revision"],
        state_revision=runtime["state_revision"],
        runtime_generation=runtime["runtime_generation"],
        code_digest=_manifest_digest(runtime["code_digest"]),
        schema_version=runtime["schema_version"],
    )


def _install_evidence_from_manifest(
    manifest_bytes: bytes,
) -> _StoredInstallProjection:
    try:
        return _parse_stored_install_manifest(manifest_bytes)
    except BusinessCutoverStoredManifestError as exc:
        raise SQLiteProductionAuthorityError(
            "production_mount_manifest_invalid"
        ) from exc


def _coverage_evidence_from_manifest(
    manifest_bytes: bytes,
) -> _StoredCoverageProjection:
    try:
        return _parse_stored_coverage_manifest(manifest_bytes)
    except BusinessCutoverStoredManifestError as exc:
        raise SQLiteProductionAuthorityError(
            "production_mount_manifest_invalid"
        ) from exc


class _AuditProjection(NamedTuple):
    evidence: RepositoryAuditCertificateEvidence
    findings_sha256: bytes


def _audit_evidence_from_manifest(manifest_bytes: bytes) -> _AuditProjection:
    root = _decode_exact_manifest(
        manifest_bytes,
        MAX_REPOSITORY_AUDIT_MANIFEST_BYTES,
        MAX_REPOSITORY_AUDIT_MANIFEST_NODES,
        (
            "schema", "protocol", "status", "certificate", "receipt_parent",
            "runtime", "schema_identity", "coverage", "findings",
            "predecessor",
        ),
    )
    certificate = _exact_object(
        root["certificate"], ("certificate_id", "sequence", "purpose")
    )
    receipt = _exact_object(root["receipt_parent"], (
        "ledger", "transaction_id", "kind", "committed_at",
        "specialised_protocol", "specialised_certificate_id",
    ))
    runtime = _exact_object(root["runtime"], (
        "authority", "authority_state", "repository_epoch", "cutover_id",
        "source_admission_revision", "repository_revision", "state_revision",
        "runtime_generation", "code_digest", "schema_version", "updated_at",
    ))
    schema = _exact_object(root["schema_identity"], (
        "user_version", "schema_fingerprint", "migration_ledger_count",
        "migration_ledger_sha256",
    ))
    coverage = _exact_object(root["coverage"], (
        "covered_business_revision", "worker_ledger_sequence",
        "worker_ledger_tail_sha256", "receipt_namespace_count",
        "receipt_namespace_sha256", "cutover_install_manifest_sha256",
    ))
    findings = _exact_object(root["findings"], (
        "page_integrity", "foreign_key_violation_count",
        "namespace_violation_count", "started_at", "completed_at",
    ))
    predecessor = root["predecessor"]
    if predecessor is not None:
        predecessor = _exact_object(predecessor, (
            "certificate_id", "sequence", "manifest_sha256",
            "certificate_tail_sha256",
        ))
    runtime_evidence = RepositoryAuditRuntimeBinding(
        authority=runtime["authority"],
        authority_state=RepositoryAuditAuthorityState(runtime["authority_state"]),
        repository_epoch=runtime["repository_epoch"],
        cutover_id=runtime["cutover_id"],
        source_admission_revision=runtime["source_admission_revision"],
        repository_revision=runtime["repository_revision"],
        state_revision=runtime["state_revision"],
        runtime_generation=runtime["runtime_generation"],
        code_digest=_manifest_digest(runtime["code_digest"]),
        schema_version=runtime["schema_version"],
        updated_at=runtime["updated_at"],
    )
    schema_evidence = RepositoryAuditSchemaObservation(
        user_version=schema["user_version"],
        schema_fingerprint=schema["schema_fingerprint"],
        migration_ledger_count=schema["migration_ledger_count"],
        migration_ledger_sha256=_manifest_digest(
            schema["migration_ledger_sha256"]
        ),
    )
    coverage_evidence = RepositoryAuditCoverage(
        covered_business_revision=coverage["covered_business_revision"],
        worker_ledger_sequence=coverage["worker_ledger_sequence"],
        worker_ledger_tail_sha256=_manifest_digest(
            coverage["worker_ledger_tail_sha256"]
        ),
        receipt_namespace_count=coverage["receipt_namespace_count"],
        receipt_namespace_sha256=_manifest_digest(
            coverage["receipt_namespace_sha256"]
        ),
        cutover_install_manifest_sha256=(
            None
            if coverage["cutover_install_manifest_sha256"] is None
            else _manifest_digest(coverage["cutover_install_manifest_sha256"])
        ),
    )
    findings_evidence = RepositoryAuditFindings(
        page_integrity=findings["page_integrity"],
        foreign_key_violation_count=findings["foreign_key_violation_count"],
        namespace_violation_count=findings["namespace_violation_count"],
        started_at=findings["started_at"],
        completed_at=findings["completed_at"],
    )
    manifest_sha256 = _audit_manifest_digest(manifest_bytes)
    previous_id = None if predecessor is None else predecessor["certificate_id"]
    previous_manifest = (
        None
        if predecessor is None
        else _manifest_digest(predecessor["manifest_sha256"])
    )
    previous_tail = (
        None
        if predecessor is None
        else _manifest_digest(predecessor["certificate_tail_sha256"])
    )
    evidence = RepositoryAuditCertificateEvidence(
        certificate_id=certificate["certificate_id"],
        purpose=RepositoryAuditPurpose(certificate["purpose"]),
        sequence=certificate["sequence"],
        runtime=runtime_evidence,
        schema=schema_evidence,
        coverage=coverage_evidence,
        findings=findings_evidence,
        receipt_parent=RepositoryAuditGenericReceiptEvidence(
            receipt["transaction_id"], receipt["kind"], receipt["committed_at"]
        ),
        previous_certificate_id=previous_id,
        previous_manifest_sha256=previous_manifest,
        previous_certificate_tail_sha256=previous_tail,
        manifest_bytes=manifest_bytes,
        manifest_sha256=manifest_sha256,
        certificate_tail_sha256=_audit_certificate_tail_digest(
            certificate["sequence"],
            (
                EMPTY_REPOSITORY_AUDIT_CERTIFICATE_TAIL_SHA256
                if previous_tail is None
                else previous_tail
            ),
            manifest_sha256,
        ),
    )
    evidence = _require_repository_audit_certificate_evidence(evidence)
    return _AuditProjection(evidence, _audit_findings_digest(findings_evidence))


def _activation_evidence_from_manifest(
    manifest_bytes: bytes,
) -> BusinessCutoverActivationEvidence:
    root = _decode_exact_manifest(
        manifest_bytes,
        MAX_BUSINESS_CUTOVER_ACTIVATION_MANIFEST_BYTES,
        MAX_BUSINESS_CUTOVER_ACTIVATION_MANIFEST_NODES,
        (
            "schema", "protocol", "status", "manifest_hash_domain",
            "activation", "receipt_parent", "final_admission", "install",
            "audit_certificate", "runtime_transition", "target_schema",
            "actual_schema",
        ),
    )
    activation = _exact_object(
        root["activation"], ("cutover_id", "activated_at")
    )
    receipt = _exact_object(root["receipt_parent"], (
        "ledger", "transaction_id", "kind", "committed_at",
        "specialised_protocol", "specialised_cutover_id",
    ))
    admission = _exact_object(root["final_admission"], (
        "schema", "authority_state", "authority_epoch", "revision",
        "cutover_id", "sha256",
    ))
    install = _exact_object(
        root["install"], ("transaction_id", "receipt_kind", "manifest_sha256")
    )
    audit = _exact_object(root["audit_certificate"], (
        "certificate_id", "sequence", "purpose", "transaction_id",
        "receipt_kind", "manifest_sha256", "certificate_tail_sha256",
        "completed_at",
    ))
    transition = _exact_object(root["runtime_transition"], ("before", "after"))
    target_schema = _exact_object(root["target_schema"], (
        "user_version", "schema_fingerprint", "migration_ledger_count",
        "migration_ledger_sha256",
    ))
    actual_schema = _exact_object(root["actual_schema"], (
        "user_version", "schema_fingerprint", "migration_ledger_count",
        "migration_ledger_sha256",
    ))
    final_admission = BusinessCutoverAdmission(
        authority_state=admission["authority_state"],
        authority_epoch=admission["authority_epoch"],
        revision=admission["revision"],
        cutover_id=admission["cutover_id"],
    )
    evidence = BusinessCutoverActivationEvidence(
        cutover_id=activation["cutover_id"],
        receipt_parent=BusinessCutoverActivationGenericReceiptEvidence(
            receipt["transaction_id"], receipt["kind"], receipt["committed_at"]
        ),
        final_admission=final_admission,
        final_admission_sha256=_manifest_digest(admission["sha256"]),
        before_runtime=_business_runtime_from_manifest(transition["before"]),
        after_runtime=_business_runtime_from_manifest(transition["after"]),
        install_transaction_id=install["transaction_id"],
        install_receipt_kind=install["receipt_kind"],
        install_manifest_sha256=_manifest_digest(install["manifest_sha256"]),
        audit_certificate_id=audit["certificate_id"],
        audit_certificate_sequence=audit["sequence"],
        audit_transaction_id=audit["transaction_id"],
        audit_receipt_kind=audit["receipt_kind"],
        audit_manifest_sha256=_manifest_digest(audit["manifest_sha256"]),
        audit_certificate_tail_sha256=_manifest_digest(
            audit["certificate_tail_sha256"]
        ),
        audit_completed_at=audit["completed_at"],
        expected_migration_ledger_count=target_schema["migration_ledger_count"],
        expected_migration_ledger_sha256=_manifest_digest(
            target_schema["migration_ledger_sha256"]
        ),
        actual_schema=RepositoryAuditSchemaObservation(
            user_version=actual_schema["user_version"],
            schema_fingerprint=actual_schema["schema_fingerprint"],
            migration_ledger_count=actual_schema["migration_ledger_count"],
            migration_ledger_sha256=_manifest_digest(
                actual_schema["migration_ledger_sha256"]
            ),
        ),
        activated_at=activation["activated_at"],
        manifest_bytes=manifest_bytes,
        manifest_sha256=_activation_manifest_digest(manifest_bytes),
    )
    return _require_business_cutover_activation_evidence(evidence)


def _mount_evidence_from_row(row: Any) -> BusinessCutoverMountEvidence:
    """Rebuild the typed pure evidence from exact canonical manifest bytes."""

    try:
        root = decode_payload_bounded(
            row["manifest_bytes"],
            MAX_BUSINESS_CUTOVER_MOUNT_MANIFEST_BYTES,
            MAX_BUSINESS_CUTOVER_MOUNT_MANIFEST_NODES,
        )
        root = _exact_object(root, (
            "schema", "protocol", "status", "manifest_hash_domain",
            "mount", "receipt_parent", "authority", "install",
            "asset_repository_coverage", "activation",
        ))
        mount = _exact_object(root["mount"], ("cutover_id", "mounted_at"))
        receipt = _exact_object(root["receipt_parent"], (
            "ledger", "transaction_id", "kind", "committed_at",
            "specialised_protocol", "specialised_cutover_id",
        ))
        authority = _exact_object(root["authority"], (
            "runtime", "source_admission_sha256", "final_admission_sha256",
        ))
        runtime = _exact_object(authority["runtime"], (
            "authority", "authority_state", "repository_epoch",
            "source_admission_revision", "repository_revision",
            "state_revision", "runtime_generation", "code_digest",
            "runtime_schema_version", "target_schema_version",
            "target_schema_fingerprint",
        ))
        install = _exact_object(root["install"], (
            "transaction_id", "receipt_kind", "manifest_sha256",
            "receipt_parent",
        ))
        install_parent = _exact_object(install["receipt_parent"], (
            "ledger", "transaction_id", "kind", "committed_at",
            "specialised_protocol",
        ))
        coverage = _exact_object(root["asset_repository_coverage"], (
            "transaction_id", "receipt_kind", "manifest_sha256",
            "receipt_parent",
            "coverage_sha256", "member_root_sha256", "batch_count",
            "repository_member_count", "live_project_count",
            "deleted_project_count", "project_count", "reference_count",
            "unique_blob_count", "unique_blob_bytes", "referenced_bytes",
            "observed_occurrence_count", "observed_row_count",
            "observed_manifest_bytes",
        ))
        coverage_parent = _exact_object(coverage["receipt_parent"], (
            "ledger", "transaction_id", "kind", "committed_at",
            "specialised_protocol",
        ))
        if (
            install_parent != {
                "ledger": "transaction_receipts",
                "transaction_id": install["transaction_id"],
                "kind": install["receipt_kind"],
                "committed_at": install_parent["committed_at"],
                "specialised_protocol": BUSINESS_CUTOVER_PROTOCOL,
            }
            or coverage_parent != {
                "ledger": "transaction_receipts",
                "transaction_id": coverage["transaction_id"],
                "kind": coverage["receipt_kind"],
                "committed_at": coverage_parent["committed_at"],
                "specialised_protocol": (
                    BUSINESS_CUTOVER_ASSET_COVERAGE_PROTOCOL
                ),
            }
        ):
            _fail("production_mount_manifest_invalid")
        activation = _exact_object(root["activation"], (
            "transaction_id", "receipt_kind", "manifest_sha256",
            "activated_at", "audit_certificate", "target_migration_ledger",
        ))
        audit = _exact_object(activation["audit_certificate"], (
            "certificate_id", "sequence", "transaction_id", "receipt_kind",
            "manifest_sha256", "certificate_tail_sha256", "completed_at",
        ))
        ledger = _exact_object(activation["target_migration_ledger"], (
            "count", "sha256",
        ))
        evidence = BusinessCutoverMountEvidence(
            cutover_id=mount["cutover_id"],
            receipt_parent=BusinessCutoverMountGenericReceiptEvidence(
                receipt["transaction_id"],
                receipt["kind"],
                receipt["committed_at"],
            ),
            runtime=BusinessCutoverMountRuntimeIdentity(
                authority=runtime["authority"],
                authority_state=runtime["authority_state"],
                repository_epoch=runtime["repository_epoch"],
                source_admission_revision=runtime["source_admission_revision"],
                repository_revision=runtime["repository_revision"],
                state_revision=runtime["state_revision"],
                runtime_generation=runtime["runtime_generation"],
                code_digest=_manifest_digest(runtime["code_digest"]),
                runtime_schema_version=runtime["runtime_schema_version"],
                target_schema_version=runtime["target_schema_version"],
                target_schema_fingerprint=runtime["target_schema_fingerprint"],
            ),
            source_admission_sha256=_manifest_digest(
                authority["source_admission_sha256"]
            ),
            final_admission_sha256=_manifest_digest(
                authority["final_admission_sha256"]
            ),
            install=BusinessCutoverMountInstallBinding(
                install["transaction_id"],
                install["receipt_kind"],
                install_parent["committed_at"],
                _manifest_digest(install["manifest_sha256"]),
            ),
            coverage=BusinessCutoverMountCoverageBinding(
                transaction_id=coverage["transaction_id"],
                receipt_kind=coverage["receipt_kind"],
                committed_at=coverage_parent["committed_at"],
                manifest_sha256=_manifest_digest(coverage["manifest_sha256"]),
                coverage_sha256=_manifest_digest(coverage["coverage_sha256"]),
                member_root_sha256=_manifest_digest(
                    coverage["member_root_sha256"]
                ),
                batch_count=coverage["batch_count"],
                repository_member_count=coverage["repository_member_count"],
                live_project_count=coverage["live_project_count"],
                deleted_project_count=coverage["deleted_project_count"],
                project_count=coverage["project_count"],
                reference_count=coverage["reference_count"],
                unique_blob_count=coverage["unique_blob_count"],
                unique_blob_bytes=coverage["unique_blob_bytes"],
                referenced_bytes=coverage["referenced_bytes"],
                observed_occurrence_count=coverage["observed_occurrence_count"],
                observed_row_count=coverage["observed_row_count"],
                observed_manifest_bytes=coverage["observed_manifest_bytes"],
            ),
            activation=BusinessCutoverMountActivationBinding(
                transaction_id=activation["transaction_id"],
                receipt_kind=activation["receipt_kind"],
                manifest_sha256=_manifest_digest(activation["manifest_sha256"]),
                audit_certificate_id=audit["certificate_id"],
                audit_certificate_sequence=audit["sequence"],
                audit_transaction_id=audit["transaction_id"],
                audit_receipt_kind=audit["receipt_kind"],
                audit_manifest_sha256=_manifest_digest(audit["manifest_sha256"]),
                audit_certificate_tail_sha256=_manifest_digest(
                    audit["certificate_tail_sha256"]
                ),
                expected_migration_ledger_count=ledger["count"],
                expected_migration_ledger_sha256=_manifest_digest(ledger["sha256"]),
                audit_completed_at=audit["completed_at"],
                activated_at=activation["activated_at"],
            ),
            mounted_at=mount["mounted_at"],
            manifest_bytes=row["manifest_bytes"],
            manifest_sha256=row["manifest_sha256"],
        )
        return _require_business_cutover_mount_evidence(evidence)
    except (
        BusinessCodecError,
        BusinessCutoverMountContractError,
        BusinessCutoverMountIntegrityError,
        KeyError,
        SQLiteProductionAuthorityError,
        TypeError,
        ValueError,
    ):
        _fail("production_mount_manifest_invalid")
    raise AssertionError("unreachable")


def _mounted_row(connection: Any) -> Any:
    return connection.execute(
        """
        SELECT m.cutover_id,m.transaction_id,m.receipt_kind,m.committed_at,
               m.install_transaction_id,m.install_receipt_kind,
               m.install_committed_at,m.install_manifest_sha256,
               m.coverage_transaction_id,m.coverage_receipt_kind,
               m.coverage_committed_at,m.coverage_manifest_sha256,
               m.audit_transaction_id,m.audit_manifest_sha256,
               m.activation_transaction_id,m.activation_manifest_sha256,
               m.repository_epoch,m.runtime_generation,m.code_digest,
               m.manifest_bytes,m.manifest_sha256,
               r.kind AS generic_kind,r.committed_at AS generic_committed_at
        FROM business_cutover_mount_receipts AS m
        JOIN business_cutover_head AS h
          ON h.singleton=1 AND h.cutover_id=m.cutover_id
         AND h.state_revision=5 AND h.phase_revision=5 AND h.phase='mounted'
         AND h.authority_state='active'
         AND h.mount_transaction_id=m.transaction_id
         AND h.mount_manifest_sha256=m.manifest_sha256
        JOIN transaction_receipts AS r ON r.transaction_id=m.transaction_id
        """
    ).fetchone()


_CUTOVER_HEAD_ROW_COLUMNS = (
    "singleton",
    "cutover_id",
    "state_revision",
    "phase_revision",
    "phase",
    "authority_state",
    "repository_epoch",
    "runtime_generation",
    "code_digest",
    "install_transaction_id",
    "install_manifest_sha256",
    "coverage_transaction_id",
    "coverage_manifest_sha256",
    "audit_transaction_id",
    "audit_manifest_sha256",
    "activation_transaction_id",
    "activation_manifest_sha256",
    "mount_transaction_id",
    "mount_manifest_sha256",
    "previous_head_sha256",
    "head_sha256",
    "updated_at",
)


def _cutover_head_row_exact(
    connection: Any,
    evidence: BusinessCutoverMountEvidence,
) -> bool:
    """Rebuild phase 1..5 and compare every persisted final-head column."""

    row = connection.execute(
        "SELECT " + ",".join(_CUTOVER_HEAD_ROW_COLUMNS)
        + " FROM business_cutover_head WHERE singleton=1 AND cutover_id=?",
        (evidence.cutover_id,),
    ).fetchone()
    try:
        final = issue_business_cutover_head_chain_from_mount(evidence)[-1]
        values = final.row_values()
        return _row_is(row, tuple(values[name] for name in _CUTOVER_HEAD_ROW_COLUMNS))
    except (
        BusinessCutoverHeadIntegrityError,
        IndexError,
        KeyError,
        TypeError,
        ValueError,
    ):
        return False


def _v13_binding_chain_exact(
    connection: Any,
    evidence: BusinessCutoverMountEvidence,
) -> bool:
    """Require the fresh all-field V13 semantic prefix before authorization."""

    if evidence.runtime.target_schema_version < _SCHEMA_V13_VERSION:
        return True
    try:
        prefix = _read_cutover_phase_binding_prefix(connection)
    except SQLiteCutoverPhaseBindingReaderError:
        return False
    head = prefix.current_head
    return bool(
        prefix.cutover_id == evidence.cutover_id
        and head.phase_revision == 5
        and len(prefix.bindings) == 4
        and head.mount is not None
        and head.mount.transaction_id == evidence.transaction_id
        and hmac.compare_digest(
            head.mount.manifest_sha256,
            evidence.manifest_sha256,
        )
    )


def _linked_mount_rows_exact(
    connection: Any,
    evidence: BusinessCutoverMountEvidence,
) -> bool:
    install = connection.execute(
        "SELECT s.cutover_id,s.transaction_id,s.receipt_kind,s.committed_at,"
        "s.repository_epoch,s.runtime_generation,s.code_digest,"
        "s.member_mapping_sha256,s.manifest_bytes,s.manifest_sha256,"
        "r.transaction_id,r.kind,r.committed_at FROM "
        "business_cutover_install_receipts AS s JOIN transaction_receipts AS r "
        "ON r.transaction_id=s.transaction_id WHERE s.cutover_id=?",
        (evidence.cutover_id,),
    ).fetchone()
    coverage = connection.execute(
        "SELECT s.cutover_id,s.transaction_id,s.receipt_kind,s.committed_at,"
        "s.repository_epoch,s.source_admission_revision,"
        "s.source_admission_sha256,s.batch_count,s.member_root_sha256,"
        "s.repository_member_count,s.live_project_count,s.deleted_project_count,"
        "s.observed_occurrence_count,s.observed_row_count,"
        "s.observed_manifest_bytes,s.project_count,s.reference_count,"
        "s.unique_blob_count,s.unique_blob_bytes,s.referenced_bytes,"
        "s.project_root_sha256,s.blob_root_sha256,s.occurrence_root_sha256,"
        "s.coverage_sha256,s.manifest_bytes,s.manifest_sha256,"
        "r.transaction_id,r.kind,r.committed_at FROM "
        "business_asset_coverage_receipts AS s JOIN transaction_receipts AS r "
        "ON r.transaction_id=s.transaction_id WHERE s.cutover_id=?",
        (evidence.cutover_id,),
    ).fetchone()
    audit = connection.execute(
        "SELECT s.certificate_id,s.cutover_id,s.sequence,s.purpose,"
        "s.transaction_id,s.receipt_kind,s.committed_at,"
        "s.previous_certificate_id,s.previous_manifest_sha256,"
        "s.previous_tail_sha256,s.schema_version,s.migration_ledger_count,"
        "s.migration_ledger_sha256,s.receipt_count,s.receipt_root_sha256,"
        "s.worker_ledger_sequence,s.worker_ledger_tail_sha256,"
        "s.findings_sha256,s.manifest_bytes,s.manifest_sha256,"
        "s.certificate_tail_sha256,r.transaction_id,r.kind,r.committed_at "
        "FROM repository_audit_certificates AS s JOIN transaction_receipts AS r "
        "ON r.transaction_id=s.transaction_id WHERE s.certificate_id=?",
        (evidence.activation.audit_certificate_id,),
    ).fetchone()
    activation = connection.execute(
        "SELECT s.cutover_id,s.transaction_id,s.receipt_kind,s.committed_at,"
        "s.install_transaction_id,s.install_manifest_sha256,"
        "s.audit_transaction_id,s.audit_manifest_sha256,"
        "s.migration_ledger_sha256,s.manifest_bytes,s.manifest_sha256,"
        "r.transaction_id,r.kind,r.committed_at FROM "
        "business_cutover_activation_receipts AS s "
        "JOIN transaction_receipts AS r ON r.transaction_id=s.transaction_id "
        "WHERE s.cutover_id=?",
        (evidence.cutover_id,),
    ).fetchone()
    try:
        if any(row is None for row in (install, coverage, audit, activation)):
            return False
        install_projection = _install_evidence_from_manifest(install[8])
        install_evidence = install_projection.evidence
        coverage_projection = _coverage_evidence_from_manifest(coverage[24])
        coverage_evidence = coverage_projection.evidence
        audit_projection = _audit_evidence_from_manifest(audit[18])
        audit_evidence = audit_projection.evidence
        activation_evidence = _activation_evidence_from_manifest(activation[9])
        stored_previous_tail = (
            EMPTY_REPOSITORY_AUDIT_CERTIFICATE_TAIL_SHA256
            if audit_evidence.previous_certificate_tail_sha256 is None
            else audit_evidence.previous_certificate_tail_sha256
        )
        install_exact = _row_is(install, (
            install_evidence.cutover_id,
            install_evidence.transaction_id,
            install_evidence.receipt_kind,
            evidence.install.committed_at,
            install_evidence.runtime.repository_epoch,
            install_evidence.runtime.runtime_generation,
            install_evidence.runtime.code_digest,
            install_evidence.member_mapping_sha256,
            install_evidence.manifest_bytes,
            install_evidence.manifest_sha256,
            install_evidence.transaction_id,
            install_evidence.receipt_kind,
            evidence.install.committed_at,
        ))
        coverage_exact = _row_is(coverage, (
            coverage_evidence.cutover_id,
            coverage_evidence.transaction_id,
            coverage_evidence.receipt_kind,
            evidence.coverage.committed_at,
            coverage_evidence.repository_epoch,
            coverage_evidence.source_admission_revision,
            coverage_evidence.source_admission_sha256,
            coverage_evidence.batch_count,
            coverage_projection.member_root_sha256,
            coverage_projection.repository_member_count,
            coverage_projection.live_project_count,
            coverage_projection.deleted_project_count,
            coverage_evidence.observed_occurrence_count,
            coverage_evidence.observed_row_count,
            coverage_evidence.observed_manifest_bytes,
            coverage_evidence.project_count,
            coverage_evidence.reference_count,
            coverage_evidence.unique_blob_count,
            coverage_evidence.unique_blob_bytes,
            coverage_evidence.referenced_bytes,
            coverage_evidence.project_root_sha256,
            coverage_evidence.blob_root_sha256,
            coverage_evidence.occurrence_root_sha256,
            coverage_evidence.coverage_sha256,
            coverage_evidence.manifest_bytes,
            coverage_evidence.manifest_sha256,
            coverage_evidence.transaction_id,
            coverage_evidence.receipt_kind,
            evidence.coverage.committed_at,
        ))
        audit_exact = _row_is(audit, (
            audit_evidence.certificate_id,
            audit_evidence.runtime.cutover_id,
            audit_evidence.sequence,
            audit_evidence.purpose.value,
            audit_evidence.transaction_id,
            audit_evidence.receipt_kind,
            audit_evidence.receipt_parent.committed_at,
            audit_evidence.previous_certificate_id,
            audit_evidence.previous_manifest_sha256,
            stored_previous_tail,
            audit_evidence.schema.user_version,
            audit_evidence.schema.migration_ledger_count,
            audit_evidence.schema.migration_ledger_sha256,
            audit_evidence.coverage.receipt_namespace_count,
            audit_evidence.coverage.receipt_namespace_sha256,
            audit_evidence.coverage.worker_ledger_sequence,
            audit_evidence.coverage.worker_ledger_tail_sha256,
            audit_projection.findings_sha256,
            audit_evidence.manifest_bytes,
            audit_evidence.manifest_sha256,
            audit_evidence.certificate_tail_sha256,
            audit_evidence.transaction_id,
            audit_evidence.receipt_kind,
            audit_evidence.receipt_parent.committed_at,
        ))
        activation_exact = _row_is(activation, (
            activation_evidence.cutover_id,
            activation_evidence.transaction_id,
            activation_evidence.receipt_kind,
            activation_evidence.receipt_parent.committed_at,
            activation_evidence.install_transaction_id,
            activation_evidence.install_manifest_sha256,
            activation_evidence.audit_transaction_id,
            activation_evidence.audit_manifest_sha256,
            activation_evidence.expected_migration_ledger_sha256,
            activation_evidence.manifest_bytes,
            activation_evidence.manifest_sha256,
            activation_evidence.transaction_id,
            activation_evidence.receipt_kind,
            activation_evidence.receipt_parent.committed_at,
        ))
        identity_exact = bool(
            install_evidence.cutover_id == evidence.cutover_id
            and install_evidence.transaction_id == evidence.install.transaction_id
            and install_evidence.receipt_kind == evidence.install.receipt_kind
            and install[3] == evidence.install.committed_at
            and hmac.compare_digest(
                install_evidence.manifest_sha256,
                evidence.install.manifest_sha256,
            )
            and install_evidence.runtime.repository_epoch
            == evidence.runtime.repository_epoch
            and install_evidence.runtime.source_admission_revision
            == evidence.runtime.source_admission_revision
            and install_evidence.runtime.runtime_generation
            == evidence.runtime.runtime_generation
            and hmac.compare_digest(
                install_evidence.runtime.code_digest,
                evidence.runtime.code_digest,
            )
            and hmac.compare_digest(
                install_projection.source_admission_sha256,
                evidence.source_admission_sha256,
            )
            and hmac.compare_digest(
                install_projection.final_admission_sha256,
                evidence.final_admission_sha256,
            )
            and install_projection.target_schema_version
            == evidence.runtime.target_schema_version
            and install_projection.target_schema_fingerprint
            == evidence.runtime.target_schema_fingerprint
            and coverage_evidence.cutover_id == evidence.cutover_id
            and coverage_evidence.transaction_id
            == evidence.coverage.transaction_id
            and coverage_evidence.receipt_kind == evidence.coverage.receipt_kind
            and coverage[3] == evidence.coverage.committed_at
            and hmac.compare_digest(
                coverage_evidence.manifest_sha256,
                evidence.coverage.manifest_sha256,
            )
            and hmac.compare_digest(
                coverage_evidence.source_admission_sha256,
                evidence.source_admission_sha256,
            )
            and hmac.compare_digest(
                coverage_projection.pending_admission_sha256,
                install_projection.pending_admission_sha256,
            )
            and coverage_projection.runtime_generation
            == install_evidence.runtime.runtime_generation
            and hmac.compare_digest(
                coverage_projection.code_digest,
                install_evidence.runtime.code_digest,
            )
            and coverage_projection.member_root_sha256
            == evidence.coverage.member_root_sha256
            and coverage_projection.repository_member_count
            == evidence.coverage.repository_member_count
            and coverage_projection.live_project_count
            == evidence.coverage.live_project_count
            and coverage_projection.deleted_project_count
            == evidence.coverage.deleted_project_count
            and coverage_evidence.batch_count == evidence.coverage.batch_count
            and coverage_evidence.project_count == evidence.coverage.project_count
            and coverage_evidence.reference_count
            == evidence.coverage.reference_count
            and coverage_evidence.unique_blob_count
            == evidence.coverage.unique_blob_count
            and coverage_evidence.unique_blob_bytes
            == evidence.coverage.unique_blob_bytes
            and coverage_evidence.referenced_bytes
            == evidence.coverage.referenced_bytes
            and coverage_evidence.observed_occurrence_count
            == evidence.coverage.observed_occurrence_count
            and coverage_evidence.observed_row_count
            == evidence.coverage.observed_row_count
            and coverage_evidence.observed_manifest_bytes
            == evidence.coverage.observed_manifest_bytes
            and hmac.compare_digest(
                coverage_evidence.coverage_sha256,
                evidence.coverage.coverage_sha256,
            )
            and audit_evidence.certificate_id
            == evidence.activation.audit_certificate_id
            and audit_evidence.sequence
            == evidence.activation.audit_certificate_sequence
            and audit_evidence.purpose is RepositoryAuditPurpose.INSTALL
            and audit_evidence.runtime.cutover_id == evidence.cutover_id
            and audit_evidence.runtime.authority
            == install_evidence.runtime.authority
            and audit_evidence.runtime.authority_state.value
            == install_evidence.runtime.authority_state
            and audit_evidence.runtime.repository_epoch
            == install_evidence.runtime.repository_epoch
            and audit_evidence.runtime.source_admission_revision
            == install_evidence.runtime.source_admission_revision
            and audit_evidence.runtime.repository_revision
            == install_evidence.runtime.repository_revision
            and audit_evidence.runtime.state_revision
            == install_evidence.runtime.state_revision
            and audit_evidence.runtime.runtime_generation
            == install_evidence.runtime.runtime_generation
            and hmac.compare_digest(
                audit_evidence.runtime.code_digest,
                install_evidence.runtime.code_digest,
            )
            and audit_evidence.runtime.schema_version
            == install_evidence.runtime.schema_version
            and hmac.compare_digest(
                audit_evidence.coverage.cutover_install_manifest_sha256,
                install_evidence.manifest_sha256,
            )
            and audit_evidence.transaction_id
            == evidence.activation.audit_transaction_id
            and audit_evidence.receipt_kind
            == evidence.activation.audit_receipt_kind
            and audit_evidence.findings.completed_at
            == evidence.activation.audit_completed_at
            and audit_evidence.schema.migration_ledger_count
            == evidence.activation.expected_migration_ledger_count
            and hmac.compare_digest(
                audit_evidence.schema.migration_ledger_sha256,
                evidence.activation.expected_migration_ledger_sha256,
            )
            and hmac.compare_digest(
                audit_evidence.manifest_sha256,
                evidence.activation.audit_manifest_sha256,
            )
            and hmac.compare_digest(
                audit_evidence.certificate_tail_sha256,
                evidence.activation.audit_certificate_tail_sha256,
            )
            and activation_evidence.cutover_id == evidence.cutover_id
            and activation_evidence.transaction_id
            == evidence.activation.transaction_id
            and activation_evidence.receipt_kind
            == evidence.activation.receipt_kind
            and hmac.compare_digest(
                activation_evidence.manifest_sha256,
                evidence.activation.manifest_sha256,
            )
            and activation_evidence.install_transaction_id
            == install_evidence.transaction_id
            and hmac.compare_digest(
                activation_evidence.install_manifest_sha256,
                install_evidence.manifest_sha256,
            )
            and activation_evidence.audit_transaction_id
            == audit_evidence.transaction_id
            and hmac.compare_digest(
                activation_evidence.audit_manifest_sha256,
                audit_evidence.manifest_sha256,
            )
            and hmac.compare_digest(
                activation_evidence.audit_certificate_tail_sha256,
                audit_evidence.certificate_tail_sha256,
            )
            and activation_evidence.audit_completed_at
            == audit_evidence.findings.completed_at
            and activation_evidence.actual_schema == audit_evidence.schema
            and activation_evidence.before_runtime == install_evidence.runtime
            and activation_evidence.final_admission_sha256
            == evidence.final_admission_sha256
            and install[3] <= audit_evidence.findings.started_at
            and coverage[3] <= audit_evidence.findings.started_at
            and audit_evidence.findings.completed_at
            <= activation_evidence.activated_at
            <= evidence.mounted_at
        )
        install_closure_exact = True
        if evidence.runtime.target_schema_version >= _SCHEMA_V12_VERSION:
            install_closure = _require_cutover_install_closure_binding(
                _read_business_cutover_install_closure(
                    connection,
                    cutover_id=evidence.cutover_id,
                    install_transaction_id=install_evidence.transaction_id,
                    install_manifest_sha256=install_evidence.manifest_sha256,
                    install_committed_at=evidence.install.committed_at,
                    target_schema_version=(
                        evidence.runtime.target_schema_version
                    ),
                    target_schema_fingerprint=(
                        evidence.runtime.target_schema_fingerprint
                    ),
                )
            )
            install_closure_exact = bool(
                install_closure.cutover_id == evidence.cutover_id
                and install_closure.install_transaction_id
                == install_evidence.transaction_id
                and hmac.compare_digest(
                    install_closure.install_manifest_sha256,
                    install_evidence.manifest_sha256,
                )
            )
        return bool(
            install_exact
            and coverage_exact
            and audit_exact
            and activation_exact
            and identity_exact
            and install_closure_exact
            and _v13_binding_chain_exact(connection, evidence)
            and _cutover_head_row_exact(connection, evidence)
        )
    except (
        BusinessCodecError,
        BusinessCutoverActivationContractError,
        BusinessCutoverActivationIntegrityError,
        BusinessCutoverAssetContractError,
        BusinessCutoverContractError,
        BusinessCutoverHeadIntegrityError,
        RepositoryAuditContractError,
        RepositoryAuditIntegrityError,
        SQLiteCutoverInstallClosureError,
        SQLiteProductionAuthorityError,
        IndexError,
        KeyError,
        TypeError,
        ValueError,
    ):
        return False


def _binding_from_snapshot(
    connection: Any,
    profile: Any,
    row: Any,
) -> ProductionMountedAuthorityBinding:
    if row is None:
        _fail("production_mount_authorization_invalid")
    evidence = _mount_evidence_from_row(row)
    if (
        profile.authority_state.value != "active"
        or evidence.cutover_id != profile.cutover_id
        or evidence.runtime.authority != profile.authority
        or evidence.runtime.authority_state != "active"
        or evidence.runtime.repository_epoch != profile.repository_epoch
        or evidence.runtime.source_admission_revision
        != profile.source_admission_revision
        or profile.repository_revision < evidence.runtime.repository_revision
        or profile.state_revision < evidence.runtime.state_revision
        or evidence.runtime.runtime_generation != profile.runtime_generation
        or not hmac.compare_digest(evidence.runtime.code_digest, profile.code_digest)
        or not _mount_target_identity_is_compatible(
            evidence.runtime.target_schema_version,
            evidence.runtime.target_schema_fingerprint,
        )
        or not _row_is(row, (
            evidence.cutover_id,
            evidence.transaction_id,
            evidence.receipt_kind,
            evidence.mounted_at,
            evidence.install.transaction_id,
            evidence.install.receipt_kind,
            evidence.install.committed_at,
            evidence.install.manifest_sha256,
            evidence.coverage.transaction_id,
            evidence.coverage.receipt_kind,
            evidence.coverage.committed_at,
            evidence.coverage.manifest_sha256,
            evidence.activation.audit_transaction_id,
            evidence.activation.audit_manifest_sha256,
            evidence.activation.transaction_id,
            evidence.activation.manifest_sha256,
            evidence.runtime.repository_epoch,
            evidence.runtime.runtime_generation,
            evidence.runtime.code_digest,
            evidence.manifest_bytes,
            evidence.manifest_sha256,
            evidence.receipt_kind,
            evidence.mounted_at,
        ))
        or not _linked_mount_rows_exact(connection, evidence)
    ):
        _fail("production_mount_authorization_invalid")
    pure = ProductionMountedAuthorityEvidence(
        authority=profile.authority,
        authority_state=profile.authority_state.value,
        repository_epoch=profile.repository_epoch,
        cutover_id=profile.cutover_id,
        source_admission_revision=profile.source_admission_revision,
        repository_revision=profile.repository_revision,
        control_revision=profile.state_revision,
        runtime_generation=profile.runtime_generation,
        code_digest=profile.code_digest,
        target_schema_version=evidence.runtime.target_schema_version,
        target_schema_fingerprint=evidence.runtime.target_schema_fingerprint,
        mount_transaction_id=evidence.transaction_id,
        mount_manifest_sha256=evidence.manifest_sha256,
        mounted_at=evidence.mounted_at,
    )
    return ProductionMountedAuthorityBinding(pure, _issuer=_FRESH_MOUNT_ISSUER)


def _read_production_mounted_authority(
    database: SqliteDatabase,
) -> ProductionMountedAuthorityBinding:
    """Read runtime, phase-5 head and manifest closure in one fresh snapshot."""

    if type(database) is not SqliteDatabase:
        raise TypeError("database must be an exact SqliteDatabase")
    connection = None
    try:
        connection, _filesystem = database._open_unvalidated()
        connection.execute("BEGIN")
        profile = _validate_business_runtime_connection(
            database,
            connection,
            access=BusinessRuntimeAccess.READER,
        )
        binding = _binding_from_snapshot(
            connection,
            profile,
            _mounted_row(connection),
        )
    except SQLiteProductionAuthorityError:
        raise
    except Exception as exc:
        raise SQLiteProductionAuthorityError(
            "production_mount_authorization_unavailable"
        ) from exc
    finally:
        if connection is not None:
            try:
                if connection.in_transaction:
                    connection.rollback()
            finally:
                connection.close()
    return binding


class ProductionAtomicPlanningSnapshot(NamedTuple):
    """One-snapshot inputs needed before issuing a production atomic plan."""

    mounted_authority: ProductionMountedAuthorityBinding
    outbox_allocation_head: ProductionOutboxAllocationHead
    worker_ledger_head: ProductionWorkerLedgerHeadBinding
    outbox_dispatch_head: ProductionOutboxDispatchHeadBinding
    committed_at: str


def _read_scheduler_head_bindings(
    connection: Any,
) -> Tuple[
    ProductionWorkerLedgerHeadBinding,
    ProductionOutboxDispatchHeadBinding,
]:
    """Read and stamp both scheduler time authorities in one caller snapshot."""

    worker_row = connection.execute(
        "SELECT state_revision,sequence,previous_tail_sha256,tail_sha256,"
        "updated_at FROM worker_ledger_head WHERE singleton=1"
    ).fetchone()
    if worker_row is None or len(worker_row) != 5:
        _fail("production_worker_ledger_head_invalid")
    worker = ProductionWorkerLedgerHeadEvidence(
        state_revision=worker_row[0],
        ledger_head=WorkerLedgerHead(worker_row[1], worker_row[3]),
        previous_tail_sha256=worker_row[2],
        updated_at=worker_row[4],
    )

    dispatch_row = connection.execute(
        "SELECT state_revision,published_sequence,"
        "published_repository_revision,published_ordinal,inflight_sequence,"
        "inflight_repository_revision,inflight_ordinal,previous_head_sha256,"
        "head_sha256,updated_at FROM outbox_dispatch_head WHERE singleton=1"
    ).fetchone()
    if dispatch_row is None or len(dispatch_row) != 10:
        _fail("production_outbox_dispatch_head_invalid")
    if dispatch_row[1] == 0:
        if dispatch_row[2] is not None or dispatch_row[3] is not None:
            _fail("production_outbox_dispatch_head_invalid")
        published = WorkerOutboxDispatchCursor(0, 0, -1)
    else:
        published = WorkerOutboxDispatchCursor(
            dispatch_row[1],
            dispatch_row[2],
            dispatch_row[3],
        )
    inflight_values = dispatch_row[4:7]
    if all(value is None for value in inflight_values):
        inflight = None
    elif any(value is None for value in inflight_values):
        _fail("production_outbox_dispatch_head_invalid")
    else:
        inflight = WorkerOutboxOrder(*inflight_values)
    dispatch = ProductionOutboxDispatchHeadEvidence(
        dispatch_state=WorkerOutboxDispatchState(
            dispatch_row[0],
            published,
            inflight,
        ),
        previous_head_sha256=dispatch_row[7],
        head_sha256=dispatch_row[8],
        updated_at=dispatch_row[9],
    )
    return (
        ProductionWorkerLedgerHeadBinding(
            worker,
            _issuer=_FRESH_WORKER_LEDGER_HEAD_ISSUER,
        ),
        ProductionOutboxDispatchHeadBinding(
            dispatch,
            _issuer=_FRESH_OUTBOX_DISPATCH_HEAD_ISSUER,
        ),
    )


def _read_production_atomic_planning_snapshot(
    database: SqliteDatabase,
) -> ProductionAtomicPlanningSnapshot:
    """Read mount, current allocation CAS and database time atomically."""

    if type(database) is not SqliteDatabase:
        raise TypeError("database must be an exact SqliteDatabase")
    connection = None
    try:
        connection, _filesystem = database._open_unvalidated()
        connection.execute("BEGIN")
        profile = _validate_business_runtime_connection(
            database,
            connection,
            access=BusinessRuntimeAccess.READER,
        )
        binding = _binding_from_snapshot(
            connection,
            profile,
            _mounted_row(connection),
        )
        row = connection.execute(
            "SELECT state_revision,sequence,previous_tail_sha256,"
            "tail_sha256,updated_at FROM outbox_allocation_head "
            "WHERE singleton=1"
        ).fetchone()
        if row is None or len(row) != 5:
            _fail("production_outbox_allocation_head_invalid")
        head = ProductionOutboxAllocationHead(
            state_revision=row[0],
            sequence=row[1],
            previous_tail_sha256=row[2],
            tail_sha256=row[3],
            updated_at=row[4],
        )
        worker_head, dispatch_head = _read_scheduler_head_bindings(connection)
        time_row = connection.execute(
            "SELECT max(strftime('%Y-%m-%dT%H:%M:%fZ','now'),?,?,?,?,?)",
            (
                profile.updated_at,
                head.updated_at,
                binding.evidence.mounted_at,
                worker_head.evidence.updated_at,
                dispatch_head.evidence.updated_at,
            ),
        ).fetchone()
        if (
            time_row is None
            or type(time_row[0]) is not str
            or len(time_row[0]) != 24
        ):
            _fail("production_database_clock_invalid")
        result = ProductionAtomicPlanningSnapshot(
            binding,
            head,
            worker_head,
            dispatch_head,
            time_row[0],
        )
    except SQLiteProductionAuthorityError:
        raise
    except Exception as exc:
        raise SQLiteProductionAuthorityError(
            "production_planning_snapshot_unavailable"
        ) from exc
    finally:
        if connection is not None:
            try:
                if connection.in_transaction:
                    connection.rollback()
            finally:
                connection.close()
    return result


__all__ = ()
