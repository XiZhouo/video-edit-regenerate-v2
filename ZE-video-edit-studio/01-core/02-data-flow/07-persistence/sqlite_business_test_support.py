"""Test-only construction of a closed current mount plus one runtime baseline.

This module exists so writer/reader tests do not import helpers from another
test module or duplicate a subtly different authority transition.  It is not a
cutover implementation: the only entry point is explicitly test support, no
SDK module imports it, and it creates a fresh database before installing one
small canonical post-mount baseline commit.  The fixture persists an exact
empty V4 catalog, V5 source proof, fresh asset coverage, V12 install closure,
canonical V13 phase bindings when available, cutover receipts/head and genesis
worker/outbox heads.  It never disables a guard or mutates the schema.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from asset_reference_projection import (
    AssetReferenceProjection,
    project_asset_references,
)
from business_codec import encode_payload_bounded
from business_atomic_write_contract import (
    ATOMIC_BUSINESS_WRITE_MANIFEST_SCHEMA,
    ATOMIC_BUSINESS_WRITE_PLAN_STATUS,
    ATOMIC_BUSINESS_WRITE_PROTOCOL,
    MAX_ATOMIC_BUSINESS_WRITE_MANIFEST_BYTES,
    MAX_ATOMIC_BUSINESS_WRITE_MANIFEST_NODES,
    AtomicBusinessWriteGenericReceiptEvidence,
)
from business_atomic_write_integrity import (
    ATOMIC_BUSINESS_WRITE_CONTROLS_HASH_DOMAIN,
    _atomic_receipt_digest,
    _digest as _atomic_digest,
    _manifest_digest as _atomic_manifest_digest,
)
from business_baseline_contract import (
    BUSINESS_BASELINE_MANIFEST_PROTOCOL,
    BUSINESS_BASELINE_MANIFEST_SCHEMA,
    digest_business_baseline_manifest,
)
from business_cutover_head_integrity import (
    BusinessCutoverHeadInput,
    BusinessCutoverHeadPointer,
    BusinessCutoverHeadPointerRole,
    _require_business_cutover_head_chain,
    issue_business_cutover_head_chain,
    issue_business_cutover_head_chain_from_mount,
)
from business_cutover_contract import (
    BUSINESS_CUTOVER_MEMBER_MAPPING_SCHEMA,
    BUSINESS_CUTOVER_RUNTIME_CONTROL_SCHEMA_VERSION,
    BusinessCutoverAdmission,
    BusinessCutoverRuntimeBinding,
    digest_business_cutover_admission,
)
from business_cutover_activation_contract import (
    BusinessCutoverActivationEvidence as CanonicalActivationEvidence,
    BusinessCutoverActivationGenericReceiptEvidence,
)
from business_cutover_activation_integrity import (
    _build_manifest as _build_activation_manifest,
    _manifest_digest as _activation_manifest_digest,
    _require_business_cutover_activation_evidence,
)
from business_cutover_mount_contract import (
    BUSINESS_CUTOVER_MOUNT_TRANSACTION_KIND,
    BusinessCutoverMountActivationBinding,
    BusinessCutoverMountCoverageBinding,
    BusinessCutoverMountEvidence as CanonicalMountEvidence,
    BusinessCutoverMountGenericReceiptEvidence,
    BusinessCutoverMountInstallBinding,
    BusinessCutoverMountRuntimeIdentity,
)
from business_cutover_mount_integrity import (
    _build_manifest as _build_mount_manifest,
    _manifest_digest as _mount_manifest_digest,
    _require_business_cutover_mount_evidence,
)
from business_cutover_baseline_projection import (
    BUSINESS_CUTOVER_BASELINE_OUTER_ATOMIC_STATUS as BUSINESS_CUTOVER_BASELINE_V1_OUTER_ATOMIC_STATUS,
    BUSINESS_CUTOVER_BASELINE_PROJECTION_PROTOCOL,
    BUSINESS_CUTOVER_BASELINE_PROJECTION_SCHEMA,
    BUSINESS_CUTOVER_BASELINE_PROJECTION_STATUS,
    _projection_digest,
)
from business_cutover_phase_binding_contract import (
    BusinessCutoverActivationEvidence,
    BusinessCutoverAdmissionCheckpoint,
    BusinessCutoverAllocationHighWater,
    BusinessCutoverAssetCoverageCheckpoint,
    BusinessCutoverAuditEvidence,
    BusinessCutoverDispatchHighWater,
    BusinessCutoverDurableCheckpoint,
    BusinessCutoverMaintenanceCheckpoint,
    BusinessCutoverMountEvidence,
    BusinessCutoverPhaseBinding,
    BusinessCutoverPhaseBindingHead,
    BusinessCutoverPhaseBindingIdentity,
    BusinessCutoverPhaseBindingProjection,
    BusinessCutoverPhaseBindingStage,
    BusinessCutoverReceiptPointer,
    BusinessCutoverRuntimeImage,
    BusinessCutoverWorkerHighWater,
)
from business_cutover_phase_binding_integrity import (
    issue_business_cutover_asset_provider_evidence,
    issue_business_cutover_phase_binding,
    verify_business_cutover_phase_binding,
)
from business_cutover_phase_binding_persistability import (
    _BUSINESS_CUTOVER_PHASE_BINDING_LAYOUTS,
    _project_business_cutover_phase_binding_row,
)
from business_cutover_integrity import (
    BUSINESS_CUTOVER_MEMBER_MAPPING_HASH_DOMAIN,
)
from business_cutover_install_closure import (
    BUSINESS_CUTOVER_ARTIFACT_CHUNK_HASH_DOMAIN,
    BUSINESS_CUTOVER_BASELINE_OUTER_ATOMIC_STATUS,
    BUSINESS_CUTOVER_BASELINE_PROJECTION_V12_HASH_DOMAIN,
    BUSINESS_CUTOVER_BASELINE_PROJECTION_V12_PROTOCOL,
    BUSINESS_CUTOVER_BASELINE_PROJECTION_V12_SCHEMA,
    BUSINESS_CUTOVER_INSTALL_CLOSURE_PROTOCOL,
    BUSINESS_CUTOVER_INSTALL_CLOSURE_SCHEMA,
    BUSINESS_CUTOVER_INSTALL_CLOSURE_STATUS,
    _baseline_receipt_root as _install_baseline_receipt_root,
    _closure_manifest_digest as _install_closure_manifest_digest,
    _digest as _install_digest,
    _integer as _install_integer,
    _text as _install_text,
    _asset_coverage_manifest as _install_asset_coverage_manifest,
    _source_proof_manifest as _install_source_proof_manifest,
)
from business_cutover_asset_integrity import (
    _issue_business_cutover_asset_coverage_plan,
    _repository_member_root,
)
from business_cutover_integrity import _issue_business_cutover_plan
from business_snapshot import (
    BusinessSnapshotSet,
    capture_business_snapshot,
    verify_business_snapshot,
)
from business_transaction_contract import (
    BeforeBusinessHead,
    MAX_ATOMIC_MANIFEST_BYTES,
    MAX_ATOMIC_MANIFEST_NODES,
)
from business_transaction_integrity import _component_digest
from document import Document
from legacy_catalog_contract import (
    LEGACY_CATALOG_TRANSACTION_KIND,
    SQLITE_CATALOG_STATE_HISTORICAL_FENCED,
    SQLITE_CATALOG_STATE_KEY,
)
from legacy_repository_contract import LEGACY_REPOSITORY_TRANSACTION_KIND
from production_atomic_write_contract import (
    MAX_PRODUCTION_ATOMIC_MANIFEST_BYTES,
    MAX_PRODUCTION_ATOMIC_MANIFEST_NODES,
    PRODUCTION_ATOMIC_TARGET_GROUP_ORDER,
    PRODUCTION_ATOMIC_WRITE_MANIFEST_SCHEMA,
    PRODUCTION_ATOMIC_WRITE_PROTOCOL,
    PRODUCTION_ATOMIC_WRITE_STATUS,
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
    _domain_digest as _production_domain_digest,
    _groups as _production_groups,
    _production_targets_sha256,
)
from repository_audit_contract import (
    EMPTY_REPOSITORY_AUDIT_CERTIFICATE_TAIL_SHA256,
    EMPTY_WORKER_LEDGER_TAIL_SHA256,
    RepositoryAuditAuthorityState,
    RepositoryAuditCertificateEvidence,
    RepositoryAuditCoverage,
    RepositoryAuditFindings,
    RepositoryAuditGenericReceiptEvidence,
    RepositoryAuditObservedInput,
    RepositoryAuditPurpose,
    RepositoryAuditRuntimeBinding,
    RepositoryAuditSchemaObservation,
)
from repository_audit_integrity import (
    _build_manifest as _build_audit_manifest,
    _certificate_tail_digest,
    _findings_digest,
    _manifest_digest as _audit_manifest_digest,
    _require_repository_audit_certificate_evidence,
)
from sqlite_business_runtime import (
    BusinessAuthorityState,
    BusinessRuntimeAccess,
    BusinessRuntimeProfile,
    _validate_business_runtime_connection,
    inspect_business_runtime,
)
from sqlite_business_audit import _read_receipt_namespace_identity
from sqlite_kernel import SqliteDatabase, _expected_schema_fingerprint
import sqlite_legacy_catalog_exact as _legacy_catalog_exact
import sqlite_legacy_repository_exact as _legacy_repository_exact
from sqlite_schema import (
    BUSINESS_TRANSACTION_KIND,
    BUSINESS_TRANSACTION_PREFIX,
    INSTALL_CLOSURE_MIGRATION,
    MIGRATIONS,
    _expected_migration_ledger_sha256,
)
from worker_execution_contract import WorkerLedgerHead


TEST_BASELINE_MANIFEST_SCHEMA = BUSINESS_BASELINE_MANIFEST_SCHEMA
TEST_BASELINE_MANIFEST_PROTOCOL = BUSINESS_BASELINE_MANIFEST_PROTOCOL
TEST_BASELINE_COMMITTED_AT = "2026-07-12T00:00:00.000Z"
TEST_SECOND_BASELINE_COMMITTED_AT = "2026-07-12T00:00:00.001Z"
TEST_BASELINE_OPERATION_TAIL = hashlib.sha256(
    b"ze.business-operation-chain.empty.test.v1"
).digest()
TEST_BASELINE_BEFORE_ROOT = b"\x00" * 32
TEST_CUTOVER_COMMITTED_AT = "2026-07-11T23:59:59.999Z"
HISTORICAL_V7_SCHEMA_VERSION = 7


def _test_digest(domain: str, *values: str) -> bytes:
    hasher = hashlib.sha256()
    hasher.update(("ze.sqlite-business-test." + domain + ".v1").encode("ascii"))
    for value in values:
        encoded = value.encode("utf-8", "strict")
        hasher.update(len(encoded).to_bytes(8, "big"))
        hasher.update(encoded)
    return hasher.digest()


def _test_manifest(domain: str, *values: str) -> tuple[bytes, bytes]:
    manifest = encode_payload_bounded(
        {
            "schema": "ze.sqlite-business-test-authority.v1",
            "kind": domain,
            "values": list(values),
        },
        MAX_ATOMIC_MANIFEST_BYTES,
        MAX_ATOMIC_MANIFEST_NODES,
    )
    return manifest, hashlib.sha256(manifest).digest()


def _insert_generic_receipt(
    connection: Any,
    transaction_id: str,
    kind: str,
) -> None:
    connection.execute(
        "INSERT INTO transaction_receipts(transaction_id,kind,committed_at) "
        "VALUES (?,?,?)",
        (transaction_id, kind, TEST_CUTOVER_COMMITTED_AT),
    )


def _prepare_empty_cutover_authority(
    *,
    repository_epoch: str,
    source_admission_revision: int,
    runtime_generation: str,
    code_digest: bytes,
) -> tuple[Any, Any, Any, Any]:
    """Build the exact durable source/install/coverage graph for an empty mount.

    The first application project in this shared fixture is deliberately a
    *post-mount* baseline.  The cutover source is therefore the canonical
    empty V4 catalog and empty V5 repository proof, not a convenient test
    digest.  Its content-addressed ``cutover_id`` is the only identity used by
    the mounted fixture.
    """

    catalog = _legacy_catalog_exact.project_empty_catalog()
    repository = _legacy_repository_exact._issue_repository_projection(
        catalog,
        (),
        repository_epoch=repository_epoch,
        repository_revision=source_admission_revision,
        catalog_revision=source_admission_revision,
        runtime_generation=runtime_generation,
        code_digest=code_digest,
    )
    plan = _issue_business_cutover_plan(
        legacy_integrity=repository.integrity,
        legacy_members=(),
        catalog_seal_sha256=catalog.integrity.seal_sha256,
        legacy_schema_fingerprint=repository.schema_fingerprint,
        runtime_generation=runtime_generation,
        code_digest=code_digest,
        expected_target_schema_version=MIGRATIONS[-1].version,
        expected_target_schema_fingerprint=_expected_schema_fingerprint(),
        business_snapshots=(),
    )
    coverage = _issue_business_cutover_asset_coverage_plan(
        cutover_plan=plan,
        business_snapshots=(),
        stage_observations=(),
    )
    return catalog, repository, plan, coverage


def _insert_empty_legacy_source_proof(
    connection: Any,
    *,
    catalog: Any,
    repository: Any,
) -> None:
    """Persist exact V4/V5 evidence with the fixture's frozen chronology."""

    connection.execute(
        "INSERT INTO kernel_metadata(key,value) VALUES (?,?)",
        (SQLITE_CATALOG_STATE_KEY, SQLITE_CATALOG_STATE_HISTORICAL_FENCED),
    )
    connection.execute(
        "INSERT INTO transaction_receipts(transaction_id,kind,committed_at) "
        "VALUES (?,?,?)",
        (
            catalog.integrity.transaction_id,
            LEGACY_CATALOG_TRANSACTION_KIND,
            TEST_CUTOVER_COMMITTED_AT,
        ),
    )
    connection.execute(
        """
        INSERT INTO legacy_catalog_seals(
            repository_singleton,catalog_id,transaction_id,receipt_kind,
            receipt_protocol,import_id,seal_protocol,manifest_schema,status,
            selection,project_count,entity_count,source_head_bytes,
            row_snapshot_bytes,manifest_bytes,manifest_sha256,
            source_catalog_sha256,target_coverage_sha256,seal_sha256,
            target_schema_version,schema_fingerprint,cutover_ready
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        _legacy_catalog_exact.seal_values(catalog),
    )
    connection.execute(
        """
        INSERT INTO legacy_repository_proof_control(
            singleton,authority,authority_state,repository_epoch,
            repository_revision,catalog_revision,cutover_id,
            runtime_generation,code_digest,cutover_ready
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        _legacy_repository_exact.control_values(repository),
    )
    connection.execute(
        "INSERT INTO transaction_receipts(transaction_id,kind,committed_at) "
        "VALUES (?,?,?)",
        (
            repository.integrity.transaction_id,
            LEGACY_REPOSITORY_TRANSACTION_KIND,
            TEST_CUTOVER_COMMITTED_AT,
        ),
    )
    connection.execute(
        """
        INSERT INTO legacy_repository_seals(
            repository_singleton,cutover_id,transaction_id,receipt_kind,
            receipt_protocol,catalog_id,catalog_seal_sha256,proof_protocol,
            manifest_schema,status,selection,repository_epoch,
            repository_revision,catalog_revision,runtime_generation,
            code_digest,project_count,live_project_count,
            deleted_project_count,source_state_bytes,manifest_bytes,
            manifest_sha256,source_repository_sha256,
            catalog_coverage_sha256,proof_sha256,target_schema_version,
            schema_fingerprint,cutover_ready
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        _legacy_repository_exact.seal_values(repository),
    )


def _install_v12_test_closure(
    connection: Any,
    *,
    plan: Any,
    coverage_plan: Any,
) -> Optional[dict[str, bytes]]:
    """Install one semantically valid empty V12 closure for private fixtures."""

    exists = connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='table' "
        "AND name='business_cutover_install_closures'"
    ).fetchone()
    if exists is None:
        return None

    cutover_id = plan.cutover_id
    install_transaction_id = plan.install.transaction_id
    install_sha256 = plan.install.manifest_sha256
    member_mapping_bytes = plan.closure.member_mapping_bytes
    member_mapping_sha256 = plan.closure.member_mapping_sha256
    expected_member_mapping_sha256 = _install_digest(
        BUSINESS_CUTOVER_MEMBER_MAPPING_HASH_DOMAIN,
        member_mapping_bytes,
    )
    if not hmac.compare_digest(
        member_mapping_sha256,
        expected_member_mapping_sha256,
    ):
        raise AssertionError("test install receipt has a noncanonical member mapping")

    source_projection_v1 = {
        "schema": BUSINESS_CUTOVER_BASELINE_PROJECTION_SCHEMA,
        "protocol": BUSINESS_CUTOVER_BASELINE_PROJECTION_PROTOCOL,
        "status": BUSINESS_CUTOVER_BASELINE_PROJECTION_STATUS,
        "cutover_id": cutover_id,
        "committed_at": TEST_CUTOVER_COMMITTED_AT,
        "runtime_repository_revision": 1,
        "project_count": 0,
        "live_project_count": 0,
        "deleted_project_count": 0,
        "total_snapshot_bytes": 0,
        "entity_row_count": 0,
        "asset_reference_row_count": 0,
        "unique_asset_blob_count": 0,
        "outer_atomic_status": (
            BUSINESS_CUTOVER_BASELINE_V1_OUTER_ATOMIC_STATUS
        ),
        "asset_blobs": [],
        "scheduler_genesis": {
            "state_revision": 1,
            "worker_sequence": 0,
            "outbox_allocation_sequence": 0,
            "outbox_dispatch_revision": 1,
            "empty_sha256": (b"\0" * 32).hex(),
        },
        "members": [],
    }
    source_projection_v1_bytes = encode_payload_bounded(
        source_projection_v1,
        MAX_ATOMIC_MANIFEST_BYTES,
        MAX_ATOMIC_MANIFEST_NODES,
    )
    source_projection_v1_sha256 = _projection_digest(
        source_projection_v1_bytes
    )
    baseline_projection = dict(source_projection_v1)
    baseline_projection.update({
        "schema": BUSINESS_CUTOVER_BASELINE_PROJECTION_V12_SCHEMA,
        "protocol": BUSINESS_CUTOVER_BASELINE_PROJECTION_V12_PROTOCOL,
        "outer_atomic_status": BUSINESS_CUTOVER_BASELINE_OUTER_ATOMIC_STATUS,
        "asset_blob_write": {
            "mode": "insert_available_v1_from_fresh_coverage",
            "created_at": TEST_CUTOVER_COMMITTED_AT,
            "count": 0,
            "asset_coverage": {
                "transaction_id": coverage_plan.transaction_id,
                "receipt_kind": coverage_plan.expected_evidence.receipt_kind,
                "manifest_sha256": coverage_plan.manifest_sha256.hex(),
                "coverage_sha256": coverage_plan.coverage.coverage_sha256.hex(),
                "project_root_sha256": (
                    coverage_plan.coverage.project_root_sha256.hex()
                ),
                "occurrence_root_sha256": (
                    coverage_plan.coverage.occurrence_root_sha256.hex()
                ),
                "blob_root_sha256": (
                    coverage_plan.coverage.blob_root_sha256.hex()
                ),
            },
        },
        "source_projection_v1": {
            "schema": BUSINESS_CUTOVER_BASELINE_PROJECTION_SCHEMA,
            "sha256": source_projection_v1_sha256.hex(),
            "asset_blob_metadata": [],
        },
    })
    baseline_projection_bytes = encode_payload_bounded(
        baseline_projection,
        MAX_ATOMIC_MANIFEST_BYTES,
        MAX_ATOMIC_MANIFEST_NODES,
    )
    baseline_projection_sha256 = _install_digest(
        BUSINESS_CUTOVER_BASELINE_PROJECTION_V12_HASH_DOMAIN,
        baseline_projection_bytes,
    )
    artifacts = (
        (
            "member_mapping",
            BUSINESS_CUTOVER_MEMBER_MAPPING_SCHEMA,
            member_mapping_bytes,
            member_mapping_sha256,
        ),
        (
            "baseline_projection",
            BUSINESS_CUTOVER_BASELINE_PROJECTION_V12_SCHEMA,
            baseline_projection_bytes,
            baseline_projection_sha256,
        ),
    )
    for artifact_kind, artifact_schema, artifact_bytes, artifact_sha256 in artifacts:
        connection.execute(
            "INSERT INTO business_cutover_install_artifacts("
            "cutover_id,artifact_kind,artifact_schema,byte_length,"
            "chunk_count,artifact_sha256) VALUES (?,?,?,?,1,?)",
            (
                cutover_id,
                artifact_kind,
                artifact_schema,
                len(artifact_bytes),
                artifact_sha256,
            ),
        )
        chunk_sha256 = _install_digest(
            BUSINESS_CUTOVER_ARTIFACT_CHUNK_HASH_DOMAIN,
            _install_text(cutover_id),
            _install_text(artifact_kind),
            _install_integer(0),
            _install_integer(0),
            artifact_bytes,
        )
        connection.execute(
            "INSERT INTO business_cutover_install_artifact_chunks("
            "cutover_id,artifact_kind,ordinal,byte_offset,chunk_bytes,"
            "chunk_sha256) VALUES (?,?,0,0,?,?)",
            (cutover_id, artifact_kind, artifact_bytes, chunk_sha256),
        )

    baseline_outer_root = _install_baseline_receipt_root(())
    install_closure_manifest = encode_payload_bounded(
        {
            "schema": BUSINESS_CUTOVER_INSTALL_CLOSURE_SCHEMA,
            "protocol": BUSINESS_CUTOVER_INSTALL_CLOSURE_PROTOCOL,
            "status": BUSINESS_CUTOVER_INSTALL_CLOSURE_STATUS,
            "cutover_id": cutover_id,
            "install": {
                "transaction_id": install_transaction_id,
                "manifest_sha256": install_sha256.hex(),
                "committed_at": TEST_CUTOVER_COMMITTED_AT,
            },
            "schema_target": {
                "version": MIGRATIONS[-1].version,
                "fingerprint": _expected_schema_fingerprint(),
            },
            "source_proof": _install_source_proof_manifest(
                plan,
                member_root_sha256=_repository_member_root(plan),
            ),
            "asset_coverage": _install_asset_coverage_manifest(
                coverage_plan,
                member_root_sha256=_repository_member_root(plan),
            ),
            "artifacts": [
                {
                    "kind": artifact_kind,
                    "schema": artifact_schema,
                    "byte_length": len(artifact_bytes),
                    "chunk_count": 1,
                    "sha256": artifact_sha256.hex(),
                }
                for (
                    artifact_kind,
                    artifact_schema,
                    artifact_bytes,
                    artifact_sha256,
                ) in artifacts
            ],
            "projects": {
                "count": 0,
                "live_count": 0,
                "deleted_count": 0,
            },
            "baseline_receipts": {
                "count": 0,
                "root_sha256": baseline_outer_root.hex(),
                "members": [],
            },
        },
        MAX_ATOMIC_MANIFEST_BYTES,
        MAX_ATOMIC_MANIFEST_NODES,
    )
    install_closure_sha256 = _install_closure_manifest_digest(
        install_closure_manifest
    )
    connection.execute(
        """
        INSERT INTO business_cutover_install_closures(
            cutover_id,install_transaction_id,install_manifest_sha256,
            source_transaction_id,source_receipt_kind,
            source_admission_revision,source_proof_manifest_sha256,
            source_proof_sha256,source_admission_sha256,
            coverage_transaction_id,coverage_receipt_kind,
            coverage_manifest_sha256,coverage_sha256,
            committed_at,member_mapping_kind,member_mapping_sha256,
            baseline_projection_kind,baseline_projection_sha256,
            project_count,live_project_count,deleted_project_count,
            baseline_commit_count,baseline_outer_receipt_root_sha256,
            manifest_bytes,manifest_sha256
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'member_mapping',?,
                  'baseline_projection',?,
                  0,0,0,0,?,?,?)
        """,
        (
            cutover_id,
            install_transaction_id,
            install_sha256,
            plan.legacy_proof.transaction_id,
            LEGACY_REPOSITORY_TRANSACTION_KIND,
            plan.legacy_proof.source_admission_revision,
            plan.legacy_proof.manifest_sha256,
            plan.legacy_proof.proof_sha256,
            plan.closure.source_admission_sha256,
            coverage_plan.transaction_id,
            coverage_plan.expected_evidence.receipt_kind,
            coverage_plan.manifest_sha256,
            coverage_plan.coverage.coverage_sha256,
            TEST_CUTOVER_COMMITTED_AT,
            member_mapping_sha256,
            baseline_projection_sha256,
            baseline_outer_root,
            install_closure_manifest,
            install_closure_sha256,
        ),
    )
    return {
        "install_closure_sha256": install_closure_sha256,
        "baseline_outer_root": baseline_outer_root,
    }


def _canonical_v13_fixture_evidence(
    *,
    cutover_id: str,
    repository_epoch: str,
    source_admission_revision: int,
    runtime_generation: str,
    code_digest: bytes,
    install: BusinessCutoverReceiptPointer,
    coverage: BusinessCutoverAssetCoverageCheckpoint,
    certificate_id: str,
    audit_receipts: tuple[int, bytes],
) -> dict[str, Any]:
    """Issue canonical audit, activation and mount evidence before INSERT."""

    source_admission = BusinessCutoverAdmission(
        "legacy",
        repository_epoch,
        source_admission_revision,
        "",
    )
    pending_admission = BusinessCutoverAdmission(
        "cutover_pending",
        repository_epoch,
        source_admission_revision + 1,
        cutover_id,
    )
    final_admission = BusinessCutoverAdmission(
        "sqlite",
        repository_epoch,
        source_admission_revision + 2,
        cutover_id,
    )
    source_admission_sha256 = digest_business_cutover_admission(
        source_admission
    )
    pending_admission_sha256 = digest_business_cutover_admission(
        pending_admission
    )
    final_admission_sha256 = digest_business_cutover_admission(
        final_admission
    )
    schema = RepositoryAuditSchemaObservation(
        user_version=MIGRATIONS[-1].version,
        schema_fingerprint=_expected_schema_fingerprint(),
        migration_ledger_count=MIGRATIONS[-1].version,
        migration_ledger_sha256=_expected_migration_ledger_sha256(),
    )
    audit_runtime = RepositoryAuditRuntimeBinding(
        authority="sqlite",
        authority_state=RepositoryAuditAuthorityState.READ_ONLY,
        repository_epoch=repository_epoch,
        cutover_id=cutover_id,
        source_admission_revision=source_admission_revision,
        repository_revision=1,
        state_revision=1,
        runtime_generation=runtime_generation,
        code_digest=code_digest,
        schema_version=BUSINESS_CUTOVER_RUNTIME_CONTROL_SCHEMA_VERSION,
        updated_at=TEST_CUTOVER_COMMITTED_AT,
    )
    audit_coverage = RepositoryAuditCoverage(
        covered_business_revision=1,
        worker_ledger_sequence=0,
        worker_ledger_tail_sha256=EMPTY_WORKER_LEDGER_TAIL_SHA256,
        receipt_namespace_count=audit_receipts[0],
        receipt_namespace_sha256=audit_receipts[1],
        cutover_install_manifest_sha256=install.manifest_sha256,
    )
    findings = RepositoryAuditFindings(
        page_integrity="ok",
        foreign_key_violation_count=0,
        namespace_violation_count=0,
        started_at=TEST_CUTOVER_COMMITTED_AT,
        completed_at=TEST_CUTOVER_COMMITTED_AT,
    )
    audit_observed = RepositoryAuditObservedInput(
        certificate_id=certificate_id,
        purpose=RepositoryAuditPurpose.INSTALL,
        runtime=audit_runtime,
        schema=schema,
        coverage=audit_coverage,
        findings=findings,
    )
    audit_receipt = RepositoryAuditGenericReceiptEvidence(
        "_repository-audit:" + certificate_id,
        "_repository_audit_certificate_v1",
        TEST_CUTOVER_COMMITTED_AT,
    )
    audit_manifest = _build_audit_manifest(
        audit_observed,
        sequence=1,
        receipt=audit_receipt,
        previous_certificate_id=None,
        previous_manifest_sha256=None,
        previous_certificate_tail_sha256=None,
    )
    audit_manifest_sha256 = _audit_manifest_digest(audit_manifest)
    certificate_tail_sha256 = _certificate_tail_digest(
        1,
        EMPTY_REPOSITORY_AUDIT_CERTIFICATE_TAIL_SHA256,
        audit_manifest_sha256,
    )
    audit_evidence = _require_repository_audit_certificate_evidence(
        RepositoryAuditCertificateEvidence(
            certificate_id=certificate_id,
            purpose=RepositoryAuditPurpose.INSTALL,
            sequence=1,
            runtime=audit_runtime,
            schema=schema,
            coverage=audit_coverage,
            findings=findings,
            receipt_parent=audit_receipt,
            previous_certificate_id=None,
            previous_manifest_sha256=None,
            previous_certificate_tail_sha256=None,
            manifest_bytes=audit_manifest,
            manifest_sha256=audit_manifest_sha256,
            certificate_tail_sha256=certificate_tail_sha256,
        )
    )

    before_runtime = BusinessCutoverRuntimeBinding(
        authority="sqlite",
        authority_state="read_only",
        repository_epoch=repository_epoch,
        cutover_id=cutover_id,
        source_admission_revision=source_admission_revision,
        repository_revision=1,
        state_revision=1,
        runtime_generation=runtime_generation,
        code_digest=code_digest,
        schema_version=BUSINESS_CUTOVER_RUNTIME_CONTROL_SCHEMA_VERSION,
    )
    after_runtime = BusinessCutoverRuntimeBinding(
        authority="sqlite",
        authority_state="active",
        repository_epoch=repository_epoch,
        cutover_id=cutover_id,
        source_admission_revision=source_admission_revision,
        repository_revision=1,
        state_revision=2,
        runtime_generation=runtime_generation,
        code_digest=code_digest,
        schema_version=BUSINESS_CUTOVER_RUNTIME_CONTROL_SCHEMA_VERSION,
    )
    activation_receipt = BusinessCutoverActivationGenericReceiptEvidence(
        "_business-cutover-activation:" + cutover_id,
        "_business_cutover_activation_v2",
        TEST_CUTOVER_COMMITTED_AT,
    )
    activation_manifest = _build_activation_manifest(
        cutover_id=cutover_id,
        receipt=activation_receipt,
        final_admission=final_admission,
        final_admission_sha256=final_admission_sha256,
        before_runtime=before_runtime,
        after_runtime=after_runtime,
        install_transaction_id=install.transaction_id,
        install_receipt_kind=install.receipt_kind,
        install_manifest_sha256=install.manifest_sha256,
        audit_certificate_id=audit_evidence.certificate_id,
        audit_certificate_sequence=audit_evidence.sequence,
        audit_purpose=audit_evidence.purpose,
        audit_transaction_id=audit_evidence.transaction_id,
        audit_receipt_kind=audit_evidence.receipt_kind,
        audit_manifest_sha256=audit_evidence.manifest_sha256,
        audit_certificate_tail_sha256=(
            audit_evidence.certificate_tail_sha256
        ),
        audit_completed_at=findings.completed_at,
        expected_migration_ledger_count=schema.migration_ledger_count,
        expected_migration_ledger_sha256=schema.migration_ledger_sha256,
        actual_schema=schema,
        activated_at=TEST_CUTOVER_COMMITTED_AT,
    )
    activation_manifest_sha256 = _activation_manifest_digest(
        activation_manifest
    )
    activation_evidence = _require_business_cutover_activation_evidence(
        CanonicalActivationEvidence(
            cutover_id=cutover_id,
            receipt_parent=activation_receipt,
            final_admission=final_admission,
            final_admission_sha256=final_admission_sha256,
            before_runtime=before_runtime,
            after_runtime=after_runtime,
            install_transaction_id=install.transaction_id,
            install_receipt_kind=install.receipt_kind,
            install_manifest_sha256=install.manifest_sha256,
            audit_certificate_id=audit_evidence.certificate_id,
            audit_certificate_sequence=audit_evidence.sequence,
            audit_transaction_id=audit_evidence.transaction_id,
            audit_receipt_kind=audit_evidence.receipt_kind,
            audit_manifest_sha256=audit_evidence.manifest_sha256,
            audit_certificate_tail_sha256=(
                audit_evidence.certificate_tail_sha256
            ),
            audit_completed_at=findings.completed_at,
            expected_migration_ledger_count=schema.migration_ledger_count,
            expected_migration_ledger_sha256=schema.migration_ledger_sha256,
            actual_schema=schema,
            activated_at=TEST_CUTOVER_COMMITTED_AT,
            manifest_bytes=activation_manifest,
            manifest_sha256=activation_manifest_sha256,
        )
    )

    mount_receipt = BusinessCutoverMountGenericReceiptEvidence(
        "_business-cutover-mount:" + cutover_id,
        BUSINESS_CUTOVER_MOUNT_TRANSACTION_KIND,
        TEST_CUTOVER_COMMITTED_AT,
    )
    mount_runtime = BusinessCutoverMountRuntimeIdentity(
        authority="sqlite",
        authority_state="active",
        repository_epoch=repository_epoch,
        source_admission_revision=source_admission_revision,
        repository_revision=1,
        state_revision=2,
        runtime_generation=runtime_generation,
        code_digest=code_digest,
        runtime_schema_version=BUSINESS_CUTOVER_RUNTIME_CONTROL_SCHEMA_VERSION,
        target_schema_version=MIGRATIONS[-1].version,
        target_schema_fingerprint=schema.schema_fingerprint,
    )
    mount_install = BusinessCutoverMountInstallBinding(
        transaction_id=install.transaction_id,
        receipt_kind=install.receipt_kind,
        committed_at=install.committed_at,
        manifest_sha256=install.manifest_sha256,
    )
    mount_coverage = BusinessCutoverMountCoverageBinding(
        transaction_id=coverage.pointer.transaction_id,
        receipt_kind=coverage.pointer.receipt_kind,
        committed_at=coverage.pointer.committed_at,
        manifest_sha256=coverage.pointer.manifest_sha256,
        coverage_sha256=coverage.coverage_sha256,
        member_root_sha256=coverage.member_root_sha256,
        batch_count=coverage.batch_count,
        repository_member_count=coverage.repository_member_count,
        live_project_count=coverage.live_project_count,
        deleted_project_count=coverage.deleted_project_count,
        project_count=coverage.project_count,
        reference_count=coverage.reference_count,
        unique_blob_count=coverage.unique_blob_count,
        unique_blob_bytes=coverage.unique_blob_bytes,
        referenced_bytes=coverage.referenced_bytes,
        observed_occurrence_count=coverage.observed_occurrence_count,
        observed_row_count=coverage.observed_row_count,
        observed_manifest_bytes=coverage.observed_manifest_bytes,
    )
    mount_activation = BusinessCutoverMountActivationBinding(
        transaction_id=activation_evidence.transaction_id,
        receipt_kind=activation_evidence.receipt_kind,
        manifest_sha256=activation_evidence.manifest_sha256,
        audit_certificate_id=audit_evidence.certificate_id,
        audit_certificate_sequence=audit_evidence.sequence,
        audit_transaction_id=audit_evidence.transaction_id,
        audit_receipt_kind=audit_evidence.receipt_kind,
        audit_manifest_sha256=audit_evidence.manifest_sha256,
        audit_certificate_tail_sha256=(
            audit_evidence.certificate_tail_sha256
        ),
        expected_migration_ledger_count=schema.migration_ledger_count,
        expected_migration_ledger_sha256=schema.migration_ledger_sha256,
        audit_completed_at=findings.completed_at,
        activated_at=activation_evidence.activated_at,
    )
    mount_manifest = _build_mount_manifest(
        cutover_id=cutover_id,
        receipt=mount_receipt,
        runtime=mount_runtime,
        source_admission_sha256=source_admission_sha256,
        final_admission_sha256=final_admission_sha256,
        install=mount_install,
        coverage=mount_coverage,
        activation=mount_activation,
        mounted_at=TEST_CUTOVER_COMMITTED_AT,
    )
    mount_manifest_sha256 = _mount_manifest_digest(mount_manifest)
    mount_evidence = _require_business_cutover_mount_evidence(
        CanonicalMountEvidence(
            cutover_id=cutover_id,
            receipt_parent=mount_receipt,
            runtime=mount_runtime,
            source_admission_sha256=source_admission_sha256,
            final_admission_sha256=final_admission_sha256,
            install=mount_install,
            coverage=mount_coverage,
            activation=mount_activation,
            mounted_at=TEST_CUTOVER_COMMITTED_AT,
            manifest_bytes=mount_manifest,
            manifest_sha256=mount_manifest_sha256,
        )
    )
    return {
        "source_admission_sha256": source_admission_sha256,
        "pending_admission_sha256": pending_admission_sha256,
        "final_admission_sha256": final_admission_sha256,
        "schema": schema,
        "findings_sha256": _findings_digest(findings),
        "audit_manifest": audit_manifest,
        "audit_manifest_sha256": audit_manifest_sha256,
        "certificate_tail_sha256": certificate_tail_sha256,
        "activation_manifest": activation_manifest,
        "activation_manifest_sha256": activation_manifest_sha256,
        "mount_manifest": mount_manifest,
        "mount_manifest_sha256": mount_manifest_sha256,
        "head_chain": _require_business_cutover_head_chain(
            issue_business_cutover_head_chain_from_mount(mount_evidence)
        ),
    }


def _has_complete_v13_binding_schema(connection: Any) -> bool:
    """Distinguish an authentic current schema from a historical registry."""

    tables = {
        layout.table for layout in _BUSINESS_CUTOVER_PHASE_BINDING_LAYOUTS
    }
    guards = {
        "business_cutover_head_v13_%d_binding_guard" % revision
        for revision in range(2, 6)
    }
    names = tables | guards
    placeholders = ",".join("?" for _name in names)
    rows = connection.execute(
        "SELECT type,name FROM sqlite_schema WHERE name IN (" +
        placeholders + ")",
        tuple(sorted(names)),
    ).fetchall()
    actual_tables = {row[1] for row in rows if row[0] == "table"}
    actual_guards = {row[1] for row in rows if row[0] == "trigger"}
    if not actual_tables and not actual_guards:
        return False
    if actual_tables != tables or actual_guards != guards:
        raise AssertionError("test authority observed a partial V13 binding schema")
    return True


def _binding_pointer(
    transaction_id: str,
    receipt_kind: str,
    manifest_sha256: bytes,
) -> BusinessCutoverReceiptPointer:
    return BusinessCutoverReceiptPointer(
        transaction_id=transaction_id,
        receipt_kind=receipt_kind,
        committed_at=TEST_CUTOVER_COMMITTED_AT,
        manifest_sha256=manifest_sha256,
    )


def _binding_head(state: Any) -> BusinessCutoverPhaseBindingHead:
    values = state.row_values()
    pointers = {
        pointer.role: BusinessCutoverReceiptPointer(
            transaction_id=pointer.transaction_id,
            receipt_kind=pointer.receipt_kind,
            committed_at=pointer.occurred_at,
            manifest_sha256=pointer.manifest_sha256,
        )
        for pointer in state.pointers
    }
    return BusinessCutoverPhaseBindingHead(
        state_revision=values["state_revision"],
        phase_revision=values["phase_revision"],
        phase=values["phase"],
        authority_state=values["authority_state"],
        head_sha256=values["head_sha256"],
        updated_at=values["updated_at"],
        previous_head_sha256=values["previous_head_sha256"],
        install=pointers[BusinessCutoverHeadPointerRole.INSTALL],
        coverage=pointers.get(BusinessCutoverHeadPointerRole.COVERAGE),
        audit=pointers.get(BusinessCutoverHeadPointerRole.AUDIT),
        activation=pointers.get(BusinessCutoverHeadPointerRole.ACTIVATION),
        mount=pointers.get(BusinessCutoverHeadPointerRole.MOUNT),
    )


def _binding_checkpoint(
    connection: Any,
    *,
    before_receipts: tuple[int, bytes],
    after_receipts: tuple[int, bytes],
) -> BusinessCutoverDurableCheckpoint:
    runtime = connection.execute(
        "SELECT authority,authority_state,source_admission_revision,"
        "repository_revision,state_revision,schema_version,updated_at "
        "FROM repository_runtime_control WHERE singleton=1"
    ).fetchone()
    worker = connection.execute(
        "SELECT state_revision,sequence,tail_sha256,updated_at "
        "FROM worker_ledger_head WHERE singleton=1"
    ).fetchone()
    allocation = connection.execute(
        "SELECT state_revision,sequence,tail_sha256,updated_at "
        "FROM outbox_allocation_head WHERE singleton=1"
    ).fetchone()
    dispatch = connection.execute(
        "SELECT state_revision,published_sequence,"
        "published_repository_revision,published_ordinal,inflight_sequence,"
        "inflight_repository_revision,inflight_ordinal,head_sha256,updated_at "
        "FROM outbox_dispatch_head WHERE singleton=1"
    ).fetchone()
    if (
        runtime is None or len(runtime) != 7
        or worker is None or len(worker) != 4
        or allocation is None or len(allocation) != 4
        or dispatch is None or len(dispatch) != 9
    ):
        raise AssertionError("test phase binding checkpoint is incomplete")
    return BusinessCutoverDurableCheckpoint(
        runtime=BusinessCutoverRuntimeImage(*runtime),
        receipt_before_count=before_receipts[0],
        receipt_before_sha256=before_receipts[1],
        receipt_after_count=after_receipts[0],
        receipt_after_sha256=after_receipts[1],
        worker=BusinessCutoverWorkerHighWater(*worker),
        allocation=BusinessCutoverAllocationHighWater(*allocation),
        dispatch=BusinessCutoverDispatchHighWater(*dispatch),
    )


def _issue_fixture_phase_binding(
    connection: Any,
    *,
    stage: BusinessCutoverPhaseBindingStage,
    old_state: Any,
    new_state: Any,
    direct_evidence: Any,
    repository_epoch: str,
    source_admission_revision: int,
    source_admission_sha256: bytes,
    source_freeze_sha256: bytes,
    pending_admission_sha256: bytes,
    final_admission_sha256: bytes,
    runtime_generation: str,
    code_digest: bytes,
    before_receipts: tuple[int, bytes],
    after_receipts: tuple[int, bytes],
) -> BusinessCutoverPhaseBinding:
    pending = stage in (
        BusinessCutoverPhaseBindingStage.ASSET_PROVIDER,
        BusinessCutoverPhaseBindingStage.AUDIT,
    )
    projection = BusinessCutoverPhaseBindingProjection(
        stage=stage,
        identity=BusinessCutoverPhaseBindingIdentity(
            cutover_id=old_state.row_values()["cutover_id"],
            repository_epoch=repository_epoch,
            runtime_generation=runtime_generation,
            code_digest=code_digest,
        ),
        old_head=_binding_head(old_state),
        new_head=_binding_head(new_state),
        admission=BusinessCutoverAdmissionCheckpoint(
            source_revision=source_admission_revision,
            source_admission_sha256=source_admission_sha256,
            source_freeze_sha256=source_freeze_sha256,
            file_authority_state="cutover_pending" if pending else "sqlite",
            file_authority_epoch=repository_epoch,
            file_revision=source_admission_revision + (1 if pending else 2),
            file_cutover_id=old_state.row_values()["cutover_id"],
            file_admission_sha256=(
                pending_admission_sha256 if pending
                else final_admission_sha256
            ),
        ),
        maintenance=BusinessCutoverMaintenanceCheckpoint(
            scope="catalog_ex_repository_ex",
            proof_sha256=source_freeze_sha256,
            observed_at=TEST_CUTOVER_COMMITTED_AT,
        ),
        checkpoint=_binding_checkpoint(
            connection,
            before_receipts=before_receipts,
            after_receipts=after_receipts,
        ),
        direct_evidence=direct_evidence,
    )
    return verify_business_cutover_phase_binding(
        issue_business_cutover_phase_binding(projection)
    )


def _insert_fixture_phase_binding(
    connection: Any,
    binding: BusinessCutoverPhaseBinding,
) -> None:
    persisted = _project_business_cutover_phase_binding_row(binding)
    schema_columns = tuple(
        row[1] for row in connection.execute(
            "PRAGMA table_info(" + persisted.table + ")"
        ).fetchall()
    )
    if persisted.columns != schema_columns:
        raise AssertionError("test phase binding SQL column map drifted")
    connection.execute(
        "INSERT INTO " + persisted.table + "(" +
        ",".join(persisted.columns) + ") VALUES (" +
        ",".join("?" for _column in persisted.columns) + ")",
        persisted.values,
    )


def _insert_fixture_cutover_head(connection: Any, state: Any) -> None:
    values = state.row_values()
    connection.execute(
        """
        INSERT INTO business_cutover_head(
            singleton,cutover_id,state_revision,phase_revision,phase,
            authority_state,repository_epoch,runtime_generation,code_digest,
            install_transaction_id,install_manifest_sha256,
            previous_head_sha256,head_sha256,updated_at
        ) VALUES (1,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            values["cutover_id"],
            values["state_revision"],
            values["phase_revision"],
            values["phase"],
            values["authority_state"],
            values["repository_epoch"],
            values["runtime_generation"],
            values["code_digest"],
            values["install_transaction_id"],
            values["install_manifest_sha256"],
            values["previous_head_sha256"],
            values["head_sha256"],
            values["updated_at"],
        ),
    )


def _advance_fixture_cutover_head(connection: Any, state: Any) -> None:
    values = state.row_values()
    connection.execute(
        """
        UPDATE business_cutover_head
        SET state_revision=?,phase_revision=?,phase=?,authority_state=?,
            coverage_transaction_id=?,coverage_manifest_sha256=?,
            audit_transaction_id=?,audit_manifest_sha256=?,
            activation_transaction_id=?,activation_manifest_sha256=?,
            mount_transaction_id=?,mount_manifest_sha256=?,
            previous_head_sha256=?,head_sha256=?,updated_at=?
        WHERE singleton=1
        """,
        (
            values["state_revision"],
            values["phase_revision"],
            values["phase"],
            values["authority_state"],
            values["coverage_transaction_id"],
            values["coverage_manifest_sha256"],
            values["audit_transaction_id"],
            values["audit_manifest_sha256"],
            values["activation_transaction_id"],
            values["activation_manifest_sha256"],
            values["mount_transaction_id"],
            values["mount_manifest_sha256"],
            values["previous_head_sha256"],
            values["head_sha256"],
            values["updated_at"],
        ),
    )




def _install_mounted_test_authority(
    connection: Any,
    *,
    repository_epoch: str,
    source_admission_revision: int,
    runtime_generation: str,
    code_digest: bytes,
    authority_state: BusinessAuthorityState = BusinessAuthorityState.ACTIVE,
) -> str:
    """Install an exact empty-source mount and return its derived identity."""

    if type(authority_state) is not BusinessAuthorityState:
        raise TypeError("authority_state must be a BusinessAuthorityState")

    catalog, repository, plan, coverage_plan = _prepare_empty_cutover_authority(
        repository_epoch=repository_epoch,
        source_admission_revision=source_admission_revision,
        runtime_generation=runtime_generation,
        code_digest=code_digest,
    )
    cutover_id = plan.cutover_id
    _insert_empty_legacy_source_proof(
        connection,
        catalog=catalog,
        repository=repository,
    )

    zero = b"\x00" * 32
    install_transaction_id = plan.install.transaction_id
    coverage_transaction_id = coverage_plan.transaction_id
    certificate_id = _test_digest("audit-certificate", cutover_id).hex()[:32]
    audit_transaction_id = "_repository-audit:" + certificate_id
    activation_transaction_id = "_business-cutover-activation:" + cutover_id
    mount_transaction_id = "_business-cutover-mount:" + cutover_id

    install_manifest = plan.install.manifest_bytes
    install_sha256 = plan.install.manifest_sha256
    coverage_manifest = coverage_plan.manifest_bytes
    coverage_sha256 = coverage_plan.manifest_sha256
    audit_manifest, audit_sha256 = _test_manifest("audit", cutover_id)
    activation_manifest, activation_sha256 = _test_manifest(
        "activation", cutover_id
    )
    mount_manifest, mount_sha256 = _test_manifest("mount", cutover_id)
    member_mapping_sha256 = plan.closure.member_mapping_sha256

    _insert_generic_receipt(
        connection,
        coverage_transaction_id,
        coverage_plan.expected_evidence.receipt_kind,
    )
    connection.execute(
        """
        INSERT INTO business_asset_coverage_receipts(
            cutover_id,transaction_id,receipt_kind,committed_at,
            repository_epoch,source_admission_revision,
            source_admission_sha256,batch_count,member_root_sha256,
            repository_member_count,live_project_count,
            deleted_project_count,observed_occurrence_count,
            observed_row_count,observed_manifest_bytes,project_count,
            reference_count,unique_blob_count,unique_blob_bytes,
            referenced_bytes,project_root_sha256,blob_root_sha256,
            occurrence_root_sha256,coverage_sha256,
            manifest_bytes,manifest_sha256
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            cutover_id,
            coverage_transaction_id,
            coverage_plan.expected_evidence.receipt_kind,
            TEST_CUTOVER_COMMITTED_AT,
            repository_epoch,
            source_admission_revision,
            coverage_plan.source_admission_sha256,
            coverage_plan.expected_evidence.batch_count,
            _repository_member_root(plan),
            plan.legacy_proof.project_count,
            plan.legacy_proof.live_project_count,
            plan.legacy_proof.deleted_project_count,
            coverage_plan.expected_evidence.observed_occurrence_count,
            coverage_plan.expected_evidence.observed_row_count,
            coverage_plan.expected_evidence.observed_manifest_bytes,
            coverage_plan.coverage.project_count,
            coverage_plan.coverage.reference_count,
            coverage_plan.coverage.unique_blob_count,
            coverage_plan.coverage.unique_blob_bytes,
            coverage_plan.coverage.referenced_bytes,
            coverage_plan.coverage.project_root_sha256,
            coverage_plan.coverage.blob_root_sha256,
            coverage_plan.coverage.occurrence_root_sha256,
            coverage_plan.coverage.coverage_sha256,
            coverage_manifest,
            coverage_sha256,
        ),
    )
    _insert_generic_receipt(
        connection,
        install_transaction_id,
        plan.install.receipt_kind,
    )
    connection.execute(
        """
        INSERT INTO business_cutover_install_receipts(
            cutover_id,transaction_id,receipt_kind,committed_at,
            repository_epoch,runtime_generation,code_digest,
            member_mapping_sha256,manifest_bytes,manifest_sha256
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (
            cutover_id,
            install_transaction_id,
            plan.install.receipt_kind,
            TEST_CUTOVER_COMMITTED_AT,
            repository_epoch,
            runtime_generation,
            code_digest,
            member_mapping_sha256,
            install_manifest,
            install_sha256,
        ),
    )
    connection.execute(
        """
        INSERT INTO worker_ledger_head(
            singleton,state_revision,sequence,previous_tail_sha256,
            tail_sha256,updated_at
        ) VALUES (1,1,0,?,?,?)
        """,
        (zero, zero, TEST_CUTOVER_COMMITTED_AT),
    )
    connection.execute(
        """
        INSERT INTO outbox_allocation_head(
            singleton,state_revision,sequence,previous_tail_sha256,
            tail_sha256,updated_at
        ) VALUES (1,1,0,?,?,?)
        """,
        (zero, zero, TEST_CUTOVER_COMMITTED_AT),
    )
    connection.execute(
        """
        INSERT INTO outbox_dispatch_head(
            singleton,state_revision,published_sequence,
            published_repository_revision,published_ordinal,
            inflight_sequence,inflight_repository_revision,inflight_ordinal,
            previous_head_sha256,head_sha256,updated_at
        ) VALUES (1,1,0,NULL,NULL,NULL,NULL,NULL,?,?,?)
        """,
        (zero, zero, TEST_CUTOVER_COMMITTED_AT),
    )
    connection.execute(
        """
        INSERT INTO repository_runtime_control(
            authority,authority_state,repository_epoch,cutover_id,
            source_admission_revision,repository_revision,state_revision,
            runtime_generation,code_digest,schema_version,updated_at
        ) VALUES ('sqlite','read_only',?,?,?,1,1,?,?,6,?)
        """,
        (
            repository_epoch,
            cutover_id,
            source_admission_revision,
            runtime_generation,
            code_digest,
            TEST_CUTOVER_COMMITTED_AT,
        ),
    )
    _install_v12_test_closure(
        connection,
        plan=plan,
        coverage_plan=coverage_plan,
    )
    head_chain = issue_business_cutover_head_chain(BusinessCutoverHeadInput(
        cutover_id=cutover_id,
        repository_epoch=repository_epoch,
        runtime_generation=runtime_generation,
        code_digest=code_digest,
        install=BusinessCutoverHeadPointer(
            BusinessCutoverHeadPointerRole.INSTALL,
            install_transaction_id,
            "_business_cutover_install_v1",
            TEST_CUTOVER_COMMITTED_AT,
            install_sha256,
        ),
        coverage=BusinessCutoverHeadPointer(
            BusinessCutoverHeadPointerRole.COVERAGE,
            coverage_transaction_id,
            "_business_cutover_asset_coverage_v1",
            TEST_CUTOVER_COMMITTED_AT,
            coverage_sha256,
        ),
        audit=BusinessCutoverHeadPointer(
            BusinessCutoverHeadPointerRole.AUDIT,
            audit_transaction_id,
            "_repository_audit_certificate_v1",
            TEST_CUTOVER_COMMITTED_AT,
            audit_sha256,
        ),
        activation=BusinessCutoverHeadPointer(
            BusinessCutoverHeadPointerRole.ACTIVATION,
            activation_transaction_id,
            "_business_cutover_activation_v2",
            TEST_CUTOVER_COMMITTED_AT,
            activation_sha256,
        ),
        mount=BusinessCutoverHeadPointer(
            BusinessCutoverHeadPointerRole.MOUNT,
            mount_transaction_id,
            "_business_cutover_mount_v2",
            TEST_CUTOVER_COMMITTED_AT,
            mount_sha256,
        ),
    ))
    has_v13_bindings = _has_complete_v13_binding_schema(connection)
    if (
        has_v13_bindings
        and authority_state is not BusinessAuthorityState.ACTIVE
    ):
        raise ValueError("the current V13 mounted fixture requires active authority")

    source_admission_sha256 = coverage_plan.source_admission_sha256
    source_freeze_sha256 = repository.integrity.proof_sha256
    pending_admission_sha256 = digest_business_cutover_admission(
        BusinessCutoverAdmission(
            "cutover_pending",
            repository_epoch,
            source_admission_revision + 1,
            cutover_id,
        )
    )
    final_admission_sha256 = digest_business_cutover_admission(
        BusinessCutoverAdmission(
            "sqlite",
            repository_epoch,
            source_admission_revision + 2,
            cutover_id,
        )
    )
    schema_fingerprint = _expected_schema_fingerprint()
    migration_ledger_sha256 = _expected_migration_ledger_sha256()
    install_pointer = _binding_pointer(
        install_transaction_id,
        "_business_cutover_install_v1",
        install_sha256,
    )
    coverage_pointer = _binding_pointer(
        coverage_transaction_id,
        "_business_cutover_asset_coverage_v1",
        coverage_sha256,
    )
    coverage_checkpoint = BusinessCutoverAssetCoverageCheckpoint(
        pointer=coverage_pointer,
        coverage_sha256=coverage_plan.coverage.coverage_sha256,
        member_root_sha256=_repository_member_root(plan),
        blob_root_sha256=coverage_plan.coverage.blob_root_sha256,
        batch_count=coverage_plan.expected_evidence.batch_count,
        repository_member_count=plan.legacy_proof.project_count,
        live_project_count=plan.legacy_proof.live_project_count,
        deleted_project_count=plan.legacy_proof.deleted_project_count,
        project_count=coverage_plan.coverage.project_count,
        reference_count=coverage_plan.coverage.reference_count,
        unique_blob_count=coverage_plan.coverage.unique_blob_count,
        unique_blob_bytes=coverage_plan.coverage.unique_blob_bytes,
        referenced_bytes=coverage_plan.coverage.referenced_bytes,
        observed_occurrence_count=(
            coverage_plan.expected_evidence.observed_occurrence_count
        ),
        observed_row_count=coverage_plan.expected_evidence.observed_row_count,
        observed_manifest_bytes=(
            coverage_plan.expected_evidence.observed_manifest_bytes
        ),
    )

    # Phase 1 is the durable predecessor.  Every current-schema transition
    # below inserts its canonical sidecar immediately before the guarded head
    # update, all inside the fixture caller's one transaction.
    _insert_fixture_cutover_head(connection, head_chain[0])

    asset_receipts = _read_receipt_namespace_identity(connection)
    if has_v13_bindings:
        provider = issue_business_cutover_asset_provider_evidence(
            coverage=coverage_checkpoint,
            provider_protocol=(
                "ze.business-cutover.asset-provider-checkpoint.v1"
            ),
            provider_id="fixture-asset-provider",
            provider_generation=runtime_generation,
            provider_sequence=1,
            checked_at=TEST_CUTOVER_COMMITTED_AT,
            object_count=coverage_checkpoint.unique_blob_count,
            total_bytes=coverage_checkpoint.unique_blob_bytes,
            blob_root_sha256=coverage_checkpoint.blob_root_sha256,
        )
        _insert_fixture_phase_binding(connection, _issue_fixture_phase_binding(
            connection,
            stage=BusinessCutoverPhaseBindingStage.ASSET_PROVIDER,
            old_state=head_chain[0],
            new_state=head_chain[1],
            direct_evidence=provider,
            repository_epoch=repository_epoch,
            source_admission_revision=source_admission_revision,
            source_admission_sha256=source_admission_sha256,
            source_freeze_sha256=source_freeze_sha256,
            pending_admission_sha256=pending_admission_sha256,
            final_admission_sha256=final_admission_sha256,
            runtime_generation=runtime_generation,
            code_digest=code_digest,
            before_receipts=asset_receipts,
            after_receipts=asset_receipts,
        ))
    _advance_fixture_cutover_head(connection, head_chain[1])

    audit_before = _read_receipt_namespace_identity(connection)
    if has_v13_bindings:
        canonical = _canonical_v13_fixture_evidence(
            cutover_id=cutover_id,
            repository_epoch=repository_epoch,
            source_admission_revision=source_admission_revision,
            runtime_generation=runtime_generation,
            code_digest=code_digest,
            install=install_pointer,
            coverage=coverage_checkpoint,
            certificate_id=certificate_id,
            audit_receipts=audit_before,
        )
        canonical_head_chain = canonical["head_chain"]
        if tuple(canonical_head_chain[:2]) != tuple(head_chain[:2]):
            raise AssertionError(
                "later canonical evidence changed an installed predecessor"
            )
        if not hmac.compare_digest(
            canonical["source_admission_sha256"],
            source_admission_sha256,
        ):
            raise AssertionError("canonical source admission changed")
        head_chain = canonical_head_chain
        pending_admission_sha256 = canonical["pending_admission_sha256"]
        final_admission_sha256 = canonical["final_admission_sha256"]
        schema_fingerprint = canonical["schema"].schema_fingerprint
        audit_manifest = canonical["audit_manifest"]
        audit_sha256 = canonical["audit_manifest_sha256"]
        activation_manifest = canonical["activation_manifest"]
        activation_sha256 = canonical["activation_manifest_sha256"]
        mount_manifest = canonical["mount_manifest"]
        mount_sha256 = canonical["mount_manifest_sha256"]
        certificate_tail = canonical["certificate_tail_sha256"]
        findings_sha256 = canonical["findings_sha256"]
    else:
        certificate_tail = _test_digest("audit-tail", certificate_id)
        findings_sha256 = _test_digest("audit-findings", cutover_id)
    _insert_generic_receipt(
        connection,
        audit_transaction_id,
        "_repository_audit_certificate_v1",
    )
    audit_after = _read_receipt_namespace_identity(connection)
    connection.execute(
        """
        INSERT INTO repository_audit_certificates(
            certificate_id,cutover_id,sequence,purpose,transaction_id,
            receipt_kind,committed_at,previous_certificate_id,
            previous_manifest_sha256,previous_tail_sha256,schema_version,
            migration_ledger_count,migration_ledger_sha256,receipt_count,
            receipt_root_sha256,worker_ledger_sequence,
            worker_ledger_tail_sha256,findings_sha256,manifest_bytes,
            manifest_sha256,certificate_tail_sha256
        ) VALUES (?,?,1,'install',?,?,?,NULL,NULL,?,?,?,?,?,?,0,?,?,?,?,?)
        """,
        (
            certificate_id,
            cutover_id,
            audit_transaction_id,
            "_repository_audit_certificate_v1",
            TEST_CUTOVER_COMMITTED_AT,
            zero,
            MIGRATIONS[-1].version,
            MIGRATIONS[-1].version,
            migration_ledger_sha256,
            audit_before[0],
            audit_before[1],
            zero,
            findings_sha256,
            audit_manifest,
            audit_sha256,
            certificate_tail,
        ),
    )
    connection.execute(
        """
        INSERT INTO repository_audit_head(
            singleton,state_revision,sequence,certificate_id,
            previous_tail_sha256,tail_sha256,updated_at
        ) VALUES (1,1,1,?,?,?,?)
        """,
        (certificate_id, zero, certificate_tail, TEST_CUTOVER_COMMITTED_AT),
    )
    audit_pointer = _binding_pointer(
        audit_transaction_id,
        "_repository_audit_certificate_v1",
        audit_sha256,
    )
    if has_v13_bindings:
        audit_evidence = BusinessCutoverAuditEvidence(
            certificate_id=certificate_id,
            certificate_sequence=1,
            purpose="install",
            receipt_parent=audit_pointer,
            previous_certificate_id=None,
            previous_tail_sha256=zero,
            certificate_tail_sha256=certificate_tail,
            findings_sha256=findings_sha256,
            schema_version=MIGRATIONS[-1].version,
            schema_fingerprint=schema_fingerprint,
            migration_ledger_count=MIGRATIONS[-1].version,
            migration_ledger_sha256=migration_ledger_sha256,
            install_manifest_sha256=install_sha256,
            covered_business_revision=1,
            started_at=TEST_CUTOVER_COMMITTED_AT,
            completed_at=TEST_CUTOVER_COMMITTED_AT,
            page_integrity="ok",
            foreign_key_violation_count=0,
            namespace_violation_count=0,
            audit_head_state_revision=1,
            audit_head_sequence=1,
            audit_head_certificate_id=certificate_id,
            audit_head_previous_tail_sha256=zero,
            audit_head_tail_sha256=certificate_tail,
            audit_head_updated_at=TEST_CUTOVER_COMMITTED_AT,
        )
        _insert_fixture_phase_binding(connection, _issue_fixture_phase_binding(
            connection,
            stage=BusinessCutoverPhaseBindingStage.AUDIT,
            old_state=head_chain[1],
            new_state=head_chain[2],
            direct_evidence=audit_evidence,
            repository_epoch=repository_epoch,
            source_admission_revision=source_admission_revision,
            source_admission_sha256=source_admission_sha256,
            source_freeze_sha256=source_freeze_sha256,
            pending_admission_sha256=pending_admission_sha256,
            final_admission_sha256=final_admission_sha256,
            runtime_generation=runtime_generation,
            code_digest=code_digest,
            before_receipts=audit_before,
            after_receipts=audit_after,
        ))
    _advance_fixture_cutover_head(connection, head_chain[2])

    activation_before = _read_receipt_namespace_identity(connection)
    _insert_generic_receipt(
        connection,
        activation_transaction_id,
        "_business_cutover_activation_v2",
    )
    activation_after = _read_receipt_namespace_identity(connection)
    connection.execute(
        """
        INSERT INTO business_cutover_activation_receipts(
            cutover_id,transaction_id,receipt_kind,committed_at,
            install_transaction_id,install_manifest_sha256,
            audit_transaction_id,audit_manifest_sha256,
            migration_ledger_sha256,manifest_bytes,manifest_sha256
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            cutover_id,
            activation_transaction_id,
            "_business_cutover_activation_v2",
            TEST_CUTOVER_COMMITTED_AT,
            install_transaction_id,
            install_sha256,
            audit_transaction_id,
            audit_sha256,
            migration_ledger_sha256,
            activation_manifest,
            activation_sha256,
        ),
    )
    activation_pointer = _binding_pointer(
        activation_transaction_id,
        "_business_cutover_activation_v2",
        activation_sha256,
    )
    if has_v13_bindings:
        before_runtime = _binding_checkpoint(
            connection,
            before_receipts=activation_before,
            after_receipts=activation_after,
        ).runtime
        activation_evidence = BusinessCutoverActivationEvidence(
            activation=activation_pointer,
            install=install_pointer,
            audit_certificate_id=certificate_id,
            audit_certificate_sequence=1,
            audit=audit_pointer,
            audit_binding=audit_evidence,
            audit_certificate_tail_sha256=certificate_tail,
            audit_completed_at=TEST_CUTOVER_COMMITTED_AT,
            expected_migration_ledger_count=MIGRATIONS[-1].version,
            expected_migration_ledger_sha256=migration_ledger_sha256,
            actual_schema_version=MIGRATIONS[-1].version,
            actual_schema_fingerprint=schema_fingerprint,
            final_admission_state="sqlite",
            final_admission_epoch=repository_epoch,
            final_admission_revision=source_admission_revision + 2,
            final_admission_cutover_id=cutover_id,
            final_admission_sha256=final_admission_sha256,
            before_runtime=before_runtime,
            after_runtime=BusinessCutoverRuntimeImage(
                authority="sqlite",
                authority_state="active",
                source_admission_revision=source_admission_revision,
                repository_revision=before_runtime.repository_revision,
                state_revision=before_runtime.state_revision + 1,
                schema_version=before_runtime.schema_version,
                updated_at=TEST_CUTOVER_COMMITTED_AT,
            ),
        )
        _insert_fixture_phase_binding(connection, _issue_fixture_phase_binding(
            connection,
            stage=BusinessCutoverPhaseBindingStage.ACTIVATION,
            old_state=head_chain[2],
            new_state=head_chain[3],
            direct_evidence=activation_evidence,
            repository_epoch=repository_epoch,
            source_admission_revision=source_admission_revision,
            source_admission_sha256=source_admission_sha256,
            source_freeze_sha256=source_freeze_sha256,
            pending_admission_sha256=pending_admission_sha256,
            final_admission_sha256=final_admission_sha256,
            runtime_generation=runtime_generation,
            code_digest=code_digest,
            before_receipts=activation_before,
            after_receipts=activation_after,
        ))
    _advance_fixture_cutover_head(connection, head_chain[3])

    connection.execute(
        """
        UPDATE repository_runtime_control
        SET authority_state=?,state_revision=2,updated_at=?
        WHERE singleton=1 AND authority_state='read_only' AND
              repository_revision=1 AND state_revision=1
        """,
        (authority_state.value, TEST_CUTOVER_COMMITTED_AT),
    )
    if connection.execute("SELECT changes()").fetchone()[0] != 1:
        raise AssertionError("test mount runtime activation CAS failed")

    mount_before = _read_receipt_namespace_identity(connection)
    _insert_generic_receipt(
        connection,
        mount_transaction_id,
        "_business_cutover_mount_v2",
    )
    mount_after = _read_receipt_namespace_identity(connection)
    connection.execute(
        """
        INSERT INTO business_cutover_mount_receipts(
            cutover_id,transaction_id,receipt_kind,committed_at,
            install_transaction_id,install_receipt_kind,install_committed_at,
            install_manifest_sha256,coverage_transaction_id,
            coverage_receipt_kind,coverage_committed_at,
            coverage_manifest_sha256,
            audit_transaction_id,audit_manifest_sha256,
            activation_transaction_id,activation_manifest_sha256,
            repository_epoch,runtime_generation,code_digest,
            manifest_bytes,manifest_sha256
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            cutover_id,
            mount_transaction_id,
            "_business_cutover_mount_v2",
            TEST_CUTOVER_COMMITTED_AT,
            install_transaction_id,
            "_business_cutover_install_v1",
            TEST_CUTOVER_COMMITTED_AT,
            install_sha256,
            coverage_transaction_id,
            "_business_cutover_asset_coverage_v1",
            TEST_CUTOVER_COMMITTED_AT,
            coverage_sha256,
            audit_transaction_id,
            audit_sha256,
            activation_transaction_id,
            activation_sha256,
            repository_epoch,
            runtime_generation,
            code_digest,
            mount_manifest,
            mount_sha256,
        ),
    )
    mount_pointer = _binding_pointer(
        mount_transaction_id,
        "_business_cutover_mount_v2",
        mount_sha256,
    )
    if has_v13_bindings:
        mount_evidence = BusinessCutoverMountEvidence(
            mount=mount_pointer,
            source_admission_sha256=source_admission_sha256,
            final_admission_sha256=final_admission_sha256,
            install=install_pointer,
            coverage=coverage_checkpoint,
            activation=activation_pointer,
            activation_binding=activation_evidence,
            audit_certificate_id=certificate_id,
            audit_certificate_sequence=1,
            audit=audit_pointer,
            audit_certificate_tail_sha256=certificate_tail,
            audit_completed_at=TEST_CUTOVER_COMMITTED_AT,
            activated_at=TEST_CUTOVER_COMMITTED_AT,
            target_schema_version=MIGRATIONS[-1].version,
            target_schema_fingerprint=schema_fingerprint,
        )
        _insert_fixture_phase_binding(connection, _issue_fixture_phase_binding(
            connection,
            stage=BusinessCutoverPhaseBindingStage.MOUNT,
            old_state=head_chain[3],
            new_state=head_chain[4],
            direct_evidence=mount_evidence,
            repository_epoch=repository_epoch,
            source_admission_revision=source_admission_revision,
            source_admission_sha256=source_admission_sha256,
            source_freeze_sha256=source_freeze_sha256,
            pending_admission_sha256=pending_admission_sha256,
            final_admission_sha256=final_admission_sha256,
            runtime_generation=runtime_generation,
            code_digest=code_digest,
            before_receipts=mount_before,
            after_receipts=mount_after,
        ))
    _advance_fixture_cutover_head(connection, head_chain[4])
    return cutover_id


def _zero_control_atomic_receipt_values(
    connection: Any,
    *,
    commit_id: str,
    committed_at: str,
    atomic_manifest_sha256: bytes,
    mounted_repository_revision: int,
    mounted_control_revision: int,
) -> dict[str, Any]:
    """Issue the real frozen inner/outer roots for a target-free baseline.

    Baseline commits are not ordinary change plans, but schema-v8 still gives
    each one an ``atomic_business_write_receipts`` row.  The old fixture used
    convenient test digests in that row, which made a healthy mounted database
    impossible to audit plan-free.  A zero-control/zero-target write is a legal
    shape in both frozen protocols, so build its exact evidence instead.
    """

    transaction_id = BUSINESS_TRANSACTION_PREFIX + commit_id
    parent = AtomicBusinessWriteGenericReceiptEvidence(
        transaction_id,
        BUSINESS_TRANSACTION_KIND,
        committed_at,
    )
    pipeline_intents_sha256 = _component_digest(
        b"ze.atomic-business-commit.pipelines.v1",
        (),
    )
    controls_sha256 = _atomic_digest(
        ATOMIC_BUSINESS_WRITE_CONTROLS_HASH_DOMAIN,
        (),
    )
    worker_row = connection.execute(
        "SELECT state_revision,sequence,previous_tail_sha256,tail_sha256,"
        "updated_at FROM worker_ledger_head WHERE singleton=1"
    ).fetchone()
    dispatch_row = connection.execute(
        "SELECT state_revision,published_sequence,"
        "published_repository_revision,published_ordinal,inflight_sequence,"
        "inflight_repository_revision,inflight_ordinal,previous_head_sha256,"
        "head_sha256,updated_at FROM outbox_dispatch_head WHERE singleton=1"
    ).fetchone()
    allocation_row = connection.execute(
        "SELECT state_revision,sequence,previous_tail_sha256,tail_sha256,"
        "updated_at FROM outbox_allocation_head WHERE singleton=1"
    ).fetchone()
    mount_row = connection.execute(
        """
        SELECT runtime.authority,runtime.authority_state,
               runtime.repository_epoch,runtime.cutover_id,
               runtime.source_admission_revision,runtime.runtime_generation,
               runtime.code_digest,mount.transaction_id,
               mount.manifest_sha256,mount.committed_at
        FROM repository_runtime_control AS runtime
        JOIN business_cutover_mount_receipts AS mount
          ON mount.cutover_id=runtime.cutover_id
        WHERE runtime.singleton=1
        """
    ).fetchone()
    if (
        worker_row is None
        or len(worker_row) != 5
        or dispatch_row is None
        or len(dispatch_row) != 10
        or allocation_row is None
        or len(allocation_row) != 5
        or mount_row is None
        or len(mount_row) != 10
    ):
        raise AssertionError("canonical zero-target atomic inputs are missing")

    before_worker = WorkerLedgerHead(worker_row[1], worker_row[3])
    inner_manifest = {
        "schema": ATOMIC_BUSINESS_WRITE_MANIFEST_SCHEMA,
        "protocol": ATOMIC_BUSINESS_WRITE_PROTOCOL,
        "status": ATOMIC_BUSINESS_WRITE_PLAN_STATUS,
        "commit": {
            "commit_id": commit_id,
            "committed_at": committed_at,
            "atomic_manifest_sha256": atomic_manifest_sha256.hex(),
            "pipeline_intents_sha256": pipeline_intents_sha256.hex(),
        },
        "worker_ledger": {
            "before": {
                "sequence": before_worker.sequence,
                "tail_sha256": before_worker.tail_sha256.hex(),
            },
            "after": {
                "sequence": before_worker.sequence,
                "tail_sha256": before_worker.tail_sha256.hex(),
            },
        },
        "controls": {
            "count": 0,
            "sha256": controls_sha256.hex(),
            "members": [],
        },
    }
    manifest_bytes = encode_payload_bounded(
        inner_manifest,
        MAX_ATOMIC_BUSINESS_WRITE_MANIFEST_BYTES,
        MAX_ATOMIC_BUSINESS_WRITE_MANIFEST_NODES,
    )
    manifest_sha256 = _atomic_manifest_digest(manifest_bytes)
    receipt_sha256 = _atomic_receipt_digest(
        commit_id=commit_id,
        receipt_parent=parent,
        atomic_manifest_sha256=atomic_manifest_sha256,
        pipeline_intents_sha256=pipeline_intents_sha256,
        controls_sha256=controls_sha256,
        control_count=0,
        before_worker_ledger=before_worker,
        after_worker_ledger=before_worker,
        manifest_sha256=manifest_sha256,
    )

    if dispatch_row[1] == 0:
        published = {
            "sequence": 0,
            "repository_revision": 0,
            "ordinal": -1,
        }
    else:
        published = {
            "sequence": dispatch_row[1],
            "repository_revision": dispatch_row[2],
            "ordinal": dispatch_row[3],
        }
    inflight = None
    if any(value is not None for value in dispatch_row[4:7]):
        if any(value is None for value in dispatch_row[4:7]):
            raise AssertionError("canonical dispatch head is partially populated")
        inflight = {
            "sequence": dispatch_row[4],
            "repository_revision": dispatch_row[5],
            "ordinal": dispatch_row[6],
        }
    allocation = {
        "state_revision": allocation_row[0],
        "sequence": allocation_row[1],
        "previous_tail_sha256": allocation_row[2].hex(),
        "tail_sha256": allocation_row[3].hex(),
        "updated_at": allocation_row[4],
    }
    targets_sha256 = _production_targets_sha256(_production_groups(()))
    production_manifest = {
        "schema": PRODUCTION_ATOMIC_WRITE_MANIFEST_SCHEMA,
        "protocol": PRODUCTION_ATOMIC_WRITE_PROTOCOL,
        "status": PRODUCTION_ATOMIC_WRITE_STATUS,
        "hash_domains": {
            "manifest": PRODUCTION_ATOMIC_MANIFEST_HASH_DOMAIN.decode("ascii"),
            "receipt": PRODUCTION_ATOMIC_RECEIPT_HASH_DOMAIN.decode("ascii"),
            "targets": PRODUCTION_ATOMIC_TARGETS_HASH_DOMAIN.decode("ascii"),
            "target_group": (
                PRODUCTION_ATOMIC_TARGET_GROUP_HASH_DOMAIN.decode("ascii")
            ),
            "target_member": (
                PRODUCTION_ATOMIC_TARGET_MEMBER_HASH_DOMAIN.decode("ascii")
            ),
            "genesis": PRODUCTION_WORKER_GENESIS_STATE_HASH_DOMAIN.decode(
                "ascii"
            ),
            "outbox_allocation_tail": (
                PRODUCTION_OUTBOX_ALLOCATION_TAIL_HASH_DOMAIN.decode("ascii")
            ),
            "control_member": (
                PRODUCTION_ATOMIC_CONTROL_MEMBER_HASH_DOMAIN.decode("ascii")
            ),
        },
        "pure_atomic": {
            "commit_id": commit_id,
            "transaction_id": transaction_id,
            "receipt_kind": BUSINESS_TRANSACTION_KIND,
            "committed_at": committed_at,
            "manifest_sha256": manifest_sha256.hex(),
            "receipt_sha256": receipt_sha256.hex(),
        },
        "mounted_authority": {
            "authority": mount_row[0],
            "authority_state": mount_row[1],
            "repository_epoch": mount_row[2],
            "cutover_id": mount_row[3],
            "source_admission_revision": mount_row[4],
            "repository_revision": mounted_repository_revision,
            "control_revision": mounted_control_revision,
            "runtime_generation": mount_row[5],
            "code_digest": mount_row[6].hex(),
            "target_schema_version": MIGRATIONS[-1].version,
            "target_schema_fingerprint": _expected_schema_fingerprint(),
            "mount_transaction_id": mount_row[7],
            "mount_manifest_sha256": mount_row[8].hex(),
            "mounted_at": mount_row[9],
        },
        "scheduler_heads": {
            "worker_ledger": {
                "state_revision": worker_row[0],
                "sequence": worker_row[1],
                "previous_tail_sha256": worker_row[2].hex(),
                "tail_sha256": worker_row[3].hex(),
                "updated_at": worker_row[4],
            },
            "outbox_dispatch": {
                "state_revision": dispatch_row[0],
                "published": published,
                "inflight": inflight,
                "previous_head_sha256": dispatch_row[7].hex(),
                "head_sha256": dispatch_row[8].hex(),
                "updated_at": dispatch_row[9],
            },
        },
        "outbox_allocation": {
            "count": 0,
            "before": allocation,
            "after": allocation,
        },
        "targets": {
            "group_order": [
                value.value for value in PRODUCTION_ATOMIC_TARGET_GROUP_ORDER
            ],
            "count": 0,
            "sha256": targets_sha256.hex(),
        },
    }
    production_manifest_bytes = encode_payload_bounded(
        production_manifest,
        MAX_PRODUCTION_ATOMIC_MANIFEST_BYTES,
        MAX_PRODUCTION_ATOMIC_MANIFEST_NODES,
    )
    production_manifest_sha256 = _production_domain_digest(
        PRODUCTION_ATOMIC_MANIFEST_HASH_DOMAIN,
        (production_manifest_bytes,),
    )
    production_receipt_sha256 = _production_domain_digest(
        PRODUCTION_ATOMIC_RECEIPT_HASH_DOMAIN,
        (
            commit_id.encode("ascii"),
            transaction_id.encode("utf-8"),
            BUSINESS_TRANSACTION_KIND.encode("utf-8"),
            committed_at.encode("ascii"),
            receipt_sha256,
            production_manifest_sha256,
            (0).to_bytes(8, "big"),
            targets_sha256,
        ),
    )
    return {
        "commit_id": commit_id,
        "transaction_id": transaction_id,
        "receipt_kind": BUSINESS_TRANSACTION_KIND,
        "committed_at": committed_at,
        "atomic_manifest_sha256": atomic_manifest_sha256,
        "pipeline_intents_sha256": pipeline_intents_sha256,
        "controls_sha256": controls_sha256,
        "before_worker_sequence": before_worker.sequence,
        "before_worker_tail_sha256": before_worker.tail_sha256,
        "after_worker_sequence": before_worker.sequence,
        "after_worker_tail_sha256": before_worker.tail_sha256,
        "manifest_bytes": manifest_bytes,
        "manifest_sha256": manifest_sha256,
        "receipt_sha256": receipt_sha256,
        "production_manifest_bytes": production_manifest_bytes,
        "production_manifest_sha256": production_manifest_sha256,
        "targets_sha256": targets_sha256,
        "production_receipt_sha256": production_receipt_sha256,
    }


def _insert_zero_control_atomic_receipt(
    connection: Any,
    *,
    commit_id: str,
    committed_at: str,
    atomic_manifest_sha256: bytes,
    mounted_repository_revision: int,
    mounted_control_revision: int,
) -> None:
    values = _zero_control_atomic_receipt_values(
        connection,
        commit_id=commit_id,
        committed_at=committed_at,
        atomic_manifest_sha256=atomic_manifest_sha256,
        mounted_repository_revision=mounted_repository_revision,
        mounted_control_revision=mounted_control_revision,
    )
    connection.execute(
        """
        INSERT INTO atomic_business_write_receipts(
            commit_id,transaction_id,receipt_kind,committed_at,
            atomic_manifest_sha256,pipeline_intents_sha256,controls_sha256,
            control_count,before_worker_sequence,before_worker_tail_sha256,
            after_worker_sequence,after_worker_tail_sha256,
            manifest_bytes,manifest_sha256,receipt_sha256,
            production_manifest_bytes,production_manifest_sha256,
            target_count,targets_sha256,production_receipt_sha256
        ) VALUES (
            :commit_id,:transaction_id,:receipt_kind,:committed_at,
            :atomic_manifest_sha256,:pipeline_intents_sha256,:controls_sha256,
            0,:before_worker_sequence,:before_worker_tail_sha256,
            :after_worker_sequence,:after_worker_tail_sha256,
            :manifest_bytes,:manifest_sha256,:receipt_sha256,
            :production_manifest_bytes,:production_manifest_sha256,
            0,:targets_sha256,:production_receipt_sha256
        )
        """,
        values,
    )


@dataclass(frozen=True)
class BusinessTestBaseline:
    """All typed facts needed to build reader/writer tests above the baseline."""

    database: SqliteDatabase = field(repr=False, compare=False)
    profile: BusinessRuntimeProfile
    snapshot: BusinessSnapshotSet = field(repr=False)
    asset_projection: AssetReferenceProjection = field(repr=False)
    project_id: str
    generation: int
    document_id: str
    document_version: int
    project_revision: int
    head_revision: int
    commit_id: str
    transaction_id: str
    business_root_sha256: bytes = field(repr=False)
    document_sha256: bytes = field(repr=False)
    operation_tail_sha256: bytes = field(repr=False)
    manifest_bytes: bytes = field(repr=False)
    manifest_sha256: bytes = field(repr=False)

    def before_business_head(self) -> BeforeBusinessHead:
        """Return the exact immutable head expected by the first change plan."""

        return BeforeBusinessHead(
            project_id=self.project_id,
            generation=self.generation,
            document_id=self.document_id,
            document_version=self.document_version,
            project_revision=self.project_revision,
            head_revision=self.head_revision,
            expected_last_commit_id=self.commit_id,
            business_root_sha256=self.business_root_sha256,
            operation_tail_hash=self.operation_tail_sha256.hex(),
        )


def _build_snapshot(
    project_id: str,
    document_id: str,
    *,
    generation: int = 1,
) -> BusinessSnapshotSet:
    document = Document(
        id=document_id,
        name="business baseline",
        schema_version="1.0.0",
        collaboration_mode="with_save",
        save_policy="auto_commit",
        schema={},
    )
    document._v = 1
    document._created_at = 1
    document._updated_at = 1
    return capture_business_snapshot(project_id, generation, document).snapshot


def _build_manifest(
    *,
    snapshot: BusinessSnapshotSet,
    projection: AssetReferenceProjection,
    commit_id: str,
    repository_epoch: str,
    cutover_id: str,
    source_admission_revision: int,
    runtime_generation: str,
    code_digest: bytes,
    authority_state: BusinessAuthorityState,
    repository_revision: int,
    state_revision: int,
) -> bytes:
    value = {
        "schema": TEST_BASELINE_MANIFEST_SCHEMA,
        "protocol": TEST_BASELINE_MANIFEST_PROTOCOL,
        "status": "committed",
        "authority": {
            "authority": "sqlite",
            "authority_state": authority_state.value,
            "epoch": repository_epoch,
            "cutover_id": cutover_id,
            "source_admission_revision": source_admission_revision,
            "repository_revision": repository_revision,
            "state_revision": state_revision,
            "runtime_generation": runtime_generation,
            "code_digest": code_digest.hex(),
        },
        "commit": {
            "commit_id": commit_id,
            "commit_kind": "baseline",
            "project_id": snapshot.project.project_id,
            "generation": snapshot.project.generation,
            "document_id": snapshot.document.document_id,
            "document_version": snapshot.document.document_version,
            "save_policy": snapshot.document.save_policy or "legacy",
        },
        "after": {
            "business_root_sha256": snapshot.project.snapshot_sha256.hex(),
            "document_sha256": snapshot.document.snapshot_sha256.hex(),
            "entity_count": len(snapshot.entities),
            "operation_tail_sha256": TEST_BASELINE_OPERATION_TAIL.hex(),
        },
        "asset_references": {
            "protocol": projection.protocol,
            "reference_count": projection.reference_count,
            "unique_reference_count": projection.unique_reference_count,
            "projection_sha256": projection.projection_sha256.hex(),
        },
    }
    return encode_payload_bounded(
        value,
        MAX_ATOMIC_MANIFEST_BYTES,
        MAX_ATOMIC_MANIFEST_NODES,
    )


def create_active_business_database(
    path: Any,
    *,
    authority_state: BusinessAuthorityState = BusinessAuthorityState.ACTIVE,
    project_id: str = "project-v6",
    document_id: str = "document-v6",
    commit_id: str = "1" * 32,
    repository_epoch: str = "e" * 64,
    cutover_id: Optional[str] = None,
    source_admission_revision: int = 1,
    runtime_generation: str = "runtime-v6-test",
    code_digest: Optional[bytes] = None,
    _historical_v7: bool = False,
) -> BusinessTestBaseline:
    """Create a fresh, private current-schema active baseline for core tests."""

    if type(authority_state) is not BusinessAuthorityState:
        raise TypeError("authority_state must be a BusinessAuthorityState")
    if type(_historical_v7) is not bool:
        raise TypeError("_historical_v7 must be an exact bool")
    if (
        _historical_v7
        and MIGRATIONS[-1].version != HISTORICAL_V7_SCHEMA_VERSION
    ):
        raise AssertionError(
            "historical v7 fixture requires the test-scoped v7 migration registry"
        )
    digest = (
        hashlib.sha256(b"ze.business-runtime.test.v1").digest()
        if code_digest is None
        else code_digest
    )
    snapshot = verify_business_snapshot(_build_snapshot(project_id, document_id))
    projection = project_asset_references(snapshot)
    transaction_id = BUSINESS_TRANSACTION_PREFIX + commit_id
    manifest_bytes = b""
    manifest_sha256 = b""

    # macOS exposes /tmp through a /var symlink; resolve only inside this
    # test-only constructor so ordinary kernel callers retain strict path
    # rejection semantics.
    database = SqliteDatabase(Path(path).resolve())
    database.initialize()
    connection, _filesystem = database._open_unvalidated()
    committed = False
    try:
        connection.execute("BEGIN IMMEDIATE")
        # Prove this is the exact fresh, unmounted state before the test-only
        # authority installation.  The generic validator remains unchanged.
        database._validate_initialized(connection)
        connection.execute(
            "UPDATE kernel_repository SET authority = 'sqlite' WHERE singleton = 1"
        )
        if _historical_v7:
            active_cutover_id = "f" * 64 if cutover_id is None else cutover_id
            baseline_repository_revision = 1
            baseline_state_revision = 1
            final_runtime_state_revision = 2
            manifest_authority_state = authority_state
        else:
            active_cutover_id = _install_mounted_test_authority(
                connection,
                repository_epoch=repository_epoch,
                source_admission_revision=source_admission_revision,
                runtime_generation=runtime_generation,
                code_digest=digest,
                authority_state=BusinessAuthorityState.ACTIVE,
            )
            if cutover_id is not None and cutover_id != active_cutover_id:
                raise ValueError(
                    "cutover_id is content-addressed; omit it or pass the "
                    "derived V5 repository proof identity"
                )
            baseline_repository_revision = (
                2
                if MIGRATIONS[-1].version
                >= INSTALL_CLOSURE_MIGRATION.version
                else 1
            )
            baseline_state_revision = 2
            final_runtime_state_revision = 2
            manifest_authority_state = BusinessAuthorityState.ACTIVE
        if _historical_v7:
            connection.execute(
                """
                INSERT INTO repository_runtime_control(
                    authority, authority_state, repository_epoch, cutover_id,
                    source_admission_revision, repository_revision,
                    state_revision, runtime_generation, code_digest,
                    schema_version, updated_at
                ) VALUES ('sqlite', ?, ?, ?, ?, 1, 1, ?, ?, 6, ?)
                """,
                (
                    authority_state.value,
                    repository_epoch,
                    active_cutover_id,
                    source_admission_revision,
                    runtime_generation,
                    digest,
                    TEST_BASELINE_COMMITTED_AT,
                ),
            )
        manifest_bytes = _build_manifest(
            snapshot=snapshot,
            projection=projection,
            commit_id=commit_id,
            repository_epoch=repository_epoch,
            cutover_id=active_cutover_id,
            source_admission_revision=source_admission_revision,
            runtime_generation=runtime_generation,
            code_digest=digest,
            authority_state=manifest_authority_state,
            repository_revision=baseline_repository_revision,
            state_revision=baseline_state_revision,
        )
        manifest_sha256 = digest_business_baseline_manifest(manifest_bytes)
        project = snapshot.project
        document = snapshot.document
        connection.execute(
            """
            INSERT INTO projects(
                project_id, generation, codec_id, codec_version,
                snapshot_format_version, snapshot_bytes, snapshot_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project.project_id,
                project.generation,
                project.codec_id,
                project.codec_version,
                project.snapshot_format_version,
                project.snapshot_bytes,
                project.snapshot_sha256,
            ),
        )
        connection.execute(
            """
            INSERT INTO documents(
                project_id, document_id, document_version, schema_version,
                collaboration_mode, save_policy, codec_id, codec_version,
                snapshot_format_version, snapshot_bytes, snapshot_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                document.project_id,
                document.document_id,
                document.document_version,
                document.schema_version,
                document.collaboration_mode,
                document.save_policy,
                document.codec_id,
                document.codec_version,
                document.snapshot_format_version,
                document.snapshot_bytes,
                document.snapshot_sha256,
            ),
        )
        for entity in snapshot.entities:
            connection.execute(
                """
                INSERT INTO entities(
                    project_id, document_id, entity_type, entity_id,
                    entity_version, deleted, codec_id, codec_version,
                    snapshot_format_version, snapshot_bytes, snapshot_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entity.project_id,
                    entity.document_id,
                    entity.entity_type,
                    entity.entity_id,
                    entity.entity_version,
                    entity.deleted,
                    entity.codec_id,
                    entity.codec_version,
                    entity.snapshot_format_version,
                    entity.snapshot_bytes,
                    entity.snapshot_sha256,
                ),
            )
        connection.execute(
            """
            INSERT INTO transaction_receipts(transaction_id, kind, committed_at)
            VALUES (?, ?, ?)
            """,
            (
                transaction_id,
                BUSINESS_TRANSACTION_KIND,
                TEST_BASELINE_COMMITTED_AT,
            ),
        )
        connection.execute(
            """
            INSERT INTO business_commits(
                commit_id, transaction_id, receipt_kind, project_id,
                generation, repository_revision, project_revision,
                head_revision, repository_epoch, cutover_id,
                source_admission_revision, document_id, document_version,
                previous_commit_id, commit_kind, save_policy, trace_id,
                operation_count, operation_target_count, outbox_event_count,
                durable_action_count, pipeline_intent_count,
                before_root_sha256, after_root_sha256,
                previous_operation_tail_sha256, operation_tail_sha256,
                asset_projection_sha256, asset_reference_count, entity_count,
                manifest_bytes, manifest_sha256, committed_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, 1, 1, ?, ?, ?, ?, ?, NULL, 'baseline', ?, ?,
                0, 0, 0, 0, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                commit_id,
                transaction_id,
                BUSINESS_TRANSACTION_KIND,
                project.project_id,
                project.generation,
                baseline_repository_revision,
                repository_epoch,
                active_cutover_id,
                source_admission_revision,
                document.document_id,
                document.document_version,
                document.save_policy or "legacy",
                "baseline-test",
                TEST_BASELINE_BEFORE_ROOT,
                project.snapshot_sha256,
                TEST_BASELINE_OPERATION_TAIL,
                TEST_BASELINE_OPERATION_TAIL,
                projection.projection_sha256,
                projection.reference_count,
                len(snapshot.entities),
                manifest_bytes,
                manifest_sha256,
                TEST_BASELINE_COMMITTED_AT,
            ),
        )
        connection.execute(
            """
            INSERT INTO repository_projects(
                project_id, generation, project_revision, status,
                current_snapshot_sha256, created_at, updated_at
            ) VALUES (?, ?, 1, 'live', ?, ?, ?)
            """,
            (
                project.project_id,
                project.generation,
                project.snapshot_sha256,
                TEST_BASELINE_COMMITTED_AT,
                TEST_BASELINE_COMMITTED_AT,
            ),
        )
        if not _historical_v7:
            _insert_zero_control_atomic_receipt(
                connection,
                commit_id=commit_id,
                committed_at=TEST_BASELINE_COMMITTED_AT,
                atomic_manifest_sha256=manifest_sha256,
                mounted_repository_revision=1,
                mounted_control_revision=2,
            )
        connection.execute(
            """
            INSERT INTO document_commit_heads(
                project_id, generation, document_id, document_version,
                commit_id, head_revision, status, current_snapshot_sha256,
                current_project_snapshot_sha256, business_root_sha256,
                operation_tail_sha256, asset_projection_sha256,
                asset_reference_count, entity_count, updated_at
            ) VALUES (?, ?, ?, ?, ?, 1, 'live', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project.project_id,
                project.generation,
                document.document_id,
                document.document_version,
                commit_id,
                document.snapshot_sha256,
                project.snapshot_sha256,
                project.snapshot_sha256,
                TEST_BASELINE_OPERATION_TAIL,
                projection.projection_sha256,
                projection.reference_count,
                len(snapshot.entities),
                TEST_BASELINE_COMMITTED_AT,
            ),
        )
        if _historical_v7 or baseline_repository_revision > 1:
            connection.execute(
                """
                UPDATE repository_runtime_control
                SET repository_revision=?,state_revision=?,updated_at=?
                WHERE singleton=1 AND repository_revision=? AND
                      state_revision=?
                """,
                (
                    baseline_repository_revision,
                    final_runtime_state_revision,
                    TEST_BASELINE_COMMITTED_AT,
                    1 if not _historical_v7 else baseline_repository_revision,
                    1 if _historical_v7 else baseline_state_revision,
                ),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise AssertionError("test baseline runtime CAS failed")
        if (
            not _historical_v7
            and authority_state is not BusinessAuthorityState.ACTIVE
        ):
            connection.execute(
                "UPDATE repository_runtime_control SET authority_state=?,"
                "state_revision=state_revision+1 "
                "WHERE singleton=1 AND authority_state='active' AND "
                "repository_revision=? AND state_revision=?",
                (
                    authority_state.value,
                    baseline_repository_revision,
                    final_runtime_state_revision,
                ),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise AssertionError("test baseline authority-state CAS failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise AssertionError("test baseline contains a foreign-key violation")
        connection.commit()
        committed = True
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()
    if committed:
        database._finalize_committed_storage()

    profile = inspect_business_runtime(
        database,
        access=BusinessRuntimeAccess.READER,
    )
    return BusinessTestBaseline(
        database=database,
        profile=profile,
        snapshot=snapshot,
        asset_projection=projection,
        project_id=project.project_id,
        generation=project.generation,
        document_id=document.document_id,
        document_version=document.document_version,
        project_revision=1,
        head_revision=1,
        commit_id=commit_id,
        transaction_id=transaction_id,
        business_root_sha256=project.snapshot_sha256,
        document_sha256=document.snapshot_sha256,
        operation_tail_sha256=TEST_BASELINE_OPERATION_TAIL,
        manifest_bytes=manifest_bytes,
        manifest_sha256=manifest_sha256,
    )


def add_active_business_test_project(
    database: SqliteDatabase,
    *,
    project_id: str = "project-v7-second",
    document_id: str = "document-v7-second",
    commit_id: str = "2" * 32,
    generation: int = 1,
    _historical_v7: bool = False,
) -> BusinessTestBaseline:
    """Append one legal baseline commit for a second test-only project.

    This is the only supported way for reader/writer tests to model unrelated
    repository progress.  Merely incrementing ``repository_runtime_control``
    would violate schema-v7's exact ``global=max(commits)`` closure and would
    teach tests to rely on a state production validation correctly rejects.
    """

    if type(database) is not SqliteDatabase:
        raise TypeError("database must be an exact SqliteDatabase")
    if type(_historical_v7) is not bool:
        raise TypeError("_historical_v7 must be an exact bool")
    if (
        _historical_v7
        and MIGRATIONS[-1].version != HISTORICAL_V7_SCHEMA_VERSION
    ):
        raise AssertionError(
            "historical v7 fixture requires the test-scoped v7 migration registry"
        )
    if type(generation) is not int or isinstance(generation, bool) or generation < 1:
        raise TypeError("generation must be a positive exact integer")
    profile = inspect_business_runtime(
        database,
        access=BusinessRuntimeAccess.WRITER,
    )
    if profile.authority_state is not BusinessAuthorityState.ACTIVE:
        raise TypeError("test project requires an active business runtime")
    repository_revision = profile.repository_revision + 1
    snapshot = verify_business_snapshot(
        _build_snapshot(project_id, document_id, generation=generation)
    )
    projection = project_asset_references(snapshot)
    transaction_id = BUSINESS_TRANSACTION_PREFIX + commit_id
    manifest_bytes = _build_manifest(
        snapshot=snapshot,
        projection=projection,
        commit_id=commit_id,
        repository_epoch=profile.repository_epoch,
        cutover_id=profile.cutover_id,
        source_admission_revision=profile.source_admission_revision,
        runtime_generation=profile.runtime_generation,
        code_digest=profile.code_digest,
        authority_state=profile.authority_state,
        repository_revision=repository_revision,
        state_revision=profile.state_revision,
    )
    manifest_sha256 = digest_business_baseline_manifest(manifest_bytes)
    project = snapshot.project
    document = snapshot.document
    connection, _filesystem = database._open_unvalidated()
    committed = False
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = _validate_business_runtime_connection(
            database,
            connection,
            access=BusinessRuntimeAccess.WRITER,
        )
        if current != profile:
            raise AssertionError("test runtime advanced before second baseline transaction")
        runtime_time_row = connection.execute(
            "SELECT updated_at FROM repository_runtime_control WHERE singleton=1"
        ).fetchone()
        if runtime_time_row is None:
            raise AssertionError("test runtime row disappeared before baseline append")
        committed_at = max(
            TEST_SECOND_BASELINE_COMMITTED_AT,
            str(runtime_time_row[0]),
        )
        connection.execute(
            """
            INSERT INTO projects(
                project_id,generation,codec_id,codec_version,
                snapshot_format_version,snapshot_bytes,snapshot_sha256
            ) VALUES (?,?,?,?,?,?,?)
            """,
            (
                project.project_id,
                project.generation,
                project.codec_id,
                project.codec_version,
                project.snapshot_format_version,
                project.snapshot_bytes,
                project.snapshot_sha256,
            ),
        )
        connection.execute(
            """
            INSERT INTO documents(
                project_id,document_id,document_version,schema_version,
                collaboration_mode,save_policy,codec_id,codec_version,
                snapshot_format_version,snapshot_bytes,snapshot_sha256
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                document.project_id,
                document.document_id,
                document.document_version,
                document.schema_version,
                document.collaboration_mode,
                document.save_policy,
                document.codec_id,
                document.codec_version,
                document.snapshot_format_version,
                document.snapshot_bytes,
                document.snapshot_sha256,
            ),
        )
        for entity in snapshot.entities:
            connection.execute(
                """
                INSERT INTO entities(
                    project_id,document_id,entity_type,entity_id,
                    entity_version,deleted,codec_id,codec_version,
                    snapshot_format_version,snapshot_bytes,snapshot_sha256
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    entity.project_id,
                    entity.document_id,
                    entity.entity_type,
                    entity.entity_id,
                    entity.entity_version,
                    entity.deleted,
                    entity.codec_id,
                    entity.codec_version,
                    entity.snapshot_format_version,
                    entity.snapshot_bytes,
                    entity.snapshot_sha256,
                ),
            )
        connection.execute(
            """
            INSERT INTO transaction_receipts(transaction_id,kind,committed_at)
            VALUES (?,?,?)
            """,
            (transaction_id, BUSINESS_TRANSACTION_KIND, committed_at),
        )
        connection.execute(
            """
            INSERT INTO business_commits(
                commit_id,transaction_id,receipt_kind,project_id,generation,
                repository_revision,project_revision,head_revision,
                repository_epoch,cutover_id,source_admission_revision,
                document_id,document_version,previous_commit_id,commit_kind,
                save_policy,trace_id,operation_count,operation_target_count,
                outbox_event_count,durable_action_count,pipeline_intent_count,
                before_root_sha256,after_root_sha256,
                previous_operation_tail_sha256,operation_tail_sha256,
                asset_projection_sha256,asset_reference_count,entity_count,
                manifest_bytes,manifest_sha256,committed_at
            ) VALUES (
                ?,?,?,?, ?,?,1,1, ?,?,?, ?,?,NULL,'baseline', ?,?,
                0,0,0,0,0, ?,?,?,?, ?,?,?, ?,?,?
            )
            """,
            (
                commit_id,
                transaction_id,
                BUSINESS_TRANSACTION_KIND,
                project.project_id,
                project.generation,
                repository_revision,
                profile.repository_epoch,
                profile.cutover_id,
                profile.source_admission_revision,
                document.document_id,
                document.document_version,
                document.save_policy or "legacy",
                "baseline-test-second",
                TEST_BASELINE_BEFORE_ROOT,
                project.snapshot_sha256,
                TEST_BASELINE_OPERATION_TAIL,
                TEST_BASELINE_OPERATION_TAIL,
                projection.projection_sha256,
                projection.reference_count,
                len(snapshot.entities),
                manifest_bytes,
                manifest_sha256,
                committed_at,
            ),
        )
        connection.execute(
            """
            INSERT INTO repository_projects(
                project_id,generation,project_revision,status,
                current_snapshot_sha256,created_at,updated_at
            ) VALUES (?,?,1,'live',?,?,?)
            """,
            (
                project.project_id,
                project.generation,
                project.snapshot_sha256,
                committed_at,
                committed_at,
            ),
        )
        if not _historical_v7:
            _insert_zero_control_atomic_receipt(
                connection,
                commit_id=commit_id,
                committed_at=committed_at,
                atomic_manifest_sha256=manifest_sha256,
                mounted_repository_revision=profile.repository_revision,
                mounted_control_revision=profile.state_revision,
            )
        connection.execute(
            """
            INSERT INTO document_commit_heads(
                project_id,generation,document_id,document_version,commit_id,
                head_revision,status,current_snapshot_sha256,
                current_project_snapshot_sha256,business_root_sha256,
                operation_tail_sha256,asset_projection_sha256,
                asset_reference_count,entity_count,updated_at
            ) VALUES (?,?,?,?,?,1,'live',?,?,?,?,?,?,?,?)
            """,
            (
                project.project_id,
                project.generation,
                document.document_id,
                document.document_version,
                commit_id,
                document.snapshot_sha256,
                project.snapshot_sha256,
                project.snapshot_sha256,
                TEST_BASELINE_OPERATION_TAIL,
                projection.projection_sha256,
                projection.reference_count,
                len(snapshot.entities),
                committed_at,
            ),
        )
        updated = connection.execute(
            """
            UPDATE repository_runtime_control
            SET repository_revision=?,state_revision=state_revision,updated_at=?
            WHERE singleton=1 AND repository_revision=? AND state_revision=?
            """,
            (
                repository_revision,
                committed_at,
                profile.repository_revision,
                profile.state_revision,
            ),
        )
        if updated.rowcount != 1:
            raise AssertionError("test runtime finalization CAS did not advance once")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise AssertionError("second test baseline contains a foreign-key violation")
        connection.commit()
        committed = True
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()
    if committed:
        database._finalize_committed_storage()
    current = inspect_business_runtime(database, access=BusinessRuntimeAccess.READER)
    return BusinessTestBaseline(
        database=database,
        profile=current,
        snapshot=snapshot,
        asset_projection=projection,
        project_id=project.project_id,
        generation=project.generation,
        document_id=document.document_id,
        document_version=document.document_version,
        project_revision=1,
        head_revision=1,
        commit_id=commit_id,
        transaction_id=transaction_id,
        business_root_sha256=project.snapshot_sha256,
        document_sha256=document.snapshot_sha256,
        operation_tail_sha256=TEST_BASELINE_OPERATION_TAIL,
        manifest_bytes=manifest_bytes,
        manifest_sha256=manifest_sha256,
    )


def create_historical_v7_active_business_database(
    path: Any,
    *,
    authority_state: BusinessAuthorityState = BusinessAuthorityState.ACTIVE,
    project_id: str = "project-v6",
    document_id: str = "document-v6",
    commit_id: str = "1" * 32,
    repository_epoch: str = "e" * 64,
    cutover_id: str = "f" * 64,
    source_admission_revision: int = 1,
    runtime_generation: str = "runtime-v6-test",
    code_digest: Optional[bytes] = None,
) -> BusinessTestBaseline:
    """Build the frozen v7 baseline used only by the retired v7 writer/reader.

    The caller must hold a pytest-scoped ``MIGRATIONS[:7]`` registry patch for
    the whole operation.  That makes initialization, every transaction-time
    validation, reconcile, and full audit observe one coherent historical
    binary.  No schema-v8 mount or production receipt is fabricated.
    """

    return create_active_business_database(
        path,
        authority_state=authority_state,
        project_id=project_id,
        document_id=document_id,
        commit_id=commit_id,
        repository_epoch=repository_epoch,
        cutover_id=cutover_id,
        source_admission_revision=source_admission_revision,
        runtime_generation=runtime_generation,
        code_digest=code_digest,
        _historical_v7=True,
    )


def add_historical_v7_active_business_test_project(
    database: SqliteDatabase,
    *,
    project_id: str = "project-v7-second",
    document_id: str = "document-v7-second",
    commit_id: str = "2" * 32,
    generation: int = 1,
) -> BusinessTestBaseline:
    """Append a second closed project through the frozen v7 test substrate."""

    return add_active_business_test_project(
        database,
        project_id=project_id,
        document_id=document_id,
        commit_id=commit_id,
        generation=generation,
        _historical_v7=True,
    )


__all__ = ()
