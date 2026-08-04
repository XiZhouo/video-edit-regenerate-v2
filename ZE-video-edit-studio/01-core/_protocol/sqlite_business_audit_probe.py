"""Audit-only runtime evidence for the private SQLite business path.

This module deliberately lives under ``_protocol``.  It is neither a test
module nor a production adapter: audit gates use it to exercise the real
private runtime/writer/reconcile/reader without importing pytest fixtures or
giving server/bootstrap code an activation seam.
"""

from __future__ import annotations

import hashlib
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Tuple

import sqlite_business_reader as reader_module
import business_transaction_integrity as integrity_module
import sqlite_business_test_support as business_support
from business_snapshot import capture_business_snapshot, verify_business_snapshot
from business_transaction_contract import (
    ATOMIC_COMMIT_STATUS_ABORTED,
    ATOMIC_COMMIT_STATUS_COMMITTED,
    ATOMIC_COMMIT_STATUS_INDETERMINATE,
    ActionReplayPolicy,
    AtomicFailureKind,
    ConfirmationConsumption,
    DurableActionIntent,
    IdempotencyPolicy,
    InboxClaim,
    InboxOutcomeKind,
    OutboxEventIntent,
    PipelineIntent,
    PipelineIntentKind,
    PipelineReplayPolicy,
    freeze_json_payload,
)
from business_transaction_integrity import _issue_atomic_commit_plan
from document import Document
from entity import Entity
from operation_record_codec import freeze_operation_record
from sqlite_business_reader import SQLiteBusinessReadError, SQLiteBusinessReader
from sqlite_business_runtime import BusinessRuntimeAccess, inspect_business_runtime
from sqlite_business_test_support import (
    BusinessTestBaseline,
    add_active_business_test_project,
    create_active_business_database,
)
from sqlite_business_transaction import (
    BusinessCommitTestPoint,
    BusinessReconcileStatus,
    SQLiteBusinessTransactionWriter,
)


_UTC_1 = "2026-07-12T00:00:00.000Z"


@dataclass(frozen=True)
class WriterFaultEvidence:
    precommit_status: str
    precommit_failure_kind: str
    precommit_reconcile: str
    precommit_rows: Tuple[int, ...]
    exact_precommit_status: str
    exact_precommit_failure_kind: str
    exact_precommit_reconcile: str
    exact_precommit_rows: Tuple[int, ...]
    precommit_process_control_propagated: bool
    precommit_process_control_reconcile: str
    ack_status: str
    ack_recovered: bool
    ack_reconcile: str
    ack_rows: Tuple[int, ...]
    postcommit_process_control_propagated: bool
    postcommit_process_control_reconcile: str
    barrier_status: str
    barrier_failure_kind: str
    barrier_fresh_reconcile: str


@dataclass(frozen=True)
class RevisionEvidence:
    before_revisions: Tuple[int, int, int]
    committed_revisions: Tuple[int, int, int]
    current_revisions: Tuple[int, int, int]
    commit_status: str
    reconcile_status: str


@dataclass(frozen=True)
class ReaderEvidence:
    full_commit_status: str
    full_head_revisions: Tuple[int, int, int]
    full_counts: Tuple[int, int, int, int, int]
    claim_states: Tuple[str, str]
    rooted_snapshot_verified: bool
    pinned_commit_id: str
    pinned_revisions: Tuple[int, int, int]
    later_commit_id: str
    later_revisions: Tuple[int, int, int]
    baseline_runtime_identity_rejections: Tuple[bool, bool]
    change_runtime_identity_rejections: Tuple[bool, bool]


def _manifest_with_wrong_runtime_identity(
    raw: bytes,
    *,
    field: str,
    runtime_generation: str,
    code_digest: bytes,
) -> bytes:
    if field == "runtime_generation":
        needle = (
            b'"runtime_generation":"'
            + runtime_generation.encode("utf-8")
            + b'"'
        )
        replacement = b'"runtime_generation":"audit-reader-runtime-mismatch"'
    elif field == "code_digest":
        needle = b'"code_digest":"' + code_digest.hex().encode("ascii") + b'"'
        replacement = (
            b'"code_digest":"'
            + hashlib.sha256(b"audit-reader-code-mismatch").hexdigest().encode("ascii")
            + b'"'
        )
    else:
        raise AssertionError("unknown runtime identity field")
    changed = raw.replace(needle, replacement, 1)
    if changed == raw:
        raise AssertionError("runtime identity field was not present in manifest")
    return changed


def _reader_rejects_corrupt_manifest(baseline: BusinessTestBaseline) -> bool:
    try:
        SQLiteBusinessReader(baseline.database).read_current_head(
            baseline.project_id,
            baseline.generation,
        )
    except SQLiteBusinessReadError as exc:
        return exc.code == "sqlite_business_read_corrupt"
    return False


def _snapshot(
    baseline: BusinessTestBaseline,
    *,
    name: str,
    version: int,
    with_asset: bool = False,
) -> Any:
    schema = {}
    if with_asset:
        schema = {
            "card": {
                "type": "object",
                "properties": {"image": {"type": "asset_ref"}},
            },
        }
    document = Document(
        id=baseline.document_id,
        name=name,
        schema_version="1.0.0",
        collaboration_mode="with_save",
        save_policy="auto_commit",
        schema=schema,
    )
    if with_asset:
        entity = Entity(
            type="card",
            id="card-1",
            name="card",
            data={
                "image": {
                    "sha256": "9" * 64,
                    "mime": "image/png",
                    "size": 12,
                    "ext": "png",
                },
            },
        )
        document.add(entity)
        entity._created_at = 1
        entity._updated_at = version
    document._v = version
    document._created_at = 1
    document._updated_at = version
    return capture_business_snapshot(
        baseline.project_id,
        baseline.generation,
        document,
    ).snapshot


def _operation(
    baseline: BusinessTestBaseline,
    *,
    operation_id: str,
    trace_id: str,
    with_asset: bool,
) -> Any:
    if with_asset:
        after: Any = {
            "sha256": "9" * 64,
            "mime": "image/png",
            "size": 12,
            "ext": "png",
        }
        entity_type = "card"
        entity_id = "card-1"
        field = "image"
        patch_op = "create"
        reverse_op = "soft_delete"
    else:
        after = "changed"
        entity_type = "__document__"
        entity_id = baseline.document_id
        field = "name"
        patch_op = "name"
        reverse_op = "name"
    return freeze_operation_record(
        {
            "id": operation_id,
            "command_id": "card.create" if with_asset else "document.rename",
            "source": "user",
            "actor": {"user_id": "audit-user", "source": "user"},
            "ts": 1.2349,
            "patches": [
                {
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                    "field": field,
                    "op": patch_op,
                    "before": None if with_asset else "business baseline",
                    "after": after,
                },
            ],
            "reverse_patches": [
                {
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                    "field": field,
                    "op": reverse_op,
                    "before": after,
                    "after": None if with_asset else "business baseline",
                },
            ],
            "group_id": "",
            "trace_id": trace_id,
            "kind": "native",
            "error": None,
            "document_id": baseline.document_id,
        },
    )


def _seed_full_prerequisites(
    baseline: BusinessTestBaseline,
    request: Any,
    nonce_digest: bytes,
) -> None:
    connection, _filesystem = baseline.database._open_unvalidated()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT INTO asset_blobs(
                asset_sha256,content_key,media_type,extension,byte_length,
                status,state_revision,created_at,updated_at
            ) VALUES (?,?, 'image/png','png',12,'available',1,?,?)
            """,
            (bytes.fromhex("9" * 64), "sha256/99/" + "9" * 64, _UTC_1, _UTC_1),
        )
        connection.execute(
            """
            INSERT INTO idempotency_inbox(
                scope,idempotency_key,request_sha256,request_bytes,
                request_schema,request_codec_id,request_codec_version,
                actor_id,claim_owner_id,claim_fence,status,state_revision,
                lease_expires_at,claimed_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,'claimed',?,?,?,?)
            """,
            (
                "project/" + baseline.project_id,
                "audit-request-1",
                request.payload_sha256,
                request.payload_bytes,
                request.schema,
                request.codec_id,
                request.codec_version,
                "audit-user",
                "8" * 32,
                10,
                9,
                "2026-07-13T00:00:00.000Z",
                _UTC_1,
                _UTC_1,
            ),
        )
        connection.execute(
            """
            INSERT INTO confirmation_requests(
                confirmation_id,project_id,generation,document_id,actor_id,
                approval_nonce_hmac,nonce_key_id,request_bytes,request_sha256,
                request_schema,request_codec_id,request_codec_version,status,
                state_revision,reservation_owner_id,reservation_fence,
                reservation_expires_at,expires_at,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'reserved',?,?,?,?,?,?,?)
            """,
            (
                "9" * 32,
                baseline.project_id,
                baseline.generation,
                baseline.document_id,
                "audit-user",
                nonce_digest,
                "audit-confirmation-key-1",
                request.payload_bytes,
                request.payload_sha256,
                request.schema,
                request.codec_id,
                request.codec_version,
                3,
                "b" * 32,
                11,
                "2026-07-13T00:00:00.000Z",
                "2026-07-14T00:00:00.000Z",
                _UTC_1,
                _UTC_1,
            ),
        )
        connection.commit()
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()
    baseline.database._finalize_committed_storage()


def _plan(
    baseline: BusinessTestBaseline,
    *,
    commit_id: str,
    authority: Any = None,
    full: bool = False,
) -> Any:
    operation_id = "2" * 32
    trace_id = "3" * 32
    operation = _operation(
        baseline,
        operation_id=operation_id,
        trace_id=trace_id,
        with_asset=full,
    )
    event_payload = freeze_json_payload(
        {"operation_id": operation_id},
        schema="ze.entity-change.v1" if full else "ze.document-change.v1",
    )
    actions = ()
    pipelines = ()
    inbox = None
    confirmation = None
    if full:
        actions = (
            DurableActionIntent(
                0,
                "6" * 32,
                "notify.push",
                "audit-provider-request-1",
                freeze_json_payload(
                    {"channel": "review"}, schema="ze.action.notify.v1",
                ),
                replay_policy=ActionReplayPolicy.RECONCILE_BEFORE_RETRY,
                idempotency_policy=IdempotencyPolicy.DURABLE_RECEIPT_RECONCILE,
                idempotency_scope="provider/audit-account-1",
            ),
        )
        pipelines = (
            PipelineIntent(
                0,
                "7" * 32,
                "7" * 32,
                "audit-render-pipeline",
                "create",
                "audit-pipeline-create-1",
                freeze_json_payload(
                    {"name": "render"}, schema="ze.pipeline.create.v1",
                ),
                intent_kind=PipelineIntentKind.CREATE,
                replay_policy=PipelineReplayPolicy.EXACT_CAS_RETRY,
            ),
        )
        request = freeze_json_payload(
            {"command": "card.create"}, schema="ze.inbox.request.v1",
        )
        nonce_digest = hashlib.sha256(b"ze-audit-confirmation-nonce").digest()
        _seed_full_prerequisites(baseline, request, nonce_digest)
        inbox = InboxClaim(
            scope="project/" + baseline.project_id,
            inbox_key="audit-request-1",
            actor_id="audit-user",
            claim_owner_id="8" * 32,
            claim_fence=10,
            state_revision=9,
            outcome_kind=InboxOutcomeKind.COMMIT,
            request_sha256=request.payload_sha256,
            request=request,
            response=freeze_json_payload(
                {"commit_id": commit_id}, schema="ze.inbox.response.v1",
            ),
        )
        confirmation = ConfirmationConsumption(
            confirmation_id="9" * 32,
            nonce_hmac=nonce_digest,
            nonce_key_id="audit-confirmation-key-1",
            reservation_fence=11,
            reservation_owner_id="b" * 32,
            expected_revision=3,
            expected_request_sha256=request.payload_sha256,
            actor_id="audit-user",
            resolution=freeze_json_payload(
                {"decision": "confirmed", "commit_id": commit_id},
                schema="ze.confirmation.resolution.v1",
            ),
        )
    return _issue_atomic_commit_plan(
        authority=(
            baseline.profile.to_authority_binding()
            if authority is None
            else authority
        ),
        commit_id=commit_id,
        trace_id=trace_id,
        before=baseline.before_business_head(),
        after_snapshot=_snapshot(
            baseline,
            name="audit changed",
            version=2,
            with_asset=full,
        ),
        operations=(operation,),
        outbox=(
            OutboxEventIntent(
                0,
                "5" * 32,
                operation_id,
                "entity_change" if full else "document_change",
                baseline.project_id,
                event_payload,
            ),
        ),
        durable_actions=actions,
        pipeline_intents=pipelines,
        inbox=inbox,
        confirmation=confirmation,
    )


def _counts(database: Any, commit_id: str) -> Tuple[int, ...]:
    connection, _filesystem = database._open_unvalidated()
    try:
        queries = (
            "SELECT count(*) FROM business_commits WHERE commit_id=?",
            "SELECT count(*) FROM business_operations WHERE commit_id=?",
            "SELECT count(*) FROM business_operation_targets AS target "
            "JOIN business_operations AS operation "
            "ON operation.operation_id=target.operation_id WHERE operation.commit_id=?",
            "SELECT count(*) FROM event_outbox WHERE commit_id=?",
            "SELECT count(*) FROM deferred_actions WHERE origin_commit_id=?",
            "SELECT count(*) FROM pipeline_job_transitions WHERE origin_commit_id=?",
            "SELECT count(*) FROM document_asset_refs WHERE commit_id=?",
        )
        values = []
        for query in queries:
            row = connection.execute(
                query,
                (commit_id,),
            ).fetchone()
            values.append(int(row[0]))
        receipt = connection.execute(
            "SELECT count(*) FROM transaction_receipts WHERE transaction_id=?",
            ("ze-business:" + commit_id,),
        ).fetchone()
        values.append(int(receipt[0]))
        return tuple(values)
    finally:
        connection.close()


def _run_process_control_case(root: Path, *, after_commit: bool) -> Tuple[bool, str]:
    baseline = create_active_business_database(root / "repository.sqlite3")
    plan = _plan(baseline, commit_id=("d" if after_commit else "c") * 32)
    target = (
        BusinessCommitTestPoint.AFTER_COMMIT_BEFORE_ACK
        if after_commit
        else BusinessCommitTestPoint.BEFORE_COMMIT
    )

    def interrupt(point: BusinessCommitTestPoint) -> None:
        if point is target:
            raise KeyboardInterrupt()

    propagated = False
    writer = SQLiteBusinessTransactionWriter(baseline.database)
    try:
        writer.commit(plan, _test_hook=interrupt)
    except KeyboardInterrupt:
        propagated = True
    return propagated, writer.reconcile(plan).status.value


def _run_exact_precommit_corruption_case(
    root: Path,
) -> Tuple[str, str, str, Tuple[int, ...]]:
    """Inject one valid-shape wrong member before COMMIT; exact closure must roll back."""

    baseline = create_active_business_database(root / "repository.sqlite3")
    plan = _plan(baseline, commit_id="9" * 32)
    writer_type = SQLiteBusinessTransactionWriter
    original_descriptor = writer_type.__dict__["_write_outbox"]
    original_write_outbox = writer_type._write_outbox
    injected = []

    def write_wrong_outbox(connection: Any, active_plan: Any, committed_at: str) -> None:
        original_events = active_plan.outbox
        event = original_events[0]
        wrong_event = OutboxEventIntent(
            ordinal=event.ordinal,
            event_id=event.event_id,
            operation_id=event.operation_id,
            topic=event.topic,
            partition_key=event.partition_key,
            payload=freeze_json_payload(
                {"wrong_same_cardinality": True},
                schema=event.payload.schema,
            ),
        )
        object.__setattr__(active_plan, "outbox", (wrong_event,))
        try:
            original_write_outbox(connection, active_plan, committed_at)
            injected.append(wrong_event.payload.payload_sha256)
        finally:
            object.__setattr__(active_plan, "outbox", original_events)

    setattr(writer_type, "_write_outbox", staticmethod(write_wrong_outbox))
    try:
        writer = writer_type(baseline.database)
        result = writer.commit(plan)
    finally:
        setattr(writer_type, "_write_outbox", original_descriptor)
    if (
        len(injected) != 1
        or injected[0] == plan.outbox[0].payload.payload_sha256
        or result.failure is None
    ):
        raise AssertionError("same-cardinality precommit corruption was not injected")
    reconciled = writer_type(baseline.database).reconcile(plan)
    return (
        result.status,
        result.failure.kind.value,
        reconciled.status.value,
        _counts(baseline.database, plan.commit_id),
    )


def run_writer_fault_probe() -> WriterFaultEvidence:
    """Exercise rollback, ack-loss, process-control and durability edges."""

    with tempfile.TemporaryDirectory(prefix="ze-business-writer-audit-") as tmp:
        root = Path(tmp).resolve()

        aborted = create_active_business_database(root / "aborted" / "repository.sqlite3")
        aborted_plan = _plan(aborted, commit_id="a" * 32)

        def fail_before(point: BusinessCommitTestPoint) -> None:
            if point is BusinessCommitTestPoint.BEFORE_COMMIT:
                raise RuntimeError("audit precommit failure")

        aborted_writer = SQLiteBusinessTransactionWriter(aborted.database)
        aborted_result = aborted_writer.commit(aborted_plan, _test_hook=fail_before)
        aborted_reconcile = aborted_writer.reconcile(aborted_plan)

        (
            exact_precommit_status,
            exact_precommit_failure_kind,
            exact_precommit_reconcile,
            exact_precommit_rows,
        ) = _run_exact_precommit_corruption_case(root / "exact-precommit")

        ack = create_active_business_database(root / "ack" / "repository.sqlite3")
        ack_plan = _plan(ack, commit_id="b" * 32)

        def lose_ack(point: BusinessCommitTestPoint) -> None:
            if point is BusinessCommitTestPoint.AFTER_COMMIT_BEFORE_ACK:
                raise RuntimeError("audit acknowledgement loss")

        ack_writer = SQLiteBusinessTransactionWriter(ack.database)
        ack_result = ack_writer.commit(ack_plan, _test_hook=lose_ack)
        ack_reconcile = ack_writer.reconcile(ack_plan)

        pre_pc, pre_pc_reconcile = _run_process_control_case(
            root / "pre-process-control",
            after_commit=False,
        )
        post_pc, post_pc_reconcile = _run_process_control_case(
            root / "post-process-control",
            after_commit=True,
        )

        barrier = create_active_business_database(
            root / "barrier" / "repository.sqlite3",
        )
        barrier_plan = _plan(barrier, commit_id="e" * 32)
        original_barrier = barrier.database._finalize_committed_storage

        def fail_barrier() -> None:
            raise OSError("audit durability barrier failure")

        barrier.database._finalize_committed_storage = fail_barrier
        try:
            barrier_result = SQLiteBusinessTransactionWriter(
                barrier.database,
            ).commit(barrier_plan)
        finally:
            barrier.database._finalize_committed_storage = original_barrier
        barrier_reconcile = SQLiteBusinessTransactionWriter(
            barrier.database,
        ).reconcile(barrier_plan)

        assert aborted_result.status == ATOMIC_COMMIT_STATUS_ABORTED
        assert ack_result.status == ATOMIC_COMMIT_STATUS_COMMITTED
        assert barrier_result.status == ATOMIC_COMMIT_STATUS_INDETERMINATE
        return WriterFaultEvidence(
            precommit_status=aborted_result.status,
            precommit_failure_kind=aborted_result.failure.kind.value,
            precommit_reconcile=aborted_reconcile.status.value,
            precommit_rows=_counts(aborted.database, aborted_plan.commit_id),
            exact_precommit_status=exact_precommit_status,
            exact_precommit_failure_kind=exact_precommit_failure_kind,
            exact_precommit_reconcile=exact_precommit_reconcile,
            exact_precommit_rows=exact_precommit_rows,
            precommit_process_control_propagated=pre_pc,
            precommit_process_control_reconcile=pre_pc_reconcile,
            ack_status=ack_result.status,
            ack_recovered=ack_result.recovered_after_ack_loss,
            ack_reconcile=ack_reconcile.status.value,
            ack_rows=_counts(ack.database, ack_plan.commit_id),
            postcommit_process_control_propagated=post_pc,
            postcommit_process_control_reconcile=post_pc_reconcile,
            barrier_status=barrier_result.status,
            barrier_failure_kind=barrier_result.failure.kind.value,
            barrier_fresh_reconcile=barrier_reconcile.status.value,
        )


def run_revision_probe() -> RevisionEvidence:
    """Prove global progress does not relabel one project's local head."""

    with tempfile.TemporaryDirectory(prefix="ze-business-revision-audit-") as tmp:
        baseline = create_active_business_database(
            Path(tmp).resolve() / "repository.sqlite3",
            project_id="project-v7-a",
            document_id="document-v7-a",
            commit_id="1" * 32,
        )
        second = add_active_business_test_project(
            baseline.database,
            project_id="project-v7-b",
            document_id="document-v7-b",
            commit_id="2" * 32,
        )
        profile = second.profile
        plan = _plan(
            baseline,
            commit_id="f" * 32,
            authority=profile.to_authority_binding(),
        )
        writer = SQLiteBusinessTransactionWriter(baseline.database)
        committed = writer.commit(plan)
        reconciled = writer.reconcile(plan)

        connection, _filesystem = baseline.database._open_unvalidated()
        try:
            commit_row = connection.execute(
                """
                SELECT repository_revision, project_revision, head_revision
                FROM business_commits WHERE commit_id=?
                """,
                (plan.commit_id,),
            ).fetchone()
            current_row = connection.execute(
                """
                SELECT
                    (SELECT repository_revision FROM repository_runtime_control
                     WHERE singleton=1),
                    (SELECT project_revision FROM repository_projects
                     WHERE project_id=?),
                    (SELECT head_revision FROM document_commit_heads
                     WHERE project_id=? AND generation=?)
                """,
                (baseline.project_id, baseline.project_id, baseline.generation),
            ).fetchone()
        finally:
            connection.close()
        return RevisionEvidence(
            before_revisions=(
                plan.authority.authority_revision,
                plan.before.project_revision,
                plan.before.head_revision,
            ),
            committed_revisions=tuple(commit_row),
            current_revisions=tuple(current_row),
            commit_status=committed.status,
            reconcile_status=reconciled.status.value,
        )


def run_reader_probe() -> ReaderEvidence:
    """Read a full closure and prove pinned-snapshot linearization."""

    with tempfile.TemporaryDirectory(prefix="ze-business-reader-audit-") as tmp:
        root = Path(tmp).resolve()

        baseline_runtime_identity_rejections = []
        original_baseline_builder = business_support._build_manifest
        for field in ("runtime_generation", "code_digest"):
            def wrong_baseline_identity(*, _field: str = field, **kwargs: Any) -> bytes:
                return _manifest_with_wrong_runtime_identity(
                    original_baseline_builder(**kwargs),
                    field=_field,
                    runtime_generation=kwargs["runtime_generation"],
                    code_digest=kwargs["code_digest"],
                )

            business_support._build_manifest = wrong_baseline_identity
            try:
                mismatched_baseline = create_active_business_database(
                    root / ("baseline-" + field) / "repository.sqlite3",
                )
            finally:
                business_support._build_manifest = original_baseline_builder
            baseline_runtime_identity_rejections.append(
                _reader_rejects_corrupt_manifest(mismatched_baseline)
            )

        change_runtime_identity_rejections = []
        original_change_builder = integrity_module._build_manifest
        for field in ("runtime_generation", "code_digest"):
            mismatched_change = create_active_business_database(
                root / ("change-" + field) / "repository.sqlite3",
            )

            def wrong_change_identity(*, _field: str = field, **kwargs: Any) -> bytes:
                authority = kwargs["authority"]
                return _manifest_with_wrong_runtime_identity(
                    original_change_builder(**kwargs),
                    field=_field,
                    runtime_generation=authority.runtime_generation,
                    code_digest=authority.code_digest,
                )

            integrity_module._build_manifest = wrong_change_identity
            try:
                mismatched_plan = _plan(
                    mismatched_change,
                    commit_id="6" * 32,
                )
                mismatched_commit = SQLiteBusinessTransactionWriter(
                    mismatched_change.database,
                ).commit(mismatched_plan)
            finally:
                integrity_module._build_manifest = original_change_builder
            if mismatched_commit.status != ATOMIC_COMMIT_STATUS_COMMITTED:
                raise AssertionError("mismatched manifest audit commit did not commit")
            change_runtime_identity_rejections.append(
                _reader_rejects_corrupt_manifest(mismatched_change)
            )

        full = create_active_business_database(root / "full" / "repository.sqlite3")
        full_plan = _plan(full, commit_id="4" * 32, full=True)
        full_commit = SQLiteBusinessTransactionWriter(full.database).commit(full_plan)
        full_head = SQLiteBusinessReader(full.database).read_current_head(
            full.project_id,
            full.generation,
            expected_repository_revision=2,
            expected_project_revision=2,
            expected_head_revision=2,
            expected_commit_id=full_plan.commit_id,
        )
        connection, _filesystem = full.database._open_unvalidated()
        try:
            inbox_state = connection.execute(
                "SELECT status FROM idempotency_inbox WHERE idempotency_key=?",
                ("audit-request-1",),
            ).fetchone()[0]
            confirmation_state = connection.execute(
                "SELECT status FROM confirmation_requests WHERE confirmation_id=?",
                ("9" * 32,),
            ).fetchone()[0]
        finally:
            connection.close()

        linear = create_active_business_database(
            root / "linear" / "repository.sqlite3",
        )
        linear_plan = _plan(linear, commit_id="a" * 32)
        original_read_pinned = reader_module._read_pinned
        overlap_committed = False

        def read_then_commit(*args: Any, **kwargs: Any) -> Any:
            nonlocal overlap_committed
            result = original_read_pinned(*args, **kwargs)
            if not overlap_committed:
                overlap_committed = True
                outcome = SQLiteBusinessTransactionWriter(linear.database).commit(
                    linear_plan,
                )
                if outcome.status != ATOMIC_COMMIT_STATUS_COMMITTED:
                    raise AssertionError("overlapping audit commit did not commit")
            return result

        reader_module._read_pinned = read_then_commit
        try:
            pinned = SQLiteBusinessReader(linear.database).read_current_head(
                linear.project_id,
                linear.generation,
            )
        finally:
            reader_module._read_pinned = original_read_pinned
        later = SQLiteBusinessReader(linear.database).read_current_head(
            linear.project_id,
            linear.generation,
        )
        return ReaderEvidence(
            full_commit_status=full_commit.status,
            full_head_revisions=(
                full_head.repository_revision,
                full_head.project_revision,
                full_head.head_revision,
            ),
            full_counts=(
                full_head.operation_count,
                full_head.outbox_event_count,
                full_head.durable_action_count,
                full_head.pipeline_intent_count,
                full_head.asset_reference_count,
            ),
            claim_states=(inbox_state, confirmation_state),
            rooted_snapshot_verified=(
                verify_business_snapshot(full_head.snapshot) is full_head.snapshot
                and full_head.snapshot == full_plan.after_snapshot
            ),
            pinned_commit_id=pinned.commit_id,
            pinned_revisions=(
                pinned.repository_revision,
                pinned.project_revision,
                pinned.head_revision,
            ),
            later_commit_id=later.commit_id,
            later_revisions=(
                later.repository_revision,
                later.project_revision,
                later.head_revision,
            ),
            baseline_runtime_identity_rejections=tuple(
                baseline_runtime_identity_rejections
            ),
            change_runtime_identity_rejections=tuple(
                change_runtime_identity_rejections
            ),
        )


__all__ = ()
