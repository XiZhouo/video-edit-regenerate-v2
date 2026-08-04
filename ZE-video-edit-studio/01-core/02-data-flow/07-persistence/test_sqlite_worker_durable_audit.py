"""Plan-free durable audit tests against real SQLite state."""

from __future__ import annotations

import dataclasses
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

from business_codec import decode_payload_bounded, encode_payload_bounded
from business_atomic_write_contract import (
    MAX_ATOMIC_BUSINESS_WRITE_MANIFEST_BYTES,
    MAX_ATOMIC_BUSINESS_WRITE_MANIFEST_NODES,
    AtomicBusinessWriteGenericReceiptEvidence,
)
from business_atomic_write_integrity import (
    _atomic_receipt_digest,
    _manifest_digest as _atomic_manifest_digest,
)
from business_transaction_contract import freeze_json_payload
from business_transaction_contract import (
    MAX_ATOMIC_MANIFEST_BYTES,
    MAX_ATOMIC_MANIFEST_NODES,
)
from business_transaction_integrity import (
    _manifest_digest as _business_manifest_digest,
)
import sqlite_worker_durable_audit as audit_module
from sqlite_worker_claim_writer import (
    SQLiteWorkerClaimStatus,
    SQLiteWorkerClaimWriter,
)
from sqlite_worker_durable_audit import (
    SQLiteWorkerDurableAuditStatus,
    SQLiteWorkerDurableAuditor,
)
from sqlite_worker_projection_writer import (
    SQLiteWorkerProjectionStatus,
)
from production_atomic_write_contract import (
    MAX_PRODUCTION_ATOMIC_MANIFEST_BYTES,
    MAX_PRODUCTION_ATOMIC_MANIFEST_NODES,
    ProductionWriteStatus,
)
from production_atomic_write_integrity import (
    PRODUCTION_ATOMIC_MANIFEST_HASH_DOMAIN,
    PRODUCTION_ATOMIC_RECEIPT_HASH_DOMAIN,
    _domain_digest as _production_domain_digest,
    _groups as _production_groups,
    _production_targets_sha256,
    _production_worker_genesis_state_sha256,
)
from sqlite_production_test_support import (
    create_canonical_mounted_business_database,
)
from sqlite_production_writer import SQLiteProductionAtomicWriter
from test_sqlite_production_writer import (
    _generic_production_plan,
    _pipeline_transition_plan,
    _persist_worker_execution,
)
from test_sqlite_worker_claim_writer import (
    _claim_fixture,
    _expiry_after_claim,
    _one,
)
from test_sqlite_worker_projection_writer import (
    _commit_authorize,
    _complete_plan,
    _reconcile_plan,
)
from worker_execution_contract import (
    WorkerExecutionStatus,
    WorkerLedgerHead,
    WorkerTerminalProof,
    WorkerTerminalProofKind,
)
from worker_execution_integrity import _issue_worker_execution_plan
from worker_result_projection_integrity import (
    _issue_worker_result_projection_plan,
)


def _tamper(database, table: str, sql: str, values=()) -> None:
    """Mutate behind guards, then restore the exact canonical trigger DDL."""

    raw = sqlite3.connect(database.path, isolation_level=None)
    try:
        triggers = tuple(raw.execute(
            "SELECT name,sql FROM sqlite_schema "
            "WHERE type='trigger' AND tbl_name=? ORDER BY name",
            (table,),
        ))
        assert all(isinstance(row[0], str) and isinstance(row[1], str)
                   for row in triggers)
        raw.execute("PRAGMA ignore_check_constraints=ON")
        raw.execute("BEGIN IMMEDIATE")
        for name, _statement in triggers:
            raw.execute("DROP TRIGGER " + name)
        raw.execute(sql, values)
        for _name, statement in triggers:
            raw.execute(statement)
        raw.commit()
    finally:
        if raw.in_transaction:
            raw.rollback()
        raw.close()


def _tamper_many(database, tables, statements) -> None:
    raw = sqlite3.connect(database.path, isolation_level=None)
    try:
        placeholders = ",".join("?" for _table in tables)
        triggers = tuple(raw.execute(
            "SELECT name,sql FROM sqlite_schema WHERE type='trigger' "
            "AND tbl_name IN (" + placeholders + ") ORDER BY name",
            tuple(tables),
        ))
        assert all(
            isinstance(row[0], str) and isinstance(row[1], str)
            for row in triggers
        )
        raw.execute("PRAGMA foreign_keys=OFF")
        raw.execute("PRAGMA ignore_check_constraints=ON")
        raw.execute("BEGIN IMMEDIATE")
        for name, _statement in triggers:
            raw.execute("DROP TRIGGER " + name)
        for sql, values in statements:
            raw.execute(sql, values)
        for _name, statement in triggers:
            raw.execute(statement)
        raw.commit()
    finally:
        if raw.in_transaction:
            raw.rollback()
        raw.close()


def _published(tmp_path, name="durable-audit-published.sqlite3"):
    baseline, claim, authorize, _projected, writer = _commit_authorize(
        tmp_path, name
    )
    complete, projected = _complete_plan(
        baseline,
        authorize,
        status=WorkerExecutionStatus.PUBLISHED,
        request_id="e" * 32,
        with_proof=True,
    )
    assert (
        writer.commit(complete, projected).status
        is SQLiteWorkerProjectionStatus.COMMITTED
    )
    return baseline, claim, authorize, complete, writer


def _terminal_dead(tmp_path, name="durable-audit-dead.sqlite3"):
    baseline, claim, authorize, _projected, writer = _commit_authorize(
        tmp_path, name
    )
    template, _template_projection = _complete_plan(
        baseline,
        authorize,
        status=WorkerExecutionStatus.PUBLISHED,
        request_id="e" * 32,
        with_proof=True,
    )
    effect = template.request.header.before.current_effect
    assert effect is not None and template.request.effect_link is not None
    evidence = freeze_json_payload(
        {"provider": "terminal-failure"},
        schema="test.outbox-terminal.v1",
    )
    terminal = WorkerTerminalProof(
        WorkerTerminalProofKind.TERMINAL_FAILURE,
        effect,
        evidence,
        template.request.effect_link,
    )
    request = dataclasses.replace(
        template.request,
        target_status=WorkerExecutionStatus.DEAD,
        error=evidence,
        effect_link=None,
        terminal_proof=terminal,
    )
    complete = _issue_worker_execution_plan(
        request,
        before_ledger=authorize.after_ledger,
    )
    projected = _issue_worker_result_projection_plan(complete)
    assert (
        writer.commit(complete, projected).status
        is SQLiteWorkerProjectionStatus.COMMITTED
    )
    return baseline, claim, authorize, complete


def _assert_corrupt(database, request_id="a" * 32) -> None:
    result = SQLiteWorkerDurableAuditor(database).inspect(request_id)
    assert result.status is SQLiteWorkerDurableAuditStatus.CORRUPT


def _generic_committed(tmp_path, name: str):
    baseline = create_canonical_mounted_business_database(tmp_path / name)
    plan = _generic_production_plan(baseline)
    assert (
        SQLiteProductionAtomicWriter(baseline.database).commit(plan).status
        is ProductionWriteStatus.COMMITTED
    )
    return baseline, plan


def test_multi_allocation_pipeline_action_genesis_is_one_exact_audit_root(
    tmp_path,
) -> None:
    baseline, plan = _generic_committed(
        tmp_path,
        "durable-audit-generic-exact.sqlite3",
    )
    result = SQLiteWorkerDurableAuditor(baseline.database).inspect("9" * 32)
    assert result.status is SQLiteWorkerDurableAuditStatus.ABSENT
    assert result.ledger_sequence == len(plan.atomic_write.control_plans)
    assert result.ledger_tail_sha256 == (
        plan.atomic_write.evidence.after_worker_ledger.tail_sha256
    )
    assert len(plan.evidence.allocations) == 2


def test_pipeline_transition_reissues_control_and_proves_existing_current(
    tmp_path,
) -> None:
    baseline, first = _generic_committed(
        tmp_path,
        "durable-audit-pipeline-transition.sqlite3",
    )
    transition = _pipeline_transition_plan(baseline, first)
    assert (
        SQLiteProductionAtomicWriter(baseline.database).commit(transition).status
        is ProductionWriteStatus.COMMITTED
    )
    result = SQLiteWorkerDurableAuditor(baseline.database).inspect("9" * 32)
    assert result.status is SQLiteWorkerDurableAuditStatus.ABSENT
    assert result.ledger_sequence == (
        transition.atomic_write.evidence.after_worker_ledger.sequence
    )
    assert result.ledger_tail_sha256 == (
        transition.atomic_write.evidence.after_worker_ledger.tail_sha256
    )


@pytest.mark.parametrize(
    ("table", "sql", "values"),
    (
        (
            "outbox_allocations",
            "UPDATE outbox_allocations SET ordinal=0 WHERE sequence=2",
            (),
        ),
        (
            "outbox_allocation_head",
            "UPDATE outbox_allocation_head SET state_revision=3 WHERE singleton=1",
            (),
        ),
        (
            "worker_target_current",
            "UPDATE worker_target_current SET state_revision=2 "
            "WHERE target_kind='action'",
            (),
        ),
        (
            "atomic_business_control_members",
            "UPDATE atomic_business_control_members SET binding_sha256=? "
            "WHERE ordinal=1",
            (b"m" * 32,),
        ),
    ),
)
def test_generic_allocation_control_and_action_tamper_is_corrupt(
    tmp_path,
    table,
    sql,
    values,
) -> None:
    baseline, _plan = _generic_committed(
        tmp_path,
        "durable-audit-generic-tamper-%s.sqlite3" % table,
    )
    _tamper(baseline.database, table, sql, values)
    _assert_corrupt(baseline.database, "9" * 32)


def test_action_genesis_rejects_mutable_legacy_drift(tmp_path) -> None:
    baseline, plan = _generic_committed(
        tmp_path,
        "durable-audit-action-genesis-mutable-tamper.sqlite3",
    )
    _tamper(
        baseline.database,
        "deferred_actions",
        "UPDATE deferred_actions SET next_attempt_at=? "
        "WHERE origin_commit_id=?",
        ("2099-01-01T00:00:00.000Z", plan.evidence.commit_id),
    )
    _assert_corrupt(baseline.database, "9" * 32)


@pytest.mark.parametrize(
    ("name", "sql", "values"),
    (
        (
            "controls-root",
            "UPDATE atomic_business_write_receipts "
            "SET controls_sha256=zeroblob(32) WHERE commit_id=?",
            (),
        ),
        (
            "inner-manifest",
            "UPDATE atomic_business_write_receipts "
            "SET manifest_bytes=CAST(manifest_bytes || x'20' AS BLOB) "
            "WHERE commit_id=?",
            (),
        ),
        (
            "inner-receipt-root",
            "UPDATE atomic_business_write_receipts SET receipt_sha256=? "
            "WHERE commit_id=?",
            (b"r" * 32,),
        ),
        (
            "target-root",
            "UPDATE atomic_business_write_receipts SET targets_sha256=? "
            "WHERE commit_id=?",
            (b"t" * 32,),
        ),
        (
            "outer-receipt-root",
            "UPDATE atomic_business_write_receipts "
            "SET production_receipt_sha256=? WHERE commit_id=?",
            (b"p" * 32,),
        ),
    ),
)
def test_atomic_business_receipt_roots_are_rebuilt_from_durable_facts(
    tmp_path,
    name,
    sql,
    values,
) -> None:
    baseline, plan = _generic_committed(
        tmp_path,
        "durable-audit-atomic-root-tamper-%s.sqlite3" % name,
    )
    _tamper(
        baseline.database,
        "atomic_business_write_receipts",
        sql,
        values + (plan.evidence.commit_id,),
    )
    _assert_corrupt(baseline.database, "9" * 32)


@pytest.mark.parametrize("mode", ("append", "same_length"))
def test_atomic_root_recomputes_raw_business_manifest_bytes(
    tmp_path,
    mode,
) -> None:
    baseline, plan = _generic_committed(
        tmp_path,
        "durable-audit-business-manifest-%s.sqlite3" % mode,
    )
    raw = sqlite3.connect(baseline.database.path)
    try:
        original = raw.execute(
            "SELECT manifest_bytes FROM business_commits WHERE commit_id=?",
            (plan.evidence.commit_id,),
        ).fetchone()[0]
    finally:
        raw.close()
    if mode == "append":
        changed = original + b" "
    else:
        changed = original.replace(b'"active"', b'"activa"', 1)
        assert len(changed) == len(original)
    assert changed != original
    _tamper(
        baseline.database,
        "business_commits",
        "UPDATE business_commits SET manifest_bytes=? WHERE commit_id=?",
        (changed, plan.evidence.commit_id),
    )

    _assert_corrupt(baseline.database, "9" * 32)


@pytest.mark.parametrize(
    "field",
    ("hash_domains", "mounted_authority", "scheduler_heads", "allocation"),
)
def test_outer_manifest_semantics_survive_self_consistent_rehash(
    tmp_path,
    field,
) -> None:
    baseline, plan = _generic_committed(
        tmp_path,
        "durable-audit-outer-self-rehash-%s.sqlite3" % field,
    )
    raw = sqlite3.connect(baseline.database.path)
    raw.row_factory = sqlite3.Row
    try:
        receipt = raw.execute(
            "SELECT * FROM atomic_business_write_receipts WHERE commit_id=?",
            (plan.evidence.commit_id,),
        ).fetchone()
    finally:
        raw.close()
    assert receipt is not None
    outer = decode_payload_bounded(
        receipt["production_manifest_bytes"],
        MAX_PRODUCTION_ATOMIC_MANIFEST_BYTES,
        MAX_PRODUCTION_ATOMIC_MANIFEST_NODES,
    )
    if field == "hash_domains":
        outer["hash_domains"]["manifest"] = "forged-domain"
    elif field == "mounted_authority":
        outer["mounted_authority"]["repository_revision"] += 1
    elif field == "scheduler_heads":
        outer["scheduler_heads"]["worker_ledger"]["state_revision"] += 1
    else:
        outer["outbox_allocation"]["before"]["state_revision"] += 1
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
            plan.evidence.commit_id.encode("ascii"),
            receipt["transaction_id"].encode("utf-8"),
            receipt["receipt_kind"].encode("utf-8"),
            receipt["committed_at"].encode("ascii"),
            receipt["receipt_sha256"],
            outer_sha256,
            receipt["target_count"].to_bytes(8, "big"),
            receipt["targets_sha256"],
        ),
    )
    _tamper(
        baseline.database,
        "atomic_business_write_receipts",
        "UPDATE atomic_business_write_receipts SET production_manifest_bytes=?,"
        "production_manifest_sha256=?,production_receipt_sha256=? "
        "WHERE commit_id=?",
        (
            outer_bytes,
            outer_sha256,
            production_receipt_sha256,
            plan.evidence.commit_id,
        ),
    )

    _assert_corrupt(baseline.database, "9" * 32)


def test_change_control_revision_cannot_be_self_reanchored(tmp_path) -> None:
    baseline, plan = _generic_committed(
        tmp_path,
        "durable-audit-control-revision-self-rehash.sqlite3",
    )
    raw = sqlite3.connect(baseline.database.path)
    raw.row_factory = sqlite3.Row
    try:
        business = raw.execute(
            "SELECT * FROM business_commits WHERE commit_id=?",
            (plan.evidence.commit_id,),
        ).fetchone()
        receipt = raw.execute(
            "SELECT * FROM atomic_business_write_receipts WHERE commit_id=?",
            (plan.evidence.commit_id,),
        ).fetchone()
    finally:
        raw.close()
    assert business is not None and receipt is not None

    business_manifest = decode_payload_bounded(
        business["manifest_bytes"],
        MAX_ATOMIC_MANIFEST_BYTES,
        MAX_ATOMIC_MANIFEST_NODES,
    )
    business_manifest["authority"]["control_revision"] += 1
    business_manifest_bytes = encode_payload_bounded(
        business_manifest,
        MAX_ATOMIC_MANIFEST_BYTES,
        MAX_ATOMIC_MANIFEST_NODES,
    )
    business_manifest_sha256 = _business_manifest_digest(
        business_manifest_bytes
    )

    inner = decode_payload_bounded(
        receipt["manifest_bytes"],
        MAX_ATOMIC_BUSINESS_WRITE_MANIFEST_BYTES,
        MAX_ATOMIC_BUSINESS_WRITE_MANIFEST_NODES,
    )
    inner["commit"]["atomic_manifest_sha256"] = (
        business_manifest_sha256.hex()
    )
    inner_bytes = encode_payload_bounded(
        inner,
        MAX_ATOMIC_BUSINESS_WRITE_MANIFEST_BYTES,
        MAX_ATOMIC_BUSINESS_WRITE_MANIFEST_NODES,
    )
    inner_sha256 = _atomic_manifest_digest(inner_bytes)
    inner_receipt_sha256 = _atomic_receipt_digest(
        commit_id=plan.evidence.commit_id,
        receipt_parent=AtomicBusinessWriteGenericReceiptEvidence(
            receipt["transaction_id"],
            receipt["receipt_kind"],
            receipt["committed_at"],
        ),
        atomic_manifest_sha256=business_manifest_sha256,
        pipeline_intents_sha256=receipt["pipeline_intents_sha256"],
        controls_sha256=receipt["controls_sha256"],
        control_count=receipt["control_count"],
        before_worker_ledger=WorkerLedgerHead(
            receipt["before_worker_sequence"],
            receipt["before_worker_tail_sha256"],
        ),
        after_worker_ledger=WorkerLedgerHead(
            receipt["after_worker_sequence"],
            receipt["after_worker_tail_sha256"],
        ),
        manifest_sha256=inner_sha256,
    )

    outer = decode_payload_bounded(
        receipt["production_manifest_bytes"],
        MAX_PRODUCTION_ATOMIC_MANIFEST_BYTES,
        MAX_PRODUCTION_ATOMIC_MANIFEST_NODES,
    )
    outer["pure_atomic"]["manifest_sha256"] = inner_sha256.hex()
    outer["pure_atomic"]["receipt_sha256"] = inner_receipt_sha256.hex()
    outer["mounted_authority"]["control_revision"] += 1
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
            plan.evidence.commit_id.encode("ascii"),
            receipt["transaction_id"].encode("utf-8"),
            receipt["receipt_kind"].encode("utf-8"),
            receipt["committed_at"].encode("ascii"),
            inner_receipt_sha256,
            outer_sha256,
            receipt["target_count"].to_bytes(8, "big"),
            receipt["targets_sha256"],
        ),
    )
    _tamper_many(
        baseline.database,
        ("business_commits", "atomic_business_write_receipts"),
        (
            (
                "UPDATE business_commits SET manifest_bytes=?,manifest_sha256=? "
                "WHERE commit_id=?",
                (
                    business_manifest_bytes,
                    business_manifest_sha256,
                    plan.evidence.commit_id,
                ),
            ),
            (
                "UPDATE atomic_business_write_receipts SET "
                "atomic_manifest_sha256=?,manifest_bytes=?,manifest_sha256=?,"
                "receipt_sha256=?,production_manifest_bytes=?,"
                "production_manifest_sha256=?,production_receipt_sha256=? "
                "WHERE commit_id=?",
                (
                    business_manifest_sha256,
                    inner_bytes,
                    inner_sha256,
                    inner_receipt_sha256,
                    outer_bytes,
                    outer_sha256,
                    production_receipt_sha256,
                    plan.evidence.commit_id,
                ),
            ),
        ),
    )

    _assert_corrupt(baseline.database, "9" * 32)


def test_self_consistent_outbox_target_rehash_is_corrupt(tmp_path) -> None:
    baseline, plan = _generic_committed(
        tmp_path,
        "durable-audit-target-self-rehash.sqlite3",
    )
    original = next(
        value
        for value in plan.targets
        if value.target.target_kind.value == "outbox"
    )
    changed_target = dataclasses.replace(
        original.target,
        idempotency_scope="attacker/self-consistent",
    )
    changed_genesis = _production_worker_genesis_state_sha256(
        changed_target,
        status=WorkerExecutionStatus.PENDING,
    )
    changed_wrapped = dataclasses.replace(
        original,
        target=changed_target,
        genesis_state_sha256=changed_genesis,
    )
    changed_targets = tuple(
        changed_wrapped if value is original else value
        for value in plan.targets
    )
    targets_sha256 = _production_targets_sha256(
        _production_groups(changed_targets)
    )
    raw = sqlite3.connect(baseline.database.path)
    raw.row_factory = sqlite3.Row
    try:
        receipt = raw.execute(
            "SELECT * FROM atomic_business_write_receipts WHERE commit_id=?",
            (plan.evidence.commit_id,),
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
            plan.evidence.commit_id.encode("ascii"),
            receipt["transaction_id"].encode("utf-8"),
            receipt["receipt_kind"].encode("utf-8"),
            receipt["committed_at"].encode("ascii"),
            receipt["receipt_sha256"],
            outer_sha256,
            receipt["target_count"].to_bytes(8, "big"),
            targets_sha256,
        ),
    )
    _tamper_many(
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
                    plan.evidence.commit_id,
                ),
            ),
        ),
    )

    _assert_corrupt(baseline.database, "9" * 32)


def test_complete_suffix_keeps_every_prior_identity_exact_and_absence_exact(
    tmp_path,
) -> None:
    baseline, claim, authorize, complete, _writer = _published(tmp_path)
    auditor = SQLiteWorkerDurableAuditor(baseline.database)

    for plan in (claim, authorize, complete):
        result = auditor.inspect(plan.closure.request_transaction_id)
        assert result.status is SQLiteWorkerDurableAuditStatus.EXACT
        assert result.identity is not None
        assert result.identity.operation == plan.operation.value
        assert result.ledger_sequence == complete.after_ledger.sequence
        assert result.ledger_tail_sha256 == complete.after_ledger.tail_sha256

    absent = auditor.inspect("9" * 32)
    assert absent.status is SQLiteWorkerDurableAuditStatus.ABSENT
    exact = auditor.inspect("a" * 32)
    assert exact.identity is not None
    conflicting = dataclasses.replace(
        exact.identity,
        receipt_sha256=b"x" * 32,
    )
    conflict = auditor.inspect("a" * 32, expected=conflicting)
    assert conflict.status is SQLiteWorkerDurableAuditStatus.CONFLICT


def test_no_proof_complete_then_reconcile_rebuilds_the_full_suffix(tmp_path) -> None:
    baseline, claim, authorize, _projection, writer = _commit_authorize(
        tmp_path,
        "durable-audit-reconcile.sqlite3",
    )
    unknown, projected = _complete_plan(
        baseline,
        authorize,
        status=WorkerExecutionStatus.OUTCOME_UNKNOWN,
        request_id="e" * 32,
        with_proof=False,
    )
    assert (
        writer.commit(unknown, projected).status
        is SQLiteWorkerProjectionStatus.COMMITTED
    )
    reconcile, projected = _reconcile_plan(baseline, unknown)
    assert (
        writer.commit(reconcile, projected).status
        is SQLiteWorkerProjectionStatus.COMMITTED
    )

    auditor = SQLiteWorkerDurableAuditor(baseline.database)
    for plan in (claim, authorize, unknown, reconcile):
        assert (
            auditor.inspect(plan.closure.request_transaction_id).status
            is SQLiteWorkerDurableAuditStatus.EXACT
        )


def test_terminal_proof_owns_provider_once_and_event_error_is_replayed(
    tmp_path,
) -> None:
    baseline, _claim, _authorize, complete = _terminal_dead(tmp_path)
    auditor = SQLiteWorkerDurableAuditor(baseline.database)
    assert (
        auditor.inspect(complete.closure.request_transaction_id).status
        is SQLiteWorkerDurableAuditStatus.EXACT
    )
    error = complete.request.error
    assert error is not None
    changed = error.payload_bytes.replace(b"failure", b"failurE")
    assert len(changed) == len(error.payload_bytes) and changed != error.payload_bytes
    _tamper(
        baseline.database,
        "event_outbox",
        "UPDATE event_outbox SET last_error_bytes=? WHERE event_id=?",
        (changed, complete.target.target_id),
    )
    _assert_corrupt(baseline.database)


@pytest.mark.parametrize(
    ("table", "sql", "values"),
    (
        (
            "worker_ledger_entries",
            "UPDATE worker_ledger_entries SET previous_tail_sha256=? "
            "WHERE sequence=1",
            (b"l" * 32,),
        ),
        (
            "worker_ledger_head",
            "UPDATE worker_ledger_head SET previous_tail_sha256=? "
            "WHERE singleton=1",
            (b"h" * 32,),
        ),
        (
            "worker_target_current",
            "UPDATE worker_target_current SET previous_state_sha256=?",
            (b"s" * 32,),
        ),
        (
            "worker_targets",
            "UPDATE worker_targets SET genesis_state_sha256=?",
            (b"g" * 32,),
        ),
        (
            "outbox_dispatch_head",
            "UPDATE outbox_dispatch_head SET previous_head_sha256=? "
            "WHERE singleton=1",
            (b"d" * 32,),
        ),
        (
            "outbox_dispatch_head",
            "UPDATE outbox_dispatch_head SET head_sha256=? WHERE singleton=1",
            (b"q" * 32,),
        ),
        (
            "outbox_dispatch_head",
            "UPDATE outbox_dispatch_head SET updated_at=? WHERE singleton=1",
            ("2026-07-12T00:00:00.001Z",),
        ),
        (
            "outbox_allocation_head",
            "UPDATE outbox_allocation_head SET previous_tail_sha256=? "
            "WHERE singleton=1",
            (b"o" * 32,),
        ),
    ),
)
def test_ledger_state_dispatch_and_allocation_heads_are_exactly_closed(
    tmp_path,
    table,
    sql,
    values,
) -> None:
    baseline, _claim, _authorize, _complete, _writer = _published(
        tmp_path,
        "durable-audit-head-%s-%s.sqlite3" % (table, values[0][:1]),
    )
    _tamper(baseline.database, table, sql, values)
    _assert_corrupt(baseline.database)


def test_same_length_manifest_and_origin_topic_tampering_cannot_hide(
    tmp_path,
) -> None:
    baseline, _production, claim = _claim_fixture(
        tmp_path,
        "durable-audit-manifest.sqlite3",
    )
    assert (
        SQLiteWorkerClaimWriter(baseline.database).commit(claim).status
        is SQLiteWorkerClaimStatus.COMMITTED
    )
    original = _one(
        baseline.database,
        "SELECT manifest_bytes FROM worker_execution_receipts "
        "WHERE request_id=?",
        ("a" * 32,),
    )[0]
    changed = original.replace(b'"claim"', b'"claiM"', 1)
    assert len(changed) == len(original) and changed != original
    _tamper(
        baseline.database,
        "worker_execution_receipts",
        "UPDATE worker_execution_receipts SET manifest_bytes=? "
        "WHERE request_id=?",
        (changed, "a" * 32),
    )
    _assert_corrupt(baseline.database)

    baseline, _production, claim = _claim_fixture(
        tmp_path,
        "durable-audit-origin.sqlite3",
    )
    assert (
        SQLiteWorkerClaimWriter(baseline.database).commit(claim).status
        is SQLiteWorkerClaimStatus.COMMITTED
    )
    _tamper(
        baseline.database,
        "event_outbox",
        "UPDATE event_outbox SET topic=? WHERE event_id=?",
        ("forged-topic", claim.target.target_id),
    )
    _assert_corrupt(baseline.database)


@pytest.mark.parametrize("tamper", ("topic", "current"))
def test_unconsumed_outbox_origin_and_genesis_are_still_in_audit_root(
    tmp_path,
    tamper,
) -> None:
    baseline, _production, claim = _claim_fixture(
        tmp_path,
        "durable-audit-unconsumed-%s.sqlite3" % tamper,
    )
    before = SQLiteWorkerDurableAuditor(baseline.database).inspect("9" * 32)
    assert before.status is SQLiteWorkerDurableAuditStatus.ABSENT
    if tamper == "topic":
        _tamper(
            baseline.database,
            "event_outbox",
            "UPDATE event_outbox SET topic=? WHERE event_id=?",
            ("forged-topic", claim.target.target_id),
        )
    else:
        _tamper(
            baseline.database,
            "worker_target_current",
            "UPDATE worker_target_current SET state_revision=2 "
            "WHERE target_id=?",
            (claim.target.target_id,),
        )

    _assert_corrupt(baseline.database, "9" * 32)


@pytest.mark.parametrize(
    ("table", "sql", "values"),
    (
        (
            "worker_target_current",
            "INSERT INTO worker_target_current SELECT ?,target_kind,status,"
            "state_revision,attempt_count,last_attempt_id,last_attempt_number,"
            "current_attempt_id,current_attempt_number,fence_high_water,"
            "cancellation_pending,next_attempt_at,lease_owner,lease_fence,"
            "lease_expires_at,current_effect_id,unresolved_effect_id,"
            "last_receipt_transaction_id,previous_state_sha256,state_sha256,"
            "updated_at FROM worker_target_current LIMIT 1",
            ("7" * 32,),
        ),
        (
            "worker_targets",
            "INSERT INTO worker_targets SELECT ?,target_kind,genesis_status,"
            "genesis_state_sha256,project_id,generation,document_id,?,"
            "source_ordinal,payload_schema,payload_codec_id,"
            "payload_codec_version,payload_bytes,payload_sha256,"
            "idempotency_scope,?,replay_policy,idempotency_policy,max_attempts,"
            "action_type,pipeline_id,intent_type,outbox_topic,"
            "outbox_partition_key,outbox_sequence+100,"
            "outbox_repository_revision+100,outbox_ordinal,created_at "
            "FROM worker_targets WHERE target_kind='outbox' LIMIT 1",
            ("7" * 32, "6" * 32, "orphan-idempotency-key"),
        ),
        (
            "event_outbox",
            "INSERT INTO event_outbox SELECT ?,previous_event_id,"
            "previous_ordinal,?,repository_revision+100,commit_event_count,?,"
            "ordinal,topic,partition_key,event_bytes,event_sha256,"
            "payload_schema,codec_id,codec_version,status,state_revision,"
            "attempt_count,available_at,lease_owner,lease_fence,"
            "lease_expires_at,published_at,outcome_unknown_at,dead_at,"
            "last_error_bytes,last_error_sha256,created_at,updated_at "
            "FROM event_outbox LIMIT 1",
            ("7" * 32, "6" * 32, "5" * 32),
        ),
    ),
)
def test_outbox_reverse_scope_rejects_orphan_mirrors(
    tmp_path,
    table,
    sql,
    values,
) -> None:
    baseline, _production, _claim = _claim_fixture(
        tmp_path,
        "durable-audit-orphan-%s.sqlite3" % table,
    )
    _tamper(baseline.database, table, sql, values)

    _assert_corrupt(baseline.database, "9" * 32)


@pytest.mark.parametrize("mode", ("missing", "same_length_payload"))
def test_effect_v9_sidecar_presence_and_raw_payload_are_exact(
    tmp_path,
    mode,
) -> None:
    baseline, _claim, authorize, _projection, _writer = _commit_authorize(
        tmp_path,
        "durable-audit-sidecar-%s.sqlite3" % mode,
    )
    transaction_id = authorize.expected_receipt.receipt_parent.transaction_id
    if mode == "missing":
        _tamper(
            baseline.database,
            "worker_effect_request_projections",
            "DELETE FROM worker_effect_request_projections "
            "WHERE authorization_transaction_id=?",
            (transaction_id,),
        )
    else:
        original = _one(
            baseline.database,
            "SELECT request_payload_bytes "
            "FROM worker_effect_request_projections "
            "WHERE authorization_transaction_id=?",
            (transaction_id,),
        )[0]
        changed = original.replace(b"9", b"8", 1)
        assert len(changed) == len(original) and changed != original
        _tamper(
            baseline.database,
            "worker_effect_request_projections",
            "UPDATE worker_effect_request_projections "
            "SET request_payload_bytes=? "
            "WHERE authorization_transaction_id=?",
            (changed, transaction_id),
        )
    _assert_corrupt(baseline.database)


def test_unsupported_legal_suffix_is_unknown_not_corrupt(tmp_path) -> None:
    baseline, _production, claim = _claim_fixture(
        tmp_path,
        "durable-audit-unsupported.sqlite3",
    )
    assert (
        SQLiteWorkerClaimWriter(baseline.database).commit(claim).status
        is SQLiteWorkerClaimStatus.COMMITTED
    )
    _persist_worker_execution(baseline.database, _expiry_after_claim(claim))

    result = SQLiteWorkerDurableAuditor(baseline.database).inspect("a" * 32)
    assert result.status is SQLiteWorkerDurableAuditStatus.UNKNOWN
    assert result.code == "worker_durable_audit_operation_unsupported"


def test_orphan_generic_worker_parent_is_corrupt(tmp_path) -> None:
    baseline, _production, claim = _claim_fixture(
        tmp_path,
        "durable-audit-orphan-parent.sqlite3",
    )
    assert (
        SQLiteWorkerClaimWriter(baseline.database).commit(claim).status
        is SQLiteWorkerClaimStatus.COMMITTED
    )
    _tamper(
        baseline.database,
        "transaction_receipts",
        "INSERT INTO transaction_receipts(transaction_id,kind,committed_at) "
        "VALUES (?,?,?)",
        (
            "_worker-execution:" + "7" * 32,
            "_worker_execution_v3",
            claim.expected_receipt.occurred_at,
        ),
    )
    _assert_corrupt(baseline.database)


@pytest.mark.parametrize("budget", ("entries", "bytes"))
def test_local_budget_exhaustion_is_unknown(tmp_path, monkeypatch, budget) -> None:
    baseline, _production, claim = _claim_fixture(
        tmp_path,
        "durable-audit-budget-%s.sqlite3" % budget,
    )
    assert (
        SQLiteWorkerClaimWriter(baseline.database).commit(claim).status
        is SQLiteWorkerClaimStatus.COMMITTED
    )
    if budget == "entries":
        monkeypatch.setattr(audit_module, "MAX_WORKER_DURABLE_AUDIT_ENTRIES", 0)
    else:
        monkeypatch.setattr(audit_module, "MAX_WORKER_DURABLE_AUDIT_BYTES", 0)
    result = SQLiteWorkerDurableAuditor(baseline.database).inspect("a" * 32)
    assert result.status is SQLiteWorkerDurableAuditStatus.UNKNOWN
    assert "budget_exceeded" in result.code


def test_fresh_publish_fence_change_and_read_failure_are_unknown(
    tmp_path,
    monkeypatch,
) -> None:
    baseline, _production, claim = _claim_fixture(
        tmp_path,
        "durable-audit-fence.sqlite3",
    )
    assert (
        SQLiteWorkerClaimWriter(baseline.database).commit(claim).status
        is SQLiteWorkerClaimStatus.COMMITTED
    )
    monkeypatch.setattr(
        SQLiteWorkerDurableAuditor,
        "_publish_fence",
        lambda self, expected: False,
    )
    changed = SQLiteWorkerDurableAuditor(baseline.database).inspect("a" * 32)
    assert changed.status is SQLiteWorkerDurableAuditStatus.UNKNOWN
    assert changed.code == "worker_durable_audit_publish_fence_changed"

    monkeypatch.undo()

    def fail_barrier(_database):
        raise OSError("simulated durability barrier failure")

    monkeypatch.setattr(
        type(baseline.database),
        "_finalize_committed_storage",
        fail_barrier,
    )
    barrier = SQLiteWorkerDurableAuditor(baseline.database).inspect("a" * 32)
    assert barrier.status is SQLiteWorkerDurableAuditStatus.UNKNOWN
    assert barrier.code == "worker_durable_audit_durability_barrier_failed"

    monkeypatch.undo()

    def unavailable(_connection):
        raise OSError("simulated storage read failure")

    monkeypatch.setattr(audit_module, "_mounted_row", unavailable)
    failed = SQLiteWorkerDurableAuditor(baseline.database).inspect("a" * 32)
    assert failed.status is SQLiteWorkerDurableAuditStatus.UNKNOWN
