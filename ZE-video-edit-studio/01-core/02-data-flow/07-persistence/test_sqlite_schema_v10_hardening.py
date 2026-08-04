"""Executable evidence for additive schema-v10 integrity hardening."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Iterable

import pytest

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import sqlite_kernel as kernel
import sqlite_schema as schema


_NOW = "2026-07-12T00:00:00.000Z"
_INVALID_24 = "x" * 24
_V8_CHECKSUM = (
    "f6c8a8e1f2aa633a52da52a04e6b58ad11230f5093e4b883e7b63674bb8ad80b"
)
_V9_CHECKSUM = (
    "23018e6b59b8d3ad235e3110abd122ba12584cee272cff0614730f4d4be0dba0"
)


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


def _v10_trigger_names(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_schema "
            "WHERE type='trigger' AND name LIKE '%_v10_%'"
        ).fetchall()
    }


def test_v10_is_additive_and_installs_the_complete_guard_set() -> None:
    v10_registry = schema.MIGRATIONS[:10]
    assert schema.MIGRATIONS[7].checksum == _V8_CHECKSUM
    assert schema.MIGRATIONS[8].checksum == _V9_CHECKSUM
    assert schema.MIGRATIONS[9] is schema.RUNTIME_SCHEMA_HARDENING_MIGRATION
    assert tuple(item.version for item in v10_registry) == tuple(range(1, 11))
    assert schema.RUNTIME_SCHEMA_HARDENING_MIGRATION.name == (
        "runtime_timestamp_dispatch_hardening_v1"
    )
    assert len(schema.RUNTIME_SCHEMA_HARDENING_MIGRATION.statements) == 39
    assert len(schema.PRODUCTION_RUNTIME_V8_TABLES) == 25
    assert len(schema.WORKER_PROJECTION_SIDECAR_TABLES) == 3

    connection = _connection()
    try:
        _apply_migrations(connection, v10_registry)
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 10
        assert len(_v10_trigger_names(connection)) == 36
        assert connection.execute(
            "SELECT count(*) FROM sqlite_schema "
            "WHERE name='schema_v10_runtime_hardening_preflight'"
        ).fetchone()[0] == 0
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_v10_guards_reject_check_null_bypass_and_partial_dispatch_tuple() -> None:
    connection = _connection()
    try:
        _apply_migrations(connection, schema.MIGRATIONS[:10])

        # V1 generic receipts remain backward compatible; only the V8-owned
        # namespace is reinterpreted as a production timestamp.
        connection.execute(
            "INSERT INTO transaction_receipts(transaction_id,kind,committed_at) "
            "VALUES ('ordinary-invalid-time','ordinary',?)",
            (_INVALID_24,),
        )
        connection.execute(
            "UPDATE transaction_receipts SET committed_at=? "
            "WHERE transaction_id='ordinary-invalid-time'",
            ("y" * 24,),
        )
        assert connection.execute(
            "SELECT committed_at FROM transaction_receipts "
            "WHERE transaction_id='ordinary-invalid-time'"
        ).fetchone()[0] == "y" * 24
        with pytest.raises(sqlite3.IntegrityError, match="production_timestamp_invalid"):
            connection.execute(
                "UPDATE transaction_receipts SET kind='_worker_execution_v3' "
                "WHERE transaction_id='ordinary-invalid-time'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="production_timestamp_invalid"):
            connection.execute(
                "INSERT INTO transaction_receipts(transaction_id,kind,committed_at) "
                "VALUES (?,?,?)",
                ("_worker-execution:" + "a" * 32, "_worker_execution_v3", _INVALID_24),
            )

        with pytest.raises(sqlite3.IntegrityError, match="production_timestamp_invalid"):
            connection.execute(
                "INSERT INTO worker_ledger_head(singleton,state_revision,sequence,"
                "previous_tail_sha256,tail_sha256,updated_at) "
                "VALUES (1,1,0,zeroblob(32),zeroblob(32),?)",
                (_INVALID_24,),
            )

        # The old V8 initial guard observes only inflight_sequence. A partial
        # revision/ordinal tuple therefore used to pass both it and CHECK(NULL).
        with pytest.raises(
            sqlite3.IntegrityError,
            match="production_outbox_dispatch_tuple_invalid",
        ):
            connection.execute(
                "INSERT INTO outbox_dispatch_head("
                "singleton,state_revision,published_sequence,"
                "published_repository_revision,published_ordinal,"
                "inflight_sequence,inflight_repository_revision,inflight_ordinal,"
                "previous_head_sha256,head_sha256,updated_at) "
                "VALUES (1,1,0,NULL,NULL,NULL,1,NULL,zeroblob(32),zeroblob(32),?)",
                (_NOW,),
            )
    finally:
        connection.close()


def test_every_v8_v9_timestamp_owner_has_an_executable_v10_insert_guard() -> None:
    cases = {
        "business_cutover_install_receipts": {"committed_at": _INVALID_24},
        "business_asset_stage_receipts": {"committed_at": _INVALID_24},
        "business_asset_coverage_receipts": {"committed_at": _INVALID_24},
        "repository_audit_certificates": {"committed_at": _INVALID_24},
        "business_cutover_activation_receipts": {"committed_at": _INVALID_24},
        "business_cutover_mount_receipts": {"committed_at": _INVALID_24},
        "atomic_business_write_receipts": {"committed_at": _INVALID_24},
        "worker_ledger_entries": {"committed_at": _INVALID_24},
        "worker_execution_receipts": {"committed_at": _INVALID_24},
        "pipeline_control_intents": {"committed_at": _INVALID_24},
        "pipeline_control_receipts": {"committed_at": _INVALID_24},
        "worker_targets": {"created_at": _INVALID_24},
        "worker_attempts": {"started_at": _INVALID_24},
        "worker_effects": {"authorized_at": _INVALID_24},
        "worker_results": {"completed_at": _INVALID_24},
        "worker_reconcile_proofs": {"observed_at": _INVALID_24},
        "outbox_allocations": {"allocated_at": _INVALID_24},
        "worker_effect_request_projections": {"authorized_at": _INVALID_24},
        "worker_result_payload_projections": {"committed_at": _INVALID_24},
        "worker_proof_payload_projections": {
            "observed_at": _NOW,
            "durable_result_recorded_at": _INVALID_24,
        },
        "repository_audit_head": {"updated_at": _INVALID_24},
        "worker_ledger_head": {"updated_at": _INVALID_24},
        "worker_target_current": {
            "updated_at": _NOW,
            "next_attempt_at": _INVALID_24,
        },
        "outbox_allocation_head": {"updated_at": _INVALID_24},
        "outbox_dispatch_head": {
            "published_sequence": 0,
            "published_repository_revision": None,
            "published_ordinal": None,
            "inflight_sequence": None,
            "inflight_repository_revision": None,
            "inflight_ordinal": None,
            "updated_at": _INVALID_24,
        },
        "business_cutover_head": {"updated_at": _INVALID_24},
    }
    connection = _connection()
    try:
        _apply_migrations(connection, schema.MIGRATIONS[:10])
        for table, values in cases.items():
            columns = tuple(values)
            sql = "INSERT INTO {0}({1}) VALUES ({2})".format(
                table,
                ",".join(columns),
                ",".join("?" for _ in columns),
            )
            with pytest.raises(
                sqlite3.IntegrityError,
                match="production_timestamp_invalid",
            ) as raised:
                connection.execute(sql, tuple(values[column] for column in columns))
            assert "production_timestamp_invalid" in str(raised.value), table
    finally:
        connection.close()


def test_invalid_v9_timestamp_history_rejects_v10_atomically() -> None:
    connection = _connection()
    try:
        _apply_migrations(connection, schema.MIGRATIONS[:9])
        before = tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT version,name,checksum,applied_at FROM schema_migrations "
                "ORDER BY version"
            ).fetchall()
        )
        connection.execute(
            "INSERT INTO worker_ledger_head(singleton,state_revision,sequence,"
            "previous_tail_sha256,tail_sha256,updated_at) "
            "VALUES (1,1,0,zeroblob(32),zeroblob(32),?)",
            (_INVALID_24,),
        )

        with pytest.raises(
            sqlite3.IntegrityError,
            match="schema_v10_runtime_hardening_precondition",
        ):
            _apply_migrations(connection, schema.MIGRATIONS[9:10])

        assert connection.execute("PRAGMA user_version").fetchone()[0] == 9
        assert tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT version,name,checksum,applied_at FROM schema_migrations "
                "ORDER BY version"
            ).fetchall()
        ) == before
        assert _v10_trigger_names(connection) == set()
        assert connection.execute(
            "SELECT count(*) FROM schema_migrations WHERE version=10"
        ).fetchone()[0] == 0
    finally:
        connection.close()


def test_partial_v9_dispatch_history_rejects_v10_atomically() -> None:
    connection = _connection()
    try:
        _apply_migrations(connection, schema.MIGRATIONS[:9])
        trigger_rows = tuple(
            connection.execute(
                "SELECT name,sql FROM sqlite_schema "
                "WHERE type='trigger' AND tbl_name='outbox_dispatch_head' "
                "ORDER BY name"
            ).fetchall()
        )
        for row in trigger_rows:
            connection.execute('DROP TRIGGER "' + row["name"] + '"')
        connection.execute(
            "INSERT INTO outbox_dispatch_head("
            "singleton,state_revision,published_sequence,"
            "published_repository_revision,published_ordinal,"
            "inflight_sequence,inflight_repository_revision,inflight_ordinal,"
            "previous_head_sha256,head_sha256,updated_at) "
            "VALUES (1,1,0,NULL,NULL,NULL,1,NULL,zeroblob(32),zeroblob(32),?)",
            (_NOW,),
        )
        for row in trigger_rows:
            connection.execute(row["sql"])

        assert kernel._schema_fingerprint(connection, 9) == kernel._expected_schema_fingerprint(9)
        with pytest.raises(
            sqlite3.IntegrityError,
            match="schema_v10_runtime_hardening_precondition",
        ):
            _apply_migrations(connection, schema.MIGRATIONS[9:10])
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 9
        assert _v10_trigger_names(connection) == set()
    finally:
        connection.close()
