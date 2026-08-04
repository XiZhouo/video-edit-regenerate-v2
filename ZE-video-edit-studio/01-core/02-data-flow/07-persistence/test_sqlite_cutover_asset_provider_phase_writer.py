from __future__ import annotations

from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import copy
from pathlib import Path
import sqlite3

import pytest

import sqlite_cutover_asset_provider_phase_writer as writer_module
from business_cutover_contract import BusinessCutoverAdmission
from sqlite_cutover_asset_provider_phase_writer import (
    SQLiteCutoverAssetProviderPhaseResult,
    SQLiteCutoverAssetProviderPhaseStatus,
    SQLiteCutoverAssetProviderPhaseTestPoint,
    SQLiteCutoverAssetProviderPhaseWriter,
    _AssetProviderPhaseAuthorizer,
    _CUTOVER_ASSET_PROVIDER_PHASE_WRITER_CAPABILITY,
    _FRESH_ASSET_PROVIDER_PHASE_OBSERVATION_ISSUER,
    _FreshAssetProviderPhaseObservation,
    _reset_authorizer,
)
from sqlite_cutover_install_test_support import (
    _create_empty_cutover_install_fixture,
    _make_cutover_install_writer,
)
from sqlite_cutover_durable_audit_test_support import (
    create_canonical_cutover_phase_database,
)
from sqlite_cutover_durable_audit import SQLiteCutoverDurablePhase
from sqlite_cutover_phase_binding_reader import (
    SQLiteCutoverPhaseBindingReaderError,
    _read_cutover_phase_binding_prefix,
)
from sqlite_cutover_phase_head_writer import (
    SQLiteCutoverPhaseHeadWriter,
    _CUTOVER_PHASE_HEAD_WRITER_CAPABILITY,
)
from sqlite_kernel import SQLiteSettings, SqliteDatabase


def _ready_fixture(path: Path):
    fixture = _create_empty_cutover_install_fixture(path)
    installed = _make_cutover_install_writer(fixture).install(fixture.projection)
    assert installed.status.value == "installed_read_only"
    finalized = SQLiteCutoverPhaseHeadWriter(
        fixture.database,
        _capability=_CUTOVER_PHASE_HEAD_WRITER_CAPABILITY,
    ).finalize()
    assert finalized.status.value == "exact"
    assert finalized.phase.value == "installed_head"
    return fixture


def _database_now(fixture) -> str:
    connection = None
    try:
        connection, _filesystem = fixture.database._open_unvalidated()
        row = connection.execute(
            "SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')"
        ).fetchone()
        assert row is not None and type(row[0]) is str
        return row[0]
    finally:
        if connection is not None:
            connection.close()


def _observation(fixture, **changes):
    coverage = fixture.coverage_plan.coverage
    values = {
        "current_admission": fixture.plan.pending_admission,
        "source_freeze_sha256": fixture.repository.integrity.proof_sha256,
        "maintenance_scope": "catalog_ex_repository_ex",
        "maintenance_proof_sha256": fixture.repository.integrity.proof_sha256,
        "maintenance_observed_at": _database_now(fixture),
        "provider_protocol": "ze.business-cutover.asset-provider-checkpoint.v1",
        "provider_id": "test-asset-provider",
        "provider_generation": fixture.plan.install.runtime.runtime_generation,
        "provider_sequence": 1,
        "provider_checked_at": _database_now(fixture),
        "object_count": coverage.unique_blob_count,
        "total_bytes": coverage.unique_blob_bytes,
        "blob_root_sha256": coverage.blob_root_sha256,
        "_issuer": _FRESH_ASSET_PROVIDER_PHASE_OBSERVATION_ISSUER,
    }
    values.update(changes)
    return _FreshAssetProviderPhaseObservation(**values)


def _writer(fixture) -> SQLiteCutoverAssetProviderPhaseWriter:
    return SQLiteCutoverAssetProviderPhaseWriter(
        fixture.database,
        _capability=_CUTOVER_ASSET_PROVIDER_PHASE_WRITER_CAPABILITY,
    )


def _state(fixture) -> tuple[int, int, int]:
    connection = None
    try:
        connection, _filesystem = fixture.database._open_unvalidated()
        head = connection.execute(
            "SELECT phase_revision FROM business_cutover_head WHERE singleton=1"
        ).fetchone()
        assert head is not None
        binding_count = connection.execute(
            "SELECT count(*) FROM business_cutover_asset_provider_bindings"
        ).fetchone()[0]
        receipt_count = connection.execute(
            "SELECT count(*) FROM transaction_receipts"
        ).fetchone()[0]
        return head[0], binding_count, receipt_count
    finally:
        if connection is not None:
            connection.close()


def test_asset_provider_writer_rebuilds_and_commits_exact_binding(tmp_path: Path) -> None:
    fixture = _ready_fixture(tmp_path / "normal.sqlite3")
    writer = _writer(fixture)
    before = _state(fixture)
    assert writer.reconcile().status is SQLiteCutoverAssetProviderPhaseStatus.READY

    committed = writer.advance(_observation(fixture))

    assert committed.status is SQLiteCutoverAssetProviderPhaseStatus.EXACT
    assert committed.code == "cutover_asset_provider_phase_committed"
    assert committed.recovered_after_ack_loss is False
    assert committed.identity is not None
    assert committed.identity.phase.value == "assets_covered"
    assert _state(fixture) == (2, 1, before[2])
    connection = None
    try:
        connection, _filesystem = fixture.database._open_unvalidated()
        connection.execute("BEGIN")
        prefix = _read_cutover_phase_binding_prefix(connection)
        assert prefix.current_head.phase_revision == 2
        assert len(prefix.bindings) == 1
        evidence = prefix.bindings[0].projection.direct_evidence
        assert evidence.provider_id == "test-asset-provider"
        assert evidence.coverage.unique_blob_count == (
            fixture.coverage_plan.coverage.unique_blob_count
        )
    finally:
        if connection is not None:
            if connection.in_transaction:
                connection.rollback()
            connection.close()


def test_asset_provider_writer_exact_retry_is_idempotent(tmp_path: Path) -> None:
    fixture = _ready_fixture(tmp_path / "retry.sqlite3")
    writer = _writer(fixture)
    observation = _observation(fixture)
    assert writer.advance(observation).status is SQLiteCutoverAssetProviderPhaseStatus.EXACT
    state = _state(fixture)

    retried = writer.advance(observation)

    assert retried.status is SQLiteCutoverAssetProviderPhaseStatus.EXACT
    assert retried.recovered_after_ack_loss is True
    assert _state(fixture) == state


def test_asset_provider_writer_revalidates_live_lease_after_exact_retry_readback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _ready_fixture(tmp_path / "retry-lease-lost.sqlite3")
    writer = _writer(fixture)
    observation = _observation(fixture)
    assert writer.advance(observation).status is (
        SQLiteCutoverAssetProviderPhaseStatus.EXACT
    )
    original = writer_module._read_cutover_phase_binding_prefix

    def expire_after_readback(connection):
        prefix = original(connection)
        observation._expire(
            _issuer=_FRESH_ASSET_PROVIDER_PHASE_OBSERVATION_ISSUER
        )
        return prefix

    monkeypatch.setattr(
        writer_module,
        "_read_cutover_phase_binding_prefix",
        expire_after_readback,
    )

    result = writer.advance(observation)

    assert result.status is SQLiteCutoverAssetProviderPhaseStatus.CONFLICT
    assert result.code == "cutover_asset_provider_external_lease_not_live"
    assert _state(fixture)[:2] == (2, 1)


@pytest.mark.parametrize(
    "failure",
    (
        SQLiteCutoverPhaseBindingReaderError("injected-reader-corruption"),
        sqlite3.DatabaseError("injected-database-error"),
    ),
)
def test_asset_provider_exact_retry_does_not_disguise_reader_failure_as_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    fixture = _ready_fixture(tmp_path / (type(failure).__name__ + ".sqlite3"))
    writer = _writer(fixture)
    observation = _observation(fixture)
    assert writer.advance(observation).status is (
        SQLiteCutoverAssetProviderPhaseStatus.EXACT
    )

    def fail(_connection):
        raise failure

    monkeypatch.setattr(
        writer_module,
        "_read_cutover_phase_binding_prefix",
        fail,
    )

    result = writer.advance(observation)

    assert result.status is SQLiteCutoverAssetProviderPhaseStatus.CORRUPT
    assert result.code != "cutover_asset_provider_checkpoint_conflict"
    assert _state(fixture)[:2] == (2, 1)


def test_asset_provider_exact_retry_returns_the_last_matching_fresh_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _ready_fixture(tmp_path / "retry-final-identity.sqlite3")
    writer = _writer(fixture)
    observation = _observation(fixture)
    initial = writer.advance(observation)
    assert initial.status is SQLiteCutoverAssetProviderPhaseStatus.EXACT
    assert initial.identity is not None
    later_identity = copy.copy(initial.identity)
    later = SQLiteCutoverAssetProviderPhaseResult(
        SQLiteCutoverAssetProviderPhaseStatus.EXACT,
        "cutover_asset_provider_phase_final_fresh",
        later_identity,
    )
    results = iter((initial, later))
    monkeypatch.setattr(
        SQLiteCutoverAssetProviderPhaseWriter,
        "reconcile",
        lambda self: next(results),
    )

    retried = writer.advance(observation)

    assert retried.status is SQLiteCutoverAssetProviderPhaseStatus.EXACT
    assert retried.identity == later.identity
    assert retried.code == "cutover_asset_provider_phase_final_fresh"
    assert retried.recovered_after_ack_loss is True


def test_asset_provider_exact_retry_rejects_foreign_final_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _ready_fixture(tmp_path / "retry-foreign-final.sqlite3")
    writer = _writer(fixture)
    observation = _observation(fixture)
    initial = writer.advance(observation)
    assert initial.status is SQLiteCutoverAssetProviderPhaseStatus.EXACT
    foreign_database = create_canonical_cutover_phase_database(
        tmp_path / "foreign-phase.sqlite3",
        phase_revision=3,
    )
    foreign = SQLiteCutoverAssetProviderPhaseWriter(
        foreign_database,
        _capability=_CUTOVER_ASSET_PROVIDER_PHASE_WRITER_CAPABILITY,
    ).reconcile()
    assert foreign.status is SQLiteCutoverAssetProviderPhaseStatus.EXACT
    assert initial.identity is not None and foreign.identity is not None
    assert initial.identity.cutover_id != foreign.identity.cutover_id
    results = iter((initial, foreign))
    monkeypatch.setattr(
        SQLiteCutoverAssetProviderPhaseWriter,
        "reconcile",
        lambda self: next(results),
    )

    retried = writer.advance(observation)

    assert retried.status is SQLiteCutoverAssetProviderPhaseStatus.CONFLICT
    assert retried.code == "cutover_asset_provider_final_identity_changed"


def test_asset_provider_exact_retry_matches_binding_on_the_final_clone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _ready_fixture(tmp_path / "retry-clone-a.sqlite3")
    first_writer = _writer(fixture)
    first_observation = _observation(fixture, provider_sequence=1)
    clone_path = (tmp_path / "retry-clone-b.sqlite3").resolve()
    source = sqlite3.connect(str(fixture.database.path))
    clone = sqlite3.connect(str(clone_path))
    try:
        source.backup(clone)
    finally:
        clone.close()
        source.close()
    clone_path.chmod(0o600)
    clone_lock = Path(str(clone_path) + "-bootstrap.lock")
    clone_lock.touch(mode=0o600, exist_ok=False)
    clone_lock.chmod(0o600)
    clone_database = SqliteDatabase(
        clone_path,
        settings=fixture.database.settings,
    )
    clone_connection, _filesystem = clone_database._open_unvalidated()
    clone_connection.close()
    values = {
        name: getattr(first_observation, name)
        for name in _FreshAssetProviderPhaseObservation.__slots__
        if not name.startswith("_")
    }
    values["provider_sequence"] = 2
    other_observation = _FreshAssetProviderPhaseObservation(
        **values,
        _issuer=_FRESH_ASSET_PROVIDER_PHASE_OBSERVATION_ISSUER,
    )
    first = first_writer.advance(first_observation)
    other = SQLiteCutoverAssetProviderPhaseWriter(
        clone_database,
        _capability=_CUTOVER_ASSET_PROVIDER_PHASE_WRITER_CAPABILITY,
    ).advance(other_observation)
    assert first.status is SQLiteCutoverAssetProviderPhaseStatus.EXACT
    assert other.status is SQLiteCutoverAssetProviderPhaseStatus.EXACT
    assert first.identity == other.identity
    results = iter((first, other))
    monkeypatch.setattr(
        SQLiteCutoverAssetProviderPhaseWriter,
        "reconcile",
        lambda self: next(results),
    )
    original_reader = writer_module._read_cutover_phase_binding_prefix

    def read_final_clone(_ignored_connection):
        connection = None
        try:
            connection, _filesystem = clone_database._open_unvalidated()
            connection.execute("BEGIN")
            return original_reader(connection)
        finally:
            if connection is not None:
                if connection.in_transaction:
                    connection.rollback()
                connection.close()

    monkeypatch.setattr(
        writer_module,
        "_read_cutover_phase_binding_prefix",
        read_final_clone,
    )

    retried = first_writer.advance(first_observation)

    assert retried.status is SQLiteCutoverAssetProviderPhaseStatus.CONFLICT
    assert retried.code == "cutover_asset_provider_checkpoint_conflict"


@pytest.mark.parametrize(
    "point",
    (
        SQLiteCutoverAssetProviderPhaseTestPoint.AFTER_BINDING_BEFORE_HEAD,
        SQLiteCutoverAssetProviderPhaseTestPoint.BEFORE_COMMIT,
    ),
)
def test_asset_provider_writer_rolls_back_both_rows(
    tmp_path: Path,
    point: SQLiteCutoverAssetProviderPhaseTestPoint,
) -> None:
    fixture = _ready_fixture(tmp_path / (point.value + ".sqlite3"))
    writer = _writer(fixture)

    def fail(selected: SQLiteCutoverAssetProviderPhaseTestPoint) -> None:
        if selected is point:
            raise RuntimeError("before-commit-test")

    result = writer.advance(_observation(fixture), _test_hook=fail)

    assert result.status is SQLiteCutoverAssetProviderPhaseStatus.READY
    assert result.code == "cutover_asset_provider_phase_rolled_back"
    assert _state(fixture)[:2] == (1, 0)


def test_asset_provider_writer_recovers_ack_loss(tmp_path: Path) -> None:
    fixture = _ready_fixture(tmp_path / "ack.sqlite3")

    def fail(point: SQLiteCutoverAssetProviderPhaseTestPoint) -> None:
        if point is SQLiteCutoverAssetProviderPhaseTestPoint.AFTER_COMMIT_BEFORE_ACK:
            raise RuntimeError("ack-lost")

    result = _writer(fixture).advance(_observation(fixture), _test_hook=fail)

    assert result.status is SQLiteCutoverAssetProviderPhaseStatus.EXACT
    assert result.recovered_after_ack_loss is True
    assert _state(fixture)[:2] == (2, 1)


def test_asset_provider_commit_uncertainty_cannot_adopt_another_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _ready_fixture(tmp_path / "commit-ambiguity.sqlite3")
    first = _observation(fixture, provider_sequence=1)
    other = _observation(fixture, provider_sequence=2)
    probe, _filesystem = fixture.database._open_unvalidated()
    connection_type = type(probe)
    probe.close()
    original_commit = connection_type.commit
    other_results = []

    def fail_before_durable_commit(connection):
        monkeypatch.setattr(connection_type, "commit", original_commit)
        connection.rollback()
        other_results.append(_writer(fixture).advance(other))
        raise sqlite3.OperationalError("injected-commit-uncertainty")

    monkeypatch.setattr(connection_type, "commit", fail_before_durable_commit)
    result = _writer(fixture).advance(first)

    assert len(other_results) == 1
    assert other_results[0].status is SQLiteCutoverAssetProviderPhaseStatus.EXACT
    assert result.status is SQLiteCutoverAssetProviderPhaseStatus.CONFLICT
    assert result.code == "cutover_asset_provider_checkpoint_conflict"
    assert _state(fixture)[:2] == (2, 1)


def test_asset_provider_precommit_failure_cannot_adopt_another_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _ready_fixture(tmp_path / "rollback-race.sqlite3")
    first = _observation(fixture, provider_sequence=1)
    other = _observation(fixture, provider_sequence=2)
    original_rollback_close = writer_module._rollback_close
    other_results = []

    def rollback_then_other(connection):
        complete = original_rollback_close(connection)
        monkeypatch.setattr(
            writer_module, "_rollback_close", original_rollback_close
        )
        other_results.append(_writer(fixture).advance(other))
        return complete

    def fail(point: SQLiteCutoverAssetProviderPhaseTestPoint) -> None:
        if point is SQLiteCutoverAssetProviderPhaseTestPoint.BEFORE_COMMIT:
            raise RuntimeError("injected-precommit-failure")

    monkeypatch.setattr(writer_module, "_rollback_close", rollback_then_other)
    result = _writer(fixture).advance(first, _test_hook=fail)

    assert len(other_results) == 1
    assert other_results[0].status is SQLiteCutoverAssetProviderPhaseStatus.EXACT
    assert result.status is SQLiteCutoverAssetProviderPhaseStatus.CONFLICT
    assert result.code == "cutover_asset_provider_checkpoint_conflict"
    assert _state(fixture)[:2] == (2, 1)


def test_asset_provider_writer_rejects_expired_external_lease(tmp_path: Path) -> None:
    fixture = _ready_fixture(tmp_path / "expired.sqlite3")
    observation = _observation(fixture)
    observation._expire(
        _issuer=_FRESH_ASSET_PROVIDER_PHASE_OBSERVATION_ISSUER
    )

    result = _writer(fixture).advance(observation)

    assert result.status is SQLiteCutoverAssetProviderPhaseStatus.CONFLICT
    assert result.code == "cutover_asset_provider_external_lease_not_live"
    assert _state(fixture)[:2] == (1, 0)


def test_asset_provider_writer_rejects_detached_expected_pending_admission(
    tmp_path: Path,
) -> None:
    fixture = _ready_fixture(tmp_path / "admission.sqlite3")
    expected = fixture.plan.pending_admission
    wrong = BusinessCutoverAdmission(
        expected.authority_state,
        expected.authority_epoch,
        expected.revision + 1,
        expected.cutover_id,
    )

    result = _writer(fixture).advance(
        _observation(fixture, current_admission=wrong)
    )

    assert result.status is SQLiteCutoverAssetProviderPhaseStatus.CONFLICT
    assert result.code == "cutover_asset_provider_external_fence_mismatch"
    assert _state(fixture)[:2] == (1, 0)


def test_asset_provider_writer_rejects_provider_coverage_mismatch(
    tmp_path: Path,
) -> None:
    fixture = _ready_fixture(tmp_path / "provider-mismatch.sqlite3")

    result = _writer(fixture).advance(
        _observation(fixture, object_count=1)
    )

    assert result.status is SQLiteCutoverAssetProviderPhaseStatus.CORRUPT
    assert _state(fixture)[:2] == (1, 0)


def test_asset_provider_writer_rejects_different_checkpoint_after_commit(
    tmp_path: Path,
) -> None:
    fixture = _ready_fixture(tmp_path / "checkpoint-conflict.sqlite3")
    writer = _writer(fixture)
    assert writer.advance(_observation(fixture)).status is (
        SQLiteCutoverAssetProviderPhaseStatus.EXACT
    )

    result = writer.advance(_observation(fixture, provider_sequence=2))

    assert result.status is SQLiteCutoverAssetProviderPhaseStatus.CONFLICT
    assert result.code == "cutover_asset_provider_checkpoint_conflict"
    assert _state(fixture)[:2] == (2, 1)


@pytest.mark.parametrize("kind", ("admission", "freeze", "maintenance_time"))
def test_asset_provider_writer_retry_requires_same_external_fence(
    tmp_path: Path,
    kind: str,
) -> None:
    fixture = _ready_fixture(tmp_path / ("retry-" + kind + ".sqlite3"))
    writer = _writer(fixture)
    assert writer.advance(_observation(fixture)).status is (
        SQLiteCutoverAssetProviderPhaseStatus.EXACT
    )
    changes = {}
    if kind == "admission":
        expected = fixture.plan.pending_admission
        changes["current_admission"] = BusinessCutoverAdmission(
            expected.authority_state,
            expected.authority_epoch,
            expected.revision + 1,
            expected.cutover_id,
        )
    elif kind == "freeze":
        changes["source_freeze_sha256"] = b"\x91" * 32
    else:
        changes["maintenance_observed_at"] = "2026-01-01T00:00:00.000Z"

    result = writer.advance(_observation(fixture, **changes))

    assert result.status is SQLiteCutoverAssetProviderPhaseStatus.CONFLICT
    assert result.code == "cutover_asset_provider_checkpoint_conflict"
    assert _state(fixture)[:2] == (2, 1)


def test_asset_provider_writer_cross_binds_detached_ready_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _ready_fixture(tmp_path / "detached-race.sqlite3")
    original = SQLiteCutoverAssetProviderPhaseWriter._prove_and_issue

    def changed(self, connection, observation):
        snapshot = original(self, connection, observation)
        return replace(snapshot, closure_manifest_sha256=b"\x92" * 32)

    monkeypatch.setattr(
        SQLiteCutoverAssetProviderPhaseWriter,
        "_prove_and_issue",
        changed,
    )

    result = _writer(fixture).advance(_observation(fixture))

    assert result.status is SQLiteCutoverAssetProviderPhaseStatus.CONFLICT
    assert result.code == "cutover_asset_provider_ready_identity_changed"
    assert _state(fixture)[:2] == (1, 0)


def test_asset_provider_detached_ready_identity_rejects_every_field_drift(
    tmp_path: Path,
) -> None:
    fixture = _ready_fixture(tmp_path / "detached-all-fields.sqlite3")
    writer = _writer(fixture)
    observation = _observation(fixture)
    ready = writer.reconcile()
    assert ready.identity is not None
    connection = None
    try:
        connection, _filesystem = fixture.database._open_unvalidated()
        connection.execute("BEGIN IMMEDIATE")
        snapshot = writer._prove_and_issue(connection, observation)
    finally:
        if connection is not None:
            if connection.in_transaction:
                connection.rollback()
            connection.close()
    assert writer._matches_detached(snapshot, ready.identity)

    for field in ready.identity.__dataclass_fields__:
        drifted = copy.copy(ready.identity)
        current = getattr(drifted, field)
        if field == "phase":
            replacement = SQLiteCutoverDurablePhase.ASSETS_COVERED
        elif type(current) is bytes:
            replacement = b"\x93" * 32
        elif type(current) is int:
            replacement = current + 1
        elif current is None:
            replacement = b"\x94" * 32 if field.endswith("_sha256") else "drift"
        elif field == "authority_state":
            replacement = "active"
        elif field in ("cutover_id", "repository_epoch", "schema_fingerprint"):
            replacement = "a" * 64 if current != "a" * 64 else "b" * 64
        else:
            replacement = current + "-drift"
        object.__setattr__(drifted, field, replacement)
        assert not writer._matches_detached(snapshot, drifted), field


def test_asset_provider_exact_retry_rejects_every_external_observation_drift(
    tmp_path: Path,
) -> None:
    fixture = _ready_fixture(tmp_path / "retry-all-external.sqlite3")
    writer = _writer(fixture)
    original = _observation(fixture)
    assert writer.advance(original).status is SQLiteCutoverAssetProviderPhaseStatus.EXACT
    base = {
        name: getattr(original, name)
        for name in _FreshAssetProviderPhaseObservation.__slots__
        if not name.startswith("_")
    }
    admission = original.current_admission
    changes = {
        "current_admission": BusinessCutoverAdmission(
            admission.authority_state,
            admission.authority_epoch,
            admission.revision + 1,
            admission.cutover_id,
        ),
        "source_freeze_sha256": b"\x81" * 32,
        "maintenance_scope": "different_scope",
        "maintenance_proof_sha256": b"\x82" * 32,
        "maintenance_observed_at": "2026-01-01T00:00:00.000Z",
        "provider_protocol": original.provider_protocol + ".different",
        "provider_id": original.provider_id + "-different",
        "provider_generation": original.provider_generation + "-different",
        "provider_sequence": original.provider_sequence + 1,
        "provider_checked_at": "2026-01-01T00:00:00.000Z",
        "object_count": original.object_count + 1,
        "total_bytes": original.total_bytes + 1,
        "blob_root_sha256": b"\x83" * 32,
    }
    assert set(changes) == {
        name for name in _FreshAssetProviderPhaseObservation.__slots__
        if not name.startswith("_")
    }
    for field, replacement in changes.items():
        values = dict(base)
        values[field] = replacement
        changed = _FreshAssetProviderPhaseObservation(
            **values,
            _issuer=_FRESH_ASSET_PROVIDER_PHASE_OBSERVATION_ISSUER,
        )
        result = writer.advance(changed)
        assert result.status is SQLiteCutoverAssetProviderPhaseStatus.CONFLICT, field
        assert result.code == "cutover_asset_provider_checkpoint_conflict", field
    assert _state(fixture)[:2] == (2, 1)


@pytest.mark.parametrize("carrier_field", ("_issuer", "_pid"))
def test_asset_provider_writer_rejects_wrong_carrier_authority(
    tmp_path: Path,
    carrier_field: str,
) -> None:
    fixture = _ready_fixture(tmp_path / ("carrier-" + carrier_field + ".sqlite3"))
    observation = _observation(fixture)
    object.__setattr__(
        observation,
        carrier_field,
        object() if carrier_field == "_issuer" else observation._pid + 1,
    )
    result = _writer(fixture).advance(observation)
    assert result.status is SQLiteCutoverAssetProviderPhaseStatus.CONFLICT
    assert result.code == "cutover_asset_provider_external_lease_not_live"
    assert _state(fixture)[:2] == (1, 0)


def test_asset_provider_writer_rejects_cross_thread_observation(
    tmp_path: Path,
) -> None:
    fixture = _ready_fixture(tmp_path / "carrier-thread.sqlite3")
    observation = _observation(fixture)
    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(_writer(fixture).advance, observation).result()
    assert result.status is SQLiteCutoverAssetProviderPhaseStatus.CONFLICT
    assert result.code == "cutover_asset_provider_external_lease_not_live"
    assert _state(fixture)[:2] == (1, 0)


def test_asset_provider_writer_revalidates_live_lease_immediately_before_insert(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _ready_fixture(tmp_path / "lease-lost-before-insert.sqlite3")
    observation = _observation(fixture)
    original = SQLiteCutoverAssetProviderPhaseWriter._prove_and_issue

    def expire_after_proof(self, connection, value):
        snapshot = original(self, connection, value)
        value._expire(
            _issuer=_FRESH_ASSET_PROVIDER_PHASE_OBSERVATION_ISSUER
        )
        return snapshot

    monkeypatch.setattr(
        SQLiteCutoverAssetProviderPhaseWriter,
        "_prove_and_issue",
        expire_after_proof,
    )

    result = _writer(fixture).advance(observation)

    assert result.status is SQLiteCutoverAssetProviderPhaseStatus.CONFLICT
    assert result.code == "cutover_asset_provider_external_lease_not_live"
    assert _state(fixture)[:2] == (1, 0)


def test_asset_provider_writer_revalidates_live_lease_after_final_readback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _ready_fixture(tmp_path / "lease-lost-after-readback.sqlite3")
    observation = _observation(fixture)
    original = writer_module._read_cutover_phase_binding_prefix

    def expire_after_readback(connection):
        prefix = original(connection)
        observation._expire(
            _issuer=_FRESH_ASSET_PROVIDER_PHASE_OBSERVATION_ISSUER
        )
        return prefix

    monkeypatch.setattr(
        writer_module,
        "_read_cutover_phase_binding_prefix",
        expire_after_readback,
    )

    result = _writer(fixture).advance(observation)

    assert result.status is SQLiteCutoverAssetProviderPhaseStatus.CONFLICT
    assert result.code == "cutover_asset_provider_external_lease_not_live"
    assert _state(fixture)[:2] == (1, 0)


def test_asset_provider_writer_revalidates_live_lease_before_commit(
    tmp_path: Path,
) -> None:
    fixture = _ready_fixture(tmp_path / "lease-lost.sqlite3")
    observation = _observation(fixture)

    def expire(point: SQLiteCutoverAssetProviderPhaseTestPoint) -> None:
        if point is SQLiteCutoverAssetProviderPhaseTestPoint.BEFORE_COMMIT:
            observation._expire(
                _issuer=_FRESH_ASSET_PROVIDER_PHASE_OBSERVATION_ISSUER
            )

    result = _writer(fixture).advance(observation, _test_hook=expire)

    assert result.status is SQLiteCutoverAssetProviderPhaseStatus.CONFLICT
    assert result.code == "cutover_asset_provider_external_lease_not_live"
    assert _state(fixture)[:2] == (1, 0)


def test_asset_provider_authorizer_rejects_every_other_mutation(tmp_path: Path) -> None:
    fixture = _ready_fixture(tmp_path / "authorizer.sqlite3")
    connection = None
    try:
        connection, _filesystem = fixture.database._open_unvalidated()
        connection.execute("BEGIN IMMEDIATE")
        authorizer = _AssetProviderPhaseAuthorizer()
        connection.set_authorizer(authorizer)
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute("INSERT INTO repository_audit_head DEFAULT VALUES")
        assert authorizer.denied is True
        _reset_authorizer(connection)
        connection.rollback()
    finally:
        if connection is not None:
            if connection.in_transaction:
                connection.rollback()
            connection.close()


def test_asset_provider_authorizer_denies_complete_mutation_surface() -> None:
    insert = sqlite3.SQLITE_INSERT
    update = sqlite3.SQLITE_UPDATE
    delete = sqlite3.SQLITE_DELETE
    pragma = sqlite3.SQLITE_PRAGMA

    closed = _AssetProviderPhaseAuthorizer()
    assert closed(
        insert, "business_cutover_asset_provider_bindings", None, "main", None
    ) == sqlite3.SQLITE_DENY
    assert closed(
        update, "business_cutover_head", "phase_revision", "main", None
    ) == sqlite3.SQLITE_DENY

    binding = _AssetProviderPhaseAuthorizer()
    binding.open_binding_insert()
    assert binding(
        insert, "business_cutover_asset_provider_bindings", None, "main", None
    ) == sqlite3.SQLITE_OK
    for action, arg1, arg2, database, trigger in (
        (insert, "transaction_receipts", None, "main", None),
        (insert, "business_cutover_asset_provider_bindings", None, "temp", None),
        (insert, "business_cutover_asset_provider_bindings", None, "main", "evil"),
        (update, "business_cutover_head", "phase_revision", "main", None),
        (delete, "business_cutover_asset_provider_bindings", None, "main", None),
    ):
        assert binding(action, arg1, arg2, database, trigger) == sqlite3.SQLITE_DENY
    binding.close()

    head = _AssetProviderPhaseAuthorizer()
    head.open_head_update()
    assert head(
        update, "business_cutover_head", "phase_revision", "main", None
    ) == sqlite3.SQLITE_OK
    for action, arg1, arg2, database, trigger in (
        (update, "business_cutover_head", "repository_epoch", "main", None),
        (update, "business_cutover_head", "phase_revision", "temp", None),
        (update, "business_cutover_head", "phase_revision", "main", "evil"),
        (insert, "business_cutover_asset_provider_bindings", None, "main", None),
        (delete, "business_cutover_head", None, "main", None),
    ):
        assert head(action, arg1, arg2, database, trigger) == sqlite3.SQLITE_DENY
    head.close()

    for action_name in (
        "SQLITE_ALTER_TABLE",
        "SQLITE_ANALYZE",
        "SQLITE_ATTACH",
        "SQLITE_CREATE_INDEX",
        "SQLITE_CREATE_TABLE",
        "SQLITE_CREATE_TEMP_INDEX",
        "SQLITE_CREATE_TEMP_TABLE",
        "SQLITE_CREATE_TEMP_TRIGGER",
        "SQLITE_CREATE_TEMP_VIEW",
        "SQLITE_CREATE_TRIGGER",
        "SQLITE_CREATE_VIEW",
        "SQLITE_CREATE_VTABLE",
        "SQLITE_DETACH",
        "SQLITE_DROP_INDEX",
        "SQLITE_DROP_TABLE",
        "SQLITE_DROP_TEMP_INDEX",
        "SQLITE_DROP_TEMP_TABLE",
        "SQLITE_DROP_TEMP_TRIGGER",
        "SQLITE_DROP_TEMP_VIEW",
        "SQLITE_DROP_TRIGGER",
        "SQLITE_DROP_VIEW",
        "SQLITE_DROP_VTABLE",
        "SQLITE_REINDEX",
        "SQLITE_TRANSACTION",
        "SQLITE_SAVEPOINT",
    ):
        action = getattr(sqlite3, action_name, None)
        if action is not None:
            assert _AssetProviderPhaseAuthorizer()(
                action, "object", None, "main", None
            ) == sqlite3.SQLITE_DENY
    assert _AssetProviderPhaseAuthorizer()(
        pragma, "journal_mode", "OFF", "main", None
    ) == sqlite3.SQLITE_DENY
    assert _AssetProviderPhaseAuthorizer()(
        pragma, "foreign_keys", None, "main", None
    ) == sqlite3.SQLITE_OK


def test_asset_provider_committed_storage_barrier_failure_is_unknown_then_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _ready_fixture(tmp_path / "durability.sqlite3")
    observation = _observation(fixture)
    original = SqliteDatabase._finalize_committed_storage

    def fail(self):
        raise OSError("injected-durability-failure")

    monkeypatch.setattr(SqliteDatabase, "_finalize_committed_storage", fail)
    result = _writer(fixture).advance(observation)
    assert result.status is SQLiteCutoverAssetProviderPhaseStatus.UNKNOWN
    assert _state(fixture)[:2] == (2, 1)

    monkeypatch.setattr(SqliteDatabase, "_finalize_committed_storage", original)
    retry = _writer(fixture).advance(observation)
    assert retry.status is SQLiteCutoverAssetProviderPhaseStatus.EXACT
    assert retry.recovered_after_ack_loss is True


@pytest.mark.parametrize("signal", (KeyboardInterrupt, SystemExit))
def test_asset_provider_process_control_before_commit_is_preserved_and_rolls_back(
    tmp_path: Path,
    signal: type[BaseException],
) -> None:
    fixture = _ready_fixture(tmp_path / ("process-" + signal.__name__ + ".sqlite3"))

    def interrupt(point: SQLiteCutoverAssetProviderPhaseTestPoint) -> None:
        if point is SQLiteCutoverAssetProviderPhaseTestPoint.BEFORE_COMMIT:
            raise signal("stop")

    with pytest.raises(signal):
        _writer(fixture).advance(_observation(fixture), _test_hook=interrupt)
    assert _state(fixture)[:2] == (1, 0)


@pytest.mark.parametrize("signal", (KeyboardInterrupt, SystemExit))
def test_asset_provider_process_control_after_commit_is_preserved_and_recovers(
    tmp_path: Path,
    signal: type[BaseException],
) -> None:
    fixture = _ready_fixture(
        tmp_path / ("process-after-" + signal.__name__ + ".sqlite3")
    )

    def interrupt(point: SQLiteCutoverAssetProviderPhaseTestPoint) -> None:
        if point is SQLiteCutoverAssetProviderPhaseTestPoint.AFTER_COMMIT_BEFORE_ACK:
            raise signal("stop-after-commit")

    with pytest.raises(signal):
        _writer(fixture).advance(_observation(fixture), _test_hook=interrupt)
    recovered = _writer(fixture).reconcile()
    assert recovered.status is SQLiteCutoverAssetProviderPhaseStatus.EXACT
    assert recovered.identity is not None
    assert recovered.identity.phase.value == "assets_covered"
    assert _state(fixture)[:2] == (2, 1)


def test_asset_provider_busy_writer_is_unknown_without_partial_rows(
    tmp_path: Path,
) -> None:
    fixture = _ready_fixture(tmp_path / "busy.sqlite3")
    observation = _observation(fixture)
    fixture.database.settings = SQLiteSettings(busy_timeout_ms=25)
    blocker = sqlite3.connect(
        str(fixture.database.path), isolation_level=None, timeout=0.1
    )
    try:
        blocker.execute("PRAGMA busy_timeout=100")
        blocker.execute("BEGIN IMMEDIATE")
        result = _writer(fixture).advance(observation)
    finally:
        if blocker.in_transaction:
            blocker.rollback()
        blocker.close()

    assert result.status is SQLiteCutoverAssetProviderPhaseStatus.UNKNOWN
    assert result.code == "cutover_asset_provider_storage_unavailable"
    assert _state(fixture)[:2] == (1, 0)


def test_two_concurrent_asset_provider_writers_converge_on_one_exact_binding(
    tmp_path: Path,
) -> None:
    fixture = _ready_fixture(tmp_path / "race.sqlite3")
    template = _observation(fixture)
    values = {
        name: getattr(template, name)
        for name in _FreshAssetProviderPhaseObservation.__slots__
        if not name.startswith("_")
    }

    def advance():
        observation = _FreshAssetProviderPhaseObservation(
            **values,
            _issuer=_FRESH_ASSET_PROVIDER_PHASE_OBSERVATION_ISSUER,
        )
        return _writer(fixture).advance(observation)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(lambda _value: advance(), range(2)))

    assert all(
        result.status is SQLiteCutoverAssetProviderPhaseStatus.EXACT
        for result in results
    )
    assert sum(result.recovered_after_ack_loss for result in results) >= 1
    assert _state(fixture)[:2] == (2, 1)
