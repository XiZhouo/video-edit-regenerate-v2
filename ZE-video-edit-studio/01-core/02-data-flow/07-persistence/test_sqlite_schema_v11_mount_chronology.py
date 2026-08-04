"""Executable evidence for schema-v11 mount chronology/state closure."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import sqlite_kernel as kernel
import sqlite_schema as schema
from test_sqlite_schema import _apply_migrations


_NOW = "2026-07-12T00:00:00.000Z"
_INSTALL = "2026-07-12T00:00:05.000Z"
_MOUNT = "2026-07-12T00:00:10.000Z"
_FROZEN_V1_V10_CHECKSUMS = (
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
)


def _connection() -> sqlite3.Connection:
    value = sqlite3.connect(":memory:", isolation_level=None)
    value.row_factory = sqlite3.Row
    value.execute("PRAGMA foreign_keys=ON")
    return value


def _related_objects(connection: sqlite3.Connection) -> set[tuple[str, str, str]]:
    return {
        (row["type"], row["name"], row["tbl_name"])
        for row in connection.execute(
            "SELECT type,name,tbl_name FROM sqlite_schema WHERE sql IS NOT NULL "
            "AND (lower(sql) LIKE '%business_cutover_mount_receipts%' "
            "OR lower(sql) LIKE '%business_cutover_head%')"
        ).fetchall()
    }


def _foreign_keys(
    connection: sqlite3.Connection,
    table: str,
) -> set[tuple[str, str, str, str, str]]:
    return {
        (
            row["table"],
            row["from"],
            row["to"],
            row["on_update"],
            row["on_delete"],
        )
        for row in connection.execute(
            'PRAGMA foreign_key_list("' + table + '")'
        ).fetchall()
    }


def test_v11_registry_freezes_v1_v10_and_rebuilds_the_complete_object_set() -> None:
    assert tuple(item.checksum for item in schema.MIGRATIONS[:10]) == (
        _FROZEN_V1_V10_CHECKSUMS
    )
    assert schema.MIGRATIONS[10] is schema.MOUNT_CHRONOLOGY_MIGRATION
    assert schema.MOUNT_CHRONOLOGY_MIGRATION.name == (
        "mount_chronology_state_closure_v1"
    )
    assert tuple(item.version for item in schema.MIGRATIONS[:11]) == tuple(
        range(1, 12)
    )

    connection = _connection()
    try:
        _apply_migrations(connection, schema.MIGRATIONS[:10])
        before_objects = _related_objects(connection)
        before_mount_fks = _foreign_keys(
            connection, "business_cutover_mount_receipts"
        )
        before_head_fks = _foreign_keys(connection, "business_cutover_head")

        _apply_migrations(connection, schema.MIGRATIONS[10:11])

        assert connection.execute("PRAGMA user_version").fetchone()[0] == 11
        assert _related_objects(connection) == before_objects
        assert _foreign_keys(
            connection, "business_cutover_mount_receipts"
        ) == before_mount_fks
        assert _foreign_keys(connection, "business_cutover_head") == before_head_fks
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute(
            "SELECT count(*) FROM sqlite_schema "
            "WHERE name='schema_v11_mount_chronology_preflight'"
        ).fetchone()[0] == 0

        mount_sql = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type='table' "
            "AND name='business_cutover_mount_receipts'"
        ).fetchone()[0]
        head_sql = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type='table' "
            "AND name='business_cutover_head'"
        ).fetchone()[0]
        assert "coverage_committed_at <= install_committed_at" in mount_sql
        assert "install_committed_at <= committed_at" in mount_sql
        assert "CHECK(state_revision = phase_revision)" in head_sql
    finally:
        connection.close()


def test_fresh_v11_checks_reject_reversed_chronology_and_split_revision() -> None:
    connection = _connection()
    try:
        _apply_migrations(connection, schema.MIGRATIONS[:11])
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("DROP TRIGGER business_cutover_mount_binding_guard")
        cutover_id = "a" * 64
        values = {
            "cutover_id": cutover_id,
            "transaction_id": "_business-cutover-mount:" + cutover_id,
            "receipt_kind": "_business_cutover_mount_v2",
            "committed_at": _MOUNT,
            "install_transaction_id": "_business-cutover-install:" + cutover_id,
            "install_receipt_kind": "_business_cutover_install_v1",
            "install_committed_at": _NOW,
            "install_manifest_sha256": b"i" * 32,
            "coverage_transaction_id": (
                "_business-cutover-asset-coverage:" + cutover_id
            ),
            "coverage_receipt_kind": "_business_cutover_asset_coverage_v1",
            "coverage_committed_at": _INSTALL,
            "coverage_manifest_sha256": b"c" * 32,
            "audit_transaction_id": "_repository-audit:" + "b" * 32,
            "audit_manifest_sha256": b"a" * 32,
            "activation_transaction_id": (
                "_business-cutover-activation:" + cutover_id
            ),
            "activation_manifest_sha256": b"v" * 32,
            "repository_epoch": "e" * 64,
            "runtime_generation": "runtime-v11-test",
            "code_digest": b"d" * 32,
            "manifest_bytes": b"{}",
            "manifest_sha256": b"m" * 32,
        }
        columns = tuple(values)
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            connection.execute(
                "INSERT INTO business_cutover_mount_receipts(" +
                ",".join(columns) + ") VALUES (" +
                ",".join("?" for _column in columns) + ")",
                tuple(values[column] for column in columns),
            )

        connection.execute(
            "DROP TRIGGER business_cutover_head_insert_binding_guard"
        )
        connection.execute("DROP TRIGGER business_cutover_head_initial_guard")
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            connection.execute(
                """
                INSERT INTO business_cutover_head(
                    singleton,cutover_id,state_revision,phase_revision,phase,
                    authority_state,repository_epoch,runtime_generation,
                    code_digest,install_transaction_id,install_manifest_sha256,
                    coverage_transaction_id,coverage_manifest_sha256,
                    previous_head_sha256,head_sha256,updated_at
                ) VALUES (
                    1,?,1,2,'assets_covered','read_only',?,?,?, ?,?,?,?,
                    zeroblob(32),?,?
                )
                """,
                (
                    cutover_id,
                    "e" * 64,
                    "runtime-v11-test",
                    b"d" * 32,
                    "_business-cutover-install:" + cutover_id,
                    b"i" * 32,
                    "_business-cutover-asset-coverage:" + cutover_id,
                    b"c" * 32,
                    b"h" * 32,
                    _MOUNT,
                ),
            )
    finally:
        connection.close()


def test_authentic_v10_upgrades_to_v11_with_real_kernel_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    full_registry = kernel.MIGRATIONS
    monkeypatch.setattr(kernel, "MIGRATIONS", full_registry[:10])
    database = kernel.SqliteDatabase(tmp_path / "authentic-v10.sqlite3")
    assert database.initialize().user_version == 10

    monkeypatch.setattr(kernel, "MIGRATIONS", full_registry[:11])
    upgraded = database.initialize()
    assert upgraded.user_version == 11
    with database._owned_connection_for_test() as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert tuple(connection.execute(
            "SELECT version,name,checksum FROM schema_migrations "
            "WHERE version=11"
        ).fetchone()) == (
            11,
            schema.MOUNT_CHRONOLOGY_MIGRATION.name,
            schema.MOUNT_CHRONOLOGY_MIGRATION.checksum,
        )


@pytest.mark.parametrize("dirty", ("production_row", "nonlegacy_authority"))
def test_dirty_v10_rejects_v11_atomically(dirty: str) -> None:
    connection = _connection()
    try:
        _apply_migrations(connection, schema.MIGRATIONS[:10])
        before_ledger = tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT version,name,checksum,applied_at FROM schema_migrations "
                "ORDER BY version"
            ).fetchall()
        )
        before_schema = tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_schema "
                "ORDER BY type,name"
            ).fetchall()
        )
        if dirty == "production_row":
            connection.execute(
                "INSERT INTO worker_ledger_head(singleton,state_revision,sequence,"
                "previous_tail_sha256,tail_sha256,updated_at) "
                "VALUES (1,1,0,zeroblob(32),zeroblob(32),?)",
                (_NOW,),
            )
        else:
            connection.execute(
                "UPDATE kernel_repository SET authority='sqlite' WHERE singleton=1"
            )

        with pytest.raises(
            sqlite3.IntegrityError,
            match="schema_v11_mount_chronology_precondition",
        ):
            _apply_migrations(connection, schema.MIGRATIONS[10:11])

        assert connection.execute("PRAGMA user_version").fetchone()[0] == 10
        assert tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT version,name,checksum,applied_at FROM schema_migrations "
                "ORDER BY version"
            ).fetchall()
        ) == before_ledger
        assert tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_schema "
                "ORDER BY type,name"
            ).fetchall()
        ) == before_schema
        assert connection.execute(
            "SELECT count(*) FROM sqlite_schema "
            "WHERE name='schema_v11_mount_chronology_preflight'"
        ).fetchone()[0] == 0
    finally:
        connection.close()


def test_old_v10_registry_fails_closed_on_v11_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    full_registry = kernel.MIGRATIONS
    monkeypatch.setattr(kernel, "MIGRATIONS", full_registry[:11])
    database = kernel.SqliteDatabase(tmp_path / "current-v11.sqlite3")
    assert database.initialize().user_version == 11
    monkeypatch.setattr(kernel, "MIGRATIONS", full_registry[:10])

    with pytest.raises(kernel.SQLiteKernelError) as initialize_error:
        database.initialize()
    assert initialize_error.value.code == "sqlite_schema_version_unsupported"
    with pytest.raises(kernel.SQLiteKernelError) as inspect_error:
        database.inspect_profile()
    assert inspect_error.value.code == "sqlite_schema_version_unsupported"
