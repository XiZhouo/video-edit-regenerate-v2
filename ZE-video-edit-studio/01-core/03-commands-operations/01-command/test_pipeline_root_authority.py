"""Active CommandRegistry root snapshots for sealed Pipeline Start issuance."""

from __future__ import annotations

import copy
import hashlib
import json
import pickle
import sys
from pathlib import Path

import pytest


HERE = Path(__file__).resolve().parent
CORE = HERE.parent.parent
for path in (
    HERE,
    CORE / "01-data-structure" / "01-document",
    CORE / "01-data-structure" / "02-entity",
    CORE / "03-commands-operations" / "02-operation",
):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from builtin_commands import register_builtin_commands  # noqa: E402
from command import (  # noqa: E402
    CommandRegistry,
    CommandSpec,
    _active_command_root_snapshot,
    _consume_pipeline_authority_root,
)
from document import DocumentRegistry  # noqa: E402
from entity import Actor, ZeError  # noqa: E402
from operation import new_operation  # noqa: E402


ROOT_COMMAND = "item.generate"
ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "status": {"type": "string"},
    },
    "additionalProperties": False,
}


def _protected_document():
    declarations = DocumentRegistry()
    declarations.declare_entity_type(
        name="item",
        schema=ITEM_SCHEMA,
        mutation_policy={
            "schema": "ze.entity-mutation-policy.v1",
            "lifecycle": {
                "create": [ROOT_COMMAND],
                "rename": [],
                "soft_delete": [],
                "restore": [],
                "hard_remove": [],
            },
            "fields": {
                "title": [ROOT_COMMAND],
                "status": [ROOT_COMMAND],
            },
        },
    )
    declarations.set_collaboration_mode("with_save")
    return declarations.build_document(schema_version="1.0", id="doc-pipeline-root")


def _empty_root_operation(actor: Actor, source: str, document_id: str):
    return new_operation(
        command_id=ROOT_COMMAND,
        source=source,
        actor=actor.to_dict(),
        patches=(),
        reverse_patches=(),
        document_id=document_id,
    )


def test_root_snapshot_is_only_available_during_exact_registered_handler():
    document = _protected_document()
    commands = CommandRegistry()
    register_builtin_commands(commands)
    captured = {}

    def _execute(*, actor, source, doc, **_kwargs):
        snapshot = _active_command_root_snapshot(
            registry=commands,
            document=doc,
        )
        captured["snapshot"] = snapshot
        captured["actor"] = snapshot.actor
        captured["source"] = snapshot.source
        captured["command_id"] = snapshot.command_id
        captured["document_id"] = snapshot.document_id
        captured["protected"] = snapshot.protected
        return _empty_root_operation(actor, source, doc.id)

    commands.register(CommandSpec(
        id=ROOT_COMMAND,
        label="Generate",
        mutation_authority=True,
        execute=_execute,
    ))
    document._validate_mutation_policy_commands(commands)
    actor = Actor(user_id="u-root", device_id="device-1", source="user")

    operation = commands.dispatch(
        ROOT_COMMAND,
        {},
        actor=actor,
        source="user",
        doc=document,
    )

    assert operation.command_id == ROOT_COMMAND
    assert captured == {
        "snapshot": captured["snapshot"],
        "actor": actor.to_dict(),
        "source": "user",
        "command_id": ROOT_COMMAND,
        "document_id": document.id,
        "protected": True,
    }
    for copier in (copy.copy, copy.deepcopy, pickle.dumps):
        with pytest.raises(TypeError):
            copier(captured["snapshot"])
    with pytest.raises(ZeError) as exc_info:
        _active_command_root_snapshot(registry=commands, document=document)
    assert exc_info.value.code == "pipeline_start_authority_required"
    assert exc_info.value.context["reason"] == "active_root_dispatch_missing"


def test_root_consume_returns_real_registration_and_scoped_document_policy_once():
    document = _protected_document()
    commands = CommandRegistry()
    register_builtin_commands(commands)
    captured = {}

    def _execute(*, actor, source, doc, **_kwargs):
        snapshot = _active_command_root_snapshot(
            registry=commands,
            document=doc,
        )
        verified = _consume_pipeline_authority_root(
            snapshot,
            document=doc,
            command_id=ROOT_COMMAND,
            document_id=doc.id,
            run_id="run-1",
            pipeline_id="item-pipeline",
            entity_types=("item",),
        )
        captured["verified"] = verified
        with pytest.raises(ZeError) as stale:
            _consume_pipeline_authority_root(
                snapshot,
                document=doc,
                command_id=ROOT_COMMAND,
                document_id=doc.id,
                run_id="run-1",
                pipeline_id="item-pipeline",
                entity_types=("item",),
            )
        assert stale.value.code == "pipeline_authority_stale"
        return _empty_root_operation(actor, source, doc.id)

    commands.register(CommandSpec(
        id=ROOT_COMMAND,
        label="Generate",
        kind="stage",
        touches=["item"],
        mutation_authority=True,
        execute=_execute,
    ))
    declaration = commands.registered_declaration(ROOT_COMMAND)
    document._validate_mutation_policy_commands(commands)

    actor = Actor(user_id="u-root", device_id="device-1", source="user")
    commands.dispatch(
        ROOT_COMMAND,
        {},
        actor=actor,
        source="ai",
        doc=document,
    )

    verified = captured["verified"]
    expected_declaration_digest = hashlib.sha256(json.dumps(
        declaration,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")).hexdigest()
    expected_schema_digest = hashlib.sha256(json.dumps(
        ITEM_SCHEMA,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")).hexdigest()
    assert verified.actor == actor.to_dict()
    assert verified.source == "ai"
    assert verified.command_id == ROOT_COMMAND
    assert verified.document_id == document.id
    assert verified.root_command_digest == expected_declaration_digest
    assert verified.document_policy == {
        "schema": "ze.pipeline-document-policy.v1",
        "entity_policies": [{
            "entity_type": "item",
            "entity_schema_digest": expected_schema_digest,
            "policy": document.mutation_policies["item"],
        }],
    }
    for copier in (copy.copy, copy.deepcopy, pickle.dumps):
        with pytest.raises(TypeError):
            copier(verified)


def test_root_consume_rejects_caller_identity_before_consuming_snapshot():
    document = _protected_document()
    commands = CommandRegistry()
    register_builtin_commands(commands)

    def _execute(*, actor, source, doc, **_kwargs):
        snapshot = _active_command_root_snapshot(
            registry=commands,
            document=doc,
        )
        with pytest.raises(ZeError) as mismatch:
            _consume_pipeline_authority_root(
                snapshot,
                document=doc,
                command_id="other.command",
                document_id=doc.id,
                run_id="run-1",
                pipeline_id="item-pipeline",
                entity_types=("item",),
            )
        assert mismatch.value.code == "pipeline_start_identity_mismatch"
        # A rejected caller projection must not burn the legitimate one-shot.
        verified = _consume_pipeline_authority_root(
            snapshot,
            document=doc,
            command_id=ROOT_COMMAND,
            document_id=doc.id,
            run_id="run-1",
            pipeline_id="item-pipeline",
            entity_types=("item",),
        )
        assert verified.command_id == ROOT_COMMAND
        return _empty_root_operation(actor, source, doc.id)

    commands.register(CommandSpec(
        id=ROOT_COMMAND,
        label="Generate",
        mutation_authority=True,
        execute=_execute,
    ))
    document._validate_mutation_policy_commands(commands)
    commands.dispatch(
        ROOT_COMMAND,
        {},
        actor=Actor(user_id="u-root"),
        source="user",
        doc=document,
    )
