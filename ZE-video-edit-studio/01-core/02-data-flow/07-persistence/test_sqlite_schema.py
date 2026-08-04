"""Database-level evidence for the immutable SQLite schema registry."""

from __future__ import annotations

import sqlite3
import sys
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Optional

import pytest


_HERE = Path(__file__).resolve().parent
_DATA_STRUCTURE = _HERE.parents[1] / "01-data-structure"
for _directory in (
    _HERE,
    _DATA_STRUCTURE / "01-document",
    _DATA_STRUCTURE / "02-entity",
    _DATA_STRUCTURE / "04-field-types",
):
    if str(_directory) not in sys.path:
        sys.path.insert(0, str(_directory))

import legacy_catalog_contract as catalog_contract
import legacy_import_contract as import_contract
import legacy_repository_contract as repository_contract
import sqlite_schema as schema
import sqlite_kernel as kernel
from migration_ledger_contract import MigrationLedgerEntry
from migration_ledger_integrity import _migration_ledger_sha256


_NOW = "2026-07-11T00:00:00.000Z"


def test_migration_ledger_root_is_one_canonical_registry_identity() -> None:
    entries = schema._migration_ledger_entries_for_registry()
    assert len(entries) == 13
    assert tuple(value.version for value in entries) == tuple(
        range(1, len(schema.MIGRATIONS) + 1)
    )
    assert schema._expected_migration_ledger_sha256(8).hex() == (
        "9595ea544b5b055f71d2c93619e49b3353a5d48424951fc410b0ca2484c1834a"
    )
    assert schema._expected_migration_ledger_sha256(6) != (
        schema._expected_migration_ledger_sha256(7)
    )
    assert schema._expected_migration_ledger_sha256(12).hex() == (
        "1537102bc6fe371ceef49a58b566dbd6f86940eaf838dcfeb263e3df37cbb77e"
    )
    assert schema._expected_migration_ledger_sha256(13).hex() == (
        "00eacfaef1b42673d0d22478eb0b3675eed3d4b42bb75b842e3a28b14355a23a"
    )
    with pytest.raises(ValueError):
        schema._expected_migration_ledger_sha256(0)


def test_actual_schema_migration_rows_project_to_the_registry_root() -> None:
    connection = _connection()
    try:
        _apply_migrations(connection, schema.MIGRATIONS)
        rows = connection.execute(
            "SELECT version, name, checksum FROM schema_migrations "
            "ORDER BY version"
        ).fetchall()
        entries = tuple(
            MigrationLedgerEntry(
                version=row["version"],
                name=row["name"],
                checksum_scheme=migration.checksum_scheme,
                checksum=row["checksum"],
            )
            for row, migration in zip(rows, schema.MIGRATIONS)
        )
        assert entries == schema._migration_ledger_entries_for_registry()
        assert _migration_ledger_sha256(entries) == (
            schema._expected_migration_ledger_sha256()
        )
    finally:
        connection.close()


def _connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _apply_migrations(
    connection: sqlite3.Connection,
    migrations: Iterable[schema.Migration],
) -> None:
    connection.execute("BEGIN IMMEDIATE")
    try:
        for migration in migrations:
            for statement in migration.statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations(version, name, checksum, applied_at) "
                "VALUES (?, ?, ?, ?)",
                (migration.version, migration.name, migration.checksum, _NOW),
            )
            if migration.version == 1:
                connection.execute(
                    "INSERT INTO kernel_repository(singleton, authority, created_at) "
                    "VALUES (1, 'legacy', ?)",
                    (_NOW,),
                )
            connection.execute("PRAGMA user_version = %d" % migration.version)
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise


def _receipt(
    connection: sqlite3.Connection,
    transaction_id: str,
    kind: str,
) -> None:
    connection.execute(
        "INSERT INTO transaction_receipts(transaction_id, kind, committed_at) "
        "VALUES (?, ?, ?)",
        (transaction_id, kind, _NOW),
    )


def _dirty_one_strict_row(
    connection: sqlite3.Connection,
    table: str,
) -> None:
    """Inject one corrupt prototype row while restoring the exact v7 triggers.

    The migration preflight is a last-resort fail-closed boundary, so it must
    reject rows even when an older or damaged writer bypassed table CHECK/FK
    validation.  SQLite's test-only pragmas let this exercise every old table
    without weakening the immutable production schema.
    """

    if table not in schema.BUSINESS_TRANSACTION_TABLES:
        raise ValueError("table is outside the frozen v7 transaction namespace")
    triggers = [
        (row["name"], row["sql"])
        for row in connection.execute(
            "SELECT name, sql FROM sqlite_schema "
            "WHERE type='trigger' AND tbl_name=? ORDER BY name",
            (table,),
        ).fetchall()
    ]
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute("PRAGMA ignore_check_constraints=ON")
    try:
        for name, _sql in triggers:
            connection.execute('DROP TRIGGER "' + name + '"')
        columns = [
            row
            for row in connection.execute(
                'PRAGMA table_info("' + table + '")'
            ).fetchall()
            if row["notnull"] or row["pk"]
        ]
        values = []
        for row in columns:
            declared = (row["type"] or "").upper()
            if "INT" in declared:
                values.append(1)
            elif "BLOB" in declared:
                values.append(b"x" * 32)
            else:
                values.append("x")
        quoted = ", ".join('"' + row["name"] + '"' for row in columns)
        placeholders = ", ".join("?" for _row in columns)
        connection.execute(
            'INSERT INTO "' + table + '" (' + quoted + ") VALUES (" +
            placeholders + ")",
            values,
        )
        for _name, sql in triggers:
            assert sql is not None
            connection.execute(sql)
    finally:
        connection.execute("PRAGMA ignore_check_constraints=OFF")
        connection.execute("PRAGMA foreign_keys=ON")


def _catalog_values(**overrides: Any) -> dict[str, Any]:
    catalog_id = "c" * 64
    values: dict[str, Any] = {
        "catalog_id": catalog_id,
        "transaction_id": (
            catalog_contract.LEGACY_CATALOG_TRANSACTION_PREFIX + catalog_id
        ),
        "receipt_kind": catalog_contract.LEGACY_CATALOG_TRANSACTION_KIND,
        "receipt_protocol": catalog_contract.LEGACY_CATALOG_RECEIPT_PROTOCOL,
        "import_id": None,
        "seal_protocol": catalog_contract.LEGACY_CATALOG_SEAL_PROTOCOL,
        "manifest_schema": catalog_contract.LEGACY_CATALOG_MANIFEST_SCHEMA,
        "status": catalog_contract.LEGACY_CATALOG_STATUS,
        "selection": catalog_contract.LEGACY_CATALOG_SELECTION,
        "project_count": 0,
        "entity_count": 0,
        "source_head_bytes": 0,
        "row_snapshot_bytes": 0,
        "manifest_bytes": b"{}",
        "manifest_sha256": b"m" * 32,
        "source_catalog_sha256": b"s" * 32,
        "target_coverage_sha256": b"t" * 32,
        "seal_sha256": b"z" * 32,
        "target_schema_version": (
            catalog_contract.LEGACY_CATALOG_TARGET_SCHEMA_VERSION
        ),
        "schema_fingerprint": "d" * 64,
        "cutover_ready": catalog_contract.LEGACY_CATALOG_CUTOVER_READY,
    }
    values.update(overrides)
    return values


_CATALOG_COLUMNS = (
    "catalog_id",
    "transaction_id",
    "receipt_kind",
    "receipt_protocol",
    "import_id",
    "seal_protocol",
    "manifest_schema",
    "status",
    "selection",
    "project_count",
    "entity_count",
    "source_head_bytes",
    "row_snapshot_bytes",
    "manifest_bytes",
    "manifest_sha256",
    "source_catalog_sha256",
    "target_coverage_sha256",
    "seal_sha256",
    "target_schema_version",
    "schema_fingerprint",
    "cutover_ready",
)


def _insert_catalog_seal(
    connection: sqlite3.Connection,
    **overrides: Any,
) -> dict[str, Any]:
    values = _catalog_values(**overrides)
    placeholders = ", ".join("?" for _name in _CATALOG_COLUMNS)
    connection.execute(
        "INSERT INTO legacy_catalog_seals(%s) VALUES (%s)"
        % (", ".join(_CATALOG_COLUMNS), placeholders),
        tuple(values[name] for name in _CATALOG_COLUMNS),
    )
    return values


def _insert_import_batch_and_descriptor(
    connection: sqlite3.Connection,
    *,
    import_id: str = "a" * 64,
    source_head_bytes: int = 2,
) -> dict[str, Any]:
    transaction_id = import_contract.SQLITE_IMPORT_TRANSACTION_PREFIX + import_id
    _receipt(connection, transaction_id, import_contract.SQLITE_IMPORT_TRANSACTION_KIND)
    connection.execute(
        """
        INSERT INTO legacy_import_batches(
            import_id, transaction_id, receipt_kind, receipt_protocol, status,
            manifest_schema, batch_protocol, payload_protocol, selection,
            project_count, entity_count, row_snapshot_bytes,
            framed_payload_bytes, manifest_bytes, manifest_sha256,
            payload_sha256, batch_sha256
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, 4, 8, ?, ?, ?, ?)
        """,
        (
            import_id,
            transaction_id,
            import_contract.SQLITE_IMPORT_TRANSACTION_KIND,
            import_contract.SQLITE_IMPORT_RECEIPT_PROTOCOL,
            import_contract.SQLITE_IMPORT_TERMINAL_STATUS,
            import_contract.LEGACY_MANIFEST_SCHEMA,
            import_contract.LEGACY_BATCH_PROTOCOL,
            import_contract.LEGACY_PAYLOAD_PROTOCOL,
            "explicit_projects",
            b"{}",
            b"m" * 32,
            b"p" * 32,
            b"b" * 32,
        ),
    )
    descriptor = {
        "import_id": import_id,
        "ordinal": 0,
        "project_id": "project-a",
        "generation": 3,
        "document_id": "document-a",
        "document_version": 7,
        "storage_version": "v2",
        "source_head_sha256": b"h" * 32,
        "source_head_bytes": source_head_bytes,
        "business_root_sha256": b"r" * 32,
        "entity_count": 0,
        "row_snapshot_bytes": 4,
    }
    connection.execute(
        """
        INSERT INTO legacy_import_projects(
            import_id, ordinal, project_id, generation, document_id,
            document_version, storage_version, source_head_sha256,
            source_head_bytes, business_root_sha256, entity_count,
            row_snapshot_bytes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        tuple(descriptor.values()),
    )
    return descriptor


def _insert_catalog_member(
    connection: sqlite3.Connection,
    descriptor: dict[str, Any],
    **overrides: Any,
) -> None:
    values = dict(descriptor)
    values.update(overrides)
    columns = ("catalog_id", *descriptor.keys())
    payload = {"catalog_id": "c" * 64, **values}
    connection.execute(
        "INSERT INTO legacy_catalog_members(%s) VALUES (%s)"
        % (
            ", ".join(columns),
            ", ".join("?" for _name in columns),
        ),
        tuple(payload[name] for name in columns),
    )


def test_v4_registry_remains_frozen_before_v5_repository_append() -> None:
    assert [(item.version, item.name) for item in schema.MIGRATIONS[:4]] == [
        (1, "sqlite_kernel_v1"),
        (2, "business_snapshot_schema_v1"),
        (3, "legacy_import_ledger_v1"),
        (4, "legacy_catalog_seal_v1"),
    ]
    assert (schema.MIGRATIONS[4].version, schema.MIGRATIONS[4].name) == (
        5,
        "legacy_repository_proof_v1",
    )
    assert schema.CATALOG_LEDGER_TABLES == (
        "legacy_catalog_seals",
        "legacy_catalog_members",
    )
    assert schema.LEDGER_TABLES == (
        *schema.IMPORT_LEDGER_TABLES,
        *schema.CATALOG_LEDGER_TABLES,
        *schema.REPOSITORY_PROOF_TABLES,
    )
    assert set(schema.LEDGER_TABLES).issubset(schema.PRIVATE_TABLES)

    connection = _connection()
    try:
        _apply_migrations(connection, schema.MIGRATIONS)
        objects = {
            (row["type"], row["name"])
            for row in connection.execute(
                "SELECT type, name FROM sqlite_schema "
                "WHERE name LIKE 'legacy_catalog_%'"
            ).fetchall()
        }
        assert objects == {
            ("index", "legacy_catalog_seals_repository_identity"),
            ("index", "legacy_catalog_members_repository_identity"),
            ("table", "legacy_catalog_seals"),
            ("table", "legacy_catalog_members"),
            ("trigger", "legacy_catalog_seals_no_update"),
            ("trigger", "legacy_catalog_seals_no_delete"),
            ("trigger", "legacy_catalog_members_no_update"),
            ("trigger", "legacy_catalog_members_no_delete"),
        }
        for table in schema.CATALOG_LEDGER_TABLES:
            ddl = connection.execute(
                "SELECT sql FROM sqlite_schema WHERE type='table' AND name=?",
                (table,),
            ).fetchone()[0]
            assert ddl.rstrip().endswith("STRICT")
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_empty_catalog_seal_is_receipted_but_never_cutover_ready() -> None:
    connection = _connection()
    try:
        _apply_migrations(connection, schema.MIGRATIONS)
        connection.execute("BEGIN")
        values = _insert_catalog_seal(connection)
        _receipt(connection, values["transaction_id"], values["receipt_kind"])
        connection.execute("COMMIT")

        row = connection.execute(
            "SELECT import_id, project_count, entity_count, "
            "source_head_bytes, row_snapshot_bytes, target_schema_version, "
            "cutover_ready "
            "FROM legacy_catalog_seals"
        ).fetchone()
        assert tuple(row) == (None, 0, 0, 0, 0, 4, 0)

        # Empty seals cannot acquire a member: a member's non-null import id
        # cannot match the seal's nullable (catalog_id, import_id) parent key.
        connection.execute("BEGIN")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_catalog_member(
                connection,
                {
                    "import_id": "a" * 64,
                    "ordinal": 0,
                    "project_id": "project-a",
                    "generation": 1,
                    "document_id": "document-a",
                    "document_version": 1,
                    "storage_version": "v2",
                    "source_head_sha256": b"h" * 32,
                    "source_head_bytes": 2,
                    "business_root_sha256": b"r" * 32,
                    "entity_count": 0,
                    "row_snapshot_bytes": 4,
                },
            )
            connection.execute("COMMIT")
        connection.execute("ROLLBACK")
    finally:
        connection.close()


def test_nonempty_catalog_member_is_exactly_bound_to_import_descriptor() -> None:
    connection = _connection()
    try:
        _apply_migrations(connection, schema.MIGRATIONS)
        connection.execute("BEGIN")
        descriptor = _insert_import_batch_and_descriptor(connection)
        values = _insert_catalog_seal(
            connection,
            import_id=descriptor["import_id"],
            project_count=1,
            source_head_bytes=descriptor["source_head_bytes"],
            row_snapshot_bytes=descriptor["row_snapshot_bytes"],
        )
        _receipt(connection, values["transaction_id"], values["receipt_kind"])
        _insert_catalog_member(connection, descriptor)
        connection.execute("COMMIT")
        assert connection.execute(
            "SELECT count(*) FROM legacy_catalog_members"
        ).fetchone()[0] == 1

        # Every copied descriptor field participates in the parent identity;
        # changing a digest cannot create a convincing historical member.
        second = _connection()
        try:
            _apply_migrations(second, schema.MIGRATIONS)
            second.execute("BEGIN")
            exact = _insert_import_batch_and_descriptor(second)
            seal = _insert_catalog_seal(
                second,
                import_id=exact["import_id"],
                project_count=1,
                source_head_bytes=exact["source_head_bytes"],
                row_snapshot_bytes=exact["row_snapshot_bytes"],
            )
            _receipt(second, seal["transaction_id"], seal["receipt_kind"])
            _insert_catalog_member(second, exact, source_head_sha256=b"x" * 32)
            with pytest.raises(sqlite3.IntegrityError):
                second.execute("COMMIT")
            second.execute("ROLLBACK")
        finally:
            second.close()
    finally:
        connection.close()


def test_catalog_member_enforces_the_per_project_source_head_budget() -> None:
    connection = _connection()
    try:
        _apply_migrations(connection, schema.MIGRATIONS)
        connection.execute("BEGIN")
        descriptor = _insert_import_batch_and_descriptor(
            connection,
            source_head_bytes=(
                catalog_contract.MAX_LEGACY_CATALOG_PROJECT_SOURCE_HEAD_BYTES + 1
            ),
        )
        values = _insert_catalog_seal(
            connection,
            import_id=descriptor["import_id"],
            project_count=1,
            source_head_bytes=descriptor["source_head_bytes"],
            row_snapshot_bytes=descriptor["row_snapshot_bytes"],
        )
        _receipt(connection, values["transaction_id"], values["receipt_kind"])
        with pytest.raises(sqlite3.IntegrityError):
            _insert_catalog_member(connection, descriptor)
        connection.execute("ROLLBACK")
    finally:
        connection.close()


@pytest.mark.parametrize(
    "overrides",
    (
        {"catalog_id": "C" * 64},
        {"receipt_protocol": "wrong"},
        {"status": "cutover_ready"},
        {"selection": "explicit_projects"},
        {"target_schema_version": 3},
        {"target_schema_version": 5},
        {"manifest_bytes": "{}"},
        {"seal_sha256": b"short"},
        {"schema_fingerprint": "D" * 64},
        {"cutover_ready": 1},
        {"import_id": "a" * 64},
        {"project_count": 1},
    ),
)
def test_catalog_seal_illegal_values_fail_closed(overrides: dict[str, Any]) -> None:
    connection = _connection()
    try:
        _apply_migrations(connection, schema.MIGRATIONS)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_catalog_seal(connection, **overrides)
    finally:
        connection.close()


def test_catalog_receipt_fk_and_append_only_triggers_are_enforced() -> None:
    connection = _connection()
    try:
        _apply_migrations(connection, schema.MIGRATIONS)
        connection.execute("BEGIN")
        _insert_catalog_seal(connection)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("COMMIT")
        connection.execute("ROLLBACK")

        connection.execute("BEGIN")
        descriptor = _insert_import_batch_and_descriptor(connection)
        values = _insert_catalog_seal(
            connection,
            import_id=descriptor["import_id"],
            project_count=1,
            source_head_bytes=descriptor["source_head_bytes"],
            row_snapshot_bytes=descriptor["row_snapshot_bytes"],
        )
        _receipt(connection, values["transaction_id"], values["receipt_kind"])
        _insert_catalog_member(connection, descriptor)
        connection.execute("COMMIT")

        for statement in (
            "UPDATE legacy_catalog_seals SET cutover_ready=0",
            "DELETE FROM legacy_catalog_seals",
            "UPDATE legacy_catalog_members SET generation=generation",
            "DELETE FROM legacy_catalog_members",
        ):
            with pytest.raises(
                sqlite3.IntegrityError, match="legacy_catalog_append_only"
            ):
                connection.execute(statement)
    finally:
        connection.close()


@pytest.mark.parametrize("with_terminal_shadow", (False, True))
def test_v3_to_v4_upgrade_preserves_empty_or_terminal_legacy_state(
    with_terminal_shadow: bool,
) -> None:
    connection = _connection()
    try:
        _apply_migrations(connection, schema.MIGRATIONS[:3])
        if with_terminal_shadow:
            connection.execute("BEGIN")
            descriptor = _insert_import_batch_and_descriptor(connection)
            connection.execute(
                "INSERT INTO projects(repository_singleton, project_id, generation, "
                "codec_id, codec_version, snapshot_format_version, snapshot_bytes, "
                "snapshot_sha256) VALUES (1, ?, ?, 'strict-json', 1, 1, ?, ?)",
                (
                    descriptor["project_id"],
                    descriptor["generation"],
                    b"{}",
                    descriptor["business_root_sha256"],
                ),
            )
            connection.execute(
                "INSERT INTO documents(project_id, document_id, document_version, "
                "schema_version, collaboration_mode, save_policy, codec_id, "
                "codec_version, snapshot_format_version, snapshot_bytes, "
                "snapshot_sha256) VALUES (?, ?, ?, '1.0.0', 'live_collab', "
                "'auto_commit', 'strict-json', 1, 1, ?, ?)",
                (
                    descriptor["project_id"],
                    descriptor["document_id"],
                    descriptor["document_version"],
                    b"{}",
                    b"d" * 32,
                ),
            )
            connection.execute(
                "INSERT INTO kernel_metadata(key, value) VALUES (?, ?)",
                (
                    import_contract.SQLITE_IMPORT_STATE_KEY,
                    import_contract.SQLITE_IMPORT_STATE_TERMINAL_SHADOW,
                ),
            )
            connection.execute("COMMIT")

        before = {
            "authority": connection.execute(
                "SELECT authority FROM kernel_repository WHERE singleton=1"
            ).fetchone()[0],
            "batches": connection.execute(
                "SELECT count(*) FROM legacy_import_batches"
            ).fetchone()[0],
            "projects": connection.execute(
                "SELECT count(*) FROM projects"
            ).fetchone()[0],
        }
        _apply_migrations(connection, schema.MIGRATIONS[3:4])
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert [
            tuple(row)
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        ] == [(1,), (2,), (3,), (4,)]
        assert before == {
            "authority": connection.execute(
                "SELECT authority FROM kernel_repository WHERE singleton=1"
            ).fetchone()[0],
            "batches": connection.execute(
                "SELECT count(*) FROM legacy_import_batches"
            ).fetchone()[0],
            "projects": connection.execute(
                "SELECT count(*) FROM projects"
            ).fetchone()[0],
        }
        assert before["authority"] == "legacy"
        assert all(
            connection.execute("SELECT count(*) FROM %s" % table).fetchone()[0]
            == 0
            for table in schema.CATALOG_LEDGER_TABLES
        )
    finally:
        connection.close()


def test_v4_ddl_drift_failure_rolls_back_indexes_ledger_and_version() -> None:
    connection = _connection()
    try:
        _apply_migrations(connection, schema.MIGRATIONS[:3])
        connection.execute("CREATE TABLE legacy_catalog_seals(drift TEXT)")

        with pytest.raises(sqlite3.OperationalError):
            _apply_migrations(connection, schema.MIGRATIONS[3:])

        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert [
            tuple(row)
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        ] == [(1,), (2,), (3,)]
        assert connection.execute(
            "PRAGMA table_info(legacy_catalog_seals)"
        ).fetchone()[1] == "drift"
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE name IN "
                "('legacy_import_batches_catalog_counts', "
                "'legacy_import_projects_catalog_identity', "
                "'legacy_catalog_members')"
            ).fetchall()
        }
        assert names == set()
    finally:
        connection.close()


def _insert_repository_proof(
    connection: sqlite3.Connection,
    *,
    catalog_id: str = "c" * 64,
    catalog_seal_sha256: bytes = b"z" * 32,
    live_count: int = 0,
    deleted_count: int = 0,
) -> None:
    cutover_id = "f" * 64
    transaction_id = (
        repository_contract.LEGACY_REPOSITORY_TRANSACTION_PREFIX + cutover_id
    )
    connection.execute(
        """
        INSERT INTO legacy_repository_proof_control(
            singleton, authority, authority_state, repository_epoch,
            repository_revision, catalog_revision, cutover_id,
            runtime_generation, code_digest, cutover_ready
        ) VALUES (1, ?, ?, ?, 7, 7, ?, 'runtime-a', ?, 0)
        """,
        (
            repository_contract.LEGACY_REPOSITORY_AUTHORITY,
            repository_contract.LEGACY_REPOSITORY_AUTHORITY_STATE,
            "e" * 64,
            cutover_id,
            b"d" * 32,
        ),
    )
    _receipt(
        connection,
        transaction_id,
        repository_contract.LEGACY_REPOSITORY_TRANSACTION_KIND,
    )
    connection.execute(
        """
        INSERT INTO legacy_repository_seals(
            repository_singleton, cutover_id, transaction_id, receipt_kind,
            receipt_protocol, catalog_id, catalog_seal_sha256,
            proof_protocol, manifest_schema, status, selection,
            repository_epoch, repository_revision, catalog_revision,
            runtime_generation, code_digest, project_count,
            live_project_count, deleted_project_count, source_state_bytes,
            manifest_bytes, manifest_sha256, source_repository_sha256,
            catalog_coverage_sha256, proof_sha256, target_schema_version,
            schema_fingerprint, cutover_ready
        ) VALUES (
            1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 7, 7, 'runtime-a', ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?, 5, ?, 0
        )
        """,
        (
            cutover_id,
            transaction_id,
            repository_contract.LEGACY_REPOSITORY_TRANSACTION_KIND,
            repository_contract.LEGACY_REPOSITORY_RECEIPT_PROTOCOL,
            catalog_id,
            catalog_seal_sha256,
            repository_contract.LEGACY_REPOSITORY_PROOF_PROTOCOL,
            repository_contract.LEGACY_REPOSITORY_MANIFEST_SCHEMA,
            repository_contract.LEGACY_REPOSITORY_STATUS,
            repository_contract.LEGACY_REPOSITORY_SELECTION,
            "e" * 64,
            b"d" * 32,
            live_count + deleted_count,
            live_count,
            deleted_count,
            2 * live_count,
            b"{}",
            b"m" * 32,
            b"s" * 32,
            b"v" * 32,
            b"p" * 32,
            "a" * 64,
        ),
    )


def test_v5_repository_tables_are_strict_private_and_non_authoritative() -> None:
    connection = _connection()
    try:
        _apply_migrations(connection, schema.MIGRATIONS)
        assert schema.REPOSITORY_PROOF_TABLES == (
            "legacy_repository_proof_control",
            "legacy_repository_seals",
            "legacy_repository_members",
        )
        assert set(schema.REPOSITORY_PROOF_TABLES).issubset(schema.PRIVATE_TABLES)
        strict = {
            row["name"]: row["strict"]
            for row in connection.execute("PRAGMA table_list").fetchall()
        }
        assert all(strict[table] == 1 for table in schema.REPOSITORY_PROOF_TABLES)
        connection.execute("BEGIN")
        catalog = _insert_catalog_seal(connection)
        _receipt(connection, catalog["transaction_id"], catalog["receipt_kind"])
        _insert_repository_proof(connection)
        connection.execute("COMMIT")
        row = connection.execute(
            "SELECT authority, authority_state, cutover_ready "
            "FROM legacy_repository_proof_control"
        ).fetchone()
        assert tuple(row) == ("legacy", "cutover_prepared", 0)
        seal = connection.execute(
            "SELECT target_schema_version, cutover_ready "
            "FROM legacy_repository_seals"
        ).fetchone()
        assert tuple(seal) == (5, 0)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        for statement in (
            "UPDATE legacy_repository_proof_control SET cutover_ready=0",
            "DELETE FROM legacy_repository_proof_control",
            "UPDATE legacy_repository_seals SET cutover_ready=0",
            "DELETE FROM legacy_repository_seals",
        ):
            with pytest.raises(
                sqlite3.IntegrityError, match="legacy_repository_append_only"
            ):
                connection.execute(statement)
    finally:
        connection.close()


def test_v5_deleted_member_has_no_live_fk_and_preserves_generation_high_water() -> None:
    connection = _connection()
    try:
        _apply_migrations(connection, schema.MIGRATIONS)
        connection.execute("BEGIN")
        catalog = _insert_catalog_seal(connection)
        _receipt(connection, catalog["transaction_id"], catalog["receipt_kind"])
        _insert_repository_proof(connection, deleted_count=1)
        connection.execute(
            """
            INSERT INTO legacy_repository_members(
                cutover_id, ordinal, project_id, generation, status,
                operation_id, source_state_sha256, source_state_bytes
            ) VALUES (?, 0, 'deleted-project', 41, 'deleted', ?, ?, 0)
            """,
            (
                "f" * 64,
                "1" * 32,
                __import__("hashlib").sha256(b"").digest(),
            ),
        )
        connection.execute("COMMIT")
        row = connection.execute(
            "SELECT generation, status, catalog_id FROM legacy_repository_members"
        ).fetchone()
        assert tuple(row) == (41, "deleted", None)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE legacy_repository_members SET generation=generation"
            )
    finally:
        connection.close()


def test_v5_live_member_is_exact_fk_to_v4_catalog_descriptor() -> None:
    connection = _connection()
    try:
        _apply_migrations(connection, schema.MIGRATIONS)
        descriptor = _insert_import_batch_and_descriptor(connection)
        connection.execute("BEGIN")
        catalog = _insert_catalog_seal(
            connection,
            import_id=descriptor["import_id"],
            project_count=1,
            source_head_bytes=descriptor["source_head_bytes"],
            row_snapshot_bytes=descriptor["row_snapshot_bytes"],
        )
        _receipt(connection, catalog["transaction_id"], catalog["receipt_kind"])
        _insert_catalog_member(connection, descriptor)
        _insert_repository_proof(
            connection,
            catalog_seal_sha256=catalog["seal_sha256"],
            live_count=1,
        )
        connection.execute(
            """
            INSERT INTO legacy_repository_members(
                cutover_id, ordinal, project_id, generation, status,
                operation_id, source_state_sha256, source_state_bytes,
                catalog_id, catalog_ordinal, import_id, document_id,
                document_version, storage_version, business_root_sha256,
                entity_count, row_snapshot_bytes
            ) VALUES (?, 0, ?, ?, 'live', ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "f" * 64,
                descriptor["project_id"],
                descriptor["generation"],
                "2" * 32,
                descriptor["source_head_sha256"],
                descriptor["source_head_bytes"],
                catalog["catalog_id"],
                descriptor["import_id"],
                descriptor["document_id"],
                descriptor["document_version"],
                descriptor["storage_version"],
                descriptor["business_root_sha256"],
                descriptor["entity_count"],
                descriptor["row_snapshot_bytes"],
            ),
        )
        connection.execute("COMMIT")
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

        connection.execute("BEGIN")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE legacy_repository_members SET source_state_bytes=3"
            )
        connection.execute("ROLLBACK")
    finally:
        connection.close()


def test_v5_schema_rejects_legacy_adopt_above_generation_one() -> None:
    connection = _connection()
    try:
        _apply_migrations(connection, schema.MIGRATIONS)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO legacy_repository_members(
                    cutover_id, ordinal, project_id, generation, status,
                    operation_id, source_state_sha256, source_state_bytes,
                    catalog_id, catalog_ordinal, import_id, document_id,
                    document_version, storage_version, business_root_sha256,
                    entity_count, row_snapshot_bytes
                ) VALUES (?, 0, 'project-a', 2, 'live', 'legacy-adopt', ?, 1,
                          ?, 0, ?, 'document-a', 1, 'v2', ?, 0, 1)
                """,
                ("f" * 64, b"h" * 32, "c" * 64, "a" * 64, b"r" * 32),
            )
    finally:
        connection.close()


@pytest.mark.parametrize("state", ("empty", "terminal", "catalog-empty", "catalog-live"))
def test_v4_to_v5_upgrade_preserves_all_v4_states_and_starts_inert(state: str) -> None:
    connection = _connection()
    try:
        _apply_migrations(connection, schema.MIGRATIONS[:4])
        if state in ("terminal", "catalog-live"):
            descriptor = _insert_import_batch_and_descriptor(connection)
        if state in ("catalog-empty", "catalog-live"):
            connection.execute("BEGIN")
            catalog = _insert_catalog_seal(
                connection,
                import_id=None if state == "catalog-empty" else descriptor["import_id"],
                project_count=0 if state == "catalog-empty" else 1,
                source_head_bytes=0 if state == "catalog-empty" else descriptor["source_head_bytes"],
                row_snapshot_bytes=0 if state == "catalog-empty" else descriptor["row_snapshot_bytes"],
            )
            _receipt(connection, catalog["transaction_id"], catalog["receipt_kind"])
            if state == "catalog-live":
                _insert_catalog_member(connection, descriptor)
            connection.execute("COMMIT")
        before = {
            table: connection.execute("SELECT count(*) FROM %s" % table).fetchone()[0]
            for table in (*schema.IMPORT_LEDGER_TABLES, *schema.CATALOG_LEDGER_TABLES)
        }
        _apply_migrations(connection, schema.MIGRATIONS[4:5])
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
        assert before == {
            table: connection.execute("SELECT count(*) FROM %s" % table).fetchone()[0]
            for table in (*schema.IMPORT_LEDGER_TABLES, *schema.CATALOG_LEDGER_TABLES)
        }
        assert all(
            connection.execute("SELECT count(*) FROM %s" % table).fetchone()[0] == 0
            for table in schema.REPOSITORY_PROOF_TABLES
        )
        assert connection.execute(
            "SELECT authority FROM kernel_repository"
        ).fetchone()[0] == "legacy"
    finally:
        connection.close()


def test_v5_ddl_failure_rolls_back_indexes_ledger_and_version() -> None:
    connection = _connection()
    try:
        _apply_migrations(connection, schema.MIGRATIONS[:4])
        connection.execute("CREATE TABLE legacy_repository_proof_control(drift TEXT)")
        with pytest.raises(sqlite3.OperationalError):
            _apply_migrations(connection, schema.MIGRATIONS[4:])
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert connection.execute(
            "SELECT count(*) FROM schema_migrations WHERE version=5"
        ).fetchone()[0] == 0
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE name IN "
                "('legacy_catalog_seals_repository_identity', "
                "'legacy_catalog_members_repository_identity')"
            ).fetchall()
        }
        assert names == set()
    finally:
        connection.close()


def test_v7_registry_remains_frozen_but_is_no_longer_the_current_schema() -> None:
    frozen = (
        "390305f196309ed13d84a09428b5dbecb28e300d4222c2f980e3b11902a33a26",
        "e90a68e8c36513213618817e39d398a7944fbb39fc365e4abd946546164fb0fa",
        "b5fc2a8d86f45db33d6771c9bf29e8f6f14fbced22e598340c2ff19cf1d1d590",
        "71cdb236889b5e5194c7e3f9d48c4cb7400941c324dd8c1f5bfd46e1c521adc9",
        "b7d543c2594a1fdc78fe18949432e2baf6df4c447caa57317c6361ba63313eff",
        "41fe4ba13a35bacfa75af648381c0dadc4d256a8872c65a1bbf0138b64252254",
    )
    assert tuple(item.checksum for item in schema.MIGRATIONS[:6]) == frozen
    assert (
        schema.BUSINESS_TRANSACTION_MIGRATION.version,
        schema.BUSINESS_TRANSACTION_MIGRATION.name,
    ) == (6, "business_transaction_substrate_v1")
    assert (
        schema.LOCAL_REVISION_MIGRATION.version,
        schema.LOCAL_REVISION_MIGRATION.name,
    ) == (7, "business_local_revision_separation_v1")
    assert schema.MIGRATIONS[6] is schema.LOCAL_REVISION_MIGRATION
    assert schema.MIGRATIONS[7] is schema.PRODUCTION_AUTHORITY_WORKER_MIGRATION
    assert schema.MIGRATIONS[8] is schema.WORKER_PAYLOAD_PROJECTION_MIGRATION
    assert schema.MIGRATIONS[9] is schema.RUNTIME_SCHEMA_HARDENING_MIGRATION
    assert schema.MIGRATIONS[10] is schema.MOUNT_CHRONOLOGY_MIGRATION
    assert schema.MIGRATIONS[11] is schema.INSTALL_CLOSURE_MIGRATION
    assert schema.MIGRATIONS[12] is schema.DIRECT_PREDECESSOR_BINDING_MIGRATION
    assert schema.MIGRATIONS[-1] is schema.DIRECT_PREDECESSOR_BINDING_MIGRATION
    assert len(schema.BUSINESS_TRANSACTION_TABLES) == 17
    assert len(schema.CUTOVER_INSTALL_CLOSURE_TABLES) == 4
    assert len(schema.DIRECT_PREDECESSOR_BINDING_TABLES) == 4
    assert len(schema.PRODUCTION_RUNTIME_TABLES) == 36
    assert set(schema.BUSINESS_TRANSACTION_TABLES).issubset(schema.PRIVATE_TABLES)

    connection = _connection()
    try:
        _apply_migrations(connection, schema.MIGRATIONS[:7])
        strict = {
            row["name"]: row["strict"]
            for row in connection.execute("PRAGMA table_list").fetchall()
        }
        assert all(
            strict[table] == 1 for table in schema.BUSINESS_TRANSACTION_TABLES
        )
        assert all(
            connection.execute("SELECT count(*) FROM %s" % table).fetchone()[0]
            == 0
            for table in schema.BUSINESS_TRANSACTION_TABLES
        )
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='index'"
            ).fetchall()
        }
        assert {
            "projects_transaction_identity",
            "documents_transaction_identity",
            "kernel_repository_transaction_authority",
            "business_commits_document_history",
            "event_outbox_dispatch",
            "document_asset_refs_asset",
        }.issubset(indexes)
        transition_columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(pipeline_job_transitions)"
            ).fetchall()
        }
        assert {
            "idempotency_key",
            "max_attempts",
            "commit_pipeline_intent_count",
        }.issubset(
            transition_columns
        )
        commit_columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(business_commits)"
            ).fetchall()
        }
        assert {
            "project_revision",
            "head_revision",
            "durable_action_count",
            "pipeline_intent_count",
            "asset_reference_count",
            "asset_projection_sha256",
        }.issubset(commit_columns)
        assert "commit_action_count" in {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(deferred_actions)"
            ).fetchall()
        }
        ref_table_info = connection.execute(
            "PRAGMA table_info(document_asset_refs)"
        ).fetchall()
        assert "commit_asset_reference_count" in {
            row["name"] for row in ref_table_info
        }
        assert tuple(
            row["name"]
            for row in sorted(
                (row for row in ref_table_info if row["pk"] > 0),
                key=lambda row: row["pk"],
            )
        ) == ("commit_id", "ordinal")
        transition_unique_shapes = {
            tuple(
                column["name"]
                for column in connection.execute(
                    "PRAGMA index_info(%s)" % index_row["name"]
                ).fetchall()
            )
            for index_row in connection.execute(
                "PRAGMA index_list(pipeline_job_transitions)"
            ).fetchall()
            if index_row["unique"] == 1
        }
        assert (
            "project_id",
            "generation",
            "idempotency_key",
        ) in transition_unique_shapes
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_v8_registry_shape_fingerprint_and_fresh_empty_state_are_frozen() -> None:
    migration = schema.PRODUCTION_AUTHORITY_WORKER_MIGRATION
    assert (migration.version, migration.name) == (
        8,
        "production_authority_worker_runtime_v1",
    )
    assert migration.checksum == (
        "f6c8a8e1f2aa633a52da52a04e6b58ad11230f5093e4b883e7b63674bb8ad80b"
    )
    assert len(schema.PRODUCTION_RUNTIME_V8_TABLES) == 25
    assert len(set(schema.PRODUCTION_RUNTIME_V8_TABLES)) == 25
    assert set(schema.PRODUCTION_RUNTIME_V8_TABLES).issubset(
        schema.PRIVATE_TABLES
    )

    connection = _connection()
    try:
        _apply_migrations(connection, schema.MIGRATIONS[:8])
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 8
        assert kernel._schema_fingerprint(connection, 8) == (
            "0d96ffa4e88ac3c3d3fd1ba1b6f09f11a3a2d7a0e1861f5781bf0856d7fce771"
        )
        strict = {
            row["name"]: row["strict"]
            for row in connection.execute("PRAGMA table_list").fetchall()
        }
        assert all(
            strict[table] == 1
            for table in schema.PRODUCTION_RUNTIME_V8_TABLES
        )
        assert all(
            connection.execute(
                'SELECT count(*) FROM "' + table + '"'
            ).fetchone()[0] == 0
            for table in schema.PRODUCTION_RUNTIME_V8_TABLES
        )
        assert connection.execute(
            "SELECT count(*) FROM sqlite_schema "
            "WHERE name='schema_v8_production_runtime_preflight'"
        ).fetchone()[0] == 0
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

        expected_columns = {
            "atomic_business_write_receipts": {
                "commit_id", "transaction_id", "control_count",
                "before_worker_sequence", "before_worker_tail_sha256",
                "after_worker_sequence", "after_worker_tail_sha256",
                "manifest_sha256", "receipt_sha256",
            },
            "worker_ledger_entries": {
                "sequence", "entry_kind", "transaction_id",
                "previous_tail_sha256", "ledger_tail_sha256",
                "request_sha256", "after_state_sha256", "receipt_sha256",
            },
            "worker_target_current": {
                "target_id", "status", "state_revision", "attempt_count",
                "fence_high_water", "cancellation_pending",
                "current_effect_id", "unresolved_effect_id",
                "previous_state_sha256", "state_sha256",
            },
            "outbox_dispatch_head": {
                "published_sequence", "published_repository_revision",
                "published_ordinal", "inflight_sequence",
                "inflight_repository_revision", "inflight_ordinal",
            },
            "business_cutover_mount_receipts": {
                "install_receipt_kind", "install_committed_at",
                "coverage_receipt_kind", "coverage_committed_at",
            },
            "business_cutover_head": {
                "phase_revision", "phase", "authority_state",
                "install_transaction_id", "coverage_transaction_id",
                "audit_transaction_id", "activation_transaction_id",
                "mount_transaction_id", "previous_head_sha256", "head_sha256",
            },
        }
        for table, wanted in expected_columns.items():
            observed = {
                row["name"]
                for row in connection.execute(
                    'PRAGMA table_info("' + table + '")'
                ).fetchall()
            }
            assert wanted.issubset(observed)

        trigger_names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='trigger'"
            ).fetchall()
        }
        assert {
            "worker_ledger_entries_chain_guard",
            "worker_ledger_head_cas_update_guard",
            "worker_ledger_head_sequence_guard",
            "outbox_dispatch_head_progress_guard",
            "business_cutover_head_phase_guard",
            "business_cutover_mount_binding_guard",
            "repository_runtime_control_v8_atomic_finalization_guard",
            "atomic_business_write_receipts_no_update",
            "atomic_business_write_receipts_no_delete",
        }.issubset(trigger_names)
    finally:
        connection.close()


def test_v8_mount_binding_guard_rejects_a_forged_install_receipt_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sqlite_business_test_support as business_support
    import sqlite_business_audit as business_audit
    import sqlite_business_runtime as business_runtime

    # This is frozen schema-v11/v12 chronology evidence.  Schema-v13 rightly
    # requires four new direct-predecessor bindings that the historical test
    # constructor cannot and must not fabricate.
    frozen_v12 = schema.MIGRATIONS[:12]
    monkeypatch.setattr(kernel, "MIGRATIONS", frozen_v12)
    monkeypatch.setattr(
        kernel,
        "PRODUCTION_RUNTIME_TABLES",
        schema.PRODUCTION_RUNTIME_TABLES[:32],
    )
    monkeypatch.setattr(business_support, "MIGRATIONS", frozen_v12)
    monkeypatch.setattr(business_audit, "MIGRATIONS", frozen_v12)
    monkeypatch.setattr(business_runtime, "MIGRATIONS", frozen_v12)
    monkeypatch.setattr(
        business_audit,
        "_expected_migration_ledger_sha256",
        lambda: schema._expected_migration_ledger_sha256(12),
    )
    baseline = business_support.create_active_business_database(
        tmp_path / "mount-binding.sqlite3"
    )
    connection = sqlite3.connect(baseline.database.path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    insert_sql = """
        INSERT INTO business_cutover_mount_receipts(
            cutover_id,transaction_id,receipt_kind,committed_at,
            install_transaction_id,install_receipt_kind,install_committed_at,
            install_manifest_sha256,coverage_transaction_id,
            coverage_receipt_kind,coverage_committed_at,
            coverage_manifest_sha256,audit_transaction_id,
            audit_manifest_sha256,activation_transaction_id,
            activation_manifest_sha256,repository_epoch,runtime_generation,
            code_digest,manifest_bytes,manifest_sha256
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """
    try:
        mount = connection.execute(
            "SELECT * FROM business_cutover_mount_receipts"
        ).fetchone()
        assert mount is not None
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            "DROP TRIGGER business_cutover_mount_receipts_no_delete"
        )
        connection.execute("DELETE FROM business_cutover_mount_receipts")
        connection.execute("PRAGMA foreign_keys=ON")

        values = (
            mount["cutover_id"],
            mount["transaction_id"],
            mount["receipt_kind"],
            mount["committed_at"],
            mount["install_transaction_id"],
            mount["install_receipt_kind"],
            "2026-07-11T23:59:59.998Z",
            mount["install_manifest_sha256"],
            mount["coverage_transaction_id"],
            mount["coverage_receipt_kind"],
            mount["coverage_committed_at"],
            mount["coverage_manifest_sha256"],
            mount["audit_transaction_id"],
            mount["audit_manifest_sha256"],
            mount["activation_transaction_id"],
            mount["activation_manifest_sha256"],
            mount["repository_epoch"],
            mount["runtime_generation"],
            mount["code_digest"],
            mount["manifest_bytes"],
            mount["manifest_sha256"],
        )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="production_cutover_binding_invalid",
        ):
            connection.execute(insert_sql, values)

        restored = list(values)
        restored[6] = mount["install_committed_at"]
        connection.execute(insert_sql, restored)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_v8_worker_and_pipeline_payloads_require_the_official_codec() -> None:
    connection = _connection()
    payload = b'{"value":1}'
    payload_sha256 = hashlib.sha256(payload).digest()
    try:
        _apply_migrations(connection, schema.MIGRATIONS)
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("DROP TRIGGER worker_targets_action_binding_guard")
        connection.execute("DROP TRIGGER worker_targets_finalized_guard")
        connection.execute("DROP TRIGGER pipeline_control_intents_origin_guard")
        connection.execute(
            "DROP TRIGGER pipeline_control_intents_legacy_binding_guard"
        )

        worker_sql = """
            INSERT INTO worker_targets(
                target_id,target_kind,genesis_status,genesis_state_sha256,
                project_id,generation,document_id,origin_commit_id,
                source_ordinal,payload_schema,payload_codec_id,
                payload_codec_version,payload_bytes,payload_sha256,
                idempotency_scope,idempotency_key,replay_policy,
                idempotency_policy,max_attempts,action_type,created_at
            ) VALUES (?,'action','queued',?,'project-a',1,'document-a',?,0,
                      'action/v1',?,1,?,?,'action/project-a',?,
                      'provider_idempotent','required_external_key',3,
                      'notify',?)
        """
        connection.execute(
            worker_sql,
            (
                "1" * 32,
                hashlib.sha256(b"worker-genesis").digest(),
                "2" * 32,
                "ze.deterministic-json",
                payload,
                payload_sha256,
                "official-worker",
                _NOW,
            ),
        )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            connection.execute(
                worker_sql,
                (
                    "3" * 32,
                    hashlib.sha256(b"worker-json").digest(),
                    "4" * 32,
                    "json",
                    payload,
                    payload_sha256,
                    "legacy-json-worker",
                    _NOW,
                ),
            )

        intent_sql = """
            INSERT INTO pipeline_control_intents(
                intent_id,origin_commit_id,ordinal,target_id,target_kind,
                operation,intent_type,idempotency_key,payload_schema,
                payload_codec_id,payload_codec_version,payload_bytes,
                payload_sha256,replay_policy,idempotency_policy,max_attempts,
                before_status,after_status,before_state_revision,
                after_state_revision,request_sha256,committed_at
            ) VALUES (?,?,0,?,'pipeline','apply_control_intent','render.pause',?,
                      'pipeline-control/v1',?,1,?,?,'exact_cas_retry',
                      'durable_receipt_reconcile',3,'running','paused',1,2,?,?)
        """
        connection.execute(
            intent_sql,
            (
                "5" * 32,
                "6" * 32,
                "7" * 32,
                "official-intent",
                "ze.deterministic-json",
                payload,
                payload_sha256,
                hashlib.sha256(b"official-intent-request").digest(),
                _NOW,
            ),
        )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            connection.execute(
                intent_sql,
                (
                    "8" * 32,
                    "9" * 32,
                    "a" * 32,
                    "legacy-json-intent",
                    "json",
                    payload,
                    payload_sha256,
                    hashlib.sha256(b"legacy-intent-request").digest(),
                    _NOW,
                ),
            )
    finally:
        connection.close()


def test_v8_pipeline_derived_scope_has_a_bounded_full_component_budget() -> None:
    connection = _connection()
    payload = b'{"value":1}'
    payload_sha256 = hashlib.sha256(payload).digest()
    project_id = "\U0001f600" * 160
    pipeline_id = "p" * 1024
    generation = 2**63 - 1
    scope = "pipeline/%s/%d/%s" % (project_id, generation, pipeline_id)
    assert len(scope.encode("utf-8")) == schema.MAX_PIPELINE_WORKER_SCOPE_BYTES
    try:
        _apply_migrations(connection, schema.MIGRATIONS)
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("DROP TRIGGER worker_targets_pipeline_binding_guard")
        connection.execute("DROP TRIGGER worker_targets_action_binding_guard")
        connection.execute(
            """
            INSERT INTO worker_targets(
                target_id,target_kind,genesis_status,genesis_state_sha256,
                project_id,generation,document_id,origin_commit_id,
                source_ordinal,payload_schema,payload_codec_id,
                payload_codec_version,payload_bytes,payload_sha256,
                idempotency_scope,idempotency_key,replay_policy,
                idempotency_policy,max_attempts,pipeline_id,intent_type,created_at
            ) VALUES (?,'pipeline','queued',?,?,?,?,?,0,'pipeline/v1',
                      'ze.deterministic-json',1,?,?,?,'pipeline-key',
                      'exact_cas_retry','durable_receipt_reconcile',3,?,?,?)
            """,
            (
                "7" * 32,
                hashlib.sha256(b"pipeline-genesis").digest(),
                project_id,
                generation,
                "document-a",
                "8" * 32,
                payload,
                payload_sha256,
                scope,
                pipeline_id,
                "generate",
                _NOW,
            ),
        )
        assert connection.execute(
            "SELECT length(CAST(idempotency_scope AS BLOB)) "
            "FROM worker_targets WHERE target_id=?",
            ("7" * 32,),
        ).fetchone()[0] == schema.MAX_PIPELINE_WORKER_SCOPE_BYTES

        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            connection.execute(
                """
                INSERT INTO worker_targets(
                    target_id,target_kind,genesis_status,genesis_state_sha256,
                    project_id,generation,document_id,origin_commit_id,
                    source_ordinal,payload_schema,payload_codec_id,
                    payload_codec_version,payload_bytes,payload_sha256,
                    idempotency_scope,idempotency_key,replay_policy,
                    idempotency_policy,max_attempts,action_type,created_at
                ) VALUES (?,'action','queued',?,'project-a',1,'document-a',?,0,
                          'action/v1','ze.deterministic-json',1,?,?,?,'action-key',
                          'provider_idempotent','required_external_key',3,
                          'notify',?)
                """,
                (
                    "9" * 32,
                    hashlib.sha256(b"action-genesis").digest(),
                    "a" * 32,
                    payload,
                    payload_sha256,
                    "s" * (schema.MAX_BUSINESS_ID_BYTES + 1),
                    _NOW,
                ),
            )
    finally:
        connection.close()


def test_pristine_v7_upgrades_to_v8_without_rewriting_historical_ledger() -> None:
    connection = _connection()
    try:
        _apply_migrations(connection, schema.MIGRATIONS[:7])
        before = [
            tuple(row)
            for row in connection.execute(
                "SELECT version, name, checksum, applied_at FROM schema_migrations "
                "ORDER BY version"
            ).fetchall()
        ]
        _apply_migrations(connection, schema.MIGRATIONS[7:8])
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 8
        assert [
            tuple(row)
            for row in connection.execute(
                "SELECT version, name, checksum, applied_at FROM schema_migrations "
                "WHERE version <= 7 ORDER BY version"
            ).fetchall()
        ] == before
        assert tuple(connection.execute(
            "SELECT name, checksum FROM schema_migrations WHERE version=8"
        ).fetchone()) == (
            "production_authority_worker_runtime_v1",
            schema.PRODUCTION_AUTHORITY_WORKER_MIGRATION.checksum,
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def _assert_v8_preflight_rolled_back(connection: sqlite3.Connection) -> None:
    with pytest.raises(
        sqlite3.IntegrityError,
        match="schema_v8_production_runtime_precondition",
    ):
        _apply_migrations(
            connection,
            (schema.PRODUCTION_AUTHORITY_WORKER_MIGRATION,),
        )
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 7
    assert connection.execute(
        "SELECT count(*) FROM schema_migrations WHERE version=8"
    ).fetchone()[0] == 0
    assert connection.execute(
        "SELECT count(*) FROM sqlite_schema WHERE name IN "
        "('schema_v8_production_runtime_preflight', "
        "'transaction_receipts_identity_kind_time')"
    ).fetchone()[0] == 0
    assert not {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='table'"
        ).fetchall()
    }.intersection(schema.PRODUCTION_RUNTIME_TABLES)


@pytest.mark.parametrize("dirty_table", schema.BUSINESS_TRANSACTION_TABLES)
def test_each_dirty_v7_transaction_table_blocks_v8_and_rolls_back(
    dirty_table: str,
) -> None:
    connection = _connection()
    try:
        _apply_migrations(connection, schema.MIGRATIONS[:7])
        _dirty_one_strict_row(connection, dirty_table)
        assert connection.execute(
            'SELECT count(*) FROM "' + dirty_table + '"'
        ).fetchone()[0] == 1
        _assert_v8_preflight_rolled_back(connection)
        assert connection.execute(
            'SELECT count(*) FROM "' + dirty_table + '"'
        ).fetchone()[0] == 1
    finally:
        connection.close()


_V8_RESERVED_RECEIPT_PREFIXES = (
    "_business-cutover-install:",
    "_business-cutover-asset-stage:",
    "_business-cutover-asset-coverage:",
    "_repository-audit:",
    "_business-cutover-activation:",
    "_business-cutover-mount:",
    "_worker-execution:",
    "_pipeline-business-control:",
    "ze-business:",
)
_V8_RESERVED_RECEIPT_KINDS = (
    "_business_cutover_install_v1",
    "_business_cutover_asset_stage_v1",
    "_business_cutover_asset_coverage_v1",
    "_repository_audit_certificate_v1",
    "_business_cutover_activation_v2",
    "_business_cutover_mount_v2",
    "_worker_execution_v3",
    "_pipeline_business_control_v2",
    "ze_business_commit_v1",
)


@pytest.mark.parametrize("reserved_prefix", _V8_RESERVED_RECEIPT_PREFIXES)
def test_reserved_v8_receipt_prefix_blocks_upgrade_before_any_ddl(
    reserved_prefix: str,
) -> None:
    connection = _connection()
    try:
        _apply_migrations(connection, schema.MIGRATIONS[:7])
        _receipt(connection, reserved_prefix + "occupied", "unrelated-kind")
        _assert_v8_preflight_rolled_back(connection)
    finally:
        connection.close()


@pytest.mark.parametrize("reserved_kind", _V8_RESERVED_RECEIPT_KINDS)
def test_reserved_v8_receipt_kind_blocks_upgrade_before_any_ddl(
    reserved_kind: str,
) -> None:
    connection = _connection()
    try:
        _apply_migrations(connection, schema.MIGRATIONS[:7])
        _receipt(connection, "unrelated-transaction", reserved_kind)
        _assert_v8_preflight_rolled_back(connection)
    finally:
        connection.close()


def test_nonlegacy_v7_authority_blocks_v8_and_rolls_back() -> None:
    connection = _connection()
    try:
        _apply_migrations(connection, schema.MIGRATIONS[:7])
        connection.execute(
            "UPDATE kernel_repository SET authority='sqlite' WHERE singleton=1"
        )
        _assert_v8_preflight_rolled_back(connection)
        assert connection.execute(
            "SELECT authority FROM kernel_repository WHERE singleton=1"
        ).fetchone()[0] == "sqlite"
    finally:
        connection.close()


def test_v8_worker_shared_ledger_chain_head_cas_and_append_only_guards() -> None:
    connection = _connection()
    zero = b"\0" * 32
    transaction_id = "_worker-execution:" + "a" * 32
    tail = b"t" * 32
    try:
        _apply_migrations(connection, schema.MIGRATIONS)
        connection.execute(
            "INSERT INTO worker_ledger_head("
            "singleton,state_revision,sequence,previous_tail_sha256,"
            "tail_sha256,updated_at) VALUES (1,1,0,?,?,?)",
            (zero, zero, _NOW),
        )
        _receipt(connection, transaction_id, "_worker_execution_v3")

        with pytest.raises(
            sqlite3.IntegrityError,
            match="production_worker_ledger_chain_invalid",
        ):
            connection.execute(
                "INSERT INTO worker_ledger_entries("
                "sequence,entry_kind,transaction_id,receipt_kind,committed_at,"
                "previous_tail_sha256,ledger_tail_sha256,request_sha256,"
                "after_state_sha256,receipt_sha256,occurred_at) "
                "VALUES (2,'worker_execution',?,'_worker_execution_v3',?,?,?,?,?,?,?)",
                (
                    transaction_id, _NOW, b"p" * 32, tail,
                    b"r" * 32, b"a" * 32, b"q" * 32, _NOW,
                ),
            )

        connection.execute(
            "INSERT INTO worker_ledger_entries("
            "sequence,entry_kind,transaction_id,receipt_kind,committed_at,"
            "previous_tail_sha256,ledger_tail_sha256,request_sha256,"
            "after_state_sha256,receipt_sha256,occurred_at) "
            "VALUES (1,'worker_execution',?,'_worker_execution_v3',?,?,?,?,?,?,?)",
            (
                transaction_id, _NOW, zero, tail,
                b"r" * 32, b"a" * 32, b"q" * 32, _NOW,
            ),
        )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="production_worker_current_closure_invalid",
        ):
            connection.execute(
                "UPDATE worker_ledger_head SET state_revision=2, sequence=1, "
                "previous_tail_sha256=?, tail_sha256=?, updated_at=? "
                "WHERE singleton=1",
                (zero, tail, _NOW),
            )
        # The rest of this test isolates the generic CAS/sequence guards; the
        # production closure guard above intentionally rejects an orphan entry.
        connection.execute(
            "DROP TRIGGER worker_ledger_head_current_closure_guard"
        )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="production_head_cas_invalid",
        ):
            connection.execute(
                "UPDATE worker_ledger_head SET state_revision=2, sequence=1, "
                "previous_tail_sha256=?, tail_sha256=?, updated_at=? "
                "WHERE singleton=1",
                (b"w" * 32, tail, _NOW),
            )
        connection.execute(
            "UPDATE worker_ledger_head SET state_revision=2, sequence=1, "
            "previous_tail_sha256=?, tail_sha256=?, updated_at=? "
            "WHERE singleton=1",
            (zero, tail, _NOW),
        )
        assert tuple(connection.execute(
            "SELECT state_revision,sequence,previous_tail_sha256,tail_sha256 "
            "FROM worker_ledger_head"
        ).fetchone()) == (2, 1, zero, tail)
        with pytest.raises(
            sqlite3.IntegrityError,
            match="production_history_append_only",
        ):
            connection.execute(
                "UPDATE worker_ledger_entries SET occurred_at=occurred_at"
            )
    finally:
        connection.close()


def test_v8_outbox_dispatch_keeps_published_and_inflight_separate() -> None:
    connection = _connection()
    zero = b"\0" * 32
    try:
        _apply_migrations(connection, schema.MIGRATIONS)
        connection.execute(
            "INSERT INTO outbox_dispatch_head("
            "singleton,state_revision,published_sequence,"
            "published_repository_revision,published_ordinal,"
            "inflight_sequence,inflight_repository_revision,inflight_ordinal,"
            "previous_head_sha256,head_sha256,updated_at) "
            "VALUES (1,1,0,NULL,NULL,NULL,NULL,NULL,?,?,?)",
            (zero, zero, _NOW),
        )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="production_outbox_dispatch_invalid",
        ):
            connection.execute(
                "UPDATE outbox_dispatch_head SET state_revision=2, "
                "inflight_sequence=1,inflight_repository_revision=1,"
                "inflight_ordinal=0,previous_head_sha256=?,head_sha256=?,"
                "updated_at=? WHERE singleton=1",
                (zero, b"h" * 32, _NOW),
            )
        assert tuple(connection.execute(
            "SELECT published_sequence,inflight_sequence FROM outbox_dispatch_head"
        ).fetchone()) == (0, None)
        with pytest.raises(
            sqlite3.IntegrityError,
            match="production_head_no_delete",
        ):
            connection.execute("DELETE FROM outbox_dispatch_head")
    finally:
        connection.close()


def _insert_v8_test_worker_receipt(
    connection: sqlite3.Connection,
    *,
    request_id: str,
    sequence: int,
    operation: str,
    target_id: str,
    before_status: str,
    after_status: str,
    before_revision: int,
    after_state_sha256: bytes,
    previous_tail_sha256: bytes,
    ledger_tail_sha256: bytes,
    receipt_sha256: bytes,
    manifest_bytes: Optional[bytes] = None,
) -> str:
    transaction_id = "_worker-execution:" + request_id
    connection.execute(
        """
        INSERT INTO worker_execution_receipts(
            request_id,transaction_id,entry_kind,receipt_kind,committed_at,
            operation,target_kind,target_id,before_status,after_status,
            before_state_revision,after_state_revision,sequence,
            previous_ledger_tail_sha256,ledger_tail_sha256,manifest_bytes,
            request_sha256,after_state_sha256,receipt_sha256
        ) VALUES (?,?,'worker_execution','_worker_execution_v3',?,?,?,?,?,?,?,
                  ?,?,?,?,?,?,?,?)
        """,
        (
            request_id,
            transaction_id,
            _NOW,
            operation,
            "outbox",
            target_id,
            before_status,
            after_status,
            before_revision,
            before_revision + 1,
            sequence,
            previous_tail_sha256,
            ledger_tail_sha256,
            (
                ("receipt-" + operation).encode("ascii")
                if manifest_bytes is None
                else manifest_bytes
            ),
            hashlib.sha256(("request-" + operation).encode("ascii")).digest(),
            after_state_sha256,
            receipt_sha256,
        ),
    )
    return transaction_id


def _seed_v8_test_outbox_dispatch_target(
    connection: sqlite3.Connection,
    *,
    max_attempts: int = 3,
    migrations: Optional[Iterable[schema.Migration]] = None,
) -> dict[str, Any]:
    """Seed one target while retaining the dispatch guard under test.

    Foreign keys and the genesis receipt trigger are disabled only because this
    focused schema test does not recreate the independently tested business
    commit/cutover ledger.  Every row read by the dispatch closure itself stays
    present and exact; all attempt/effect/result binding triggers remain live.
    """

    _apply_migrations(
        connection,
        schema.MIGRATIONS if migrations is None else migrations,
    )
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute("DROP TRIGGER worker_target_current_insert_binding_guard")
    zero = b"\0" * 32
    event_id = "a" * 32
    commit_id = "b" * 32
    operation_id = "c" * 32
    payload = b'{"event":1}'
    payload_sha256 = hashlib.sha256(payload).digest()
    genesis_state = hashlib.sha256(b"outbox-genesis").digest()
    connection.execute(
        """
        INSERT INTO event_outbox(
            event_id,previous_event_id,previous_ordinal,commit_id,
            repository_revision,commit_event_count,operation_id,ordinal,
            topic,partition_key,event_bytes,event_sha256,payload_schema,
            codec_id,codec_version,status,state_revision,attempt_count,
            available_at,created_at,updated_at
        ) VALUES (?,NULL,NULL,?,1,1,?,0,'document.changed','project-a',
                  ?,?,'event/v1','ze.deterministic-json',1,'pending',1,0,?,?,?)
        """,
        (
            event_id,
            commit_id,
            operation_id,
            payload,
            payload_sha256,
            _NOW,
            _NOW,
            _NOW,
        ),
    )
    connection.execute(
        """
        INSERT INTO outbox_allocations(
            sequence,event_id,commit_id,repository_revision,ordinal,
            previous_tail_sha256,allocation_tail_sha256,allocated_at
        ) VALUES (1,?,?,1,0,?,?,?)
        """,
        (event_id, commit_id, zero, b"o" * 32, _NOW),
    )
    connection.execute(
        """
        INSERT INTO worker_targets(
            target_id,target_kind,genesis_status,genesis_state_sha256,
            project_id,generation,document_id,origin_commit_id,source_ordinal,
            payload_schema,payload_codec_id,payload_codec_version,payload_bytes,
            payload_sha256,idempotency_scope,idempotency_key,replay_policy,
            idempotency_policy,max_attempts,outbox_topic,
            outbox_partition_key,outbox_sequence,
            outbox_repository_revision,outbox_ordinal,created_at
        ) VALUES (?,'outbox','pending',?,'project-a',1,'document-a',?,0,
                  'event/v1','ze.deterministic-json',1,?,?,'outbox/project-a','event-a',
                  'provider_idempotent','required_external_key',?,
                  'document.changed','project-a',1,1,0,?)
        """,
        (
            event_id,
            genesis_state,
            commit_id,
            payload,
            payload_sha256,
            max_attempts,
            _NOW,
        ),
    )
    connection.execute(
        """
        INSERT INTO worker_target_current(
            target_id,target_kind,status,state_revision,attempt_count,
            fence_high_water,cancellation_pending,last_receipt_transaction_id,
            previous_state_sha256,state_sha256,updated_at
        ) VALUES (?,'outbox','pending',1,0,0,0,'test-genesis',?,?,?)
        """,
        (event_id, zero, genesis_state, _NOW),
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
        (zero, zero, _NOW),
    )
    return {
        "zero": zero,
        "event_id": event_id,
        "genesis_state": genesis_state,
        "claim_state": hashlib.sha256(b"outbox-claim").digest(),
        "effect_state": hashlib.sha256(b"outbox-effect").digest(),
        "terminal_state": hashlib.sha256(b"outbox-terminal").digest(),
        "attempt_id": "d" * 32,
        "effect_id": "e" * 32,
        "result_id": "f" * 32,
        "claim_tail": b"1" * 32,
        "effect_tail": b"2" * 32,
        "terminal_tail": b"3" * 32,
        # No separate dispatch-only hash domain exists.  A cursor-changing
        # head is the already frozen worker after-state digest, whose manifest
        # includes both worker_after and dispatch_after.
        "claimed_head": hashlib.sha256(b"outbox-claim").digest(),
        "terminal_head": hashlib.sha256(b"outbox-terminal").digest(),
        "max_attempts": max_attempts,
    }


def test_closed_claim_only_v8_worker_history_upgrades_to_v9() -> None:
    connection = _connection()
    zero = b"\0" * 32
    try:
        facts = _seed_v8_test_outbox_dispatch_target(
            connection,
            migrations=schema.MIGRATIONS[:8],
        )
        connection.execute(
            "INSERT INTO worker_ledger_head("
            "singleton,state_revision,sequence,previous_tail_sha256,"
            "tail_sha256,updated_at) VALUES (1,1,0,?,?,?)",
            (zero, zero, _NOW),
        )
        _claim_v8_test_outbox(connection, facts)
        receipt = connection.execute(
            "SELECT * FROM worker_execution_receipts WHERE sequence=1"
        ).fetchone()
        _receipt(
            connection,
            receipt["transaction_id"],
            receipt["receipt_kind"],
        )
        connection.execute(
            "INSERT INTO worker_ledger_entries("
            "sequence,entry_kind,transaction_id,receipt_kind,committed_at,"
            "previous_tail_sha256,ledger_tail_sha256,request_sha256,"
            "after_state_sha256,receipt_sha256,occurred_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                receipt["sequence"],
                receipt["entry_kind"],
                receipt["transaction_id"],
                receipt["receipt_kind"],
                receipt["committed_at"],
                receipt["previous_ledger_tail_sha256"],
                receipt["ledger_tail_sha256"],
                receipt["request_sha256"],
                receipt["after_state_sha256"],
                receipt["receipt_sha256"],
                receipt["committed_at"],
            ),
        )
        _set_v8_test_outbox_inflight(connection, facts)
        connection.execute(
            "UPDATE worker_ledger_head SET state_revision=2,sequence=1,"
            "previous_tail_sha256=?,tail_sha256=?,updated_at=? "
            "WHERE singleton=1",
            (zero, facts["claim_tail"], _NOW),
        )

        _apply_migrations(
            connection,
            (schema.WORKER_PAYLOAD_PROJECTION_MIGRATION,),
        )
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 9
        assert tuple(
            connection.execute(
                "SELECT operation,sequence FROM worker_execution_receipts"
            ).fetchone()
        ) == ("claim", 1)
        assert all(
            connection.execute('SELECT count(*) FROM "' + table + '"').fetchone()[0]
            == 0
            for table in schema.WORKER_PROJECTION_SIDECAR_TABLES
        )
    finally:
        connection.close()


def _claim_v8_test_outbox(
    connection: sqlite3.Connection,
    facts: dict[str, Any],
) -> None:
    claim_transaction = _insert_v8_test_worker_receipt(
        connection,
        request_id="1" * 32,
        sequence=1,
        operation="claim",
        target_id=facts["event_id"],
        before_status="pending",
        after_status="leased",
        before_revision=1,
        after_state_sha256=facts["claim_state"],
        previous_tail_sha256=facts["zero"],
        ledger_tail_sha256=facts["claim_tail"],
        receipt_sha256=b"r" * 32,
    )
    connection.execute(
        """
        INSERT INTO worker_attempts(
            attempt_id,target_id,attempt_number,previous_attempt_id,
            execution_fence,owner_id,started_at,receipt_transaction_id
        ) VALUES (?,?,1,NULL,1,'worker-a',?,?)
        """,
        (facts["attempt_id"], facts["event_id"], _NOW, claim_transaction),
    )
    connection.execute(
        """
        UPDATE event_outbox
        SET status='leased',state_revision=2,attempt_count=1,
            lease_owner='worker-a',lease_fence=1,lease_expires_at=?,updated_at=?
        WHERE event_id=?
        """,
        (_NOW, _NOW, facts["event_id"]),
    )
    connection.execute(
        """
        UPDATE worker_target_current
        SET status='leased',state_revision=2,attempt_count=1,
            current_attempt_id=?,current_attempt_number=1,
            fence_high_water=1,lease_owner='worker-a',lease_fence=1,
            lease_expires_at=?,last_receipt_transaction_id=?,
            previous_state_sha256=state_sha256,state_sha256=?,updated_at=?
        WHERE target_id=?
        """,
        (
            facts["attempt_id"],
            _NOW,
            claim_transaction,
            facts["claim_state"],
            _NOW,
            facts["event_id"],
        ),
    )


def _authorize_v8_test_outbox_effect(
    connection: sqlite3.Connection,
    facts: dict[str, Any],
) -> None:
    effect_transaction = _insert_v8_test_worker_receipt(
        connection,
        request_id="2" * 32,
        sequence=2,
        operation="authorize_effect",
        target_id=facts["event_id"],
        before_status="leased",
        after_status="leased",
        before_revision=2,
        after_state_sha256=facts["effect_state"],
        previous_tail_sha256=facts["claim_tail"],
        ledger_tail_sha256=facts["effect_tail"],
        receipt_sha256=b"s" * 32,
    )
    connection.execute(
        """
        INSERT INTO worker_effects(
            effect_id,target_id,attempt_id,effect_kind,
            authorization_transaction_id,idempotency_key,authorized_at
        ) VALUES (?,?,?,'outbox_publish',?,'provider-event-a',?)
        """,
        (
            facts["effect_id"],
            facts["event_id"],
            facts["attempt_id"],
            effect_transaction,
            _NOW,
        ),
    )
    connection.execute(
        "UPDATE event_outbox SET state_revision=3,updated_at=? WHERE event_id=?",
        (_NOW, facts["event_id"]),
    )
    connection.execute(
        """
        UPDATE worker_target_current
        SET state_revision=3,current_effect_id=?,
            last_receipt_transaction_id=?,previous_state_sha256=state_sha256,
            state_sha256=?,updated_at=?
        WHERE target_id=?
        """,
        (
            facts["effect_id"],
            effect_transaction,
            facts["effect_state"],
            _NOW,
            facts["event_id"],
        ),
    )


def _terminalize_v8_test_outbox(
    connection: sqlite3.Connection,
    facts: dict[str, Any],
    *,
    status: str,
    include_result: bool,
) -> str:
    terminal_transaction = _insert_v8_test_worker_receipt(
        connection,
        request_id="3" * 32,
        sequence=3,
        operation="complete",
        target_id=facts["event_id"],
        before_status="leased",
        after_status=status,
        before_revision=3,
        after_state_sha256=facts["terminal_state"],
        previous_tail_sha256=facts["effect_tail"],
        ledger_tail_sha256=facts["terminal_tail"],
        receipt_sha256=b"t" * 32,
    )
    if include_result:
        connection.execute(
            """
            INSERT INTO worker_results(
                result_id,target_id,attempt_id,effect_id,terminal_status,
                result_sha256,proof_sha256,receipt_transaction_id,completed_at
            ) VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                facts["result_id"],
                facts["event_id"],
                facts["attempt_id"],
                facts["effect_id"],
                status,
                hashlib.sha256(b"terminal-result").digest(),
                hashlib.sha256(b"terminal-proof").digest(),
                terminal_transaction,
                _NOW,
            ),
        )
    terminal_column = "published_at" if status == "published" else "dead_at"
    connection.execute(
        "UPDATE event_outbox SET status=?,state_revision=4,"
        "lease_owner=NULL,lease_fence=NULL,lease_expires_at=NULL," +
        terminal_column + "=?,updated_at=? WHERE event_id=?",
        (status, _NOW, _NOW, facts["event_id"]),
    )
    connection.execute(
        """
        UPDATE worker_target_current
        SET status=?,state_revision=4,last_attempt_id=?,last_attempt_number=1,
            current_attempt_id=NULL,current_attempt_number=NULL,
            lease_owner=NULL,lease_fence=NULL,lease_expires_at=NULL,
            current_effect_id=NULL,last_receipt_transaction_id=?,
            previous_state_sha256=state_sha256,state_sha256=?,updated_at=?
        WHERE target_id=?
        """,
        (
            status,
            facts["attempt_id"],
            terminal_transaction,
            facts["terminal_state"],
            _NOW,
            facts["event_id"],
        ),
    )
    return terminal_transaction


def _effectless_expire_manifest(
    facts: dict[str, Any],
    *,
    request_id: str,
    lease_expires_at: str = _NOW,
) -> bytes:
    return json.dumps(
        {
            "schema": "ze.worker-execution.manifest.v3",
            "operation": "expire",
            "header": {
                "request_transaction_id": request_id,
                "occurred_at": _NOW,
                "before": {
                    "current_attempt_id": facts["attempt_id"],
                    "current_attempt_number": 1,
                    "current_effect": None,
                },
            },
            "request": {
                "lease": {
                    "attempt_id": facts["attempt_id"],
                    "attempt_number": 1,
                    "lease_owner": "worker-a",
                    "execution_fence": 1,
                    "lease_expires_at": lease_expires_at,
                },
                "expired_at": _NOW,
                "target_status": "dead",
                "next_attempt_at": None,
            },
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _expire_v8_test_outbox_without_effect(
    connection: sqlite3.Connection,
    facts: dict[str, Any],
    *,
    operation: str = "expire",
    lease_expires_at: str = _NOW,
    terminal_time: str = _NOW,
) -> str:
    request_id = "3" * 32
    terminal_transaction = _insert_v8_test_worker_receipt(
        connection,
        request_id=request_id,
        sequence=2,
        operation=operation,
        target_id=facts["event_id"],
        before_status="leased",
        after_status="dead",
        before_revision=2,
        after_state_sha256=facts["terminal_state"],
        previous_tail_sha256=facts["claim_tail"],
        ledger_tail_sha256=facts["terminal_tail"],
        receipt_sha256=b"t" * 32,
        manifest_bytes=_effectless_expire_manifest(
            facts,
            request_id=request_id,
            lease_expires_at=lease_expires_at,
        ),
    )
    connection.execute(
        """
        UPDATE event_outbox
        SET status='dead',state_revision=3,
            lease_owner=NULL,lease_fence=NULL,lease_expires_at=NULL,
            dead_at=?,updated_at=?
        WHERE event_id=?
        """,
        (terminal_time, _NOW, facts["event_id"]),
    )
    connection.execute(
        """
        UPDATE worker_target_current
        SET status='dead',state_revision=3,
            last_attempt_id=?,last_attempt_number=1,
            current_attempt_id=NULL,current_attempt_number=NULL,
            lease_owner=NULL,lease_fence=NULL,lease_expires_at=NULL,
            current_effect_id=NULL,unresolved_effect_id=NULL,
            last_receipt_transaction_id=?,
            previous_state_sha256=state_sha256,state_sha256=?,updated_at=?
        WHERE target_id=?
        """,
        (
            facts["attempt_id"],
            terminal_transaction,
            facts["terminal_state"],
            _NOW,
            facts["event_id"],
        ),
    )
    return terminal_transaction


def _set_v8_test_outbox_inflight(
    connection: sqlite3.Connection,
    facts: dict[str, Any],
) -> None:
    connection.execute(
        """
        UPDATE outbox_dispatch_head
        SET state_revision=2,inflight_sequence=1,
            inflight_repository_revision=1,inflight_ordinal=0,
            previous_head_sha256=?,head_sha256=?,updated_at=?
        WHERE singleton=1
        """,
        (facts["zero"], facts["claimed_head"], _NOW),
    )


def _advance_v8_test_outbox_published(
    connection: sqlite3.Connection,
    facts: dict[str, Any],
) -> None:
    connection.execute(
        """
        UPDATE outbox_dispatch_head
        SET state_revision=3,published_sequence=1,
            published_repository_revision=1,published_ordinal=0,
            inflight_sequence=NULL,inflight_repository_revision=NULL,
            inflight_ordinal=NULL,previous_head_sha256=?,head_sha256=?,updated_at=?
        WHERE singleton=1
        """,
        (facts["claimed_head"], facts["terminal_head"], _NOW),
    )


def test_v8_outbox_dispatch_first_claim_is_exact_and_inflight_cannot_vanish() -> None:
    connection = _connection()
    try:
        facts = _seed_v8_test_outbox_dispatch_target(connection)
        with pytest.raises(
            sqlite3.IntegrityError,
            match="production_outbox_dispatch_invalid",
        ):
            _set_v8_test_outbox_inflight(connection, facts)

        _claim_v8_test_outbox(connection, facts)
        wrong_claim_head = dict(facts)
        wrong_claim_head["claimed_head"] = b"x" * 32
        with pytest.raises(
            sqlite3.IntegrityError,
            match="production_outbox_dispatch_invalid",
        ):
            _set_v8_test_outbox_inflight(connection, wrong_claim_head)
        _set_v8_test_outbox_inflight(connection, facts)
        assert tuple(connection.execute(
            "SELECT published_sequence,inflight_sequence FROM outbox_dispatch_head"
        ).fetchone()) == (0, 1)

        with pytest.raises(
            sqlite3.IntegrityError,
            match="production_outbox_dispatch_invalid",
        ):
            connection.execute(
                """
                UPDATE outbox_dispatch_head
                SET state_revision=3,inflight_sequence=NULL,
                    inflight_repository_revision=NULL,inflight_ordinal=NULL,
                    previous_head_sha256=?,head_sha256=?,updated_at=?
                WHERE singleton=1
                """,
                (facts["claimed_head"], b"x" * 32, _NOW),
            )
    finally:
        connection.close()


def test_v8_outbox_dispatch_advances_only_after_exact_terminal_effect_chain() -> None:
    connection = _connection()
    try:
        facts = _seed_v8_test_outbox_dispatch_target(connection)
        _claim_v8_test_outbox(connection, facts)
        _set_v8_test_outbox_inflight(connection, facts)
        _authorize_v8_test_outbox_effect(connection, facts)

        with pytest.raises(
            sqlite3.IntegrityError,
            match="production_outbox_dispatch_invalid",
        ):
            _advance_v8_test_outbox_published(connection, facts)

        _terminalize_v8_test_outbox(
            connection,
            facts,
            status="published",
            include_result=True,
        )
        wrong_terminal_head = dict(facts)
        wrong_terminal_head["terminal_head"] = b"x" * 32
        with pytest.raises(
            sqlite3.IntegrityError,
            match="production_outbox_dispatch_invalid",
        ):
            _advance_v8_test_outbox_published(connection, wrong_terminal_head)
        _advance_v8_test_outbox_published(connection, facts)
        assert tuple(connection.execute(
            "SELECT published_sequence,published_repository_revision,"
            "published_ordinal,inflight_sequence FROM outbox_dispatch_head"
        ).fetchone()) == (1, 1, 0, None)
    finally:
        connection.close()


def test_v8_outbox_dead_without_terminal_result_cannot_skip_the_event() -> None:
    connection = _connection()
    try:
        facts = _seed_v8_test_outbox_dispatch_target(connection)
        _claim_v8_test_outbox(connection, facts)
        _set_v8_test_outbox_inflight(connection, facts)
        _authorize_v8_test_outbox_effect(connection, facts)
        terminal_transaction = _terminalize_v8_test_outbox(
            connection,
            facts,
            status="dead",
            include_result=False,
        )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="production_outbox_dispatch_invalid",
        ):
            _advance_v8_test_outbox_published(connection, facts)

        connection.execute(
            """
            INSERT INTO worker_results(
                result_id,target_id,attempt_id,effect_id,terminal_status,
                result_sha256,proof_sha256,receipt_transaction_id,completed_at
            ) VALUES (?,?,?,?,'dead',?,?,?,?)
            """,
            (
                facts["result_id"],
                facts["event_id"],
                facts["attempt_id"],
                facts["effect_id"],
                hashlib.sha256(b"dead-result").digest(),
                hashlib.sha256(b"strong-dead-proof").digest(),
                terminal_transaction,
                _NOW,
            ),
        )
        _advance_v8_test_outbox_published(connection, facts)
    finally:
        connection.close()


def test_v8_outbox_exhausted_effectless_expiry_advances_exact_cursor() -> None:
    connection = _connection()
    try:
        facts = _seed_v8_test_outbox_dispatch_target(
            connection,
            max_attempts=1,
        )
        _claim_v8_test_outbox(connection, facts)
        _set_v8_test_outbox_inflight(connection, facts)
        _expire_v8_test_outbox_without_effect(connection, facts)

        _advance_v8_test_outbox_published(connection, facts)

        assert tuple(connection.execute(
            "SELECT published_sequence,published_repository_revision,"
            "published_ordinal,inflight_sequence FROM outbox_dispatch_head"
        ).fetchone()) == (1, 1, 0, None)
        assert connection.execute(
            "SELECT count(*) FROM worker_effects WHERE target_id=?",
            (facts["event_id"],),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT count(*) FROM worker_results WHERE target_id=?",
            (facts["event_id"],),
        ).fetchone()[0] == 0
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("max_attempts", "operation"),
    (
        (3, "expire"),
        (1, "complete"),
    ),
)
def test_v8_outbox_effectless_dead_cannot_advance_without_exact_exhausted_expiry(
    max_attempts: int,
    operation: str,
) -> None:
    connection = _connection()
    try:
        facts = _seed_v8_test_outbox_dispatch_target(
            connection,
            max_attempts=max_attempts,
        )
        _claim_v8_test_outbox(connection, facts)
        _set_v8_test_outbox_inflight(connection, facts)
        _expire_v8_test_outbox_without_effect(
            connection,
            facts,
            operation=operation,
        )

        with pytest.raises(
            sqlite3.IntegrityError,
            match="production_outbox_dispatch_invalid",
        ):
            _advance_v8_test_outbox_published(connection, facts)
    finally:
        connection.close()


def test_v8_outbox_effectless_expiry_requires_exact_terminal_time_closure() -> None:
    connection = _connection()
    try:
        facts = _seed_v8_test_outbox_dispatch_target(
            connection,
            max_attempts=1,
        )
        _claim_v8_test_outbox(connection, facts)
        _set_v8_test_outbox_inflight(connection, facts)
        _expire_v8_test_outbox_without_effect(
            connection,
            facts,
            terminal_time="2026-07-11T00:00:01.000Z",
        )

        with pytest.raises(
            sqlite3.IntegrityError,
            match="production_outbox_dispatch_invalid",
        ):
            _advance_v8_test_outbox_published(connection, facts)
    finally:
        connection.close()


def test_current_outbox_effectless_expiry_rejects_a_fabricated_result() -> None:
    connection = _connection()
    try:
        facts = _seed_v8_test_outbox_dispatch_target(
            connection,
            max_attempts=1,
        )
        _claim_v8_test_outbox(connection, facts)
        _set_v8_test_outbox_inflight(connection, facts)
        terminal_transaction = _expire_v8_test_outbox_without_effect(
            connection,
            facts,
        )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="worker_payload_projection_result_invalid",
        ):
            connection.execute(
                """
                INSERT INTO worker_results(
                    result_id,target_id,attempt_id,effect_id,terminal_status,
                    result_sha256,proof_sha256,receipt_transaction_id,completed_at
                ) VALUES (?,?,?,NULL,'dead',?,?,?,?)
                """,
                (
                    facts["result_id"],
                    facts["event_id"],
                    facts["attempt_id"],
                    hashlib.sha256(b"fabricated-result").digest(),
                    hashlib.sha256(b"fabricated-proof").digest(),
                    terminal_transaction,
                    _NOW,
                ),
            )

        _advance_v8_test_outbox_published(connection, facts)
    finally:
        connection.close()


def _insert_v8_install_and_head(connection: sqlite3.Connection) -> dict[str, Any]:
    cutover_id = "f" * 64
    transaction_id = "_business-cutover-install:" + cutover_id
    install_manifest = b"i" * 32
    _receipt(connection, transaction_id, "_business_cutover_install_v1")
    connection.execute(
        "INSERT INTO business_cutover_install_receipts("
        "cutover_id,transaction_id,receipt_kind,committed_at,repository_epoch,"
        "runtime_generation,code_digest,member_mapping_sha256,manifest_bytes,"
        "manifest_sha256) VALUES (?,?, '_business_cutover_install_v1',?,?,?,?,?,?,?)",
        (
            cutover_id, transaction_id, _NOW, "e" * 64, "runtime-v8",
            b"c" * 32, b"m" * 32, b"{}", install_manifest,
        ),
    )
    connection.execute(
        "INSERT INTO business_cutover_head("
        "singleton,cutover_id,state_revision,phase_revision,phase,"
        "authority_state,repository_epoch,runtime_generation,code_digest,"
        "install_transaction_id,install_manifest_sha256,"
        "previous_head_sha256,head_sha256,updated_at) "
        "VALUES (1,?,1,1,'installed','read_only',?,?,?,?,?,?,?,?)",
        (
            cutover_id, "e" * 64, "runtime-v8", b"c" * 32,
            transaction_id, install_manifest, b"\0" * 32, b"h" * 32, _NOW,
        ),
    )
    return {
        "cutover_id": cutover_id,
        "transaction_id": transaction_id,
        "manifest_sha256": install_manifest,
    }


def test_v8_cutover_phase_binding_and_history_are_fail_closed() -> None:
    connection = _connection()
    try:
        _apply_migrations(connection, schema.MIGRATIONS[:11])
        installed = _insert_v8_install_and_head(connection)
        with pytest.raises(
            sqlite3.IntegrityError,
            match="production_cutover_phase_invalid",
        ):
            connection.execute(
                "UPDATE business_cutover_head SET state_revision=2,"
                "phase_revision=3,phase='audited',"
                "coverage_transaction_id='missing-coverage',"
                "coverage_manifest_sha256=?,audit_transaction_id='missing-audit',"
                "audit_manifest_sha256=?,previous_head_sha256=head_sha256,"
                "head_sha256=?,updated_at=? WHERE singleton=1",
                (b"v" * 32, b"a" * 32, b"n" * 32, _NOW),
            )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="production_history_append_only",
        ):
            connection.execute(
                "UPDATE business_cutover_install_receipts "
                "SET manifest_sha256=manifest_sha256 WHERE cutover_id=?",
                (installed["cutover_id"],),
            )
    finally:
        connection.close()


def test_v8_worker_schema_persists_receipt_bytes_and_only_final_attempt_facts() -> None:
    connection = _connection()
    try:
        _apply_migrations(connection, schema.MIGRATIONS)

        def columns(table: str) -> set[str]:
            return {
                row["name"]
                for row in connection.execute(
                    'PRAGMA table_info("' + table + '")'
                ).fetchall()
            }

        assert {
            "manifest_bytes", "manifest_sha256", "receipt_sha256",
            "production_manifest_bytes", "production_manifest_sha256",
            "target_count", "targets_sha256", "production_receipt_sha256",
        }.issubset(columns("atomic_business_write_receipts"))
        assert "manifest_bytes" in columns("worker_execution_receipts")
        assert "manifest_bytes" in columns("pipeline_control_receipts")
        assert {
            "source_ordinal", "genesis_status", "genesis_state_sha256",
            "action_type", "pipeline_id", "intent_type", "outbox_topic",
            "outbox_partition_key",
        }.issubset(columns("worker_targets"))
        assert "source_ordinal" not in columns("pipeline_control_intents")
        assert {"finished_at", "outcome"}.isdisjoint(
            columns("worker_attempts")
        )
        assert "provider_reference_sha256" not in columns("worker_effects")
        proof_columns = {
            row["name"]: row
            for row in connection.execute(
                'PRAGMA table_info("worker_reconcile_proofs")'
            ).fetchall()
        }
        assert proof_columns["effect_id"]["notnull"] == 1

        current_foreign_keys: dict[int, list[tuple[str, str]]] = {}
        for row in connection.execute(
            'PRAGMA foreign_key_list("worker_target_current")'
        ).fetchall():
            if row["table"] == "worker_attempts":
                current_foreign_keys.setdefault(row["id"], []).append(
                    (row["from"], row["to"])
                )
        assert [
            ("last_attempt_id", "attempt_id"),
            ("target_id", "target_id"),
            ("last_attempt_number", "attempt_number"),
        ] in current_foreign_keys.values()
        assert [
            ("current_attempt_id", "attempt_id"),
            ("target_id", "target_id"),
            ("current_attempt_number", "attempt_number"),
            ("lease_owner", "owner_id"),
            ("lease_fence", "execution_fence"),
        ] in current_foreign_keys.values()

        trigger_sql = {
            row["name"]: row["sql"]
            for row in connection.execute(
                "SELECT name, sql FROM sqlite_schema WHERE type='trigger'"
            ).fetchall()
        }
        assert "pipeline_jobs" in trigger_sql[
            "worker_targets_pipeline_binding_guard"
        ]
        assert "pipeline_job_transitions" in trigger_sql[
            "worker_targets_pipeline_binding_guard"
        ]
        assert "source_ordinal" in trigger_sql[
            "worker_targets_pipeline_binding_guard"
        ]
        assert "pipeline_intent_count" in trigger_sql[
            "pipeline_control_intents_origin_guard"
        ]
        assert "repository_runtime_control" in trigger_sql[
            "pipeline_control_intents_origin_guard"
        ]
    finally:
        connection.close()


def test_v8_worker_current_status_pointer_contract_and_first_claim_revision() -> None:
    connection = _connection()
    zero = b"\0" * 32
    shared_state = b"s" * 32
    try:
        _apply_migrations(connection, schema.MIGRATIONS)
        # Isolate the current-row CHECK/CAS contract from the independently
        # tested target/receipt binding triggers and composite foreign keys.
        connection.execute("DROP TRIGGER worker_target_current_insert_binding_guard")
        connection.execute("DROP TRIGGER worker_target_current_update_binding_guard")
        connection.execute("PRAGMA foreign_keys=OFF")

        insert_sql = """
            INSERT INTO worker_target_current(
                target_id, target_kind, status, state_revision, attempt_count,
                fence_high_water, cancellation_pending,
                last_receipt_transaction_id, previous_state_sha256,
                state_sha256, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        connection.execute(
            insert_sql,
            ("1" * 32, "action", "queued", 1, 0, 0, 0,
             "genesis-1", zero, shared_state, _NOW),
        )
        connection.execute(
            insert_sql,
            ("2" * 32, "action", "queued", 1, 0, 0, 0,
             "genesis-2", zero, shared_state, _NOW),
        )
        assert connection.execute(
            "SELECT count(*) FROM worker_target_current WHERE state_sha256=?",
            (shared_state,),
        ).fetchone()[0] == 2

        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            connection.execute(
                insert_sql,
                ("3" * 32, "pipeline", "running", 1, 0, 0, 0,
                 "invalid-running", zero, b"3" * 32, _NOW),
            )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            connection.execute(
                """
                INSERT INTO worker_target_current(
                    target_id,target_kind,status,state_revision,attempt_count,
                    current_attempt_id,current_attempt_number,fence_high_water,
                    cancellation_pending,lease_owner,lease_fence,lease_expires_at,
                    last_receipt_transaction_id,previous_state_sha256,
                    state_sha256,updated_at
                ) VALUES (?, 'action', 'queued', 1, 1, ?, 1, 1, 0,
                          'worker', 1, ?, 'invalid-idle', ?, ?, ?)
                """,
                ("4" * 32, "a" * 32, _NOW, zero, b"4" * 32, _NOW),
            )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            connection.execute(
                insert_sql,
                ("5" * 32, "action", "outcome_unknown", 1, 0, 0, 0,
                 "missing-effect", zero, b"5" * 32, _NOW),
            )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            connection.execute(
                """
                INSERT INTO worker_target_current(
                    target_id,target_kind,status,state_revision,attempt_count,
                    fence_high_water,cancellation_pending,unresolved_effect_id,
                    last_receipt_transaction_id,previous_state_sha256,
                    state_sha256,updated_at
                ) VALUES (?, 'action', 'outcome_unknown', 1, 0, 0, 0, ?,
                          'orphan-effect', ?, ?, ?)
                """,
                ("7" * 32, "e" * 32, zero, b"7" * 32, _NOW),
            )
        with pytest.raises(
            sqlite3.IntegrityError, match="production_head_cas_invalid"
        ):
            connection.execute(
                insert_sql,
                ("6" * 32, "action", "queued", 2, 0, 0, 0,
                 "not-genesis", shared_state, b"6" * 32, _NOW),
            )

        connection.execute(
            """
            UPDATE worker_target_current
            SET status='running', state_revision=2, attempt_count=1,
                current_attempt_id=?, current_attempt_number=1,
                fence_high_water=1, lease_owner='worker', lease_fence=1,
                lease_expires_at=?, last_receipt_transaction_id='claim-1',
                previous_state_sha256=state_sha256, state_sha256=?, updated_at=?
            WHERE target_id=?
            """,
            ("a" * 32, _NOW, b"n" * 32, _NOW, "1" * 32),
        )
        assert tuple(connection.execute(
            "SELECT status,state_revision,current_attempt_number "
            "FROM worker_target_current WHERE target_id=?",
            ("1" * 32,),
        ).fetchone()) == ("running", 2, 1)
    finally:
        connection.close()


def test_v8_asset_stage_is_exactly_closed_before_coverage() -> None:
    connection = _connection()
    cutover_id = "a" * 64
    batch_id = "b" * 64
    stage_transaction = (
        "_business-cutover-asset-stage:" + cutover_id + ":" + batch_id
    )
    coverage_transaction = "_business-cutover-asset-coverage:" + cutover_id
    stage_manifest = b'{"batch":1}'
    try:
        _apply_migrations(connection, schema.MIGRATIONS)
        connection.execute("BEGIN")
        for kind, member_id, size in (
            ("project", "project-a", 0),
            ("blob", "blob-a", 7),
            ("occurrence", "occurrence-a", 7),
        ):
            connection.execute(
                """
                INSERT INTO business_asset_stage_members(
                    batch_id,cutover_id,member_kind,ordinal,member_id,
                    member_bytes,member_sha256,metadata_sha256
                ) VALUES (?,?,?,?,?,?,?,?)
                """,
                (batch_id, cutover_id, kind, 0, member_id, size,
                 hashlib.sha256(member_id.encode()).digest(), b"m" * 32),
            )
        _receipt(
            connection,
            stage_transaction,
            "_business_cutover_asset_stage_v1",
        )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="production_asset_stage_closure_invalid",
        ):
            connection.execute(
                """
                INSERT INTO business_asset_stage_receipts(
                    batch_id,cutover_id,transaction_id,receipt_kind,committed_at,
                    repository_epoch,source_admission_revision,
                    source_admission_sha256,member_count,project_count,blob_count,
                    occurrence_count,unique_blob_bytes,referenced_bytes,
                    project_root_sha256,blob_root_sha256,occurrence_root_sha256,
                    coverage_sha256,manifest_bytes,manifest_sha256
                ) VALUES (?,?,?,'_business_cutover_asset_stage_v1',?, ?,1,?,
                          4,1,1,2,7,14,?,?,?,?,?,?)
                """,
                (batch_id, cutover_id, stage_transaction, _NOW, "e" * 64,
                 b"a" * 32, b"p" * 32, b"b" * 32, b"o" * 32, b"c" * 32,
                 stage_manifest, hashlib.sha256(stage_manifest).digest()),
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
            ) VALUES (?,?,?,'_business_cutover_asset_stage_v1',?, ?,1,?,
                      3,1,1,1,7,7,?,?,?,?,?,?)
            """,
            (batch_id, cutover_id, stage_transaction, _NOW, "e" * 64,
             b"a" * 32, b"p" * 32, b"b" * 32, b"o" * 32, b"c" * 32,
             stage_manifest, hashlib.sha256(stage_manifest).digest()),
        )
        connection.execute("COMMIT")

        with pytest.raises(
            sqlite3.IntegrityError, match="production_asset_stage_finalized"
        ):
            connection.execute(
                """
                INSERT INTO business_asset_stage_members(
                    batch_id,cutover_id,member_kind,ordinal,member_id,
                    member_bytes,member_sha256,metadata_sha256
                ) VALUES (?,?,'blob',1,'late-blob',1,?,?)
                """,
                (batch_id, cutover_id, b"l" * 32, b"m" * 32),
            )

        _receipt(
            connection,
            coverage_transaction,
            "_business_cutover_asset_coverage_v1",
        )
        coverage_manifest = b'{"coverage":1}'
        coverage_sql = """
            INSERT INTO business_asset_coverage_receipts(
                cutover_id,transaction_id,receipt_kind,committed_at,
                repository_epoch,source_admission_revision,
                source_admission_sha256,batch_count,member_root_sha256,
                repository_member_count,live_project_count,
                deleted_project_count,observed_occurrence_count,
                observed_row_count,observed_manifest_bytes,project_count,
                reference_count,unique_blob_count,unique_blob_bytes,
                referenced_bytes,project_root_sha256,blob_root_sha256,
                occurrence_root_sha256,coverage_sha256,manifest_bytes,
                manifest_sha256
            ) VALUES (?,?, '_business_cutover_asset_coverage_v1',?, ?,1,?,
                      1,?,1,1,0,1,?,?,1,1,1,7,7,?,?,?,?,?,?)
        """
        coverage_values = (
            cutover_id, coverage_transaction, _NOW, "e" * 64, b"a" * 32,
            b"r" * 32, 3, len(stage_manifest), b"p" * 32, b"b" * 32,
            b"o" * 32, b"c" * 32, coverage_manifest,
            hashlib.sha256(coverage_manifest).digest(),
        )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="production_asset_coverage_closure_invalid",
        ):
            connection.execute(
                coverage_sql,
                (*coverage_values[:6], 4, *coverage_values[7:]),
            )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="production_asset_coverage_closure_invalid",
        ):
            connection.execute(
                coverage_sql,
                (*coverage_values[:3], "d" * 64, *coverage_values[4:]),
            )
        connection.execute(coverage_sql, coverage_values)
        with pytest.raises(
            sqlite3.IntegrityError, match="production_asset_stage_finalized"
        ):
            connection.execute(
                """
                INSERT INTO business_asset_stage_members(
                    batch_id,cutover_id,member_kind,ordinal,member_id,
                    member_bytes,member_sha256,metadata_sha256
                ) VALUES (?,?,'project',0,'late-project',0,?,?)
                """,
                ("d" * 64, cutover_id, b"l" * 32, b"m" * 32),
            )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_v8_rejects_unmounted_activation_and_unclosed_atomic_progress() -> None:
    connection = _connection()
    try:
        _apply_migrations(connection, schema.MIGRATIONS)
        connection.execute(
            "UPDATE kernel_repository SET authority='sqlite' WHERE singleton=1"
        )
        runtime_values = (
            "e" * 64,
            "f" * 64,
            b"c" * 32,
            _NOW,
        )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="production_activation_authorization_required",
        ):
            connection.execute(
                "INSERT INTO repository_runtime_control("
                "authority,authority_state,repository_epoch,cutover_id,"
                "source_admission_revision,repository_revision,state_revision,"
                "runtime_generation,code_digest,schema_version,updated_at) "
                "VALUES ('sqlite','active',?,?,1,1,1,'runtime-v8',?,6,?)",
                runtime_values,
            )
        connection.execute(
            "INSERT INTO repository_runtime_control("
            "authority,authority_state,repository_epoch,cutover_id,"
            "source_admission_revision,repository_revision,state_revision,"
            "runtime_generation,code_digest,schema_version,updated_at) "
            "VALUES ('sqlite','read_only',?,?,1,1,1,'runtime-v8',?,6,?)",
            runtime_values,
        )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="production_atomic_finalization_required",
        ):
            connection.execute(
                "UPDATE repository_runtime_control SET repository_revision=2,"
                "updated_at=? WHERE singleton=1",
                (_NOW,),
            )
    finally:
        connection.close()


def _insert_frozen_v6_stream(
    connection: sqlite3.Connection,
    *,
    project_id: str,
    document_id: str,
    commit_id: str,
    repository_revision: int,
    project_revision: int,
    head_revision: int,
    repository_epoch: str,
    cutover_id: str,
) -> dict[str, Any]:
    """Insert one baseline using only the published v6 column surface."""

    project_payload = (project_id + "-v1").encode("utf-8")
    project_hash = hashlib.sha256(project_payload).digest()
    document_payload = (document_id + "-v1").encode("utf-8")
    document_hash = hashlib.sha256(document_payload).digest()
    operation_tail = hashlib.sha256((project_id + "-tail-v1").encode()).digest()
    asset_projection = hashlib.sha256(
        (project_id + "-asset-projection-v1").encode()
    ).digest()
    connection.execute(
        """
        INSERT INTO projects(
            project_id, generation, codec_id, codec_version,
            snapshot_format_version, snapshot_bytes, snapshot_sha256
        ) VALUES (?, 1, 'json', 1, 1, ?, ?)
        """,
        (project_id, project_payload, project_hash),
    )
    connection.execute(
        """
        INSERT INTO documents(
            project_id, document_id, document_version, schema_version,
            collaboration_mode, save_policy, codec_id, codec_version,
            snapshot_format_version, snapshot_bytes, snapshot_sha256
        ) VALUES (?, ?, 1, '1.0.0', 'with_save', 'auto_commit',
                  'json', 1, 1, ?, ?)
        """,
        (project_id, document_id, document_payload, document_hash),
    )
    connection.execute(
        """
        INSERT INTO repository_projects(
            project_id, generation, project_revision, status,
            current_snapshot_sha256, created_at, updated_at
        ) VALUES (?, 1, ?, 'live', ?, ?, ?)
        """,
        (project_id, project_revision, project_hash, _NOW, _NOW),
    )
    transaction_id = schema.BUSINESS_TRANSACTION_PREFIX + commit_id
    _receipt(connection, transaction_id, schema.BUSINESS_TRANSACTION_KIND)
    manifest = ("frozen-v6-" + commit_id).encode("ascii")
    connection.execute(
        """
        INSERT INTO business_commits(
            commit_id, transaction_id, receipt_kind, project_id, generation,
            repository_revision, repository_epoch, cutover_id,
            source_admission_revision, document_id, document_version,
            previous_commit_id, commit_kind, save_policy, trace_id,
            operation_count, operation_target_count, outbox_event_count,
            durable_action_count, pipeline_intent_count,
            before_root_sha256, after_root_sha256,
            previous_operation_tail_sha256, operation_tail_sha256,
            asset_projection_sha256, asset_reference_count, entity_count,
            manifest_bytes, manifest_sha256, committed_at
        ) VALUES (
            ?, ?, ?, ?, 1, ?, ?, ?, 7, ?, 1, NULL, 'baseline',
            'auto_commit', ?, 0, 0, 0, 0, 0, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?
        )
        """,
        (
            commit_id,
            transaction_id,
            schema.BUSINESS_TRANSACTION_KIND,
            project_id,
            repository_revision,
            repository_epoch,
            cutover_id,
            document_id,
            "trace-" + commit_id,
            b"0" * 32,
            project_hash,
            b"0" * 32,
            operation_tail,
            asset_projection,
            manifest,
            hashlib.sha256(manifest).digest(),
            _NOW,
        ),
    )
    connection.execute(
        """
        INSERT INTO document_commit_heads(
            project_id, generation, document_id, document_version, commit_id,
            head_revision, status, current_snapshot_sha256,
            current_project_snapshot_sha256, business_root_sha256,
            operation_tail_sha256, asset_projection_sha256,
            asset_reference_count, entity_count, updated_at
        ) VALUES (?, 1, ?, 1, ?, ?, 'live', ?, ?, ?, ?, ?, 0, 0, ?)
        """,
        (
            project_id,
            document_id,
            commit_id,
            head_revision,
            document_hash,
            project_hash,
            project_hash,
            operation_tail,
            asset_projection,
            _NOW,
        ),
    )
    return {
        "project_hash": project_hash,
        "document_hash": document_hash,
        "operation_tail": operation_tail,
        "asset_projection": asset_projection,
        "commit_id": commit_id,
        "transaction_id": transaction_id,
        "manifest": manifest,
    }


def _seed_frozen_v6_baseline(
    connection: sqlite3.Connection,
) -> dict[str, Any]:
    """Create the one-stream legacy shape that v7 can upgrade losslessly."""

    repository_epoch = "e" * 64
    cutover_id = "f" * 64
    connection.execute(
        "UPDATE kernel_repository SET authority='sqlite' WHERE singleton=1"
    )
    connection.execute(
        """
        INSERT INTO repository_runtime_control(
            authority, authority_state, repository_epoch, cutover_id,
            source_admission_revision, repository_revision, state_revision,
            runtime_generation, code_digest, schema_version, updated_at
        ) VALUES ('sqlite', 'active', ?, ?, 7, 1, 1,
                  'runtime-frozen-v6', ?, 6, ?)
        """,
        (repository_epoch, cutover_id, b"c" * 32, _NOW),
    )
    facts = _insert_frozen_v6_stream(
        connection,
        project_id="project-a",
        document_id="document-a",
        commit_id="a" * 32,
        repository_revision=1,
        project_revision=1,
        head_revision=1,
        repository_epoch=repository_epoch,
        cutover_id=cutover_id,
    )
    facts.update(
        {
            "repository_epoch": repository_epoch,
            "cutover_id": cutover_id,
        }
    )
    return facts


def _seed_v6_live_projection(connection: sqlite3.Connection) -> dict[str, Any]:
    repository_epoch = "e" * 64
    cutover_id = "f" * 64
    connection.execute(
        "UPDATE kernel_repository SET authority='sqlite' WHERE singleton=1"
    )
    connection.execute(
        """
        INSERT INTO repository_runtime_control(
            authority, authority_state, repository_epoch,
            cutover_id, source_admission_revision, repository_revision,
            state_revision, runtime_generation, code_digest, schema_version,
            updated_at
        ) VALUES ('sqlite', 'active', ?, ?, 7, 1, 1, 'runtime-v6', ?, 6, ?)
        """,
        (repository_epoch, cutover_id, b"c" * 32, _NOW),
    )
    project_snapshot = b"project-v1"
    project_hash = hashlib.sha256(project_snapshot).digest()
    document_snapshot = b"document-v1"
    document_hash = hashlib.sha256(document_snapshot).digest()
    connection.execute(
        """
        INSERT INTO projects(
            project_id, generation, codec_id, codec_version,
            snapshot_format_version, snapshot_bytes, snapshot_sha256
        ) VALUES ('project-v6', 1, 'json', 1, 1, ?, ?)
        """,
        (project_snapshot, project_hash),
    )
    connection.execute(
        """
        INSERT INTO documents(
            project_id, document_id, document_version, schema_version,
            collaboration_mode, save_policy, codec_id, codec_version,
            snapshot_format_version, snapshot_bytes, snapshot_sha256
        ) VALUES (
            'project-v6', 'document-v6', 1, '1.0.0', 'with_save',
            'auto_commit', 'json', 1, 1, ?, ?
        )
        """,
        (document_snapshot, document_hash),
    )
    commit_id = "1" * 32
    transaction_id = schema.BUSINESS_TRANSACTION_PREFIX + commit_id
    _receipt(connection, transaction_id, schema.BUSINESS_TRANSACTION_KIND)
    connection.execute(
        """
        INSERT INTO business_commits(
            commit_id, transaction_id, receipt_kind, project_id, generation,
            repository_revision, project_revision, head_revision,
            repository_epoch, cutover_id,
            source_admission_revision,
            document_id, document_version,
            previous_commit_id, commit_kind, save_policy, trace_id,
            operation_count, operation_target_count, outbox_event_count,
            durable_action_count, pipeline_intent_count,
            before_root_sha256, after_root_sha256,
            previous_operation_tail_sha256, operation_tail_sha256,
            asset_projection_sha256, asset_reference_count, entity_count,
            manifest_bytes, manifest_sha256, committed_at
        ) VALUES (?, ?, ?, 'project-v6', 1, 1, 1, 1, ?, ?, 7,
                  'document-v6', 1,
                  NULL, 'baseline', 'auto_commit', 'trace-v6', 0, 0,
                  0, 0, 0, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?)
        """,
        (
            commit_id,
            transaction_id,
            schema.BUSINESS_TRANSACTION_KIND,
            repository_epoch,
            cutover_id,
            b"0" * 32,
            project_hash,
            b"0" * 32,
            b"t" * 32,
            b"v" * 32,
            b"{}",
            hashlib.sha256(b"{}").digest(),
            _NOW,
        ),
    )
    connection.execute(
        """
        INSERT INTO repository_projects(
            project_id, generation, project_revision, status,
            current_snapshot_sha256, created_at, updated_at
        ) VALUES ('project-v6', 1, 1, 'live', ?, ?, ?)
        """,
        (project_hash, _NOW, _NOW),
    )
    connection.execute(
        """
        INSERT INTO document_commit_heads(
            project_id, generation, document_id, document_version, commit_id,
            head_revision, status, current_snapshot_sha256,
            current_project_snapshot_sha256, business_root_sha256,
            operation_tail_sha256, asset_projection_sha256,
            asset_reference_count, entity_count, updated_at
        ) VALUES (
            'project-v6', 1, 'document-v6', 1, ?, 1, 'live', ?, ?, ?, ?, ?,
            0, 0, ?
        )
        """,
        (
            commit_id,
            document_hash,
            project_hash,
            project_hash,
            b"t" * 32,
            b"v" * 32,
            _NOW,
        ),
    )
    return {
        "project_hash": project_hash,
        "document_hash": document_hash,
        "commit_id": commit_id,
        "repository_epoch": repository_epoch,
        "cutover_id": cutover_id,
    }


def test_v6_current_projections_are_exact_to_v2_rows_but_history_is_stable() -> None:
    connection = _connection()
    try:
        _apply_migrations(connection, schema.MIGRATIONS[:7])
        connection.execute("BEGIN")
        facts = _seed_v6_live_projection(connection)
        connection.execute("COMMIT")

        connection.execute("BEGIN")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO repository_projects(
                    project_id, generation, project_revision, status,
                    current_snapshot_sha256, created_at, updated_at
                ) VALUES ('missing-project', 1, 1, 'live', ?, ?, ?)
                """,
                (b"x" * 32, _NOW, _NOW),
            )
            connection.execute("COMMIT")
        connection.execute("ROLLBACK")

        next_project = b"project-v2"
        next_project_hash = hashlib.sha256(next_project).digest()
        next_document = b"document-v2"
        next_document_hash = hashlib.sha256(next_document).digest()
        connection.execute("BEGIN")
        connection.execute(
            "UPDATE projects SET snapshot_bytes=?, snapshot_sha256=? "
            "WHERE project_id='project-v6'",
            (next_project, next_project_hash),
        )
        connection.execute(
            "UPDATE documents SET document_version=2, snapshot_bytes=?, "
            "snapshot_sha256=? WHERE project_id='project-v6'",
            (next_document, next_document_hash),
        )
        second_commit = "2" * 32
        second_transaction = schema.BUSINESS_TRANSACTION_PREFIX + second_commit
        _receipt(connection, second_transaction, schema.BUSINESS_TRANSACTION_KIND)
        connection.execute(
            """
            INSERT INTO business_commits(
                commit_id, transaction_id, receipt_kind, project_id,
                generation, repository_revision, project_revision,
                head_revision, repository_epoch, cutover_id,
                source_admission_revision,
                document_id,
                document_version, previous_commit_id, commit_kind,
                save_policy, trace_id, operation_count,
                operation_target_count, last_operation_id,
                last_operation_ordinal, outbox_event_count,
                durable_action_count, pipeline_intent_count, manifest_bytes,
                manifest_sha256, before_root_sha256, after_root_sha256,
                previous_operation_tail_sha256, operation_tail_sha256,
                asset_projection_sha256, asset_reference_count, entity_count,
                committed_at
            ) VALUES (?, ?, ?, 'project-v6', 1, 2, 2, 2, ?, ?, 7,
                      'document-v6', 2, ?,
                      'change', 'auto_commit', 'trace-v6-2', 1, 0, ?, 0, 0,
                      1, 1, ?, ?, ?, ?, ?, ?, ?, 1, 0, ?)
            """,
            (
                second_commit,
                second_transaction,
                schema.BUSINESS_TRANSACTION_KIND,
                facts["repository_epoch"],
                facts["cutover_id"],
                facts["commit_id"],
                "a" * 32,
                b"{\"v\":2}",
                hashlib.sha256(b"{\"v\":2}").digest(),
                facts["project_hash"],
                next_project_hash,
                b"t" * 32,
                b"u" * 32,
                b"w" * 32,
                _NOW,
            ),
        )
        connection.execute(
            "UPDATE repository_projects SET project_revision=2, "
            "current_snapshot_sha256=?, updated_at=? "
            "WHERE project_id='project-v6'",
            (next_project_hash, _NOW),
        )
        connection.execute(
            """
            INSERT INTO business_operations(
                operation_id, previous_operation_id, previous_ordinal,
                previous_hash, self_hash, commit_id, commit_operation_count,
                commit_kind, ordinal, project_id, generation, document_id,
                command_id, source, kind, trace_id, actor_bytes, actor_sha256,
                operation_bytes, operation_sha256, operation_schema, codec_id,
                codec_version, occurred_at
            ) VALUES (?, NULL, NULL, ?, ?, ?, 1, 'change', 0, 'project-v6',
                      1, 'document-v6', 'document.change', 'user', 'native',
                      'trace-v6-2', ?, ?, ?, ?, 'operation/v1', 'json', 1, ?)
            """,
            (
                "a" * 32,
                b"t" * 32,
                b"u" * 32,
                second_commit,
                b"{}",
                hashlib.sha256(b"{}").digest(),
                b"{\"op\":2}",
                hashlib.sha256(b"{\"op\":2}").digest(),
                _NOW,
            ),
        )
        closure_asset = hashlib.sha256(b"closure-asset").digest()
        closure_asset_key = "sha256/%s/%s" % (
            closure_asset.hex()[:2],
            closure_asset.hex(),
        )
        connection.execute(
            """
            INSERT INTO asset_blobs(
                asset_sha256, content_key, media_type, extension, byte_length,
                status, state_revision, created_at, updated_at
            ) VALUES (?, ?, 'image/png', 'png', 13, 'available', 1, ?, ?)
            """,
            (closure_asset, closure_asset_key, _NOW, _NOW),
        )

        def insert_closure_action(
            action_id: str = "b" * 32,
            key: str = "closure-action",
            ordinal: int = 0,
        ) -> None:
            action_bytes = b'{"action":"closure"}'
            connection.execute(
                """
                INSERT INTO deferred_actions(
                    action_id, idempotency_scope, idempotency_key,
                    project_id, generation, document_id, origin_commit_id,
                    commit_action_count, ordinal, action_kind, action_bytes,
                    action_sha256, payload_schema, codec_id, codec_version,
                    replay_policy, idempotency_policy, status, state_revision,
                    attempt_count, max_attempts, next_attempt_at, created_at,
                    updated_at
                ) VALUES (?, 'closure', ?, 'project-v6', 1, 'document-v6',
                          ?, 1, ?, 'notify', ?, ?, 'action/v1', 'json', 1,
                          'provider_idempotent', 'required_external_key',
                          'queued', 1, 0, 3, ?, ?, ?)
                """,
                (
                    action_id,
                    key,
                    second_commit,
                    ordinal,
                    action_bytes,
                    hashlib.sha256(action_bytes).digest(),
                    _NOW,
                    _NOW,
                    _NOW,
                ),
            )

        def insert_closure_pipeline_transition(
            intent_id: str = "c" * 32,
            key: str = "closure-pipeline-create",
            ordinal: int = 0,
            intent_kind: str = "create",
            before_revision: Optional[int] = None,
            after_revision: int = 1,
            before_status: Optional[str] = None,
            after_status: str = "queued",
        ) -> None:
            intent_bytes = b'{"pipeline":"closure"}'
            connection.execute(
                """
                INSERT INTO pipeline_job_transitions(
                    intent_id, origin_commit_id,
                    commit_pipeline_intent_count, ordinal, job_id, project_id,
                    generation, document_id, intent_type, idempotency_key,
                    intent_kind, before_state_revision, after_state_revision,
                    before_status, after_status, intent_bytes, intent_sha256,
                    payload_schema, codec_id, codec_version, replay_policy,
                    max_attempts, committed_at
                ) VALUES (?, ?, 1, ?, ?, 'project-v6', 1, 'document-v6',
                          'closure.intent', ?, ?, ?, ?, ?, ?, ?, ?,
                          'pipeline/v1', 'json', 1, 'exact_cas_retry', 3, ?)
                """,
                (
                    intent_id,
                    second_commit,
                    ordinal,
                    "c" * 32,
                    key,
                    intent_kind,
                    before_revision,
                    after_revision,
                    before_status,
                    after_status,
                    intent_bytes,
                    hashlib.sha256(intent_bytes).digest(),
                    _NOW,
                ),
            )

        def insert_closure_pipeline() -> None:
            job_bytes = b'{"job":"closure"}'
            connection.execute(
                """
                INSERT INTO pipeline_jobs(
                    job_id, last_intent_id, idempotency_key, project_id,
                    generation, document_id, pipeline_id, intent_type,
                    intent_kind, input_commit_id, head_commit_id, job_bytes,
                    job_sha256, payload_schema, codec_id, codec_version,
                    replay_policy, status, state_revision, attempt_count,
                    max_attempts, next_attempt_at, created_at, updated_at
                ) VALUES (?, ?, 'closure-pipeline-create', 'project-v6', 1,
                          'document-v6', 'closure', 'closure.create', 'create',
                          ?, ?, ?, ?, 'pipeline/v1', 'json', 1,
                          'exact_cas_retry', 'queued', 1, 0, 3, ?, ?, ?)
                """,
                (
                    "c" * 32,
                    "c" * 32,
                    second_commit,
                    second_commit,
                    job_bytes,
                    hashlib.sha256(job_bytes).digest(),
                    _NOW,
                    _NOW,
                    _NOW,
                ),
            )
            insert_closure_pipeline_transition()

        def insert_closure_asset_ref(ordinal: int = 0) -> None:
            connection.execute(
                """
                INSERT INTO document_asset_refs(
                    project_id, generation, document_id, document_version,
                    commit_id, commit_asset_reference_count, ordinal,
                    asset_sha256, entity_type, entity_id, entity_deleted,
                    value_path, ref_mime, ref_size, ref_ext
                ) VALUES ('project-v6', 1, 'document-v6', 2, ?, 1, ?, ?,
                          'image', 'closure-cover', 0, '/fields/asset',
                          'image/png', 13, 'png')
                """,
                (second_commit, ordinal, closure_asset),
            )

        head_update_sql = """
            UPDATE document_commit_heads
            SET document_version=2, commit_id=?, head_revision=2,
                current_snapshot_sha256=?, business_root_sha256=?,
                current_project_snapshot_sha256=?, operation_tail_sha256=?,
                asset_projection_sha256=?, asset_reference_count=1,
                updated_at=?
            WHERE project_id='project-v6'
        """
        head_update_values = (
            second_commit,
            next_document_hash,
            next_project_hash,
            next_project_hash,
            b"u" * 32,
            b"w" * 32,
            _NOW,
        )

        connection.execute("SAVEPOINT missing_action")
        insert_closure_pipeline()
        insert_closure_asset_ref()
        with pytest.raises(
            sqlite3.IntegrityError,
            match="business_durable_action_count_mismatch",
        ):
            connection.execute(head_update_sql, head_update_values)
        connection.execute("ROLLBACK TO missing_action")
        connection.execute("RELEASE missing_action")

        connection.execute("SAVEPOINT missing_pipeline")
        insert_closure_action()
        insert_closure_asset_ref()
        with pytest.raises(
            sqlite3.IntegrityError,
            match="business_pipeline_intent_count_mismatch",
        ):
            connection.execute(head_update_sql, head_update_values)
        connection.execute("ROLLBACK TO missing_pipeline")
        connection.execute("RELEASE missing_pipeline")

        connection.execute("SAVEPOINT missing_asset")
        insert_closure_action()
        insert_closure_pipeline()
        with pytest.raises(
            sqlite3.IntegrityError,
            match="business_asset_reference_count_mismatch",
        ):
            connection.execute(head_update_sql, head_update_values)
        connection.execute("ROLLBACK TO missing_asset")
        connection.execute("RELEASE missing_asset")

        insert_closure_action()
        insert_closure_pipeline()
        insert_closure_asset_ref()

        connection.execute("SAVEPOINT target_count_mismatch")
        connection.execute(
            """
            INSERT INTO business_operation_targets(
                operation_id, ordinal, entity_type, entity_id, field, patch_kind
            ) VALUES (?, 0, 'shape', 'shape-1', 'x', 'set')
            """,
            ("a" * 32,),
        )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="business_operation_target_count_mismatch",
        ):
            connection.execute(head_update_sql, head_update_values)
        connection.execute("ROLLBACK TO target_count_mismatch")
        connection.execute("RELEASE target_count_mismatch")
        connection.execute(head_update_sql, head_update_values)
        connection.execute(
            "UPDATE repository_runtime_control SET repository_revision=2, "
            "updated_at=?",
            (_NOW,),
        )
        connection.execute("COMMIT")
        assert connection.execute(
            "SELECT count(*) FROM business_commits"
        ).fetchone()[0] == 2
        with pytest.raises(
            sqlite3.IntegrityError,
            match="business_operation_targets_finalized",
        ):
            connection.execute(
                """
                INSERT INTO business_operation_targets(
                    operation_id, ordinal, entity_type, entity_id,
                    field, patch_kind
                ) VALUES (?, 0, 'shape', 'shape-1', 'x', 'set')
                """,
                ("a" * 32,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint"):
            insert_closure_action("d" * 32, "late-closure-action", 1)
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint"):
            insert_closure_pipeline_transition(
                intent_id="e" * 32,
                key="late-closure-pipeline",
                ordinal=1,
                intent_kind="transition",
                before_revision=1,
                after_revision=2,
                before_status="queued",
                after_status="paused",
            )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint"):
            insert_closure_asset_ref(1)
        with pytest.raises(
            sqlite3.IntegrityError, match="business_history_append_only"
        ):
            connection.execute(
                "UPDATE document_asset_refs SET entity_deleted=1 "
                "WHERE commit_id=?",
                (second_commit,),
            )
        with pytest.raises(
            sqlite3.IntegrityError, match="business_history_append_only"
        ):
            connection.execute(
                "DELETE FROM document_asset_refs WHERE commit_id=?",
                (second_commit,),
            )
        with pytest.raises(
            sqlite3.IntegrityError, match="business_asset_still_referenced"
        ):
            connection.execute(
                "UPDATE asset_blobs SET status='deleting', state_revision=2, "
                "updated_at=? WHERE asset_sha256=?",
                (_NOW, closure_asset),
            )
        declared_asset_closure = connection.execute(
            """
            SELECT head.asset_projection_sha256, head.asset_reference_count,
                   business_commit.asset_projection_sha256,
                   business_commit.asset_reference_count
            FROM document_commit_heads AS head
            JOIN business_commits AS business_commit
              ON business_commit.commit_id = head.commit_id
            WHERE head.project_id='project-v6'
            """
        ).fetchone()
        assert declared_asset_closure[0] == declared_asset_closure[2]
        assert declared_asset_closure[1] == declared_asset_closure[3] == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_empty_v6_reopen_upgrades_to_v7_without_rewriting_frozen_ledger(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "empty-v6-to-v7.sqlite3"
    connection = sqlite3.connect(str(database_path), isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        _apply_migrations(connection, schema.MIGRATIONS[:6])
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
        frozen_ledger = [
            tuple(row)
            for row in connection.execute(
                "SELECT version, name, checksum FROM schema_migrations "
                "ORDER BY version"
            ).fetchall()
        ]
    finally:
        connection.close()

    reopened = sqlite3.connect(str(database_path), isolation_level=None)
    reopened.row_factory = sqlite3.Row
    reopened.execute("PRAGMA foreign_keys=ON")
    try:
        assert reopened.execute("PRAGMA user_version").fetchone()[0] == 6
        assert "project_revision" not in {
            row["name"]
            for row in reopened.execute(
                "PRAGMA table_info(business_commits)"
            ).fetchall()
        }
        _apply_migrations(reopened, schema.MIGRATIONS[6:7])
        assert reopened.execute("PRAGMA user_version").fetchone()[0] == 7
        assert [
            tuple(row)
            for row in reopened.execute(
                "SELECT version, name, checksum FROM schema_migrations "
                "WHERE version <= 6 ORDER BY version"
            ).fetchall()
        ] == frozen_ledger
        assert reopened.execute(
            "SELECT checksum FROM schema_migrations WHERE version=6"
        ).fetchone()[0] == (
            "41fe4ba13a35bacfa75af648381c0dadc4d256a8872c65a1bbf0138b64252254"
        )
        assert {
            "project_revision",
            "head_revision",
        }.issubset(
            {
                row["name"]
                for row in reopened.execute(
                    "PRAGMA table_info(business_commits)"
                ).fetchall()
            }
        )
        head_foreign_keys = reopened.execute(
            "PRAGMA foreign_key_list(document_commit_heads)"
        ).fetchall()
        assert any(
            row["table"] == "business_commits"
            and row["from"] == "head_revision"
            and row["to"] == "head_revision"
            for row in head_foreign_keys
        )
        assert reopened.execute(
            "SELECT count(*) FROM sqlite_schema "
            "WHERE name='schema_v7_local_revision_preflight'"
        ).fetchone()[0] == 0
        assert reopened.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        reopened.close()


def test_ambiguous_multi_project_v6_upgrade_fails_closed_and_rolls_back() -> None:
    connection = _connection()
    repository_epoch = "e" * 64
    cutover_id = "f" * 64
    try:
        _apply_migrations(connection, schema.MIGRATIONS[:6])
        connection.execute("BEGIN")
        connection.execute(
            "UPDATE kernel_repository SET authority='sqlite' WHERE singleton=1"
        )
        connection.execute(
            """
            INSERT INTO repository_runtime_control(
                authority, authority_state, repository_epoch, cutover_id,
                source_admission_revision, repository_revision,
                state_revision, runtime_generation, code_digest,
                schema_version, updated_at
            ) VALUES ('sqlite', 'active', ?, ?, 7, 2, 1,
                      'runtime-ambiguous-v6', ?, 6, ?)
            """,
            (repository_epoch, cutover_id, b"c" * 32, _NOW),
        )
        first = _insert_frozen_v6_stream(
            connection,
            project_id="project-a",
            document_id="document-a",
            commit_id="a" * 32,
            repository_revision=1,
            project_revision=1,
            head_revision=1,
            repository_epoch=repository_epoch,
            cutover_id=cutover_id,
        )
        second = _insert_frozen_v6_stream(
            connection,
            project_id="project-b",
            document_id="document-b",
            commit_id="b" * 32,
            repository_revision=2,
            project_revision=2,
            head_revision=2,
            repository_epoch=repository_epoch,
            cutover_id=cutover_id,
        )
        connection.execute("COMMIT")
        before_rows = [
            tuple(row)
            for row in connection.execute(
                "SELECT commit_id, project_id, repository_revision, "
                "after_root_sha256, manifest_bytes FROM business_commits "
                "ORDER BY repository_revision"
            ).fetchall()
        ]

        with pytest.raises(
            sqlite3.IntegrityError,
            match="schema_v7_local_revision_precondition",
        ):
            _apply_migrations(connection, schema.MIGRATIONS[6:])

        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
        assert connection.execute(
            "SELECT count(*) FROM schema_migrations WHERE version=7"
        ).fetchone()[0] == 0
        assert {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(business_commits)"
            ).fetchall()
        }.isdisjoint({"project_revision", "head_revision"})
        assert [
            tuple(row)
            for row in connection.execute(
                "SELECT commit_id, project_id, repository_revision, "
                "after_root_sha256, manifest_bytes FROM business_commits "
                "ORDER BY repository_revision"
            ).fetchall()
        ] == before_rows
        assert before_rows[0][3:] == (first["project_hash"], first["manifest"])
        assert before_rows[1][3:] == (
            second["project_hash"],
            second["manifest"],
        )
        assert connection.execute(
            "SELECT count(*) FROM document_commit_heads"
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT count(*) FROM sqlite_schema "
            "WHERE type='trigger' AND name='document_commit_heads_no_delete'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT count(*) FROM sqlite_schema "
            "WHERE name='schema_v7_local_revision_preflight'"
        ).fetchone()[0] == 0
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_v6_baseline_upgrade_preserves_evidence_and_allows_aba_interleaving() -> None:
    connection = _connection()
    repository_epoch = "e" * 64
    cutover_id = "f" * 64
    empty_projection = hashlib.sha256(b"asset-projection-empty").digest()

    def insert_v2_rows(
        project_id: str,
        document_id: str,
        project_snapshot: bytes,
        document_snapshot: bytes,
    ) -> tuple[bytes, bytes]:
        project_hash = hashlib.sha256(project_snapshot).digest()
        document_hash = hashlib.sha256(document_snapshot).digest()
        connection.execute(
            """
            INSERT INTO projects(
                project_id, generation, codec_id, codec_version,
                snapshot_format_version, snapshot_bytes, snapshot_sha256
            ) VALUES (?, 1, 'json', 1, 1, ?, ?)
            """,
            (project_id, project_snapshot, project_hash),
        )
        connection.execute(
            """
            INSERT INTO documents(
                project_id, document_id, document_version, schema_version,
                collaboration_mode, save_policy, codec_id, codec_version,
                snapshot_format_version, snapshot_bytes, snapshot_sha256
            ) VALUES (?, ?, 1, '1.0.0', 'with_save', 'auto_commit',
                      'json', 1, 1, ?, ?)
            """,
            (project_id, document_id, document_snapshot, document_hash),
        )
        return project_hash, document_hash

    def insert_commit(
        *,
        commit_id: str,
        project_id: str,
        document_id: str,
        repository_revision: int,
        project_revision: int,
        head_revision: int,
        document_version: int,
        previous_commit_id: Optional[str],
        before_root: bytes,
        after_root: bytes,
        previous_tail: bytes,
        operation_tail: bytes,
        operation_id: Optional[str] = None,
    ) -> None:
        transaction_id = schema.BUSINESS_TRANSACTION_PREFIX + commit_id
        _receipt(connection, transaction_id, schema.BUSINESS_TRANSACTION_KIND)
        manifest = ("manifest-" + commit_id).encode("ascii")
        operation_count = 0 if operation_id is None else 1
        connection.execute(
            """
            INSERT INTO business_commits(
                commit_id, transaction_id, receipt_kind, project_id,
                generation, repository_revision, project_revision,
                head_revision, repository_epoch, cutover_id,
                source_admission_revision, document_id, document_version,
                previous_commit_id, commit_kind, save_policy, trace_id,
                operation_count, operation_target_count, last_operation_id,
                last_operation_ordinal, outbox_event_count,
                durable_action_count, pipeline_intent_count,
                before_root_sha256, after_root_sha256,
                previous_operation_tail_sha256, operation_tail_sha256,
                asset_projection_sha256, asset_reference_count, entity_count,
                manifest_bytes, manifest_sha256, committed_at
            ) VALUES (
                :commit_id, :transaction_id, :receipt_kind, :project_id, 1,
                :repository_revision, :project_revision, :head_revision,
                :repository_epoch, :cutover_id, 7, :document_id,
                :document_version, :previous_commit_id, :commit_kind,
                'auto_commit', :trace_id, :operation_count, 0,
                :last_operation_id, :last_operation_ordinal, 0, 0, 0,
                :before_root, :after_root, :previous_tail, :operation_tail,
                :asset_projection, 0, 0, :manifest,
                :manifest_sha256, :committed_at
            )
            """,
            {
                "commit_id": commit_id,
                "transaction_id": transaction_id,
                "receipt_kind": schema.BUSINESS_TRANSACTION_KIND,
                "project_id": project_id,
                "repository_revision": repository_revision,
                "project_revision": project_revision,
                "head_revision": head_revision,
                "repository_epoch": repository_epoch,
                "cutover_id": cutover_id,
                "document_id": document_id,
                "document_version": document_version,
                "previous_commit_id": previous_commit_id,
                "commit_kind": (
                    "baseline" if previous_commit_id is None else "change"
                ),
                "trace_id": "trace-" + commit_id,
                "operation_count": operation_count,
                "last_operation_id": operation_id,
                "last_operation_ordinal": None if operation_id is None else 0,
                "before_root": before_root,
                "after_root": after_root,
                "previous_tail": previous_tail,
                "operation_tail": operation_tail,
                "asset_projection": empty_projection,
                "manifest": manifest,
                "manifest_sha256": hashlib.sha256(manifest).digest(),
                "committed_at": _NOW,
            },
        )

    def insert_head(
        *,
        project_id: str,
        document_id: str,
        document_version: int,
        commit_id: str,
        head_revision: int,
        project_hash: bytes,
        document_hash: bytes,
        operation_tail: bytes,
    ) -> None:
        connection.execute(
            """
            INSERT INTO document_commit_heads(
                project_id, generation, document_id, document_version,
                commit_id, head_revision, status, current_snapshot_sha256,
                current_project_snapshot_sha256, business_root_sha256,
                operation_tail_sha256, asset_projection_sha256,
                asset_reference_count, entity_count, updated_at
            ) VALUES (?, 1, ?, ?, ?, ?, 'live', ?, ?, ?, ?, ?, 0, 0, ?)
            """,
            (
                project_id,
                document_id,
                document_version,
                commit_id,
                head_revision,
                document_hash,
                project_hash,
                project_hash,
                operation_tail,
                empty_projection,
                _NOW,
            ),
        )

    try:
        _apply_migrations(connection, schema.MIGRATIONS[:6])
        connection.execute("BEGIN")
        frozen = _seed_frozen_v6_baseline(connection)
        connection.execute("COMMIT")
        frozen_v6_receipt = connection.execute(
            "SELECT kind, committed_at FROM transaction_receipts "
            "WHERE transaction_id=?",
            (frozen["transaction_id"],),
        ).fetchone()

        _apply_migrations(connection, schema.MIGRATIONS[6:7])
        a1_project = frozen["project_hash"]
        a1_document = frozen["document_hash"]
        a1_tail = frozen["operation_tail"]
        migrated = connection.execute(
            "SELECT project_revision, head_revision, after_root_sha256, "
            "manifest_bytes FROM business_commits WHERE commit_id=?",
            (frozen["commit_id"],),
        ).fetchone()
        assert tuple(migrated) == (
            1,
            1,
            a1_project,
            frozen["manifest"],
        )
        assert tuple(
            connection.execute(
                "SELECT kind, committed_at FROM transaction_receipts "
                "WHERE transaction_id=?",
                (frozen["transaction_id"],),
            ).fetchone()
        ) == tuple(frozen_v6_receipt)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

        connection.execute("BEGIN")
        b1_project, b1_document = insert_v2_rows(
            "project-b", "document-b", b"project-b-v1", b"document-b-v1"
        )
        b1_tail = hashlib.sha256(b"project-b-tail-v1").digest()
        connection.execute("SAVEPOINT invalid_project_local_revision")
        with pytest.raises(
            sqlite3.IntegrityError,
            match="business_local_revision_invalid",
        ):
            insert_commit(
                commit_id="b" * 32,
                project_id="project-b",
                document_id="document-b",
                repository_revision=2,
                project_revision=2,
                head_revision=1,
                document_version=1,
                previous_commit_id=None,
                before_root=b"0" * 32,
                after_root=b1_project,
                previous_tail=b"0" * 32,
                operation_tail=b1_tail,
            )
        connection.execute("ROLLBACK TO invalid_project_local_revision")
        connection.execute("RELEASE invalid_project_local_revision")
        insert_commit(
            commit_id="b" * 32,
            project_id="project-b",
            document_id="document-b",
            repository_revision=2,
            project_revision=1,
            head_revision=1,
            document_version=1,
            previous_commit_id=None,
            before_root=b"0" * 32,
            after_root=b1_project,
            previous_tail=b"0" * 32,
            operation_tail=b1_tail,
        )
        connection.execute(
            """
            INSERT INTO repository_projects(
                project_id, generation, project_revision, status,
                current_snapshot_sha256, created_at, updated_at
            ) VALUES ('project-b', 1, 1, 'live', ?, ?, ?)
            """,
            (b1_project, _NOW, _NOW),
        )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="business_local_revision_binding_mismatch",
        ):
            insert_head(
                project_id="project-b",
                document_id="document-b",
                document_version=1,
                commit_id="b" * 32,
                head_revision=2,
                project_hash=b1_project,
                document_hash=b1_document,
                operation_tail=b1_tail,
            )
        insert_head(
            project_id="project-b",
            document_id="document-b",
            document_version=1,
            commit_id="b" * 32,
            head_revision=1,
            project_hash=b1_project,
            document_hash=b1_document,
            operation_tail=b1_tail,
        )
        connection.execute(
            "UPDATE repository_runtime_control SET repository_revision=2, "
            "updated_at=?",
            (_NOW,),
        )
        connection.execute("COMMIT")

        a2_project_bytes = b"project-a-v2"
        a2_document_bytes = b"document-a-v2"
        a2_project = hashlib.sha256(a2_project_bytes).digest()
        a2_document = hashlib.sha256(a2_document_bytes).digest()
        a2_tail = hashlib.sha256(b"project-a-tail-v2").digest()
        a2_operation = "d" * 32
        connection.execute("BEGIN")
        connection.execute(
            "UPDATE projects SET snapshot_bytes=?, snapshot_sha256=? "
            "WHERE project_id='project-a'",
            (a2_project_bytes, a2_project),
        )
        connection.execute(
            "UPDATE documents SET document_version=2, snapshot_bytes=?, "
            "snapshot_sha256=? WHERE project_id='project-a'",
            (a2_document_bytes, a2_document),
        )
        insert_commit(
            commit_id="c" * 32,
            project_id="project-a",
            document_id="document-a",
            repository_revision=3,
            project_revision=2,
            head_revision=2,
            document_version=2,
            previous_commit_id="a" * 32,
            before_root=a1_project,
            after_root=a2_project,
            previous_tail=a1_tail,
            operation_tail=a2_tail,
            operation_id=a2_operation,
        )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="business_local_closure_invalid",
        ):
            connection.execute(
                "UPDATE repository_runtime_control "
                "SET repository_revision=3, updated_at=?",
                (_NOW,),
            )
        connection.execute(
            "UPDATE repository_projects SET project_revision=2, "
            "current_snapshot_sha256=?, updated_at=? "
            "WHERE project_id='project-a'",
            (a2_project, _NOW),
        )
        actor = b"{}"
        operation = b'{"kind":"a2"}'
        connection.execute(
            """
            INSERT INTO business_operations(
                operation_id, previous_operation_id, previous_ordinal,
                previous_hash, self_hash, commit_id,
                commit_operation_count, commit_kind, ordinal, project_id,
                generation, document_id, command_id, source, kind, trace_id,
                actor_bytes, actor_sha256, operation_bytes, operation_sha256,
                operation_schema, codec_id, codec_version, occurred_at
            ) VALUES (?, NULL, NULL, ?, ?, ?, 1, 'change', 0, 'project-a', 1,
                      'document-a', 'a2.change', 'user', 'native', 'trace-a2',
                      ?, ?, ?, ?, 'operation/v1', 'json', 1, ?)
            """,
            (
                a2_operation,
                a1_tail,
                a2_tail,
                "c" * 32,
                actor,
                hashlib.sha256(actor).digest(),
                operation,
                hashlib.sha256(operation).digest(),
                _NOW,
            ),
        )
        connection.execute(
            """
            UPDATE document_commit_heads
                SET document_version=2, commit_id=?, head_revision=2,
                    current_snapshot_sha256=?,
                    current_project_snapshot_sha256=?, business_root_sha256=?,
                    operation_tail_sha256=?, asset_projection_sha256=?,
                    updated_at=?
                WHERE project_id='project-a'
                """,
                (
                    "c" * 32,
                    a2_document,
                    a2_project,
                    a2_project,
                    a2_tail,
                    empty_projection,
                    _NOW,
                ),
            )
        connection.execute(
            "UPDATE repository_runtime_control SET repository_revision=3, "
            "updated_at=?",
            (_NOW,),
        )
        connection.execute("COMMIT")

        commit_revisions = [
            tuple(row)
            for row in connection.execute(
                """
                SELECT project_id, repository_revision,
                       project_revision, head_revision
                FROM business_commits
                ORDER BY repository_revision
                """
            ).fetchall()
        ]
        assert commit_revisions == [
            ("project-a", 1, 1, 1),
            ("project-b", 2, 1, 1),
            ("project-a", 3, 2, 2),
        ]
        assert connection.execute(
            "SELECT repository_revision FROM repository_runtime_control"
        ).fetchone()[0] == 3
        assert [
            tuple(row)
            for row in connection.execute(
                "SELECT project_id, project_revision FROM repository_projects "
                "ORDER BY project_id"
            ).fetchall()
        ] == [("project-a", 2), ("project-b", 1)]
        assert [
            tuple(row)
            for row in connection.execute(
                "SELECT project_id, head_revision FROM document_commit_heads "
                "ORDER BY project_id"
            ).fetchall()
        ] == [("project-a", 2), ("project-b", 1)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_v6_history_and_mutable_projection_trigger_responsibilities_are_exact() -> None:
    connection = _connection()
    try:
        _apply_migrations(connection, schema.MIGRATIONS[:7])
        connection.execute("BEGIN")
        facts = _seed_v6_live_projection(connection)
        connection.execute(
            """
            INSERT INTO asset_blobs(
                asset_sha256, content_key, media_type, extension, byte_length, status,
                state_revision, created_at, updated_at
            ) VALUES (?, ?, 'image/png', 'png', 1, 'available', 1, ?, ?)
            """,
            (b"a" * 32, "sha256/61/" + (b"a" * 32).hex(), _NOW, _NOW),
        )
        connection.execute("COMMIT")

        with pytest.raises(
            sqlite3.IntegrityError, match="business_history_append_only"
        ):
            connection.execute(
                "UPDATE business_commits SET committed_at=committed_at"
            )
        with pytest.raises(
            sqlite3.IntegrityError, match="business_state_no_delete"
        ):
            connection.execute("DELETE FROM repository_projects")

        with pytest.raises(
            sqlite3.IntegrityError,
            match="business_local_revision_binding_mismatch",
        ):
            connection.execute(
                "UPDATE repository_projects "
                "SET project_revision=2, updated_at=?",
                (_NOW,),
            )
        with pytest.raises(
            sqlite3.IntegrityError, match="business_state_no_delete"
        ):
            connection.execute("DELETE FROM asset_blobs")
        with pytest.raises(
            sqlite3.IntegrityError, match="business_asset_reference_unavailable"
        ):
            connection.execute(
                """
                INSERT INTO document_asset_refs(
                    project_id, generation, document_id, document_version,
                    commit_id, commit_asset_reference_count, ordinal,
                    asset_sha256, entity_type, entity_id, entity_deleted,
                    value_path, ref_size
                ) VALUES (
                    'project-v6', 1, 'document-v6', 1, ?, 1, 0, ?, 'image',
                    'other', 0, '/fields/asset', 2
                )
                """,
                (facts["commit_id"], b"a" * 32),
            )

        triggers = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='trigger'"
            ).fetchall()
        }
        for table in (
            "business_commits",
            "business_operations",
            "business_operation_targets",
            "deferred_action_attempts",
            "pipeline_job_attempts",
            "pipeline_job_transitions",
            "document_asset_refs",
        ):
            assert "%s_no_update" % table in triggers
            assert "%s_no_delete" % table in triggers
    finally:
        connection.close()


def test_v6_inbox_outbox_confirmation_action_pipeline_and_asset_states_close() -> None:
    connection = _connection()
    try:
        _apply_migrations(connection, schema.MIGRATIONS[:7])
        connection.execute("BEGIN")
        facts = _seed_v6_live_projection(connection)
        confirmation_id = "3" * 32
        connection.execute(
            """
            INSERT INTO confirmation_requests(
                confirmation_id, project_id, generation, document_id,
                actor_id, approval_nonce_hmac, nonce_key_id, request_bytes,
                request_sha256, request_schema, request_codec_id,
                request_codec_version, status, state_revision, expires_at,
                created_at, updated_at
            ) VALUES (?, 'project-v6', 1, 'document-v6', 'actor-v6', ?,
                      'nonce-key-v1', ?, ?, 'confirmation/v1', 'json', 1,
                      'pending', 1, ?, ?, ?)
            """,
            (
                confirmation_id,
                b"h" * 32,
                b"confirm",
                hashlib.sha256(b"confirm").digest(),
                _NOW,
                _NOW,
                _NOW,
            ),
        )
        response = b'{"confirmation":"created"}'
        connection.execute(
            """
            INSERT INTO idempotency_inbox(
                scope, idempotency_key, request_sha256, request_bytes,
                request_schema, request_codec_id, request_codec_version,
                actor_id, claim_owner_id, claim_fence, status,
                state_revision, result_kind, confirmation_id, response_bytes,
                response_sha256, response_schema, response_codec_id,
                response_codec_version, claimed_at, updated_at
            ) VALUES ('api', 'request-confirm', ?, ?, 'request/v1', 'json', 1,
                      'actor-v6', ?, 1, 'committed', 2, 'confirmation', ?, ?,
                      ?, 'response/v1', 'json', 1, ?, ?)
            """,
            (
                b"q" * 32,
                b"request-confirm",
                "a" * 32,
                confirmation_id,
                response,
                hashlib.sha256(response).digest(),
                _NOW,
                _NOW,
            ),
        )
        noop_response = b'{"noop":true}'
        connection.execute(
            """
            INSERT INTO idempotency_inbox(
                scope, idempotency_key, request_sha256, request_bytes,
                request_schema, request_codec_id, request_codec_version,
                actor_id, claim_owner_id, claim_fence, status,
                state_revision, result_kind, response_bytes,
                response_sha256, response_schema, response_codec_id,
                response_codec_version, claimed_at, updated_at
            ) VALUES ('api', 'request-noop', ?, ?, 'request/v1', 'json', 1,
                      'actor-v6', ?, 2, 'committed', 2, 'noop', ?, ?,
                      'response/v1', 'json', 1, ?, ?)
            """,
            (
                b"n" * 32,
                b"request-noop",
                "b" * 32,
                noop_response,
                hashlib.sha256(noop_response).digest(),
                _NOW,
                _NOW,
            ),
        )
        effect_commit = "2" * 32
        effect_transaction = schema.BUSINESS_TRANSACTION_PREFIX + effect_commit
        _receipt(connection, effect_transaction, schema.BUSINESS_TRANSACTION_KIND)
        connection.execute(
            """
            INSERT INTO business_commits(
                commit_id, transaction_id, receipt_kind, project_id,
                generation, repository_revision, project_revision,
                head_revision, repository_epoch, cutover_id,
                source_admission_revision,
                document_id, document_version, previous_commit_id,
                commit_kind, save_policy, trace_id, operation_count,
                operation_target_count, last_operation_id,
                last_operation_ordinal, outbox_event_count,
                last_outbox_event_id, last_outbox_ordinal,
                durable_action_count, pipeline_intent_count,
                before_root_sha256, after_root_sha256,
                previous_operation_tail_sha256, operation_tail_sha256,
                asset_projection_sha256, asset_reference_count, entity_count,
                manifest_bytes, manifest_sha256, committed_at
            ) VALUES (?, ?, ?, 'project-v6', 1, 2, 2, 2, ?, ?, 7,
                      'document-v6', 2,
                      ?, 'change', 'auto_commit', 'trace-effects', 1, 0, ?, 0, 1,
                      ?, 0, 1, 3, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?)
            """,
            (
                effect_commit,
                effect_transaction,
                schema.BUSINESS_TRANSACTION_KIND,
                facts["repository_epoch"],
                facts["cutover_id"],
                facts["commit_id"],
                "9" * 32,
                "4" * 32,
                facts["project_hash"],
                b"z" * 32,
                b"t" * 32,
                b"9" * 32,
                b"v" * 32,
                b"{\"effects\":true}",
                hashlib.sha256(b"{\"effects\":true}").digest(),
                _NOW,
            ),
        )
        connection.execute(
            """
            INSERT INTO business_operations(
                operation_id, previous_operation_id, previous_ordinal,
                previous_hash, self_hash, commit_id, commit_operation_count,
                commit_kind, ordinal, project_id, generation, document_id,
                command_id, source, kind, trace_id, actor_bytes, actor_sha256,
                operation_bytes, operation_sha256, operation_schema, codec_id,
                codec_version, occurred_at
            ) VALUES (?, NULL, NULL, ?, ?, ?, 1, 'change', 0, 'project-v6',
                      1, 'document-v6',
                      'document.seed', 'system', 'native', 'trace-v6', ?, ?,
                      ?, ?, 'operation/v1', 'json', 1, ?)
            """,
            (
                "9" * 32,
                b"t" * 32,
                b"9" * 32,
                effect_commit,
                b"{}",
                hashlib.sha256(b"{}").digest(),
                b"{\"op\":1}",
                hashlib.sha256(b"{\"op\":1}").digest(),
                _NOW,
            ),
        )
        connection.execute(
            """
            INSERT INTO event_outbox(
                event_id, previous_event_id, previous_ordinal, commit_id,
                repository_revision, commit_event_count, operation_id,
                ordinal, topic, partition_key, event_bytes, event_sha256,
                payload_schema, codec_id, codec_version, status,
                state_revision, attempt_count, available_at, created_at,
                updated_at
            ) VALUES (?, NULL, NULL, ?, 2, 1, ?, 0, 'document.changed',
                      'project-v6', ?, ?,
                      'event/v1', 'json', 1, 'pending', 1, 0, ?, ?, ?)
            """,
            (
                "4" * 32,
                effect_commit,
                "9" * 32,
                b"event",
                hashlib.sha256(b"event").digest(),
                _NOW,
                _NOW,
                _NOW,
            ),
        )
        connection.execute(
            """
            INSERT INTO deferred_actions(
                action_id, idempotency_scope, idempotency_key,
                project_id, generation,
                document_id, origin_commit_id, commit_action_count,
                ordinal, action_kind, action_bytes,
                action_sha256, payload_schema, codec_id, codec_version,
                replay_policy, idempotency_policy, status, state_revision,
                attempt_count, max_attempts, next_attempt_at, created_at,
                updated_at
            ) VALUES (?, 'global', 'action-key', 'project-v6', 1,
                      'document-v6', ?, 1, 0, 'notify', ?, ?,
                      'action/v1', 'json', 1,
                      'provider_idempotent', 'required_external_key',
                      'queued', 1, 0, 3, ?, ?, ?)
            """,
            (
                "5" * 32,
                effect_commit,
                b"action",
                hashlib.sha256(b"action").digest(),
                _NOW,
                _NOW,
                _NOW,
            ),
        )
        connection.execute(
            """
            INSERT INTO pipeline_jobs(
                job_id, last_intent_id, idempotency_key, project_id,
                generation, document_id, pipeline_id, intent_type,
                intent_kind, input_commit_id, head_commit_id, job_bytes,
                job_sha256, payload_schema, codec_id, codec_version,
                replay_policy, status, state_revision,
                attempt_count, max_attempts, next_attempt_at, created_at,
                updated_at
            ) VALUES (?, ?, 'job-key', 'project-v6', 1, 'document-v6',
                      'render', 'render.create', 'create', ?, ?, ?, ?,
                      'pipeline/v1', 'json', 1, 'exact_cas_retry', 'queued',
                      1, 0, 3, ?, ?, ?)
            """,
            (
                "6" * 32,
                "6" * 32,
                effect_commit,
                effect_commit,
                b"job",
                hashlib.sha256(b"job").digest(),
                _NOW,
                _NOW,
                _NOW,
            ),
        )
        connection.execute(
            """
            INSERT INTO pipeline_job_transitions(
                intent_id, origin_commit_id, ordinal, job_id, project_id,
                commit_pipeline_intent_count,
                generation, document_id, intent_type, idempotency_key,
                intent_kind,
                after_state_revision, after_status, intent_bytes,
                intent_sha256, payload_schema, codec_id, codec_version,
                replay_policy, max_attempts, committed_at
            ) VALUES (?, ?, 0, ?, 'project-v6', 3, 1, 'document-v6',
                      'render.create', 'job-key', 'create', 1, 'queued', ?, ?,
                      'pipeline/v1', 'json', 1, 'exact_cas_retry', 3, ?)
            """,
            (
                "6" * 32,
                effect_commit,
                "6" * 32,
                b"job",
                hashlib.sha256(b"job").digest(),
                _NOW,
            ),
        )
        empty_digest = hashlib.sha256(b"").digest()
        empty_key = "sha256/%s/%s" % (empty_digest.hex()[:2], empty_digest.hex())
        connection.execute(
            """
            INSERT INTO asset_blobs(
                asset_sha256, content_key, byte_length, status,
                state_revision, created_at, updated_at
            ) VALUES (?, ?, 0, 'staged', 1, ?, ?)
            """,
            (empty_digest, empty_key, _NOW, _NOW),
        )
        connection.execute("COMMIT")

        pipeline_probe_sql = """
            INSERT INTO pipeline_job_transitions(
                intent_id, origin_commit_id, ordinal, job_id, project_id,
                commit_pipeline_intent_count,
                generation, document_id, intent_type, idempotency_key,
                intent_kind, before_state_revision, after_state_revision,
                before_status, after_status, intent_bytes, intent_sha256,
                payload_schema, codec_id, codec_version, replay_policy,
                max_attempts, committed_at
            ) VALUES (?, ?, 0, ?, 'project-v6', 1, 1, 'document-v6',
                      'render.pause', ?, 'transition', 1, 2, 'queued',
                      'paused', ?, ?, 'pipeline/v1', 'json', 1, ?, ?, ?)
        """
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint"):
            connection.execute(
                pipeline_probe_sql,
                (
                    "e" * 32,
                    facts["commit_id"],
                    "6" * 32,
                    "job-key",
                    b"pause",
                    hashlib.sha256(b"pause").digest(),
                    "exact_cas_retry",
                    3,
                    _NOW,
                ),
            )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint"):
            connection.execute(
                pipeline_probe_sql,
                (
                    "f" * 32,
                    facts["commit_id"],
                    "6" * 32,
                    "job-budget-invalid",
                    b"pause",
                    hashlib.sha256(b"pause").digest(),
                    "never_replay",
                    2,
                    _NOW,
                ),
            )

        with pytest.raises(
            sqlite3.IntegrityError, match="business_repair_mode_required"
        ):
            connection.execute(
                "UPDATE asset_blobs SET status='repair_required', "
                "state_revision=2, updated_at=? WHERE asset_sha256=?",
                (_NOW, empty_digest),
            )
        connection.execute(
            "UPDATE repository_runtime_control "
            "SET authority_state='repair_required', state_revision=2, "
            "updated_at=?",
            (_NOW,),
        )
        connection.execute(
            "UPDATE asset_blobs SET status='repair_required', "
            "state_revision=2, updated_at=? WHERE asset_sha256=?",
            (_NOW, empty_digest),
        )
        with pytest.raises(
            sqlite3.IntegrityError, match="business_repair_rows_remain"
        ):
            connection.execute(
                "UPDATE repository_runtime_control "
                "SET authority_state='active', state_revision=3, updated_at=?",
                (_NOW,),
            )
        connection.execute(
            "UPDATE asset_blobs SET status='quarantined', "
            "state_revision=3, updated_at=? WHERE asset_sha256=?",
            (_NOW, empty_digest),
        )
        connection.execute(
            "UPDATE repository_runtime_control "
            "SET authority_state='active', state_revision=3, updated_at=?",
            (_NOW,),
        )

        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(confirmation_requests)"
            ).fetchall()
        }
        assert "approval_nonce_hmac" in columns
        assert "approval_nonce" not in columns
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE confirmation_requests SET status='reserved', "
                "state_revision=2"
            )
        connection.execute(
            """
            UPDATE confirmation_requests
            SET status='reserved', state_revision=2,
                reservation_owner_id=?, reservation_fence=1,
                reservation_expires_at=?, updated_at=?
            """,
            ("c" * 32, _NOW, _NOW),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE event_outbox SET status='leased', state_revision=2"
            )
        with pytest.raises(
            sqlite3.IntegrityError, match="business_status_transition_invalid"
        ):
            connection.execute(
                "UPDATE event_outbox SET status='published', published_at=?, "
                "state_revision=2, updated_at=?",
                (_NOW, _NOW),
            )
        connection.execute(
            """
            UPDATE event_outbox
            SET status='leased', state_revision=2, lease_owner='worker-a',
                lease_fence=1, lease_expires_at=?, updated_at=?
            """,
            (_NOW, _NOW),
        )
        with pytest.raises(
            sqlite3.IntegrityError, match="business_revision_invalid"
        ):
            connection.execute("UPDATE event_outbox SET state_revision=1")
        changed_response = b'{"confirmation":"changed"}'
        with pytest.raises(
            sqlite3.IntegrityError, match="business_terminal_result_immutable"
        ):
            connection.execute(
                """
                UPDATE idempotency_inbox
                SET response_bytes=?, response_sha256=?, state_revision=3
                WHERE idempotency_key='request-confirm'
                """,
                (changed_response, hashlib.sha256(changed_response).digest()),
            )
        connection.execute(
            """
            UPDATE deferred_actions
            SET status='running', state_revision=2, lease_owner='worker-a',
                lease_fence=1, lease_expires_at=?, current_attempt_id=?,
                current_attempt_started_at=?, attempt_count=1, updated_at=?
            """,
            (_NOW, "7" * 32, _NOW, _NOW),
        )
        action_attempt_sql = """
            INSERT INTO deferred_action_attempts(
                action_id, attempt_number, attempt_id, outcome,
                execution_fence, idempotency_scope, idempotency_key,
                replay_policy, idempotency_policy, result_bytes,
                result_sha256, started_at, finished_at
            ) VALUES (?, 1, ?, 'outcome_unknown', 1, 'global', 'action-key',
                      'provider_idempotent', 'required_external_key', ?, ?, ?, ?)
        """
        action_attempt_values = (
            "5" * 32,
            "7" * 32,
            b"unknown",
            hashlib.sha256(b"unknown").digest(),
            _NOW,
            _NOW,
        )
        with pytest.raises(
            sqlite3.IntegrityError, match="business_attempt_pointer_invalid"
        ):
            connection.execute(action_attempt_sql, action_attempt_values)
        connection.execute("BEGIN")
        connection.execute(
            """
            UPDATE deferred_actions
            SET status='outcome_unknown', state_revision=3,
                lease_owner=NULL, lease_fence=NULL, lease_expires_at=NULL,
                current_attempt_id=NULL, current_attempt_started_at=NULL,
                last_attempt_id=?, last_attempt_number=1, updated_at=?
            """,
            ("7" * 32, _NOW),
        )
        connection.execute(
            action_attempt_sql,
            (
                *action_attempt_values,
            ),
        )
        connection.execute("COMMIT")
        with pytest.raises(
            sqlite3.IntegrityError, match="business_revision_invalid"
        ):
            connection.execute(
                "UPDATE deferred_actions SET attempt_count=0, state_revision=4"
            )
        connection.execute(
            """
            INSERT INTO pipeline_job_transitions(
                intent_id, origin_commit_id, ordinal, job_id, project_id,
                commit_pipeline_intent_count,
                generation, document_id, intent_type, idempotency_key,
                intent_kind,
                before_state_revision, after_state_revision, before_status,
                after_status, intent_bytes, intent_sha256, payload_schema,
                codec_id, codec_version, replay_policy, max_attempts,
                committed_at
            ) VALUES (?, ?, 1, ?, 'project-v6', 3, 1, 'document-v6',
                      'render.start', 'job-start-key', 'transition',
                      1, 2, 'queued', 'running',
                      ?, ?, 'pipeline/v1', 'json', 1, 'exact_cas_retry', 3, ?)
            """,
            (
                "8" * 32,
                effect_commit,
                "6" * 32,
                b"start",
                hashlib.sha256(b"start").digest(),
                _NOW,
            ),
        )
        connection.execute(
            """
            UPDATE pipeline_jobs
            SET last_intent_id=?, head_commit_id=?, status='running',
                state_revision=2, lease_owner='worker-a', lease_fence=1,
                lease_expires_at=?, current_attempt_id=?,
                current_attempt_started_at=?, attempt_count=1, updated_at=?
            """,
            ("8" * 32, effect_commit, _NOW, "8" * 32, _NOW, _NOW),
        )
        with pytest.raises(
            sqlite3.IntegrityError, match="business_identity_immutable"
        ):
            connection.execute(
                "UPDATE pipeline_jobs SET job_bytes=x'00', "
                "state_revision=state_revision+1"
            )
        pipeline_attempt_sql = """
            INSERT INTO pipeline_job_attempts(
                job_id, attempt_number, attempt_id, outcome,
                execution_fence, replay_policy, started_at, finished_at
            ) VALUES (?, 1, ?, 'failed', 1, 'exact_cas_retry', ?, ?)
        """
        pipeline_attempt_values = ("6" * 32, "8" * 32, _NOW, _NOW)
        with pytest.raises(
            sqlite3.IntegrityError, match="business_attempt_pointer_invalid"
        ):
            connection.execute(pipeline_attempt_sql, pipeline_attempt_values)
        connection.execute("BEGIN")
        connection.execute(
            """
            INSERT INTO pipeline_job_transitions(
                intent_id, origin_commit_id, ordinal, job_id, project_id,
                commit_pipeline_intent_count,
                generation, document_id, intent_type, idempotency_key,
                intent_kind,
                before_state_revision, after_state_revision, before_status,
                after_status, intent_bytes, intent_sha256, payload_schema,
                codec_id, codec_version, replay_policy, max_attempts,
                committed_at
            ) VALUES (?, ?, 2, ?, 'project-v6', 3, 1, 'document-v6',
                      'render.fail', 'job-fail-key', 'transition',
                      2, 3, 'running', 'failed',
                      ?, ?, 'pipeline/v1', 'json', 1, 'exact_cas_retry', 3, ?)
            """,
            (
                "d" * 32,
                effect_commit,
                "6" * 32,
                b"failed",
                hashlib.sha256(b"failed").digest(),
                _NOW,
            ),
        )
        connection.execute(
            """
            UPDATE pipeline_jobs
            SET last_intent_id=?, head_commit_id=?, status='failed',
                state_revision=3, lease_owner=NULL, lease_fence=NULL,
                lease_expires_at=NULL, current_attempt_id=NULL,
                current_attempt_started_at=NULL, last_attempt_id=?,
                last_attempt_number=1, updated_at=?
            """,
            ("d" * 32, effect_commit, "8" * 32, _NOW),
        )
        connection.execute(pipeline_attempt_sql, pipeline_attempt_values)
        connection.execute("COMMIT")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE repository_projects SET project_revision=2, updated_at=?",
                ("x" * 24,),
            )
        for statement in (
            "UPDATE idempotency_inbox SET request_bytes=x'00', "
            "state_revision=state_revision+1",
            "UPDATE event_outbox SET event_bytes=x'00', "
            "state_revision=state_revision+1",
            "UPDATE confirmation_requests SET request_bytes=x'00', "
            "state_revision=state_revision+1",
            "UPDATE deferred_actions SET action_bytes=x'00', "
            "state_revision=state_revision+1",
            "UPDATE asset_blobs SET content_key='sha256/00/escape', "
            "state_revision=state_revision+1",
        ):
            with pytest.raises(
                sqlite3.IntegrityError, match="business_identity_immutable"
            ):
                connection.execute(statement)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO asset_blobs(
                    asset_sha256, content_key, byte_length, status,
                    state_revision, created_at, updated_at
                ) VALUES (?, '../escape', 0, 'staged', 1, ?, ?)
                """,
                (b"z" * 32, _NOW, _NOW),
            )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


@pytest.mark.parametrize("state", ("empty", "catalog-empty", "catalog-live"))
def test_v5_to_v6_upgrade_preserves_legacy_evidence_and_starts_unmounted(
    state: str,
) -> None:
    connection = _connection()
    try:
        _apply_migrations(connection, schema.MIGRATIONS[:5])
        if state == "catalog-live":
            descriptor = _insert_import_batch_and_descriptor(connection)
        if state in ("catalog-empty", "catalog-live"):
            connection.execute("BEGIN")
            catalog = _insert_catalog_seal(
                connection,
                import_id=None if state == "catalog-empty" else descriptor["import_id"],
                project_count=0 if state == "catalog-empty" else 1,
                source_head_bytes=0
                if state == "catalog-empty"
                else descriptor["source_head_bytes"],
                row_snapshot_bytes=0
                if state == "catalog-empty"
                else descriptor["row_snapshot_bytes"],
            )
            _receipt(connection, catalog["transaction_id"], catalog["receipt_kind"])
            if state == "catalog-live":
                _insert_catalog_member(connection, descriptor)
            connection.execute("COMMIT")
        before = {
            table: connection.execute("SELECT count(*) FROM %s" % table).fetchone()[0]
            for table in schema.LEDGER_TABLES
        }

        _apply_migrations(connection, schema.MIGRATIONS[5:6])
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
        assert before == {
            table: connection.execute("SELECT count(*) FROM %s" % table).fetchone()[0]
            for table in schema.LEDGER_TABLES
        }
        assert all(
            connection.execute("SELECT count(*) FROM %s" % table).fetchone()[0]
            == 0
            for table in schema.BUSINESS_TRANSACTION_TABLES
        )
        assert connection.execute(
            "SELECT authority FROM kernel_repository"
        ).fetchone()[0] == "legacy"
    finally:
        connection.close()


def test_v6_ddl_failure_rolls_back_indexes_ledger_and_version() -> None:
    connection = _connection()
    try:
        _apply_migrations(connection, schema.MIGRATIONS[:5])
        connection.execute("CREATE TABLE repository_runtime_control(drift TEXT)")
        with pytest.raises(sqlite3.OperationalError):
            _apply_migrations(connection, schema.MIGRATIONS[5:6])
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
        assert connection.execute(
            "SELECT count(*) FROM schema_migrations WHERE version=6"
        ).fetchone()[0] == 0
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE name IN "
                "('projects_transaction_identity', "
                "'documents_transaction_identity')"
            ).fetchall()
        }
        assert indexes == set()
    finally:
        connection.close()
