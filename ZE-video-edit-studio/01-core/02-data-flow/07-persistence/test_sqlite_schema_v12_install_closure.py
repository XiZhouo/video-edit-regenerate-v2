"""Executable schema-v12 install-artifact and baseline closure evidence."""

from __future__ import annotations

import hashlib
import sqlite3
import sys
from pathlib import Path
from typing import Any, Mapping

import pytest


_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import sqlite_kernel as kernel
import sqlite_schema as schema
import legacy_repository_contract as repository_contract
from test_sqlite_schema import (
    _apply_migrations,
    _insert_catalog_member,
    _insert_catalog_seal,
    _insert_import_batch_and_descriptor,
)


_NOW = "2026-07-12T00:00:00.000Z"
_SOURCE_TIME = "2026-07-11T23:59:59.998Z"
_COVERAGE_TIME = "2026-07-11T23:59:59.999Z"
_MOUNTED = "2026-07-12T00:00:00.001Z"
_LATER = "2026-07-12T00:00:00.002Z"
_INVALID_24 = "x" * 24
_CUTOVER_ID = "a" * 64
_EPOCH = "e" * 64
_COMMIT_ID = "b" * 32
_INSTALL_TRANSACTION_ID = "_business-cutover-install:" + _CUTOVER_ID
_INSTALL_KIND = "_business_cutover_install_v1"
_INSTALL_MANIFEST = b"v12-install-manifest"
_INSTALL_MANIFEST_SHA256 = hashlib.sha256(_INSTALL_MANIFEST).digest()
_SOURCE_TRANSACTION_ID = "_legacy-repository-seal:" + _CUTOVER_ID
_SOURCE_MANIFEST_SHA256 = hashlib.sha256(b"v12-source-manifest").digest()
_SOURCE_PROOF_SHA256 = hashlib.sha256(b"v12-source-proof").digest()
_SOURCE_ADMISSION_SHA256 = hashlib.sha256(b"v12-source-admission").digest()
_COVERAGE_TRANSACTION_ID = "_business-cutover-asset-coverage:" + _CUTOVER_ID
_COVERAGE_KIND = "_business_cutover_asset_coverage_v1"
_COVERAGE_MANIFEST_SHA256 = hashlib.sha256(b"v12-coverage-manifest").digest()
_COVERAGE_SHA256 = hashlib.sha256(b"v12-coverage").digest()
_MEMBER_MAPPING = b"v12-member-mapping"
_BASELINE_PROJECTION = b"v12-baseline-projection"
_MEMBER_MAPPING_SHA256 = hashlib.sha256(_MEMBER_MAPPING).digest()
_BASELINE_PROJECTION_SHA256 = hashlib.sha256(_BASELINE_PROJECTION).digest()
_ZERO = b"\0" * 32
_INNER_MANIFEST = b"v12-zero-control-inner-atomic"
_INNER_MANIFEST_SHA256 = hashlib.sha256(_INNER_MANIFEST).digest()
_INNER_RECEIPT_SHA256 = hashlib.sha256(b"v12-inner-receipt").digest()
_BASELINE_OUTER_MANIFEST = b"v12-pre-mount-baseline-outer"
_BASELINE_OUTER_MANIFEST_SHA256 = hashlib.sha256(_BASELINE_OUTER_MANIFEST).digest()
_BASELINE_OUTER_RECEIPT_SHA256 = hashlib.sha256(b"v12-baseline-outer-receipt").digest()
_PIPELINE_INTENTS_SHA256 = hashlib.sha256(b"v12-pipelines").digest()
_CONTROLS_SHA256 = hashlib.sha256(b"v12-controls").digest()
_TARGETS_SHA256 = bytes.fromhex(
    "edc9bee4937fa3cc223a52ca677f0e6825dc6ef503000bfabd4fc24941ca153f"
)

_FROZEN_V1_V11_CHECKSUMS = (
    "390305f196309ed13d84a09428b5dbecb28e300d4222c2f980e3b11902a33a26",
    "e90a68e8c36513213618817e39d398a7944fbb39fc365e4abd946546164fb0fa",
    "b5fc2a8d86f45db33d6771c9bf29e8f6f14fbced22e598340c2ff19cf1d1d590",
    "71cdb236889b5e5194c7e3f9d48c4cb7400941c324dd8c1f5bfd46e1c521adc9",
    "b7d543c2594a1fdc78fe18949432e2baf6df4c447caa57317c6361ba63313eff",
    "41fe4ba13a35bacfa75af648381c0dadc4d256a8872c65a1bbf0138b64252254",
    "d96606b2d6b47ee42e53dd216e7c711e00c877573814c4cc73526004e8aa0656",
    "f6c8a8e1f2aa633a52da52a04e6b58ad11230f5093e4b883e7b63674bb8ad80b",
    "23018e6b59b8d3ad235e3110abd122ba12584cee272cff0614730f4d4be0dba0",
    "1be88f2630f37ca45d1e84ea7547b78ebc9da48385774d7e279b74721af81baa",
    "48de0b2513175f8575a7127e54f684922db4eb687739e0ebbab93a0a524f54cb",
)


def _connection(
    migrations: tuple[schema.Migration, ...] | None = None,
) -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    _apply_migrations(
        connection,
        schema.MIGRATIONS if migrations is None else migrations,
    )
    return connection


def _insert(
    connection: sqlite3.Connection,
    table: str,
    values: Mapping[str, Any],
) -> None:
    columns = tuple(values)
    connection.execute(
        "INSERT INTO "
        + table
        + "("
        + ",".join(columns)
        + ") VALUES ("
        + ",".join("?" for _column in columns)
        + ")",
        tuple(values[column] for column in columns),
    )


def _insert_install_receipt(connection: sqlite3.Connection) -> None:
    _insert(
        connection,
        "transaction_receipts",
        {
            "transaction_id": _INSTALL_TRANSACTION_ID,
            "kind": _INSTALL_KIND,
            "committed_at": _NOW,
        },
    )
    _insert(
        connection,
        "business_cutover_install_receipts",
        {
            "cutover_id": _CUTOVER_ID,
            "transaction_id": _INSTALL_TRANSACTION_ID,
            "receipt_kind": _INSTALL_KIND,
            "committed_at": _NOW,
            "repository_epoch": _EPOCH,
            "runtime_generation": "runtime-v12-schema-test",
            "code_digest": hashlib.sha256(b"v12-code").digest(),
            "member_mapping_sha256": _MEMBER_MAPPING_SHA256,
            "manifest_bytes": _INSTALL_MANIFEST,
            "manifest_sha256": _INSTALL_MANIFEST_SHA256,
        },
    )


def _insert_source_and_coverage_evidence(
    connection: sqlite3.Connection,
    *,
    live_project_count: int,
    deleted_project_count: int,
    source_time: str = _SOURCE_TIME,
    coverage_time: str = _COVERAGE_TIME,
    source_revision: int = 1,
    coverage_revision: int = 1,
    source_runtime_generation: str = "runtime-v12-schema-test",
    coverage_epoch: str = _EPOCH,
    coverage_deleted_project_count: int | None = None,
    include_coverage: bool = True,
) -> None:
    if live_project_count not in (0, 1):
        raise ValueError("schema-v12 test source supports zero or one live project")
    descriptor = None
    if live_project_count == 1:
        descriptor = _insert_import_batch_and_descriptor(connection)

    connection.execute("BEGIN")
    try:
        catalog_overrides: dict[str, Any] = {}
        if descriptor is not None:
            catalog_overrides = {
                "import_id": descriptor["import_id"],
                "project_count": 1,
                "entity_count": descriptor["entity_count"],
                "source_head_bytes": descriptor["source_head_bytes"],
                "row_snapshot_bytes": descriptor["row_snapshot_bytes"],
            }
        catalog = _insert_catalog_seal(connection, **catalog_overrides)
        _insert(
            connection,
            "transaction_receipts",
            {
                "transaction_id": catalog["transaction_id"],
                "kind": catalog["receipt_kind"],
                "committed_at": source_time,
            },
        )
        if descriptor is not None:
            _insert_catalog_member(connection, descriptor)
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise

    _insert(
        connection,
        "legacy_repository_proof_control",
        {
            "singleton": 1,
            "authority": repository_contract.LEGACY_REPOSITORY_AUTHORITY,
            "authority_state": (repository_contract.LEGACY_REPOSITORY_AUTHORITY_STATE),
            "repository_epoch": _EPOCH,
            "repository_revision": source_revision,
            "catalog_revision": source_revision,
            "cutover_id": _CUTOVER_ID,
            "runtime_generation": source_runtime_generation,
            "code_digest": hashlib.sha256(b"v12-code").digest(),
            "cutover_ready": repository_contract.LEGACY_REPOSITORY_CUTOVER_READY,
        },
    )
    _insert(
        connection,
        "transaction_receipts",
        {
            "transaction_id": _SOURCE_TRANSACTION_ID,
            "kind": repository_contract.LEGACY_REPOSITORY_TRANSACTION_KIND,
            "committed_at": source_time,
        },
    )
    _insert(
        connection,
        "legacy_repository_seals",
        {
            "repository_singleton": 1,
            "cutover_id": _CUTOVER_ID,
            "transaction_id": _SOURCE_TRANSACTION_ID,
            "receipt_kind": repository_contract.LEGACY_REPOSITORY_TRANSACTION_KIND,
            "receipt_protocol": (
                repository_contract.LEGACY_REPOSITORY_RECEIPT_PROTOCOL
            ),
            "catalog_id": catalog["catalog_id"],
            "catalog_seal_sha256": catalog["seal_sha256"],
            "proof_protocol": repository_contract.LEGACY_REPOSITORY_PROOF_PROTOCOL,
            "manifest_schema": repository_contract.LEGACY_REPOSITORY_MANIFEST_SCHEMA,
            "status": repository_contract.LEGACY_REPOSITORY_STATUS,
            "selection": repository_contract.LEGACY_REPOSITORY_SELECTION,
            "repository_epoch": _EPOCH,
            "repository_revision": source_revision,
            "catalog_revision": source_revision,
            "runtime_generation": source_runtime_generation,
            "code_digest": hashlib.sha256(b"v12-code").digest(),
            "project_count": live_project_count + deleted_project_count,
            "live_project_count": live_project_count,
            "deleted_project_count": deleted_project_count,
            "source_state_bytes": 2 * live_project_count,
            "manifest_bytes": b"v12-source-proof-manifest",
            "manifest_sha256": _SOURCE_MANIFEST_SHA256,
            "source_repository_sha256": hashlib.sha256(
                b"v12-source-repository"
            ).digest(),
            "catalog_coverage_sha256": hashlib.sha256(b"v12-catalog-coverage").digest(),
            "proof_sha256": _SOURCE_PROOF_SHA256,
            "target_schema_version": (
                repository_contract.LEGACY_REPOSITORY_TARGET_SCHEMA_VERSION
            ),
            "schema_fingerprint": "d" * 64,
            "cutover_ready": repository_contract.LEGACY_REPOSITORY_CUTOVER_READY,
        },
    )

    if not include_coverage:
        return
    coverage_deleted = (
        deleted_project_count
        if coverage_deleted_project_count is None
        else coverage_deleted_project_count
    )
    _insert(
        connection,
        "transaction_receipts",
        {
            "transaction_id": _COVERAGE_TRANSACTION_ID,
            "kind": _COVERAGE_KIND,
            "committed_at": coverage_time,
        },
    )
    _insert(
        connection,
        "business_asset_coverage_receipts",
        {
            "cutover_id": _CUTOVER_ID,
            "transaction_id": _COVERAGE_TRANSACTION_ID,
            "receipt_kind": _COVERAGE_KIND,
            "committed_at": coverage_time,
            "repository_epoch": coverage_epoch,
            "source_admission_revision": coverage_revision,
            "source_admission_sha256": _SOURCE_ADMISSION_SHA256,
            "batch_count": 0,
            "member_root_sha256": hashlib.sha256(b"v12-members").digest(),
            "repository_member_count": (live_project_count + coverage_deleted),
            "live_project_count": live_project_count,
            "deleted_project_count": coverage_deleted,
            "observed_occurrence_count": 0,
            "observed_row_count": 0,
            "observed_manifest_bytes": 0,
            "project_count": live_project_count,
            "reference_count": 0,
            "unique_blob_count": 0,
            "unique_blob_bytes": 0,
            "referenced_bytes": 0,
            "project_root_sha256": hashlib.sha256(b"v12-projects").digest(),
            "blob_root_sha256": hashlib.sha256(b"v12-blobs").digest(),
            "occurrence_root_sha256": hashlib.sha256(b"v12-occurrences").digest(),
            "coverage_sha256": _COVERAGE_SHA256,
            "manifest_bytes": b"v12-coverage-manifest",
            "manifest_sha256": _COVERAGE_MANIFEST_SHA256,
        },
    )


def _artifact_header_values(
    kind: str,
    *,
    byte_length: int,
    chunk_count: int,
    artifact_sha256: bytes,
) -> dict[str, Any]:
    return {
        "cutover_id": _CUTOVER_ID,
        "artifact_kind": kind,
        "artifact_schema": (
            "ze.business-cutover.member-mapping.v1"
            if kind == "member_mapping"
            else "ze.business-cutover-baseline-projection.manifest.v2"
        ),
        "byte_length": byte_length,
        "chunk_count": chunk_count,
        "artifact_sha256": artifact_sha256,
    }


def _insert_artifact_header(
    connection: sqlite3.Connection,
    kind: str,
    *,
    byte_length: int,
    chunk_count: int,
    artifact_sha256: bytes,
) -> None:
    _insert(
        connection,
        "business_cutover_install_artifacts",
        _artifact_header_values(
            kind,
            byte_length=byte_length,
            chunk_count=chunk_count,
            artifact_sha256=artifact_sha256,
        ),
    )


def _insert_artifact_chunk(
    connection: sqlite3.Connection,
    kind: str,
    ordinal: int,
    chunk: bytes,
    *,
    byte_offset: int | None = None,
) -> None:
    _insert(
        connection,
        "business_cutover_install_artifact_chunks",
        {
            "cutover_id": _CUTOVER_ID,
            "artifact_kind": kind,
            "ordinal": ordinal,
            "byte_offset": (
                ordinal * schema.MAX_BUSINESS_CUTOVER_ARTIFACT_CHUNK_BYTES
                if byte_offset is None
                else byte_offset
            ),
            "chunk_bytes": chunk,
            "chunk_sha256": hashlib.sha256(chunk).digest(),
        },
    )


def _insert_artifact(
    connection: sqlite3.Connection,
    kind: str,
    payload: bytes,
) -> bytes:
    digest = hashlib.sha256(payload).digest()
    limit = schema.MAX_BUSINESS_CUTOVER_ARTIFACT_CHUNK_BYTES
    chunk_count = ((len(payload) - 1) // limit) + 1
    _insert_artifact_header(
        connection,
        kind,
        byte_length=len(payload),
        chunk_count=chunk_count,
        artifact_sha256=digest,
    )
    for ordinal in range(chunk_count):
        _insert_artifact_chunk(
            connection,
            kind,
            ordinal,
            payload[ordinal * limit : (ordinal + 1) * limit],
        )
    return digest


def _insert_complete_artifacts(connection: sqlite3.Connection) -> None:
    assert (
        _insert_artifact(
            connection,
            "member_mapping",
            _MEMBER_MAPPING,
        )
        == _MEMBER_MAPPING_SHA256
    )
    assert (
        _insert_artifact(
            connection,
            "baseline_projection",
            _BASELINE_PROJECTION,
        )
        == _BASELINE_PROJECTION_SHA256
    )


def _insert_scheduler_genesis(connection: sqlite3.Connection) -> None:
    _insert(
        connection,
        "worker_ledger_head",
        {
            "singleton": 1,
            "state_revision": 1,
            "sequence": 0,
            "previous_tail_sha256": _ZERO,
            "tail_sha256": _ZERO,
            "updated_at": _NOW,
        },
    )
    _insert(
        connection,
        "outbox_allocation_head",
        {
            "singleton": 1,
            "state_revision": 1,
            "sequence": 0,
            "previous_tail_sha256": _ZERO,
            "tail_sha256": _ZERO,
            "updated_at": _NOW,
        },
    )
    _insert(
        connection,
        "outbox_dispatch_head",
        {
            "singleton": 1,
            "state_revision": 1,
            "published_sequence": 0,
            "published_repository_revision": None,
            "published_ordinal": None,
            "inflight_sequence": None,
            "inflight_repository_revision": None,
            "inflight_ordinal": None,
            "previous_head_sha256": _ZERO,
            "head_sha256": _ZERO,
            "updated_at": _NOW,
        },
    )


def _insert_empty_runtime_and_scheduler_genesis(
    connection: sqlite3.Connection,
) -> None:
    connection.execute(
        "UPDATE kernel_repository SET authority='sqlite' WHERE singleton=1"
    )
    _insert(
        connection,
        "repository_runtime_control",
        {
            "singleton": 1,
            "authority": "sqlite",
            "authority_state": "read_only",
            "repository_epoch": _EPOCH,
            "cutover_id": _CUTOVER_ID,
            "source_admission_revision": 1,
            "repository_revision": 1,
            "state_revision": 1,
            "runtime_generation": "runtime-v12-schema-test",
            "code_digest": hashlib.sha256(b"v12-code").digest(),
            "schema_version": 6,
            "updated_at": _NOW,
        },
    )
    _insert_scheduler_genesis(connection)


def _insert_runtime_and_baseline_commit(
    connection: sqlite3.Connection,
) -> bytes:
    connection.execute(
        "UPDATE kernel_repository SET authority='sqlite' WHERE singleton=1"
    )
    _insert(
        connection,
        "repository_runtime_control",
        {
            "singleton": 1,
            "authority": "sqlite",
            "authority_state": "read_only",
            "repository_epoch": _EPOCH,
            "cutover_id": _CUTOVER_ID,
            "source_admission_revision": 1,
            "repository_revision": 1,
            "state_revision": 1,
            "runtime_generation": "runtime-v12-schema-test",
            "code_digest": hashlib.sha256(b"v12-code").digest(),
            "schema_version": 6,
            "updated_at": _NOW,
        },
    )
    _insert_scheduler_genesis(connection)
    project_snapshot = b"v12-project-snapshot"
    project_snapshot_sha256 = hashlib.sha256(project_snapshot).digest()
    _insert(
        connection,
        "projects",
        {
            "repository_singleton": 1,
            "project_id": "project-v12",
            "generation": 1,
            "codec_id": "ze.business-json",
            "codec_version": 1,
            "snapshot_format_version": 1,
            "snapshot_bytes": project_snapshot,
            "snapshot_sha256": project_snapshot_sha256,
        },
    )
    transaction_id = schema.BUSINESS_TRANSACTION_PREFIX + _COMMIT_ID
    _insert(
        connection,
        "transaction_receipts",
        {
            "transaction_id": transaction_id,
            "kind": schema.BUSINESS_TRANSACTION_KIND,
            "committed_at": _NOW,
        },
    )
    atomic_manifest_sha256 = hashlib.sha256(b"v12-baseline-atomic-manifest").digest()
    _insert(
        connection,
        "business_commits",
        {
            "commit_id": _COMMIT_ID,
            "transaction_id": transaction_id,
            "receipt_kind": schema.BUSINESS_TRANSACTION_KIND,
            "project_id": "project-v12",
            "generation": 1,
            "repository_revision": 1,
            "repository_epoch": _EPOCH,
            "cutover_id": _CUTOVER_ID,
            "source_admission_revision": 1,
            "document_id": "document-v12",
            "document_version": 1,
            "previous_commit_id": None,
            "commit_kind": "baseline",
            "save_policy": "legacy",
            "trace_id": "v12-schema-test",
            "operation_count": 0,
            "operation_target_count": 0,
            "last_operation_id": None,
            "last_operation_ordinal": None,
            "outbox_event_count": 0,
            "last_outbox_event_id": None,
            "last_outbox_ordinal": None,
            "durable_action_count": 0,
            "pipeline_intent_count": 0,
            "before_root_sha256": _ZERO,
            "after_root_sha256": project_snapshot_sha256,
            "previous_operation_tail_sha256": _ZERO,
            "operation_tail_sha256": _ZERO,
            "asset_projection_sha256": hashlib.sha256(b"v12-assets-empty").digest(),
            "asset_reference_count": 0,
            "entity_count": 0,
            "manifest_bytes": b"v12-baseline-business-manifest",
            "manifest_sha256": atomic_manifest_sha256,
            "committed_at": _NOW,
            "project_revision": 1,
            "head_revision": 1,
        },
    )
    _insert(
        connection,
        "repository_projects",
        {
            "repository_singleton": 1,
            "project_id": "project-v12",
            "generation": 1,
            "project_revision": 1,
            "status": "live",
            "current_snapshot_sha256": project_snapshot_sha256,
            "created_at": _NOW,
            "updated_at": _NOW,
            "deleted_at": None,
        },
    )
    _insert(
        connection,
        "atomic_business_write_receipts",
        {
            "commit_id": _COMMIT_ID,
            "transaction_id": transaction_id,
            "receipt_kind": schema.BUSINESS_TRANSACTION_KIND,
            "committed_at": _NOW,
            "atomic_manifest_sha256": atomic_manifest_sha256,
            "pipeline_intents_sha256": _PIPELINE_INTENTS_SHA256,
            "controls_sha256": _CONTROLS_SHA256,
            "control_count": 0,
            "before_worker_sequence": 0,
            "before_worker_tail_sha256": _ZERO,
            "after_worker_sequence": 0,
            "after_worker_tail_sha256": _ZERO,
            "manifest_bytes": _INNER_MANIFEST,
            "manifest_sha256": _INNER_MANIFEST_SHA256,
            "receipt_sha256": _INNER_RECEIPT_SHA256,
            "production_manifest_bytes": _BASELINE_OUTER_MANIFEST,
            "production_manifest_sha256": _BASELINE_OUTER_MANIFEST_SHA256,
            "target_count": 0,
            "targets_sha256": _TARGETS_SHA256,
            "production_receipt_sha256": _BASELINE_OUTER_RECEIPT_SHA256,
        },
    )
    return atomic_manifest_sha256


def _insert_deleted_project(
    connection: sqlite3.Connection,
    *,
    project_id: str,
    occurred_at: str,
) -> None:
    _insert(
        connection,
        "repository_projects",
        {
            "repository_singleton": 1,
            "project_id": project_id,
            "generation": 1,
            "project_revision": 1,
            "status": "deleted",
            "current_snapshot_sha256": None,
            "created_at": occurred_at,
            "updated_at": occurred_at,
            "deleted_at": occurred_at,
        },
    )


def _insert_later_baseline_commit(
    connection: sqlite3.Connection,
    *,
    project_id: str,
    commit_id: str,
    committed_at: str,
    repository_revision: int | None = None,
) -> bytes:
    """Insert a valid post-mount baseline parent without its project row."""

    runtime = connection.execute(
        "SELECT repository_epoch,cutover_id,source_admission_revision,"
        "repository_revision "
        "FROM repository_runtime_control WHERE singleton=1"
    ).fetchone()
    assert runtime is not None
    if repository_revision is None:
        repository_revision = (
            max(
                runtime[3],
                connection.execute(
                    "SELECT COALESCE(max(repository_revision),0) "
                    "FROM business_commits"
                ).fetchone()[0],
            )
            + 1
        )
    snapshot_bytes = ("later-snapshot:" + project_id).encode("utf-8")
    snapshot_sha256 = hashlib.sha256(snapshot_bytes).digest()
    _insert(
        connection,
        "projects",
        {
            "repository_singleton": 1,
            "project_id": project_id,
            "generation": 1,
            "codec_id": "ze.business-json",
            "codec_version": 1,
            "snapshot_format_version": 1,
            "snapshot_bytes": snapshot_bytes,
            "snapshot_sha256": snapshot_sha256,
        },
    )
    transaction_id = schema.BUSINESS_TRANSACTION_PREFIX + commit_id
    _insert(
        connection,
        "transaction_receipts",
        {
            "transaction_id": transaction_id,
            "kind": schema.BUSINESS_TRANSACTION_KIND,
            "committed_at": committed_at,
        },
    )
    manifest_bytes = ("later-baseline:" + project_id).encode("utf-8")
    _insert(
        connection,
        "business_commits",
        {
            "commit_id": commit_id,
            "transaction_id": transaction_id,
            "receipt_kind": schema.BUSINESS_TRANSACTION_KIND,
            "project_id": project_id,
            "generation": 1,
            "repository_revision": repository_revision,
            "repository_epoch": runtime[0],
            "cutover_id": runtime[1],
            "source_admission_revision": runtime[2],
            "document_id": "document-" + project_id,
            "document_version": 1,
            "previous_commit_id": None,
            "commit_kind": "baseline",
            "save_policy": "legacy",
            "trace_id": "later-baseline-test",
            "operation_count": 0,
            "operation_target_count": 0,
            "last_operation_id": None,
            "last_operation_ordinal": None,
            "outbox_event_count": 0,
            "last_outbox_event_id": None,
            "last_outbox_ordinal": None,
            "durable_action_count": 0,
            "pipeline_intent_count": 0,
            "before_root_sha256": _ZERO,
            "after_root_sha256": snapshot_sha256,
            "previous_operation_tail_sha256": _ZERO,
            "operation_tail_sha256": _ZERO,
            "asset_projection_sha256": hashlib.sha256(b"later-assets-empty").digest(),
            "asset_reference_count": 0,
            "entity_count": 0,
            "manifest_bytes": manifest_bytes,
            "manifest_sha256": hashlib.sha256(manifest_bytes).digest(),
            "committed_at": committed_at,
            "project_revision": 1,
            "head_revision": 1,
        },
    )
    return snapshot_sha256


def _baseline_atomic_values(
    atomic_manifest_sha256: bytes,
) -> dict[str, Any]:
    return {
        "cutover_id": _CUTOVER_ID,
        "member_ordinal": 0,
        "repository_revision": 1,
        "baseline_outer_schema": (
            "ze.business-cutover-baseline-atomic-outer.manifest.v1"
        ),
        "baseline_outer_protocol": ("ze.business-cutover-baseline-atomic-outer.v1"),
        "member_mapping_kind": "member_mapping",
        "member_mapping_sha256": _MEMBER_MAPPING_SHA256,
        "commit_id": _COMMIT_ID,
        "transaction_id": schema.BUSINESS_TRANSACTION_PREFIX + _COMMIT_ID,
        "receipt_kind": schema.BUSINESS_TRANSACTION_KIND,
        "committed_at": _NOW,
        "install_transaction_id": _INSTALL_TRANSACTION_ID,
        "install_manifest_sha256": _INSTALL_MANIFEST_SHA256,
        "baseline_projection_kind": "baseline_projection",
        "baseline_projection_sha256": _BASELINE_PROJECTION_SHA256,
        "atomic_manifest_sha256": atomic_manifest_sha256,
        "pipeline_intents_sha256": _PIPELINE_INTENTS_SHA256,
        "controls_sha256": _CONTROLS_SHA256,
        "control_count": 0,
        "before_worker_sequence": 0,
        "before_worker_tail_sha256": _ZERO,
        "after_worker_sequence": 0,
        "after_worker_tail_sha256": _ZERO,
        "manifest_bytes": _INNER_MANIFEST,
        "manifest_sha256": _INNER_MANIFEST_SHA256,
        "receipt_sha256": _INNER_RECEIPT_SHA256,
        "baseline_outer_manifest_bytes": _BASELINE_OUTER_MANIFEST,
        "baseline_outer_manifest_sha256": (_BASELINE_OUTER_MANIFEST_SHA256),
        "target_count": 0,
        "targets_sha256": _TARGETS_SHA256,
        "baseline_outer_receipt_sha256": (_BASELINE_OUTER_RECEIPT_SHA256),
    }


def _insert_baseline_atomic_receipt(
    connection: sqlite3.Connection,
    atomic_manifest_sha256: bytes,
) -> None:
    _insert(
        connection,
        "business_cutover_baseline_atomic_receipts",
        _baseline_atomic_values(atomic_manifest_sha256),
    )


def _closure_values(
    *,
    baseline_commit_count: int,
    live_project_count: int | None = None,
    deleted_project_count: int = 0,
) -> dict[str, Any]:
    live = baseline_commit_count if live_project_count is None else live_project_count
    project_count = live + deleted_project_count
    manifest = b"v12-install-closure"
    return {
        "cutover_id": _CUTOVER_ID,
        "install_transaction_id": _INSTALL_TRANSACTION_ID,
        "install_manifest_sha256": _INSTALL_MANIFEST_SHA256,
        "source_transaction_id": _SOURCE_TRANSACTION_ID,
        "source_receipt_kind": (repository_contract.LEGACY_REPOSITORY_TRANSACTION_KIND),
        "source_admission_revision": 1,
        "source_proof_manifest_sha256": _SOURCE_MANIFEST_SHA256,
        "source_proof_sha256": _SOURCE_PROOF_SHA256,
        "source_admission_sha256": _SOURCE_ADMISSION_SHA256,
        "coverage_transaction_id": _COVERAGE_TRANSACTION_ID,
        "coverage_receipt_kind": _COVERAGE_KIND,
        "coverage_manifest_sha256": _COVERAGE_MANIFEST_SHA256,
        "coverage_sha256": _COVERAGE_SHA256,
        "committed_at": _NOW,
        "member_mapping_kind": "member_mapping",
        "member_mapping_sha256": _MEMBER_MAPPING_SHA256,
        "baseline_projection_kind": "baseline_projection",
        "baseline_projection_sha256": _BASELINE_PROJECTION_SHA256,
        "project_count": project_count,
        "live_project_count": live,
        "deleted_project_count": deleted_project_count,
        "baseline_commit_count": baseline_commit_count,
        "baseline_outer_receipt_root_sha256": hashlib.sha256(
            b"v12-baseline-outer-receipt-root"
        ).digest(),
        "manifest_bytes": manifest,
        "manifest_sha256": hashlib.sha256(manifest).digest(),
    }


def _insert_closure(
    connection: sqlite3.Connection,
    *,
    baseline_commit_count: int,
) -> None:
    _insert(
        connection,
        "business_cutover_install_closures",
        _closure_values(baseline_commit_count=baseline_commit_count),
    )


def _head_values(*, updated_at: str = _NOW) -> dict[str, Any]:
    return {
        "singleton": 1,
        "cutover_id": _CUTOVER_ID,
        "state_revision": 1,
        "phase_revision": 1,
        "phase": "installed",
        "authority_state": "read_only",
        "repository_epoch": _EPOCH,
        "runtime_generation": "runtime-v12-schema-test",
        "code_digest": hashlib.sha256(b"v12-code").digest(),
        "install_transaction_id": _INSTALL_TRANSACTION_ID,
        "install_manifest_sha256": _INSTALL_MANIFEST_SHA256,
        "previous_head_sha256": _ZERO,
        "head_sha256": hashlib.sha256(b"v12-install-head").digest(),
        "updated_at": updated_at,
    }


def _insert_head(connection: sqlite3.Connection) -> None:
    _insert(connection, "business_cutover_head", _head_values())


def _force_test_head_to_mounted(connection: sqlite3.Connection) -> None:
    """Create a mounted boundary while keeping phase tests out of this file."""

    trigger_names = (
        "business_cutover_head_cas_update_guard",
        "business_cutover_head_phase_guard",
        "business_cutover_head_update_binding_guard",
    )
    originals = {
        row[0]: row[1]
        for row in connection.execute(
            "SELECT name,sql FROM sqlite_schema WHERE type='trigger' AND "
            "name IN (?,?,?)",
            trigger_names,
        ).fetchall()
    }
    assert set(originals) == set(trigger_names)
    connection.execute("PRAGMA foreign_keys=OFF")
    try:
        for name in trigger_names:
            connection.execute('DROP TRIGGER "' + name + '"')
        old_head = connection.execute(
            "SELECT head_sha256 FROM business_cutover_head WHERE singleton=1"
        ).fetchone()[0]
        connection.execute(
            "UPDATE business_cutover_head SET "
            "state_revision=5,phase_revision=5,phase='mounted',"
            "authority_state='active',"
            "coverage_transaction_id='_test-coverage',"
            "coverage_manifest_sha256=?,"
            "audit_transaction_id='_test-audit',audit_manifest_sha256=?,"
            "activation_transaction_id='_test-activation',"
            "activation_manifest_sha256=?,"
            "mount_transaction_id='_test-mount',mount_manifest_sha256=?,"
            "previous_head_sha256=?,head_sha256=?,updated_at=? "
            "WHERE singleton=1",
            (
                hashlib.sha256(b"test-coverage").digest(),
                hashlib.sha256(b"test-audit").digest(),
                hashlib.sha256(b"test-activation").digest(),
                hashlib.sha256(b"test-mount").digest(),
                old_head,
                hashlib.sha256(b"test-mounted-head").digest(),
                _MOUNTED,
            ),
        )
    finally:
        for name in trigger_names:
            if (
                connection.execute(
                    "SELECT 1 FROM sqlite_schema WHERE type='trigger' AND name=?",
                    (name,),
                ).fetchone()
                is None
            ):
                connection.execute(originals[name])
        connection.execute("PRAGMA foreign_keys=ON")


def _empty_mounted_runtime_fixture() -> sqlite3.Connection:
    connection = _connection()
    _insert_install_receipt(connection)
    _insert_complete_artifacts(connection)
    _insert_source_and_coverage_evidence(
        connection,
        live_project_count=0,
        deleted_project_count=0,
    )
    _insert_empty_runtime_and_scheduler_genesis(connection)
    _insert_closure(connection, baseline_commit_count=0)
    _insert_head(connection)
    _force_test_head_to_mounted(connection)
    connection.execute(
        "UPDATE repository_runtime_control SET authority_state='active',"
        "state_revision=2,updated_at=? WHERE singleton=1",
        (_MOUNTED,),
    )
    return connection


def _insert_live_project_for_commit(
    connection: sqlite3.Connection,
    *,
    project_id: str,
    snapshot_sha256: bytes,
    committed_at: str,
) -> None:
    _insert(
        connection,
        "repository_projects",
        {
            "repository_singleton": 1,
            "project_id": project_id,
            "generation": 1,
            "project_revision": 1,
            "status": "live",
            "current_snapshot_sha256": snapshot_sha256,
            "created_at": committed_at,
            "updated_at": committed_at,
            "deleted_at": None,
        },
    )


def _insert_document_and_head_for_commit(
    connection: sqlite3.Connection,
    *,
    commit_id: str,
    committed_at: str,
) -> None:
    commit = connection.execute(
        "SELECT project_id,generation,document_id,document_version,"
        "after_root_sha256,operation_tail_sha256,asset_projection_sha256,"
        "asset_reference_count,entity_count,head_revision "
        "FROM business_commits WHERE commit_id=?",
        (commit_id,),
    ).fetchone()
    assert commit is not None
    document_bytes = ("runtime-document:" + commit_id).encode("ascii")
    document_sha256 = hashlib.sha256(document_bytes).digest()
    _insert(
        connection,
        "documents",
        {
            "project_id": commit[0],
            "document_id": commit[2],
            "document_version": commit[3],
            "schema_version": "1",
            "collaboration_mode": "with_save",
            "save_policy": "legacy",
            "codec_id": "ze.business-json",
            "codec_version": 1,
            "snapshot_format_version": 1,
            "snapshot_bytes": document_bytes,
            "snapshot_sha256": document_sha256,
        },
    )
    _insert(
        connection,
        "document_commit_heads",
        {
            "project_id": commit[0],
            "generation": commit[1],
            "document_id": commit[2],
            "document_version": commit[3],
            "commit_id": commit_id,
            "head_revision": commit[9],
            "status": "live",
            "current_snapshot_sha256": document_sha256,
            "current_project_snapshot_sha256": commit[4],
            "business_root_sha256": commit[4],
            "operation_tail_sha256": commit[5],
            "asset_projection_sha256": commit[6],
            "asset_reference_count": commit[7],
            "entity_count": commit[8],
            "updated_at": committed_at,
        },
    )


def _insert_runtime_atomic_receipt(
    connection: sqlite3.Connection,
    *,
    commit_id: str,
    committed_at: str,
) -> None:
    commit = connection.execute(
        "SELECT transaction_id,receipt_kind,manifest_sha256 "
        "FROM business_commits WHERE commit_id=?",
        (commit_id,),
    ).fetchone()
    assert commit is not None
    inner = ("runtime-inner:" + commit_id).encode("ascii")
    outer = ("runtime-outer:" + commit_id).encode("ascii")
    _insert(
        connection,
        "atomic_business_write_receipts",
        {
            "commit_id": commit_id,
            "transaction_id": commit[0],
            "receipt_kind": commit[1],
            "committed_at": committed_at,
            "atomic_manifest_sha256": commit[2],
            "pipeline_intents_sha256": hashlib.sha256(
                b"runtime-pipelines:" + commit_id.encode("ascii")
            ).digest(),
            "controls_sha256": hashlib.sha256(
                b"runtime-controls:" + commit_id.encode("ascii")
            ).digest(),
            "control_count": 0,
            "before_worker_sequence": 0,
            "before_worker_tail_sha256": _ZERO,
            "after_worker_sequence": 0,
            "after_worker_tail_sha256": _ZERO,
            "manifest_bytes": inner,
            "manifest_sha256": hashlib.sha256(inner).digest(),
            "receipt_sha256": hashlib.sha256(
                b"runtime-inner-receipt:" + commit_id.encode("ascii")
            ).digest(),
            "production_manifest_bytes": outer,
            "production_manifest_sha256": hashlib.sha256(outer).digest(),
            "target_count": 0,
            "targets_sha256": _TARGETS_SHA256,
            "production_receipt_sha256": hashlib.sha256(
                b"runtime-outer-receipt:" + commit_id.encode("ascii")
            ).digest(),
        },
    )


def _complete_fixture(
    *,
    with_baseline: bool,
    with_head: bool = True,
) -> sqlite3.Connection:
    connection = _connection()
    _insert_install_receipt(connection)
    _insert_complete_artifacts(connection)
    _insert_source_and_coverage_evidence(
        connection,
        live_project_count=(1 if with_baseline else 0),
        deleted_project_count=0,
    )
    baseline_count = 0
    if with_baseline:
        atomic_manifest = _insert_runtime_and_baseline_commit(connection)
        _insert_baseline_atomic_receipt(connection, atomic_manifest)
        baseline_count = 1
    else:
        _insert_empty_runtime_and_scheduler_genesis(connection)
    _insert_closure(connection, baseline_commit_count=baseline_count)
    if with_head:
        _insert_head(connection)
    return connection


def _ready_baseline_closure_fixture(
    **evidence_overrides: Any,
) -> sqlite3.Connection:
    connection = _connection()
    _insert_install_receipt(connection)
    _insert_complete_artifacts(connection)
    _insert_source_and_coverage_evidence(
        connection,
        live_project_count=1,
        deleted_project_count=0,
        **evidence_overrides,
    )
    atomic_manifest = _insert_runtime_and_baseline_commit(connection)
    _insert_baseline_atomic_receipt(connection, atomic_manifest)
    return connection


def _schema_snapshot(connection: sqlite3.Connection) -> tuple[tuple[Any, ...], ...]:
    return tuple(
        tuple(row)
        for row in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_schema " "ORDER BY type,name"
        ).fetchall()
    )


def test_v12_registry_freezes_v1_v11_and_installs_exact_object_graph() -> None:
    v12_tables = (
        "business_cutover_install_artifacts",
        "business_cutover_install_artifact_chunks",
        "business_cutover_baseline_atomic_receipts",
        "business_cutover_install_closures",
    )
    assert tuple(item.checksum for item in schema.MIGRATIONS[:11]) == (
        _FROZEN_V1_V11_CHECKSUMS
    )
    assert tuple(schema.MIGRATIONS[:11]) == (
        schema.KERNEL_MIGRATION,
        schema.BUSINESS_SNAPSHOT_MIGRATION,
        schema.LEGACY_IMPORT_LEDGER_MIGRATION,
        schema.LEGACY_CATALOG_SEAL_MIGRATION,
        schema.LEGACY_REPOSITORY_PROOF_MIGRATION,
        schema.BUSINESS_TRANSACTION_MIGRATION,
        schema.LOCAL_REVISION_MIGRATION,
        schema.PRODUCTION_AUTHORITY_WORKER_MIGRATION,
        schema.WORKER_PAYLOAD_PROJECTION_MIGRATION,
        schema.RUNTIME_SCHEMA_HARDENING_MIGRATION,
        schema.MOUNT_CHRONOLOGY_MIGRATION,
    )
    assert schema.MIGRATIONS[11] is schema.INSTALL_CLOSURE_MIGRATION
    assert schema.INSTALL_CLOSURE_MIGRATION.name == "cutover_install_closure_v1"
    assert schema.INSTALL_CLOSURE_MIGRATION.checksum == (
        "aa61960d2bd4e10a41d1ef51767bf4f4" "809f5fab26cae5ebef3bfa30fccf35a2"
    )
    assert kernel._expected_schema_fingerprint(12) == (
        "4c265275dcf7d90e4ee77fda3b3a1749" "646e5e55783ce17fdff40e737195b226"
    )
    assert schema._expected_migration_ledger_sha256(12).hex() == (
        "1537102bc6fe371ceef49a58b566dbd6" "f86940eaf838dcfeb263e3df37cbb77e"
    )
    assert tuple(item.version for item in schema.MIGRATIONS[:12]) == tuple(
        range(1, 13)
    )
    assert schema.CUTOVER_INSTALL_CLOSURE_TABLES == v12_tables

    connection = _connection(schema.MIGRATIONS[:12])
    try:
        placeholders = ",".join("?" for _table in v12_tables)
        tables = {
            row["name"]: row["sql"]
            for row in connection.execute(
                "SELECT name,sql FROM sqlite_schema WHERE type='table' "
                "AND name IN (" + placeholders + ")",
                v12_tables,
            ).fetchall()
        }
        assert set(tables) == set(v12_tables)
        assert all(sql.rstrip().endswith("STRICT") for sql in tables.values())
        closure_columns = tuple(
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(business_cutover_install_closures)"
            ).fetchall()
        )
        assert closure_columns[3:13] == (
            "source_transaction_id",
            "source_receipt_kind",
            "source_admission_revision",
            "source_proof_manifest_sha256",
            "source_proof_sha256",
            "source_admission_sha256",
            "coverage_transaction_id",
            "coverage_receipt_kind",
            "coverage_manifest_sha256",
            "coverage_sha256",
        )
        assert closure_columns[13] == "committed_at"

        trigger_names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='trigger' AND ("
                "tbl_name IN (" + placeholders + ") OR name IN (?,?,?,?))",
                (
                    *v12_tables,
                    "business_cutover_head_v12_install_closure_guard",
                    "business_cutover_baseline_commits_closed_guard",
                    "repository_projects_v12_cutover_namespace_insert_guard",
                    "repository_projects_v12_cutover_namespace_update_guard",
                ),
            ).fetchall()
        }
        append_only_triggers = {
            trigger
            for table in v12_tables
            for trigger in (table + "_no_update", table + "_no_delete")
        }
        assert trigger_names == append_only_triggers | {
            "business_cutover_install_artifact_chunks_binding_guard",
            "business_cutover_baseline_atomic_binding_guard",
            "business_cutover_baseline_atomic_receipts_v10_timestamp_insert_guard",
            "business_cutover_install_closure_binding_guard",
            "business_cutover_install_closures_v10_timestamp_insert_guard",
            "business_cutover_install_artifacts_closed_guard",
            "business_cutover_install_artifact_chunks_closed_guard",
            "business_cutover_baseline_atomic_receipts_closed_guard",
            "business_cutover_baseline_commits_closed_guard",
            "business_cutover_head_v12_install_closure_guard",
            "repository_projects_v12_cutover_namespace_insert_guard",
            "repository_projects_v12_cutover_namespace_update_guard",
        }

        frozen_trigger_names = {
            table + "_v12_premount_" + operation + "_guard"
            for table in schema._SCHEMA_V12_PREMOUNT_FROZEN_TABLES
            for operation in ("insert", "update", "delete")
        } | {
            "transaction_receipts_v12_premount_insert_guard",
            "transaction_receipts_v12_premount_update_guard",
            "transaction_receipts_v12_premount_delete_guard",
        }
        actual_frozen_trigger_names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='trigger' AND "
                "name LIKE '%_v12_premount_%_guard'"
            ).fetchall()
        }
        assert actual_frozen_trigger_names == frozen_trigger_names
        singleton_triggers = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='trigger' AND "
                "name IN (?,?)",
                (
                    "transaction_receipts_v12_install_singleton_guard",
                    "business_cutover_install_receipts_v12_singleton_guard",
                ),
            ).fetchall()
        }
        assert singleton_triggers == {
            "transaction_receipts_v12_install_singleton_guard",
            "business_cutover_install_receipts_v12_singleton_guard",
        }

        expected_foreign_keys = {
            "business_cutover_install_artifacts": (
                1,
                {"business_cutover_install_receipts"},
            ),
            "business_cutover_install_artifact_chunks": (
                2,
                {"business_cutover_install_artifacts"},
            ),
            "business_cutover_baseline_atomic_receipts": (
                31,
                {
                    "transaction_receipts",
                    "business_commits",
                    "business_cutover_install_receipts",
                    "business_cutover_install_artifacts",
                    "atomic_business_write_receipts",
                },
            ),
            "business_cutover_install_closures": (
                22,
                {
                    "business_cutover_install_receipts",
                    "business_cutover_install_artifacts",
                    "legacy_repository_seals",
                    "business_asset_coverage_receipts",
                },
            ),
        }
        for table, (expected_rows, expected_parents) in expected_foreign_keys.items():
            rows = connection.execute(
                'PRAGMA foreign_key_list("' + table + '")'
            ).fetchall()
            assert len(rows) == expected_rows
            assert {row["table"] for row in rows} == expected_parents
            assert all(
                row["on_update"] == "RESTRICT" and row["on_delete"] == "RESTRICT"
                for row in rows
            )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        pointer_indexes = {
            row[0]: row[1]
            for row in connection.execute(
                "SELECT name,tbl_name FROM sqlite_schema WHERE type='index' "
                "AND name IN (?,?)",
                (
                    "legacy_repository_seals_v12_closure_pointer",
                    "business_asset_coverage_receipts_v12_closure_pointer",
                ),
            ).fetchall()
        }
        assert pointer_indexes == {
            "legacy_repository_seals_v12_closure_pointer": ("legacy_repository_seals"),
            "business_asset_coverage_receipts_v12_closure_pointer": (
                "business_asset_coverage_receipts"
            ),
        }
        expected_pointer_columns = {
            "legacy_repository_seals_v12_closure_pointer": (
                "cutover_id",
                "transaction_id",
                "receipt_kind",
                "repository_revision",
                "manifest_sha256",
                "proof_sha256",
            ),
            "business_asset_coverage_receipts_v12_closure_pointer": (
                "cutover_id",
                "transaction_id",
                "receipt_kind",
                "source_admission_revision",
                "source_admission_sha256",
                "manifest_sha256",
                "coverage_sha256",
            ),
        }
        for index_name, expected_columns in expected_pointer_columns.items():
            assert (
                tuple(
                    row["name"]
                    for row in connection.execute(
                        'PRAGMA index_info("' + index_name + '")'
                    ).fetchall()
                )
                == expected_columns
            )
            parent_table = pointer_indexes[index_name]
            index_list = connection.execute(
                'PRAGMA index_list("' + parent_table + '")'
            ).fetchall()
            assert (
                next(row["unique"] for row in index_list if row["name"] == index_name)
                == 1
            )
        assert (
            connection.execute(
                "SELECT count(*) FROM sqlite_schema "
                "WHERE name='schema_v12_install_closure_preflight'"
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()


def test_fresh_v12_initializes_with_the_real_kernel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(kernel, "MIGRATIONS", kernel.MIGRATIONS[:12])
    monkeypatch.setattr(
        kernel,
        "PRODUCTION_RUNTIME_TABLES",
        kernel.PRODUCTION_RUNTIME_TABLES[:32],
    )
    database = kernel.SqliteDatabase(tmp_path / "fresh-v12.sqlite3")
    profile = database.initialize()
    assert profile.user_version == 12
    with database._owned_connection_for_test() as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert (
            connection.execute(
                "SELECT count(*) FROM schema_migrations WHERE version=12 AND "
                "name=? AND checksum=?",
                (
                    schema.INSTALL_CLOSURE_MIGRATION.name,
                    schema.INSTALL_CLOSURE_MIGRATION.checksum,
                ),
            ).fetchone()[0]
            == 1
        )


def test_authentic_v11_upgrades_to_v12_with_the_real_kernel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    full_registry = kernel.MIGRATIONS
    v12_registry = full_registry[:12]
    full_runtime_tables = kernel.PRODUCTION_RUNTIME_TABLES
    v11_runtime_tables = (
        *schema.PRODUCTION_RUNTIME_V8_TABLES,
        *schema.WORKER_PROJECTION_SIDECAR_TABLES,
    )
    monkeypatch.setattr(kernel, "MIGRATIONS", full_registry[:11])
    monkeypatch.setattr(
        kernel,
        "PRODUCTION_RUNTIME_TABLES",
        v11_runtime_tables,
    )
    database = kernel.SqliteDatabase(tmp_path / "authentic-v11.sqlite3")
    assert database.initialize().user_version == 11

    monkeypatch.setattr(kernel, "MIGRATIONS", v12_registry)
    monkeypatch.setattr(
        kernel,
        "PRODUCTION_RUNTIME_TABLES",
        full_runtime_tables[:32],
    )
    assert database.initialize().user_version == 12
    with database._owned_connection_for_test() as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert tuple(
            connection.execute(
                "SELECT version,name,checksum FROM schema_migrations "
                "WHERE version=12"
            ).fetchone()
        ) == (
            12,
            schema.INSTALL_CLOSURE_MIGRATION.name,
            schema.INSTALL_CLOSURE_MIGRATION.checksum,
        )


@pytest.mark.parametrize(
    "dirty",
    ("production_row", "nonlegacy_authority", "reserved_receipt"),
)
def test_dirty_v11_rejects_v12_atomically_without_leaving_objects(
    dirty: str,
) -> None:
    connection = _connection(schema.MIGRATIONS[:11])
    try:
        if dirty == "production_row":
            connection.execute(
                "INSERT INTO worker_ledger_head(singleton,state_revision,sequence,"
                "previous_tail_sha256,tail_sha256,updated_at) "
                "VALUES (1,1,0,zeroblob(32),zeroblob(32),?)",
                (_NOW,),
            )
        elif dirty == "nonlegacy_authority":
            connection.execute(
                "UPDATE kernel_repository SET authority='sqlite' WHERE singleton=1"
            )
        else:
            _insert(
                connection,
                "transaction_receipts",
                {
                    "transaction_id": _INSTALL_TRANSACTION_ID,
                    "kind": _INSTALL_KIND,
                    "committed_at": _NOW,
                },
            )

        before_schema = _schema_snapshot(connection)
        before_ledger = tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT version,name,checksum,applied_at FROM schema_migrations "
                "ORDER BY version"
            ).fetchall()
        )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="schema_v12_install_closure_precondition",
        ):
            _apply_migrations(connection, schema.MIGRATIONS[11:])

        assert connection.execute("PRAGMA user_version").fetchone()[0] == 11
        assert _schema_snapshot(connection) == before_schema
        assert (
            tuple(
                tuple(row)
                for row in connection.execute(
                    "SELECT version,name,checksum,applied_at FROM schema_migrations "
                    "ORDER BY version"
                ).fetchall()
            )
            == before_ledger
        )
        assert not any(
            connection.execute(
                "SELECT count(*) FROM sqlite_schema WHERE name=?",
                (name,),
            ).fetchone()[0]
            for name in (
                *schema.CUTOVER_INSTALL_CLOSURE_TABLES,
                "schema_v12_install_closure_preflight",
            )
        )
    finally:
        connection.close()


@pytest.mark.parametrize(
    "source_time",
    ("0" * 24, "2026-07-11T23:59:59.\x000Z"),
)
def test_v11_noncanonical_source_receipt_rejects_v12_atomically(
    source_time: str,
) -> None:
    connection = _connection(schema.MIGRATIONS[:11])
    try:
        _insert_source_and_coverage_evidence(
            connection,
            live_project_count=0,
            deleted_project_count=0,
            source_time=source_time,
            include_coverage=False,
        )
        before_schema = _schema_snapshot(connection)
        before_ledger = tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT version,name,checksum,applied_at FROM schema_migrations "
                "ORDER BY version"
            ).fetchall()
        )

        with pytest.raises(
            sqlite3.IntegrityError,
            match="schema_v12_install_closure_precondition",
        ):
            _apply_migrations(connection, schema.MIGRATIONS[11:])

        assert connection.execute("PRAGMA user_version").fetchone()[0] == 11
        assert _schema_snapshot(connection) == before_schema
        assert (
            tuple(
                tuple(row)
                for row in connection.execute(
                    "SELECT version,name,checksum,applied_at FROM schema_migrations "
                    "ORDER BY version"
                ).fetchall()
            )
            == before_ledger
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM sqlite_schema "
                "WHERE name='transaction_receipts_v12_source_update_guard'"
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()


@pytest.mark.parametrize(
    "payload_size,expected_chunk_lengths",
    (
        (
            schema.MAX_BUSINESS_CUTOVER_ARTIFACT_CHUNK_BYTES,
            (schema.MAX_BUSINESS_CUTOVER_ARTIFACT_CHUNK_BYTES,),
        ),
        (
            schema.MAX_BUSINESS_CUTOVER_ARTIFACT_CHUNK_BYTES + 1,
            (schema.MAX_BUSINESS_CUTOVER_ARTIFACT_CHUNK_BYTES, 1),
        ),
    ),
)
def test_artifact_chunking_accepts_the_exact_16_mib_boundary(
    payload_size: int,
    expected_chunk_lengths: tuple[int, ...],
) -> None:
    connection = _connection()
    try:
        _insert_install_receipt(connection)
        payload = b"p" * payload_size
        _insert_artifact(connection, "member_mapping", payload)
        assert (
            tuple(
                row[0]
                for row in connection.execute(
                    "SELECT length(chunk_bytes) "
                    "FROM business_cutover_install_artifact_chunks "
                    "WHERE cutover_id=? AND artifact_kind='member_mapping' "
                    "ORDER BY ordinal",
                    (_CUTOVER_ID,),
                ).fetchall()
            )
            == expected_chunk_lengths
        )
    finally:
        connection.close()


def test_artifact_header_and_chunks_reject_bad_count_order_offset_and_length() -> None:
    limit = schema.MAX_BUSINESS_CUTOVER_ARTIFACT_CHUNK_BYTES

    connection = _connection()
    try:
        _insert_install_receipt(connection)
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            _insert_artifact_header(
                connection,
                "member_mapping",
                byte_length=limit + 1,
                chunk_count=1,
                artifact_sha256=hashlib.sha256(b"bad-count").digest(),
            )
    finally:
        connection.close()

    connection = _connection()
    try:
        _insert_install_receipt(connection)
        _insert_artifact_header(
            connection,
            "member_mapping",
            byte_length=limit + 1,
            chunk_count=2,
            artifact_sha256=hashlib.sha256(b"out-of-order").digest(),
        )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="production_cutover_artifact_chunk_invalid",
        ):
            _insert_artifact_chunk(
                connection,
                "member_mapping",
                1,
                b"x",
            )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            _insert_artifact_chunk(
                connection,
                "member_mapping",
                0,
                b"x" * limit,
                byte_offset=1,
            )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="production_cutover_artifact_chunk_invalid",
        ):
            _insert_artifact_chunk(
                connection,
                "member_mapping",
                0,
                b"x",
            )
    finally:
        connection.close()


@pytest.mark.parametrize(
    "changed",
    ("commit", "install", "projection"),
)
def test_baseline_atomic_binding_rejects_wrong_commit_install_or_projection(
    changed: str,
) -> None:
    connection = _connection()
    try:
        _insert_install_receipt(connection)
        _insert_complete_artifacts(connection)
        atomic_manifest = _insert_runtime_and_baseline_commit(connection)
        values = _baseline_atomic_values(atomic_manifest)
        if changed == "commit":
            values["commit_id"] = "c" * 32
            values["transaction_id"] = schema.BUSINESS_TRANSACTION_PREFIX + "c" * 32
        elif changed == "install":
            values["install_manifest_sha256"] = b"i" * 32
        else:
            values["baseline_projection_sha256"] = b"p" * 32

        with pytest.raises(
            sqlite3.IntegrityError,
            match="production_cutover_baseline_atomic_binding_invalid",
        ):
            _insert(
                connection,
                "business_cutover_baseline_atomic_receipts",
                values,
            )
    finally:
        connection.close()


@pytest.mark.parametrize(
    "failure",
    ("missing_chunk", "missing_install", "wrong_count", "wrong_project_count"),
)
def test_install_closure_rejects_incomplete_artifacts_receipt_or_count(
    failure: str,
) -> None:
    connection = _connection()
    try:
        _insert_install_receipt(connection)
        if failure == "missing_chunk":
            _insert_artifact_header(
                connection,
                "member_mapping",
                byte_length=len(_MEMBER_MAPPING),
                chunk_count=1,
                artifact_sha256=_MEMBER_MAPPING_SHA256,
            )
            _insert_artifact(
                connection,
                "baseline_projection",
                _BASELINE_PROJECTION,
            )
            values = _closure_values(baseline_commit_count=0)
        else:
            _insert_complete_artifacts(connection)
            values = _closure_values(
                baseline_commit_count=(1 if failure == "wrong_count" else 0),
            )
            if failure == "missing_install":
                values["install_manifest_sha256"] = b"i" * 32
            elif failure == "wrong_project_count":
                values["project_count"] = 999
                values["deleted_project_count"] = 999

        with pytest.raises(
            sqlite3.IntegrityError,
            match="production_cutover_install_closure_invalid",
        ):
            _insert(connection, "business_cutover_install_closures", values)
    finally:
        connection.close()


def test_business_cutover_head_requires_the_exact_install_closure() -> None:
    connection = _connection()
    try:
        _insert_install_receipt(connection)
        with pytest.raises(
            sqlite3.IntegrityError,
            match="production_cutover_install_closure_invalid",
        ):
            _insert(connection, "business_cutover_head", _head_values())
    finally:
        connection.close()


def test_install_closure_requires_every_baseline_commit_sidecar() -> None:
    connection = _connection()
    try:
        _insert_install_receipt(connection)
        _insert_complete_artifacts(connection)
        _insert_source_and_coverage_evidence(
            connection,
            live_project_count=1,
            deleted_project_count=0,
        )
        _insert_runtime_and_baseline_commit(connection)
        with pytest.raises(
            sqlite3.IntegrityError,
            match="production_cutover_install_closure_invalid",
        ):
            _insert_closure(connection, baseline_commit_count=1)
    finally:
        connection.close()


def test_install_closure_requires_coverage_to_exist_before_closing() -> None:
    connection = _ready_baseline_closure_fixture(include_coverage=False)
    try:
        with pytest.raises(
            sqlite3.IntegrityError,
            match="production_cutover_install_closure_invalid",
        ):
            _insert_closure(connection, baseline_commit_count=1)
    finally:
        connection.close()


@pytest.mark.parametrize(
    "failure",
    ("source", "coverage", "epoch", "revision", "count", "time"),
)
def test_install_closure_rejects_mismatched_source_or_coverage_provenance(
    failure: str,
) -> None:
    evidence_overrides: dict[str, Any] = {}
    if failure == "source":
        evidence_overrides["source_runtime_generation"] = "wrong-runtime"
    elif failure == "epoch":
        evidence_overrides["coverage_epoch"] = "d" * 64
    elif failure == "revision":
        evidence_overrides["source_revision"] = 2
    elif failure == "count":
        evidence_overrides["coverage_deleted_project_count"] = 1
    elif failure == "time":
        evidence_overrides["coverage_time"] = _LATER

    connection = _ready_baseline_closure_fixture(**evidence_overrides)
    try:
        values = _closure_values(baseline_commit_count=1)
        if failure == "coverage":
            values["coverage_sha256"] = b"w" * 32
        with pytest.raises(
            sqlite3.IntegrityError,
            match="production_cutover_install_closure_invalid",
        ):
            _insert(connection, "business_cutover_install_closures", values)
    finally:
        connection.close()


@pytest.mark.parametrize(
    "source_time",
    (
        "0" * 24,
        "2026-07-11T23:59:59.\x000Z",
        "2026-07-11T23:59:59.00０Z",
    ),
)
def test_install_closure_rejects_noncanonical_source_receipt_time(
    source_time: str,
) -> None:
    connection = _connection()
    try:
        # Isolate the closure's independent chronology check from the earlier
        # source INSERT guard.  Both layers must reject the malformed carrier.
        connection.execute(
            "DROP TRIGGER transaction_receipts_v12_source_timestamp_insert_guard"
        )
        _insert_install_receipt(connection)
        _insert_complete_artifacts(connection)
        _insert_source_and_coverage_evidence(
            connection,
            live_project_count=1,
            deleted_project_count=0,
            source_time=source_time,
        )
        atomic_manifest = _insert_runtime_and_baseline_commit(connection)
        _insert_baseline_atomic_receipt(connection, atomic_manifest)
        with pytest.raises(
            sqlite3.IntegrityError,
            match="production_cutover_install_closure_invalid",
        ):
            _insert_closure(connection, baseline_commit_count=1)
    finally:
        connection.close()


@pytest.mark.parametrize(
    "source_time",
    ("0" * 24, "2026-07-11T23:59:59.\x000Z", "not-a-time"),
)
def test_first_v12_source_receipt_rejects_noncanonical_time_without_residue(
    source_time: str,
) -> None:
    connection = _connection()
    try:
        with pytest.raises(
            sqlite3.IntegrityError,
            match="production_cutover_source_receipt_invalid",
        ):
            _insert(
                connection,
                "transaction_receipts",
                {
                    "transaction_id": _SOURCE_TRANSACTION_ID,
                    "kind": repository_contract.LEGACY_REPOSITORY_TRANSACTION_KIND,
                    "committed_at": source_time,
                },
            )
        assert (
            connection.execute(
                "SELECT count(*) FROM transaction_receipts " "WHERE transaction_id=?",
                (_SOURCE_TRANSACTION_ID,),
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()


def test_install_closure_accepts_equal_canonical_source_coverage_install_time() -> None:
    connection = _ready_baseline_closure_fixture(
        source_time=_NOW,
        coverage_time=_NOW,
    )
    try:
        _insert_closure(connection, baseline_commit_count=1)
        assert (
            connection.execute(
                "SELECT count(*) FROM business_cutover_install_closures"
            ).fetchone()[0]
            == 1
        )
    finally:
        connection.close()


@pytest.mark.parametrize("closed", (False, True), ids=("before", "after"))
def test_v12_source_receipt_identity_and_time_are_permanently_immutable(
    closed: bool,
) -> None:
    connection = (
        _complete_fixture(with_baseline=True)
        if closed
        else _ready_baseline_closure_fixture()
    )
    try:
        ordinary_id = "ordinary-source-receipt-mutation"
        _insert(
            connection,
            "transaction_receipts",
            {
                "transaction_id": ordinary_id,
                "kind": "ordinary-v12",
                "committed_at": _LATER,
            },
        )
        guarded_mutations = (
            (
                "UPDATE transaction_receipts SET committed_at=? "
                "WHERE transaction_id=?",
                (_SOURCE_TIME, _SOURCE_TRANSACTION_ID),
            ),
            (
                "DELETE FROM transaction_receipts WHERE transaction_id=?",
                (_SOURCE_TRANSACTION_ID,),
            ),
            (
                "UPDATE transaction_receipts "
                "SET kind='_legacy_repository_seal_v1' "
                "WHERE transaction_id=?",
                (ordinary_id,),
            ),
            (
                "UPDATE transaction_receipts SET transaction_id=? "
                "WHERE transaction_id=?",
                ("_legacy-repository-seal:" + "e" * 64, ordinary_id),
            ),
        )
        for statement, parameters in guarded_mutations:
            with pytest.raises(
                sqlite3.IntegrityError,
                match=(
                    "production_(cutover_source_namespace_not_singleton|"
                    "receipt_history_append_only)"
                ),
            ):
                connection.execute(statement, parameters)

        with pytest.raises(
            sqlite3.IntegrityError,
            match="production_cutover_source_namespace_not_singleton",
        ):
            _insert(
                connection,
                "transaction_receipts",
                {
                    "transaction_id": "_legacy-repository-seal:" + "f" * 64,
                    "kind": "ordinary-v12",
                    "committed_at": _LATER,
                },
            )
    finally:
        connection.close()


@pytest.mark.parametrize("pointer", ("source", "coverage"))
def test_install_closure_source_and_coverage_composite_foreign_keys(
    pointer: str,
) -> None:
    connection = _ready_baseline_closure_fixture()
    try:
        connection.execute(
            "DROP TRIGGER business_cutover_install_closure_binding_guard"
        )
        values = _closure_values(baseline_commit_count=1)
        values["source_proof_sha256" if pointer == "source" else "coverage_sha256"] = (
            b"x" * 32
        )
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            _insert(connection, "business_cutover_install_closures", values)
    finally:
        connection.close()


def test_valid_closure_persists_exact_source_and_coverage_provenance() -> None:
    connection = _ready_baseline_closure_fixture()
    try:
        _insert_closure(connection, baseline_commit_count=1)
        row = connection.execute(
            "SELECT source_transaction_id,source_receipt_kind,"
            "source_admission_revision,source_proof_manifest_sha256,"
            "source_proof_sha256,source_admission_sha256,"
            "coverage_transaction_id,coverage_receipt_kind,"
            "coverage_manifest_sha256,coverage_sha256 "
            "FROM business_cutover_install_closures"
        ).fetchone()
        assert tuple(row) == (
            _SOURCE_TRANSACTION_ID,
            repository_contract.LEGACY_REPOSITORY_TRANSACTION_KIND,
            1,
            _SOURCE_MANIFEST_SHA256,
            _SOURCE_PROOF_SHA256,
            _SOURCE_ADMISSION_SHA256,
            _COVERAGE_TRANSACTION_ID,
            _COVERAGE_KIND,
            _COVERAGE_MANIFEST_SHA256,
            _COVERAGE_SHA256,
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_install_closure_rejects_a_commit_outside_the_live_baseline_set() -> None:
    connection = _connection()
    try:
        _insert_install_receipt(connection)
        _insert_complete_artifacts(connection)
        _insert_source_and_coverage_evidence(
            connection,
            live_project_count=1,
            deleted_project_count=0,
        )
        atomic_manifest = _insert_runtime_and_baseline_commit(connection)
        _insert_baseline_atomic_receipt(connection, atomic_manifest)
        _insert_later_baseline_commit(
            connection,
            project_id="unmapped-other-time-baseline",
            commit_id="7" * 32,
            committed_at=_LATER,
            repository_revision=2,
        )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="production_cutover_install_closure_invalid",
        ):
            _insert_closure(connection, baseline_commit_count=1)
    finally:
        connection.close()


def test_install_closure_exactly_closes_live_deleted_and_total_projects() -> None:
    connection = _connection()
    try:
        _insert_install_receipt(connection)
        _insert_complete_artifacts(connection)
        _insert_source_and_coverage_evidence(
            connection,
            live_project_count=1,
            deleted_project_count=1,
        )
        atomic_manifest = _insert_runtime_and_baseline_commit(connection)
        _insert_baseline_atomic_receipt(connection, atomic_manifest)
        _insert_deleted_project(
            connection,
            project_id="project-v12-deleted",
            occurred_at=_NOW,
        )
        _insert(
            connection,
            "business_cutover_install_closures",
            _closure_values(
                baseline_commit_count=1,
                deleted_project_count=1,
            ),
        )
        assert tuple(
            connection.execute(
                "SELECT project_count,live_project_count,"
                "deleted_project_count,baseline_commit_count "
                "FROM business_cutover_install_closures"
            ).fetchone()
        ) == (2, 1, 1, 1)
    finally:
        connection.close()


def test_install_closure_rejects_project_rows_outside_its_time_boundary() -> None:
    connection = _connection()
    try:
        _insert_install_receipt(connection)
        _insert_complete_artifacts(connection)
        _insert_source_and_coverage_evidence(
            connection,
            live_project_count=1,
            deleted_project_count=1,
        )
        atomic_manifest = _insert_runtime_and_baseline_commit(connection)
        _insert_baseline_atomic_receipt(connection, atomic_manifest)
        _insert_deleted_project(
            connection,
            project_id="project-v12-future-deleted",
            occurred_at=_LATER,
        )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="production_cutover_install_closure_invalid",
        ):
            _insert(
                connection,
                "business_cutover_install_closures",
                _closure_values(
                    baseline_commit_count=1,
                    deleted_project_count=1,
                ),
            )
    finally:
        connection.close()


@pytest.mark.parametrize(
    "late_member",
    ("artifact", "chunk", "baseline", "baseline_commit"),
)
def test_install_closure_freezes_every_install_membership_table(
    late_member: str,
) -> None:
    connection = _complete_fixture(with_baseline=False, with_head=False)
    try:
        with pytest.raises(
            sqlite3.IntegrityError,
            match=(
                "production_cutover_premount_namespace_frozen"
                if late_member == "baseline_commit"
                else "production_cutover_install_already_closed"
            ),
        ):
            if late_member == "artifact":
                _insert_artifact_header(
                    connection,
                    "member_mapping",
                    byte_length=1,
                    chunk_count=1,
                    artifact_sha256=hashlib.sha256(b"late").digest(),
                )
            elif late_member == "chunk":
                _insert_artifact_chunk(
                    connection,
                    "member_mapping",
                    1,
                    b"late",
                )
            elif late_member == "baseline":
                _insert(
                    connection,
                    "business_cutover_baseline_atomic_receipts",
                    _baseline_atomic_values(hashlib.sha256(b"late-atomic").digest()),
                )
            else:
                _insert_later_baseline_commit(
                    connection,
                    project_id="late-baseline-v12",
                    commit_id="4" * 32,
                    committed_at=_LATER,
                    repository_revision=2,
                )
    finally:
        connection.close()


def test_closed_unmounted_cutover_freezes_every_materialized_namespace_table() -> None:
    connection = _complete_fixture(with_baseline=True, with_head=False)
    try:
        for table in schema._SCHEMA_V12_PREMOUNT_FROZEN_TABLES:
            with pytest.raises(
                sqlite3.IntegrityError,
                match="production_cutover_premount_namespace_frozen",
            ):
                connection.execute('INSERT INTO "' + table + '" DEFAULT VALUES')

        for statement in (
            "UPDATE projects SET codec_version=codec_version",
            "DELETE FROM projects",
        ):
            with pytest.raises(
                sqlite3.IntegrityError,
                match="production_cutover_premount_namespace_frozen",
            ):
                connection.execute(statement)

        with pytest.raises(
            sqlite3.IntegrityError,
            match="production_cutover_install_already_closed",
        ):
            connection.execute("INSERT INTO business_commits DEFAULT VALUES")
        with pytest.raises(
            sqlite3.IntegrityError,
            match="production_cutover_project_namespace_invalid",
        ):
            connection.execute("INSERT INTO repository_projects DEFAULT VALUES")
    finally:
        connection.close()


def test_mounted_head_does_not_unfreeze_namespace_before_runtime_is_active() -> None:
    connection = _complete_fixture(with_baseline=True, with_head=True)
    try:
        _force_test_head_to_mounted(connection)
        assert (
            connection.execute(
                "SELECT authority_state FROM repository_runtime_control "
                "WHERE singleton=1"
            ).fetchone()[0]
            == "read_only"
        )

        with pytest.raises(
            sqlite3.IntegrityError,
            match="production_cutover_premount_namespace_frozen",
        ):
            connection.execute("UPDATE projects SET codec_version=codec_version+1")

        connection.execute(
            "UPDATE repository_runtime_control SET authority_state='active',"
            "state_revision=2,updated_at=? WHERE singleton=1",
            (_MOUNTED,),
        )
        assert (
            connection.execute(
                "UPDATE projects SET codec_version=codec_version+1"
            ).rowcount
            == 1
        )
    finally:
        connection.close()


def test_closed_unmounted_cutover_allows_only_phase_generic_receipts() -> None:
    connection = _complete_fixture(with_baseline=False, with_head=False)
    try:
        phase_receipts = (
            (
                "_business-cutover-asset-stage:phase-test",
                "_business_cutover_asset_stage_v1",
            ),
            (
                "_business-cutover-asset-coverage:phase-test",
                "_business_cutover_asset_coverage_v1",
            ),
            ("_repository-audit:phase-test", "_repository_audit_certificate_v1"),
            (
                "_business-cutover-activation:phase-test",
                "_business_cutover_activation_v2",
            ),
            (
                "_business-cutover-mount:phase-test",
                "_business_cutover_mount_v2",
            ),
        )
        for transaction_id, kind in phase_receipts:
            _insert(
                connection,
                "transaction_receipts",
                {
                    "transaction_id": transaction_id,
                    "kind": kind,
                    "committed_at": _LATER,
                },
            )
        assert connection.execute(
            "SELECT count(*) FROM transaction_receipts WHERE committed_at=?",
            (_LATER,),
        ).fetchone()[0] == len(phase_receipts)
    finally:
        connection.close()


@pytest.mark.parametrize(
    "state",
    ("staged", "closed", "mounted"),
)
def test_install_namespace_is_global_singleton_in_every_cutover_state(
    state: str,
) -> None:
    if state == "staged":
        connection = _connection()
        _insert_install_receipt(connection)
    elif state == "closed":
        connection = _complete_fixture(with_baseline=False, with_head=False)
    else:
        connection = _empty_mounted_runtime_fixture()
    try:
        second_cutover_id = "c" * 64
        with pytest.raises(
            sqlite3.IntegrityError,
            match="production_cutover_install_namespace_not_singleton",
        ):
            _insert(
                connection,
                "transaction_receipts",
                {
                    "transaction_id": (
                        "_business-cutover-install:" + second_cutover_id
                    ),
                    "kind": "_business_cutover_install_v1",
                    "committed_at": _LATER,
                },
            )

        ordinary_transaction_id = "ordinary-v12-install-namespace-" + state
        _insert(
            connection,
            "transaction_receipts",
            {
                "transaction_id": ordinary_transaction_id,
                "kind": "ordinary-v12",
                "committed_at": _LATER,
            },
        )
        guarded_mutations = (
            (
                "UPDATE transaction_receipts "
                "SET kind='_business_cutover_install_v1' "
                "WHERE transaction_id=?",
                (ordinary_transaction_id,),
            ),
            (
                "UPDATE transaction_receipts "
                "SET transaction_id='_business-cutover-install:" + "d" * 64 + "' "
                "WHERE transaction_id=?",
                (ordinary_transaction_id,),
            ),
            (
                "UPDATE transaction_receipts SET kind=kind " "WHERE transaction_id=?",
                (_INSTALL_TRANSACTION_ID,),
            ),
            (
                "DELETE FROM transaction_receipts WHERE transaction_id=?",
                (_INSTALL_TRANSACTION_ID,),
            ),
        )
        for statement, parameters in guarded_mutations:
            with pytest.raises(
                sqlite3.IntegrityError,
                match=(
                    "production_(cutover_install_namespace_not_singleton|"
                    "receipt_history_append_only)"
                ),
            ):
                connection.execute(statement, parameters)
    finally:
        connection.close()


@pytest.mark.parametrize("with_head", (False, True), ids=("closure", "installed-head"))
def test_closed_install_rejects_late_direct_deleted_projects(
    with_head: bool,
) -> None:
    connection = _complete_fixture(with_baseline=True, with_head=with_head)
    try:
        with pytest.raises(
            sqlite3.IntegrityError,
            match="production_cutover_project_namespace_invalid",
        ):
            _insert_deleted_project(
                connection,
                project_id="late-deleted-v12",
                occurred_at=_LATER,
            )
    finally:
        connection.close()


def test_mounted_head_rejects_backdated_live_project_state() -> None:
    connection = _empty_mounted_runtime_fixture()
    try:
        snapshot_sha256 = _insert_later_baseline_commit(
            connection,
            project_id="backdated-live-v12",
            commit_id="9" * 32,
            committed_at=_LATER,
            repository_revision=2,
        )
        closure_time = connection.execute(
            "SELECT committed_at FROM business_cutover_install_closures"
        ).fetchone()[0]
        with pytest.raises(
            sqlite3.IntegrityError,
            match="production_cutover_project_namespace_invalid",
        ):
            _insert(
                connection,
                "repository_projects",
                {
                    "repository_singleton": 1,
                    "project_id": "backdated-live-v12",
                    "generation": 1,
                    "project_revision": 1,
                    "status": "live",
                    "current_snapshot_sha256": snapshot_sha256,
                    "created_at": closure_time,
                    "updated_at": closure_time,
                    "deleted_at": None,
                },
            )
    finally:
        connection.close()


@pytest.mark.parametrize("repository_revision", (1, 999))
def test_mounted_empty_boundary_rejects_reused_or_future_commit_revision(
    repository_revision: int,
) -> None:
    connection = _empty_mounted_runtime_fixture()
    try:
        with pytest.raises(
            sqlite3.IntegrityError,
            match="production_cutover_install_already_closed",
        ):
            _insert_later_baseline_commit(
                connection,
                project_id="invalid-revision-" + str(repository_revision),
                commit_id=("6" if repository_revision == 1 else "5") * 32,
                committed_at=_LATER,
                repository_revision=repository_revision,
            )
    finally:
        connection.close()


def test_mounted_head_allows_revision_two_baseline_project_and_runtime_cas() -> None:
    connection = _empty_mounted_runtime_fixture()
    commit_id = "8" * 32
    project_id = "later-live-v12"
    try:
        snapshot_sha256 = _insert_later_baseline_commit(
            connection,
            project_id=project_id,
            commit_id=commit_id,
            committed_at=_LATER,
            repository_revision=2,
        )
        _insert_live_project_for_commit(
            connection,
            project_id=project_id,
            snapshot_sha256=snapshot_sha256,
            committed_at=_LATER,
        )
        _insert_document_and_head_for_commit(
            connection,
            commit_id=commit_id,
            committed_at=_LATER,
        )
        _insert_runtime_atomic_receipt(
            connection,
            commit_id=commit_id,
            committed_at=_LATER,
        )
        connection.execute(
            "UPDATE repository_runtime_control SET repository_revision=2,"
            "state_revision=state_revision,updated_at=? WHERE singleton=1",
            (_LATER,),
        )
        row = connection.execute(
            "SELECT project.status,project.created_at,"
            "business_commit.committed_at,"
            "head.updated_at,closure.committed_at "
            "FROM repository_projects AS project "
            "JOIN business_commits AS business_commit "
            "ON business_commit.project_id=project.project_id AND "
            "business_commit.generation=project.generation AND "
            "business_commit.project_revision=project.project_revision "
            "JOIN business_cutover_head AS head ON head.singleton=1 "
            "JOIN business_cutover_install_closures AS closure "
            "ON closure.cutover_id=head.cutover_id "
            "WHERE project.project_id=?",
            (project_id,),
        ).fetchone()
        assert row is not None
        assert row[0] == "live"
        assert row[1] == row[2]
        assert row[1] > row[3]
        assert row[1] > row[4]
        assert (
            connection.execute(
                "SELECT repository_revision FROM repository_runtime_control "
                "WHERE singleton=1"
            ).fetchone()[0]
            == 2
        )
    finally:
        connection.close()


def test_valid_baseline_closure_head_and_foreign_keys_are_complete() -> None:
    connection = _complete_fixture(with_baseline=True)
    try:
        assert (
            connection.execute(
                "SELECT baseline_commit_count FROM business_cutover_install_closures"
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT phase_revision FROM business_cutover_head WHERE singleton=1"
            ).fetchone()[0]
            == 1
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_all_four_v12_history_tables_are_append_only() -> None:
    connection = _complete_fixture(with_baseline=True, with_head=False)
    try:
        identities = {
            "business_cutover_install_artifacts": (
                "cutover_id=? AND artifact_kind='member_mapping'",
                (_CUTOVER_ID,),
            ),
            "business_cutover_install_artifact_chunks": (
                "cutover_id=? AND artifact_kind='member_mapping' AND ordinal=0",
                (_CUTOVER_ID,),
            ),
            "business_cutover_baseline_atomic_receipts": (
                "cutover_id=? AND member_ordinal=0",
                (_CUTOVER_ID,),
            ),
            "business_cutover_install_closures": (
                "cutover_id=?",
                (_CUTOVER_ID,),
            ),
        }
        for table, (predicate, parameters) in identities.items():
            with pytest.raises(
                sqlite3.IntegrityError,
                match="production_history_append_only",
            ):
                connection.execute(
                    "UPDATE " + table + " SET cutover_id=cutover_id WHERE " + predicate,
                    parameters,
                )
            with pytest.raises(
                sqlite3.IntegrityError,
                match="production_history_append_only",
            ):
                connection.execute(
                    "DELETE FROM " + table + " WHERE " + predicate,
                    parameters,
                )
    finally:
        connection.close()


@pytest.mark.parametrize(
    "table,binding_trigger,values",
    (
        (
            "business_cutover_baseline_atomic_receipts",
            "business_cutover_baseline_atomic_binding_guard",
            {
                **_baseline_atomic_values(hashlib.sha256(b"atomic").digest()),
                "committed_at": _INVALID_24,
            },
        ),
        (
            "business_cutover_install_closures",
            "business_cutover_install_closure_binding_guard",
            {
                **_closure_values(baseline_commit_count=0),
                "committed_at": _INVALID_24,
            },
        ),
    ),
)
def test_v12_timestamp_columns_reject_noncanonical_24_byte_values(
    table: str,
    binding_trigger: str,
    values: Mapping[str, Any],
) -> None:
    connection = _connection()
    try:
        # Isolate the dedicated V10-style timestamp guard.  The binding guards
        # are covered independently above and would also reject the altered
        # parent time before SQLite reaches the deferred foreign keys.
        connection.execute('DROP TRIGGER "' + binding_trigger + '"')
        with pytest.raises(
            sqlite3.IntegrityError,
            match="production_timestamp_invalid",
        ):
            _insert(connection, table, values)
    finally:
        connection.close()
