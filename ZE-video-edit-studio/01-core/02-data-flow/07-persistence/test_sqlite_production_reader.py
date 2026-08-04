"""Plan-free, single-snapshot production rooted-reader tests."""

from __future__ import annotations

from dataclasses import replace
import inspect
from pathlib import Path
import sqlite3
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

from business_codec import decode_payload_bounded, encode_payload_bounded  # noqa: E402
from production_atomic_write_contract import (  # noqa: E402
    MAX_PRODUCTION_ATOMIC_MANIFEST_BYTES,
    MAX_PRODUCTION_ATOMIC_MANIFEST_NODES,
    ProductionWriteStatus,
)
from production_atomic_write_integrity import (  # noqa: E402
    PRODUCTION_ATOMIC_MANIFEST_HASH_DOMAIN,
    PRODUCTION_ATOMIC_RECEIPT_HASH_DOMAIN,
    _domain_digest as _production_domain_digest,
    _groups,
    _production_outbox_allocation_tail,
    _production_targets_sha256,
    _production_worker_genesis_state_sha256,
)
import sqlite_production_reader as production_reader_module  # noqa: E402
from sqlite_production_reader import (  # noqa: E402
    SQLiteProductionHead,
    SQLiteProductionReadError,
    SQLiteProductionReader,
)
from sqlite_production_test_support import (  # noqa: E402
    create_canonical_mounted_business_database,
    issue_mounted_production_atomic_write_plan,
)
from sqlite_production_writer import SQLiteProductionAtomicWriter  # noqa: E402
from sqlite_worker_claim_writer import (  # noqa: E402
    SQLiteWorkerClaimStatus,
    SQLiteWorkerClaimWriter,
)
from worker_execution_contract import WorkerExecutionStatus  # noqa: E402
from test_sqlite_business_transaction import _plan as _business_plan  # noqa: E402
from test_sqlite_production_test_support import (  # noqa: E402
    _multi_group_atomic_plan,
)
from test_sqlite_production_writer import (  # noqa: E402
    _generic_production_plan,
    _pipeline_transition_plan,
)
from test_sqlite_worker_claim_writer import _claim_fixture  # noqa: E402
from business_transaction_contract import freeze_json_payload  # noqa: E402


def _committed_multigroup(tmp_path):
    baseline = create_canonical_mounted_business_database(
        tmp_path / "production-reader.sqlite3"
    )
    atomic = _multi_group_atomic_plan(baseline)
    plan = issue_mounted_production_atomic_write_plan(baseline.database, atomic)
    result = SQLiteProductionAtomicWriter(baseline.database).commit(plan)
    assert result.status is ProductionWriteStatus.COMMITTED
    return baseline, atomic, plan


def _tamper_atomic_receipt(database, commit_id: str) -> None:
    raw = sqlite3.connect(database.path, isolation_level=None)
    try:
        trigger = raw.execute(
            "SELECT sql FROM sqlite_schema WHERE type='trigger' AND "
            "name='atomic_business_write_receipts_no_update'"
        ).fetchone()
        assert trigger is not None and isinstance(trigger[0], str)
        raw.execute("BEGIN IMMEDIATE")
        raw.execute("DROP TRIGGER atomic_business_write_receipts_no_update")
        raw.execute(
            "UPDATE atomic_business_write_receipts "
            "SET production_manifest_bytes="
            "CAST(production_manifest_bytes || x'20' AS BLOB) "
            "WHERE commit_id=?",
            (commit_id,),
        )
        raw.execute(trigger[0])
        raw.commit()
    finally:
        if raw.in_transaction:
            raw.rollback()
        raw.close()


def _mutate_without_table_triggers(database, tables, statements) -> None:
    raw = sqlite3.connect(database.path, isolation_level=None)
    try:
        raw.execute("PRAGMA foreign_keys=OFF")
        placeholders = ",".join("?" for _value in tables)
        trigger_rows = raw.execute(
            "SELECT name,sql FROM sqlite_schema WHERE type='trigger' "
            "AND tbl_name IN (" + placeholders + ") ORDER BY name",
            tuple(tables),
        ).fetchall()
        assert trigger_rows and all(
            isinstance(row[0], str) and isinstance(row[1], str)
            for row in trigger_rows
        )
        raw.execute("BEGIN IMMEDIATE")
        for name, _sql in trigger_rows:
            raw.execute("DROP TRIGGER " + name)
        for sql, parameters in statements:
            raw.execute(sql, parameters)
        for _name, sql in trigger_rows:
            raw.execute(sql)
        raw.commit()
    finally:
        if raw.in_transaction:
            raw.rollback()
        raw.close()


def test_reader_is_plan_free_and_rebuilds_every_target_group(tmp_path) -> None:
    baseline, atomic, plan = _committed_multigroup(tmp_path)
    reader = SQLiteProductionReader(baseline.database)

    current = reader.read_current_head(
        baseline.project_id,
        baseline.generation,
        expected_commit_id=atomic.commit_id,
    )
    rooted = reader.read_commit(atomic.commit_id)

    assert isinstance(current, SQLiteProductionHead)
    assert current == rooted
    assert current.business.commit_id == atomic.commit_id
    assert current.commit_id == atomic.commit_id
    assert current.exact_at_commit is True
    assert (
        current.pipeline_target_count,
        current.action_target_count,
        current.outbox_target_count,
    ) == (2, 1, 2)
    assert current.target_count == 5
    assert current.control_count == 2
    assert current.allocation_count == 2
    assert current.allocation_head_sequence == 2
    assert current.targets_sha256 == plan.evidence.targets_sha256
    assert (
        current.production_manifest_sha256
        == plan.evidence.production_manifest_sha256
    )
    assert (
        current.production_receipt_sha256
        == plan.expected_receipt.production_receipt_sha256
    )
    assert not hasattr(current, "connection")
    assert not hasattr(current, "path")

    # The public rooted boundary has selectors only; prior plan evidence is
    # neither accepted nor retained.
    assert "plan" not in inspect.signature(reader.read_current_head).parameters
    assert "plan" not in inspect.signature(reader.read_commit).parameters
    assert not hasattr(reader, "reconcile")


def test_reader_rejects_baseline_without_claiming_production_corruption(
    tmp_path,
) -> None:
    baseline = create_canonical_mounted_business_database(
        tmp_path / "baseline-only.sqlite3"
    )

    with pytest.raises(SQLiteProductionReadError) as raised:
        SQLiteProductionReader(baseline.database).read_current_head(
            baseline.project_id,
            baseline.generation,
        )

    assert raised.value.code == "sqlite_production_read_not_committed"


def test_reader_detects_outer_manifest_tamper_without_a_plan(tmp_path) -> None:
    baseline, atomic, _plan = _committed_multigroup(tmp_path)
    _tamper_atomic_receipt(baseline.database, atomic.commit_id)

    with pytest.raises(SQLiteProductionReadError) as raised:
        SQLiteProductionReader(baseline.database).read_commit(atomic.commit_id)

    assert raised.value.code == "sqlite_production_read_corrupt"


def test_pipeline_intent_payload_row_must_match_control_manifest(
    tmp_path,
) -> None:
    baseline, atomic, _plan = _committed_multigroup(tmp_path)
    replacement = freeze_json_payload(
        {"name": "offline-replacement"},
        schema="ze.pipeline.create.replaced.v1",
    )
    _mutate_without_table_triggers(
        baseline.database,
        ("pipeline_control_intents",),
        ((
            "UPDATE pipeline_control_intents SET payload_schema=?,"
            "payload_codec_id=?,payload_codec_version=?,payload_bytes=?,"
            "payload_sha256=? WHERE origin_commit_id=? AND ordinal=0",
            (
                replacement.schema,
                replacement.codec_id,
                replacement.codec_version,
                replacement.payload_bytes,
                replacement.payload_sha256,
                atomic.commit_id,
            ),
        ),),
    )

    with pytest.raises(SQLiteProductionReadError) as raised:
        SQLiteProductionReader(baseline.database).read_commit(atomic.commit_id)

    assert raised.value.code == "sqlite_production_read_corrupt"


def test_self_consistent_outbox_scope_rehash_is_still_corrupt(tmp_path) -> None:
    baseline, atomic, plan = _committed_multigroup(tmp_path)
    original = next(
        value
        for value in plan.targets
        if value.target.target_kind.value == "outbox"
    )
    changed_target = replace(
        original.target,
        idempotency_scope="attacker/self-consistent",
    )
    changed_genesis = _production_worker_genesis_state_sha256(
        changed_target,
        status=WorkerExecutionStatus.PENDING,
    )
    changed_wrapped = replace(
        original,
        target=changed_target,
        genesis_state_sha256=changed_genesis,
    )
    changed_targets = tuple(
        changed_wrapped if value is original else value
        for value in plan.targets
    )
    targets_sha256 = _production_targets_sha256(_groups(changed_targets))

    raw = sqlite3.connect(baseline.database.path, isolation_level=None)
    raw.row_factory = sqlite3.Row
    try:
        receipt = raw.execute(
            "SELECT * FROM atomic_business_write_receipts WHERE commit_id=?",
            (atomic.commit_id,),
        ).fetchone()
    finally:
        raw.close()
    assert receipt is not None
    outer = decode_payload_bounded(
        receipt["production_manifest_bytes"],
        MAX_PRODUCTION_ATOMIC_MANIFEST_BYTES,
        MAX_PRODUCTION_ATOMIC_MANIFEST_NODES,
    )
    outer["targets"]["sha256"] = targets_sha256.hex()
    outer_bytes = encode_payload_bounded(
        outer,
        MAX_PRODUCTION_ATOMIC_MANIFEST_BYTES,
        MAX_PRODUCTION_ATOMIC_MANIFEST_NODES,
    )
    outer_sha256 = _production_domain_digest(
        PRODUCTION_ATOMIC_MANIFEST_HASH_DOMAIN,
        (outer_bytes,),
    )
    production_receipt_sha256 = _production_domain_digest(
        PRODUCTION_ATOMIC_RECEIPT_HASH_DOMAIN,
        (
            atomic.commit_id.encode("ascii"),
            receipt["transaction_id"].encode("utf-8"),
            receipt["receipt_kind"].encode("utf-8"),
            receipt["committed_at"].encode("ascii"),
            receipt["receipt_sha256"],
            outer_sha256,
            receipt["target_count"].to_bytes(8, "big"),
            targets_sha256,
        ),
    )
    _mutate_without_table_triggers(
        baseline.database,
        (
            "worker_targets",
            "worker_target_current",
            "atomic_business_write_receipts",
        ),
        (
            (
                "UPDATE worker_targets SET idempotency_scope=?,"
                "genesis_state_sha256=? WHERE target_id=?",
                (
                    changed_target.idempotency_scope,
                    changed_genesis,
                    changed_target.target_id,
                ),
            ),
            (
                "UPDATE worker_target_current SET state_sha256=? WHERE target_id=?",
                (changed_genesis, changed_target.target_id),
            ),
            (
                "UPDATE atomic_business_write_receipts SET targets_sha256=?,"
                "production_manifest_bytes=?,production_manifest_sha256=?,"
                "production_receipt_sha256=? WHERE commit_id=?",
                (
                    targets_sha256,
                    outer_bytes,
                    outer_sha256,
                    production_receipt_sha256,
                    atomic.commit_id,
                ),
            ),
        ),
    )

    with pytest.raises(SQLiteProductionReadError) as raised:
        SQLiteProductionReader(baseline.database).read_commit(atomic.commit_id)

    assert raised.value.code == "sqlite_production_read_corrupt"


def test_self_consistent_fake_allocation_descendant_is_corrupt(tmp_path) -> None:
    baseline, atomic, _plan = _committed_multigroup(tmp_path)
    raw = sqlite3.connect(baseline.database.path)
    try:
        head = raw.execute(
            "SELECT state_revision,sequence,tail_sha256 FROM "
            "outbox_allocation_head WHERE singleton=1"
        ).fetchone()
    finally:
        raw.close()
    assert head is not None
    fake_event_id = "f" * 32
    fake_commit_id = "e" * 32
    fake_revision = 3
    allocated_at = "2099-01-01T00:00:00.000Z"
    fake_tail = _production_outbox_allocation_tail(
        sequence=head[1] + 1,
        event_id=fake_event_id,
        commit_id=fake_commit_id,
        repository_revision=fake_revision,
        ordinal=0,
        previous_tail_sha256=head[2],
    )
    _mutate_without_table_triggers(
        baseline.database,
        ("outbox_allocations", "outbox_allocation_head"),
        (
            (
                "INSERT INTO outbox_allocations(sequence,event_id,commit_id,"
                "repository_revision,ordinal,previous_tail_sha256,"
                "allocation_tail_sha256,allocated_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    head[1] + 1,
                    fake_event_id,
                    fake_commit_id,
                    fake_revision,
                    0,
                    head[2],
                    fake_tail,
                    allocated_at,
                ),
            ),
            (
                "UPDATE outbox_allocation_head SET state_revision=?,sequence=?,"
                "previous_tail_sha256=?,tail_sha256=?,updated_at=? WHERE singleton=1",
                (
                    head[0] + 1,
                    head[1] + 1,
                    head[2],
                    fake_tail,
                    allocated_at,
                ),
            ),
        ),
    )

    with pytest.raises(SQLiteProductionReadError) as raised:
        SQLiteProductionReader(baseline.database).read_commit(atomic.commit_id)

    assert raised.value.code == "sqlite_production_read_corrupt"


def test_offline_current_change_without_new_ledger_receipt_is_corrupt(
    tmp_path,
) -> None:
    baseline, atomic, plan = _committed_multigroup(tmp_path)
    target_id = plan.targets[-1].target.target_id
    _mutate_without_table_triggers(
        baseline.database,
        ("worker_target_current",),
        ((
            "UPDATE worker_target_current SET updated_at=? WHERE target_id=?",
            ("2099-01-01T00:00:00.000Z", target_id),
        ),),
    )

    with pytest.raises(SQLiteProductionReadError) as raised:
        SQLiteProductionReader(baseline.database).read_commit(atomic.commit_id)

    assert raised.value.code == "sqlite_production_read_corrupt"
    assert raised.value.code != "sqlite_production_read_progress_unsupported"


def test_missing_database_is_unavailable_not_corrupt(tmp_path) -> None:
    baseline, atomic, _plan = _committed_multigroup(tmp_path)
    missing = baseline.database.path.with_suffix(".temporarily-missing")
    baseline.database.path.rename(missing)

    with pytest.raises(SQLiteProductionReadError) as raised:
        SQLiteProductionReader(baseline.database).read_commit(atomic.commit_id)

    assert raised.value.code == "sqlite_production_read_unavailable"
    assert "corrupt" not in raised.value.code


def test_legal_read_only_authority_is_inactive_not_corrupt(tmp_path) -> None:
    baseline, _atomic, _plan = _committed_multigroup(tmp_path)
    raw = sqlite3.connect(baseline.database.path, isolation_level=None)
    try:
        raw.execute(
            "UPDATE repository_runtime_control SET authority_state='read_only',"
            "state_revision=state_revision+1,updated_at=? WHERE singleton=1",
            ("2099-01-01T00:00:00.000Z",),
        )
    finally:
        raw.close()

    with pytest.raises(SQLiteProductionReadError) as raised:
        SQLiteProductionReader(baseline.database).read_current_head(
            baseline.project_id,
            baseline.generation,
        )

    assert raised.value.code == "sqlite_production_read_authority_inactive"
    assert "corrupt" not in raised.value.code


def test_legal_worker_progress_is_unknown_to_exact_reader_not_corrupt(
    tmp_path,
) -> None:
    baseline, _production, claim = _claim_fixture(
        tmp_path,
        "progress.sqlite3",
    )
    result = SQLiteWorkerClaimWriter(baseline.database).commit(claim)
    assert result.status is SQLiteWorkerClaimStatus.COMMITTED

    with pytest.raises(SQLiteProductionReadError) as raised:
        SQLiteProductionReader(baseline.database).read_current_head(
            baseline.project_id,
            baseline.generation,
        )

    assert (
        raised.value.code
        == "sqlite_production_read_progress_unsupported"
    )
    assert "corrupt" not in raised.value.code


def test_publish_fence_rejects_claim_between_snapshot_and_handoff(
    tmp_path,
    monkeypatch,
) -> None:
    baseline, _production, claim = _claim_fixture(
        tmp_path,
        "publish-race.sqlite3",
    )
    original_finish = production_reader_module._finish_read_connection
    calls = 0

    def finish_with_claim(connection, *, reset_authorizer):
        nonlocal calls
        complete = original_finish(
            connection,
            reset_authorizer=reset_authorizer,
        )
        calls += 1
        if calls == 1:
            committed = SQLiteWorkerClaimWriter(baseline.database).commit(claim)
            assert committed.status is SQLiteWorkerClaimStatus.COMMITTED
        return complete

    monkeypatch.setattr(
        production_reader_module,
        "_finish_read_connection",
        finish_with_claim,
    )

    with pytest.raises(SQLiteProductionReadError) as raised:
        SQLiteProductionReader(baseline.database).read_current_head(
            baseline.project_id,
            baseline.generation,
        )

    assert raised.value.code == "sqlite_production_read_head_changed"


def test_commit_selector_is_current_head_root_not_historical_snapshot(
    tmp_path,
) -> None:
    baseline = create_canonical_mounted_business_database(
        tmp_path / "commit-selector.sqlite3"
    )
    atomic = _business_plan(baseline)
    plan = issue_mounted_production_atomic_write_plan(baseline.database, atomic)
    assert (
        SQLiteProductionAtomicWriter(baseline.database).commit(plan).status
        is ProductionWriteStatus.COMMITTED
    )

    with pytest.raises(SQLiteProductionReadError) as raised:
        SQLiteProductionReader(baseline.database).read_commit("9" * 32)
    assert raised.value.code == "sqlite_production_read_not_committed"


def test_reader_closes_transition_control_against_prior_target_history(
    tmp_path,
) -> None:
    baseline = create_canonical_mounted_business_database(
        tmp_path / "transition.sqlite3"
    )
    writer = SQLiteProductionAtomicWriter(baseline.database)
    first = _generic_production_plan(baseline)
    assert writer.commit(first).status is ProductionWriteStatus.COMMITTED
    transition = _pipeline_transition_plan(baseline, first)
    assert writer.commit(transition).status is ProductionWriteStatus.COMMITTED

    rooted = SQLiteProductionReader(baseline.database).read_commit(
        transition.evidence.commit_id
    )

    assert rooted.pipeline_target_count == 0
    assert rooted.outbox_target_count == 1
    assert rooted.control_count == 1
    assert rooted.allocation_head_sequence == 3


def test_invalid_root_inputs_fail_before_opening_storage(tmp_path) -> None:
    baseline = create_canonical_mounted_business_database(
        tmp_path / "invalid-root.sqlite3"
    )
    reader = SQLiteProductionReader(baseline.database)

    for invoke in (
        lambda: reader.read_commit("ABC"),
        lambda: reader.read_current_head("", 1),
        lambda: reader.read_current_head(baseline.project_id, True),
    ):
        with pytest.raises(SQLiteProductionReadError) as raised:
            invoke()
        assert raised.value.code == "sqlite_production_read_input_invalid"
