"""Test-only constructor for genuinely canonical mounted authority fixtures.

The shared business fixture already persists exact source, install and asset-
coverage evidence.  Current V13 now creates canonical later-phase evidence and
all four immutable phase bindings at the source, so this module only verifies
that result.  Historical V8--V12 fixtures retain the old test-only maintenance
upgrade, restoring every temporarily removed trigger from its original
``sqlite_master`` SQL before returning the database.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
import sys
from typing import Any, Optional


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

import sqlite_business_test_support as support
from business_codec import decode_payload_bounded, encode_payload_bounded
from business_atomic_write_integrity import _issue_atomic_business_write_plan
from business_transaction_contract import PipelineIntentKind
from business_cutover_contract import (
    BUSINESS_CUTOVER_RUNTIME_CONTROL_SCHEMA_VERSION,
    MAX_BUSINESS_CUTOVER_MANIFEST_BYTES,
    MAX_BUSINESS_CUTOVER_MANIFEST_NODES,
    BusinessCutoverAdmission,
    BusinessCutoverRuntimeBinding,
    digest_business_cutover_admission,
)
from business_cutover_mount_contract import (
    BUSINESS_CUTOVER_MOUNT_TRANSACTION_KIND,
    BusinessCutoverMountActivationBinding,
    BusinessCutoverMountCoverageBinding,
    BusinessCutoverMountEvidence,
    BusinessCutoverMountGenericReceiptEvidence,
    BusinessCutoverMountInstallBinding,
    BusinessCutoverMountRuntimeIdentity,
)
from business_cutover_mount_integrity import (
    _build_manifest as _build_mount_manifest,
    _manifest_digest as _mount_manifest_digest,
    _require_business_cutover_mount_evidence,
)
from business_cutover_head_integrity import (
    _require_business_cutover_head_chain,
    issue_business_cutover_head_chain_from_mount,
)
from business_cutover_activation_contract import (
    BusinessCutoverActivationEvidence,
    BusinessCutoverActivationGenericReceiptEvidence,
)
from business_cutover_activation_integrity import (
    _build_manifest as _build_activation_manifest,
    _manifest_digest as _activation_manifest_digest,
    _require_business_cutover_activation_evidence,
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
from sqlite_kernel import _expected_schema_fingerprint
from sqlite_production_authority import (
    _read_production_atomic_planning_snapshot,
)
from production_atomic_write_contract import (
    PRODUCTION_OUTBOX_IDEMPOTENCY_POLICY,
    PRODUCTION_OUTBOX_MAX_ATTEMPTS,
    PRODUCTION_OUTBOX_REPLAY_POLICY,
    ProductionWorkerTarget,
)
from production_atomic_write_integrity import (
    _issue_production_atomic_write_plan,
    _production_worker_genesis_state_sha256,
)
from sqlite_schema import MIGRATIONS, _expected_migration_ledger_sha256
from worker_execution_contract import (
    PipelineBusinessControlIntentBinding,
    PipelineBusinessControlOperation,
    PipelineBusinessControlRequest,
    WorkerExecutionStatus,
    WorkerIdempotencyPolicy,
    WorkerOutboxOrder,
    WorkerReplayPolicy,
    WorkerTarget,
    WorkerTargetKind,
)
from worker_execution_integrity import _issue_pipeline_business_control_plan


def _trigger_sql(connection: sqlite3.Connection, names: tuple[str, ...]) -> dict:
    # PROJECT-13 reserves the member name ``join`` for the historical cutover
    # join capability.  Build this test-only SQL placeholder list without an
    # unrelated ``str.join`` reference so that the ownership gate stays exact.
    placeholders = ("?," * len(names)).rstrip(",")
    rows = connection.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name IN ("
        + placeholders
        + ")",
        names,
    ).fetchall()
    result = {name: sql for name, sql in rows}
    if set(result) != set(names) or any(type(sql) is not str for sql in result.values()):
        raise AssertionError("canonical fixture trigger set changed")
    return result


def _upgrade_to_canonical_mount(
    baseline: Any,
    *,
    repository_epoch: str,
    cutover_id: str,
    source_revision: int,
    runtime_generation: str,
    code_digest: bytes,
) -> None:
    connection = sqlite3.connect(baseline.database.path)
    connection.row_factory = sqlite3.Row
    if support._has_complete_v13_binding_schema(connection):
        connection.close()
        _read_production_atomic_planning_snapshot(baseline.database)
        return
    trigger_names = (
        "atomic_business_write_receipts_no_update",
        "business_cutover_install_receipts_no_update",
        "business_asset_coverage_receipts_no_update",
        "repository_audit_certificates_no_update",
        "business_cutover_activation_receipts_no_update",
        "business_cutover_mount_receipts_no_update",
        "repository_audit_head_advance_guard",
        "repository_audit_head_cas_update_guard",
        "repository_audit_head_sequence_guard",
        "business_cutover_head_cas_update_guard",
        "business_cutover_head_phase_guard",
        "business_cutover_head_update_binding_guard",
    )
    v12_trigger_names = (
        "business_cutover_install_closures_no_update",
    )
    v12_existing = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name IN ("
            + ("?," * len(v12_trigger_names)).rstrip(",")
            + ")",
            v12_trigger_names,
        ).fetchall()
    }
    if v12_existing and v12_existing != set(v12_trigger_names):
        raise AssertionError("canonical fixture V12 trigger set is partial")
    if v12_existing:
        trigger_names = (*trigger_names, *v12_trigger_names)
    originals = _trigger_sql(connection, trigger_names)
    try:
        connection.execute("PRAGMA defer_foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        for name in trigger_names:
            connection.execute('DROP TRIGGER "' + name + '"')

        install_row = connection.execute(
            "SELECT * FROM business_cutover_install_receipts WHERE cutover_id=?",
            (cutover_id,),
        ).fetchone()
        coverage_row = connection.execute(
            "SELECT * FROM business_asset_coverage_receipts WHERE cutover_id=?",
            (cutover_id,),
        ).fetchone()
        audit_row = connection.execute(
            "SELECT * FROM repository_audit_certificates WHERE cutover_id=?",
            (cutover_id,),
        ).fetchone()
        if any(row is None for row in (install_row, coverage_row, audit_row)):
            raise AssertionError("canonical fixture authority rows missing")

        source_admission = BusinessCutoverAdmission(
            "legacy", repository_epoch, source_revision, ""
        )
        pending_admission = BusinessCutoverAdmission(
            "cutover_pending", repository_epoch, source_revision + 1, cutover_id
        )
        final_admission = BusinessCutoverAdmission(
            "sqlite", repository_epoch, source_revision + 2, cutover_id
        )
        source_sha256 = digest_business_cutover_admission(source_admission)
        pending_sha256 = digest_business_cutover_admission(pending_admission)
        final_sha256 = digest_business_cutover_admission(final_admission)

        # The shared V12 fixture now persists the real pure install and fresh
        # coverage plans.  Production support must preserve those bytes; its
        # upgrade owns only the later audit/activation/mount protocols.
        install_manifest = bytes(install_row["manifest_bytes"])
        install_sha256 = bytes(install_row["manifest_sha256"])
        coverage_manifest = bytes(coverage_row["manifest_bytes"])
        coverage_manifest_sha256 = bytes(coverage_row["manifest_sha256"])

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
            source_admission_revision=source_revision,
            repository_revision=1,
            state_revision=1,
            runtime_generation=runtime_generation,
            code_digest=code_digest,
            schema_version=BUSINESS_CUTOVER_RUNTIME_CONTROL_SCHEMA_VERSION,
            updated_at=audit_row["committed_at"],
        )
        audit_coverage = RepositoryAuditCoverage(
            covered_business_revision=1,
            worker_ledger_sequence=0,
            worker_ledger_tail_sha256=EMPTY_WORKER_LEDGER_TAIL_SHA256,
            receipt_namespace_count=audit_row["receipt_count"],
            receipt_namespace_sha256=audit_row["receipt_root_sha256"],
            cutover_install_manifest_sha256=install_sha256,
        )
        findings = RepositoryAuditFindings(
            page_integrity="ok",
            foreign_key_violation_count=0,
            namespace_violation_count=0,
            started_at=audit_row["committed_at"],
            completed_at=audit_row["committed_at"],
        )
        audit_observed = RepositoryAuditObservedInput(
            certificate_id=audit_row["certificate_id"],
            purpose=RepositoryAuditPurpose.INSTALL,
            runtime=audit_runtime,
            schema=schema,
            coverage=audit_coverage,
            findings=findings,
        )
        audit_receipt = RepositoryAuditGenericReceiptEvidence(
            audit_row["transaction_id"],
            audit_row["receipt_kind"],
            audit_row["committed_at"],
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
        audit_tail = _certificate_tail_digest(
            1,
            EMPTY_REPOSITORY_AUDIT_CERTIFICATE_TAIL_SHA256,
            audit_manifest_sha256,
        )
        audit_evidence = _require_repository_audit_certificate_evidence(
            RepositoryAuditCertificateEvidence(
                certificate_id=audit_observed.certificate_id,
                purpose=audit_observed.purpose,
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
                certificate_tail_sha256=audit_tail,
            )
        )

        before_runtime = BusinessCutoverRuntimeBinding(
            authority="sqlite",
            authority_state="read_only",
            repository_epoch=repository_epoch,
            cutover_id=cutover_id,
            source_admission_revision=source_revision,
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
            source_admission_revision=source_revision,
            repository_revision=1,
            state_revision=2,
            runtime_generation=runtime_generation,
            code_digest=code_digest,
            schema_version=BUSINESS_CUTOVER_RUNTIME_CONTROL_SCHEMA_VERSION,
        )
        activation_transaction_id = "_business-cutover-activation:" + cutover_id
        activation_receipt = BusinessCutoverActivationGenericReceiptEvidence(
            activation_transaction_id,
            "_business_cutover_activation_v2",
            audit_row["committed_at"],
        )
        activation_manifest = _build_activation_manifest(
            cutover_id=cutover_id,
            receipt=activation_receipt,
            final_admission=final_admission,
            final_admission_sha256=final_sha256,
            before_runtime=before_runtime,
            after_runtime=after_runtime,
            install_transaction_id=install_row["transaction_id"],
            install_receipt_kind=install_row["receipt_kind"],
            install_manifest_sha256=install_sha256,
            audit_certificate_id=audit_evidence.certificate_id,
            audit_certificate_sequence=audit_evidence.sequence,
            audit_purpose=audit_evidence.purpose,
            audit_transaction_id=audit_evidence.transaction_id,
            audit_receipt_kind=audit_evidence.receipt_kind,
            audit_manifest_sha256=audit_evidence.manifest_sha256,
            audit_certificate_tail_sha256=audit_evidence.certificate_tail_sha256,
            audit_completed_at=findings.completed_at,
            expected_migration_ledger_count=schema.migration_ledger_count,
            expected_migration_ledger_sha256=schema.migration_ledger_sha256,
            actual_schema=schema,
            activated_at=audit_row["committed_at"],
        )
        activation_sha256 = _activation_manifest_digest(activation_manifest)
        activation_evidence = _require_business_cutover_activation_evidence(
            BusinessCutoverActivationEvidence(
                cutover_id=cutover_id,
                receipt_parent=activation_receipt,
                final_admission=final_admission,
                final_admission_sha256=final_sha256,
                before_runtime=before_runtime,
                after_runtime=after_runtime,
                install_transaction_id=install_row["transaction_id"],
                install_receipt_kind=install_row["receipt_kind"],
                install_manifest_sha256=install_sha256,
                audit_certificate_id=audit_evidence.certificate_id,
                audit_certificate_sequence=audit_evidence.sequence,
                audit_transaction_id=audit_evidence.transaction_id,
                audit_receipt_kind=audit_evidence.receipt_kind,
                audit_manifest_sha256=audit_evidence.manifest_sha256,
                audit_certificate_tail_sha256=audit_evidence.certificate_tail_sha256,
                audit_completed_at=findings.completed_at,
                expected_migration_ledger_count=schema.migration_ledger_count,
                expected_migration_ledger_sha256=schema.migration_ledger_sha256,
                actual_schema=schema,
                activated_at=audit_row["committed_at"],
                manifest_bytes=activation_manifest,
                manifest_sha256=activation_sha256,
            )
        )

        mount_transaction_id = "_business-cutover-mount:" + cutover_id
        mount_receipt = BusinessCutoverMountGenericReceiptEvidence(
            mount_transaction_id,
            BUSINESS_CUTOVER_MOUNT_TRANSACTION_KIND,
            audit_row["committed_at"],
        )
        mount_runtime = BusinessCutoverMountRuntimeIdentity(
            authority="sqlite",
            authority_state="active",
            repository_epoch=repository_epoch,
            source_admission_revision=source_revision,
            repository_revision=1,
            state_revision=2,
            runtime_generation=runtime_generation,
            code_digest=code_digest,
            runtime_schema_version=BUSINESS_CUTOVER_RUNTIME_CONTROL_SCHEMA_VERSION,
            target_schema_version=MIGRATIONS[-1].version,
            target_schema_fingerprint=_expected_schema_fingerprint(),
        )
        mount_install = BusinessCutoverMountInstallBinding(
            transaction_id=install_row["transaction_id"],
            receipt_kind=install_row["receipt_kind"],
            committed_at=install_row["committed_at"],
            manifest_sha256=install_sha256,
        )
        mount_coverage = BusinessCutoverMountCoverageBinding(
            transaction_id=coverage_row["transaction_id"],
            receipt_kind=coverage_row["receipt_kind"],
            committed_at=coverage_row["committed_at"],
            manifest_sha256=coverage_manifest_sha256,
            coverage_sha256=coverage_row["coverage_sha256"],
            member_root_sha256=coverage_row["member_root_sha256"],
            batch_count=0,
            repository_member_count=0,
            live_project_count=0,
            deleted_project_count=0,
            project_count=0,
            reference_count=0,
            unique_blob_count=0,
            unique_blob_bytes=0,
            referenced_bytes=0,
            observed_occurrence_count=0,
            observed_row_count=0,
            observed_manifest_bytes=0,
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
            audit_certificate_tail_sha256=audit_evidence.certificate_tail_sha256,
            expected_migration_ledger_count=schema.migration_ledger_count,
            expected_migration_ledger_sha256=schema.migration_ledger_sha256,
            audit_completed_at=findings.completed_at,
            activated_at=activation_evidence.activated_at,
        )
        mount_manifest = _build_mount_manifest(
            cutover_id=cutover_id,
            receipt=mount_receipt,
            runtime=mount_runtime,
            source_admission_sha256=source_sha256,
            final_admission_sha256=final_sha256,
            install=mount_install,
            coverage=mount_coverage,
            activation=mount_activation,
            mounted_at=audit_row["committed_at"],
        )
        mount_sha256 = _mount_manifest_digest(mount_manifest)
        mount_evidence = _require_business_cutover_mount_evidence(
            BusinessCutoverMountEvidence(
                cutover_id=cutover_id,
                receipt_parent=mount_receipt,
                runtime=mount_runtime,
                source_admission_sha256=source_sha256,
                final_admission_sha256=final_sha256,
                install=mount_install,
                coverage=mount_coverage,
                activation=mount_activation,
                mounted_at=audit_row["committed_at"],
                manifest_bytes=mount_manifest,
                manifest_sha256=mount_sha256,
            )
        )
        canonical_head = _require_business_cutover_head_chain(
            issue_business_cutover_head_chain_from_mount(mount_evidence)
        )[-1].row_values()

        connection.execute(
            "UPDATE business_cutover_install_receipts SET manifest_bytes=?,"
            "manifest_sha256=? WHERE cutover_id=?",
            (install_manifest, install_sha256, cutover_id),
        )
        connection.execute(
            "UPDATE business_asset_coverage_receipts SET "
            "source_admission_sha256=?,manifest_bytes=?,manifest_sha256=? "
            "WHERE cutover_id=?",
            (source_sha256, coverage_manifest, coverage_manifest_sha256, cutover_id),
        )
        connection.execute(
            "UPDATE repository_audit_certificates SET schema_version=?,"
            "migration_ledger_count=?,migration_ledger_sha256=?,"
            "worker_ledger_sequence=?,worker_ledger_tail_sha256=?,"
            "findings_sha256=?,manifest_bytes=?,manifest_sha256=?,"
            "certificate_tail_sha256=? WHERE certificate_id=?",
            (
                schema.user_version,
                schema.migration_ledger_count,
                schema.migration_ledger_sha256,
                audit_coverage.worker_ledger_sequence,
                audit_coverage.worker_ledger_tail_sha256,
                _findings_digest(findings),
                audit_manifest,
                audit_manifest_sha256,
                audit_tail,
                audit_evidence.certificate_id,
            ),
        )
        connection.execute(
            "UPDATE repository_audit_head SET tail_sha256=? WHERE singleton=1",
            (audit_tail,),
        )
        connection.execute(
            "UPDATE business_cutover_activation_receipts SET "
            "install_manifest_sha256=?,audit_manifest_sha256=?,"
            "migration_ledger_sha256=?,manifest_bytes=?,manifest_sha256=? "
            "WHERE cutover_id=?",
            (
                install_sha256,
                audit_manifest_sha256,
                schema.migration_ledger_sha256,
                activation_manifest,
                activation_sha256,
                cutover_id,
            ),
        )
        connection.execute(
            "UPDATE business_cutover_mount_receipts SET "
            "install_receipt_kind=?,install_committed_at=?,"
            "install_manifest_sha256=?,coverage_receipt_kind=?,"
            "coverage_committed_at=?,coverage_manifest_sha256=?,"
            "audit_manifest_sha256=?,activation_manifest_sha256=?,"
            "manifest_bytes=?,manifest_sha256=? WHERE cutover_id=?",
            (
                install_row["receipt_kind"],
                install_row["committed_at"],
                install_sha256,
                coverage_row["receipt_kind"],
                coverage_row["committed_at"],
                coverage_manifest_sha256,
                audit_manifest_sha256,
                activation_sha256,
                mount_manifest,
                mount_sha256,
                cutover_id,
            ),
        )
        connection.execute(
            "UPDATE business_cutover_head SET "
            "singleton=:singleton,cutover_id=:cutover_id,"
            "state_revision=:state_revision,phase_revision=:phase_revision,"
            "phase=:phase,authority_state=:authority_state,"
            "repository_epoch=:repository_epoch,"
            "runtime_generation=:runtime_generation,code_digest=:code_digest,"
            "install_transaction_id=:install_transaction_id,"
            "install_manifest_sha256=:install_manifest_sha256,"
            "coverage_transaction_id=:coverage_transaction_id,"
            "coverage_manifest_sha256=:coverage_manifest_sha256,"
            "audit_transaction_id=:audit_transaction_id,"
            "audit_manifest_sha256=:audit_manifest_sha256,"
            "activation_transaction_id=:activation_transaction_id,"
            "activation_manifest_sha256=:activation_manifest_sha256,"
            "mount_transaction_id=:mount_transaction_id,"
            "mount_manifest_sha256=:mount_manifest_sha256,"
            "previous_head_sha256=:previous_head_sha256,"
            "head_sha256=:head_sha256,updated_at=:updated_at "
            "WHERE singleton=1",
            canonical_head,
        )
        if v12_existing:
            closure_row = connection.execute(
                "SELECT manifest_bytes FROM business_cutover_install_closures "
                "WHERE cutover_id=?",
                (cutover_id,),
            ).fetchone()
            if closure_row is None:
                raise AssertionError("canonical fixture V12 closure missing")
            source_row = connection.execute(
                "SELECT * FROM legacy_repository_seals WHERE cutover_id=?",
                (cutover_id,),
            ).fetchone()
            if source_row is None:
                raise AssertionError("canonical fixture V12 source proof missing")
            closure_manifest_value = decode_payload_bounded(
                closure_row[0],
                MAX_BUSINESS_CUTOVER_MANIFEST_BYTES,
                MAX_BUSINESS_CUTOVER_MANIFEST_NODES,
            )
            closure_manifest_value["install"] = {
                "transaction_id": install_row["transaction_id"],
                "manifest_sha256": install_sha256.hex(),
                "committed_at": install_row["committed_at"],
            }
            closure_manifest_value["schema_target"] = {
                "version": MIGRATIONS[-1].version,
                "fingerprint": _expected_schema_fingerprint(),
            }
            closure_manifest_value["source_proof"] = {
                "transaction_id": source_row["transaction_id"],
                "receipt_kind": source_row["receipt_kind"],
                "catalog_id": source_row["catalog_id"],
                "catalog_seal_sha256": source_row[
                    "catalog_seal_sha256"
                ].hex(),
                "repository_epoch": source_row["repository_epoch"],
                "source_admission_revision": source_row[
                    "repository_revision"
                ],
                "manifest_sha256": source_row["manifest_sha256"].hex(),
                "proof_sha256": source_row["proof_sha256"].hex(),
                "source_admission_sha256": source_sha256.hex(),
                "source_repository_sha256": source_row[
                    "source_repository_sha256"
                ].hex(),
                "catalog_coverage_sha256": source_row[
                    "catalog_coverage_sha256"
                ].hex(),
                "schema_fingerprint": source_row["schema_fingerprint"],
                "member_root_sha256": coverage_row[
                    "member_root_sha256"
                ].hex(),
                "project_count": source_row["project_count"],
                "live_project_count": source_row["live_project_count"],
                "deleted_project_count": source_row[
                    "deleted_project_count"
                ],
            }
            closure_manifest_value["asset_coverage"] = {
                "transaction_id": coverage_row["transaction_id"],
                "receipt_kind": coverage_row["receipt_kind"],
                "repository_epoch": coverage_row["repository_epoch"],
                "source_admission_revision": coverage_row[
                    "source_admission_revision"
                ],
                "source_admission_sha256": coverage_row[
                    "source_admission_sha256"
                ].hex(),
                "manifest_sha256": coverage_manifest_sha256.hex(),
                "coverage_sha256": coverage_row["coverage_sha256"].hex(),
                "member_root_sha256": coverage_row[
                    "member_root_sha256"
                ].hex(),
                "batch_count": coverage_row["batch_count"],
                "project_count": coverage_row["project_count"],
                "reference_count": coverage_row["reference_count"],
                "unique_blob_count": coverage_row["unique_blob_count"],
                "unique_blob_bytes": coverage_row["unique_blob_bytes"],
                "referenced_bytes": coverage_row["referenced_bytes"],
                "project_root_sha256": coverage_row[
                    "project_root_sha256"
                ].hex(),
                "occurrence_root_sha256": coverage_row[
                    "occurrence_root_sha256"
                ].hex(),
                "blob_root_sha256": coverage_row[
                    "blob_root_sha256"
                ].hex(),
            }
            closure_manifest_bytes = encode_payload_bounded(
                closure_manifest_value,
                MAX_BUSINESS_CUTOVER_MANIFEST_BYTES,
                MAX_BUSINESS_CUTOVER_MANIFEST_NODES,
            )
            closure_manifest_sha256 = (
                support._install_closure_manifest_digest(
                    closure_manifest_bytes
                )
            )
            connection.execute(
                "UPDATE business_cutover_install_closures SET "
                "install_transaction_id=?,install_manifest_sha256=?,"
                "source_transaction_id=?,source_receipt_kind=?,"
                "source_admission_revision=?,"
                "source_proof_manifest_sha256=?,source_proof_sha256=?,"
                "source_admission_sha256=?,coverage_transaction_id=?,"
                "coverage_receipt_kind=?,coverage_manifest_sha256=?,"
                "coverage_sha256=?,manifest_bytes=?,"
                "manifest_sha256=? WHERE cutover_id=?",
                (
                    install_row["transaction_id"],
                    install_sha256,
                    source_row["transaction_id"],
                    source_row["receipt_kind"],
                    source_row["repository_revision"],
                    source_row["manifest_sha256"],
                    source_row["proof_sha256"],
                    source_sha256,
                    coverage_row["transaction_id"],
                    coverage_row["receipt_kind"],
                    coverage_manifest_sha256,
                    coverage_row["coverage_sha256"],
                    closure_manifest_bytes,
                    closure_manifest_sha256,
                    cutover_id,
                ),
            )
        baseline_receipts = connection.execute(
            "SELECT business.commit_id,business.committed_at,"
            "business.manifest_sha256 FROM business_commits AS business "
            "JOIN atomic_business_write_receipts AS atomic "
            "ON atomic.commit_id=business.commit_id "
            "WHERE business.commit_kind='baseline' AND atomic.control_count=0 "
            "AND atomic.target_count=0 ORDER BY business.repository_revision"
        ).fetchall()
        for baseline_receipt in baseline_receipts:
            values = support._zero_control_atomic_receipt_values(
                connection,
                commit_id=baseline_receipt["commit_id"],
                committed_at=baseline_receipt["committed_at"],
                atomic_manifest_sha256=baseline_receipt["manifest_sha256"],
                mounted_repository_revision=mount_runtime.repository_revision,
                mounted_control_revision=mount_runtime.state_revision,
            )
            connection.execute(
                "UPDATE atomic_business_write_receipts SET "
                "pipeline_intents_sha256=:pipeline_intents_sha256,"
                "controls_sha256=:controls_sha256,"
                "before_worker_sequence=:before_worker_sequence,"
                "before_worker_tail_sha256=:before_worker_tail_sha256,"
                "after_worker_sequence=:after_worker_sequence,"
                "after_worker_tail_sha256=:after_worker_tail_sha256,"
                "manifest_bytes=:manifest_bytes,manifest_sha256=:manifest_sha256,"
                "receipt_sha256=:receipt_sha256,"
                "production_manifest_bytes=:production_manifest_bytes,"
                "production_manifest_sha256=:production_manifest_sha256,"
                "targets_sha256=:targets_sha256,"
                "production_receipt_sha256=:production_receipt_sha256 "
                "WHERE commit_id=:commit_id",
                values,
            )
        for name in trigger_names:
            connection.execute(originals[name])
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise AssertionError("canonical fixture contains a foreign-key violation")
        connection.commit()
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()


def create_canonical_mounted_business_database(
    path: Any,
    *,
    project_id: str = "project-production-v8",
    document_id: str = "document-production-v8",
    commit_id: str = "1" * 32,
    repository_epoch: str = "e" * 64,
    cutover_id: Optional[str] = None,
    source_admission_revision: int = 1,
    runtime_generation: str = "runtime-production-v8-test",
    code_digest: Optional[bytes] = None,
):
    digest = (
        hashlib.sha256(b"ze.business-runtime.test.v1").digest()
        if code_digest is None
        else code_digest
    )
    baseline = support.create_active_business_database(
        path,
        project_id=project_id,
        document_id=document_id,
        commit_id=commit_id,
        repository_epoch=repository_epoch,
        cutover_id=cutover_id,
        source_admission_revision=source_admission_revision,
        runtime_generation=runtime_generation,
        code_digest=digest,
    )
    _upgrade_to_canonical_mount(
        baseline,
        repository_epoch=repository_epoch,
        cutover_id=baseline.profile.cutover_id,
        source_revision=source_admission_revision,
        runtime_generation=runtime_generation,
        code_digest=digest,
    )
    return baseline


def _pipeline_create_control(
    atomic_plan: Any,
    intent: Any,
    *,
    committed_at: str,
    before_ledger: Any,
):
    """Issue the exact worker-ledger member owned by one pipeline CREATE."""

    if intent.intent_kind is not PipelineIntentKind.CREATE:
        raise ValueError(
            "automatic production test controls require pipeline CREATE intents"
        )
    replay = WorkerReplayPolicy(intent.replay_policy.value)
    idempotency = (
        WorkerIdempotencyPolicy.SINGLE_ATTEMPT
        if replay is WorkerReplayPolicy.NEVER_REPLAY
        else WorkerIdempotencyPolicy.DURABLE_RECEIPT_RECONCILE
    )
    target = WorkerTarget(
        target_kind=WorkerTargetKind.PIPELINE,
        target_id=intent.job_id,
        project_id=atomic_plan.before.project_id,
        generation=atomic_plan.before.generation,
        document_id=atomic_plan.before.document_id,
        origin_commit_id=atomic_plan.commit_id,
        payload=intent.payload,
        replay_policy=replay,
        idempotency_policy=idempotency,
        idempotency_scope="pipeline/%s/%d/%s"
        % (
            atomic_plan.before.project_id,
            atomic_plan.before.generation,
            intent.pipeline_id,
        ),
        idempotency_key=intent.idempotency_key,
        max_attempts=intent.max_attempts,
        pipeline_id=intent.pipeline_id,
        intent_type=intent.intent_type,
    )
    request = PipelineBusinessControlRequest(
        request_transaction_id=intent.intent_id,
        authority=atomic_plan.authority,
        target=target,
        control_intent=PipelineBusinessControlIntentBinding(
            origin_commit_id=atomic_plan.commit_id,
            intent_type=intent.intent_type,
            idempotency_key=intent.idempotency_key,
            payload=intent.payload,
            replay_policy=replay,
            idempotency_policy=idempotency,
            max_attempts=intent.max_attempts,
        ),
        operation=PipelineBusinessControlOperation.CREATE,
        before=None,
        target_status=WorkerExecutionStatus.QUEUED,
        occurred_at=committed_at,
    )
    return _issue_pipeline_business_control_plan(
        request,
        before_ledger=before_ledger,
    )


def issue_mounted_production_atomic_write_plan(
    database: Any,
    atomic_plan: Any,
    *,
    planning: Any = None,
    control_plans: Optional[tuple] = None,
):
    """Build a closed mounted plan for every production target group.

    With ``control_plans=None`` this test fixture issues CREATE controls in
    pipeline-intent order and threads their receipts through the one shared
    worker ledger read by the planning snapshot.  Transition controls need a
    durable pre-state and can therefore be supplied explicitly.  Targets are
    flattened in the protocol's required pipeline/action/outbox group order;
    outbox allocation sequence is independent of each event's ordinal.
    """

    snapshot = (
        _read_production_atomic_planning_snapshot(database)
        if planning is None
        else planning
    )
    before_worker_ledger = snapshot.worker_ledger_head.evidence.ledger_head
    if control_plans is None:
        issued_controls = []
        previous = before_worker_ledger
        for intent in atomic_plan.pipeline_intents:
            control = _pipeline_create_control(
                atomic_plan,
                intent,
                committed_at=snapshot.committed_at,
                before_ledger=previous,
            )
            issued_controls.append(control)
            previous = control.after_ledger
        controls = tuple(issued_controls)
    else:
        if type(control_plans) is not tuple:
            raise TypeError("control_plans must be an exact tuple")
        controls = control_plans

    inner = _issue_atomic_business_write_plan(
        atomic_plan=atomic_plan,
        control_plans=controls,
        committed_at=snapshot.committed_at,
        before_worker_ledger=before_worker_ledger,
    )

    pipeline_targets = tuple(
        ProductionWorkerTarget(
            source_ordinal=intent.ordinal,
            target=control.target,
            genesis_status=WorkerExecutionStatus.QUEUED,
            genesis_state_sha256=control.closure.after_state_sha256,
        )
        for intent, control in zip(atomic_plan.pipeline_intents, controls)
        if intent.intent_kind is PipelineIntentKind.CREATE
    )
    action_targets = []
    for action in atomic_plan.durable_actions:
        target = WorkerTarget(
            target_kind=WorkerTargetKind.ACTION,
            target_id=action.action_id,
            project_id=atomic_plan.before.project_id,
            generation=atomic_plan.before.generation,
            document_id=atomic_plan.before.document_id,
            origin_commit_id=atomic_plan.commit_id,
            payload=action.payload,
            replay_policy=WorkerReplayPolicy(action.replay_policy.value),
            idempotency_policy=WorkerIdempotencyPolicy(
                action.idempotency_policy.value
            ),
            idempotency_scope=action.idempotency_scope,
            idempotency_key=action.idempotency_key,
            max_attempts=action.max_attempts,
        )
        action_targets.append(ProductionWorkerTarget(
            source_ordinal=action.ordinal,
            target=target,
            genesis_status=WorkerExecutionStatus.QUEUED,
            genesis_state_sha256=_production_worker_genesis_state_sha256(
                target,
                status=WorkerExecutionStatus.QUEUED,
            ),
            action_type=action.action_type,
        ))

    outbox_targets = []
    for offset, event in enumerate(atomic_plan.outbox, start=1):
        target = WorkerTarget(
            target_kind=WorkerTargetKind.OUTBOX,
            target_id=event.event_id,
            project_id=atomic_plan.before.project_id,
            generation=atomic_plan.before.generation,
            document_id=atomic_plan.before.document_id,
            origin_commit_id=atomic_plan.commit_id,
            payload=event.payload,
            replay_policy=PRODUCTION_OUTBOX_REPLAY_POLICY,
            idempotency_policy=PRODUCTION_OUTBOX_IDEMPOTENCY_POLICY,
            idempotency_scope="outbox/%s/%d"
            % (atomic_plan.before.project_id, atomic_plan.before.generation),
            idempotency_key=event.event_id,
            max_attempts=PRODUCTION_OUTBOX_MAX_ATTEMPTS,
            outbox_order=WorkerOutboxOrder(
                snapshot.outbox_allocation_head.sequence + offset,
                atomic_plan.candidate.authority_revision,
                event.ordinal,
            ),
        )
        outbox_targets.append(ProductionWorkerTarget(
            source_ordinal=event.ordinal,
            target=target,
            genesis_status=WorkerExecutionStatus.PENDING,
            genesis_state_sha256=_production_worker_genesis_state_sha256(
                target,
                status=WorkerExecutionStatus.PENDING,
            ),
            outbox_topic=event.topic,
            outbox_partition_key=event.partition_key,
        ))

    return _issue_production_atomic_write_plan(
        atomic_write=inner,
        mounted_authority=snapshot.mounted_authority,
        worker_ledger_head=snapshot.worker_ledger_head,
        outbox_dispatch_head=snapshot.outbox_dispatch_head,
        targets=pipeline_targets + tuple(action_targets) + tuple(outbox_targets),
        before_outbox_allocation=snapshot.outbox_allocation_head,
    )


__all__ = ()
