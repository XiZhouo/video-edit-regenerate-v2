"""Adversarial tests for the strict mounted-production authority reader."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import sys

import pytest


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

import sqlite_business_audit
import sqlite_business_runtime
import sqlite_business_test_support
import sqlite_kernel
import sqlite_production_authority as module
import sqlite_production_test_support as production_support
import test_business_cutover_mount_integrity as mount_support
from business_codec import encode_payload_bounded
from business_cutover_mount_contract import (
    MAX_BUSINESS_CUTOVER_MOUNT_MANIFEST_BYTES,
    MAX_BUSINESS_CUTOVER_MOUNT_MANIFEST_NODES,
)
from business_cutover_head_integrity import (
    issue_business_cutover_head_chain_from_mount,
)
from repository_audit_contract import (
    EMPTY_REPOSITORY_AUDIT_CERTIFICATE_TAIL_SHA256,
)
from repository_audit_integrity import _findings_digest
from sqlite_production_authority import (
    SQLiteProductionAuthorityError,
    _binding_from_snapshot,
    _linked_mount_rows_exact,
    _mount_evidence_from_row,
    _read_production_atomic_planning_snapshot,
    _read_scheduler_head_bindings,
    _require_mounted_binding,
)
from sqlite_production_test_support import (
    create_canonical_mounted_business_database,
)


def _mount_evidence():
    return mount_support._bundle()[-1].evidence


def _row(evidence):
    return {
        "manifest_bytes": evidence.manifest_bytes,
        "manifest_sha256": evidence.manifest_sha256,
    }


def _linked_bundle():
    cutover, _snapshots, coverage, _coverage_read, audit, activation, mount = (
        mount_support._bundle()
    )
    evidence = mount.evidence
    install_evidence = cutover.install
    coverage_evidence = coverage.expected_evidence
    audit_evidence = audit.evidence
    activation_evidence = activation.evidence
    head_values = issue_business_cutover_head_chain_from_mount(evidence)[
        -1
    ].row_values()
    rows = {
        "install": (
            install_evidence.cutover_id,
            install_evidence.transaction_id,
            install_evidence.receipt_kind,
            evidence.install.committed_at,
            install_evidence.runtime.repository_epoch,
            install_evidence.runtime.runtime_generation,
            install_evidence.runtime.code_digest,
            install_evidence.member_mapping_sha256,
            install_evidence.manifest_bytes,
            install_evidence.manifest_sha256,
            install_evidence.transaction_id,
            install_evidence.receipt_kind,
            evidence.install.committed_at,
        ),
        "coverage": (
            coverage_evidence.cutover_id,
            coverage_evidence.transaction_id,
            coverage_evidence.receipt_kind,
            evidence.coverage.committed_at,
            coverage_evidence.repository_epoch,
            coverage_evidence.source_admission_revision,
            coverage_evidence.source_admission_sha256,
            coverage_evidence.batch_count,
            evidence.coverage.member_root_sha256,
            evidence.coverage.repository_member_count,
            evidence.coverage.live_project_count,
            evidence.coverage.deleted_project_count,
            coverage_evidence.observed_occurrence_count,
            coverage_evidence.observed_row_count,
            coverage_evidence.observed_manifest_bytes,
            coverage_evidence.project_count,
            coverage_evidence.reference_count,
            coverage_evidence.unique_blob_count,
            coverage_evidence.unique_blob_bytes,
            coverage_evidence.referenced_bytes,
            coverage_evidence.project_root_sha256,
            coverage_evidence.blob_root_sha256,
            coverage_evidence.occurrence_root_sha256,
            coverage_evidence.coverage_sha256,
            coverage_evidence.manifest_bytes,
            coverage_evidence.manifest_sha256,
            coverage_evidence.transaction_id,
            coverage_evidence.receipt_kind,
            evidence.coverage.committed_at,
        ),
        "audit": (
            audit_evidence.certificate_id,
            audit_evidence.runtime.cutover_id,
            audit_evidence.sequence,
            audit_evidence.purpose.value,
            audit_evidence.transaction_id,
            audit_evidence.receipt_kind,
            audit_evidence.receipt_parent.committed_at,
            audit_evidence.previous_certificate_id,
            audit_evidence.previous_manifest_sha256,
            (
                EMPTY_REPOSITORY_AUDIT_CERTIFICATE_TAIL_SHA256
                if audit_evidence.previous_certificate_tail_sha256 is None
                else audit_evidence.previous_certificate_tail_sha256
            ),
            audit_evidence.schema.user_version,
            audit_evidence.schema.migration_ledger_count,
            audit_evidence.schema.migration_ledger_sha256,
            audit_evidence.coverage.receipt_namespace_count,
            audit_evidence.coverage.receipt_namespace_sha256,
            audit_evidence.coverage.worker_ledger_sequence,
            audit_evidence.coverage.worker_ledger_tail_sha256,
            _findings_digest(audit_evidence.findings),
            audit_evidence.manifest_bytes,
            audit_evidence.manifest_sha256,
            audit_evidence.certificate_tail_sha256,
            audit_evidence.transaction_id,
            audit_evidence.receipt_kind,
            audit_evidence.receipt_parent.committed_at,
        ),
        "activation": (
            activation_evidence.cutover_id,
            activation_evidence.transaction_id,
            activation_evidence.receipt_kind,
            activation_evidence.receipt_parent.committed_at,
            activation_evidence.install_transaction_id,
            activation_evidence.install_manifest_sha256,
            activation_evidence.audit_transaction_id,
            activation_evidence.audit_manifest_sha256,
            activation_evidence.expected_migration_ledger_sha256,
            activation_evidence.manifest_bytes,
            activation_evidence.manifest_sha256,
            activation_evidence.transaction_id,
            activation_evidence.receipt_kind,
            activation_evidence.receipt_parent.committed_at,
        ),
        "head": tuple(
            head_values[name] for name in module._CUTOVER_HEAD_ROW_COLUMNS
        ),
    }
    return evidence, rows


class _LinkedConnection:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, sql, values):
        del values
        if "business_cutover_install_receipts" in sql:
            row = self.rows["install"]
        elif "business_asset_coverage_receipts" in sql:
            row = self.rows["coverage"]
        elif "repository_audit_certificates" in sql:
            row = self.rows["audit"]
        elif "business_cutover_activation_receipts" in sql:
            row = self.rows["activation"]
        elif "business_cutover_head" in sql:
            row = self.rows["head"]
        else:
            raise AssertionError("unexpected linked-row query")
        return SimpleNamespace(fetchone=lambda: row)


def _same_length_tamper(value):
    assert type(value) is bytes and value
    return value[:-1] + bytes((value[-1] ^ 1,))


def test_real_canonical_mount_manifest_is_rebuilt_not_join_trusted() -> None:
    evidence = _mount_evidence()
    rebuilt = _mount_evidence_from_row(_row(evidence))
    assert rebuilt == evidence
    assert rebuilt.manifest_bytes == evidence.manifest_bytes
    assert rebuilt.manifest_sha256 == evidence.manifest_sha256


def test_all_linked_rows_and_domain_hashed_manifest_bytes_are_exact() -> None:
    evidence, rows = _linked_bundle()
    assert _linked_mount_rows_exact(_LinkedConnection(rows), evidence)


def _changed_projection(value):
    if value is None:
        return "a" * 32
    if type(value) is bytes:
        return _same_length_tamper(value)
    if type(value) is int:
        return value + 1
    if type(value) is str:
        assert value
        replacement = "x" if value[-1] != "x" else "y"
        return value[:-1] + replacement
    raise AssertionError("unhandled projection type")


@pytest.mark.parametrize(
    "row_name,index",
    tuple(
        (row_name, index)
        for row_name, width in (
            ("install", 13),
            ("coverage", 29),
            ("audit", 24),
            ("activation", 14),
            ("head", 22),
        )
        for index in range(width)
    ),
)
def test_every_linked_persisted_projection_field_is_enforced(
    row_name,
    index,
) -> None:
    evidence, rows = _linked_bundle()
    changed = list(rows[row_name])
    changed[index] = _changed_projection(changed[index])
    rows[row_name] = tuple(changed)
    assert not _linked_mount_rows_exact(_LinkedConnection(rows), evidence)


@pytest.mark.parametrize("row_name,index", (
    ("install", 8),
    ("coverage", 24),
    ("audit", 18),
    ("activation", 9),
))
def test_linked_manifest_bytes_are_rehashed_in_their_own_domains(
    row_name,
    index,
) -> None:
    evidence, rows = _linked_bundle()
    changed = list(rows[row_name])
    changed[index] = _same_length_tamper(changed[index])
    rows[row_name] = tuple(changed)
    assert not _linked_mount_rows_exact(_LinkedConnection(rows), evidence)


@pytest.mark.parametrize(
    "row_name,specialised_index,parent_index",
    (
        ("install", 3, 12),
        ("coverage", 3, 28),
    ),
)
def test_linked_parent_and_specialised_time_double_tamper_is_rejected(
    row_name,
    specialised_index,
    parent_index,
) -> None:
    evidence, rows = _linked_bundle()
    changed = list(rows[row_name])
    forged_time = "2026-07-11T23:59:59.998Z"
    assert changed[specialised_index] != forged_time
    changed[specialised_index] = forged_time
    changed[parent_index] = forged_time
    rows[row_name] = tuple(changed)
    assert not _linked_mount_rows_exact(_LinkedConnection(rows), evidence)


def test_real_sqlite_canonical_mount_is_authorized(tmp_path) -> None:
    baseline = create_canonical_mounted_business_database(
        tmp_path / "canonical-authority.sqlite3"
    )
    snapshot = _read_production_atomic_planning_snapshot(baseline.database)
    binding = _require_mounted_binding(snapshot.mounted_authority)
    assert binding.evidence.target_schema_version == module.MIGRATIONS[-1].version
    assert (
        binding.evidence.target_schema_fingerprint
        == module._expected_schema_fingerprint()
    )


def test_v8_mounted_authority_remains_v8_targeted_under_frozen_v10_reader(
    tmp_path,
    monkeypatch,
) -> None:
    """Build under V8, apply V9/V10, then prove the frozen V10 reader."""

    v8_registry = module.MIGRATIONS[:module._SCHEMA_V8_VERSION]
    with monkeypatch.context() as v8:
        for owner in (
            sqlite_kernel,
            sqlite_business_runtime,
            sqlite_business_audit,
            sqlite_business_test_support,
            production_support,
        ):
            v8.setattr(owner, "MIGRATIONS", v8_registry)
        for owner in (
            sqlite_business_audit,
            sqlite_business_test_support,
            production_support,
        ):
            v8.setattr(
                owner,
                "_expected_migration_ledger_sha256",
                lambda: module._SCHEMA_V8_MIGRATION_ROOT,
            )
        baseline = create_canonical_mounted_business_database(
            tmp_path / "v8-mounted-then-v9.sqlite3"
        )
        with sqlite3.connect(baseline.database.path) as before:
            assert before.execute("PRAGMA user_version").fetchone() == (
                module._SCHEMA_V8_VERSION,
            )

    connection = sqlite3.connect(baseline.database.path)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN EXCLUSIVE")
        for migration in module.MIGRATIONS[
            module._SCHEMA_V8_VERSION : module._SCHEMA_V10_VERSION
        ]:
            for statement in migration.statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations(version,name,checksum,applied_at) "
                "VALUES (?,?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                (migration.version, migration.name, migration.checksum),
            )
        connection.execute(
            "UPDATE kernel_metadata SET value=? WHERE key='schema_fingerprint'",
            (module._expected_schema_fingerprint(module._SCHEMA_V10_VERSION),),
        )
        connection.execute(
            "UPDATE kernel_metadata SET value=? "
            "WHERE key='kernel_schema_version'",
            (str(module._SCHEMA_V10_VERSION),),
        )
        connection.execute(
            "PRAGMA user_version=%d" % module._SCHEMA_V10_VERSION
        )
        connection.commit()
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()
    baseline.database._finalize_committed_storage()

    v10_registry = module.MIGRATIONS[:module._SCHEMA_V10_VERSION]
    with monkeypatch.context() as v10:
        for owner in (
            module,
            sqlite_kernel,
            sqlite_business_runtime,
            sqlite_business_audit,
        ):
            v10.setattr(owner, "MIGRATIONS", v10_registry)
        snapshot = _read_production_atomic_planning_snapshot(baseline.database)
        binding = _require_mounted_binding(snapshot.mounted_authority)
    assert binding.evidence.target_schema_version == module._SCHEMA_V8_VERSION
    assert (
        binding.evidence.target_schema_fingerprint
        == module._SCHEMA_V8_FINGERPRINT
    )


@pytest.mark.parametrize(
    "assignment",
    (
        "member_mapping_sha256=zeroblob(32)",
        "manifest_bytes=CAST(substr(manifest_bytes,1,"
        "length(manifest_bytes)-1)||x'7b' AS BLOB)",
    ),
)
def test_restored_real_append_only_trigger_does_not_hide_linked_tamper(
    tmp_path,
    assignment,
) -> None:
    baseline = create_canonical_mounted_business_database(
        tmp_path / ("linked-tamper-%d.sqlite3" % len(assignment))
    )
    trigger_name = "business_cutover_install_receipts_no_update"
    connection = sqlite3.connect(baseline.database.path)
    try:
        trigger_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
            (trigger_name,),
        ).fetchone()[0]
        connection.execute('DROP TRIGGER "' + trigger_name + '"')
        connection.execute(
            "UPDATE business_cutover_install_receipts SET " + assignment
        )
        connection.execute(trigger_sql)
        connection.commit()
        restored = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
            (trigger_name,),
        ).fetchone()[0]
        assert restored == trigger_sql
    finally:
        connection.close()

    with pytest.raises(SQLiteProductionAuthorityError) as rejected:
        _read_production_atomic_planning_snapshot(baseline.database)
    assert rejected.value.code == "production_mount_authorization_invalid"


@pytest.mark.parametrize(
    "column_name",
    ("previous_head_sha256", "head_sha256"),
)
def test_real_phase_head_hash_tamper_is_rebuilt_and_rejected(
    tmp_path,
    column_name,
) -> None:
    baseline = create_canonical_mounted_business_database(
        tmp_path / ("head-hash-tamper-%s.sqlite3" % column_name)
    )
    trigger_names = (
        "business_cutover_head_phase_guard",
        "business_cutover_head_cas_update_guard",
    )
    connection = sqlite3.connect(baseline.database.path)
    try:
        trigger_sql = {
            name: connection.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type='trigger' AND name=?",
                (name,),
            ).fetchone()[0]
            for name in trigger_names
        }
        for name in trigger_names:
            connection.execute('DROP TRIGGER "' + name + '"')
        connection.execute(
            "UPDATE business_cutover_head SET " + column_name + "=?",
            (b"q" * 32,),
        )
        for name in trigger_names:
            connection.execute(trigger_sql[name])
        connection.commit()
        for name in trigger_names:
            assert connection.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type='trigger' AND name=?",
                (name,),
            ).fetchone()[0] == trigger_sql[name]
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()

    with pytest.raises(SQLiteProductionAuthorityError) as rejected:
        _read_production_atomic_planning_snapshot(baseline.database)
    assert rejected.value.code == "production_mount_authorization_invalid"


@pytest.mark.parametrize(
    "table_name,mount_time_column,forged_time,advance_mount_receipt",
    (
        (
            "business_cutover_install_receipts",
            "install_committed_at",
            "2026-07-11T23:59:59.998Z",
            False,
        ),
        (
            "business_asset_coverage_receipts",
            "coverage_committed_at",
            "2026-07-12T00:00:00.000Z",
            True,
        ),
    ),
)
def test_real_specialised_and_parent_time_double_tamper_is_rejected(
    tmp_path,
    table_name,
    mount_time_column,
    forged_time,
    advance_mount_receipt,
) -> None:
    baseline = create_canonical_mounted_business_database(
        tmp_path / ("linked-time-double-tamper-%s.sqlite3" % table_name)
    )
    trigger_names = (
        table_name + "_no_update",
        "business_cutover_mount_receipts_no_update",
        "transaction_receipts_v13_no_update_after_cutover_head",
        *(
            ("transaction_receipts_v12_install_update_guard",)
            if table_name == "business_cutover_install_receipts"
            else ()
        ),
    )
    connection = sqlite3.connect(baseline.database.path)
    try:
        trigger_sql = {
            name: connection.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type='trigger' AND name=?",
                (name,),
            ).fetchone()[0]
            for name in trigger_names
        }
        transaction_id = connection.execute(
            "SELECT transaction_id FROM " + table_name
        ).fetchone()[0]
        mount_transaction_id = connection.execute(
            "SELECT transaction_id FROM business_cutover_mount_receipts"
        ).fetchone()[0]
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute("BEGIN IMMEDIATE")
        for name in trigger_names:
            connection.execute('DROP TRIGGER "' + name + '"')
        connection.execute(
            "UPDATE " + table_name + " SET committed_at=?",
            (forged_time,),
        )
        connection.execute(
            "UPDATE transaction_receipts SET committed_at=? "
            "WHERE transaction_id=?",
            (forged_time, transaction_id),
        )
        mount_assignment = mount_time_column + "=?"
        mount_values = [forged_time]
        if advance_mount_receipt:
            mount_assignment += ",committed_at=?"
            mount_values.append(forged_time)
            connection.execute(
                "UPDATE transaction_receipts SET committed_at=? "
                "WHERE transaction_id=?",
                (forged_time, mount_transaction_id),
            )
        connection.execute(
            "UPDATE business_cutover_mount_receipts SET " + mount_assignment,
            tuple(mount_values),
        )
        for name in trigger_names:
            connection.execute(trigger_sql[name])
        connection.commit()
        connection.execute("PRAGMA ignore_check_constraints=OFF")
        assert connection.execute("PRAGMA foreign_key_check").fetchone() is None
        for name in trigger_names:
            restored = connection.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type='trigger' AND name=?",
                (name,),
            ).fetchone()[0]
            assert restored == trigger_sql[name]
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()

    # The specialised row still joins its equally forged generic parent.  The
    # independently hashed mount manifest must nevertheless expose the time
    # substitution rather than treating the internally consistent pair as
    # fresh authority.
    with pytest.raises(SQLiteProductionAuthorityError) as rejected:
        _read_production_atomic_planning_snapshot(baseline.database)
    assert rejected.value.code == "production_mount_authorization_invalid"


def test_manifest_extra_key_and_row_digest_substitution_fail_closed() -> None:
    evidence = _mount_evidence()
    decoded = json.loads(evidence.manifest_bytes)
    decoded["unsealed"] = True
    altered = encode_payload_bounded(
        decoded,
        MAX_BUSINESS_CUTOVER_MOUNT_MANIFEST_BYTES,
        MAX_BUSINESS_CUTOVER_MOUNT_MANIFEST_NODES,
    )
    with pytest.raises(SQLiteProductionAuthorityError) as rejected:
        _mount_evidence_from_row({
            "manifest_bytes": altered,
            "manifest_sha256": evidence.manifest_sha256,
        })
    assert rejected.value.code == "production_mount_manifest_invalid"

    with pytest.raises(SQLiteProductionAuthorityError) as rejected:
        _mount_evidence_from_row({
            "manifest_bytes": evidence.manifest_bytes,
            "manifest_sha256": b"x" * 32,
        })
    assert rejected.value.code == "production_mount_manifest_invalid"


def test_mount_v1_identity_cannot_be_reauthorized_as_v2() -> None:
    evidence = _mount_evidence()
    decoded = json.loads(evidence.manifest_bytes)
    decoded["schema"] = "ze.business-cutover.mount-authorization-manifest.v1"
    decoded["protocol"] = "ze.business-cutover.mount-authorization.v1"
    decoded["receipt_parent"]["kind"] = "_business_cutover_mount_v1"
    decoded["receipt_parent"]["specialised_protocol"] = decoded["protocol"]
    altered = encode_payload_bounded(
        decoded,
        MAX_BUSINESS_CUTOVER_MOUNT_MANIFEST_BYTES,
        MAX_BUSINESS_CUTOVER_MOUNT_MANIFEST_NODES,
    )
    with pytest.raises(SQLiteProductionAuthorityError) as rejected:
        _mount_evidence_from_row({
            "manifest_bytes": altered,
            "manifest_sha256": (
                mount_support.integrity_module._manifest_digest(altered)
            ),
        })
    assert rejected.value.code == "production_mount_manifest_invalid"


def _profile(evidence, *, cutover_id=None):
    runtime = evidence.runtime
    return SimpleNamespace(
        authority_state=SimpleNamespace(value="active"),
        authority=runtime.authority,
        repository_epoch=runtime.repository_epoch,
        cutover_id=evidence.cutover_id if cutover_id is None else cutover_id,
        source_admission_revision=runtime.source_admission_revision,
        repository_revision=runtime.repository_revision,
        state_revision=runtime.state_revision,
        runtime_generation=runtime.runtime_generation,
        code_digest=runtime.code_digest,
    )


def _joined_row(evidence):
    return (
        evidence.cutover_id,
        evidence.transaction_id,
        evidence.receipt_kind,
        evidence.mounted_at,
        evidence.install.transaction_id,
        evidence.install.receipt_kind,
        evidence.install.committed_at,
        evidence.install.manifest_sha256,
        evidence.coverage.transaction_id,
        evidence.coverage.receipt_kind,
        evidence.coverage.committed_at,
        evidence.coverage.manifest_sha256,
        evidence.activation.audit_transaction_id,
        evidence.activation.audit_manifest_sha256,
        evidence.activation.transaction_id,
        evidence.activation.manifest_sha256,
        evidence.runtime.repository_epoch,
        evidence.runtime.runtime_generation,
        evidence.runtime.code_digest,
        evidence.manifest_bytes,
        evidence.manifest_sha256,
        evidence.receipt_kind,
        evidence.mounted_at,
    )


def test_v13_mount_target_identity_accepts_only_current_target() -> None:
    current_version = module.MIGRATIONS[-1].version
    current_fingerprint = module._expected_schema_fingerprint()
    assert current_version == module._SCHEMA_V13_VERSION
    assert not module._current_v11_identity_is_frozen()
    assert not module._current_v12_identity_is_frozen()
    assert module._current_v13_identity_is_frozen()
    assert module._mount_target_identity_is_compatible(
        current_version,
        current_fingerprint,
    )
    assert not module._mount_target_identity_is_compatible(
        module._SCHEMA_V8_VERSION,
        module._SCHEMA_V8_FINGERPRINT,
    )
    assert not module._mount_target_identity_is_compatible(
        module._SCHEMA_V9_VERSION,
        module._SCHEMA_V9_FINGERPRINT,
    )
    assert not module._mount_target_identity_is_compatible(
        module._SCHEMA_V10_VERSION,
        module._SCHEMA_V10_FINGERPRINT,
    )
    assert not module._mount_target_identity_is_compatible(
        module._SCHEMA_V11_VERSION,
        module._SCHEMA_V11_FINGERPRINT,
    )
    assert not module._mount_target_identity_is_compatible(
        module._SCHEMA_V8_VERSION - 1,
        module._expected_schema_fingerprint(module._SCHEMA_V8_VERSION - 1),
    )
    assert not module._mount_target_identity_is_compatible(
        current_version + 1,
        current_fingerprint,
    )
    assert not module._mount_target_identity_is_compatible(
        module._SCHEMA_V8_VERSION,
        "0" * 64,
    )
    assert not module._mount_target_identity_is_compatible(
        current_version,
        module._SCHEMA_V8_FINGERPRINT,
    )


@pytest.mark.parametrize(
    "drift",
    (
        "registry_version",
        "v8_name",
        "v8_checksum",
        "v8_fingerprint",
        "v8_root",
        "v9_name",
        "v9_checksum",
        "v9_fingerprint",
        "v10_name",
        "v10_checksum",
        "v10_fingerprint",
    ),
)
def test_historical_mount_compatibility_fails_closed_on_prefix_drift(
    monkeypatch,
    drift,
) -> None:
    registry = [
        SimpleNamespace(
            version=item.version,
            name=item.name,
            checksum=item.checksum,
        )
        for item in module.MIGRATIONS[:module._SCHEMA_V10_VERSION]
    ]
    expected_fingerprint = module._expected_schema_fingerprint
    expected_root = module._expected_migration_ledger_sha256
    if drift == "registry_version":
        registry[-1].version += 1
    elif drift == "v8_name":
        registry[module._SCHEMA_V8_VERSION - 1].name += "-changed"
    elif drift == "v8_checksum":
        registry[module._SCHEMA_V8_VERSION - 1].checksum = "0" * 64
    elif drift == "v8_fingerprint":
        monkeypatch.setattr(
            module,
            "_expected_schema_fingerprint",
            lambda version=None: (
                "0" * 64
                if version == module._SCHEMA_V8_VERSION
                else expected_fingerprint(version)
            ),
        )
    elif drift == "v8_root":
        monkeypatch.setattr(
            module,
            "_expected_migration_ledger_sha256",
            lambda version=None: (
                b"\0" * 32
                if version == module._SCHEMA_V8_VERSION
                else expected_root(version)
            ),
        )
    elif drift == "v9_name":
        registry[module._SCHEMA_V9_VERSION - 1].name += "-changed"
    elif drift == "v9_checksum":
        registry[module._SCHEMA_V9_VERSION - 1].checksum = "0" * 64
    elif drift == "v9_fingerprint":
        monkeypatch.setattr(
            module,
            "_expected_schema_fingerprint",
            lambda version=None: (
                "0" * 64
                if version == module._SCHEMA_V9_VERSION
                else expected_fingerprint(version)
            ),
        )
    elif drift == "v10_name":
        registry[module._SCHEMA_V10_VERSION - 1].name += "-changed"
    elif drift == "v10_checksum":
        registry[module._SCHEMA_V10_VERSION - 1].checksum = "0" * 64
    elif drift == "v10_fingerprint":
        monkeypatch.setattr(
            module,
            "_expected_schema_fingerprint",
            lambda version=None: (
                "0" * 64
                if version == module._SCHEMA_V10_VERSION
                else expected_fingerprint(version)
            ),
        )
    else:
        raise AssertionError("unhandled drift")
    monkeypatch.setattr(module, "MIGRATIONS", tuple(registry))

    assert not module._mount_target_identity_is_compatible(
        module._SCHEMA_V8_VERSION,
        module._SCHEMA_V8_FINGERPRINT,
    )


@pytest.mark.parametrize(
    "drift",
    (
        "registry_length",
        "v11_name",
        "v11_checksum",
        "v11_fingerprint",
        "v11_root",
    ),
)
def test_frozen_v11_mount_identity_fails_closed_on_registry_drift(
    monkeypatch,
    drift,
) -> None:
    registry = [
        SimpleNamespace(
            version=item.version,
            name=item.name,
            checksum=item.checksum,
        )
        for item in module.MIGRATIONS[: module._SCHEMA_V11_VERSION]
    ]
    expected_fingerprint = module._expected_schema_fingerprint
    expected_root = module._expected_migration_ledger_sha256
    if drift == "registry_length":
        registry.pop()
    elif drift == "v11_name":
        registry[-1].name += "-changed"
    elif drift == "v11_checksum":
        registry[-1].checksum = "0" * 64
    elif drift == "v11_fingerprint":
        monkeypatch.setattr(
            module,
            "_expected_schema_fingerprint",
            lambda version=None: (
                "0" * 64
                if version == module._SCHEMA_V11_VERSION
                else expected_fingerprint(version)
            ),
        )
    elif drift == "v11_root":
        monkeypatch.setattr(
            module,
            "_expected_migration_ledger_sha256",
            lambda version=None: (
                b"\0" * 32
                if version == module._SCHEMA_V11_VERSION
                else expected_root(version)
            ),
        )
    else:
        raise AssertionError("unhandled drift")
    monkeypatch.setattr(module, "MIGRATIONS", tuple(registry))

    assert not module._current_v11_identity_is_frozen()
    assert not module._mount_target_identity_is_compatible(
        module._SCHEMA_V11_VERSION,
        module._SCHEMA_V11_FINGERPRINT,
    )


@pytest.mark.parametrize(
    "drift",
    (
        "registry_length",
        "v13_version",
        "v13_name",
        "v13_checksum",
        "v13_fingerprint",
        "v13_root",
    ),
)
def test_current_v13_mount_identity_fails_closed_on_registry_drift(
    monkeypatch,
    drift,
) -> None:
    registry = [
        SimpleNamespace(
            version=item.version,
            name=item.name,
            checksum=item.checksum,
        )
        for item in module.MIGRATIONS
    ]
    expected_fingerprint = module._expected_schema_fingerprint
    expected_root = module._expected_migration_ledger_sha256
    if drift == "registry_length":
        registry.pop()
    elif drift == "v13_version":
        registry[-1].version += 1
    elif drift == "v13_name":
        registry[-1].name += "-changed"
    elif drift == "v13_checksum":
        registry[-1].checksum = "0" * 64
    elif drift == "v13_fingerprint":
        monkeypatch.setattr(
            module,
            "_expected_schema_fingerprint",
            lambda version=None: (
                "0" * 64
                if version == module._SCHEMA_V13_VERSION
                else expected_fingerprint(version)
            ),
        )
    elif drift == "v13_root":
        monkeypatch.setattr(
            module,
            "_expected_migration_ledger_sha256",
            lambda version=None: (
                b"\0" * 32
                if version == module._SCHEMA_V13_VERSION
                else expected_root(version)
            ),
        )
    else:
        raise AssertionError("unhandled drift")
    monkeypatch.setattr(module, "MIGRATIONS", tuple(registry))

    assert not module._current_v13_identity_is_frozen()
    assert not module._mount_target_identity_is_compatible(
        module._SCHEMA_V13_VERSION,
        module._SCHEMA_V13_FINGERPRINT,
    )


def test_snapshot_binding_compares_top_level_cutover_id_not_runtime_field(
    monkeypatch,
) -> None:
    """Regression: BusinessCutoverMountRuntimeIdentity has no cutover_id."""

    evidence = _mount_evidence()
    monkeypatch.setattr(module, "_mount_evidence_from_row", lambda row: evidence)
    monkeypatch.setattr(module, "_linked_mount_rows_exact", lambda *_: True)
    monkeypatch.setattr(
        module,
        "MIGRATIONS",
        (SimpleNamespace(version=evidence.runtime.target_schema_version),),
    )
    monkeypatch.setattr(
        module,
        "_expected_schema_fingerprint",
        lambda: evidence.runtime.target_schema_fingerprint,
    )
    monkeypatch.setattr(
        module,
        "_mount_target_identity_is_compatible",
        lambda *_args: True,
    )
    profile = _profile(evidence)
    # Mount evidence is a static cutover seal. Mutable runtime revisions may
    # advance after every successful production write and must come from the
    # fresh profile, not be frozen to their values at mount time.
    profile.repository_revision += 9
    profile.state_revision += 4
    binding = _binding_from_snapshot(
        object(),
        profile,
        _joined_row(evidence),
    )
    assert _require_mounted_binding(binding) is binding
    assert binding.evidence.cutover_id == evidence.cutover_id
    assert binding.evidence.repository_revision == profile.repository_revision
    assert binding.evidence.control_revision == profile.state_revision

    with pytest.raises(SQLiteProductionAuthorityError) as rejected:
        _binding_from_snapshot(
            object(),
            _profile(evidence, cutover_id="f" * 64),
            _joined_row(evidence),
        )
    assert rejected.value.code == "production_mount_authorization_invalid"


@pytest.mark.parametrize(
    "profile_change",
    (
        {"repository_epoch": "f" * 64},
        {"runtime_generation": "other-runtime"},
        {"code_digest": b"z" * 32},
    ),
)
def test_runtime_identity_mismatch_cannot_be_hidden_by_valid_mount_manifest(
    monkeypatch,
    profile_change,
) -> None:
    evidence = _mount_evidence()
    monkeypatch.setattr(module, "_mount_evidence_from_row", lambda row: evidence)
    monkeypatch.setattr(module, "_linked_mount_rows_exact", lambda *_: True)
    monkeypatch.setattr(
        module,
        "MIGRATIONS",
        (SimpleNamespace(version=evidence.runtime.target_schema_version),),
    )
    monkeypatch.setattr(
        module,
        "_expected_schema_fingerprint",
        lambda: evidence.runtime.target_schema_fingerprint,
    )
    profile = _profile(evidence)
    for name, value in profile_change.items():
        setattr(profile, name, value)
    with pytest.raises(SQLiteProductionAuthorityError) as rejected:
        _binding_from_snapshot(object(), profile, _joined_row(evidence))
    assert rejected.value.code == "production_mount_authorization_invalid"


@pytest.mark.parametrize(
    "revision_name",
    ("repository_revision", "state_revision"),
)
def test_runtime_revision_may_advance_but_cannot_retreat_behind_mount(
    monkeypatch,
    revision_name,
) -> None:
    evidence = _mount_evidence()
    monkeypatch.setattr(module, "_mount_evidence_from_row", lambda row: evidence)
    monkeypatch.setattr(module, "_linked_mount_rows_exact", lambda *_: True)
    monkeypatch.setattr(
        module,
        "MIGRATIONS",
        (SimpleNamespace(version=evidence.runtime.target_schema_version),),
    )
    monkeypatch.setattr(
        module,
        "_expected_schema_fingerprint",
        lambda: evidence.runtime.target_schema_fingerprint,
    )
    profile = _profile(evidence)
    setattr(
        profile,
        revision_name,
        getattr(evidence.runtime, revision_name) - 1,
    )
    with pytest.raises(SQLiteProductionAuthorityError) as rejected:
        _binding_from_snapshot(object(), profile, _joined_row(evidence))
    assert rejected.value.code == "production_mount_authorization_invalid"


def test_plain_evidence_and_forged_wrapper_are_not_fresh_capabilities() -> None:
    evidence = _mount_evidence()
    with pytest.raises(SQLiteProductionAuthorityError) as rejected:
        _require_mounted_binding(evidence)
    assert rejected.value.code == "production_mount_authorization_invalid"
    with pytest.raises(SQLiteProductionAuthorityError) as rejected:
        module.ProductionMountedAuthorityBinding(evidence, _issuer=object())
    assert rejected.value.code == "production_mount_reader_issuer_invalid"


def test_scheduler_reader_rejects_partial_inflight_projection() -> None:
    timestamp = _mount_evidence().mounted_at
    zero = b"\x00" * 32

    class _Connection:
        def execute(self, sql):
            if "worker_ledger_head" in sql:
                row = (1, 0, zero, zero, timestamp)
            elif "outbox_dispatch_head" in sql:
                row = (1, 0, None, None, None, 1, 0, zero, zero, timestamp)
            else:
                raise AssertionError("unexpected scheduler query")
            return SimpleNamespace(fetchone=lambda: row)

    with pytest.raises(SQLiteProductionAuthorityError) as rejected:
        _read_scheduler_head_bindings(_Connection())
    assert rejected.value.code == "production_outbox_dispatch_head_invalid"


__all__ = ()
