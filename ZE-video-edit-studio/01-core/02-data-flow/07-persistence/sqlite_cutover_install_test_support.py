"""Test-only construction for the fixed V12 cutover install writer.

The fixture is a real current-schema preinstall database: it retains the
legacy repository marker, persists the canonical empty V4 catalog and V5
repository proof, and appends the exact V8 repository asset-coverage seal.
It never disables foreign keys, triggers, schema checks, or the kernel safety
profile.  The production writer must perform the legacy->sqlite transition.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Any, NamedTuple, Optional, Tuple

from business_cutover_asset_contract import BusinessCutoverAssetBlobCarrier
from business_cutover_asset_integrity import (
    _DURABLE_READER_TOKEN,
    _issue_business_cutover_asset_coverage_plan,
    _issue_business_cutover_asset_stage_members,
    _issue_business_cutover_asset_stage_plan,
    _repository_member_root,
    _stamp_business_cutover_asset_stage_observation,
)
from business_cutover_baseline_projection import (
    BusinessCutoverBaselineProjection,
    _project_business_cutover_baseline,
)
from business_cutover_integrity import _issue_business_cutover_plan
from business_cutover_install_closure import (
    BusinessCutoverInstallClosureProjection,
    _project_business_cutover_install_closure,
)
from business_snapshot import BusinessSnapshotSet, capture_business_snapshot
from document import Document
from entity import Entity
from legacy_import_contract import (
    LEGACY_BATCH_PROTOCOL,
    LEGACY_IMPORT_PLAN_PROTOCOL,
    LEGACY_MANIFEST_SCHEMA,
    LEGACY_PAYLOAD_PROTOCOL,
    SQLITE_IMPORT_RECEIPT_PROTOCOL,
    SQLITE_IMPORT_TERMINAL_STATUS,
    SQLITE_IMPORT_TRANSACTION_KIND,
    SQLITE_IMPORT_TRANSACTION_PREFIX,
)
from legacy_import_integrity import (
    LegacyImportIntegrityProject,
    build_legacy_import_integrity,
)
from legacy_repository_contract import (
    LEGACY_REPOSITORY_LEGACY_ADOPT_OPERATION_ID,
    LEGACY_REPOSITORY_MEMBER_DELETED,
    LEGACY_REPOSITORY_MEMBER_LIVE,
    LEGACY_REPOSITORY_TRANSACTION_KIND,
)
from legacy_repository_integrity import LegacyRepositoryIntegrityMember
from sqlite_business_test_support import (
    TEST_BASELINE_COMMITTED_AT,
    TEST_CUTOVER_COMMITTED_AT,
    _insert_empty_legacy_source_proof,
    _prepare_empty_cutover_authority,
)
from sqlite_cutover_install_writer import (
    SQLiteCutoverInstallWriter,
    _CUTOVER_INSTALL_WRITER_CAPABILITY,
)
from sqlite_kernel import SqliteDatabase, _expected_schema_fingerprint
import sqlite_legacy_catalog
import sqlite_legacy_import
import sqlite_legacy_repository
import sqlite_legacy_repository_exact
from sqlite_schema import MIGRATIONS


TEST_LIVE_ASSET_BYTES = b"cutover-install-live-asset-v1"


class _LegacyProjectRow(NamedTuple):
    project_id: str
    generation: int
    codec_id: str
    codec_version: int
    snapshot_format_version: int
    snapshot_bytes: bytes
    snapshot_sha256: bytes


class _LegacyDocumentRow(NamedTuple):
    project_id: str
    document_id: str
    document_version: int
    schema_version: str
    collaboration_mode: str
    save_policy: Optional[str]
    codec_id: str
    codec_version: int
    snapshot_format_version: int
    snapshot_bytes: bytes
    snapshot_sha256: bytes


class _LegacyEntityRow(NamedTuple):
    project_id: str
    document_id: str
    entity_type: str
    entity_id: str
    entity_version: int
    deleted: int
    codec_id: str
    codec_version: int
    snapshot_format_version: int
    snapshot_bytes: bytes
    snapshot_sha256: bytes


class _LegacyProjectPlan(NamedTuple):
    ordinal: int
    project_id: str
    generation: int
    document_id: str
    document_version: int
    storage_version: str
    source_head_sha256: bytes
    source_head_bytes: int
    business_root_sha256: bytes
    entity_count: int
    row_snapshot_bytes: int
    project_row: _LegacyProjectRow
    document_row: _LegacyDocumentRow
    entity_rows: Tuple[_LegacyEntityRow, ...]


class _LegacyImportPlan(NamedTuple):
    plan_protocol: str
    import_id: str
    transaction_id: str
    receipt_kind: str
    receipt_protocol: str
    status: str
    manifest_schema: str
    batch_protocol: str
    payload_protocol: str
    selection: str
    project_count: int
    entity_count: int
    row_snapshot_bytes: int
    framed_payload_bytes: int
    manifest_bytes: bytes
    manifest_sha256: bytes
    payload_sha256: bytes
    batch_sha256: bytes
    projects: Tuple[_LegacyProjectPlan, ...]


@dataclass(frozen=True)
class SQLiteCutoverInstallFixture:
    database: SqliteDatabase
    catalog: Any
    repository: Any
    plan: Any
    coverage_plan: Any
    baseline: BusinessCutoverBaselineProjection
    projection: BusinessCutoverInstallClosureProjection
    source_committed_at: str
    install_committed_at: str


def _live_business_snapshot(
    *,
    asset_blob: Optional[bytes] = None,
) -> BusinessSnapshotSet:
    """Capture one deterministic source graph shared by V3 and cutover."""

    asset = None
    if asset_blob is not None:
        asset = {
            "sha256": hashlib.sha256(asset_blob).hexdigest(),
            "size": len(asset_blob),
        }
    document = Document(
        id="document-project-a",
        name="cutover install source",
        schema_version="1",
        collaboration_mode="live_collab",
        save_policy="auto_commit",
        schema=(
            {
                "card": {
                    "type": "object",
                    "properties": {"title": {"type": "string"}},
                }
            }
            if asset is None
            else {
                "media": {
                    "type": "object",
                    "properties": {"asset": {"type": "asset_ref"}},
                }
            }
        ),
        config={},
        acl={},
    )
    entity = Entity(
        type="card" if asset is None else "media",
        id="entity-1",
        name="Card" if asset is None else "Media",
        data={"title": "alpha"} if asset is None else {"asset": asset},
    )
    document.add(entity)
    entity._version = 1
    entity._created_at = 1
    entity._updated_at = 1
    document._v = 7
    document._created_at = 1
    document._updated_at = 2
    return capture_business_snapshot("project-a", 1, document).snapshot


def _legacy_import_plan(
    snapshot: BusinessSnapshotSet,
    *,
    import_id: str = "a" * 64,
) -> _LegacyImportPlan:
    project = snapshot.project
    document = snapshot.document
    project_row = _LegacyProjectRow(
        project.project_id,
        project.generation,
        project.codec_id,
        project.codec_version,
        project.snapshot_format_version,
        project.snapshot_bytes,
        project.snapshot_sha256,
    )
    document_row = _LegacyDocumentRow(
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
    )
    entity_rows = tuple(
        _LegacyEntityRow(
            row.project_id,
            row.document_id,
            row.entity_type,
            row.entity_id,
            row.entity_version,
            row.deleted,
            row.codec_id,
            row.codec_version,
            row.snapshot_format_version,
            row.snapshot_bytes,
            row.snapshot_sha256,
        )
        for row in snapshot.entities
    )
    source = b"cutover-install-live-source-v1"
    row_snapshot_bytes = (
        len(project.snapshot_bytes)
        + len(document.snapshot_bytes)
        + sum(len(row.snapshot_bytes) for row in snapshot.entities)
    )
    planned = _LegacyProjectPlan(
        0,
        project.project_id,
        project.generation,
        document.document_id,
        document.document_version,
        "v2",
        hashlib.sha256(source).digest(),
        len(source),
        project.snapshot_sha256,
        len(entity_rows),
        row_snapshot_bytes,
        project_row,
        document_row,
        entity_rows,
    )
    integrity = build_legacy_import_integrity(
        import_id,
        (
            LegacyImportIntegrityProject(
                project_id=planned.project_id,
                generation=planned.generation,
                document_id=planned.document_id,
                document_version=planned.document_version,
                storage_version=planned.storage_version,
                source_head_sha256=planned.source_head_sha256,
                source_head_bytes=planned.source_head_bytes,
                business_root_sha256=planned.business_root_sha256,
                entity_count=planned.entity_count,
                row_snapshot_bytes=planned.row_snapshot_bytes,
                project_snapshot_bytes=project.snapshot_bytes,
                document_snapshot_bytes=document.snapshot_bytes,
                entity_snapshot_bytes=tuple(
                    row.snapshot_bytes for row in snapshot.entities
                ),
            ),
        ),
    )
    return _LegacyImportPlan(
        LEGACY_IMPORT_PLAN_PROTOCOL,
        import_id,
        SQLITE_IMPORT_TRANSACTION_PREFIX + import_id,
        SQLITE_IMPORT_TRANSACTION_KIND,
        SQLITE_IMPORT_RECEIPT_PROTOCOL,
        SQLITE_IMPORT_TERMINAL_STATUS,
        LEGACY_MANIFEST_SCHEMA,
        LEGACY_BATCH_PROTOCOL,
        LEGACY_PAYLOAD_PROTOCOL,
        "explicit_projects",
        1,
        integrity.entity_count,
        integrity.row_snapshot_bytes,
        integrity.framed_payload_bytes,
        integrity.manifest_bytes,
        integrity.manifest_sha256,
        integrity.payload_sha256,
        integrity.batch_sha256,
        (planned,),
    )


def _live_repository_member(catalog: Any) -> LegacyRepositoryIntegrityMember:
    project = catalog.projects[0]
    return LegacyRepositoryIntegrityMember(
        project_id=project.project_id,
        generation=project.generation,
        status=LEGACY_REPOSITORY_MEMBER_LIVE,
        operation_id=LEGACY_REPOSITORY_LEGACY_ADOPT_OPERATION_ID,
        source_state_sha256=project.source_head_sha256,
        source_state_bytes=project.source_head_bytes,
        catalog_ordinal=0,
        import_id=catalog.integrity.import_id,
        document_id=project.document_id,
        document_version=project.document_version,
        storage_version=project.storage_version,
        business_root_sha256=project.business_root_sha256,
        entity_count=project.entity_count,
        row_snapshot_bytes=project.row_snapshot_bytes,
    )


def _deleted_repository_member() -> LegacyRepositoryIntegrityMember:
    return LegacyRepositoryIntegrityMember(
        project_id="project-z-deleted",
        generation=4,
        status=LEGACY_REPOSITORY_MEMBER_DELETED,
        operation_id="1" * 32,
        source_state_sha256=hashlib.sha256(b"").digest(),
        source_state_bytes=0,
        catalog_ordinal=None,
        import_id=None,
        document_id=None,
        document_version=None,
        storage_version=None,
        business_root_sha256=None,
        entity_count=None,
        row_snapshot_bytes=None,
    )


def _insert_coverage_receipt(
    connection: Any,
    *,
    plan: Any,
    coverage_plan: Any,
    committed_at: str,
) -> None:
    evidence = coverage_plan.expected_evidence
    coverage = coverage_plan.coverage
    for stage in coverage_plan.batch_evidences:
        connection.execute(
            "INSERT INTO transaction_receipts(transaction_id,kind,committed_at) "
            "VALUES (?,?,?)",
            (stage.transaction_id, stage.receipt_kind, committed_at),
        )
        stage_members = _issue_business_cutover_asset_stage_members(
            projects=stage.projects,
            blobs=stage.blobs,
            occurrences=stage.occurrences,
        )
        connection.executemany(
            """
            INSERT INTO business_asset_stage_members(
                batch_id,cutover_id,member_kind,ordinal,member_id,
                member_bytes,member_sha256,metadata_sha256
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            tuple(
                (
                    stage.batch_id,
                    stage.cutover_id,
                    member.member_kind,
                    member.ordinal,
                    member.member_id,
                    member.member_bytes,
                    member.member_sha256,
                    member.metadata_sha256,
                )
                for member in stage_members
            ),
        )
        connection.execute(
            """
            INSERT INTO business_asset_stage_receipts(
                batch_id,cutover_id,transaction_id,receipt_kind,committed_at,
                repository_epoch,source_admission_revision,
                source_admission_sha256,member_count,project_count,blob_count,
                occurrence_count,unique_blob_bytes,referenced_bytes,
                project_root_sha256,blob_root_sha256,occurrence_root_sha256,
                coverage_sha256,manifest_bytes,manifest_sha256
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                stage.batch_id,
                stage.cutover_id,
                stage.transaction_id,
                stage.receipt_kind,
                committed_at,
                stage.repository_epoch,
                stage.source_admission_revision,
                stage.source_admission_sha256,
                len(stage_members),
                stage.project_count,
                stage.unique_blob_count,
                stage.reference_count,
                stage.unique_blob_bytes,
                stage.referenced_bytes,
                stage.project_root_sha256,
                stage.blob_root_sha256,
                stage.occurrence_root_sha256,
                stage.coverage_sha256,
                stage.manifest_bytes,
                stage.manifest_sha256,
            ),
        )
    connection.execute(
        "INSERT INTO transaction_receipts(transaction_id,kind,committed_at) "
        "VALUES (?,?,?)",
        (coverage_plan.transaction_id, evidence.receipt_kind, committed_at),
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
            plan.cutover_id,
            coverage_plan.transaction_id,
            evidence.receipt_kind,
            committed_at,
            coverage_plan.repository_epoch,
            coverage_plan.source_admission_revision,
            coverage_plan.source_admission_sha256,
            evidence.batch_count,
            _repository_member_root(plan),
            plan.legacy_proof.project_count,
            plan.legacy_proof.live_project_count,
            plan.legacy_proof.deleted_project_count,
            evidence.observed_occurrence_count,
            evidence.observed_row_count,
            evidence.observed_manifest_bytes,
            coverage.project_count,
            coverage.reference_count,
            coverage.unique_blob_count,
            coverage.unique_blob_bytes,
            coverage.referenced_bytes,
            coverage.project_root_sha256,
            coverage.blob_root_sha256,
            coverage.occurrence_root_sha256,
            coverage.coverage_sha256,
            coverage_plan.manifest_bytes,
            coverage_plan.manifest_sha256,
        ),
    )


def _project_empty_cutover_install(
    *,
    repository_epoch: str,
    source_admission_revision: int,
    runtime_generation: str,
    code_digest: bytes,
    install_committed_at: str,
) -> tuple[
    Any,
    Any,
    Any,
    Any,
    BusinessCutoverBaselineProjection,
    BusinessCutoverInstallClosureProjection,
]:
    """Build the issuer-sealed empty plan/coverage/baseline/closure bundle."""

    catalog, repository, plan, coverage_plan = _prepare_empty_cutover_authority(
        repository_epoch=repository_epoch,
        source_admission_revision=source_admission_revision,
        runtime_generation=runtime_generation,
        code_digest=code_digest,
    )
    baseline = _project_business_cutover_baseline(
        plan,
        committed_at=install_committed_at,
    )
    projection = _project_business_cutover_install_closure(
        plan,
        baseline,
        coverage_plan=coverage_plan,
    )
    return catalog, repository, plan, coverage_plan, baseline, projection


def _make_cutover_install_writer(
    fixture: SQLiteCutoverInstallFixture,
) -> SQLiteCutoverInstallWriter:
    if type(fixture) is not SQLiteCutoverInstallFixture:
        raise TypeError("fixture must be an exact SQLiteCutoverInstallFixture")
    return SQLiteCutoverInstallWriter(
        fixture.database,
        _capability=_CUTOVER_INSTALL_WRITER_CAPABILITY,
    )


def _create_empty_cutover_install_fixture(
    path: Any,
    *,
    repository_epoch: str = "e" * 64,
    source_admission_revision: int = 1,
    runtime_generation: str = "cutover-install-writer-test",
    code_digest: bytes = hashlib.sha256(b"ze.cutover-install-writer.test.v1").digest(),
) -> SQLiteCutoverInstallFixture:
    """Create one exact empty preinstall authority with all guards enabled."""

    if type(repository_epoch) is not str:
        raise TypeError("repository_epoch must be an exact string")
    if type(source_admission_revision) is not int:
        raise TypeError("source_admission_revision must be an exact integer")
    if type(runtime_generation) is not str:
        raise TypeError("runtime_generation must be an exact string")
    if type(code_digest) is not bytes:
        raise TypeError("code_digest must be exact bytes")

    database = SqliteDatabase(Path(path).resolve())
    database.initialize()
    (
        catalog,
        repository,
        plan,
        coverage_plan,
        baseline,
        projection,
    ) = _project_empty_cutover_install(
        repository_epoch=repository_epoch,
        source_admission_revision=source_admission_revision,
        runtime_generation=runtime_generation,
        code_digest=code_digest,
        install_committed_at=TEST_BASELINE_COMMITTED_AT,
    )

    connection = None
    committed = False
    try:
        connection, _filesystem = database._open_unvalidated()
        connection.execute("BEGIN IMMEDIATE")
        database._validate_initialized(connection)
        _insert_empty_legacy_source_proof(
            connection,
            catalog=catalog,
            repository=repository,
        )
        evidence = coverage_plan.expected_evidence
        coverage = coverage_plan.coverage
        connection.execute(
            "INSERT INTO transaction_receipts(transaction_id,kind,committed_at) "
            "VALUES (?,?,?)",
            (
                coverage_plan.transaction_id,
                evidence.receipt_kind,
                TEST_CUTOVER_COMMITTED_AT,
            ),
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
                plan.cutover_id,
                coverage_plan.transaction_id,
                evidence.receipt_kind,
                TEST_CUTOVER_COMMITTED_AT,
                repository_epoch,
                source_admission_revision,
                coverage_plan.source_admission_sha256,
                evidence.batch_count,
                _repository_member_root(plan),
                plan.legacy_proof.project_count,
                plan.legacy_proof.live_project_count,
                plan.legacy_proof.deleted_project_count,
                evidence.observed_occurrence_count,
                evidence.observed_row_count,
                evidence.observed_manifest_bytes,
                coverage.project_count,
                coverage.reference_count,
                coverage.unique_blob_count,
                coverage.unique_blob_bytes,
                coverage.referenced_bytes,
                coverage.project_root_sha256,
                coverage.blob_root_sha256,
                coverage.occurrence_root_sha256,
                coverage.coverage_sha256,
                coverage_plan.manifest_bytes,
                coverage_plan.manifest_sha256,
            ),
        )
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise AssertionError(
                "empty cutover preinstall fixture violates a foreign key"
            )
        connection.commit()
        committed = True
    finally:
        if connection is not None:
            if connection.in_transaction:
                connection.rollback()
            connection.close()
    if not committed:
        raise AssertionError("empty cutover preinstall fixture did not commit")
    database._finalize_committed_storage()
    return SQLiteCutoverInstallFixture(
        database=database,
        catalog=catalog,
        repository=repository,
        plan=plan,
        coverage_plan=coverage_plan,
        baseline=baseline,
        projection=projection,
        source_committed_at=TEST_CUTOVER_COMMITTED_AT,
        install_committed_at=TEST_BASELINE_COMMITTED_AT,
    )


def _create_live_cutover_install_fixture(
    path: Any,
    *,
    repository_epoch: str = "e" * 64,
    source_admission_revision: int = 7,
    runtime_generation: str = "cutover-install-live-test",
    code_digest: bytes = hashlib.sha256(b"ze.cutover-install-live.test.v1").digest(),
    include_deleted: bool = False,
    asset_blob: Optional[bytes] = None,
) -> SQLiteCutoverInstallFixture:
    """Create a guarded one-live preinstall through the real V3/V4/V5 chain."""

    database = SqliteDatabase(Path(path).resolve())
    database.initialize()
    snapshot = _live_business_snapshot(asset_blob=asset_blob)
    import_plan = _legacy_import_plan(snapshot)
    sqlite_legacy_import.SqliteLegacyImporter(database).import_plan(import_plan)
    catalog_sealer = sqlite_legacy_catalog.SqliteLegacyCatalogSealer(
        database,
        _capability=sqlite_legacy_catalog._LEGACY_CATALOG_SEAL_CAPABILITY,
    )
    catalog_sealer.seal(import_plan)
    catalog = sqlite_legacy_catalog._project_nonempty(import_plan)
    members = (_live_repository_member(catalog),)
    if include_deleted:
        members = tuple(
            sorted(
                members + (_deleted_repository_member(),),
                key=lambda value: value.project_id,
            )
        )
    repository = sqlite_legacy_repository_exact._issue_repository_projection(
        catalog,
        members,
        repository_epoch=repository_epoch,
        repository_revision=source_admission_revision,
        catalog_revision=source_admission_revision,
        runtime_generation=runtime_generation,
        code_digest=code_digest,
    )
    repository_sealer = sqlite_legacy_repository.SqliteLegacyRepositorySealer(
        database,
        _capability=sqlite_legacy_repository._LEGACY_REPOSITORY_SEAL_CAPABILITY,
    )
    repository_sealer.seal(repository)
    plan = _issue_business_cutover_plan(
        legacy_integrity=repository.integrity,
        legacy_members=members,
        catalog_seal_sha256=catalog.integrity.seal_sha256,
        legacy_schema_fingerprint=repository.schema_fingerprint,
        runtime_generation=runtime_generation,
        code_digest=code_digest,
        expected_target_schema_version=MIGRATIONS[-1].version,
        expected_target_schema_fingerprint=_expected_schema_fingerprint(),
        business_snapshots=(snapshot,),
    )
    stage_observations = ()
    if asset_blob is not None:
        carrier = BusinessCutoverAssetBlobCarrier(
            sha256=hashlib.sha256(asset_blob).hexdigest(),
            blob_bytes=asset_blob,
            byte_length=len(asset_blob),
            media_type="image/png",
            extension=".png",
        )
        stage = _issue_business_cutover_asset_stage_plan(
            cutover_plan=plan,
            business_snapshots=(snapshot,),
            blob_carriers=(carrier,),
        )
        stage_observations = (
            _stamp_business_cutover_asset_stage_observation(
                plan=stage,
                current_admission=plan.source_admission,
                evidence=stage.expected_evidence,
                _reader_token=_DURABLE_READER_TOKEN,
            ),
        )
    coverage_plan = _issue_business_cutover_asset_coverage_plan(
        cutover_plan=plan,
        business_snapshots=(snapshot,),
        stage_observations=stage_observations,
    )
    connection = None
    try:
        connection, _filesystem = database._open_unvalidated()
        connection.execute("BEGIN IMMEDIATE")
        source_row = connection.execute(
            "SELECT committed_at FROM transaction_receipts "
            "WHERE transaction_id=? AND kind=?",
            (
                repository.integrity.transaction_id,
                LEGACY_REPOSITORY_TRANSACTION_KIND,
            ),
        ).fetchone()
        if source_row is None:
            raise AssertionError("live source receipt is missing")
        source_committed_at = source_row[0]
        clock_row = connection.execute(
            "SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')"
        ).fetchone()
        if clock_row is None or source_committed_at > clock_row[0]:
            raise AssertionError("live source receipt is ahead of the database clock")
        install_committed_at = clock_row[0]
        _insert_coverage_receipt(
            connection,
            plan=plan,
            coverage_plan=coverage_plan,
            committed_at=install_committed_at,
        )
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise AssertionError("live cutover preinstall violates a foreign key")
        connection.commit()
    finally:
        if connection is not None:
            if connection.in_transaction:
                connection.rollback()
            connection.close()
    baseline = _project_business_cutover_baseline(
        plan,
        committed_at=install_committed_at,
    )
    projection = _project_business_cutover_install_closure(
        plan,
        baseline,
        coverage_plan=coverage_plan,
    )
    database._finalize_committed_storage()
    return SQLiteCutoverInstallFixture(
        database=database,
        catalog=catalog,
        repository=repository,
        plan=plan,
        coverage_plan=coverage_plan,
        baseline=baseline,
        projection=projection,
        source_committed_at=source_committed_at,
        install_committed_at=install_committed_at,
    )


def _create_deleted_cutover_install_fixture(
    path: Any,
    *,
    repository_epoch: str = "e" * 64,
    source_admission_revision: int = 7,
    runtime_generation: str = "cutover-install-deleted-test",
    code_digest: bytes = hashlib.sha256(b"ze.cutover-install-deleted.test.v1").digest(),
) -> SQLiteCutoverInstallFixture:
    """Create an empty-catalog V5 deleted high-water plus V8 coverage."""

    database = SqliteDatabase(Path(path).resolve())
    database.initialize()
    catalog_sealer = sqlite_legacy_catalog.SqliteLegacyCatalogSealer(
        database,
        _capability=sqlite_legacy_catalog._LEGACY_CATALOG_SEAL_CAPABILITY,
    )
    catalog_sealer.seal_empty()
    catalog = sqlite_legacy_catalog._project_empty()
    member = _deleted_repository_member()
    repository = sqlite_legacy_repository_exact._issue_repository_projection(
        catalog,
        (member,),
        repository_epoch=repository_epoch,
        repository_revision=source_admission_revision,
        catalog_revision=source_admission_revision,
        runtime_generation=runtime_generation,
        code_digest=code_digest,
    )
    sqlite_legacy_repository.SqliteLegacyRepositorySealer(
        database,
        _capability=sqlite_legacy_repository._LEGACY_REPOSITORY_SEAL_CAPABILITY,
    ).seal(repository)
    plan = _issue_business_cutover_plan(
        legacy_integrity=repository.integrity,
        legacy_members=(member,),
        catalog_seal_sha256=catalog.integrity.seal_sha256,
        legacy_schema_fingerprint=repository.schema_fingerprint,
        runtime_generation=runtime_generation,
        code_digest=code_digest,
        expected_target_schema_version=MIGRATIONS[-1].version,
        expected_target_schema_fingerprint=_expected_schema_fingerprint(),
        business_snapshots=(),
    )
    coverage_plan = _issue_business_cutover_asset_coverage_plan(
        cutover_plan=plan,
        business_snapshots=(),
        stage_observations=(),
    )
    connection = None
    try:
        connection, _filesystem = database._open_unvalidated()
        connection.execute("BEGIN IMMEDIATE")
        source_row = connection.execute(
            "SELECT committed_at FROM transaction_receipts "
            "WHERE transaction_id=? AND kind=?",
            (
                repository.integrity.transaction_id,
                LEGACY_REPOSITORY_TRANSACTION_KIND,
            ),
        ).fetchone()
        if source_row is None:
            raise AssertionError("deleted source receipt is missing")
        source_committed_at = source_row[0]
        clock_row = connection.execute(
            "SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')"
        ).fetchone()
        if clock_row is None or source_committed_at > clock_row[0]:
            raise AssertionError(
                "deleted source receipt is ahead of the database clock"
            )
        install_committed_at = clock_row[0]
        _insert_coverage_receipt(
            connection,
            plan=plan,
            coverage_plan=coverage_plan,
            committed_at=install_committed_at,
        )
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise AssertionError("deleted cutover preinstall violates a foreign key")
        connection.commit()
    finally:
        if connection is not None:
            if connection.in_transaction:
                connection.rollback()
            connection.close()
    baseline = _project_business_cutover_baseline(
        plan,
        committed_at=install_committed_at,
    )
    projection = _project_business_cutover_install_closure(
        plan,
        baseline,
        coverage_plan=coverage_plan,
    )
    database._finalize_committed_storage()
    return SQLiteCutoverInstallFixture(
        database=database,
        catalog=catalog,
        repository=repository,
        plan=plan,
        coverage_plan=coverage_plan,
        baseline=baseline,
        projection=projection,
        source_committed_at=source_committed_at,
        install_committed_at=install_committed_at,
    )


__all__ = ()
