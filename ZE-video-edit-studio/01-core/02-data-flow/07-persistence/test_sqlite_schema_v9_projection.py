"""Database evidence for the additive schema-v9 projection sidecars."""

from __future__ import annotations

import sqlite3
from typing import Iterable

import pytest

import sqlite_kernel as kernel
import sqlite_schema as schema


_NOW = "2026-07-12T00:00:00.000Z"
_V8_CHECKSUM = "f6c8a8e1f2aa633a52da52a04e6b58ad11230f5093e4b883e7b63674bb8ad80b"
_V8_FINGERPRINT = "0d96ffa4e88ac3c3d3fd1ba1b6f09f11a3a2d7a0e1861f5781bf0856d7fce771"
_V8_LEDGER_ROOT = "9595ea544b5b055f71d2c93619e49b3353a5d48424951fc410b0ca2484c1834a"
_V9_CHECKSUM = "23018e6b59b8d3ad235e3110abd122ba12584cee272cff0614730f4d4be0dba0"
_V9_FINGERPRINT = "ccb5b4d248c2c429c0a99a5811816521cf4563de3bee457db826b4e4d4090ddf"
_V9_LEDGER_ROOT = "32ade765a739bca3427b4901fa4eee6a737d9d4a0519c369edd2deea56d7b26a"


def _connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
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
                "INSERT INTO schema_migrations(version,name,checksum,applied_at) "
                "VALUES (?,?,?,?)",
                (migration.version, migration.name, migration.checksum, _NOW),
            )
            if migration.version == 1:
                connection.execute(
                    "INSERT INTO kernel_repository(singleton,authority,created_at) "
                    "VALUES (1,'legacy',?)",
                    (_NOW,),
                )
            connection.execute("PRAGMA user_version=%d" % migration.version)
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise


def _drop_triggers(connection: sqlite3.Connection, table: str) -> None:
    names = tuple(
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='trigger' AND tbl_name=?",
            (table,),
        ).fetchall()
    )
    for name in names:
        connection.execute('DROP TRIGGER "' + name + '"')


def test_v9_identity_registry_and_fresh_shape_are_exact() -> None:
    v9_registry = schema.MIGRATIONS[:9]
    assert schema.MIGRATIONS[7] is schema.PRODUCTION_AUTHORITY_WORKER_MIGRATION
    assert schema.MIGRATIONS[8] is schema.WORKER_PAYLOAD_PROJECTION_MIGRATION
    assert tuple(item.version for item in v9_registry) == tuple(range(1, 10))
    assert schema.MIGRATIONS[7].checksum == _V8_CHECKSUM
    assert schema.MIGRATIONS[8].checksum == _V9_CHECKSUM
    assert kernel._expected_schema_fingerprint(8) == _V8_FINGERPRINT
    assert kernel._expected_schema_fingerprint(9) == _V9_FINGERPRINT
    assert schema._expected_migration_ledger_sha256(8).hex() == _V8_LEDGER_ROOT
    assert schema._expected_migration_ledger_sha256(9).hex() == _V9_LEDGER_ROOT
    assert len(schema.PRODUCTION_RUNTIME_V8_TABLES) == 25
    assert schema.WORKER_PROJECTION_SIDECAR_TABLES == (
        "worker_effect_request_projections",
        "worker_result_payload_projections",
        "worker_proof_payload_projections",
    )
    v9_runtime_tables = (
        *schema.PRODUCTION_RUNTIME_V8_TABLES,
        *schema.WORKER_PROJECTION_SIDECAR_TABLES,
    )
    assert (
        schema.PRODUCTION_RUNTIME_TABLES[: len(v9_runtime_tables)] == v9_runtime_tables
    )
    assert len(v9_runtime_tables) == 28

    connection = _connection()
    try:
        _apply_migrations(connection, v9_registry)
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 9
        tables = {
            row["name"]: row["strict"]
            for row in connection.execute("PRAGMA table_list").fetchall()
        }
        assert all(
            tables[name] == 1 for name in schema.WORKER_PROJECTION_SIDECAR_TABLES
        )
        assert all(
            connection.execute('SELECT count(*) FROM "' + name + '"').fetchone()[0] == 0
            for name in schema.WORKER_PROJECTION_SIDECAR_TABLES
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert (
            connection.execute(
                "SELECT count(*) FROM sqlite_schema "
                "WHERE name='schema_v9_worker_projection_preflight'"
            ).fetchone()[0]
            == 0
        )
        trigger_names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='trigger'"
            ).fetchall()
        }
        assert {
            "worker_effects_v9_preadvance_guard",
            "worker_results_v9_operation_preadvance_guard",
            "worker_reconcile_proofs_v9_preadvance_guard",
            "worker_effect_request_projection_preadvance_guard",
            "worker_result_payload_projection_preadvance_guard",
            "worker_proof_payload_projection_preadvance_guard",
            "worker_result_payload_projections_v8_mirror_guard",
            "worker_proof_payload_projections_binding_guard",
            "worker_ledger_head_v9_projection_closure_guard",
        }.issubset(trigger_names)
    finally:
        connection.close()


def test_authentic_pristine_v8_upgrades_without_rewriting_its_ledger() -> None:
    connection = _connection()
    try:
        _apply_migrations(connection, schema.MIGRATIONS[:8])
        before = tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT version,name,checksum,applied_at FROM schema_migrations "
                "ORDER BY version"
            ).fetchall()
        )
        _apply_migrations(connection, schema.MIGRATIONS[8:9])
        after = tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT version,name,checksum,applied_at FROM schema_migrations "
                "ORDER BY version"
            ).fetchall()
        )
        assert after[:8] == before
        assert after[8][:3] == (
            9,
            "worker_payload_projection_sidecars_v1",
            _V9_CHECKSUM,
        )
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 9
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_v9_preflight_failure_rolls_back_every_v9_object() -> None:
    connection = _connection()
    try:
        _apply_migrations(connection, schema.MIGRATIONS[:8])
        connection.execute(
            "INSERT INTO transaction_receipts(transaction_id,kind,committed_at) "
            "VALUES ('_worker-execution:orphan','_worker_execution_v3',?)",
            (_NOW,),
        )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="schema_v9_worker_projection_precondition",
        ):
            _apply_migrations(
                connection,
                (schema.WORKER_PAYLOAD_PROJECTION_MIGRATION,),
            )
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 8
        assert (
            connection.execute(
                "SELECT count(*) FROM schema_migrations WHERE version=9"
            ).fetchone()[0]
            == 0
        )
        names = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_schema").fetchall()
        }
        assert "schema_v9_worker_projection_preflight" not in names
        assert not set(schema.WORKER_PROJECTION_SIDECAR_TABLES).intersection(names)
    finally:
        connection.close()


def test_nullable_payload_five_tuples_reject_partial_null_shapes() -> None:
    connection = _connection()
    try:
        _apply_migrations(connection, schema.MIGRATIONS)
        connection.execute("PRAGMA foreign_keys=OFF")
        _drop_triggers(connection, "worker_result_payload_projections")
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            connection.execute(
                """
                INSERT INTO worker_result_payload_projections(
                    result_id,target_id,target_kind,attempt_id,attempt_number,
                    effect_id,receipt_transaction_id,operation,protocol,
                    projection_schema,after_status,committed_at,
                    result_schema,projection_manifest_bytes,result_sha256,
                    proof_sha256,closure_manifest_bytes,
                    closure_manifest_sha256
                ) VALUES (?,?, 'action',?,1,NULL,?,'complete',
                    'ze.worker-result-projection.v1',
                    'ze.worker-result-payload-projection.v1','succeeded',?,
                    'partial.v1',?,?,NULL,?,?)
                """,
                (
                    "1" * 32,
                    "2" * 32,
                    "3" * 32,
                    "_worker-execution:" + "4" * 32,
                    _NOW,
                    b"{}",
                    b"5" * 32,
                    b"{}",
                    b"6" * 32,
                ),
            )

        _drop_triggers(connection, "worker_proof_payload_projections")
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            connection.execute(
                """
                INSERT INTO worker_proof_payload_projections(
                    proof_id,result_id,target_id,target_kind,attempt_id,
                    attempt_number,effect_id,receipt_transaction_id,operation,
                    protocol,projection_schema,proof_class,proof_kind,
                    observed_at,provider_receipt_schema,result_commit_id,
                    projection_manifest_bytes,proof_sha256,
                    closure_manifest_bytes,closure_manifest_sha256
                ) VALUES (?,?,?,'pipeline',?,1,NULL,?,'complete',
                    'ze.worker-result-projection.v1',
                    'ze.worker-proof-payload-projection.v1','complete',
                    'result_commit',?,'partial-provider.v1',?,?,?, ?,?)
                """,
                (
                    "7" * 32,
                    "8" * 32,
                    "9" * 32,
                    "a" * 32,
                    "_worker-execution:" + "b" * 32,
                    _NOW,
                    "c" * 32,
                    b"{}",
                    b"d" * 32,
                    b"{}",
                    b"e" * 32,
                ),
            )
    finally:
        connection.close()


def test_projection_rows_are_append_only_even_if_binding_guards_are_isolated() -> None:
    connection = _connection()
    try:
        _apply_migrations(connection, schema.MIGRATIONS)
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            "DROP TRIGGER worker_effect_request_projections_binding_guard"
        )
        connection.execute(
            "DROP TRIGGER worker_effect_request_projection_preadvance_guard"
        )
        connection.execute(
            """
            INSERT INTO worker_effect_request_projections(
                effect_id,target_id,target_kind,attempt_id,attempt_number,
                effect_kind,authorization_transaction_id,protocol,
                projection_schema,idempotency_scope,idempotency_key,
                request_schema,request_codec_id,request_codec_version,
                request_payload_bytes,request_payload_sha256,
                projection_manifest_bytes,projection_sha256,
                closure_manifest_bytes,closure_manifest_sha256,authorized_at
            ) VALUES (?,?, 'action',?,1,'action_provider',?,
                'ze.worker-result-projection.v1',
                'ze.worker-effect-request-projection.v1','action/p','key',
                'request.v1','ze.deterministic-json',1,?,?,?,?,?,?,?)
            """,
            (
                "1" * 32,
                "2" * 32,
                "3" * 32,
                "_worker-execution:" + "4" * 32,
                b"{}",
                b"5" * 32,
                b"{}",
                b"6" * 32,
                b"{}",
                b"7" * 32,
                _NOW,
            ),
        )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="production_history_append_only",
        ):
            connection.execute(
                "UPDATE worker_effect_request_projections "
                "SET idempotency_key='changed'"
            )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="production_history_append_only",
        ):
            connection.execute("DELETE FROM worker_effect_request_projections")
    finally:
        connection.close()
