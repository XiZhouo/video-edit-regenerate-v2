"""Adversarial tests for the synchronous runtime-load coordinator."""

from __future__ import annotations

import ast
import os
import threading
from pathlib import Path

import pytest

from sdk import Actor, Document, DocumentRegistry, Entity, ZeError
from command import CommandRegistry, CommandSpec
from operation import new_operation
from sdk.runtime_rehydration import (
    AUTHORITY_WRITE_PROTOCOL,
    DetachedBusinessHead,
    RegistryDeclarationSnapshot,
    RuntimeAdmission,
    RuntimeAuthorityFenceReceipt,
    RuntimeAuthorityReceipt,
    _issue_unpublished_document_for_test,
    detach_restored_business_head,
    rehydrate_runtime as _runtime_rehydrate,
    snapshot_registry_declarations,
)


CARD_SCHEMA = {
    "type": "object",
    "properties": {"title": {"type": "string", "required": True}},
}


def _registry(
    *,
    schema=CARD_SCHEMA,
    mode="live_collab",
    policy="auto_commit",
    migrations=(),
    sources=(),
    collaboration_unit=True,
    mutation_policy=None,
):
    registry = DocumentRegistry()
    registry.declare_entity_type(
        name="card", schema=schema, collaboration_unit=collaboration_unit,
        mutation_policy=mutation_policy,
    )
    registry.set_collaboration_mode(mode)
    if policy is not None:
        registry.set_save_policy(policy)
    for migration in migrations:
        registry.declare_open_migration(migration)
    for name, entity_type, loader_path in sources:
        registry.declare_read_only_source(
            name=name, entity_type=entity_type, loader_path=loader_path,
        )
    return registry


def _declarations(
    registry=None, *, schema_version="1", runtime_generation="test-runtime",
):
    return snapshot_registry_declarations(
        registry or _registry(),
        schema_version=schema_version,
        runtime_generation=runtime_generation,
    )


def _document(registry=None, *, title="current", schema_version="1"):
    registry = registry or _registry()
    document = registry.build_document(
        schema_version=schema_version, id="document",
    )
    document.add(Entity(type="card", id="local", data={"title": title}))
    return document


def _restore(document, *, project_id="project", generation=1):
    from business_snapshot import capture_business_snapshot, restore_business_snapshot

    snapshot = capture_business_snapshot(
        project_id, generation, document,
    ).snapshot
    return restore_business_snapshot(snapshot)


def _detach(document, *, project_id="project", generation=1):
    restored = _restore(
        document, project_id=project_id, generation=generation,
    )
    return restored, detach_restored_business_head(restored)


def _receipt(request, outcome="committed", transaction_id="tx-1", **changes):
    values = {
        "request_identity": request.request_identity,
        "outcome": outcome,
        "protocol": request.protocol,
        "project_id": request.project_id,
        "generation": request.generation,
        "before_business_root": request.before_business_root,
        "before_document_version": request.before_document_version,
        "after_business_root": request.after_business_root,
        "after_document_version": request.after_document_version,
        "transaction_id": transaction_id,
    }
    values.update(changes)
    return RuntimeAuthorityReceipt(**values)


def _current_fence(request):
    return RuntimeAuthorityFenceReceipt(
        request_identity=request.request_identity,
        outcome="current",
        protocol=request.protocol,
        project_id=request.project_id,
        generation=request.generation,
        business_root=request.business_root,
        document_version=request.document_version,
        declaration_digest=request.declaration_digest,
        runtime_generation=request.runtime_generation,
        authority_revision="revision-1",
    )


def rehydrate_runtime(*args, **kwargs):
    kwargs.setdefault("authority_fence", _current_fence)
    return _runtime_rehydrate(*args, **kwargs)


def require_runtime_ready_head(value):
    return value.document


def _committed_writer(seen=None):
    def writer(request):
        if seen is not None:
            seen.append(request)
        return _receipt(request)

    return writer


def test_registry_snapshot_is_issued_detached_complete_and_digest_bound():
    def migration(document):
        return False

    registry = _registry(
        migrations=(migration,),
        sources=(
            ("catalog-a", "card", "memory://a"),
            ("catalog-b", "card", "memory://b"),
        ),
        collaboration_unit=True,
    )
    declarations = _declarations(registry, schema_version="2")
    first_digest = declarations.digest
    registry._entity_types["card"]["schema"]["properties"]["title"]["type"] = "integer"
    registry._open_migrations.clear()

    assert declarations.schema_version == "2"
    assert declarations.schema == {"card": CARD_SCHEMA}
    assert declarations.mutation_policies == {}
    assert declarations.collaboration_units == (("card", True),)
    assert declarations.migrations == (migration,)
    assert [source.name for source in declarations.read_only_sources] == [
        "catalog-a", "catalog-b",
    ]
    assert declarations.digest == first_digest
    first = declarations.schema
    first["card"]["properties"]["title"]["type"] = "boolean"
    assert declarations.schema == {"card": CARD_SCHEMA}


def _card_mutation_policy(*, max_total=1):
    return {
        "schema": "ze.entity-mutation-policy.v1",
        "lifecycle": {
            "create": ["Card.Create"],
            "rename": ["Card.Rename"],
            "soft_delete": ["Card.Delete"],
            "restore": ["Card.Restore"],
            "hard_remove": ["Card.Remove"],
        },
        "fields": {"title": ["Card.Update"]},
        "cardinality": {"max_total": max_total},
    }


def _register_card_policy_commands(commands, calls=None):
    for command_id in (
        "Card.Create",
        "Card.Rename",
        "Card.Delete",
        "Card.Restore",
        "Card.Remove",
        "Card.Update",
    ):
        def execute(*, actor, source, _command_id=command_id, **_kwargs):
            if calls is not None:
                calls.append(_command_id)
            return new_operation(
                command_id=_command_id,
                source=source,
                actor=actor.to_dict(),
                patches=(),
                reverse_patches=(),
            )

        commands.register(CommandSpec(
            id=command_id,
            label=command_id,
            mutation_authority=True,
            execute=execute,
        ))


def test_mutation_policy_is_detached_and_bound_into_declaration_digest():
    policy = _card_mutation_policy()
    declarations = _declarations(_registry(mutation_policy=policy))
    baseline = _declarations(_registry())

    policy["fields"]["title"].append("Forged")
    detached = declarations.mutation_policies
    detached["card"]["fields"]["title"].append("GetterForged")

    assert declarations.mutation_policies == {
        "card": _card_mutation_policy(),
    }
    assert declarations.digest != baseline.digest


def test_policy_only_rehydration_changes_no_business_head_or_version():
    old_registry = _registry()
    restored, detached = _detach(_document(old_registry))
    before_version = restored.document.version
    from business_snapshot import capture_business_snapshot

    before = capture_business_snapshot("project", 1, restored.document).snapshot
    writes = []
    admission = rehydrate_runtime(
        detached,
        _declarations(_registry(mutation_policy=_card_mutation_policy())),
        authority_writer=lambda request: writes.append(request),
    )
    after = capture_business_snapshot("project", 1, admission.document).snapshot

    assert writes == []
    assert admission.authority_proof == "canonical_noop"
    assert admission.document_version == before_version
    assert after == before
    assert admission.document.mutation_policies == {
        "card": _card_mutation_policy(),
    }


def test_rehydration_discards_old_registry_seal_and_requires_fresh_runtime_binding():
    policy = _card_mutation_policy(max_total=2)
    old_commands = CommandRegistry()
    _register_card_policy_commands(old_commands)
    original = _document(_registry(mutation_policy=policy))
    original._validate_mutation_policy_commands(old_commands)
    old_registry_authority = original._mutation_policy_registry_authority
    old_command_authorities = tuple(
        original._mutation_policy_command_authorities.values()
    )

    _, detached = _detach(original)
    runtime = require_runtime_ready_head(rehydrate_runtime(
        detached,
        _declarations(_registry(mutation_policy=policy)),
    ))

    # A business snapshot carries policy declarations, never opaque process-local
    # command authority. Rehydration is passive until the new runtime seals itself.
    assert runtime._mutation_policy_sealed is False
    assert runtime._mutation_policy_registry_authority is None
    assert runtime._mutation_policy_registry_epoch is None
    assert runtime._mutation_policy_registry_pid is None
    assert runtime._mutation_policy_command_authorities == {}

    calls = []
    new_commands = CommandRegistry()
    _register_card_policy_commands(new_commands, calls)
    with pytest.raises(ZeError) as unsealed:
        new_commands.dispatch(
            "Card.Update", {}, actor=Actor(user_id="u1"), source="user",
            doc=runtime,
        )
    assert unsealed.value.context["reason"] == "registry_authority_not_sealed"
    assert calls == []

    runtime._validate_mutation_policy_commands(new_commands)
    assert runtime._mutation_policy_registry_authority is new_commands
    assert runtime._mutation_policy_registry_authority is not old_registry_authority
    assert runtime._mutation_policy_registry_pid == os.getpid()
    assert all(
        current is not previous
        for current, previous in zip(
            runtime._mutation_policy_command_authorities.values(),
            old_command_authorities,
        )
    )

    new_commands.dispatch(
        "Card.Update", {}, actor=Actor(user_id="u1"), source="user",
        doc=runtime,
    )
    assert calls == ["Card.Update"]


def test_migration_may_repair_cardinality_before_final_validation():
    old_registry = _registry()
    document = _document(old_registry)
    document.add(Entity(type="card", id="extra", data={"title": "extra"}))
    restored, detached = _detach(document)
    before_version = restored.document.version

    def repair(doc):
        doc.hard_remove("card", "extra")
        return True

    target = _registry(
        mutation_policy=_card_mutation_policy(max_total=1),
        migrations=(repair,),
    )
    writes = []
    admission = rehydrate_runtime(
        detached,
        _declarations(target),
        authority_writer=_committed_writer(writes),
    )

    assert len(writes) == 1
    assert admission.document_version == before_version + 1
    assert admission.document.find("card", "extra") is None
    admission.document._validate_mutation_policy_cardinality()


def test_unrepaired_cardinality_rolls_back_runtime_authority_install():
    old_registry = _registry()
    document = _document(old_registry)
    document.add(Entity(type="card", id="extra", data={"title": "extra"}))
    restored, detached = _detach(document)
    from business_snapshot import capture_business_snapshot

    before = capture_business_snapshot("project", 1, restored.document).snapshot

    with pytest.raises(ZeError) as captured:
        rehydrate_runtime(
            detached,
            _declarations(
                _registry(mutation_policy=_card_mutation_policy(max_total=1)),
            ),
        )

    assert captured.value.code == "mutation_cardinality_violation"
    assert capture_business_snapshot("project", 1, restored.document).snapshot == before
    assert restored.document.mutation_policies == {}


def test_registry_snapshot_rejects_unknown_state_and_forged_handle():
    registry = _registry()
    registry._future_runtime_port = object()
    with pytest.raises(ZeError) as unknown:
        _declarations(registry)
    assert unknown.value.context["reason"] == "runtime_registry_state_invalid"

    forged = RegistryDeclarationSnapshot()
    with pytest.raises(ZeError) as carrier:
        rehydrate_runtime(
            _detach(_document())[1], forged,
        )
    assert carrier.value.context["reason"] == "runtime_declaration_carrier_invalid"


def test_read_only_source_must_use_declared_unified_schema():
    registry = _registry()
    registry.declare_read_only_source(
        name="foreign", entity_type="undeclared", loader_path="memory://foreign",
    )
    with pytest.raises(ZeError) as error:
        _declarations(registry)
    assert error.value.context["reason"] == "runtime_read_only_entity_type_undeclared"


def test_runtime_generation_is_required_and_bound_into_declaration_digest():
    registry = _registry()
    with pytest.raises(ZeError) as error:
        snapshot_registry_declarations(
            registry, schema_version="1", runtime_generation="",
        )
    assert error.value.context["reason"] == "runtime_generation_invalid"
    first = _declarations(registry, runtime_generation="deployment-a")
    second = _declarations(registry, runtime_generation="deployment-b")
    assert first.runtime_generation == "deployment-a"
    assert first.digest != second.digest


def test_noop_rehydration_preserves_version_and_historical_dirty_data():
    registry = _registry()
    document = registry.build_document(schema_version="1", id="document")
    dirty = Entity.from_dict({
        "id": "dirty", "type": "card", "data": {"title": 42},
    })
    document._add_entity_internal(dirty)
    restored, detached = _detach(document)
    before_version = restored.document.version
    before_updated_at = restored.document.updated_at

    called = []
    ready = rehydrate_runtime(
        detached,
        _declarations(registry),
        authority_writer=lambda request: called.append(request),
    )
    assert ready.authority_proof == "canonical_noop"
    assert ready.document_version == before_version
    assert called == []
    runtime = require_runtime_ready_head(ready)
    assert runtime.updated_at == before_updated_at
    assert runtime.get("card", "dirty").data == {"title": 42}
    with pytest.raises(ZeError) as error:
        runtime.get("card", "dirty").update(
            {"title": 43},
            expected_version=runtime.get("card", "dirty").version,
            actor=Actor(user_id="u"),
        )
    assert error.value.code == "schema_violation"


def test_changed_declarations_advance_exactly_one_version_and_bind_cas():
    old_registry = _registry(schema={}, policy="checkpoint_commit")
    restored, detached = _detach(_document(old_registry))
    before = restored.document.version
    before_updated_at = restored.document.updated_at
    seen = []

    ready = rehydrate_runtime(
        detached,
        _declarations(_registry(), schema_version="2"),
        authority_writer=_committed_writer(seen),
    )
    assert len(seen) == 1
    request = seen[0]
    assert request.protocol == AUTHORITY_WRITE_PROTOCOL
    assert request.project_id == "project"
    assert request.generation == 1
    assert request.before_document_version == before
    assert request.after_document_version == before + 1
    assert request.before_business_root != request.after_business_root
    assert request.business_snapshot.document.document_version == before + 1
    assert ready.document_version == before + 1
    assert ready.authority_proof == "authority_commit:tx-1"

    admission = ready
    runtime = admission.document
    assert runtime.version == before + 1
    assert runtime.updated_at >= before_updated_at
    assert runtime.schema_version == "2"
    assert runtime.save_policy == "auto_commit"
    assert runtime.schema["card"] == CARD_SCHEMA
    assert admission.project_id == "project"
    assert admission.generation == 1
    assert admission.authority_revision == "revision-1"
    assert admission.declarations.schema_version == "2"
    assert admission.collaboration_units == (("card", True),)


def test_rehydrate_returns_manifest_bound_admission_and_forgery_is_rejected():
    admission = rehydrate_runtime(
        _detach(_document())[1], _declarations(),
    )
    runtime = admission.document
    assert type(runtime) is Document
    with pytest.raises(ZeError) as forged:
        _ = RuntimeAdmission().document
    assert forged.value.context["reason"] == "runtime_admission_invalid"


@pytest.mark.parametrize("changed", [False, True])
def test_final_authority_fence_blocks_stale_head_before_admission(changed):
    registry = _registry(schema={}) if changed else _registry()
    restored, detached = _detach(_document(registry))

    def advanced(request):
        return RuntimeAuthorityFenceReceipt(
            request_identity=request.request_identity,
            outcome="advanced",
            protocol=request.protocol,
            project_id=request.project_id,
            generation=request.generation,
            business_root=b"a" * 32,
            document_version=request.document_version + 1,
            declaration_digest=request.declaration_digest,
            runtime_generation=request.runtime_generation,
            authority_revision="revision-advanced",
        )

    with pytest.raises(ZeError) as error:
        _runtime_rehydrate(
            detached,
            _declarations(
                _registry(), schema_version="2" if changed else "1",
            ),
            authority_fence=advanced,
            authority_writer=_committed_writer() if changed else None,
        )
    assert error.value.code == "runtime_authority_head_advanced"
    if changed:
        assert restored.document.version == 2
        retry = _issue_unpublished_document_for_test(
            restored.document, project_id="project", generation=1,
        )
        with pytest.raises(ZeError) as quarantined:
            rehydrate_runtime(retry, _declarations(_registry(), schema_version="2"))
        assert quarantined.value.context["reason"] == "runtime_document_quarantined"
    else:
        assert restored.document.version == 1
        retry = _issue_unpublished_document_for_test(
            restored.document, project_id="project", generation=1,
        )
        ready = rehydrate_runtime(retry, _declarations())
        assert require_runtime_ready_head(ready) is restored.document


def test_final_fence_rejects_runtime_manifest_generation_mismatch():
    restored, detached = _detach(_document())

    def mismatched(request):
        return RuntimeAuthorityFenceReceipt(
            request_identity=request.request_identity,
            outcome="current",
            protocol=request.protocol,
            project_id=request.project_id,
            generation=request.generation,
            business_root=request.business_root,
            document_version=request.document_version,
            declaration_digest=b"x" * 32,
            runtime_generation="other-deployment",
            authority_revision="revision-other",
        )

    with pytest.raises(ZeError) as error:
        _runtime_rehydrate(
            detached,
            _declarations(runtime_generation="deployment-a"),
            authority_fence=mismatched,
        )
    assert error.value.context["reason"] == "runtime_authority_fence_receipt_invalid"
    assert restored.document.version == 1


def test_missing_writer_rolls_back_every_canonical_change():
    old_registry = _registry(schema={}, policy="checkpoint_commit")
    restored, detached = _detach(_document(old_registry))
    before_version = restored.document.version
    before_schema = restored.document.schema
    before_policy = restored.document.save_policy

    with pytest.raises(ZeError) as error:
        rehydrate_runtime(detached, _declarations(_registry(), schema_version="2"))
    assert error.value.context["reason"] == "runtime_authority_writer_missing"
    assert restored.document.version == before_version
    assert restored.document.schema == before_schema
    assert restored.document.save_policy == before_policy


def test_not_durable_receipt_rolls_back_and_preserves_stable_error():
    old_registry = _registry(schema={})
    restored, detached = _detach(_document(old_registry))

    with pytest.raises(ZeError) as error:
        rehydrate_runtime(
            detached,
            _declarations(_registry(), schema_version="2"),
            authority_writer=lambda request: _receipt(
                request, outcome="not_durable", transaction_id="",
            ),
        )
    assert error.value.code == "runtime_authority_not_durable"
    assert restored.document.version == 1
    assert restored.document.schema["card"] == {}


@pytest.mark.parametrize(
    "writer_kind", ["unknown", "raise", "mismatch", "unhashable_outcome"],
)
def test_unknown_or_invalid_authority_outcome_quarantines_without_rollback(writer_kind):
    old_registry = _registry(schema={})
    restored, detached = _detach(_document(old_registry))

    if writer_kind == "unknown":
        writer = lambda request: _receipt(
            request, outcome="unknown", transaction_id="",
        )
    elif writer_kind == "raise":
        def writer(request):
            raise OSError("lost response")
    elif writer_kind == "unhashable_outcome":
        writer = lambda request: _receipt(request, outcome=[])
    else:
        writer = lambda request: _receipt(
            request, after_business_root=b"x" * 32,
        )

    with pytest.raises(ZeError) as error:
        rehydrate_runtime(
            detached,
            _declarations(_registry(), schema_version="2"),
            authority_writer=writer,
        )
    assert error.value.code == "runtime_authority_outcome_unknown"
    # It may already be durable: never move memory behind the candidate head.
    assert restored.document.version == 2
    assert restored.document.schema["card"] == CARD_SCHEMA

    another = _issue_unpublished_document_for_test(
        restored.document, project_id="project", generation=1,
    )
    with pytest.raises(ZeError) as quarantined:
        rehydrate_runtime(another, _declarations(_registry(), schema_version="2"))
    assert quarantined.value.context["reason"] == "runtime_document_quarantined"


def test_migration_new_invalid_data_is_rejected_and_rolled_back():
    def migration(document):
        document.get("card", "local")._data["title"] = 7
        return True

    restored, detached = _detach(_document())
    called = []
    with pytest.raises(ZeError) as error:
        rehydrate_runtime(
            detached,
            _declarations(_registry(migrations=(migration,))),
            authority_writer=lambda request: called.append(request),
        )
    assert error.value.code == "schema_violation"
    assert restored.document.get("card", "local").data == {"title": "current"}
    assert restored.document.version == 1
    assert called == []


def test_only_changed_rows_are_revalidated_under_final_schema():
    registry = _registry()
    document = registry.build_document(schema_version="1", id="document")
    dirty = Entity.from_dict({
        "id": "dirty", "type": "card", "data": {"title": 42},
    })
    document._add_entity_internal(dirty)
    document.add(Entity(type="card", id="clean", data={"title": "before"}))

    def migration(runtime):
        runtime.get("card", "clean")._data["title"] = "after"
        return True

    restored, detached = _detach(document)
    ready = rehydrate_runtime(
        detached,
        _declarations(_registry(migrations=(migration,))),
        authority_writer=_committed_writer(),
    )
    runtime = require_runtime_ready_head(ready)
    assert runtime.get("card", "dirty").data == {"title": 42}
    assert runtime.get("card", "clean").data == {"title": "after"}


@pytest.mark.parametrize(
    ("migration", "reason"),
    [
        (lambda document: 1, "runtime_migration_result_not_bool"),
        (
            lambda document: (
                document.get("card", "local")._data.__setitem__("title", "changed")
                or False
            ),
            "runtime_migration_claim_mismatch",
        ),
        (lambda document: True, "runtime_migration_claim_mismatch"),
    ],
)
def test_migration_result_and_claimed_delta_are_strict_and_atomic(migration, reason):
    restored, detached = _detach(_document())
    entity = restored.document.get("card", "local")
    before = entity.to_dict()

    with pytest.raises(ZeError) as error:
        rehydrate_runtime(
            detached, _declarations(_registry(migrations=(migration,))),
        )
    assert error.value.context["reason"] == reason
    assert restored.document.get("card", "local") is entity
    assert entity.to_dict() == before


def test_migration_may_add_canonical_row_but_not_replace_authority():
    def add_row(document):
        document.add(Entity(type="card", id="new", data={"title": "new"}))
        return True

    ready = rehydrate_runtime(
        _detach(_document())[1],
        _declarations(_registry(migrations=(add_row,))),
        authority_writer=_committed_writer(),
    )
    runtime = require_runtime_ready_head(ready)
    assert runtime.get("card", "new").data == {"title": "new"}

    def replace_authority(document):
        document._schema = {"card": {}}
        return True

    restored, detached = _detach(_document())
    with pytest.raises(ZeError) as authority:
        rehydrate_runtime(
            detached,
            _declarations(_registry(migrations=(replace_authority,))),
        )
    assert authority.value.context["reason"] == "runtime_migration_authority_mutation"
    assert restored.document.schema["card"] == CARD_SCHEMA


@pytest.mark.parametrize(
    ("member", "value", "reason"),
    [
        ("_v", 9, "runtime_document_version_authority_mutation"),
        ("_id", "other", "runtime_migration_document_identity_mutation"),
        ("_created_at", 0.0, "runtime_migration_document_identity_mutation"),
    ],
)
def test_migration_cannot_own_document_identity_or_revision(member, value, reason):
    def migration(document):
        setattr(document, member, value)
        return True

    restored, detached = _detach(_document())
    with pytest.raises(ZeError) as error:
        rehydrate_runtime(
            detached, _declarations(_registry(migrations=(migration,))),
        )
    assert error.value.context["reason"] == reason
    assert restored.document.id == "document"
    assert restored.document.version == 1


def test_migration_cannot_persist_live_document_during_exclusive_load(tmp_path):
    target = tmp_path / "leak" / "document.json"

    def migration(document):
        document.save(target)
        return False

    restored, detached = _detach(_document())
    with pytest.raises(ZeError) as error:
        rehydrate_runtime(
            detached, _declarations(_registry(migrations=(migration,))),
        )
    assert error.value.code == "document_save_forbidden"
    assert not target.parent.exists()
    assert restored.document.version == 1


def test_restore_head_mutation_before_detach_is_rejected():
    restored = _restore(_document())
    restored.document.get("card", "local")._data["title"] = "not durable"
    with pytest.raises(ZeError) as error:
        detach_restored_business_head(restored)
    assert error.value.context["reason"] == "business_snapshot_restored_head_changed"


def test_detached_head_mutation_before_rehydrate_is_rejected_and_one_shot():
    restored, detached = _detach(_document())
    restored.document.get("card", "local")._data["title"] = "late"
    with pytest.raises(ZeError) as error:
        rehydrate_runtime(detached, _declarations())
    assert error.value.context["reason"] == "runtime_detached_head_changed"
    with pytest.raises(ZeError) as consumed:
        rehydrate_runtime(detached, _declarations())
    assert consumed.value.context["reason"] == "runtime_detached_head_consumed"


def test_same_document_cannot_reenter_pending_ready_or_admitted_lifecycle(monkeypatch):
    import sdk.runtime_rehydration as module

    document = _document(_registry(schema={}))
    first = _issue_unpublished_document_for_test(
        document, project_id="project", generation=1,
    )
    entered = threading.Event()
    release = threading.Event()

    def writer(request):
        entered.set()
        assert release.wait(5)
        return _receipt(request)

    result = {}

    def run():
        result["ready"] = rehydrate_runtime(
            first,
            _declarations(_registry(), schema_version="2"),
            authority_writer=writer,
        )

    thread = threading.Thread(target=run)
    thread.start()
    assert entered.wait(5)
    second = _issue_unpublished_document_for_test(
        document, project_id="project", generation=1,
    )
    with pytest.raises(ZeError) as pending:
        rehydrate_runtime(second, _declarations(_registry(), schema_version="2"))
    assert pending.value.context["reason"] == "runtime_rehydration_already_pending"
    release.set()
    thread.join(5)
    assert not thread.is_alive()
    admission = result["ready"]

    third = _issue_unpublished_document_for_test(
        document, project_id="project", generation=1,
    )
    with pytest.raises(ZeError) as admitted_error:
        rehydrate_runtime(third, _declarations(_registry(), schema_version="2"))
    assert admitted_error.value.context["reason"] == "runtime_document_already_admitted"
    assert admission.document is document


def test_outer_mutation_session_blocks_runtime_load_and_rolls_back_cleanly():
    restored, detached = _detach(_document())
    outer = restored.document.begin_mutation_session()
    with pytest.raises(ZeError) as error:
        rehydrate_runtime(detached, _declarations())
    assert error.value.code == "mutation_session_busy"
    outer.rollback()
    exclusive = restored.document.begin_mutation_session(exclusive=True)
    exclusive.rollback()


def test_authority_commit_precedes_overlay_loading_and_overlay_supports_same_bucket():
    sources = (
        ("catalog-a", "card", "memory://a"),
        ("catalog-b", "card", "memory://b"),
    )
    old_registry = _registry(schema={})
    restored, detached = _detach(_document(old_registry))
    order = []

    def writer(request):
        order.append("authority")
        assert restored.document.read_only_sources() == []
        return _receipt(request)

    def resolver(source):
        order.append("resolve:" + source.name)
        suffix = source.name[-1]
        return lambda: [{
            "id": "external-" + suffix,
            "data": {"title": "external " + suffix},
        }]

    ready = rehydrate_runtime(
        detached,
        _declarations(_registry(sources=sources), schema_version="2"),
        authority_writer=writer,
        read_only_loader_resolver=resolver,
    )
    assert order == ["authority", "resolve:catalog-a", "resolve:catalog-b"]
    runtime = require_runtime_ready_head(ready)
    assert runtime.read_only_sources() == ["catalog-a", "catalog-b"]
    assert runtime.get("card", "external-a").read_only is True
    assert runtime.get("card", "external-b").read_only is True


@pytest.mark.parametrize("tamper", ["source_spec", "bucket_order"])
def test_later_loader_cannot_mutate_earlier_source_state(tamper):
    sources = (
        ("catalog-a", "card", "memory://a"),
        ("catalog-b", "card", "memory://b"),
    )
    restored, detached = _detach(_document())

    def resolver(source):
        if source.name == "catalog-a":
            return lambda: [
                {"id": "external-a1", "data": {"title": "external a1"}},
                {"id": "external-a2", "data": {"title": "external a2"}},
            ]

        def second_loader():
            if tamper == "source_spec":
                restored.document._read_only_sources["catalog-a"].name = "forged-a"
                restored.document._read_only_sources["catalog-a"].entity_type = "other"
            else:
                bucket = restored.document._collections["card"]
                first = bucket.pop("external-a1")
                bucket["external-a1"] = first
            return [{
                "id": "external-b", "data": {"title": "external b"},
            }]
        return second_loader

    with pytest.raises(ZeError) as error:
        rehydrate_runtime(
            detached,
            _declarations(_registry(sources=sources)),
            read_only_loader_resolver=resolver,
        )
    assert error.value.context["reason"] == "runtime_read_only_loader_mutated_existing_state"
    assert restored.document.read_only_sources() == []
    assert restored.document.find("card", "external-a1") is None
    assert restored.document.find("card", "external-a2") is None
    assert restored.document.find("card", "external-b") is None


def test_overlay_failure_after_durable_commit_quarantines_without_rollback():
    old_registry = _registry(schema={})
    restored, detached = _detach(_document(old_registry))
    sources = (("catalog", "card", "memory://catalog"),)

    with pytest.raises(ZeError) as error:
        rehydrate_runtime(
            detached,
            _declarations(_registry(sources=sources), schema_version="2"),
            authority_writer=_committed_writer(),
            read_only_loader_resolver=lambda source: (
                lambda: [{"id": "bad", "data": {"title": 7}}]
            ),
        )
    assert error.value.code == "schema_violation"
    assert restored.document.version == 2
    assert restored.document.schema["card"] == CARD_SCHEMA
    assert restored.document.find("card", "bad") is None

    retry = _issue_unpublished_document_for_test(
        restored.document, project_id="project", generation=1,
    )
    with pytest.raises(ZeError) as quarantine:
        rehydrate_runtime(retry, _declarations(_registry(), schema_version="2"))
    assert quarantine.value.context["reason"] == "runtime_document_quarantined"


def test_writer_canonical_tamper_after_committed_receipt_is_quarantined():
    old_registry = _registry(schema={})
    restored, detached = _detach(_document(old_registry))

    def writer(request):
        restored.document.get("card", "local")._data["title"] = "tampered"
        return _receipt(request)

    with pytest.raises(ZeError) as error:
        rehydrate_runtime(
            detached,
            _declarations(_registry(), schema_version="2"),
            authority_writer=writer,
        )
    assert error.value.context["reason"] == "runtime_rehydration_candidate_tampered"
    retry = _issue_unpublished_document_for_test(
        restored.document, project_id="project", generation=1,
    )
    with pytest.raises(ZeError) as quarantine:
        rehydrate_runtime(retry, _declarations(_registry(), schema_version="2"))
    assert quarantine.value.context["reason"] == "runtime_document_quarantined"
    # The terminal guard closes the exclusive mutation session even though the
    # quarantined Document itself may never be admitted.
    session = restored.document.begin_mutation_session()
    session.rollback()


def test_overlay_callback_cannot_smuggle_canonical_mutation_into_noop_ready():
    restored, detached = _detach(_document())
    sources = (("catalog", "card", "memory://catalog"),)

    def resolver(source):
        def loader():
            restored.document.get("card", "local")._data["title"] = "smuggled"
            return []
        return loader

    with pytest.raises(ZeError) as error:
        rehydrate_runtime(
            detached,
            _declarations(_registry(sources=sources)),
            read_only_loader_resolver=resolver,
        )
    assert error.value.context["reason"] == "runtime_read_only_loader_mutated_existing_state"
    assert restored.document.get("card", "local").data == {"title": "current"}


def test_resolver_cannot_rewrite_its_declaration_view():
    restored, detached = _detach(_document())
    sources = (("catalog", "card", "memory://catalog"),)

    def resolver(source):
        object.__setattr__(source, "name", "forged")
        object.__setattr__(source, "entity_type", "other")
        return lambda: []

    with pytest.raises(ZeError) as error:
        rehydrate_runtime(
            detached,
            _declarations(_registry(sources=sources)),
            read_only_loader_resolver=resolver,
        )
    assert error.value.context["reason"] == "runtime_read_only_declaration_tampered"
    assert restored.document.read_only_sources() == []
    assert "other" not in restored.document.types()


def test_final_fence_callback_cannot_tamper_overlay_before_admission():
    sources = (("catalog", "card", "memory://catalog"),)
    restored, detached = _detach(_document())

    def tampering_fence(request):
        restored.document._read_only_sources["catalog"].name = "renamed"
        restored.document._read_only_sources["catalog"].loader = lambda: []
        return _current_fence(request)

    with pytest.raises(ZeError) as error:
        _runtime_rehydrate(
            detached,
            _declarations(_registry(sources=sources)),
            authority_fence=tampering_fence,
            read_only_loader_resolver=lambda source: (
                lambda: [{"id": "external", "data": {"title": "external"}}]
            ),
        )
    assert error.value.context["reason"] == "runtime_rehydration_candidate_tampered"
    assert restored.document.read_only_sources() == []
    assert restored.document.find("card", "external") is None


def test_candidate_issuance_failure_rolls_back_and_releases_exclusive_session(monkeypatch):
    import sdk.runtime_rehydration as module

    restored, detached = _detach(_document(_registry(schema={})))

    def fail_issue(record):
        raise RuntimeError("issuance failed")

    monkeypatch.setattr(module._LEDGER, "issue_candidate", fail_issue)
    with pytest.raises(RuntimeError, match="issuance failed"):
        rehydrate_runtime(
            detached, _declarations(_registry(), schema_version="2"),
            authority_writer=_committed_writer(),
        )
    assert restored.document.version == 1
    assert restored.document.schema["card"] == {}
    session = restored.document.begin_mutation_session(exclusive=True)
    session.rollback()


def test_initial_capture_failure_releases_document_lease(monkeypatch):
    import sdk.runtime_rehydration as module

    restored, detached = _detach(_document())

    def fail_capture(*args, **kwargs):
        raise RuntimeError("capture failed")

    monkeypatch.setattr(module, "_capture_business_projection", fail_capture)
    with pytest.raises(RuntimeError, match="capture failed"):
        rehydrate_runtime(detached, _declarations())
    session = restored.document.begin_mutation_session(exclusive=True)
    session.rollback()


def test_rollback_failure_quarantines_preparation(monkeypatch):
    import sdk.runtime_rehydration as module

    def bad_migration(document):
        document._schema = {"card": {}}
        return True

    restored, detached = _detach(_document())

    def fail_rollback(self):
        raise ZeError(
            "mutation_rollback_failed", "injected", context={"reason": "injected"},
        )

    monkeypatch.setattr(module.DocumentMutationSession, "rollback", fail_rollback)
    with pytest.raises(ZeError) as error:
        rehydrate_runtime(
            detached,
            _declarations(_registry(migrations=(bad_migration,))),
        )
    assert error.value.code == "mutation_rollback_failed"
    with module._LEDGER._lock:
        assert module._LEDGER._leases[restored.document].state == "quarantined"


def test_runtime_seam_has_no_direct_io_log_publish_cache_or_candidate_surface(monkeypatch):
    import sdk.runtime_rehydration as module

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported |= {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert not ({
        "sqlite3", "pathlib", "operation", "broker", "file_store",
        "project_topology", "op_log", "repository",
    } & imported)
    assert "RuntimeRehydrationCandidate" not in module.__all__
    assert "AuthorityCommitProof" not in module.__all__
    assert "RuntimeReadyHead" not in module.__all__
    assert "detach_legacy_document" not in module.__all__

    def forbidden(*args, **kwargs):
        raise AssertionError("runtime load must not call legacy Document.save")

    monkeypatch.setattr(Document, "save", forbidden)
    ready = rehydrate_runtime(_detach(_document())[1], _declarations())
    require_runtime_ready_head(ready)
