"""Adversarial boundaries for runtime mutation-policy authority.

These tests deliberately use Python escape hatches such as
``object.__setattr__`` and private registry replacement.  A frozen dataclass or
public declaration is not an authority boundary if an untrusted callback can
mutate the object that the framework continues to trust.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


_HERE = Path(__file__).resolve().parent
_CORE = _HERE.parent.parent
for _path in (
    _HERE,
    _CORE / "01-data-structure" / "01-document",
    _CORE / "01-data-structure" / "02-entity",
    _CORE / "02-data-flow" / "07-persistence",
    _CORE / "03-commands-operations" / "02-operation",
):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import command as command_module
from builtin_commands import register_builtin_commands
from business_snapshot import capture_business_snapshot
from command import CommandRegistry, CommandSpec
from commit_contracts import CommitReceipt
from commit_coordinator import CommitCoordinator
from document import DocumentRegistry
from entity import Actor, Entity, ZeError
from operation import Patch, PendingConfirmation, new_operation


def _plain_document():
    registry = DocumentRegistry()
    registry.declare_entity_type(
        name="plain",
        schema={
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "additionalProperties": False,
        },
    )
    registry.set_collaboration_mode("with_save")
    return registry.build_document(schema_version="1.0", id="doc-security")


def _protected_document(*, command_id: str):
    registry = DocumentRegistry()
    registry.declare_entity_type(
        name="protected",
        schema={
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "additionalProperties": False,
        },
        mutation_policy={
            "schema": "ze.entity-mutation-policy.v1",
            "lifecycle": {
                "create": [],
                "rename": [],
                "soft_delete": [],
                "restore": [],
                "hard_remove": [],
            },
            "fields": {"value": [command_id]},
        },
    )
    registry.set_collaboration_mode("with_save")
    document = registry.build_document(schema_version="1.0", id="doc-security")
    document.add(Entity(type="protected", id="e1", data={"value": 1}))
    return document


def _empty_operation(command_id: str, actor: Actor, source: str = "user"):
    return new_operation(
        command_id=command_id,
        source=source,
        actor=actor.to_dict(),
        patches=(),
        reverse_patches=(),
    )


def _confirmation_registry(document, command_id: str = "Confirm.Update"):
    commands = CommandRegistry()
    commands.register(CommandSpec(
        id=command_id,
        label="confirm",
        requires_confirm=True,
        execute=lambda *, actor, source, **_kwargs: _empty_operation(
            command_id, actor, source,
        ),
    ))
    return commands


def _reserve_confirmation(commands, pending, actor):
    return commands.reserve_confirmation(
        pending.token,
        actor=actor,
        approval_channel="human_http",
        approval_nonce=pending.approval_nonce,
    )


def _field_operation(
    command_id: str,
    actor: Actor,
    *,
    before: int,
    after: int,
    source: str = "user",
):
    return new_operation(
        command_id=command_id,
        source=source,
        actor=actor.to_dict(),
        patches=(Patch("protected", "e1", "value", "set", before, after),),
        reverse_patches=(
            Patch("protected", "e1", "value", "set", after, before),
        ),
    )


class _ForbiddenCommitStore:
    """Prove a rejected protected command never reaches persistence."""

    def __init__(self):
        self.calls = 0

    def commit(self, _intent):
        self.calls += 1
        raise AssertionError("rejected protected mutation reached persistence")


def _register_noop_authority(
    commands,
    command_id,
    *,
    execute=None,
    poll=None,
    requires_confirm=False,
):
    if execute is None:
        execute = lambda *, actor, source, **_kwargs: _empty_operation(
            command_id, actor, source,
        )
    commands.register(CommandSpec(
        id=command_id,
        label="sealed business command",
        mutation_authority=True,
        poll=poll,
        requires_confirm=requires_confirm,
        confirm_template="confirm {target}",
        execute=execute,
    ))


def test_handler_actor_mutation_then_raise_never_pollutes_caller_actor():
    document = _plain_document()
    commands = CommandRegistry()
    caller_actor = Actor(user_id="owner")
    before = caller_actor.to_dict()

    def execute(*, args, actor, source, doc):
        object.__setattr__(actor, "user_id", "victim")
        raise RuntimeError("injected handler failure")

    commands.register(CommandSpec(id="Actor.Attack", label="attack", execute=execute))

    with pytest.raises(ZeError) as captured:
        commands.dispatch(
            "Actor.Attack", {}, actor=caller_actor, source="user", doc=document,
        )

    assert captured.value.code == "mutation_actor_authority_mutated"
    assert caller_actor.to_dict() == before


def test_governor_actor_mutation_then_raise_never_pollutes_caller_actor():
    class MutatingGovernor:
        @staticmethod
        def guard(spec, args, actor):
            del spec, args
            object.__setattr__(actor, "user_id", "victim")
            raise RuntimeError("injected governor failure")

    document = _plain_document()
    commands = CommandRegistry(ai_governor=MutatingGovernor())
    caller_actor = Actor(user_id="owner", source="ai")
    before = caller_actor.to_dict()
    commands.register(CommandSpec(
        id="Actor.AiAttack",
        label="attack",
        execute=lambda *, actor, source, **_kwargs: _empty_operation(
            "Actor.AiAttack", actor, source,
        ),
    ))

    with pytest.raises(ZeError) as captured:
        commands.dispatch(
            "Actor.AiAttack", {}, actor=caller_actor, source="ai", doc=document,
        )

    assert captured.value.code == "mutation_actor_authority_mutated"
    assert caller_actor.to_dict() == before


def test_mutated_callback_actor_cannot_authorize_protected_entity_delta():
    command_id = "Business.Update"
    document = _protected_document(command_id=command_id)
    entity = document.get("protected", "e1")
    before = entity.to_dict()
    caller_actor = Actor(user_id="owner")
    commands = CommandRegistry()

    def execute(*, args, actor, source, doc):
        del args, source, doc
        object.__setattr__(actor, "user_id", "victim")
        entity.update({"value": 2}, expected_version=1, actor=actor)
        object.__setattr__(actor, "user_id", "owner")
        return _field_operation(command_id, actor, before=1, after=2)

    commands.register(CommandSpec(
        id=command_id,
        label="business update",
        mutation_authority=True,
        execute=execute,
    ))
    document._validate_mutation_policy_commands(commands)
    store = _ForbiddenCommitStore()

    with pytest.raises(ZeError) as captured:
        CommitCoordinator(document, store).execute(
            lambda: commands.dispatch(
                command_id, {}, actor=caller_actor, source="user", doc=document,
            )
        )

    assert captured.value.code == "mutation_policy_denied"
    assert captured.value.context["reason"] == "mutation_policy_actor_identity_mismatch"
    assert entity.to_dict() == before
    assert caller_actor.to_dict() == {"user_id": "owner", "device_id": "default", "source": "user"}
    assert store.calls == 0


def test_registration_time_mutation_authority_cannot_be_flipped_for_protected_root():
    command_id = "Business.Update"
    document = _protected_document(command_id=command_id)
    entity = document.get("protected", "e1")
    commands = CommandRegistry()

    def execute(*, args, actor, source, doc):
        del args, source, doc
        entity.update({"value": 2}, expected_version=1, actor=actor)
        return _field_operation(command_id, actor, before=1, after=2)

    spec = CommandSpec(
        id=command_id,
        label="business update",
        mutation_authority=True,
        execute=execute,
    )
    commands.register(spec)
    document._validate_mutation_policy_commands(commands)
    spec.mutation_authority = False
    store = _ForbiddenCommitStore()

    with pytest.raises(ZeError) as captured:
        CommitCoordinator(document, store).execute(
            lambda: commands.dispatch(
                command_id,
                {},
                actor=Actor(user_id="owner"),
                source="user",
                doc=document,
            )
        )

    assert captured.value.code == "mutation_command_registry_corrupted"
    assert entity.data["value"] == 1
    assert store.calls == 0


@pytest.mark.parametrize("attack", ["execute", "spec"])
def test_temporary_registry_replacement_cannot_run_then_restore_evil_handler(attack):
    command_id = "Business.Update"
    document = _protected_document(command_id=command_id)
    entity = document.get("protected", "e1")
    commands = CommandRegistry()
    calls: list[str] = []

    def honest(*, actor, source, **_kwargs):
        calls.append("honest")
        return _empty_operation(command_id, actor, source)

    original = CommandSpec(
        id=command_id,
        label="business update",
        mutation_authority=True,
        execute=honest,
    )
    commands.register(original)
    document._validate_mutation_policy_commands(commands)

    def evil(*, actor, source, **_kwargs):
        calls.append("evil")
        if attack == "execute":
            original.execute = honest
        else:
            commands._specs[command_id] = original
        entity.update({"value": 2}, expected_version=1, actor=actor)
        return _field_operation(
            command_id, actor, before=1, after=2, source=source,
        )

    if attack == "execute":
        original.execute = evil
    else:
        commands._specs[command_id] = CommandSpec(
            id=command_id,
            label="forged replacement",
            mutation_authority=True,
            execute=evil,
        )

    try:
        commands.dispatch(
            command_id, {}, actor=Actor(user_id="owner"), source="user", doc=document,
        )
    except ZeError as exc:
        assert exc.code == "mutation_command_registry_corrupted"

    assert "evil" not in calls
    assert entity.data["value"] == 1


def test_builtin_generic_command_cannot_be_promoted_after_registration():
    commands = CommandRegistry()
    register_builtin_commands(commands)
    commands.get("entity.update").mutation_authority = True
    document = _protected_document(command_id="entity.update")

    with pytest.raises(ZeError) as captured:
        document._validate_mutation_policy_commands(commands)

    assert captured.value.code == "mutation_command_registry_corrupted"


def _registered_protected_update(**spec_overrides):
    command_id = "Business.Update"
    document = _protected_document(command_id=command_id)
    entity = document.get("protected", "e1")
    commands = CommandRegistry(ai_governor=spec_overrides.pop("ai_governor", None))
    calls: list[str] = []

    def execute(*, actor, source, **_kwargs):
        calls.append(source)
        entity.update({"value": 2}, expected_version=1, actor=actor)
        return _field_operation(
            command_id, actor, before=1, after=2, source=source,
        )

    spec = CommandSpec(
        id=command_id,
        label="business update",
        mutation_authority=True,
        execute=execute,
        **spec_overrides,
    )
    commands.register(spec)
    document._validate_mutation_policy_commands(commands)
    return document, entity, commands, spec, calls


def test_registered_sources_cannot_be_relaxed_before_protected_dispatch():
    _document, entity, commands, spec, calls = _registered_protected_update(
        sources_allowed=("user",),
    )
    spec.sources_allowed = ("user", "system")

    with pytest.raises(ZeError) as captured:
        commands.dispatch(
            "Business.Update",
            {},
            actor=Actor(user_id="__system__", source="system"),
            source="system",
            doc=_document,
        )

    assert captured.value.code in {"acl_denied", "mutation_command_registry_corrupted"}
    assert calls == []
    assert entity.data["value"] == 1


def test_registered_human_confirmation_cannot_be_disabled_before_dispatch():
    document, entity, commands, spec, calls = _registered_protected_update(
        requires_confirm=True,
    )
    spec.requires_confirm = False

    result = commands.dispatch(
        "Business.Update", {}, actor=Actor(user_id="owner"), source="user", doc=document,
    )

    assert isinstance(result, PendingConfirmation)
    assert calls == []
    assert entity.data["value"] == 1


def test_registered_poll_cannot_be_replaced_to_bypass_precondition():
    document, entity, commands, spec, calls = _registered_protected_update(
        poll=lambda _args: False,
    )
    spec.poll = lambda _args: True

    with pytest.raises(ZeError) as captured:
        commands.dispatch(
            "Business.Update", {}, actor=Actor(user_id="owner"), source="user", doc=document,
        )

    assert captured.value.code in {"poll_failed", "mutation_command_registry_corrupted"}
    assert calls == []
    assert entity.data["value"] == 1


def test_registered_ai_risk_cannot_be_lowered_before_governor():
    class RiskGovernor:
        @staticmethod
        def guard(spec, args, actor):
            del args, actor
            return SimpleNamespace(
                require_confirm=bool(spec.ai_high_risk or spec.ai_cost_usd >= 10.0),
            )

    document, entity, commands, spec, calls = _registered_protected_update(
        ai_governor=RiskGovernor(),
        ai_high_risk=True,
        ai_cost_usd=10.0,
    )
    spec.ai_high_risk = False
    spec.ai_cost_usd = 0.0

    result = commands.dispatch(
        "Business.Update",
        {},
        actor=Actor(user_id="worker", source="ai"),
        source="ai",
        doc=document,
    )

    assert isinstance(result, PendingConfirmation)
    assert calls == []
    assert entity.data["value"] == 1


def test_new_authority_bit_does_not_change_unprotected_command_compatibility():
    document = _plain_document()
    commands = CommandRegistry()
    calls: list[str] = []

    def execute(*, actor, source, **_kwargs):
        calls.append(actor.user_id)
        return _empty_operation("Plain.Update", actor, source)

    spec = CommandSpec(id="Plain.Update", label="plain", execute=execute)
    commands.register(spec)
    # This new declaration bit is irrelevant when the command neither mutates
    # nor claims a protected type.  Per-type rollout must not impose a new
    # global registry invariant on legacy commands.
    spec.mutation_authority = True

    result = commands.dispatch(
        "Plain.Update", {}, actor=Actor(user_id="owner"), source="user", doc=document,
    )

    assert result.command_id == "Plain.Update"
    assert calls == ["owner"]


def test_confirmation_document_authority_is_cleared_by_fork_reset():
    document = _plain_document()
    commands = _confirmation_registry(document)
    actor = Actor(user_id="owner")
    pending = commands.dispatch(
        "Confirm.Update", {}, actor=actor, source="user", doc=document,
    )
    assert isinstance(pending, PendingConfirmation)
    assert commands.pending_store._document_authority[pending.token] is document

    command_module._reset_command_dispatch_state_after_fork()

    assert commands.pending_store.state(pending.token) is None
    assert commands.pending_store._document_authority == {}


def test_confirmation_cancel_clears_bound_document_authority():
    document = _plain_document()
    commands = _confirmation_registry(document)
    actor = Actor(user_id="owner")
    pending = commands.dispatch(
        "Confirm.Update", {}, actor=actor, source="user", doc=document,
    )

    assert commands.pending_store.cancel(pending.token, actor=actor) is True
    assert commands.pending_store.state(pending.token) is None
    assert pending.token not in commands.pending_store._document_authority


@pytest.mark.parametrize("state", ["reserved", "frozen"])
def test_confirmation_fork_reset_clears_nonpending_authority(state):
    document = _plain_document()
    commands = _confirmation_registry(document)
    actor = Actor(user_id="owner")
    pending = commands.dispatch(
        "Confirm.Update", {}, actor=actor, source="user", doc=document,
    )
    reservation = _reserve_confirmation(commands, pending, actor)
    if state == "frozen":
        commands.pending_store.freeze(
            reservation,
            code="commit_indeterminate",
            message="repair required",
        )
    assert commands.pending_store.state(pending.token) == state
    assert commands.pending_store._document_authority[pending.token] is document

    command_module._reset_command_dispatch_state_after_fork()

    assert commands.pending_store.state(pending.token) is None
    assert commands.pending_store._document_authority == {}


def test_confirmation_consume_clears_bound_document_authority():
    document = _plain_document()
    commands = _confirmation_registry(document)
    actor = Actor(user_id="owner")
    pending = commands.dispatch(
        "Confirm.Update", {}, actor=actor, source="user", doc=document,
    )
    reservation = _reserve_confirmation(commands, pending, actor)

    commands.pending_store.consume(reservation)

    assert commands.pending_store.state(pending.token) is None
    assert pending.token not in commands.pending_store._document_authority


def test_confirmation_expiry_prunes_bound_document_authority():
    document = _plain_document()
    commands = _confirmation_registry(document)
    commands.pending_store._pending_ttl_sec = 0.001
    actor = Actor(user_id="owner")
    expired = commands.dispatch(
        "Confirm.Update", {"attempt": 1}, actor=actor, source="user", doc=document,
    )
    time.sleep(0.01)
    current = commands.dispatch(
        "Confirm.Update", {"attempt": 2}, actor=actor, source="user", doc=document,
    )

    assert commands.pending_store.state(expired.token) is None
    assert expired.token not in commands.pending_store._document_authority
    assert commands.pending_store._document_authority[current.token] is document


def test_patch_projection_is_deeply_detached_from_frozen_audit_body():
    patch = Patch(
        "protected",
        "e1",
        "value",
        "set",
        {"nested": [1]},
        {"nested": [2]},
        provenance={
            "field": "value",
            "before": {"present": False, "value": None},
            "after": {
                "present": True,
                "value": {
                    "actor": {"user_id": "owner"},
                    "model": "m",
                },
            },
        },
    )

    projected = patch.to_dict()
    projected["before"]["nested"].append(9)
    projected["after"]["nested"].append(9)
    projected["provenance"]["after"]["value"]["actor"]["user_id"] = "victim"

    fresh = patch.to_dict()
    assert fresh["before"] == {"nested": [1]}
    assert fresh["after"] == {"nested": [2]}
    assert fresh["provenance"]["after"]["value"]["actor"] == {"user_id": "owner"}


def test_operation_projection_detaches_nested_error_and_external_envelopes():
    operation = new_operation(
        command_id="Business.Update",
        source="user",
        actor={"user_id": "owner", "device_id": "default", "source": "user"},
        patches=(),
        reverse_patches=(),
        error={"context": {"attempts": [1]}},
        external={"target_ref": {"entity_type": "protected", "entity_id": "e1"}},
    )

    projected = operation.to_dict()
    projected["error"]["context"]["attempts"].append(2)
    projected["external"]["target_ref"]["entity_id"] = "victim"

    fresh = operation.to_dict()
    assert fresh["error"] == {"context": {"attempts": [1]}}
    assert fresh["external"] == {
        "target_ref": {"entity_type": "protected", "entity_id": "e1"},
    }


def test_patch_and_operation_own_all_mutable_construction_inputs():
    before = {"nested": [1]}
    after = {"nested": [2]}
    provenance = {
        "field": "value",
        "before": {"present": False, "value": None},
        "after": {"present": True, "value": {"actor": {"user_id": "owner"}}},
    }
    ancestry = {
        "field": "container",
        "before": {"present": True, "value": {"nested": [1]}},
        "after": {"present": True, "value": {"nested": [2]}},
    }
    actor = {
        "user_id": "owner",
        "device_id": "default",
        "source": "user",
    }
    error = {"context": {"attempts": [1]}}
    external = {"target_ref": {"entity_type": "protected", "entity_id": "e1"}}
    patch = Patch(
        "protected", "e1", "value", "set", before, after,
        provenance=provenance,
        ancestry=ancestry,
    )
    operation = new_operation(
        command_id="Business.Update",
        source="user",
        actor=actor,
        patches=(patch,),
        reverse_patches=(
            Patch(
                "protected", "e1", "value", "set", after, before,
                provenance={
                    "field": "value",
                    "before": provenance["after"],
                    "after": provenance["before"],
                },
                ancestry={
                    "field": ancestry["field"],
                    "before": ancestry["after"],
                    "after": ancestry["before"],
                },
            ),
        ),
        error=error,
        external=external,
    )
    baseline = operation.to_dict()

    before["nested"].append(9)
    after["nested"].append(9)
    provenance["after"]["value"]["actor"]["user_id"] = "victim"
    ancestry["after"]["value"]["nested"].append(9)
    actor["user_id"] = "victim"
    error["context"]["attempts"].append(2)
    external["target_ref"]["entity_id"] = "victim"

    assert operation.to_dict() == baseline


def test_public_patch_and_operation_views_do_not_expose_mutable_authority():
    patch = Patch(
        "protected",
        "e1",
        "value",
        "set",
        {"nested": [1]},
        {"nested": [2]},
        provenance={
            "field": "value",
            "before": {"present": False, "value": None},
            "after": {"present": True, "value": {"actor": {"user_id": "owner"}}},
        },
        ancestry={
            "field": "container",
            "before": {"present": True, "value": {"nested": [1]}},
            "after": {"present": True, "value": {"nested": [2]}},
        },
    )
    operation = new_operation(
        command_id="Business.Update",
        source="user",
        actor={"user_id": "owner", "device_id": "default", "source": "user"},
        patches=(patch,),
        reverse_patches=(),
        error={"context": {"attempts": [1]}},
        external={"target_ref": {"entity_type": "protected", "entity_id": "e1"}},
    )
    baseline = operation.to_dict()

    mutable_views = (
        lambda: patch.before["nested"].append(9),
        lambda: patch.after["nested"].append(9),
        lambda: patch.provenance["after"]["value"]["actor"].update(user_id="victim"),
        lambda: patch.ancestry["after"]["value"]["nested"].append(9),
        lambda: operation.actor.update(user_id="victim"),
        lambda: operation.error["context"]["attempts"].append(2),
        lambda: operation.external["target_ref"].update(entity_id="victim"),
    )
    for mutate in mutable_views:
        try:
            mutate()
        except (AttributeError, KeyError, TypeError):
            # An immutable public read view is also safe; the authority must
            # remain unchanged either way.
            pass

    assert operation.to_dict() == baseline


def test_store_cannot_change_validated_operation_through_public_views():
    command_id = "Business.Update"
    registry = DocumentRegistry()
    registry.declare_entity_type(
        name="protected",
        schema={
            "type": "object",
            "properties": {
                "value": {
                    "type": "object",
                    "properties": {
                        "nested": {"type": "array", "items": {"type": "integer"}},
                    },
                    "required": ["nested"],
                    "additionalProperties": False,
                },
            },
            "additionalProperties": False,
        },
        mutation_policy={
            "schema": "ze.entity-mutation-policy.v1",
            "lifecycle": {
                "create": [],
                "rename": [],
                "soft_delete": [],
                "restore": [],
                "hard_remove": [],
            },
            "fields": {"value": [command_id]},
        },
    )
    registry.set_collaboration_mode("with_save")
    document = registry.build_document(schema_version="1.0", id="doc-store-attack")
    entity = document.add(Entity(
        type="protected", id="e1", data={"value": {"nested": [1]}},
    ))
    actor = Actor(user_id="owner")
    commands = CommandRegistry()

    def execute(*, args, actor, source, doc):
        del args, doc
        before = {"nested": [1]}
        after = {"nested": [2]}
        entity.update({"value": after}, expected_version=1, actor=actor)
        return new_operation(
            command_id=command_id,
            source=source,
            actor=actor.to_dict(),
            patches=(Patch("protected", "e1", "value", "set", before, after),),
            reverse_patches=(
                Patch("protected", "e1", "value", "set", after, before),
            ),
            external={
                "target_ref": {"entity_type": "protected", "entity_id": "e1"},
            },
        )

    commands.register(CommandSpec(
        id=command_id,
        label="business update",
        mutation_authority=True,
        execute=execute,
    ))
    document._validate_mutation_policy_commands(commands)

    class MutatingStore:
        def __init__(self):
            self.body = None

        def commit(self, intent):
            operation = intent.operations[0]
            for mutate in (
                lambda: operation.patches[0].after["nested"].append(9),
                lambda: operation.external["target_ref"].update(entity_id="victim"),
            ):
                try:
                    mutate()
                except (AttributeError, KeyError, TypeError):
                    pass
            self.body = operation.to_dict()
            document._v += 1
            return CommitReceipt.committed(
                commit_id=intent.commit_id,
                document_id=intent.document_id,
                operation_ids=intent.operation_ids,
                trace_id=intent.trace_id,
                save_policy=intent.save_policy,
                document_version=document.version,
            )

    store = MutatingStore()
    coordinator = CommitCoordinator(document, store)
    execution = coordinator.execute(lambda: commands.dispatch(
        command_id, {}, actor=actor, source="user", doc=document,
    ))

    assert execution.is_committed
    assert entity.data["value"] == {"nested": [2]}
    assert store.body["patches"][0]["after"] == {"nested": [2]}
    assert store.body["external"]["target_ref"]["entity_id"] == "e1"
    assert execution.events[0]["changes"][0]["patches"][0]["after"] == {
        "nested": [2],
    }


def test_other_registry_with_same_id_declaration_and_handler_is_rejected_preflight():
    command_id = "Business.Update"
    document = _protected_document(command_id=command_id)
    other = _protected_document(command_id=command_id)
    calls = []

    def shared_handler(*, actor, source, **_kwargs):
        calls.append("handler")
        return _empty_operation(command_id, actor, source)

    first = CommandRegistry()
    second = CommandRegistry()
    _register_noop_authority(first, command_id, execute=shared_handler)
    _register_noop_authority(second, command_id, execute=shared_handler)
    document._validate_mutation_policy_commands(first)
    other._validate_mutation_policy_commands(second)

    with pytest.raises(ZeError) as captured:
        second.dispatch(
            command_id, {}, actor=Actor(user_id="owner"), source="user",
            doc=document,
        )

    assert captured.value.code == "mutation_policy_registry_authority_mismatch"
    assert captured.value.context["reason"] == "dispatch_registry_generation_mismatch"
    assert calls == []


def test_coherent_private_registry_graph_replacement_cannot_replace_sealed_binding():
    command_id = "Business.Update"
    document = _protected_document(command_id=command_id)
    commands = CommandRegistry()
    calls = []

    def honest(*, actor, source, **_kwargs):
        calls.append("honest")
        return _empty_operation(command_id, actor, source)

    _register_noop_authority(commands, command_id, execute=honest)
    document._validate_mutation_policy_commands(commands)

    def evil(*, actor, source, **_kwargs):
        calls.append("evil")
        return _empty_operation(command_id, actor, source)

    forged_spec = CommandSpec(
        id=command_id,
        label="sealed business command",
        mutation_authority=True,
        confirm_template="confirm {target}",
        execute=evil,
    )
    forged_record = command_module._RegisteredCommand.capture(forged_spec)
    commands._specs[command_id] = forged_spec
    commands._registered_commands[command_id] = forged_record
    commands._mutation_policy_attestations[command_id] = (
        commands._capture_mutation_policy_attestation(forged_record)
    )

    with pytest.raises(ZeError) as captured:
        commands.dispatch(
            command_id, {}, actor=Actor(user_id="owner"), source="user",
            doc=document,
        )

    assert captured.value.code == "mutation_policy_registry_authority_mismatch"
    assert captured.value.context["reason"] == "dispatch_command_binding_mismatch"
    assert calls == []


@pytest.mark.parametrize("field", ("execute", "poll", "label", "mutation_authority"))
def test_in_place_registered_record_mutation_is_rejected_before_callback(field):
    command_id = "Business.Update"
    document = _protected_document(command_id=command_id)
    commands = CommandRegistry()
    calls = []

    def honest(*, actor, source, **_kwargs):
        calls.append("honest")
        return _empty_operation(command_id, actor, source)

    _register_noop_authority(commands, command_id, execute=honest)
    document._validate_mutation_policy_commands(commands)
    record = commands._registered_commands[command_id]
    replacement = {
        "execute": lambda **_kwargs: calls.append("evil"),
        "poll": lambda _args: calls.append("poll") or True,
        "label": "forged",
        "mutation_authority": False,
    }[field]
    object.__setattr__(record, field, replacement)

    with pytest.raises(ZeError) as captured:
        commands.dispatch(
            command_id, {}, actor=Actor(user_id="owner"), source="user",
            doc=document,
        )

    assert captured.value.code == "mutation_command_registry_corrupted"
    assert calls == []


def test_sealed_registry_rejects_late_registration_without_poisoning_existing_binding():
    command_id = "Business.Update"
    document = _protected_document(command_id=command_id)
    commands = CommandRegistry()
    calls = []

    def execute(*, actor, source, **_kwargs):
        calls.append("existing")
        return _empty_operation(command_id, actor, source)

    _register_noop_authority(commands, command_id, execute=execute)
    document._validate_mutation_policy_commands(commands)

    with pytest.raises(ZeError) as captured:
        _register_noop_authority(commands, "Business.Late")
    assert captured.value.code == "mutation_command_registry_sealed"

    commands.dispatch(
        command_id, {}, actor=Actor(user_id="owner"), source="user",
        doc=document,
    )
    assert calls == ["existing"]


def test_registry_mismatch_precedes_poll_governor_confirm_render_session_and_pending(
    monkeypatch,
):
    command_id = "Business.Update"
    document = _protected_document(command_id=command_id)
    other = _protected_document(command_id=command_id)
    calls = {name: 0 for name in (
        "poll", "governor", "render", "session", "handler",
    )}

    class Governor:
        @staticmethod
        def guard(*_args):
            calls["governor"] += 1
            return SimpleNamespace(require_confirm=False)

    class RenderProbe:
        def __str__(self):
            calls["render"] += 1
            return "target"

    def poll(_args):
        calls["poll"] += 1
        return True

    def execute(*, actor, source, **_kwargs):
        calls["handler"] += 1
        return _empty_operation(command_id, actor, source)

    first = CommandRegistry(ai_governor=Governor())
    second = CommandRegistry(ai_governor=Governor())
    for registry in (first, second):
        _register_noop_authority(
            registry,
            command_id,
            execute=execute,
            poll=poll,
            requires_confirm=True,
        )
    document._validate_mutation_policy_commands(first)
    other._validate_mutation_policy_commands(second)
    original_begin = type(document).begin_mutation_session

    def observed_begin(self, *args, **kwargs):
        calls["session"] += 1
        return original_begin(self, *args, **kwargs)

    monkeypatch.setattr(type(document), "begin_mutation_session", observed_begin)
    with pytest.raises(ZeError) as captured:
        second.dispatch(
            command_id,
            {"target": RenderProbe()},
            actor=Actor(user_id="ai-owner", source="ai"),
            source="ai",
            doc=document,
        )

    assert captured.value.code == "mutation_policy_registry_authority_mismatch"
    assert calls == {name: 0 for name in calls}
    assert second.pending_store._entries == {}


def test_stale_sealed_binding_rejects_confirm_without_consuming_reserved_grant():
    command_id = "Business.Update"
    document = _protected_document(command_id=command_id)
    commands = CommandRegistry()
    calls = []

    def execute(*, actor, source, **_kwargs):
        calls.append("handler")
        return _empty_operation(command_id, actor, source)

    _register_noop_authority(
        commands,
        command_id,
        execute=execute,
        requires_confirm=True,
    )
    document._validate_mutation_policy_commands(commands)
    actor = Actor(user_id="owner")
    pending = commands.dispatch(
        command_id, {"target": "card"}, actor=actor, source="user", doc=document,
    )
    reservation = _reserve_confirmation(commands, pending, actor)

    # Corrupt the exact registered binding only after the human grant exists.
    # confirm() must re-enter the same root preflight and leave the lease intact.
    record = commands._registered_commands[command_id]
    object.__setattr__(record, "label", "tampered after approval")
    with pytest.raises(ZeError) as captured:
        commands.confirm(reservation, actor=actor, doc=document)

    assert captured.value.code == "mutation_command_registry_corrupted"
    assert calls == []
    assert commands.pending_store.state(pending.token) == "reserved"
    assert commands.pending_store._document_authority[pending.token] is document
    assert getattr(command_module._CONFIRMATION_DISPATCH_LOCAL, "grant", None) is None


def test_snapshot_rollback_restores_exact_registry_seal_identities():
    command_id = "Business.Update"
    document = _protected_document(command_id=command_id)
    commands = CommandRegistry()
    _register_noop_authority(commands, command_id)
    document._validate_mutation_policy_commands(commands)
    original = (
        document._mutation_policy_registry_authority,
        document._mutation_policy_registry_epoch,
        document._mutation_policy_registry_pid,
        document._mutation_policy_command_authorities,
        document._mutation_policy_command_authorities[command_id],
    )

    session = document.begin_mutation_session()
    document._mutation_policy_registry_authority = object()
    document._mutation_policy_registry_epoch = object()
    document._mutation_policy_registry_pid = -1
    document._mutation_policy_command_authorities = {command_id: object()}
    session.rollback()

    assert document._mutation_policy_registry_authority is original[0]
    assert document._mutation_policy_registry_epoch is original[1]
    assert document._mutation_policy_registry_pid == original[2]
    assert document._mutation_policy_command_authorities is original[3]
    assert document._mutation_policy_command_authorities[command_id] is original[4]
    commands.dispatch(
        command_id, {}, actor=Actor(user_id="owner"), source="user",
        doc=document,
    )


def test_registry_seal_is_runtime_only_and_does_not_change_business_snapshot_bytes():
    command_id = "Business.Update"
    document = _protected_document(command_id=command_id)
    before = capture_business_snapshot("project", 1, document).snapshot
    commands = CommandRegistry()
    _register_noop_authority(commands, command_id)
    document._validate_mutation_policy_commands(commands)
    after = capture_business_snapshot("project", 1, document).snapshot

    assert after == before


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_inherited_registry_and_document_seal_fail_closed_after_real_fork():
    command_id = "Business.Update"
    document = _protected_document(command_id=command_id)
    commands = CommandRegistry()
    _register_noop_authority(commands, command_id)
    document._validate_mutation_policy_commands(commands)
    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child == 0:
        try:
            os.close(read_fd)
            try:
                commands.dispatch(
                    command_id,
                    {},
                    actor=Actor(user_id="child"),
                    source="user",
                    doc=document,
                )
            except ZeError as exc:
                result = exc.code
            else:
                result = "unexpected_success"
            os.write(write_fd, result.encode("utf-8", "strict"))
        finally:
            os.close(write_fd)
            os._exit(0)

    os.close(write_fd)
    try:
        result = os.read(read_fd, 4096).decode("utf-8", "strict")
    finally:
        os.close(read_fd)
        waited, status = os.waitpid(child, 0)
    assert waited == child
    assert os.waitstatus_to_exitcode(status) == 0
    assert result == "mutation_command_registry_process_mismatch"

    # The parent process retains its exact epoch and remains usable.
    commands.dispatch(
        command_id, {}, actor=Actor(user_id="parent"), source="user",
        doc=document,
    )
