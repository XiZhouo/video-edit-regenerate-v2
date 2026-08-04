"""Closure, ordering and tamper tests for private AtomicCommitPlan issuance."""

from __future__ import annotations

import dataclasses
import hashlib
import inspect
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

import business_transaction_integrity as integrity_module  # noqa: E402
from business_snapshot import capture_business_snapshot  # noqa: E402
from business_transaction_contract import (  # noqa: E402
    ActionReplayPolicy,
    AuthorityBinding,
    BeforeBusinessHead,
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
from business_transaction_integrity import (  # noqa: E402
    BusinessTransactionIntegrityError,
    _issue_atomic_commit_plan,
    _require_atomic_commit_plan,
)
from document import Document  # noqa: E402
from entity import Entity  # noqa: E402
from operation_record_codec import freeze_operation_record  # noqa: E402


def _snapshot():
    document = Document(
        id="document-1",
        name="doc",
        schema_version="1",
        collaboration_mode="live_collab",
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
                "sha256": "9" * 64,
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
    return capture_business_snapshot("project-1", 5, document).snapshot


def _operation(operation_id: str = "1" * 32):
    return freeze_operation_record(
        {
            "id": operation_id,
            "command_id": "card.rename",
            "source": "user",
            "actor": {"user_id": "user-1", "source": "user"},
            "ts": 1.5,
            "patches": [
                {
                    "entity_type": "card",
                    "entity_id": "card-1",
                    "field": "name",
                    "op": "name",
                    "before": "old",
                    "after": "card",
                }
            ],
            "reverse_patches": [
                {
                    "entity_type": "card",
                    "entity_id": "card-1",
                    "field": "name",
                    "op": "name",
                    "before": "card",
                    "after": "old",
                }
            ],
            "group_id": "",
            "trace_id": "2" * 32,
            "kind": "native",
            "error": None,
            "document_id": "document-1",
        }
    )


def _parts():
    snapshot = _snapshot()
    operation = _operation()
    event_payload = freeze_json_payload(
        {"kind": "entity_change", "operation_id": operation.operation_id},
        schema="ze.entity-change.v1",
    )
    action_payload = freeze_json_payload(
        {"channel": "review"}, schema="ze.action.notify.v1"
    )
    pipeline_payload = freeze_json_payload(
        {"name": "render"}, schema="ze.pipeline.create.v1"
    )
    request = freeze_json_payload(
        {"command": "card.rename"}, schema="ze.inbox.request.v1"
    )
    return {
        "authority": AuthorityBinding(
            "sqlite", "active", "e" * 64, 7, 8, 4,
            "c" * 64, "runtime-1", b"d" * 32,
        ),
        "commit_id": "a" * 32,
        "trace_id": "2" * 32,
        "before": BeforeBusinessHead(
            "project-1", 5, "document-1", 1, 12, 4,
            "d" * 32, b"b" * 32, "0" * 64
        ),
        "after_snapshot": snapshot,
        "operations": (operation,),
        "outbox": (
            OutboxEventIntent(
                0,
                "3" * 32,
                operation.operation_id,
                "entity_change",
                "project-1/document-1",
                event_payload,
            ),
        ),
        "durable_actions": (
            DurableActionIntent(
                0,
                "4" * 32,
                "notify.push",
                "provider-request-1",
                action_payload,
                replay_policy=ActionReplayPolicy.RECONCILE_BEFORE_RETRY,
                idempotency_policy=IdempotencyPolicy.DURABLE_RECEIPT_RECONCILE,
                idempotency_scope="provider/account-1",
            ),
        ),
        "pipeline_intents": (
            PipelineIntent(
                0,
                "5" * 32,
                "5" * 32,
                "render-pipeline",
                "create",
                "pipeline-create-1",
                pipeline_payload,
                intent_kind=PipelineIntentKind.CREATE,
                replay_policy=PipelineReplayPolicy.EXACT_CAS_RETRY,
            ),
        ),
        "inbox": InboxClaim(
            scope="project/project-1",
            inbox_key="request-1",
            actor_id="user-1",
            claim_owner_id="8" * 32,
            claim_fence=10,
            state_revision=9,
            outcome_kind=InboxOutcomeKind.COMMIT,
            request_sha256=request.payload_sha256,
            request=request,
            response=freeze_json_payload(
                {"commit_id": "a" * 32}, schema="ze.inbox.response.v1"
            ),
        ),
        "confirmation": ConfirmationConsumption(
            confirmation_id="9" * 32,
            nonce_hmac=hashlib.sha256(b"confirmation-nonce-domain").digest(),
            nonce_key_id="confirmation-hmac-2026-01",
            reservation_fence=11,
            reservation_owner_id="b" * 32,
            expected_revision=3,
            expected_request_sha256=request.payload_sha256,
            actor_id="user-1",
            resolution=freeze_json_payload(
                {"commit_id": "a" * 32, "decision": "confirmed"},
                schema="ze.confirmation.resolution.v1",
            ),
        ),
    }


def test_plan_closes_every_atomic_member_and_candidate_head() -> None:
    plan = _issue_atomic_commit_plan(**_parts())
    assert _require_atomic_commit_plan(plan) is plan
    assert plan.candidate.project_id == "project-1"
    assert plan.candidate.document_version == 2
    assert plan.candidate.authority_revision == 9
    assert plan.candidate.project_revision == 13
    assert plan.candidate.head_revision == 5
    assert plan.candidate.operation_tail_hash == plan.operation_chain[-1].self_hash
    assert plan.closure.operation_count == 1
    assert plan.closure.operation_target_count == 1
    assert plan.operation_targets[0].operation_id == plan.operations[0].operation_id
    assert plan.operation_targets[0].patch_kind == "name"
    assert plan.closure.outbox_count == 1
    assert plan.closure.durable_action_count == 1
    assert plan.closure.pipeline_intent_count == 1
    assert plan.closure.asset_reference_count == 1
    assert plan.closure.before_repository_revision == 8
    assert plan.closure.after_repository_revision == 9
    assert plan.closure.before_project_revision == 12
    assert plan.closure.after_project_revision == 13
    assert plan.closure.before_head_revision == 4
    assert plan.closure.after_head_revision == 5
    assert plan.closure.after_snapshot_sha256 == plan.after_snapshot.project.snapshot_sha256
    assert plan.closure.asset_references_sha256 == plan.asset_references.projection_sha256
    assert len(plan.closure.manifest_sha256) == 32


def test_manifest_digest_has_fixed_vector() -> None:
    plan = _issue_atomic_commit_plan(**_parts())
    assert plan.closure.manifest_sha256.hex() == (
        "b59745c4e5dc18e1da8e8cbd975c7e671fc8fe1099e81be72531a6df4049eb60"
    )


def test_source_admission_revision_is_manifest_bound_and_tamper_evident() -> None:
    plan = _issue_atomic_commit_plan(**_parts())
    assert b'"source_admission_revision":7' in plan.closure.manifest_bytes
    object.__setattr__(
        plan,
        "authority",
        dataclasses.replace(plan.authority, source_admission_revision=9),
    )
    with pytest.raises(BusinessTransactionIntegrityError):
        _require_atomic_commit_plan(plan)


def test_global_project_and_head_revisions_are_independent_and_manifest_bound() -> None:
    plan = _issue_atomic_commit_plan(**_parts())
    assert b'"repository_revision":8' in plan.closure.manifest_bytes
    assert b'"repository_revision":9' in plan.closure.manifest_bytes
    assert b'"project_revision":12' in plan.closure.manifest_bytes
    assert b'"project_revision":13' in plan.closure.manifest_bytes
    assert b'"head_revision":4' in plan.closure.manifest_bytes
    assert b'"head_revision":5' in plan.closure.manifest_bytes
    object.__setattr__(
        plan,
        "before",
        dataclasses.replace(plan.before, project_revision=99),
    )
    with pytest.raises(BusinessTransactionIntegrityError):
        _require_atomic_commit_plan(plan)


def test_confirmation_request_resolution_and_budget_are_closed() -> None:
    plan = _issue_atomic_commit_plan(**_parts())
    confirmation = plan.confirmation
    assert confirmation is not None
    assert confirmation.expected_request_sha256.hex().encode() in plan.closure.manifest_bytes
    assert confirmation.resolution.payload_sha256.hex().encode() in plan.closure.manifest_bytes
    object.__setattr__(
        plan,
        "confirmation",
        dataclasses.replace(
            confirmation,
            resolution=freeze_json_payload(
                {"decision": "changed"},
                schema="ze.confirmation.resolution.v1",
            ),
        ),
    )
    with pytest.raises(BusinessTransactionIntegrityError):
        _require_atomic_commit_plan(plan)


def test_auxiliary_budget_fails_before_any_decode_or_projection(monkeypatch) -> None:
    parts = _parts()
    monkeypatch.setattr(
        integrity_module,
        "MAX_ATOMIC_TOTAL_AUXILIARY_BYTES",
        len(parts["operations"][0].body_bytes) - 1,
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("oversized carrier reached a decoding/projection seam")

    monkeypatch.setattr(integrity_module, "materialize_operation_body", forbidden)
    monkeypatch.setattr(integrity_module, "project_asset_references", forbidden)
    with pytest.raises(BusinessTransactionIntegrityError) as raised:
        _issue_atomic_commit_plan(**parts)
    assert raised.value.code == "atomic_commit_auxiliary_budget_exceeded"


def test_operation_target_total_budget_and_carrier_are_closed(monkeypatch) -> None:
    parts = _parts()
    second = _operation("6" * 32)
    parts["operations"] = (parts["operations"][0], second)
    first_event = parts["outbox"][0]
    parts["outbox"] = (
        first_event,
        dataclasses.replace(
            first_event,
            ordinal=1,
            event_id="7" * 32,
            operation_id=second.operation_id,
        ),
    )
    monkeypatch.setattr(integrity_module, "MAX_ATOMIC_OPERATION_TARGETS", 1)
    with pytest.raises(BusinessTransactionIntegrityError) as raised:
        _issue_atomic_commit_plan(**parts)
    assert raised.value.code == "atomic_commit_operation_target_count_exceeded"

    monkeypatch.setattr(integrity_module, "MAX_ATOMIC_OPERATION_TARGETS", 1_000_000)
    plan = _issue_atomic_commit_plan(**_parts())
    object.__setattr__(
        plan,
        "operation_targets",
        (dataclasses.replace(plan.operation_targets[0], field="changed"),),
    )
    with pytest.raises(BusinessTransactionIntegrityError):
        _require_atomic_commit_plan(plan)


def test_raw_dict_callable_wrong_order_and_duplicate_fail_closed() -> None:
    parts = _parts()
    parts["durable_actions"] = ({"run": lambda: None},)
    with pytest.raises(BusinessTransactionIntegrityError):
        _issue_atomic_commit_plan(**parts)

    parts = _parts()
    event = parts["outbox"][0]
    parts["outbox"] = (dataclasses.replace(event, ordinal=1),)
    with pytest.raises(BusinessTransactionIntegrityError):
        _issue_atomic_commit_plan(**parts)

    parts = _parts()
    parts["operations"] = (_operation("1" * 32), _operation("1" * 32))
    parts["outbox"] = (parts["outbox"][0],)
    with pytest.raises(BusinessTransactionIntegrityError):
        _issue_atomic_commit_plan(**parts)


def test_plan_tamper_and_wrong_before_head_are_detected() -> None:
    plan = _issue_atomic_commit_plan(**_parts())
    object.__setattr__(plan, "trace_id", "f" * 32)
    with pytest.raises(BusinessTransactionIntegrityError):
        _require_atomic_commit_plan(plan)

    parts = _parts()
    parts["before"] = dataclasses.replace(parts["before"], generation=6)
    with pytest.raises(BusinessTransactionIntegrityError):
        _issue_atomic_commit_plan(**parts)


def test_integrity_leaf_has_no_io_sqlite_sdk_or_document_dependency() -> None:
    source = inspect.getsource(integrity_module)
    for forbidden in (
        "import sqlite3",
        "from sqlite",
        "import os",
        "from pathlib",
        "import sdk",
        "from sdk",
        "from document",
        "from operation import",
    ):
        assert forbidden not in source


def test_snapshot_error_translation_does_not_retain_lower_exception() -> None:
    parts = _parts()
    parts["after_snapshot"] = object()
    with pytest.raises(BusinessTransactionIntegrityError) as raised:
        _issue_atomic_commit_plan(**parts)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
