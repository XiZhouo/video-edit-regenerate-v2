"""Immutable SQLite schema registry for the ZE repository database.

This module owns DDL identity only.  It does not open databases, import legacy
projects, encode business snapshots, or decide which storage backend is the
production authority.  Schema migrations stay DDL-only; business data moves
through separately audited importer/adapter transactions.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Optional, Tuple

from migration_ledger_contract import MigrationLedgerEntry
from migration_ledger_integrity import _migration_ledger_sha256

from legacy_catalog_contract import (
    LEGACY_CATALOG_CUTOVER_READY,
    LEGACY_CATALOG_MANIFEST_SCHEMA,
    LEGACY_CATALOG_RECEIPT_PROTOCOL,
    LEGACY_CATALOG_SEAL_PROTOCOL,
    LEGACY_CATALOG_SELECTION,
    LEGACY_CATALOG_STATUS,
    LEGACY_CATALOG_TARGET_SCHEMA_VERSION,
    LEGACY_CATALOG_TRANSACTION_KIND,
    LEGACY_CATALOG_TRANSACTION_PREFIX,
    MAX_LEGACY_CATALOG_MANIFEST_BYTES,
    MAX_LEGACY_CATALOG_PROJECT_SOURCE_HEAD_BYTES,
    MAX_LEGACY_CATALOG_PROJECTS,
    MAX_LEGACY_CATALOG_SOURCE_HEAD_BYTES,
    MAX_LEGACY_CATALOG_TARGET_SNAPSHOT_BYTES,
)
from legacy_import_contract import (
    LEGACY_BATCH_PROTOCOL,
    LEGACY_MANIFEST_SCHEMA,
    LEGACY_PAYLOAD_PROTOCOL,
    MAX_LEGACY_ENTITIES,
    MAX_LEGACY_MANIFEST_BYTES,
    MAX_LEGACY_PROJECTS,
    MAX_LEGACY_ROW_SNAPSHOT_BYTES,
    SQLITE_IMPORT_RECEIPT_PROTOCOL,
    SQLITE_IMPORT_TERMINAL_STATUS,
    SQLITE_IMPORT_TRANSACTION_KIND,
    SQLITE_IMPORT_TRANSACTION_PREFIX,
)
from legacy_repository_contract import (
    LEGACY_REPOSITORY_AUTHORITY,
    LEGACY_REPOSITORY_AUTHORITY_STATE,
    LEGACY_REPOSITORY_CUTOVER_READY,
    LEGACY_REPOSITORY_LEGACY_ADOPT_OPERATION_ID,
    LEGACY_REPOSITORY_MANIFEST_SCHEMA,
    LEGACY_REPOSITORY_MEMBER_DELETED,
    LEGACY_REPOSITORY_MEMBER_LIVE,
    LEGACY_REPOSITORY_PROOF_PROTOCOL,
    LEGACY_REPOSITORY_RECEIPT_PROTOCOL,
    LEGACY_REPOSITORY_SELECTION,
    LEGACY_REPOSITORY_STATUS,
    LEGACY_REPOSITORY_TARGET_SCHEMA_VERSION,
    LEGACY_REPOSITORY_TRANSACTION_KIND,
    LEGACY_REPOSITORY_TRANSACTION_PREFIX,
    MAX_LEGACY_REPOSITORY_MANIFEST_BYTES,
    MAX_LEGACY_REPOSITORY_PROJECTS,
    MAX_LEGACY_REPOSITORY_PROJECT_SOURCE_STATE_BYTES,
    MAX_LEGACY_REPOSITORY_RUNTIME_GENERATION_BYTES,
    MAX_LEGACY_REPOSITORY_SOURCE_STATE_BYTES,
)


LEGACY_CHECKSUM_SCHEME = "legacy-whitespace-v1"
EXACT_CHECKSUM_SCHEME = "exact-utf8-v2"

# Schema-v6 reserves one ordinary-looking identity namespace before the
# production transaction writer exists.  A v5 binary could otherwise mint a
# weak generic receipt that a later business adapter might mistake for an
# exact commit receipt during cutover.
BUSINESS_TRANSACTION_PREFIX = "ze-business:"
BUSINESS_TRANSACTION_KIND = "ze_business_commit_v1"

# Schema-v8 owns these receipt identities.  The v7 generic kernel did not yet
# reserve them, so the migration must reject any lookalike row before creating
# the specialised child tables.  Keep the literals here: schema identity must
# not depend on importing mutable runtime/issuer modules.
_PRODUCTION_RECEIPT_PREFIXES = (
    "_business-cutover-install:",
    "_business-cutover-asset-stage:",
    "_business-cutover-asset-coverage:",
    "_repository-audit:",
    "_business-cutover-activation:",
    "_business-cutover-mount:",
    "_worker-execution:",
    "_pipeline-business-control:",
    BUSINESS_TRANSACTION_PREFIX,
)
_PRODUCTION_RECEIPT_KINDS = (
    "_business_cutover_install_v1",
    "_business_cutover_asset_stage_v1",
    "_business_cutover_asset_coverage_v1",
    "_repository_audit_certificate_v1",
    "_business_cutover_activation_v2",
    "_business_cutover_mount_v2",
    "_worker_execution_v3",
    "_pipeline_business_control_v2",
    BUSINESS_TRANSACTION_KIND,
)

# Unknown/user-extensible business identifiers are exact UTF-8 strings, not a
# narrow ASCII grammar.  The database still needs a finite byte budget so an
# index key cannot become an unbounded resource-exhaustion input.
MAX_BUSINESS_ID_BYTES = 1024
MAX_BUSINESS_RUNTIME_GENERATION_BYTES = 256
MAX_BUSINESS_SMALL_PAYLOAD_BYTES = 16 * 1024 * 1024
# Schema-v12 stores the two potentially large install artifacts in ordered,
# independently hashed chunks.  The complete artifact retains the frozen
# cutover issuer's 64 MiB ceiling while each SQL bind stays within the
# ordinary 16 MiB payload carrier.
MAX_BUSINESS_CUTOVER_ARTIFACT_BYTES = 64 * 1024 * 1024
MAX_BUSINESS_CUTOVER_ARTIFACT_CHUNK_BYTES = MAX_BUSINESS_SMALL_PAYLOAD_BYTES
MAX_BUSINESS_CUTOVER_ARTIFACT_CHUNKS = (
    MAX_BUSINESS_CUTOVER_ARTIFACT_BYTES
    // MAX_BUSINESS_CUTOVER_ARTIFACT_CHUNK_BYTES
)
# Exact upper bound for ``pipeline/<project>/<generation>/<pipeline>``:
# 9-byte prefix + 640-byte project + two separators + 19-digit SQLite integer
# + one 1024-byte pipeline identifier.  Other idempotency scopes retain the
# ordinary one-identifier budget.
MAX_PIPELINE_WORKER_SCOPE_BYTES = 1694


def normalize_schema_sql(value: str) -> str:
    """Normalize transport line endings/outer padding without touching SQL literals."""
    if not isinstance(value, str):
        raise TypeError("migration statement must be a string")
    normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    return normalized.rstrip(";").rstrip()


def legacy_normalize_schema_sql(value: str) -> str:
    """Frozen W06.1 algorithm, retained only for the already-issued v1 checksum."""
    return " ".join(value.strip().rstrip(";").split())


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: Tuple[str, ...]
    checksum_scheme: str = EXACT_CHECKSUM_SCHEME

    def __post_init__(self) -> None:
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 1:
            raise ValueError("migration version must be a positive integer")
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("migration name must be a non-empty string")
        if not isinstance(self.statements, (tuple, list)) or not self.statements:
            raise ValueError("migration statements must be a non-empty sequence")
        if any(not isinstance(statement, str) or not statement.strip() for statement in self.statements):
            raise ValueError("migration statements must be non-empty strings")
        if self.checksum_scheme not in (LEGACY_CHECKSUM_SCHEME, EXACT_CHECKSUM_SCHEME):
            raise ValueError("unknown migration checksum scheme")

    @property
    def checksum(self) -> str:
        if self.checksum_scheme == LEGACY_CHECKSUM_SCHEME:
            # Do not add the scheme field here: this exact payload produced the
            # v1 checksum already persisted by W06.1 databases.
            body = {
                "version": self.version,
                "name": self.name,
                "statements": [legacy_normalize_schema_sql(value) for value in self.statements],
            }
        else:
            body = {
                "version": self.version,
                "name": self.name,
                "checksum_scheme": self.checksum_scheme,
                "statements": [normalize_schema_sql(value) for value in self.statements],
            }
        payload = json.dumps(
            body,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


KERNEL_MIGRATION = Migration(
    version=1,
    name="sqlite_kernel_v1",
    checksum_scheme=LEGACY_CHECKSUM_SCHEME,
    statements=(
        """
        CREATE TABLE schema_migrations (
            version INTEGER PRIMARY KEY CHECK(version >= 1),
            name TEXT NOT NULL,
            checksum TEXT NOT NULL CHECK(length(checksum) = 64),
            applied_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE kernel_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE kernel_repository (
            singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
            authority TEXT NOT NULL CHECK(authority IN ('legacy', 'sqlite')),
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE transaction_receipts (
            transaction_id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            committed_at TEXT NOT NULL
        )
        """,
    ),
)


BUSINESS_SNAPSHOT_MIGRATION = Migration(
    version=2,
    name="business_snapshot_schema_v1",
    statements=(
        """
        CREATE TABLE projects (
            repository_singleton INTEGER NOT NULL DEFAULT 1
                CHECK(repository_singleton = 1),
            project_id TEXT NOT NULL PRIMARY KEY
                CHECK(length(project_id) BETWEEN 1 AND 160),
            generation INTEGER NOT NULL CHECK(generation >= 1),
            codec_id TEXT NOT NULL CHECK(length(codec_id) BETWEEN 1 AND 64),
            codec_version INTEGER NOT NULL CHECK(codec_version >= 1),
            snapshot_format_version INTEGER NOT NULL
                CHECK(snapshot_format_version >= 1),
            snapshot_bytes BLOB NOT NULL
                CHECK(typeof(snapshot_bytes) = 'blob' AND length(snapshot_bytes) > 0),
            snapshot_sha256 BLOB NOT NULL
                CHECK(typeof(snapshot_sha256) = 'blob' AND length(snapshot_sha256) = 32),
            FOREIGN KEY(repository_singleton)
                REFERENCES kernel_repository(singleton)
                ON UPDATE RESTRICT ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE documents (
            project_id TEXT NOT NULL PRIMARY KEY,
            document_id TEXT NOT NULL CHECK(length(document_id) >= 1),
            document_version INTEGER NOT NULL CHECK(document_version >= 1),
            schema_version TEXT NOT NULL CHECK(length(schema_version) >= 1),
            collaboration_mode TEXT NOT NULL CHECK(
                collaboration_mode IN (
                    'live_collab', 'with_save', 'realtime_checkpoint'
                )
            ),
            save_policy TEXT CHECK(
                save_policy IS NULL OR
                save_policy IN ('auto_commit', 'checkpoint_commit', 'legacy')
            ),
            codec_id TEXT NOT NULL CHECK(length(codec_id) BETWEEN 1 AND 64),
            codec_version INTEGER NOT NULL CHECK(codec_version >= 1),
            snapshot_format_version INTEGER NOT NULL
                CHECK(snapshot_format_version >= 1),
            snapshot_bytes BLOB NOT NULL
                CHECK(typeof(snapshot_bytes) = 'blob' AND length(snapshot_bytes) > 0),
            snapshot_sha256 BLOB NOT NULL
                CHECK(typeof(snapshot_sha256) = 'blob' AND length(snapshot_sha256) = 32),
            UNIQUE(document_id),
            UNIQUE(project_id, document_id),
            FOREIGN KEY(project_id)
                REFERENCES projects(project_id)
                ON UPDATE RESTRICT ON DELETE CASCADE
                DEFERRABLE INITIALLY DEFERRED
        ) STRICT
        """,
        """
        CREATE TABLE entities (
            project_id TEXT NOT NULL,
            document_id TEXT NOT NULL,
            entity_type TEXT NOT NULL CHECK(length(entity_type) >= 1),
            entity_id TEXT NOT NULL CHECK(length(entity_id) >= 1),
            entity_version INTEGER NOT NULL CHECK(entity_version >= 1),
            deleted INTEGER NOT NULL CHECK(deleted IN (0, 1)),
            codec_id TEXT NOT NULL CHECK(length(codec_id) BETWEEN 1 AND 64),
            codec_version INTEGER NOT NULL CHECK(codec_version >= 1),
            snapshot_format_version INTEGER NOT NULL
                CHECK(snapshot_format_version >= 1),
            snapshot_bytes BLOB NOT NULL
                CHECK(typeof(snapshot_bytes) = 'blob' AND length(snapshot_bytes) > 0),
            snapshot_sha256 BLOB NOT NULL
                CHECK(typeof(snapshot_sha256) = 'blob' AND length(snapshot_sha256) = 32),
            PRIMARY KEY(project_id, entity_type, entity_id),
            FOREIGN KEY(project_id, document_id)
                REFERENCES documents(project_id, document_id)
                ON UPDATE RESTRICT ON DELETE CASCADE
                DEFERRABLE INITIALLY DEFERRED
        ) STRICT
        """,
    ),
)


LEGACY_IMPORT_LEDGER_MIGRATION = Migration(
    version=3,
    name="legacy_import_ledger_v1",
    statements=(
        """
        CREATE UNIQUE INDEX transaction_receipts_identity_kind
        ON transaction_receipts(transaction_id, kind)
        """,
        f"""
        CREATE TABLE legacy_import_batches (
            import_id TEXT NOT NULL PRIMARY KEY CHECK(
                length(import_id) = 64 AND
                length(CAST(import_id AS BLOB)) = 64 AND
                import_id NOT GLOB '*[^0-9a-f]*'
            ),
            transaction_id TEXT NOT NULL UNIQUE CHECK(
                transaction_id = '{SQLITE_IMPORT_TRANSACTION_PREFIX}' || import_id
            ),
            receipt_kind TEXT NOT NULL CHECK(
                receipt_kind = '{SQLITE_IMPORT_TRANSACTION_KIND}'
            ),
            receipt_protocol TEXT NOT NULL CHECK(
                receipt_protocol = '{SQLITE_IMPORT_RECEIPT_PROTOCOL}'
            ),
            status TEXT NOT NULL CHECK(
                status = '{SQLITE_IMPORT_TERMINAL_STATUS}'
            ),
            manifest_schema TEXT NOT NULL CHECK(
                manifest_schema = '{LEGACY_MANIFEST_SCHEMA}'
            ),
            batch_protocol TEXT NOT NULL CHECK(
                batch_protocol = '{LEGACY_BATCH_PROTOCOL}'
            ),
            payload_protocol TEXT NOT NULL CHECK(
                payload_protocol = '{LEGACY_PAYLOAD_PROTOCOL}'
            ),
            selection TEXT NOT NULL CHECK(selection = 'explicit_projects'),
            project_count INTEGER NOT NULL CHECK(
                project_count BETWEEN 1 AND {MAX_LEGACY_PROJECTS}
            ),
            entity_count INTEGER NOT NULL CHECK(
                entity_count BETWEEN 0 AND {MAX_LEGACY_ENTITIES}
            ),
            row_snapshot_bytes INTEGER NOT NULL CHECK(
                row_snapshot_bytes BETWEEN 1 AND {MAX_LEGACY_ROW_SNAPSHOT_BYTES}
            ),
            framed_payload_bytes INTEGER NOT NULL CHECK(
                framed_payload_bytes > row_snapshot_bytes
            ),
            manifest_bytes BLOB NOT NULL CHECK(
                typeof(manifest_bytes) = 'blob' AND
                length(manifest_bytes) BETWEEN 1 AND {MAX_LEGACY_MANIFEST_BYTES}
            ),
            manifest_sha256 BLOB NOT NULL CHECK(
                typeof(manifest_sha256) = 'blob' AND
                length(manifest_sha256) = 32
            ),
            payload_sha256 BLOB NOT NULL CHECK(
                typeof(payload_sha256) = 'blob' AND
                length(payload_sha256) = 32
            ),
            batch_sha256 BLOB NOT NULL CHECK(
                typeof(batch_sha256) = 'blob' AND
                length(batch_sha256) = 32
            ),
            FOREIGN KEY(transaction_id, receipt_kind)
                REFERENCES transaction_receipts(transaction_id, kind)
                ON UPDATE RESTRICT ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED
        ) STRICT
        """,
        f"""
        CREATE TABLE legacy_import_projects (
            import_id TEXT NOT NULL CHECK(
                length(import_id) = 64 AND
                length(CAST(import_id AS BLOB)) = 64 AND
                import_id NOT GLOB '*[^0-9a-f]*'
            ),
            ordinal INTEGER NOT NULL CHECK(
                ordinal BETWEEN 0 AND {MAX_LEGACY_PROJECTS - 1}
            ),
            project_id TEXT NOT NULL CHECK(
                length(project_id) BETWEEN 1 AND 160
            ),
            generation INTEGER NOT NULL CHECK(generation >= 1),
            document_id TEXT NOT NULL CHECK(length(document_id) >= 1),
            document_version INTEGER NOT NULL CHECK(document_version >= 1),
            storage_version TEXT NOT NULL CHECK(
                storage_version IN ('legacy', 'v2')
            ),
            source_head_sha256 BLOB NOT NULL CHECK(
                typeof(source_head_sha256) = 'blob' AND
                length(source_head_sha256) = 32
            ),
            source_head_bytes INTEGER NOT NULL CHECK(source_head_bytes > 0),
            business_root_sha256 BLOB NOT NULL CHECK(
                typeof(business_root_sha256) = 'blob' AND
                length(business_root_sha256) = 32
            ),
            entity_count INTEGER NOT NULL CHECK(
                entity_count BETWEEN 0 AND {MAX_LEGACY_ENTITIES}
            ),
            row_snapshot_bytes INTEGER NOT NULL CHECK(
                row_snapshot_bytes BETWEEN 1 AND {MAX_LEGACY_ROW_SNAPSHOT_BYTES}
            ),
            PRIMARY KEY(import_id, project_id),
            UNIQUE(import_id, ordinal),
            UNIQUE(import_id, document_id),
            FOREIGN KEY(import_id)
                REFERENCES legacy_import_batches(import_id)
                ON UPDATE RESTRICT ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED
        ) STRICT
        """,
    ),
)


LEGACY_CATALOG_SEAL_MIGRATION = Migration(
    version=4,
    name="legacy_catalog_seal_v1",
    statements=(
        """
        CREATE UNIQUE INDEX legacy_import_batches_catalog_counts
        ON legacy_import_batches(
            import_id, project_count, entity_count, row_snapshot_bytes
        )
        """,
        """
        CREATE UNIQUE INDEX legacy_import_projects_catalog_identity
        ON legacy_import_projects(
            import_id, ordinal, project_id, generation, document_id,
            document_version, storage_version, source_head_sha256,
            source_head_bytes, business_root_sha256, entity_count,
            row_snapshot_bytes
        )
        """,
        f"""
        CREATE TABLE legacy_catalog_seals (
            repository_singleton INTEGER NOT NULL DEFAULT 1
                PRIMARY KEY CHECK(repository_singleton = 1),
            catalog_id TEXT NOT NULL UNIQUE CHECK(
                length(catalog_id) = 64 AND
                length(CAST(catalog_id AS BLOB)) = 64 AND
                catalog_id NOT GLOB '*[^0-9a-f]*'
            ),
            transaction_id TEXT NOT NULL UNIQUE CHECK(
                transaction_id = '{LEGACY_CATALOG_TRANSACTION_PREFIX}' || catalog_id
            ),
            receipt_kind TEXT NOT NULL CHECK(
                receipt_kind = '{LEGACY_CATALOG_TRANSACTION_KIND}'
            ),
            receipt_protocol TEXT NOT NULL CHECK(
                receipt_protocol = '{LEGACY_CATALOG_RECEIPT_PROTOCOL}'
            ),
            import_id TEXT CHECK(
                import_id IS NULL OR (
                    length(import_id) = 64 AND
                    length(CAST(import_id AS BLOB)) = 64 AND
                    import_id NOT GLOB '*[^0-9a-f]*'
                )
            ),
            seal_protocol TEXT NOT NULL CHECK(
                seal_protocol = '{LEGACY_CATALOG_SEAL_PROTOCOL}'
            ),
            manifest_schema TEXT NOT NULL CHECK(
                manifest_schema = '{LEGACY_CATALOG_MANIFEST_SCHEMA}'
            ),
            status TEXT NOT NULL CHECK(
                status = '{LEGACY_CATALOG_STATUS}'
            ),
            selection TEXT NOT NULL CHECK(
                selection = '{LEGACY_CATALOG_SELECTION}'
            ),
            project_count INTEGER NOT NULL CHECK(
                project_count BETWEEN 0 AND {MAX_LEGACY_CATALOG_PROJECTS}
            ),
            entity_count INTEGER NOT NULL CHECK(
                entity_count BETWEEN 0 AND {MAX_LEGACY_ENTITIES}
            ),
            source_head_bytes INTEGER NOT NULL CHECK(
                source_head_bytes BETWEEN 0 AND
                    {MAX_LEGACY_CATALOG_SOURCE_HEAD_BYTES}
            ),
            row_snapshot_bytes INTEGER NOT NULL CHECK(
                row_snapshot_bytes BETWEEN 0 AND
                    {MAX_LEGACY_CATALOG_TARGET_SNAPSHOT_BYTES}
            ),
            manifest_bytes BLOB NOT NULL CHECK(
                typeof(manifest_bytes) = 'blob' AND
                length(manifest_bytes) BETWEEN 1 AND
                    {MAX_LEGACY_CATALOG_MANIFEST_BYTES}
            ),
            manifest_sha256 BLOB NOT NULL CHECK(
                typeof(manifest_sha256) = 'blob' AND
                length(manifest_sha256) = 32
            ),
            source_catalog_sha256 BLOB NOT NULL CHECK(
                typeof(source_catalog_sha256) = 'blob' AND
                length(source_catalog_sha256) = 32
            ),
            target_coverage_sha256 BLOB NOT NULL CHECK(
                typeof(target_coverage_sha256) = 'blob' AND
                length(target_coverage_sha256) = 32
            ),
            seal_sha256 BLOB NOT NULL CHECK(
                typeof(seal_sha256) = 'blob' AND
                length(seal_sha256) = 32
            ),
            target_schema_version INTEGER NOT NULL CHECK(
                target_schema_version >= 1 AND
                target_schema_version =
                    {LEGACY_CATALOG_TARGET_SCHEMA_VERSION}
            ),
            schema_fingerprint TEXT NOT NULL CHECK(
                length(schema_fingerprint) = 64 AND
                length(CAST(schema_fingerprint AS BLOB)) = 64 AND
                schema_fingerprint NOT GLOB '*[^0-9a-f]*'
            ),
            cutover_ready INTEGER NOT NULL DEFAULT
                {LEGACY_CATALOG_CUTOVER_READY} CHECK(
                    cutover_ready = {LEGACY_CATALOG_CUTOVER_READY}
                ),
            UNIQUE(catalog_id, import_id),
            CHECK(
                (
                    project_count = 0 AND import_id IS NULL AND
                    entity_count = 0 AND source_head_bytes = 0 AND
                    row_snapshot_bytes = 0
                ) OR (
                    project_count BETWEEN 1 AND
                        {MAX_LEGACY_CATALOG_PROJECTS} AND
                    import_id IS NOT NULL AND
                    source_head_bytes >= project_count AND
                    row_snapshot_bytes >= project_count
                )
            ),
            FOREIGN KEY(repository_singleton)
                REFERENCES kernel_repository(singleton)
                ON UPDATE RESTRICT ON DELETE RESTRICT,
            FOREIGN KEY(transaction_id, receipt_kind)
                REFERENCES transaction_receipts(transaction_id, kind)
                ON UPDATE RESTRICT ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED,
            FOREIGN KEY(
                import_id, project_count, entity_count, row_snapshot_bytes
            ) REFERENCES legacy_import_batches(
                import_id, project_count, entity_count, row_snapshot_bytes
            ) ON UPDATE RESTRICT ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED
        ) STRICT
        """,
        f"""
        CREATE TABLE legacy_catalog_members (
            catalog_id TEXT NOT NULL CHECK(
                length(catalog_id) = 64 AND
                length(CAST(catalog_id AS BLOB)) = 64 AND
                catalog_id NOT GLOB '*[^0-9a-f]*'
            ),
            ordinal INTEGER NOT NULL CHECK(
                ordinal BETWEEN 0 AND {MAX_LEGACY_CATALOG_PROJECTS - 1}
            ),
            import_id TEXT NOT NULL CHECK(
                length(import_id) = 64 AND
                length(CAST(import_id AS BLOB)) = 64 AND
                import_id NOT GLOB '*[^0-9a-f]*'
            ),
            project_id TEXT NOT NULL CHECK(
                length(project_id) BETWEEN 1 AND 160
            ),
            generation INTEGER NOT NULL CHECK(generation >= 1),
            document_id TEXT NOT NULL CHECK(length(document_id) >= 1),
            document_version INTEGER NOT NULL CHECK(document_version >= 1),
            storage_version TEXT NOT NULL CHECK(
                storage_version IN ('legacy', 'v2')
            ),
            source_head_sha256 BLOB NOT NULL CHECK(
                typeof(source_head_sha256) = 'blob' AND
                length(source_head_sha256) = 32
            ),
            source_head_bytes INTEGER NOT NULL CHECK(
                source_head_bytes BETWEEN 1 AND
                    {MAX_LEGACY_CATALOG_PROJECT_SOURCE_HEAD_BYTES}
            ),
            business_root_sha256 BLOB NOT NULL CHECK(
                typeof(business_root_sha256) = 'blob' AND
                length(business_root_sha256) = 32
            ),
            entity_count INTEGER NOT NULL CHECK(
                entity_count BETWEEN 0 AND {MAX_LEGACY_ENTITIES}
            ),
            row_snapshot_bytes INTEGER NOT NULL CHECK(
                row_snapshot_bytes BETWEEN 1 AND
                    {MAX_LEGACY_CATALOG_TARGET_SNAPSHOT_BYTES}
            ),
            PRIMARY KEY(catalog_id, project_id),
            UNIQUE(catalog_id, ordinal),
            UNIQUE(catalog_id, document_id),
            FOREIGN KEY(catalog_id, import_id)
                REFERENCES legacy_catalog_seals(catalog_id, import_id)
                ON UPDATE RESTRICT ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED,
            FOREIGN KEY(
                import_id, ordinal, project_id, generation, document_id,
                document_version, storage_version, source_head_sha256,
                source_head_bytes, business_root_sha256, entity_count,
                row_snapshot_bytes
            ) REFERENCES legacy_import_projects(
                import_id, ordinal, project_id, generation, document_id,
                document_version, storage_version, source_head_sha256,
                source_head_bytes, business_root_sha256, entity_count,
                row_snapshot_bytes
            ) ON UPDATE RESTRICT ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED
        ) STRICT
        """,
        """
        CREATE TRIGGER legacy_catalog_seals_no_update
        BEFORE UPDATE ON legacy_catalog_seals
        BEGIN
            SELECT RAISE(ABORT, 'legacy_catalog_append_only');
        END
        """,
        """
        CREATE TRIGGER legacy_catalog_seals_no_delete
        BEFORE DELETE ON legacy_catalog_seals
        BEGIN
            SELECT RAISE(ABORT, 'legacy_catalog_append_only');
        END
        """,
        """
        CREATE TRIGGER legacy_catalog_members_no_update
        BEFORE UPDATE ON legacy_catalog_members
        BEGIN
            SELECT RAISE(ABORT, 'legacy_catalog_append_only');
        END
        """,
        """
        CREATE TRIGGER legacy_catalog_members_no_delete
        BEFORE DELETE ON legacy_catalog_members
        BEGIN
            SELECT RAISE(ABORT, 'legacy_catalog_append_only');
        END
        """,
    ),
)


LEGACY_REPOSITORY_PROOF_MIGRATION = Migration(
    version=5,
    name="legacy_repository_proof_v1",
    statements=(
        """
        CREATE UNIQUE INDEX legacy_catalog_seals_repository_identity
        ON legacy_catalog_seals(catalog_id, seal_sha256, project_count)
        """,
        """
        CREATE UNIQUE INDEX legacy_catalog_members_repository_identity
        ON legacy_catalog_members(
            catalog_id, ordinal, import_id, project_id, generation,
            document_id, document_version, storage_version,
            source_head_sha256, source_head_bytes, business_root_sha256,
            entity_count, row_snapshot_bytes
        )
        """,
        f"""
        CREATE TABLE legacy_repository_proof_control (
            singleton INTEGER NOT NULL DEFAULT 1
                PRIMARY KEY CHECK(singleton = 1),
            authority TEXT NOT NULL CHECK(
                authority = '{LEGACY_REPOSITORY_AUTHORITY}'
            ),
            authority_state TEXT NOT NULL CHECK(
                authority_state = '{LEGACY_REPOSITORY_AUTHORITY_STATE}'
            ),
            repository_epoch TEXT NOT NULL CHECK(
                length(repository_epoch) = 64 AND
                length(CAST(repository_epoch AS BLOB)) = 64 AND
                repository_epoch NOT GLOB '*[^0-9a-f]*'
            ),
            repository_revision INTEGER NOT NULL CHECK(
                repository_revision >= 1
            ),
            catalog_revision INTEGER NOT NULL CHECK(
                catalog_revision = repository_revision
            ),
            cutover_id TEXT NOT NULL UNIQUE CHECK(
                length(cutover_id) = 64 AND
                length(CAST(cutover_id AS BLOB)) = 64 AND
                cutover_id NOT GLOB '*[^0-9a-f]*'
            ),
            runtime_generation TEXT NOT NULL CHECK(
                length(CAST(runtime_generation AS BLOB)) BETWEEN 1 AND
                    {MAX_LEGACY_REPOSITORY_RUNTIME_GENERATION_BYTES}
            ),
            code_digest BLOB NOT NULL CHECK(
                typeof(code_digest) = 'blob' AND length(code_digest) = 32
            ),
            cutover_ready INTEGER NOT NULL DEFAULT
                {LEGACY_REPOSITORY_CUTOVER_READY} CHECK(
                    cutover_ready = {LEGACY_REPOSITORY_CUTOVER_READY}
                ),
            UNIQUE(singleton, cutover_id),
            FOREIGN KEY(singleton)
                REFERENCES kernel_repository(singleton)
                ON UPDATE RESTRICT ON DELETE RESTRICT
        ) STRICT
        """,
        f"""
        CREATE TABLE legacy_repository_seals (
            repository_singleton INTEGER NOT NULL DEFAULT 1
                PRIMARY KEY CHECK(repository_singleton = 1),
            cutover_id TEXT NOT NULL UNIQUE CHECK(
                length(cutover_id) = 64 AND
                length(CAST(cutover_id AS BLOB)) = 64 AND
                cutover_id NOT GLOB '*[^0-9a-f]*'
            ),
            transaction_id TEXT NOT NULL UNIQUE CHECK(
                transaction_id =
                    '{LEGACY_REPOSITORY_TRANSACTION_PREFIX}' || cutover_id
            ),
            receipt_kind TEXT NOT NULL CHECK(
                receipt_kind = '{LEGACY_REPOSITORY_TRANSACTION_KIND}'
            ),
            receipt_protocol TEXT NOT NULL CHECK(
                receipt_protocol = '{LEGACY_REPOSITORY_RECEIPT_PROTOCOL}'
            ),
            catalog_id TEXT NOT NULL CHECK(
                length(catalog_id) = 64 AND
                length(CAST(catalog_id AS BLOB)) = 64 AND
                catalog_id NOT GLOB '*[^0-9a-f]*'
            ),
            catalog_seal_sha256 BLOB NOT NULL CHECK(
                typeof(catalog_seal_sha256) = 'blob' AND
                length(catalog_seal_sha256) = 32
            ),
            proof_protocol TEXT NOT NULL CHECK(
                proof_protocol = '{LEGACY_REPOSITORY_PROOF_PROTOCOL}'
            ),
            manifest_schema TEXT NOT NULL CHECK(
                manifest_schema = '{LEGACY_REPOSITORY_MANIFEST_SCHEMA}'
            ),
            status TEXT NOT NULL CHECK(
                status = '{LEGACY_REPOSITORY_STATUS}'
            ),
            selection TEXT NOT NULL CHECK(
                selection = '{LEGACY_REPOSITORY_SELECTION}'
            ),
            repository_epoch TEXT NOT NULL CHECK(
                length(repository_epoch) = 64 AND
                length(CAST(repository_epoch AS BLOB)) = 64 AND
                repository_epoch NOT GLOB '*[^0-9a-f]*'
            ),
            repository_revision INTEGER NOT NULL CHECK(
                repository_revision >= 1
            ),
            catalog_revision INTEGER NOT NULL CHECK(
                catalog_revision = repository_revision
            ),
            runtime_generation TEXT NOT NULL CHECK(
                length(CAST(runtime_generation AS BLOB)) BETWEEN 1 AND
                    {MAX_LEGACY_REPOSITORY_RUNTIME_GENERATION_BYTES}
            ),
            code_digest BLOB NOT NULL CHECK(
                typeof(code_digest) = 'blob' AND length(code_digest) = 32
            ),
            project_count INTEGER NOT NULL CHECK(
                project_count BETWEEN 0 AND {MAX_LEGACY_REPOSITORY_PROJECTS}
            ),
            live_project_count INTEGER NOT NULL CHECK(
                live_project_count BETWEEN 0 AND
                    {MAX_LEGACY_REPOSITORY_PROJECTS}
            ),
            deleted_project_count INTEGER NOT NULL CHECK(
                deleted_project_count BETWEEN 0 AND
                    {MAX_LEGACY_REPOSITORY_PROJECTS}
            ),
            source_state_bytes INTEGER NOT NULL CHECK(
                source_state_bytes BETWEEN 0 AND
                    {MAX_LEGACY_REPOSITORY_SOURCE_STATE_BYTES}
            ),
            manifest_bytes BLOB NOT NULL CHECK(
                typeof(manifest_bytes) = 'blob' AND
                length(manifest_bytes) BETWEEN 1 AND
                    {MAX_LEGACY_REPOSITORY_MANIFEST_BYTES}
            ),
            manifest_sha256 BLOB NOT NULL CHECK(
                typeof(manifest_sha256) = 'blob' AND
                length(manifest_sha256) = 32
            ),
            source_repository_sha256 BLOB NOT NULL CHECK(
                typeof(source_repository_sha256) = 'blob' AND
                length(source_repository_sha256) = 32
            ),
            catalog_coverage_sha256 BLOB NOT NULL CHECK(
                typeof(catalog_coverage_sha256) = 'blob' AND
                length(catalog_coverage_sha256) = 32
            ),
            proof_sha256 BLOB NOT NULL CHECK(
                typeof(proof_sha256) = 'blob' AND length(proof_sha256) = 32
            ),
            target_schema_version INTEGER NOT NULL CHECK(
                target_schema_version =
                    {LEGACY_REPOSITORY_TARGET_SCHEMA_VERSION}
            ),
            schema_fingerprint TEXT NOT NULL CHECK(
                length(schema_fingerprint) = 64 AND
                length(CAST(schema_fingerprint AS BLOB)) = 64 AND
                schema_fingerprint NOT GLOB '*[^0-9a-f]*'
            ),
            cutover_ready INTEGER NOT NULL DEFAULT
                {LEGACY_REPOSITORY_CUTOVER_READY} CHECK(
                    cutover_ready = {LEGACY_REPOSITORY_CUTOVER_READY}
                ),
            CHECK(project_count = live_project_count + deleted_project_count),
            UNIQUE(cutover_id, catalog_id),
            FOREIGN KEY(repository_singleton, cutover_id)
                REFERENCES legacy_repository_proof_control(singleton, cutover_id)
                ON UPDATE RESTRICT ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED,
            FOREIGN KEY(transaction_id, receipt_kind)
                REFERENCES transaction_receipts(transaction_id, kind)
                ON UPDATE RESTRICT ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED,
            FOREIGN KEY(catalog_id, catalog_seal_sha256, live_project_count)
                REFERENCES legacy_catalog_seals(
                    catalog_id, seal_sha256, project_count
                ) ON UPDATE RESTRICT ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED
        ) STRICT
        """,
        f"""
        CREATE TABLE legacy_repository_members (
            cutover_id TEXT NOT NULL CHECK(
                length(cutover_id) = 64 AND
                length(CAST(cutover_id AS BLOB)) = 64 AND
                cutover_id NOT GLOB '*[^0-9a-f]*'
            ),
            ordinal INTEGER NOT NULL CHECK(
                ordinal BETWEEN 0 AND {MAX_LEGACY_REPOSITORY_PROJECTS - 1}
            ),
            project_id TEXT NOT NULL CHECK(
                length(project_id) BETWEEN 1 AND 160
            ),
            generation INTEGER NOT NULL CHECK(generation >= 1),
            status TEXT NOT NULL CHECK(
                status IN (
                    '{LEGACY_REPOSITORY_MEMBER_LIVE}',
                    '{LEGACY_REPOSITORY_MEMBER_DELETED}'
                )
            ),
            operation_id TEXT NOT NULL CHECK(
                operation_id =
                    '{LEGACY_REPOSITORY_LEGACY_ADOPT_OPERATION_ID}' OR (
                    length(operation_id) = 32 AND
                    length(CAST(operation_id AS BLOB)) = 32 AND
                    operation_id NOT GLOB '*[^0-9a-f]*'
                )
            ),
            source_state_sha256 BLOB NOT NULL CHECK(
                typeof(source_state_sha256) = 'blob' AND
                length(source_state_sha256) = 32
            ),
            source_state_bytes INTEGER NOT NULL CHECK(
                source_state_bytes BETWEEN 0 AND
                    {MAX_LEGACY_REPOSITORY_PROJECT_SOURCE_STATE_BYTES}
            ),
            catalog_id TEXT,
            catalog_ordinal INTEGER,
            import_id TEXT,
            document_id TEXT,
            document_version INTEGER,
            storage_version TEXT,
            business_root_sha256 BLOB,
            entity_count INTEGER,
            row_snapshot_bytes INTEGER,
            PRIMARY KEY(cutover_id, project_id),
            UNIQUE(cutover_id, ordinal),
            CHECK(
                (
                    status = '{LEGACY_REPOSITORY_MEMBER_DELETED}' AND
                    operation_id !=
                        '{LEGACY_REPOSITORY_LEGACY_ADOPT_OPERATION_ID}' AND
                    source_state_bytes = 0 AND
                    hex(source_state_sha256) =
                        'E3B0C44298FC1C149AFBF4C8996FB92427AE41E4649B934CA495991B7852B855' AND
                    catalog_id IS NULL AND catalog_ordinal IS NULL AND
                    import_id IS NULL AND document_id IS NULL AND
                    document_version IS NULL AND storage_version IS NULL AND
                    business_root_sha256 IS NULL AND entity_count IS NULL AND
                    row_snapshot_bytes IS NULL
                ) OR (
                    status = '{LEGACY_REPOSITORY_MEMBER_LIVE}' AND
                    (
                        operation_id !=
                            '{LEGACY_REPOSITORY_LEGACY_ADOPT_OPERATION_ID}' OR
                        generation = 1
                    ) AND
                    source_state_bytes BETWEEN 1 AND
                        {MAX_LEGACY_REPOSITORY_PROJECT_SOURCE_STATE_BYTES} AND
                    catalog_id IS NOT NULL AND catalog_ordinal IS NOT NULL AND
                    import_id IS NOT NULL AND document_id IS NOT NULL AND
                    document_version IS NOT NULL AND storage_version IS NOT NULL AND
                    business_root_sha256 IS NOT NULL AND entity_count IS NOT NULL AND
                    row_snapshot_bytes IS NOT NULL
                )
            ),
            FOREIGN KEY(cutover_id)
                REFERENCES legacy_repository_seals(cutover_id)
                ON UPDATE RESTRICT ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED,
            FOREIGN KEY(
                catalog_id, catalog_ordinal, import_id, project_id,
                generation, document_id, document_version, storage_version,
                source_state_sha256, source_state_bytes,
                business_root_sha256, entity_count, row_snapshot_bytes
            ) REFERENCES legacy_catalog_members(
                catalog_id, ordinal, import_id, project_id, generation,
                document_id, document_version, storage_version,
                source_head_sha256, source_head_bytes,
                business_root_sha256, entity_count, row_snapshot_bytes
            ) ON UPDATE RESTRICT ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED
        ) STRICT
        """,
        """
        CREATE TRIGGER legacy_repository_proof_control_no_update
        BEFORE UPDATE ON legacy_repository_proof_control
        BEGIN
            SELECT RAISE(ABORT, 'legacy_repository_append_only');
        END
        """,
        """
        CREATE TRIGGER legacy_repository_proof_control_no_delete
        BEFORE DELETE ON legacy_repository_proof_control
        BEGIN
            SELECT RAISE(ABORT, 'legacy_repository_append_only');
        END
        """,
        """
        CREATE TRIGGER legacy_repository_seals_no_update
        BEFORE UPDATE ON legacy_repository_seals
        BEGIN
            SELECT RAISE(ABORT, 'legacy_repository_append_only');
        END
        """,
        """
        CREATE TRIGGER legacy_repository_seals_no_delete
        BEFORE DELETE ON legacy_repository_seals
        BEGIN
            SELECT RAISE(ABORT, 'legacy_repository_append_only');
        END
        """,
        """
        CREATE TRIGGER legacy_repository_members_no_update
        BEFORE UPDATE ON legacy_repository_members
        BEGIN
            SELECT RAISE(ABORT, 'legacy_repository_append_only');
        END
        """,
        """
        CREATE TRIGGER legacy_repository_members_no_delete
        BEFORE DELETE ON legacy_repository_members
        BEGIN
            SELECT RAISE(ABORT, 'legacy_repository_append_only');
        END
        """,
    ),
)


BUSINESS_TRANSACTION_MIGRATION = Migration(
    version=6,
    name="business_transaction_substrate_v1",
    statements=(
        """
        CREATE UNIQUE INDEX projects_transaction_identity
        ON projects(project_id, generation, snapshot_sha256)
        """,
        """
        CREATE UNIQUE INDEX documents_transaction_identity
        ON documents(
            project_id, document_id, document_version, snapshot_sha256
        )
        """,
        """
        CREATE UNIQUE INDEX kernel_repository_transaction_authority
        ON kernel_repository(singleton, authority)
        """,
        f"""
        CREATE TABLE repository_runtime_control (
            singleton INTEGER NOT NULL DEFAULT 1
                PRIMARY KEY CHECK(singleton = 1),
            authority TEXT NOT NULL CHECK(authority = 'sqlite'),
            authority_state TEXT NOT NULL CHECK(
                authority_state IN ('active', 'read_only', 'repair_required')
            ),
            repository_epoch TEXT NOT NULL CHECK(
                length(repository_epoch) = 64 AND
                length(CAST(repository_epoch AS BLOB)) = 64 AND
                repository_epoch NOT GLOB '*[^0-9a-f]*'
            ),
            cutover_id TEXT NOT NULL CHECK(
                length(cutover_id) = 64 AND
                length(CAST(cutover_id AS BLOB)) = 64 AND
                cutover_id NOT GLOB '*[^0-9a-f]*'
            ),
            source_admission_revision INTEGER NOT NULL CHECK(
                source_admission_revision BETWEEN 1 AND 9223372036854775807
            ),
            repository_revision INTEGER NOT NULL CHECK(
                repository_revision BETWEEN 1 AND 9223372036854775807
            ),
            state_revision INTEGER NOT NULL CHECK(
                state_revision BETWEEN 1 AND 9223372036854775807
            ),
            runtime_generation TEXT NOT NULL CHECK(
                length(CAST(runtime_generation AS BLOB)) BETWEEN 1 AND
                    {MAX_BUSINESS_RUNTIME_GENERATION_BYTES}
            ),
            code_digest BLOB NOT NULL CHECK(
                typeof(code_digest) = 'blob' AND length(code_digest) = 32
            ),
            schema_version INTEGER NOT NULL CHECK(schema_version = 6),
            updated_at TEXT NOT NULL CHECK(
                length(CAST(updated_at AS BLOB)) = 24 AND
                strftime('%Y-%m-%dT%H:%M:%fZ', updated_at) IS NOT NULL AND
                strftime('%Y-%m-%dT%H:%M:%fZ', updated_at) = updated_at
            ),
            UNIQUE(repository_epoch),
            UNIQUE(cutover_id),
            UNIQUE(repository_epoch, cutover_id),
            UNIQUE(
                repository_epoch, cutover_id, source_admission_revision
            ),
            FOREIGN KEY(singleton, authority)
                REFERENCES kernel_repository(singleton, authority)
                ON UPDATE RESTRICT ON DELETE RESTRICT
        ) STRICT
        """,
        f"""
        CREATE TABLE repository_projects (
            repository_singleton INTEGER NOT NULL DEFAULT 1
                CHECK(repository_singleton = 1),
            project_id TEXT NOT NULL PRIMARY KEY CHECK(
                length(project_id) BETWEEN 1 AND 160 AND
                length(CAST(project_id AS BLOB)) BETWEEN 1 AND 640
            ),
            generation INTEGER NOT NULL CHECK(
                generation BETWEEN 1 AND 9223372036854775807
            ),
            project_revision INTEGER NOT NULL CHECK(
                project_revision BETWEEN 1 AND 9223372036854775807
            ),
            status TEXT NOT NULL CHECK(
                status IN ('live', 'deleted', 'repair_required')
            ),
            current_snapshot_sha256 BLOB CHECK(
                current_snapshot_sha256 IS NULL OR (
                    typeof(current_snapshot_sha256) = 'blob' AND
                    length(current_snapshot_sha256) = 32
                )
            ),
            created_at TEXT NOT NULL CHECK(
                length(CAST(created_at AS BLOB)) = 24 AND
                strftime('%Y-%m-%dT%H:%M:%fZ', created_at) IS NOT NULL AND
                strftime('%Y-%m-%dT%H:%M:%fZ', created_at) = created_at
            ),
            updated_at TEXT NOT NULL CHECK(
                length(CAST(updated_at AS BLOB)) = 24 AND
                strftime('%Y-%m-%dT%H:%M:%fZ', updated_at) IS NOT NULL AND
                strftime('%Y-%m-%dT%H:%M:%fZ', updated_at) = updated_at
            ),
            deleted_at TEXT CHECK(
                deleted_at IS NULL OR
                length(CAST(deleted_at AS BLOB)) = 24 AND
                strftime('%Y-%m-%dT%H:%M:%fZ', deleted_at) IS NOT NULL AND
                strftime('%Y-%m-%dT%H:%M:%fZ', deleted_at) = deleted_at
            ),
            UNIQUE(project_id, generation),
            UNIQUE(project_id, generation, current_snapshot_sha256),
            CHECK(
                (status = 'live' AND current_snapshot_sha256 IS NOT NULL AND
                    deleted_at IS NULL) OR
                (status = 'deleted' AND current_snapshot_sha256 IS NULL AND
                    deleted_at IS NOT NULL) OR
                (status = 'repair_required' AND
                    current_snapshot_sha256 IS NULL AND deleted_at IS NULL)
            ),
            FOREIGN KEY(repository_singleton)
                REFERENCES repository_runtime_control(singleton)
                ON UPDATE RESTRICT ON DELETE RESTRICT,
            FOREIGN KEY(project_id, generation, current_snapshot_sha256)
                REFERENCES projects(project_id, generation, snapshot_sha256)
                ON UPDATE NO ACTION ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED
        ) STRICT
        """,
        f"""
        CREATE TABLE business_commits (
            commit_id TEXT NOT NULL PRIMARY KEY CHECK(
                length(commit_id) = 32 AND
                length(CAST(commit_id AS BLOB)) = 32 AND
                commit_id NOT GLOB '*[^0-9a-f]*'
            ),
            transaction_id TEXT NOT NULL UNIQUE CHECK(
                transaction_id = '{BUSINESS_TRANSACTION_PREFIX}' || commit_id
            ),
            receipt_kind TEXT NOT NULL CHECK(
                receipt_kind = '{BUSINESS_TRANSACTION_KIND}'
            ),
            project_id TEXT NOT NULL CHECK(
                length(project_id) BETWEEN 1 AND 160 AND
                length(CAST(project_id AS BLOB)) BETWEEN 1 AND 640
            ),
            generation INTEGER NOT NULL CHECK(
                generation BETWEEN 1 AND 9223372036854775807
            ),
            repository_revision INTEGER NOT NULL UNIQUE CHECK(
                repository_revision BETWEEN 1 AND 9223372036854775807
            ),
            repository_epoch TEXT NOT NULL CHECK(
                length(repository_epoch) = 64 AND
                length(CAST(repository_epoch AS BLOB)) = 64 AND
                repository_epoch NOT GLOB '*[^0-9a-f]*'
            ),
            cutover_id TEXT NOT NULL CHECK(
                length(cutover_id) = 64 AND
                length(CAST(cutover_id AS BLOB)) = 64 AND
                cutover_id NOT GLOB '*[^0-9a-f]*'
            ),
            source_admission_revision INTEGER NOT NULL CHECK(
                source_admission_revision BETWEEN 1 AND 9223372036854775807
            ),
            document_id TEXT NOT NULL CHECK(
                length(CAST(document_id AS BLOB)) BETWEEN 1 AND
                    {MAX_BUSINESS_ID_BYTES}
            ),
            document_version INTEGER NOT NULL CHECK(
                document_version BETWEEN 1 AND 9223372036854775807
            ),
            previous_commit_id TEXT,
            commit_kind TEXT NOT NULL CHECK(
                commit_kind IN ('baseline', 'change')
            ),
            save_policy TEXT NOT NULL CHECK(
                save_policy IN ('auto_commit', 'checkpoint_commit', 'legacy')
            ),
            trace_id TEXT NOT NULL CHECK(
                length(CAST(trace_id AS BLOB)) BETWEEN 1 AND
                    {MAX_BUSINESS_ID_BYTES}
            ),
            operation_count INTEGER NOT NULL CHECK(
                operation_count BETWEEN 0 AND 10000
            ),
            operation_target_count INTEGER NOT NULL CHECK(
                operation_target_count BETWEEN 0 AND 1000000
            ),
            last_operation_id TEXT CHECK(
                last_operation_id IS NULL OR (
                    length(last_operation_id) = 32 AND
                    length(CAST(last_operation_id AS BLOB)) = 32 AND
                    last_operation_id NOT GLOB '*[^0-9a-f]*'
                )
            ),
            last_operation_ordinal INTEGER CHECK(
                last_operation_ordinal IS NULL OR
                last_operation_ordinal BETWEEN 0 AND 9999
            ),
            outbox_event_count INTEGER NOT NULL CHECK(
                outbox_event_count BETWEEN 0 AND 10000
            ),
            last_outbox_event_id TEXT CHECK(
                last_outbox_event_id IS NULL OR (
                    length(last_outbox_event_id) = 32 AND
                    length(CAST(last_outbox_event_id AS BLOB)) = 32 AND
                    last_outbox_event_id NOT GLOB '*[^0-9a-f]*'
                )
            ),
            last_outbox_ordinal INTEGER CHECK(
                last_outbox_ordinal IS NULL OR
                last_outbox_ordinal BETWEEN 0 AND 9999
            ),
            durable_action_count INTEGER NOT NULL CHECK(
                durable_action_count BETWEEN 0 AND 10000
            ),
            pipeline_intent_count INTEGER NOT NULL CHECK(
                pipeline_intent_count BETWEEN 0 AND 10000
            ),
            before_root_sha256 BLOB NOT NULL CHECK(
                typeof(before_root_sha256) = 'blob' AND
                length(before_root_sha256) = 32
            ),
            after_root_sha256 BLOB NOT NULL CHECK(
                typeof(after_root_sha256) = 'blob' AND
                length(after_root_sha256) = 32
            ),
            previous_operation_tail_sha256 BLOB NOT NULL CHECK(
                typeof(previous_operation_tail_sha256) = 'blob' AND
                length(previous_operation_tail_sha256) = 32
            ),
            operation_tail_sha256 BLOB NOT NULL CHECK(
                typeof(operation_tail_sha256) = 'blob' AND
                length(operation_tail_sha256) = 32
            ),
            asset_projection_sha256 BLOB NOT NULL CHECK(
                typeof(asset_projection_sha256) = 'blob' AND
                length(asset_projection_sha256) = 32
            ),
            asset_reference_count INTEGER NOT NULL CHECK(
                asset_reference_count BETWEEN 0 AND 1000000
            ),
            entity_count INTEGER NOT NULL CHECK(
                entity_count BETWEEN 0 AND 1000000
            ),
            manifest_bytes BLOB NOT NULL CHECK(
                typeof(manifest_bytes) = 'blob' AND
                length(manifest_bytes) BETWEEN 1 AND
                    {MAX_BUSINESS_SMALL_PAYLOAD_BYTES}
            ),
            manifest_sha256 BLOB NOT NULL CHECK(
                typeof(manifest_sha256) = 'blob' AND
                length(manifest_sha256) = 32
            ),
            committed_at TEXT NOT NULL CHECK(
                length(CAST(committed_at AS BLOB)) = 24 AND
                strftime('%Y-%m-%dT%H:%M:%fZ', committed_at) IS NOT NULL AND
                strftime('%Y-%m-%dT%H:%M:%fZ', committed_at) = committed_at
            ),
            UNIQUE(commit_id, project_id, generation, document_id),
            UNIQUE(commit_id, repository_revision),
            UNIQUE(commit_id, operation_count, commit_kind),
            UNIQUE(commit_id, outbox_event_count),
            UNIQUE(commit_id, durable_action_count),
            UNIQUE(commit_id, pipeline_intent_count),
            UNIQUE(commit_id, asset_reference_count),
            UNIQUE(commit_id, project_id, generation, document_id, document_version),
            UNIQUE(
                commit_id, project_id, generation, document_id,
                document_version, after_root_sha256, operation_tail_sha256
            ),
            UNIQUE(
                commit_id, project_id, generation, document_id,
                after_root_sha256, operation_tail_sha256
            ),
            UNIQUE(
                commit_id, project_id, generation, document_id,
                document_version, repository_revision, after_root_sha256,
                operation_tail_sha256, asset_projection_sha256,
                asset_reference_count, entity_count
            ),
            UNIQUE(project_id, generation, document_id, document_version),
            CHECK(
                (commit_kind = 'baseline' AND previous_commit_id IS NULL) OR
                (commit_kind = 'change' AND previous_commit_id IS NOT NULL)
            ),
            CHECK(
                (commit_kind = 'baseline' AND operation_count = 0) OR
                (commit_kind = 'change' AND operation_count >= 1)
            ),
            CHECK(operation_count > 0 OR operation_target_count = 0),
            CHECK(
                (operation_count = 0 AND last_operation_id IS NULL AND
                    last_operation_ordinal IS NULL) OR
                (operation_count > 0 AND last_operation_id IS NOT NULL AND
                    last_operation_ordinal = operation_count - 1)
            ),
            CHECK(
                (outbox_event_count = 0 AND last_outbox_event_id IS NULL AND
                    last_outbox_ordinal IS NULL) OR
                (outbox_event_count > 0 AND
                    last_outbox_event_id IS NOT NULL AND
                    last_outbox_ordinal = outbox_event_count - 1)
            ),
            FOREIGN KEY(transaction_id, receipt_kind)
                REFERENCES transaction_receipts(transaction_id, kind)
                ON UPDATE RESTRICT ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED,
            FOREIGN KEY(
                repository_epoch, cutover_id, source_admission_revision
            )
                REFERENCES repository_runtime_control(
                    repository_epoch, cutover_id, source_admission_revision
                )
                ON UPDATE RESTRICT ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED,
            FOREIGN KEY(
                previous_commit_id, project_id, generation, document_id,
                before_root_sha256, previous_operation_tail_sha256
            )
                REFERENCES business_commits(
                    commit_id, project_id, generation, document_id,
                    after_root_sha256, operation_tail_sha256
                ) ON UPDATE RESTRICT ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED,
            FOREIGN KEY(
                last_operation_id, commit_id, last_operation_ordinal,
                operation_tail_sha256
            ) REFERENCES business_operations(
                operation_id, commit_id, ordinal, self_hash
            ) ON UPDATE RESTRICT ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED,
            FOREIGN KEY(
                last_outbox_event_id, commit_id, last_outbox_ordinal
            ) REFERENCES event_outbox(event_id, commit_id, ordinal)
                ON UPDATE RESTRICT ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED
        ) STRICT
        """,
        f"""
        CREATE TABLE document_commit_heads (
            project_id TEXT NOT NULL PRIMARY KEY CHECK(
                length(project_id) BETWEEN 1 AND 160 AND
                length(CAST(project_id AS BLOB)) BETWEEN 1 AND 640
            ),
            generation INTEGER NOT NULL CHECK(
                generation BETWEEN 1 AND 9223372036854775807
            ),
            document_id TEXT NOT NULL UNIQUE CHECK(
                length(CAST(document_id AS BLOB)) BETWEEN 1 AND
                    {MAX_BUSINESS_ID_BYTES}
            ),
            document_version INTEGER NOT NULL CHECK(
                document_version BETWEEN 1 AND 9223372036854775807
            ),
            commit_id TEXT NOT NULL UNIQUE CHECK(
                length(commit_id) = 32 AND
                length(CAST(commit_id AS BLOB)) = 32 AND
                commit_id NOT GLOB '*[^0-9a-f]*'
            ),
            head_revision INTEGER NOT NULL CHECK(
                head_revision BETWEEN 1 AND 9223372036854775807
            ),
            status TEXT NOT NULL CHECK(
                status IN ('live', 'deleted', 'repair_required')
            ),
            current_snapshot_sha256 BLOB CHECK(
                current_snapshot_sha256 IS NULL OR (
                    typeof(current_snapshot_sha256) = 'blob' AND
                    length(current_snapshot_sha256) = 32
                )
            ),
            current_project_snapshot_sha256 BLOB CHECK(
                current_project_snapshot_sha256 IS NULL OR (
                    typeof(current_project_snapshot_sha256) = 'blob' AND
                    length(current_project_snapshot_sha256) = 32
                )
            ),
            business_root_sha256 BLOB NOT NULL CHECK(
                typeof(business_root_sha256) = 'blob' AND
                length(business_root_sha256) = 32
            ),
            operation_tail_sha256 BLOB NOT NULL CHECK(
                typeof(operation_tail_sha256) = 'blob' AND
                length(operation_tail_sha256) = 32
            ),
            asset_projection_sha256 BLOB NOT NULL CHECK(
                typeof(asset_projection_sha256) = 'blob' AND
                length(asset_projection_sha256) = 32
            ),
            asset_reference_count INTEGER NOT NULL CHECK(
                asset_reference_count BETWEEN 0 AND 1000000
            ),
            entity_count INTEGER NOT NULL CHECK(
                entity_count BETWEEN 0 AND 1000000
            ),
            updated_at TEXT NOT NULL CHECK(
                length(CAST(updated_at AS BLOB)) = 24 AND
                strftime('%Y-%m-%dT%H:%M:%fZ', updated_at) IS NOT NULL AND
                strftime('%Y-%m-%dT%H:%M:%fZ', updated_at) = updated_at
            ),
            UNIQUE(project_id, generation, document_id),
            UNIQUE(project_id, generation, document_id, document_version),
            CHECK(
                (status = 'live' AND current_snapshot_sha256 IS NOT NULL AND
                    current_project_snapshot_sha256 IS NOT NULL AND
                    current_project_snapshot_sha256 = business_root_sha256) OR
                (status IN ('deleted', 'repair_required') AND
                    current_snapshot_sha256 IS NULL AND
                    current_project_snapshot_sha256 IS NULL)
            ),
            FOREIGN KEY(project_id, generation)
                REFERENCES repository_projects(project_id, generation)
                ON UPDATE RESTRICT ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED,
            FOREIGN KEY(
                project_id, generation, current_project_snapshot_sha256
            ) REFERENCES repository_projects(
                project_id, generation, current_snapshot_sha256
            ) ON UPDATE NO ACTION ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED,
            FOREIGN KEY(
                commit_id, project_id, generation, document_id,
                document_version, head_revision, business_root_sha256,
                operation_tail_sha256, asset_projection_sha256,
                asset_reference_count, entity_count
            ) REFERENCES business_commits(
                commit_id, project_id, generation, document_id,
                document_version, repository_revision, after_root_sha256,
                operation_tail_sha256, asset_projection_sha256,
                asset_reference_count, entity_count
            ) ON UPDATE RESTRICT ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED,
            FOREIGN KEY(
                project_id, document_id, document_version,
                current_snapshot_sha256
            ) REFERENCES documents(
                project_id, document_id, document_version, snapshot_sha256
            ) ON UPDATE NO ACTION ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED
        ) STRICT
        """,
        f"""
        CREATE TABLE business_operations (
            operation_id TEXT NOT NULL PRIMARY KEY CHECK(
                length(operation_id) = 32 AND
                length(CAST(operation_id AS BLOB)) = 32 AND
                operation_id NOT GLOB '*[^0-9a-f]*'
            ),
            previous_operation_id TEXT CHECK(
                previous_operation_id IS NULL OR (
                    length(previous_operation_id) = 32 AND
                    length(CAST(previous_operation_id AS BLOB)) = 32 AND
                    previous_operation_id NOT GLOB '*[^0-9a-f]*'
                )
            ),
            previous_ordinal INTEGER CHECK(
                previous_ordinal IS NULL OR
                previous_ordinal BETWEEN 0 AND 9998
            ),
            previous_hash BLOB NOT NULL CHECK(
                typeof(previous_hash) = 'blob' AND length(previous_hash) = 32
            ),
            self_hash BLOB NOT NULL CHECK(
                typeof(self_hash) = 'blob' AND length(self_hash) = 32
            ),
            commit_id TEXT NOT NULL CHECK(
                length(commit_id) = 32 AND
                length(CAST(commit_id AS BLOB)) = 32 AND
                commit_id NOT GLOB '*[^0-9a-f]*'
            ),
            commit_operation_count INTEGER NOT NULL CHECK(
                commit_operation_count BETWEEN 1 AND 10000
            ),
            commit_kind TEXT NOT NULL CHECK(commit_kind = 'change'),
            ordinal INTEGER NOT NULL CHECK(ordinal BETWEEN 0 AND 9999),
            project_id TEXT NOT NULL CHECK(
                length(project_id) BETWEEN 1 AND 160 AND
                length(CAST(project_id AS BLOB)) BETWEEN 1 AND 640
            ),
            generation INTEGER NOT NULL CHECK(
                generation BETWEEN 1 AND 9223372036854775807
            ),
            document_id TEXT NOT NULL CHECK(
                length(CAST(document_id AS BLOB)) BETWEEN 1 AND
                    {MAX_BUSINESS_ID_BYTES}
            ),
            command_id TEXT NOT NULL CHECK(
                length(CAST(command_id AS BLOB)) BETWEEN 1 AND
                    {MAX_BUSINESS_ID_BYTES}
            ),
            source TEXT NOT NULL CHECK(source IN ('user', 'ai', 'sync', 'system')),
            kind TEXT NOT NULL CHECK(
                kind IN ('native', 'stage', 'compose', 'rollback')
            ),
            trace_id TEXT NOT NULL CHECK(
                length(CAST(trace_id AS BLOB)) BETWEEN 1 AND
                    {MAX_BUSINESS_ID_BYTES}
            ),
            group_id TEXT CHECK(
                group_id IS NULL OR
                length(CAST(group_id AS BLOB)) BETWEEN 1 AND
                    {MAX_BUSINESS_ID_BYTES}
            ),
            actor_bytes BLOB NOT NULL CHECK(
                typeof(actor_bytes) = 'blob' AND
                length(actor_bytes) BETWEEN 1 AND {MAX_BUSINESS_SMALL_PAYLOAD_BYTES}
            ),
            actor_sha256 BLOB NOT NULL CHECK(
                typeof(actor_sha256) = 'blob' AND length(actor_sha256) = 32
            ),
            operation_bytes BLOB NOT NULL CHECK(
                typeof(operation_bytes) = 'blob' AND
                length(operation_bytes) BETWEEN 1 AND
                    {MAX_BUSINESS_SMALL_PAYLOAD_BYTES}
            ),
            operation_sha256 BLOB NOT NULL CHECK(
                typeof(operation_sha256) = 'blob' AND
                length(operation_sha256) = 32
            ),
            operation_schema TEXT NOT NULL CHECK(
                length(CAST(operation_schema AS BLOB)) BETWEEN 1 AND
                    {MAX_BUSINESS_ID_BYTES}
            ),
            codec_id TEXT NOT NULL CHECK(
                length(CAST(codec_id AS BLOB)) BETWEEN 1 AND 64
            ),
            codec_version INTEGER NOT NULL CHECK(
                codec_version BETWEEN 1 AND 9223372036854775807
            ),
            occurred_at TEXT NOT NULL CHECK(
                length(CAST(occurred_at AS BLOB)) = 24 AND
                strftime('%Y-%m-%dT%H:%M:%fZ', occurred_at) IS NOT NULL AND
                strftime('%Y-%m-%dT%H:%M:%fZ', occurred_at) = occurred_at
            ),
            UNIQUE(commit_id, ordinal),
            UNIQUE(operation_id, commit_id),
            UNIQUE(operation_id, commit_id, self_hash),
            UNIQUE(operation_id, commit_id, ordinal, self_hash),
            CHECK(
                (ordinal = 0 AND previous_operation_id IS NULL AND
                    previous_ordinal IS NULL) OR
                (ordinal > 0 AND previous_operation_id IS NOT NULL AND
                    previous_operation_id != operation_id AND
                    previous_ordinal = ordinal - 1)
            ),
            CHECK(ordinal < commit_operation_count),
            FOREIGN KEY(commit_id, project_id, generation, document_id)
                REFERENCES business_commits(
                    commit_id, project_id, generation, document_id
                ) ON UPDATE RESTRICT ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED,
            FOREIGN KEY(commit_id, commit_operation_count, commit_kind)
                REFERENCES business_commits(
                    commit_id, operation_count, commit_kind
                ) ON UPDATE RESTRICT ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED,
            FOREIGN KEY(
                previous_operation_id, commit_id, previous_ordinal,
                previous_hash
            )
                REFERENCES business_operations(
                    operation_id, commit_id, ordinal, self_hash
                ) ON UPDATE RESTRICT ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED
        ) STRICT
        """,
        f"""
        CREATE TABLE business_operation_targets (
            operation_id TEXT NOT NULL CHECK(
                length(operation_id) = 32 AND
                length(CAST(operation_id AS BLOB)) = 32 AND
                operation_id NOT GLOB '*[^0-9a-f]*'
            ),
            ordinal INTEGER NOT NULL CHECK(ordinal BETWEEN 0 AND 999999),
            entity_type TEXT NOT NULL CHECK(
                length(CAST(entity_type AS BLOB)) BETWEEN 1 AND
                    {MAX_BUSINESS_ID_BYTES}
            ),
            entity_id TEXT NOT NULL CHECK(
                length(CAST(entity_id AS BLOB)) BETWEEN 1 AND
                    {MAX_BUSINESS_ID_BYTES}
            ),
            field TEXT NOT NULL CHECK(
                length(CAST(field AS BLOB)) BETWEEN 1 AND
                    {MAX_BUSINESS_ID_BYTES}
            ),
            patch_kind TEXT NOT NULL CHECK(
                patch_kind IN ('set', 'delete', 'create', 'soft_delete', 'restore', 'name')
            ),
            PRIMARY KEY(operation_id, ordinal),
            FOREIGN KEY(operation_id)
                REFERENCES business_operations(operation_id)
                ON UPDATE RESTRICT ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED
        ) STRICT
        """,
        f"""
        CREATE TABLE idempotency_inbox (
            scope TEXT NOT NULL CHECK(
                length(CAST(scope AS BLOB)) BETWEEN 1 AND
                    {MAX_BUSINESS_ID_BYTES}
            ),
            idempotency_key TEXT NOT NULL CHECK(
                length(CAST(idempotency_key AS BLOB)) BETWEEN 1 AND
                    {MAX_BUSINESS_ID_BYTES}
            ),
            request_sha256 BLOB NOT NULL CHECK(
                typeof(request_sha256) = 'blob' AND length(request_sha256) = 32
            ),
            request_bytes BLOB NOT NULL CHECK(
                typeof(request_bytes) = 'blob' AND
                length(request_bytes) BETWEEN 1 AND
                    {MAX_BUSINESS_SMALL_PAYLOAD_BYTES}
            ),
            request_schema TEXT NOT NULL CHECK(
                length(CAST(request_schema AS BLOB)) BETWEEN 1 AND
                    {MAX_BUSINESS_ID_BYTES}
            ),
            request_codec_id TEXT NOT NULL CHECK(
                length(CAST(request_codec_id AS BLOB)) BETWEEN 1 AND 64
            ),
            request_codec_version INTEGER NOT NULL CHECK(
                request_codec_version BETWEEN 1 AND 9223372036854775807
            ),
            actor_id TEXT NOT NULL CHECK(
                length(CAST(actor_id AS BLOB)) BETWEEN 1 AND
                    {MAX_BUSINESS_ID_BYTES}
            ),
            claim_owner_id TEXT NOT NULL CHECK(
                length(claim_owner_id) = 32 AND
                length(CAST(claim_owner_id AS BLOB)) = 32 AND
                claim_owner_id NOT GLOB '*[^0-9a-f]*'
            ),
            claim_fence INTEGER NOT NULL CHECK(
                claim_fence BETWEEN 1 AND 9223372036854775807
            ),
            status TEXT NOT NULL CHECK(
                status IN ('claimed', 'committed', 'released', 'repair_required')
            ),
            state_revision INTEGER NOT NULL CHECK(
                state_revision BETWEEN 1 AND 9223372036854775807
            ),
            lease_expires_at TEXT CHECK(
                lease_expires_at IS NULL OR
                length(CAST(lease_expires_at AS BLOB)) = 24 AND
                strftime('%Y-%m-%dT%H:%M:%fZ', lease_expires_at) IS NOT NULL AND
                strftime('%Y-%m-%dT%H:%M:%fZ', lease_expires_at) = lease_expires_at
            ),
            result_kind TEXT CHECK(
                result_kind IS NULL OR
                result_kind IN ('commit', 'confirmation', 'noop')
            ),
            commit_id TEXT UNIQUE,
            confirmation_id TEXT UNIQUE,
            response_bytes BLOB CHECK(
                response_bytes IS NULL OR (
                    typeof(response_bytes) = 'blob' AND
                    length(response_bytes) BETWEEN 1 AND
                        {MAX_BUSINESS_SMALL_PAYLOAD_BYTES}
                )
            ),
            response_sha256 BLOB CHECK(
                response_sha256 IS NULL OR (
                    typeof(response_sha256) = 'blob' AND
                    length(response_sha256) = 32
                )
            ),
            response_schema TEXT CHECK(
                response_schema IS NULL OR
                length(CAST(response_schema AS BLOB)) BETWEEN 1 AND
                    {MAX_BUSINESS_ID_BYTES}
            ),
            response_codec_id TEXT CHECK(
                response_codec_id IS NULL OR
                length(CAST(response_codec_id AS BLOB)) BETWEEN 1 AND 64
            ),
            response_codec_version INTEGER CHECK(
                response_codec_version IS NULL OR
                response_codec_version BETWEEN 1 AND 9223372036854775807
            ),
            claimed_at TEXT NOT NULL CHECK(
                length(CAST(claimed_at AS BLOB)) = 24 AND
                strftime('%Y-%m-%dT%H:%M:%fZ', claimed_at) IS NOT NULL AND
                strftime('%Y-%m-%dT%H:%M:%fZ', claimed_at) = claimed_at
            ),
            updated_at TEXT NOT NULL CHECK(
                length(CAST(updated_at AS BLOB)) = 24 AND
                strftime('%Y-%m-%dT%H:%M:%fZ', updated_at) IS NOT NULL AND
                strftime('%Y-%m-%dT%H:%M:%fZ', updated_at) = updated_at
            ),
            PRIMARY KEY(scope, idempotency_key),
            CHECK(
                (status = 'claimed' AND lease_expires_at IS NOT NULL AND
                    commit_id IS NULL AND
                    confirmation_id IS NULL AND result_kind IS NULL AND
                    response_bytes IS NULL AND response_sha256 IS NULL) OR
                (status = 'committed' AND lease_expires_at IS NULL AND
                    result_kind IS NOT NULL AND
                    response_bytes IS NOT NULL AND response_sha256 IS NOT NULL) OR
                (status = 'released' AND lease_expires_at IS NULL AND
                    commit_id IS NULL AND
                    confirmation_id IS NULL AND result_kind IS NULL AND
                    response_bytes IS NULL AND response_sha256 IS NULL) OR
                (status = 'repair_required')
            ),
            CHECK(
                result_kind IS NULL OR
                (result_kind = 'commit' AND commit_id IS NOT NULL AND
                    confirmation_id IS NULL) OR
                (result_kind = 'confirmation' AND commit_id IS NULL AND
                    confirmation_id IS NOT NULL) OR
                (result_kind = 'noop' AND commit_id IS NULL AND
                    confirmation_id IS NULL)
            ),
            CHECK((response_bytes IS NULL) = (response_sha256 IS NULL)),
            CHECK(
                (response_bytes IS NULL AND response_schema IS NULL AND
                    response_codec_id IS NULL AND
                    response_codec_version IS NULL) OR
                (response_bytes IS NOT NULL AND response_schema IS NOT NULL AND
                    response_codec_id IS NOT NULL AND
                    response_codec_version IS NOT NULL)
            ),
            FOREIGN KEY(commit_id)
                REFERENCES business_commits(commit_id)
                ON UPDATE RESTRICT ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED,
            FOREIGN KEY(confirmation_id)
                REFERENCES confirmation_requests(confirmation_id)
                ON UPDATE RESTRICT ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED
        ) STRICT
        """,
        f"""
        CREATE TABLE event_outbox (
            event_id TEXT NOT NULL PRIMARY KEY CHECK(
                length(event_id) = 32 AND
                length(CAST(event_id AS BLOB)) = 32 AND
                event_id NOT GLOB '*[^0-9a-f]*'
            ),
            previous_event_id TEXT CHECK(
                previous_event_id IS NULL OR (
                    length(previous_event_id) = 32 AND
                    length(CAST(previous_event_id AS BLOB)) = 32 AND
                    previous_event_id NOT GLOB '*[^0-9a-f]*'
                )
            ),
            previous_ordinal INTEGER CHECK(
                previous_ordinal IS NULL OR
                previous_ordinal BETWEEN 0 AND 9998
            ),
            commit_id TEXT NOT NULL CHECK(
                length(commit_id) = 32 AND
                length(CAST(commit_id AS BLOB)) = 32 AND
                commit_id NOT GLOB '*[^0-9a-f]*'
            ),
            repository_revision INTEGER NOT NULL CHECK(
                repository_revision BETWEEN 1 AND 9223372036854775807
            ),
            commit_event_count INTEGER NOT NULL CHECK(
                commit_event_count BETWEEN 1 AND 10000
            ),
            operation_id TEXT NOT NULL CHECK(
                length(operation_id) = 32 AND
                length(CAST(operation_id AS BLOB)) = 32 AND
                operation_id NOT GLOB '*[^0-9a-f]*'
            ),
            ordinal INTEGER NOT NULL CHECK(ordinal BETWEEN 0 AND 9999),
            topic TEXT NOT NULL CHECK(
                length(CAST(topic AS BLOB)) BETWEEN 1 AND
                    {MAX_BUSINESS_ID_BYTES}
            ),
            partition_key TEXT NOT NULL CHECK(
                length(CAST(partition_key AS BLOB)) BETWEEN 1 AND
                    {MAX_BUSINESS_ID_BYTES}
            ),
            event_bytes BLOB NOT NULL CHECK(
                typeof(event_bytes) = 'blob' AND
                length(event_bytes) BETWEEN 1 AND
                    {MAX_BUSINESS_SMALL_PAYLOAD_BYTES}
            ),
            event_sha256 BLOB NOT NULL CHECK(
                typeof(event_sha256) = 'blob' AND length(event_sha256) = 32
            ),
            payload_schema TEXT NOT NULL CHECK(
                length(CAST(payload_schema AS BLOB)) BETWEEN 1 AND
                    {MAX_BUSINESS_ID_BYTES}
            ),
            codec_id TEXT NOT NULL CHECK(
                length(CAST(codec_id AS BLOB)) BETWEEN 1 AND 64
            ),
            codec_version INTEGER NOT NULL CHECK(
                codec_version BETWEEN 1 AND 9223372036854775807
            ),
            status TEXT NOT NULL CHECK(
                status IN (
                    'pending', 'retry', 'leased', 'published',
                    'outcome_unknown', 'dead'
                )
            ),
            state_revision INTEGER NOT NULL CHECK(
                state_revision BETWEEN 1 AND 9223372036854775807
            ),
            attempt_count INTEGER NOT NULL CHECK(
                attempt_count BETWEEN 0 AND 100
            ),
            available_at TEXT NOT NULL CHECK(
                length(CAST(available_at AS BLOB)) = 24 AND
                strftime('%Y-%m-%dT%H:%M:%fZ', available_at) IS NOT NULL AND
                strftime('%Y-%m-%dT%H:%M:%fZ', available_at) = available_at
            ),
            lease_owner TEXT CHECK(
                lease_owner IS NULL OR
                length(CAST(lease_owner AS BLOB)) BETWEEN 1 AND
                    {MAX_BUSINESS_ID_BYTES}
            ),
            lease_fence INTEGER CHECK(
                lease_fence IS NULL OR lease_fence BETWEEN 1 AND
                    9223372036854775807
            ),
            lease_expires_at TEXT CHECK(
                lease_expires_at IS NULL OR
                length(CAST(lease_expires_at AS BLOB)) = 24 AND
                strftime('%Y-%m-%dT%H:%M:%fZ', lease_expires_at) IS NOT NULL AND
                strftime('%Y-%m-%dT%H:%M:%fZ', lease_expires_at) = lease_expires_at
            ),
            published_at TEXT CHECK(
                published_at IS NULL OR
                length(CAST(published_at AS BLOB)) = 24 AND
                strftime('%Y-%m-%dT%H:%M:%fZ', published_at) IS NOT NULL AND
                strftime('%Y-%m-%dT%H:%M:%fZ', published_at) = published_at
            ),
            outcome_unknown_at TEXT CHECK(
                outcome_unknown_at IS NULL OR
                length(CAST(outcome_unknown_at AS BLOB)) = 24 AND
                strftime('%Y-%m-%dT%H:%M:%fZ', outcome_unknown_at) IS NOT NULL AND
                strftime('%Y-%m-%dT%H:%M:%fZ', outcome_unknown_at) = outcome_unknown_at
            ),
            dead_at TEXT CHECK(
                dead_at IS NULL OR
                length(CAST(dead_at AS BLOB)) = 24 AND
                strftime('%Y-%m-%dT%H:%M:%fZ', dead_at) IS NOT NULL AND
                strftime('%Y-%m-%dT%H:%M:%fZ', dead_at) = dead_at
            ),
            last_error_bytes BLOB CHECK(
                last_error_bytes IS NULL OR (
                    typeof(last_error_bytes) = 'blob' AND
                    length(last_error_bytes) BETWEEN 1 AND
                        {MAX_BUSINESS_SMALL_PAYLOAD_BYTES}
                )
            ),
            last_error_sha256 BLOB CHECK(
                last_error_sha256 IS NULL OR (
                    typeof(last_error_sha256) = 'blob' AND
                    length(last_error_sha256) = 32
                )
            ),
            created_at TEXT NOT NULL CHECK(
                length(CAST(created_at AS BLOB)) = 24 AND
                strftime('%Y-%m-%dT%H:%M:%fZ', created_at) IS NOT NULL AND
                strftime('%Y-%m-%dT%H:%M:%fZ', created_at) = created_at
            ),
            updated_at TEXT NOT NULL CHECK(
                length(CAST(updated_at AS BLOB)) = 24 AND
                strftime('%Y-%m-%dT%H:%M:%fZ', updated_at) IS NOT NULL AND
                strftime('%Y-%m-%dT%H:%M:%fZ', updated_at) = updated_at
            ),
            UNIQUE(commit_id, ordinal),
            UNIQUE(repository_revision, ordinal),
            UNIQUE(event_id, commit_id, ordinal),
            CHECK(ordinal < commit_event_count),
            CHECK(
                (ordinal = 0 AND previous_event_id IS NULL AND
                    previous_ordinal IS NULL) OR
                (ordinal > 0 AND previous_event_id IS NOT NULL AND
                    previous_event_id != event_id AND
                    previous_ordinal = ordinal - 1)
            ),
            CHECK((last_error_bytes IS NULL) = (last_error_sha256 IS NULL)),
            CHECK(
                (lease_owner IS NULL AND lease_fence IS NULL AND
                    lease_expires_at IS NULL) OR
                (lease_owner IS NOT NULL AND lease_fence IS NOT NULL AND
                    lease_expires_at IS NOT NULL)
            ),
            CHECK(
                (status = 'leased' AND lease_owner IS NOT NULL AND
                    published_at IS NULL AND outcome_unknown_at IS NULL AND
                    dead_at IS NULL) OR
                (status = 'published' AND lease_owner IS NULL AND
                    published_at IS NOT NULL AND outcome_unknown_at IS NULL AND
                    dead_at IS NULL) OR
                (status = 'outcome_unknown' AND lease_owner IS NULL AND
                    published_at IS NULL AND outcome_unknown_at IS NOT NULL AND
                    dead_at IS NULL) OR
                (status = 'dead' AND lease_owner IS NULL AND
                    published_at IS NULL AND outcome_unknown_at IS NULL AND
                    dead_at IS NOT NULL) OR
                (status IN ('pending', 'retry') AND lease_owner IS NULL AND
                    published_at IS NULL AND outcome_unknown_at IS NULL AND
                    dead_at IS NULL)
            ),
            FOREIGN KEY(commit_id, repository_revision)
                REFERENCES business_commits(commit_id, repository_revision)
                ON UPDATE RESTRICT ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED,
            FOREIGN KEY(commit_id, commit_event_count)
                REFERENCES business_commits(commit_id, outbox_event_count)
                ON UPDATE RESTRICT ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED,
            FOREIGN KEY(
                previous_event_id, commit_id, previous_ordinal
            ) REFERENCES event_outbox(event_id, commit_id, ordinal)
                ON UPDATE RESTRICT ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED,
            FOREIGN KEY(operation_id, commit_id)
                REFERENCES business_operations(operation_id, commit_id)
                ON UPDATE RESTRICT ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED
        ) STRICT
        """,
        f"""
        CREATE TABLE event_outbox_dispatch_control (
            singleton INTEGER NOT NULL DEFAULT 1
                PRIMARY KEY CHECK(singleton = 1),
            state_revision INTEGER NOT NULL CHECK(
                state_revision BETWEEN 1 AND 9223372036854775807
            ),
            lease_owner TEXT CHECK(
                lease_owner IS NULL OR
                length(CAST(lease_owner AS BLOB)) BETWEEN 1 AND
                    {MAX_BUSINESS_ID_BYTES}
            ),
            lease_fence INTEGER CHECK(
                lease_fence IS NULL OR lease_fence BETWEEN 1 AND
                    9223372036854775807
            ),
            lease_expires_at TEXT CHECK(
                lease_expires_at IS NULL OR (
                    length(CAST(lease_expires_at AS BLOB)) = 24 AND
                    strftime(
                        '%Y-%m-%dT%H:%M:%fZ', lease_expires_at
                    ) IS NOT NULL AND
                    strftime(
                        '%Y-%m-%dT%H:%M:%fZ', lease_expires_at
                    ) = lease_expires_at
                )
            ),
            published_repository_revision INTEGER NOT NULL CHECK(
                published_repository_revision BETWEEN 0 AND
                    9223372036854775807
            ),
            published_ordinal INTEGER NOT NULL CHECK(
                published_ordinal BETWEEN -1 AND 9999
            ),
            updated_at TEXT NOT NULL CHECK(
                length(CAST(updated_at AS BLOB)) = 24 AND
                strftime('%Y-%m-%dT%H:%M:%fZ', updated_at) IS NOT NULL AND
                strftime('%Y-%m-%dT%H:%M:%fZ', updated_at) = updated_at
            ),
            CHECK(
                (lease_owner IS NULL AND lease_fence IS NULL AND
                    lease_expires_at IS NULL) OR
                (lease_owner IS NOT NULL AND lease_fence IS NOT NULL AND
                    lease_expires_at IS NOT NULL)
            ),
            CHECK(
                (published_repository_revision = 0 AND
                    published_ordinal = -1) OR
                (published_repository_revision >= 1 AND
                    published_ordinal >= 0)
            ),
            FOREIGN KEY(singleton)
                REFERENCES repository_runtime_control(singleton)
                ON UPDATE RESTRICT ON DELETE RESTRICT
        ) STRICT
        """,
        f"""
        CREATE TABLE confirmation_requests (
            confirmation_id TEXT NOT NULL PRIMARY KEY CHECK(
                length(confirmation_id) = 32 AND
                length(CAST(confirmation_id AS BLOB)) = 32 AND
                confirmation_id NOT GLOB '*[^0-9a-f]*'
            ),
            project_id TEXT NOT NULL CHECK(
                length(project_id) BETWEEN 1 AND 160 AND
                length(CAST(project_id AS BLOB)) BETWEEN 1 AND 640
            ),
            generation INTEGER NOT NULL CHECK(
                generation BETWEEN 1 AND 9223372036854775807
            ),
            document_id TEXT NOT NULL CHECK(
                length(CAST(document_id AS BLOB)) BETWEEN 1 AND
                    {MAX_BUSINESS_ID_BYTES}
            ),
            actor_id TEXT NOT NULL CHECK(
                length(CAST(actor_id AS BLOB)) BETWEEN 1 AND
                    {MAX_BUSINESS_ID_BYTES}
            ),
            approval_nonce_hmac BLOB NOT NULL UNIQUE CHECK(
                typeof(approval_nonce_hmac) = 'blob' AND
                length(approval_nonce_hmac) = 32
            ),
            nonce_key_id TEXT NOT NULL CHECK(
                length(CAST(nonce_key_id AS BLOB)) BETWEEN 1 AND
                    {MAX_BUSINESS_ID_BYTES}
            ),
            request_bytes BLOB NOT NULL CHECK(
                typeof(request_bytes) = 'blob' AND
                length(request_bytes) BETWEEN 1 AND
                    {MAX_BUSINESS_SMALL_PAYLOAD_BYTES}
            ),
            request_sha256 BLOB NOT NULL CHECK(
                typeof(request_sha256) = 'blob' AND length(request_sha256) = 32
            ),
            request_schema TEXT NOT NULL CHECK(
                length(CAST(request_schema AS BLOB)) BETWEEN 1 AND
                    {MAX_BUSINESS_ID_BYTES}
            ),
            request_codec_id TEXT NOT NULL CHECK(
                length(CAST(request_codec_id AS BLOB)) BETWEEN 1 AND 64
            ),
            request_codec_version INTEGER NOT NULL CHECK(
                request_codec_version BETWEEN 1 AND 9223372036854775807
            ),
            status TEXT NOT NULL CHECK(
                status IN (
                    'pending', 'reserved', 'consumed', 'rejected', 'cancelled',
                    'expired', 'frozen', 'repair_required'
                )
            ),
            state_revision INTEGER NOT NULL CHECK(
                state_revision BETWEEN 1 AND 9223372036854775807
            ),
            reservation_owner_id TEXT CHECK(
                reservation_owner_id IS NULL OR (
                    length(reservation_owner_id) = 32 AND
                    length(CAST(reservation_owner_id AS BLOB)) = 32 AND
                    reservation_owner_id NOT GLOB '*[^0-9a-f]*'
                )
            ),
            reservation_fence INTEGER CHECK(
                reservation_fence IS NULL OR reservation_fence BETWEEN 1 AND
                    9223372036854775807
            ),
            reservation_expires_at TEXT CHECK(
                reservation_expires_at IS NULL OR
                length(CAST(reservation_expires_at AS BLOB)) = 24 AND
                strftime('%Y-%m-%dT%H:%M:%fZ', reservation_expires_at) IS NOT NULL AND
                strftime('%Y-%m-%dT%H:%M:%fZ', reservation_expires_at) = reservation_expires_at
            ),
            decision TEXT CHECK(
                decision IS NULL OR decision IN ('confirmed', 'rejected')
            ),
            resolution_bytes BLOB CHECK(
                resolution_bytes IS NULL OR (
                    typeof(resolution_bytes) = 'blob' AND
                    length(resolution_bytes) BETWEEN 1 AND
                        {MAX_BUSINESS_SMALL_PAYLOAD_BYTES}
                )
            ),
            resolution_sha256 BLOB CHECK(
                resolution_sha256 IS NULL OR (
                    typeof(resolution_sha256) = 'blob' AND
                    length(resolution_sha256) = 32
                )
            ),
            resolution_schema TEXT CHECK(
                resolution_schema IS NULL OR
                length(CAST(resolution_schema AS BLOB)) BETWEEN 1 AND
                    {MAX_BUSINESS_ID_BYTES}
            ),
            resolution_codec_id TEXT CHECK(
                resolution_codec_id IS NULL OR
                length(CAST(resolution_codec_id AS BLOB)) BETWEEN 1 AND 64
            ),
            resolution_codec_version INTEGER CHECK(
                resolution_codec_version IS NULL OR
                resolution_codec_version BETWEEN 1 AND 9223372036854775807
            ),
            result_commit_id TEXT,
            expires_at TEXT NOT NULL CHECK(
                length(CAST(expires_at AS BLOB)) = 24 AND
                strftime('%Y-%m-%dT%H:%M:%fZ', expires_at) IS NOT NULL AND
                strftime('%Y-%m-%dT%H:%M:%fZ', expires_at) = expires_at
            ),
            resolved_at TEXT CHECK(
                resolved_at IS NULL OR
                length(CAST(resolved_at AS BLOB)) = 24 AND
                strftime('%Y-%m-%dT%H:%M:%fZ', resolved_at) IS NOT NULL AND
                strftime('%Y-%m-%dT%H:%M:%fZ', resolved_at) = resolved_at
            ),
            created_at TEXT NOT NULL CHECK(
                length(CAST(created_at AS BLOB)) = 24 AND
                strftime('%Y-%m-%dT%H:%M:%fZ', created_at) IS NOT NULL AND
                strftime('%Y-%m-%dT%H:%M:%fZ', created_at) = created_at
            ),
            updated_at TEXT NOT NULL CHECK(
                length(CAST(updated_at AS BLOB)) = 24 AND
                strftime('%Y-%m-%dT%H:%M:%fZ', updated_at) IS NOT NULL AND
                strftime('%Y-%m-%dT%H:%M:%fZ', updated_at) = updated_at
            ),
            CHECK((resolution_bytes IS NULL) = (resolution_sha256 IS NULL)),
            CHECK(
                (resolution_bytes IS NULL AND resolution_schema IS NULL AND
                    resolution_codec_id IS NULL AND
                    resolution_codec_version IS NULL) OR
                (resolution_bytes IS NOT NULL AND
                    resolution_schema IS NOT NULL AND
                    resolution_codec_id IS NOT NULL AND
                    resolution_codec_version IS NOT NULL)
            ),
            CHECK(
                (reservation_owner_id IS NULL AND reservation_fence IS NULL AND
                    reservation_expires_at IS NULL) OR
                (reservation_owner_id IS NOT NULL AND
                    reservation_fence IS NOT NULL AND
                    reservation_expires_at IS NOT NULL)
            ),
            CHECK(
                (status = 'pending' AND resolution_bytes IS NULL AND
                    resolved_at IS NULL AND result_commit_id IS NULL AND
                    decision IS NULL AND reservation_owner_id IS NULL) OR
                (status = 'reserved' AND resolution_bytes IS NULL AND
                    resolved_at IS NULL AND result_commit_id IS NULL AND
                    decision IS NULL AND reservation_owner_id IS NOT NULL) OR
                (status = 'consumed' AND resolution_bytes IS NOT NULL AND
                    resolved_at IS NOT NULL AND decision = 'confirmed' AND
                    reservation_owner_id IS NULL AND
                    result_commit_id IS NOT NULL) OR
                (status = 'rejected' AND resolution_bytes IS NOT NULL AND
                    resolved_at IS NOT NULL AND decision = 'rejected' AND
                    reservation_owner_id IS NULL AND
                    result_commit_id IS NULL) OR
                (status IN ('cancelled', 'expired', 'frozen') AND
                    resolution_bytes IS NOT NULL AND resolved_at IS NOT NULL AND
                    result_commit_id IS NULL AND decision IS NULL AND
                    reservation_owner_id IS NULL) OR
                (status = 'repair_required')
            ),
            FOREIGN KEY(project_id, generation)
                REFERENCES repository_projects(project_id, generation)
                ON UPDATE RESTRICT ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED,
            FOREIGN KEY(result_commit_id, project_id, generation, document_id)
                REFERENCES business_commits(
                    commit_id, project_id, generation, document_id
                ) ON UPDATE RESTRICT ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED
        ) STRICT
        """,
        f"""
        CREATE TABLE deferred_actions (
            action_id TEXT NOT NULL PRIMARY KEY CHECK(
                length(action_id) = 32 AND
                length(CAST(action_id AS BLOB)) = 32 AND
                action_id NOT GLOB '*[^0-9a-f]*'
            ),
            idempotency_scope TEXT NOT NULL CHECK(
                length(CAST(idempotency_scope AS BLOB)) BETWEEN 1 AND
                    {MAX_BUSINESS_ID_BYTES}
            ),
            idempotency_key TEXT NOT NULL CHECK(
                length(CAST(idempotency_key AS BLOB)) BETWEEN 1 AND
                    {MAX_BUSINESS_ID_BYTES}
            ),
            project_id TEXT NOT NULL CHECK(
                length(project_id) BETWEEN 1 AND 160 AND
                length(CAST(project_id AS BLOB)) BETWEEN 1 AND 640
            ),
            generation INTEGER NOT NULL CHECK(
                generation BETWEEN 1 AND 9223372036854775807
            ),
            document_id TEXT NOT NULL CHECK(
                length(CAST(document_id AS BLOB)) BETWEEN 1 AND
                    {MAX_BUSINESS_ID_BYTES}
            ),
            origin_commit_id TEXT NOT NULL CHECK(
                length(origin_commit_id) = 32 AND
                length(CAST(origin_commit_id AS BLOB)) = 32 AND
                origin_commit_id NOT GLOB '*[^0-9a-f]*'
            ),
            commit_action_count INTEGER NOT NULL CHECK(
                commit_action_count BETWEEN 1 AND 10000
            ),
            ordinal INTEGER NOT NULL CHECK(ordinal BETWEEN 0 AND 9999),
            action_kind TEXT NOT NULL CHECK(
                length(CAST(action_kind AS BLOB)) BETWEEN 1 AND
                    {MAX_BUSINESS_ID_BYTES}
            ),
            action_bytes BLOB NOT NULL CHECK(
                typeof(action_bytes) = 'blob' AND
                length(action_bytes) BETWEEN 1 AND
                    {MAX_BUSINESS_SMALL_PAYLOAD_BYTES}
            ),
            action_sha256 BLOB NOT NULL CHECK(
                typeof(action_sha256) = 'blob' AND length(action_sha256) = 32
            ),
            payload_schema TEXT NOT NULL CHECK(
                length(CAST(payload_schema AS BLOB)) BETWEEN 1 AND
                    {MAX_BUSINESS_ID_BYTES}
            ),
            codec_id TEXT NOT NULL CHECK(
                length(CAST(codec_id AS BLOB)) BETWEEN 1 AND 64
            ),
            codec_version INTEGER NOT NULL CHECK(
                codec_version BETWEEN 1 AND 9223372036854775807
            ),
            replay_policy TEXT NOT NULL CHECK(
                replay_policy IN (
                    'provider_idempotent', 'reconcile_before_retry',
                    'never_replay'
                )
            ),
            idempotency_policy TEXT NOT NULL CHECK(
                idempotency_policy IN (
                    'required_external_key', 'durable_receipt_reconcile',
                    'single_attempt'
                )
            ),
            status TEXT NOT NULL CHECK(
                status IN (
                    'queued', 'running', 'retry', 'succeeded',
                    'outcome_unknown', 'failed_terminal', 'cancelled',
                    'repair_required'
                )
            ),
            state_revision INTEGER NOT NULL CHECK(
                state_revision BETWEEN 1 AND 9223372036854775807
            ),
            attempt_count INTEGER NOT NULL CHECK(
                attempt_count BETWEEN 0 AND 100
            ),
            last_attempt_id TEXT CHECK(
                last_attempt_id IS NULL OR (
                    length(last_attempt_id) = 32 AND
                    length(CAST(last_attempt_id AS BLOB)) = 32 AND
                    last_attempt_id NOT GLOB '*[^0-9a-f]*'
                )
            ),
            last_attempt_number INTEGER CHECK(
                last_attempt_number IS NULL OR
                last_attempt_number BETWEEN 1 AND 100
            ),
            max_attempts INTEGER NOT NULL CHECK(
                max_attempts BETWEEN 1 AND 100 AND
                attempt_count <= max_attempts
            ),
            next_attempt_at TEXT CHECK(
                next_attempt_at IS NULL OR
                length(CAST(next_attempt_at AS BLOB)) = 24 AND
                strftime('%Y-%m-%dT%H:%M:%fZ', next_attempt_at) IS NOT NULL AND
                strftime('%Y-%m-%dT%H:%M:%fZ', next_attempt_at) = next_attempt_at
            ),
            lease_owner TEXT CHECK(
                lease_owner IS NULL OR
                length(CAST(lease_owner AS BLOB)) BETWEEN 1 AND
                    {MAX_BUSINESS_ID_BYTES}
            ),
            lease_fence INTEGER CHECK(
                lease_fence IS NULL OR lease_fence BETWEEN 1 AND
                    9223372036854775807
            ),
            lease_expires_at TEXT CHECK(
                lease_expires_at IS NULL OR
                length(CAST(lease_expires_at AS BLOB)) = 24 AND
                strftime('%Y-%m-%dT%H:%M:%fZ', lease_expires_at) IS NOT NULL AND
                strftime('%Y-%m-%dT%H:%M:%fZ', lease_expires_at) = lease_expires_at
            ),
            result_commit_id TEXT,
            current_attempt_id TEXT CHECK(
                current_attempt_id IS NULL OR (
                    length(current_attempt_id) = 32 AND
                    length(CAST(current_attempt_id AS BLOB)) = 32 AND
                    current_attempt_id NOT GLOB '*[^0-9a-f]*'
                )
            ),
            current_attempt_started_at TEXT CHECK(
                current_attempt_started_at IS NULL OR (
                    length(CAST(current_attempt_started_at AS BLOB)) = 24 AND
                    strftime(
                        '%Y-%m-%dT%H:%M:%fZ', current_attempt_started_at
                    ) IS NOT NULL AND
                    strftime(
                        '%Y-%m-%dT%H:%M:%fZ', current_attempt_started_at
                    ) = current_attempt_started_at
                )
            ),
            created_at TEXT NOT NULL CHECK(
                length(CAST(created_at AS BLOB)) = 24 AND
                strftime('%Y-%m-%dT%H:%M:%fZ', created_at) IS NOT NULL AND
                strftime('%Y-%m-%dT%H:%M:%fZ', created_at) = created_at
            ),
            updated_at TEXT NOT NULL CHECK(
                length(CAST(updated_at AS BLOB)) = 24 AND
                strftime('%Y-%m-%dT%H:%M:%fZ', updated_at) IS NOT NULL AND
                strftime('%Y-%m-%dT%H:%M:%fZ', updated_at) = updated_at
            ),
            UNIQUE(idempotency_scope, idempotency_key),
            UNIQUE(origin_commit_id, ordinal),
            UNIQUE(action_id, idempotency_scope, idempotency_key),
            UNIQUE(
                action_id, idempotency_scope, idempotency_key,
                replay_policy, idempotency_policy
            ),
            CHECK(
                (lease_owner IS NULL AND lease_fence IS NULL AND
                    lease_expires_at IS NULL) OR
                (lease_owner IS NOT NULL AND lease_fence IS NOT NULL AND
                    lease_expires_at IS NOT NULL)
            ),
            CHECK(
                (status = 'running' AND lease_owner IS NOT NULL AND
                    current_attempt_id IS NOT NULL AND
                    current_attempt_started_at IS NOT NULL) OR
                (status != 'running' AND lease_owner IS NULL AND
                    current_attempt_id IS NULL AND
                    current_attempt_started_at IS NULL)
            ),
            CHECK(
                (status = 'running' AND attempt_count >= 1 AND (
                    (attempt_count = 1 AND last_attempt_id IS NULL AND
                        last_attempt_number IS NULL) OR
                    (attempt_count > 1 AND last_attempt_id IS NOT NULL AND
                        last_attempt_number = attempt_count - 1)
                )) OR
                (status != 'running' AND (
                    (attempt_count = 0 AND last_attempt_id IS NULL AND
                        last_attempt_number IS NULL) OR
                    (attempt_count > 0 AND last_attempt_id IS NOT NULL AND
                        last_attempt_number = attempt_count)
                ))
            ),
            CHECK(
                (replay_policy = 'never_replay' OR
                    idempotency_policy = 'single_attempt') IS NOT TRUE OR
                max_attempts = 1
            ),
            CHECK(ordinal < commit_action_count),
            FOREIGN KEY(project_id, generation)
                REFERENCES repository_projects(project_id, generation)
                ON UPDATE RESTRICT ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED,
            FOREIGN KEY(origin_commit_id, project_id, generation, document_id)
                REFERENCES business_commits(
                    commit_id, project_id, generation, document_id
                ) ON UPDATE RESTRICT ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED,
            FOREIGN KEY(origin_commit_id, commit_action_count)
                REFERENCES business_commits(commit_id, durable_action_count)
                ON UPDATE RESTRICT ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED,
            FOREIGN KEY(result_commit_id, project_id, generation, document_id)
                REFERENCES business_commits(
                    commit_id, project_id, generation, document_id
                ) ON UPDATE RESTRICT ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED,
            FOREIGN KEY(action_id, last_attempt_id, last_attempt_number)
                REFERENCES deferred_action_attempts(
                    action_id, attempt_id, attempt_number
                ) ON UPDATE RESTRICT ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED
        ) STRICT
        """,
        f"""
        CREATE TABLE deferred_action_attempts (
            action_id TEXT NOT NULL CHECK(
                length(action_id) = 32 AND
                length(CAST(action_id AS BLOB)) = 32 AND
                action_id NOT GLOB '*[^0-9a-f]*'
            ),
            attempt_number INTEGER NOT NULL CHECK(
                attempt_number BETWEEN 1 AND 100
            ),
            attempt_id TEXT NOT NULL UNIQUE CHECK(
                length(attempt_id) = 32 AND
                length(CAST(attempt_id AS BLOB)) = 32 AND
                attempt_id NOT GLOB '*[^0-9a-f]*'
            ),
            previous_attempt_id TEXT CHECK(
                previous_attempt_id IS NULL OR (
                    length(previous_attempt_id) = 32 AND
                    length(CAST(previous_attempt_id AS BLOB)) = 32 AND
                    previous_attempt_id NOT GLOB '*[^0-9a-f]*'
                )
            ),
            previous_attempt_number INTEGER CHECK(
                previous_attempt_number IS NULL OR
                previous_attempt_number BETWEEN 1 AND 99
            ),
            outcome TEXT NOT NULL CHECK(
                outcome IN (
                    'succeeded', 'retry', 'outcome_unknown',
                    'failed_terminal', 'cancelled', 'abandoned'
                )
            ),
            execution_fence INTEGER NOT NULL CHECK(
                execution_fence BETWEEN 1 AND 9223372036854775807
            ),
            idempotency_scope TEXT NOT NULL CHECK(
                length(CAST(idempotency_scope AS BLOB)) BETWEEN 1 AND
                    {MAX_BUSINESS_ID_BYTES}
            ),
            idempotency_key TEXT NOT NULL CHECK(
                length(CAST(idempotency_key AS BLOB)) BETWEEN 1 AND
                    {MAX_BUSINESS_ID_BYTES}
            ),
            replay_policy TEXT NOT NULL CHECK(
                replay_policy IN (
                    'provider_idempotent', 'reconcile_before_retry',
                    'never_replay'
                )
            ),
            idempotency_policy TEXT NOT NULL CHECK(
                idempotency_policy IN (
                    'required_external_key', 'durable_receipt_reconcile',
                    'single_attempt'
                )
            ),
            result_commit_id TEXT,
            result_bytes BLOB CHECK(
                result_bytes IS NULL OR (
                    typeof(result_bytes) = 'blob' AND
                    length(result_bytes) BETWEEN 1 AND
                        {MAX_BUSINESS_SMALL_PAYLOAD_BYTES}
                )
            ),
            result_sha256 BLOB CHECK(
                result_sha256 IS NULL OR (
                    typeof(result_sha256) = 'blob' AND
                    length(result_sha256) = 32
                )
            ),
            started_at TEXT NOT NULL CHECK(
                length(CAST(started_at AS BLOB)) = 24 AND
                strftime('%Y-%m-%dT%H:%M:%fZ', started_at) IS NOT NULL AND
                strftime('%Y-%m-%dT%H:%M:%fZ', started_at) = started_at
            ),
            finished_at TEXT NOT NULL CHECK(
                length(CAST(finished_at AS BLOB)) = 24 AND
                strftime('%Y-%m-%dT%H:%M:%fZ', finished_at) IS NOT NULL AND
                strftime('%Y-%m-%dT%H:%M:%fZ', finished_at) = finished_at
            ),
            PRIMARY KEY(action_id, attempt_number),
            UNIQUE(action_id, attempt_id, attempt_number),
            CHECK((result_bytes IS NULL) = (result_sha256 IS NULL)),
            CHECK(finished_at >= started_at),
            CHECK(
                (attempt_number = 1 AND previous_attempt_id IS NULL AND
                    previous_attempt_number IS NULL) OR
                (attempt_number > 1 AND previous_attempt_id IS NOT NULL AND
                    previous_attempt_id != attempt_id AND
                    previous_attempt_number = attempt_number - 1)
            ),
            FOREIGN KEY(
                action_id, idempotency_scope, idempotency_key,
                replay_policy, idempotency_policy
            )
                REFERENCES deferred_actions(
                    action_id, idempotency_scope, idempotency_key,
                    replay_policy, idempotency_policy
                )
                ON UPDATE RESTRICT ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED,
            FOREIGN KEY(result_commit_id)
                REFERENCES business_commits(commit_id)
                ON UPDATE RESTRICT ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED,
            FOREIGN KEY(
                action_id, previous_attempt_id, previous_attempt_number
            ) REFERENCES deferred_action_attempts(
                action_id, attempt_id, attempt_number
            ) ON UPDATE RESTRICT ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED
        ) STRICT
        """,
        f"""
        CREATE TABLE pipeline_jobs (
            job_id TEXT NOT NULL PRIMARY KEY CHECK(
                length(job_id) = 32 AND
                length(CAST(job_id AS BLOB)) = 32 AND
                job_id NOT GLOB '*[^0-9a-f]*'
            ),
            last_intent_id TEXT NOT NULL CHECK(
                length(last_intent_id) = 32 AND
                length(CAST(last_intent_id AS BLOB)) = 32 AND
                last_intent_id NOT GLOB '*[^0-9a-f]*'
            ),
            idempotency_key TEXT NOT NULL CHECK(
                length(CAST(idempotency_key AS BLOB)) BETWEEN 1 AND
                    {MAX_BUSINESS_ID_BYTES}
            ),
            project_id TEXT NOT NULL CHECK(
                length(project_id) BETWEEN 1 AND 160 AND
                length(CAST(project_id AS BLOB)) BETWEEN 1 AND 640
            ),
            generation INTEGER NOT NULL CHECK(
                generation BETWEEN 1 AND 9223372036854775807
            ),
            document_id TEXT NOT NULL CHECK(
                length(CAST(document_id AS BLOB)) BETWEEN 1 AND
                    {MAX_BUSINESS_ID_BYTES}
            ),
            pipeline_id TEXT NOT NULL CHECK(
                length(CAST(pipeline_id AS BLOB)) BETWEEN 1 AND
                    {MAX_BUSINESS_ID_BYTES}
            ),
            intent_type TEXT NOT NULL CHECK(
                length(CAST(intent_type AS BLOB)) BETWEEN 1 AND
                    {MAX_BUSINESS_ID_BYTES}
            ),
            intent_kind TEXT NOT NULL CHECK(
                intent_kind IN ('create', 'transition')
            ),
            expected_state_revision INTEGER CHECK(
                expected_state_revision IS NULL OR
                expected_state_revision BETWEEN 1 AND 9223372036854775807
            ),
            input_commit_id TEXT NOT NULL CHECK(
                length(input_commit_id) = 32 AND
                length(CAST(input_commit_id AS BLOB)) = 32 AND
                input_commit_id NOT GLOB '*[^0-9a-f]*'
            ),
            head_commit_id TEXT NOT NULL CHECK(
                length(head_commit_id) = 32 AND
                length(CAST(head_commit_id AS BLOB)) = 32 AND
                head_commit_id NOT GLOB '*[^0-9a-f]*'
            ),
            job_bytes BLOB NOT NULL CHECK(
                typeof(job_bytes) = 'blob' AND
                length(job_bytes) BETWEEN 1 AND
                    {MAX_BUSINESS_SMALL_PAYLOAD_BYTES}
            ),
            job_sha256 BLOB NOT NULL CHECK(
                typeof(job_sha256) = 'blob' AND length(job_sha256) = 32
            ),
            payload_schema TEXT NOT NULL CHECK(
                length(CAST(payload_schema AS BLOB)) BETWEEN 1 AND
                    {MAX_BUSINESS_ID_BYTES}
            ),
            codec_id TEXT NOT NULL CHECK(
                length(CAST(codec_id AS BLOB)) BETWEEN 1 AND 64
            ),
            codec_version INTEGER NOT NULL CHECK(
                codec_version BETWEEN 1 AND 9223372036854775807
            ),
            replay_policy TEXT NOT NULL CHECK(
                replay_policy IN (
                    'exact_cas_retry', 'reload_and_replan', 'never_replay'
                )
            ),
            status TEXT NOT NULL CHECK(
                status IN (
                    'queued', 'running', 'paused', 'cancel_requested',
                    'cancelled', 'succeeded', 'failed', 'repair_required'
                )
            ),
            state_revision INTEGER NOT NULL CHECK(
                state_revision BETWEEN 1 AND 9223372036854775807
            ),
            attempt_count INTEGER NOT NULL CHECK(
                attempt_count BETWEEN 0 AND 100
            ),
            last_attempt_id TEXT CHECK(
                last_attempt_id IS NULL OR (
                    length(last_attempt_id) = 32 AND
                    length(CAST(last_attempt_id AS BLOB)) = 32 AND
                    last_attempt_id NOT GLOB '*[^0-9a-f]*'
                )
            ),
            last_attempt_number INTEGER CHECK(
                last_attempt_number IS NULL OR
                last_attempt_number BETWEEN 1 AND 100
            ),
            max_attempts INTEGER NOT NULL CHECK(
                max_attempts BETWEEN 1 AND 100 AND
                attempt_count <= max_attempts
            ),
            next_attempt_at TEXT CHECK(
                next_attempt_at IS NULL OR
                length(CAST(next_attempt_at AS BLOB)) = 24 AND
                strftime('%Y-%m-%dT%H:%M:%fZ', next_attempt_at) IS NOT NULL AND
                strftime('%Y-%m-%dT%H:%M:%fZ', next_attempt_at) = next_attempt_at
            ),
            lease_owner TEXT CHECK(
                lease_owner IS NULL OR
                length(CAST(lease_owner AS BLOB)) BETWEEN 1 AND
                    {MAX_BUSINESS_ID_BYTES}
            ),
            lease_fence INTEGER CHECK(
                lease_fence IS NULL OR lease_fence BETWEEN 1 AND
                    9223372036854775807
            ),
            lease_expires_at TEXT CHECK(
                lease_expires_at IS NULL OR
                length(CAST(lease_expires_at AS BLOB)) = 24 AND
                strftime('%Y-%m-%dT%H:%M:%fZ', lease_expires_at) IS NOT NULL AND
                strftime('%Y-%m-%dT%H:%M:%fZ', lease_expires_at) = lease_expires_at
            ),
            result_commit_id TEXT,
            current_attempt_id TEXT CHECK(
                current_attempt_id IS NULL OR (
                    length(current_attempt_id) = 32 AND
                    length(CAST(current_attempt_id AS BLOB)) = 32 AND
                    current_attempt_id NOT GLOB '*[^0-9a-f]*'
                )
            ),
            current_attempt_started_at TEXT CHECK(
                current_attempt_started_at IS NULL OR (
                    length(CAST(current_attempt_started_at AS BLOB)) = 24 AND
                    strftime(
                        '%Y-%m-%dT%H:%M:%fZ', current_attempt_started_at
                    ) IS NOT NULL AND
                    strftime(
                        '%Y-%m-%dT%H:%M:%fZ', current_attempt_started_at
                    ) = current_attempt_started_at
                )
            ),
            created_at TEXT NOT NULL CHECK(
                length(CAST(created_at AS BLOB)) = 24 AND
                strftime('%Y-%m-%dT%H:%M:%fZ', created_at) IS NOT NULL AND
                strftime('%Y-%m-%dT%H:%M:%fZ', created_at) = created_at
            ),
            updated_at TEXT NOT NULL CHECK(
                length(CAST(updated_at AS BLOB)) = 24 AND
                strftime('%Y-%m-%dT%H:%M:%fZ', updated_at) IS NOT NULL AND
                strftime('%Y-%m-%dT%H:%M:%fZ', updated_at) = updated_at
            ),
            UNIQUE(job_id, replay_policy),
            UNIQUE(job_id, project_id, generation, document_id),
            UNIQUE(project_id, generation, idempotency_key),
            CHECK(
                (lease_owner IS NULL AND lease_fence IS NULL AND
                    lease_expires_at IS NULL) OR
                (lease_owner IS NOT NULL AND lease_fence IS NOT NULL AND
                    lease_expires_at IS NOT NULL)
            ),
            CHECK(
                (status = 'running' AND lease_owner IS NOT NULL AND
                    current_attempt_id IS NOT NULL AND
                    current_attempt_started_at IS NOT NULL) OR
                (status = 'cancel_requested' AND (
                    (lease_owner IS NOT NULL AND
                        current_attempt_id IS NOT NULL AND
                        current_attempt_started_at IS NOT NULL) OR
                    (lease_owner IS NULL AND current_attempt_id IS NULL AND
                        current_attempt_started_at IS NULL)
                )) OR
                (status NOT IN ('running', 'cancel_requested') AND
                    lease_owner IS NULL AND
                    current_attempt_id IS NULL AND
                    current_attempt_started_at IS NULL)
            ),
            CHECK(
                (intent_kind = 'create' AND expected_state_revision IS NULL) OR
                (intent_kind = 'transition' AND
                    expected_state_revision IS NOT NULL)
            ),
            CHECK(
                (status = 'running' AND attempt_count >= 1 AND (
                    (attempt_count = 1 AND last_attempt_id IS NULL AND
                        last_attempt_number IS NULL) OR
                    (attempt_count > 1 AND last_attempt_id IS NOT NULL AND
                        last_attempt_number = attempt_count - 1)
                )) OR
                (status = 'cancel_requested' AND
                    current_attempt_id IS NOT NULL AND attempt_count >= 1 AND (
                        (attempt_count = 1 AND last_attempt_id IS NULL AND
                            last_attempt_number IS NULL) OR
                        (attempt_count > 1 AND last_attempt_id IS NOT NULL AND
                            last_attempt_number = attempt_count - 1)
                    )) OR
                (status = 'cancel_requested' AND
                    current_attempt_id IS NULL AND (
                        (attempt_count = 0 AND last_attempt_id IS NULL AND
                            last_attempt_number IS NULL) OR
                        (attempt_count > 0 AND last_attempt_id IS NOT NULL AND
                            last_attempt_number = attempt_count)
                    )) OR
                (status NOT IN ('running', 'cancel_requested') AND (
                    (attempt_count = 0 AND last_attempt_id IS NULL AND
                        last_attempt_number IS NULL) OR
                    (attempt_count > 0 AND last_attempt_id IS NOT NULL AND
                        last_attempt_number = attempt_count)
                ))
            ),
            CHECK(
                replay_policy != 'never_replay' OR max_attempts = 1
            ),
            FOREIGN KEY(project_id, generation)
                REFERENCES repository_projects(project_id, generation)
                ON UPDATE RESTRICT ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED,
            FOREIGN KEY(input_commit_id, project_id, generation, document_id)
                REFERENCES business_commits(
                    commit_id, project_id, generation, document_id
                ) ON UPDATE RESTRICT ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED,
            FOREIGN KEY(head_commit_id, project_id, generation, document_id)
                REFERENCES business_commits(
                    commit_id, project_id, generation, document_id
                ) ON UPDATE RESTRICT ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED,
            FOREIGN KEY(result_commit_id, project_id, generation, document_id)
                REFERENCES business_commits(
                    commit_id, project_id, generation, document_id
                ) ON UPDATE RESTRICT ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED,
            FOREIGN KEY(job_id, last_attempt_id, last_attempt_number)
                REFERENCES pipeline_job_attempts(
                    job_id, attempt_id, attempt_number
                ) ON UPDATE RESTRICT ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED,
            FOREIGN KEY(job_id, idempotency_key, max_attempts)
                REFERENCES pipeline_job_transitions(
                    intent_id, idempotency_key, max_attempts
                ) ON UPDATE RESTRICT ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED,
            FOREIGN KEY(
                last_intent_id, job_id, project_id, generation, document_id,
                head_commit_id, state_revision, status
            ) REFERENCES pipeline_job_transitions(
                intent_id, job_id, project_id, generation, document_id,
                origin_commit_id, after_state_revision, after_status
            ) ON UPDATE NO ACTION ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED
        ) STRICT
        """,
        f"""
        CREATE TABLE pipeline_job_attempts (
            job_id TEXT NOT NULL CHECK(
                length(job_id) = 32 AND
                length(CAST(job_id AS BLOB)) = 32 AND
                job_id NOT GLOB '*[^0-9a-f]*'
            ),
            attempt_number INTEGER NOT NULL CHECK(
                attempt_number BETWEEN 1 AND 100
            ),
            attempt_id TEXT NOT NULL UNIQUE CHECK(
                length(attempt_id) = 32 AND
                length(CAST(attempt_id AS BLOB)) = 32 AND
                attempt_id NOT GLOB '*[^0-9a-f]*'
            ),
            previous_attempt_id TEXT CHECK(
                previous_attempt_id IS NULL OR (
                    length(previous_attempt_id) = 32 AND
                    length(CAST(previous_attempt_id AS BLOB)) = 32 AND
                    previous_attempt_id NOT GLOB '*[^0-9a-f]*'
                )
            ),
            previous_attempt_number INTEGER CHECK(
                previous_attempt_number IS NULL OR
                previous_attempt_number BETWEEN 1 AND 99
            ),
            outcome TEXT NOT NULL CHECK(
                outcome IN (
                    'succeeded', 'failed', 'cancelled', 'abandoned',
                    'outcome_unknown'
                )
            ),
            execution_fence INTEGER NOT NULL CHECK(
                execution_fence BETWEEN 1 AND 9223372036854775807
            ),
            replay_policy TEXT NOT NULL CHECK(
                replay_policy IN (
                    'exact_cas_retry', 'reload_and_replan', 'never_replay'
                )
            ),
            result_commit_id TEXT,
            result_bytes BLOB CHECK(
                result_bytes IS NULL OR (
                    typeof(result_bytes) = 'blob' AND
                    length(result_bytes) BETWEEN 1 AND
                        {MAX_BUSINESS_SMALL_PAYLOAD_BYTES}
                )
            ),
            result_sha256 BLOB CHECK(
                result_sha256 IS NULL OR (
                    typeof(result_sha256) = 'blob' AND
                    length(result_sha256) = 32
                )
            ),
            started_at TEXT NOT NULL CHECK(
                length(CAST(started_at AS BLOB)) = 24 AND
                strftime('%Y-%m-%dT%H:%M:%fZ', started_at) IS NOT NULL AND
                strftime('%Y-%m-%dT%H:%M:%fZ', started_at) = started_at
            ),
            finished_at TEXT NOT NULL CHECK(
                length(CAST(finished_at AS BLOB)) = 24 AND
                strftime('%Y-%m-%dT%H:%M:%fZ', finished_at) IS NOT NULL AND
                strftime('%Y-%m-%dT%H:%M:%fZ', finished_at) = finished_at
            ),
            PRIMARY KEY(job_id, attempt_number),
            UNIQUE(job_id, attempt_id, attempt_number),
            CHECK((result_bytes IS NULL) = (result_sha256 IS NULL)),
            CHECK(finished_at >= started_at),
            CHECK(
                (attempt_number = 1 AND previous_attempt_id IS NULL AND
                    previous_attempt_number IS NULL) OR
                (attempt_number > 1 AND previous_attempt_id IS NOT NULL AND
                    previous_attempt_id != attempt_id AND
                    previous_attempt_number = attempt_number - 1)
            ),
            FOREIGN KEY(job_id, replay_policy)
                REFERENCES pipeline_jobs(job_id, replay_policy)
                ON UPDATE RESTRICT ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED,
            FOREIGN KEY(result_commit_id)
                REFERENCES business_commits(commit_id)
                ON UPDATE RESTRICT ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED,
            FOREIGN KEY(
                job_id, previous_attempt_id, previous_attempt_number
            ) REFERENCES pipeline_job_attempts(
                job_id, attempt_id, attempt_number
            ) ON UPDATE RESTRICT ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED
        ) STRICT
        """,
        f"""
        CREATE TABLE pipeline_job_transitions (
            intent_id TEXT NOT NULL PRIMARY KEY CHECK(
                length(intent_id) = 32 AND
                length(CAST(intent_id AS BLOB)) = 32 AND
                intent_id NOT GLOB '*[^0-9a-f]*'
            ),
            origin_commit_id TEXT NOT NULL CHECK(
                length(origin_commit_id) = 32 AND
                length(CAST(origin_commit_id AS BLOB)) = 32 AND
                origin_commit_id NOT GLOB '*[^0-9a-f]*'
            ),
            commit_pipeline_intent_count INTEGER NOT NULL CHECK(
                commit_pipeline_intent_count BETWEEN 1 AND 10000
            ),
            ordinal INTEGER NOT NULL CHECK(ordinal BETWEEN 0 AND 9999),
            job_id TEXT NOT NULL CHECK(
                length(job_id) = 32 AND
                length(CAST(job_id AS BLOB)) = 32 AND
                job_id NOT GLOB '*[^0-9a-f]*'
            ),
            project_id TEXT NOT NULL CHECK(
                length(project_id) BETWEEN 1 AND 160 AND
                length(CAST(project_id AS BLOB)) BETWEEN 1 AND 640
            ),
            generation INTEGER NOT NULL CHECK(
                generation BETWEEN 1 AND 9223372036854775807
            ),
            document_id TEXT NOT NULL CHECK(
                length(CAST(document_id AS BLOB)) BETWEEN 1 AND
                    {MAX_BUSINESS_ID_BYTES}
            ),
            intent_type TEXT NOT NULL CHECK(
                length(CAST(intent_type AS BLOB)) BETWEEN 1 AND
                    {MAX_BUSINESS_ID_BYTES}
            ),
            idempotency_key TEXT NOT NULL CHECK(
                length(CAST(idempotency_key AS BLOB)) BETWEEN 1 AND
                    {MAX_BUSINESS_ID_BYTES}
            ),
            intent_kind TEXT NOT NULL CHECK(
                intent_kind IN ('create', 'transition')
            ),
            before_state_revision INTEGER CHECK(
                before_state_revision IS NULL OR
                before_state_revision BETWEEN 1 AND 9223372036854775806
            ),
            after_state_revision INTEGER NOT NULL CHECK(
                after_state_revision BETWEEN 1 AND 9223372036854775807
            ),
            before_status TEXT CHECK(
                before_status IS NULL OR before_status IN (
                    'queued', 'running', 'paused', 'cancel_requested',
                    'cancelled', 'succeeded', 'failed', 'repair_required'
                )
            ),
            after_status TEXT NOT NULL CHECK(
                after_status IN (
                    'queued', 'running', 'paused', 'cancel_requested',
                    'cancelled', 'succeeded', 'failed', 'repair_required'
                )
            ),
            intent_bytes BLOB NOT NULL CHECK(
                typeof(intent_bytes) = 'blob' AND
                length(intent_bytes) BETWEEN 1 AND
                    {MAX_BUSINESS_SMALL_PAYLOAD_BYTES}
            ),
            intent_sha256 BLOB NOT NULL CHECK(
                typeof(intent_sha256) = 'blob' AND length(intent_sha256) = 32
            ),
            payload_schema TEXT NOT NULL CHECK(
                length(CAST(payload_schema AS BLOB)) BETWEEN 1 AND
                    {MAX_BUSINESS_ID_BYTES}
            ),
            codec_id TEXT NOT NULL CHECK(
                length(CAST(codec_id AS BLOB)) BETWEEN 1 AND 64
            ),
            codec_version INTEGER NOT NULL CHECK(
                codec_version BETWEEN 1 AND 9223372036854775807
            ),
            replay_policy TEXT NOT NULL CHECK(
                replay_policy IN (
                    'exact_cas_retry', 'reload_and_replan', 'never_replay'
                )
            ),
            max_attempts INTEGER NOT NULL CHECK(
                max_attempts BETWEEN 1 AND 100
            ),
            committed_at TEXT NOT NULL CHECK(
                length(CAST(committed_at AS BLOB)) = 24 AND
                strftime('%Y-%m-%dT%H:%M:%fZ', committed_at) IS NOT NULL AND
                strftime('%Y-%m-%dT%H:%M:%fZ', committed_at) = committed_at
            ),
            UNIQUE(origin_commit_id, ordinal),
            UNIQUE(job_id, after_state_revision),
            UNIQUE(project_id, generation, idempotency_key),
            UNIQUE(intent_id, idempotency_key, max_attempts),
            UNIQUE(
                intent_id, job_id, project_id, generation, document_id,
                origin_commit_id, after_state_revision, after_status,
                intent_sha256, replay_policy, idempotency_key, max_attempts
            ),
            UNIQUE(
                intent_id, job_id, project_id, generation, document_id,
                origin_commit_id, after_state_revision, after_status
            ),
            CHECK(
                (intent_kind = 'create' AND intent_id = job_id AND
                    before_state_revision IS NULL AND before_status IS NULL AND
                    after_state_revision = 1 AND after_status = 'queued') OR
                (intent_kind = 'transition' AND
                    before_state_revision IS NOT NULL AND
                    before_status IS NOT NULL AND
                    after_state_revision = before_state_revision + 1)
            ),
            CHECK(
                intent_kind = 'create' OR
                (before_status = 'queued' AND
                    after_status IN ('running', 'paused', 'cancel_requested')) OR
                (before_status = 'running' AND after_status IN (
                    'paused', 'cancel_requested', 'succeeded', 'failed',
                    'repair_required'
                )) OR
                (before_status = 'paused' AND
                    after_status IN ('running', 'cancel_requested')) OR
                (before_status = 'cancel_requested' AND
                    after_status IN ('cancelled', 'repair_required')) OR
                (before_status = 'repair_required' AND
                    after_status IN ('paused', 'cancelled', 'failed'))
            ),
            CHECK(replay_policy != 'never_replay' OR max_attempts = 1),
            CHECK(ordinal < commit_pipeline_intent_count),
            FOREIGN KEY(origin_commit_id, project_id, generation, document_id)
                REFERENCES business_commits(
                    commit_id, project_id, generation, document_id
                )
                ON UPDATE RESTRICT ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED,
            FOREIGN KEY(
                origin_commit_id, commit_pipeline_intent_count
            ) REFERENCES business_commits(commit_id, pipeline_intent_count)
                ON UPDATE RESTRICT ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED,
            FOREIGN KEY(job_id, project_id, generation, document_id)
                REFERENCES pipeline_jobs(
                    job_id, project_id, generation, document_id
                )
                ON UPDATE RESTRICT ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED
        ) STRICT
        """,
        f"""
        CREATE TABLE asset_blobs (
            asset_sha256 BLOB NOT NULL PRIMARY KEY CHECK(
                typeof(asset_sha256) = 'blob' AND length(asset_sha256) = 32
            ),
            content_key TEXT NOT NULL UNIQUE CHECK(
                content_key =
                    'sha256/' || substr(lower(hex(asset_sha256)), 1, 2) ||
                    '/' || lower(hex(asset_sha256))
            ),
            media_type TEXT CHECK(
                media_type IS NULL OR
                length(CAST(media_type AS BLOB)) BETWEEN 0 AND 255
            ),
            extension TEXT CHECK(
                extension IS NULL OR (
                    length(CAST(extension AS BLOB)) BETWEEN 0 AND 32 AND
                    instr(extension, '/') = 0 AND
                    instr(extension, char(92)) = 0
                )
            ),
            byte_length INTEGER NOT NULL CHECK(
                byte_length BETWEEN 0 AND 9223372036854775807
            ),
            status TEXT NOT NULL CHECK(
                status IN (
                    'staged', 'available', 'quarantined', 'deleting',
                    'tombstoned', 'repair_required'
                )
            ),
            state_revision INTEGER NOT NULL CHECK(
                state_revision BETWEEN 1 AND 9223372036854775807
            ),
            created_at TEXT NOT NULL CHECK(
                length(CAST(created_at AS BLOB)) = 24 AND
                strftime('%Y-%m-%dT%H:%M:%fZ', created_at) IS NOT NULL AND
                strftime('%Y-%m-%dT%H:%M:%fZ', created_at) = created_at
            ),
            updated_at TEXT NOT NULL CHECK(
                length(CAST(updated_at AS BLOB)) = 24 AND
                strftime('%Y-%m-%dT%H:%M:%fZ', updated_at) IS NOT NULL AND
                strftime('%Y-%m-%dT%H:%M:%fZ', updated_at) = updated_at
            )
        ) STRICT
        """,
        f"""
        -- V5b.1 keeps commit-scoped asset-reference history append-only.
        -- GC therefore fails closed while any historical commit references a
        -- blob; retention/compaction must be designed before GC is enabled.
        CREATE TABLE document_asset_refs (
            project_id TEXT NOT NULL CHECK(
                length(project_id) BETWEEN 1 AND 160 AND
                length(CAST(project_id AS BLOB)) BETWEEN 1 AND 640
            ),
            generation INTEGER NOT NULL CHECK(
                generation BETWEEN 1 AND 9223372036854775807
            ),
            document_id TEXT NOT NULL CHECK(
                length(CAST(document_id AS BLOB)) BETWEEN 1 AND
                    {MAX_BUSINESS_ID_BYTES}
            ),
            document_version INTEGER NOT NULL CHECK(
                document_version BETWEEN 1 AND 9223372036854775807
            ),
            commit_id TEXT NOT NULL CHECK(
                length(commit_id) = 32 AND
                length(CAST(commit_id AS BLOB)) = 32 AND
                commit_id NOT GLOB '*[^0-9a-f]*'
            ),
            commit_asset_reference_count INTEGER NOT NULL CHECK(
                commit_asset_reference_count BETWEEN 1 AND 1000000
            ),
            ordinal INTEGER NOT NULL CHECK(ordinal BETWEEN 0 AND 999999),
            asset_sha256 BLOB NOT NULL CHECK(
                typeof(asset_sha256) = 'blob' AND length(asset_sha256) = 32
            ),
            entity_type TEXT NOT NULL CHECK(
                length(CAST(entity_type AS BLOB)) BETWEEN 1 AND
                    {MAX_BUSINESS_ID_BYTES}
            ),
            entity_id TEXT NOT NULL CHECK(
                length(CAST(entity_id AS BLOB)) BETWEEN 1 AND
                    {MAX_BUSINESS_ID_BYTES}
            ),
            entity_deleted INTEGER NOT NULL CHECK(entity_deleted IN (0, 1)),
            value_path TEXT NOT NULL CHECK(
                length(CAST(value_path AS BLOB)) BETWEEN 1 AND
                    {MAX_BUSINESS_ID_BYTES}
            ),
            ref_mime TEXT CHECK(
                ref_mime IS NULL OR
                length(CAST(ref_mime AS BLOB)) BETWEEN 0 AND 255
            ),
            ref_size INTEGER CHECK(
                ref_size IS NULL OR
                ref_size BETWEEN 0 AND 9223372036854775807
            ),
            ref_ext TEXT CHECK(
                ref_ext IS NULL OR
                length(CAST(ref_ext AS BLOB)) BETWEEN 0 AND 32
            ),
            PRIMARY KEY(commit_id, ordinal),
            UNIQUE(
                commit_id, entity_type, entity_id, value_path
            ),
            CHECK(ordinal < commit_asset_reference_count),
            FOREIGN KEY(
                commit_id, project_id, generation, document_id,
                document_version
            ) REFERENCES business_commits(
                commit_id, project_id, generation, document_id,
                document_version
            ) ON UPDATE RESTRICT ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED,
            FOREIGN KEY(commit_id, commit_asset_reference_count)
                REFERENCES business_commits(commit_id, asset_reference_count)
                ON UPDATE RESTRICT ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED,
            FOREIGN KEY(asset_sha256)
                REFERENCES asset_blobs(asset_sha256)
                ON UPDATE RESTRICT ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED
        ) STRICT
        """,
        """
        CREATE INDEX repository_projects_status
        ON repository_projects(status, updated_at)
        """,
        """
        CREATE INDEX business_commits_document_history
        ON business_commits(project_id, generation, document_id, document_version DESC)
        """,
        """
        CREATE INDEX business_operations_trace
        ON business_operations(trace_id, occurred_at)
        """,
        """
        CREATE INDEX business_operation_targets_entity
        ON business_operation_targets(entity_type, entity_id, operation_id)
        """,
        """
        CREATE INDEX idempotency_inbox_status
        ON idempotency_inbox(status, lease_expires_at)
        """,
        """
        CREATE INDEX event_outbox_dispatch
        ON event_outbox(status, available_at, event_id)
        """,
        """
        CREATE INDEX confirmation_requests_pending
        ON confirmation_requests(status, expires_at, confirmation_id)
        """,
        """
        CREATE INDEX deferred_actions_dispatch
        ON deferred_actions(status, next_attempt_at, action_id)
        """,
        """
        CREATE INDEX pipeline_jobs_dispatch
        ON pipeline_jobs(status, next_attempt_at, job_id)
        """,
        """
        CREATE INDEX pipeline_job_transitions_history
        ON pipeline_job_transitions(job_id, after_state_revision)
        """,
        """
        CREATE INDEX event_outbox_repository_order
        ON event_outbox(repository_revision, ordinal)
        """,
        """
        CREATE INDEX asset_blobs_status
        ON asset_blobs(status, updated_at)
        """,
        """
        CREATE INDEX document_asset_refs_asset
        ON document_asset_refs(asset_sha256, project_id, document_id)
        """,
        """
        CREATE TRIGGER business_operations_commit_chain_root_guard
        BEFORE INSERT ON business_operations
        WHEN NEW.ordinal = 0 AND NOT EXISTS (
            SELECT 1 FROM business_commits
            WHERE commit_id = NEW.commit_id
              AND previous_operation_tail_sha256 = NEW.previous_hash
        )
        BEGIN SELECT RAISE(ABORT, 'business_operation_chain_root_invalid'); END
        """,
        """
        CREATE TRIGGER business_operation_targets_finalized_guard
        BEFORE INSERT ON business_operation_targets
        WHEN EXISTS (
            SELECT 1
            FROM business_operations AS business_operation
            WHERE business_operation.operation_id = NEW.operation_id
              AND (
                EXISTS (
                    SELECT 1 FROM document_commit_heads
                    WHERE commit_id = business_operation.commit_id
                ) OR EXISTS (
                    SELECT 1 FROM business_commits
                    WHERE previous_commit_id = business_operation.commit_id
                )
              )
        )
        BEGIN
            SELECT RAISE(ABORT, 'business_operation_targets_finalized');
        END
        """,
        """
        CREATE TRIGGER document_commit_heads_target_count_insert
        BEFORE INSERT ON document_commit_heads
        WHEN NOT EXISTS (
            SELECT 1
            FROM business_commits AS business_commit
            WHERE business_commit.commit_id = NEW.commit_id
              AND business_commit.operation_target_count = (
                SELECT count(*)
                FROM business_operation_targets AS operation_target
                JOIN business_operations AS business_operation
                  ON business_operation.operation_id =
                    operation_target.operation_id
                WHERE business_operation.commit_id = NEW.commit_id
              )
        )
        BEGIN
            SELECT RAISE(
                ABORT, 'business_operation_target_count_mismatch'
            );
        END
        """,
        """
        CREATE TRIGGER document_commit_heads_target_count_update
        BEFORE UPDATE OF commit_id ON document_commit_heads
        WHEN NOT EXISTS (
            SELECT 1
            FROM business_commits AS business_commit
            WHERE business_commit.commit_id = NEW.commit_id
              AND business_commit.operation_target_count = (
                SELECT count(*)
                FROM business_operation_targets AS operation_target
                JOIN business_operations AS business_operation
                  ON business_operation.operation_id =
                    operation_target.operation_id
                WHERE business_operation.commit_id = NEW.commit_id
              )
        )
        BEGIN
            SELECT RAISE(
                ABORT, 'business_operation_target_count_mismatch'
            );
        END
        """,
        """
        CREATE TRIGGER document_commit_heads_effect_closure_insert
        BEFORE INSERT ON document_commit_heads
        BEGIN
            SELECT CASE
                WHEN NOT EXISTS (
                    SELECT 1 FROM business_commits AS business_commit
                    WHERE business_commit.commit_id = NEW.commit_id
                      AND business_commit.durable_action_count = (
                        SELECT count(*) FROM deferred_actions
                        WHERE origin_commit_id = NEW.commit_id
                      )
                ) THEN RAISE(
                    ABORT, 'business_durable_action_count_mismatch'
                )
                WHEN NOT EXISTS (
                    SELECT 1 FROM business_commits AS business_commit
                    WHERE business_commit.commit_id = NEW.commit_id
                      AND business_commit.pipeline_intent_count = (
                        SELECT count(*) FROM pipeline_job_transitions
                        WHERE origin_commit_id = NEW.commit_id
                      )
                ) THEN RAISE(
                    ABORT, 'business_pipeline_intent_count_mismatch'
                )
                WHEN NOT EXISTS (
                    SELECT 1 FROM business_commits AS business_commit
                    WHERE business_commit.commit_id = NEW.commit_id
                      AND business_commit.asset_reference_count = (
                        SELECT count(*) FROM document_asset_refs
                        WHERE commit_id = NEW.commit_id
                      )
                ) THEN RAISE(
                    ABORT, 'business_asset_reference_count_mismatch'
                )
            END;
        END
        """,
        """
        CREATE TRIGGER document_commit_heads_effect_closure_update
        BEFORE UPDATE OF commit_id ON document_commit_heads
        BEGIN
            SELECT CASE
                WHEN NOT EXISTS (
                    SELECT 1 FROM business_commits AS business_commit
                    WHERE business_commit.commit_id = NEW.commit_id
                      AND business_commit.durable_action_count = (
                        SELECT count(*) FROM deferred_actions
                        WHERE origin_commit_id = NEW.commit_id
                      )
                ) THEN RAISE(
                    ABORT, 'business_durable_action_count_mismatch'
                )
                WHEN NOT EXISTS (
                    SELECT 1 FROM business_commits AS business_commit
                    WHERE business_commit.commit_id = NEW.commit_id
                      AND business_commit.pipeline_intent_count = (
                        SELECT count(*) FROM pipeline_job_transitions
                        WHERE origin_commit_id = NEW.commit_id
                      )
                ) THEN RAISE(
                    ABORT, 'business_pipeline_intent_count_mismatch'
                )
                WHEN NOT EXISTS (
                    SELECT 1 FROM business_commits AS business_commit
                    WHERE business_commit.commit_id = NEW.commit_id
                      AND business_commit.asset_reference_count = (
                        SELECT count(*) FROM document_asset_refs
                        WHERE commit_id = NEW.commit_id
                      )
                ) THEN RAISE(
                    ABORT, 'business_asset_reference_count_mismatch'
                )
            END;
        END
        """,
        """
        CREATE TRIGGER repository_runtime_control_no_delete
        BEFORE DELETE ON repository_runtime_control
        BEGIN SELECT RAISE(ABORT, 'business_state_no_delete'); END
        """,
        """
        CREATE TRIGGER repository_runtime_control_repair_exit_guard
        BEFORE UPDATE OF authority_state ON repository_runtime_control
        WHEN OLD.authority_state = 'repair_required' AND
             NEW.authority_state != 'repair_required' AND EXISTS (
                SELECT 1 FROM repository_projects
                    WHERE status = 'repair_required'
                UNION ALL
                SELECT 1 FROM document_commit_heads
                    WHERE status = 'repair_required'
                UNION ALL
                SELECT 1 FROM idempotency_inbox
                    WHERE status = 'repair_required'
                UNION ALL
                SELECT 1 FROM confirmation_requests
                    WHERE status = 'repair_required'
                UNION ALL
                SELECT 1 FROM deferred_actions
                    WHERE status = 'repair_required'
                UNION ALL
                SELECT 1 FROM pipeline_jobs
                    WHERE status = 'repair_required'
                UNION ALL
                SELECT 1 FROM asset_blobs
                    WHERE status = 'repair_required'
             )
        BEGIN SELECT RAISE(ABORT, 'business_repair_rows_remain'); END
        """,
        """
        CREATE TRIGGER repository_projects_no_delete
        BEFORE DELETE ON repository_projects
        BEGIN SELECT RAISE(ABORT, 'business_state_no_delete'); END
        """,
        """
        CREATE TRIGGER document_commit_heads_no_delete
        BEFORE DELETE ON document_commit_heads
        BEGIN SELECT RAISE(ABORT, 'business_state_no_delete'); END
        """,
        """
        CREATE TRIGGER idempotency_inbox_no_delete
        BEFORE DELETE ON idempotency_inbox
        BEGIN SELECT RAISE(ABORT, 'business_state_no_delete'); END
        """,
        """
        CREATE TRIGGER event_outbox_no_delete
        BEFORE DELETE ON event_outbox
        BEGIN SELECT RAISE(ABORT, 'business_state_no_delete'); END
        """,
        """
        CREATE TRIGGER event_outbox_dispatch_control_no_delete
        BEFORE DELETE ON event_outbox_dispatch_control
        BEGIN SELECT RAISE(ABORT, 'business_state_no_delete'); END
        """,
        """
        CREATE TRIGGER confirmation_requests_no_delete
        BEFORE DELETE ON confirmation_requests
        BEGIN SELECT RAISE(ABORT, 'business_state_no_delete'); END
        """,
        """
        CREATE TRIGGER deferred_actions_no_delete
        BEFORE DELETE ON deferred_actions
        BEGIN SELECT RAISE(ABORT, 'business_state_no_delete'); END
        """,
        """
        CREATE TRIGGER pipeline_jobs_no_delete
        BEFORE DELETE ON pipeline_jobs
        BEGIN SELECT RAISE(ABORT, 'business_state_no_delete'); END
        """,
        """
        CREATE TRIGGER asset_blobs_no_delete
        BEFORE DELETE ON asset_blobs
        BEGIN SELECT RAISE(ABORT, 'business_state_no_delete'); END
        """,
        """
        CREATE TRIGGER idempotency_inbox_immutable_columns
        BEFORE UPDATE ON idempotency_inbox
        WHEN NOT (
            NEW.scope IS OLD.scope AND
            NEW.idempotency_key IS OLD.idempotency_key AND
            NEW.request_sha256 IS OLD.request_sha256 AND
            NEW.request_bytes IS OLD.request_bytes AND
            NEW.request_schema IS OLD.request_schema AND
            NEW.request_codec_id IS OLD.request_codec_id AND
            NEW.request_codec_version IS OLD.request_codec_version AND
            NEW.actor_id IS OLD.actor_id AND
            NEW.claimed_at IS OLD.claimed_at
        )
        BEGIN SELECT RAISE(ABORT, 'business_identity_immutable'); END
        """,
        """
        CREATE TRIGGER repository_runtime_control_immutable_columns
        BEFORE UPDATE ON repository_runtime_control
        WHEN NOT (
            NEW.singleton IS OLD.singleton AND
            NEW.authority IS OLD.authority AND
            NEW.repository_epoch IS OLD.repository_epoch AND
            NEW.cutover_id IS OLD.cutover_id AND
            NEW.source_admission_revision IS OLD.source_admission_revision AND
            NEW.runtime_generation IS OLD.runtime_generation AND
            NEW.code_digest IS OLD.code_digest AND
            NEW.schema_version IS OLD.schema_version
        )
        BEGIN SELECT RAISE(ABORT, 'business_identity_immutable'); END
        """,
        """
        CREATE TRIGGER repository_projects_immutable_columns
        BEFORE UPDATE ON repository_projects
        WHEN NOT (
            NEW.repository_singleton IS OLD.repository_singleton AND
            NEW.project_id IS OLD.project_id AND
            NEW.created_at IS OLD.created_at
        )
        BEGIN SELECT RAISE(ABORT, 'business_identity_immutable'); END
        """,
        """
        CREATE TRIGGER document_commit_heads_immutable_columns
        BEFORE UPDATE ON document_commit_heads
        WHEN NOT (
            NEW.project_id IS OLD.project_id AND
            NEW.document_id IS OLD.document_id
        )
        BEGIN SELECT RAISE(ABORT, 'business_identity_immutable'); END
        """,
        """
        CREATE TRIGGER event_outbox_dispatch_control_immutable_columns
        BEFORE UPDATE ON event_outbox_dispatch_control
        WHEN NOT (NEW.singleton IS OLD.singleton)
        BEGIN SELECT RAISE(ABORT, 'business_identity_immutable'); END
        """,
        """
        CREATE TRIGGER event_outbox_immutable_columns
        BEFORE UPDATE ON event_outbox
        WHEN NOT (
            NEW.event_id IS OLD.event_id AND
            NEW.previous_event_id IS OLD.previous_event_id AND
            NEW.previous_ordinal IS OLD.previous_ordinal AND
            NEW.commit_id IS OLD.commit_id AND
            NEW.repository_revision IS OLD.repository_revision AND
            NEW.commit_event_count IS OLD.commit_event_count AND
            NEW.operation_id IS OLD.operation_id AND
            NEW.ordinal IS OLD.ordinal AND
            NEW.topic IS OLD.topic AND
            NEW.partition_key IS OLD.partition_key AND
            NEW.event_bytes IS OLD.event_bytes AND
            NEW.event_sha256 IS OLD.event_sha256 AND
            NEW.payload_schema IS OLD.payload_schema AND
            NEW.codec_id IS OLD.codec_id AND
            NEW.codec_version IS OLD.codec_version AND
            NEW.created_at IS OLD.created_at
        )
        BEGIN SELECT RAISE(ABORT, 'business_identity_immutable'); END
        """,
        """
        CREATE TRIGGER confirmation_requests_immutable_columns
        BEFORE UPDATE ON confirmation_requests
        WHEN NOT (
            NEW.confirmation_id IS OLD.confirmation_id AND
            NEW.project_id IS OLD.project_id AND
            NEW.generation IS OLD.generation AND
            NEW.document_id IS OLD.document_id AND
            NEW.actor_id IS OLD.actor_id AND
            NEW.approval_nonce_hmac IS OLD.approval_nonce_hmac AND
            NEW.nonce_key_id IS OLD.nonce_key_id AND
            NEW.request_bytes IS OLD.request_bytes AND
            NEW.request_sha256 IS OLD.request_sha256 AND
            NEW.request_schema IS OLD.request_schema AND
            NEW.request_codec_id IS OLD.request_codec_id AND
            NEW.request_codec_version IS OLD.request_codec_version AND
            NEW.expires_at IS OLD.expires_at AND
            NEW.created_at IS OLD.created_at
        )
        BEGIN SELECT RAISE(ABORT, 'business_identity_immutable'); END
        """,
        """
        CREATE TRIGGER deferred_actions_immutable_columns
        BEFORE UPDATE ON deferred_actions
        WHEN NOT (
            NEW.action_id IS OLD.action_id AND
            NEW.idempotency_scope IS OLD.idempotency_scope AND
            NEW.idempotency_key IS OLD.idempotency_key AND
            NEW.project_id IS OLD.project_id AND
            NEW.generation IS OLD.generation AND
            NEW.document_id IS OLD.document_id AND
            NEW.origin_commit_id IS OLD.origin_commit_id AND
            NEW.commit_action_count IS OLD.commit_action_count AND
            NEW.ordinal IS OLD.ordinal AND
            NEW.action_kind IS OLD.action_kind AND
            NEW.action_bytes IS OLD.action_bytes AND
            NEW.action_sha256 IS OLD.action_sha256 AND
            NEW.payload_schema IS OLD.payload_schema AND
            NEW.codec_id IS OLD.codec_id AND
            NEW.codec_version IS OLD.codec_version AND
            NEW.replay_policy IS OLD.replay_policy AND
            NEW.idempotency_policy IS OLD.idempotency_policy AND
            NEW.max_attempts IS OLD.max_attempts AND
            NEW.created_at IS OLD.created_at
        )
        BEGIN SELECT RAISE(ABORT, 'business_identity_immutable'); END
        """,
        """
        CREATE TRIGGER pipeline_jobs_immutable_columns
        BEFORE UPDATE ON pipeline_jobs
        WHEN NOT (
            NEW.job_id IS OLD.job_id AND
            NEW.idempotency_key IS OLD.idempotency_key AND
            NEW.project_id IS OLD.project_id AND
            NEW.generation IS OLD.generation AND
            NEW.document_id IS OLD.document_id AND
            NEW.pipeline_id IS OLD.pipeline_id AND
            NEW.input_commit_id IS OLD.input_commit_id AND
            NEW.intent_type IS OLD.intent_type AND
            NEW.intent_kind IS OLD.intent_kind AND
            NEW.expected_state_revision IS OLD.expected_state_revision AND
            NEW.job_bytes IS OLD.job_bytes AND
            NEW.job_sha256 IS OLD.job_sha256 AND
            NEW.payload_schema IS OLD.payload_schema AND
            NEW.codec_id IS OLD.codec_id AND
            NEW.codec_version IS OLD.codec_version AND
            NEW.replay_policy IS OLD.replay_policy AND
            NEW.max_attempts IS OLD.max_attempts AND
            NEW.created_at IS OLD.created_at
        )
        BEGIN SELECT RAISE(ABORT, 'business_identity_immutable'); END
        """,
        """
        CREATE TRIGGER asset_blobs_immutable_columns
        BEFORE UPDATE ON asset_blobs
        WHEN NOT (
            NEW.asset_sha256 IS OLD.asset_sha256 AND
            NEW.content_key IS OLD.content_key AND
            NEW.media_type IS OLD.media_type AND
            NEW.extension IS OLD.extension AND
            NEW.byte_length IS OLD.byte_length AND
            NEW.created_at IS OLD.created_at
        )
        BEGIN SELECT RAISE(ABORT, 'business_identity_immutable'); END
        """,
        """
        CREATE TRIGGER document_asset_refs_available_insert
        BEFORE INSERT ON document_asset_refs
        WHEN NOT EXISTS (
            SELECT 1 FROM asset_blobs
            WHERE asset_sha256 = NEW.asset_sha256
              AND status = 'available'
              AND (NEW.ref_size IS NULL OR byte_length = NEW.ref_size)
              AND (NEW.ref_mime IS NULL OR media_type IS NEW.ref_mime)
              AND (NEW.ref_ext IS NULL OR extension IS NEW.ref_ext)
        )
        BEGIN SELECT RAISE(ABORT, 'business_asset_reference_unavailable'); END
        """,
        """
        CREATE TRIGGER document_asset_refs_available_update
        BEFORE UPDATE ON document_asset_refs
        WHEN NOT EXISTS (
            SELECT 1 FROM asset_blobs
            WHERE asset_sha256 = NEW.asset_sha256
              AND status = 'available'
              AND (NEW.ref_size IS NULL OR byte_length = NEW.ref_size)
              AND (NEW.ref_mime IS NULL OR media_type IS NEW.ref_mime)
              AND (NEW.ref_ext IS NULL OR extension IS NEW.ref_ext)
        )
        BEGIN SELECT RAISE(ABORT, 'business_asset_reference_unavailable'); END
        """,
        """
        CREATE TRIGGER asset_blobs_referenced_gc_guard
        BEFORE UPDATE OF status ON asset_blobs
        WHEN NEW.status != 'available' AND EXISTS (
            SELECT 1 FROM document_asset_refs
            WHERE asset_sha256 = OLD.asset_sha256
        )
        BEGIN SELECT RAISE(ABORT, 'business_asset_still_referenced'); END
        """,
        """
        CREATE TRIGGER deferred_actions_attempt_pointer_guard
        BEFORE UPDATE ON deferred_actions
        WHEN NOT (
            (OLD.current_attempt_id IS NULL AND
                NEW.current_attempt_id IS NULL AND
                NEW.attempt_count = OLD.attempt_count AND
                NEW.last_attempt_id IS OLD.last_attempt_id AND
                NEW.last_attempt_number IS OLD.last_attempt_number) OR
            (OLD.current_attempt_id IS NULL AND
                NEW.current_attempt_id IS NOT NULL AND
                OLD.attempt_count < 100 AND
                NEW.attempt_count = OLD.attempt_count + 1 AND
                NEW.status = 'running' AND
                NEW.current_attempt_id IS NOT OLD.last_attempt_id AND
                NEW.last_attempt_id IS OLD.last_attempt_id AND
                NEW.last_attempt_number IS OLD.last_attempt_number) OR
            (OLD.current_attempt_id IS NOT NULL AND
                NEW.current_attempt_id IS OLD.current_attempt_id AND
                NEW.current_attempt_started_at IS
                    OLD.current_attempt_started_at AND
                NEW.attempt_count = OLD.attempt_count AND
                NEW.last_attempt_id IS OLD.last_attempt_id AND
                NEW.last_attempt_number IS OLD.last_attempt_number AND
                NEW.status = 'running') OR
            (OLD.current_attempt_id IS NOT NULL AND
                NEW.current_attempt_id IS NULL AND
                NEW.attempt_count = OLD.attempt_count AND
                NEW.last_attempt_id IS OLD.current_attempt_id AND
                NEW.last_attempt_number = OLD.attempt_count AND
                NEW.status != 'running')
        )
        BEGIN SELECT RAISE(ABORT, 'business_attempt_pointer_invalid'); END
        """,
        """
        CREATE TRIGGER deferred_action_attempts_parent_tail_guard
        BEFORE INSERT ON deferred_action_attempts
        WHEN NOT EXISTS (
            SELECT 1 FROM deferred_actions
            WHERE action_id = NEW.action_id
              AND current_attempt_id IS NULL
              AND attempt_count = NEW.attempt_number
              AND last_attempt_id = NEW.attempt_id
              AND last_attempt_number = NEW.attempt_number
        )
        BEGIN SELECT RAISE(ABORT, 'business_attempt_pointer_invalid'); END
        """,
        """
        CREATE TRIGGER pipeline_jobs_attempt_pointer_guard
        BEFORE UPDATE ON pipeline_jobs
        WHEN NOT (
            (OLD.current_attempt_id IS NULL AND
                NEW.current_attempt_id IS NULL AND
                NEW.attempt_count = OLD.attempt_count AND
                NEW.last_attempt_id IS OLD.last_attempt_id AND
                NEW.last_attempt_number IS OLD.last_attempt_number) OR
            (OLD.current_attempt_id IS NULL AND
                NEW.current_attempt_id IS NOT NULL AND
                OLD.attempt_count < 100 AND
                NEW.attempt_count = OLD.attempt_count + 1 AND
                NEW.status = 'running' AND
                NEW.current_attempt_id IS NOT OLD.last_attempt_id AND
                NEW.last_attempt_id IS OLD.last_attempt_id AND
                NEW.last_attempt_number IS OLD.last_attempt_number) OR
            (OLD.current_attempt_id IS NOT NULL AND
                NEW.current_attempt_id IS OLD.current_attempt_id AND
                NEW.current_attempt_started_at IS
                    OLD.current_attempt_started_at AND
                NEW.attempt_count = OLD.attempt_count AND
                NEW.last_attempt_id IS OLD.last_attempt_id AND
                NEW.last_attempt_number IS OLD.last_attempt_number AND
                NEW.status IN ('running', 'cancel_requested')) OR
            (OLD.current_attempt_id IS NOT NULL AND
                NEW.current_attempt_id IS NULL AND
                NEW.attempt_count = OLD.attempt_count AND
                NEW.last_attempt_id IS OLD.current_attempt_id AND
                NEW.last_attempt_number = OLD.attempt_count AND
                NEW.status != 'running')
        )
        BEGIN SELECT RAISE(ABORT, 'business_attempt_pointer_invalid'); END
        """,
        """
        CREATE TRIGGER pipeline_job_attempts_parent_tail_guard
        BEFORE INSERT ON pipeline_job_attempts
        WHEN NOT EXISTS (
            SELECT 1 FROM pipeline_jobs
            WHERE job_id = NEW.job_id
              AND current_attempt_id IS NULL
              AND attempt_count = NEW.attempt_number
              AND last_attempt_id = NEW.attempt_id
              AND last_attempt_number = NEW.attempt_number
        )
        BEGIN SELECT RAISE(ABORT, 'business_attempt_pointer_invalid'); END
        """,
        """
        CREATE TRIGGER repository_projects_repair_mode_guard
        BEFORE UPDATE OF status ON repository_projects
        WHEN (OLD.status = 'repair_required' OR
              NEW.status = 'repair_required') AND NOT EXISTS (
            SELECT 1 FROM repository_runtime_control
            WHERE singleton = 1 AND authority_state = 'repair_required'
        )
        BEGIN SELECT RAISE(ABORT, 'business_repair_mode_required'); END
        """,
        """
        CREATE TRIGGER document_commit_heads_repair_mode_guard
        BEFORE UPDATE OF status ON document_commit_heads
        WHEN (OLD.status = 'repair_required' OR
              NEW.status = 'repair_required') AND NOT EXISTS (
            SELECT 1 FROM repository_runtime_control
            WHERE singleton = 1 AND authority_state = 'repair_required'
        )
        BEGIN SELECT RAISE(ABORT, 'business_repair_mode_required'); END
        """,
        """
        CREATE TRIGGER idempotency_inbox_repair_mode_guard
        BEFORE UPDATE OF status ON idempotency_inbox
        WHEN (OLD.status = 'repair_required' OR
              NEW.status = 'repair_required') AND NOT EXISTS (
            SELECT 1 FROM repository_runtime_control
            WHERE singleton = 1 AND authority_state = 'repair_required'
        )
        BEGIN SELECT RAISE(ABORT, 'business_repair_mode_required'); END
        """,
        """
        CREATE TRIGGER confirmation_requests_repair_mode_guard
        BEFORE UPDATE OF status ON confirmation_requests
        WHEN (OLD.status = 'repair_required' OR
              NEW.status = 'repair_required') AND NOT EXISTS (
            SELECT 1 FROM repository_runtime_control
            WHERE singleton = 1 AND authority_state = 'repair_required'
        )
        BEGIN SELECT RAISE(ABORT, 'business_repair_mode_required'); END
        """,
        """
        CREATE TRIGGER deferred_actions_repair_mode_guard
        BEFORE UPDATE OF status ON deferred_actions
        WHEN (OLD.status = 'repair_required' OR
              NEW.status = 'repair_required') AND NOT EXISTS (
            SELECT 1 FROM repository_runtime_control
            WHERE singleton = 1 AND authority_state = 'repair_required'
        )
        BEGIN SELECT RAISE(ABORT, 'business_repair_mode_required'); END
        """,
        """
        CREATE TRIGGER pipeline_jobs_repair_mode_guard
        BEFORE UPDATE OF status ON pipeline_jobs
        WHEN (OLD.status = 'repair_required' OR
              NEW.status = 'repair_required') AND NOT EXISTS (
            SELECT 1 FROM repository_runtime_control
            WHERE singleton = 1 AND authority_state = 'repair_required'
        )
        BEGIN SELECT RAISE(ABORT, 'business_repair_mode_required'); END
        """,
        """
        CREATE TRIGGER pipeline_job_transitions_repair_mode_guard
        BEFORE INSERT ON pipeline_job_transitions
        WHEN (NEW.before_status = 'repair_required' OR
              NEW.after_status = 'repair_required') AND NOT EXISTS (
            SELECT 1 FROM repository_runtime_control
            WHERE singleton = 1 AND authority_state = 'repair_required'
        )
        BEGIN SELECT RAISE(ABORT, 'business_repair_mode_required'); END
        """,
        """
        CREATE TRIGGER asset_blobs_repair_mode_guard
        BEFORE UPDATE OF status ON asset_blobs
        WHEN (OLD.status = 'repair_required' OR
              NEW.status = 'repair_required') AND NOT EXISTS (
            SELECT 1 FROM repository_runtime_control
            WHERE singleton = 1 AND authority_state = 'repair_required'
        )
        BEGIN SELECT RAISE(ABORT, 'business_repair_mode_required'); END
        """,
        """
        CREATE TRIGGER idempotency_inbox_status_edges
        BEFORE UPDATE OF status ON idempotency_inbox
        WHEN NEW.status != OLD.status AND NOT (
            (OLD.status = 'claimed' AND NEW.status IN (
                'committed', 'released', 'repair_required'
            )) OR
            (OLD.status = 'released' AND NEW.status IN (
                'claimed', 'repair_required'
            )) OR
            (OLD.status = 'committed' AND NEW.status = 'repair_required') OR
            (OLD.status = 'repair_required' AND NEW.status = 'released')
        )
        BEGIN SELECT RAISE(ABORT, 'business_status_transition_invalid'); END
        """,
        """
        CREATE TRIGGER event_outbox_status_edges
        BEFORE UPDATE OF status ON event_outbox
        WHEN NEW.status != OLD.status AND NOT (
            (OLD.status IN ('pending', 'retry') AND
                NEW.status IN ('leased', 'dead')) OR
            (OLD.status = 'leased' AND NEW.status IN (
                'published', 'retry', 'outcome_unknown', 'dead'
            )) OR
            (OLD.status = 'outcome_unknown' AND NEW.status IN (
                'published', 'retry', 'dead'
            ))
        )
        BEGIN SELECT RAISE(ABORT, 'business_status_transition_invalid'); END
        """,
        """
        CREATE TRIGGER confirmation_requests_status_edges
        BEFORE UPDATE OF status ON confirmation_requests
        WHEN NEW.status != OLD.status AND NOT (
            (OLD.status = 'pending' AND NEW.status IN (
                'reserved', 'cancelled', 'expired', 'frozen',
                'repair_required'
            )) OR
            (OLD.status = 'reserved' AND NEW.status IN (
                'consumed', 'rejected', 'cancelled', 'expired', 'frozen',
                'repair_required'
            )) OR
            (OLD.status = 'frozen' AND NEW.status IN (
                'reserved', 'cancelled', 'expired', 'repair_required'
            )) OR
            (OLD.status = 'repair_required' AND NEW.status = 'frozen')
        )
        BEGIN SELECT RAISE(ABORT, 'business_status_transition_invalid'); END
        """,
        """
        CREATE TRIGGER deferred_actions_status_edges
        BEFORE UPDATE OF status ON deferred_actions
        WHEN NEW.status != OLD.status AND NOT (
            (OLD.status IN ('queued', 'retry') AND
                NEW.status IN ('running', 'cancelled', 'repair_required')) OR
            (OLD.status = 'running' AND NEW.status IN (
                'succeeded', 'retry', 'outcome_unknown', 'failed_terminal',
                'cancelled', 'repair_required'
            )) OR
            (OLD.status = 'outcome_unknown' AND NEW.status IN (
                'succeeded', 'retry', 'failed_terminal', 'repair_required'
            )) OR
            (OLD.status = 'repair_required' AND NEW.status IN (
                'outcome_unknown', 'failed_terminal', 'cancelled'
            ))
        )
        BEGIN SELECT RAISE(ABORT, 'business_status_transition_invalid'); END
        """,
        """
        CREATE TRIGGER asset_blobs_status_edges
        BEFORE UPDATE OF status ON asset_blobs
        WHEN NEW.status != OLD.status AND NOT (
            (OLD.status = 'staged' AND NEW.status IN (
                'available', 'quarantined', 'tombstoned', 'repair_required'
            )) OR
            (OLD.status = 'available' AND NEW.status IN (
                'quarantined', 'deleting', 'repair_required'
            )) OR
            (OLD.status = 'quarantined' AND NEW.status IN (
                'available', 'deleting', 'tombstoned', 'repair_required'
            )) OR
            (OLD.status = 'deleting' AND NEW.status IN (
                'tombstoned', 'repair_required'
            )) OR
            (OLD.status = 'repair_required' AND NEW.status IN (
                'quarantined', 'deleting', 'tombstoned'
            ))
        )
        BEGIN SELECT RAISE(ABORT, 'business_status_transition_invalid'); END
        """,
        """
        CREATE TRIGGER repository_runtime_control_revision_guard
        BEFORE UPDATE ON repository_runtime_control
        WHEN NOT (
            (
                OLD.repository_revision < 9223372036854775807 AND
                NEW.repository_revision = OLD.repository_revision + 1 AND
                NEW.state_revision = OLD.state_revision AND
                NEW.authority_state = OLD.authority_state
            ) OR (
                NEW.repository_revision = OLD.repository_revision AND
                OLD.state_revision < 9223372036854775807 AND
                NEW.state_revision = OLD.state_revision + 1
            )
        ) OR NEW.updated_at < OLD.updated_at
        BEGIN SELECT RAISE(ABORT, 'business_revision_invalid'); END
        """,
        """
        CREATE TRIGGER repository_projects_revision_guard
        BEFORE UPDATE ON repository_projects
        WHEN OLD.project_revision >= 9223372036854775807 OR
             NEW.project_revision != OLD.project_revision + 1 OR
             NEW.generation < OLD.generation OR
             NEW.updated_at < OLD.updated_at OR NOT (
                NEW.status = OLD.status OR
                (OLD.status = 'live' AND
                    NEW.status IN ('deleted', 'repair_required')) OR
                (OLD.status = 'deleted' AND NEW.status = 'live' AND
                    NEW.generation > OLD.generation) OR
                (OLD.status = 'repair_required' AND
                    NEW.status IN ('live', 'deleted'))
             )
        BEGIN SELECT RAISE(ABORT, 'business_revision_invalid'); END
        """,
        """
        CREATE TRIGGER document_commit_heads_revision_guard
        BEFORE UPDATE ON document_commit_heads
        WHEN OLD.head_revision >= 9223372036854775807 OR
             NEW.head_revision != OLD.head_revision + 1 OR
             NEW.generation < OLD.generation OR
             NEW.updated_at < OLD.updated_at OR
             (NEW.generation = OLD.generation AND
                NEW.document_version < OLD.document_version) OR NOT (
                NEW.status = OLD.status OR
                (OLD.status = 'live' AND
                    NEW.status IN ('deleted', 'repair_required')) OR
                (OLD.status = 'deleted' AND NEW.status = 'live' AND
                    NEW.generation > OLD.generation) OR
                (OLD.status = 'repair_required' AND
                    NEW.status IN ('live', 'deleted'))
             )
        BEGIN SELECT RAISE(ABORT, 'business_revision_invalid'); END
        """,
        """
        CREATE TRIGGER idempotency_inbox_revision_guard
        BEFORE UPDATE ON idempotency_inbox
        WHEN OLD.state_revision >= 9223372036854775807 OR
             NEW.state_revision != OLD.state_revision + 1 OR
             NEW.claim_fence < OLD.claim_fence OR
             NEW.updated_at < OLD.updated_at
        BEGIN SELECT RAISE(ABORT, 'business_revision_invalid'); END
        """,
        """
        CREATE TRIGGER idempotency_inbox_committed_result_guard
        BEFORE UPDATE ON idempotency_inbox
        WHEN OLD.status = 'committed' AND NOT (
            NEW.result_kind IS OLD.result_kind AND
            NEW.commit_id IS OLD.commit_id AND
            NEW.confirmation_id IS OLD.confirmation_id AND
            NEW.response_bytes IS OLD.response_bytes AND
            NEW.response_sha256 IS OLD.response_sha256 AND
            NEW.response_schema IS OLD.response_schema AND
            NEW.response_codec_id IS OLD.response_codec_id AND
            NEW.response_codec_version IS OLD.response_codec_version
        )
        BEGIN SELECT RAISE(ABORT, 'business_terminal_result_immutable'); END
        """,
        """
        CREATE TRIGGER event_outbox_revision_guard
        BEFORE UPDATE ON event_outbox
        WHEN OLD.state_revision >= 9223372036854775807 OR
             NEW.state_revision != OLD.state_revision + 1 OR
             NEW.attempt_count < OLD.attempt_count OR
             (NEW.lease_fence IS NOT NULL AND OLD.lease_fence IS NOT NULL AND
                NEW.lease_fence < OLD.lease_fence) OR
             NEW.updated_at < OLD.updated_at
        BEGIN SELECT RAISE(ABORT, 'business_revision_invalid'); END
        """,
        """
        CREATE TRIGGER event_outbox_terminal_guard
        BEFORE UPDATE ON event_outbox
        WHEN OLD.status IN ('published', 'dead')
        BEGIN SELECT RAISE(ABORT, 'business_terminal_result_immutable'); END
        """,
        """
        CREATE TRIGGER event_outbox_dispatch_control_revision_guard
        BEFORE UPDATE ON event_outbox_dispatch_control
        WHEN OLD.state_revision >= 9223372036854775807 OR
             NEW.state_revision != OLD.state_revision + 1 OR
             (NEW.lease_fence IS NOT NULL AND OLD.lease_fence IS NOT NULL AND
                NEW.lease_fence < OLD.lease_fence) OR
             NEW.published_repository_revision <
                OLD.published_repository_revision OR
             (NEW.published_repository_revision =
                OLD.published_repository_revision AND
                NEW.published_ordinal < OLD.published_ordinal) OR
             NEW.updated_at < OLD.updated_at
        BEGIN SELECT RAISE(ABORT, 'business_revision_invalid'); END
        """,
        """
        CREATE TRIGGER confirmation_requests_revision_guard
        BEFORE UPDATE ON confirmation_requests
        WHEN OLD.state_revision >= 9223372036854775807 OR
             NEW.state_revision != OLD.state_revision + 1 OR
             (NEW.reservation_fence IS NOT NULL AND
                OLD.reservation_fence IS NOT NULL AND
                NEW.reservation_fence < OLD.reservation_fence) OR
             NEW.updated_at < OLD.updated_at
        BEGIN SELECT RAISE(ABORT, 'business_revision_invalid'); END
        """,
        """
        CREATE TRIGGER confirmation_requests_terminal_guard
        BEFORE UPDATE ON confirmation_requests
        WHEN OLD.status IN (
            'consumed', 'rejected', 'cancelled', 'expired'
        )
        BEGIN SELECT RAISE(ABORT, 'business_terminal_result_immutable'); END
        """,
        """
        CREATE TRIGGER deferred_actions_revision_guard
        BEFORE UPDATE ON deferred_actions
        WHEN OLD.state_revision >= 9223372036854775807 OR
             NEW.state_revision != OLD.state_revision + 1 OR
             NEW.attempt_count < OLD.attempt_count OR
             (NEW.lease_fence IS NOT NULL AND OLD.lease_fence IS NOT NULL AND
                NEW.lease_fence < OLD.lease_fence) OR
             NEW.updated_at < OLD.updated_at
        BEGIN SELECT RAISE(ABORT, 'business_revision_invalid'); END
        """,
        """
        CREATE TRIGGER deferred_actions_terminal_guard
        BEFORE UPDATE ON deferred_actions
        WHEN OLD.status IN ('succeeded', 'failed_terminal', 'cancelled')
        BEGIN SELECT RAISE(ABORT, 'business_terminal_result_immutable'); END
        """,
        """
        CREATE TRIGGER pipeline_jobs_revision_guard
        BEFORE UPDATE ON pipeline_jobs
        WHEN OLD.state_revision >= 9223372036854775807 OR
             NEW.state_revision != OLD.state_revision + 1 OR
             NEW.attempt_count < OLD.attempt_count OR
             (NEW.lease_fence IS NOT NULL AND OLD.lease_fence IS NOT NULL AND
                NEW.lease_fence < OLD.lease_fence) OR
             NEW.updated_at < OLD.updated_at
        BEGIN SELECT RAISE(ABORT, 'business_revision_invalid'); END
        """,
        """
        CREATE TRIGGER pipeline_jobs_terminal_guard
        BEFORE UPDATE ON pipeline_jobs
        WHEN OLD.status IN ('cancelled', 'succeeded', 'failed')
        BEGIN SELECT RAISE(ABORT, 'business_terminal_result_immutable'); END
        """,
        """
        CREATE TRIGGER asset_blobs_revision_guard
        BEFORE UPDATE ON asset_blobs
        WHEN OLD.state_revision >= 9223372036854775807 OR
             NEW.state_revision != OLD.state_revision + 1 OR
             NEW.updated_at < OLD.updated_at
        BEGIN SELECT RAISE(ABORT, 'business_revision_invalid'); END
        """,
        """
        CREATE TRIGGER asset_blobs_terminal_guard
        BEFORE UPDATE ON asset_blobs
        WHEN OLD.status = 'tombstoned'
        BEGIN SELECT RAISE(ABORT, 'business_terminal_result_immutable'); END
        """,
        """
        CREATE TRIGGER business_commits_no_update
        BEFORE UPDATE ON business_commits
        BEGIN SELECT RAISE(ABORT, 'business_history_append_only'); END
        """,
        """
        CREATE TRIGGER business_commits_no_delete
        BEFORE DELETE ON business_commits
        BEGIN SELECT RAISE(ABORT, 'business_history_append_only'); END
        """,
        """
        CREATE TRIGGER business_operations_no_update
        BEFORE UPDATE ON business_operations
        BEGIN SELECT RAISE(ABORT, 'business_history_append_only'); END
        """,
        """
        CREATE TRIGGER business_operations_no_delete
        BEFORE DELETE ON business_operations
        BEGIN SELECT RAISE(ABORT, 'business_history_append_only'); END
        """,
        """
        CREATE TRIGGER business_operation_targets_no_update
        BEFORE UPDATE ON business_operation_targets
        BEGIN SELECT RAISE(ABORT, 'business_history_append_only'); END
        """,
        """
        CREATE TRIGGER business_operation_targets_no_delete
        BEFORE DELETE ON business_operation_targets
        BEGIN SELECT RAISE(ABORT, 'business_history_append_only'); END
        """,
        """
        CREATE TRIGGER document_asset_refs_no_update
        BEFORE UPDATE ON document_asset_refs
        BEGIN SELECT RAISE(ABORT, 'business_history_append_only'); END
        """,
        """
        CREATE TRIGGER document_asset_refs_no_delete
        BEFORE DELETE ON document_asset_refs
        BEGIN SELECT RAISE(ABORT, 'business_history_append_only'); END
        """,
        """
        CREATE TRIGGER deferred_action_attempts_no_update
        BEFORE UPDATE ON deferred_action_attempts
        BEGIN SELECT RAISE(ABORT, 'business_history_append_only'); END
        """,
        """
        CREATE TRIGGER deferred_action_attempts_no_delete
        BEFORE DELETE ON deferred_action_attempts
        BEGIN SELECT RAISE(ABORT, 'business_history_append_only'); END
        """,
        """
        CREATE TRIGGER pipeline_job_attempts_no_update
        BEFORE UPDATE ON pipeline_job_attempts
        BEGIN SELECT RAISE(ABORT, 'business_history_append_only'); END
        """,
        """
        CREATE TRIGGER pipeline_job_attempts_no_delete
        BEFORE DELETE ON pipeline_job_attempts
        BEGIN SELECT RAISE(ABORT, 'business_history_append_only'); END
        """,
        """
        CREATE TRIGGER pipeline_job_transitions_no_update
        BEFORE UPDATE ON pipeline_job_transitions
        BEGIN SELECT RAISE(ABORT, 'business_history_append_only'); END
        """,
        """
        CREATE TRIGGER pipeline_job_transitions_no_delete
        BEFORE DELETE ON pipeline_job_transitions
        BEGIN SELECT RAISE(ABORT, 'business_history_append_only'); END
        """,
    ),
)


def _business_transaction_statement(prefix: str) -> str:
    """Return one frozen v6 statement for exact trigger/table reconstruction."""

    matches = tuple(
        statement
        for statement in BUSINESS_TRANSACTION_MIGRATION.statements
        if normalize_schema_sql(statement).startswith(prefix)
    )
    if len(matches) != 1:
        raise RuntimeError("frozen business statement lookup is not unique")
    return matches[0]


_V7_DOCUMENT_COMMIT_HEADS_TABLE = _business_transaction_statement(
    "CREATE TABLE document_commit_heads ("
).replace(
    "CREATE TABLE document_commit_heads (",
    "CREATE TABLE document_commit_heads_v7 (",
    1,
).replace(
    "document_version, repository_revision, after_root_sha256,",
    "document_version, head_revision, after_root_sha256,",
    1,
)

LOCAL_REVISION_PREFLIGHT_PREDICATE = """
(
    (
        NOT EXISTS (SELECT 1 FROM business_commits) AND
        NOT EXISTS (SELECT 1 FROM repository_projects) AND
        NOT EXISTS (SELECT 1 FROM document_commit_heads)
    ) OR (
        (SELECT count(*) FROM business_commits) >= 1 AND
        (
            SELECT count(*) FROM (
                SELECT project_id, generation, document_id
                FROM business_commits
                GROUP BY project_id, generation, document_id
            )
        ) = 1 AND
        (SELECT count(*) FROM repository_projects) = 1 AND
        (SELECT count(*) FROM document_commit_heads) = 1 AND
        (SELECT min(repository_revision) FROM business_commits) = 1 AND
        (SELECT max(repository_revision) FROM business_commits) =
            (SELECT count(*) FROM business_commits) AND
        EXISTS (
            SELECT 1
            FROM business_commits AS business_commit
            JOIN repository_projects AS repository_project
              ON repository_project.project_id = business_commit.project_id
             AND repository_project.generation = business_commit.generation
            JOIN document_commit_heads AS document_head
              ON document_head.commit_id = business_commit.commit_id
             AND document_head.project_id = business_commit.project_id
             AND document_head.generation = business_commit.generation
             AND document_head.document_id = business_commit.document_id
            JOIN repository_runtime_control AS runtime_control
              ON runtime_control.singleton = 1
            WHERE business_commit.repository_revision = (
                    SELECT max(repository_revision) FROM business_commits
                )
              AND repository_project.project_revision =
                    business_commit.repository_revision
              AND document_head.head_revision =
                    business_commit.repository_revision
              AND repository_project.current_snapshot_sha256 =
                    business_commit.after_root_sha256
              AND document_head.business_root_sha256 =
                    business_commit.after_root_sha256
              AND runtime_control.repository_revision =
                    business_commit.repository_revision
        )
    )
)
"""


LOCAL_REVISION_MIGRATION = Migration(
    version=7,
    name="business_local_revision_separation_v1",
    statements=(
        """
        CREATE TABLE schema_v7_local_revision_preflight (
            verified INTEGER NOT NULL
                CONSTRAINT schema_v7_local_revision_precondition
                CHECK(verified = 1)
        ) STRICT
        """,
        "INSERT INTO schema_v7_local_revision_preflight(verified) "
        "SELECT CASE WHEN " + LOCAL_REVISION_PREFLIGHT_PREDICATE +
        " THEN 1 ELSE 0 END",
        """
        ALTER TABLE business_commits
        ADD COLUMN project_revision INTEGER CHECK(
            project_revision IS NULL OR
            project_revision BETWEEN 1 AND 9223372036854775807
        )
        """,
        """
        ALTER TABLE business_commits
        ADD COLUMN head_revision INTEGER CHECK(
            head_revision IS NULL OR
            head_revision BETWEEN 1 AND 9223372036854775807
        )
        """,
        "DROP TRIGGER business_commits_no_update",
        """
        UPDATE business_commits
        SET project_revision = repository_revision,
            head_revision = repository_revision
        """,
        """
        INSERT INTO schema_v7_local_revision_preflight(verified)
        SELECT CASE WHEN NOT EXISTS (
            SELECT 1 FROM business_commits
            WHERE project_revision IS NULL OR head_revision IS NULL
        ) THEN 1 ELSE 0 END
        """,
        """
        CREATE UNIQUE INDEX business_commits_project_local_revision
        ON business_commits(project_id, generation, project_revision)
        """,
        """
        CREATE UNIQUE INDEX business_commits_project_local_root
        ON business_commits(
            project_id, generation, project_revision, after_root_sha256
        )
        """,
        """
        CREATE UNIQUE INDEX business_commits_head_local_revision
        ON business_commits(
            project_id, generation, document_id, head_revision
        )
        """,
        """
        CREATE UNIQUE INDEX business_commits_head_local_identity
        ON business_commits(
            commit_id, project_id, generation, document_id,
            document_version, head_revision, after_root_sha256,
            operation_tail_sha256, asset_projection_sha256,
            asset_reference_count, entity_count
        )
        """,
        """
        CREATE TRIGGER business_commits_local_revision_insert_guard
        BEFORE INSERT ON business_commits
        WHEN NEW.project_revision IS NULL OR NEW.head_revision IS NULL OR
             NEW.project_revision NOT BETWEEN 1 AND 9223372036854775807 OR
             NEW.head_revision NOT BETWEEN 1 AND 9223372036854775807 OR
             NEW.project_revision != COALESCE((
                 SELECT max(project_revision) + 1
                 FROM business_commits
                 WHERE project_id = NEW.project_id
                   AND generation = NEW.generation
             ), 1) OR
             NEW.head_revision != COALESCE((
                 SELECT max(head_revision) + 1
                 FROM business_commits
                 WHERE project_id = NEW.project_id
                   AND generation = NEW.generation
                   AND document_id = NEW.document_id
             ), 1)
        BEGIN SELECT RAISE(ABORT, 'business_local_revision_invalid'); END
        """,
        """
        CREATE TRIGGER business_commits_local_revision_update_guard
        BEFORE UPDATE OF project_revision, head_revision ON business_commits
        WHEN NEW.project_revision IS NULL OR NEW.head_revision IS NULL OR
             NEW.project_revision NOT BETWEEN 1 AND 9223372036854775807 OR
             NEW.head_revision NOT BETWEEN 1 AND 9223372036854775807
        BEGIN SELECT RAISE(ABORT, 'business_local_revision_invalid'); END
        """,
        _business_transaction_statement(
            "CREATE TRIGGER business_commits_no_update"
        ),
        "DROP TRIGGER business_operation_targets_finalized_guard",
        "DROP TRIGGER repository_runtime_control_repair_exit_guard",
        _V7_DOCUMENT_COMMIT_HEADS_TABLE,
        """
        INSERT INTO document_commit_heads_v7(
            project_id, generation, document_id, document_version,
            commit_id, head_revision, status, current_snapshot_sha256,
            current_project_snapshot_sha256, business_root_sha256,
            operation_tail_sha256, asset_projection_sha256,
            asset_reference_count, entity_count, updated_at
        )
        SELECT
            project_id, generation, document_id, document_version,
            commit_id, head_revision, status, current_snapshot_sha256,
            current_project_snapshot_sha256, business_root_sha256,
            operation_tail_sha256, asset_projection_sha256,
            asset_reference_count, entity_count, updated_at
        FROM document_commit_heads
        """,
        "DROP TABLE document_commit_heads",
        "ALTER TABLE document_commit_heads_v7 RENAME TO document_commit_heads",
        _business_transaction_statement(
            "CREATE TRIGGER document_commit_heads_target_count_insert"
        ),
        _business_transaction_statement(
            "CREATE TRIGGER document_commit_heads_target_count_update"
        ),
        _business_transaction_statement(
            "CREATE TRIGGER document_commit_heads_effect_closure_insert"
        ),
        _business_transaction_statement(
            "CREATE TRIGGER document_commit_heads_effect_closure_update"
        ),
        _business_transaction_statement(
            "CREATE TRIGGER document_commit_heads_no_delete"
        ),
        _business_transaction_statement(
            "CREATE TRIGGER document_commit_heads_immutable_columns"
        ),
        _business_transaction_statement(
            "CREATE TRIGGER document_commit_heads_repair_mode_guard"
        ),
        _business_transaction_statement(
            "CREATE TRIGGER document_commit_heads_revision_guard"
        ),
        _business_transaction_statement(
            "CREATE TRIGGER business_operation_targets_finalized_guard"
        ),
        _business_transaction_statement(
            "CREATE TRIGGER repository_runtime_control_repair_exit_guard"
        ),
        """
        CREATE TRIGGER repository_projects_local_revision_insert_guard
        BEFORE INSERT ON repository_projects
        WHEN NEW.status = 'live' AND NOT EXISTS (
            SELECT 1 FROM business_commits
            WHERE project_id = NEW.project_id
              AND generation = NEW.generation
              AND project_revision = NEW.project_revision
              AND after_root_sha256 = NEW.current_snapshot_sha256
        )
        BEGIN
            SELECT RAISE(ABORT, 'business_local_revision_binding_mismatch');
        END
        """,
        """
        CREATE TRIGGER repository_projects_local_revision_update_guard
        BEFORE UPDATE OF project_revision, current_snapshot_sha256, status
        ON repository_projects
        WHEN NEW.status = 'live' AND NOT EXISTS (
            SELECT 1 FROM business_commits
            WHERE project_id = NEW.project_id
              AND generation = NEW.generation
              AND project_revision = NEW.project_revision
              AND after_root_sha256 = NEW.current_snapshot_sha256
        )
        BEGIN
            SELECT RAISE(ABORT, 'business_local_revision_binding_mismatch');
        END
        """,
        """
        CREATE TRIGGER document_commit_heads_local_revision_insert_guard
        BEFORE INSERT ON document_commit_heads
        WHEN NOT EXISTS (
            SELECT 1
            FROM business_commits AS business_commit
            JOIN repository_projects AS repository_project
              ON repository_project.project_id = business_commit.project_id
             AND repository_project.generation = business_commit.generation
             AND repository_project.project_revision =
                    business_commit.project_revision
             AND repository_project.current_snapshot_sha256 =
                    business_commit.after_root_sha256
            WHERE business_commit.commit_id = NEW.commit_id
              AND business_commit.head_revision = NEW.head_revision
        )
        BEGIN
            SELECT RAISE(ABORT, 'business_local_revision_binding_mismatch');
        END
        """,
        """
        CREATE TRIGGER document_commit_heads_local_revision_update_guard
        BEFORE UPDATE OF commit_id, head_revision ON document_commit_heads
        WHEN NOT EXISTS (
            SELECT 1
            FROM business_commits AS business_commit
            JOIN repository_projects AS repository_project
              ON repository_project.project_id = business_commit.project_id
             AND repository_project.generation = business_commit.generation
             AND repository_project.project_revision =
                    business_commit.project_revision
             AND repository_project.current_snapshot_sha256 =
                    business_commit.after_root_sha256
            WHERE business_commit.commit_id = NEW.commit_id
              AND business_commit.head_revision = NEW.head_revision
        )
        BEGIN
            SELECT RAISE(ABORT, 'business_local_revision_binding_mismatch');
        END
        """,
        """
        CREATE TRIGGER repository_runtime_control_local_closure_insert_guard
        BEFORE INSERT ON repository_runtime_control
        WHEN NOT (
            (
                NOT EXISTS (SELECT 1 FROM business_commits) AND
                NOT EXISTS (
                    SELECT 1 FROM repository_projects WHERE status = 'live'
                ) AND
                NOT EXISTS (
                    SELECT 1 FROM document_commit_heads WHERE status = 'live'
                )
            ) OR (
                NEW.repository_revision = (
                    SELECT max(repository_revision) FROM business_commits
                ) AND
                EXISTS (
                    SELECT 1
                    FROM business_commits AS newest_commit
                    JOIN repository_projects AS newest_project
                      ON newest_project.project_id = newest_commit.project_id
                     AND newest_project.generation = newest_commit.generation
                     AND newest_project.status = 'live'
                     AND newest_project.project_revision =
                            newest_commit.project_revision
                     AND newest_project.current_snapshot_sha256 =
                            newest_commit.after_root_sha256
                    JOIN document_commit_heads AS newest_head
                      ON newest_head.commit_id = newest_commit.commit_id
                     AND newest_head.status = 'live'
                     AND newest_head.head_revision = newest_commit.head_revision
                     AND newest_head.business_root_sha256 =
                            newest_commit.after_root_sha256
                    WHERE newest_commit.repository_revision =
                            NEW.repository_revision
                ) AND
                NOT EXISTS (
                    SELECT 1
                    FROM repository_projects AS repository_project
                    WHERE repository_project.status = 'live' AND NOT EXISTS (
                        SELECT 1
                        FROM document_commit_heads AS document_head
                        JOIN business_commits AS business_commit
                          ON business_commit.commit_id = document_head.commit_id
                        WHERE document_head.project_id =
                                repository_project.project_id
                          AND document_head.generation =
                                repository_project.generation
                          AND document_head.status = 'live'
                          AND business_commit.project_revision =
                                repository_project.project_revision
                          AND business_commit.head_revision =
                                document_head.head_revision
                          AND business_commit.after_root_sha256 =
                                repository_project.current_snapshot_sha256
                          AND business_commit.after_root_sha256 =
                                document_head.business_root_sha256
                    )
                ) AND
                NOT EXISTS (
                    SELECT 1
                    FROM document_commit_heads AS document_head
                    WHERE document_head.status = 'live' AND NOT EXISTS (
                        SELECT 1 FROM repository_projects
                        WHERE project_id = document_head.project_id
                          AND generation = document_head.generation
                          AND status = 'live'
                    )
                )
            )
        )
        BEGIN SELECT RAISE(ABORT, 'business_local_closure_invalid'); END
        """,
        """
        CREATE TRIGGER repository_runtime_control_local_closure_update_guard
        BEFORE UPDATE OF repository_revision ON repository_runtime_control
        WHEN NOT (
            NEW.repository_revision = (
                SELECT max(repository_revision) FROM business_commits
            ) AND
            EXISTS (
                SELECT 1
                FROM business_commits AS newest_commit
                JOIN repository_projects AS newest_project
                  ON newest_project.project_id = newest_commit.project_id
                 AND newest_project.generation = newest_commit.generation
                 AND newest_project.status = 'live'
                 AND newest_project.project_revision =
                        newest_commit.project_revision
                 AND newest_project.current_snapshot_sha256 =
                        newest_commit.after_root_sha256
                JOIN document_commit_heads AS newest_head
                  ON newest_head.commit_id = newest_commit.commit_id
                 AND newest_head.status = 'live'
                 AND newest_head.head_revision = newest_commit.head_revision
                 AND newest_head.business_root_sha256 =
                        newest_commit.after_root_sha256
                WHERE newest_commit.repository_revision =
                        NEW.repository_revision
            ) AND
            NOT EXISTS (
                SELECT 1
                FROM repository_projects AS repository_project
                WHERE repository_project.status = 'live' AND NOT EXISTS (
                    SELECT 1
                    FROM document_commit_heads AS document_head
                    JOIN business_commits AS business_commit
                      ON business_commit.commit_id = document_head.commit_id
                    WHERE document_head.project_id = repository_project.project_id
                      AND document_head.generation = repository_project.generation
                      AND document_head.status = 'live'
                      AND business_commit.project_revision =
                            repository_project.project_revision
                      AND business_commit.head_revision =
                            document_head.head_revision
                      AND business_commit.after_root_sha256 =
                            repository_project.current_snapshot_sha256
                      AND business_commit.after_root_sha256 =
                            document_head.business_root_sha256
                )
            ) AND
            NOT EXISTS (
                SELECT 1
                FROM document_commit_heads AS document_head
                WHERE document_head.status = 'live' AND NOT EXISTS (
                    SELECT 1 FROM repository_projects
                    WHERE project_id = document_head.project_id
                      AND generation = document_head.generation
                      AND status = 'live'
                )
            )
        )
        BEGIN SELECT RAISE(ABORT, 'business_local_closure_invalid'); END
        """,
        "DROP TABLE schema_v7_local_revision_preflight",
    ),
)


PRODUCTION_RUNTIME_PREFLIGHT_PREDICATE = """
(
    (
        ((SELECT count(*) FROM kernel_repository) = 1 AND EXISTS (
            SELECT 1 FROM kernel_repository
            WHERE singleton = 1 AND authority = 'legacy'
        )) OR
        (
            NOT EXISTS (SELECT 1 FROM kernel_repository) AND
            NOT EXISTS (SELECT 1 FROM schema_migrations)
        )
    ) AND
    NOT EXISTS (SELECT 1 FROM repository_runtime_control) AND
    NOT EXISTS (SELECT 1 FROM repository_projects) AND
    NOT EXISTS (SELECT 1 FROM document_commit_heads) AND
    NOT EXISTS (SELECT 1 FROM business_commits) AND
    NOT EXISTS (SELECT 1 FROM business_operations) AND
    NOT EXISTS (SELECT 1 FROM business_operation_targets) AND
    NOT EXISTS (SELECT 1 FROM idempotency_inbox) AND
    NOT EXISTS (SELECT 1 FROM event_outbox) AND
    NOT EXISTS (SELECT 1 FROM event_outbox_dispatch_control) AND
    NOT EXISTS (SELECT 1 FROM confirmation_requests) AND
    NOT EXISTS (SELECT 1 FROM deferred_actions) AND
    NOT EXISTS (SELECT 1 FROM deferred_action_attempts) AND
    NOT EXISTS (SELECT 1 FROM pipeline_jobs) AND
    NOT EXISTS (SELECT 1 FROM pipeline_job_attempts) AND
    NOT EXISTS (SELECT 1 FROM pipeline_job_transitions) AND
    NOT EXISTS (SELECT 1 FROM asset_blobs) AND
    NOT EXISTS (SELECT 1 FROM document_asset_refs) AND
    NOT EXISTS (
        SELECT 1
        FROM transaction_receipts
        WHERE substr(CAST(transaction_id AS TEXT), 1, 26) =
                '_business-cutover-install:'
           OR substr(CAST(transaction_id AS TEXT), 1, 30) =
                '_business-cutover-asset-stage:'
           OR substr(CAST(transaction_id AS TEXT), 1, 33) =
                '_business-cutover-asset-coverage:'
           OR substr(CAST(transaction_id AS TEXT), 1, 18) =
                '_repository-audit:'
           OR substr(CAST(transaction_id AS TEXT), 1, 29) =
                '_business-cutover-activation:'
           OR substr(CAST(transaction_id AS TEXT), 1, 24) =
                '_business-cutover-mount:'
           OR substr(CAST(transaction_id AS TEXT), 1, 18) =
                '_worker-execution:'
           OR substr(CAST(transaction_id AS TEXT), 1, 27) =
                '_pipeline-business-control:'
           OR substr(CAST(transaction_id AS TEXT), 1, 12) = 'ze-business:'
           OR CAST(kind AS TEXT) IN (
                '_business_cutover_install_v1',
                '_business_cutover_asset_stage_v1',
                '_business_cutover_asset_coverage_v1',
                '_repository_audit_certificate_v1',
                '_business_cutover_activation_v2',
                '_business_cutover_mount_v2',
                '_worker_execution_v3',
                '_pipeline_business_control_v2',
                'ze_business_commit_v1'
           )
    )
)
"""


def _production_append_only_triggers(table: str) -> Tuple[str, str]:
    """Build the two immutable-history guards owned by schema-v8."""

    if not table.replace("_", "").isalnum():
        raise ValueError("production history table name is invalid")
    return (
        """
        CREATE TRIGGER {table}_no_update
        BEFORE UPDATE ON {table}
        BEGIN SELECT RAISE(ABORT, 'production_history_append_only'); END
        """.format(table=table),
        """
        CREATE TRIGGER {table}_no_delete
        BEFORE DELETE ON {table}
        BEGIN SELECT RAISE(ABORT, 'production_history_append_only'); END
        """.format(table=table),
    )


def _production_head_cas_triggers(
    table: str,
    identity_column: str,
    previous_column: str,
    current_column: str,
) -> Tuple[str, str, str]:
    """Build insert/update/delete guards for one exact revisioned head."""

    names = (table, identity_column, previous_column, current_column)
    if any(not value.replace("_", "").isalnum() for value in names):
        raise ValueError("production head identifier is invalid")
    return (
        """
        CREATE TRIGGER {table}_cas_insert_guard
        BEFORE INSERT ON {table}
        WHEN NEW.state_revision != 1 OR
             NEW.{previous_column} != zeroblob(32)
        BEGIN SELECT RAISE(ABORT, 'production_head_cas_invalid'); END
        """.format(
            table=table,
            previous_column=previous_column,
        ),
        """
        CREATE TRIGGER {table}_cas_update_guard
        BEFORE UPDATE ON {table}
        WHEN NEW.{identity_column} != OLD.{identity_column} OR
             NEW.state_revision != OLD.state_revision + 1 OR
             NEW.{previous_column} != OLD.{current_column} OR
             NEW.{current_column} = OLD.{current_column}
        BEGIN SELECT RAISE(ABORT, 'production_head_cas_invalid'); END
        """.format(
            table=table,
            identity_column=identity_column,
            previous_column=previous_column,
            current_column=current_column,
        ),
        """
        CREATE TRIGGER {table}_no_delete
        BEFORE DELETE ON {table}
        BEGIN SELECT RAISE(ABORT, 'production_head_no_delete'); END
        """.format(table=table),
    )


PRODUCTION_AUTHORITY_WORKER_MIGRATION = Migration(
    version=8,
    name="production_authority_worker_runtime_v1",
    statements=(
        """
        CREATE TABLE schema_v8_production_runtime_preflight (
            verified INTEGER NOT NULL
                CONSTRAINT schema_v8_production_runtime_precondition
                CHECK(verified = 1)
        ) STRICT
        """,
        "INSERT INTO schema_v8_production_runtime_preflight(verified) "
        "SELECT CASE WHEN " + PRODUCTION_RUNTIME_PREFLIGHT_PREDICATE +
        " THEN 1 ELSE 0 END",
        """
        CREATE UNIQUE INDEX transaction_receipts_identity_kind_time
        ON transaction_receipts(transaction_id, kind, committed_at)
        """,
        f"""
        CREATE TABLE business_cutover_install_receipts (
            cutover_id TEXT NOT NULL PRIMARY KEY CHECK(
                length(cutover_id) = 64 AND
                length(CAST(cutover_id AS BLOB)) = 64 AND
                cutover_id NOT GLOB '*[^0-9a-f]*'
            ),
            transaction_id TEXT NOT NULL UNIQUE CHECK(
                transaction_id = '_business-cutover-install:' || cutover_id
            ),
            receipt_kind TEXT NOT NULL CHECK(
                receipt_kind = '_business_cutover_install_v1'
            ),
            committed_at TEXT NOT NULL CHECK(
                length(CAST(committed_at AS BLOB)) = 24 AND
                strftime('%Y-%m-%dT%H:%M:%fZ', committed_at) = committed_at
            ),
            repository_epoch TEXT NOT NULL CHECK(
                length(repository_epoch) = 64 AND
                length(CAST(repository_epoch AS BLOB)) = 64 AND
                repository_epoch NOT GLOB '*[^0-9a-f]*'
            ),
            runtime_generation TEXT NOT NULL CHECK(
                length(CAST(runtime_generation AS BLOB)) BETWEEN 1 AND
                    {MAX_BUSINESS_RUNTIME_GENERATION_BYTES}
            ),
            code_digest BLOB NOT NULL CHECK(
                typeof(code_digest) = 'blob' AND length(code_digest) = 32
            ),
            member_mapping_sha256 BLOB NOT NULL CHECK(
                typeof(member_mapping_sha256) = 'blob' AND
                length(member_mapping_sha256) = 32
            ),
            manifest_bytes BLOB NOT NULL CHECK(
                typeof(manifest_bytes) = 'blob' AND
                length(manifest_bytes) BETWEEN 1 AND {MAX_BUSINESS_SMALL_PAYLOAD_BYTES}
            ),
            manifest_sha256 BLOB NOT NULL UNIQUE CHECK(
                typeof(manifest_sha256) = 'blob' AND length(manifest_sha256) = 32
            ),
            FOREIGN KEY(transaction_id, receipt_kind, committed_at)
                REFERENCES transaction_receipts(transaction_id, kind, committed_at)
                ON UPDATE RESTRICT ON DELETE RESTRICT
        ) STRICT
        """,
        f"""
        CREATE TABLE business_asset_stage_receipts (
            batch_id TEXT NOT NULL PRIMARY KEY CHECK(
                length(batch_id) = 64 AND length(CAST(batch_id AS BLOB)) = 64 AND
                batch_id NOT GLOB '*[^0-9a-f]*'
            ),
            cutover_id TEXT NOT NULL CHECK(
                length(cutover_id) = 64 AND length(CAST(cutover_id AS BLOB)) = 64 AND
                cutover_id NOT GLOB '*[^0-9a-f]*'
            ),
            transaction_id TEXT NOT NULL UNIQUE CHECK(
                transaction_id = '_business-cutover-asset-stage:' ||
                    cutover_id || ':' || batch_id
            ),
            receipt_kind TEXT NOT NULL CHECK(
                receipt_kind = '_business_cutover_asset_stage_v1'
            ),
            committed_at TEXT NOT NULL CHECK(
                length(CAST(committed_at AS BLOB)) = 24 AND
                strftime('%Y-%m-%dT%H:%M:%fZ', committed_at) = committed_at
            ),
            repository_epoch TEXT NOT NULL CHECK(
                length(repository_epoch) = 64 AND length(CAST(repository_epoch AS BLOB)) = 64 AND
                repository_epoch NOT GLOB '*[^0-9a-f]*'
            ),
            source_admission_revision INTEGER NOT NULL CHECK(
                source_admission_revision BETWEEN 1 AND 9223372036854775807
            ),
            source_admission_sha256 BLOB NOT NULL CHECK(
                typeof(source_admission_sha256) = 'blob' AND
                length(source_admission_sha256) = 32
            ),
            member_count INTEGER NOT NULL CHECK(member_count BETWEEN 0 AND 3000000),
            project_count INTEGER NOT NULL CHECK(
                project_count BETWEEN 0 AND 10000
            ),
            blob_count INTEGER NOT NULL CHECK(
                blob_count BETWEEN 0 AND 1000000
            ),
            occurrence_count INTEGER NOT NULL CHECK(
                occurrence_count BETWEEN 0 AND 1000000
            ),
            unique_blob_bytes INTEGER NOT NULL CHECK(
                unique_blob_bytes BETWEEN 0 AND 9223372036854775807
            ),
            referenced_bytes INTEGER NOT NULL CHECK(
                referenced_bytes BETWEEN 0 AND 9223372036854775807
            ),
            project_root_sha256 BLOB NOT NULL CHECK(
                typeof(project_root_sha256) = 'blob' AND
                length(project_root_sha256) = 32
            ),
            blob_root_sha256 BLOB NOT NULL CHECK(
                typeof(blob_root_sha256) = 'blob' AND
                length(blob_root_sha256) = 32
            ),
            occurrence_root_sha256 BLOB NOT NULL CHECK(
                typeof(occurrence_root_sha256) = 'blob' AND
                length(occurrence_root_sha256) = 32
            ),
            coverage_sha256 BLOB NOT NULL CHECK(
                typeof(coverage_sha256) = 'blob' AND
                length(coverage_sha256) = 32
            ),
            manifest_bytes BLOB NOT NULL CHECK(
                typeof(manifest_bytes) = 'blob' AND
                length(manifest_bytes) BETWEEN 1 AND {MAX_BUSINESS_SMALL_PAYLOAD_BYTES}
            ),
            manifest_sha256 BLOB NOT NULL UNIQUE CHECK(
                typeof(manifest_sha256) = 'blob' AND length(manifest_sha256) = 32
            ),
            UNIQUE(batch_id, cutover_id),
            CHECK(
                member_count = project_count + blob_count + occurrence_count
            ),
            CHECK(blob_count <= occurrence_count),
            CHECK(unique_blob_bytes <= referenced_bytes),
            FOREIGN KEY(transaction_id, receipt_kind, committed_at)
                REFERENCES transaction_receipts(transaction_id, kind, committed_at)
                ON UPDATE RESTRICT ON DELETE RESTRICT
        ) STRICT
        """,
        f"""
        CREATE TABLE business_asset_stage_members (
            batch_id TEXT NOT NULL,
            cutover_id TEXT NOT NULL,
            member_kind TEXT NOT NULL CHECK(
                member_kind IN ('project', 'blob', 'occurrence')
            ),
            ordinal INTEGER NOT NULL CHECK(ordinal BETWEEN 0 AND 2999999),
            member_id TEXT NOT NULL CHECK(
                length(CAST(member_id AS BLOB)) BETWEEN 1 AND {MAX_BUSINESS_ID_BYTES}
            ),
            member_bytes INTEGER NOT NULL CHECK(
                member_bytes BETWEEN 0 AND 9223372036854775807
            ),
            member_sha256 BLOB NOT NULL CHECK(
                typeof(member_sha256) = 'blob' AND length(member_sha256) = 32
            ),
            metadata_sha256 BLOB NOT NULL CHECK(
                typeof(metadata_sha256) = 'blob' AND length(metadata_sha256) = 32
            ),
            PRIMARY KEY(batch_id, member_kind, ordinal),
            UNIQUE(batch_id, member_kind, member_id),
            FOREIGN KEY(batch_id, cutover_id)
                REFERENCES business_asset_stage_receipts(batch_id, cutover_id)
                ON UPDATE RESTRICT ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED
        ) STRICT
        """,
        f"""
        CREATE TABLE business_asset_coverage_receipts (
            cutover_id TEXT NOT NULL PRIMARY KEY CHECK(
                length(cutover_id) = 64 AND length(CAST(cutover_id AS BLOB)) = 64 AND
                cutover_id NOT GLOB '*[^0-9a-f]*'
            ),
            transaction_id TEXT NOT NULL UNIQUE CHECK(
                transaction_id = '_business-cutover-asset-coverage:' || cutover_id
            ),
            receipt_kind TEXT NOT NULL CHECK(
                receipt_kind = '_business_cutover_asset_coverage_v1'
            ),
            committed_at TEXT NOT NULL CHECK(
                length(CAST(committed_at AS BLOB)) = 24 AND
                strftime('%Y-%m-%dT%H:%M:%fZ', committed_at) = committed_at
            ),
            repository_epoch TEXT NOT NULL CHECK(
                length(repository_epoch) = 64 AND length(CAST(repository_epoch AS BLOB)) = 64 AND
                repository_epoch NOT GLOB '*[^0-9a-f]*'
            ),
            source_admission_revision INTEGER NOT NULL CHECK(
                source_admission_revision BETWEEN 1 AND 9223372036854775807
            ),
            source_admission_sha256 BLOB NOT NULL CHECK(
                typeof(source_admission_sha256) = 'blob' AND
                length(source_admission_sha256) = 32
            ),
            batch_count INTEGER NOT NULL CHECK(batch_count BETWEEN 0 AND 1000000),
            member_root_sha256 BLOB NOT NULL CHECK(
                typeof(member_root_sha256) = 'blob' AND
                length(member_root_sha256) = 32
            ),
            repository_member_count INTEGER NOT NULL CHECK(
                repository_member_count BETWEEN 0 AND 10000
            ),
            live_project_count INTEGER NOT NULL CHECK(
                live_project_count BETWEEN 0 AND 10000
            ),
            deleted_project_count INTEGER NOT NULL CHECK(
                deleted_project_count BETWEEN 0 AND 10000
            ),
            observed_occurrence_count INTEGER NOT NULL CHECK(
                observed_occurrence_count BETWEEN 0 AND 1000000
            ),
            observed_row_count INTEGER NOT NULL CHECK(
                observed_row_count BETWEEN 0 AND 3000000
            ),
            observed_manifest_bytes INTEGER NOT NULL CHECK(
                observed_manifest_bytes BETWEEN 0 AND 9223372036854775807
            ),
            project_count INTEGER NOT NULL CHECK(project_count BETWEEN 0 AND 10000),
            reference_count INTEGER NOT NULL CHECK(reference_count BETWEEN 0 AND 1000000),
            unique_blob_count INTEGER NOT NULL CHECK(unique_blob_count BETWEEN 0 AND 1000000),
            unique_blob_bytes INTEGER NOT NULL CHECK(
                unique_blob_bytes BETWEEN 0 AND 9223372036854775807
            ),
            referenced_bytes INTEGER NOT NULL CHECK(
                referenced_bytes BETWEEN 0 AND 9223372036854775807
            ),
            project_root_sha256 BLOB NOT NULL CHECK(
                typeof(project_root_sha256) = 'blob' AND
                length(project_root_sha256) = 32
            ),
            blob_root_sha256 BLOB NOT NULL CHECK(
                typeof(blob_root_sha256) = 'blob' AND
                length(blob_root_sha256) = 32
            ),
            occurrence_root_sha256 BLOB NOT NULL CHECK(
                typeof(occurrence_root_sha256) = 'blob' AND
                length(occurrence_root_sha256) = 32
            ),
            coverage_sha256 BLOB NOT NULL CHECK(
                typeof(coverage_sha256) = 'blob' AND length(coverage_sha256) = 32
            ),
            manifest_bytes BLOB NOT NULL CHECK(
                typeof(manifest_bytes) = 'blob' AND
                length(manifest_bytes) BETWEEN 1 AND {MAX_BUSINESS_SMALL_PAYLOAD_BYTES}
            ),
            manifest_sha256 BLOB NOT NULL UNIQUE CHECK(
                typeof(manifest_sha256) = 'blob' AND length(manifest_sha256) = 32
            ),
            CHECK(
                repository_member_count =
                    live_project_count + deleted_project_count
            ),
            CHECK(project_count = live_project_count),
            CHECK(unique_blob_count <= reference_count),
            CHECK(unique_blob_bytes <= referenced_bytes),
            CHECK(observed_occurrence_count >= reference_count),
            CHECK(observed_row_count >= observed_occurrence_count),
            CHECK((batch_count = 0) = (reference_count = 0)),
            CHECK((batch_count = 0) = (observed_manifest_bytes = 0)),
            FOREIGN KEY(transaction_id, receipt_kind, committed_at)
                REFERENCES transaction_receipts(transaction_id, kind, committed_at)
                ON UPDATE RESTRICT ON DELETE RESTRICT
        ) STRICT
        """,
        f"""
        CREATE TABLE repository_audit_certificates (
            certificate_id TEXT NOT NULL PRIMARY KEY CHECK(
                length(certificate_id) = 32 AND
                length(CAST(certificate_id AS BLOB)) = 32 AND
                certificate_id NOT GLOB '*[^0-9a-f]*'
            ),
            cutover_id TEXT NOT NULL CHECK(
                length(cutover_id) = 64 AND length(CAST(cutover_id AS BLOB)) = 64 AND
                cutover_id NOT GLOB '*[^0-9a-f]*'
            ),
            sequence INTEGER NOT NULL UNIQUE CHECK(
                sequence BETWEEN 1 AND 9223372036854775807
            ),
            purpose TEXT NOT NULL CHECK(
                purpose IN ('startup', 'periodic', 'install', 'repair_exit')
            ),
            transaction_id TEXT NOT NULL UNIQUE CHECK(
                transaction_id = '_repository-audit:' || certificate_id
            ),
            receipt_kind TEXT NOT NULL CHECK(
                receipt_kind = '_repository_audit_certificate_v1'
            ),
            committed_at TEXT NOT NULL CHECK(
                length(CAST(committed_at AS BLOB)) = 24 AND
                strftime('%Y-%m-%dT%H:%M:%fZ', committed_at) = committed_at
            ),
            previous_certificate_id TEXT,
            previous_manifest_sha256 BLOB,
            previous_tail_sha256 BLOB NOT NULL CHECK(
                typeof(previous_tail_sha256) = 'blob' AND length(previous_tail_sha256) = 32
            ),
            schema_version INTEGER NOT NULL CHECK(schema_version BETWEEN 1 AND 9223372036854775807),
            migration_ledger_count INTEGER NOT NULL CHECK(migration_ledger_count BETWEEN 1 AND 1000000),
            migration_ledger_sha256 BLOB NOT NULL CHECK(
                typeof(migration_ledger_sha256) = 'blob' AND length(migration_ledger_sha256) = 32
            ),
            receipt_count INTEGER NOT NULL CHECK(receipt_count BETWEEN 0 AND 9223372036854775807),
            receipt_root_sha256 BLOB NOT NULL CHECK(
                typeof(receipt_root_sha256) = 'blob' AND length(receipt_root_sha256) = 32
            ),
            worker_ledger_sequence INTEGER NOT NULL CHECK(
                worker_ledger_sequence BETWEEN 0 AND 9223372036854775807
            ),
            worker_ledger_tail_sha256 BLOB NOT NULL CHECK(
                typeof(worker_ledger_tail_sha256) = 'blob' AND length(worker_ledger_tail_sha256) = 32
            ),
            findings_sha256 BLOB NOT NULL CHECK(
                typeof(findings_sha256) = 'blob' AND length(findings_sha256) = 32
            ),
            manifest_bytes BLOB NOT NULL CHECK(
                typeof(manifest_bytes) = 'blob' AND
                length(manifest_bytes) BETWEEN 1 AND {MAX_BUSINESS_SMALL_PAYLOAD_BYTES}
            ),
            manifest_sha256 BLOB NOT NULL UNIQUE CHECK(
                typeof(manifest_sha256) = 'blob' AND length(manifest_sha256) = 32
            ),
            certificate_tail_sha256 BLOB NOT NULL UNIQUE CHECK(
                typeof(certificate_tail_sha256) = 'blob' AND length(certificate_tail_sha256) = 32
            ),
            CHECK(
                (sequence = 1 AND previous_certificate_id IS NULL AND
                    previous_manifest_sha256 IS NULL AND
                    previous_tail_sha256 = zeroblob(32)) OR
                (sequence > 1 AND previous_certificate_id IS NOT NULL AND
                    typeof(previous_manifest_sha256) = 'blob' AND
                    length(previous_manifest_sha256) = 32 AND
                    previous_tail_sha256 != zeroblob(32))
            ),
            UNIQUE(certificate_id, sequence, certificate_tail_sha256),
            FOREIGN KEY(transaction_id, receipt_kind, committed_at)
                REFERENCES transaction_receipts(transaction_id, kind, committed_at)
                ON UPDATE RESTRICT ON DELETE RESTRICT,
            FOREIGN KEY(previous_certificate_id)
                REFERENCES repository_audit_certificates(certificate_id)
                ON UPDATE RESTRICT ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED
        ) STRICT
        """,
        """
        CREATE TABLE repository_audit_head (
            singleton INTEGER NOT NULL PRIMARY KEY CHECK(singleton = 1),
            state_revision INTEGER NOT NULL CHECK(
                state_revision BETWEEN 1 AND 9223372036854775807
            ),
            sequence INTEGER NOT NULL CHECK(sequence BETWEEN 1 AND 9223372036854775807),
            certificate_id TEXT NOT NULL,
            previous_tail_sha256 BLOB NOT NULL CHECK(
                typeof(previous_tail_sha256) = 'blob' AND length(previous_tail_sha256) = 32
            ),
            tail_sha256 BLOB NOT NULL UNIQUE CHECK(
                typeof(tail_sha256) = 'blob' AND length(tail_sha256) = 32 AND
                tail_sha256 != zeroblob(32)
            ),
            updated_at TEXT NOT NULL CHECK(
                length(CAST(updated_at AS BLOB)) = 24 AND
                strftime('%Y-%m-%dT%H:%M:%fZ', updated_at) = updated_at
            ),
            FOREIGN KEY(certificate_id, sequence, tail_sha256)
                REFERENCES repository_audit_certificates(
                    certificate_id, sequence, certificate_tail_sha256
                ) ON UPDATE RESTRICT ON DELETE RESTRICT
        ) STRICT
        """,
        f"""
        CREATE TABLE business_cutover_activation_receipts (
            cutover_id TEXT NOT NULL PRIMARY KEY CHECK(
                length(cutover_id) = 64 AND length(CAST(cutover_id AS BLOB)) = 64 AND
                cutover_id NOT GLOB '*[^0-9a-f]*'
            ),
            transaction_id TEXT NOT NULL UNIQUE CHECK(
                transaction_id = '_business-cutover-activation:' || cutover_id
            ),
            receipt_kind TEXT NOT NULL CHECK(
                receipt_kind = '_business_cutover_activation_v2'
            ),
            committed_at TEXT NOT NULL CHECK(
                length(CAST(committed_at AS BLOB)) = 24 AND
                strftime('%Y-%m-%dT%H:%M:%fZ', committed_at) = committed_at
            ),
            install_transaction_id TEXT NOT NULL,
            install_manifest_sha256 BLOB NOT NULL CHECK(
                typeof(install_manifest_sha256) = 'blob' AND length(install_manifest_sha256) = 32
            ),
            audit_transaction_id TEXT NOT NULL,
            audit_manifest_sha256 BLOB NOT NULL CHECK(
                typeof(audit_manifest_sha256) = 'blob' AND length(audit_manifest_sha256) = 32
            ),
            migration_ledger_sha256 BLOB NOT NULL CHECK(
                typeof(migration_ledger_sha256) = 'blob' AND length(migration_ledger_sha256) = 32
            ),
            manifest_bytes BLOB NOT NULL CHECK(
                typeof(manifest_bytes) = 'blob' AND
                length(manifest_bytes) BETWEEN 1 AND {MAX_BUSINESS_SMALL_PAYLOAD_BYTES}
            ),
            manifest_sha256 BLOB NOT NULL UNIQUE CHECK(
                typeof(manifest_sha256) = 'blob' AND length(manifest_sha256) = 32
            ),
            FOREIGN KEY(transaction_id, receipt_kind, committed_at)
                REFERENCES transaction_receipts(transaction_id, kind, committed_at)
                ON UPDATE RESTRICT ON DELETE RESTRICT,
            FOREIGN KEY(cutover_id, install_transaction_id, install_manifest_sha256)
                REFERENCES business_cutover_install_receipts(
                    cutover_id, transaction_id, manifest_sha256
                )
                ON UPDATE RESTRICT ON DELETE RESTRICT,
            FOREIGN KEY(cutover_id, audit_transaction_id, audit_manifest_sha256)
                REFERENCES repository_audit_certificates(
                    cutover_id, transaction_id, manifest_sha256
                )
                ON UPDATE RESTRICT ON DELETE RESTRICT
        ) STRICT
        """,
        f"""
        CREATE TABLE business_cutover_mount_receipts (
            cutover_id TEXT NOT NULL PRIMARY KEY CHECK(
                length(cutover_id) = 64 AND length(CAST(cutover_id AS BLOB)) = 64 AND
                cutover_id NOT GLOB '*[^0-9a-f]*'
            ),
            transaction_id TEXT NOT NULL UNIQUE CHECK(
                transaction_id = '_business-cutover-mount:' || cutover_id
            ),
            receipt_kind TEXT NOT NULL CHECK(receipt_kind = '_business_cutover_mount_v2'),
            committed_at TEXT NOT NULL CHECK(
                length(CAST(committed_at AS BLOB)) = 24 AND
                strftime('%Y-%m-%dT%H:%M:%fZ', committed_at) = committed_at
            ),
            install_transaction_id TEXT NOT NULL,
            install_receipt_kind TEXT NOT NULL CHECK(
                install_receipt_kind = '_business_cutover_install_v1'
            ),
            install_committed_at TEXT NOT NULL CHECK(
                length(CAST(install_committed_at AS BLOB)) = 24 AND
                strftime('%Y-%m-%dT%H:%M:%fZ', install_committed_at) =
                    install_committed_at
            ),
            install_manifest_sha256 BLOB NOT NULL CHECK(
                typeof(install_manifest_sha256) = 'blob' AND
                length(install_manifest_sha256) = 32
            ),
            coverage_transaction_id TEXT NOT NULL,
            coverage_receipt_kind TEXT NOT NULL CHECK(
                coverage_receipt_kind =
                    '_business_cutover_asset_coverage_v1'
            ),
            coverage_committed_at TEXT NOT NULL CHECK(
                length(CAST(coverage_committed_at AS BLOB)) = 24 AND
                strftime('%Y-%m-%dT%H:%M:%fZ', coverage_committed_at) =
                    coverage_committed_at
            ),
            coverage_manifest_sha256 BLOB NOT NULL CHECK(
                typeof(coverage_manifest_sha256) = 'blob' AND
                length(coverage_manifest_sha256) = 32
            ),
            audit_transaction_id TEXT NOT NULL,
            audit_manifest_sha256 BLOB NOT NULL CHECK(
                typeof(audit_manifest_sha256) = 'blob' AND
                length(audit_manifest_sha256) = 32
            ),
            activation_transaction_id TEXT NOT NULL,
            activation_manifest_sha256 BLOB NOT NULL CHECK(
                typeof(activation_manifest_sha256) = 'blob' AND
                length(activation_manifest_sha256) = 32
            ),
            repository_epoch TEXT NOT NULL CHECK(
                length(repository_epoch) = 64 AND length(CAST(repository_epoch AS BLOB)) = 64 AND
                repository_epoch NOT GLOB '*[^0-9a-f]*'
            ),
            runtime_generation TEXT NOT NULL CHECK(
                length(CAST(runtime_generation AS BLOB)) BETWEEN 1 AND
                    {MAX_BUSINESS_RUNTIME_GENERATION_BYTES}
            ),
            code_digest BLOB NOT NULL CHECK(
                typeof(code_digest) = 'blob' AND length(code_digest) = 32
            ),
            manifest_bytes BLOB NOT NULL CHECK(
                typeof(manifest_bytes) = 'blob' AND
                length(manifest_bytes) BETWEEN 1 AND {MAX_BUSINESS_SMALL_PAYLOAD_BYTES}
            ),
            manifest_sha256 BLOB NOT NULL UNIQUE CHECK(
                typeof(manifest_sha256) = 'blob' AND length(manifest_sha256) = 32
            ),
            CHECK(
                install_committed_at <= coverage_committed_at AND
                coverage_committed_at <= committed_at
            ),
            FOREIGN KEY(transaction_id, receipt_kind, committed_at)
                REFERENCES transaction_receipts(transaction_id, kind, committed_at)
                ON UPDATE RESTRICT ON DELETE RESTRICT,
            FOREIGN KEY(
                install_transaction_id,
                install_receipt_kind,
                install_committed_at
            ) REFERENCES transaction_receipts(
                transaction_id, kind, committed_at
            ) ON UPDATE RESTRICT ON DELETE RESTRICT,
            FOREIGN KEY(
                coverage_transaction_id,
                coverage_receipt_kind,
                coverage_committed_at
            ) REFERENCES transaction_receipts(
                transaction_id, kind, committed_at
            ) ON UPDATE RESTRICT ON DELETE RESTRICT,
            FOREIGN KEY(cutover_id, install_transaction_id, install_manifest_sha256)
                REFERENCES business_cutover_install_receipts(
                    cutover_id, transaction_id, manifest_sha256
                )
                ON UPDATE RESTRICT ON DELETE RESTRICT,
            FOREIGN KEY(cutover_id, coverage_transaction_id, coverage_manifest_sha256)
                REFERENCES business_asset_coverage_receipts(
                    cutover_id, transaction_id, manifest_sha256
                )
                ON UPDATE RESTRICT ON DELETE RESTRICT,
            FOREIGN KEY(cutover_id, audit_transaction_id, audit_manifest_sha256)
                REFERENCES repository_audit_certificates(
                    cutover_id, transaction_id, manifest_sha256
                )
                ON UPDATE RESTRICT ON DELETE RESTRICT,
            FOREIGN KEY(cutover_id, activation_transaction_id, activation_manifest_sha256)
                REFERENCES business_cutover_activation_receipts(
                    cutover_id, transaction_id, manifest_sha256
                )
                ON UPDATE RESTRICT ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE UNIQUE INDEX business_cutover_install_receipts_pointer
        ON business_cutover_install_receipts(
            cutover_id, transaction_id, manifest_sha256
        )
        """,
        """
        CREATE UNIQUE INDEX business_asset_coverage_receipts_pointer
        ON business_asset_coverage_receipts(
            cutover_id, transaction_id, manifest_sha256
        )
        """,
        """
        CREATE UNIQUE INDEX repository_audit_certificates_pointer
        ON repository_audit_certificates(
            cutover_id, transaction_id, manifest_sha256
        )
        """,
        """
        CREATE UNIQUE INDEX business_cutover_activation_receipts_pointer
        ON business_cutover_activation_receipts(
            cutover_id, transaction_id, manifest_sha256
        )
        """,
        """
        CREATE UNIQUE INDEX business_cutover_mount_receipts_pointer
        ON business_cutover_mount_receipts(
            cutover_id, transaction_id, manifest_sha256
        )
        """,
        f"""
        CREATE TABLE atomic_business_write_receipts (
            commit_id TEXT NOT NULL PRIMARY KEY CHECK(
                length(commit_id) = 32 AND length(CAST(commit_id AS BLOB)) = 32 AND
                commit_id NOT GLOB '*[^0-9a-f]*'
            ),
            transaction_id TEXT NOT NULL UNIQUE CHECK(
                transaction_id = '{BUSINESS_TRANSACTION_PREFIX}' || commit_id
            ),
            receipt_kind TEXT NOT NULL CHECK(
                receipt_kind = '{BUSINESS_TRANSACTION_KIND}'
            ),
            committed_at TEXT NOT NULL CHECK(
                length(CAST(committed_at AS BLOB)) = 24 AND
                strftime('%Y-%m-%dT%H:%M:%fZ', committed_at) = committed_at
            ),
            atomic_manifest_sha256 BLOB NOT NULL CHECK(
                typeof(atomic_manifest_sha256) = 'blob' AND
                length(atomic_manifest_sha256) = 32
            ),
            pipeline_intents_sha256 BLOB NOT NULL CHECK(
                typeof(pipeline_intents_sha256) = 'blob' AND
                length(pipeline_intents_sha256) = 32
            ),
            controls_sha256 BLOB NOT NULL CHECK(
                typeof(controls_sha256) = 'blob' AND length(controls_sha256) = 32
            ),
            control_count INTEGER NOT NULL CHECK(control_count BETWEEN 0 AND 10000),
            before_worker_sequence INTEGER NOT NULL CHECK(
                before_worker_sequence BETWEEN 0 AND 9223372036854775807
            ),
            before_worker_tail_sha256 BLOB NOT NULL CHECK(
                typeof(before_worker_tail_sha256) = 'blob' AND
                length(before_worker_tail_sha256) = 32
            ),
            after_worker_sequence INTEGER NOT NULL CHECK(
                after_worker_sequence = before_worker_sequence + control_count
            ),
            after_worker_tail_sha256 BLOB NOT NULL CHECK(
                typeof(after_worker_tail_sha256) = 'blob' AND
                length(after_worker_tail_sha256) = 32
            ),
            manifest_bytes BLOB NOT NULL CHECK(
                typeof(manifest_bytes) = 'blob' AND
                length(manifest_bytes) BETWEEN 1 AND
                    {MAX_BUSINESS_SMALL_PAYLOAD_BYTES}
            ),
            manifest_sha256 BLOB NOT NULL CHECK(
                typeof(manifest_sha256) = 'blob' AND length(manifest_sha256) = 32
            ),
            receipt_sha256 BLOB NOT NULL UNIQUE CHECK(
                typeof(receipt_sha256) = 'blob' AND length(receipt_sha256) = 32
            ),
            production_manifest_bytes BLOB NOT NULL CHECK(
                typeof(production_manifest_bytes) = 'blob' AND
                length(production_manifest_bytes) BETWEEN 1 AND
                    {MAX_BUSINESS_SMALL_PAYLOAD_BYTES}
            ),
            production_manifest_sha256 BLOB NOT NULL CHECK(
                typeof(production_manifest_sha256) = 'blob' AND
                length(production_manifest_sha256) = 32
            ),
            target_count INTEGER NOT NULL CHECK(
                target_count BETWEEN 0 AND 30000
            ),
            targets_sha256 BLOB NOT NULL CHECK(
                typeof(targets_sha256) = 'blob' AND
                length(targets_sha256) = 32
            ),
            production_receipt_sha256 BLOB NOT NULL UNIQUE CHECK(
                typeof(production_receipt_sha256) = 'blob' AND
                length(production_receipt_sha256) = 32
            ),
            FOREIGN KEY(transaction_id, receipt_kind, committed_at)
                REFERENCES transaction_receipts(transaction_id, kind, committed_at)
                ON UPDATE RESTRICT ON DELETE RESTRICT,
            FOREIGN KEY(commit_id)
                REFERENCES business_commits(commit_id)
                ON UPDATE RESTRICT ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE worker_ledger_head (
            singleton INTEGER NOT NULL PRIMARY KEY CHECK(singleton = 1),
            state_revision INTEGER NOT NULL CHECK(
                state_revision BETWEEN 1 AND 9223372036854775807
            ),
            sequence INTEGER NOT NULL CHECK(sequence BETWEEN 0 AND 9223372036854775807),
            previous_tail_sha256 BLOB NOT NULL CHECK(
                typeof(previous_tail_sha256) = 'blob' AND length(previous_tail_sha256) = 32
            ),
            tail_sha256 BLOB NOT NULL UNIQUE CHECK(
                typeof(tail_sha256) = 'blob' AND length(tail_sha256) = 32
            ),
            updated_at TEXT NOT NULL CHECK(
                length(CAST(updated_at AS BLOB)) = 24 AND
                strftime('%Y-%m-%dT%H:%M:%fZ', updated_at) = updated_at
            ),
            CHECK((sequence = 0) = (tail_sha256 = zeroblob(32)))
        ) STRICT
        """,
        """
        CREATE TABLE worker_ledger_entries (
            sequence INTEGER NOT NULL PRIMARY KEY CHECK(
                sequence BETWEEN 1 AND 9223372036854775807
            ),
            entry_kind TEXT NOT NULL CHECK(
                entry_kind IN ('worker_execution', 'pipeline_control')
            ),
            transaction_id TEXT NOT NULL UNIQUE CHECK(
                length(CAST(transaction_id AS BLOB)) BETWEEN 1 AND 128
            ),
            receipt_kind TEXT NOT NULL CHECK(
                length(CAST(receipt_kind AS BLOB)) BETWEEN 1 AND 128
            ),
            committed_at TEXT NOT NULL CHECK(
                length(CAST(committed_at AS BLOB)) = 24 AND
                strftime('%Y-%m-%dT%H:%M:%fZ', committed_at) = committed_at
            ),
            previous_tail_sha256 BLOB NOT NULL CHECK(
                typeof(previous_tail_sha256) = 'blob' AND length(previous_tail_sha256) = 32
            ),
            ledger_tail_sha256 BLOB NOT NULL UNIQUE CHECK(
                typeof(ledger_tail_sha256) = 'blob' AND
                length(ledger_tail_sha256) = 32 AND
                ledger_tail_sha256 != zeroblob(32)
            ),
            request_sha256 BLOB NOT NULL CHECK(
                typeof(request_sha256) = 'blob' AND length(request_sha256) = 32
            ),
            after_state_sha256 BLOB NOT NULL CHECK(
                typeof(after_state_sha256) = 'blob' AND length(after_state_sha256) = 32
            ),
            receipt_sha256 BLOB NOT NULL UNIQUE CHECK(
                typeof(receipt_sha256) = 'blob' AND length(receipt_sha256) = 32
            ),
            occurred_at TEXT NOT NULL CHECK(
                occurred_at = committed_at AND
                length(CAST(occurred_at AS BLOB)) = 24 AND
                strftime('%Y-%m-%dT%H:%M:%fZ', occurred_at) = occurred_at
            ),
            CHECK(
                (sequence = 1 AND previous_tail_sha256 = zeroblob(32)) OR
                (sequence > 1 AND previous_tail_sha256 != zeroblob(32))
            ),
            UNIQUE(sequence, transaction_id, receipt_sha256),
            UNIQUE(
                sequence, entry_kind, transaction_id, previous_tail_sha256,
                ledger_tail_sha256, request_sha256, after_state_sha256,
                receipt_sha256, occurred_at
            ),
            FOREIGN KEY(transaction_id, receipt_kind, committed_at)
                REFERENCES transaction_receipts(transaction_id, kind, committed_at)
                ON UPDATE RESTRICT ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE worker_execution_receipts (
            request_id TEXT NOT NULL PRIMARY KEY CHECK(
                length(request_id) = 32 AND length(CAST(request_id AS BLOB)) = 32 AND
                request_id NOT GLOB '*[^0-9a-f]*'
            ),
            transaction_id TEXT NOT NULL UNIQUE CHECK(
                transaction_id = '_worker-execution:' || request_id
            ),
            entry_kind TEXT NOT NULL CHECK(entry_kind = 'worker_execution'),
            receipt_kind TEXT NOT NULL CHECK(receipt_kind = '_worker_execution_v3'),
            committed_at TEXT NOT NULL CHECK(
                length(CAST(committed_at AS BLOB)) = 24 AND
                strftime('%Y-%m-%dT%H:%M:%fZ', committed_at) = committed_at
            ),
            operation TEXT NOT NULL CHECK(
                operation IN (
                    'claim', 'acknowledge_cancel', 'renew', 'authorize_effect',
                    'complete', 'expire', 'reconcile'
                )
            ),
            target_kind TEXT NOT NULL CHECK(
                target_kind IN ('action', 'pipeline', 'outbox')
            ),
            target_id TEXT NOT NULL CHECK(
                length(target_id) = 32 AND length(CAST(target_id AS BLOB)) = 32 AND
                target_id NOT GLOB '*[^0-9a-f]*'
            ),
            before_status TEXT NOT NULL CHECK(
                before_status IN (
                    'pending', 'queued', 'retry', 'running', 'leased', 'paused',
                    'cancel_requested', 'succeeded', 'failed', 'failed_terminal',
                    'cancelled', 'published', 'dead', 'outcome_unknown',
                    'repair_required'
                )
            ),
            after_status TEXT NOT NULL CHECK(
                after_status IN (
                    'pending', 'queued', 'retry', 'running', 'leased', 'paused',
                    'cancel_requested', 'succeeded', 'failed', 'failed_terminal',
                    'cancelled', 'published', 'dead', 'outcome_unknown',
                    'repair_required'
                )
            ),
            before_state_revision INTEGER NOT NULL CHECK(
                before_state_revision BETWEEN 1 AND 9223372036854775806
            ),
            after_state_revision INTEGER NOT NULL CHECK(
                after_state_revision = before_state_revision + 1
            ),
            sequence INTEGER NOT NULL UNIQUE,
            previous_ledger_tail_sha256 BLOB NOT NULL CHECK(
                typeof(previous_ledger_tail_sha256) = 'blob' AND
                length(previous_ledger_tail_sha256) = 32
            ),
            ledger_tail_sha256 BLOB NOT NULL UNIQUE CHECK(
                typeof(ledger_tail_sha256) = 'blob' AND length(ledger_tail_sha256) = 32
            ),
            manifest_bytes BLOB NOT NULL CHECK(
                typeof(manifest_bytes) = 'blob' AND
                length(manifest_bytes) BETWEEN 1 AND 4194304
            ),
            request_sha256 BLOB NOT NULL CHECK(
                typeof(request_sha256) = 'blob' AND length(request_sha256) = 32
            ),
            after_state_sha256 BLOB NOT NULL CHECK(
                typeof(after_state_sha256) = 'blob' AND length(after_state_sha256) = 32
            ),
            receipt_sha256 BLOB NOT NULL UNIQUE CHECK(
                typeof(receipt_sha256) = 'blob' AND length(receipt_sha256) = 32
            ),
            CHECK(
                (target_kind = 'pipeline' AND
                    before_status IN (
                        'queued', 'retry', 'running', 'paused',
                        'cancel_requested', 'succeeded', 'failed', 'cancelled',
                        'outcome_unknown', 'repair_required'
                    ) AND after_status IN (
                        'queued', 'retry', 'running', 'paused',
                        'cancel_requested', 'succeeded', 'failed', 'cancelled',
                        'outcome_unknown', 'repair_required'
                    )) OR
                (target_kind = 'action' AND
                    before_status IN (
                        'queued', 'retry', 'running', 'succeeded',
                        'failed_terminal', 'cancelled', 'outcome_unknown',
                        'repair_required'
                    ) AND after_status IN (
                        'queued', 'retry', 'running', 'succeeded',
                        'failed_terminal', 'cancelled', 'outcome_unknown',
                        'repair_required'
                    )) OR
                (target_kind = 'outbox' AND
                    before_status IN (
                        'pending', 'retry', 'leased', 'published', 'dead',
                        'outcome_unknown'
                    ) AND after_status IN (
                        'pending', 'retry', 'leased', 'published', 'dead',
                        'outcome_unknown'
                    ))
            ),
            UNIQUE(
                transaction_id, target_id, after_status, committed_at
            ),
            FOREIGN KEY(transaction_id, receipt_kind, committed_at)
                REFERENCES transaction_receipts(transaction_id, kind, committed_at)
                ON UPDATE RESTRICT ON DELETE RESTRICT,
            FOREIGN KEY(
                sequence, entry_kind, transaction_id,
                previous_ledger_tail_sha256, ledger_tail_sha256,
                request_sha256, after_state_sha256, receipt_sha256, committed_at
            ) REFERENCES worker_ledger_entries(
                sequence, entry_kind, transaction_id,
                previous_tail_sha256, ledger_tail_sha256,
                request_sha256, after_state_sha256, receipt_sha256, occurred_at
            )
                ON UPDATE RESTRICT ON DELETE RESTRICT,
            FOREIGN KEY(target_id, target_kind)
                REFERENCES worker_targets(target_id, target_kind)
                ON UPDATE RESTRICT ON DELETE RESTRICT
        ) STRICT
        """,
        f"""
        CREATE TABLE pipeline_control_intents (
            intent_id TEXT NOT NULL PRIMARY KEY CHECK(
                length(intent_id) = 32 AND length(CAST(intent_id AS BLOB)) = 32 AND
                intent_id NOT GLOB '*[^0-9a-f]*'
            ),
            origin_commit_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL CHECK(ordinal BETWEEN 0 AND 9999),
            target_id TEXT NOT NULL CHECK(
                length(target_id) = 32 AND length(CAST(target_id AS BLOB)) = 32 AND
                target_id NOT GLOB '*[^0-9a-f]*'
            ),
            target_kind TEXT NOT NULL CHECK(target_kind = 'pipeline'),
            operation TEXT NOT NULL CHECK(
                operation IN ('create', 'apply_control_intent')
            ),
            intent_type TEXT NOT NULL CHECK(
                length(CAST(intent_type AS BLOB)) BETWEEN 1 AND {MAX_BUSINESS_ID_BYTES}
            ),
            idempotency_key TEXT NOT NULL CHECK(
                length(CAST(idempotency_key AS BLOB)) BETWEEN 1 AND {MAX_BUSINESS_ID_BYTES}
            ),
            payload_schema TEXT NOT NULL CHECK(
                length(CAST(payload_schema AS BLOB)) BETWEEN 1 AND 1024
            ),
            payload_codec_id TEXT NOT NULL CHECK(
                payload_codec_id = 'ze.deterministic-json'
            ),
            payload_codec_version INTEGER NOT NULL CHECK(payload_codec_version = 1),
            payload_bytes BLOB NOT NULL CHECK(
                typeof(payload_bytes) = 'blob' AND
                length(payload_bytes) BETWEEN 1 AND {MAX_BUSINESS_SMALL_PAYLOAD_BYTES}
            ),
            payload_sha256 BLOB NOT NULL CHECK(
                typeof(payload_sha256) = 'blob' AND length(payload_sha256) = 32
            ),
            replay_policy TEXT NOT NULL CHECK(
                replay_policy IN ('exact_cas_retry', 'reload_and_replan', 'never_replay')
            ),
            idempotency_policy TEXT NOT NULL CHECK(
                idempotency_policy IN ('durable_receipt_reconcile', 'single_attempt')
            ),
            max_attempts INTEGER NOT NULL CHECK(max_attempts BETWEEN 1 AND 100),
            before_status TEXT,
            after_status TEXT NOT NULL CHECK(
                length(CAST(after_status AS BLOB)) BETWEEN 1 AND 64
            ),
            before_state_revision INTEGER,
            after_state_revision INTEGER NOT NULL CHECK(
                after_state_revision BETWEEN 1 AND 9223372036854775807
            ),
            request_sha256 BLOB NOT NULL CHECK(
                typeof(request_sha256) = 'blob' AND length(request_sha256) = 32
            ),
            committed_at TEXT NOT NULL CHECK(
                length(CAST(committed_at AS BLOB)) = 24 AND
                strftime('%Y-%m-%dT%H:%M:%fZ', committed_at) = committed_at
            ),
            UNIQUE(origin_commit_id, ordinal),
            CHECK(
                (operation = 'create' AND before_status IS NULL AND
                    before_state_revision IS NULL AND after_state_revision = 1) OR
                (operation = 'apply_control_intent' AND before_status IS NOT NULL AND
                    before_state_revision BETWEEN 1 AND 9223372036854775806 AND
                    after_state_revision = before_state_revision + 1)
            ),
            CHECK(
                (replay_policy = 'never_replay' AND
                    idempotency_policy = 'single_attempt' AND max_attempts = 1) OR
                (replay_policy != 'never_replay' AND
                    idempotency_policy = 'durable_receipt_reconcile')
            ),
            FOREIGN KEY(origin_commit_id)
                REFERENCES business_commits(commit_id)
                ON UPDATE RESTRICT ON DELETE RESTRICT,
            FOREIGN KEY(target_id, target_kind)
                REFERENCES worker_targets(target_id, target_kind)
                ON UPDATE RESTRICT ON DELETE RESTRICT
        ) STRICT
        """,
        f"""
        CREATE TABLE pipeline_control_receipts (
            intent_id TEXT NOT NULL PRIMARY KEY,
            transaction_id TEXT NOT NULL UNIQUE CHECK(
                transaction_id = '_pipeline-business-control:' || intent_id
            ),
            entry_kind TEXT NOT NULL CHECK(entry_kind = 'pipeline_control'),
            receipt_kind TEXT NOT NULL CHECK(
                receipt_kind = '_pipeline_business_control_v2'
            ),
            committed_at TEXT NOT NULL CHECK(
                length(CAST(committed_at AS BLOB)) = 24 AND
                strftime('%Y-%m-%dT%H:%M:%fZ', committed_at) = committed_at
            ),
            operation TEXT NOT NULL CHECK(
                operation IN ('create', 'apply_control_intent')
            ),
            intent_type TEXT NOT NULL CHECK(
                length(CAST(intent_type AS BLOB)) BETWEEN 1 AND {MAX_BUSINESS_ID_BYTES}
            ),
            target_id TEXT NOT NULL CHECK(
                length(target_id) = 32 AND length(CAST(target_id AS BLOB)) = 32 AND
                target_id NOT GLOB '*[^0-9a-f]*'
            ),
            before_status TEXT,
            after_status TEXT NOT NULL,
            before_state_revision INTEGER,
            after_state_revision INTEGER NOT NULL CHECK(
                after_state_revision BETWEEN 1 AND 9223372036854775807
            ),
            sequence INTEGER NOT NULL UNIQUE,
            previous_ledger_tail_sha256 BLOB NOT NULL CHECK(
                typeof(previous_ledger_tail_sha256) = 'blob' AND
                length(previous_ledger_tail_sha256) = 32
            ),
            ledger_tail_sha256 BLOB NOT NULL UNIQUE CHECK(
                typeof(ledger_tail_sha256) = 'blob' AND length(ledger_tail_sha256) = 32
            ),
            manifest_bytes BLOB NOT NULL CHECK(
                typeof(manifest_bytes) = 'blob' AND
                length(manifest_bytes) BETWEEN 1 AND 4194304
            ),
            request_sha256 BLOB NOT NULL CHECK(
                typeof(request_sha256) = 'blob' AND length(request_sha256) = 32
            ),
            after_state_sha256 BLOB NOT NULL CHECK(
                typeof(after_state_sha256) = 'blob' AND length(after_state_sha256) = 32
            ),
            receipt_sha256 BLOB NOT NULL UNIQUE CHECK(
                typeof(receipt_sha256) = 'blob' AND length(receipt_sha256) = 32
            ),
            FOREIGN KEY(transaction_id, receipt_kind, committed_at)
                REFERENCES transaction_receipts(transaction_id, kind, committed_at)
                ON UPDATE RESTRICT ON DELETE RESTRICT,
            FOREIGN KEY(
                sequence, entry_kind, transaction_id,
                previous_ledger_tail_sha256, ledger_tail_sha256,
                request_sha256, after_state_sha256, receipt_sha256, committed_at
            ) REFERENCES worker_ledger_entries(
                sequence, entry_kind, transaction_id,
                previous_tail_sha256, ledger_tail_sha256,
                request_sha256, after_state_sha256, receipt_sha256, occurred_at
            )
                ON UPDATE RESTRICT ON DELETE RESTRICT,
            FOREIGN KEY(intent_id)
                REFERENCES pipeline_control_intents(intent_id)
                ON UPDATE RESTRICT ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE UNIQUE INDEX pipeline_control_intents_receipt_identity
        ON pipeline_control_intents(
            intent_id, operation, intent_type, target_id, before_status,
            after_status, before_state_revision, after_state_revision,
            request_sha256, committed_at
        )
        """,
        """
        CREATE TABLE atomic_business_control_members (
            commit_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL CHECK(ordinal BETWEEN 0 AND 9999),
            intent_id TEXT NOT NULL UNIQUE,
            control_transaction_id TEXT NOT NULL UNIQUE,
            sequence INTEGER NOT NULL UNIQUE,
            ledger_tail_sha256 BLOB NOT NULL CHECK(
                typeof(ledger_tail_sha256) = 'blob' AND length(ledger_tail_sha256) = 32
            ),
            binding_sha256 BLOB NOT NULL CHECK(
                typeof(binding_sha256) = 'blob' AND length(binding_sha256) = 32
            ),
            PRIMARY KEY(commit_id, ordinal),
            FOREIGN KEY(commit_id)
                REFERENCES atomic_business_write_receipts(commit_id)
                ON UPDATE RESTRICT ON DELETE RESTRICT,
            FOREIGN KEY(intent_id)
                REFERENCES pipeline_control_intents(intent_id)
                ON UPDATE RESTRICT ON DELETE RESTRICT,
            FOREIGN KEY(intent_id, control_transaction_id, sequence, ledger_tail_sha256)
                REFERENCES pipeline_control_receipts(
                    intent_id, transaction_id, sequence, ledger_tail_sha256
                ) ON UPDATE RESTRICT ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE UNIQUE INDEX pipeline_control_receipts_member_identity
        ON pipeline_control_receipts(
            intent_id, transaction_id, sequence, ledger_tail_sha256
        )
        """,
        f"""
        CREATE TABLE worker_targets (
            target_id TEXT NOT NULL PRIMARY KEY CHECK(
                length(target_id) = 32 AND length(CAST(target_id AS BLOB)) = 32 AND
                target_id NOT GLOB '*[^0-9a-f]*'
            ),
            target_kind TEXT NOT NULL CHECK(target_kind IN ('action', 'pipeline', 'outbox')),
            genesis_status TEXT NOT NULL CHECK(
                (target_kind IN ('action', 'pipeline') AND
                    genesis_status = 'queued') OR
                (target_kind = 'outbox' AND genesis_status = 'pending')
            ),
            genesis_state_sha256 BLOB NOT NULL CHECK(
                typeof(genesis_state_sha256) = 'blob' AND
                length(genesis_state_sha256) = 32 AND
                genesis_state_sha256 != zeroblob(32)
            ),
            project_id TEXT NOT NULL CHECK(
                length(CAST(project_id AS BLOB)) BETWEEN 1 AND {MAX_BUSINESS_ID_BYTES}
            ),
            generation INTEGER NOT NULL CHECK(generation BETWEEN 1 AND 9223372036854775807),
            document_id TEXT NOT NULL CHECK(
                length(CAST(document_id AS BLOB)) BETWEEN 1 AND {MAX_BUSINESS_ID_BYTES}
            ),
            origin_commit_id TEXT NOT NULL,
            source_ordinal INTEGER NOT NULL CHECK(
                source_ordinal BETWEEN 0 AND 9999
            ),
            payload_schema TEXT NOT NULL CHECK(
                length(CAST(payload_schema AS BLOB)) BETWEEN 1 AND 1024
            ),
            payload_codec_id TEXT NOT NULL CHECK(
                payload_codec_id = 'ze.deterministic-json'
            ),
            payload_codec_version INTEGER NOT NULL CHECK(payload_codec_version = 1),
            payload_bytes BLOB NOT NULL CHECK(
                typeof(payload_bytes) = 'blob' AND
                length(payload_bytes) BETWEEN 1 AND {MAX_BUSINESS_SMALL_PAYLOAD_BYTES}
            ),
            payload_sha256 BLOB NOT NULL CHECK(
                typeof(payload_sha256) = 'blob' AND length(payload_sha256) = 32
            ),
            idempotency_scope TEXT NOT NULL CHECK(
                length(CAST(idempotency_scope AS BLOB)) BETWEEN 1 AND
                    CASE target_kind
                        WHEN 'pipeline' THEN {MAX_PIPELINE_WORKER_SCOPE_BYTES}
                        ELSE {MAX_BUSINESS_ID_BYTES}
                    END
            ),
            idempotency_key TEXT NOT NULL CHECK(
                length(CAST(idempotency_key AS BLOB)) BETWEEN 1 AND {MAX_BUSINESS_ID_BYTES}
            ),
            replay_policy TEXT NOT NULL CHECK(
                replay_policy IN (
                    'exact_cas_retry', 'reload_and_replan',
                    'provider_idempotent', 'reconcile_before_retry',
                    'never_replay'
                )
            ),
            idempotency_policy TEXT NOT NULL CHECK(
                idempotency_policy IN (
                    'required_external_key', 'durable_receipt_reconcile',
                    'single_attempt'
                )
            ),
            max_attempts INTEGER NOT NULL CHECK(max_attempts BETWEEN 1 AND 100),
            action_type TEXT CHECK(
                action_type IS NULL OR
                length(CAST(action_type AS BLOB)) BETWEEN 1 AND
                    {MAX_BUSINESS_ID_BYTES}
            ),
            pipeline_id TEXT CHECK(
                pipeline_id IS NULL OR
                length(CAST(pipeline_id AS BLOB)) BETWEEN 1 AND {MAX_BUSINESS_ID_BYTES}
            ),
            intent_type TEXT CHECK(
                intent_type IS NULL OR
                length(CAST(intent_type AS BLOB)) BETWEEN 1 AND {MAX_BUSINESS_ID_BYTES}
            ),
            outbox_topic TEXT CHECK(
                outbox_topic IS NULL OR
                length(CAST(outbox_topic AS BLOB)) BETWEEN 1 AND
                    {MAX_BUSINESS_ID_BYTES}
            ),
            outbox_partition_key TEXT CHECK(
                outbox_partition_key IS NULL OR
                length(CAST(outbox_partition_key AS BLOB)) BETWEEN 1 AND
                    {MAX_BUSINESS_ID_BYTES}
            ),
            outbox_sequence INTEGER CHECK(
                outbox_sequence IS NULL OR
                outbox_sequence BETWEEN 1 AND 9223372036854775807
            ),
            outbox_repository_revision INTEGER CHECK(
                outbox_repository_revision IS NULL OR
                outbox_repository_revision BETWEEN 1 AND 9223372036854775807
            ),
            outbox_ordinal INTEGER CHECK(
                outbox_ordinal IS NULL OR outbox_ordinal BETWEEN 0 AND 9999
            ),
            created_at TEXT NOT NULL CHECK(
                length(CAST(created_at AS BLOB)) = 24 AND
                strftime('%Y-%m-%dT%H:%M:%fZ', created_at) = created_at
            ),
            UNIQUE(target_kind, idempotency_scope, idempotency_key),
            UNIQUE(target_id, target_kind),
            UNIQUE(origin_commit_id, target_kind, source_ordinal),
            CHECK(
                (target_kind = 'pipeline' AND action_type IS NULL AND
                    pipeline_id IS NOT NULL AND intent_type IS NOT NULL AND
                    outbox_topic IS NULL AND outbox_partition_key IS NULL AND
                    outbox_sequence IS NULL AND
                    outbox_repository_revision IS NULL AND outbox_ordinal IS NULL) OR
                (target_kind = 'outbox' AND action_type IS NULL AND
                    pipeline_id IS NULL AND intent_type IS NULL AND
                    outbox_topic IS NOT NULL AND
                    outbox_partition_key IS NOT NULL AND
                    outbox_sequence IS NOT NULL AND
                    outbox_repository_revision IS NOT NULL AND outbox_ordinal IS NOT NULL) OR
                (target_kind = 'action' AND action_type IS NOT NULL AND
                    pipeline_id IS NULL AND intent_type IS NULL AND
                    outbox_topic IS NULL AND outbox_partition_key IS NULL AND
                    outbox_sequence IS NULL AND outbox_repository_revision IS NULL AND
                    outbox_ordinal IS NULL)
            ),
            CHECK(
                target_kind != 'pipeline' OR idempotency_scope =
                    'pipeline/' || project_id || '/' || CAST(generation AS TEXT) ||
                    '/' || pipeline_id
            ),
            CHECK(
                (target_kind = 'pipeline' AND (
                    (replay_policy IN ('exact_cas_retry', 'reload_and_replan') AND
                        idempotency_policy = 'durable_receipt_reconcile') OR
                    (replay_policy = 'never_replay' AND
                        idempotency_policy = 'single_attempt' AND max_attempts = 1)
                )) OR
                (target_kind IN ('action', 'outbox') AND (
                    (replay_policy = 'provider_idempotent' AND
                        idempotency_policy = 'required_external_key') OR
                    (replay_policy = 'reconcile_before_retry' AND
                        idempotency_policy = 'durable_receipt_reconcile') OR
                    (replay_policy = 'never_replay' AND
                        idempotency_policy = 'single_attempt' AND max_attempts = 1)
                ))
            ),
            FOREIGN KEY(origin_commit_id)
                REFERENCES business_commits(commit_id)
                ON UPDATE RESTRICT ON DELETE RESTRICT,
            FOREIGN KEY(
                target_id, outbox_sequence, outbox_repository_revision, outbox_ordinal
            ) REFERENCES outbox_allocations(
                event_id, sequence, repository_revision, ordinal
            )
                ON UPDATE RESTRICT ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE worker_target_current (
            target_id TEXT NOT NULL PRIMARY KEY,
            target_kind TEXT NOT NULL CHECK(target_kind IN ('action', 'pipeline', 'outbox')),
            status TEXT NOT NULL CHECK(
                (target_kind = 'pipeline' AND status IN (
                    'queued', 'retry', 'running', 'paused',
                    'cancel_requested', 'succeeded', 'failed', 'cancelled',
                    'outcome_unknown', 'repair_required'
                )) OR
                (target_kind = 'action' AND status IN (
                    'queued', 'retry', 'running', 'succeeded',
                    'failed_terminal', 'cancelled', 'outcome_unknown',
                    'repair_required'
                )) OR
                (target_kind = 'outbox' AND status IN (
                    'pending', 'retry', 'leased', 'published', 'dead',
                    'outcome_unknown'
                ))
            ),
            state_revision INTEGER NOT NULL CHECK(
                state_revision BETWEEN 1 AND 9223372036854775807
            ),
            attempt_count INTEGER NOT NULL CHECK(attempt_count BETWEEN 0 AND 100),
            last_attempt_id TEXT,
            last_attempt_number INTEGER CHECK(
                last_attempt_number IS NULL OR last_attempt_number BETWEEN 1 AND 100
            ),
            current_attempt_id TEXT,
            current_attempt_number INTEGER CHECK(
                current_attempt_number IS NULL OR current_attempt_number BETWEEN 1 AND 100
            ),
            fence_high_water INTEGER NOT NULL CHECK(
                fence_high_water BETWEEN 0 AND 9223372036854775807
            ),
            cancellation_pending INTEGER NOT NULL CHECK(cancellation_pending IN (0, 1)),
            next_attempt_at TEXT CHECK(
                next_attempt_at IS NULL OR (
                    length(CAST(next_attempt_at AS BLOB)) = 24 AND
                    strftime('%Y-%m-%dT%H:%M:%fZ', next_attempt_at) = next_attempt_at
                )
            ),
            lease_owner TEXT CHECK(
                lease_owner IS NULL OR length(CAST(lease_owner AS BLOB)) BETWEEN 1 AND 1024
            ),
            lease_fence INTEGER CHECK(
                lease_fence IS NULL OR lease_fence BETWEEN 1 AND 9223372036854775807
            ),
            lease_expires_at TEXT CHECK(
                lease_expires_at IS NULL OR (
                    length(CAST(lease_expires_at AS BLOB)) = 24 AND
                    strftime('%Y-%m-%dT%H:%M:%fZ', lease_expires_at) = lease_expires_at
                )
            ),
            current_effect_id TEXT,
            unresolved_effect_id TEXT,
            last_receipt_transaction_id TEXT NOT NULL,
            previous_state_sha256 BLOB NOT NULL CHECK(
                typeof(previous_state_sha256) = 'blob' AND length(previous_state_sha256) = 32
            ),
            state_sha256 BLOB NOT NULL CHECK(
                typeof(state_sha256) = 'blob' AND length(state_sha256) = 32 AND
                state_sha256 != zeroblob(32)
            ),
            updated_at TEXT NOT NULL CHECK(
                length(CAST(updated_at AS BLOB)) = 24 AND
                strftime('%Y-%m-%dT%H:%M:%fZ', updated_at) = updated_at
            ),
            CHECK(
                (lease_owner IS NULL AND lease_fence IS NULL AND lease_expires_at IS NULL) OR
                (lease_owner IS NOT NULL AND lease_fence IS NOT NULL AND lease_expires_at IS NOT NULL)
            ),
            CHECK(
                (current_attempt_id IS NULL AND current_attempt_number IS NULL AND
                    lease_owner IS NULL) OR
                (current_attempt_id IS NOT NULL AND
                    current_attempt_number = attempt_count AND
                    lease_owner IS NOT NULL AND lease_fence = fence_high_water)
            ),
            CHECK(
                (attempt_count = 0 AND last_attempt_id IS NULL AND
                    last_attempt_number IS NULL) OR
                (attempt_count > 0 AND current_attempt_id IS NULL AND
                    last_attempt_id IS NOT NULL AND last_attempt_number = attempt_count) OR
                (attempt_count > 0 AND current_attempt_id IS NOT NULL AND (
                    (attempt_count = 1 AND last_attempt_id IS NULL AND
                        last_attempt_number IS NULL) OR
                    (attempt_count > 1 AND last_attempt_id IS NOT NULL AND
                        last_attempt_number = attempt_count - 1)
                ))
            ),
            CHECK((status = 'retry') = (next_attempt_at IS NOT NULL)),
            CHECK(
                (status = 'cancel_requested' AND cancellation_pending = 1) OR
                (status IN ('outcome_unknown', 'repair_required')) OR
                (status NOT IN ('cancel_requested', 'outcome_unknown', 'repair_required') AND
                    cancellation_pending = 0)
            ),
            CHECK(cancellation_pending = 0 OR target_kind = 'pipeline'),
            CHECK(current_effect_id IS NULL OR current_attempt_id IS NOT NULL),
            CHECK(
                (current_attempt_id IS NOT NULL AND (
                    (target_kind IN ('pipeline', 'action') AND status = 'running') OR
                    (target_kind = 'pipeline' AND status = 'cancel_requested') OR
                    (target_kind = 'outbox' AND status = 'leased')
                )) OR
                (current_attempt_id IS NULL AND NOT (
                    (target_kind IN ('pipeline', 'action') AND status = 'running') OR
                    (target_kind = 'outbox' AND status = 'leased')
                ))
            ),
            CHECK(
                (status IN ('outcome_unknown', 'repair_required') AND
                    unresolved_effect_id IS NOT NULL AND
                    current_attempt_id IS NULL AND
                    last_attempt_id IS NOT NULL AND
                    last_attempt_number IS NOT NULL AND attempt_count > 0) OR
                (status NOT IN ('outcome_unknown', 'repair_required') AND
                    unresolved_effect_id IS NULL)
            ),
            FOREIGN KEY(target_id, target_kind)
                REFERENCES worker_targets(target_id, target_kind)
                ON UPDATE RESTRICT ON DELETE RESTRICT,
            FOREIGN KEY(last_receipt_transaction_id)
                REFERENCES transaction_receipts(transaction_id)
                ON UPDATE RESTRICT ON DELETE RESTRICT,
            FOREIGN KEY(last_attempt_id, target_id, last_attempt_number)
                REFERENCES worker_attempts(attempt_id, target_id, attempt_number)
                ON UPDATE RESTRICT ON DELETE RESTRICT,
            FOREIGN KEY(
                current_attempt_id, target_id, current_attempt_number,
                lease_owner, lease_fence
            ) REFERENCES worker_attempts(
                attempt_id, target_id, attempt_number, owner_id, execution_fence
            )
                ON UPDATE RESTRICT ON DELETE RESTRICT,
            FOREIGN KEY(current_effect_id, target_id, current_attempt_id)
                REFERENCES worker_effects(effect_id, target_id, attempt_id)
                ON UPDATE RESTRICT ON DELETE RESTRICT,
            FOREIGN KEY(unresolved_effect_id, target_id, last_attempt_id)
                REFERENCES worker_effects(effect_id, target_id, attempt_id)
                ON UPDATE RESTRICT ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE worker_attempts (
            attempt_id TEXT NOT NULL PRIMARY KEY CHECK(
                length(attempt_id) = 32 AND length(CAST(attempt_id AS BLOB)) = 32 AND
                attempt_id NOT GLOB '*[^0-9a-f]*'
            ),
            target_id TEXT NOT NULL,
            attempt_number INTEGER NOT NULL CHECK(attempt_number BETWEEN 1 AND 100),
            previous_attempt_id TEXT,
            execution_fence INTEGER NOT NULL CHECK(
                execution_fence BETWEEN 1 AND 9223372036854775807
            ),
            owner_id TEXT NOT NULL CHECK(length(CAST(owner_id AS BLOB)) BETWEEN 1 AND 1024),
            started_at TEXT NOT NULL CHECK(
                length(CAST(started_at AS BLOB)) = 24 AND
                strftime('%Y-%m-%dT%H:%M:%fZ', started_at) = started_at
            ),
            receipt_transaction_id TEXT NOT NULL UNIQUE,
            UNIQUE(target_id, attempt_number),
            UNIQUE(attempt_id, target_id),
            UNIQUE(attempt_id, target_id, attempt_number),
            UNIQUE(
                attempt_id, target_id, attempt_number, owner_id, execution_fence
            ),
            CHECK(
                (attempt_number = 1 AND previous_attempt_id IS NULL) OR
                (attempt_number > 1 AND previous_attempt_id IS NOT NULL)
            ),
            FOREIGN KEY(target_id)
                REFERENCES worker_targets(target_id)
                ON UPDATE RESTRICT ON DELETE RESTRICT,
            FOREIGN KEY(previous_attempt_id, target_id)
                REFERENCES worker_attempts(attempt_id, target_id)
                ON UPDATE RESTRICT ON DELETE RESTRICT
                DEFERRABLE INITIALLY DEFERRED,
            FOREIGN KEY(receipt_transaction_id)
                REFERENCES worker_execution_receipts(transaction_id)
                ON UPDATE RESTRICT ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE worker_effects (
            effect_id TEXT NOT NULL PRIMARY KEY CHECK(
                length(effect_id) = 32 AND length(CAST(effect_id AS BLOB)) = 32 AND
                effect_id NOT GLOB '*[^0-9a-f]*'
            ),
            target_id TEXT NOT NULL,
            attempt_id TEXT NOT NULL,
            effect_kind TEXT NOT NULL CHECK(length(CAST(effect_kind AS BLOB)) BETWEEN 1 AND 64),
            authorization_transaction_id TEXT NOT NULL UNIQUE,
            idempotency_key TEXT NOT NULL CHECK(
                length(CAST(idempotency_key AS BLOB)) BETWEEN 1 AND 1024
            ),
            authorized_at TEXT NOT NULL CHECK(
                length(CAST(authorized_at AS BLOB)) = 24 AND
                strftime('%Y-%m-%dT%H:%M:%fZ', authorized_at) = authorized_at
            ),
            UNIQUE(target_id, attempt_id, effect_kind),
            UNIQUE(effect_id, target_id),
            UNIQUE(effect_id, target_id, attempt_id),
            FOREIGN KEY(attempt_id, target_id)
                REFERENCES worker_attempts(attempt_id, target_id)
                ON UPDATE RESTRICT ON DELETE RESTRICT,
            FOREIGN KEY(authorization_transaction_id)
                REFERENCES worker_execution_receipts(transaction_id)
                ON UPDATE RESTRICT ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE worker_results (
            result_id TEXT NOT NULL PRIMARY KEY CHECK(
                length(result_id) = 32 AND length(CAST(result_id AS BLOB)) = 32 AND
                result_id NOT GLOB '*[^0-9a-f]*'
            ),
            target_id TEXT NOT NULL,
            attempt_id TEXT,
            effect_id TEXT,
            terminal_status TEXT NOT NULL CHECK(
                length(CAST(terminal_status AS BLOB)) BETWEEN 1 AND 64
            ),
            result_sha256 BLOB NOT NULL CHECK(
                typeof(result_sha256) = 'blob' AND length(result_sha256) = 32
            ),
            proof_sha256 BLOB NOT NULL CHECK(
                typeof(proof_sha256) = 'blob' AND length(proof_sha256) = 32
            ),
            receipt_transaction_id TEXT NOT NULL UNIQUE,
            completed_at TEXT NOT NULL CHECK(
                length(CAST(completed_at AS BLOB)) = 24 AND
                strftime('%Y-%m-%dT%H:%M:%fZ', completed_at) = completed_at
            ),
            UNIQUE(result_id, target_id),
            CHECK(effect_id IS NULL OR attempt_id IS NOT NULL),
            FOREIGN KEY(target_id)
                REFERENCES worker_targets(target_id)
                ON UPDATE RESTRICT ON DELETE RESTRICT,
            FOREIGN KEY(attempt_id, target_id)
                REFERENCES worker_attempts(attempt_id, target_id)
                ON UPDATE RESTRICT ON DELETE RESTRICT,
            FOREIGN KEY(effect_id, target_id, attempt_id)
                REFERENCES worker_effects(effect_id, target_id, attempt_id)
                ON UPDATE RESTRICT ON DELETE RESTRICT,
            FOREIGN KEY(
                receipt_transaction_id, target_id, terminal_status, completed_at
            ) REFERENCES worker_execution_receipts(
                transaction_id, target_id, after_status, committed_at
            )
                ON UPDATE RESTRICT ON DELETE RESTRICT
        ) STRICT
        """,
        f"""
        CREATE TABLE worker_reconcile_proofs (
            proof_id TEXT NOT NULL PRIMARY KEY CHECK(
                length(proof_id) = 32 AND length(CAST(proof_id AS BLOB)) = 32 AND
                proof_id NOT GLOB '*[^0-9a-f]*'
            ),
            target_id TEXT NOT NULL,
            effect_id TEXT NOT NULL,
            result_id TEXT,
            proof_kind TEXT NOT NULL CHECK(
                proof_kind IN (
                    'result_commit', 'provider_receipt', 'durable_result',
                    'proven_not_executed', 'provider_absence',
                    'terminal_failure', 'still_unknown'
                )
            ),
            proof_bytes BLOB NOT NULL CHECK(
                typeof(proof_bytes) = 'blob' AND
                length(proof_bytes) BETWEEN 1 AND {MAX_BUSINESS_SMALL_PAYLOAD_BYTES}
            ),
            proof_sha256 BLOB NOT NULL CHECK(
                typeof(proof_sha256) = 'blob' AND length(proof_sha256) = 32
            ),
            receipt_transaction_id TEXT NOT NULL UNIQUE,
            observed_at TEXT NOT NULL CHECK(
                length(CAST(observed_at AS BLOB)) = 24 AND
                strftime('%Y-%m-%dT%H:%M:%fZ', observed_at) = observed_at
            ),
            CHECK((proof_kind = 'durable_result') = (result_id IS NOT NULL)),
            FOREIGN KEY(target_id)
                REFERENCES worker_targets(target_id)
                ON UPDATE RESTRICT ON DELETE RESTRICT,
            FOREIGN KEY(effect_id, target_id)
                REFERENCES worker_effects(effect_id, target_id)
                ON UPDATE RESTRICT ON DELETE RESTRICT,
            FOREIGN KEY(result_id, target_id)
                REFERENCES worker_results(result_id, target_id)
                ON UPDATE RESTRICT ON DELETE RESTRICT,
            FOREIGN KEY(receipt_transaction_id)
                REFERENCES worker_execution_receipts(transaction_id)
                ON UPDATE RESTRICT ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE outbox_allocations (
            sequence INTEGER NOT NULL PRIMARY KEY CHECK(
                sequence BETWEEN 1 AND 9223372036854775807
            ),
            event_id TEXT NOT NULL UNIQUE CHECK(
                length(event_id) = 32 AND length(CAST(event_id AS BLOB)) = 32 AND
                event_id NOT GLOB '*[^0-9a-f]*'
            ),
            commit_id TEXT NOT NULL,
            repository_revision INTEGER NOT NULL CHECK(
                repository_revision BETWEEN 1 AND 9223372036854775807
            ),
            ordinal INTEGER NOT NULL CHECK(ordinal BETWEEN 0 AND 9999),
            previous_tail_sha256 BLOB NOT NULL CHECK(
                typeof(previous_tail_sha256) = 'blob' AND length(previous_tail_sha256) = 32
            ),
            allocation_tail_sha256 BLOB NOT NULL UNIQUE CHECK(
                typeof(allocation_tail_sha256) = 'blob' AND
                length(allocation_tail_sha256) = 32 AND
                allocation_tail_sha256 != zeroblob(32)
            ),
            allocated_at TEXT NOT NULL CHECK(
                length(CAST(allocated_at AS BLOB)) = 24 AND
                strftime('%Y-%m-%dT%H:%M:%fZ', allocated_at) = allocated_at
            ),
            CHECK(
                (sequence = 1 AND previous_tail_sha256 = zeroblob(32)) OR
                (sequence > 1 AND previous_tail_sha256 != zeroblob(32))
            ),
            UNIQUE(sequence, repository_revision, ordinal),
            UNIQUE(event_id, sequence, repository_revision, ordinal),
            FOREIGN KEY(event_id)
                REFERENCES event_outbox(event_id)
                ON UPDATE RESTRICT ON DELETE RESTRICT,
            FOREIGN KEY(commit_id)
                REFERENCES atomic_business_write_receipts(commit_id)
                ON UPDATE RESTRICT ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE outbox_allocation_head (
            singleton INTEGER NOT NULL PRIMARY KEY CHECK(singleton = 1),
            state_revision INTEGER NOT NULL CHECK(
                state_revision BETWEEN 1 AND 9223372036854775807
            ),
            sequence INTEGER NOT NULL CHECK(sequence BETWEEN 0 AND 9223372036854775807),
            previous_tail_sha256 BLOB NOT NULL CHECK(
                typeof(previous_tail_sha256) = 'blob' AND length(previous_tail_sha256) = 32
            ),
            tail_sha256 BLOB NOT NULL UNIQUE CHECK(
                typeof(tail_sha256) = 'blob' AND length(tail_sha256) = 32
            ),
            updated_at TEXT NOT NULL CHECK(
                length(CAST(updated_at AS BLOB)) = 24 AND
                strftime('%Y-%m-%dT%H:%M:%fZ', updated_at) = updated_at
            ),
            CHECK((sequence = 0) = (tail_sha256 = zeroblob(32)))
        ) STRICT
        """,
        """
        CREATE TABLE outbox_dispatch_head (
            singleton INTEGER NOT NULL PRIMARY KEY CHECK(singleton = 1),
            state_revision INTEGER NOT NULL CHECK(
                state_revision BETWEEN 1 AND 9223372036854775807
            ),
            published_sequence INTEGER NOT NULL CHECK(
                published_sequence BETWEEN 0 AND 9223372036854775807
            ),
            published_repository_revision INTEGER,
            published_ordinal INTEGER,
            inflight_sequence INTEGER,
            inflight_repository_revision INTEGER,
            inflight_ordinal INTEGER,
            previous_head_sha256 BLOB NOT NULL CHECK(
                typeof(previous_head_sha256) = 'blob' AND length(previous_head_sha256) = 32
            ),
            head_sha256 BLOB NOT NULL UNIQUE CHECK(
                typeof(head_sha256) = 'blob' AND length(head_sha256) = 32
            ),
            updated_at TEXT NOT NULL CHECK(
                length(CAST(updated_at AS BLOB)) = 24 AND
                strftime('%Y-%m-%dT%H:%M:%fZ', updated_at) = updated_at
            ),
            CHECK(
                (published_sequence = 0 AND published_repository_revision IS NULL AND
                    published_ordinal IS NULL) OR
                (published_sequence > 0 AND published_repository_revision BETWEEN 1 AND
                    9223372036854775807 AND published_ordinal BETWEEN 0 AND 9999)
            ),
            CHECK(
                (inflight_sequence IS NULL AND inflight_repository_revision IS NULL AND
                    inflight_ordinal IS NULL) OR
                (inflight_sequence = published_sequence + 1 AND
                    inflight_repository_revision BETWEEN 1 AND 9223372036854775807 AND
                    inflight_ordinal BETWEEN 0 AND 9999)
            ),
            FOREIGN KEY(
                published_sequence, published_repository_revision,
                published_ordinal
            ) REFERENCES outbox_allocations(
                sequence, repository_revision, ordinal
            ) ON UPDATE RESTRICT ON DELETE RESTRICT,
            FOREIGN KEY(
                inflight_sequence, inflight_repository_revision, inflight_ordinal
            ) REFERENCES outbox_allocations(
                sequence, repository_revision, ordinal
            ) ON UPDATE RESTRICT ON DELETE RESTRICT
        ) STRICT
        """,
        f"""
        CREATE TABLE business_cutover_head (
            singleton INTEGER NOT NULL PRIMARY KEY CHECK(singleton = 1),
            cutover_id TEXT NOT NULL UNIQUE CHECK(
                length(cutover_id) = 64 AND length(CAST(cutover_id AS BLOB)) = 64 AND
                cutover_id NOT GLOB '*[^0-9a-f]*'
            ),
            state_revision INTEGER NOT NULL CHECK(
                state_revision BETWEEN 1 AND 9223372036854775807
            ),
            phase_revision INTEGER NOT NULL CHECK(phase_revision BETWEEN 1 AND 5),
            phase TEXT NOT NULL CHECK(
                (phase_revision = 1 AND phase = 'installed') OR
                (phase_revision = 2 AND phase = 'assets_covered') OR
                (phase_revision = 3 AND phase = 'audited') OR
                (phase_revision = 4 AND phase = 'activated') OR
                (phase_revision = 5 AND phase = 'mounted')
            ),
            authority_state TEXT NOT NULL CHECK(
                (phase_revision < 4 AND authority_state = 'read_only') OR
                (phase_revision >= 4 AND authority_state = 'active')
            ),
            repository_epoch TEXT NOT NULL CHECK(
                length(repository_epoch) = 64 AND length(CAST(repository_epoch AS BLOB)) = 64 AND
                repository_epoch NOT GLOB '*[^0-9a-f]*'
            ),
            runtime_generation TEXT NOT NULL CHECK(
                length(CAST(runtime_generation AS BLOB)) BETWEEN 1 AND
                    {MAX_BUSINESS_RUNTIME_GENERATION_BYTES}
            ),
            code_digest BLOB NOT NULL CHECK(
                typeof(code_digest) = 'blob' AND length(code_digest) = 32
            ),
            install_transaction_id TEXT NOT NULL,
            install_manifest_sha256 BLOB NOT NULL CHECK(
                typeof(install_manifest_sha256) = 'blob' AND length(install_manifest_sha256) = 32
            ),
            coverage_transaction_id TEXT,
            coverage_manifest_sha256 BLOB,
            audit_transaction_id TEXT,
            audit_manifest_sha256 BLOB,
            activation_transaction_id TEXT,
            activation_manifest_sha256 BLOB,
            mount_transaction_id TEXT,
            mount_manifest_sha256 BLOB,
            previous_head_sha256 BLOB NOT NULL CHECK(
                typeof(previous_head_sha256) = 'blob' AND length(previous_head_sha256) = 32
            ),
            head_sha256 BLOB NOT NULL UNIQUE CHECK(
                typeof(head_sha256) = 'blob' AND length(head_sha256) = 32 AND
                head_sha256 != zeroblob(32)
            ),
            updated_at TEXT NOT NULL CHECK(
                length(CAST(updated_at AS BLOB)) = 24 AND
                strftime('%Y-%m-%dT%H:%M:%fZ', updated_at) = updated_at
            ),
            CHECK((coverage_transaction_id IS NULL) = (phase_revision < 2)),
            CHECK((coverage_manifest_sha256 IS NULL) = (phase_revision < 2)),
            CHECK((audit_transaction_id IS NULL) = (phase_revision < 3)),
            CHECK((audit_manifest_sha256 IS NULL) = (phase_revision < 3)),
            CHECK((activation_transaction_id IS NULL) = (phase_revision < 4)),
            CHECK((activation_manifest_sha256 IS NULL) = (phase_revision < 4)),
            CHECK((mount_transaction_id IS NULL) = (phase_revision < 5)),
            CHECK((mount_manifest_sha256 IS NULL) = (phase_revision < 5)),
            CHECK(
                (coverage_manifest_sha256 IS NULL OR (
                    typeof(coverage_manifest_sha256) = 'blob' AND
                    length(coverage_manifest_sha256) = 32
                )) AND
                (audit_manifest_sha256 IS NULL OR (
                    typeof(audit_manifest_sha256) = 'blob' AND
                    length(audit_manifest_sha256) = 32
                )) AND
                (activation_manifest_sha256 IS NULL OR (
                    typeof(activation_manifest_sha256) = 'blob' AND
                    length(activation_manifest_sha256) = 32
                )) AND
                (mount_manifest_sha256 IS NULL OR (
                    typeof(mount_manifest_sha256) = 'blob' AND
                    length(mount_manifest_sha256) = 32
                ))
            ),
            FOREIGN KEY(cutover_id, install_transaction_id, install_manifest_sha256)
                REFERENCES business_cutover_install_receipts(
                    cutover_id, transaction_id, manifest_sha256
                )
                ON UPDATE RESTRICT ON DELETE RESTRICT,
            FOREIGN KEY(cutover_id, coverage_transaction_id, coverage_manifest_sha256)
                REFERENCES business_asset_coverage_receipts(
                    cutover_id, transaction_id, manifest_sha256
                )
                ON UPDATE RESTRICT ON DELETE RESTRICT,
            FOREIGN KEY(cutover_id, audit_transaction_id, audit_manifest_sha256)
                REFERENCES repository_audit_certificates(
                    cutover_id, transaction_id, manifest_sha256
                )
                ON UPDATE RESTRICT ON DELETE RESTRICT,
            FOREIGN KEY(cutover_id, activation_transaction_id, activation_manifest_sha256)
                REFERENCES business_cutover_activation_receipts(
                    cutover_id, transaction_id, manifest_sha256
                )
                ON UPDATE RESTRICT ON DELETE RESTRICT,
            FOREIGN KEY(cutover_id, mount_transaction_id, mount_manifest_sha256)
                REFERENCES business_cutover_mount_receipts(
                    cutover_id, transaction_id, manifest_sha256
                )
                ON UPDATE RESTRICT ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TRIGGER worker_ledger_entries_chain_guard
        BEFORE INSERT ON worker_ledger_entries
        WHEN NEW.sequence != COALESCE(
                 (SELECT max(sequence) + 1 FROM worker_ledger_entries), 1
             ) OR
             NEW.previous_tail_sha256 != COALESCE(
                 (SELECT ledger_tail_sha256 FROM worker_ledger_entries
                  ORDER BY sequence DESC LIMIT 1),
                 zeroblob(32)
             )
        BEGIN SELECT RAISE(ABORT, 'production_worker_ledger_chain_invalid'); END
        """,
        """
        CREATE TRIGGER repository_audit_certificates_chain_guard
        BEFORE INSERT ON repository_audit_certificates
        WHEN NEW.sequence != COALESCE(
                 (SELECT max(sequence) + 1 FROM repository_audit_certificates), 1
             ) OR
             NEW.previous_tail_sha256 != COALESCE(
                 (SELECT certificate_tail_sha256
                  FROM repository_audit_certificates
                  ORDER BY sequence DESC LIMIT 1),
                 zeroblob(32)
             ) OR
             NOT (
                 (NEW.sequence = 1 AND NEW.previous_certificate_id IS NULL) OR
                 (NEW.sequence > 1 AND NEW.previous_certificate_id = (
                     SELECT certificate_id FROM repository_audit_certificates
                     ORDER BY sequence DESC LIMIT 1
                 ))
             )
        BEGIN SELECT RAISE(ABORT, 'production_audit_ledger_chain_invalid'); END
        """,
        """
        CREATE TRIGGER outbox_allocations_chain_guard
        BEFORE INSERT ON outbox_allocations
        WHEN NEW.sequence != COALESCE(
                 (SELECT max(sequence) + 1 FROM outbox_allocations), 1
             ) OR
             NEW.previous_tail_sha256 != COALESCE(
                 (SELECT allocation_tail_sha256 FROM outbox_allocations
                  ORDER BY sequence DESC LIMIT 1),
                 zeroblob(32)
             )
        BEGIN SELECT RAISE(ABORT, 'production_outbox_allocation_chain_invalid'); END
        """,
        """
        CREATE TRIGGER outbox_allocations_event_binding_guard
        BEFORE INSERT ON outbox_allocations
        WHEN NOT EXISTS (
                 SELECT 1 FROM event_outbox
                 WHERE event_id = NEW.event_id AND commit_id = NEW.commit_id AND
                       repository_revision = NEW.repository_revision AND
                       ordinal = NEW.ordinal
             ) OR EXISTS (
                 SELECT 1
                 FROM business_commits AS business_commit
                 JOIN repository_runtime_control AS runtime
                   ON runtime.singleton = 1 AND
                      runtime.repository_revision >=
                          business_commit.repository_revision
                 WHERE business_commit.commit_id = NEW.commit_id
             )
        BEGIN SELECT RAISE(ABORT, 'production_outbox_allocation_binding_invalid'); END
        """,
        """
        CREATE TRIGGER pipeline_control_receipts_binding_guard
        BEFORE INSERT ON pipeline_control_receipts
        WHEN NOT EXISTS (
            SELECT 1 FROM pipeline_control_intents
            WHERE intent_id = NEW.intent_id AND operation = NEW.operation AND
                  intent_type = NEW.intent_type AND
                  target_id = NEW.target_id AND before_status IS NEW.before_status AND
                  after_status = NEW.after_status AND
                  before_state_revision IS NEW.before_state_revision AND
                  after_state_revision = NEW.after_state_revision AND
                  request_sha256 = NEW.request_sha256 AND
                  committed_at = NEW.committed_at
        )
        BEGIN SELECT RAISE(ABORT, 'production_pipeline_control_binding_invalid'); END
        """,
        """
        CREATE TRIGGER atomic_business_control_members_binding_guard
        BEFORE INSERT ON atomic_business_control_members
        WHEN NOT EXISTS (
                 SELECT 1
                 FROM atomic_business_write_receipts AS atomic_receipt
                 JOIN pipeline_control_intents AS control_intent
                   ON control_intent.intent_id = NEW.intent_id AND
                      control_intent.origin_commit_id = atomic_receipt.commit_id AND
                      control_intent.ordinal = NEW.ordinal
                 WHERE atomic_receipt.commit_id = NEW.commit_id AND
                       NEW.ordinal < atomic_receipt.control_count
             ) OR EXISTS (
                 SELECT 1
                 FROM business_commits AS business_commit
                 JOIN repository_runtime_control AS runtime
                   ON runtime.singleton = 1 AND
                      runtime.repository_revision >=
                          business_commit.repository_revision
                 WHERE business_commit.commit_id = NEW.commit_id
             )
        BEGIN SELECT RAISE(ABORT, 'production_atomic_control_binding_invalid'); END
        """,
        """
        CREATE TRIGGER pipeline_control_intents_origin_guard
        BEFORE INSERT ON pipeline_control_intents
        WHEN NOT EXISTS (
                 SELECT 1 FROM business_commits
                 WHERE commit_id = NEW.origin_commit_id AND
                       NEW.ordinal < pipeline_intent_count
             ) OR EXISTS (
                 SELECT 1
                 FROM business_commits AS business_commit
                 JOIN repository_runtime_control AS runtime
                   ON runtime.singleton = 1 AND
                      runtime.repository_revision >=
                          business_commit.repository_revision
                 WHERE business_commit.commit_id = NEW.origin_commit_id
             )
        BEGIN SELECT RAISE(ABORT, 'production_pipeline_intent_finalized'); END
        """,
        """
        CREATE TRIGGER pipeline_control_intents_legacy_binding_guard
        BEFORE INSERT ON pipeline_control_intents
        WHEN NOT EXISTS (
            SELECT 1 FROM pipeline_job_transitions AS legacy_intent
            WHERE legacy_intent.intent_id = NEW.intent_id AND
                  legacy_intent.origin_commit_id = NEW.origin_commit_id AND
                  legacy_intent.ordinal = NEW.ordinal AND
                  legacy_intent.job_id = NEW.target_id AND
                  legacy_intent.intent_type = NEW.intent_type AND
                  legacy_intent.idempotency_key = NEW.idempotency_key AND
                  legacy_intent.before_status IS NEW.before_status AND
                  legacy_intent.after_status = NEW.after_status AND
                  legacy_intent.before_state_revision IS
                      NEW.before_state_revision AND
                  legacy_intent.after_state_revision =
                      NEW.after_state_revision AND
                  legacy_intent.intent_bytes = NEW.payload_bytes AND
                  legacy_intent.intent_sha256 = NEW.payload_sha256 AND
                  legacy_intent.payload_schema = NEW.payload_schema AND
                  legacy_intent.codec_id = NEW.payload_codec_id AND
                  legacy_intent.codec_version = NEW.payload_codec_version AND
                  legacy_intent.replay_policy = NEW.replay_policy AND
                  legacy_intent.max_attempts = NEW.max_attempts AND
                  legacy_intent.committed_at = NEW.committed_at AND
                  ((NEW.operation = 'create' AND
                        legacy_intent.intent_kind = 'create') OR
                   (NEW.operation = 'apply_control_intent' AND
                        legacy_intent.intent_kind = 'transition'))
        )
        BEGIN SELECT RAISE(ABORT, 'production_pipeline_legacy_binding_invalid'); END
        """,
        """
        CREATE TRIGGER pipeline_control_intents_create_binding_guard
        BEFORE INSERT ON pipeline_control_intents
        WHEN NEW.operation = 'create' AND NOT EXISTS (
            SELECT 1 FROM worker_targets
            WHERE target_id = NEW.target_id AND target_kind = 'pipeline' AND
                  origin_commit_id = NEW.origin_commit_id AND
                  source_ordinal = NEW.ordinal AND
                  intent_type = NEW.intent_type AND
                  idempotency_key = NEW.idempotency_key AND
                  payload_schema = NEW.payload_schema AND
                  payload_codec_id = NEW.payload_codec_id AND
                  payload_codec_version = NEW.payload_codec_version AND
                  payload_bytes = NEW.payload_bytes AND
                  payload_sha256 = NEW.payload_sha256 AND
                  replay_policy = NEW.replay_policy AND
                  idempotency_policy = NEW.idempotency_policy AND
                  max_attempts = NEW.max_attempts AND
                  genesis_status = NEW.after_status AND
                  created_at = NEW.committed_at
        )
        BEGIN SELECT RAISE(ABORT, 'production_pipeline_create_binding_invalid'); END
        """,
        """
        CREATE TRIGGER worker_target_current_insert_binding_guard
        BEFORE INSERT ON worker_target_current
        WHEN NOT (
            NEW.state_revision = 1 AND
            NEW.previous_state_sha256 = zeroblob(32) AND
            NEW.attempt_count = 0 AND NEW.last_attempt_id IS NULL AND
            NEW.last_attempt_number IS NULL AND
            NEW.current_attempt_id IS NULL AND
            NEW.current_attempt_number IS NULL AND
            NEW.fence_high_water = 0 AND NEW.cancellation_pending = 0 AND
            NEW.next_attempt_at IS NULL AND NEW.lease_owner IS NULL AND
            NEW.lease_fence IS NULL AND NEW.lease_expires_at IS NULL AND
            NEW.current_effect_id IS NULL AND NEW.unresolved_effect_id IS NULL AND
            (
                EXISTS (
                    SELECT 1
                    FROM worker_targets AS target
                    JOIN atomic_business_write_receipts AS atomic_receipt
                      ON atomic_receipt.commit_id = target.origin_commit_id
                    WHERE target.target_id = NEW.target_id AND
                          target.target_kind = NEW.target_kind AND
                          target.target_kind IN ('action', 'outbox') AND
                          target.genesis_status = NEW.status AND
                          target.genesis_state_sha256 = NEW.state_sha256 AND
                          atomic_receipt.transaction_id =
                              NEW.last_receipt_transaction_id AND
                          atomic_receipt.committed_at = NEW.updated_at
                ) OR
                EXISTS (
                    SELECT 1
                    FROM worker_targets AS target
                    JOIN pipeline_control_receipts AS control_receipt
                      ON control_receipt.target_id = target.target_id
                    WHERE target.target_id = NEW.target_id AND
                          target.target_kind = NEW.target_kind AND
                          target.target_kind = 'pipeline' AND
                          target.genesis_status = NEW.status AND
                          target.genesis_state_sha256 = NEW.state_sha256 AND
                          control_receipt.operation = 'create' AND
                          control_receipt.before_state_revision IS NULL AND
                          control_receipt.after_state_revision = 1 AND
                          control_receipt.after_status = NEW.status AND
                          control_receipt.after_state_sha256 = NEW.state_sha256 AND
                          control_receipt.transaction_id =
                              NEW.last_receipt_transaction_id AND
                          control_receipt.committed_at = NEW.updated_at
                )
            )
        )
        BEGIN SELECT RAISE(ABORT, 'production_worker_current_binding_invalid'); END
        """,
        """
        CREATE TRIGGER worker_targets_action_binding_guard
        BEFORE INSERT ON worker_targets
        WHEN NEW.target_kind = 'action' AND NOT EXISTS (
            SELECT 1 FROM deferred_actions
            WHERE action_id = NEW.target_id AND
                  project_id = NEW.project_id AND generation = NEW.generation AND
                  document_id = NEW.document_id AND
                  origin_commit_id = NEW.origin_commit_id AND
                  ordinal = NEW.source_ordinal AND
                  action_kind = NEW.action_type AND
                  action_bytes = NEW.payload_bytes AND
                  action_sha256 = NEW.payload_sha256 AND
                  payload_schema = NEW.payload_schema AND
                  codec_id = NEW.payload_codec_id AND
                  codec_version = NEW.payload_codec_version AND
                  idempotency_scope = NEW.idempotency_scope AND
                  idempotency_key = NEW.idempotency_key AND
                  replay_policy = NEW.replay_policy AND
                  idempotency_policy = NEW.idempotency_policy AND
                  max_attempts = NEW.max_attempts AND
                  status = NEW.genesis_status AND status = 'queued' AND
                  state_revision = 1 AND attempt_count = 0 AND
                  last_attempt_id IS NULL AND last_attempt_number IS NULL AND
                  next_attempt_at = NEW.created_at AND lease_owner IS NULL AND
                  lease_fence IS NULL AND lease_expires_at IS NULL AND
                  result_commit_id IS NULL AND current_attempt_id IS NULL AND
                  current_attempt_started_at IS NULL AND
                  created_at = NEW.created_at AND updated_at = NEW.created_at
        )
        BEGIN SELECT RAISE(ABORT, 'production_worker_action_binding_invalid'); END
        """,
        """
        CREATE TRIGGER worker_targets_pipeline_binding_guard
        BEFORE INSERT ON worker_targets
        WHEN NEW.target_kind = 'pipeline' AND NOT EXISTS (
            SELECT 1
            FROM pipeline_jobs AS job
            JOIN pipeline_job_transitions AS creation
              ON creation.intent_id = job.job_id AND
                 creation.job_id = job.job_id AND
                 creation.intent_kind = 'create'
            WHERE job.job_id = NEW.target_id AND
                  job.last_intent_id = creation.intent_id AND
                  job.project_id = NEW.project_id AND
                  job.generation = NEW.generation AND
                  job.document_id = NEW.document_id AND
                  job.pipeline_id = NEW.pipeline_id AND
                  job.intent_type = NEW.intent_type AND
                  job.intent_kind = 'create' AND
                  job.expected_state_revision IS NULL AND
                  job.input_commit_id = NEW.origin_commit_id AND
                  job.head_commit_id = NEW.origin_commit_id AND
                  job.job_bytes = NEW.payload_bytes AND
                  job.job_sha256 = NEW.payload_sha256 AND
                  job.payload_schema = NEW.payload_schema AND
                  job.codec_id = NEW.payload_codec_id AND
                  job.codec_version = NEW.payload_codec_version AND
                  job.idempotency_key = NEW.idempotency_key AND
                  job.replay_policy = NEW.replay_policy AND
                  job.max_attempts = NEW.max_attempts AND
                  job.status = NEW.genesis_status AND
                  job.status = 'queued' AND job.state_revision = 1 AND
                  job.attempt_count = 0 AND job.last_attempt_id IS NULL AND
                  job.last_attempt_number IS NULL AND
                  job.next_attempt_at = NEW.created_at AND
                  job.lease_owner IS NULL AND
                  job.lease_fence IS NULL AND job.lease_expires_at IS NULL AND
                  job.result_commit_id IS NULL AND
                  job.current_attempt_id IS NULL AND
                  job.current_attempt_started_at IS NULL AND
                  job.created_at = NEW.created_at AND
                  job.updated_at = NEW.created_at AND
                  creation.origin_commit_id = NEW.origin_commit_id AND
                  creation.ordinal = NEW.source_ordinal AND
                  creation.project_id = NEW.project_id AND
                  creation.generation = NEW.generation AND
                  creation.document_id = NEW.document_id AND
                  creation.intent_type = NEW.intent_type AND
                  creation.idempotency_key = NEW.idempotency_key AND
                  creation.before_state_revision IS NULL AND
                  creation.after_state_revision = 1 AND
                  creation.before_status IS NULL AND
                  creation.after_status = NEW.genesis_status AND
                  creation.intent_bytes = NEW.payload_bytes AND
                  creation.intent_sha256 = NEW.payload_sha256 AND
                  creation.payload_schema = NEW.payload_schema AND
                  creation.codec_id = NEW.payload_codec_id AND
                  creation.codec_version = NEW.payload_codec_version AND
                  creation.replay_policy = NEW.replay_policy AND
                  creation.max_attempts = NEW.max_attempts AND
                  creation.committed_at = NEW.created_at
        )
        BEGIN SELECT RAISE(ABORT, 'production_worker_pipeline_binding_invalid'); END
        """,
        """
        CREATE TRIGGER worker_targets_finalized_guard
        BEFORE INSERT ON worker_targets
        WHEN EXISTS (
            SELECT 1
            FROM business_commits AS business_commit
            JOIN repository_runtime_control AS runtime
              ON runtime.singleton = 1 AND
                 runtime.repository_revision >= business_commit.repository_revision
            WHERE business_commit.commit_id = NEW.origin_commit_id
        )
        BEGIN SELECT RAISE(ABORT, 'production_worker_target_finalized'); END
        """,
        """
        CREATE TRIGGER worker_targets_outbox_binding_guard
        BEFORE INSERT ON worker_targets
        WHEN NEW.target_kind = 'outbox' AND NOT EXISTS (
            SELECT 1
            FROM outbox_allocations AS allocation
            JOIN event_outbox AS event
              ON event.event_id = allocation.event_id AND
                 event.commit_id = allocation.commit_id AND
                 event.repository_revision = allocation.repository_revision AND
                 event.ordinal = allocation.ordinal
            WHERE allocation.sequence = NEW.outbox_sequence AND
                  allocation.repository_revision =
                      NEW.outbox_repository_revision AND
                  allocation.ordinal = NEW.outbox_ordinal AND
                  allocation.ordinal = NEW.source_ordinal AND
                  allocation.event_id = NEW.target_id AND
                  allocation.commit_id = NEW.origin_commit_id AND
                  event.topic = NEW.outbox_topic AND
                  event.partition_key = NEW.outbox_partition_key AND
                  event.event_bytes = NEW.payload_bytes AND
                  event.event_sha256 = NEW.payload_sha256 AND
                  event.payload_schema = NEW.payload_schema AND
                  event.codec_id = NEW.payload_codec_id AND
                  event.codec_version = NEW.payload_codec_version AND
                  event.status = NEW.genesis_status AND
                  event.status = 'pending' AND event.state_revision = 1 AND
                  event.attempt_count = 0 AND
                  event.available_at = NEW.created_at AND
                  event.lease_owner IS NULL AND event.lease_fence IS NULL AND
                  event.lease_expires_at IS NULL AND
                  event.published_at IS NULL AND
                  event.outcome_unknown_at IS NULL AND event.dead_at IS NULL AND
                  event.last_error_bytes IS NULL AND
                  event.last_error_sha256 IS NULL AND
                  event.created_at = NEW.created_at AND
                  event.updated_at = NEW.created_at
        )
        BEGIN SELECT RAISE(ABORT, 'production_worker_outbox_binding_invalid'); END
        """,
        """
        CREATE TRIGGER worker_target_current_update_binding_guard
        BEFORE UPDATE ON worker_target_current
        WHEN NOT EXISTS (
            SELECT 1 FROM worker_execution_receipts
            WHERE transaction_id = NEW.last_receipt_transaction_id AND
                  target_id = NEW.target_id AND target_kind = NEW.target_kind AND
                  before_status = OLD.status AND
                  before_state_revision = OLD.state_revision AND
                  after_status = NEW.status AND
                  after_state_revision = NEW.state_revision AND
                  after_state_sha256 = NEW.state_sha256 AND
                  committed_at = NEW.updated_at
            UNION ALL
            SELECT 1 FROM pipeline_control_receipts
            WHERE transaction_id = NEW.last_receipt_transaction_id AND
                  target_id = NEW.target_id AND before_status = OLD.status AND
                  before_state_revision = OLD.state_revision AND
                  after_status = NEW.status AND
                  after_state_revision = NEW.state_revision AND
                  after_state_sha256 = NEW.state_sha256 AND
                  committed_at = NEW.updated_at
        )
        BEGIN SELECT RAISE(ABORT, 'production_worker_current_binding_invalid'); END
        """,
        """
        CREATE TRIGGER worker_attempts_receipt_binding_guard
        BEFORE INSERT ON worker_attempts
        WHEN NOT EXISTS (
                 SELECT 1 FROM worker_execution_receipts
                 WHERE transaction_id = NEW.receipt_transaction_id AND
                       operation = 'claim' AND target_id = NEW.target_id AND
                       committed_at = NEW.started_at
             ) OR NOT EXISTS (
                 SELECT 1 FROM worker_targets
                 WHERE target_id = NEW.target_id AND
                       NEW.attempt_number <= max_attempts
             ) OR (
                 NEW.attempt_number > 1 AND NOT EXISTS (
                     SELECT 1 FROM worker_attempts
                     WHERE attempt_id = NEW.previous_attempt_id AND
                           target_id = NEW.target_id AND
                           attempt_number = NEW.attempt_number - 1
                 )
             )
        BEGIN SELECT RAISE(ABORT, 'production_worker_attempt_binding_invalid'); END
        """,
        """
        CREATE TRIGGER worker_effects_receipt_binding_guard
        BEFORE INSERT ON worker_effects
        WHEN NOT EXISTS (
            SELECT 1 FROM worker_execution_receipts
            WHERE transaction_id = NEW.authorization_transaction_id AND
                  operation = 'authorize_effect' AND
                  target_id = NEW.target_id AND committed_at = NEW.authorized_at
        ) OR NOT EXISTS (
            SELECT 1 FROM worker_targets
            WHERE target_id = NEW.target_id AND (
                (target_kind = 'pipeline' AND
                    NEW.effect_kind = 'pipeline_document_commit') OR
                (target_kind = 'action' AND
                    NEW.effect_kind = 'action_provider') OR
                (target_kind = 'outbox' AND
                    NEW.effect_kind = 'outbox_publish')
            )
        )
        BEGIN SELECT RAISE(ABORT, 'production_worker_effect_binding_invalid'); END
        """,
        """
        CREATE TRIGGER worker_results_receipt_binding_guard
        BEFORE INSERT ON worker_results
        WHEN NOT EXISTS (
            SELECT 1 FROM worker_execution_receipts
            WHERE transaction_id = NEW.receipt_transaction_id AND
                  operation IN (
                      'acknowledge_cancel', 'complete', 'expire', 'reconcile'
                  ) AND
                  target_id = NEW.target_id AND
                  after_status = NEW.terminal_status AND
                  committed_at = NEW.completed_at
        )
        BEGIN SELECT RAISE(ABORT, 'production_worker_result_binding_invalid'); END
        """,
        """
        CREATE TRIGGER worker_reconcile_proofs_receipt_binding_guard
        BEFORE INSERT ON worker_reconcile_proofs
        WHEN NOT EXISTS (
            SELECT 1 FROM worker_execution_receipts
            WHERE transaction_id = NEW.receipt_transaction_id AND
                  operation = 'reconcile' AND target_id = NEW.target_id AND
                  committed_at = NEW.observed_at
        )
        BEGIN SELECT RAISE(ABORT, 'production_worker_reconcile_binding_invalid'); END
        """,
        """
        CREATE TRIGGER business_asset_stage_members_open_guard
        BEFORE INSERT ON business_asset_stage_members
        WHEN EXISTS (
                 SELECT 1 FROM business_asset_stage_receipts
                 WHERE batch_id = NEW.batch_id
             ) OR EXISTS (
                 SELECT 1 FROM business_asset_coverage_receipts
                 WHERE cutover_id = NEW.cutover_id
             ) OR EXISTS (
                 SELECT 1 FROM business_cutover_head
                 WHERE singleton = 1 AND cutover_id = NEW.cutover_id AND
                       phase_revision >= 2
             )
        BEGIN SELECT RAISE(ABORT, 'production_asset_stage_finalized'); END
        """,
        """
        CREATE TRIGGER business_asset_stage_receipts_closure_guard
        BEFORE INSERT ON business_asset_stage_receipts
        WHEN EXISTS (
                 SELECT 1 FROM business_asset_coverage_receipts
                 WHERE cutover_id = NEW.cutover_id
             ) OR EXISTS (
                 SELECT 1 FROM business_cutover_head
                 WHERE singleton = 1 AND cutover_id = NEW.cutover_id AND
                       phase_revision >= 2
             ) OR EXISTS (
                 SELECT 1 FROM business_asset_stage_members
                 WHERE batch_id = NEW.batch_id AND cutover_id != NEW.cutover_id
             ) OR (
                 SELECT count(*) FROM business_asset_stage_members
                 WHERE batch_id = NEW.batch_id AND
                       cutover_id = NEW.cutover_id AND member_kind = 'project'
             ) != NEW.project_count OR (
                 SELECT count(*) FROM business_asset_stage_members
                 WHERE batch_id = NEW.batch_id AND
                       cutover_id = NEW.cutover_id AND member_kind = 'blob'
             ) != NEW.blob_count OR (
                 SELECT count(*) FROM business_asset_stage_members
                 WHERE batch_id = NEW.batch_id AND
                       cutover_id = NEW.cutover_id AND member_kind = 'occurrence'
             ) != NEW.occurrence_count OR EXISTS (
                 SELECT 1 FROM business_asset_stage_members
                 WHERE batch_id = NEW.batch_id AND cutover_id = NEW.cutover_id AND (
                     (member_kind = 'project' AND ordinal >= NEW.project_count) OR
                     (member_kind = 'blob' AND ordinal >= NEW.blob_count) OR
                     (member_kind = 'occurrence' AND ordinal >= NEW.occurrence_count)
                 )
             ) OR COALESCE((
                 SELECT sum(member_bytes) FROM business_asset_stage_members
                 WHERE batch_id = NEW.batch_id AND
                       cutover_id = NEW.cutover_id AND member_kind = 'blob'
             ), 0) != NEW.unique_blob_bytes OR COALESCE((
                 SELECT sum(member_bytes) FROM business_asset_stage_members
                 WHERE batch_id = NEW.batch_id AND
                       cutover_id = NEW.cutover_id AND member_kind = 'occurrence'
             ), 0) != NEW.referenced_bytes
        BEGIN SELECT RAISE(ABORT, 'production_asset_stage_closure_invalid'); END
        """,
        """
        CREATE TRIGGER business_asset_coverage_receipts_closure_guard
        BEFORE INSERT ON business_asset_coverage_receipts
        WHEN EXISTS (
                 SELECT 1 FROM business_cutover_head
                 WHERE singleton = 1 AND cutover_id = NEW.cutover_id AND
                       phase_revision >= 2
             ) OR EXISTS (
                 SELECT 1 FROM business_asset_stage_receipts
                 WHERE cutover_id = NEW.cutover_id AND (
                     repository_epoch != NEW.repository_epoch OR
                     source_admission_revision !=
                         NEW.source_admission_revision OR
                     source_admission_sha256 !=
                         NEW.source_admission_sha256
                 )
             ) OR (
                 SELECT count(*) FROM business_asset_stage_receipts
                 WHERE cutover_id = NEW.cutover_id
             ) != NEW.batch_count OR COALESCE((
                 SELECT sum(member_count) FROM business_asset_stage_receipts
                 WHERE cutover_id = NEW.cutover_id
             ), 0) != NEW.observed_row_count OR COALESCE((
                 SELECT sum(occurrence_count) FROM business_asset_stage_receipts
                 WHERE cutover_id = NEW.cutover_id
             ), 0) != NEW.observed_occurrence_count OR COALESCE((
                 SELECT sum(length(manifest_bytes))
                 FROM business_asset_stage_receipts
                 WHERE cutover_id = NEW.cutover_id
             ), 0) != NEW.observed_manifest_bytes
        BEGIN SELECT RAISE(ABORT, 'production_asset_coverage_closure_invalid'); END
        """,
        """
        CREATE TRIGGER business_cutover_activation_binding_guard
        BEFORE INSERT ON business_cutover_activation_receipts
        WHEN NOT EXISTS (
                 SELECT 1 FROM business_cutover_install_receipts
                 WHERE transaction_id = NEW.install_transaction_id AND
                       manifest_sha256 = NEW.install_manifest_sha256 AND
                       cutover_id = NEW.cutover_id
             ) OR NOT EXISTS (
                 SELECT 1 FROM repository_audit_certificates
                 WHERE transaction_id = NEW.audit_transaction_id AND
                       manifest_sha256 = NEW.audit_manifest_sha256 AND
                       cutover_id = NEW.cutover_id
             )
        BEGIN SELECT RAISE(ABORT, 'production_cutover_binding_invalid'); END
        """,
        """
        CREATE TRIGGER business_cutover_mount_binding_guard
        BEFORE INSERT ON business_cutover_mount_receipts
        WHEN NOT EXISTS (
                 SELECT 1 FROM business_cutover_install_receipts
                 WHERE transaction_id = NEW.install_transaction_id AND
                       receipt_kind = NEW.install_receipt_kind AND
                       committed_at = NEW.install_committed_at AND
                       manifest_sha256 = NEW.install_manifest_sha256 AND
                       cutover_id = NEW.cutover_id AND
                       repository_epoch = NEW.repository_epoch AND
                       runtime_generation = NEW.runtime_generation AND
                       code_digest = NEW.code_digest
             ) OR NOT EXISTS (
                 SELECT 1 FROM business_asset_coverage_receipts
                 WHERE transaction_id = NEW.coverage_transaction_id AND
                       receipt_kind = NEW.coverage_receipt_kind AND
                       committed_at = NEW.coverage_committed_at AND
                       manifest_sha256 = NEW.coverage_manifest_sha256 AND
                       cutover_id = NEW.cutover_id AND
                       repository_epoch = NEW.repository_epoch
             ) OR NOT EXISTS (
                 SELECT 1 FROM transaction_receipts
                 WHERE transaction_id = NEW.install_transaction_id AND
                       kind = NEW.install_receipt_kind AND
                       committed_at = NEW.install_committed_at
             ) OR NOT EXISTS (
                 SELECT 1 FROM transaction_receipts
                 WHERE transaction_id = NEW.coverage_transaction_id AND
                       kind = NEW.coverage_receipt_kind AND
                       committed_at = NEW.coverage_committed_at
             ) OR NOT EXISTS (
                 SELECT 1 FROM repository_audit_certificates
                 WHERE transaction_id = NEW.audit_transaction_id AND
                       manifest_sha256 = NEW.audit_manifest_sha256 AND
                       cutover_id = NEW.cutover_id
             ) OR NOT EXISTS (
                 SELECT 1 FROM business_cutover_activation_receipts
                 WHERE transaction_id = NEW.activation_transaction_id AND
                       manifest_sha256 = NEW.activation_manifest_sha256 AND
                       cutover_id = NEW.cutover_id
             )
        BEGIN SELECT RAISE(ABORT, 'production_cutover_binding_invalid'); END
        """,
        """
        CREATE TRIGGER business_cutover_head_insert_binding_guard
        BEFORE INSERT ON business_cutover_head
        WHEN NOT EXISTS (
            SELECT 1 FROM business_cutover_install_receipts
            WHERE transaction_id = NEW.install_transaction_id AND
                  manifest_sha256 = NEW.install_manifest_sha256 AND
                  cutover_id = NEW.cutover_id AND
                  repository_epoch = NEW.repository_epoch AND
                  runtime_generation = NEW.runtime_generation AND
                  code_digest = NEW.code_digest
        )
        BEGIN SELECT RAISE(ABORT, 'production_cutover_binding_invalid'); END
        """,
        """
        CREATE TRIGGER business_cutover_head_update_binding_guard
        BEFORE UPDATE ON business_cutover_head
        WHEN NOT EXISTS (
                 SELECT 1 FROM business_cutover_install_receipts
                 WHERE transaction_id = NEW.install_transaction_id AND
                       manifest_sha256 = NEW.install_manifest_sha256 AND
                       cutover_id = NEW.cutover_id
             ) OR (NEW.phase_revision >= 2 AND NOT EXISTS (
                 SELECT 1 FROM business_asset_coverage_receipts
                 WHERE transaction_id = NEW.coverage_transaction_id AND
                       manifest_sha256 = NEW.coverage_manifest_sha256 AND
                       cutover_id = NEW.cutover_id AND
                       repository_epoch = NEW.repository_epoch
             )) OR (NEW.phase_revision >= 3 AND NOT EXISTS (
                 SELECT 1 FROM repository_audit_certificates
                 WHERE transaction_id = NEW.audit_transaction_id AND
                       manifest_sha256 = NEW.audit_manifest_sha256 AND
                       cutover_id = NEW.cutover_id
             )) OR (NEW.phase_revision >= 4 AND NOT EXISTS (
                 SELECT 1 FROM business_cutover_activation_receipts
                 WHERE transaction_id = NEW.activation_transaction_id AND
                       manifest_sha256 = NEW.activation_manifest_sha256 AND
                       cutover_id = NEW.cutover_id
             )) OR (NEW.phase_revision = 5 AND NOT EXISTS (
                 SELECT 1 FROM business_cutover_mount_receipts
                 WHERE transaction_id = NEW.mount_transaction_id AND
                       manifest_sha256 = NEW.mount_manifest_sha256 AND
                       cutover_id = NEW.cutover_id
             ))
        BEGIN SELECT RAISE(ABORT, 'production_cutover_binding_invalid'); END
        """,
        *_production_append_only_triggers("business_cutover_install_receipts"),
        *_production_append_only_triggers("business_asset_stage_receipts"),
        *_production_append_only_triggers("business_asset_stage_members"),
        *_production_append_only_triggers("business_asset_coverage_receipts"),
        *_production_append_only_triggers("repository_audit_certificates"),
        *_production_append_only_triggers("business_cutover_activation_receipts"),
        *_production_append_only_triggers("business_cutover_mount_receipts"),
        *_production_append_only_triggers("atomic_business_write_receipts"),
        *_production_append_only_triggers("atomic_business_control_members"),
        *_production_append_only_triggers("worker_ledger_entries"),
        *_production_append_only_triggers("worker_execution_receipts"),
        *_production_append_only_triggers("pipeline_control_intents"),
        *_production_append_only_triggers("pipeline_control_receipts"),
        *_production_append_only_triggers("worker_targets"),
        *_production_append_only_triggers("worker_attempts"),
        *_production_append_only_triggers("worker_effects"),
        *_production_append_only_triggers("worker_results"),
        *_production_append_only_triggers("worker_reconcile_proofs"),
        *_production_append_only_triggers("outbox_allocations"),
        *_production_head_cas_triggers(
            "repository_audit_head", "singleton", "previous_tail_sha256", "tail_sha256"
        ),
        *_production_head_cas_triggers(
            "worker_ledger_head", "singleton", "previous_tail_sha256", "tail_sha256"
        ),
        *_production_head_cas_triggers(
            "worker_target_current", "target_id", "previous_state_sha256", "state_sha256"
        ),
        *_production_head_cas_triggers(
            "outbox_allocation_head", "singleton", "previous_tail_sha256", "tail_sha256"
        ),
        *_production_head_cas_triggers(
            "outbox_dispatch_head", "singleton", "previous_head_sha256", "head_sha256"
        ),
        *_production_head_cas_triggers(
            "business_cutover_head", "singleton", "previous_head_sha256", "head_sha256"
        ),
        """
        CREATE TRIGGER repository_audit_head_sequence_guard
        BEFORE INSERT ON repository_audit_head
        WHEN NEW.sequence != 1
        BEGIN SELECT RAISE(ABORT, 'production_head_sequence_invalid'); END
        """,
        """
        CREATE TRIGGER repository_audit_head_advance_guard
        BEFORE UPDATE ON repository_audit_head
        WHEN NEW.sequence != OLD.sequence + 1
        BEGIN SELECT RAISE(ABORT, 'production_head_sequence_invalid'); END
        """,
        """
        CREATE TRIGGER worker_ledger_head_initial_guard
        BEFORE INSERT ON worker_ledger_head
        WHEN NEW.sequence != 0 OR NEW.tail_sha256 != zeroblob(32)
        BEGIN SELECT RAISE(ABORT, 'production_head_sequence_invalid'); END
        """,
        """
        CREATE TRIGGER worker_ledger_head_sequence_guard
        BEFORE UPDATE ON worker_ledger_head
        WHEN NEW.sequence <= OLD.sequence OR NOT EXISTS (
            SELECT 1 FROM worker_ledger_entries
            WHERE sequence = NEW.sequence AND ledger_tail_sha256 = NEW.tail_sha256
        )
        BEGIN SELECT RAISE(ABORT, 'production_head_sequence_invalid'); END
        """,
        """
        CREATE TRIGGER worker_ledger_head_current_closure_guard
        BEFORE UPDATE ON worker_ledger_head
        WHEN (
                 SELECT count(*) FROM worker_ledger_entries
                 WHERE sequence > OLD.sequence AND sequence <= NEW.sequence
             ) != (
                 (SELECT count(*) FROM worker_execution_receipts
                  WHERE sequence > OLD.sequence AND sequence <= NEW.sequence) +
                 (SELECT count(*) FROM pipeline_control_receipts
                  WHERE sequence > OLD.sequence AND sequence <= NEW.sequence)
             ) OR EXISTS (
                 SELECT 1
                 FROM worker_execution_receipts AS receipt
                 WHERE receipt.sequence > OLD.sequence AND
                       receipt.sequence <= NEW.sequence AND NOT EXISTS (
                           SELECT 1 FROM worker_target_current AS current
                           WHERE current.target_id = receipt.target_id AND
                                 current.target_kind = receipt.target_kind AND
                                 current.status = receipt.after_status AND
                                 current.state_revision =
                                    receipt.after_state_revision AND
                                 current.state_sha256 =
                                    receipt.after_state_sha256 AND
                                 current.last_receipt_transaction_id =
                                    receipt.transaction_id AND
                                 current.updated_at = receipt.committed_at
                       )
             ) OR EXISTS (
                 SELECT 1
                 FROM pipeline_control_receipts AS receipt
                 WHERE receipt.sequence > OLD.sequence AND
                       receipt.sequence <= NEW.sequence AND NOT EXISTS (
                           SELECT 1 FROM worker_target_current AS current
                           WHERE current.target_id = receipt.target_id AND
                                 current.target_kind = 'pipeline' AND
                                 current.status = receipt.after_status AND
                                 current.state_revision =
                                    receipt.after_state_revision AND
                                 current.state_sha256 =
                                    receipt.after_state_sha256 AND
                                 current.last_receipt_transaction_id =
                                    receipt.transaction_id AND
                                 current.updated_at = receipt.committed_at
                       )
             )
        BEGIN SELECT RAISE(ABORT, 'production_worker_current_closure_invalid'); END
        """,
        """
        CREATE TRIGGER outbox_allocation_head_initial_guard
        BEFORE INSERT ON outbox_allocation_head
        WHEN NEW.sequence != 0 OR NEW.tail_sha256 != zeroblob(32)
        BEGIN SELECT RAISE(ABORT, 'production_head_sequence_invalid'); END
        """,
        """
        CREATE TRIGGER outbox_allocation_head_sequence_guard
        BEFORE UPDATE ON outbox_allocation_head
        WHEN NEW.sequence <= OLD.sequence OR NOT EXISTS (
            SELECT 1 FROM outbox_allocations
            WHERE sequence = NEW.sequence AND
                  allocation_tail_sha256 = NEW.tail_sha256
        )
        BEGIN SELECT RAISE(ABORT, 'production_head_sequence_invalid'); END
        """,
        """
        CREATE TRIGGER outbox_dispatch_head_initial_guard
        BEFORE INSERT ON outbox_dispatch_head
        WHEN NEW.published_sequence != 0 OR NEW.inflight_sequence IS NOT NULL OR
             NEW.head_sha256 != zeroblob(32)
        BEGIN SELECT RAISE(ABORT, 'production_outbox_dispatch_invalid'); END
        """,
        """
        CREATE TRIGGER outbox_dispatch_head_progress_guard
        BEFORE UPDATE ON outbox_dispatch_head
        WHEN NEW.updated_at < OLD.updated_at OR NOT (
                 (
                     OLD.inflight_sequence IS NULL AND
                     NEW.published_sequence = OLD.published_sequence AND
                     NEW.published_repository_revision IS
                         OLD.published_repository_revision AND
                     NEW.published_ordinal IS OLD.published_ordinal AND
                     NEW.inflight_sequence = OLD.published_sequence + 1
                 ) OR
                 (
                     OLD.inflight_sequence IS NOT NULL AND
                     NEW.published_sequence = OLD.inflight_sequence AND
                     NEW.published_repository_revision =
                         OLD.inflight_repository_revision AND
                     NEW.published_ordinal = OLD.inflight_ordinal AND
                     NEW.inflight_sequence IS NULL AND
                     NEW.inflight_repository_revision IS NULL AND
                     NEW.inflight_ordinal IS NULL
                 )
             ) OR (
                 OLD.inflight_sequence IS NULL AND NOT EXISTS (
                     SELECT 1
                     FROM outbox_allocations AS allocation
                     JOIN event_outbox AS event
                       ON event.event_id = allocation.event_id AND
                          event.commit_id = allocation.commit_id AND
                          event.repository_revision =
                              allocation.repository_revision AND
                          event.ordinal = allocation.ordinal
                     JOIN worker_targets AS target
                       ON target.target_id = allocation.event_id AND
                          target.target_kind = 'outbox' AND
                          target.origin_commit_id = allocation.commit_id AND
                          target.source_ordinal = allocation.ordinal AND
                          target.outbox_sequence = allocation.sequence AND
                          target.outbox_repository_revision =
                              allocation.repository_revision AND
                          target.outbox_ordinal = allocation.ordinal
                     JOIN worker_target_current AS current
                       ON current.target_id = target.target_id AND
                          current.target_kind = target.target_kind
                     JOIN worker_execution_receipts AS receipt
                       ON receipt.transaction_id =
                              current.last_receipt_transaction_id AND
                          receipt.target_id = current.target_id AND
                          receipt.target_kind = current.target_kind
                     JOIN worker_attempts AS attempt
                       ON attempt.receipt_transaction_id =
                              receipt.transaction_id AND
                          attempt.attempt_id = current.current_attempt_id AND
                          attempt.target_id = current.target_id AND
                          attempt.attempt_number =
                              current.current_attempt_number AND
                          attempt.owner_id = current.lease_owner AND
                          attempt.execution_fence = current.lease_fence
                     WHERE allocation.sequence = NEW.inflight_sequence AND
                           allocation.repository_revision =
                               NEW.inflight_repository_revision AND
                           allocation.ordinal = NEW.inflight_ordinal AND
                           target.genesis_status = 'pending' AND
                           event.status = 'leased' AND
                           current.status = event.status AND
                           current.state_revision = event.state_revision AND
                           current.attempt_count = 1 AND
                           current.attempt_count = event.attempt_count AND
                           current.current_attempt_number = 1 AND
                           current.fence_high_water = current.lease_fence AND
                           current.lease_owner = event.lease_owner AND
                           current.lease_fence = event.lease_fence AND
                           current.lease_expires_at = event.lease_expires_at AND
                           receipt.operation = 'claim' AND
                           receipt.before_status = 'pending' AND
                           receipt.after_status = current.status AND
                           receipt.after_state_revision =
                               current.state_revision AND
                           receipt.before_state_revision = 1 AND
                           receipt.after_state_sha256 = current.state_sha256 AND
                           receipt.committed_at = current.updated_at AND
                           receipt.committed_at = event.updated_at AND
                           receipt.committed_at = attempt.started_at AND
                           receipt.committed_at = NEW.updated_at AND
                           NEW.head_sha256 = receipt.after_state_sha256
                 )
             ) OR (
                 OLD.inflight_sequence IS NOT NULL AND NOT EXISTS (
                     SELECT 1
                     FROM outbox_allocations AS allocation
                     JOIN event_outbox AS event
                       ON event.event_id = allocation.event_id AND
                          event.commit_id = allocation.commit_id AND
                          event.repository_revision =
                              allocation.repository_revision AND
                          event.ordinal = allocation.ordinal
                     JOIN worker_targets AS target
                       ON target.target_id = allocation.event_id AND
                          target.target_kind = 'outbox' AND
                          target.origin_commit_id = allocation.commit_id AND
                          target.source_ordinal = allocation.ordinal AND
                          target.outbox_sequence = allocation.sequence AND
                          target.outbox_repository_revision =
                              allocation.repository_revision AND
                          target.outbox_ordinal = allocation.ordinal
                     JOIN worker_target_current AS current
                       ON current.target_id = target.target_id AND
                          current.target_kind = target.target_kind
                     JOIN worker_execution_receipts AS receipt
                       ON receipt.transaction_id =
                              current.last_receipt_transaction_id AND
                          receipt.target_id = current.target_id AND
                          receipt.target_kind = current.target_kind
                     JOIN worker_results AS result
                       ON result.receipt_transaction_id =
                              receipt.transaction_id AND
                          result.target_id = current.target_id AND
                          result.terminal_status = current.status AND
                          result.attempt_id = current.last_attempt_id
                     JOIN worker_attempts AS attempt
                       ON attempt.attempt_id = current.last_attempt_id AND
                          attempt.target_id = current.target_id AND
                          attempt.attempt_number = current.last_attempt_number
                     JOIN worker_effects AS effect
                       ON effect.effect_id = result.effect_id AND
                          effect.target_id = current.target_id AND
                          effect.attempt_id = attempt.attempt_id AND
                          effect.effect_kind = 'outbox_publish'
                     JOIN worker_execution_receipts AS effect_receipt
                       ON effect_receipt.transaction_id =
                              effect.authorization_transaction_id AND
                          effect_receipt.target_id = current.target_id AND
                          effect_receipt.target_kind = current.target_kind AND
                          effect_receipt.operation = 'authorize_effect'
                     WHERE allocation.sequence = OLD.inflight_sequence AND
                           allocation.repository_revision =
                               OLD.inflight_repository_revision AND
                           allocation.ordinal = OLD.inflight_ordinal AND
                           event.status IN ('published', 'dead') AND
                           current.status = event.status AND
                           current.state_revision = event.state_revision AND
                           current.attempt_count = event.attempt_count AND
                           current.current_attempt_id IS NULL AND
                           current.current_attempt_number IS NULL AND
                           current.current_effect_id IS NULL AND
                           current.unresolved_effect_id IS NULL AND
                           current.last_attempt_id IS NOT NULL AND
                           current.last_attempt_number =
                               current.attempt_count AND
                           (
                               (receipt.operation = 'complete' AND
                                   receipt.before_status = 'leased') OR
                               (receipt.operation = 'reconcile' AND
                                   receipt.before_status = 'outcome_unknown')
                           ) AND
                           receipt.after_status = current.status AND
                           receipt.after_state_revision =
                               current.state_revision AND
                           receipt.before_state_revision =
                               current.state_revision - 1 AND
                           receipt.after_state_sha256 = current.state_sha256 AND
                           receipt.committed_at = current.updated_at AND
                           receipt.committed_at = event.updated_at AND
                           receipt.committed_at = result.completed_at AND
                           receipt.committed_at = NEW.updated_at AND
                           NEW.head_sha256 = receipt.after_state_sha256 AND
                           effect_receipt.before_status = 'leased' AND
                           effect_receipt.after_status = 'leased' AND
                           effect_receipt.after_state_revision <=
                               receipt.before_state_revision AND
                           (
                               (current.status = 'published' AND
                                   event.published_at = receipt.committed_at AND
                                   event.dead_at IS NULL) OR
                               (current.status = 'dead' AND
                                   event.dead_at = receipt.committed_at AND
                                   event.published_at IS NULL)
                           ) AND
                           (
                               receipt.operation != 'reconcile' OR EXISTS (
                                   SELECT 1
                                   FROM worker_reconcile_proofs AS proof
                                   WHERE proof.receipt_transaction_id =
                                             receipt.transaction_id AND
                                         proof.target_id = current.target_id AND
                                         proof.effect_id = effect.effect_id AND
                                         (
                                             (current.status = 'published' AND
                                                 proof.proof_kind =
                                                     'provider_receipt') OR
                                             (current.status = 'dead' AND
                                                 proof.proof_kind IN (
                                                     'terminal_failure',
                                                     'proven_not_executed',
                                                     'provider_absence'
                                                 ))
                                         ) AND
                                         (
                                             proof.result_id IS NULL OR
                                             proof.result_id = result.result_id
                                   )
                               )
                           )
                 ) AND NOT EXISTS (
                     SELECT 1
                     FROM outbox_allocations AS allocation
                     JOIN event_outbox AS event
                       ON event.event_id = allocation.event_id AND
                          event.commit_id = allocation.commit_id AND
                          event.repository_revision =
                              allocation.repository_revision AND
                          event.ordinal = allocation.ordinal
                     JOIN worker_targets AS target
                       ON target.target_id = allocation.event_id AND
                          target.target_kind = 'outbox' AND
                          target.origin_commit_id = allocation.commit_id AND
                          target.source_ordinal = allocation.ordinal AND
                          target.outbox_sequence = allocation.sequence AND
                          target.outbox_repository_revision =
                              allocation.repository_revision AND
                          target.outbox_ordinal = allocation.ordinal
                     JOIN worker_target_current AS current
                       ON current.target_id = target.target_id AND
                          current.target_kind = target.target_kind
                     JOIN worker_execution_receipts AS receipt
                       ON receipt.transaction_id =
                              current.last_receipt_transaction_id AND
                          receipt.target_id = current.target_id AND
                          receipt.target_kind = current.target_kind
                     JOIN worker_attempts AS attempt
                       ON attempt.attempt_id = current.last_attempt_id AND
                          attempt.target_id = current.target_id AND
                          attempt.attempt_number = current.last_attempt_number
                     JOIN worker_execution_receipts AS claim_receipt
                       ON claim_receipt.transaction_id =
                              attempt.receipt_transaction_id AND
                          claim_receipt.target_id = current.target_id AND
                          claim_receipt.target_kind = current.target_kind
                     WHERE allocation.sequence = OLD.inflight_sequence AND
                           allocation.repository_revision =
                               OLD.inflight_repository_revision AND
                           allocation.ordinal = OLD.inflight_ordinal AND
                           event.status = 'dead' AND
                           event.state_revision = current.state_revision AND
                           event.attempt_count = current.attempt_count AND
                           event.lease_owner IS NULL AND
                           event.lease_fence IS NULL AND
                           event.lease_expires_at IS NULL AND
                           event.published_at IS NULL AND
                           event.outcome_unknown_at IS NULL AND
                           event.dead_at = receipt.committed_at AND
                           event.last_error_bytes IS NULL AND
                           event.last_error_sha256 IS NULL AND
                           current.status = 'dead' AND
                           current.attempt_count = target.max_attempts AND
                           current.current_attempt_id IS NULL AND
                           current.current_attempt_number IS NULL AND
                           current.lease_owner IS NULL AND
                           current.lease_fence IS NULL AND
                           current.lease_expires_at IS NULL AND
                           current.current_effect_id IS NULL AND
                           current.unresolved_effect_id IS NULL AND
                           current.next_attempt_at IS NULL AND
                           current.cancellation_pending = 0 AND
                           current.last_attempt_id = attempt.attempt_id AND
                           current.last_attempt_number =
                               current.attempt_count AND
                           attempt.attempt_number = target.max_attempts AND
                           claim_receipt.operation = 'claim' AND
                           claim_receipt.after_status = 'leased' AND
                           claim_receipt.after_state_revision <=
                               receipt.before_state_revision AND
                           claim_receipt.committed_at = attempt.started_at AND
                           receipt.operation = 'expire' AND
                           receipt.before_status = 'leased' AND
                           receipt.after_status = 'dead' AND
                           receipt.after_state_revision =
                               current.state_revision AND
                           receipt.before_state_revision =
                               current.state_revision - 1 AND
                           receipt.after_state_sha256 = current.state_sha256 AND
                           receipt.committed_at = current.updated_at AND
                           receipt.committed_at = event.updated_at AND
                           receipt.committed_at = NEW.updated_at AND
                           receipt.committed_at >= attempt.started_at AND
                           NEW.head_sha256 = receipt.after_state_sha256 AND
                           NOT EXISTS (
                               SELECT 1 FROM worker_effects AS any_effect
                               WHERE any_effect.target_id = current.target_id AND
                                     any_effect.attempt_id = attempt.attempt_id
                           ) AND NOT EXISTS (
                               SELECT 1 FROM worker_results AS any_result
                               WHERE any_result.receipt_transaction_id =
                                         receipt.transaction_id OR
                                     (any_result.target_id = current.target_id AND
                                      any_result.attempt_id = attempt.attempt_id)
                           )
                 )
             )
        BEGIN SELECT RAISE(ABORT, 'production_outbox_dispatch_invalid'); END
        """,
        """
        CREATE TRIGGER business_cutover_head_initial_guard
        BEFORE INSERT ON business_cutover_head
        WHEN NEW.phase_revision != 1 OR NEW.phase != 'installed' OR
             NEW.authority_state != 'read_only'
        BEGIN SELECT RAISE(ABORT, 'production_cutover_phase_invalid'); END
        """,
        """
        CREATE TRIGGER business_cutover_head_phase_guard
        BEFORE UPDATE ON business_cutover_head
        WHEN OLD.phase_revision = 5 OR
             NEW.phase_revision != OLD.phase_revision + 1 OR
             NEW.cutover_id != OLD.cutover_id OR
             NEW.repository_epoch != OLD.repository_epoch OR
             NEW.runtime_generation != OLD.runtime_generation OR
             NEW.code_digest != OLD.code_digest OR
             NEW.install_transaction_id != OLD.install_transaction_id OR
             NEW.install_manifest_sha256 != OLD.install_manifest_sha256 OR
             (OLD.coverage_transaction_id IS NOT NULL AND (
                 NEW.coverage_transaction_id IS NOT OLD.coverage_transaction_id OR
                 NEW.coverage_manifest_sha256 IS NOT OLD.coverage_manifest_sha256
             )) OR
             (OLD.audit_transaction_id IS NOT NULL AND (
                 NEW.audit_transaction_id IS NOT OLD.audit_transaction_id OR
                 NEW.audit_manifest_sha256 IS NOT OLD.audit_manifest_sha256
             )) OR
             (OLD.activation_transaction_id IS NOT NULL AND (
                 NEW.activation_transaction_id IS NOT OLD.activation_transaction_id OR
                 NEW.activation_manifest_sha256 IS NOT OLD.activation_manifest_sha256
             )) OR
             (OLD.mount_transaction_id IS NOT NULL AND (
                 NEW.mount_transaction_id IS NOT OLD.mount_transaction_id OR
                 NEW.mount_manifest_sha256 IS NOT OLD.mount_manifest_sha256
             ))
        BEGIN SELECT RAISE(ABORT, 'production_cutover_phase_invalid'); END
        """,
        """
        CREATE TRIGGER repository_runtime_control_v8_activation_insert_guard
        BEFORE INSERT ON repository_runtime_control
        WHEN NEW.authority_state = 'active' AND NOT EXISTS (
            SELECT 1 FROM business_cutover_head
            WHERE singleton = 1 AND phase_revision >= 4 AND
                  authority_state = 'active' AND cutover_id = NEW.cutover_id AND
                  runtime_generation = NEW.runtime_generation AND
                  code_digest = NEW.code_digest
        )
        BEGIN SELECT RAISE(ABORT, 'production_activation_authorization_required'); END
        """,
        """
        CREATE TRIGGER repository_runtime_control_v8_activation_update_guard
        BEFORE UPDATE OF authority_state, cutover_id, runtime_generation, code_digest
        ON repository_runtime_control
        WHEN NEW.authority_state = 'active' AND NOT EXISTS (
            SELECT 1 FROM business_cutover_head
            WHERE singleton = 1 AND phase_revision >= 4 AND
                  authority_state = 'active' AND cutover_id = NEW.cutover_id AND
                  runtime_generation = NEW.runtime_generation AND
                  code_digest = NEW.code_digest
        )
        BEGIN SELECT RAISE(ABORT, 'production_activation_authorization_required'); END
        """,
        """
        CREATE TRIGGER repository_runtime_control_v8_atomic_finalization_guard
        BEFORE UPDATE OF repository_revision ON repository_runtime_control
        WHEN NEW.repository_revision != OLD.repository_revision AND NOT (
            EXISTS (
                SELECT 1 FROM business_cutover_head
                WHERE singleton = 1 AND phase_revision = 5 AND phase = 'mounted' AND
                      authority_state = 'active' AND cutover_id = NEW.cutover_id AND
                      repository_epoch = NEW.repository_epoch AND
                      runtime_generation = NEW.runtime_generation AND
                      code_digest = NEW.code_digest AND
                      mount_transaction_id IS NOT NULL
            ) AND
            EXISTS (
                SELECT 1
                FROM business_commits AS business_commit
                JOIN atomic_business_write_receipts AS atomic_receipt
                  ON atomic_receipt.commit_id = business_commit.commit_id
                 AND atomic_receipt.atomic_manifest_sha256 =
                        business_commit.manifest_sha256
                 AND atomic_receipt.committed_at = business_commit.committed_at
                 AND atomic_receipt.control_count =
                        business_commit.pipeline_intent_count
                 AND atomic_receipt.target_count =
                        business_commit.durable_action_count +
                        business_commit.outbox_event_count + (
                            SELECT count(*)
                            FROM pipeline_control_intents AS creation_intent
                            WHERE creation_intent.origin_commit_id =
                                    business_commit.commit_id AND
                                  creation_intent.operation = 'create'
                        )
                JOIN worker_ledger_head AS worker_head
                  ON worker_head.singleton = 1
                 AND worker_head.sequence = atomic_receipt.after_worker_sequence
                 AND worker_head.tail_sha256 =
                        atomic_receipt.after_worker_tail_sha256
                WHERE business_commit.repository_revision = NEW.repository_revision
                  AND business_commit.repository_epoch = NEW.repository_epoch
                  AND business_commit.cutover_id = NEW.cutover_id
                  AND (
                    SELECT count(*)
                    FROM atomic_business_control_members AS control_member
                    JOIN pipeline_control_intents AS member_intent
                      ON member_intent.intent_id = control_member.intent_id
                    WHERE control_member.commit_id = business_commit.commit_id AND
                          member_intent.origin_commit_id = business_commit.commit_id
                  ) = atomic_receipt.control_count
                  AND (
                    SELECT count(*) FROM pipeline_control_intents
                    WHERE origin_commit_id = business_commit.commit_id
                  ) = business_commit.pipeline_intent_count
                  AND (
                    SELECT count(*)
                    FROM pipeline_control_receipts AS control_receipt
                    JOIN pipeline_control_intents AS control_intent
                      ON control_intent.intent_id = control_receipt.intent_id
                    WHERE control_intent.origin_commit_id = business_commit.commit_id
                  ) = business_commit.pipeline_intent_count
                  AND (
                    SELECT count(*)
                    FROM pipeline_control_intents AS control_intent
                    JOIN pipeline_control_receipts AS control_receipt
                      ON control_receipt.intent_id = control_intent.intent_id
                    JOIN worker_target_current AS current
                      ON current.target_id = control_intent.target_id AND
                         current.target_kind = 'pipeline' AND
                         current.status = control_receipt.after_status AND
                         current.state_revision =
                            control_receipt.after_state_revision AND
                         current.state_sha256 =
                            control_receipt.after_state_sha256 AND
                         current.last_receipt_transaction_id =
                            control_receipt.transaction_id AND
                         current.updated_at = control_receipt.committed_at
                    WHERE control_intent.origin_commit_id =
                            business_commit.commit_id AND
                          control_intent.operation = 'apply_control_intent'
                  ) = (
                    SELECT count(*)
                    FROM pipeline_control_intents
                    WHERE origin_commit_id = business_commit.commit_id AND
                          operation = 'apply_control_intent'
                  )
                  AND (
                    SELECT count(*) FROM outbox_allocations
                    WHERE commit_id = business_commit.commit_id
                  ) = business_commit.outbox_event_count
                  AND (
                    business_commit.outbox_event_count = 0 OR EXISTS (
                        SELECT 1
                        FROM outbox_allocation_head AS allocation_head
                        JOIN outbox_allocations AS newest_allocation
                          ON newest_allocation.sequence = allocation_head.sequence
                         AND newest_allocation.allocation_tail_sha256 =
                                allocation_head.tail_sha256
                        WHERE allocation_head.singleton = 1 AND
                              newest_allocation.commit_id =
                                  business_commit.commit_id AND
                              allocation_head.sequence = (
                                  SELECT max(sequence) FROM outbox_allocations
                              ) AND allocation_head.sequence = (
                                  SELECT max(sequence) FROM outbox_allocations
                                  WHERE commit_id = business_commit.commit_id
                              ) AND (
                                  SELECT min(sequence) FROM outbox_allocations
                                  WHERE commit_id = business_commit.commit_id
                              ) = allocation_head.sequence -
                                  business_commit.outbox_event_count + 1
                    )
                  )
                  AND (
                    SELECT count(*) FROM worker_targets
                    WHERE origin_commit_id = business_commit.commit_id AND
                          target_kind = 'outbox'
                  ) = business_commit.outbox_event_count
                  AND (
                    SELECT count(*) FROM worker_targets
                    WHERE origin_commit_id = business_commit.commit_id AND
                          target_kind = 'action'
                  ) = business_commit.durable_action_count
                  AND (
                    SELECT count(*) FROM worker_targets
                    WHERE origin_commit_id = business_commit.commit_id AND
                          target_kind = 'pipeline'
                  ) = (
                    SELECT count(*) FROM pipeline_control_intents
                    WHERE origin_commit_id = business_commit.commit_id AND
                          operation = 'create'
                  )
                  AND (
                    SELECT count(*) FROM worker_targets
                    WHERE origin_commit_id = business_commit.commit_id
                  ) = atomic_receipt.target_count
                  AND (
                    SELECT count(*)
                    FROM worker_targets AS target
                    JOIN worker_target_current AS current
                      ON current.target_id = target.target_id AND
                         current.target_kind = target.target_kind AND
                         current.status = target.genesis_status AND
                         current.state_revision = 1 AND
                         current.attempt_count = 0 AND
                         current.fence_high_water = 0 AND
                         current.previous_state_sha256 = zeroblob(32) AND
                         current.state_sha256 = target.genesis_state_sha256
                    WHERE target.origin_commit_id = business_commit.commit_id
                  ) = atomic_receipt.target_count
            )
        )
        BEGIN SELECT RAISE(ABORT, 'production_atomic_finalization_required'); END
        """,
        "DROP TABLE schema_v8_production_runtime_preflight",
    ),
)


# Schema-v9 closes the payload-reconstruction gap without rewriting any
# schema-v8 statement.  Its preflight is intentionally conservative: existing
# payload-bearing worker transitions cannot be reconstructed from the v8
# mirrors alone, so an upgrade is admitted only before any such receipt or row
# exists.  Claim/control history remains admissible when its ledger is fully
# headed and every reserved generic receipt has its specialised child.
EMPTY_WORKER_PROOF_PROJECTION_SHA256 = bytes.fromhex(
    "f14fd90bc57f6d54c068aff36091f91a"
    "2853b2b4ffa48c148a3a46d8854a3a47"
)

WORKER_PAYLOAD_PROJECTION_PREFLIGHT_PREDICATE = """
    NOT EXISTS (SELECT 1 FROM worker_effects) AND
    NOT EXISTS (SELECT 1 FROM worker_results) AND
    NOT EXISTS (SELECT 1 FROM worker_reconcile_proofs) AND
    NOT EXISTS (
        SELECT 1 FROM worker_execution_receipts
        WHERE operation != 'claim'
    ) AND
    NOT EXISTS (
        SELECT 1
        FROM transaction_receipts AS parent
        WHERE (
            substr(CAST(parent.transaction_id AS TEXT), 1, 18) =
                '_worker-execution:' OR
            CAST(parent.kind AS TEXT) = '_worker_execution_v3'
        ) AND NOT EXISTS (
            SELECT 1 FROM worker_execution_receipts AS child
            WHERE child.transaction_id = parent.transaction_id AND
                  child.receipt_kind = parent.kind AND
                  child.committed_at = parent.committed_at
        )
    ) AND
    NOT EXISTS (
        SELECT 1
        FROM transaction_receipts AS parent
        WHERE (
            substr(CAST(parent.transaction_id AS TEXT), 1, 27) =
                '_pipeline-business-control:' OR
            CAST(parent.kind AS TEXT) = '_pipeline_business_control_v2'
        ) AND NOT EXISTS (
            SELECT 1 FROM pipeline_control_receipts AS child
            WHERE child.transaction_id = parent.transaction_id AND
                  child.receipt_kind = parent.kind AND
                  child.committed_at = parent.committed_at
        )
    ) AND
    NOT EXISTS (
        SELECT 1
        FROM worker_execution_receipts AS child
        WHERE NOT EXISTS (
            SELECT 1 FROM transaction_receipts AS parent
            WHERE parent.transaction_id = child.transaction_id AND
                  parent.kind = child.receipt_kind AND
                  parent.committed_at = child.committed_at
        ) OR NOT EXISTS (
            SELECT 1 FROM worker_ledger_entries AS entry
            WHERE entry.sequence = child.sequence AND
                  entry.entry_kind = 'worker_execution' AND
                  entry.transaction_id = child.transaction_id AND
                  entry.previous_tail_sha256 =
                      child.previous_ledger_tail_sha256 AND
                  entry.ledger_tail_sha256 = child.ledger_tail_sha256 AND
                  entry.request_sha256 = child.request_sha256 AND
                  entry.after_state_sha256 = child.after_state_sha256 AND
                  entry.receipt_sha256 = child.receipt_sha256 AND
                  entry.occurred_at = child.committed_at
        )
    ) AND
    NOT EXISTS (
        SELECT 1
        FROM pipeline_control_receipts AS child
        WHERE NOT EXISTS (
            SELECT 1 FROM transaction_receipts AS parent
            WHERE parent.transaction_id = child.transaction_id AND
                  parent.kind = child.receipt_kind AND
                  parent.committed_at = child.committed_at
        ) OR NOT EXISTS (
            SELECT 1 FROM worker_ledger_entries AS entry
            WHERE entry.sequence = child.sequence AND
                  entry.entry_kind = 'pipeline_control' AND
                  entry.transaction_id = child.transaction_id AND
                  entry.previous_tail_sha256 =
                      child.previous_ledger_tail_sha256 AND
                  entry.ledger_tail_sha256 = child.ledger_tail_sha256 AND
                  entry.request_sha256 = child.request_sha256 AND
                  entry.after_state_sha256 = child.after_state_sha256 AND
                  entry.receipt_sha256 = child.receipt_sha256 AND
                  entry.occurred_at = child.committed_at
        )
    ) AND
    NOT EXISTS (
        SELECT 1
        FROM worker_ledger_entries AS entry
        WHERE CAST(entry.entry_kind AS TEXT) NOT IN (
            'worker_execution', 'pipeline_control'
        ) OR (
            entry.sequence = 1 AND
            entry.previous_tail_sha256 != zeroblob(32)
        ) OR (
            entry.sequence > 1 AND NOT EXISTS (
                SELECT 1 FROM worker_ledger_entries AS previous
                WHERE previous.sequence = entry.sequence - 1 AND
                      previous.ledger_tail_sha256 =
                          entry.previous_tail_sha256
            )
        ) OR (
            entry.entry_kind = 'worker_execution' AND (
                SELECT count(*) FROM worker_execution_receipts AS receipt
                WHERE receipt.sequence = entry.sequence AND
                      receipt.transaction_id = entry.transaction_id AND
                      receipt.previous_ledger_tail_sha256 =
                          entry.previous_tail_sha256 AND
                      receipt.ledger_tail_sha256 = entry.ledger_tail_sha256 AND
                      receipt.request_sha256 = entry.request_sha256 AND
                      receipt.after_state_sha256 = entry.after_state_sha256 AND
                      receipt.receipt_sha256 = entry.receipt_sha256 AND
                      receipt.committed_at = entry.occurred_at
            ) != 1
        ) OR (
            entry.entry_kind = 'pipeline_control' AND (
                SELECT count(*) FROM pipeline_control_receipts AS receipt
                WHERE receipt.sequence = entry.sequence AND
                      receipt.transaction_id = entry.transaction_id AND
                      receipt.previous_ledger_tail_sha256 =
                          entry.previous_tail_sha256 AND
                      receipt.ledger_tail_sha256 = entry.ledger_tail_sha256 AND
                      receipt.request_sha256 = entry.request_sha256 AND
                      receipt.after_state_sha256 = entry.after_state_sha256 AND
                      receipt.receipt_sha256 = entry.receipt_sha256 AND
                      receipt.committed_at = entry.occurred_at
            ) != 1
        )
    ) AND
    (
        (
            NOT EXISTS (SELECT 1 FROM worker_ledger_entries) AND
            NOT EXISTS (
                SELECT 1 FROM worker_ledger_head
                WHERE state_revision != 1 OR sequence != 0 OR
                      previous_tail_sha256 != zeroblob(32) OR
                      tail_sha256 != zeroblob(32)
            )
        ) OR
        (
            EXISTS (SELECT 1 FROM worker_ledger_entries) AND
            EXISTS (
                SELECT 1
                FROM worker_ledger_head AS head
                JOIN worker_ledger_entries AS tail
                  ON tail.sequence = head.sequence AND
                     tail.ledger_tail_sha256 = head.tail_sha256
                WHERE head.singleton = 1 AND
                      head.state_revision BETWEEN 2 AND head.sequence + 1 AND
                      head.sequence = (
                          SELECT max(sequence) FROM worker_ledger_entries
                      ) AND
                      head.sequence = (
                          SELECT count(*) FROM worker_ledger_entries
                      ) AND (
                          head.previous_tail_sha256 = zeroblob(32) OR
                          EXISTS (
                              SELECT 1
                              FROM worker_ledger_entries AS previous_head
                              WHERE previous_head.sequence < head.sequence AND
                                    previous_head.ledger_tail_sha256 =
                                        head.previous_tail_sha256
                          )
                      )
            )
        )
    ) AND
    NOT EXISTS (
        SELECT 1 FROM worker_target_current
        WHERE current_effect_id IS NOT NULL OR
              unresolved_effect_id IS NOT NULL
    ) AND
    NOT EXISTS (
        SELECT 1
        FROM worker_execution_receipts AS receipt
        WHERE (
            SELECT count(*) FROM worker_attempts AS attempt
            WHERE attempt.receipt_transaction_id = receipt.transaction_id AND
                  attempt.target_id = receipt.target_id AND
                  attempt.started_at = receipt.committed_at
        ) != 1
    ) AND
    NOT EXISTS (
        SELECT 1
        FROM worker_attempts AS attempt
        WHERE NOT EXISTS (
            SELECT 1 FROM worker_execution_receipts AS receipt
            WHERE receipt.transaction_id = attempt.receipt_transaction_id AND
                  receipt.operation = 'claim' AND
                  receipt.target_id = attempt.target_id AND
                  receipt.committed_at = attempt.started_at
        )
    ) AND
    NOT EXISTS (
        SELECT 1
        FROM worker_execution_receipts AS receipt
        WHERE NOT EXISTS (
            SELECT 1 FROM worker_execution_receipts AS later
            WHERE later.target_id = receipt.target_id AND
                  later.sequence > receipt.sequence
            UNION ALL
            SELECT 1 FROM pipeline_control_receipts AS later
            WHERE later.target_id = receipt.target_id AND
                  later.sequence > receipt.sequence
        ) AND NOT EXISTS (
            SELECT 1 FROM worker_target_current AS current
            WHERE current.target_id = receipt.target_id AND
                  current.target_kind = receipt.target_kind AND
                  current.status = receipt.after_status AND
                  current.state_revision = receipt.after_state_revision AND
                  current.state_sha256 = receipt.after_state_sha256 AND
                  current.last_receipt_transaction_id =
                      receipt.transaction_id AND
                  current.updated_at = receipt.committed_at
        )
    ) AND
    NOT EXISTS (
        SELECT 1
        FROM pipeline_control_receipts AS receipt
        WHERE NOT EXISTS (
            SELECT 1 FROM worker_execution_receipts AS later
            WHERE later.target_id = receipt.target_id AND
                  later.sequence > receipt.sequence
            UNION ALL
            SELECT 1 FROM pipeline_control_receipts AS later
            WHERE later.target_id = receipt.target_id AND
                  later.sequence > receipt.sequence
        ) AND NOT EXISTS (
            SELECT 1 FROM worker_target_current AS current
            WHERE current.target_id = receipt.target_id AND
                  current.target_kind = 'pipeline' AND
                  current.status = receipt.after_status AND
                  current.state_revision = receipt.after_state_revision AND
                  current.state_sha256 = receipt.after_state_sha256 AND
                  current.last_receipt_transaction_id =
                      receipt.transaction_id AND
                  current.updated_at = receipt.committed_at
        )
    )
"""


WORKER_PAYLOAD_PROJECTION_MIGRATION = Migration(
    version=9,
    name="worker_payload_projection_sidecars_v1",
    statements=(
        """
        CREATE TABLE schema_v9_worker_projection_preflight (
            verified INTEGER NOT NULL
                CONSTRAINT schema_v9_worker_projection_precondition
                CHECK(verified = 1)
        ) STRICT
        """,
        "INSERT INTO schema_v9_worker_projection_preflight(verified) "
        "SELECT CASE WHEN " + WORKER_PAYLOAD_PROJECTION_PREFLIGHT_PREDICATE +
        " THEN 1 ELSE 0 END",
        f"""
        CREATE TABLE worker_effect_request_projections (
            effect_id TEXT NOT NULL PRIMARY KEY CHECK(
                length(effect_id) = 32 AND
                length(CAST(effect_id AS BLOB)) = 32 AND
                effect_id NOT GLOB '*[^0-9a-f]*'
            ),
            target_id TEXT NOT NULL CHECK(
                length(target_id) = 32 AND
                length(CAST(target_id AS BLOB)) = 32 AND
                target_id NOT GLOB '*[^0-9a-f]*'
            ),
            target_kind TEXT NOT NULL CHECK(
                target_kind IN ('action', 'pipeline', 'outbox')
            ),
            attempt_id TEXT NOT NULL CHECK(
                length(attempt_id) = 32 AND
                length(CAST(attempt_id AS BLOB)) = 32 AND
                attempt_id NOT GLOB '*[^0-9a-f]*'
            ),
            attempt_number INTEGER NOT NULL CHECK(
                attempt_number BETWEEN 1 AND 100
            ),
            effect_kind TEXT NOT NULL CHECK(
                effect_kind IN (
                    'pipeline_document_commit', 'action_provider',
                    'outbox_publish'
                )
            ),
            authorization_transaction_id TEXT NOT NULL UNIQUE,
            protocol TEXT NOT NULL CHECK(
                protocol = 'ze.worker-result-projection.v1'
            ),
            projection_schema TEXT NOT NULL CHECK(
                projection_schema =
                    'ze.worker-effect-request-projection.v1'
            ),
            idempotency_scope TEXT NOT NULL CHECK(
                length(CAST(idempotency_scope AS BLOB)) BETWEEN 1 AND
                    {MAX_PIPELINE_WORKER_SCOPE_BYTES}
            ),
            idempotency_key TEXT NOT NULL CHECK(
                length(CAST(idempotency_key AS BLOB)) BETWEEN 1 AND
                    {MAX_BUSINESS_ID_BYTES}
            ),
            request_schema TEXT NOT NULL CHECK(
                length(CAST(request_schema AS BLOB)) BETWEEN 1 AND 1024
            ),
            request_codec_id TEXT NOT NULL CHECK(
                request_codec_id = 'ze.deterministic-json'
            ),
            request_codec_version INTEGER NOT NULL CHECK(
                request_codec_version = 1
            ),
            request_payload_bytes BLOB NOT NULL CHECK(
                typeof(request_payload_bytes) = 'blob' AND
                length(request_payload_bytes) BETWEEN 1 AND
                    {MAX_BUSINESS_SMALL_PAYLOAD_BYTES}
            ),
            request_payload_sha256 BLOB NOT NULL CHECK(
                typeof(request_payload_sha256) = 'blob' AND
                length(request_payload_sha256) = 32
            ),
            projection_manifest_bytes BLOB NOT NULL CHECK(
                typeof(projection_manifest_bytes) = 'blob' AND
                length(projection_manifest_bytes) BETWEEN 1 AND 4194304
            ),
            projection_sha256 BLOB NOT NULL UNIQUE CHECK(
                typeof(projection_sha256) = 'blob' AND
                length(projection_sha256) = 32
            ),
            closure_manifest_bytes BLOB NOT NULL CHECK(
                typeof(closure_manifest_bytes) = 'blob' AND
                length(closure_manifest_bytes) BETWEEN 1 AND 4194304
            ),
            closure_manifest_sha256 BLOB NOT NULL CHECK(
                typeof(closure_manifest_sha256) = 'blob' AND
                length(closure_manifest_sha256) = 32
            ),
            authorized_at TEXT NOT NULL CHECK(
                length(CAST(authorized_at AS BLOB)) = 24 AND
                strftime('%Y-%m-%dT%H:%M:%fZ', authorized_at) = authorized_at
            ),
            UNIQUE(effect_id, target_id, attempt_id),
            FOREIGN KEY(target_id, target_kind)
                REFERENCES worker_targets(target_id, target_kind)
                ON UPDATE RESTRICT ON DELETE RESTRICT,
            FOREIGN KEY(attempt_id, target_id, attempt_number)
                REFERENCES worker_attempts(
                    attempt_id, target_id, attempt_number
                ) ON UPDATE RESTRICT ON DELETE RESTRICT,
            FOREIGN KEY(effect_id, target_id, attempt_id)
                REFERENCES worker_effects(effect_id, target_id, attempt_id)
                ON UPDATE RESTRICT ON DELETE RESTRICT,
            FOREIGN KEY(authorization_transaction_id)
                REFERENCES worker_execution_receipts(transaction_id)
                ON UPDATE RESTRICT ON DELETE RESTRICT
        ) STRICT
        """,
        f"""
        CREATE TABLE worker_result_payload_projections (
            result_id TEXT NOT NULL PRIMARY KEY CHECK(
                length(result_id) = 32 AND
                length(CAST(result_id AS BLOB)) = 32 AND
                result_id NOT GLOB '*[^0-9a-f]*'
            ),
            target_id TEXT NOT NULL CHECK(
                length(target_id) = 32 AND
                length(CAST(target_id AS BLOB)) = 32 AND
                target_id NOT GLOB '*[^0-9a-f]*'
            ),
            target_kind TEXT NOT NULL CHECK(
                target_kind IN ('action', 'pipeline', 'outbox')
            ),
            attempt_id TEXT NOT NULL CHECK(
                length(attempt_id) = 32 AND
                length(CAST(attempt_id AS BLOB)) = 32 AND
                attempt_id NOT GLOB '*[^0-9a-f]*'
            ),
            attempt_number INTEGER NOT NULL CHECK(
                attempt_number BETWEEN 1 AND 100
            ),
            effect_id TEXT CHECK(
                effect_id IS NULL OR (
                    length(effect_id) = 32 AND
                    length(CAST(effect_id AS BLOB)) = 32 AND
                    effect_id NOT GLOB '*[^0-9a-f]*'
                )
            ),
            receipt_transaction_id TEXT NOT NULL UNIQUE,
            operation TEXT NOT NULL CHECK(
                operation IN ('complete', 'reconcile')
            ),
            protocol TEXT NOT NULL CHECK(
                protocol = 'ze.worker-result-projection.v1'
            ),
            projection_schema TEXT NOT NULL CHECK(
                projection_schema =
                    'ze.worker-result-payload-projection.v1'
            ),
            after_status TEXT NOT NULL CHECK(
                length(CAST(after_status AS BLOB)) BETWEEN 1 AND 64
            ),
            committed_at TEXT NOT NULL CHECK(
                length(CAST(committed_at AS BLOB)) = 24 AND
                strftime('%Y-%m-%dT%H:%M:%fZ', committed_at) = committed_at
            ),
            result_schema TEXT,
            result_codec_id TEXT,
            result_codec_version INTEGER,
            result_payload_bytes BLOB,
            result_payload_sha256 BLOB,
            error_schema TEXT,
            error_codec_id TEXT,
            error_codec_version INTEGER,
            error_payload_bytes BLOB,
            error_payload_sha256 BLOB,
            projection_manifest_bytes BLOB NOT NULL CHECK(
                typeof(projection_manifest_bytes) = 'blob' AND
                length(projection_manifest_bytes) BETWEEN 1 AND 4194304
            ),
            result_sha256 BLOB NOT NULL UNIQUE CHECK(
                typeof(result_sha256) = 'blob' AND length(result_sha256) = 32
            ),
            proof_sha256 BLOB CHECK(
                proof_sha256 IS NULL OR (
                    typeof(proof_sha256) = 'blob' AND
                    length(proof_sha256) = 32 AND
                    proof_sha256 != X'{EMPTY_WORKER_PROOF_PROJECTION_SHA256.hex()}'
                )
            ),
            closure_manifest_bytes BLOB NOT NULL CHECK(
                typeof(closure_manifest_bytes) = 'blob' AND
                length(closure_manifest_bytes) BETWEEN 1 AND 4194304
            ),
            closure_manifest_sha256 BLOB NOT NULL CHECK(
                typeof(closure_manifest_sha256) = 'blob' AND
                length(closure_manifest_sha256) = 32
            ),
            CHECK(
                (result_schema IS NULL AND result_codec_id IS NULL AND
                    result_codec_version IS NULL AND
                    result_payload_bytes IS NULL AND
                    result_payload_sha256 IS NULL) OR
                (result_schema IS NOT NULL AND
                    result_codec_id IS NOT NULL AND
                    result_codec_version IS NOT NULL AND
                    result_payload_bytes IS NOT NULL AND
                    result_payload_sha256 IS NOT NULL AND
                    length(CAST(result_schema AS BLOB)) BETWEEN 1 AND 1024 AND
                    result_codec_id = 'ze.deterministic-json' AND
                    result_codec_version = 1 AND
                    typeof(result_payload_bytes) = 'blob' AND
                    length(result_payload_bytes) BETWEEN 1 AND
                        {MAX_BUSINESS_SMALL_PAYLOAD_BYTES} AND
                    typeof(result_payload_sha256) = 'blob' AND
                    length(result_payload_sha256) = 32)
            ),
            CHECK(
                (error_schema IS NULL AND error_codec_id IS NULL AND
                    error_codec_version IS NULL AND
                    error_payload_bytes IS NULL AND
                    error_payload_sha256 IS NULL) OR
                (error_schema IS NOT NULL AND
                    error_codec_id IS NOT NULL AND
                    error_codec_version IS NOT NULL AND
                    error_payload_bytes IS NOT NULL AND
                    error_payload_sha256 IS NOT NULL AND
                    length(CAST(error_schema AS BLOB)) BETWEEN 1 AND 1024 AND
                    error_codec_id = 'ze.deterministic-json' AND
                    error_codec_version = 1 AND
                    typeof(error_payload_bytes) = 'blob' AND
                    length(error_payload_bytes) BETWEEN 1 AND
                        {MAX_BUSINESS_SMALL_PAYLOAD_BYTES} AND
                    typeof(error_payload_sha256) = 'blob' AND
                    length(error_payload_sha256) = 32)
            ),
            CHECK(result_payload_bytes IS NULL OR error_payload_bytes IS NULL),
            CHECK(
                operation != 'reconcile' OR (
                    effect_id IS NOT NULL AND proof_sha256 IS NOT NULL AND
                    result_payload_bytes IS NULL AND
                    error_payload_bytes IS NULL
                )
            ),
            UNIQUE(result_id, target_id),
            UNIQUE(
                result_id, target_id, attempt_id,
                receipt_transaction_id, proof_sha256
            ),
            FOREIGN KEY(target_id, target_kind)
                REFERENCES worker_targets(target_id, target_kind)
                ON UPDATE RESTRICT ON DELETE RESTRICT,
            FOREIGN KEY(result_id, target_id)
                REFERENCES worker_results(result_id, target_id)
                ON UPDATE RESTRICT ON DELETE RESTRICT,
            FOREIGN KEY(attempt_id, target_id, attempt_number)
                REFERENCES worker_attempts(
                    attempt_id, target_id, attempt_number
                ) ON UPDATE RESTRICT ON DELETE RESTRICT,
            FOREIGN KEY(effect_id, target_id, attempt_id)
                REFERENCES worker_effects(effect_id, target_id, attempt_id)
                ON UPDATE RESTRICT ON DELETE RESTRICT,
            FOREIGN KEY(
                receipt_transaction_id, target_id, after_status, committed_at
            ) REFERENCES worker_execution_receipts(
                transaction_id, target_id, after_status, committed_at
            ) ON UPDATE RESTRICT ON DELETE RESTRICT
        ) STRICT
        """,
        f"""
        CREATE TABLE worker_proof_payload_projections (
            proof_id TEXT NOT NULL PRIMARY KEY CHECK(
                length(proof_id) = 32 AND
                length(CAST(proof_id AS BLOB)) = 32 AND
                proof_id NOT GLOB '*[^0-9a-f]*'
            ),
            result_id TEXT NOT NULL CHECK(
                length(result_id) = 32 AND
                length(CAST(result_id AS BLOB)) = 32 AND
                result_id NOT GLOB '*[^0-9a-f]*'
            ),
            target_id TEXT NOT NULL CHECK(
                length(target_id) = 32 AND
                length(CAST(target_id AS BLOB)) = 32 AND
                target_id NOT GLOB '*[^0-9a-f]*'
            ),
            target_kind TEXT NOT NULL CHECK(
                target_kind IN ('action', 'pipeline', 'outbox')
            ),
            attempt_id TEXT NOT NULL CHECK(
                length(attempt_id) = 32 AND
                length(CAST(attempt_id AS BLOB)) = 32 AND
                attempt_id NOT GLOB '*[^0-9a-f]*'
            ),
            attempt_number INTEGER NOT NULL CHECK(
                attempt_number BETWEEN 1 AND 100
            ),
            effect_id TEXT CHECK(
                effect_id IS NULL OR (
                    length(effect_id) = 32 AND
                    length(CAST(effect_id AS BLOB)) = 32 AND
                    effect_id NOT GLOB '*[^0-9a-f]*'
                )
            ),
            receipt_transaction_id TEXT NOT NULL UNIQUE,
            operation TEXT NOT NULL CHECK(
                operation IN ('complete', 'reconcile')
            ),
            protocol TEXT NOT NULL CHECK(
                protocol = 'ze.worker-result-projection.v1'
            ),
            projection_schema TEXT NOT NULL CHECK(
                projection_schema =
                    'ze.worker-proof-payload-projection.v1'
            ),
            proof_class TEXT NOT NULL CHECK(
                proof_class IN ('complete', 'reconcile')
            ),
            proof_kind TEXT NOT NULL CHECK(
                proof_kind IN (
                    'result_commit', 'provider_receipt', 'durable_result',
                    'proven_not_executed', 'provider_absence',
                    'terminal_failure', 'still_unknown', 'cancelled'
                )
            ),
            observed_at TEXT NOT NULL CHECK(
                length(CAST(observed_at AS BLOB)) = 24 AND
                strftime('%Y-%m-%dT%H:%M:%fZ', observed_at) = observed_at
            ),
            evidence_schema TEXT,
            evidence_codec_id TEXT,
            evidence_codec_version INTEGER,
            evidence_payload_bytes BLOB,
            evidence_payload_sha256 BLOB,
            provider_receipt_schema TEXT,
            provider_receipt_codec_id TEXT,
            provider_receipt_codec_version INTEGER,
            provider_receipt_payload_bytes BLOB,
            provider_receipt_payload_sha256 BLOB,
            durable_result_id TEXT,
            durable_result_schema TEXT,
            durable_result_codec_id TEXT,
            durable_result_codec_version INTEGER,
            durable_result_payload_bytes BLOB,
            durable_result_payload_sha256 BLOB,
            durable_result_recorded_at TEXT,
            result_commit_id TEXT,
            projection_manifest_bytes BLOB NOT NULL CHECK(
                typeof(projection_manifest_bytes) = 'blob' AND
                length(projection_manifest_bytes) BETWEEN 1 AND 4194304
            ),
            proof_sha256 BLOB NOT NULL UNIQUE CHECK(
                typeof(proof_sha256) = 'blob' AND
                length(proof_sha256) = 32 AND
                proof_sha256 != X'{EMPTY_WORKER_PROOF_PROJECTION_SHA256.hex()}'
            ),
            closure_manifest_bytes BLOB NOT NULL CHECK(
                typeof(closure_manifest_bytes) = 'blob' AND
                length(closure_manifest_bytes) BETWEEN 1 AND 4194304
            ),
            closure_manifest_sha256 BLOB NOT NULL CHECK(
                typeof(closure_manifest_sha256) = 'blob' AND
                length(closure_manifest_sha256) = 32
            ),
            CHECK(
                (evidence_schema IS NULL AND evidence_codec_id IS NULL AND
                    evidence_codec_version IS NULL AND
                    evidence_payload_bytes IS NULL AND
                    evidence_payload_sha256 IS NULL) OR
                (evidence_schema IS NOT NULL AND
                    evidence_codec_id IS NOT NULL AND
                    evidence_codec_version IS NOT NULL AND
                    evidence_payload_bytes IS NOT NULL AND
                    evidence_payload_sha256 IS NOT NULL AND
                    length(CAST(evidence_schema AS BLOB)) BETWEEN 1 AND 1024 AND
                    evidence_codec_id = 'ze.deterministic-json' AND
                    evidence_codec_version = 1 AND
                    typeof(evidence_payload_bytes) = 'blob' AND
                    length(evidence_payload_bytes) BETWEEN 1 AND
                        {MAX_BUSINESS_SMALL_PAYLOAD_BYTES} AND
                    typeof(evidence_payload_sha256) = 'blob' AND
                    length(evidence_payload_sha256) = 32)
            ),
            CHECK(
                (provider_receipt_schema IS NULL AND
                    provider_receipt_codec_id IS NULL AND
                    provider_receipt_codec_version IS NULL AND
                    provider_receipt_payload_bytes IS NULL AND
                    provider_receipt_payload_sha256 IS NULL) OR
                (provider_receipt_schema IS NOT NULL AND
                    provider_receipt_codec_id IS NOT NULL AND
                    provider_receipt_codec_version IS NOT NULL AND
                    provider_receipt_payload_bytes IS NOT NULL AND
                    provider_receipt_payload_sha256 IS NOT NULL AND
                    length(CAST(provider_receipt_schema AS BLOB)) BETWEEN 1 AND
                    1024 AND
                    provider_receipt_codec_id = 'ze.deterministic-json' AND
                    provider_receipt_codec_version = 1 AND
                    typeof(provider_receipt_payload_bytes) = 'blob' AND
                    length(provider_receipt_payload_bytes) BETWEEN 1 AND
                        {MAX_BUSINESS_SMALL_PAYLOAD_BYTES} AND
                    typeof(provider_receipt_payload_sha256) = 'blob' AND
                    length(provider_receipt_payload_sha256) = 32)
            ),
            CHECK(
                (durable_result_id IS NULL AND durable_result_schema IS NULL AND
                    durable_result_codec_id IS NULL AND
                    durable_result_codec_version IS NULL AND
                    durable_result_payload_bytes IS NULL AND
                    durable_result_payload_sha256 IS NULL AND
                    durable_result_recorded_at IS NULL) OR
                (durable_result_id IS NOT NULL AND
                    durable_result_schema IS NOT NULL AND
                    durable_result_codec_id IS NOT NULL AND
                    durable_result_codec_version IS NOT NULL AND
                    durable_result_payload_bytes IS NOT NULL AND
                    durable_result_payload_sha256 IS NOT NULL AND
                    durable_result_recorded_at IS NOT NULL AND
                    length(durable_result_id) = 32 AND
                    length(CAST(durable_result_id AS BLOB)) = 32 AND
                    durable_result_id NOT GLOB '*[^0-9a-f]*' AND
                    durable_result_id = result_id AND
                    length(CAST(durable_result_schema AS BLOB)) BETWEEN 1 AND
                        1024 AND
                    durable_result_codec_id = 'ze.deterministic-json' AND
                    durable_result_codec_version = 1 AND
                    typeof(durable_result_payload_bytes) = 'blob' AND
                    length(durable_result_payload_bytes) BETWEEN 1 AND
                        {MAX_BUSINESS_SMALL_PAYLOAD_BYTES} AND
                    typeof(durable_result_payload_sha256) = 'blob' AND
                    length(durable_result_payload_sha256) = 32 AND
                    length(CAST(durable_result_recorded_at AS BLOB)) = 24 AND
                    strftime('%Y-%m-%dT%H:%M:%fZ',
                        durable_result_recorded_at) =
                        durable_result_recorded_at)
            ),
            CHECK(proof_kind != 'durable_result' OR
                durable_result_id IS NOT NULL),
            CHECK(proof_class != 'reconcile' OR
                evidence_payload_bytes IS NOT NULL),
            CHECK(
                (operation = 'complete' AND proof_class = 'complete' AND
                    proof_kind IN (
                        'result_commit', 'provider_receipt', 'durable_result',
                        'terminal_failure', 'cancelled',
                        'proven_not_executed'
                    )) OR
                (operation = 'reconcile' AND proof_class = 'reconcile' AND
                    proof_kind IN (
                        'result_commit', 'provider_receipt', 'durable_result',
                        'proven_not_executed', 'provider_absence',
                        'terminal_failure', 'still_unknown'
                    ))
            ),
            CHECK(proof_kind != 'result_commit' OR
                result_commit_id IS NOT NULL),
            CHECK(proof_kind != 'provider_receipt' OR
                provider_receipt_payload_bytes IS NOT NULL),
            CHECK(proof_kind NOT IN (
                'terminal_failure', 'cancelled', 'proven_not_executed'
            ) OR evidence_payload_bytes IS NOT NULL),
            CHECK(result_commit_id IS NULL OR (
                length(result_commit_id) = 32 AND
                length(CAST(result_commit_id AS BLOB)) = 32 AND
                result_commit_id NOT GLOB '*[^0-9a-f]*'
            )),
            UNIQUE(proof_id, result_id, target_id),
            FOREIGN KEY(
                result_id, target_id, attempt_id,
                receipt_transaction_id, proof_sha256
            ) REFERENCES worker_result_payload_projections(
                result_id, target_id, attempt_id,
                receipt_transaction_id, proof_sha256
            ) ON UPDATE RESTRICT ON DELETE RESTRICT,
            FOREIGN KEY(target_id, target_kind)
                REFERENCES worker_targets(target_id, target_kind)
                ON UPDATE RESTRICT ON DELETE RESTRICT,
            FOREIGN KEY(attempt_id, target_id, attempt_number)
                REFERENCES worker_attempts(
                    attempt_id, target_id, attempt_number
                ) ON UPDATE RESTRICT ON DELETE RESTRICT,
            FOREIGN KEY(effect_id, target_id, attempt_id)
                REFERENCES worker_effects(effect_id, target_id, attempt_id)
                ON UPDATE RESTRICT ON DELETE RESTRICT,
            FOREIGN KEY(receipt_transaction_id)
                REFERENCES worker_execution_receipts(transaction_id)
                ON UPDATE RESTRICT ON DELETE RESTRICT,
            FOREIGN KEY(result_commit_id)
                REFERENCES business_commits(commit_id)
                ON UPDATE RESTRICT ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE INDEX worker_effect_request_projections_target_attempt
        ON worker_effect_request_projections(target_id, attempt_id, effect_id)
        """,
        """
        CREATE INDEX worker_result_payload_projections_target_receipt
        ON worker_result_payload_projections(target_id, receipt_transaction_id)
        """,
        """
        CREATE INDEX worker_result_payload_projections_effect_attempt
        ON worker_result_payload_projections(effect_id, target_id, attempt_id)
        WHERE effect_id IS NOT NULL
        """,
        """
        CREATE INDEX worker_proof_payload_projections_result_receipt
        ON worker_proof_payload_projections(result_id, receipt_transaction_id)
        """,
        """
        CREATE INDEX worker_proof_payload_projections_effect_attempt
        ON worker_proof_payload_projections(effect_id, target_id, attempt_id)
        WHERE effect_id IS NOT NULL
        """,
        """
        CREATE TRIGGER worker_effects_v9_preadvance_guard
        BEFORE INSERT ON worker_effects
        WHEN EXISTS (
            SELECT 1
            FROM worker_execution_receipts AS receipt
            JOIN worker_ledger_head AS head ON head.singleton = 1
            WHERE receipt.transaction_id =
                      NEW.authorization_transaction_id AND
                  receipt.sequence <= head.sequence
        )
        BEGIN SELECT RAISE(ABORT,
            'worker_payload_projection_must_precede_head'); END
        """,
        """
        CREATE TRIGGER worker_results_v9_operation_preadvance_guard
        BEFORE INSERT ON worker_results
        WHEN NOT EXISTS (
            SELECT 1 FROM worker_execution_receipts
            WHERE transaction_id = NEW.receipt_transaction_id AND
                  operation IN ('complete', 'reconcile')
        ) OR EXISTS (
            SELECT 1
            FROM worker_execution_receipts AS receipt
            JOIN worker_ledger_head AS head ON head.singleton = 1
            WHERE receipt.transaction_id = NEW.receipt_transaction_id AND
                  receipt.sequence <= head.sequence
        )
        BEGIN SELECT RAISE(ABORT,
            'worker_payload_projection_result_invalid'); END
        """,
        """
        CREATE TRIGGER worker_reconcile_proofs_v9_preadvance_guard
        BEFORE INSERT ON worker_reconcile_proofs
        WHEN EXISTS (
            SELECT 1
            FROM worker_execution_receipts AS receipt
            JOIN worker_ledger_head AS head ON head.singleton = 1
            WHERE receipt.transaction_id = NEW.receipt_transaction_id AND
                  receipt.sequence <= head.sequence
        )
        BEGIN SELECT RAISE(ABORT,
            'worker_payload_projection_must_precede_head'); END
        """,
        """
        CREATE TRIGGER worker_effect_request_projection_preadvance_guard
        BEFORE INSERT ON worker_effect_request_projections
        WHEN EXISTS (
            SELECT 1
            FROM worker_execution_receipts AS receipt
            JOIN worker_ledger_head AS head ON head.singleton = 1
            WHERE receipt.transaction_id =
                      NEW.authorization_transaction_id AND
                  receipt.sequence <= head.sequence
        )
        BEGIN SELECT RAISE(ABORT,
            'worker_payload_projection_must_precede_head'); END
        """,
        """
        CREATE TRIGGER worker_result_payload_projection_preadvance_guard
        BEFORE INSERT ON worker_result_payload_projections
        WHEN EXISTS (
            SELECT 1
            FROM worker_execution_receipts AS receipt
            JOIN worker_ledger_head AS head ON head.singleton = 1
            WHERE receipt.transaction_id = NEW.receipt_transaction_id AND
                  receipt.sequence <= head.sequence
        )
        BEGIN SELECT RAISE(ABORT,
            'worker_payload_projection_must_precede_head'); END
        """,
        """
        CREATE TRIGGER worker_proof_payload_projection_preadvance_guard
        BEFORE INSERT ON worker_proof_payload_projections
        WHEN EXISTS (
            SELECT 1
            FROM worker_execution_receipts AS receipt
            JOIN worker_ledger_head AS head ON head.singleton = 1
            WHERE receipt.transaction_id = NEW.receipt_transaction_id AND
                  receipt.sequence <= head.sequence
        )
        BEGIN SELECT RAISE(ABORT,
            'worker_payload_projection_must_precede_head'); END
        """,
        """
        CREATE TRIGGER worker_effect_request_projections_binding_guard
        BEFORE INSERT ON worker_effect_request_projections
        WHEN NOT EXISTS (
            SELECT 1
            FROM worker_effects AS effect
            JOIN worker_execution_receipts AS receipt
              ON receipt.transaction_id = effect.authorization_transaction_id
            JOIN worker_targets AS target
              ON target.target_id = effect.target_id
            JOIN worker_attempts AS attempt
              ON attempt.attempt_id = effect.attempt_id AND
                 attempt.target_id = effect.target_id
            WHERE effect.effect_id = NEW.effect_id AND
                  effect.target_id = NEW.target_id AND
                  effect.attempt_id = NEW.attempt_id AND
                  effect.effect_kind = NEW.effect_kind AND
                  effect.authorization_transaction_id =
                      NEW.authorization_transaction_id AND
                  effect.idempotency_key = NEW.idempotency_key AND
                  effect.authorized_at = NEW.authorized_at AND
                  target.target_kind = NEW.target_kind AND
                  target.idempotency_scope = NEW.idempotency_scope AND
                  attempt.attempt_number = NEW.attempt_number AND
                  receipt.operation = 'authorize_effect' AND
                  receipt.target_kind = NEW.target_kind AND
                  receipt.target_id = NEW.target_id AND
                  receipt.committed_at = NEW.authorized_at
        )
        BEGIN SELECT RAISE(ABORT,
            'worker_effect_request_projection_binding_invalid'); END
        """,
        f"""
        CREATE TRIGGER worker_result_payload_projections_v8_mirror_guard
        BEFORE INSERT ON worker_result_payload_projections
        WHEN NOT EXISTS (
            SELECT 1
            FROM worker_results AS result
            JOIN worker_execution_receipts AS receipt
              ON receipt.transaction_id = result.receipt_transaction_id
            JOIN worker_attempts AS attempt
              ON attempt.attempt_id = NEW.attempt_id AND
                 attempt.target_id = NEW.target_id
            WHERE result.result_id = NEW.result_id AND
                  result.target_id = NEW.target_id AND
                  result.attempt_id = NEW.attempt_id AND
                  result.effect_id IS NEW.effect_id AND
                  result.terminal_status = NEW.after_status AND
                  result.receipt_transaction_id =
                      NEW.receipt_transaction_id AND
                  result.completed_at = NEW.committed_at AND
                  result.result_sha256 = NEW.result_sha256 AND
                  result.proof_sha256 = CASE
                      WHEN NEW.proof_sha256 IS NULL
                      THEN X'{EMPTY_WORKER_PROOF_PROJECTION_SHA256.hex()}'
                      ELSE NEW.proof_sha256
                  END AND
                  attempt.attempt_number = NEW.attempt_number AND
                  receipt.operation = NEW.operation AND
                  receipt.target_kind = NEW.target_kind AND
                  receipt.target_id = NEW.target_id AND
                  receipt.committed_at = NEW.committed_at
        )
        BEGIN SELECT RAISE(ABORT,
            'worker_result_projection_v8_mirror_invalid'); END
        """,
        """
        CREATE TRIGGER worker_proof_payload_projections_binding_guard
        BEFORE INSERT ON worker_proof_payload_projections
        WHEN NOT EXISTS (
            SELECT 1
            FROM worker_result_payload_projections AS result
            JOIN worker_execution_receipts AS receipt
              ON receipt.transaction_id = NEW.receipt_transaction_id
            WHERE result.result_id = NEW.result_id AND
                  result.target_id = NEW.target_id AND
                  result.target_kind = NEW.target_kind AND
                  result.attempt_id = NEW.attempt_id AND
                  result.attempt_number = NEW.attempt_number AND
                  result.effect_id IS NEW.effect_id AND
                  result.receipt_transaction_id =
                      NEW.receipt_transaction_id AND
                  result.operation = NEW.operation AND
                  result.committed_at = NEW.observed_at AND
                  result.proof_sha256 = NEW.proof_sha256 AND
                  result.closure_manifest_bytes =
                      NEW.closure_manifest_bytes AND
                  result.closure_manifest_sha256 =
                      NEW.closure_manifest_sha256 AND
                  receipt.operation = NEW.operation AND
                  receipt.target_kind = NEW.target_kind AND
                  receipt.target_id = NEW.target_id AND
                  receipt.committed_at = NEW.observed_at AND
                  receipt.operation = NEW.proof_class
        ) OR (
            NEW.effect_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM worker_effects
                WHERE effect_id = NEW.effect_id AND
                      target_id = NEW.target_id AND
                      attempt_id = NEW.attempt_id
            )
        ) OR (
            NEW.proof_class = 'reconcile' AND NOT EXISTS (
                SELECT 1 FROM worker_reconcile_proofs AS proof
                WHERE proof.proof_id = NEW.proof_id AND
                      proof.target_id = NEW.target_id AND
                      proof.effect_id = NEW.effect_id AND
                      proof.proof_kind = NEW.proof_kind AND
                      proof.result_id IS CASE
                          WHEN NEW.proof_kind = 'durable_result'
                          THEN NEW.result_id ELSE NULL END AND
                      proof.proof_bytes = NEW.projection_manifest_bytes AND
                      proof.proof_sha256 = NEW.proof_sha256 AND
                      proof.receipt_transaction_id =
                          NEW.receipt_transaction_id AND
                      proof.observed_at = NEW.observed_at
            )
        )
        BEGIN SELECT RAISE(ABORT,
            'worker_proof_projection_binding_invalid'); END
        """,
        *_production_append_only_triggers(
            "worker_effect_request_projections"
        ),
        *_production_append_only_triggers(
            "worker_result_payload_projections"
        ),
        *_production_append_only_triggers(
            "worker_proof_payload_projections"
        ),
        f"""
        CREATE TRIGGER worker_ledger_head_v9_projection_closure_guard
        BEFORE UPDATE ON worker_ledger_head
        WHEN EXISTS (
            SELECT 1
            FROM worker_execution_receipts AS receipt
            WHERE receipt.sequence > OLD.sequence AND
                  receipt.sequence <= NEW.sequence AND (
                      (receipt.operation = 'authorize_effect' AND NOT EXISTS (
                          SELECT 1
                          FROM worker_effect_request_projections AS effect
                          WHERE effect.authorization_transaction_id =
                                    receipt.transaction_id AND
                                effect.target_id = receipt.target_id AND
                                effect.authorized_at = receipt.committed_at
                      )) OR
                      (receipt.operation IN ('complete', 'reconcile') AND
                          NOT EXISTS (
                              SELECT 1
                              FROM worker_result_payload_projections AS result
                              WHERE result.receipt_transaction_id =
                                        receipt.transaction_id AND
                                    result.target_id = receipt.target_id AND
                                    result.after_status = receipt.after_status AND
                                    result.committed_at = receipt.committed_at
                          )) OR
                      (receipt.operation = 'reconcile' AND NOT EXISTS (
                          SELECT 1
                          FROM worker_proof_payload_projections AS proof
                          WHERE proof.receipt_transaction_id =
                                    receipt.transaction_id AND
                                proof.target_id = receipt.target_id AND
                                proof.proof_class = 'reconcile' AND
                                proof.observed_at = receipt.committed_at
                      )) OR
                      (receipt.operation = 'complete' AND EXISTS (
                          SELECT 1
                          FROM worker_result_payload_projections AS result
                          WHERE result.receipt_transaction_id =
                                    receipt.transaction_id AND (
                              (result.proof_sha256 IS NULL AND EXISTS (
                                  SELECT 1
                                  FROM worker_proof_payload_projections AS proof
                                  WHERE proof.receipt_transaction_id =
                                            receipt.transaction_id
                              )) OR
                              (result.proof_sha256 IS NOT NULL AND NOT EXISTS (
                                  SELECT 1
                                  FROM worker_proof_payload_projections AS proof
                                  WHERE proof.receipt_transaction_id =
                                            receipt.transaction_id AND
                                        proof.proof_class = 'complete' AND
                                        proof.proof_sha256 =
                                            result.proof_sha256
                              ))
                          )
                      ))
                  )
        )
        BEGIN SELECT RAISE(ABORT,
            'worker_payload_projection_closure_invalid'); END
        """,
        "DROP TABLE schema_v9_worker_projection_preflight",
    ),
)

# Schema-v10 hardens timestamp and dispatch tuple shapes without rewriting the
# already-issued v8/v9 DDL. SQLite accepts CHECK expressions that evaluate to
# NULL, so ``strftime(...) = value`` alone is not a closed validation rule.
_SCHEMA_V10_APPEND_ONLY_TIMESTAMPS = (
    ("business_cutover_install_receipts", ("committed_at",), ()),
    ("business_asset_stage_receipts", ("committed_at",), ()),
    ("business_asset_coverage_receipts", ("committed_at",), ()),
    ("repository_audit_certificates", ("committed_at",), ()),
    ("business_cutover_activation_receipts", ("committed_at",), ()),
    (
        "business_cutover_mount_receipts",
        ("committed_at", "install_committed_at", "coverage_committed_at"),
        (),
    ),
    ("atomic_business_write_receipts", ("committed_at",), ()),
    ("worker_ledger_entries", ("committed_at", "occurred_at"), ()),
    ("worker_execution_receipts", ("committed_at",), ()),
    ("pipeline_control_intents", ("committed_at",), ()),
    ("pipeline_control_receipts", ("committed_at",), ()),
    ("worker_targets", ("created_at",), ()),
    ("worker_attempts", ("started_at",), ()),
    ("worker_effects", ("authorized_at",), ()),
    ("worker_results", ("completed_at",), ()),
    ("worker_reconcile_proofs", ("observed_at",), ()),
    ("outbox_allocations", ("allocated_at",), ()),
    ("worker_effect_request_projections", ("authorized_at",), ()),
    ("worker_result_payload_projections", ("committed_at",), ()),
    (
        "worker_proof_payload_projections",
        ("observed_at",),
        ("durable_result_recorded_at",),
    ),
)

_SCHEMA_V10_MUTABLE_TIMESTAMPS = (
    ("repository_audit_head", ("updated_at",), ()),
    ("worker_ledger_head", ("updated_at",), ()),
    (
        "worker_target_current",
        ("updated_at",),
        ("next_attempt_at", "lease_expires_at"),
    ),
    ("outbox_allocation_head", ("updated_at",), ()),
    ("outbox_dispatch_head", ("updated_at",), ()),
    ("business_cutover_head", ("updated_at",), ()),
)


def _schema_v10_reserved_receipt(prefix: str) -> str:
    """Return the exact schema-v8-owned receipt namespace predicate."""

    if prefix not in ("NEW", "OLD", "row"):
        raise ValueError("schema-v10 receipt prefix is invalid")
    identity = "CAST({0}.transaction_id AS TEXT)".format(prefix)
    kind = "CAST({0}.kind AS TEXT)".format(prefix)
    prefixes = " OR ".join(
        "substr({0}, 1, {1}) = '{2}'".format(identity, len(value), value)
        for value in _PRODUCTION_RECEIPT_PREFIXES
    )
    kinds = ", ".join("'{0}'".format(value) for value in _PRODUCTION_RECEIPT_KINDS)
    return "(({0}) OR {1} IN ({2}))".format(prefixes, kind, kinds)


def _schema_v10_timestamp_invalid(
    prefix: str,
    required: Tuple[str, ...],
    nullable: Tuple[str, ...],
) -> str:
    columns = required + nullable
    if (
        not columns
        or len(set(columns)) != len(columns)
        or any(not value.replace("_", "").isalnum() for value in columns)
        or prefix not in ("NEW", "row")
    ):
        raise ValueError("schema-v10 timestamp specification is invalid")

    def malformed(column: str) -> str:
        value = prefix + "." + column
        return (
            "typeof({0}) != 'text' OR length(CAST({0} AS BLOB)) != 24 OR "
            "strftime('%Y-%m-%dT%H:%M:%fZ', {0}) IS NULL OR "
            "strftime('%Y-%m-%dT%H:%M:%fZ', {0}) != {0}"
        ).format(value)

    predicates = [
        "{0}.{1} IS NULL OR ({2})".format(prefix, column, malformed(column))
        for column in required
    ]
    predicates.extend(
        "{0}.{1} IS NOT NULL AND ({2})".format(
            prefix, column, malformed(column)
        )
        for column in nullable
    )
    return "(" + ") OR (".join(predicates) + ")"


def _schema_v10_timestamp_triggers(
    table: str,
    required: Tuple[str, ...],
    nullable: Tuple[str, ...],
    *,
    mutable: bool,
) -> Tuple[str, ...]:
    if not table.replace("_", "").isalnum():
        raise ValueError("schema-v10 table name is invalid")
    columns = required + nullable
    invalid = _schema_v10_timestamp_invalid("NEW", required, nullable)
    statements = [
        """
        CREATE TRIGGER {table}_v10_timestamp_insert_guard
        BEFORE INSERT ON {table}
        WHEN {invalid}
        BEGIN SELECT RAISE(ABORT, 'production_timestamp_invalid'); END
        """.format(table=table, invalid=invalid)
    ]
    if mutable:
        statements.append(
            """
            CREATE TRIGGER {table}_v10_timestamp_update_guard
            BEFORE UPDATE OF {columns} ON {table}
            WHEN {invalid}
            BEGIN SELECT RAISE(ABORT, 'production_timestamp_invalid'); END
            """.format(
                table=table,
                columns=", ".join(columns),
                invalid=invalid,
            )
        )
    return tuple(statements)


def _schema_v10_dispatch_tuple_valid(prefix: str) -> str:
    """Close both dispatch tuples without relying on CHECK(NULL) semantics."""

    if prefix not in ("NEW", "row"):
        raise ValueError("schema-v10 dispatch prefix is invalid")
    return """
        (
            (
                {p}.published_sequence = 0 AND
                {p}.published_repository_revision IS NULL AND
                {p}.published_ordinal IS NULL
            ) OR (
                {p}.published_sequence BETWEEN 1 AND 9223372036854775807 AND
                {p}.published_repository_revision BETWEEN 1 AND
                    9223372036854775807 AND
                {p}.published_ordinal BETWEEN 0 AND 9999
            )
        ) AND (
            (
                {p}.inflight_sequence IS NULL AND
                {p}.inflight_repository_revision IS NULL AND
                {p}.inflight_ordinal IS NULL
            ) OR (
                {p}.inflight_sequence = {p}.published_sequence + 1 AND
                {p}.inflight_repository_revision BETWEEN 1 AND
                    9223372036854775807 AND
                {p}.inflight_ordinal BETWEEN 0 AND 9999
            )
        )
    """.format(p=prefix)


def _schema_v10_preflight_predicate() -> str:
    predicates = []
    for table, required, nullable in (
        _SCHEMA_V10_APPEND_ONLY_TIMESTAMPS + _SCHEMA_V10_MUTABLE_TIMESTAMPS
    ):
        predicates.append(
            "NOT EXISTS (SELECT 1 FROM {0} AS row WHERE {1})".format(
                table,
                _schema_v10_timestamp_invalid("row", required, nullable),
            )
        )
    predicates.append(
        "NOT EXISTS (SELECT 1 FROM transaction_receipts AS row WHERE "
        + _schema_v10_reserved_receipt("row")
        + " AND "
        + _schema_v10_timestamp_invalid("row", ("committed_at",), ())
        + ")"
    )
    predicates.append(
        "NOT EXISTS (SELECT 1 FROM outbox_dispatch_head AS row WHERE "
        "NOT COALESCE(" + _schema_v10_dispatch_tuple_valid("row") + ", 0))"
    )
    return "(\n    " + " AND\n    ".join(predicates) + "\n)"


SCHEMA_V10_HARDENING_PREFLIGHT_PREDICATE = _schema_v10_preflight_predicate()

_SCHEMA_V10_TIMESTAMP_TRIGGER_STATEMENTS = tuple(
    statement
    for table, required, nullable in _SCHEMA_V10_APPEND_ONLY_TIMESTAMPS
    for statement in _schema_v10_timestamp_triggers(
        table, required, nullable, mutable=False
    )
) + tuple(
    statement
    for table, required, nullable in _SCHEMA_V10_MUTABLE_TIMESTAMPS
    for statement in _schema_v10_timestamp_triggers(
        table, required, nullable, mutable=True
    )
)

_SCHEMA_V10_RESERVED_RECEIPT_TRIGGER_STATEMENTS = (
    """
    CREATE TRIGGER transaction_receipts_v10_timestamp_insert_guard
    BEFORE INSERT ON transaction_receipts
    WHEN {reserved} AND {invalid}
    BEGIN SELECT RAISE(ABORT, 'production_timestamp_invalid'); END
    """.format(
        reserved=_schema_v10_reserved_receipt("NEW"),
        invalid=_schema_v10_timestamp_invalid("NEW", ("committed_at",), ()),
    ),
    """
    CREATE TRIGGER transaction_receipts_v10_timestamp_update_guard
    BEFORE UPDATE OF transaction_id, kind, committed_at ON transaction_receipts
    WHEN ({new_reserved} OR {old_reserved}) AND {invalid}
    BEGIN SELECT RAISE(ABORT, 'production_timestamp_invalid'); END
    """.format(
        new_reserved=_schema_v10_reserved_receipt("NEW"),
        old_reserved=_schema_v10_reserved_receipt("OLD"),
        invalid=_schema_v10_timestamp_invalid("NEW", ("committed_at",), ()),
    ),
)

RUNTIME_SCHEMA_HARDENING_MIGRATION = Migration(
    version=10,
    name="runtime_timestamp_dispatch_hardening_v1",
    statements=(
        """
        CREATE TABLE schema_v10_runtime_hardening_preflight (
            verified INTEGER NOT NULL
                CONSTRAINT schema_v10_runtime_hardening_precondition
                CHECK(verified = 1)
        ) STRICT
        """,
        "INSERT INTO schema_v10_runtime_hardening_preflight(verified) "
        "SELECT CASE WHEN " + SCHEMA_V10_HARDENING_PREFLIGHT_PREDICATE +
        " THEN 1 ELSE 0 END",
        *_SCHEMA_V10_TIMESTAMP_TRIGGER_STATEMENTS,
        *_SCHEMA_V10_RESERVED_RECEIPT_TRIGGER_STATEMENTS,
        """
        CREATE TRIGGER outbox_dispatch_head_v10_tuple_insert_guard
        BEFORE INSERT ON outbox_dispatch_head
        WHEN NOT COALESCE({dispatch_valid}, 0)
        BEGIN SELECT RAISE(ABORT,
            'production_outbox_dispatch_tuple_invalid'); END
        """.format(
            dispatch_valid=_schema_v10_dispatch_tuple_valid("NEW")
        ),
        """
        CREATE TRIGGER outbox_dispatch_head_v10_tuple_update_guard
        BEFORE UPDATE OF published_sequence, published_repository_revision,
                         published_ordinal, inflight_sequence,
                         inflight_repository_revision, inflight_ordinal
        ON outbox_dispatch_head
        WHEN NOT COALESCE({dispatch_valid}, 0)
        BEGIN SELECT RAISE(ABORT,
            'production_outbox_dispatch_tuple_invalid'); END
        """.format(
            dispatch_valid=_schema_v10_dispatch_tuple_valid("NEW")
        ),
        "DROP TABLE schema_v10_runtime_hardening_preflight",
    ),
)


# Schema-v11 corrects two chronology/state invariants in schema-v8 objects.
# V1-V10 remain byte-for-byte immutable.  Rebuilding is intentionally allowed
# only for an authentic empty V10 production namespace under legacy authority,
# so no issued receipt or active cutover state is reinterpreted in place.
_SCHEMA_V11_PRODUCTION_NAMESPACE_TABLES = (
    "business_cutover_head",
    "business_cutover_install_receipts",
    "business_asset_stage_receipts",
    "business_asset_stage_members",
    "business_asset_coverage_receipts",
    "repository_audit_certificates",
    "repository_audit_head",
    "business_cutover_activation_receipts",
    "business_cutover_mount_receipts",
    "atomic_business_write_receipts",
    "atomic_business_control_members",
    "worker_ledger_head",
    "worker_ledger_entries",
    "worker_execution_receipts",
    "pipeline_control_intents",
    "pipeline_control_receipts",
    "worker_targets",
    "worker_target_current",
    "worker_attempts",
    "worker_effects",
    "worker_results",
    "worker_reconcile_proofs",
    "outbox_allocations",
    "outbox_allocation_head",
    "outbox_dispatch_head",
    "worker_effect_request_projections",
    "worker_result_payload_projections",
    "worker_proof_payload_projections",
)

_SCHEMA_V11_FROZEN_V10_REGISTRY = (
    KERNEL_MIGRATION,
    BUSINESS_SNAPSHOT_MIGRATION,
    LEGACY_IMPORT_LEDGER_MIGRATION,
    LEGACY_CATALOG_SEAL_MIGRATION,
    LEGACY_REPOSITORY_PROOF_MIGRATION,
    BUSINESS_TRANSACTION_MIGRATION,
    LOCAL_REVISION_MIGRATION,
    PRODUCTION_AUTHORITY_WORKER_MIGRATION,
    WORKER_PAYLOAD_PROJECTION_MIGRATION,
    RUNTIME_SCHEMA_HARDENING_MIGRATION,
)


def _schema_v11_preflight_predicate() -> str:
    ledger_rows = [
        "(SELECT count(*) FROM schema_migrations) = 10",
    ]
    ledger_rows.extend(
        "EXISTS (SELECT 1 FROM schema_migrations WHERE version={0} "
        "AND name='{1}' AND checksum='{2}')".format(
            migration.version,
            migration.name,
            migration.checksum,
        )
        for migration in _SCHEMA_V11_FROZEN_V10_REGISTRY
    )
    ledger = (
        "((NOT EXISTS (SELECT 1 FROM kernel_repository) AND "
        "NOT EXISTS (SELECT 1 FROM schema_migrations)) OR ("
        + " AND ".join(ledger_rows)
        + "))"
    )
    empty = [
        "NOT EXISTS (SELECT 1 FROM {0})".format(table)
        for table in _SCHEMA_V11_PRODUCTION_NAMESPACE_TABLES
    ]
    return "(\n    " + " AND\n    ".join(
        [PRODUCTION_RUNTIME_PREFLIGHT_PREDICATE, ledger, *empty]
    ) + "\n)"


SCHEMA_V11_MOUNT_CHRONOLOGY_PREFLIGHT_PREDICATE = (
    _schema_v11_preflight_predicate()
)

_SCHEMA_V11_MOUNT_RECEIPTS_TABLE = f"""
    CREATE TABLE business_cutover_mount_receipts (
        cutover_id TEXT NOT NULL PRIMARY KEY CHECK(
            length(cutover_id) = 64 AND
            length(CAST(cutover_id AS BLOB)) = 64 AND
            cutover_id NOT GLOB '*[^0-9a-f]*'
        ),
        transaction_id TEXT NOT NULL UNIQUE CHECK(
            transaction_id = '_business-cutover-mount:' || cutover_id
        ),
        receipt_kind TEXT NOT NULL CHECK(
            receipt_kind = '_business_cutover_mount_v2'
        ),
        committed_at TEXT NOT NULL CHECK(
            length(CAST(committed_at AS BLOB)) = 24 AND
            strftime('%Y-%m-%dT%H:%M:%fZ', committed_at) = committed_at
        ),
        install_transaction_id TEXT NOT NULL,
        install_receipt_kind TEXT NOT NULL CHECK(
            install_receipt_kind = '_business_cutover_install_v1'
        ),
        install_committed_at TEXT NOT NULL CHECK(
            length(CAST(install_committed_at AS BLOB)) = 24 AND
            strftime('%Y-%m-%dT%H:%M:%fZ', install_committed_at) =
                install_committed_at
        ),
        install_manifest_sha256 BLOB NOT NULL CHECK(
            typeof(install_manifest_sha256) = 'blob' AND
            length(install_manifest_sha256) = 32
        ),
        coverage_transaction_id TEXT NOT NULL,
        coverage_receipt_kind TEXT NOT NULL CHECK(
            coverage_receipt_kind = '_business_cutover_asset_coverage_v1'
        ),
        coverage_committed_at TEXT NOT NULL CHECK(
            length(CAST(coverage_committed_at AS BLOB)) = 24 AND
            strftime('%Y-%m-%dT%H:%M:%fZ', coverage_committed_at) =
                coverage_committed_at
        ),
        coverage_manifest_sha256 BLOB NOT NULL CHECK(
            typeof(coverage_manifest_sha256) = 'blob' AND
            length(coverage_manifest_sha256) = 32
        ),
        audit_transaction_id TEXT NOT NULL,
        audit_manifest_sha256 BLOB NOT NULL CHECK(
            typeof(audit_manifest_sha256) = 'blob' AND
            length(audit_manifest_sha256) = 32
        ),
        activation_transaction_id TEXT NOT NULL,
        activation_manifest_sha256 BLOB NOT NULL CHECK(
            typeof(activation_manifest_sha256) = 'blob' AND
            length(activation_manifest_sha256) = 32
        ),
        repository_epoch TEXT NOT NULL CHECK(
            length(repository_epoch) = 64 AND
            length(CAST(repository_epoch AS BLOB)) = 64 AND
            repository_epoch NOT GLOB '*[^0-9a-f]*'
        ),
        runtime_generation TEXT NOT NULL CHECK(
            length(CAST(runtime_generation AS BLOB)) BETWEEN 1 AND
                {MAX_BUSINESS_RUNTIME_GENERATION_BYTES}
        ),
        code_digest BLOB NOT NULL CHECK(
            typeof(code_digest) = 'blob' AND length(code_digest) = 32
        ),
        manifest_bytes BLOB NOT NULL CHECK(
            typeof(manifest_bytes) = 'blob' AND
            length(manifest_bytes) BETWEEN 1 AND
                {MAX_BUSINESS_SMALL_PAYLOAD_BYTES}
        ),
        manifest_sha256 BLOB NOT NULL UNIQUE CHECK(
            typeof(manifest_sha256) = 'blob' AND
            length(manifest_sha256) = 32
        ),
        CHECK(
            coverage_committed_at <= install_committed_at AND
            install_committed_at <= committed_at
        ),
        FOREIGN KEY(transaction_id, receipt_kind, committed_at)
            REFERENCES transaction_receipts(transaction_id, kind, committed_at)
            ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY(
            install_transaction_id,
            install_receipt_kind,
            install_committed_at
        ) REFERENCES transaction_receipts(
            transaction_id, kind, committed_at
        ) ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY(
            coverage_transaction_id,
            coverage_receipt_kind,
            coverage_committed_at
        ) REFERENCES transaction_receipts(
            transaction_id, kind, committed_at
        ) ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY(cutover_id, install_transaction_id, install_manifest_sha256)
            REFERENCES business_cutover_install_receipts(
                cutover_id, transaction_id, manifest_sha256
            )
            ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY(cutover_id, coverage_transaction_id, coverage_manifest_sha256)
            REFERENCES business_asset_coverage_receipts(
                cutover_id, transaction_id, manifest_sha256
            )
            ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY(cutover_id, audit_transaction_id, audit_manifest_sha256)
            REFERENCES repository_audit_certificates(
                cutover_id, transaction_id, manifest_sha256
            )
            ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY(cutover_id, activation_transaction_id,
                    activation_manifest_sha256)
            REFERENCES business_cutover_activation_receipts(
                cutover_id, transaction_id, manifest_sha256
            )
            ON UPDATE RESTRICT ON DELETE RESTRICT
    ) STRICT
"""

_SCHEMA_V11_CUTOVER_HEAD_TABLE = f"""
    CREATE TABLE business_cutover_head (
        singleton INTEGER NOT NULL PRIMARY KEY CHECK(singleton = 1),
        cutover_id TEXT NOT NULL UNIQUE CHECK(
            length(cutover_id) = 64 AND
            length(CAST(cutover_id AS BLOB)) = 64 AND
            cutover_id NOT GLOB '*[^0-9a-f]*'
        ),
        state_revision INTEGER NOT NULL CHECK(
            state_revision BETWEEN 1 AND 9223372036854775807
        ),
        phase_revision INTEGER NOT NULL CHECK(phase_revision BETWEEN 1 AND 5),
        phase TEXT NOT NULL CHECK(
            (phase_revision = 1 AND phase = 'installed') OR
            (phase_revision = 2 AND phase = 'assets_covered') OR
            (phase_revision = 3 AND phase = 'audited') OR
            (phase_revision = 4 AND phase = 'activated') OR
            (phase_revision = 5 AND phase = 'mounted')
        ),
        authority_state TEXT NOT NULL CHECK(
            (phase_revision < 4 AND authority_state = 'read_only') OR
            (phase_revision >= 4 AND authority_state = 'active')
        ),
        repository_epoch TEXT NOT NULL CHECK(
            length(repository_epoch) = 64 AND
            length(CAST(repository_epoch AS BLOB)) = 64 AND
            repository_epoch NOT GLOB '*[^0-9a-f]*'
        ),
        runtime_generation TEXT NOT NULL CHECK(
            length(CAST(runtime_generation AS BLOB)) BETWEEN 1 AND
                {MAX_BUSINESS_RUNTIME_GENERATION_BYTES}
        ),
        code_digest BLOB NOT NULL CHECK(
            typeof(code_digest) = 'blob' AND length(code_digest) = 32
        ),
        install_transaction_id TEXT NOT NULL,
        install_manifest_sha256 BLOB NOT NULL CHECK(
            typeof(install_manifest_sha256) = 'blob' AND
            length(install_manifest_sha256) = 32
        ),
        coverage_transaction_id TEXT,
        coverage_manifest_sha256 BLOB,
        audit_transaction_id TEXT,
        audit_manifest_sha256 BLOB,
        activation_transaction_id TEXT,
        activation_manifest_sha256 BLOB,
        mount_transaction_id TEXT,
        mount_manifest_sha256 BLOB,
        previous_head_sha256 BLOB NOT NULL CHECK(
            typeof(previous_head_sha256) = 'blob' AND
            length(previous_head_sha256) = 32
        ),
        head_sha256 BLOB NOT NULL UNIQUE CHECK(
            typeof(head_sha256) = 'blob' AND length(head_sha256) = 32 AND
            head_sha256 != zeroblob(32)
        ),
        updated_at TEXT NOT NULL CHECK(
            length(CAST(updated_at AS BLOB)) = 24 AND
            strftime('%Y-%m-%dT%H:%M:%fZ', updated_at) = updated_at
        ),
        CHECK(state_revision = phase_revision),
        CHECK((coverage_transaction_id IS NULL) = (phase_revision < 2)),
        CHECK((coverage_manifest_sha256 IS NULL) = (phase_revision < 2)),
        CHECK((audit_transaction_id IS NULL) = (phase_revision < 3)),
        CHECK((audit_manifest_sha256 IS NULL) = (phase_revision < 3)),
        CHECK((activation_transaction_id IS NULL) = (phase_revision < 4)),
        CHECK((activation_manifest_sha256 IS NULL) = (phase_revision < 4)),
        CHECK((mount_transaction_id IS NULL) = (phase_revision < 5)),
        CHECK((mount_manifest_sha256 IS NULL) = (phase_revision < 5)),
        CHECK(
            (coverage_manifest_sha256 IS NULL OR (
                typeof(coverage_manifest_sha256) = 'blob' AND
                length(coverage_manifest_sha256) = 32
            )) AND
            (audit_manifest_sha256 IS NULL OR (
                typeof(audit_manifest_sha256) = 'blob' AND
                length(audit_manifest_sha256) = 32
            )) AND
            (activation_manifest_sha256 IS NULL OR (
                typeof(activation_manifest_sha256) = 'blob' AND
                length(activation_manifest_sha256) = 32
            )) AND
            (mount_manifest_sha256 IS NULL OR (
                typeof(mount_manifest_sha256) = 'blob' AND
                length(mount_manifest_sha256) = 32
            ))
        ),
        FOREIGN KEY(cutover_id, install_transaction_id, install_manifest_sha256)
            REFERENCES business_cutover_install_receipts(
                cutover_id, transaction_id, manifest_sha256
            )
            ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY(cutover_id, coverage_transaction_id, coverage_manifest_sha256)
            REFERENCES business_asset_coverage_receipts(
                cutover_id, transaction_id, manifest_sha256
            )
            ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY(cutover_id, audit_transaction_id, audit_manifest_sha256)
            REFERENCES repository_audit_certificates(
                cutover_id, transaction_id, manifest_sha256
            )
            ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY(cutover_id, activation_transaction_id,
                    activation_manifest_sha256)
            REFERENCES business_cutover_activation_receipts(
                cutover_id, transaction_id, manifest_sha256
            )
            ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY(cutover_id, mount_transaction_id, mount_manifest_sha256)
            REFERENCES business_cutover_mount_receipts(
                cutover_id, transaction_id, manifest_sha256
            )
            ON UPDATE RESTRICT ON DELETE RESTRICT
    ) STRICT
"""

_SCHEMA_V11_MOUNT_BINDING_TRIGGER = """
    CREATE TRIGGER business_cutover_mount_binding_guard
    BEFORE INSERT ON business_cutover_mount_receipts
    WHEN NOT EXISTS (
             SELECT 1 FROM business_cutover_install_receipts
             WHERE transaction_id = NEW.install_transaction_id AND
                   receipt_kind = NEW.install_receipt_kind AND
                   committed_at = NEW.install_committed_at AND
                   manifest_sha256 = NEW.install_manifest_sha256 AND
                   cutover_id = NEW.cutover_id AND
                   repository_epoch = NEW.repository_epoch AND
                   runtime_generation = NEW.runtime_generation AND
                   code_digest = NEW.code_digest
         ) OR NOT EXISTS (
             SELECT 1 FROM business_asset_coverage_receipts
             WHERE transaction_id = NEW.coverage_transaction_id AND
                   receipt_kind = NEW.coverage_receipt_kind AND
                   committed_at = NEW.coverage_committed_at AND
                   manifest_sha256 = NEW.coverage_manifest_sha256 AND
                   cutover_id = NEW.cutover_id AND
                   repository_epoch = NEW.repository_epoch
         ) OR NOT EXISTS (
             SELECT 1 FROM transaction_receipts
             WHERE transaction_id = NEW.install_transaction_id AND
                   kind = NEW.install_receipt_kind AND
                   committed_at = NEW.install_committed_at
         ) OR NOT EXISTS (
             SELECT 1 FROM transaction_receipts
             WHERE transaction_id = NEW.coverage_transaction_id AND
                   kind = NEW.coverage_receipt_kind AND
                   committed_at = NEW.coverage_committed_at
         ) OR NOT EXISTS (
             SELECT 1 FROM repository_audit_certificates
             WHERE transaction_id = NEW.audit_transaction_id AND
                   manifest_sha256 = NEW.audit_manifest_sha256 AND
                   cutover_id = NEW.cutover_id
         ) OR NOT EXISTS (
             SELECT 1 FROM business_cutover_activation_receipts
             WHERE transaction_id = NEW.activation_transaction_id AND
                   manifest_sha256 = NEW.activation_manifest_sha256 AND
                   cutover_id = NEW.cutover_id
         )
    BEGIN SELECT RAISE(ABORT, 'production_cutover_binding_invalid'); END
"""

_SCHEMA_V11_HEAD_BINDING_TRIGGERS = (
    """
    CREATE TRIGGER business_cutover_head_insert_binding_guard
    BEFORE INSERT ON business_cutover_head
    WHEN NOT EXISTS (
        SELECT 1 FROM business_cutover_install_receipts
        WHERE transaction_id = NEW.install_transaction_id AND
              manifest_sha256 = NEW.install_manifest_sha256 AND
              cutover_id = NEW.cutover_id AND
              repository_epoch = NEW.repository_epoch AND
              runtime_generation = NEW.runtime_generation AND
              code_digest = NEW.code_digest
    )
    BEGIN SELECT RAISE(ABORT, 'production_cutover_binding_invalid'); END
    """,
    """
    CREATE TRIGGER business_cutover_head_update_binding_guard
    BEFORE UPDATE ON business_cutover_head
    WHEN NOT EXISTS (
             SELECT 1 FROM business_cutover_install_receipts
             WHERE transaction_id = NEW.install_transaction_id AND
                   manifest_sha256 = NEW.install_manifest_sha256 AND
                   cutover_id = NEW.cutover_id
         ) OR (NEW.phase_revision >= 2 AND NOT EXISTS (
             SELECT 1 FROM business_asset_coverage_receipts
             WHERE transaction_id = NEW.coverage_transaction_id AND
                   manifest_sha256 = NEW.coverage_manifest_sha256 AND
                   cutover_id = NEW.cutover_id AND
                   repository_epoch = NEW.repository_epoch
         )) OR (NEW.phase_revision >= 3 AND NOT EXISTS (
             SELECT 1 FROM repository_audit_certificates
             WHERE transaction_id = NEW.audit_transaction_id AND
                   manifest_sha256 = NEW.audit_manifest_sha256 AND
                   cutover_id = NEW.cutover_id
         )) OR (NEW.phase_revision >= 4 AND NOT EXISTS (
             SELECT 1 FROM business_cutover_activation_receipts
             WHERE transaction_id = NEW.activation_transaction_id AND
                   manifest_sha256 = NEW.activation_manifest_sha256 AND
                   cutover_id = NEW.cutover_id
         )) OR (NEW.phase_revision = 5 AND NOT EXISTS (
             SELECT 1 FROM business_cutover_mount_receipts
             WHERE transaction_id = NEW.mount_transaction_id AND
                   manifest_sha256 = NEW.mount_manifest_sha256 AND
                   cutover_id = NEW.cutover_id
         ))
    BEGIN SELECT RAISE(ABORT, 'production_cutover_binding_invalid'); END
    """,
)

_SCHEMA_V11_HEAD_PHASE_TRIGGERS = (
    """
    CREATE TRIGGER business_cutover_head_initial_guard
    BEFORE INSERT ON business_cutover_head
    WHEN NEW.phase_revision != 1 OR NEW.phase != 'installed' OR
         NEW.authority_state != 'read_only'
    BEGIN SELECT RAISE(ABORT, 'production_cutover_phase_invalid'); END
    """,
    """
    CREATE TRIGGER business_cutover_head_phase_guard
    BEFORE UPDATE ON business_cutover_head
    WHEN OLD.phase_revision = 5 OR
         NEW.phase_revision != OLD.phase_revision + 1 OR
         NEW.cutover_id != OLD.cutover_id OR
         NEW.repository_epoch != OLD.repository_epoch OR
         NEW.runtime_generation != OLD.runtime_generation OR
         NEW.code_digest != OLD.code_digest OR
         NEW.install_transaction_id != OLD.install_transaction_id OR
         NEW.install_manifest_sha256 != OLD.install_manifest_sha256 OR
         (OLD.coverage_transaction_id IS NOT NULL AND (
             NEW.coverage_transaction_id IS NOT OLD.coverage_transaction_id OR
             NEW.coverage_manifest_sha256 IS NOT OLD.coverage_manifest_sha256
         )) OR
         (OLD.audit_transaction_id IS NOT NULL AND (
             NEW.audit_transaction_id IS NOT OLD.audit_transaction_id OR
             NEW.audit_manifest_sha256 IS NOT OLD.audit_manifest_sha256
         )) OR
         (OLD.activation_transaction_id IS NOT NULL AND (
             NEW.activation_transaction_id IS NOT OLD.activation_transaction_id OR
             NEW.activation_manifest_sha256 IS NOT OLD.activation_manifest_sha256
         )) OR
         (OLD.mount_transaction_id IS NOT NULL AND (
             NEW.mount_transaction_id IS NOT OLD.mount_transaction_id OR
             NEW.mount_manifest_sha256 IS NOT OLD.mount_manifest_sha256
         ))
    BEGIN SELECT RAISE(ABORT, 'production_cutover_phase_invalid'); END
    """,
)

MOUNT_CHRONOLOGY_MIGRATION = Migration(
    version=11,
    name="mount_chronology_state_closure_v1",
    statements=(
        """
        CREATE TABLE schema_v11_mount_chronology_preflight (
            verified INTEGER NOT NULL
                CONSTRAINT schema_v11_mount_chronology_precondition
                CHECK(verified = 1)
        ) STRICT
        """,
        "INSERT INTO schema_v11_mount_chronology_preflight(verified) "
        "SELECT CASE WHEN " +
        SCHEMA_V11_MOUNT_CHRONOLOGY_PREFLIGHT_PREDICATE +
        " THEN 1 ELSE 0 END",
        "DROP TABLE business_cutover_head",
        "DROP TABLE business_cutover_mount_receipts",
        _SCHEMA_V11_MOUNT_RECEIPTS_TABLE,
        """
        CREATE UNIQUE INDEX business_cutover_mount_receipts_pointer
        ON business_cutover_mount_receipts(
            cutover_id, transaction_id, manifest_sha256
        )
        """,
        _SCHEMA_V11_CUTOVER_HEAD_TABLE,
        _SCHEMA_V11_MOUNT_BINDING_TRIGGER,
        *_SCHEMA_V11_HEAD_BINDING_TRIGGERS,
        *_production_append_only_triggers("business_cutover_mount_receipts"),
        *_production_head_cas_triggers(
            "business_cutover_head",
            "cutover_id",
            "previous_head_sha256",
            "head_sha256",
        ),
        *_SCHEMA_V11_HEAD_PHASE_TRIGGERS,
        *_schema_v10_timestamp_triggers(
            "business_cutover_mount_receipts",
            ("committed_at", "install_committed_at", "coverage_committed_at"),
            (),
            mutable=False,
        ),
        *_schema_v10_timestamp_triggers(
            "business_cutover_head",
            ("updated_at",),
            (),
            mutable=True,
        ),
        "DROP TABLE schema_v11_mount_chronology_preflight",
    ),
)


# Schema-v12 is the last clean-database migration before production cutover is
# allowed to acquire durable callers.  V1-V11 remain byte-for-byte immutable.
# The preflight deliberately accepts only an authentic, production-empty V11:
# no earlier binary could persist the complete install mapping or a legal
# pre-mount baseline atomic receipt, so grandfathering such rows would turn an
# unprovable partial cutover into trusted history.
_SCHEMA_V12_FROZEN_V11_REGISTRY = (
    KERNEL_MIGRATION,
    BUSINESS_SNAPSHOT_MIGRATION,
    LEGACY_IMPORT_LEDGER_MIGRATION,
    LEGACY_CATALOG_SEAL_MIGRATION,
    LEGACY_REPOSITORY_PROOF_MIGRATION,
    BUSINESS_TRANSACTION_MIGRATION,
    LOCAL_REVISION_MIGRATION,
    PRODUCTION_AUTHORITY_WORKER_MIGRATION,
    WORKER_PAYLOAD_PROJECTION_MIGRATION,
    RUNTIME_SCHEMA_HARDENING_MIGRATION,
    MOUNT_CHRONOLOGY_MIGRATION,
)


def _schema_v12_preflight_predicate() -> str:
    ledger_rows = [
        "(SELECT count(*) FROM schema_migrations) = 11",
    ]
    ledger_rows.extend(
        "EXISTS (SELECT 1 FROM schema_migrations WHERE version={0} "
        "AND name='{1}' AND checksum='{2}')".format(
            migration.version,
            migration.name,
            migration.checksum,
        )
        for migration in _SCHEMA_V12_FROZEN_V11_REGISTRY
    )
    ledger = (
        "((NOT EXISTS (SELECT 1 FROM kernel_repository) AND "
        "NOT EXISTS (SELECT 1 FROM schema_migrations)) OR ("
        + " AND ".join(ledger_rows)
        + "))"
    )
    empty = [
        "NOT EXISTS (SELECT 1 FROM {0})".format(table)
        for table in _SCHEMA_V11_PRODUCTION_NAMESPACE_TABLES
    ]
    source_receipt = """
    (
        (
            NOT EXISTS (SELECT 1 FROM legacy_repository_seals) AND
            NOT EXISTS (
                SELECT 1 FROM transaction_receipts AS source_receipt
                WHERE source_receipt.kind = '_legacy_repository_seal_v1' OR
                      substr(source_receipt.transaction_id, 1, 24) =
                        '_legacy-repository-seal:'
            )
        ) OR (
            (SELECT count(*) FROM legacy_repository_seals) = 1 AND
            (SELECT count(*) FROM transaction_receipts AS source_receipt
             WHERE source_receipt.kind = '_legacy_repository_seal_v1' OR
                   substr(source_receipt.transaction_id, 1, 24) =
                    '_legacy-repository-seal:') = 1 AND
            EXISTS (
                SELECT 1
                FROM legacy_repository_seals AS source_seal
                JOIN transaction_receipts AS source_receipt
                  ON source_receipt.transaction_id =
                        source_seal.transaction_id AND
                     source_receipt.kind = source_seal.receipt_kind
                WHERE length(
                        CAST(source_receipt.committed_at AS BLOB)
                      ) = 24 AND
                      strftime(
                        '%Y-%m-%dT%H:%M:%fZ',
                        source_receipt.committed_at
                      ) = source_receipt.committed_at
            )
        )
    )
    """
    return "(\n    " + " AND\n    ".join(
        [
            PRODUCTION_RUNTIME_PREFLIGHT_PREDICATE,
            ledger,
            source_receipt,
            *empty,
        ]
    ) + "\n)"


SCHEMA_V12_INSTALL_CLOSURE_PREFLIGHT_PREDICATE = (
    _schema_v12_preflight_predicate()
)

_SCHEMA_V12_INSTALL_ARTIFACTS_TABLE = f"""
    CREATE TABLE business_cutover_install_artifacts (
        cutover_id TEXT NOT NULL CHECK(
            length(cutover_id) = 64 AND
            length(CAST(cutover_id AS BLOB)) = 64 AND
            cutover_id NOT GLOB '*[^0-9a-f]*'
        ),
        artifact_kind TEXT NOT NULL CHECK(
            artifact_kind IN ('member_mapping', 'baseline_projection')
        ),
        artifact_schema TEXT NOT NULL CHECK(
            (artifact_kind = 'member_mapping' AND
             artifact_schema = 'ze.business-cutover.member-mapping.v1') OR
            (artifact_kind = 'baseline_projection' AND
             artifact_schema =
                'ze.business-cutover-baseline-projection.manifest.v2')
        ),
        byte_length INTEGER NOT NULL CHECK(
            byte_length BETWEEN 1 AND {MAX_BUSINESS_CUTOVER_ARTIFACT_BYTES}
        ),
        chunk_count INTEGER NOT NULL CHECK(
            chunk_count BETWEEN 1 AND {MAX_BUSINESS_CUTOVER_ARTIFACT_CHUNKS} AND
            chunk_count =
                ((byte_length - 1) /
                    {MAX_BUSINESS_CUTOVER_ARTIFACT_CHUNK_BYTES}) + 1
        ),
        artifact_sha256 BLOB NOT NULL CHECK(
            typeof(artifact_sha256) = 'blob' AND
            length(artifact_sha256) = 32
        ),
        PRIMARY KEY(cutover_id, artifact_kind),
        UNIQUE(cutover_id, artifact_kind, artifact_sha256),
        FOREIGN KEY(cutover_id)
            REFERENCES business_cutover_install_receipts(cutover_id)
            ON UPDATE RESTRICT ON DELETE RESTRICT
            DEFERRABLE INITIALLY DEFERRED
    ) STRICT
"""

_SCHEMA_V12_INSTALL_ARTIFACT_CHUNKS_TABLE = f"""
    CREATE TABLE business_cutover_install_artifact_chunks (
        cutover_id TEXT NOT NULL,
        artifact_kind TEXT NOT NULL CHECK(
            artifact_kind IN ('member_mapping', 'baseline_projection')
        ),
        ordinal INTEGER NOT NULL CHECK(
            ordinal BETWEEN 0 AND
                {MAX_BUSINESS_CUTOVER_ARTIFACT_CHUNKS - 1}
        ),
        byte_offset INTEGER NOT NULL CHECK(
            byte_offset = ordinal *
                {MAX_BUSINESS_CUTOVER_ARTIFACT_CHUNK_BYTES}
        ),
        chunk_bytes BLOB NOT NULL CHECK(
            typeof(chunk_bytes) = 'blob' AND
            length(chunk_bytes) BETWEEN 1 AND
                {MAX_BUSINESS_CUTOVER_ARTIFACT_CHUNK_BYTES}
        ),
        chunk_sha256 BLOB NOT NULL CHECK(
            typeof(chunk_sha256) = 'blob' AND length(chunk_sha256) = 32
        ),
        PRIMARY KEY(cutover_id, artifact_kind, ordinal),
        UNIQUE(cutover_id, artifact_kind, ordinal, chunk_sha256),
        FOREIGN KEY(cutover_id, artifact_kind)
            REFERENCES business_cutover_install_artifacts(
                cutover_id, artifact_kind
            ) ON UPDATE RESTRICT ON DELETE RESTRICT
            DEFERRABLE INITIALLY DEFERRED
    ) STRICT
"""

_SCHEMA_V12_ATOMIC_BASELINE_POINTER_INDEX = """
    CREATE UNIQUE INDEX atomic_business_write_receipts_v12_baseline_pointer
    ON atomic_business_write_receipts(
        commit_id, transaction_id, receipt_kind, committed_at,
        atomic_manifest_sha256, pipeline_intents_sha256, controls_sha256,
        control_count, before_worker_sequence, before_worker_tail_sha256,
        after_worker_sequence, after_worker_tail_sha256, manifest_sha256,
        receipt_sha256, production_manifest_sha256, target_count,
        targets_sha256, production_receipt_sha256
    )
"""

_SCHEMA_V12_SOURCE_PROOF_POINTER_INDEX = """
    CREATE UNIQUE INDEX legacy_repository_seals_v12_closure_pointer
    ON legacy_repository_seals(
        cutover_id, transaction_id, receipt_kind, repository_revision,
        manifest_sha256, proof_sha256
    )
"""

_SCHEMA_V12_COVERAGE_POINTER_INDEX = """
    CREATE UNIQUE INDEX business_asset_coverage_receipts_v12_closure_pointer
    ON business_asset_coverage_receipts(
        cutover_id, transaction_id, receipt_kind,
        source_admission_revision, source_admission_sha256,
        manifest_sha256, coverage_sha256
    )
"""

_SCHEMA_V12_BASELINE_ATOMIC_RECEIPTS_TABLE = f"""
    CREATE TABLE business_cutover_baseline_atomic_receipts (
        cutover_id TEXT NOT NULL CHECK(
            length(cutover_id) = 64 AND
            length(CAST(cutover_id AS BLOB)) = 64 AND
            cutover_id NOT GLOB '*[^0-9a-f]*'
        ),
        member_ordinal INTEGER NOT NULL CHECK(
            member_ordinal BETWEEN 0 AND 9999
        ),
        repository_revision INTEGER NOT NULL CHECK(
            repository_revision BETWEEN 1 AND 9223372036854775807
        ),
        baseline_outer_schema TEXT NOT NULL CHECK(
            baseline_outer_schema =
                'ze.business-cutover-baseline-atomic-outer.manifest.v1'
        ),
        baseline_outer_protocol TEXT NOT NULL CHECK(
            baseline_outer_protocol =
                'ze.business-cutover-baseline-atomic-outer.v1'
        ),
        member_mapping_kind TEXT NOT NULL CHECK(
            member_mapping_kind = 'member_mapping'
        ),
        member_mapping_sha256 BLOB NOT NULL CHECK(
            typeof(member_mapping_sha256) = 'blob' AND
            length(member_mapping_sha256) = 32
        ),
        baseline_projection_kind TEXT NOT NULL CHECK(
            baseline_projection_kind = 'baseline_projection'
        ),
        baseline_projection_sha256 BLOB NOT NULL CHECK(
            typeof(baseline_projection_sha256) = 'blob' AND
            length(baseline_projection_sha256) = 32
        ),
        commit_id TEXT NOT NULL UNIQUE CHECK(
            length(commit_id) = 32 AND
            length(CAST(commit_id AS BLOB)) = 32 AND
            commit_id NOT GLOB '*[^0-9a-f]*'
        ),
        transaction_id TEXT NOT NULL UNIQUE CHECK(
            transaction_id = '{BUSINESS_TRANSACTION_PREFIX}' || commit_id
        ),
        receipt_kind TEXT NOT NULL CHECK(
            receipt_kind = '{BUSINESS_TRANSACTION_KIND}'
        ),
        committed_at TEXT NOT NULL CHECK(
            length(CAST(committed_at AS BLOB)) = 24 AND
            strftime('%Y-%m-%dT%H:%M:%fZ', committed_at) = committed_at
        ),
        install_transaction_id TEXT NOT NULL CHECK(
            install_transaction_id =
                '_business-cutover-install:' || cutover_id
        ),
        install_manifest_sha256 BLOB NOT NULL CHECK(
            typeof(install_manifest_sha256) = 'blob' AND
            length(install_manifest_sha256) = 32
        ),
        atomic_manifest_sha256 BLOB NOT NULL CHECK(
            typeof(atomic_manifest_sha256) = 'blob' AND
            length(atomic_manifest_sha256) = 32
        ),
        pipeline_intents_sha256 BLOB NOT NULL CHECK(
            typeof(pipeline_intents_sha256) = 'blob' AND
            length(pipeline_intents_sha256) = 32
        ),
        controls_sha256 BLOB NOT NULL CHECK(
            typeof(controls_sha256) = 'blob' AND
            length(controls_sha256) = 32
        ),
        control_count INTEGER NOT NULL CHECK(control_count = 0),
        before_worker_sequence INTEGER NOT NULL CHECK(
            before_worker_sequence = 0
        ),
        before_worker_tail_sha256 BLOB NOT NULL CHECK(
            typeof(before_worker_tail_sha256) = 'blob' AND
            before_worker_tail_sha256 = zeroblob(32)
        ),
        after_worker_sequence INTEGER NOT NULL CHECK(
            after_worker_sequence = 0
        ),
        after_worker_tail_sha256 BLOB NOT NULL CHECK(
            typeof(after_worker_tail_sha256) = 'blob' AND
            after_worker_tail_sha256 = zeroblob(32)
        ),
        manifest_bytes BLOB NOT NULL CHECK(
            typeof(manifest_bytes) = 'blob' AND
            length(manifest_bytes) BETWEEN 1 AND
                {MAX_BUSINESS_SMALL_PAYLOAD_BYTES}
        ),
        manifest_sha256 BLOB NOT NULL CHECK(
            typeof(manifest_sha256) = 'blob' AND
            length(manifest_sha256) = 32
        ),
        receipt_sha256 BLOB NOT NULL UNIQUE CHECK(
            typeof(receipt_sha256) = 'blob' AND length(receipt_sha256) = 32
        ),
        baseline_outer_manifest_bytes BLOB NOT NULL CHECK(
            typeof(baseline_outer_manifest_bytes) = 'blob' AND
            length(baseline_outer_manifest_bytes) BETWEEN 1 AND
                {MAX_BUSINESS_SMALL_PAYLOAD_BYTES}
        ),
        baseline_outer_manifest_sha256 BLOB NOT NULL CHECK(
            typeof(baseline_outer_manifest_sha256) = 'blob' AND
            length(baseline_outer_manifest_sha256) = 32
        ),
        target_count INTEGER NOT NULL CHECK(target_count = 0),
        targets_sha256 BLOB NOT NULL CHECK(
            typeof(targets_sha256) = 'blob' AND length(targets_sha256) = 32
        ),
        baseline_outer_receipt_sha256 BLOB NOT NULL UNIQUE CHECK(
            typeof(baseline_outer_receipt_sha256) = 'blob' AND
            length(baseline_outer_receipt_sha256) = 32
        ),
        PRIMARY KEY(cutover_id, member_ordinal),
        UNIQUE(cutover_id, repository_revision),
        FOREIGN KEY(transaction_id, receipt_kind, committed_at)
            REFERENCES transaction_receipts(
                transaction_id, kind, committed_at
            ) ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY(commit_id)
            REFERENCES business_commits(commit_id)
            ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY(
            cutover_id, install_transaction_id, install_manifest_sha256
        ) REFERENCES business_cutover_install_receipts(
            cutover_id, transaction_id, manifest_sha256
        ) ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY(
            cutover_id, member_mapping_kind, member_mapping_sha256
        ) REFERENCES business_cutover_install_artifacts(
            cutover_id, artifact_kind, artifact_sha256
        ) ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY(
            cutover_id, baseline_projection_kind,
            baseline_projection_sha256
        ) REFERENCES business_cutover_install_artifacts(
            cutover_id, artifact_kind, artifact_sha256
        ) ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY(
            commit_id, transaction_id, receipt_kind, committed_at,
            atomic_manifest_sha256, pipeline_intents_sha256,
            controls_sha256, control_count, before_worker_sequence,
            before_worker_tail_sha256, after_worker_sequence,
            after_worker_tail_sha256, manifest_sha256, receipt_sha256,
            baseline_outer_manifest_sha256, target_count,
            targets_sha256, baseline_outer_receipt_sha256
        ) REFERENCES atomic_business_write_receipts(
            commit_id, transaction_id, receipt_kind, committed_at,
            atomic_manifest_sha256, pipeline_intents_sha256,
            controls_sha256, control_count, before_worker_sequence,
            before_worker_tail_sha256, after_worker_sequence,
            after_worker_tail_sha256, manifest_sha256, receipt_sha256,
            production_manifest_sha256, target_count,
            targets_sha256, production_receipt_sha256
        ) ON UPDATE RESTRICT ON DELETE RESTRICT
    ) STRICT
"""

_SCHEMA_V12_INSTALL_CLOSURES_TABLE = f"""
    CREATE TABLE business_cutover_install_closures (
        cutover_id TEXT NOT NULL PRIMARY KEY CHECK(
            length(cutover_id) = 64 AND
            length(CAST(cutover_id AS BLOB)) = 64 AND
            cutover_id NOT GLOB '*[^0-9a-f]*'
        ),
        install_transaction_id TEXT NOT NULL UNIQUE CHECK(
            install_transaction_id =
                '_business-cutover-install:' || cutover_id
        ),
        install_manifest_sha256 BLOB NOT NULL CHECK(
            typeof(install_manifest_sha256) = 'blob' AND
            length(install_manifest_sha256) = 32
        ),
        source_transaction_id TEXT NOT NULL CHECK(
            source_transaction_id =
                '_legacy-repository-seal:' || cutover_id
        ),
        source_receipt_kind TEXT NOT NULL CHECK(
            source_receipt_kind = '_legacy_repository_seal_v1'
        ),
        source_admission_revision INTEGER NOT NULL CHECK(
            source_admission_revision BETWEEN 1 AND 9223372036854775807
        ),
        source_proof_manifest_sha256 BLOB NOT NULL CHECK(
            typeof(source_proof_manifest_sha256) = 'blob' AND
            length(source_proof_manifest_sha256) = 32
        ),
        source_proof_sha256 BLOB NOT NULL CHECK(
            typeof(source_proof_sha256) = 'blob' AND
            length(source_proof_sha256) = 32
        ),
        source_admission_sha256 BLOB NOT NULL CHECK(
            typeof(source_admission_sha256) = 'blob' AND
            length(source_admission_sha256) = 32
        ),
        coverage_transaction_id TEXT NOT NULL CHECK(
            coverage_transaction_id =
                '_business-cutover-asset-coverage:' || cutover_id
        ),
        coverage_receipt_kind TEXT NOT NULL CHECK(
            coverage_receipt_kind =
                '_business_cutover_asset_coverage_v1'
        ),
        coverage_manifest_sha256 BLOB NOT NULL CHECK(
            typeof(coverage_manifest_sha256) = 'blob' AND
            length(coverage_manifest_sha256) = 32
        ),
        coverage_sha256 BLOB NOT NULL CHECK(
            typeof(coverage_sha256) = 'blob' AND
            length(coverage_sha256) = 32
        ),
        committed_at TEXT NOT NULL CHECK(
            length(CAST(committed_at AS BLOB)) = 24 AND
            strftime('%Y-%m-%dT%H:%M:%fZ', committed_at) = committed_at
        ),
        member_mapping_kind TEXT NOT NULL CHECK(
            member_mapping_kind = 'member_mapping'
        ),
        member_mapping_sha256 BLOB NOT NULL CHECK(
            typeof(member_mapping_sha256) = 'blob' AND
            length(member_mapping_sha256) = 32
        ),
        baseline_projection_kind TEXT NOT NULL CHECK(
            baseline_projection_kind = 'baseline_projection'
        ),
        baseline_projection_sha256 BLOB NOT NULL CHECK(
            typeof(baseline_projection_sha256) = 'blob' AND
            length(baseline_projection_sha256) = 32
        ),
        project_count INTEGER NOT NULL CHECK(
            project_count BETWEEN 0 AND 10000
        ),
        live_project_count INTEGER NOT NULL CHECK(
            live_project_count BETWEEN 0 AND 10000
        ),
        deleted_project_count INTEGER NOT NULL CHECK(
            deleted_project_count BETWEEN 0 AND 10000
        ),
        baseline_commit_count INTEGER NOT NULL CHECK(
            baseline_commit_count = live_project_count
        ),
        baseline_outer_receipt_root_sha256 BLOB NOT NULL CHECK(
            typeof(baseline_outer_receipt_root_sha256) = 'blob' AND
            length(baseline_outer_receipt_root_sha256) = 32
        ),
        manifest_bytes BLOB NOT NULL CHECK(
            typeof(manifest_bytes) = 'blob' AND
            length(manifest_bytes) BETWEEN 1 AND
                {MAX_BUSINESS_SMALL_PAYLOAD_BYTES}
        ),
        manifest_sha256 BLOB NOT NULL UNIQUE CHECK(
            typeof(manifest_sha256) = 'blob' AND
            length(manifest_sha256) = 32
        ),
        CHECK(project_count = live_project_count + deleted_project_count),
        UNIQUE(cutover_id, manifest_sha256),
        UNIQUE(cutover_id, install_transaction_id, manifest_sha256),
        FOREIGN KEY(
            cutover_id, install_transaction_id, install_manifest_sha256
        ) REFERENCES business_cutover_install_receipts(
            cutover_id, transaction_id, manifest_sha256
        ) ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY(
            cutover_id, source_transaction_id, source_receipt_kind,
            source_admission_revision, source_proof_manifest_sha256,
            source_proof_sha256
        ) REFERENCES legacy_repository_seals(
            cutover_id, transaction_id, receipt_kind,
            repository_revision, manifest_sha256, proof_sha256
        ) ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY(
            cutover_id, coverage_transaction_id, coverage_receipt_kind,
            source_admission_revision, source_admission_sha256,
            coverage_manifest_sha256, coverage_sha256
        ) REFERENCES business_asset_coverage_receipts(
            cutover_id, transaction_id, receipt_kind,
            source_admission_revision, source_admission_sha256,
            manifest_sha256, coverage_sha256
        ) ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY(
            cutover_id, member_mapping_kind, member_mapping_sha256
        ) REFERENCES business_cutover_install_artifacts(
            cutover_id, artifact_kind, artifact_sha256
        ) ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY(
            cutover_id, baseline_projection_kind,
            baseline_projection_sha256
        ) REFERENCES business_cutover_install_artifacts(
            cutover_id, artifact_kind, artifact_sha256
        ) ON UPDATE RESTRICT ON DELETE RESTRICT
    ) STRICT
"""

_SCHEMA_V12_ARTIFACT_CHUNK_BINDING_TRIGGER = f"""
    CREATE TRIGGER business_cutover_install_artifact_chunks_binding_guard
    BEFORE INSERT ON business_cutover_install_artifact_chunks
    WHEN NOT EXISTS (
             SELECT 1 FROM business_cutover_install_artifacts AS artifact
             WHERE artifact.cutover_id = NEW.cutover_id AND
                   artifact.artifact_kind = NEW.artifact_kind
         ) OR NEW.ordinal != COALESCE((
             SELECT max(existing.ordinal) + 1
             FROM business_cutover_install_artifact_chunks AS existing
             WHERE existing.cutover_id = NEW.cutover_id AND
                   existing.artifact_kind = NEW.artifact_kind
         ), 0) OR NEW.ordinal >= (
             SELECT artifact.chunk_count
             FROM business_cutover_install_artifacts AS artifact
             WHERE artifact.cutover_id = NEW.cutover_id AND
                   artifact.artifact_kind = NEW.artifact_kind
         ) OR length(NEW.chunk_bytes) != (
             SELECT CASE
                 WHEN NEW.ordinal + 1 < artifact.chunk_count
                 THEN {MAX_BUSINESS_CUTOVER_ARTIFACT_CHUNK_BYTES}
                 ELSE artifact.byte_length - NEW.byte_offset
             END
             FROM business_cutover_install_artifacts AS artifact
             WHERE artifact.cutover_id = NEW.cutover_id AND
                   artifact.artifact_kind = NEW.artifact_kind
         )
    BEGIN SELECT RAISE(ABORT,
        'production_cutover_artifact_chunk_invalid'); END
"""

_SCHEMA_V12_BASELINE_ATOMIC_BINDING_TRIGGER = """
    CREATE TRIGGER business_cutover_baseline_atomic_binding_guard
    BEFORE INSERT ON business_cutover_baseline_atomic_receipts
    WHEN NOT EXISTS (
             SELECT 1
             FROM business_commits AS business_commit
             WHERE business_commit.commit_id = NEW.commit_id AND
                   business_commit.transaction_id = NEW.transaction_id AND
                   business_commit.receipt_kind = NEW.receipt_kind AND
                   business_commit.committed_at = NEW.committed_at AND
                   business_commit.cutover_id = NEW.cutover_id AND
                   business_commit.repository_revision =
                        NEW.repository_revision AND
                   business_commit.commit_kind = 'baseline' AND
                   business_commit.previous_commit_id IS NULL AND
                   business_commit.operation_count = 0 AND
                   business_commit.operation_target_count = 0 AND
                   business_commit.outbox_event_count = 0 AND
                   business_commit.durable_action_count = 0 AND
                   business_commit.pipeline_intent_count = 0 AND
                   business_commit.manifest_sha256 =
                        NEW.atomic_manifest_sha256
         ) OR NOT EXISTS (
             SELECT 1
             FROM business_cutover_install_receipts AS install
             WHERE install.cutover_id = NEW.cutover_id AND
                   install.transaction_id = NEW.install_transaction_id AND
                   install.manifest_sha256 = NEW.install_manifest_sha256 AND
                   install.committed_at = NEW.committed_at
         ) OR NOT EXISTS (
             SELECT 1
             FROM business_cutover_install_artifacts AS artifact
             WHERE artifact.cutover_id = NEW.cutover_id AND
                   artifact.artifact_kind =
                        NEW.baseline_projection_kind AND
                   artifact.artifact_sha256 =
                        NEW.baseline_projection_sha256
         ) OR NOT EXISTS (
             SELECT 1
             FROM business_cutover_install_artifacts AS artifact
             WHERE artifact.cutover_id = NEW.cutover_id AND
                   artifact.artifact_kind = NEW.member_mapping_kind AND
                   artifact.artifact_sha256 = NEW.member_mapping_sha256
         ) OR NOT EXISTS (
             SELECT 1
             FROM atomic_business_write_receipts AS atomic_receipt
             WHERE atomic_receipt.commit_id = NEW.commit_id AND
                   atomic_receipt.transaction_id = NEW.transaction_id AND
                   atomic_receipt.receipt_kind = NEW.receipt_kind AND
                   atomic_receipt.committed_at = NEW.committed_at AND
                   atomic_receipt.atomic_manifest_sha256 =
                        NEW.atomic_manifest_sha256 AND
                   atomic_receipt.pipeline_intents_sha256 =
                        NEW.pipeline_intents_sha256 AND
                   atomic_receipt.controls_sha256 = NEW.controls_sha256 AND
                   atomic_receipt.control_count = NEW.control_count AND
                   atomic_receipt.before_worker_sequence =
                        NEW.before_worker_sequence AND
                   atomic_receipt.before_worker_tail_sha256 =
                        NEW.before_worker_tail_sha256 AND
                   atomic_receipt.after_worker_sequence =
                        NEW.after_worker_sequence AND
                   atomic_receipt.after_worker_tail_sha256 =
                        NEW.after_worker_tail_sha256 AND
                   atomic_receipt.manifest_bytes = NEW.manifest_bytes AND
                   atomic_receipt.manifest_sha256 = NEW.manifest_sha256 AND
                   atomic_receipt.receipt_sha256 = NEW.receipt_sha256 AND
                   atomic_receipt.production_manifest_bytes =
                        NEW.baseline_outer_manifest_bytes AND
                   atomic_receipt.production_manifest_sha256 =
                        NEW.baseline_outer_manifest_sha256 AND
                   atomic_receipt.target_count = NEW.target_count AND
                   atomic_receipt.targets_sha256 = NEW.targets_sha256 AND
                   atomic_receipt.production_receipt_sha256 =
                        NEW.baseline_outer_receipt_sha256
         )
    BEGIN SELECT RAISE(ABORT,
        'production_cutover_baseline_atomic_binding_invalid'); END
"""

# A closure is useful only if it freezes the whole materialized business
# namespace, not merely the four V12 evidence tables.  Phase evidence and the
# runtime/head authority switch remain deliberately outside this list: they
# are the only writes that are expected between ``installed`` and ``mounted``.
_SCHEMA_V12_PREMOUNT_FROZEN_TABLES = (
    "projects",
    "documents",
    "entities",
    "document_commit_heads",
    "business_operations",
    "business_operation_targets",
    "idempotency_inbox",
    "event_outbox",
    "event_outbox_dispatch_control",
    "confirmation_requests",
    "deferred_actions",
    "deferred_action_attempts",
    "pipeline_jobs",
    "pipeline_job_attempts",
    "pipeline_job_transitions",
    "asset_blobs",
    "document_asset_refs",
    "atomic_business_write_receipts",
    "atomic_business_control_members",
    "worker_ledger_head",
    "worker_ledger_entries",
    "worker_execution_receipts",
    "pipeline_control_intents",
    "pipeline_control_receipts",
    "worker_targets",
    "worker_target_current",
    "worker_attempts",
    "worker_effects",
    "worker_results",
    "worker_reconcile_proofs",
    "outbox_allocations",
    "outbox_allocation_head",
    "outbox_dispatch_head",
    "worker_effect_request_projections",
    "worker_result_payload_projections",
    "worker_proof_payload_projections",
)

# These tables have no legal install-baseline members.  Requiring the empty
# complement at closure prevents a writer from smuggling ordinary operations,
# asynchronous work, or worker history into what is claimed to be a baseline.
_SCHEMA_V12_CLOSURE_EMPTY_TABLES = (
    "business_operations",
    "business_operation_targets",
    "idempotency_inbox",
    "event_outbox",
    "event_outbox_dispatch_control",
    "confirmation_requests",
    "deferred_actions",
    "deferred_action_attempts",
    "pipeline_jobs",
    "pipeline_job_attempts",
    "pipeline_job_transitions",
    "atomic_business_control_members",
    "worker_ledger_entries",
    "worker_execution_receipts",
    "pipeline_control_intents",
    "pipeline_control_receipts",
    "worker_targets",
    "worker_target_current",
    "worker_attempts",
    "worker_effects",
    "worker_results",
    "worker_reconcile_proofs",
    "outbox_allocations",
    "worker_effect_request_projections",
    "worker_result_payload_projections",
    "worker_proof_payload_projections",
)


def _schema_v12_any_rows_predicate(tables: Tuple[str, ...]) -> str:
    if (
        not tables
        or len(set(tables)) != len(tables)
        or any(not table.replace("_", "").isalnum() for table in tables)
    ):
        raise ValueError("schema-v12 table predicate is invalid")
    return " OR ".join(
        "EXISTS (SELECT 1 FROM {0})".format(table) for table in tables
    )


_SCHEMA_V12_CLOSURE_EMPTY_PREDICATE = _schema_v12_any_rows_predicate(
    _SCHEMA_V12_CLOSURE_EMPTY_TABLES
)

_SCHEMA_V12_PREMOUNT_FREEZE_PREDICATE = """
    EXISTS (
        SELECT 1
        FROM business_cutover_install_closures AS closure
        WHERE NOT EXISTS (
            SELECT 1
            FROM business_cutover_head AS head
            JOIN repository_runtime_control AS runtime
              ON runtime.singleton = 1 AND
                 runtime.cutover_id = head.cutover_id AND
                 runtime.repository_epoch = head.repository_epoch AND
                 runtime.runtime_generation = head.runtime_generation AND
                 runtime.code_digest = head.code_digest
            WHERE head.singleton = 1 AND
                  head.cutover_id = closure.cutover_id AND
                  head.install_transaction_id =
                    closure.install_transaction_id AND
                  head.install_manifest_sha256 =
                    closure.install_manifest_sha256 AND
                  head.phase_revision = 5 AND
                  head.phase = 'mounted' AND
                  head.authority_state = 'active' AND
                  runtime.authority = 'sqlite' AND
                  runtime.authority_state = 'active' AND
                  runtime.source_admission_revision =
                    closure.source_admission_revision
        )
    )
"""


def _schema_v12_premount_freeze_triggers(table: str) -> Tuple[str, ...]:
    """Freeze one materialized namespace table until the mounted boundary."""

    if not table.replace("_", "").isalnum():
        raise ValueError("schema-v12 frozen table name is invalid")
    statements = []
    for operation in ("INSERT", "UPDATE", "DELETE"):
        statements.append(
            """
            CREATE TRIGGER {table}_v12_premount_{suffix}_guard
            BEFORE {operation} ON {table}
            WHEN {predicate}
            BEGIN SELECT RAISE(ABORT,
                'production_cutover_premount_namespace_frozen'); END
            """.format(
                table=table,
                operation=operation,
                suffix=operation.lower(),
                predicate=_SCHEMA_V12_PREMOUNT_FREEZE_PREDICATE,
            )
        )
    return tuple(statements)


_SCHEMA_V12_PREMOUNT_FREEZE_TRIGGERS = tuple(
    statement
    for table in _SCHEMA_V12_PREMOUNT_FROZEN_TABLES
    for statement in _schema_v12_premount_freeze_triggers(table)
)

_SCHEMA_V12_FROZEN_GENERIC_RECEIPT_KINDS = (
    "_worker_execution_v3",
    "_pipeline_business_control_v2",
    BUSINESS_TRANSACTION_KIND,
)
_SCHEMA_V12_FROZEN_GENERIC_RECEIPT_KIND_SQL = ", ".join(
    "'{0}'".format(kind) for kind in _SCHEMA_V12_FROZEN_GENERIC_RECEIPT_KINDS
)
_SCHEMA_V12_PREMOUNT_GENERIC_RECEIPT_TRIGGERS = (
    """
    CREATE TRIGGER transaction_receipts_v12_premount_insert_guard
    BEFORE INSERT ON transaction_receipts
    WHEN NEW.kind IN ({kinds}) AND {predicate}
    BEGIN SELECT RAISE(ABORT,
        'production_cutover_premount_namespace_frozen'); END
    """.format(
        kinds=_SCHEMA_V12_FROZEN_GENERIC_RECEIPT_KIND_SQL,
        predicate=_SCHEMA_V12_PREMOUNT_FREEZE_PREDICATE,
    ),
    """
    CREATE TRIGGER transaction_receipts_v12_premount_update_guard
    BEFORE UPDATE ON transaction_receipts
    WHEN (OLD.kind IN ({kinds}) OR NEW.kind IN ({kinds})) AND {predicate}
    BEGIN SELECT RAISE(ABORT,
        'production_cutover_premount_namespace_frozen'); END
    """.format(
        kinds=_SCHEMA_V12_FROZEN_GENERIC_RECEIPT_KIND_SQL,
        predicate=_SCHEMA_V12_PREMOUNT_FREEZE_PREDICATE,
    ),
    """
    CREATE TRIGGER transaction_receipts_v12_premount_delete_guard
    BEFORE DELETE ON transaction_receipts
    WHEN OLD.kind IN ({kinds}) AND {predicate}
    BEGIN SELECT RAISE(ABORT,
        'production_cutover_premount_namespace_frozen'); END
    """.format(
        kinds=_SCHEMA_V12_FROZEN_GENERIC_RECEIPT_KIND_SQL,
        predicate=_SCHEMA_V12_PREMOUNT_FREEZE_PREDICATE,
    ),
)

_SCHEMA_V12_INSTALL_RECEIPT_SINGLETON_TRIGGER = """
    CREATE TRIGGER business_cutover_install_receipts_v12_singleton_guard
    BEFORE INSERT ON business_cutover_install_receipts
    WHEN EXISTS (SELECT 1 FROM business_cutover_install_receipts) OR
         EXISTS (SELECT 1 FROM business_cutover_install_closures) OR
         EXISTS (SELECT 1 FROM business_cutover_head)
    BEGIN SELECT RAISE(ABORT,
        'production_cutover_install_namespace_not_singleton'); END
"""

_SCHEMA_V12_GENERIC_INSTALL_SINGLETON_TRIGGER = """
    CREATE TRIGGER transaction_receipts_v12_install_singleton_guard
    BEFORE INSERT ON transaction_receipts
    WHEN (NEW.kind = '_business_cutover_install_v1' OR
          substr(NEW.transaction_id, 1, 26) =
            '_business-cutover-install:') AND (
         EXISTS (
             SELECT 1 FROM transaction_receipts AS receipt
             WHERE receipt.kind = '_business_cutover_install_v1' OR
                   substr(receipt.transaction_id, 1, 26) =
                    '_business-cutover-install:'
         ) OR EXISTS (
             SELECT 1 FROM business_cutover_install_receipts
         ) OR EXISTS (
             SELECT 1 FROM business_cutover_install_closures
         ) OR EXISTS (
             SELECT 1 FROM business_cutover_head
         )
    )
    BEGIN SELECT RAISE(ABORT,
    'production_cutover_install_namespace_not_singleton'); END
"""

_SCHEMA_V12_GENERIC_INSTALL_IMMUTABILITY_TRIGGERS = (
    """
    CREATE TRIGGER transaction_receipts_v12_install_update_guard
    BEFORE UPDATE ON transaction_receipts
    WHEN OLD.kind = '_business_cutover_install_v1' OR
         NEW.kind = '_business_cutover_install_v1' OR
         substr(OLD.transaction_id, 1, 26) =
            '_business-cutover-install:' OR
         substr(NEW.transaction_id, 1, 26) =
            '_business-cutover-install:'
    BEGIN SELECT RAISE(ABORT,
        'production_cutover_install_namespace_not_singleton'); END
    """,
    """
    CREATE TRIGGER transaction_receipts_v12_install_delete_guard
    BEFORE DELETE ON transaction_receipts
    WHEN OLD.kind = '_business_cutover_install_v1' OR
         substr(OLD.transaction_id, 1, 26) =
            '_business-cutover-install:'
    BEGIN SELECT RAISE(ABORT,
        'production_cutover_install_namespace_not_singleton'); END
    """,
)

_SCHEMA_V12_GENERIC_SOURCE_RECEIPT_TRIGGERS = (
    """
    CREATE TRIGGER transaction_receipts_v12_source_timestamp_insert_guard
    BEFORE INSERT ON transaction_receipts
    WHEN (NEW.kind = '_legacy_repository_seal_v1' OR
          substr(NEW.transaction_id, 1, 24) =
            '_legacy-repository-seal:') AND (
         length(CAST(NEW.committed_at AS BLOB)) != 24 OR
         strftime('%Y-%m-%dT%H:%M:%fZ', NEW.committed_at)
            IS NOT NEW.committed_at
    )
    BEGIN SELECT RAISE(ABORT,
        'production_cutover_source_receipt_invalid'); END
    """,
    """
    CREATE TRIGGER transaction_receipts_v12_source_singleton_guard
    BEFORE INSERT ON transaction_receipts
    WHEN (NEW.kind = '_legacy_repository_seal_v1' OR
          substr(NEW.transaction_id, 1, 24) =
            '_legacy-repository-seal:') AND EXISTS (
         SELECT 1 FROM transaction_receipts AS receipt
         WHERE receipt.kind = '_legacy_repository_seal_v1' OR
               substr(receipt.transaction_id, 1, 24) =
                '_legacy-repository-seal:'
    )
    BEGIN SELECT RAISE(ABORT,
        'production_cutover_source_namespace_not_singleton'); END
    """,
    """
    CREATE TRIGGER transaction_receipts_v12_source_update_guard
    BEFORE UPDATE ON transaction_receipts
    WHEN OLD.kind = '_legacy_repository_seal_v1' OR
         NEW.kind = '_legacy_repository_seal_v1' OR
         substr(OLD.transaction_id, 1, 24) =
            '_legacy-repository-seal:' OR
         substr(NEW.transaction_id, 1, 24) =
            '_legacy-repository-seal:'
    BEGIN SELECT RAISE(ABORT,
        'production_cutover_source_namespace_not_singleton'); END
    """,
    """
    CREATE TRIGGER transaction_receipts_v12_source_delete_guard
    BEFORE DELETE ON transaction_receipts
    WHEN OLD.kind = '_legacy_repository_seal_v1' OR
         substr(OLD.transaction_id, 1, 24) =
            '_legacy-repository-seal:'
    BEGIN SELECT RAISE(ABORT,
        'production_cutover_source_namespace_not_singleton'); END
    """,
)

_SCHEMA_V12_INSTALL_CLOSURE_BINDING_TRIGGER = f"""
    CREATE TRIGGER business_cutover_install_closure_binding_guard
    BEFORE INSERT ON business_cutover_install_closures
    WHEN NOT EXISTS (
             SELECT 1
             FROM legacy_repository_seals AS source_seal
             JOIN legacy_repository_proof_control AS source_control
               ON source_control.singleton =
                    source_seal.repository_singleton AND
                  source_control.cutover_id = source_seal.cutover_id
             JOIN business_cutover_install_receipts AS install
               ON install.cutover_id = source_seal.cutover_id
             JOIN business_asset_coverage_receipts AS coverage
               ON coverage.cutover_id = source_seal.cutover_id
             JOIN transaction_receipts AS source_receipt
               ON source_receipt.transaction_id =
                    source_seal.transaction_id AND
                  source_receipt.kind = source_seal.receipt_kind
             JOIN transaction_receipts AS coverage_receipt
               ON coverage_receipt.transaction_id =
                    coverage.transaction_id AND
                  coverage_receipt.kind = coverage.receipt_kind AND
                  coverage_receipt.committed_at = coverage.committed_at
             JOIN transaction_receipts AS install_receipt
               ON install_receipt.transaction_id = install.transaction_id AND
                  install_receipt.kind = install.receipt_kind AND
                  install_receipt.committed_at = install.committed_at
             WHERE source_seal.cutover_id = NEW.cutover_id AND
                   source_seal.transaction_id =
                        NEW.source_transaction_id AND
                   source_seal.receipt_kind = NEW.source_receipt_kind AND
                   source_seal.repository_revision =
                        NEW.source_admission_revision AND
                   source_seal.manifest_sha256 =
                        NEW.source_proof_manifest_sha256 AND
                   source_seal.proof_sha256 = NEW.source_proof_sha256 AND
                   install.transaction_id = NEW.install_transaction_id AND
                   install.manifest_sha256 = NEW.install_manifest_sha256 AND
                   install.member_mapping_sha256 =
                        NEW.member_mapping_sha256 AND
                   install.committed_at = NEW.committed_at AND
                   coverage.transaction_id =
                        NEW.coverage_transaction_id AND
                   coverage.receipt_kind = NEW.coverage_receipt_kind AND
                   coverage.source_admission_revision =
                        NEW.source_admission_revision AND
                   coverage.source_admission_sha256 =
                        NEW.source_admission_sha256 AND
                   coverage.manifest_sha256 =
                        NEW.coverage_manifest_sha256 AND
                   coverage.coverage_sha256 = NEW.coverage_sha256 AND
                   source_control.repository_epoch =
                        source_seal.repository_epoch AND
                   source_seal.repository_epoch = install.repository_epoch AND
                   install.repository_epoch = coverage.repository_epoch AND
                   source_control.repository_revision =
                        NEW.source_admission_revision AND
                   source_seal.runtime_generation =
                        install.runtime_generation AND
                   source_control.runtime_generation =
                        install.runtime_generation AND
                   source_seal.code_digest = install.code_digest AND
                   source_control.code_digest = install.code_digest AND
                   source_seal.project_count = NEW.project_count AND
                   source_seal.live_project_count =
                        NEW.live_project_count AND
                   source_seal.deleted_project_count =
                        NEW.deleted_project_count AND
                   coverage.repository_member_count = NEW.project_count AND
                   coverage.live_project_count = NEW.live_project_count AND
                   coverage.deleted_project_count =
                        NEW.deleted_project_count AND
                   coverage.project_count = NEW.live_project_count AND
                   length(CAST(source_receipt.committed_at AS BLOB)) = 24 AND
                   strftime(
                        '%Y-%m-%dT%H:%M:%fZ',
                        source_receipt.committed_at
                   ) = source_receipt.committed_at AND
                   source_receipt.committed_at <=
                        coverage_receipt.committed_at AND
                   coverage_receipt.committed_at <=
                        install_receipt.committed_at
         ) OR EXISTS (
             SELECT 1 FROM business_cutover_install_closures
         ) OR EXISTS (
             SELECT 1 FROM business_cutover_head
         ) OR (
             SELECT count(*) FROM business_cutover_install_receipts
         ) != 1 OR (
             SELECT count(*)
             FROM transaction_receipts AS receipt
             WHERE receipt.kind = '_legacy_repository_seal_v1' OR
                   substr(receipt.transaction_id, 1, 24) =
                    '_legacy-repository-seal:'
         ) != 1 OR (
             SELECT count(*)
             FROM transaction_receipts AS receipt
             WHERE receipt.kind = '_business_cutover_install_v1' OR
                   substr(receipt.transaction_id, 1, 26) =
                    '_business-cutover-install:'
         ) != 1 OR NOT EXISTS (
             SELECT 1
             FROM transaction_receipts AS receipt
             WHERE receipt.transaction_id = NEW.install_transaction_id AND
                   receipt.kind = '_business_cutover_install_v1' AND
                   receipt.committed_at = NEW.committed_at
         ) OR NOT EXISTS (
             SELECT 1
             FROM repository_runtime_control AS runtime
             JOIN business_cutover_install_receipts AS install
               ON install.cutover_id = runtime.cutover_id AND
                  install.repository_epoch = runtime.repository_epoch AND
                  install.runtime_generation = runtime.runtime_generation AND
                  install.code_digest = runtime.code_digest
             WHERE runtime.singleton = 1 AND
                   runtime.authority = 'sqlite' AND
                   runtime.authority_state = 'read_only' AND
                   runtime.cutover_id = NEW.cutover_id AND
                   runtime.source_admission_revision =
                        NEW.source_admission_revision AND
                   runtime.repository_revision = CASE
                       WHEN NEW.baseline_commit_count = 0 THEN 1
                       ELSE NEW.baseline_commit_count
                   END AND
                   runtime.state_revision = 1 AND
                   runtime.schema_version = 6 AND
                   runtime.updated_at = NEW.committed_at
         ) OR NOT EXISTS (
             SELECT 1 FROM worker_ledger_head
             WHERE singleton = 1 AND state_revision = 1 AND sequence = 0 AND
                   previous_tail_sha256 = zeroblob(32) AND
                   tail_sha256 = zeroblob(32) AND
                   updated_at = NEW.committed_at
         ) OR NOT EXISTS (
             SELECT 1 FROM outbox_allocation_head
             WHERE singleton = 1 AND state_revision = 1 AND sequence = 0 AND
                   previous_tail_sha256 = zeroblob(32) AND
                   tail_sha256 = zeroblob(32) AND
                   updated_at = NEW.committed_at
         ) OR NOT EXISTS (
             SELECT 1 FROM outbox_dispatch_head
             WHERE singleton = 1 AND state_revision = 1 AND
                   published_sequence = 0 AND
                   published_repository_revision IS NULL AND
                   published_ordinal IS NULL AND
                   inflight_sequence IS NULL AND
                   inflight_repository_revision IS NULL AND
                   inflight_ordinal IS NULL AND
                   previous_head_sha256 = zeroblob(32) AND
                   head_sha256 = zeroblob(32) AND
                   updated_at = NEW.committed_at
         ) OR EXISTS (
             SELECT 1 FROM business_cutover_install_artifacts
             WHERE cutover_id != NEW.cutover_id
         ) OR EXISTS (
             SELECT 1 FROM business_cutover_install_artifact_chunks
             WHERE cutover_id != NEW.cutover_id
         ) OR EXISTS (
             SELECT 1 FROM business_cutover_baseline_atomic_receipts
             WHERE cutover_id != NEW.cutover_id
         ) OR EXISTS (
             SELECT 1
             FROM business_cutover_install_artifacts AS artifact
             WHERE artifact.cutover_id = NEW.cutover_id AND
                   artifact.artifact_kind IN (
                        NEW.member_mapping_kind,
                        NEW.baseline_projection_kind
                   ) AND (
                       (SELECT count(*)
                        FROM business_cutover_install_artifact_chunks AS chunk
                        WHERE chunk.cutover_id = artifact.cutover_id AND
                              chunk.artifact_kind = artifact.artifact_kind)
                            != artifact.chunk_count OR
                       (SELECT COALESCE(sum(length(chunk.chunk_bytes)), 0)
                        FROM business_cutover_install_artifact_chunks AS chunk
                        WHERE chunk.cutover_id = artifact.cutover_id AND
                              chunk.artifact_kind = artifact.artifact_kind)
                            != artifact.byte_length
                   )
         ) OR (
             SELECT count(*)
             FROM business_cutover_install_artifacts AS artifact
             WHERE artifact.cutover_id = NEW.cutover_id AND (
                 (artifact.artifact_kind = NEW.member_mapping_kind AND
                  artifact.artifact_sha256 = NEW.member_mapping_sha256) OR
                 (artifact.artifact_kind = NEW.baseline_projection_kind AND
                  artifact.artifact_sha256 =
                    NEW.baseline_projection_sha256)
             )
         ) != 2 OR (
             SELECT count(*)
             FROM business_cutover_baseline_atomic_receipts AS baseline
             WHERE baseline.cutover_id = NEW.cutover_id AND
                   baseline.install_transaction_id =
                        NEW.install_transaction_id AND
                   baseline.install_manifest_sha256 =
                        NEW.install_manifest_sha256 AND
                   baseline.baseline_projection_sha256 =
                        NEW.baseline_projection_sha256 AND
                   baseline.committed_at = NEW.committed_at
         ) != NEW.baseline_commit_count OR EXISTS (
             SELECT 1
             WHERE NEW.baseline_commit_count > 0 AND (
                 (SELECT min(baseline.repository_revision)
                  FROM business_cutover_baseline_atomic_receipts AS baseline
                  WHERE baseline.cutover_id = NEW.cutover_id) != 1 OR
                 (SELECT max(baseline.repository_revision)
                  FROM business_cutover_baseline_atomic_receipts AS baseline
                  WHERE baseline.cutover_id = NEW.cutover_id) !=
                    NEW.baseline_commit_count
             )
         ) OR (
             SELECT count(*)
             FROM business_commits AS business_commit
             WHERE business_commit.cutover_id = NEW.cutover_id AND
                   business_commit.commit_kind = 'baseline' AND
                   business_commit.committed_at = NEW.committed_at
         ) != NEW.baseline_commit_count OR (
             -- No commit outside the exact install baseline set may already
             -- exist.  This closes other-time baselines, changes and foreign
             -- cutover commits that the timestamp-scoped count cannot see.
             SELECT count(*) FROM business_commits
         ) != NEW.baseline_commit_count OR (
             SELECT count(*)
             FROM transaction_receipts
             WHERE kind = '{BUSINESS_TRANSACTION_KIND}'
         ) != NEW.baseline_commit_count OR (
             SELECT count(*) FROM atomic_business_write_receipts
         ) != NEW.baseline_commit_count OR
         {_SCHEMA_V12_CLOSURE_EMPTY_PREDICATE} OR (
             SELECT count(*) FROM repository_projects
         ) != NEW.project_count OR (
             SELECT count(*)
             FROM repository_projects AS project
             WHERE project.status = 'live'
         ) != NEW.live_project_count OR (
             SELECT count(*)
             FROM repository_projects AS project
             WHERE project.status = 'deleted'
         ) != NEW.deleted_project_count OR EXISTS (
             -- The install projection owns one canonical materialization
             -- instant.  Requiring every projected project row to use that
             -- instant makes the live/deleted/total counts a closed relation,
             -- rather than a count that can accidentally include an earlier
             -- or future runtime row.
             SELECT 1
             FROM repository_projects AS project
             WHERE project.status NOT IN ('live', 'deleted') OR
                   project.created_at != NEW.committed_at OR
                   project.updated_at != NEW.committed_at OR
                   (project.status = 'deleted' AND
                    project.deleted_at != NEW.committed_at)
         ) OR EXISTS (
             SELECT 1
             FROM business_commits AS business_commit
             WHERE business_commit.cutover_id = NEW.cutover_id AND
                   business_commit.commit_kind = 'baseline' AND
                   business_commit.committed_at = NEW.committed_at AND
                   NOT EXISTS (
                       SELECT 1
                       FROM business_cutover_baseline_atomic_receipts
                            AS baseline
                       WHERE baseline.cutover_id = NEW.cutover_id AND
                             baseline.commit_id = business_commit.commit_id AND
                             baseline.repository_revision =
                                business_commit.repository_revision
                   )
         ) OR EXISTS (
             SELECT 1
             FROM repository_projects AS project
             WHERE project.status = 'live' AND NOT EXISTS (
                 SELECT 1
                 FROM business_commits AS business_commit
                 WHERE business_commit.cutover_id = NEW.cutover_id AND
                       business_commit.commit_kind = 'baseline' AND
                       business_commit.committed_at = NEW.committed_at AND
                       business_commit.project_id = project.project_id AND
                       business_commit.generation = project.generation
             )
         ) OR EXISTS (
             SELECT 1
             FROM business_commits AS business_commit
             WHERE business_commit.cutover_id = NEW.cutover_id AND
                   business_commit.commit_kind = 'baseline' AND
                   business_commit.committed_at = NEW.committed_at AND
                   NOT EXISTS (
                       SELECT 1
                       FROM repository_projects AS project
                       WHERE project.project_id =
                                business_commit.project_id AND
                             project.generation =
                                business_commit.generation AND
                             project.status = 'live'
                   )
         ) OR EXISTS (
             SELECT 1
             FROM business_cutover_baseline_atomic_receipts AS baseline
             WHERE baseline.cutover_id = NEW.cutover_id AND (
                 baseline.install_transaction_id !=
                    NEW.install_transaction_id OR
                 baseline.install_manifest_sha256 !=
                    NEW.install_manifest_sha256 OR
                 baseline.baseline_projection_sha256 !=
                    NEW.baseline_projection_sha256 OR
                 baseline.committed_at != NEW.committed_at
             )
         )
    BEGIN SELECT RAISE(ABORT,
        'production_cutover_install_closure_invalid'); END
"""

_SCHEMA_V12_HEAD_INSTALL_CLOSURE_TRIGGER = """
    CREATE TRIGGER business_cutover_head_v12_install_closure_guard
    BEFORE INSERT ON business_cutover_head
    WHEN NOT EXISTS (
        SELECT 1
        FROM business_cutover_install_closures AS closure
        WHERE closure.cutover_id = NEW.cutover_id AND
              closure.install_transaction_id =
                NEW.install_transaction_id AND
              closure.install_manifest_sha256 =
                NEW.install_manifest_sha256
    )
    BEGIN SELECT RAISE(ABORT,
        'production_cutover_install_closure_invalid'); END
"""

_SCHEMA_V12_INSTALL_CLOSED_TRIGGERS = (
    """
    CREATE TRIGGER business_cutover_baseline_commits_closed_guard
    BEFORE INSERT ON business_commits
    WHEN EXISTS (
         SELECT 1
         FROM business_cutover_install_closures AS closure
         WHERE NOT EXISTS (
             -- Every post-closure commit, baseline or change, is a mounted
             -- runtime transaction.  It must consume exactly the next live
             -- repository revision; a commit cannot reuse the empty-shell
             -- revision or reserve a distant future revision.
             SELECT 1
             FROM business_cutover_head AS head
             JOIN repository_runtime_control AS runtime
               ON runtime.singleton = 1 AND
                  runtime.cutover_id = head.cutover_id AND
                  runtime.repository_epoch = head.repository_epoch AND
                  runtime.runtime_generation = head.runtime_generation AND
                  runtime.code_digest = head.code_digest
             WHERE head.singleton = 1 AND
                   head.cutover_id = closure.cutover_id AND
                   head.install_transaction_id =
                        closure.install_transaction_id AND
                   head.install_manifest_sha256 =
                        closure.install_manifest_sha256 AND
                   head.phase_revision = 5 AND
                   head.phase = 'mounted' AND
                   head.authority_state = 'active' AND
                   runtime.authority = 'sqlite' AND
                   runtime.authority_state = 'active' AND
                   runtime.repository_revision < 9223372036854775807 AND
                   NEW.cutover_id = runtime.cutover_id AND
                   NEW.repository_epoch = runtime.repository_epoch AND
                   NEW.source_admission_revision =
                        runtime.source_admission_revision AND
                   NEW.repository_revision =
                        runtime.repository_revision + 1 AND
                   NEW.committed_at > head.updated_at
         )
    )
    BEGIN SELECT RAISE(ABORT,
        'production_cutover_install_already_closed'); END
    """,
    """
    CREATE TRIGGER repository_projects_v12_cutover_namespace_insert_guard
    BEFORE INSERT ON repository_projects
    WHEN EXISTS (
         SELECT 1
         FROM business_cutover_install_closures AS closure
         WHERE NEW.status != 'live' OR
               NEW.created_at != NEW.updated_at OR
               NEW.created_at <= closure.committed_at OR
               NOT EXISTS (
                   -- Direct INSERT is creation, never deletion.  A project
                   -- created after cutover must be backed by its own later
                   -- baseline and must cross the mounted-head time boundary.
                   SELECT 1
                   FROM business_cutover_head AS head
                   WHERE head.singleton = 1 AND
                         head.cutover_id = closure.cutover_id AND
                         head.install_transaction_id =
                            closure.install_transaction_id AND
                         head.install_manifest_sha256 =
                            closure.install_manifest_sha256 AND
                         head.phase_revision = 5 AND
                         head.phase = 'mounted' AND
                         head.authority_state = 'active' AND
                         NEW.created_at > head.updated_at
               ) OR NOT EXISTS (
                   SELECT 1
                   FROM business_commits AS business_commit
                   JOIN repository_runtime_control AS runtime
                     ON runtime.singleton = 1 AND
                        runtime.cutover_id = business_commit.cutover_id AND
                        runtime.repository_epoch =
                            business_commit.repository_epoch AND
                        runtime.source_admission_revision =
                            business_commit.source_admission_revision
                   WHERE business_commit.cutover_id = closure.cutover_id AND
                         business_commit.project_id = NEW.project_id AND
                         business_commit.generation = NEW.generation AND
                         business_commit.project_revision =
                            NEW.project_revision AND
                         business_commit.after_root_sha256 =
                            NEW.current_snapshot_sha256 AND
                         business_commit.commit_kind = 'baseline' AND
                         business_commit.previous_commit_id IS NULL AND
                         business_commit.repository_revision >
                            closure.baseline_commit_count AND
                         runtime.authority = 'sqlite' AND
                         runtime.authority_state = 'active' AND
                         runtime.repository_revision < 9223372036854775807 AND
                         business_commit.repository_revision =
                            runtime.repository_revision + 1 AND
                         business_commit.committed_at = NEW.created_at
               )
    )
    BEGIN SELECT RAISE(ABORT,
        'production_cutover_project_namespace_invalid'); END
    """,
    """
    CREATE TRIGGER repository_projects_v12_cutover_namespace_update_guard
    BEFORE UPDATE ON repository_projects
    WHEN EXISTS (
         SELECT 1
         FROM business_cutover_install_closures AS closure
         WHERE NEW.updated_at <= closure.committed_at OR
               NOT EXISTS (
                   -- Before mount, the closed install relation is immutable.
                   -- After mount, ordinary revision/status transitions remain
                   -- legal only when their timestamp crosses the mount head.
                   SELECT 1
                   FROM business_cutover_head AS head
                   WHERE head.singleton = 1 AND
                         head.cutover_id = closure.cutover_id AND
                         head.install_transaction_id =
                            closure.install_transaction_id AND
                         head.install_manifest_sha256 =
                            closure.install_manifest_sha256 AND
                         head.phase_revision = 5 AND
                         head.phase = 'mounted' AND
                         head.authority_state = 'active' AND
                         NEW.updated_at > head.updated_at
               )
    )
    BEGIN SELECT RAISE(ABORT,
        'production_cutover_project_namespace_invalid'); END
    """,
    """
    CREATE TRIGGER business_cutover_install_artifacts_closed_guard
    BEFORE INSERT ON business_cutover_install_artifacts
    WHEN EXISTS (
             SELECT 1 FROM business_cutover_install_closures
         ) OR EXISTS (
             SELECT 1 FROM business_cutover_head
         )
    BEGIN SELECT RAISE(ABORT,
        'production_cutover_install_already_closed'); END
    """,
    """
    CREATE TRIGGER business_cutover_install_artifact_chunks_closed_guard
    BEFORE INSERT ON business_cutover_install_artifact_chunks
    WHEN EXISTS (
             SELECT 1 FROM business_cutover_install_closures
         ) OR EXISTS (
             SELECT 1 FROM business_cutover_head
         )
    BEGIN SELECT RAISE(ABORT,
        'production_cutover_install_already_closed'); END
    """,
    """
    CREATE TRIGGER business_cutover_baseline_atomic_receipts_closed_guard
    BEFORE INSERT ON business_cutover_baseline_atomic_receipts
    WHEN EXISTS (
             SELECT 1 FROM business_cutover_install_closures
         ) OR EXISTS (
             SELECT 1 FROM business_cutover_head
         )
    BEGIN SELECT RAISE(ABORT,
        'production_cutover_install_already_closed'); END
    """,
)


INSTALL_CLOSURE_MIGRATION = Migration(
    version=12,
    name="cutover_install_closure_v1",
    statements=(
        """
        CREATE TABLE schema_v12_install_closure_preflight (
            verified INTEGER NOT NULL
                CONSTRAINT schema_v12_install_closure_precondition
                CHECK(verified = 1)
        ) STRICT
        """,
        "INSERT INTO schema_v12_install_closure_preflight(verified) "
        "SELECT CASE WHEN " +
        SCHEMA_V12_INSTALL_CLOSURE_PREFLIGHT_PREDICATE +
        " THEN 1 ELSE 0 END",
        _SCHEMA_V12_INSTALL_ARTIFACTS_TABLE,
        _SCHEMA_V12_INSTALL_ARTIFACT_CHUNKS_TABLE,
        _SCHEMA_V12_ATOMIC_BASELINE_POINTER_INDEX,
        _SCHEMA_V12_SOURCE_PROOF_POINTER_INDEX,
        _SCHEMA_V12_COVERAGE_POINTER_INDEX,
        _SCHEMA_V12_BASELINE_ATOMIC_RECEIPTS_TABLE,
        _SCHEMA_V12_INSTALL_CLOSURES_TABLE,
        _SCHEMA_V12_GENERIC_INSTALL_SINGLETON_TRIGGER,
        *_SCHEMA_V12_GENERIC_INSTALL_IMMUTABILITY_TRIGGERS,
        *_SCHEMA_V12_GENERIC_SOURCE_RECEIPT_TRIGGERS,
        _SCHEMA_V12_INSTALL_RECEIPT_SINGLETON_TRIGGER,
        _SCHEMA_V12_ARTIFACT_CHUNK_BINDING_TRIGGER,
        _SCHEMA_V12_BASELINE_ATOMIC_BINDING_TRIGGER,
        _SCHEMA_V12_INSTALL_CLOSURE_BINDING_TRIGGER,
        _SCHEMA_V12_HEAD_INSTALL_CLOSURE_TRIGGER,
        *_SCHEMA_V12_INSTALL_CLOSED_TRIGGERS,
        *_SCHEMA_V12_PREMOUNT_GENERIC_RECEIPT_TRIGGERS,
        *_SCHEMA_V12_PREMOUNT_FREEZE_TRIGGERS,
        *_production_append_only_triggers(
            "business_cutover_install_artifacts"
        ),
        *_production_append_only_triggers(
            "business_cutover_install_artifact_chunks"
        ),
        *_production_append_only_triggers(
            "business_cutover_baseline_atomic_receipts"
        ),
        *_production_append_only_triggers(
            "business_cutover_install_closures"
        ),
        *_schema_v10_timestamp_triggers(
            "business_cutover_baseline_atomic_receipts",
            ("committed_at",),
            (),
            mutable=False,
        ),
        *_schema_v10_timestamp_triggers(
            "business_cutover_install_closures",
            ("committed_at",),
            (),
            mutable=False,
        ),
        "DROP TABLE schema_v12_install_closure_preflight",
    ),
)


# Schema-v13 closes the last trust gap in the five-step cutover head.  Earlier
# schemas made each phase evidence row durable, but the mutable singleton head
# could not prove that the writer had observed the exact predecessor and the
# complete same-transaction repository high-water snapshot.  These four
# append-only sidecars make every transition a direct predecessor binding.
# V1-V12 remain byte-for-byte immutable; installation is allowed only over an
# authentic, production-empty V12 database under legacy authority.
_SCHEMA_V13_FROZEN_V12_REGISTRY = (
    KERNEL_MIGRATION,
    BUSINESS_SNAPSHOT_MIGRATION,
    LEGACY_IMPORT_LEDGER_MIGRATION,
    LEGACY_CATALOG_SEAL_MIGRATION,
    LEGACY_REPOSITORY_PROOF_MIGRATION,
    BUSINESS_TRANSACTION_MIGRATION,
    LOCAL_REVISION_MIGRATION,
    PRODUCTION_AUTHORITY_WORKER_MIGRATION,
    WORKER_PAYLOAD_PROJECTION_MIGRATION,
    RUNTIME_SCHEMA_HARDENING_MIGRATION,
    MOUNT_CHRONOLOGY_MIGRATION,
    INSTALL_CLOSURE_MIGRATION,
)

_SCHEMA_V13_FROZEN_V12_PRODUCTION_TABLES = (
    *_SCHEMA_V11_PRODUCTION_NAMESPACE_TABLES,
    "business_cutover_install_artifacts",
    "business_cutover_install_artifact_chunks",
    "business_cutover_baseline_atomic_receipts",
    "business_cutover_install_closures",
)


def _schema_v13_preflight_predicate() -> str:
    ledger = ["(SELECT count(*) FROM schema_migrations) = 12"]
    ledger.extend(
        "EXISTS (SELECT 1 FROM schema_migrations WHERE version={0} "
        "AND name='{1}' AND checksum='{2}')".format(
            migration.version,
            migration.name,
            migration.checksum,
        )
        for migration in _SCHEMA_V13_FROZEN_V12_REGISTRY
    )
    empty = [
        "NOT EXISTS (SELECT 1 FROM {0})".format(table)
        for table in _SCHEMA_V13_FROZEN_V12_PRODUCTION_TABLES
    ]
    authentic_v12 = "(" + " AND ".join(
        [
            "(SELECT count(*) FROM kernel_repository) = 1",
            "EXISTS (SELECT 1 FROM kernel_repository "
            "WHERE singleton = 1 AND authority = 'legacy')",
            *ledger,
        ]
    ) + ")"
    # The empty branch exists only for the kernel's isolated deterministic
    # schema-fingerprint replay, which executes DDL without ledger inserts.
    pristine_replay = (
        "(NOT EXISTS (SELECT 1 FROM kernel_repository) AND "
        "NOT EXISTS (SELECT 1 FROM schema_migrations))"
    )
    return "(\n    " + " AND\n    ".join(
        [
            PRODUCTION_RUNTIME_PREFLIGHT_PREDICATE,
            "(" + pristine_replay + " OR " + authentic_v12 + ")",
            *empty,
        ]
    ) + "\n)"


SCHEMA_V13_DIRECT_PREDECESSOR_PREFLIGHT_PREDICATE = (
    _schema_v13_preflight_predicate()
)


def _schema_v13_binding_table(
    table: str,
    *,
    predecessor_revision: int,
    predecessor_phase: str,
    predecessor_authority: str,
    successor_revision: int,
    successor_phase: str,
    successor_authority: str,
    file_admission_state: str,
    file_admission_revision_delta: int,
    receipt_delta: int,
    successor_time_check: str,
    specialised_columns: str,
    specialised_checks: str,
    specialised_foreign_keys: str,
) -> str:
    """Build one exact STRICT direct-predecessor sidecar table."""

    if not table.replace("_", "").isalnum():
        raise ValueError("binding table name is invalid")
    return f"""
    CREATE TABLE {table} (
        cutover_id TEXT NOT NULL PRIMARY KEY CHECK(
            length(cutover_id) = 64 AND
            length(CAST(cutover_id AS BLOB)) = 64 AND
            cutover_id NOT GLOB '*[^0-9a-f]*'
        ),
        repository_epoch TEXT NOT NULL CHECK(
            length(repository_epoch) = 64 AND
            length(CAST(repository_epoch AS BLOB)) = 64 AND
            repository_epoch NOT GLOB '*[^0-9a-f]*'
        ),
        runtime_generation TEXT NOT NULL CHECK(
            length(CAST(runtime_generation AS BLOB)) BETWEEN 1 AND
                {MAX_BUSINESS_RUNTIME_GENERATION_BYTES}
        ),
        code_digest BLOB NOT NULL CHECK(
            typeof(code_digest) = 'blob' AND length(code_digest) = 32
        ),
        predecessor_state_revision INTEGER NOT NULL CHECK(
            predecessor_state_revision = {predecessor_revision}
        ),
        predecessor_phase_revision INTEGER NOT NULL CHECK(
            predecessor_phase_revision = {predecessor_revision}
        ),
        predecessor_phase TEXT NOT NULL CHECK(
            predecessor_phase = '{predecessor_phase}'
        ),
        predecessor_authority_state TEXT NOT NULL CHECK(
            predecessor_authority_state = '{predecessor_authority}'
        ),
        predecessor_head_sha256 BLOB NOT NULL CHECK(
            typeof(predecessor_head_sha256) = 'blob' AND
            length(predecessor_head_sha256) = 32 AND
            predecessor_head_sha256 != zeroblob(32)
        ),
        predecessor_previous_head_sha256 BLOB NOT NULL CHECK(
            typeof(predecessor_previous_head_sha256) = 'blob' AND
            length(predecessor_previous_head_sha256) = 32
        ),
        predecessor_updated_at TEXT NOT NULL CHECK(
            length(CAST(predecessor_updated_at AS BLOB)) = 24 AND
            strftime('%Y-%m-%dT%H:%M:%fZ', predecessor_updated_at) IS
                predecessor_updated_at
        ),
        successor_state_revision INTEGER NOT NULL CHECK(
            successor_state_revision = {successor_revision}
        ),
        successor_phase_revision INTEGER NOT NULL CHECK(
            successor_phase_revision = {successor_revision}
        ),
        successor_phase TEXT NOT NULL CHECK(
            successor_phase = '{successor_phase}'
        ),
        successor_authority_state TEXT NOT NULL CHECK(
            successor_authority_state = '{successor_authority}'
        ),
        successor_previous_head_sha256 BLOB NOT NULL CHECK(
            typeof(successor_previous_head_sha256) = 'blob' AND
            length(successor_previous_head_sha256) = 32
        ),
        successor_head_sha256 BLOB NOT NULL UNIQUE CHECK(
            typeof(successor_head_sha256) = 'blob' AND
            length(successor_head_sha256) = 32 AND
            successor_head_sha256 != zeroblob(32)
        ),
        successor_updated_at TEXT NOT NULL CHECK(
            length(CAST(successor_updated_at AS BLOB)) = 24 AND
            strftime('%Y-%m-%dT%H:%M:%fZ', successor_updated_at) IS
                successor_updated_at
        ),
        source_admission_revision INTEGER NOT NULL CHECK(
            source_admission_revision BETWEEN 1 AND 9223372036854775805
        ),
        source_admission_sha256 BLOB NOT NULL CHECK(
            typeof(source_admission_sha256) = 'blob' AND
            length(source_admission_sha256) = 32
        ),
        source_freeze_sha256 BLOB NOT NULL CHECK(
            typeof(source_freeze_sha256) = 'blob' AND
            length(source_freeze_sha256) = 32
        ),
        file_admission_state TEXT NOT NULL CHECK(
            file_admission_state = '{file_admission_state}'
        ),
        file_admission_epoch TEXT NOT NULL CHECK(
            length(file_admission_epoch) = 64 AND
            length(CAST(file_admission_epoch AS BLOB)) = 64 AND
            file_admission_epoch NOT GLOB '*[^0-9a-f]*'
        ),
        file_admission_revision INTEGER NOT NULL CHECK(
            file_admission_revision =
                source_admission_revision + {file_admission_revision_delta}
        ),
        file_admission_cutover_id TEXT NOT NULL CHECK(
            file_admission_cutover_id = cutover_id
        ),
        file_admission_sha256 BLOB NOT NULL CHECK(
            typeof(file_admission_sha256) = 'blob' AND
            length(file_admission_sha256) = 32
        ),
        maintenance_scope TEXT NOT NULL CHECK(
            maintenance_scope = 'catalog_ex_repository_ex'
        ),
        maintenance_repository_proof_sha256 BLOB NOT NULL CHECK(
            typeof(maintenance_repository_proof_sha256) = 'blob' AND
            length(maintenance_repository_proof_sha256) = 32
        ),
        maintenance_observed_at TEXT NOT NULL CHECK(
            length(CAST(maintenance_observed_at AS BLOB)) = 24 AND
            strftime('%Y-%m-%dT%H:%M:%fZ', maintenance_observed_at) IS
                maintenance_observed_at
        ),
        runtime_authority TEXT NOT NULL CHECK(runtime_authority = 'sqlite'),
        runtime_authority_state TEXT NOT NULL CHECK(
            runtime_authority_state = '{predecessor_authority}'
        ),
        runtime_repository_revision INTEGER NOT NULL CHECK(
            runtime_repository_revision BETWEEN 1 AND 9223372036854775807
        ),
        runtime_state_revision INTEGER NOT NULL CHECK(
            runtime_state_revision BETWEEN 1 AND 9223372036854775807
        ),
        runtime_schema_version INTEGER NOT NULL CHECK(
            runtime_schema_version = 6
        ),
        runtime_updated_at TEXT NOT NULL CHECK(
            length(CAST(runtime_updated_at AS BLOB)) = 24 AND
            strftime('%Y-%m-%dT%H:%M:%fZ', runtime_updated_at) IS
                runtime_updated_at
        ),
        before_receipt_count INTEGER NOT NULL CHECK(
            before_receipt_count BETWEEN 0 AND 9223372036854775807
        ),
        before_receipt_root_sha256 BLOB NOT NULL CHECK(
            typeof(before_receipt_root_sha256) = 'blob' AND
            length(before_receipt_root_sha256) = 32
        ),
        after_receipt_count INTEGER NOT NULL CHECK(
            after_receipt_count = before_receipt_count + {receipt_delta}
        ),
        after_receipt_root_sha256 BLOB NOT NULL CHECK(
            typeof(after_receipt_root_sha256) = 'blob' AND
            length(after_receipt_root_sha256) = 32
        ),
        worker_head_state_revision INTEGER NOT NULL CHECK(
            worker_head_state_revision = 1
        ),
        worker_ledger_sequence INTEGER NOT NULL CHECK(worker_ledger_sequence = 0),
        worker_ledger_tail_sha256 BLOB NOT NULL CHECK(
            worker_ledger_tail_sha256 = zeroblob(32)
        ),
        worker_head_updated_at TEXT NOT NULL CHECK(
            length(CAST(worker_head_updated_at AS BLOB)) = 24 AND
            strftime('%Y-%m-%dT%H:%M:%fZ', worker_head_updated_at) IS
                worker_head_updated_at
        ),
        allocation_head_state_revision INTEGER NOT NULL CHECK(
            allocation_head_state_revision = 1
        ),
        allocation_sequence INTEGER NOT NULL CHECK(allocation_sequence = 0),
        allocation_tail_sha256 BLOB NOT NULL CHECK(
            allocation_tail_sha256 = zeroblob(32)
        ),
        allocation_head_updated_at TEXT NOT NULL CHECK(
            length(CAST(allocation_head_updated_at AS BLOB)) = 24 AND
            strftime('%Y-%m-%dT%H:%M:%fZ', allocation_head_updated_at) IS
                allocation_head_updated_at
        ),
        dispatch_head_state_revision INTEGER NOT NULL CHECK(
            dispatch_head_state_revision = 1
        ),
        dispatch_published_sequence INTEGER NOT NULL CHECK(
            dispatch_published_sequence = 0
        ),
        dispatch_published_repository_revision INTEGER CHECK(
            dispatch_published_repository_revision IS NULL
        ),
        dispatch_published_ordinal INTEGER CHECK(dispatch_published_ordinal IS NULL),
        dispatch_inflight_sequence INTEGER CHECK(dispatch_inflight_sequence IS NULL),
        dispatch_inflight_repository_revision INTEGER CHECK(
            dispatch_inflight_repository_revision IS NULL
        ),
        dispatch_inflight_ordinal INTEGER CHECK(dispatch_inflight_ordinal IS NULL),
        dispatch_head_sha256 BLOB NOT NULL CHECK(
            dispatch_head_sha256 = zeroblob(32)
        ),
        dispatch_head_updated_at TEXT NOT NULL CHECK(
            length(CAST(dispatch_head_updated_at AS BLOB)) = 24 AND
            strftime('%Y-%m-%dT%H:%M:%fZ', dispatch_head_updated_at) IS
                dispatch_head_updated_at
        ),
        binding_manifest_bytes BLOB NOT NULL CHECK(
            typeof(binding_manifest_bytes) = 'blob' AND
            length(binding_manifest_bytes) BETWEEN 1 AND
                {MAX_BUSINESS_SMALL_PAYLOAD_BYTES}
        ),
        binding_manifest_sha256 BLOB NOT NULL UNIQUE CHECK(
            typeof(binding_manifest_sha256) = 'blob' AND
            length(binding_manifest_sha256) = 32
        ),
        {specialised_columns},
        CHECK(successor_previous_head_sha256 = predecessor_head_sha256),
        CHECK({successor_time_check}),
        CHECK(file_admission_epoch = repository_epoch),
        CHECK(maintenance_observed_at >= predecessor_updated_at),
        CHECK(runtime_updated_at <= successor_updated_at),
        CHECK(worker_head_updated_at <= predecessor_updated_at),
        CHECK(allocation_head_updated_at <= predecessor_updated_at),
        CHECK(dispatch_head_updated_at <= predecessor_updated_at),
        CHECK(
            ({receipt_delta} = 0 AND
             after_receipt_root_sha256 = before_receipt_root_sha256) OR
            ({receipt_delta} = 1 AND
             after_receipt_root_sha256 != before_receipt_root_sha256)
        ),
        {specialised_checks},
        {specialised_foreign_keys}
    ) STRICT
    """


_SCHEMA_V13_ASSET_BINDING_TABLE = _schema_v13_binding_table(
    "business_cutover_asset_provider_bindings",
    predecessor_revision=1,
    predecessor_phase="installed",
    predecessor_authority="read_only",
    successor_revision=2,
    successor_phase="assets_covered",
    successor_authority="read_only",
    file_admission_state="cutover_pending",
    file_admission_revision_delta=1,
    receipt_delta=0,
    successor_time_check="successor_updated_at = predecessor_updated_at",
    specialised_columns=f"""
        coverage_transaction_id TEXT NOT NULL CHECK(
            coverage_transaction_id =
                '_business-cutover-asset-coverage:' || cutover_id
        ),
        coverage_receipt_kind TEXT NOT NULL CHECK(
            coverage_receipt_kind = '_business_cutover_asset_coverage_v1'
        ),
        coverage_committed_at TEXT NOT NULL CHECK(
            length(CAST(coverage_committed_at AS BLOB)) = 24 AND
            strftime('%Y-%m-%dT%H:%M:%fZ', coverage_committed_at) IS
                coverage_committed_at
        ),
        coverage_manifest_sha256 BLOB NOT NULL CHECK(
            typeof(coverage_manifest_sha256) = 'blob' AND
            length(coverage_manifest_sha256) = 32
        ),
        coverage_sha256 BLOB NOT NULL CHECK(
            typeof(coverage_sha256) = 'blob' AND length(coverage_sha256) = 32
        ),
        coverage_member_root_sha256 BLOB NOT NULL CHECK(
            typeof(coverage_member_root_sha256) = 'blob' AND
            length(coverage_member_root_sha256) = 32
        ),
        coverage_blob_root_sha256 BLOB NOT NULL CHECK(
            typeof(coverage_blob_root_sha256) = 'blob' AND
            length(coverage_blob_root_sha256) = 32
        ),
        coverage_batch_count INTEGER NOT NULL CHECK(
            coverage_batch_count BETWEEN 0 AND 1000000
        ),
        coverage_repository_member_count INTEGER NOT NULL CHECK(
            coverage_repository_member_count BETWEEN 0 AND 10000
        ),
        coverage_live_project_count INTEGER NOT NULL CHECK(
            coverage_live_project_count BETWEEN 0 AND 10000
        ),
        coverage_deleted_project_count INTEGER NOT NULL CHECK(
            coverage_deleted_project_count BETWEEN 0 AND 10000
        ),
        coverage_observed_occurrence_count INTEGER NOT NULL CHECK(
            coverage_observed_occurrence_count BETWEEN 0 AND 1000000
        ),
        coverage_observed_row_count INTEGER NOT NULL CHECK(
            coverage_observed_row_count BETWEEN 0 AND 3000000
        ),
        coverage_observed_manifest_bytes INTEGER NOT NULL CHECK(
            coverage_observed_manifest_bytes BETWEEN 0 AND 9223372036854775807
        ),
        coverage_project_count INTEGER NOT NULL CHECK(
            coverage_project_count BETWEEN 0 AND 10000
        ),
        coverage_reference_count INTEGER NOT NULL CHECK(
            coverage_reference_count BETWEEN 0 AND 1000000
        ),
        coverage_unique_blob_count INTEGER NOT NULL CHECK(
            coverage_unique_blob_count BETWEEN 0 AND 1000000
        ),
        coverage_unique_blob_bytes INTEGER NOT NULL CHECK(
            coverage_unique_blob_bytes BETWEEN 0 AND 9223372036854775807
        ),
        coverage_referenced_bytes INTEGER NOT NULL CHECK(
            coverage_referenced_bytes BETWEEN 0 AND 9223372036854775807
        ),
        provider_protocol TEXT NOT NULL CHECK(
            provider_protocol = 'ze.business-cutover.asset-provider-checkpoint.v1'
        ),
        provider_id TEXT NOT NULL CHECK(
            length(CAST(provider_id AS BLOB)) BETWEEN 1 AND 1024
        ),
        provider_generation TEXT NOT NULL CHECK(
            length(CAST(provider_generation AS BLOB)) BETWEEN 1 AND 1024
        ),
        provider_checkpoint_sequence INTEGER NOT NULL CHECK(
            provider_checkpoint_sequence BETWEEN 1 AND 9223372036854775807
        ),
        provider_checked_at TEXT NOT NULL CHECK(
            length(CAST(provider_checked_at AS BLOB)) = 24 AND
            strftime('%Y-%m-%dT%H:%M:%fZ', provider_checked_at) IS
                provider_checked_at
        ),
        provider_blob_count INTEGER NOT NULL CHECK(
            provider_blob_count BETWEEN 0 AND 1000000
        ),
        provider_blob_bytes INTEGER NOT NULL CHECK(
            provider_blob_bytes BETWEEN 0 AND 9223372036854775807
        ),
        provider_blob_root_sha256 BLOB NOT NULL CHECK(
            typeof(provider_blob_root_sha256) = 'blob' AND
            length(provider_blob_root_sha256) = 32
        ),
        provider_receipt_schema TEXT NOT NULL CHECK(
            provider_receipt_schema =
                'ze.business-cutover.asset-provider-receipt.v1'
        ),
        provider_receipt_codec_id TEXT NOT NULL CHECK(
            provider_receipt_codec_id = 'ze.deterministic-json'
        ),
        provider_receipt_codec_version INTEGER NOT NULL CHECK(
            provider_receipt_codec_version = 1
        ),
        provider_receipt_payload_bytes BLOB NOT NULL CHECK(
            typeof(provider_receipt_payload_bytes) = 'blob' AND
            length(provider_receipt_payload_bytes) BETWEEN 1 AND
                {MAX_BUSINESS_SMALL_PAYLOAD_BYTES}
        ),
        provider_receipt_payload_sha256 BLOB NOT NULL CHECK(
            typeof(provider_receipt_payload_sha256) = 'blob' AND
            length(provider_receipt_payload_sha256) = 32
        ),
        provider_checkpoint_sha256 BLOB NOT NULL UNIQUE CHECK(
            typeof(provider_checkpoint_sha256) = 'blob' AND
            length(provider_checkpoint_sha256) = 32
        )
    """,
    specialised_checks="""
        CHECK(
            coverage_repository_member_count =
                coverage_live_project_count + coverage_deleted_project_count
        ),
        CHECK(coverage_project_count = coverage_live_project_count),
        CHECK(coverage_unique_blob_count <= coverage_reference_count),
        CHECK(coverage_unique_blob_bytes <= coverage_referenced_bytes),
        CHECK(coverage_observed_occurrence_count >= coverage_reference_count),
        CHECK(coverage_observed_row_count >= coverage_observed_occurrence_count),
        CHECK((coverage_batch_count = 0) = (coverage_reference_count = 0)),
        CHECK((coverage_batch_count = 0) =
            (coverage_observed_manifest_bytes = 0)),
        CHECK(provider_blob_count = coverage_unique_blob_count),
        CHECK(provider_blob_bytes = coverage_unique_blob_bytes),
        CHECK(provider_blob_root_sha256 = coverage_blob_root_sha256),
        CHECK(coverage_committed_at <= predecessor_updated_at),
        CHECK(provider_checked_at >= predecessor_updated_at)
    """,
    specialised_foreign_keys="""
        FOREIGN KEY(
            cutover_id, coverage_transaction_id, coverage_receipt_kind,
            source_admission_revision, source_admission_sha256,
            coverage_manifest_sha256, coverage_sha256
        ) REFERENCES business_asset_coverage_receipts(
            cutover_id, transaction_id, receipt_kind,
            source_admission_revision, source_admission_sha256,
            manifest_sha256, coverage_sha256
        ) ON UPDATE RESTRICT ON DELETE RESTRICT
    """,
)

_SCHEMA_V13_AUDIT_BINDING_TABLE = _schema_v13_binding_table(
    "business_cutover_audit_bindings",
    predecessor_revision=2,
    predecessor_phase="assets_covered",
    predecessor_authority="read_only",
    successor_revision=3,
    successor_phase="audited",
    successor_authority="read_only",
    file_admission_state="cutover_pending",
    file_admission_revision_delta=1,
    receipt_delta=1,
    successor_time_check="successor_updated_at >= predecessor_updated_at",
    specialised_columns="""
        audit_certificate_id TEXT NOT NULL CHECK(
            length(audit_certificate_id) = 32 AND
            length(CAST(audit_certificate_id AS BLOB)) = 32 AND
            audit_certificate_id NOT GLOB '*[^0-9a-f]*'
        ),
        audit_certificate_sequence INTEGER NOT NULL CHECK(
            audit_certificate_sequence BETWEEN 1 AND 9223372036854775807
        ),
        audit_purpose TEXT NOT NULL CHECK(audit_purpose = 'install'),
        audit_transaction_id TEXT NOT NULL CHECK(
            audit_transaction_id = '_repository-audit:' || audit_certificate_id
        ),
        audit_receipt_kind TEXT NOT NULL CHECK(
            audit_receipt_kind = '_repository_audit_certificate_v1'
        ),
        audit_committed_at TEXT NOT NULL CHECK(
            length(CAST(audit_committed_at AS BLOB)) = 24 AND
            strftime('%Y-%m-%dT%H:%M:%fZ', audit_committed_at) IS
                audit_committed_at
        ),
        audit_previous_certificate_id TEXT,
        audit_previous_manifest_sha256 BLOB,
        audit_previous_tail_sha256 BLOB NOT NULL CHECK(
            typeof(audit_previous_tail_sha256) = 'blob' AND
            length(audit_previous_tail_sha256) = 32
        ),
        audit_manifest_sha256 BLOB NOT NULL CHECK(
            typeof(audit_manifest_sha256) = 'blob' AND
            length(audit_manifest_sha256) = 32
        ),
        audit_certificate_tail_sha256 BLOB NOT NULL UNIQUE CHECK(
            typeof(audit_certificate_tail_sha256) = 'blob' AND
            length(audit_certificate_tail_sha256) = 32 AND
            audit_certificate_tail_sha256 != zeroblob(32)
        ),
        audit_findings_sha256 BLOB NOT NULL CHECK(
            typeof(audit_findings_sha256) = 'blob' AND
            length(audit_findings_sha256) = 32
        ),
        actual_schema_version INTEGER NOT NULL CHECK(
            actual_schema_version = 13
        ),
        actual_schema_fingerprint TEXT NOT NULL CHECK(
            length(actual_schema_fingerprint) = 64 AND
            length(CAST(actual_schema_fingerprint AS BLOB)) = 64 AND
            actual_schema_fingerprint NOT GLOB '*[^0-9a-f]*'
        ),
        actual_migration_ledger_count INTEGER NOT NULL CHECK(
            actual_migration_ledger_count = 13
        ),
        actual_migration_ledger_sha256 BLOB NOT NULL CHECK(
            typeof(actual_migration_ledger_sha256) = 'blob' AND
            length(actual_migration_ledger_sha256) = 32
        ),
        audited_install_manifest_sha256 BLOB NOT NULL CHECK(
            typeof(audited_install_manifest_sha256) = 'blob' AND
            length(audited_install_manifest_sha256) = 32
        ),
        covered_repository_revision INTEGER NOT NULL CHECK(
            covered_repository_revision BETWEEN 1 AND 9223372036854775807
        ),
        audit_started_at TEXT NOT NULL CHECK(
            length(CAST(audit_started_at AS BLOB)) = 24 AND
            strftime('%Y-%m-%dT%H:%M:%fZ', audit_started_at) IS audit_started_at
        ),
        audit_completed_at TEXT NOT NULL CHECK(
            length(CAST(audit_completed_at AS BLOB)) = 24 AND
            strftime('%Y-%m-%dT%H:%M:%fZ', audit_completed_at) IS
                audit_completed_at
        ),
        page_integrity TEXT NOT NULL CHECK(page_integrity = 'ok'),
        foreign_key_violation_count INTEGER NOT NULL CHECK(
            foreign_key_violation_count = 0
        ),
        namespace_violation_count INTEGER NOT NULL CHECK(
            namespace_violation_count = 0
        ),
        audit_head_state_revision INTEGER NOT NULL CHECK(
            audit_head_state_revision BETWEEN 1 AND 9223372036854775807
        ),
        audit_head_sequence INTEGER NOT NULL CHECK(
            audit_head_sequence = audit_certificate_sequence
        ),
        audit_head_certificate_id TEXT NOT NULL CHECK(
            audit_head_certificate_id = audit_certificate_id
        ),
        audit_head_previous_tail_sha256 BLOB NOT NULL CHECK(
            typeof(audit_head_previous_tail_sha256) = 'blob' AND
            length(audit_head_previous_tail_sha256) = 32
        ),
        audit_head_tail_sha256 BLOB NOT NULL CHECK(
            audit_head_tail_sha256 = audit_certificate_tail_sha256
        ),
        audit_head_updated_at TEXT NOT NULL CHECK(
            length(CAST(audit_head_updated_at AS BLOB)) = 24 AND
            strftime('%Y-%m-%dT%H:%M:%fZ', audit_head_updated_at) IS
                audit_head_updated_at
        )
    """,
    specialised_checks="""
        CHECK(
            (audit_certificate_sequence = 1 AND
             audit_previous_certificate_id IS NULL AND
             audit_previous_manifest_sha256 IS NULL AND
             audit_previous_tail_sha256 = zeroblob(32)) OR
            (audit_certificate_sequence > 1 AND
             length(audit_previous_certificate_id) = 32 AND
             typeof(audit_previous_manifest_sha256) = 'blob' AND
             length(audit_previous_manifest_sha256) = 32 AND
             audit_previous_tail_sha256 != zeroblob(32))
        ),
        CHECK(covered_repository_revision = runtime_repository_revision),
        CHECK(audit_started_at >= runtime_updated_at),
        CHECK(audit_completed_at >= audit_started_at),
        CHECK(audit_committed_at = audit_completed_at),
        CHECK(audit_completed_at = successor_updated_at),
        CHECK(audit_head_updated_at = audit_completed_at),
        CHECK(audit_head_previous_tail_sha256 = audit_previous_tail_sha256),
        CHECK(before_receipt_count = after_receipt_count - 1)
    """,
    specialised_foreign_keys="""
        FOREIGN KEY(audit_certificate_id)
            REFERENCES repository_audit_certificates(certificate_id)
            ON UPDATE RESTRICT ON DELETE RESTRICT
    """,
)

_SCHEMA_V13_ACTIVATION_BINDING_TABLE = _schema_v13_binding_table(
    "business_cutover_activation_bindings",
    predecessor_revision=3,
    predecessor_phase="audited",
    predecessor_authority="read_only",
    successor_revision=4,
    successor_phase="activated",
    successor_authority="active",
    file_admission_state="sqlite",
    file_admission_revision_delta=2,
    receipt_delta=1,
    successor_time_check="successor_updated_at >= predecessor_updated_at",
    specialised_columns="""
        activation_transaction_id TEXT NOT NULL CHECK(
            activation_transaction_id =
                '_business-cutover-activation:' || cutover_id
        ),
        activation_receipt_kind TEXT NOT NULL CHECK(
            activation_receipt_kind = '_business_cutover_activation_v2'
        ),
        activation_committed_at TEXT NOT NULL CHECK(
            length(CAST(activation_committed_at AS BLOB)) = 24 AND
            strftime('%Y-%m-%dT%H:%M:%fZ', activation_committed_at) IS
                activation_committed_at
        ),
        activation_manifest_sha256 BLOB NOT NULL CHECK(
            typeof(activation_manifest_sha256) = 'blob' AND
            length(activation_manifest_sha256) = 32
        ),
        install_transaction_id TEXT NOT NULL CHECK(
            install_transaction_id = '_business-cutover-install:' || cutover_id
        ),
        install_receipt_kind TEXT NOT NULL CHECK(
            install_receipt_kind = '_business_cutover_install_v1'
        ),
        install_manifest_sha256 BLOB NOT NULL CHECK(
            typeof(install_manifest_sha256) = 'blob' AND
            length(install_manifest_sha256) = 32
        ),
        audit_certificate_id TEXT NOT NULL CHECK(
            length(audit_certificate_id) = 32 AND
            audit_certificate_id NOT GLOB '*[^0-9a-f]*'
        ),
        audit_certificate_sequence INTEGER NOT NULL CHECK(
            audit_certificate_sequence BETWEEN 1 AND 9223372036854775807
        ),
        audit_transaction_id TEXT NOT NULL CHECK(
            audit_transaction_id = '_repository-audit:' || audit_certificate_id
        ),
        audit_receipt_kind TEXT NOT NULL CHECK(
            audit_receipt_kind = '_repository_audit_certificate_v1'
        ),
        audit_manifest_sha256 BLOB NOT NULL CHECK(
            typeof(audit_manifest_sha256) = 'blob' AND
            length(audit_manifest_sha256) = 32
        ),
        audit_certificate_tail_sha256 BLOB NOT NULL CHECK(
            typeof(audit_certificate_tail_sha256) = 'blob' AND
            length(audit_certificate_tail_sha256) = 32
        ),
        audit_completed_at TEXT NOT NULL CHECK(
            length(CAST(audit_completed_at AS BLOB)) = 24 AND
            strftime('%Y-%m-%dT%H:%M:%fZ', audit_completed_at) IS
                audit_completed_at
        ),
        expected_migration_ledger_count INTEGER NOT NULL CHECK(
            expected_migration_ledger_count = 13
        ),
        expected_migration_ledger_sha256 BLOB NOT NULL CHECK(
            typeof(expected_migration_ledger_sha256) = 'blob' AND
            length(expected_migration_ledger_sha256) = 32
        ),
        actual_schema_version INTEGER NOT NULL CHECK(actual_schema_version = 13),
        actual_schema_fingerprint TEXT NOT NULL CHECK(
            length(actual_schema_fingerprint) = 64 AND
            actual_schema_fingerprint NOT GLOB '*[^0-9a-f]*'
        ),
        final_admission_state TEXT NOT NULL CHECK(final_admission_state = 'sqlite'),
        final_admission_epoch TEXT NOT NULL CHECK(
            final_admission_epoch = repository_epoch
        ),
        final_admission_revision INTEGER NOT NULL CHECK(
            final_admission_revision BETWEEN 1 AND 9223372036854775807
        ),
        final_admission_cutover_id TEXT NOT NULL CHECK(
            final_admission_cutover_id = cutover_id
        ),
        final_admission_sha256 BLOB NOT NULL CHECK(
            typeof(final_admission_sha256) = 'blob' AND
            length(final_admission_sha256) = 32
        ),
        before_runtime_repository_revision INTEGER NOT NULL CHECK(
            before_runtime_repository_revision BETWEEN 1 AND 9223372036854775807
        ),
        before_runtime_state_revision INTEGER NOT NULL CHECK(
            before_runtime_state_revision BETWEEN 1 AND 9223372036854775807
        ),
        before_runtime_authority_state TEXT NOT NULL CHECK(
            before_runtime_authority_state = 'read_only'
        ),
        before_runtime_updated_at TEXT NOT NULL CHECK(
            length(CAST(before_runtime_updated_at AS BLOB)) = 24 AND
            strftime('%Y-%m-%dT%H:%M:%fZ', before_runtime_updated_at) IS
                before_runtime_updated_at
        ),
        after_runtime_repository_revision INTEGER NOT NULL CHECK(
            after_runtime_repository_revision = before_runtime_repository_revision
        ),
        after_runtime_state_revision INTEGER NOT NULL CHECK(
            after_runtime_state_revision = before_runtime_state_revision + 1
        ),
        after_runtime_authority_state TEXT NOT NULL CHECK(
            after_runtime_authority_state = 'active'
        ),
        after_runtime_updated_at TEXT NOT NULL CHECK(
            length(CAST(after_runtime_updated_at AS BLOB)) = 24 AND
            strftime('%Y-%m-%dT%H:%M:%fZ', after_runtime_updated_at) IS
                after_runtime_updated_at
        )
    """,
    specialised_checks="""
        CHECK(final_admission_revision >= source_admission_revision),
        CHECK(final_admission_sha256 = file_admission_sha256),
        CHECK(before_runtime_repository_revision = runtime_repository_revision),
        CHECK(before_runtime_state_revision = runtime_state_revision),
        CHECK(before_runtime_updated_at = runtime_updated_at),
        CHECK(after_runtime_updated_at = activation_committed_at),
        CHECK(activation_committed_at = successor_updated_at),
        CHECK(audit_completed_at <= activation_committed_at)
    """,
    specialised_foreign_keys="""
        FOREIGN KEY(cutover_id, activation_transaction_id,
                    activation_manifest_sha256)
            REFERENCES business_cutover_activation_receipts(
                cutover_id, transaction_id, manifest_sha256
            ) ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY(cutover_id, install_transaction_id,
                    install_manifest_sha256)
            REFERENCES business_cutover_install_receipts(
                cutover_id, transaction_id, manifest_sha256
            ) ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY(audit_certificate_id)
            REFERENCES repository_audit_certificates(certificate_id)
            ON UPDATE RESTRICT ON DELETE RESTRICT
    """,
)

_SCHEMA_V13_MOUNT_BINDING_TABLE = _schema_v13_binding_table(
    "business_cutover_mount_bindings",
    predecessor_revision=4,
    predecessor_phase="activated",
    predecessor_authority="active",
    successor_revision=5,
    successor_phase="mounted",
    successor_authority="active",
    file_admission_state="sqlite",
    file_admission_revision_delta=2,
    receipt_delta=1,
    successor_time_check="successor_updated_at >= predecessor_updated_at",
    specialised_columns="""
        mount_transaction_id TEXT NOT NULL CHECK(
            mount_transaction_id = '_business-cutover-mount:' || cutover_id
        ),
        mount_receipt_kind TEXT NOT NULL CHECK(
            mount_receipt_kind = '_business_cutover_mount_v2'
        ),
        mount_committed_at TEXT NOT NULL CHECK(
            length(CAST(mount_committed_at AS BLOB)) = 24 AND
            strftime('%Y-%m-%dT%H:%M:%fZ', mount_committed_at) IS
                mount_committed_at
        ),
        mount_manifest_sha256 BLOB NOT NULL CHECK(
            typeof(mount_manifest_sha256) = 'blob' AND
            length(mount_manifest_sha256) = 32
        ),
        mount_source_admission_sha256 BLOB NOT NULL CHECK(
            typeof(mount_source_admission_sha256) = 'blob' AND
            length(mount_source_admission_sha256) = 32
        ),
        mount_final_admission_sha256 BLOB NOT NULL CHECK(
            typeof(mount_final_admission_sha256) = 'blob' AND
            length(mount_final_admission_sha256) = 32
        ),
        install_transaction_id TEXT NOT NULL CHECK(
            install_transaction_id = '_business-cutover-install:' || cutover_id
        ),
        install_receipt_kind TEXT NOT NULL CHECK(
            install_receipt_kind = '_business_cutover_install_v1'
        ),
        install_committed_at TEXT NOT NULL CHECK(
            length(CAST(install_committed_at AS BLOB)) = 24 AND
            strftime('%Y-%m-%dT%H:%M:%fZ', install_committed_at) IS
                install_committed_at
        ),
        install_manifest_sha256 BLOB NOT NULL CHECK(
            typeof(install_manifest_sha256) = 'blob' AND
            length(install_manifest_sha256) = 32
        ),
        coverage_transaction_id TEXT NOT NULL CHECK(
            coverage_transaction_id =
                '_business-cutover-asset-coverage:' || cutover_id
        ),
        coverage_receipt_kind TEXT NOT NULL CHECK(
            coverage_receipt_kind = '_business_cutover_asset_coverage_v1'
        ),
        coverage_committed_at TEXT NOT NULL CHECK(
            length(CAST(coverage_committed_at AS BLOB)) = 24 AND
            strftime('%Y-%m-%dT%H:%M:%fZ', coverage_committed_at) IS
                coverage_committed_at
        ),
        coverage_manifest_sha256 BLOB NOT NULL CHECK(
            typeof(coverage_manifest_sha256) = 'blob' AND
            length(coverage_manifest_sha256) = 32
        ),
        coverage_sha256 BLOB NOT NULL CHECK(
            typeof(coverage_sha256) = 'blob' AND length(coverage_sha256) = 32
        ),
        coverage_member_root_sha256 BLOB NOT NULL CHECK(
            typeof(coverage_member_root_sha256) = 'blob' AND
            length(coverage_member_root_sha256) = 32
        ),
        coverage_blob_root_sha256 BLOB NOT NULL CHECK(
            typeof(coverage_blob_root_sha256) = 'blob' AND
            length(coverage_blob_root_sha256) = 32
        ),
        coverage_batch_count INTEGER NOT NULL CHECK(
            coverage_batch_count BETWEEN 0 AND 1000000
        ),
        coverage_repository_member_count INTEGER NOT NULL CHECK(
            coverage_repository_member_count BETWEEN 0 AND 10000
        ),
        coverage_live_project_count INTEGER NOT NULL CHECK(
            coverage_live_project_count BETWEEN 0 AND 10000
        ),
        coverage_deleted_project_count INTEGER NOT NULL CHECK(
            coverage_deleted_project_count BETWEEN 0 AND 10000
        ),
        coverage_observed_occurrence_count INTEGER NOT NULL CHECK(
            coverage_observed_occurrence_count BETWEEN 0 AND 1000000
        ),
        coverage_observed_row_count INTEGER NOT NULL CHECK(
            coverage_observed_row_count BETWEEN 0 AND 3000000
        ),
        coverage_observed_manifest_bytes INTEGER NOT NULL CHECK(
            coverage_observed_manifest_bytes BETWEEN 0 AND 9223372036854775807
        ),
        coverage_project_count INTEGER NOT NULL CHECK(
            coverage_project_count BETWEEN 0 AND 10000
        ),
        coverage_reference_count INTEGER NOT NULL CHECK(
            coverage_reference_count BETWEEN 0 AND 1000000
        ),
        coverage_unique_blob_count INTEGER NOT NULL CHECK(
            coverage_unique_blob_count BETWEEN 0 AND 1000000
        ),
        coverage_unique_blob_bytes INTEGER NOT NULL CHECK(
            coverage_unique_blob_bytes BETWEEN 0 AND 9223372036854775807
        ),
        coverage_referenced_bytes INTEGER NOT NULL CHECK(
            coverage_referenced_bytes BETWEEN 0 AND 9223372036854775807
        ),
        activation_transaction_id TEXT NOT NULL CHECK(
            activation_transaction_id =
                '_business-cutover-activation:' || cutover_id
        ),
        activation_receipt_kind TEXT NOT NULL CHECK(
            activation_receipt_kind = '_business_cutover_activation_v2'
        ),
        activation_committed_at TEXT NOT NULL CHECK(
            length(CAST(activation_committed_at AS BLOB)) = 24 AND
            strftime('%Y-%m-%dT%H:%M:%fZ', activation_committed_at) IS
                activation_committed_at
        ),
        activation_manifest_sha256 BLOB NOT NULL CHECK(
            typeof(activation_manifest_sha256) = 'blob' AND
            length(activation_manifest_sha256) = 32
        ),
        audit_certificate_id TEXT NOT NULL CHECK(
            length(audit_certificate_id) = 32 AND
            audit_certificate_id NOT GLOB '*[^0-9a-f]*'
        ),
        audit_certificate_sequence INTEGER NOT NULL CHECK(
            audit_certificate_sequence BETWEEN 1 AND 9223372036854775807
        ),
        audit_transaction_id TEXT NOT NULL CHECK(
            audit_transaction_id = '_repository-audit:' || audit_certificate_id
        ),
        audit_receipt_kind TEXT NOT NULL CHECK(
            audit_receipt_kind = '_repository_audit_certificate_v1'
        ),
        audit_committed_at TEXT NOT NULL CHECK(
            length(CAST(audit_committed_at AS BLOB)) = 24 AND
            strftime('%Y-%m-%dT%H:%M:%fZ', audit_committed_at) IS
                audit_committed_at
        ),
        audit_manifest_sha256 BLOB NOT NULL CHECK(
            typeof(audit_manifest_sha256) = 'blob' AND
            length(audit_manifest_sha256) = 32
        ),
        target_schema_version INTEGER NOT NULL CHECK(target_schema_version = 13),
        target_schema_fingerprint TEXT NOT NULL CHECK(
            length(target_schema_fingerprint) = 64 AND
            target_schema_fingerprint NOT GLOB '*[^0-9a-f]*'
        )
    """,
    specialised_checks="""
        CHECK(mount_source_admission_sha256 = source_admission_sha256),
        CHECK(mount_final_admission_sha256 = file_admission_sha256),
        CHECK(
            coverage_repository_member_count =
                coverage_live_project_count + coverage_deleted_project_count
        ),
        CHECK(coverage_project_count = coverage_live_project_count),
        CHECK(coverage_unique_blob_count <= coverage_reference_count),
        CHECK(coverage_unique_blob_bytes <= coverage_referenced_bytes),
        CHECK(coverage_observed_occurrence_count >= coverage_reference_count),
        CHECK(coverage_observed_row_count >= coverage_observed_occurrence_count),
        CHECK((coverage_batch_count = 0) = (coverage_reference_count = 0)),
        CHECK((coverage_batch_count = 0) =
            (coverage_observed_manifest_bytes = 0)),
        CHECK(coverage_committed_at <= install_committed_at),
        CHECK(install_committed_at <= audit_committed_at),
        CHECK(audit_committed_at <= activation_committed_at),
        CHECK(activation_committed_at <= mount_committed_at),
        CHECK(mount_committed_at = successor_updated_at)
    """,
    specialised_foreign_keys="""
        FOREIGN KEY(cutover_id, mount_transaction_id, mount_manifest_sha256)
            REFERENCES business_cutover_mount_receipts(
                cutover_id, transaction_id, manifest_sha256
            ) ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY(cutover_id, install_transaction_id,
                    install_manifest_sha256)
            REFERENCES business_cutover_install_receipts(
                cutover_id, transaction_id, manifest_sha256
            ) ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY(cutover_id, coverage_transaction_id,
                    coverage_manifest_sha256)
            REFERENCES business_asset_coverage_receipts(
                cutover_id, transaction_id, manifest_sha256
            ) ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY(cutover_id, activation_transaction_id,
                    activation_manifest_sha256)
            REFERENCES business_cutover_activation_receipts(
                cutover_id, transaction_id, manifest_sha256
            ) ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY(audit_certificate_id)
            REFERENCES repository_audit_certificates(certificate_id)
            ON UPDATE RESTRICT ON DELETE RESTRICT
    """,
)


def _schema_v13_binding_snapshot_guard(
    table: str,
    predecessor_revision: int,
) -> str:
    """Require the binding INSERT to observe the live predecessor snapshot."""

    return f"""
    CREATE TRIGGER {table}_snapshot_guard
    BEFORE INSERT ON {table}
    WHEN NOT EXISTS (
             SELECT 1 FROM business_cutover_head AS head
             WHERE head.singleton = 1 AND
                   head.cutover_id = NEW.cutover_id AND
                   head.repository_epoch = NEW.repository_epoch AND
                   head.runtime_generation = NEW.runtime_generation AND
                   head.code_digest = NEW.code_digest AND
                   head.state_revision = NEW.predecessor_state_revision AND
                   head.phase_revision = NEW.predecessor_phase_revision AND
                   head.phase = NEW.predecessor_phase AND
                   head.authority_state = NEW.predecessor_authority_state AND
                   head.head_sha256 = NEW.predecessor_head_sha256 AND
                   head.previous_head_sha256 =
                        NEW.predecessor_previous_head_sha256 AND
                   head.updated_at = NEW.predecessor_updated_at
         ) OR NEW.predecessor_phase_revision != {predecessor_revision} OR
         NOT EXISTS (
             SELECT 1 FROM repository_runtime_control AS runtime
             WHERE runtime.singleton = 1 AND
                   runtime.cutover_id = NEW.cutover_id AND
                   runtime.repository_epoch = NEW.repository_epoch AND
                   runtime.runtime_generation = NEW.runtime_generation AND
                   runtime.code_digest = NEW.code_digest AND
                   runtime.authority = NEW.runtime_authority AND
                   runtime.authority_state = NEW.runtime_authority_state AND
                   runtime.source_admission_revision =
                        NEW.source_admission_revision AND
                   runtime.repository_revision =
                        NEW.runtime_repository_revision AND
                   runtime.state_revision = NEW.runtime_state_revision AND
                   runtime.schema_version = NEW.runtime_schema_version AND
                   runtime.updated_at = NEW.runtime_updated_at
         ) OR NOT EXISTS (
             SELECT 1 FROM worker_ledger_head AS worker
             WHERE worker.singleton = 1 AND
                   worker.state_revision = NEW.worker_head_state_revision AND
                   worker.sequence = NEW.worker_ledger_sequence AND
                   worker.tail_sha256 = NEW.worker_ledger_tail_sha256 AND
                   worker.updated_at = NEW.worker_head_updated_at
         ) OR NOT EXISTS (
             SELECT 1 FROM outbox_allocation_head AS allocation
             WHERE allocation.singleton = 1 AND
                   allocation.state_revision =
                        NEW.allocation_head_state_revision AND
                   allocation.sequence = NEW.allocation_sequence AND
                   allocation.tail_sha256 = NEW.allocation_tail_sha256 AND
                   allocation.updated_at = NEW.allocation_head_updated_at
         ) OR NOT EXISTS (
             SELECT 1 FROM outbox_dispatch_head AS dispatch
             WHERE dispatch.singleton = 1 AND
                   dispatch.state_revision = NEW.dispatch_head_state_revision AND
                   dispatch.published_sequence =
                        NEW.dispatch_published_sequence AND
                   dispatch.published_repository_revision IS
                        NEW.dispatch_published_repository_revision AND
                   dispatch.published_ordinal IS
                        NEW.dispatch_published_ordinal AND
                   dispatch.inflight_sequence IS NEW.dispatch_inflight_sequence AND
                   dispatch.inflight_repository_revision IS
                        NEW.dispatch_inflight_repository_revision AND
                   dispatch.inflight_ordinal IS NEW.dispatch_inflight_ordinal AND
                   dispatch.head_sha256 = NEW.dispatch_head_sha256 AND
                   dispatch.updated_at = NEW.dispatch_head_updated_at
         ) OR (SELECT count(*) FROM transaction_receipts) !=
                NEW.after_receipt_count
    BEGIN SELECT RAISE(ABORT,
        'production_cutover_direct_predecessor_invalid'); END
    """


def _schema_v13_head_reverse_guard(
    table: str,
    predecessor_revision: int,
    successor_revision: int,
) -> str:
    """Require an exact, still-live sidecar for a head transition."""

    stage_pointer = {
        2: """
                   binding.coverage_transaction_id =
                        NEW.coverage_transaction_id AND
                   binding.coverage_manifest_sha256 =
                        NEW.coverage_manifest_sha256 AND""",
        3: """
                   binding.audit_transaction_id = NEW.audit_transaction_id AND
                   binding.audit_manifest_sha256 = NEW.audit_manifest_sha256 AND""",
        4: """
                   binding.activation_transaction_id =
                        NEW.activation_transaction_id AND
                   binding.activation_manifest_sha256 =
                        NEW.activation_manifest_sha256 AND""",
        5: """
                   binding.mount_transaction_id = NEW.mount_transaction_id AND
                   binding.mount_manifest_sha256 = NEW.mount_manifest_sha256 AND""",
    }[successor_revision]
    audit_head_freshness = ""
    if successor_revision == 3:
        audit_head_freshness = """
         OR NOT EXISTS (
             SELECT 1 FROM repository_audit_head AS audit_head
             JOIN business_cutover_audit_bindings AS binding
               ON binding.cutover_id = OLD.cutover_id
             WHERE audit_head.singleton = 1 AND
                   audit_head.state_revision =
                        binding.audit_head_state_revision AND
                   audit_head.sequence = binding.audit_head_sequence AND
                   audit_head.certificate_id =
                        binding.audit_head_certificate_id AND
                   audit_head.previous_tail_sha256 =
                        binding.audit_head_previous_tail_sha256 AND
                   audit_head.tail_sha256 = binding.audit_head_tail_sha256 AND
                   audit_head.updated_at = binding.audit_head_updated_at
         )"""

    return f"""
    CREATE TRIGGER business_cutover_head_v13_{successor_revision}_binding_guard
    BEFORE UPDATE ON business_cutover_head
    WHEN OLD.phase_revision = {predecessor_revision} AND
         NEW.phase_revision = {successor_revision} AND
         (NOT EXISTS (
             SELECT 1 FROM {table} AS binding
             WHERE binding.cutover_id = OLD.cutover_id AND
                   binding.repository_epoch = OLD.repository_epoch AND
                   binding.runtime_generation = OLD.runtime_generation AND
                   binding.code_digest = OLD.code_digest AND
                   binding.predecessor_state_revision = OLD.state_revision AND
                   binding.predecessor_phase_revision = OLD.phase_revision AND
                   binding.predecessor_phase = OLD.phase AND
                   binding.predecessor_authority_state = OLD.authority_state AND
                   binding.predecessor_head_sha256 = OLD.head_sha256 AND
                   binding.predecessor_previous_head_sha256 =
                        OLD.previous_head_sha256 AND
                   binding.predecessor_updated_at = OLD.updated_at AND
{stage_pointer}
                   binding.successor_state_revision = NEW.state_revision AND
                   binding.successor_phase_revision = NEW.phase_revision AND
                   binding.successor_phase = NEW.phase AND
                   binding.successor_authority_state = NEW.authority_state AND
                   binding.successor_previous_head_sha256 =
                        NEW.previous_head_sha256 AND
                   binding.successor_head_sha256 = NEW.head_sha256 AND
                   binding.successor_updated_at = NEW.updated_at
         ) OR NOT EXISTS (
             SELECT 1 FROM {table} AS binding
             JOIN repository_runtime_control AS runtime
               ON runtime.singleton = 1 AND
                  runtime.cutover_id = binding.cutover_id AND
                  runtime.repository_epoch = binding.repository_epoch AND
                  runtime.runtime_generation = binding.runtime_generation AND
                  runtime.code_digest = binding.code_digest AND
                  runtime.authority = binding.runtime_authority AND
                  runtime.authority_state = binding.runtime_authority_state AND
                  runtime.source_admission_revision =
                       binding.source_admission_revision AND
                  runtime.repository_revision =
                       binding.runtime_repository_revision AND
                  runtime.state_revision = binding.runtime_state_revision AND
                  runtime.schema_version = binding.runtime_schema_version AND
                  runtime.updated_at = binding.runtime_updated_at
             JOIN worker_ledger_head AS worker
               ON worker.singleton = 1 AND
                  worker.state_revision = binding.worker_head_state_revision AND
                  worker.sequence = binding.worker_ledger_sequence AND
                  worker.tail_sha256 = binding.worker_ledger_tail_sha256 AND
                  worker.updated_at = binding.worker_head_updated_at
             JOIN outbox_allocation_head AS allocation
               ON allocation.singleton = 1 AND
                  allocation.state_revision =
                       binding.allocation_head_state_revision AND
                  allocation.sequence = binding.allocation_sequence AND
                  allocation.tail_sha256 = binding.allocation_tail_sha256 AND
                  allocation.updated_at = binding.allocation_head_updated_at
             JOIN outbox_dispatch_head AS dispatch
               ON dispatch.singleton = 1 AND
                  dispatch.state_revision =
                       binding.dispatch_head_state_revision AND
                  dispatch.published_sequence =
                       binding.dispatch_published_sequence AND
                  dispatch.published_repository_revision IS
                       binding.dispatch_published_repository_revision AND
                  dispatch.published_ordinal IS
                       binding.dispatch_published_ordinal AND
                  dispatch.inflight_sequence IS
                       binding.dispatch_inflight_sequence AND
                  dispatch.inflight_repository_revision IS
                       binding.dispatch_inflight_repository_revision AND
                  dispatch.inflight_ordinal IS
                       binding.dispatch_inflight_ordinal AND
                  dispatch.head_sha256 = binding.dispatch_head_sha256 AND
                  dispatch.updated_at = binding.dispatch_head_updated_at
             WHERE binding.cutover_id = OLD.cutover_id AND
                   binding.predecessor_head_sha256 = OLD.head_sha256 AND
                   (SELECT count(*) FROM transaction_receipts) =
                        binding.after_receipt_count
         ){audit_head_freshness})
    BEGIN SELECT RAISE(ABORT,
        'production_cutover_direct_predecessor_missing'); END
    """


def _schema_v13_specialised_phase_guard(
    table: str,
    predecessor_revision: int,
) -> str:
    """Give later phase writers a stable off-phase INSERT rejection."""

    return f"""
    CREATE TRIGGER {table}_phase_guard
    BEFORE INSERT ON {table}
    WHEN NOT EXISTS (
        SELECT 1 FROM business_cutover_head AS head
        WHERE head.singleton = 1 AND
              head.cutover_id = NEW.cutover_id AND
              head.phase_revision = {predecessor_revision} AND
              head.state_revision = NEW.predecessor_state_revision AND
              head.head_sha256 = NEW.predecessor_head_sha256
    )
    BEGIN SELECT RAISE(ABORT,
        'production_cutover_binding_phase_invalid'); END
    """


_SCHEMA_V13_ASSET_EVIDENCE_GUARD = """
    CREATE TRIGGER business_cutover_asset_provider_bindings_evidence_guard
    BEFORE INSERT ON business_cutover_asset_provider_bindings
    WHEN NOT EXISTS (
        SELECT 1 FROM business_asset_coverage_receipts AS coverage
        WHERE coverage.cutover_id = NEW.cutover_id AND
              coverage.transaction_id = NEW.coverage_transaction_id AND
              coverage.receipt_kind = NEW.coverage_receipt_kind AND
              coverage.committed_at = NEW.coverage_committed_at AND
              coverage.source_admission_revision =
                NEW.source_admission_revision AND
              coverage.source_admission_sha256 =
                NEW.source_admission_sha256 AND
              coverage.manifest_sha256 = NEW.coverage_manifest_sha256 AND
              coverage.coverage_sha256 = NEW.coverage_sha256 AND
              coverage.member_root_sha256 = NEW.coverage_member_root_sha256 AND
              coverage.blob_root_sha256 = NEW.coverage_blob_root_sha256 AND
              coverage.batch_count = NEW.coverage_batch_count AND
              coverage.repository_member_count =
                NEW.coverage_repository_member_count AND
              coverage.live_project_count = NEW.coverage_live_project_count AND
              coverage.deleted_project_count =
                NEW.coverage_deleted_project_count AND
              coverage.observed_occurrence_count =
                NEW.coverage_observed_occurrence_count AND
              coverage.observed_row_count = NEW.coverage_observed_row_count AND
              coverage.observed_manifest_bytes =
                NEW.coverage_observed_manifest_bytes AND
              coverage.project_count = NEW.coverage_project_count AND
              coverage.reference_count = NEW.coverage_reference_count AND
              coverage.unique_blob_count = NEW.coverage_unique_blob_count AND
              coverage.unique_blob_bytes = NEW.coverage_unique_blob_bytes AND
              coverage.referenced_bytes = NEW.coverage_referenced_bytes
    )
    BEGIN SELECT RAISE(ABORT,
        'production_cutover_asset_provider_binding_invalid'); END
"""

_SCHEMA_V13_AUDIT_EVIDENCE_GUARD = """
    CREATE TRIGGER business_cutover_audit_bindings_evidence_guard
    BEFORE INSERT ON business_cutover_audit_bindings
    WHEN NOT EXISTS (
             SELECT 1 FROM business_cutover_asset_provider_bindings AS previous
             WHERE previous.cutover_id = NEW.cutover_id AND
                   previous.repository_epoch = NEW.repository_epoch AND
                   previous.runtime_generation = NEW.runtime_generation AND
                   previous.code_digest = NEW.code_digest AND
                   previous.successor_head_sha256 = NEW.predecessor_head_sha256 AND
                   previous.source_admission_revision =
                        NEW.source_admission_revision AND
                   previous.source_admission_sha256 =
                        NEW.source_admission_sha256 AND
                   previous.source_freeze_sha256 = NEW.source_freeze_sha256 AND
                   previous.file_admission_state = NEW.file_admission_state AND
                   previous.file_admission_epoch = NEW.file_admission_epoch AND
                   previous.file_admission_revision =
                        NEW.file_admission_revision AND
                   previous.file_admission_cutover_id =
                        NEW.file_admission_cutover_id AND
                   previous.file_admission_sha256 =
                        NEW.file_admission_sha256 AND
                   previous.maintenance_scope = NEW.maintenance_scope AND
                   previous.maintenance_repository_proof_sha256 =
                        NEW.maintenance_repository_proof_sha256 AND
                   previous.maintenance_observed_at <=
                        NEW.maintenance_observed_at AND
                   previous.runtime_authority = NEW.runtime_authority AND
                   previous.runtime_authority_state =
                        NEW.runtime_authority_state AND
                   previous.runtime_repository_revision =
                        NEW.runtime_repository_revision AND
                   previous.runtime_state_revision =
                        NEW.runtime_state_revision AND
                   previous.runtime_schema_version = NEW.runtime_schema_version AND
                   previous.runtime_updated_at = NEW.runtime_updated_at AND
                   previous.after_receipt_count = NEW.before_receipt_count AND
                   previous.after_receipt_root_sha256 =
                        NEW.before_receipt_root_sha256 AND
                   previous.worker_head_state_revision =
                        NEW.worker_head_state_revision AND
                   previous.worker_ledger_sequence =
                        NEW.worker_ledger_sequence AND
                   previous.worker_ledger_tail_sha256 =
                        NEW.worker_ledger_tail_sha256 AND
                   previous.worker_head_updated_at =
                        NEW.worker_head_updated_at AND
                   previous.allocation_head_state_revision =
                        NEW.allocation_head_state_revision AND
                   previous.allocation_sequence = NEW.allocation_sequence AND
                   previous.allocation_tail_sha256 =
                        NEW.allocation_tail_sha256 AND
                   previous.allocation_head_updated_at =
                        NEW.allocation_head_updated_at AND
                   previous.dispatch_head_state_revision =
                        NEW.dispatch_head_state_revision AND
                   previous.dispatch_published_sequence =
                        NEW.dispatch_published_sequence AND
                   previous.dispatch_published_repository_revision IS
                        NEW.dispatch_published_repository_revision AND
                   previous.dispatch_published_ordinal IS
                        NEW.dispatch_published_ordinal AND
                   previous.dispatch_inflight_sequence IS
                        NEW.dispatch_inflight_sequence AND
                   previous.dispatch_inflight_repository_revision IS
                        NEW.dispatch_inflight_repository_revision AND
                   previous.dispatch_inflight_ordinal IS
                        NEW.dispatch_inflight_ordinal AND
                   previous.dispatch_head_sha256 = NEW.dispatch_head_sha256 AND
                   previous.dispatch_head_updated_at =
                        NEW.dispatch_head_updated_at
         ) OR NOT EXISTS (
             SELECT 1 FROM repository_audit_certificates AS certificate
             WHERE certificate.certificate_id = NEW.audit_certificate_id AND
                   certificate.cutover_id = NEW.cutover_id AND
                   certificate.sequence = NEW.audit_certificate_sequence AND
                   certificate.purpose = NEW.audit_purpose AND
                   certificate.transaction_id = NEW.audit_transaction_id AND
                   certificate.receipt_kind = NEW.audit_receipt_kind AND
                   certificate.committed_at = NEW.audit_committed_at AND
                   certificate.previous_certificate_id IS
                        NEW.audit_previous_certificate_id AND
                   certificate.previous_manifest_sha256 IS
                        NEW.audit_previous_manifest_sha256 AND
                   certificate.previous_tail_sha256 =
                        NEW.audit_previous_tail_sha256 AND
                   certificate.schema_version = NEW.actual_schema_version AND
                   certificate.migration_ledger_count =
                        NEW.actual_migration_ledger_count AND
                   certificate.migration_ledger_sha256 =
                        NEW.actual_migration_ledger_sha256 AND
                   certificate.receipt_count = NEW.before_receipt_count AND
                   certificate.receipt_root_sha256 =
                        NEW.before_receipt_root_sha256 AND
                   certificate.worker_ledger_sequence =
                        NEW.worker_ledger_sequence AND
                   certificate.worker_ledger_tail_sha256 =
                        NEW.worker_ledger_tail_sha256 AND
                   certificate.findings_sha256 = NEW.audit_findings_sha256 AND
                   certificate.manifest_sha256 = NEW.audit_manifest_sha256 AND
                   certificate.certificate_tail_sha256 =
                        NEW.audit_certificate_tail_sha256
         ) OR NOT EXISTS (
             SELECT 1 FROM repository_audit_head AS head
             WHERE head.singleton = 1 AND
                   head.state_revision = NEW.audit_head_state_revision AND
                   head.sequence = NEW.audit_head_sequence AND
                   head.certificate_id = NEW.audit_head_certificate_id AND
                   head.previous_tail_sha256 =
                        NEW.audit_head_previous_tail_sha256 AND
                   head.tail_sha256 = NEW.audit_head_tail_sha256 AND
                   head.updated_at = NEW.audit_head_updated_at
         )
    BEGIN SELECT RAISE(ABORT,
        'production_cutover_audit_binding_invalid'); END
"""

_SCHEMA_V13_ACTIVATION_EVIDENCE_GUARD = """
    CREATE TRIGGER business_cutover_activation_bindings_evidence_guard
    BEFORE INSERT ON business_cutover_activation_bindings
    WHEN NOT EXISTS (
        SELECT 1 FROM business_cutover_audit_bindings AS previous
        WHERE previous.cutover_id = NEW.cutover_id AND
              previous.repository_epoch = NEW.repository_epoch AND
              previous.runtime_generation = NEW.runtime_generation AND
              previous.code_digest = NEW.code_digest AND
              previous.successor_head_sha256 = NEW.predecessor_head_sha256 AND
              previous.source_admission_revision =
                NEW.source_admission_revision AND
              previous.source_admission_sha256 =
                NEW.source_admission_sha256 AND
              previous.source_freeze_sha256 = NEW.source_freeze_sha256 AND
              previous.file_admission_epoch = NEW.file_admission_epoch AND
              previous.file_admission_cutover_id =
                NEW.file_admission_cutover_id AND
              previous.maintenance_scope = NEW.maintenance_scope AND
              previous.maintenance_repository_proof_sha256 =
                NEW.maintenance_repository_proof_sha256 AND
              previous.maintenance_observed_at <= NEW.maintenance_observed_at AND
              previous.runtime_authority = NEW.runtime_authority AND
              previous.runtime_authority_state = NEW.runtime_authority_state AND
              previous.runtime_repository_revision =
                NEW.runtime_repository_revision AND
              previous.runtime_state_revision = NEW.runtime_state_revision AND
              previous.runtime_schema_version = NEW.runtime_schema_version AND
              previous.runtime_updated_at = NEW.runtime_updated_at AND
              previous.after_receipt_count = NEW.before_receipt_count AND
              previous.after_receipt_root_sha256 =
                NEW.before_receipt_root_sha256 AND
              previous.worker_head_state_revision =
                NEW.worker_head_state_revision AND
              previous.worker_ledger_sequence = NEW.worker_ledger_sequence AND
              previous.worker_ledger_tail_sha256 =
                NEW.worker_ledger_tail_sha256 AND
              previous.worker_head_updated_at = NEW.worker_head_updated_at AND
              previous.allocation_head_state_revision =
                NEW.allocation_head_state_revision AND
              previous.allocation_sequence = NEW.allocation_sequence AND
              previous.allocation_tail_sha256 =
                NEW.allocation_tail_sha256 AND
              previous.allocation_head_updated_at =
                NEW.allocation_head_updated_at AND
              previous.dispatch_head_state_revision =
                NEW.dispatch_head_state_revision AND
              previous.dispatch_published_sequence =
                NEW.dispatch_published_sequence AND
              previous.dispatch_published_repository_revision IS
                NEW.dispatch_published_repository_revision AND
              previous.dispatch_published_ordinal IS
                NEW.dispatch_published_ordinal AND
              previous.dispatch_inflight_sequence IS
                NEW.dispatch_inflight_sequence AND
              previous.dispatch_inflight_repository_revision IS
                NEW.dispatch_inflight_repository_revision AND
              previous.dispatch_inflight_ordinal IS
                NEW.dispatch_inflight_ordinal AND
              previous.dispatch_head_sha256 = NEW.dispatch_head_sha256 AND
              previous.dispatch_head_updated_at = NEW.dispatch_head_updated_at AND
              previous.audit_certificate_id = NEW.audit_certificate_id AND
              previous.audit_certificate_sequence =
                NEW.audit_certificate_sequence AND
              previous.audit_transaction_id = NEW.audit_transaction_id AND
              previous.audit_receipt_kind = NEW.audit_receipt_kind AND
              previous.audit_manifest_sha256 = NEW.audit_manifest_sha256 AND
              previous.audit_certificate_tail_sha256 =
                NEW.audit_certificate_tail_sha256 AND
              previous.audit_completed_at = NEW.audit_completed_at AND
              previous.actual_schema_version = NEW.actual_schema_version AND
              previous.actual_schema_fingerprint =
                NEW.actual_schema_fingerprint AND
              previous.actual_migration_ledger_count =
                NEW.expected_migration_ledger_count AND
              previous.actual_migration_ledger_sha256 =
                NEW.expected_migration_ledger_sha256 AND
              previous.audited_install_manifest_sha256 =
                NEW.install_manifest_sha256
    ) OR NOT EXISTS (
        SELECT 1 FROM business_cutover_activation_receipts AS activation
        WHERE activation.cutover_id = NEW.cutover_id AND
              activation.transaction_id = NEW.activation_transaction_id AND
              activation.receipt_kind = NEW.activation_receipt_kind AND
              activation.committed_at = NEW.activation_committed_at AND
              activation.install_transaction_id = NEW.install_transaction_id AND
              activation.install_manifest_sha256 =
                NEW.install_manifest_sha256 AND
              activation.audit_transaction_id = NEW.audit_transaction_id AND
              activation.audit_manifest_sha256 = NEW.audit_manifest_sha256 AND
              activation.migration_ledger_sha256 =
                NEW.expected_migration_ledger_sha256 AND
              activation.manifest_sha256 = NEW.activation_manifest_sha256
    )
    BEGIN SELECT RAISE(ABORT,
        'production_cutover_activation_binding_invalid'); END
"""

_SCHEMA_V13_MOUNT_EVIDENCE_GUARD = """
    CREATE TRIGGER business_cutover_mount_bindings_evidence_guard
    BEFORE INSERT ON business_cutover_mount_bindings
    WHEN NOT EXISTS (
        SELECT 1 FROM business_cutover_activation_bindings AS previous
        WHERE previous.cutover_id = NEW.cutover_id AND
              previous.repository_epoch = NEW.repository_epoch AND
              previous.runtime_generation = NEW.runtime_generation AND
              previous.code_digest = NEW.code_digest AND
              previous.successor_head_sha256 = NEW.predecessor_head_sha256 AND
              previous.source_admission_revision =
                NEW.source_admission_revision AND
              previous.source_admission_sha256 =
                NEW.source_admission_sha256 AND
              previous.source_freeze_sha256 = NEW.source_freeze_sha256 AND
              previous.file_admission_state = NEW.file_admission_state AND
              previous.file_admission_epoch = NEW.file_admission_epoch AND
              previous.file_admission_revision = NEW.file_admission_revision AND
              previous.file_admission_cutover_id =
                NEW.file_admission_cutover_id AND
              previous.file_admission_sha256 = NEW.file_admission_sha256 AND
              previous.final_admission_sha256 =
                NEW.mount_final_admission_sha256 AND
              previous.maintenance_scope = NEW.maintenance_scope AND
              previous.maintenance_repository_proof_sha256 =
                NEW.maintenance_repository_proof_sha256 AND
              previous.maintenance_observed_at <= NEW.maintenance_observed_at AND
              previous.runtime_authority = NEW.runtime_authority AND
              previous.after_runtime_repository_revision =
                NEW.runtime_repository_revision AND
              previous.after_runtime_state_revision =
                NEW.runtime_state_revision AND
              previous.after_runtime_authority_state =
                NEW.runtime_authority_state AND
              previous.runtime_schema_version = NEW.runtime_schema_version AND
              previous.after_runtime_updated_at = NEW.runtime_updated_at AND
              previous.after_receipt_count = NEW.before_receipt_count AND
              previous.after_receipt_root_sha256 =
                NEW.before_receipt_root_sha256 AND
              previous.worker_head_state_revision =
                NEW.worker_head_state_revision AND
              previous.worker_ledger_sequence = NEW.worker_ledger_sequence AND
              previous.worker_ledger_tail_sha256 =
                NEW.worker_ledger_tail_sha256 AND
              previous.worker_head_updated_at = NEW.worker_head_updated_at AND
              previous.allocation_head_state_revision =
                NEW.allocation_head_state_revision AND
              previous.allocation_sequence = NEW.allocation_sequence AND
              previous.allocation_tail_sha256 =
                NEW.allocation_tail_sha256 AND
              previous.allocation_head_updated_at =
                NEW.allocation_head_updated_at AND
              previous.dispatch_head_state_revision =
                NEW.dispatch_head_state_revision AND
              previous.dispatch_published_sequence =
                NEW.dispatch_published_sequence AND
              previous.dispatch_published_repository_revision IS
                NEW.dispatch_published_repository_revision AND
              previous.dispatch_published_ordinal IS
                NEW.dispatch_published_ordinal AND
              previous.dispatch_inflight_sequence IS
                NEW.dispatch_inflight_sequence AND
              previous.dispatch_inflight_repository_revision IS
                NEW.dispatch_inflight_repository_revision AND
              previous.dispatch_inflight_ordinal IS
                NEW.dispatch_inflight_ordinal AND
              previous.dispatch_head_sha256 = NEW.dispatch_head_sha256 AND
              previous.dispatch_head_updated_at = NEW.dispatch_head_updated_at AND
              previous.activation_transaction_id =
                NEW.activation_transaction_id AND
              previous.activation_receipt_kind =
                NEW.activation_receipt_kind AND
              previous.activation_committed_at = NEW.activation_committed_at AND
              previous.activation_manifest_sha256 =
                NEW.activation_manifest_sha256 AND
              previous.install_transaction_id = NEW.install_transaction_id AND
              previous.install_receipt_kind = NEW.install_receipt_kind AND
              previous.install_manifest_sha256 = NEW.install_manifest_sha256 AND
              previous.audit_certificate_id = NEW.audit_certificate_id AND
              previous.audit_certificate_sequence =
                NEW.audit_certificate_sequence AND
              previous.audit_transaction_id = NEW.audit_transaction_id AND
              previous.audit_receipt_kind = NEW.audit_receipt_kind AND
              previous.audit_manifest_sha256 = NEW.audit_manifest_sha256 AND
              previous.audit_completed_at = NEW.audit_committed_at AND
              previous.actual_schema_version = NEW.target_schema_version AND
              previous.actual_schema_fingerprint =
                NEW.target_schema_fingerprint
    ) OR NOT EXISTS (
        SELECT 1 FROM business_cutover_asset_provider_bindings AS asset
        WHERE asset.cutover_id = NEW.cutover_id AND
              asset.coverage_transaction_id = NEW.coverage_transaction_id AND
              asset.coverage_receipt_kind = NEW.coverage_receipt_kind AND
              asset.coverage_committed_at = NEW.coverage_committed_at AND
              asset.coverage_manifest_sha256 = NEW.coverage_manifest_sha256 AND
              asset.coverage_sha256 = NEW.coverage_sha256 AND
              asset.coverage_member_root_sha256 =
                NEW.coverage_member_root_sha256 AND
              asset.coverage_blob_root_sha256 = NEW.coverage_blob_root_sha256 AND
              asset.coverage_batch_count = NEW.coverage_batch_count AND
              asset.coverage_repository_member_count =
                NEW.coverage_repository_member_count AND
              asset.coverage_live_project_count =
                NEW.coverage_live_project_count AND
              asset.coverage_deleted_project_count =
                NEW.coverage_deleted_project_count AND
              asset.coverage_project_count = NEW.coverage_project_count AND
              asset.coverage_reference_count = NEW.coverage_reference_count AND
              asset.coverage_unique_blob_count = NEW.coverage_unique_blob_count AND
              asset.coverage_unique_blob_bytes = NEW.coverage_unique_blob_bytes AND
              asset.coverage_referenced_bytes = NEW.coverage_referenced_bytes AND
              asset.coverage_observed_occurrence_count =
                NEW.coverage_observed_occurrence_count AND
              asset.coverage_observed_row_count =
                NEW.coverage_observed_row_count AND
              asset.coverage_observed_manifest_bytes =
                NEW.coverage_observed_manifest_bytes
    ) OR NOT EXISTS (
        SELECT 1 FROM business_cutover_mount_receipts AS mount
        WHERE mount.cutover_id = NEW.cutover_id AND
              mount.transaction_id = NEW.mount_transaction_id AND
              mount.receipt_kind = NEW.mount_receipt_kind AND
              mount.committed_at = NEW.mount_committed_at AND
              mount.install_transaction_id = NEW.install_transaction_id AND
              mount.install_receipt_kind = NEW.install_receipt_kind AND
              mount.install_committed_at = NEW.install_committed_at AND
              mount.install_manifest_sha256 = NEW.install_manifest_sha256 AND
              mount.coverage_transaction_id = NEW.coverage_transaction_id AND
              mount.coverage_receipt_kind = NEW.coverage_receipt_kind AND
              mount.coverage_committed_at = NEW.coverage_committed_at AND
              mount.coverage_manifest_sha256 =
                NEW.coverage_manifest_sha256 AND
              mount.audit_transaction_id = NEW.audit_transaction_id AND
              mount.audit_manifest_sha256 = NEW.audit_manifest_sha256 AND
              mount.activation_transaction_id =
                NEW.activation_transaction_id AND
              mount.activation_manifest_sha256 =
                NEW.activation_manifest_sha256 AND
              mount.repository_epoch = NEW.repository_epoch AND
              mount.runtime_generation = NEW.runtime_generation AND
              mount.code_digest = NEW.code_digest AND
              mount.manifest_sha256 = NEW.mount_manifest_sha256
    )
    BEGIN SELECT RAISE(ABORT,
        'production_cutover_mount_binding_invalid'); END
"""

_SCHEMA_V13_RECEIPT_HISTORY_GUARDS = (
    """
    CREATE TRIGGER transaction_receipts_v13_no_update_after_cutover_head
    BEFORE UPDATE ON transaction_receipts
    WHEN EXISTS (SELECT 1 FROM business_cutover_head)
    BEGIN SELECT RAISE(ABORT, 'production_receipt_history_append_only'); END
    """,
    """
    CREATE TRIGGER transaction_receipts_v13_no_delete_after_cutover_head
    BEFORE DELETE ON transaction_receipts
    WHEN EXISTS (SELECT 1 FROM business_cutover_head)
    BEGIN SELECT RAISE(ABORT, 'production_receipt_history_append_only'); END
    """,
)


DIRECT_PREDECESSOR_BINDING_MIGRATION = Migration(
    version=13,
    name="direct_predecessor_phase_bindings_v1",
    statements=(
        """
        CREATE TABLE schema_v13_direct_predecessor_preflight (
            verified INTEGER NOT NULL
                CONSTRAINT schema_v13_direct_predecessor_precondition
                CHECK(verified = 1)
        ) STRICT
        """,
        "INSERT INTO schema_v13_direct_predecessor_preflight(verified) "
        "SELECT CASE WHEN " +
        SCHEMA_V13_DIRECT_PREDECESSOR_PREFLIGHT_PREDICATE +
        " THEN 1 ELSE 0 END",
        _SCHEMA_V13_ASSET_BINDING_TABLE,
        _SCHEMA_V13_AUDIT_BINDING_TABLE,
        _SCHEMA_V13_ACTIVATION_BINDING_TABLE,
        _SCHEMA_V13_MOUNT_BINDING_TABLE,
        """
        CREATE UNIQUE INDEX business_cutover_asset_provider_binding_transition
        ON business_cutover_asset_provider_bindings(
            cutover_id, predecessor_head_sha256, successor_head_sha256
        )
        """,
        """
        CREATE UNIQUE INDEX business_cutover_asset_provider_checkpoint
        ON business_cutover_asset_provider_bindings(
            provider_id, provider_generation, provider_checkpoint_sequence
        )
        """,
        """
        CREATE UNIQUE INDEX business_cutover_audit_binding_transition
        ON business_cutover_audit_bindings(
            cutover_id, predecessor_head_sha256, successor_head_sha256
        )
        """,
        """
        CREATE UNIQUE INDEX business_cutover_activation_binding_transition
        ON business_cutover_activation_bindings(
            cutover_id, predecessor_head_sha256, successor_head_sha256
        )
        """,
        """
        CREATE UNIQUE INDEX business_cutover_mount_binding_transition
        ON business_cutover_mount_bindings(
            cutover_id, predecessor_head_sha256, successor_head_sha256
        )
        """,
        _schema_v13_binding_snapshot_guard(
            "business_cutover_asset_provider_bindings", 1
        ),
        _schema_v13_binding_snapshot_guard(
            "business_cutover_audit_bindings", 2
        ),
        _schema_v13_binding_snapshot_guard(
            "business_cutover_activation_bindings", 3
        ),
        _schema_v13_binding_snapshot_guard(
            "business_cutover_mount_bindings", 4
        ),
        _SCHEMA_V13_ASSET_EVIDENCE_GUARD,
        _SCHEMA_V13_AUDIT_EVIDENCE_GUARD,
        _SCHEMA_V13_ACTIVATION_EVIDENCE_GUARD,
        _SCHEMA_V13_MOUNT_EVIDENCE_GUARD,
        _schema_v13_specialised_phase_guard(
            "business_cutover_audit_bindings", 2
        ),
        _schema_v13_specialised_phase_guard(
            "business_cutover_activation_bindings", 3
        ),
        _schema_v13_specialised_phase_guard(
            "business_cutover_mount_bindings", 4
        ),
        _schema_v13_head_reverse_guard(
            "business_cutover_asset_provider_bindings", 1, 2
        ),
        _schema_v13_head_reverse_guard(
            "business_cutover_audit_bindings", 2, 3
        ),
        _schema_v13_head_reverse_guard(
            "business_cutover_activation_bindings", 3, 4
        ),
        _schema_v13_head_reverse_guard(
            "business_cutover_mount_bindings", 4, 5
        ),
        *_SCHEMA_V13_RECEIPT_HISTORY_GUARDS,
        *_production_append_only_triggers(
            "business_cutover_asset_provider_bindings"
        ),
        *_production_append_only_triggers("business_cutover_audit_bindings"),
        *_production_append_only_triggers(
            "business_cutover_activation_bindings"
        ),
        *_production_append_only_triggers("business_cutover_mount_bindings"),
        "DROP TABLE schema_v13_direct_predecessor_preflight",
    ),
)
MIGRATIONS: Tuple[Migration, ...] = (
    KERNEL_MIGRATION,
    BUSINESS_SNAPSHOT_MIGRATION,
    LEGACY_IMPORT_LEDGER_MIGRATION,
    LEGACY_CATALOG_SEAL_MIGRATION,
    LEGACY_REPOSITORY_PROOF_MIGRATION,
    BUSINESS_TRANSACTION_MIGRATION,
    LOCAL_REVISION_MIGRATION,
    PRODUCTION_AUTHORITY_WORKER_MIGRATION,
    WORKER_PAYLOAD_PROJECTION_MIGRATION,
    RUNTIME_SCHEMA_HARDENING_MIGRATION,
    MOUNT_CHRONOLOGY_MIGRATION,
    INSTALL_CLOSURE_MIGRATION,
    DIRECT_PREDECESSOR_BINDING_MIGRATION,
)


def _migration_ledger_entries_for_registry(
    schema_version: Optional[int] = None,
) -> Tuple[MigrationLedgerEntry, ...]:
    """Project one exact registry prefix into the canonical ledger contract."""

    version = MIGRATIONS[-1].version if schema_version is None else schema_version
    if type(version) is not int or version < 1 or version > MIGRATIONS[-1].version:
        raise ValueError("schema version is outside the immutable registry")
    migrations = tuple(item for item in MIGRATIONS if item.version <= version)
    if tuple(item.version for item in migrations) != tuple(
        range(1, version + 1)
    ):
        raise ValueError("migration registry is not one contiguous prefix")
    return tuple(
        MigrationLedgerEntry(
            version=item.version,
            name=item.name,
            checksum_scheme=item.checksum_scheme,
            checksum=item.checksum,
        )
        for item in migrations
    )


def _expected_migration_ledger_sha256(
    schema_version: Optional[int] = None,
) -> bytes:
    """Return the deployment-independent root expected for one schema version."""

    return _migration_ledger_sha256(
        _migration_ledger_entries_for_registry(schema_version)
    )

BUSINESS_TABLES = ("projects", "documents", "entities")
IMPORT_LEDGER_TABLES = ("legacy_import_batches", "legacy_import_projects")
CATALOG_LEDGER_TABLES = ("legacy_catalog_seals", "legacy_catalog_members")
REPOSITORY_PROOF_TABLES = (
    "legacy_repository_proof_control",
    "legacy_repository_seals",
    "legacy_repository_members",
)
BUSINESS_TRANSACTION_TABLES = (
    "repository_runtime_control",
    "repository_projects",
    "document_commit_heads",
    "business_commits",
    "business_operations",
    "business_operation_targets",
    "idempotency_inbox",
    "event_outbox",
    "event_outbox_dispatch_control",
    "confirmation_requests",
    "deferred_actions",
    "deferred_action_attempts",
    "pipeline_jobs",
    "pipeline_job_attempts",
    "pipeline_job_transitions",
    "asset_blobs",
    "document_asset_refs",
)
PRODUCTION_RUNTIME_V8_TABLES = (
    "business_cutover_head",
    "business_cutover_install_receipts",
    "business_asset_stage_receipts",
    "business_asset_stage_members",
    "business_asset_coverage_receipts",
    "repository_audit_certificates",
    "repository_audit_head",
    "business_cutover_activation_receipts",
    "business_cutover_mount_receipts",
    "atomic_business_write_receipts",
    "atomic_business_control_members",
    "worker_ledger_head",
    "worker_ledger_entries",
    "worker_execution_receipts",
    "pipeline_control_intents",
    "pipeline_control_receipts",
    "worker_targets",
    "worker_target_current",
    "worker_attempts",
    "worker_effects",
    "worker_results",
    "worker_reconcile_proofs",
    "outbox_allocations",
    "outbox_allocation_head",
    "outbox_dispatch_head",
)
WORKER_PROJECTION_SIDECAR_TABLES = (
    "worker_effect_request_projections",
    "worker_result_payload_projections",
    "worker_proof_payload_projections",
)
CUTOVER_INSTALL_CLOSURE_TABLES = (
    "business_cutover_install_artifacts",
    "business_cutover_install_artifact_chunks",
    "business_cutover_baseline_atomic_receipts",
    "business_cutover_install_closures",
)
DIRECT_PREDECESSOR_BINDING_TABLES = (
    "business_cutover_asset_provider_bindings",
    "business_cutover_audit_bindings",
    "business_cutover_activation_bindings",
    "business_cutover_mount_bindings",
)
PRODUCTION_RUNTIME_TABLES = (
    *PRODUCTION_RUNTIME_V8_TABLES,
    *WORKER_PROJECTION_SIDECAR_TABLES,
    *CUTOVER_INSTALL_CLOSURE_TABLES,
    *DIRECT_PREDECESSOR_BINDING_TABLES,
)
LEDGER_TABLES = (
    *IMPORT_LEDGER_TABLES,
    *CATALOG_LEDGER_TABLES,
    *REPOSITORY_PROOF_TABLES,
)

PRIVATE_TABLES = frozenset(
    (
        "schema_migrations",
        "kernel_metadata",
        "kernel_repository",
        "transaction_receipts",
        *BUSINESS_TABLES,
        *LEDGER_TABLES,
        *BUSINESS_TRANSACTION_TABLES,
        *PRODUCTION_RUNTIME_TABLES,
    )
)


__all__ = [
    "BUSINESS_SNAPSHOT_MIGRATION",
    "BUSINESS_TABLES",
    "BUSINESS_TRANSACTION_KIND",
    "BUSINESS_TRANSACTION_MIGRATION",
    "BUSINESS_TRANSACTION_PREFIX",
    "BUSINESS_TRANSACTION_TABLES",
    "CATALOG_LEDGER_TABLES",
    "CUTOVER_INSTALL_CLOSURE_TABLES",
    "DIRECT_PREDECESSOR_BINDING_MIGRATION",
    "DIRECT_PREDECESSOR_BINDING_TABLES",
    "EMPTY_WORKER_PROOF_PROJECTION_SHA256",
    "EXACT_CHECKSUM_SCHEME",
    "IMPORT_LEDGER_TABLES",
    "INSTALL_CLOSURE_MIGRATION",
    "KERNEL_MIGRATION",
    "LEGACY_IMPORT_LEDGER_MIGRATION",
    "LEGACY_CATALOG_SEAL_MIGRATION",
    "LEGACY_REPOSITORY_PROOF_MIGRATION",
    "LEGACY_CHECKSUM_SCHEME",
    "LOCAL_REVISION_MIGRATION",
    "LOCAL_REVISION_PREFLIGHT_PREDICATE",
    "MOUNT_CHRONOLOGY_MIGRATION",
    "MIGRATIONS",
    "LEDGER_TABLES",
    "MAX_BUSINESS_CUTOVER_ARTIFACT_BYTES",
    "MAX_BUSINESS_CUTOVER_ARTIFACT_CHUNK_BYTES",
    "MAX_BUSINESS_CUTOVER_ARTIFACT_CHUNKS",
    "Migration",
    "PRIVATE_TABLES",
    "PRODUCTION_AUTHORITY_WORKER_MIGRATION",
    "PRODUCTION_RUNTIME_PREFLIGHT_PREDICATE",
    "PRODUCTION_RUNTIME_TABLES",
    "PRODUCTION_RUNTIME_V8_TABLES",
    "REPOSITORY_PROOF_TABLES",
    "RUNTIME_SCHEMA_HARDENING_MIGRATION",
    "SCHEMA_V11_MOUNT_CHRONOLOGY_PREFLIGHT_PREDICATE",
    "SCHEMA_V12_INSTALL_CLOSURE_PREFLIGHT_PREDICATE",
    "SCHEMA_V13_DIRECT_PREDECESSOR_PREFLIGHT_PREDICATE",
    "SCHEMA_V10_HARDENING_PREFLIGHT_PREDICATE",
    "WORKER_PAYLOAD_PROJECTION_MIGRATION",
    "WORKER_PAYLOAD_PROJECTION_PREFLIGHT_PREDICATE",
    "WORKER_PROJECTION_SIDECAR_TABLES",
    "legacy_normalize_schema_sql",
    "normalize_schema_sql",
]
