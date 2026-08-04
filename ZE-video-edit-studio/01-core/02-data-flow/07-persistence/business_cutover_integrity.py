"""Pure issuer and complete closure for one private business cutover plan.

The issuer consumes one already exact V5 legacy-repository proof plus typed
live baseline facts.  It reconstructs the V5 proof, derives the only legal
legacy/pending/sqlite admission succession, maps every live or deleted member,
and emits domain-separated install/read-only and join/active manifests.

No storage or runtime authority lives here.  A future coordinator must still
hold the repository barrier, persist the issued facts through fixed SQL, and
freshly classify the durable result before returning.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any, NamedTuple, Optional, Tuple

from asset_reference_projection import (
    ASSET_REFERENCE_PROJECTION_PROTOCOL,
    AssetReferenceProjectionError,
    project_asset_references,
    verify_asset_reference_projection,
)
from business_baseline_contract import (
    BUSINESS_BASELINE_MANIFEST_PROTOCOL,
    BUSINESS_BASELINE_MANIFEST_SCHEMA,
    digest_business_baseline_manifest,
)
from business_codec import BusinessCodecError, encode_payload_bounded
from business_cutover_contract import (
    BUSINESS_CUTOVER_AUTHORITY,
    BUSINESS_CUTOVER_INSTALL_AUTHORITY_STATE,
    BUSINESS_CUTOVER_INSTALL_MANIFEST_SCHEMA,
    BUSINESS_CUTOVER_INSTALL_STATE_REVISION,
    BUSINESS_CUTOVER_INSTALL_STATUS,
    BUSINESS_CUTOVER_INSTALL_TRANSACTION_KIND,
    BUSINESS_CUTOVER_INSTALL_TRANSACTION_PREFIX,
    BUSINESS_CUTOVER_JOIN_AUTHORITY_STATE,
    BUSINESS_CUTOVER_JOIN_MANIFEST_SCHEMA,
    BUSINESS_CUTOVER_JOIN_STATE_REVISION,
    BUSINESS_CUTOVER_JOIN_STATUS,
    BUSINESS_CUTOVER_JOIN_TRANSACTION_KIND,
    BUSINESS_CUTOVER_JOIN_TRANSACTION_PREFIX,
    BUSINESS_CUTOVER_MEMBER_DELETED,
    BUSINESS_CUTOVER_MEMBER_LIVE,
    BUSINESS_CUTOVER_MEMBER_MAPPING_SCHEMA,
    BUSINESS_CUTOVER_PLAN_STATUS,
    BUSINESS_CUTOVER_PROTOCOL,
    BUSINESS_CUTOVER_RUNTIME_CONTROL_SCHEMA_VERSION,
    MAX_BUSINESS_CUTOVER_MANIFEST_BYTES,
    MAX_BUSINESS_CUTOVER_MANIFEST_NODES,
    MAX_BUSINESS_CUTOVER_PROJECTS,
    MAX_SQLITE_INTEGER,
    BusinessCutoverAdmission,
    BusinessCutoverBaselineMember,
    BusinessCutoverInstallEvidence,
    BusinessCutoverInstallReconcileStatus,
    BusinessCutoverInstallResult,
    BusinessCutoverJoinEvidence,
    BusinessCutoverJoinReconcileStatus,
    BusinessCutoverJoinResult,
    BusinessCutoverLegacyProof,
    BusinessCutoverLiveBaseline,
    BusinessCutoverRuntimeBinding,
    digest_business_cutover_admission,
)
from business_snapshot import (
    BusinessSnapshotError,
    BusinessSnapshotSet,
    verify_business_snapshot,
)
from legacy_repository_contract import (
    LEGACY_REPOSITORY_MEMBER_DELETED,
    LEGACY_REPOSITORY_MEMBER_LIVE,
    LEGACY_REPOSITORY_TARGET_SCHEMA_VERSION,
    LEGACY_REPOSITORY_TRANSACTION_PREFIX,
)
from legacy_repository_integrity import (
    LegacyRepositoryIntegrity,
    LegacyRepositoryIntegrityError,
    LegacyRepositoryIntegrityMember,
    build_legacy_repository_integrity,
)


BUSINESS_CUTOVER_MEMBER_MAPPING_HASH_DOMAIN = (
    b"ze.business-cutover.member-mapping-digest.v1"
)
BUSINESS_CUTOVER_INSTALL_MANIFEST_HASH_DOMAIN = (
    b"ze.business-cutover.install-manifest-digest.v1"
)
BUSINESS_CUTOVER_JOIN_MANIFEST_HASH_DOMAIN = (
    b"ze.business-cutover.join-manifest-digest.v1"
)
BUSINESS_CUTOVER_BASELINE_ID_HASH_DOMAIN = (
    b"ze.business-cutover.baseline-commit-id.v1"
)

_PLAN_ISSUER = object()
_LOWER_HEX = frozenset("0123456789abcdef")


class BusinessCutoverIntegrityError(ValueError):
    """Stable payload-free failure at the private issuer boundary."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        if type(code) is not str or not code:
            raise TypeError("code must be a non-empty exact string")
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise BusinessCutoverIntegrityError(code)


def _frame(hasher: Any, value: bytes) -> None:
    hasher.update(len(value).to_bytes(8, "big"))
    hasher.update(value)


def _domain_digest(domain: bytes, payload: bytes) -> bytes:
    hasher = hashlib.sha256()
    _frame(hasher, domain)
    _frame(hasher, payload)
    return hasher.digest()


def _identity_digest(domain: bytes, *values: bytes) -> bytes:
    hasher = hashlib.sha256()
    _frame(hasher, domain)
    for value in values:
        _frame(hasher, value)
    return hasher.digest()


def _exact_digest(value: Any, code: str) -> bytes:
    if type(value) is not bytes or len(value) != 32:
        _fail(code)
    return value


def _exact_lower_hex(value: Any, length: int, code: str) -> str:
    if (
        type(value) is not str
        or len(value) != length
        or any(character not in _LOWER_HEX for character in value)
    ):
        _fail(code)
    return value


def _encode_manifest(value: Any, code: str) -> bytes:
    try:
        return encode_payload_bounded(
            value,
            MAX_BUSINESS_CUTOVER_MANIFEST_BYTES,
            MAX_BUSINESS_CUTOVER_MANIFEST_NODES,
        )
    except BusinessCodecError as exc:
        raise BusinessCutoverIntegrityError(code) from exc


class BusinessCutoverClosure(NamedTuple):
    source_admission_sha256: bytes
    pending_admission_sha256: bytes
    final_admission_sha256: bytes
    member_mapping_bytes: bytes
    member_mapping_sha256: bytes
    install_manifest_bytes: bytes
    install_manifest_sha256: bytes
    join_manifest_bytes: bytes
    join_manifest_sha256: bytes


class _PreparedCutover(NamedTuple):
    source_admission: BusinessCutoverAdmission
    pending_admission: BusinessCutoverAdmission
    final_admission: BusinessCutoverAdmission
    legacy_proof: BusinessCutoverLegacyProof
    members: Tuple[BusinessCutoverBaselineMember, ...]
    install: BusinessCutoverInstallEvidence
    join: BusinessCutoverJoinEvidence
    closure: BusinessCutoverClosure


class BusinessCutoverPlan:
    """Issuer-stamped immutable plan; not itself an authority or receipt."""

    __slots__ = (
        "cutover_id",
        "source_admission",
        "pending_admission",
        "final_admission",
        "legacy_proof",
        "members",
        "install",
        "join",
        "closure",
        "expected_target_schema_version",
        "expected_target_schema_fingerprint",
        "protocol",
        "status",
        "_legacy_integrity",
        "_legacy_members",
        "_catalog_seal_sha256",
        "_legacy_schema_fingerprint",
        "_runtime_generation",
        "_code_digest",
        "_business_snapshots",
        "_issuer",
    )

    def __init__(
        self,
        prepared: _PreparedCutover,
        *,
        expected_target_schema_version: int,
        expected_target_schema_fingerprint: str,
        legacy_integrity: LegacyRepositoryIntegrity,
        legacy_members: Tuple[LegacyRepositoryIntegrityMember, ...],
        catalog_seal_sha256: bytes,
        legacy_schema_fingerprint: str,
        runtime_generation: str,
        code_digest: bytes,
        business_snapshots: Tuple[BusinessSnapshotSet, ...],
        _issuer: Any,
    ) -> None:
        if _issuer is not _PLAN_ISSUER:
            _fail("business_cutover_plan_issuer_invalid")
        object.__setattr__(self, "cutover_id", prepared.legacy_proof.cutover_id)
        object.__setattr__(self, "source_admission", prepared.source_admission)
        object.__setattr__(self, "pending_admission", prepared.pending_admission)
        object.__setattr__(self, "final_admission", prepared.final_admission)
        object.__setattr__(self, "legacy_proof", prepared.legacy_proof)
        object.__setattr__(self, "members", prepared.members)
        object.__setattr__(self, "install", prepared.install)
        object.__setattr__(self, "join", prepared.join)
        object.__setattr__(self, "closure", prepared.closure)
        object.__setattr__(
            self,
            "expected_target_schema_version",
            expected_target_schema_version,
        )
        object.__setattr__(
            self,
            "expected_target_schema_fingerprint",
            expected_target_schema_fingerprint,
        )
        object.__setattr__(self, "protocol", BUSINESS_CUTOVER_PROTOCOL)
        object.__setattr__(self, "status", BUSINESS_CUTOVER_PLAN_STATUS)
        object.__setattr__(self, "_legacy_integrity", legacy_integrity)
        object.__setattr__(self, "_legacy_members", legacy_members)
        object.__setattr__(self, "_catalog_seal_sha256", catalog_seal_sha256)
        object.__setattr__(
            self, "_legacy_schema_fingerprint", legacy_schema_fingerprint
        )
        object.__setattr__(self, "_runtime_generation", runtime_generation)
        object.__setattr__(self, "_code_digest", code_digest)
        object.__setattr__(self, "_business_snapshots", business_snapshots)
        object.__setattr__(self, "_issuer", _issuer)

    def __setattr__(self, name: str, value: Any) -> None:
        del name, value
        raise AttributeError("BusinessCutoverPlan is immutable")


def _admission_manifest(
    admission: BusinessCutoverAdmission,
    digest: bytes,
) -> dict:
    value = admission.canonical_value()
    value["sha256"] = digest.hex()
    return value


def _legacy_proof_manifest(proof: BusinessCutoverLegacyProof) -> dict:
    return {
        "cutover_id": proof.cutover_id,
        "transaction_id": proof.transaction_id,
        "catalog_id": proof.catalog_id,
        "catalog_seal_sha256": proof.catalog_seal_sha256.hex(),
        "repository_epoch": proof.repository_epoch,
        "source_admission_revision": proof.source_admission_revision,
        "manifest_sha256": proof.manifest_sha256.hex(),
        "source_repository_sha256": proof.source_repository_sha256.hex(),
        "catalog_coverage_sha256": proof.catalog_coverage_sha256.hex(),
        "proof_sha256": proof.proof_sha256.hex(),
        "schema_fingerprint": proof.schema_fingerprint,
        "project_count": proof.project_count,
        "live_project_count": proof.live_project_count,
        "deleted_project_count": proof.deleted_project_count,
    }


def _runtime_manifest(runtime: BusinessCutoverRuntimeBinding) -> dict:
    return {
        "authority": runtime.authority,
        "authority_state": runtime.authority_state,
        "repository_epoch": runtime.repository_epoch,
        "cutover_id": runtime.cutover_id,
        "source_admission_revision": runtime.source_admission_revision,
        "repository_revision": runtime.repository_revision,
        "state_revision": runtime.state_revision,
        "runtime_generation": runtime.runtime_generation,
        "code_digest": runtime.code_digest.hex(),
        "schema_version": runtime.schema_version,
    }


def _baseline_commit_id(
    cutover_id: str,
    ordinal: int,
    project_id: str,
    generation: int,
) -> str:
    digest = _identity_digest(
        BUSINESS_CUTOVER_BASELINE_ID_HASH_DOMAIN,
        cutover_id.encode("ascii"),
        ordinal.to_bytes(8, "big"),
        project_id.encode("utf-8"),
        generation.to_bytes(8, "big"),
    )
    # v6 business commit identities are 128-bit lowercase hex strings.
    return digest[:16].hex()


def _baseline_manifest(
    *,
    proof: BusinessCutoverLegacyProof,
    baseline: BusinessCutoverLiveBaseline,
    commit_id: str,
    repository_revision: int,
    runtime_generation: str,
    code_digest: bytes,
) -> bytes:
    return _encode_manifest(
        {
            "schema": BUSINESS_BASELINE_MANIFEST_SCHEMA,
            "protocol": BUSINESS_BASELINE_MANIFEST_PROTOCOL,
            "status": "committed",
            "authority": {
                "authority": BUSINESS_CUTOVER_AUTHORITY,
                "authority_state": BUSINESS_CUTOVER_INSTALL_AUTHORITY_STATE,
                "epoch": proof.repository_epoch,
                "cutover_id": proof.cutover_id,
                "source_admission_revision": proof.source_admission_revision,
                "repository_revision": repository_revision,
                "state_revision": BUSINESS_CUTOVER_INSTALL_STATE_REVISION,
                "runtime_generation": runtime_generation,
                "code_digest": code_digest.hex(),
            },
            "commit": {
                "commit_id": commit_id,
                "commit_kind": "baseline",
                "project_id": baseline.project_id,
                "generation": baseline.generation,
                "document_id": baseline.document_id,
                "document_version": baseline.document_version,
                "save_policy": baseline.save_policy,
            },
            "after": {
                "business_root_sha256": baseline.business_root_sha256.hex(),
                "document_sha256": baseline.document_snapshot_sha256.hex(),
                "entity_count": baseline.entity_count,
                "operation_tail_sha256": baseline.operation_tail_sha256.hex(),
            },
            "asset_references": {
                "protocol": ASSET_REFERENCE_PROJECTION_PROTOCOL,
                "reference_count": baseline.asset_reference_count,
                "unique_reference_count": baseline.unique_asset_reference_count,
                "projection_sha256": baseline.asset_projection_sha256.hex(),
            },
        },
        "business_cutover_baseline_manifest_encoding_failed",
    )


def _member_manifest(member: BusinessCutoverBaselineMember) -> dict:
    value = {
        "ordinal": member.ordinal,
        "project_id": member.project_id,
        "generation": member.generation,
        "status": member.status,
        "operation_id": member.operation_id,
        "source_state_sha256": member.source_state_sha256.hex(),
        "source_state_bytes": member.source_state_bytes,
        "catalog_ordinal": member.catalog_ordinal,
        "import_id": member.import_id,
        "storage_version": member.storage_version,
        "row_snapshot_bytes": member.row_snapshot_bytes,
        "baseline_commit_id": member.baseline_commit_id,
        "baseline_repository_revision": member.baseline_repository_revision,
        "document_id": member.document_id,
        "document_version": member.document_version,
        "business_root_sha256": (
            None
            if member.business_root_sha256 is None
            else member.business_root_sha256.hex()
        ),
        "document_snapshot_sha256": (
            None
            if member.document_snapshot_sha256 is None
            else member.document_snapshot_sha256.hex()
        ),
        "entity_count": member.entity_count,
        "save_policy": member.save_policy,
        "operation_tail_sha256": (
            None
            if member.operation_tail_sha256 is None
            else member.operation_tail_sha256.hex()
        ),
        "asset_projection_sha256": (
            None
            if member.asset_projection_sha256 is None
            else member.asset_projection_sha256.hex()
        ),
        "asset_reference_count": member.asset_reference_count,
        "unique_asset_reference_count": member.unique_asset_reference_count,
        "baseline_manifest_sha256": (
            None
            if member.baseline_manifest_sha256 is None
            else member.baseline_manifest_sha256.hex()
        ),
    }
    return value


def _rebuild_legacy_proof(
    legacy_integrity: Any,
    legacy_members: Any,
    *,
    catalog_seal_sha256: Any,
    legacy_schema_fingerprint: Any,
    runtime_generation: Any,
    code_digest: Any,
) -> Tuple[
    LegacyRepositoryIntegrity,
    Tuple[LegacyRepositoryIntegrityMember, ...],
    BusinessCutoverLegacyProof,
]:
    if type(legacy_integrity) is not LegacyRepositoryIntegrity:
        _fail("business_cutover_legacy_proof_type_invalid")
    if type(legacy_members) is not tuple or any(
        type(member) is not LegacyRepositoryIntegrityMember
        for member in legacy_members
    ):
        _fail("business_cutover_legacy_members_invalid")
    if type(catalog_seal_sha256) is not bytes or len(catalog_seal_sha256) != 32:
        _fail("business_cutover_catalog_seal_invalid")
    _exact_lower_hex(
        legacy_schema_fingerprint,
        64,
        "business_cutover_legacy_schema_fingerprint_invalid",
    )
    if type(runtime_generation) is not str or not runtime_generation:
        _fail("business_cutover_runtime_generation_invalid")
    _exact_digest(code_digest, "business_cutover_code_digest_invalid")
    if (
        legacy_integrity.runtime_generation != runtime_generation
        or not hmac.compare_digest(legacy_integrity.code_digest, code_digest)
    ):
        _fail("business_cutover_runtime_identity_mismatch")
    if (
        type(legacy_integrity.repository_revision) is not int
        or not 1 <= legacy_integrity.repository_revision <= MAX_SQLITE_INTEGER - 2
    ):
        _fail("business_cutover_admission_revision_not_contiguous")
    try:
        rebuilt = build_legacy_repository_integrity(
            catalog_id=legacy_integrity.catalog_id,
            catalog_seal_sha256=catalog_seal_sha256,
            repository_epoch=legacy_integrity.repository_epoch,
            repository_revision=legacy_integrity.repository_revision,
            catalog_revision=legacy_integrity.catalog_revision,
            runtime_generation=runtime_generation,
            code_digest=code_digest,
            members=legacy_members,
            schema_fingerprint=legacy_schema_fingerprint,
        )
    except LegacyRepositoryIntegrityError as exc:
        raise BusinessCutoverIntegrityError(
            "business_cutover_legacy_proof_invalid"
        ) from exc
    if rebuilt != legacy_integrity:
        _fail("business_cutover_legacy_proof_changed")
    if rebuilt.transaction_id != LEGACY_REPOSITORY_TRANSACTION_PREFIX + rebuilt.cutover_id:
        _fail("business_cutover_legacy_proof_identity_invalid")
    binding = BusinessCutoverLegacyProof(
        cutover_id=rebuilt.cutover_id,
        transaction_id=rebuilt.transaction_id,
        catalog_id=rebuilt.catalog_id,
        catalog_seal_sha256=catalog_seal_sha256,
        repository_epoch=rebuilt.repository_epoch,
        source_admission_revision=rebuilt.repository_revision,
        manifest_sha256=rebuilt.manifest_sha256,
        source_repository_sha256=rebuilt.source_repository_sha256,
        catalog_coverage_sha256=rebuilt.catalog_coverage_sha256,
        proof_sha256=rebuilt.proof_sha256,
        schema_fingerprint=legacy_schema_fingerprint,
        project_count=rebuilt.project_count,
        live_project_count=rebuilt.live_project_count,
        deleted_project_count=rebuilt.deleted_project_count,
    )
    return rebuilt, legacy_members, binding


def _prepare_members(
    proof: BusinessCutoverLegacyProof,
    legacy_members: Tuple[LegacyRepositoryIntegrityMember, ...],
    business_snapshots: Any,
    *,
    runtime_generation: str,
    code_digest: bytes,
) -> Tuple[BusinessCutoverBaselineMember, ...]:
    if type(business_snapshots) is not tuple or any(
        type(snapshot) is not BusinessSnapshotSet
        for snapshot in business_snapshots
    ):
        _fail("business_cutover_snapshots_invalid")
    if len(business_snapshots) != proof.live_project_count:
        _fail("business_cutover_snapshot_count_mismatch")

    mapped = []
    live_index = 0
    baseline_revision = 0
    for ordinal, source in enumerate(legacy_members):
        if ordinal >= MAX_BUSINESS_CUTOVER_PROJECTS:
            _fail("business_cutover_project_budget_exceeded")
        if source.status == LEGACY_REPOSITORY_MEMBER_DELETED:
            if source.status != BUSINESS_CUTOVER_MEMBER_DELETED:
                _fail("business_cutover_member_status_mismatch")
            mapped.append(BusinessCutoverBaselineMember(
                ordinal=ordinal,
                project_id=source.project_id,
                generation=source.generation,
                status=source.status,
                operation_id=source.operation_id,
                source_state_sha256=source.source_state_sha256,
                source_state_bytes=source.source_state_bytes,
            ))
            continue
        if source.status != LEGACY_REPOSITORY_MEMBER_LIVE:
            _fail("business_cutover_member_status_mismatch")
        if source.status != BUSINESS_CUTOVER_MEMBER_LIVE:
            _fail("business_cutover_member_status_mismatch")
        candidate_snapshot = business_snapshots[live_index]
        live_index += 1
        try:
            snapshot = verify_business_snapshot(candidate_snapshot)
        except BusinessSnapshotError as exc:
            raise BusinessCutoverIntegrityError(
                "business_cutover_snapshot_invalid"
            ) from exc
        if (
            snapshot.project.project_id != source.project_id
            or snapshot.project.generation != source.generation
            or snapshot.document.project_id != source.project_id
            or snapshot.document.document_id != source.document_id
            or snapshot.document.document_version != source.document_version
            or not hmac.compare_digest(
                snapshot.project.snapshot_sha256,
                source.business_root_sha256,
            )
            or len(snapshot.entities) != source.entity_count
        ):
            _fail("business_cutover_snapshot_source_mismatch")
        try:
            asset_projection = verify_asset_reference_projection(
                project_asset_references(snapshot)
            )
        except AssetReferenceProjectionError as exc:
            raise BusinessCutoverIntegrityError(
                "business_cutover_asset_projection_invalid"
            ) from exc
        baseline = BusinessCutoverLiveBaseline(
            project_id=snapshot.project.project_id,
            generation=snapshot.project.generation,
            document_id=snapshot.document.document_id,
            document_version=snapshot.document.document_version,
            business_root_sha256=snapshot.project.snapshot_sha256,
            document_snapshot_sha256=snapshot.document.snapshot_sha256,
            entity_count=len(snapshot.entities),
            save_policy=snapshot.document.save_policy or "legacy",
            operation_tail_sha256=b"\0" * 32,
            asset_projection_sha256=asset_projection.projection_sha256,
            asset_reference_count=asset_projection.reference_count,
            unique_asset_reference_count=asset_projection.unique_reference_count,
        )
        baseline_revision += 1
        commit_id = _baseline_commit_id(
            proof.cutover_id,
            ordinal,
            source.project_id,
            source.generation,
        )
        manifest_bytes = _baseline_manifest(
            proof=proof,
            baseline=baseline,
            commit_id=commit_id,
            repository_revision=baseline_revision,
            runtime_generation=runtime_generation,
            code_digest=code_digest,
        )
        mapped.append(BusinessCutoverBaselineMember(
            ordinal=ordinal,
            project_id=source.project_id,
            generation=source.generation,
            status=source.status,
            operation_id=source.operation_id,
            source_state_sha256=source.source_state_sha256,
            source_state_bytes=source.source_state_bytes,
            catalog_ordinal=source.catalog_ordinal,
            import_id=source.import_id,
            storage_version=source.storage_version,
            row_snapshot_bytes=source.row_snapshot_bytes,
            baseline_commit_id=commit_id,
            baseline_repository_revision=baseline_revision,
            document_id=baseline.document_id,
            document_version=baseline.document_version,
            business_root_sha256=baseline.business_root_sha256,
            document_snapshot_sha256=baseline.document_snapshot_sha256,
            entity_count=baseline.entity_count,
            save_policy=baseline.save_policy,
            operation_tail_sha256=baseline.operation_tail_sha256,
            asset_projection_sha256=baseline.asset_projection_sha256,
            asset_reference_count=baseline.asset_reference_count,
            unique_asset_reference_count=baseline.unique_asset_reference_count,
            baseline_manifest_bytes=manifest_bytes,
            baseline_manifest_sha256=digest_business_baseline_manifest(manifest_bytes),
        ))
    if live_index != len(business_snapshots):
        _fail("business_cutover_snapshot_count_mismatch")
    members = tuple(mapped)
    if (
        len(members) != proof.project_count
        or sum(member.status == BUSINESS_CUTOVER_MEMBER_LIVE for member in members)
        != proof.live_project_count
        or sum(member.status == BUSINESS_CUTOVER_MEMBER_DELETED for member in members)
        != proof.deleted_project_count
        or tuple(member.ordinal for member in members) != tuple(range(len(members)))
        or tuple(member.project_id for member in members)
        != tuple(sorted(member.project_id for member in members))
    ):
        _fail("business_cutover_member_mapping_not_closed")
    return members


def _prepare(
    *,
    legacy_integrity: Any,
    legacy_members: Any,
    catalog_seal_sha256: Any,
    legacy_schema_fingerprint: Any,
    runtime_generation: Any,
    code_digest: Any,
    expected_target_schema_version: Any,
    expected_target_schema_fingerprint: Any,
    business_snapshots: Any,
) -> _PreparedCutover:
    _rebuilt, source_members, proof = _rebuild_legacy_proof(
        legacy_integrity,
        legacy_members,
        catalog_seal_sha256=catalog_seal_sha256,
        legacy_schema_fingerprint=legacy_schema_fingerprint,
        runtime_generation=runtime_generation,
        code_digest=code_digest,
    )
    if (
        type(expected_target_schema_version) is not int
        or not LEGACY_REPOSITORY_TARGET_SCHEMA_VERSION
        < expected_target_schema_version
        <= MAX_SQLITE_INTEGER
    ):
        _fail("business_cutover_expected_target_schema_version_invalid")
    _exact_lower_hex(
        expected_target_schema_fingerprint,
        64,
        "business_cutover_expected_target_schema_fingerprint_invalid",
    )

    source_admission = BusinessCutoverAdmission(
        "legacy",
        proof.repository_epoch,
        proof.source_admission_revision,
        "",
    )
    pending_admission = BusinessCutoverAdmission(
        "cutover_pending",
        proof.repository_epoch,
        proof.source_admission_revision + 1,
        proof.cutover_id,
    )
    final_admission = BusinessCutoverAdmission(
        "sqlite",
        proof.repository_epoch,
        proof.source_admission_revision + 2,
        proof.cutover_id,
    )
    source_admission_sha256 = digest_business_cutover_admission(source_admission)
    pending_admission_sha256 = digest_business_cutover_admission(pending_admission)
    final_admission_sha256 = digest_business_cutover_admission(final_admission)

    members = _prepare_members(
        proof,
        source_members,
        business_snapshots,
        runtime_generation=runtime_generation,
        code_digest=code_digest,
    )
    member_values = [_member_manifest(member) for member in members]
    member_mapping_bytes = _encode_manifest(
        {
            "schema": BUSINESS_CUTOVER_MEMBER_MAPPING_SCHEMA,
            "protocol": BUSINESS_CUTOVER_PROTOCOL,
            "cutover_id": proof.cutover_id,
            "project_count": proof.project_count,
            "live_project_count": proof.live_project_count,
            "deleted_project_count": proof.deleted_project_count,
            "members": member_values,
        },
        "business_cutover_member_mapping_encoding_failed",
    )
    member_mapping_sha256 = _domain_digest(
        BUSINESS_CUTOVER_MEMBER_MAPPING_HASH_DOMAIN,
        member_mapping_bytes,
    )
    runtime_repository_revision = max(1, proof.live_project_count)
    install_runtime = BusinessCutoverRuntimeBinding(
        authority=BUSINESS_CUTOVER_AUTHORITY,
        authority_state=BUSINESS_CUTOVER_INSTALL_AUTHORITY_STATE,
        repository_epoch=proof.repository_epoch,
        cutover_id=proof.cutover_id,
        source_admission_revision=proof.source_admission_revision,
        repository_revision=runtime_repository_revision,
        state_revision=BUSINESS_CUTOVER_INSTALL_STATE_REVISION,
        runtime_generation=runtime_generation,
        code_digest=code_digest,
        schema_version=BUSINESS_CUTOVER_RUNTIME_CONTROL_SCHEMA_VERSION,
    )
    install_transaction_id = (
        BUSINESS_CUTOVER_INSTALL_TRANSACTION_PREFIX + proof.cutover_id
    )
    install_manifest_bytes = _encode_manifest(
        {
            "schema": BUSINESS_CUTOVER_INSTALL_MANIFEST_SCHEMA,
            "protocol": BUSINESS_CUTOVER_PROTOCOL,
            "status": BUSINESS_CUTOVER_INSTALL_STATUS,
            "transaction": {
                "transaction_id": install_transaction_id,
                "receipt_kind": BUSINESS_CUTOVER_INSTALL_TRANSACTION_KIND,
            },
            "cutover_id": proof.cutover_id,
            "admissions": {
                "source": _admission_manifest(
                    source_admission, source_admission_sha256
                ),
                "pending": _admission_manifest(
                    pending_admission, pending_admission_sha256
                ),
                "final": _admission_manifest(final_admission, final_admission_sha256),
            },
            "legacy_proof": _legacy_proof_manifest(proof),
            "runtime": _runtime_manifest(install_runtime),
            # This is the coordinator's expected target, not proof that the
            # current SQLite registry has that fingerprint.  The fixed-SQL
            # coordinator must establish current-registry equality in the same
            # transaction that persists this manifest.
            "expected_target": {
                "schema_version": expected_target_schema_version,
                "schema_fingerprint": expected_target_schema_fingerprint,
            },
            "member_mapping": {
                "schema": BUSINESS_CUTOVER_MEMBER_MAPPING_SCHEMA,
                "sha256": member_mapping_sha256.hex(),
                "project_count": proof.project_count,
                "live_project_count": proof.live_project_count,
                "deleted_project_count": proof.deleted_project_count,
                "baseline_commit_count": proof.live_project_count,
            },
        },
        "business_cutover_install_manifest_encoding_failed",
    )
    install_manifest_sha256 = _domain_digest(
        BUSINESS_CUTOVER_INSTALL_MANIFEST_HASH_DOMAIN,
        install_manifest_bytes,
    )
    install = BusinessCutoverInstallEvidence(
        cutover_id=proof.cutover_id,
        transaction_id=install_transaction_id,
        receipt_kind=BUSINESS_CUTOVER_INSTALL_TRANSACTION_KIND,
        authority_state=BUSINESS_CUTOVER_INSTALL_AUTHORITY_STATE,
        runtime=install_runtime,
        member_mapping_sha256=member_mapping_sha256,
        manifest_bytes=install_manifest_bytes,
        manifest_sha256=install_manifest_sha256,
    )

    join_runtime = BusinessCutoverRuntimeBinding(
        authority=BUSINESS_CUTOVER_AUTHORITY,
        authority_state=BUSINESS_CUTOVER_JOIN_AUTHORITY_STATE,
        repository_epoch=proof.repository_epoch,
        cutover_id=proof.cutover_id,
        source_admission_revision=proof.source_admission_revision,
        repository_revision=runtime_repository_revision,
        state_revision=BUSINESS_CUTOVER_JOIN_STATE_REVISION,
        runtime_generation=runtime_generation,
        code_digest=code_digest,
        schema_version=BUSINESS_CUTOVER_RUNTIME_CONTROL_SCHEMA_VERSION,
    )
    join_transaction_id = BUSINESS_CUTOVER_JOIN_TRANSACTION_PREFIX + proof.cutover_id
    join_manifest_bytes = _encode_manifest(
        {
            "schema": BUSINESS_CUTOVER_JOIN_MANIFEST_SCHEMA,
            "protocol": BUSINESS_CUTOVER_PROTOCOL,
            "status": BUSINESS_CUTOVER_JOIN_STATUS,
            "transaction": {
                "transaction_id": join_transaction_id,
                "receipt_kind": BUSINESS_CUTOVER_JOIN_TRANSACTION_KIND,
            },
            "cutover_id": proof.cutover_id,
            "install": {
                "transaction_id": install_transaction_id,
                "manifest_sha256": install_manifest_sha256.hex(),
                "member_mapping_sha256": member_mapping_sha256.hex(),
            },
            "final_admission": _admission_manifest(
                final_admission,
                final_admission_sha256,
            ),
            "runtime": _runtime_manifest(join_runtime),
            "expected_target": {
                "schema_version": expected_target_schema_version,
                "schema_fingerprint": expected_target_schema_fingerprint,
            },
        },
        "business_cutover_join_manifest_encoding_failed",
    )
    join_manifest_sha256 = _domain_digest(
        BUSINESS_CUTOVER_JOIN_MANIFEST_HASH_DOMAIN,
        join_manifest_bytes,
    )
    join = BusinessCutoverJoinEvidence(
        cutover_id=proof.cutover_id,
        transaction_id=join_transaction_id,
        receipt_kind=BUSINESS_CUTOVER_JOIN_TRANSACTION_KIND,
        authority_state=BUSINESS_CUTOVER_JOIN_AUTHORITY_STATE,
        runtime=join_runtime,
        install_manifest_sha256=install_manifest_sha256,
        final_admission_sha256=final_admission_sha256,
        manifest_bytes=join_manifest_bytes,
        manifest_sha256=join_manifest_sha256,
    )
    closure = BusinessCutoverClosure(
        source_admission_sha256=source_admission_sha256,
        pending_admission_sha256=pending_admission_sha256,
        final_admission_sha256=final_admission_sha256,
        member_mapping_bytes=member_mapping_bytes,
        member_mapping_sha256=member_mapping_sha256,
        install_manifest_bytes=install_manifest_bytes,
        install_manifest_sha256=install_manifest_sha256,
        join_manifest_bytes=join_manifest_bytes,
        join_manifest_sha256=join_manifest_sha256,
    )
    return _PreparedCutover(
        source_admission,
        pending_admission,
        final_admission,
        proof,
        members,
        install,
        join,
        closure,
    )


def _issue_business_cutover_plan(
    *,
    legacy_integrity: LegacyRepositoryIntegrity,
    legacy_members: Tuple[LegacyRepositoryIntegrityMember, ...],
    catalog_seal_sha256: bytes,
    legacy_schema_fingerprint: str,
    runtime_generation: str,
    code_digest: bytes,
    expected_target_schema_version: int,
    expected_target_schema_fingerprint: str,
    business_snapshots: Tuple[BusinessSnapshotSet, ...],
) -> BusinessCutoverPlan:
    """Issue from verified snapshots and a caller-declared expected target.

    The target version/fingerprint is intentionally named ``expected``: this
    pure leaf has no schema-registry capability.  A fixed-SQL coordinator must
    prove it equals the current registry before persisting the install.
    """

    prepared = _prepare(
        legacy_integrity=legacy_integrity,
        legacy_members=legacy_members,
        catalog_seal_sha256=catalog_seal_sha256,
        legacy_schema_fingerprint=legacy_schema_fingerprint,
        runtime_generation=runtime_generation,
        code_digest=code_digest,
        expected_target_schema_version=expected_target_schema_version,
        expected_target_schema_fingerprint=expected_target_schema_fingerprint,
        business_snapshots=business_snapshots,
    )
    return BusinessCutoverPlan(
        prepared,
        expected_target_schema_version=expected_target_schema_version,
        expected_target_schema_fingerprint=expected_target_schema_fingerprint,
        legacy_integrity=legacy_integrity,
        legacy_members=legacy_members,
        catalog_seal_sha256=catalog_seal_sha256,
        legacy_schema_fingerprint=legacy_schema_fingerprint,
        runtime_generation=runtime_generation,
        code_digest=code_digest,
        business_snapshots=business_snapshots,
        _issuer=_PLAN_ISSUER,
    )


def _require_business_cutover_plan(value: Any) -> BusinessCutoverPlan:
    """Rebuild every digest, mapping and manifest before an adapter may trust it."""

    if (
        type(value) is not BusinessCutoverPlan
        or getattr(value, "_issuer", None) is not _PLAN_ISSUER
        or getattr(value, "protocol", None) != BUSINESS_CUTOVER_PROTOCOL
        or getattr(value, "status", None) != BUSINESS_CUTOVER_PLAN_STATUS
    ):
        _fail("business_cutover_plan_invalid")
    prepared = _prepare(
        legacy_integrity=value._legacy_integrity,
        legacy_members=value._legacy_members,
        catalog_seal_sha256=value._catalog_seal_sha256,
        legacy_schema_fingerprint=value._legacy_schema_fingerprint,
        runtime_generation=value._runtime_generation,
        code_digest=value._code_digest,
        expected_target_schema_version=value.expected_target_schema_version,
        expected_target_schema_fingerprint=value.expected_target_schema_fingerprint,
        business_snapshots=value._business_snapshots,
    )
    if (
        value.cutover_id != prepared.legacy_proof.cutover_id
        or value.source_admission != prepared.source_admission
        or value.pending_admission != prepared.pending_admission
        or value.final_admission != prepared.final_admission
        or value.legacy_proof != prepared.legacy_proof
        or value.members != prepared.members
        or value.install != prepared.install
        or value.join != prepared.join
        or value.closure != prepared.closure
        or not hmac.compare_digest(
            value.closure.install_manifest_sha256,
            prepared.closure.install_manifest_sha256,
        )
        or not hmac.compare_digest(
            value.closure.join_manifest_sha256,
            prepared.closure.join_manifest_sha256,
        )
    ):
        _fail("business_cutover_plan_changed")
    return value


def _classify_business_cutover_install(
    plan: BusinessCutoverPlan,
    current_admission: BusinessCutoverAdmission,
    observed_install: Optional[BusinessCutoverInstallEvidence],
    *,
    indeterminate: bool = False,
) -> BusinessCutoverInstallResult:
    """Classify install evidence against the exact current file admission."""

    plan = _require_business_cutover_plan(plan)
    if type(current_admission) is not BusinessCutoverAdmission:
        _fail("business_cutover_current_admission_type_invalid")
    if type(indeterminate) is not bool:
        _fail("business_cutover_classification_input_invalid")
    if (
        observed_install is not None
        and type(observed_install) is not BusinessCutoverInstallEvidence
    ):
        _fail("business_cutover_install_evidence_type_invalid")
    if current_admission == plan.source_admission:
        admission_position = "source"
    elif current_admission == plan.pending_admission:
        admission_position = "pending"
    elif current_admission == plan.final_admission:
        admission_position = "final"
    else:
        admission_position = "other"
    if admission_position == "other":
        return BusinessCutoverInstallResult(
            BusinessCutoverInstallReconcileStatus.CONFLICT,
            plan.cutover_id,
            error_code="business_cutover_file_admission_conflict",
        )
    if (
        observed_install is not None
        and observed_install.cutover_id != plan.cutover_id
    ):
        return BusinessCutoverInstallResult(
            BusinessCutoverInstallReconcileStatus.CONFLICT,
            plan.cutover_id,
            error_code="business_cutover_install_identity_conflict",
        )
    if indeterminate:
        if observed_install is not None:
            _fail("business_cutover_classification_input_invalid")
        if admission_position == "source":
            return BusinessCutoverInstallResult(
                BusinessCutoverInstallReconcileStatus.CONFLICT,
                plan.cutover_id,
                error_code="business_cutover_source_not_fenced",
            )
        return BusinessCutoverInstallResult(
            BusinessCutoverInstallReconcileStatus.UNKNOWN,
            plan.cutover_id,
            error_code="business_cutover_install_observation_indeterminate",
        )
    if observed_install is None:
        if admission_position == "source":
            return BusinessCutoverInstallResult(
                BusinessCutoverInstallReconcileStatus.CONFLICT,
                plan.cutover_id,
                error_code="business_cutover_source_not_fenced",
            )
        if admission_position == "final":
            return BusinessCutoverInstallResult(
                BusinessCutoverInstallReconcileStatus.CORRUPT,
                plan.cutover_id,
                error_code="business_cutover_final_without_install",
            )
        return BusinessCutoverInstallResult(
            BusinessCutoverInstallReconcileStatus.NOT_INSTALLED,
            plan.cutover_id,
        )
    if admission_position == "source":
        return BusinessCutoverInstallResult(
            BusinessCutoverInstallReconcileStatus.CORRUPT,
            plan.cutover_id,
            error_code="business_cutover_install_before_source_fence",
        )
    if observed_install != plan.install:
        return BusinessCutoverInstallResult(
            BusinessCutoverInstallReconcileStatus.CORRUPT,
            plan.cutover_id,
            error_code="business_cutover_install_evidence_mismatch",
        )
    return BusinessCutoverInstallResult(
        BusinessCutoverInstallReconcileStatus.INSTALLED_READ_ONLY,
        plan.cutover_id,
        evidence=observed_install,
    )


def _classify_business_cutover_join(
    plan: BusinessCutoverPlan,
    current_admission: BusinessCutoverAdmission,
    observed_install: Optional[BusinessCutoverInstallEvidence],
    observed_join: Optional[BusinessCutoverJoinEvidence],
    *,
    indeterminate: bool = False,
) -> BusinessCutoverJoinResult:
    """Classify join only after final admission and exact install evidence."""

    plan = _require_business_cutover_plan(plan)
    if type(current_admission) is not BusinessCutoverAdmission:
        _fail("business_cutover_current_admission_type_invalid")
    if type(indeterminate) is not bool:
        _fail("business_cutover_classification_input_invalid")
    if (
        observed_install is not None
        and type(observed_install) is not BusinessCutoverInstallEvidence
    ):
        _fail("business_cutover_install_evidence_type_invalid")
    if observed_join is not None and type(observed_join) is not BusinessCutoverJoinEvidence:
        _fail("business_cutover_join_evidence_type_invalid")
    if current_admission in (plan.source_admission, plan.pending_admission):
        return BusinessCutoverJoinResult(
            BusinessCutoverJoinReconcileStatus.CONFLICT,
            plan.cutover_id,
            error_code="business_cutover_file_authority_not_sqlite",
        )
    if current_admission != plan.final_admission:
        return BusinessCutoverJoinResult(
            BusinessCutoverJoinReconcileStatus.CONFLICT,
            plan.cutover_id,
            error_code="business_cutover_file_admission_conflict",
        )
    if observed_install is None:
        return BusinessCutoverJoinResult(
            BusinessCutoverJoinReconcileStatus.CORRUPT,
            plan.cutover_id,
            error_code="business_cutover_final_without_install",
        )
    if observed_install.cutover_id != plan.cutover_id:
        return BusinessCutoverJoinResult(
            BusinessCutoverJoinReconcileStatus.CONFLICT,
            plan.cutover_id,
            error_code="business_cutover_install_identity_conflict",
        )
    if observed_install != plan.install:
        return BusinessCutoverJoinResult(
            BusinessCutoverJoinReconcileStatus.CORRUPT,
            plan.cutover_id,
            error_code="business_cutover_install_evidence_mismatch",
        )
    if observed_join is not None and observed_join.cutover_id != plan.cutover_id:
        return BusinessCutoverJoinResult(
            BusinessCutoverJoinReconcileStatus.CONFLICT,
            plan.cutover_id,
            error_code="business_cutover_join_identity_conflict",
        )
    if indeterminate:
        if observed_join is not None:
            _fail("business_cutover_classification_input_invalid")
        return BusinessCutoverJoinResult(
            BusinessCutoverJoinReconcileStatus.UNKNOWN,
            plan.cutover_id,
            error_code="business_cutover_join_observation_indeterminate",
        )
    if observed_join is None:
        return BusinessCutoverJoinResult(
            BusinessCutoverJoinReconcileStatus.NOT_JOINED,
            plan.cutover_id,
        )
    if observed_join != plan.join:
        return BusinessCutoverJoinResult(
            BusinessCutoverJoinReconcileStatus.CORRUPT,
            plan.cutover_id,
            error_code="business_cutover_join_evidence_mismatch",
        )
    return BusinessCutoverJoinResult(
        BusinessCutoverJoinReconcileStatus.JOINED_ACTIVE,
        plan.cutover_id,
        evidence=observed_join,
    )


# Private cutover leaf.  It is intentionally absent from the stable core/SDK
# surface; future consumers must import exact underscored issuer names.
__all__ = ()
