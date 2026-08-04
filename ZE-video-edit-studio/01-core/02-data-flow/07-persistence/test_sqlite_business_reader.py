"""Focused historical schema-v7 evidence for the retired business reader."""

from __future__ import annotations

import hashlib
import sqlite3
import sys
from dataclasses import FrozenInstanceError
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

import sqlite_business_reader as reader_module  # noqa: E402
import sqlite_business_test_support as business_support  # noqa: E402
import sqlite_business_audit as business_audit_module  # noqa: E402
import sqlite_business_runtime as business_runtime_module  # noqa: E402
import sqlite_kernel as sqlite_kernel_module  # noqa: E402
import sqlite_schema as sqlite_schema_module  # noqa: E402
import business_transaction_integrity as integrity_module  # noqa: E402
from business_snapshot import capture_business_snapshot, verify_business_snapshot  # noqa: E402
from business_transaction_contract import (  # noqa: E402
    ActionReplayPolicy,
    DurableActionIntent,
    IdempotencyPolicy,
    OutboxEventIntent,
    PipelineIntent,
    PipelineIntentKind,
    PipelineReplayPolicy,
    freeze_json_payload,
)
from business_transaction_integrity import _issue_atomic_commit_plan  # noqa: E402
from document import Document  # noqa: E402
from entity import Entity  # noqa: E402
from operation_record_codec import freeze_operation_record  # noqa: E402
from sqlite_business_reader import (  # noqa: E402
    SQLiteBusinessHead,
    SQLiteBusinessReadError,
    SQLiteBusinessReader,
)
from sqlite_business_runtime import BusinessAuthorityState  # noqa: E402
from sqlite_business_test_support import (  # noqa: E402
    BusinessTestBaseline,
    add_historical_v7_active_business_test_project,
    create_historical_v7_active_business_database,
)
from sqlite_business_transaction import SQLiteBusinessTransactionWriter  # noqa: E402


_LATER = "2026-07-12T00:00:01.000Z"


@pytest.fixture(autouse=True)
def _freeze_retired_reader_to_historical_v7(monkeypatch):
    """Model one coherent v7 binary for initialization, reads, and audits."""

    historical = sqlite_schema_module.MIGRATIONS[:7]
    assert tuple(migration.version for migration in historical) == tuple(range(1, 8))
    for module in (
        sqlite_schema_module,
        sqlite_kernel_module,
        business_runtime_module,
        business_audit_module,
        business_support,
    ):
        monkeypatch.setattr(module, "MIGRATIONS", historical)


def _baseline(tmp_path: Path, **kwargs) -> BusinessTestBaseline:
    return create_historical_v7_active_business_database(
        tmp_path / "private" / "repository.sqlite3",
        **kwargs,
    )


def _read(baseline: BusinessTestBaseline, **kwargs) -> SQLiteBusinessHead:
    return SQLiteBusinessReader(baseline.database).read_current_head(
        baseline.project_id,
        baseline.generation,
        **kwargs,
    )


def _error(call, code: str) -> SQLiteBusinessReadError:
    with pytest.raises(SQLiteBusinessReadError) as caught:
        call()
    assert caught.value.code == code
    assert str(caught.value) == code
    return caught.value


def _raw(baseline: BusinessTestBaseline) -> sqlite3.Connection:
    connection = sqlite3.connect(str(baseline.database.path), isolation_level=None)
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _minimal_change_plan(baseline: BusinessTestBaseline):
    document = Document(
        id=baseline.document_id,
        name="changed",
        schema_version="1.0.0",
        collaboration_mode="with_save",
        save_policy="auto_commit",
        schema={},
    )
    document._v = 2
    document._created_at = 1
    document._updated_at = 2
    snapshot = capture_business_snapshot(
        baseline.project_id,
        baseline.generation,
        document,
    ).snapshot
    operation = freeze_operation_record(
        {
            "id": "a" * 32,
            "command_id": "document.rename",
            "source": "user",
            "actor": {"user_id": "user-1", "source": "user"},
            "ts": 1.5,
            "patches": [
                {
                    "entity_type": "__document__",
                    "entity_id": baseline.document_id,
                    "field": "name",
                    "op": "name",
                    "before": "business baseline",
                    "after": "changed",
                }
            ],
            "reverse_patches": [
                {
                    "entity_type": "__document__",
                    "entity_id": baseline.document_id,
                    "field": "name",
                    "op": "name",
                    "before": "changed",
                    "after": "business baseline",
                }
            ],
            "group_id": "",
            "trace_id": "b" * 32,
            "kind": "native",
            "error": None,
            "document_id": baseline.document_id,
        }
    )
    payload = freeze_json_payload(
        {"operation_id": operation.operation_id},
        schema="ze.document-change.v1",
    )
    return _issue_atomic_commit_plan(
        authority=baseline.profile.to_authority_binding(),
        commit_id="c" * 32,
        trace_id="b" * 32,
        before=baseline.before_business_head(),
        after_snapshot=snapshot,
        operations=(operation,),
        outbox=(
            OutboxEventIntent(
                0,
                "d" * 32,
                operation.operation_id,
                "document_change",
                baseline.project_id,
                payload,
            ),
        ),
    )


def test_reads_active_baseline_as_detached_immutable_root(tmp_path: Path) -> None:
    baseline = _baseline(tmp_path)

    result = _read(baseline)

    assert type(result) is SQLiteBusinessHead
    assert result.project_id == baseline.project_id
    assert result.generation == baseline.generation
    assert result.document_id == baseline.document_id
    assert result.document_version == baseline.document_version
    assert result.commit_id == baseline.commit_id
    assert result.project_revision == 1
    assert result.head_revision == 1
    assert result.repository_revision == 1
    assert result.operation_count == 0
    assert result.outbox_event_count == 0
    assert result.durable_action_count == 0
    assert result.pipeline_intent_count == 0
    assert result.asset_reference_count == 0
    assert result.snapshot == baseline.snapshot
    assert verify_business_snapshot(result.snapshot) is result.snapshot
    assert result.runtime == baseline.profile
    assert "path" not in result.__dict__
    assert "connection" not in result.__dict__
    assert "row" not in result.__dict__
    with pytest.raises(FrozenInstanceError):
        result.commit_id = "2" * 32  # type: ignore[misc]


def test_reader_accepts_exact_read_only_profile(tmp_path: Path) -> None:
    baseline = _baseline(
        tmp_path,
        authority_state=BusinessAuthorityState.READ_ONLY,
    )

    result = _read(baseline)

    assert result.runtime.authority_state is BusinessAuthorityState.READ_ONLY


def test_existing_head_remains_readable_after_legal_read_only_transition(
    tmp_path: Path,
) -> None:
    baseline = _baseline(tmp_path)
    connection = _raw(baseline)
    try:
        connection.execute(
            """
            UPDATE repository_runtime_control
            SET authority_state='read_only',state_revision=state_revision+1,
                updated_at=?
            WHERE singleton=1
            """,
            (_LATER,),
        )
    finally:
        connection.close()

    result = _read(baseline)

    assert result.commit_id == baseline.commit_id
    assert result.runtime.authority_state is BusinessAuthorityState.READ_ONLY
    assert result.runtime.state_revision == baseline.profile.state_revision + 1


def test_global_revision_may_advance_without_relabeling_this_project_head(
    tmp_path: Path,
) -> None:
    baseline = _baseline(tmp_path)
    second = add_historical_v7_active_business_test_project(
        baseline.database,
        project_id="project-v7-unrelated",
        document_id="document-v7-unrelated",
        commit_id="9" * 32,
    )

    result = _read(baseline, expected_repository_revision=2)

    assert second.profile.repository_revision == 2
    assert result.repository_revision == 2
    assert result.project_revision == 1
    assert result.head_revision == 1
    assert result.commit_id == baseline.commit_id


def test_reader_rejects_repair_required_profile(tmp_path: Path) -> None:
    baseline = _baseline(
        tmp_path,
        authority_state=BusinessAuthorityState.REPAIR_REQUIRED,
    )

    _error(
        lambda: _read(baseline),
        "sqlite_business_read_repair_required",
    )


@pytest.mark.parametrize(
    "kwargs,code",
    (
        ({"expected_repository_revision": 2}, "sqlite_business_read_revision_stale"),
        ({"expected_project_revision": 2}, "sqlite_business_read_revision_stale"),
        ({"expected_head_revision": 2}, "sqlite_business_read_revision_stale"),
        ({"expected_commit_id": "2" * 32}, "sqlite_business_read_revision_stale"),
    ),
)
def test_reader_rejects_stale_expected_head_fences(
    tmp_path: Path,
    kwargs,
    code: str,
) -> None:
    baseline = _baseline(tmp_path)

    _error(lambda: _read(baseline, **kwargs), code)


def test_reader_rejects_stale_generation_without_falling_back(tmp_path: Path) -> None:
    baseline = _baseline(tmp_path)

    _error(
        lambda: SQLiteBusinessReader(baseline.database).read_current_head(
            baseline.project_id,
            baseline.generation + 1,
        ),
        "sqlite_business_read_generation_stale",
    )


def test_reader_rejects_a_stale_expected_runtime_profile(tmp_path: Path) -> None:
    baseline = _baseline(tmp_path)
    changed = _baseline(
        tmp_path / "other",
        repository_epoch="a" * 64,
    )

    _error(
        lambda: _read(baseline, expected_runtime=changed.profile),
        "sqlite_business_read_authority_stale",
    )


def test_reader_detects_tampered_document_payload(tmp_path: Path) -> None:
    baseline = _baseline(tmp_path)
    connection = _raw(baseline)
    try:
        connection.execute(
            "UPDATE documents SET snapshot_bytes=? WHERE project_id=?",
            (b"{}", baseline.project_id),
        )
    finally:
        connection.close()

    _error(lambda: _read(baseline), "sqlite_business_read_corrupt")


def test_reader_detects_extra_scoped_entity_before_decoding_it(tmp_path: Path) -> None:
    baseline = _baseline(tmp_path)
    raw = b"not-a-valid-entity-envelope"
    connection = _raw(baseline)
    try:
        connection.execute(
            """
            INSERT INTO entities(
                project_id,document_id,entity_type,entity_id,entity_version,
                deleted,codec_id,codec_version,snapshot_format_version,
                snapshot_bytes,snapshot_sha256
            ) VALUES (?,?, 'card','extra',1,0,'ze.deterministic-json',1,1,?,?)
            """,
            (
                baseline.project_id,
                baseline.document_id,
                raw,
                hashlib.sha256(raw).digest(),
            ),
        )
    finally:
        connection.close()

    _error(lambda: _read(baseline), "sqlite_business_read_corrupt")


def test_reader_detects_missing_reserved_receipt(tmp_path: Path) -> None:
    baseline = _baseline(tmp_path)
    connection = sqlite3.connect(str(baseline.database.path), isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            "DELETE FROM transaction_receipts WHERE transaction_id=?",
            (baseline.transaction_id,),
        )
    finally:
        connection.close()

    _error(lambda: _read(baseline), "sqlite_business_read_corrupt")


def test_baseline_manifest_rejects_valid_hash_with_wrong_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        business_support,
        "_build_manifest",
        lambda **_kwargs: b'{"schema":"ze.business-baseline.v1"}',
    )
    baseline = _baseline(tmp_path)

    _error(lambda: _read(baseline), "sqlite_business_read_corrupt")


def test_baseline_manifest_rejects_wrong_exact_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = business_support._build_manifest

    def wrong_schema(**kwargs):
        raw = original(**kwargs)
        changed = raw.replace(
            b'"schema":"ze.business-baseline.v1"',
            b'"schema":"ze.business-baseline-unknown.v1"',
        )
        assert changed != raw
        return changed

    monkeypatch.setattr(business_support, "_build_manifest", wrong_schema)
    baseline = _baseline(tmp_path)

    _error(lambda: _read(baseline), "sqlite_business_read_corrupt")


def test_baseline_manifest_rejects_wrong_domain_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        business_support,
        "digest_business_baseline_manifest",
        lambda _raw: b"x" * 32,
    )
    baseline = _baseline(tmp_path)

    _error(lambda: _read(baseline), "sqlite_business_read_corrupt")


def test_baseline_manifest_cannot_claim_a_future_state_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = business_support._build_manifest

    def future_state(**kwargs):
        raw = original(**kwargs)
        changed = raw.replace(b'"state_revision":1', b'"state_revision":3')
        assert changed != raw
        return changed

    monkeypatch.setattr(business_support, "_build_manifest", future_state)
    baseline = _baseline(tmp_path)

    _error(lambda: _read(baseline), "sqlite_business_read_corrupt")


@pytest.mark.parametrize("field", ("runtime_generation", "code_digest"))
def test_baseline_manifest_rejects_nonexact_runtime_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    original = business_support._build_manifest

    def mismatched_identity(**kwargs):
        raw = original(**kwargs)
        if field == "runtime_generation":
            needle = (
                b'"runtime_generation":"'
                + kwargs["runtime_generation"].encode("utf-8")
                + b'"'
            )
            replacement = b'"runtime_generation":"reader-runtime-mismatch"'
        else:
            needle = (
                b'"code_digest":"'
                + kwargs["code_digest"].hex().encode("ascii")
                + b'"'
            )
            replacement = (
                b'"code_digest":"'
                + hashlib.sha256(b"reader-code-mismatch").hexdigest().encode("ascii")
                + b'"'
            )
        changed = raw.replace(needle, replacement, 1)
        assert changed != raw
        return changed

    monkeypatch.setattr(business_support, "_build_manifest", mismatched_identity)
    baseline = _baseline(tmp_path)

    _error(lambda: _read(baseline), "sqlite_business_read_corrupt")


def test_change_manifest_cannot_claim_a_future_control_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = _baseline(tmp_path)
    original = integrity_module._build_manifest

    def future_control(**kwargs):
        raw = original(**kwargs)
        changed = raw.replace(
            b'"control_revision":2',
            b'"control_revision":3',
        )
        assert changed != raw
        return changed

    monkeypatch.setattr(integrity_module, "_build_manifest", future_control)
    plan = _minimal_change_plan(baseline)
    committed = SQLiteBusinessTransactionWriter(baseline.database).commit(plan)
    assert committed.status == "committed"

    _error(lambda: _read(baseline), "sqlite_business_read_corrupt")


@pytest.mark.parametrize("field", ("runtime_generation", "code_digest"))
def test_change_manifest_rejects_nonexact_runtime_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    baseline = _baseline(tmp_path)
    original = integrity_module._build_manifest

    def mismatched_identity(**kwargs):
        raw = original(**kwargs)
        authority = kwargs["authority"]
        if field == "runtime_generation":
            needle = (
                b'"runtime_generation":"'
                + authority.runtime_generation.encode("utf-8")
                + b'"'
            )
            replacement = b'"runtime_generation":"reader-runtime-mismatch"'
        else:
            needle = (
                b'"code_digest":"'
                + authority.code_digest.hex().encode("ascii")
                + b'"'
            )
            replacement = (
                b'"code_digest":"'
                + hashlib.sha256(b"reader-code-mismatch").hexdigest().encode("ascii")
                + b'"'
            )
        changed = raw.replace(needle, replacement, 1)
        assert changed != raw
        return changed

    monkeypatch.setattr(integrity_module, "_build_manifest", mismatched_identity)
    plan = _minimal_change_plan(baseline)
    committed = SQLiteBusinessTransactionWriter(baseline.database).commit(plan)
    assert committed.status == "committed"

    _error(lambda: _read(baseline), "sqlite_business_read_corrupt")


def test_reader_detects_authority_change_before_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = _baseline(tmp_path)
    original = reader_module._read_pinned
    changed = False

    def read_then_change(*args, **kwargs):
        nonlocal changed
        result = original(*args, **kwargs)
        if not changed:
            changed = True
            connection = _raw(baseline)
            try:
                connection.execute(
                    """
                    UPDATE repository_runtime_control
                    SET authority_state='read_only',state_revision=state_revision+1,
                        updated_at=?
                    WHERE singleton=1
                    """,
                    (_LATER,),
                )
            finally:
                connection.close()
        return result

    monkeypatch.setattr(reader_module, "_read_pinned", read_then_change)

    _error(
        lambda: _read(baseline),
        "sqlite_business_read_authority_changed",
    )


def test_unrelated_global_progress_during_read_keeps_the_pinned_head_valid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = _baseline(tmp_path)
    original = reader_module._read_pinned
    advanced = False

    def read_then_advance_global(*args, **kwargs):
        nonlocal advanced
        result = original(*args, **kwargs)
        if not advanced:
            advanced = True
            add_historical_v7_active_business_test_project(
                baseline.database,
                project_id="project-v7-overlap",
                document_id="document-v7-overlap",
                commit_id="9" * 32,
            )
        return result

    monkeypatch.setattr(reader_module, "_read_pinned", read_then_advance_global)

    result = _read(baseline)

    assert result.repository_revision == 1
    assert result.project_revision == 1
    assert result.head_revision == 1
    assert result.commit_id == baseline.commit_id


def test_overlapping_same_project_commit_linearizes_after_the_pinned_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = _baseline(tmp_path)
    plan = _minimal_change_plan(baseline)
    original = reader_module._read_pinned
    committed = False

    def read_then_commit(*args, **kwargs):
        nonlocal committed
        result = original(*args, **kwargs)
        if not committed:
            committed = True
            outcome = SQLiteBusinessTransactionWriter(baseline.database).commit(plan)
            assert outcome.status == "committed"
        return result

    monkeypatch.setattr(reader_module, "_read_pinned", read_then_commit)

    pinned = _read(baseline)

    # The first BEGIN is the linearization point, so the overlapping writer is
    # ordered after this read even though it committed before the response.
    assert pinned.commit_id == baseline.commit_id
    assert pinned.repository_revision == 1
    assert pinned.project_revision == 1
    assert pinned.head_revision == 1

    monkeypatch.setattr(reader_module, "_read_pinned", original)
    later = _read(baseline)
    assert later.commit_id == plan.commit_id
    assert later.repository_revision == 2
    assert later.project_revision == 2
    assert later.head_revision == 2


def test_reader_input_validation_is_payload_free(tmp_path: Path) -> None:
    baseline = _baseline(tmp_path)
    reader = SQLiteBusinessReader(baseline.database)

    _error(
        lambda: reader.read_current_head("", 1),
        "sqlite_business_read_input_invalid",
    )
    _error(
        lambda: reader.read_current_head(baseline.project_id, 0),
        "sqlite_business_read_input_invalid",
    )
    _error(
        lambda: reader.read_current_head(
            baseline.project_id,
            1,
            expected_commit_id="secret-not-a-commit-id",
        ),
        "sqlite_business_read_input_invalid",
    )


def test_reader_proves_a_committed_change_and_every_declared_member_group(
    tmp_path: Path,
) -> None:
    baseline = _baseline(tmp_path)
    asset_hex = "9" * 64
    asset_digest = bytes.fromhex(asset_hex)
    document = Document(
        id=baseline.document_id,
        name="changed",
        schema_version="1.0.0",
        collaboration_mode="with_save",
        save_policy="auto_commit",
        schema={
            "card": {
                "type": "object",
                "properties": {"image": {"type": "asset_ref"}},
            }
        },
    )
    entity = Entity(
        type="card",
        id="card-1",
        name="card",
        data={
            "image": {
                "sha256": asset_hex,
                "mime": "image/png",
                "size": 12,
                "ext": "png",
            }
        },
    )
    document.add(entity)
    entity._created_at = 1
    entity._updated_at = 2
    document._v = 2
    document._created_at = 1
    document._updated_at = 2
    snapshot = capture_business_snapshot(
        baseline.project_id,
        baseline.generation,
        document,
    ).snapshot
    operation = freeze_operation_record(
        {
            "id": "2" * 32,
            "command_id": "card.create",
            "source": "user",
            "actor": {"user_id": "user-1", "source": "user"},
            "ts": 1.5,
            "patches": [
                {
                    "entity_type": "card",
                    "entity_id": "card-1",
                    "field": "image",
                    "op": "create",
                    "before": None,
                    "after": entity.data["image"],
                }
            ],
            "reverse_patches": [
                {
                    "entity_type": "card",
                    "entity_id": "card-1",
                    "field": "image",
                    "op": "soft_delete",
                    "before": entity.data["image"],
                    "after": None,
                }
            ],
            "group_id": "",
            "trace_id": "3" * 32,
            "kind": "native",
            "error": None,
            "document_id": baseline.document_id,
        }
    )
    event_payload = freeze_json_payload(
        {"operation_id": operation.operation_id},
        schema="ze.entity-change.v1",
    )
    action_payload = freeze_json_payload(
        {"channel": "review"},
        schema="ze.action.notify.v1",
    )
    pipeline_payload = freeze_json_payload(
        {"name": "render"},
        schema="ze.pipeline.create.v1",
    )
    plan = _issue_atomic_commit_plan(
        authority=baseline.profile.to_authority_binding(),
        commit_id="4" * 32,
        trace_id="3" * 32,
        before=baseline.before_business_head(),
        after_snapshot=snapshot,
        operations=(operation,),
        outbox=(
            OutboxEventIntent(
                0,
                "5" * 32,
                operation.operation_id,
                "entity_change",
                baseline.project_id,
                event_payload,
            ),
        ),
        durable_actions=(
            DurableActionIntent(
                0,
                "6" * 32,
                "notify.push",
                "provider-request-1",
                action_payload,
                replay_policy=ActionReplayPolicy.RECONCILE_BEFORE_RETRY,
                idempotency_policy=IdempotencyPolicy.DURABLE_RECEIPT_RECONCILE,
                idempotency_scope="provider/account-1",
            ),
        ),
        pipeline_intents=(
            PipelineIntent(
                0,
                "7" * 32,
                "7" * 32,
                "render-pipeline",
                "create",
                "pipeline-create-1",
                pipeline_payload,
                intent_kind=PipelineIntentKind.CREATE,
                replay_policy=PipelineReplayPolicy.EXACT_CAS_RETRY,
            ),
        ),
    )
    connection = _raw(baseline)
    try:
        connection.execute(
            """
            INSERT INTO asset_blobs(
                asset_sha256,content_key,media_type,extension,byte_length,
                status,state_revision,created_at,updated_at
            ) VALUES (?,?, 'image/png','png',12,'available',1,?,?)
            """,
            (
                asset_digest,
                "sha256/99/" + asset_hex,
                "2026-07-12T00:00:00.000Z",
                "2026-07-12T00:00:00.000Z",
            ),
        )
    finally:
        connection.close()

    committed = SQLiteBusinessTransactionWriter(baseline.database).commit(plan)
    assert committed.status == "committed"

    result = _read(
        baseline,
        expected_repository_revision=2,
        expected_project_revision=2,
        expected_head_revision=2,
        expected_commit_id=plan.commit_id,
    )
    assert result.snapshot == snapshot
    assert result.document_version == 2
    assert result.operation_count == 1
    assert result.outbox_event_count == 1
    assert result.durable_action_count == 1
    assert result.pipeline_intent_count == 1
    assert result.asset_reference_count == 1
