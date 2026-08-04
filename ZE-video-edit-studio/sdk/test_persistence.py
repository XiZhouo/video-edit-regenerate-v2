from types import SimpleNamespace

from sdk import (
    ActionReceipt, ActionStatus, CommitExecution, CommitFailure, CommitReceipt, CommitStage, Document,
    DurabilityFacts, DurabilityState, EventStatus, SavePolicy,
    MutationBatch, new_operation, new_pending_confirmation,
)
from sdk.persistence import commit_execution_payload, persistence_payload


def _operation():
    return new_operation(
        command_id="test.write",
        source="user",
        actor={"user_id": "u1"},
        patches=[],
        reverse_patches=[],
        document_id="doc-1",
        trace_id="trace-1",
    )


def test_persistence_payload_reports_durable_autosave():
    doc = SimpleNamespace(effective_save_policy="auto_commit", version=7)

    assert persistence_payload(doc, document_durable=True) == {
        "status": "clean",
        "document_version": 7,
        "save_policy": "auto_commit",
        "document_durable": True,
        "retry_safe": False,
    }


def test_persistence_payload_does_not_call_legacy_dirty_state_saved():
    doc = SimpleNamespace(effective_save_policy="legacy", version=3)

    payload = persistence_payload(doc, document_durable=False)

    assert payload["status"] == "dirty"
    assert payload["document_durable"] is False
    assert payload["retry_safe"] is False


def test_w01_and_document_share_the_same_save_policy_wire_values():
    assert {policy.value for policy in SavePolicy} == {
        "auto_commit", "checkpoint_commit", "legacy",
    }
    for policy in SavePolicy:
        doc = Document(
            schema_version="1.0.0",
            collaboration_mode="live_collab",
            save_policy=policy.value,
        )
        assert doc.save_policy == policy.value


def test_commit_execution_payload_maps_committed_receipt_and_canonical_event():
    operation = _operation()
    receipt = CommitReceipt.committed(
        commit_id="commit-1",
        document_id="doc-1",
        operation_ids=(operation.id,),
        trace_id="trace-1",
        save_policy=SavePolicy.AUTO_COMMIT,
        document_version=8,
        event_status=EventStatus.PUBLISHED,
    )
    event = {
        "type": "entity_change",
        "doc_id": "doc-1",
        "op_id": operation.id,
        "commit_id": "commit-1",
        "document_version": 8,
        "changes": [],
    }
    execution = CommitExecution(
        result=operation,
        receipt=receipt,
        events=(event,),
    )

    payload = commit_execution_payload(
        execution,
        document=SimpleNamespace(effective_save_policy="auto_commit", version=8),
    )

    assert payload["op"]["id"] == operation.id
    assert payload["changes"] == event
    assert payload["persistence"] == receipt.to_dict()
    assert payload["persistence"]["status"] == "committed"
    assert payload["persistence"]["event_status"] == "published"


def test_commit_execution_payload_does_not_disclose_pipeline_authority_body(
    monkeypatch,
):
    operation = new_operation(
        command_id="pipeline.start",
        source="user",
        actor={"user_id": "u1"},
        patches=[],
        reverse_patches=[],
        document_id="doc-1",
        trace_id="trace-pipeline",
        pipeline={
            "schema": "ze.pipeline-start.v1",
            "run_id": "run-1",
            "work_list": {"prompt": "private execution material"},
        },
    )
    receipt = CommitReceipt.committed(
        commit_id="commit-pipeline",
        document_id="doc-1",
        operation_ids=(operation.id,),
        trace_id="trace-pipeline",
        save_policy=SavePolicy.AUTO_COMMIT,
        document_version=2,
        event_status=EventStatus.PUBLISHED,
    )
    execution = CommitExecution(result=operation, receipt=receipt)

    assert "pipeline" in operation.to_dict()

    def forbidden_full_projection(_self):
        raise AssertionError("public response must not copy the full Pipeline body")

    monkeypatch.setattr(type(operation), "to_dict", forbidden_full_projection)
    single = commit_execution_payload(
        execution,
        document=SimpleNamespace(effective_save_policy="auto_commit", version=2),
    )
    many = commit_execution_payload(
        execution,
        document=SimpleNamespace(effective_save_policy="auto_commit", version=2),
        many=True,
    )
    assert "pipeline" not in single["op"]
    assert "pipeline" not in many["ops"][0]
    assert "private execution material" not in repr(single)
    assert "private execution material" not in repr(many)


def test_commit_execution_payload_maps_aborted_without_claiming_changes():
    operation = _operation()
    failure = CommitFailure(
        stage=CommitStage.APPLY,
        code="apply_rolled_back",
        message="mutation failed and was rolled back",
        facts=DurabilityFacts(mutation_applied=True, rollback_succeeded=True),
    )
    receipt = CommitReceipt.from_failure(
        commit_id="commit-aborted",
        document_id="doc-1",
        operation_ids=(operation.id,),
        trace_id="trace-1",
        save_policy=SavePolicy.AUTO_COMMIT,
        failure=failure,
    )

    payload = commit_execution_payload(
        CommitExecution(result=operation, receipt=receipt),
        document=SimpleNamespace(effective_save_policy="auto_commit", version=7),
    )

    assert payload["op"]["id"] == operation.id
    assert "changes" not in payload
    assert payload["persistence"]["status"] == "aborted"
    assert payload["persistence"]["retry_safe"] is True
    assert payload["persistence"]["document_durable"] is False


def test_commit_execution_payload_maps_indeterminate_as_repair_required_fact():
    operation = _operation()
    failure = CommitFailure(
        stage=CommitStage.OPLOG,
        code="oplog_outcome_unknown",
        message="document durable but operation log outcome unknown",
        facts=DurabilityFacts(
            mutation_applied=True,
            document=DurabilityState.DURABLE,
            oplog=DurabilityState.UNKNOWN,
        ),
    )
    receipt = CommitReceipt.from_failure(
        commit_id="commit-indeterminate",
        document_id="doc-1",
        operation_ids=(operation.id,),
        trace_id="trace-1",
        save_policy=SavePolicy.AUTO_COMMIT,
        failure=failure,
        document_version=7,
    )

    payload = commit_execution_payload(
        CommitExecution(result=operation, receipt=receipt),
        document=SimpleNamespace(effective_save_policy="auto_commit", version=7),
    )

    assert payload["persistence"] == receipt.to_dict()
    assert payload["persistence"]["status"] == "indeterminate"
    assert payload["persistence"]["retry_safe"] is False
    assert payload["persistence"]["document_durable"] is True
    assert payload["persistence"]["oplog_durable"] is False
    assert "changes" not in payload


def test_commit_execution_payload_maps_noop_to_settled_compatibility_snapshot():
    document = SimpleNamespace(effective_save_policy="auto_commit", version=11)

    single = commit_execution_payload(
        CommitExecution(result=None), document=document,
    )
    many = commit_execution_payload(
        CommitExecution(result=None), document=document, many=True,
    )

    assert single == {
        "op": None,
        "persistence": {
            "status": "clean",
            "document_version": 11,
            "save_policy": "auto_commit",
            "document_durable": True,
            "retry_safe": False,
        },
    }
    assert many["ops"] == []
    assert many["changes"] == []
    assert many["persistence"]["status"] == "clean"


def test_commit_execution_payload_maps_pending_without_fake_persistence():
    pending = new_pending_confirmation(
        command_id="dangerous.delete",
        args={"id": "e1", "api_key": "sk-never-public"},
        source="user",
        actor={"user_id": "u1"},
        trace_id="trace-pending",
    )

    payload = commit_execution_payload(
        CommitExecution(result=pending),
        document=SimpleNamespace(effective_save_policy="auto_commit", version=5),
    )

    assert payload == {"pending": pending.to_public_dict()}
    assert "args" not in payload["pending"]
    assert "approval_nonce" not in payload["pending"]
    assert "sk-never-public" not in str(payload)
    assert "persistence" not in payload


def test_command_payload_preserves_primary_op_and_serializes_batch_actions():
    primary = _operation()
    placeholder = new_operation(
        command_id="entity.add", source="system", actor={"user_id": "__system__"},
        patches=[], reverse_patches=[], document_id="doc-1", trace_id="trace-1",
    )
    receipt = CommitReceipt.committed(
        commit_id="commit-batch",
        document_id="doc-1",
        operation_ids=(primary.id, placeholder.id),
        trace_id="trace-1",
        save_policy=SavePolicy.AUTO_COMMIT,
        document_version=12,
        event_status=EventStatus.PUBLISHED,
    )
    events = (
        {"type": "entity_change", "op_id": primary.id, "changes": []},
        {"type": "entity_change", "op_id": placeholder.id, "changes": []},
    )
    execution = CommitExecution(
        result=MutationBatch((primary, placeholder)),
        receipt=receipt,
        events=events,
        actions=(ActionReceipt(
            action_id="pipeline.start:run-1",
            name="pipeline.start",
            status=ActionStatus.SUCCEEDED,
        ),),
    )

    payload = commit_execution_payload(
        execution,
        document=SimpleNamespace(effective_save_policy="auto_commit", version=12),
    )

    assert payload["op"]["id"] == primary.id
    assert [operation["id"] for operation in payload["ops"]] == [primary.id, placeholder.id]
    assert [event["op_id"] for event in payload["changes"]] == [primary.id, placeholder.id]
    assert payload["action_status"] == "succeeded"
    assert payload["actions"] == [{
        "action_id": "pipeline.start:run-1",
        "name": "pipeline.start",
        "status": "succeeded",
    }]
