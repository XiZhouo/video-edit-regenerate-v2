"""Executable schema-v13 direct-predecessor binding evidence."""

from __future__ import annotations

import hashlib
import sqlite3
import sys
from pathlib import Path

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

import sqlite_schema as schema
from sqlite_cutover_phase_head_writer import (
    SQLiteCutoverPhaseHeadWriter,
    SQLiteCutoverPhaseHeadStatus,
    _CUTOVER_PHASE_HEAD_WRITER_CAPABILITY,
)
from sqlite_cutover_install_test_support import (
    _create_empty_cutover_install_fixture,
    _make_cutover_install_writer,
)
from sqlite_business_test_support import create_active_business_database
from sqlite_cutover_durable_audit_test_support import (
    _trim_phase,
    create_canonical_cutover_phase_database,
)
from test_sqlite_schema import _apply_migrations


_NOW = "2026-07-13T00:00:00.000Z"
_FROZEN_V1_V12_CHECKSUMS = (
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
    "aa61960d2bd4e10a41d1ef51767bf4f4809f5fab26cae5ebef3bfa30fccf35a2",
)


def _connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def test_v13_is_one_exact_additive_registry_suffix() -> None:
    assert tuple(item.checksum for item in schema.MIGRATIONS[:12]) == (
        _FROZEN_V1_V12_CHECKSUMS
    )
    migration = schema.DIRECT_PREDECESSOR_BINDING_MIGRATION
    assert (migration.version, migration.name) == (
        13,
        "direct_predecessor_phase_bindings_v1",
    )
    assert schema.MIGRATIONS[12:] == (migration,)
    assert len(schema.PRODUCTION_RUNTIME_TABLES) == 36
    assert schema.DIRECT_PREDECESSOR_BINDING_TABLES == (
        "business_cutover_asset_provider_bindings",
        "business_cutover_audit_bindings",
        "business_cutover_activation_bindings",
        "business_cutover_mount_bindings",
    )


def test_clean_authentic_v12_installs_four_strict_append_only_tables() -> None:
    connection = _connection()
    try:
        _apply_migrations(connection, schema.MIGRATIONS[:12])
        _apply_migrations(connection, schema.MIGRATIONS[12:])

        strict = {
            row["name"]: row["strict"]
            for row in connection.execute("PRAGMA table_list").fetchall()
        }
        triggers = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='trigger'"
            ).fetchall()
        }
        for table in schema.DIRECT_PREDECESSOR_BINDING_TABLES:
            assert strict[table] == 1
            assert table + "_no_update" in triggers
            assert table + "_no_delete" in triggers
            columns = {
                row["name"]
                for row in connection.execute(
                    'PRAGMA table_info("' + table + '")'
                ).fetchall()
            }
            assert {
                "predecessor_head_sha256",
                "predecessor_previous_head_sha256",
                "successor_previous_head_sha256",
                "successor_head_sha256",
                "source_admission_sha256",
                "source_freeze_sha256",
                "file_admission_sha256",
                "maintenance_repository_proof_sha256",
                "before_receipt_root_sha256",
                "after_receipt_root_sha256",
                "worker_ledger_tail_sha256",
                "allocation_tail_sha256",
                "dispatch_head_sha256",
                "binding_manifest_bytes",
                "binding_manifest_sha256",
            }.issubset(columns)
            if table in (
                "business_cutover_asset_provider_bindings",
                "business_cutover_mount_bindings",
            ):
                assert "coverage_blob_root_sha256" in columns
        assert {
            "business_cutover_head_v13_2_binding_guard",
            "business_cutover_head_v13_3_binding_guard",
            "business_cutover_head_v13_4_binding_guard",
            "business_cutover_head_v13_5_binding_guard",
        }.issubset(triggers)
        reverse_sql = {
            row["name"]: row["sql"]
            for row in connection.execute(
                "SELECT name,sql FROM sqlite_schema WHERE type='trigger' "
                "AND name GLOB 'business_cutover_head_v13_*_binding_guard'"
            ).fetchall()
        }
        assert "NEW.coverage_transaction_id" in reverse_sql[
            "business_cutover_head_v13_2_binding_guard"
        ]
        assert "NEW.audit_transaction_id" in reverse_sql[
            "business_cutover_head_v13_3_binding_guard"
        ]
        assert "NEW.activation_transaction_id" in reverse_sql[
            "business_cutover_head_v13_4_binding_guard"
        ]
        assert "NEW.mount_transaction_id" in reverse_sql[
            "business_cutover_head_v13_5_binding_guard"
        ]
        for sql in reverse_sql.values():
            assert "OLD.previous_head_sha256" in sql
            assert "JOIN repository_runtime_control AS runtime" in sql
            assert "JOIN worker_ledger_head AS worker" in sql
            assert "JOIN outbox_allocation_head AS allocation" in sql
            assert "JOIN outbox_dispatch_head AS dispatch" in sql
            assert "count(*) FROM transaction_receipts" in sql
        provider_index = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type='index' AND "
            "name='business_cutover_asset_provider_checkpoint'"
        ).fetchone()[0]
        assert "provider_checkpoint_sequence" in provider_index
        assert "provider_checkpoint_sha256" not in provider_index
    finally:
        connection.close()


def test_v13_preflight_rejects_a_noncanonical_v12_ledger_atomically() -> None:
    connection = _connection()
    try:
        _apply_migrations(connection, schema.MIGRATIONS[:12])
        connection.execute(
            "UPDATE schema_migrations SET checksum=? WHERE version=12",
            ("0" * 64,),
        )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="schema_v13_direct_predecessor_precondition",
        ):
            _apply_migrations(connection, schema.MIGRATIONS[12:])
        assert connection.execute(
            "SELECT count(*) FROM schema_migrations WHERE version=13"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT count(*) FROM sqlite_schema "
            "WHERE type='table' AND name=?",
            (schema.DIRECT_PREDECESSOR_BINDING_TABLES[0],),
        ).fetchone()[0] == 0
    finally:
        connection.close()


def test_v13_preflight_rejects_reserved_production_receipt_but_preserves_legacy() -> None:
    rejected = _connection()
    accepted = _connection()
    try:
        for connection in (rejected, accepted):
            _apply_migrations(connection, schema.MIGRATIONS[:12])
        rejected.execute(
            "INSERT INTO transaction_receipts(transaction_id,kind,committed_at) "
            "VALUES (?,?,?)",
            (
                "_business-cutover-mount:" + "a" * 64,
                "_business_cutover_mount_v2",
                _NOW,
            ),
        )
        with pytest.raises(sqlite3.IntegrityError):
            _apply_migrations(rejected, schema.MIGRATIONS[12:])

        accepted.execute(
            "INSERT INTO transaction_receipts(transaction_id,kind,committed_at) "
            "VALUES ('legacy-test:one','legacy_test_v1',?)",
            (_NOW,),
        )
        _apply_migrations(accepted, schema.MIGRATIONS[12:])
        assert accepted.execute(
            "SELECT kind FROM transaction_receipts "
            "WHERE transaction_id='legacy-test:one'"
        ).fetchone()[0] == "legacy_test_v1"
    finally:
        rejected.close()
        accepted.close()


def test_head_cannot_advance_without_exact_direct_predecessor_binding(
    tmp_path: Path,
) -> None:
    fixture = _create_empty_cutover_install_fixture(tmp_path / "v13.sqlite3")
    installed = _make_cutover_install_writer(fixture).install(fixture.projection)
    assert installed.evidence is not None
    finalized = SQLiteCutoverPhaseHeadWriter(
        fixture.database,
        _capability=_CUTOVER_PHASE_HEAD_WRITER_CAPABILITY,
    ).finalize()
    assert finalized.status is SQLiteCutoverPhaseHeadStatus.EXACT

    connection = sqlite3.connect(fixture.database.path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        head = connection.execute(
            "SELECT * FROM business_cutover_head WHERE singleton=1"
        ).fetchone()
        coverage = connection.execute(
            "SELECT transaction_id,manifest_sha256 "
            "FROM business_asset_coverage_receipts"
        ).fetchone()
        assert head is not None and coverage is not None
        with pytest.raises(
            sqlite3.IntegrityError,
            match="production_cutover_direct_predecessor_missing",
        ):
            connection.execute(
                "UPDATE business_cutover_head SET "
                "state_revision=2,phase_revision=2,phase='assets_covered',"
                "authority_state='read_only',coverage_transaction_id=?,"
                "coverage_manifest_sha256=?,previous_head_sha256=?,"
                "head_sha256=?,updated_at=? WHERE singleton=1",
                (
                    coverage["transaction_id"],
                    coverage["manifest_sha256"],
                    head["head_sha256"],
                    hashlib.sha256(b"v13-unbound-head").digest(),
                    head["updated_at"],
                ),
            )
    finally:
        connection.close()


def test_current_fixture_replays_all_four_guarded_transitions_without_bypass(
    tmp_path: Path,
) -> None:
    fixture = create_active_business_database(tmp_path / "v13-real-chain.sqlite3")
    connection = sqlite3.connect(fixture.database.path, isolation_level=None)
    try:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert tuple(
            connection.execute('SELECT count(*) FROM "' + table + '"').fetchone()[0]
            for table in schema.DIRECT_PREDECESSOR_BINDING_TABLES
        ) == (1, 1, 1, 1)
        assert connection.execute(
            "SELECT count(*) FROM sqlite_schema WHERE type='trigger' AND "
            "name GLOB 'business_cutover_head_v13_*_binding_guard'"
        ).fetchone()[0] == 4
        chain = connection.execute(
            """
            SELECT asset.successor_head_sha256=audit.predecessor_head_sha256,
                   audit.successor_head_sha256=activation.predecessor_head_sha256,
                   activation.successor_head_sha256=mount.predecessor_head_sha256,
                   mount.successor_head_sha256=head.head_sha256
            FROM business_cutover_asset_provider_bindings AS asset
            JOIN business_cutover_audit_bindings AS audit
              ON audit.cutover_id=asset.cutover_id
            JOIN business_cutover_activation_bindings AS activation
              ON activation.cutover_id=asset.cutover_id
            JOIN business_cutover_mount_bindings AS mount
              ON mount.cutover_id=asset.cutover_id
            JOIN business_cutover_head AS head
              ON head.cutover_id=asset.cutover_id
            """
        ).fetchone()
        assert chain == (1, 1, 1, 1)
        for table in schema.DIRECT_PREDECESSOR_BINDING_TABLES:
            with pytest.raises(
                sqlite3.IntegrityError,
                match="production_history_append_only",
            ):
                connection.execute(
                    'UPDATE "' + table + '" SET cutover_id=cutover_id'
                )
            with pytest.raises(
                sqlite3.IntegrityError,
                match="production_history_append_only",
            ):
                connection.execute('DELETE FROM "' + table + '"')
    finally:
        connection.close()


def test_committed_binding_cannot_advance_after_its_live_snapshot_is_stale(
    tmp_path: Path,
) -> None:
    fixture = create_active_business_database(tmp_path / "v13-stale.sqlite3")
    connection = sqlite3.connect(fixture.database.path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        trigger_rows = connection.execute(
            "SELECT name,sql FROM sqlite_schema WHERE type='trigger' AND "
            "tbl_name='business_cutover_head' ORDER BY name"
        ).fetchall()
        connection.execute("BEGIN IMMEDIATE")
        for row in trigger_rows:
            connection.execute('DROP TRIGGER "' + row["name"] + '"')
        connection.execute(
            """
            UPDATE business_cutover_head
            SET state_revision=asset.predecessor_state_revision,
                phase_revision=asset.predecessor_phase_revision,
                phase=asset.predecessor_phase,
                authority_state=asset.predecessor_authority_state,
                coverage_transaction_id=NULL,coverage_manifest_sha256=NULL,
                audit_transaction_id=NULL,audit_manifest_sha256=NULL,
                activation_transaction_id=NULL,activation_manifest_sha256=NULL,
                mount_transaction_id=NULL,mount_manifest_sha256=NULL,
                previous_head_sha256=asset.predecessor_previous_head_sha256,
                head_sha256=asset.predecessor_head_sha256,
                updated_at=asset.predecessor_updated_at
            FROM business_cutover_asset_provider_bindings AS asset
            WHERE business_cutover_head.cutover_id=asset.cutover_id
            """
        )
        for row in trigger_rows:
            connection.execute(row["sql"])
        connection.commit()

        with pytest.raises(
            sqlite3.IntegrityError,
            match="production_cutover_direct_predecessor_missing",
        ):
            connection.execute(
                """
                UPDATE business_cutover_head
                SET state_revision=asset.successor_state_revision,
                    phase_revision=asset.successor_phase_revision,
                    phase=asset.successor_phase,
                    authority_state=asset.successor_authority_state,
                    coverage_transaction_id=asset.coverage_transaction_id,
                    coverage_manifest_sha256=asset.coverage_manifest_sha256,
                    previous_head_sha256=asset.successor_previous_head_sha256,
                    head_sha256=asset.successor_head_sha256,
                    updated_at=asset.successor_updated_at
                FROM business_cutover_asset_provider_bindings AS asset
                WHERE business_cutover_head.cutover_id=asset.cutover_id
                """
            )
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()


def test_provider_generation_sequence_rejects_equivocation(
    tmp_path: Path,
) -> None:
    fixture = create_active_business_database(tmp_path / "v13-provider-fork.sqlite3")
    connection = sqlite3.connect(fixture.database.path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    table = "business_cutover_asset_provider_bindings"
    try:
        source = connection.execute('SELECT * FROM "' + table + '"').fetchone()
        assert source is not None
        connection.execute("PRAGMA foreign_keys=OFF")
        for name in (
            table + "_snapshot_guard",
            table + "_evidence_guard",
        ):
            connection.execute('DROP TRIGGER "' + name + '"')
        columns = tuple(
            row[1] for row in connection.execute(
                'PRAGMA table_info("' + table + '")'
            ).fetchall()
        )
        fork_cutover_id = "f" * 64
        values = {column: source[column] for column in columns}
        values.update({
            "cutover_id": fork_cutover_id,
            "file_admission_cutover_id": fork_cutover_id,
            "coverage_transaction_id": (
                "_business-cutover-asset-coverage:" + fork_cutover_id
            ),
            "successor_head_sha256": hashlib.sha256(
                b"v13-provider-fork-successor"
            ).digest(),
            "binding_manifest_sha256": hashlib.sha256(
                b"v13-provider-fork-binding"
            ).digest(),
            "provider_checkpoint_sha256": hashlib.sha256(
                b"v13-provider-fork-checkpoint"
            ).digest(),
        })
        with pytest.raises(
            sqlite3.IntegrityError,
            match=(
                "provider_id.*provider_generation.*"
                "provider_checkpoint_sequence"
            ),
        ):
            connection.execute(
                'INSERT INTO "' + table + '"(' + ",".join(columns) + ") "
                "VALUES (" + ",".join("?" for _column in columns) + ")",
                tuple(values[column] for column in columns),
            )
    finally:
        connection.close()


@pytest.mark.parametrize(
    "timestamp_column",
    ("maintenance_observed_at", "provider_checked_at"),
)
def test_v13_timestamp_checks_reject_unparseable_24_byte_values(
    tmp_path: Path,
    timestamp_column: str,
) -> None:
    database = create_canonical_cutover_phase_database(
        tmp_path / ("v13-bad-time-%s.sqlite3" % timestamp_column),
        phase_revision=5,
    )
    connection = sqlite3.connect(database.path)
    connection.row_factory = sqlite3.Row
    try:
        binding = connection.execute(
            "SELECT * FROM business_cutover_asset_provider_bindings"
        ).fetchone()
        assert binding is not None
    finally:
        connection.close()

    _trim_phase(database, 1)
    connection = sqlite3.connect(database.path, isolation_level=None)
    try:
        columns = tuple(binding.keys())
        values = {column: binding[column] for column in columns}
        values[timestamp_column] = "z" * 24
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            connection.execute(
                "INSERT INTO business_cutover_asset_provider_bindings("
                + ",".join(columns) + ") VALUES ("
                + ",".join("?" for _column in columns) + ")",
                tuple(values[column] for column in columns),
            )
    finally:
        connection.close()


def test_transaction_receipts_become_immutable_after_first_cutover_head(
    tmp_path: Path,
) -> None:
    database = create_canonical_cutover_phase_database(
        tmp_path / "v13-receipt-history.sqlite3",
        phase_revision=5,
    )
    connection = sqlite3.connect(database.path, isolation_level=None)
    try:
        connection.execute(
            "INSERT INTO transaction_receipts(transaction_id,kind,committed_at) "
            "VALUES ('ordinary:after-mount','ordinary_v1',?)",
            (_NOW,),
        )
        for statement in (
            "UPDATE transaction_receipts SET kind='changed' "
            "WHERE transaction_id='ordinary:after-mount'",
            "DELETE FROM transaction_receipts "
            "WHERE transaction_id='ordinary:after-mount'",
        ):
            with pytest.raises(
                sqlite3.IntegrityError,
                match="production_receipt_history_append_only",
            ):
                connection.execute(statement)
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("phase_revision", "binding_table", "receipt_table", "tamper_column"),
    (
        (
            3,
            "business_cutover_activation_bindings",
            "business_cutover_activation_receipts",
            "audit_certificate_tail_sha256",
        ),
        (
            3,
            "business_cutover_activation_bindings",
            "business_cutover_activation_receipts",
            "source_freeze_sha256",
        ),
        (
            3,
            "business_cutover_activation_bindings",
            "business_cutover_activation_receipts",
            "before_receipt_root_sha256",
        ),
        (
            4,
            "business_cutover_mount_bindings",
            "business_cutover_mount_receipts",
            "target_schema_fingerprint",
        ),
        (
            4,
            "business_cutover_mount_bindings",
            "business_cutover_mount_receipts",
            "final_admission_pair",
        ),
    ),
)
def test_later_binding_cannot_fork_from_verified_predecessor_evidence(
    tmp_path: Path,
    phase_revision: int,
    binding_table: str,
    receipt_table: str,
    tamper_column: str,
) -> None:
    database = create_canonical_cutover_phase_database(
        tmp_path / ("v13-prior-fork-%d.sqlite3" % phase_revision),
        phase_revision=5,
    )
    connection = sqlite3.connect(database.path)
    connection.row_factory = sqlite3.Row
    try:
        binding = connection.execute(
            'SELECT * FROM "' + binding_table + '"'
        ).fetchone()
        receipt = connection.execute(
            'SELECT * FROM "' + receipt_table + '"'
        ).fetchone()
        generic = connection.execute(
            "SELECT * FROM transaction_receipts WHERE transaction_id=?",
            (receipt["transaction_id"],),
        ).fetchone()
        assert binding is not None and receipt is not None and generic is not None
    finally:
        connection.close()

    _trim_phase(database, phase_revision)
    connection = sqlite3.connect(database.path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO transaction_receipts(transaction_id,kind,committed_at) "
            "VALUES (?,?,?)",
            tuple(generic),
        )
        receipt_columns = tuple(receipt.keys())
        connection.execute(
            'INSERT INTO "' + receipt_table + '"('
            + ",".join(receipt_columns) + ") VALUES ("
            + ",".join("?" for _column in receipt_columns) + ")",
            tuple(receipt[column] for column in receipt_columns),
        )
        binding_columns = tuple(binding.keys())
        values = {column: binding[column] for column in binding_columns}
        if tamper_column == "final_admission_pair":
            values["file_admission_sha256"] = hashlib.sha256(
                b"v13-final-admission-fork"
            ).digest()
            values["mount_final_admission_sha256"] = values[
                "file_admission_sha256"
            ]
        else:
            values[tamper_column] = (
                hashlib.sha256(b"v13-prior-evidence-fork").digest()
                if tamper_column.endswith("sha256")
                else "9" * 64
            )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="production_cutover_(activation|mount)_binding_invalid",
        ):
            connection.execute(
                'INSERT INTO "' + binding_table + '"('
                + ",".join(binding_columns) + ") VALUES ("
                + ",".join("?" for _column in binding_columns) + ")",
                tuple(values[column] for column in binding_columns),
            )
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()
