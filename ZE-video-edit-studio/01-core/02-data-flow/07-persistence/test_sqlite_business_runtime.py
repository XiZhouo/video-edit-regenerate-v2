"""Focused admission evidence for the private mounted SQLite runtime."""

from __future__ import annotations

import ast
import dataclasses
import inspect
import os
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


from asset_reference_projection import verify_asset_reference_projection  # noqa: E402
from business_codec import decode_payload_bounded  # noqa: E402
from business_snapshot import verify_business_snapshot  # noqa: E402
import sqlite_business_audit as business_audit  # noqa: E402
from receipt_namespace_contract import ReceiptNamespaceEntry  # noqa: E402
from receipt_namespace_integrity import _receipt_namespace_sha256  # noqa: E402
from business_transaction_contract import (  # noqa: E402
    MAX_ATOMIC_MANIFEST_BYTES,
    MAX_ATOMIC_MANIFEST_NODES,
)
from sqlite_business_runtime import (  # noqa: E402
    BusinessAuthorityState,
    BusinessRuntimeAccess,
    BusinessRuntimeProfile,
    _validate_business_runtime_connection,
    inspect_business_runtime,
)
from sqlite_business_audit import (  # noqa: E402
    periodic_business_runtime_full_audit,
    startup_business_runtime_full_audit,
)
from sqlite_business_test_support import (  # noqa: E402
    TEST_BASELINE_MANIFEST_SCHEMA,
    BusinessTestBaseline,
    _install_mounted_test_authority,
    create_active_business_database,
)
from sqlite_kernel import SQLiteKernelError, SqliteDatabase  # noqa: E402
from sqlite_production_authority import (  # noqa: E402
    _read_production_mounted_authority,
)
from sqlite_schema import (  # noqa: E402
    BUSINESS_TRANSACTION_KIND,
    BUSINESS_TRANSACTION_PREFIX,
    LOCAL_REVISION_MIGRATION,
    MIGRATIONS,
    PRODUCTION_AUTHORITY_WORKER_MIGRATION,
    _expected_migration_ledger_sha256,
)


def _assert_kernel_error(callable_, code: str) -> SQLiteKernelError:
    with pytest.raises(SQLiteKernelError) as raised:
        callable_()
    assert raised.value.code == code
    return raised.value


def _raw(database) -> sqlite3.Connection:
    connection = sqlite3.connect(str(database.path), isolation_level=None)
    connection.row_factory = sqlite3.Row
    return connection


def test_full_audit_receipt_namespace_uses_the_canonical_streaming_root(
    tmp_path: Path,
) -> None:
    baseline = create_active_business_database(tmp_path / "receipt-root.sqlite3")
    with _raw(baseline.database) as owned:
        rows = owned.execute(
            "SELECT transaction_id, kind, committed_at "
            "FROM transaction_receipts ORDER BY transaction_id COLLATE BINARY"
        ).fetchall()
        expected = _receipt_namespace_sha256(
            tuple(
                ReceiptNamespaceEntry(
                    row["transaction_id"],
                    row["kind"],
                    row["committed_at"],
                )
                for row in rows
            ),
            expected_count=len(rows),
        )
        assert business_audit._read_receipt_namespace_identity(owned) == expected


def test_full_audit_connection_observation_keeps_all_roots_in_one_snapshot(
    tmp_path: Path,
) -> None:
    baseline = create_active_business_database(tmp_path / "audit-observation.sqlite3")
    connection = None
    try:
        connection, _filesystem = baseline.database._open_unvalidated()
        connection.execute("BEGIN")
        observed = business_audit._audit_business_runtime_connection(
            baseline.database,
            connection,
            access=BusinessRuntimeAccess.READER,
        )
        assert observed.profile == baseline.profile
        assert observed.user_version == MIGRATIONS[-1].version
        assert len(observed.schema_fingerprint) == 64
        assert observed.migration_ledger_count == MIGRATIONS[-1].version
        assert observed.migration_ledger_sha256 == (
            _expected_migration_ledger_sha256()
        )
        assert observed.receipt_namespace_count >= 1
        assert len(observed.receipt_namespace_sha256) == 32
    finally:
        if connection is not None:
            if connection.in_transaction:
                connection.rollback()
            connection.close()


def test_full_audit_rejects_business_commit_without_atomic_receipt(
    tmp_path: Path,
) -> None:
    baseline = create_active_business_database(
        tmp_path / "missing-baseline-atomic-receipt.sqlite3"
    )
    with _raw(baseline.database) as connection:
        # Model an offline/tampering writer that ignored the immutable-history
        # trigger.  The remaining business commit, runtime and current heads
        # are otherwise untouched and therefore exercise the reverse closure.
        trigger = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
            ("atomic_business_write_receipts_no_delete",),
        ).fetchone()
        assert trigger is not None and type(trigger["sql"]) is str
        connection.execute(
            "DROP TRIGGER atomic_business_write_receipts_no_delete"
        )
        connection.execute(
            "DELETE FROM atomic_business_write_receipts WHERE commit_id=?",
            (baseline.commit_id,),
        )
        connection.execute(trigger["sql"])
    _assert_kernel_error(
        lambda: startup_business_runtime_full_audit(baseline.database),
        "sqlite_business_atomic_receipt_missing",
    )


def test_full_audit_rejects_noncanonical_ordinary_receipt_time(
    tmp_path: Path,
) -> None:
    baseline = create_active_business_database(tmp_path / "receipt-time-all.sqlite3")
    with _raw(baseline.database) as connection:
        connection.execute(
            "INSERT INTO transaction_receipts(transaction_id, kind, committed_at) "
            "VALUES ('ordinary:bad-time', 'ordinary', 'not-a-time')"
        )
    _assert_kernel_error(
        lambda: startup_business_runtime_full_audit(baseline.database),
        "sqlite_receipt_namespace_invalid",
    )


def test_active_baseline_is_canonical_but_generic_kernel_stays_unmounted(
    tmp_path: Path,
) -> None:
    baseline = create_active_business_database(tmp_path / "business.sqlite3")
    assert type(baseline) is BusinessTestBaseline
    assert type(baseline.profile) is BusinessRuntimeProfile
    assert baseline.profile.authority == "sqlite"
    assert baseline.profile.authority_state is BusinessAuthorityState.ACTIVE
    assert baseline.profile.repository_revision == 2
    # The empty V12 mount reserves revision 1; the first ordinary runtime
    # baseline consumes revision 2.  State revision 2 is the mounted/runtime
    # control fence used by its outer atomic receipt.
    assert baseline.profile.state_revision == 2
    assert verify_business_snapshot(baseline.snapshot) is baseline.snapshot
    assert (
        verify_asset_reference_projection(baseline.asset_projection)
        is baseline.asset_projection
    )
    assert baseline.asset_projection.reference_count == 0
    manifest = decode_payload_bounded(
        baseline.manifest_bytes,
        MAX_ATOMIC_MANIFEST_BYTES,
        MAX_ATOMIC_MANIFEST_NODES,
    )
    assert manifest["schema"] == TEST_BASELINE_MANIFEST_SCHEMA
    assert manifest["commit"]["commit_kind"] == "baseline"
    assert manifest["after"]["business_root_sha256"] == (
        baseline.business_root_sha256.hex()
    )
    assert baseline.before_business_head().expected_last_commit_id == baseline.commit_id

    binding = baseline.profile.to_authority_binding()
    assert binding.authority == "sqlite"
    assert binding.authority_epoch == baseline.profile.repository_epoch
    assert binding.authority_revision == baseline.profile.repository_revision
    assert binding.control_revision == baseline.profile.state_revision

    connection = _raw(baseline.database)
    try:
        assert connection.execute(
            "SELECT count(*) FROM business_commits"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT count(*) FROM document_commit_heads"
        ).fetchone()[0] == 1
        assert tuple(
            connection.execute(
                "SELECT transaction_id, kind FROM transaction_receipts "
                "WHERE transaction_id = ?",
                (baseline.transaction_id,),
            ).fetchone()
        ) == (baseline.transaction_id, BUSINESS_TRANSACTION_KIND)
    finally:
        connection.close()

    # The shared kernel validator is intentionally still legacy+empty only.
    _assert_kernel_error(
        baseline.database.inspect_profile,
        "sqlite_authority_marker_invalid",
    )


def test_current_v13_fixture_mints_exact_production_authority(
    tmp_path: Path,
) -> None:
    """Current fixtures use canonical evidence and the real four-bind chain."""

    baseline = create_active_business_database(
        tmp_path / "canonical-v13-mount-authority.sqlite3"
    )
    binding = _read_production_mounted_authority(baseline.database)
    assert binding.evidence.cutover_id == baseline.profile.cutover_id
    assert binding.evidence.target_schema_version == MIGRATIONS[-1].version


def test_empty_runtime_revision_floor_is_one_in_fast_and_full_fences(
    tmp_path: Path,
) -> None:
    database = SqliteDatabase((tmp_path / "empty-revision.sqlite3").resolve())
    database.initialize()
    connection = _raw(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE kernel_repository SET authority='sqlite' WHERE singleton=1"
        )
        _install_mounted_test_authority(
            connection,
            repository_epoch="e" * 64,
            source_admission_revision=1,
            runtime_generation="empty-runtime",
            code_digest=b"d" * 32,
        )
        trigger_names = (
            "repository_runtime_control_revision_guard",
            "repository_runtime_control_local_closure_update_guard",
            "repository_runtime_control_v8_atomic_finalization_guard",
        )
        trigger_rows = connection.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND "
            "name IN (?,?,?)",
            trigger_names,
        ).fetchall()
        original_triggers = {row["name"]: row["sql"] for row in trigger_rows}
        assert set(original_triggers) == set(trigger_names)
        for trigger_name in trigger_names:
            connection.execute('DROP TRIGGER "' + trigger_name + '"')
        connection.execute(
            "UPDATE repository_runtime_control SET repository_revision=999 "
            "WHERE singleton=1"
        )
        for trigger_name in trigger_names:
            connection.execute(original_triggers[trigger_name])
        connection.commit()
    finally:
        connection.close()

    owned, _filesystem = database._open_unvalidated()
    try:
        _assert_kernel_error(
            lambda: _validate_business_runtime_connection(
                database,
                owned,
                access=BusinessRuntimeAccess.WRITER,
            ),
            "sqlite_business_runtime_revision_invalid",
        )
    finally:
        owned.close()
    _assert_kernel_error(
        lambda: startup_business_runtime_full_audit(database),
        "sqlite_business_runtime_revision_invalid",
    )


@pytest.mark.parametrize(
    "phase_revision,phase",
    ((1, "installed"), (2, "assets_covered")),
)
def test_full_audit_can_run_before_activation_without_requiring_future_mount(
    tmp_path: Path,
    phase_revision: int,
    phase: str,
) -> None:
    database = SqliteDatabase(
        (tmp_path / ("pre-activation-%d.sqlite3" % phase_revision)).resolve()
    )
    database.initialize()
    connection = _raw(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE kernel_repository SET authority='sqlite' WHERE singleton=1"
        )
        _install_mounted_test_authority(
            connection,
            repository_epoch="e" * 64,
            source_admission_revision=1,
            runtime_generation="pre-activation-runtime",
            code_digest=b"p" * 32,
        )
        controlled_tables = (
            "business_cutover_head",
            "repository_runtime_control",
        )
        placeholders = ",".join("?" for _table in controlled_tables)
        triggers = connection.execute(
            "SELECT name,sql FROM sqlite_schema WHERE type='trigger' AND "
            "tbl_name IN (" + placeholders + ") ORDER BY name",
            controlled_tables,
        ).fetchall()
        for trigger in triggers:
            connection.execute('DROP TRIGGER "' + trigger["name"] + '"')
        clear_coverage = (
            "coverage_transaction_id=NULL,coverage_manifest_sha256=NULL,"
            if phase_revision == 1
            else ""
        )
        connection.execute(
            "UPDATE business_cutover_head SET state_revision=?,"
            "phase_revision=?,phase=?,authority_state='read_only',"
            + clear_coverage
            + "audit_transaction_id=NULL,audit_manifest_sha256=NULL,"
            "activation_transaction_id=NULL,activation_manifest_sha256=NULL,"
            "mount_transaction_id=NULL,mount_manifest_sha256=NULL "
            "WHERE singleton=1",
            (phase_revision, phase_revision, phase),
        )
        connection.execute(
            "UPDATE repository_runtime_control SET authority_state='read_only',"
            "state_revision=1 WHERE singleton=1"
        )
        for trigger in triggers:
            connection.execute(trigger["sql"])
        connection.commit()
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()
    database._finalize_committed_storage()

    profile = startup_business_runtime_full_audit(database)
    assert profile.authority_state is BusinessAuthorityState.READ_ONLY
    assert profile.repository_revision == 1


def test_full_audit_rejects_main_file_replacement_during_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = create_active_business_database(tmp_path / "pinned.sqlite3")
    replacement = create_active_business_database(
        tmp_path / "replacement.sqlite3",
        repository_epoch="a" * 64,
    )
    backup_path = tmp_path / "pinned-original.sqlite3"
    original_verify = business_audit._verify_business_revision_history

    def replace_after_snapshot(connection, profile) -> None:
        original_verify(connection, profile)
        os.replace(baseline.database.path, backup_path)
        os.replace(replacement.database.path, baseline.database.path)

    monkeypatch.setattr(
        business_audit,
        "_verify_business_revision_history",
        replace_after_snapshot,
    )
    _assert_kernel_error(
        lambda: startup_business_runtime_full_audit(baseline.database),
        "sqlite_business_storage_identity_changed",
    )


def test_full_audit_publish_fence_rejects_open_time_inode_aba(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = create_active_business_database(
        tmp_path / "aba.sqlite3",
        repository_epoch="a" * 64,
    )
    replacement = create_active_business_database(
        tmp_path / "aba-replacement.sqlite3",
        repository_epoch="c" * 64,
    )
    hidden_baseline = tmp_path / "aba-hidden-a.sqlite3"
    hidden_replacement = tmp_path / "aba-hidden-b.sqlite3"
    original_open = baseline.database._open_unvalidated
    calls = 0

    def open_with_one_aba(*, allow_create: bool = False):
        nonlocal calls
        calls += 1
        if calls != 1:
            return original_open(allow_create=allow_create)
        os.replace(baseline.database.path, hidden_baseline)
        os.replace(replacement.database.path, baseline.database.path)
        connection, filesystem = original_open(allow_create=allow_create)
        os.replace(baseline.database.path, hidden_replacement)
        os.replace(hidden_baseline, baseline.database.path)
        return connection, filesystem

    monkeypatch.setattr(
        baseline.database,
        "_open_unvalidated",
        open_with_one_aba,
    )
    _assert_kernel_error(
        lambda: startup_business_runtime_full_audit(baseline.database),
        "sqlite_business_full_audit_publish_changed",
    )
    assert calls == 2


@pytest.mark.parametrize(
    "state",
    (
        BusinessAuthorityState.ACTIVE,
        BusinessAuthorityState.READ_ONLY,
        BusinessAuthorityState.REPAIR_REQUIRED,
    ),
)
def test_reader_states_are_explicit_and_only_active_can_issue_writer_binding(
    tmp_path: Path,
    state: BusinessAuthorityState,
) -> None:
    baseline = create_active_business_database(
        tmp_path / (state.value + ".sqlite3"),
        authority_state=state,
    )
    reader = inspect_business_runtime(
        baseline.database,
        access=BusinessRuntimeAccess.READER,
    )
    assert reader.authority_state is state
    if state is BusinessAuthorityState.ACTIVE:
        writer = inspect_business_runtime(
            baseline.database,
            access=BusinessRuntimeAccess.WRITER,
        )
        assert writer.authority_state is BusinessAuthorityState.ACTIVE
        assert writer.to_authority_binding().authority_state == "active"
    else:
        _assert_kernel_error(
            lambda: inspect_business_runtime(
                baseline.database,
                access=BusinessRuntimeAccess.WRITER,
            ),
            "sqlite_business_authority_state_forbidden",
        )
        _assert_kernel_error(
            reader.to_authority_binding,
            "sqlite_business_authority_state_forbidden",
        )


def test_profile_is_frozen_and_access_does_not_coerce_strings(tmp_path: Path) -> None:
    baseline = create_active_business_database(tmp_path / "frozen.sqlite3")
    with pytest.raises(dataclasses.FrozenInstanceError):
        baseline.profile.repository_revision = 9  # type: ignore[misc]
    _assert_kernel_error(
        lambda: inspect_business_runtime(
            baseline.database,
            access="reader",  # type: ignore[arg-type]
        ),
        "sqlite_business_access_invalid",
    )


def test_transaction_scoped_validator_neither_ends_nor_mutates_owner_snapshot(
    tmp_path: Path,
) -> None:
    baseline = create_active_business_database(tmp_path / "owned.sqlite3")
    connection, _filesystem = baseline.database._open_unvalidated()
    try:
        connection.execute("BEGIN IMMEDIATE")
        before = tuple(
            connection.execute(
                "SELECT authority_state, repository_revision, state_revision, "
                "updated_at FROM repository_runtime_control"
            ).fetchone()
        )
        profile = _validate_business_runtime_connection(
            baseline.database,
            connection,
            access=BusinessRuntimeAccess.WRITER,
        )
        assert profile.repository_revision == 2
        assert connection.in_transaction is True
        after = tuple(
            connection.execute(
                "SELECT authority_state, repository_revision, state_revision, "
                "updated_at FROM repository_runtime_control"
            ).fetchone()
        )
        assert after == before
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()


def test_autocommit_validator_owns_and_releases_only_its_read_snapshot(
    tmp_path: Path,
) -> None:
    baseline = create_active_business_database(tmp_path / "autocommit.sqlite3")
    connection, _filesystem = baseline.database._open_unvalidated()
    try:
        assert connection.in_transaction is False
        profile = _validate_business_runtime_connection(
            baseline.database,
            connection,
            access=BusinessRuntimeAccess.READER,
        )
        assert profile.authority_state is BusinessAuthorityState.ACTIVE
        assert connection.in_transaction is False
    finally:
        connection.close()


def test_startup_and_periodic_full_audits_are_explicit_and_equivalent(
    tmp_path: Path,
) -> None:
    baseline = create_active_business_database(tmp_path / "full-audit.sqlite3")

    startup = startup_business_runtime_full_audit(baseline.database)
    periodic = periodic_business_runtime_full_audit(baseline.database)

    assert startup == periodic == baseline.profile
    # Keep inspect's pre-split behavior: diagnostics still request a full
    # startup audit rather than silently receiving only the fast fence.
    assert inspect_business_runtime(baseline.database) == startup


def test_transaction_fast_fence_has_no_full_audit_call_graph() -> None:
    import sqlite_business_reader as reader
    import sqlite_business_runtime as runtime
    import sqlite_business_transaction as transaction

    forbidden_calls = {
        "startup_business_runtime_full_audit",
        "periodic_business_runtime_full_audit",
        "_run_business_runtime_full_audit",
        "_verify_relational_integrity",
        "_verify_business_receipt_namespace",
    }
    for module in (reader, transaction):
        tree = ast.parse(inspect.getsource(module))
        calls = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert calls.isdisjoint(forbidden_calls)

    fast_source = inspect.getsource(runtime._validate_business_runtime_connection)
    assert "integrity_check" not in fast_source
    assert "foreign_key_check" not in fast_source
    assert "receipt_namespace" not in fast_source
    assert 'execute("PRAGMA foreign_key_check")' not in inspect.getsource(transaction)


def test_noncanonical_repository_marker_time_is_rejected(tmp_path: Path) -> None:
    baseline = create_active_business_database(tmp_path / "marker.sqlite3")
    connection = _raw(baseline.database)
    try:
        connection.execute(
            "UPDATE kernel_repository SET created_at='not-utc' WHERE singleton=1"
        )
    finally:
        connection.close()
    _assert_kernel_error(
        lambda: inspect_business_runtime(baseline.database),
        "sqlite_business_authority_invalid",
    )


def test_noncanonical_runtime_time_is_rejected_even_if_checks_were_bypassed(
    tmp_path: Path,
) -> None:
    baseline = create_active_business_database(tmp_path / "runtime-time.sqlite3")
    connection = _raw(baseline.database)
    try:
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute(
            "UPDATE repository_runtime_control "
            "SET state_revision=3, updated_at='not-utc' WHERE singleton=1"
        )
    finally:
        connection.close()
    _assert_kernel_error(
        lambda: inspect_business_runtime(baseline.database),
        "sqlite_business_runtime_control_invalid",
    )


@pytest.mark.parametrize(
    ("transaction_id", "kind"),
    (
        (BUSINESS_TRANSACTION_PREFIX + "a" * 32, "generic"),
        ("ordinary-receipt", BUSINESS_TRANSACTION_KIND),
    ),
)
def test_reserved_business_receipt_namespace_rejects_partial_collisions(
    tmp_path: Path,
    transaction_id: str,
    kind: str,
) -> None:
    baseline = create_active_business_database(
        tmp_path / ("receipt-" + kind + ".sqlite3")
    )
    connection = _raw(baseline.database)
    try:
        connection.execute(
            "INSERT INTO transaction_receipts(transaction_id, kind, committed_at) "
            "VALUES (?, ?, '2026-07-12T01:00:00.000Z')",
            (transaction_id, kind),
        )
    finally:
        connection.close()
    _assert_kernel_error(
        lambda: startup_business_runtime_full_audit(baseline.database),
        "sqlite_business_receipt_namespace_invalid",
    )


def test_reserved_receipt_time_must_match_its_immutable_business_commit(
    tmp_path: Path,
) -> None:
    baseline = create_active_business_database(
        tmp_path / "receipt-time.sqlite3"
    )
    connection = _raw(baseline.database)
    try:
        with pytest.raises(
            sqlite3.IntegrityError,
            match="production_receipt_history_append_only",
        ):
            connection.execute(
                "UPDATE transaction_receipts SET committed_at=? "
                "WHERE transaction_id=?",
                ("2026-07-12T00:00:00.001Z", baseline.transaction_id),
            )
    finally:
        connection.close()


def test_runtime_revision_cannot_advance_beyond_durable_commit_history(
    tmp_path: Path,
) -> None:
    baseline = create_active_business_database(
        tmp_path / "runtime-revision.sqlite3"
    )
    trigger_sql = next(
        statement
        for statement in LOCAL_REVISION_MIGRATION.statements
        if "CREATE TRIGGER repository_runtime_control_local_closure_update_guard"
        in statement
    )
    production_trigger_sql = next(
        statement
        for statement in PRODUCTION_AUTHORITY_WORKER_MIGRATION.statements
        if "CREATE TRIGGER repository_runtime_control_v8_atomic_finalization_guard"
        in statement
    )
    connection = _raw(baseline.database)
    try:
        # Model an offline/tampering write that bypassed the normal final
        # closure trigger, then restore the exact schema before admission.
        connection.execute(
            "DROP TRIGGER repository_runtime_control_local_closure_update_guard"
        )
        connection.execute(
            "DROP TRIGGER repository_runtime_control_v8_atomic_finalization_guard"
        )
        connection.execute(
            "UPDATE repository_runtime_control SET repository_revision=3 "
            "WHERE singleton=1"
        )
        connection.execute(trigger_sql)
        connection.execute(production_trigger_sql)
    finally:
        connection.close()
    _assert_kernel_error(
        lambda: inspect_business_runtime(baseline.database),
        "sqlite_business_runtime_revision_invalid",
    )


@pytest.mark.parametrize("forged_revision", (1, 3))
def test_full_audit_allows_only_the_v12_empty_install_revision_two_origin(
    tmp_path: Path,
    forged_revision: int,
) -> None:
    baseline = create_active_business_database(
        tmp_path / ("revision-origin-%d.sqlite3" % forged_revision)
    )
    connection = _raw(baseline.database)
    controlled_tables = ("business_commits", "repository_runtime_control")
    placeholders = ",".join("?" for _table in controlled_tables)
    triggers = connection.execute(
        "SELECT name,sql FROM sqlite_schema WHERE type='trigger' AND "
        "tbl_name IN (" + placeholders + ") ORDER BY name",
        controlled_tables,
    ).fetchall()
    try:
        connection.execute("BEGIN IMMEDIATE")
        for trigger in triggers:
            connection.execute('DROP TRIGGER "' + trigger["name"] + '"')
        connection.execute(
            "UPDATE business_commits SET repository_revision=? "
            "WHERE commit_id=?",
            (forged_revision, baseline.commit_id),
        )
        connection.execute(
            "UPDATE repository_runtime_control SET repository_revision=? "
            "WHERE singleton=1",
            (forged_revision,),
        )
        for trigger in triggers:
            connection.execute(trigger["sql"])
        connection.commit()
        assert connection.execute("PRAGMA foreign_key_check").fetchone() is None
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()

    _assert_kernel_error(
        lambda: startup_business_runtime_full_audit(baseline.database),
        "sqlite_business_revision_history_invalid",
    )


def test_foreign_key_check_rejects_structural_orphan(tmp_path: Path) -> None:
    baseline = create_active_business_database(tmp_path / "foreign-key.sqlite3")
    connection = _raw(baseline.database)
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            """
            INSERT INTO entities(
                project_id, document_id, entity_type, entity_id,
                entity_version, deleted, codec_id, codec_version,
                snapshot_format_version, snapshot_bytes, snapshot_sha256
            ) VALUES (
                'missing-project', 'missing-document', 'card', 'orphan',
                1, 0, 'json', 1, 1, x'7b7d', ?
            )
            """,
            (b"x" * 32,),
        )
    finally:
        connection.close()
    _assert_kernel_error(
        lambda: startup_business_runtime_full_audit(baseline.database),
        "sqlite_business_foreign_key_violation",
    )


def test_full_audit_detects_a_corrupt_history_btree_page(tmp_path: Path) -> None:
    baseline = create_active_business_database(tmp_path / "page-corrupt.sqlite3")
    connection, _filesystem = baseline.database._open_unvalidated()
    try:
        page_size = connection.execute("PRAGMA page_size").fetchone()[0]
        root_page = connection.execute(
            "SELECT rootpage FROM sqlite_schema "
            "WHERE type='table' AND name='business_operation_targets'"
        ).fetchone()[0]
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()

    with baseline.database.path.open("r+b") as handle:
        handle.seek((root_page - 1) * page_size)
        assert handle.read(1) in (b"\x05", b"\x0d")
        handle.seek((root_page - 1) * page_size)
        handle.write(b"\x00")
        handle.flush()
        os.fsync(handle.fileno())

    _assert_kernel_error(
        lambda: periodic_business_runtime_full_audit(baseline.database),
        "sqlite_business_integrity_failed",
    )


def test_runtime_admission_does_not_decode_business_payloads(tmp_path: Path) -> None:
    baseline = create_active_business_database(tmp_path / "opaque.sqlite3")
    connection = _raw(baseline.database)
    try:
        connection.execute(
            "UPDATE documents SET snapshot_bytes=? WHERE project_id=?",
            (b"not-canonical-json", baseline.project_id),
        )
        connection.execute(
            "UPDATE business_commits SET manifest_bytes=? WHERE commit_id=?",
            (b"also-not-json", baseline.commit_id),
        )
    except sqlite3.IntegrityError:
        # business_commits is append-only by design; the Document row alone is
        # enough to prove this validator never invokes the snapshot codec.
        pass
    finally:
        connection.close()
    profile = inspect_business_runtime(baseline.database)
    assert profile.repository_revision == 2


def test_exact_schema_drift_fails_before_runtime_rows_are_trusted(tmp_path: Path) -> None:
    baseline = create_active_business_database(tmp_path / "schema-drift.sqlite3")
    connection = _raw(baseline.database)
    try:
        connection.execute("DROP INDEX event_outbox_repository_order")
    finally:
        connection.close()
    _assert_kernel_error(
        lambda: inspect_business_runtime(baseline.database),
        "sqlite_schema_drift",
    )


def test_runtime_module_has_no_authority_activation_or_public_exports() -> None:
    import sqlite_business_runtime as runtime

    assert runtime.__all__ == ()
    assert "UPDATE kernel_repository" not in inspect.getsource(runtime)
    assert "INSERT INTO repository_runtime_control" not in inspect.getsource(runtime)
