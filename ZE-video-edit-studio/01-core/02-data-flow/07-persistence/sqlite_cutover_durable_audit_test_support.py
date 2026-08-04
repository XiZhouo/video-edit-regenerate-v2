"""Test-only constructors for canonical cutover phase prefixes."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import sqlite_business_test_support as business_support
import sqlite_production_test_support as production_support
from business_cutover_head_integrity import issue_business_cutover_head_chain_from_mount
from sqlite_business_runtime import BusinessAuthorityState
from sqlite_kernel import SqliteDatabase, _expected_schema_fingerprint, _schema_fingerprint
from sqlite_production_authority import (
    _CUTOVER_HEAD_ROW_COLUMNS,
    _mount_evidence_from_row,
    _mounted_row,
)
from sqlite_schema import MIGRATIONS


_REPOSITORY_EPOCH = "e" * 64
_SOURCE_REVISION = 1
_RUNTIME_GENERATION = "cutover-durable-audit-test"
_CODE_DIGEST = hashlib.sha256(b"ze.cutover-durable-audit.test.v1").digest()


def _trigger_sql(connection: Any, names: tuple[str, ...]) -> dict[str, str]:
    placeholders = ",".join("?" for _ in names)
    rows = connection.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name IN ("
        + placeholders
        + ")",
        names,
    ).fetchall()
    result = {row[0]: row[1] for row in rows}
    if set(result) != set(names) or any(type(value) is not str for value in result.values()):
        raise AssertionError("cutover phase fixture trigger set changed")
    return result


def _trim_phase(database: SqliteDatabase, phase_revision: int) -> None:
    connection = None
    try:
        connection, _filesystem = database._open_unvalidated()
        connection.execute("BEGIN IMMEDIATE")
        mounted = _mount_evidence_from_row(_mounted_row(connection))
        chain = issue_business_cutover_head_chain_from_mount(mounted)
        target = chain[phase_revision - 1].row_values()
        names = [
            "business_cutover_head_cas_update_guard",
            "business_cutover_head_phase_guard",
            "business_cutover_mount_receipts_no_delete",
            "transaction_receipts_v13_no_update_after_cutover_head",
            "transaction_receipts_v13_no_delete_after_cutover_head",
        ]
        binding_tables = (
            "business_cutover_asset_provider_bindings",
            "business_cutover_audit_bindings",
            "business_cutover_activation_bindings",
            "business_cutover_mount_bindings",
        )
        keep_binding_count = max(0, phase_revision - 1)
        removed_binding_tables = binding_tables[keep_binding_count:]
        names.extend(table + "_no_delete" for table in removed_binding_tables)
        if phase_revision < 4:
            names.extend((
                "repository_runtime_control_revision_guard",
                "business_cutover_activation_receipts_no_delete",
            ))
        if phase_revision < 3:
            names.extend((
                "repository_audit_head_no_delete",
                "repository_audit_certificates_no_delete",
            ))
        originals = _trigger_sql(connection, tuple(names))
        for name in names:
            connection.execute('DROP TRIGGER "' + name + '"')
        for table in reversed(removed_binding_tables):
            connection.execute('DELETE FROM "' + table + '"')
        connection.execute(
            "UPDATE business_cutover_head SET "
            + ",".join(column + "=:" + column for column in _CUTOVER_HEAD_ROW_COLUMNS)
            + " WHERE singleton=1",
            target,
        )
        if phase_revision < 4:
            connection.execute(
                "UPDATE repository_runtime_control SET authority_state='read_only',"
                "state_revision=1 WHERE singleton=1"
            )
        mount_transaction = connection.execute(
            "SELECT transaction_id FROM business_cutover_mount_receipts"
        ).fetchone()[0]
        connection.execute("DELETE FROM business_cutover_mount_receipts")
        connection.execute(
            "DELETE FROM transaction_receipts WHERE transaction_id=?",
            (mount_transaction,),
        )
        if phase_revision < 4:
            activation_transaction = connection.execute(
                "SELECT transaction_id FROM business_cutover_activation_receipts"
            ).fetchone()[0]
            connection.execute("DELETE FROM business_cutover_activation_receipts")
            connection.execute(
                "DELETE FROM transaction_receipts WHERE transaction_id=?",
                (activation_transaction,),
            )
        if phase_revision < 3:
            audit_transaction = connection.execute(
                "SELECT transaction_id FROM repository_audit_certificates"
            ).fetchone()[0]
            connection.execute("DELETE FROM repository_audit_head")
            connection.execute("DELETE FROM repository_audit_certificates")
            connection.execute(
                "DELETE FROM transaction_receipts WHERE transaction_id=?",
                (audit_transaction,),
            )
        for name in names:
            connection.execute(originals[name])
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise AssertionError("trimmed cutover phase violates a foreign key")
        if (
            _schema_fingerprint(connection, MIGRATIONS[-1].version)
            != _expected_schema_fingerprint()
        ):
            raise AssertionError("trimmed cutover phase changed schema identity")
        connection.commit()
    finally:
        if connection is not None:
            if connection.in_transaction:
                connection.rollback()
            connection.close()
    database._finalize_committed_storage()


def create_canonical_cutover_phase_database(
    path: Any,
    *,
    phase_revision: int,
) -> SqliteDatabase:
    if type(phase_revision) is not int or phase_revision not in (1, 2, 3, 4, 5):
        raise TypeError("phase_revision must be an integer from 1 through 5")
    database = SqliteDatabase(Path(path).resolve())
    database.initialize()
    connection = None
    try:
        connection, _filesystem = database._open_unvalidated()
        connection.execute("BEGIN IMMEDIATE")
        database._validate_initialized(connection)
        connection.execute(
            "UPDATE kernel_repository SET authority='sqlite' "
            "WHERE singleton=1 AND authority='legacy'"
        )
        cutover_id = business_support._install_mounted_test_authority(
            connection,
            repository_epoch=_REPOSITORY_EPOCH,
            source_admission_revision=_SOURCE_REVISION,
            runtime_generation=_RUNTIME_GENERATION,
            code_digest=_CODE_DIGEST,
            authority_state=BusinessAuthorityState.ACTIVE,
        )
        connection.commit()
    finally:
        if connection is not None:
            if connection.in_transaction:
                connection.rollback()
            connection.close()
    database._finalize_committed_storage()
    production_support._upgrade_to_canonical_mount(
        SimpleNamespace(database=database),
        repository_epoch=_REPOSITORY_EPOCH,
        cutover_id=cutover_id,
        source_revision=_SOURCE_REVISION,
        runtime_generation=_RUNTIME_GENERATION,
        code_digest=_CODE_DIGEST,
    )
    database._finalize_committed_storage()
    if phase_revision < 5:
        _trim_phase(database, phase_revision)
    return database


__all__ = ()
